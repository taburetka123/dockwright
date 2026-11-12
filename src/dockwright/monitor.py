from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from . import config, identity, lane_io, lane_journal, lane_lines, paths, registry, state
from .lane_io import LaneDead, emit


def _resolve() -> dict:
    return identity.resolve_manager()


def _resolve_named(name: str) -> dict:
    records = identity._list_manager_records()
    matches = [r for r in records if r.get("name") == name]
    if len(matches) == 1:
        return {"name": matches[0]["name"], "sid": matches[0]["claude_sid"]}
    if len(matches) > 1:
        print(
            f"dockwright monitor: name {name!r} is ambiguous "
            f"({len(matches)} active manager records match).",
            file=sys.stderr,
        )
        sys.exit(2)
    names = sorted(r.get("name", "?") for r in records)
    print(
        f"dockwright monitor: no active manager record named {name!r}. "
        f"Active managers: {names}.",
        file=sys.stderr,
    )
    sys.exit(2)


def _seen_file(kind: str, manager_name: str) -> Path:
    return paths.ROOT / f".seen-{kind}-{manager_name}"


MANAGER_LIMITED_FLAG_TTL_SEC = 600


def _manager_limited(manager_name: str) -> bool:
    flag = paths.ROOT / f".manager-limited-{paths._event_bucket(manager_name)}"
    try:
        age = time.time() - flag.stat().st_mtime
    except OSError:
        return False
    if age > MANAGER_LIMITED_FLAG_TTL_SEC:
        try:
            flag.unlink()
        except OSError:
            pass
        return False
    return True


def _load_seen(seen_path: Path) -> set[str]:
    if not seen_path.exists():
        return set()
    lines = {line.rstrip("\n") for line in seen_path.read_text().splitlines() if line}
    legacy_prefix = str(config.legacy_state_root()) + "/"
    new_prefix = str(paths.ROOT) + "/"
    return {
        new_prefix + line[len(legacy_prefix):] if line.startswith(legacy_prefix) else line
        for line in lines
    }


def _append_seen(seen_path: Path, new_paths: list[Path]) -> None:
    if not new_paths:
        return
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    with seen_path.open("a") as f:
        for p in new_paths:
            f.write(f"{p}\n")


def _drain_notify_outbox(manager_name: str) -> None:
    try:
        outbox = paths.notify_outbox_dir_for(manager_name)
        if not outbox.is_dir():
            return
        for entry in sorted(outbox.glob("*.json")):
            try:
                payload = json.loads(entry.read_text())
            except FileNotFoundError:
                continue
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                print(f"monitor: dropped undecodable outbox entry {entry.name}",
                      file=sys.stderr)
                entry.unlink(missing_ok=True)
                continue
            line = payload.get("line") if isinstance(payload, dict) else None
            if isinstance(line, str) and line:
                emit(line)
            entry.unlink(missing_ok=True)
    except LaneDead:
        raise
    except Exception as e:
        print(f"monitor: outbox drain failed ({e})", file=sys.stderr)


def _task_of(sid: object) -> str | None:
    if not isinstance(sid, str) or not sid:
        return None
    try:
        assignment = state.read_json(paths.assignment_path(sid))
    except Exception:
        return None
    task_key = assignment.get("ticket") if isinstance(assignment, dict) else None
    return task_key if isinstance(task_key, str) and task_key else None


def _handed_off_task(sid: str) -> str | None:
    try:
        task_key = _task_of(sid)
        if task_key is None:
            return None
        from . import mcp_server
        return task_key if mcp_server._handed_off(sid, task_key) else None
    except Exception:
        return None


def _advanced_node(payload: dict) -> str | None:
    sid = payload.get("claude_sid")
    if not isinstance(sid, str) or not sid:
        return None
    task_key = _handed_off_task(sid)
    if task_key is None:
        return None
    try:
        from . import mcp_server, pipeline
        assignment = state.read_json(paths.assignment_path(sid))
        aid = assignment.get("assignment_id") if isinstance(assignment, dict) else None
        events = mcp_server._read_events(paths.artifact_events_path(task_key))
        requests = pipeline.fold(events).open_requests
    except Exception:
        return None

    def own(event) -> bool:
        return event.get("actor_sid") == sid or bool(aid and event.get("actor_assignment") == aid)

    if any(own(r) for r in requests):
        return None
    committed = {e.get("request_id") for e in events if e.get("type") == "advance_committed"}
    if any(own(e) and (e.get("type") == "token_retired"
                       or (e.get("type") == "advance_requested" and e.get("request_id") in committed))
           for e in events):
        return task_key
    return None


