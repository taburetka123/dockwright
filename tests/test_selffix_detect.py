"""Integration tests for selffix-trigger.sh detect logic.

Each test constructs a fake transcript (and optionally a fake active record),
invokes the trigger script under a tmp $HOME with debug logging on, and asserts
the log line's outcome verb (spawn / none / skip:*).

The repo copy under deploy/scripts/ is the source of truth — these tests
exec that copy directly. Changes to detection logic only have to be made in one
place (the repo). setup.sh deploys the repo copies to ~/.claude/scripts/.
"""
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
    # The trigger sources loop-label-prefix.sh for the [modules] gardener toggle
    # + [gardener] high_skills resolution; ship it alongside so the helper
    # resolves (as it does deployed to ~/.claude/scripts/).
    shutil.copy(LOOP_LABEL_PREFIX, scripts_dir / "loop-label-prefix.sh")
    # Stub selffix-run.sh so the HIGH path can fork-and-disown a no-op.
    run_stub = scripts_dir / "selffix-run.sh"
    run_stub.write_text("#!/bin/bash\nexit 0\n")
    run_stub.chmod(0o755)
    # Enable debug logging so we can inspect outcome. The legacy flag path is
    # still honored by the trigger's dual-check (one release).
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
    # Drop SELFFIX_DEBUG from env in case the developer has it exported globally —
    # we want the file-based toggle to be the only debug source so tests are
    # deterministic against shell env.
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
    # Last line is this invocation's outcome.
    return lines[-1]


def _outcome(log_line: str) -> str:
    # Format: "<ISO-ts>  <outcome>  <sid>  <detail>" (two-space separators).
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


# --- HIGH signals ---------------------------------------------------------

def test_no_high_when_spawn_worker_tool_use(selffix):
    """spawn_worker alone no longer triggers HIGH. Removed from the gate on
    2026-05-20: gating on orchestrator MCP tool-use fired a retro for ~every
    worker session and stampeded the rate limiter on manager teardown."""
    transcript = _write_transcript(selffix["home"], "sid-1", [
        _user_text("dispatch the rebase worker"),
        _assistant_tool_use("mcp__claude-orchestrator__spawn_worker", {"name": "rebase"}),
    ])
    line = _invoke(selffix, "sid-1", transcript)
    assert _outcome(line) == "none", f"expected none, got: {line!r}"


def test_no_high_when_worker_done_tool_use(selffix):
    """worker_done alone no longer triggers HIGH. Every orchestrator worker calls
    worker_done on its terminal turn, so gating on it fired selffix on every
    worker — removed from the gate on 2026-05-20."""
    transcript = _write_transcript(selffix["home"], "sid-2", [
        _user_text("rebase the branch"),
        _assistant_tool_use("mcp__claude-orchestrator__worker_done", {"summary": "done"}),
    ])
    line = _invoke(selffix, "sid-2", transcript)
    assert _outcome(line) == "none", f"expected none, got: {line!r}"


def test_high_when_five_edit_or_write_tool_uses(selffix):
    """≥5 Edit/Write tool_uses = substantive code work, even without a PR or Skill."""
    events = [_user_text("apply the patches")]
    for i in range(3):
        events.append(_assistant_tool_use("Edit", {"file_path": f"/x/{i}.py"}))
    for i in range(2):
        events.append(_assistant_tool_use("Write", {"file_path": f"/x/new-{i}.py"}))
    transcript = _write_transcript(selffix["home"], "sid-3", events)
    line = _invoke(selffix, "sid-3", transcript)
    assert _outcome(line) == "spawn", f"expected high/spawn, got: {line!r}"


def test_high_when_pr_create_inside_multiline_bash(selffix):
    """`gh pr create` embedded in a here-doc / multi-line bash block must still match.
    The pre-fix regex anchored to ^\\s*, so a line starting with `--body \"$(cat <<EOF` would miss."""
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
    """Manager sessions always merit a retro — they coordinate work, the value of
    reflecting on routing/dispatch decisions is high even when no Skill fired."""
    transcript = _write_transcript(selffix["home"], "sid-5", [
        _user_text("hello"),
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
    ])
    _write_active_record(selffix["home"], "sid-5", agent="manager")
    line = _invoke(selffix, "sid-5", transcript)
    assert _outcome(line) == "spawn", f"expected high/spawn, got: {line!r}"


