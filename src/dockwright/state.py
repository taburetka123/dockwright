import json
import os
import errno
import fcntl
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import paths


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
    except (OSError, ValueError):
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

def migrate_assignment(old_sid: str, new_sid: str) -> None:
    old_path = paths.assignment_path(old_sid)
    new_path = paths.assignment_path(new_sid)
    if not old_path.exists() or new_path.exists():
        return
    record = read_json(old_path)
    if not isinstance(record, dict):
        return
    write_json_atomic(new_path, {**record, "claude_sid": new_sid})
    old_path.unlink(missing_ok=True)

@contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock_nb(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


_memo: dict[Path, tuple[int, list[str]] | None] = {}


def memo_dirs(directories: Iterable[Path]) -> None:
    global _memo
    _memo = {Path(d): None for d in directories}


def _memoized_json_in(directory: Path, memo: dict) -> Iterator[dict]:
    try:
        mtime_ns = os.stat(directory).st_mtime_ns
    except OSError:
        return
    saved = memo[directory]
    if saved is None or saved[0] != mtime_ns:
        texts = []
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        for p in entries:
            if p.suffix != ".json":
                continue
            try:
                text = p.read_text()
                if json.loads(text) is not None:
                    texts.append(text)
            except (OSError, ValueError):
                continue
        saved = memo[directory] = (mtime_ns, texts)
    for text in saved[1]:
        yield json.loads(text)


def list_json_in(directory: Path) -> Iterator[dict]:
    memo = _memo
    if directory in memo:
        yield from _memoized_json_in(directory, memo)
        return
    if not directory.is_dir():
        return
    for p in directory.iterdir():
        if p.suffix == ".json":
            data = read_json(p)
            if data is not None:
                yield data


def parent_name_of(record) -> str | None:
    if not isinstance(record, dict):
        return None
    name = record.get("parent_name")
    if name is not None:
        return name
    return record.get("parent_manager_name")


def default_provider_for(runtime) -> str:
    return "codex-tui" if runtime == "codex" else "claude-tui"


def provider_of(record) -> str | None:
    if not isinstance(record, dict) or record.get("nested"):
        return None
    return record.get("provider") or default_provider_for(record.get("runtime"))


def live_children_of(name, records=None) -> list:
    if not name:
        return []
    if records is None:
        from . import paths
        records = list(list_json_in(paths.ACTIVE))
    out = []
    for record in records:
        if not isinstance(record, dict) or record.get("kind") != "subworker" or record.get("nested"):
            continue
        if parent_name_of(record) != name:
            continue
        pid = record.get("pid")
        if isinstance(pid, int) and not isinstance(pid, bool) and pid >= 1 and not _pid_alive(pid):
            continue
        out.append(record)
    return out


def has_live_child(record, records=None) -> bool:
    return isinstance(record, dict) and bool(live_children_of(record.get("name"), records))


def child_depth(parent_record, agent: str, kind: str | None = None) -> int:
    if isinstance(parent_record, dict):
        base = parent_record.get("depth")
        if isinstance(base, bool) or not isinstance(base, int):
            base = 1 if parent_record.get("agent") == "worker" else 0
        return base + 1
    if agent == "manager":
        return 0
    return 2 if kind == "subworker" else 1


def tree_fields(agent: str, *, kind: str | None = None, parent_name=None, parent_sid=None,
                parent_record=None, nested: bool = False) -> dict:
    if nested:
        return {"kind": agent, "parent_sid": None, "parent_name": parent_name, "depth": None}
    if agent == "manager":
        return {"kind": "manager", "parent_sid": None, "parent_name": parent_name, "depth": 0}
    return {
        "kind": kind or agent,
        "parent_sid": parent_sid or None,
        "parent_name": parent_name,
        "depth": child_depth(parent_record, agent, kind),
    }


_FM_DELIM = "---"
_FM_KEYS = ("phase", "name", "status", "writer_sid", "contract_hash", "written_at", "read_set", "scope")
_FM_OPTIONAL_KEYS = ("decided_by",)


def serialize_artifact(stamp: dict, body: str) -> str:
    lines = [_FM_DELIM]
    for k in _FM_KEYS:
        lines.append(f"{k}: {json.dumps(stamp.get(k))}")
    for k in _FM_OPTIONAL_KEYS:
        if stamp.get(k) is not None:
            lines.append(f"{k}: {json.dumps(stamp[k])}")
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


EVENT_LINE_MAX = 3500


def event_line(event: dict) -> bytes:
    return (json.dumps(event, separators=(",", ":")) + "\n").encode()


def append_event(events_path: Path, event: dict) -> None:
    events_path.parent.mkdir(parents=True, exist_ok=True)
    event = dict(event)
    event.setdefault("ts", time.time())
    event.setdefault("event_id", uuid.uuid4().hex[:8])
    payload = event_line(event)
    if len(payload) > EVENT_LINE_MAX:
        event["reason"] = (event.get("reason", "")[:1000] + "…[truncated]")
        payload = event_line(event)
    if len(payload) > EVENT_LINE_MAX:
        event = {"ts": event["ts"], "event_id": event["event_id"],
                 "type": str(event.get("type"))[:64],
                 "reason": "…[event truncated: oversized fields]"}
        payload = event_line(event)
    fd = os.open(events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
