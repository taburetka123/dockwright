import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from . import config, notify, paths, state, stale_monitor
from .state import _pid_alive
from .terminal import get_driver
from .session_pid import SESSION_BINARIES, _iter_ancestors, _pid_looks_like_session, resolve_cli_pid
from .profiles import profile_for
from .permission_policy import symlink_tool_results_hint

ROLE_MARK_PREFIX = "<!-- dockwright-role:"
SUBAGENTS_WITHOUT_CLAUDE_MD = ("Explore", "Plan")
TRIPWIRE_TEXT = ("dockwright: this session carries no role render — the governance rules did not load. "
                 "Start it through your role wrapper, or pass "
                 "`dockwright role-args <Role> --launch <launch> --cwd \"$PWD\"`. Say so in your first reply.")


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


def _ancestor_chain(start_pid: int, max_hops: int = 15) -> list:
    return list(_iter_ancestors(start_pid, max_hops))


def _ancestor_pids(start_pid: int, max_hops: int = 15) -> set:
    return set(_ancestor_chain(start_pid, max_hops))


def _resolve_session_pid() -> int:
    raw = os.environ.get("CLAUDE_PARENT_PID", "")
    captured = int(raw) if raw.isdigit() else os.getppid()
    return resolve_cli_pid() or captured


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

def permission_request() -> None:
    try:
        data = _read_stdin_json()
        sid = data.get("session_id")
        if not _is_orchestrator_session() or not sid:
            return
        record = state.read_json(paths.ACTIVE / f"{sid}.json")
        at = time.time()
        tool_name = str(data.get("tool_name") or "")
        tool_input = data.get("tool_input")
        marker = {
            "at": at, "sid": sid, "name": record.get("name") if isinstance(record, dict) else None,
            "tool_name": tool_name, "summary": stale_monitor.permission_summary(tool_input),
            "transcript_path": data.get("transcript_path"), "cwd": data.get("cwd"),
            "decided": None, "attempts": [], "tool_use_id": None, "closed_at": None,
        }
        marker_path = paths.PERMISSION_PROMPTS / f"{sid}.json"
        hint = None
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
        if tool_name == "Bash" and isinstance(tool_input, dict):
            hint = symlink_tool_results_hint(str(tool_input.get("command") or ""), config_dir)
        if hint:
            marker["decided"] = {"by": "hook", "answer": "deny-hint", "at": at}
        with stale_monitor.marker_lock(paths.PERMISSION_PROMPTS, sid):
            state.write_json_atomic(marker_path, marker)
        if not hint:
            return
        stale_monitor._append_permission_ledger({
            "at": at, "sid": sid, "name": marker["name"], "tool_name": tool_name,
            "summary": marker["summary"], "answer": "deny-hint", "by": "hook", "hint": hint,
        }, paths.PERMISSION_LEDGER)
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {
            "behavior": "deny",
            "message": (f"{config_dir}/projects is a symlink into ~/.claude/projects; Claude Code asks for "
                        f"permission on such a path. Re-run the same command with the real path: {hint}"),
        }}}))
    except Exception:
        return


def _sent_dict(resp) -> dict | None:
    if isinstance(resp, str):
        d = json.loads(resp)
    elif isinstance(resp, list):
        block = next((b for b in resp if isinstance(b, dict) and b.get("type") == "text"), None)
        d = json.loads(block["text"]) if block else None
    else:
        d = resp
    return d if isinstance(d, dict) else None


def _sent_fields(resp, tool_input: dict) -> tuple[str, str, str] | None:
    from .event_sources.chat import TS_SHAPE
    d = _sent_dict(resp)
    if d is None:
        return None
    ctx = d.get("message_context") if isinstance(d.get("message_context"), dict) else {}
    ts = ctx.get("message_ts") or d.get("message_ts") or d.get("ts")
    channel = ctx.get("channel_id") or d.get("channel_id") or d.get("channel")
    if not (isinstance(ts, str) and isinstance(channel, str) and channel and TS_SHAPE.fullmatch(ts)):
        return None
    thread = tool_input.get("thread_ts")
    return channel, ts, thread if isinstance(thread, str) else ""


