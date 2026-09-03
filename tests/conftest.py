import asyncio
import os
import shutil
import subprocess
import types
from pathlib import Path

import pytest

from dockwright import paths, terminal

_REAL_SUBPROCESS_RUN = subprocess.run

_LIVE_SPEND_LEDGER = paths.SPEND_LEDGER

_LIVE_TMUX_SOCKETS = ("dockwright", "claude-orch")
_ABSORBED_TMUX_PANE = "%no-live-tmux"


def _tmux_socket_path(sock: str) -> Path:
    tmpdir = os.environ.get("TMUX_TMPDIR") or "/tmp"
    return Path(tmpdir) / f"tmux-{os.getuid()}" / sock


def _teardown_ephemeral_tmux(sock: str) -> None:
    subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)
    _tmux_socket_path(sock).unlink(missing_ok=True)


def _leaked_test_sockets() -> list[Path]:
    sock_dir = _tmux_socket_path("_").parent
    if not sock_dir.is_dir():
        return []
    pid = os.getpid()
    patterns = (f"wt-iso-{pid}-*", f"dockwright-e2e-{pid}")
    return sorted(p for pat in patterns for p in sock_dir.glob(pat))


@pytest.fixture(autouse=True)
def isolate_terminal_backend(monkeypatch):
    monkeypatch.setattr(paths, "TMUX_CONF", Path("/nonexistent/__no_tmux_conf__"))
    monkeypatch.setattr(paths, "TMUX_CONF_LEGACY", Path("/nonexistent/__no_tmux_conf_legacy__"))
    terminal._DRIVER = None


class _FakeProc:

    def __init__(self, stdout: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self, input=None):
        return (self._stdout, b"")

    async def wait(self):
        return self.returncode


def _assert_throwaway_tmux(argv) -> None:
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
    return _ABSORBED_TMUX_PANE.encode() if ("new-window" in argv or "new-session" in argv) else b""


@pytest.fixture(autouse=True)
def no_live_tmux(monkeypatch, request):
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


_TMUX_SHIM = """#!/bin/bash
REAL_TMUX="__REAL_TMUX__"
if [ "${1:-}" = "-V" ]; then echo "tmux-shim (dockwright test guard)"; exit 0; fi
sock=""
targets_mgr=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-L" ]; then sock="$a"; fi
  if [ "$prev" = "-t" ] || [ "$prev" = "-s" ]; then
    case "${a%%:*}" in mgr) targets_mgr=1 ;; esac
  fi
  prev="$a"
done
if [ -z "$sock" ] && [ -n "${TMUX:-}" ]; then
  tmux_path="${TMUX%%,*}"
  sock="${tmux_path##*/}"
fi
case "$sock" in
  wt-iso-*|dockwright-e2e-*)
    if [ -n "$REAL_TMUX" ]; then exec "$REAL_TMUX" "$@"; fi
    echo "BLOCKED: no real tmux on this machine for throwaway socket $sock" >&2
    exit 97 ;;
esac
if [ -n "$targets_mgr" ]; then
  echo "BLOCKED: test subprocess targeted the manager session 'mgr' on non-throwaway socket '${sock:-<default=live>}' (argv: $*)" >&2
  exit 97
fi
echo "BLOCKED: test subprocess tried to reach tmux socket '${sock:-<default=live>}' (argv: $*)" >&2
exit 97
"""

_CLI_AGENT_SHIM = """#!/bin/bash
echo "BLOCKED: test subprocess tried to launch a real CLI agent ($0 $*)" >&2
exit 97
"""


@pytest.fixture(scope="session")
def _cli_shim_dir(tmp_path_factory):
    real_tmux = shutil.which("tmux") or ""
    d = tmp_path_factory.mktemp("cli-shim")
    tmux = d / "tmux"
    tmux.write_text(_TMUX_SHIM.replace("__REAL_TMUX__", real_tmux))
    tmux.chmod(0o755)
    for name in ("claude", "codex"):
        p = d / name
        p.write_text(_CLI_AGENT_SHIM)
        p.chmod(0o755)
    return d


@pytest.fixture(autouse=True)
def no_live_subprocess_cli(_cli_shim_dir, monkeypatch):
    monkeypatch.setenv("PATH", f"{_cli_shim_dir}{os.pathsep}{os.environ['PATH']}")


@pytest.fixture(autouse=True)
def _dockwright_config_hermetic(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "no-dockwright.toml"))
    monkeypatch.delenv("DOCKWRIGHT_MANAGER_RC", raising=False)
    monkeypatch.delenv("DOCKWRIGHT_MANAGER_SKIP_PERMS", raising=False)


