import asyncio
import concurrent.futures
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from mcp.server.fastmcp import FastMCP
from . import config, identity, names, paths, state
from .state import _pid_alive
from .terminal import get_driver
from .transcript import (find_session_log, is_delegating, last_assistant_summary,
                         last_assistant_ends_in_tool_use)
from .registry import (
    _drop_questions_for_worker,
    _prune_stale_active_records,
    _question_paths,
    _resolve_unique_name,
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

mcp = FastMCP("dockwright")

DEFAULT_DOMAIN = paths.DEFAULT_DOMAIN

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
        if agent == "worker" and record.get("parent_manager_name") is None:
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
        state.write_json_atomic(p, record)
        count += 1
    print(
        f"backfill: stamped parent_manager_name={only_manager_name!r} on "
        f"{count} legacy worker record(s)",
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

def _find_question_path(question_id: str) -> Any:
    for q_path in _question_paths():
        if q_path.stem == question_id:
            return q_path
        record = state.read_json(q_path)
        if record is not None and record.get("question_id") == question_id:
            return q_path
    return None

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
) -> dict:
    paths.ensure_dirs()
    _prune_stale_active_records()
    for record in state.list_json_in(paths.ACTIVE):
        if record.get("name") == name and record.get("claude_sid") != claude_sid:
            raise ValueError(f"name '{name}' is taken by session {record.get('claude_sid')}")
    if pid is None:
        pid = os.getppid()
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
        "account": account,
    }
    if agent in ("manager", "worker"):
        record["runtime"] = runtime or "claude"
        record["terminal"] = "tmux"
    state.write_json_atomic(paths.ACTIVE / f"{claude_sid}.json", record)
    return {"ok": True}

def _matches_manager(record: dict, manager_name: str | None) -> bool:
    if manager_name is None:
        return True
    return record.get("parent_manager_name") == manager_name

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


def list_workers_impl(manager_name: str | None = None) -> list[dict]:
    _prune_stale_active_records()
    workers = []
    for record in state.list_json_in(paths.ACTIVE):
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
            "alive": _pid_alive(record.get("pid", 0)),
        }
        worker["runtime"] = record.get("runtime") or "claude"
        worker["brief"] = _assignment_brief_for_sid(record.get("claude_sid"))
        worker["spend"] = _format_spend(record.get("spend"))
        if log is not None and record.get("state") == "idle" and is_delegating(record, time.time(), log=log):
            worker["state"] = "processing"
            worker["delegating"] = True
        workers.append(worker)
    return workers

def _write_question(
    worker_sid: str,
    worker_name: str,
    question: str,
    parent_manager_name: str | None = None,
) -> str:
    qid = uuid.uuid4().hex
    question_dir = paths.question_dir_for(parent_manager_name)
    state.write_json_atomic(question_dir / f"{qid}.json", {
        "question_id": qid,
        "worker_sid": worker_sid,
        "worker_name": worker_name,
        "parent_manager_name": parent_manager_name,
        "question": question,
        "asked_at": time.time(),
    })
    return qid

ASK_MANAGER_TIMEOUT_SEC = 1500


