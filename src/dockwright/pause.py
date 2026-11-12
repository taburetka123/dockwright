from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, daemon, lane_io, paths, revive, state
from .mcp_server import _gate_state
from .registry import _drop_questions_for_worker, is_engineer_ask, unique_question_paths
from .revive import PAUSE_CLOSED_REASON, Candidate, Window, deliver, not_ready_reason, spawn_manager_window
from .stale_monitor import _closed_record
from .terminal import get_driver

__all__ = ["PAUSE_CLOSED_REASON", "main"]

PID_GONE_TIMEOUT_SEC = 15.0
PID_GONE_POLL_SEC = 0.2
HEADLESS_PROVIDERS = ("claude-exec", "codex-exec")
_HOLD_INSERT_RE = re.compile(r"\{([^{}:]+):([^{}]*)\}")

PAUSE_INSTRUCTION = (
    "You are manager {name} on domain {domain}, back on your own transcript after a fleet "
    "pause from {paused_at} to {now}. First invoke Skill(dockwright:arm-lanes) and, after "
    "`sleep 10`, confirm with `dockwright lanes`. {workers} Pipeline nodes parked with "
    "closed_reason fleet-pause wake on their own events; a NODE_PARKED page for one of them is "
    "yours to act on as it says. "
    "daemon-stop is held by dockwright resume and lifts by itself once every manager is "
    "back: never remove it. {held} Then continue your own work."
)
PAUSE_WORKERS_SENTENCE = (
    "Your plain workers were paused too: {names}. Resume each with resume_worker(<name>) — "
    "list_closed_workers shows them with closed_reason fleet-pause — and tell each one it was "
    "paused for the engineer's vacation and should pick its task back up."
)
PAUSE_NO_WORKERS_SENTENCE = "No plain workers of yours were paused."
PAUSE_HELD_SENTENCE = "The lanes behind {hold_files} stay off until the engineer lifts them himself."

_run = daemon._run


def _uid() -> int:
    return os.getuid()


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)


def _unlink(path: Path, missing_ok: bool = True) -> None:
    path.unlink(missing_ok=missing_ok)


def _rename(src: Path, dst: Path) -> None:
    os.replace(src, dst)


def _stop_path(name: str) -> Path:
    path = Path(os.path.expanduser(name))
    return path if path.is_absolute() else paths.ROOT / path


