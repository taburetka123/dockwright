"""gardener-install.sh --lane writes only the selected lane's plist(s), and the
shared prelude ($GARDENER_DIR + selffix-debug flag) is created for EVERY lane."""
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "deploy" / "scripts" / "gardener-install.sh"


def _run(tmp_path, lane_args):
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    (home / ".claude" / "scripts").mkdir(parents=True)
    # The installer requires the gate scripts to exist.
    (home / ".claude" / "scripts" / "gardener_gate.py").write_text("# stub\n")
    (home / ".claude" / "scripts" / "frontier_gate.py").write_text("# stub\n")
    # Stub launchctl so the installer never mutates the real machine's launchd
    # (temp $HOME does not sandbox launchd's per-uid GUI domain). The plist FILES
    # are written by `cat > $PLIST_PATH` before any launchctl call, so every
    # plist-file / prelude assertion still holds with zero launchd side effect.
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    launchctl_stub = stub_bin / "launchctl"
    launchctl_stub.write_text("#!/bin/sh\nexit 0\n")
    launchctl_stub.chmod(0o755)

    # Config-hermetic: scrub the operator's real dockwright.toml discovery so the
    # module gate reads the default (gardener enabled), not an operator override.
    env = {k: v for k, v in os.environ.items()
           if k not in ("DOCKWRIGHT_CONFIG", "XDG_CONFIG_HOME")}
    env["HOME"] = str(home)
    env["PATH"] = f"{stub_bin}:{env.get('PATH', '')}"
    r = subprocess.run(["bash", str(INSTALLER), *lane_args], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    la = home / "Library" / "LaunchAgents"
    plists = {p.name for p in la.glob("*.plist")}
    gdir = home / ".claude" / "dockwright" / "gardener"
    debug = home / ".claude" / "dockwright" / "selffix" / "debug"
    return plists, gdir, debug


def test_lane_digest_installs_only_gate(tmp_path):
    plists, gdir, debug = _run(tmp_path, ["--lane", "digest"])
    assert any("gardener-gate" in p for p in plists)
    assert not any("gardener-frontier" in p for p in plists)
    assert gdir.is_dir() and debug.exists()   # shared prelude ran


def test_lane_frontier_installs_only_frontier_and_shared_prelude(tmp_path):
    plists, gdir, debug = _run(tmp_path, ["--lane", "frontier"])
    assert any("gardener-frontier" in p for p in plists)
    assert not any(p.endswith("gardener-gate.plist") for p in plists)
    assert gdir.is_dir() and debug.exists()   # I-4: prelude NOT skipped for frontier


def test_bare_invocation_installs_both(tmp_path):
    plists, _, _ = _run(tmp_path, [])
    assert any("gardener-gate" in p for p in plists)
    assert any("gardener-frontier" in p for p in plists)
