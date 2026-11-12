from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from . import config, identity, lane_io, monitor, monitor_loop, paths, registry, state
from .state import _pid_alive

OK = "OK"
DEAD = "DEAD"
NEVER_ARMED = "NEVER-ARMED"
BACKLOGGED = "BACKLOGGED"
ARMED_UNRESOLVED = "ARMED-UNRESOLVED"
STALLED = "STALLED"
HUNG = "HUNG"
IN_DAEMON = "IN-DAEMON"

_SEVERITY = (NEVER_ARMED, DEAD, HUNG, STALLED, ARMED_UNRESOLVED, BACKLOGGED, OK)

MIN_ITER_WINDOW_SEC = 60
ITER_GRACE_SEC = 15

_BUCKETS = {
    "questions": lambda name: paths.question_dir_for(name),
    "done": lambda name: paths.DONE / name,
    "turn-ends": lambda name: paths.TURN_ENDS / name,
    "pages": lambda name: paths.notify_outbox_dir_for(name),
}

FALLBACK = ("arm a Monitor task with command `dockwright monitor-loop {lane}` "
            "(timeout_ms 1800000, re-arm on each expiry)")

_RECOVERY = {
    NEVER_ARMED: ("no lane process for this CLI: re-run dockwright lanes after 10 s; if it "
                  "is still NEVER-ARMED, invoke Skill(dockwright:arm-lanes) and re-run "
                  "dockwright lanes after another 10 s; if it stays NEVER-ARMED the "
                  "dockwright plugin is not installed — run setup.sh; until then "
                  + FALLBACK),
    DEAD: ("the lane process died and the plugin cannot re-arm it in this CLI process: "
           + FALLBACK + " or recreate the manager tab"),
    ARMED_UNRESOLVED: ("the lane is running; its last scan could not resolve the manager — "
                       "re-run dockwright lanes after one lane interval; arm nothing"),
    HUNG: ("the lane process {lane_pid} is alive but stopped iterating {age}s ago: "
           "`kill {lane_pid}`, then " + FALLBACK + " or recreate the manager tab"),
    STALLED: ("the lane is running but its scans fail; it ends itself after "
              "max_consecutive_errors failing scans (about 60 s for the 2 s lanes, one scan "
              "for stale) — re-run dockwright lanes, then treat DEAD as above"),
    BACKLOGGED: "unconsumed events are waiting; re-query list_pending_questions / list_workers",
}


def recovery_line(verdict: str, lane: str, row: dict | None = None) -> str:
    text = _RECOVERY.get(verdict, "")
    if not text:
        return ""
    row = row or {}
    age = row.get("iter_age_sec")
    return text.format(lane=lane, lane_pid=row.get("pid") or "<lane pid>",
                       age="?" if not isinstance(age, (int, float)) else f"{age:.0f}")


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
    if lane == "pages":
        bucket = bucket_for(manager_name)
        if not bucket.is_dir():
            return 0
        stale = 0
        for entry in bucket.glob("*.json"):
            try:
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            payload = state.read_json(entry)
            from_daemon = isinstance(payload, dict) and payload.get("source") in monitor.URGENT_SOURCES
            if (from_daemon and age > window) or age > monitor.OUTBOX_MAX_HOLD_SEC:
                stale += 1
        return stale
    seen = monitor._load_seen(monitor._seen_file(lane, manager_name))
    if lane == "questions":
        seen_ids = {Path(line).stem for line in seen}
        entries = [e for e in registry.manager_question_paths(manager_name) if e.stem not in seen_ids]
    else:
        bucket = bucket_for(manager_name)
        if not bucket.is_dir():
            return 0
        entries = [e for e in bucket.glob("*.json") if str(e) not in seen]
    stale = 0
    for entry in entries:
        try:
            if now - entry.stat().st_mtime > window:
                stale += 1
        except OSError:
            continue
    return stale


def _active_record(manager_name: str) -> dict | None:
    if not paths.ACTIVE.is_dir():
        return None
    for record in state.list_json_in(paths.ACTIVE):
        if record.get("agent") == "manager" and record.get("name") == manager_name:
            return record
    return None


def _record_pid(manager_name: str) -> int | None:
    record = _active_record(manager_name)
    pid = (record or {}).get("pid")
    return pid if isinstance(pid, int) else None


def _lane_command(lane_pid: int) -> str:
    try:
        result = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(lane_pid)],
                                capture_output=True, text=True, timeout=2, check=False)
        return "" if result.returncode != 0 else result.stdout.strip()
    except Exception:
        return ""


def _command_runs_lane(command: str, lane: str) -> bool:
    tokens = command.split()
    return any(token == "monitor-loop" and tokens[index + 1] == lane
               for index, token in enumerate(tokens[:-1]))


def _lane_process_alive(lane_pid: int, lane: str) -> bool:
    return _pid_alive(lane_pid) and _command_runs_lane(_lane_command(lane_pid), lane)


