import json
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.test_model_pin_inventory import RESOLVER_PROBE_ID

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


LAUNCH_TAIL = f"--remote-control --effort high --model '{RESOLVER_PROBE_ID}'"


def launch_args_stub(bin_dir, prints: str, *, exit_code: int = 0, manager_home=None) -> None:
    stub = Path(bin_dir) / "dockwright"
    home_branch = ""
    if manager_home is not None:
        home_branch = (f'if [ "$1" = ensure-manager-home ]; then\n'
                       f'  echo {shlex.quote(str(manager_home))}\n  exit 0\nfi\n')
    stub.write_text(
        "#!/bin/bash\n"
        f"{home_branch}"
        '[ "$1" = notebook ] && exit 0\n'
        '[ "$1" = manager-launch-args ] || exit 2\n'
        f"printf '%s\\n' {shlex.quote(prints)}\n"
        f"exit {exit_code}\n")
    stub.chmod(0o755)


def _dry_run_tokens(r) -> list[str]:
    cmd = next(l for l in r.stdout.splitlines() if "cmd=[" in l)
    return shlex.split(cmd.split("cmd=[", 1)[1].rsplit("]", 1)[0])


def _run_bootstrap(tmp_path, fakebin, *extra, env_overrides=None, seed=True, notebook=None, mark=None,
                   notebook_domain="personal"):
    home = tmp_path / "home"
    (home / ".claude" / "dockwright").mkdir(parents=True)
    notebooks = home / ".claude" / "dockwright" / "notebook"
    if notebook is not None:
        notebooks.mkdir(parents=True, exist_ok=True)
        (notebooks / f"{notebook_domain}.md").write_text(notebook)
    if mark is not None:
        (notebooks / "marks").mkdir(parents=True, exist_ok=True)
        (notebooks / "marks" / f"{notebook_domain}.json").write_text(json.dumps(mark))
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


def test_spawn_pins_cwd_to_manager_home_not_pwd(tmp_path):
    fakebin, log = _fake_tmux_dir(tmp_path)
    mgr_home = tmp_path / "mgr-home"
    r, _home = _run_bootstrap(tmp_path, fakebin,
                              env_overrides={"DOCKWRIGHT_MANAGER_HOME": str(mgr_home),
                                             "DOCKWRIGHT_TMUX_SOCKET": f"wt-iso-{os.getpid()}-probe"})
    assert r.returncode == 0, r.stderr
    invocations = log.read_text()
    assert f"-c {mgr_home} " in invocations, invocations
    assert f"-c {os.getcwd()} " not in invocations, invocations
    assert mgr_home.is_dir()


def test_spawn_asks_the_cli_for_the_manager_home_when_env_is_unset(tmp_path):
    fakebin, log = _fake_tmux_dir(tmp_path)
    cli_home = tmp_path / "cli-home"
    cli_home.mkdir()
    (fakebin / "dockwright").write_text(
        "#!/bin/bash\n"
        f"[ \"$1\" = ensure-manager-home ] && echo '{cli_home}' && exit 0\n"
        "[ \"$1\" = notebook ] && exit 0\n"
        "[ \"$1\" = manager-launch-args ] && echo \"--remote-control --model 'claude-opus-5-5[1m]'\" && exit 0\n"
        "exit 2\n")
    (fakebin / "dockwright").chmod(0o755)
    r, _home = _run_bootstrap(tmp_path, fakebin,
                              env_overrides={"DOCKWRIGHT_MANAGER_HOME": "",
                                             "DOCKWRIGHT_TMUX_SOCKET": f"wt-iso-{os.getpid()}-probe"})
    assert r.returncode == 0, r.stderr
    assert f"-c {cli_home} " in log.read_text(), log.read_text()


