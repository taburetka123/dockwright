"""Regression tests for the no_live_tmux / real_tmux test-isolation guard.

Motivating incident: running the suite spawned 50+ real
`claude /manager-takeover-recovery` sessions into the live `mgr` tmux session.
The recovery spawn (stale_monitor -> TmuxDriver.spawn) shells tmux through
asyncio.create_subprocess_exec. These tests pin that the spawn is now
intercepted, and that any test which genuinely drives real tmux is physically
barred from the live socket / mgr.
"""
import asyncio

import pytest

from dockwright import terminal

# Mirror of conftest._ABSORBED_TMUX_PANE — duplicated on purpose so a change to the
# sentinel trips this regression test and is reviewed.
_SENTINEL_PANE = "%no-live-tmux"


def test_tmux_spawn_is_absorbed_not_executed(no_live_tmux, monkeypatch):
    """The detonation path: tmux backend + the real TmuxDriver, even pointed at
    the LIVE socket. The recovery new-window spawn must be intercepted (sentinel
    pane, recorded argv carrying the dangerous command) and never executed."""
    monkeypatch.setenv("CLAUDE_ORCH_TERMINAL", "tmux")
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "claude-orch")  # live socket — still absorbed
    terminal._DRIVER = None

    drv = terminal.get_driver()
    assert isinstance(drv, terminal.TmuxDriver)

    pane = asyncio.run(drv.spawn(
        cwd="/tmp", title="manager (recovery)",
        argv=["zsh", "-ic", "claude /manager-takeover-recovery sid-1"],
        route_to_manager_session=True))

    assert pane == _SENTINEL_PANE, "spawn returned a real pane id — it was NOT absorbed"
    spawns = [a for a in no_live_tmux.exec if "new-window" in a or "new-session" in a]
    assert spawns, "the spawn never reached the absorber"
    assert any("/manager-takeover-recovery sid-1" in " ".join(a) for a in no_live_tmux.exec), \
        "the dangerous recovery command was not the one intercepted"


def test_tmux_sync_ops_are_absorbed(no_live_tmux, monkeypatch):
    """Sync tmux ops (send_text -> subprocess.run) are absorbed, not shelled."""
    monkeypatch.setenv("CLAUDE_ORCH_TERMINAL", "tmux")
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "claude-orch")
    terminal._DRIVER = None

    drv = terminal.get_driver()
    drv.send_text("%9", "resume your task")  # swallows errors internally; must run dummies only

    assert any("load-buffer" in a for a in no_live_tmux.run)
    assert any("send-keys" in a and a[-1] == "Enter" for a in no_live_tmux.run)
    assert all(a[0] == "tmux" for a in no_live_tmux.run)


def test_tmux_async_ls_is_absorbed(no_live_tmux, monkeypatch):
    """The async create_subprocess_exec entry point absorbs tmux too: patching
    subprocess.run alone would leave TmuxDriver's async ls/spawn/pane_exists
    uncovered. Even pointed at the live socket, the call must be intercepted (no
    real pane discovered)."""
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "claude-orch")  # the live socket
    terminal._DRIVER = None

    drv = terminal.get_driver()
    assert isinstance(drv, terminal.TmuxDriver)

    assert asyncio.run(drv.find_group_pane()) is None  # absorbed -> no live pane
    assert any(a[0] == "tmux" for a in no_live_tmux.exec)


@pytest.mark.real_tmux
def test_real_tmux_guard_blocks_live_socket(monkeypatch):
    """A real_tmux test that lands on the live socket is hard-failed before exec."""
    monkeypatch.setenv("CLAUDE_ORCH_TERMINAL", "tmux")
    monkeypatch.delenv("CLAUDE_ORCH_TMUX_SOCKET", raising=False)  # default -> dockwright
    terminal._DRIVER = None

    drv = terminal.get_driver()
    with pytest.raises(AssertionError, match="LIVE socket"):
        asyncio.run(drv.spawn(cwd="/tmp", title="x", argv=["true"]))


@pytest.mark.real_tmux
def test_real_tmux_guard_blocks_mgr_session(monkeypatch):
    """A real_tmux test targeting session mgr is hard-failed, even off-socket."""
    monkeypatch.setenv("CLAUDE_ORCH_TERMINAL", "tmux")
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "wt-iso-safe")  # NOT the live socket
    terminal._DRIVER = None

    drv = terminal.get_driver()
    with pytest.raises(AssertionError, match="manager session"):
        asyncio.run(drv.spawn(cwd="/tmp", title="x", argv=["true"],
                              route_to_manager_session=True))


@pytest.mark.real_tmux
def test_real_tmux_fixture_drives_throwaway_socket(real_tmux):
    """The real_tmux fixture supplies an isolated throwaway server. Driving the
    SAME spawn path against it really creates a window (proving the path is live,
    i.e. without the absorber it would have shelled real tmux) — but never the
    live socket. Counterpart to test_tmux_spawn_is_absorbed_not_executed."""
    sock = real_tmux
    assert sock != "claude-orch" and sock.startswith("wt-iso-")

    drv = terminal.get_driver()
    assert isinstance(drv, terminal.TmuxDriver)
    assert drv.socket() == sock

    pane = asyncio.run(drv.spawn(cwd="/tmp", title="iso", argv=["sleep", "600"],
                                 route_to_workers_window=True))
    assert pane and pane != _SENTINEL_PANE  # a REAL pane id from the throwaway server
    assert asyncio.run(drv.pane_exists(pane)) is True
