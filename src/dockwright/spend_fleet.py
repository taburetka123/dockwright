import argparse
import json
import os
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from multiprocessing import Pool
from pathlib import Path

from . import config, paths, pricing, state
from .spend_session import _content_chars, _tool_results, _tool_use_blocks
from .transcript import _parse_event_ts, subagent_logs_for, usage_totals_of

TOKEN_KEYS = ("in", "out", "cache_read", "cache_5m", "cache_1h")
USD_PARTS = ("input", "output", "cache_read", "cache_write")
GAP_THRESHOLD_S = 300
BASH_LABEL_CHARS = 50
FIRST_USER_CHARS = 300
BOOT_KEYS = ("sysprompt_chars", "role_marker", "skill_listing_chars",
             "deferred_tool_names", "mcp_servers", "mcp_instr_chars")
CHILD_CHAIN_DEPTH = 4

_RATES = None


def _rates():
    global _RATES
    if _RATES is None:
        _RATES = pricing.get_rates()
    return _RATES


def window_bounds(since: date, until: date) -> tuple[float, float]:
    return (datetime(since.year, since.month, since.day).astimezone().timestamp(),
            datetime(until.year, until.month, until.day).astimezone().timestamp())


def transcript_roots() -> list[Path]:
    candidates = [Path(os.environ.get("HOME", "")) / ".claude" / "projects"]
    for letter in config.account_names():
        candidates.append(paths.account_config_dir(letter) / "projects")
    out: list[Path] = []
    seen: set[str] = set()
    for root in candidates:
        if not root.is_dir():
            continue
        real = os.path.realpath(root)
        if real in seen:
            continue
        seen.add(real)
        out.append(root)
    return out


def list_main_files(roots: list[Path], since_ts: float) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        try:
            projects = [p for p in root.iterdir() if p.is_dir()]
        except OSError:
            continue
        for project in projects:
            try:
                files = [p for p in project.iterdir() if p.is_file() and p.suffix == ".jsonl"]
            except OSError:
                continue
            for path in files:
                try:
                    if path.stat().st_mtime >= since_ts:
                        out.append(path)
                except OSError:
                    continue
    return sorted(out)


def tool_label(block: dict) -> str:
    name = block.get("name")
    if not isinstance(name, str) or not name:
        return "(unknown)"
    inp = block.get("input")
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        command = inp.get("command")
        head = command.split("\n", 1)[0].strip() if isinstance(command, str) else ""
        return f"Bash:{head[:BASH_LABEL_CHARS]}"
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return f"mcp:{parts[1]}:{parts[2]}"
        return f"mcp:{name[5:]}"
    if name == "Agent":
        return f"Agent:{inp.get('subagent_type')}:{inp.get('model')}"
    if name == "Read":
        file_path = inp.get("file_path")
        if isinstance(file_path, str) and file_path:
            return f"Read:{file_path}"
        return "Read"
    return name