def _sender_domain(record: dict) -> str:
    if record.get("agent") == "manager":
        return record.get("domain") or paths.DEFAULT_DOMAIN
    parent = state.parent_name_of(record)
    for other in state.list_json_in(paths.ACTIVE):
        if (isinstance(other, dict) and other.get("agent") == "manager" and not other.get("nested")
                and other.get("name") == parent):
            return other.get("domain") or paths.DEFAULT_DOMAIN
    return paths.DEFAULT_DOMAIN


def post_tool_use() -> None:
    try:
        from . import events
        data = _read_stdin_json()
        sid = data.get("session_id")
        if not _is_orchestrator_session() or not sid:
            return
        tool = str(data.get("tool_name") or "")
        if tool not in config.events_sent()["tools"]:
            return
        record = state.read_json(paths.ACTIVE / f"{sid}.json")
        if not _record_is_orchestrator(record):
            return
        name = str(record.get("name") or "")
        tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
        resp = data.get("tool_response")
        try:
            fields = _sent_fields(resp, tool_input)
        except (ValueError, TypeError, KeyError):
            fields = None
        if fields is None:
            now = time.time()
            events.append(events.Event(f"sent:unparsed:{data.get('tool_use_id') or ''}", now, "sent", "unparsed",
                                       {"sid": sid, "name": name, "tool": tool}, str(now),
                                       f"{tool}: {str(resp)[:200]}"))
            return
        channel, ts, thread = fields
        text = str(tool_input.get("message") or "")
        link = str((_sent_dict(resp) or {}).get("message_link") or "")
        events.append(events.Event(
            f"sent:{channel}:{ts}", float(ts), "sent", "chat",
            {"channel": channel, "thread": thread, "sid": sid, "name": name, "agent": record["agent"],
             "domain": _sender_domain(record), "tool": tool, "pid": str(record.get("pid") or "")},
            ts, f"{name} → {channel}: {text[:80]}",
            {"to": str(tool_input.get("channel_id") or ""), "link": link, "text": text[:200], "thread_ts": thread}))
    except Exception:
        return


def _is_distill_session() -> bool:
    return bool(os.environ.get(paths.DISTILL_ENV_SENTINEL))

def _is_orchestrator_session() -> bool:
    if _is_distill_session():
        return False
    return os.environ.get("CLAUDE_AGENT") in ("manager", "worker")

def _record_is_orchestrator(record) -> bool:
    return isinstance(record, dict) and record.get("agent") in ("manager", "worker")

def _render_in_argv(argv: list) -> bool:
    for i, arg in enumerate(argv):
        if arg == "--append-system-prompt-file" and i + 1 < len(argv):
            path = argv[i + 1]
        elif isinstance(arg, str) and arg.startswith("--append-system-prompt-file="):
            path = arg.split("=", 1)[1]
        else:
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.readline().startswith(ROLE_MARK_PREFIX)
        except OSError:
            return False
    return False


def _tripwire_context() -> str | None:
    try:
        if os.environ.get("CLAUDE_WORKER_RUNTIME") == "codex":
            return None
        if not (config.claude_config_home() / "rules" / "no-role.md").is_file():
            return None
        argv = _proc_argv(_resolve_session_pid()) or []
        if not argv or not os.path.basename(str(argv[0])).endswith("claude"):
            return None
        if _render_in_argv(argv):
            return None
        return TRIPWIRE_TEXT
    except Exception:
        return None


def _print_context(event: str, text: str | None) -> None:
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))


