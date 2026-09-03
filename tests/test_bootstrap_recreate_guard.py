import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "deploy" / "scripts" / "bootstrap-recreate.sh"


def _fake_tmux_dir(tmp_path):
    d = tmp_path / "fakebin"
    d.mkdir()
    log = tmp_path / "tmux-invocations.log"
    (d / "tmux").write_text(
        "#!/bin/bash\n"
        f"echo \"ENV_SKIP=${{DOCKWRIGHT_MANAGER_SKIP_PERMS:-unset}} $@\" >> {log}\n"
        "case \"$*\" in *has-session*) exit 1 ;; *new-session*|*new-window*) echo '@1'; exit 0 ;; esac\n"
        "exit 0\n")
    (d / "tmux").chmod(0o755)
    (d / "jq").symlink_to(shutil.which("jq"))
    (d / "uuidgen").symlink_to(shutil.which("uuidgen"))
    return d, log


def _seed_predecessor(home, name="mighty-demon", domain="personal", sid="sid-x",
                      agent="manager"):
    active = home / ".claude" / "dockwright" / "active"
    active.mkdir(parents=True, exist_ok=True)
    record = {"claude_sid": sid, "agent": agent, "name": name,
              "domain": domain, "pid": 4242}
    if name is None:
        del record["name"]
    if domain is None:
        del record["domain"]
    (active / f"{sid}.json").write_text(json.dumps(record))


def _handoff_leftovers(home):
    handoffs = home / ".claude" / "dockwright" / "handoffs"
    if not handoffs.exists():
        return []
    return list(handoffs.glob("*.json")) + list(handoffs.glob("*.tmp"))


def _error_fields_segment(stderr):
    error_line = next(l for l in stderr.splitlines() if l.startswith("ERROR:"))
    return error_line.split(" for predecessor", 1)[0]


def _run_bootstrap(tmp_path, fakebin, *extra, env_overrides=None, seed=True):
    home = tmp_path / "home"
    (home / ".claude" / "dockwright").mkdir(parents=True)
    if seed is True:
        _seed_predecessor(home)
    elif seed:
        _seed_predecessor(home, **seed)
    env = {**os.environ, "HOME": str(home),
           "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}"}
    env.pop("DOCKWRIGHT_MANAGER_RC", None)
    env.pop("DOCKWRIGHT_MANAGER_SKIP_PERMS", None)
    env.update(env_overrides or {})
    return subprocess.run(
        ["bash", str(SCRIPT), "--narrative", "probe", "--from-sid", "sid-x", *extra],
        capture_output=True, text=True, env=env), home


