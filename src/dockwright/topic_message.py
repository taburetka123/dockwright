import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from pathlib import Path

from . import board, paths, pipeline, pipeline_reconcile, state, topic, topic_agent, topic_outbox
from . import mcp_server as ms

MESSAGE_KINDS = ("message", "answer", "priority")
KINDS = (*MESSAGE_KINDS, "new", "verdict")
VIAS = ("board", "cli", "mcp", "gate", "reconcile")
TEXT_MAX_BYTES = 16 * 1024
PANE_MAX_CHARS = 2000
NAME_TRIES = 99
MARKER_OPEN = "[TOPIC"
INTAKE_SCHEMA = "intake"


def stamp(now: float) -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))


def marker(record: str, decided_by: str) -> str:
    return f"{MARKER_OPEN} {record} from {decided_by}] "


def pane_text(record: str, text: str) -> str:
    if len(text) <= PANE_MAX_CHARS:
        return text
    return f"{text[:PANE_MAX_CHARS]} … (full text: artifact_get {record})"


def body(text: str, decided_by: str, via: str) -> str:
    return f"{text}\n\nfrom: {decided_by}\nvia: {via}\n"


def text_of(content: str) -> str:
    head, sep, _ = content.rpartition("\n\nfrom: ")
    return head if sep else content


def decider() -> str:
    return "manager" if os.environ.get("CLAUDE_AGENT") else "engineer"


def _check(task_key, text, kind, decided_by, via) -> None:
    if kind not in KINDS:
        raise ValueError(f"topic_message: kind must be one of {'|'.join(KINDS)}, got {kind!r}")
    if decided_by not in ms.DECIDERS:
        raise ValueError(f"topic_message: decided_by must be one of {'|'.join(ms.DECIDERS)}, got {decided_by!r}")
    if via not in VIAS:
        raise ValueError(f"topic_message: via must be one of {'|'.join(VIAS)}, got {via!r}")
    if kind != "priority" and not text.strip():
        raise ValueError("topic_message: the text is empty")
    if kind != "verdict" and len(text.encode("utf-8")) > TEXT_MAX_BYTES:
        raise ValueError(f"topic_message: the text is over {TEXT_MAX_BYTES} bytes")
    if kind != "new" and task_key not in board.keys():
        raise ValueError(f"topic_message: no topic {task_key}")


def _record_message(task_key, text, decided_by, via, now) -> str:
    base = f"msg-{stamp(now)}"
    for n in range(1, NAME_TRIES + 1):
        name = base if n == 1 else f"{base}-{n}"
        if paths.artifact_path(task_key, "decision", name).exists():
            continue
        try:
            ms.artifact_put_impl(task_key, "decision", name, body(text, decided_by, via), "complete",
                                 writer_sid=f"topic:{via}", scope="output", decided_by=decided_by)
        except ValueError:
            if paths.artifact_path(task_key, "decision", name).exists():
                continue
            raise
        return f"decision.{name}"
    raise ValueError(f"topic_message: no free record name under decision.{base}")


def _event(task_key, ref, kind, outcome, via) -> None:
    event = {"type": "message_delivered", **ref, "kind": kind, "route": outcome["route"], "ok": outcome["ok"],
             "reason": outcome["reason"], "via": via}
    if outcome.get("pane"):
        event["pane"] = outcome["pane"]
    if outcome.get("attached"):
        event["attached"] = True
    ms._append_task_event(task_key, event)


def _owner_name(task_key) -> str | None:
    sid = ms._manager_sid_for(task_key, None) or topic_agent.domain_manager_sid(task_key)
    return (state.read_json(paths.ACTIVE / f"{sid}.json") or {}).get("name") if sid else None


def _queue(task_key, record, decided_by, via, kind, reason, entry, delivered, attached=False) -> None:
    now = time.time()
    previous = entry or state.read_json(topic_outbox.entry_path(task_key, record)) or {}
    topic_outbox.write({
        "key": task_key, "record": record, "kind": kind, "decided_by": decided_by, "via": via,
        "domain": (topic.load(task_key) or {}).get("domain") or paths.DEFAULT_DOMAIN,
        "manager": previous.get("manager") or _owner_name(task_key),
        "first_failed_at": previous.get("first_failed_at") or now, "last_try_at": now,
        "reason": reason, "paged": bool(previous.get("paged")), "delivered_tokens": delivered,
        "attached": attached,
    })


