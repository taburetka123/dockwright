from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time

from . import lane_io, paths, state
from .session_pid import resolve_cli_pid

DEFAULT_RESOLVE_GRACE_SEC = 600
BACKOFF_AFTER_SEC = 60
BACKOFF_INTERVAL_SEC = 15
UNKNOWN_EXIT_STREAK = 3
GRACE_ENV = "CLAUDE_ORCH_LANE_RESOLVE_GRACE_SEC"


def lanes_dir(cli_pid: int):
    return paths.LANES / str(cli_pid)


def marker_path(cli_pid: int, lane: str):
    return lanes_dir(cli_pid) / f"{lane}.json"


def lock_path(cli_pid: int, lane: str):
    return lanes_dir(cli_pid) / f"{lane}.lock"


IN_DAEMON_LANES = ("questions", "done", "turn-ends", "stale")


def runs_in_daemon(lane: str) -> bool:
    return lane in IN_DAEMON_LANES and not lane_io.lanes_in_manager()


def write_marker(cli_pid: int, lane: str, *, started: float, resolved: bool | None,
                 now: float | None = None) -> None:
    now = time.time() if now is None else now
    try:
        state.write_json_atomic(marker_path(cli_pid, lane), {
            "lane": lane,
            "lane_pid": os.getpid(),
            "cli_pid": cli_pid,
            "started": started,
            "last_iter": now,
            "resolved": resolved,
        })
    except OSError as exc:
        print(f"monitor-loop: marker write failed for {lane} ({exc})", file=sys.stderr)


def acquire_lock(cli_pid: int, lane: str):
    path = lock_path(cli_pid, lane)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def run_scan(lane: str) -> int:
    return subprocess.run([sys.executable, "-m", "dockwright", "monitor", lane],
                          check=False).returncode


def loop(lane: str, *, cli_pid: int, run=run_scan, sleep=time.sleep, clock=time.time,
         grace: float = DEFAULT_RESOLVE_GRACE_SEC, worker: bool = False) -> int:
    lock_fd = acquire_lock(cli_pid, lane)
    if lock_fd is None:
        print(f"monitor-loop: {lane} lane already running under CLI pid {cli_pid}; "
              f"exiting as a duplicate", file=sys.stderr)
        return lane_io.EXIT_LANE_DUPLICATE
    try:
        return _run(lane, cli_pid=cli_pid, run=run, sleep=sleep, clock=clock, grace=grace,
                    worker=worker)
    finally:
        os.close(lock_fd)


def _run(lane: str, *, cli_pid: int, run, sleep, clock, grace: float, worker: bool) -> int:
    interval = lane_io.LANES[lane]
    started = clock()
    unresolved_since = None
    unknown_streak = 0
    unknown_code = None
    resolved: bool | None = None
    write_marker(cli_pid, lane, started=started, resolved=resolved, now=started)
    while True:
        if runs_in_daemon(lane):
            run(lane)
            print(f"monitor-loop: {lane} lane runs in the daemon ([lanes] in_manager = false)", file=sys.stderr)
            return 0
        rc = run(lane)
        now = clock()
        if rc == 0:
            resolved = True
        elif rc == 2:
            resolved = False
        write_marker(cli_pid, lane, started=started, resolved=resolved, now=now)
        if rc == 0:
            unresolved_since = None
            unknown_streak = 0
            unknown_code = None
            sleep(interval)
            continue
        if rc == 2:
            unknown_streak = 0
            unknown_code = None
            if worker:
                print(f"monitor-loop: {lane} lane in a worker session; no manager here",
                      file=sys.stderr)
                return lane_io.EXIT_LANE_UNRESOLVED
            unresolved_since = now if unresolved_since is None else unresolved_since
            waited = now - unresolved_since
            if waited >= grace:
                print(f"monitor-loop: {lane} lane found no owning manager for "
                      f"{int(grace)}s; ending", file=sys.stderr)
                return lane_io.EXIT_LANE_UNRESOLVED
            sleep(interval if waited < BACKOFF_AFTER_SEC else BACKOFF_INTERVAL_SEC)
            continue
        if rc in (lane_io.EXIT_LANE_DEAD, lane_io.EXIT_LANE_WEDGED):
            return rc
        unknown_streak = unknown_streak + 1 if rc == unknown_code else 1
        unknown_code = rc
        if unknown_streak >= UNKNOWN_EXIT_STREAK:
            print(f"monitor-loop: {lane} scan exited {rc} {unknown_streak} times in a row; "
                  f"ending with that code", file=sys.stderr)
            return rc
        sleep(interval)


def _grace_from_env() -> float:
    raw = os.environ.get(GRACE_ENV, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_RESOLVE_GRACE_SEC
    except ValueError:
        value = DEFAULT_RESOLVE_GRACE_SEC
    return value if value >= 0 else DEFAULT_RESOLVE_GRACE_SEC


def main(argv: list[str]) -> int:
    usage = f"Usage: dockwright monitor-loop <{'|'.join(lane_io.LANES)}>"
    if len(argv) != 1 or argv[0] not in lane_io.LANES:
        print(usage, file=sys.stderr)
        return 2
    lane = argv[0]
    if runs_in_daemon(lane):
        print(f"monitor-loop: {lane} lane runs in the daemon ([lanes] in_manager = false)", file=sys.stderr)
        return 0
    cli_pid = resolve_cli_pid(os.getppid())
    if cli_pid is None:
        print(f"monitor-loop: no claude/codex process among the ancestors of pid "
              f"{os.getppid()}; this lane cannot be filed or deduplicated", file=sys.stderr)
        return lane_io.EXIT_NO_CLI
    return loop(lane, cli_pid=cli_pid, grace=_grace_from_env(),
                worker=os.environ.get("CLAUDE_AGENT") == "worker")