def subagent_start() -> None:
    data = _read_stdin_json()
    agent_type = str(data.get("agent_type") or "")
    if not agent_type or agent_type in SUBAGENTS_WITHOUT_CLAUDE_MD or agent_type in config.subagents():
        return
    default_role = config.subagent_default_role()
    if default_role is None:
        return
    profile = profile_for(data.get("cwd") or os.getcwd())
    suffix = "" if profile == "none" else f".{profile}"
    bundle = paths.ROLES_DIR / default_role / f"role.agent{suffix}.md"
    _print_context("SubagentStart", (
        f"Subagent type `{agent_type}` carries no dockwright role render; the rules did not load into your "
        f"prompt. Session profile: `{profile}`. The role bundle for it: `{bundle}` — Read it before you "
        "write or review code."))


def _emit_session_context(sid: str, agent: str, kind: str | None = None, extra: str | None = None) -> None:
    if os.environ.get("CLAUDE_WORKER_RUNTIME") == "codex":
        return
    if kind == "subworker":
        line = (f"dockwright: your child_sid is {sid} — pass it as child_sid "
                "to ask_parent / child_done, and as writer_sid to artifact_put.")
    elif agent == "worker":
        line = (f"dockwright: your claude_sid is {sid} — pass it as claude_sid "
                "to ask_manager / worker_done, and as writer_sid to artifact_put.")
    else:
        line = (f"dockwright: your session id is {sid} — pass it as manager_sid "
                "to spawn_worker / list_workers / list_pending_questions.")
    if extra:
        line = f"{line}\n{extra}"
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": line,
    }}))

def _parent_record(parent_sid):
    if not parent_sid:
        return None
    return state.read_json(paths.ACTIVE / f"{parent_sid}.json")


def _backfill_tree_fields(record: dict, agent: str) -> None:
    agent_of = record.get("agent") or agent
    record.setdefault("kind", agent_of)
    if "parent_name" not in record:
        record["parent_name"] = record.get("parent_manager_name")
    if record.get("nested"):
        record["parent_sid"] = None
        record["depth"] = None
        return
    if agent_of == "worker":
        record.setdefault("provider", state.default_provider_for(record.get("runtime")))
    if "parent_sid" not in record:
        parent_sid = None
        parent_name = state.parent_name_of(record)
        if parent_name is not None and (os.environ.get("CLAUDE_PARENT_MANAGER") or None) == parent_name:
            parent_sid = os.environ.get("CLAUDE_PARENT_SID") or None
        record["parent_sid"] = parent_sid
    if "depth" not in record:
        if agent_of == "manager":
            record["depth"] = 0
        else:
            record["depth"] = state.child_depth(_parent_record(record.get("parent_sid")), agent_of)


def _env_worker_kind(agent: str) -> str | None:
    if agent != "worker":
        return None
    kind = os.environ.get("CLAUDE_WORKER_KIND")
    return kind if kind in ("worker", "subworker") else None


def _env_worker_provider(agent: str) -> str | None:
    if agent != "worker":
        return None
    from .providers import known_provider_names
    provider = os.environ.get("CLAUDE_WORKER_PROVIDER")
    return provider if provider in known_provider_names() else None


def session_start() -> None:
    if not _is_orchestrator_session():
        _print_context("SessionStart", _tripwire_context())
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
    env_kind = _env_worker_kind(agent)
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
        try:
            state.migrate_assignment(rotated.get("claude_sid"), sid)
        except Exception:
            pass
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
            **state.tree_fields(agent, parent_name=os.environ.get("CLAUDE_PARENT_MANAGER") or None,
                                nested=True),
            "nested": True,
            "nested_parent_sid": nested_parent["sid"],
            "nested_parent_name": nested_parent["name"],
            "agent_id": nested_parent.get("agent_id"),
            "runtime": "claude",
            "provider": None,
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
        parent_sid = os.environ.get("CLAUDE_PARENT_SID") or None
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
            **state.tree_fields(agent, kind=env_kind, parent_name=parent_manager_name,
                                parent_sid=parent_sid, parent_record=_parent_record(parent_sid)),
            "account": env_account if env_account in config.account_names() else None,
        }
        if agent in ("manager", "worker"):
            record["runtime"] = runtime
        if agent == "worker":
            record["provider"] = _env_worker_provider(agent) or state.default_provider_for(runtime)
    _backfill_tree_fields(record, agent)
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", record)
    if agent == "worker" and existing is None and not record.get("nested"):
        _claim_pending_assignment(sid, name)
        _apply_captured_window_id(sid, record)
    _emit_session_context(sid, agent, kind=record.get("kind"), extra=_tripwire_context())
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

