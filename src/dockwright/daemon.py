from __future__ import annotations

import fcntl
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import config, daemon_push, lane_io, lane_lines, monitor, notify, paths, registry, state

HEARTBEAT_STALE_SEC = 180
ALIVE_BEAT_SEC = 30
ERROR_REPEAT_SEC = 3600
TICKS_KEEP_SEC = 8 * 24 * 3600
TICKS_PRUNE_AFTER_SEC = 9 * 24 * 3600
TICK_INTERVAL_SEC = 60
WAKE_ROUNDS_MAX = 3
PAGES_SILENT_AFTER_SEC = 180
PAGES_SILENT_LADDER_MIN = (10, 20, 40)
STATE_LOCK_ATTEMPTS = 3
STATE_LOCK_RETRY_SEC = 0.05
WAKE_LABEL_SUFFIX = "daemon-wake"
DAEMON_SID = "daemon"
USAGE = "Usage: dockwright daemon tick | observe | sync | status | schedules | shadow-report"
_ROOTS = {"done": lambda: paths.DONE, "turn-ends": lambda: paths.TURN_ENDS}
_running: tuple[str, float, float] | None = None
_step_seconds: dict[str, float] = {}


def _file(name: str) -> Path:
    return paths.DAEMON / name


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, (json.dumps(record, separators=(",", ":")) + "\n").encode())
    finally:
        os.close(fd)


def read_jsonl(path: Path) -> list[dict]:
    try:
        text = path.read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def ledger(record: dict, now: float) -> None:
    append_jsonl(_file("ledger.jsonl"), {"ts": now, **record})


def wake() -> None:
    try:
        paths.DAEMON.mkdir(parents=True, exist_ok=True)
        with open(paths.DAEMON_WAKE, "a"):
            pass
        os.utime(paths.DAEMON_WAKE, None)
    except OSError:
        return


def _state_locked(write) -> None:
    for attempt in range(STATE_LOCK_ATTEMPTS):
        try:
            with state.file_lock_nb(_file("state.lock")):
                write()
            return
        except BlockingIOError:
            time.sleep(STATE_LOCK_RETRY_SEC)
    print("daemon: state.lock busy; skipped one write", file=sys.stderr)


def _enter(name: str) -> None:
    global _running
    began = time.monotonic()
    running = _running
    if running is not None:
        _step_seconds[running[0]] = round(_step_seconds.get(running[0], 0.0) + began - running[2], 3)
    _running = (name, lane_io.awake_seconds(), began)


def alive_stamp(now: float) -> None:
    record = {"ts": time.time(), "awake": lane_io.awake_seconds(), "pid": os.getpid(), "pass_at": now}
    running = _running
    if running is not None:
        record["step"], record["step_awake"] = running[0], running[1]
    state.write_json_atomic(_file("alive.json"), record)


def _beat(now: float, stop: threading.Event) -> None:
    while not stop.wait(ALIVE_BEAT_SEC):
        try:
            alive_stamp(now)
        except OSError:
            continue


class OutboxEmit:
    def __init__(self, manager_name: str, source: str = "daemon", domain: str | None = None):
        self.manager_name = manager_name
        self.source = source
        self.domain = domain

    def emit(self, line: str) -> None:
        target = paths.notify_outbox_dir_for(self.manager_name)
        target.mkdir(parents=True, exist_ok=True)
        name = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}.json"
        payload = {"line": line, "source": self.source}
        if self.domain:
            payload["domain"] = self.domain
        state.write_json_atomic(target / name, payload)


class LedgerEmit:
    def __init__(self, bucket: str, now: float):
        self.bucket, self.now = bucket, now

    def emit(self, line: str) -> None:
        ledger({"type": "line", "bucket": self.bucket, "line": line}, self.now)


def _recorded_start(path: Path) -> float | None:
    ts = _read_dict(path).get("ts")
    return float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else None


def ensure_start(now: float) -> float:
    path = _file("start.json")
    recorded = _recorded_start(path)
    if recorded is not None:
        return recorded
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"ts": now}))
    try:
        os.link(tmp, path)
    except FileExistsError:
        return _restore_start(path, now)
    finally:
        tmp.unlink(missing_ok=True)
    ledger({"type": "start"}, now)
    return now


