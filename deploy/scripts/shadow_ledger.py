#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_HOME_ENV = os.environ.get("HOME")
HOME = Path(_HOME_ENV) if _HOME_ENV else Path("/nonexistent-no-home")
DEFAULT_SHADOW_DIR = HOME / ".claude" / "dockwright" / "shadow"

CRITERIA_KEYS = ("min_n", "min_used_rate", "min_window_days", "min_abstained")
DISPOSITIONS = ("used", "edited", "discarded", "abstained")


class LedgerCorruption(ValueError):
    pass


def _canonical(criteria: dict) -> str:
    return json.dumps(criteria, sort_keys=True, separators=(",", ":"))


def _lane_paths(shadow_dir: Path, lane: str):
    return shadow_dir / f"{lane}.jsonl", shadow_dir / f"{lane}.criteria.json"


def _append_event(ledger: Path, event: str, ts=None, **fields) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    record = {"type": event, "v": 1, "ts": time.time() if ts is None else ts}
    record.update(fields)
    with ledger.open("a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def _events(ledger: Path):
    if not ledger.is_file():
        return
    for lineno, line in enumerate(ledger.read_text().splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            raise LedgerCorruption(
                f"ledger {ledger} line {lineno} is not valid JSON - refusing to "
                "compute a verdict over a damaged ledger")
        if isinstance(record, dict):
            yield record


def _first_stamp(ledger: Path):
    for record in _events(ledger):
        if record.get("type") == "criteria_armed":
            return record.get("criteria")
    return None


def _validate_criteria(criteria) -> list:
    problems = []
    if not isinstance(criteria, dict):
        return ["criteria must be a JSON object"]
    for key in criteria:
        if key not in CRITERIA_KEYS:
            problems.append(f"unknown criteria key: {key} (closed vocabulary: "
                            f"{', '.join(CRITERIA_KEYS)})")
    for key in CRITERIA_KEYS:
        if key not in criteria:
            problems.append(f"missing criteria key: {key} (all four are required; "
                            "opt out of abstention with min_abstained: 0)")
        elif not isinstance(criteria[key], (int, float)) or isinstance(criteria[key], bool):
            problems.append(f"criteria key {key} must be numeric")
    return problems


def cmd_arm(lane: str, criteria_json: str, shadow_dir: Path) -> int:
    try:
        criteria = json.loads(criteria_json)
    except json.JSONDecodeError as exc:
        print(f"shadow-ledger: criteria is not valid JSON: {exc}", file=sys.stderr)
        return 2
    problems = _validate_criteria(criteria)
    if problems:
        for problem in problems:
            print(f"shadow-ledger: {problem}", file=sys.stderr)
        return 2
    ledger, criteria_path = _lane_paths(shadow_dir, lane)
    stamp = _first_stamp(ledger)
    if stamp is not None:
        if _canonical(stamp) != _canonical(criteria):
            print(f"shadow-ledger: lane {lane} already armed with different "
                  "criteria - first stamp is immutable (re-arming with weaker "
                  "criteria is the failure mode this guards)", file=sys.stderr)
            return 2
        if not criteria_path.exists():
            criteria_path.parent.mkdir(parents=True, exist_ok=True)
            criteria_path.write_text(json.dumps(criteria, sort_keys=True, indent=1) + "\n")
        print(f"shadow-ledger: lane {lane} already armed (idempotent)")
        return 0
    criteria_path.parent.mkdir(parents=True, exist_ok=True)
    criteria_path.write_text(json.dumps(criteria, sort_keys=True, indent=1) + "\n")
    _append_event(ledger, "criteria_armed", lane=lane, criteria=criteria)
    print(f"shadow-ledger: armed lane {lane}: {_canonical(criteria)}")
    return 0


def cmd_append(lane: str, draft_id: str, disposition: str, note: str,
               shadow_dir: Path, now: float) -> int:
    if disposition not in DISPOSITIONS:
        print(f"shadow-ledger: unknown disposition {disposition!r} "
              f"({'|'.join(DISPOSITIONS)})", file=sys.stderr)
        return 2
    ledger, _criteria_path = _lane_paths(shadow_dir, lane)
    if _first_stamp(ledger) is None:
        print(f"shadow-ledger: lane {lane} is not armed - declare criteria "
              "BEFORE collecting data (arm first)", file=sys.stderr)
        return 2
    record = {"lane": lane, "id": draft_id, "disposition": disposition}
    if note:
        record["note"] = note
    _append_event(ledger, "disposition", ts=now, **record)
    print(f"shadow-ledger: {lane} {draft_id} -> {disposition}")
    return 0


def cmd_report(lane: str, shadow_dir: Path, require_graduate: bool, now: float) -> int:
    ledger, criteria_path = _lane_paths(shadow_dir, lane)
    stamp = _first_stamp(ledger)
    if stamp is None:
        print(f"shadow-ledger: lane {lane} is not armed", file=sys.stderr)
        return 2
    try:
        criteria = json.loads(criteria_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"shadow-ledger: cannot read criteria file: {exc}", file=sys.stderr)
        return 2
    if _canonical(criteria) != _canonical(stamp):
        print("shadow-ledger: criteria file does not match the first armed "
              "stamp - hand-edited criteria are not honored (verbatim compare)",
              file=sys.stderr)
        return 2
    counts = {d: 0 for d in DISPOSITIONS}
    first_ts = None
    anomalies = []
    for record in _events(ledger):
        if record.get("type") != "disposition":
            continue
        disposition = record.get("disposition")
        if disposition not in counts:
            anomalies.append(f"unknown disposition in ledger: {disposition!r}")
            continue
        counts[disposition] += 1
        ts = record.get("ts")
        if isinstance(ts, (int, float)) and (first_ts is None or ts < first_ts):
            first_ts = ts
    if anomalies:
        for anomaly in anomalies:
            print(f"shadow-ledger: {anomaly}", file=sys.stderr)
        return 2
    denom = counts["used"] + counts["edited"] + counts["discarded"]
    used_rate = (counts["used"] / denom) if denom else None
    window_days = ((now - first_ts) / 86400) if first_ts is not None else None
    checks = [
        ("min_n", f"{denom}/{criteria['min_n']}", denom >= criteria["min_n"]),
        ("min_used_rate",
         f"{used_rate:.2f}/{criteria['min_used_rate']}" if used_rate is not None
         else f"NOT-ENOUGH-DATA (n=0)/{criteria['min_used_rate']}",
         used_rate is not None and used_rate >= criteria["min_used_rate"]),
        ("min_window_days",
         f"{window_days:.1f}/{criteria['min_window_days']}" if window_days is not None
         else f"NOT-ENOUGH-DATA (no entries)/{criteria['min_window_days']}",
         window_days is not None and window_days >= criteria["min_window_days"]),
        ("min_abstained", f"{counts['abstained']}/{criteria['min_abstained']}",
         counts["abstained"] >= criteria["min_abstained"]),
    ]
    for name, evidence, passed in checks:
        print(f"  {name}: {evidence} {'MET' if passed else 'NOT MET'}")
    graduated = all(passed for _name, _evidence, passed in checks)
    status = "GRADUATE" if graduated else "NOT-YET"
    print(f"shadow-ledger: lane {lane}: {status} "
          f"(used={counts['used']} edited={counts['edited']} "
          f"discarded={counts['discarded']} abstained={counts['abstained']})")
    if require_graduate and not graduated:
        if denom == 0:
            total_events = denom + counts["abstained"]
            if total_events == 0:
                print("shadow-ledger: empty lane under --require-graduate - either "
                      "the lane never ran or the append wiring is broken; refusing "
                      "to treat 'no data' as a verdict", file=sys.stderr)
            else:
                print("shadow-ledger: no draft dispositions to rate (all abstained) "
                      "under --require-graduate; refusing to treat 'no data' as a "
                      "verdict", file=sys.stderr)
            return 2
        return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Shadow graduation ledger.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_arm = sub.add_parser("arm")
    p_arm.add_argument("--lane", required=True)
    p_arm.add_argument("--criteria", required=True)
    p_arm.add_argument("--shadow-dir", default=str(DEFAULT_SHADOW_DIR))
    p_app = sub.add_parser("append")
    p_app.add_argument("--lane", required=True)
    p_app.add_argument("--id", required=True, dest="draft_id")
    p_app.add_argument("--disposition", required=True)
    p_app.add_argument("--note", default="")
    p_app.add_argument("--shadow-dir", default=str(DEFAULT_SHADOW_DIR))
    p_app.add_argument("--now", type=float, default=None,
                       help="Override the event clock (epoch; tests).")
    p_rep = sub.add_parser("report")
    p_rep.add_argument("--lane", required=True)
    p_rep.add_argument("--require-graduate", action="store_true")
    p_rep.add_argument("--shadow-dir", default=str(DEFAULT_SHADOW_DIR))
    p_rep.add_argument("--now", type=float, default=None)
    args = parser.parse_args(argv)
    shadow_dir = Path(args.shadow_dir).expanduser()
    try:
        if args.cmd == "arm":
            return cmd_arm(args.lane, args.criteria, shadow_dir)
        if args.cmd == "append":
            return cmd_append(args.lane, args.draft_id, args.disposition,
                              args.note, shadow_dir, args.now)
        now = args.now if args.now is not None else time.time()
        return cmd_report(args.lane, shadow_dir, args.require_graduate, now)
    except LedgerCorruption as exc:
        print(f"shadow-ledger: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