def _sent(route, result, pane=None) -> dict:
    outcome = {"route": route, "ok": True, "reason": None}
    if result.get("status") == "human_attached":
        reason = f"human_attached (line: {result['line']})" if "line" in result else "human_attached"
        outcome = {"route": route, "ok": False, "reason": reason, "attached": True}
    elif result.get("confirmed") is False:
        outcome = {"route": route, "ok": False, "reason": "pane send failed"}
    if pane:
        outcome["pane"] = pane
    return outcome


def _policy(decided_by, via):
    return ms.idle_line_policy if decided_by == "engineer" or via == "reconcile" else ms.manager_policy


def _to_pane(name, line_marker, shown, policy=None) -> dict:
    try:
        result = ms.deliver_to_pane(name, shown, line_marker, policy or ms.manager_policy)
    except ValueError as err:
        return {"route": "pane", "ok": False, "reason": str(err)[:200]}
    return _sent("pane", result, pane=result.get("worker") or name)


def _resumable(name, sid) -> bool:
    try:
        _, closed = ms._find_closed_record_by_name(name)
        ms._refuse_handed_off_node(name, sid)
    except ValueError:
        return False
    return closed.get("claude_sid") == sid and ms._headless_provider(closed) is None


async def _to_holder(task_key, token, events, line_marker, shown, policy=None, restart_done=False) -> dict:
    sid, runtime, record = pipeline_reconcile._holder_session(pipeline_reconcile._Claimed(), events, token.holder)
    name = record.get("name") or sid
    if pipeline_reconcile.due_for_fresh_session(task_key, token, sid, runtime):
        replaced = await pipeline_reconcile.replace_for_message(task_key, token, f"{line_marker}{shown}")
        if replaced["ok"] or not ms._session_alive(sid):
            return replaced
    if sid and ms._session_alive(sid):
        return _to_pane(name, line_marker, shown, policy)
    if sid and name and _resumable(name, sid):
        try:
            result = await ms.send_manager_to_worker_auto_impl(name, shown, marker=line_marker)
        except (ValueError, RuntimeError) as err:
            return {"route": "resume", "ok": False, "reason": str(err)[:200]}
        return _sent("resume", result, pane=name)
    return await pipeline_reconcile.recover_for_message(task_key, token, restart_done, f"{line_marker}{shown}")


async def _to_holders(task_key, line_marker, shown, done, policy=None, restart_done=False) -> dict:
    events = ms._read_events(paths.artifact_events_path(task_key))
    fold_ = pipeline.fold(events)
    requested = {r.get("token_id") for r in fold_.open_requests}
    tokens = [t for t in fold_.tokens.values()
              if not t.forked and t.holder is not None and t.token_id not in done]
    if not tokens:
        return {"route": "engine", "ok": bool(done), "reason": None if done else "no holder", "delivered_tokens": []}
    outcomes = []
    for token in tokens:
        if token.token_id in requested:
            outcomes.append((token.token_id, {"route": "engine", "ok": False, "reason": "advance in flight"}))
            continue
        try:
            outcome = await _to_holder(task_key, token, events, line_marker, shown, policy, restart_done)
        except Exception as err:
            outcome = _failure(err)
        outcomes.append((token.token_id, outcome))
    failed = [o for _, o in outcomes if not o["ok"]]
    summary = {"route": "+".join(sorted({o["route"] for _, o in outcomes})), "ok": not failed,
               "reason": "; ".join(str(o["reason"]) for o in failed) or None,
               "delivered_tokens": [tid for tid, o in outcomes if o["ok"]],
               "attached": bool(failed) and all(o.get("attached") is True for o in failed)}
    panes = ", ".join(o["pane"] for _, o in outcomes if o.get("pane"))
    if panes:
        summary["pane"] = panes
    return summary


def _failure(err) -> dict:
    return {"route": "error", "ok": False, "reason": f"{type(err).__name__}: {str(err)[:200]}"}


def _live_topic_worker(task_key) -> dict | None:
    for assignment in reversed(ms._assignments_for_ticket(task_key)):
        sid = assignment.get("claude_sid")
        if not sid or not ms._session_alive(sid):
            continue
        active = state.read_json(paths.ACTIVE / f"{sid}.json") or {}
        if (active.get("agent") == "worker" and active.get("kind") != "subworker" and not active.get("nested")
                and ms._headless_provider(active) is None):
            return active
    return None


async def _route(task_key, record, decided_by, text, done=frozenset(), via="mcp") -> dict:
    line_marker, shown = marker(record, decided_by), pane_text(record, text)
    policy = _policy(decided_by, via)
    if pipeline_reconcile.reconcilable(task_key):
        restart_done = via == "reconcile" and text.startswith((pipeline_reconcile.REVIEW_MARK,
                                                               pipeline_reconcile.CONFLICT_MARK,
                                                               pipeline_reconcile.OLD_FEEDBACK_MARK))
        return await _to_holders(task_key, line_marker, shown, done, policy, restart_done)
    worker = _live_topic_worker(task_key)
    if worker is not None:
        return _to_pane(worker["name"], line_marker, shown, policy)
    return await topic_agent.spawn(task_key, record)


