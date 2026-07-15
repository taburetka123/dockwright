"""FastMCP server exposing dockwright tools for manager + worker sessions."""
import asyncio
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
from . import config, names, paths, state
from .state import _pid_alive
from .terminal import get_driver
from .transcript import find_session_log, is_delegating, last_assistant_summary
# Re-exported for the MANY internal call sites + existing test imports; the
# canonical home is registry.py so the hook path never touches this module.
from .registry import (
    _drop_questions_for_worker,
    _prune_stale_active_records,
    _question_paths,
    _resolve_unique_name,
)
# Only distill_and_write_memory has internal call sites; the private names are
# re-exported for the existing test corpus.
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

# How long spawn_worker_impl waits for the freshly-launched worker's SessionStart
# hook to write its active record, and the poll cadence while waiting. Overridable
# per-call (and shrunk by the test suite's autouse fixture) so the ~36 existing
# spawn tests don't each block the full default.
_DEFAULT_REGISTRATION_TIMEOUT_SEC = 12.0
_DEFAULT_REGISTRATION_POLL_SEC = 0.5

# --- Implementations (separate from FastMCP decorators so they're testable) ---

def _backfill_legacy_workers() -> int:
    """Stamp parent_manager_name on null-parent worker records, when unambiguous.

    Workers spawned before multi-manager support have `parent_manager_name=None`,
    making them wildcard-visible to every manager. If exactly ONE manager is
    active, attribute the orphans to it. If 0 or 2+ managers exist, skip and
    warn loudly — the right owner is ambiguous, manual cleanup needed.

    Idempotent: once workers have non-null parents, subsequent boots are no-ops.
    """
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
            # Nested sub-sessions are neither attributable workers nor real
            # managers — a nested manager-agent ghost must not break the
            # exactly-one-manager attribution.
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
    """One-shot: move pre-multi-manager flat *.md files into manager-memory/general/.

    Idempotent: if `general/` is already the layout, returns 0. Skips subdirs and
    non-.md files. Safe to call on every bootstrap; cheap (one readdir).

    Older builds wrote `manager-memory/<date>-<sid>.md` directly under the root.
    Multi-manager moves to `manager-memory/<domain>/<date>-<sid>.md`. The "general"
    subdir is the back-compat home for those flat files.
    """
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
            # If target already exists, leave the file where it is.
            pass
    return moved


def _looks_like_manager_bootstrap_ghost(record: dict, keep_window_id: str) -> bool:
    """True if `record` is this tab's own stale manager identity, not a live peer.

    Identified STRUCTURALLY — agent==manager AND same tmux pane as the incoming
    manager — NOT by name. The caller (_prune_same_pid_ghosts) has already filtered
    to records that share the incoming manager's pid and carry a DIFFERENT sid. One
    OS process = one Claude Code session, so a same-pid + same-window record under
    another sid can only be a prior identity of the very session now re-registering:
    the SessionStart placeholder, or a two-phase become_manager call's first record.
    Both are rolled a funny <adjective>-<creature> name (SessionStart and become_manager
    both call roll_manager_name), so a literal-"manager" name check would miss them.
    The same-window guard is what keeps a live peer manager — its own tab/window, pid
    shared only in tests — off this cleanup path.
    """
    if record.get("agent") != "manager":
        return False
    if not keep_window_id:
        return False
    return state.window_id_of(record) == keep_window_id


