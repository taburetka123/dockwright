"""Tests for the selffix durable-retry queue (~/.claude/dockwright/selffix/retry/).

Producer side: selffix-run.sh enqueues one retry entry when a run fails
(rate-limit brick, lock-timeout, degenerate stub); a retried run
(SELFFIX_RETRY_ATTEMPT=1) never re-enqueues. Trigger side: a fresh
.manager-limited-* flag makes selffix-trigger.sh enqueue instead of spawning
a doomed run. Consumer side lives in gardener_gate.py (test_gardener_gate.py).

Same harness style as test_selffix_detect.py: repo scripts copied into a tmp
$HOME, `claude` stubbed on PATH.
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "deploy" / "scripts"

FAILING_CLAUDE_STUB = (
    "#!/usr/bin/env bash\n"
    "echo \"You've hit your session limit · resets 12am (Asia/Novosibirsk)\"\n"
    "exit 1\n"
)
# >200 bytes of findings so the stub-size guard does not classify success as a stub.
OK_CLAUDE_STUB = (
    "#!/usr/bin/env bash\n"
    "echo '## Selffix findings (stub)'\n"
    "for i in $(seq 1 20); do echo \"- finding line $i: lorem ipsum dolor sit amet\"; done\n"
    "echo 'Status: ok'\n"
)


@pytest.fixture
def retry_home(tmp_path, monkeypatch):
    home = tmp_path
    monkeypatch.setenv("HOME", str(home))
    scripts_dir = home / ".claude" / "scripts"
    scripts_dir.mkdir(parents=True)
    for name in ("selffix-trigger.sh", "selffix-run.sh", "runlock.sh", "selffix-retry-lib.sh"):
        dst = scripts_dir / name
        shutil.copy(SCRIPTS / name, dst)
        dst.chmod(0o755)
    (home / ".claude" / "selffix-debug").touch()
    bin_dir = home / "bin"
    bin_dir.mkdir()
    return {
        "home": home,
        "scripts": scripts_dir,
        "bin": bin_dir,
        "log": home / ".claude" / "dockwright" / "selffix" / "trigger.log",
        "retry_dir": home / ".claude" / "dockwright" / "selffix" / "retry",
    }


def _stub_claude(retry_home, body: str) -> None:
    stub = retry_home["bin"] / "claude"
    stub.write_text(body)
    stub.chmod(0o755)


def _write_transcript(home: Path, sid: str) -> Path:
    project_dir = home / ".claude" / "projects" / "fake-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{sid}.jsonl"
    path.write_text(json.dumps(
        {"type": "user", "message": {"content": "open it"}}) + "\n" + json.dumps(
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash",
             "input": {"command": "gh pr create --title x --body y"}}]}}) + "\n")
    return path


def _run_sh(retry_home, transcript: Path, sid: str, extra_env: dict | None = None):
    # stdout/stderr MUST be DEVNULL, not captured: run.sh's watchdog leaves an
    # orphaned `sleep` child holding inherited pipes, so capture_output=True
    # blocks until the subprocess timeout (verified empirically in plan
    # review). Nothing reads stdout — all asserts go via the log + queue dir.
    # SELFFIX_TIMEOUT_SEC bounds the orphan sleep's lifetime: the subprocess
    # timeout=30 makes any watchdog >30s unreachable in a passing test, and 35
    # keeps that margin while orphan sleeps die in 35s instead of 10 min.
    env = {**os.environ, "HOME": str(retry_home["home"]),
           "PATH": f"{retry_home['bin']}:{os.environ.get('PATH', '')}",
           "SELFFIX_TIMEOUT_SEC": "35"}
    env.pop("SELFFIX_DEBUG", None)
    env.pop("SELFFIX_RETRY_ATTEMPT", None)
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(retry_home["scripts"] / "selffix-run.sh"), str(transcript), sid],
        text=True, timeout=30, check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)


def _log_text(retry_home) -> str:
    return retry_home["log"].read_text() if retry_home["log"].is_file() else ""


def test_failed_run_enqueues_retry(retry_home):
    _stub_claude(retry_home, FAILING_CLAUDE_STUB)
    sid = "brick-1"
    transcript = _write_transcript(retry_home["home"], sid)
    _run_sh(retry_home, transcript, sid)
    entry_path = retry_home["retry_dir"] / f"{sid}.json"
    assert entry_path.is_file(), f"no queue entry; log:\n{_log_text(retry_home)}"
    entry = json.loads(entry_path.read_text())
    assert entry["sid"] == sid
    assert entry["transcript_path"] == str(transcript)
    assert entry["attempts"] == 0
    assert entry["reason"] == "finished-error"
    assert "retry:enqueued" in _log_text(retry_home)


def test_retry_attempt_never_reenqueues(retry_home):
    """SELFFIX_RETRY_ATTEMPT=1 + failure -> retry:exhausted, queue stays empty.
    This is the retry-once cap; without it the gate->run.sh->queue chain loops."""
    _stub_claude(retry_home, FAILING_CLAUDE_STUB)
    sid = "brick-2"
    transcript = _write_transcript(retry_home["home"], sid)
    _run_sh(retry_home, transcript, sid, {"SELFFIX_RETRY_ATTEMPT": "1"})
    assert not (retry_home["retry_dir"] / f"{sid}.json").exists()
    assert "retry:exhausted" in _log_text(retry_home)


def test_successful_run_does_not_enqueue(retry_home):
    _stub_claude(retry_home, OK_CLAUDE_STUB)
    sid = "ok-1"
    transcript = _write_transcript(retry_home["home"], sid)
    _run_sh(retry_home, transcript, sid)
    assert not retry_home["retry_dir"].exists() or not list(retry_home["retry_dir"].glob("*.json"))
    assert "retry:" not in _log_text(retry_home)


def test_zero_exit_stub_output_enqueues(retry_home):
    """exit 0 but <200 bytes of output = degenerate stub -> enqueue reason=stub."""
    _stub_claude(retry_home, "#!/usr/bin/env bash\necho 'Status: ok'\n")
    sid = "stub-1"
    transcript = _write_transcript(retry_home["home"], sid)
    _run_sh(retry_home, transcript, sid)
    entry_path = retry_home["retry_dir"] / f"{sid}.json"
    assert entry_path.is_file(), f"no queue entry; log:\n{_log_text(retry_home)}"
    assert json.loads(entry_path.read_text())["reason"] == "stub"


def test_lock_timeout_enqueues(retry_home):
    """A run that gives up waiting on a live lock holder enqueues lock-timeout."""
    _stub_claude(retry_home, OK_CLAUDE_STUB)
    lock_dir = retry_home["home"] / ".claude" / "locks" / "analyst-run.lock"
    lock_dir.mkdir(parents=True)
    (lock_dir / "pid").write_text(str(os.getpid()))  # live holder: this pytest process
    sid = "locked-1"
    transcript = _write_transcript(retry_home["home"], sid)
    _run_sh(retry_home, transcript, sid, {"SELFFIX_LOCK_WAIT_MAX": "0"})
    entry_path = retry_home["retry_dir"] / f"{sid}.json"
    assert entry_path.is_file(), f"no queue entry; log:\n{_log_text(retry_home)}"
    assert json.loads(entry_path.read_text())["reason"] == "lock-timeout"


def _invoke_trigger(retry_home, sid: str, transcript: Path):
    payload = json.dumps({"session_id": sid, "transcript_path": str(transcript)})
    env = {**os.environ, "HOME": str(retry_home["home"]),
           "PATH": f"{retry_home['bin']}:{os.environ.get('PATH', '')}",
           "SELFFIX_TIMEOUT_SEC": "35"}
    env.pop("SELFFIX_DEBUG", None)
    return subprocess.run(
        ["bash", str(retry_home["scripts"] / "selffix-trigger.sh")],
        input=payload, text=True, timeout=15, check=False, capture_output=True, env=env)


def _touch_limited_flag(retry_home, age_sec: float = 0.0) -> Path:
    orch = retry_home["home"] / ".claude" / "dockwright"
    orch.mkdir(parents=True, exist_ok=True)
    flag = orch / ".manager-limited-grumpy-sloth"
    flag.touch()
    if age_sec:
        stamp = time.time() - age_sec
        os.utime(flag, (stamp, stamp))
    return flag


def test_trigger_brick_check_enqueues_instead_of_spawning(retry_home):
    """Fresh .manager-limited-* flag = account bricked right now: the HIGH
    spawn would die in seconds, so the trigger enqueues directly."""
    _stub_claude(retry_home, FAILING_CLAUDE_STUB)
    _touch_limited_flag(retry_home)
    sid = "brick-trigger-1"
    transcript = _write_transcript(retry_home["home"], sid)
    _invoke_trigger(retry_home, sid, transcript)
    entry_path = retry_home["retry_dir"] / f"{sid}.json"
    assert entry_path.is_file(), f"no queue entry; log:\n{_log_text(retry_home)}"
    assert json.loads(entry_path.read_text())["reason"] == "brick"
    assert "retry:enqueued" in _log_text(retry_home)
    assert "  spawn  " not in _log_text(retry_home)
    # No findings file = run.sh never spawned.
    findings = retry_home["home"] / ".claude" / "dockwright" / "selffix" / "findings" / f"{sid}.md"
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        assert not findings.is_file(), "trigger spawned despite fresh brick flag"
        time.sleep(0.2)
    # Dedup marker still written: the session is handled, a retry storm must not re-fire.
    assert list((retry_home["home"] / ".claude" / "dockwright" / "selffix" / "findings" / ".dedup").glob("*"))


def test_trigger_stale_brick_flag_spawns_normally(retry_home):
    """A flag older than the freshness window means the brick cleared (the
    monitor refreshes mtime every scan while limited) — spawn as usual."""
    _stub_claude(retry_home, OK_CLAUDE_STUB)
    _touch_limited_flag(retry_home, age_sec=600)
    sid = "brick-trigger-2"
    transcript = _write_transcript(retry_home["home"], sid)
    _invoke_trigger(retry_home, sid, transcript)
    assert not (retry_home["retry_dir"] / f"{sid}.json").exists()
    assert "  spawn  " in _log_text(retry_home)