def _restore_start(path: Path, now: float) -> float:
    recorded = _recorded_start(path)
    if recorded is not None:
        return recorded
    starts = [r["ts"] for r in read_jsonl(_file("ledger.jsonl"))
              if r.get("type") == "start" and isinstance(r.get("ts"), (int, float))]
    ts = float(starts[0]) if starts else now
    state.write_json_atomic(path, {"ts": ts})
    _error("start", ValueError(f"start.json was unreadable; restored ts {ts}"), now)
    if not starts:
        ledger({"type": "start"}, now)
    return ts


def _read_dict(path: Path) -> dict:
    data = state.read_json(path)
    return data if isinstance(data, dict) else {}


def _session_kind(sid) -> str | None:
    if not isinstance(sid, str) or not sid:
        return None
    for root in (paths.ACTIVE, paths.CLOSED):
        record = state.read_json(root / f"{sid}.json")
        if isinstance(record, dict):
            return record.get("kind")
    return None


def _writer_sid(kind: str, entry: Path) -> str:
    payload = _read_dict(entry)
    sid = payload.get("claude_sid") if kind == "done" else payload.get("sid")
    return sid if isinstance(sid, str) and sid else entry.name.rsplit("-", 1)[0]


def _source_ts(entry, now: float) -> float:
    try:
        return entry.stat().st_mtime
    except (OSError, AttributeError):
        return now


def _pre_start(entry, start: float, now: float) -> bool:
    return _source_ts(entry, now) < start


class DaemonScope:
    def __init__(self, start: float, now: float):
        self.start = start
        self.now = now
        self._managers: dict[str, str] = {}
        for record in state.list_json_in(paths.ACTIVE):
            if isinstance(record, dict) and record.get("agent") == "manager":
                self._managers[paths._event_bucket(record.get("name"))] = record.get("claude_sid")
        self._limited = {b for b in self._managers if monitor._manager_limited(b)}
        self.lanes_in_manager = lane_io.lanes_in_manager()
        self._question_stems: set[str] = set()

    def buckets(self, kind: str) -> list[str]:
        root = _ROOTS[kind]()
        if not root.is_dir():
            return []
        return sorted(d.name for d in root.iterdir() if d.is_dir() and d.name not in self._limited)

    def entries(self, kind: str, bucket: str) -> list[Path]:
        return [e for e in sorted((_ROOTS[kind]() / bucket).glob("*.json"))
                if _session_kind(_writer_sid(kind, e)) != "subworker"]

    def question_entries(self) -> list[tuple[Path, str]]:
        from . import mcp_server
        out = []
        self._question_stems = set()
        for entry in registry.unique_question_paths():
            self._question_stems.add(entry.stem)
            record = _read_dict(entry)
            if registry.is_engineer_ask(record):
                continue
            sid = record.get("worker_sid")
            if sid and mcp_server._asked_by_subworker(sid):
                continue
            bucket = paths._event_bucket(state.parent_name_of(record))
            if bucket in self._limited:
                continue
            out.append((entry, bucket))
        return out

    def load_seen(self, kind: str) -> set[str]:
        try:
            return {line for line in _file(f"seen-{kind}").read_text().splitlines() if line}
        except OSError:
            return set()

    def save_seen(self, kind: str, marked: list[str]) -> None:
        if kind == "questions":
            kept = {s for s in self.load_seen(kind) if s in self._question_stems}
        else:
            kept = {s for s in self.load_seen(kind) if Path(s).exists()}
        path = _file(f"seen-{kind}")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("".join(f"{s}\n" for s in sorted(kept | set(marked))))
        os.replace(tmp, path)

    def ladder_path(self, bucket: str) -> Path:
        return _file(f"fs-ladder-{bucket}.json")

    def own_sid(self, bucket: str) -> str | None:
        return self._managers.get(bucket)

    def may_journal(self, entry: Path) -> bool:
        return not _pre_start(entry, self.start, self.now)


def _task_key(kind: str, payload: dict) -> str | None:
    own = payload.get("task_key") or payload.get("ticket")
    if isinstance(own, str) and own:
        return own
    if kind == "QUESTION":
        return None
    sid = payload.get("claude_sid") if kind == "DONE" else payload.get("sid")
    if not isinstance(sid, str) or not sid:
        return None
    ticket = _read_dict(paths.assignment_path(sid)).get("ticket")
    return ticket if isinstance(ticket, str) and ticket else None


