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
    "echo \"You've hit your session limit · resets 12am (Etc/GMT-9)\"\n"
    "exit 1\n"
)
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
    for name in ("selffix-trigger.sh", "selffix-run.sh", "runlock.sh",
                 "selffix-retry-lib.sh", "transcript_signal.py"):
        dst = scripts_dir / name
        shutil.copy(SCRIPTS / name, dst)
        dst.chmod(0o755)
    skill = home / ".claude" / "skills" / "dockwright-selffix"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# dockwright-selffix\nstub body\n")
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
    _stub_claude(retry_home, "#!/usr/bin/env bash\necho 'Status: ok'\n")
    sid = "stub-1"
    transcript = _write_transcript(retry_home["home"], sid)
    _run_sh(retry_home, transcript, sid)
    entry_path = retry_home["retry_dir"] / f"{sid}.json"
    assert entry_path.is_file(), f"no queue entry; log:\n{_log_text(retry_home)}"
    assert json.loads(entry_path.read_text())["reason"] == "stub"


def test_lock_timeout_enqueues(retry_home):
    _stub_claude(retry_home, OK_CLAUDE_STUB)
    lock_dir = retry_home["home"] / ".claude" / "locks" / "analyst-run.lock"
    lock_dir.mkdir(parents=True)
    (lock_dir / "pid").write_text(str(os.getpid()))
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
    findings = retry_home["home"] / ".claude" / "dockwright" / "selffix" / "findings" / f"{sid}.md"
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        assert not findings.is_file(), "trigger spawned despite fresh brick flag"
        time.sleep(0.2)
    assert list((retry_home["home"] / ".claude" / "dockwright" / "selffix" / "findings" / ".dedup").glob("*"))


def test_trigger_queues_the_retro_while_a_usage_cap_pauses_the_fleet(retry_home):
    _stub_claude(retry_home, OK_CLAUDE_STUB)
    stub = retry_home["bin"] / "dockwright"
    stub.write_text('#!/usr/bin/env bash\n[ "$*" = "usage-cap --paused" ] && exit 0\nexit 9\n')
    stub.chmod(0o755)
    sid = "cap-trigger-1"
    transcript = _write_transcript(retry_home["home"], sid)
    _invoke_trigger(retry_home, sid, transcript)
    entry_path = retry_home["retry_dir"] / f"{sid}.json"
    assert entry_path.is_file(), f"no queue entry; log:\n{_log_text(retry_home)}"
    assert json.loads(entry_path.read_text())["reason"] == "usage-cap"
    assert "reason=usage-cap" in _log_text(retry_home)
    assert "  spawn  " not in _log_text(retry_home)


def test_trigger_stale_brick_flag_spawns_normally(retry_home):
    _stub_claude(retry_home, OK_CLAUDE_STUB)
    _touch_limited_flag(retry_home, age_sec=600)
    sid = "brick-trigger-2"
    transcript = _write_transcript(retry_home["home"], sid)
    _invoke_trigger(retry_home, sid, transcript)
    assert not (retry_home["retry_dir"] / f"{sid}.json").exists()
    assert "  spawn  " in _log_text(retry_home)


ARGV_RECORDING_OK_STUB = (
    "#!/usr/bin/env bash\n"
    'printf "%s\\n" "$@" > "$HOME/claude-argv.txt"\n'
    "echo '## Selffix findings (stub)'\n"
    "for i in $(seq 1 20); do echo \"- finding line $i: lorem ipsum dolor sit\"; done\n"
    "echo 'Status: ok'\n"
)

STATUS_ERROR_STUB = (
    "#!/usr/bin/env bash\n"
    "echo 'Status: error (transcript-unreadable)'\n"
)


def test_run_passes_add_dir_and_allowed_tools(retry_home):
    _stub_claude(retry_home, ARGV_RECORDING_OK_STUB)
    sid = "argv-1"
    transcript = _write_transcript(retry_home["home"], sid)
    _run_sh(retry_home, transcript, sid)
    argv = (retry_home["home"] / "claude-argv.txt").read_text().splitlines()
    assert "--add-dir" in argv
    assert argv[argv.index("--add-dir") + 1] == str(transcript.parent)
    assert "--allowedTools" in argv
    allowed = argv[argv.index("--allowedTools") + 1]
    for frag in ("Bash(jq:*)", "Bash(wc:*)", "Bash(head:*)", "Bash(grep:*)"):
        assert frag in allowed, f"{frag} missing from --allowedTools: {allowed}"
    assert "Read" not in allowed.split(), (
        f"bare Read re-granted whole-filesystem read: {allowed}"
    )


