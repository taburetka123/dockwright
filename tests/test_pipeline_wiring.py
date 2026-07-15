import json
from pathlib import Path

import pytest

from dockwright import pipeline_wiring as pw


def _settings(tmp_path):
    return tmp_path / "settings.json"


def _scripts(tmp_path):
    return tmp_path / "scripts"


def test_enable_selffix_on_absent_file_creates_session_end_hook(tmp_path):
    sp = _settings(tmp_path)
    changed = pw.enable_selffix(sp, _scripts(tmp_path))
    assert changed is True
    data = json.loads(sp.read_text())
    cmds = [h["command"] for b in data["hooks"]["SessionEnd"] for h in b["hooks"]]
    assert any("selffix-trigger.sh" in c for c in cmds)
    assert pw.is_selffix_wired(sp) is True


def test_enable_selffix_idempotent(tmp_path):
    sp = _settings(tmp_path)
    pw.enable_selffix(sp, _scripts(tmp_path))
    changed = pw.enable_selffix(sp, _scripts(tmp_path))
    assert changed is False
    data = json.loads(sp.read_text())
    cmds = [h["command"] for b in data["hooks"]["SessionEnd"] for h in b["hooks"]]
    assert sum("selffix-trigger.sh" in c for c in cmds) == 1


def test_enable_selffix_preserves_existing_hooks(tmp_path):
    sp = _settings(tmp_path)
    sp.write_text(json.dumps({
        "model": "opus",
        "hooks": {"SessionEnd": [{"hooks": [
            {"type": "command", "command": "bash /x/.venv/bin/dockwright session-end"}]}]}}))
    pw.enable_selffix(sp, _scripts(tmp_path))
    data = json.loads(sp.read_text())
    cmds = [h["command"] for b in data["hooks"]["SessionEnd"] for h in b["hooks"]]
    assert any("dockwright session-end" in c for c in cmds)
    assert any("selffix-trigger.sh" in c for c in cmds)
    assert data["model"] == "opus"


def test_disable_selffix_removes_only_selffix(tmp_path):
    sp = _settings(tmp_path)
    sp.write_text(json.dumps({"hooks": {"SessionEnd": [
        {"hooks": [{"type": "command", "command": "bash /x/dockwright session-end"}]},
        {"hooks": [{"type": "command", "command": "bash /s/selffix-trigger.sh", "timeout": 30}]}]}}))
    changed = pw.disable_selffix(sp)
    assert changed is True
    cmds = [h["command"] for b in json.loads(sp.read_text())["hooks"]["SessionEnd"] for h in b["hooks"]]
    assert any("dockwright session-end" in c for c in cmds)
    assert not any("selffix-trigger.sh" in c for c in cmds)


def test_disable_selffix_noop_on_absent(tmp_path):
    assert pw.disable_selffix(_settings(tmp_path)) is False


def test_enable_writes_backup_on_change(tmp_path):
    sp = _settings(tmp_path)
    sp.write_text(json.dumps({"model": "x"}))
    pw.enable_selffix(sp, _scripts(tmp_path))
    assert list(tmp_path.glob("settings.json.bak.*"))


def test_dispatch_selffix_routes_to_pipeline_wiring(monkeypatch):
    from dockwright import __main__ as m
    called = {}
    def _fake(argv):        # NOT a setdefault-lambda: setdefault returns the value, not 0
        called["argv"] = argv
        return 0
    monkeypatch.setattr(pw, "selffix_main", _fake)
    monkeypatch.setattr(m.sys, "argv", ["dockwright", "selffix", "enable"])
    with pytest.raises(SystemExit) as ei:
        m.main()
    assert ei.value.code == 0
    assert called["argv"] == ["enable"]


def _wire_selffix(tmp_path):
    sp = _settings(tmp_path)
    pw.enable_selffix(sp, _scripts(tmp_path))
    return sp


