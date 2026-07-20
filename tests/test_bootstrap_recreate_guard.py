"""bootstrap-recreate.sh account stamping must read the registry (any pool
account name), not the hardcoded a/b pair (F4) — a custom account name gets
no CONFIG_PREFIX stamp and silently rides the default login.

Also pins the incident guard (2026-07-17): the actual 08:41 vector was an AGENT
hand-running the script under a sandboxed HOME — and -L namespaces tmux by uid,
not HOME, so it spawned two rogue managers onto the LIVE socket. The script now:
  * --dry-run                    → prints the plan, exits before any spawn;
  * sandboxed HOME + live socket → REFUSED (exit 3), naming --dry-run;
  * sandboxed HOME + scratch sock→ still spawns (the legitimate test shape — the
                                   guard's socket gate must not over-fire).
All run the REAL script and are self-contained in safety — each prepends its OWN
fake-tmux dir to PATH, so any spawn tail hits a logging stub, never real tmux,
even if the conftest autouse shim is ever removed."""
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "deploy" / "scripts" / "bootstrap-recreate.sh"


def test_bootstrap_recreate_stamps_from_registry_not_ab_hardcode():
    text = SCRIPT.read_text()
    executed = "\n".join(l for l in text.splitlines()
                         if not l.lstrip().startswith("#"))
    assert '"$ACTIVE_LETTER" = "a"' not in executed      # the old hardcode
    assert 'account-registry.json' in executed


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


def _run_bootstrap(tmp_path, fakebin, *extra, env_overrides=None):
    home = tmp_path / "home"
    (home / ".claude" / "dockwright").mkdir(parents=True)
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
    assert list((home / ".claude" / "dockwright" / "handoffs").glob("*.json"))


def test_sandboxed_home_live_socket_is_refused(tmp_path):
    """This IS the 2026-07-17 08:41 incident shape, now refused instead of
    spawning: an agent hand-ran the script under a sandboxed HOME against the
    live/default socket, and -L namespaces tmux by uid not HOME, so it spawned
    onto the live fleet. The guard refuses it (exit 3, naming --dry-run) BEFORE
    any tmux call — the fake-tmux log stays absent.
    RED against the unfixed script: no guard → the spawn tail reached tmux
    (`-L dockwright` + `/manager-resume` in the log), exit != 3 (still caged by
    the test-local fake tmux)."""
    fakebin, log = _fake_tmux_dir(tmp_path)
    r, home = _run_bootstrap(tmp_path, fakebin)
    assert r.returncode == 3, f"expected refusal exit 3, got {r.returncode}: {r.stderr}"
    assert "--dry-run" in r.stderr, r.stderr
    assert not log.exists(), f"refusal still reached tmux: {log.read_text()}"


def test_sandboxed_home_scratch_socket_still_spawns(tmp_path):
    """The guard's socket gate is a deliberate refinement over a bare HOME check:
    a sandboxed HOME against an EXPLICIT scratch socket is a legitimate test shape
    (test_gardener_run_tmux does it), so the guard fires only on sandbox-HOME +
    live/default socket. This preserves the old test's spawn-tail pin (the tail
    reaches tmux) AND proves the socket gate does not over-fire."""
    fakebin, log = _fake_tmux_dir(tmp_path)
    sock = f"wt-iso-{os.getpid()}-probe"
    home = tmp_path / "home"
    (home / ".claude" / "dockwright").mkdir(parents=True)
    env = {**os.environ, "HOME": str(home),
           "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
           "DOCKWRIGHT_TMUX_SOCKET": sock}
    subprocess.run(
        ["bash", str(SCRIPT), "--narrative", "probe", "--from-sid", "sid-x"],
        capture_output=True, text=True, env=env)
    invocations = log.read_text() if log.exists() else ""
    assert f"-L {sock}" in invocations and "/manager-resume" in invocations, invocations


def test_dry_run_cmd_carries_remote_control(tmp_path):
    """The composed RUNTIME_CMD — printed VERBATIM by --dry-run as cmd=[…] —
    carries --remote-control before the /manager-resume prompt. Anchored to
    the executed command, not script prose (drift-guard discipline); same
    tail as manager_launch.manager_claude_args()."""
    fakebin, _log = _fake_tmux_dir(tmp_path)
    r, _home = _run_bootstrap(tmp_path, fakebin, "--dry-run")
    assert r.returncode == 0, r.stderr
    cmd = next(l for l in r.stdout.splitlines() if "cmd=[" in l)
    assert "--remote-control" in cmd, cmd
    assert cmd.index("--remote-control") < cmd.index("/manager-resume"), cmd
    # Parse-shape invariant: --remote-control adjacent to the --model dash-option
    # (never the trailing /manager-resume prompt, which --remote-control [name]
    # would otherwise bind as the RC session name).
    assert "--remote-control --model" in cmd, cmd


def test_dry_run_cmd_rc_opt_out(tmp_path):
    fakebin, _log = _fake_tmux_dir(tmp_path)
    r, _home = _run_bootstrap(tmp_path, fakebin, "--dry-run",
                              env_overrides={"DOCKWRIGHT_MANAGER_RC": "0"})
    assert r.returncode == 0, r.stderr
    cmd = next(l for l in r.stdout.splitlines() if "cmd=[" in l)
    assert "--remote-control" not in cmd, cmd


def test_dry_run_cmd_carries_skip_perms_opt_in(tmp_path):
    """RUNTIME_CMD (printed VERBATIM by --dry-run as cmd=[…]) carries the flag
    when DOCKWRIGHT_MANAGER_SKIP_PERMS=1 — anchored to the executed command,
    not script prose. Adjacency pins the parse shape: RC, then skip, then the
    --model dash-option."""
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
    """Server-birth stickiness guard (spec § Server-birth scrub): the script
    unsets the var AFTER composing RUNTIME_CMD, so the tmux invocation (which
    may birth the server) sees a clean env while the spawned command line still
    carries the one-shot flag. The fake tmux logs ENV_SKIP=<value-or-unset>
    per invocation. Scratch socket: the sandbox-HOME guard only permits
    non-dry-run spawns on an explicit non-live socket."""
    fakebin, log = _fake_tmux_dir(tmp_path)
    sock = f"wt-iso-{os.getpid()}-skip"
    home = tmp_path / "home"
    (home / ".claude" / "dockwright").mkdir(parents=True)
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
