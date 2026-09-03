import json
from pathlib import Path

from . import pricing
from .transcript import _parse_event_ts, usage_totals_of

_TOKEN_KEYS = ("out_tokens", "in_tokens", "cache_read_tokens",
               "cache_creation_5m_tokens", "cache_creation_1h_tokens")


def _iter_records(path: Path):
    try:
        raw = path.read_text(errors="replace")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            yield record


def _is_prompt(record: dict) -> bool:
    if record.get("isMeta") is True:
        return False
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") != "tool_result"
                   for b in content)
    return False


def _tool_use_blocks(record: dict):
    for block in (record.get("message") or {}).get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            yield block


def _tool_result_ids(record: dict):
    content = (record.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            yield block.get("tool_use_id")


def _new_bucket() -> dict:
    return {key: 0 for key in _TOKEN_KEYS} | {"records": 0, "api_calls": 0}


def _add_tokens(bucket: dict, entry: dict) -> None:
    for key in _TOKEN_KEYS:
        bucket[key] += entry[key]


def _parse_file(path: Path, *, sidecar: bool, birth_ts, seen_ids: set,
                state: dict) -> None:
    open_turn = False
    for record in _iter_records(path):
        ts = _parse_event_ts(record.get("timestamp"))
        if birth_ts is not None and ts is not None and ts < birth_ts:
            state["excluded_replayed"] += 1
            continue
        sidechain = sidecar or record.get("isSidechain") is True
        if not sidechain and ts is not None:
            wall = state["wall"]
            wall[0] = ts if wall[0] is None else min(wall[0], ts)
            wall[1] = ts if wall[1] is None else max(wall[1], ts)
        rtype = record.get("type")
        if rtype == "assistant":
            open_turn = _handle_assistant(record, ts, sidechain, open_turn,
                                          seen_ids, state)
        elif rtype == "user":
            _handle_user(record, ts, sidechain, open_turn, state)
        if (rtype in ("assistant", "user") and not sidechain
                and ts is not None and state["episodes"]):
            state["episodes"][-1]["last_ts"] = ts


def _handle_assistant(record: dict, ts, sidechain: bool, open_turn: bool,
                      seen_ids: set, state: dict) -> bool:
    entry = usage_totals_of(record)
    if entry is not None:
        state["unknown_usage_fields"].update(entry["unknown_usage_keys"])
        bucket = state["sub" if sidechain else "main"]
        bucket["records"] += 1
        if entry["message_id"] not in seen_ids:
            seen_ids.add(entry["message_id"])
            _count_entry(record, entry, ts, sidechain, bucket, state)
    for block in _tool_use_blocks(record):
        tool_use_id = block.get("id")
        if tool_use_id is not None:
            if tool_use_id in state["seen_tool_ids"]:
                continue
            state["seen_tool_ids"].add(tool_use_id)
        name = block.get("name") or "(unknown)"
        state["open_tools"][tool_use_id] = {
            "tool": name, "ts": ts, "sidechain": sidechain}
        _tool_row(state, name, sidechain)["calls"] += 1
    stop = (record.get("message") or {}).get("stop_reason")
    if isinstance(stop, str) and not sidechain:
        open_turn = stop == "tool_use"
        if stop == "end_turn":
            state["end_turn_count"] += 1
    return open_turn


def _count_entry(record: dict, entry: dict, ts, sidechain: bool,
                 bucket: dict, state: dict) -> None:
    bucket["api_calls"] += 1
    _add_tokens(bucket, entry)
    model_bucket = state["by_model"].setdefault(
        (entry["model"], sidechain), _new_bucket())
    model_bucket["api_calls"] += 1
    _add_tokens(model_bucket, entry)
    skill = record.get("attributionSkill")
    skill_label = skill if isinstance(skill, str) and skill else "(none)"
    skill_bucket = state["by_skill"].setdefault(
        (skill_label, entry["model"]), _new_bucket())
    skill_bucket["api_calls"] += 1
    _add_tokens(skill_bucket, entry)
    server = record.get("attributionMcpServer")
    if isinstance(server, str) and server:
        tool = record.get("attributionMcpTool")
        tool_label = tool if isinstance(tool, str) and tool else "(none)"
        mcp_bucket = state["by_mcp"].setdefault(
            (server, tool_label, entry["model"]), _new_bucket())
        mcp_bucket["api_calls"] += 1
        _add_tokens(mcp_bucket, entry)
    server_tool_use = ((record.get("message") or {}).get("usage") or {}).get(
        "server_tool_use")
    if isinstance(server_tool_use, dict):
        for key, value in server_tool_use.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            state["server_tool_use"][key] = (
                state["server_tool_use"].get(key, 0) + int(value))
    if sidechain:
        return
    if state["episodes"]:
        ep = state["episodes"][-1]
        ep["api_calls"] += 1
        ep["out_tokens"] += entry["out_tokens"]
        ep["entries"].append(entry)
    else:
        state["episodes"].append(_new_episode(ts, entry))


def _handle_user(record: dict, ts, sidechain: bool, open_turn: bool,
                 state: dict) -> None:
    for tool_use_id in _tool_result_ids(record):
        started = state["open_tools"].pop(tool_use_id, None)
        if started is None:
            continue
        row = _tool_row(state, started["tool"], started["sidechain"])
        if started["ts"] is not None and ts is not None:
            row["timed"] += 1
            row["total_s"] += ts - started["ts"]
    if sidechain:
        return
    if _is_prompt(record) and not open_turn:
        state["episodes"].append(_new_episode(ts, None))


def _new_episode(ts, entry) -> dict:
    ep = {"start_ts": ts, "last_ts": ts, "api_calls": 0, "out_tokens": 0,
          "entries": []}
    if entry is not None:
        ep["api_calls"] = 1
        ep["out_tokens"] = entry["out_tokens"]
        ep["entries"].append(entry)
    return ep


def _tool_row(state: dict, tool: str, sidechain: bool) -> dict:
    return state["tools"].setdefault((tool, sidechain), {
        "tool": tool, "sidechain": sidechain,
        "calls": 0, "timed": 0, "total_s": 0.0})


def _price_bucket(bucket: dict, model, rates) -> dict:
    return pricing.cost_breakdown(
        model, rates=rates,
        output_tokens=bucket["out_tokens"], input_tokens=bucket["in_tokens"],
        cache_read_tokens=bucket["cache_read_tokens"],
        cache_creation_5m_tokens=bucket["cache_creation_5m_tokens"],
        cache_creation_1h_tokens=bucket["cache_creation_1h_tokens"])


def _attr_rows(mapping: dict, label_keys: tuple, rates) -> list:
    merged: dict = {}
    for key, bucket in mapping.items():
        *labels, model = key
        agg = merged.setdefault(tuple(labels), {
            "api_calls": 0, "out_tokens": 0, "cache_read_tokens": 0,
            "cost": 0.0})
        agg["api_calls"] += bucket["api_calls"]
        agg["out_tokens"] += bucket["out_tokens"]
        agg["cache_read_tokens"] += bucket["cache_read_tokens"]
        agg["cost"] += _price_bucket(bucket, model, rates)["total"]
    rows = [dict(zip(label_keys, labels)) | agg
            for labels, agg in merged.items()]
    return sorted(rows, key=lambda r: r["cost"], reverse=True)


def build_report(main_log: Path, *, subagent_logs=None, started_at=None,
                 include_replayed=False, name=None, sid=None) -> dict:
    if subagent_logs is None:
        from .transcript import subagent_logs_for
        subagent_logs = subagent_logs_for(main_log, sid)
    birth_ts = None
    if not include_replayed and isinstance(started_at, (int, float)) and started_at > 0:
        birth_ts = float(started_at)
    state = {
        "main": _new_bucket(), "sub": _new_bucket(),
        "by_model": {}, "by_skill": {}, "by_mcp": {},
        "episodes": [], "end_turn_count": 0,
        "tools": {}, "open_tools": {}, "seen_tool_ids": set(),
        "wall": [None, None],
        "excluded_replayed": 0,
        "unknown_usage_fields": set(),
        "server_tool_use": {},
    }
    seen_ids: set = set()
    _parse_file(main_log, sidecar=False, birth_ts=birth_ts,
                seen_ids=seen_ids, state=state)
    for sub_log in subagent_logs or []:
        _parse_file(sub_log, sidecar=True, birth_ts=birth_ts,
                    seen_ids=seen_ids, state=state)
    rates = pricing.get_rates()
    try:
        from . import config
        overridden = bool(config.pricing_overrides())
    except Exception:
        overridden = False
    model_totals: dict = {}
    money = {"main": 0.0, "subagents": 0.0, "unpriced_out_tokens": 0}
    anatomy = {"input": 0.0, "output": 0.0, "cache_read": 0.0,
               "cache_write": 0.0}
    for (model, sidechain), bucket in sorted(
            state["by_model"].items(), key=lambda kv: str(kv[0][0])):
        b = _price_bucket(bucket, model, rates)
        money["subagents" if sidechain else "main"] += b["total"]
        for component in anatomy:
            anatomy[component] += b[component]
        if not b["priced"]:
            money["unpriced_out_tokens"] += bucket["out_tokens"]
        row = model_totals.setdefault(model or "(none)", {
            "model": model or "(none)", "cost": 0.0, "priced": b["priced"],
            **{key: 0 for key in _TOKEN_KEYS}, "api_calls": 0})
        row["cost"] += b["total"]
        row["api_calls"] += bucket["api_calls"]
        _add_tokens(row, bucket)
    money["total"] = money["main"] + money["subagents"]
    by_model_rows = sorted(model_totals.values(),
                           key=lambda r: r["cost"], reverse=True)
    episodes = []
    for ep in state["episodes"]:
        cost = 0.0
        per_model: dict = {}
        for entry in ep["entries"]:
            per_model.setdefault(entry["model"], []).append(entry)
        for model, entries in per_model.items():
            bucket = _new_bucket()
            for entry in entries:
                _add_tokens(bucket, entry)
            cost += _price_bucket(bucket, model, rates)["total"]
        duration = (ep["last_ts"] - ep["start_ts"]
                    if ep["start_ts"] is not None and ep["last_ts"] is not None
                    else None)
        episodes.append({"start_ts": ep["start_ts"], "duration_s": duration,
                         "api_calls": ep["api_calls"],
                         "out_tokens": ep["out_tokens"], "cost": cost})
    tools = []
    for (tool, sidechain), row in sorted(state["tools"].items(),
                                         key=lambda kv: -kv[1]["total_s"]):
        tools.append({**row, "avg_s": (row["total_s"] / row["timed"]
                                       if row["timed"] else None)})
    wall_first, wall_last = state["wall"]
    return {
        "session": {"name": name, "sid": sid},
        "transcript": {"path": str(main_log),
                       "subagent_files": len(subagent_logs or [])},
        "prices": {
            "rates": {m: list(rates[m]) for m in sorted(rates)},
            "multipliers": {"cache_read": pricing.CACHE_READ_MULT,
                            "cache_write_5m": pricing.CACHE_WRITE_5M_MULT,
                            "cache_write_1h": pricing.CACHE_WRITE_1H_MULT},
            "source": ("built-in + dockwright.toml [pricing.rates] overrides"
                       if overridden else "built-in defaults"),
        },
        "wall_clock": {
            "seconds": (wall_last - wall_first
                        if wall_first is not None and wall_last is not None
                        else None),
            "first": wall_first, "last": wall_last,
        },
        "episode_count": len(state["episodes"]),
        "end_turn_count": state["end_turn_count"],
        "episodes": episodes,
        "api_calls": {"unique": state["main"]["api_calls"]
                                + state["sub"]["api_calls"],
                      "records": state["main"]["records"]
                                 + state["sub"]["records"]},
        "tokens": {
            "main": {**{k: state["main"][k] for k in _TOKEN_KEYS},
                     "api_calls": state["main"]["api_calls"],
                     "cost": money["main"]},
            "subagents": {**{k: state["sub"][k] for k in _TOKEN_KEYS},
                          "api_calls": state["sub"]["api_calls"],
                          "cost": money["subagents"]},
        },
        "money": {**money, "anatomy": anatomy, "by_model": by_model_rows},
        "tools": tools,
        "attribution": {
            "by_skill": _attr_rows(state["by_skill"], ("skill",), rates),
            "by_mcp": _attr_rows(state["by_mcp"], ("server", "tool"), rates),
        },
        "excluded_replayed": state["excluded_replayed"],
        "unknown_usage_fields": sorted(state["unknown_usage_fields"]),
        "server_tool_use": {key: total for key, total
                            in sorted(state["server_tool_use"].items())
                            if total},
    }


def _num(value, default=0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _recency(primary, started_at) -> tuple:
    return (primary, _num(started_at))


def _candidate_sources():
    from . import paths, state
    from .spend_ledger import read_events
    for record in state.list_json_in(paths.ACTIVE):
        if isinstance(record, dict) and record.get("claude_sid"):
            yield {"sid": record["claude_sid"], "name": record.get("name"),
                   "runtime": record.get("runtime") or "claude",
                   "started_at": record.get("started_at"),
                   "transcript_path": record.get("transcript_path"),
                   "date": _recency(float("inf"), record.get("started_at"))}
    for record in state.list_json_in(paths.CLOSED):
        if isinstance(record, dict) and record.get("claude_sid"):
            yield {"sid": record["claude_sid"], "name": record.get("name"),
                   "runtime": record.get("runtime") or "claude",
                   "started_at": record.get("started_at"),
                   "transcript_path": record.get("transcript_path"),
                   "date": _recency(_num(record.get("closed_at")),
                                    record.get("started_at"))}
    for event in read_events():
        if event.get("sid"):
            yield {"sid": event["sid"], "name": event.get("name"),
                   "runtime": event.get("runtime") or "claude",
                   "started_at": event.get("started_at"),
                   "transcript_path": None,
                   "date": _recency(_num(event.get("ts")),
                                    event.get("started_at"))}


def resolve_session(arg: str) -> dict | None:
    import sys as _sys
    from .transcript import find_session_log
    by_sid: dict = {}
    for cand in _candidate_sources():
        cur = by_sid.get(cand["sid"])
        if cur is None:
            by_sid[cand["sid"]] = dict(cand)
            continue
        newer, older = (cand, cur) if cand["date"] >= cur["date"] else (cur, cand)
        by_sid[cand["sid"]] = {
            **older, **{k: v for k, v in newer.items() if v is not None}}
    match = by_sid.get(arg)
    if match is None:
        named = sorted((c for c in by_sid.values() if c.get("name") == arg),
                       key=lambda c: c["date"], reverse=True)
        if named:
            match = named[0]
            if len(named) > 1:
                match = {**match, "ambiguous_sids": [c["sid"] for c in named]}
                others = ", ".join(f"{c['sid'][:8]}…" for c in named[1:])
                print(f"note: {len(named) - 1} other session(s) named "
                      f"{arg!r} — pass a sid for those: {others}",
                      file=_sys.stderr)
    if match is None:
        if find_session_log(arg) is None:
            return None
        match = {"sid": arg, "name": None, "runtime": "claude",
                 "started_at": None, "transcript_path": None}
    recorded = match.get("transcript_path")
    if isinstance(recorded, str) and recorded and Path(recorded).is_file():
        log_path = Path(recorded)
    else:
        log_path = find_session_log(match["sid"],
                                    runtime=match.get("runtime") or "claude")
    return {**match, "log": log_path}


def _fmt_duration(seconds) -> str:
    if seconds is None or seconds < 0:
        return "-"
    ms = int(round((seconds - int(seconds)) * 1000))
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}.{ms:03d}"


def _fmt_money(x) -> str:
    return f"${x:,.2f}"


def _fmt_tok(n) -> str:
    if n is None:
        return "-"
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def _rate_keys_used(report: dict) -> list:
    rates = report["prices"]["rates"]
    used = []
    for row in report["money"]["by_model"]:
        key = pricing.rate_key(row["model"], rates)
        if key is not None and key not in used:
            used.append(key)
    return used or sorted(rates)


_TOOL_COL = 44


def _render_text(report: dict) -> str:
    from datetime import datetime
    session = report["session"]
    wall = report["wall_clock"]
    label = session.get("name") or session.get("sid") or "?"
    sid8 = (session.get("sid") or "")[:8]
    header = f"Session spend — {label}" + (f" ({sid8}…)" if sid8 else "")
    if wall["first"] is not None:
        header += " · " + datetime.fromtimestamp(wall["first"]).strftime("%Y-%m-%d")
    lines = [header,
             f"transcript: {report['transcript']['path']}"
             + (f" (+{report['transcript']['subagent_files']} subagent transcript(s))"
                if report['transcript']['subagent_files'] else ""),
             ""]
    money = report["money"]
    lines.append(f"MONEY (USD)   TOTAL {_fmt_money(money['total'])}   "
                 f"main-loop {_fmt_money(money['main'])}   "
                 f"subagents {_fmt_money(money['subagents'])}")
    for row in money["by_model"]:
        flag = "" if row["priced"] else "  (unpriced)"
        lines.append(
            f"  {row['model']:<26} {_fmt_money(row['cost']):>10}   "
            f"out {_fmt_tok(row['out_tokens'])}  in {_fmt_tok(row['in_tokens'])}  "
            f"cache-rd {_fmt_tok(row['cache_read_tokens'])}  "
            f"cache-wr {_fmt_tok(row['cache_creation_5m_tokens'] + row['cache_creation_1h_tokens'])}  "
            f"[{row['api_calls']} calls]{flag}")
    anatomy = money["anatomy"]
    lines.append(f"  anatomy: cache-read {_fmt_money(anatomy['cache_read'])} / "
                 f"cache-write {_fmt_money(anatomy['cache_write'])} / "
                 f"output {_fmt_money(anatomy['output'])} / "
                 f"input {_fmt_money(anatomy['input'])}")
    rates = report["prices"]["rates"]
    lines.append("  prices: " + ", ".join(
        f"{key} in ${rates[key][0]:g}/M out ${rates[key][1]:g}/M"
        for key in _rate_keys_used(report)))
    lines.append(f"          (cache rd {report['prices']['multipliers']['cache_read']}x, "
                 f"wr 5m {report['prices']['multipliers']['cache_write_5m']}x / "
                 f"1h {report['prices']['multipliers']['cache_write_1h']}x) — "
                 f"{report['prices']['source']}")
    tokens = report["tokens"]

    def _tok_line(bucket):
        return (f"out {_fmt_tok(bucket['out_tokens'])} / in {_fmt_tok(bucket['in_tokens'])} / "
                f"cache-rd {_fmt_tok(bucket['cache_read_tokens'])} / "
                f"cache-wr {_fmt_tok(bucket['cache_creation_5m_tokens'] + bucket['cache_creation_1h_tokens'])}"
                f" = {_fmt_money(bucket['cost'])}")

    lines.append(f"TOKENS        main: {_tok_line(tokens['main'])}   "
                 f"subagents: {_tok_line(tokens['subagents'])}")
    first = (datetime.fromtimestamp(wall["first"]).strftime("%Y-%m-%d %H:%M:%S")
             if wall["first"] is not None else "-")
    last = (datetime.fromtimestamp(wall["last"]).strftime("%H:%M:%S")
            if wall["last"] is not None else "-")
    api_calls = report["api_calls"]
    api_calls_str = f"{api_calls['unique']} API calls"
    if api_calls["records"] != api_calls["unique"]:
        api_calls_str += f" ({api_calls['records']} records)"
    lines.append(f"WALL CLOCK    {_fmt_duration(wall['seconds'])}   ({first} → {last})   "
                 f"{report['episode_count']} prompt-episode(s) · "
                 f"{api_calls_str} · "
                 f"{sum(t['calls'] for t in report['tools'])} tool calls")
    if report["tools"]:
        lines.append(f"TOOLS         {'tool':<{_TOOL_COL}} {'calls':>5}  "
                     f"{'total-s':>7}  {'avg-s':>6}")
        for t in report["tools"]:
            tag = " (sub)" if t["sidechain"] else ""
            avg = f"{t['avg_s']:.2f}" if t["avg_s"] is not None else "-"
            lines.append(f"              {t['tool'] + tag:<{_TOOL_COL}} {t['calls']:>5}  "
                         f"{t['total_s']:>7.1f}  {avg:>6}")
        unpaired = sum(t["calls"] - t["timed"] for t in report["tools"])
        if unpaired:
            lines.append(f"note: {unpaired} tool call(s) unpaired or "
                         "un-timestamped — counted in calls, excluded from "
                         "total-s/avg-s")
    for title, rows, label_fn in (
            ("BY SKILL", report["attribution"]["by_skill"],
             lambda r: r["skill"]),
            ("BY MCP", report["attribution"]["by_mcp"],
             lambda r: f"{r['server']}/{r['tool']}")):
        if rows:
            lines.append(f"{title:<13} " + "  ".join(
                f"{label_fn(r)}: {r['api_calls']} calls, "
                f"out {_fmt_tok(r['out_tokens'])}, {_fmt_money(r['cost'])}"
                for r in rows[:8]))
    if report["episodes"]:
        lines.append("EPISODES      #  start     dur          api-calls  out-tok      $")
        for i, ep in enumerate(report["episodes"], 1):
            start = (datetime.fromtimestamp(ep["start_ts"]).strftime("%H:%M:%S")
                     if ep["start_ts"] is not None else "-")
            lines.append(f"              {i:<2} {start:<9} {_fmt_duration(ep['duration_s']):<12} "
                         f"{ep['api_calls']:>9}  {_fmt_tok(ep['out_tokens']):>7}  "
                         f"{_fmt_money(ep['cost']):>7}")
    notes = []
    if report["excluded_replayed"]:
        notes.append(f"{report['excluded_replayed']} replayed pre-session record(s) "
                     "excluded (--include-replayed to count them)")
    if report["unknown_usage_fields"]:
        notes.append("usage fields not accounted: "
                     + ", ".join(report["unknown_usage_fields"]))
    if money["unpriced_out_tokens"]:
        notes.append(f"{money['unpriced_out_tokens']:,} output tokens on unpriced "
                     "models — counted as $0")
    if report["server_tool_use"]:
        notes.append("server tool use not priced: " + ", ".join(
            f"{key}={total}" for key, total in report["server_tool_use"].items()))
    for note in notes:
        lines.append(f"note: {note}")
    return "\n".join(lines) + "\n"


def run(argv=None) -> int:
    import argparse
    import sys as _sys
    parser = argparse.ArgumentParser(
        prog="dockwright spend-report <session>",
        description="Per-session time + money report from the transcript.")
    parser.add_argument("session", nargs="?", default=None,
                        help="worker name or claude sid")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--include-replayed", action="store_true",
                        help="count replayed pre-session history (resume copies)")
    parser.add_argument("--transcript", type=str, default=None,
                        help="report an arbitrary transcript .jsonl directly")
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    if args.transcript:
        log = Path(args.transcript)
        if not log.is_file():
            print(f"transcript not found: {log}", file=_sys.stderr)
            return 2
        resolved = {"sid": log.stem, "name": args.session, "runtime": "claude",
                    "started_at": None, "log": log}
    else:
        if not args.session:
            print("session (worker name or sid) required", file=_sys.stderr)
            return 2
        resolved = resolve_session(args.session)
        if resolved is None:
            print(f"session not found: {args.session!r} (not in active/, "
                  "closed/, the spend ledger, or ~/.claude/projects)",
                  file=_sys.stderr)
            return 2
    if (resolved.get("runtime") or "claude") == "codex":
        note = "codex transcripts carry no usage data"
        if args.as_json:
            print(json.dumps({"session": {"name": resolved.get("name"),
                                          "sid": resolved.get("sid")},
                              "runtime": "codex", "note": note},
                             indent=2, default=str))
        else:
            print(f"{note} — no spend report is derivable for this session")
        return 0
    log = resolved.get("log")
    if log is None:
        print(f"transcript not found for {args.session!r} (pruned?) — "
              "the report needs the transcript file", file=_sys.stderr)
        return 2
    report = build_report(
        log,
        started_at=resolved.get("started_at"),
        include_replayed=args.include_replayed,
        name=resolved.get("name"), sid=resolved.get("sid"))
    if resolved.get("ambiguous_sids"):
        report["resolution"] = {"picked_sid": resolved.get("sid"),
                                "ambiguous_sids": resolved["ambiguous_sids"]}
    if args.as_json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _sys.stdout.write(_render_text(report))
    return 0
