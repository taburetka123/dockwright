"""`dockwright lanes` — can this manager still hear its fleet?

Silence is both the healthy state and the failure mode of a monitor lane, so
before this existed a manager had no way to tell a quiet fleet from a dead
lane. Two managers spent a day mistaking one for the other in both directions:
real events were lost unnoticed, and two perfectly healthy idle lanes were
reported as broken.

The verdict is the WORST of two signals that fail independently:

1. **Heartbeat age.** Written by a scan only after it passed its reader
   preflight and flushed every line it emitted (see lane_io). A lane that
   cannot deliver raises before the write, so a fresh heartbeat is evidence of
   delivery CAPABILITY, not a self-report. Stale past HEARTBEAT_STALE_INTERVALS
   poll intervals -> DEAD; never written -> NEVER-ARMED.

2. **Backlog**, derived from the event directory and the cursor — never from
   the heartbeat. Events sitting unconsumed past the same window mean the lane
   is not draining whatever it claims about itself.

`OK` requires both. Signal 2 is derived from the thing being guarded, which is
what keeps this from reporting healthy while broken: a lane that stops draining
grows a backlog, and a lane that dies while the fleet is quiet goes stale with
no backlog to show.

Where signal 2 does not apply it says so (`backlog=n/a`) rather than quietly
counting a check it never ran — `turn-ends` legitimately HOLDS events without
consuming them (delegation, turn-burst, and FS-ladder rungs reaching 4h) and
`stale` keeps a threshold ladder rather than a per-event cursor.
"""
from __future__ import annotations

import sys
import time

from . import identity, lane_io, monitor, paths, state

OK = "OK"
DEAD = "DEAD"
NEVER_ARMED = "NEVER-ARMED"
BACKLOGGED = "BACKLOGGED"

# Worst-first, so a lane with several problems reports the most alarming one.
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
    """Unconsumed events older than the window, or None when not applicable."""
    if lane not in lane_io.BACKLOG_CHECKED_LANES:
        return None
    bucket_for = _BUCKETS.get(lane)
    if bucket_for is None:
        return None
    bucket = bucket_for(manager_name)
    if not bucket.is_dir():
        return 0
    # Via monitor._load_seen, not a raw read: that helper normalizes cursor
    # lines written under the pre-rename state root. Reading raw meant every
    # legacy line failed to match mid-migration and the lane reported a
    # phantom BACKLOGGED — a false alarm from the check whose whole job is to
    # be trustworthy when it fires.
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
    """One row per lane, iterating the canonical set — never a second list."""
    now = time.time() if now is None else now
    # While stale_monitor flags the manager as bricked on a rate-limit banner,
    # every lane deliberately holds: it prints nothing and marks nothing seen,
    # so events pile up ON PURPOSE and replay in full when the flag clears.
    # Reporting that hold as BACKLOGGED would be a false alarm at the one
    # moment the manager can least act on it, so the backlog arm is suspended
    # and the row says WHY. The heartbeat arm keeps running — a held lane still
    # scans, still passes preflight, and still has to prove it is alive.
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
    """One row. The backlog column deliberately does NOT render "not checked"
    in the same shape as a checked-and-empty result.

    `backlog=0` and `backlog=n/a` skim as the same thing, and that substitution
    — "not checked" read as "checked and fine" — is the exact confusion this
    whole command exists to end. So a real count is `backlog N` and a
    non-applicable one is a sentence saying why nobody looked.
    """
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
    """Print one row per lane; non-zero when any lane is not OK.

    The exit code is what lets this compose — into `dockwright doctor`, into a
    manager's boot check, into a shell guard — without anyone parsing the text.
    """
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
