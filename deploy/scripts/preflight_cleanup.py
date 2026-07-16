#!/usr/bin/env python3
"""One-shot dockwright hygiene at /manager startup.

Prunes:
  - handoffs/<id>.json older than 1h with consumed_at == null  (aborted recreations)
  - handoffs/<id>.json older than 24h with consumed_at != null (already used, can go)
  - done/<id>.json older than 24h                              (manager already saw them)
  - closed/<sid>.json older than 7d                            (auto-closed workers, unresumed)
  - active/<sid>.json with a provably-dead positive pid        (orphan active records;
    odd-looking ones — no usable pid, or pid alive but not a claude/codex
    process — are kept and reported, never deleted)
  - unowned debris (arch review B5): monitor cursors
    (.seen-* / .batch-turn-ends-* / .last-seen* / .fs-emitted-*) >7d, stale
    notify-outbox entries >7d, empty per-manager bucket dirs under done/
    turn-ends/ questions/ notify-outbox/, and the dead manager.lock husk

Prints one line summary; silent if everything's clean. Standalone — only uses stdlib
so it can be run as `python3 ~/.claude/scripts/preflight_cleanup.py` without the
orchestrator's venv on PATH.
"""
from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import time
from pathlib import Path

STALE_UNCONSUMED_HANDOFF_SEC = 60 * 60        # 1 hour
STALE_CONSUMED_HANDOFF_SEC = 24 * 60 * 60     # 24 hours
STALE_DONE_SEC = 24 * 60 * 60                 # 24 hours
STALE_TURN_END_SEC = 24 * 60 * 60             # 24 hours
STALE_CLOSED_SEC = 7 * 24 * 60 * 60           # 7 days
STALE_CURSOR_SEC = 7 * 24 * 60 * 60           # 7 days

# os.kill raises OverflowError (not OSError) for pids above the C int range, so a
# poisoned record would traceback the whole preflight — and never get cleaned, so
# it repeats at every /manager boot. Pids beyond this bound are "no usable pid".
MAX_OS_PID = 0x7FFFFFFF

def _prefer_new(new: Path, legacy: Path) -> Path:
    # deprecated, one release: legacy fallback while orchestrator-era state migrates
    if new.exists():
        return new
    if legacy.exists():
        return legacy
    return new


_HOME = Path(os.environ.get("HOME", ""))
ROOT = _prefer_new(_HOME / ".claude" / "dockwright", _HOME / ".claude" / "orchestrator")
ACTIVE = ROOT / "active"
HANDOFFS = ROOT / "handoffs"
DONE = ROOT / "done"
CLOSED = ROOT / "closed"
QUESTIONS = ROOT / "questions"
TURN_ENDS = ROOT / "turn-ends"
MANAGER_LOCK = ROOT / "manager.lock"
SPEND_LEDGER = ROOT / "spend-ledger.jsonl"
NOTIFY_OUTBOX = ROOT / "notify-outbox"


def _load(p: Path) -> dict | None:
    try:
        return json.load(open(p))
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM
    return True


def _process_command(pid: int) -> str:
    """Command line of pid's CURRENT process, or "" when it can't be read.

    Any lookup failure reads as "" (= not a session), which downstream only ever
    KEEPS a record — never deletes on an unreadable command line."""
    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except Exception:
        return ""


def _looks_like_session(command: str) -> bool:
    """Mirrors sweep._looks_like_session (intentionally duplicated, stdlib-only):
    argv[0]'s basename only, so a claude/codex-shaped token in the args can't
    make a recycled pid read as a session."""
    tokens = command.split()
    return bool(tokens) and os.path.basename(tokens[0]) in ("claude", "codex")


def _tmux_socket() -> str:
    return (os.environ.get("DOCKWRIGHT_TMUX_SOCKET")
            or os.environ.get("CLAUDE_ORCH_TMUX_SOCKET")  # deprecated, one release
            or "dockwright")