AUTOMATION_MARKERS = ("[MANAGER", "[bootlite watchdog]", "[TOPIC")
HARNESS_OPENINGS = (
    "[SYSTEM NOTIFICATION",
    "Your claude.ai usage limit has reset.",
    "Your claude.ai usage is available again",
    "# Autonomous loop",
    "# /loop tick",
    "The user approved the ultraplan",
    "The following one-shot scheduled task",
)
SCHEDULER_TOOLS = ("ScheduleWakeup", "CronCreate")
_PASTE_OPEN_RE = re.compile(r"\A\s*<pasted_content[^>]*>\s*")
_ENVELOPE_OPEN_RE = re.compile(r"\A\s*<(?!pasted_content[\s>])[A-Za-z][\w-]*[\s>]")


def _unwrapped(prompt: str) -> str:
    return _PASTE_OPEN_RE.sub("", prompt)


def _looks_automated(prompt: str) -> bool:
    return _unwrapped(prompt).lstrip().startswith(AUTOMATION_MARKERS)


def _scheduled_by_session(transcript_path, prompt: str) -> bool:
    text = prompt.strip()
    if not text or not isinstance(transcript_path, str):
        return False
    try:
        raw = Path(transcript_path).read_bytes()
    except OSError:
        return False
    names = [tool.encode() for tool in SCHEDULER_TOOLS]
    if not any(name in raw for name in names):
        return False
    for line in raw.splitlines():
        if not any(name in line for name in names):
            continue
        try:
            content = json.loads(line)["message"]["content"]
        except (ValueError, KeyError, TypeError):
            continue
        for block in content if isinstance(content, list) else ():
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_input = block.get("input")
            scheduled = tool_input.get("prompt") if isinstance(tool_input, dict) else None
            if (block.get("name") in SCHEDULER_TOOLS and isinstance(scheduled, str)
                    and scheduled.strip() == text):
                return True
    return False


def _harness_generated(data: dict, prompt: str) -> bool:
    source = data.get("source")
    return ((isinstance(source, str) and source != "user")
            or _ENVELOPE_OPEN_RE.match(prompt) is not None
            or prompt.lstrip().startswith(HARNESS_OPENINGS)
            or _scheduled_by_session(data.get("transcript_path"), prompt))


def _consume_pane_send(sid: str, now: float) -> bool:
    path = paths.PANE_SENDS / f"{sid}.json"
    try:
        with stale_monitor.pane_send_lock(paths.PANE_SENDS, sid):
            data = state.read_json(path)
            pending = data.get("pending") if isinstance(data, dict) else None
            if not isinstance(pending, list):
                if path.exists():
                    state.write_json_atomic(path, {"pending": []})
                return False
            live = sorted(s for s in pending
                          if isinstance(s, (int, float)) and not isinstance(s, bool)
                          and now - s < stale_monitor.AUTOMATION_STAMP_TTL_SEC)
            state.write_json_atomic(path, {"pending": live[1:]})
            return bool(live)
    except Exception:
        return False


def _clear_pane_sends(sid: str) -> None:
    try:
        with stale_monitor.pane_send_lock(paths.PANE_SENDS, sid):
            state.write_json_atomic(paths.PANE_SENDS / f"{sid}.json", {"pending": []})
    except Exception:
        pass