def _reask_sentinel(qid: str, timeout_sec: float) -> str:
    return (
        f"NO_ANSWER_YET: the manager has not answered within {timeout_sec:.0f}s. "
        f"Your question is still pending (question_id: {qid}). To keep waiting, call "
        f"ask_manager again with the same claude_sid and question text plus "
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
        return data["answer"]
    answer_path.unlink(missing_ok=True)
    return None


async def ask_manager_impl(
    claude_sid: str,
    question: str,
    poll_interval: float = 0.5,
    timeout_sec: float = ASK_MANAGER_TIMEOUT_SEC,
    resume_question_id: str | None = None,
) -> str:
    record = state.read_json(paths.ACTIVE / f"{claude_sid}.json")
    if record is None:
        raise ValueError(f"session {claude_sid} not registered; SessionStart hook missing?")
    if record.get("nested"):
        raise ValueError(
            f"session {claude_sid} is a nested sub-session of "
            f"{record.get('nested_parent_name') or 'another session'}; ask_manager is "
            "disabled for nested sessions — surface the question to the parent process instead"
        )
    if resume_question_id is None:
        qid = _write_question(
            worker_sid=claude_sid,
            worker_name=record["name"],
            question=question,
            parent_manager_name=record.get("parent_manager_name"),
        )
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
    while True:
        answer = _try_consume_answer(qid, claude_sid)
        if answer is not None:
            return answer
        if time.monotonic() >= deadline:
            return _reask_sentinel(qid, timeout_sec)
        await asyncio.sleep(poll_interval)

def answer_question_impl(question_id: str, text: str) -> dict:
    q_path = _find_question_path(question_id)
    if q_path is None or not q_path.exists():
        raise ValueError(f"no pending question with id {question_id}")
    question = state.read_json(q_path)
    payload = {
        "question_id": question_id,
        "answer": text,
        "answered_at": time.time(),
    }
    if question is not None and question.get("worker_sid"):
        payload["worker_sid"] = question["worker_sid"]
    state.write_json_atomic(paths.ANSWERS / f"{question_id}.json", payload)
    q_path.unlink(missing_ok=True)
    return {"ok": True}

def list_pending_questions_impl(manager_name: str | None = None) -> list[dict]:
    questions = []
    for q_path in _question_paths():
        question = state.read_json(q_path)
        if question is not None and _matches_manager(question, manager_name):
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

def _capture_text(window_id: str) -> str | None:
    return get_driver().capture_screen_ansi(window_id)


_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")


def _strip_ansi(text: str) -> str:
    return _SGR_RE.sub("", text)


def _strip_dim_spans(text: str) -> str:
    out: list[str] = []
    pos = 0
    faint = False
    for m in _SGR_RE.finditer(text):
        if not faint:
            out.append(text[pos:m.start()])
        for param in m.group(1).split(";"):
            if param == "2":
                faint = True
            elif param in ("", "0", "22"):
                faint = False
        pos = m.end()
    if not faint:
        out.append(text[pos:])
    return "".join(out)


def _input_is_idle(screen_text: str | None) -> bool:
    if not screen_text:
        return False
    if "Press up to edit queued messages" in _strip_ansi(screen_text):
        return False
    caret_lines = [line for line in screen_text.splitlines() if "❯" in line]
    if not caret_lines:
        return False
    after_caret = caret_lines[-1].split("❯", 1)[1]
    after_caret = _strip_dim_spans(after_caret)
    after_caret = after_caret.strip().strip("│|").strip()
    return after_caret == ""


def _send_text(window_id: str, text: str) -> None:
    get_driver().send_text(window_id, text)


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


def send_manager_to_worker_impl(worker: str, text: str) -> dict:
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
    _send_text(window_id, MANAGER_MARKER + text)
    record["tasked_at"] = time.time()
    sid = record.get("claude_sid")
    if sid:
        state.write_json_atomic(paths.ACTIVE / f"{sid}.json", record)
    return {"status": "delivered", "worker": record["name"]}


async def send_manager_to_worker_auto_impl(
    worker: str,
    text: str,
    _registration_timeout_sec: float = 10.0,
    _poll_interval: float = 0.5,
) -> dict:
    try:
        return send_manager_to_worker_impl(worker, text)
    except ValueError as send_err:
        try:
            _closed_path, closed_record = _find_closed_record_by_name(worker)
        except ValueError as probe_err:
            raise ValueError(f"{send_err} (auto_resume: {probe_err})") from send_err
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
        out = send_manager_to_worker_impl(name, text)
    except ValueError as deliver_err:
        raise ValueError(
            f"worker '{name}' WAS resumed (sid={result.get('sid')}) but delivery "
            f"failed: {deliver_err}; retry send_manager_to_worker"
        ) from deliver_err
    out["resumed"] = True
    out["sid"] = result.get("sid")
    return out


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
        _send_text(window_id, _peer_marker(sender) + text)
        return {"status": "delivered_live", "manager": name,
                "sender": (sender or {}).get("name") or None}
    return {"status": "peer_busy", "delivered": False, "manager": name}

def kill_worker_impl(worker: str, dry_run: bool = False) -> dict:
    record = _find_worker_by_name_or_sid(worker)
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

def get_worker_summary_impl(worker: str) -> dict:
    record = _find_worker_by_name_or_sid(worker)
    sid = record["claude_sid"]
    log = find_session_log(sid, runtime=record.get("runtime") or "claude")
    alive = _pid_alive(record.get("pid", 0))
    if log is None:
        return {
            "name": record["name"],
            "summary": None,
            "last_turn_at": None,
            "alive": alive,
            "error": "transcript not found",
        }
    summary, ts = last_assistant_summary(log, max_chars=sys.maxsize)
    return {
        "name": record["name"],
        "summary": summary,
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
            "parent_manager_name": assignment.get("parent_manager_name"),
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
    done_dir = paths.done_dir_for(record.get("parent_manager_name"))
    done_dir.mkdir(parents=True, exist_ok=True)
    done_event = {
        "event_id": event_id,
        "claude_sid": claude_sid,
        "worker_name": record.get("name"),
        "parent_manager_name": record.get("parent_manager_name"),
        "summary": summary,
        "spend": _spend_totals(record.get("spend")),
        "completed_at": time.time(),
    }
    published = _published_count(claude_sid)
    if published is not None:
        done_event["ticket"], done_event["artifacts_published"] = published
    if self_healed:
        done_event["self_healed"] = True
    state.write_json_atomic(done_dir / f"{claude_sid}-{event_id}.json", done_event)
    result = {"ok": True, "event_id": event_id}
    if self_healed:
        result["self_healed"] = True
    return result

_RETASK_GRACE_SEC = 2.0

async def wait_for_worker_impl(
    name: str,
    timeout_sec: int = 3600,
    _poll_interval: float = 1.0,
    manager_name: str | None = None,
) -> dict:
    paths.ensure_dirs()

    sid = None
    found_via_active = False
    manager_holder = None
    active_record = None
    for record in state.list_json_in(paths.ACTIVE):
        if record.get("agent") != "worker":
            if record.get("name") == name:
                manager_holder = record
            continue
        if record.get("name") == name and _matches_manager(record, manager_name):
            sid = record.get("claude_sid")
            found_via_active = True
            active_record = record
            break
    if sid is None:
        for record in state.list_json_in(paths.CLOSED):
            if record.get("name") == name and _matches_manager(record, manager_name):
                sid = record.get("claude_sid")
                break

    min_completed_at = 0.0
    if active_record is not None:
        candidates = [active_record.get("tasked_at") or 0]
        if active_record.get("state") == "processing":
            candidates.append(active_record.get("processing_since") or 0)
        bound = max(candidates)
        if bound:
            min_completed_at = bound - _RETASK_GRACE_SEC

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
            if (event.get("completed_at") or 0) < min_completed_at:
                continue
            if sid is not None and event.get("claude_sid") == sid:
                matching.append(event)
            elif sid is None and event.get("worker_name") == name:
                matching.append(event)
        if not matching:
            return None
        matching.sort(key=lambda e: e.get("completed_at", 0), reverse=True)
        return matching[0]

    def _done_response(event: dict) -> dict:
        return {
            "found": "done",
            "name": name,
            "sid": event.get("claude_sid"),
            "summary": event.get("summary"),
            "event_id": event.get("event_id"),
            "completed_at": event.get("completed_at"),
        }

    initial_done = _latest_done_event()
    if initial_done is not None:
        return _done_response(initial_done)

    if sid is None:
        if manager_holder is not None:
            raise ValueError(
                f"'{name}' is held by an active "
                f"{manager_holder.get('agent') or 'session'}, not a worker — "
                f"wait_for_worker only waits on workers"
            )
        raise ValueError(f"no worker named '{name}'")

    if not found_via_active:
        return {
            "found": "exited",
            "name": name,
            "sid": sid,
            "reason": "session_ended_without_worker_done",
        }

    deadline = time.monotonic() + timeout_sec
    while True:
        done_event = _latest_done_event()
        if done_event is not None:
            return _done_response(done_event)
        if not (paths.ACTIVE / f"{sid}.json").exists():
            return {
                "found": "exited",
                "name": name,
                "sid": sid,
                "reason": "session_ended_without_worker_done",
            }
        if time.monotonic() >= deadline:
            raise TimeoutError(f"worker '{name}' did not complete within {timeout_sec}s")
        await asyncio.sleep(_poll_interval)

def attach_existing_impl(manager_name: str | None = None) -> dict:
    return {
        "workers": list_workers_impl(manager_name=manager_name),
        "orphan_questions": list_pending_questions_impl(manager_name=manager_name),
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
            "parent_manager_name": record.get("parent_manager_name"),
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
    pid = os.getppid()
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
        cwd=os.getcwd(),
        iterm_sid=iterm_sid,
        pid=pid,
        domain=domain,
        runtime="claude",
    )
    _backfill_legacy_workers()
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
    workers_snapshot = list_workers_impl(manager_name=manager_name)
    questions_snapshot = list_pending_questions_impl(manager_name=manager_name)
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
        "questions_snapshot": list_pending_questions_impl(manager_name=manager_name),
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
            if record.get("agent") != "worker" or record.get("parent_manager_name") != registered_name:
                continue
            worker_sid = record.get("claude_sid")
            if not worker_sid:
                continue
            record["parent_manager_name"] = None
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


def _resolve_parent_manager(manager_sid: str | None) -> str | None:
    if not manager_sid:
        return None
    record = state.read_json(paths.ACTIVE / f"{manager_sid}.json")
    if record is None:
        raise ValueError(
            f"spawn_worker: manager_sid {manager_sid!r} does not match any active "
            "manager record — the worker would register UNSCOPED "
            "(parent_manager_name=null) and its done/question/turn-end events "
            "would never route back to you. Pass your own session UUID (the "
            "claude_sid from your boot context), not the funny name; "
            "list_managers() shows live manager sids — if it shows none, your "
            "own registration is gone: re-run become_manager. For a "
            "deliberately unscoped spawn pass manager_sid=None."
        )
    if record.get("agent") != "manager" or record.get("nested"):
        kind = ("nested manager-agent" if record.get("agent") == "manager"
                else (record.get("agent") or "session"))
        raise ValueError(
            f"spawn_worker: manager_sid {manager_sid!r} belongs to an active "
            f"{kind} record ({record.get('name')!r}), not a live top-level "
            "manager — the worker would be parented to a name no manager "
            "watches. Pass YOUR manager session UUID (list_managers() shows "
            "live sids), or manager_sid=None for a deliberately unscoped spawn."
        )
    name = record.get("name")
    if not name:
        raise ValueError(
            f"spawn_worker: manager_sid {manager_sid!r} resolves to a manager "
            f"record with no name ({name!r}) — the registration is incomplete "
            "or corrupt, and the worker would register UNSCOPED "
            "(parent_manager_name=null) with its events routed to buckets no "
            "manager watches. Re-run become_manager to rewrite your "
            "registration, or pass manager_sid=None for a deliberately "
            "unscoped spawn."
        )
    return name


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
async def ask_manager(claude_sid: str, question: str, resume_question_id: str | None = None) -> str:
    """[WORKER] Ask the manager a question; the manager relays it to the human and
    the answer is returned. Waits up to ~25 minutes server-side. If unanswered by
    then, returns a NO_ANSWER_YET sentinel naming your question_id — the question
    is STILL pending with the manager; to keep waiting WITHOUT duplicating it, call
    ask_manager again with the same claude_sid and question plus
    resume_question_id="<question_id>". Never proceed without the answer."""
    return await ask_manager_impl(claude_sid, question, resume_question_id=resume_question_id)

@mcp.tool()
def worker_done(claude_sid: str, summary: str) -> dict:
    """[WORKER] Signal the manager that this worker has completed its task. Writes a one-shot done event."""
    return worker_done_impl(claude_sid, summary)

@mcp.tool()
async def wait_for_worker(name: str, timeout_sec: int = 3600, manager_sid: str | None = None) -> dict:
    """[WORKER] Block until the named worker completes (worker_done) or its session exits.

    `manager_sid` (optional) is the caller's own claude_sid; passing it scopes the
    lookup to workers owned by the same manager. Default None = wildcard match
    (back-compat).
    """
    return await wait_for_worker_impl(name, timeout_sec, manager_name=_resolve_manager_name_for_filter(manager_sid, "wait_for_worker"))

@mcp.tool()
def answer_question(question_id: str, text: str) -> dict:
    """[MANAGER] Answer a pending worker question."""
    return answer_question_impl(question_id, text)

@mcp.tool()
def list_pending_questions(manager_sid: str | None = None) -> list[dict]:
    """[MANAGER] List worker questions waiting for an answer, oldest first.

    `manager_sid` is the caller's own claude_sid; passing it filters to questions
    owned by this manager. Default None = return all (back-compat).

    Null-parent (legacy) questions are INVISIBLE to scoped calls under strict
    routing — pass `manager_sid=None` to see them, or boot a single manager
    so `_backfill_legacy_workers` adopts the orphans.
    """
    return list_pending_questions_impl(manager_name=_resolve_manager_name_for_filter(manager_sid, "list_pending_questions"))

@mcp.tool()
def list_workers(manager_sid: str | None = None) -> list[dict]:
    """[MANAGER] List worker sessions. Pass `manager_sid` (caller's sid) to filter
    to this manager's own workers; default returns all (back-compat).
    """
    return list_workers_impl(manager_name=_resolve_manager_name_for_filter(manager_sid, "list_workers"))

@mcp.tool()
async def send_manager_to_worker(worker: str, text: str, auto_resume: bool = False) -> dict:
    """[MANAGER] Send an instruction to a worker. Types the message content directly
    into the worker's pane prefixed `[MANAGER] ` — workers use the marker to tell a
    manager relay from the engineer typing directly into their pane, so do NOT
    hand-prepend a marker of your own. The terminal buffers the text if the worker
    is mid-turn; it submits on the worker's next idle. Resolves the live pane via
    the terminal driver, stamping the discovered id back onto the worker record.
    No inbox file is ever written. Returns {status: "delivered", worker}.

    auto_resume=False (default): RAISES if the worker has no live window
    (dead/closed) — there is NO silent inbox, so a failed send is a hard error:
    resume_worker or re-spawn, don't assume it was queued.

    auto_resume=True: on a failed live send, if a closed record with a resumable
    transcript exists under this NAME (sid identifiers get the live path only),
    resumes it — new tab in the original cwd via `claude --resume`/`codex resume`,
    full prior conversation restored — waits briefly for the TUI to accept input,
    and delivers in the same call; the result also carries {resumed: true, sid}.
    Delivers to the resumed session's ACTUAL registered handle (can come back
    suffixed; use the returned worker name for follow-ups). Resumes the NEWEST
    resumable closed record. Still RAISES when nothing is resumable (never
    existed / no transcript / name held by an active session / registration
    timeout) — the no-silent-inbox contract is unchanged."""
    if auto_resume:
        return await send_manager_to_worker_auto_impl(worker, text)
    return send_manager_to_worker_impl(worker, text)


@mcp.tool()
def send_manager_to_manager(name: str, text: str) -> dict:
    """[MANAGER] Message a peer manager by name. If the peer's input box is idle, types
    the message content directly into their pane (status delivered_live), prefixed
    `[MANAGER <your name> · <your domain>] ` so the peer can tell a peer relay from the
    engineer typing into their pane — do NOT hand-prepend a marker of your own. The
    sender is resolved from YOUR OWN active manager record, not from an argument;
    when it cannot be resolved the prefix degrades to `[MANAGER] ` and the returned
    `sender` is null. Because of the prefix a leading slash arrives as plain text and
    never triggers harness slash expansion — spell the ask out. If a human is
    mid-typing, does NOT type and returns peer_busy (delivered=False) so it never
    clobbers the peer's input — retry when the peer is free; there is NO silent inbox.
    RAISES if the peer has no live window (dead/closed) or an unreadable one — a failed
    send is a hard error, not a queue. Resolve peer names via list_managers(). Returns
    {status, manager, sender} (delivered_live) or {status, delivered, manager} (peer_busy)."""
    return send_manager_to_manager_impl(name, text)

@mcp.tool()
def kill_worker(worker: str) -> dict:
    """[MANAGER] Terminate a worker session by closing its terminal pane."""
    return kill_worker_impl(worker, dry_run=False)

@mcp.tool()
def get_worker_summary(worker: str) -> dict:
    """[MANAGER] Return the full un-truncated last assistant summary for a worker."""
    return get_worker_summary_impl(worker)

@mcp.tool()
def get_worker_tail(worker: str, lines: int = 50) -> dict:
    """[MANAGER] Return the last N entries from the worker's transcript .jsonl (role + 200-char content preview)."""
    return get_worker_tail_impl(worker, lines)

@mcp.tool()
def attach_existing(manager_sid: str | None = None) -> dict:
    """[MANAGER] Called by /manager on startup; returns running workers + orphan questions.

    `manager_sid` (caller's sid) scopes results to this manager's own workers.
    """
    return attach_existing_impl(manager_name=_resolve_manager_name_for_filter(manager_sid, "attach_existing"))

@mcp.tool()
def become_manager(
    claude_sid: str,
    iterm_sid: str = "",
    domain: str | None = None,
    name: str | None = None,
) -> dict:
    """[MANAGER /manager command] Register this session as a manager.

    `domain` defaults to 'general'. The name is auto-rolled as a funny
    <adjective>-<creature> pair, unique across all active records; pass `name`
    to preserve a prior identity instead (the `/manager-reboot` in-place recycle
    lane) — a passed name taken by a different live session is auto-suffixed.
    Managers are Claude-only; the record is always stamped runtime="claude".
    Returns {ok, name, domain, runtime, preflight} — preflight is the one-line
    server-side cleanup summary ("" = clean).
    """
    return become_manager_impl(claude_sid, iterm_sid, domain=domain, name=name)


@mcp.tool()
def close_manager_self(claude_sid: str) -> dict:
    """[MANAGER /manager-close] Distill + persist this manager's memory, then exit.

    Synchronously runs the same distill that prepare_handoff does, writes the
    result to manager-memory/<domain>/<date>-<sid>.md, then closes the
    session's own tab via the terminal driver (`tmux kill-pane`). The slash command will
    /exit immediately after; this tool only handles the durable-state side.

    Returns {ok, distill_path, name, domain} on success. If the manager record
    is missing, raises ValueError. Distill failure is non-fatal — the active
    record is still moved to closed/, tab still closed, distill_path is None.
    """
    return close_manager_self_impl(claude_sid)


def close_manager_self_impl(claude_sid: str) -> dict:
    record = state.read_json(paths.ACTIVE / f"{claude_sid}.json")
    if record is None or record.get("agent") != "manager":
        raise ValueError(f"session {claude_sid} is not a manager; cannot close")
    name = record.get("name")
    domain = record.get("domain") or DEFAULT_DOMAIN
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
    """[MANAGER] List all active manager sessions (name, domain, sid)."""
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
def list_closed_workers(manager_sid: str | None = None, limit: int | None = None) -> list[dict]:
    """[MANAGER] List closed worker sessions newest first.

    `manager_sid` scopes to this manager's workers; default None = return all.
    `limit` defaults to None (unlimited); when set, it must be a positive integer
    and is applied after manager scoping and newest-first ordering.
    """
    return list_closed_workers_impl(manager_name=_resolve_manager_name_for_filter(manager_sid, "list_closed_workers"), limit=limit)


@mcp.tool()
def prepare_handoff(claude_sid: str, narrative_summary: str, trigger_reason: str) -> dict:
    """[MANAGER] Snapshot manager state for handoff to a replacement manager.

    Returns {handoff_id, path}. The replacement manager (spawned via
    `spawn_replacement_manager`) reads the file at `path` and calls
    `become_manager_with_takeover` to atomically take over.
    """
    return prepare_handoff_impl(claude_sid, narrative_summary, trigger_reason)

@mcp.tool()
def prepare_recovery_handoff(from_sid: str, trigger_reason: str = "account-flip-recovery") -> dict:
    """[MANAGER] Synthesize a handoff for a bricked predecessor manager that cannot
    take turns (used by /manager-takeover-recovery as its first act, before
    become_manager_with_takeover). Writes a prepare_handoff-shaped record with a
    placeholder narrative; runs no distill. Returns {handoff_id, path}."""
    return prepare_recovery_handoff_impl(from_sid, trigger_reason)

@mcp.tool()
def become_manager_with_takeover(claude_sid: str, takeover_from: str, handoff_id: str, iterm_sid: str = "") -> dict:
    """[MANAGER /manager-resume] Atomic takeover from a previous manager.

    Verifies the handoff matches `takeover_from`, closes the old manager's tmux
    pane via the terminal driver (tmux kill-pane — not SIGTERM; the graceful
    close lets its SessionEnd fire the retro + memory distill), inherits its
    name + domain so workers' parent_manager_name references stay valid, marks
    the handoff consumed, and appends to manager-triggers.jsonl. Returns
    {ok, name, domain, runtime, preflight, predecessor_pane_closed} — preflight
    is the reused inner become_manager cleanup line; predecessor_pane_closed
    confirms the old pane is gone (false → /manager-resume step 4b kill-panes it
    manually).
    """
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
    from .manager_launch import manager_claude_args
    handoff_path = paths.HANDOFFS / f"{handoff_id}.json"
    handoff = state.read_json(handoff_path)
    if handoff is None:
        raise ValueError(f"no handoff with id {handoff_id}")
    if handoff.get("consumed_at") is not None:
        raise ValueError(f"handoff {handoff_id} already consumed")
    cwd = os.getcwd()
    initial_prompt = f"/manager-resume {handoff_id}"
    target_window_match = await _resolve_old_manager_window_match(handoff)
    manager_extra_args = [*manager_claude_args(), "--model", config.manager_model()]
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
    """[MANAGER] Spawn a new manager tab that will take over via /manager-resume.

    The new tab opens with initial prompt `/manager-resume <handoff_id>` in the
    OLD manager's OS-window (so the in-place swap keeps the manager OS-window
    stable across recreates). Its SessionStart hook will rename the tab title
    from 'manager (incoming)' to its normal manager title once it takes over.
    Managers are Claude-only; the replacement always launches the Claude CLI.
    """
    return await spawn_replacement_manager_impl(handoff_id)

_RESUMES_IN_FLIGHT: set[str] = set()


async def resume_worker_impl(
    name: str,
    _registration_timeout_sec: float = 10.0,
    _poll_interval: float = 0.5,
) -> dict:
    from .spawner import spawn_worker_tab
    closed_path, record = _find_closed_record_by_name(name)
    sid = record.get("claude_sid")
    cwd = record.get("cwd") or os.getcwd()
    if not sid:
        raise ValueError(f"closed worker '{name}' has no claude_sid; cannot resume")
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
        return await _spawn_and_confirm_resume(
            spawn_worker_tab,
            closed_path=closed_path,
            record=record,
            name=name,
            sid=sid,
            cwd=cwd,
            _registration_timeout_sec=_registration_timeout_sec,
            _poll_interval=_poll_interval,
        )
    finally:
        _RESUMES_IN_FLIGHT.discard(name)


async def _spawn_and_confirm_resume(
    spawn_worker_tab,
    closed_path,
    record: dict,
    name: str,
    sid: str,
    cwd: str,
    _registration_timeout_sec: float,
    _poll_interval: float,
) -> dict:
    parent_manager_name = record.get("parent_manager_name")
    env = {"CLAUDE_PARENT_MANAGER": parent_manager_name} if parent_manager_name else None
    runtime = record.get("runtime") or "claude"
    extra_args = _resume_settings_args(sid) if runtime == "claude" else None
    pre_spawn_sids = {
        r.get("claude_sid") for r in state.list_json_in(paths.ACTIVE) if r.get("claude_sid")
    }
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
        if runtime == "codex":
            for candidate in state.list_json_in(paths.ACTIVE):
                candidate_sid = candidate.get("claude_sid")
                if (
                    candidate.get("agent") == "worker"
                    and candidate.get("name") == name
                    and candidate_sid
                    and candidate_sid not in pre_spawn_sids
                ):
                    _migrate_assignment(sid, candidate_sid)
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
async def resume_worker(name: str) -> dict:
    """[MANAGER] Resume a previously closed worker by name.

    Opens a new tmux window in the worker's original cwd using the closed record's
    runtime (`claude --resume <sid>` or `codex resume <sid>`), restoring the full
    conversation history. The SessionStart hook re-registers the session into
    active/ under the same name. The closed/ record is deleted only after the
    resumed session is confirmed registered — keyed on the resumed session's own
    sid, with the registered record's actual handle returned in `name` (it can
    come back suffixed if another session claimed the requested name meanwhile;
    use the returned name for follow-ups). Returns {ok: false, ...} if it never
    registers within ~10s, leaving the closed record intact for retry.
    Raises ValueError if a live session already holds the name (message that
    worker via send_manager_to_worker, or kill_worker it, instead) or if a
    resume of this name is already in progress.
    """
    return await resume_worker_impl(name)


def _resolve_preset(preset: str) -> str:
    preset_path = paths.PRESETS / f"{preset}.md"
    if not preset_path.is_file():
        available = sorted(p.stem for p in paths.PRESETS.glob("*.md")) if paths.PRESETS.is_dir() else []
        raise ValueError(
            f"preset '{preset}' not found at {preset_path}; available: {available}"
        )
    return preset_path.read_text()


def _claude_worker_settings_args(extra_args: list[str] | None = None) -> list[str]:
    rc_on = os.environ.get("CLAUDE_ORCH_WORKER_RC", "").strip() == "1"
    if extra_args:
        from .spawner import _matches_option
        if any(_matches_option(a, {"--settings"}) for a in extra_args):
            return ["--remote-control"] if rc_on else []
    preset_path = paths.PRESETS / "worker-headless-settings.json"
    preset: dict | None = None
    if config.worker_headless_preset():
        try:
            loaded = json.loads(preset_path.read_text())
            if isinstance(loaded, dict):
                preset = loaded
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            preset = None
    if preset is None:
        return _legacy_inline_settings_args()
    if rc_on:
        preset.pop("remoteControlAtStartup", None)
        preset.pop("disableRemoteControl", None)
        return ["--settings", json.dumps(preset), "--remote-control"]
    return ["--settings", str(preset_path)]


def _legacy_inline_settings_args() -> list[str]:
    rc_on = os.environ.get("CLAUDE_ORCH_WORKER_RC", "").strip() == "1"
    settings: dict = {"enableAllProjectMcpServers": True}
    if rc_on:
        return ["--settings", json.dumps(settings), "--remote-control"]
    settings["remoteControlAtStartup"] = False
    settings["disableRemoteControl"] = True
    return ["--settings", json.dumps(settings)]


def _resume_settings_args(sid: str) -> list[str]:
    record = state.read_json(paths.assignment_path(sid)) or {}
    spawn_args = record.get("spawn_extra_args")
    if isinstance(spawn_args, list) and all(isinstance(a, str) for a in spawn_args):
        return list(spawn_args)
    return _legacy_inline_settings_args()


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
) -> dict:
    from .spawner import (normalize_runtime, spawn_worker_tab, usage_spawn_gate,
                          write_registry_snapshot)
    runtime = normalize_runtime(runtime)
    _validate_task_key(task_key)
    parent_manager_name = _resolve_parent_manager(manager_sid)
    write_registry_snapshot()
    gate = usage_spawn_gate(force=force)
    if gate.get("status") == "paused":
        return gate
    if cwd is None:
        home = paths.ensure_worker_home()
        cwd = str(home) if home.is_dir() else os.getcwd()
    raw_name = name
    if name is None:
        name = f"worker-{int(time.time())}"
    name = _resolve_unique_name(name)
    raw_prompt = initial_prompt
    ticket = task_key
    if preset is not None:
        initial_prompt = f"{_resolve_preset(preset)}\n\n---\n\n{initial_prompt}"
    if ticket and (raw_prompt or "").strip():
        initial_prompt += _artifact_discipline_footer(ticket)
    if (raw_prompt or "").strip():
        initial_prompt += _repo_sync_footer()
    if runtime == "claude":
        extra_args = _claude_worker_settings_args(extra_args) + (extra_args or [])
    else:
        extra_args = extra_args or []
    if runtime == "claude":
        env = {**config.spawn_env(), **(env or {})}
    if parent_manager_name:
        env = {**(env or {}), "CLAUDE_PARENT_MANAGER": parent_manager_name}
    _prune_stale_assignments()
    assignment_id = uuid.uuid4().hex
    _write_pending_assignment(
        assignment_id, name, raw_prompt, preset, cwd,
        manager_sid, parent_manager_name, runtime,
        ticket=ticket,
        spawn_extra_args=extra_args,
    )
    env = {**(env or {}), "CLAUDE_ASSIGNMENT_ID": assignment_id}
    try:
        async with asyncio.timeout(15):
            iterm_sid, _ = await spawn_worker_tab(
                cwd=cwd,
                initial_prompt=initial_prompt,
                name=name,
                runtime=runtime,
                route_to_workers_window=True,
                extra_args=extra_args,
                env=env,
                force=force,
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
        "parent_manager_name": parent_manager_name,
        "window_id": iterm_sid,
    }
    if ticket is None:
        hint = _unkeyed_key_hint(raw_name, raw_prompt)
        if hint:
            result["task_key_hint"] = hint
    if registered is not None:
        result["status"] = "registered"
        result["claude_sid"] = registered.get("claude_sid")
        result["note"] = "worker registered its active record via SessionStart hook"
    else:
        result["status"] = "no_register"
        result["assignment_id"] = assignment_id
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
async def spawn_worker(
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
) -> dict:
    """[MANAGER] Spawn a new worker in a fresh tmux window.

    Args:
        initial_prompt: First prompt given to the worker (can be empty for a bare runtime session).
        name: Worker handle (e.g., 'web-rebase'). Auto-generated if None. If the name is
              already taken by an active record, an auto-suffix '-2', '-3' is appended.
        cwd: Working directory for the worker. Defaults to current cwd.
        extra_args: Extra CLI flags passed to the selected runtime before the
            prompt (e.g. ["--model", "gpt-5.5"]). Claude workers default to `--settings <deployed
            worker-headless-settings.json>` (auto mode + protocol allowlist +
            operator code-roots additionalDirectories) before caller args; a
            caller-passed --settings REPLACES the preset entirely (last-wins).
            extra_args=["--permission-mode", "default"] re-gates FILE EDITS only
            for one spawn (the preset's allow rules — git commit/checkout/etc. —
            still auto-approve, since allow rules are consulted before the mode
            default); for a fully-manual spawn pass your own --settings. The
            final composed extra_args are persisted on the assignment record and
            replayed verbatim by resume_worker, so a resumed worker keeps the
            exact permissions it was born with. Fleet-wide opt-out:
            [spawn] worker_headless_preset=false. Codex workers get `--ask-for-approval never --sandbox
            danger-full-access --dangerously-bypass-hook-trust` plus a
            worker-protocol bootstrap prompt; caller args cannot override those
            defaults or pass known Claude-only flags.
        env: Extra env vars exported in the worker's shell before the selected runtime runs
            (e.g. {"MY_FLAG": "1"}). Merged over the operator's [spawn.env]
            defaults (caller values win). The orchestrator-controlled CLAUDE_AGENT,
            CLAUDE_WORKER_NAME, and CLAUDE_WORKER_RUNTIME cannot be overridden —
            those keys are silently dropped from the caller's dict.
            Default: no extra vars.
        preset: Name of a preset under ~/.claude/dockwright/presets/<name>.md whose
            content is prepended to initial_prompt with a `\\n\\n---\\n\\n` divider.
            Useful for shared workflow boilerplate (rebase-first, commit style, test
            invocation, worker_done at end) so callers stop retyping it on every spawn.
            Raises ValueError if the file is missing. Default: None (no preset).
        manager_sid: Caller's own claude_sid. When supplied, must match a live
            top-level manager's ACTIVE record (written by become_manager); the
            worker's active record then carries `parent_manager_name`, scoping
            it to this manager for routing (list_workers / wait_for_worker /
            questions / done events). A sid matching no live manager — or a
            worker/nested record — raises ValueError BEFORE anything is
            spawned (a mistyped sid used to spawn an UNSCOPED worker whose
            events silently routed to _unscoped/). Default None = intentional
            unscoped legacy single-manager behaviour.
        runtime: Worker CLI runtime: "claude" (default, backward compatible) or
            "codex". The runtime is stamped into active records via
            CLAUDE_WORKER_RUNTIME and returned by list_workers.
        task_key: Grouping key stamped into the worker's assignment record —
            joins the worker into `pipeline_status(task_key)` and the artifact
            store under `artifacts/<task_key>/`. Use a stable slug for personal
            multi-agent tasks with no tracker key (e.g. "yt-bot-public"); pass
            the SAME slug on every spawn of that task's workers. Explicit
            task_key is the ONLY keying path — nothing is ever derived from
            prompt or name text (a mention is not an assignment). When omitted
            the spawn is UNKEYED; if [task_keys] key_regex is configured and
            matches the prompt or caller-supplied name, the result carries an
            advisory `task_key_hint` (nothing is stamped or filed).
            Validated fail-fast: must be a stable [A-Za-z0-9_-] slug; blank
            raises (omit entirely for an unkeyed spawn).
            Keyed spawns (explicit task_key) get an artifact-discipline footer
            appended to the prompt; every non-blank prompt (keyed or not)
            additionally gets a repo-sync footer (sync a repo once before
            reading it). Blank prompts are left untouched.
        force: Bypass the usage breaker + the all-accounts-near-limit pause for THIS spawn only (still
            headroom-weighted, still skips bricked accounts). Use for a genuinely
            urgent spawn when the gate returned {"status":"paused"}. If the chosen
            account is truly maxed the worker will brick and stale_monitor's flip
            recovers it.

    Returns:
        On a normal spawn, the worker record dict. May instead return
        `{"status":"paused", "reason", "hint", "<name>_pct" (one per pool
        account), "earliest_reset_ts", "retry_after_s"}` instead of spawning
        when every selectable account is >= the pause threshold ([accounts]
        usage_pause_pct, default 88) of its 5h limit; pass `force=True` to
        override.
    """
    return await spawn_worker_impl(initial_prompt, name, cwd, extra_args, env, preset, manager_sid, runtime, task_key, force)


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


def artifact_put_impl(task_key, phase, name, content, status, writer_sid,
                      contract_hash=None, read_set=None, overwrite=False) -> dict:
    if status not in ("partial", "complete"):
        raise ValueError(f"status must be 'partial'|'complete', got {status!r}")
    if not writer_sid:
        raise ValueError(
            f"writer_sid must be a non-empty session id, got {writer_sid!r} "
            "(two callers passing the same falsy sid would alias into one "
            "store owner)")
    paths.ensure_dirs()
    written_at = time.time()
    stamp = {"phase": phase, "name": name, "status": status, "writer_sid": writer_sid,
             "contract_hash": contract_hash, "written_at": written_at,
             "read_set": read_set or []}
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
    if overwrite:
        event["overwrite"] = True
    if archived:
        event["archived_previous"] = archived
    state.append_event(paths.artifact_events_path(task_key), event)
    result = {"ok": True, "path": str(path), "written_at": written_at, "status": status}
    if archived:
        result["archived_previous"] = archived
    return result


def artifact_get_impl(task_key, phase, name) -> dict:
    path = paths.artifact_path(task_key, phase, name)
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise ValueError(f"no artifact {phase}.{name} for {task_key}") from None
    stamp, body = state.parse_artifact(text)
    return {**stamp, "content": body, "path": str(path)}


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
        if newest < cutoff:
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
    d = paths.artifact_ticket_dir(task_key)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        try:
            stamp, _ = state.parse_artifact(p.read_text())
        except ValueError:
            continue
        out.append({**stamp, "path": str(p)})
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
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
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


def pipeline_status_impl(task_key) -> str:
    _prune_stale_assignments()
    artifacts = artifact_list_impl(task_key)
    events = _read_events(paths.artifact_events_path(task_key))
    assignments = _assignments_for_ticket(task_key)
    liveness_memo: dict = {}

    def _liveness(sid) -> str:
        if sid not in liveness_memo:
            liveness_memo[sid] = _join_worker_liveness(sid)
        return liveness_memo[sid][0]

    lines = [f"# pipeline_status({task_key})", ""]
    lines.append("## artifacts (directory-as-index)")
    for a in artifacts:
        sid = a.get("writer_sid") or ""
        lines.append(f"- {a.get('phase', '?')}.{a.get('name', '?')}  [{a.get('status', '?')}]  "
                     f"writer={sid[:8]}({_liveness(sid)})  "
                     f"@{_iso(a.get('written_at'))}  read={len(a.get('read_set') or [])} input(s)")
    lines.append("\n## assignments (ownership plane)")
    for s in assignments:
        sid = s.get("claude_sid") or ""
        lines.append(f"- {s.get('name')}  sid={sid[:8]}({_liveness(sid)})  "
                     f"branch={s.get('branch')}  brief={_brief_of(s)!r}")
    lines.append("\n## events (events.jsonl, chronological)")
    for e in events:
        lines.append(f"- {_iso(e.get('ts'))} {e.get('type')} {e.get('phase', '')}.{e.get('name', '')} "
                     f"{('— ' + str(e['reason'])) if e.get('reason') else ''}")
    return "\n".join(lines)


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


def _artifact_discipline_footer(task_key: str) -> str:
    return (
        "\n\n---\n"
        f"[orchestrator] Artifact discipline — task_key: `{task_key}`\n"
        "Persist phase outputs to the artifact store as they stabilize, without "
        f"waiting to be asked: `artifact_put(task_key=\"{task_key}\", "
        "phase=<spec|plan|implement|review|summary>, name=<repo-or-scope>, "
        "content=..., status=\"partial\"|\"complete\", writer_sid=<your sid>)`. "
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
                              ticket=None, spawn_extra_args=None) -> None:
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
        "runtime": runtime,
        "ticket": ticket,
        "spawn_extra_args": spawn_extra_args,
        "spawned_at": time.time(),
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
    if not paths.ASSIGNMENTS.is_dir():
        return
    cutoff = now - paths.ASSIGNMENT_RETENTION_DAYS * 86400
    for p in paths.ASSIGNMENTS.glob("*.json"):
        record = state.read_json(p) or {}
        sid = record.get("claude_sid") or p.stem
        if (paths.ACTIVE / f"{sid}.json").exists():
            continue
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
        except OSError:
            continue


def _migrate_assignment(old_sid: str, new_sid: str) -> None:
    old_path = paths.assignment_path(old_sid)
    new_path = paths.assignment_path(new_sid)
    if not old_path.exists() or new_path.exists():
        return
    try:
        os.replace(old_path, new_path)
    except OSError:
        return
    record = state.read_json(new_path) or {}
    record["claude_sid"] = new_sid
    state.write_json_atomic(new_path, record)


def pipeline_event_impl(task_key, type, phase=None, name=None, reason=None, actor_sid=None) -> dict:
    paths.ensure_dirs()
    event = {"type": type, "phase": phase, "name": name, "reason": reason, "actor_sid": actor_sid}
    state.append_event(paths.artifact_events_path(task_key),
                       {k: v for k, v in event.items() if v is not None})
    return {"ok": True}


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


def _evict_stale_holders(holders: list) -> list:
    fresh: list = []
    for h in holders:
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


def acquire_worker_slot_impl(
    claude_sid: str,
    category: str,
    max_concurrent: int | None = None,
    timeout_sec: int = 1800,
    _poll_interval: float = 0.1,
) -> dict:
    paths.ensure_dirs()
    cap = _resolve_max_concurrent(category, max_concurrent)
    slot_path = paths.SLOTS / f"{category}.json"
    deadline = time.monotonic() + timeout_sec
    while True:
        with _slots_lock():
            data = state.read_json(slot_path) or {}
            holders = _evict_stale_holders(data.get("holders") or [])
            if len(holders) < cap:
                slot_id = uuid.uuid4().hex
                holders.append({
                    "slot_id": slot_id,
                    "claude_sid": claude_sid,
                    "acquired_at": time.time(),
                    "pid": os.getpid(),
                })
                state.write_json_atomic(slot_path, {"max_concurrent": cap, "holders": holders})
                return {
                    "slot_id": slot_id,
                    "category": category,
                    "max_concurrent": cap,
                    "holders_count": len(holders),
                }
            state.write_json_atomic(slot_path, {"max_concurrent": cap, "holders": holders})
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"could not acquire slot for category '{category}' within "
                f"{timeout_sec}s (cap={cap})"
            )
        time.sleep(_poll_interval)


