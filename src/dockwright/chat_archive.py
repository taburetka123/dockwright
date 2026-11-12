from __future__ import annotations

import datetime
import getpass
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config, failure_streak, lane_io, paths
from .models import resolve_model

PROTOCOL = "2025-06-18"
CLIENT = {"name": "dockwright-chat-archive", "version": "1"}
NUDGE_TIMEOUT_SEC = 60
CLAUDE_TIMEOUT_SEC = 60
CLAUDE_TIMEOUT_PER_CALL_SEC = 30
PAGE_LIMIT = 40
SEARCH_BYTES = 40_000
THREAD_BYTES = 40_000
READ_BYTES = 1_000
BATCH_BYTES = 150_000
FILE_POINTER = "Error: result ("
CLAUDE_PROMPT = ("Call exactly these tools once each, in this order, with exactly these arguments, then answer done. "
                 "The calls are a JSON array on stdin, one object per call: tool, arguments.")

LEDGER_STALE_SEC = 900
TS_VALUE = re.compile(r"^\d{9,}(\.\d+)?$")
RELATIVE = re.compile(r"^(\d+)([dh])$")
SCHEMA = (
    "create table if not exists conversations (id text primary key, kind text, name text not null default '', "
    "members text not null default '', last_ts text, swept_at real, next_try real not null default 0, "
    "backoff real not null default 0)",
    "create table if not exists messages (conversation_id text not null, ts text not null, thread_ts text, "
    "sender_id text not null default '', sender text not null default '', bot integer not null default 0, "
    "text text not null default '', link text not null default '', source text not null default '', "
    "captured_at real, edited_at real, raw_ref text not null default '', contiguous integer not null default 0, "
    "primary key (conversation_id, ts))",
    "create index if not exists messages_by_ts on messages (ts)",
    "create virtual table if not exists messages_fts using fts5(text, content='messages', content_rowid='rowid')",
    "create trigger if not exists messages_ai after insert on messages begin "
    "insert into messages_fts (rowid, text) values (new.rowid, new.text); end",
    "create trigger if not exists messages_ad after delete on messages begin "
    "insert into messages_fts (messages_fts, rowid, text) values ('delete', old.rowid, old.text); end",
    "create trigger if not exists messages_au after update on messages begin "
    "insert into messages_fts (messages_fts, rowid, text) values ('delete', old.rowid, old.text); "
    "insert into messages_fts (rowid, text) values (new.rowid, new.text); end",
    "create table if not exists threads (conversation_id text not null, thread_ts text not null, last_reply_ts text, "
    "checked_at real, next_try real not null default 0, backoff real not null default 0, "
    "primary key (conversation_id, thread_ts))",
    "create table if not exists resolved (iden text primary key, ts text, conversation_id text, kind text, name text, "
    "thread_ts text, sender_id text, sender_name text, link text, resolved_at real)",
    "create table if not exists cursors (step text primary key, ts text, updated_at real)",
)
ADDED_COLUMNS = (("conversations", "gap_ts", "text"), ("conversations", "gap_cursor", "text"),
                 ("messages", "contiguous", "integer not null default 0"))
UPSERT_MESSAGE = (
    "insert into messages (conversation_id, ts, thread_ts, sender_id, sender, bot, text, link, source, captured_at, "
    "edited_at, raw_ref, contiguous) values (?, ?, nullif(?, ''), ?, ?, ?, ?, ?, ?, ?, null, ?, ?) "
    "on conflict (conversation_id, ts) do update set text = excluded.text, "
    "contiguous = max(contiguous, excluded.contiguous), "
    "thread_ts = coalesce(nullif(excluded.thread_ts, ''), thread_ts), "
    "sender_id = case when excluded.sender_id <> '' then excluded.sender_id else sender_id end, "
    "sender = case when excluded.sender <> '' then excluded.sender else sender end, "
    "link = case when excluded.link <> '' then excluded.link else link end, "
    "edited_at = case when text <> excluded.text then excluded.captured_at else edited_at end")
TOUCH_CONVERSATION = (
    "update conversations set kind = coalesce(kind, ?), name = case when name = '' then ? else name end, "
    "last_ts = case when last_ts is null or last_ts < ? then ? else last_ts end where id = ?")
PRUNE_MESSAGES = (
    "delete from messages where ts < :cut and not exists (select 1 from messages r "
    "where r.conversation_id = messages.conversation_id "
    "and coalesce(r.thread_ts, r.ts) = coalesce(messages.thread_ts, messages.ts) and r.ts >= :cut)")
PRUNE_THREADS = (
    "delete from threads where not exists (select 1 from messages m where m.conversation_id = threads.conversation_id "
    "and coalesce(m.thread_ts, m.ts) = threads.thread_ts)")
READ_ROWS = ("select m.conversation_id, c.kind, c.name, m.ts, m.sender_id, m.sender, m.text, m.thread_ts, "
             "m.edited_at, m.link from messages m left join conversations c on c.id = m.conversation_id")
SEARCH_ROWS = ("select m.conversation_id, c.name, m.ts, m.sender, m.text, m.thread_ts, m.link from messages_fts f "
               "join messages m on m.rowid = f.rowid left join conversations c on c.id = m.conversation_id "
               "where messages_fts match ?")
RAW_DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")

TEXT_FIELDS = ("messages", "results", "result")
CURSOR_TOKEN = (re.compile(r"`([^`]+)`"), re.compile(r'"([^"]+)"'))
PAGINATION_LINE = re.compile(r"^(There are more messages available\. To view the next page, use cursor: |"
                             r"For the next page of results use cursor |Pagination: More results available\. Use cursor: )")
HEADS = {
    "conversation": re.compile(r"^=== Message from .* at .* ===\s*$"),
    "search": re.compile(r"^### Result \d+ of \d+\s*$"),
    "thread": re.compile(r"^(=== THREAD PARENT MESSAGE ===|--- Reply \d+ of \d+ ---)\s*$"),
    "list": re.compile(r"^### "),
}
SEPARATORS = {"thread": re.compile(r"^=== THREAD REPLIES \(\d+ total\) ===\s*$"), "list": re.compile(r"^---\s*$")}
CONTEXT = re.compile(r"^Context (before|after):")
LABEL = re.compile(r"^(From|Time|Message TS|Message_ts|Channel|Permalink|Reply count|Participants|Text):\s*(.*)$")
THREAD_LINE = re.compile(r"^Thread: (\d+) repl(?:y|ies)(?: \(latest: ([^)]*)\))?\s*$")
REACTIONS_LINE = re.compile(r"^Reactions: \S.*\(\d+\)\s*$")
FILES_LINE = re.compile(r"^Files: .*\(ID: [A-Za-z0-9]+, .*\)\s*$")
PERSON = re.compile(r"^(?P<name>.*?)(?:\s*<[^>]*>)?\s*\((?:ID:\s*)?(?P<id>[A-Za-z0-9]+)\)\s*(?P<bot>\[BOT\])?\s*$")
EXTERNAL = re.compile(r"^(?P<name>.*?)(?:\s*<[^>]*>)?\s*\((?P<id>[A-Za-z0-9]+), external: [^)]*\)\s*$")
MESSAGE_HEAD = re.compile(r"^=== Message from (?P<who>.*) at .* ===\s*$")
CHANNEL_LABEL = re.compile(r"^(?P<label>.*?)\s*\((?:ID:\s*)?(?P<id>[A-Za-z0-9]+)\)\s*$")
PERMALINK = re.compile(r"\((https?://[^)\s]+)\)")
LIST_FIELD = re.compile(r"^- \*\*(?P<key>[^*]+):\*\*\s*(?P<value>.*)$")
GROUP_MEMBERS = "Group messaging with: "


def unwrap(result_text: str) -> tuple[str, str]:
    try:
        body = json.loads(result_text)
    except ValueError:
        return result_text, ""
    if not isinstance(body, dict):
        return result_text, ""
    text = next((body[f] for f in TEXT_FIELDS if isinstance(body.get(f), str)), "")
    page = body.get("pagination_info")
    return text, page if isinstance(page, str) else ""


def _cursor(pagination_info: str) -> str | None:
    for pattern in CURSOR_TOKEN:
        found = pattern.search(pagination_info)
        if found:
            return found.group(1)
    return None


def _kind_of(label: str) -> tuple[str | None, str]:
    label = label.strip()
    if label.startswith("#"):
        return "channel", label[1:]
    low = label.lower()
    if "group" in low:
        return "group", ""
    if low in ("dm", "direct message") or low.startswith("dm "):
        return "dm", ""
    return None, label


def _person(value: str) -> tuple[str, str, bool]:
    found = PERSON.match(value.strip())
    if not found:
        return value.strip(), "", False
    return found.group("name").strip(), found.group("id"), bool(found.group("bot"))


def _blocks(kind: str, text: str) -> tuple[list[list[str]], str, str]:
    head, separator = HEADS[kind], SEPARATORS.get(kind)
    blocks: list[list[str]] = []
    preamble: list[str] = []
    pagination = ""
    current: list[str] | None = None
    skipping = False
    for line in text.splitlines():
        if PAGINATION_LINE.match(line):
            pagination = line
            current, skipping = None, False
            continue
        if head.match(line):
            current, skipping = [line], False
            blocks.append(current)
            continue
        if separator is not None and separator.match(line):
            current, skipping = None, False
            continue
        if kind == "search" and CONTEXT.match(line):
            skipping = True
            continue
        if skipping:
            continue
        if current is None:
            preamble.append(line)
        else:
            current.append(line)
    return blocks, pagination, "\n".join(preamble)


