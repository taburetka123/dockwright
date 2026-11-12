from __future__ import annotations

import os
import plistlib
import re
import sqlite3
import sys
import urllib.parse
from pathlib import Path
from xml.parsers.expat import ExpatError

from .. import chat_archive, config, events, lane_io, paths, state
from . import BUDGET_SPENT, FAILED, NOTES, failure

MAC_EPOCH = 978307200
STORE = Path("Library") / "Group Containers" / "group.com.apple.usernoted" / "db2" / "db"
SUMMARY_CHARS = 200
NAME_LEAD = re.compile(r"^(\S[^:\n]{0,78}[^\s:]): ")
QUERY = ("SELECT record.rec_id, app.identifier, record.uuid, record.delivered_date, record.data FROM record "
         "JOIN app ON app.app_id = record.app_id "
         "WHERE record.delivered_date IS NOT NULL AND app.identifier IN ({}) ORDER BY record.rec_id")
FAILED_RETRIED = False
PENDING = "pending"
ARCHIVE = "archive"
ARCHIVE_REF = "archive_"
NOT_IN_ARCHIVE = "not in archive"
OFF = "off"
FALLBACK_SEC = 300
WAKE_FILE = ".wake"
TS_SHAPE = re.compile(r"[0-9]{1,20}(\.[0-9]{1,9})?")
EVENT_KINDS = {"channel": "channel", "group": "dm", "dm": "dm"}
ROW_KEYS = ("app", "subt", "kind", "sender", "conversation", "titl", "iden", "delivered")
_NO_ACCESS = ("unable to open", "authoriz", "not permitted")
_BAD_ROW = (ValueError, KeyError, TypeError, AttributeError, ExpatError)


def _no_access(path: Path, text: str) -> events.PollError:
    try:
        os.stat(path)
    except FileNotFoundError:
        return events.PollError(f"no macOS notification store at {path}: {text}")
    except OSError:
        pass
    who = "the terminal app the manager runs in" if lane_io.lanes_in_manager() else os.path.realpath(sys.executable)
    return events.PollError(f"cannot read the macOS notification store: grant Full Disk Access to {who} ({text})")


def _poll_error(err: sqlite3.Error, path: Path) -> events.PollError:
    text = str(err)
    if "no such" in text:
        return events.PollError(f"notification store schema changed: {text}")
    if any(marker in text for marker in _NO_ACCESS):
        return _no_access(path, text)
    return events.PollError(f"cannot read the macOS notification store: {text}")


def default_call(apps: list[str]) -> list[tuple[int, str, bytes, float, bytes]]:
    path = Path.home() / STORE
    try:
        con = sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True)
    except sqlite3.Error as err:
        raise _poll_error(err, path) from err
    try:
        return list(con.execute(QUERY.format(", ".join("?" * len(apps))), list(apps)))
    except sqlite3.Error as err:
        raise _poll_error(err, path) from err
    finally:
        con.close()


def _name_lead(body: str) -> str:
    found = NAME_LEAD.match(body)
    if not found:
        return ""
    lead = found.group(1)
    return lead if lead[0].isupper() and " " in lead else ""


def _row(app: str, delivered: float, data: bytes) -> dict:
    req = plistlib.loads(data)["req"]
    subt = req.get("subt")
    if not isinstance(subt, str) or not subt:
        raise ValueError("no subtitle")
    lead = _name_lead(str(req.get("body") or ""))
    if subt.startswith("#"):
        kind, sender, conversation = "channel", lead, subt
    elif lead and not any(c.isspace() or c.isupper() for c in subt):
        kind, sender, conversation = "channel", lead, "#" + subt
    else:
        kind, sender, conversation = "dm", subt, subt
    return {"app": app, "subt": subt, "kind": kind, "sender": sender, "conversation": conversation,
            "titl": str(req.get("titl") or ""), "iden": str(req.get("iden") or ""), "delivered": delivered + MAC_EPOCH}


