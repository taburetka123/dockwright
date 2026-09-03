import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from . import paths, state
from .spend_ledger import read_events

def _prefer_new(new: Path, legacy: Path) -> Path:
    if new.exists():
        return new
    if legacy.exists():
        return legacy
    return new


_HOME = Path(os.environ.get("HOME", ""))
GARDENER_LEDGER = _prefer_new(
    _HOME / ".claude" / "dockwright" / "gardener",
    _HOME / ".claude" / "gardener",
) / "ledger.jsonl"

_TOKEN_KEYS = ("out_tokens", "in_tokens", "cache_read_tokens", "cache_creation_tokens")


def _date_of(ts) -> date:
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
        try:
            return datetime.fromtimestamp(ts).date()
        except (OverflowError, OSError, ValueError):
            pass
    return date.today()


def _coerce_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _coerce_float(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _spend_of(spend) -> dict | None:
    if not isinstance(spend, dict):
        return None
    totals = {}
    for key in ("turns",) + _TOKEN_KEYS:
        coerced = _coerce_int(spend.get(key))
        if coerced is not None:
            totals[key] = coerced
    return totals or None


def _unit(name, agent, source, day, spend) -> dict:
    return {"name": name, "agent": agent, "source": source, "date": day, "spend": spend}


def collect_units() -> list[dict]:
    units = []
    ledger_sids = set()
    for event in read_events():
        spend = _spend_of(event.get("spend"))
        if spend is None:
            continue
        if event.get("sid"):
            ledger_sids.add(event["sid"])
        units.append(_unit(event.get("name") or event.get("sid") or "?",
                           event.get("agent") or "worker",
                           event.get("source") or "ledger",
                           _date_of(event.get("ts")), spend))
    for record in state.list_json_in(paths.CLOSED):
        if not isinstance(record, dict):
            continue
        spend = _spend_of(record.get("spend"))
        if spend is None:
            continue
        if record.get("claude_sid") in ledger_sids and record.get("closed_reason") == "session_end":
            continue
        units.append(_unit(record.get("name") or record.get("claude_sid") or "?",
                           "worker", "closed", _date_of(record.get("closed_at")), spend))
    for record in state.list_json_in(paths.ACTIVE):
        if not isinstance(record, dict):
            continue
        spend = _spend_of(record.get("spend"))
        if spend is None:
            continue
        agent = "nested" if record.get("nested") else (record.get("agent") or "worker")
        units.append(_unit(record.get("name") or record.get("claude_sid") or "?",
                           agent, "live", date.today(), spend))
    units.extend(_gardener_units())
    return units


def _gardener_units() -> list[dict]:
    try:
        raw = GARDENER_LEDGER.read_text(errors="replace")
    except OSError:
        return []
    units = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("event") != "run_end":
            continue
        spend = {}
        for key in _TOKEN_KEYS:
            coerced = _coerce_int(event.get(key))
            if coerced is not None:
                spend[key] = coerced
        if not spend:
            continue
        lane = event.get("lane") or "run"
        units.append(_unit(f"gardener:{lane}", "headless", "gardener",
                           _date_of(_coerce_float(event.get("ts"))), spend))
    return units


def group_by_day(units: list[dict], since: date, until: date | None = None) -> list[dict]:
    until = until or date.today()
    buckets: dict[date, dict] = {}
    source_sets: dict[tuple, set] = {}
    for unit in units:
        if not (since <= unit["date"] <= until):
            continue
        rows = buckets.setdefault(unit["date"], {})
        row_key = (unit["date"], unit["name"], unit["agent"])
        if row_key not in rows:
            rows[row_key] = {"name": unit["name"], "agent": unit["agent"],
                             "source": "", "turns": None}
            source_sets[row_key] = set()
        row = rows[row_key]
        source_sets[row_key].add(unit["source"])
        turns = unit["spend"].get("turns")
        if turns is not None:
            row["turns"] = (row["turns"] or 0) + turns
        for key_name in _TOKEN_KEYS:
            value = unit["spend"].get(key_name)
            if value is not None:
                row[key_name] = row.get(key_name, 0) + value
    for row_key, srcs in source_sets.items():
        day, _name, _agent = row_key
        row = buckets[day][row_key]
        row["source"] = "+".join(sorted(srcs))
        for key_name in _TOKEN_KEYS:
            if key_name not in row:
                row[key_name] = None
    days = []
    for day in sorted(buckets, reverse=True):
        rows = sorted(buckets[day].values(),
                      key=lambda r: r.get("out_tokens") or 0, reverse=True)
        days.append({"date": day, "rows": rows, "total": _total(rows)})
    return days


def _total(rows: list[dict]) -> dict:
    total: dict = {}
    for key in _TOKEN_KEYS:
        values = [row[key] for row in rows if row.get(key) is not None]
        total[key] = sum(values) if values else None
    turn_values = [row["turns"] for row in rows if row["turns"] is not None]
    total["turns"] = sum(turn_values) if turn_values else None
    return total


def _humanize(n) -> str:
    if n is None:
        return "-"
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    if n < 1_000_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1_000_000_000:.1f}G"


