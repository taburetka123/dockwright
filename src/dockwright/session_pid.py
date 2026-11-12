from __future__ import annotations

import os
import subprocess

SESSION_BINARIES = ("claude", "codex")
MAX_HOPS = 15


def _looks_like_session(command: str) -> bool:
    tokens = command.split()
    return bool(tokens) and os.path.basename(tokens[0]) in SESSION_BINARIES


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
    except Exception:
        return None


def _iter_ancestors(start_pid: int, max_hops: int = MAX_HOPS):
    seen: set = set()
    cursor = start_pid
    for _ in range(max_hops):
        parent = _ppid_of(cursor)
        if parent is None or parent <= 1 or parent == cursor or parent in seen:
            break
        yield parent
        seen.add(parent)
        cursor = parent


def pid_command(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-ww", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except Exception:
        return ""


def _pid_looks_like_session(pid: int) -> bool:
    return _looks_like_session(pid_command(pid))


def resolve_cli_pid(start: int | None = None) -> int | None:
    if start is not None:
        starts: tuple[int, ...] = (start,)
    else:
        raw = os.environ.get("CLAUDE_PARENT_PID", "")
        captured = int(raw) if raw.isdigit() else os.getppid()
        starts = tuple(dict.fromkeys((captured, os.getppid())))
    for candidate in starts:
        if _pid_looks_like_session(candidate):
            return candidate
        for pid in _iter_ancestors(candidate):
            if _pid_looks_like_session(pid):
                return pid
    return None