def _prune_same_pid_ghosts(pid: int, keep_sid: str, keep_window_id: str = "") -> None:
    """Drop active records that share `pid` but carry a different claude_sid.

    Call this AFTER _prune_stale_active_records. Live manager records are preserved
    unless they are this tab's own bootstrap placeholder — same pid AND same tmux
    pane, under a different sid. That placeholder is written by SessionStart (or
    a two-phase become_manager call) under a funny <adjective>-<creature> name before
    become_manager registers the authoritative sid/name; it's matched structurally
    (see _looks_like_manager_bootstrap_ghost), NOT by name. Peer managers run in
    their own tab/window, so the same-window guard keeps them off this cleanup path.
    """
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
    # Reject duplicate names (across all agents, all domains — names must be globally unique)
    for record in state.list_json_in(paths.ACTIVE):
        if record.get("name") == name and record.get("claude_sid") != claude_sid:
            raise ValueError(f"name '{name}' is taken by session {record.get('claude_sid')}")
    if pid is None:
        pid = os.getppid()
    if agent == "manager" and not domain:
        domain = DEFAULT_DOMAIN
    # Account stamp precedence: spawn env (the MCP server inherits the session's
    # env — recovery tabs / pool spawns carry CLAUDE_ORCH_ACCOUNT), else the
    # existing record's stamp — the SessionStart hook stamps it (hooks.py) and
    # become_manager re-registers through here minutes later; rebuilding the
    # record must not erase it. Neither present (user-launched keychain-auth
    # session) ⇒ None.
    env_account = os.environ.get("CLAUDE_ORCH_ACCOUNT")
    if env_account in config.account_names():
        account = env_account
    else:
        prior_account = (state.read_json(paths.ACTIVE / f"{claude_sid}.json") or {}).get("account")
        account = prior_account if prior_account in config.account_names() else None
    record = {
        "claude_sid": claude_sid,
        "agent": agent,
        "name": name,
        "cwd": cwd,
        "window_id": iterm_sid,
        "pid": pid,
        "started_at": time.time(),
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
    """Routing filter: does this record belong to manager `manager_name`?

    manager_name=None → no filter (back-compat: legacy single-manager calls
    and wildcard lookups). Otherwise: strict — include records whose
    parent_manager_name == manager_name. Null-parent (legacy) records are
    INVISIBLE to per-manager calls; recovery path is
    `_backfill_legacy_workers` on a single-manager `become_manager` boot.
    """
    if manager_name is None:
        return True
    return record.get("parent_manager_name") == manager_name

def _humanize_tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{round(count / 1_000)}k"
    return str(count)


def _format_spend(spend) -> str | None:
    """Compact spend line for list output, e.g. '12 turns / 340k out'."""
    if not isinstance(spend, dict):
        return None
    turns = spend.get("turns")
    out_tokens = spend.get("out_tokens")
    if not isinstance(turns, int) or not isinstance(out_tokens, int):
        return None
    turn_label = "turn" if turns == 1 else "turns"
    return f"{turns} {turn_label} / {_humanize_tokens(out_tokens)} out"


def _spend_totals(spend) -> dict | None:
    """Spend totals for durable events — drops the tail cursor + per-turn value."""
    if not isinstance(spend, dict):
        return None
    return {key: spend.get(key)
            for key in ("turns", "out_tokens", "in_tokens", "cache_read_tokens")}


def _reclaim_closed_spend(closed_record: dict) -> None:
    """An autoclosed record's spend exists ONLY in closed/ (stale_monitor wrote
    it after unlinking active/, so session_end never ledgered the period).
    Resume deletes that record — archive first or the period vanishes.
    session_end-reason records were already ledgered at close; appending again
    would double-count."""
    if closed_record.get("closed_reason") == "session_end":
        return
    from .spend_ledger import append_drop_event
    append_drop_event(closed_record, "resume_reclaim")


def list_workers_impl(manager_name: str | None = None) -> list[dict]:
    """List worker sessions with last status + liveness.

    `manager_name` filters by parent_manager_name (see _matches_manager).
    """
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
        # Read-side effective state: a worker whose background subagent is
        # still writing reads as working — on-disk state stays turn-truth
        # (autoclose/sweep consumers unaffected); no third state value.
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

# Bounded server-side wait per ask_manager call — must stay safely under the
# MCP client's 1800s no-progress abort so the tool self-terminates with a
# re-ask sentinel instead of being killed mid-blocking-loop.
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
    """Consume ANSWERS/<qid>.json if a valid answer is present. Corrupt file →
    unlink and return None (the manager can re-write a fresh answer). An answer
    stamped with a DIFFERENT worker_sid is left in place and raises — a worker
    may only consume its own answer; an absent stamp (legacy/skewed writer) is
    tolerated."""
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
    """Write a question (or resume a pending one via resume_question_id), poll
    for the answer file without blocking the event loop, and return the answer
    text — or a NO_ANSWER_YET re-ask sentinel when timeout_sec elapses."""
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
        # Resume: reattach to a pending question instead of duplicating it; the
        # `question` text is accepted and ignored. Answer file FIRST —
        # answer_question writes the answer THEN unlinks the question, so a
        # missing question can still mean "answered while this worker was away".
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
            # Question absent (or vanished between find and read): re-check the
            # answer once — a mid-flight answer_question may have landed between
            # our two checks. Only then declare the qid dead.
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
    # Additive ownership stamp for ask_manager's resume path; readers tolerate
    # its absence (manager and worker servers are separate processes — version
    # skew is a normal deployment state). Unreadable question record → write
    # unstamped rather than fail the answer.
    if question is not None and question.get("worker_sid"):
        payload["worker_sid"] = question["worker_sid"]
    state.write_json_atomic(paths.ANSWERS / f"{question_id}.json", payload)
    # Remove from questions/ so it doesn't show in list_pending
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
    """Resolve an ACTIVE WORKER record by routing name or sid.

    Manager records never match: every caller is a worker-targeted tool
    (kill_worker, send_manager_to_worker, get_worker_summary/tail), a manager
    record can still hold the name (pools were combined before the role split,
    legacy records persist, and callers can pass a manager's name by mistake),
    and send_manager_to_worker types with NO idle guard — resolving a manager
    here would let kill_worker close a manager tab or clobber a human mid-typing
    (send_manager_to_manager is the guarded path for manager panes).
    """
    non_worker_holder = None
    for record in state.list_json_in(paths.ACTIVE):
        if record.get("name") == identifier or record.get("claude_sid") == identifier:
            if record.get("agent") == "worker":
                return record
            non_worker_holder = record
    if non_worker_holder is not None:
        # A bare "no worker named X" reads as a typo when X visibly exists in
        # list_managers — name the holder, mirroring the resume_worker refusal.
        agent = non_worker_holder.get("agent") or "session"
        raise ValueError(
            f"'{identifier}' is an active {agent}, not a worker; "
            f"use send_manager_to_manager to message managers"
        )
    raise ValueError(f"no worker named '{identifier}'")

def _capture_text(window_id: str) -> str | None:
    """Return the ANSI-styled on-screen text of a terminal pane, or None if unreadable.

    Captured WITH styling (`capture_screen_ansi`, i.e. `tmux capture-pane -p -e`) so
    `_input_is_idle` can tell an empty box (faint placeholder ghost-text) from real
    typed input. Used for the idle check before firing a wake into a peer
    manager's input box (a wake mid-typing would clobber) and for the
    auto_resume lane's readiness wait (`_await_input_ready`). Fully defensive: any
    subprocess failure, non-zero exit, or window-gone condition returns None (the
    manager-wake caller treats None as an unreadable window → hard error, no wake;
    the readiness wait treats it as not-ready and proceeds best-effort on timeout)."""
    return get_driver().capture_screen_ansi(window_id)


_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")


def _strip_ansi(text: str) -> str:
    """Drop all SGR (color/style) escape sequences, leaving plain glyphs."""
    return _SGR_RE.sub("", text)


def _strip_dim_spans(text: str) -> str:
    """Drop text rendered with the faint/dim SGR attribute (code 2).

    Claude Code renders the empty-input-box PLACEHOLDER ghost-text in faint
    (`\\x1b[2m … \\x1b[0m`); genuinely-typed input is normal intensity. Removing
    faint spans leaves only real input, so a box holding only a placeholder reads as
    empty. Faint is turned off by a reset (0, or the bare `\\x1b[m`) or by explicit
    normal-intensity (22). Returns text with ALL SGR sequences removed (matched
    escapes are never re-emitted), so callers need no separate _strip_ansi pass."""
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
    """True only when the target's Claude input box is empty and ready for input.

    `screen_text` is captured WITH ANSI styling preserved (`capture_screen_ansi`):
    the only on-wire signal distinguishing a genuinely-empty box from typed input is
    styling. An empty box renders the caret `❯` followed by a faint/dim PLACEHOLDER
    suggestion (`\\x1b[2m … \\x1b[0m`); typed/queued input shows normal-intensity text
    after the caret or a "Press up to edit queued messages" hint. We drop faint
    placeholder spans, then check whether anything typed remains. Be conservative:
    anything we can't positively confirm as empty (no screen, no caret line, queued
    indicator, leftover non-faint text) is treated as busy so we never wake a pane
    mid-input. Plain (un-styled) text has no faint spans, so this degrades to a pure
    empty-after-caret check."""
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
    """Type the message CONTENT directly into a terminal pane's program, then submit.

    Used by the DIRECT manager-messaging paths (manager→worker, and manager→manager
    when the peer is idle). The content is the real message, not a sentinel.

    Multi-line content is a hazard: a literal newline typed into the Claude Code input
    box submits the message early, fragmenting it. We therefore delegate to the terminal
    driver, which delivers the content wrapped in bracketed paste so the TUI inserts every
    newline as text rather than as a submit, then sends a SINGLE Enter to submit the whole
    message. The content is delivered verbatim — no escape interpretation — so a literal
    "\\n" in the message stays literal. (Verified live: the payload arrives as
    `\\e[200~<content>\\e[201~` followed by one `\\r`.) Best-effort; swallows failures so
    it never blocks the caller.
    """
    get_driver().send_text(window_id, text)


_WINDOW_RESOLVE_RETRIES = 3
_WINDOW_RESOLVE_RETRY_SLEEP = 1.0

# Prefix for every manager→worker pane relay. Workers use it to tell a manager
# relay (orchestration) from the engineer typing directly into their pane (a
# user instruction that can override the brief) — worker.core.md principle 6.
# Plain ASCII inside the bracketed-paste body: the buffering/submit mechanics
# are untouched. Prepended ONLY here (single _send_text site in
# send_manager_to_worker_impl) so the auto_resume lane cannot double-mark.
# Known limitation: a relayed text starting with a slash command arrives as
# "[MANAGER] /foo ..." — plain text to the harness, so slash expansion never
# fires (the worker model can still invoke the skill by name).
MANAGER_MARKER = "[MANAGER] "

_INPUT_READY_TIMEOUT_SEC = 15.0
_INPUT_READY_POLL_SEC = 0.5
_INPUT_READY_CODEX_SLEEP_SEC = 2.0


async def _await_input_ready(window_id: str, runtime: str) -> None:
    """Bounded wait for a freshly resumed pane's TUI to accept a paste.

    A resumed session registers (SessionStart hook) slightly before its TUI
    enables bracketed paste; typing at zero delay can paste raw, where a
    literal newline submits early and fragments a multi-line message (tmux
    `paste-buffer -p` only wraps when the application has enabled the mode).
    The manual resume→send dance hides this behind manager round-trip latency;
    the auto_resume lane compresses it to ~0, so wait here. Claude lane: poll
    for an idle input box. Codex lane: `_input_is_idle` needs Claude Code's
    caret + faint-placeholder rendering and can NEVER pass on a codex pane, so
    take a short fixed sleep instead of an unconditional full-timeout stall.
    Async on purpose — a sync sleep would freeze every in-flight tool on the
    manager's MCP event loop. Best-effort: returns on timeout (typing into a
    busy/booting pane is the existing buffering contract); never raises.
    """
    if runtime != "claude":
        # TODO: replace the fixed sleep with a codex-aware readiness poll. A
        # codex resume that takes >2s to enable bracketed paste can still
        # fragment a multi-line send — the exact hazard this wait exists for.
        # Needs a verified codex TUI idle signature (its prompt rendering in a
        # capture-pane) before a poll can be written; spike that first.
        await asyncio.sleep(_INPUT_READY_CODEX_SLEEP_SEC)
        return
    if not window_id:
        return
    deadline = time.monotonic() + _INPUT_READY_TIMEOUT_SEC
    while time.monotonic() < deadline:
        # to_thread: capture-pane is a sync subprocess (ms-scale, 2s worst
        # case); off the loop so a slow capture can't stall in-flight tools.
        if _input_is_idle(await asyncio.to_thread(_capture_text, window_id)):
            return
        await asyncio.sleep(_INPUT_READY_POLL_SEC)


def _match_worker_window_by_cwd_runtime(data: list, record: dict) -> str:
    """Exactly-one live window whose cwd == record cwd and a foreground process
    cmdline carries the runtime token, else "" (zero or ambiguous >1 match).
    cwd is the only viable live marker (env lacks CLAUDE_WORKER_NAME, the title
    is the runtime's spinner) — unique only for worktree-isolated workers, hence
    the captured id (A2) is primary and this is the fallback."""
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
                    # tmux ls omits per-pane foreground processes; fall back to
                    # cwd-uniqueness (cwd is a unique live marker only for
                    # worktree-isolated workers).
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
    """Resolve a live tmux pane for a worker record, stamping a freshly
    discovered id back into active/<sid>.json. Order: persisted id if it's live
    in the tmux list-panes output, else a cwd+runtime match. "" if neither resolves.
    When the listing is unavailable we trust the persisted id (don't break a working
    path on a transient listing hiccup)."""
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
    """Manager→worker: type the message CONTENT directly into the worker's window.

    Resolution self-heals: persisted window_id (if live) → live tmux list-panes
    match by cwd+runtime (stamped back) → bounded retry (covers a worker still
    mid-SessionStart). If no live window resolves, RAISE — there is no silent
    inbox; an unresolvable worker is dead/closed and the manager must
    resume_worker or re-spawn. `delivered` = typed best-effort (driver calls
    swallow failures), not a receipt.
    """
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
            record = _find_worker_by_name_or_sid(worker)   # claim may have landed
    if not window_id:
        raise ValueError(
            f"'{record['name']}' has no live window (worker dead/closed?) — "
            "resume_worker or re-spawn; message NOT delivered"
        )
    _send_text(window_id, MANAGER_MARKER + text)
    # Stamp the tasking episode: done files live 24h, so without a lower bound
    # a follow-up wait_for_worker would instantly return the PREVIOUS task's
    # done event. Stamped after a successful type, in the same process that
    # serves wait_for_worker — the stamp always precedes a subsequent wait.
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
    """auto_resume lane: live delivery, else resume the closed worker and deliver.

    Wraps send_manager_to_worker_impl. On a live-path failure, probes closed/
    for a resumable record under `worker` (NAME-keyed — a sid identifier gets
    the live path only). Nothing resumable → combined raise; there is still NO
    silent inbox. Resumable → resume_worker_impl (all its guards apply: active-
    holder refusal, in-flight dedup, sid-keyed registration confirm; ok=False →
    raise with the closed record intact), a bounded TUI-readiness wait, then
    delivery to the ACTUAL registered handle (can come back suffixed). Resumes
    the NEWEST resumable closed record — a crash-orphaned newer session (dead
    pid, never archived) is pruned, not resumable, so an older episode can be
    the one revived; same semantics as the manual resume_worker → send dance.
    """
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


def send_manager_to_manager_impl(name: str, text: str) -> dict:
    """Manager→manager: DIRECT delivery with an idle guard, loud on failure.

    Resolves the peer among active manager records (workers are never matched, even if
    a worker shares the name), self-healing a missing window_id via
    `_resolve_manager_window` (stamped back). Because a human may be typing in a peer
    manager's window, the direct type is guarded by an idle check:
    - peer window idle (just `❯`) → type the CONTENT directly → status `delivered_live`.
    - human-typed text / queued-messages indicator → do NOT type → `peer_busy`
      (delivered=False); there is NO silent inbox, so the caller retries when the peer
      is free.
    - no live window (dead/closed) or an unreadable window → RAISE: a failed send is a
      hard error, not a silent queue.
    """
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
        _send_text(window_id, text)
        return {"status": "delivered_live", "manager": name}
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
    # Graceful pane close (SIGHUP → grace → SIGKILL) lets the runtime's
    # SessionEnd hook fire — which in turn runs selffix-trigger.sh and the
    # orchestrator session-end archive. SIGTERM bypassed those and required
    # a manual selffix trigger; this path is empirically verified to fire
    # SessionEnd even for processing workers mid-tool-call.
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
    """(task_key, count-of-own-artifacts) from the worker's assignment, or None.

    Forensic stamp for done events: 0 on a keyed task = the discipline was
    skipped — the manager's nudge signal. Best-effort by design; worker_done
    must never fail because the store is unreadable."""
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
    """Write a one-shot done event for an active worker. Returns the event id.

    Self-heals when the active record is gone but the claimed assignment
    survives: a reaped registration must not strand a finished worker's
    completion signal — the assignment carries the routing fields (name,
    parent manager); spend died with the record and is unknowable."""
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

# A done event landing within this many seconds BEFORE a re-task is treated as
# the just-finished task's legit completion, not a stale event — without the
# grace, a worker_done crossing a re-task/nudge in flight would be gated out
# and hang the wait until timeout. Real stale events are minutes-to-hours old.
_RETASK_GRACE_SEC = 2.0

async def wait_for_worker_impl(
    name: str,
    timeout_sec: int = 3600,
    _poll_interval: float = 1.0,
    manager_name: str | None = None,
) -> dict:
    """Block until the named worker writes a done event or its session exits.

    Termination conditions:
    1. A done/<sid>-*.json exists (or appears during the wait) → {"found": "done", ...}
       with the LATEST event by completed_at when multiple exist.
    2. The active record disappears with no done event written → {"found": "exited", ...}.
       Also returned immediately when the worker was already closed at start of wait.
    3. timeout_sec elapses → TimeoutError.
    Raises ValueError if no active/closed record AND no done event matches `name`.
    Done events older than the record's tasking episode (`tasked_at`, or
    `processing_since` while state=processing, minus a 2s grace) are ignored —
    they belong to a task the worker already reported before this wait started.

    `manager_name` (optional) scopes lookups to a single manager — only workers
    whose parent_manager_name matches will be resolved (legacy records with no
    parent are also matched, per _matches_manager).
    """
    paths.ensure_dirs()

    sid = None
    found_via_active = False
    manager_holder = None
    active_record = None
    for record in state.list_json_in(paths.ACTIVE):
        # Workers only: a MANAGER can hold the name (pools were combined before
        # the role split, legacy records persist, and callers can pass a
        # manager's name by mistake) and would pin the wait on a sid that never
        # writes a done event — hanging the call until TimeoutError instead of
        # failing fast. closed/ needs no filter: only workers are ever archived
        # there.
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

    # Tasking-episode lower bound on done events (see _RETASK_GRACE_SEC): a
    # done file older than the worker's current tasking episode belongs to a
    # PREVIOUS task and must not satisfy this wait. Records without stamps
    # (pre-upgrade) get bound 0 — old behavior. Closed/sid-less paths keep no
    # bound: returning the latest done for a finished worker is the correct
    # harvest. `active_record` is captured at the `break` above precisely so
    # this can never pick up an unrelated record left over from loop iteration.
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
        # rglob recurses the per-manager subdirs, the _unscoped bucket, and any
        # legacy flat done/<sid>-<id>.json files (written before per-manager scoping
        # or by an old manager still running). _matches_manager does the routing.
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
    """List closed worker records (auto-closed for idleness or manually moved)."""
    if limit is not None and limit <= 0:
        raise ValueError("limit must be a positive integer")
    _prune_stale_assignments()      # opportunistic, cold path (list_workers stays hot/untouched)
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
    """True if the runtime resume command has a non-empty transcript to restore.

    Reuses find_session_log (Claude: ~/.claude/projects/*/<sid>.jsonl; Codex:
    ~/.codex/sessions/**/rollout-*-<sid>.jsonl). A missing or empty transcript
    means resume fails instantly, so a record pointing at one must not be chosen.
    """
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
    """Return (path, record) for the best closed record named `name`.

    A worker can be closed more than once (autoclose churn) leaving duplicate records
    under one name. Picking the filesystem-arbitrary first iterdir match can grab a
    stale/junk session. Instead collect ALL name-matches, keep only those whose resume
    transcript still exists, and return the newest (max closed_at) among them. If none
    have a live transcript, raise naming the sids tried — resuming any of them would
    fail instantly.
    """
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
    """Routing `name` ∪ `funny_name` over all active records — the taken-set
    for fresh funny-name rolls. Pools are role-disjoint for new rolls, but
    active legacy records may carry old-pool names, so candidates are checked
    against every live display name regardless of role."""
    names_set: set[str] = set()
    for record in state.list_json_in(paths.ACTIVE):
        for key in ("name", "funny_name"):
            if record.get(key):
                names_set.add(record[key])
    return names_set


def become_manager_impl(
    claude_sid: str,
    iterm_sid: str = "",
    domain: str | None = None,
    name: str | None = None,
) -> dict:
    """Register this session as a manager in the given domain.

    - `domain` defaults to "general".
    - `name` is auto-rolled as <adjective>-<creature> if not supplied; uniqueness
      is enforced across ALL active records (workers + other managers).
    - If the caller passes a pre-rolled name and it's taken, we auto-suffix
      via _resolve_unique_name (so /manager-resume can preserve the previous
      manager's name even if a brief overlap window exists during takeover).
    - Managers are Claude-only; the record is always stamped runtime="claude".
    """
    paths.ensure_dirs()
    _migrate_flat_manager_memory()
    _prune_stale_active_records()
    # /manager calls become_manager twice from one physical session: once with a
    # placeholder sid before the real session_id is in context, then with the real
    # sid. Both share this process's pid, so the first leaves an alive-pid ghost
    # that _prune_stale_active_records can't reap. Drop any same-pid record under a
    # different sid before registering. No false positives: one OS process = one
    # Claude Code session, and managers each run in their own tab/process.
    pid = os.getppid()
    # Record the manager's tmux pane so close-on-takeover and send_manager_to_manager
    # can target it. The MCP server runs as a foreground process in the manager's tmux
    # pane, so it inherits the pane id — used as the source unless the caller
    # passed an explicit iterm_sid (param wins, for testability + robustness).
    if not iterm_sid:
        iterm_sid = (get_driver().current_pane_id() or "")
    _prune_same_pid_ghosts(pid, keep_sid=claude_sid, keep_window_id=iterm_sid)
    domain = domain or DEFAULT_DOMAIN
    existing = _active_display_names()
    # If the existing record for our own sid is in there, drop it from the set
    # so re-registering doesn't reject our own name(s).
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
    # Backfill runs after register_self so the just-registered manager is counted.
    # If it's the only one, legacy parent-null workers get attributed to it.
    _backfill_legacy_workers()
    return {"ok": True, "name": name, "domain": domain, "runtime": "claude"}

# --- Manager recreation / handoff ---

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

def prepare_recovery_handoff_impl(from_sid: str, trigger_reason: str = "account-flip-recovery") -> dict:
    """Synthesize a handoff FOR a bricked predecessor that cannot take turns.

    Called by the incoming recovery manager (fresh session, fresh MCP code) as
    its first act — design A3-v2: the monitor only flips + launches; the
    successor does the takeover. Shape-parity with prepare_handoff_impl so
    become_manager_with_takeover consumes it unchanged. No distill here: the
    successor dispatches it post-takeover (the predecessor's account is the
    bricked one; the successor's env carries the healthy token).
    """
    paths.ensure_dirs()
    manager_record = state.read_json(paths.ACTIVE / f"{from_sid}.json")
    if manager_record is None or manager_record.get("agent") != "manager":
        raise ValueError(
            f"session {from_sid} is not an active manager; cannot synthesize recovery handoff")
    # A double-launch race creates two unconsumed handoff files, which is harmless —
    # handoffs/ has no pruning, so orphaned files accumulate like normal. The real
    # double-takeover guard is two-pronged: (1) become_manager_with_takeover unlinks
    # the predecessor's active record (paths.ACTIVE/{from_sid}.json) on first
    # consumption, so a second call to this function for the same from_sid raises
    # ValueError("not an active manager") before any handoff is synthesized;
    # (2) even if a racing session registers via become_manager, _resolve_unique_name
    # (~:996-1000) auto-suffixes the inherited name so two registrations never collide.
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
    # Gracefully close the old manager's tmux window — SIGHUP → grace → SIGKILL
    # gives Claude Code time to run SessionEnd, which fires selffix-trigger.sh
    # and the manager-memory distill fallback if needed. SIGTERM bypassed
    # those hooks and required a manual selffix trigger.
    old_record = state.read_json(paths.ACTIVE / f"{takeover_from}.json")
    old_pid = old_record.get("pid") if old_record else None
    old_iterm_sid = state.window_id_of(old_record or {})
    inherited_name = (old_record or {}).get("name") or handoff.get("manager_name")
    inherited_domain = (old_record or {}).get("domain") or handoff.get("domain") or DEFAULT_DOMAIN
    if isinstance(old_pid, int) and _pid_alive(old_pid):
        # Closing the predecessor's window is a required handoff step. Prefer its recorded
        # iterm_sid; if that's empty (legacy record from before managers stored their
        # window), resolve the window via tmux list-panes by sid/name — excluding the
        # incoming manager's own window so we never close the wrong window.
        target_window = old_iterm_sid
        if not target_window:
            new_window = iterm_sid or (get_driver().current_pane_id() or "")
            target_window = _resolve_manager_window(takeover_from, inherited_name, exclude_id=new_window)
        if target_window:
            _close_window(target_window)
    # Drop the old manager's active record so register_self_impl below doesn't trip on
    # the duplicate name. (Window close is async; SessionEnd may still be propagating.)
    paths.ACTIVE.joinpath(f"{takeover_from}.json").unlink(missing_ok=True)
    # Stay consistent with session_end / kill_worker_impl: any pending questions
    # addressed to the old sid would orphan, and its questions shouldn't be inherited.
    _drop_questions_for_worker(takeover_from)
    # Register the new manager. Inherit name + domain so workers' parent_manager_name
    # references stay valid (no re-parent needed: the inherited name is identical).
    become_manager_impl(
        claude_sid=claude_sid,
        iterm_sid=iterm_sid,
        domain=inherited_domain,
        name=inherited_name,
    )
    # Mark handoff consumed.
    now = time.time()
    handoff["consumed_at"] = now
    handoff["to_sid"] = claude_sid
    state.write_json_atomic(handoff_path, handoff)
    # Append to trigger log.
    narrative = handoff.get("narrative_summary") or ""
    _append_trigger_log({
        "ts": now,
        "from_sid": takeover_from,
        "to_sid": claude_sid,
        "handoff_id": handoff_id,
        "trigger_reason": handoff.get("trigger_reason"),
        "narrative_excerpt": narrative[:200],
    })
    return {"ok": True, "name": inherited_name, "domain": inherited_domain, "runtime": "claude"}


# --- MCP tool registrations ---

def _manager_name_from_sid(manager_sid: str | None) -> str | None:
    """Look up the manager's name from its sid. Returns None if not found (which
    in turn means filters degrade to wildcard behavior — back-compat).
    """
    if not manager_sid:
        return None
    record = state.read_json(paths.ACTIVE / f"{manager_sid}.json")
    if record is None:
        return None
    return record.get("name")


def _resolve_parent_manager(manager_sid: str | None) -> tuple[str | None, str | None]:
    """Resolve manager_sid → (parent_manager_name, warning) for the spawn path.

    - manager_sid falsy (None/empty)        → (None, None): intentional legacy
      single-manager wildcard; no warning.
    - manager_sid resolves to a live manager → (name, None).
    - manager_sid truthy but unresolvable    → (None, warning): no ACTIVE record
      for that sid, so the worker would register UNSCOPED (parent_manager_name=null)
      and its turn-end/done/question events would route to _unscoped/ instead of
      back to the manager. The usual cause is passing the manager's funny NAME
      instead of its session UUID.
    """
    parent_manager_name = _manager_name_from_sid(manager_sid)
    if manager_sid and parent_manager_name is None:
        warning = (
            f"manager_sid {manager_sid!r} did not resolve to an active manager record; "
            "worker spawned UNSCOPED (parent_manager_name=null) — its events will not "
            "route to that manager. Pass the manager's session UUID, not its funny name."
        )
        return None, warning
    return parent_manager_name, None


def _resolve_manager_name_for_filter(manager_sid: str | None, tool: str) -> str | None:
    """Like _manager_name_from_sid, for the READ/filter tools. Warns to stderr when
    a non-empty manager_sid fails to resolve — otherwise the filter silently degrades
    to wildcard and returns every manager's records, not just the caller's. Returns
    the resolved name (or None) so the return shape of the calling tool is unchanged.
    """
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
    the message content directly into their pane (status delivered_live). If a human is
    mid-typing, does NOT type and returns peer_busy (delivered=False) so it never
    clobbers the peer's input — retry when the peer is free; there is NO silent inbox.
    RAISES if the peer has no live window (dead/closed) or an unreadable one — a failed
    send is a hard error, not a queue. Resolve peer names via list_managers(). Returns
    {status, manager} (delivered_live) or {status, delivered, manager} (peer_busy)."""
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
    Returns {ok, name, domain, runtime}.
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
    # Distill — best-effort. Synchronous because the slash command blocks on it.
    distill_path = distill_and_write_memory(claude_sid, domain=domain)
    # Move active → no-archive for managers (mirrors session_end's behaviour).
    paths.ACTIVE.joinpath(f"{claude_sid}.json").unlink(missing_ok=True)
    _drop_questions_for_worker(claude_sid)
    # Close the manager's own tmux window so the user doesn't have to.
    _close_window(iterm_sid)
    return {"ok": True, "distill_path": distill_path, "name": name, "domain": domain}


def _close_window(window_id: str) -> None:
    get_driver().close(window_id)


def _terminal_ls() -> list | None:
    """Return the parsed tmux list-panes window tree, or None if it can't be read."""
    return get_driver().ls()


def _resolve_manager_window(sid: str, name: str, exclude_id: str = "") -> str:
    """Best-effort resolve a manager's tmux pane id when its iterm_sid wasn't
    recorded (legacy record). Managers share OS-window 1; match a window by the
    predecessor's session id (window env CLAUDE_CODE_SESSION_ID) or, failing that,
    by its manager name appearing in the tab/window title. `exclude_id` skips the
    caller's own window so a takeover never closes the incoming manager's window.
    Returns "" if nothing resolves (caller then skips the close silently).
    """
    data = _terminal_ls()
    if not data:
        return ""
    # Pass 1: session-id env is the unambiguous signal — always safe to match,
    # even without an exclude_id (a sid identifies exactly one window).
    for os_window in data:
        for tab in os_window.get("tabs", []):
            for w in tab.get("windows", []):
                wid = str(w.get("id", ""))
                if not wid or wid == str(exclude_id):
                    continue
                env = w.get("env") or {}
                if sid and env.get("CLAUDE_CODE_SESSION_ID") == sid:
                    return wid
    # Pass 2: name-in-title. The no-exclude_id caller is send_manager_to_manager
    # (a send, not a close), so matching the peer's own titled window is the intent;
    # active manager names are unique and titles carry the full `<name> · <domain>`
    # form, so no cross-manager prefix collision.
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

    Verifies the handoff matches `takeover_from`, SIGTERMs the old manager,
    inherits its name + domain so workers' parent_manager_name references stay
    valid, marks the handoff consumed, and appends to manager-triggers.jsonl.
    """
    return become_manager_with_takeover_impl(claude_sid, takeover_from, handoff_id, iterm_sid)

async def _resolve_old_manager_window_match(handoff: dict) -> str | None:
    """Resolve a `--match` selector that places the recreated manager tab in the
    OLD manager's OS-window, or None to fall back to focus-follows behavior.

    Falls back (returns None) if `from_sid` is missing, the old manager's active
    record is gone, its `iterm_sid` is falsy, or that window id no longer appears
    in tmux list-panes (old manager already dead) — so recreate never hard-fails.
    """
    from .spawner import window_id_exists  # lazy: mirror spawn_worker_tab import
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
    # window_id: (not id:) — iterm_sid is a window id, and `launch --match=id:N`
    # resolves a tab id first; a window id colliding with an unrelated tab's id
    # would land the recreated manager tab in the wrong OS-window.
    return f"window_id:{old_window_id}"


async def spawn_replacement_manager_impl(handoff_id: str) -> dict:
    """Testable core of spawn_replacement_manager."""
    # Lazy import: don't crash MCP startup if terminal/runtime launch support is unavailable.
    from .spawner import spawn_worker_tab
    handoff_path = paths.HANDOFFS / f"{handoff_id}.json"
    handoff = state.read_json(handoff_path)
    if handoff is None:
        raise ValueError(f"no handoff with id {handoff_id}")
    if handoff.get("consumed_at") is not None:
        raise ValueError(f"handoff {handoff_id} already consumed")
    cwd = os.getcwd()
    initial_prompt = f"/manager-resume {handoff_id}"
    target_window_match = await _resolve_old_manager_window_match(handoff)
    try:
        async with asyncio.timeout(15):
            window_id, _ = await spawn_worker_tab(
                cwd=cwd,
                initial_prompt=initial_prompt,
                # Inherit the predecessor's funny name so the incoming tab's
                # placeholder IS the eventual name (become_manager_with_takeover does
                # the authoritative rename). Empty when there's no recorded name →
                # CLAUDE_WORKER_NAME="" and the SessionStart hook rolls a fresh funny
                # name instead of the literal "manager".
                name=handoff.get("manager_name") or "",
                agent="manager",
                tab_title="manager (incoming)",
                target_window_match=target_window_match,
                runtime="claude",
                # Manager lane is pinned (orch-audit model-allocation): without
                # an explicit flag the tab rides the spawner's WORKER default,
                # and a worker-default change would silently move managers too;
                # value from dockwright.toml [spawn].manager_model.
                extra_args=["--model", config.manager_model()],
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

# Names with a resume in flight in THIS process. resume_worker is a manager-side
# tool and each manager session runs one MCP server, so concurrent resume calls
# for one name land on this server's single event loop — a plain set (checked and
# updated with no await in between) is race-free here. It exists to stop a double
# resume from attaching two processes to the same transcript; cross-process
# double-resume (two managers racing the same worker name) is not covered.
_RESUMES_IN_FLIGHT: set[str] = set()


async def resume_worker_impl(
    name: str,
    _registration_timeout_sec: float = 10.0,
    _poll_interval: float = 0.5,
) -> dict:
    """Resume a previously closed worker by name (testable core of resume_worker).

    Opens a new tmux window in the worker's original cwd using the closed record's
    runtime (`claude --resume <sid>` or `codex resume <sid>`), restoring the full
    conversation history. The SessionStart hook re-registers the session into
    active/ under the same name. The closed/ record is deleted only AFTER the
    resumed session is confirmed to have registered into active/ — a window id
    alone only means the tab opened, not that the session launched and
    re-registered.

    Registration is confirmed by the resumed session's OWN sid re-appearing in
    active/ (both runtimes reuse the session id on resume), with the registered
    record's actual name surfaced in the result — so a foreign session claiming
    the requested name inside the confirmation window can neither false-confirm
    nor get the closed record deleted. A codex-lane fallback also accepts a
    record that claimed the name and did not exist pre-spawn, in case a codex
    build ever rolls a fresh thread id on resume.
    """
    from .spawner import spawn_worker_tab  # lazy import to mirror spawn_worker
    closed_path, record = _find_closed_record_by_name(name)
    sid = record.get("claude_sid")
    cwd = record.get("cwd") or os.getcwd()
    if not sid:
        raise ValueError(f"closed worker '{name}' has no claude_sid; cannot resume")
    # Refuse while ANY live session holds the name. The registration poll below is
    # keyed on the name, so a foreign holder (e.g. a fresh worker spawned under the
    # same task name after this one closed) would "confirm" instantly: the closed
    # record gets deleted and the result claims name=<name>, while the resumed
    # session actually re-registers suffixed (<name>-2) — routing follow-ups to the
    # wrong worker. Erroring out also matches the manager contract: a live worker
    # takes follow-ups via send_manager_to_worker, never a second spawn.
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
            # A manager record can hold the name — pools were combined before
            # the role split, legacy records persist, and callers can pass a
            # manager's name by mistake; the worker-only tools would raise
            # "no worker named" for this name, so don't point at them.
            hint = f"the name is held by an active {holder.get('agent') or 'session'}"
        raise ValueError(f"'{name}' is already active; {hint}")
    # A live record under the closed record's OWN sid (different name, so the
    # name guard above passed) means the session is already running: spawning
    # `--resume <sid>` again would attach a second process to the same
    # transcript, and the sid-keyed confirmation below would instantly
    # false-confirm on the pre-existing record.
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
    # Preserve the worker's parent_manager_name across the close→resume cycle so
    # routing filters keep scoping it to the right manager. Without this, an
    # auto-closed (stale_monitor) or cmd+w-closed worker comes back unscoped and
    # disappears from strict per-manager views.
    parent_manager_name = record.get("parent_manager_name")
    env = {"CLAUDE_PARENT_MANAGER": parent_manager_name} if parent_manager_name else None
    runtime = record.get("runtime") or "claude"
    # Resumed claude workers must carry the SAME Remote-Control flags as a fresh
    # spawn (spawn_worker_impl) so resuming never re-enrolls the worker on the
    # phone / Desktop. codex resume must NOT get --settings — it's Claude-only and
    # _validate_codex_extra_args rejects it.
    extra_args = _claude_worker_settings_args() if runtime == "claude" else None
    # Snapshot BEFORE spawning: the codex-lane fallback below accepts a name claim
    # only from a record that did not exist at this point.
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
    # A window id only means the tab opened — not that the selected runtime
    # launched or that the SessionStart hook re-registered the worker. Delete the
    # closed record ONLY once registration is confirmed, so a session that fails
    # to resume stays recoverable. Confirmation is keyed on the resumed session's
    # sid — `claude --resume <sid>` (and codex resume) reuse the session id — NOT
    # on the name: a foreign session claiming the name inside this window (e.g. a
    # concurrent spawn_worker under the same task name) must not confirm. If the
    # name was stolen and the hook suffixed the resumed session, the result
    # carries the ACTUAL registered handle.
    deadline = time.monotonic() + _registration_timeout_sec
    while True:
        resumed = state.read_json(paths.ACTIVE / f"{sid}.json")
        if resumed is not None:
            # A2: stamp the spawn-returned window id directly — the resume lane
            # carries no assignment sidecar (it reuses the original sid), so the
            # SessionStart override never fires; without this a worker whose
            # shell rebuilt the wrong driver would re-register window_id="".
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
            # Codex sid reuse on resume is less battle-proven than Claude's: if a
            # codex build rolls a fresh thread id, the old sid never re-appears.
            # Accept a record that claimed the name and did NOT exist pre-spawn,
            # and point the result at the sid that actually registered. Residual:
            # a foreign same-name registration inside this window can still match
            # here — codex lane only, narrowed by the pre-spawn snapshot.
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
    """Read a preset markdown file from paths.PRESETS. Raise ValueError if missing."""
    preset_path = paths.PRESETS / f"{preset}.md"
    if not preset_path.is_file():
        available = sorted(p.stem for p in paths.PRESETS.glob("*.md")) if paths.PRESETS.is_dir() else []
        raise ValueError(
            f"preset '{preset}' not found at {preset_path}; available: {available}"
        )
    return preset_path.read_text()


def _claude_worker_settings_args() -> list[str]:
    """CLI args injecting a claude worker's --settings (MCP auto-approval + Remote Control).

    enableAllProjectMcpServers auto-approves every server in the worktree's .mcp.json so a
    fresh-worktree worker never blocks on Claude Code's interactive "N new MCP servers found in
    this project" startup prompt. That prompt fires BEFORE the SessionStart hook registers the
    worker: a fresh git-worktree dir has no .claude/settings.local.json enable record, so all
    .mcp.json servers are "pending" (Claude reads MCP approval from the merged settings chain,
    not the legacy ~/.claude.json projects map). The flag short-circuits that gate — empirically
    confirmed against claude v2.1.186.

    Remote Control: default OFF so workers don't auto-enroll on the user's phone; set
    CLAUDE_ORCH_WORKER_RC=1 to enable it via the reliable --remote-control flag
    (anthropics/claude-code #54527/#29929/#41036).
    """
    settings: dict = {"enableAllProjectMcpServers": True}
    if os.environ.get("CLAUDE_ORCH_WORKER_RC", "").strip() == "1":
        return ["--settings", json.dumps(settings), "--remote-control"]
    settings["remoteControlAtStartup"] = False
    settings["disableRemoteControl"] = True
    return ["--settings", json.dumps(settings)]


async def _confirm_spawn_registration(
    name: str,
    timeout_sec: float,
    poll_interval: float,
) -> dict | None:
    """Poll the active plane until a worker record under `name` appears, or the deadline passes.

    Fresh-spawn analogue of _spawn_and_confirm_resume's confirm loop. Keys on `name` (not sid):
    a fresh spawn has no pre-registration sid (it's born at the worker's SessionStart) and the
    assignment_id is claimed onto the assignments plane, not the active record. `name` is freshly
    uniquified by _resolve_unique_name at spawn, so its steal window in this short poll is
    negligible (unlike resume, where the name pre-exists in closed/). Returns the active record
    dict, or None on timeout.

    The worker's SessionStart hook re-resolves the name via _resolve_unique_name, so if a
    collision occurs inside the spawn->registration window the worker registers under a suffixed
    name (`name-2`) and this exact-match poll then times out -> a BENIGN false `no_register`: the
    worker is in fact live and its pending assignment is left intact, so the manager just gets a
    spurious heads-up. (Exact match is deliberate; fuzzy/startswith matching would risk false
    positives, which are worse than this rare benign false negative.)
    """
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
    # Lazy import: don't crash MCP startup if terminal/runtime launch support is unavailable.
    from .spawner import normalize_runtime, spawn_worker_tab, usage_spawn_gate
    runtime = normalize_runtime(runtime)
    _validate_task_key(task_key)
    gate = usage_spawn_gate(force=force)
    if gate.get("status") == "paused":
        return gate
    if cwd is None:
        home = paths.ensure_worker_home()
        cwd = str(home) if home.is_dir() else os.getcwd()
    if name is None:
        name = f"worker-{int(time.time())}"
    name = _resolve_unique_name(name)
    # The ownership record stores the caller's actual ask — capture BEFORE the
    # preset expansion below (boilerplate is reconstructable from the preset name).
    raw_prompt = initial_prompt
    # Single resolution point for the grouping key: footer and assignment record
    # must never diverge. Explicit task_key ALWAYS wins; derivation stays
    # configured-regex-only (stretching it to arbitrary names is how the
    # WORKER-<epoch> garbage-key bug happened).
    ticket = task_key or _derive_ticket(name, raw_prompt)
    if preset is not None:
        initial_prompt = f"{_resolve_preset(preset)}\n\n---\n\n{initial_prompt}"
    if ticket and (raw_prompt or "").strip():
        initial_prompt += _artifact_discipline_footer(ticket)
    if (raw_prompt or "").strip():
        initial_prompt += _repo_sync_footer()
    if runtime == "claude":
        extra_args = _claude_worker_settings_args() + (extra_args or [])
    else:
        extra_args = extra_args or []
    # Operators inject worker env via [spawn.env] (e.g. a headless-autonomy flag a
    # worker cannot self-set — a Bash `export` never reaches a spawned tool's process
    # env, so the spawn path is the only durable home). Caller-supplied env still
    # wins; codex is excluded — it has its own protocol.
    if runtime == "claude":
        env = {**config.spawn_env(), **(env or {})}
    # Stamp the spawning manager's name into the worker's env so its SessionStart
    # hook can record parent_manager_name on the active record (and routing
    # filters work). Default None = legacy single-manager behaviour. A non-empty
    # manager_sid that doesn't resolve (e.g. the funny NAME passed instead of the
    # UUID) yields a warning and an UNSCOPED worker rather than a silent drop.
    parent_manager_name, manager_resolution_warning = _resolve_parent_manager(manager_sid)
    if manager_resolution_warning:
        print(f"spawn_worker: {manager_resolution_warning}", file=sys.stderr)
    if parent_manager_name:
        env = {**(env or {}), "CLAUDE_PARENT_MANAGER": parent_manager_name}
    # Durable ownership record:
    # pending file written pre-launch, claimed by the worker's SessionStart hook
    # via the env-carried id. Resume/promote/replacement lanes never set the id.
    assignment_id = uuid.uuid4().hex
    _write_pending_assignment(
        assignment_id, name, raw_prompt, preset, cwd,
        manager_sid, parent_manager_name, runtime,
        ticket=ticket,
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
        # Any other failure (codex extra-args ValueError, CancelledError, ...)
        # must not leak a pending file either — no worker will ever claim it.
        paths.pending_assignment_path(assignment_id).unlink(missing_ok=True)
        paths.pending_window_path(assignment_id).unlink(missing_ok=True)
        raise
    # A2: stamp the launched window id for the worker's SessionStart to claim —
    # driver-independent of whatever TerminalDriver the worker's shell builds.
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
    if manager_resolution_warning:
        result["warning"] = manager_resolution_warning
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
            prompt (e.g. ["--model", "gpt-5.5"]). Claude workers still get
            the orchestrator's remote-control-off `--settings` flags before
            caller args. Codex workers get `--ask-for-approval never --sandbox
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
        manager_sid: Caller's own claude_sid. When supplied, the worker's active record
            will have `parent_manager_name` set to this manager's name, scoping it to
            this manager for routing (list_workers / wait_for_worker / questions).
            Default None = legacy single-manager behaviour.
        runtime: Worker CLI runtime: "claude" (default, backward compatible) or
            "codex". The runtime is stamped into active records via
            CLAUDE_WORKER_RUNTIME and returned by list_workers.
        task_key: Grouping key stamped into the worker's assignment record —
            joins the worker into `pipeline_status(task_key)` and the artifact
            store under `artifacts/<task_key>/`. Use a stable slug for personal
            multi-agent tasks with no tracker key (e.g. "yt-bot-public"); pass
            the SAME slug on every spawn of that task's workers. Explicit
            task_key always wins over derivation; when omitted, the key is
            derived from the first configured-key-shaped reference in name/prompt
            (only when [task_keys] key_regex is set — arbitrary names are never
            auto-derived, and the generic default derives nothing).
            Validated fail-fast: must be a stable [A-Za-z0-9_-] slug; blank
            raises (omit entirely to derive).
            Keyed spawns (explicit task_key or derived) get an
            artifact-discipline footer appended to the prompt; every
            non-blank prompt (keyed or not) additionally gets a repo-sync
            footer (sync a repo once before reading it). Blank prompts are
            left untouched.
        force: Bypass the usage breaker + both-hot pause for THIS spawn only (still
            headroom-weighted, still skips bricked accounts). Use for a genuinely
            urgent spawn when the gate returned {"status":"paused"}. If the chosen
            account is truly maxed the worker will brick and stale_monitor's flip
            recovers it.

    Returns:
        On a normal spawn, the worker record dict. May instead return
        `{"status":"paused", "reason", "a_pct", "b_pct", "earliest_reset_ts",
        "retry_after_s"}` instead of spawning when every selectable account is
        >=88% of its limit; pass `force=True` to override.
    """
    return await spawn_worker_impl(initial_prompt, name, cwd, extra_args, env, preset, manager_sid, runtime, task_key, force)

# --- Artifact store --------------------------------------------------------
#
# Two durability planes:
#   artifacts/<task_key>/<phase>.<name>.md  — document plane, directory-as-index
#   assignments/<sid>.json                — ownership plane, spawn-authored
# Correctness reduces to per-file POSIX atomic rename: every artifact file has
# exactly one writer (keyed (phase, name)), reads are lock-free snapshots, and
# events.jsonl is the single shared append-only structure (atomic small-line
# O_APPEND writes, state.append_event).


def _write_artifact_atomic(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique tmp name (pid+uuid) so an accidental double-writer can't stomp one
    # tmp; os.replace is atomic regardless. Same temp+rename idiom as
    # state.write_json_atomic. Readers glob *.md only, so .tmp is invisible.
    tmp = path.parent / f".{path.stem}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    tmp.write_text(text)
    os.replace(tmp, path)


def artifact_put_impl(task_key, phase, name, content, status, writer_sid,
                      contract_hash=None, read_set=None) -> dict:
    if status not in ("partial", "complete"):
        raise ValueError(f"status must be 'partial'|'complete', got {status!r}")
    paths.ensure_dirs()
    written_at = time.time()
    stamp = {"phase": phase, "name": name, "status": status, "writer_sid": writer_sid,
             "contract_hash": contract_hash, "written_at": written_at,
             "read_set": read_set or []}
    path = paths.artifact_path(task_key, phase, name)
    # FILE FIRST (authoritative), then audit (best-effort): a crash between the
    # two yields an artifact with no audit line — safe. Never the reverse.
    _write_artifact_atomic(path, state.serialize_artifact(stamp, content))
    state.append_event(paths.artifact_events_path(task_key), {
        "type": "artifact_put", "phase": phase, "name": name,
        "actor_sid": writer_sid, "status": status, "contract_hash": contract_hash})
    return {"ok": True, "path": str(path), "written_at": written_at, "status": status}


def artifact_get_impl(task_key, phase, name) -> dict:
    path = paths.artifact_path(task_key, phase, name)
    # EAFP, not exists()+read: a prune between the check and the read would leak
    # FileNotFoundError past callers that handle the documented ValueError.
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise ValueError(f"no artifact {phase}.{name} for {task_key}") from None
    stamp, body = state.parse_artifact(text)
    return {**stamp, "content": body, "path": str(path)}


def _prune_stale_artifacts() -> None:
    """Reap ticket dirs whose NEWEST entry is >30d old (an in-flight or
    recently-resumed pipeline is never collected) + aged .tmp orphans.

    Accepted residual race (spec §9): a put resurrecting a dir at exactly the
    dormancy boundary, concurrent with another session's prune, can be swept.
    """
    if not paths.ARTIFACTS.is_dir():
        return
    now = time.time()
    cutoff = now - paths.ARTIFACT_RETENTION_DAYS * 86400
    for d in paths.ARTIFACTS.iterdir():
        if not d.is_dir():
            continue
        # Per-item stat guard: rglob stats lazily, and a concurrent put's
        # tmp→md rename (or a peer MCP process's rmtree) can vanish an entry
        # mid-scan — that must skip the entry, not crash a read-only fold.
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
        # Generator-level guard too: rglob itself can raise OSError mid-iteration
        # on py3.11/3.12 pathlib when a directory vanishes under the walk.
        for tmp in paths.ARTIFACTS.rglob("*.tmp"):
            try:
                if tmp.stat().st_mtime < now - 3600:
                    tmp.unlink(missing_ok=True)
            except OSError:
                continue
    except OSError:
        pass


def artifact_list_impl(task_key) -> list[dict]:
    # Opportunistic, mirrors _prune_stale_active_records call sites. Sole direct
    # call site — pipeline_status/artifact_view reach it transitively (no double scan).
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
    """events.jsonl lines, parsed; malformed lines (the only residue a crash
    mid-append can leave) are skipped, never break a fold."""
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
    """(liveness, runtime) for a writer_sid, joined to the existing planes.

    done/ is per-manager-scoped (done/<bucket>/<sid>-<event_id>.json) — rglob
    ALL buckets exactly like _latest_done_event. Runtime resolves from closed/
    FIRST: done events carry no runtime, and a finished codex worker commonly
    has both a done event and a closed record — short-circuiting on "done" with
    a "claude" default would point forensics at the wrong transcript tree.
    """
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
    """The ownership record's 200-char ask, joined by sid. None-safe: a record
    without claude_sid (legacy/hand-written closed records) must not crash a
    listing via _safe_segment's empty-input raise."""
    if not sid:
        return None
    return _brief_of(state.read_json(paths.assignment_path(sid)))


def _assignments_for_ticket(ticket: str) -> list[dict]:
    if not paths.ASSIGNMENTS.is_dir():
        return []
    out = []
    for p in paths.ASSIGNMENTS.glob("*.json"):     # .pending/ is a dir → invisible to this glob
        record = state.read_json(p)
        if record and record.get("ticket") == ticket:
            out.append(record)
    out.sort(key=lambda r: r.get("spawned_at") or 0)
    return out


def pipeline_status_impl(task_key) -> str:
    _prune_stale_assignments()      # opportunistic, cold path
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
        # .get with placeholders throughout: parse_artifact deliberately skips
        # corrupt frontmatter lines, so any stamp key can be absent — the fold
        # must render the board anyway, not KeyError on the exact corruption
        # the parser tolerates.
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
        # Stamps can be missing any key (corrupt frontmatter lines are skipped
        # by the parser). The body fetch needs phase+name; recover them from the
        # filename when the stamp lost them — the path is list-derived, so the
        # <phase>.<name>.md shape is guaranteed even if the stamp is mangled.
        phase, name = a.get("phase"), a.get("name")
        if not phase or not name:
            stem_parts = Path(a["path"]).stem.split(".", 1)
            if len(stem_parts) == 2:
                phase, name = phase or stem_parts[0], name or stem_parts[1]
        try:
            full = artifact_get_impl(task_key, phase, name)
        except ValueError:
            continue          # pruned/removed between list and get — skip, don't abort the view
        excerpt = full["content"][:1200]
        out += [f"\n## {phase}.{name}  [{a.get('status', '?')}]",
                f"writer_sid={a.get('writer_sid')}  contract_hash={a.get('contract_hash')}  written_at={_iso(a.get('written_at'))}",
                f"read_set={a.get('read_set')}", "", excerpt,
                ("…[truncated]" if len(full["content"]) > 1200 else "")]
    return "\n".join(out)


def _validate_task_key(task_key: str | None) -> None:
    """Fail-fast at the spawn/promote boundary. None = "derive" and is fine;
    blank is an EXPLICIT error ("" is falsy and would silently fall through to
    key derivation); a slug sanitization would alter ("yt bot") would store
    raw in the assignment while the artifact dir gets the sanitized variant —
    diverging the join key from the dir name."""
    if task_key is None:
        return
    if not task_key.strip():
        raise ValueError("task_key must not be blank; omit it entirely to derive from prompt/name")
    if paths._safe_segment(task_key) != task_key:
        raise ValueError(
            f"task_key {task_key!r} is not a stable slug; use only [A-Za-z0-9_-] so the "
            "artifact dir and the assignment join key never diverge"
        )


def _derive_ticket(name: str, initial_prompt: str) -> str | None:
    """First configured-key-shaped reference, uppercased, or None when no key
    regex is configured ([task_keys] key_regex) — the generic default derives
    nothing and explicit task_key is then the only keying path. PROMPT wins over
    name: the canonical keyed dispatch carries the key in the prompt (<your
    keyed-dispatch command> <KEY>), and spawn's auto-generated "worker-<epoch>"
    default is itself key-shaped — name-first matching turned every name=None
    keyed spawn into a garbage "WORKER-<epoch>" key. The auto-name shape never
    contributes at all. A malformed operator regex fails open to None (never
    crashes a spawn)."""
    pattern = config.task_key_regex()
    if not pattern:
        return None
    try:
        key_re = re.compile(rf"\b({pattern})\b")
    except re.error:
        return None
    m = key_re.search(initial_prompt or "")
    if m is None and not re.fullmatch(r"worker-\d+", name or ""):
        m = key_re.search(name or "")
    return m.group(1).upper() if m else None


def _artifact_discipline_footer(task_key: str) -> str:
    """Appended to keyed spawn prompts so the publish discipline arrives without
    the manager asking (worker agent definition § "Persist pipeline artifacts" is
    the long form). The assignment record keeps the raw pre-footer ask."""
    return (
        "\n\n---\n"
        f"[orchestrator] Artifact discipline — task_key: `{task_key}`\n"
        "Persist phase outputs to the artifact store as they stabilize, without "
        f"waiting to be asked: `artifact_put(task_key=\"{task_key}\", "
        "phase=<spec|plan|implement|review|summary>, name=<repo-or-scope>, "
        "content=..., status=\"partial\"|\"complete\", writer_sid=<your sid>)`. "
        "Flip final outputs to status=\"complete\" before worker_done. Store/publish "
        "failures are non-blocking — note them and continue; they must never fail "
        "your task. Long form: worker agent definition § \"Persist pipeline artifacts\"."
    )


def _repo_sync_footer() -> str:
    """Appended to every non-blank spawn prompt: on-disk clones' working trees
    run weeks-to-months behind origin/main, so an investigator that reads them
    unsynced reads history. Sync is once-at-start; reads stay native tooling
    afterwards. The assignment record keeps the raw pre-footer ask."""
    return (
        "\n\n---\n"
        "[orchestrator] Repo freshness — sync once, then read normally\n"
        "Before you read any repo on this machine to investigate it, sync it "
        "ONCE first: `git -C <repo> fetch origin main`, then "
        "`git -C <repo> merge --ff-only origin/main` (if on main) or "
        "`git -C <repo> rebase origin/main` (feature branch). If it can't "
        "ff/rebase (local uncommitted changes / diverged — abort a conflicted "
        "rebase with `git rebase --abort`), do NOT silently read a stale tree "
        "— note it and read the specific files off `origin/main` "
        "(`git show origin/main:<path>`). Then use normal Grep/Read on the "
        "now-current tree."
    )


def _current_branch(cwd: str) -> str | None:
    """Best-effort branch snapshot at spawn time. Never raises, never blocks >2s.

    `branch --show-current` (not `rev-parse --abbrev-ref HEAD`): it resolves an
    unborn branch (repo with no commits yet) instead of erroring, and prints
    empty on detached HEAD — both degrade to None here.
    """
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
                              ticket=None) -> None:
    """Spawn-authored half of the ownership record. The spawn path cannot know
    the sid (it's born at the worker's SessionStart), so content is written here
    under a private uuid and the hook claims it to assignments/<sid>.json —
    env-keyed (CLAUDE_ASSIGNMENT_ID), exactly-once via os.replace.

    The `ticket` field (kept as the on-disk JSON key for state compat) is the
    grouping key for pipeline_status — a tracker key OR any stable personal-task
    slug, resolved once at the spawn site (spawn_worker_impl) so the assignment
    record and the prompt footer can never diverge."""
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
        "spawned_at": time.time(),
    })


