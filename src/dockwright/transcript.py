import json
import os
from pathlib import Path
from typing import Tuple

def _find_claude_session_log(session_id: str) -> Path | None:
    """Locate ~/.claude/projects/*/<sid>.jsonl."""
    projects_root = Path(os.environ.get("HOME", "")) / ".claude" / "projects"
    if not projects_root.is_dir():
        return None
    for project_dir in projects_root.iterdir():
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.is_file():
            return candidate
    return None


def _find_codex_session_log(session_id: str) -> Path | None:
    """Locate ~/.codex/sessions/**/rollout-*-<sid>.jsonl."""
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
    """Locate the saved transcript for the selected worker runtime."""
    if runtime == "codex":
        return _find_codex_session_log(session_id)
    return _find_claude_session_log(session_id)


def latest_subagent_mtime(session_log: Path, session_id: str) -> float:
    """Newest mtime across <project>/<sid>/subagents/agent-*.jsonl, else 0.0.

    Background subagents (Agent run_in_background / Workflow) keep appending
    to these transcripts after the parent worker's turn ends — the freshest
    write is the delegation liveness signal. Mirrors stale_monitor's
    _last_activity mtime-max pattern. Crash-proof: any I/O failure reads as 0.0
    (= no delegation = pre-change behavior). Claude layout only.
    """
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


DELEGATION_FRESH_SEC = 120  # == monitor.TURN_END_GRACE_SEC_DEFAULT: a worker stops
                            # reading as working at the same moment its silent-finish
                            # alert becomes eligible.

TURN_END_GRACE_ENV = "CLAUDE_ORCH_TURN_END_GRACE_SEC"


def delegation_fresh_sec() -> int:
    """Shared grace for 'delegation is still fresh': the monitor's silent-finish
    grace env override moves the read-side surfaces with it, so a non-default
    grace can't split monitor truth from list_workers/paint truth. (The
    statusline's `-mmin -2` is a documented hardcoded approximation.)"""
    try:
        value = int(os.environ.get(TURN_END_GRACE_ENV, str(DELEGATION_FRESH_SEC)))
    except ValueError:
        return DELEGATION_FRESH_SEC
    return value if value >= 0 else DELEGATION_FRESH_SEC


def is_delegating(record: dict, now: float, log: Path | None = None,
                  fresh_sec: float | None = None) -> bool:
    """Whether this session's newest subagent write is BOTH newer than the
    session's own transcript AND fresh within fresh_sec (default: the shared
    delegation_fresh_sec() grace).

    The growth predicate (subagent > main log) discriminates background
    delegation from a foreground agent whose result the worker already
    consumed in-turn: a consumed agent's last write predates the main log's
    final appends, while a background agent keeps writing after the main log
    froze at Stop. State-agnostic — callers decide which states to apply it
    to. Crash-proof: any I/O failure reads as False (pre-change behavior).
    """
    try:
        if fresh_sec is None:
            fresh_sec = delegation_fresh_sec()
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


SPEND_TAIL_MAX_BYTES = 65536


def _int_field(mapping: dict, key: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _usage_entry(line: str, seen_ids: set) -> dict | None:
    """One transcript line → usage entry, or None. Every level shape-checked:
    the transcript is another process's output, any valid-JSON shape can appear.
    Split API responses repeat the same message id; each id counts once via
    seen_ids."""
    line = line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict) or event.get("type") != "assistant":
        return None
    message = event.get("message")
    if not isinstance(message, dict):
        return None
    message_id = message.get("id")
    usage = message.get("usage")
    if not isinstance(message_id, str) or not message_id or not isinstance(usage, dict):
        return None
    if message_id in seen_ids:
        return None
    seen_ids.add(message_id)
    return {
        "message_id": message_id,
        "output_tokens": _int_field(usage, "output_tokens"),
        "input_tokens": _int_field(usage, "input_tokens"),
        "cache_read_tokens": _int_field(usage, "cache_read_input_tokens"),
        "cache_creation_tokens": _int_field(usage, "cache_creation_input_tokens"),
    }


def tail_usage_entries(log_path: Path, max_bytes: int = SPEND_TAIL_MAX_BYTES) -> list[dict]:
    """Per-API-call usage entries from the transcript tail, deduped by message id.

    Reads only the last max_bytes (transcripts grow to many MB; stale_monitor's
    tail pattern) and drops the window's first line as possibly partial. Claude
    transcript shape only — every assistant event carries message.id + usage; an
    API response split across several events repeats the SAME id and usage, so
    each id counts once. The transcript is another process's output: any
    valid-JSON shape can appear, so every level is shape-checked and a bad line
    is skipped, never raised.
    """
    try:
        size = log_path.stat().st_size
        with open(log_path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read(max_bytes)
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]
    entries: list[dict] = []
    seen_ids: set[str] = set()
    for line in lines:
        entry = _usage_entry(line, seen_ids)
        if entry is not None:
            entries.append(entry)
    return entries


