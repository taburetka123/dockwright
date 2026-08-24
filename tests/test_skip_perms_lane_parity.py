"""Cross-lane parity for the DOCKWRIGHT_MANAGER_SKIP_PERMS opt-in.

ONE security-relevant gate (it strips the Bash safety classifier off the
launched manager), THREE independent comparison sites — and no shared helper
can bind them: stale_monitor.py is dual-homed as a standalone stdlib-only
script that cannot import the package, and bootstrap-recreate.sh is standalone
bash. The duplication is architectural, so the anti-drift device has to be a
test that drives every lane with the SAME value and asserts they answer
identically.

bash is the reference implementation — `[ "${DOCKWRIGHT_MANAGER_SKIP_PERMS:-}"
= "1" ]`, an exact compare with no normalization. PR #218 Tier-2 review found
the two Python lanes running `.strip() == "1"`, so a whitespace-padded value
("1 ", " 1", "\\n1") authorized a skip-permissions launch in Python while bash
read the same environment as OFF. Lanes disagreeing about whether a security
opt-in is ON is the defect; matching Python to bash is the fix.

Drift-guard discipline (~/.claude/rules/drift-guard-tests.md): every lane is
driven through its EXECUTED path with a real environment value. No lane is
checked by substring-matching its source — all three files quote the variable
name verbatim in prose, so a grep-the-file guard would stay green with the gate
deleted outright.
"""
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

# (env value, gate expected ON). None means the variable is unset entirely.
# Exactly "1" enables; every whitespace-padded neighbour must NOT, because
# bash's `=` never normalizes and the lanes have to agree.
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
    """Fresh-boot lane — also the recreate lane, which imports this helper
    from mcp_server rather than re-implementing the compare."""
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "no-presets")
    _set_var(monkeypatch, value)
    return FLAG in manager_launch.manager_claude_args()


def _lane_stale_monitor(monkeypatch, tmp_path, value):
    """Account-flip recovery lane — loaded from source the way the deployed
    standalone copy runs, and driven through the real argv composition.

    HOME is redirected BEFORE exec_module because the module binds HOME and
    every derived state path (ROOT, ACTIVE, CLOSED, …) at import time, and
    resolving ROOT stats the operator's real ~/.claude/dockwright. Patching
    mod.ROOT afterwards covers only the attribute this lane happens to read
    today; scratch-by-construction keeps the test hermetic if
    _launch_recovery_manager ever reaches for a sibling path."""
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
    """Reference lane — runs the REAL script and reads the RUNTIME_CMD that
    --dry-run prints verbatim as cmd=[…]. Self-contained in safety: its own
    fake-tmux dir leads PATH, so nothing here can reach real tmux."""
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
    """Each lane must read the same environment the same way, and that way is
    bash's exact compare. A lane that normalizes whitespace turns "1 " into an
    authorized skip-permissions launch that the other lanes refuse."""
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


def test_parity_cases_cover_the_whitespace_drift():
    """The table itself is load-bearing: the defect this module exists for is
    whitespace-only padding, so a future trim that leaves only ""/"0"/"true"
    would keep the module green while re-opening the exact hole."""
    padded = {v for v, _ in CASES if v not in (None, "") and v.strip() == "1" and v != "1"}
    assert padded >= {"1 ", " 1", "\n1", "1\n"}, padded
