from __future__ import annotations

import sys
import time

from . import identity, lane_io, monitor, paths, state

OK = "OK"
DEAD = "DEAD"
NEVER_ARMED = "NEVER-ARMED"
BACKLOGGED = "BACKLOGGED"

_SEVERITY = (NEVER_ARMED, DEAD, BACKLOGGED, OK)

_BUCKETS = {
    "questions": lambda name: paths.question_dir_for(name),
    "done": lambda name: paths.DONE / name,
    "turn-ends": lambda name: paths.TURN_ENDS / name,
}


def _worst(*verdicts: str) -> str:
    for candidate in _SEVERITY:
        if candidate in verdicts:
            return candidate
    return OK


def _backlog(manager_name: str, lane: str, window: float, now: float):
    if lane not in lane_io.BACKLOG_CHECKED_LANES:
        return None
    bucket_for = _BUCKETS.get(lane)
    if bucket_for is None:
        return None
    bucket = bucket_for(manager_name)
    if not bucket.is_dir():
        return 0
    seen = monitor._load_seen(monitor._seen_file(lane, manager_name))
    stale = 0
    for entry in bucket.glob("*.json"):
        if str(entry) in seen:
            continue
        try:
            if now - entry.stat().st_mtime > window:
                stale += 1
        except OSError:
            continue
    return stale


def inspect(manager_name: str, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    limited = monitor._manager_limited(manager_name)
    rows = []
    for lane, interval in lane_io.LANES.items():
        window = interval * lane_io.HEARTBEAT_STALE_INTERVALS
        record = state.read_json(lane_io.heartbeat_path(manager_name, lane))
        last_scan = (record or {}).get("last_scan")
        if not isinstance(last_scan, (int, float)):
            heartbeat_verdict, age = NEVER_ARMED, None
        else:
            age = now - last_scan
            heartbeat_verdict = DEAD if age > window else OK
        backlog = None if limited else _backlog(manager_name, lane, window, now)
        backlog_verdict = BACKLOGGED if backlog else OK
        rows.append({
            "lane": lane,
            "interval": interval,
            "verdict": _worst(heartbeat_verdict, backlog_verdict),
            "heartbeat": heartbeat_verdict,
            "age_sec": age,
            "backlog": backlog,
            "limited": limited,
            "last_emit": (record or {}).get("last_emit"),
            "pid": (record or {}).get("pid"),
        })
    return rows


def _format(row: dict) -> str:
    age = "never" if row["age_sec"] is None else f"{row['age_sec']:.0f}s ago"
    if row["backlog"] is None:
        backlog = ("BACKLOG NOT CHECKED — held while the manager is rate-limited"
                   if row["limited"] else
                   "BACKLOG NOT CHECKED — this lane holds events by design")
    else:
        backlog = f"backlog {row['backlog']}"
    return (f"{row['verdict']:<12} {row['lane']:<10} "
            f"last-scan={age:<14} {backlog}")


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        print("Usage: dockwright lanes [manager-name]", file=sys.stderr)
        return 2
    if argv:
        manager_name = argv[0]
    else:
        try:
            manager_name = identity.resolve_manager()["name"]
        except SystemExit:
            print("dockwright lanes: cannot resolve the owning manager; pass "
                  "a manager name explicitly.", file=sys.stderr)
            return 2
    rows = inspect(manager_name)
    print(f"lanes for {manager_name}:")
    for row in rows:
        print(f"  {_format(row)}")
    broken = [r for r in rows if r["verdict"] != OK]
    if broken:
        print(f"{len(broken)} of {len(rows)} lanes are not delivering: "
              f"{', '.join(r['lane'] for r in broken)}. Re-arm them "
              f"(see /manager step 7); events already consumed are gone.",
              file=sys.stderr)
        return 1
    return 0
