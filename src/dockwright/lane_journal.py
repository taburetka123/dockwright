from __future__ import annotations

from . import paths


def would_journal(task_key: str | None) -> bool:
    return bool(task_key) and paths.artifact_events_path(task_key).is_file()


def journal_once(task_key: str | None, kind: str, fields: dict) -> bool:
    if not would_journal(task_key):
        return False
    events = paths.artifact_events_path(task_key)
    from . import mcp_server as ms
    with ms._token_lock(task_key):
        for event in ms._read_events(events):
            if event.get("type") == "lane_event" and event.get("kind") == kind and event.get("fields") == fields:
                return True
        ms._append_task_event(task_key, {"type": "lane_event", "kind": kind, "fields": fields})
    return True


def journal_first(task_key: str | None, kind: str, fields: dict, identity: tuple[str, ...]) -> bool:
    if not would_journal(task_key):
        return False
    events = paths.artifact_events_path(task_key)
    from . import mcp_server as ms
    with ms._token_lock(task_key):
        for event in ms._read_events(events):
            seen = event.get("fields") or {}
            if (event.get("type") == "lane_event" and event.get("kind") == kind
                    and all(seen.get(key) == fields.get(key) for key in identity)):
                return False
        ms._append_task_event(task_key, {"type": "lane_event", "kind": kind, "fields": fields})
    return True
