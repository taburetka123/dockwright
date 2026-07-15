"""Tests for the /dockwright-fix structural command marker in selffix-trigger.sh.

When a user invokes the /dockwright-fix slash command (or its deprecated
1-release alias /fix), the harness writes a type=user transcript record
whose message.content is a STRING carrying the tag
<command-name>/dockwright-fix</command-name> (or
<command-name>/fix</command-name>).
That structural record is a deliberate request to retrospect the session —
detection keys on the tag, NOT a textual sigil. It is scanned in the SAME
per-record user-string branch as PUSHBACK_RE/HARSH_RE and selects the session
HIGH, but WITHOUT the user_msgs>=2 reaction gate: a one-shot single-message
/dockwright-fix invocation must still fire. Keying on the structural tag (vs
the old @gardener/@fix text sigil) means prose/backtick mentions, >8KB
embedded payloads, and the old @fix/@gardener text never false-fire.

Same harness as test_selffix_detect.py: the repo copy at
deploy/scripts/selffix-trigger.sh is the source of truth; tests exec it
directly under a tmp $HOME, no install required.
"""
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
    # Mirrors the harness transcript shape for a custom slash command (verified
    # across 845 real command records): a type=user record whose message.content is a
    # STRING carrying the command-message / command-name / command-args tags.
    content = (
        f"<command-message>{name}</command-message>\n"
        f"<command-name>/{name}</command-name>\n"
        f"<command-args>{args}</command-args>"
    )
    return {"type": "user", "message": {"content": content}}


# --- /dockwright-fix command escalates a quiet session to HIGH -------------

def test_fix_command_escalates_quiet_session(selffix):
    """A quiet session (no edits/PR/pushback) whose only event is a
    /dockwright-fix command invocation is selected HIGH and spawns the
    retro — core behavior."""
    transcript = _write_transcript(selffix["home"], "sid-c1", [
        _command_invocation("dockwright-fix", "the retry double-fires on replay"),
    ])
    line = _invoke(selffix, "sid-c1", transcript)
    assert _outcome(line) == "spawn", f"expected spawn, got: {line!r}"
    assert "fix-command" in line


def test_fix_command_fires_single_message_no_usermsg_gate(selffix):
    """Contrast with pushback/harsh (need user_msgs>=2 — see
    test_none_when_single_english_pushback_suppressed): a lone
    /dockwright-fix invocation (user_msgs==1) still fires. The command is a
    deliberate directive, not a reaction needing a prior assistant turn."""
    transcript = _write_transcript(selffix["home"], "sid-c2", [
        _command_invocation("dockwright-fix", "the dedup window is too short"),
    ])
    line = _invoke(selffix, "sid-c2", transcript)
    assert _outcome(line) == "spawn", f"expected spawn, got: {line!r}"
    assert "fix-command" in line


def test_bare_fix_command_no_args_fires(selffix):
    """A bare /dockwright-fix with empty args still fires — the note rides
    in <command-args> but is not required for the structural flag to fire."""
    transcript = _write_transcript(selffix["home"], "sid-c3", [
        _command_invocation("dockwright-fix", ""),
    ])
    line = _invoke(selffix, "sid-c3", transcript)
    assert _outcome(line) == "spawn", f"expected spawn, got: {line!r}"
    assert "fix-command" in line


def test_fix_command_plus_other_high_signal_reports_both(selffix):
    """A /dockwright-fix invocation plus an independent HIGH signal (>=5
    edits) reports both reasons; neither suppresses the other."""
    events = [_command_invocation("dockwright-fix", "the migration is wrong")]
    for i in range(5):
        events.append(_assistant_tool_use("Edit", {"file_path": f"/x/{i}.py"}))
    transcript = _write_transcript(selffix["home"], "sid-c4", events)
    line = _invoke(selffix, "sid-c4", transcript)
    assert _outcome(line) == "spawn", f"expected spawn, got: {line!r}"
    assert "fix-command" in line
    assert "edits:5" in line


# --- deprecated /fix alias (1-release deprecation window) ------------------

def test_fix_alias_still_escalates(selffix):
    """The deprecated /fix alias (deploy/commands/fix.md) still carries the
    structural human-flag for this release — FIX_CMD_RE matches
    dockwright-fix and fix so the alias keeps working until it's
    removed."""
    transcript = _write_transcript(selffix["home"], "sid-c6", [
        _command_invocation("fix", "the retry double-fires on replay"),
    ])
    line = _invoke(selffix, "sid-c6", transcript)
    assert _outcome(line) == "spawn", f"expected spawn, got: {line!r}"
    assert "fix-command" in line


def test_fix_alias_files_exist():
    """The renamed primary command file must exist on disk — this is what makes
    /dockwright-fix invocable. The deprecated /fix alias file is
    publish-excluded from this export, so it is not asserted here."""
    assert (REPO_ROOT / "deploy" / "commands" / "dockwright-fix.md").is_file()


# --- False-positive guards -------------------------------------------------

def test_prose_mention_of_fix_command_no_flag(selffix):
    """A prose / backtick mention of /dockwright-fix (e.g. while BUILDING the
    command) must NOT self-trigger — only the structural <command-name> tag
    counts. This is the FP class the structural marker kills."""
    transcript = _write_transcript(selffix["home"], "sid-fp1", [
        _user_text("build the `/dockwright-fix` command, see deploy/commands/dockwright-fix.md"),
    ])
    line = _invoke(selffix, "sid-fp1", transcript)
    assert _outcome(line) == "none", f"expected none, got: {line!r}"
    assert "fix-command" not in line


def test_legacy_at_sigil_no_longer_flags(selffix):
    """Regression: the old @gardener / @fix textual sigil is REMOVED. A quiet
    session containing it must no longer escalate to HIGH (neither manual: nor
    fix-command)."""
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
    """A <command-name>/dockwright-fix</command-name> tag inside a
    list-content user record (tool_result) is invisible to the str-content-
    only scan — only genuine str-content user records carry the structural
    command marker."""
    transcript = _write_transcript(selffix["home"], "sid-fp2", [
        _user_text("run the linter"),
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "<command-name>/dockwright-fix</command-name>"},
        ]}},
    ])
    line = _invoke(selffix, "sid-fp2", transcript)
    assert _outcome(line) == "none", f"expected none, got: {line!r}"
    assert "fix-command" not in line


# --- E2E: /dockwright-fix-only quiet session actually spawns the retro -----

def test_fix_command_only_session_spawns_retro_e2e(selffix_e2e):
    """End-to-end: a quiet session whose ONLY HIGH signal is a
    /dockwright-fix command invocation runs the repo trigger -> repo
    selffix-run.sh -> writes <sid>.md. Proves the command escalation reaches
    the real spawn path (claude stubbed)."""
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
