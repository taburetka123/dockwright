import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SELFFIX_TRIGGER = REPO_ROOT / "deploy" / "scripts" / "selffix-trigger.sh"
LOOP_LABEL_PREFIX = REPO_ROOT / "deploy" / "scripts" / "loop-label-prefix.sh"


@pytest.fixture
def selffix(tmp_path, monkeypatch):
    home = tmp_path
    monkeypatch.setenv("HOME", str(home))
    scripts_dir = home / ".claude" / "scripts"
    scripts_dir.mkdir(parents=True)
    script_path = scripts_dir / "selffix-trigger.sh"
    shutil.copy(SELFFIX_TRIGGER, script_path)
    script_path.chmod(0o755)
    shutil.copy(LOOP_LABEL_PREFIX, scripts_dir / "loop-label-prefix.sh")
    shutil.copy(REPO_ROOT / "deploy" / "scripts" / "transcript_signal.py",
                scripts_dir / "transcript_signal.py")
    run_stub = scripts_dir / "selffix-run.sh"
    run_stub.write_text("#!/bin/bash\nexit 0\n")
    run_stub.chmod(0o755)
    (home / ".claude" / "selffix-debug").touch()
    return {
        "home": home,
        "script": script_path,
        "log": home / ".claude" / "dockwright" / "selffix" / "trigger.log",
    }


def _write_transcript(home: Path, sid: str, events: list) -> Path:
    project_dir = home / ".claude" / "projects" / "fake-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return path


def _write_active_record(home: Path, sid: str, agent: str) -> None:
    active_dir = home / ".claude" / "dockwright" / "active"
    active_dir.mkdir(parents=True, exist_ok=True)
    (active_dir / f"{sid}.json").write_text(json.dumps({
        "claude_sid": sid,
        "agent": agent,
        "name": f"{agent}-test",
    }))


def _invoke(selffix, sid: str, transcript: Path, dockwright_config=None) -> str:
    payload = json.dumps({"session_id": sid, "transcript_path": str(transcript)})
    env = {**os.environ, "HOME": str(selffix["home"])}
    env.pop("SELFFIX_DEBUG", None)
    if dockwright_config is not None:
        env["DOCKWRIGHT_CONFIG"] = dockwright_config
    subprocess.run(
        ["bash", str(selffix["script"])],
        input=payload, text=True, timeout=15, check=False,
        capture_output=True, env=env,
    )
    assert selffix["log"].is_file(), "no log written — DEBUG not enabled or script failed silently"
    lines = [ln for ln in selffix["log"].read_text().splitlines() if ln.strip()]
    assert lines, "log file empty"
    return lines[-1]


def _outcome(log_line: str) -> str:
    parts = log_line.split("  ")
    assert len(parts) >= 2, f"unexpected log shape: {log_line!r}"
    return parts[1]


def _assistant_tool_use(name: str, inp: dict | None = None) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "name": name, "input": inp or {}}],
        },
    }


def _user_text(text: str) -> dict:
    return {"type": "user", "message": {"content": text}}


def test_no_high_when_spawn_worker_tool_use(selffix):
    transcript = _write_transcript(selffix["home"], "sid-1", [
        _user_text("dispatch the rebase worker"),
        _assistant_tool_use("mcp__claude-orchestrator__spawn_worker", {"name": "rebase"}),
    ])
    line = _invoke(selffix, "sid-1", transcript)
    assert _outcome(line) == "none", f"expected none, got: {line!r}"


def test_no_high_when_worker_done_tool_use(selffix):
    transcript = _write_transcript(selffix["home"], "sid-2", [
        _user_text("rebase the branch"),
        _assistant_tool_use("mcp__claude-orchestrator__worker_done", {"summary": "done"}),
    ])
    line = _invoke(selffix, "sid-2", transcript)
    assert _outcome(line) == "none", f"expected none, got: {line!r}"


