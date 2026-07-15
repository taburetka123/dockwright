#!/usr/bin/env python3
"""Boot-lite watchdog — LLM-free tick fallback for manager-less workers.

All fleet self-healing (stale monitor, autonudge, limit recovery) runs inside
the manager's Monitor task, so a manager that dies uncleanly leaves its
surviving workers with zero supervision. The event half (hooks.session_end)
catches manager closes that still fire the SessionEnd hook; THIS script is the
fallback for the paths where no hook fires at all (SIGKILL, power loss,
hardware crash). Design: deploy/loops-registry.md (bootlite-watchdog
block; deployed to ~/.claude/dockwright/loops-registry.md).

Invoked hourly by launchd (com.dockwright.bootlite-watchdog, installed by
bootlite-install.sh) and manually with --dry-run. Zero tokens — every check is
file/pid arithmetic, cloned from the gardener_gate.py pattern.

Tick decision (run_tick):
  stopped  — ~/.claude/dockwright/bootlite-stop exists. Nothing is scanned or written
             beyond the check.log line.
  ok       — no orphaned workers. Healthy sweep still runs: stretch-state
             entries and orphans/<manager>.json flags whose group recovered
             (manager alive again — e.g. a takeover inherited the name — or no
             live workers remain) are dropped/unlinked with an orphan_cleared
             ledger event. The sweep covers flags WITHOUT state entries too: a
             flag whose stretch resolved before any tick saw it orphaned (the
             takeover close-before-unlink race, a fast /manager-resume) must
             not leak and poison a later same-name stretch's first_seen.
  orphans  — live worker records whose parent manager has no live session:
             parent_manager_name set and (no manager record with that name OR
             its pid dead); legacy null-parent workers count only when NO live
             manager exists at all (any live manager can adopt those via
             _backfill_legacy_workers). Dead-pid worker records are sweep
             debris, not orphans.

Per orphan stretch (state.json entry keyed by manager name, "_unscoped" for
the legacy group): first_seen is adopted from the event half's
orphans/<manager>.json flag when present — and a source=session_end flag also
seeds last_notified/notify_count=1, because the hook already notified at that
moment. Notifications repeat at RENOTIFY_SEC (default 4h) up to
MAX_NOTIFY_PER_STRETCH (default 6) per stretch — adoption of named orphans is
manual today, so an unresolvable stretch must nag a bounded number of times,
not forever. Past the cap the per-tick check.log keeps recording the orphan
state (ledger events fire on transitions only).

Autonudge (opt-in via CLAUDE_ORCH_AUTONUDGE=1, default OFF): each orphaned
worker with a tmux window and NO pending question gets ONE typed message per
stretch telling it the manager is gone and to bring its task to a durable
checkpoint (commit/push, then worker_done — done events persist and any future
manager reads them). Deliberately NOT "resume your task": the worker may be
mid-flight and fine; what is broken is its control plane, so the correct move
is durable completion, not a restart. Workers blocked inside ask_manager are
skipped (typed text cannot submit into a blocked MCP call) — the human-facing
notification covers those.

Known limitations (accepted): pid reuse can pin a phantom "live" worker (EPERM
reads alive — same exposure as the rest of the codebase), in which case the
stretch clears only manually and the notify cap bounds the noise. A
recreate-manager flow straddling a tick can notify spuriously once; the next
tick's sweep clears it. Conversely, a tick racing a DYING manager (the hook
just wrote the flag but the manager record+pid linger for a few more seconds)
sees the group as healthy and sweeps the fresh flag — losing the first_seen
seed, nothing else; the next tick re-detects with first_seen = then.
Seconds-per-hour window, self-healing.

Kill switch: touch ~/.claude/dockwright/bootlite-stop. Uninstall: launchctl bootout
gui/$(id -u)/com.dockwright.bootlite-watchdog && rm the plist.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _env_positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


HOME = Path(os.environ.get("HOME", ""))


def _prefer_new(new: Path, legacy: Path) -> Path:
    # deprecated, one release: legacy fallback while orchestrator-era state migrates
    if new.exists():
        return new
    if legacy.exists():
        return legacy
    return new


ORCH_ROOT = _prefer_new(HOME / ".claude" / "dockwright", HOME / ".claude" / "orchestrator")


def _resolve_get_driver():
    """Best-effort import of the terminal driver under /usr/bin/python3. terminal.py is
    stdlib-only, so a sys.path insert of the repo src suffices. Returns the get_driver
    callable or None (degrade to skip-nudge; never crash orphan detection)."""
    try:
        from dockwright.terminal import get_driver
        return get_driver
    except Exception:
        pass
    try:
        src = os.environ.get("CLAUDE_ORCH_SRC") or str(
            HOME / "projects" / "personal" / "claude-orchestrator" / "src")
        if Path(src).is_dir() and src not in sys.path:
            sys.path.insert(0, src)
        from dockwright.terminal import get_driver
        return get_driver
    except Exception:
        return None
ACTIVE = ORCH_ROOT / "active"
QUESTIONS = ORCH_ROOT / "questions"
ORPHANS = ORCH_ROOT / "orphans"
BOOTLITE_DIR = _prefer_new(HOME / ".claude" / "dockwright" / "bootlite", HOME / ".claude" / "bootlite")
STATE_PATH = BOOTLITE_DIR / "state.json"
LEDGER_PATH = BOOTLITE_DIR / "ledger.jsonl"
CHECK_LOG_PATH = BOOTLITE_DIR / "check.log"
# deprecated, one release: operator stop-file honored at either home
STOP_PATHS = (HOME / ".claude" / "dockwright" / "bootlite-stop", HOME / ".claude" / "bootlite-stop")

RENOTIFY_SEC = _env_positive_int("BOOTLITE_RENOTIFY_SEC", 4 * 3600)
MAX_NOTIFY_PER_STRETCH = _env_positive_int("BOOTLITE_MAX_NOTIFY", 6)
AUTONUDGE = os.environ.get("CLAUDE_ORCH_AUTONUDGE") == "1"

UNSCOPED = "_unscoped"

NUDGE_TEXT = (
    "[bootlite watchdog] Your manager session is gone (crashed or closed "
    "uncleanly) — there is currently no live manager supervising you. Do not "
    "block on ask_manager: nothing will answer until a replacement manager "
    "appears. Bring your task to a durable checkpoint now: commit and push "
    "your work, then call worker_done with a complete summary — done events "
    "persist and any future manager will read them. If you are blocked on a "
    "question, state the question and your chosen assumption in that summary "
    "instead of asking."
)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno == errno.EPERM


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def _bucket(manager_name) -> str:
    """Group key / flag-file stem for a manager name (mirrors paths._event_bucket)."""
    if not manager_name:
        return UNSCOPED
    return str(manager_name).replace("/", "_").replace("\\", "_")


def _ledger_append(event: str, **fields) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {"event": event, "ts": time.time()}
    record.update(fields)
    with LEDGER_PATH.open("a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def _log_check(decision: str, detail: dict) -> None:
    CHECK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with CHECK_LOG_PATH.open("a") as f:
        f.write(f"{stamp}  {decision}  {json.dumps(detail, sort_keys=True)}\n")


def _notify_macos(message: str) -> None:
    # No-op under pytest (PYTEST_CURRENT_TEST, inherited by child processes):
    # a test exec'ing this deployed script must never fire a real desktop
    # notification (the 2026-07-03 gardener-gate leak class).
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        sanitized = message.replace('"', "")
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{sanitized}" with title "bootlite watchdog"'],
            capture_output=True, timeout=5, check=False,
        )
    except Exception:
        pass


def _pending_question_sids() -> set:
    sids = set()
    if not QUESTIONS.is_dir():
        return sids
    for p in QUESTIONS.rglob("*.json"):
        record = _read_json(p)
        if record and record.get("worker_sid"):
            sids.add(record["worker_sid"])
    return sids


def scan_orphans() -> dict[str, list[dict]]:
    """Map group key -> live worker records whose parent manager has no live
    session. Per-record defensive: corrupt JSON / non-int pid records are
    skipped — one bad record must never abort the scan."""
    managers_alive: dict[str, bool] = {}
    workers: list[dict] = []
    if not ACTIVE.is_dir():
        return {}
    for record_path in ACTIVE.glob("*.json"):
        record = _read_json(record_path)
        if not isinstance(record, dict):
            continue
        # Nested sub-sessions inherit CLAUDE_PARENT_MANAGER, so they'd read as
        # a dead manager's workers (and a nested manager-ghost as a manager) —
        # they're excluded from all lifecycle surfaces and die with their
        # parent process; skip them entirely.
        if record.get("nested"):
            continue
        pid = record.get("pid")
        alive = isinstance(pid, int) and _pid_alive(pid)
        agent = record.get("agent")
        if agent == "manager" and record.get("name"):
            managers_alive[record["name"]] = managers_alive.get(record["name"]) or alive
        elif agent == "worker" and alive:
            workers.append(record)
    any_manager_alive = any(managers_alive.values())
    groups: dict[str, list[dict]] = {}
    for worker in workers:
        parent = worker.get("parent_manager_name")
        if parent:
            if managers_alive.get(parent):
                continue
        elif any_manager_alive:
            continue
        groups.setdefault(_bucket(parent), []).append(worker)
    return groups


def _load_state() -> dict:
    """Stretch state, hostile-input safe BY SHAPE, not just by parse: a
    valid-JSON-wrong-shape file ({"yak": 5}) would otherwise wedge every
    subsequent tick with an AttributeError — and nothing repairs state.json
    except this loader. Non-dict entries are dropped, not fatal."""
    data = _read_json(STATE_PATH)
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items() if isinstance(value, dict)}


def _sweep_resolved(groups: dict, state: dict, dry_run: bool) -> None:
    """Drop stretch state + unlink orphan flags for groups that recovered."""
    flag_keys = set()
    if ORPHANS.is_dir():
        flag_keys = {p.stem for p in ORPHANS.glob("*.json")}
    for key in (set(state) | flag_keys) - set(groups):
        if dry_run:
            continue
        state.pop(key, None)
        flag = ORPHANS / f"{key}.json"
        if flag.exists():
            flag.unlink(missing_ok=True)
        _ledger_append("orphan_cleared", manager=key)


def _new_stretch_entry(key: str, now: float) -> dict:
    entry = {"first_seen": now, "last_notified": None, "notify_count": 0, "nudged": {}}
    flag = _read_json(ORPHANS / f"{key}.json")
    if isinstance(flag, dict) and isinstance(flag.get("orphaned_at"), (int, float)):
        entry["first_seen"] = float(flag["orphaned_at"])
        if flag.get("source") == "session_end":
            # The hook already notified at that moment — don't double-notify
            # inside the renotify window.
            entry["last_notified"] = float(flag["orphaned_at"])
            entry["notify_count"] = 1
    return entry


def _notify_group(key: str, workers: list, entry: dict, pending_sids: set, now: float) -> bool:
    last = entry.get("last_notified")
    count = entry.get("notify_count") or 0
    if last is not None and now - last < RENOTIFY_SEC:
        return False
    if count >= MAX_NOTIFY_PER_STRETCH:
        return False
    questioned = sum(1 for w in workers if w.get("claude_sid") in pending_sids)
    cause = (f"manager {key} has no live session" if key != UNSCOPED
             else "no live manager exists at all")
    _notify_macos(
        f"{len(workers)} worker(s) orphaned — {cause} "
        f"({questioned} waiting on questions). Resume or start a manager; "
        "workers keep running until adopted or closed."
    )
    entry["last_notified"] = now
    entry["notify_count"] = count + 1
    return True


def _nudge_group(key: str, workers: list, entry: dict, pending_sids: set,
                 send, now: float) -> list[str]:
    nudged_sids = []
    if send is None:
        return nudged_sids
    nudged_map = entry.setdefault("nudged", {})
    for worker in workers:
        sid = worker.get("claude_sid")
        window_id = worker.get("window_id") or worker.get("iterm_sid") or ""
        if not sid or sid in nudged_map or not window_id or sid in pending_sids:
            continue
        send(str(window_id), NUDGE_TEXT)
        nudged_map[sid] = now
        nudged_sids.append(sid)
    return nudged_sids


def run_tick(now: float, dry_run: bool = False) -> tuple[str, dict]:
    if any(p.exists() for p in STOP_PATHS):
        if not dry_run:
            _log_check("stopped", {})
        return "stopped", {}

    groups = scan_orphans()
    detail = {"groups": {key: len(workers) for key, workers in groups.items()}}
    state = _load_state()
    _sweep_resolved(groups, state, dry_run)

    if not groups:
        if not dry_run:
            _write_json_atomic(STATE_PATH, state)
            _log_check("ok", detail)
        return "ok", detail

    if dry_run:
        return "orphans", detail

    pending_sids = _pending_question_sids()
    send = None
    if AUTONUDGE:
        gd = _resolve_get_driver()
        if gd is not None:
            send = lambda wid, txt: gd().send_text(wid, txt)
    for key, workers in groups.items():
        entry = state.get(key)
        if entry is None:
            entry = _new_stretch_entry(key, now)
            state[key] = entry
            _ledger_append("orphan_detected", manager=key, workers=len(workers),
                           first_seen=entry["first_seen"])
        live_sids = {w.get("claude_sid") for w in workers}
        entry["nudged"] = {sid: ts for sid, ts in (entry.get("nudged") or {}).items()
                           if sid in live_sids}
        if _notify_group(key, workers, entry, pending_sids, now):
            _ledger_append("notified", manager=key, workers=len(workers),
                           notify_count=entry["notify_count"])
        if AUTONUDGE:
            for sid in _nudge_group(key, workers, entry, pending_sids, send, now):
                _ledger_append("nudged", manager=key, worker_sid=sid)

    _write_json_atomic(STATE_PATH, state)
    _log_check("orphans", detail)
    return "orphans", detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Boot-lite watchdog: manager-less worker detection (LLM-free).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the decision; no notifications, nudges, or state writes.")
    args = parser.parse_args(argv)
    decision, detail = run_tick(time.time(), dry_run=args.dry_run)
    print(f"bootlite-watchdog: {decision} {json.dumps(detail, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
