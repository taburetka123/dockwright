"""Hook subcommand entry points. Each reads JSON from stdin and acts based on CLAUDE_AGENT env."""
import json
import os
import subprocess
import sys
import time
from . import config, paths, state
from .state import _pid_alive
from .terminal import get_driver


MANAGER_TAB_COLOR = ("#aa0066", "#440022")
WORKER_TAB_COLOR_IDLE = ("#444444", "#222222")
WORKER_TAB_COLOR_BUSY = ("#aa8800", "#443300")
WORKER_TAB_COLOR_QUESTION = ("#aa3300", "#441100")


def _set_tab_color(color: tuple) -> None:
    get_driver().set_tab_color(*color)


def _set_tab_title(title: str) -> None:
    get_driver().set_tab_title(title)


def _style_manager_tab(name: str = "manager", domain: str = "general") -> None:
    # "manager" is the sentinel pre-roll name/domain — omit the suffix then, but
    # always show a real domain including "general".
    suffix = f" · {domain}" if domain and domain != "manager" else ""
    _set_tab_title(f"{name}{suffix}")
    _set_tab_color(MANAGER_TAB_COLOR)


def _style_worker_tab(funny_name, task_name, color: tuple) -> None:
    if funny_name and task_name and funny_name != task_name:
        title = f"{funny_name} · {task_name}"
    else:
        title = funny_name or task_name or "worker"
    _set_tab_title(title)
    _set_tab_color(color)


def _existing_display_names(excluding_sid: str) -> set:
    """Collect routing `name` AND `funny_name` values from other active records.

    Fresh rolls check uniqueness against BOTH: the noun pools are role-disjoint
    for new rolls, but active legacy records may carry names from the old
    combined pool, so a candidate must not collide with any live display name
    regardless of role.
    """
    names = set()
    for record_path in paths.ACTIVE.glob("*.json"):
        if record_path.stem == excluding_sid:
            continue
        record = state.read_json(record_path)
        if not record:
            continue
        if record.get("name"):
            names.add(record["name"])
        if record.get("funny_name"):
            names.add(record["funny_name"])
    return names


def _has_pending_question_for_worker(sid: str) -> bool:
    if not paths.QUESTIONS.is_dir():
        return False
    for p in paths.QUESTIONS.rglob("*.json"):
        record = state.read_json(p)
        if record and record.get("worker_sid") == sid:
            return True
    return False

def _claim_pending_assignment(sid: str, registered_name: str) -> None:
    """Move the spawn-authored pending assignment to its sid-keyed home.

    Env-keyed (CLAUDE_ASSIGNMENT_ID), exactly-once via os.replace. No env →
    no claim (resume / promote / replacement-manager lanes set no id). An
    existing assignments/<sid>.json is never clobbered — a resumed session
    with a stale env id must keep its original record.
    """
    assignment_id = os.environ.get("CLAUDE_ASSIGNMENT_ID")
    if not assignment_id:
        return
    # Best-effort by design: ownership is forensic metadata, and this runs
    # inside SessionStart for every orchestrator session — NOTHING here may
    # raise past this function (a malformed env id hits _safe_segment's
    # ValueError; permissions can fail the replace or the rewrite).
    try:
        target = paths.assignment_path(sid)
        if target.exists():
            return
        pending = paths.pending_assignment_path(assignment_id)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(pending, target)
        except FileNotFoundError:
            return                   # already swept / spawn-cleanup raced — degrade silently
        record = state.read_json(target) or {}
        record.update({"claude_sid": sid, "name": registered_name, "claimed_at": time.time()})
        state.write_json_atomic(target, record)
    except Exception:
        return


def _apply_captured_window_id(sid: str, record: dict) -> None:
    """Override the driver-derived window_id with the spawn-captured one.

    The spawn path stamps the launched window id into a sidecar keyed by
    CLAUDE_ASSIGNMENT_ID (it can't write the worker record — the sid is born
    here). Reading it makes capture independent of whatever TerminalDriver the
    worker's shell built; a missing pane id would otherwise yield window_id="".
    Best-effort: nothing here may raise past SessionStart.
    """
    assignment_id = os.environ.get("CLAUDE_ASSIGNMENT_ID")
    if not assignment_id:
        return
    try:
        sidecar = paths.pending_window_path(assignment_id)
        captured = sidecar.read_text().strip() if sidecar.exists() else ""
        if captured:
            record["window_id"] = captured
            state.write_json_atomic(paths.ACTIVE / f"{sid}.json", record)
        sidecar.unlink(missing_ok=True)
    except Exception:
        return