def _own_marker(record_pid: int, lane: str) -> dict | None:
    marker = state.read_json(monitor_loop.marker_path(record_pid, lane))
    return marker if isinstance(marker, dict) else None


def _iter_age(marker: dict | None, now: float) -> float | None:
    last_iter = (marker or {}).get("last_iter")
    return None if not isinstance(last_iter, (int, float)) else now - last_iter


def _marker_verdict(marker: dict | None, window: float, iter_age: float | None,
                    lane: str) -> str:
    if marker is None:
        return NEVER_ARMED
    lane_pid = marker.get("lane_pid")
    if not isinstance(lane_pid, int) or not _lane_process_alive(lane_pid, lane):
        return DEAD
    if iter_age is not None and iter_age > max(window, MIN_ITER_WINDOW_SEC) + ITER_GRACE_SEC:
        return HUNG
    return STALLED if marker.get("resolved") is True else ARMED_UNRESOLVED


def inspect(manager_name: str, now: float | None = None, record_pid: int | None = None,
            heartbeat_only: bool = False) -> list[dict]:
    now = time.time() if now is None else now
    record_missing = True
    if record_pid is None and not heartbeat_only:
        active = _active_record(manager_name)
        record_missing = active is None
        pid = (active or {}).get("pid")
        record_pid = pid if isinstance(pid, int) else None
        heartbeat_only = record_pid is None
    limited = monitor._manager_limited(manager_name)
    rows = []
    for lane, interval in lane_io.LANES.items():
        window = lane_io.stale_window(lane)
        in_daemon = monitor_loop.runs_in_daemon(lane)
        record = state.read_json(lane_io.heartbeat_path(manager_name, lane))
        last_scan = (lane_io.observed_at(lane) if in_daemon and lane in lane_io.OBSERVED_LANES
                     else (record or {}).get("last_scan"))
        fresh = isinstance(last_scan, (int, float)) and now - last_scan <= window
        age = None if not isinstance(last_scan, (int, float)) else now - last_scan
        marker = None if heartbeat_only else _own_marker(record_pid, lane)
        iter_age = _iter_age(marker, now)
        if fresh:
            heartbeat_verdict = OK
        elif heartbeat_only:
            heartbeat_verdict = DEAD if age is not None else NEVER_ARMED
        else:
            heartbeat_verdict = _marker_verdict(marker, window, iter_age, lane)
        if in_daemon:
            heartbeat_verdict, backlog, backlog_verdict = IN_DAEMON, None, OK
        else:
            backlog = None if limited else _backlog(manager_name, lane, window, now)
            backlog_verdict = BACKLOGGED if backlog else OK
        rows.append({
            "lane": lane,
            "interval": interval,
            "verdict": IN_DAEMON if heartbeat_verdict == IN_DAEMON else _worst(heartbeat_verdict, backlog_verdict),
            "heartbeat": heartbeat_verdict,
            "age_sec": age,
            "backlog": backlog,
            "limited": limited,
            "last_emit": (record or {}).get("last_emit"),
            "pid": (marker or {}).get("lane_pid"),
            "iter_age_sec": iter_age,
            "scan_pid": (record or {}).get("pid"),
            "heartbeat_only": heartbeat_only,
            "record_missing": record_missing,
        })
    return rows


def _format(row: dict) -> str:
    age = "never" if row["age_sec"] is None else f"{row['age_sec']:.0f}s ago"
    if row["verdict"] == IN_DAEMON:
        backlog = "runs in the daemon; dockwright daemon status"
    elif row["backlog"] is None:
        backlog = ("BACKLOG NOT CHECKED — held while the manager is rate-limited"
                   if row["limited"] else
                   "BACKLOG NOT CHECKED — this lane holds events by design")
    else:
        backlog = f"backlog {row['backlog']}"
    verdict = row["verdict"] + (" (heartbeat-only)" if row["heartbeat_only"] else "")
    return (f"{verdict:<28} {row['lane']:<10} "
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
    header = f"lanes for {manager_name}:"
    if rows and rows[0]["heartbeat_only"]:
        header += (f" (no active record for {manager_name}; marker lookup skipped)"
                   if rows[0].get("record_missing", True) else
                   f" (active record for {manager_name} has no pid; marker lookup skipped)")
    print(header)
    for row in rows:
        print(f"  {_format(row)}")
        if row["verdict"] not in (OK, IN_DAEMON):
            print(f"    → {recovery_line(row['verdict'], row['lane'], row)}")
    from . import doctor
    tmux_env = doctor.check_tmux_env()
    if not tmux_env.ok:
        print(f"  WARNING {tmux_env.name} — {tmux_env.detail}")
    broken = [r for r in rows if r["verdict"] not in (OK, IN_DAEMON)]
    if broken:
        print(f"{len(broken)} of {len(rows)} lanes are not delivering: "
              f"{', '.join(r['lane'] for r in broken)}. Events already consumed are gone.",
              file=sys.stderr)
        return 1
    return 0
