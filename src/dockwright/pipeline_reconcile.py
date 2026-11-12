from __future__ import annotations

import asyncio
import calendar
import dataclasses
import functools
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import config, failure_streak, lane_journal, lane_lines, paths, pipeline, state, topic_outbox, usage_cap
from . import waiting_on
from . import events as event_store
from . import mcp_server as ms
from .event_sources import tracker
from .lane_io import LaneDead, emit
from .revive import PAUSE_CLOSED_REASON, RESUME_CLOSED_REASON
from .spawner import UsageCapPaused
from .transcript import find_session_log

_PAGE_EVERY_SEC = 600
_PARKED_EVERY_SEC = 3600
_PAGES_KEEP_SEC = 86400
_NO_SUMMARY_WINDOW_SEC = 3600
_SPAWN_ERRORS = (RuntimeError, ValueError, OSError)
_STOP_WAIT_SEC = 10
_STOP_POLL_SEC = 0.5
STATUS_KEY = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")
STATUS_STREAK = "trigger-failures.json"
STATUS_LOCK = ".trigger-failures.lock"
_STATUS_PAGE_EVERY_SEC = 3600
HARVEST_AFTER_SEC = 60


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class _Pages:
    def __init__(self, manager_name):
        self.path = paths.ROOT / f".pipeline-pages-{paths._event_bucket(manager_name)}.json"

    def throttled(self, key, every, now) -> bool:
        name = f"{key[0]}:{key[1]}"
        try:
            pages = state.read_json(self.path)
        except OSError:
            pages = None
        pages = pages if isinstance(pages, dict) else {}
        last = _number(pages.get(name))
        if last is not None and 0 <= now - last < every:
            return True
        kept = {k: v for k, v in pages.items() if (t := _number(v)) is not None and now - t <= _PAGES_KEEP_SEC}
        kept[name] = now
        try:
            state.write_json_atomic(self.path, kept)
        except OSError:
            pass
        return False


def _default_gh(argv, cwd=None) -> str | None:
    from . import gh_cli
    return gh_cli.run_text(argv, cwd)


def _lane_emit():
    return emit


def _journal(task_key) -> list[dict]:
    try:
        raw = paths.artifact_events_path(task_key).read_bytes()
    except FileNotFoundError:
        return []
    out = []
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def _owner_of(events) -> str | None:
    try:
        token = pipeline.fold(events).tokens.get(pipeline.DEFAULT_TOKEN_ID)
    except (KeyError, ValueError, TypeError):
        return _scanned_owner(events)
    return token.manager if token is not None else None


def _scanned_owner(events) -> str | None:
    owner = None
    for e in events:
        if e.get("type") == "pipeline_started":
            owner = e.get("manager")
        elif e.get("type") == "token_override" and e.get("manager"):
            owner = e["manager"]
    return owner


def _records(directory) -> list[dict]:
    return [r for r in state.list_json_in(directory) if isinstance(r, dict)]


def _newest(records, match) -> dict | None:
    found = [r for r in records if match(r)]
    return max(found, key=lambda r: _number(r.get("spawned_at")) or 0.0) if found else None


class _Claimed:
    def __init__(self):
        self._records = None

    def records(self) -> list[dict]:
        if self._records is None:
            self._records = _records(paths.ASSIGNMENTS)
        return self._records

    def of(self, assignment_id) -> dict | None:
        if not assignment_id:
            return None
        return _newest(self.records(), lambda r: r.get("assignment_id") == assignment_id)


def _request_assignment(claimed, request_id, pending_aid):
    def match(r):
        return bool(pending_aid and r.get("assignment_id") == pending_aid) or r.get("pipeline_request_id") == request_id

    for kind, records in (("claimed", claimed.records()), ("pending", _records(paths.ASSIGNMENTS_PENDING))):
        record = _newest(records, match)
        if record is not None:
            return kind, record
    return None


def _window_of(assignment_id) -> str:
    try:
        return paths.pending_window_path(assignment_id).read_text().strip() or "?"
    except OSError:
        return "?"


def _holder_session(claimed, events, assignment_id):
    record = claimed.of(assignment_id) or {}
    sid = record.get("claude_sid")
    if not sid:
        for e in events:
            if e.get("type") == "advance_committed" and e.get("holder") == assignment_id:
                sid = e.get("sid")
    return sid or None, record.get("runtime") or "claude", record


def _transcript_bytes(sid, runtime) -> int | None:
    if not sid:
        return None
    log = find_session_log(sid, runtime=runtime)
    if log is None:
        return None
    try:
        return log.stat().st_size
    except OSError:
        return None


def _restamp(claimed, task_key, token_id, request_id, new_holder) -> None:
    events = ms._read_events(paths.artifact_events_path(task_key))
    at = next((i for i, e in enumerate(events)
               if e.get("type") == "advance_committed" and e.get("request_id") == request_id), None)
    if at is None:
        return
    before = pipeline.fold(events[:at]).tokens.get(token_id)
    old = before.holder if before is not None else None
    sid, runtime, _ = _holder_session(claimed, events, old) if old else (None, "claude", {})
    size = _transcript_bytes(sid, runtime)
    with ms._token_lock(task_key):
        events = ms._read_events(paths.artifact_events_path(task_key))
        current = pipeline.fold(events).tokens.get(token_id)
        if (current is None or current.holder != new_holder
                or any(e.get("type") == "holder_restamped" and e.get("request_id") == request_id for e in events)):
            return
        ms._append_task_event(task_key, {"type": "holder_restamped", "token_id": token_id, "request_id": request_id,
                                         "old": old, "new": new_holder, "transcript_bytes": size})
    from .pipeline_child import close_children_of
    close_children_of(task_key, old, "restarted")


def _output_complete(task_key, full) -> bool:
    phase, _, name = full.partition(".")
    try:
        return ms.artifact_get_impl(task_key, phase, name).get("status") == "complete"
    except (OSError, ValueError):
        return False


def _unfinished_terminal(task_key) -> bool:
    snap = ms._load_snapshot_for(task_key)
    if snap is None:
        return False
    try:
        fold_ = ms._fold_for(task_key)
    except (OSError, KeyError, ValueError, TypeError):
        return False
    if fold_.closed is not None:
        return False
    token = fold_.tokens.get(pipeline.DEFAULT_TOKEN_ID)
    if token is None or any(e.frm == token.node for e in snap.edges):
        return False
    if any(r.get("token_id") == token.token_id for r in fold_.open_requests):
        return True
    node = snap.nodes.get(token.node)
    return node is not None and not all(_output_complete(task_key, pipeline._expand(o, token.index))
                                        for o in node.outputs)


def _sight_and_owner(task_key, claimed) -> tuple[bool, str | None]:
    snap = ms._load_snapshot_for(task_key)
    if snap is None:
        return False, None
    try:
        events = ms._read_events(paths.artifact_events_path(task_key))
        fold_ = pipeline.fold(events)
    except (OSError, KeyError, ValueError, TypeError):
        return True, None
    token = fold_.tokens.get(pipeline.DEFAULT_TOKEN_ID)
    if token is None or fold_.closed is not None:
        return False, None
    return _token_in_sight(task_key, claimed, snap, fold_, token, events), token.manager


def _token_in_sight(task_key, claimed, snap, fold_, token, events) -> bool:
    if any(e.frm == token.node for e in snap.edges):
        return True
    if any(r.get("token_id") == token.token_id for r in fold_.open_requests):
        return True
    if event_store.subs_for(task_key):
        return True
    node = snap.nodes.get(token.node)
    if node is not None and not all(_output_complete(task_key, pipeline._expand(o, token.index))
                                    for o in node.outputs):
        return True
    released = {e.get("sid") for e in events if e.get("type") == "handed_off" and e.get("sid")}
    if any(ms._session_alive(sid) for sid in released):
        return True
    if token.holder is None:
        return False
    sid, _, _ = _holder_session(claimed, events, token.holder)
    return bool(sid) and ms._session_alive(sid)


def _in_the_reconcilers_sight(task_key, claimed) -> bool:
    return _sight_and_owner(task_key, claimed)[0]


def sight() -> dict[str | None, list[str]]:
    if not paths.ARTIFACTS.is_dir():
        return {}
    claimed = _Claimed()
    by_owner: dict[str | None, list[str]] = {}
    for d in paths.ARTIFACTS.iterdir():
        if not d.is_dir():
            continue
        in_sight, owner = _sight_and_owner(d.name, claimed)
        if in_sight:
            by_owner.setdefault(owner, []).append(d.name)
    return {owner: sorted(keys) for owner, keys in by_owner.items()}


