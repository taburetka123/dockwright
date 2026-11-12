from __future__ import annotations

import asyncio
import time
from pathlib import Path

from . import chat_archive, config, event_queue, events, paths, state, waiting_on
from .event_sources import SelectorFailed, tracker
from .event_sources import exec as exec_source

STALL_PAGE_SEC = 3600
ERROR_CHARS = 200
_UNREAD = (OSError, ValueError)
REPLY_HINT = (" — read it (when chat_read shows no message from them after that ts, read the thread with the chat "
              "app's thread-read tool first). If your brief covers the whole answer, call event_ack(\"{rule}\", <id>) "
              "for each of {event_ids}, then act per your brief and name it in your report. For any other answer "
              "call no event_ack: ask_manager with the first line `dm-reply: {rule} {event_id} {channel} "
              "{thread_or_dash} {msg_ts}` and the gist, then go on with your task. Never post the answer yourself")
FOOTER_SENDER = "a session (footer match)"


def _engine():
    from . import mcp_server
    return mcp_server


def _live_managers() -> list[dict]:
    from . import topic_agent
    return topic_agent._live_managers()


def _manager_for(domain: str) -> dict | None:
    from . import topic_agent
    return next((r for r in _live_managers() if topic_agent._domain_of(r) == domain), None)


def _cursor_path(name: str):
    return paths.EVENTS_CURSORS / "rules" / f"{name}.json"


def _load(name: str, now: float) -> dict:
    fresh = not _cursor_path(name).exists()
    cursor = state.read_json(_cursor_path(name))
    cursor = cursor if isinstance(cursor, dict) else {}
    cursor.setdefault("last", None)
    cursor.setdefault("started", {})
    cursor.setdefault("stalled_since", None)
    cursor.setdefault("last_value", None)
    cursor.setdefault("last_queued_at", None)
    cursor.setdefault("pending_value", None)
    cursor.setdefault("pending_count", 0)
    if fresh:
        cursor["last"] = [now, ""]
        state.write_json_atomic(_cursor_path(name), cursor)
    return cursor


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _after_cursor(event: events.Event, last) -> bool:
    return last is None or (event.ts, event.id) > (float(last[0]), str(last[1]))


def _wait_key(event: events.Event) -> tuple:
    return (event.kind, tuple(sorted(event.match.items())), event.cursor_key)


def _schema_written(key: str) -> bool:
    return (paths.artifact_ticket_dir(key) / "schema.toml").is_file()


def _open_works(engine, started: dict) -> list[str]:
    out = []
    for key in started:
        if engine._load_snapshot_for(key) is None:
            if _schema_written(key):
                out.append(key)
            continue
        try:
            if engine._engine_fold(key, "events").closed is None:
                out.append(key)
        except _UNREAD:
            out.append(key)
    return out


def _sprint_rule(rule: dict) -> bool:
    return rule["source"] == "tracker" and rule["kind"] == tracker.SPRINT_KIND


def _parked(value: str) -> bool:
    return value in (waiting_on.HUMAN_GATE, waiting_on.NONE) or value.startswith("external:")


def _holds_slot(engine, key: str, now: float, index, assignments: list) -> bool:
    snap = engine._load_snapshot_for(key)
    if snap is None:
        return _schema_written(key)
    try:
        stamps = {f"{a.get('phase')}.{a.get('name')}": a for a in engine._list_artifacts(key)}
        status = engine._waiting_on_status(key, snap, engine._read_events(paths.artifact_events_path(key)), stamps,
                                           now, index=index, assignments=assignments)
    except Exception:
        return True
    fold = status.get("fold")
    if status.get("fold_error") is not None or fold is None:
        return True
    if fold.closed is not None:
        return False
    if fold.open_requests:
        return True
    live = [tid for tid, token in fold.tokens.items() if not token.forked]
    if not live:
        return not _parked(status["waiting_on"])
    unheld = [status["tokens"].get(tid) for tid in live if tid not in fold.held]
    if not unheld:
        return False
    return bool(status.get("questions")) or any(value is None or not _parked(value) for value in unheld)


def _slots_held(engine, keys, now: float) -> int:
    index = engine._process_index()
    assignments = [r for r in state.list_json_in(paths.ASSIGNMENTS) if isinstance(r, dict)]
    return sum(1 for key in dict.fromkeys(keys) if _holds_slot(engine, key, now, index, assignments))