PARK_PREFIX = "PARKED"


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _bare_park(summary: object) -> str | None:
    if not isinstance(summary, str) or "\n" in summary.strip():
        return None
    line = _collapse(summary)
    return line if line.startswith(PARK_PREFIX) else None


def _first_line(summary: object) -> str:
    return _collapse(str(summary).strip().split("\n", 1)[0])


def _done_time(entry: Path, payload: dict) -> float | None:
    done_ts = payload.get("completed_at")
    if isinstance(done_ts, (int, float)):
        return done_ts
    try:
        return entry.stat().st_mtime
    except OSError:
        return None


def _is_stamp(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _answers_his_pane_line(payload: dict, previous_ts: float) -> bool:
    typed_at, tasked_at = payload.get("engineer_input_at"), payload.get("tasked_at")
    return _is_stamp(typed_at) and not (_is_stamp(tasked_at) and tasked_at > previous_ts)


def _repeats_previous_park(entry: Path, payload: dict) -> bool:
    summary = payload.get("summary", "")
    sid = payload.get("claude_sid")
    if not isinstance(summary, str) or not isinstance(sid, str) or not sid:
        return False
    line = _first_line(summary)
    if not line.startswith(PARK_PREFIX):
        return False
    done_ts = _done_time(entry, payload)
    if done_ts is None:
        return False
    previous, previous_ts = None, None
    for sibling in entry.parent.glob(f"{sid}-*.json"):
        if sibling == entry:
            continue
        sibling_payload = state.read_json(sibling)
        if not isinstance(sibling_payload, dict) or sibling_payload.get("claude_sid") != sid:
            continue
        sibling_ts = _done_time(sibling, sibling_payload)
        if sibling_ts is None or sibling_ts >= done_ts:
            continue
        if previous_ts is None or sibling_ts > previous_ts:
            previous, previous_ts = sibling_payload, sibling_ts
    if previous is None or _first_line(previous.get("summary", "")) != line:
        return False
    return _bare_park(summary) is not None or _answers_his_pane_line(payload, previous_ts)


def _journal_done(task_key: str | None, worker: str, event_id, write: bool, reason: str = "advanced") -> bool:
    if not task_key or not lane_lines.journals("DONE"):
        return False
    try:
        if not write:
            return lane_journal.would_journal(task_key)
        return lane_journal.journal_once(task_key, "DONE", {"worker": worker, "event_id": event_id, reason: True})
    except (OSError, ValueError):
        return False


SILENT_FINISH_GRACE_SEC_DEFAULT = 1800
SILENT_FINISH_GRACE_ENV = "CLAUDE_ORCH_SILENT_FINISH_GRACE_SEC"
DONE_FRESH_LOOKBACK_SEC = 600
RETASK_GRACE_SEC = 2.0
TURN_END_BUSY_HOLD_SEC_DEFAULT = 7200

TURN_END_PENDING = "pending"
TURN_END_SUPPRESS = "suppress"
TURN_END_EMIT = "emit"
TURN_END_EMIT_EXITED = "emit-exited"

SILENT_FINISH_SUMMARY_MAX = 400


FS_LADDER_BASE_SEC_DEFAULT = 900
FS_LADDER_RUNG_CAP_SEC = 4 * 3600
FS_LADDER_ENTRY_TTL_SEC = 48 * 3600

FS_EMIT_RESET = "emit-reset"
FS_EMIT_RUNG = "emit-rung"
FS_HOLD = "hold"

DAEMON_SILENT_AFTER_SEC = 180
DAEMON_STUCK_AFTER_SEC = 600
DAEMON_SILENT_LADDER_MIN = (10, 20, 40)
DAEMON_FIRST_STAMP_GRACE_SEC = 600
CONFIG_ERROR_REPEAT_SEC = 600
OUTBOX_MAX_HOLD_SEC = 1800
URGENT_SOURCES = ("daemon", "board")


def _silent_finish_grace_sec() -> int:
    try:
        value = int(os.environ.get(SILENT_FINISH_GRACE_ENV, str(SILENT_FINISH_GRACE_SEC_DEFAULT)))
    except ValueError:
        return SILENT_FINISH_GRACE_SEC_DEFAULT
    return value if value >= 0 else SILENT_FINISH_GRACE_SEC_DEFAULT


def _episode_grace_sec() -> int:
    from .transcript import episode_grace_sec
    return episode_grace_sec()


def _turn_end_sid(payload: dict, entry: Path) -> str:
    return payload.get("sid") or entry.name.rsplit("-", 1)[0]


def _turn_end_ts(payload: dict, entry: Path) -> float:
    ts = payload.get("completed_at")
    if isinstance(ts, (int, float)) and ts > 0:
        return float(ts)
    try:
        return entry.stat().st_mtime
    except OSError:
        return 0.0


def _episode_start(record: dict | None) -> float:
    if not isinstance(record, dict):
        return 0.0
    stamps = [record.get("tasked_at"), record.get("processing_since")]
    return max((s for s in stamps if isinstance(s, (int, float))), default=0.0)


def _predates_current_episode(event_ts: float, episode_start: float) -> bool:
    return episode_start > 0 and event_ts < episode_start - RETASK_GRACE_SEC


def _has_fresh_done_event(manager_name: str, sid: str, turn_end_ts: float,
                          episode_start: float) -> bool:
    done_dir = paths.DONE / manager_name
    if not done_dir.is_dir():
        return False
    for done_path in done_dir.glob(f"{sid}-*.json"):
        done_payload = state.read_json(done_path) or {}
        if not isinstance(done_payload, dict):
            continue
        done_ts = done_payload.get("completed_at")
        if not isinstance(done_ts, (int, float)):
            try:
                done_ts = done_path.stat().st_mtime
            except OSError:
                continue
        if done_ts < turn_end_ts - DONE_FRESH_LOOKBACK_SEC:
            continue
        if _predates_current_episode(done_ts, episode_start):
            continue
        return True
    return False


def _has_pending_question_for_sid(sid: str, episode_start: float) -> bool:
    if not paths.QUESTIONS.is_dir():
        return False
    for question_path in paths.QUESTIONS.rglob("*.json"):
        record = state.read_json(question_path)
        if not isinstance(record, dict) or record.get("worker_sid") != sid or registry.is_engineer_ask(record):
            continue
        asked_at = record.get("asked_at")
        if isinstance(asked_at, (int, float)) and \
                _predates_current_episode(asked_at, episode_start):
            continue
        return True
    return False


def _delegation_hold(record: dict, sid: str, turn_end_ts: float, now: float) -> bool:
    try:
        if state.has_live_child(record):
            return True
        if (record.get("runtime") or "claude") != "claude":
            return False
        from .transcript import find_session_log, latest_subagent_mtime
        log = find_session_log(sid)
        if log is None:
            return False
        latest = latest_subagent_mtime(log, sid)
        return latest >= turn_end_ts and now - latest < _episode_grace_sec()
    except Exception as e:
        print(f"monitor: delegation check failed for {sid} ({e})", file=sys.stderr)
        return False


def _turn_end_busy_hold_sec() -> int:
    raw = os.environ.get("CLAUDE_ORCH_TURN_END_BUSY_SEC", "")
    try:
        value = int(raw)
    except ValueError:
        return TURN_END_BUSY_HOLD_SEC_DEFAULT
    return value if value > 0 else TURN_END_BUSY_HOLD_SEC_DEFAULT


def _process_index() -> dict | None:
    from .stale_monitor import _process_index as build_index
    return build_index()


def _busy_shell_hold(record: dict, turn_end_ts: float, now: float,
                     process_index) -> bool:
    if now - turn_end_ts >= _turn_end_busy_hold_sec():
        return False
    try:
        from .stale_monitor import _has_live_background_shell
        return _has_live_background_shell(record, process_index())
    except Exception as e:
        print(f"monitor: background-shell check failed for "
              f"{record.get('claude_sid')} ({e})", file=sys.stderr)
        return False


def _fs_ladder_base_sec() -> int:
    raw = os.environ.get("CLAUDE_ORCH_FS_LADDER_BASE_SEC", "")
    try:
        value = int(raw)
    except ValueError:
        return FS_LADDER_BASE_SEC_DEFAULT
    return value if value > 0 else FS_LADDER_BASE_SEC_DEFAULT


def _fs_ladder_path(manager_name: str) -> Path:
    return paths.ROOT / f".fs-emitted-{paths._event_bucket(manager_name)}.json"


def _load_fs_ladder(ladder_path: Path) -> dict:
    data = state.read_json(ladder_path)
    return data if isinstance(data, dict) else {}


def _prune_fs_ladder(ladder: dict, now: float) -> bool:
    stale_sids = [sid for sid, entry in ladder.items()
                  if not isinstance(entry, dict)
                  or not isinstance(entry.get("last_emit"), (int, float))
                  or now - entry["last_emit"] > FS_LADDER_ENTRY_TTL_SEC]
    for sid in stale_sids:
        del ladder[sid]
    return bool(stale_sids)


def _fs_rung_sec(level: int) -> float:
    exponent = max(int(level) - 1, 0)
    return min(_fs_ladder_base_sec() * (2 ** min(exponent, 32)), FS_LADDER_RUNG_CAP_SEC)


def _done_event_after(manager_name: str, sid: str, after_ts: float) -> bool:
    done_dir = paths.DONE / manager_name
    if not done_dir.is_dir():
        return False
    for done_path in done_dir.glob(f"{sid}-*.json"):
        payload = state.read_json(done_path) or {}
        ts = payload.get("completed_at")
        if not isinstance(ts, (int, float)):
            try:
                ts = done_path.stat().st_mtime
            except OSError:
                continue
        if ts > after_ts:
            return True
    return False


def _fs_ladder_gate(ladder: dict, sid: str, verdict: str,
                    manager_name: str, now: float) -> str:
    try:
        entry = ladder.get(sid)
        if not isinstance(entry, dict):
            return FS_EMIT_RESET
        last_emit = entry.get("last_emit")
        if not isinstance(last_emit, (int, float)) or last_emit <= 0:
            return FS_EMIT_RESET
        record = state.read_json(paths.ACTIVE / f"{sid}.json") or {}
        processing_since = record.get("processing_since")
        if isinstance(processing_since, (int, float)) and processing_since > last_emit:
            return FS_EMIT_RESET
        if _done_event_after(manager_name, sid, last_emit):
            return FS_EMIT_RESET
        if verdict == TURN_END_EMIT_EXITED and not entry.get("exited"):
            return FS_EMIT_RESET
        level = entry.get("level")
        level = level if isinstance(level, int) and level > 0 else 1
        if now - last_emit >= _fs_rung_sec(level):
            return FS_EMIT_RUNG
        return FS_HOLD
    except Exception as e:
        print(f"monitor: FS ladder gate failed for {sid} ({e})", file=sys.stderr)
        return FS_EMIT_RESET


def _fs_ladder_record(ladder: dict, sid: str, verdict: str, gate: str,
                      now: float) -> None:
    prior = ladder.get(sid)
    prior = prior if isinstance(prior, dict) else {}
    level = prior.get("level")
    level = level if isinstance(level, int) and level > 0 else 0
    ladder[sid] = {
        "last_emit": now,
        "level": (level + 1) if gate == FS_EMIT_RUNG else 1,
        "exited": bool(verdict == TURN_END_EMIT_EXITED
                       or (gate == FS_EMIT_RUNG and prior.get("exited"))),
    }


def _classify_turn_end(payload: dict, entry: Path, manager_name: str,
                       own_sid: str | None, now: float,
                       process_index=None) -> tuple[str, str]:
    try:
        sid = _turn_end_sid(payload, entry)
        if own_sid and sid == own_sid:
            return TURN_END_SUPPRESS, "own-sid"
        if payload.get("agent") == "manager":
            return TURN_END_SUPPRESS, "manager"
        ts = _turn_end_ts(payload, entry)
        if ts <= 0:
            return TURN_END_PENDING, "no-ts"
        if now - ts < _silent_finish_grace_sec():
            return TURN_END_PENDING, "grace"
        prior_ts = 0.0
        for sibling in entry.parent.glob(f"{sid}-*.json"):
            if sibling == entry:
                continue
            sibling_payload = state.read_json(sibling) or {}
            if not isinstance(sibling_payload, dict):
                continue
            sibling_ts = _turn_end_ts(sibling_payload, sibling)
            if sibling_ts > ts:
                return TURN_END_SUPPRESS, "newer-sibling"
            prior_ts = max(prior_ts, sibling_ts)
        record = state.read_json(paths.ACTIVE / f"{sid}.json")
        episode_start = _episode_start(record)
        if _has_fresh_done_event(manager_name, sid, ts, episode_start):
            return TURN_END_SUPPRESS, "fresh-done"
        from .stale_monitor import _input_bound
        if _has_pending_question_for_sid(sid, _input_bound(record)):
            return TURN_END_SUPPRESS, "question"
        if record is None:
            return TURN_END_EMIT_EXITED, "exited"
        if record.get("nested"):
            return TURN_END_SUPPRESS, "nested"
        from .stale_monitor import is_human_attached
        if is_human_attached(record, now):
            return TURN_END_SUPPRESS, "human-attached"
        if record.get("state") == "processing":
            return TURN_END_SUPPRESS, "processing"
        if _delegation_hold(record, sid, ts, now):
            return TURN_END_PENDING, "delegation"
        if _busy_shell_hold(
                record, ts, now,
                process_index if process_index is not None else _process_index):
            return TURN_END_PENDING, "busy-shell"
        if prior_ts > 0 and ts - prior_ts <= _episode_grace_sec() \
                and now - ts < _episode_grace_sec():
            return TURN_END_PENDING, "episode"
        return TURN_END_EMIT, "silent-finish"
    except Exception as e:
        print(f"monitor: turn-end classification failed for {entry} ({e})",
              file=sys.stderr)
        return TURN_END_SUPPRESS, "error"


def classify_turn_end_reason(payload: dict, entry: Path, manager_name: str,
                             own_sid: str | None, now: float,
                             process_index=None) -> tuple[str, str]:
    verdict, reason = _classify_turn_end(payload, entry, manager_name, own_sid, now, process_index)
    if verdict in (TURN_END_EMIT, TURN_END_EMIT_EXITED):
        sid = _turn_end_sid(payload, entry)
        if _handed_off_task(sid):
            return TURN_END_SUPPRESS, "handed-off"
    return verdict, reason


def classify_turn_end(payload: dict, entry: Path, manager_name: str,
                      own_sid: str | None, now: float,
                      process_index=None) -> str:
    return classify_turn_end_reason(payload, entry, manager_name, own_sid, now, process_index)[0]


def _resolve_live_summary(payload: dict) -> str | None:
    try:
        sid = payload.get("sid")
        if not sid:
            return None
        from .transcript import find_session_log, last_assistant_summary
        log = find_session_log(sid, runtime=payload.get("runtime") or "claude")
        if log is None:
            return None
        summary, _ = last_assistant_summary(log, max_chars=SILENT_FINISH_SUMMARY_MAX)
        return summary
    except Exception:
        return None


def _format_silent_finish_line(payload: dict, entry: Path, verdict: str) -> str:
    display = payload.get("name") or entry.name.rsplit("-", 1)[0]
    summary = (_resolve_live_summary(payload) or payload.get("last_summary") or "").strip().replace("\n", " ")
    if len(summary) > SILENT_FINISH_SUMMARY_MAX:
        summary = summary[:SILENT_FINISH_SUMMARY_MAX - 1] + "…"
    return lane_lines.render("FINISHED_SILENTLY", name=display, exited=verdict == TURN_END_EMIT_EXITED,
                             summary=summary)


OBSERVE_EMIT = "emit"
OBSERVE_SILENT = "silent"
OBSERVE_SUPPRESS = "suppress"

_OBSERVED_ROOTS = {"done": lambda: paths.DONE, "turn-ends": lambda: paths.TURN_ENDS}


class LaneScope:
    def __init__(self, mgr: dict):
        self.name = mgr["name"]
        self.sid = mgr.get("sid")

    def buckets(self, kind: str) -> list[str]:
        (_OBSERVED_ROOTS[kind]() / self.name).mkdir(parents=True, exist_ok=True)
        return [self.name]

    def entries(self, kind: str, bucket: str) -> list[Path]:
        return sorted((_OBSERVED_ROOTS[kind]() / bucket).glob("*.json"))

    def question_entries(self) -> list[tuple[Path, str]]:
        paths.question_dir_for(self.name).mkdir(parents=True, exist_ok=True)
        return [(entry, self.name) for entry in registry.manager_question_paths(self.name)]

    def load_seen(self, kind: str) -> set[str]:
        seen = _load_seen(_seen_file(kind, self.name))
        return {Path(line).stem for line in seen} if kind == "questions" else seen

    def save_seen(self, kind: str, marked: list[str]) -> None:
        _append_seen(_seen_file(kind, self.name), marked)

    def ladder_path(self, bucket: str) -> Path:
        return _fs_ladder_path(self.name)

    def own_sid(self, bucket: str) -> str | None:
        return self.sid

    def may_journal(self, entry: Path) -> bool:
        return True


class LaneSink:
    def __init__(self, mgr: dict, lane: str):
        self.name = mgr["name"]
        self.lane = lane
        self.printed = 0

    def begin(self) -> bool:
        lane_io.preflight()
        if _manager_limited(self.name):
            lane_io.write_heartbeat(self.name, self.lane, emitted=False)
            return False
        return True

    def record(self, kind: str, key: str, outcome: str, fields: dict, line: str | None) -> None:
        if outcome == OBSERVE_EMIT:
            emit(line)
            self.printed += 1

    def end(self) -> None:
        if self.printed:
            _drain_notify_outbox(self.name)
        lane_io.write_heartbeat(self.name, self.lane, emitted=bool(self.printed))


def observe_done(scope, sink) -> None:
    if not sink.begin():
        return
    seen = scope.load_seen("done")
    for bucket in scope.buckets("done"):
        marked: list[str] = []
        for entry in scope.entries("done", bucket):
            if str(entry) in seen:
                continue
            fields = {"bucket": bucket, "entry": entry, "payload": {}, "reason": "unparseable"}
            try:
                payload = json.loads(entry.read_text())
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = None
            except OSError:
                continue
            if not isinstance(payload, dict):
                print(f"monitor: dropped unparseable done event {entry.name}", file=sys.stderr)
                sink.record("DONE", entry.name, OBSERVE_SUPPRESS, fields, None)
                marked.append(str(entry))
                continue
            fields["payload"] = payload
            worker = payload.get("worker_name") or payload.get("claude_sid", "?")
            summary = payload.get("summary", "")
            task_key = _advanced_node(payload)
            repeat = task_key is None and _repeats_previous_park(entry, payload)
            line = lane_lines.render("DONE", worker=worker, summary=summary, advanced=task_key is not None,
                                     repeat=repeat)
            if line is None:
                reason = "advanced" if task_key else "repeat"
                if _journal_done(task_key or _task_of(payload.get("claude_sid")), worker, payload.get("event_id"),
                                 scope.may_journal(entry), reason):
                    sink.record("DONE", entry.name, OBSERVE_SILENT, {**fields, "reason": reason}, None)
                    marked.append(str(entry))
                    continue
                line = lane_lines.render("DONE", worker=worker, summary=summary)
            sink.record("DONE", entry.name, OBSERVE_EMIT, {**fields, "reason": "done"}, line)
            marked.append(str(entry))
        scope.save_seen("done", marked)
    sink.end()


def observe_turn_ends(scope, sink) -> None:
    if not sink.begin():
        return
    seen = scope.load_seen("turn-ends")
    now = time.time()
    index_memo: list = []

    def process_index():
        if not index_memo:
            try:
                index_memo.append(_process_index())
            except Exception as e:
                print(f"monitor: process index unavailable ({e})", file=sys.stderr)
                index_memo.append(None)
        return index_memo[0]

    for bucket in scope.buckets("turn-ends"):
        ladder_path = scope.ladder_path(bucket)
        ladder = _load_fs_ladder(ladder_path)
        ladder_dirty = _prune_fs_ladder(ladder, now)
        marked: list[str] = []
        for entry in scope.entries("turn-ends", bucket):
            if str(entry) in seen:
                continue
            try:
                payload = json.loads(entry.read_text())
            except (OSError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            verdict, reason = classify_turn_end_reason(payload, entry, bucket, scope.own_sid(bucket),
                                                       now, process_index)
            if verdict == TURN_END_PENDING:
                continue
            fields = {"bucket": bucket, "entry": entry, "payload": payload, "reason": reason}
            if verdict in (TURN_END_EMIT, TURN_END_EMIT_EXITED):
                sid = _turn_end_sid(payload, entry)
                gate = _fs_ladder_gate(ladder, sid, verdict, bucket, now)
                if gate == FS_HOLD:
                    continue
                sink.record("FINISHED_SILENTLY", entry.name, OBSERVE_EMIT, {**fields, "gate": gate},
                            _format_silent_finish_line(payload, entry, verdict))
                _fs_ladder_record(ladder, sid, verdict, gate, now)
                ladder_dirty = True
            else:
                sink.record("FINISHED_SILENTLY", entry.name, OBSERVE_SUPPRESS, fields, None)
            marked.append(str(entry))
        if ladder_dirty:
            try:
                state.write_json_atomic(ladder_path, ladder)
            except OSError as e:
                print(f"monitor: failed to write {ladder_path} ({e})", file=sys.stderr)
        scope.save_seen("turn-ends", marked)
    sink.end()


def observe_questions(scope, sink) -> None:
    if not sink.begin():
        return
    seen = scope.load_seen("questions")
    marked: list[str] = []
    for entry, bucket in scope.question_entries():
        if entry.stem in seen:
            continue
        fields = {"bucket": bucket, "entry": entry, "payload": {}, "reason": "unparseable"}
        try:
            payload = json.loads(entry.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        except OSError:
            continue
        if not isinstance(payload, dict):
            print(f"monitor: dropped unparseable question {entry.name}", file=sys.stderr)
            sink.record("QUESTION", entry.stem, OBSERVE_SUPPRESS, fields, None)
            marked.append(entry.stem)
            continue
        worker = payload.get("worker_name") or payload.get("worker_sid", "?")
        question = payload.get("question", "")
        sink.record("QUESTION", entry.stem, OBSERVE_EMIT, {**fields, "payload": payload, "reason": "asked"},
                    lane_lines.render("QUESTION", worker=worker, question=question))
        marked.append(entry.stem)
    scope.save_seen("questions", marked)
    sink.end()


def run_done_scan(mgr: dict | None = None) -> None:
    mgr = mgr or _resolve()
    observe_done(LaneScope(mgr), LaneSink(mgr, "done"))


def run_turn_ends_scan(mgr: dict | None = None) -> None:
    mgr = mgr or _resolve()
    observe_turn_ends(LaneScope(mgr), LaneSink(mgr, "turn-ends"))


def run_questions_scan(mgr: dict | None = None) -> None:
    mgr = mgr or _resolve()
    observe_questions(LaneScope(mgr), LaneSink(mgr, "questions"))


def _emit_event_line(mgr: dict, line: str, manager_name: str | None) -> None:
    if manager_name and manager_name != mgr["name"]:
        from .daemon import OutboxEmit
        OutboxEmit(manager_name).emit(line)
        return
    emit(line)


def run_stale_scan(mgr: dict | None = None) -> None:
    mgr = mgr or _resolve()
    lane_io.preflight()
    result = subprocess.run(
        [sys.executable, "-m", "dockwright.stale_monitor",
         "--manager", mgr["name"]],
        capture_output=False, check=False,
    )
    if result.returncode == lane_io.EXIT_LANE_DEAD:
        raise LaneDead("stale_monitor reported its stdout reader is gone")
    if result.returncode == lane_io.EXIT_LANE_WEDGED:
        lane_io.detach_stdout()
        sys.exit(lane_io.EXIT_LANE_WEDGED)
    if result.returncode != 0:
        raise RuntimeError(
            f"stale_monitor exited {result.returncode}")
    if _manager_limited(mgr["name"]):
        return
    try:
        from . import event_poll
        event_poll.step(time.time(), [mgr], lambda line, manager_name=None: _emit_event_line(mgr, line, manager_name))
    except LaneDead:
        raise
    except Exception as err:
        emit(lane_lines.render("EVENT_POLL_FAILED", source="*", error=f"{type(err).__name__}: {str(err)[:200]}"))
    try:
        from . import pipeline_reconcile
        pipeline_reconcile.reconcile(mgr)
    except LaneDead:
        raise
    except Exception as err:
        emit(lane_lines.render("RECONCILE_ERROR", task_key="*", error=f"{type(err).__name__}: {str(err)[:200]}"))
    try:
        from . import note_check
        note_check.run()
    except LaneDead:
        raise
    except Exception as err:
        _page_note_check_error(mgr, err)


def _daemon_silent_minutes(beat: dict) -> int | None:
    awake = beat.get("awake")
    if not isinstance(awake, (int, float)):
        return None
    silent = lane_io.awake_since(awake)
    return int(silent // 60) if silent > DAEMON_SILENT_AFTER_SEC else None


def _daemon_stuck_step(beat: dict) -> tuple[str, int] | None:
    step, since = beat.get("step"), beat.get("step_awake")
    if not isinstance(step, str) or isinstance(since, bool) or not isinstance(since, (int, float)):
        return None
    ran = lane_io.awake_since(since)
    return (step, int(ran // 60)) if ran > DAEMON_STUCK_AFTER_SEC else None


def _daemon_alive_state() -> tuple[int | None, bool, tuple[str, int] | None]:
    beat = state.read_json(paths.DAEMON / "alive.json")
    if isinstance(beat, dict):
        minutes = _daemon_silent_minutes(beat)
        return minutes, True, _daemon_stuck_step(beat) if minutes is None else None
    plist = paths.LAUNCH_AGENTS / f"{config.loop_label_prefix()}.daemon.plist"
    try:
        installed_age = time.time() - plist.stat().st_mtime
    except OSError:
        return None, False, None
    if installed_age > DAEMON_FIRST_STAMP_GRACE_SEC:
        return int(installed_age // 60), False, None
    return None, False, None


def _daemon_silent_line(prior: dict, now_awake: float) -> tuple[str | None, dict]:
    if lane_io.lanes_in_manager():
        return None, {}
    if paths.DAEMON_STOP.exists():
        if paths.PAUSE_MANIFEST.exists():
            return None, {}
        line = lane_lines.render("DAEMON_STOPPED", stop=str(paths.DAEMON_STOP))
    else:
        minutes, _, stuck = _daemon_alive_state()
        if minutes is not None:
            line = lane_lines.render("DAEMON_SILENT", minutes=minutes)
        elif stuck is not None:
            line = lane_lines.render("DAEMON_STUCK", step=stuck[0], minutes=stuck[1])
        else:
            return None, {"daemon_silent_level": 0, "daemon_silent_at": None}
    level = prior.get("daemon_silent_level")
    level = level if isinstance(level, int) and level >= 0 else 0
    last = prior.get("daemon_silent_at")
    rung = DAEMON_SILENT_LADDER_MIN[min(level - 1, len(DAEMON_SILENT_LADDER_MIN) - 1)] * 60 if level else 0
    if level and isinstance(last, (int, float)) and now_awake - last < rung:
        return None, {"daemon_silent_level": level, "daemon_silent_at": last}
    return line, {"daemon_silent_level": level + 1, "daemon_silent_at": now_awake}


def _config_error_line(prior: dict, now_awake: float) -> tuple[str | None, dict]:
    error = lane_io.lanes_mode_error()
    if error is None:
        return None, {"config_error_at": None}
    last = prior.get("config_error_at")
    if isinstance(last, (int, float)) and now_awake - last < CONFIG_ERROR_REPEAT_SEC:
        return None, {"config_error_at": last}
    return (lane_lines.render("CONFIG_UNREADABLE", path=str(config.config_path() or "dockwright.toml"), error=error),
            {"config_error_at": now_awake})


def _outbox_due(name: str) -> bool:
    outbox = paths.notify_outbox_dir_for(name)
    if not outbox.is_dir():
        return False
    oldest = None
    for entry in outbox.glob("*.json"):
        payload = state.read_json(entry)
        if isinstance(payload, dict) and payload.get("source") in URGENT_SOURCES:
            return True
        try:
            oldest = min(oldest or entry.stat().st_mtime, entry.stat().st_mtime)
        except OSError:
            continue
    return oldest is not None and time.time() - oldest >= OUTBOX_MAX_HOLD_SEC


def run_pages_scan(mgr: dict | None = None) -> None:
    mgr = mgr or _resolve()
    name = mgr["name"]
    lane_io.preflight()
    if _manager_limited(name):
        lane_io.write_heartbeat(name, "pages", emitted=False)
        return
    printed = False
    if _outbox_due(name):
        _drain_notify_outbox(name)
        printed = True
    prior = state.read_json(lane_io.heartbeat_path(name, "pages")) or {}
    prior = prior if isinstance(prior, dict) else {}
    now_awake = lane_io.awake_seconds()
    extra = {}
    for line, fields in (_config_error_line(prior, now_awake), _daemon_silent_line(prior, now_awake)):
        extra.update(fields)
        if line is not None:
            emit(line)
            printed = True
    lane_io.write_heartbeat(name, "pages", emitted=printed, extra=extra)


def _page_note_check_error(mgr: dict, err: Exception) -> None:
    from .pipeline_reconcile import _PAGE_EVERY_SEC, _Pages
    if not _Pages(mgr["name"]).throttled(("*", "note-check"), _PAGE_EVERY_SEC, time.time()):
        emit(lane_lines.render("RECONCILE_ERROR", task_key="*",
                               error=f"note-check {type(err).__name__}: {str(err)[:200]}"))


_MONITOR_SUBCOMMANDS = tuple(lane_io.LANES)

_SCANS = {
    "questions": "run_questions_scan",
    "done": "run_done_scan",
    "turn-ends": "run_turn_ends_scan",
    "stale": "run_stale_scan",
    "pages": "run_pages_scan",
}


def main(argv: list[str]) -> None:
    lanes = " | ".join(_MONITOR_SUBCOMMANDS)
    usage = f"Usage: dockwright monitor <{'|'.join(_MONITOR_SUBCOMMANDS)}> [manager-name]"
    if not argv:
        print(usage, file=sys.stderr)
        sys.exit(2)
    sub = argv[0]
    if sub not in _MONITOR_SUBCOMMANDS:
        print(f"Unknown monitor subcommand: {sub!r}. "
              f"Try {lanes}.", file=sys.stderr)
        sys.exit(2)
    if len(argv) > 2:
        print(f"Unexpected arguments {argv[2:]!r}. {usage}", file=sys.stderr)
        sys.exit(2)
    mgr = None
    try:
        mgr = _resolve_named(argv[1]) if len(argv) == 2 else _resolve()
        globals()[_SCANS[sub]](mgr)
    except LaneDead as e:
        print(f"dockwright monitor: {sub} lane is dead ({e}); ending the lane "
              f"so its Monitor task exits and the manager is told.",
              file=sys.stderr)
        lane_io.detach_stdout()
        sys.exit(lane_io.EXIT_LANE_DEAD)
    except SystemExit:
        raise
    except Exception as e:
        cap = lane_io.max_consecutive_errors(sub)
        run = lane_io.record_scan_error(
            mgr["name"] if mgr else lane_io.UNRESOLVED_BUCKET, sub)
        print(f"dockwright monitor: {sub} scan failed ({type(e).__name__}: {e}) "
              f"[{run}/{cap} consecutive]", file=sys.stderr)
        if run >= cap:
            print(f"dockwright monitor: {sub} lane wedged after {run} "
                  f"consecutive failures; ending it so the manager is told.",
                  file=sys.stderr)
            lane_io.detach_stdout()
            sys.exit(lane_io.EXIT_LANE_WEDGED)
        lane_io.detach_stdout()
        sys.exit(0)