@pytest.fixture(autouse=True)
def _no_live_step_summary(monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


@pytest.fixture(autouse=True)
def _no_live_presets(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "PRESETS", tmp_path / "no-presets")


@pytest.fixture(autouse=True)
def _no_live_account_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "ACCOUNT_REGISTRY", tmp_path / "account-registry.json")


@pytest.fixture(autouse=True)
def _no_live_account_state(monkeypatch, tmp_path, request):
    if request.module.__name__.endswith("test_paths"):
        yield
        return
    monkeypatch.setattr(paths, "ACCOUNT_USAGE", tmp_path / "no-live-usage")
    monkeypatch.setattr(paths, "ACCOUNT_ACTIVE", tmp_path / "no-live-account-active")
    monkeypatch.setattr(paths, "ACCOUNT_STATE", tmp_path / "no-live-account-state.json")
    monkeypatch.setattr(paths, "SPAWN_COUNTER", tmp_path / "no-live-spawn-counter.json")
    yield


@pytest.fixture(autouse=True)
def _no_live_spend_ledger(monkeypatch, tmp_path):
    from dockwright import spend_ledger
    monkeypatch.setattr(paths, "SPEND_LEDGER", tmp_path / "no-live-spend-ledger.jsonl")
    state_dir = tmp_path / "no-live-state"
    state_dir.mkdir()
    monkeypatch.setenv("DOCKWRIGHT_STATE_DIR", str(state_dir))
    violations: list[str] = []
    real_append = spend_ledger._append_line

    def guarded(entry):
        target = paths.SPEND_LEDGER
        try:
            is_live = target.resolve().is_relative_to(_LIVE_SPEND_LEDGER.parent.resolve())
        except OSError:
            is_live = True
        if is_live:
            violations.append(f"live-ledger write attempted: {entry!r}")
            return
        real_append(entry)

    monkeypatch.setattr(spend_ledger, "_append_line", guarded)
    yield violations
    assert not violations, (
        "test attempted to write the LIVE spend ledger: " + "; ".join(violations))


@pytest.fixture(autouse=True)
def _fast_spawn_registration(monkeypatch):
    from dockwright import mcp_server
    monkeypatch.setattr(mcp_server, "_DEFAULT_REGISTRATION_TIMEOUT_SEC", 0.05, raising=True)
    monkeypatch.setattr(mcp_server, "_DEFAULT_REGISTRATION_POLL_SEC", 0.01, raising=True)


@pytest.fixture(autouse=True)
def _no_real_preflight_cleanup(monkeypatch):
    from dockwright import mcp_server
    monkeypatch.setattr(mcp_server, "_run_preflight_cleanup", lambda: "", raising=True)


@pytest.fixture
def real_tmux(monkeypatch, request, tmp_path):
    if request.node.get_closest_marker("real_tmux") is None:
        pytest.fail("real_tmux fixture requires @pytest.mark.real_tmux on the test")
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed")
    sock = f"wt-iso-{os.getpid()}-{tmp_path.name}"
    request.addfinalizer(lambda: _teardown_ephemeral_tmux(sock))
    monkeypatch.setenv("CLAUDE_ORCH_TERMINAL", "tmux")
    monkeypatch.delenv("DOCKWRIGHT_TMUX_SOCKET", raising=False)
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", sock)
    terminal._DRIVER = None
    return sock


@pytest.fixture(autouse=True)
def _isolate_gardener_ledger(monkeypatch, tmp_path):
    import sys as _sys
    root = tmp_path / "gardener-state"
    monkeypatch.setenv("DOCKWRIGHT_GARDENER_DIR", str(root))
    derived = {
        "GARDENER_DIR": root,
        "PENDING_DIR": root / "proposals" / "pending",
        "ACCEPTED_DIR": root / "proposals" / "accepted",
        "DECLINED_DIR": root / "proposals" / "declined",
        "REJECTED_DIR": root / "proposals" / "rejected",
        "CHECKS_DIR": root / "checks",
        "LEDGER_PATH": root / "ledger.jsonl",
    }
    for name, module in list(_sys.modules.items()):
        if name.startswith("gardener_postrun") and hasattr(module, "GARDENER_DIR"):
            for attr, val in derived.items():
                monkeypatch.setattr(module, attr, val, raising=False)


@pytest.fixture(autouse=True)
def _no_host_claude_json_writes(monkeypatch, tmp_path):
    from dockwright import trust
    monkeypatch.setattr(trust, "_default_config_json",
                        lambda: tmp_path / "host-claude-config.json")


@pytest.fixture(autouse=True, scope="session")
def no_leaked_test_sockets():
    yield
    leaked = _leaked_test_sockets()
    if leaked:
        pytest.fail("tmux test sockets leaked (kill+unlink teardown missed): "
                    + ", ".join(str(p) for p in leaked), pytrace=False)