def _message(kind: str, lines: list[str], base: dict) -> dict | None:
    out = {**base, "ts": None, "thread_ts": None, "sender_id": "", "sender": "", "bot": False, "text": "",
           "link": "", "reply_count": None, "latest_reply": None}
    body: list[str] = []
    marked = in_body = False
    head = MESSAGE_HEAD.match(lines[0])
    if head:
        out["sender"], out["sender_id"], out["bot"] = _person(head.group("who"))
    for line in lines[1:]:
        if in_body:
            mark = THREAD_LINE.match(line)
            if mark:
                out["reply_count"], out["latest_reply"], marked = int(mark.group(1)), mark.group(2), True
            elif not REACTIONS_LINE.match(line) and not FILES_LINE.match(line):
                body.append(line)
            continue
        label = LABEL.match(line)
        if not label:
            if line.strip():
                in_body = True
                body.append(line)
            continue
        key, value = label.group(1), label.group(2).strip()
        if key in ("Message TS", "Message_ts"):
            if TS_VALUE.match(value):
                out["ts"] = value
            in_body = kind != "search"
        elif key == "From":
            out["sender"], out["sender_id"], out["bot"] = _person(value)
        elif key == "Channel":
            found = CHANNEL_LABEL.match(value)
            if found:
                out["conversation_id"] = found.group("id")
                out["conversation_kind"], out["conversation_name"] = _kind_of(found.group("label"))
        elif key == "Permalink":
            found = PERMALINK.search(value)
            if found:
                out["link"] = found.group(1)
                parent = re.search(r"[?&]thread_ts=([0-9.]+)", found.group(1))
                if parent:
                    out["thread_ts"] = parent.group(1)
        elif key == "Reply count":
            if value.isdigit():
                out["reply_count"], marked = int(value), True
        elif key == "Text":
            in_body = True
            if value:
                body.append(value)
    if not out["ts"]:
        return None
    if marked and out["thread_ts"] is None:
        out["thread_ts"] = out["ts"]
    while body and (not body[-1].strip() or body[-1].strip() == "---"):
        body.pop()
    while body and not body[0].strip():
        body.pop(0)
    out["text"] = html.unescape("\n".join(body).strip())
    return out


def _conversation_entry(lines: list[str]) -> dict | None:
    fields = {}
    for line in lines[1:]:
        found = LIST_FIELD.match(line)
        if found:
            fields[found.group("key").strip()] = found.group("value").strip()
    ident = fields.get("ID")
    if not ident:
        return None
    kind, _ = _kind_of(fields.get("Type", lines[0].lstrip("# ").strip()))
    title = lines[0].lstrip("# ").strip()
    name = title[len("DM with "):] if title.startswith("DM with ") else ""
    members = fields.get("Members", "")
    if members.startswith(GROUP_MEMBERS):
        members = members[len(GROUP_MEMBERS):]
    return {"id": ident, "kind": kind, "name": name, "members": members or fields.get("User ID", "")}


def parse(kind: str, text: str, conversation_id: str | None = None, pagination_info: str = "") -> dict:
    blocks, page_line, preamble = _blocks(kind, text)
    out = {"messages": [], "conversations": [], "cursor": _cursor(pagination_info if pagination_info else page_line),
           "unparsed": 0}
    base = {"conversation_id": conversation_id, "conversation_kind": None, "conversation_name": ""}
    if kind == "conversation":
        channel = CHANNEL_LABEL.match(preamble.strip().removeprefix("Channel:").strip())
        if channel:
            base["conversation_kind"], base["conversation_name"] = _kind_of(channel.group("label"))
    parent_ts = None
    for lines in blocks:
        if kind == "list":
            entry = _conversation_entry(lines)
            if entry is None:
                out["unparsed"] += 1
            else:
                out["conversations"].append(entry)
            continue
        message = _message(kind, lines, base)
        if message is None:
            out["unparsed"] += 1
            continue
        if kind == "thread":
            if lines[0].startswith("=== THREAD PARENT"):
                parent_ts = message["ts"]
            message["thread_ts"] = parent_ts or message["ts"]
        out["messages"].append(message)
    return out


def _ts(value: float) -> str:
    return f"{value:.6f}"