def release_worker_slot_impl(slot_id: str) -> dict:
    paths.ensure_dirs()
    with _slots_lock():
        if not paths.SLOTS.is_dir():
            return {"released": True, "slot_id": slot_id, "found": False}
        for p in paths.SLOTS.iterdir():
            if p.suffix != ".json":
                continue
            data = state.read_json(p)
            if not isinstance(data, dict):
                continue
            holders = data.get("holders") or []
            new_holders = [h for h in holders if h.get("slot_id") != slot_id]
            if len(new_holders) != len(holders):
                data["holders"] = new_holders
                state.write_json_atomic(p, data)
                return {"released": True, "slot_id": slot_id, "found": True, "category": p.stem}
    return {"released": True, "slot_id": slot_id, "found": False}


def _resolve_task_key(task_key: str, ticket: str) -> str:
    key = task_key or ticket
    if not key:
        raise ValueError("task_key is required (the `ticket=` alias is deprecated)")
    return key


@mcp.tool()
def artifact_put(task_key: str = "", *, phase: str, name: str, content: str, status: str,
                 writer_sid: str, contract_hash: str | None = None,
                 read_set: list[dict] | None = None, overwrite: bool = False,
                 ticket: str = "") -> dict:
    """[WORKER] Publish an artifact for a task_key's phase. status='partial'|'complete'.
    Atomic, single-writer-per-(phase,name) — ENFORCED: a put that would replace a
    record owned by another writer_sid, a file the store did not write with
    different content, YOUR OWN finalized (non-partial) record with different
    content, a byte-equal re-put demoting a final record (or a finished hand
    file) to partial, or a sanitize-colliding (phase, name) pair is refused
    unless overwrite=True (canonical use: a successor finishing a dead worker's
    phase). Your own partial-record updates and partial→complete flips need no
    flag. Content-replacing writes archive the previous file to
    <phase>.<name>.md.prev (latest-only) and report it as archived_previous.
    `ticket=` is a deprecated alias for `task_key=`."""
    return artifact_put_impl(_resolve_task_key(task_key, ticket), phase, name, content,
                             status, writer_sid, contract_hash, read_set, overwrite)


