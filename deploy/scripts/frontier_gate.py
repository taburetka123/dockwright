#!/usr/bin/env python3
"""LLM-free trigger gate for the FRONTIER loop — a separate registered loop,
NOT a gardener_gate lane (arch-soundness review C1: a calendar-interval lane
inside the accumulation gate is the loop-master runtime Part B rejected).

The frontier loop re-runs the orchestrator frontier research sweep
(the manual v1 sweep is run #0, 2026-06-11) on a fixed
interval. It SHARES the Gardener's trust substrate — the artifact contract
into proposals/pending/, the FR-8 postrun quarantine, the review sitting, the
decision ledger, the analyst-run mutex — and shares NOTHING of the digest
gate's decision chain or budget pool.

Invoked daily by launchd (com.dockwright.gardener-frontier, installed by
gardener-install.sh) and manually with --force. Decision order:

  stopped     — ~/.claude/frontier-stop exists (per-loop kill switch, B3
                convention). --force refuses too (exit 3): stopped means
                stopped, and stopping the digest loop does NOT stop this one.
  locked      — the shared analyst-run mutex is held by a live pid (one
                claude -p / analyst session fleet-wide; rate-limiter guard).
                Applies to --force as well (exit 4).
  cooldown    — the newest lane=frontier run_start in the shared ledger is
                younger than GARDENER_FRONTIER_RETRY_GAP seconds (default
                48h). A failed web-heavy run retries in days, not hourly —
                the marker only advances on ok, so without this a wedged run
                would re-fire every tick.
  not_armed   — the last-frontier-run marker is ABSENT. The first run is an
                explicit human decision: gardener-install.sh arms the marker
                at install (run #0 = the manual v1 research), so a fresh
                deploy never fires a surprise token-heavy web sweep.
  not_due     — marker younger than GARDENER_FRONTIER_INTERVAL_DAYS
                (default 7).
  frontier    — due; spawn gardener-run.sh --lane frontier (the run wrapper
                is shared MECHANISM — tmux spawn, watchdog, write-guard,
                audit, postrun — parameterized by lane; the gate decision
                chains stay separate).

The interval IS the budget: one run per interval, the retry gap bounds
failure bursts, and digest-lane caps are untouched by construction.
Every invocation appends one decision line to ~/.claude/dockwright/gardener/frontier-gate.log.
"""
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
    """Discover dockwright.toml the way config.py does (env DOCKWRIGHT_CONFIG ->
    XDG_CONFIG_HOME/dockwright -> ~/.claude/dockwright.toml). Deployed gates must
    NOT import dockwright, so discovery is re-implemented."""
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
    """Bare `key = true|false` inside [section] — the tomllib-less fallback for
    the py3.9 /usr/bin/python3 this gate's launchd plist runs it under."""
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
    """[modules] gardener toggle from dockwright.toml (the frontier loop is part
    of the Gardener subsystem, so it shares the switch). tomllib when available;
    the bare-bool scanner otherwise. Default + fail-open: ENABLED."""
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
    # deprecated, one release: legacy fallback while orchestrator-era state migrates
    if new.exists():
        return new
    if legacy.exists():
        return legacy
    return new


GARDENER_DIR = _prefer_new(HOME / ".claude" / "dockwright" / "gardener", HOME / ".claude" / "gardener")
LEDGER_PATH = GARDENER_DIR / "ledger.jsonl"
MARKER_PATH = GARDENER_DIR / "last-frontier-run"
GATE_LOG_PATH = GARDENER_DIR / "frontier-gate.log"
# deprecated, one release: operator stop-file honored at either home
STOP_PATHS = (HOME / ".claude" / "dockwright" / "frontier-stop", HOME / ".claude" / "frontier-stop")
# Neutral lock home shared with selffix-run.sh + gardener-run.sh
# (deploy/scripts/runlock.sh owns the protocol — arch review A5).
RUN_LOCK_DIR = HOME / ".claude" / "locks" / "analyst-run.lock"
RUN_SCRIPT = HOME / ".claude" / "scripts" / "gardener-run.sh"

INTERVAL_DAYS = _env_positive_float("GARDENER_FRONTIER_INTERVAL_DAYS", 7.0)
RETRY_GAP_SEC = _env_positive_int("GARDENER_FRONTIER_RETRY_GAP", 48 * 3600)

EXIT_OK = 0
EXIT_REFUSED_STOPPED = 3
EXIT_FORCE_LOCKED = 4


def _marker_mtime() -> float | None:
    """None when the marker is absent (loop not armed)."""
    try:
        return MARKER_PATH.stat().st_mtime
    except OSError:
        return None


def newest_frontier_run_age(now: float) -> float | None:
    """Age of the newest lane=frontier run_start in the SHARED ledger.
    Envelope key `type` with `event` tolerated. Corrupt lines skipped."""
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
    """Same protocol as gardener_gate (cheap advisory pre-check; the run
    wrapper re-acquires atomically via runlock.sh, which owns the run-side
    mutex semantics). Duplicated by the loop convention — gates are
    self-contained domain code."""
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
        # gardener=false disables the frontier loop too — no dir, no gate.log.
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
