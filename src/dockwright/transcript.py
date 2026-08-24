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


DELEGATION_FRESH_SEC = 120  # the monitor's turn-end / young-file grace only

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
    """Whether this session's newest subagent write is BOTH newer than the
    session's own transcript AND fresh within fresh_sec (default: the shared
    episode_grace_sec() liveness window).

    The growth predicate (subagent > main log) discriminates background
    delegation from a foreground agent whose result the worker already
    consumed in-turn: a consumed agent's last write predates the main log's
    final appends, while a background agent keeps writing after the main log
    froze at Stop. State-agnostic — callers decide which states to apply it
    to. Crash-proof: any I/O failure reads as False (pre-change behavior).
    """
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
    """One transcript line → usage entry, or None. Dedup here; extraction in
    usage_totals_of (split API responses repeat the same message id; each id
    counts once via seen_ids). sum_usage keeps the FLAT
    cache_creation_input_tokens field (headless-ledger capture + the standalone
    gardener_spend.py mirror pin it); the TTL-split consumers are recount_spend
    and the session report."""
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
    """Whole-transcript usage totals, deduped by message id.

    Full-file read — only for bounded headless transcripts (CLAUDE_SPEND_CLASS
    capture); the per-turn Stop path uses recount_spend.
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


_SPEND_TOTAL_KEYS = ("out_tokens", "in_tokens", "cache_read_tokens", "cache_creation_tokens")


def _parse_event_ts(value) -> float | None:
    """Transcript event timestamp (ISO-8601, Z-suffixed) → epoch seconds, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def subagent_logs_for(log_path: Path, sid: str | None = None) -> list:
    """Sidecar transcripts of a session's background subagents —
    `<log.parent>/<sid>/subagents/agent-*.jsonl`, sorted; `sid` defaults to
    the log's filename stem (the claude layout). THE single acquisition path
    for subagent spend: recount_spend (Stop hook) and the session report both
    resolve sidecars through here, so their token sources cannot diverge —
    list_workers hiding 41% of subagent-session money was exactly a second
    hand-rolled acquisition path. Crash-proof: any OSError reads as none.
    """
    try:
        base = log_path.parent / (sid or log_path.stem) / "subagents"
        return sorted(base.glob("agent-*.jsonl"))
    except OSError:
        return []


def recount_spend(log_path: Path, prior_spend: dict | None,
                  started_at: float | None = None,
                  subagent_logs: list | None = None) -> dict | None:
    """Whole-session recount of the spend — a pure function of the fully-read
    main transcript PLUS the session's subagent sidecars, recomputed on every
    Stop. Replaces the retired 64KiB-tail fold, which silently lost every
    usage entry that rolled out of the window on a big turn (the 2026-07-28
    2.3x under-count). Sidecars auto-discover via subagent_logs_for when the
    param is None — the same acquisition path the session report uses — so a
    caller cannot silently reproduce the main-only under-count by omitting a
    parameter; pass [] to deliberately scope to the main file. Dedup by
    message id is GLOBAL across files, main file first.

    Replayed-history exclusion: a resume can copy the predecessor's events into
    the successor transcript with sessionId REWRITTEN to the new sid, so the sid
    cannot discriminate them — but copied events keep their ORIGINAL timestamps,
    which strictly predate this record's started_at. Entries older than
    started_at are skipped; a missing/unparseable timestamp counts (fail-open).

    Conservative failure semantics (deliberately unlike sum_usage's partial
    totals): any OSError on the MAIN file, or a session that parses to ZERO
    usage entries, returns prior_spend unchanged — the caller's `if spend is
    not None` then leaves the record exactly as it was. A sidecar that fails
    to read is SKIPPED (best-effort: a torn sidecar must not lose main spend).
    Unchanged totals also return prior_spend, so a Stop re-fire never drifts
    `turns`. A fully-read session with lower totals is adopted as the files'
    truth (last_turn_out clamps at 0).
    """
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
        # TTL split, not the flat field: a 1h-TTL cache write puts 0 in the
        # flat cache_creation_input_tokens with the real value only in the
        # structured object.
        totals["cache_creation_tokens"] += (entry["cache_creation_5m_tokens"]
                                            + entry["cache_creation_1h_tokens"])
        # Per-model token buckets so display surfaces (list_workers) can price
        # at READ time with current rates. A model-less entry counts in the
        # totals above but cannot be priced — absent here by construction.
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


_KNOWN_USAGE_KEYS = frozenset({
    "input_tokens", "output_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens", "cache_creation", "iterations",
    "service_tier", "speed", "inference_geo", "server_tool_use",
})

# Keys that nest further token/count data. A new key INSIDE them (e.g. a 2h
# cache TTL beside the shipped 5m/1h) is dropped from totals — no multiplier
# exists for it — so it must fail as loud as a top-level unknown; recognising
# only the outer key would be blind one level down.
_KNOWN_CACHE_CREATION_KEYS = frozenset({
    "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens",
})
_KNOWN_SERVER_TOOL_USE_KEYS = frozenset({
    "web_search_requests", "web_fetch_requests",
})

# Nested contract per known container key. Structural default-deny: a KNOWN
# key whose value is a dict but has NO entry here gets ALL its contents
# flagged — the next container added to _KNOWN_USAGE_KEYS cannot arrive
# silently unguarded (the hand-maintained pair list was exactly that hole).
_NESTED_KNOWN_KEYS = {
    "cache_creation": _KNOWN_CACHE_CREATION_KEYS,
    "server_tool_use": _KNOWN_SERVER_TOOL_USE_KEYS,
}

# Expected value shape per money-bearing / accounted key (None always allowed
# — APIs emit nulls). A known key whose value flips type (cache_creation
# becoming a list) silently zeroes an existing money bucket; validate the
# shape we sum, fail loud on anything else. Informational strings
# (service_tier/speed/inference_geo) are deliberately unconstrained.
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
            # bool is an int subclass but _int_field zeroes it — same silent loss
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
                    # Nested VALUES are numeric counts; a type flip here
                    # silently zeroes an existing bucket (_int_field -> 0),
                    # same silent-money-loss class as the top-level shapes.
                    unknown.append(f"{key}.{nested}(unexpected-shape)")
    return sorted(unknown)


def usage_totals_of(event) -> dict | None:
    """Assistant-event acceptance + usage extraction shared by every accountant.

    The single point deciding which records count and which usage fields sum —
    recount_spend (Stop hook), sum_usage (headless ledger), sum_usage_by_model
    (spend-cost), and the session report all consume it, so they cannot drift.
    NO dedup (callers own seen_ids) and NO birth filter (recount_spend applies
    its filter BEFORE this call). `model` is None when message.model is absent —
    that is not a rejection: recount_spend/sum_usage accept model-less events;
    sum_usage_by_model applies its own model gate. `usage.iterations[]` is a
    sub-breakdown of this same record's top-level totals — top-level only, or
    the totals double-count. unknown_usage_keys (any JSON type) lets read-side
    consumers fail loud when the API grows a token class this set doesn't know.
    """
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


def last_assistant_ends_in_tool_use(log_path: Path) -> bool:
    """True when the transcript's LAST assistant event's final content block is
    a tool_use — the CLI is waiting on a tool or modal result (AskUserQuestion,
    a long Bash): mid-turn, alive. A latched brick banner is appended as a
    synthetic assistant TEXT event (model "<synthetic>", isApiErrorMessage —
    verified against the 2026-07-29 incident transcripts), so a bricked
    transcript never ends in tool_use. Crash-proof: absent/unreadable/empty
    reads as False (no aliveness evidence)."""
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
