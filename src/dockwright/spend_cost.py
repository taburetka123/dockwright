"""`dockwright spend-cost`: per-model USD spend reconstructed from full
transcripts.

Fixes the three meter defects of the prior ledger-based meter: tail truncation (reads the WHOLE
transcript), flat-Sonnet pricing (prices per model via pricing.py), and
cache-creation omission (includes cache writes with their TTL multiplier).

Reconstructs from transcripts rather than the token ledger because the ledger
is tail-truncated and modelless for the historical window; the transcripts are
the source of truth. One session = one sid = one transcript (account-autoswitch's
multiple ledger rows collapse to one session).
"""
import argparse
import json
import sys
from datetime import date, datetime, timedelta

from . import paths, pricing, state
from .spend_ledger import read_events
from .transcript import find_session_log, sum_usage_by_model


def _date_of(ts) -> date:
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
        try:
            return datetime.fromtimestamp(ts).date()
        except (OverflowError, OSError, ValueError):
            pass
    return date.today()


def collect_sessions() -> list:
    """One {sid, date, name, runtime} per unique session across the ledger,
    closed/, and active/ — keyed on sid (a session is one transcript), keeping
    the latest end date when a sid appears more than once."""
    seen: dict = {}

    def _add(sid, when, name, runtime):
        if not sid:
            return
        prior = seen.get(sid)
        if prior is None or when > prior["date"]:
            seen[sid] = {"sid": sid, "date": when, "name": name or sid,
                         "runtime": runtime or "claude"}

    for event in read_events():
        _add(event.get("sid"), _date_of(event.get("ts")),
             event.get("name"), event.get("runtime"))
    for record in state.list_json_in(paths.CLOSED):
        if isinstance(record, dict):
            _add(record.get("claude_sid"), _date_of(record.get("closed_at")),
                 record.get("name"), record.get("runtime"))
    for record in state.list_json_in(paths.ACTIVE):
        if isinstance(record, dict):
            _add(record.get("claude_sid"), date.today(),
                 record.get("name"), record.get("runtime"))
    return list(seen.values())


def build_report(*, since: date, until: date) -> dict:
    """Reconstruct per-model USD spend for sessions whose end date is in
    [since, until]."""
    models: dict = {}        # canonical token+cost accumulation, keyed by raw model id
    anatomy = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}
    total = 0.0
    sessions_counted = 0
    missing = 0
    unpriced_out = 0
    for sess in collect_sessions():
        if not (since <= sess["date"] <= until):
            continue
        log = find_session_log(sess["sid"], runtime=sess.get("runtime") or "claude")
        if log is None:
            missing += 1
            continue
        by_model = sum_usage_by_model(log)
        if not by_model:
            missing += 1
            continue
        sessions_counted += 1
        for model_id, u in by_model.items():
            b = pricing.cost_breakdown(
                model_id,
                output_tokens=u["out_tokens"], input_tokens=u["in_tokens"],
                cache_read_tokens=u["cache_read_tokens"],
                cache_creation_5m_tokens=u["cache_creation_5m_tokens"],
                cache_creation_1h_tokens=u["cache_creation_1h_tokens"])
            entry = models.setdefault(model_id, {
                "model": model_id, "cost": 0.0, "sessions": 0, "calls": 0,
                "priced": b["priced"]})
            entry["cost"] += b["total"]
            entry["sessions"] += 1
            entry["calls"] += u["calls"]
            for k in anatomy:
                anatomy[k] += b[k]
            total += b["total"]
            if not b["priced"]:
                unpriced_out += u["out_tokens"]
    model_rows = sorted(models.values(), key=lambda m: m["cost"], reverse=True)
    return {
        "since": str(since), "until": str(until),
        "total": total, "sessions": sessions_counted,
        "missing_transcripts": missing,
        "unpriced_out_tokens": unpriced_out,
        "cache_cost": anatomy["cache_read"] + anatomy["cache_write"],
        "anatomy": anatomy,
        "models": model_rows,
    }


def _pct(part, whole):
    return (part / whole * 100.0) if whole else 0.0


def _render(report: dict) -> str:
    lines = [
        f"Spend cost (USD) — {report['since']} → {report['until']}",
        f"Reconstructed per-model from full transcripts; cache write+read priced.",
        "",
        f"  {'model':<26} {'cost':>10} {'%':>6} {'sess':>5} {'calls':>6}",
    ]
    total = report["total"]
    for m in report["models"]:
        flag = "" if m["priced"] else "  (unpriced)"
        lines.append(f"  {m['model']:<26} ${m['cost']:>9.2f} "
                     f"{_pct(m['cost'], total):>5.1f}% {m['sessions']:>5} {m['calls']:>6}{flag}")
    lines.append("")
    lines.append(f"  TOTAL ${total:.2f}  over {report['sessions']} session(s)")
    a = report["anatomy"]
    lines.append(f"  cache (write+read): ${report['cache_cost']:.2f} "
                 f"({_pct(report['cache_cost'], total):.1f}%)  "
                 f"[write ${a['cache_write']:.2f} / read ${a['cache_read']:.2f}]")
    lines.append(f"  output ${a['output']:.2f}  input(uncached) ${a['input']:.2f}")
    if report["missing_transcripts"]:
        lines.append(f"  note: {report['missing_transcripts']} session(s) in window "
                     "had no resolvable transcript (pruned) — not priced")
    if report["unpriced_out_tokens"]:
        lines.append(f"  note: {report['unpriced_out_tokens']:,} output tokens on "
                     "unpriced/unknown models (e.g. <synthetic>) — counted as $0")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="dockwright spend-cost",
        description="Per-model USD spend reconstructed from full transcripts "
                    "(fixes tail-truncation, flat-Sonnet pricing, cache-creation omission).")
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--days", type=int, default=None,
                        help="window size in days, today inclusive (default 7)")
    window.add_argument("--since", type=str, default=None,
                        help="window start date YYYY-MM-DD (until today)")
    parser.add_argument("--json", action="store_true", dest="as_json")
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    until = date.today()
    if args.since is not None:
        try:
            since = date.fromisoformat(args.since)
        except ValueError:
            print(f"invalid --since date: {args.since!r} (want YYYY-MM-DD)", file=sys.stderr)
            return 2
    else:
        since = until - timedelta(days=(args.days or 7) - 1)
    report = build_report(since=since, until=until)
    if args.as_json:
        print(json.dumps(report, indent=2))
    else:
        sys.stdout.write(_render(report))
    return 0