def _iter_ancestors(start_pid: int, max_hops: int = 15):
    """Lazy strict process-ancestors of start_pid, walk order (parent first).

    Same semantics as _ancestor_chain (exclusive, cycle/failure-stopping) but
    yields on demand: a caller that only needs the nearest match (e.g.
    _resolve_session_pid) can stop as soon as it finds one instead of paying
    for the full walk's `ps` calls up front."""
    from .identity import _ppid_of
    seen: set = set()
    cursor = start_pid
    for _ in range(max_hops):
        parent = _ppid_of(cursor)
        if parent is None or parent <= 1 or parent == cursor or parent in seen:
            break
        yield parent
        seen.add(parent)
        cursor = parent


def _ancestor_chain(start_pid: int, max_hops: int = 15) -> list:
    """Strict process-ancestors of start_pid in walk order (parent first).

    Excludes start_pid itself — _ancestor_pids consumers (nested detection)
    rely on the exclusive semantics. Stops at pid 1, a self-loop, a cycle, or
    any lookup failure — best-effort by design, callers treat a short walk as
    "no match"."""
    return list(_iter_ancestors(start_pid, max_hops))


def _ancestor_pids(start_pid: int, max_hops: int = 15) -> set:
    """Strict process-ancestors of start_pid, walked ppid-by-ppid."""
    return set(_ancestor_chain(start_pid, max_hops))


def _pid_looks_like_session(pid: int) -> bool:
    """Whether pid's CURRENT process is a claude/codex session.

    Guards the nested-detection matches against pid recycling: a stale active
    record whose dead session's pid was reclaimed by a non-session process
    (the terminal itself, a build daemon) would otherwise nest-flag every new
    session whose ancestry or window it shadows. Any lookup failure reads as
    "not a session" — a missed detection degrades to the old ghost behavior,
    while a false nested flag would mute a real worker."""
    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if result.returncode != 0:
            return False
        from .sweep import _looks_like_session
        return _looks_like_session(result.stdout.strip())
    except Exception:
        return False


def _resolve_session_pid() -> int:
    """Pid of the long-lived claude/codex CLI process owning this session.

    On macOS the hook wrapper's $PPID (CLAUDE_PARENT_PID) IS the claude
    process. On Linux, claude's hook runner interposes a short-lived
    intermediate (`/bin/sh -c …` on 2.1.210), so the captured pid dies within
    seconds of registration — and recording it made every prune-bearing fleet
    call reap the record. Resolve the NEAREST ancestor whose process is a
    claude/codex session instead, walking from the captured pid and falling
    back to our own live chain when the intermediate is already gone. Nearest
    (not farthest) match keeps a nested `claude -p` child resolving to its OWN
    claude — /clear-rotation supersede and nested detection both key on it.
    No session on either chain → the captured pid (old behavior; the prune's
    pane-liveness gate covers that residual)."""
    raw = os.environ.get("CLAUDE_PARENT_PID", "")
    captured = int(raw) if raw.isdigit() else os.getppid()
    for start in dict.fromkeys((captured, os.getppid())):
        if _pid_looks_like_session(start):
            return start
        for pid in _iter_ancestors(start):
            if _pid_looks_like_session(pid):
                return pid
    return captured


def _proc_argv(pid: int) -> list | None:
    """True argv vector of a live same-user process, or None.

    Flat `ps -o command=` output is NOT usable for flag detection: it joins
    argv with spaces, so a prompt argument *containing* the text
    "--agent-id" is indistinguishable from the real flag — and that false
    positive would mute a real worker. The kernel's argv vector keeps the
    prompt as a single element, so exact-element matching is immune.

    darwin reads sysctl KERN_PROCARGS2 with the canonical two-call shape
    (size query, then a buffer of exactly that size; the kernel writes at
    most `size` bytes, so a parse surprise cannot overrun). Elsewhere:
    /proc/<pid>/cmdline. Best-effort by design — dead pid, foreign-user
    pid, short/odd buffers all read as None, never an exception (hook
    stderr is swallowed; a raise here would silently break fleet-wide
    SessionStart registration).
    """
    try:
        if sys.platform != "darwin":
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read()
            argv = [a.decode("utf-8", "replace") for a in raw.split(b"\x00") if a]
            return argv or None
        import ctypes
        import struct
        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None
        raw = buf.raw[: size.value]
        if len(raw) < 4:
            return None
        argc = struct.unpack("=i", raw[:4])[0]
        if argc <= 0:
            return None
        rest = raw[4:]
        exec_end = rest.find(b"\x00")
        if exec_end < 0:
            return None
        cursor = exec_end
        while cursor < len(rest) and rest[cursor : cursor + 1] == b"\x00":
            cursor += 1
        parts = rest[cursor:].split(b"\x00")
        argv = [p.decode("utf-8", "replace") for p in parts[:argc]]
        return argv or None
    except Exception:
        return None


