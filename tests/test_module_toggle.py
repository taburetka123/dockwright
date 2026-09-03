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
    home = _home(tmp_path)
    r = subprocess.run(["python3", str(SCRIPTS / "gardener_gate.py"), "--dry-run"],
                       env=_env(str(tmp_path / "nope.toml"), home),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "module-off" not in (r.stdout + r.stderr)


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
    assert not (home / ".claude" / "dockwright" / "selffix" / "findings" / "runsid.md").exists()
    log = home / ".claude" / "dockwright" / "selffix" / "trigger.log"
    assert log.is_file() and "module-off" in log.read_text()


def test_gardener_run_noops_when_off(tmp_path):
    home = _home(tmp_path)
    r = subprocess.run(["bash", str(SCRIPTS / "gardener-run.sh"), "--trigger", "force"],
                       env=_env(_toml(tmp_path, OFF), home),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
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
    assert not (home / ".claude" / "dockwright" / "gardener").exists()
