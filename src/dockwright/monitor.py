"""One-shot monitor scans for the Claude Code Monitor task to wrap in a loop.

Each subcommand is invoked from a long-running Monitor task like:

    while true; do dockwright monitor done; sleep 2; done

Identity is resolved per-scan via identity.resolve_manager() — cheap; lets
the Monitor command be a literal one-liner (no substitution of name/sid).

Questions, done, turn-ends, and stale scans live here so every trigger resolves
the owning manager before emitting.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from . import config, identity, paths, state


# Resolves once per scan in the public entry points; tests can monkey-patch.
def _resolve() -> dict:
    return identity.resolve_manager()


def _seen_file(kind: str, manager_name: str) -> Path:
    """Where to persist the SEEN file across one-shot invocations.

    Production layout: ~/.claude/dockwright/.seen-<kind>-<manager-name>.
    Per-manager so two managers' scans don't compete.

    Resolved dynamically (function call) so that tests which monkeypatch
    paths.ROOT see the updated location.
    """
    return paths.ROOT / f".seen-{kind}-{manager_name}"


# The flag is fail-closed and its only writer is the sleep-60 stale loop; if
# that loop dies mid-outage the flag would hold events forever. The stale loop
# refreshes the flag mtime on every limited scan, so an mtime older than this
# means the loop is dead — fail open.
MANAGER_LIMITED_FLAG_TTL_SEC = 600


def _manager_limited(manager_name: str) -> bool:
    """True while stale_monitor's scoped scan flags the owning manager as
    bricked on a rate-limit banner (flag file lifecycle lives there). Held
    scans print nothing and mark nothing seen — every line would be a
    task-notification the bricked manager can't act on — so events replay in
    full on the first scan after the flag clears. Sanitization mirrors
    stale_monitor._limited_flag_path. A flag whose mtime is past the TTL means
    the writer loop died: ignore it and best-effort unlink so the manager is
    never permanently deaf."""
    safe = manager_name.replace("/", "_").replace("\\", "_")
    flag = paths.ROOT / f".manager-limited-{safe}"
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
    # One-release compat: pre-rename cursors carry absolute paths under the old
    # state root; normalize so migrated events aren't replayed. deprecated, one release.
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


# stale_monitor's scoped scan diverts informational lines (today: AUTOCLOSED)
# into notify-outbox/<manager>/ instead of paging a dedicated wake. Any scan
# that is already printing drains the outbox into the same stdout burst — the
# Monitor harness batches lines printed within ~200ms into one notification,
# so piggybacked lines cost zero extra manager turns. Writer-side mechanics
# (divert, fallback-to-print, 30min timeout flush) live in stale_monitor.py;
# the entry schema is the cross-file contract:
# {"line": str, "kind": str, "buffered_at": epoch}.


def _drain_notify_outbox(manager_name: str) -> None:
    """Print-then-unlink every buffered entry. At-least-once by construction:
    a crash between print and unlink replays the entry next drain, and a
    FileNotFoundError means a concurrent drainer already delivered it — skip.
    An undecodable entry is unlinked (with a stderr note) so it can never
    block entries sorted after it; the durable closed/<sid>.json record
    remains the fallback source for what it described."""
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
                print(line)
            entry.unlink(missing_ok=True)
    except Exception as e:
        print(f"monitor: outbox drain failed ({e})", file=sys.stderr)


def run_done_scan() -> None:
    """One-shot: emit any new done/<manager>/*.json files; persist SEEN."""
    mgr = _resolve()
    name = mgr["name"]
    if _manager_limited(name):
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
        new_paths.append(entry)
        try:
            payload = json.loads(entry.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        worker = payload.get("worker_name") or payload.get("claude_sid", "?")
        summary = payload.get("summary", "")
        print(f"{worker} done: {summary}")
        printed += 1
    _append_seen(seen_path, new_paths)
    if printed:
        _drain_notify_outbox(name)


# Silent-finish detection over turn-end events. A worker that finishes its
# work WITHOUT calling worker_done leaves only a turn-end behind; everything
# else a turn-end can mean (worker reported done, worker kept working, worker
# is asking a question) already has its own notification lane — so routine
# turn-ends never reach the manager. ~95% of raw per-turn pings were discarded
# as noise; the silent-finish case is the signal worth a wake-up. A turn-end
# whose worker has a subagent transcript growing past it is a delegation —
# held as PENDING with the grace aged from the newest subagent write.
# Documentation constant: the live default is transcript.DELEGATION_FRESH_SEC
# (same 120), read via _turn_end_grace_sec → transcript.delegation_fresh_sec.
TURN_END_GRACE_SEC_DEFAULT = 120
# worker_done fires DURING the final turn (an MCP call before the Stop hook),
# so a done event normally predates its turn-end by seconds-to-minutes. The
# lookback tolerates post-done cleanup work inside the same turn — keep it
# MINUTES-scale: a suppressed turn-end is marked seen, so a too-wide lookback
# doesn't delay a re-tasked worker's silent finish, it masks it forever
# (caught by the #62 verifier at the original 1h). Cleanup running past the
# lookback costs one spurious FINISHED_SILENTLY whose summary shows the done.
DONE_FRESH_LOOKBACK_SEC = 600

TURN_END_PENDING = "pending"        # within grace — re-evaluate next scan
TURN_END_SUPPRESS = "suppress"      # routine — mark seen, never surface
TURN_END_EMIT = "emit"              # probable silent finish
TURN_END_EMIT_EXITED = "emit-exited"  # silent finish AND the session is gone

# The silent-finish line is the actionable wake signal; widen past the old 160
# so a multi-line pause status's substance survives. The live re-read uses it
# too; the marker-fallback text was already baked at last_assistant_summary's
# 200 default, so fallback lines stay <=200 (the list_workers/statusline cap).
SILENT_FINISH_SUMMARY_MAX = 400


# FINISHED_SILENTLY per-sid emit ladder. A worker in a poll/wait loop ends a
# turn every ~10min; each lull past grace re-paged the manager (observed
# 14/day from one worker; >=64% of the lane false). First FS of a lull pages
# immediately; repeats for the SAME uninterrupted lull are HELD — not marked
# seen, so the newest turn-end always eventually surfaces — until a doubling
# rung matures. The rung is hard-capped: preflight prunes turn-end files at
# 24h, so an uncapped ladder could out-wait its own evidence and lose a held
# genuine finish. Resets to immediate when the manager re-engaged since the
# last page (processing_since — stamped only by real prompt submissions,
# never by task-notification wakes, so a poll loop cannot reset itself), when
# a done event for the sid landed after the last page, or when the session
# exited mid-lull (new information, page once).
FS_LADDER_BASE_SEC_DEFAULT = 900
FS_LADDER_RUNG_CAP_SEC = 4 * 3600
FS_LADDER_ENTRY_TTL_SEC = 48 * 3600

FS_EMIT_RESET = "emit-reset"   # new episode — emit now, level restarts at 1
FS_EMIT_RUNG = "emit-rung"     # same lull, rung matured — emit, level += 1
FS_HOLD = "hold"               # same lull, rung pending — not printed, NOT seen


def _turn_end_grace_sec() -> int:
    # Thin delegate: transcript.delegation_fresh_sec owns the env read so the
    # classifier grace and the read-side surfaces (list_workers, paint) cannot
    # drift apart under a CLAUDE_ORCH_TURN_END_GRACE_SEC override.
    from .transcript import delegation_fresh_sec
    return delegation_fresh_sec()


def _turn_end_ts(payload: dict, entry: Path) -> float:
    ts = payload.get("completed_at")
    if isinstance(ts, (int, float)) and ts > 0:
        return float(ts)
    try:
        return entry.stat().st_mtime
    except OSError:
        return 0.0


def _has_fresh_done_event(manager_name: str, sid: str, turn_end_ts: float) -> bool:
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
        if done_ts >= turn_end_ts - DONE_FRESH_LOOKBACK_SEC:
            return True
    return False


def _has_pending_question_for_sid(sid: str) -> bool:
    if not paths.QUESTIONS.is_dir():
        return False
    for question_path in paths.QUESTIONS.rglob("*.json"):
        record = state.read_json(question_path)
        if record and record.get("worker_sid") == sid:
            return True
    return False


def _delegation_hold(record: dict, sid: str, turn_end_ts: float, now: float) -> bool:
    """A worker that dispatched a background subagent ends its TURN but not
    its WORK (4 false FINISHED_SILENTLY in one day, 2026-06-12). Hold while
    the newest subagent transcript write is at/after the turn-end AND fresh
    within grace — the hold ages from the newest WRITE, so a dead subagent
    goes quiet past grace and the alert still fires once. Crash-proof: any
    failure reads as no-hold (pre-change behavior); the caller's outer
    try/except stays the last resort. Deliberately NOT transcript.is_delegating:
    the baseline here is the turn-end ts, not the main-log mtime (the grace
    itself is shared — _turn_end_grace_sec delegates to
    transcript.delegation_fresh_sec)."""
    try:
        if (record.get("runtime") or "claude") != "claude":
            return False
        from .transcript import find_session_log, latest_subagent_mtime
        log = find_session_log(sid)
        if log is None:
            return False
        latest = latest_subagent_mtime(log, sid)
        return latest >= turn_end_ts and now - latest < _turn_end_grace_sec()
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
    # Sanitized like _event_bucket / .stale-emitted-<mgr>.json. The sibling
    # .seen-<kind>-<mgr> cursors deliberately keep RAW names — do not "fix"
    # them to match; that would orphan live cursors.
    safe = manager_name.replace("/", "_").replace("\\", "_")
    return paths.ROOT / f".fs-emitted-{safe}.json"


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
    """FS_EMIT_RESET / FS_EMIT_RUNG / FS_HOLD for one EMIT-classified turn-end.

    Crash-proof like classify_turn_end, but failing OPEN to emission: a
    duplicate page beats a silenced lull."""
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
    """Classify one turn-end file for the silent-finish detector.

    Drives the Claude-manager monitor scan's turn-end notification lane.
    Crash-proof: any failure reads as SUPPRESS (a lost wake-up beats a crashed
    scan; the 2h idle autoclose remains the catch-all).
    """
    try:
        sid = payload.get("sid") or entry.name.rsplit("-", 1)[0]
        if own_sid and sid == own_sid:
            return TURN_END_SUPPRESS
        if payload.get("agent") == "manager":
            return TURN_END_SUPPRESS
        ts = _turn_end_ts(payload, entry)
        if ts <= 0:
            # No payload timestamp and stat failed — the file was likely
            # pruned between glob and read. ts=0 would sail past the grace
            # and emit a spurious exited-line; PENDING self-resolves (a gone
            # file isn't globbed next scan).
            return TURN_END_PENDING
        if now - ts < _turn_end_grace_sec():
            return TURN_END_PENDING
        # Superseded by a newer turn-end for the same sid: that file carries
        # the current lull; emitting both would double-page one silence.
        for sibling in entry.parent.glob(f"{sid}-*.json"):
            if sibling == entry:
                continue
            sibling_payload = state.read_json(sibling) or {}
            if _turn_end_ts(sibling_payload, sibling) > ts:
                return TURN_END_SUPPRESS
        # Accepted edge: done/ files are pruned at 24h. After a >24h manager
        # outage a surviving unseen turn-end can lose its done file and fire
        # one spurious FINISHED_SILENTLY on recovery — harmless next to the
        # outage itself, and the line's summary shows the work completed.
        if _has_fresh_done_event(manager_name, sid, ts):
            return TURN_END_SUPPRESS
        if _has_pending_question_for_sid(sid):
            return TURN_END_SUPPRESS       # the questions monitor owns that wake
        record = state.read_json(paths.ACTIVE / f"{sid}.json")
        if record is None:
            return TURN_END_EMIT_EXITED
        if record.get("nested"):
            return TURN_END_SUPPRESS       # parent process supervises it
        if record.get("state") == "processing":
            return TURN_END_SUPPRESS       # worker continued
        if _delegation_hold(record, sid, ts, now):
            return TURN_END_PENDING        # background subagent still working
        return TURN_END_EMIT
    except Exception as e:
        print(f"monitor: turn-end classification failed for {entry} ({e})",
              file=sys.stderr)
        return TURN_END_SUPPRESS


def _resolve_live_summary(payload: dict) -> str | None:
    """True last assistant message from the worker's LIVE transcript.

    By emit time the turn-end is >= grace old, so the transcript has flushed
    past the Stop-hook snapshot — which can freeze on a mid-turn narration when
    the turn's final text frame lands after the hook reads (the staleness bug).
    Reached only for EMIT/EMIT_EXITED, i.e. the worker is idle or gone, never
    mid-new-turn (a resumed worker reads state=processing -> SUPPRESS upstream),
    so the live last message is the relevant turn's final message. Returns None
    on any failure so the caller falls back to the marker's last_summary."""
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


def run_turn_ends_scan() -> None:
    """One-shot: silent-finish detector over new turn-end files.

    A turn-end is held (NOT marked seen) until it is GRACE old, then
    classified — suppressed when the worker reported done, kept working, has
    a pending question, is nested, or is the manager itself; held while a
    background subagent still writes (the delegation hold); emitted as
    `FINISHED_SILENTLY <name>: <summary>` otherwise (with a `(session
    exited)` variant when the active record is gone). Routine turn-ends
    never reach the manager. Repeats for the same uninterrupted lull are
    rate-limited by the per-sid emit ladder (HELD, not seen-marked, until a
    doubling rung matures — see the FS_LADDER constants). A scan that pages
    also drains the notify outbox into the same burst."""
    mgr = _resolve()
    name = mgr["name"]
    if _manager_limited(name):
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
                # Same uninterrupted lull, rung pending: not printed and NOT
                # marked seen — the newest turn-end keeps carrying the lull,
                # re-evaluated every scan until the rung matures or a reset
                # (re-instruction / done / session-exit) fires.
                continue
            print(_format_silent_finish_line(payload, entry, verdict))
            printed += 1
            _fs_ladder_record(ladder, sid, verdict, gate, now)
            ladder_dirty = True
        new_paths.append(entry)
    # Ordering (spec I4): print -> ladder write -> seen append. Every crash
    # window between the steps degrades to a rate-limited duplicate page,
    # never a silenced lull.
    if ladder_dirty:
        try:
            state.write_json_atomic(ladder_path, ladder)
        except OSError as e:
            print(f"monitor: failed to write {ladder_path} ({e})", file=sys.stderr)
    _append_seen(seen_path, new_paths)
    if printed:
        _drain_notify_outbox(name)


def run_questions_scan() -> None:
    """One-shot: emit any new questions/<manager>/*.json files; persist SEEN."""
    mgr = _resolve()
    name = mgr["name"]
    if _manager_limited(name):
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
        new_paths.append(entry)
        try:
            payload = json.loads(entry.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        worker = payload.get("worker_name") or payload.get("worker_sid", "?")
        question = payload.get("question", "")
        print(f"{worker} asks: {question}")
        printed += 1
    _append_seen(seen_path, new_paths)
    if printed:
        _drain_notify_outbox(name)


def run_stale_scan() -> None:
    """One-shot: run the packaged stale monitor with the resolved manager name.

    Runs `sys.executable -m dockwright.stale_monitor` — a fresh
    interpreter, same as the old deployed-script shell-out, so module-level
    env reads stay per-scan. Output flows straight through (STALE_PROCESSING /
    STALE_QUESTION / AUTOCLOSED lines on stdout). Errors surface via stderr;
    non-zero exit propagates.
    """
    mgr = _resolve()
    result = subprocess.run(
        [sys.executable, "-m", "dockwright.stale_monitor",
         "--manager", mgr["name"]],
        capture_output=False, check=False,
    )
    if result.returncode != 0:
        sys.exit(result.returncode)


def main(argv: list[str]) -> None:
    """Dispatch for `dockwright monitor <subcommand>`."""
    if not argv:
        print("Usage: dockwright monitor <questions|done|turn-ends|stale>",
              file=sys.stderr)
        sys.exit(2)
    sub = argv[0]
    if sub == "questions":
        run_questions_scan()
    elif sub == "done":
        run_done_scan()
    elif sub == "turn-ends":
        run_turn_ends_scan()
    elif sub == "stale":
        run_stale_scan()
    else:
        print(f"Unknown monitor subcommand: {sub!r}. "
              f"Try questions | done | turn-ends | stale.", file=sys.stderr)
        sys.exit(2)
