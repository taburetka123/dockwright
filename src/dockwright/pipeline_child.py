from __future__ import annotations

import asyncio

from . import config, paths, pipeline, state
from . import mcp_server as ms

CLAUDE_PROVIDER = "claude-tui"
CODEX_PROVIDER = "codex-exec"
_STARTED = ("registered", "no_register")
_VERDICTS = "decision.verdict-r"
_REFUSALS = (ValueError, RuntimeError, OSError)
_KILL_ERRORS = (RuntimeError, OSError, KeyError, TypeError)
_SHUTDOWN_TRIES = 5
_SHUTDOWN_RETRY_SEC = 2.0


def _events(task_key) -> list[dict]:
    return ms._read_events(paths.artifact_events_path(task_key))


def _token_of(event) -> str:
    return event.get("token_id") or pipeline.DEFAULT_TOKEN_ID


def _scope_of(event) -> tuple:
    return _token_of(event), event.get("node"), event.get("slot")


def _slot_rounds(events, scope) -> list[dict]:
    return [e for e in events if e.get("type") == "child_round" and _scope_of(e) == scope]


def _open_legs(events, owner=None, scope=None) -> list[str]:
    legs: dict[str, None] = {}
    for e in events:
        if owner is not None and e.get("assignment_id") != owner:
            continue
        if e.get("type") == "child_round" and (scope is None or _scope_of(e) == scope):
            legs.update(dict.fromkeys(e.get("legs") or []))
        elif e.get("type") == "child_closed":
            for name in e.get("names") or []:
                legs.pop(name, None)
    return list(legs)


def _killed(name) -> bool:
    try:
        result = ms.kill_worker_impl(name)
    except ValueError as err:
        return str(err).startswith("no worker named")
    except _KILL_ERRORS:
        return False
    return (result or {}).get("already_dead") is not False


def _close(task_key, owner, names, reason, gone=()) -> list[str]:
    closed = [name for name in names if _killed(name) or name in gone]
    if closed:
        ms._append_task_event(task_key, {"type": "child_closed", "assignment_id": owner, "names": closed,
                                         "reason": reason})
    return closed


def close_children_of(task_key: str, assignment_id: str, reason: str) -> list[str]:
    if not assignment_id:
        return []
    return _close(task_key, assignment_id, _open_legs(_events(task_key), assignment_id), reason)


def _arrivals(events, token_id, node) -> list[dict]:
    kinds = {e.get("request_id"): e.get("kind") for e in events if e.get("type") == "advance_requested"}
    return [e for e in events if e.get("type") == "advance_committed" and _token_of(e) == token_id
            and e.get("node") == node and kinds.get(e.get("request_id")) != "restart"]


def _entry(task_key, events, token_id, node) -> int:
    arrivals = _arrivals(events, token_id, node)
    if not arrivals:
        return 1
    first = arrivals[0].get("ts") or 0
    verdicts = [a for a in ms.artifact_list_impl(task_key)
                if f"{a.get('phase')}.{a.get('name')}".startswith(_VERDICTS) and (a.get("written_at") or 0) > first]
    return len(arrivals) + len(verdicts)


def _alive(name, sid) -> bool:
    if sid:
        return ms._session_alive(sid)
    return any(r.get("name") == name and ms._pid_reads_alive(r.get("pid")) for r in state.list_json_in(paths.ACTIVE))


def _legs_of(event) -> list[tuple[str, str, str | None]]:
    names = event.get("legs") or []
    providers = event.get("providers") or [CLAUDE_PROVIDER] * len(names)
    sids = event.get("sids") or [None] * len(names)
    return list(zip(names, providers, sids))


def _result_text(result) -> str:
    parts = [str(result.get(k)) for k in ("status", "note", "reason") if result.get(k)]
    return ": ".join(parts) or repr(result)


def _leg(result, provider, action) -> dict:
    return {"name": result.get("name"), "provider": provider, "action": action,
            "confirmed": result.get("status") == "registered", "sid": result.get("claude_sid")}


