import json
from datetime import datetime
from pathlib import Path

from .spend_session import _fmt_duration, _fmt_tok
from .transcript import (_int_field, _parse_event_ts, codex_subagent_rollouts,
                         codex_usage_totals)

TOKEN_KEYS = ("in_tokens", "cache_read_tokens", "cache_creation_tokens",
              "out_tokens", "reasoning_tokens")
_TOTAL_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                 "output_tokens", "reasoning_output_tokens", "total_tokens")
NO_MODEL = "(none)"
PRICING_NOTE = "codex models are not priced — tokens only"
FALLBACK_NOTE = ("rollout not found or empty — tokens from the session record's "
                 "spend: the latest round of the session's own thread, sub-agent "
                 "threads not included")
NO_USAGE_NOTE = ("codex session carries no usage data — no rollout token counts "
                 "and no recorded spend")


def parse_rollout(path) -> dict:
    calls = []
    events = 0
    first_ts = last_ts = None
    model = None
    previous = None
    try:
        raw = Path(path).read_text(errors="replace")
    except OSError:
        raw = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        ts = _parse_event_ts(record.get("timestamp"))
        if ts is not None:
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "turn_context":
            named = payload.get("model")
            if isinstance(named, str) and named:
                model = named
            continue
        if record.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict) or not isinstance(info.get("total_token_usage"), dict):
            continue
        events += 1
        total = tuple(_int_field(info["total_token_usage"], field)
                      for field in _TOTAL_FIELDS)
        if total == previous:
            continue
        previous = total
        last = info.get("last_token_usage")
        calls.append({"model": model,
                      **codex_usage_totals(last if isinstance(last, dict) else {})})
    return {"calls": calls, "events": events, "first_ts": first_ts, "last_ts": last_ts}


def _bucket() -> dict:
    return {key: 0 for key in TOKEN_KEYS} | {"api_calls": 0}


def _add(bucket: dict, call: dict) -> None:
    bucket["api_calls"] += 1
    for key in TOKEN_KEYS:
        bucket[key] += call[key]


def build_codex_report(rollout, *, name=None, sid=None, sessions_root=None) -> dict | None:
    own = parse_rollout(rollout)
    if not own["events"]:
        return None
    main, sub = _bucket(), _bucket()
    by_model: dict = {}
    records = own["events"]
    for call in own["calls"]:
        _add(main, call)
        _add(by_model.setdefault(call["model"] or NO_MODEL, _bucket()), call)
    counted = forked = 0
    for path, is_forked in (codex_subagent_rollouts(sid, root=sessions_root)
                            if sid else []):
        if is_forked:
            forked += 1
            continue
        parsed = parse_rollout(path)
        counted += 1
        records += parsed["events"]
        for call in parsed["calls"]:
            _add(sub, call)
            _add(by_model.setdefault(call["model"] or NO_MODEL, _bucket()), call)
    first, last = own["first_ts"], own["last_ts"]
    return {
        "session": {"name": name, "sid": sid},
        "runtime": "codex",
        "source": "rollout",
        "transcript": {"path": str(rollout), "subagent_files": counted,
                       "forked_subagents_not_counted": forked},
        "wall_clock": {"seconds": (last - first
                                   if first is not None and last is not None else None),
                       "first": first, "last": last},
        "api_calls": {"unique": main["api_calls"] + sub["api_calls"],
                      "records": records},
        "tokens": {"main": main, "subagents": sub},
        "by_model": sorted(({"model": model, **bucket}
                            for model, bucket in by_model.items()),
                           key=lambda row: (-row["api_calls"], row["model"])),
        "money": None,
        "note": PRICING_NOTE,
    }


def _count(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def report_from_spend(spend, *, name=None, sid=None) -> dict | None:
    if not isinstance(spend, dict):
        return None
    main = {key: int(spend[key]) for key in TOKEN_KEYS if _count(spend.get(key))}
    if not main:
        return None
    turns = spend.get("turns")
    return {
        "session": {"name": name, "sid": sid},
        "runtime": "codex",
        "source": "record",
        "transcript": None,
        "wall_clock": None,
        "api_calls": None,
        "turns": int(turns) if _count(turns) else None,
        "tokens": {"main": main, "subagents": None},
        "by_model": [],
        "money": None,
        "note": FALLBACK_NOTE,
    }


def no_usage_report(*, name=None, sid=None) -> dict:
    return {"session": {"name": name, "sid": sid}, "runtime": "codex",
            "note": NO_USAGE_NOTE}


def _token_line(bucket: dict) -> str:
    line = (f"in {_fmt_tok(bucket.get('in_tokens'))} / "
            f"cache-rd {_fmt_tok(bucket.get('cache_read_tokens'))} / "
            f"cache-wr {_fmt_tok(bucket.get('cache_creation_tokens'))} / "
            f"out {_fmt_tok(bucket.get('out_tokens'))}")
    if bucket.get("reasoning_tokens") is not None:
        line += f" (reasoning {_fmt_tok(bucket['reasoning_tokens'])})"
    return line


def render_codex_text(report: dict) -> str:
    if "tokens" not in report:
        return f"{report['note']}\n"
    session = report["session"]
    label = session.get("name") or session.get("sid") or "?"
    sid8 = (session.get("sid") or "")[:8]
    wall = report.get("wall_clock")
    header = f"Session spend — {label}" + (f" ({sid8}…)" if sid8 else "")
    if wall and wall["first"] is not None:
        header += " · " + datetime.fromtimestamp(wall["first"]).strftime("%Y-%m-%d")
    lines = [header + " · codex"]
    transcript = report.get("transcript")
    if transcript:
        extra = (f" (+{transcript['subagent_files']} sub-agent rollout(s))"
                 if transcript["subagent_files"] else "")
        lines.append(f"rollout: {transcript['path']}{extra}")
    else:
        lines.append(f"note: {report['note']}")
    lines.append("")
    main = report["tokens"]["main"]
    if report.get("api_calls") is not None:
        count = f" [{main['api_calls']} calls]"
    elif report.get("turns") is not None:
        count = f" [{report['turns']} turn(s)]"
    else:
        count = ""
    lines.append(f"TOKENS        main: {_token_line(main)}{count}")
    sub = report["tokens"].get("subagents")
    if sub and transcript and transcript["subagent_files"]:
        lines.append(f"              sub-agents: {_token_line(sub)} "
                     f"[{sub['api_calls']} calls]")
    for row in report.get("by_model") or []:
        lines.append(f"  {row['model']:<26} in {_fmt_tok(row['in_tokens'])}  "
                     f"cache-rd {_fmt_tok(row['cache_read_tokens'])}  "
                     f"out {_fmt_tok(row['out_tokens'])}  [{row['api_calls']} calls]")
    lines.append("MONEY         not priced — codex models carry no rates (tokens only)")
    if wall:
        first = (datetime.fromtimestamp(wall["first"]).strftime("%Y-%m-%d %H:%M:%S")
                 if wall["first"] is not None else "-")
        last = (datetime.fromtimestamp(wall["last"]).strftime("%H:%M:%S")
                if wall["last"] is not None else "-")
        lines.append(f"WALL CLOCK    {_fmt_duration(wall['seconds'])}   "
                     f"({first} → {last})   {report['api_calls']['unique']} API calls")
    forked = (transcript or {}).get("forked_subagents_not_counted") or 0
    if forked:
        lines.append(f"note: {forked} forked sub-agent thread(s) not counted — "
                     "a forked rollout replays its parent's calls")
    return "\n".join(lines) + "\n"
