import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "deploy" / "scripts" / "gardener-install.sh"


PASSING_LAUNCHCTL = "#!/bin/sh\nexit 0\n"

FAILING_LAUNCHCTL = """#!/bin/sh
case "$1" in
  bootstrap) exit 125 ;;
  list) exit 1 ;;
esac
exit 0
"""


def _run_installer(tmp_path, lane_args, launchctl_body=PASSING_LAUNCHCTL):
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    (home / ".claude" / "scripts").mkdir(parents=True)
    (home / ".claude" / "scripts" / "gardener_gate.py").write_text("# stub\n")
    (home / ".claude" / "scripts" / "frontier_gate.py").write_text("# stub\n")
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    launchctl_stub = stub_bin / "launchctl"
    launchctl_stub.write_text(launchctl_body)
    launchctl_stub.chmod(0o755)

    env = {k: v for k, v in os.environ.items()
           if k not in ("DOCKWRIGHT_CONFIG", "XDG_CONFIG_HOME")}
    env["HOME"] = str(home)
    env["PATH"] = f"{stub_bin}:{env.get('PATH', '')}"
    return subprocess.run(["bash", str(INSTALLER), *lane_args], env=env,
                          capture_output=True, text=True), home


def _run(tmp_path, lane_args):
    r, home = _run_installer(tmp_path, lane_args)
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
    assert gdir.is_dir() and debug.exists()


def test_lane_frontier_installs_only_frontier_and_shared_prelude(tmp_path):
    plists, gdir, debug = _run(tmp_path, ["--lane", "frontier"])
    assert any("gardener-frontier" in p for p in plists)
    assert not any(p.endswith("gardener-gate.plist") for p in plists)
    assert gdir.is_dir() and debug.exists()


def test_bare_invocation_installs_both(tmp_path):
    plists, _, _ = _run(tmp_path, [])
    assert any("gardener-gate" in p for p in plists)
    assert any("gardener-frontier" in p for p in plists)


def test_bootstrap_failure_exits_nonzero_and_reports_not_armed(tmp_path):
    r, home = _run_installer(tmp_path, ["--lane", "digest"], FAILING_LAUNCHCTL)
    assert r.returncode != 0
    assert "NOT armed" in r.stderr
    assert "gardener-gate" in r.stderr
    plists = {p.name for p in (home / "Library" / "LaunchAgents").glob("*.plist")}
    assert any("gardener-gate" in p for p in plists)
    assert "Gardener loops installed" not in r.stdout


def test_bootstrap_failure_all_lane_names_both_labels(tmp_path):
    r, _ = _run_installer(tmp_path, [], FAILING_LAUNCHCTL)
    assert r.returncode != 0
    assert "gardener-gate" in r.stderr
    assert "gardener-frontier" in r.stderr