# --- Low / no-signal ------------------------------------------------------

def test_none_when_no_signals(selffix):
    """Three short Edit/Write calls + a couple of user messages, no manager record,
    no Skill, no PR — sub-threshold; should NOT spawn."""
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
    "superpowers:writing-plans",
    "superpowers:executing-plans",
    "superpowers:subagent-driven-development",
])
def test_configured_high_skill_triggers_high(selffix, tmp_path, skill):
    """Config-driven HIGH: a skill listed in [gardener] high_skills fires HIGH.
    The three here are the operator's live high_skills — the mechanism is
    dockwright.toml opt-in now, not a hardcoded set."""
    cfg = tmp_path / "dw.toml"
    cfg.write_text(f'[gardener]\nhigh_skills = ["{skill}"]\n')
    transcript = _write_transcript(selffix["home"], "sid-hs", [
        _user_text("plan this out"),
        _assistant_tool_use("Skill", {"skill": skill}),
    ])
    line = _invoke(selffix, "sid-hs", transcript, dockwright_config=str(cfg))
    assert _outcome(line) == "spawn", f"expected spawn (configured high skill), got: {line!r}"


def test_skill_high_off_by_default(selffix):
    """Generic default (no [gardener] high_skills): skill-based HIGH detection is
    OFF — a plan/execute Skill alone no longer spawns. This is the OSS default;
    the operator opts back in via config (see test above)."""
    transcript = _write_transcript(selffix["home"], "sid-hs-off", [
        _user_text("plan this out"),
        _assistant_tool_use("Skill", {"skill": "superpowers:writing-plans"}),
    ])
    line = _invoke(selffix, "sid-hs-off", transcript)
    assert _outcome(line) == "none", f"expected none (skill-HIGH off by default), got: {line!r}"


def test_legacy_simple_pr_create_still_triggers_high(selffix):
    """Regression guard: the simplest bash gh-pr-create one-liner must keep working."""
    transcript = _write_transcript(selffix["home"], "sid-8", [
        _user_text("open it"),
        _assistant_tool_use("Bash", {"command": "gh pr create --title x --body y"}),
    ])
    line = _invoke(selffix, "sid-8", transcript)
    assert _outcome(line) == "spawn", f"expected high/spawn (legacy PR), got: {line!r}"


def test_already_ran_selffix_still_skipped(selffix):
    """Regression guard: if the session already ran /dockwright-selffix or invoked
    the dockwright-selffix Skill, the trigger must skip (already_ran takes
    precedence over any HIGH signal)."""
    transcript = _write_transcript(selffix["home"], "sid-9", [
        _user_text("rebase"),
        _assistant_tool_use("mcp__claude-orchestrator__spawn_worker", {"name": "x"}),
        _assistant_tool_use("Skill", {"skill": "dockwright-selffix"}),
    ])
    line = _invoke(selffix, "sid-9", transcript)
    assert _outcome(line) == "skip:already-ran", f"expected skip:already-ran, got: {line!r}"


# --- Pushback / harsh-language signals (EN+RU, user messages only) ---------

def test_none_when_single_english_pushback_suppressed(selffix):
    """A single-user-message session cannot be genuine pushback — there is no
    prior assistant turn to react to. The regex still MATCHES (pushback=1 in the
    counter) but the user_msgs>=2 gate suppresses HIGH. This kills the
    embedded-document false-positive: foreign-language filler inside a `claude
    -p` transcript payload no longer spawns a billed retro."""
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
    """re.IGNORECASE must still fold Cyrillic — the match registers (pushback=1)
    even as the single-message gate suppresses HIGH."""
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
    """'блять, это не то' carries both signals; both counters still report even
    though the single-message gate suppresses HIGH."""
    transcript = _write_transcript(selffix["home"], "sid-h2", [
        _user_text("блять, это не то"),
    ])
    line = _invoke(selffix, "sid-h2", transcript)
    assert _outcome(line) == "none", f"expected none, got: {line!r}"
    assert "harsh=1" in line and "pushback=1" in line