def _covered(engine, event: events.Event) -> str:
    parent, subtasks = event.payload.get("parent"), event.payload.get("subtasks")
    related = [parent] + (subtasks if isinstance(subtasks, list) else [])
    return next(iter(_open_works(engine, dict.fromkeys(k for k in related if isinstance(k, str) and k))), "")


def _start(engine, rule: dict, name: str, event: events.Event, mgr: dict, cursor: dict, now: float) -> dict | None:
    task_key = event.match.get("issue") or event.id
    covered = _covered(engine, event)
    if covered:
        return {"type": "event_rule_skipped", "rule": name, "task_key": task_key, "event_id": event.id,
                "reason": f"{covered} has an open work"}
    if _sprint_rule(rule):
        env, _ = events.load_env(config.events_env_file())
        if env is None:
            return None
        subtasks = event.payload.get("subtasks")
        subtasks = [k for k in subtasks if isinstance(k, str)] if isinstance(subtasks, list) else []
        try:
            blocker = tracker.start_blocker(rule, task_key, subtasks, env)
        except SelectorFailed:
            return None
        if blocker:
            return {"type": "event_rule_skipped", "rule": name, "task_key": task_key, "event_id": event.id,
                    "reason": blocker}
    ask = f"{event.summary}\n{event.payload.get('url', '')}".rstrip()
    domain = rule.get("domain") or paths.DEFAULT_DOMAIN
    earlier = task_key in cursor["started"]
    cursor["started"][task_key] = now
    state.write_json_atomic(_cursor_path(name), cursor)
    try:
        asyncio.run(engine.pipeline_start_impl(mgr["claude_sid"], task_key, rule["schema"],
                                               str(paths.ensure_worker_home()), ask, domain=domain))
    except ValueError as err:
        if not earlier:
            cursor["started"].pop(task_key, None)
            state.write_json_atomic(_cursor_path(name), cursor)
        return {"type": "event_rule_skipped", "rule": name, "task_key": task_key, "event_id": event.id,
                "reason": str(err)[:200]}
    engine._append_task_event(task_key, {"type": "event_started", "rule": name, "event_id": event.id})
    return {"type": "event_started", "rule": name, "task_key": task_key, "event_id": event.id,
            "manager_sid": mgr["claude_sid"], "manager_name": mgr.get("name")}


def _due(rule: dict, cursor: dict, event: events.Event, now: float) -> bool:
    field = rule.get("on_change")
    if not field:
        return True
    value = event.match.get(field, "")
    if cursor["last_value"] is None or value != cursor["last_value"]:
        count = cursor["pending_count"] + 1 if value == cursor["pending_value"] else 1
        due = count >= (rule.get("confirm_events") or 1)
        cursor["pending_value"], cursor["pending_count"] = (None, 0) if due else (value, count)
    else:
        repeat = rule.get("repeat_sec")
        named = rule.get("repeat_values") or []
        cursor["pending_value"], cursor["pending_count"] = None, 0
        due = bool((not named or value in named) and repeat and cursor["last_queued_at"] is not None
                   and now - cursor["last_queued_at"] >= repeat)
    if due:
        cursor["last_value"] = value
        cursor["last_queued_at"] = now
    return due


def _stall(name: str, cursor: dict, domain: str, now: float, records: list[dict]) -> list[dict]:
    since = cursor["stalled_since"]
    if since is None:
        records.append({"type": "event_stalled", "rule": name, "domain": domain, "since": _iso(now)})
        cursor["stalled_since"] = cursor["stalled_paged"] = now
    elif now - cursor.get("stalled_paged", since) >= STALL_PAGE_SEC:
        records.append({"type": "event_stalled", "rule": name, "domain": domain, "since": _iso(since)})
        cursor["stalled_paged"] = now
    state.write_json_atomic(_cursor_path(name), cursor)
    return records


def _event_time(e: events.Event) -> float:
    delivered = e.payload.get("delivered")
    return delivered if isinstance(delivered, (int, float)) and not isinstance(delivered, bool) else e.ts


def rule_window_start(rule: dict, now: float, pool: list[events.Event] | None) -> float | None:
    gate = rule["while"]
    return exec_source.window_start(gate["kind"], gate["match"], events.recent("exec", now) if pool is None else pool,
                                    now)