def _detect_agent_team_parent(data: dict, cli_pid: int) -> dict | None:
    """Agent-team subagent detection: the nested-parent dict, or None.

    Claude Code's agent-teams feature (Agent tool / SDD subagents) launches
    each subagent as its OWN tmux session — a child of the tmux SERVER, so
    _detect_nested_parent's ancestry walk never matches, and the session
    would fall through to the default branch and register as a phantom
    manager off the polluted tmux global env (CLAUDE_AGENT=manager,
    CLAUDE_WORKER_NAME=manager). Detection keys on the launch-declared
    subagent markers instead, never on ancestry:

      1. the SessionStart payload carries an `agent_type` key — present for
         teammates, absent for interactive and headless non-teammates
         (empirically verified against live captures);
      2. redundancy against payload drift: the CLI's true argv carries an
         exact `--agent-id` element. True argv only (_proc_argv) — a real
         worker's PROMPT can contain the literal text "--agent-id", and a
         flat-string match would mute that worker.

    Attribution (agent id, parent sid -> name) is best-effort argv
    enrichment; a payload-detected teammate with unreadable argv still
    returns a dict, because muting the phantom matters more than
    attributing it. The dict always carries sid/name (possibly None) so the
    caller can treat it exactly like a _detect_nested_parent match. Any
    failure reads as "not a teammate" — never raises.
    """
    try:
        try:
            argv = _proc_argv(cli_pid) or []
        except Exception:
            argv = []
        if not data.get("agent_type") and "--agent-id" not in argv:
            return None

        def flag_value(flag: str):
            try:
                i = argv.index(flag)
            except ValueError:
                return None
            return argv[i + 1] if i + 1 < len(argv) else None

        parent_sid = flag_value("--parent-session-id")
        parent_name = None
        if parent_sid:
            parent_record = state.read_json(paths.ACTIVE / f"{parent_sid}.json")
            if parent_record:
                parent_name = parent_record.get("name")
        return {"sid": parent_sid, "name": parent_name,
                "agent_id": flag_value("--agent-id")}
    except Exception:
        return None


def _supersede_rotated_records(sid: str, cli_pid: int) -> dict | None:
    """Detect /clear sid rotation; return the prior identity record, or None.

    /clear keeps the CLI process (and its window) but issues a NEW session
    id. One OS process = one live session, so another active record carrying
    THIS process's pid is a prior identity of this same session — not a peer,
    and definitely not a nesting parent. Remove it (the old conversation is
    gone; its pending questions can never be answered into it) and hand back
    the identity fields so re-registration keeps name/routing continuity.
    """
    if not paths.ACTIVE.is_dir():
        return None
    inherited = None
    for record_path in paths.ACTIVE.glob("*.json"):
        if record_path.stem == sid:
            continue
        record = state.read_json(record_path)
        if not record or record.get("pid") != cli_pid:
            continue
        old_sid = record.get("claude_sid") or record_path.stem
        _append_spend_drop(record, "rotation")
        record_path.unlink(missing_ok=True)
        from .registry import _drop_questions_for_worker
        _drop_questions_for_worker(old_sid)
        if inherited is None or (record.get("started_at") or 0) > (inherited.get("started_at") or 0):
            inherited = record
    return inherited


def _detect_nested_parent(sid: str, cli_pid: int) -> dict | None:
    """The live registered session this one was launched from, or None.

    A `claude -p` (or interactive claude) started from inside a registered
    session's Bash inherits the orchestrator env (CLAUDE_AGENT et al.) and
    would otherwise self-register as a ghost worker whose Stop hook pings the
    manager every turn. Env markers can't discriminate — the CLI re-stamps
    CLAUDECODE / CLAUDE_CODE_CHILD_SESSION / CLAUDE_CODE_SESSION_ID afresh for
    its own hook children, so a nested session's hooks see exactly what a
    top-level session's hooks see. Detection is structural instead:

      1. another active record's pid is a process-ancestor of this CLI
         (covers foreground and background Bash children, any parent runtime);
      2. fallback: another LIVE record owns the inherited pane id
         (covers detached children whose ancestry chain was severed — the
         pid-alive guard keeps crash-leftover records from false-positiving).

    Best-effort: any failure reads as "not nested" (pre-detection behavior).
    """
    try:
        if not paths.ACTIVE.is_dir():
            return None
        records = []
        for record_path in paths.ACTIVE.glob("*.json"):
            if record_path.stem == sid:
                continue
            record = state.read_json(record_path)
            if record and isinstance(record.get("pid"), int):
                records.append(record)
        if not records:
            return None
        ancestors = _ancestor_pids(cli_pid)
        for record in records:
            if record["pid"] in ancestors and _pid_looks_like_session(record["pid"]):
                return {"sid": record.get("claude_sid"), "name": record.get("name")}
        window_id = get_driver().current_pane_id() or ""
        if window_id:
            for record in records:
                if record["pid"] == cli_pid:
                    # Same process under another sid = /clear rotation, never
                    # nesting — the rotation pre-step owns that case.
                    continue
                if (state.window_id_of(record) == window_id
                        and _pid_alive(record["pid"])
                        and _pid_looks_like_session(record["pid"])):
                    return {"sid": record.get("claude_sid"), "name": record.get("name")}
        return None
    except Exception:
        return None


