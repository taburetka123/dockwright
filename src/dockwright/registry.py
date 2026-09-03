from . import paths, state
from .state import _pid_alive
from .spend_ledger import append_drop_event


_MAX_OS_PID = 0x7FFFFFFF


def _live_pane_ids() -> "set | None":
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