def rule_off(rule: dict, now: float, pool: list[events.Event] | None = None) -> str | None:
    if not rule["enabled"]:
        return "disabled"
    if rule["errors"]:
        return "error"
    stop = rule.get("stop_file")
    if stop and Path(stop).exists():
        return "stop_file"
    if rule.get("while") and rule_window_start(rule, now, pool) is None:
        return "window"
    return None


def _drop_before(name: str, start: float) -> int:
    dropped = 0
    for e in event_queue.read(name):
        if _event_time(e) < start:
            dropped += event_queue.ack(name, e.id)[0]
    return dropped


def _run_queue(name: str, rule: dict, pending: list[events.Event], cursor: dict, domain: str, now: float,
               records: list[dict], start: float | None = None) -> list[dict]:
    inside = pending if start is None else [e for e in pending if _event_time(e) >= start]
    acting = [e for e in inside if _due(rule, cursor, e, now)]
    added = event_queue.push(name, acting)
    if pending:
        cursor["last"] = [pending[-1].ts, pending[-1].id]
        state.write_json_atomic(_cursor_path(name), cursor)
    if start is not None:
        kept_out = len(pending) - len(inside) + _drop_before(name, start)
        if kept_out:
            records.append({"type": "event_queue_dropped", "rule": name, "count": kept_out, "reason": "window"})
    queued = event_queue.read(name)
    if not queued:
        return records
    mgr = _manager_for(domain)
    if mgr is None:
        return _stall(name, cursor, domain, now, records)
    if cursor["stalled_since"] is not None or "stalled_paged" in cursor:
        cursor["stalled_since"] = None
        cursor.pop("stalled_paged", None)
        state.write_json_atomic(_cursor_path(name), cursor)
    nudged = event_queue.nudged(name)
    if added or nudged is None or now - nudged >= rule["nudge_sec"]:
        records.append({"type": "event_queue_page", "rule": name, "count": len(queued), "line": queued[0].summary,
                        "event_id": queued[-1].id, "manager_name": mgr["name"]})
        event_queue.touch_nudged(name, now)
    return records


def _rule_takes(other: dict, e: events.Event, now: float, pool: list[events.Event] | None) -> bool:
    if not (other["source"] == e.source and other["kind"] == e.kind
            and all(e.match.get(k) == v for k, v in (other.get("match") or {}).items())):
        return False
    if rule_off(other, now, pool) is not None:
        return False
    return not (other["while"] and other["action"] == "queue" and _event_time(e) < rule_window_start(other, now, pool))


def _record_for(e: events.Event, msg_ts: str, sent: list[events.Event]) -> events.Event | None:
    channel, thread = e.match.get("channel", ""), e.match.get("thread", "")
    try:
        before = float(msg_ts)
    except ValueError:
        return None
    if e.kind.endswith("-thread"):
        fits = [r for r in sent if r.kind == "chat" and r.match.get("channel") == channel
                and thread and thread in (r.cursor_key, r.match.get("thread"))]
    else:
        fits = [r for r in sent if r.kind == "chat" and r.match.get("channel") == channel
                and not r.match.get("thread")]
    fits = [r for r in fits if r.ts < before]
    return max(fits, key=lambda r: r.ts) if fits else None


def _reply_line(sender: str, conversation: str, channel: str, thread: str, name: str, sent_ts: str) -> str:
    where = f"{channel}, thread {thread}" if thread else channel
    return (f"{sender} replied in {conversation} ({where}) to {name}'s message of {sent_ts}: "
            f"chat_read(conversation=\"{channel}\", after=\"{sent_ts}\")")


def _reply_target(e: events.Event, msg_ts: str, now: float, cfg: dict) -> dict | None:
    rec = _record_for(e, msg_ts, events.recent("sent", now))
    if rec is not None:
        return {"record": rec, "name": rec.match["name"], "sid": rec.match["sid"], "sent_ts": rec.cursor_key,
                "domain": rec.match.get("domain") or paths.DEFAULT_DOMAIN, "link": rec.payload.get("link") or ""}
    if not (cfg["sent_footer"] and cfg["self_user_id"]):
        return None
    hit = chat_archive.sent_by_footer(e.match["channel"], e.match.get("thread") or None, msg_ts, cfg["sent_footer"],
                                      cfg["self_user_id"])
    if hit is None:
        return None
    return {"record": None, "name": FOOTER_SENDER, "sid": "", "sent_ts": hit["ts"], "domain": paths.DEFAULT_DOMAIN,
            "link": ""}