def _read_stdin_json() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}

def _is_distill_session() -> bool:
    """True for headless `claude -p` distill children spawned by the orchestrator.

    Those must never act as orchestrator sessions: registering one as a manager
    (inherited CLAUDE_AGENT=manager) makes its own SessionEnd re-distill, which
    spawns another `claude -p` — infinite fan-out. _distill_manager_session
    strips the orchestrator env and sets this sentinel; the guard here is
    defense in depth for any future spawn path that forgets to strip.
    """
    return bool(os.environ.get(paths.DISTILL_ENV_SENTINEL))

def _is_orchestrator_session() -> bool:
    if _is_distill_session():
        return False
    return os.environ.get("CLAUDE_AGENT") in ("manager", "worker")

def session_start() -> None:
    if not _is_orchestrator_session():
        return
    data = _read_stdin_json()
    sid = data.get("session_id")
    cwd = data.get("cwd") or os.getcwd()
    if not sid:
        return
    agent = os.environ["CLAUDE_AGENT"]
    iterm_sid = os.environ.get("CLAUDE_ITERM_SID") or get_driver().current_pane_id() or ""
    cli_pid = _resolve_session_pid()
    paths.ensure_dirs()
    existing = state.read_json(paths.ACTIVE / f"{sid}.json")
    if existing is not None and existing.get("name") and existing.get("agent") == agent:
        # SessionStart re-fires on every resume / context compaction, not just at
        # session birth. Re-rolling names here would flip the tab title on each
        # re-fire and silently break routing (turn-ends/done/questions are keyed
        # on the manager's `name`); rebuilding the record would wipe live progress
        # state. Keep the registered identity (including a name assigned via
        # become_manager) and refresh only fields that can change on resume.
        # Nested records keep window_id="" (the inherited pane id is the
        # parent's window) and ignore the inherited runtime env.
        record = existing
        record["cwd"] = cwd
        if iterm_sid and not record.get("nested"):
            record["window_id"] = iterm_sid
        record["pid"] = cli_pid
        if agent == "worker":
            env_runtime = os.environ.get("CLAUDE_WORKER_RUNTIME")
            if env_runtime in ("claude", "codex") and not record.get("nested"):
                record["runtime"] = env_runtime
        elif not record.get("nested"):
            # Managers are Claude-only — pin the runtime regardless of env.
            record["runtime"] = "claude"
        env_account = os.environ.get("CLAUDE_ORCH_ACCOUNT")
        if env_account in config.account_names() and not record.get("nested"):
            record["account"] = env_account
        name = record["name"]
        funny_name = record.get("funny_name")
        domain = record.get("domain")
    elif (rotated := _supersede_rotated_records(sid, cli_pid)) is not None:
        # /clear lane: same process, new sid. Carry the registered identity
        # over (name keeps worker routing AND a manager's event buckets /
        # parent_manager_name pointers intact); reset the per-conversation
        # state. A nested record that /clear'd stays nested.
        record = dict(rotated)
        record.pop("spend", None)
        record.pop("last_turn_at_uptime", None)
        record.pop("transcript_path", None)
        from .registry import _resolve_unique_name
        name = _resolve_unique_name(record.get("name") or f"worker-{sid[:8]}",
                                    excluding_sid=sid)
        record.update({
            "claude_sid": sid,
            "name": name,
            "cwd": cwd,
            "pid": cli_pid,
            "started_at": time.time(),
            "state": "idle",
            "last_turn_at": None,
            "last_summary": None,
        })
        if iterm_sid and not record.get("nested"):
            record["window_id"] = iterm_sid
        funny_name = record.get("funny_name")
        domain = record.get("domain")
    elif (nested_parent := _detect_agent_team_parent(data, cli_pid)
                           or _detect_nested_parent(sid, cli_pid)) is not None:
        # Launched from INSIDE a registered session (`claude -p` child), or an
        # agent-team subagent launched BY one as its own tmux session:
        # register for on-disk visibility, but as an unmistakable
        # `nested-<sid8>` record that no monitor pages on, with no window
        # claim (a `claude -p` child's inherited pane id belongs to the
        # parent; a teammate's pane belongs to Claude Code's team UI —
        # adopting either would let autoclose/kill close a tab this record
        # doesn't own) and no name/funny-name rolls off the inherited env.
        from .registry import _resolve_unique_name
        name = _resolve_unique_name(f"nested-{sid[:8]}", excluding_sid=sid)
        funny_name = None
        domain = None
        record = {
            "claude_sid": sid,
            "agent": agent,
            "name": name,
            "funny_name": None,
            "cwd": cwd,
            "window_id": "",
            "pid": cli_pid,
            "started_at": time.time(),
            "state": "idle",
            "last_turn_at": None,
            "last_summary": None,
            "domain": None,
            "parent_manager_name": os.environ.get("CLAUDE_PARENT_MANAGER") or None,
            "nested": True,
            "nested_parent_sid": nested_parent["sid"],
            "nested_parent_name": nested_parent["name"],
            "agent_id": nested_parent.get("agent_id"),
            # These hooks only run under the claude CLI; an inherited
            # CLAUDE_WORKER_RUNTIME=codex would mislead transcript resolution.
            "runtime": "claude",
        }
    else:
        explicit_name = os.environ.get("CLAUDE_WORKER_NAME")
        if explicit_name:
            base_name = explicit_name
        elif agent == "manager":
            # A manager with no caller-pinned name must get a funny <adjective>-<creature>,
            # never the literal "manager"/"manager-2". /manager (become_manager) and
            # /manager-resume (become_manager_with_takeover) re-roll or inherit a funny
            # name, but a manager-agent session whose SessionStart hook registers it
            # before/without that call would otherwise be stuck as "manager".
            from .names import roll_manager_name
            taken = _existing_display_names(sid)
            base_name = roll_manager_name(lambda candidate: candidate in taken)
        else:
            base_name = f"worker-{sid[:8]}"
        from .registry import _resolve_unique_name
        name = _resolve_unique_name(base_name, excluding_sid=sid)
        parent_manager_name = os.environ.get("CLAUDE_PARENT_MANAGER") or None
        domain = os.environ.get("CLAUDE_DOMAIN") if agent == "manager" else None
        runtime = None
        if agent == "worker":
            runtime = os.environ.get("CLAUDE_WORKER_RUNTIME") or "claude"
            if runtime not in ("claude", "codex"):
                runtime = "claude"
        elif agent == "manager":
            # Managers are Claude-only.
            runtime = "claude"
        # Workers get a cosmetic funny name in addition to their routing `name` (task
        # label). It's display-only — never the routing key. Rolled here in the hook
        # (not the MCP) so it deploys via file copy with no MCP-server restart.
        funny_name = None
        if agent == "worker":
            from .names import roll_worker_name
            taken = _existing_display_names(sid)
            funny_name = roll_worker_name(lambda candidate: candidate in taken)
        env_account = os.environ.get("CLAUDE_ORCH_ACCOUNT")
        record = {
            "claude_sid": sid,
            "agent": agent,
            "name": name,
            "funny_name": funny_name,
            "cwd": cwd,
            "window_id": iterm_sid,
            "pid": cli_pid,
            "started_at": time.time(),
            "state": "idle",
            "last_turn_at": None,
            "last_summary": None,
            "domain": domain,
            "parent_manager_name": parent_manager_name,
            "account": env_account if env_account in config.account_names() else None,
        }
        if agent in ("manager", "worker"):
            record["runtime"] = runtime
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", record)
    # Fresh worker registrations claim their spawn-authored assignment; the
    # existing-record branch (resume re-fire / compaction) must never claim,
    # and a nested child's inherited CLAUDE_ASSIGNMENT_ID is the parent's.
    if agent == "worker" and existing is None and not record.get("nested"):
        _claim_pending_assignment(sid, name)
        _apply_captured_window_id(sid, record)
    if record.get("nested"):
        # No tab of its own — painting would retitle the parent's tab.
        return
    if agent == "manager":
        _style_manager_tab(name=name, domain=domain or "general")
    elif agent == "worker":
        # On resume (e.g. resume_worker brought back an auto-closed worker with a
        # pending question), the tab must paint red — not gray — so the human can
        # see at a glance which workers are waiting on them.
        color = (
            WORKER_TAB_COLOR_QUESTION
            if _has_pending_question_for_worker(sid)
            else WORKER_TAB_COLOR_IDLE
        )
        _style_worker_tab(funny_name=funny_name, task_name=name, color=color)