def _summary(kind: str, sender: str, conversation: str, iden: str, *marks: str) -> str:
    if kind.startswith("channel"):
        head = f"{sender} in {conversation}" if sender else conversation
    else:
        head = sender
    tail = "".join(f" · {mark}" for mark in marks)
    if iden:
        tail = f"{tail} · ref {iden}"
    return (head[:max(0, SUMMARY_CHARS - len(tail))] + tail)[:SUMMARY_CHARS]


def _fallback(key: str, row: dict, now: float, scout: str) -> events.Event:
    match = {"app": row["app"], "conversation": row["conversation"], "sender": row["sender"], "channel": "",
             "thread": ""}
    return events.Event(f"chat:{key}", now, "chat", row["kind"], match, events.chat_time(row["delivered"]),
                        _summary(row["kind"], row["sender"], row["conversation"], row["iden"]),
                        {"title": row["titl"], "ref": row["iden"], "delivered": row["delivered"], "scout": scout})


def _message_ts(iden: str) -> str:
    return iden.split("_", 1)[1] if "_" in iden else iden


def _archive_event(rec: dict, app: str, now: float) -> events.Event:
    ts, thread, cid, name = rec["ts"], rec["thread_ts"], rec["conversation_id"], rec["name"]
    kind = EVENT_KINDS[rec["kind"]]
    marks = []
    if thread and thread != ts:
        kind = f"{kind}-thread"
        marks.append(f"thread {thread}")
    marks.append(f"channel {cid}")
    if rec["kind"] == "channel":
        conversation = "#" + name.lstrip("#") if name else ""
    else:
        conversation = name if rec["kind"] == "dm" else rec["members"]
    conversation = conversation or rec["sender"]
    sender = name if rec["kind"] == "dm" and name else rec["sender"]
    ref = ARCHIVE_REF + ts
    match = {"app": app, "conversation": conversation, "sender": sender, "channel": cid, "thread": thread}
    return events.Event(f"chat:msg:{cid}:{ts}", now, "chat", kind, match, events.chat_time(float(ts)),
                        _summary(kind, sender, conversation, ref, *marks),
                        {"title": "", "ref": ref, "delivered": float(ts), "sender_id": rec["sender_id"],
                         "link": rec["link"], "scout": "ok"})


def _marks(cursor: dict) -> tuple[float, float] | None:
    raw = cursor.get(ARCHIVE)
    if isinstance(raw, dict) and _number(raw.get("messages")) and _number(raw.get("resolved")):
        return float(raw["messages"]), float(raw["resolved"])
    return None


def _archive_rows(cursor: dict, now: float, self_id: str) -> tuple[list[dict], dict | None]:
    if not paths.CHAT_DB.exists():
        return [], None
    try:
        con = chat_archive.open_db(readonly=True)
        try:
            marks = _marks(cursor) or chat_archive.high_marks(con)
            rows = chat_archive.new_rows(*marks, now - events.retention_sec(), self_id, con=con)
        finally:
            con.close()
    except sqlite3.Error:
        return [], None
    return rows, {table: max([mark, *(r["captured_at"] for r in rows if r["table"] == table)])
                  for table, mark in zip(("messages", "resolved"), marks)}


def _shaped(rec: dict) -> bool:
    return bool(TS_SHAPE.fullmatch(rec["ts"])) and (not rec["thread_ts"] or bool(TS_SHAPE.fullmatch(rec["thread_ts"])))


def _ref_ts(event: events.Event) -> str:
    return _message_ts(str(event.payload.get("ref") or ""))


def _archive_events(cursor: dict, now: float, self_id: str,
                    app: str) -> tuple[list[events.Event], dict | None, set[str]]:
    logged = events.recent("chat", now)
    known_ids = {e.id for e in logged}
    resolved = [e for e in logged if e.match.get("channel")]
    old_ts = {_ref_ts(e) for e in resolved if e.id.endswith(":scout") or e.id.startswith("chat:catchup:")}
    records, marks = _archive_rows(cursor, now, self_id)
    out = [_archive_event(rec, app, now) for rec in records
           if _shaped(rec) and f"chat:msg:{rec['conversation_id']}:{rec['ts']}" not in known_ids
           and rec["ts"] not in old_ts]
    return out, marks, {_ref_ts(e) for e in [*resolved, *out]} - {""}