async def _spawn(claude_sid, task_key, lane, text, provider, model, guard=None, name=None) -> dict:
    return await ms.spawn_worker_impl(text, manager_sid=claude_sid, task_key=task_key, _tool="spawn_child",
                                      provider=provider, context_role=lane.role, mode=lane.mode, model=model,
                                      effort=lane.effort if provider == CLAUDE_PROVIDER else None, _agent_guard=guard,
                                      **({"name": name} if name else {}))


async def _codex_leg(claude_sid, task_key, lane, text) -> tuple[dict | None, str]:
    if not lane.codex:
        return None, "no codex lane"
    try:
        result = await _spawn(claude_sid, task_key, lane, text, CODEX_PROVIDER, lane.codex)
    except _REFUSALS as err:
        return None, f"refused: {err}"
    if result.get("status") in _STARTED:
        return _leg(result, CODEX_PROVIDER, "spawned"), str(result.get("status"))
    if result.get("name"):
        _killed(result["name"])
    return None, _result_text(result)


async def _start(claude_sid, task_key, lane, message, codex_message, guard, legacy, parent=None,
                 role_name=None) -> tuple[list[dict], str]:
    name = ms._role_child_name(parent or "unscoped", role_name) if role_name else None
    first = await _spawn(claude_sid, task_key, lane, message, CLAUDE_PROVIDER, lane.model, guard, name=name)
    if first.get("status") not in _STARTED:
        if first.get("window_id"):
            ms._close_superseded(first)
        raise ValueError(f"pipeline_child: the {lane.name} child did not start: {_result_text(first)}")
    codex, note = await _codex_leg(claude_sid, task_key, lane, codex_message or message)
    legs = [_leg(first, CLAUDE_PROVIDER, "spawned"), *([codex] if codex else [])]
    return legs, ("old snapshot: no codex leg" if legacy else note)


def _sent(name, provider, sid, result) -> dict:
    confirmed = result.get("confirmed") is not False and result.get("ok") is not False
    return {"name": name, "provider": provider, "action": "sent", "confirmed": confirmed, "sid": sid}


async def _send_codex(name, text) -> dict:
    for _ in range(_SHUTDOWN_TRIES - 1):
        try:
            return await ms.send_to_child_impl(name, text)
        except ValueError as err:
            if "shutting down" not in str(err):
                raise
        await asyncio.sleep(_SHUTDOWN_RETRY_SEC)
    return await ms.send_to_child_impl(name, text)


def _gone(task_key, owner, scope, name) -> ValueError:
    _close(task_key, owner, _open_legs(_events(task_key), owner, scope), "child gone", gone=(name,))
    return ValueError(f"pipeline_child: {name} is gone; nothing was sent and no round was spent; call again with the "
                      "child's full brief and the slot's earlier verdicts: that call starts a new child")


async def _continue(task_key, claude_sid, owner, scope, lane, latest, message, codex_message) -> tuple[list[dict], str]:
    legs = _legs_of(latest)
    claude = next(leg for leg in legs if leg[1] == CLAUDE_PROVIDER)
    if not _alive(claude[0], claude[2]):
        raise _gone(task_key, owner, scope, claude[0])
    try:
        result = await ms.send_to_child_impl(claude[0], message)
    except ValueError:
        raise _gone(task_key, owner, scope, claude[0]) from None
    if result.get("status") == "human_attached":
        raise ValueError(f"pipeline_child: {claude[0]} is human-attached (he is typing in its pane); nothing was "
                         "sent and no round was spent; call again after the flag drops")
    out = [_sent(*claude, result)]
    codex = next((leg for leg in legs if leg[1] == CODEX_PROVIDER), None)
    if codex is None:
        return out, "no codex leg: the slot's child started none"
    try:
        out.append(_sent(*codex, await _send_codex(codex[0], codex_message or message)))
        return out, "sent"
    except ValueError:
        _close(task_key, owner, [codex[0]], "codex leg could not take the round")
    fresh, note = await _codex_leg(claude_sid, task_key, lane, codex_message or message)
    return out + ([fresh] if fresh else []), note