def user_prompt_submit() -> None:
    if not _is_orchestrator_session():
        return
    data = _read_stdin_json()
    sid = data.get("session_id")
    if not sid:
        return
    active_path = paths.ACTIVE / f"{sid}.json"
    record = state.read_json(active_path)
    if record is not None:
        record["state"] = "processing"
        # Tasking-episode stamp for wait_for_worker's stale-done bound; covers
        # re-tasks typed directly into the worker window (no manager send).
        record["processing_since"] = time.time()
        state.write_json_atomic(active_path, record)
    if os.environ.get("CLAUDE_AGENT") == "worker" and not (record or {}).get("nested"):
        _set_tab_color(WORKER_TAB_COLOR_BUSY)

def stop_hook() -> None:
    if not _is_orchestrator_session():
        return
    data = _read_stdin_json()
    sid = data.get("session_id")
    if not sid:
        return
    active_path = paths.ACTIVE / f"{sid}.json"
    record = state.read_json(active_path)
    if record is None:
        return
    from .transcript import find_session_log, is_delegating, last_assistant_summary
    log = find_session_log(sid, runtime=record.get("runtime") or "claude")
    if log:
        summary, ts = last_assistant_summary(log)
        if summary is not None:
            record["last_summary"] = summary
        if ts is not None:
            record["last_turn_at"] = ts
        _accumulate_record_spend(record, log)
        # Cache the resolved transcript path so shell consumers (statusline)
        # can find the subagents dir without re-deriving the cwd slug.
        record["transcript_path"] = str(log)
    # Paired uptime stamp so stale_monitor's idle-elapsed math survives laptop
    # sleep — wall-clock keeps ticking through sleep, CLOCK_UPTIME_RAW doesn't.
    record["last_turn_at_uptime"] = time.clock_gettime(time.CLOCK_UPTIME_RAW)
    record["state"] = "idle"
    state.write_json_atomic(active_path, record)
    if record.get("nested"):
        # The record stays fresh for list_workers debugging, but a nested
        # sub-session is supervised by its parent process — no turn-end marker
        # (the manager-noise source), no tab to repaint.
        return
    # Dedicated turn-end marker for the manager's monitor — the active/ dir is also
    # touched by session_start and user_prompt_submit, so watching active/ alone would
    # fire on non-turn-end events. Scoped into the owning manager's subdir so only that
    # manager's monitor sees it. A manager has no parent, so its OWN turn-end is keyed
    # on its own name — landing in turn-ends/<manager>/, which only that manager watches
    # (and the monitor's self-sid grep then suppresses the self-ping). Null-parent
    # (legacy) workers still WRITE to _unscoped (write-side contract preserved for
    # audit), but no manager monitor READS it under strict routing — recovery via
    # `_backfill_legacy_workers` on single-manager boot.
    bucket_key = record.get("name") if record.get("agent") == "manager" else record.get("parent_manager_name")
    turn_ends_dir = paths.turn_ends_dir_for(bucket_key)
    turn_ends_dir.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(turn_ends_dir / f"{sid}-{int(time.time() * 1000)}.json", {
        "sid": sid,
        "agent": record.get("agent"),
        "name": record.get("name"),
        "last_summary": record.get("last_summary"),
        "last_turn_at": record.get("last_turn_at"),
        "runtime": record.get("runtime") or "claude",
        "completed_at": time.time(),
    })
    if record.get("agent") == "worker":
        if _has_pending_question_for_worker(sid):
            _set_tab_color(WORKER_TAB_COLOR_QUESTION)
        elif log and is_delegating(record, time.time(), log=log):
            # Turn ended but a background subagent is still writing — the
            # worker reads as working. State stays "idle" (turn-truth);
            # the completion notification's next turn repaints either way.
            # Best-effort paint: it catches the case where a subagent write
            # landed after the main log's final flush; a subagent mid-LLM-call
            # at Stop loses the race and the tab stays grey until the
            # statusline/list_workers lanes (which re-evaluate continuously)
            # show the truth.
            _set_tab_color(WORKER_TAB_COLOR_BUSY)
        else:
            _set_tab_color(WORKER_TAB_COLOR_IDLE)