def test_spawn_refuses_when_the_cli_refuses_the_manager_home(tmp_path):
    fakebin, log = _fake_tmux_dir(tmp_path)
    (fakebin / "dockwright").write_text(
        "#!/bin/bash\n"
        "echo 'ERROR: manager home /Volumes/ext/manager could not be created' >&2\n"
        "exit 1\n")
    (fakebin / "dockwright").chmod(0o755)
    r, home = _run_bootstrap(tmp_path, fakebin,
                             env_overrides={"DOCKWRIGHT_MANAGER_HOME": "",
                                            "DOCKWRIGHT_TMUX_SOCKET": f"wt-iso-{os.getpid()}-probe"})
    assert r.returncode == 5, r.stderr
    assert "could not be created" in r.stderr and "not spawning a manager elsewhere" in r.stderr, r.stderr
    assert not log.exists(), log.read_text()
    assert not (home / "projects" / "work" / "manager").exists()
    assert not _handoff_leftovers(home)


def test_spawn_refuses_when_manager_home_cannot_be_created(tmp_path):
    fakebin, log = _fake_tmux_dir(tmp_path)
    parent_file = tmp_path / "afile"
    parent_file.write_text("x")
    r, _home = _run_bootstrap(tmp_path, fakebin,
                              env_overrides={"DOCKWRIGHT_MANAGER_HOME": str(parent_file / "manager"),
                                             "DOCKWRIGHT_TMUX_SOCKET": f"wt-iso-{os.getpid()}-probe"})
    assert r.returncode == 5, r.stderr
    assert "manager home" in r.stderr and "could not be created" in r.stderr, r.stderr
    assert not log.exists(), log.read_text()
    assert not _handoff_leftovers(_home), "a handoff was written for a refused spawn"


def test_dry_run_reports_manager_home_cwd(tmp_path):
    fakebin, _log = _fake_tmux_dir(tmp_path)
    mgr_home = tmp_path / "mgr-home"
    r, _home = _run_bootstrap(tmp_path, fakebin, "--dry-run",
                              env_overrides={"DOCKWRIGHT_MANAGER_HOME": str(mgr_home)})
    assert r.returncode == 0, r.stderr
    assert f"cwd={mgr_home} " in r.stdout, r.stdout


def test_spawn_defaults_cwd_to_home_manager_dir_when_the_cli_names_none(tmp_path):
    fakebin, log = _fake_tmux_dir(tmp_path)
    (fakebin / "dockwright").write_text(
        "#!/bin/bash\n"
        "case \"$1\" in\n"
        "  ensure-manager-home) ;;\n"
        "  notebook) ;;\n"
        "  manager-launch-args) echo \"--remote-control --model 'claude-opus-5-5[1m]'\" ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n"
        "exit 0\n")
    (fakebin / "dockwright").chmod(0o755)
    r, home = _run_bootstrap(tmp_path, fakebin,
                             env_overrides={"PATH": f"{fakebin}{os.pathsep}/bin{os.pathsep}/usr/bin",
                                            "DOCKWRIGHT_BIN": "", "DOCKWRIGHT_MANAGER_HOME": "",
                                            "DOCKWRIGHT_TMUX_SOCKET": f"wt-iso-{os.getpid()}-probe"})
    assert r.returncode == 0, r.stderr
    assert f"-c {home / 'projects' / 'work' / 'manager'} " in log.read_text(), log.read_text()


def test_spawn_hands_tmux_the_launch_args_the_cli_printed(tmp_path):
    fakebin, log = _fake_tmux_dir(tmp_path)
    launch_args_stub(fakebin, LAUNCH_TAIL)
    r, _home = _run_bootstrap(tmp_path, fakebin,
                              env_overrides={"DOCKWRIGHT_TMUX_SOCKET": f"wt-iso-{os.getpid()}-probe"})
    assert r.returncode == 0, r.stderr
    spawned = log.read_text()
    assert f"{fakebin / 'dockwright'} launch {LAUNCH_TAIL} '/manager-resume " in spawned, (
        f"the second shell did not get the launch args exactly as the CLI printed them: {spawned}")


