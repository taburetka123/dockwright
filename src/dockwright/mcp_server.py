import asyncio
import concurrent.futures
import dataclasses
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from mcp.server.fastmcp import Context
from pydantic import Field
from . import (artifact_export, config, identity, lane_lines, models, monitor, names, notebook_mark, paths, pipeline,
               state, tool_schema, topic, transcript, waiting_on)
from . import events as event_store
from . import event_queue as event_queue_store
from .payload_guard import SERVER_INSTRUCTIONS, GuardedFastMCP
from .session_pid import resolve_cli_pid
from .state import _pid_alive
from .terminal import get_driver
from .transcript import (find_session_log, is_delegating, last_assistant_summary,
                         last_assistant_ends_in_tool_use)
from .registry import (
    _all_question_paths,
    _drop_questions_for_worker,
    _prune_stale_active_records,
    _question_paths,
    _resolve_unique_name,
    is_engineer_ask,
    manager_question_paths,
    unique_question_paths,
)
from .distill import (
    _DISTILL_MAX_INPUT_BYTES,
    _DISTILL_PROMPT,
    _DISTILL_TIMEOUT_SECONDS,
    _extract_tool_result_text,
    _slim_transcript,
    _distill_manager_session,
    _write_memory_file_atomic,
    distill_and_write_memory,
)

mcp = GuardedFastMCP("dockwright", instructions=SERVER_INSTRUCTIONS)

_MANAGER_SID = ("Your own session id (the session_id in your system context, $CLAUDE_CODE_SESSION_ID) — never your "
                "adjective-creature name.")
ManagerSid = Annotated[str | None, Field(description=_MANAGER_SID + (
    " It scopes the result to your own workers' records. A bad id degrades to every manager's records with a "
    "stderr warning, so you see MORE, not fewer. None reads every manager's records, orphans included."))]
ManagerSidRequired = Annotated[str, Field(description=_MANAGER_SID + " An unknown id is refused.")]
OwnSid = Annotated[str, Field(description="Your own session id, as your session context names it — never a name.")]
_TICKET_ALIAS = Field(description="Deprecated alias for task_key.")
_RESUME_QUESTION_ID = Field(description=(
    "The question_id a NO_ANSWER_YET sentinel named. Pass it with the same question to keep waiting on that "
    "pending question without duplicating it."))
_DECIDED_BY_ANSWER = Field(description=(
    "\"manager\" (default) when the answer comes through the manager — its own ruling or a relay; \"engineer\" "
    "when the engineer answered through their own channel, such as the board."))

DEFAULT_DOMAIN = paths.DEFAULT_DOMAIN
DAEMON_SID = "daemon"


def _daemon_record() -> dict | None:
    from . import daemon
    if paths.DAEMON_STOP.exists():
        return None
    beat = state.read_json(paths.DAEMON / "alive.json")
    ts = beat.get("ts") if isinstance(beat, dict) else None
    if not isinstance(ts, (int, float)) or time.time() - ts > daemon.HEARTBEAT_STALE_SEC:
        return None
    return {"claude_sid": DAEMON_SID, "name": DAEMON_SID, "agent": "daemon", "kind": "system",
            "domain": DEFAULT_DOMAIN, "pid": None}


def _record_for_parent(sid, tool: str) -> dict | None:
    if sid == DAEMON_SID:
        return _daemon_record() if tool == "pipeline" else None
    return state.read_json(paths.ACTIVE / f"{sid}.json")

_DEFAULT_REGISTRATION_TIMEOUT_SEC = 12.0
_DEFAULT_REGISTRATION_POLL_SEC = 0.5


def _backfill_legacy_workers() -> int:
    if not paths.ACTIVE.is_dir():
        return 0
    null_parent_workers: list = []
    managers: list = []
    for p in paths.ACTIVE.iterdir():
        if p.suffix != ".json":
            continue
        record = state.read_json(p)
        if record is None:
            continue
        if record.get("nested"):
            continue
        agent = record.get("agent")
        if agent == "worker" and state.parent_name_of(record) is None:
            null_parent_workers.append((p, record))
        elif agent == "manager":
            managers.append(record)
    if not null_parent_workers:
        return 0
    if len(managers) != 1:
        worker_names = [r.get("name") for _, r in null_parent_workers]
        print(
            f"backfill: skipping {len(null_parent_workers)} legacy parent-null "
            f"worker(s) {worker_names} — {len(managers)} managers active "
            f"(need exactly 1 for unambiguous attribution)",
            file=sys.stderr,
        )
        return 0
    only_manager_name = managers[0].get("name")
    if not only_manager_name:
        return 0
    count = 0
    for p, record in null_parent_workers:
        record["parent_manager_name"] = only_manager_name
        record["parent_name"] = only_manager_name
        record["parent_sid"] = managers[0].get("claude_sid")
        state.write_json_atomic(p, record)
        count += 1
    print(
        f"backfill: stamped parent_manager_name/parent_name={only_manager_name!r} and "
        f"parent_sid={managers[0].get('claude_sid')!r} on {count} legacy worker record(s)",
        file=sys.stderr,
    )
    return count


def _migrate_flat_manager_memory() -> int:
    if not paths.MANAGER_MEMORY.is_dir():
        return 0
    moved = 0
    target = paths.MANAGER_MEMORY / paths.DEFAULT_DOMAIN
    for p in paths.MANAGER_MEMORY.iterdir():
        if not p.is_file() or p.suffix != ".md":
            continue
        target.mkdir(parents=True, exist_ok=True)
        try:
            p.rename(target / p.name)
            moved += 1
        except OSError:
            pass
    return moved


def _looks_like_manager_bootstrap_ghost(record: dict, keep_window_id: str) -> bool:
    if record.get("agent") != "manager":
        return False
    if not keep_window_id:
        return False
    return state.window_id_of(record) == keep_window_id


def _prune_same_pid_ghosts(pid: int, keep_sid: str, keep_window_id: str = "") -> None:
    if not paths.ACTIVE.is_dir():
        return
    for record_path in paths.ACTIVE.iterdir():
        if record_path.suffix != ".json":
            continue
        record = state.read_json(record_path)
        if record is None:
            continue
        if record.get("pid") != pid:
            continue
        sid = record.get("claude_sid")
        if sid == keep_sid:
            continue
        if not _looks_like_manager_bootstrap_ghost(record, keep_window_id):
            continue
        record_path.unlink(missing_ok=True)
        if sid:
            _drop_questions_for_worker(sid)

def _find_question_paths(question_id: str) -> list:
    found = []
    for q_path in _all_question_paths():
        if q_path.stem == question_id:
            found.append(q_path)
            continue
        record = state.read_json(q_path)
        if record is not None and record.get("question_id") == question_id:
            found.append(q_path)
    return found


def _find_question_path(question_id: str) -> Any:
    found = _find_question_paths(question_id)
    return found[0] if found else None

def register_self_impl(
    claude_sid: str,
    agent: str,
    name: str,
    cwd: str,
    iterm_sid: str,
    pid: int | None = None,
    domain: str | None = None,
    parent_manager_name: str | None = None,
    runtime: str | None = None,
    parent_sid: str | None = None,
    kind: str | None = None,
    provider: str | None = None,
) -> dict:
    paths.ensure_dirs()
    _prune_stale_active_records()
    for record in state.list_json_in(paths.ACTIVE):
        if record.get("name") == name and record.get("claude_sid") != claude_sid:
            raise ValueError(f"name '{name}' is taken by session {record.get('claude_sid')}")
    if pid is None:
        parent_pid = os.getppid()
        pid = resolve_cli_pid(parent_pid) or parent_pid
    if agent == "manager" and not domain:
        domain = DEFAULT_DOMAIN
    prior = state.read_json(paths.ACTIVE / f"{claude_sid}.json")
    env_account = os.environ.get("CLAUDE_ORCH_ACCOUNT")
    if env_account in config.account_names():
        account = env_account
    else:
        prior_account = (prior or {}).get("account")
        account = prior_account if prior_account in config.account_names() else None
    record = {
        "claude_sid": claude_sid,
        "agent": agent,
        "name": name,
        "cwd": cwd,
        "window_id": iterm_sid,
        "pid": pid,
        "started_at": (prior or {}).get("started_at") or time.time(),
        "state": "idle",
        "last_turn_at": None,
        "last_summary": None,
        "domain": domain,
        "parent_manager_name": parent_manager_name,
        **state.tree_fields(
            agent, kind=kind, parent_name=parent_manager_name, parent_sid=parent_sid,
            parent_record=state.read_json(paths.ACTIVE / f"{parent_sid}.json") if parent_sid else None),
        "account": account,
        "provider": provider if provider else (
            state.default_provider_for(runtime) if agent == "worker" else None),
    }
    if agent in ("manager", "worker"):
        record["runtime"] = runtime or "claude"
        record["terminal"] = "tmux"
    state.write_json_atomic(paths.ACTIVE / f"{claude_sid}.json", record)
    return {"ok": True}

def _matches_manager(record: dict, manager_name: str | None) -> bool:
    if manager_name is None:
        return True
    return state.parent_name_of(record) == manager_name

def _humanize_tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{round(count / 1_000)}k"
    return str(count)


def _spend_money(by_model) -> tuple[float, bool] | None:
    if not isinstance(by_model, dict) or not by_model:
        return None
    from .pricing import cost_breakdown, get_rates

    def _num(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        return value

    rates = get_rates()
    total, all_priced = 0.0, True
    for model, bucket in by_model.items():
        if not isinstance(bucket, dict):
            continue
        breakdown = cost_breakdown(
            model, rates=rates,
            output_tokens=_num(bucket.get("out_tokens")),
            input_tokens=_num(bucket.get("in_tokens")),
            cache_read_tokens=_num(bucket.get("cache_read_tokens")),
            cache_creation_5m_tokens=_num(bucket.get("cache_creation_5m_tokens")),
            cache_creation_1h_tokens=_num(bucket.get("cache_creation_1h_tokens")))
        total += breakdown["total"]
        if not breakdown["priced"] and any(
                _num(bucket.get(key)) for key in (
                    "out_tokens", "in_tokens", "cache_read_tokens",
                    "cache_creation_5m_tokens", "cache_creation_1h_tokens")):
            all_priced = False
    return total, all_priced


def _format_spend(spend) -> str | None:
    if not isinstance(spend, dict):
        return None
    out_tokens = spend.get("out_tokens")
    if not isinstance(out_tokens, int):
        return None
    parts = []
    money = _spend_money(spend.get("by_model"))
    if money is not None:
        total, all_priced = money
        parts.append(f"{'' if all_priced else '≥'}${total:,.2f}")
    parts.append(f"{_humanize_tokens(out_tokens)} out")
    cache_read = spend.get("cache_read_tokens")
    if isinstance(cache_read, int) and cache_read > 0:
        parts.append(f"{_humanize_tokens(cache_read)} cache-rd")
    return " / ".join(parts)


def _spend_totals(spend) -> dict | None:
    if not isinstance(spend, dict):
        return None
    return {key: spend.get(key)
            for key in ("turns", "out_tokens", "in_tokens", "cache_read_tokens",
                        "cache_creation_tokens")}


def _reclaim_closed_spend(closed_record: dict) -> None:
    if closed_record.get("closed_reason") == "session_end":
        return
    from .spend_ledger import append_drop_event
    append_drop_event(closed_record, "resume_reclaim")


def _oldest_pending_question(sid: str, manager_name: str | None = None) -> dict | None:
    oldest = None
    for q_path in unique_question_paths():
        question = state.read_json(q_path)
        if question is None or question.get("worker_sid") != sid or is_engineer_ask(question):
            continue
        if not _matches_manager(question, manager_name):
            continue
        if oldest is None or (question.get("asked_at") or 0) < (oldest.get("asked_at") or 0):
            oldest = question
    return oldest


def _pid_reads_alive(pid) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        return True
    return _pid_alive(pid)


def list_workers_impl(manager_name: str | None = None) -> list[dict]:
    _prune_stale_active_records()
    workers = []
    records = list(state.list_json_in(paths.ACTIVE))
    for record in records:
        if record.get("agent") != "worker":
            continue
        if not _matches_manager(record, manager_name):
            continue
        runtime = record.get("runtime") or "claude"
        log = find_session_log(record["claude_sid"], runtime=runtime)
        summary, ts = (None, None)
        if log is not None:
            summary, ts = last_assistant_summary(log)
        worker = {
            **record,
            "last_summary": summary,
            "last_turn_at": ts,
            "alive": _pid_reads_alive(record.get("pid")),
        }
        worker["runtime"] = record.get("runtime") or "claude"
        worker["brief"] = _assignment_brief_for_sid(record.get("claude_sid"))
        worker["spend"] = _format_spend(record.get("spend"))
        pending = _oldest_pending_question(record.get("claude_sid"))
        worker["question_pending"] = None if pending is None else pending.get("question_id")
        if record.get("state") == "idle" and is_delegating(record, time.time(), log=log,
                                                           records=records):
            worker["state"] = "processing"
            worker["delegating"] = True
        worker["provider"] = state.provider_of(record)
        worker["human_attached"] = is_human_attached(record)
        until = _attached_until(record)
        worker["human_attached_until"] = (None if until is None else
                                          datetime.fromtimestamp(until).astimezone().isoformat(timespec="seconds"))
        assignment = state.read_json(paths.assignment_path(record.get("claude_sid") or "")) or {}
        for key in ("mode", "report_path", "round", "role", "context_role"):
            worker[key] = assignment.get(key)
        workers.append(worker)
    return workers

def _write_question(
    worker_sid: str,
    worker_name: str,
    question: str,
    parent_manager_name: str | None = None,
    task_key: str | None = None,
    *,
    asked_by: str | None = None,
    domain: str | None = None,
) -> str:
    qid = uuid.uuid4().hex
    keyed = bool(task_key) and not _asked_by_subworker(worker_sid)
    record = {
        "question_id": qid,
        "worker_sid": worker_sid,
        "worker_name": worker_name,
        "parent_manager_name": parent_manager_name,
        "parent_name": parent_manager_name,
        "question": question,
        "asked_at": time.time(),
        "task_key": task_key if keyed else None,
    }
    if asked_by:
        record["asked_by"] = asked_by
        record["domain"] = domain
    if keyed:
        state.write_json_atomic(paths.question_task_dir(task_key) / f"{qid}.json", record)
    else:
        state.write_json_atomic(paths.question_dir_for(parent_manager_name) / f"{qid}.json", record)
    from . import daemon
    daemon.wake()
    return qid

def ask_engineer_impl(claude_sid: str, question: str, task_key: str | None = None) -> dict:
    record = state.read_json(paths.ACTIVE / f"{claude_sid}.json")
    if not isinstance(record, dict) or record.get("agent") != "manager" or record.get("nested"):
        raise ValueError(f"ask_engineer: {claude_sid} is not a live manager; a worker asks ask_parent")
    domain = record.get("domain") or DEFAULT_DOMAIN
    key = task_key or topic.fleet_key(domain)
    if not topic.is_key(key):
        raise ValueError(f"ask_engineer: {key!r} is not a topic key")
    qid = _write_question(claude_sid, record.get("name"), question, parent_manager_name=record.get("name"),
                          task_key=key, asked_by="manager", domain=domain)
    return {"ok": True, "question_id": qid, "task_key": key}


def _write_node_question(claude_sid, worker_name, question, parent_name, tool_name) -> str:
    try:
        assignment = state.read_json(paths.assignment_path(claude_sid))
    except ValueError:
        assignment = None
    ticket = assignment.get("ticket") if isinstance(assignment, dict) else None
    if not (ticket and assignment.get("pipeline_request_id")):
        return _write_question(claude_sid, worker_name, question, parent_name, task_key=ticket)
    with _token_lock(ticket):
        if _superseded_request(assignment):
            raise ValueError(f"{tool_name} refused: the pipeline request of this session was superseded, so the node "
                             "belongs to another session; call worker_done")
        return _write_question(claude_sid, worker_name, question, parent_name, task_key=ticket)


ASK_MANAGER_TIMEOUT_SEC = 1500
ASK_MANAGER_LONG_WAIT_SEC = 12 * 3600
ASK_KEEPALIVE_SEC = 60


def _reask_sentinel(qid: str, timeout_sec: float, tool_name: str = "ask_manager",
                    sid_param: str = "claude_sid") -> str:
    return (
        f"NO_ANSWER_YET: the manager has not answered within {timeout_sec:.0f}s. "
        f"Your question is still pending (question_id: {qid}). To keep waiting, call "
        f"{tool_name} again with the same {sid_param} and question text plus "
        f'resume_question_id="{qid}". Do not proceed without the answer, and do not '
        "re-send the question without resume_question_id — that would duplicate it."
    )


def _try_consume_answer(qid: str, claude_sid: str) -> str | None:
    answer_path = paths.ANSWERS / f"{qid}.json"
    if not answer_path.exists():
        return None
    data = state.read_json(answer_path)
    if data is not None and "answer" in data:
        stamp = data.get("worker_sid")
        if stamp is not None and stamp != claude_sid:
            raise ValueError(
                f"answer for question {qid} belongs to another worker; "
                "a worker may only resume its own question"
            )
        answer_path.unlink(missing_ok=True)
        for q_path in _find_question_paths(qid):
            q_path.unlink(missing_ok=True)
        return data["answer"]
    answer_path.unlink(missing_ok=True)
    return None


async def ask_manager_impl(
    claude_sid: str,
    question: str,
    poll_interval: float = 0.5,
    timeout_sec: float | None = None,
    resume_question_id: str | None = None,
    tool_name: str = "ask_manager",
    sid_param: str = "claude_sid",
    keepalive=None,
    keepalive_sec: float | None = None,
) -> str:
    record = state.read_json(paths.ACTIVE / f"{claude_sid}.json")
    if record is None:
        raise ValueError(f"session {claude_sid} not registered; SessionStart hook missing?")
    if record.get("nested"):
        raise ValueError(
            f"session {claude_sid} is a nested sub-session of "
            f"{record.get('nested_parent_name') or 'another session'}; {tool_name} is "
            "disabled for nested sessions — surface the question to the parent process instead"
        )
    if timeout_sec is None:
        timeout_sec = ASK_MANAGER_LONG_WAIT_SEC if keepalive is not None else ASK_MANAGER_TIMEOUT_SEC
    if keepalive_sec is None:
        keepalive_sec = ASK_KEEPALIVE_SEC
    limit = config.ask_parent_wait_sec(state.provider_of(record))
    if limit:
        timeout_sec = min(timeout_sec, limit)
    if resume_question_id is None:
        qid = await asyncio.to_thread(_write_node_question, claude_sid, record["name"], question,
                                      state.parent_name_of(record), tool_name)
    else:
        qid = resume_question_id
        answer = _try_consume_answer(qid, claude_sid)
        if answer is not None:
            return answer
        q_path = _find_question_path(qid)
        q_record = state.read_json(q_path) if q_path is not None else None
        if q_record is not None:
            if q_record.get("worker_sid") != claude_sid:
                raise ValueError(
                    f"question {qid} belongs to another worker; "
                    "a worker may only resume its own question"
                )
        else:
            answer = _try_consume_answer(qid, claude_sid)
            if answer is not None:
                return answer
            raise ValueError(f"no pending question or answer with id {qid}")
    deadline = time.monotonic() + timeout_sec
    next_beat = time.monotonic() + keepalive_sec
    beats = 0
    while True:
        answer = _try_consume_answer(qid, claude_sid)
        if answer is not None:
            return answer
        now_m = time.monotonic()
        if now_m >= deadline:
            return _reask_sentinel(qid, timeout_sec, tool_name, sid_param)
        if keepalive is not None and now_m >= next_beat:
            beats += 1
            try:
                await keepalive(beats)
            except Exception:
                return _reask_sentinel(qid, timeout_sec, tool_name, sid_param)
            next_beat = now_m + keepalive_sec
        await asyncio.sleep(poll_interval)


def _progress_keepalive(ctx):
    try:
        meta = ctx.request_context.meta if ctx is not None else None
    except Exception:
        return None
    if meta is None or getattr(meta, "progressToken", None) is None:
        return None

    async def beat(n: int) -> None:
        await ctx.report_progress(n)

    return beat

def _answer_addressee(question: dict) -> str:
    asked = question.get("parent_manager_name")
    live = [r for r in state.list_json_in(paths.ACTIVE)
            if isinstance(r, dict) and r.get("agent") == "manager" and not r.get("nested")
            and _session_alive(r.get("claude_sid"))]
    if any(r.get("name") == asked for r in live):
        return asked
    same_domain = sorted(r.get("name") for r in live if r.get("domain") == question.get("domain") and r.get("name"))
    return same_domain[0] if same_domain else asked


def _answer_engineer_ask(question, question_id, text, answered_at, decided_by, q_paths, via) -> dict:
    from . import daemon
    task_key = question.get("task_key")
    if not task_key:
        raise ValueError(f"no topic to record the answer to question {question_id} under; nothing was answered")
    _put_decision(task_key, question_id, question, text, answered_at, None, decided_by)
    if via == "board" or (via == "cli" and decided_by == "engineer"):
        tail = str(text).split("\n", 1)[0].strip()[:300]
        daemon.OutboxEmit(_answer_addressee(question), source="board", domain=question.get("domain")).emit(
            lane_lines.render("ANSWER", task_key=task_key, qid=question_id[:8], answer=tail))
    for q_path in q_paths:
        q_path.unlink(missing_ok=True)
    return {"ok": True, "question_id": question_id, "task_key": task_key}


def answer_question_impl(question_id: str, text: str, decided_by: str = "manager", strict: bool = False,
                         via: str | None = None) -> dict:
    if decided_by not in DECIDERS:
        raise ValueError(f"decided_by must be one of {'|'.join(DECIDERS)}, got {decided_by!r}")
    q_paths = [p for p in _find_question_paths(question_id) if p.exists()]
    if not q_paths:
        raise ValueError(f"no pending question with id {question_id}")
    question = state.read_json(q_paths[0])
    answered_at = time.time()
    if is_engineer_ask(question):
        return _answer_engineer_ask(question, question_id, text, answered_at, decided_by, q_paths, via)
    worker_sid = (question or {}).get("worker_sid")
    try:
        record = _decision_assignment(worker_sid)
        ticket = record.get("ticket") or (question or {}).get("task_key")
        if ticket:
            _put_decision(ticket, question_id, question, text, answered_at, record.get("assignment_id"),
                          decided_by)
            _journal_answer(ticket, question_id, question, text, record.get("assignment_id"), decided_by)
        elif strict:
            raise ValueError(f"no topic to record the answer to question {question_id} under; nothing was answered")
    except (ValueError, OSError):
        if strict:
            raise
    payload = {"question_id": question_id, "answer": text, "answered_at": answered_at}
    if worker_sid:
        payload["worker_sid"] = worker_sid
    state.write_json_atomic(paths.ANSWERS / f"{question_id}.json", payload)
    for q_path in q_paths:
        q_path.unlink(missing_ok=True)
    return {"ok": True}


def _asked_by_subworker(worker_sid) -> bool:
    return (state.read_json(paths.ACTIVE / f"{worker_sid}.json") or {}).get("kind") == "subworker"


def _decision_assignment(worker_sid) -> dict:
    record = state.read_json(paths.assignment_path(worker_sid)) if worker_sid else None
    if not isinstance(record, dict) or not record.get("ticket") or _asked_by_subworker(worker_sid):
        return {}
    return record


def _topic_questions(task_key) -> list[dict]:
    found = {}

    def keep(q_path, question):
        qid = question.get("question_id") or q_path.stem
        found.setdefault(qid, {**question, "question_id": qid})

    task_dir = paths.question_task_dir(task_key)
    if task_dir.is_dir():
        for q_path in sorted(task_dir.glob("*.json")):
            question = state.read_json(q_path)
            if isinstance(question, dict):
                keep(q_path, question)
    for q_path in _question_paths():
        question = state.read_json(q_path)
        if not isinstance(question, dict):
            continue
        if question.get("task_key"):
            if question["task_key"] == task_key:
                keep(q_path, question)
            continue
        try:
            ticket = _decision_assignment(question.get("worker_sid")).get("ticket")
        except (ValueError, OSError):
            continue
        if ticket == task_key:
            keep(q_path, question)
    return sorted(found.values(), key=lambda q: q.get("asked_at") or 0)


def _pending_questions(task_key) -> list[dict]:
    return [{"question_id": q.get("question_id"), "worker_sid": q.get("worker_sid"), "asked_at": q.get("asked_at")}
            for q in _topic_questions(task_key)]


def _pending_question_ids(task_key) -> list[str]:
    return [q["question_id"] for q in _topic_questions(task_key) if not is_engineer_ask(q)]


def _round_started_event(events) -> dict | None:
    return next((e for e in reversed(events) if e.get("type") == "round_started"), None)


DECIDERS = ("manager", "engineer")


def _put_decision(task_key, question_id, question, text, answered_at, assignment_id, decided_by) -> None:
    position = None
    if _load_snapshot_for(task_key) is not None and assignment_id:
        position = _positions_by_holder(_read_events(paths.artifact_events_path(task_key))).get(assignment_id)
    node = position[0] if position else None
    body = "\n".join([f"# decision {question_id[:8]}", "", "## question", str(question.get("question", "")), "",
                      "## answer", text, "", f"asked_by: {question.get('worker_name')} ({question.get('worker_sid')})",
                      f"node: {node or '-'}", f"asked_at: {question.get('asked_at')}",
                      f"answered_at: {answered_at}", f"decided_by: {decided_by}"])
    name, writer_sid = f"q-{question_id[:8]}", f"answer:{question_id}"
    replaces_own = _put_clobber_verdict(paths.artifact_path(task_key, "decision", name), "decision", name, body,
                                        "complete", writer_sid) == "own_complete_record"
    artifact_put_impl(task_key, "decision", name, body, "complete", writer_sid=writer_sid, scope="output",
                      decided_by=decided_by, overwrite=replaces_own)


def _first_line(text) -> str:
    return str(text or "").split("\n", 1)[0].strip()[:200]


def _journal_answer(task_key, question_id, question, text, assignment_id, decided_by) -> None:
    if not assignment_id or _load_snapshot_for(task_key) is None:
        return
    _append_task_event(task_key, {"type": "question_answered", "question_id": question_id,
                                  "assignment_id": assignment_id, "decided_by": decided_by,
                                  "question_head": _first_line((question or {}).get("question")),
                                  "answer_head": _first_line(text)})

def list_pending_questions_impl(manager_name: str | None = None, include_engineer_asks: bool = False) -> list[dict]:
    questions = []
    for q_path in unique_question_paths():
        question = state.read_json(q_path)
        if question is None or not _matches_manager(question, manager_name):
            continue
        if not include_engineer_asks and is_engineer_ask(question):
            continue
        questions.append(question)
    questions.sort(key=lambda q: q.get("asked_at", 0))
    return questions

def _find_worker_by_name_or_sid(identifier: str) -> dict:
    non_worker_holder = None
    for record in state.list_json_in(paths.ACTIVE):
        if record.get("name") == identifier or record.get("claude_sid") == identifier:
            if record.get("agent") == "worker":
                return record
            non_worker_holder = record
    if non_worker_holder is not None:
        agent = non_worker_holder.get("agent") or "session"
        raise ValueError(
            f"'{identifier}' is an active {agent}, not a worker; "
            f"use send_manager_to_manager to message managers"
        )
    raise ValueError(f"no worker named '{identifier}'")


def _attached_until(record: dict) -> float | None:
    stamp = record.get("human_attached_at")
    if not is_human_attached(record):
        return None
    return float(stamp) + HUMAN_ATTACHED_TTL_SEC


def _attached_refusal(record: dict) -> str:
    until = datetime.fromtimestamp(_attached_until(record) or time.time()).strftime("%H:%M")
    name = record.get("name") or record.get("claude_sid")
    return (f"refused: {name} is human-attached — he is typing in its pane; the flag drops at {until} "
            f"({HUMAN_ATTACHED_TTL_SEC // 60} min after his last line) or now with "
            f"set_human_attached(\"{name}\", false)")


def _refuse_if_attached(identifier: str) -> None:
    try:
        record = _find_worker_by_name_or_sid(identifier)
    except ValueError:
        return
    if is_human_attached(record):
        raise ValueError(_attached_refusal(record))


def _pane_refusal(record: dict) -> dict:
    until = datetime.fromtimestamp(_attached_until(record) or time.time()).strftime("%H:%M")
    return {"status": "human_attached", "delivered": False, "worker": record["name"], "until": until,
            "explanation": _attached_refusal(record)}


def manager_policy(record: dict, window_id: str) -> dict | None:
    return _pane_refusal(record) if is_human_attached(record) else None


def idle_line_policy(record: dict, window_id: str) -> dict | None:
    if not is_human_attached(record):
        return None
    line = _input_state(_capture_text(window_id))
    return None if line == "idle" else {**_pane_refusal(record), "line": line}


def reply_policy(record: dict, window_id: str) -> dict | None:
    line = _input_state(_capture_text(window_id))
    if line not in ("idle", "queued") or (is_human_attached(record) and line != "idle"):
        return {**_pane_refusal(record), "line": line}
    return None

def _capture_text(window_id: str) -> str | None:
    return get_driver().capture_screen_ansi(window_id)


def _capture_plain(window_id: str) -> str | None:
    return get_driver().capture_screen(window_id)


from .stale_monitor import (  # noqa: E402  re-exported: stale_monitor is the
    _input_is_idle,             # canonical home, since it also ships standalone
    _input_state,
    _SGR_RE,
    _strip_ansi,
    _strip_dim_spans,
    is_human_attached,
    HUMAN_ATTACHED_TTL_SEC,
    _stamp_pane_send,
    RATE_LIMIT_NUDGE_SEC,
    RATE_LIMIT_SIGNATURES,
    _approval_dialog_block,
    _dialog_sign,
    _open_prompt,
    _prompt_holds,
    _prompt_status,
    _update_marker,
    _append_permission_ledger,
    SELF_EDIT_OPTION_TEXT,
    SAFEGUARD_SETTLE_POLLS,
    SAFEGUARD_SETTLE_STEP_SEC,
    _limit_banner_text,
    _looks_like_session,
    _parse_iso,
    _parse_limit_reset_ts,
    _process_index,
    _reported_since_input,
)


def _type_unguarded(window_id: str, text: str) -> bool:
    """NOT the guarded `stale_monitor._send_text`. This types with no input-box
    check, because a relay must always deliver; each of its two callers decides
    for itself whether to type, on its own rule. Returns whether the terminal
    confirmed the send — callers still report `delivered` either way, but only a
    confirmed send may be stamped."""
    try:
        return get_driver().send_text(window_id, text) is not False
    except Exception:
        return False


_WINDOW_RESOLVE_RETRIES = 3
_WINDOW_RESOLVE_RETRY_SLEEP = 1.0

MANAGER_MARKER_OPEN = "[MANAGER"
MANAGER_MARKER = f"{MANAGER_MARKER_OPEN}] "

_INPUT_READY_TIMEOUT_SEC = 15.0
_INPUT_READY_POLL_SEC = 0.5
_INPUT_READY_CODEX_SLEEP_SEC = 2.0


async def _await_input_ready(window_id: str, runtime: str) -> None:
    if runtime != "claude":
        await asyncio.sleep(_INPUT_READY_CODEX_SLEEP_SEC)
        return
    if not window_id:
        return
    deadline = time.monotonic() + _INPUT_READY_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if _input_is_idle(await asyncio.to_thread(_capture_text, window_id)):
            return
        await asyncio.sleep(_INPUT_READY_POLL_SEC)


def _match_worker_window_by_cwd_runtime(data: list, record: dict) -> str:
    cwd = record.get("cwd")
    if not cwd or not data:
        return ""
    runtime = (record.get("runtime") or "claude").lower()
    matches = []
    for os_window in data:
        for tab in os_window.get("tabs", []):
            for w in tab.get("windows", []):
                if w.get("cwd") != cwd:
                    continue
                fps = w.get("foreground_processes")
                if fps is None:
                    wid = str(w.get("id", ""))
                    if wid:
                        matches.append(wid)
                    continue
                cmdlines = " ".join(
                    " ".join(p.get("cmdline") or [])
                    for p in fps
                ).lower()
                if runtime in cmdlines:
                    wid = str(w.get("id", ""))
                    if wid:
                        matches.append(wid)
    return matches[0] if len(matches) == 1 else ""


def _resolve_live_worker_window(record: dict) -> str:
    persisted = state.window_id_of(record)
    data = _terminal_ls()
    if not data:
        return persisted or ""
    live_ids = {
        str(w.get("id", ""))
        for osw in data for tab in osw.get("tabs", []) for w in tab.get("windows", [])
    }
    if persisted and persisted in live_ids:
        return persisted
    matched = _match_worker_window_by_cwd_runtime(data, record)
    if matched:
        record["window_id"] = matched
        sid = record.get("claude_sid")
        if sid:
            state.write_json_atomic(paths.ACTIVE / f"{sid}.json", record)
    return matched


def deliver_to_pane(worker: str, text: str, marker: str, policy) -> dict:
    record = _find_worker_by_name_or_sid(worker)
    if record.get("nested"):
        raise ValueError(
            f"'{worker}' is a nested sub-session of "
            f"{record.get('nested_parent_name') or 'another session'} — it cannot "
            "receive manager messages; message the parent worker instead"
        )
    window_id = ""
    for attempt in range(_WINDOW_RESOLVE_RETRIES):
        window_id = _resolve_live_worker_window(record)
        if window_id:
            break
        if attempt < _WINDOW_RESOLVE_RETRIES - 1:
            time.sleep(_WINDOW_RESOLVE_RETRY_SLEEP)
            record = _find_worker_by_name_or_sid(worker)
    if not window_id:
        raise ValueError(
            f"'{record['name']}' has no live window (worker dead/closed?) — "
            "resume_worker or re-spawn; message NOT delivered"
        )
    refused = policy(record, window_id)
    if refused is not None:
        return refused
    confirmed = _type_unguarded(window_id, marker + text)
    record["tasked_at"] = time.time()
    sid = record.get("claude_sid")
    if sid:
        if confirmed:
            _stamp_pane_send(sid, root=paths.PANE_SENDS)
        state.write_json_atomic(paths.ACTIVE / f"{sid}.json", record)
    result = {"status": "delivered", "worker": record["name"], "confirmed": confirmed}
    pending = _oldest_pending_question(sid) if sid else None
    if pending is not None:
        result["blocked_on"] = pending.get("question_id")
    return result


def send_manager_to_worker_impl(worker: str, text: str, marker: str = MANAGER_MARKER) -> dict:
    return deliver_to_pane(worker, text, marker, manager_policy)


def _headless_provider(record):
    from .providers import get_provider
    name = state.provider_of(record)
    if name is None:
        return None
    try:
        provider = get_provider(name)
    except ValueError:
        return None
    return provider if provider.headless else None


async def send_to_child_impl(name: str, text: str, *,
                             _registration_timeout_sec: float = 10.0,
                             _poll_interval: float = 0.5) -> dict:
    record = None
    try:
        record = _find_worker_by_name_or_sid(name)
    except ValueError as err:
        if not str(err).startswith("no worker named"):
            raise
    if record is not None:
        provider = _headless_provider(record)
        if provider is not None:
            assignment = state.read_json(paths.assignment_path(record.get("claude_sid") or "")) or {}
            if _report_has_content(Path(assignment.get("report_path") or "")):
                raise ValueError(
                    f"'{record['name']}' has written its round-{assignment.get('round')} report and "
                    "is shutting down; retry send_to_child in a few seconds"
                )
            raise ValueError(
                f"'{record['name']}' is a headless {provider.name} child mid-round; wait_for_child "
                "first — send_to_child opens the next round only after the current one ends"
            )
        return send_manager_to_worker_impl(name, text)
    try:
        closed_path, closed = _find_closed_record_by_name(name)
    except ValueError:
        if not any(r.get("name") == name and _headless_provider(r) is not None
                   for r in state.list_json_in(paths.CLOSED)):
            raise ValueError(f"no worker named '{name}'") from None
        raise
    provider = _headless_provider(closed)
    if provider is None:
        raise ValueError(f"no worker named '{name}'")
    _refuse_orphaned_child(closed)
    return await _open_child_round(closed_path, closed, provider, text,
                                   _registration_timeout_sec=_registration_timeout_sec,
                                   _poll_interval=_poll_interval)


async def _open_child_round(closed_path, record: dict, provider, text: str, *,
                            _registration_timeout_sec: float = 10.0,
                            _poll_interval: float = 0.5) -> dict:
    from .spawner import spawn_worker_tab
    from .providers import ChildRound
    name = record.get("name") or ""
    sid = record.get("claude_sid")
    if not sid:
        raise ValueError(f"closed child '{name}' has no claude_sid; cannot open a round")
    if name in _RESUMES_IN_FLIGHT:
        raise ValueError(f"resume of '{name}' is already in progress")
    assignment_path = paths.assignment_path(sid)
    previous = state.read_json(assignment_path)
    if not previous:
        raise ValueError(
            f"'{name}' has no assignment record (assignments/{sid}.json); the round cannot "
            "rebuild the spawn — respawn the child"
        )
    _RESUMES_IN_FLIGHT.add(name)
    try:
        assignment = dict(previous)
        rounds_dir = Path(assignment.get("rounds_dir")
                          or paths.child_rounds_dir(assignment.get("assignment_id") or sid))
        rounds_dir.mkdir(parents=True, exist_ok=True)
        round_no = int(assignment.get("round") or 1) + 1
        brief_path = rounds_dir / f"r{round_no}.brief.md"
        report_path = rounds_dir / f"r{round_no}.report.md"
        log_path = rounds_dir / f"r{round_no}.log"
        brief_path.write_text(text)
        assignment.update({"round": round_no, "round_started_at": time.time(),
                           "report_path": str(report_path), "log_path": str(log_path),
                           "rounds_dir": str(rounds_dir)})
        state.write_json_atomic(assignment_path, assignment)
        cwd = record.get("cwd") or os.getcwd()
        try:
            child_round = ChildRound(
                mode=assignment.get("mode") or "report", cwd=cwd, brief_path=brief_path,
                report_path=report_path, log_path=log_path, model=assignment.get("model"),
                effort=assignment.get("effort"), resume_sid=sid,
                extra_args=tuple(assignment.get("spawn_extra_args") or ()),
                context_role=assignment.get("child_context_role"),
            )
            result = await _spawn_and_confirm_resume(
                spawn_worker_tab, closed_path=closed_path, record=record, name=name, sid=sid, cwd=cwd,
                _registration_timeout_sec=_registration_timeout_sec, _poll_interval=_poll_interval,
                provider=provider, child_round=child_round,
                env_extra=assignment.get("spawn_env") or {},
                tab_title=f"{state.parent_name_of(record) or 'unscoped'}/{name} r{round_no}",
            )
        except BaseException:
            state.write_json_atomic(assignment_path, previous)
            brief_path.unlink(missing_ok=True)
            raise
        result.update({"round": round_no, "report_path": str(report_path)})
        return result
    finally:
        _RESUMES_IN_FLIGHT.discard(name)


async def send_manager_to_worker_auto_impl(
    worker: str,
    text: str,
    _registration_timeout_sec: float = 10.0,
    _poll_interval: float = 0.5,
    marker: str = MANAGER_MARKER,
) -> dict:
    try:
        return send_manager_to_worker_impl(worker, text, marker=marker)
    except ValueError as send_err:
        try:
            _closed_path, closed_record = _find_closed_record_by_name(worker)
        except ValueError as probe_err:
            raise ValueError(f"{send_err} (auto_resume: {probe_err})") from send_err
    replaced = await _replace_closed_holder(worker, closed_record, text)
    if replaced is not None:
        return replaced
    result = await resume_worker_impl(
        worker,
        _registration_timeout_sec=_registration_timeout_sec,
        _poll_interval=_poll_interval,
    )
    if not result.get("ok"):
        raise ValueError(
            f"auto_resume: {result.get('reason')}; message NOT delivered "
            "(closed record left intact — retry)"
        )
    name = result.get("name") or worker
    await _await_input_ready(
        result.get("window_id") or "", closed_record.get("runtime") or "claude"
    )
    try:
        out = send_manager_to_worker_impl(name, text, marker=marker)
    except ValueError as deliver_err:
        raise ValueError(
            f"worker '{name}' WAS resumed (sid={result.get('sid')}) but delivery "
            f"failed: {deliver_err}; retry send_manager_to_worker"
        ) from deliver_err
    out["resumed"] = True
    out["sid"] = result.get("sid")
    return out


async def _replace_closed_holder(worker: str, record: dict, text: str) -> dict | None:
    sid = record.get("claude_sid")
    try:
        assignment = state.read_json(paths.assignment_path(sid)) if sid else None
    except ValueError:
        assignment = None
    task_key = assignment.get("ticket") if isinstance(assignment, dict) else None
    if not task_key or _handed_off(sid, task_key):
        return None
    try:
        token = _held_token(_fold_for(task_key), assignment.get("assignment_id"))
    except (OSError, KeyError, ValueError, TypeError):
        return None
    tokens = _due_fresh_session(task_key, token, sid, record.get("runtime") or "claude")
    if tokens is None:
        return None
    from . import pipeline_reconcile
    outcome = await pipeline_reconcile.replace_for_message(task_key, token, text)
    if outcome["pending"]:
        return {"status": "replace_pending", "worker": None, "replaced": False, "reason": outcome["reason"],
                "note": "the engine holds an open restart of the node that carries your message and starts the "
                        "node with it; do not send it again"}
    if not outcome["ok"]:
        raise ValueError(
            f"auto_resume: '{worker}' holds {tokens} context tokens, at or over [pipeline] fresh_context_tokens, and "
            f"the engine did not restart its node: {outcome['reason']}; message NOT delivered"
        )
    return {"status": "replaced", "worker": outcome["name"], "replaced": True, "sid": outcome["sid"]}


def _resolve_sender_manager() -> dict | None:
    return identity.resolve_manager_record()


def _peer_marker(sender: dict | None) -> str:
    if not sender or not sender.get("name"):
        return MANAGER_MARKER
    return (f"{MANAGER_MARKER_OPEN} {sender['name']} · "
            f"{sender.get('domain') or DEFAULT_DOMAIN}] ")


def send_manager_to_manager_impl(name: str, text: str) -> dict:
    peer = None
    for record in state.list_json_in(paths.ACTIVE):
        if record.get("agent") == "manager" and record.get("name") == name:
            peer = record
            break
    if peer is None:
        raise ValueError(f"no manager named '{name}'")
    sid = peer["claude_sid"]
    window_id = state.window_id_of(peer)
    if not window_id:
        window_id = _resolve_manager_window(sid, peer.get("name") or name)
        if window_id:
            peer["window_id"] = window_id
            state.write_json_atomic(paths.ACTIVE / f"{sid}.json", peer)
    if not window_id:
        raise ValueError(
            f"manager '{name}' has no live window (dead/closed?) — message NOT delivered"
        )
    screen = _capture_text(window_id)
    if screen is None:
        raise ValueError(
            f"manager '{name}' window {window_id} is unreadable — message NOT delivered"
        )
    if _input_is_idle(screen):
        try:
            sender = _resolve_sender_manager()
        except Exception as resolve_err:
            print(f"send_manager_to_manager: sender resolution failed "
                  f"({resolve_err}); stamping the unnamed manager marker",
                  file=sys.stderr)
            sender = None
        _type_unguarded(window_id, _peer_marker(sender) + text)
        return {"status": "delivered_live", "manager": name,
                "sender": (sender or {}).get("name") or None}
    return {"status": "peer_busy", "delivered": False, "manager": name}

def _pending_assignment_by_name(name: str) -> tuple[str, dict] | None:
    if not paths.ASSIGNMENTS_PENDING.is_dir():
        return None
    newest = None
    for p in paths.ASSIGNMENTS_PENDING.glob("*.json"):
        record = state.read_json(p)
        if not isinstance(record, dict) or record.get("name") != name:
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, p.stem, record)
    return None if newest is None else (newest[1], newest[2])


def _kill_pending_child(worker: str, assignment_id: str, dry_run: bool) -> dict:
    window_path = paths.pending_window_path(assignment_id)
    try:
        window_id = window_path.read_text().strip() if window_path.is_file() else ""
    except OSError:
        window_id = ""
    pane_state = get_driver().pane_state(window_id) if window_id else "unknown"
    if pane_state not in ("dead", "gone"):
        return {"closed_window": None, "already_dead": False, "pending_assignment": assignment_id,
                "note": f"'{worker}' never registered and its pane is {pane_state} — not closed; "
                        "it may still register"}
    if not dry_run:
        if pane_state == "dead":
            _close_window(window_id)
        paths.pending_assignment_path(assignment_id).unlink(missing_ok=True)
        window_path.unlink(missing_ok=True)
    return {"closed_window": window_id if pane_state == "dead" else None, "already_dead": True,
            "pending_assignment": assignment_id}


def kill_worker_impl(worker: str, dry_run: bool = False) -> dict:
    try:
        record = _find_worker_by_name_or_sid(worker)
    except ValueError as err:
        if not str(err).startswith("no worker named"):
            raise
        candidates = [r for r in state.list_json_in(paths.CLOSED) if r.get("name") == worker
                      and _headless_provider(r) is not None and state.window_id_of(r)]
        if not candidates:
            pending = _pending_assignment_by_name(worker)
            if pending is None:
                raise
            return _kill_pending_child(worker, pending[0], dry_run)
        window_id = state.window_id_of(max(candidates, key=lambda r: r.get("closed_at") or 0))
        pane_state = get_driver().pane_state(window_id)
        if pane_state == "gone":
            return {"closed_window": None, "already_dead": True,
                    "note": "the recorded window no longer exists — nothing to close"}
        if pane_state == "unknown":
            return {"closed_window": None, "already_dead": True,
                    "note": "the pane state could not be read — not closed"}
        if pane_state != "dead":
            return {"closed_window": None, "already_dead": True,
                    "note": "the recorded window is live and not this child's — not closed"}
        if not dry_run:
            _close_window(window_id)
        return {"closed_window": window_id, "already_dead": True}
    if record.get("nested"):
        raise ValueError(
            f"'{worker}' is a nested sub-session of "
            f"{record.get('nested_parent_name') or 'another session'} — it has no own "
            "tab to close; the parent process manages its lifecycle"
        )
    pid = record["pid"]
    iterm_sid = state.window_id_of(record)
    sid = record["claude_sid"]
    if dry_run:
        return {"would_kill": pid, "iterm_sid": iterm_sid}
    dropped = _drop_questions_for_worker(sid)
    if not _pid_alive(pid):
        return {"killed_pid": pid, "iterm_sid": iterm_sid, "already_dead": True, "dropped_questions": dropped}
    _close_window(iterm_sid)
    return {"killed_pid": pid, "iterm_sid": iterm_sid, "dropped_questions": dropped}

def _latest_turn_end(sid: str, record: dict) -> float:
    latest = _parse_iso(record.get("last_turn_at")) or 0.0
    if not paths.TURN_ENDS.is_dir():
        return latest
    for entry in paths.TURN_ENDS.rglob(f"{sid}-*.json"):
        payload = state.read_json(entry)
        if not isinstance(payload, dict) or monitor._turn_end_sid(payload, entry) != sid:
            continue
        latest = max(latest, monitor._turn_end_ts(payload, entry))
    return latest

def _current_done_event(sid: str, record: dict) -> tuple[dict, float] | None:
    if not paths.DONE.is_dir():
        return None
    episode_start = monitor._episode_start(record)
    lookback_floor = _latest_turn_end(sid, record) - monitor.DONE_FRESH_LOOKBACK_SEC
    newest: tuple[dict, float] | None = None
    for entry in paths.DONE.rglob(f"{sid}-*.json"):
        payload = state.read_json(entry)
        if not isinstance(payload, dict) or payload.get("claude_sid") != sid:
            continue
        done_ts = monitor._done_time(entry, payload)
        if done_ts is None or monitor._predates_current_episode(done_ts, episode_start):
            continue
        if done_ts < lookback_floor:
            continue
        if newest is None or done_ts > newest[1]:
            newest = (payload, done_ts)
    return newest

def get_worker_summary_impl(worker: str) -> dict:
    record = _find_worker_by_name_or_sid(worker)
    sid = record["claude_sid"]
    alive = _pid_reads_alive(record.get("pid"))
    done = _current_done_event(sid, record)
    if done is not None:
        payload, done_ts = done
        return {
            "name": record["name"],
            "summary": payload.get("summary"),
            "source": "worker_done",
            "event_id": payload.get("event_id"),
            "completed_at": done_ts,
            "alive": alive,
        }
    log = find_session_log(sid, runtime=record.get("runtime") or "claude")
    if log is None:
        return {
            "name": record["name"],
            "summary": None,
            "source": "transcript",
            "last_turn_at": None,
            "alive": alive,
            "error": "transcript not found",
        }
    summary, ts = last_assistant_summary(log, max_chars=sys.maxsize)
    return {
        "name": record["name"],
        "summary": summary,
        "source": "transcript",
        "last_turn_at": ts,
        "alive": alive,
    }

def _content_preview(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
                continue
            nested = item.get("content")
            if isinstance(nested, str):
                parts.append(nested)
        if parts:
            return "\n".join(parts)
    return json.dumps(content)

def _tail_event_role_and_content(event: dict) -> tuple[str | None, Any]:
    role = event.get("type")
    message = event.get("message")
    if isinstance(message, dict):
        return role, message.get("content")

    payload = event.get("payload")
    if isinstance(payload, dict):
        payload_type = payload.get("type")
        if payload_type == "message":
            return payload.get("role") or role, payload.get("content")
        if isinstance(payload.get("message"), str):
            return payload_type or role, payload.get("message")

    return role, event.get("content")

def get_worker_tail_impl(worker: str, lines: int = 50) -> dict:
    record = _find_worker_by_name_or_sid(worker)
    sid = record["claude_sid"]
    log = find_session_log(sid, runtime=record.get("runtime") or "claude")
    if log is None:
        return {"name": record["name"], "error": "transcript not found"}
    raw_lines = [l for l in log.read_text().splitlines() if l.strip()]
    tail = raw_lines[-lines:]
    entries = []
    for line in tail:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        role, content = _tail_event_role_and_content(event)
        content_str = _content_preview(content)
        if len(content_str) > 200:
            content_str = content_str[:199] + "…"
        entries.append({"role": role, "content_preview": content_str})
    return {
        "name": record["name"],
        "log_path": str(log),
        "lines_returned": len(entries),
        "entries": entries,
    }

def _published_count(claude_sid: str):
    try:
        assignment = state.read_json(paths.assignment_path(claude_sid))
        task_key = (assignment or {}).get("ticket")
        if not task_key:
            return None
        own = sum(1 for a in artifact_list_impl(task_key)
                  if a.get("writer_sid") == claude_sid)
        return task_key, own
    except Exception:
        return None

def _is_stamp(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)

def worker_done_impl(claude_sid: str, summary: str) -> dict:
    record = state.read_json(paths.ACTIVE / f"{claude_sid}.json")
    self_healed = False
    if record is None:
        assignment = state.read_json(paths.assignment_path(claude_sid))
        if (not isinstance(assignment, dict)
                or assignment.get("claude_sid") != claude_sid):
            raise ValueError(f"session {claude_sid} not registered; cannot signal done")
        record = {
            "name": assignment.get("name"),
            "parent_manager_name": state.parent_name_of(assignment),
            "parent_name": state.parent_name_of(assignment),
        }
        self_healed = True
    if record.get("nested"):
        return {
            "ok": False,
            "nested": True,
            "message": (
                f"session {claude_sid} is a nested sub-session of "
                f"{record.get('nested_parent_name') or 'another session'} — done event "
                "suppressed; the parent session supervises its own subprocesses"
            ),
        }
    paths.ensure_dirs()
    event_id = uuid.uuid4().hex
    done_dir = paths.done_dir_for(state.parent_name_of(record))
    done_dir.mkdir(parents=True, exist_ok=True)
    done_event = {
        "event_id": event_id,
        "claude_sid": claude_sid,
        "worker_name": record.get("name"),
        "parent_manager_name": state.parent_name_of(record),
        "parent_name": state.parent_name_of(record),
        "summary": summary,
        "spend": _spend_totals(record.get("spend")),
        "completed_at": time.time(),
    }
    published = _published_count(claude_sid)
    if published is not None:
        done_event["ticket"], done_event["artifacts_published"] = published
    if self_healed:
        done_event["self_healed"] = True
    typed_at = record.get("human_attached_at")
    if _is_stamp(typed_at) and typed_at == record.get("input_at"):
        done_event["engineer_input_at"] = typed_at
    if _is_stamp(record.get("tasked_at")):
        done_event["tasked_at"] = record["tasked_at"]
    state.write_json_atomic(done_dir / f"{claude_sid}-{event_id}.json", done_event)
    from . import daemon
    daemon.wake()
    result = {"ok": True, "event_id": event_id}
    if self_healed:
        result["self_healed"] = True
    return result

_RETASK_GRACE_SEC = 2.0
_REPORT_GRACE_SEC = 5.0


def _report_has_content(report: Path) -> bool:
    try:
        return report.is_file() and report.stat().st_size > 0
    except OSError:
        return False


def _record_child_round_spend(sid, assignment: dict) -> None:
    log_path = assignment.get("log_path")
    if not sid or not isinstance(log_path, str) or not log_path:
        return
    try:
        from .transcript import recount_codex_spend
        for record_path in (paths.ACTIVE / f"{sid}.json", paths.CLOSED / f"{sid}.json"):
            record = state.read_json(record_path)
            if record is None:
                continue
            if (record.get("runtime") or "claude") != "codex":
                return
            spend = recount_codex_spend(Path(log_path), record.get("spend"))
            if spend is not None and spend != record.get("spend"):
                if not record_path.exists():
                    continue
                record["spend"] = spend
                state.write_json_atomic(record_path, record)
            return
    except Exception:
        return

async def wait_for_child_impl(
    name_or_sid: str,
    timeout_sec: int = 1500,
    _poll_interval: float = 1.0,
    manager_name: str | None = None,
    tool: str = "wait_for_child",
    surface_questions: bool = True,
) -> dict:
    paths.ensure_dirs()
    name = name_or_sid

    def _is_target(record: dict) -> bool:
        return record.get("name") == name or record.get("claude_sid") == name

    sid = None
    found_via_active = False
    manager_holder = None
    active_record = None
    resolved_name = name_or_sid
    for record in state.list_json_in(paths.ACTIVE):
        if record.get("agent") != "worker":
            if _is_target(record):
                manager_holder = record
            continue
        if _is_target(record) and _matches_manager(record, manager_name):
            sid = record.get("claude_sid")
            found_via_active = True
            active_record = record
            resolved_name = record.get("name") or name_or_sid
            break
    if sid is None:
        for record in state.list_json_in(paths.CLOSED):
            if _is_target(record) and _matches_manager(record, manager_name):
                sid = record.get("claude_sid")
                resolved_name = record.get("name") or name_or_sid
                break

    min_completed_at = 0.0
    if active_record is not None:
        candidates = [active_record.get("tasked_at") or 0]
        if active_record.get("state") == "processing":
            candidates.append(active_record.get("processing_since") or 0)
        bound = max(candidates)
        if bound:
            min_completed_at = bound - _RETASK_GRACE_SEC

    def _assignment() -> dict:
        return (state.read_json(paths.assignment_path(sid)) or {}) if sid else {}

    def _round_floor() -> float:
        started = _assignment().get("round_started_at")
        if isinstance(started, (int, float)) and not isinstance(started, bool) and started:
            return max(min_completed_at, started)
        return min_completed_at

    def _report_done() -> dict | None:
        assignment = _assignment()
        report_path = assignment.get("report_path")
        if not report_path:
            return None
        report = Path(report_path)
        if not _report_has_content(report):
            return None
        try:
            text = report.read_text()
            mtime = report.stat().st_mtime
        except OSError:
            return None
        _record_child_round_spend(sid, assignment)
        return {"found": "done", "name": resolved_name, "sid": sid, "summary": text,
                "report_path": report_path, "round": assignment.get("round"),
                "event_id": None, "completed_at": mtime}

    async def _report_or_exited() -> dict:
        has_report_path = bool(_assignment().get("report_path"))
        deadline_grace = time.monotonic() + (_REPORT_GRACE_SEC if has_report_path else 0.0)
        while True:
            done = _report_done()
            if done is not None:
                return done
            if time.monotonic() >= deadline_grace:
                break
            await asyncio.sleep(0.5)
        return {"found": "exited", "name": resolved_name, "sid": sid,
                "reason": "session_ended_without_report" if has_report_path
                else "session_ended_without_worker_done"}

    def _latest_done_event() -> dict | None:
        if not paths.DONE.is_dir():
            return None
        matching = []
        for p in paths.DONE.rglob("*.json"):
            event = state.read_json(p)
            if event is None:
                continue
            if not _matches_manager(event, manager_name):
                continue
            if (event.get("completed_at") or 0) < _round_floor():
                continue
            if sid is not None and event.get("claude_sid") == sid:
                matching.append(event)
            elif sid is None and (event.get("worker_name") == name or event.get("claude_sid") == name):
                matching.append(event)
        if not matching:
            return None
        matching.sort(key=lambda e: e.get("completed_at", 0), reverse=True)
        return matching[0]

    def _done_response(event: dict) -> dict:
        return {
            "found": "done",
            "name": resolved_name,
            "sid": event.get("claude_sid"),
            "summary": event.get("summary"),
            "event_id": event.get("event_id"),
            "completed_at": event.get("completed_at"),
        }

    def _question_response(question: dict) -> dict:
        return {
            "found": "question",
            "name": resolved_name,
            "sid": sid,
            "question_id": question.get("question_id"),
            "question": question.get("question"),
            "asked_at": question.get("asked_at"),
            "hint": ("answer with answer_child(question_id, text), then call "
                     f"{tool} again to keep waiting; if you cannot answer it yourself, "
                     "relay it upward first (ask_parent for a worker, the human for a "
                     f"manager) — calling {tool} again without answering returns this "
                     "same question at once"),
        }

    def _pending_question() -> dict | None:
        if not (surface_questions and found_via_active):
            return None
        return _oldest_pending_question(sid, manager_name)

    if sid is not None:
        early = _report_done()
        if early is not None:
            return early

    initial_done = _latest_done_event()
    if initial_done is not None:
        return _done_response(initial_done)

    if sid is None:
        if manager_holder is not None:
            raise ValueError(
                f"'{name}' is held by an active "
                f"{manager_holder.get('agent') or 'session'}, not a worker — "
                f"{tool} only waits on workers"
            )
        raise ValueError(f"no worker named '{name}'")

    pending = _pending_question()
    if pending is not None and (paths.ACTIVE / f"{sid}.json").exists():
        return _question_response(pending)

    if not found_via_active:
        return await _report_or_exited()

    deadline = time.monotonic() + timeout_sec
    while True:
        done = _report_done()
        if done is not None:
            return done
        done_event = _latest_done_event()
        if done_event is not None:
            return _done_response(done_event)
        pending = _pending_question()
        if pending is not None and (paths.ACTIVE / f"{sid}.json").exists():
            return _question_response(pending)
        if not (paths.ACTIVE / f"{sid}.json").exists():
            return await _report_or_exited()
        if time.monotonic() >= deadline:
            return {
                "found": "timeout",
                "name": resolved_name,
                "sid": sid,
                "timeout_sec": timeout_sec,
                "hint": (f"'{resolved_name}' did not complete within {timeout_sec}s and is still "
                         f"registered; call {tool} again to keep waiting"),
            }
        await asyncio.sleep(_poll_interval)


async def wait_for_worker_impl(
    name: str,
    timeout_sec: int = 3600,
    _poll_interval: float = 1.0,
    manager_name: str | None = None,
) -> dict:
    result = await wait_for_child_impl(
        name, timeout_sec, _poll_interval=_poll_interval,
        manager_name=manager_name, tool="wait_for_worker",
        surface_questions=False,
    )
    if result.get("found") == "timeout":
        raise TimeoutError(f"worker '{name}' did not complete within {timeout_sec}s")
    return result

def attach_existing_impl(manager_name: str | None = None) -> dict:
    return {
        "workers": list_workers_impl(manager_name=manager_name),
        "orphan_questions": list_pending_questions_impl(manager_name=manager_name, include_engineer_asks=True),
    }

def list_closed_workers_impl(manager_name: str | None = None, limit: int | None = None) -> list[dict]:
    if limit is not None and limit <= 0:
        raise ValueError("limit must be a positive integer")
    _prune_stale_assignments()
    if not paths.CLOSED.is_dir():
        return []
    records = []
    for p in paths.CLOSED.iterdir():
        if p.suffix != ".json":
            continue
        record = state.read_json(p)
        if record is None:
            continue
        if not _matches_manager(record, manager_name):
            continue
        records.append({
            "name": record.get("name"),
            "claude_sid": record.get("claude_sid"),
            "cwd": record.get("cwd"),
            "runtime": record.get("runtime") or "claude",
            "last_summary": record.get("last_summary"),
            "last_turn_at": record.get("last_turn_at"),
            "closed_at": record.get("closed_at"),
            "closed_reason": record.get("closed_reason"),
            "moved": record.get("moved"),
            "parent_manager_name": state.parent_name_of(record),
            "parent_name": state.parent_name_of(record),
            "parent_sid": record.get("parent_sid"),
            "kind": record.get("kind") or record.get("agent"),
            "depth": record.get("depth"),
            "brief": _assignment_brief_for_sid(record.get("claude_sid")),
        })
    records.sort(key=lambda r: r.get("closed_at") or 0, reverse=True)
    if limit is not None:
        records = records[:limit]
    return records

def _has_live_transcript(sid: str | None, runtime: str | None = None) -> bool:
    if not sid:
        return False
    log_path = find_session_log(sid, runtime=runtime or "claude")
    if log_path is None:
        return False
    try:
        return log_path.stat().st_size > 0
    except OSError:
        return False


def _find_closed_record_by_name(name: str) -> tuple:
    matches: list[tuple] = []
    if paths.CLOSED.is_dir():
        for p in paths.CLOSED.iterdir():
            if p.suffix != ".json":
                continue
            record = state.read_json(p)
            if record is None:
                continue
            if record.get("name") == name:
                matches.append((p, record))
    if not matches:
        raise ValueError(f"no closed worker named '{name}'")
    live = [
        m
        for m in matches
        if _has_live_transcript(m[1].get("claude_sid"), runtime=m[1].get("runtime") or "claude")
    ]
    if live:
        return max(live, key=lambda m: m[1].get("closed_at") or 0)
    sids_tried = [m[1].get("claude_sid") for m in matches]
    raise ValueError(
        f"closed worker '{name}' has {len(matches)} record(s) but none have a live "
        f"transcript to resume; sids tried: {sids_tried}"
    )

def _active_display_names() -> set[str]:
    names_set: set[str] = set()
    for record in state.list_json_in(paths.ACTIVE):
        for key in ("name", "funny_name"):
            if record.get(key):
                names_set.add(record[key])
    return names_set


def _paint_manager_tab(name: str, domain: str) -> None:
    from .hooks import _style_manager_tab
    _style_manager_tab(name, domain)


def _run_preflight_cleanup() -> str:
    script = Path.home() / ".claude" / "scripts" / "preflight_cleanup.py"
    if not script.is_file():
        return ""
    proc = subprocess.run([sys.executable, str(script)],
                          capture_output=True, text=True, timeout=10)
    return (proc.stdout or "").strip()


def _restamp_parent_sid(parent_name: str, parent_sid: str) -> int:
    count = 0
    for p in paths.ACTIVE.glob("*.json"):
        record = state.read_json(p)
        if record is None:
            continue
        if record.get("agent") != "worker" or record.get("nested"):
            continue
        if state.parent_name_of(record) != parent_name:
            continue
        sid = record.get("claude_sid")
        if not sid or record.get("parent_sid") == parent_sid:
            continue
        record["parent_sid"] = parent_sid
        state.write_json_atomic(p, record)
        count += 1
    return count


def _adopt_orphan_outbox(name: str, domain: str) -> int:
    root = paths.ROOT / "notify-outbox"
    own = paths.notify_outbox_dir_for(name)
    if not root.is_dir():
        return 0
    live = {paths._event_bucket(r.get("name")) for r in state.list_json_in(paths.ACTIVE)
            if isinstance(r, dict) and r.get("agent") == "manager" and not r.get("nested")
            and _session_alive(r.get("claude_sid"))}
    moved = 0
    for bucket in root.iterdir():
        if not bucket.is_dir() or bucket == own or bucket.name in live:
            continue
        for entry in sorted(bucket.glob("*.json")):
            payload = state.read_json(entry)
            if not isinstance(payload, dict) or payload.get("domain") != domain:
                continue
            own.mkdir(parents=True, exist_ok=True)
            try:
                entry.rename(own / entry.name)
            except OSError:
                continue
            moved += 1
    return moved


def become_manager_impl(
    claude_sid: str,
    iterm_sid: str = "",
    domain: str | None = None,
    name: str | None = None,
) -> dict:
    paths.ensure_dirs()
    os.environ.pop("DOCKWRIGHT_PENDING_TAKEOVER", None)
    _migrate_flat_manager_memory()
    _prune_stale_active_records()
    cwd = os.getcwd()
    paths.refuse_manager_cwd(cwd)
    parent_pid = os.getppid()
    pid = resolve_cli_pid(parent_pid) or parent_pid
    if not iterm_sid:
        iterm_sid = (get_driver().current_pane_id() or "")
    _prune_same_pid_ghosts(pid, keep_sid=claude_sid, keep_window_id=iterm_sid)
    domain = domain or DEFAULT_DOMAIN
    existing = _active_display_names()
    own = state.read_json(paths.ACTIVE / f"{claude_sid}.json")
    if own is not None:
        existing.discard(own.get("name"))
        existing.discard(own.get("funny_name"))
    if name is None:
        name = names.roll_manager_name(is_taken=lambda n: n in existing)
    else:
        if name in existing:
            name = _resolve_unique_name(name, excluding_sid=claude_sid)
    register_self_impl(
        claude_sid=claude_sid,
        agent="manager",
        name=name,
        cwd=cwd,
        iterm_sid=iterm_sid,
        pid=pid,
        domain=domain,
        runtime="claude",
    )
    _backfill_legacy_workers()
    _restamp_parent_sid(name, claude_sid)
    _adopt_orphan_outbox(name, domain)
    try:
        _paint_manager_tab(name, domain)
    except Exception:
        pass
    try:
        preflight = _run_preflight_cleanup()
    except Exception:
        preflight = ""
    return {"ok": True, "name": name, "domain": domain, "runtime": "claude",
            "preflight": preflight}


def _find_manager_record() -> dict | None:
    for record in state.list_json_in(paths.ACTIVE):
        if record.get("agent") == "manager" and not record.get("nested"):
            return record
    return None


def prepare_handoff_impl(claude_sid: str, narrative_summary: str, trigger_reason: str) -> dict:
    paths.ensure_dirs()
    manager_record = state.read_json(paths.ACTIVE / f"{claude_sid}.json")
    if manager_record is None or manager_record.get("agent") != "manager":
        raise ValueError(f"session {claude_sid} is not the current manager; cannot prepare handoff")
    handoff_id = uuid.uuid4().hex
    manager_name = manager_record.get("name")
    domain = manager_record.get("domain") or DEFAULT_DOMAIN
    refusal = notebook_mark.refusal(claude_sid, domain)
    if refusal is not None:
        raise ValueError(refusal)
    workers_snapshot = list_workers_impl(manager_name=manager_name)
    questions_snapshot = list_pending_questions_impl(manager_name=manager_name, include_engineer_asks=True)
    record = {
        "handoff_id": handoff_id,
        "from_sid": claude_sid,
        "to_sid": None,
        "prepared_at": time.time(),
        "consumed_at": None,
        "trigger_reason": trigger_reason,
        "narrative_summary": narrative_summary,
        "manager_name": manager_name,
        "domain": domain,
        "workers_snapshot": workers_snapshot,
        "questions_snapshot": questions_snapshot,
    }
    handoff_path = paths.HANDOFFS / f"{handoff_id}.json"
    state.write_json_atomic(handoff_path, record)

    distill_path = distill_and_write_memory(claude_sid, domain=domain)

    return {"handoff_id": handoff_id, "path": str(handoff_path), "distill_path": distill_path}

RECOVERY_TAKEOVER_MIN_SILENCE_SEC = 120

NEVER_TOOK_A_TURN_GRACE_SEC = 600


def _recovery_target_liveness(from_sid: str, record: dict) -> str | None:
    pid = record.get("pid")
    if not (isinstance(pid, int) and _pid_alive(pid)):
        return None
    log = None
    try:
        last_activity = (paths.ACTIVE / f"{from_sid}.json").stat().st_mtime
        try:
            log = find_session_log(from_sid, runtime=record.get("runtime") or "claude")
            if log is not None:
                last_activity = max(last_activity, log.stat().st_mtime)
        except OSError:
            pass
        silence = time.time() - last_activity
    except OSError:
        silence = None
    if (record.get("state") != "processing"
            or (silence is not None and silence < RECOVERY_TAKEOVER_MIN_SILENCE_SEC)):
        return (f"its process is alive and its record shows recent activity "
                f"(state={record.get('state')!r}, last activity "
                f"{'unknown' if silence is None else f'{silence:.0f}s'} ago)")
    if log is not None and last_assistant_ends_in_tool_use(log):
        return ("its transcript's last assistant event ends in an unfinished "
                "tool_use — the CLI is waiting on a tool or modal result "
                "(mid-turn, alive), not latched on a brick banner")
    return None


def prepare_recovery_handoff_impl(from_sid: str, trigger_reason: str = "account-flip-recovery") -> dict:
    paths.ensure_dirs()
    manager_record = state.read_json(paths.ACTIVE / f"{from_sid}.json")
    if manager_record is None or manager_record.get("agent") != "manager":
        raise ValueError(
            f"session {from_sid} is not an active manager; cannot synthesize recovery handoff")
    liveness = _recovery_target_liveness(from_sid, manager_record)
    if liveness is not None:
        raise ValueError(
            f"session {from_sid} is not an active manager takeover target: {liveness}; "
            "refusing to synthesize a recovery handoff against a live manager — if a live "
            "manager owns the fleet, stand down")
    handoff_id = uuid.uuid4().hex
    manager_name = manager_record.get("name")
    domain = manager_record.get("domain") or DEFAULT_DOMAIN
    notebook_mark.write_unless_fresh(from_sid, domain, reason=f"recovery handoff ({trigger_reason})")
    record = {
        "handoff_id": handoff_id,
        "from_sid": from_sid,
        "to_sid": None,
        "prepared_at": time.time(),
        "consumed_at": None,
        "trigger_reason": trigger_reason,
        "narrative_summary": (
            f"[auto-recovery] predecessor {from_sid[:8]} bricked on a session limit; "
            "account pointer flipped by stale_monitor. Real narrative pending — the "
            "takeover subagent appends it after reading the predecessor transcript. "
            "Reconstruct interim state from workers_snapshot + the domain notebook."
        ),
        "manager_name": manager_name,
        "domain": domain,
        "workers_snapshot": list_workers_impl(manager_name=manager_name),
        "questions_snapshot": list_pending_questions_impl(manager_name=manager_name, include_engineer_asks=True),
        "recovery": True,
    }
    handoff_path = paths.HANDOFFS / f"{handoff_id}.json"
    state.write_json_atomic(handoff_path, record)
    return {"handoff_id": handoff_id, "path": str(handoff_path)}


def _append_trigger_log(entry: dict) -> None:
    paths.MANAGER_TRIGGERS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with paths.MANAGER_TRIGGERS_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")

def become_manager_with_takeover_impl(claude_sid: str, takeover_from: str, handoff_id: str, iterm_sid: str = "") -> dict:
    paths.refuse_manager_cwd(os.getcwd())
    paths.ensure_dirs()
    handoff_path = paths.HANDOFFS / f"{handoff_id}.json"
    handoff = state.read_json(handoff_path)
    if handoff is None:
        raise ValueError(f"no handoff with id {handoff_id}")
    if handoff.get("from_sid") != takeover_from:
        raise ValueError(
            f"handoff {handoff_id} was prepared by {handoff.get('from_sid')}, not {takeover_from}"
        )
    if handoff.get("consumed_at") is not None:
        raise ValueError(f"handoff {handoff_id} already consumed at {handoff.get('consumed_at')}")
    old_record = state.read_json(paths.ACTIVE / f"{takeover_from}.json")
    old_pid = old_record.get("pid") if old_record else None
    old_iterm_sid = state.window_id_of(old_record or {})
    if handoff.get("recovery") and old_record is not None:
        liveness = _recovery_target_liveness(takeover_from, old_record)
        if liveness is not None:
            raise ValueError(
                f"refusing takeover: predecessor {takeover_from} looked bricked at "
                f"handoff synthesis but now shows liveness ({liveness}); not closing a "
                "live manager's window — stand down and re-verify")
    inherited_name = (old_record or {}).get("name") or handoff.get("manager_name")
    inherited_domain = (old_record or {}).get("domain") or handoff.get("domain")
    missing = [f for f, v in (("manager_name", inherited_name), ("domain", inherited_domain)) if not v]
    if missing:
        raise ValueError(
            f"handoff {handoff_id}: cannot establish predecessor identity — no usable active "
            f"record for {takeover_from} and the handoff omits {', '.join(missing)}. Refusing "
            "to roll a fresh identity/domain: workers' parent_manager_name routing and the "
            "domain memory pool depend on inheritance and there is no re-parenting mechanism. "
            "Re-create the handoff with a writer that stamps manager_name + domain "
            "(prepare_handoff / prepare_recovery_handoff / bootstrap-recreate.sh "
            "--manager-name <name> --domain <domain>)."
        )
    _prune_stale_active_records()
    for record in state.list_json_in(paths.ACTIVE):
        holder_sid = record.get("claude_sid")
        if holder_sid in (takeover_from, claude_sid):
            continue
        if inherited_name in (record.get("name"), record.get("funny_name")):
            raise ValueError(
                f"handoff {handoff_id}: the inherited name '{inherited_name}' is already held by "
                f"session {holder_sid} ({record.get('agent')}). Registering would silently roll a "
                f"suffixed name (e.g. '{inherited_name}-2') and strand every in-flight worker whose "
                "parent_manager_name still points at the un-suffixed name. If this is a duplicate "
                "recovery launch, the takeover already happened here — stand down. Otherwise resolve "
                "the name collision (kill or rename the other session) and retry the takeover."
            )
    closed_window_id = ""
    if isinstance(old_pid, int) and _pid_alive(old_pid):
        target_window = old_iterm_sid
        if not target_window:
            new_window = iterm_sid or (get_driver().current_pane_id() or "")
            target_window = _resolve_manager_window(takeover_from, inherited_name, exclude_id=new_window)
        if target_window:
            _close_window(target_window)
            closed_window_id = target_window
    paths.ACTIVE.joinpath(f"{takeover_from}.json").unlink(missing_ok=True)
    _drop_questions_for_worker(takeover_from)
    bm_result = become_manager_impl(
        claude_sid=claude_sid,
        iterm_sid=iterm_sid,
        domain=inherited_domain,
        name=inherited_name,
    )
    registered_name = bm_result.get("name")
    if registered_name != inherited_name:
        paths.ACTIVE.joinpath(f"{claude_sid}.json").unlink(missing_ok=True)
        reverted = 0
        for record in state.list_json_in(paths.ACTIVE):
            if record.get("agent") != "worker" or state.parent_name_of(record) != registered_name:
                continue
            worker_sid = record.get("claude_sid")
            if not worker_sid:
                continue
            record["parent_manager_name"] = None
            record["parent_name"] = None
            record["parent_sid"] = None
            state.write_json_atomic(paths.ACTIVE / f"{worker_sid}.json", record)
            reverted += 1
        revert_note = (
            f" Reverted {reverted} worker record(s) just stamped with that dead suffixed name back "
            "to unowned (parent_manager_name=None) so the next single-manager boot re-adopts them."
            if reverted else ""
        )
        raise ValueError(
            f"handoff {handoff_id}: takeover raced a concurrent registration — registered as "
            f"'{registered_name}' instead of the inherited '{inherited_name}'. Unregistered this "
            "session; workers still route to the inherited name. The predecessor was already "
            "closed and unlinked by THIS attempt, so a bare retry will hit the same collision — "
            f"resolve the name collision (kill or rename the session now holding '{inherited_name}') "
            f"before retrying the takeover.{revert_note}"
        )
    try:
        if closed_window_id:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pane_closed = not pool.submit(
                    asyncio.run, get_driver().pane_exists(closed_window_id)
                ).result(timeout=5)
        else:
            pane_closed = True
    except Exception:
        pane_closed = False
    now = time.time()
    handoff["consumed_at"] = now
    handoff["to_sid"] = claude_sid
    state.write_json_atomic(handoff_path, handoff)
    narrative = handoff.get("narrative_summary") or ""
    _append_trigger_log({
        "ts": now,
        "from_sid": takeover_from,
        "to_sid": claude_sid,
        "handoff_id": handoff_id,
        "trigger_reason": handoff.get("trigger_reason"),
        "narrative_excerpt": narrative[:200],
    })
    return {"ok": True, "name": registered_name, "domain": inherited_domain, "runtime": "claude",
            "preflight": bm_result.get("preflight", ""),
            "predecessor_pane_closed": pane_closed}


def _manager_name_from_sid(manager_sid: str | None) -> str | None:
    if not manager_sid:
        return None
    record = state.read_json(paths.ACTIVE / f"{manager_sid}.json")
    if record is None:
        return None
    return record.get("name")


_PARENT_RESOLVER_TEXT = {
    "spawn_worker": {
        "param": "manager_sid",
        "parents": ("manager",),
        "record_noun": "manager",
        "expects": "a live top-level manager",
        "nested_worker_label": "worker",
        "missing": ("the worker would register UNSCOPED (parent_manager_name=null) and its "
                    "done/question/turn-end events would never route back to you"),
        "missing_tail": " For a deliberately unscoped spawn pass manager_sid=None.",
        "wrong_kind": "the worker would be parented to a name no manager watches",
        "wrong_kind_advice": "Pass YOUR manager session UUID (list_managers() shows live sids)",
        "wrong_kind_tail": ", or manager_sid=None for a deliberately unscoped spawn",
        "unnamed": ("the worker would register UNSCOPED (parent_manager_name=null) with its "
                    "events routed to buckets no manager watches"),
        "unnamed_tail": ", or pass manager_sid=None for a deliberately unscoped spawn",
        "advice": ("Pass your own session UUID (the claude_sid from your boot context), not "
                   "the funny name; list_managers() shows live manager sids — if it shows "
                   "none, your own registration is gone: re-run become_manager."),
        "unnamed_advice": "Re-run become_manager to rewrite your registration",
    },
    "spawn_child": {
        "param": "parent_sid",
        "parents": ("manager", "worker"),
        "record_noun": "parent",
        "expects": "a live parent session (a top-level manager or a worker)",
        "nested_worker_label": "nested worker-agent",
        "missing": ("the child would register UNSCOPED (parent_name=null) and its "
                    "done/question/turn-end events would never route back to you"),
        "missing_tail": "",
        "wrong_kind": "the child would be parented to a name no parent watches",
        "wrong_kind_advice": ("A nested sub-session cannot spawn children; run spawn_child from "
                              "the top-level session that owns it, with THAT session's UUID"),
        "wrong_kind_tail": "",
        "unnamed": ("the child would register UNSCOPED (parent_name=null) with its "
                    "events routed to buckets no parent watches"),
        "unnamed_tail": "",
        "advice": ("Pass your OWN session UUID (the sid on your boot line) — a manager's or a "
                   "worker's; a name is not a sid"),
        "unnamed_advice": ("Re-register the parent — become_manager for a manager, the next "
                           "SessionStart for a worker — and retry"),
    },
    "wait_for_child": {
        "param": "parent_sid",
        "parents": ("manager", "worker"),
        "record_noun": "parent",
        "expects": "a live parent session (a top-level manager or a worker)",
        "nested_worker_label": "nested worker-agent",
        "missing": "the wait would cover the whole fleet instead of your own children",
        "missing_tail": "",
        "wrong_kind": "a nested sub-session owns no event buckets to wait on",
        "wrong_kind_advice": ("A nested sub-session cannot wait on children; wait from the "
                              "top-level session that owns it, with THAT session's UUID"),
        "wrong_kind_tail": "",
        "unnamed": "a parent with no name owns no event buckets to wait on",
        "unnamed_tail": "",
        "advice": ("Pass the live parent sid — your own session UUID for your own children (a "
                   "manager's or a worker's), the parent_sid on your own active record when you "
                   "are a worker waiting on a sibling; $CLAUDE_PARENT_SID is the spawn-time copy "
                   "and goes stale when the manager is recreated"),
        "unnamed_advice": ("Re-register the parent — become_manager for a manager, the next "
                           "SessionStart for a worker — and retry"),
    },
    "list_children": {
        "param": "parent_sid",
        "parents": ("manager", "worker"),
        "record_noun": "parent",
        "expects": "a live parent session (a top-level manager or a worker)",
        "nested_worker_label": "nested worker-agent",
        "missing": "the list would cover the whole fleet instead of your own children",
        "missing_tail": "",
        "wrong_kind": "a nested sub-session owns no children",
        "wrong_kind_advice": ("A nested sub-session has no children to list; list from the "
                              "top-level session that owns it, with THAT session's UUID"),
        "wrong_kind_tail": "",
        "unnamed": "a parent with no name owns no children to list",
        "unnamed_tail": "",
        "advice": ("Pass your OWN session UUID (the sid on your boot line); a worker lists its "
                   "own children with its own sid, its siblings only through its manager"),
        "unnamed_advice": ("Re-register the parent — become_manager for a manager, the next "
                           "SessionStart for a worker — and retry"),
    },
}
_PARENT_RESOLVER_TEXT["pipeline"] = {**_PARENT_RESOLVER_TEXT["spawn_worker"], "missing_tail": "", "wrong_kind_tail": "",
                                     "unnamed_tail": "", "parents": ("manager", "daemon")}
_PARENT_RESOLVER_TEXT["topic"] = {**_PARENT_RESOLVER_TEXT["spawn_worker"], "missing_tail": "", "wrong_kind_tail": "",
                                  "unnamed_tail": ""}


def _record_label(record: dict, tool_text: dict) -> str:
    agent = record.get("agent")
    if record.get("nested") and agent == "manager":
        return "nested manager-agent"
    if record.get("nested") and agent == "worker":
        return tool_text["nested_worker_label"]
    return record.get("kind") or agent or "session"


def _resolve_parent_record(manager_sid: str | None, tool: str = "spawn_worker") -> dict | None:
    if not manager_sid:
        return None
    t = _PARENT_RESOLVER_TEXT[tool]
    param = t["param"]
    record = _record_for_parent(manager_sid, tool)
    if record is None and manager_sid == DAEMON_SID and tool == "pipeline":
        raise ValueError(f"{tool}: {param} 'daemon' names the fleet daemon, and the daemon is not running "
                         f"(no fresh daemon/alive.json, or the stop file is set)")
    if record is None:
        raise ValueError(
            f"{tool}: {param} {manager_sid!r} does not match any active {t['record_noun']} record — "
            f"{t['missing']}. {t['advice']}{t['missing_tail']}"
        )
    if record.get("agent") not in t["parents"] or record.get("nested"):
        raise ValueError(
            f"{tool}: {param} {manager_sid!r} belongs to an active {_record_label(record, t)} record "
            f"({record.get('name')!r}), not {t['expects']} — {t['wrong_kind']}. "
            f"{t['wrong_kind_advice']}{t['wrong_kind_tail']}."
        )
    name = record.get("name")
    if not name:
        raise ValueError(
            f"{tool}: {param} {manager_sid!r} resolves to a {_record_label(record, t)} record with no name "
            f"({name!r}) — the registration is incomplete or corrupt, and {t['unnamed']}. "
            f"{t['unnamed_advice']}{t['unnamed_tail']}."
        )
    return record


def _resolve_parent_manager(manager_sid: str | None, tool: str = "spawn_worker") -> str | None:
    record = _resolve_parent_record(manager_sid, tool=tool)
    return None if record is None else record.get("name")

def _resolve_manager_name_for_filter(manager_sid: str | None, tool: str) -> str | None:
    name = _manager_name_from_sid(manager_sid)
    if manager_sid and name is None:
        print(
            f"{tool}: manager_sid {manager_sid!r} did not resolve to an active manager "
            "record; filter degraded to wildcard (returning ALL records, not just this "
            "manager's). Pass the manager's session UUID, not its funny name.",
            file=sys.stderr,
        )
    return name


@mcp.tool()
async def ask_parent(child_sid: OwnSid, question: str,
                     resume_question_id: Annotated[str | None, _RESUME_QUESTION_ID] = None,
                     ctx: Context | None = None) -> str:
    """[CHILD] Ask your parent session a question and wait for the answer; the parent relays it (a manager relays
    it to the human). Returns the answer text. Never proceed without the answer. The wait lasts up to 12 hours
    server-side (25 minutes when the client sends no progress token). When it ends unanswered, the call returns a
    NO_ANSWER_YET sentinel naming your question_id: the question is STILL pending, so call ask_parent again with
    the same child_sid and question plus resume_question_id="<question_id>" to keep waiting without duplicating
    it."""
    return await ask_manager_impl(child_sid, question, resume_question_id=resume_question_id,
                                  tool_name="ask_parent", sid_param="child_sid",
                                  keepalive=_progress_keepalive(ctx))


@mcp.tool()
async def ask_engineer(claude_sid: OwnSid, question: str, task_key: str | None = None) -> dict:
    """[MANAGER] Record a question to the engineer on a topic (task_key, else fleet-<your domain>) so it sits in
    the board's Inbox until answered; returns at once with question_id. The answer reaches you as an ANSWER line
    in your tab; read the full text at decision.q-<id8> of that topic. If the engineer answers in chat, close it
    yourself with answer_question(question_id, <his words>, decided_by="engineer")."""
    return ask_engineer_impl(claude_sid, question, task_key=task_key)


@mcp.tool()
async def ask_manager(claude_sid: OwnSid, question: str,
                      resume_question_id: Annotated[str | None, _RESUME_QUESTION_ID] = None,
                      ctx: Context | None = None) -> str:
    """[DEPRECATED alias for `ask_parent` — kept until the alias-deletion design runs] [WORKER] Ask the manager a
    question and wait for the answer; the manager relays it to the human. Returns the answer text. Never proceed
    without the answer. The wait lasts up to 12 hours server-side (25 minutes when the client sends no progress
    token). When it ends unanswered, the call returns a `NO_ANSWER_YET:` sentinel naming your question_id: the
    question is STILL pending with the manager, so call ask_manager again with the same claude_sid and question
    plus resume_question_id="<question_id>" to keep waiting without duplicating it."""
    return await ask_manager_impl(claude_sid, question, resume_question_id=resume_question_id,
                                  keepalive=_progress_keepalive(ctx))

@mcp.tool()
def child_done(child_sid: OwnSid, summary: str) -> dict:
    """[CHILD] Signal your parent that this session has completed its task: a one-shot done event that pages your
    parent (a pipeline node whose own advance committed goes to the work's events.jsonl instead). Call it once, as
    your last action on the task. Returns {ok: true, event_id}. A nested sub-session gets {ok: false, nested: true}
    and no event: its parent supervises it. Raises ValueError when the session is not registered."""
    return worker_done_impl(child_sid, summary)


@mcp.tool()
def worker_done(claude_sid: OwnSid, summary: str) -> dict:
    """[DEPRECATED alias for `child_done` — kept until the alias-deletion design runs] [WORKER] Signal the manager that this worker has
    completed its task: a one-shot done event, written to done/<parent-manager-name>/<sid>-<event_id>.json, which
    pages the owning manager only (a pipeline node whose own advance committed goes to the work's events.jsonl
    instead). Call it once, as your last action on the task. A keyed worker's event also carries artifacts_published,
    the count of artifacts it wrote. Returns {ok: true, event_id}. A nested sub-session gets {ok: false, nested: true} and no event: its parent supervises it. Raises ValueError when the session is not registered."""
    return worker_done_impl(claude_sid, summary)


@mcp.tool()
async def wait_for_child(
    name_or_sid: Annotated[str, Field(description="The child's name or session id.")],
    parent_sid: Annotated[str, Field(description=(
        "Required: the parent whose children you wait on. Your own session UUID for your own children (a "
        "manager's or a worker's). A worker waiting on a sibling passes the parent_sid on its own active record "
        "(<state root>/active/<your sid>.json), which the orchestrator re-stamps when your manager is recreated; "
        "$CLAUDE_PARENT_SID is the same value at spawn time and goes stale after a manager recreate."))],
    timeout_sec: int = 1500,
) -> dict:
    """[PARENT] Block until the named child completes (child_done), asks you a question (ask_parent), or its
    session exits. The wait is scoped to parent_sid's children. Returns {found: "done"|"question"|"exited"|
    "timeout", …}. On "question", answer it with answer_child(question_id, text) and call again. On "timeout"
    (after timeout_sec, default 1500 s) the child is still registered: call again to keep waiting. Raises
    ValueError for an empty or dead parent_sid instead of degrading to the whole fleet, and at once when no child
    holds the name."""
    if not parent_sid:
        raise ValueError("wait_for_child requires a live parent_sid; pass your own session UUID "
                         "(the sid from your boot context), not a name")
    parent_name = _resolve_parent_manager(parent_sid, tool="wait_for_child")
    return await wait_for_child_impl(name_or_sid, timeout_sec, manager_name=parent_name)


@mcp.tool()
async def wait_for_worker(
    name: str,
    timeout_sec: int = 3600,
    manager_sid: Annotated[str | None, Field(description=(
        "Scopes the lookup to workers whose parent is the session this id names: a manager passes its own session "
        "id. A worker waiting on a sibling passes nothing — its own sid would match only its own children. None "
        "matches any worker; an id that names no live session degrades to any worker with a stderr warning."))] = None,
) -> dict:
    """[DEPRECATED alias for `wait_for_child` — kept until the alias-deletion design runs] [WORKER] Block until
    the named worker completes (worker_done) or its session exits. A downstream worker calls it to chain on its
    upstreams. Returns {"found": "done", summary, sid, …} on the worker's done event, or {"found": "exited", …}
    when its session exits without one. Done events older than your latest send_manager_to_worker to that worker
    (minus 2 s grace) do not count: each send starts a new tasking episode, so harvest a done event before any
    follow-up. On "exited" (a crashed worker): read its brief (list_workers / list_closed_workers) and
    artifact_list(task_key), then re-dispatch from the last complete artifact. Raises TimeoutError after
    timeout_sec (default 3600 s), and ValueError at once when no such worker exists."""
    return await wait_for_worker_impl(name, timeout_sec, manager_name=_resolve_manager_name_for_filter(manager_sid, "wait_for_worker"))

@mcp.tool()
def answer_child(question_id: str, text: str,
                 decided_by: Annotated[str, _DECIDED_BY_ANSWER] = "manager") -> dict:
    """[PARENT] Answer a pending child question: writes the answer the child's ask_parent waits on, which unblocks
    it. A question from a keyed top-level worker also gets a decision.q-<id8> record carrying decided_by. On a
    pipeline work it also journals a question_answered event: a node's redo edge counts only an answer to its
    Redesign request whose first line is "redesign: yes", given with decided_by="engineer". Returns {ok: true}.
    Raises ValueError for an unknown question_id or a decided_by outside manager|engineer."""
    return answer_question_impl(question_id, text, decided_by)


@mcp.tool()
def answer_question(question_id: str, text: str,
                    decided_by: Annotated[str, _DECIDED_BY_ANSWER] = "manager") -> dict:
    """[DEPRECATED alias for `answer_child` — kept until the alias-deletion design runs] [MANAGER] Answer a
    pending worker question. Until you answer, the worker is BLOCKED: its ask_manager waits up to 12 h without
    re-asking, and only this call unblocks it — a send_manager_to_worker does not. After a kill and resume the
    question is gone: send the answer with send_manager_to_worker instead. A keyed worker's question also gets a
    decision.q-<id8> record carrying decided_by. On a pipeline work it also journals a question_answered event: a
    node's redo edge counts only an answer to its Redesign request whose first line is "redesign: yes", given with
    decided_by="engineer". Returns {ok: true}. Raises ValueError for an unknown question_id or a decided_by outside
    manager|engineer."""
    return answer_question_impl(question_id, text, decided_by)

@mcp.tool()
def list_pending_questions(
    manager_sid: Annotated[str | None, Field(description=_MANAGER_SID + (
        " It scopes the list to your own workers' questions; a bad id degrades to every manager's questions with "
        "a stderr warning. None returns every question, including null-parent (legacy) ones and legacy flat "
        "questions/<question_id>.json files, which scoped calls and the lanes never surface; booting a single "
        "manager also adopts those orphans (_backfill_legacy_workers)."))] = None,
    include_engineer_asks: Annotated[bool, Field(description=(
        "True also lists your own open ask_engineer questions, the reminders included; False (default) lists "
        "worker questions only."))] = False,
) -> list[dict]:
    """[MANAGER] List worker questions waiting for an answer, oldest first. Call it on a QUESTION lane line, relay
    the oldest verbatim, labelled with the worker name, then answer it with answer_question. Returns one dict per
    question: question_id, worker_name, worker_sid, question, asked_at, parent_manager_name. With
    include_engineer_asks=True the list also holds your own open ask_engineer questions."""
    return list_pending_questions_impl(manager_name=_resolve_manager_name_for_filter(manager_sid, "list_pending_questions"),
                                       include_engineer_asks=include_engineer_asks)

@mcp.tool()
def list_children(
    parent_sid: Annotated[str, Field(description="Your own session UUID — a manager's or a worker's.")],
) -> list[dict]:
    """[PARENT] List your own child sessions with their state, last_summary and question_pending (the oldest
    unanswered question_id, or null). Raises ValueError for an empty or dead parent_sid. Rows carry
    human_attached and human_attached_until (ISO time the flag drops, or null)."""
    if not parent_sid:
        raise ValueError("list_children requires parent_sid; pass your own session UUID "
                         "(the sid from your boot context)")
    return list_workers_impl(manager_name=_resolve_parent_manager(parent_sid, tool="list_children"))


@mcp.tool()
def list_workers(manager_sid: ManagerSid = None) -> list[dict]:
    """[DEPRECATED alias for `list_children` — kept until the alias-deletion design runs] [MANAGER] List worker
    sessions. Call it for status on request (a compact table), to verify a lane line, and after a takeover or
    recreate for each worker's brief. Do not poll it on a timer: the lanes page you. Returns one row per worker:
    its active record plus runtime, state, last_summary, last_turn_at, alive (a live pid check), brief (its
    assignment record), question_pending (the oldest unanswered question_id, or null, as on list_children),
    human_attached, provider, spend and context_role (the role it loaded). A claude -p or interactive claude launched from a worker's or your Bash
    shows as a nested-<sid8> row with nested: true and nested_parent_name. It sends no turn-end or done events and
    no ask_manager; the stale monitor never pages, nudges or autocloses it; its parent worker supervises it.
    /clear is not nesting: the hook re-registers the same-process record under its existing name and buckets.
    Every call runs the dead-pid cleanup, which removes dead leftovers, nested ones included. Rows carry
    human_attached and human_attached_until (ISO time the flag drops, or null)."""
    return list_workers_impl(manager_name=_resolve_manager_name_for_filter(manager_sid, "list_workers"))

@mcp.tool()
async def send_to_child(
    name: str,
    text: Annotated[str, Field(description=(
        "For an interactive child: the message, typed prefixed `[MANAGER] `. For a headless child whose round "
        "ended: the next round's follow-up prompt."))],
) -> dict:
    """[PARENT] Send to a child. For an interactive child (claude-tui): types text into its pane, prefixed
    `[MANAGER] `; the result carries blocked_on: <question_id> while the child has a pending question. For a
    headless child (codex-exec, claude-exec) whose round has ended: opens the next round with the same provider,
    env and arguments as the spawn, in a new window `<parent>/<child> rN`; wait_for_child then returns the new
    round's report. completed_early: true in the result means the round ended before its registration was
    observed — call wait_for_child as usual; it returns the report when one landed and "exited" otherwise.
    RAISES when an interactive child has no live window, and while a headless round is still running. RAISES
    for a child pipeline_child started while its slot is open: its next round goes through pipeline_child. Into
    a HUMAN-ATTACHED child it types nothing and returns {status: "human_attached", delivered: false, until,
    explanation}."""
    from .pipeline_child import refuse_send_to_leg
    refuse_send_to_leg(name, "send_to_child")
    return await send_to_child_impl(name, text)


@mcp.tool()
async def send_manager_to_worker(
    worker: Annotated[str, Field(description="The worker's name or session id.")],
    text: Annotated[str, Field(description=(
        "The message. The `[MANAGER] ` prefix is added for you — workers use it to tell a manager relay from the "
        "engineer typing into their pane — so never hand-prepend a marker of your own. A leading `/` arrives as "
        "plain text, never a slash command: spell the ask out. End every follow-up with the worker_done "
        "contract: \"When done, call worker_done(claude_sid, summary).\""))],
    auto_resume: Annotated[bool, Field(description=(
        "False (default): a worker with no live window RAISES. True: on a failed live send, if a closed record "
        "with a resumable transcript exists under this NAME (a sid gets the live path only), resume the NEWEST "
        "such record — a new tab in the original cwd via claude --resume / codex resume, full prior conversation "
        "restored — wait briefly for the TUI to accept input, and deliver in the same call; the result also "
        "carries {resumed: true, sid}. It delivers to the resumed session's ACTUAL registered handle, which can "
        "come back suffixed: use the returned worker name for follow-ups. Still RAISES when nothing is resumable "
        "(never existed, no transcript, name held by an active session, registration timeout); the "
        "no-silent-inbox contract is unchanged. A closed pipeline node that holds the work's token, declares a checkpoint and holds at "
        "least [pipeline] fresh_context_tokens of context is never resumed: the engine restarts the node in a fresh "
        "session from its checkpoints with your message in its brief and returns {status: \"replaced\", worker, "
        "replaced: true, sid}, or {status: \"replace_pending\", reason} while that restart waits on its spawn: the "
        "message rides the restart, so do not send it again. This is the resume-first send of a task-keyed dispatch "
        "(spawn_worker's task_key names the match); on a resume-side failure fall back to resume_worker, then a "
        "plain send."))] = False,
) -> dict:
    """[DEPRECATED alias for `send_to_child` — kept until the alias-deletion design runs] [MANAGER] Send an
    instruction to a worker: types text into its pane. Use it for a follow-up to a worker that holds the task's
    context; a new task gets a fresh spawn_worker. The terminal buffers the text while the worker is mid-turn and
    submits it at its next idle. The call resolves the live pane via the terminal driver and stamps the
    discovered id onto the worker record; no inbox file is ever written. Each send starts a new tasking episode:
    harvest a done event before any follow-up, because wait_for_worker then ignores done events older than this
    send (minus 2 s grace). A worker whose worker_done is newer than both your last message and the engineer's
    last typed line gets no idle nudge: its report IS its hold, so never send it hold/ok — any message brings a
    nudge back.

    Returns {status: "delivered", worker, confirmed} (confirmed false: the terminal did not confirm the keys),
    plus blocked_on: <question_id> while the worker has a pending
    question; it stays BLOCKED until answer_question, so message it only to redirect. Into a HUMAN-ATTACHED
    worker (he typed at its pane within the last 40 min) it types nothing and returns {status: "human_attached",
    delivered: false, until: "HH:MM", explanation}: hold the message and retry after until, or
    set_human_attached(worker, false) first; never re-word it as a nudge, never hand it to him as a question.

    RAISES when the worker has no live window (dead or closed): there is NO silent inbox, so a failed send is a
    hard error, never a queue — resume_worker or re-spawn (or pass auto_resume=True). RAISES for a nested
    sub-session: message its parent worker instead. RAISES for a child pipeline_child started while its slot is
    open."""
    from .pipeline_child import refuse_send_to_leg
    refuse_send_to_leg(worker, "send_manager_to_worker")
    if auto_resume:
        return await send_manager_to_worker_auto_impl(worker, text)
    return send_manager_to_worker_impl(worker, text)


@mcp.tool()
def send_manager_to_manager(
    name: Annotated[str, Field(description="The peer manager's name; list_managers() resolves peer names.")],
    text: Annotated[str, Field(description=(
        "The message, typed prefixed `[MANAGER <your name> · <your domain>] ` so the peer can tell a peer relay "
        "from the engineer typing into their pane — never hand-prepend a marker of your own. The sender comes "
        "from YOUR OWN active manager record, never an argument; when it cannot be resolved the prefix degrades "
        "to `[MANAGER] `. Because of the prefix a leading slash arrives as plain text and never triggers harness "
        "slash expansion: spell the ask out."))],
) -> dict:
    """[MANAGER] Message a peer manager by name. If the peer's input box is idle, types text into their pane
    (status delivered_live). If a human is mid-typing, it does NOT type, so it never clobbers the peer's input,
    and returns peer_busy (delivered=False): retry when the peer is free. There is NO silent inbox. Returns
    {status, manager, sender} on delivered_live — sender is null when your record could not be resolved — or
    {status, delivered, manager} on peer_busy. RAISES when the peer has no live window (dead or closed) or an
    unreadable one: a failed send is a hard error, never a queue."""
    return send_manager_to_manager_impl(name, text)

def set_human_attached_impl(name: str, attached: bool) -> dict:
    record = _find_worker_by_name_or_sid(name)
    sid = record.get("claude_sid")
    if not sid:
        raise ValueError(f"'{name}' has no claude_sid; cannot set human-attached mode")
    record["human_attached_at"] = time.time() if attached else None
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", record)
    return {"status": "ok", "worker": record["name"], "human_attached": attached}


def _pending_spawn_window(name: str) -> str | None:
    if not paths.ASSIGNMENTS_PENDING.is_dir():
        return None
    for sidecar in paths.ASSIGNMENTS_PENDING.glob("*.window"):
        try:
            window = sidecar.read_text().strip()
        except OSError:
            continue
        if not window:
            continue
        pending = state.read_json(sidecar.with_suffix(".json")) or {}
        if sidecar.stem == name or pending.get("name") == name:
            return window
    return None


def _prompt_target(name: str) -> tuple[dict | None, str, str]:
    try:
        record = _find_worker_by_name_or_sid(name)
    except ValueError:
        window = _pending_spawn_window(name)
        if window:
            return None, window, name
        raise
    window = ""
    for attempt in range(_WINDOW_RESOLVE_RETRIES):
        window = _resolve_live_worker_window(record)
        if window:
            break
        if attempt < _WINDOW_RESOLVE_RETRIES - 1:
            time.sleep(_WINDOW_RESOLVE_RETRY_SLEEP)
            record = _find_worker_by_name_or_sid(name)
    if not window:
        raise ValueError(f"'{record['name']}' has no live window (worker dead/closed?)")
    return record, window, record["name"]


def _prompt_view(marker: dict | None, status: str) -> dict | None:
    if marker is None:
        return None
    return {"tool_name": marker.get("tool_name"), "summary": marker.get("summary"), "at": marker.get("at"),
            "state": status, "tool_use_id": marker.get("tool_use_id"), "attempts": marker.get("attempts") or [],
            "decided": marker.get("decided")}


def get_worker_pane_impl(worker: str, lines: int = 60) -> dict:
    record, window, display = _prompt_target(worker)
    screen = _capture_plain(window) or ""
    prompt = None
    prompt_at = None
    if record is not None:
        marker, status = _prompt_status(record, time.time(), prompts_dir=paths.PERMISSION_PROMPTS)
        prompt = _prompt_view(marker, status)
        prompt_at = marker["at"] if status == "open" else None
    return {
        "worker": display, "pane": window,
        "text": "\n".join(screen.rstrip().splitlines()[-lines:]),
        "classic_dialog": _approval_dialog_block(screen) is not None,
        "dialog_sign": _dialog_sign(screen),
        "prompt_at": prompt_at,
        "prompt": prompt,
        "human_attached": bool(record) and is_human_attached(record),
    }


PERMISSION_ANSWER_KEYS = {"approve": "1", "refuse": "Escape", "approve_session": "2"}


def _dialog_on_screen(screen: str, marker_open: bool) -> bool:
    return (marker_open and _dialog_sign(screen)) or _approval_dialog_block(screen) is not None


def answer_permission_prompt_impl(worker: str, answer: str, prompt_at: float | None = None,
                                  manager_sid: str | None = None) -> dict:
    if answer not in PERMISSION_ANSWER_KEYS:
        raise ValueError(f"answer must be one of {sorted(PERMISSION_ANSWER_KEYS)}, not {answer!r}")
    record, window, display = _prompt_target(worker)
    now = time.time()
    marker = status = None
    if record is not None:
        marker, status = _prompt_status(record, now, prompts_dir=paths.PERMISSION_PROMPTS)
    open_marker = marker if status == "open" else None
    base = {"worker": display, "tool_name": open_marker.get("tool_name") if open_marker else None,
            "summary": open_marker.get("summary") if open_marker else None}
    if record is not None and is_human_attached(record, now):
        return {"status": "human_attached", **base}
    expected_at = open_marker["at"] if open_marker is not None else None
    if prompt_at != expected_at:
        return {"status": "changed", **base, "prompt_at": expected_at, "prompt": _prompt_view(marker, status)}
    screen = _capture_plain(window) or ""
    if not _dialog_on_screen(screen, open_marker is not None):
        return {"status": "no_dialog", **base, "prompt": _prompt_view(marker, status)}
    if answer == "approve_session" and SELF_EDIT_OPTION_TEXT not in screen:
        return {"status": "wrong_dialog", **base}
    sid = record.get("claude_sid") if record is not None else None
    entry = {"at": now, "sid": sid, "worker": display, "tool_name": base["tool_name"], "summary": base["summary"],
             "answer": answer, "by": "manager", "manager_sid": manager_sid}
    _append_permission_ledger({**entry, "outcome": "pressed"}, paths.PERMISSION_LEDGER)
    get_driver().send_key(window, PERMISSION_ANSWER_KEYS[answer])
    settled = False
    for _ in range(SAFEGUARD_SETTLE_POLLS):
        time.sleep(SAFEGUARD_SETTLE_STEP_SEC)
        try:
            screen = _capture_plain(window) or ""
        except Exception:
            continue
        if not _dialog_on_screen(screen, True):
            settled = True
            break
    if open_marker is None:
        if not settled:
            entry["outcome"] = "unsettled"
        _append_permission_ledger(entry, paths.PERMISSION_LEDGER)
        return {"status": "answered" if settled else "unsettled", **base}
    path = paths.PERMISSION_PROMPTS / f"{sid}.json"
    at = open_marker["at"]
    if settled:
        _update_marker(path, at, lambda m: m.update(
            decided={"by": "manager", "answer": answer, "at": now, "manager_sid": manager_sid}))
        _append_permission_ledger(entry, paths.PERMISSION_LEDGER)
        return {"status": "answered", **base}
    written = _update_marker(path, at, lambda m: m.setdefault("attempts", []).append(
        {"answer": answer, "at": now, "manager_sid": manager_sid}))
    entry["outcome"] = "unsettled" if written else "superseded"
    _append_permission_ledger(entry, paths.PERMISSION_LEDGER)
    return {"status": "unsettled" if written else "answered", **base}


@mcp.tool()
def answer_permission_prompt(
    worker: Annotated[str, Field(description="The worker's name or session id, or a pending spawn's name.")],
    answer: Annotated[Literal["approve", "refuse", "approve_session"], Field(description=(
        "approve presses 1; refuse presses Escape; approve_session presses 2, only on the ~/.claude "
        "self-edit dialog whose option says 'allow Claude to edit files in its ~/.claude folder'."))],
    prompt_at: Annotated[float | None, Field(description=(
        "The top-level prompt_at that get_worker_pane returned for the pane you read (the open marker's at, "
        "or null for a classic dialog with no open marker). Pass it as returned; a mismatch presses nothing."))] = None,
    manager_sid: Annotated[str | None, Field(description=(
        "Your own session id; recorded with the answer, never validated."))] = None,
) -> dict:
    """[MANAGER] Answer the permission dialog on a worker's pane with one keypress, after get_worker_pane showed
    you the whole command. prompt_at binds the keypress to that pane read: pass get_worker_pane's top-level
    prompt_at as returned (the open marker's at, or null on a classic dialog with no open marker). It presses
    only when a dialog is on screen: the marker is open and the pane shows a dialog box with its title, or a
    classic dialog (question and options) is visible. Four statuses press nothing: changed (the pane's prompt_at
    is not the one you passed: read the pane again; the result carries the current prompt_at and marker),
    no_dialog (nothing on screen), human_attached (the engineer is at the pane),
    wrong_dialog (approve_session without the self-edit option on screen). After the keypress it polls up to 5 s:
    answered records the decision on the marker and in permission-answers.jsonl; unsettled means the dialog
    stayed, the attempt is recorded and a second call is allowed. Returns {status, worker, tool_name, summary}.
    Raises ValueError for an unknown name or a dead window."""
    return answer_permission_prompt_impl(worker, answer, prompt_at, manager_sid)


@mcp.tool()
def set_human_attached(
    name: Annotated[str, Field(description="The worker's name or session id.")],
    attached: Annotated[bool, Field(description=(
        "True only when the engineer tells YOU he is taking the worker over — the mode arms itself when he types "
        "at a worker's pane, so you rarely set it. False hands the worker back to automation."))],
) -> dict:
    """[MANAGER] Arm or drop a worker's human-attached flag. It arms itself for 40 min from each line the
    engineer types at the worker's pane (`CLAUDE_ORCH_HUMAN_ATTACHED_TTL_SEC`); attached=True arms it for
    40 min from now. While it is set: kill_worker / kill_child refuse, send_manager_to_worker returns
    {status: "human_attached"} and types nothing, the engine neither closes nor replaces the session, and the
    stale lane pages no IDLE_NUDGED / IDLE_UNTASKED / STALE_PROCESSING / FINISHED_SILENTLY / APPROVAL_PROMPT for
    it and skips it in the 2 h idle autoclose. His own topic messages, gate verdicts and the engine's merge wakes still type when his
    input line is idle; a refused one retries each lane pass, and its reason names the line (draft, queued,
    no prompt, no capture). ask_manager, worker_done and answer_question flow as always, except that a worker_done
    repeating the session's previous PARKED line while his own line is the latest input at the pane and no relay
    started a wake since the previous done goes to the work's journal (the guide's done-lane row). It drops when he types `handback`
    at the pane, on attached=False, or 40 min after his last line. No page is sent either way; list_workers
    rows carry human_attached and human_attached_until. Returns {status: "ok", worker, human_attached}. Raises
    ValueError for an unknown worker."""
    return set_human_attached_impl(name, attached)


@mcp.tool()
def kill_child(name: str) -> dict:
    """[PARENT] Terminate a child session: drops its pending questions and closes its terminal pane. Capture the
    pane first; a live approval prompt means the worker is alive: do not kill it. A manager clears the prompt per
    dockwright-orchestrator-guide § APPROVAL_PROMPT pages; a worker asks its manager, naming the child and its
    window_id. Returns
    {killed_pid, iterm_sid, dropped_questions}, with already_dead: true when the process was already gone; a
    headless child whose round ended returns {closed_window, already_dead, note}. Raises ValueError for a nested
    sub-session (its parent process manages it) and for an unknown name. Refused while the engineer is attached to
    the worker (40 min after his last typed line): the error names the time the flag drops and
    set_human_attached(name, false), which drops it now."""
    _refuse_if_attached(name)
    return kill_worker_impl(name, dry_run=False)


@mcp.tool()
def kill_worker(worker: Annotated[str, Field(description="The worker's name or session id.")]) -> dict:
    """[DEPRECATED alias for `kill_child` — kept until the alias-deletion design runs] [MANAGER] Terminate a
    worker session: drops its pending questions, then closes its terminal pane, so the worker's SessionEnd hook
    fires and its record lands in closed/. Capture the pane first; a live approval prompt means the worker is
    alive: do not kill it. A manager clears the prompt per dockwright-orchestrator-guide § APPROVAL_PROMPT pages; a
    worker asks its manager, naming the child and its window_id. A token-holding pipeline node gets no manual successor: kill_worker it
    and the reconciler restarts it. Returns {killed_pid, iterm_sid, dropped_questions}, with already_dead: true
    when the process was already gone. Raises ValueError for a nested sub-session — talk to its parent worker —
    and for an unknown name. Refused while the engineer is attached to the worker (40 min after his last typed
    line): the error names the time the flag drops and set_human_attached(name, false), which drops it now."""
    _refuse_if_attached(worker)
    return kill_worker_impl(worker, dry_run=False)

@mcp.tool()
def get_worker_summary(worker: Annotated[str, Field(description="The worker's name or session id.")]) -> dict:
    """[MANAGER] Return the worker's full report: the summary of its newest worker_done since its current episode
    began (its last tasking or turn start) and no more than 600 s before its latest turn end, the dones
    FINISHED_SILENTLY counts; else its full, un-truncated last assistant message. Call it when a lane line's
    summary is cut or unclear, instead of reading the done file. Returns {name, summary, source, alive}: source
    "worker_done" adds event_id and completed_at; source "transcript" adds last_turn_at, and summary is null with
    error "transcript not found" when the transcript is missing. A line queued during the worker's turn does not
    start a new episode: after such a line, check the worker answered it. Raises ValueError for an unknown
    worker."""
    return get_worker_summary_impl(worker)

@mcp.tool()
def get_child_tail(name: str, lines: int = 50) -> dict:
    """[PARENT] Return the last N entries from the child's transcript (role + 200-char content preview). Returns
    {name, log_path, lines_returned, entries}, or {name, error} when the transcript is missing."""
    return get_worker_tail_impl(name, lines)


@mcp.tool()
def get_worker_tail(worker: str, lines: int = 50) -> dict:
    """[DEPRECATED alias for `get_child_tail` — kept until the alias-deletion design runs] [MANAGER] Return
    the last N entries from the worker's transcript .jsonl (role + 200-char content preview). Returns {name,
    log_path, lines_returned, entries}, or {name, error} when the transcript is missing."""
    return get_worker_tail_impl(worker, lines)

@mcp.tool()
def get_worker_pane(worker: str, lines: int = 60) -> dict:
    """[MANAGER] Read a worker's pane: the sanctioned capture before any kill and before any dialog answer, in
    place of a raw tmux capture-pane. Finds a registered worker by name or session id, or a pending spawn by
    name. Returns {worker, pane, text (the last N lines, trailing blank rows dropped), classic_dialog (a question
    and options are visible), dialog_sign (a dialog box with a permission-dialog title is on screen), prompt_at
    (the value answer_permission_prompt takes: the open marker's at, else null), prompt (the hook marker:
    {tool_name, summary, at, state open|closed|unknown, tool_use_id, attempts, decided}, or null),
    human_attached}. Raises ValueError for an unknown name or a dead window."""
    return get_worker_pane_impl(worker, lines)


@mcp.tool()
def attach_existing(manager_sid: ManagerSid = None) -> dict:
    """[MANAGER] Called by /manager on startup: returns your running workers and orphan (pending) questions, so
    you can announce the re-attached workers and the pending questions. Returns {workers, orphan_questions}: rows
    shaped as list_workers and list_pending_questions return them."""
    return attach_existing_impl(manager_name=_resolve_manager_name_for_filter(manager_sid, "attach_existing"))

@mcp.tool()
def become_manager(
    claude_sid: OwnSid,
    iterm_sid: str = "",
    domain: Annotated[str | None, Field(description="The manager's domain; defaults to 'general'.")] = None,
    name: Annotated[str | None, Field(description=(
        "A prior identity to preserve (the /manager-reboot in-place recycle lane); a name taken by a different "
        "live session is auto-suffixed. None rolls a funny <adjective>-<creature> pair, unique across all active "
        "records."))] = None,
) -> dict:
    """[MANAGER /manager command] Register this session as a manager. Managers are Claude-only; the record is
    always stamped runtime="claude". Returns {ok, name, domain, runtime, preflight}; preflight is the one-line
    server-side cleanup summary ("" = clean). A refusal `name '<name>' is taken by session <sid>` means an active
    record holds the name: PID pruning clears a dead holder on every call, and if a stale record remains, remove
    <state root>/active/<sid>.json."""
    return become_manager_impl(claude_sid, iterm_sid, domain=domain, name=name)


@mcp.tool()
def close_manager_self(claude_sid: OwnSid) -> dict:
    """[MANAGER /manager-close] Distill and persist this manager's memory, then exit: the graceful shutdown, with
    no replacement spawned. Synchronously runs the same distill that prepare_handoff does, writes the result to
    manager-memory/<domain>/<date>-<sid>.md, clears your records, then closes the session's own tab via the
    terminal driver (`tmux kill-pane`). The slash command will /exit immediately after; this tool only handles the
    durable-state side. Returns {ok, distill_path, name, domain}. Raises ValueError when the session is not a
    manager, or, before any write, while your domain's notebook exists and holds no `dockwright notebook mark` of
    this session from the last 60 minutes. A distill failure is non-fatal: the active record is still removed, the
    tab still closed, and distill_path is None."""
    return close_manager_self_impl(claude_sid)


def close_manager_self_impl(claude_sid: str) -> dict:
    record = state.read_json(paths.ACTIVE / f"{claude_sid}.json")
    if record is None or record.get("agent") != "manager":
        raise ValueError(f"session {claude_sid} is not a manager; cannot close")
    name = record.get("name")
    domain = record.get("domain") or DEFAULT_DOMAIN
    refusal = notebook_mark.refusal(claude_sid, domain)
    if refusal is not None:
        raise ValueError(refusal)
    iterm_sid = state.window_id_of(record)
    distill_path = distill_and_write_memory(claude_sid, domain=domain)
    paths.ACTIVE.joinpath(f"{claude_sid}.json").unlink(missing_ok=True)
    _drop_questions_for_worker(claude_sid)
    _close_window(iterm_sid)
    return {"ok": True, "distill_path": distill_path, "name": name, "domain": domain}


def _close_window(window_id: str) -> None:
    get_driver().close(window_id)


def _terminal_ls() -> list | None:
    return get_driver().ls()


def _resolve_manager_window(sid: str, name: str, exclude_id: str = "") -> str:
    data = _terminal_ls()
    if not data:
        return ""
    for os_window in data:
        for tab in os_window.get("tabs", []):
            for w in tab.get("windows", []):
                wid = str(w.get("id", ""))
                if not wid or wid == str(exclude_id):
                    continue
                env = w.get("env") or {}
                if sid and env.get("CLAUDE_CODE_SESSION_ID") == sid:
                    return wid
    for os_window in data:
        for tab in os_window.get("tabs", []):
            tab_title = tab.get("title") or ""
            for w in tab.get("windows", []):
                wid = str(w.get("id", ""))
                if not wid or wid == str(exclude_id):
                    continue
                title = w.get("title") or ""
                if name and (name in title or name in tab_title):
                    return wid
    return ""


@mcp.tool()
def list_managers() -> list[dict]:
    """[MANAGER] List all active manager sessions, peers across all domains. Call it to resolve a peer's name for
    send_manager_to_manager, or a live manager sid. Returns one row per manager: name, domain, claude_sid,
    iterm_sid, runtime, started_at, never_took_a_turn (registered over 10 minutes ago and has not finished a
    turn)."""
    _prune_stale_active_records()
    out = []
    for record in state.list_json_in(paths.ACTIVE):
        if record.get("agent") != "manager" or record.get("nested"):
            continue
        out.append({
            "name": record.get("name"),
            "domain": record.get("domain") or DEFAULT_DOMAIN,
            "claude_sid": record.get("claude_sid"),
            "iterm_sid": state.window_id_of(record),
            "runtime": record.get("runtime") or "claude",
            "started_at": record.get("started_at"),
            "never_took_a_turn": (record.get("last_turn_at") is None
                                  and record.get("last_turn_at_uptime") is None
                                  and time.time() - (record.get("started_at") or 0)
                                  > NEVER_TOOK_A_TURN_GRACE_SEC),
        })
    return out


@mcp.tool()
def list_closed_workers(
    manager_sid: ManagerSid = None,
    limit: Annotated[int | None, Field(description=(
        "None (default) returns every record. A positive integer keeps the newest N, applied after manager "
        "scoping and newest-first ordering; any other value raises ValueError."))] = None,
) -> list[dict]:
    """[MANAGER] List closed worker records, newest first by closed_at. The stale monitor closes your own idle
    workers silently — last turn older than 2 h, no pending question, no credit banner — writing closed/<sid>.json
    and killing the pane. Call it after a takeover or recreate for each worker's brief, and before a task-keyed
    dispatch to find a worker to resume (spawn_worker's task_key names the match; resume_worker or
    send_manager_to_worker(auto_resume=True) brings it back). Returns rows: name, claude_sid, cwd, runtime,
    last_summary, last_turn_at, closed_at, closed_reason, parent_manager_name, parent_sid, kind, depth, brief (its
    assignment record)."""
    return list_closed_workers_impl(manager_name=_resolve_manager_name_for_filter(manager_sid, "list_closed_workers"), limit=limit)


@mcp.tool()
def prepare_handoff(
    claude_sid: OwnSid,
    narrative_summary: Annotated[str, Field(description=(
        "What this session did, open threads with the user, in-flight worker state, and decisions not obvious "
        "from list_workers; about 10 sentences, read verbatim by the successor. Workers and pending questions "
        "carry across on their own."))],
    trigger_reason: Annotated[str, Field(description="Why the manager is recreated, e.g. \"mcp-refresh\".")],
) -> dict:
    """[MANAGER] Snapshot manager state for handoff to a replacement manager: writes handoffs/<id>.json with your
    workers and pending questions, and distills your transcript into manager memory. Then call
    spawn_replacement_manager(handoff_id); the replacement reads the file at path and calls
    become_manager_with_takeover to take over atomically. Returns {handoff_id, path, distill_path}. Raises
    ValueError when claude_sid is not the current manager, or, before any write, while your domain's notebook
    exists and holds no `dockwright notebook mark` of this session from the last 60 minutes."""
    return prepare_handoff_impl(claude_sid, narrative_summary, trigger_reason)

@mcp.tool()
def prepare_recovery_handoff(from_sid: str, trigger_reason: str = "account-flip-recovery") -> dict:
    """[MANAGER] Synthesize a handoff for a bricked predecessor manager that cannot take turns (used by
    /manager-takeover-recovery as its first act, before become_manager_with_takeover). Writes a
    prepare_handoff-shaped record with a placeholder narrative; runs no distill. Returns {handoff_id, path}."""
    return prepare_recovery_handoff_impl(from_sid, trigger_reason)

@mcp.tool()
def become_manager_with_takeover(claude_sid: OwnSid, takeover_from: str, handoff_id: str,
                                 iterm_sid: str = "") -> dict:
    """[MANAGER /manager-resume] Atomic takeover from a previous manager. Verifies the handoff matches
    takeover_from, closes the old manager's tmux pane via the terminal driver (tmux kill-pane — not SIGTERM; the
    graceful close lets its SessionEnd fire the retro and memory distill), inherits its name and domain so
    workers' parent_manager_name references stay valid, marks the handoff consumed, and appends to
    manager-triggers.jsonl. Returns {ok, name, domain, runtime, preflight, predecessor_pane_closed}: preflight is
    the reused inner become_manager cleanup line; predecessor_pane_closed confirms the old pane is gone (false →
    /manager-resume step 4b kill-panes it manually)."""
    return become_manager_with_takeover_impl(claude_sid, takeover_from, handoff_id, iterm_sid)

async def _resolve_old_manager_window_match(handoff: dict) -> str | None:
    from .spawner import window_id_exists
    from_sid = handoff.get("from_sid")
    if not from_sid:
        return None
    old_record = state.read_json(paths.ACTIVE / f"{from_sid}.json")
    if not old_record:
        return None
    old_window_id = state.window_id_of(old_record)
    if not old_window_id:
        return None
    if not await window_id_exists(str(old_window_id)):
        return None
    return f"window_id:{old_window_id}"


async def spawn_replacement_manager_impl(handoff_id: str) -> dict:
    from .spawner import spawn_worker_tab
    handoff_path = paths.HANDOFFS / f"{handoff_id}.json"
    handoff = state.read_json(handoff_path)
    if handoff is None:
        raise ValueError(f"no handoff with id {handoff_id}")
    if handoff.get("consumed_at") is not None:
        raise ValueError(f"handoff {handoff_id} already consumed")
    cwd = str(paths.ensure_manager_home())
    initial_prompt = f"/manager-resume {handoff_id}"
    target_window_match = await _resolve_old_manager_window_match(handoff)
    manager_extra_args = ["--model", config.manager_model()]
    try:
        async with asyncio.timeout(15):
            window_id, _ = await spawn_worker_tab(
                cwd=cwd,
                initial_prompt=initial_prompt,
                name=handoff.get("manager_name") or "",
                agent="manager",
                tab_title="manager (incoming)",
                target_window_match=target_window_match,
                runtime="claude",
                extra_args=manager_extra_args,
            )
    except (asyncio.TimeoutError, ConnectionRefusedError, OSError) as e:
        raise RuntimeError(
            "Could not spawn manager tab. Is tmux installed and able to start "
            "a server on -L dockwright?"
        ) from e
    return {
        "window_id": window_id,
        "tab_title": "manager (incoming)",
        "handoff_id": handoff_id,
        "runtime": "claude",
    }


@mcp.tool()
async def spawn_replacement_manager(handoff_id: str) -> dict:
    """[MANAGER] Spawn a new manager tab that will take over via /manager-resume: the second step of a recreate,
    after prepare_handoff. The new tab opens with initial prompt `/manager-resume <handoff_id>` in the OLD
    manager's OS-window, so the in-place swap keeps the manager OS-window stable across recreates. Its SessionStart
    hook renames the tab title from 'manager (incoming)' to its normal manager title once it takes over. Managers
    are Claude-only; the replacement always launches the Claude CLI. Returns {window_id, tab_title, handoff_id,
    runtime}. Raises ValueError for an unknown or already consumed handoff, and RuntimeError when the tab cannot
    be spawned."""
    return await spawn_replacement_manager_impl(handoff_id)

_RESUMES_IN_FLIGHT: set[str] = set()


async def resume_worker_impl(
    name: str,
    _registration_timeout_sec: float = 10.0,
    _poll_interval: float = 0.5,
    model: str | None = None,
    effort: str | None = None,
) -> dict:
    from .spawner import spawn_worker_tab
    closed_path, record = _find_closed_record_by_name(name)
    _refuse_orphaned_child(record)
    provider = _headless_provider(record)
    if provider is not None:
        raise ValueError(
            f"'{name}' is a headless {provider.name} child; a round needs a follow-up prompt — "
            "use send_to_child(name, text)"
        )
    sid = record.get("claude_sid")
    cwd = record.get("cwd") or os.getcwd()
    if not sid:
        raise ValueError(f"closed worker '{name}' has no claude_sid; cannot resume")
    _refuse_handed_off_node(name, sid)
    _refuse_over_fresh_context(name, sid, record.get("runtime") or "claude")
    _prune_stale_active_records()
    holder = next(
        (r for r in state.list_json_in(paths.ACTIVE) if r.get("name") == name), None
    )
    if holder is not None:
        if holder.get("agent") == "worker":
            hint = (
                "message it via send_manager_to_worker (or kill_worker first) "
                "instead of resuming"
            )
        else:
            hint = f"the name is held by an active {holder.get('agent') or 'session'}"
        raise ValueError(f"'{name}' is already active; {hint}")
    if (paths.ACTIVE / f"{sid}.json").exists():
        raise ValueError(
            f"session {sid} behind closed worker '{name}' is already active; "
            f"not resuming a live session"
        )
    if name in _RESUMES_IN_FLIGHT:
        raise ValueError(f"resume of '{name}' is already in progress")
    _RESUMES_IN_FLIGHT.add(name)
    try:
        with state.file_lock_nb(paths.ROOT / "resume-locks" / f"{paths._safe_segment(sid)}.lock"):
            return await _spawn_and_confirm_resume(
                spawn_worker_tab,
                closed_path=closed_path,
                record=record,
                name=name,
                sid=sid,
                cwd=cwd,
                _registration_timeout_sec=_registration_timeout_sec,
                _poll_interval=_poll_interval,
                model=model, effort=effort,
            )
    except BlockingIOError:
        raise ValueError(f"resume of '{name}' is already in progress") from None
    finally:
        _RESUMES_IN_FLIGHT.discard(name)


def _live_parent_sid(parent_name: str | None) -> str | None:
    return (_live_parent_record(parent_name) or {}).get("claude_sid") or None


def _live_parent_record(parent_name: str | None) -> dict | None:
    if not parent_name:
        return None
    if parent_name == DAEMON_SID:
        return _daemon_record()
    for record in state.list_json_in(paths.ACTIVE):
        if record.get("nested") or record.get("name") != parent_name:
            continue
        if not _pid_reads_alive(record.get("pid")):
            continue
        return record
    return None


def _refuse_handed_off_node(name: str, sid: str) -> None:
    try:
        assignment = state.read_json(paths.assignment_path(sid))
    except ValueError:
        return
    ticket = assignment.get("ticket") if isinstance(assignment, dict) else None
    if not ticket or not _handed_off(sid, ticket):
        return
    finished = _finished_node_of(ticket, assignment.get("assignment_id"))
    if finished is not None:
        raise ValueError(
            f"'{name}' finished {finished} of {ticket}: its outputs are complete and it reported, so the engine closed "
            "it; the token still sits on that node and nothing restarts it — start new work or a topic instead"
        )
    raise ValueError(
        f"'{name}' is a pipeline node of {ticket} that the token has left (handed off); resuming it would make "
        "a second owner — the engine restarts or advances the node with a fresh session, never a resume"
    )


def _finished_node_of(task_key, assignment_id) -> str | None:
    snap = _load_snapshot_for(task_key)
    if snap is None or not assignment_id:
        return None
    try:
        token = _fold_for(task_key).tokens.get(pipeline.DEFAULT_TOKEN_ID)
    except (OSError, KeyError, ValueError, TypeError):
        return None
    if token is None or token.holder != assignment_id or any(e.frm == token.node for e in snap.edges):
        return None
    return token.node


def _refuse_over_fresh_context(name: str, sid: str, runtime: str) -> None:
    try:
        assignment = state.read_json(paths.assignment_path(sid))
    except ValueError:
        return
    ticket = assignment.get("ticket") if isinstance(assignment, dict) else None
    if not (ticket and assignment.get("pipeline_request_id")) or _load_snapshot_for(ticket) is None:
        return
    try:
        token = _held_token(_fold_for(ticket), assignment.get("assignment_id"))
    except (OSError, KeyError, ValueError, TypeError):
        token = None
    tokens = _due_fresh_session(ticket, token, sid, runtime)
    if tokens is not None:
        raise ValueError(
            f"'{name}' holds {tokens} context tokens, at or over [pipeline] fresh_context_tokens: send the ask with "
            f"send_manager_to_worker(\"{name}\", <ask>, auto_resume=True) and the engine restarts the node fresh "
            "from its checkpoints with your message"
        )


def _refuse_orphaned_child(record: dict) -> None:
    if record.get("kind") != "subworker":
        return
    parent = state.parent_name_of(record)
    if _live_parent_record(parent) is None:
        raise ValueError(
            f"'{record.get('name')}' is a child of worker '{parent}', which is not live — "
            "resume the parent first"
        )


def _round_finished_early(child_round, closed_path, spawned_at: float) -> bool:
    if _report_has_content(Path(child_round.report_path)):
        return True
    closed = state.read_json(closed_path) or {}
    closed_at = closed.get("closed_at")
    if isinstance(closed_at, bool) or not isinstance(closed_at, (int, float)):
        return False
    return closed_at >= spawned_at


def _resume_model_args(args: list[str], model: str | None, effort: str | None) -> list[str]:
    from .spawner import _matches_option
    added: list[str] = []
    for flag, value, options in (("--model", model, set(models.MODEL_OPTIONS)), ("--effort", effort, {"--effort"})):
        present = any(_matches_option(a, options) for a in args)
        if present and value is not None:
            raise ValueError(f"resume_worker: the record carries its own {flag}; drop {flag.lstrip('-')}=")
        if not present and value is None:
            raise ValueError(f"resume_worker: the record carries no {flag}; pass {flag.lstrip('-')}=")
        if not present:
            added += [flag, value]
    return [*args, *added]


async def _spawn_and_confirm_resume(
    spawn_worker_tab,
    closed_path,
    record: dict,
    name: str,
    sid: str,
    cwd: str,
    _registration_timeout_sec: float,
    _poll_interval: float,
    provider=None,
    child_round=None,
    env_extra: dict | None = None,
    tab_title: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> dict:
    parent_manager_name = state.parent_name_of(record)
    env = {**(env_extra or {}),
           **({"CLAUDE_PARENT_MANAGER": parent_manager_name} if parent_manager_name else {})}
    parent_sid = _live_parent_sid(parent_manager_name)
    if parent_manager_name and parent_sid:
        env["CLAUDE_PARENT_SID"] = parent_sid
    if record.get("kind") == "subworker":
        env["CLAUDE_WORKER_KIND"] = "subworker"
    env = env or None
    runtime = provider.runtime if provider is not None else (record.get("runtime") or "claude")
    extra_args = None if provider is not None else (
        _resume_settings_args(sid, subworker=record.get("kind") == "subworker") if runtime == "claude" else None)
    if extra_args is not None:
        extra_args = _resume_model_args(extra_args, model, effort)
    pre_spawn_sids = {
        r.get("claude_sid") for r in state.list_json_in(paths.ACTIVE) if r.get("claude_sid")
    }
    spawned_at = time.time()
    try:
        async with asyncio.timeout(15):
            window_id, _ = await spawn_worker_tab(
                cwd=cwd,
                initial_prompt="",
                name=name,
                runtime=runtime,
                resume_sid=sid,
                route_to_workers_window=True,
                env=env,
                extra_args=extra_args,
                provider=provider,
                child_round=child_round,
                tab_title=tab_title,
            )
    except (asyncio.TimeoutError, ConnectionRefusedError, OSError) as e:
        raise RuntimeError(
            "Could not spawn tab to resume worker. Is tmux installed and able to "
            "start a server on -L dockwright?"
        ) from e
    deadline = time.monotonic() + _registration_timeout_sec
    while True:
        resumed = state.read_json(paths.ACTIVE / f"{sid}.json")
        if resumed is not None:
            if window_id and state.window_id_of(resumed) != window_id:
                resumed["window_id"] = window_id
                state.write_json_atomic(paths.ACTIVE / f"{sid}.json", resumed)
            _reclaim_closed_spend(record)
            closed_path.unlink(missing_ok=True)
            return {
                "ok": True, "sid": sid, "name": resumed.get("name") or name,
                "cwd": cwd, "window_id": window_id,
            }
        if child_round is not None and _round_finished_early(child_round, closed_path, spawned_at):
            _reclaim_closed_spend(record)
            return {
                "ok": True, "sid": sid, "name": name, "cwd": cwd,
                "window_id": window_id, "completed_early": True,
            }
        if runtime == "codex":
            for candidate in state.list_json_in(paths.ACTIVE):
                candidate_sid = candidate.get("claude_sid")
                if (
                    candidate.get("agent") == "worker"
                    and candidate.get("name") == name
                    and candidate_sid
                    and candidate_sid not in pre_spawn_sids
                ):
                    state.migrate_assignment(sid, candidate_sid)
                    _reclaim_closed_spend(record)
                    closed_path.unlink(missing_ok=True)
                    return {
                        "ok": True, "sid": candidate_sid, "name": name,
                        "cwd": cwd, "window_id": window_id,
                    }
        if time.monotonic() >= deadline:
            return {
                "ok": False, "name": name, "sid": sid, "cwd": cwd,
                "window_id": window_id,
                "reason": (
                    f"resumed session did not register within {int(_registration_timeout_sec)}s; "
                    "closed record left intact for retry"
                ),
            }
        await asyncio.sleep(_poll_interval)


@mcp.tool()
async def resume_worker(
    name: str,
    model: Annotated[str | None, Field(description=(
        "The --model for a Claude resume whose assignment record carries none; refused when the record carries "
        "its own."))] = None,
    effort: Annotated[str | None, Field(description=(
        "The --effort for a Claude resume whose assignment record carries none; refused when the record carries "
        "its own."))] = None,
) -> dict:
    """[MANAGER] Resume a previously closed worker by name — closed by its own exit, a kill, or the stale monitor's
    silent 2 h idle autoclose (list_closed_workers lists them). Opens a new tmux window in the worker's original cwd
    using the closed record's runtime (`claude --resume <sid>` or `codex resume <sid>`), with its full
    conversation history; with several closed records it takes the newest whose transcript still exists. The launch replays the extra_args persisted on the worker's assignment record, so a resumed worker
    keeps the exact permissions it was born with; a legacy record with no persisted args resumes in manual inline
    mode. Claude args with no system-prompt flag resume in GenericWorker once role manifests exist. A Claude resume
    replays the record's --model and --effort; a record lacking one takes it from model= / effort=, else is refused.
    A worker stopped by the Fable-limit banner resumes on an account with Fable left, else on Opus; one stopped by
    another credit banner resumes on the spent model: switch that pane's model or kill_worker it instead. The
    closed/ record is deleted only after the SessionStart hook re-registers the resumed session's own sid in
    active/. Returns the registered record's actual handle in name, and sid: route follow-ups by them — the name
    can come back suffixed if another session claimed it, and a codex resume under a new sid. Returns {ok: false,
    reason} when it never registers within ~10 s, leaving the closed record intact for retry. Raises ValueError
    when no closed record has a live transcript (fall back to a fresh spawn_worker), when a live session already
    holds the name (send_manager_to_worker to it, or kill_worker it), when a resume of
    this name is already in progress, for a headless child (send_to_child opens its next round), for a pipeline
    node the token has left (the engine restarts it), and for a subworker whose parent is not live. Nested
    sub-sessions are never archived to closed/ and cannot be resumed."""
    return await resume_worker_impl(name, model=model, effort=effort)


def _resolve_preset(preset: str) -> str:
    preset_path = paths.PRESETS / f"{preset}.md"
    if not preset_path.is_file():
        available = sorted(p.stem for p in paths.PRESETS.glob("*.md")) if paths.PRESETS.is_dir() else []
        raise ValueError(
            f"preset '{preset}' not found at {preset_path}; available: {available}"
        )
    return preset_path.read_text()


def _load_worker_preset() -> dict | None:
    if not config.worker_headless_preset():
        return None
    try:
        loaded = json.loads((paths.PRESETS / "worker-headless-settings.json").read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _claude_worker_settings_args(extra_args: list[str] | None = None) -> list[str]:
    rc_on = os.environ.get("CLAUDE_ORCH_WORKER_RC", "").strip() == "1"
    if extra_args:
        from .spawner import _matches_option
        if any(_matches_option(a, {"--settings"}) for a in extra_args):
            return ["--remote-control"] if rc_on else []
    if not rc_on and _load_worker_preset() is not None:
        return ["--settings", str(paths.PRESETS / "worker-headless-settings.json")]
    return ["--settings", json.dumps(_worker_settings_base()), *(["--remote-control"] if rc_on else [])]


def _legacy_inline_settings(rc_on: bool) -> dict:
    settings: dict = {"enableAllProjectMcpServers": True}
    if not rc_on:
        settings["remoteControlAtStartup"] = False
        settings["disableRemoteControl"] = True
    return settings


def _legacy_inline_settings_args() -> list[str]:
    rc_on = os.environ.get("CLAUDE_ORCH_WORKER_RC", "").strip() == "1"
    return ["--settings", json.dumps(_legacy_inline_settings(rc_on)), *(["--remote-control"] if rc_on else [])]


def _worker_settings_base() -> dict:
    rc_on = os.environ.get("CLAUDE_ORCH_WORKER_RC", "").strip() == "1"
    base = _load_worker_preset()
    if base is None:
        return _legacy_inline_settings(rc_on)
    if rc_on:
        base.pop("remoteControlAtStartup", None)
        base.pop("disableRemoteControl", None)
    return base


def _agent_guard_bin() -> str:
    from .daemon import _dockwright_bin
    return str(_dockwright_bin())


def _node_settings_args(task_key: str, request_id: str, extra_args: list[str], profile: dict,
                        guard: list[str] | None = None) -> list[str]:
    rest, value = pipeline.split_settings_arg(extra_args)
    base = _worker_settings_base() if value is None else pipeline.read_settings_value(value)
    problems = [*pipeline.profile_problems(profile, "profile_settings"),
                *pipeline.profile_problems({k: v for k, v in base.items() if k in pipeline.PROFILE_KEYS},
                                           "--settings base")]
    if problems:
        raise ValueError(f"pipeline {task_key}: node settings refused: " + "; ".join(problems))
    merged = pipeline.merge_settings(base, profile)
    if guard is not None:
        merged = pipeline.with_guard(merged, guard, _agent_guard_bin())
    target = paths.artifact_ticket_dir(task_key) / "settings" / f"{request_id}.json"
    state.write_json_atomic(target, merged)
    return [*rest, "--settings", str(target)]


def _resume_settings_args(sid: str, subworker: bool = False) -> list[str]:
    from . import roles
    from .spawner import SYSTEM_PROMPT_OPTIONS, _matches_option
    record = state.read_json(paths.assignment_path(sid)) or {}
    spawn_args = record.get("spawn_extra_args")
    if isinstance(spawn_args, list) and all(isinstance(a, str) for a in spawn_args):
        args = list(spawn_args)
    else:
        args = _legacy_inline_settings_args()
    if any(_matches_option(a, SYSTEM_PROMPT_OPTIONS) for a in args):
        return args
    try:
        resumed = roles.default_role_args(args, "child" if subworker else "worker", cwd=record.get("cwd"))
    except ValueError as err:
        raise ValueError(f"resume_worker: {err}") from err
    if resumed != args and record:
        state.write_json_atomic(paths.assignment_path(sid), {**record, "context_role": roles.DEFAULT_ROLE})
    return resumed


def _subworker_settings_args(mode: str) -> list[str]:
    from .providers import REPORT_MODE
    if mode != REPORT_MODE:
        return []
    preset_path = paths.PRESETS / "verifier-settings.json"
    if not preset_path.is_file():
        raise ValueError(f"subworker settings: preset {preset_path} missing; refusing to spawn a read-only "
                         "child without its preset")
    return ["--settings", str(preset_path)]


def _role_settings_args(extra_args: list[str], layer: dict, target: Path | None) -> list[str]:
    rest, value = pipeline.split_settings_arg(extra_args)
    base = _worker_settings_base() if value is None else pipeline.read_settings_value(value)
    from . import roles
    merged = roles.apply_layer(base, layer)
    if target is None:
        path = roles.write_spawn_settings(merged)
    else:
        state.write_json_atomic(target, merged)
        path = target
    return [*rest, "--settings", str(path)]


def _guarded_settings_args(extra_args: list[str], allowed: list[str], task_key: str, name: str) -> list[str]:
    rest, value = pipeline.split_settings_arg(extra_args)
    base = {} if value is None else pipeline.read_settings_value(value)
    target = paths.artifact_ticket_dir(task_key) / "settings" / f"{name}.json"
    state.write_json_atomic(target, pipeline.with_guard(base, allowed, _agent_guard_bin()))
    return [*rest, "--settings", str(target)]


async def _confirm_spawn_registration(
    name: str,
    timeout_sec: float,
    poll_interval: float,
) -> dict | None:
    deadline = time.monotonic() + timeout_sec
    while True:
        for record in state.list_json_in(paths.ACTIVE):
            if record.get("agent") == "worker" and record.get("name") == name:
                return record
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(poll_interval)


_SUBWORKER_CLEARED_ENV_KEYS = ("CLAUDE_PARENT_PID", "CLAUDE_DOMAIN", "CLAUDE_ITERM_SID")


def _role_slug(role: str) -> str:
    return role.strip().lower().replace("_", "-")


def _role_child_name(parent: str, role: str) -> str:
    base = f"{parent}-{_role_slug(role)}-r"
    taken = 0
    for directory in (paths.ACTIVE, paths.CLOSED, paths.ASSIGNMENTS_PENDING):
        for record in state.list_json_in(directory):
            name = record.get("name")
            if isinstance(name, str) and name.startswith(base) and name[len(base):].isdecimal():
                taken = max(taken, int(name[len(base):]))
    return f"{base}{taken + 1}"


def _log_tail(log_path) -> str:
    try:
        lines = [line for line in Path(log_path).read_text(errors="replace").splitlines() if line.strip()]
    except OSError:
        return "no log written"
    return lines[-1][:400] if lines else "empty log"


def _provider_died_early(provider, child_round, window_id) -> bool:
    if provider.family != "codex":
        return bool(window_id) and get_driver().pane_state(window_id) == "dead"
    try:
        return '"thread.started"' not in Path(child_round.log_path).read_text(errors="replace")
    except OSError:
        return True


async def spawn_worker_impl(
    initial_prompt: str,
    name: str | None = None,
    cwd: str | None = None,
    extra_args: list[str] | None = None,
    env: dict[str, str] | None = None,
    preset: str | None = None,
    manager_sid: str | None = None,
    runtime: str = "claude",
    task_key: str | None = None,
    force: bool = False,
    _registration_timeout_sec: float | None = None,
    _poll_interval: float | None = None,
    _tool: str = "spawn_worker",
    provider: str | None = None,
    model: str | None = None,
    mode: str | None = None,
    effort: str | None = None,
    _pipeline_request_id: str | None = None,
    context_role: str | None = None,
    _agent_guard: list[str] | None = None,
    _handed_inputs: list[str] | None = None,
) -> dict:
    from .spawner import (SYSTEM_PROMPT_OPTIONS, _matches_option, codex_gate, normalize_runtime,
                          spawn_worker_tab, usage_spawn_gate, worker_agent_file,
                          write_registry_snapshot)
    from .providers import LEG_MODES, REPLY_MODE, REPORT_MODE, ChildRound, get_provider, validate_mode
    runtime = normalize_runtime(runtime)
    _validate_task_key(task_key)
    parent_record = _resolve_parent_record(manager_sid, tool=_tool)
    parent_manager_name = None if parent_record is None else parent_record.get("name")
    child_kind = "subworker" if (parent_record or {}).get("agent") == "worker" else "worker"
    if parent_record is not None:
        child_depth = state.child_depth(parent_record, "worker", kind=child_kind)
        max_depth = config.subworkers_max_depth()
        if child_depth >= max_depth:
            raise ValueError(
                f"{_tool}: {_PARENT_RESOLVER_TEXT[_tool]['param']} {manager_sid!r} "
                f"({parent_manager_name!r}) is at depth {child_depth - 1}; its child would be at "
                f"depth {child_depth}, which reaches [subworkers] max_depth = {max_depth} — a session "
                "at that depth cannot spawn children. Ask your own parent to spawn a sibling instead"
            )
    if mode is not None:
        validate_mode(mode)
    if mode == REPLY_MODE and _tool != "topic":
        raise ValueError(f"{_tool}: mode 'reply' is the topic agent's; ask for report or interactive")
    cwd_defaulted = cwd is None
    if cwd is None:
        if child_kind == "subworker" and parent_record.get("cwd"):
            cwd = parent_record["cwd"]
        else:
            cwd = str(paths.worker_home())
    session_args: list[str] = []
    provider_name = provider
    implicit_provider = provider is None and child_kind == "subworker"
    if implicit_provider:
        provider_name = config.subworkers_default_provider()
    child_provider = None
    if provider_name is not None:
        selected = get_provider(provider_name)
        runtime = selected.runtime
        if selected.headless:
            child_provider = selected
    claude_tui_child = _tool == "spawn_child" and runtime == "claude" and child_provider is None
    if claude_tui_child:
        if mode is None:
            raise ValueError(f"{_tool}: mode is required for a claude-tui child: report or interactive")
        if mode not in LEG_MODES:
            raise ValueError(f"{_tool}: mode {mode!r} is not one of {', '.join(LEG_MODES)}")
        if not model:
            raise ValueError(f"{_tool}: model is required: a family token like @opus or a concrete "
                             "claude-<family>-<version> id")
        if not effort:
            raise ValueError(f"{_tool}: effort is required: one of {', '.join(models.EFFORTS)}")
        if extra_args:
            raise ValueError(f"{_tool}: a claude-tui child takes no extra_args")
        session_args = ["--model", model, "--effort", effort, *_subworker_settings_args(mode)]
    elif child_provider is not None:
        if not model:
            raise ValueError(f"{_tool}: model is required for {child_provider.name}")
        if child_provider.name == "codex-exec" and mode is None:
            raise ValueError(f"{_tool}: mode is required for codex-exec: report or interactive")
        if child_provider.family == "codex" and effort:
            raise ValueError(f"{_tool}: Codex takes no effort; drop effort=")
        if child_provider.family == "claude" and not effort:
            raise ValueError(f"{_tool}: effort is required for {child_provider.name}")
    mode = mode or REPORT_MODE
    validate_mode(mode)
    if mode == REPLY_MODE and provider_name != "claude-exec":
        raise ValueError("topic: mode 'reply' runs only on the claude-exec provider")
    from . import roles
    if cwd_defaulted and not (child_kind == "subworker" and parent_record.get("cwd")):
        home = paths.ensure_worker_home()
        cwd = str(home) if home.is_dir() else os.getcwd()
    from .profiles import profile_for
    profile = profile_for(cwd)
    launch = None
    if runtime == "claude" and child_provider is None:
        launch = "child" if claude_tui_child or child_kind == "subworker" else "worker"
    context = context_role
    own_prompt = any(_matches_option(a, SYSTEM_PROMPT_OPTIONS) for a in (extra_args or []))
    if own_prompt and context_role is not None:
        raise ValueError(f"{_tool}: role {context_role!r} and a system-prompt flag in extra_args are exclusive")
    role_files = role_layer = None
    if launch is not None and not own_prompt:
        try:
            role_files = roles.launch_files(context, launch, profile=profile)
            role_layer = None if role_files is None else roles.read_role_layer(role_files[1])
        except ValueError as err:
            raise ValueError(f"{_tool}: {err}") from err
    codex_family = child_provider is not None and child_provider.family == "codex"
    role_text = child_role_text = ""
    child_claude_files = None
    try:
        if runtime == "codex" and child_provider is None and _tool in ("spawn_worker", "pipeline"):
            role_text = roles.codex_text(context_role, profile=profile)
        elif codex_family:
            child_role_text = roles.codex_text(context, profile=profile).strip()
        elif child_provider is not None and child_provider.family == "claude":
            child_claude_files = roles.launch_files(context, "child", profile=profile)
    except roles.RoleError as err:
        raise ValueError(f"{_tool}: {err}") from err
    loaded_role = ((context or roles.DEFAULT_ROLE)
                   if (role_files is not None or role_text or child_role_text or child_claude_files is not None)
                   else None)
    write_registry_snapshot()
    if not codex_family:
        gate = usage_spawn_gate(force=force)
        if gate.get("status") in ("paused", "held"):
            return gate
    if runtime == "codex" or codex_family:
        try:
            codex_gate(cwd)
        except ValueError as err:
            if not implicit_provider:
                raise
            raise ValueError(
                f"{err} — a worker's child defaults to {provider_name}; for the previous "
                'release\'s interactive Claude child pass provider="claude-tui"'
            ) from err
    raw_name = name
    if name is None:
        if claude_tui_child and context_role is not None:
            name = _role_child_name(parent_manager_name or "unscoped", context_role)
        elif child_kind == "subworker":
            name = f"{parent_manager_name}-child"
        else:
            name = f"worker-{int(time.time())}"
    name = _resolve_unique_name(name)
    raw_prompt = initial_prompt
    ticket = task_key
    if preset is not None:
        initial_prompt = f"{_resolve_preset(preset)}\n\n---\n\n{initial_prompt}"
    report_child = mode in (REPORT_MODE, REPLY_MODE) and (child_provider is not None or claude_tui_child)
    agent_in_context = runtime == "claude" and child_provider is None and (
        (role_files is not None and launch == "worker")
        or (role_files is None and worker_agent_file() is not None and not own_prompt)
    )
    if ticket and (raw_prompt or "").strip() and not report_child:
        initial_prompt += _artifact_discipline_footer(ticket, agent_in_context)
    if (raw_prompt or "").strip() and not report_child:
        initial_prompt += _repo_sync_footer()
    if claude_tui_child:
        models.resolve_model(model)
        extra_args = session_args
    if child_provider is None and runtime == "claude":
        extra_args = _claude_worker_settings_args(extra_args) + (extra_args or [])
    else:
        extra_args = extra_args or []
    if role_files is not None:
        target = (paths.artifact_ticket_dir(task_key) / "settings" / f"{_pipeline_request_id}.json"
                  if _tool == "pipeline" and task_key and _pipeline_request_id else None)
        extra_args = ["--append-system-prompt-file", str(role_files[0]),
                      *_role_settings_args(extra_args, role_layer, target), *roles.agents_arg(profile)]
    if _agent_guard is not None and runtime == "claude" and child_provider is None:
        extra_args = _guarded_settings_args(extra_args, _agent_guard, task_key, name)
    if runtime == "claude":
        env = {**config.spawn_env(), **(env or {})}
    if env:
        env = {k: v for k, v in env.items()
               if k not in ("CLAUDE_PARENT_SID", "CLAUDE_WORKER_KIND", "CLAUDE_WORKER_PROVIDER")}
    if parent_manager_name:
        env = {**(env or {}), "CLAUDE_PARENT_MANAGER": parent_manager_name,
               "CLAUDE_PARENT_SID": manager_sid}
        if child_kind == "subworker":
            env["CLAUDE_WORKER_KIND"] = "subworker"
            for key in _SUBWORKER_CLEARED_ENV_KEYS:
                env.setdefault(key, "")
    _prune_stale_assignments()
    assignment_id = uuid.uuid4().hex
    if _handed_inputs and task_key:
        try:
            for event in _handed_events(assignment_id, _handed_inputs):
                _append_task_event(task_key, event)
        except (ValueError, OSError):
            pass
    child_round = None
    tab_title = None
    round_fields: dict = {}
    child_context = loaded_role if child_claude_files is not None else context
    if child_provider is not None:
        rounds_dir = paths.child_rounds_dir(assignment_id)
        rounds_dir.mkdir(parents=True, exist_ok=True)
        brief_path = rounds_dir / "r1.brief.md"
        report_path = rounds_dir / "r1.report.md"
        log_path = rounds_dir / "r1.log"
        brief_path.write_text(f"{child_provider.bootstrap(mode)}\n\n{child_role_text}\n\n{initial_prompt}"
                              if child_role_text else f"{child_provider.bootstrap(mode)}\n\n{initial_prompt}")
        child_round = ChildRound(
            mode=mode, cwd=cwd, brief_path=brief_path, report_path=report_path, log_path=log_path,
            model=model, effort=effort,
            session_id=str(uuid.uuid4()) if child_provider.family == "claude" else None,
            extra_args=tuple(extra_args), context_role=child_context,
        )
        tab_title = f"{parent_manager_name or 'unscoped'}/{name} r1"
        round_fields = {
            "provider": child_provider.name, "mode": mode, "model": child_round.model, "effort": effort,
            "child_context_role": child_context,
            "round": 1, "round_started_at": time.time(),
            "report_path": str(report_path), "log_path": str(log_path), "rounds_dir": str(rounds_dir),
            "spawn_env": {k: v for k, v in (env or {}).items() if k not in paths.ORCHESTRATOR_ENV_KEYS},
        }
    pipeline_fields = {} if _pipeline_request_id is None else {"pipeline_request_id": _pipeline_request_id}
    _write_pending_assignment(
        assignment_id, name, raw_prompt, preset, cwd,
        manager_sid, parent_manager_name, runtime,
        ticket=ticket,
        parent_sid=manager_sid,
        spawn_extra_args=extra_args,
        context_role=loaded_role,
        **round_fields,
        **pipeline_fields,
    )
    env = {**(env or {}), "CLAUDE_ASSIGNMENT_ID": assignment_id}
    try:
        async with asyncio.timeout(15):
            iterm_sid, _ = await spawn_worker_tab(
                cwd=cwd,
                initial_prompt="" if child_provider is not None else initial_prompt,
                name=name,
                runtime=runtime,
                route_to_workers_window=True,
                extra_args=extra_args,
                env=env,
                force=force,
                provider=child_provider,
                child_round=child_round,
                tab_title=tab_title,
                role_text=role_text,
            )
    except (asyncio.TimeoutError, ConnectionRefusedError, OSError) as e:
        paths.pending_assignment_path(assignment_id).unlink(missing_ok=True)
        paths.pending_window_path(assignment_id).unlink(missing_ok=True)
        raise RuntimeError(
            "Could not spawn a tab. Is tmux installed and able to start a server "
            "on -L dockwright?"
        ) from e
    except BaseException:
        paths.pending_assignment_path(assignment_id).unlink(missing_ok=True)
        paths.pending_window_path(assignment_id).unlink(missing_ok=True)
        if child_round is not None:
            shutil.rmtree(paths.child_rounds_dir(assignment_id), ignore_errors=True)
        raise
    if iterm_sid:
        try:
            paths.pending_window_path(assignment_id).write_text(str(iterm_sid))
        except OSError:
            pass
    timeout_sec = (
        _registration_timeout_sec if _registration_timeout_sec is not None
        else _DEFAULT_REGISTRATION_TIMEOUT_SEC
    )
    poll_interval = (
        _poll_interval if _poll_interval is not None else _DEFAULT_REGISTRATION_POLL_SEC
    )
    registered = await _confirm_spawn_registration(name, timeout_sec, poll_interval)
    result = {
        "iterm_sid": iterm_sid,
        "name": name,
        "cwd": cwd,
        "runtime": runtime,
        "kind": child_kind,
        "parent_manager_name": parent_manager_name,
        "parent_name": parent_manager_name,
        "parent_sid": manager_sid if parent_manager_name else None,
        "window_id": iterm_sid,
        "provider": None if child_provider is None else child_provider.name,
        "context_role": loaded_role,
        "assignment_id": assignment_id,
    }
    if child_provider is not None:
        result.update({"mode": mode, "round": 1, "report_path": str(child_round.report_path),
                       "rounds_dir": str(child_round.report_path.parent)})
    if claude_tui_child:
        result["mode"] = mode
    if ticket is None:
        hint = _unkeyed_key_hint(raw_name, raw_prompt)
        if hint:
            result["task_key_hint"] = hint
    if registered is not None:
        result["status"] = "registered"
        result["claude_sid"] = registered.get("claude_sid")
        result["note"] = "worker registered its active record via SessionStart hook"
    elif child_provider is not None and _provider_died_early(child_provider, child_round, iterm_sid):
        result["status"] = "provider-error"
        result["reason"] = _log_tail(child_round.log_path)
        result["note"] = (f"{child_provider.name} had not started when registration timed out; the "
                          "window is left open for reading and the pending assignment is left intact "
                          "for a late registration")
    else:
        result["status"] = "no_register"
        result["note"] = "worker registers itself via SessionStart hook"
        result["reason"] = (
            f"worker '{name}' did not register an active record within {int(timeout_sec)}s — "
            "it may be blocked on a pre-registration prompt (e.g. Claude Code's 'N new MCP "
            "servers found in this project' enable prompt, or the workspace-trust dialog). "
            "Capture the pane at window_id and clear it; the pending assignment is left intact "
            "for late registration."
        )
    return result


@mcp.tool()
async def spawn_child(
    parent_sid: Annotated[str, Field(description=(
        "Your own session UUID, required — a live top-level manager's or a live worker's."))],
    brief: Annotated[str, Field(description="The child's first prompt.")],
    name: Annotated[str | None, Field(description=(
        "The child's handle; auto-suffixes -2, -3 on collision with an active record. A worker's child defaults "
        "to `<parent>-child`. A `claude-tui` child with a context role is named `<parent>-<role-slug>-r<N>`, N "
        "counting spawns of that role under that parent; the slug rule and its example live in the "
        "dockwright-orchestrator-guide skill."))] = None,
    cwd: Annotated[str | None, Field(description=(
        "The child's working directory. A worker's child defaults to the parent's cwd; a manager's child to the "
        "worker home ([paths] worker_home), or the server's cwd when that home cannot be created."))] = None,
    task_key: Annotated[str | None, Field(description=(
        "Grouping key stamped into the child's assignment record, as on spawn_worker."))] = None,
    env: Annotated[dict[str, str] | None, Field(description=(
        "Variables added to the child's environment. They cannot override the orchestrator-controlled keys "
        "(`CLAUDE_AGENT`, `CLAUDE_WORKER_NAME`, `CLAUDE_WORKER_RUNTIME`, `CLAUDE_WORKER_PROVIDER`, "
        "`CLAUDE_ORCH_ACCOUNT`, `CLAUDE_CONFIG_DIR`, `CLAUDE_PARENT_SID`, `CLAUDE_WORKER_KIND`)."))] = None,
    provider: Annotated[str | None, Field(description=(
        "Who runs the child: `codex-exec` (headless `codex exec` in its own tmux window; only under `[codex] "
        "allowed_roots`, refused everywhere else), `claude-exec` (headless `claude -p`), `claude-tui` (a full "
        "interactive Claude session in tmux). A worker's child defaults to `[subworkers] default_provider` "
        "(`codex-exec`); a manager's child to `claude-tui`."))] = None,
    model: Annotated[str | None, Field(description=(
        "Required. A `claude-tui` or `claude-exec` child takes a family token like `@opus` or a concrete "
        "`claude-<family>-<version>` id; a `codex-exec` child a concrete Codex model id such as `gpt-5.6-sol`."))] = None,
    mode: Annotated[str | None, Field(description=(
        "\"report\" or \"interactive\"; required for `claude-tui` and `codex-exec`. A `claude-tui` child runs as a "
        "full session with `--model`, `--effort`, the `context` role's `role.child.md` and the settings preset "
        "`mode` selects: `report` is `verifier-settings.json`, read-only, the child keeps the MCP and finishes "
        "through child_done; `interactive` is the worker preset. `mode=\"report\"` runs a headless child with no "
        "dockwright MCP — its final message is its report and wait_for_child returns it as `done`. A `report` child "
        "gets no orchestrator footers; an `interactive` child gets them as a worker does. `mode=\"interactive\"` "
        "lets the child call ask_parent/child_done. Read-only is enforced differently per provider: `codex-exec` "
        "runs under `-c sandbox_mode=\"read-only\"`, a kernel sandbox that refuses writes, plus `-c "
        "approval_policy=\"never\"`, which refuses every tool escalation including a call to another MCP server; "
        "`claude-exec` and `claude-tui` run under `verifier-settings.json`, which denies the edit tools and the "
        "mutating git verbs but is a denylist, not a sandbox."))] = None,
    role: Annotated[str | None, Field(description=(
        "Gone with the roster; any value is refused."))] = None,
    context: Annotated[str | None, Field(description=(
        "The role manifest the child loads (GenericWorker when omitted): a `claude-tui` child its child render, a "
        "`claude-exec` child the same, or the role's Codex render in report mode; a `codex-exec` child its Codex "
        "translation as text."))] = None,
    effort: Annotated[str | None, Field(description=(
        "The child's --effort for claude-tui and claude-exec: low, medium, high, xhigh or max. Refused for "
        "codex-exec."))] = None,
) -> dict:
    """[PARENT] Spawn a child session in a fresh tmux window and register it under your name. A worker's child
    registers as kind=subworker at depth parent+1 and is refused when its depth would reach `[subworkers]
    max_depth` (default 3: a subworker cannot spawn children). Legacy spawn knobs (extra_args, preset, runtime,
    force) stay on spawn_worker. Returns the child record; the result carries parent_name, parent_sid and kind.
    Check the spawn result before you wait: status must be `registered`, a role-bearing Claude child must carry
    the `context_role` you passed, and a reviewer child `mode: "report"`. On `no_register` the child is alive and registers late: retry
    wait_for_child for up to a minute and spawn no other child. A child that fails the check is not the helper
    you asked for: kill it. Then block on wait_for_child until it returns done. Never kill a child that passed
    this check before wait_for_child returns done or exited. After a headless round ends, send_to_child(name, text)
    opens the next round. Raises
    ValueError for an empty parent_sid — an unscoped spawn stays on spawn_worker(manager_sid=None)."""
    if not parent_sid:
        raise ValueError("spawn_child requires parent_sid; an unscoped spawn stays on "
                         "spawn_worker(manager_sid=None)")
    if role is not None:
        raise ValueError("spawn_child: the roster is gone: pass context, mode, model and effort")
    from .pipeline_child import refuse_spawn_child
    refuse_spawn_child(parent_sid)
    return await spawn_worker_impl(
        brief, name=name, cwd=cwd, env=env, manager_sid=parent_sid,
        task_key=task_key, _tool="spawn_child",
        provider=provider, model=model, mode=mode, effort=effort, context_role=context,
    )


@mcp.tool()
async def spawn_worker(
    initial_prompt: Annotated[str, Field(description=(
        "First prompt given to the worker (can be empty for a bare runtime session). End it with the worker_done "
        "contract: \"When done, call worker_done(claude_sid, summary).\" The worker's own agent definition "
        "(Claude) or bootstrap prompt (Codex) tells it where its sid is. Every non-blank prompt gets a repo-sync "
        "footer appended (sync a repo once before reading it), and a keyed spawn also the artifact-discipline "
        "footer; blank prompts are left untouched. A worker DAG: a downstream worker blocks on its upstreams "
        "itself, so its prompt tells it to call wait_for_worker(<upstream>, timeout_sec=3600) for each upstream "
        "and read their summaries first. Fan out with spawn_worker(name=\"worker-a\", initial_prompt=\"Do A. End "
        "with worker_done(...).\") and the same for worker-b; fan in with spawn_worker(name=\"worker-c\", "
        "initial_prompt=\"First wait_for_worker('worker-a') and wait_for_worker('worker-b'); read their "
        "summaries, then do C. End with worker_done(claude_sid, summary).\")."))],
    name: Annotated[str | None, Field(description=(
        "Worker handle (e.g. 'web-rebase'). Auto-generated if None. If the name is already taken by an active "
        "record, an auto-suffix '-2', '-3' is appended."))] = None,
    cwd: Annotated[str | None, Field(description=(
        "Working directory for the worker. Defaults to the worker home ([paths] worker_home), or the server's cwd "
        "when that home cannot be created."))] = None,
    extra_args: Annotated[list[str] | None, Field(description=(
        "Extra CLI flags passed to the selected runtime before the prompt (e.g. [\"--model\", \"gpt-5.6-sol\"]). "
        "A Claude worker launch without both `--model` and `--effort` is refused. "
        "Claude workers default to `--settings <state root>/presets/worker-headless-settings.json` — the deployed "
        "headless preset: auto permission mode, the dockwright protocol allowlist, local-git verbs, and "
        "permissions.additionalDirectories for your [paths] code roots (absolute, injected by setup.sh) — before "
        "caller args. The default does not apply when the caller passes its own --settings, when [spawn] "
        "worker_headless_preset = false (the fleet-wide opt-out), or when the preset file is missing; a "
        "caller-passed --settings REPLACES the preset entirely (last-wins). Compose a task-scoped COPY of the "
        "preset (extra allow rules) only when Bash must be fast-pathed beyond the classifier and the shipped "
        "allowlist. In that copy: (1) keep the top-level keys (enableAllProjectMcpServers, the remote-control-off "
        "pair); (2) keep permissions.additionalDirectories — it clears the out-of-cwd first-write gate; (3) $…, "
        "heredocs and && chains are never allowlisted — the classifier vets them at runtime, and expansion-free "
        "prompts (printenv, not echo $VAR) are a fast-path preference only; (4) the copy is sticky across "
        "resume_worker. extra_args=[\"--permission-mode\", \"default\"] re-gates FILE EDITS only, for one spawn "
        "(a human at the pane): the preset's allow rules (git commit/checkout and the rest) still auto-approve, "
        "since allow rules are consulted before the mode default. For a fully manual spawn pass your own "
        "--settings, e.g. [\"--settings\", \"{\\\"enableAllProjectMcpServers\\\": true}\"]. The final composed "
        "extra_args are persisted on the assignment record and replayed verbatim by resume_worker, so a resumed "
        "worker keeps the exact permissions it was born with. Codex workers get `--ask-for-approval never "
        "--sandbox danger-full-access --dangerously-bypass-hook-trust` plus a worker-protocol bootstrap prompt; "
        "caller args cannot override those defaults or pass known Claude-only flags (--settings)."))] = None,
    env: Annotated[dict[str, str] | None, Field(description=(
        "Extra env vars exported in the worker's shell before the selected runtime runs (e.g. {\"MY_FLAG\": "
        "\"1\"}), merged over the operator's [spawn.env] defaults (caller values win). The orchestrator-controlled "
        "keys cannot be overridden — CLAUDE_AGENT, CLAUDE_WORKER_NAME, CLAUDE_WORKER_RUNTIME, "
        "CLAUDE_WORKER_PROVIDER, CLAUDE_ORCH_ACCOUNT, CLAUDE_CONFIG_DIR, CLAUDE_PARENT_SID and CLAUDE_WORKER_KIND "
        "are silently dropped from the caller's dict. Default: no extra vars."))] = None,
    preset: Annotated[str | None, Field(description=(
        "Name of a preset under <state root>/presets/<name>.md whose content is prepended to initial_prompt with "
        "a `\\n\\n---\\n\\n` divider: shared workflow boilerplate (rebase-first, commit style, test invocation, "
        "worker_done at end) so callers stop retyping it on every spawn. Raises ValueError if the file is "
        "missing. Default: None (no preset)."))] = None,
    manager_sid: Annotated[str | None, Field(description=_MANAGER_SID + (
        " It must match a live top-level manager's ACTIVE record (written by become_manager); the worker's active "
        "record then carries your name as parent_manager_name, which routes its list_workers rows, "
        "wait_for_worker lookups, questions and done events to you. An unknown id — or a worker or nested "
        "record's — is refused (ValueError, nothing spawned): a mistyped sid would otherwise spawn an unscoped "
        "worker whose events route to _unscoped/. None spawns a deliberately unscoped worker whose questions and "
        "done events no lane of yours pages: poll it yourself."))] = None,
    runtime: Annotated[str, Field(description=(
        "Worker CLI runtime: \"claude\" (default) or \"codex\" — codex only when the user explicitly asks. The "
        "runtime is stamped into active records via CLAUDE_WORKER_RUNTIME and returned by list_workers."))] = "claude",
    task_key: Annotated[str | None, Field(description=(
        "Grouping key stamped into the worker's assignment record, assignments/<sid>.json: initial prompt, cwd, "
        "branch, runtime, explicit task key and your manager name, written at spawn, claimed at registration, and "
        "surviving SessionEnd and crashes; list_workers and list_closed_workers surface it as brief. The key joins "
        "the worker into pipeline_status(task_key) and the artifact store under artifacts/<task_key>/. Explicit "
        "task_key is the ONLY keying path — nothing is ever derived from prompt or name text (a mention is not an "
        "assignment). Pass the SAME key on every spawn of the task's workers; use a stable slug for personal "
        "multi-agent tasks with no tracker key (e.g. \"demo-task\"). Validated fail-fast: a stable "
        "[A-Za-z0-9_-] slug; blank raises (omit it entirely for an unkeyed spawn). A keyed spawn gets the "
        "artifact-discipline footer appended — do not re-instruct it. When omitted the spawn is UNKEYED; if "
        "[task_keys] key_regex is configured and matches the prompt or the caller-supplied name, the result "
        "carries an advisory task_key_hint (nothing is stamped or filed). Resume-first: before a fresh task-keyed "
        "spawn, call list_closed_workers(manager_sid=<your sid>, limit=50) and match name == key.lower() or "
        "name.startswith(key.lower() + \"-\"). On a match, take the newest by closed_at (with several, prefer the "
        "more specific name) and call send_manager_to_worker(<that name>, <ask>, auto_resume=True); on a "
        "resume-side failure, resume_worker then a plain send. Spawn fresh on zero matches, when you kill_worker'ed "
        "that worker yourself (a killed record looks like a clean shutdown in closed/; your recollection is the "
        "only signal), or when its last_summary shows the task already done. Autoclosed-for-idle and "
        "closed-mid-task (cmd+w) workers are safe to resume. A missing transcript makes resume_worker raise "
        "ValueError: fall back to a fresh spawn."))] = None,
    force: Annotated[bool, Field(description=(
        "Bypass the usage breaker and the all-accounts-near-limit pause for THIS spawn only; the pick stays "
        "headroom-weighted and still skips bricked accounts. Use it for a genuinely urgent spawn after a "
        "{\"status\": \"paused\"} result: re-issue the SAME call with force=True. If the chosen account is truly "
        "maxed the worker bricks and the stale monitor's account flip recovers it."))] = False,
    role: Annotated[str | None, Field(description=(
        "The context role the worker loads — a role with a manifest in the roles directory. Omitted means "
        "GenericWorker; an unknown role refuses once any manifest exists."))] = None,
    tier: Annotated[str | None, Field(description=(
        "Gone with the roster; any value is refused: pass --model and --effort in extra_args."))] = None,
) -> dict:
    """[DEPRECATED alias for `spawn_child` — kept until the alias-deletion design runs] [MANAGER] Spawn a new
    worker in a fresh tmux window, registered under you. Use it for any new task. A follow-up to a worker that
    holds the task's context goes through send_manager_to_worker, and a task-keyed dispatch resumes a closed
    worker first (see task_key). Default to a fresh spawn unless an existing worker's loaded context (file reads,
    an in-flight branch) is genuinely valuable. Keep at most 10 spawns in flight: fan out in waves of at most 10,
    each gated on the prior wave (wait_for_worker or done events); never downgrade effort to compensate. A Fable spawn
    goes to an account with Fable left, else to Opus at xhigh or above; one on another spent family fails too.

    Returns the worker record. Instead of spawning — no tab, no assignment — it may return {"status": "paused",
    "reason", "hint", "<name>_pct" (one per pool account), "earliest_reset_ts", "retry_after_s"} when every
    selectable account is at or over [accounts] usage_pause_pct (dockwright.toml, default 88; env
    CLAUDE_ORCH_USAGE_PAUSE_PCT wins) of its 5 h limit. Routing reads each account's cached 5 h / 7 d usage and
    biases new spawns to the less-used account, dropping any at or over the threshold; a hot 7 d window biases
    but never refuses, and this pause never touches running workers. On paused: surface each <name>_pct, plus
    earliest_reset_ts / retry_after_s when non-null (null = retry whenever); hint names the force bypass. Then
    wait for the 5 h reset, queue the task, or re-issue the SAME call with force=True.

    An operator usage cap (`dockwright usage-cap`) returns the same shape with "pend_reason": "usage-cap";
    force does not lift it: wait for earliest_reset_ts or have the operator edit the cap file. While it
    holds, the stale lane closes every running worker.

    Raises ValueError, before anything is spawned, for a rejected manager_sid, a missing preset or a malformed
    task_key."""
    if tier is not None:
        raise ValueError("spawn_worker: tier is gone: pass --model and --effort in extra_args")
    return await spawn_worker_impl(initial_prompt, name, cwd, extra_args, env, preset, manager_sid, runtime, task_key,
                                   force, context_role=role)


def _write_artifact_atomic(path, text: str, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.stem}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    tmp.write_text(text)
    if exclusive:
        try:
            os.link(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return
    os.replace(tmp, path)


def _archive_replaced(path, incoming_content: str) -> str | None:
    try:
        old = path.read_text()
    except FileNotFoundError:
        return None
    try:
        _, old_body = state.parse_artifact(old)
    except ValueError:
        old_body = old
    if old_body == incoming_content:
        return None
    prev = path.with_name(path.name + ".prev")
    tmp = path.parent / f".{path.stem}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    tmp.write_text(old)
    os.replace(tmp, prev)
    return str(prev)


def _put_clobber_verdict(path, phase, name, content, status, writer_sid) -> str:
    try:
        existing = path.read_text()
    except FileNotFoundError:
        return "absent"
    try:
        stamp, body = state.parse_artifact(existing)
    except ValueError:
        if existing != content:
            return "non_record_file"
        return "demotes_final" if status == "partial" else "allowed"
    if stamp.get("phase") != phase or stamp.get("name") != name:
        return "colliding_record"
    owner = stamp.get("writer_sid")
    if not owner or owner != writer_sid:
        return "foreign_record"
    if stamp.get("status") == "partial":
        return "allowed"
    if body != content:
        return "own_complete_record"
    return "demotes_final" if status == "partial" else "allowed"


_PUT_REFUSALS = {
    "foreign_record": (
        "an artifact record owned by a different writer_sid. Publish under a "
        "different (phase, name), or pass overwrite=True to replace it "
        "deliberately (e.g. a successor finishing a dead worker's phase)."),
    "non_record_file": (
        "a file the store did not write (likely a hand-authored report). "
        "Publish under a different (phase, name), or pass overwrite=True to "
        "replace it deliberately."),
    "own_complete_record": (
        "your own finalized record (status is not \"partial\") with different "
        "content — a published record is final, and this refusal usually "
        "means you are about to destroy your published deliverable. Publish "
        "under a different (phase, name) instead; pass overwrite=True only "
        "when discarding the existing content is the intent."),
    "demotes_final": (
        "byte-equal content arriving with status=\"partial\" — demoting a "
        "final record (or stamping a finished hand-written report as partial) "
        "would make it silently replaceable. Re-put with status=\"complete\", "
        "or pass overwrite=True."),
    "colliding_record": (
        "a record stamped with a DIFFERENT (phase, name) — this put's pair "
        "collides with it after filename sanitization. Publish under a "
        "non-colliding (phase, name), or pass overwrite=True to replace it "
        "deliberately."),
}


def _handed_off(sid, task_key) -> bool:
    try:
        record = state.read_json(paths.assignment_path(sid))
    except ValueError:
        record = None
    try:
        events = _read_events(paths.artifact_events_path(task_key))
    except (OSError, ValueError):
        events = None
    assignment_id = record.get("assignment_id") if isinstance(record, dict) else None
    if events is not None and _taken_back(events, sid, assignment_id):
        return False
    if isinstance(record, dict) and record.get("handed_off_at"):
        return True
    if events is None:
        return False
    if any(e.get("type") == "handed_off" and e.get("sid") == sid for e in events):
        return True
    return isinstance(record, dict) and record.get("ticket") == task_key and _superseded_request(record, events)


def _taken_back(events, sid, assignment_id) -> bool:
    if not assignment_id:
        return False
    handed = restamped = -1
    for i, e in enumerate(events):
        if e.get("type") == "handed_off" and (e.get("assignment_id") == assignment_id or e.get("sid") == sid):
            handed = i
        elif e.get("type") == "holder_restamped" and e.get("new") == assignment_id:
            restamped = i
    if restamped > handed >= 0:
        return True
    try:
        open_requests = pipeline.fold(events).open_requests
    except (KeyError, ValueError, TypeError):
        return False
    return any(r.get("kind") == "back" and r.get("resume_candidate") == assignment_id for r in open_requests)


def _assignment_id_of(sid) -> str | None:
    try:
        record = state.read_json(paths.assignment_path(sid))
    except ValueError:
        return None
    return record.get("assignment_id") if isinstance(record, dict) else None


def _superseded_request(record, events=None) -> bool:
    request_id = record.get("pipeline_request_id") if isinstance(record, dict) else None
    task_key = record.get("ticket") if isinstance(record, dict) else None
    if not request_id or not task_key:
        return False
    try:
        events = _read_events(paths.artifact_events_path(task_key)) if events is None else events
        return request_id in pipeline.fold(events).superseded
    except (OSError, KeyError, ValueError, TypeError):
        return False


def artifact_put_impl(task_key, phase, name, content, status, writer_sid,
                      contract_hash=None, read_set=None, overwrite=False, scope="internal",
                      decided_by=None) -> dict:
    if status not in ("partial", "complete"):
        raise ValueError(f"status must be 'partial'|'complete', got {status!r}")
    if scope not in pipeline.SCOPES:
        raise ValueError(f"scope must be one of {'|'.join(pipeline.SCOPES)}, got {scope!r}")
    if decided_by is not None and (phase != "decision" or decided_by not in DECIDERS):
        raise ValueError(f"decided_by must be one of {'|'.join(DECIDERS)} and goes on decision.* records only, "
                         f"got {decided_by!r} on {phase}.{name}")
    if not writer_sid:
        raise ValueError(
            f"writer_sid must be a non-empty session id, got {writer_sid!r} "
            "(two callers passing the same falsy sid would alias into one "
            "store owner)")
    if _handed_off(writer_sid, task_key):
        state.append_event(paths.artifact_events_path(task_key), {
            "type": "artifact_put_refused", "phase": phase, "name": name,
            "actor_sid": writer_sid, "reason": "handed_off"})
        raise ValueError("artifact_put refused: node handed off; call worker_done")
    paths.ensure_dirs()
    written_at = time.time()
    stamp = {"phase": phase, "name": name, "status": status, "writer_sid": writer_sid,
             "contract_hash": contract_hash, "written_at": written_at,
             "read_set": read_set or [], "scope": scope}
    if decided_by is not None:
        stamp["decided_by"] = decided_by
    path = paths.artifact_path(task_key, phase, name)
    text = state.serialize_artifact(stamp, content)

    def _refuse(reason):
        state.append_event(paths.artifact_events_path(task_key), {
            "type": "artifact_put_refused", "phase": phase, "name": name,
            "actor_sid": writer_sid, "reason": reason})
        what = _PUT_REFUSALS.get(reason, (
            f"content the no-clobber guard could not classify "
            f"(verdict {reason!r}) — refusing by default; pass "
            "overwrite=True only for a deliberate replacement."))
        raise ValueError(f"artifact_put refused: {path} already holds {what}")

    archived = None
    if overwrite:
        archived = _archive_replaced(path, content)
        _write_artifact_atomic(path, text)
    else:
        verdict = _put_clobber_verdict(path, phase, name, content, status, writer_sid)
        if verdict == "allowed":
            archived = _archive_replaced(path, content)
            _write_artifact_atomic(path, text)
        elif verdict == "absent":
            try:
                _write_artifact_atomic(path, text, exclusive=True)
            except FileExistsError:
                verdict = _put_clobber_verdict(path, phase, name, content, status, writer_sid)
                if verdict == "allowed":
                    archived = _archive_replaced(path, content)
                    _write_artifact_atomic(path, text)
                elif verdict == "absent":
                    _write_artifact_atomic(path, text, exclusive=True)
                else:
                    _refuse(verdict)
        else:
            _refuse(verdict)
    event = {"type": "artifact_put", "phase": phase, "name": name,
             "actor_sid": writer_sid, "status": status, "contract_hash": contract_hash}
    holder = _assignment_id_of(writer_sid)
    if holder:
        event["actor_assignment"] = holder
    if overwrite:
        event["overwrite"] = True
    if archived:
        event["archived_previous"] = archived
    state.append_event(paths.artifact_events_path(task_key), event)
    warnings = _scope_warnings(task_key, phase, name, scope)
    result = {"ok": True, "path": str(path), "written_at": written_at, "status": status}
    if archived:
        result["archived_previous"] = archived
    if warnings:
        result["warnings"] = warnings
    tokens = _refresh_request(task_key, phase, name, scope, writer_sid)
    if tokens is not None:
        result["refresh"] = True
        result["refresh_note"] = _REFRESH_NOTE.format(tokens=tokens)
    return result


_REFRESH_NOTE = ("Your context holds {tokens} tokens, at or over [pipeline] fresh_context_tokens. End your turn now: "
                 "the engine continues this node in a fresh session from your checkpoints.")


def _fresh_context_over(sid, runtime="claude") -> int | None:
    tokens = transcript.context_tokens(sid, runtime)
    if tokens is None or tokens < config.pipeline_int("fresh_context_tokens", 500000):
        return None
    return tokens


def _declares_checkpoint(task_key, token) -> bool:
    snap = _load_snapshot_for(task_key) if token is not None else None
    node = snap.nodes.get(token.node) if snap is not None else None
    return node is not None and bool(node.checkpoints)


def _due_fresh_session(task_key, token, sid, runtime="claude") -> int | None:
    return _fresh_context_over(sid, runtime) if _declares_checkpoint(task_key, token) else None


def _refresh_request(task_key, phase, name, scope, writer_sid) -> int | None:
    if scope != "checkpoint":
        return None
    snap = _load_snapshot_for(task_key)
    if snap is None:
        return None
    caller, _, active = _caller_for(writer_sid)
    try:
        fold_ = _fold_for(task_key)
    except (OSError, KeyError, ValueError, TypeError):
        return None
    token = _held_token(fold_, caller.assignment_id)
    node = snap.nodes.get(token.node) if token is not None and fold_.closed is None else None
    if node is None or f"{phase}.{name}" not in {pipeline._expand(c, token.index) for c in node.checkpoints}:
        return None
    tokens = _fresh_context_over(writer_sid, active.get("runtime") or "claude")
    if tokens is None:
        return None
    with _token_lock(task_key):
        if not any(e.get("type") == "refresh_requested" and e.get("assignment_id") == token.holder
                   for e in _read_events(paths.artifact_events_path(task_key))):
            _append_task_event(task_key, {"type": "refresh_requested", "token_id": token.token_id,
                                          "assignment_id": token.holder, "sid": writer_sid, "tokens": tokens})
    return tokens


def _scope_warnings(task_key, phase, name, scope) -> list[str]:
    if scope == "output":
        return []
    snap = _load_snapshot_for(task_key)
    if snap is None:
        return []
    try:
        tokens = list(_fold_for(task_key).tokens.values())
    except (OSError, KeyError, ValueError, TypeError):
        return []
    full = f"{phase}.{name}"
    for token in tokens:
        node = snap.nodes.get(token.node)
        if node is not None and full in {pipeline._expand(o, token.index) for o in node.outputs}:
            return [f"{full} is a declared output of node {token.node}; it was put with scope={scope!r} and will "
                    "block pipeline_advance until re-put with scope=\"output\""]
    return []


def _load_snapshot_for(task_key):
    try:
        return pipeline.load_snapshot((paths.artifact_ticket_dir(task_key) / "schema.toml").read_text())
    except (OSError, ValueError):
        return None


def _fold_for(task_key):
    return pipeline.fold(_read_events(paths.artifact_events_path(task_key)))


def _held_token(fold_, assignment_id):
    if not assignment_id:
        return None
    return next((t for t in fold_.tokens.values() if t.holder == assignment_id), None)


def _has_open_token(task_key) -> bool:
    snap = _load_snapshot_for(task_key)
    if snap is None:
        return False
    try:
        fold_ = _fold_for(task_key)
    except (OSError, KeyError, ValueError, TypeError):
        return True
    token = fold_.tokens.get(pipeline.DEFAULT_TOKEN_ID)
    return token is not None and fold_.closed is None and any(e.frm == token.node for e in snap.edges)


def _open_token_task_keys() -> set[str]:
    if not paths.ARTIFACTS.is_dir():
        return set()
    return {d.name for d in paths.ARTIFACTS.iterdir() if d.is_dir() and _has_open_token(d.name)}


def _positions_by_holder(events) -> dict[str, tuple[str, int]]:
    out: dict[str, tuple[str, int]] = {}
    for e in events:
        holder = e.get("holder")
        if e.get("type") in ("pipeline_started", "advance_committed") and isinstance(holder, str) and holder:
            out[holder] = (e.get("node"), e.get("index", 1))
    return out


def _stamp_view(stamp: dict) -> dict:
    return {**stamp, "scope": stamp.get("scope") or "internal"}


def artifact_get_impl(task_key, phase, name, reader_sid=None) -> dict:
    path = paths.artifact_path(task_key, phase, name)
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise ValueError(f"no artifact {phase}.{name} for {task_key}") from None
    stamp, body = state.parse_artifact(text)
    if reader_sid:
        state.append_event(paths.artifact_events_path(task_key), {
            "type": "artifact_read", "phase": phase, "name": name, "actor_sid": reader_sid})
    return {**_stamp_view(stamp), "content": body, "path": str(path)}


def _prune_stale_artifacts() -> None:
    if not paths.ARTIFACTS.is_dir():
        return
    now = time.time()
    cutoff = now - paths.ARTIFACT_RETENTION_DAYS * 86400
    for d in paths.ARTIFACTS.iterdir():
        if not d.is_dir():
            continue
        newest = 0.0
        try:
            for p in d.rglob("*"):
                try:
                    newest = max(newest, p.stat().st_mtime)
                except OSError:
                    continue
        except OSError:
            continue
        if newest < cutoff and not _has_open_token(d.name) and not topic.keeps(d.name):
            shutil.rmtree(d, ignore_errors=True)
    try:
        for tmp in paths.ARTIFACTS.rglob("*.tmp"):
            try:
                if tmp.stat().st_mtime < now - 3600:
                    tmp.unlink(missing_ok=True)
            except OSError:
                continue
    except OSError:
        pass


def artifact_list_impl(task_key) -> list[dict]:
    _prune_stale_artifacts()
    return _list_artifacts(task_key)


def _list_artifacts(task_key) -> list[dict]:
    d = paths.artifact_ticket_dir(task_key)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        try:
            stamp, _ = state.parse_artifact(p.read_text())
        except ValueError:
            continue
        out.append({**_stamp_view(stamp), "path": str(p)})
    out.sort(key=lambda s: (s.get("phase", ""), s.get("name", "")))
    return out


def _iso(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).isoformat(sep=" ", timespec="seconds")
    except (TypeError, ValueError, OSError):
        return "-"


def _read_events(events_path) -> list[dict]:
    if not events_path.is_file():
        return []
    out = []
    for line in events_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def _join_worker_liveness(sid) -> tuple[str, str]:
    record = state.read_json(paths.ACTIVE / f"{sid}.json")
    if record:
        return "active", record.get("runtime") or "claude"
    closed_runtime = None
    for r in state.list_json_in(paths.CLOSED):
        if r.get("claude_sid") == sid:
            closed_runtime = r.get("runtime") or "claude"
            break
    if paths.DONE.is_dir():
        for p in paths.DONE.rglob("*.json"):
            ev = state.read_json(p)
            if ev and ev.get("claude_sid") == sid:
                return "done", closed_runtime or "claude"
    if closed_runtime is not None:
        return "closed", closed_runtime
    return "unknown", "claude"


def _brief_of(assignment) -> str | None:
    if not assignment:
        return None
    prompt = assignment.get("initial_prompt")
    if not prompt:
        return None
    return " ".join(str(prompt).split())[:200]


def _assignment_brief_for_sid(sid) -> str | None:
    if not sid:
        return None
    return _brief_of(state.read_json(paths.assignment_path(sid)))


def _assignments_for_ticket(ticket: str) -> list[dict]:
    if not paths.ASSIGNMENTS.is_dir():
        return []
    out = []
    for p in paths.ASSIGNMENTS.glob("*.json"):
        record = state.read_json(p)
        if record and record.get("ticket") == ticket:
            out.append(record)
    out.sort(key=lambda r: r.get("spawned_at") or 0)
    return out


STATUS_FORMATS = ("text", "json")
_NO_HOLDER = {"sid": None, "active": {}, "pid": None, "alive": False, "runtime": "claude", "reported": False}


def _active_record(sid) -> dict:
    active = state.read_json(paths.ACTIVE / f"{sid}.json")
    return active if isinstance(active, dict) else {}


def _session_model_requests(sid) -> int | None:
    if not sid:
        return None
    for record_path in (paths.ACTIVE / f"{sid}.json", paths.CLOSED / f"{sid}.json"):
        record = state.read_json(record_path)
        if not isinstance(record, dict):
            continue
        spend = record.get("spend")
        count = spend.get("model_requests") if isinstance(spend, dict) else None
        return count if isinstance(count, int) and not isinstance(count, bool) else None
    return None


def _record_pid(active) -> int | None:
    pid = active.get("pid")
    return pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else None


def _record_alive(sid, active, index) -> bool:
    pid = _record_pid(active)
    return _session_alive(sid) and (pid is None or index is None
                                    or _looks_like_session(index["command_by_pid"].get(pid, "")))


def _session_state(sid, index, runtime="claude") -> dict:
    active = _active_record(sid)
    return {"sid": sid, "active": active, "pid": _record_pid(active), "alive": _record_alive(sid, active, index),
            "runtime": active.get("runtime") or runtime,
            "reported": bool(active) and _reported_since_input(active, sid, root=paths.ROOT)}


def _holder_state(holder, index, records=None) -> dict:
    record = _holder_record(holder, records) or {}
    sid = record.get("claude_sid") or None
    if not sid:
        return dict(_NO_HOLDER)
    return _session_state(sid, index, record.get("runtime") or "claude")


def _holder_limit_banner(sid, runtime, processing, now, errors) -> str | None:
    try:
        log = find_session_log(sid, runtime)
        quiet = now - log.stat().st_mtime if processing and log is not None else None
    except Exception:
        log = None
    if log is None:
        errors.append(f"transcript:{sid[:8]}")
        return None
    if processing and quiet < RATE_LIMIT_NUDGE_SEC:
        return None
    banner = _limit_banner_text(log, strict=True)
    return banner if banner and any(s in banner.lower() for s in RATE_LIMIT_SIGNATURES) else None


def _session_waits(session, index, errors) -> tuple[bool, bool]:
    sid, active = session["sid"], session["active"]
    claude = session["alive"] and session["runtime"] == "claude"
    approval = gate = False
    window = state.window_id_of(active)
    if claude and active.get("state") == "processing" and window:
        marker_open = _open_prompt(active, time.time(), prompts_dir=paths.PERMISSION_PROMPTS) is not None
        try:
            screen = _capture_plain(window)
        except Exception:
            screen = None
        if screen is None:
            errors.append(f"pane:{sid[:8]}")
        else:
            approval = _dialog_on_screen(screen, marker_open)
    if claude and session["pid"] is not None and index is not None:
        parents = index.get("parent_by_pid") or {}
        gate = any(waiting_on.is_gate_launch(command) and waiting_on.descends_from(pid, session["pid"], parents)
                   for pid, command in index["command_by_pid"].items())
    return approval, gate


def _process_command(pid, index=None) -> str:
    if index is not None:
        return str((index.get("command_by_pid") or {}).get(pid, ""))
    try:
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _verdict_round(name) -> int:
    digits = name.removeprefix("decision.verdict-r")
    return int(digits) if digits.isdigit() else 0


def _relaunches(out) -> list[str]:
    log = Path(out).with_suffix(".log") if isinstance(out, str) and out.endswith(".out") else None
    try:
        text = log.read_text(errors="replace") if log else ""
    except OSError:
        return []
    return [line for line in text.splitlines() if line.startswith("relaunched ")]


def _gate_state(task_key, stamps=None, index=None) -> dict | None:
    if stamps is None:
        stamps = {f"{a.get('phase')}.{a.get('name')}": a for a in _list_artifacts(task_key)}
    gate = stamps.get("gate.open")
    if gate is None:
        return None
    try:
        info = json.loads(artifact_get_impl(task_key, "gate", "open")["content"])
    except (OSError, ValueError, KeyError, TypeError):
        info = {}
    info = info if isinstance(info, dict) else {}
    pid, out, round_ = info.get("pid"), info.get("out"), info.get("round")
    opened = float(gate.get("written_at") or 0)
    verdicts = [n for n in stamps if n.startswith("decision.verdict-r")]
    if any(float(stamps[n].get("written_at") or 0) >= opened for n in verdicts):
        return None
    numbered = isinstance(round_, int) and not isinstance(round_, bool)
    if numbered and any(_verdict_round(n) >= round_ for n in verdicts):
        return None
    alive = (isinstance(pid, int) and not isinstance(pid, bool) and isinstance(out, str) and bool(out)
             and state._pid_alive(pid) and out in _process_command(pid, index))
    return {"state": "open" if alive else "lost", "url": info.get("url"), "node": info.get("node"),
            "round": round_, "pid": pid, "out": out, "relaunches": _relaunches(out)}


def _gate_at(gate, node, gate_state) -> bool:
    return bool(gate) and gate["state"] == gate_state and gate.get("node") in (None, node)


def _author_wait_at(gate, author_waits, task_key, node) -> bool:
    return (task_key, node) in author_waits and not _gate_at(gate, node, "lost")


def _gate_line(gate) -> str:
    line = f"gate: {gate['state']} r{gate.get('round')} {gate.get('node') or '?'} {gate.get('url') or '-'}"
    relaunches = gate.get("relaunches") or []
    if relaunches:
        last = (relaunches[-1].split() + ["?", "?"])[1]
        line += f" · relaunched {len(relaunches)}× (last {last})"
    return line


def _position_facts(task_key, snap, token, holder, stamps, questions, index, tracker_keys, now, errors,
                    store_gate=None, author_waits=frozenset(), started_at=None):
    sid, active = holder["sid"], holder["active"]
    claude = holder["alive"] and holder["runtime"] == "claude"
    processing = active.get("state") == "processing"
    limited = False
    reset_at = None
    approval, gate = _session_waits(holder, index, errors)
    if claude and not holder["reported"]:
        banner = _holder_limit_banner(sid, holder["runtime"], processing, now, errors)
        if banner:
            limited, reset_at = True, _parse_limit_reset_ts(banner, int(now))
    node = snap.nodes.get(token.node)
    finished = node is not None and not any(e.frm == token.node for e in snap.edges) and all(
        (stamps.get(pipeline._expand(o, token.index)) or {}).get("status") == "complete" for o in node.outputs)
    facts = waiting_on.PositionFacts(
        node=token.node, question=bool(sid) and any(q["worker_sid"] == sid for q in questions),
        alive=holder["alive"], approval_dialog=approval, gate=gate, limited=limited, reported=holder["reported"],
        lease_wait=any(w["node"] == token.node for w in lease_waits(task_key)),
        merge_wait=bool(_merge_wait_urls(task_key, snap, token, stamps)),
        tracker_watch=task_key in tracker_keys or _author_wait_at(store_gate, author_waits, task_key, token.node),
        finished=finished, gate_open=_gate_at(store_gate, token.node, "open"), started_at=started_at,
        event_subs=tuple(sorted({s.source for s in event_store.subs_for(task_key, node=token.node)})))
    return facts, reset_at


def _token_started_at(events) -> dict[str, float]:
    out: dict[str, float] = {}
    requests: dict[str, dict] = {}
    for e in events:
        if e.get("type") == "advance_requested" and e.get("request_id"):
            requests[e["request_id"]] = e
        elif e.get("type") in ("pipeline_started", "advance_committed") and isinstance(e.get("ts"), (int, float)):
            out[pipeline._token_of(e, requests)] = float(e["ts"])
    return out


_PROBE = object()


def _waiting_on_status(task_key, snap, events, stamps, now, index=_PROBE, assignments=None) -> dict:
    questions = _pending_questions(task_key)
    index = _process_index() if index is _PROBE else index
    errors = [] if index is not None else ["ps"]
    status = {"fold": None, "fold_error": None, "questions": questions, "probe_errors": errors, "index": index,
              "holders": {}, "tokens": {}, "requests": {}, "reset_at": {}, "gate": None,
              "waiting_on": waiting_on.work_value([], bool(questions))}
    if snap is None:
        return status
    status["gate"] = gate = _gate_state(task_key, stamps, index)
    try:
        fold_ = pipeline.fold(events)
    except (KeyError, ValueError, TypeError) as err:
        status["fold_error"] = err
        errors.append("fold")
        return status
    tracker_keys, author_waits = waiting_on.process_waits(index)
    holders = {tid: _holder_state(t.holder, index, assignments) for tid, t in fold_.tokens.items() if t.holder}
    reset_at: dict = {}
    started = _token_started_at(events)

    def value_of(token) -> str:
        facts, reset_at[token.token_id] = _position_facts(
            task_key, snap, token, holders.get(token.token_id, _NO_HOLDER), stamps, questions, index, tracker_keys,
            now, errors, gate, author_waits, started_at=started.get(token.token_id))
        return waiting_on.token_value(facts, now)

    work, tokens, requests = waiting_on.evaluate(fold_, value_of, bool(questions), now=now,
                                                 started_at_of=lambda t: started.get(t.token_id))
    status.update(fold=fold_, holders=holders, tokens=tokens, requests=requests, reset_at=reset_at,
                  waiting_on=work)
    return status


def _waiting_on_line(status) -> str:
    errors = status["probe_errors"]
    return f"waiting_on: {status['waiting_on']}" + (f" (probe failed: {', '.join(errors)})" if errors else "")


def _topic_view(task_key, waiting_on) -> dict | None:
    card = topic.load(task_key)
    if card is None:
        return None
    return {**card, "priority_shown": topic.shown_priority(card.get("priority"), waiting_on)}


def _topic_line(view) -> str:
    if view is None:
        return "topic: none"
    priority, shown = view.get("priority"), view["priority_shown"]
    return (f"topic: {view.get('kind') or '?'} · {view.get('domain') or '?'} · {priority or '?'}"
            + (f" (shown {shown})" if shown != priority else "") + f" · {view.get('title') or ''}")


def _assignment_model(record: dict) -> str | None:
    if isinstance(record.get("model"), str) and record["model"]:
        return record["model"]
    args = record.get("spawn_extra_args")
    if not isinstance(args, list):
        return None
    for i, arg in enumerate(args):
        if not isinstance(arg, str):
            continue
        if arg in models.MODEL_OPTIONS and i + 1 < len(args) and isinstance(args[i + 1], str):
            return args[i + 1]
        option = next((o for o in models.MODEL_OPTIONS if arg.startswith(f"{o}=")), None)
        if option is not None:
            return arg[len(option) + 1:]
    return None


def _status_json(task_key, status, view, artifacts, assignments, events, liveness, boundary) -> dict:
    f = status["fold"]
    tokens, requests = [], []
    if f is not None:
        for tid, t in f.tokens.items():
            holder = status["holders"].get(tid) or {}
            tokens.append({"token_id": tid, "node": t.node, "index": t.index, "holder": t.holder,
                           "holder_sid": holder.get("sid"), "holder_alive": holder.get("alive"),
                           "holder_reported": holder.get("reported"), "row": t.row, "forked": t.forked,
                           "held": f.held.get(tid), "waiting_on": status["tokens"].get(tid),
                           "reset_at": status["reset_at"].get(tid)})
        for r in f.open_requests:
            edge, pend = r.get("edge") or {}, r.get("pending") or {}
            requests.append({"request_id": r.get("request_id"), "token_id": r.get("token_id"),
                             "from": edge.get("from"), "to": edge.get("to"), "kind": r.get("kind"),
                             "pending_reason": pend.get("reason"), "assignment_id": pend.get("assignment_id"),
                             "waiting_on": status["requests"].get(r.get("request_id"))})
    rows = []
    for a in artifacts:
        row = {"phase": a.get("phase"), "name": a.get("name"), "status": a.get("status"),
               "scope": a.get("scope") or "internal", "writer_sid": a.get("writer_sid"),
               "writer_liveness": liveness(a.get("writer_sid") or ""), "written_at": a.get("written_at"),
               "read_count": len(a.get("read_set") or []),
               "carried": boundary is not None and (a.get("written_at") or 0) < boundary}
        if a.get("phase") == "decision":
            row["decided_by"] = a.get("decided_by")
        rows.append(row)
    return {"version": 1, "task_key": task_key, "waiting_on": status["waiting_on"], "topic": view,
            "gate": status["gate"], "probe_errors": list(status["probe_errors"]),
            "round": f.round if f is not None else 1,
            "closed": f.closed if f is not None else None,
            "carried_prs": pipeline.redo_carried_prs(f.round_event) if f is not None else [], "tokens": tokens, "open_requests": requests, "questions": status["questions"], "artifacts": rows,
            "assignments": [{"name": s.get("name"), "assignment_id": s.get("assignment_id"),
                             "claude_sid": s.get("claude_sid"), "liveness": liveness(s.get("claude_sid") or ""),
                             "model_requests": _session_model_requests(s.get("claude_sid") or ""),
                             "branch": s.get("branch"), "role": s.get("role"),
                             "context_role": s.get("context_role"), "model": _assignment_model(s)}
                            for s in assignments],
            "events": events}


def pipeline_status_impl(task_key, format="text") -> str:
    if format not in STATUS_FORMATS:
        raise ValueError(f"pipeline_status: unknown format {format!r}; use one of {', '.join(STATUS_FORMATS)}")
    artifacts = _list_artifacts(task_key)
    events = _read_events(paths.artifact_events_path(task_key))
    started = _round_started_event(events)
    boundary = (started or {}).get("ts")
    assignments = _assignments_for_ticket(task_key)
    snap = _load_snapshot_for(task_key)
    stamps = {f"{a.get('phase')}.{a.get('name')}": a for a in artifacts}
    status = _waiting_on_status(task_key, snap, events, stamps, time.time())
    view = _topic_view(task_key, status["waiting_on"])
    liveness_memo: dict = {}

    def _liveness(sid) -> str:
        if sid not in liveness_memo:
            label = _join_worker_liveness(sid)[0]
            if label == "active" and not _record_alive(sid, _active_record(sid), status["index"]):
                label = "dead"
            liveness_memo[sid] = label
        return liveness_memo[sid]

    if format == "json":
        return json.dumps(_status_json(task_key, status, view, artifacts, assignments, events, _liveness, boundary),
                          ensure_ascii=False)
    lines = [f"# pipeline_status({task_key})", _waiting_on_line(status), _topic_line(view),
             *([_gate_line(status["gate"])] if status["gate"] else []), ""]
    lines.append("## artifacts (directory-as-index)")
    for a in artifacts:
        sid = a.get("writer_sid") or ""
        decided = f"  decided_by={a.get('decided_by') or '?'}" if a.get("phase") == "decision" else ""
        lines.append(f"- {a.get('phase', '?')}.{a.get('name', '?')}  [{a.get('status', '?')}]  "
                     f"writer={sid[:8]}({_liveness(sid)})  "
                     f"@{_iso(a.get('written_at'))}  read={len(a.get('read_set') or [])} input(s)" + decided
                     + ("  carried" if boundary is not None and (a.get("written_at") or 0) < boundary else ""))
    if snap is not None:
        from .pipeline_child import children_lines
        lines.extend(_token_lines(status))
        lines.extend(children_lines(task_key))
        lines.extend(_reads_lines(task_key, snap, artifacts, events, assignments))
        lines.extend(_rounds_lines(task_key, events))
    lines.append("\n## assignments (ownership plane)")
    for s in assignments:
        sid = s.get("claude_sid") or ""
        count = _session_model_requests(sid)
        lines.append(f"- {s.get('name')}  sid={sid[:8]}({_liveness(sid)})  "
                     f"model_requests={'?' if count is None else count}  "
                     f"role={s.get('context_role') or '-'}/{s.get('role') or '-'}  "
                     f"model={_assignment_model(s) or '-'}  "
                     f"branch={s.get('branch')}  brief={_brief_of(s)!r}")
    lines.append("\n## events (events.jsonl, chronological)")
    for e in events:
        what = (lane_lines.label(e.get("kind"), e.get("fields")) if e.get("type") == "lane_event"
                else f"{e.get('phase', '')}.{e.get('name', '')}")
        lines.append(f"- {_iso(e.get('ts'))} {e.get('type')} {what} "
                     f"{('— ' + str(e['reason'])) if e.get('reason') else ''}")
    return "\n".join(lines)


def _token_lines(status) -> list[str]:
    lines = ["\n## token"]
    f = status["fold"]
    if f is None:
        return [*lines, f"- fold failed: {status['fold_error']!r}"]
    if f.closed is not None:
        lines.append(f"- closed: {_iso(f.closed.get('ts'))} on {f.closed.get('node')} — "
                     f"{' '.join(str(f.closed.get('reason') or '').split())}")
    for tid, t in f.tokens.items():
        extra = f" waiting_on={status['tokens'].get(tid) or '-'}"
        extra += (f" row={t.row}" if t.row else "") + (" forked" if t.forked else "")
        extra += f" held: {' '.join(f.held[tid].split())}" if tid in f.held else ""
        lines.append(f"- {tid}: node={t.node} index={t.index} holder={t.holder} cwd={t.cwd} manager={t.manager}{extra}")
    for r in f.open_requests:
        pend = r.get("pending") or {}
        edge = r.get("edge") or {}
        token = f"token={r['token_id']} " if r.get("token_id") != pipeline.DEFAULT_TOKEN_ID else ""
        lines.append(f"- open request {r.get('request_id')} {token}{edge.get('from')}->{edge.get('to')} "
                     f"kind={r.get('kind')} pending={pend.get('reason')} assignment={pend.get('assignment_id')}"
                     f" waiting_on={status['requests'].get(r.get('request_id'))}"
                     + (f" cwd={r['cwd']}" if r.get("cwd") else ""))
    if f.round > 1:
        ev = f.round_event or {}
        if pipeline.redo_round(ev):
            lines.append(f"- round {f.round}: redo from {ev.get('from')} at {ev.get('to')} since {_iso(ev.get('ts'))}, "
                         f"ask {ev.get('ask')}, carried_prs {', '.join(pipeline.redo_carried_prs(ev)) or '(none)'}")
        else:
            lines.append(f"- round {f.round}: rework at {ev.get('to')} since {_iso(ev.get('ts'))}, ask {ev.get('ask')}, "
                         f"inputs {', '.join(ev.get('inputs') or []) or '(none)'}")
    return lines


def _rounds_lines(task_key, events) -> list[str]:
    base = paths.artifact_ticket_dir(task_key) / "rounds"
    if not base.is_dir():
        return []
    opened = {str(e.get("round")): e for e in events if e.get("type") == "round_started"}
    lines = ["\n## rounds"]
    for d in sorted((p for p in base.iterdir() if p.is_dir() and p.name.isdigit()), key=lambda p: int(p.name)):
        inputs = (opened.get(d.name) or {}).get("inputs") or []
        lines.append(f"- round {d.name}: {len(list(d.glob('*.md')))} archived artifact(s) in {d}; "
                     f"inputs {', '.join(inputs) or '(none)'}")
    return lines


def _read_set_label(task_key, entry: dict) -> str:
    name = f"{entry.get('phase', '')}.{entry.get('name', '')}" if "phase" in entry else str(entry.get("name"))
    other = entry.get("task_key")
    return f"{other}:{name}" if other and other != task_key else name


def _position_of(assignment, positions, by_sid):
    seen: set[str] = set()
    while assignment is not None:
        position = positions.get(assignment.get("assignment_id"))
        if position is not None:
            return position
        seen.add(assignment.get("claude_sid"))
        parent = assignment.get("parent_sid")
        if not isinstance(parent, str) or parent in seen:
            return None
        assignment = by_sid.get(parent)
    return None


def _task_indexes(facts) -> list[int]:
    return list(range(1, len(facts.plan_tasks or []) + 1))


def _allowed_reads(snap, position, indexes) -> set[str]:
    node = snap.nodes.get(position[0]) if position else None
    if node is None:
        return set()
    return {*pipeline.expand_inputs(node, position[1], indexes),
            *(pipeline._expand(n, position[1]) for n in (*node.outputs, *node.checkpoints))}


def _reads_lines(task_key, snap, artifacts, events, assignments) -> list[str]:
    reads_by_sid: dict[str, set[str]] = {}
    started_at = max((i for i, e in enumerate(events) if e.get("type") == "round_started"), default=-1)
    started = events[started_at] if started_at >= 0 else {}
    boundary = started.get("ts")
    round_inputs = set(started.get("inputs") or [])
    for a in artifacts:
        if boundary is not None and (a.get("written_at") or 0) < boundary:
            continue
        for entry in a.get("read_set") or []:
            if isinstance(entry, dict):
                reads_by_sid.setdefault(a.get("writer_sid") or "", set()).add(_read_set_label(task_key, entry))
    own_reads_by_sid: dict[str, set[str]] = {}
    positions = _positions_by_holder(events)
    sid_by_assignment = {a["assignment_id"]: a["claude_sid"] for a in assignments
                         if a.get("assignment_id") in positions and a.get("claude_sid")}
    for e in events[started_at + 1:]:
        if e.get("type") == "artifact_read":
            label = f"{e.get('phase')}.{e.get('name')}"
            reads_by_sid.setdefault(e.get("actor_sid") or "", set()).add(label)
            own_reads_by_sid.setdefault(e.get("actor_sid") or "", set()).add(label)
        elif e.get("type") == "inputs_handed":
            sid = sid_by_assignment.get(e.get("assignment_id"))
            if not sid:
                continue
            for label in (n for n in e.get("names") or [] if isinstance(n, str)):
                reads_by_sid.setdefault(sid, set()).add(label)
                own_reads_by_sid.setdefault(sid, set()).add(label)
    by_sid ={a.get("claude_sid"): a for a in assignments if a.get("claude_sid")}
    indexes = _task_indexes(_facts_for(task_key))
    rulings = _domain_rulings(task_key)
    ruling_reads = set() if rulings is None else {f"{topic.DECISIONS_PHASE}.{rulings[1]}",
                                                  f"{rulings[0]}:{topic.DECISIONS_PHASE}.{rulings[1]}"}

    def _flag(name, allowed, sid) -> str:
        exempt = (name.startswith(("decision.", "context.rework-")) or name in round_inputs
                  or name == pipeline.SUMMARY_THREAD
                  or (name in ruling_reads and name not in own_reads_by_sid.get(sid, ())))
        return "" if name in allowed or exempt else "  OUTSIDE INPUTS"

    lines = ["\n## reads"]
    attributed = {""}
    for s in sorted(assignments, key=lambda r: (r.get("spawned_at") or 0, str(r.get("name")), r.get("claude_sid") or "")):
        sid = s.get("claude_sid") or ""
        if sid in attributed:
            continue
        attributed.add(sid)
        position = _position_of(s, positions, by_sid)
        node = position[0] if position else None
        allowed = _allowed_reads(snap, position, indexes)
        for name in sorted(reads_by_sid.get(sid, ())):
            lines.append(f"- {s.get('name')} ({node or '?'}) read {name}{_flag(name, allowed, sid)}")
    for sid in sorted(set(reads_by_sid) - attributed):
        for name in sorted(reads_by_sid[sid]):
            lines.append(f"- sid={sid[:8]} (?) read {name}{_flag(name, set(), sid)}")
    for name in sorted(reads_by_sid.get("", ())):
        lines.append(f"- unattributed read {name}")
    return lines


def artifact_view_impl(task_key) -> str:
    out = [f"# artifact_view({task_key})"]
    for a in artifact_list_impl(task_key):
        phase, name = a.get("phase"), a.get("name")
        if not phase or not name:
            stem_parts = Path(a["path"]).stem.split(".", 1)
            if len(stem_parts) == 2:
                phase, name = phase or stem_parts[0], name or stem_parts[1]
        try:
            full = artifact_get_impl(task_key, phase, name)
        except ValueError:
            continue
        excerpt = full["content"][:1200]
        out += [f"\n## {phase}.{name}  [{a.get('status', '?')}]",
                f"writer_sid={a.get('writer_sid')}  contract_hash={a.get('contract_hash')}  written_at={_iso(a.get('written_at'))}",
                f"read_set={a.get('read_set')}", "", excerpt,
                ("…[truncated]" if len(full["content"]) > 1200 else "")]
    return "\n".join(out)


def _validate_task_key(task_key: str | None) -> None:
    if task_key is None:
        return
    if not task_key.strip():
        raise ValueError("task_key must not be blank; omit it entirely for an unkeyed spawn")
    if paths._safe_segment(task_key) != task_key:
        raise ValueError(
            f"task_key {task_key!r} is not a stable slug; use only [A-Za-z0-9_-] so the "
            "artifact dir and the assignment join key never diverge"
        )


def _unkeyed_key_hint(raw_name: str | None, raw_prompt: str) -> str | None:
    pattern = config.task_key_regex()
    if not pattern:
        return None
    try:
        key_re = re.compile(rf"\b({pattern})\b")
    except re.error:
        return None
    m = key_re.search(raw_prompt or "") or key_re.search(raw_name or "")
    if m is None:
        return None
    return (
        f"this spawn is UNKEYED; its prompt/name mentions a key-shaped token "
        f"({m.group(1)}). If this worker is actually ASSIGNED to that task, "
        "respawn or continue it with explicit task_key=; if the token is "
        "merely mentioned, ignore this hint — a mention is not an assignment."
    )


def _artifact_discipline_footer(task_key: str, agent_in_context: bool = False) -> str:
    if agent_in_context:
        return (
            "\n\n---\n"
            f"[orchestrator] Artifact discipline — task_key: `{task_key}`\n"
            f"Persist phase outputs with `artifact_put(task_key=\"{task_key}\", ..., "
            "writer_sid=<your sid>)` per your worker agent § \"Persist pipeline artifacts\"."
        )
    return (
        "\n\n---\n"
        f"[orchestrator] Artifact discipline — task_key: `{task_key}`\n"
        "Persist phase outputs to the artifact store as they stabilize, without "
        f"waiting to be asked: `artifact_put(task_key=\"{task_key}\", "
        "phase=<spec|plan|implement|review|summary>, name=<repo-or-scope>, "
        "content_path=<a file you wrote>, status=\"partial\"|\"complete\", writer_sid=<your sid>)`. "
        "Publish final outputs (a brief-directed full report included) with "
        "status=\"complete\"; flip any remaining partials before worker_done. "
        "A put that would replace another writer's record — or a "
        "hand-authored file with different content — or revise or "
        "demote your own complete record, or demote a finished hand-written "
        "report — is refused: publish under a different (phase, name), or "
        "pass overwrite=True only for a deliberate "
        "replacement (e.g. finishing a dead predecessor's phase). Store/publish "
        "failures are non-blocking — note them and continue; they must never fail "
        "your task. Long form: worker agent definition § \"Persist pipeline artifacts\"."
    )


def _repo_sync_footer() -> str:
    return (
        "\n\n---\n"
        "[orchestrator] Repo freshness — sync once, then read normally\n"
        "Before you read any repo on this machine to investigate it, sync it "
        "ONCE first — run each as its OWN single command (plain single "
        "commands stay headless-approvable; chained commands and path-scoped "
        "git forms trip the permission gate): `cd <repo>`, then `git fetch "
        "origin main`, then `git merge --ff-only origin/main` (if on main) or "
        "`git rebase origin/main` (feature branch). If it can't ff/rebase "
        "(local uncommitted changes / diverged — abort a conflicted rebase "
        "with `git rebase --abort`), do NOT silently read a stale tree — "
        "note it and read the specific files off `origin/main` "
        "(`git show origin/main:<path>`). Then use normal Grep/Read on the "
        "now-current tree."
    )


def _current_branch(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"],
            capture_output=True, timeout=2, check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.decode().strip() or None
    except Exception:
        return None


def _write_pending_assignment(assignment_id, name, raw_prompt, preset, cwd,
                              manager_sid, parent_manager_name, runtime,
                              ticket=None, spawn_extra_args=None,
                              parent_sid=None, **extra) -> None:
    paths.ensure_dirs()
    state.write_json_atomic(paths.pending_assignment_path(assignment_id), {
        "assignment_id": assignment_id,
        "requested_name": name,
        "name": name,
        "initial_prompt": raw_prompt,
        "preset": preset,
        "cwd": cwd,
        "branch": _current_branch(cwd),
        "manager_sid": manager_sid or None,
        "parent_manager_name": parent_manager_name,
        "parent_name": parent_manager_name,
        "parent_sid": parent_sid or None,
        "runtime": runtime,
        "ticket": ticket,
        "spawn_extra_args": spawn_extra_args,
        "spawned_at": time.time(),
        **extra,
    })


def _prune_stale_assignments() -> None:
    now = time.time()
    if paths.ASSIGNMENTS_PENDING.is_dir():
        for p in paths.ASSIGNMENTS_PENDING.glob("*.json"):
            try:
                if p.stat().st_mtime < now - paths.PENDING_ASSIGNMENT_TTL_SEC:
                    p.unlink(missing_ok=True)
            except OSError:
                continue
        for p in paths.ASSIGNMENTS_PENDING.glob("*.window"):
            try:
                if p.stat().st_mtime < now - paths.PENDING_ASSIGNMENT_TTL_SEC:
                    p.unlink(missing_ok=True)
            except OSError:
                continue
    cutoff = now - paths.ASSIGNMENT_RETENTION_DAYS * 86400
    for p in paths.PROMPT_FILES.glob("*.txt"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
        except OSError:
            continue
    if not paths.ASSIGNMENTS.is_dir():
        return
    for p in paths.ASSIGNMENTS.glob("*.json"):
        record = state.read_json(p) or {}
        sid = record.get("claude_sid") or p.stem
        if (paths.ACTIVE / f"{sid}.json").exists():
            continue
        try:
            if p.stat().st_mtime < cutoff and not (record.get("ticket") and _has_open_token(record["ticket"])):
                p.unlink(missing_ok=True)
        except OSError:
            continue


def pipeline_event_impl(task_key, type, phase=None, name=None, reason=None, actor_sid=None) -> dict:
    if type in pipeline.RESERVED_EVENT_TYPES:
        raise ValueError(f"pipeline_event refuses reserved type {type!r}; only the pipeline engine writes it")
    paths.ensure_dirs()
    event = {"type": type, "phase": phase, "name": name, "reason": reason, "actor_sid": actor_sid}
    state.append_event(paths.artifact_events_path(task_key),
                       {k: v for k, v in event.items() if v is not None})
    return {"ok": True}


def topic_set_impl(task_key, actor_sid, priority=None, title=None, domain=None, status=None, blocked_by=None,
                   links=None) -> dict:
    if not actor_sid:
        raise ValueError("topic_set requires actor_sid, your own session UUID")
    if not task_key:
        raise ValueError("topic_set requires task_key")
    _validate_task_key(task_key)
    if isinstance(blocked_by, list) and task_key in blocked_by:
        raise ValueError(f"topic_set: {task_key} cannot block itself; drop it from blocked_by")
    base = topic.load(task_key)
    if base is None:
        raise ValueError(f"topic_set: {task_key} has no topic card and no pipeline start to build one from")
    changed: list[str] = []

    def _apply(current):
        new, diff = topic.edit(current or base, priority=priority, title=title, domain=domain, status=status,
                               blocked_by=blocked_by, links=links)
        changed[:] = diff
        return new if diff or current is None else None

    card = topic.update(task_key, _apply)
    if changed:
        _append_task_event(task_key, {"type": "topic_set", "actor_sid": actor_sid, "changed": changed})
    return {"ok": True, "task_key": task_key, "changed": changed, "topic": card}


_after_fold_hook = None
_token_thread_locks: dict[str, threading.Lock] = {}
_token_thread_locks_guard = threading.Lock()
_EVENT_TEXT_FIELDS = ("reason", "evidence", "error")
_EVENT_TEXT_BUDGET = 1000
_EVENT_TEXT_CUT = "…[truncated]"
_RESTART_LINE = ("The node's previous session died; read the checkpoints listed above with artifact_get(..., "
                 "reader_sid=<your sid>), continue from them, and re-put your own checkpoints and outputs with "
                 "overwrite=True.")


@contextmanager
def _token_lock(task_key):
    lock_path = paths.artifact_ticket_dir(task_key) / ".token.lock"
    with _token_thread_locks_guard:
        thread_lock = _token_thread_locks.setdefault(str(lock_path), threading.Lock())
    with thread_lock:
        with state.file_lock(lock_path):
            yield


def _fit_event_text(value: str) -> str:
    if len(json.dumps(value)) <= _EVENT_TEXT_BUDGET:
        return value
    room = _EVENT_TEXT_BUDGET - (len(json.dumps(_EVENT_TEXT_CUT)) - 2)
    lo, hi = 0, len(value)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(json.dumps(value[:mid])) <= room:
            lo = mid
        else:
            hi = mid - 1
    return value[:lo] + _EVENT_TEXT_CUT


def _fit_task_event(event: dict) -> dict:
    fitted = {**event, "ts": event.get("ts") or time.time(), "event_id": event.get("event_id") or uuid.uuid4().hex[:8]}
    for key in _EVENT_TEXT_FIELDS:
        if isinstance(fitted.get(key), str):
            fitted[key] = _fit_event_text(fitted[key])
    size = len(state.event_line(fitted))
    if size > state.EVENT_LINE_MAX:
        raise ValueError(f"pipeline: a {fitted.get('type')} event needs {size} bytes, over the "
                         f"{state.EVENT_LINE_MAX}-byte journal line; nothing was written")
    return fitted


_CARD_MOVING_EVENTS = ("advance_committed", "token_override", "schema_snapshot")


def _append_task_event(task_key, event):
    state.append_event(paths.artifact_events_path(task_key), _fit_task_event(event))
    if (event.get("type") in _CARD_MOVING_EVENTS
            and (event.get("token_id") or pipeline.DEFAULT_TOKEN_ID) == pipeline.DEFAULT_TOKEN_ID):
        _sync_card(task_key)


def _engine_fold(task_key, tool):
    try:
        return _fold_for(task_key)
    except (KeyError, ValueError, TypeError) as err:
        raise ValueError(f"{tool}: events.jsonl for {task_key} is unreadable: {err!r}") from err


def _facts_for(task_key):
    stamps = {f"{a.get('phase')}.{a.get('name')}": a for a in artifact_list_impl(task_key)}
    plan = None
    if pipeline.REPEAT_LIST in stamps:
        phase, name = pipeline.REPEAT_LIST.split(".", 1)
        try:
            plan = pipeline.read_task_list(artifact_get_impl(task_key, phase, name)["content"])
        except ValueError:
            plan = []
    return pipeline.Facts(artifacts=stamps, plan_tasks=plan, command_runner=None,
                          read=lambda full: _artifact_body(task_key, full))


def _artifact_body(task_key, full):
    phase, _, name = full.partition(".")
    return artifact_get_impl(task_key, phase, name)["content"]


def _holder_committed_at(task_key, holder) -> float:
    at = 0.0
    for e in _read_events(paths.artifact_events_path(task_key)):
        if e.get("type") in ("advance_committed", "pipeline_started") and e.get("holder") == holder:
            at = float(e.get("ts") or 0)
    return at


def _merge_state_word(task_key, artifacts, token=None) -> str:
    stamp = artifacts.get(pipeline.MERGE_STATE)
    if stamp is None:
        return ""
    if token is not None and float(stamp.get("written_at") or 0) <= _holder_committed_at(task_key, token.holder):
        return ""
    first = (_artifact_body(task_key, pipeline.MERGE_STATE) or "").split("\n", 1)[0]
    return first.split(" ", 1)[0] if first else ""


def _caller_for(claude_sid):
    if claude_sid == DAEMON_SID:
        if _daemon_record() is None:
            return pipeline.Caller(None, None, "worker"), {}, {}
        return pipeline.Caller(None, None, "manager"), {}, {"agent": "manager", "name": DAEMON_SID}
    active = state.read_json(paths.ACTIVE / f"{claude_sid}.json") or {}
    try:
        assignment = state.read_json(paths.assignment_path(claude_sid)) or {}
    except ValueError:
        assignment = {}
    agent = "manager" if active.get("agent") == "manager" and not active.get("nested") else "worker"
    return pipeline.Caller(assignment.get("assignment_id"), assignment.get("ticket"), agent), assignment, active


def _manager_sid_for(task_key, caller_assignment) -> str | None:
    assignment = caller_assignment or {}
    sid = assignment.get("manager_sid")
    if isinstance(sid, str) and sid:
        if sid == DAEMON_SID:
            return DAEMON_SID if _daemon_record() else None
        record = state.read_json(paths.ACTIVE / f"{sid}.json") or {}
        if record.get("agent") == "manager" and not record.get("nested") and _pid_reads_alive(record.get("pid")):
            return sid
    live = _live_parent_sid(assignment.get("parent_name"))
    if live:
        return live
    try:
        token = _fold_for(task_key).tokens.get(pipeline.DEFAULT_TOKEN_ID)
    except (OSError, KeyError, ValueError, TypeError):
        return None
    return _live_parent_sid(token.manager) if token is not None else None


def _schema_snapshot_text(schema, cwd) -> str:
    try:
        safe = paths._safe_segment(schema) if isinstance(schema, str) else None
    except ValueError:
        safe = None
    if safe != schema:
        raise ValueError(f"schema {schema!r} is not a bare schema name; use only [A-Za-z0-9_-]")
    if schema in topic.TOKENLESS_KINDS:
        raise ValueError(f"schema {schema!r} names a tokenless topic kind ({', '.join(topic.TOKENLESS_KINDS)}); no "
                         "schema may take that name")
    path = config.pipeline_schemas_dir() / f"{schema}.toml"
    if not path.is_file():
        raise ValueError(f"no schema file at {path}")
    try:
        loaded = pipeline.load_schema(path)
        problems = pipeline.validate(loaded, cwd)
        text = None if problems else pipeline.snapshot(loaded)
    except (OSError, ValueError) as err:
        raise ValueError(f"schema {path} cannot be read: {err}") from err
    if problems:
        raise ValueError(f"schema {path} is invalid: " + "; ".join(problems))
    return text


def _started_schema_name(task_key, token_id, fallback):
    return pipeline.running_schema(_read_events(paths.artifact_events_path(task_key)), token_id, fallback)


def _write_snapshot(task_key, text) -> None:
    d = paths.artifact_ticket_dir(task_key)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".schema.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
    try:
        tmp.write_text(text)
        os.replace(tmp, d / "schema.toml")
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _refuse_advance(task_key, token_id, to, actor_sid, reason, message=None):
    try:
        _append_task_event(task_key, {"type": "advance_refused", "token_id": token_id, "reason": reason, "to": to,
                                      "actor_sid": actor_sid})
    except ValueError as err:
        reason = f"{reason} (not recorded: {err})"
    raise ValueError(message or f"pipeline_advance refused: {reason}")


def _moved_cwd(cwd, refuse) -> str | None:
    if cwd is None:
        return None
    if not (isinstance(cwd, str) and os.path.isabs(cwd) and os.path.isdir(os.path.normpath(cwd))):
        refuse(f"cwd {cwd!r} must be an absolute path to an existing directory")
    return os.path.normpath(cwd)


def _closed_text(task_key, closed) -> str:
    return (f"{task_key} was closed at {_iso(closed.get('ts'))} on {closed.get('node')}: "
            f"{' '.join(str(closed.get('reason') or '').split())}; pipeline_rework reopens it once its round PR is "
            "merged or closed")


def _advance_context_locked(claude_sid, task_key, to, token_id):
    tid = token_id or pipeline.DEFAULT_TOKEN_ID
    try:
        fold_ = _fold_for(task_key)
    except (KeyError, ValueError, TypeError) as err:
        message = f"pipeline_advance: events.jsonl for {task_key} is unreadable: {err!r}"
        _refuse_advance(task_key, tid, to, claude_sid, message, message)
    if _after_fold_hook is not None:
        _after_fold_hook()
    snap = _load_snapshot_for(task_key)
    token = fold_.tokens.get(tid)
    if snap is not None and token is None and "/" in tid:
        _refuse_advance(task_key, tid, to, claude_sid,
                        f"row token {tid} is not live (not started yet, retired, or removed by a rework); "
                        "read pipeline_status")
    if snap is None or token is None:
        _refuse_advance(task_key, tid, to, claude_sid,
                        f"{task_key} has no pipeline token {tid}; the manager starts a work with pipeline_start")
    if fold_.closed is not None:
        _refuse_advance(task_key, tid, to, claude_sid, _closed_text(task_key, fold_.closed))
    if not claude_sid:
        _refuse_advance(task_key, tid, to, claude_sid, "claude_sid must be your own session id")
    caller, assignment, active = _caller_for(claude_sid)
    if token_id is None and caller.agent != "manager" and caller.assignment_id:
        own = _held_token(fold_, caller.assignment_id)
        if own is not None:
            tid, token = own.token_id, own
    if _handed_off(claude_sid, task_key):
        _refuse_advance(task_key, tid, to, claude_sid, "node handed off; call worker_done")
    if caller.agent != "manager" and caller.ticket != task_key:
        _refuse_advance(task_key, tid, to, claude_sid,
                        f"session {claude_sid} holds no assignment on {task_key} (its assignment is keyed "
                        f"{caller.ticket!r})")
    return fold_, snap, token, tid, caller, assignment, active


def _request_advance_locked(claude_sid, task_key, to, reason=None, override=False, index=None, resnapshot=False,
                            token_id=None, cwd=None) -> dict:
    if not task_key:
        raise ValueError("pipeline_advance requires task_key")
    _validate_task_key(task_key)
    with _token_lock(task_key):
        fold_, snap, token, tid, caller, assignment, active = _advance_context_locked(claude_sid, task_key, to,
                                                                                      token_id)
        events: list[dict] = []
        node_stamp = ({"actor_assignment": caller.assignment_id}
                      if caller.agent != "manager" and caller.assignment_id else {})
        new_text = None
        back_to = None
        if to == "?" and caller.agent == "manager" and not override:
            if cwd is not None or index is not None or resnapshot:
                _refuse_advance(task_key, tid, to, claude_sid, "a hold or a release takes only a reason")
            for r in fold_.open_requests:
                if r["token_id"] == tid:
                    _refuse_advance(task_key, tid, to, claude_sid,
                                    f"advance in flight (request {r.get('request_id')}); wait for reconciliation")
            if token.forked:
                rows = ", ".join(sorted(k for k in fold_.tokens if k.startswith(f"{tid}/")))
                _refuse_advance(task_key, tid, to, claude_sid,
                                f"{tid} is forked: hold or release each row with token_id= one of {rows}" if rows
                                else f"{tid} is forked and no row token exists yet: once a row's spawn commits, hold "
                                     "or release it with its token_id (pipeline_status lists it)")
            if isinstance(reason, str) and reason.strip():
                event, outcome = {"type": "token_held", "reason": reason}, "held"
            elif tid in fold_.held:
                event, outcome = {"type": "token_released"}, "released"
            else:
                _refuse_advance(task_key, tid, to, claude_sid,
                                f"no hold to release on {tid}; a hold needs a reason, pipeline_status shows it")
            try:
                fitted = _fit_task_event({**event, "token_id": tid, "node": token.node, "actor_sid": claude_sid})
            except ValueError as err:
                _refuse_advance(task_key, tid, to, claude_sid, str(err))
            _append_task_event(task_key, fitted)
            return {"held": outcome, "token": token, "caller": caller}
        cwd = _moved_cwd(cwd, lambda reason: _refuse_advance(task_key, tid, to, claude_sid, reason))
        verdict = None
        if override:
            if caller.agent != "manager":
                _refuse_advance(task_key, tid, to, claude_sid, "override is manager-only; a node ends with worker_done")
            if any(t.row is not None for t in fold_.tokens.values()):
                source = pipeline.fork_source(snap, fold_.tokens[pipeline.DEFAULT_TOKEN_ID].node)
                _refuse_advance(task_key, tid, to, claude_sid,
                                "rows of a fork are live: resume a dead row (send_manager_to_worker with "
                                "auto_resume=true); to redo the fork, kill_worker every row and pipeline_rework at "
                                f"{source}")
            if not (isinstance(reason, str) and reason.strip()):
                _refuse_advance(task_key, tid, to, claude_sid, "override needs a reason; it is relayed to the user")
            if index is not None and (isinstance(index, bool) or not isinstance(index, int) or index < 1):
                _refuse_advance(task_key, tid, to, claude_sid, f"override index {index!r} must be an integer >= 1")
            if resnapshot:
                schema_name = _started_schema_name(task_key, tid, snap.name)
                try:
                    new_text = _schema_snapshot_text(schema_name, cwd if cwd is not None else token.cwd)
                except ValueError as err:
                    _refuse_advance(task_key, tid, to, claude_sid, f"resnapshot: {err}")
                snap = pipeline.load_snapshot(new_text)
            if to not in snap.nodes:
                _refuse_advance(task_key, tid, to, claude_sid,
                                f"override target {to!r} is not a node of the schema snapshot; "
                                f"its nodes are {', '.join(snap.nodes)}")
            start = pipeline.start_node(snap)
            if snap.rework_at is not None and to == start and _left_start(task_key, start):
                _refuse_advance(task_key, tid, to, claude_sid,
                                f"override into the start node {start} after it left: use pipeline_rework")
            problem = pipeline.fork_entry_problem(snap, to, _facts_for(task_key).plan_tasks)
            if problem:
                _refuse_advance(task_key, tid, to, claude_sid, problem)
            index_to = index or 1
            target = snap.nodes[to]
            last = pipeline.last_index(target, _facts_for(task_key))
            if target.repeat and index_to > last:
                _refuse_advance(task_key, tid, to, claude_sid,
                                f"override index {index_to} is past the {last} task(s) the engine read from "
                                f"{target.repeat}")
            if new_text is not None:
                events.append({"type": "schema_snapshot", "token_id": tid,
                               "schema_sha256": pipeline.schema_sha256(new_text), "schema": schema_name,
                               "reason": reason, "actor_sid": claude_sid})
            events.append({"type": "token_override", "token_id": tid, "from": token.node, "to": to,
                           "index": index_to, "reason": reason, "actor_sid": claude_sid,
                           "resnapshot": bool(resnapshot), "manager": active.get("name")})
        else:
            if index is not None or resnapshot:
                _refuse_advance(task_key, tid, to, claude_sid, "index and resnapshot apply only with override=True")
            verdict = pipeline.check_advance(snap, token, caller, to, _facts_for(task_key), fold_.open_requests,
                                             cwd=token.cwd,
                                             timeout_sec=config.pipeline_int("exit_check_timeout_sec", 600),
                                             reason=reason)
            if not verdict.ok:
                _refuse_advance(task_key, tid, to, claude_sid, verdict.reason)
            if verdict.edge.redo:
                _refuse_advance(task_key, tid, to, claude_sid,
                                f"{token.node} -> {to} is a redo edge: call pipeline_advance with only claude_sid, "
                                "task_key, to and reason")
            if verdict.edge.back:
                _refuse_advance(task_key, tid, to, claude_sid,
                                f"{token.node} -> {to} is a back edge: call pipeline_advance with only claude_sid, "
                                "task_key, to, row and reason")
            index_to = verdict.index_to
            if verdict.edge.trigger:
                events.append({"type": "trigger_fired", "token_id": tid,
                               "edge": {"from": token.node, "to": to, "trigger": verdict.edge.trigger},
                               "evidence": reason, "actor_sid": claude_sid})
            if verdict.edge.to != token.node and not verdict.edge.trigger:
                stay = _back_stay(_read_events(paths.artifact_events_path(task_key)), token, token.node)
                if stay is not None:
                    back_to = (stay["from"], int(stay.get("from_index") or 1))
        spawns: list[dict] = []
        returning = {"return": True} if back_to else {}

        def request(tok, to_node, idx, kind="advance", extra=None):
            rid = uuid.uuid4().hex[:12]
            ev = {"type": "advance_requested", "token_id": tok.token_id, "request_id": rid,
                  "edge": {"from": token.node, "to": to_node}, "index_to": idx, "kind": kind,
                  "actor_sid": claude_sid, **node_stamp}
            ev.update(extra or {})
            events.append(ev)
            spawns.append({"request_id": rid, "token": tok, "to": to_node, "index_to": idx})

        stamp_now = None
        facts = _facts_for(task_key)
        rows = pipeline.task_rows(facts.plan_tasks)
        edge = verdict.edge if verdict else None
        forking = edge is not None and edge.fork and len(rows) > 1
        joining = edge is not None and edge.join and token.row is not None
        if cwd is not None and (forking or joining or token.row is not None):
            _refuse_advance(task_key, tid, to, claude_sid,
                            "cwd moves a single token; a fork or join edge keeps the work's cwd, and each row spawns "
                            "in its plan.tasks entry's cwd")
        if forking:
            forked = [dict(r, token_id=pipeline.row_token_id(tid, r["row"])) for r in rows]
            events.append({"type": "token_forked", "token_id": tid, "node": to, "actor_sid": claude_sid,
                           "rows": [{"row": r["row"], "stage": r["stage"], "token_id": r["token_id"]} for r in forked]})
            first = min(r["stage"] for r in forked)
            for r in forked:
                if r["stage"] == first:
                    tok = pipeline.Token(r["token_id"], to, r["indexes"][0], None, token.cwd, token.manager, r["row"])
                    request(tok, to, r["indexes"][0], "fork", {"row": r["row"], "cwd": token.cwd})
            stamp_now = token.holder
        elif joining:
            events.append({"type": "token_retired", "token_id": tid, "actor_sid": claude_sid, **node_stamp})
            states = (fold_.fork or {}).get("rows") or {}
            stage = (states.get(token.row) or {}).get("stage", 1)
            live = [n for n, r in states.items() if n != token.row and r["stage"] == stage and r["state"] != "done"]
            pending = {n: r for n, r in states.items() if r["state"] == "pending"}
            parent = fold_.tokens[pipeline.DEFAULT_TOKEN_ID]
            if not live and pending:
                nxt = min(r["stage"] for r in pending.values())
                first_index = {r["row"]: r["indexes"][0] for r in rows}
                missing = next((n for n, r in pending.items() if r["stage"] == nxt and n not in first_index), None)
                if missing is not None:
                    _refuse_advance(task_key, tid, to, claude_sid,
                                    f"plan.tasks no longer lists row {missing!r} of the fork at {token.node}; "
                                    f"ask_manager to pipeline_rework at {pipeline.fork_source(snap, token.node)}")
                for n, r in pending.items():
                    if r["stage"] == nxt:
                        tok = pipeline.Token(r["token_id"], token.node, first_index[n], None, parent.cwd,
                                             parent.manager, n)
                        request(tok, token.node, first_index[n], "fork", {"row": n, "cwd": parent.cwd})
            elif not live:
                request(parent, *(back_to or (to, 1)), extra=returning)
            stamp_now = caller.assignment_id
        else:
            moved = dataclasses.replace(token, cwd=cwd) if cwd is not None else token
            request(moved, *(back_to or (to, index_to)), "advance",
                    {**({"cwd": cwd} if cwd is not None else {}), **returning})
        try:
            fitted = [_fit_task_event(e) for e in events]
        except ValueError as err:
            _refuse_advance(task_key, tid, to, claude_sid, str(err))
        if new_text is not None:
            _write_snapshot(task_key, new_text)
        for event in fitted:
            _append_task_event(task_key, event)
        if override and token.holder:
            _stamp_handed_off(task_key, tid, token.holder)
        if stamp_now:
            _stamp_handed_off(task_key, tid, stamp_now)
    first = spawns[0] if spawns else {}
    return {"request_id": first.get("request_id"), "token": first.get("token", token), "to": to,
            "index_to": first.get("index_to"), "caller": caller, "assignment": assignment, "snap": snap,
            "spawns": spawns, "caller_stamped": bool(stamp_now)}


def _rework_number(name) -> int:
    tail = name.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def _domain_rulings(task_key) -> tuple[str, str] | None:
    domain = (topic.load(task_key) or {}).get("domain")
    if not topic.is_key(domain):
        return None
    fleet = topic.fleet_key(domain)
    if not paths.artifact_path(fleet, topic.DECISIONS_PHASE, domain).is_file():
        return None
    return fleet, domain


async def _spawn_node(task_key, snap, token, node_name, index, manager_sid, request_id, kind, checkpoints_present, *,
                      name_suffix="", _registration_timeout_sec=None, _poll_interval=None) -> dict:
    node = snap.nodes.get(node_name)
    if node is None:
        raise ValueError(f"pipeline: node {node_name!r} is not in the schema snapshot of {task_key}")
    facts = _facts_for(task_key)
    fold_ = _engine_fold(task_key, "pipeline")
    round_inputs: list[str] = []
    if fold_.round > 1:
        asks = sorted((n for n in facts.artifacts if n.startswith("context.rework-")), key=_rework_number)
        named = [n for n in (fold_.round_event or {}).get("inputs") or [] if n in facts.artifacts]
        round_inputs = [*asks, *named]
    cwd = token.cwd
    if node.repeat:
        tasks = facts.plan_tasks or []
        task_cwd = tasks[index - 1].get("cwd") if 0 < index <= len(tasks) else None
        if isinstance(task_cwd, str) and task_cwd.strip():
            cwd = task_cwd
    decisions = [full for full, a in facts.artifacts.items() if a.get("phase") == "decision"]
    events = _read_events(paths.artifact_events_path(task_key))
    redo_from = (pipeline.redo_round(fold_.round_event) or {}).get("from") if fold_.round > 1 else None
    rework_ask = pipeline.last_rework_ask(events) if redo_from else None
    back = _back_stay(events, token, node_name)
    joined_from = {e.frm for e in snap.edges if e.join and e.to == node_name}
    returned = back is None and any(
        e.get("type") == "token_back" and (e.get("from") == node_name or e.get("to") in joined_from)
        for e in _round_events(events))
    checkpoints = list(checkpoints_present)
    back_out = [e for e in snap.back_edges if e.frm == node_name]
    if back is not None or returned:
        present = [c for c in (pipeline._expand(c, index) for c in node.checkpoints) if c in facts.artifacts]
        checkpoints = list(dict.fromkeys([*checkpoints, *present]))
    inputs = pipeline.expand_inputs(node, index, _task_indexes(facts))
    listed = pipeline.listed_inputs(inputs, decisions, round_inputs)
    copies = _input_copies(task_key, facts, listed)
    brief = pipeline.compose_brief(node, dataclasses.replace(token, node=node_name, index=index, cwd=cwd), task_key,
                                   inputs, checkpoints,
                                   decisions, [e for e in snap.edges if e.frm == node_name], round_no=fold_.round,
                                   round_inputs=round_inputs, rulings=_domain_rulings(task_key),
                                   redo_out=[e for e in snap.redo_edges if e.frm == node_name], round_from=redo_from,
                                   round_rework_ask=rework_ask, back_out=back_out,
                                   back_rows={e.to: [r["row"] for r in pipeline.back_rows(snap, e.to, facts.plan_tasks)]
                                              for e in back_out},
                                   copies=copies)
    if kind == "restart":
        brief += "\n" + _RESTART_LINE
    if back is not None:
        brief += (f"\n{_back_message(back.get('from'), back.get('reason'))}. "
                  f"Your checkpoints: {', '.join(checkpoints) or '(none)'}.")
    elif returned:
        brief += ("\nThe work came back to this node after a return; continue from your checkpoints: "
                  + (", ".join(checkpoints) or "(none)"))
    message = _request_message(task_key, request_id)
    if message:
        brief += f"\nThe manager's message: {message}"
    name = f"{task_key}-{node_name}" + (f"-{index}" if node.repeat else "") + name_suffix
    from . import roles
    names = roles.role_names()

    def read(full):
        return _artifact_body(task_key, full)

    if node.model is not None:
        model, effort = pipeline.node_model(node, facts, index, read)
        context_role = node.role if node.role in names else None
        extra_args = ["--model", model, "--effort", effort, *node.extra_args]
    else:
        from . import former_rows
        row = node.tier or node.role
        context_role = node.role if node.role in names else None
        extra_args = [*former_rows.node_args(pipeline.node_tier(node, row, facts, index, read)), *node.extra_args]
    if pipeline.declares_children(snap):
        extra_args = _node_settings_args(task_key, request_id, extra_args, node.profile_settings or {},
                                         guard=[s.entry for s in node.subagents.values()])
    elif node.profile_settings:
        extra_args = _node_settings_args(task_key, request_id, extra_args, node.profile_settings)
    return await spawn_worker_impl(brief, name=name, cwd=cwd, extra_args=extra_args,
                                   manager_sid=manager_sid, task_key=task_key,
                                   _registration_timeout_sec=_registration_timeout_sec, _poll_interval=_poll_interval,
                                   _tool="pipeline", _pipeline_request_id=request_id, context_role=context_role,
                                   _handed_inputs=[n for n in listed if copies.get(n) and copies[n].path])


_HANDED_NAMES_BUDGET = 2500


def _input_copies(task_key: str, facts, names: list[str]) -> dict[str, pipeline.InputCopy]:
    copies: dict[str, pipeline.InputCopy] = {}
    for full in names:
        stamp = facts.artifacts.get(full)
        if stamp is None:
            continue
        phase, _, name = full.partition(".")
        try:
            body = _artifact_body(task_key, full)
            path = None if body is None else str(artifact_export.export_body(task_key, phase, name, body,
                                                                             prune_after=False))
        except (OSError, ValueError):
            path = None
        copies[full] = pipeline.InputCopy(path, stamp.get("written_at"), stamp.get("contract_hash"), stamp.get("status"))
    if any(c.path for c in copies.values()):
        try:
            artifact_export.prune()
        except OSError:
            pass
    return copies


def _handed_events(assignment_id: str, names: list[str]) -> list[dict]:
    groups: list[list[str]] = []
    for name in names:
        if groups and len(json.dumps([*groups[-1], name])) <= _HANDED_NAMES_BUDGET:
            groups[-1].append(name)
        else:
            groups.append([name])
    return [{"type": "inputs_handed", "assignment_id": assignment_id, "names": g} for g in groups]


def _round_events(events) -> list[dict]:
    start = max((i for i, e in enumerate(events) if e.get("type") == "round_started"), default=-1)
    return events[start + 1:]


def _back_stay(events, token, node_name) -> dict | None:
    back = None
    family = token.token_id.split("/", 1)[0]
    for e in _round_events(events):
        kind = e.get("type")
        event_tid = e.get("token_id") or pipeline.DEFAULT_TOKEN_ID
        if kind == "token_back" and e.get("to") == node_name and e.get("row") == token.row:
            back = e
        elif back is not None and event_tid == token.token_id and (
                kind == "token_retired" or (kind == "advance_committed" and e.get("node") != node_name)):
            back = None
        elif back is not None and event_tid == family and kind in ("token_override", "token_forked"):
            back = None
    return back


def _back_message(frm, reason) -> str:
    return f"The {frm} node returned the work to this node: {reason}"


def _request_message_path(task_key, request_id) -> Path:
    return paths.artifact_ticket_dir(task_key) / "messages" / f"{request_id}.txt"


def _request_message(task_key, request_id) -> str:
    if not request_id:
        return ""
    try:
        return _request_message_path(task_key, request_id).read_text()
    except OSError:
        return ""


def _stamp_handed_off(task_key, token_id, assignment_id) -> bool:
    for p in paths.ASSIGNMENTS.glob("*.json"):
        record = state.read_json(p)
        if not isinstance(record, dict) or record.get("assignment_id") != assignment_id:
            continue
        _append_task_event(task_key, {"type": "handed_off", "token_id": token_id, "assignment_id": assignment_id,
                                      "sid": record.get("claude_sid")})
        record["handed_off_at"] = time.time()
        state.write_json_atomic(p, record)
        from .pipeline_child import close_children_of
        close_children_of(task_key, assignment_id, "handed_off")
        return True
    return False


def _close_superseded(spawn_result) -> None:
    if spawn_result.get("status") == "registered":
        kill_worker_impl(spawn_result["claude_sid"])
        return
    window_id = spawn_result.get("window_id")
    if not window_id:
        return
    _close_window(window_id)
    assignment_id = spawn_result.get("assignment_id")
    if assignment_id:
        paths.pending_assignment_path(assignment_id).unlink(missing_ok=True)
        paths.pending_window_path(assignment_id).unlink(missing_ok=True)


def _committed(request_id, spawn_result) -> dict:
    return {"ok": True, "state": "committed", "request_id": request_id,
            "next": {"name": spawn_result.get("name"), "sid": spawn_result.get("claude_sid"),
                     "assignment_id": spawn_result["assignment_id"]}}


def _commit_or_pend(task_key, request_id, token_id, node_name, index, spawn_result, caller_assignment_id,
                    reconciled=False) -> dict:
    with _token_lock(task_key):
        fold_ = _engine_fold(task_key, "pipeline_advance")
        open_ids = {r.get("request_id") for r in fold_.open_requests}
        token = fold_.tokens.get(token_id)
        if request_id in open_ids and spawn_result.get("status") == "registered":
            resumed = {"resumed": True} if spawn_result.get("resumed") else {}
            _append_task_event(task_key, {"type": "advance_committed", "token_id": token_id, "request_id": request_id,
                                          "node": node_name, "index": index,
                                          "holder": spawn_result["assignment_id"],
                                          "sid": spawn_result.get("claude_sid"), "reconciled": reconciled, **resumed})
            for displaced in dict.fromkeys((token.holder if token else None, caller_assignment_id)):
                if displaced and displaced != spawn_result["assignment_id"]:
                    _stamp_handed_off(task_key, token_id, displaced)
            if resumed:
                _restamp_resumed(task_key, token_id, request_id, spawn_result)
            return {**_committed(request_id, spawn_result), **resumed}
        if request_id in open_ids:
            pend_reason = spawn_result.get("pend_reason")
            _append_task_event(task_key, {"type": "advance_pending", "token_id": token_id, "request_id": request_id,
                                          "reason": pend_reason or spawn_result.get("status") or "paused",
                                          "assignment_id": spawn_result.get("assignment_id")})
            out = {"ok": True, "state": "pending", "request_id": request_id, "next": None,
                   "reason": spawn_result.get("reason")}
            if pend_reason:
                out["pend_reason"] = pend_reason
            return out
        if (spawn_result.get("status") == "registered" and spawn_result.get("assignment_id")
                and token is not None and token.holder == spawn_result["assignment_id"]):
            return _committed(request_id, spawn_result)
    out = {"ok": False, "state": "superseded", "request_id": request_id}
    try:
        _close_superseded(spawn_result)
    except Exception as err:
        out["close_error"] = repr(err)
    return out


def _restamp_resumed(task_key, token_id, request_id, spawn_result) -> None:
    _append_task_event(task_key, {"type": "holder_restamped", "token_id": token_id, "request_id": request_id,
                                  "old": None, "new": spawn_result["assignment_id"], "reason": "back"})
    path = paths.assignment_path(spawn_result["claude_sid"])
    record = state.read_json(path)
    if isinstance(record, dict) and record.pop("handed_off_at", None) is not None:
        state.write_json_atomic(path, record)


def _pend_request(task_key, token_id, request_id, reason, error=None) -> dict:
    event = {"type": "advance_pending", "token_id": token_id, "request_id": request_id, "reason": reason,
             "assignment_id": None}
    if error is not None:
        event["error"] = error
    with _token_lock(task_key):
        _append_task_event(task_key, event)
    return {"ok": True, "state": "pending", "request_id": request_id, "next": None, "reason": error or reason}


def _write_start_card(task_key, schema, domain, ask, cwd) -> None:
    card = topic.new_card(schema, domain, topic.title_of(ask), worktree=cwd, ticket=topic.ticket_link(task_key))
    try:
        topic.update(task_key, lambda current: card if current is None else None)
    except OSError:
        return


def _sync_card(task_key) -> None:
    try:
        events = _read_events(paths.artifact_events_path(task_key))
        token = pipeline.fold(events).tokens.get(pipeline.DEFAULT_TOKEN_ID)
        if token is None:
            return
        kind = pipeline.running_schema(events, pipeline.DEFAULT_TOKEN_ID, None)
        topic.update(task_key, lambda current: topic.follow(current, kind=kind, worktree=token.cwd or None))
    except (OSError, KeyError, ValueError, TypeError):
        return


def _start_locked(manager_sid, manager_name, task_key, schema, cwd, ask, text, start, domain) -> str:
    tid = pipeline.DEFAULT_TOKEN_ID
    with _token_lock(task_key):
        if _engine_fold(task_key, "pipeline_start").tokens:
            raise ValueError(f"pipeline_start: {task_key} already has a token; move it with pipeline_advance, or "
                             "open a new round of the work with pipeline_rework")
        request_id = uuid.uuid4().hex[:12]
        started = _fit_task_event({"type": "pipeline_started", "token_id": tid, "schema": schema,
                                   "schema_sha256": pipeline.schema_sha256(text), "node": start, "index": 1,
                                   "holder": None, "cwd": cwd, "manager": manager_name, "actor_sid": manager_sid})
        requested = _fit_task_event({"type": "advance_requested", "token_id": tid, "request_id": request_id,
                                     "edge": {"from": None, "to": start}, "index_to": 1, "kind": "advance",
                                     "actor_sid": manager_sid})
        artifact_put_impl(task_key, "context", "ask", ask, "complete", writer_sid=manager_sid, scope="output")
        _write_snapshot(task_key, text)
        _append_task_event(task_key, started)
        _append_task_event(task_key, requested)
        _write_start_card(task_key, schema, domain, ask, cwd)
    return request_id


START_GRANT = "mcp__dockwright__pipeline_start"


def _node_may_start(assignment) -> bool:
    task_key = assignment["ticket"]
    snap = _load_snapshot_for(task_key)
    if snap is None:
        return False
    try:
        token = _held_token(_fold_for(task_key), assignment.get("assignment_id"))
    except (OSError, KeyError, ValueError, TypeError):
        return False
    node = snap.nodes.get(token.node) if token is not None else None
    return node is not None and START_GRANT in node.extra_args


def _starting_manager_sid(caller_sid):
    record = state.read_json(paths.ACTIVE / f"{caller_sid}.json") or {}
    if record.get("agent") != "worker":
        return caller_sid
    try:
        assignment = state.read_json(paths.assignment_path(caller_sid)) or {}
    except ValueError:
        return caller_sid
    if not (assignment.get("pipeline_request_id") and assignment.get("ticket")):
        return caller_sid
    if _handed_off(caller_sid, assignment["ticket"]):
        raise ValueError(f"pipeline_start refused: node {caller_sid} of {assignment['ticket']} handed off; "
                         "call worker_done")
    if not _node_may_start(assignment):
        return caller_sid
    found = _manager_sid_for(assignment["ticket"], assignment)
    if found is None:
        raise ValueError("pipeline_start: the calling node's work has no live manager; nothing was started")
    return found


async def pipeline_start_impl(manager_sid, task_key, schema, cwd, ask, _registration_timeout_sec=None,
                              _poll_interval=None, domain=None) -> dict:
    if not manager_sid:
        raise ValueError("pipeline_start requires manager_sid, your own session UUID")
    if not task_key:
        raise ValueError("pipeline_start requires task_key")
    _validate_task_key(task_key)
    if not cwd:
        raise ValueError("pipeline_start requires cwd, the work's directory")
    manager_sid = _starting_manager_sid(manager_sid)
    record = _resolve_parent_record(manager_sid, tool="pipeline")
    manager_name = record["name"]
    domain = domain or record.get("domain") or DEFAULT_DOMAIN
    try:
        text = _schema_snapshot_text(schema, cwd)
    except ValueError as err:
        raise ValueError(f"pipeline_start: {err}") from err
    snap = pipeline.load_snapshot(text)
    start = pipeline.start_node(snap)
    tid = pipeline.DEFAULT_TOKEN_ID
    request_id = await asyncio.to_thread(_start_locked, manager_sid, manager_name, task_key, schema, cwd, ask, text,
                                         start, domain)
    token = pipeline.Token(tid, start, 1, None, cwd, manager_name)
    try:
        result = await _spawn_node(task_key, snap, token, start, 1, manager_sid, request_id, "advance", [],
                                   _registration_timeout_sec=_registration_timeout_sec, _poll_interval=_poll_interval)
    except (RuntimeError, ValueError, OSError) as err:
        return await asyncio.to_thread(_pend_request, task_key, tid, request_id, "spawn-error", str(err))
    return await asyncio.to_thread(_commit_or_pend, task_key, request_id, tid, start, 1, result, None)


async def pipeline_advance_impl(claude_sid, task_key, to, reason=None, override=False, index=None, resnapshot=False,
                                token_id=None, cwd=None, _registration_timeout_sec=None, _poll_interval=None,
                                row=None) -> dict:
    if not (override or index is not None or resnapshot or token_id is not None or cwd is not None):
        redo_token = await asyncio.to_thread(_redo_token, claude_sid, task_key, to)
        if redo_token is not None:
            return await _redo_advance(claude_sid, task_key, to, reason, redo_token, _registration_timeout_sec,
                                       _poll_interval)
        if await asyncio.to_thread(_back_token, claude_sid, task_key, to) is not None:
            return await _back_advance(claude_sid, task_key, to, reason, row, _registration_timeout_sec,
                                       _poll_interval)
    request = await asyncio.to_thread(_request_advance_locked, claude_sid, task_key, to, reason, override, index,
                                      resnapshot, token_id, cwd)
    if request.get("held"):
        return {"ok": True, "state": request["held"], "request_id": None, "next": None}
    caller = request["caller"]
    if not request["spawns"]:
        return {"ok": True, "state": "joined", "request_id": None, "next": None}
    is_manager = caller.agent == "manager"
    manager_sid = claude_sid if is_manager else _manager_sid_for(task_key, request["assignment"])
    displaced = None if is_manager or request["caller_stamped"] else caller.assignment_id
    parent = request["spawns"][0]["token"]
    results = []
    for sp in request["spawns"]:
        tok = sp["token"]
        if not manager_sid:
            results.append(await asyncio.to_thread(_pend_request, task_key, tok.token_id, sp["request_id"],
                                                   "no-manager"))
            continue
        if not is_manager and parent.manager and monitor._manager_limited(parent.manager):
            results.append(await asyncio.to_thread(_pend_request, task_key, tok.token_id, sp["request_id"],
                                                   "manager-limited"))
            continue
        try:
            result = await _spawn_node(task_key, request["snap"], tok, sp["to"], sp["index_to"], manager_sid,
                                       sp["request_id"], "advance", [],
                                       _registration_timeout_sec=_registration_timeout_sec,
                                       _poll_interval=_poll_interval)
        except (RuntimeError, ValueError, OSError) as err:
            results.append(await asyncio.to_thread(_pend_request, task_key, tok.token_id, sp["request_id"],
                                                   "spawn-error", str(err)))
            continue
        results.append(await asyncio.to_thread(_commit_or_pend, task_key, sp["request_id"], tok.token_id, sp["to"],
                                               sp["index_to"], result, displaced))
    return results[0] if len(results) == 1 else {**results[0], "rows": results}


_PR_STATES = ("OPEN", "CLOSED", "MERGED")


def _gh_pr_json(url, cwd, fields) -> dict:
    from . import gh_cli
    return gh_cli.pr_json(url, fields, cwd)


def _reported_state(data) -> str:
    reported = data.get("state")
    if reported not in _PR_STATES:
        raise ValueError(f"gh reported state {reported!r}, not one of {'/'.join(_PR_STATES)}")
    return reported


def _pr_state(url, cwd) -> str:
    return _reported_state(_gh_pr_json(url, cwd, "state"))


def _pr_draft_state(url, cwd) -> tuple[str, bool]:
    data = _gh_pr_json(url, cwd, "state,isDraft")
    draft = data.get("isDraft")
    if not isinstance(draft, bool):
        raise ValueError(f"gh reported isDraft {draft!r}, not a boolean")
    return _reported_state(data), draft


def _node_pr_urls(task_key, node, index) -> list[str]:
    urls: list[str] = []
    for full in (pipeline._expand(o, index) for o in node.outputs):
        if not full.startswith("implement."):
            continue
        phase, name = full.split(".", 1)
        try:
            body = artifact_get_impl(task_key, phase, name)["content"]
        except ValueError:
            continue
        urls.extend(pipeline.pr_urls(body))
    return urls


def _merge_wait_urls(task_key, snap, token, artifacts) -> list[str]:
    node = snap.nodes.get(token.node)
    if node is None:
        return []
    edge = next((e for e in snap.edges if e.to == token.node and e.trigger == "approve"), None)
    if edge is not None and edge.frm in snap.nodes and _merge_state_word(task_key, artifacts, token) == "waiting":
        return _node_pr_urls(task_key, snap.nodes[edge.frm], token.index)
    if not any(e.frm == token.node and e.trigger in ("merge", "approve") for e in snap.edges):
        return []
    if not all((artifacts.get(pipeline._expand(o, token.index)) or {}).get("status") == "complete"
               for o in node.outputs):
        return []
    return _node_pr_urls(task_key, node, token.index)


def _implement_pr_urls(task_key, snap) -> list[str]:
    if snap is None:
        return []
    count = len(_facts_for(task_key).plan_tasks or [])
    urls = [url for node in snap.nodes.values() for index in (range(1, count + 1) if node.repeat else (1,))
            for url in _node_pr_urls(task_key, node, index)]
    return list(dict.fromkeys(urls))


def _carried_prs(task_key) -> list[str]:
    try:
        return pipeline.redo_carried_prs(_fold_for(task_key).round_event)
    except (OSError, KeyError, ValueError, TypeError):
        return []


def _round_pr_urls(task_key, snap) -> list[str]:
    if snap is None:
        return []
    return list(dict.fromkeys([*_implement_pr_urls(task_key, snap), *_carried_prs(task_key)]))


def _rework_token(task_key):
    try:
        return _fold_for(task_key).tokens.get(pipeline.DEFAULT_TOKEN_ID)
    except (KeyError, ValueError, TypeError):
        return None


def _refuse_open_prs(manager_sid, task_key, to, token) -> list[str]:
    tid = pipeline.DEFAULT_TOKEN_ID
    snap = _load_snapshot_for(task_key)
    own = set(_implement_pr_urls(task_key, snap))
    urls = _round_pr_urls(task_key, snap)
    for url in urls:
        try:
            state_ = _pr_state(url, token.cwd if token is not None else None)
        except ValueError as err:
            _refuse_advance(task_key, tid, to, manager_sid, f"cannot read the state of {url}: {err}",
                            f"pipeline_rework refused: cannot read the state of {url} ({err}); retry once gh works")
        if state_ == "OPEN" and url not in own:
            _refuse_advance(task_key, tid, to, manager_sid, f"PR {url} is a draft a redo carried",
                            f"pipeline_rework refused: PR {url} is a draft a redo carried: the redesign round "
                            "continues it; a rework waits until it closes or merges")
        if state_ == "OPEN":
            _refuse_advance(task_key, tid, to, manager_sid, f"PR {url} is open",
                            f"pipeline_rework refused: PR {url} of this round is open. Changes on it go through the "
                            "node that holds the work's token (resume it and send the ask); a rework needs the PR "
                            "merged or closed")
    return urls


def _spawned_at(record) -> float:
    value = record.get("spawned_at")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _newest_assignment(match, records=None) -> dict | None:
    records = state.list_json_in(paths.ASSIGNMENTS) if records is None else records
    found = [r for r in records if isinstance(r, dict) and match(r)]
    return max(found, key=_spawned_at) if found else None


def _holder_record(holder, records=None) -> dict | None:
    return _newest_assignment(lambda r: r.get("assignment_id") == holder, records) if holder else None


def _request_record(request, records=None) -> dict | None:
    request_id = request.get("request_id")
    pending = (request.get("pending") or {}).get("assignment_id")
    return _newest_assignment(lambda r: bool(pending and r.get("assignment_id") == pending)
                              or bool(request_id and r.get("pipeline_request_id") == request_id), records)


def _session_alive(sid) -> bool:
    path = paths.ACTIVE / f"{sid}.json"
    if not sid or not path.exists():
        return False
    record = state.read_json(path)
    return _pid_reads_alive(record.get("pid") if isinstance(record, dict) else None)


def _archive_round(task_key, closing, keep) -> None:
    d = paths.artifact_ticket_dir(task_key)
    dest = d / "rounds" / str(closing)
    dest.mkdir(parents=True, exist_ok=True)
    if (d / "schema.toml").is_file() and not (dest / "schema.toml").exists():
        shutil.copy2(d / "schema.toml", dest / "schema.toml")
    for p in sorted(d.glob("*.md")):
        try:
            stamp, _ = state.parse_artifact(p.read_text())
            name = f"{stamp.get('phase')}.{stamp.get('name')}"
        except (OSError, ValueError):
            name = p.stem
        prev = p.with_name(p.name + ".prev")
        if name in keep:
            if prev.is_file():
                shutil.copy2(prev, dest / prev.name)
            continue
        os.replace(p, dest / p.name)
        if prev.is_file():
            os.replace(prev, dest / prev.name)


def _open_round_locked(task_key, fold_, token, to, keep, ask, *, writer_sid, actor_sid, manager_name, reason, inputs,
                       refuse, snapshot=None, round_extra=None, requested_extra=None) -> dict:
    tid = pipeline.DEFAULT_TOKEN_ID
    new_round = fold_.round + 1
    request_id = uuid.uuid4().hex[:12]
    requested = {"type": "advance_requested", "token_id": tid, "request_id": request_id,
                 "edge": {"from": token.node, "to": to}, "index_to": 1, "kind": "advance", "actor_sid": actor_sid}
    requested.update(requested_extra or {})
    events = []
    if snapshot is not None:
        new_text, schema_name = snapshot
        events.append({"type": "schema_snapshot", "token_id": tid, "schema_sha256": pipeline.schema_sha256(new_text),
                       "schema": schema_name, "reason": reason, "actor_sid": actor_sid})
    events.append({"type": "round_started", "token_id": tid, "round": new_round, "to": to,
                   "ask": pipeline.rework_ask_name(new_round), "inputs": inputs, "actor_sid": actor_sid,
                   "manager": manager_name, **(round_extra or {})})
    events.append({"type": "token_override", "token_id": tid, "from": token.node, "to": to, "index": 1,
                   "reason": reason, "actor_sid": actor_sid, "resnapshot": snapshot is not None,
                   "manager": manager_name, "via": (round_extra or {}).get("via", "rework")})
    events.append(requested)
    try:
        fitted = [_fit_task_event(e) for e in events]
    except ValueError as err:
        refuse(str(err))
    _archive_round(task_key, fold_.round, keep)
    if snapshot is not None:
        _write_snapshot(task_key, snapshot[0])
    artifact_put_impl(task_key, "context", f"rework-{new_round}", ask, "complete", writer_sid=writer_sid,
                      scope="output", overwrite=True)
    for event in fitted:
        _append_task_event(task_key, event)
    for tok in fold_.tokens.values():
        if tok.holder:
            _stamp_handed_off(task_key, tok.token_id, tok.holder)
    return {"request_id": request_id, "round": new_round}


def _rework_entry(snap, to, ask, ticket_changed, refuse) -> tuple[str, str]:
    start = pipeline.start_node(snap)
    if ticket_changed:
        if start is None or "context.ticket" not in snap.nodes[start].outputs:
            refuse(f"ticket_changed needs a start node that outputs context.ticket; {start} does not")
        ask = f"ticket_changed: yes\n{ask}"
    if snap.rework_at is None:
        if to is None:
            refuse("name to: the schema declares no rework_at")
        return to, ask
    to = snap.rework_at if to is None else to
    if to == start != snap.rework_at and not ticket_changed:
        refuse(f"a rework enters the start node {start} only with ticket_changed=True; this schema's rework entry is "
               f"{snap.rework_at}")
    return to, ask


def _request_rework_locked(manager_sid, task_key, to, ask, inputs, checked_urls, schema=None, cwd=None,
                           ticket_changed=False) -> dict:
    tid = pipeline.DEFAULT_TOKEN_ID

    def refuse(reason):
        _refuse_advance(task_key, tid, to, manager_sid, reason, f"pipeline_rework refused: {reason}")

    with _token_lock(task_key):
        try:
            fold_ = _fold_for(task_key)
        except (KeyError, ValueError, TypeError) as err:
            refuse(f"events.jsonl for {task_key} is unreadable: {err!r}")
        snap = _load_snapshot_for(task_key)
        token = fold_.tokens.get(tid)
        if snap is None or token is None:
            refuse(f"{task_key} has no pipeline token; start a work with pipeline_start")
        _, _, active = _caller_for(manager_sid)
        if not (isinstance(ask, str) and ask.strip()):
            refuse("ask must be the rework's text: his words, his rulings that still bind, the facts with sources")
        if not all(isinstance(n, str) and n for n in inputs):
            refuse("inputs must be artifact names, phase.name")
        for tok in fold_.tokens.values():
            holder = _holder_record(tok.holder)
            if holder is not None and _session_alive(holder.get("claude_sid")):
                refuse(f"holder {holder.get('name')} ({holder.get('claude_sid')}) of {tok.node} is alive: kill_worker "
                       "it, then call pipeline_rework again")
        for request in fold_.open_requests:
            record = _request_record(request)
            if record is not None and _session_alive(record.get("claude_sid")):
                refuse(f"session {record.get('name')} ({record.get('claude_sid')}) of open request "
                       f"{request.get('request_id')} is alive: kill_worker it, then call pipeline_rework again")
        pending = _pending_question_ids(task_key)
        if pending:
            refuse(f"pending question(s) of {task_key}: {', '.join(pending)}. Answer each with answer_question, then "
                   "call pipeline_rework again")
        if _round_pr_urls(task_key, snap) != checked_urls:
            refuse("the round's PR changed during the check; call pipeline_rework again")
        schema_name = schema if schema is not None else _started_schema_name(task_key, tid, snap.name)
        try:
            new_text = _schema_snapshot_text(schema_name, cwd if cwd is not None else token.cwd)
        except ValueError as err:
            refuse(f"resnapshot: {err}")
        new_snap = pipeline.load_snapshot(new_text)
        to, ask = _rework_entry(new_snap, to, ask, ticket_changed, refuse)
        problem = pipeline.fork_entry_problem(new_snap, to, _facts_for(task_key).plan_tasks)
        if problem:
            refuse(problem)
        stamps = _facts_for(task_key).artifacts
        problems = pipeline.rework_problems(new_snap, to, stamps, inputs)
        if problems:
            refuse("; ".join(problems))
        keep = pipeline.carried_names(new_snap, to, set(stamps), inputs)
        gate = _gate_state(task_key)
        if gate and gate["state"] == "open" and "gate.open" not in keep:
            refuse(f"the {gate.get('node')} gate still runs (supervisor pid {gate.get('pid')}); its verdict would land "
                   f"in the new round: end it with kill -TERM -- -{gate.get('pid')}, then call pipeline_rework again")
        new_round = fold_.round + 1
        opened = _open_round_locked(task_key, fold_, token, to, keep, ask, writer_sid=manager_sid,
                                    actor_sid=manager_sid, manager_name=active.get("name"),
                                    reason=f"rework round {new_round}", inputs=inputs, refuse=refuse,
                                    snapshot=(new_text, schema_name),
                                    requested_extra={"cwd": cwd} if cwd is not None else None)
    return {"request_id": opened["request_id"],
            "token": dataclasses.replace(token, cwd=cwd) if cwd is not None else token,
            "snap": new_snap, "round": opened["round"], "to": to}


REDO_QUESTION = "Redesign request"
REDO_YES = "redesign: yes"
_REDO_NO_PERMIT = ("a redo edge needs the engineer's explicit permission: ask_manager with a first line "
                   "`Redesign request`, then call again after his `redesign: yes`")


def _held_edge_token(claude_sid, task_key, to, edges, find):
    if not (claude_sid and task_key) or to == "?":
        return None
    _validate_task_key(task_key)
    snap = _load_snapshot_for(task_key)
    if snap is None or not getattr(snap, edges):
        return None
    caller, _, _ = _caller_for(claude_sid)
    if caller.agent == "manager" or not caller.assignment_id:
        return None
    try:
        token = _held_token(_fold_for(task_key), caller.assignment_id)
    except (KeyError, ValueError, TypeError):
        return None
    if token is None or find(snap, token.node, to) is None:
        return None
    return token


def _redo_token(claude_sid, task_key, to):
    return _held_edge_token(claude_sid, task_key, to, "redo_edges", pipeline.redo_edge)


def _back_token(claude_sid, task_key, to):
    return _held_edge_token(claude_sid, task_key, to, "back_edges", pipeline.back_edge)


def _redo_permit(events, assignment_id) -> dict | None:
    if not assignment_id:
        return None
    start = None
    for i, e in enumerate(events):
        if ((e.get("type") in ("pipeline_started", "advance_committed") and e.get("holder") == assignment_id)
                or (e.get("type") == "holder_restamped" and e.get("new") == assignment_id)):
            start = i
    if start is None:
        return None
    latest = None
    for e in events[start + 1:]:
        if (e.get("type") == "question_answered" and e.get("assignment_id") == assignment_id
                and e.get("question_head") == REDO_QUESTION):
            latest = e
    if latest is None or latest.get("decided_by") != "engineer" or latest.get("answer_head") != REDO_YES:
        return None
    return latest


def _redo_ask(task_key, reason, permit, carried) -> str:
    qid = str(permit.get("question_id") or "")
    try:
        record = artifact_get_impl(task_key, "decision", f"q-{qid[:8]}")["content"]
    except ValueError:
        record = f"## question\n{permit.get('question_head')}\n\n## answer\n{permit.get('answer_head')}"
    parts = [reason.strip(), "", f"Permitted by question {qid}:", record.strip()]
    if carried:
        parts += ["", "Open draft PRs this round continues:", *carried]
    return "\n".join(parts) + "\n"


def _redo_preflight(claude_sid, task_key, to, reason) -> None:
    with _token_lock(task_key):
        fold_, snap, token, tid, caller, _, _ = _advance_context_locked(claude_sid, task_key, to, None)
        verdict = pipeline.check_advance(snap, token, caller, to, _facts_for(task_key), fold_.open_requests,
                                         reason=reason)
        if not verdict.ok:
            _refuse_advance(task_key, tid, to, claude_sid, verdict.reason)
        if _redo_permit(_read_events(paths.artifact_events_path(task_key)), caller.assignment_id) is None:
            _refuse_advance(task_key, tid, to, claude_sid, _REDO_NO_PERMIT)


def _redo_pr_check(claude_sid, task_key, to, token) -> tuple[list[str], list[str]]:
    urls = _round_pr_urls(task_key, _load_snapshot_for(task_key))
    carried: list[str] = []
    for url in urls:
        try:
            state_, draft = _pr_draft_state(url, token.cwd)
        except ValueError as err:
            _refuse_advance(task_key, token.token_id, to, claude_sid,
                            f"cannot read the state of {url} ({err}); retry once gh works")
        if state_ == "MERGED":
            _refuse_advance(task_key, token.token_id, to, claude_sid,
                            f"PR {url} has merged; a redesign after a merge is not a redo: ask_manager")
        if state_ == "OPEN" and not draft:
            _refuse_advance(task_key, token.token_id, to, claude_sid,
                            f"PR {url} is open and not a draft: convert it to a draft first: gh pr ready --undo {url}")
        if state_ == "OPEN":
            carried.append(url)
    return urls, carried


def _request_redo_locked(claude_sid, task_key, to, reason, checked_urls, carried) -> dict:
    with _token_lock(task_key):
        fold_, snap, token, tid, caller, assignment, _ = _advance_context_locked(claude_sid, task_key, to, None)

        def refuse(why):
            _refuse_advance(task_key, tid, to, claude_sid, why)

        verdict = pipeline.check_advance(snap, token, caller, to, _facts_for(task_key), fold_.open_requests,
                                         reason=reason)
        if not verdict.ok:
            refuse(verdict.reason)
        if not verdict.edge.redo:
            refuse(f"{token.node} -> {to} is no longer a redo edge; call pipeline_advance again")
        for tok in fold_.tokens.values():
            if tok.holder == caller.assignment_id:
                continue
            holder = _holder_record(tok.holder)
            if holder is not None and _session_alive(holder.get("claude_sid")):
                refuse(f"holder {holder.get('name')} ({holder.get('claude_sid')}) of {tok.node} is alive: ask_manager "
                       "to stop it (it commits its unfinished work, calls worker_done, then kill_worker), then call "
                       "pipeline_advance again")
        for request in fold_.open_requests:
            record = _request_record(request)
            if record is not None and _session_alive(record.get("claude_sid")):
                refuse(f"session {record.get('name')} ({record.get('claude_sid')}) of open request "
                       f"{request.get('request_id')} is alive: ask_manager to kill_worker it, then call "
                       "pipeline_advance again")
        pending = _pending_question_ids(task_key)
        if pending:
            refuse(f"pending question(s) of {task_key}: {', '.join(pending)}. The manager answers each; then call "
                   "pipeline_advance again")
        if _round_pr_urls(task_key, snap) != checked_urls:
            refuse("the round's PRs changed during the check; call pipeline_advance again")
        stamps = _facts_for(task_key).artifacts
        problems = pipeline.rework_problems(snap, to, stamps, [])
        if problems:
            refuse("; ".join(problems))
        keep = pipeline.carried_names(snap, to, set(stamps), [])
        gate = _gate_state(task_key)
        if gate and gate["state"] == "open" and "gate.open" not in keep:
            refuse(f"the {gate.get('node')} gate still runs (supervisor pid {gate.get('pid')}); its verdict would land "
                   "in the new round: ask_manager")
        permit = _redo_permit(_read_events(paths.artifact_events_path(task_key)), caller.assignment_id)
        if permit is None:
            refuse(_REDO_NO_PERMIT)
        parent = fold_.tokens[pipeline.DEFAULT_TOKEN_ID]
        new_round = fold_.round + 1
        opened = _open_round_locked(
            task_key, fold_, parent, to, keep, _redo_ask(task_key, reason, permit, carried),
            writer_sid=f"redo:{token.node}", actor_sid=claude_sid, manager_name=parent.manager,
            reason=f"redo round {new_round} from {token.node}", inputs=[], refuse=refuse,
            round_extra={"via": "redo", "from": token.node, "carried_prs": carried},
            requested_extra={"edge": {"from": token.node, "to": to}, "actor_assignment": caller.assignment_id})
    return {**opened, "token": parent, "snap": snap, "assignment": assignment}


async def _redo_advance(claude_sid, task_key, to, reason, token, _registration_timeout_sec, _poll_interval) -> dict:
    tid = pipeline.DEFAULT_TOKEN_ID
    await asyncio.to_thread(_redo_preflight, claude_sid, task_key, to, reason)
    checked, carried = await asyncio.to_thread(_redo_pr_check, claude_sid, task_key, to, token)
    request = await asyncio.to_thread(_request_redo_locked, claude_sid, task_key, to, reason, checked, carried)
    request_id = request["request_id"]
    manager_sid = _manager_sid_for(task_key, request["assignment"])
    if not manager_sid:
        out = await asyncio.to_thread(_pend_request, task_key, tid, request_id, "no-manager")
        return {**out, "round": request["round"]}
    if request["token"].manager and monitor._manager_limited(request["token"].manager):
        out = await asyncio.to_thread(_pend_request, task_key, tid, request_id, "manager-limited")
        return {**out, "round": request["round"]}
    try:
        result = await _spawn_node(task_key, request["snap"], request["token"], to, 1, manager_sid, request_id,
                                   "advance", [], _registration_timeout_sec=_registration_timeout_sec,
                                   _poll_interval=_poll_interval)
    except (RuntimeError, ValueError, OSError) as err:
        out = await asyncio.to_thread(_pend_request, task_key, tid, request_id, "spawn-error", str(err))
    else:
        out = await asyncio.to_thread(_commit_or_pend, task_key, request_id, tid, to, 1, result, None)
    return {**out, "round": request["round"]}


def _back_context_locked(claude_sid, task_key, to, reason, row):
    fold_, snap, token, tid, caller, assignment, _ = _advance_context_locked(claude_sid, task_key, to, None)
    facts = _facts_for(task_key)
    verdict = pipeline.check_advance(snap, token, caller, to, facts, fold_.open_requests, reason=reason, row=row)
    if not verdict.ok:
        _refuse_advance(task_key, tid, to, claude_sid, verdict.reason)
    if not verdict.edge.back:
        _refuse_advance(task_key, tid, to, claude_sid,
                        f"{token.node} -> {to} is no longer a back edge; call pipeline_advance again")
    rows = pipeline.back_rows(snap, to, facts.plan_tasks)
    row = row if rows else None
    events = _read_events(paths.artifact_events_path(task_key))
    went = sum(1 for e in _round_events(events) if e.get("type") == "token_back" and e.get("row") == row)
    if went >= pipeline.back_cap():
        _refuse_advance(task_key, tid, to, claude_sid,
                        f"the work went back to {row or to} {went} times this round; ask_manager")
    return {"fold": fold_, "snap": snap, "token": token, "tid": tid, "caller": caller, "assignment": assignment,
            "facts": facts, "index": verdict.index_to, "rows": rows, "row": row, "events": events}


def _back_preflight(claude_sid, task_key, to, reason, row) -> dict:
    with _token_lock(task_key):
        return _back_context_locked(claude_sid, task_key, to, reason, row)


def _back_pr_check(claude_sid, task_key, to, context) -> list[str]:
    token, row, index = context["token"], context["row"], context["index"]
    tasks = context["facts"].plan_tasks or []
    task_cwd = tasks[index - 1].get("cwd") if 0 < index <= len(tasks) else None
    cwd = task_cwd if isinstance(task_cwd, str) and os.path.isdir(task_cwd) else token.cwd
    urls = _node_pr_urls(task_key, context["snap"].nodes[to], index)
    for url in urls:
        try:
            state_, _ = _pr_draft_state(url, cwd)
        except ValueError as err:
            _refuse_advance(task_key, token.token_id, to, claude_sid,
                            f"cannot read the state of {url} ({err}); retry once gh works")
        if state_ == "MERGED":
            label = f"row {row}" if row else to
            _refuse_advance(task_key, token.token_id, to, claude_sid,
                            f"PR {url} of {label} has merged; a return after a merge is new work: ask_manager")
    return urls


def _resume_candidate(events, superseded, token_id, to) -> dict | None:
    holder = sid = node = None
    for e in _round_events(events):
        if (e.get("token_id") or pipeline.DEFAULT_TOKEN_ID) != token_id:
            continue
        kind = e.get("type")
        if kind == "advance_committed" and e.get("request_id") not in superseded:
            node = e.get("node")
            if node == to:
                holder, sid = e.get("holder"), e.get("sid")
        elif kind == "token_override":
            node = e.get("to")
        elif kind == "holder_restamped" and node == to and e.get("new"):
            holder, sid = e["new"], None
    record = _holder_record(holder) or {}
    sid = record.get("claude_sid") or sid
    if not (holder and sid):
        return None
    active = state.read_json(paths.ACTIVE / f"{sid}.json")
    if isinstance(active, dict) and is_human_attached(active):
        return None
    closed = state.read_json(paths.CLOSED / f"{sid}.json")
    session = next((r for r in (active, closed) if isinstance(r, dict)), record)
    tokens = transcript.context_tokens(sid, session.get("runtime") or "claude")
    if tokens is None or tokens >= config.pipeline_int("fresh_context_tokens", 500000):
        return None
    if not (_session_alive(sid) or isinstance(closed, dict)):
        return None
    return {"assignment_id": holder, "sid": sid, "name": session.get("name") or record.get("name") or sid}


def _request_back_locked(claude_sid, task_key, to, reason, row, checked_urls) -> dict:
    with _token_lock(task_key):
        context = _back_context_locked(claude_sid, task_key, to, reason, row)
        token, tid, row, index = context["token"], context["tid"], context["row"], context["index"]
        caller = context["caller"]

        def refuse(why):
            _refuse_advance(task_key, tid, to, claude_sid, why)

        if _node_pr_urls(task_key, context["snap"].nodes[to], index) != checked_urls:
            refuse("the row's PRs changed during the check; call pipeline_advance again")
        row_tid = pipeline.row_token_id(tid, row) if row else tid
        candidate = _resume_candidate(context["events"], context["fold"].superseded, row_tid, to)
        request_id = uuid.uuid4().hex[:12]
        back = {"type": "token_back", "token_id": tid, "from": token.node, "from_index": token.index, "to": to,
                "row": row, "index": index, "reason": reason, "actor_sid": claude_sid}
        if row:
            back["rows"] = [{"row": r["row"], "stage": r["stage"], "token_id": pipeline.row_token_id(tid, r["row"])}
                            for r in context["rows"]]
        requested = {"type": "advance_requested", "token_id": row_tid, "request_id": request_id,
                     "edge": {"from": token.node, "to": to}, "index_to": index, "kind": "back", "row": row,
                     "reason": reason, "actor_sid": claude_sid, "actor_assignment": caller.assignment_id}
        if row:
            requested["cwd"] = token.cwd
        if candidate is not None:
            requested["resume_candidate"] = candidate["assignment_id"]
        try:
            fitted = [_fit_task_event(e) for e in (back, requested)]
        except ValueError as err:
            refuse(str(err))
        _append_task_event(task_key, fitted[0])
        _stamp_handed_off(task_key, tid, caller.assignment_id)
        _append_task_event(task_key, fitted[1])
    spawn_token = (pipeline.Token(row_tid, to, index, None, token.cwd, token.manager, row) if row else token)
    return {"request_id": request_id, "event": fitted[1], "token": spawn_token, "to": to, "index": index,
            "snap": context["snap"], "assignment": context["assignment"]}


def _back_candidate(task_key, request_id) -> dict | None:
    events = _read_events(paths.artifact_events_path(task_key))
    request = next((e for e in events if e.get("type") == "advance_requested" and e.get("request_id") == request_id),
                   {})
    named = request.get("resume_candidate")
    if not named:
        return None
    candidate = _resume_candidate(events, pipeline.fold(events).superseded,
                                  request.get("token_id") or pipeline.DEFAULT_TOKEN_ID, request["edge"]["to"])
    if candidate is None or candidate["assignment_id"] != named:
        return None
    return {**candidate, "message": _back_message(request["edge"]["from"], request.get("reason"))}


async def _resumed(candidate) -> bool:
    try:
        sent = await send_manager_to_worker_auto_impl(candidate["name"], candidate["message"])
    except (RuntimeError, ValueError, OSError):
        return False
    if isinstance(sent, dict) and sent.get("status") == "delivered" and sent.get("confirmed") is True:
        return True
    try:
        kill_worker_impl((sent.get("sid") if isinstance(sent, dict) else None) or candidate["sid"])
    except ValueError:
        pass
    return False


async def _resume_or_spawn_back(task_key, snap, token, to, index, manager_sid, request_id, *, name_suffix="",
                                _registration_timeout_sec=None, _poll_interval=None) -> dict:
    candidate = await asyncio.to_thread(_back_candidate, task_key, request_id)
    if candidate is not None and await _resumed(candidate):
        return {"status": "registered", "assignment_id": candidate["assignment_id"], "claude_sid": candidate["sid"],
                "name": candidate["name"], "resumed": True}
    return await _spawn_node(task_key, snap, token, to, index, manager_sid, request_id, "back", [],
                             name_suffix=name_suffix, _registration_timeout_sec=_registration_timeout_sec,
                             _poll_interval=_poll_interval)


async def _back_advance(claude_sid, task_key, to, reason, row, _registration_timeout_sec, _poll_interval) -> dict:
    context = await asyncio.to_thread(_back_preflight, claude_sid, task_key, to, reason, row)
    checked = await asyncio.to_thread(_back_pr_check, claude_sid, task_key, to, context)
    request = await asyncio.to_thread(_request_back_locked, claude_sid, task_key, to, reason, row, checked)
    token, request_id = request["token"], request["request_id"]
    manager_sid = _manager_sid_for(task_key, request["assignment"])
    if not manager_sid:
        return await asyncio.to_thread(_pend_request, task_key, token.token_id, request_id, "no-manager")
    if token.manager and monitor._manager_limited(token.manager):
        return await asyncio.to_thread(_pend_request, task_key, token.token_id, request_id, "manager-limited")
    try:
        result = await _resume_or_spawn_back(task_key, request["snap"], token, to, request["index"], manager_sid,
                                             request_id, _registration_timeout_sec=_registration_timeout_sec,
                                             _poll_interval=_poll_interval)
    except (RuntimeError, ValueError, OSError) as err:
        return await asyncio.to_thread(_pend_request, task_key, token.token_id, request_id, "spawn-error", str(err))
    return await asyncio.to_thread(_commit_or_pend, task_key, request_id, token.token_id, to, request["index"], result,
                                   None)


def _left_start(task_key, start) -> bool:
    return any(e.get("type") == "advance_committed" and e.get("node") != start
               and (e.get("token_id") or pipeline.DEFAULT_TOKEN_ID) == pipeline.DEFAULT_TOKEN_ID
               for e in _round_events(_read_events(paths.artifact_events_path(task_key))))


async def pipeline_rework_impl(manager_sid, task_key, to, ask, inputs=None, schema=None, cwd=None,
                               _registration_timeout_sec=None, _poll_interval=None, ticket_changed=False) -> dict:
    if not manager_sid:
        raise ValueError("pipeline_rework requires manager_sid, your own session UUID")
    if not task_key:
        raise ValueError("pipeline_rework requires task_key")
    _validate_task_key(task_key)
    names = list(inputs or [])
    tid = pipeline.DEFAULT_TOKEN_ID
    if _caller_for(manager_sid)[0].agent != "manager":
        _refuse_advance(task_key, tid, to, manager_sid, "pipeline_rework is manager-only",
                        "pipeline_rework refused: pipeline_rework is manager-only")
    cwd = _moved_cwd(cwd, lambda reason: _refuse_advance(task_key, tid, to, manager_sid, reason,
                                                         f"pipeline_rework refused: {reason}"))
    token = await asyncio.to_thread(_rework_token, task_key)
    if token is not None and cwd is not None:
        token = dataclasses.replace(token, cwd=cwd)
    if token is not None and not os.path.isdir(token.cwd or ""):
        _refuse_advance(task_key, tid, to, manager_sid, f"the work's cwd {token.cwd} is gone",
                        f"pipeline_rework refused: the work's cwd {token.cwd} is gone: recreate the worktree, or pass "
                        "cwd=<an existing directory> with a `to` that recreates it, then call pipeline_rework again")
    checked = await asyncio.to_thread(_refuse_open_prs, manager_sid, task_key, to, token)
    request = await asyncio.to_thread(_request_rework_locked, manager_sid, task_key, to, ask, names, checked, schema,
                                      cwd, ticket_changed)
    request_id, to = request["request_id"], request["to"]
    try:
        result = await _spawn_node(task_key, request["snap"], request["token"], to, 1, manager_sid, request_id,
                                   "advance", [], _registration_timeout_sec=_registration_timeout_sec,
                                   _poll_interval=_poll_interval)
    except (RuntimeError, ValueError, OSError) as err:
        out = await asyncio.to_thread(_pend_request, task_key, tid, request_id, "spawn-error", str(err))
    else:
        out = await asyncio.to_thread(_commit_or_pend, task_key, request_id, tid, to, 1, result, None)
    return {**out, "round": request["round"]}


def pipeline_close_impl(manager_sid, task_key, reason) -> dict:
    if not manager_sid:
        raise ValueError("pipeline_close requires manager_sid, your own session UUID")
    if not task_key:
        raise ValueError("pipeline_close requires task_key")
    _validate_task_key(task_key)

    def refuse(why):
        raise ValueError(f"pipeline_close refused: {why}")

    if _caller_for(manager_sid)[0].agent != "manager":
        refuse("pipeline_close is manager-only")
    with _token_lock(task_key):
        snap = _load_snapshot_for(task_key)
        if snap is None:
            refuse(f"{task_key} has no pipeline token; start a work with pipeline_start")
        try:
            events = _read_events(paths.artifact_events_path(task_key))
            fold_ = pipeline.fold(events)
        except (OSError, KeyError, ValueError, TypeError) as err:
            refuse(f"events.jsonl for {task_key} is unreadable: {err!r}")
        token = fold_.tokens.get(pipeline.DEFAULT_TOKEN_ID)
        if token is None:
            refuse(f"{task_key} has no pipeline token; start a work with pipeline_start")
        if fold_.closed is not None:
            refuse(_closed_text(task_key, fold_.closed))
        if not (isinstance(reason, str) and reason.strip()):
            refuse("reason must say why the work will never run again")
        for tok in fold_.tokens.values():
            holder = _holder_record(tok.holder)
            if holder is not None and _session_alive(holder.get("claude_sid")):
                refuse(f"holder {holder.get('name')} ({holder.get('claude_sid')}) of {tok.node} is alive: kill_worker "
                       "it, then call pipeline_close again")
        for request in fold_.open_requests:
            record = _request_record(request)
            if record is not None and _session_alive(record.get("claude_sid")):
                refuse(f"session {record.get('name')} ({record.get('claude_sid')}) of open request "
                       f"{request.get('request_id')} is alive: kill_worker it, then call pipeline_close again")
        handed: dict[str, str | None] = {}
        for e in events:
            if e.get("type") == "handed_off" and e.get("assignment_id"):
                handed.setdefault(e["assignment_id"], e.get("sid"))
        for aid, event_sid in handed.items():
            record = _newest_assignment(lambda r, aid=aid: r.get("assignment_id") == aid) or {}
            sid = record.get("claude_sid") or event_sid
            if _session_alive(sid):
                refuse(f"handed-off session {record.get('name') or sid} ({sid}) of {task_key} is alive: kill_worker "
                       "it, then call pipeline_close again")
        pending = _pending_question_ids(task_key)
        if pending:
            refuse(f"pending question(s) of {task_key}: {', '.join(pending)}. Answer each with answer_question, then "
                   "call pipeline_close again")
        gate = _gate_state(task_key)
        if gate and gate["state"] == "open":
            refuse(f"the {gate.get('node')} gate still runs (supervisor pid {gate.get('pid')}): end it with "
                   f"kill -TERM -- -{gate.get('pid')}, then call pipeline_close again")
        superseded = [{"request_id": r.get("request_id"),
                       "assignment_id": (r.get("pending") or {}).get("assignment_id")} for r in fold_.open_requests]
        release_work_slots(task_key)
        _append_task_event(task_key, {"type": "work_closed", "token_id": pipeline.DEFAULT_TOKEN_ID,
                                      "node": token.node, "reason": reason, "actor_sid": manager_sid})
        for tok in fold_.tokens.values():
            if tok.holder:
                _stamp_handed_off(task_key, tok.token_id, tok.holder)
    event_store.unsubscribe(task_key=task_key, reason="work closed")
    return {"ok": True, "state": "closed", "task_key": task_key, "node": token.node, "round": fold_.round,
            "superseded": superseded}


DEFAULT_SLOT_COUNTS: dict[str, int] = {"mvn": 5}

_slots_thread_lock = threading.Lock()


@contextmanager
def _slots_lock():
    paths.SLOTS.mkdir(parents=True, exist_ok=True)
    lock_path = paths.SLOTS / ".lock"
    with _slots_thread_lock:
        with open(lock_path, "w") as lockf:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


def _resolve_max_concurrent(category: str, max_concurrent: int | None) -> int:
    if max_concurrent is not None:
        return max_concurrent
    env_val = os.environ.get(f"CLAUDE_ORCH_SLOTS_{category.upper()}")
    if env_val is not None:
        try:
            return int(env_val)
        except ValueError:
            pass
    if category in DEFAULT_SLOT_COUNTS:
        return DEFAULT_SLOT_COUNTS[category]
    raise ValueError(
        f"no max_concurrent for category '{category}' — pass max_concurrent, "
        f"set CLAUDE_ORCH_SLOTS_{category.upper()}, or add a default to DEFAULT_SLOT_COUNTS"
    )


def _slot_path(category: str) -> Path:
    return paths.SLOTS / (urllib.parse.quote(category, safe="") + ".json")


def _write_slot(path: Path, category: str, cap: int, holders: list, waiters: list) -> None:
    state.write_json_atomic(path, {"category": category, "max_concurrent": cap, "holders": holders,
                                   "waiters": waiters})


def _evict_stale_holders(holders: list, now: float | None = None) -> list:
    now = time.time() if now is None else now
    fresh: list = []
    for h in holders:
        expires_at = h.get("expires_at")
        if expires_at is not None:
            if now < expires_at:
                fresh.append(h)
            continue
        sid = h.get("claude_sid")
        pid = h.get("pid", 0)
        if not sid:
            continue
        if not (paths.ACTIVE / f"{sid}.json").exists():
            continue
        if not _pid_alive(pid):
            continue
        fresh.append(h)
    return fresh


_HOLDER_FACTS = ("owner", "task_key", "node", "expires_at", "claude_sid")


def _without_wait(waiters: list, task_key: str | None, node: str | None = None, any_node: bool = False) -> list:
    return [w for w in waiters
            if not (w.get("task_key") == task_key and (any_node or w.get("node") == node))]


def acquire_worker_slot_impl(
    claude_sid: str,
    category: str,
    max_concurrent: int | None = None,
    timeout_sec: int = 1800,
    _poll_interval: float = 0.1,
    *,
    task_key: str | None = None,
    node: str | None = None,
    ttl_sec: int | None = None,
    wait: bool = True,
) -> dict:
    paths.ensure_dirs()
    cap = _resolve_max_concurrent(category, max_concurrent)
    slot_path = _slot_path(category)
    lease = task_key is not None or not wait
    owner = task_key or f"session:{claude_sid}"
    ttl = ttl_sec if ttl_sec is not None else config.pipeline_int("lease_ttl_sec", 7200)
    deadline = time.monotonic() + timeout_sec
    while True:
        with _slots_lock():
            now = time.time()
            data = state.read_json(slot_path) or {}
            before = data.get("holders") or []
            holders = _evict_stale_holders(before, now)
            waiters = data.get("waiters") or []
            mine = next((h for h in holders if lease and h.get("owner") == owner), None)
            if mine is not None:
                mine.update({"claude_sid": claude_sid, "pid": os.getpid(), "expires_at": now + ttl,
                             "node": node if node is not None else mine.get("node")})
                _write_slot(slot_path, category, cap, holders, _without_wait(waiters, task_key, any_node=True))
                return {"acquired": True, "slot_id": mine["slot_id"], "category": category, "max_concurrent": cap,
                        "holders_count": len(holders), "expires_at": mine["expires_at"], "renewed": True}
            if len(holders) < cap:
                slot_id = uuid.uuid4().hex
                expires_at = now + ttl if lease else None
                holders.append({
                    "slot_id": slot_id,
                    "claude_sid": claude_sid,
                    "acquired_at": now,
                    "pid": os.getpid(),
                    "owner": owner if lease else None,
                    "task_key": task_key,
                    "node": node if lease else None,
                    "expires_at": expires_at,
                })
                _write_slot(slot_path, category, cap, holders, _without_wait(waiters, task_key, any_node=True))
                result = {"slot_id": slot_id, "category": category, "max_concurrent": cap,
                          "holders_count": len(holders)}
                if lease:
                    return {"acquired": True, **result, "expires_at": expires_at, "renewed": False}
                return result
            if not wait:
                if task_key is not None and node is None:
                    raise ValueError(f"{category} is held and a work's lease wait needs node: the engine wakes the "
                                     "node the waiter names")
                if task_key is not None and not any(w.get("task_key") == task_key and w.get("node") == node
                                                    for w in waiters):
                    waiters = [*waiters, {"task_key": task_key, "node": node, "since": now}]
                _write_slot(slot_path, category, cap, holders, waiters)
                return {"acquired": False, "category": category,
                        "holders": [{k: h.get(k) for k in _HOLDER_FACTS} for h in holders]}
            if holders != before:
                _write_slot(slot_path, category, cap, holders, waiters)
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"could not acquire slot for category '{category}' within "
                f"{timeout_sec}s (cap={cap})"
            )
        time.sleep(_poll_interval)


def _slot_files() -> list[Path]:
    if not paths.SLOTS.is_dir():
        return []
    return [p for p in paths.SLOTS.iterdir() if p.suffix == ".json"]


def release_worker_slot_impl(slot_id: str) -> dict:
    paths.ensure_dirs()
    with _slots_lock():
        for p in _slot_files():
            data = state.read_json(p)
            if not isinstance(data, dict):
                continue
            holders = data.get("holders") or []
            new_holders = [h for h in holders if h.get("slot_id") != slot_id]
            if len(new_holders) != len(holders):
                data["holders"] = new_holders
                state.write_json_atomic(p, data)
                return {"released": True, "slot_id": slot_id, "found": True,
                        "category": data.get("category") or p.stem}
    return {"released": True, "slot_id": slot_id, "found": False}


def release_work_slots(task_key: str) -> dict:
    paths.ensure_dirs()
    released: list[dict] = []
    waits_removed = 0
    with _slots_lock():
        for p in _slot_files():
            data = state.read_json(p)
            if not isinstance(data, dict):
                continue
            holders = data.get("holders") or []
            waiters = data.get("waiters") or []
            kept = [h for h in holders if h.get("task_key") != task_key]
            kept_waits = _without_wait(waiters, task_key, any_node=True)
            if len(kept) == len(holders) and len(kept_waits) == len(waiters):
                continue
            category = data.get("category") or p.stem
            released.extend({"category": category, "slot_id": h.get("slot_id")}
                            for h in holders if h.get("task_key") == task_key)
            waits_removed += len(waiters) - len(kept_waits)
            _write_slot(p, category, data.get("max_concurrent") or 1, kept, kept_waits)
    return {"released": released, "waits_removed": waits_removed}


def lease_waits(task_key: str, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    out: list[dict] = []
    for p in _slot_files():
        data = state.read_json(p)
        if not isinstance(data, dict):
            continue
        mine = [w for w in data.get("waiters") or [] if w.get("task_key") == task_key]
        if not mine:
            continue
        free = len(_evict_stale_holders(data.get("holders") or [], now)) < (data.get("max_concurrent") or 1)
        category = data.get("category") or p.stem
        out.extend({"category": category, "node": w.get("node"), "since": w.get("since"), "free": free}
                   for w in mine)
    return out


def drop_lease_wait(task_key: str, category: str, node: str | None) -> bool:
    with _slots_lock():
        p = _slot_path(category)
        data = state.read_json(p)
        if not isinstance(data, dict):
            return False
        waiters = data.get("waiters") or []
        kept = _without_wait(waiters, task_key, node)
        if len(kept) == len(waiters):
            return False
        _write_slot(p, data.get("category") or category, data.get("max_concurrent") or 1,
                    data.get("holders") or [], kept)
        return True


def _resolve_task_key(task_key: str, ticket: str) -> str:
    key = task_key or ticket
    if not key:
        raise ValueError("task_key is required (the `ticket=` alias is deprecated)")
    return key


@mcp.tool()
def artifact_put(task_key: str = "", *, phase: str, name: str,
                 content_path: Annotated[str, Field(description=(
                     "An absolute path to a UTF-8 text file; the store copies its text as the body."))],
                 status: Annotated[Literal["partial", "complete"], Field(description=(
                     "partial while the work is in flight; complete when final."))],
                 writer_sid: Annotated[str, Field(description=(
                     "Your own session id; the record's single writer."))],
                 contract_hash: str | None = None,
                 read_set: Annotated[list[dict] | None, Field(description=(
                     "The upstream artifacts you built on, one {name, written_at, contract_hash} per "
                     "artifact."))] = None,
                 overwrite: Annotated[bool, Field(description=(
                     "Replace a record a put is otherwise refused for (see the description). Canonical use: a "
                     "successor finishing a dead worker's phase — a crashed worker's record stays for forensics, "
                     "and flipping it needs overwrite=True."))] = False,
                 scope: Annotated[Literal[pipeline.SCOPES], Field(description=(
                     "output for a pipeline node's declared outputs — the result carries warnings when one is put "
                     "with another scope; checkpoint for a node's declared checkpoints; internal (default) for "
                     "anything else."))] = "internal",
                 decided_by: Annotated[Literal[DECIDERS] | None, Field(description=(
                     "Decision records only: the channel the decision came through — \"manager\" for anything in "
                     "a [MANAGER] message, a relay of the engineer's words or gate verdict included; \"engineer\" "
                     "only for a pane message without that prefix, or a gate verdict the node read from the gate "
                     "itself."))] = None,
                 ticket: Annotated[str, _TICKET_ALIAS] = "") -> dict:
    """[WORKER] Publish an artifact for a task_key's phase from a file: the store copies the text of content_path
    as the body and takes no text. Atomic, single writer per (phase, name). Returns {ok, path, written_at, status}, plus archived_previous when a content-replacing write archived the previous file to
    <phase>.<name>.md.prev (latest only), and warnings when a pipeline node's declared output is put with another
    scope. Refused unless overwrite=True — ENFORCED — when the put would replace a record owned by another
    writer_sid, a file the store did not write with different content, or YOUR OWN finalized (non-partial) record
    with different content; when a byte-equal re-put would demote a final record (or a finished hand file) to
    partial; and for a sanitize-colliding (phase, name) pair. Your own partial-record updates and partial→complete
    flips need no flag."""
    return artifact_put_impl(_resolve_task_key(task_key, ticket), phase, name,
                             artifact_export.read_content_file(content_path),
                             status, writer_sid, contract_hash, read_set, overwrite, scope,
                             decided_by=decided_by)


@mcp.tool()
def artifact_get(task_key: str = "", *, phase: str, name: str,
                 reader_sid: Annotated[str | None, Field(description=(
                     "Your own session id: records an artifact_read event. Pipeline nodes must pass it."))] = None,
                 ticket: Annotated[str, _TICKET_ALIAS] = "") -> dict:
    """[WORKER/MANAGER] Read one artifact: its frontmatter stamp and content_path, a copy of the body in a temp
    folder outside the store — Read that file. path names the store file: never read it. A stamp without scope
    reads as scope='internal'. Raises when the artifact is absent."""
    key = _resolve_task_key(task_key, ticket)
    record = artifact_get_impl(key, phase, name, reader_sid)
    body = record.pop("content")
    return {**record, "content_path": str(artifact_export.export_body(key, phase, name, body))}


@mcp.tool()
def artifact_list(task_key: str = "", ticket: Annotated[str, _TICKET_ALIAS] = "") -> list[dict]:
    """[MANAGER/WORKER] List a task_key's artifacts — frontmatter stamps only, no bodies: the derived index. For a
    crashed worker (wait_for_worker → "exited"), read its brief and this list, then re-dispatch from the last
    complete artifact."""
    return artifact_list_impl(_resolve_task_key(task_key, ticket))


@mcp.tool()
def artifact_view(task_key: str = "", ticket: Annotated[str, _TICKET_ALIAS] = "") -> dict:
    """[MANAGER] Pretty whole-blackboard fold, every artifact's stamp and a body excerpt, written to a file: Read
    its content_path."""
    key = _resolve_task_key(task_key, ticket)
    return {"content_path": str(artifact_export.export_view(key, artifact_view_impl(key)))}


@mcp.tool()
def pipeline_status(task_key: str = "", ticket: Annotated[str, _TICKET_ALIAS] = "",
                    format: Annotated[str, Field(description=(
                        "\"text\" (default), or \"json\" for the same facts as one JSON object (version "
                        "1)."))] = "text") -> str:
    """[MANAGER] The per-key board, a pipeline replay: folds artifacts, assignments and events.jsonl,
    liveness-joined by claude_sid, including workers that published nothing. Call it after a takeover or recreate,
    and to find complete-but-unpublished gaps (done events carry artifacts_published). The `waiting_on:` line
    names what the work waits on: human:question | human:approval-dialog | human:gate | stalled:<node> |
    worker:<node> | external:time | the issue-tracker watcher's external: value | external:lease | external:merge | none."""
    task_key = _resolve_task_key(task_key, ticket)
    _prune_stale_assignments()
    _prune_stale_artifacts()
    return pipeline_status_impl(task_key, format)


@mcp.tool()
def pipeline_event(task_key: str = "", *,
                   type: Annotated[str, Field(description=(
                       "dispatch | phase_complete | note | publish | …; reserved engine event types "
                       "(pipeline_started, advance_*, token_override, artifact_read, …) are refused."))],
                   phase: str | None = None,
                   name: str | None = None, reason: str | None = None,
                   actor_sid: str | None = None, ticket: Annotated[str, _TICKET_ALIAS] = "") -> dict:
    """[MANAGER/WORKER] Append an audit event to the task_key's events.jsonl. Log your decisions on a keyed task
    with type="dispatch", "phase_complete" or "note". Raises ValueError for a reserved engine event type."""
    return pipeline_event_impl(_resolve_task_key(task_key, ticket), type, phase, name, reason, actor_sid)


@mcp.tool()
async def pipeline_start(manager_sid: ManagerSidRequired, task_key: str, schema: str, cwd: str, ask: str) -> dict:
    """[MANAGER] Start a work on a schema: validate `<[pipeline] schemas_dir>/<schema>.toml`, snapshot it into
    the work's artifact dir, publish `ask` as `context.ask` (scope output), and spawn the start node as your
    worker in `cwd`. Returns {ok, state: "committed"|"pending", request_id, next: {name, sid, assignment_id} |
    null}; a pending start stays open for reconciliation. Refused when the work already has a token. A node whose
    schema node lists mcp__dockwright__pipeline_start in extra_args (the intake sorter) may pass its own sid; the
    work starts under that node's manager."""
    if manager_sid == DAEMON_SID:
        raise ValueError("pipeline_start: manager_sid 'daemon' is refused here; the daemon starts works only from its "
                         "own pass ([schedules])")
    return await pipeline_start_impl(manager_sid, task_key, schema, cwd, ask)


@mcp.tool()
def topic_set(task_key: str, actor_sid: str,
              priority: Annotated[str | None, Field(description="P0-P3.")] = None,
              title: str | None = None,
              domain: str | None = None,
              status: Annotated[str | None, Field(description=(
                  "open|closed, for note/lane/fleet topics only."))] = None,
              blocked_by: Annotated[list[str] | None, Field(description=(
                  "Replaces the list of topic keys."))] = None,
              links: Annotated[dict[str, str | None] | None, Field(description=(
                  "Merges into {pr, ticket, worktree}, and null erases one. worktree is settable on "
                  "note/lane/fleet topics only, since a work's comes from its token."))] = None) -> dict:
    """[MANAGER] Change a topic card, `topic.json` in the task_key's artifact dir. An omitted field stays. A work
    started before cards existed gets its card built from its journal and written with the change. Appends a
    topic_set event naming the changed fields. Returns {ok, task_key, changed, topic}."""
    return topic_set_impl(task_key, actor_sid, priority, title, domain, status, blocked_by, links)


@mcp.tool()
async def topic_message(task_key: str,
                        text: Annotated[str, Field(description=(
                            "The message or the answer, verbatim; at most 16 KiB. Empty only for kind "
                            "priority."))] = "",
                        kind: Annotated[str, Field(description=(
                            "message (default), answer (needs question_id), priority (needs priority) or new "
                            "(needs domain)."))] = "message",
                        decided_by: Annotated[Literal[DECIDERS], Field(description=(
                            "\"manager\" (default) when the message comes through you — your own words or a "
                            "relay of the engineer's; \"engineer\" only for words he sent through his own channel, "
                            "such as the board."))] = "manager",
                        question_id: Annotated[str | None, Field(description=(
                            "kind answer: an open question of this topic."))] = None,
                        priority: Annotated[str | None, Field(description="kind priority: P0-P3.")] = None,
                        domain: Annotated[str | None, Field(description=(
                            "kind new: the domain whose live manager the intake sorter runs under."))] = None) -> dict:
    """[MANAGER] Write to a topic, then deliver. The record is written before any delivery. kind message records
    decision.msg-<UTC stamp> and delivers it as a `[TOPIC <record> from <decided_by>]` line: to the work's live
    session, a resume of its closed one, an engine restart of a dead one, a live worker of the topic, or a
    read-only topic agent. kind answer answers the topic's open question question_id and records decision.q-<id8>.
    kind priority sets the card priority. kind new starts the intake sorter for new work in domain (task_key is
    ignored). An undelivered message is queued: your reconcile pass retries it every 5 min, or every pass when
    each refusal was the engineer's presence at the pane, and pages MESSAGE_UNDELIVERED after an hour; the board
    inbox shows it at once. Returns {ok, task_key, record, route,
    delivered, reason}; ok means the record exists, delivered whether a session got it."""
    from .topic_message import deliver
    return await deliver(task_key, text, kind=kind, decided_by=decided_by, via="mcp", question_id=question_id,
                         priority=priority, domain=domain)


@mcp.tool()
async def pipeline_advance(
    claude_sid: OwnSid,
    task_key: str,
    to: Annotated[str, Field(description=(
        "A node of the work's schema snapshot reachable by an edge from the token's node, or \"?\" when the next "
        "node is unclear. The listed values are every schema's nodes, as of the server start; a refusal names the "
        "edges out of the node. From a node, to=\"?\" is refused: call ask_manager with the reason. From the "
        "manager, to=\"?\" with a reason marks the token held (state \"held\"): a live holder can still advance, "
        "and its commit ends the hold; the reconciler neither restarts a dead holder nor pages NODE_UNADVANCED or "
        "NODE_PARKED, and NODE_HELD pages once per hold. A hold stops restarts and pages, never a trigger edge. "
        "to=\"?\" with no reason releases the hold (state \"released\"); the token's next commit, override or "
        "rework also ends it."))],
    reason: Annotated[str | None, Field(description=(
        "The manager's evidence on a trigger move; required with override, and relayed to the user; with "
        "to=\"?\", the reason for the hold. On a redo edge: the finding, its link and the decision it breaks."))] = None,
    override: Annotated[bool, Field(description=(
        "Manager-only; needs reason. Moves the token to any node at index, makes you the work's owner, hands off "
        "the displaced holder — kill_worker it — and supersedes every open request: close the pane of each "
        "request it superseded (the assignment_id of its last advance_pending), never clear its prompt. Relay "
        "each override and its reason to the user. A token-holding node gets no manual successor: kill_worker it "
        "and the reconciler restarts it. Refused while rows of a fork live, and into a fork edge's target while "
        "plan.tasks has two or more rows: enter at the fork's source or earlier. An override into an approve "
        "trigger's source holds until a PR merges or closes after it, or the first open PR is ready; once every PR "
        "of the work has merged or closed it moves on at the first trigger poll that finds its holder idle."))] = False,
    index: Annotated[int | None, Field(description=(
        "Override only: the repeat index to enter the target at, an integer >= 1 (default 1)."))] = None,
    resnapshot: Annotated[bool, Field(description=(
        "Override only: re-read and re-validate the schema first."))] = False,
    token_id: Annotated[str | None, Field(description=(
        "The token the manager holds, releases or moves (default t1; a fork row is t1/<row>, listed by "
        "pipeline_status). A node never needs it."))] = None,
    cwd: Annotated[str | None, Field(description=(
        "An absolute path to an existing directory; moves the work there: the next node spawns in it, and every "
        "later node, restart, merge poll and rework uses it. This edge's exit checks still run in the old cwd, "
        "and a repeat node's plan.tasks entry that names its own cwd still wins over the moved one. Refused on a "
        "fork or join edge and on any row's advance."))] = None,
    row: Annotated[str | None, Field(description=(
        "Back edge only: the plan.tasks row the work goes back to, required when the target is a fork target and "
        "plan.tasks has two or more rows. That row reopens and the others stay done; its previous session resumes "
        "when it holds less than [pipeline] fresh_context_tokens, else a fresh one starts from its checkpoints. "
        "Refused past [pipeline] back_cap returns per row per round and once the row's PR has merged. When that "
        "row hands over, the work returns to the node that sent it back."))] = None,
) -> dict:
    """[WORKER/MANAGER] Move the work's token along an edge. A node calls it with its own sid as its last action
    before worker_done; the engine checks that you hold the token, that the edge exists, that your declared
    outputs are complete with scope output and that the edge's exit checks pass, then spawns the next node as a
    sibling under your manager and marks you handed off (artifact_put and pipeline_advance refuse you after
    that). The manager moves only trigger edges (reason = the evidence) unless override=True. A fork edge starts
    one row token per plan.tasks row, stage 1 first; a row's node advances within its row and leaves by the join
    edge, which spawns the join target once every row is done (state "joined" until then). A node a back edge
    reopened hands over to the node that sent it back.

    A redo edge (the footer's `Redo edges:` line) is the holder's only, after the engineer's answer: ask_manager with
    a first line `Redesign request`; the manager answers with answer_question(..., "redesign: yes\\n<his words>",
    decided_by="engineer"); then call this with to=<target> and reason. It opens round n+1 like pipeline_rework and
    returns round. Refused without that answer, while another session of the work lives or a question is pending,
    while a gate runs, and while an open PR of the round is not a draft or has merged. A back edge (the footer's
    `Back edges:` line) is the holder's: pass reason and, on a forked target, row.

    Returns {ok, state: "committed"|"pending"|"superseded"|"held"|"released"|"joined", request_id, next}. On
    "pending" the request stays open and you still call worker_done. "superseded" means the manager overrode the
    token while your spawn ran, so the session it started was closed: call worker_done. An advance that spawns two
    or more rows returns the first row's result plus rows, one result per row. A refusal raises with one fixable
    reason: a missing edge names the edges out of the node, a bad override target the snapshot's nodes."""
    return await pipeline_advance_impl(claude_sid, task_key, to, reason=reason, override=override, index=index,
                                       resnapshot=resnapshot, token_id=token_id, cwd=cwd, row=row)


@mcp.tool()
async def pipeline_child(
    claude_sid: OwnSid,
    task_key: str,
    child: Annotated[str, Field(description=(
        "A child slot your node declares; `dockwright schema show <schema>` lists each node's slots."))],
    role: Annotated[str | None, Field(description=(
        "A row of an old snapshot's slot; refused on a slot with lanes."))] = None,
    message: Annotated[str, Field(description=(
        "The child's brief on a start, the round's message on a later round. Name the report path in it."))] = "",
    lane: Annotated[str | None, Field(description=(
        "A lane of the slot; default its first. Another lane than the last round's closes that child and starts "
        "a new one. On a snapshot taken before roster-removal the slot has rows, and role= names one."))] = None,
    close: Annotated[bool, Field(description=(
        "End every leg of the slot that you started, once its last verdict is in."))] = False,
    cut: Annotated[str | None, Field(description=(
        "Ignored: a round past the slot's recommendation needs no cut. Removed in the next release."))] = None,
    codex_message: Annotated[str | None, Field(description=(
        "The Codex leg's text when it differs from message."))] = None,
) -> dict:
    """[WORKER] Start or continue a child your node's schema declares. Only the token holder calls it, and a node
    of a schema that declares children starts children only through this tool: spawn_child refuses it. Round 1
    starts the Claude leg on the lane's model and effort and, when the lane names a `codex` model and `[codex]
    allowed_roots` covers your cwd, the Codex leg on it (`codex` in the result says which). A later round of a warm
    slot goes to the same live child; a cold slot or a new lane gets a new one, and
    the round count goes on. A warm child that is gone refuses the call and spends nothing: call again with the full
    brief and the slot's earlier verdicts, which starts a new child. A gate verdict or a return to your node opens a new entry with a fresh
    count. Past the slot's `rounds` (its recommendation) the call runs and the result's note says so; `cut` is
    ignored. Wait for each leg with wait_for_child(<name>, parent_sid=<your sid>); a leg with confirmed: false
    needs get_child_tail(<name>) first. Publish each verdict yourself. Moving the token, a restart of your node and
    close=True end the children. Returns {ok, round, entry, recommended_rounds, legs: [{name, provider, action,
    confirmed}], codex, note?}, or {ok, closed: [names]} on close."""
    from .pipeline_child import pipeline_child_impl
    return await pipeline_child_impl(claude_sid, task_key, child, role, message, close, cut, codex_message, lane=lane)


@mcp.tool()
async def pipeline_rework(
    manager_sid: ManagerSidRequired,
    task_key: str,
    ask: Annotated[str, Field(description=(
        "What the round must change; it becomes context.rework-<n+1>, which every node of the round reads."))],
    to: Annotated[str | None, Field(description=(
        "The node the round re-enters: a node of the work's schema; omitted, the schema's rework_at. The listed "
        "values are every schema's nodes, as of the server start. On a schema with rework_at the start node is open "
        "only with ticket_changed=True; any other node is open on every schema. Entering a fork edge's target is "
        "refused while plan.tasks has two or more rows: enter at the fork's source or earlier."))] = None,
    inputs: Annotated[list[str] | None, Field(description=(
        "Artifact names the round carries and every node of it reads. decision.* records carry only when named "
        "here; a name that is a redone node's own output or checkpoint is refused."))] = None,
    schema: Annotated[str | None, Field(description=(
        "Moves the work onto another schema: the round snapshots it, and every later rework and resnapshot reads "
        "it."))] = None,
    cwd: Annotated[str | None, Field(description=(
        "An absolute path to an existing directory; runs the round there and moves the work on commit. Use it when "
        "the work's cwd is gone and `to` recreates the worktree."))] = None,
    ticket_changed: Annotated[bool, Field(description=(
        "The ticket itself changed: lets the round enter the start node of a schema with rework_at and prefixes the "
        "ask with `ticket_changed: yes`. Refused on a schema whose start node does not output context.ticket: such a "
        "work re-enters at a later node, and the manager creates the round's worktree and passes it as cwd."))] = False,
) -> dict:
    """[MANAGER] Rework a work: open its next round at node `to`, from wherever the token sits, a finished or closed
    work included. The schema is re-read and re-validated. Every artifact of the round moves to rounds/<n>/ except
    context.ask, context.rework-*, the outputs and checkpoints of nodes the round does not redo, and the names in
    inputs. Returns {ok, state, request_id, next, round}. After it, close the pane of each open request it
    superseded (the assignment_id of its last advance_pending), as after an override; never clear its prompt.
    Refused while any token's holder or a session of any open request is alive (kill_worker it first), while a
    question from a session of the work is pending (answer it first), while the work's cwd is gone (recreate the
    worktree, or pass cwd), while the PR of any node's implement.* output is open (changes on an open PR go
    through the node that holds the work's token; a gh failure refuses too),
    when ticket_changed=True names a start node without context.ticket, and when a redone node needs an input
    nothing carried or redone supplies: enter at an earlier node."""
    return await pipeline_rework_impl(manager_sid, task_key, to, ask, inputs, schema, cwd,
                                      ticket_changed=ticket_changed)


@mcp.tool()
def pipeline_close(
    manager_sid: ManagerSidRequired,
    task_key: str,
    reason: Annotated[str, Field(description=(
        "Why the work will never run again; pipeline_status prints it and every later refusal quotes it."))],
) -> dict:
    """[MANAGER] Close a work that will never run again. The reconciler stops restarting its nodes, settling its
    requests, paging about it and polling its PR; every pipeline_advance on it is refused (a move, an override, a
    hold or release, a redo edge); waiting_on reads none (human:question while a manager's ask to the engineer on the work is open); pipeline_status prints the close; its artifacts are pruned
    30 days later like a finished work's. The work's PR and directory are not touched. pipeline_rework reopens it
    once the PR of its round is merged or closed. Returns {ok, state: "closed", task_key, node, round, superseded}.
    After it, close the pane of each request it superseded (its assignment_id in superseded), never clear its
    prompt. Refused, writing nothing, while any token's holder, a handed-off session of the work or a session of
    any open request is alive (kill_worker it first), while a question from a session of the work is pending
    (answer it first), while a gate of the work runs (end it first), when the work is already closed, and for an
    empty reason."""
    return pipeline_close_impl(manager_sid, task_key, reason)


@mcp.tool()
def acquire_worker_slot(
    claude_sid: OwnSid,
    category: Annotated[str, Field(description=(
        "A free-form string (\"mvn\", \"npm\", \"docker-build\"); each category is an independent "
        "semaphore."))],
    max_concurrent: Annotated[int | None, Field(description=(
        "The category's cap. None falls back to env CLAUDE_ORCH_SLOTS_<CATEGORY>, then to a built-in "
        "default."))] = None,
    timeout_sec: int = 1800,
    task_key: Annotated[str | None, Field(description=(
        "The work that owns the slot: makes it a lease (see the docstring). Pass your [pipeline] footer's "
        "task_key."))] = None,
    node: Annotated[str | None, Field(description=(
        "Your pipeline node; recorded on the holder and on the waiter the engine wakes."))] = None,
    ttl_sec: Annotated[int | None, Field(description=(
        "A lease's lifetime in seconds. None: [pipeline] lease_ttl_sec, default 7200."))] = None,
    wait: Annotated[bool, Field(description=(
        "False: never block; a held category returns {acquired: false, holders} at once. False without "
        "task_key makes a session lease (owner session:<sid>, no waiter)."))] = True,
) -> dict:
    """[WORKER] Block until a slot for category is free, then return a slot_id. Use it before running
    memory-heavy commands (mvn test, gradle test, big docker builds) so N concurrent workers don't OOM the host.
    Always pair it with release_worker_slot(slot_id). With task_key the slot is a lease of the work: it survives
    the session, expires after ttl_sec (default [pipeline] lease_ttl_sec, 7200), a repeat call by the same owner
    renews it (same slot_id, renewed: true), and with wait=False a held category answers {acquired: false,
    holders: [{owner, task_key, node, expires_at, claude_sid}]} at once and records the work as a waiter: the
    engine wakes the node with `lease: free — <category>` once the lease frees; without node that call is refused on a
    held category, because no node could be woken. wait=False without task_key is a
    session lease (owner session:<sid>, kept by its expiry even for an unregistered session, no waiter). A
    lease's response always carries acquired."""
    return acquire_worker_slot_impl(claude_sid, category, max_concurrent, timeout_sec, task_key=task_key,
                                    node=node, ttl_sec=ttl_sec, wait=wait)


@mcp.tool()
def release_worker_slot(slot_id: str) -> dict:
    """[WORKER] Release a previously acquired slot. Idempotent — safe to call twice."""
    return release_worker_slot_impl(slot_id)


def _subscribing_token(tool, claude_sid, task_key, node):
    caller, _, _ = _caller_for(claude_sid)
    token = _held_token(_fold_for(task_key), caller.assignment_id)
    if token is None or token.node != node:
        raise ValueError(f"{tool}: you hold no token of {task_key} on {node}")
    return token


def event_subscribe_impl(claude_sid, task_key, node, source, match, kinds=None, after=None, deadline=None,
                         handler=None, skip_own=None) -> dict:
    _subscribing_token("event_subscribe", claude_sid, task_key, node)
    sub = event_store.subscribe(task_key, node, source, dict(match or {}), kinds=kinds, after=after,
                                deadline=deadline, handler=handler, skip_own=skip_own)
    return {"ok": True, "sub_id": sub.sub_id, "source": sub.source, "after": sub.after, "deadline": sub.deadline}


def event_unsubscribe_impl(claude_sid, task_key, node, sub_id=None) -> dict:
    _subscribing_token("event_unsubscribe", claude_sid, task_key, node)
    return {"ok": True, "removed": event_store.unsubscribe(sub_id, task_key=task_key, node=node,
                                                           reason="unsubscribed")}


@mcp.tool()
def event_subscribe(
    claude_sid: Annotated[str, Field(description="Your own session id, as your session context names it — never a name.")],
    task_key: str,
    node: Annotated[str, Field(description="Your pipeline node; the subscription dies when the token leaves it.")],
    source: Annotated[Literal["tracker", "deploy", "time", "chat"], Field(description=(
        "tracker: comments and status of one issue (--match issue=<KEY>; after is required). deploy: the GitHub "
        "Actions deploy job of one commit or run on staging or dev (match repo=<owner>/<repo>, env=staging|dev|prod, "
        "sha=<sha40> or run_id=<id>); env=prod reads GitHub Deployments of [events.sources.github].deployed_envs and "
        "needs sha (run_id is refused), emits only success. time: a deadline alone (match {}). chat: a chat app's messages, "
        "from the chat archive when `[chat_archive]` is enabled and from the macOS notification store otherwise "
        "(match any of app=<bundle id>, conversation=<#channel or DM name; every "
        "channel is #name, private ones included>, sender=<display name>, channel=<conversation id>, thread=<parent "
        "message ts>; after is refused and set to the subscription time, so only messages sent after it "
        "wake you; the polling process needs Full Disk Access; channel, thread and the thread kinds come from the "
        "chat archive and are empty on a banner event)."))],
    match: Annotated[dict[str, str], Field(description="The selector the event must carry, key by key.")],
    kinds: Annotated[list[str] | None, Field(description=(
        "The kinds to wake on (tracker: comment, status, sprint-assigned; deploy: job; time: deadline; chat: dm, "
        "channel, dm-thread, channel-thread). None: all."))] = None,
    after: Annotated[str | None, Field(description=(
        "The cursor to start after: the last comment id you have read. Required for tracker."))] = None,
    deadline: Annotated[float | None, Field(description=(
        "Unix seconds; when it passes the time source wakes you with event: time.deadline and ends the "
        "subscription."))] = None,
    handler: Annotated[list[str] | None, Field(description=(
        "argv the daemon runs with the batch as JSON on stdin before waking you; argv[0] is a name from [events] "
        "handlers found in handler_path (a path is refused). Exit 0 = handled, no wake; any other exit wakes you "
        "with (handler failed: <code>)."))] = None,
    skip_own: Annotated[str | None, Field(description=(
        "An author id (the tracker's account id) whose events never wake you: your own posts."))] = None,
) -> dict:
    """[WORKER] Record what your node waits for and park: the daemon polls the source and the engine wakes you
    once with one pane line `event: <source>.<kind> — <summary> (sub <id>)` carrying every new event of the
    pass (a lease line rides the same batch). Only the token holder subscribes, and only on its own node; a
    fork row cannot. A repeat with the same (task_key, node, source, match) replaces the earlier subscription; a
    chat repeat keeps the earlier one's window and delivered mark.
    A deploy.job or time.deadline wake ends its subscription; a tracker or chat one lives until you unsubscribe,
    the node hands over, or the work closes. Refused (ValueError with the reason) while [events] enabled is false,
    for env=prod without sha or with run_id, for a tracker subscription without after or while the [events] env_file is missing, not mode 0600
    or lacks a TRACKER_* key, for a chat subscription with after, a key outside app, conversation, sender, channel and
    thread, or an empty [events.sources.chat] apps, and for a handler that is not a listed name."""
    return event_subscribe_impl(claude_sid, task_key, node, source, match, kinds=kinds, after=after,
                                deadline=deadline, handler=handler, skip_own=skip_own)


@mcp.tool()
def event_unsubscribe(
    claude_sid: Annotated[str, Field(description="Your own session id, as your session context names it — never a name.")],
    task_key: str,
    node: str,
    sub_id: Annotated[str | None, Field(description="One subscription; None removes every one of your node.")] = None,
) -> dict:
    """[WORKER] Remove your node's subscriptions (one by sub_id, or all of them). Returns {ok, removed}. Refused
    unless you hold the work's token on node."""
    return event_unsubscribe_impl(claude_sid, task_key, node, sub_id=sub_id)


def _queue_rule(tool: str, rule: str) -> None:
    rules = config.events_rules()
    if rule not in rules:
        raise ValueError(f"{tool}: unknown rule {rule}")
    if rules[rule]["action"] not in ("queue", "reply"):
        raise ValueError(f"{tool}: {rule} is not a queue or reply rule (action = {rules[rule]['action']})")


def event_queue_impl(rule: str) -> dict:
    _queue_rule("event_queue", rule)
    return {"ok": True, "rule": rule, "pending": [e.to_dict() for e in event_queue_store.read(rule)]}


def event_ack_impl(rule: str, event_id: str) -> dict:
    _queue_rule("event_ack", rule)
    if event_id == "*" and config.events_rules()[rule]["action"] == "reply":
        raise ValueError(f"event_ack: {rule} is a reply rule; close its records one record at a time, by id")
    removed, remaining = event_queue_store.ack(rule, event_id)
    return {"ok": True, "rule": rule, "removed": removed, "remaining": remaining}


@mcp.tool()
def event_queue(
    rule: Annotated[str, Field(description='A [events.rules.<name>] with action = "queue" or "reply".')],
) -> dict:
    """[WORKER/MANAGER] Read a queue or reply rule's pending events, oldest first; the manager reads, no token needed.
    Returns {ok, rule, pending: [event…]}, each event whole (id, ts, source, kind, match, summary, payload).
    Refused (ValueError) for a rule name [events.rules] does not hold."""
    return event_queue_impl(rule)


@mcp.tool()
def event_ack(
    rule: Annotated[str, Field(description='A [events.rules.<name>] with action = "queue" or "reply".')],
    event_id: Annotated[str, Field(description='The event id to close, or "*" for every pending event of a queue rule.')],
) -> dict:
    """[WORKER/MANAGER] Close one pending event of a queue or reply rule, or all of a queue rule's with "*". Returns {ok,
    rule, removed, remaining}. An id the queue does not hold removes nothing. Refused (ValueError) for a rule name
    [events.rules] does not hold, and for "*" on a reply rule: its records belong to several sessions."""
    return event_ack_impl(rule, event_id)


@mcp.tool()
def chat_read(
    after: Annotated[str, Field(description="An ISO date (local midnight) or datetime, a chat ts, or <N>d / <N>h ago.")],
    before: Annotated[str | None, Field(description="Same forms; None reads to the newest message.")] = None,
    conversation: Annotated[str | None, Field(description="One conversation id.")] = None,
    thread: Annotated[str | None, Field(description="One thread: its parent ts.")] = None,
    bots: Annotated[bool, Field(description="False hides app senders.")] = True,
    limit: Annotated[int, Field(description="Messages at most; truncated says when more exist.")] = 500,
) -> dict:
    """[WORKER/MANAGER] Read the chat archive by date range: the operator's direct and group conversations and the
    threads that mention them, grouped by conversation (kind dm, group or channel) in ts order. archive is "ok",
    "off" (the archive is disabled), "empty" (nothing archived yet) or "stale" (a search step of the pass has not committed within max(15 min, 3 × every_sec)): on anything
    but "ok" read the chat app directly; since is when the archive started, and earlier days are not in it.
    Text from the archive never goes into a PR, a tracker comment, a post or any artifact another person reads."""
    from . import chat_archive
    return chat_archive.read(after, before=before, conversation=conversation, thread=thread, bots=bots, limit=limit)


@mcp.tool()
def chat_search(
    query: Annotated[str, Field(description="An FTS5 query: words, \"quoted phrases\", NEAR().")],
    after: str | None = None,
    before: str | None = None,
    conversation: str | None = None,
    sender: Annotated[str | None, Field(description="A sender id or display name.")] = None,
    limit: int = 50,
) -> dict:
    """[WORKER/MANAGER] Search the chat archive by word, ranked by bm25; a bad query answers {hits: [], error}.
    Text from the archive never goes into a PR, a tracker comment, a post or any artifact another person reads."""
    from . import chat_archive
    return chat_archive.search(query, after=after, before=before, conversation=conversation, sender=sender, limit=limit)


@mcp.tool()
def chat_conversations(
    kind: Annotated[str | None, Field(description="dm, group or channel; None lists all.")] = None,
    since: Annotated[str | None, Field(description="Only conversations with a message after this (same forms as chat_read).")] = None,
) -> dict:
    """[WORKER/MANAGER] List the archived conversations with their kind, name, last message ts and message count.
    Text from the archive never goes into a PR, a tracker comment, a post or any artifact another person reads."""
    from . import chat_archive
    return chat_archive.conversations(kind=kind, since=since)


def main() -> None:
    try:
        from .spawner import write_registry_snapshot
        write_registry_snapshot()
    except Exception:
        pass
    tool_schema.advertise(mcp)
    mcp.run()

if __name__ == "__main__":
    main()
