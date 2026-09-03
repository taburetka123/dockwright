from __future__ import annotations

import os
import select
import sys

from . import paths, state

EXIT_LANE_DEAD = 3

_READER_GONE = select.POLLERR | select.POLLHUP | select.POLLNVAL

LANES: dict[str, int] = {
    "questions": 2,
    "done": 2,
    "turn-ends": 5,
    "stale": 60,
}

HEARTBEAT_STALE_INTERVALS = 3

MAX_SCAN_ERROR_WINDOW_SEC = 60


def max_consecutive_errors(lane: str) -> int:
    interval = LANES.get(lane, 0)
    if interval <= 0:
        return 1
    return max(1, -(-MAX_SCAN_ERROR_WINDOW_SEC // interval))

EXIT_LANE_WEDGED = 4

BACKLOG_CHECKED_LANES = ("questions", "done")

UNRESOLVED_BUCKET = None


class LaneDead(Exception):
    pass


def reader_is_dead(fd: int = 1) -> bool:
    try:
        poller = select.poll()
        poller.register(fd, select.POLLOUT)
        for _fd, revents in poller.poll(0):
            return bool(revents & _READER_GONE)
        return False
    except Exception:
        return False


def preflight(fd: int = 1) -> None:
    if reader_is_dead(fd):
        raise LaneDead("stdout reader is gone (poll reports HUP/ERR/NVAL)")


def emit(line: str) -> None:
    try:
        sys.stdout.write(f"{line}\n")
        sys.stdout.flush()
    except (BrokenPipeError, OSError) as exc:
        raise LaneDead(f"stdout write failed: {exc}") from exc


def detach_stdout() -> None:
    try:
        sys.stdout.flush()
    except Exception:
        pass
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, 1)
        finally:
            os.close(devnull)
    except OSError:
        pass


def error_counter_path(manager_name: str | None, lane: str):
    if manager_name is None:
        return paths.ROOT / f"lane-errors-unresolved-{paths._event_bucket(lane)}.json"
    return heartbeat_path(manager_name, lane)


def heartbeat_path(manager_name: str, lane: str):
    return (paths.LANE_HEALTH / paths._event_bucket(manager_name)
            / f"{paths._event_bucket(lane)}.json")


def write_heartbeat(manager_name: str, lane: str, *, emitted: bool,
                    now: float | None = None) -> None:
    import time
    now = time.time() if now is None else now
    path = heartbeat_path(manager_name, lane)
    prior = state.read_json(path) or {}
    last_emit = now if emitted else prior.get("last_emit")
    try:
        state.write_json_atomic(path, {
            "lane": lane,
            "manager": manager_name,
            "pid": os.getpid(),
            "last_scan": now,
            "last_emit": last_emit if isinstance(last_emit, (int, float)) else None,
            "interval_hint": LANES.get(lane, 0),
            "consecutive_errors": 0,
        })
    except OSError as exc:
        print(f"monitor: heartbeat write failed for {lane} ({exc})",
              file=sys.stderr)


def record_scan_error(manager_name: str | None, lane: str) -> int:
    path = error_counter_path(manager_name, lane)
    prior = state.read_json(path) or {}
    count = prior.get("consecutive_errors")
    count = (count if isinstance(count, int) and count > 0 else 0) + 1
    try:
        state.write_json_atomic(path, {**prior, "consecutive_errors": count})
    except OSError as exc:
        print(f"monitor: cannot record the error count for {lane} ({exc}); "
              f"the state root is unwritable, so this lane cannot function — "
              f"ending it rather than retrying blind.", file=sys.stderr)
        return max_consecutive_errors(lane)
    return count
