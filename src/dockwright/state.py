import json
import os
import errno
import time
import uuid
from pathlib import Path
from typing import Any, Iterator


def _pid_alive(pid: int) -> bool:
    """True if `pid` is a live process. Returns False for non-positive pids."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno == errno.EPERM


def window_id_of(record: dict) -> str:
    """Return the record's tmux pane id, with back-compat for the legacy
    `iterm_sid` field. Persistent records written before the JSON field rename
    only carry `iterm_sid`; everything written after this PR carries
    `window_id`. Either key returns ``""`` if absent or set to None.
    """
    return record.get("window_id") or record.get("iterm_sid") or ""


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        # OSError: the file can be unlinked between exists() and read_text()
        # by a concurrent session_end/sweep — a routine race for every scanner
        # that globs active/, not an error worth propagating.
        return None

def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique tmp per invocation (pid+uuid): this record is rewritten from
    # multiple OS processes (worker hooks, the manager's MCP server), and a
    # target-derived tmp name lets those writers interleave on ONE tmp file —
    # truncated JSON at the final path or FileNotFoundError from the second
    # os.replace. Same idiom as mcp_server._write_artifact_atomic.
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


# --- Artifact-store helpers ---
#
# Frontmatter format: YAML-shaped block where every value is a JSON literal —
# dependency-free to parse/serialize, and still pyyaml-readable if a dep ever lands.

_FM_DELIM = "---"
_FM_KEYS = ("phase", "name", "status", "writer_sid", "contract_hash", "written_at", "read_set")


def serialize_artifact(stamp: dict, body: str) -> str:
    lines = [_FM_DELIM]
    for k in _FM_KEYS:
        lines.append(f"{k}: {json.dumps(stamp.get(k))}")
    lines.append(_FM_DELIM)
    return "\n".join(lines) + "\n" + body


def parse_artifact(text: str) -> tuple[dict, str]:
    """Returns (stamp, body). Raises ValueError when there is no frontmatter block.

    Delimiter matching is LINE-anchored, not substring: a "---" inside a
    frontmatter value (e.g. name="acme---web") must not sever the block.
    Frontmatter values are single-line `key: <json>` pairs, so no value line
    can ever BE exactly "---" — the first bare delimiter line is unambiguous.
    Body is reconstructed byte-exact (leading blank lines preserved).

    Per-line defensive: a hand-edited/corrupt frontmatter line is skipped, never
    crashes a fold over the whole store.
    """
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
    """Atomic small-line append to a per-task_key events.jsonl.

    O_APPEND makes seek-to-end + write one atomic step; a single os.write of a
    line capped well under the local-APFS atomic-append bound does not interleave
    across concurrent appenders. Single-Mac guarantee — the multi-host fallback
    (per-writer event files) is documented in the spec, not built.
    """
    events_path.parent.mkdir(parents=True, exist_ok=True)
    event = dict(event)          # never mutate the caller's dict
    event.setdefault("ts", time.time())
    event.setdefault("event_id", uuid.uuid4().hex[:8])
    payload = (json.dumps(event, separators=(",", ":")) + "\n").encode()
    if len(payload) > 3500:
        event["reason"] = (event.get("reason", "")[:1000] + "…[truncated]")
        payload = (json.dumps(event, separators=(",", ":")) + "\n").encode()
    if len(payload) > 3500:
        # Reason truncation wasn't the culprit (oversize name/phase/...): drop to
        # a minimal event rather than breach the atomic-append bound.
        event = {"ts": event["ts"], "event_id": event["event_id"],
                 "type": str(event.get("type"))[:64],
                 "reason": "…[event truncated: oversized fields]"}
        payload = (json.dumps(event, separators=(",", ":")) + "\n").encode()
    fd = os.open(events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
