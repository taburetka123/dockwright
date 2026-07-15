"""Shared test guard: no test may touch the live orchestrator tmux server.

The suite exercises hook/MCP code whose production behavior is to shell out to
`tmux -L dockwright` (spawn/send/capture/kill/rename/ls) — or the legacy
`-L claude-orch` the live fleet still rides until the gated migration. The live
server hosts the manager (`mgr`) session and every worker pane, so an unmocked code path can
spawn into, repaint, or kill a real session. The worst case is the manager
recovery spawn (stale_monitor._launch_recovery_manager -> TmuxDriver.spawn),
which runs `tmux -L claude-orch new-window … claude /manager-takeover-recovery`
through asyncio.create_subprocess_exec — a test that resolved the TmuxDriver once
spawned 50+ real recovery sessions into the live `mgr` session.

The guard is the autouse `no_live_tmux` fixture: it absorbs BOTH tmux entry
points (subprocess.run for sync ops, asyncio.create_subprocess_exec for async
ops INCLUDING spawn), returning dummy success instead of executing the real
binary. Tests that genuinely need real tmux mark themselves
@pytest.mark.real_tmux and use the `real_tmux` fixture (a throwaway per-pid
socket); for those `_assert_throwaway_tmux` HARD-FAILS any invocation targeting
a live socket (`-L dockwright` or the legacy `-L claude-orch`) or the `mgr` session.
"""
import asyncio
import os
import shutil
import subprocess
import types
from pathlib import Path

import pytest

from dockwright import paths, terminal

_REAL_SUBPROCESS_RUN = subprocess.run

# TmuxDriver.socket() defaults to "dockwright" when DOCKWRIGHT_TMUX_SOCKET and
# CLAUDE_ORCH_TMUX_SOCKET are both unset — the LIVE orchestrator server the
# manager/workers run on. The live fleet still rides the legacy "claude-orch"
# socket until the gated migration, so BOTH names are guarded — no test may ever
# touch either.
_LIVE_TMUX_SOCKETS = ("dockwright", "claude-orch")
# Sentinel pane id the tmux absorber returns for spawn calls — recognizably fake,
# so a test can prove the spawn was intercepted rather than really executed.
_ABSORBED_TMUX_PANE = "%no-live-tmux"


def _tmux_socket_path(sock: str) -> Path:
    """Where tmux -L <sock> puts its socket: $TMUX_TMPDIR (or /tmp) /tmux-<uid>/<sock>."""
    tmpdir = os.environ.get("TMUX_TMPDIR") or "/tmp"
    return Path(tmpdir) / f"tmux-{os.getuid()}" / sock


def _teardown_ephemeral_tmux(sock: str) -> None:
    """Kill a throwaway test server AND remove its socket file. tmux (3.7b) does
    not unlink the socket on kill-server, so without the explicit unlink every
    test run leaks one file per throwaway socket into /tmp/tmux-<uid>."""
    subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)
    _tmux_socket_path(sock).unlink(missing_ok=True)


# Socket-name patterns owned by this test suite, pid-scoped so parallel runs
# and the live fleet's sockets never match. dockwright-e2e-<pid> is the retired
# test_terminal_e2e naming, kept in the net in case it is ever reintroduced.
def _leaked_test_sockets() -> list[Path]:
    sock_dir = _tmux_socket_path("_").parent
    if not sock_dir.is_dir():
        return []
    pid = os.getpid()
    patterns = (f"wt-iso-{pid}-*", f"dockwright-e2e-{pid}")
    return sorted(p for pat in patterns for p in sock_dir.glob(pat))


@pytest.fixture(autouse=True)
def isolate_terminal_backend(monkeypatch):
    """Stop a real on-disk tmux conf from steering driver behavior in tests.
    Point TMUX_CONF and TMUX_CONF_LEGACY at absent paths and reset the
    process-wide driver cache so each test resolves a fresh TmuxDriver; the
    no_live_tmux absorber intercepts its calls."""
    monkeypatch.setattr(paths, "TMUX_CONF", Path("/nonexistent/__no_tmux_conf__"))
    monkeypatch.setattr(paths, "TMUX_CONF_LEGACY", Path("/nonexistent/__no_tmux_conf_legacy__"))
    terminal._DRIVER = None