def test_high_when_five_edit_or_write_tool_uses(selffix):
    events = [_user_text("apply the patches")]
    for i in range(3):
        events.append(_assistant_tool_use("Edit", {"file_path": f"/x/{i}.py"}))
    for i in range(2):
        events.append(_assistant_tool_use("Write", {"file_path": f"/x/new-{i}.py"}))
    transcript = _write_transcript(selffix["home"], "sid-3", events)
    line = _invoke(selffix, "sid-3", transcript)
    assert _outcome(line) == "spawn", f"expected high/spawn, got: {line!r}"


def test_high_when_pr_create_inside_multiline_bash(selffix):
    multiline_cmd = (
        "set -e\n"
        "TITLE=\"fix: thing\"\n"
        "git push -u origin HEAD\n"
        "gh pr create --title \"$TITLE\" --body \"see ticket\" --assignee @me\n"
    )
    transcript = _write_transcript(selffix["home"], "sid-4", [
        _user_text("open the PR"),
        _assistant_tool_use("Bash", {"command": multiline_cmd}),
    ])
    line = _invoke(selffix, "sid-4", transcript)
    assert _outcome(line) == "spawn", f"expected high/spawn, got: {line!r}"


def test_high_when_session_is_manager_agent(selffix):
    transcript = _write_transcript(selffix["home"], "sid-5", [
        _user_text("hello"),
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
    ])
    _write_active_record(selffix["home"], "sid-5", agent="manager")
    line = _invoke(selffix, "sid-5", transcript)
    assert _outcome(line) == "spawn", f"expected high/spawn, got: {line!r}"


def test_none_when_no_signals(selffix):
    events = [
        _user_text("look at the file"),
        _assistant_tool_use("Read", {"file_path": "/x/a.py"}),
        _assistant_tool_use("Edit", {"file_path": "/x/a.py"}),
        _assistant_tool_use("Edit", {"file_path": "/x/b.py"}),
    ]
    transcript = _write_transcript(selffix["home"], "sid-6", events)
    line = _invoke(selffix, "sid-6", transcript)
    assert _outcome(line) == "none", f"expected none, got: {line!r}"


@pytest.mark.parametrize("skill", [
    "planner:writing-plans",
    "planner:executing-plans",
    "planner:subagent-driven-development",
])
def test_configured_high_skill_triggers_high(selffix, tmp_path, skill):
    cfg = tmp_path / "dw.toml"
    cfg.write_text(f'[gardener]\nhigh_skills = ["{skill}"]\n')
    transcript = _write_transcript(selffix["home"], "sid-hs", [
        _user_text("plan this out"),
        _assistant_tool_use("Skill", {"skill": skill}),
    ])
    line = _invoke(selffix, "sid-hs", transcript, dockwright_config=str(cfg))
    assert _outcome(line) == "spawn", f"expected spawn (configured high skill), got: {line!r}"


def test_skill_high_off_by_default(selffix):
    transcript = _write_transcript(selffix["home"], "sid-hs-off", [
        _user_text("plan this out"),
        _assistant_tool_use("Skill", {"skill": "planner:writing-plans"}),
    ])
    line = _invoke(selffix, "sid-hs-off", transcript)
    assert _outcome(line) == "none", f"expected none (skill-HIGH off by default), got: {line!r}"


def test_legacy_simple_pr_create_still_triggers_high(selffix):
    transcript = _write_transcript(selffix["home"], "sid-8", [
        _user_text("open it"),
        _assistant_tool_use("Bash", {"command": "gh pr create --title x --body y"}),
    ])
    line = _invoke(selffix, "sid-8", transcript)
    assert _outcome(line) == "spawn", f"expected high/spawn (legacy PR), got: {line!r}"


