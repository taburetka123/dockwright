import asyncio
import os
import shutil
import subprocess
import time as _t
import pytest
from dockwright import terminal

# real_tmux: drives a real server, so no_live_tmux must delegate to the real
# binary (with the live-socket / mgr guard still armed) instead of absorbing.
pytestmark = [
    pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed"),
    pytest.mark.real_tmux,
]


@pytest.fixture
def tmux_server(real_tmux):
    # real_tmux owns env pinning, driver reset, and kill+unlink teardown — its
    # finalizer is already registered here, so even a check=True failure below
    # cannot orphan the server or its socket file.
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
    # -s lists panes across ALL windows of the session; the recreate path uses
    # new-window, so each spawn lands in a different window and a plain list-panes
    # (current window only) would miss the recreated panes.
    out = subprocess.run(["tmux", "-L", sock, "list-panes", "-s", "-t", session,
                          "-F", "#{pane_id}"], capture_output=True, text=True)
    return {l.strip() for l in out.stdout.splitlines() if l.strip()}


def test_e2e_manager_lifecycle_recreate_and_recovery(real_tmux):
    """Drive the manager-lifecycle spawn/recreate/close/recovery/inject machinery
    against a REAL throwaway tmux server (#126's real_tmux fixture). Uses
    route_to_workers_window (claude-workers session) — NOT route_to_manager_session
    (mgr) — because the no_live_tmux guard hard-fails any real_tmux invocation
    targeting session `mgr`. This exercises the SAME new-session/new-window/
    find_group_pane/close/pane_exists/send_text/capture_screen code the manager
    recreate/recovery path uses; the route_to_manager_session->mgr argv shape is
    covered in absorbed mode by test_no_live_tmux.py::test_tmux_spawn_is_absorbed_not_executed.
    """
    sock = real_tmux
    drv = terminal.get_driver()
    assert isinstance(drv, terminal.TmuxDriver)
    cwd = os.getcwd()

    # 1. First spawn: no claude-workers session yet -> new-session
    pane1 = asyncio.run(drv.spawn(cwd=cwd, title="w1", argv=["sleep", "600"],
                                  route_to_workers_window=True))
    assert pane1
    assert asyncio.run(drv._has_session("claude-workers")) is True  # has-session primitive vs real tmux (manager branch uses this)
    assert pane1 in _panes_in_session(sock, "claude-workers")

    # 2. Recreate: session exists -> new-window
    pane2 = asyncio.run(drv.spawn(cwd=cwd, title="w2", argv=["sleep", "600"],
                                  route_to_workers_window=True))
    assert pane2 and pane2 != pane1
    panes = _panes_in_session(sock, "claude-workers")
    assert pane1 in panes and pane2 in panes

    # 3. Predecessor takeover: close pane1 (become_manager_with_takeover SIGTERMs the predecessor)
    drv.close(pane1)
    deadline = _t.time() + 5
    while _t.time() < deadline and asyncio.run(drv.pane_exists(pane1)):
        _t.sleep(0.2)
    assert asyncio.run(drv.pane_exists(pane1)) is False
    assert asyncio.run(drv.pane_exists(pane2)) is True

    # 4. Recovery: spawn a fresh window into the existing session
    pane3 = asyncio.run(drv.spawn(cwd=cwd, title="w-recovery", argv=["sleep", "600"],
                                  route_to_workers_window=True))
    assert pane3 and pane3 in _panes_in_session(sock, "claude-workers")

    # 5. Inject into the recovery pane (manager->worker inject / recycle self-type shape)
    drv.send_text(pane3, "echo RECOVERY_MARKER", submit=True)
    deadline = _t.time() + 5
    seen = ""
    while _t.time() < deadline:
        seen = drv.capture_screen(pane3) or ""
        if "RECOVERY_MARKER" in seen:
            break
        _t.sleep(0.2)
    assert "RECOVERY_MARKER" in seen