def _stamp(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "unrenderable local time"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _inside_dockwright_tmux(driver) -> bool:
    raw = os.environ.get("TMUX", "")
    if not raw:
        return False
    return Path(raw.split(",", 1)[0]).name == driver.socket()


def write_manifest(manifest: dict) -> None:
    state.write_json_atomic(paths.PAUSE_MANIFEST, manifest)


def read_manifest() -> dict | None:
    manifest = state.read_json(paths.PAUSE_MANIFEST)
    return manifest if isinstance(manifest, dict) else None


def is_pipeline_node(sid: str) -> bool:
    assignment = state.read_json(paths.assignment_path(sid))
    return isinstance(assignment, dict) and bool(assignment.get("ticket") and assignment.get("pipeline_request_id"))


def expand_hold_command(command: str, flags: dict) -> str:
    expanded = _HOLD_INSERT_RE.sub(lambda m: m.group(2) if flags.get(m.group(1)) else "", command)
    return re.sub(r" {2,}", " ", expanded).strip()


def _session_alive(record: dict) -> bool:
    return revive.record_pid_is_any_session(record)


def _active_records() -> list[dict]:
    return [r for r in state.list_json_in(paths.ACTIVE) if isinstance(r, dict) and not r.get("nested")]


def _pending_question_sids() -> set[str]:
    sids = set()
    for q_path in unique_question_paths():
        question = state.read_json(q_path)
        if isinstance(question, dict) and not is_engineer_ask(question) and question.get("worker_sid"):
            sids.add(question["worker_sid"])
    return sids


def check_quiet(records: list[dict], driver) -> list[str]:
    problems = []
    asked = _pending_question_sids()
    for record in records:
        sid, name = record.get("claude_sid") or "?", record.get("name") or record.get("claude_sid") or "?"
        if (record.get("runtime") or "claude") != "claude":
            problems.append(f"{name}: codex session: close it through the manager first")
            continue
        if record.get("provider") in HEADLESS_PROVIDERS:
            if _session_alive(record):
                problems.append(f"{name}: headless child alive")
            continue
        pane = state.window_id_of(record)
        screen = driver.capture_screen_ansi(pane) if pane else None
        if screen is None and not _session_alive(record):
            continue
        reason = not_ready_reason(screen, driver.pane_current_command(pane) if pane else None)
        if reason is not None:
            problems.append(f"{name}: {reason}")
        if sid in asked:
            problems.append(f"{name}: pending question")
        if record.get("agent") != "worker":
            continue
        for other in records:
            if other.get("parent_sid") == sid and _session_alive(other):
                problems.append(f"{name}: live subworker {other.get('name') or other.get('claude_sid')}")
    return problems


def _hold_flags(hold: dict[str, str]) -> dict[str, bool]:
    flags = {}
    for command in hold.values():
        for match in _HOLD_INSERT_RE.finditer(command):
            flags[match.group(1)] = (paths.ROOT / match.group(1)).exists()
    return flags


def build_manifest(records: list[dict], *, run, uid: int, now: float) -> dict:
    managers, workers = [], []
    for record in records:
        if record.get("agent") == "manager":
            managers.append({"claude_sid": record.get("claude_sid"), "name": record.get("name") or "",
                             "domain": record.get("domain") or "", "account": record.get("account"),
                             "cwd": record.get("cwd"), "pid": record.get("pid"),
                             "window_id": state.window_id_of(record), "started_at": record.get("started_at"),
                             "resume_pane": None, "notice_delivered_at": None, "record": record})
        elif record.get("agent") == "worker":
            sid = record.get("claude_sid") or ""
            workers.append({"claude_sid": sid, "name": record.get("name") or "",
                            "parent_manager_name": record.get("parent_manager_name") or record.get("parent_name"),
                            "pipeline_node": is_pipeline_node(sid), "record": record})
    hold = config.pause_hold()
    held = {_stop_path(name) for name in hold}
    stop_files = [name for name in config.pause_stop_files() if _stop_path(name) not in held]
    if not any(_stop_path(name) == paths.DAEMON_STOP for name in stop_files):
        stop_files = [str(paths.DAEMON_STOP), *stop_files]
    return {"paused_at": now, "resumed_at": None, "managers": managers, "workers": workers,
            "stop_files": {name: "existed" if _stop_path(name).exists() else "created" for name in stop_files},
            "hold": {name: "existed" if _stop_path(name).exists() else "created" for name in hold},
            "flags": _hold_flags(hold),
            "launchd": {label: {"was_loaded": daemon._loaded(run, uid, label),
                                "plist": str(paths.LAUNCH_AGENTS / f"{label}.plist")}
                        for label in config.pause_launchd_disable()},
            "tmux_socket": get_driver().socket()}


def launchd_disable(label: str, *, run, uid: int) -> None:
    run(["launchctl", "bootout", f"gui/{uid}/{label}"])
    run(["launchctl", "disable", f"gui/{uid}/{label}"])


def launchd_enable(label: str, plist: Path, *, run, uid: int) -> str | None:
    if not plist.exists():
        return f"launchd {label}: plist {plist} is missing — bootstrap it yourself"
    run(["launchctl", "enable", f"gui/{uid}/{label}"])
    run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)])
    return None


def stop_loops(manifest: dict, *, run, uid: int) -> None:
    for name in list(manifest.get("stop_files") or {}) + list(manifest.get("hold") or {}):
        _touch(_stop_path(name))
    for label, info in (manifest.get("launchd") or {}).items():
        if info.get("was_loaded"):
            launchd_disable(label, run=run, uid=uid)