def test_already_ran_selffix_still_skipped(selffix):
    transcript = _write_transcript(selffix["home"], "sid-9", [
        _user_text("rebase"),
        _assistant_tool_use("mcp__claude-orchestrator__spawn_worker", {"name": "x"}),
        _assistant_tool_use("Skill", {"skill": "dockwright-selffix"}),
    ])
    line = _invoke(selffix, "sid-9", transcript)
    assert _outcome(line) == "skip:already-ran", f"expected skip:already-ran, got: {line!r}"


def test_none_when_single_english_pushback_suppressed(selffix):
    transcript = _write_transcript(selffix["home"], "sid-pb1", [
        _user_text("that's wrong, the handler lives in the facade"),
    ])
    line = _invoke(selffix, "sid-pb1", transcript)
    assert _outcome(line) == "none", f"expected none, got: {line!r}"
    assert "users=1" in line and "pushback=1" in line


def test_none_when_single_russian_pushback_suppressed(selffix):
    transcript = _write_transcript(selffix["home"], "sid-pb2", [
        _user_text("почему ты остановился"),
    ])
    line = _invoke(selffix, "sid-pb2", transcript)
    assert _outcome(line) == "none", f"expected none, got: {line!r}"
    assert "users=1" in line and "pushback=1" in line


def test_none_when_russian_pushback_uppercase_suppressed(selffix):
    transcript = _write_transcript(selffix["home"], "sid-pb3", [
        _user_text("ТЫ НЕ ПРАВ, перечитай тикет"),
    ])
    line = _invoke(selffix, "sid-pb3", transcript)
    assert _outcome(line) == "none", f"expected none, got: {line!r}"
    assert "users=1" in line and "pushback=1" in line


def test_none_when_single_harsh_english_suppressed(selffix):
    transcript = _write_transcript(selffix["home"], "sid-h1", [
        _user_text("wtf is this, the build broke again"),
    ])
    line = _invoke(selffix, "sid-h1", transcript)
    assert _outcome(line) == "none", f"expected none, got: {line!r}"
    assert "users=1" in line and "harsh=1" in line


def test_none_when_single_harsh_russian_suppressed(selffix):
    transcript = _write_transcript(selffix["home"], "sid-h2", [
        _user_text("блять, это не то"),
    ])
    line = _invoke(selffix, "sid-h2", transcript)
    assert _outcome(line) == "none", f"expected none, got: {line!r}"
    assert "harsh=1" in line and "pushback=1" in line


def test_high_when_multiturn_pushback_reacts(selffix):
    transcript = _write_transcript(selffix["home"], "sid-pb-mt", [
        _user_text("wire up the auth handler"),
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "done — wired it into the facade"},
        ]}},
        _user_text("ТЫ НЕ ПРАВ, the handler belongs in the service"),
    ])
    line = _invoke(selffix, "sid-pb-mt", transcript)
    assert _outcome(line) == "spawn", f"expected spawn, got: {line!r}"
    assert "pushback:1" in line


def test_high_when_multiturn_harsh_reacts(selffix):
    transcript = _write_transcript(selffix["home"], "sid-h-mt", [
        _user_text("run the build"),
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "build finished"},
        ]}},
        _user_text("wtf is this, the build broke again"),
    ])
    line = _invoke(selffix, "sid-h-mt", transcript)
    assert _outcome(line) == "spawn", f"expected spawn, got: {line!r}"
    assert "harsh:1" in line


def test_none_when_benign_russian_word_boundaries(selffix):
    transcript = _write_transcript(selffix["home"], "sid-b1", [
        _user_text("не только корабля коснулось, но и художника"),
    ])
    line = _invoke(selffix, "sid-b1", transcript)
    assert _outcome(line) == "none", f"expected none, got: {line!r}"
    assert "pushback=0" in line and "harsh=0" in line


def test_none_when_assistant_swears(selffix):
    transcript = _write_transcript(selffix["home"], "sid-b2", [
        _user_text("clean up the test file"),
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "fuck, that's wrong — let me retry"},
        ]}},
    ])
    line = _invoke(selffix, "sid-b2", transcript)
    assert _outcome(line) == "none", f"expected none, got: {line!r}"