def _live_pane_ids():
    """Pane ids alive on the orchestrator tmux server; None when tmux cannot
    answer (error/timeout), set() when the server simply isn't running.
    Mirrors registry._live_pane_ids semantics — intentionally duplicated to
    keep this script stdlib-only, the same way _prune_active mirrors registry."""
    try:
        proc = subprocess.run(
            ["tmux", "-L", _tmux_socket(), "list-panes", "-a", "-F", "#{pane_id}"],
            capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if proc.returncode != 0:
        return set() if "no server" in (proc.stderr or "").lower() else None
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _prune_handoffs(now: float) -> tuple[int, int]:
    if not HANDOFFS.is_dir():
        return 0, 0
    stale_unconsumed = 0
    stale_consumed = 0
    for p in HANDOFFS.iterdir():
        if p.suffix != ".json":
            continue
        record = _load(p)
        if record is None:
            continue
        prepared_at = record.get("prepared_at") or 0
        age = now - prepared_at
        consumed_at = record.get("consumed_at")
        if consumed_at is None and age > STALE_UNCONSUMED_HANDOFF_SEC:
            p.unlink(missing_ok=True)
            stale_unconsumed += 1
        elif consumed_at is not None and age > STALE_CONSUMED_HANDOFF_SEC:
            p.unlink(missing_ok=True)
            stale_consumed += 1
    return stale_unconsumed, stale_consumed


def _prune_done(now: float) -> int:
    if not DONE.is_dir():
        return 0
    pruned = 0
    # rglob recurses the per-manager subdirs + _unscoped + any legacy flat files.
    for p in DONE.rglob("*.json"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if now - mtime > STALE_DONE_SEC:
            p.unlink(missing_ok=True)
            pruned += 1
    return pruned


def _prune_turn_ends(now: float) -> int:
    if not TURN_ENDS.is_dir():
        return 0
    pruned = 0
    # rglob recurses the per-manager subdirs + _unscoped + any legacy flat files.
    for p in TURN_ENDS.rglob("*.json"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if now - mtime > STALE_TURN_END_SEC:
            p.unlink(missing_ok=True)
            pruned += 1
    return pruned


def _prune_closed(now: float) -> int:
    if not CLOSED.is_dir():
        return 0
    pruned = 0
    for p in CLOSED.iterdir():
        if p.suffix != ".json":
            continue
        record = _load(p)
        closed_at = record.get("closed_at") if record else None
        if not isinstance(closed_at, (int, float)) or closed_at <= 0:
            try:
                closed_at = p.stat().st_mtime
            except OSError:
                continue
        if now - closed_at > STALE_CLOSED_SEC:
            if record and record.get("closed_reason") != "session_end":
                # session_end closures were ledgered at close; autoclosed
                # records' spend exists only here — last chance to keep it.
                _append_spend_drop(record, "closed_prune")
            p.unlink(missing_ok=True)
            pruned += 1
    return pruned


def _append_spend_drop(record: dict, source: str) -> None:
    """Mirrors dockwright/spend_ledger.append_drop_event — intentionally
    duplicated to keep this script stdlib-only, the same way _prune_active
    mirrors registry. Best-effort: hygiene must never fail on observability."""
    try:
        spend = record.get("spend")
        spend = spend if isinstance(spend, dict) else {}
        totals = {key: spend[key]
                  for key in ("turns", "out_tokens", "in_tokens",
                              "cache_read_tokens", "cache_creation_tokens")
                  if isinstance(spend.get(key), int) and not isinstance(spend.get(key), bool)}
        if not totals and source != "preflight_prune":
            # A preflight prune unlinks the record with no other durable trace,
            # so it ledgers even at zero spend; closed_prune records were
            # already ledgered at their original drop.
            return
        entry = {
            "ts": time.time(),
            "sid": record.get("claude_sid"),
            "name": record.get("name"),
            "agent": "nested" if record.get("nested") else (record.get("agent") or "worker"),
            "parent_manager_name": record.get("parent_manager_name"),
            "runtime": record.get("runtime") or "claude",
            "started_at": record.get("started_at"),
            "source": source,
            "spend": totals,
        }
        with open(SPEND_LEDGER, "a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _drop_questions_for_worker(worker_sid: str) -> None:
    if not QUESTIONS.is_dir():
        return
    for q in QUESTIONS.rglob("*.json"):
        record = _load(q)
        if record and record.get("worker_sid") == worker_sid:
            q.unlink(missing_ok=True)


def _prune_active() -> tuple[list[str], list[str]]:
    """Mirrors registry._prune_stale_active_records (intentionally duplicated to
    keep this script stdlib-only), with a stricter never-delete-live invariant —
    the MCP mirror shares the in-range-positive-pid-and-dead deletion bar but
    skips the per-record `ps` check (subprocess latency on MCP request paths).
    Deletion requires an in-OS-range positive int pid that os.kill(pid, 0) says
    is dead. Anything else odd — no usable pid, or pid alive but its process
    command line isn't a claude/codex session (pid recycling) — is KEPT and
    reported (todo 1779779425-d1511675).

    Closed lanes: pid<=0 / out-of-range / missing pid, and alive-pid records
    whose process isn't a session. A recycled pid claimed by ANOTHER claude
    session is indistinguishable from the record's own and is kept silently —
    still the safe direction. Residual lane closed by the pane gate: a record
    whose stored pid is dead while its pane lives (Linux transient-$PPID
    registrations, recycled pids) is KEPT and reported; only a dead pid with
    no surviving pane deletes.

    Returns (pruned_labels, kept_odd_labels)."""
    if not ACTIVE.is_dir():
        return [], []
    pruned: list[str] = []
    kept_odd: list[str] = []
    live_panes = None
    panes_fetched = False
    for p in ACTIVE.iterdir():
        if p.suffix != ".json":
            continue
        record = _load(p)
        if record is None:
            continue
        label = record.get("name") or record.get("claude_sid") or p.stem
        pid = record.get("pid")
        if not isinstance(pid, int) or pid <= 0 or pid > MAX_OS_PID:
            kept_odd.append(f"{label} (no usable pid)")
            continue
        if _pid_alive(pid):
            if not _looks_like_session(_process_command(pid)):
                kept_odd.append(f"{label} (alive but non-session command)")
            continue
        window_id = record.get("window_id") or record.get("iterm_sid") or ""
        if window_id:
            if not panes_fetched:
                live_panes = _live_pane_ids()
                panes_fetched = True
            if live_panes is None:
                kept_odd.append(f"{label} (dead pid; tmux unanswerable, pane {window_id} unverified)")
                continue
            if str(window_id) in live_panes:
                kept_odd.append(f"{label} (dead pid but live pane {window_id})")
                continue
        sid = record.get("claude_sid")
        _append_spend_drop(record, "preflight_prune")
        p.unlink(missing_ok=True)
        if sid:
            _drop_questions_for_worker(sid)
        pruned.append(label)
    return pruned, kept_odd


def _gc_husks(now: float) -> int:
    """Unowned-debris GC (arch-soundness review B5). Everything here is
    either re-created on demand by its writer (cursors, bucket dirs) or
    referenced by no code path at all (the May-era manager.lock husk)."""
    pruned = 0
    # Monitor cursors: live managers' cursors are touched on every append, so
    # only dead managers' cursors age past the threshold. The events they
    # index die at 24h, so losing a cursor at worst re-shows <24h of events.
    for pattern in (".seen-*", ".batch-turn-ends-*", ".last-seen*", ".fs-emitted-*"):
        for p in ROOT.glob(pattern):
            try:
                if p.is_file() and now - p.stat().st_mtime > STALE_CURSOR_SEC:
                    p.unlink(missing_ok=True)
                    pruned += 1
            except OSError:
                continue
    # Outbox entries are drained (unlinked) by live managers' scans within
    # minutes; anything 7d old belongs to a dead manager and will never drain.
    if NOTIFY_OUTBOX.is_dir():
        for p in NOTIFY_OUTBOX.rglob("*.json"):
            try:
                if now - p.stat().st_mtime > STALE_CURSOR_SEC:
                    p.unlink(missing_ok=True)
                    pruned += 1
            except OSError:
                continue
    # Per-manager bucket dirs are mkdir'd on demand and never rmdir'd; every
    # scan walks all of them. rmdir refuses non-empty dirs by contract.
    for bucket in (DONE, TURN_ENDS, QUESTIONS, NOTIFY_OUTBOX):
        if not bucket.is_dir():
            continue
        for sub in bucket.iterdir():
            if not sub.is_dir():
                continue
            try:
                sub.rmdir()
                pruned += 1
            except OSError:
                continue
    if MANAGER_LOCK.is_file():
        MANAGER_LOCK.unlink(missing_ok=True)
        pruned += 1
    return pruned


def main() -> int:
    now = time.time()
    stale_unconsumed, stale_consumed = _prune_handoffs(now)
    done_pruned = _prune_done(now)
    turn_ends_pruned = _prune_turn_ends(now)
    closed_pruned = _prune_closed(now)
    active_pruned, active_kept_odd = _prune_active()
    husks_pruned = _gc_husks(now)
    total = (stale_unconsumed + stale_consumed + done_pruned + turn_ends_pruned
             + closed_pruned + len(active_pruned) + husks_pruned)
    segments = []
    if total > 0:
        parts = []
        if stale_unconsumed:
            parts.append(f"{stale_unconsumed} stale unconsumed handoff(s)")
        if stale_consumed:
            parts.append(f"{stale_consumed} old consumed handoff(s)")
        if done_pruned:
            parts.append(f"{done_pruned} old done event(s)")
        if turn_ends_pruned:
            parts.append(f"{turn_ends_pruned} old turn-end event(s)")
        if closed_pruned:
            parts.append(f"{closed_pruned} old closed worker record(s)")
        if active_pruned:
            parts.append(f"{len(active_pruned)} stale active record(s) ({', '.join(active_pruned)})")
        if husks_pruned:
            parts.append(f"{husks_pruned} stale husk(s) (cursors/empty buckets/manager.lock)")
        segments.append(f"pruned {', '.join(parts)}")
    if active_kept_odd:
        segments.append(
            f"kept {len(active_kept_odd)} odd-looking active record(s), not pruned: "
            f"{', '.join(active_kept_odd)}")
    if segments:
        print(f"preflight: {'; '.join(segments)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