def _wait_pid_gone(record: dict) -> None:
    deadline = time.monotonic() + PID_GONE_TIMEOUT_SEC
    while _session_alive(record) and time.monotonic() < deadline:
        time.sleep(PID_GONE_POLL_SEC)


def archive_workers(manifest: dict, driver, *, now: float) -> list[str]:
    lines = []
    for worker in manifest.get("workers") or []:
        record, sid = worker["record"], worker["claude_sid"]
        window_id = state.window_id_of(record)
        paths.CLOSED.mkdir(parents=True, exist_ok=True)
        state.write_json_atomic(paths.CLOSED / f"{sid}.json",
                                _closed_record(record, PAUSE_CLOSED_REASON, window_id, record.get("transcript_path")))
        _unlink(paths.ACTIVE / f"{sid}.json")
        if window_id:
            driver.close(window_id)
        _wait_pid_gone(record)
        _drop_questions_for_worker(sid)
        lines.append(f"pause: archived {worker['name'] or sid} sid {sid}")
    return lines


def close_managers(manifest: dict, driver) -> list[str]:
    lines = []
    for manager in manifest.get("managers") or []:
        sid = manager["claude_sid"]
        if manager.get("window_id"):
            driver.close(manager["window_id"])
        _wait_pid_gone(manager["record"])
        _unlink(paths.ACTIVE / f"{sid}.json")
        lines.append(f"pause: closed manager {manager['name'] or sid} ({manager['domain']}) sid {sid}")
    return lines


def kill_tmux(manifest: dict, driver) -> list[str]:
    known = {m.get("window_id") for m in manifest.get("managers") or []}
    known |= {state.window_id_of(w["record"]) for w in manifest.get("workers") or []}
    lines = []
    for session in driver.ls() or []:
        for tab in session.get("tabs") or []:
            for window in tab.get("windows") or []:
                if window.get("id") not in known:
                    lines.append(f"pause: killing tmux with a pane not in the manifest: {window.get('title') or '?'}")
    _run(["tmux", "-L", manifest.get("tmux_socket") or driver.socket(), "kill-server"])
    return lines


def _armed_rule_names() -> list[str]:
    names = [name for name, rule in config.events_rules().items() if rule.get("stop_file")]
    names += [cmd["selector"] for cmd in config.events_source("exec")["commands"] if cmd.get("stop_file")]
    return names


def _hold_warnings() -> list[str]:
    armed = _armed_rule_names()
    if not config.pause_hold() and armed:
        return [f"pause: WARNING [pause.hold] is empty — rules with a stop_file stay armed: {', '.join(armed)}"]
    return []


def _plan_lines(manifest: dict) -> list[str]:
    lines = [f"pause: would write {paths.PAUSE_MANIFEST} for {len(manifest['managers'])} manager(s) and "
             f"{len(manifest['workers'])} worker(s)"]
    for name, status in {**manifest["stop_files"], **manifest["hold"]}.items():
        lines.append(f"pause: would {'keep' if status == 'existed' else 'create'} stop file {_stop_path(name)}")
    for label, info in manifest["launchd"].items():
        lines.append(f"pause: would bootout and disable {label}" if info["was_loaded"]
                     else f"pause: would leave {label} alone (not loaded)")
    for worker in manifest["workers"]:
        lines.append(f"pause: would archive {worker['name']} sid {worker['claude_sid']} as {PAUSE_CLOSED_REASON}")
    for manager in manifest["managers"]:
        lines.append(f"pause: would close manager {manager['name']} ({manager['domain']}) sid {manager['claude_sid']}")
    lines.append(f"pause: would kill tmux server {manifest['tmux_socket']}")
    return lines


