import os
import sys
from typing import Callable, Optional


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def resolve_general_manager(
    records: list[dict],
    is_alive: Callable[[int], bool] = _pid_alive,
) -> tuple[Optional[dict], list[dict], Optional[str]]:
    managers = []
    for record in records:
        if record.get("agent") != "manager":
            continue
        domain = record.get("domain")
        if domain not in (None, "", "general"):
            continue
        pid = record.get("pid")
        try:
            pid_int = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            pid_int = None
        if pid_int is not None and not is_alive(pid_int):
            continue
        managers.append(record)
    if not managers:
        return None, [], "No active general-domain manager. Start one with /manager."
    managers.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
    return managers[0], managers[1:], None


def _read_active_records() -> list[dict]:
    from . import paths, state
    return list(state.list_json_in(paths.ACTIVE))


def _write_promoted_assignment(sid: str, name: str, manager_name: str,
                               task_key: str | None = None) -> None:
    import time

    from . import paths, state

    try:
        target = paths.assignment_path(sid)
        if target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        state.write_json_atomic(target, {
            "claude_sid": sid,
            "name": name,
            "requested_name": name,
            "initial_prompt": None,
            "promoted": True,
            "cwd": os.getcwd(),
            "branch": None,
            "manager_sid": None,
            "parent_manager_name": manager_name,
            "runtime": "claude",
            "ticket": task_key,
            "spawned_at": time.time(),
        })
    except Exception:
        pass


def assign_to_manager_cli() -> None:
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(prog="dockwright assign-to-manager")
    parser.add_argument("--name", default=None, help="Worker routing name. Defaults to adopted-<sid8>.")
    parser.add_argument("--sid", default=None, help="Session id to resume. Defaults to $CLAUDE_CODE_SESSION_ID.")
    parser.add_argument("--task-key", default=None,
                        help="Grouping key for the assignment record (task_key: any stable slug); "
                             "joins this session into pipeline_status(task_key).")
    args = parser.parse_args(sys.argv[2:])

    if args.task_key is not None:
        from .mcp_server import _validate_task_key
        try:
            _validate_task_key(args.task_key)
        except ValueError as exc:
            print(f"ERROR: invalid --task-key: {exc}", file=sys.stderr)
            sys.exit(1)

    sid = args.sid or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        from . import config
        print(
            "ERROR: CLAUDE_CODE_SESSION_ID is not set; cannot resume this session. "
            f"Run {config.assign_command_hint()} from inside a live Claude Code session.",
            file=sys.stderr,
        )
        sys.exit(1)

    chosen, others, error = resolve_general_manager(_read_active_records())
    if error:
        print(error)
        sys.exit(1)

    manager_name = chosen.get("name") or "manager"
    name = args.name or f"adopted-{sid[:8]}"

    print(f"Assigning this session to general manager '{manager_name}'.")
    if others:
        other_names = ", ".join(o.get("name") or "?" for o in others)
        print(
            f"Note: {len(others)} other general manager(s) also active ({other_names}); "
            "picked the newest by started_at."
        )

    extra_args = ["--settings", '{"remoteControlAtStartup": false, "disableRemoteControl": true}']
    env = {"CLAUDE_PARENT_MANAGER": manager_name}

    from .spawner import spawn_worker_tab

    async def _spawn_with_timeout():
        async with asyncio.timeout(15):
            return await spawn_worker_tab(
                cwd=os.getcwd(),
                initial_prompt="",
                name=name,
                agent="worker",
                resume_sid=sid,
                route_to_workers_window=True,
                extra_args=extra_args,
                env=env,
            )

    try:
        window_id, _ = asyncio.run(_spawn_with_timeout())
    except (asyncio.TimeoutError, ConnectionRefusedError, OSError, RuntimeError) as exc:
        print(f"ERROR: could not launch the worker tab via tmux: {exc}", file=sys.stderr)
        sys.exit(1)

    _write_promoted_assignment(sid, name, manager_name, task_key=args.task_key)

    print(
        f"Relaunched as worker '{name}' (assigned to '{manager_name}') in the "
        f"'claude workers' window — tmux window {window_id}."
    )
    print(
        "Now close THIS tab (cmd+w). The worker copy in the workers window is the "
        "live continuation of this conversation; this tab is a stale duplicate until closed."
    )