def test_spawn_asks_for_the_launch_args_through_the_scripts_own_cli_lookup(tmp_path):
    fakebin, log = _fake_tmux_dir(tmp_path)
    local_bin = tmp_path / "home" / ".local" / "bin"
    local_bin.mkdir(parents=True)
    mgr_home = tmp_path / "mgr-home"
    launch_args_stub(local_bin, LAUNCH_TAIL, manager_home=mgr_home)
    r, _home = _run_bootstrap(
        tmp_path, fakebin,
        env_overrides={"PATH": f"{fakebin}{os.pathsep}/bin{os.pathsep}/usr/bin",
                       "DOCKWRIGHT_BIN": "", "DOCKWRIGHT_MANAGER_HOME": "",
                       "DOCKWRIGHT_TMUX_SOCKET": f"wt-iso-{os.getpid()}-localbin"})
    assert r.returncode == 0, r.stderr
    spawned = log.read_text() if log.exists() else ""
    assert f"{local_bin / 'dockwright'} launch {LAUNCH_TAIL} '/manager-resume " in spawned, (
        "the launch args did not come from the CLI the script's own lookup found: "
        f"rc={r.returncode} stderr={r.stderr!r} spawned={spawned!r}")
    assert f"-c {mgr_home} " in spawned, (
        f"the same CLI must have named the manager home: {spawned!r}")


@pytest.mark.parametrize("mode", ["cli-absent", "cli-exits-nonzero", "cli-prints-nothing"])
def test_spawn_refuses_when_the_cli_names_no_launch_args(tmp_path, mode):
    fakebin, log = _fake_tmux_dir(tmp_path)
    overrides = {"DOCKWRIGHT_TMUX_SOCKET": f"wt-iso-{os.getpid()}-probe"}
    if mode == "cli-absent":
        overrides.update({"PATH": f"{fakebin}{os.pathsep}/bin{os.pathsep}/usr/bin",
                          "DOCKWRIGHT_BIN": "", "DOCKWRIGHT_MANAGER_HOME": ""})
    elif mode == "cli-exits-nonzero":
        launch_args_stub(fakebin, LAUNCH_TAIL, exit_code=1)
    else:
        launch_args_stub(fakebin, "")
    r, home = _run_bootstrap(tmp_path, fakebin, env_overrides=overrides)
    assert r.returncode == 1, (r.returncode, r.stderr)
    assert not log.exists(), (
        f"spawned a manager with no launch args: {log.read_text() if log.exists() else ''}")
    assert not _handoff_leftovers(home), "a handoff was written for a refused spawn"
    assert "ERROR:" in r.stderr and "manager-launch-args" in r.stderr, (
        f"the {mode} shape refused without naming the command: {r.stderr!r}")


def test_dry_run_cmd_carries_remote_control(tmp_path):
    fakebin, _log = _fake_tmux_dir(tmp_path)
    r, _home = _run_bootstrap(tmp_path, fakebin, "--dry-run")
    assert r.returncode == 0, r.stderr
    tokens = _dry_run_tokens(r)
    assert tokens[0].endswith("/dockwright") and tokens[1] == "launch", tokens
    assert "--remote-control" in tokens and "--model" in tokens, tokens
    assert tokens[-1].startswith("/manager-resume "), tokens


def test_dry_run_cmd_rc_opt_out(tmp_path):
    fakebin, _log = _fake_tmux_dir(tmp_path)
    r, _home = _run_bootstrap(tmp_path, fakebin, "--dry-run",
                              env_overrides={"DOCKWRIGHT_MANAGER_RC": "0"})
    assert r.returncode == 0, r.stderr
    cmd = next(l for l in r.stdout.splitlines() if "cmd=[" in l)
    assert "--remote-control" not in cmd, cmd
    tokens = shlex.split(cmd.split("cmd=[", 1)[1][:-1])
    assert json.loads(tokens[tokens.index("--settings") + 1])["remoteControlAtStartup"] is False


