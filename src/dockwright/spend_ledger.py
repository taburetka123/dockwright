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


_TRACELESS_DROP_SOURCES = frozenset({"prune", "preflight_prune"})


def append_drop_event(record, source: str) -> None:
    try:
        if not isinstance(record, dict):
            return
        spend = _spend_totals(record.get("spend"))
        if spend is None:
            if source not in _TRACELESS_DROP_SOURCES:
                return
            spend = {}
        _append_line({
            "ts": time.time(),
            "sid": record.get("claude_sid"),
            "name": record.get("name"),
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