def _started_at() -> float | None:
    try:
        return paths.CHAT_STARTED.stat().st_mtime
    except OSError:
        return None


def _write_trigger(key: str, row: dict) -> None:
    ts = _message_ts(row["iden"])
    if not TS_SHAPE.fullmatch(ts):
        return
    body = {"key": key, "iden": row["iden"], "ts": ts, "subt": row["subt"], "kind": row["kind"],
            "delivered": row["delivered"], "tries": 0}
    state.write_json_atomic(paths.CHAT_TRIGGERS / f"{key.replace(':', '_')}.json", body)


def _toggle_wake() -> None:
    wake = paths.CHAT_TRIGGERS / WAKE_FILE
    try:
        if wake.exists():
            wake.unlink()
        else:
            wake.parent.mkdir(parents=True, exist_ok=True)
            wake.touch()
    except OSError:
        return


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _entry_ok(entry) -> bool:
    if not isinstance(entry, dict) or not isinstance(entry.get("row"), dict):
        return False
    row = entry["row"]
    return (_number(entry.get("first")) and all(isinstance(row.get(f), str) for f in ROW_KEYS if f != "delivered")
            and _number(row.get("delivered")))


def _pending_of(cursor: dict) -> tuple[dict, set[str]]:
    raw = cursor.get(PENDING)
    raw = raw if isinstance(raw, dict) else {}
    entries = {k: v for k, v in raw.items() if isinstance(k, str) and _entry_ok(v)}
    sent = {k for k, v in entries.items() if v.get("fallback_sent") is True}
    return {k: {"first": v["first"], "row": v["row"]} for k, v in entries.items() if k not in sent}, sent


def poll(subs: list[events.Sub], rules: dict[str, dict], cursor: dict, env: dict[str, str], now: float, call=None,
         until: float | None = None) -> tuple[list[events.Event], dict]:
    call = call or default_call
    apps = config.events_source("chat")["apps"]
    if not apps:
        raise events.PollError("[events.sources.chat] apps is empty: name the app bundle ids whose notifications to read")
    archive = config.chat_archive()
    reads = archive["enabled"] and bool(archive["self_user_id"])
    started = _started_at() if archive["enabled"] else None
    store_rows = call(apps)
    out: list[events.Event] = []
    marks, resolved_ts = None, set()
    if reads:
        out, marks, resolved_ts = _archive_events(cursor, now, archive["self_user_id"], apps[0])
    seen = {k for k in cursor.get("seen") or [] if isinstance(k, str)}
    pending, sent = _pending_of(cursor)
    failed: list[list] = []
    keys: list[str] = []
    for rec_id, app, uid, delivered, data in store_rows:
        try:
            key = f"{uid.hex()}:{delivered:.6f}"
        except _BAD_ROW:
            key = f"rec:{rec_id}"
        keys.append(key)
        if key in seen or key in pending or key in sent:
            continue
        try:
            row = _row(app, delivered, data)
        except _BAD_ROW as err:
            failed.append(failure(f"notification {key}", err))
            continue
        if started is not None and row["delivered"] > started:
            _write_trigger(key, row)
        pending[key] = {"first": now, "row": row}
    for key in list(pending):
        entry = pending[key]
        if not reads:
            out.append(_fallback(key, entry["row"], now, OFF))
        elif _message_ts(entry["row"]["iden"]) in resolved_ts:
            pass
        elif now - entry["first"] >= FALLBACK_SEC:
            out.append(_fallback(key, entry["row"], now, NOT_IN_ARCHIVE))
        else:
            continue
        del pending[key]
    if started is not None and pending:
        _toggle_wake()
    new_cursor = {k: v for k, v in cursor.items() if k not in (BUDGET_SPENT, FAILED, NOTES, "scout_last", "catchup")}
    new_cursor["seen"] = sorted(k for k in keys if k not in pending)
    new_cursor[PENDING] = pending
    if marks is not None:
        new_cursor[ARCHIVE] = marks
    new_cursor[BUDGET_SPENT] = []
    new_cursor[FAILED] = failed
    return sorted(out, key=lambda e: e.id), new_cursor