def _local_day(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().date().isoformat()


def _sid_of(path: Path, sub: bool) -> str:
    return path.parent.parent.name if sub else path.stem


def _text_of(content) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                return block["text"]
    return None


def _note_boot(boot: dict, attachment: dict) -> None:
    kind = attachment.get("type")
    if kind == "prompt_snapshot" and boot["sysprompt_chars"] is None:
        prompt = attachment.get("systemPrompt") or ""
        if not isinstance(prompt, str):
            prompt = json.dumps(prompt)
        boot["sysprompt_chars"] = len(prompt)
        at = prompt.find("dockwright-role:")
        boot["role_marker"] = prompt[at:at + 60].split("-->")[0].strip() if at >= 0 else None
    elif kind == "skill_listing" and boot["skill_listing_chars"] is None:
        content = attachment.get("content")
        boot["skill_listing_chars"] = len(content) if isinstance(content, str) else 0
    elif kind == "deferred_tools_delta":
        names = [n for n in (attachment.get("addedNames") or []) if isinstance(n, str)]
        boot["deferred_tool_names"] = (boot["deferred_tool_names"] or 0) + len(names)
        servers = set(boot["mcp_servers"] or [])
        for name in names:
            if name.startswith("mcp__"):
                parts = name.split("__", 2)
                if len(parts) == 3:
                    servers.add(parts[1])
        boot["mcp_servers"] = sorted(servers)
    elif kind == "mcp_instructions_delta":
        blocks = attachment.get("addedBlocks") or []
        boot["mcp_instr_chars"] = (boot["mcp_instr_chars"] or 0) + len(json.dumps(blocks))


def _new_row(sid, agent, path, ts, ts_s, record, entry) -> dict:
    model = entry["model"]
    tokens = {"in": entry["in_tokens"], "out": entry["out_tokens"],
              "cache_read": entry["cache_read_tokens"],
              "cache_5m": entry["cache_creation_5m_tokens"],
              "cache_1h": entry["cache_creation_1h_tokens"]}
    usd = pricing.cost_breakdown(
        model, rates=_rates(), output_tokens=tokens["out"], input_tokens=tokens["in"],
        cache_read_tokens=tokens["cache_read"], cache_creation_5m_tokens=tokens["cache_5m"],
        cache_creation_1h_tokens=tokens["cache_1h"])
    effort = record.get("effort") or record.get("perTurnEffort")
    return {
        "ts": ts_s, "day": _local_day(ts) if ts is not None else None,
        "sid": sid, "agent": agent, "path": path, "model": model,
        "effort": effort if isinstance(effort, str) else None,
        **tokens, "ctx": tokens["in"] + tokens["cache_read"] + tokens["cache_5m"] + tokens["cache_1h"],
        "usd": {k: usd[k] for k in (*USD_PARTS, "total")}, "priced": usd["priced"],
        "tools": [], "result_chars": 0, "gap_s": 0,
        "_ts": ts, "_tool_ids": set(),
    }


def scan_transcript(path: str, since_ts: float, until_ts: float, sub: bool = False) -> dict | None:
    p = Path(path)
    try:
        raw = p.read_text(errors="replace")
    except OSError:
        return None
    sid = _sid_of(p, sub)
    meta = {"path": path, "sub": sub, "sid": sid, "agent": None, "cwd": None, "version": None,
            "entrypoint": None,
            "first_user": None, "first_user_ts": None, "compactions": 0,
            "tool_calls": Counter(), "result_chars": Counter(),
            "calls_all": 0, "tokens_all": {k: 0 for k in TOKEN_KEYS},
            "boot": {k: None for k in BOOT_KEYS}, "gaps": []}
    rows: dict[str, dict] = {}
    order: list[str] = []
    row_of_tool: dict[str, str] = {}
    for record in _iter_records_text(raw):
        kind = record.get("type")
        ts_s = record.get("timestamp")
        ts = _parse_event_ts(ts_s)
        in_window = ts is not None and since_ts <= ts < until_ts
        if kind == "attachment":
            attachment = record.get("attachment")
            if isinstance(attachment, dict):
                _note_boot(meta["boot"], attachment)
            continue
        if kind == "system":
            if record.get("subtype") == "compact_boundary" and in_window:
                meta["compactions"] += 1
            continue
        if kind == "user":
            content = (record.get("message") or {}).get("content")
            if meta["first_user"] is None and record.get("isMeta") is not True:
                text = _text_of(content)
                if text:
                    meta["first_user"] = text[:FIRST_USER_CHARS]
                    meta["first_user_ts"] = ts_s
            for tool_use_id, result in _tool_results(record):
                mid = row_of_tool.get(tool_use_id)
                if mid is None:
                    continue
                row = rows[mid]
                chars = _content_chars(result)
                row["result_chars"] += chars
                if row["_in_window"]:
                    meta["result_chars"][row["_label_of"][tool_use_id]] += chars
            continue
        if kind != "assistant":
            continue
        if meta["cwd"] is None and isinstance(record.get("cwd"), str):
            meta["cwd"] = record["cwd"]
        if meta["version"] is None and isinstance(record.get("version"), str):
            meta["version"] = record["version"]
        if meta["entrypoint"] is None and isinstance(record.get("entrypoint"), str):
            meta["entrypoint"] = record["entrypoint"]
        if sub and meta["agent"] is None and isinstance(record.get("agentId"), str):
            meta["agent"] = record["agentId"]
        entry = usage_totals_of(record)
        if entry is None or entry["model"] == "<synthetic>":
            continue
        mid = entry["message_id"]
        row = rows.get(mid)
        if row is None:
            meta["calls_all"] += 1
            for key, src in zip(TOKEN_KEYS, ("in_tokens", "out_tokens", "cache_read_tokens",
                                             "cache_creation_5m_tokens", "cache_creation_1h_tokens")):
                meta["tokens_all"][key] += entry[src]
            row = _new_row(sid, meta["agent"], path, ts, ts_s, record, entry)
            row["_in_window"] = in_window
            row["_label_of"] = {}
            rows[mid] = row
            order.append(mid)
        for block in _tool_use_blocks(record):
            tool_use_id = block.get("id")
            if tool_use_id in row["_tool_ids"]:
                continue
            label = tool_label(block)
            row["tools"].append(label)
            row["_label_of"][tool_use_id] = label
            row["_tool_ids"].add(tool_use_id)
            row_of_tool[tool_use_id] = mid
            if row["_in_window"]:
                meta["tool_calls"][label] += 1
    if sub and meta["agent"] is None:
        meta["agent"] = p.stem[len("agent-"):] if p.stem.startswith("agent-") else p.stem
    out_rows = []
    last_ts = None
    for mid in order:
        row = rows[mid]
        ts = row.pop("_ts")
        row.pop("_tool_ids")
        row.pop("_label_of")
        in_window = row.pop("_in_window")
        if ts is None:
            continue
        if last_ts is not None:
            row["gap_s"] = int(round(ts - last_ts))
        if in_window:
            row["agent"] = meta["agent"]
            if last_ts is not None and row["gap_s"] > GAP_THRESHOLD_S:
                meta["gaps"].append([row["gap_s"], row["cache_5m"] + row["cache_1h"], row["ctx"]])
            out_rows.append(row)
        last_ts = ts
    meta["tool_calls"] = dict(meta["tool_calls"])
    meta["result_chars"] = dict(meta["result_chars"])
    return {"meta": meta, "rows": out_rows}


def _iter_records_text(raw: str):
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            yield record


def scan_transcript_star(args: tuple) -> dict | None:
    return scan_transcript(*args)


def _read_events(events_path: Path) -> list[dict]:
    if not events_path.is_file():
        return []
    out = []
    try:
        lines = events_path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def load_labels() -> dict:
    assignments = {}
    for record in state.list_json_in(paths.ASSIGNMENTS):
        if isinstance(record, dict) and isinstance(record.get("claude_sid"), str):
            assignments[record["claude_sid"]] = record
    records = {}
    for directory in (paths.CLOSED, paths.ACTIVE):
        for record in state.list_json_in(directory):
            if isinstance(record, dict) and isinstance(record.get("claude_sid"), str):
                records[record["claude_sid"]] = record
    node_of: dict = {}
    child_of: dict = {}
    rounds: Counter = Counter()
    event_role: dict = {}
    for store in (paths.ARTIFACTS, paths.ARTIFACTS.parent / "artifacts-archive"):
        if not store.is_dir():
            continue
        try:
            works = sorted(p for p in store.iterdir() if p.is_dir())
        except OSError:
            continue
        for work in works:
            for event in _read_events(work / "events.jsonl"):
                kind = event.get("type")
                if kind == "advance_committed" and isinstance(event.get("sid"), str):
                    node_of[event["sid"]] = (work.name, event.get("node"), event.get("index"))
                elif kind == "child_round":
                    legs = event.get("legs") or []
                    providers = event.get("providers") or []
                    for i, sid in enumerate(event.get("sids") or []):
                        if not isinstance(sid, str) or not sid:
                            continue
                        child_of[sid] = (work.name, event.get("node"), event.get("slot"),
                                         event.get("lane"),
                                         legs[i] if i < len(legs) else None,
                                         providers[i] if i < len(providers) else None)
                        rounds[sid] += 1
                        if isinstance(event.get("role"), str):
                            event_role[sid] = event["role"]
    return {"assignments": assignments, "records": records, "node_of": node_of,
            "child_of": child_of, "rounds": dict(rounds), "event_role": event_role}


def _extra_arg(assignment: dict, flag: str):
    args = assignment.get("spawn_extra_args") or []
    if not isinstance(args, list) or flag not in args:
        return None
    at = args.index(flag) + 1
    return args[at] if at < len(args) and isinstance(args[at], str) else None


def _find_node(sid, labels: dict, depth: int = 0):
    if sid in labels["node_of"]:
        return labels["node_of"][sid]
    assignment = labels["assignments"].get(sid)
    parent = assignment.get("parent_sid") if assignment else None
    if isinstance(parent, str) and parent and depth < CHILD_CHAIN_DEPTH:
        return _find_node(parent, labels, depth + 1)
    return None


def _under(path, root) -> bool:
    if not isinstance(path, str) or not path:
        return False
    real = os.path.realpath(path)
    root = os.path.realpath(str(root))
    return real == root or real.startswith(root.rstrip("/") + "/")


def classify(sid: str, meta: dict, labels: dict) -> dict:
    assignment = labels["assignments"].get(sid) or {}
    record = labels["records"].get(sid) or {}
    role_file = _extra_arg(assignment, "--append-system-prompt-file")
    out = {
        "kind": None, "work": assignment.get("ticket"), "node": None, "index": None,
        "child_slot": None, "child_lane": None, "child_rounds": None, "child_role": None,
        "context_role": assignment.get("context_role"),
        "spawn_model": _extra_arg(assignment, "--model"), "spawn_effort": _extra_arg(assignment, "--effort"),
        "role_file": role_file.split("/roles/")[-1] if role_file else None,
        "parent_name": assignment.get("parent_name"),
        "name": assignment.get("name") or record.get("name"),
        "account": record.get("account"), "record_kind": record.get("kind"),
        "closed_reason": record.get("closed_reason"), "provider": record.get("provider"),
        "record_spend": record.get("spend") if isinstance(record.get("spend"), dict) else None,
    }
    cwd = meta.get("cwd")
    if sid in labels["node_of"]:
        out["work"], out["node"], out["index"] = labels["node_of"][sid]
        out["kind"] = "node"
    elif sid in labels["child_of"]:
        work, node, slot, lane, _leg, provider = labels["child_of"][sid]
        out.update(kind="child", work=work, node=node, child_slot=slot, child_lane=lane,
                   child_rounds=labels["rounds"].get(sid, 0))
        out["provider"] = out["provider"] or provider
    elif assignment and _find_node(assignment.get("parent_sid"), labels) is not None:
        work, node, _index = _find_node(assignment.get("parent_sid"), labels)
        out.update(kind="child", work=work, node=node)
    elif record.get("kind") == "manager" or (
            meta.get("entrypoint") != "sdk-cli"
            and _under(cwd, config.manager_home_default().expanduser())):
        out["kind"] = "manager"
    elif assignment:
        parent = assignment.get("parent_sid")
        out["kind"] = "child-nonpipe" if parent in labels["assignments"] else "worker"
    elif _under(cwd, tempfile.gettempdir()) or _under(cwd, "/tmp"):
        out["kind"] = "headless"
    else:
        out["kind"] = "unregistered"
    if out["kind"] == "child":
        out["child_role"] = (assignment.get("role") or assignment.get("context_role")
                             or labels["event_role"].get(sid))
    return out


def _family(model) -> str:
    return pricing.normalize_model(model) or str(model)


def build_sessions(scans: list[dict], labels: dict) -> list[dict]:
    by_sid: dict[str, list[dict]] = defaultdict(list)
    for scan in sorted(scans, key=lambda s: (s["meta"]["sid"], s["meta"]["path"])):
        by_sid[scan["meta"]["sid"]].append(scan)
    sessions = []
    for sid, files in by_sid.items():
        mains = [f for f in files if not f["meta"]["sub"]]
        subs = [f for f in files if f["meta"]["sub"]]
        base = (mains or subs)[0]["meta"]
        session = {"sid": sid, "paths": [f["meta"]["path"] for f in files],
                   **classify(sid, base, labels),
                   "cwd": base["cwd"], "first_user": base["first_user"], "version": base["version"],
                   "entrypoint": base["entrypoint"],
                   "calls": 0, "calls_sub": 0,
                   "main_calls_all": sum(f["meta"]["calls_all"] for f in mains) if mains else None,
                   "tokens_all": {k: sum(f["meta"]["tokens_all"][k] for f in files) for k in TOKEN_KEYS},
                   "usd": 0.0, "usd_sub": 0.0, "usd_parts": {k: 0.0 for k in USD_PARTS},
                   "tokens_by_family": {}, "by_day": {}, "first_ctx": None, "peak_ctx": None,
                   "mean_ctx": None, "last_ctx": None, "efforts": {}, "days": [], "compactions": 0,
                   "tool_calls": {}, "result_chars": {}, "boot": base["boot"], "subagents": [], "gaps": []}
        ctxs = []
        efforts: Counter = Counter()
        days: set = set()
        tool_calls: Counter = Counter()
        result_chars: Counter = Counter()
        for scan in files:
            meta = scan["meta"]
            rows = scan["rows"]
            file_usd = 0.0
            for row in rows:
                total = row["usd"]["total"]
                file_usd += total
                for part in USD_PARTS:
                    session["usd_parts"][part] += row["usd"][part]
                fam = session["tokens_by_family"].setdefault(_family(row["model"]), {k: 0 for k in TOKEN_KEYS})
                for key in TOKEN_KEYS:
                    fam[key] += row[key]
                session["by_day"][row["day"]] = session["by_day"].get(row["day"], 0.0) + total
                days.add(row["day"])
                efforts[row["effort"] or "-"] += 1
            session["usd"] += file_usd
            session["compactions"] += meta["compactions"]
            tool_calls.update(meta["tool_calls"])
            result_chars.update(meta["result_chars"])
            if meta["sub"]:
                session["calls_sub"] += len(rows)
                session["usd_sub"] += file_usd
                sub_ctxs = [row["ctx"] for row in rows]
                session["subagents"].append({
                    "agent": meta["agent"], "usd": file_usd, "calls": len(rows),
                    "model": rows[0]["model"] if rows else None,
                    "first_ctx": sub_ctxs[0] if sub_ctxs else None,
                    "peak_ctx": max(sub_ctxs) if sub_ctxs else None,
                    "first_user": meta["first_user"]})
            else:
                session["calls"] += len(rows)
                ctxs.extend(row["ctx"] for row in rows)
                session["gaps"].extend(meta["gaps"])
        if ctxs:
            session.update(first_ctx=ctxs[0], peak_ctx=max(ctxs), mean_ctx=sum(ctxs) / len(ctxs),
                           last_ctx=ctxs[-1])
        session["efforts"] = dict(efforts)
        session["days"] = sorted(days)
        session["tool_calls"] = dict(tool_calls)
        session["result_chars"] = dict(result_chars)
        sessions.append(session)
    return sessions


def cross_check(sessions: list[dict]) -> dict:
    out = {"compared": 0, "exact": 0, "record_lags": 0, "scan_short": 0, "worst": None}
    worst_key = None
    for session in sorted(sessions, key=lambda s: s["sid"]):
        spend = session.get("record_spend")
        if not isinstance(spend, dict) or "model_requests" not in spend or session["main_calls_all"] is None:
            continue
        tokens = session["tokens_all"]
        diff = {
            "calls": session["main_calls_all"] - _int(spend.get("model_requests")),
            "out_tokens": tokens["out"] - _int(spend.get("out_tokens")),
            "cache_read_tokens": tokens["cache_read"] - _int(spend.get("cache_read_tokens")),
            "cache_creation_tokens": tokens["cache_5m"] + tokens["cache_1h"] - _int(spend.get("cache_creation_tokens")),
        }
        out["compared"] += 1
        if all(v == 0 for v in diff.values()):
            out["exact"] += 1
        elif any(v < 0 for v in diff.values()):
            out["scan_short"] += 1
            magnitude = sum(abs(v) for v in diff.values())
            if worst_key is None or magnitude > worst_key:
                worst_key = magnitude
                out["worst"] = {"sid": session["sid"], "diff": diff}
        else:
            out["record_lags"] += 1
    return out


def _int(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def summarize(sessions: list[dict], rows: list[dict], *, window: dict, skipped_files: int,
              files: int = 0) -> dict:
    by_day: dict = {}
    by_model_effort: dict = {}
    usd_parts = {k: 0.0 for k in USD_PARTS}
    unpriced: set = set()
    total = 0.0
    for row in rows:
        total += row["usd"]["total"]
        for part in USD_PARTS:
            usd_parts[part] += row["usd"][part]
        day = by_day.setdefault(row["day"], {"usd": 0.0, "by_family": {}})
        day["usd"] += row["usd"]["total"]
        fam = day["by_family"].setdefault(_family(row["model"]), {"usd": 0.0, "tokens": {k: 0 for k in TOKEN_KEYS}})
        fam["usd"] += row["usd"]["total"]
        for key in TOKEN_KEYS:
            fam["tokens"][key] += row[key]
        me = by_model_effort.setdefault(f"{row['model']}/{row['effort'] or '-'}", {"calls": 0, "usd": 0.0})
        me["calls"] += 1
        me["usd"] += row["usd"]["total"]
        if not row["priced"]:
            unpriced.add(str(row["model"]))
    by_kind: dict = {}
    by_node: dict = {}
    for session in sessions:
        calls = session["calls"] + session["calls_sub"]
        kind = by_kind.setdefault(session["kind"], {"sessions": 0, "usd": 0.0, "calls": 0})
        kind["sessions"] += 1
        kind["usd"] += session["usd"]
        kind["calls"] += calls
        if session["node"] is not None:
            node = by_node.setdefault(session["node"], {"sessions": 0, "usd": 0.0, "calls": 0})
            node["sessions"] += 1
            node["usd"] += session["usd"]
            node["calls"] += calls
    return {
        "window": window, "files": files, "skipped_files": skipped_files,
        "sessions": len(sessions), "calls": len(rows), "usd": total, "usd_parts": usd_parts,
        "by_day": dict(sorted(by_day.items())),
        "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1]["usd"])),
        "by_node": dict(sorted(by_node.items(), key=lambda kv: -kv[1]["usd"])),
        "by_model_effort": dict(sorted(by_model_effort.items(), key=lambda kv: -kv[1]["usd"])),
        "cross_check": cross_check(sessions),
        "unpriced_models": sorted(unpriced),
    }


def render_text(summary: dict) -> str:
    w = summary["window"]
    cc = summary["cross_check"]
    lines = [
        f"Fleet spend — {w['since']} → {w['until']} (exclusive, {w['tz']}): "
        f"{summary['files']} file(s), {summary['sessions']} session(s), {summary['calls']} call(s), "
        f"${summary['usd']:.1f}" + (f", {summary['skipped_files']} skipped" if summary["skipped_files"] else ""),
        f"cross-check: compared {cc['compared']}, exact {cc['exact']}, record_lags {cc['record_lags']}, "
        f"scan_short: {cc['scan_short']}" + (f" (worst {cc['worst']['sid']} {cc['worst']['diff']})" if cc["worst"] else ""),
        "",
        f"  {'day':<12} {'$':>8}  by family",
    ]
    for day, entry in summary["by_day"].items():
        fams = "  ".join(f"{fam} ${v['usd']:.1f}" for fam, v in sorted(entry["by_family"].items(), key=lambda kv: -kv[1]["usd"]))
        lines.append(f"  {day:<12} {entry['usd']:>8.1f}  {fams}")
    lines += ["", f"  {'kind':<16} {'sess':>5} {'calls':>7} {'$':>8}"]
    for kind, entry in summary["by_kind"].items():
        lines.append(f"  {str(kind):<16} {entry['sessions']:>5} {entry['calls']:>7} {entry['usd']:>8.1f}")
    lines += ["", f"  {'node':<16} {'sess':>5} {'calls':>7} {'$':>8}"]
    for node, entry in summary["by_node"].items():
        lines.append(f"  {str(node):<16} {entry['sessions']:>5} {entry['calls']:>7} {entry['usd']:>8.1f}")
    lines += ["", f"  {'model/effort':<34} {'calls':>7} {'$':>8}"]
    for key, entry in summary["by_model_effort"].items():
        lines.append(f"  {key:<34} {entry['calls']:>7} {entry['usd']:>8.1f}")
    if summary["unpriced_models"]:
        lines += ["", "  unpriced models (counted as $0): " + ", ".join(summary["unpriced_models"])]
    return "\n".join(lines) + "\n"


def build(since: date, until: date, *, jobs: int, roots: list[Path] | None = None) -> dict:
    since_ts, until_ts = window_bounds(since, until)
    labels = load_labels()
    files = list_main_files(roots if roots is not None else transcript_roots(), since_ts)
    work = []
    for main in files:
        work.append((str(main), since_ts, until_ts, False))
        for sidecar in subagent_logs_for(main):
            work.append((str(sidecar), since_ts, until_ts, True))
    if jobs <= 1 or len(work) <= 1:
        results = [scan_transcript_star(args) for args in work]
    else:
        with Pool(jobs) as pool:
            results = list(pool.imap_unordered(scan_transcript_star, work, chunksize=2))
    skipped = sum(1 for r in results if r is None)
    scans = sorted((r for r in results if r is not None), key=lambda r: (r["meta"]["sid"], r["meta"]["path"]))
    live_sids = {s["meta"]["sid"] for s in scans if s["rows"]}
    scans = [s for s in scans if s["meta"]["sid"] in live_sids]
    sessions = build_sessions(scans, labels)
    rows = [row for scan in scans for row in scan["rows"]]
    window = {"since": since.isoformat(), "until": until.isoformat(),
              "tz": str(datetime.now().astimezone().tzinfo)}
    summary = summarize(sessions, rows, window=window, skipped_files=skipped, files=len(files))
    return {"summary": summary, "rows": rows, "sessions": sessions}


def _write_jsonl(path: Path, items: list[dict]) -> None:
    with open(path, "w") as f:
        for item in items:
            f.write(json.dumps(item, default=str) + "\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="dockwright spend-fleet",
        description="Per-call fleet spend over a local-day window: one row per API call priced by "
                    "pricing.py, one row per session labelled from dockwright records, and a summary "
                    "cross-checked against the closed records' spend.")
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--days", type=int, default=None,
                        help="window size in days ending before --until (default 7)")
    window.add_argument("--since", type=str, default=None, help="window start YYYY-MM-DD, inclusive")
    parser.add_argument("--until", type=str, default=None,
                        help="window end YYYY-MM-DD, exclusive (default tomorrow)")
    parser.add_argument("--out", type=str, default=None,
                        help="directory for calls.jsonl, sessions.jsonl and summary.json")
    parser.add_argument("--json", action="store_true", dest="as_json", help="print summary.json")
    parser.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1),
                        help="worker processes (1 runs in-process)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    if args.days is not None and args.days < 1:
        print(f"--days must be at least 1, got {args.days}", file=sys.stderr)
        return 2
    days = 7 if args.days is None else args.days
    try:
        until = date.fromisoformat(args.until) if args.until else date.today() + timedelta(days=1)
        since = date.fromisoformat(args.since) if args.since else until - timedelta(days=days)
    except ValueError as e:
        print(f"invalid date: {e} (want YYYY-MM-DD)", file=sys.stderr)
        return 2
    if since > until:
        print(f"--since {since} is after --until {until}", file=sys.stderr)
        return 2
    out_dir = None
    if args.out:
        out_dir = Path(args.out)
        if out_dir.exists() and not out_dir.is_dir():
            print(f"--out {out_dir} exists and is not a directory", file=sys.stderr)
            return 2
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            print(f"--out {out_dir}: {e}", file=sys.stderr)
            return 2
    result = build(since, until, jobs=max(1, args.jobs))
    summary = result["summary"]
    if out_dir is not None:
        _write_jsonl(out_dir / "calls.jsonl", result["rows"])
        _write_jsonl(out_dir / "sessions.jsonl", result["sessions"])
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    if args.as_json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        sys.stdout.write(render_text(summary))
    return 0
