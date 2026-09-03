from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from . import config, identity, lane_io, paths, state
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


def run_done_scan(mgr: dict | None = None) -> None:
    mgr = mgr or _resolve()
    name = mgr["name"]
    lane_io.preflight()
    if _manager_limited(name):
        lane_io.write_heartbeat(name, "done", emitted=False)
        return
    target_dir = paths.DONE / name
    target_dir.mkdir(parents=True, exist_ok=True)
    seen_path = _seen_file("done", name)
    seen = _load_seen(seen_path)
    printed = 0
    new_paths: list[Path] = []
    for entry in sorted(target_dir.glob("*.json")):
        if str(entry) in seen:
            continue
        try:
            payload = json.loads(entry.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            print(f"monitor: dropped unparseable done event {entry.name}",
                  file=sys.stderr)
            new_paths.append(entry)
            continue
        except OSError:
            continue
        worker = payload.get("worker_name") or payload.get("claude_sid", "?")
        summary = payload.get("summary", "")
        emit(f"{worker} done: {summary}")
        new_paths.append(entry)
        printed += 1
    _append_seen(seen_path, new_paths)
    if printed:
        _drain_notify_outbox(name)
    lane_io.write_heartbeat(name, "done", emitted=bool(printed))


TURN_END_GRACE_SEC_DEFAULT = 120
DONE_FRESH_LOOKBACK_SEC = 600
RETASK_GRACE_SEC = 2.0

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


def _turn_end_grace_sec() -> int:
    from .transcript import delegation_fresh_sec
    return delegation_fresh_sec()


def _episode_grace_sec() -> int:
    from .transcript import episode_grace_sec
    return episode_grace_sec()


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
        if not record or record.get("worker_sid") != sid:
            continue
        asked_at = record.get("asked_at")
        if isinstance(asked_at, (int, float)) and \
                _predates_current_episode(asked_at, episode_start):
            continue
        return True
    return False


def _delegation_hold(record: dict, sid: str, turn_end_ts: float, now: float) -> bool:
    try:
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


def classify_turn_end(payload: dict, entry: Path, manager_name: str,
                      own_sid: str | None, now: float) -> str:
    try:
        sid = payload.get("sid") or entry.name.rsplit("-", 1)[0]
        if own_sid and sid == own_sid:
            return TURN_END_SUPPRESS
        if payload.get("agent") == "manager":
            return TURN_END_SUPPRESS
        ts = _turn_end_ts(payload, entry)
        if ts <= 0:
            return TURN_END_PENDING
        if now - ts < _turn_end_grace_sec():
            return TURN_END_PENDING
        prior_ts = 0.0
        for sibling in entry.parent.glob(f"{sid}-*.json"):
            if sibling == entry:
                continue
            sibling_payload = state.read_json(sibling) or {}
            sibling_ts = _turn_end_ts(sibling_payload, sibling)
            if sibling_ts > ts:
                return TURN_END_SUPPRESS
            prior_ts = max(prior_ts, sibling_ts)
        record = state.read_json(paths.ACTIVE / f"{sid}.json")
        episode_start = _episode_start(record)
        if _has_fresh_done_event(manager_name, sid, ts, episode_start):
            return TURN_END_SUPPRESS
        if _has_pending_question_for_sid(sid, episode_start):
            return TURN_END_SUPPRESS
        if record is None:
            return TURN_END_EMIT_EXITED
        if record.get("nested"):
            return TURN_END_SUPPRESS
        if record.get("state") == "processing":
            return TURN_END_SUPPRESS
        if _delegation_hold(record, sid, ts, now):
            return TURN_END_PENDING
        if prior_ts > 0 and ts - prior_ts <= _episode_grace_sec() \
                and now - ts < _episode_grace_sec():
            return TURN_END_PENDING
        return TURN_END_EMIT
    except Exception as e:
        print(f"monitor: turn-end classification failed for {entry} ({e})",
              file=sys.stderr)
        return TURN_END_SUPPRESS


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
    suffix = " (session exited)" if verdict == TURN_END_EMIT_EXITED else ""
    summary = (_resolve_live_summary(payload) or payload.get("last_summary") or "").strip().replace("\n", " ")
    if len(summary) > SILENT_FINISH_SUMMARY_MAX:
        summary = summary[:SILENT_FINISH_SUMMARY_MAX - 1] + "…"
    line = f"FINISHED_SILENTLY {display}{suffix}"
    if summary:
        line += f": {summary}"
    return line


def run_turn_ends_scan(mgr: dict | None = None) -> None:
    mgr = mgr or _resolve()
    name = mgr["name"]
    lane_io.preflight()
    if _manager_limited(name):
        lane_io.write_heartbeat(name, "turn-ends", emitted=False)
        return
    own_sid = mgr["sid"]
    target_dir = paths.TURN_ENDS / name
    target_dir.mkdir(parents=True, exist_ok=True)
    seen_path = _seen_file("turn-ends", name)
    seen = _load_seen(seen_path)
    now = time.time()
    ladder_path = _fs_ladder_path(name)
    ladder = _load_fs_ladder(ladder_path)
    ladder_dirty = _prune_fs_ladder(ladder, now)
    printed = 0
    new_paths: list[Path] = []
    for entry in sorted(target_dir.glob("*.json")):
        if str(entry) in seen:
            continue
        try:
            payload = json.loads(entry.read_text())
        except (OSError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        verdict = classify_turn_end(payload, entry, name, own_sid, now)
        if verdict == TURN_END_PENDING:
            continue
        if verdict in (TURN_END_EMIT, TURN_END_EMIT_EXITED):
            sid = payload.get("sid") or entry.name.rsplit("-", 1)[0]
            gate = _fs_ladder_gate(ladder, sid, verdict, name, now)
            if gate == FS_HOLD:
                continue
            emit(_format_silent_finish_line(payload, entry, verdict))
            printed += 1
            _fs_ladder_record(ladder, sid, verdict, gate, now)
            ladder_dirty = True
        new_paths.append(entry)
    if ladder_dirty:
        try:
            state.write_json_atomic(ladder_path, ladder)
        except OSError as e:
            print(f"monitor: failed to write {ladder_path} ({e})", file=sys.stderr)
    _append_seen(seen_path, new_paths)
    if printed:
        _drain_notify_outbox(name)
    lane_io.write_heartbeat(name, "turn-ends", emitted=bool(printed))


def run_questions_scan(mgr: dict | None = None) -> None:
    mgr = mgr or _resolve()
    name = mgr["name"]
    lane_io.preflight()
    if _manager_limited(name):
        lane_io.write_heartbeat(name, "questions", emitted=False)
        return
    target_dir = paths.question_dir_for(name)
    target_dir.mkdir(parents=True, exist_ok=True)
    seen_path = _seen_file("questions", name)
    seen = _load_seen(seen_path)
    printed = 0
    new_paths: list[Path] = []
    for entry in sorted(target_dir.glob("*.json")):
        if str(entry) in seen:
            continue
        try:
            payload = json.loads(entry.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            print(f"monitor: dropped unparseable question {entry.name}",
                  file=sys.stderr)
            new_paths.append(entry)
            continue
        except OSError:
            continue
        worker = payload.get("worker_name") or payload.get("worker_sid", "?")
        question = payload.get("question", "")
        emit(f"{worker} asks: {question}")
        new_paths.append(entry)
        printed += 1
    _append_seen(seen_path, new_paths)
    if printed:
        _drain_notify_outbox(name)
    lane_io.write_heartbeat(name, "questions", emitted=bool(printed))


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


_MONITOR_SUBCOMMANDS = tuple(lane_io.LANES)

_SCANS = {
    "questions": "run_questions_scan",
    "done": "run_done_scan",
    "turn-ends": "run_turn_ends_scan",
    "stale": "run_stale_scan",
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