def test_high_two_pushbacks_report_counter(selffix):
    transcript = _write_transcript(selffix["home"], "sid-pb4", [
        _user_text("that's wrong"),
        _user_text("i told you to use the fixture"),
    ])
    line = _invoke(selffix, "sid-pb4", transcript)
    assert _outcome(line) == "spawn", f"expected spawn, got: {line!r}"
    assert "pushback:2" in line


def test_none_when_pushback_only_in_tool_result_user_record(selffix):
    transcript = _write_transcript(selffix["home"], "sid-b3", [
        _user_text("run the linter"),
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "stderr: you're wrong, fuck"},
        ]}},
    ])
    line = _invoke(selffix, "sid-b3", transcript)
    assert _outcome(line) == "none", f"expected none, got: {line!r}"


SELFFIX_RUN = REPO_ROOT / "deploy" / "scripts" / "selffix-run.sh"
RUNLOCK = REPO_ROOT / "deploy" / "scripts" / "runlock.sh"


@pytest.fixture
def selffix_e2e(tmp_path, monkeypatch):
    home = tmp_path
    monkeypatch.setenv("HOME", str(home))
    scripts_dir = home / ".claude" / "scripts"
    scripts_dir.mkdir(parents=True)
    for name, src in (("selffix-trigger.sh", SELFFIX_TRIGGER),
                      ("selffix-run.sh", SELFFIX_RUN),
                      ("runlock.sh", RUNLOCK),
                      ("transcript_signal.py", SELFFIX_RUN.parent / "transcript_signal.py")):
        dst = scripts_dir / name
        shutil.copy(src, dst)
        dst.chmod(0o755)
    skill = home / ".claude" / "skills" / "dockwright-selffix"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# dockwright-selffix\nstub body\n")
    bin_dir = home / "bin"
    bin_dir.mkdir()
    claude_stub = bin_dir / "claude"
    claude_stub.write_text(
        "#!/usr/bin/env bash\n"
        'echo "## Selffix findings (stub)"\n'
        'echo "args: $*"\n'
        'echo "Status: ok"\n'
    )
    claude_stub.chmod(0o755)
    (home / ".claude" / "selffix-debug").touch()
    return {
        "home": home,
        "script": scripts_dir / "selffix-trigger.sh",
        "bin": bin_dir,
        "findings_dir": home / ".claude" / "dockwright" / "selffix" / "findings",
        "log": home / ".claude" / "dockwright" / "selffix" / "trigger.log",
    }


def test_high_signal_writes_findings_file_to_disk(selffix_e2e):
    sid = "e2e-1"
    transcript = _write_transcript(selffix_e2e["home"], sid, [
        _user_text("open it"),
        _assistant_tool_use("Bash", {"command": "gh pr create --title x --body y"}),
    ])
    payload = json.dumps({"session_id": sid, "transcript_path": str(transcript)})
    env = {**os.environ, "HOME": str(selffix_e2e["home"]),
           "PATH": f"{selffix_e2e['bin']}:{os.environ.get('PATH', '')}"}
    env.pop("SELFFIX_DEBUG", None)
    subprocess.run(
        ["bash", str(selffix_e2e["script"])],
        input=payload, text=True, timeout=15, check=False,
        capture_output=True, env=env,
    )
    findings = selffix_e2e["findings_dir"] / f"{sid}.md"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if findings.is_file() and findings.read_text().strip():
            break
        time.sleep(0.1)
    assert findings.is_file(), (
        f"no findings file at {findings}; trigger log:\n"
        f"{selffix_e2e['log'].read_text() if selffix_e2e['log'].is_file() else '(no log)'}"
    )
    content = findings.read_text()
    assert "Selffix findings (stub)" in content, f"unexpected findings content: {content!r}"
    assert "Status:" in content, f"findings missing Status line: {content!r}"
