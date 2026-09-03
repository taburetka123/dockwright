import json
import os
import subprocess
import time
from pathlib import Path

from tests.test_selffix_detect import (  # noqa: F401  (fixtures + helpers)
    _assistant_tool_use,
    _invoke,
    _outcome,
    _user_text,
    _write_transcript,
    selffix,
    selffix_e2e,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _command_invocation(name: str, args: str) -> dict:
    content = (
        f"<command-message>{name}</command-message>\n"
        f"<command-name>/{name}</command-name>\n"
        f"<command-args>{args}</command-args>"
    )
    return {"type": "user", "message": {"content": content}}


def test_fix_command_escalates_quiet_session(selffix):
    transcript = _write_transcript(selffix["home"], "sid-c1", [
        _command_invocation("dockwright-fix", "the retry double-fires on replay"),
    ])
    line = _invoke(selffix, "sid-c1", transcript)
    assert _outcome(line) == "spawn", f"expected spawn, got: {line!r}"
    assert "fix-command" in line


def test_fix_command_fires_single_message_no_usermsg_gate(selffix):
    transcript = _write_transcript(selffix["home"], "sid-c2", [
        _command_invocation("dockwright-fix", "the dedup window is too short"),
    ])
    line = _invoke(selffix, "sid-c2", transcript)
    assert _outcome(line) == "spawn", f"expected spawn, got: {line!r}"
    assert "fix-command" in line


def test_bare_fix_command_no_args_fires(selffix):
    transcript = _write_transcript(selffix["home"], "sid-c3", [
        _command_invocation("dockwright-fix", ""),
    ])
    line = _invoke(selffix, "sid-c3", transcript)
    assert _outcome(line) == "spawn", f"expected spawn, got: {line!r}"
    assert "fix-command" in line


def test_fix_command_plus_other_high_signal_reports_both(selffix):
    events = [_command_invocation("dockwright-fix", "the migration is wrong")]
    for i in range(5):
        events.append(_assistant_tool_use("Edit", {"file_path": f"/x/{i}.py"}))
    transcript = _write_transcript(selffix["home"], "sid-c4", events)
    line = _invoke(selffix, "sid-c4", transcript)
    assert _outcome(line) == "spawn", f"expected spawn, got: {line!r}"
    assert "fix-command" in line
    assert "edits:5" in line


def test_fix_alias_still_escalates(selffix):
    transcript = _write_transcript(selffix["home"], "sid-c6", [
        _command_invocation("fix", "the retry double-fires on replay"),
    ])
    line = _invoke(selffix, "sid-c6", transcript)
    assert _outcome(line) == "spawn", f"expected spawn, got: {line!r}"
    assert "fix-command" in line


def test_fix_alias_files_exist():
    assert (REPO_ROOT / "deploy" / "commands" / "dockwright-fix.md").is_file()


def test_prose_mention_of_fix_command_no_flag(selffix):
    transcript = _write_transcript(selffix["home"], "sid-fp1", [
        _user_text("build the `/dockwright-fix` command, see deploy/commands/dockwright-fix.md"),
    ])
    line = _invoke(selffix, "sid-fp1", transcript)
    assert _outcome(line) == "none", f"expected none, got: {line!r}"
    assert "fix-command" not in line


def test_legacy_at_sigil_no_longer_flags(selffix):
    transcript = _write_transcript(selffix["home"], "sid-leg1", [
        _user_text("@gardener fix the retry logic"),
    ])
    line = _invoke(selffix, "sid-leg1", transcript)
    assert _outcome(line) == "none", f"expected none, got: {line!r}"
    assert "manual:" not in line
    assert "fix-command" not in line

    transcript2 = _write_transcript(selffix["home"], "sid-leg2", [
        _user_text("@fix the off-by-one"),
    ])
    line2 = _invoke(selffix, "sid-leg2", transcript2)
    assert _outcome(line2) == "none", f"expected none, got: {line2!r}"
    assert "manual:" not in line2
    assert "fix-command" not in line2


def test_fix_command_tag_in_tool_result_not_scanned(selffix):
    transcript = _write_transcript(selffix["home"], "sid-fp2", [
        _user_text("run the linter"),
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "<command-name>/dockwright-fix</command-name>"},
        ]}},
    ])
    line = _invoke(selffix, "sid-fp2", transcript)
    assert _outcome(line) == "none", f"expected none, got: {line!r}"
    assert "fix-command" not in line


def test_fix_command_only_session_spawns_retro_e2e(selffix_e2e):
    sid = "e2e-fixcmd"
    transcript = _write_transcript(selffix_e2e["home"], sid, [
        _command_invocation("dockwright-fix", "we mishandled the dedup key, revisit it"),
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
        f"no findings file at {findings}; the /dockwright-fix command did not "
        f"escalate to HIGH or the retro did not spawn. trigger log:\n"
        f"{selffix_e2e['log'].read_text() if selffix_e2e['log'].is_file() else '(no log)'}"
    )