def _prune_stale_assignments() -> None:
    """Assignment-plane retention (spec §14). NOT presence-keyed: "no active +
    no closed record" is exactly the SIGHUP-crash state where the assignment is
    the ONLY surviving ownership record — the case the plane exists for. So:
    mtime > 30d AND not active → prune; pendings (pre-claim orphans) at 24h.
    30d matches the resume substrate's own lifetime (Claude's cleanupPeriodDays
    transcript reaping) — an older assignment backs an unresumable session anyway.
    """
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
    """Codex resume can come back under a fresh thread id (#43 fallback lane);
    carry the ownership record to the sid that actually registered. Best-effort,
    races nothing: the old session is dead (its record came from closed/), and
    the resumed session never claims (resume sets no CLAUDE_ASSIGNMENT_ID)."""
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


# --- Worker-slot semaphore -------------------------------------------------
#
# Per-category concurrent-slot cap so N workers don't run memory-heavy commands
# (mvn test, gradle test, big docker builds) simultaneously and OOM the host.
# State lives in paths.SLOTS / "<category>.json"; a global file lock + thread
# lock guard the read-modify-write. Stale holders (sid evicted from active/ or
# pid dead) are reaped on every acquire poll.

DEFAULT_SLOT_COUNTS: dict[str, int] = {"mvn": 3}

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
    """F1 alias resolution for the artifact/pipeline tools. `task_key` is the
    canonical param; `ticket` is a deprecated alias kept one release (live callers
    still pass it). task_key wins when both are set; an empty result — neither
    supplied — fails fast, mirroring the old required-key behavior."""
    key = task_key or ticket
    if not key:
        raise ValueError("task_key is required (the `ticket=` alias is deprecated)")
    return key


@mcp.tool()
def artifact_put(task_key: str = "", *, phase: str, name: str, content: str, status: str,
                 writer_sid: str, contract_hash: str | None = None,
                 read_set: list[dict] | None = None, ticket: str = "") -> dict:
    """[WORKER] Publish an artifact for a task_key's phase. status='partial'|'complete'.
    Atomic, single-writer-per-(phase,name). `ticket=` is a deprecated alias for `task_key=`."""
    return artifact_put_impl(_resolve_task_key(task_key, ticket), phase, name, content,
                             status, writer_sid, contract_hash, read_set)


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
    mcp.run()

if __name__ == "__main__":
    main()