def _day(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")


def _time(ts: str) -> str:
    return datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def open_db(path: Path | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    path = paths.CHAT_DB if path is None else path
    if readonly:
        con = sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True, isolation_level=None)
        con.row_factory = None
        return con
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    existed = path.exists()
    con = sqlite3.connect(str(path), isolation_level=None)
    if not existed:
        path.chmod(0o600)
    con.execute("pragma journal_mode=wal")
    for statement in SCHEMA:
        con.execute(statement)
    for table, column, kind in ADDED_COLUMNS:
        if column not in {r[1] for r in con.execute(f"pragma table_info({table})")}:
            con.execute(f"alter table {table} add column {column} {kind}")
    return con


def _raw_line(raw_dir: Path, now: float, tool: str, args: dict, text: str) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(raw_dir / f"{_day(now)}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps({"ts": now, "tool": tool, "args": args, "text": text}) + "\n").encode())
    finally:
        os.close(fd)


def record(con, raw_dir: Path, tool: str, args: dict, text: str, parsed: dict, source: str, now: float, *,
           keep_raw: bool, contiguous: bool = False) -> tuple[int, int]:
    if keep_raw:
        _raw_line(raw_dir, now, tool, args, text)
    new = updated = 0
    for message in parsed["messages"]:
        cid, ts = message["conversation_id"], message["ts"]
        if not cid or not ts:
            continue
        existing = con.execute("select text from messages where conversation_id = ? and ts = ?", (cid, ts)).fetchone()
        con.execute(UPSERT_MESSAGE, (cid, ts, message["thread_ts"] or "", message["sender_id"], message["sender"],
                                     int(bool(message["bot"])), message["text"], message["link"], source, now,
                                     f"{_day(now)}.jsonl" if keep_raw else "", int(contiguous)))
        if existing is None:
            new += 1
        elif existing[0] != message["text"]:
            updated += 1
        con.execute("insert or ignore into conversations (id, kind, name, members, last_ts, swept_at) "
                    "values (?, ?, ?, '', ?, null)", (cid, message["conversation_kind"], message["conversation_name"], ts))
        con.execute(TOUCH_CONVERSATION, (message["conversation_kind"], message["conversation_name"], ts, ts, cid))
    return new, updated


def when(value: str, now: float, *, tz: datetime.tzinfo | None = None) -> str:
    value = value.strip()
    if TS_VALUE.match(value):
        return value if "." in value else _ts(float(value))
    relative = RELATIVE.match(value)
    if relative:
        unit = 86400 if relative.group(2) == "d" else 3600
        return _ts(now - int(relative.group(1)) * unit)
    zone = tz or datetime.datetime.now().astimezone().tzinfo
    moment = datetime.datetime.fromisoformat(value)
    if len(value) == 10:
        moment = datetime.datetime.combine(moment.date(), datetime.time())
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return _ts(moment.timestamp())


def archive_state(con, now: float) -> dict:
    started = con.execute("select ts from cursors where step = 'started'").fetchone() if con is not None else None
    since = _time(started[0]) if started and started[0] else None
    if not config.chat_archive()["enabled"]:
        return {"archive": "off", "since": since}
    if con is None or con.execute("select count(*) from messages").fetchone()[0] == 0:
        return {"archive": "empty", "since": since}
    cfg = config.chat_archive()
    steps = ["dm", "threads", *(f"mention:{key}" for key in cfg["mention_keys"])]
    marks = dict(con.execute(f"select step, updated_at from cursors where step in ({','.join('?' * len(steps))})", steps))
    window = max(LEDGER_STALE_SEC, 3 * cfg["every_sec"])
    fresh = all(marks.get(step) for step in steps) and now - min(marks.values()) <= window
    return {"archive": "ok" if fresh else "stale", "since": since}


def _with_db(fn, empty: dict, con, now):
    now = time.time() if now is None else now
    if con is not None:
        return {**fn(con, now), **archive_state(con, now)}
    if not paths.CHAT_DB.exists():
        return {**empty, **archive_state(None, now)}
    con = open_db(readonly=True)
    try:
        state = archive_state(con, now)
        if state["archive"] == "empty":
            return {**empty, **state}
        return {**fn(con, now), **state}
    finally:
        con.close()


def read(after: str, before: str | None = None, conversation: str | None = None, thread: str | None = None,
         bots: bool = True, limit: int = 500, *, con=None, now: float | None = None) -> dict:
    def run(con, now):
        where, args = ["m.ts >= ?"], [when(after, now)]
        if before is not None:
            where.append("m.ts < ?")
            args.append(when(before, now))
        if conversation:
            where.append("m.conversation_id = ?")
            args.append(conversation)
        if thread:
            where.append("coalesce(m.thread_ts, m.ts) = ?")
            args.append(thread)
        if not bots:
            where.append("m.bot = 0")
        rows = con.execute(f"{READ_ROWS} where {' and '.join(where)} order by m.ts desc limit ?",
                           [*args, limit + 1]).fetchall()
        truncated = len(rows) > limit
        grouped: dict[str, dict] = {}
        for cid, kind, name, ts, sender_id, sender, text, thread_ts, edited_at, link in reversed(rows[:limit]):
            entry = grouped.setdefault(cid, {"id": cid, "kind": kind, "name": name or "", "messages": []})
            entry["messages"].append({"ts": ts, "time": _time(ts), "sender_id": sender_id, "sender": sender, "text": text,
                                      "thread_ts": thread_ts, "edited": edited_at is not None, "link": link})
        return {"conversations": list(grouped.values()), "truncated": truncated}
    return _with_db(run, {"conversations": [], "truncated": False}, con, now)


def search(query: str, after=None, before=None, conversation=None, sender=None, limit: int = 50, *, con=None,
           now: float | None = None) -> dict:
    def run(con, now):
        where, args = [], [query]
        if after is not None:
            where.append("m.ts >= ?")
            args.append(when(after, now))
        if before is not None:
            where.append("m.ts < ?")
            args.append(when(before, now))
        if conversation:
            where.append("m.conversation_id = ?")
            args.append(conversation)
        if sender:
            where.append("(m.sender_id = ? or m.sender = ?)")
            args += [sender, sender]
        sql = SEARCH_ROWS + "".join(f" and {w}" for w in where) + " order by bm25(messages_fts) limit ?"
        try:
            rows = con.execute(sql, [*args, limit]).fetchall()
        except sqlite3.OperationalError as err:
            return {"hits": [], "error": str(err)}
        return {"hits": [{"conversation": cid, "name": name or "", "ts": ts, "time": _time(ts), "sender": sender_name,
                          "text": text, "thread_ts": thread_ts, "link": link}
                         for cid, name, ts, sender_name, text, thread_ts, link in rows]}
    return _with_db(run, {"hits": []}, con, now)


def conversations(kind: str | None = None, since: str | None = None, *, con=None, now: float | None = None) -> dict:
    def run(con, now):
        where, args = [], []
        if kind:
            where.append("c.kind = ?")
            args.append(kind)
        if since:
            where.append("c.last_ts >= ?")
            args.append(when(since, now))
        sql = ("select c.id, c.kind, c.name, c.last_ts, (select count(*) from messages m where m.conversation_id = c.id) "
               "from conversations c" + (" where " + " and ".join(where) if where else "") + " order by c.last_ts desc")
        return {"conversations": [{"id": cid, "kind": k, "name": name, "last_ts": last_ts, "messages": count}
                                  for cid, k, name, last_ts, count in con.execute(sql, args)]}
    return _with_db(run, {"conversations": []}, con, now)


def high_marks(con) -> tuple[float, float]:
    messages = con.execute("select max(captured_at) from messages").fetchone()[0]
    resolved = con.execute("select max(resolved_at) from resolved").fetchone()[0]
    return float(messages or 0.0), float(resolved or 0.0)


def _bot(sender_id: str, known: dict, con) -> bool:
    if not sender_id:
        return False
    if sender_id not in known:
        known[sender_id] = sender_id.startswith("B") or con.execute(
            "select 1 from messages where sender_id = ? and bot = 1 limit 1", (sender_id,)).fetchone() is not None
    return known[sender_id]


def _sender(sender_id, name) -> tuple[str, str]:
    found = EXTERNAL.match(name or "")
    if found:
        return sender_id or found.group("id"), found.group("name").strip()
    return sender_id or "", name or ""


def new_rows(since_messages: float, since_resolved: float, floor_ts: float, self_id: str, *, con) -> list[dict]:
    pairs = set(con.execute("select conversation_id, ts from messages where captured_at >= ? "
                            "and cast(ts as real) >= ?", (since_messages, floor_ts)))
    pairs |= set(con.execute("select conversation_id, ts from resolved where resolved_at >= ? "
                             "and conversation_id is not null and cast(ts as real) >= ?", (since_resolved, floor_ts)))
    bots: dict = {}
    out = []
    for cid, ts in pairs:
        message = con.execute("select thread_ts, sender_id, sender, link, captured_at from messages "
                              "where conversation_id = ? and ts = ?", (cid, ts)).fetchone()
        twins = con.execute("select kind, name, thread_ts, sender_id, sender_name, link, resolved_at from resolved "
                            "where conversation_id = ? and ts = ? order by resolved_at desc", (cid, ts)).fetchall()
        if message:
            message = (message[0], *_sender(message[1], message[2]), *message[3:])
        twins = [(*twin[:3], *_sender(twin[3], twin[4]), *twin[5:]) for twin in twins]
        senders = [message[1]] if message else []
        senders += [twin[3] for twin in twins]
        if any((self_id and sender == self_id) or _bot(sender, bots, con) for sender in senders):
            continue
        conversation = con.execute("select kind, name, members from conversations where id = ?", (cid,)).fetchone()
        twin = twins[0] if twins else (None,) * 7
        kind, name, members = conversation or (twin[0], twin[1], "")
        if kind not in ("dm", "group", "channel"):
            continue
        thread_ts, sender_id, sender, link, captured_at = message or (None, "", "", "", twin[6])
        out.append({"conversation_id": cid, "kind": kind, "name": name or "", "members": members or "", "ts": ts,
                    "thread_ts": thread_ts or twin[2] or "", "sender_id": sender_id or twin[3] or "",
                    "sender": sender or twin[4] or "", "link": link or twin[5] or "",
                    "captured_at": float(captured_at or 0.0), "table": "messages" if message else "resolved"})
    return sorted(out, key=lambda r: (r["captured_at"], r["ts"]))


def _readonly(fn, *args, miss):
    if not paths.CHAT_DB.exists():
        return miss
    try:
        con = open_db(readonly=True)
    except sqlite3.Error:
        return miss
    try:
        return fn(*args, con=con)
    except sqlite3.Error:
        return miss
    finally:
        con.close()


def taken_over(channel: str, after_ts: str, before_ts: str, footer: str, self_user_id: str, *, con=None) -> bool:
    if con is None:
        return _readonly(taken_over, channel, after_ts, before_ts, footer, self_user_id, miss=False)
    hit = con.execute("select 1 from messages where conversation_id = ? and sender_id = ? and cast(ts as real) > ? "
                      "and cast(ts as real) < ? and text not like ? limit 1",
                      (channel, self_user_id, float(after_ts), float(before_ts), "%" + footer)).fetchone()
    return hit is not None


def dm_closed(channel: str, msg_ts: str, sender_id: str, self_user_id: str, footer: str, skip_senders: list[str], *,
              con=None) -> str | None:
    if sender_id in skip_senders:
        return "skip"
    if con is None:
        return _readonly(dm_closed, channel, msg_ts, sender_id, self_user_id, footer, skip_senders, miss=None)
    if taken_over(channel, msg_ts, str(time.time()), footer, self_user_id, con=con):
        return "answered"
    return None


def sent_by_footer(channel: str, thread: str | None, before_ts: str, footer: str, self_user_id: str, *,
                   con=None) -> dict | None:
    if con is None:
        return _readonly(sent_by_footer, channel, thread, before_ts, footer, self_user_id, miss=None)
    where = "thread_ts = ?" if thread else "(thread_ts is null or thread_ts = '' or thread_ts = ts)"
    args = (channel, self_user_id, "%" + footer, *((thread,) if thread else ()), float(before_ts))
    row = con.execute(f"select ts, thread_ts from messages where conversation_id = ? and sender_id = ? and text like ? "
                      f"and {where} and cast(ts as real) < ? order by cast(ts as real) desc limit 1", args).fetchone()
    return None if row is None else {"ts": row[0], "thread": row[1] or ""}


def prune(con, now: float, retention_days: int, raw_dir: Path) -> dict:
    cut = _ts(now - retention_days * 86400)
    messages = con.execute(PRUNE_MESSAGES, {"cut": cut}).rowcount
    threads = con.execute(PRUNE_THREADS).rowcount
    resolved = con.execute("delete from resolved where ts < ?", (cut,)).rowcount
    raw_files = 0
    cut_day = _day(now - retention_days * 86400)
    for path in raw_dir.glob("*.jsonl") if raw_dir.is_dir() else []:
        found = RAW_DAY.match(path.name)
        if found and found.group(1) < cut_day:
            path.unlink()
            raw_files += 1
    return {"messages": messages, "threads": threads, "resolved": resolved, "raw_files": raw_files}


class HandError(Exception):
    def __init__(self, reason: str, *, status: int | None = None, tool: str = ""):
        super().__init__(reason)
        self.reason, self.status, self.tool = reason, status, tool

    def __str__(self) -> str:
        return f"{self.reason} {self.status}" if self.status is not None else self.reason


class AuthError(HandError):
    pass


class RateLimited(HandError):
    def __init__(self, reason: str, *, status: int | None = None, tool: str = "", retry_after: float = 1.0):
        super().__init__(reason, status=status, tool=tool)
        self.retry_after = retry_after


class _Unauthorized(Exception):
    pass


ITEM_REASONS = ("ToolError", "JSONRPCError", "ValueError")
BACKOFF_MIN_SEC = 600
BACKOFF_MAX_SEC = 86400
BACKOFF_SQL = f"min(max(backoff * 2, {BACKOFF_MIN_SEC}), {BACKOFF_MAX_SEC})"


def item_failure(err: HandError) -> bool:
    if isinstance(err, (AuthError, RateLimited)):
        return False
    if err.tool == "claude":
        return err.reason.endswith("oversized")
    if err.status is not None:
        return 400 <= err.status < 500
    return err.reason in ITEM_REASONS


def _role_args() -> list[str]:
    from . import roles
    try:
        files = roles.launch_files("Blank", "lane")
    except roles.RoleError as err:
        print(f"chat archive: role Blank unavailable ({err}); the hand uses the GenericWorker lane render",
              file=sys.stderr)
        try:
            files = roles.launch_files("GenericWorker", "lane")
        except roles.RoleError as err:
            print(f"chat archive: runs without a role render: {err}", file=sys.stderr)
            return []
    return [] if files is None else ["--append-system-prompt-file", str(files[0])]


def _full_tool(cfg: dict, tool: str) -> str:
    return f"mcp__{cfg['mcp_server']}__{cfg['tools'].get(tool) or tool}"


def _lockdown(cfg: dict, allowed: list[str], max_turns: int, output_format: str) -> list[str]:
    mcp = {"mcpServers": {cfg["mcp_server"]: {"type": "http", "url": cfg["mcp_url"]}}}
    return ["--model", resolve_model(config.scout_model()), "--effort", "low", "--max-turns", str(max_turns),
            "--output-format", output_format, "--no-session-persistence", "--tools", "", "--strict-mcp-config",
            "--mcp-config", json.dumps(mcp), "--allowedTools", ",".join(allowed),
            "--disallowedTools", ",".join(f"mcp__{cfg['mcp_server']}__{w}" for w in cfg["write_tools"]),
            "--setting-sources", "", *_role_args()]


CLAUDE_ENV_DROPPED = (*paths.ORCHESTRATOR_ENV_KEYS, "CLAUDE_CONFIG_DIR")
NUDGE_EVERY_SEC = 3600
NUDGE_MARK = "nudged"


def _claude_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in CLAUDE_ENV_DROPPED}


def _expires_at(entry) -> float:
    value = entry.get("expiresAt") if isinstance(entry, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def keychain_items(cfg: dict) -> list[tuple[str, str]]:
    base, default = cfg["keychain_service"], config.default_account()
    others = [(a.name, f"{base}-{hashlib.sha256(str(paths.account_config_dir(a.name)).encode()).hexdigest()[:8]}")
              for a in config.accounts() if a.name != default]
    return [(default, base), *others]


class DirectHand:
    def __init__(self, cfg: dict, *, run=subprocess.run, urlopen=urllib.request.urlopen, now=time.time):
        self.cfg, self.run, self.urlopen, self.now = cfg, run, urlopen, now
        self.token_key = "none"
        self.token_account = "none"
        self.auth_retries = 0
        self._token: str | None = None
        self._tried: set[str] = set()
        self._session: str | None = None
        self._initialized = False
        self._next_id = 1

    def _read_item(self, service: str) -> dict:
        try:
            done = self.run(["security", "find-generic-password", "-s", service, "-a", getpass.getuser(), "-w"],
                            capture_output=True, check=False, timeout=NUDGE_TIMEOUT_SEC)
        except Exception as err:
            raise AuthError(f"keychain: {type(err).__name__}") from None
        if done.returncode != 0:
            raise AuthError("keychain: CalledProcessError")
        try:
            data = json.loads(done.stdout.decode("utf-8", errors="replace"))
        except ValueError:
            raise AuthError("keychain: ValueError") from None
        return data if isinstance(data, dict) else {}

    def _candidates(self) -> list[tuple[str, str, str]]:
        server = self.cfg["mcp_server"]
        found: list[tuple[float, str, str, str]] = []
        failure: AuthError | None = None
        read_any = False
        for account, service in keychain_items(self.cfg):
            try:
                data = self._read_item(service)
            except AuthError as err:
                failure = failure or err
                continue
            read_any = True
            entries = data.get("mcpOAuth")
            entries = entries if isinstance(entries, dict) else {}
            found += [(_expires_at(v), account, k, v["accessToken"]) for k, v in entries.items()
                      if isinstance(k, str) and (k == server or k.startswith(f"{server}|")) and isinstance(v, dict)
                      and isinstance(v.get("accessToken"), str) and v["accessToken"]]
        if not found:
            if not read_any and failure is not None:
                raise failure
            raise AuthError(f"keychain: no entry for {server}")
        found.sort(key=lambda c: c[0], reverse=True)
        return [(account, key, token) for _, account, key, token in found]

    def _take(self, candidate: tuple[str, str, str]) -> tuple[str, str]:
        self.token_account, self.token_key, self._token = candidate
        return candidate[2], candidate[1]

    def token(self) -> tuple[str, str]:
        return self._take(self._candidates()[0])

    def _next_token(self) -> bool:
        fresh = next((c for c in self._candidates() if c[2] not in self._tried), None)
        if fresh is None:
            return False
        self._take(fresh)
        return True

    def _post(self, payload: dict, tool: str) -> dict | None:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Authorization": f"Bearer {self._token}", "MCP-Protocol-Version": PROTOCOL}
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        request = urllib.request.Request(self.cfg["mcp_url"], data=json.dumps(payload).encode(), headers=headers,
                                         method="POST")
        try:
            with self.urlopen(request, timeout=NUDGE_TIMEOUT_SEC) as response:
                body = response.read()
                content_type = response.headers.get("Content-Type") or ""
                session = response.headers.get("Mcp-Session-Id")
        except urllib.error.HTTPError as err:
            if err.code == 401:
                raise _Unauthorized() from None
            if err.code == 429:
                retry = err.headers.get("Retry-After") if err.headers is not None else None
                try:
                    wait = float(retry)
                except (TypeError, ValueError):
                    wait = 1.0
                raise RateLimited("HTTPError", status=429, tool=tool, retry_after=wait) from None
            raise HandError("HTTPError", status=err.code, tool=tool) from None
        except Exception as err:
            raise HandError(type(err).__name__, tool=tool) from None
        if session:
            self._session = session
        if "id" not in payload:
            return None
        text = body.decode("utf-8", errors="replace")
        if "text/event-stream" in content_type:
            messages = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
            text = next((m for m in messages if m), "")
        try:
            parsed = json.loads(text) if text.strip() else {}
        except ValueError:
            raise HandError("ValueError", tool=tool) from None
        return parsed if isinstance(parsed, dict) else {}

    def _rpc(self, method: str, params: dict, tool: str, *, notify: bool = False) -> dict | None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            payload["id"] = self._next_id
            self._next_id += 1
        return self._post(payload, tool)

    def _ensure_session(self, tool: str) -> None:
        if self._initialized:
            return
        self._rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT}, tool)
        self._rpc("notifications/initialized", {}, tool, notify=True)
        self._initialized = True

    def _call_once(self, tool: str, args: dict) -> str:
        if self._token is None:
            self.token()
        self._ensure_session(tool)
        reply = self._rpc("tools/call", {"name": self.cfg["tools"].get(tool) or tool, "arguments": args}, tool) or {}
        if "error" in reply:
            raise HandError("JSONRPCError", tool=tool)
        result = reply.get("result") if isinstance(reply.get("result"), dict) else {}
        if result.get("isError"):
            raise HandError("ToolError", tool=tool)
        content = result.get("content") if isinstance(result.get("content"), list) else []
        return "\n".join(c["text"] for c in content if isinstance(c, dict) and isinstance(c.get("text"), str))

    def call(self, tool: str, args: dict) -> str:
        try:
            return self._call_once(tool, args)
        except _Unauthorized:
            pass
        nudged = False
        while True:
            self._session, self._initialized = None, False
            self._tried.add(self._token)
            if not self._next_token():
                if nudged or not self._nudge_due():
                    raise AuthError("AuthError", tool=tool) from None
                nudged = True
                self.nudge()
                if not self._next_token():
                    raise AuthError("AuthError", tool=tool) from None
            self.auth_retries += 1
            try:
                return self._call_once(tool, args)
            except _Unauthorized:
                continue

    def batch(self, calls: list[tuple[str, dict]]) -> list:
        out: list = []
        for tool, args in calls:
            try:
                out.append(self.call(tool, args))
            except HandError as err:
                if not item_failure(err):
                    raise
                out.append(err)
        return out

    def _nudge_due(self) -> bool:
        mark = paths.CHAT / NUDGE_MARK
        now = self.now()
        try:
            if now - mark.stat().st_mtime < NUDGE_EVERY_SEC:
                return False
        except OSError:
            pass
        try:
            mark.parent.mkdir(parents=True, exist_ok=True)
            mark.touch()
            os.utime(mark, (now, now))
        except OSError:
            pass
        return True

    def nudge(self) -> bool:
        cfg = self.cfg
        prompt = (f"Call the tool {cfg['tools']['list_conversations']} once with types=im and limit=1, "
                  "then answer done.")
        argv = [shutil.which("claude") or "claude", "-p", prompt,
                *_lockdown(cfg, [_full_tool(cfg, "list_conversations")], 2, "json")]
        try:
            done = self.run(argv, capture_output=True, check=False, timeout=NUDGE_TIMEOUT_SEC, env=_claude_env(),
                            stdin=subprocess.DEVNULL)
        except Exception:
            return False
        return done.returncode == 0