class _FakeProc:
    """Stand-in for asyncio.subprocess.Process: only what the driver awaits."""

    def __init__(self, stdout: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self, input=None):
        return (self._stdout, b"")

    async def wait(self):
        return self.returncode


def _assert_throwaway_tmux(argv) -> None:
    """Hard-fail if a real_tmux invocation targets a LIVE socket or the manager
    session. Defense in depth: a real-tmux test must be PHYSICALLY unable to touch
    `-L dockwright` / the legacy `-L claude-orch` or session `mgr`, no matter what
    it forgot to override."""
    toks = [str(a) for a in argv]
    for i, tok in enumerate(toks):
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if tok == "-L" and nxt in _LIVE_TMUX_SOCKETS:
            raise AssertionError(
                f"real_tmux test tried to use a LIVE socket -L {nxt}: {toks}")
        if tok in ("-t", "-s") and nxt.split(":")[0] == terminal.MANAGER_SESSION:
            raise AssertionError(
                f"real_tmux test tried to target the manager session "
                f"'{terminal.MANAGER_SESSION}': {toks}")


def _absorbed_exec_stdout(argv) -> bytes:
    # A spawn (new-window / new-session) must return a pane id; everything else
    # (has-session, list-panes, …) returns empty so find_group_pane/pane_exists
    # read as "nothing live", which is the truth under absorption.
    return _ABSORBED_TMUX_PANE.encode() if ("new-window" in argv or "new-session" in argv) else b""


@pytest.fixture(autouse=True)
def no_live_tmux(monkeypatch, request):
    """Absorb tmux subprocess invocations so NO test shells the real binary.

    The detonation that motivated this: the manager recovery spawn
    (stale_monitor._launch_recovery_manager -> TmuxDriver.spawn) runs
    `tmux -L claude-orch new-window … claude /manager-takeover-recovery` through
    asyncio.create_subprocess_exec. A test that resolved the TmuxDriver therefore
    spawned 50+ real recovery sessions into the live `mgr` session, burning the
    account and polluting the working terminal.

    Both entry points are patched as MODULE ATTRIBUTES — terminal.py calls them as
    such by design (see its module docstring) so the guards intercept them:
      * subprocess.run                  -> sync tmux ops (send/capture/kill/rename/ls)
      * asyncio.create_subprocess_exec  -> async tmux ops INCLUDING spawn (the bomb)

    Non-tmux argv falls through to the REAL binary (_REAL_SUBPROCESS_RUN /
    real_exec), untouched.

    Tests that genuinely need real tmux mark themselves @pytest.mark.real_tmux
    (see the real_tmux fixture): for those we DELEGATE to the real binary but
    HARD-FAIL via _assert_throwaway_tmux any invocation that targets the live
    socket or the mgr session.

    osascript argv is absorbed too (into absorbed.osascript, real_tmux
    included) so no in-process code path can fire a real desktop
    notification — see test_no_desktop_notifications.py."""
    absorbed = types.SimpleNamespace(run=[], exec=[], osascript=[])
    is_real = request.node.get_closest_marker("real_tmux") is not None
    real_exec = asyncio.create_subprocess_exec

    def guarded_run(args, *pargs, **kwargs):
        if isinstance(args, (list, tuple)) and args and str(args[0]) == "tmux":
            if is_real:
                _assert_throwaway_tmux(args)
                return _REAL_SUBPROCESS_RUN(args, *pargs, **kwargs)
            absorbed.run.append([str(a) for a in args])
            out = "" if kwargs.get("text") else b""
            return subprocess.CompletedProcess(args, returncode=0, stdout=out, stderr=out)
        # osascript is ALWAYS absorbed (real_tmux included): its only use here
        # is desktop notifications, and no test may ever fire a real one — the
        # 2026-07-03 gardener-gate leak. Subprocess-exec'd scripts bypass this
        # guard entirely; their notify helpers no-op on PYTEST_CURRENT_TEST.
        if (isinstance(args, (list, tuple)) and args
                and str(args[0]).rsplit("/", 1)[-1] == "osascript"):
            absorbed.osascript.append([str(a) for a in args])
            out = "" if kwargs.get("text") else b""
            return subprocess.CompletedProcess(args, returncode=0, stdout=out, stderr=out)
        return _REAL_SUBPROCESS_RUN(args, *pargs, **kwargs)

    async def guarded_exec(program, *args, **kwargs):
        prog = str(program)
        if prog == "tmux":
            argv = [prog, *[str(a) for a in args]]
            if is_real:
                _assert_throwaway_tmux(argv)
                return await real_exec(program, *args, **kwargs)
            absorbed.exec.append(argv)
            return _FakeProc(_absorbed_exec_stdout(argv))
        return await real_exec(program, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", guarded_exec)
    return absorbed


@pytest.fixture(autouse=True)
def _dockwright_config_hermetic(monkeypatch, tmp_path):
    """Every test runs as if no dockwright.toml exists unless it sets
    DOCKWRIGHT_CONFIG itself — an operator's real ~/.claude/dockwright.toml
    must never leak into the suite. An explicit env path that doesn't exist
    is authoritative 'no config' per config.config_path()."""
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "no-dockwright.toml"))