_ROW_COLUMNS = ("turns", "in_tokens", "out_tokens", "cache_read_tokens", "cache_creation_tokens")
_HEADER = (f"  {'name':<34} {'agent':<9} {'turns':>6} {'in':>9} {'out':>9}"
           f" {'cache-rd':>9} {'cache-cr':>9}  source")


def _row_line(label, agent, values, source) -> str:
    cells = " ".join(
        f"{_humanize(values.get(key)):>{6 if key == 'turns' else 9}}"
        for key in _ROW_COLUMNS)
    return f"  {label:<34.34} {agent:<9} {cells}  {source}"


def _grand_total(days) -> dict:
    day_totals = [day["total"] for day in days]
    total: dict = {}
    for key in _TOKEN_KEYS:
        values = [t[key] for t in day_totals if t.get(key) is not None]
        total[key] = sum(values) if values else None
    turn_values = [t["turns"] for t in day_totals if t.get("turns") is not None]
    total["turns"] = sum(turn_values) if turn_values else None
    return total


def _render_text(days, since, until, spendless_closed) -> str:
    lines = [
        f"Spend report — {since} → {until}",
        "Tokens only: no $-cost field exists in the captured data; counts are",
        "tokens summed per session period, attributed to the day the period ended.",
        "",
    ]
    if not days:
        lines.append("No spend data in window.")
        lines.append("")
    for day in days:
        lines.append(str(day["date"]))
        lines.append(_HEADER)
        for row in day["rows"]:
            lines.append(_row_line(row["name"], row["agent"], row, row["source"]))
        lines.append(_row_line("day total", "", day["total"], ""))
        lines.append("")
    if days:
        total = _grand_total(days)
        lines.append(_row_line("TOTAL", "", total, ""))
        exact = ", ".join(f"{key}={total[key]:,}"
                          for key in ("out_tokens", "in_tokens", "cache_read_tokens")
                          if total.get(key) is not None)
        lines.append(f"  exact: {exact}")
        lines.append("")
    lines.append("note: codex-runtime sessions carry no spend data (their transcripts lack usage)")
    if spendless_closed:
        lines.append(f"note: {spendless_closed} closed session(s) in window have no spend data"
                     " (pre-capture closures)")
    return "\n".join(lines) + "\n"


def _count_spendless_closed(since, until) -> int:
    count = 0
    for record in state.list_json_in(paths.CLOSED):
        if not isinstance(record, dict):
            continue
        if _spend_of(record.get("spend")) is not None:
            continue
        if since <= _date_of(record.get("closed_at")) <= until:
            count += 1
    return count


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="dockwright spend-report",
        description="Fleet token-spend report (tokens only — no $-cost data exists). "
                    "Spend is attributed to the day a session period ENDED (local time); "
                    "live sessions count toward today. "
                    "With a worker name / sid, reports that ONE session's time and "
                    "money from its transcript instead.")
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--days", type=int, default=None,
                        help="window size in days, today inclusive (default 7)")
    window.add_argument("--since", type=str, default=None,
                        help="window start date YYYY-MM-DD (until today)")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="machine-readable output")
    parser.add_argument("session", nargs="?", default=None,
                        help="worker name or sid — switches to the per-session report")
    parser.add_argument("--include-replayed", action="store_true",
                        help="per-session mode: count replayed pre-session history")
    parser.add_argument("--transcript", type=str, default=None,
                        help="per-session mode: report an arbitrary transcript .jsonl")
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    if args.session or args.transcript:
        if args.days is not None or args.since is not None:
            print("--days/--since are fleet-mode flags; the per-session report "
                  "covers the whole session", file=sys.stderr)
            return 2
        from .spend_session import run as session_run
        forwarded = []
        if args.as_json:
            forwarded.append("--json")
        if args.include_replayed:
            forwarded.append("--include-replayed")
        if args.transcript:
            forwarded += ["--transcript", args.transcript]
        if args.session:
            forwarded += ["--", args.session]
        return session_run(forwarded)
    if args.include_replayed:
        print("--include-replayed is a per-session flag; pass a worker name/sid "
              "or --transcript", file=sys.stderr)
        return 2
    until = date.today()
    if args.since is not None:
        try:
            since = date.fromisoformat(args.since)
        except ValueError:
            print(f"invalid --since date: {args.since!r} (want YYYY-MM-DD)", file=sys.stderr)
            return 2
    else:
        since = until - timedelta(days=(args.days or 7) - 1)
    days = group_by_day(collect_units(), since=since, until=until)
    spendless_closed = _count_spendless_closed(since, until)
    if args.as_json:
        print(json.dumps({
            "window": {"since": str(since), "until": str(until)},
            "days": [{"date": str(day["date"]), "rows": day["rows"], "total": day["total"]}
                     for day in days],
            "total": _grand_total(days) if days else {**{k: None for k in _TOKEN_KEYS}, "turns": None},
            "closed_without_spend": spendless_closed,
        }, indent=2))
    else:
        sys.stdout.write(_render_text(days, since, until, spendless_closed))
    return 0
