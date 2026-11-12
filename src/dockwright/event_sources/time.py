from __future__ import annotations

from datetime import datetime, timezone

from .. import events


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def poll(subs: list[events.Sub], rules: dict[str, dict], cursor: dict, env: dict[str, str], now: float, call=None,
         until: float | None = None) -> tuple[list[events.Event], dict]:
    out = [events.Event(f"time:{s.sub_id}:deadline", now, "time", "deadline", {"sub_id": s.sub_id}, "",
                        f"deadline {_iso(s.deadline)} passed for {s.source} {s.match}", {})
           for s in subs if s.deadline is not None and s.deadline <= now]
    return out, dict(cursor)