def _inbound_line(sender: str, conversation: str, channel: str, thread: str, msg_ts: str) -> str:
    where = f"{channel}, thread {thread}" if thread else channel
    return (f"{sender} wrote in {conversation} ({where}) with no message of ours before it: "
            f"chat_read(conversation=\"{channel}\", after=\"{msg_ts}\")")


def _hint(rule: str, event_ids: list[str], channel: str, thread: str, msg_ts: str) -> str:
    return REPLY_HINT.format(rule=rule, event_ids=", ".join(event_ids), event_id=event_ids[-1], channel=channel,
                             thread_or_dash=thread or "-", msg_ts=msg_ts)


def _live_worker(engine, reply: dict) -> dict | None:
    if reply.get("sender_agent") != "worker":
        return None
    sid = reply.get("sender_sid") or ""
    if sid and engine._session_alive(sid):
        active = state.read_json(paths.ACTIVE / f"{sid}.json")
        return active if isinstance(active, dict) and active.get("agent") in ("manager", "worker") else None
    pid = reply.get("sender_pid") or ""
    if not pid:
        return None
    return next((r for r in state.list_json_in(paths.ACTIVE)
                 if isinstance(r, dict) and r.get("agent") == "worker" and not r.get("nested")
                 and r.get("name") == reply.get("sender_name") and str(r.get("pid") or "") == pid
                 and engine._session_alive(r.get("claude_sid"))), None)


def _try_pane(engine, live: dict, text: str) -> bool:
    try:
        res = engine.deliver_to_pane(str(live.get("claude_sid") or live.get("name") or ""),
                                     events.EVENT_MARK + "sent.reply — " + text, "", engine.reply_policy)
    except Exception:
        return False
    return isinstance(res, dict) and res.get("status") == "delivered" and res.get("confirmed") is True


def _asked(rule: str) -> set[tuple[str, str]]:
    from . import registry
    out = set()
    for path in registry.unique_question_paths():
        record = state.read_json(path)
        if not registry.is_engineer_ask(record):
            continue
        first = str(record.get("question") or "").split("\n", 1)[0]
        fields = first.split()
        if first.startswith("dm-reply: ") and len(fields) >= 6 and fields[1] == rule:
            out.add((fields[3], fields[5]))
    return out


def _ts_key(ts: str) -> float:
    try:
        return float(ts)
    except ValueError:
        return 0.0


def _deliver(engine, name: str, queued: list[tuple[str, dict]], lives: dict, now: float,
             records: list[dict]) -> None:
    groups: dict[tuple[str, str, str], list[tuple[str, dict]]] = {}
    for event_id, reply in queued:
        live = lives.get(event_id)
        if live is None or reply.get("escalated_at") is not None or reply.get("delivered_at") is not None:
            continue
        key = (str(live.get("claude_sid") or ""), reply.get("channel") or "", reply.get("thread") or "")
        groups.setdefault(key, []).append((event_id, reply))
    for (sid, channel, thread), members in groups.items():
        newest = max((reply.get("msg_ts") or "" for _, reply in members), key=_ts_key)
        text = members[0][1]["summary"] + _hint(name, [event_id for event_id, _ in members], channel, thread, newest)
        if not _try_pane(engine, lives[members[0][0]], text):
            continue
        for event_id, reply in members:
            reply["delivered_at"], reply["delivered_sid"] = now, sid
            if not event_queue.update(name, event_id, {"delivered_at": now, "delivered_sid": sid}):
                continue
            records.append({"type": "event_reply_wake", "rule": name, "event_id": event_id,
                            "worker": reply.get("sender_name") or ""})


def _answered(reply: dict, live: dict | None, rule: dict, cfg: dict) -> str | None:
    channel, msg_ts = reply.get("channel") or "", reply.get("msg_ts") or ""
    if not (cfg["sent_footer"] and cfg["self_user_id"] and channel.startswith("D") and msg_ts
            and not reply.get("thread")):
        return None
    if live is not None and reply.get("delivered_at") is None:
        return None
    return chat_archive.dm_closed(channel, msg_ts, reply.get("sender_id") or "", cfg["self_user_id"],
                                  cfg["sent_footer"], rule["inbound_skip_senders"])


