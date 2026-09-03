import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from dockwright import manager_launch

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "deploy" / "scripts" / "bootstrap-recreate.sh"
STALE_MONITOR_PATH = REPO / "src" / "dockwright" / "stale_monitor.py"

VAR = "DOCKWRIGHT_MANAGER_SKIP_PERMS"
FLAG = "--dangerously-skip-permissions"

CASES = [
    ("1", True),
    ("1 ", False),
    (" 1", False),
    ("\n1", False),
    ("1\n", False),
    ("", False),
    ("0", False),
    ("true", False),
    (None, False),
]
IDS = ["exact-1", "trailing-space", "leading-space", "leading-newline",
       "trailing-newline", "empty", "zero", "true", "unset"]


def _set_var(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(VAR, raising=False)
    else:
        monkeypatch.setenv(VAR, value)


def _lane_manager_launch(monkeypatch, tmp_path, value):
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "no-presets")
    _set_var(monkeypatch, value)
    return FLAG in manager_launch.manager_claude_args()


def _lane_stale_monitor(monkeypatch, tmp_path, value):
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "stale_monitor_parity", STALE_MONITOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "_account_config_prefix", lambda letter: "")
    captured = {}

    class FakeDrv:
        async def spawn(self, **kw):
            captured.update(kw)
            return "%9"

    monkeypatch.setattr(mod, "_get_driver", lambda: FakeDrv())
    _set_var(monkeypatch, value)
    mod._launch_recovery_manager({"cwd": "/c", "name": "m"}, "sid-1", "a")
    return FLAG in captured["argv"][-1]


def _lane_bootstrap_bash(tmp_path, value):
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(parents=True)
    (fakebin / "tmux").write_text(
        "#!/bin/bash\n"
        "case \"$*\" in *has-session*) exit 1 ;; esac\nexit 0\n")
    (fakebin / "tmux").chmod(0o755)
    (fakebin / "jq").symlink_to(shutil.which("jq"))
    (fakebin / "uuidgen").symlink_to(shutil.which("uuidgen"))
    home = tmp_path / "home"
    active = home / ".claude" / "dockwright" / "active"
    active.mkdir(parents=True)
    (active / "sid-x.json").write_text(json.dumps(
        {"claude_sid": "sid-x", "agent": "manager", "name": "mighty-demon",
         "domain": "personal", "pid": 4242}))
    env = {**os.environ, "HOME": str(home),
           "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}"}
    env.pop("DOCKWRIGHT_MANAGER_RC", None)
    env.pop(VAR, None)
    if value is not None:
        env[VAR] = value
    r = subprocess.run(
        ["bash", str(SCRIPT), "--narrative", "probe", "--from-sid", "sid-x",
         "--dry-run"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    cmd = next(l for l in r.stdout.splitlines() if "cmd=[" in l)
    return FLAG in cmd


@pytest.mark.parametrize("value,expected", CASES, ids=IDS)
def test_skip_perms_gate_agrees_across_lanes(monkeypatch, tmp_path, value, expected):
    lanes = {
        "manager_launch.manager_claude_args": _lane_manager_launch(
            monkeypatch, tmp_path / "ml", value),
        "stale_monitor._launch_recovery_manager": _lane_stale_monitor(
            monkeypatch, tmp_path / "sm", value),
        "bootstrap-recreate.sh": _lane_bootstrap_bash(tmp_path / "sh", value),
    }
    disagreeing = {name: on for name, on in lanes.items() if on is not expected}
    assert not disagreeing, (
        f"{VAR}={value!r} must leave the gate "
        f"{'ON' if expected else 'OFF'} in every lane; these disagreed: "
        f"{disagreeing} (bash is the reference — exact `= \"1\"`, no strip)")