class DaemonSink:
    def __init__(self, start: float, now: float, scope: DaemonScope):
        self.start = start
        self.now = now
        self.scope = scope
        self.events = 0

    def begin(self) -> bool:
        return True

    def record(self, kind: str, key: str, outcome: str, fields: dict, line: str | None) -> None:
        payload = fields.get("payload") or {}
        ledger({"type": "event", "kind": kind, "key": key, "outcome": outcome,
                "reason": fields.get("reason"), "task_key": _task_key(kind, payload),
                "bucket": fields.get("bucket"), "gate": fields.get("gate"),
                "pre_start": _pre_start(fields.get("entry"), self.start, self.now)}, self.now)
        self.events += 1
        bucket = fields.get("bucket")
        if (outcome == monitor.OBSERVE_EMIT and line and not self.scope.lanes_in_manager
                and bucket not in (paths.UNSCOPED_BUCKET, DAEMON_SID, None)):
            OutboxEmit(bucket).emit(line)

    def end(self) -> None:
        return None


def _error(scan: str, err: Exception, now: float) -> None:
    print(f"daemon: {scan} failed ({type(err).__name__}: {err})", file=sys.stderr)
    key = f"{scan}:{type(err).__name__}"

    def write():
        seen = _read_dict(_file("errors.json"))
        last = seen.get(key)
        if isinstance(last, (int, float)) and now - last < ERROR_REPEAT_SEC:
            return
        seen[key] = now
        state.write_json_atomic(_file("errors.json"), seen)
        ledger({"type": "error", "scan": scan, "error": type(err).__name__, "detail": str(err)[:200]}, now)

    _state_locked(write)


def _oldest_tick(path: Path) -> float | None:
    try:
        with path.open() as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and isinstance(record.get("ts"), (int, float)):
                    return record["ts"]
    except OSError:
        return None
    return None


def _append_tick(record: dict, now: float) -> None:
    path = _file("ticks.jsonl")

    def write():
        oldest = _oldest_tick(path)
        if oldest is not None and now - oldest > TICKS_PRUNE_AFTER_SEC:
            kept = [r for r in read_jsonl(path)
                    if isinstance(r.get("ts"), (int, float)) and now - r["ts"] <= TICKS_KEEP_SEC]
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in kept))
            os.replace(tmp, path)
        append_jsonl(path, record)

    _state_locked(write)


def _push(now: float, start: float) -> list[dict]:
    from . import board
    return daemon_push.run(now, start, board.inbox()["waiting"])


def _observe_once(start: float, now: float) -> tuple[int, int]:
    scope = DaemonScope(start, now)
    sink = DaemonSink(start, now, scope)
    errors = 0
    scanned = {}
    for name, observe in (("done", monitor.observe_done), ("turn-ends", monitor.observe_turn_ends),
                          ("questions", monitor.observe_questions)):
        try:
            observe(scope, sink)
            scanned[name] = time.time()
        except Exception as err:
            errors += 1
            _error(name, err, now)
    lane_io.remember_observed(scanned)
    return sink.events, errors


def _wake_mtime() -> float:
    try:
        return paths.DAEMON_WAKE.stat().st_mtime
    except OSError:
        return 0.0


def _observe_rounds(start: float, now: float) -> tuple[int, int, int]:
    events = errors = rounds = 0
    while rounds < WAKE_ROUNDS_MAX:
        began = time.time()
        rounds += 1
        got, bad = _observe_once(start, now)
        events, errors = events + got, errors + bad
        if _wake_mtime() <= began:
            break
    return events, errors, rounds