def _remind_reply(name: str, rule: dict, cursor: dict, now: float, records: list[dict], cfg: dict) -> list[dict]:
    read = event_queue.read(name)
    if not read:
        return records
    engine = _engine()
    queued = []
    for ev in read:
        reply = ev.payload.get("reply")
        queued.append((ev.id, {**(reply if isinstance(reply, dict) else {}), "summary": ev.summary, "ts": ev.ts}))
    lives = {event_id: None if reply.get("inbound") else _live_worker(engine, reply) for event_id, reply in queued}
    _deliver(engine, name, queued, lives, now, records)
    escalated_now = False
    open_records = []
    for event_id, reply in queued:
        ripe = reply.get("escalated_at") is not None
        if not ripe and now - (reply.get("delivered_at") or reply.get("queued_at") or reply["ts"]) < rule["ack_sec"]:
            continue
        reason = _answered(reply, lives[event_id], rule, cfg)
        if reason is not None:
            event_queue.ack(name, event_id)
            records.append({"type": "event_reply_dropped", "rule": name, "event_id": event_id, "reason": reason})
            continue
        if not ripe:
            new_id = event_id + ":esc"
            if not event_queue.rekey(name, event_id, new_id, {"escalated_at": now}):
                continue
            records.append({"type": "event_reply_escalated", "rule": name, "event_id": event_id,
                            "new_event_id": new_id})
            event_id, escalated_now = new_id, True
        open_records.append((event_id, reply))
    asked = _asked(name) if open_records else set()
    groups: dict[str, list[tuple[str, dict]]] = {}
    for event_id, reply in open_records:
        if (reply.get("channel") or "", reply.get("msg_ts") or "") in asked:
            continue
        groups.setdefault(reply.get("domain") or paths.DEFAULT_DOMAIN, []).append((event_id, reply))
    nudged = event_queue.nudged(name)
    due = escalated_now or nudged is None or now - nudged >= rule["nudge_sec"]
    missing = []
    paged = False
    for domain, members in groups.items():
        mgr = _manager_for(domain)
        if mgr is None:
            missing.append(domain)
        elif due:
            records.append({"type": "event_queue_page", "rule": name, "count": len(members),
                            "line": members[0][1]["summary"], "event_id": members[-1][0],
                            "manager_name": mgr["name"]})
            paged = True
    if due and paged:
        event_queue.touch_nudged(name, now)
    if missing:
        return _stall(name, cursor, missing[0], now, records)
    if cursor["stalled_since"] is not None or "stalled_paged" in cursor:
        cursor["stalled_since"] = None
        cursor.pop("stalled_paged", None)
        state.write_json_atomic(_cursor_path(name), cursor)
    return records


def _reply_record(engine, name: str, rule: dict, e: events.Event, now: float, cfg: dict,
                  rules: dict, pool: list[events.Event] | None) -> tuple[str, dict] | None:
    from .event_sources import chat
    channel, thread = e.match.get("channel", ""), e.match.get("thread", "")
    if not channel or any(_rule_takes(rules[r], e, now, pool) for r in rule["unless_rules"] if r in rules):
        return None
    msg_ts = chat._message_ts(str(e.payload.get("ref") or ""))
    sender, conversation = e.match.get("sender", ""), e.match.get("conversation", "")
    target = _reply_target(e, msg_ts, now, cfg)
    if (target is not None and e.kind == "dm" and cfg["sent_footer"] and cfg["self_user_id"]
            and chat_archive.taken_over(channel, target["sent_ts"], msg_ts, cfg["sent_footer"], cfg["self_user_id"])):
        target = None
    common = {"channel": channel, "thread": thread, "msg_ts": msg_ts, "sender_id": e.payload.get("sender_id") or "",
              "queued_at": now, "delivered_at": None, "delivered_sid": "", "escalated_at": None}
    if target is not None:
        line = _reply_line(sender, conversation, channel, thread, target["name"], target["sent_ts"])
        rec = target["record"]
        reply = {**common, "sent_ts": target["sent_ts"], "sender_name": target["name"], "sender_sid": target["sid"],
                 "sender_agent": (rec.match.get("agent") or "") if rec is not None else "",
                 "sender_pid": str(rec.match.get("pid") or "") if rec is not None else "",
                 "domain": target["domain"], "link": target["link"] or e.payload.get("link") or "",
                 "text": line + _hint(name, [e.id], channel, thread, msg_ts), "inbound": False}
        live = _live_worker(engine, reply)
        if live is not None:
            reply["sender_sid"] = str(live.get("claude_sid") or target["sid"])
        return line, reply
    if rule["inbound"] and e.kind in ("dm", "dm-thread") and channel.startswith("D"):
        line = _inbound_line(sender, conversation, channel, thread, msg_ts)
        return line, {**common, "sent_ts": "", "sender_name": "", "sender_sid": "", "sender_agent": "",
                      "sender_pid": "", "domain": rule.get("domain") or paths.DEFAULT_DOMAIN,
                      "link": e.payload.get("link") or "", "text": line, "inbound": True}
    return None