def sum_usage(log_path: Path) -> dict:
    """Whole-transcript usage totals, deduped by message id.

    Full-file read — only for bounded headless transcripts (CLAUDE_SPEND_CLASS
    capture), never the per-turn Stop path (that stays on tail_usage_entries).
    Mirrors deploy/scripts/gardener_spend.py's sum_usage, which stays
    standalone-duplicated by design (it runs under /usr/bin/python3 with no
    package on path).
    """
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


def _cache_creation_split(usage: dict) -> tuple[int, int]:
    """(5m_tokens, 1h_tokens) from a usage block.

    Prefer the structured cache_creation object's TTL split. If it is absent
    (older transcripts) but the flat cache_creation_input_tokens is present,
    attribute the flat total to the 5m bucket — the API default TTL, and the
    conservative choice (1.25x < the 1h 2x rate, so it never over-charges).
    """
    cc = usage.get("cache_creation")
    if isinstance(cc, dict):
        return (_int_field(cc, "ephemeral_5m_input_tokens"),
                _int_field(cc, "ephemeral_1h_input_tokens"))
    return (_int_field(usage, "cache_creation_input_tokens"), 0)


def sum_usage_by_model(log_path: Path) -> dict:
    """Whole-transcript usage totals grouped by message.model, deduped by id.

    Full-file read (never the tail) so long sessions are not undercounted — the
    basis for the dollar-cost meter. Each model maps to per-token totals plus a
    cache-creation TTL split (5m / 1h) and a call count. Claude transcript shape
    only; an event without a string model is skipped. Crash-proof: any I/O
    failure returns {}.
    """
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
                if not isinstance(event, dict) or event.get("type") != "assistant":
                    continue
                message = event.get("message")
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                model = message.get("model")
                usage = message.get("usage")
                if (not isinstance(message_id, str) or not message_id
                        or not isinstance(model, str) or not model
                        or not isinstance(usage, dict) or message_id in seen_ids):
                    continue
                seen_ids.add(message_id)
                bucket = by_model.setdefault(model, {
                    "calls": 0, "out_tokens": 0, "in_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_creation_5m_tokens": 0, "cache_creation_1h_tokens": 0,
                })
                cc_5m, cc_1h = _cache_creation_split(usage)
                bucket["calls"] += 1
                bucket["out_tokens"] += _int_field(usage, "output_tokens")
                bucket["in_tokens"] += _int_field(usage, "input_tokens")
                bucket["cache_read_tokens"] += _int_field(usage, "cache_read_input_tokens")
                bucket["cache_creation_5m_tokens"] += cc_5m
                bucket["cache_creation_1h_tokens"] += cc_1h
    except OSError:
        return {}
    return by_model


def accumulate_spend(spend: dict | None, entries: list[dict]) -> dict | None:
    """Fold the just-ended turn's tail usage entries into the running spend dict.

    The cursor (last_msg_id) marks the last entry already counted: only entries
    after it are new. Cursor absent from the tail means the window rolled past
    it — count the whole visible tail and accept the undercount beyond it.
    Nothing new after the cursor (Stop re-fire) → return spend unchanged so
    turns don't drift.
    """
    if not entries:
        return spend
    new_entries = entries
    if spend is not None:
        cursor = spend.get("last_msg_id")
        for index, entry in enumerate(entries):
            if entry.get("message_id") == cursor:
                new_entries = entries[index + 1:]
                break
    if not new_entries:
        return spend
    prior = spend or {}
    turn_out = sum(e.get("output_tokens", 0) for e in new_entries)
    return {
        "turns": _int_field(prior, "turns") + 1,
        "out_tokens": _int_field(prior, "out_tokens") + turn_out,
        "in_tokens": _int_field(prior, "in_tokens") + sum(e.get("input_tokens", 0) for e in new_entries),
        "cache_read_tokens": _int_field(prior, "cache_read_tokens")
            + sum(e.get("cache_read_tokens", 0) for e in new_entries),
        "last_turn_out": turn_out,
        "last_msg_id": new_entries[-1].get("message_id"),
    }


def last_assistant_summary(log_path: Path, max_chars: int = 200) -> Tuple[str | None, str | None]:
    """Return (text_summary, iso_timestamp) of the last assistant turn, or (None, None)."""
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