def test_high_when_multiturn_pushback_reacts(selffix):
    """The user_msgs>=2 gate lets a GENUINE multi-turn correction through:
    user asks -> assistant replies -> user pushes back -> HIGH/spawn. No tool
    HIGH signal here, so the spawn is attributable to pushback alone."""
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
    """A genuine multi-turn harsh reaction still fires HIGH under the >=2 gate."""
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
    """'не только' must not match '\\bне то\\b'; 'корабля' must not match
    '\\bбля'; 'художника' must not match '\\bху[йяеё]'."""
    transcript = _write_transcript(selffix["home"], "sid-b1", [
        _user_text("не только корабля коснулось, но и художника"),
    ])
    line = _invoke(selffix, "sid-b1", transcript)
    assert _outcome(line) == "none", f"expected none, got: {line!r}"
    assert "pushback=0" in line and "harsh=0" in line


def test_none_when_assistant_swears(selffix):
    """Assistant's own harsh text must never trigger — user messages only."""
    transcript = _write_transcript(selffix["home"], "sid-b2", [
        _user_text("clean up the test file"),
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "fuck, that's wrong — let me retry"},
        ]}},
    ])
    line = _invoke(selffix, "sid-b2", transcript)
    assert _outcome(line) == "none", f"expected none, got: {line!r}"


def test_high_two_pushbacks_report_counter(selffix):
    """Counter formatting: two pushback messages -> pushback:2 (the old MED
    tier at >=3 is gone — any count spawns)."""
    transcript = _write_transcript(selffix["home"], "sid-pb4", [
        _user_text("that's wrong"),
        _user_text("i told you to use the fixture"),
    ])
    line = _invoke(selffix, "sid-pb4", transcript)
    assert _outcome(line) == "spawn", f"expected spawn, got: {line!r}"
    assert "pushback:2" in line


def test_none_when_pushback_only_in_tool_result_user_record(selffix):
    """tool_result records arrive as type=user with list content — they must
    stay invisible to the pushback/harsh scan (str-content branch only)."""
    transcript = _write_transcript(selffix["home"], "sid-b3", [
        _user_text("run the linter"),
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "stderr: you're wrong, fuck"},
        ]}},
    ])
    line = _invoke(selffix, "sid-b3", transcript)
    assert _outcome(line) == "none", f"expected none, got: {line!r}"


# --- End-to-end: HIGH signal -> findings file lands on disk ----------------
# The detect tests above stub selffix-run.sh as a no-op and only assert the
# trigger's outcome log line. This test exercises the full
# trigger -> spawn -> findings-file chain against BOTH repo scripts, with
# only `claude` itself stubbed (we can't run a real `claude -p` in tests).
# The findings dir is NOT independently overridable — selffix-run.sh derives it
# from $HOME ($HOME/.claude/dockwright/selffix/findings) — so the tmp_path HOME
# override is the findings-dir override the task calls for. See selffix-run.sh OUT_DIR.

SELFFIX_RUN = REPO_ROOT / "deploy" / "scripts" / "selffix-run.sh"
# selffix-run.sh sources the shared mutex lib; the repo copy is canonical.
RUNLOCK = REPO_ROOT / "deploy" / "scripts" / "runlock.sh"


@pytest.fixture
def selffix_e2e(tmp_path, monkeypatch):
    home = tmp_path
    monkeypatch.setenv("HOME", str(home))
    scripts_dir = home / ".claude" / "scripts"
    scripts_dir.mkdir(parents=True)
    for name, src in (("selffix-trigger.sh", SELFFIX_TRIGGER),
                      ("selffix-run.sh", SELFFIX_RUN),
                      ("runlock.sh", RUNLOCK)):
        dst = scripts_dir / name
        shutil.copy(src, dst)
        dst.chmod(0o755)
    # Stub `claude` on PATH: selffix-run.sh pipes its stdout into the findings
    # file, so a stub that prints findings text makes a real file land on disk
    # without invoking the actual model. Echo the transcript arg back so we can
    # assert the stub received the right invocation.
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
    """E2E: a HIGH-signal session run through the repo trigger spawns the
    repo selffix-run.sh, which writes a real findings file under
    $HOME/.claude/dockwright/selffix/findings/<sid>.md. Only `claude` is stubbed."""
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
    # selffix-run.sh is forked (nohup + disown), so poll for the findings file.
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
