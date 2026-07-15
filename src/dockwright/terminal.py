"""Terminal driver abstraction — a tmux driver behind one interface.

All terminal remote-control command construction in the package funnels through
get_driver(), which returns the process-wide TmuxDriver. The TerminalDriver
Protocol is the backend-agnostic seam: callers depend on the Protocol, not on
TmuxDriver directly, so a future frontend can swap in behind get_driver()
without touching call sites.

FastMCP-free by construction (stdlib + paths only): hooks.py imports this on
the every-session hook path, which tests/test_import_graph.py forbids from
importing mcp_server / mcp. Call subprocess.run / asyncio.create_subprocess_exec
as MODULE ATTRIBUTES so the test guards intercept them.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

from . import paths

WORKERS_OS_WINDOW_CLASS = "claude-workers"
MANAGER_SESSION = "mgr"  # tmux session for manager panes (mirrors bootstrap-recreate.sh)


@runtime_checkable
class TerminalDriver(Protocol):
    def socket(self) -> str: ...
    def current_pane_id(self) -> str | None: ...
    async def spawn(self, *, cwd: str, title: str, argv: list[str],
                    route_to_workers_window: bool = False,
                    route_to_manager_session: bool = False,
                    target_window_match: str | None = None) -> str: ...
    async def find_group_pane(self) -> str | None: ...
    async def pane_exists(self, pane: str) -> bool: ...
    def send_text(self, pane: str, text: str, submit: bool = True) -> None: ...
    def send_text_checked(self, pane: str, text: str) -> bool: ...
    def capture_screen(self, pane: str) -> str | None: ...
    def capture_screen_ansi(self, pane: str) -> str | None: ...
    def ls(self) -> list | None: ...
    def ls_with_error(self) -> tuple[list | None, str | None]: ...
    def close(self, pane: str) -> None: ...
    def set_tab_title(self, title: str) -> None: ...
    def set_tab_color(self, active_bg: str, inactive_bg: str) -> None: ...


_LS_FS = "\x1f"  # field separator for list-panes -F (survives | in titles/paths)
_LS_FORMAT = _LS_FS.join([
    "#{session_name}", "#{window_id}", "#{window_name}",
    "#{pane_id}", "#{pane_current_path}", "#{pane_title}", "#{pane_pid}",
])


class TmuxDriver:
    # ---- shared / identity (d) ----
    def socket(self) -> str:
        return (os.environ.get("DOCKWRIGHT_TMUX_SOCKET")
                or os.environ.get("CLAUDE_ORCH_TMUX_SOCKET")  # deprecated, one release
                or "dockwright")

    def current_pane_id(self) -> str | None:
        return os.environ.get("TMUX_PANE")

    def _resolve_conf(self) -> Path | None:
        # `-f <conf>` is read by tmux ONLY at server birth (a no-op on a running
        # server). Prefer the deployed conf; fall back to the pre-rename name for
        # installs whose setup.sh hasn't redeployed since the identity rename
        # (deprecated, one release — retire with CLAUDE_ORCH_TMUX_SOCKET). None
        # when neither exists: tmux's own default config loading beats a hard
        # failure on the every-session hook path.
        for conf in (paths.TMUX_CONF, paths.TMUX_CONF_LEGACY):
            if conf.exists():
                return conf
        return None

    def _tmux_conf_args(self) -> list:
        conf = self._resolve_conf()
        return ["-f", str(conf)] if conf is not None else []

    def _tmux_base(self) -> list:
        return ["tmux", "-L", self.socket(), *self._tmux_conf_args()]

    # ---- spawn (a) ----
    async def find_group_pane(self) -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._tmux_base(), "list-panes", "-t", WORKERS_OS_WINDOW_CLASS,
                "-F", "#{pane_id}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
        except OSError:
            return None
        if proc.returncode != 0:
            return None
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                return line
        return None

    async def pane_exists(self, pane: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._tmux_base(), "list-panes", "-a", "-F", "#{pane_id}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
        except OSError:
            return False
        if proc.returncode != 0:
            return False
        ids = {l.strip() for l in stdout.decode("utf-8", errors="replace").splitlines()}
        return str(pane) in ids

    async def _has_session(self, session: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._tmux_base(), "has-session", "-t", session,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
        except OSError:
            return False
        return proc.returncode == 0

    async def spawn(self, *, cwd: str, title: str, argv: list[str],
                    route_to_workers_window: bool = False,
                    route_to_manager_session: bool = False,
                    target_window_match: str | None = None) -> str:
        if route_to_workers_window:
            match_id = await self.find_group_pane()
            if match_id is None:
                head = ["new-session", "-d", "-s", WORKERS_OS_WINDOW_CLASS]
            else:
                head = ["new-window", "-d", "-t", WORKERS_OS_WINDOW_CLASS]
        elif route_to_manager_session:
            head = (["new-window", "-d", "-t", MANAGER_SESSION]
                    if await self._has_session(MANAGER_SESSION)
                    else ["new-session", "-d", "-s", MANAGER_SESSION])
        elif target_window_match:
            head = ["new-window", "-d", "-t", target_window_match]
        else:
            head = ["new-window", "-d"]
        cmd = [*self._tmux_base(), *head,
               "-n", title, "-c", cwd, "-P", "-F", "#{pane_id}", "--", *argv]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"tmux {head[0]} failed (rc={proc.returncode}): {err}. "
                f"Is tmux installed and able to start a server on -L {self.socket()}?"
            )
        pane = (stdout or b"").decode("utf-8", errors="replace").strip()
        if head[0] == "new-session":
            await self._source_conf_best_effort()
        return pane

    async def _source_conf_best_effort(self) -> None:
        # A server that pre-existed BARE (born without -f — e.g. the manual
        # `tmux -L dockwright new-session` operator lane during a socket
        # cutover) never re-reads the conf on its own; -f on later commands is
        # a no-op. new-session is the birth-adjacent branch, so re-source here.
        # Same resolution as -f (can't diverge); safe to re-apply per the conf's
        # own header. Fire-and-forget: never fails the spawn.
        conf = self._resolve_conf()
        if conf is None:
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "-L", self.socket(), "source-file", str(conf),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
        except Exception:
            pass

    # ---- inject (b) ----
    def _buffer_name(self, pane: str) -> str:
        return "orch_" + str(pane).replace("%", "")

    def _ensure_inject_safe(self) -> None:
        # Pin extended-keys-format=xterm (Claude Code #43169 csi-u newline loss).
        # Fully isolated, fire-and-forget: result ignored, never affects inject return.
        try:
            subprocess.run([*self._tmux_base(), "set-option", "-s",
                            "extended-keys-format", "xterm"],
                           capture_output=True, timeout=2, check=False)
        except Exception:
            pass

    def send_text(self, pane: str, text: str, submit: bool = True) -> None:
        self._ensure_inject_safe()
        buf = self._buffer_name(pane)
        body = text.rstrip("\n")  # #4098: single explicit Enter is the only submit
        try:
            subprocess.run([*self._tmux_base(), "load-buffer", "-b", buf, "-"],
                           input=body.encode("utf-8"), capture_output=True,
                           timeout=2, check=False)
            subprocess.run([*self._tmux_base(), "paste-buffer", "-p", "-d",
                            "-b", buf, "-t", pane],
                           capture_output=True, timeout=2, check=False)
            if submit:
                subprocess.run([*self._tmux_base(), "send-keys", "-t", pane, "Enter"],
                               capture_output=True, timeout=2, check=False)
        except Exception:
            pass

    def send_text_checked(self, pane: str, text: str) -> bool:
        self._ensure_inject_safe()
        buf = self._buffer_name(pane)
        body = text.rstrip("\n")
        try:
            load = subprocess.run([*self._tmux_base(), "load-buffer", "-b", buf, "-"],
                                  input=body, text=True, capture_output=True,
                                  check=False, timeout=2)
            if load.returncode != 0:
                return False
            paste = subprocess.run([*self._tmux_base(), "paste-buffer", "-p", "-d",
                                    "-b", buf, "-t", pane],
                                   capture_output=True, text=True, check=False, timeout=2)
            if paste.returncode != 0:
                return False
            enter = subprocess.run([*self._tmux_base(), "send-keys", "-t", pane, "Enter"],
                                   capture_output=True, text=True, check=False, timeout=2)
            return enter.returncode == 0
        except Exception:
            return False

    def capture_screen(self, pane: str) -> str | None:
        try:
            result = subprocess.run(
                [*self._tmux_base(), "capture-pane", "-p", "-t", pane],
                capture_output=True, timeout=2, check=False)
            if result.returncode != 0:
                return None
            return result.stdout.decode("utf-8", errors="replace")
        except Exception:
            return None

    def capture_screen_ansi(self, pane: str) -> str | None:
        try:
            completed = subprocess.run(
                [*self._tmux_base(), "capture-pane", "-p", "-e", "-t", pane],
                capture_output=True, text=True, check=False, timeout=2)
        except Exception:
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout

    # ---- lifecycle (c) ----
    def _parse_panes(self, text: str) -> list:
        sessions: dict = {}
        order: list = []
        for line in text.splitlines():
            if not line:
                continue
            parts = line.split(_LS_FS)
            # tmux escapes a literal \x1f in titles/paths to the 4-char \037 in -F
            # output, so a legit field never splits a line into >7 parts; the skip
            # only drops truly truncated lines.
            if len(parts) != 7:
                continue
            sess, win_id, win_name, pane_id, cwd, pane_title, pid = parts
            if sess not in sessions:
                sessions[sess] = {}
                order.append(sess)
            tabs = sessions[sess]
            if win_id not in tabs:
                tabs[win_id] = {"title": win_name, "windows": []}
            tabs[win_id]["windows"].append(
                {"id": pane_id, "cwd": cwd, "title": pane_title, "pid": pid})
        return [{"wm_class": s, "tabs": list(sessions[s].values())} for s in order]

    def _run_list_panes(self, *, text: bool, timeout: int):
        return subprocess.run(
            [*self._tmux_base(), "list-panes", "-a", "-F", _LS_FORMAT],
            capture_output=True, text=text, timeout=timeout, check=False)

    def ls(self) -> list | None:
        try:
            result = self._run_list_panes(text=False, timeout=2)
        except Exception:
            return None
        if result.returncode != 0:
            stderr = (result.stderr or b"").decode("utf-8", errors="replace")
            return [] if "no server" in stderr.lower() else None
        return self._parse_panes((result.stdout or b"").decode("utf-8", errors="replace"))

    def ls_with_error(self) -> tuple[list | None, str | None]:
        try:
            proc = self._run_list_panes(text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            return None, f"tmux list-panes failed: {e}"
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            if "no server" in err.lower():
                return [], None
            return None, f"tmux list-panes exited {proc.returncode}: {err or 'no stderr'}"
        return self._parse_panes(proc.stdout), None

    def close(self, pane: str) -> None:
        if not pane:
            return
        try:
            subprocess.run([*self._tmux_base(), "kill-pane", "-t", pane],
                           capture_output=True, timeout=2, check=False)
        except Exception:
            pass

    def set_tab_title(self, title: str) -> None:
        pane = self.current_pane_id()
        if not pane:
            return
        try:
            subprocess.run([*self._tmux_base(), "rename-window", "-t", pane, title],
                           capture_output=True, timeout=2, check=False)
        except Exception:
            pass

    def set_tab_color(self, active_bg: str, inactive_bg: str) -> None:
        # Color THIS window's status-line entry per state by styling its
        # status-line entry: active_bg -> the entry when this is the current
        # window, inactive_bg -> when it isn't. tmux has no per-PANE background,
        # but window-status-current-style / window-status-style ARE per-window
        # options, so the orchestrator hooks' existing set_tab_color calls
        # (idle/busy/question/manager) drive tmux coloring with no hook change.
        # Best-effort, fire-and-forget: a bad value or down server is swallowed.
        pane = self.current_pane_id()
        if not pane:
            return
        fg = "#ffffff"  # one readable fg across every state bg
        for opt, bg in (("window-status-current-style", active_bg),
                        ("window-status-style", inactive_bg)):
            try:
                subprocess.run([*self._tmux_base(), "set-window-option", "-t", pane,
                                opt, f"bg={bg},fg={fg}"],
                               capture_output=True, timeout=2, check=False)
            except Exception:
                pass


_DRIVER: "TerminalDriver | None" = None


def get_driver() -> TerminalDriver:
    """Return the process-wide tmux terminal driver (cached)."""
    global _DRIVER
    if _DRIVER is None:
        _DRIVER = TmuxDriver()
    return _DRIVER
