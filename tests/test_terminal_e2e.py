import asyncio
import os
import shutil
import subprocess
import time as _t
import pytest
from dockwright import terminal

pytestmark = [
    pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed"),
    pytest.mark.real_tmux,
]


@pytest.fixture
def tmux_server(real_tmux):
    sock = real_tmux
    subprocess.run(["tmux", "-L", sock, "new-session", "-d", "-s", "claude-workers",
                    "--", "sleep", "600"], check=True)
    pane = subprocess.run(["tmux", "-L", sock, "list-panes", "-t", "claude-workers",
                           "-F", "#{pane_id}"], capture_output=True, text=True).stdout.strip()
    return sock, pane


def test_e2e_send_text_then_capture_shows_text(tmux_server):
    sock, pane = tmux_server
    drv = terminal.get_driver()
    assert isinstance(drv, terminal.TmuxDriver)
    drv.send_text(pane, "echo NUDGED_MARKER", submit=True)
    deadline = _t.time() + 5
    seen = ""
    while _t.time() < deadline:
        seen = drv.capture_screen(pane) or ""
        if "NUDGED_MARKER" in seen:
            break
        _t.sleep(0.2)
    assert "NUDGED_MARKER" in seen


def test_e2e_close_removes_pane(tmux_server):
    sock, pane = tmux_server
    drv = terminal.get_driver()
    assert asyncio.run(drv.pane_exists(pane)) is True
    drv.close(pane)
    deadline = _t.time() + 5
    while _t.time() < deadline and asyncio.run(drv.pane_exists(pane)):
        _t.sleep(0.2)
    assert asyncio.run(drv.pane_exists(pane)) is False


def _panes_in_session(sock, session):
    out = subprocess.run(["tmux", "-L", sock, "list-panes", "-s", "-t", session,
                          "-F", "#{pane_id}"], capture_output=True, text=True)
    return {l.strip() for l in out.stdout.splitlines() if l.strip()}


def test_e2e_manager_lifecycle_recreate_and_recovery(real_tmux):
    sock = real_tmux
    drv = terminal.get_driver()
    assert isinstance(drv, terminal.TmuxDriver)
    cwd = os.getcwd()

    pane1 = asyncio.run(drv.spawn(cwd=cwd, title="w1", argv=["sleep", "600"],
                                  route_to_workers_window=True))
    assert pane1
    assert asyncio.run(drv._has_session("claude-workers")) is True
    assert pane1 in _panes_in_session(sock, "claude-workers")

    pane2 = asyncio.run(drv.spawn(cwd=cwd, title="w2", argv=["sleep", "600"],
                                  route_to_workers_window=True))
    assert pane2 and pane2 != pane1
    panes = _panes_in_session(sock, "claude-workers")
    assert pane1 in panes and pane2 in panes

    drv.close(pane1)
    deadline = _t.time() + 5
    while _t.time() < deadline and asyncio.run(drv.pane_exists(pane1)):
        _t.sleep(0.2)
    assert asyncio.run(drv.pane_exists(pane1)) is False
    assert asyncio.run(drv.pane_exists(pane2)) is True

    pane3 = asyncio.run(drv.spawn(cwd=cwd, title="w-recovery", argv=["sleep", "600"],
                                  route_to_workers_window=True))
    assert pane3 and pane3 in _panes_in_session(sock, "claude-workers")

    drv.send_text(pane3, "echo RECOVERY_MARKER", submit=True)
    deadline = _t.time() + 5
    seen = ""
    while _t.time() < deadline:
        seen = drv.capture_screen(pane3) or ""
        if "RECOVERY_MARKER" in seen:
            break
        _t.sleep(0.2)
    assert "RECOVERY_MARKER" in seen