def _accumulate_record_spend(record: dict, log) -> None:
    """Fold the just-ended turn's token usage into record["spend"].

    Observability only — runs inside the Stop hook on every turn, so it must
    stay cheap (64KB tail read, never the full transcript) and must NEVER
    raise past this function: malformed/missing usage degrades to a no-op.
    Claude transcript shape only; codex rollouts carry no message.usage.
    """
    if (record.get("runtime") or "claude") != "claude":
        return
    try:
        from .transcript import accumulate_spend, tail_usage_entries
        spend = accumulate_spend(record.get("spend"), tail_usage_entries(log))
        if spend is not None:
            record["spend"] = spend
    except Exception:
        pass


def _append_spend_drop(record, source: str) -> None:
    """Best-effort spend archive at record-drop time; never blocks the drop."""
    try:
        from .spend_ledger import append_drop_event
        append_drop_event(record, source)
    except Exception:
        pass


def _capture_tagged_headless_spend() -> None:
    """CLAUDE_SPEND_CLASS contract: an env-stripped headless `claude -p` (distill
    here; other bounded headless runs once their code exports the var) tags itself, and
    its SessionEnd lands whole-transcript spend in the ledger — the only capture
    these sessions get (no active record, no Stop-hook accumulation). Never
    raises: teardown proceeds regardless. Callers must tag only bounded
    single-shot runs; a long multi-turn tagged session risks the SessionEnd
    hook's 5s budget on the full-transcript read."""
    try:
        spend_class = os.environ.get("CLAUDE_SPEND_CLASS")
        if not spend_class:
            return
        data = _read_stdin_json()
        from .spend_ledger import append_headless_event
        append_headless_event(spend_class, data.get("session_id"), data.get("transcript_path"))
    except Exception:
        pass