def _handback(record: dict, active_path, now: float) -> None:
    _clear_pane_sends(record.get("claude_sid") or "")
    record["human_attached_at"] = None
    state.write_json_atomic(active_path, record)
    print(json.dumps({"decision": "block", "reason": (
        "HANDBACK — this worker is back under manager automation: nudges, idle pages "
        "and the idle autoclose resume.")}))


def user_prompt_submit() -> None:
    if not _is_orchestrator_session():
        return
    data = _read_stdin_json()
    sid = data.get("session_id")
    if not sid:
        return
    active_path = paths.ACTIVE / f"{sid}.json"
    record = state.read_json(active_path)
    prompt = data.get("prompt")
    prompt = prompt if isinstance(prompt, str) else ""
    now = time.time()
    is_worker = (record is not None and record.get("agent") == "worker"
                 and not record.get("nested")
                 and record.get("kind") != "subworker")
    if is_worker and _unwrapped(prompt).strip().lower() == stale_monitor.HANDBACK_TOKEN:
        _handback(record, active_path, now)
        return
    if is_worker:
        seen = record.get("prompts_seen")
        seen = seen if isinstance(seen, int) and not isinstance(seen, bool) and seen > 0 else 0
        record["prompts_seen"] = seen + 1
        if not _harness_generated(data, prompt):
            record["input_at"] = now
            consumed = _consume_pane_send(sid, now)
            lived = (seen >= 1
                     or isinstance(record.get("last_turn_at"), str)
                     or isinstance(record.get("tasked_at"), (int, float)))
            if not consumed and lived and not _looks_automated(prompt):
                record["human_attached_at"] = now
    if record is not None:
        record["state"] = "processing"
        record["processing_since"] = now
        state.write_json_atomic(active_path, record)
    if os.environ.get("CLAUDE_AGENT") == "worker" and not (record or {}).get("nested"):
        _set_tab_color(WORKER_TAB_COLOR_BUSY)

def _assignment_ticket(sid: str) -> str | None:
    try:
        assignment = state.read_json(paths.assignment_path(sid))
    except ValueError:
        return None
    ticket = assignment.get("ticket") if isinstance(assignment, dict) else None
    return ticket if isinstance(ticket, str) and ticket else None


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
        record["transcript_path"] = str(log)
    _accumulate_record_spend(record, log)
    record["last_turn_at_uptime"] = _awake_seconds()
    record["state"] = "idle"
    state.write_json_atomic(active_path, record)
    if record.get("nested"):
        return
    bucket_key = record.get("name") if record.get("agent") == "manager" else state.parent_name_of(record)
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
        "task_key": _assignment_ticket(sid),
    })
    if record.get("agent") == "worker":
        if _has_pending_question_for_worker(sid):
            _set_tab_color(WORKER_TAB_COLOR_QUESTION)
        elif is_delegating(record, time.time(), log=log):
            _set_tab_color(WORKER_TAB_COLOR_BUSY)
        else:
            _set_tab_color(WORKER_TAB_COLOR_IDLE)

def _codex_round_log(record: dict):
    sid = record.get("claude_sid")
    if not sid:
        return None
    assignment = state.read_json(paths.assignment_path(sid)) or {}
    log_path = assignment.get("log_path")
    if not isinstance(log_path, str) or not log_path:
        return None
    return Path(log_path)


def _accumulate_record_spend(record: dict, log) -> None:
    try:
        runtime = record.get("runtime") or "claude"
        if runtime == "codex":
            round_log = _codex_round_log(record)
            if round_log is None:
                return
            from .transcript import recount_codex_spend
            spend = recount_codex_spend(round_log, record.get("spend"))
        elif runtime == "claude" and log is not None:
            from .transcript import recount_spend
            spend = recount_spend(log, record.get("spend"), record.get("started_at"))
        else:
            return
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
    notify.macos("dockwright", message)


def _live_workers_of(manager_name: str) -> list:
    workers = []
    for record_path in paths.ACTIVE.glob("*.json"):
        record = state.read_json(record_path)
        if not isinstance(record, dict) or record.get("agent") != "worker":
            continue
        if record.get("nested"):
            continue
        if state.parent_name_of(record) != manager_name:
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


