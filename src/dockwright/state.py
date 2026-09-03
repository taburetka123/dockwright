import json
import os
import errno
import time
import uuid
from pathlib import Path
from typing import Any, Iterator


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno == errno.EPERM


def window_id_of(record: dict) -> str:
    return record.get("window_id") or record.get("iterm_sid") or ""


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None

def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise

def list_json_in(directory: Path) -> Iterator[dict]:
    if not directory.is_dir():
        return
    for p in directory.iterdir():
        if p.suffix == ".json":
            data = read_json(p)
            if data is not None:
                yield data


_FM_DELIM = "---"
_FM_KEYS = ("phase", "name", "status", "writer_sid", "contract_hash", "written_at", "read_set")


def serialize_artifact(stamp: dict, body: str) -> str:
    lines = [_FM_DELIM]
    for k in _FM_KEYS:
        lines.append(f"{k}: {json.dumps(stamp.get(k))}")
    lines.append(_FM_DELIM)
    return "\n".join(lines) + "\n" + body


def parse_artifact(text: str) -> tuple[dict, str]:
    lines = text.split("\n")
    if lines[0] != _FM_DELIM:
        raise ValueError("artifact missing frontmatter")
    try:
        end = lines.index(_FM_DELIM, 1)
    except ValueError:
        raise ValueError("artifact missing frontmatter") from None
    stamp: dict = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, _, raw = line.partition(":")
        try:
            stamp[key.strip()] = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
    return stamp, "\n".join(lines[end + 1:])


def append_event(events_path: Path, event: dict) -> None:
    events_path.parent.mkdir(parents=True, exist_ok=True)
    event = dict(event)
    event.setdefault("ts", time.time())
    event.setdefault("event_id", uuid.uuid4().hex[:8])
    payload = (json.dumps(event, separators=(",", ":")) + "\n").encode()
    if len(payload) > 3500:
        event["reason"] = (event.get("reason", "")[:1000] + "…[truncated]")
        payload = (json.dumps(event, separators=(",", ":")) + "\n").encode()
    if len(payload) > 3500:
        event = {"ts": event["ts"], "event_id": event["event_id"],
                 "type": str(event.get("type"))[:64],
                 "reason": "…[event truncated: oversized fields]"}
        payload = (json.dumps(event, separators=(",", ":")) + "\n").encode()
    fd = os.open(events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