def test_refuses_on_a_dockwright_that_predates_manager_launch_args(tmp_path):
    fakebin, log = _fake_tmux_dir(tmp_path)
    fake_dw = fakebin / "dockwright-old"
    fake_dw.write_text(
        "#!/bin/bash\n"
        "case \"$1\" in\n"
        "  model) echo claude-opus-5 ;;\n"
        "  manager-rc-args) echo --remote-control ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n")
    fake_dw.chmod(0o755)
    r, home = _run_bootstrap(tmp_path, fakebin,
                             env_overrides={"DOCKWRIGHT_BIN": str(fake_dw)})
    assert r.returncode == 1, r.stderr
    assert "manager-launch-args" in r.stderr, r.stderr
    assert not log.exists() or "new-session" not in log.read_text()
    assert not _handoff_leftovers(home)


def test_dry_run_cmd_carries_skip_perms_opt_in(tmp_path):
    fakebin, _log = _fake_tmux_dir(tmp_path)
    r, _home = _run_bootstrap(tmp_path, fakebin, "--dry-run",
                              env_overrides={"DOCKWRIGHT_MANAGER_SKIP_PERMS": "1"})
    assert r.returncode == 0, r.stderr
    tokens = _dry_run_tokens(r)
    assert "--dangerously-skip-permissions" in tokens, tokens
    assert tokens[-1].startswith("/manager-resume "), tokens


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


NOTEBOOK = "# N\n\n## e\n"


def test_notebook_without_mark_refuses_before_any_write(tmp_path):
    fakebin, log = _fake_tmux_dir(tmp_path)
    r, home = _run_bootstrap(tmp_path, fakebin, notebook=NOTEBOOK)
    assert r.returncode == 6, (r.returncode, r.stderr)
    assert "dockwright notebook mark --sid sid-x --domain personal" in r.stderr, r.stderr
    assert not _handoff_leftovers(home)
    assert not log.exists() or "new-session" not in log.read_text()


def test_dry_run_reports_the_notebook_refusal(tmp_path):
    fakebin, _log = _fake_tmux_dir(tmp_path)
    r, _home = _run_bootstrap(tmp_path, fakebin, "--dry-run", notebook=NOTEBOOK)
    assert r.returncode == 6, (r.returncode, r.stderr)
    assert "DRY_RUN" not in r.stdout, r.stdout


def test_fresh_mark_lets_the_dry_run_through(tmp_path):
    fakebin, _log = _fake_tmux_dir(tmp_path)
    r, _home = _run_bootstrap(tmp_path, fakebin, "--dry-run", notebook=NOTEBOOK,
                              mark={"sid": "sid-x", "at": time.time(), "actualized": True, "reason": None})
    assert r.returncode == 0, r.stderr


def test_a_mark_of_a_dash_domain_lets_the_dry_run_through(tmp_path):
    fakebin, _log = _fake_tmux_dir(tmp_path)
    r, _home = _run_bootstrap(tmp_path, fakebin, "--dry-run", seed={"domain": "-x"}, notebook=NOTEBOOK,
                              notebook_domain="-x",
                              mark={"sid": "sid-x", "at": time.time(), "actualized": True, "reason": None})
    assert r.returncode == 0, r.stderr


def test_a_cli_without_the_notebook_subcommand_names_the_cause(tmp_path):
    fakebin, log = _fake_tmux_dir(tmp_path)
    (fakebin / "dockwright").write_text(
        "#!/bin/bash\n"
        "[ \"$1\" = manager-launch-args ] && echo --remote-control && exit 0\n"
        "echo \"Unknown subcommand: $1\" >&2\n"
        "exit 2\n")
    (fakebin / "dockwright").chmod(0o755)
    r, home = _run_bootstrap(tmp_path, fakebin)
    assert r.returncode == 6, (r.returncode, r.stderr)
    assert "Unknown subcommand: notebook" in r.stderr, r.stderr
    assert "predates `dockwright notebook`" in r.stderr, r.stderr
    assert not _handoff_leftovers(home)
    assert not log.exists() or "new-session" not in log.read_text()
