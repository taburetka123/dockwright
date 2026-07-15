"""Promote the current live (plain) claude session into an orchestrator worker.

`claude --resume <sid>` re-opens a session with its full history AND reuses the
original session id (verified: no `--fork-session` => same sid, same transcript
appended). Worker recognition is launch-time env (`CLAUDE_AGENT=worker`,
`CLAUDE_WORKER_NAME`, `CLAUDE_PARENT_MANAGER`) read by the SessionStart hook —
it can't be retrofitted into a live process. So the only clean promotion is to
relaunch the session under that env via `--resume`, which the hook then
registers as a worker. We deliberately do NOT hand-write the active record:
letting the hook do it is the whole point of the relaunch path.

This module reads `~/.claude/dockwright/active/*.json` directly (no MCP
dependency) so it works from a plain session that never connected to the
orchestrator MCP server.
"""
import os
import sys
from typing import Callable, Optional


def _pid_alive(pid: int) -> bool:
    """True if a process with `pid` currently exists. PermissionError means the
    process exists but is owned by someone else — still alive for our purposes."""
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
    """Pick the live general-domain manager to assign to.

    Returns (chosen, others, error):
      - chosen: the selected manager record, or None when there is none.
      - others: the remaining live general managers (non-empty only when >1).
      - error: a human message when no manager was found (chosen is None), else None.

    A manager is eligible when agent=="manager", its domain is general (treating
    absent/null/empty as general), and its pid is still alive. Among eligible
    managers the newest by started_at wins.
    """
    managers = []
    for record in records:
        if record.get("agent") != "manager":
            continue
        domain = record.get("domain")
        if domain not in (None, "", "general"):
            continue
        # Records are read straight off disk and may carry a malformed pid
        # (foreign tool, older schema) — a non-numeric pid must not crash the
        # whole resolution; treat it as "can't prove dead" and keep the manager.
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
    """Ownership record for an adopted live session. The promote path knows its
    own sid already (it resumes itself), so it writes assignments/<sid>.json
    directly — no pending/claim dance, and the resumed session's SessionStart
    must not claim anything (no CLAUDE_ASSIGNMENT_ID is set on this lane).
    There is no spawn prompt by construction → initial_prompt stays None.
    Best-effort: never blocks or fails the promotion.
    """
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
        # "never fails the promotion" includes _safe_segment's ValueError on a
        # malformed sid, not just filesystem errors.
        pass


def assign_to_manager_cli() -> None:
    """CLI entry: `dockwright assign-to-manager [--name N] [--sid S]`.

    Relaunches THIS session as a worker (resuming its own sid) in the workers
    window, assigned to the chosen general manager. The session id and
    window are read from the live shell env; --sid is an override for testing.
    """
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

    # Keep promoted workers off the phone remote-control enrolment. Intentionally does NOT use
    # _claude_worker_settings_args (which also sets enableAllProjectMcpServers for fresh-worktree
    # workers): promote resumes an already-interactive session in os.getcwd(), where any MCP-enable
    # prompt was already cleared — so only the remote-control-off settings apply here.
    extra_args = ["--settings", '{"remoteControlAtStartup": false, "disableRemoteControl": true}']
    env = {"CLAUDE_PARENT_MANAGER": manager_name}

    from .spawner import spawn_worker_tab

    # Mirror spawn_worker_impl: bound the spawn at 15s and treat a missing/hung
    # tmux (FileNotFoundError → OSError, refused connection, timeout) as a clean
    # launch failure rather than an uncaught traceback or an infinite hang.
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
