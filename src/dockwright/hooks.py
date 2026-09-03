import json
import os
import subprocess
import sys
import time
from . import config, paths, state
from .state import _pid_alive
from .terminal import get_driver


def _awake_seconds() -> float:
    clk = getattr(time, "CLOCK_UPTIME_RAW", None)
    if clk is not None:
        return time.clock_gettime(clk)
    return time.monotonic()


MANAGER_TAB_COLOR = ("#aa0066", "#440022")
WORKER_TAB_COLOR_IDLE = ("#444444", "#222222")
WORKER_TAB_COLOR_BUSY = ("#aa8800", "#443300")
WORKER_TAB_COLOR_QUESTION = ("#aa3300", "#441100")


def _set_tab_color(color: tuple) -> None:
    get_driver().set_tab_color(*color)


def _set_tab_title(title: str) -> None:
    get_driver().set_tab_title(title)


def _style_manager_tab(name: str = "manager", domain: str = "general") -> None:
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
    assignment_id = os.environ.get("CLAUDE_ASSIGNMENT_ID")
    if not assignment_id:
        return
    try:
        target = paths.assignment_path(sid)
        if target.exists():
            return
        pending = paths.pending_assignment_path(assignment_id)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(pending, target)
        except FileNotFoundError:
            return
        record = state.read_json(target) or {}
        record.update({"claude_sid": sid, "name": registered_name, "claimed_at": time.time()})
        state.write_json_atomic(target, record)
    except Exception:
        return


def _apply_captured_window_id(sid: str, record: dict) -> None:
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
    return list(_iter_ancestors(start_pid, max_hops))


def _ancestor_pids(start_pid: int, max_hops: int = 15) -> set:
    return set(_ancestor_chain(start_pid, max_hops))


def _pid_looks_like_session(pid: int) -> bool:
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
    try:
        if sys.platform != "darwin":
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read()
            argv = [a.decode("utf-8", "replace") for a in raw.split(b"\x00") if a]
            return argv or None
        import ctypes
        import struct
        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, pid)
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
    return bool(os.environ.get(paths.DISTILL_ENV_SENTINEL))

def _is_orchestrator_session() -> bool:
    if _is_distill_session():
        return False
    return os.environ.get("CLAUDE_AGENT") in ("manager", "worker")

def _record_is_orchestrator(record) -> bool:
    return isinstance(record, dict) and record.get("agent") in ("manager", "worker")

def _emit_session_context(sid: str, agent: str) -> None:
    if os.environ.get("CLAUDE_WORKER_RUNTIME") == "codex":
        return
    if agent == "worker":
        line = (f"dockwright: your claude_sid is {sid} — pass it as claude_sid "
                "to ask_manager / worker_done / artifact_put.")
    else:
        line = (f"dockwright: your session id is {sid} — pass it as manager_sid "
                "to spawn_worker / list_workers / list_pending_questions.")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": line,
    }}))

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
            record["runtime"] = "claude"
        env_account = os.environ.get("CLAUDE_ORCH_ACCOUNT")
        if env_account in config.account_names() and not record.get("nested"):
            record["account"] = env_account
        name = record["name"]
        funny_name = record.get("funny_name")
        domain = record.get("domain")
    elif (rotated := _supersede_rotated_records(sid, cli_pid)) is not None:
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
            "runtime": "claude",
        }
    else:
        if agent == "manager" and os.environ.get("DOCKWRIGHT_PENDING_TAKEOVER") == "1":
            _emit_session_context(sid, agent)
            return
        explicit_name = os.environ.get("CLAUDE_WORKER_NAME")
        if explicit_name:
            base_name = explicit_name
        elif agent == "manager":
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
            runtime = "claude"
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
    if agent == "worker" and existing is None and not record.get("nested"):
        _claim_pending_assignment(sid, name)
        _apply_captured_window_id(sid, record)
    _emit_session_context(sid, agent)
    if record.get("nested"):
        return
    if agent == "manager":
        _style_manager_tab(name=name, domain=domain or "general")
    elif agent == "worker":
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
        record["transcript_path"] = str(log)
    record["last_turn_at_uptime"] = _awake_seconds()
    record["state"] = "idle"
    state.write_json_atomic(active_path, record)
    if record.get("nested"):
        return
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
            _set_tab_color(WORKER_TAB_COLOR_BUSY)
        else:
            _set_tab_color(WORKER_TAB_COLOR_IDLE)

