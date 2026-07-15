#!/usr/bin/env python3
"""LLM-free Gardener trigger gate — Phase 0.

Invoked hourly by launchd (com.dockwright.gardener-gate, installed by
gardener-install.sh) and manually with --force. Decides whether a digest
analyst run should fire and spawns gardener-run.sh detached when it should.
Zero tokens while the gate is closed — every check is file arithmetic.

Decision order (first hit wins):
  stopped     — ~/.claude/dockwright/gardener-stop exists. --force refuses too (exit 3,
                remove the stop file first): stopped means stopped, PRD §5.
  locked      — the shared analyst-run mutex (~/.claude/locks/analyst-run.lock,
                protocol in runlock.sh) is held by a live pid. Applies to
                --force as well: the mutex protects the rate limiter, not the
                cadence. Cheap pre-check only — gardener-run.sh re-acquires
                atomically; the hourly tick is the retry.
  retry-pre-step — queued selffix retros (one per tick) spawn before any
                digest; a retry-spawning tick defers the digest decision to
                the next tick. --force skips the pre-step (human-initiated
                digest wins; the queue waits for the next hourly tick). The
                gate.log decision stays the digest decision; a consumed retry
                shows as retry_spawned in detail.
  cooldown    — the newest run_start is younger than GARDENER_MIN_RUN_GAP
                seconds (default 6h). Bounds the burst a wedged/failed spawn
                could otherwise produce (without it, a failing first install
                could open a tab per tick until the weekly cap bit). --force
                bypasses. Counts attempts, like the cap.
  cap         — >= GARDENER_MAX_RUNS_PER_WEEK (default 3) run_start events in
                the ledger's trailing 7 days. --force bypasses: human-
                initiated spend is the human's own decision (PRD §5).
  accum       — >= GARDENER_K (default 8) unreviewed selffix findings newer
                than the last-digest marker.
  floor       — last digest >= GARDENER_FLOOR_DAYS (default 7) ago AND at
                least one new unreviewed finding since (a quiet week costs
                nothing, PRD §5).
  no_material — neither trigger armed.

Unreviewed = *.md in ~/.claude/dockwright/selffix/findings/ with no .reviewed sibling
(the dockwright-selffix-review marker contract). "Newer than the last digest"
compares finding mtime against the ~/.claude/dockwright/gardener/last-digest marker
mtime; marker absent = epoch, so the first run sees the whole backlog.

BOUNDARY (PRD v2 §2 ISN'T, arch-soundness review): this gate carries exactly
ONE accumulation predicate and ONE budget pool — the digest lane's. A second
trigger lane or budget pool is a NEW loop requiring a PRD amendment (the
frontier loop lives in frontier_gate.py for exactly this reason). The cap and
cooldown counters filter the shared ledger to lane=digest so the two loops'
budgets stay separate by construction.

Every invocation appends one decision line to ~/.claude/dockwright/gardener/gate.log.
Run records (run_start / run_end) are written by gardener-run.sh, not here;
the cap check only reads them.

Every invocation also runs the producer-liveness asserts (producer_warnings):
the expected SessionEnd hooks are still wired in settings.json, and the
newest finding is not lagging session activity by >48h. Advisory only —
WARN lines in gate.log plus a throttled notification, never a gate decision.
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
    XDG_CONFIG_HOME/dockwright -> ~/.claude/dockwright.toml). This deployed gate
    must NOT import dockwright, so it re-implements discovery."""
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
    the py3.9 /usr/bin/python3 this gate's launchd plist runs it under (no
    tomllib there). Not a general TOML parser; [modules] keys are bare bools."""
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
    """[modules] gardener toggle straight from dockwright.toml. tomllib when the
    interpreter has it (py3.11+); the bare-bool scanner otherwise. Default +
    fail-open (no config / key unset / parse error): ENABLED."""
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
DIGESTS_DIR = GARDENER_DIR / "digests"
PROPOSALS_DIR = GARDENER_DIR / "proposals"
LEDGER_PATH = GARDENER_DIR / "ledger.jsonl"
MARKER_PATH = GARDENER_DIR / "last-digest"
GATE_LOG_PATH = GARDENER_DIR / "gate.log"
# deprecated, one release: operator stop-file honored at either home
STOP_PATHS = (HOME / ".claude" / "dockwright" / "gardener-stop", HOME / ".claude" / "gardener-stop")
FINDINGS_DIR = _prefer_new(HOME / ".claude" / "dockwright" / "selffix" / "findings", HOME / ".claude" / "selffix-findings")
# Neutral lock home shared with selffix-run.sh (deploy/scripts/runlock.sh
# owns the protocol). Previously lived inside selffix's data dir — a wholesale
# prune there would have deleted a held lock (arch review A5).
RUN_LOCK_DIR = HOME / ".claude" / "locks" / "analyst-run.lock"
RUN_SCRIPT = HOME / ".claude" / "scripts" / "gardener-run.sh"
CLOSED_DIR = _prefer_new(HOME / ".claude" / "dockwright", HOME / ".claude" / "orchestrator") / "closed"
SETTINGS_PATH = HOME / ".claude" / "settings.json"
WARN_MARKER_PATH = GARDENER_DIR / ".producer-warn"
# Selffix durable-retry queue (see deploy/scripts/selffix-retry-lib.sh
# for the producer contract). The gate is the consumer because it already
# fires hourly with stop/lock pre-checks and the retry must not run inside
# gardener-run.sh (it holds the same analyst-run mutex selffix-run.sh needs).
RETRY_DIR = _prefer_new(HOME / ".claude" / "dockwright" / "selffix" / "retry", HOME / ".claude" / "selffix-retry")
ORCHESTRATOR_DIR = _prefer_new(HOME / ".claude" / "dockwright", HOME / ".claude" / "orchestrator")
SELFFIX_RUN_SCRIPT = HOME / ".claude" / "scripts" / "selffix-run.sh"
SELFFIX_LOG_PATH = _prefer_new(HOME / ".claude" / "dockwright" / "selffix" / "trigger.log", HOME / ".claude" / "selffix-trigger.log")
# deprecated, one release: operator debug flag honored at either home
SELFFIX_DEBUG_PATHS = (HOME / ".claude" / "dockwright" / "selffix" / "debug", HOME / ".claude" / "selffix-debug")
BRICK_FRESH_SEC = 300
# The findings producer is a SessionEnd hook chain — a surface with many
# writers and no invariant, severed silently once already (2026-05-25, found
# 17 days later by the arch-soundness review). The hourly gate is the one tick
# that can notice; these needles are what the gate asserts is still wired.
# Each entry is a group of acceptable alternatives — the hook counts as wired
# when ANY alternative appears (dockwright vs legacy orchestrator binary name:
# one-release dual recognition).
EXPECTED_SESSION_END_HOOKS = (
    ("dockwright session-end", "orchestrator session-end"),
    ("selffix-trigger.sh",),
)

K_THRESHOLD = _env_positive_int("GARDENER_K", 8)
FLOOR_DAYS = _env_positive_float("GARDENER_FLOOR_DAYS", 7.0)
MAX_RUNS_PER_WEEK = _env_positive_int("GARDENER_MAX_RUNS_PER_WEEK", 3)
MIN_RUN_GAP_SEC = _env_positive_int("GARDENER_MIN_RUN_GAP", 6 * 3600)
TRAILING_WINDOW_SEC = 7 * 86400
PRODUCER_STALE_GAP_SEC = _env_positive_int("GARDENER_PRODUCER_STALE_SEC", 48 * 3600)
WARN_NOTIFY_THROTTLE_SEC = 24 * 3600

EXIT_OK = 0
EXIT_REFUSED_STOPPED = 3
EXIT_FORCE_LOCKED = 4


def _marker_mtime() -> float:
    """Last-digest marker mtime; epoch when no digest has ever run."""
    try:
        return MARKER_PATH.stat().st_mtime
    except OSError:
        return 0.0


def count_new_unreviewed(since: float) -> int:
    """Unreviewed findings (*.md, no .reviewed sibling) with mtime > since."""
    if not FINDINGS_DIR.is_dir():
        return 0
    count = 0
    for finding in FINDINGS_DIR.glob("*.md"):
        if finding.with_suffix(".reviewed").exists():
            continue
        try:
            if finding.stat().st_mtime > since:
                count += 1
        except OSError:
            continue
    return count


def _run_start_timestamps(lane: str = "digest") -> list[float]:
    """Timestamps of run_start ledger events belonging to `lane`. The ledger
    is SHARED with the frontier loop (frontier_gate.py): this gate's cap and
    cooldown are the DIGEST lane's budget pool only — a frontier run must not
    consume a digest slot nor arm the digest cooldown, and vice versa
    (arch-soundness review C2: the lane-blind pool was the first silent
    break). Events without a `lane` field predate the vocabulary and are
    digest by definition. Envelope key is `type` with `event` tolerated
    (B2 rename, readers accept both). Corrupt lines are skipped — a damaged
    ledger must never wedge the gate."""
    if not LEDGER_PATH.is_file():
        return []
    try:
        lines = LEDGER_PATH.read_text().splitlines()
    except OSError:
        return []
    stamps: list[float] = []
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
        if (event.get("lane") or "digest") != lane:
            continue
        ts = event.get("ts")
        if isinstance(ts, (int, float)):
            stamps.append(float(ts))
    return stamps


def runs_in_trailing_week(now: float) -> int:
    return sum(1 for ts in _run_start_timestamps() if now - ts < TRAILING_WINDOW_SEC)


def newest_run_start_age(now: float) -> float | None:
    stamps = _run_start_timestamps()
    if not stamps:
        return None
    return now - max(stamps)


def lock_held_by_live_pid() -> bool:
    """True when the shared selffix/gardener run mutex is held by a live
    process. A dead holder reads as free — gardener-run.sh steals it the same
    way selffix-run.sh does."""
    if not RUN_LOCK_DIR.is_dir():
        return False
    try:
        pid = int((RUN_LOCK_DIR / "pid").read_text().strip())
    except (OSError, ValueError):
        # Lock dir without a readable pid: mid-acquisition or corrupt. Treat
        # as held — the wrapper's atomic re-check is the authority.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _newest_mtime(directory: Path, pattern: str) -> float:
    try:
        return max((p.stat().st_mtime for p in directory.glob(pattern)), default=0.0)
    except OSError:
        return 0.0


def producer_warnings() -> list[str]:
    """Producer-liveness asserts (arch-soundness review 2026-06-11 A1).

    Two checks, both advisory — they never change the gate decision:
      hooks_missing  — an expected SessionEnd hook command is absent from
                       settings.json (the surface that silently dropped the
                       selffix hook on 2026-05-25).
      producer_stale — sessions keep closing (closed/*.json is fresh) but the
                       newest finding lags by > PRODUCER_STALE_GAP_SEC: the
                       producer is dead, not the workload quiet. No closed
                       records ⇒ no activity ⇒ no signal either way.
    """
    warnings: list[str] = []
    try:
        hooks = json.loads(SETTINGS_PATH.read_text()).get("hooks", {})
        session_end_cmds = " | ".join(
            h.get("command") or ""
            for block in hooks.get("SessionEnd", []) for h in block.get("hooks", []))
        for alternatives in EXPECTED_SESSION_END_HOOKS:
            if not any(needle in session_end_cmds for needle in alternatives):
                wanted = " / ".join(repr(n) for n in alternatives)
                warnings.append(f"hooks_missing {wanted} not wired in settings.json SessionEnd")
    except (OSError, ValueError):
        warnings.append(f"hooks_missing {SETTINGS_PATH} unreadable")
    newest_finding = _newest_mtime(FINDINGS_DIR, "*.md")
    newest_closed = _newest_mtime(CLOSED_DIR, "*.json")
    if newest_closed and newest_closed - newest_finding > PRODUCER_STALE_GAP_SEC:
        gap_h = (newest_closed - newest_finding) / 3600
        warnings.append(
            f"producer_stale newest finding lags newest closed session by {gap_h:.0f}h "
            f"(threshold {PRODUCER_STALE_GAP_SEC // 3600}h)")
    return warnings


def _notify(message: str) -> None:
    """Best-effort local notification; never blocks or fails the gate.

    No-ops under pytest (PYTEST_CURRENT_TEST, inherited by child processes):
    tests exec this script for REAL (test_module_toggle.py), where no
    monkeypatch can reach — without this guard every full-suite run fired
    real hooks_missing desktop notifications (2026-07-03)."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e",
             f'display notification "{message[:200]}" with title "gardener-gate"'],
            capture_output=True, timeout=10)
    except Exception:
        pass


def _warn_producer(now: float) -> None:
    """Log every warning each tick; notify at most once per throttle window."""
    warnings = producer_warnings()
    if not warnings:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with GATE_LOG_PATH.open("a") as f:
        for warning in warnings:
            f.write(f"{stamp}  WARN  {warning}\n")
    try:
        last_notified = WARN_MARKER_PATH.stat().st_mtime
    except OSError:
        last_notified = 0.0
    if now - last_notified > WARN_NOTIFY_THROTTLE_SEC:
        suffix = f" (+{len(warnings) - 1} more)" if len(warnings) > 1 else ""
        _notify(warnings[0] + suffix)
        WARN_MARKER_PATH.touch()


def _selffix_log(verb: str, sid: str, detail: str) -> None:
    """One line in selffix-trigger.log's format, honoring its debug toggle —
    the retry lifecycle stays traceable next to the rest of the selffix chain."""
    if not (any(p.exists() for p in SELFFIX_DEBUG_PATHS) or os.environ.get("SELFFIX_DEBUG") == "1"):
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with SELFFIX_LOG_PATH.open("a") as f:
            f.write(f"{stamp}  {verb}  {sid}  {detail}\n")
    except OSError:
        pass


def limit_bricked(now: float) -> bool:
    """True when stale_monitor's banner state says the account is rate-limit
    bricked RIGHT NOW: a .manager-limited-* flag with fresh mtime (the monitor
    refreshes it every limited scan and removes it on clear; 300s ≈ 5× that
    cadence). Absent flags read as not-bricked — fail-open; a wrong guess
    costs one failed spawn that selffix-run.sh classifies as retry:exhausted."""
    try:
        for flag in ORCHESTRATOR_DIR.glob(".manager-limited-*"):
            if now - flag.stat().st_mtime < BRICK_FRESH_SEC:
                return True
    except OSError:
        pass
    return False


def spawn_retry(transcript: str, sid: str) -> None:
    """Launch selffix-run.sh detached for a queued retro. SELFFIX_RETRY_ATTEMPT=1
    is the retry-once cap: the run never re-enqueues itself on failure."""
    subprocess.Popen(
        ["bash", str(SELFFIX_RUN_SCRIPT), transcript, sid],
        env={**os.environ, "SELFFIX_RETRY_ATTEMPT": "1"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def process_retry_queue(now: float) -> bool:
    """Selffix durable-retry pre-step: at most ONE spawn per tick, oldest
    entry first. NOT an analyst lane — no ledger run_start, no cap/cooldown
    consumption; this is selffix producer plumbing riding the hourly tick
    (the BOUNDARY invariant above is about analyst runs and is unchanged).
    Returns True when a retry spawned: the caller skips this tick's digest
    spawn so the two never contend on the analyst-run mutex.

    Fail-open everywhere: garbage entries are dropped with a log line, never
    wedge the gate; entries are deleted BEFORE spawning so no failure mode
    can loop.

    Because main() runs the pre-step only past its stop/lock decision
    exclusions, the gardener stop file also pauses retry consumption
    (entries accumulate inert until the loop resumes)."""
    if not RETRY_DIR.is_dir():
        return False
    try:
        entries = sorted(RETRY_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return False
    for path in entries:
        try:
            entry = json.loads(path.read_text())
            sid = entry["sid"]
            transcript = str(entry["transcript_path"])
            attempts = int(entry.get("attempts", 0))
        except Exception:
            _selffix_log("retry:dropped", path.stem, "reason=garbage")
            path.unlink(missing_ok=True)
            continue
        if not Path(transcript).is_file():
            _selffix_log("retry:dropped", sid, "reason=transcript-missing")
            path.unlink(missing_ok=True)
            continue
        if attempts >= 1:
            _selffix_log("retry:dropped", sid, "reason=attempts-exhausted")
            path.unlink(missing_ok=True)
            continue
        if limit_bricked(now):
            # Don't burn the single attempt into a live brick; the entry
            # stays queued for a later tick.
            _selffix_log("retry:deferred", sid, "reason=brick")
            return False
        path.unlink(missing_ok=True)
        try:
            spawn_retry(transcript, sid)
        except OSError as e:
            _selffix_log("retry:dropped", sid, f"reason=spawn-failed {e}")
            return False
        _selffix_log("retry:spawn", sid, "attempts=1")
        return True
    return False


def decide(now: float, force: bool) -> tuple[str, dict]:
    """Gate decision per the docstring's order. Returns (decision, detail)."""
    detail: dict = {"force": force}
    if any(p.exists() for p in STOP_PATHS):
        return ("refused_stopped" if force else "stopped"), detail
    if lock_held_by_live_pid():
        return "locked", detail
    runs = runs_in_trailing_week(now)
    detail["runs_in_week"] = runs
    if not force:
        last_age = newest_run_start_age(now)
        if last_age is not None and last_age < MIN_RUN_GAP_SEC:
            detail["last_run_age_sec"] = int(last_age)
            return "cooldown", detail
        if runs >= MAX_RUNS_PER_WEEK:
            return "cap", detail
    if force:
        return "force", detail
    marker = _marker_mtime()
    fresh = count_new_unreviewed(marker)
    detail["new_unreviewed"] = fresh
    detail["marker_age_days"] = round((now - marker) / 86400, 2) if marker else None
    if fresh >= K_THRESHOLD:
        return "accum", detail
    if marker and (now - marker) >= FLOOR_DAYS * 86400 and fresh >= 1:
        return "floor", detail
    if not marker and fresh >= 1:
        # No digest has ever run and material exists below K: let the weekly
        # floor semantics apply from epoch — fire rather than wait forever.
        return "floor", detail
    return "no_material", detail


def spawn_run(trigger: str) -> None:
    """Launch gardener-run.sh fully detached; the gate never waits on it."""
    subprocess.Popen(
        ["bash", str(RUN_SCRIPT), "--trigger", trigger],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _log_gate(decision: str, detail: dict, spawned: bool) -> None:
    GATE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = json.dumps(detail, sort_keys=True)
    with GATE_LOG_PATH.open("a") as f:
        f.write(f"{stamp}  {decision}  spawned={spawned}  {payload}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gardener trigger gate (LLM-free).")
    parser.add_argument("--force", action="store_true",
                        help="Bypass accumulation/floor/cap. Still refuses under "
                             "the stop file and the live run mutex.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the decision without spawning a run.")
    args = parser.parse_args(argv)

    if not gardener_module_enabled():
        # gardener=false: no-op the whole gate (design-gate). No dirs created,
        # no gate.log line, no producer-liveness WARNs — a clean off switch.
        print("gardener-gate: module-off ([modules] gardener=false) — no-op")
        return EXIT_OK

    for d in (GARDENER_DIR, DIGESTS_DIR, PROPOSALS_DIR):
        d.mkdir(parents=True, exist_ok=True)

    now = time.time()
    decision, detail = decide(now, force=args.force)
    retry_spawned = False
    if (not args.force and not args.dry_run
            and decision not in ("stopped", "refused_stopped", "locked")):
        try:
            retry_queue = len(list(RETRY_DIR.glob("*.json")))
        except OSError:
            retry_queue = 0
        if retry_queue:
            detail["retry_queue"] = retry_queue
            retry_spawned = process_retry_queue(now)
            detail["retry_spawned"] = retry_spawned
    should_spawn = (decision in ("accum", "floor", "force")
                    and not args.dry_run and not retry_spawned)
    if should_spawn:
        spawn_run(decision)
    _log_gate(decision, detail, spawned=should_spawn)
    _warn_producer(now)
    print(f"gardener-gate: {decision} spawned={should_spawn} {json.dumps(detail, sort_keys=True)}")

    if decision == "refused_stopped":
        print(f"gardener-gate: stopped — remove {STOP_PATHS[0]} first", file=sys.stderr)
        return EXIT_REFUSED_STOPPED
    if decision == "locked" and args.force:
        print("gardener-gate: run mutex held by a live process — retry when the "
              "current selffix/gardener run finishes", file=sys.stderr)
        return EXIT_FORCE_LOCKED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
