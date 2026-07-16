"""Active-session records + pending-questions registry.

Shared by the every-session hook path (hooks.py) and the MCP server. Must stay
free of FastMCP (and any other heavyweight import): a bad merge to
mcp_server.py must not be able to break SessionStart registration — hook
stderr is swallowed, so that failure surfaces only as fleet routing gaps.

(Session-records registry, not to be confused with deploy/
loops-registry.md — the background-loops census.)
"""
from . import paths, state
from .state import _pid_alive
from .spend_ledger import append_drop_event


# os.kill raises OverflowError (not OSError) for pids above the C int range; the
# reaper runs on become_manager / spawn_worker / register_self paths, so a poisoned
# record must be skipped, not allowed to traceback fleet-wide.
_MAX_OS_PID = 0x7FFFFFFF


def _live_pane_ids() -> "set | None":
    """Pane ids currently alive on the orchestrator tmux server, or None when
    tmux cannot answer (error/timeout — distinct from an empty/absent server,
    which is the empty set). One list-panes subprocess per call; the prune
    fetches lazily, only when a dead-pid record still holds a window claim."""
    try:
        from .terminal import get_driver
        os_windows = get_driver().ls()
    except Exception:
        return None
    if os_windows is None:
        return None
    ids: set = set()
    for osw in os_windows:
        if not isinstance(osw, dict):
            continue
        tabs = osw.get("tabs")
        if not isinstance(tabs, list):
            continue
        for tab in tabs:
            windows = tab.get("windows") if isinstance(tab, dict) else None
            if not isinstance(windows, list):
                continue
            for win in windows:
                if isinstance(win, dict) and win.get("id") is not None:
                    ids.add(str(win["id"]))
    return ids


def _prune_stale_active_records() -> None:
    """Drop active/<sid>.json records whose pid is dead AND whose terminal
    pane is gone.

    A tab closed via SIGHUP kills the process before SessionEnd hooks can run,
    leaving an orphan record that blocks future name-collision checks.

    The pane gate exists because a dead recorded pid does NOT prove the
    session dead: Linux hook runners hand SessionStart a short-lived
    intermediate $PPID (the VM-E2E ghost-worker incident), and pids recycle.
    A record whose pane is still alive is kept; when tmux cannot answer, the
    window-bearing candidates are kept too — deferral is free, deletion is
    not. Windowless records keep the pid-only bar. The pane set is fetched
    lazily (zero subprocess calls in the common all-pids-alive pass — this
    runs on MCP request paths AND on every SessionStart hook via
    _resolve_unique_name, under the hook's 5s budget). Deletion still
    requires an in-OS-range positive int pid that os.kill(pid, 0) says is
    dead; anything else odd is left for preflight to surface.
    """
    if not paths.ACTIVE.is_dir():
        return
    live_panes: "set | None" = None
    panes_fetched = False
    for record_path in paths.ACTIVE.iterdir():
        if record_path.suffix != ".json":
            continue
        record = state.read_json(record_path)
        if record is None:
            continue
        pid = record.get("pid")
        if not isinstance(pid, int) or pid <= 0 or pid > _MAX_OS_PID or _pid_alive(pid):
            continue
        window_id = state.window_id_of(record)
        if window_id:
            if not panes_fetched:
                live_panes = _live_pane_ids()
                panes_fetched = True
            if live_panes is None or str(window_id) in live_panes:
                continue
        sid = record.get("claude_sid")
        append_drop_event(record, "prune")
        record_path.unlink(missing_ok=True)
        if sid:
            _drop_questions_for_worker(sid)

def _resolve_unique_name(base: str, excluding_sid: str | None = None) -> str:
    """Return base, or base-2, base-3... until no active record has the name.

    `funny_name`s count as taken too: routing stays keyed on `name`, but a
    caller-passed name matching another session's display handle would show
    two sessions under one label."""
    _prune_stale_active_records()
    existing_names = {
        record.get(key)
        for record in state.list_json_in(paths.ACTIVE)
        for key in ("name", "funny_name")
        if record.get("claude_sid") != excluding_sid
    }
    if base not in existing_names:
        return base
    suffix = 2
    while f"{base}-{suffix}" in existing_names:
        suffix += 1
    return f"{base}-{suffix}"

def _drop_questions_for_worker(worker_sid: str) -> int:
    """Remove pending questions belonging to a worker. Returns count removed."""
    removed = 0
    for q_path in _question_paths():
        record = state.read_json(q_path)
        if record is None:
            continue
        if record.get("worker_sid") == worker_sid:
            q_path.unlink(missing_ok=True)
            removed += 1
    return removed


def _question_paths() -> list:
    if not paths.QUESTIONS.is_dir():
        return []
    return sorted(paths.QUESTIONS.rglob("*.json"))