async def _deliver_record(task_key, record, text, decided_by, via, kind, entry=None) -> dict:
    done = set((entry or {}).get("delivered_tokens") or [])
    try:
        outcome = await _route(task_key, record, decided_by, text, frozenset(done), via)
    except Exception as err:
        outcome = _failure(err)
    done |= set(outcome.get("delivered_tokens") or [])
    if entry is None or outcome["ok"]:
        _event(task_key, {"record": record}, kind, outcome, via)
    if outcome["ok"]:
        topic_outbox.drop(task_key, record)
    else:
        _queue(task_key, record, decided_by, via, kind, outcome["reason"], entry, sorted(done),
               bool(outcome.get("attached")))
    return {"ok": True, "task_key": task_key, "record": record, "route": outcome["route"],
            "delivered": outcome["ok"], "reason": outcome["reason"], "pane": outcome.get("pane")}


async def _start_intake(text, domain, via) -> dict:
    if not domain or not topic.is_key(domain):
        raise ValueError("topic_message: kind new needs a domain slug")
    manager_sid = topic_agent.live_domain_manager(domain)
    if manager_sid is None:
        raise ValueError(f"topic_message: no live manager for domain {domain}; nothing was written")
    key = f"in-{stamp(time.time())}-{secrets.token_hex(2)}"
    result = await ms.pipeline_start_impl(manager_sid, key, INTAKE_SCHEMA, str(paths.ensure_worker_home()), text)
    delivered = result.get("state") == "committed"
    outcome = {"route": "intake", "ok": delivered,
               "reason": None if delivered else result.get("reason") or result.get("state")}
    _event(key, {"record": "context.ask"}, "new", outcome, via)
    return {"ok": True, "task_key": key, "record": "context.ask", "route": "intake", "delivered": delivered,
            "reason": outcome["reason"]}


async def _deliver_verdict(task_key, text, decided_by, via, gate_round) -> dict:
    if decided_by != "engineer":
        raise ValueError("topic_message: a gate verdict is the engineer's; decided_by must be engineer")
    if not isinstance(gate_round, int) or isinstance(gate_round, bool) or gate_round < 1:
        raise ValueError("topic_message: kind verdict needs round >= 1")
    try:
        parsed = json.loads(text)
    except ValueError:
        raise ValueError("topic_message: a verdict is the gate's JSON object") from None
    if not isinstance(parsed, dict) or "decision" not in parsed:
        raise ValueError("topic_message: a verdict is the gate's JSON object with a decision")
    name = f"verdict-r{gate_round}"
    ms.artifact_put_impl(task_key, "decision", name, text, "complete", writer_sid=f"topic:{via}", scope="output",
                         decided_by="engineer")
    return await _deliver_record(task_key, f"decision.{name}", text, "engineer", via, "verdict")


async def deliver(task_key, text, kind="message", decided_by="manager", via="mcp", question_id=None,
                  priority=None, domain=None, gate_round=None) -> dict:
    text = text or ""
    _check(task_key, text, kind, decided_by, via)
    if kind == "verdict":
        return await _deliver_verdict(task_key, text, decided_by, via, gate_round)
    if kind == "new":
        return await _start_intake(text, domain, via)
    if kind == "priority":
        if priority is None:
            raise ValueError("topic_message: kind priority needs priority")
        ms.topic_set_impl(task_key, f"topic:{via}", priority=priority)
        return {"ok": True, "task_key": task_key, "record": None, "route": "card", "delivered": True, "reason": None}
    if kind == "answer":
        if question_id not in {q.get("question_id") for q in ms._topic_questions(task_key)}:
            raise ValueError(f"topic_message: no open question {question_id} on {task_key}")
        ms.answer_question_impl(question_id, text, decided_by=decided_by, strict=True, via=via)
        outcome = {"route": "answer", "ok": True, "reason": None}
        _event(task_key, {"question_id": question_id}, kind, outcome, via)
        return {"ok": True, "task_key": task_key, "record": f"decision.q-{question_id[:8]}", "route": "answer",
                "delivered": True, "reason": None}
    record = _record_message(task_key, text, decided_by, via, time.time())
    return await _deliver_record(task_key, record, text, decided_by, via, kind)


