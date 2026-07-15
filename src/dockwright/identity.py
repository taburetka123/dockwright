"""Resolve the calling subprocess's owning manager.

Used by the `monitor` CLI to figure out which manager's events to watch,
WITHOUT having to substitute the manager's funny-name into the Monitor
command at arm time.

Resolution order:
  1. Current pane id (TMUX_PANE): match
     against active manager records' window_id (state.window_id_of handles the
     iterm_sid legacy key).
  2. PPID-walk: walk up `os.getppid()` chain up to MAX_HOPS times, matching
     each pid against active manager records' pid.
  3. Fail loudly to stderr + exit 2 with an actionable message.

Each fallback step logs to stderr so the failure mode is observable.
"""
from __future__ import annotations

import os
import subprocess
import sys
from . import paths, state
from .terminal import get_driver

MAX_HOPS = 8


def _list_manager_records() -> list[dict]:
    if not paths.ACTIVE.is_dir():
        return []
    # Nested manager-agent records (claude -p children of a manager) are
    # ghosts, never THE manager — resolving one would scope monitors to it.
    return [r for r in state.list_json_in(paths.ACTIVE)
            if r.get("agent") == "manager" and not r.get("nested")]


def _resolve_via_pane_id(records: list[dict]) -> dict | None:
    wid = get_driver().current_pane_id() or ""
    if not wid:
        print("identity: TMUX_PANE unset; trying PPID-walk", file=sys.stderr)
        return None
    matches = [r for r in records if state.window_id_of(r) == wid]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"identity: TMUX_PANE={wid!r} matches {len(matches)} "
              f"manager records; ambiguous, falling through", file=sys.stderr)
    else:
        print(f"identity: TMUX_PANE={wid!r} matches no manager record; "
              f"trying PPID-walk", file=sys.stderr)
    return None


def _ppid_of(pid: int) -> int | None:
    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if result.returncode != 0:
            return None
        token = result.stdout.strip()
        if not token:
            return None
        return int(token)
    except (subprocess.SubprocessError, ValueError):
        return None


def _resolve_via_ppid_walk(records: list[dict]) -> dict | None:
    pid_to_record = {int(r["pid"]): r for r in records if r.get("pid")}
    cursor = os.getppid()
    for _ in range(MAX_HOPS):
        if cursor in pid_to_record:
            return pid_to_record[cursor]
        next_pid = _ppid_of(cursor)
        if next_pid is None or next_pid <= 1 or next_pid == cursor:
            break
        cursor = next_pid
    print(f"identity: PPID-walk from pid {os.getppid()} matched no manager record",
          file=sys.stderr)
    return None


def resolve_manager() -> dict:
    """Return {'name': ..., 'sid': ...} for the calling subprocess's manager.

    Exits 2 with an actionable stderr message on failure.
    """
    records = _list_manager_records()
    for resolver in (_resolve_via_pane_id, _resolve_via_ppid_walk):
        match = resolver(records)
        if match is not None:
            return {"name": match["name"], "sid": match["claude_sid"]}
    names = sorted(r.get("name", "?") for r in records)
    print(
        f"dockwright monitor: cannot resolve owning manager. "
        f"Tried TMUX_PANE={(get_driver().current_pane_id() or '')!r}, "
        f"PPID-walk from pid {os.getppid()}. "
        f"Active manager records: {names}. Exiting.",
        file=sys.stderr,
    )
    sys.exit(2)