def _journal(task_key, assignment_id, token, event) -> bool:
    with ms._token_lock(task_key):
        current = ms._held_token(ms._fold_for(task_key), assignment_id)
        if current is None or current.token_id != token.token_id:
            return False
        ms._append_task_event(task_key, event)
    return True


async def pipeline_child_impl(claude_sid: str, task_key: str, child: str, role: str | None = None,
                              message: str = "", close: bool = False, cut: str | None = None,
                              codex_message: str | None = None, lane: str | None = None) -> dict:
    caller, _, active = ms._caller_for(claude_sid)
    events = _events(task_key)
    fold_ = pipeline.fold(events)
    token = ms._held_token(fold_, caller.assignment_id)
    if token is None:
        raise ValueError(f"pipeline_child: you hold no token of {task_key}")
    snap = ms._load_snapshot_for(task_key)
    node = snap.nodes.get(token.node) if snap is not None else None
    slot = node.children.get(child) if node is not None else None
    if slot is None:
        declared = ", ".join(node.children) if node is not None and node.children else "(none)"
        raise ValueError(f"pipeline_child: [nodes.{token.node}] declares no child slot '{child}'; declared: {declared}")
    scope = (token.token_id, token.node, child)
    owner = caller.assignment_id
    if close:
        return {"ok": True, "closed": _close(task_key, owner, _open_legs(events, owner, scope), "close")}
    if role is not None and lane is not None:
        raise ValueError("pipeline_child: pass role= or lane=, not both")
    if slot.lanes:
        if role is not None:
            raise ValueError(f"pipeline_child: role names a roster row; this slot has lanes: {', '.join(slot.lanes)}")
        lane_name = lane or next(iter(slot.lanes))
        if lane_name not in slot.lanes:
            raise ValueError(f"pipeline_child: lane {lane_name!r} is not a lane of slot {child}; its lanes: "
                             f"{', '.join(slot.lanes)}")
        chosen, legacy = slot.lanes[lane_name], False
    else:
        from . import former_rows
        lane_name = role or lane or slot.roles[0]
        if lane_name not in slot.roles:
            raise ValueError(f"pipeline_child: lane {lane_name!r} is not a row of slot {child}; its rows: "
                             f"{', '.join(slot.roles)}")
        chosen, legacy = former_rows.lane_for(lane_name), True
    if not message.strip():
        raise ValueError("pipeline_child: message is empty; pass the child's brief or the round's message")
    rounds = _slot_rounds(events, scope)
    entry = _entry(task_key, events, token.token_id, token.node)
    n = 1 + sum(1 for e in rounds if e.get("entry") == entry and int(e.get("work_round") or 1) == fold_.round)
    role_name = None if (fold_.round == 1 and entry == 1) else (
        f"{chosen.role}{'-w' + str(fold_.round) if fold_.round > 1 else ''}{'-e' + str(entry) if entry > 1 else ''}")
    latest = next((e for e in reversed(rounds) if e.get("assignment_id") == owner), None)
    claude = next((leg[0] for leg in _legs_of(latest) if leg[1] == CLAUDE_PROVIDER), None) if latest else None
    if (slot.warm and latest is not None and (latest.get("lane") or latest.get("role")) == lane_name
            and claude in _open_legs(events, owner, scope)):
        sent = await _continue(task_key, claude_sid, owner, scope, chosen, latest, message, codex_message)
    else:
        _close(task_key, owner, _open_legs(events, owner, scope), "new child")
        sent = await _start(claude_sid, task_key, chosen, message, codex_message,
                            [s.entry for s in slot.subagents.values()], legacy, parent=active.get("name"),
                            role_name=role_name)
    legs, codex = sent
    event = {"type": "child_round", "token_id": token.token_id, "node": token.node, "row": token.row, "slot": child,
             "entry": entry, "round": n, "work_round": fold_.round, "lane": lane_name, "assignment_id": owner,
             "parent_sid": claude_sid,
             "legs": [leg["name"] for leg in legs], "providers": [leg["provider"] for leg in legs],
             "sids": [leg["sid"] for leg in legs]}
    if not _journal(task_key, owner, token, event):
        _close(task_key, owner, event["legs"], "token left the caller")
        raise ValueError(f"pipeline_child: the token of {task_key} left you while the child started; its legs "
                         "were closed")
    result = {"ok": True, "round": n, "entry": entry, "recommended_rounds": slot.rounds,
              "legs": [{k: leg[k] for k in ("name", "provider", "action", "confirmed")} for leg in legs],
              "codex": codex}
    notes = [f"delivery to {leg['name']} is unconfirmed: read get_child_tail({leg['name']}) before you wait"
             for leg in legs if not leg["confirmed"]]
    if n > slot.rounds:
        notes.append(f"round {n} is past the {slot.rounds} rounds recommended for slot {child}: write the convergence "
                     "judgment before you read the verdict — continue, cut, reshape or request a redesign; the count "
                     "alone goes to no manager")
    if notes:
        result["note"] = "; ".join(notes)
    return result