def _pause_report(manifest: dict, archived: int, closed: int) -> list[str]:
    stops = ", ".join(f"{name} ({status})" for name, status in {**manifest["stop_files"], **manifest["hold"]}.items())
    lines = [f"pause: archived {archived} worker(s), closed {closed} manager(s)", f"pause: stop files: {stops}"]
    if manifest["launchd"]:
        lines.append("pause: launchd disabled: "
                     + (", ".join(l for l, i in manifest["launchd"].items() if i["was_loaded"]) or "none"))
    lines.append("pause: reboot is safe; run `dockwright resume` to come back")
    return lines


def pause_main(args) -> int:
    driver = get_driver()
    if _inside_dockwright_tmux(driver):
        print("pause: run me outside the dockwright tmux server", file=sys.stderr)
        return 1
    if paths.PAUSE_MANIFEST.exists():
        print("pause: a pause is already in effect; run dockwright resume first", file=sys.stderr)
        return 1
    errors = config.pause_errors()
    if errors:
        for error in errors:
            print(f"pause: {error}", file=sys.stderr)
        print("pause: fix dockwright.toml first; nothing was written", file=sys.stderr)
        return 1
    if lane_io.lanes_in_manager():
        print("pause: the lanes run in the managers, whose reconcile and event rules keep running while pause "
              "closes the fleet; set [lanes] in_manager = false in dockwright.toml first", file=sys.stderr)
        return 1
    for warning in _hold_warnings():
        print(warning)
    print("pause: waiting for the daemon tick to finish")
    paths.DAEMON.mkdir(parents=True, exist_ok=True)
    tick_fd = daemon._try_lock("tick.lock", blocking=True)
    try:
        observe_fd = daemon._try_lock("observe.lock", blocking=True)
        try:
            return _pause_locked(args, driver)
        finally:
            os.close(observe_fd)
    finally:
        os.close(tick_fd)


def _pause_locked(args, driver) -> int:
    if paths.PAUSE_MANIFEST.exists():
        print("pause: a pause is already in effect; run dockwright resume first", file=sys.stderr)
        return 1
    records = _active_records()
    problems = check_quiet(records, driver)
    if problems:
        for problem in problems:
            print(f"pause: not quiet — {problem}")
        print("pause: nothing was written; quiet the fleet through the manager and run me again")
        return 1
    now = time.time()
    manifest = build_manifest(records, run=_run, uid=_uid(), now=now)
    if args.dry_run:
        for line in _plan_lines(manifest):
            print(line)
        return 0
    paths.PAUSE.mkdir(parents=True, exist_ok=True)
    write_manifest(manifest)
    stop_loops(manifest, run=_run, uid=_uid())
    archived = archive_workers(manifest, driver, now=now)
    for line in archived:
        print(line)
    closed = close_managers(manifest, driver)
    for line in closed:
        print(line)
    for line in kill_tmux(manifest, driver):
        print(line)
    for line in _pause_report(manifest, len(archived), len(closed)):
        print(line)
    return 0


def _plain_worker_names(manager: dict, manifest: dict) -> list[str]:
    names = []
    for worker in manifest.get("workers") or []:
        if worker.get("pipeline_node") or worker.get("parent_manager_name") != manager.get("name"):
            continue
        closed = state.read_json(paths.CLOSED / f"{worker['claude_sid']}.json")
        if isinstance(closed, dict) and closed.get("closed_reason") == PAUSE_CLOSED_REASON:
            names.append(worker.get("name") or worker["claude_sid"])
    return names


def pause_notice(manager: dict, manifest: dict, now: float) -> str:
    names = _plain_worker_names(manager, manifest)
    workers = PAUSE_WORKERS_SENTENCE.format(names=", ".join(names)) if names else PAUSE_NO_WORKERS_SENTENCE
    hold = list(manifest.get("hold") or {})
    held = PAUSE_HELD_SENTENCE.format(hold_files=", ".join(hold)) if hold else ""
    text = PAUSE_INSTRUCTION.format(name=manager.get("name") or "?", domain=manager.get("domain") or "?",
                                    paused_at=_stamp(float(manifest.get("paused_at") or 0)), now=_stamp(now),
                                    workers=workers, held=held)
    return re.sub(r" {2,}", " ", text)


