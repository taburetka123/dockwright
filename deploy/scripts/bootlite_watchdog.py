#!/usr/bin/env python3
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
    if new.exists():
        return new
    if legacy.exists():
        return legacy
    return new


ORCH_ROOT = _prefer_new(HOME / ".claude" / "dockwright", HOME / ".claude" / "orchestrator")


def _resolve_get_driver():
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
    managers_alive: dict[str, bool] = {}
    workers: list[dict] = []
    if not ACTIVE.is_dir():
        return {}
    for record_path in ACTIVE.glob("*.json"):
        record = _read_json(record_path)
        if not isinstance(record, dict):
            continue
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
    data = _read_json(STATE_PATH)
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items() if isinstance(value, dict)}


def _sweep_resolved(groups: dict, state: dict, dry_run: bool) -> None:
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