def reconcilable(task_key) -> bool:
    return ms._has_open_token(task_key) or _unfinished_terminal(task_key)


def _reconcilable_task_keys() -> set[str]:
    if not paths.ARTIFACTS.is_dir():
        return set()
    claimed = _Claimed()
    return {d.name for d in paths.ARTIFACTS.iterdir() if d.is_dir() and _in_the_reconcilers_sight(d.name, claimed)}


def _attempt(events, request) -> int:
    request_id = request["request_id"]
    pending = sum(1 for e in events if e.get("type") == "advance_pending" and e.get("request_id") == request_id)
    if request.get("kind") != "restart":
        return max(2, 1 + pending)
    earlier = 0
    for e in events:
        if e.get("type") != "advance_requested":
            continue
        if e.get("request_id") == request_id:
            break
        if (e.get("kind") == "restart" and (e.get("edge") or {}).get("to") == request["edge"]["to"]
                and int(e.get("index_to") or 1) == int(request.get("index_to") or 1)):
            earlier += 1
    return 2 + earlier + pending


def _checkpoints_present(task_key, node, index) -> list[str]:
    if node is None:
        return []
    artifacts = ms._facts_for(task_key).artifacts
    return [name for name in (pipeline._expand(c, index) for c in node.checkpoints) if name in artifacts]


async def _spawn_and_commit_async(claimed, manager, task_key, snap, token, request_id, to, index_to, restart,
                                  name_suffix, kind=None) -> dict:
    kind = "restart" if restart else (kind or "reconcile")
    checkpoints = _checkpoints_present(task_key, snap.nodes.get(to), index_to) if restart else []
    try:
        if kind == "back":
            result = await ms._resume_or_spawn_back(task_key, snap, token, to, index_to, manager["sid"], request_id,
                                                    name_suffix=name_suffix)
        else:
            result = await ms._spawn_node(task_key, snap, token, to, index_to, manager["sid"], request_id, kind,
                                          checkpoints, name_suffix=name_suffix)
    except UsageCapPaused as err:
        ms._pend_request(task_key, token.token_id, request_id, usage_cap.PEND_REASON, str(err))
        return {"state": "pending", "pend_reason": usage_cap.PEND_REASON, "status": "paused", "reason": str(err)}
    except _SPAWN_ERRORS as err:
        ms._pend_request(task_key, token.token_id, request_id, "spawn-error", str(err))
        return {"state": "spawn-error", "reason": str(err)}
    committed = ms._commit_or_pend(task_key, request_id, token.token_id, to, index_to, result, None, reconciled=True)
    if committed["state"] == "committed" and restart:
        _restamp(claimed, task_key, token.token_id, request_id, committed["next"]["assignment_id"])
    return {**committed, "status": result.get("status")}


def _spawn_and_commit(claimed, manager, task_key, snap, token, request_id, to, index_to, restart, name_suffix, now,
                      pages, out, kind=None) -> None:
    committed = asyncio.run(_spawn_and_commit_async(claimed, manager, task_key, snap, token, request_id, to,
                                                    index_to, restart, name_suffix, kind))
    if committed["state"] == "spawn-error":
        still_open = any(r.get("request_id") == request_id
                         for r in ms._engine_fold(task_key, "reconcile").open_requests)
        if still_open and not pages.throttled((task_key, _page_key("stuck", token.token_id)), _PAGE_EVERY_SEC, now):
            out.append(("ADVANCE_STUCK", {"task_key": task_key, "to": to, "reason": "spawn-error"}))
        return
    if committed["state"] == "committed":
        out.append(("NODE_RESTARTED" if restart else "ADVANCE_RECONCILED",
                    {"task_key": task_key, "to": to, "name": committed["next"]["name"]}))
    elif (committed["state"] == "pending" and committed.get("pend_reason") != usage_cap.PEND_REASON
          and not pages.throttled((task_key, _page_key("stuck", token.token_id)), _PAGE_EVERY_SEC, now)):
        out.append(("ADVANCE_STUCK", {"task_key": task_key, "to": to, "reason": committed["status"] or "paused"}))


def _settle_requests(claimed, manager, task_key, snap, fold_, token, now, pages, out) -> bool:
    ttl = config.pipeline_int("requested_ttl_sec", 300)
    for req in fold_.open_requests:
        tok = fold_.tokens.get(req.get("token_id")) or pipeline.Token(
            req.get("token_id"), req["edge"]["to"], int(req.get("index_to") or 1), None, token.cwd, token.manager,
            req.get("row"))
        last = max(_number(req.get("ts")) or 0.0, _number((req.get("pending") or {}).get("ts")) or 0.0)
        if now - last < ttl:
            continue
        request_id = req["request_id"]
        to = req["edge"]["to"]
        index_to = int(req.get("index_to") or 1)
        restart = req.get("kind") == "restart"
        found = _request_assignment(claimed, request_id, (req.get("pending") or {}).get("assignment_id"))
        if found is not None and found[0] == "claimed":
            record = found[1]
            committed = ms._commit_or_pend(task_key, request_id, tok.token_id, to, index_to, {
                "status": "registered", "assignment_id": record["assignment_id"],
                "claude_sid": record.get("claude_sid"), "name": record.get("name")}, None, reconciled=True)
            if committed["state"] == "committed":
                if restart:
                    _restamp(claimed, task_key, tok.token_id, request_id, record["assignment_id"])
                out.append(("ADVANCE_SETTLED", {"task_key": task_key, "to": to, "name": committed["next"]["name"]}))
            continue
        if found is not None:
            record = found[1]
            if not pages.throttled((task_key, _page_key("unregistered", tok.token_id)), _PAGE_EVERY_SEC, now):
                out.append(("ADVANCE_UNREGISTERED", {"task_key": task_key, "name": record.get("name"),
                                                     "window": _window_of(record.get("assignment_id"))}))
            continue
        attempt = _attempt(ms._read_events(paths.artifact_events_path(task_key)), req)
        spawn_token = dataclasses.replace(tok, cwd=req["cwd"]) if req.get("cwd") else tok
        _spawn_and_commit(claimed, manager, task_key, snap, spawn_token, request_id, to, index_to, restart,
                          f"-r{attempt}", now, pages, out, req.get("kind"))
        return True
    return False


def _page_key(kind, token_id) -> str:
    return kind if token_id == pipeline.DEFAULT_TOKEN_ID else f"{kind}:{token_id}"


def _holder_verdict(claimed, task_key, snap, token, now, held) -> tuple[str, str, str]:
    events = ms._read_events(paths.artifact_events_path(task_key))
    sid, _, record = _holder_session(claimed, events, token.holder)
    if not sid or ms._session_alive(sid):
        return "skip", "", ""
    node = snap.nodes.get(token.node)
    if node is None:
        return "skip", "", ""
    closed = state.read_json(paths.CLOSED / f"{sid}.json")
    closed = closed if isinstance(closed, dict) else {}
    name = closed.get("name") or record.get("name") or sid
    if held is not None:
        return "held", name, ""
    if _waiting(task_key, snap, token) is not None:
        return "waiting", name, ""
    if (token.token_id == pipeline.DEFAULT_TOKEN_ID and not token.forked
            and any(w["node"] == token.node for w in ms.lease_waits(task_key, now))):
        return "waiting", name, ""
    outputs = [pipeline._expand(o, token.index) for o in node.outputs]
    artifacts = ms._facts_for(task_key).artifacts
    outputs_complete = bool(outputs) and all((artifacts.get(o) or {}).get("status") == "complete" for o in outputs)
    if outputs_complete and not pipeline.holder_advances([e for e in snap.edges if e.frm == token.node]):
        return "done", name, ""
    reason = str(closed.get("closed_reason") or "")
    if reason.startswith("idle>") or reason in (RESUME_CLOSED_REASON, PAUSE_CLOSED_REASON):
        gate = ms._gate_state(task_key)
        author_waits = waiting_on.process_waits(ms._process_index())[1]
        if ms._gate_at(gate, token.node, "open") or ms._author_wait_at(gate, author_waits, task_key, token.node):
            return "gate", name, reason
        if event_store.subs_for(task_key, node=token.node):
            return "gate", name, reason
        return "parked", name, reason
    closed_at = _number(closed.get("closed_at"))
    if closed_at is not None and now - closed_at < config.pipeline_int("requested_ttl_sec", 300):
        return "fresh", name, ""
    if outputs_complete:
        return "unadvanced", name, ""
    return "restart", name, ""