def lost_gates() -> list[str]:
    lines = []
    if not paths.ARTIFACTS.is_dir():
        return lines
    for task_dir in sorted(paths.ARTIFACTS.iterdir()):
        if not (task_dir / "gate.open.md").exists():
            continue
        gate = _gate_state(task_dir.name)
        if gate and gate.get("state") == "lost":
            lines.append(f"resume: gate of {task_dir.name}/{gate.get('node') or '?'} is lost — the node reopens "
                         "it on its next wake; nudge it with topic_message")
    return lines


def _restamp_closed(manifest: dict, now: float) -> None:
    for worker in manifest.get("workers") or []:
        path = paths.CLOSED / f"{worker['claude_sid']}.json"
        closed = state.read_json(path)
        if not isinstance(closed, dict) or closed.get("closed_reason") != PAUSE_CLOSED_REASON:
            continue
        closed.setdefault("paused_at", closed.get("closed_at"))
        closed["closed_at"] = closed["resumed_at"] = now
        state.write_json_atomic(path, closed)


def start_loops(manifest: dict, *, run, uid: int, now: float) -> list[str]:
    _restamp_closed(manifest, now)
    lines = lost_gates() or ["resume: lost gates: none"]
    removed = [name for name, status in (manifest.get("stop_files") or {}).items() if status == "created"]
    for name in removed:
        _unlink(_stop_path(name), missing_ok=True)
    lines.append("resume: stop files removed: " + (", ".join(removed) or "none"))
    enabled = []
    for label, info in (manifest.get("launchd") or {}).items():
        if not info.get("was_loaded"):
            continue
        error = launchd_enable(label, Path(info.get("plist") or ""), run=run, uid=uid)
        if error:
            lines.append(f"resume: {error}")
        else:
            enabled.append(label)
    if manifest.get("launchd"):
        lines.append("resume: launchd enabled: " + (", ".join(enabled) or "none"))
    return lines


def _hold_report(manifest: dict) -> list[str]:
    hold = config.pause_hold()
    created = [name for name, status in (manifest.get("hold") or {}).items() if status == "created"]
    if not created:
        return []
    lines = [f"resume: kept: {', '.join(created)} — arm them yourself:"]
    for name in created:
        command = hold.get(name)
        if command:
            lines.append(f"resume:   {expand_hold_command(command, manifest.get('flags') or {})}")
    return lines


def _manager_candidate(manager: dict) -> Candidate:
    return Candidate(record=manager.get("record") or {}, sid=manager["claude_sid"], name=manager.get("name") or "",
                     domain=manager.get("domain") or "", verdict="revive", reason=PAUSE_CLOSED_REASON)


def _pane_resumes(driver, pane: str, sid: str) -> bool:
    for session in driver.ls() or []:
        for tab in session.get("tabs") or []:
            for window in tab.get("windows") or []:
                if window.get("id") == pane:
                    return sid in (window.get("start_command") or "")
    return False


async def _open_windows(manifest: dict, driver, now: float) -> list[tuple[dict, Window]]:
    windows = []
    for manager in manifest.get("managers") or []:
        if manager.get("notice_delivered_at"):
            continue
        candidate = _manager_candidate(manager)
        text = pause_notice(manager, manifest, now)
        active = state.read_json(paths.ACTIVE / f"{manager['claude_sid']}.json")
        registered = False
        if isinstance(active, dict) and _session_alive(active):
            pane, registered = state.window_id_of(active), True
        elif manager.get("resume_pane") and _pane_resumes(driver, manager["resume_pane"], manager["claude_sid"]):
            pane = manager["resume_pane"]
        else:
            try:
                pane = await spawn_manager_window(candidate, resume=True)
            except Exception as err:
                print(f"resume: {manager.get('name') or '?'} ({manager.get('domain') or '?'}) — NOT resumed — "
                      f"{type(err).__name__}: {err}")
                continue
            manager["resume_pane"] = pane
            write_manifest(manifest)
        windows.append((manager, Window(candidate, digest=None, pane=pane, text=text, resume=True,
                                        registered=registered, confirmed=registered)))
    return windows