def refuse_spawn_child(parent_sid: str) -> None:
    sid = parent_sid
    for _ in range(config.subworkers_max_depth()):
        if not sid:
            return
        try:
            assignment = state.read_json(paths.assignment_path(sid)) or {}
        except ValueError:
            assignment = {}
        if assignment.get("pipeline_request_id"):
            key = assignment.get("ticket")
            snap = ms._load_snapshot_for(key) if key else None
            if snap is not None and pipeline.declares_children(snap):
                raise ValueError(f"spawn_child: the nodes of {key} start children only through pipeline_child: its "
                                 "schema declares them")
            return
        record = state.read_json(paths.ACTIVE / f"{sid}.json") or {}
        sid = record.get("parent_sid") or assignment.get("parent_sid")


def _records_of(target) -> list[dict]:
    for directory in (paths.ACTIVE, paths.CLOSED):
        found = [r for r in state.list_json_in(directory) if target in (r.get("name"), r.get("claude_sid"))]
        if found:
            return found
    return []


def refuse_send_to_leg(target: str, tool: str) -> None:
    for record in _records_of(target):
        sid = record.get("claude_sid")
        key = (state.read_json(paths.assignment_path(sid)) or {}).get("ticket") if sid else None
        if key and record.get("name") in _open_legs(_events(key)):
            raise ValueError(f"{tool}: {record['name']} is a child pipeline_child started for {key}: send its next "
                             "round through pipeline_child, which counts it as a round of the slot")


def children_lines(task_key: str) -> list[str]:
    events = _events(task_key)
    latest: dict[tuple, dict] = {}
    for e in events:
        if e.get("type") == "child_round":
            latest[(*_scope_of(e), int(e.get("work_round") or 1), e.get("entry"))] = e
    if not latest:
        return []
    snap = ms._load_snapshot_for(task_key)
    lines = ["\n## children"]
    for (token_id, node, slot, work_round, entry), e in latest.items():
        spec = snap.nodes[node].children.get(slot) if snap is not None and node in snap.nodes else None
        still_open = set(_open_legs(events, e.get("assignment_id")))
        legs = ", ".join(f"{name} ({'alive' if name in still_open and _alive(name, sid) else 'closed'})"
                         for name, _, sid in _legs_of(e))
        where = f"entry {entry} of work round {work_round}" if work_round > 1 else f"entry {entry}"
        lines.append(f"- {token_id} {node} {slot} {where}: round {e.get('round')}/{spec.rounds if spec else '?'} "
                     f"lane {e.get('lane') or e.get('role')} legs {legs}")
    return lines