async def redeliver(entry: dict) -> dict:
    task_key, record = entry["key"], entry["record"]
    phase, _, name = record.partition(".")
    try:
        content = ms.artifact_get_impl(task_key, phase, name).get("content") or ""
    except (OSError, ValueError) as err:
        topic_outbox.drop(task_key, record)
        return {"ok": False, "task_key": task_key, "record": record, "route": "error", "delivered": False,
                "reason": f"record gone: {err}"}
    return await _deliver_record(task_key, record, text_of(content), entry.get("decided_by") or "manager",
                                 entry.get("via") or "mcp", entry.get("kind") or "message", entry)


def close_node(task_key, node, text, checkpoints, outputs, expect, decided_by="manager", via="cli") -> dict:
    text = text or ""
    _check(task_key, text, "message", decided_by, via)
    closed = pipeline_reconcile.close_parked(task_key, node, checkpoints, outputs, expect, f"topic:{via}")
    result = {"ok": True, "closed": True, "task_key": task_key, "node": node, "record": None,
              "session": closed["session"]}
    errors = [closed["error"]] if closed.get("error") else []
    try:
        result["record"] = _record_message(task_key, text, decided_by, via, time.time())
        _event(task_key, {"record": result["record"]}, "close", {"route": "closed", "ok": True, "reason": None}, via)
    except (OSError, ValueError) as err:
        errors.append(f"{type(err).__name__}: {str(err)[:200]}")
    if errors:
        result["error"] = "; ".join(errors)
    return result


def read_record(task_key, full) -> dict:
    phase, _, name = full.partition(".")
    found = ms.artifact_get_impl(task_key, phase, name)
    return {"ok": True, "record": full, "status": found.get("status"), "written_at": found.get("written_at"),
            "content": found["content"]}


def _record_file(value) -> tuple[str, str]:
    full, sep, rest = value.partition("=")
    phase, dot, name = full.partition(".")
    if not sep or not dot or not phase or not name or not rest:
        raise argparse.ArgumentTypeError(f"expected PHASE.NAME=VALUE, got {value!r}")
    return full, rest


def _expected(value) -> tuple[str, float]:
    full, written_at = _record_file(value)
    try:
        return full, float(written_at)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected PHASE.NAME=WRITTEN_AT, got {value!r}") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dockwright topic", description="Write to a topic, then deliver it.")
    sub = parser.add_subparsers(dest="action", required=True)
    message = sub.add_parser("message", help="a message, an answer or a priority")
    message.add_argument("task_key")
    message.add_argument("--kind", choices=(*MESSAGE_KINDS, "verdict"), default="message")
    source = message.add_mutually_exclusive_group()
    source.add_argument("--text")
    source.add_argument("--file")
    message.add_argument("--question")
    message.add_argument("--priority", choices=topic.PRIORITIES)
    message.add_argument("--round", type=int)
    new = sub.add_parser("new", help="new work: the intake sorter under the domain's live manager")
    new.add_argument("--domain", required=True)
    source = new.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--file")
    record = sub.add_parser("record", help="print one record's stamp and body as JSON")
    record.add_argument("task_key")
    record.add_argument("record", metavar="phase.name")
    close = sub.add_parser("close", help="write a parked terminal node's records and finish it without a wake")
    close.add_argument("task_key")
    close.add_argument("--node", required=True)
    close.add_argument("--text", required=True)
    close.add_argument("--checkpoint", type=_record_file, action="append", default=[], metavar="P.N=FILE")
    close.add_argument("--output", type=_record_file, action="append", default=[], metavar="P.N=FILE")
    close.add_argument("--expect", type=_expected, action="append", default=[], metavar="P.N=WRITTEN_AT")
    return parser


def _close(args) -> int:
    try:
        result = close_node(args.task_key, args.node, args.text,
                            {full: Path(f).read_text() for full, f in args.checkpoint},
                            {full: Path(f).read_text() for full, f in args.output}, dict(args.expect),
                            decided_by=decider(), via="cli")
    except pipeline_reconcile.CloseRefused as err:
        print(json.dumps({"ok": False, "closed": False, "reason": str(err)}))
        return 3
    print(json.dumps(result))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "close":
            return _close(args)
        if args.action == "record":
            print(json.dumps(read_record(args.task_key, args.record)))
            return 0
        text = Path(args.file).read_text() if args.file else (args.text or "")
        if args.action == "new":
            result = asyncio.run(deliver(None, text, kind="new", decided_by=decider(), via="cli", domain=args.domain))
        else:
            result = asyncio.run(deliver(args.task_key, text, kind=args.kind, decided_by=decider(), via="cli",
                                         question_id=args.question, priority=args.priority, gate_round=args.round))
    except (ValueError, OSError) as err:
        print(str(err), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0