def _restart_refusal(fold_, token) -> str | None:
    current = fold_.tokens.get(token.token_id)
    if fold_.closed is not None:
        return "the work is closed"
    if current is None or (current.node, current.index, current.holder) != (token.node, token.index, token.holder):
        return "the token moved to another session"
    if token.token_id in fold_.held:
        return ("the token is held: release it with pipeline_advance(claude_sid=<your sid>, task_key=<key>, to=\"?\") "
                "and no reason, then send again")
    if any(r.get("token_id") == token.token_id for r in fold_.open_requests):
        return "a restart or advance of the node is already in flight"
    return None


def _restart_request(task_key, token, manager, message=None) -> dict | None:
    with ms._token_lock(task_key):
        fold_ = ms._engine_fold(task_key, "reconcile")
        if _restart_refusal(fold_, token) is not None:
            return None
        current = fold_.tokens[token.token_id]
        request = {"type": "advance_requested", "token_id": token.token_id, "request_id": uuid.uuid4().hex[:12],
                   "edge": {"from": token.node, "to": token.node}, "index_to": token.index, "kind": "restart",
                   "actor_sid": manager["sid"]}
        attempt = _attempt(ms._read_events(paths.artifact_events_path(task_key)), request)
        if message:
            path = ms._request_message_path(task_key, request["request_id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(message)
        ms._append_task_event(task_key, request)
    return {"current": current, "request_id": request["request_id"], "attempt": attempt}


def _recover_holder(claimed, manager, task_key, snap, token, now, pages, out, held=None) -> bool:
    verdict, name, reason = _holder_verdict(claimed, task_key, snap, token, now, held)
    if verdict == "held":
        hold = pipeline.latest_hold(ms._read_events(paths.artifact_events_path(task_key)), token.token_id) or {}
        fields = {"task_key": task_key, "node": token.node, "name": name,
                  "token_id": token.token_id if token.row else None,
                  "reason": " ".join(str(hold.get("reason") or held).split())[:200]}
        if lane_journal.journal_first(task_key, "NODE_HELD", {**fields, "hold": hold.get("event_id")}, ("hold",)):
            out.append(("NODE_HELD", fields))
        return False
    if verdict == "parked":
        if not pages.throttled((task_key, _page_key("parked", token.token_id)), _PARKED_EVERY_SEC, now):
            out.append(("NODE_PARKED", {"task_key": task_key, "node": token.node, "name": name,
                                        "token_id": token.token_id if token.row else None,
                                        "gate": bool(snap.nodes[token.node].gate), "reason": reason,
                                        "gate_lost": ms._gate_at(ms._gate_state(task_key), token.node, "lost")}))
        return False
    if verdict == "unadvanced":
        if not pages.throttled((task_key, _page_key("unadvanced", token.token_id)), _PAGE_EVERY_SEC, now):
            out.append(("NODE_UNADVANCED", {"task_key": task_key, "node": token.node, "index": token.index,
                                            "name": name, "row": token.row}))
        return False
    if verdict != "restart":
        return False
    request = _restart_request(task_key, token, manager)
    if request is None:
        return False
    _spawn_and_commit(claimed, manager, task_key, snap, request["current"], request["request_id"], token.node,
                      token.index, True, f"-r{request['attempt']}", now, pages, out)
    return True


def _idle_or_dead(sid) -> bool:
    if not ms._session_alive(sid):
        return True
    return (state.read_json(paths.ACTIVE / f"{sid}.json") or {}).get("state") == "idle"


def _attached(sid) -> bool:
    return ms.is_human_attached(state.read_json(paths.ACTIVE / f"{sid}.json") or {})


def due_for_fresh_session(task_key, token, sid, runtime) -> bool:
    return bool(sid) and ms._due_fresh_session(task_key, token, sid, runtime) is not None and _idle_or_dead(sid)


def _replaceable(claimed, task_key, token, by_context) -> tuple[str | None, str, str | None]:
    events = ms._read_events(paths.artifact_events_path(task_key))
    sid, runtime, record = _holder_session(claimed, events, token.holder)
    name = record.get("name") or sid
    if not sid:
        return None, "", "the token's holder has no session"
    if not _idle_or_dead(sid):
        return None, name, f"{name} is in a turn"
    if _attached(sid):
        return None, name, f"{name} is human-attached"
    requested = any(e.get("type") == "refresh_requested" and e.get("assignment_id") == token.holder for e in events)
    if not requested and by_context and not ms._declares_checkpoint(task_key, token):
        return None, name, f"{token.node} declares no checkpoint"
    if not (requested or (by_context and ms._fresh_context_over(sid, runtime) is not None)):
        return None, name, f"{name} holds less than [pipeline] fresh_context_tokens"
    refusal = _restart_refusal(ms._engine_fold(task_key, "reconcile"), token)
    return (None, name, refusal) if refusal is not None else (sid, name, None)


def _stop(sid, name) -> str | None:
    if not ms._session_alive(sid):
        return None
    try:
        ms.kill_worker_impl(sid)
    except ValueError as err:
        return f"could not stop {name}: {str(err)[:200]}"
    deadline = time.time() + _STOP_WAIT_SEC
    while ms._session_alive(sid):
        if time.time() >= deadline:
            return f"could not stop {name}: it still runs after the kill"
        time.sleep(_STOP_POLL_SEC)
    return None


def _replace_holder(claimed, manager, task_key, snap, token, now, pages, out) -> bool:
    sid, name, _ = _replaceable(claimed, task_key, token, by_context=False)
    if sid is None:
        return False
    stopped = _stop(sid, name)
    if stopped is not None:
        if not pages.throttled((task_key, _page_key("replace", token.token_id)), _PAGE_EVERY_SEC, now):
            out.append(("RECONCILE_ERROR", {"task_key": task_key, "error": stopped}))
        return False
    request = _restart_request(task_key, token, manager)
    if request is None:
        return False
    _spawn_and_commit(claimed, manager, task_key, snap, request["current"], request["request_id"], token.node,
                      token.index, True, f"-r{request['attempt']}", now, pages, out)
    return True


def _replace_refused(reason) -> dict:
    return {"route": "replace", "ok": False, "pending": False, "reason": reason, "name": None, "sid": None}


async def replace_for_message(task_key: str, token, text: str | None = None) -> dict:
    snap = ms._load_snapshot_for(task_key)
    if snap is None:
        return _replace_refused("no snapshot")
    manager_sid = ms._manager_sid_for(task_key, None)
    if manager_sid is None:
        return _replace_refused("no manager")
    manager = {"sid": manager_sid, "name": (state.read_json(paths.ACTIVE / f"{manager_sid}.json") or {}).get("name")}
    claimed = _Claimed()
    sid, name, refusal = _replaceable(claimed, task_key, token, by_context=True)
    if sid is None:
        return _replace_refused(refusal)
    stopped = _stop(sid, name)
    if stopped is not None:
        return _replace_refused(stopped)
    request = _restart_request(task_key, token, manager, text)
    if request is None:
        return _replace_refused(_restart_refusal(ms._engine_fold(task_key, "reconcile"), token)
                                or "the token moved to another session")
    committed = await _spawn_and_commit_async(claimed, manager, task_key, snap, request["current"],
                                              request["request_id"], token.node, token.index, True,
                                              f"-r{request['attempt']}")
    if committed["state"] == "committed":
        nxt = committed.get("next") or {}
        return {"route": "replace", "ok": True, "pending": False, "reason": None, "name": nxt.get("name"),
                "sid": nxt.get("sid")}
    pending = any(r.get("request_id") == request["request_id"]
                  for r in ms._engine_fold(task_key, "reconcile").open_requests)
    return {"route": "replace", "ok": pending, "pending": pending,
            "reason": committed.get("reason") or committed["state"], "name": None, "sid": None}


async def recover_for_message(task_key, token, restart_done=False, message=None) -> dict:
    snap = ms._load_snapshot_for(task_key)
    if snap is None:
        return {"route": "engine", "ok": False, "reason": "no snapshot"}
    claimed = _Claimed()
    held = ms._engine_fold(task_key, "topic_message").held.get(token.token_id)
    verdict, _, _ = _holder_verdict(claimed, task_key, snap, token, time.time(), held)
    if verdict == "skip":
        return {"route": "engine", "ok": False, "reason": "holder changed"}
    if verdict == "done" and not restart_done:
        triggers = "/".join(sorted({e.trigger for e in snap.edges if e.frm == token.node and e.trigger}))
        return {"route": "done", "ok": False,
                "reason": f"node {token.node} finished; waits on the {triggers} trigger; "
                          "the next node reads the record as an input"}
    if verdict not in ("restart", "gate", "parked", "waiting", "done"):
        return {"route": verdict, "ok": False, "reason": verdict}
    manager_sid = ms._manager_sid_for(task_key, None)
    if manager_sid is None:
        return {"route": "restart", "ok": False, "reason": "no manager"}
    manager = {"sid": manager_sid, "name": (state.read_json(paths.ACTIVE / f"{manager_sid}.json") or {}).get("name")}
    request = _restart_request(task_key, token, manager, message)
    if request is None:
        return {"route": "restart", "ok": False, "reason": "token moved"}
    committed = await _spawn_and_commit_async(claimed, manager, task_key, snap, request["current"],
                                              request["request_id"], token.node, token.index, True,
                                              f"-r{request['attempt']}")
    ok = committed["state"] == "committed"
    return {"route": "restart", "ok": ok, "reason": None if ok else committed.get("reason") or committed["state"]}


def _done_after(claimed, manager, aid, sid, threshold) -> bool:
    from .monitor import _done_event_after
    parent = state.parent_name_of(claimed.of(aid) or {}) or manager["name"]
    tasked_at = _number((state.read_json(paths.ACTIVE / f"{sid}.json") or {}).get("tasked_at")) or 0.0
    return _done_event_after(parent, sid, max(threshold, tasked_at))


def _held_open(task_key, sid, record, node, now) -> bool:
    if ms._gate_at(ms._gate_state(task_key), node, "open"):
        return True
    if ms._oldest_pending_question(sid) is not None:
        return True
    if ms.is_human_attached(record, now):
        return True
    return any(ms.is_human_attached(child, now) for child in state.live_children_of(record.get("name")))


def _close_session(task_key, record, aid, reason, out) -> None:
    name = record.get("name") or record.get("claude_sid")
    for child in state.live_children_of(name):
        ms.kill_worker_impl(child.get("name") or child.get("claude_sid"))
    ms.kill_worker_impl(name)
    out.append(("HANDED_OFF_CLOSED", {"task_key": task_key, "name": name, "assignment_id": aid, "reason": reason}))


def _released(events, holders) -> dict[str, dict]:
    handed: dict[str, dict] = {}
    for e in events:
        if e.get("type") == "holder_restamped" and e.get("new"):
            handed.pop(e["new"], None)
            continue
        aid = e.get("assignment_id")
        if e.get("type") != "handed_off" or not aid or aid in holders:
            continue
        seen = handed.setdefault(aid, {"ts": _number(e.get("ts")) or 0.0, "sid": e.get("sid")})
        seen["ts"] = min(seen["ts"], _number(e.get("ts")) or 0.0)
    return handed


def _closable_token(task_key, snap, fold_) -> pipeline.Token | None:
    token = fold_.tokens.get(pipeline.DEFAULT_TOKEN_ID)
    if token is None or token.holder is None or any(e.frm == token.node for e in snap.edges):
        return None
    if any(r.get("token_id") == token.token_id for r in fold_.open_requests):
        return None
    node = snap.nodes.get(token.node)
    if node is None or not node.outputs:
        return None
    if len(_entry_times(ms._read_events(paths.artifact_events_path(task_key)), token)) != 1:
        return None
    return token


def _finished_holder(task_key, snap, fold_) -> tuple[str, float] | None:
    token = _closable_token(task_key, snap, fold_)
    if token is None:
        return None
    artifacts = ms._facts_for(task_key).artifacts
    stamps = [artifacts.get(pipeline._expand(o, token.index)) or {} for o in snap.nodes[token.node].outputs]
    if not all(s.get("status") == "complete" for s in stamps):
        return None
    return token.holder, max(_number(s.get("written_at")) or 0.0 for s in stamps)


def _entry_times(events, token, node: str | None = None) -> list[float]:
    target = token.node if node is None else node
    entries, at = [], None
    for e in ms._round_events(events):
        if (e.get("token_id") or pipeline.DEFAULT_TOKEN_ID) != token.token_id:
            continue
        kind = e.get("type")
        if kind in ("pipeline_started", "advance_committed"):
            moved_to = e.get("node")
            if moved_to == target and at != target:
                entries.append(_number(e.get("ts")) or 0.0)
        elif kind in ("token_override", "token_back"):
            moved_to = e.get("to")
            if moved_to == target:
                entries.append(_number(e.get("ts")) or 0.0)
        else:
            continue
        at = moved_to
    return entries


def _stamp_finished(task_key, holder) -> bool:
    with ms._token_lock(task_key):
        fold_ = ms._engine_fold(task_key, "reconcile")
        token = fold_.tokens.get(pipeline.DEFAULT_TOKEN_ID)
        if token is None or token.holder != holder or fold_.closed is not None:
            return False
        if any(r.get("token_id") == token.token_id for r in fold_.open_requests):
            return False
        return ms._stamp_handed_off(task_key, token.token_id, holder)


class CloseRefused(Exception):
    pass


def _declared(names, index) -> set[str]:
    return {pipeline._expand(n, index) for n in names}


def _parked_holder(task_key, snap, fold_, node, checkpoints, outputs, expect) -> tuple[pipeline.Token, str | None]:
    if fold_.closed is not None:
        raise CloseRefused(f"{task_key} is closed")
    token = _closable_token(task_key, snap, fold_)
    if token is None:
        raise CloseRefused(f"{task_key} has no closable terminal holder: one entry into a node with no edge out "
                           "and no open request")
    if token.node != node:
        raise CloseRefused(f"the token of {task_key} is at {token.node}, not {node}")
    if token.token_id in fold_.held:
        raise CloseRefused(f"the token of {task_key} is held: {fold_.held[token.token_id]}")
    spec = snap.nodes[token.node]
    for given, declared, kind in ((checkpoints, spec.checkpoints, "checkpoint"), (outputs, spec.outputs, "output")):
        for full in given:
            if full not in _declared(declared, token.index):
                raise CloseRefused(f"{full} is not a declared {kind} of {node}")
    artifacts = ms._facts_for(task_key).artifacts
    for full in _declared(spec.outputs, token.index):
        complete = (artifacts.get(full) or {}).get("status") == "complete"
        if full in outputs and complete:
            raise CloseRefused(f"{full} is already complete")
        if full not in outputs and not complete:
            raise CloseRefused(f"{full} is not complete")
    for full, written_at in expect.items():
        if (artifacts.get(full) or {}).get("written_at") != written_at:
            raise CloseRefused(f"{full} changed since it was read")
    events = ms._read_events(paths.artifact_events_path(task_key))
    sid, _, assignment = _holder_session(_Claimed(), events, token.holder)
    if not assignment or assignment.get("handed_off_at"):
        raise CloseRefused(f"the holder of {node} is already handed off")
    record = state.read_json(paths.ACTIVE / f"{sid}.json") if sid else None
    if record:
        if not ms._reported_since_input(record, sid, root=paths.ROOT):
            raise CloseRefused(f"the holder of {node} is not parked: it got input after its last report")
        if _held_open(task_key, sid, record, node, time.time()):
            raise CloseRefused(f"the holder of {node} is held open by a gate, a question or an attached human")
    return token, sid if sid and ms._session_alive(sid) else None


def close_parked(task_key, node, checkpoints, outputs, expect, writer_sid) -> dict:
    snap = ms._load_snapshot_for(task_key)
    if snap is None:
        raise CloseRefused(f"no pipeline work {task_key}")
    with ms._token_lock(task_key):
        token, sid = _parked_holder(task_key, snap, ms._engine_fold(task_key, "topic close"), node, checkpoints,
                                    outputs, expect)
        for records, scope in ((checkpoints, "checkpoint"), (outputs, "output")):
            for full, content in records.items():
                phase, name = full.split(".", 1)
                ms.artifact_put_impl(task_key, phase, name, content, "complete", writer_sid, overwrite=True,
                                     scope=scope)
        ms._stamp_handed_off(task_key, token.token_id, token.holder)
    event_store.unsubscribe(task_key=task_key, node=node, reason="closed")
    result = {"holder": token.holder, "session": "dead" if sid is None else "left"}
    if sid is None:
        return result
    try:
        record = state.read_json(paths.ACTIVE / f"{sid}.json") or {"claude_sid": sid}
        if ms._reported_since_input(record, sid, root=paths.ROOT):
            out: list[tuple[str, dict]] = []
            _close_session(task_key, record, token.holder, "finished", out)
            result["session"] = "closed"
            for kind, fields in out:
                lane_journal.journal_once(task_key, kind, fields)
    except Exception as err:
        result["error"] = f"{type(err).__name__}: {str(err)[:200]}"
    return result


def _close_released(claimed, manager, task_key, snap, fold_, events, now, pages, out) -> None:
    grace = config.pipeline_int("handed_off_grace_sec", 300)
    positions = ms._positions_by_holder(events)
    resuming = {r["resume_candidate"] for r in fold_.open_requests
                if r.get("kind") == "back" and r.get("resume_candidate")}
    holders = {t.holder for t in fold_.tokens.values() if t.holder} | resuming
    candidates: list[tuple[str, str, str, str | None]] = []
    for aid, seen in _released(events, holders).items():
        sid = (claimed.of(aid) or {}).get("claude_sid") or seen["sid"]
        if not sid or not ms._session_alive(sid):
            continue
        node = (positions.get(aid) or (None,))[0]
        if _done_after(claimed, manager, aid, sid, seen["ts"]):
            candidates.append((aid, sid, "done", node))
        elif now - seen["ts"] >= grace:
            candidates.append((aid, sid, "grace", node))
    finished = _finished_holder(task_key, snap, fold_)
    if finished is not None:
        holder, published_at = finished
        sid, _, _ = _holder_session(claimed, events, holder)
        if sid and ms._session_alive(sid) and _done_after(claimed, manager, holder, sid, published_at):
            candidates.append((holder, sid, "finished", fold_.tokens[pipeline.DEFAULT_TOKEN_ID].node))
    for aid, sid, reason, node in candidates:
        record = state.read_json(paths.ACTIVE / f"{sid}.json") or {"claude_sid": sid}
        if _held_open(task_key, sid, record, node, now):
            continue
        if reason == "finished" and not (claimed.of(aid) or {}).get("handed_off_at"):
            if not _stamp_finished(task_key, aid):
                continue
        if pages.throttled((task_key, f"handed-off:{aid}"), _PAGE_EVERY_SEC, now):
            continue
        _close_session(task_key, record, aid, reason, out)


def _flag_no_summary(claimed, task_key, events, now, pages, out) -> None:
    self_moved: set[str] = set()
    last_put: dict[str, str] = {}
    handed: dict[str, float] = {}
    for e in events:
        kind, position = e.get("type"), e.get("actor_assignment")
        if kind in ("advance_requested", "token_retired") and position:
            self_moved.add(position)
        elif kind == "artifact_put" and position:
            last_put[position] = f"{e.get('phase')}.{e.get('name')}"
        elif kind == "handed_off" and e.get("assignment_id"):
            ts = _number(e.get("ts")) or 0.0
            handed[e["assignment_id"]] = min(handed.get(e["assignment_id"], ts), ts)
    positions = ms._positions_by_holder(events)
    for aid, ts in handed.items():
        if aid not in self_moved or last_put.get(aid) == pipeline.SUMMARY_THREAD:
            continue
        if now - ts > _NO_SUMMARY_WINDOW_SEC:
            continue
        if pages.throttled((task_key, f"no-summary:{aid}"), _PAGES_KEEP_SEC, now):
            continue
        node = (positions.get(aid) or ("?", 1))[0]
        name = (claimed.of(aid) or {}).get("name") or aid
        out.append(("NODE_NO_SUMMARY", {"task_key": task_key, "node": node, "name": name, "assignment_id": aid}))


PR_FIELDS = ("state,mergedAt,isDraft,reviewDecision,mergeStateStatus,headRefOid,statusCheckRollup,author,latestReviews,"
             "closedAt")
READY_STATUSES = ("CLEAN", "HAS_HOOKS", "UNSTABLE")
FAILED_CONCLUSIONS = frozenset({"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"})
FAILED_STATES = frozenset({"FAILURE", "ERROR"})
PENDING_STATES = frozenset({"PENDING", "EXPECTED"})
CHECK_KEYS = ("__typename", "name", "context", "workflowName", "status", "conclusion", "state", "startedAt",
              "completedAt")
UNKNOWN_RETRY_SEC = 10
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
PR_MARK = "event: github.pr — "
REVIEW_MARK = "event: github.review — "
CONFLICT_MARK = "event: github.conflict — "
LEASE_MARK = "event: lease.free — "
GITHUB_PREFIX = "event: github."
OLD_TRIGGER_MARK = "trigger: "
OLD_FEEDBACK_MARK = f"{OLD_TRIGGER_MARK}feedback — "
OLD_LEASE_MARK = "lease: free — "
TRIGGER_MARK = OLD_TRIGGER_MARK
FEEDBACK_MARK = OLD_FEEDBACK_MARK
_HANDLER_TIMEOUT_SEC = 60
_sleep = time.sleep


def _phase(data: dict) -> str:
    if data.get("mergedAt"):
        return "merged"
    if data.get("state") == "CLOSED":
        return "closed"
    status = data.get("mergeStateStatus") or ""
    if status == "DIRTY":
        return "conflict"
    decision = data.get("reviewDecision") or ""
    if decision == "CHANGES_REQUESTED":
        return "changes"
    if not data.get("isDraft") and decision in ("APPROVED", "") and status in READY_STATUSES:
        return "ready"
    return "open"


def _checks(rollup) -> str:
    items = rollup if isinstance(rollup, list) else []
    if not items:
        return "none"
    word = _checks_word(items)
    finished = sorted(json.dumps([str(i.get(k) or "") for k in CHECK_KEYS]) for i in items if _finished(i))
    if not finished:
        return word
    digest = hashlib.sha256("\n".join(finished).encode()).hexdigest()[:6]
    return f"{word}/{len(finished)}.{digest}"


def _finished(item: dict) -> bool:
    if item.get("__typename") == "StatusContext":
        return str(item.get("state") or "") not in PENDING_STATES
    return item.get("status") == "COMPLETED"


def _checks_word(items: list) -> str:
    if not all(_finished(i) for i in items):
        return "pending"
    states = [str(i.get("state") or "") for i in items if i.get("__typename") == "StatusContext"]
    runs = [i for i in items if i.get("__typename") != "StatusContext"]
    if any(str(r.get("conclusion") or "") in FAILED_CONCLUSIONS for r in runs) or any(s in FAILED_STATES for s in states):
        return "failed"
    return "passed"


def _review_stamp(data: dict) -> str:
    author = (data.get("author") or {}).get("login")
    stamps = [str(r.get("submittedAt") or "") for r in data.get("latestReviews") or []
              if isinstance(r, dict) and (r.get("author") or {}).get("login") not in (None, author)]
    return max((s for s in stamps if s), default="-")


def _pr_line(url: str, data: dict) -> str:
    parts = url.rstrip("/").split("/")
    head = str(data.get("headRefOid") or "")[:7] or "-"
    return (f"{parts[-4]}/{parts[-3]}#{parts[-1]} {_phase(data)} {head} {data.get('reviewDecision') or '-'} "
            f"{data.get('mergeStateStatus') or '-'} {_checks(data.get('statusCheckRollup'))} {_review_stamp(data)}")


def _quiet(stamp: str, now: float) -> bool:
    try:
        at = calendar.timegm(time.strptime(stamp, ISO_FORMAT))
    except ValueError:
        return True
    return now - at >= config.pipeline_int("review_quiet_sec", 1800)


def _unknown_while_open(data: dict) -> bool:
    return data.get("mergeStateStatus") == "UNKNOWN" and data.get("state") == "OPEN" and not data.get("mergedAt")


def _end_time(data: dict) -> str | None:
    return data.get("mergedAt") or data.get("closedAt") or None


def _observe(run_gh, urls, previous: dict[str, str]) -> tuple[list[str] | None, str | None, dict[str, str | None]]:
    lines, ends = [], {}
    for url in urls:
        data = _poll(run_gh, url, PR_FIELDS)
        if data is None:
            return None, url, {}
        if _unknown_while_open(data):
            _sleep(UNKNOWN_RETRY_SEC)
            again = _poll(run_gh, url, PR_FIELDS)
            if again is None:
                return None, url, {}
            if _unknown_while_open(again):
                line = _pr_line(url, {**again, "mergeStateStatus": ""})
                key = line.split(" ", 1)[0]
                kept = previous.get(key)
                if kept and kept.split(" ")[1] in ("merged", "closed"):
                    kept = None
                lines.append(" ".join(kept.split(" ")[:6] + line.split(" ")[6:]) if kept else line)
                ends[key] = _end_time(again)
                continue
            data = again
        line = _pr_line(url, data)
        lines.append(line)
        ends[line.split(" ", 1)[0]] = _end_time(data)
    return lines, None, ends


def _approve_in(snap, node_name):
    return next((e for e in snap.edges if e.to == node_name and e.trigger == "approve"), None)


def _merge_state(task_key, token) -> tuple[str, dict | None]:
    artifacts = ms._facts_for(task_key).artifacts
    stamp = artifacts.get(pipeline.MERGE_STATE)
    if stamp is None:
        return "", None
    return ms._merge_state_word(task_key, artifacts, token), stamp


def _waiting(task_key, snap, token):
    edge = _approve_in(snap, token.node)
    if edge is None:
        return None
    word, _ = _merge_state(task_key, token)
    return edge if word == "waiting" else None


def _last_observation(task_key, token, since: float = 0.0) -> tuple[str | None, dict[str, str], float | None]:
    from . import topic_message
    artifacts = ms._facts_for(task_key).artifacts
    records = [(full, stamp) for full, stamp in artifacts.items()
               if full.startswith("decision.msg-") and (_number(stamp.get("written_at")) or 0.0) > since]
    last_text, last_at = None, None
    for full, stamp in sorted(records, key=lambda p: p[1].get("written_at") or 0):
        text = topic_message.text_of(ms._artifact_body(task_key, full) or "")
        if text.startswith((GITHUB_PREFIX, OLD_TRIGGER_MARK)):
            last_text, last_at = text.split(" — ", 1)[1] if " — " in text else "", stamp.get("written_at")
    events = ms._read_events(paths.artifact_events_path(task_key))
    fired = [e for e in events
             if e.get("type") == "trigger_fired" and e.get("edge", {}).get("to") == token.node
             and (_number(e.get("ts")) or 0.0) > since]
    if fired and (last_at is None or float(fired[-1].get("ts") or 0) > float(last_at)):
        evidence = str(fired[-1].get("evidence") or "")
        if evidence.endswith(ms._EVENT_TEXT_CUT):
            last_text = None
        else:
            floor = _review_floor(events, token, fired[-1])
            carried = _by_url(last_text) if last_at is not None and float(last_at) > floor else {}
            last_text = _with_review_marks(evidence, carried, time.strftime(ISO_FORMAT, time.gmtime(floor)))
        last_at = fired[-1].get("ts")
    return last_text, _by_url(last_text), last_at


def _by_url(text: str | None) -> dict[str, str]:
    by_url = {}
    for line in (text or "").split("\n"):
        head = line.split(" ", 1)[0]
        if "#" in head:
            by_url[head] = line
    return by_url


def _review_floor(events, token, fired: dict) -> float:
    at = _number(fired.get("ts")) or 0.0
    entries = _entry_times(events, token, (fired.get("edge") or {}).get("from"))
    return max((t for t in entries if t <= at), default=0.0)


def _with_review_marks(evidence: str, carried: dict[str, str], floor_iso: str) -> str:
    lines = []
    for line in evidence.split("\n"):
        fields = line.split(" ")
        if len(fields) > 6:
            kept = (carried.get(fields[0]) or "").split(" ")
            fields[6] = kept[6] if len(kept) > 6 else floor_iso
        lines.append(" ".join(fields))
    return "\n".join(lines)


def _deliver_pr(task_key: str, node: str, lines: list[str], out: list) -> None:
    from . import topic_message
    result = asyncio.run(topic_message.deliver(task_key, PR_MARK + "\n".join(lines), kind="message",
                                               decided_by="manager", via="reconcile"))
    out.append(("TRIGGER_WAKE", {"task_key": task_key, "node": node, "record": result.get("record")}))


def _wake_waiting(manager, task_key, snap, token, now, pages, run_gh, out) -> None:
    edge = _waiting(task_key, snap, token)
    if edge is None:
        return
    source = snap.nodes.get(edge.frm)
    urls = [] if source is None else ms._node_pr_urls(task_key, source, token.index)
    if not urls or pages.throttled((task_key, "merge-poll"), config.pipeline_int("trigger_poll_sec", 600), now):
        return
    sid, _, _ = _holder_session(_Claimed(), ms._read_events(paths.artifact_events_path(task_key)), token.holder)
    if sid and not _idle_or_dead(sid):
        return
    if run_gh is None:
        run_gh = functools.partial(_default_gh, cwd=token.cwd if token.cwd and os.path.isdir(token.cwd) else None)
    last_text, by_head, last_at = _last_observation(task_key, token)
    lines, failed, _ = _observe(run_gh, list(dict.fromkeys(urls)), by_head)
    if lines is None:
        out.append(("TRIGGER_POLL_FAILED", {"task_key": task_key, "url": failed, "node": token.node}))
        return
    observation = "\n".join(lines)
    if last_at is None:
        last_at = _merge_state(task_key, token)[1].get("written_at")
    heartbeat = config.pipeline_int("trigger_heartbeat_sec", 0)
    changed = observation != last_text and _changed(lines, by_head, now)
    if not changed and not (heartbeat > 0 and last_at is not None and now - float(last_at) > heartbeat):
        return
    _deliver_pr(task_key, token.node, lines, out)


def _lease_wake(manager, task_key, token, events, now, out) -> list[tuple[str, str]]:
    waits = ms.lease_waits(task_key, now)
    for w in waits:
        if w["node"] != token.node:
            ms.drop_lease_wait(task_key, w["category"], w["node"])
    mine = [w for w in waits if w["node"] == token.node and w["free"]]
    if not mine:
        return []
    sid, _, _ = _holder_session(_Claimed(), events, token.holder)
    if sid and not _idle_or_dead(sid):
        return []
    return [(w["category"], f"{LEASE_MARK}{w['category']}") for w in mine]


def _handler_env(source: str) -> dict[str, str]:
    bin_dir = os.path.dirname(sys.executable)
    env = {"PATH": ":".join(p for p in (bin_dir, os.environ.get("PATH", "")) if p),
           "HOME": os.environ.get("HOME", "")}
    env.update({k: os.environ[k] for k in (config.ENV_CONFIG_PATH, "XDG_CONFIG_HOME") if os.environ.get(k)})
    if source == "tracker":
        from . import event_poll
        env.update(event_poll.load_env(config.events_env_file())[0] or {})
    return env


def _run_handler(task_key, sub, evs) -> str | None:
    path = event_store.handler_ok(sub.handler)
    if path is None:
        return "(handler refused)"
    try:
        run = subprocess.run([path, *sub.handler[1:]], input=json.dumps([e.to_dict() for e in evs]), text=True,
                             capture_output=True, timeout=_HANDLER_TIMEOUT_SEC, env=_handler_env(sub.source))
    except subprocess.TimeoutExpired:
        return "(handler failed: timeout)"
    except OSError as err:
        return f"(handler failed: {type(err).__name__})"
    if run.returncode != 0:
        return f"(handler failed: {run.returncode})"
    event_store.mark_delivered([sub.sub_id], evs[-1])
    ms._append_task_event(task_key, {"type": "event_handled", "sub_id": sub.sub_id, "node": sub.node,
                                     "event_ids": [e.id for e in evs]})
    return None


def _node_done(task_key, snap, token) -> bool:
    node = snap.nodes.get(token.node)
    if node is None:
        return False
    outputs = [pipeline._expand(o, token.index) for o in node.outputs]
    artifacts = ms._facts_for(task_key).artifacts
    complete = bool(outputs) and all((artifacts.get(o) or {}).get("status") == "complete" for o in outputs)
    return complete and not pipeline.holder_advances([e for e in snap.edges if e.frm == token.node])


def _event_wake(manager, task_key, snap, token, events, now, out, lease_lines) -> None:
    event_store.drop_left(task_key, ms._engine_fold(task_key, "reconcile"))
    subs = event_store.subs_for(task_key, node=token.node)
    batches: list[tuple[event_store.Sub, list[event_store.Event]]] = []
    for sub in subs:
        pool = event_store.recent(sub.source, now) + (event_store.recent("time", now) if sub.source != "time" else [])
        evs = sorted((e for e in pool if event_store.matches(sub, e)), key=lambda e: (e.ts, e.id))
        if evs:
            batches.append((sub, evs))
    if not batches and not lease_lines:
        return
    sid, _, _ = _holder_session(_Claimed(), events, token.holder)
    if sid and not _idle_or_dead(sid):
        return
    if any(kind == "TRIGGER_WAKE" and fields.get("task_key") == task_key for kind, fields in out):
        return
    if _node_done(task_key, snap, token):
        event_store.unsubscribe(task_key=task_key, node=token.node, reason="node done")
        return
    lines: list[str] = [text for _, text in lease_lines]
    delivered: list[tuple[event_store.Sub, list[event_store.Event]]] = []
    for sub, evs in batches:
        tail = _run_handler(task_key, sub, evs) if sub.handler else None
        if sub.handler and tail is None:
            if any((e.source, e.kind) in event_store.ONE_SHOT for e in evs):
                event_store.unsubscribe(sub.sub_id, reason="fired")
            continue
        lines.extend(event_store.line(e, sub) + (f" {tail}" if tail else "") for e in evs)
        delivered.append((sub, evs))
    if not lines:
        return
    from . import topic_message
    text = " · ".join(lines)[:topic_message.PANE_MAX_CHARS]
    result = asyncio.run(topic_message.deliver(task_key, text, kind="message", decided_by="manager", via="reconcile"))
    for category, _ in lease_lines:
        ms.drop_lease_wait(task_key, category, token.node)
    for sub, evs in delivered:
        event_store.mark_delivered([sub.sub_id], evs[-1])
        if any((e.source, e.kind) in event_store.ONE_SHOT for e in evs):
            event_store.unsubscribe(sub.sub_id, reason="fired")
    if delivered:
        ms._append_task_event(task_key, {"type": "event_delivered", "record": result.get("record"),
                                         "node": token.node, "sub_ids": [s.sub_id for s, _ in delivered],
                                         "event_ids": [e.id for _, evs in delivered for e in evs]})
    out.append(("EVENT_WAKE", {"task_key": task_key, "node": token.node, "record": result.get("record"),
                               "events": len(lines)}))


def _pr_trigger(manager, task_key, snap, token, now, pages, run_gh, out, read_status=None) -> None:
    outs = [e for e in snap.edges if e.frm == token.node]
    edge = next((e for e in outs if e.trigger in ("merge", "approve")), None)
    status_edge = next((e for e in outs if e.trigger == "status"), None)
    if edge is None:
        _wake_waiting(manager, task_key, snap, token, now, pages, run_gh, out)
    if status_edge is not None and _status_trigger(manager, task_key, snap, token, status_edge, now, pages, out,
                                                   read_status):
        return
    if edge is None:
        return
    urls = ms._merge_wait_urls(task_key, snap, token, ms._facts_for(task_key).artifacts)
    if not urls or pages.throttled((task_key, "merge-poll"), config.pipeline_int("trigger_poll_sec", 600), now):
        return
    if run_gh is None:
        run_gh = functools.partial(_default_gh, cwd=token.cwd if token.cwd and os.path.isdir(token.cwd) else None)
    polled = list(dict.fromkeys(urls))
    if edge.trigger == "merge" and _merge_fire(manager, task_key, edge.to, polled, run_gh, out):
        return
    since = _source_baseline(task_key, token)
    seen = _last_observation(task_key, token, since)[1]
    lines, failed, ends = _observe(run_gh, polled, seen)
    if lines is None:
        out.append(("TRIGGER_POLL_FAILED", {"task_key": task_key, "url": failed, "trigger": edge.trigger}))
        return
    if ms._engine_fold(task_key, "reconcile").closed is not None:
        return
    events = ms._read_events(paths.artifact_events_path(task_key))
    sid, _, _ = _holder_session(_Claimed(), events, token.holder)
    if sid and not _idle_or_dead(sid):
        return
    since_iso = time.strftime(ISO_FORMAT, time.gmtime(since))
    if edge.trigger == "approve" and _ready_to_fire(lines, ends, since_iso):
        result = asyncio.run(ms.pipeline_advance_impl(manager["sid"], task_key, edge.to, reason="\n".join(lines)))
        out.append(("TRIGGER_FIRED", {"task_key": task_key, "to": edge.to, "state": result.get("state")}))
        if result.get("state") == "committed" and _review_due(lines, seen, since_iso, now):
            _deliver_pr(task_key, edge.to, lines, out)
        return
    _wake_source(task_key, token, lines, seen, since, now, out)


def _default_read_status(key: str) -> tuple[str, str] | None:
    env = _handler_env("tracker")
    base = env["TRACKER_URL"].rstrip("/")
    return tracker._live_status(tracker.default_call, base, env, key)


def _status_holder_busy(task_key, token) -> bool:
    if ms._engine_fold(task_key, "reconcile").closed is not None:
        return True
    sid, _, _ = _holder_session(_Claimed(), ms._read_events(paths.artifact_events_path(task_key)), token.holder)
    if sid and not _idle_or_dead(sid):
        return True
    return bool(sid) and ms._oldest_pending_question(sid) is not None


def _status_streak(task_key, now, failed: bool) -> dict:
    folder = paths.EVENTS_CURSORS / "tracker"
    with state.file_lock(folder / STATUS_LOCK):
        streaks = failure_streak.load(folder / STATUS_STREAK)
        entry = failure_streak.fail(streaks, task_key, now) if failed else {}
        if not failed:
            failure_streak.clear(streaks, task_key)
        failure_streak.save(folder / STATUS_STREAK, streaks)
    return entry


def _status_trigger(manager, task_key, snap, token, edge, now, pages, out, read_status=None) -> bool:
    if not STATUS_KEY.match(task_key):
        return False
    if pages.throttled((task_key, "status-poll"), config.pipeline_int("trigger_poll_sec", 600), now):
        return False
    if _status_holder_busy(task_key, token):
        return False
    facts = ms._facts_for(task_key)
    timeout = config.pipeline_int("exit_check_timeout_sec", 600)
    for check in edge.exit_checks:
        ok, _ = pipeline.exit_check(check, facts, token.cwd or "", timeout, index=token.index)
        if not ok:
            return False
    read = read_status or _default_read_status
    begun = failure_streak.mark()
    try:
        found = read(task_key)
    except (tracker.SelectorFailed, KeyError):
        found = None
    if found is not None and not found[0]:
        found = None
    if found is None:
        if failure_streak.slept(begun):
            return False
        entry = _status_streak(task_key, now, failed=True)
        if failure_streak.due(entry) and not pages.throttled((task_key, "status-poll-failed"),
                                                             _STATUS_PAGE_EVERY_SEC, now):
            out.append(("TRIGGER_POLL_FAILED", {"task_key": task_key, "url": task_key, "trigger": "status",
                                                "status": ", ".join(edge.status), "to": edge.to}))
        return False
    _status_streak(task_key, now, failed=False)
    name, category = found
    if name not in edge.status and f"category:{category}" not in edge.status:
        return False
    if _status_holder_busy(task_key, token):
        return False
    result = asyncio.run(ms.pipeline_advance_impl(manager["sid"], task_key, edge.to,
                                                  reason=f"tracker status {name} ({category})"))
    out.append(("TRIGGER_FIRED", {"task_key": task_key, "to": edge.to, "state": result.get("state")}))
    return True


def _merge_fire(manager, task_key, to, polled, run_gh, out) -> bool:
    merged = []
    for url in polled:
        data = _poll(run_gh, url)
        if data is None:
            out.append(("TRIGGER_POLL_FAILED", {"task_key": task_key, "url": url, "trigger": "merge"}))
            return True
        if data.get("mergedAt"):
            merged.append(str(data["mergedAt"]))
    if len(merged) < len(polled):
        return False
    if ms._engine_fold(task_key, "reconcile").closed is not None:
        return True
    result = asyncio.run(ms.pipeline_advance_impl(manager["sid"], task_key, to, reason=max(merged)))
    out.append(("TRIGGER_FIRED", {"task_key": task_key, "to": to, "state": result.get("state")}))
    return True


def _source_baseline(task_key, token) -> float:
    entries = _entry_times(ms._read_events(paths.artifact_events_path(task_key)), token)
    return entries[-1] if entries else ms._holder_committed_at(task_key, token.holder)


def _wake_source(task_key, token, lines, seen, since, now, out) -> None:
    word = _feedback(lines, seen, time.strftime(ISO_FORMAT, time.gmtime(since)), now)
    if word is None:
        return
    from . import topic_message
    mark = CONFLICT_MARK if word == "conflict" else REVIEW_MARK
    result = asyncio.run(topic_message.deliver(task_key, mark + "\n".join(lines), kind="message",
                                               decided_by="manager", via="reconcile"))
    out.append(("TRIGGER_WAKE", {"task_key": task_key, "node": token.node, "record": result.get("record")}))


def _feedback(lines, seen, entered, now) -> str | None:
    for line in lines:
        fields = line.split(" ")
        before = (seen.get(fields[0]) or "").split(" ")
        if fields[1] == "conflict" and before[1:3] != fields[1:3]:
            return "conflict"
    return "review" if _review_due(lines, seen, entered, now) else None


def _review_due(lines: list[str], seen: dict[str, str], entered: str, now: float) -> bool:
    for line in lines:
        fields = line.split(" ")
        before = (seen.get(fields[0]) or "").split(" ")
        reviewed = before[6] if len(before) > 6 else entered
        if fields[6] != "-" and fields[6] > reviewed and _quiet(fields[6], now):
            return True
    return False


def _changed(lines, seen, now) -> bool:
    if sorted(seen) != sorted(line.split(" ", 1)[0] for line in lines):
        return True
    for line in lines:
        fields = line.split(" ")
        before = seen[fields[0]].split(" ")
        if before[:6] != fields[:6]:
            return True
        if fields[6] > (before[6] if len(before) > 6 else "-") and _quiet(fields[6], now):
            return True
    return False


def _ready_to_fire(lines: list[str], ends: dict[str, str | None], since: str) -> bool:
    keyed = [(line.split(" ")[0], line.split(" ")[1]) for line in lines]
    ended = [(key, phase) for key, phase in keyed if phase in ("merged", "closed")]
    if any(ends.get(key) is None or ends[key] > since for key, _ in ended):
        return True
    if ended and len(ended) == len(keyed):
        return True
    unmerged = [phase for _, phase in keyed if phase not in ("merged", "closed")]
    return bool(unmerged) and unmerged[0] == "ready"


def _poll(run_gh, url, fields="state,mergedAt") -> dict | None:
    raw = run_gh(["pr", "view", url, "--json", fields])
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _harvest_verdict(task_key, now) -> None:
    gate = ms._gate_state(task_key)
    round_ = (gate or {}).get("round")
    if not gate or not gate.get("out") or not isinstance(round_, int) or isinstance(round_, bool):
        return
    out = Path(gate["out"])
    try:
        if now - out.stat().st_mtime < HARVEST_AFTER_SEC:
            return
        text = out.read_text()
    except OSError:
        return
    if not text.strip() or paths.artifact_path(task_key, "decision", f"verdict-r{round_}").exists():
        return
    from . import topic_message
    try:
        asyncio.run(topic_message.deliver(task_key, text, kind="verdict", decided_by="engineer", via="gate",
                                          gate_round=round_))
    except ValueError:
        return


def _reconcile_work(manager, task_key, now, pages, run_gh, out, read_status=None) -> None:
    if _owner_of(_journal(task_key)) != manager["name"]:
        return
    _harvest_verdict(task_key, now)
    snap = ms._load_snapshot_for(task_key)
    if snap is None:
        return
    fold_ = ms._engine_fold(task_key, "reconcile")
    token = fold_.tokens.get(pipeline.DEFAULT_TOKEN_ID)
    if token is None or fold_.closed is not None:
        return
    claimed = _Claimed()
    events = ms._read_events(paths.artifact_events_path(task_key))
    _close_released(claimed, manager, task_key, snap, fold_, events, now, pages, out)
    if _settle_requests(claimed, manager, task_key, snap, fold_, token, now, pages, out):
        return
    fold_ = ms._engine_fold(task_key, "reconcile")
    if fold_.closed is not None:
        return
    busy = {r.get("token_id") for r in fold_.open_requests}
    for tok in list(fold_.tokens.values()):
        if tok.forked or tok.holder is None or tok.token_id in busy:
            continue
        if _replace_holder(claimed, manager, task_key, snap, tok, now, pages, out):
            return
        if _recover_holder(claimed, manager, task_key, snap, tok, now, pages, out, held=fold_.held.get(tok.token_id)):
            return
    events = ms._read_events(paths.artifact_events_path(task_key))
    _flag_no_summary(claimed, task_key, events, now, pages, out)
    token = fold_.tokens.get(pipeline.DEFAULT_TOKEN_ID)
    if not fold_.open_requests and token is not None and not token.forked:
        lease_lines = _lease_wake(manager, task_key, token, events, now, out)
        _pr_trigger(manager, task_key, snap, token, now, pages, run_gh, out, read_status)
        _event_wake(manager, task_key, snap, token, events, now, out, lease_lines)


def _flush(task_key: str, out: list, lines: list[str], pages: _Pages, now: float, emit=None) -> None:
    emit = emit if emit is not None else _lane_emit()
    for kind, fields in out:
        try:
            line = lane_lines.render(kind, **fields)
            if line is None:
                if lane_lines.journals(kind):
                    lane_journal.journal_once(task_key, kind, fields)
                continue
        except (KeyError, OSError, ValueError) as err:
            if pages.throttled((task_key, "error"), _PAGE_EVERY_SEC, now):
                continue
            line = lane_lines.render("RECONCILE_ERROR", task_key=task_key,
                                     error=f"{type(err).__name__}: {str(err)[:200]}")
        emit(line)
        lines.append(line)


def _drain_topic_outbox(manager, now, pages, lines, emit=None) -> None:
    if not topic_outbox.outbox_dir().is_dir():
        return
    try:
        with state.file_lock_nb(topic_outbox.drain_lock_path()):
            _drain_entries(manager, now, pages, lines, emit)
    except BlockingIOError:
        return


def _drain_entries(manager, now, pages, lines, emit=None) -> None:
    from . import topic_message
    domain = (ms._record_for_parent(manager["sid"], "pipeline") or {}).get("domain") or paths.DEFAULT_DOMAIN
    for entry in topic_outbox.entries():
        owner = entry.get("manager")
        mine = owner == manager["name"] or (entry.get("domain") == domain
                                            and not (owner and ms._live_parent_sid(owner)))
        if not mine or not topic_outbox.retry_due(entry, now):
            continue
        if asyncio.run(topic_message.redeliver(entry)).get("delivered"):
            continue
        current = state.read_json(topic_outbox.entry_path(entry["key"], entry["record"])) or {}
        first = _number(current.get("first_failed_at"))
        if current and not current.get("paged") and first is not None and now - first >= topic_outbox.PAGE_SEC:
            _flush(entry["key"], [("MESSAGE_UNDELIVERED", {
                "task_key": entry["key"], "record": entry["record"],
                "reason": " ".join(str(current.get("reason")).split())[:200]})], lines, pages, now, emit)
            topic_outbox.mark_paged(entry["key"], entry["record"])


def reconcile(manager: dict, now: float | None = None, run_gh=None, emit=None, read_status=None,
              task_keys=None) -> list[str]:
    emit = emit if emit is not None else _lane_emit()
    now = time.time() if now is None else now
    pages = _Pages(manager["name"])
    lines: list[str] = []
    try:
        if manager.get("sid") != ms.DAEMON_SID:
            _drain_topic_outbox(manager, now, pages, lines, emit)
    except LaneDead:
        raise
    except Exception as err:
        if not pages.throttled(("*", "topic-outbox"), _PAGE_EVERY_SEC, now):
            _flush("*", [("RECONCILE_ERROR", {"task_key": "*", "error": (
                f"topic-outbox {type(err).__name__}: {str(err)[:200]}")})], lines, pages, now, emit)
    try:
        task_keys = sorted(_reconcilable_task_keys() if task_keys is None else task_keys)
    except LaneDead:
        raise
    except Exception as err:
        if not pages.throttled(("*", "error"), _PAGE_EVERY_SEC, now):
            line = lane_lines.render("RECONCILE_ERROR", task_key="*", error=f"{type(err).__name__}: {str(err)[:200]}")
            lines.append(line)
            emit(line)
        return lines
    for task_key in task_keys:
        out: list[tuple[str, dict]] = []
        try:
            _reconcile_work(manager, task_key, now, pages, run_gh, out, read_status)
        except LaneDead:
            raise
        except Exception as err:
            if not pages.throttled((task_key, "error"), _PAGE_EVERY_SEC, now):
                out.append(("RECONCILE_ERROR", {"task_key": task_key,
                                                "error": f"{type(err).__name__}: {str(err)[:200]}"}))
        _flush(task_key, out, lines, pages, now, emit)
    return lines