def test_dry_run_probes_without_spawning(tmp_path):
    fakebin, log = _fake_tmux_dir(tmp_path)
    r, home = _run_bootstrap(tmp_path, fakebin, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "DRY_RUN: no spawn." in r.stdout
    assert not log.exists(), f"--dry-run still reached tmux: {log.read_text()}"
    leftovers = _handoff_leftovers(home)
    assert not leftovers, f"--dry-run wrote handoff files: {leftovers}"
    payload_line = next(l for l in r.stdout.splitlines()
                        if l.startswith("handoff_payload: "))
    payload = json.loads(payload_line[len("handoff_payload: "):])
    assert payload["manager_name"] == "mighty-demon", payload
    assert payload["domain"] == "personal", payload


def test_real_run_handoff_carries_predecessor_identity(tmp_path):
    fakebin, log = _fake_tmux_dir(tmp_path)
    sock = f"wt-iso-{os.getpid()}-identity"
    r, home = _run_bootstrap(tmp_path, fakebin, "--reason", "probe-reason",
                             env_overrides={"DOCKWRIGHT_TMUX_SOCKET": sock})
    assert r.returncode == 0, r.stderr
    handoffs = list((home / ".claude" / "dockwright" / "handoffs").glob("*.json"))
    assert len(handoffs) == 1, handoffs
    payload = json.loads(handoffs[0].read_text())
    assert payload["trigger_reason"] == "probe-reason", payload
    assert payload["manager_name"] == "mighty-demon", payload
    assert payload["domain"] == "personal", payload


def test_missing_predecessor_record_fails_before_spawn_and_before_sandbox_guard(tmp_path):
    fakebin, log = _fake_tmux_dir(tmp_path)
    r, home = _run_bootstrap(tmp_path, fakebin, seed=False)
    assert r.returncode == 4, f"expected exit 4, got {r.returncode}: {r.stderr}"
    segment = _error_fields_segment(r.stderr)
    assert "manager_name" in segment and "domain" in segment, segment
    assert "--manager-name" in r.stderr, r.stderr
    leftovers = _handoff_leftovers(home)
    assert not leftovers, f"exit-4 path still wrote a handoff: {leftovers}"
    assert not log.exists(), f"exit-4 path still reached tmux: {log.read_text()}"


def test_dry_run_with_unresolvable_identity_fails_loud(tmp_path):
    fakebin, log = _fake_tmux_dir(tmp_path)
    r, home = _run_bootstrap(tmp_path, fakebin, "--dry-run", seed=False)
    assert r.returncode == 4, f"expected exit 4, got {r.returncode}: {r.stdout}{r.stderr}"
    segment = _error_fields_segment(r.stderr)
    assert "manager_name" in segment and "domain" in segment, segment
    assert "handoff_payload:" not in r.stdout, r.stdout
    leftovers = _handoff_leftovers(home)
    assert not leftovers, f"dry-run exit-4 path wrote handoff files: {leftovers}"
    assert not log.exists(), f"dry-run exit-4 path reached tmux: {log.read_text()}"


def test_explicit_overrides_rescue_missing_record(tmp_path):
    fakebin, log = _fake_tmux_dir(tmp_path)
    sock = f"wt-iso-{os.getpid()}-override"
    r, home = _run_bootstrap(
        tmp_path, fakebin,
        "--manager-name", "mighty-demon", "--domain", "personal",
        env_overrides={"DOCKWRIGHT_TMUX_SOCKET": sock}, seed=False)
    assert r.returncode == 0, r.stderr
    invocations = log.read_text() if log.exists() else ""
    assert "/manager-resume" in invocations, invocations
    handoffs = list((home / ".claude" / "dockwright" / "handoffs").glob("*.json"))
    assert len(handoffs) == 1, handoffs
    payload = json.loads(handoffs[0].read_text())
    assert payload["manager_name"] == "mighty-demon", payload
    assert payload["domain"] == "personal", payload


def test_explicit_flags_beat_record(tmp_path):
    fakebin, log = _fake_tmux_dir(tmp_path)
    sock = f"wt-iso-{os.getpid()}-precedence"
    r, home = _run_bootstrap(
        tmp_path, fakebin,
        "--manager-name", "other-name", "--domain", "other-domain",
        env_overrides={"DOCKWRIGHT_TMUX_SOCKET": sock})
    assert r.returncode == 0, r.stderr
    handoffs = list((home / ".claude" / "dockwright" / "handoffs").glob("*.json"))
    assert len(handoffs) == 1, handoffs
    payload = json.loads(handoffs[0].read_text())
    assert payload["manager_name"] == "other-name", payload
    assert payload["domain"] == "other-domain", payload


def _case_label_flags():
    lines = [l for l in SCRIPT.read_text().splitlines()
             if not l.lstrip().startswith("#")]
    flags = set()
    for line in lines:
        for m in re.finditer(r"(?:^|;;)\s*((?:--[\w-]+\|)*--[\w-]+)\)", line):
            flags.update(m.group(1).split("|"))
    return flags


EXPECTED_BLOCK = '''\
while [ $# -gt 0 ]; do
    case "$1" in
        --narrative|--from-sid|--reason|--manager-name|--domain)
            if [ $# -lt 2 ] || [ "${2#--}" != "$2" ]; then
                echo "ERROR: $1 requires a value (got '${2:-}')" >&2
                echo "Usage: $0 --narrative <prose> --from-sid <sid> [--manager-name <name>] [--domain <domain>] [--reason <string>] [--dry-run]" >&2
                exit 2
            fi
            case "$1" in
                --narrative) NARRATIVE="$2" ;;
                --from-sid) FROM_SID="$2" ;;
                --reason) REASON="$2" ;;
                --manager-name) MANAGER_NAME="$2" ;;
                --domain) DOMAIN="$2" ;;
                *) echo "internal: unhandled value flag $1" >&2; exit 2 ;;
            esac
            shift 2 ;;
        --dry-run)
            DRY_RUN=1; shift ;;
        *)
            echo "ERROR: unknown arg '$1'" >&2
            echo "Usage: $0 --narrative <prose> --from-sid <sid> [--manager-name <name>] [--domain <domain>] [--reason <string>] [--dry-run]" >&2
            exit 2 ;;
    esac
done'''


@pytest.mark.parametrize("flag", sorted(_case_label_flags()))
def test_no_flag_can_swallow_dry_run(tmp_path, flag):
    fakebin, log = _fake_tmux_dir(tmp_path)
    sock = f"wt-iso-{os.getpid()}-swallow"
    r, home = _run_bootstrap(tmp_path, fakebin, flag, "--dry-run",
                             env_overrides={"DOCKWRIGHT_TMUX_SOCKET": sock})
    assert not log.exists(), (
        f"`{flag} --dry-run` reached tmux (the flag swallowed the probe flag "
        f"and the run spawned): {log.read_text()}")
    leftovers = _handoff_leftovers(home)
    assert not leftovers, f"`{flag} --dry-run` wrote a handoff: {leftovers}"


VALUE_FLAGS = ["--narrative", "--from-sid", "--reason", "--manager-name", "--domain"]


@pytest.mark.parametrize("shape", ["flag-shaped-value", "argv-end"])
@pytest.mark.parametrize("flag", VALUE_FLAGS)
def test_value_flag_missing_value_rejected(tmp_path, flag, shape):
    fakebin, log = _fake_tmux_dir(tmp_path)
    extra = [flag, "--dry-run"] if shape == "flag-shaped-value" else [flag]
    r, home = _run_bootstrap(tmp_path, fakebin, *extra)
    assert r.returncode == 2, f"expected exit 2, got {r.returncode}: {r.stderr}"
    assert "value" in r.stderr and "Usage:" in r.stderr, r.stderr
    assert not _handoff_leftovers(home)
    assert not log.exists(), f"exit-2 path still reached tmux: {log.read_text()}"


def test_worker_record_never_donates_identity(tmp_path):
    fakebin, log = _fake_tmux_dir(tmp_path)
    r, home = _run_bootstrap(tmp_path, fakebin,
                             seed={"name": "busy-otter", "agent": "worker"})
    assert r.returncode == 4, f"expected exit 4, got {r.returncode}: {r.stderr}"
    segment = _error_fields_segment(r.stderr)
    assert "manager_name" in segment and "domain" in segment, segment
    assert not _handoff_leftovers(home)
    assert not log.exists(), f"exit-4 path still reached tmux: {log.read_text()}"


def test_record_without_domain_fails_loud(tmp_path):
    fakebin, log = _fake_tmux_dir(tmp_path)
    r, home = _run_bootstrap(tmp_path, fakebin, seed={"domain": None})
    assert r.returncode == 4, f"expected exit 4, got {r.returncode}: {r.stderr}"
    segment = _error_fields_segment(r.stderr)
    assert "domain" in segment, segment
    assert "manager_name" not in segment, segment
    assert not _handoff_leftovers(home)
    assert not log.exists(), f"exit-4 path still reached tmux: {log.read_text()}"


def test_record_without_name_fails_loud(tmp_path):
    fakebin, log = _fake_tmux_dir(tmp_path)
    r, home = _run_bootstrap(tmp_path, fakebin, seed={"name": None})
    assert r.returncode == 4, f"expected exit 4, got {r.returncode}: {r.stderr}"
    segment = _error_fields_segment(r.stderr)
    assert "manager_name" in segment, segment
    assert "domain" not in segment, segment
    assert not _handoff_leftovers(home)
    assert not log.exists(), f"exit-4 path still reached tmux: {log.read_text()}"


def test_sandboxed_home_live_socket_is_refused(tmp_path):
    fakebin, log = _fake_tmux_dir(tmp_path)
    r, home = _run_bootstrap(tmp_path, fakebin)
    assert r.returncode == 3, f"expected refusal exit 3, got {r.returncode}: {r.stderr}"
    assert "--dry-run" in r.stderr, r.stderr
    assert not log.exists(), f"refusal still reached tmux: {log.read_text()}"
    leftovers = _handoff_leftovers(home)
    assert not leftovers, f"refused run still wrote handoff files: {leftovers}"


def test_sandboxed_home_scratch_socket_still_spawns(tmp_path):
    fakebin, log = _fake_tmux_dir(tmp_path)
    sock = f"wt-iso-{os.getpid()}-probe"
    home = tmp_path / "home"
    (home / ".claude" / "dockwright").mkdir(parents=True)
    _seed_predecessor(home)
    env = {**os.environ, "HOME": str(home),
           "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
           "DOCKWRIGHT_TMUX_SOCKET": sock}
    subprocess.run(
        ["bash", str(SCRIPT), "--narrative", "probe", "--from-sid", "sid-x"],
        capture_output=True, text=True, env=env)
    invocations = log.read_text() if log.exists() else ""
    assert f"-L {sock}" in invocations and "/manager-resume" in invocations, invocations


def test_dry_run_cmd_carries_remote_control(tmp_path):
    fakebin, _log = _fake_tmux_dir(tmp_path)
    r, _home = _run_bootstrap(tmp_path, fakebin, "--dry-run")
    assert r.returncode == 0, r.stderr
    cmd = next(l for l in r.stdout.splitlines() if "cmd=[" in l)
    assert "--remote-control" in cmd, cmd
    assert cmd.index("--remote-control") < cmd.index("/manager-resume"), cmd
    assert "--remote-control --model" in cmd, cmd


def test_dry_run_cmd_rc_opt_out(tmp_path):
    fakebin, _log = _fake_tmux_dir(tmp_path)
    r, _home = _run_bootstrap(tmp_path, fakebin, "--dry-run",
                              env_overrides={"DOCKWRIGHT_MANAGER_RC": "0"})
    assert r.returncode == 0, r.stderr
    cmd = next(l for l in r.stdout.splitlines() if "cmd=[" in l)
    assert "--remote-control" not in cmd, cmd


def test_dry_run_cmd_carries_skip_perms_opt_in(tmp_path):
    fakebin, _log = _fake_tmux_dir(tmp_path)
    r, _home = _run_bootstrap(tmp_path, fakebin, "--dry-run",
                              env_overrides={"DOCKWRIGHT_MANAGER_SKIP_PERMS": "1"})
    assert r.returncode == 0, r.stderr
    cmd = next(l for l in r.stdout.splitlines() if "cmd=[" in l)
    assert "--remote-control --dangerously-skip-permissions --model" in cmd, cmd
    assert cmd.index("--dangerously-skip-permissions") < cmd.index("/manager-resume"), cmd


def test_dry_run_cmd_skip_perms_default_off(tmp_path):
    fakebin, _log = _fake_tmux_dir(tmp_path)
    r, _home = _run_bootstrap(tmp_path, fakebin, "--dry-run")
    assert r.returncode == 0, r.stderr
    cmd = next(l for l in r.stdout.splitlines() if "cmd=[" in l)
    assert "--dangerously-skip-permissions" not in cmd, cmd


def test_spawn_env_scrubbed_but_cmd_carries_flag(tmp_path):
    fakebin, log = _fake_tmux_dir(tmp_path)
    sock = f"wt-iso-{os.getpid()}-skip"
    home = tmp_path / "home"
    (home / ".claude" / "dockwright").mkdir(parents=True)
    _seed_predecessor(home)
    env = {**os.environ, "HOME": str(home),
           "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
           "DOCKWRIGHT_TMUX_SOCKET": sock,
           "DOCKWRIGHT_MANAGER_SKIP_PERMS": "1"}
    env.pop("DOCKWRIGHT_MANAGER_RC", None)
    subprocess.run(
        ["bash", str(SCRIPT), "--narrative", "probe", "--from-sid", "sid-x"],
        capture_output=True, text=True, env=env)
    invocations = log.read_text() if log.exists() else ""
    spawn_line = next(l for l in invocations.splitlines() if "/manager-resume" in l)
    assert "--dangerously-skip-permissions" in spawn_line, spawn_line
    assert spawn_line.startswith("ENV_SKIP=unset"), spawn_line
