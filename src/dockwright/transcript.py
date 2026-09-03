import json
import os
from pathlib import Path
from typing import Tuple

def _find_claude_session_log(session_id: str) -> Path | None:
    projects_root = Path(os.environ.get("HOME", "")) / ".claude" / "projects"
    if not projects_root.is_dir():
        return None
    for project_dir in projects_root.iterdir():
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.is_file():
            return candidate
    return None


def _find_codex_session_log(session_id: str) -> Path | None:
    sessions_root = Path(os.environ.get("HOME", "")) / ".codex" / "sessions"
    if not sessions_root.is_dir():
        return None
    matches = sorted(
        sessions_root.rglob(f"rollout-*-{session_id}.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def find_session_log(session_id: str, runtime: str = "claude") -> Path | None:
    if runtime == "codex":
        return _find_codex_session_log(session_id)
    return _find_claude_session_log(session_id)


def latest_subagent_mtime(session_log: Path, session_id: str) -> float:
    try:
        subagents_dir = session_log.parent / session_id / "subagents"
        newest = 0.0
        for entry in subagents_dir.glob("agent-*.jsonl"):
            try:
                newest = max(newest, entry.stat().st_mtime)
            except OSError:
                continue
        return newest
    except OSError:
        return 0.0


DELEGATION_FRESH_SEC = 120

TURN_END_GRACE_ENV = "CLAUDE_ORCH_TURN_END_GRACE_SEC"

EPISODE_GRACE_SEC_DEFAULT = 900
EPISODE_GRACE_ENV = "CLAUDE_ORCH_EPISODE_GRACE_SEC"


def delegation_fresh_sec() -> int:
    try:
        value = int(os.environ.get(TURN_END_GRACE_ENV, str(DELEGATION_FRESH_SEC)))
    except ValueError:
        return DELEGATION_FRESH_SEC
    return value if value >= 0 else DELEGATION_FRESH_SEC


def episode_grace_sec() -> int:
    raw = os.environ.get(EPISODE_GRACE_ENV, "")
    try:
        value = int(raw)
    except ValueError:
        value = EPISODE_GRACE_SEC_DEFAULT
    if value <= 0:
        value = EPISODE_GRACE_SEC_DEFAULT
    return max(value, delegation_fresh_sec())


def is_delegating(record: dict, now: float, log: Path | None = None,
                  fresh_sec: float | None = None) -> bool:
    try:
        if fresh_sec is None:
            fresh_sec = episode_grace_sec()
        if (record.get("runtime") or "claude") != "claude":
            return False
        sid = record.get("claude_sid")
        if not sid:
            return False
        if log is None:
            log = find_session_log(sid)
        if log is None:
            return False
        latest = latest_subagent_mtime(log, sid)
        if latest <= 0:
            return False
        return latest > log.stat().st_mtime and now - latest < fresh_sec
    except OSError:
        return False


def _assistant_text(event: dict) -> tuple[str | None, str | None]:
    if event.get("type") == "assistant":
        content = event.get("message", {}).get("content", [])
        text_parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        return (" ".join(text_parts).strip() or None, event.get("timestamp"))

    payload = event.get("payload") or {}
    if event.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "assistant":
        content = payload.get("content", [])
        text_parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "output_text"]
        return (" ".join(text_parts).strip() or None, event.get("timestamp"))

    return (None, None)


def _int_field(mapping: dict, key: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _usage_entry(line: str, seen_ids: set) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    entry = usage_totals_of(event)
    if entry is None or entry["message_id"] in seen_ids:
        return None
    seen_ids.add(entry["message_id"])
    return {
        "message_id": entry["message_id"],
        "output_tokens": entry["out_tokens"],
        "input_tokens": entry["in_tokens"],
        "cache_read_tokens": entry["cache_read_tokens"],
        "cache_creation_tokens": entry["cache_creation_flat"],
    }


def sum_usage(log_path: Path) -> dict:
    totals = {"out_tokens": 0, "in_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0}
    seen_ids: set[str] = set()
    try:
        with open(log_path, "r", errors="replace") as f:
            for line in f:
                entry = _usage_entry(line, seen_ids)
                if entry is None:
                    continue
                totals["out_tokens"] += entry["output_tokens"]
                totals["in_tokens"] += entry["input_tokens"]
                totals["cache_read_tokens"] += entry["cache_read_tokens"]
                totals["cache_creation_tokens"] += entry["cache_creation_tokens"]
    except OSError:
        pass
    return totals


_SPEND_TOTAL_KEYS = ("out_tokens", "in_tokens", "cache_read_tokens", "cache_creation_tokens")


def _parse_event_ts(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def subagent_logs_for(log_path: Path, sid: str | None = None) -> list:
    try:
        base = log_path.parent / (sid or log_path.stem) / "subagents"
        return sorted(base.glob("agent-*.jsonl"))
    except OSError:
        return []


def recount_spend(log_path: Path, prior_spend: dict | None,
                  started_at: float | None = None,
                  subagent_logs: list | None = None) -> dict | None:
    try:
        with open(log_path, "rb") as f:
            raw = f.read()
    except OSError:
        return prior_spend
    raws = [raw]
    if subagent_logs is None:
        subagent_logs = subagent_logs_for(log_path)
    for sidecar in subagent_logs:
        try:
            raws.append(Path(sidecar).read_bytes())
        except OSError:
            continue
    totals = {key: 0 for key in _SPEND_TOTAL_KEYS}
    by_model: dict = {}
    seen_ids: set[str] = set()
    check_birth = isinstance(started_at, (int, float)) and started_at > 0
    for line in (line for file_raw in raws
                 for line in file_raw.decode("utf-8", errors="replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        if check_birth:
            event_ts = _parse_event_ts(event.get("timestamp"))
            if event_ts is not None and event_ts < started_at:
                continue
        entry = usage_totals_of(event)
        if entry is None or entry["message_id"] in seen_ids:
            continue
        seen_ids.add(entry["message_id"])
        totals["out_tokens"] += entry["out_tokens"]
        totals["in_tokens"] += entry["in_tokens"]
        totals["cache_read_tokens"] += entry["cache_read_tokens"]
        totals["cache_creation_tokens"] += (entry["cache_creation_5m_tokens"]
                                            + entry["cache_creation_1h_tokens"])
        if entry["model"] is not None:
            bucket = by_model.setdefault(entry["model"], {
                "out_tokens": 0, "in_tokens": 0, "cache_read_tokens": 0,
                "cache_creation_5m_tokens": 0, "cache_creation_1h_tokens": 0})
            bucket["out_tokens"] += entry["out_tokens"]
            bucket["in_tokens"] += entry["in_tokens"]
            bucket["cache_read_tokens"] += entry["cache_read_tokens"]
            bucket["cache_creation_5m_tokens"] += entry["cache_creation_5m_tokens"]
            bucket["cache_creation_1h_tokens"] += entry["cache_creation_1h_tokens"]
    if not seen_ids:
        return prior_spend
    prior = prior_spend if isinstance(prior_spend, dict) else {}
    prior_totals = {key: _int_field(prior, key) for key in _SPEND_TOTAL_KEYS}
    if totals == prior_totals:
        return prior_spend
    return {
        "turns": _int_field(prior, "turns") + 1,
        **totals,
        "last_turn_out": max(0, totals["out_tokens"] - prior_totals["out_tokens"]),
        "by_model": by_model,
    }


def _cache_creation_split(usage: dict) -> tuple[int, int]:
    cc = usage.get("cache_creation")
    if isinstance(cc, dict):
        return (_int_field(cc, "ephemeral_5m_input_tokens"),
                _int_field(cc, "ephemeral_1h_input_tokens"))
    return (_int_field(usage, "cache_creation_input_tokens"), 0)


_KNOWN_USAGE_KEYS = frozenset({
    "input_tokens", "output_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens", "cache_creation", "iterations",
    "service_tier", "speed", "inference_geo", "server_tool_use",
})

_KNOWN_CACHE_CREATION_KEYS = frozenset({
    "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens",
})
_KNOWN_SERVER_TOOL_USE_KEYS = frozenset({
    "web_search_requests", "web_fetch_requests",
})

_NESTED_KNOWN_KEYS = {
    "cache_creation": _KNOWN_CACHE_CREATION_KEYS,
    "server_tool_use": _KNOWN_SERVER_TOOL_USE_KEYS,
}

_EXPECTED_USAGE_SHAPES = {
    "input_tokens": (int, float),
    "output_tokens": (int, float),
    "cache_read_input_tokens": (int, float),
    "cache_creation_input_tokens": (int, float),
    "cache_creation": (dict,),
    "server_tool_use": (dict,),
    "iterations": (list,),
}


def _unknown_usage_keys(usage: dict) -> list:
    unknown = [key for key in usage if key not in _KNOWN_USAGE_KEYS]
    for key, value in usage.items():
        if key not in _KNOWN_USAGE_KEYS or value is None:
            continue
        expected = _EXPECTED_USAGE_SHAPES.get(key)
        if expected is not None and (isinstance(value, bool)
                                     or not isinstance(value, expected)):
            unknown.append(f"{key}(unexpected-shape)")
            continue
        if isinstance(value, dict):
            known = _NESTED_KNOWN_KEYS.get(key)
            if known is None:
                unknown.extend(f"{key}.{nested}" for nested in value)
                continue
            for nested, nested_value in value.items():
                if nested not in known:
                    unknown.append(f"{key}.{nested}")
                elif nested_value is not None and (
                        isinstance(nested_value, bool)
                        or not isinstance(nested_value, (int, float))):
                    unknown.append(f"{key}.{nested}(unexpected-shape)")
    return sorted(unknown)


def usage_totals_of(event) -> dict | None:
    if not isinstance(event, dict) or event.get("type") != "assistant":
        return None
    message = event.get("message")
    if not isinstance(message, dict):
        return None
    message_id = message.get("id")
    usage = message.get("usage")
    if not isinstance(message_id, str) or not message_id or not isinstance(usage, dict):
        return None
    model = message.get("model")
    cc_5m, cc_1h = _cache_creation_split(usage)
    return {
        "message_id": message_id,
        "model": model if isinstance(model, str) and model else None,
        "out_tokens": _int_field(usage, "output_tokens"),
        "in_tokens": _int_field(usage, "input_tokens"),
        "cache_read_tokens": _int_field(usage, "cache_read_input_tokens"),
        "cache_creation_5m_tokens": cc_5m,
        "cache_creation_1h_tokens": cc_1h,
        "cache_creation_flat": _int_field(usage, "cache_creation_input_tokens"),
        "unknown_usage_keys": _unknown_usage_keys(usage),
    }


def sum_usage_by_model(log_path: Path) -> dict:
    by_model: dict = {}
    seen_ids: set[str] = set()
    try:
        with open(log_path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entry = usage_totals_of(event)
                if (entry is None or entry["model"] is None
                        or entry["message_id"] in seen_ids):
                    continue
                seen_ids.add(entry["message_id"])
                bucket = by_model.setdefault(entry["model"], {
                    "calls": 0, "out_tokens": 0, "in_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_creation_5m_tokens": 0, "cache_creation_1h_tokens": 0,
                })
                bucket["calls"] += 1
                bucket["out_tokens"] += entry["out_tokens"]
                bucket["in_tokens"] += entry["in_tokens"]
                bucket["cache_read_tokens"] += entry["cache_read_tokens"]
                bucket["cache_creation_5m_tokens"] += entry["cache_creation_5m_tokens"]
                bucket["cache_creation_1h_tokens"] += entry["cache_creation_1h_tokens"]
    except OSError:
        return {}
    return by_model


def last_assistant_summary(log_path: Path, max_chars: int = 200) -> Tuple[str | None, str | None]:
    if not log_path.is_file():
        return (None, None)
    last_summary = None
    last_timestamp = None
    for line in log_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        summary, timestamp = _assistant_text(event)
        if summary is not None:
            last_summary = summary
            last_timestamp = timestamp
    if last_summary is None:
        return (None, None)
    if len(last_summary) > max_chars:
        last_summary = last_summary[:max_chars - 1] + "…"
    return (last_summary, last_timestamp)


def last_assistant_ends_in_tool_use(log_path: Path) -> bool:
    try:
        if not log_path.is_file():
            return False
        last_blocks = None
        for line in log_path.read_text(errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "assistant":
                continue
            content = (event.get("message") or {}).get("content")
            if isinstance(content, list) and content:
                last_blocks = content
        if not last_blocks:
            return False
        final = last_blocks[-1]
        return isinstance(final, dict) and final.get("type") == "tool_use"
    except OSError:
        return False
