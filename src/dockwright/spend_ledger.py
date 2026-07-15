"""Durable token-spend ledger: one JSONL line per finished spend period.

Capture-side counterpart of `orchestrator spend-report`. Every path that drops
a spend-carrying session record (session_end unlink, /clear rotation, dead-pid
prunes, resume reclaiming an autoclosed record) appends here, so spend survives
the closed/ 7-day prune. Headless env-stripped `claude -p` runs land here too,
via the CLAUDE_SPEND_CLASS contract — the ledger is their ONLY capture (no
active record, no Stop-hook accumulation).

Hook-path module: must stay FastMCP-free and cheap to import, same contract as
registry.py (tests/test_import_graph.py pins it transitively).
"""
import json
import os
import time
from pathlib import Path

from . import paths

_SPEND_KEYS = ("turns", "out_tokens", "in_tokens", "cache_read_tokens", "cache_creation_tokens")


def _spend_totals(spend) -> dict | None:
    if not isinstance(spend, dict):
        return None
    totals = {key: spend[key] for key in _SPEND_KEYS
              if isinstance(spend.get(key), int) and not isinstance(spend.get(key), bool)}
    return totals or None


def _append_line(entry: dict) -> None:
    paths.SPEND_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(entry, separators=(",", ":")) + "\n").encode()
    fd = os.open(paths.SPEND_LEDGER, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def append_drop_event(record, source: str) -> None:
    """Archive a record's accumulated spend at drop time.

    Best-effort: spend is observability; the drop paths calling this (teardown
    hooks, prunes, resume) must proceed no matter what happens here. Cursor
    fields (last_msg_id, last_turn_out) are stripped — a validated
    superset of mcp_server._spend_totals's vocabulary (adds cache_creation_tokens
    + int-validation).
    """
    try:
        if not isinstance(record, dict):
            return
        spend = _spend_totals(record.get("spend"))
        if spend is None:
            return
        _append_line({
            "ts": time.time(),
            "sid": record.get("claude_sid"),
            "name": record.get("name"),
            # closed/ records carry no agent key; only workers are archived there.
            "agent": "nested" if record.get("nested") else (record.get("agent") or "worker"),
            "parent_manager_name": record.get("parent_manager_name"),
            "runtime": record.get("runtime") or "claude",
            "account": record.get("account"),
            "started_at": record.get("started_at"),
            "source": source,
            "spend": spend,
        })
    except Exception:
        pass


def append_headless_event(spend_class, sid, transcript_path) -> None:
    """Whole-transcript spend for an env-stripped tagged headless run."""
    try:
        if not spend_class or not transcript_path:
            return
        from .transcript import sum_usage
        totals = sum_usage(Path(transcript_path))
        if not any(totals.values()):
            return
        _append_line({
            "ts": time.time(),
            "sid": sid,
            "name": str(spend_class),
            "agent": "headless",
            "source": "headless",
            "spend": totals,
        })
    except Exception:
        pass


def read_events() -> list[dict]:
    """All ledger entries; malformed lines skipped (many writers, a torn line
    must not hide the rest)."""
    try:
        raw = paths.SPEND_LEDGER.read_text(errors="replace")
    except OSError:
        return []
    events = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and isinstance(event.get("spend"), dict):
            events.append(event)
    return events
