from pathlib import Path
import pytest
from dockwright import manager_launch


def test_build_command_fresh_new_session_with_conf(monkeypatch, tmp_path):
    conf = tmp_path / "dockwright.tmux.conf"
    conf.write_text("# conf")
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")
    monkeypatch.setattr(manager_launch, "_conf", lambda: conf)
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: False)
    cmd = manager_launch.build_command()
    assert cmd == ["tmux", "-L", "dockwright", "-f", str(conf),
                   "new-session", "-s", "mgr", "--",
                   "claude", "--model", "opus[1m]", "/manager"]


def test_build_command_without_conf_omits_dash_f(monkeypatch):
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")
    monkeypatch.setattr(manager_launch, "_conf", lambda: None)
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: False)
    cmd = manager_launch.build_command()
    assert "-f" not in cmd


def test_build_command_existing_mgr_session_attaches(monkeypatch):
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")
    monkeypatch.setattr(manager_launch, "_conf", lambda: None)
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: True)
    cmd = manager_launch.build_command()
    assert cmd == ["tmux", "-L", "dockwright", "attach-session", "-t", "mgr"]


def test_main_refuses_inside_tmux(monkeypatch, capsys):
    monkeypatch.setenv("TMUX", "/tmp/sock,1,0")
    assert manager_launch.main([]) == 2
    assert "inside tmux" in capsys.readouterr().err


def test_main_sources_conf_before_new_session_on_bare_alive_server(monkeypatch, tmp_path):
    conf = tmp_path / "dockwright.tmux.conf"
    conf.write_text("# conf")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")
    monkeypatch.setattr(manager_launch, "_conf", lambda: conf)
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: False)
    monkeypatch.setattr(manager_launch, "_server_alive", lambda: True)

    run_calls = []

    def _fake_run(cmd, **kwargs):
        run_calls.append(cmd)
        class _Result:
            returncode = 0
        return _Result()

    monkeypatch.setattr(manager_launch.subprocess, "run", _fake_run)

    exec_calls = []
    monkeypatch.setattr(manager_launch.os, "execvp",
                         lambda prog, cmd: exec_calls.append(cmd))

    manager_launch.main([])

    assert ["tmux", "-L", "dockwright", "source-file", str(conf)] in run_calls
    assert exec_calls == [["tmux", "-L", "dockwright", "-f", str(conf),
                            "new-session", "-s", "mgr", "--",
                            "claude", "--model", "opus[1m]", "/manager"]]


def test_main_attaches_to_existing_mgr_session_without_sourcing(monkeypatch, capsys):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")
    monkeypatch.setattr(manager_launch, "_conf", lambda: None)
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: True)

    def _unexpected_run(cmd, **kwargs):
        raise AssertionError(f"subprocess.run should not be called: {cmd}")

    monkeypatch.setattr(manager_launch.subprocess, "run", _unexpected_run)

    exec_calls = []
    monkeypatch.setattr(manager_launch.os, "execvp",
                         lambda prog, cmd: exec_calls.append(cmd))

    manager_launch.main([])

    assert exec_calls == [["tmux", "-L", "dockwright", "attach-session", "-t", "mgr"]]
    assert "attaching to existing manager session" in capsys.readouterr().err
