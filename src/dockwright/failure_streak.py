from __future__ import annotations

import time
from pathlib import Path

from . import lane_io, state

FAILURES_TO_PAGE = 3
AWAKE_SEC_TO_PAGE = 600
SLEPT_SEC = 5


def awake_seconds() -> float:
    return lane_io.awake_seconds()


def wall_seconds() -> float:
    return time.time()


def mark() -> tuple[float, float]:
    return wall_seconds(), awake_seconds()


def slept(since: tuple[float, float]) -> bool:
    return (wall_seconds() - since[0]) - (awake_seconds() - since[1]) >= SLEPT_SEC


def _stamp(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _valid(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    n = entry.get("n")
    return (isinstance(n, int) and not isinstance(n, bool) and n >= 1
            and _stamp(entry.get("since")) and _stamp(entry.get("awake")))


def load(path: Path) -> dict[str, dict]:
    data = state.read_json(path)
    if not isinstance(data, dict):
        return {}
    return {key: entry for key, entry in data.items() if _valid(entry)}


def fail(streaks: dict[str, dict], key: str, now: float) -> dict:
    entry = streaks.get(key)
    if entry is None:
        entry = streaks[key] = {"n": 1, "since": now, "awake": awake_seconds()}
    else:
        entry["n"] += 1
    return entry


def clear(streaks: dict[str, dict], key: str) -> None:
    streaks.pop(key, None)


def due(entry: dict) -> bool:
    now = awake_seconds()
    span = now - entry["awake"]
    if span < 0:
        span = now
    return entry["n"] >= FAILURES_TO_PAGE and span >= AWAKE_SEC_TO_PAGE


def save(path: Path, streaks: dict[str, dict]) -> None:
    state.write_json_atomic(path, streaks)
