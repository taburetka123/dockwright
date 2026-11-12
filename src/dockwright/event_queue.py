from __future__ import annotations

from pathlib import Path

from . import events, paths, state


def _path(rule: str) -> Path:
    return paths.EVENTS_QUEUES / f"{rule}.json"


def _read_raw(rule: str) -> dict:
    raw = state.read_json(_path(rule))
    raw = raw if isinstance(raw, dict) else {}
    pending = [d for d in raw.get("pending") or [] if isinstance(d, dict)] if isinstance(raw.get("pending"), list) else []
    nudged = raw.get("nudged")
    return {"pending": pending, "nudged": nudged if isinstance(nudged, (int, float)) and not isinstance(nudged, bool)
            else None}


def _events(raw: dict) -> list[events.Event]:
    out = []
    for d in raw["pending"]:
        try:
            out.append(events.Event.from_dict(d))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _write(rule: str, raw: dict) -> None:
    state.write_json_atomic(_path(rule), raw)


def read(rule: str) -> list[events.Event]:
    with events._lock():
        return _events(_read_raw(rule))


def nudged(rule: str) -> float | None:
    with events._lock():
        return _read_raw(rule)["nudged"]


def push(rule: str, new: list[events.Event]) -> int:
    with events._lock():
        raw = _read_raw(rule)
        ids = {d.get("id") for d in raw["pending"]}
        added = 0
        for event in new:
            if event.id in ids:
                continue
            raw["pending"].append(event.to_dict())
            ids.add(event.id)
            added += 1
        if added:
            _write(rule, raw)
        return added


def ack(rule: str, event_id: str) -> tuple[int, int]:
    with events._lock():
        raw = _read_raw(rule)
        kept = [] if event_id == "*" else [d for d in raw["pending"] if d.get("id") != event_id]
        removed = len(raw["pending"]) - len(kept)
        if removed:
            raw["pending"] = kept
            _write(rule, raw)
        return removed, len(kept)


def _patch(rule: str, event_id: str, new_id: str, patch: dict) -> bool:
    with events._lock():
        raw = _read_raw(rule)
        hit = next((d for d in raw["pending"] if d.get("id") == event_id), None)
        if hit is None:
            return False
        payload = hit.get("payload") if isinstance(hit.get("payload"), dict) else {}
        reply = payload.get("reply") if isinstance(payload.get("reply"), dict) else {}
        payload["reply"] = {**reply, **patch}
        hit["payload"] = payload
        hit["id"] = new_id
        _write(rule, raw)
        return True


def update(rule: str, event_id: str, patch: dict) -> bool:
    return _patch(rule, event_id, event_id, patch)


def rekey(rule: str, event_id: str, new_id: str, patch: dict) -> bool:
    return _patch(rule, event_id, new_id, patch)


def touch_nudged(rule: str, now: float) -> None:
    with events._lock():
        raw = _read_raw(rule)
        raw["nudged"] = now
        _write(rule, raw)