def _notify_macos(message: str) -> None:
    """Best-effort local notification. The 2s timeout respects the 5s SessionEnd
    hook budget; every failure (no osascript, sandbox, timeout) is swallowed.

    No-ops under pytest (PYTEST_CURRENT_TEST, inherited by child processes):
    a test exec'ing the orchestrator CLI must never fire a real desktop
    notification (the 2026-07-03 gardener-gate leak class)."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        sanitized = message.replace('"', "")
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{sanitized}" with title "dockwright"'],
            capture_output=True, timeout=2, check=False,
        )
    except Exception:
        pass


def _live_workers_of(manager_name: str) -> list:
    """Active worker records parented to `manager_name` whose pid is alive.

    Per-record defensive: a malformed record (non-int pid, corrupt JSON) is
    skipped, never aborts the scan — losing a real orphan event to one bad
    record would defeat the watchdog."""
    workers = []
    for record_path in paths.ACTIVE.glob("*.json"):
        record = state.read_json(record_path)
        if not isinstance(record, dict) or record.get("agent") != "worker":
            continue
        # Nested sub-sessions inherit CLAUDE_PARENT_MANAGER, so they'd read as
        # the manager's workers — but they're excluded from all lifecycle
        # surfaces (#62) and die with their parent process anyway.
        if record.get("nested"):
            continue
        if record.get("parent_manager_name") != manager_name:
            continue
        pid = record.get("pid")
        if not isinstance(pid, int) or not state._pid_alive(pid):
            continue
        workers.append(record)
    return workers


def _flag_orphaned_workers(sid: str, record: dict, reason) -> None:
    """Boot-lite event half: a manager ending while workers it parents are still
    alive leaves them unsupervised (stale monitor + autonudge die with the
    manager). Durably flag the orphan event + notify the human.

    Clean closes are silent by construction: /manager-close and takeover unlink
    the active record before(/around) the tab close, so session_end reads
    record=None and never reaches this. The takeover path closes the tab first
    and unlinks microseconds later — a SessionEnd that wins that race writes a
    spurious flag; accepted, the watchdog's healthy-sweep unlinks it on the
    next tick once the inheriting manager is live under the same name.

    Best-effort throughout: nothing here may break session_end."""
    try:
        manager_name = record.get("name")
        if not manager_name:
            return
        workers = _live_workers_of(manager_name)
        if not workers:
            return
        state.write_json_atomic(paths.orphan_flag_path(manager_name), {
            "manager_name": manager_name,
            "manager_sid": sid,
            "orphaned_at": time.time(),
            "source": "session_end",
            "reason": reason,
            "workers": [{
                "claude_sid": w.get("claude_sid"),
                "name": w.get("name"),
                "funny_name": w.get("funny_name"),
                "pid": w.get("pid"),
                "window_id": state.window_id_of(w),
                "state": w.get("state"),
            } for w in workers],
        })
        _notify_macos(
            f"manager {manager_name} ended with {len(workers)} live worker(s) — "
            "resume or start a manager to adopt them"
        )
    except Exception:
        return


def session_end() -> None:
    if not _is_orchestrator_session():
        _capture_tagged_headless_spend()
        return
    data = _read_stdin_json()
    sid = data.get("session_id")
    if not sid:
        return
    active_path = paths.ACTIVE / f"{sid}.json"
    record = state.read_json(active_path)
    # Boot-lite event half — must land before the unlink below and before the
    # distill (which can be SIGKILLed mid-flight at tab-close timeout). Nested
    # manager-ghosts never flag (consistent with their lifecycle exclusion).
    if record is not None and record.get("agent") == "manager" and not record.get("nested"):
        _flag_orphaned_workers(sid, record, data.get("reason"))
    # Archive worker records to closed/ so resume_worker can bring them back.
    # Managers don't get resumed via resume_worker, so don't archive them.
    # Nested sub-sessions aren't resumable either — archiving them would only
    # clutter list_closed_workers.
    if record is not None and record.get("agent") == "worker" and not record.get("nested"):
        state.write_json_atomic(paths.CLOSED / f"{sid}.json", {
            "claude_sid": sid,
            "name": record.get("name") or "",
            "cwd": record.get("cwd"),
            "window_id": state.window_id_of(record),
            "last_summary": record.get("last_summary"),
            "last_turn_at": record.get("last_turn_at"),
            "spend": record.get("spend"),
            "started_at": record.get("started_at"),
            "closed_at": time.time(),
            "closed_reason": "session_end",
            "parent_manager_name": record.get("parent_manager_name"),
            "runtime": record.get("runtime") or "claude",
            "account": record.get("account"),
        })
    # Archive ANY spend before the record drops — managers and nested records
    # get no closed/ archive, and even the worker archive above is pruned at 7d.
    if record is not None:
        _append_spend_drop(record, "session_end")
    active_path.unlink(missing_ok=True)
    # Drop any pending questions for this worker — they'd otherwise orphan
    from .registry import _drop_questions_for_worker
    _drop_questions_for_worker(sid)
    # Manager-mode fallback: if SessionEnd fires for a manager and no memory file
    # exists yet for this session, spawn a detached distill so the cmd+w close
    # path also gets a memory entry. Best-effort: any failure to spawn is
    # swallowed here; failures inside the detached child are logged to
    # distill-fallback.log by _distill_manager_session, unobserved by us.
    # The _is_distill_session check is redundant with the top-of-function
    # _is_orchestrator_session guard, but kept explicit: a distill child must
    # never reach _maybe_distill_on_session_end even if that guard is refactored.
    # `not nested`: a nested child of a manager inherits CLAUDE_AGENT=manager;
    # letting it distill would spawn another `claude -p`, which registers
    # nested, whose SessionEnd would distill again — the same fan-out the
    # distill sentinel guards against, one layer further out.
    if (
        os.environ.get("CLAUDE_AGENT") == "manager"
        and not _is_distill_session()
        and record is not None
        and not record.get("nested")
    ):
        try:
            _maybe_distill_on_session_end(sid, record)
        except Exception:
            pass


def _maybe_distill_on_session_end(sid: str, record: dict) -> None:
    """Spawn a DETACHED fallback distill at SessionEnd if no memory file exists.

    Catches the cmd+w-without-/manager-close path. The SessionEnd hook budget
    is 5s (settings.snippet.json) while the distill's `claude -p` round-trip is
    10-30s, so it can never finish in-process. start_new_session=True (setsid)
    detaches the child from the hook's process group and controlling tty, so it
    survives both the harness killing the hook at its timeout and the
    terminal's SIGHUP on tab close, regardless of how the harness handles hook
    process groups. `--domain` is passed explicitly because the active record
    is already unlinked by this point in session_end. The `claude -p`
    grandchild's env-strip + distill sentinel live in _distill_manager_session,
    unchanged. Idempotent: if /manager-close already wrote the file, skip.
    """
    domain = record.get("domain") or paths.DEFAULT_DOMAIN
    from datetime import datetime as _dt
    date_str = _dt.now().strftime("%Y-%m-%d")
    expected = paths.manager_memory_domain_dir(domain) / f"{date_str}-{sid}.md"
    if expected.exists():
        return
    log_path = paths.ROOT / "distill-fallback.log"
    with open(log_path, "ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "dockwright", "distill", sid, "--domain", domain],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
