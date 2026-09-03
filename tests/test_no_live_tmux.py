import asyncio

import pytest

from dockwright import terminal

_SENTINEL_PANE = "%no-live-tmux"


def test_tmux_sync_ops_are_absorbed(no_live_tmux, monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TERMINAL", "tmux")
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "claude-orch")
    terminal._DRIVER = None

    drv = terminal.get_driver()
    drv.send_text("%9", "resume your task")

    assert any("load-buffer" in a for a in no_live_tmux.run)
    assert any("send-keys" in a and a[-1] == "Enter" for a in no_live_tmux.run)
    assert all(a[0] == "tmux" for a in no_live_tmux.run)


@pytest.mark.real_tmux
def test_real_tmux_fixture_drives_throwaway_socket(real_tmux):
    sock = real_tmux
    assert sock != "claude-orch" and sock.startswith("wt-iso-")

    drv = terminal.get_driver()
    assert isinstance(drv, terminal.TmuxDriver)
    assert drv.socket() == sock

    pane = asyncio.run(drv.spawn(cwd="/tmp", title="iso", argv=["sleep", "600"],
                                 route_to_workers_window=True))
    assert pane and pane != _SENTINEL_PANE
    assert asyncio.run(drv.pane_exists(pane)) is True