@pytest.fixture(autouse=True)
def _fast_spawn_registration(monkeypatch):
    """Shrink spawn_worker_impl's post-launch registration poll so the ~36 existing
    spawn tests (which mock spawn_worker_tab and never register) don't each wait the
    12 s production default. Detection-net tests pass explicit timeouts and are
    unaffected."""
    from dockwright import mcp_server
    monkeypatch.setattr(mcp_server, "_DEFAULT_REGISTRATION_TIMEOUT_SEC", 0.05, raising=True)
    monkeypatch.setattr(mcp_server, "_DEFAULT_REGISTRATION_POLL_SEC", 0.01, raising=True)


@pytest.fixture
def real_tmux(monkeypatch, request, tmp_path):
    """Throwaway tmux server for lifecycle/E2E tests. Per-pid/per-tmpdir socket,
    NEVER a live socket; killed AND socket file removed on teardown. The test
    MUST also be marked @pytest.mark.real_tmux so no_live_tmux delegates to the
    real binary while the live-socket / mgr guard stays armed."""
    if request.node.get_closest_marker("real_tmux") is None:
        pytest.fail("real_tmux fixture requires @pytest.mark.real_tmux on the test")
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed")
    sock = f"wt-iso-{os.getpid()}-{tmp_path.name}"
    # Finalizer BEFORE any env pinning or tmux action: it must run even when a
    # consumer fixture's setup dies halfway (the old `yield`-tail teardown did
    # not, orphaning both the server and its socket file).
    request.addfinalizer(lambda: _teardown_ephemeral_tmux(sock))
    monkeypatch.setenv("CLAUDE_ORCH_TERMINAL", "tmux")
    # Drop the higher-precedence primary so the pinned CLAUDE_ORCH_TMUX_SOCKET
    # deterministically wins socket() — an ambient DOCKWRIGHT_TMUX_SOCKET must
    # never steer a REAL tmux invocation off the throwaway socket.
    monkeypatch.delenv("DOCKWRIGHT_TMUX_SOCKET", raising=False)
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", sock)
    terminal._DRIVER = None
    return sock


@pytest.fixture(autouse=True, scope="session")
def no_leaked_test_sockets():
    """Standing regression net: fail the run if any test-owned tmux socket file
    survives session teardown. Pid-scoped (see _leaked_test_sockets), so a live
    fleet or parallel pytest processes on the same box never trip it. Blind
    spots (accepted): a run killed before session teardown leaks unattributed
    files; the net sees socket FILES only, not orphan server processes. A trip
    surfaces as 'ERROR at teardown' of the last item, not a failed test."""
    yield
    leaked = _leaked_test_sockets()
    if leaked:
        pytest.fail("tmux test sockets leaked (kill+unlink teardown missed): "
                    + ", ".join(str(p) for p in leaked), pytrace=False)
