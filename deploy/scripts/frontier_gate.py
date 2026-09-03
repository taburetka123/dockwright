#!/usr/bin/env python3
from __future__ import annotations

import argparse
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


def _env_positive_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _dockwright_config_file() -> Path | None:
    env = os.environ.get("DOCKWRIGHT_CONFIG", "").strip()
    if env:
        p = Path(env).expanduser()
        return p if p.is_file() else None
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    for candidate in (base / "dockwright" / "dockwright.toml",
                      Path.home() / ".claude" / "dockwright.toml"):
        if candidate.is_file():
            return candidate
    return None


def _scan_toml_bool(text: str, section: str, key: str) -> bool | None:
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1].strip()
            continue
        if cur != section or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() != key:
            continue
        v = v.split("#", 1)[0].strip()
        if v == "true":
            return True
        if v == "false":
            return False
        return None
    return None


def gardener_module_enabled() -> bool:
    path = _dockwright_config_file()
    if path is None:
        return True
    try:
        import tomllib
        with open(path, "rb") as fh:
            value = tomllib.load(fh).get("modules", {}).get("gardener")
    except ModuleNotFoundError:
        try:
            value = _scan_toml_bool(path.read_text(), "modules", "gardener")
        except OSError:
            return True
    except Exception:
        return True
    return value if isinstance(value, bool) else True


HOME = Path(os.environ.get("HOME", ""))


def _prefer_new(new: Path, legacy: Path) -> Path:
    if new.exists():
        return new
    if legacy.exists():
        return legacy
    return new


GARDENER_DIR = _prefer_new(HOME / ".claude" / "dockwright" / "gardener", HOME / ".claude" / "gardener")
LEDGER_PATH = GARDENER_DIR / "ledger.jsonl"
MARKER_PATH = GARDENER_DIR / "last-frontier-run"
GATE_LOG_PATH = GARDENER_DIR / "frontier-gate.log"
STOP_PATHS = (HOME / ".claude" / "dockwright" / "frontier-stop", HOME / ".claude" / "frontier-stop")
RUN_LOCK_DIR = HOME / ".claude" / "locks" / "analyst-run.lock"
RUN_SCRIPT = HOME / ".claude" / "scripts" / "gardener-run.sh"

INTERVAL_DAYS = _env_positive_float("GARDENER_FRONTIER_INTERVAL_DAYS", 7.0)
RETRY_GAP_SEC = _env_positive_int("GARDENER_FRONTIER_RETRY_GAP", 48 * 3600)

EXIT_OK = 0
EXIT_REFUSED_STOPPED = 3
EXIT_FORCE_LOCKED = 4


def _marker_mtime() -> float | None:
    try:
        return MARKER_PATH.stat().st_mtime
    except OSError:
        return None


def newest_frontier_run_age(now: float) -> float | None:
    if not LEDGER_PATH.is_file():
        return None
    try:
        lines = LEDGER_PATH.read_text().splitlines()
    except OSError:
        return None
    newest = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if (event.get("type") or event.get("event")) != "run_start":
            continue
        if (event.get("lane") or "digest") != "frontier":
            continue
        ts = event.get("ts")
        if isinstance(ts, (int, float)) and (newest is None or ts > newest):
            newest = float(ts)
    return None if newest is None else now - newest


def lock_held_by_live_pid() -> bool:
    if not RUN_LOCK_DIR.is_dir():
        return False
    try:
        pid = int((RUN_LOCK_DIR / "pid").read_text().strip())
    except (OSError, ValueError):
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def decide(now: float, force: bool) -> tuple[str, dict]:
    detail: dict = {"force": force}
    if any(p.exists() for p in STOP_PATHS):
        return ("refused_stopped" if force else "stopped"), detail
    if lock_held_by_live_pid():
        return "locked", detail
    if not force:
        last_age = newest_frontier_run_age(now)
        if last_age is not None and last_age < RETRY_GAP_SEC:
            detail["last_run_age_sec"] = int(last_age)
            return "cooldown", detail
    if force:
        return "force", detail
    marker = _marker_mtime()
    if marker is None:
        return "not_armed", detail
    age_days = (now - marker) / 86400
    detail["marker_age_days"] = round(age_days, 2)
    if age_days >= INTERVAL_DAYS:
        return "frontier", detail
    return "not_due", detail


def spawn_run(trigger: str) -> None:
    subprocess.Popen(
        ["bash", str(RUN_SCRIPT), "--trigger", trigger, "--lane", "frontier"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _log_gate(decision: str, detail: dict, spawned: bool) -> None:
    GATE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with GATE_LOG_PATH.open("a") as f:
        f.write(f"{stamp}  {decision}  spawned={spawned}  "
                f"{json.dumps(detail, sort_keys=True)}\n")


def main(argv: list[str] | None = None) -> int:
    if not os.environ.get("HOME"):
        print("frontier-gate: HOME is not set — refusing to guess paths", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="Frontier loop trigger gate (LLM-free).")
    parser.add_argument("--force", action="store_true",
                        help="Bypass interval + cooldown. Still refuses under the "
                             "stop file and the live run mutex.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the decision without spawning a run.")
    args = parser.parse_args(argv)

    if not gardener_module_enabled():
        print("frontier-gate: module-off ([modules] gardener=false) — no-op")
        return EXIT_OK

    GARDENER_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    decision, detail = decide(now, force=args.force)
    should_spawn = decision in ("frontier", "force") and not args.dry_run
    if should_spawn:
        spawn_run(decision)
    _log_gate(decision, detail, spawned=should_spawn)
    print(f"frontier-gate: {decision} spawned={should_spawn} "
          f"{json.dumps(detail, sort_keys=True)}")

    if decision == "refused_stopped":
        print(f"frontier-gate: stopped — remove {STOP_PATHS[0]} first", file=sys.stderr)
        return EXIT_REFUSED_STOPPED
    if decision == "locked" and args.force:
        print("frontier-gate: run mutex held by a live process — retry when the "
              "current analyst run finishes", file=sys.stderr)
        return EXIT_FORCE_LOCKED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
