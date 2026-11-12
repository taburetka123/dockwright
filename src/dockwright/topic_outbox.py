from pathlib import Path

from . import paths, state

OUTBOX = "topic-outbox"
RETRY_SEC = 300
ATTACHED_RETRY_SEC = 30
PAGE_SEC = 3600


def outbox_dir() -> Path:
    return paths.ROOT / OUTBOX


def drain_lock_path() -> Path:
    return outbox_dir() / ".drain.lock"


def entry_path(task_key: str, record: str) -> Path:
    return outbox_dir() / f"{task_key}--{paths._safe_segment(record)}.json"


def entries() -> list[dict]:
    if not outbox_dir().is_dir():
        return []
    out = []
    for path in sorted(outbox_dir().glob("*.json")):
        try:
            entry = state.read_json(path)
        except (OSError, ValueError):
            continue
        if isinstance(entry, dict) and entry.get("key") and entry.get("record"):
            out.append(entry)
    return out


def retry_due(entry: dict, now: float) -> bool:
    last = entry.get("last_try_at")
    if isinstance(last, bool) or not isinstance(last, (int, float)):
        last = 0.0
    return now - last >= (ATTACHED_RETRY_SEC if entry.get("attached") is True else RETRY_SEC)


def write(entry: dict) -> None:
    state.write_json_atomic(entry_path(entry["key"], entry["record"]), entry)


def drop(task_key: str, record: str) -> None:
    entry_path(task_key, record).unlink(missing_ok=True)


def mark_paged(task_key: str, record: str) -> None:
    entry = state.read_json(entry_path(task_key, record))
    if isinstance(entry, dict):
        write({**entry, "paged": True})