@mcp.tool()
def artifact_get(task_key: str = "", *, phase: str, name: str, ticket: str = "") -> dict:
    """[WORKER/MANAGER] Read one artifact (frontmatter stamp + body). Raises if absent.
    `ticket=` is a deprecated alias for `task_key=`."""
    return artifact_get_impl(_resolve_task_key(task_key, ticket), phase, name)


@mcp.tool()
def artifact_list(task_key: str = "", ticket: str = "") -> list[dict]:
    """[MANAGER/WORKER] List a task_key's artifacts — frontmatter stamps only, no bodies.
    The derived index. `ticket=` is a deprecated alias for `task_key=`."""
    return artifact_list_impl(_resolve_task_key(task_key, ticket))


@mcp.tool()
def artifact_view(task_key: str = "", ticket: str = "") -> str:
    """[MANAGER] Pretty whole-blackboard fold: every artifact's stamp + body excerpt.
    `ticket=` is a deprecated alias for `task_key=`."""
    return artifact_view_impl(_resolve_task_key(task_key, ticket))


@mcp.tool()
def pipeline_status(task_key: str = "", ticket: str = "") -> str:
    """[MANAGER] Fold artifacts + assignments + events.jsonl, liveness-joined by claude_sid.
    The pipeline replay. `ticket=` is a deprecated alias for `task_key=`."""
    return pipeline_status_impl(_resolve_task_key(task_key, ticket))