def _try_lock(name: str, blocking: bool):
    fd = os.open(_file(name), os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _remember_mode() -> None:
    if lane_io.config_names_lanes_mode():
        lane_io.remember_lanes_mode(config.lanes_in_manager())


def observe(now: float | None = None) -> int:
    now = time.time() if now is None else now
    paths.DAEMON.mkdir(parents=True, exist_ok=True)
    if paths.DAEMON_STOP.exists():
        return 0
    _remember_mode()
    lock_fd = _try_lock("observe.lock", blocking=False)
    if lock_fd is None:
        return 0
    started = time.monotonic()
    try:
        events, errors, rounds = _observe_rounds(ensure_start(now), now)
    finally:
        os.close(lock_fd)
    duration = round(time.monotonic() - started, 3)
    beat = {"ts": now + duration, "pid": os.getpid(), "duration_sec": duration, "events": events, "rounds": rounds}
    state.write_json_atomic(_file("wake-heartbeat.json"), beat)
    _append_tick({**beat, "errors": errors, "trigger": "wake"}, now)
    return 0


def tick(now: float | None = None, push=None) -> int:
    now = time.time() if now is None else now
    paths.DAEMON.mkdir(parents=True, exist_ok=True)
    prior = _read_dict(_file("heartbeat.json"))
    if paths.DAEMON_STOP.exists():
        if not prior.get("stopped"):
            ledger({"type": "stopped"}, now)
        state.write_json_atomic(_file("heartbeat.json"), {"ts": now, "pid": os.getpid(), "stopped": True})
        return 0
    lock_fd = _try_lock("tick.lock", blocking=False)
    if lock_fd is None:
        return 0
    try:
        return _pass(now, prior, push or _push)
    finally:
        os.close(lock_fd)


def _pass(now: float, prior: dict, push) -> int:
    global _running
    _running = None
    _step_seconds.clear()
    started = time.monotonic()
    start = ensure_start(now)
    last = prior.get("ts")
    if prior.get("stopped"):
        ledger({"type": "resumed"}, now)
    elif isinstance(last, (int, float)) and now - last > HEARTBEAT_STALE_SEC:
        ledger({"type": "gap", "seconds": round(now - last), "since": last}, now)
    _remember_mode()
    _enter("observe")
    alive_stamp(now)
    try:
        from . import stale_monitor
    except Exception:
        stale_monitor = None
    stop = threading.Event()
    beater = threading.Thread(target=_beat, args=(now, stop), name="daemon-alive-beat", daemon=True)
    beater.start()
    try:
        state.memo_dirs([paths.ASSIGNMENTS, paths.CLOSED])
        if stale_monitor is not None:
            stale_monitor._DIR_MEMO = {}
        observe_fd = _try_lock("observe.lock", blocking=True)
        try:
            events, errors, rounds = _observe_rounds(start, now)
        finally:
            os.close(observe_fd)
        acting = not lane_io.lanes_in_manager()
        if acting:
            errors += _act(now, _managers(), acting)
        _enter("push")
        alive_stamp(now)
        pushes = 0
        try:
            for record in push(now + (time.monotonic() - started), start):
                ledger(record, now)
                pushes += record.get("type") == "push"
        except Exception as err:
            errors += 1
            _error("push", err, now)
        _enter("finish")
        duration = round(time.monotonic() - started, 3)
        beat = {"ts": now + duration, "pid": os.getpid(), "duration_sec": duration,
                "events": events, "pushes": pushes, "errors": errors, "acting": acting, "rounds": rounds}
        state.write_json_atomic(_file("heartbeat.json"), beat)
        _append_tick({**beat, "trigger": "interval", "steps": dict(_step_seconds)}, now)
    finally:
        stop.set()
        beater.join()
        state.memo_dirs(())
        if stale_monitor is not None:
            stale_monitor._DIR_MEMO = None
    alive_stamp(now)
    return 0


def _managers() -> list[dict]:
    out = []
    for record in state.list_json_in(paths.ACTIVE):
        if (isinstance(record, dict) and record.get("agent") == "manager" and not record.get("nested")
                and isinstance(record.get("pid"), int) and state._pid_alive(record["pid"]) and record.get("name")):
            out.append({"sid": record.get("claude_sid"), "name": record["name"], "started_at": record.get("started_at")})
    return out


def _env_mismatch() -> list[str]:
    return sorted(key for key, value in config.lanes_env().items() if os.environ.get(key) != value)


def _step(scan: str, now: float, fn) -> int:
    _enter(scan)
    alive_stamp(now)
    try:
        fn()
        return 0
    except Exception as err:
        _error(scan, err, now)
        return 1


def _run_stale(now: float, mgr: dict) -> None:
    from . import stale_monitor
    stale_monitor.main(mgr["name"], emit=OutboxEmit(mgr["name"]).emit, lanes_in_manager=False)


def _run_stale_daemon(now: float) -> None:
    from . import stale_monitor
    stale_monitor.main(DAEMON_SID, emit=LedgerEmit(DAEMON_SID, now).emit, lanes_in_manager=False)


def _page_error(now: float, mgr: dict, key: str, err: Exception) -> None:
    from .pipeline_reconcile import _PAGE_EVERY_SEC, _Pages
    if _Pages(mgr["name"]).throttled(("*", key), _PAGE_EVERY_SEC, now):
        return
    OutboxEmit(mgr["name"]).emit(lane_lines.render("RECONCILE_ERROR", task_key="*",
                                                   error=f"{key} {type(err).__name__}: {str(err)[:200]}"))


def _run_events(now: float, managers: list[dict]) -> None:
    from . import event_poll

    def emit(line: str, manager_name: str | None = None) -> None:
        targets = [manager_name] if manager_name else [m["name"] for m in managers]
        for name in targets:
            OutboxEmit(name).emit(line)

    event_poll.step(now, managers, emit, record=lambda r: ledger(r, now))


def _run_reconcile(now: float, mgr: dict, task_keys: list[str] | None = None) -> None:
    from . import pipeline_reconcile
    if monitor._manager_limited(mgr["name"]):
        return
    try:
        pipeline_reconcile.reconcile(mgr, now=now, emit=OutboxEmit(mgr["name"]).emit, task_keys=task_keys)
    except Exception as err:
        _page_error(now, mgr, "reconcile", err)
        raise


def _reconcile_daemon_works(now: float, emit, task_keys: list[str] | None = None) -> None:
    from . import pipeline_reconcile
    pipeline_reconcile.reconcile({"sid": DAEMON_SID, "name": DAEMON_SID}, now=now, emit=emit, task_keys=task_keys)


def _works_of(in_sight: dict | None, owner: str) -> list[str] | None:
    if in_sight is None:
        return None
    return sorted({*in_sight.get(owner, ()), *in_sight.get(None, ())})


def _run_notes(now: float, managers: list[dict]) -> None:
    from . import note_check
    try:
        note_check.run()
    except Exception as err:
        for mgr in managers:
            _page_error(now, mgr, "note-check", err)
        raise


def _run_schedules(now: float) -> list[dict]:
    from . import daemon_schedule
    return daemon_schedule.run(now)


def _pages_silence(mgr: dict, now: float) -> float | None:
    beat = state.read_json(lane_io.heartbeat_path(mgr["name"], "pages"))
    awake = beat.get("awake") if isinstance(beat, dict) else None
    if isinstance(awake, (int, float)):
        return lane_io.awake_since(awake)
    started = mgr.get("started_at")
    return now - started if isinstance(started, (int, float)) else None


def _pages_silent(now: float, managers: list[dict]) -> list[dict]:
    out = []
    now_awake = lane_io.awake_seconds()
    ladder = _read_dict(_file("pages-silent.json"))
    kept: dict = {}
    for mgr in managers:
        silence = _pages_silence(mgr, now)
        if silence is None or silence <= PAGES_SILENT_AFTER_SEC:
            continue
        prior = ladder.get(mgr["name"]) if isinstance(ladder.get(mgr["name"]), dict) else {}
        level = prior.get("level") if isinstance(prior.get("level"), int) else 0
        last = prior.get("at")
        rung = PAGES_SILENT_LADDER_MIN[min(level - 1, len(PAGES_SILENT_LADDER_MIN) - 1)] * 60 if level else 0
        if level and isinstance(last, (int, float)) and now_awake - last < rung:
            kept[mgr["name"]] = prior
            continue
        minutes = int(silence // 60)
        kept[mgr["name"]] = {"level": level + 1, "at": now_awake}
        out.append({"type": "pages_silent", "manager": mgr["name"], "minutes": minutes})
    state.write_json_atomic(_file("pages-silent.json"), kept)
    return out


def _notify_pages_silent(now: float, records: list[dict]) -> None:
    if config.daemon_push() == "none":
        for record in records:
            ledger({**record, "push": "none"}, now)
        return
    cap = config.daemon_int("push_cap_per_hour", config.DEFAULT_DAEMON_PUSH_CAP_PER_HOUR)
    saved = _read_dict(daemon_push.state_path())
    stamps = [t for t in saved.get("sent", []) if isinstance(t, (int, float)) and now - t < daemon_push.CAP_WINDOW_SEC]
    for record in records:
        if len(stamps) >= cap:
            ledger({**record, "capped": True}, now)
            continue
        rc = notify.macos(daemon_push.TITLE, f"{record['manager']}: pages monitor silent {record['minutes']} min")
        stamps.append(now)
        ledger({**record, "ok": rc == 0, "rc": rc}, now)
    state.write_json_atomic(daemon_push.state_path(), {**saved, "sent": stamps})


def _env_ready(now: float, managers: list[dict]) -> bool:
    missing = _env_mismatch()
    if not missing:
        return True
    err = ValueError(f"the daemon's environment lacks [lanes.env] {', '.join(missing)}; run setup.sh so the plists "
                     "carry it; the stale checks wait")
    _error("env", err, now)
    for mgr in managers:
        _page_error(now, mgr, "env", err)
    return False


def _page_schedule_records(now: float, managers: list[dict], records: list[dict]) -> None:
    for record in records:
        kind = record.get("type")
        if kind == "schedule_failed" or (kind == "schedule_skipped" and record.get("reason") == "unreadable"):
            what = kind.removeprefix("schedule_")
            if record.get("detail"):
                what += f" ({record.get('reason')}): {record['detail']}"
            else:
                what += f": {record.get('reason')}"
            err = RuntimeError(f"schedule {record.get('name')} {what}")
            for mgr in managers:
                _page_error(now, mgr, f"schedule:{record.get('name')}", err)


def _act(now: float, managers: list[dict], acting: bool) -> int:
    errors = 0
    if _env_ready(now, managers):
        for mgr in managers:
            errors += _step(f"stale:{mgr['name']}", now, lambda m=mgr: _run_stale(now, m))
        errors += _step("stale:daemon", now, lambda: _run_stale_daemon(now))
    else:
        errors += 1
    errors += _step("events", now, lambda: _run_events(now, managers))
    scanned: list[dict] = []

    def scan():
        from . import pipeline_reconcile
        scanned.append(pipeline_reconcile.sight())

    errors += _step("reconcile:scan", now, scan)
    in_sight = scanned[0] if scanned else None
    for mgr in managers:
        errors += _step(f"reconcile:{mgr['name']}", now,
                        lambda m=mgr: _run_reconcile(now, m, _works_of(in_sight, m["name"])))
    errors += _step("reconcile:daemon", now, lambda: _reconcile_daemon_works(
        now, LedgerEmit(DAEMON_SID, now).emit, _works_of(in_sight, DAEMON_SID)))
    errors += _step("notes", now, lambda: _run_notes(now, managers))

    def schedules():
        records = _run_schedules(now)
        for record in records:
            ledger(record, now)
        _page_schedule_records(now, managers, records)

    errors += _step("schedules", now, schedules)
    errors += _step("pages", now, lambda: _notify_pages_silent(now, _pages_silent(now, managers)))
    return errors


def label() -> str:
    return f"{config.loop_label_prefix()}.daemon"


def wake_label() -> str:
    return f"{config.loop_label_prefix()}.{WAKE_LABEL_SUFFIX}"


def plist_path() -> Path:
    return paths.LAUNCH_AGENTS / f"{label()}.plist"


def wake_plist_path() -> Path:
    return paths.LAUNCH_AGENTS / f"{wake_label()}.plist"


def _dockwright_bin() -> Path:
    return Path(sys.executable).parent / "dockwright"


def _installer() -> Path:
    return config.claude_config_home() / "scripts" / "daemon-install.sh"


def _run(cmd: list[str]) -> int:
    try:
        return subprocess.run(cmd, check=False, capture_output=cmd[0] == "launchctl").returncode
    except OSError:
        return 127


def _loaded(run, uid: int, name: str | None = None) -> bool:
    return run(["launchctl", "print", f"gui/{uid}/{name or label()}"]) == 0


def _plist_matches(path: Path, program: Path, mode: str = "tick", env: dict | None = None) -> bool:
    try:
        with path.open("rb") as fh:
            data = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return False
    if not isinstance(data, dict) or data.get("ProgramArguments") != [str(program), "daemon", mode]:
        return False
    if data.get("ProcessType") != "Interactive":
        return False
    got_env = data.get("EnvironmentVariables")
    if not isinstance(got_env, dict) or list(got_env)[:1] != ["PATH"]:
        return False
    if {k: v for k, v in got_env.items() if k != "PATH"} != dict(sorted((env or {}).items())):
        return False
    if mode == "tick":
        return data.get("Label") == label() and data.get("StartInterval") == TICK_INTERVAL_SEC
    return (data.get("Label") == wake_label() and data.get("WatchPaths") == [str(paths.DAEMON_WAKE)]
            and data.get("ThrottleInterval") == 1)


def _under_a_worktree_root(program: Path) -> bool:
    roots = [Path(os.path.expanduser(r.strip())).resolve() for r in config.worktree_roots().split(",") if r.strip()]
    resolved = Path(program).resolve()
    return any(root == resolved or root in resolved.parents for root in roots)


def _jobs() -> list[tuple[str, Path, str, str]]:
    return [(label(), plist_path(), "tick", str(TICK_INTERVAL_SEC)),
            (wake_label(), wake_plist_path(), "observe", str(paths.DAEMON_WAKE))]


def sync(*, run=None, uid: int | None = None, program: Path | None = None,
         script: Path | None = None, now: float | None = None) -> int:
    run = run or _run
    uid = os.getuid() if uid is None else uid
    program = _dockwright_bin() if program is None else program
    script = _installer() if script is None else script
    if not config.daemon_enabled():
        for name, target, _mode, _arg in _jobs():
            if target.exists() or _loaded(run, uid, name):
                run(["launchctl", "bootout", f"gui/{uid}/{name}"])
                target.unlink(missing_ok=True)
                print(f"daemon: removed {name} ([daemon] enabled = false)")
        return 0
    if _under_a_worktree_root(program):
        print(f"daemon: {program} lies under a worktree root; run daemon sync from the main clone", file=sys.stderr)
        return 1
    env = config.lanes_env()
    installed = 0
    for name, target, mode, arg in _jobs():
        if _plist_matches(target, program, mode, env) and _loaded(run, uid, name):
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(env, fh)
            env_file = fh.name
        try:
            rc = run(["bash", str(script), str(program), str(paths.DAEMON), name, mode, arg, env_file])
        finally:
            Path(env_file).unlink(missing_ok=True)
        if rc != 0:
            print(f"daemon: {script} exited {rc}; the job {name} is not installed", file=sys.stderr)
            return rc
        installed += 1
    if installed:
        ensure_start(time.time() if now is None else now)
        wake()
        print(f"daemon: installed {label()} (every {TICK_INTERVAL_SEC}s) and {wake_label()} (on {paths.DAEMON_WAKE})")
    return 0


def status(*, run=None, uid: int | None = None) -> dict:
    run = run or _run
    uid = os.getuid() if uid is None else uid
    beat = state.read_json(_file("heartbeat.json"))
    alive = state.read_json(_file("alive.json"))
    return {"label": label(), "enabled": config.daemon_enabled(), "installed": plist_path().exists(),
            "loaded": _loaded(run, uid), "wake_installed": wake_plist_path().exists(),
            "wake_loaded": _loaded(run, uid, wake_label()), "stopped": paths.DAEMON_STOP.exists(),
            "start": _recorded_start(_file("start.json")),
            "heartbeat": beat if isinstance(beat, dict) else None,
            "alive": alive if isinstance(alive, dict) else None}


def _tick_cmd(argv: list[str]) -> int:
    if argv:
        print(USAGE, file=sys.stderr)
        return 2
    return tick()


def _sync_cmd(argv: list[str]) -> int:
    if argv:
        print(USAGE, file=sys.stderr)
        return 2
    return sync()


def _status_cmd(argv: list[str]) -> int:
    if argv:
        print(USAGE, file=sys.stderr)
        return 2
    print(json.dumps(status(), indent=2))
    return 0


def _observe_cmd(argv: list[str]) -> int:
    if argv:
        print(USAGE, file=sys.stderr)
        return 2
    return observe()


def _schedules_cmd(argv: list[str]) -> int:
    if argv:
        print(USAGE, file=sys.stderr)
        return 2
    from . import daemon_schedule
    for line in daemon_schedule.describe(time.time()):
        print(line)
    return 0


def _shadow_cmd(argv: list[str]) -> int:
    print("shadow-report was removed after the shadow week; its data stays under daemon/shadow/", file=sys.stderr)
    return 2


COMMANDS = {"tick": _tick_cmd, "observe": _observe_cmd, "sync": _sync_cmd, "status": _status_cmd,
            "schedules": _schedules_cmd, "shadow-report": _shadow_cmd}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in COMMANDS:
        print(USAGE, file=sys.stderr)
        return 2
    return COMMANDS[argv[0]](argv[1:])