def _tool_pairs(stdout: bytes) -> list[tuple[str, dict, str]]:
    uses: dict[str, tuple[str, dict]] = {}
    order: list[str] = []
    results: dict[str, str] = {}
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = event.get("message") if isinstance(event, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use" and isinstance(item.get("id"), str):
                uses[item["id"]] = (str(item.get("name") or ""), item.get("input") if isinstance(item.get("input"), dict) else {})
                order.append(item["id"])
            elif item.get("type") == "tool_result" and isinstance(item.get("tool_use_id"), str):
                raw = item.get("content")
                if isinstance(raw, list):
                    raw = "\n".join(c.get("text", "") for c in raw if isinstance(c, dict))
                results[item["tool_use_id"]] = raw if isinstance(raw, str) else ""
    return [(uses[i][0], uses[i][1], results.get(i, "")) for i in order]


def _expected_bytes(tool: str, args: dict) -> int:
    if tool == "search":
        return SEARCH_BYTES
    if tool == "read_thread":
        return THREAD_BYTES
    limit = args.get("limit")
    return (limit if isinstance(limit, int) and not isinstance(limit, bool) else PAGE_LIMIT) * READ_BYTES


class ClaudeHand:
    def __init__(self, cfg: dict, *, run=subprocess.run):
        self.cfg, self.run = cfg, run
        self.token_key = "claude"

    def _run(self, calls: list[tuple[str, dict]]) -> list[str]:
        cfg = self.cfg
        allowed = [_full_tool(cfg, t) for t in ("search", "read_conversation", "read_thread", "list_conversations")]
        argv = [shutil.which("claude") or "claude", "-p", CLAUDE_PROMPT,
                *_lockdown(cfg, allowed, len(calls) + 1, "stream-json"), "--verbose"]
        order = [{"tool": _full_tool(cfg, tool), "arguments": args} for tool, args in calls]
        try:
            done = self.run(argv, input=json.dumps(order).encode(), capture_output=True, check=False,
                            timeout=CLAUDE_TIMEOUT_SEC + CLAUDE_TIMEOUT_PER_CALL_SEC * len(calls), env=_claude_env())
        except Exception as err:
            raise HandError(type(err).__name__, tool="claude") from None
        if done.returncode != 0:
            raise HandError("CalledProcessError", tool="claude")
        pairs = _tool_pairs(done.stdout or b"")
        out = []
        for n, wanted in enumerate(order):
            if n >= len(pairs) or pairs[n][0] != wanted["tool"] or pairs[n][1] != wanted["arguments"]:
                raise HandError(f"hand: call {n + 1} missing", tool="claude")
            out.append(pairs[n][2])
        return out

    def batch(self, calls: list[tuple[str, dict]]) -> list[str]:
        out: list[str] = []
        chunk: list[tuple[str, dict]] = []
        size = 0
        for call in calls:
            expected = _expected_bytes(*call)
            if chunk and size + expected > BATCH_BYTES:
                out += self._run(chunk)
                chunk, size = [], 0
            chunk.append(call)
            size += expected
        if chunk:
            out += self._run(chunk)
        for n, (text, (tool, args)) in enumerate(zip(out, calls)):
            if text.startswith(FILE_POINTER):
                limit = args.get("limit")
                smaller = {**args, "limit": (limit if isinstance(limit, int) else PAGE_LIMIT) // 2}
                out[n] = self._run([(tool, smaller)])[0]
                if out[n].startswith(FILE_POINTER):
                    out[n] = HandError(f"hand: call {n + 1} oversized", tool="claude")
        return out

    def call(self, tool: str, args: dict) -> str:
        got = self.batch([(tool, args)])[0]
        if isinstance(got, HandError):
            raise got
        return got


def hand_for(cfg: dict, **kw) -> DirectHand | ClaudeHand:
    if cfg["hand"] == "claude":
        return ClaudeHand(cfg, **{k: v for k, v in kw.items() if k == "run"})
    return DirectHand(cfg, **kw)


MAX_PAGES = 10
TRIGGER_RETRY_SEC = 60
TRIGGER_TTL_SEC = 3600
WINDOW_BEFORE = 1.0
WINDOW_AFTER = 1.5
THREAD_LIMIT = 1000
SEARCH_LIMIT = 20
LIST_LIMIT = 200
SEARCHES_PER_MIN = 15
READS_PER_MIN = 45
LIVE_THREAD_DAYS = 7
PAGE_EVERY_SEC = 3600
STREAK_FILE = "failures.json"
STREAK_KEYS = ("http", "hand", "keychain")
RATE_WINDOW_SEC = 60
RATE_LIMIT_TRIES = 3
WAKE_FILE = ".wake"
TRIGGER_FIELDS = ("key", "iden", "ts", "subt", "kind", "delivered", "tries")
SEARCH_ARGS = {"sort": "timestamp", "response_format": "detailed", "include_context": False, "include_bots": True,
               "limit": SEARCH_LIMIT}
CURSOR_SEARCH_ARGS = {**SEARCH_ARGS, "sort_dir": "asc"}
LEDGER_KEEP = 5000


def _day_before(ts: float) -> str:
    return _day(ts - 86400)


def _key_in(text: str, keys: list[str]) -> bool:
    return any(key and key in text for key in keys)


def page(kind: str, failure: str, now: float, managers=None) -> bool:
    from .daemon import OutboxEmit, _managers
    from .lane_lines import render
    from .pipeline_reconcile import _Pages
    sent = False
    for manager in _managers() if managers is None else managers:
        if _Pages(manager["name"]).throttled(("chat-archive", kind), PAGE_EVERY_SEC, now):
            continue
        OutboxEmit(manager["name"], source="chat-archive").emit(render("CHAT_ARCHIVE_FAILED", failure_kind=kind,
                                                                       failure=failure))
        sent = True
    return sent


class _Skip(Exception):
    pass


class _NotFound(HandError):
    def __init__(self):
        super().__init__("not found", tool="search")


class _Calls:
    def __init__(self, hand, record: dict, clock: float, sleep):
        self.hand, self.record, self.clock, self.sleep = hand, record, clock, sleep
        self.windows: dict[str, list[float]] = {"search": [], "read": []}

    def _cap(self, tool: str) -> None:
        cls = "search" if tool == "search" else "read"
        cap = SEARCHES_PER_MIN if cls == "search" else READS_PER_MIN
        window = [t for t in self.windows[cls] if self.clock - t < RATE_WINDOW_SEC]
        if len(window) >= cap:
            wait = window[0] + RATE_WINDOW_SEC - self.clock
            if wait > 0:
                self.sleep(wait)
                self.clock += wait
            window = [t for t in window if self.clock - t < RATE_WINDOW_SEC]
        window.append(self.clock)
        self.windows[cls] = window

    def call(self, tool: str, args: dict) -> str:
        limited = 0
        while True:
            self._cap(tool)
            self.record["calls"] += 1
            if tool == "search":
                self.record["searches"] += 1
            try:
                return self.hand.call(tool, args)
            except RateLimited as err:
                limited += 1
                if limited >= RATE_LIMIT_TRIES:
                    raise
                self.sleep(err.retry_after)
                self.clock += err.retry_after

    def batch(self, calls: list[tuple[str, dict]]) -> list[str]:
        if not calls:
            return []
        limited = 0
        while True:
            for tool, _args in calls:
                self._cap(tool)
                self.record["calls"] += 1
                if tool == "search":
                    self.record["searches"] += 1
            try:
                return self.hand.batch(calls)
            except RateLimited as err:
                limited += 1
                if limited >= RATE_LIMIT_TRIES:
                    raise
                self.sleep(err.retry_after)
                self.clock += err.retry_after


def _page_for(err: HandError) -> str | None:
    if isinstance(err, AuthError):
        return "auth"
    if isinstance(err, RateLimited):
        return None
    return "hand" if err.tool == "claude" else "http"


def _streak_key(err: HandError, kind: str | None) -> str | None:
    if kind == "auth":
        return "keychain" if str(err).startswith("keychain:") else None
    return kind if kind in ("http", "hand") else None


class _Pass:
    def __init__(self, cfg: dict, con, calls: _Calls, now: float, record: dict, managers, raw_dir: Path):
        self.cfg, self.con, self.calls, self.now, self.record, self.managers, self.raw_dir = (
            cfg, con, calls, now, record, managers, raw_dir)
        self.cursors = dict(con.execute("select step, ts from cursors"))
        self.floor = self.cursors.get("started")
        self.failed: dict[str, tuple[str, str]] = {}
        self.step_key: str | None = None
        self.failing_untried = False

    def cursor(self, step: str) -> float:
        return float(self.cursors.get(step) or self.floor)

    def set_cursor(self, step: str, ts: str) -> None:
        self.con.execute("insert into cursors (step, ts, updated_at) values (?, ?, ?) on conflict (step) do update set "
                         "ts = excluded.ts, updated_at = excluded.updated_at", (step, ts, self.now))
        self.cursors[step] = ts

    def step(self, name: str, fn) -> bool:
        self.step_key = None
        self.con.execute("begin")
        try:
            fn()
        except _NotFound:
            self.con.execute("rollback")
            return False
        except HandError as err:
            self.con.execute("rollback")
            self.record["errors"].append(str(err))
            kind = _page_for(err)
            key = _streak_key(err, kind)
            if key is not None:
                self.step_key = key
                self.failed.setdefault(key, (kind, str(err)))
            elif kind:
                page(kind, str(err), self.now, self.managers)
            if isinstance(err, (AuthError, RateLimited)):
                raise _Skip() from None
            return False
        except Exception as err:
            self.con.execute("rollback")
            failure = f"{name}: {type(err).__name__}"
            self.record["errors"].append(failure)
            page("crash", failure, self.now, self.managers)
            return False
        except BaseException:
            self.con.execute("rollback")
            raise
        self.con.execute("commit")
        return True

    def settle(self, path: Path, slept: bool, complete: bool) -> None:
        held = self.record["held"]
        due: list[str] = []
        try:
            streaks = failure_streak.load(path)
            before = {key: dict(entry) for key, entry in streaks.items()}
            for key in STREAK_KEYS:
                if key in self.failed and slept:
                    held[key] = streaks[key]["n"] if key in streaks else 0
                elif key in self.failed:
                    entry = failure_streak.fail(streaks, key, self.now)
                    if failure_streak.due(entry):
                        due.append(key)
                    else:
                        held[key] = entry["n"]
                elif complete:
                    failure_streak.clear(streaks, key)
            if streaks != before:
                failure_streak.save(path, streaks)
        except Exception as err:
            print(f"chat-archive: failure streaks not counted ({type(err).__name__}: {err})", file=sys.stderr)
            due = list(self.failed)
            for key in due:
                held.pop(key, None)
        for key in due:
            kind, failure = self.failed[key]
            page(kind, failure, self.now, self.managers)

    def fetch(self, tool: str, args: dict, kind: str, *, conversation_id=None, keep_raw: bool, source: str) -> dict:
        return self.ingest(tool, args, self.calls.call(tool, args), kind, conversation_id, keep_raw, source)

    def fetch_many(self, tool: str, argss: list[dict], kind: str, cids: list, *, keep_raw: bool, source: str,
                   store: bool = True) -> list:
        texts = self.calls.batch([(tool, args) for args in argss])
        if len(texts) >= 2 and all(isinstance(text, HandError) for text in texts):
            raise texts[0]
        return [text if isinstance(text, HandError) else self.ingest(tool, args, text, kind, cid, keep_raw, source, store)
                for args, text, cid in zip(argss, texts, cids)]

    def item_failed(self, err: HandError, table: str, key) -> None:
        self.record["errors"].append(str(err))
        self.record["items_failed"] += 1
        if table == "threads":
            self.con.execute("insert into threads (conversation_id, thread_ts, last_reply_ts, checked_at, next_try, backoff) "
                             f"values (?, ?, ?, ?, ? + {BACKOFF_MIN_SEC}, {BACKOFF_MIN_SEC}) "
                             "on conflict (conversation_id, thread_ts) do update set "
                             f"next_try = excluded.checked_at + {BACKOFF_SQL}, backoff = {BACKOFF_SQL}, "
                             "checked_at = excluded.checked_at", (key[0], key[1], key[1], self.now, self.now))
        else:
            self.con.execute(f"update conversations set next_try = ? + {BACKOFF_SQL}, backoff = {BACKOFF_SQL}, "
                             "swept_at = ? where id = ?", (self.now, self.now, key))

    def item_ok(self, table: str, key) -> None:
        if table == "threads":
            self.con.execute("update threads set next_try = 0, backoff = 0 where conversation_id = ? and thread_ts = ?", key)
        else:
            self.con.execute("update conversations set next_try = 0, backoff = 0 where id = ?", (key,))

    def waiting(self, table: str) -> set:
        column = "conversation_id || ' ' || thread_ts" if table == "threads" else "id"
        return {r[0] for r in self.con.execute(f"select {column} from {table} where next_try > ?", (self.now,))}

    def _page(self, tool: str, args: dict, kind: str, cid: str, source: str, table: str, key) -> dict | None:
        try:
            return self.fetch(tool, args, kind, conversation_id=cid, keep_raw=True, source=source)
        except HandError as err:
            if not item_failure(err):
                raise
            self.item_failed(err, table, key)
            return None

    def ingest(self, tool: str, args: dict, text: str, kind: str, conversation_id, keep_raw: bool, source: str,
               store: bool = True) -> dict:
        body, page_info = unwrap(text)
        parsed = parse(kind, body, conversation_id=conversation_id, pagination_info=page_info)
        if store and kind != "list":
            new, updated = record(self.con, self.raw_dir, tool, args, text, parsed, source, self.now, keep_raw=keep_raw,
                                  contiguous=kind == "conversation")
            self.record["messages_new"] += new
            self.record["messages_updated"] += updated
        elif store and keep_raw:
            _raw_line(self.raw_dir, self.now, tool, args, text)
        if parsed["unparsed"]:
            self.record["parser_failed"] += parsed["unparsed"]
            page("parser", tool, self.now, self.managers)
        return parsed

    def conversation_args(self, cid: str, limit: int = PAGE_LIMIT) -> dict:
        oldest = max(self.floor, _ts(self.now - self.cfg["retention_days"] * 86400))
        return {"channel_id": cid, "limit": limit, "response_format": "detailed", "oldest": oldest}

    def gap(self, cid: str) -> tuple[str | None, str | None]:
        row = self.con.execute("select gap_ts, gap_cursor from conversations where id = ?", (cid,)).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def _pages(self, cid: str, args: dict, source: str, stop_known: set, first: dict | None, budget: dict,
               max_pages: int) -> tuple[str, str | None, str | None, int]:
        oldest, fetched = None, 0
        while True:
            if first is not None:
                parsed, first = first, None
            elif budget["pages"] == 0 and "cursor" not in args:
                parsed = self.fetch("read_conversation", args, "conversation", conversation_id=cid, keep_raw=True,
                                    source=source)
            else:
                parsed = self._page("read_conversation", args, "conversation", cid, source, "conversations", cid)
                if parsed is None:
                    return "failed", args.get("cursor"), oldest, fetched
            budget["pages"] += 1
            fetched += 1
            self.note_threads(cid, parsed["messages"])
            for message in parsed["messages"]:
                oldest = message["ts"] if oldest is None or message["ts"] < oldest else oldest
            if parsed["cursor"] is None or not parsed["messages"]:
                return "end", None, oldest, fetched
            if any(m["ts"] in stop_known for m in parsed["messages"]):
                return "known", parsed["cursor"], oldest, fetched
            if budget["pages"] >= max_pages:
                if max_pages >= MAX_PAGES:
                    self.record["pages_capped"] += 1
                return "capped", parsed["cursor"], oldest, fetched
            args = {**args, "cursor": parsed["cursor"]}

    def read_conversation(self, cid: str, *, limit: int = PAGE_LIMIT, source: str, first: dict | None = None,
                          max_pages: int = MAX_PAGES, known_before=None) -> bool:
        if known_before is None:
            known_before = self.known_ts(cid)
        gap_ts, gap_cursor = self.gap(cid)
        below = {ts for ts in known_before if gap_ts is None or ts < gap_ts}
        args, budget = self.conversation_args(cid, limit), {"pages": 0}
        outcome, next_cursor, oldest, _ = self._pages(cid, args, source, known_before if gap_cursor else below, first,
                                                      budget, max_pages)
        if outcome == "known" and gap_cursor:
            outcome, next_cursor, oldest, fetched = self._pages(cid, {**args, "cursor": gap_cursor}, source, below, None,
                                                                budget, max_pages)
            if outcome == "failed" and not fetched:
                next_cursor = None
        if outcome in ("end", "known"):
            gap_ts = gap_cursor = None
        else:
            marks = [ts for ts in (gap_ts, oldest) if ts]
            gap_cursor, gap_ts = next_cursor, min(marks) if marks else None
        self.con.execute("update conversations set gap_ts = ?, gap_cursor = ? where id = ?", (gap_ts, gap_cursor, cid))
        complete = outcome != "failed"
        if complete:
            self.item_ok("conversations", cid)
        return complete

    def read_conversation_item(self, cid: str, *, source: str, first: dict | None = None, known_before=None) -> bool:
        try:
            return self.read_conversation(cid, source=source, first=first, known_before=known_before)
        except HandError as err:
            if not item_failure(err):
                raise
            self.item_failed(err, "conversations", cid)
            return False

    def known_ts(self, cid: str) -> set:
        return {r[0] for r in self.con.execute("select ts from messages where conversation_id = ? and contiguous = 1",
                                                (cid,))}

    def read_conversations(self, cids: list[str], *, source: str) -> None:
        cids = [cid for cid in cids if cid not in self.waiting("conversations")]
        known = {cid: self.known_ts(cid) for cid in cids}
        firsts = self.fetch_many("read_conversation", [self.conversation_args(cid) for cid in cids], "conversation",
                                 cids, keep_raw=True, source=source)
        for cid, first in zip(cids, firsts):
            if isinstance(first, HandError):
                self.item_failed(first, "conversations", cid)
            else:
                self.read_conversation_item(cid, source=source, first=first, known_before=known[cid])

    @staticmethod
    def thread_args(cid: str, thread_ts: str) -> dict:
        return {"channel_id": cid, "message_ts": thread_ts, "response_format": "detailed", "limit": THREAD_LIMIT}

    def read_threads(self, pairs: list[tuple[str, str]]) -> None:
        waiting = self.waiting("threads")
        pairs = [(cid, ts) for cid, ts in pairs if f"{cid} {ts}" not in waiting]
        firsts = self.fetch_many("read_thread", [self.thread_args(cid, ts) for cid, ts in pairs], "thread",
                                 [cid for cid, _ in pairs], keep_raw=True, source="thread")
        for (cid, thread_ts), first in zip(pairs, firsts):
            if isinstance(first, HandError):
                self.item_failed(first, "threads", (cid, thread_ts))
                continue
            self.read_thread(cid, thread_ts, first=first)

    def our_thread_page(self, cid: str, thread_ts: str) -> dict | None:
        tool, args = "read_thread", self.thread_args(cid, thread_ts)
        try:
            text = self.calls.call(tool, args)
        except HandError as err:
            if not item_failure(err):
                raise
            self.record["errors"].append(str(err))
            return None
        parsed = self.ingest(tool, args, text, "thread", cid, False, "thread", store=False)
        footer, me = self.cfg["sent_footer"], self.cfg["self_user_id"]
        if not any(m["sender_id"] == me and m["text"].endswith(footer) for m in parsed["messages"]):
            return None
        return self.ingest(tool, args, text, "thread", cid, True, "thread")

    def read_thread_item(self, cid: str, thread_ts: str, *, first: dict | None = None) -> bool:
        try:
            return self.read_thread(cid, thread_ts, first=first)
        except HandError as err:
            if not item_failure(err):
                raise
            self.item_failed(err, "threads", (cid, thread_ts))
            return False

    def read_thread(self, cid: str, thread_ts: str, *, first: dict | None = None) -> bool:
        args = self.thread_args(cid, thread_ts)
        pages, last, complete = 0, thread_ts, True
        while True:
            if first is not None and pages == 0:
                parsed = first
            elif pages == 0:
                parsed = self.fetch("read_thread", args, "thread", conversation_id=cid, keep_raw=True, source="thread")
            else:
                parsed = self._page("read_thread", args, "thread", cid, "thread", "threads", (cid, thread_ts))
                if parsed is None:
                    complete = False
                    break
            pages += 1
            for message in parsed["messages"]:
                last = max(last, message["ts"])
            if parsed["cursor"] is None or not parsed["messages"] or pages >= MAX_PAGES:
                break
            args = {**args, "cursor": parsed["cursor"]}
        self.con.execute("insert into threads (conversation_id, thread_ts, last_reply_ts, checked_at) values (?, ?, ?, ?) "
                         "on conflict (conversation_id, thread_ts) do update set "
                         "last_reply_ts = max(last_reply_ts, excluded.last_reply_ts), checked_at = excluded.checked_at",
                         (cid, thread_ts, last, self.now))
        if complete:
            self.item_ok("threads", (cid, thread_ts))
        return complete

    def note_threads(self, cid: str, messages: list[dict]) -> None:
        for message in messages:
            if message["reply_count"]:
                self.con.execute("insert or ignore into threads (conversation_id, thread_ts, last_reply_ts, checked_at) "
                                 "values (?, ?, ?, 0)", (cid, message["ts"], message["ts"]))

    def kind_of(self, message: dict) -> str | None:
        row = self.con.execute("select kind from conversations where id = ?", (message["conversation_id"],)).fetchone()
        return (row[0] if row and row[0] else None) or message["conversation_kind"]

    def in_threads(self, cid: str, thread_ts: str | None) -> bool:
        return bool(thread_ts) and self.con.execute("select 1 from threads where conversation_id = ? and thread_ts = ?",
                                                    (cid, thread_ts)).fetchone() is not None

    def registry(self) -> None:
        args: dict = {"types": "im,mpim", "limit": LIST_LIMIT}
        pages = 0
        while True:
            parsed = self.fetch("list_conversations", args, "list", keep_raw=True, source="registry")
            pages += 1
            for conv in parsed["conversations"]:
                self.con.execute("insert into conversations (id, kind, name, members, last_ts, swept_at) values (?, ?, ?, ?, null, null) "
                                 "on conflict (id) do update set kind = coalesce(excluded.kind, kind), "
                                 "name = case when excluded.name <> '' then excluded.name else name end, members = excluded.members",
                                 (conv["id"], conv["kind"], conv["name"], conv["members"]))
            if parsed["cursor"] is None or not parsed["conversations"] or pages >= MAX_PAGES:
                break
            args = {**args, "cursor": parsed["cursor"]}
        self.set_cursor("registry", _ts(self.now))

    def triggers(self, root: Path) -> None:
        for path in sorted(p for p in root.glob("*.json")):
            try:
                body = json.loads(path.read_text())
                if not isinstance(body, dict) or any(k not in body for k in TRIGGER_FIELDS):
                    raise ValueError("trigger shape")
                if not TS_VALUE.match(str(body["ts"])):
                    raise ValueError("trigger ts")
                delivered, tries = float(body["delivered"]), int(body["tries"])
            except (OSError, ValueError, TypeError):
                path.unlink(missing_ok=True)
                self.record["triggers_dead"] += 1
                continue
            if self.now - delivered >= TRIGGER_TTL_SEC:
                path.unlink(missing_ok=True)
                self.record["triggers_dead"] += 1
                continue
            last_try = body.get("last_try")
            if tries and isinstance(last_try, (int, float)) and self.now - last_try < TRIGGER_RETRY_SEC:
                if body.get("streak"):
                    self.failing_untried = True
                continue
            if self.step(f"trigger {body['key']}", lambda: self.trigger(body, path)):
                continue
            body["tries"], body["last_try"] = tries + 1, self.now
            if self.step_key:
                body["streak"] = self.step_key
            else:
                body.pop("streak", None)
            path.write_text(json.dumps(body))

    def trigger(self, body: dict, path: Path) -> None:
        ts = str(body["ts"])
        args = {"filters": f"after:{_day_before(float(ts))}", "after": _ts(float(ts) - WINDOW_BEFORE),
                "before": _ts(float(ts) + WINDOW_AFTER), **SEARCH_ARGS}
        text = self.calls.call("search", args)
        parsed_body, page_info = unwrap(text)
        parsed = parse("search", parsed_body, pagination_info=page_info)
        hits = [m for m in parsed["messages"] if m["ts"] == ts and m["conversation_id"]]
        if len(hits) != 1:
            raise _NotFound()
        hit = hits[0]
        cid, kind = hit["conversation_id"], self.kind_of(hit)
        thread_ts = hit["thread_ts"]
        if kind in ("dm", "group"):
            self.con.execute("insert or ignore into conversations (id, kind, name, members, last_ts, swept_at) "
                             "values (?, ?, ?, '', null, null)", (cid, kind, hit["conversation_name"]))
            self.read_conversation_item(cid, source="trigger")
        elif kind == "channel" and (self.in_threads(cid, thread_ts) or _key_in(hit["text"], self.cfg["mention_keys"])):
            record(self.con, self.raw_dir, "search", args, "", {**parsed, "messages": hits}, "trigger", self.now, keep_raw=False)
            self.read_thread_item(cid, thread_ts or ts)
            thread_ts = thread_ts or ts
        elif kind == "channel" and thread_ts and thread_ts != ts and self.cfg["sent_footer"] and self.cfg["self_user_id"]:
            first = self.our_thread_page(cid, thread_ts)
            if first is not None:
                record(self.con, self.raw_dir, "search", args, "", {**parsed, "messages": hits}, "trigger", self.now,
                       keep_raw=False)
                self.read_thread_item(cid, thread_ts, first=first)
        self.con.execute("insert or replace into resolved values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (body["iden"], ts, cid, kind, hit["conversation_name"], thread_ts, hit["sender_id"], hit["sender"],
                          hit["link"], self.now))
        self.record["triggers_done"] += 1
        self.con.execute("commit")
        path.unlink(missing_ok=True)
        self.con.execute("begin")

    def search_pages(self, step: str, extra: dict) -> list[dict]:
        since = self.cursor(step)
        args = {**extra, "filters": f"{extra.get('filters', '')} after:{_day_before(since)}".strip(),
                "after": _ts(since - self.cfg["search_overlap_sec"]), **CURSOR_SEARCH_ARGS}
        hits, pages = [], 0
        while True:
            parsed = self.fetch("search", args, "search", keep_raw=True, source=step.split(":")[0])
            pages += 1
            hits += parsed["messages"]
            if parsed["cursor"] is None or not parsed["messages"]:
                break
            if pages >= MAX_PAGES:
                self.record["pages_capped"] += 1
                break
            args = {**args, "cursor": parsed["cursor"]}
        newest = max((m["ts"] for m in hits), default=None)
        self.set_cursor(step, max(newest or "", self.cursors.get(step) or "") or _ts(since))
        return hits

    def dm(self) -> None:
        hits = self.search_pages("dm", {"filters": "is:dm"})
        self.read_conversations(sorted({m["conversation_id"] for m in hits if m["conversation_id"]}), source="dm")

    def mention(self, key: str) -> None:
        hits = self.search_pages(f"mention:{key}", {"keywords": [key]})
        pairs: list[tuple[str, str]] = []
        for hit in hits:
            pair = (hit["conversation_id"], hit["thread_ts"] or hit["ts"])
            if pair[0] and pair not in pairs:
                pairs.append(pair)
        self.read_threads(pairs)

    def live_threads(self) -> None:
        retention = self.cfg["retention_days"] * 86400
        due = []
        for cid, thread_ts, last_reply, checked_at, backoff in self.con.execute(
                "select conversation_id, thread_ts, last_reply_ts, checked_at, backoff from threads where next_try <= ?",
                (self.now,)).fetchall():
            if backoff > 0:
                due.append((cid, thread_ts))
                continue
            age = self.now - float(last_reply or thread_ts)
            if age > retention:
                continue
            if age <= LIVE_THREAD_DAYS * 86400 or self.now - (checked_at or 0) >= 86400:
                due.append((cid, thread_ts))
        self.read_threads(due)
        self.set_cursor("threads", _ts(self.now))

    def sweep(self) -> None:
        live = _ts(self.now - LIVE_THREAD_DAYS * 86400)
        rows = self.con.execute("select id, kind, last_ts from conversations where kind in ('dm', 'group') "
                                "and next_try <= ?", (self.now,)).fetchall()
        active = [cid for cid, kind, last_ts in rows if last_ts and last_ts >= live]
        idle = [(cid, last_ts) for cid, kind, last_ts in rows if kind == "group" and cid not in active]
        self.read_conversations(active, source="sweep")
        self.con.executemany("update conversations set swept_at = ? where id = ?", [(self.now, cid) for cid in active])
        probes = self.fetch_many("read_conversation", [self.conversation_args(cid, 1) for cid, _ in idle], "conversation",
                                 [cid for cid, _ in idle], keep_raw=False, source="sweep", store=False)
        for (cid, last_ts), probe in zip(idle, probes):
            if isinstance(probe, HandError):
                self.item_failed(probe, "conversations", cid)
                continue
            newest = max((m["ts"] for m in probe["messages"]), default=None)
            if newest and (not last_ts or newest > last_ts):
                if not self.read_conversation_item(cid, source="sweep"):
                    continue
            self.item_ok("conversations", cid)
            self.con.execute("update conversations set swept_at = ? where id = ?", (self.now, cid))
        self.set_cursor("sweep", _ts(self.now))

    def prune(self) -> None:
        self.record["pruned"] = prune(self.con, self.now, self.cfg["retention_days"], self.raw_dir)
        self.set_cursor("full", _ts(self.now))


def _lock(path: Path):
    import fcntl
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _ledger(record: dict) -> None:
    from .daemon import append_jsonl
    first = not paths.CHAT_LEDGER.exists()
    append_jsonl(paths.CHAT_LEDGER, record)
    if first:
        paths.CHAT_LEDGER.chmod(0o600)
        return
    lines = paths.CHAT_LEDGER.read_text().splitlines()
    if len(lines) > LEDGER_KEEP:
        tmp = paths.CHAT_LEDGER.with_name(f".{paths.CHAT_LEDGER.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, ("\n".join(lines[-LEDGER_KEEP:]) + "\n").encode())
        finally:
            os.close(fd)
        os.replace(tmp, paths.CHAT_LEDGER)


def pass_(cfg: dict | None = None, *, hand=None, now: float | None = None, wake: str = "schedule",
          root: Path | None = None, managers=None, sleep=time.sleep) -> dict:
    cfg = config.chat_archive() if cfg is None else cfg
    if not cfg["enabled"]:
        return {"skipped": "disabled"}
    root = paths.CHAT if root is None else root
    now = time.time() if now is None else now
    if paths.CHAT_STOP.exists():
        return {"skipped": "stop-file"}
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    (root / "raw").mkdir(exist_ok=True)
    (root / "triggers").mkdir(exist_ok=True)
    lock_fd = _lock(root / "pass.lock")
    if lock_fd is None:
        return {"skipped": "locked"}
    begun = failure_streak.mark()
    started = time.monotonic()
    record_: dict = {"ts": now, "kind": "pass", "wake": wake, "full": False, "calls": 0, "searches": 0, "messages_new": 0,
                     "messages_updated": 0, "triggers_done": 0, "triggers_dead": 0, "pages_capped": 0,
                     "parser_failed": 0, "items_failed": 0, "pruned": {"messages": 0, "threads": 0, "resolved": 0, "raw_files": 0},
                     "seconds": 0.0, "token_key": "none", "token_account": "none", "auth_retries": 0, "errors": [],
                     "held": {}, "slept": False}
    try:
        hand = hand_for(cfg) if hand is None else hand
        con = open_db(root / "archive.db")
        try:
            run = _Pass(cfg, con, _Calls(hand, record_, now, sleep), now, record_, managers, root / "raw")
            if run.floor is None:
                (root / "started").touch()
                run.con.execute("begin")
                run.set_cursor("started", _ts(now))
                for step in ("dm", "threads", "sweep", "registry", *(f"mention:{k}" for k in cfg["mention_keys"])):
                    run.set_cursor(step, _ts(now))
                run.set_cursor("full", "0")
                run.con.execute("commit")
                run.floor = _ts(now)
            complete = False
            try:
                if con.execute("select count(*) from conversations").fetchone()[0] == 0:
                    run.step("registry", run.registry)
                run.triggers(root / "triggers")
                if now - run.cursor("full") >= cfg["every_sec"]:
                    record_["full"] = True
                    run.step("dm", run.dm)
                    for key in cfg["mention_keys"]:
                        run.step(f"mention:{key}", lambda key=key: run.mention(key))
                    run.step("threads", run.live_threads)
                    if now - run.cursor("sweep") >= cfg["group_sweep_sec"]:
                        run.step("sweep", run.sweep)
                    if now - run.cursor("registry") >= cfg["registry_sec"]:
                        run.step("registry", run.registry)
                    run.step("prune", run.prune)
                    complete = not run.failing_untried
            except _Skip:
                pass
            finally:
                record_["slept"] = failure_streak.slept(begun)
                run.settle(root / STREAK_FILE, record_["slept"], complete)
        finally:
            con.close()
    finally:
        record_["token_key"] = str(getattr(hand, "token_key", "none") or "none")
        record_["token_account"] = str(getattr(hand, "token_account", "none") or "none")
        record_["auth_retries"] = int(getattr(hand, "auth_retries", 0) or 0)
        record_["seconds"] = round(time.monotonic() - started, 3)
        _ledger(record_)
        os.close(lock_fd)
    return record_


def status(*, con=None, now: float | None = None) -> dict:
    from .daemon import read_jsonl
    cfg = config.chat_archive()
    now = time.time() if now is None else now
    records = [r for r in read_jsonl(paths.CHAT_LEDGER) if isinstance(r.get("ts"), (int, float))]
    newest = next((r for r in reversed(records) if r.get("calls")), None)
    today = [r for r in records if now - r["ts"] <= 86400]
    out = {"enabled": cfg["enabled"], "hand": cfg["hand"], "runner": cfg["runner"],
           "ledger_age_sec": round(now - records[-1]["ts"], 3) if records else None,
           "messages": 0, "conversations": 0, "cursors": {},
           "triggers_pending": sum(1 for p in paths.CHAT_TRIGGERS.glob("*.json")) if paths.CHAT_TRIGGERS.is_dir() else 0,
           "triggers_dead_24h": sum(int(r.get("triggers_dead") or 0) for r in today),
           "parser_failed_24h": sum(int(r.get("parser_failed") or 0) for r in today),
           "last_call_errors": list(newest.get("errors") or []) if newest else [],
           "token_key": str(newest.get("token_key") or "none") if newest else "none",
           "token_account": str(newest.get("token_account") or "none") if newest else "none",
           "auth_retries_24h": sum(int(r.get("auth_retries") or 0) for r in today)}
    close = con is None
    if con is None and paths.CHAT_DB.exists():
        con = open_db(readonly=True)
    if con is not None:
        try:
            out["messages"] = con.execute("select count(*) from messages").fetchone()[0]
            out["conversations"] = con.execute("select count(*) from conversations").fetchone()[0]
            out["cursors"] = dict(con.execute("select step, ts from cursors"))
        finally:
            if close:
                con.close()
    return out


TOOL_KINDS = {"read_conversation": "conversation", "read_thread": "thread", "search": "search",
              "list_conversations": "list"}
SYNC_INTERVAL_SEC = 300
LOOP_SLEEP_SEC = 10


def reindex(*, con=None, raw_dir: Path | None = None) -> dict:
    raw_dir = paths.CHAT_RAW if raw_dir is None else raw_dir
    paths.CHAT.mkdir(parents=True, exist_ok=True)
    lock_fd = _lock(paths.CHAT_LOCK)
    if lock_fd is None:
        return {"skipped": "locked"}
    out = {"files": 0, "lines": 0, "messages_new": 0, "messages_updated": 0, "unparsed": 0}
    close = con is None
    con = open_db() if con is None else con
    try:
        con.execute("begin")
        for path in sorted(raw_dir.glob("*.jsonl")) if raw_dir.is_dir() else []:
            out["files"] += 1
            for line in path.read_text().splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("tool") not in TOOL_KINDS:
                    continue
                out["lines"] += 1
                args = entry.get("args") if isinstance(entry.get("args"), dict) else {}
                body, page_info = unwrap(str(entry.get("text") or ""))
                kind = TOOL_KINDS[entry["tool"]]
                parsed = parse(kind, body, conversation_id=args.get("channel_id"), pagination_info=page_info)
                out["unparsed"] += parsed["unparsed"]
                if kind == "list":
                    for conv in parsed["conversations"]:
                        con.execute("insert into conversations (id, kind, name, members, last_ts, swept_at) values (?, ?, ?, ?, null, null) "
                                    "on conflict (id) do update set kind = coalesce(excluded.kind, kind), "
                                    "name = case when excluded.name <> '' then excluded.name else name end, members = excluded.members",
                                    (conv["id"], conv["kind"], conv["name"], conv["members"]))
                    continue
                now = entry.get("ts") if isinstance(entry.get("ts"), (int, float)) else time.time()
                new, updated = record(con, raw_dir, entry["tool"], args, "", parsed, "reindex", now, keep_raw=False,
                                      contiguous=kind == "conversation")
                out["messages_new"] += new
                out["messages_updated"] += updated
        con.execute("commit")
    finally:
        if close:
            con.close()
        os.close(lock_fd)
    return out


def label() -> str:
    return f"{config.loop_label_prefix()}.chat-archive"


def plist_path() -> Path:
    return paths.LAUNCH_AGENTS / f"{label()}.plist"


def _installer() -> Path:
    return config.claude_config_home() / "scripts" / "chat-archive-install.sh"


def _plist_matches(path: Path, program: Path, env: dict) -> bool:
    import plistlib
    try:
        with path.open("rb") as fh:
            data = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return False
    if not isinstance(data, dict) or data.get("ProgramArguments") != [str(program), "chat-archive", "pass"]:
        return False
    got_env = data.get("EnvironmentVariables")
    if not isinstance(got_env, dict) or list(got_env)[:1] != ["PATH"]:
        return False
    if {k: v for k, v in got_env.items() if k != "PATH"} != dict(sorted(env.items())):
        return False
    return (data.get("Label") == label() and data.get("StartInterval") == SYNC_INTERVAL_SEC
            and data.get("WatchPaths") == [str(paths.CHAT_TRIGGERS)] and data.get("RunAtLoad") is True)


def sync(*, run=None, uid: int | None = None, program: Path | None = None, script: Path | None = None) -> int:
    import tempfile
    from .daemon import _dockwright_bin, _loaded, _run, _under_a_worktree_root
    run = run or _run
    uid = os.getuid() if uid is None else uid
    program = _dockwright_bin() if program is None else program
    script = _installer() if script is None else script
    cfg = config.chat_archive()
    name, target = label(), plist_path()
    if not cfg["enabled"] or cfg["runner"] != "launchd":
        if target.exists() or _loaded(run, uid, name):
            run(["launchctl", "bootout", f"gui/{uid}/{name}"])
            target.unlink(missing_ok=True)
            print(f"chat-archive: removed {name} ([chat_archive] enabled = {str(cfg['enabled']).lower()}, "
                  f"runner = {cfg['runner']})")
        return 0
    if _under_a_worktree_root(program):
        print(f"chat-archive: {program} lies under a worktree root; run chat-archive sync from the main clone",
              file=sys.stderr)
        return 1
    env = config.lanes_env()
    if _plist_matches(target, program, env) and _loaded(run, uid, name):
        return 0
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(env, fh)
        env_file = fh.name
    try:
        rc = run(["bash", str(script), str(program), str(paths.CHAT), name, str(SYNC_INTERVAL_SEC),
                  str(paths.CHAT_TRIGGERS), env_file])
    finally:
        Path(env_file).unlink(missing_ok=True)
    if rc != 0:
        print(f"chat-archive: {script} exited {rc}; the job {name} is not installed", file=sys.stderr)
        return rc
    print(f"chat-archive: installed {name} (every {SYNC_INTERVAL_SEC}s and on {paths.CHAT_TRIGGERS})")
    return 0


def _trigger_pending() -> bool:
    return paths.CHAT_TRIGGERS.is_dir() and any(p.suffix == ".json" for p in paths.CHAT_TRIGGERS.iterdir())


def loop(*, once: bool = False, sleep=time.sleep, now=time.time, **kw) -> int:
    cfg = config.chat_archive()
    if cfg["runner"] != "manager":
        return 0
    lane_io.detach_stdout()
    last_schedule = 0.0
    while True:
        moment = now()
        try:
            if _trigger_pending():
                pass_(cfg, now=moment, wake="trigger", **kw)
            if moment - last_schedule >= cfg["every_sec"]:
                pass_(cfg, now=moment, wake="schedule", **kw)
                last_schedule = moment
        except Exception as err:
            print(f"chat-archive: pass failed ({type(err).__name__}); the loop goes on", file=sys.stderr)
        if once:
            return 0
        sleep(LOOP_SLEEP_SEC)


def main(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="dockwright chat-archive")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("pass")
    run.add_argument("--wake", choices=("schedule", "trigger"), default="schedule")
    read_ = sub.add_parser("read")
    read_.add_argument("--after", required=True)
    read_.add_argument("--before")
    read_.add_argument("--conversation")
    read_.add_argument("--thread")
    read_.add_argument("--no-bots", action="store_true")
    read_.add_argument("--limit", type=int, default=500)
    search_ = sub.add_parser("search")
    search_.add_argument("query")
    search_.add_argument("--after")
    search_.add_argument("--before")
    search_.add_argument("--conversation")
    search_.add_argument("--sender")
    search_.add_argument("--limit", type=int, default=50)
    conv = sub.add_parser("conversations")
    conv.add_argument("--kind", choices=("dm", "group", "channel"))
    conv.add_argument("--since")
    sub.add_parser("status")
    sub.add_parser("reindex")
    sub.add_parser("sync")
    loop_ = sub.add_parser("loop")
    loop_.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "pass":
        pass_(wake=args.wake)
        return 0
    if args.command == "read":
        out = read(args.after, before=args.before, conversation=args.conversation, thread=args.thread,
                   bots=not args.no_bots, limit=args.limit)
    elif args.command == "search":
        out = search(args.query, after=args.after, before=args.before, conversation=args.conversation,
                     sender=args.sender, limit=args.limit)
    elif args.command == "conversations":
        out = conversations(kind=args.kind, since=args.since)
    elif args.command == "status":
        out = status()
    elif args.command == "reindex":
        out = reindex()
    elif args.command == "sync":
        return sync()
    else:
        return loop(once=args.once)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0