@mcp.tool()
def pipeline_event(task_key: str = "", *, type: str, phase: str | None = None,
                   name: str | None = None, reason: str | None = None,
                   actor_sid: str | None = None, ticket: str = "") -> dict:
    """[MANAGER/WORKER] Append an audit event (dispatch|phase_complete|note|publish|...) to
    the task_key's events.jsonl. `ticket=` is a deprecated alias for `task_key=`."""
    return pipeline_event_impl(_resolve_task_key(task_key, ticket), type, phase, name, reason, actor_sid)


@mcp.tool()
def acquire_worker_slot(
    claude_sid: str,
    category: str,
    max_concurrent: int | None = None,
    timeout_sec: int = 1800,
) -> dict:
    """[WORKER] Block until a slot for `category` is free, then return a slot_id.

    Use before running memory-heavy commands (mvn test, gradle test, big docker
    builds) so N concurrent workers don't OOM the host. `category` is a free-form
    string ("mvn", "npm", "docker-build") — each category is an independent
    semaphore. If `max_concurrent` is None, falls back to env
    `CLAUDE_ORCH_SLOTS_<CATEGORY>` then to a built-in default. Always pair with
    `release_worker_slot(slot_id)`.
    """
    return acquire_worker_slot_impl(claude_sid, category, max_concurrent, timeout_sec)


@mcp.tool()
def release_worker_slot(slot_id: str) -> dict:
    """[WORKER] Release a previously acquired slot. Idempotent — safe to call twice."""
    return release_worker_slot_impl(slot_id)


def main() -> None:
    try:
        from .spawner import write_registry_snapshot
        write_registry_snapshot()
    except Exception:
        pass
    mcp.run()

if __name__ == "__main__":
    main()