def _resume_plan(manifest: dict) -> list[str]:
    lines = []
    for manager in manifest.get("managers") or []:
        if manager.get("notice_delivered_at"):
            status = "notice already delivered"
        elif manager.get("resume_pane"):
            status = f"window {manager['resume_pane']} from an earlier resume"
        else:
            status = "would open claude --resume on its own transcript"
        lines.append(f"resume: would notify {manager.get('name') or '?'} ({manager.get('domain') or '?'}) "
                     f"sid {manager['claude_sid']} — {status}")
    created = [n for n, s in (manifest.get("stop_files") or {}).items() if s == "created"]
    lines.append("resume: would remove stop files: " + (", ".join(created) or "none"))
    loaded = [l for l, i in (manifest.get("launchd") or {}).items() if i.get("was_loaded")]
    lines.append("resume: would enable and bootstrap: " + (", ".join(loaded) or "none"))
    lines.extend(_hold_report(manifest))
    return lines


async def _resume(args, driver) -> int:
    manifest = read_manifest()
    if manifest is None:
        print(f"resume: no pause manifest at {paths.PAUSE_MANIFEST}; nothing to resume", file=sys.stderr)
        return 1
    if args.dry_run:
        for line in _resume_plan(manifest):
            print(line)
        return 0
    pairs = await _open_windows(manifest, driver, time.time())
    await deliver([window for _, window in pairs], prefix="resume")
    for manager, window in pairs:
        if window.delivered:
            manager["notice_delivered_at"] = time.time()
    write_manifest(manifest)
    if not all(m.get("notice_delivered_at") for m in manifest.get("managers") or []):
        print("resume: daemon-stop stays until every manager is notified; run dockwright resume again")
        return 1
    now = time.time()
    for line in start_loops(manifest, run=_run, uid=_uid(), now=now):
        print(line)
    manifest["resumed_at"] = now
    done = paths.PAUSE / f"{_iso(float(manifest.get('paused_at') or 0))}-resumed.json"
    write_manifest(manifest)
    _rename(paths.PAUSE_MANIFEST, done)
    for line in _hold_report(manifest):
        print(line)
    named = sorted({n for m in manifest.get("managers") or [] for n in _plain_worker_names(m, manifest)})
    print("resume: plain workers named to their managers: " + (", ".join(named) or "none"))
    print(f"resume: manifest archived as {done}")
    return 0


def resume_main(args) -> int:
    rc = asyncio.run(_resume(args, get_driver()))
    if rc == 0 and not args.dry_run and not args.no_attach:
        return revive.attach_to_manager_session()
    return rc


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dockwright", description="Pause the whole fleet for a vacation, or resume it.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pause", help="quiet check, manifest, stop files, launchd disable, workers archived as "
                                     f"{PAUSE_CLOSED_REASON}, managers closed, tmux killed")
    p.add_argument("--dry-run", action="store_true", help="print the plan; write nothing, close nothing")
    r = sub.add_parser("resume", help="managers back on their own transcripts, notice delivered, then the daemon")
    r.add_argument("--dry-run", action="store_true", help="print the plan; open nothing, remove nothing")
    r.add_argument("--no-attach", action="store_true",
                   help="leave the terminal where it is instead of attaching to the manager session")
    args = parser.parse_args(argv)
    return pause_main(args) if args.command == "pause" else resume_main(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
