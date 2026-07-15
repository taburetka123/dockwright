"""[modules] gardener=false must cleanly no-op EVERY automated entry point.

The HARD design-gate requirement (dockwright Step 7b): with
`[modules] gardener=false` in dockwright.toml, the whole Gardener/selffix
pipeline no-ops — the two python gates print `module-off` and spawn nothing,
the three bash scripts early-exit before doing any work, and the installer
refuses. Default-true fail-open (no config / gardener unset / gardener=true)
is proven by the `*_runs_*` companion tests so the toggle can't silently
disable a healthy install.

Every script is exec'd straight from deploy/scripts/ (the source of truth,
same as test_selffix_detect / test_gardener_gate) so loop-label-prefix.sh sits
adjacent and the sourced `dockwright_module_enabled` helper resolves.
"""
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "deploy" / "scripts"

OFF = "[modules]\ngardener = false\n"
ON = "[modules]\ngardener = true\n"


def _toml(tmp_path, body):
    p = tmp_path / "dockwright.toml"
    p.write_text(body)
    return str(p)


def _home(tmp_path, debug=False):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    if debug:
        (home / ".claude" / "selffix-debug").touch()
    return home


def _env(config, home=None):
    env = {**os.environ, "DOCKWRIGHT_CONFIG": config}
    env.pop("SELFFIX_DEBUG", None)
    if home is not None:
        env["HOME"] = str(home)
    return env


# --- Python gates: gardener_gate.py / frontier_gate.py ---------------------

def test_gardener_gate_noops_when_off(tmp_path):
    home = _home(tmp_path)
    r = subprocess.run(["python3", str(SCRIPTS / "gardener_gate.py"), "--dry-run"],
                       env=_env(_toml(tmp_path, OFF), home),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "module-off" in (r.stdout + r.stderr)
    assert not (home / ".claude" / "dockwright" / "gardener").exists()


def test_frontier_gate_noops_when_off(tmp_path):
    home = _home(tmp_path)
    r = subprocess.run(["python3", str(SCRIPTS / "frontier_gate.py"), "--dry-run"],
                       env=_env(_toml(tmp_path, OFF), home),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "module-off" in (r.stdout + r.stderr)
    assert not (home / ".claude" / "dockwright" / "gardener").exists()


def test_gardener_gate_runs_when_on(tmp_path):
    home = _home(tmp_path)
    r = subprocess.run(["python3", str(SCRIPTS / "gardener_gate.py"), "--dry-run"],
                       env=_env(_toml(tmp_path, ON), home),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "module-off" not in (r.stdout + r.stderr)
    assert "gardener-gate:" in r.stdout


def test_frontier_gate_runs_when_on(tmp_path):
    home = _home(tmp_path)
    r = subprocess.run(["python3", str(SCRIPTS / "frontier_gate.py"), "--dry-run"],
                       env=_env(_toml(tmp_path, ON), home),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "module-off" not in (r.stdout + r.stderr)
    assert "frontier-gate:" in r.stdout


def test_gardener_gate_fail_open_when_config_absent(tmp_path):
    """No config file at all (default) => module enabled (fail-open)."""
    home = _home(tmp_path)
    r = subprocess.run(["python3", str(SCRIPTS / "gardener_gate.py"), "--dry-run"],
                       env=_env(str(tmp_path / "nope.toml"), home),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "module-off" not in (r.stdout + r.stderr)


def test_child_env_inherits_pytest_current_test(tmp_path):
    """The scripts here run as REAL subprocesses; their notify helpers no-op
    on PYTEST_CURRENT_TEST (the 2026-07-03 desktop-notification leak). That
    guard only holds if _env() keeps propagating the var to children — pin it."""
    r = subprocess.run(
        ["python3", "-c",
         "import os, sys; sys.exit(0 if os.environ.get('PYTEST_CURRENT_TEST') else 1)"],
        env=_env(str(tmp_path / "nope.toml")), capture_output=True)
    assert r.returncode == 0, "PYTEST_CURRENT_TEST must reach subprocess-exec'd scripts"


# --- Bash scripts ----------------------------------------------------------

def test_selffix_trigger_noops_when_off(tmp_path):
    home = _home(tmp_path, debug=True)
    r = subprocess.run(["bash", str(SCRIPTS / "selffix-trigger.sh")],
                       env=_env(_toml(tmp_path, OFF), home),
                       input='{"session_id":"m1","transcript_path":"/nonexistent"}',
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    log = home / ".claude" / "dockwright" / "selffix" / "trigger.log"
    assert log.is_file() and "module-off" in log.read_text()


def test_selffix_trigger_runs_when_config_absent(tmp_path):
    """Fail-open: no config => the trigger proceeds to its normal detect. The
    /nonexistent transcript makes it log a skip (not module-off)."""
    home = _home(tmp_path, debug=True)
    r = subprocess.run(["bash", str(SCRIPTS / "selffix-trigger.sh")],
                       env=_env(str(tmp_path / "nope.toml"), home),
                       input='{"session_id":"m1","transcript_path":"/nonexistent"}',
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    log = home / ".claude" / "dockwright" / "selffix" / "trigger.log"
    assert log.is_file() and "module-off" not in log.read_text()


def test_selffix_run_noops_when_off(tmp_path):
    home = _home(tmp_path, debug=True)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    r = subprocess.run(["bash", str(SCRIPTS / "selffix-run.sh"), str(transcript), "runsid"],
                       env=_env(_toml(tmp_path, OFF), home),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    # no-oped before touching OUT / acquiring the lock / spawning claude
    assert not (home / ".claude" / "dockwright" / "selffix" / "findings" / "runsid.md").exists()
    log = home / ".claude" / "dockwright" / "selffix" / "trigger.log"
    assert log.is_file() and "module-off" in log.read_text()


def test_gardener_run_noops_when_off(tmp_path):
    home = _home(tmp_path)
    r = subprocess.run(["bash", str(SCRIPTS / "gardener-run.sh"), "--trigger", "force"],
                       env=_env(_toml(tmp_path, OFF), home),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    # no-oped before the mutex / spawn: no run dir, no digests
    runs = home / ".claude" / "dockwright" / "gardener" / "runs"
    assert not runs.exists() or not any(runs.iterdir())
    run_log = home / ".claude" / "dockwright" / "gardener" / "run.log"
    assert run_log.is_file() and "module-off" in run_log.read_text()


def test_gardener_install_refuses_when_off(tmp_path):
    home = _home(tmp_path)
    r = subprocess.run(["bash", str(SCRIPTS / "gardener-install.sh")],
                       env=_env(_toml(tmp_path, OFF), home),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = (r.stdout + r.stderr).lower()
    assert "disabled" in out or "module" in out
    assert "Loaded:" not in (r.stdout + r.stderr)
    # refused before creating any gardener state or touching launchd
    assert not (home / ".claude" / "dockwright" / "gardener").exists()