def _accumulate_record_spend(record: dict, log) -> None:
    if (record.get("runtime") or "claude") != "claude":
        return
    try:
        from .transcript import recount_spend
        spend = recount_spend(log, record.get("spend"), record.get("started_at"))
        if spend is not None:
            record["spend"] = spend
    except Exception:
        pass


def _append_spend_drop(record, source: str) -> None:
    try:
        from .spend_ledger import append_drop_event
        append_drop_event(record, source)
    except Exception:
        pass


def _capture_tagged_headless_spend(data: dict) -> None:
    try:
        spend_class = os.environ.get("CLAUDE_SPEND_CLASS")
        if not spend_class:
            return
        from .spend_ledger import append_headless_event
        append_headless_event(spend_class, data.get("session_id"), data.get("transcript_path"))
    except Exception:
        pass


def _notify_macos(message: str) -> None:
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
    workers = []
    for record_path in paths.ACTIVE.glob("*.json"):
        record = state.read_json(record_path)
        if not isinstance(record, dict) or record.get("agent") != "worker":
            continue
        if record.get("nested"):
            continue
        if record.get("parent_manager_name") != manager_name:
            continue
        pid = record.get("pid")
        if not isinstance(pid, int) or not state._pid_alive(pid):
            continue
        workers.append(record)
    return workers


HANDOFF_SUPPRESS_SEC = 1800


def _handoff_prepared_recently(sid: str) -> bool:
    try:
        cutoff = time.time() - HANDOFF_SUPPRESS_SEC
        if not paths.HANDOFFS.is_dir():
            return False
        for handoff_path in paths.HANDOFFS.glob("*.json"):
            try:
                if handoff_path.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            record = state.read_json(handoff_path)
            if not isinstance(record, dict) or record.get("from_sid") != sid:
                continue
            prepared_at = record.get("prepared_at")
            if isinstance(prepared_at, (int, float)) and prepared_at >= cutoff:
                return True
        return False
    except Exception:
        return False


def _flag_orphaned_workers(sid: str, record: dict, reason) -> None:
    try:
        manager_name = record.get("name")
        if not manager_name:
            return
        workers = _live_workers_of(manager_name)
        if not workers:
            return
        if _handoff_prepared_recently(sid):
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
    data = _read_stdin_json()
    sid = data.get("session_id")
    record = state.read_json(paths.ACTIVE / f"{sid}.json") if sid else None
    if _is_distill_session() or not (
        _is_orchestrator_session() or _record_is_orchestrator(record)
    ):
        _capture_tagged_headless_spend(data)
        return
    if not sid:
        return
    active_path = paths.ACTIVE / f"{sid}.json"
    if record is not None and record.get("agent") == "manager" and not record.get("nested"):
        _flag_orphaned_workers(sid, record, data.get("reason"))
    if record is not None and record.get("agent") == "worker" and not record.get("nested"):
        transcript_path = record.get("transcript_path")
        if not transcript_path:
            try:
                from .transcript import find_session_log
                log = find_session_log(sid, runtime=record.get("runtime") or "claude")
                transcript_path = str(log) if log else None
            except Exception:
                transcript_path = None
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
            "transcript_path": transcript_path,
        })
    if record is not None:
        _append_spend_drop(record, "session_end")
    active_path.unlink(missing_ok=True)
    from .registry import _drop_questions_for_worker
    _drop_questions_for_worker(sid)
    if (
        record is not None
        and record.get("agent") == "manager"
        and not _is_distill_session()
        and not record.get("nested")
    ):
        try:
            _maybe_distill_on_session_end(sid, record)
        except Exception:
            pass


def _maybe_distill_on_session_end(sid: str, record: dict) -> None:
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