def _run_reply(name: str, rule: dict, pending: list[events.Event], cursor: dict, now: float,
               records: list[dict]) -> list[dict]:
    engine = _engine()
    rules = config.events_rules()
    cfg = config.chat_archive()
    gated = any(rules[r]["while"] for r in rule["unless_rules"] if r in rules)
    pool = events.recent("exec", now) if pending and gated else None
    for e in pending:
        built = _reply_record(engine, name, rule, e, now, cfg, rules, pool)
        if built is not None:
            line, reply = built
            copy = events.Event.from_dict(e.to_dict())
            copy.summary = line
            copy.payload["reply"] = reply
            if event_queue.push(name, [copy]):
                records.append({"type": "event_reply_queued", "rule": name, "event_id": e.id,
                                "inbound": reply["inbound"], "channel": reply["channel"], "thread": reply["thread"],
                                "msg_ts": reply["msg_ts"]})
        cursor["last"] = [e.ts, e.id]
        state.write_json_atomic(_cursor_path(name), cursor)
    return _remind_reply(name, rule, cursor, now, records, cfg)


def _run_rule(name: str, rule: dict, now: float, records: list[dict]) -> list[dict]:
    engine = _engine()
    cursor = _load(name, now)
    match = rule.get("match") or {}
    pending = sorted((e for e in events.recent(rule["source"], now)
                      if e.kind == rule["kind"] and _after_cursor(e, cursor["last"])
                      and all(e.match.get(k) == v for k, v in match.items())), key=lambda e: (e.ts, e.id))
    pool = events.recent("exec", now) if rule["while"] else None
    if rule_off(rule, now, pool) is not None:
        if pending:
            cursor["last"] = [pending[-1].ts, pending[-1].id]
            state.write_json_atomic(_cursor_path(name), cursor)
        return records
    domain = rule.get("domain") or paths.DEFAULT_DOMAIN
    if rule["action"] == "reply":
        return _run_reply(name, rule, pending, cursor, now, records)
    if rule["action"] == "queue":
        start = rule_window_start(rule, now, pool) if rule["while"] else None
        return _run_queue(name, rule, pending, cursor, domain, now, records, start)
    if not pending:
        return records
    mgr = _manager_for(domain)
    if mgr is None:
        return _stall(name, cursor, domain, now, records)
    cursor["stalled_since"] = None
    cursor.pop("stalled_paged", None)
    newest = {_wait_key(e): e.id for e in pending}
    held = None
    for event in pending:
        if rule["action"] == "page":
            if newest[_wait_key(event)] == event.id:
                records.append({"type": "event_page", "rule": name, "line": event.summary, "event_id": event.id,
                                "kind": event.kind, "manager_name": mgr["name"]})
        else:
            if held is None:
                keys = [*cursor["started"]]
                if _sprint_rule(rule):
                    from . import event_poll
                    keys.extend(tracker.sprint_keys(state.read_json(event_poll._cursor_path("tracker"))))
                held = _slots_held(engine, keys, now)
            if held >= rule["max_open"]:
                break
            record = _start(engine, rule, name, event, mgr, cursor, now)
            if record is None:
                break
            records.append(record)
            if record["type"] == "event_started":
                held += 1
        cursor["last"] = [event.ts, event.id]
        state.write_json_atomic(_cursor_path(name), cursor)
    return records


def run(now: float) -> list[dict]:
    records: list[dict] = []
    for name, rule in config.events_rules().items():
        if not rule["enabled"] or rule["errors"]:
            continue
        try:
            _run_rule(name, rule, now, records)
        except Exception as err:
            records.append({"type": "event_rule_failed", "rule": name,
                            "error": f"{type(err).__name__}: {err}"[:ERROR_CHARS]})
    return records
