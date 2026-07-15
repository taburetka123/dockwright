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


def _prune_stale_active_records() -> None:
    """Drop active/<sid>.json records whose pid is no longer alive.

    A tab closed via SIGHUP kills the process before SessionEnd hooks can run,
    leaving an orphan record that blocks future name-collision checks.

    Deliberately weaker than preflight_cleanup._prune_active: no per-record
    `ps` command-line check here (subprocess latency on MCP request paths).
    The shared invariant both keep: deletion requires an in-OS-range positive
    int pid that os.kill(pid, 0) says is dead — a non-positive or out-of-range
    pid can't prove the session dead, so such records are left for preflight
    to surface as odd-looking.
    """
    if not paths.ACTIVE.is_dir():
        return
    for record_path in paths.ACTIVE.iterdir():
        if record_path.suffix != ".json":
            continue
        record = state.read_json(record_path)
        if record is None:
            continue
        pid = record.get("pid")
        if not isinstance(pid, int) or pid <= 0 or pid > _MAX_OS_PID or _pid_alive(pid):
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