BROWSER_PROFILE_FLAG = "--user-data-dir="
NEVER_REAPED_BINARIES = SESSION_BINARIES + ("tmux",)
PS_LIST_TIMEOUT_SEC = 2


def _process_listing() -> list[tuple[int, str]]:
    try:
        result = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,args="], capture_output=True, text=True,
                                errors="replace", timeout=PS_LIST_TIMEOUT_SEC, check=False)
        if result.returncode != 0:
            return []
        listing = []
        for line in result.stdout.splitlines():
            head, _, args = line.strip().partition(" ")
            if head.isdigit():
                listing.append((int(head), args.strip()))
        return listing
    except Exception:
        return []


def _owns_browser_profile(argv: list[str], sid: str, uid: int) -> bool:
    if not argv or os.path.basename(argv[0]) in NEVER_REAPED_BINARIES:
        return False
    for arg in argv[1:]:
        if not arg.startswith(BROWSER_PROFILE_FLAG):
            continue
        value = arg[len(BROWSER_PROFILE_FLAG):]
        if not os.path.isabs(value):
            continue
        parts = os.path.normpath(value).split(os.sep)
        if any(parts[i] == f"claude-{uid}" and parts[i + 1] and parts[i + 2] == sid
               for i in range(len(parts) - 2)):
            return True
    return False


def _reap_session_browsers(sid: str) -> list[int]:
    reaped = []
    try:
        uid = os.getuid()
        for pid, args in _process_listing():
            if BROWSER_PROFILE_FLAG not in args or sid not in args:
                continue
            argv = _proc_argv(pid)
            if not argv or not _owns_browser_profile(argv, sid, uid):
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                continue
            reaped.append(pid)
    except Exception:
        pass
    return reaped


def stop_failure() -> None:
    data = _read_stdin_json()
    sid = data.get("session_id")
    record = state.read_json(paths.ACTIVE / f"{sid}.json") if sid else None
    if not isinstance(record, dict) or record.get("agent") != "worker" or record.get("nested"):
        return
    account = record.get("account")
    message = data.get("last_assistant_message")
    if not isinstance(account, str) or not account or not isinstance(message, str):
        return
    model = stale_monitor._model_credit_banner_model(message)
    if model:
        stale_monitor.record_model_spent(account, model.lower(), time.time(), paths.ACCOUNT_STATE,
                                         paths.ACCOUNT_LOCK, paths.ACCOUNT_LEDGER, by="stop-failure")


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
        log = Path(transcript_path) if isinstance(transcript_path, str) and transcript_path else None
        if log is None:
            try:
                from .transcript import find_session_log
                log = find_session_log(sid, runtime=record.get("runtime") or "claude")
                transcript_path = str(log) if log else None
            except Exception:
                log = None
                transcript_path = None
        _accumulate_record_spend(record, log)
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
            "parent_manager_name": state.parent_name_of(record),
            "parent_name": state.parent_name_of(record),
            "parent_sid": record.get("parent_sid"),
            "kind": record.get("kind") or record.get("agent"),
            "depth": record.get("depth"),
            "runtime": record.get("runtime") or "claude",
            "provider": record.get("provider"),
            "account": record.get("account"),
            "transcript_path": transcript_path,
        })
    if record is not None:
        _append_spend_drop(record, "session_end")
    active_path.unlink(missing_ok=True)
    (paths.PERMISSION_PROMPTS / f"{sid}.json").unlink(missing_ok=True)
    from .registry import _drop_questions_for_worker
    _drop_questions_for_worker(sid)
    if (os.environ.get("CLAUDE_AGENT") == "worker" or (record or {}).get("agent") == "worker") and not (
            record is not None and stale_monitor.fable_move_closing(paths.ROOT, record, active_path)):
        _reap_session_browsers(sid)
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