def test_zero_exit_status_error_is_not_success(retry_home):
    _stub_claude(retry_home, STATUS_ERROR_STUB)
    sid = "status-err-1"
    transcript = _write_transcript(retry_home["home"], sid)
    _run_sh(retry_home, transcript, sid)
    log = _log_text(retry_home)
    assert "worker:finished-error" in log
    assert "status-error" in log
    entry_path = retry_home["retry_dir"] / f"{sid}.json"
    assert entry_path.is_file(), f"no retry entry; log:\n{log}"
    entry = json.loads(entry_path.read_text())
    assert entry["reason"] == "status-error", "status-error must beat the <200B stub check"


def test_run_uses_dockwright_claude_bin(retry_home, tmp_path):
    _stub_claude(retry_home, OK_CLAUDE_STUB)
    hop = tmp_path / "hop-claude"
    hop.write_text(ARGV_RECORDING_OK_STUB)
    hop.chmod(0o755)
    sid = "hop-1"
    transcript = _write_transcript(retry_home["home"], sid)
    _run_sh(retry_home, transcript, sid, {"DOCKWRIGHT_CLAUDE_BIN": str(hop)})
    assert (retry_home["home"] / "claude-argv.txt").is_file(), "DOCKWRIGHT_CLAUDE_BIN was not used"


def test_run_falls_back_when_dockwright_claude_bin_is_not_executable(retry_home, tmp_path):
    _stub_claude(retry_home, ARGV_RECORDING_OK_STUB)
    sid = "hop-2"
    transcript = _write_transcript(retry_home["home"], sid)
    _run_sh(retry_home, transcript, sid, {"DOCKWRIGHT_CLAUDE_BIN": str(tmp_path / "missing")})
    assert (retry_home["home"] / "claude-argv.txt").is_file(), "PATH claude did not run"
    assert "not an executable file" in _log_text(retry_home)


def test_fallback_line_is_written_with_debug_off(retry_home, tmp_path):
    (retry_home["home"] / ".claude" / "selffix-debug").unlink()
    _stub_claude(retry_home, ARGV_RECORDING_OK_STUB)
    sid = "hop-3"
    transcript = _write_transcript(retry_home["home"], sid)
    _run_sh(retry_home, transcript, sid, {"DOCKWRIGHT_CLAUDE_BIN": str(tmp_path / "missing")})
    assert "not an executable file" in _log_text(retry_home)


def test_dockwright_claude_bin_in_a_directory_named_with_equals_runs(retry_home, tmp_path):
    _stub_claude(retry_home, OK_CLAUDE_STUB)
    wrapper_dir = tmp_path / "opt=wrappers"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "claude-wrapper"
    wrapper.write_text(ARGV_RECORDING_OK_STUB)
    wrapper.chmod(0o755)
    sid = "equals-1"
    transcript = _write_transcript(retry_home["home"], sid)
    _run_sh(retry_home, transcript, sid, {"DOCKWRIGHT_CLAUDE_BIN": str(wrapper)})
    argv_file = retry_home["home"] / "claude-argv.txt"
    assert argv_file.is_file(), f"wrapper under {wrapper_dir} did not run; log:\n{_log_text(retry_home)}"
    assert argv_file.read_text().splitlines()[0] == "-p"


ENV_RECORDING_OK_STUB = (
    "#!/usr/bin/env bash\n"
    'env > "$HOME/claude-env.txt"\n'
    "echo '## Selffix findings (stub)'\n"
    "for i in $(seq 1 20); do echo \"- finding line $i: lorem ipsum dolor sit\"; done\n"
    "echo 'Status: ok'\n"
)
SESSION_IDENTITY_VARS = ("CLAUDE_AGENT", "CLAUDE_WORKER_NAME", "CLAUDE_PARENT_MANAGER", "CLAUDE_DOMAIN")


def test_child_environment_drops_the_session_identity_vars(retry_home):
    _stub_claude(retry_home, ENV_RECORDING_OK_STUB)
    sid = "env-1"
    transcript = _write_transcript(retry_home["home"], sid)
    parent_env = {name: "set-by-parent" for name in SESSION_IDENTITY_VARS}
    _run_sh(retry_home, transcript, sid, {**parent_env, "EXAMPLE_KEPT_VAR": "kept"})
    env_file = retry_home["home"] / "claude-env.txt"
    assert env_file.is_file(), f"claude stub did not run; log:\n{_log_text(retry_home)}"
    names = {line.partition("=")[0] for line in env_file.read_text().splitlines()}
    assert "EXAMPLE_KEPT_VAR" in names
    assert names.isdisjoint(SESSION_IDENTITY_VARS), names & set(SESSION_IDENTITY_VARS)