def test_enable_gardener_refuses_without_selffix_for_digest(tmp_path):
    sp = _settings(tmp_path)   # selffix NOT wired
    installer = tmp_path / "gardener-install.sh"
    installer.write_text("#!/bin/sh\n")
    calls = []
    rc = pw.enable_gardener("digest", settings_path=sp, installer_path=installer,
                            run=lambda cmd: calls.append(cmd) or 0)
    assert rc == 1
    assert calls == []   # never ran the installer


def test_enable_gardener_frontier_lane_skips_selffix_gate(tmp_path):
    sp = _settings(tmp_path)   # selffix NOT wired
    installer = tmp_path / "gardener-install.sh"
    installer.write_text("#!/bin/sh\n")
    calls = []
    rc = pw.enable_gardener("frontier", settings_path=sp, installer_path=installer,
                            run=lambda cmd: calls.append(cmd) or 0)
    assert rc == 0
    assert calls == [["bash", str(installer), "--lane", "frontier"]]


def test_enable_gardener_runs_installer_with_lane_when_selffix_wired(tmp_path):
    sp = _wire_selffix(tmp_path)
    installer = tmp_path / "gardener-install.sh"
    installer.write_text("#!/bin/sh\n")
    calls = []
    rc = pw.enable_gardener("all", settings_path=sp, installer_path=installer,
                            run=lambda cmd: calls.append(cmd) or 0)
    assert rc == 0
    assert calls == [["bash", str(installer), "--lane", "all"]]


def test_enable_gardener_errors_when_installer_missing(tmp_path):
    sp = _wire_selffix(tmp_path)
    rc = pw.enable_gardener("digest", settings_path=sp,
                            installer_path=tmp_path / "nope.sh",
                            run=lambda cmd: 0)
    assert rc == 2


def test_disable_gardener_boots_out_and_unlinks_selected_lane(tmp_path):
    la = tmp_path / "LaunchAgents"
    la.mkdir()
    (la / "com.dockwright.gardener-gate.plist").write_text("x")
    (la / "com.dockwright.gardener-frontier.plist").write_text("x")
    calls = []
    pw.disable_gardener("digest", launch_agents_dir=la, label_prefix="com.dockwright",
                        run=lambda cmd: calls.append(cmd) or 0, uid=501)
    assert calls == [["launchctl", "bootout", "gui/501/com.dockwright.gardener-gate"]]
    assert not (la / "com.dockwright.gardener-gate.plist").exists()
    assert (la / "com.dockwright.gardener-frontier.plist").exists()   # untouched


def test_disable_gardener_all_removes_both(tmp_path):
    la = tmp_path / "LaunchAgents"
    la.mkdir()
    (la / "com.dockwright.gardener-gate.plist").write_text("x")
    (la / "com.dockwright.gardener-frontier.plist").write_text("x")
    pw.disable_gardener("all", launch_agents_dir=la, label_prefix="com.dockwright",
                        run=lambda cmd: 0, uid=501)
    assert not list(la.glob("*.plist"))


def test_disable_gardener_missing_plist_is_noop_not_error(tmp_path):
    la = tmp_path / "LaunchAgents"
    la.mkdir()
    pw.disable_gardener("all", launch_agents_dir=la, label_prefix="com.dockwright",
                        run=lambda cmd: 1, uid=501)   # bootout "fails" — swallowed


def test_dispatch_gardener_routes_to_pipeline_wiring(monkeypatch):
    from dockwright import __main__ as m
    called = {}
    def _fake(argv):
        called["argv"] = argv
        return 0
    monkeypatch.setattr(pw, "gardener_main", _fake)
    monkeypatch.setattr(m.sys, "argv", ["dockwright", "gardener", "enable", "--lane", "frontier"])
    with pytest.raises(SystemExit) as ei:
        m.main()
    assert ei.value.code == 0
    assert called["argv"] == ["enable", "--lane", "frontier"]
