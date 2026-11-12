from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import ATTEMPTED, BUDGET_SPENT, CALL_TIMEOUT_SEC, ERROR_CHARS, FAILED
from .. import config, events

KIND_RE = re.compile(r"^[a-z][a-z0-9-]*$")
SUMMARY_CHARS = 200


def default_call(argv: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    done = subprocess.run(argv, capture_output=True, timeout=timeout)
    return done.returncode, done.stdout, done.stderr


def _instant(raw) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _span(e: events.Event) -> tuple[float, float] | None:
    start, end = _instant(e.payload.get("start")), _instant(e.payload.get("end"))
    if start is None or end is None or not start.tzinfo or not end.tzinfo:
        return None
    return start.timestamp(), end.timestamp()


def window_start(kind: str, match: dict, pool: list[events.Event], now: float) -> float | None:
    sets: dict[str, list[tuple[float, float]]] = {}
    for e in pool:
        if e.kind == kind and all(e.match.get(k) == v for k, v in match.items()) \
                and isinstance(e.payload.get("issued"), str) and e.payload["issued"]:
            span = _span(e)
            sets.setdefault(e.payload["issued"], []).extend([span] if span else [])
    if not sets:
        return None
    spans = sets[max(sets)]
    covering = [s for s, end in spans if s <= now < end]
    if not covering:
        return None
    start = min(covering)
    while True:
        earlier = [s for s, end in spans if s < start <= end]
        if not earlier:
            return start
        start = min(earlier)


def _parse_line(line: str, ts: float) -> events.Event | None:
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("id"), str) or not isinstance(obj.get("kind"), str) \
            or not KIND_RE.match(obj["kind"]):
        return None
    raw_match = obj.get("match")
    match = {k: v for k, v in raw_match.items() if isinstance(k, str) and isinstance(v, str)} \
        if isinstance(raw_match, dict) else {}
    summary = str(obj.get("summary") or "")[:SUMMARY_CHARS]
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    return events.Event(f"exec:{obj['id']}", ts, "exec", obj["kind"], match, "", summary, payload)


def _timeout(until: float | None) -> float:
    if until is None:
        return CALL_TIMEOUT_SEC
    return max(1.0, min(CALL_TIMEOUT_SEC, until - time.time()))


def poll(subs: list[events.Sub], rules: dict[str, dict], cursor: dict, env: dict[str, str], now: float, call=None,
         until: float | None = None) -> tuple[list[events.Event], dict]:
    call = call or default_call
    cfg = config.events_source("exec")
    if not cfg["commands"]:
        raise events.PollError("[events.sources.exec] commands is empty: name the commands whose JSON lines become events")
    last_run = {k: v for k, v in (cursor.get("last_run") or {}).items()
                if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool)}
    pool = events.recent("exec", now)
    out: list[events.Event] = []
    failed: list[list] = []
    attempted: list[str] = []
    for i, cmd in enumerate(cfg["commands"]):
        selector = cmd["selector"]
        ts = now + i / 1000
        if cmd["error"]:
            failed.append([selector, cmd["error"]])
            continue
        if now - last_run.get(selector, float("-inf")) < cmd["every_sec"]:
            continue
        if cmd["stop_file"] and Path(cmd["stop_file"]).exists():
            continue
        if cmd["while"] and window_start(cmd["while"]["kind"], cmd["while"]["match"], pool + out, now) is None:
            continue
        timeout = _timeout(until)
        attempted.append(selector)
        try:
            rc, stdout, stderr = call(cmd["argv"], timeout)
        except subprocess.TimeoutExpired as err:
            stdout = err.stdout or b""
            failed.append([selector, f"timeout after {timeout:.0f}s"])
        except OSError as err:
            stdout = b""
            failed.append([selector, f"{type(err).__name__}: {err}"])
        else:
            if rc != 0:
                failed.append([selector, f"exit {rc}: {stderr.decode('utf-8', 'replace').strip()[:ERROR_CHARS]}"])
        last_run[selector] = now
        for n, line in enumerate(stdout.decode("utf-8", "replace").splitlines(), 1):
            if not line.strip():
                continue
            event = _parse_line(line, ts)
            if event is None:
                failed.append([selector, f"line {n}: not a JSON object with string id and kind"])
            else:
                out.append(event)
    return out, {BUDGET_SPENT: [], FAILED: failed, "last_run": last_run, ATTEMPTED: attempted}
