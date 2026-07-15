#!/usr/bin/env python3
"""One-shot scan for stale orchestrator state.

STDLIB-ONLY BY DESIGN: this file doubles as a standalone deployed script
(~/.claude/scripts/stale_monitor.py, copied by setup.sh) and a package module
(`python -m dockwright.stale_monitor`). Do not add package imports.

Prints one line per stale worker/question/auto-close:
  STALE_PROCESSING <name> (<minutes>min)
  STALE_QUESTION <question_id> worker=<name> (<minutes>min)
  NUDGED <name> (<minutes>min[ rate-limited] | limit-reset)
  RESUMED <name>
  AUTOCLOSED <name> idle <minutes>min
  SWITCHED account <from>→<to> (worker <name> limited | manager <name> limited)
  limit cleared <HH:MM> — while down: <N> workers stalled, <M> nudged, <K> done events

Definition of stale:
  - active/<sid>.json with state="processing" AND last activity older than
    PROCESSING_THRESHOLD_SEC, where last activity = max(active-record mtime, transcript
    mtime). The record is rewritten on session_start, user_prompt_submit, and stop_hook
    (turn start); the transcript jsonl is appended on every event the CLI emits, so its
    mtime IS the last-append time. A long busy turn keeps a fresh transcript and is not
    stale; a wedged worker (429-exhausted CLI, permission-gated, crashed stream) goes
    silent. When no transcript resolves (e.g. codex sessions dir missing), activity falls
    back to the record mtime — the old turn-age behavior, never blind. Known assumption:
    a single long generation with zero tool calls appends nothing for its whole duration;
    at the 30min default threshold that cannot realistically false-positive, but lowering
    CLAUDE_ORCH_STALE_PROCESSING_MIN below ~10min re-exposes a small version of it.
  - questions/<manager>/<qid>.json (or legacy questions/<qid>.json) older than
    QUESTION_THRESHOLD_SEC since asked_at — manager hasn't answered.
  - active/<sid>.json (workers only) with state="idle" AND last_turn_at older than
    IDLE_THRESHOLD_SEC, AND no pending question for the worker: archive the record to
    closed/<sid>.json (preserving sid/name/cwd/summary + closed_at), unlink the active
    record, then close the tmux window via the terminal driver so
    Claude Code's SessionEnd hook fires natively (runs selffix-trigger.sh + writes the
    closed/<sid>.json via orchestrator session-end). The active unlink happens BEFORE
    the close so the in-tab session-end hook sees no active record and doesn't overwrite
    our "idle>...s" closed_reason with "session_end". Override the threshold via env
    CLAUDE_ORCH_IDLE_TTL_HOURS. The manager's existing monitor surfaces the AUTOCLOSED
    line; resume the session later with `resume_worker(name)`.

Edge-triggered alarms: STALE_PROCESSING and STALE_QUESTION lines emit only when the
elapsed time crosses a doubling threshold (30, 60, 120, ... min for processing by
default; base via env CLAUDE_ORCH_STALE_PROCESSING_MIN; 2,
4, 8, 16, ... min for questions). Per-key last-emitted threshold is persisted to
~/.claude/dockwright/.stale-emitted.json. A wedged worker therefore pages once at
30min, then 60, 120 — never on every 60s scan.

Auto-nudge (opt-in via CLAUDE_ORCH_AUTONUDGE=1, default OFF): every stall
detection for a worker — a threshold crossing of the silence ladder, or a rate-limit
signature in the transcript's last assistant message at >=5min of transcript silence —
types "resume your task" into the worker's pane (same bracketed-paste send-text
+ Enter path send_manager_to_worker uses) and emits NUDGED instead of paging.
Nudges REPEAT while the worker stays silent: at each ladder crossing (30/60/120min
by default, then every NUDGE_REPEAT_INTERVAL_MIN beyond), and the early 429 path
once per processing stretch (a delivered nudge submits a prompt → fresh stretch →
~5min of new silence re-arms it) — except while a banner-scheduled nudge is
armed for the worker, which suppresses the per-stretch lane (see below).
Repetition is safe exactly because staleness is
transcript-activity age: busy workers are never stale, so repeated nudges only ever
hit silent ones — and the first nudge after an org-wide 429 resets revives the
whole fleet with no human in the loop. A typed nudge is an attempt, not a
delivery (a CLI sitting on a limit banner swallows input without starting a
turn): transcript growth after the nudge is the only delivery confirmation,
surfaced once as RESUMED <name>; until it happens the ladder keeps re-nudging.
Workers with a pending question are never
nudged; nothing is ever killed; nudge-ineligible workers (no window id, pending
question, autonudge off) page STALE_PROCESSING as before.

Banner-scheduled nudge: the session-limit banner carries a reset time ("resets
2:20am (Asia/Novosibirsk)"). When the worker fast-path detects it, a second
nudge is scheduled for reset+2min (`scheduled:<sid>` in the emitted state) on
top of the ladder — the ladder stays the universal catch-all because the
wording is fragile (it changed once already) and parsing is best-effort. A
parse landing further out than any real session window (>6h) means the banner's
wall-time already passed and rolled to tomorrow — a stale banner, treated as a
parse failure. While the schedule is armed, the ~5min per-stretch lane is
suppressed for that worker: during a hard multi-hour session limit every
delivered nudge just retries into the same banner (fresh stretch + false
RESUMED) and re-fires ~5min later — the 2026-06-11 storm produced 226 NUDGED /
192 RESUMED in one 3.3h window this way. A due schedule self-cancels only when
the worker GENUINELY moved since scheduling: activity past the stored baseline
AND the transcript no longer ending on a limit banner (the baseline is captured
pre-nudge, so a delivered nudge's failed retry always overshoots it while
leaving a fresh banner as the final text — still bricked, still fire). A
swallowed or cancelled scheduled nudge is re-covered by the ladder, and the
per-stretch lane re-arms once the schedule is consumed.

Managers (scoped runs only): a manager bricked on a limit banner stays
state="processing" and is deaf to task-notifications. The owning manager's own
record gets ONE limit-recovery path — banner detection (after 2min of
transcript silence; STRICT matching: short text, signature near the start, so
a manager merely quoting a worker's banner never reads as limited) schedules
"rate limit cleared — check list_workers and queued events, resume
orchestration" for reset+2min, with a flat 10min retry re-arm while the banner
persists (managers have no silence ladder; re-parsing a stale banner after a
swallowed fire would schedule tomorrow). Manager nudges are gated on
CLAUDE_ORCH_AUTONUDGE like worker nudges; event coalescing below is not (a
suppressed line was a wasted wake attempt regardless). Managers stay
excluded from the ladder, STALE_PROCESSING, the 5-min fast-path, and autoclose.

Account auto-switch (pool of two per-config-dir logins): the pointer file
(account-active) names the account new spawns authenticate as (a = default
~/.claude, b = ~/.claude-b via CLAUDE_CONFIG_DIR; each config dir has its own
keychain login, no injected token). When a limit banner bricks a worker or the
owning manager on the pointer account, the scan flips the pointer to the other
account — guarded by a flip cooldown (env CLAUDE_ORCH_FLIP_COOLDOWN_MIN,
default 30min), a keychain-unlocked probe (a recovery tab opening onto a locked
keychain would prompt SecurityAgent on claude's own per-config-dir login read),
and the other account's own brick window. The
worker-site banner read
is hoisted above the nudge ladder, so a flip can fire at any silence past the
5min floor — including past the processing threshold and while a
banner-scheduled nudge is armed, where the 5-min lane is unreachable — and
touches none of the nudge lane's dedup keys. A flip surfaces as a SWITCHED
line: live when the manager is healthy (its wake-up to kill+resume bricked
workers), folded into the recovery rollup when the manager is itself limited.
A flipped manager additionally gets a fresh recovery tab running
/manager-takeover-recovery on the new account; an UNSTAMPED manager (anything
alive at pool activation) whose own flip attempt is blocked also gets one
when a flip recently landed ON the current pointer (the day-one recent-flip
heuristic — see _recent_flip_landed_on). Recovery launches require a usable
target keychain (same locked-state probe as guard 3) and are bounded
once+once by the emitted-state guard key, with the ledger's recovery-launch
count as the durable backstop (_ledger_recovery_launches). Every brick
episode, flip, unparsed banner, and recovery launch is appended to
account-flips.jsonl.
Known residual: an UNSTAMPED worker still bricked on the old account resolves
its account to the post-flip pointer letter on later scans, recording phantom
bricks against the healthy account — once the old account's brick window
expires those can drive a spurious flip-back (~6h cadence), repeating until
the manager performs its documented kill+resume of bricked workers on the
SWITCHED wake-up. That duty is the mitigation: resume bricked workers
promptly; pool-spawned records carry the account stamp and are immune.
DORMANCY INVARIANT: `rm account-active` is a full disable — no state writes,
no ledger lines, no flips, no recovery launches; every lane behaves exactly
as with no pool at all. If BOTH accounts are bricked, an already-flipped
manager's recovery launch targets the (also-bricked) pointer — up to two dead
recovery tabs, bounded by the once+once launch/relaunch guards (emitted-state
key, ledger-backed); the AUTONUDGE
reset-time nudge remains the catch-all. Deliberately ungated: the pointer's
state entry can carry phantom bricks (zombie-unstamped-worker residual), and
gating on it would suppress legitimate recoveries.

Event coalescing while the owning manager is limited: every printed line is a
task-notification = a failed wake attempt at a bricked manager. On positive
limited-detection (and nothing else) the scan buffers its event lines as
aggregate counters (limited_buffer in the emitted state) and maintains
.manager-limited-<name>, which also holds the monitor.py question/done/turn-end
scans (they mark nothing seen, so those replay in full after recovery). The
flag mtime is refreshed every limited scan; the monitor.py readers treat an
mtime older than ~10min as a dead stale loop and fail open (ignore + unlink) —
a crashed monitor must never leave the manager permanently deaf. On the
first scan where detection no longer holds, ONE rollup line summarizes the
window ("limit cleared HH:MM — while down: ...") and the normal stream resumes;
buffered STALE_PROCESSING/STALE_QUESTION rungs are un-burned at recovery (their
dedup keys dropped) so the first post-recovery reminder fires live instead of
waiting for the next doubling.

The STALE_PROCESSING dedup key embeds the processing-stretch start (the active
record's mtime, which the Stop / user_prompt_submit hooks rewrite each turn), not just
the sid: `processing:<sid>:<mtime>`. So a fresh processing stretch — a new long turn
after the worker went idle — re-arms the threshold clock with a brand-new key even if the
monitor never observed the intervening idle state between two 60s scans. The threshold
ladder itself runs on activity age (transcript silence), so within one long turn each
new silence episode re-arms after activity resumes — its key is pruned the moment the
worker is seen non-stale. Keys for
workers/questions that are no longer stale on a given run are dropped from the file
(a previous stretch's key is pruned the moment the worker's mtime changes). AUTOCLOSED
is one-shot by nature (record disappears once auto-closed) and isn't edge-debounced
here.

When --manager NAME is given, the scan only considers records whose
parent_manager_name is NAME. Null-parent legacy records are invisible to scoped
runs; recovery is `_backfill_legacy_workers` on a single-manager boot. Without
--manager, behavior is global (back-compat) — every record is in scope.

Exits 0 silently if nothing is stale.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from dockwright.terminal import get_driver as _get_driver
except Exception:  # pragma: no cover - venv editable install expected in prod
    _get_driver = None


def _env_positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


PROCESSING_THRESHOLD_MIN = _env_positive_int("CLAUDE_ORCH_STALE_PROCESSING_MIN", 30)
QUESTION_THRESHOLD_MIN = 2
PROCESSING_THRESHOLD_SEC = PROCESSING_THRESHOLD_MIN * 60
QUESTION_THRESHOLD_SEC = QUESTION_THRESHOLD_MIN * 60
try:
    _IDLE_HOURS = float(os.environ.get("CLAUDE_ORCH_IDLE_TTL_HOURS", "2"))
except ValueError:
    _IDLE_HOURS = 2.0
IDLE_THRESHOLD_SEC = int(_IDLE_HOURS * 3600)
AUTOCLOSE_CADENCE_SEC = 3600

HOME = Path(os.environ.get("HOME", ""))


def _prefer_new(new: Path, legacy: Path) -> Path:
    # deprecated, one release: legacy fallback while orchestrator-era state migrates
    if new.exists():
        return new
    if legacy.exists():
        return legacy
    return new


ROOT = _prefer_new(HOME / ".claude" / "dockwright", HOME / ".claude" / "orchestrator")
_LEGACY_ROOT = HOME / ".claude" / "orchestrator"  # deprecated, one release
ACTIVE = ROOT / "active"
QUESTIONS = ROOT / "questions"
CLOSED = ROOT / "closed"
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CODEX_SESSIONS = HOME / ".codex" / "sessions"
# entries must be lowercase — matched against text.lower(). RATE_LIMIT_SIGNATURES
# is the DETECTION set: a transcript ending on either shape — the org/server
# throttle ("Server is temporarily limiting requests …") or the personal session
# limit ("You've hit your session limit · resets …") — wedges a CC session and
# earns the nudge-ladder recovery. The session-limit signature deliberately starts
# after the apostrophe so the typographic-vs-ASCII variant can't break the match.
# The weekly-limit banner's wording is NOT covered yet — capture it when first
# seen (the unparsed-banner ledger events catch reset-clause drift in KNOWN
# banners; a genuinely new banner needs its signature added here).
# Detection unions this set with the 529 transient-server-error signature (see
# TRANSIENT_SERVER_ERROR_SIGNATURES) — so the detector is a union, not this tuple alone.
RATE_LIMIT_SIGNATURES = ("temporarily limiting requests", "hit your session limit")
# Detection ≠ brick. The server-side 429 throttle is org-wide and transient:
# flipping the account pointer can't escape it (both per-config-dir logins hit the
# same server) and the worker self-recovers via the nudge ladder once it eases —
# so a banner carrying either marker drives nudge recovery but NEVER a brick/flip
# (see _is_transient_throttle). Only a genuine per-account usage limit ("hit your
# session limit") bricks+flips. "not your usage limit" is Anthropic's own
# disambiguator on the 429 banner — unique to it, never in a usage-limit banner.
TRANSIENT_THROTTLE_SIGNATURES = ("temporarily limiting requests", "not your usage limit")
# HTTP 529 "Overloaded" is the same transient-class server-side error as the 429
# throttle above — org-wide, a flip can't escape it, self-clears — so it must drive
# nudge recovery but NEVER brick/flip. Kept as its own set so the 429 tuples stay
# 429-specific. The token is the server-emitted status+reason pair (drift-robust —
# the descriptive sentence rewords); its adjacency is unique enough to avoid false
# positives.
TRANSIENT_SERVER_ERROR_SIGNATURES = ("529 overloaded",)
# ---- auth-401 self-heal (concurrent-session OAuth collision, design 2026-06-14)
# A transient/server-side 401 bricks an interactive CC session the same way a
# rate-limit banner does (it latches "Please run /login" and never re-reads the
# keychain), but RATE_LIMIT_SIGNATURES doesn't match it — so without this the
# monitor never flagged it and a human had to /login. The STABLE structured
# signal is the assistant event's top-level `isApiErrorMessage:true` +
# `apiErrorStatus:401` (identical in TUI and headless transcripts); the human
# `text` drifts ("Invalid authentication credentials" on a server reject vs
# "Invalid bearer token" on a malformed token), so the phrase match below is
# only a drift-proof fallback for builds that omit apiErrorStatus. A rate-limit
# banner carries neither a 401 status nor 401 text, so the two classes are
# disjoint. Recovery is SAME-account kill+resume (a fresh process re-reads the
# keychain login) — NOT a flip: the other account is equally exposed to a
# server blip. Bounded: after AUTH_401_MAX_ATTEMPTS failed same-account resumes
# within AUTH_401_WINDOW_SEC the login is suspect → escalate (flip + page the
# human to /login). Note: with per-config-dir logins there is no `claude
# setup-token` re-mint, so the GH#48786 sibling-token revocation cascade no
# longer applies — a persistent 401 means the login is genuinely revoked and
# the fix is /login, not a re-mint.
AUTH_FAILURE_SIGNATURES = ("api error: 401", "please run /login")
AUTH_401_WINDOW_SEC = 5 * 60        # M: attempts older than this start a fresh episode
AUTH_401_MAX_ATTEMPTS = 2           # N: same-account resume attempts before escalating
# The AUTH_401 worker trigger re-fires on this cadence while the worker stays
# 401'd (same uuid), so a missed or coalesced-then-recovered event reaches a
# live manager — decoupled from the uuid-deduped attempt count (re-emits never
# inflate it). Mirrors the rate-limit 5-min re-nudge floor.
AUTH_401_REEMIT_SEC = 5 * 60
AUTONUDGE = os.environ.get("CLAUDE_ORCH_AUTONUDGE") == "1"
# Non-urgent event kinds ride the notify outbox instead of paging a dedicated
# wake: monitor.py's scans drain the outbox whenever they are already
# printing, and the timeout flush below bounds the wait. AUTOCLOSED is
# informational by nature — the worker was already idle 2h and the durable
# closed/<sid>.json record exists regardless.
OUTBOX_DIVERT_KINDS = ("autoclosed",)
OUTBOX_MAX_HOLD_SEC = _env_positive_int("CLAUDE_ORCH_OUTBOX_MAX_HOLD_SEC", 1800)
# Worker-pane nudges carry the manager marker: worker.core.md reads an UNMARKED
# pane message as engineer-direct, and a daemon nudge is orchestration, not the
# human. Literal (not imported — this file is standalone/stdlib-only); kept in
# sync with mcp_server.MANAGER_MARKER by test_worker_nudge_marked_manager_nudge_unmarked.
# MANAGER_NUDGE_TEXT stays unmarked: it types into the MANAGER's own pane, which
# is the human's console and has no such attribution rule.
NUDGE_TEXT = "[MANAGER] resume your task"
MANAGER_NUDGE_TEXT = "rate limit cleared — check list_workers and queued events, resume orchestration"
RATE_LIMIT_NUDGE_MIN = 5
RATE_LIMIT_NUDGE_SEC = RATE_LIMIT_NUDGE_MIN * 60
NUDGE_REPEAT_INTERVAL_MIN = 60
# Schedule the post-limit nudge a little past the banner's reset time so the
# limit has actually lifted when the typed prompt submits.
SCHEDULED_NUDGE_DELAY_SEC = 120
# Session-limit windows are 5h; a parsed reset further out than this means the
# banner's wall-time already passed and rolled to tomorrow — a stale banner,
# treated as a parse failure (ladder / flat retry take over).
MAX_PLAUSIBLE_RESET_SEC = 6 * 3600
# Strict banner matching for the manager path: a real banner is a short
# one-liner with the signature near its very start (offsets 7, 10, and 11 in the
# three known banners); a manager message QUOTING a banner — even a short relay like
# "worker-1: You've hit your session limit …" (offset 17) — has it deeper.
MAX_BANNER_LEN = 200
MAX_BANNER_SIG_OFFSET = 12
# Managers have no silence ladder (AskUserQuestion legitimately holds their
# turns open for hours), so the limit-recovery nudge retries on a flat cadence
# while the banner persists — also the catch-all when the reset time is
# unparseable.
MANAGER_NUDGE_RETRY_SEC = 10 * 60
# Managers legitimately sit processing with silent transcripts (AskUserQuestion);
# only read the transcript tail for banner detection after this much silence.
MANAGER_LIMIT_CHECK_FLOOR_SEC = 120
# "resets 2:20am (Asia/Novosibirsk)" — hour, optional :minutes, am/pm, IANA zone.
_RESET_CLAUSE_RE = re.compile(
    r"resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*\(([^)]+)\)", re.IGNORECASE)


# ---- account auto-switch (pool of two per-config-dir logins; design 2026-06-15) --
# The pointer selects which account (a = default ~/.claude, b = ~/.claude-b via
# CLAUDE_CONFIG_DIR) new spawns authenticate as — each config dir has its own
# keychain login (no injected token). DORMANCY INVARIANT: every helper below
# no-ops unless the pointer file holds a valid letter — `rm account-active` is a
# full disable (no state writes, no ledger lines, no flips, no recovery launches).
ACCOUNT_ACTIVE = ROOT / "account-active"
ACCOUNT_LEDGER = ROOT / "account-flips.jsonl"
ACCOUNT_STATE = ROOT / "account-state.json"
ACCOUNT_LOCK = ROOT / ".account-flip.lock"
FLIP_COOLDOWN_SEC = _env_positive_int("CLAUDE_ORCH_FLIP_COOLDOWN_MIN", 30) * 60
TAKEOVER_GUARD_SEC = 300          # recovery tab must take over within this window
BRICK_EPISODE_GAP_SEC = 600       # banner unseen this long ⇒ next sighting is a new episode


def _pool_account() -> str | None:
    """rstrip("\\n") only — NOT .strip(): must match spawner._pick_account /
    spawner._active_account byte-for-byte so the flip lane and the spawn gate
    agree on which pointer is valid (a whitespace-padded letter would word-split
    inside the shell-side $(cat) and yield a lying account stamp)."""
    try:
        letter = ACCOUNT_ACTIVE.read_text().rstrip("\n")
    except Exception:
        return None
    return letter if letter in ("a", "b") else None


def _account_of(record: dict, pool_letter: str) -> str:
    stamped = record.get("account")
    return stamped if stamped in ("a", "b") else pool_letter


def _other_account(letter: str) -> str:
    return "b" if letter == "a" else "a"


def _keychain_unlocked() -> bool:
    """True if the login keychain is unlocked (`security show-keychain-info`
    rc==0). Gate for flips / recovery-tab launches: a recovery tab opening onto
    a locked keychain would prompt SecurityAgent on claude's own per-config-dir
    login read. Conservative retention — the old token-read freeze reason is
    gone; this new reason is unspiked, but the probe only no-ops when unlocked.
    No item probe (no token to probe)."""
    try:
        return subprocess.run(["security", "show-keychain-info"],
                              capture_output=True, timeout=5, check=False).returncode == 0
    except Exception:
        return False


def _account_config_prefix(letter: str) -> str:
    """Env prefix for a manager tab on `letter` (mirrors spawner, inline — this
    standalone script can't import the package). 'a' -> default ~/.claude (no
    CLAUDE_CONFIG_DIR); other letter -> CLAUDE_CONFIG_DIR=~/.claude-<letter> iff
    its .claude.json is healthy (has the orchestrator MCP), else fall back to the
    default login with a truthful effective stamp 'a'. Workers build/maintain
    ~/.claude-<letter>; here we only CHECK.

    KNOWN FAILURE MODE: recovery onto a non-default letter assumes a worker has
    already built ~/.claude-<letter>. If account `a` bricks before any worker
    built the farm, the flip a->b launches a recovery manager that falls back
    here to the DEFAULT login stamped `a` — i.e. onto the just-bricked account,
    which may re-brick. We do NOT gate the flip on farm health (that would force
    every flip test to seed a healthy b-farm). Instead this is bounded by the
    once+once recovery-launch guard and self-heals once a worker rebuilds the
    farm; the fallback emits the stderr warning below so the degradation is
    observable."""
    effective = letter
    config_dir = None
    if letter != "a":
        farm = Path(os.path.expanduser(f"~/.claude-{letter}"))
        cj = farm / ".claude.json"
        try:
            data = json.loads(cj.read_text())
            servers = (data.get("mcpServers") or {}) if isinstance(data, dict) else {}
            # claude-orchestrator: one-release legacy MCP key recognition
            if "dockwright" in servers or "claude-orchestrator" in servers:
                config_dir = farm
            else:
                effective = "a"
        except Exception:
            effective = "a"
        if config_dir is None:
            print(f"stale_monitor: account-{letter} farm ~/.claude-{letter}/.claude.json "
                  f"not healthy; recovery falls back to the DEFAULT login (stamp a) — the "
                  f"recovery tab may land on the bricked account until a worker rebuilds "
                  f"the farm", file=sys.stderr)
    parts = []
    if config_dir is not None:
        parts.append(f"CLAUDE_CONFIG_DIR={shlex.quote(str(config_dir))}")
    parts.append(f"CLAUDE_ORCH_ACCOUNT={shlex.quote(effective)}")
    return " ".join(parts) + " "


@contextmanager
def _flip_lock():
    """Serializes read-check-write across concurrent per-manager scans.

    Not reentrant: helpers must be called sequentially, never nested — a nested
    flock on a fresh fd of the same file self-deadlocks the scan."""
    ACCOUNT_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(ACCOUNT_LOCK, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def _load_account_state() -> dict:
    state = _load(ACCOUNT_STATE) or {}
    if not isinstance(state.get("accounts"), dict):
        state["accounts"] = {}
    return state


def _append_account_ledger(entry: dict) -> None:
    try:
        ACCOUNT_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(ACCOUNT_LEDGER, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        print(f"stale_monitor: account ledger append failed ({e})", file=sys.stderr)


def _entry_bricked(entry, now: int) -> bool:
    if not isinstance(entry, dict):
        return False
    reset_ts = entry.get("reset_ts")
    if isinstance(reset_ts, (int, float)):
        return now < reset_ts
    bricked_at = entry.get("bricked_at")
    return isinstance(bricked_at, (int, float)) and now - bricked_at < MAX_PLAUSIBLE_RESET_SEC


def _other_account_bricked(state: dict, other: str, now: int) -> bool:
    return _entry_bricked(state.get("accounts", {}).get(other), now)


def _record_brick(account: str, reset_ts, source: str, now: int) -> None:
    """Track per-account brick episodes for the flip guards. Ledger line only on
    NEW episodes (first sight, banner gone >gap, or stored reset already passed)."""
    try:
        with _flip_lock():
            state = _load_account_state()
            entry = state["accounts"].get(account)
            stale_entry = (isinstance(entry, dict)
                           and isinstance(entry.get("last_seen"), (int, float))
                           and now - entry["last_seen"] > BRICK_EPISODE_GAP_SEC)
            new_episode = not _entry_bricked(entry, now) or stale_entry
            if new_episode:
                entry = {"bricked_at": now}
            entry["last_seen"] = now
            if reset_ts is not None:
                entry["reset_ts"] = reset_ts
            state["accounts"][account] = entry
            # State first, ledger second: if the state write fails persistently,
            # episode detection re-fires on every scan — the reversed order would
            # append an unbounded stream of duplicate brick lines; this way the
            # damage caps at one missing ledger line.
            _write_json_atomic(ACCOUNT_STATE, state)
            if new_episode:
                _append_account_ledger({"ts": now, "event": "brick", "account": account,
                                        "reset_ts": reset_ts, "source": source,
                                        "by": "stale_monitor"})
    except Exception as e:
        print(f"stale_monitor: brick recording failed ({e})", file=sys.stderr)


def _record_auth_401(account: str, uuid: str | None, now: int) -> str:
    """Per-account auth-401 attempt counter (uuid-deduped). Returns the action:
    "duplicate" (this exact 401 was already acted on — the resume hasn't fired/
    cleared yet, so don't re-trigger or inflate the count), "recover" (a fresh
    401 within the bound — trigger a SAME-account kill+resume), or "escalate"
    (more than AUTH_401_MAX_ATTEMPTS failed same-account resumes inside
    AUTH_401_WINDOW_SEC — the credential is suspect, flip + page).

    Per-ACCOUNT (not per-session) aggregation is the right grain for "is this
    token dead": two sessions on one account 401'ing in lockstep is strong
    evidence the token — the only thing they share — is the problem, while the
    incident's one-each across two accounts is a shared server blip that
    same-account recovery clears. State lives in its own ACCOUNT_STATE namespace
    (`auth_401`) so it never perturbs the rate-limit brick guards (`accounts`).
    Crash-proof: any failure reads as "recover" (act, don't escalate) — the safe
    default, and it mirrors _record_brick's flat _flip_lock usage (no nesting:
    the caller does _maybe_flip_account separately on escalate)."""
    try:
        with _flip_lock():
            state = _load_account_state()
            namespace = state.setdefault("auth_401", {})
            entry = namespace.get(account)
            in_window = (isinstance(entry, dict)
                         and isinstance(entry.get("last_seen"), (int, float))
                         and now - entry["last_seen"] <= AUTH_401_WINDOW_SEC)
            if in_window:
                seen = entry.get("uuids") if isinstance(entry.get("uuids"), list) else []
                if uuid is not None and uuid in seen:
                    # Same 401 still showing (the resume hasn't fired/cleared) —
                    # refresh the episode clock so a persistent unhandled 401
                    # doesn't roll past the window and get re-counted as a fresh
                    # attempt, but DON'T increment: it's one unhandled attempt.
                    entry["last_seen"] = now
                    namespace[account] = entry
                    _write_json_atomic(ACCOUNT_STATE, state)
                    return "duplicate"
                attempts = _safe_int(entry.get("attempts")) + 1
                uuids = (seen + [uuid])[-8:] if uuid is not None else seen
            else:
                attempts = 1
                uuids = [uuid] if uuid is not None else []
            namespace[account] = {"attempts": attempts, "last_seen": now, "uuids": uuids}
            _write_json_atomic(ACCOUNT_STATE, state)
            return "recover" if attempts <= AUTH_401_MAX_ATTEMPTS else "escalate"
    except Exception as e:
        print(f"stale_monitor: auth-401 record failed ({e})", file=sys.stderr)
        return "recover"


def _maybe_flip_account(bricked_account: str, reason: str, now: int) -> str | None:
    """Flip the pointer to the other account iff ALL guards pass. Returns the
    new letter, or None (already flipped / cooling down / other unusable)."""
    try:
        with _flip_lock():
            pointer = _pool_account()
            if pointer is None or pointer != bricked_account:
                return None
            other = _other_account(pointer)
            state = _load_account_state()
            last_flip = state.get("last_flip") or {}
            last_ts = last_flip.get("ts")
            if isinstance(last_ts, (int, float)) and now - last_ts < FLIP_COOLDOWN_SEC:
                return None
            if not _keychain_unlocked():
                return None
            if _other_account_bricked(state, other, now):
                return None
            tmp = ACCOUNT_ACTIVE.with_suffix(".tmp")
            tmp.write_text(other + "\n")
            os.replace(tmp, ACCOUNT_ACTIVE)
            # The rename above is the COMMIT POINT — the flip is live the moment
            # it succeeds. Bookkeeping failures past it must not turn the return
            # into None: the caller would then skip the SWITCHED event and the
            # recovery-manager launch for a pointer change that already happened.
            try:
                state["last_flip"] = {"ts": now, "from": pointer, "to": other}
                _write_json_atomic(ACCOUNT_STATE, state)
                _append_account_ledger({"ts": now, "event": "flip", "from": pointer,
                                        "to": other, "reason": reason, "by": "stale_monitor"})
            except Exception as e:
                print(f"stale_monitor: flip bookkeeping failed ({e})", file=sys.stderr)
            return other
    except Exception as e:
        print(f"stale_monitor: account flip failed ({e})", file=sys.stderr)
        return None


def _ledger_recovery_launches(from_sid: str, now: int,
                              window: int = MAX_PLAUSIBLE_RESET_SEC) -> int:
    """Recovery-launch + recovery-relaunch ledger events for from_sid within
    the window — the durable backstop behind the emitted-state once+once
    launch bound. The emitted-state key is the fast path; if its write fails
    persistently (disk full), the key never survives a scan and every 60s
    scan would otherwise open a fresh recovery tab. Reads only the ledger
    tail (launches are rare; 64KB of tail is plenty). Crash-proof fail-open:
    any failure reads as 0, deferring to the emitted-state bound. Residual:
    if the ledger append ALSO fails persistently while the terminal keeps working,
    the per-scan launch storm remains — that requires both persistence paths
    failing simultaneously."""
    try:
        if not ACCOUNT_LEDGER.exists():
            return 0
        max_bytes = 65536
        size = ACCOUNT_LEDGER.stat().st_size
        with open(ACCOUNT_LEDGER, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read(max_bytes)
        lines = data.decode("utf-8", errors="replace").splitlines()
        if size > max_bytes and lines:
            lines = lines[1:]
        count = 0
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
            if event.get("event") not in ("recovery-launch", "recovery-relaunch"):
                continue
            if event.get("from_sid") != from_sid:
                continue
            ts = event.get("ts")
            if isinstance(ts, (int, float)) and now - ts < window:
                count += 1
        return count
    except Exception as e:
        print(f"stale_monitor: ledger launch count failed ({e})", file=sys.stderr)
        return 0


def _recent_flip_landed_on(pointer: str, now: int) -> bool:
    """Day-one recovery heuristic for UNSTAMPED managers (anything alive at
    pool activation has no birth-account stamp): when a flip recently landed
    ON the current pointer, a banner-bricked unstamped manager — which
    resolves account == pool, so its own flip attempt is blocked (cooldown /
    guard 4) and `already_flipped` reads False — is presumed bricked on the
    PRE-flip account, and the caller launches recovery onto the pointer.
    Read OUTSIDE the flock on purpose: read-only heuristic, and
    _load_account_state() is cheap. Accepted residual: an unstamped manager
    actually ON the post-flip account that bricks inside the window gets a
    doomed recovery tab — bounded by the launch guards, consistent with the
    deliberately-ungated launch stance (see DORMANCY INVARIANT paragraph)."""
    try:
        last_flip = _load_account_state().get("last_flip") or {}
        ts = last_flip.get("ts")
        return (last_flip.get("to") == pointer
                and isinstance(ts, (int, float))
                and now - ts < MAX_PLAUSIBLE_RESET_SEC)
    except Exception:
        return False


def _ledger_banner_event(event: str, banner: str, source: str, now: int,
                         emitted: dict, next_emitted: dict) -> None:
    """Capture-when-seen for a recognized limit banner, ledgered once per distinct
    text per limited episode (the dedup key rides the per-manager emitted state and
    is carried only while the banner keeps being seen). `event` is one of:
    'unparsed-banner' — matched RATE_LIMIT_SIGNATURES but its reset clause didn't
    parse (design §5.4, captures wording drift in KNOWN banners); or
    'transient-throttle' — a server-side 429 the monitor saw but correctly did NOT
    brick/flip on (see _is_transient_throttle)."""
    key = f"{event}:{hashlib.sha1(banner.encode('utf-8', 'replace')).hexdigest()[:12]}"
    if key not in emitted:
        _append_account_ledger({"ts": now, "event": event,
                                "text": banner[:200], "source": source,
                                "by": "stale_monitor"})
    next_emitted[key] = now


def _launch_recovery_manager(mgr_record: dict, mgr_sid: str, new_letter: str) -> str | None:
    """Open a fresh window on the flipped account running the thin recovery
    command. The new session does the takeover itself (design A3-v2: bash is
    the LLM-free trigger only). Best-effort: returns the window id or None.
    Routes to the `mgr` tmux session via the terminal driver."""
    cwd = mgr_record.get("cwd") or os.path.expanduser("~")
    name = mgr_record.get("name") or ""
    # _account_config_prefix CHECKS the farm; for a non-default new_letter whose
    # ~/.claude-<letter> a worker hasn't built yet it falls back to the default
    # (possibly-bricked) login stamped `a` and warns on stderr — see its
    # "KNOWN FAILURE MODE" docstring. Bounded by the once+once launch guard.
    inner = (
        f"{_account_config_prefix(new_letter)}"
        f"CLAUDE_AGENT=manager CLAUDE_WORKER_NAME={shlex.quote(name)} "
        # Manager lane is pinned (orch-audit model-allocation): never inherit
        # the user's interactive model default. Quoted so zsh -ic can't glob [1m].
        f"claude --model {shlex.quote('opus[1m]')} "
        f"{shlex.quote(f'/manager-takeover-recovery {mgr_sid}')}"
    )
    if _get_driver is None:
        print("stale_monitor: recovery launch skipped (driver unavailable)", file=sys.stderr)
        return None
    try:
        return asyncio.run(asyncio.wait_for(
            _get_driver().spawn(
                cwd=cwd, title="manager (recovery)", argv=["zsh", "-ic", inner],
                route_to_manager_session=True),
            timeout=10)) or None
    except Exception as e:
        print(f"stale_monitor: recovery launch failed ({e})", file=sys.stderr)
        return None


def _emitted_state_path(manager_name: str | None) -> Path:
    """Per-manager dedup/edge-trigger state file.

    manager_name=None → the legacy global `.stale-emitted.json` (back-compat).
    Otherwise a per-manager file, so concurrent scoped scans by peer managers
    (every 60s) don't full-overwrite each other's emitted thresholds or share
    the `last_autoclose_run` gate. Sanitize the name the same way paths._event_bucket
    does for the per-manager event subdirs.
    """
    if not manager_name:
        return ROOT / ".stale-emitted.json"
    safe = manager_name.replace("/", "_").replace("\\", "_")
    return ROOT / f".stale-emitted-{safe}.json"


def _matches_manager(record: dict, manager_name: str | None) -> bool:
    """Scoping filter mirroring mcp_server._matches_manager.

    manager_name=None → no filter (wildcard back-compat lane).
    Otherwise: strict — include only records whose parent_manager_name ==
    manager_name. Null-parent (legacy) records are INVISIBLE to per-manager
    calls; recovery path is `_backfill_legacy_workers` on a single-manager
    `become_manager` boot.
    """
    if manager_name is None:
        return True
    return record.get("parent_manager_name") == manager_name


def _load(p: Path) -> dict | None:
    try:
        return json.load(open(p))
    except Exception:
        return None


def _parse_iso(s) -> float | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique tmp per invocation: closed/<sid>.json is also written by
    # hooks.session_end from the dying session's process (autoclose race);
    # a target-derived tmp would let the two writers interleave.
    tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _load_emitted_state(emitted_state_path: Path) -> dict:
    if not emitted_state_path.exists():
        return {}
    try:
        with open(emitted_state_path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        print(f"stale_monitor: {emitted_state_path} not a dict, treating as empty", file=sys.stderr)
        return {}
    except Exception as e:
        print(f"stale_monitor: failed to read {emitted_state_path} ({e}), treating as empty", file=sys.stderr)
        return {}


def _highest_threshold(elapsed_min: int, base_min: int) -> int | None:
    """Return the highest doubling threshold <= elapsed_min starting at base_min, else None."""
    if elapsed_min < base_min:
        return None
    t = base_min
    while t * 2 <= elapsed_min:
        t *= 2
    return t


def _highest_nudge_threshold(elapsed_min: int, base_min: int) -> int | None:
    """Nudge cadence: doubling for the first crossings (base, 2x, 4x — 30/60/120min
    by default), then a flat NUDGE_REPEAT_INTERVAL_MIN step beyond, so a fleet
    bricked by a long org-wide 429 gets re-kicked within an hour of the limit
    resetting instead of waiting for the next doubling (240, 480, ...)."""
    if elapsed_min < base_min:
        return None
    cap = base_min * 4
    if elapsed_min < cap:
        return _highest_threshold(elapsed_min, base_min)
    extra_steps = (elapsed_min - cap) // NUDGE_REPEAT_INTERVAL_MIN
    return cap + extra_steps * NUDGE_REPEAT_INTERVAL_MIN


def _pending_question_sids() -> set:
    sids = set()
    if not QUESTIONS.is_dir():
        return sids
    for p in QUESTIONS.rglob("*.json"):
        record = _load(p)
        if record is None:
            continue
        sid = record.get("worker_sid")
        if sid:
            sids.add(sid)
    return sids


def _close_window(window_id: str) -> None:
    if not window_id or _get_driver is None:
        return
    try:
        _get_driver().close(window_id)
    except Exception:
        pass


def _send_text(window_id: str, text: str) -> None:
    """Type message content into a worker pane and submit, via the terminal
    driver. Best-effort: swallows failures so a scan never blocks."""
    if not window_id or _get_driver is None:
        return
    try:
        _get_driver().send_text(window_id, text)
    except Exception:
        pass


def _find_claude_session_log(sid: str) -> Path | None:
    """Locate ~/.claude/projects/*/<sid>.jsonl (mirrors transcript._find_claude_session_log)."""
    if not sid or not CLAUDE_PROJECTS.is_dir():
        return None
    for project_dir in CLAUDE_PROJECTS.iterdir():
        candidate = project_dir / f"{sid}.jsonl"
        if candidate.is_file():
            return candidate
    return None


def _find_codex_session_log(sid: str) -> Path | None:
    """Locate ~/.codex/sessions/**/rollout-*-<sid>.jsonl, newest first (mirrors
    transcript._find_codex_session_log)."""
    if not sid or not CODEX_SESSIONS.is_dir():
        return None
    matches = sorted(
        CODEX_SESSIONS.rglob(f"rollout-*-{sid}.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def _resolve_transcript_path(record: dict, codex_log_cache: dict | None = None) -> Path | None:
    """Transcript path for a record's runtime. Codex thread ids ride in the same
    claude_sid field; the codex rglob result is cached (in the emitted state via
    main()) because a long codex turn would otherwise re-scan ~/.codex/sessions
    every 60s."""
    sid = record.get("claude_sid")
    if not sid:
        return None
    if (record.get("runtime") or "claude") == "codex":
        cached = (codex_log_cache or {}).get(sid)
        if isinstance(cached, str) and cached:
            cached_path = Path(cached)
            if cached_path.is_file():
                return cached_path
        log = _find_codex_session_log(sid)
        if log is not None and codex_log_cache is not None:
            codex_log_cache[sid] = str(log)
        return log
    return _find_claude_session_log(sid)


def _latest_subagent_mtime(log: Path, sid: str) -> float:
    """Newest mtime across <log.parent>/<sid>/subagents/agent-*.jsonl, else 0.0.
    Mirrors transcript.latest_subagent_mtime. Crash-proof: any OSError → 0.0.
    """
    try:
        subagents_dir = log.parent / sid / "subagents"
        newest = 0.0
        for entry in subagents_dir.glob("agent-*.jsonl"):
            try:
                newest = max(newest, entry.stat().st_mtime)
            except OSError:
                continue
        return newest
    except OSError:
        return 0.0


def _is_delegation_live(record: dict, log: Path | None = None) -> bool:
    """True when this idle worker has a background subagent still writing.

    Two predicates must both hold:
    - Growth: newest subagent write > main log mtime (post-Stop background
      delegation, not a foreground agent whose result was already consumed).
    - Freshness: now - newest < IDLE_THRESHOLD_SEC (hung-but-silent subagents
      still age out under normal autoclose).

    Crash-proof: any OSError → False (pre-change behavior).
    """
    try:
        if (record.get("runtime") or "claude") != "claude":
            return False
        sid = record.get("claude_sid")
        if not sid:
            return False
        if log is None:
            log = _resolve_transcript_path(record)
        if log is None:
            return False
        newest = _latest_subagent_mtime(log, sid)
        if newest <= 0:
            return False
        now = time.time()
        return newest > log.stat().st_mtime and now - newest < IDLE_THRESHOLD_SEC
    except OSError:
        return False


def _last_activity(record: dict, record_mtime: int,
                   codex_log_cache: dict | None = None) -> tuple[int, Path | None]:
    """(last-observed activity, transcript path) for a processing record:
    activity = max(active-record mtime, transcript mtime).

    The transcript jsonl is appended on every event the CLI emits (tool calls,
    tool results, assistant messages), so its mtime is the last-append time. The
    record mtime alone is just the turn start — a long busy turn is not a stall.
    max() covers the first moments of a fresh turn before any transcript append
    (a previous turn's old transcript must not make a brand-new turn look
    silent) and guarantees activity-elapsed <= turn-elapsed: the change strictly
    narrows when stale fires. The resolved path is returned so callers that also
    need the transcript content (banner checks) don't resolve twice.

    Crash-proof by contract: runs bare inside main()'s scan loop; any failure
    logs to stderr and falls back to the record mtime (turn-age behavior) — one
    worker's poison path must never abort monitoring for the rest.
    """
    try:
        log = _resolve_transcript_path(record, codex_log_cache)
        if log is None:
            return record_mtime, None
        return max(record_mtime, int(log.stat().st_mtime)), log
    except Exception as e:
        print(f"stale_monitor: transcript-activity check failed for {record.get('claude_sid')} ({e})",
              file=sys.stderr)
        return record_mtime, None


def _last_activity_mtime(record: dict, record_mtime: int) -> int:
    return _last_activity(record, record_mtime)[0]


def _limit_banner_text(log_path: Path | None, strict: bool = False) -> str | None:
    """The transcript's final assistant text when it is a rate-limit / session-
    limit banner (see RATE_LIMIT_SIGNATURES), else None. Crash-proof: any
    failure reads as 'no banner'.

    strict=True (the manager path) additionally requires a short text with the
    signature near the start — a manager message that merely QUOTES a banner
    (relaying a worker's limit state, very plausible in this system) must not
    read as the manager itself being limited: the blast radius there is
    suppressed events plus text typed into a live AskUserQuestion pane. The
    worker path stays loose (pre-existing behavior; a spurious worker nudge is
    benign)."""
    try:
        if log_path is None:
            return None
        text = _last_assistant_text(log_path)
        if not text:
            return None
        lowered = text.lower()
        for signature in RATE_LIMIT_SIGNATURES + TRANSIENT_SERVER_ERROR_SIGNATURES:
            index = lowered.find(signature)
            if index < 0:
                continue
            if strict and (len(text) > MAX_BANNER_LEN or index > MAX_BANNER_SIG_OFFSET):
                continue
            return text
        return None
    except Exception as e:
        print(f"stale_monitor: banner check failed for {log_path} ({e})", file=sys.stderr)
        return None


def _is_transient_throttle(banner: str | None) -> bool:
    """True iff a detected limit banner is the transient server-side 429 throttle
    (see TRANSIENT_THROTTLE_SIGNATURES) rather than a genuine per-account usage
    limit. The throttle must drive nudge recovery but never a brick/flip: it is
    org-wide (the other account shares the same server, so a flip can't escape it)
    and clears on its own. Pure (no IO) for testability."""
    if not banner:
        return False
    lowered = banner.lower()
    return any(sig in lowered
               for sig in TRANSIENT_THROTTLE_SIGNATURES + TRANSIENT_SERVER_ERROR_SIGNATURES)


def _is_auth_401_event(event) -> bool:
    """True iff this assistant event is an auth-401 API error (see
    AUTH_FAILURE_SIGNATURES). The gate is the structured isApiErrorMessage flag;
    the 401 itself is identified by the stable apiErrorStatus==401, falling back
    to the (drift-prone) human text only when the status field is absent. A
    rate-limit banner is an isApiErrorMessage message too but never a 401, so
    this stays disjoint from RATE_LIMIT_SIGNATURES. Pure (no IO) for testability."""
    if not isinstance(event, dict) or event.get("type") != "assistant":
        return False
    if not event.get("isApiErrorMessage"):
        return False
    if _safe_int(event.get("apiErrorStatus")) == 401:   # tolerate int 401 or "401"
        return True
    lowered = _assistant_event_text(event).lower()
    return any(signature in lowered for signature in AUTH_FAILURE_SIGNATURES)


def _auth_failure_signature(log_path: Path | None) -> tuple[str | None, str] | None:
    """(event uuid, text) when the transcript's last assistant event is an
    auth-401, else None. The uuid is the attempt key — a fresh uuid is a fresh
    401 (a resume that 401'd again); the same uuid still showing means the
    resume hasn't fired/cleared yet. Crash-proof: any failure reads as 'no auth
    failure'."""
    try:
        if log_path is None:
            return None
        event = _last_assistant_event(log_path)
        if event is None or not _is_auth_401_event(event):
            return None
        uuid = event.get("uuid")
        return (uuid if isinstance(uuid, str) else None,
                _assistant_event_text(event))
    except Exception as e:
        print(f"stale_monitor: auth-401 check failed for {log_path} ({e})", file=sys.stderr)
        return None


def _parse_limit_reset_ts(text: str | None, now: int) -> int | None:
    """Epoch of the banner's reset time + SCHEDULED_NUDGE_DELAY_SEC, or None.

    Best-effort by design: the banner wording is fragile (it changed once
    already), so any parse failure — no clause, nonsense time, unknown zone —
    returns None and the caller falls back to its catch-all (workers: the
    silence ladder; managers: the flat retry). Never raises.

    datetime.fromtimestamp(now, tz) rather than datetime.now() so fake-clock
    tests stay deterministic.
    """
    try:
        match = _RESET_CLAUSE_RE.search(text or "")
        if not match:
            return None
        hour12 = int(match.group(1))
        minute = int(match.group(2) or 0)
        if not (1 <= hour12 <= 12) or not (0 <= minute <= 59):
            return None
        meridiem = match.group(3).lower()
        tz = ZoneInfo(match.group(4).strip())
        hour24 = hour12 % 12 + (12 if meridiem == "pm" else 0)
        now_dt = datetime.fromtimestamp(now, tz)
        candidate = now_dt.replace(hour=hour24, minute=minute, second=0, microsecond=0)
        if candidate <= now_dt:
            candidate += timedelta(days=1)
        reset_ts = int(candidate.timestamp()) + SCHEDULED_NUDGE_DELAY_SEC
        if reset_ts - now > MAX_PLAUSIBLE_RESET_SEC:
            # The banner's wall-time already passed and rolled to tomorrow —
            # the limit was hit minutes before its own reset boundary and the
            # banner is stale. Scheduling ~24h out would leave a manager (no
            # ladder) bricked all day; a stale banner is a parse failure.
            return None
        return reset_ts
    except Exception:
        return None


def _safe_int(value) -> int:
    """Counter values from the emitted state — malformed (hand-edits,
    corruption) reads as 0; a crash in the flush would loop every scan with
    the flag held and the monitor.py scans suspended indefinitely."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _load_scheduled(emitted: dict, key: str) -> dict | None:
    """Validated `scheduled:*` value ({"at": ts, "baseline": activity}) or None."""
    value = emitted.get(key)
    if (isinstance(value, dict)
            and isinstance(value.get("at"), (int, float))
            and isinstance(value.get("baseline"), (int, float))):
        return value
    return None


def _last_assistant_text(log_path: Path, max_bytes: int = 65536) -> str | None:
    """Text of the transcript's last assistant message, reading only the file tail.

    Transcripts grow to many MB and the throttle signature is always in the final
    lines, so seek to the last max_bytes instead of reading the whole file. When
    truncated, the first line of the window is dropped as possibly partial. Claude
    transcript shape only (mirrors transcript._assistant_text's claude branch).
    """
    try:
        size = log_path.stat().st_size
        with open(log_path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read(max_bytes)
    except OSError:
        return None
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]
    last_text = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        # The transcript is another process's output — any valid-JSON shape can
        # appear (lists, scalars, null message, non-string text). Shape-check
        # every level instead of trusting it; one bad line must not kill a scan.
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content", [])
        if not isinstance(content, list):
            continue
        text_parts = [c["text"] for c in content
                      if isinstance(c, dict) and c.get("type") == "text"
                      and isinstance(c.get("text"), str)]
        text = " ".join(text_parts).strip()
        if text:
            last_text = text
    return last_text


def _assistant_event_text(event: dict) -> str:
    """Join the text parts of an assistant event's message content. Shape-checks
    every level — the transcript is another process's output, any shape can appear."""
    if not isinstance(event, dict):
        return ""
    message = event.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content", [])
    if not isinstance(content, list):
        return ""
    parts = [c["text"] for c in content
             if isinstance(c, dict) and c.get("type") == "text"
             and isinstance(c.get("text"), str)]
    return " ".join(parts).strip()


def _last_assistant_event(log_path: Path, max_bytes: int = 65536) -> dict | None:
    """The transcript's last assistant EVENT dict (tail-read only), or None.

    Sibling to _last_assistant_text — that returns only the message text, but
    auth-401 detection needs the event's top-level fields (isApiErrorMessage,
    apiErrorStatus, uuid). Deliberately independent so the rate-limit path stays
    untouched. Crash-proof: any read/parse failure reads as 'no event'.
    """
    try:
        size = log_path.stat().st_size
        with open(log_path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read(max_bytes)
    except OSError:
        return None
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]
    last_event = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "assistant":
            last_event = event
    return last_event


def _is_rate_limited(record: dict) -> bool:
    """True if the worker's transcript ends on an Anthropic throttle message or
    session-limit banner (see RATE_LIMIT_SIGNATURES).

    The active record's last_summary is only written by the Stop hook and a
    throttled worker never stops, so the live signal exists only in the
    transcript — the same source list_workers derives its last_summary from.
    Claude runtime only: codex throttle text differs. Standalone wrapper (own
    transcript resolution) kept for callers outside the scan loop; the scan
    itself reuses the path already resolved for the activity check.
    """
    try:
        if (record.get("runtime") or "claude") != "claude":
            return False
        return _limit_banner_text(_resolve_transcript_path(record)) is not None
    except Exception as e:
        print(f"stale_monitor: rate-limit check failed for {record.get('claude_sid')} ({e})",
              file=sys.stderr)
        return False


def _count_unseen_done_events(manager_name: str) -> int:
    """Done-event files for this manager that its done scan has not yet
    surfaced. Mirrors monitor.py's shapes exactly: events under DONE/<raw name>,
    seen-list at ROOT/.seen-done-<raw name> (one path per line)."""
    try:
        done_dir = ROOT / "done" / manager_name
        if not done_dir.is_dir():
            return 0
        seen_path = ROOT / f".seen-done-{manager_name}"
        seen = set()
        if seen_path.exists():
            seen = {line for line in seen_path.read_text().splitlines() if line}
        # deprecated, one release: pre-rename cursors carry absolute legacy-root
        # paths; normalize so migrated done events aren't recounted as unseen.
        legacy_prefix = str(_LEGACY_ROOT) + "/"
        new_prefix = str(ROOT) + "/"
        seen = {
            new_prefix + line[len(legacy_prefix):] if line.startswith(legacy_prefix) else line
            for line in seen
        }
        return sum(1 for p in done_dir.glob("*.json") if str(p) not in seen)
    except Exception:
        return 0


def _build_rollup_line(buffer: dict, manager_name: str, now: int) -> str:
    names = buffer.get("stalled_names")
    stalled = len(names) if isinstance(names, list) else 0
    nudged = _safe_int(buffer.get("nudged"))
    done = _count_unseen_done_events(manager_name)
    line = (f"limit cleared {datetime.fromtimestamp(now).strftime('%H:%M')} — "
            f"while down: {stalled} workers stalled, {nudged} nudged, {done} done events")
    resumed = _safe_int(buffer.get("resumed"))
    questions = _safe_int(buffer.get("questions"))
    autoclosed = _safe_int(buffer.get("autoclosed"))
    if resumed:
        line += f", {resumed} resumed"
    if questions:
        line += f", {questions} questions stale"
    if autoclosed:
        line += f", {autoclosed} autoclosed"
    switched = buffer.get("switched")
    if isinstance(switched, str) and switched:
        line += f", switched {switched}"
    since = _safe_int(buffer.get("since"))
    if since and now > since:
        line += f", down {(now - since) // 60}min"
    return line


def _limited_flag_path(manager_name: str) -> Path:
    safe = manager_name.replace("/", "_").replace("\\", "_")
    return ROOT / f".manager-limited-{safe}"


def _outbox_dir(manager_name: str) -> Path:
    # Sanitization mirrors paths._event_bucket / _limited_flag_path.
    safe = manager_name.replace("/", "_").replace("\\", "_")
    return ROOT / "notify-outbox" / safe


def _outbox_write(manager_name: str, kind: str, line: str, now: float, seq: int) -> None:
    """Divert one informational line. ANY failure falls back to printing —
    today's dedicated-wake behavior is the floor; a swallowed write would be
    a true event loss (spec I9)."""
    try:
        target = _outbox_dir(manager_name) / f"{int(now * 1000)}-{os.getpid()}-{seq}.json"
        _write_json_atomic(target, {"line": line, "kind": kind, "buffered_at": now})
    except Exception as e:
        print(line)
        print(f"stale_monitor: outbox write failed ({e}); printed instead",
              file=sys.stderr)


def _drain_outbox(manager_name: str) -> None:
    """Print-then-unlink; same at-least-once discipline and per-entry failure
    policy as monitor._drain_notify_outbox (FileNotFoundError = a concurrent
    drainer won the race; undecodable = unlink so it can't block the rest)."""
    try:
        outbox = _outbox_dir(manager_name)
        if not outbox.is_dir():
            return
        for p in sorted(outbox.glob("*.json")):
            try:
                payload = json.loads(p.read_text())
            except FileNotFoundError:
                continue
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                print(f"stale_monitor: dropped undecodable outbox entry {p.name}",
                      file=sys.stderr)
                p.unlink(missing_ok=True)
                continue
            line = payload.get("line") if isinstance(payload, dict) else None
            if isinstance(line, str) and line:
                print(line)
            p.unlink(missing_ok=True)
    except Exception as e:
        print(f"stale_monitor: outbox drain failed ({e})", file=sys.stderr)


def _outbox_oldest_ts(manager_name: str) -> float | None:
    outbox = _outbox_dir(manager_name)
    if not outbox.is_dir():
        return None
    oldest = None
    for p in outbox.glob("*.json"):
        payload = _load(p)
        ts = payload.get("buffered_at") if isinstance(payload, dict) else None
        if not isinstance(ts, (int, float)) or ts <= 0:
            try:
                ts = p.stat().st_mtime
            except OSError:
                continue
        oldest = ts if oldest is None else min(oldest, ts)
    return oldest


def _compute_idle_elapsed_sec(record: dict, current_uptime: float, now: int) -> int | None:
    """Seconds since an idle worker's last turn, sleep-correctly.

    Prefer uptime delta (CLOCK_UPTIME_RAW): macOS pauses it during laptop
    sleep, so an 8h sleep doesn't burn the worker's 2h idle grace. Wall-clock
    keeps ticking through sleep and would falsely reap a freshly-idled worker.

    Wall-clock fallback when (a) the record predates the fix and has no
    last_turn_at_uptime field, or (b) reboot reset current_uptime below the
    persisted value — without the fallback, a reboot would make elapsed
    negative and auto-close would never fire.
    """
    persisted_uptime = record.get("last_turn_at_uptime")
    # >= not > — equal uptimes mean "stamped this same tick"; elapsed is 0,
    # no point dropping to the wall fallback for the boundary case.
    if isinstance(persisted_uptime, (int, float)) and current_uptime >= persisted_uptime:
        return int(current_uptime - persisted_uptime)
    last_turn = _parse_iso(record.get("last_turn_at"))
    if last_turn is None:
        started = record.get("started_at")
        last_turn = started if isinstance(started, (int, float)) and started > 0 else None
    if last_turn is None:
        return None
    return now - int(last_turn)


def _autoclose_idle_worker(record_path: Path, record: dict, elapsed_sec: int) -> str:
    sid = record.get("claude_sid")
    name = record.get("name") or ""
    window_id = record.get("window_id") or record.get("iterm_sid") or ""
    closed_record = {
        "claude_sid": sid,
        "name": name,
        "cwd": record.get("cwd"),
        "window_id": window_id,
        "last_summary": record.get("last_summary"),
        "last_turn_at": record.get("last_turn_at"),
        "spend": record.get("spend"),
        "started_at": record.get("started_at"),
        "closed_at": time.time(),
        "closed_reason": f"idle>{IDLE_THRESHOLD_SEC}s",
        "parent_manager_name": record.get("parent_manager_name"),
        "runtime": record.get("runtime") or "claude",
        "account": record.get("account"),
    }
    if sid:
        _write_json_atomic(CLOSED / f"{sid}.json", closed_record)
    # Unlink active BEFORE the window close so the in-window orchestrator
    # session-end hook (which fires inside the closing window as a side effect
    # of the close) sees no active record and skips its closed/<sid>.json write
    # — preserving our "idle>...s" closed_reason instead of overwriting with
    # "session_end".
    record_path.unlink(missing_ok=True)
    # Graceful close: SIGHUP → grace → SIGKILL lets Claude Code run its
    # SessionEnd hook (selffix-trigger.sh fires natively, no manual trigger
    # needed). Verified for processing workers mid-tool-call.
    _close_window(window_id)
    return f"AUTOCLOSED {name} idle {elapsed_sec // 60}min"


def main(manager_name: str | None = None) -> int:
    now = int(time.time())
    emitted_state_path = _emitted_state_path(manager_name)
    emitted = _load_emitted_state(emitted_state_path)
    next_emitted: dict = {}
    blocked_sids = _pending_question_sids()
    # Event lines are collected and flushed at the end of the scan so that a
    # positively-limited owning manager can have them buffered (each printed
    # line is a task-notification = a wake attempt the bricked manager can't
    # act on) and rolled up on recovery.
    # dedup_key rides along for pure-print kinds (stalled/question): when their
    # line lands in the buffer instead of stdout, the recovery flush un-burns
    # the rung by dropping the key — the first post-recovery scan re-fires it
    # live instead of waiting for the next doubling. Action kinds (nudges)
    # advance their dedup normally; the action happened either way.
    events: list[tuple[str, str, str, str | None]] = []

    def emit(kind: str, name: str, line: str, dedup_key: str | None = None) -> None:
        events.append((kind, name, line, dedup_key))

    codex_log_cache = emitted.get("codex_log_cache")
    codex_log_cache = dict(codex_log_cache) if isinstance(codex_log_cache, dict) else {}
    seen_codex_sids: set[str] = set()
    # 60s scan cadence is right for STALE_PROCESSING (30min default threshold) and
    # STALE_QUESTION (2min threshold) but absurd for a 2h-horizon auto-close.
    # Gate the idle branch hourly via a persisted timestamp; preserve it across
    # writes since next_emitted otherwise replaces emitted entirely.
    last_autoclose = emitted.get("last_autoclose_run")
    if isinstance(last_autoclose, (int, float)) and now - last_autoclose < AUTOCLOSE_CADENCE_SEC:
        should_run_autoclose = False
        next_emitted["last_autoclose_run"] = last_autoclose
    else:
        should_run_autoclose = True
        next_emitted["last_autoclose_run"] = now
    current_uptime = time.clock_gettime(time.CLOCK_UPTIME_RAW)
    # One pointer read per scan: every unstamped record this scan resolves its
    # bricked-account against the SAME letter, so a mid-scan flip can't cascade
    # — records seen after the flip still resolve to the pre-flip letter, and
    # guard 1 in _maybe_flip_account (pointer == bricked) blocks the flip-back.
    pool = _pool_account()
    # ---- owning-manager limit handling (scoped runs only) -------------------
    # A manager bricked on a limit banner is deaf: it stays state=processing,
    # task-notifications can't wake it, and it never reaches the worker loop
    # below (its own record is null-parent → invisible to _matches_manager).
    # This block is the ONLY manager touchpoint: managers stay excluded from
    # the silence ladder, STALE_PROCESSING pages, the 5-min fast-path nudge,
    # and idle autoclose. Detection is positive-only (banner is the transcript's
    # final assistant text); anything else must not delay events.
    manager_limited = False
    if manager_name and ACTIVE.is_dir():
        mgr_path = mgr_record = None
        for p in ACTIVE.iterdir():
            if p.suffix != ".json":
                continue
            candidate = _load(p)
            if (candidate is not None and candidate.get("agent") == "manager"
                    and candidate.get("name") == manager_name):
                mgr_path, mgr_record = p, candidate
                break
        if (mgr_record is not None and mgr_record.get("state") == "processing"
                and (mgr_record.get("runtime") or "claude") == "claude"):
            try:
                mgr_mtime = int(mgr_path.stat().st_mtime)
            except OSError:
                mgr_mtime = None
            if mgr_mtime is not None:
                mgr_sid = mgr_record.get("claude_sid") or mgr_path.stem
                mgr_sched_key = f"scheduled:{mgr_sid}"
                mgr_activity, mgr_log = _last_activity(mgr_record, mgr_mtime, codex_log_cache)
                banner = None
                auth_fail = None
                if now - mgr_activity >= MANAGER_LIMIT_CHECK_FLOOR_SEC:
                    banner = _limit_banner_text(mgr_log, strict=True)
                    if banner is None:
                        auth_fail = _auth_failure_signature(mgr_log)
                if banner is not None:
                    # Coalescing is unconditional (suppressed lines were wasted
                    # wake attempts regardless); typed manager nudges belong to
                    # the opt-in autonudge feature like worker nudges do.
                    manager_limited = True
                    # ---- account flip lane (pool on only; dormancy invariant:
                    # no pointer ⇒ no state writes, no ledger, no launches).
                    # Brick is recorded BEFORE any flip attempt — guard-4's
                    # flip-back protection needs the bricked account's entry to
                    # exist when the other account later bricks. The recovery
                    # key is carried ONLY while this block runs: takeover
                    # unlinking the record, or the banner clearing, drops it
                    # from next_emitted naturally — that IS the guard teardown.
                    if pool is not None and not _is_transient_throttle(banner):
                        account = _account_of(mgr_record, pool)
                        reset_ts = _parse_limit_reset_ts(banner, now)
                        if reset_ts is None:
                            _ledger_banner_event("unparsed-banner", banner,
                                                 f"manager:{manager_name}",
                                                 now, emitted, next_emitted)
                        _record_brick(account, reset_ts, f"manager:{manager_name}", now)
                        recovery_key = f"recovery:{mgr_sid}"
                        recovery = emitted.get(recovery_key)
                        if not isinstance(recovery, dict):
                            # The recovery launch is decoupled from THIS scan's
                            # flip success: a stamped manager whose account no
                            # longer matches the pointer bricked AFTER a flip
                            # had already landed (a worker's, or another scan's)
                            # — the pointer is already the healthy letter, so
                            # launch onto it with no new flip and no SWITCHED
                            # (the original flip emitted its own; the
                            # recovery-launch ledger line is the observability).
                            # account == pool gates the flip attempt only to
                            # skip a pointless guard walk (guard 1 blocks it).
                            new_letter = (_maybe_flip_account(
                                account, f"manager {manager_name} limited", now)
                                if account == pool else None)
                            already_flipped = account != pool
                            if new_letter is None and not already_flipped:
                                # Day-one heuristic: an unstamped manager
                                # resolves account == pool, so a flip that
                                # already landed on the pointer (waking nobody)
                                # leaves it stranded — the flip attempt above
                                # returns None (cooldown / guard 4) and the
                                # stamp comparison can't see the move. A recent
                                # flip TO the pointer is the tell.
                                already_flipped = _recent_flip_landed_on(pool, now)
                            if new_letter is not None or already_flipped:
                                if new_letter is not None:
                                    emit("switched", manager_name,
                                         f"SWITCHED account {account}→{new_letter} "
                                         f"(manager {manager_name} limited)")
                                target = new_letter or pool
                                # Keychain gate: a recovery tab spawned against
                                # a locked keychain freezes pre-claude on the
                                # SecurityAgent dialog. The flip-success path
                                # already proved the target usable (guard 3);
                                # the already_flipped path proves it here.
                                # Deferred, not dropped — the guard key stays
                                # unwritten, so the launch retries the moment
                                # the keychain is usable. The ledger count is
                                # the durable once-bound backstop: with the
                                # emitted-state write dead (disk full) the
                                # recovery key never persists and this branch
                                # re-enters every scan.
                                if ((new_letter is not None
                                     or _keychain_unlocked())
                                        and _ledger_recovery_launches(mgr_sid, now) == 0):
                                    wid = _launch_recovery_manager(mgr_record, mgr_sid,
                                                                   target)
                                    _append_account_ledger({
                                        "ts": now, "event": "recovery-launch",
                                        "manager": manager_name, "from_sid": mgr_sid,
                                        "window_id": wid, "by": "stale_monitor"})
                                    next_emitted[recovery_key] = {"at": now, "relaunched": False}
                        else:
                            carried = dict(recovery)
                            if (not carried.get("relaunched")
                                    and now - _safe_int(carried.get("at")) > TAKEOVER_GUARD_SEC):
                                target = _pool_account() or pool
                                # Same keychain gate as the first launch; a
                                # locked keychain defers the once-only relaunch
                                # (relaunched stays False) rather than burning
                                # it on a tab that would freeze pre-claude.
                                # Ledger backstop: <=1 prior launch event keeps
                                # the once+once bound durable even if the
                                # emitted state stops persisting mid-episode.
                                if (_keychain_unlocked()
                                        and _ledger_recovery_launches(mgr_sid, now) <= 1):
                                    wid = _launch_recovery_manager(mgr_record, mgr_sid,
                                                                   target)
                                    _append_account_ledger({
                                        "ts": now, "event": "recovery-relaunch",
                                        "manager": manager_name, "from_sid": mgr_sid,
                                        "window_id": wid, "by": "stale_monitor"})
                                    carried["relaunched"] = True
                            next_emitted[recovery_key] = carried
                    elif pool is not None:
                        # Transient server-side 429 — never brick/flip the manager
                        # (a flip can't escape an org-wide throttle; the nudge
                        # schedule below revives it). Record for observability.
                        _ledger_banner_event("transient-throttle", banner,
                                             f"manager:{manager_name}",
                                             now, emitted, next_emitted)
                    sched = _load_scheduled(emitted, mgr_sched_key) if AUTONUDGE else None
                    if AUTONUDGE and sched is None:
                        # Parsed reset+2min when the banner cooperates; flat
                        # retry otherwise — managers have no ladder, so without
                        # this catch-all an unparseable banner would hold
                        # buffered events until a human unbricks the manager.
                        fire_at = (_parse_limit_reset_ts(banner, now)
                                   or now + MANAGER_NUDGE_RETRY_SEC)
                        next_emitted[mgr_sched_key] = {"at": fire_at, "baseline": mgr_activity}
                    elif sched is not None and now >= sched["at"]:
                        mgr_window = mgr_record.get("window_id") or ""
                        if mgr_activity <= sched["baseline"] and mgr_window:
                            _send_text(mgr_window, MANAGER_NUDGE_TEXT)
                            # Distinct kind: the manager's own recovery nudges
                            # must not inflate the rollup's worker counters.
                            emit("manager-nudged", manager_name,
                                 f"NUDGED {manager_name} (limit-reset)")
                        # RE-ARM in place at the flat retry — never drop-and-
                        # reparse: the banner is stale after a swallowed fire,
                        # so re-parsing "resets 2:20am" would schedule the next
                        # attempt for TOMORROW 2:20am.
                        next_emitted[mgr_sched_key] = {
                            "at": now + MANAGER_NUDGE_RETRY_SEC,
                            "baseline": mgr_activity,
                        }
                    elif sched is not None:
                        next_emitted[mgr_sched_key] = sched
                elif auth_fail is not None and pool is not None:
                    # auth-401 sibling to the limit branch (the limit branch is
                    # byte-for-byte unchanged). A 401'd manager is deaf, so the
                    # monitor launches a SAME-account takeover (a fresh process
                    # re-reads the keychain login) — NO flip: the other account
                    # is equally exposed to a server blip. On
                    # escalate we STOP launching dead takeover tabs (a revoked
                    # token can't be respawned out of) and flip + page instead;
                    # since each takeover is a fresh sid with its own guard key,
                    # this bounds the takeover count to ~AUTH_401_MAX_ATTEMPTS.
                    manager_limited = True
                    account = _account_of(mgr_record, pool)
                    auth_uuid, _auth_text = auth_fail
                    auth_key = f"auth-recovery:{mgr_sid}"
                    if auth_key in emitted:
                        # Persist the once-per-sid launch guard while the manager
                        # stays bricked; it drops naturally once the record is
                        # taken over or the 401 clears (guard teardown).
                        next_emitted[auth_key] = emitted[auth_key]
                    decision = _record_auth_401(account, auth_uuid, now)
                    if decision == "recover":
                        _append_account_ledger({
                            "ts": now, "event": "auth-401", "account": account,
                            "action": "recover", "source": f"manager:{manager_name}",
                            "from_sid": mgr_sid, "by": "stale_monitor"})
                        if (auth_key not in emitted
                                and _keychain_unlocked()
                                and _ledger_recovery_launches(mgr_sid, now) == 0):
                            wid = _launch_recovery_manager(mgr_record, mgr_sid, pool)
                            _append_account_ledger({
                                "ts": now, "event": "recovery-launch",
                                "manager": manager_name, "from_sid": mgr_sid,
                                "window_id": wid, "by": "stale_monitor"})
                            next_emitted[auth_key] = now
                    elif decision == "escalate":
                        _append_account_ledger({
                            "ts": now, "event": "auth-401", "account": account,
                            "action": "escalate", "source": f"manager:{manager_name}",
                            "from_sid": mgr_sid, "by": "stale_monitor"})
                        new_letter = _maybe_flip_account(
                            account, f"manager {manager_name} auth-401 credential suspect", now)
                        if new_letter is not None:
                            emit("switched", manager_name,
                                 f"SWITCHED account {account}→{new_letter} "
                                 f"(manager {manager_name} auth-401 credential suspect)")
                        emit("auth-escalate", manager_name,
                             f"AUTH_401_ESCALATED {account} (manager {manager_name}) — "
                             f"login suspect after repeated 401s; PAGE: /login the "
                             f"account-{account} config dir (default ~/.claude for a, "
                             f"~/.claude-b for b)")
                        # Recover the manager onto a HEALTHY account only — never
                        # relaunch a takeover on the suspect account (it would
                        # just 401 again; with each takeover rolling a fresh sid,
                        # an unhealthy-target relaunch would be unbounded). The
                        # healthy target is the just-flipped letter, or (if a flip
                        # already landed earlier) the current pointer when it
                        # differs from the suspect account. If neither exists
                        # (flip blocked, both logins suspect), page only — the
                        # human must /login. Guarded once per sid like the
                        # recover launch.
                        if new_letter is not None:
                            target = new_letter
                        elif account != pool:
                            target = pool          # a prior flip already moved off the suspect account
                        else:
                            target = None           # stuck on the suspect account → page only
                        if (target is not None
                                and auth_key not in emitted
                                and _keychain_unlocked()
                                and _ledger_recovery_launches(mgr_sid, now) == 0):
                            wid = _launch_recovery_manager(mgr_record, mgr_sid, target)
                            _append_account_ledger({
                                "ts": now, "event": "recovery-launch",
                                "manager": manager_name, "from_sid": mgr_sid,
                                "window_id": wid, "by": "stale_monitor"})
                            next_emitted[auth_key] = now
                    # decision == "duplicate": guard persisted above; no-op
    if ACTIVE.is_dir():
        for p in ACTIVE.iterdir():
            if p.suffix != ".json":
                continue
            record = _load(p)
            if record is None:
                continue
            if record.get("nested"):
                # Nested sub-sessions (claude -p children of a registered
                # session) are supervised by their parent process: no stale
                # pages, no nudges, and especially no autoclose — their record
                # has no window of its own, and they must never page the
                # manager. Dead-pid cleanup handles leftovers.
                continue
            if not _matches_manager(record, manager_name):
                continue
            state = record.get("state")
            if state == "processing":
                # Manager turns stay "processing" for as long as AskUserQuestion
                # holds the turn open — minutes is normal and not stale.
                if record.get("agent") != "worker":
                    continue
                try:
                    mtime = int(p.stat().st_mtime)
                except OSError:
                    continue
                sid = record.get("claude_sid") or p.stem
                # Stretch-scoped dedup for the early 429 nudge (the ladder has its
                # own threshold dedup): one rate-limit nudge per processing
                # stretch. A delivered nudge submits a prompt, which rewrites the
                # record (fresh mtime = new stretch) — so a worker still bricked
                # by a long org-wide 429 is re-nudged after ~RATE_LIMIT_NUDGE_MIN
                # of new silence and auto-revives the moment the limit resets.
                # Prior stretches' keys are pruned by not being carried over.
                stretch_nudge_key = f"nudged:{sid}:{mtime}"
                if stretch_nudge_key in emitted:
                    next_emitted[stretch_nudge_key] = emitted[stretch_nudge_key]
                # A typed nudge is an attempt, not a delivery: a CLI sitting on
                # a limit banner swallows input without starting a turn (verified
                # against incident transcripts — NUDGED events with zero "resume
                # your task" user messages). Transcript growth after the nudge is
                # the ONLY delivery confirmation: surface it once as RESUMED and
                # drop the marker; until then the ladder keeps re-nudging.
                nudge_sent_key = f"nudge_sent:{sid}"
                sent_at = emitted.get(nudge_sent_key)
                if not isinstance(sent_at, (int, float)):
                    sent_at = None
                # Banner-parsed post-reset nudge. Not-yet-due keys are carried
                # HERE, pre-gate — next_emitted is a full rewrite, so a gate-
                # `continue` below would otherwise silently drop the schedule.
                sched_key = f"scheduled:{sid}"
                sched = _load_scheduled(emitted, sched_key)
                sched_due = sched is not None and now >= sched["at"]
                if sched is not None and not sched_due:
                    next_emitted[sched_key] = sched
                if (record.get("runtime") or "claude") == "codex":
                    seen_codex_sids.add(sid)
                name = record.get("name", "")
                # No legacy iterm_sid fallback here (unlike autoclose): an iTerm
                # sid never matches a tmux pane id, so a "nudge" against it
                # would no-op silently while suppressing the human page — or, on
                # a numeric collision, type into a foreign tmux pane. Legacy
                # records fall through to plain STALE_PROCESSING.
                window_id = record.get("window_id") or ""
                nudge_eligible = (
                    AUTONUDGE
                    and sid not in blocked_sids
                    and bool(window_id)
                )
                # Staleness = transcript silence, not turn length. Resolving the
                # transcript costs IO (a dir scan; an rglob for codex) and
                # activity-elapsed <= turn-elapsed always, so skip it entirely
                # until the turn is old enough for some branch to possibly fire.
                # An outstanding nudge marker or a due scheduled nudge bypasses
                # the gate: both need a fresh activity stat to resolve.
                turn_elapsed = now - mtime
                # The account flip lane below needs the transcript (banner
                # check) at any silence >= RATE_LIMIT_NUDGE_SEC regardless of
                # nudge eligibility — claude transcripts only, the banner shape
                # is claude's. Pool off leaves the gate exactly as before.
                pool_needs_transcript = (
                    pool is not None
                    and (record.get("runtime") or "claude") == "claude"
                )
                activity_gate = (min(PROCESSING_THRESHOLD_SEC, RATE_LIMIT_NUDGE_SEC)
                                 if nudge_eligible or pool_needs_transcript
                                 else PROCESSING_THRESHOLD_SEC)
                if sent_at is None and not sched_due and turn_elapsed < activity_gate:
                    continue
                activity, log = _last_activity(record, mtime, codex_log_cache)
                if sent_at is not None:
                    if activity > sent_at:
                        emit("resumed", name, f"RESUMED {name}")
                    else:
                        next_emitted[nudge_sent_key] = sent_at
                fired_scheduled = False
                if sched_due:
                    # Self-cancel only when the worker GENUINELY moved since
                    # scheduling: activity past the baseline AND the transcript
                    # no longer ending on a limit banner. The baseline alone
                    # can't be trusted — it is captured pre-nudge, and a
                    # delivered pre-reset nudge's failed retry advances
                    # activity while leaving a fresh banner as the final text
                    # (still bricked; the 2026-06-11 storm showed every
                    # delivered nudge does this). Re-check eligibility too (a
                    # question may have arrived since). The due key is always
                    # consumed — fired, cancelled, or ineligible — the ladder
                    # remains the catch-all.
                    still_bannered = _limit_banner_text(log) is not None
                    if (activity <= sched["baseline"] or still_bannered) and nudge_eligible:
                        _send_text(window_id, NUDGE_TEXT)
                        next_emitted[nudge_sent_key] = now
                        emit("nudged", name, f"NUDGED {name} (limit-reset)")
                        fired_scheduled = True
                elapsed = now - activity
                # ---- account flip lane (pool on only; dormancy invariant:
                # no pointer ⇒ no state writes, no ledger, no flips). The
                # banner read is hoisted ABOVE the ladder branches: a flip
                # must fire at any silence past the 5min floor — including
                # past PROCESSING_THRESHOLD_SEC and while a banner-scheduled
                # nudge is armed, both of which keep the 5-min lane below
                # unreachable. Brick is recorded BEFORE the flip attempt —
                # guard-4's flip-back protection needs the bricked account's
                # entry to exist when the other account later bricks. The
                # lane touches none of the nudge lane's dedup keys.
                banner = None
                banner_read = False
                if pool_needs_transcript and elapsed >= RATE_LIMIT_NUDGE_SEC:
                    banner = _limit_banner_text(log)
                    banner_read = True
                    if banner is not None and _is_transient_throttle(banner):
                        # Server-side 429 throttle — org-wide and transient. A flip
                        # can't escape it and the worker self-recovers via the nudge
                        # lanes below; record it for observability, never brick/flip.
                        _ledger_banner_event("transient-throttle", banner,
                                             f"worker:{name}", now,
                                             emitted, next_emitted)
                    elif banner is not None:
                        account = _account_of(record, pool)
                        reset_ts = _parse_limit_reset_ts(banner, now)
                        if reset_ts is None:
                            _ledger_banner_event("unparsed-banner", banner,
                                                 f"worker:{name}",
                                                 now, emitted, next_emitted)
                        _record_brick(account, reset_ts, f"worker:{name}", now)
                        new_letter = _maybe_flip_account(
                            account, f"worker {name} limited", now)
                        if new_letter is not None:
                            emit("switched", name,
                                 f"SWITCHED account {account}→{new_letter} "
                                 f"(worker {name} limited)")
                    else:
                        # auth-401 lane (sibling to the rate-limit banner; the
                        # two signatures are disjoint). Recovery is SAME-account
                        # kill+resume via the manager's documented duty (the
                        # AUTH_401 event), NOT a flip. The AUTH_401 trigger
                        # re-fires on a cadence while the worker stays 401'd, so a
                        # missed or (manager-limited) coalesced-then-recovered
                        # event still reaches a live manager — decoupled from the
                        # uuid-deduped attempt count below, which never inflates
                        # on a re-emit. Bounded: after AUTH_401_MAX_ATTEMPTS failed
                        # resumes the login is suspect → flip (existing SWITCHED ⇒
                        # new-account kill+resume duty) + page the human to /login.
                        auth_sig = _auth_failure_signature(log)
                        if auth_sig is not None:
                            auth_uuid, _auth_text = auth_sig
                            account = _account_of(record, pool)
                            decision = _record_auth_401(account, auth_uuid, now)
                            auth_emit_key = f"auth-emit:{sid}"
                            last_emit = emitted.get(auth_emit_key)
                            reemit_due = (not isinstance(last_emit, (int, float))
                                          or now - last_emit >= AUTH_401_REEMIT_SEC)
                            if decision == "escalate":
                                _append_account_ledger({
                                    "ts": now, "event": "auth-401", "account": account,
                                    "action": "escalate", "source": f"worker:{name}",
                                    "from_sid": sid, "by": "stale_monitor"})
                                new_letter = _maybe_flip_account(
                                    account, f"worker {name} auth-401 credential suspect", now)
                                if new_letter is not None:
                                    emit("switched", name,
                                         f"SWITCHED account {account}→{new_letter} "
                                         f"(worker {name} auth-401 credential suspect)")
                                emit("auth-escalate", name,
                                     f"AUTH_401_ESCALATED {account} (worker {name}) — "
                                     f"login suspect after repeated 401s; PAGE: /login the "
                                     f"account-{account} config dir (default ~/.claude for a, "
                                     f"~/.claude-b for b)")
                                next_emitted[auth_emit_key] = now
                            elif decision == "recover" or reemit_due:
                                # Ledger only the genuine attempt (a fresh 401);
                                # a cadence re-emit of the SAME 401 is not a new
                                # attempt, so it adds no ledger line.
                                if decision == "recover":
                                    _append_account_ledger({
                                        "ts": now, "event": "auth-401", "account": account,
                                        "action": "recover", "source": f"worker:{name}",
                                        "from_sid": sid, "by": "stale_monitor"})
                                emit("auth-recover", name,
                                     f"AUTH_401 {name} — kill+resume on SAME account "
                                     f"{account} (transient auth-401; do NOT flip)",
                                     auth_emit_key)
                                next_emitted[auth_emit_key] = now
                            elif isinstance(last_emit, (int, float)):
                                # duplicate 401, cadence not yet due: carry the
                                # emit clock forward so it isn't dropped and
                                # re-fired every scan.
                                next_emitted[auth_emit_key] = last_emit
                if elapsed >= PROCESSING_THRESHOLD_SEC:
                    elapsed_min = elapsed // 60
                    # Nudges repeat at every crossing (busy workers are never
                    # stale under activity age, so repeats only ever hit silent
                    # ones); the human page keeps the pure doubling ladder. A
                    # mid-episode eligibility flip compares across the two
                    # ladders — acceptable: the only live flip source is a
                    # pending question, which pages STALE_QUESTION at 2min anyway.
                    threshold = (_highest_nudge_threshold(elapsed_min, PROCESSING_THRESHOLD_MIN)
                                 if nudge_eligible
                                 else _highest_threshold(elapsed_min, PROCESSING_THRESHOLD_MIN))
                    if threshold is not None:
                        # Embed the stretch-start (mtime) so a new processing stretch
                        # gets a fresh key and re-arms at the threshold — even if the monitor
                        # never observed the idle gap between two 60s scans.
                        key = f"processing:{sid}:{mtime}"
                        next_emitted[key] = threshold
                        last = emitted.get(key)
                        if not (isinstance(last, int) and last >= threshold):
                            if nudge_eligible:
                                # One typed nudge per scan: a scheduled fire
                                # already kicked this pane; still record the
                                # crossing so the cadence math stays intact.
                                if not fired_scheduled:
                                    _send_text(window_id, NUDGE_TEXT)
                                    next_emitted[nudge_sent_key] = now
                                    emit("nudged", name, f"NUDGED {name} ({elapsed_min}min)")
                            else:
                                emit("stalled", name,
                                     f"STALE_PROCESSING {name} ({elapsed_min}min)", key)
                elif (nudge_eligible and not fired_scheduled and sched is None
                      and elapsed >= RATE_LIMIT_NUDGE_SEC
                      and stretch_nudge_key not in emitted
                      and (record.get("runtime") or "claude") == "claude"):
                    # `sched is None`: while a banner-scheduled nudge is armed,
                    # this lane stays quiet — during a hard multi-hour session
                    # limit each delivered nudge here just retried into the
                    # same banner (fresh stretch + false RESUMED) and re-fired
                    # ~5min later, pure noise. The reset+2min fire is the
                    # precise revival; the ladder stays the catch-all. Workers
                    # with NO parsed reset (org 429s) never arm a schedule, so
                    # their 5-min lane — the early-clear revival path — is
                    # untouched.
                    # A throttled worker never resumes on its own (the CLI gave up the
                    # turn without firing the Stop hook) — kick it well before the
                    # threshold. The floor avoids racing the CLI's own retry backoff.
                    # Reuses the transcript path resolved for the activity check,
                    # and (pool on) the flip lane's banner read — this branch's
                    # gating conditions are a subset of that read's, so a
                    # pool-on pass through here never re-reads the tail.
                    if not banner_read:
                        banner = _limit_banner_text(log)
                    if banner is not None:
                        _send_text(window_id, NUDGE_TEXT)
                        next_emitted[stretch_nudge_key] = now
                        next_emitted[nudge_sent_key] = now
                        emit("nudged", name, f"NUDGED {name} ({elapsed // 60}min rate-limited)")
                        reset_ts = _parse_limit_reset_ts(banner, now)
                        if reset_ts is not None:
                            next_emitted[sched_key] = {"at": reset_ts, "baseline": activity}
                continue
            if state != "idle" or record.get("agent") != "worker":
                continue
            if not should_run_autoclose:
                continue
            sid = record.get("claude_sid")
            if sid in blocked_sids:
                continue
            elapsed = _compute_idle_elapsed_sec(record, current_uptime, now)
            if elapsed is None:
                continue
            if elapsed > IDLE_THRESHOLD_SEC:
                if _is_delegation_live(record):
                    continue
                line = _autoclose_idle_worker(p, record, elapsed)
                emit("autoclosed", record.get("name") or "", line)
    if QUESTIONS.is_dir():
        # Snapshot live sids once — questions whose worker is gone (auto-closed,
        # session ended, takeover-killed) should not page forever: the human
        # can't answer a worker that doesn't exist.
        _active_sids = {p.stem for p in ACTIVE.iterdir() if p.suffix == ".json"} if ACTIVE.is_dir() else set()
        for p in QUESTIONS.rglob("*.json"):
            record = _load(p)
            if record is None:
                continue
            if not _matches_manager(record, manager_name):
                continue
            if record.get("worker_sid") not in _active_sids:
                continue
            asked = record.get("asked_at")
            if not isinstance(asked, (int, float)) or asked <= 0:
                continue
            elapsed = now - int(asked)
            if elapsed > QUESTION_THRESHOLD_SEC:
                elapsed_min = elapsed // 60
                threshold = _highest_threshold(elapsed_min, QUESTION_THRESHOLD_MIN)
                if threshold is not None:
                    qid = record.get("question_id") or p.stem
                    key = f"question:{qid}"
                    next_emitted[key] = threshold
                    last = emitted.get(key)
                    if not (isinstance(last, int) and last >= threshold):
                        emit("question", record.get("worker_name", ""),
                             f"STALE_QUESTION {qid} worker={record.get('worker_name', '')} ({elapsed_min}min)",
                             key)
    pruned_cache = {s: p for s, p in codex_log_cache.items() if s in seen_codex_sids}
    if pruned_cache:
        next_emitted["codex_log_cache"] = pruned_cache
    # ---- flush: coalesce while the owning manager is limited -----------------
    if manager_name:
        flag_path = _limited_flag_path(manager_name)
        buffer = emitted.get("limited_buffer")
        if not isinstance(buffer, dict):
            buffer = None
        if manager_limited:
            buf = buffer or {"since": now, "stalled_names": [], "nudged": 0,
                             "resumed": 0, "questions": 0, "autoclosed": 0}
            if not isinstance(buf.get("stalled_names"), list):
                buf["stalled_names"] = []
            if not isinstance(buf.get("suppressed_keys"), list):
                buf["suppressed_keys"] = []
            for kind, event_name, line, dedup_key in events:
                # Distinct stalled/nudged names feed one list — the rollup's
                # "N workers stalled" covers both shapes of a stalled worker.
                if kind in ("stalled", "nudged") and event_name:
                    if event_name not in buf["stalled_names"] and len(buf["stalled_names"]) < 50:
                        buf["stalled_names"].append(event_name)
                # The buffered SWITCHED line never replays after the rollup —
                # its mention in the rollup (plus the ledger) IS the visibility.
                # No dedup key (flips self-dedup via the pointer) and no worker
                # counter increments for this kind.
                if kind == "switched":
                    buf["switched"] = line.removeprefix("SWITCHED ")
                # The auth-401 escalation PAGE (/login the suspect account) is a
                # human-facing action, not a wake attempt at the bricked manager
                # — it must survive coalescing. Captured here and replayed after
                # the rollup so a manager bricked on its OWN 401 still pages the
                # human once the takeover-recovery flushes.
                if kind == "auth-escalate":
                    pages = buf.setdefault("auth_pages", [])
                    if isinstance(pages, list) and line not in pages and len(pages) < 20:
                        pages.append(line)
                if (dedup_key and dedup_key not in buf["suppressed_keys"]
                        and len(buf["suppressed_keys"]) < 200):
                    buf["suppressed_keys"].append(dedup_key)
                counter = {"nudged": "nudged", "resumed": "resumed",
                           "question": "questions", "autoclosed": "autoclosed"}.get(kind)
                if counter:
                    buf[counter] = _safe_int(buf.get(counter)) + 1
            next_emitted["limited_buffer"] = buf
            try:
                flag_path.touch()
            except OSError:
                pass
            # No prints at all: each line is a task-notification at a manager
            # that cannot act on it. The flag also holds the monitor.py scans
            # (questions/done/turn-ends) — those replay in full on recovery.
        else:
            printed_any = False
            if buffer is not None or flag_path.exists():
                # Build the rollup BEFORE clearing the flag: once it clears,
                # the released done scan can mark events seen within seconds
                # and the "K done events" count would undercount.
                rollup = _build_rollup_line(buffer or {}, manager_name, now)
                # Un-burn the rungs whose lines only ever reached the buffer:
                # dropping the dedup key re-fires the same crossing live on the
                # next scan instead of waiting for the next doubling.
                suppressed = (buffer or {}).get("suppressed_keys")
                if isinstance(suppressed, list):
                    for suppressed_key in suppressed:
                        if isinstance(suppressed_key, str):
                            next_emitted.pop(suppressed_key, None)
                flag_path.unlink(missing_ok=True)
                print(rollup)
                printed_any = True
                # Replay any buffered auth-401 /login pages — the rollup itself
                # only summarizes counts; the page text must reach the human.
                for page in (buffer or {}).get("auth_pages") or []:
                    if isinstance(page, str):
                        print(page)
            # Urgent kinds print live; informational kinds (OUTBOX_DIVERT_KINDS)
            # ride a wake that is already happening — printed alongside other
            # lines, or buffered to notify-outbox/<mgr>/ for monitor.py's
            # scans to piggyback, with the timeout flush as the latency bound.
            direct = [e for e in events if e[0] not in OUTBOX_DIVERT_KINDS]
            diverted = [e for e in events if e[0] in OUTBOX_DIVERT_KINDS]
            for _kind, _event_name, line, _dedup_key in direct:
                print(line)
                printed_any = True
            if printed_any:
                for _kind, _event_name, line, _dedup_key in diverted:
                    print(line)
                _drain_outbox(manager_name)
            else:
                for seq, (kind, _event_name, line, _dedup_key) in enumerate(diverted):
                    _outbox_write(manager_name, kind, line, now, seq)
                oldest = _outbox_oldest_ts(manager_name)
                if oldest is not None and now - oldest >= OUTBOX_MAX_HOLD_SEC:
                    _drain_outbox(manager_name)
    else:
        for _kind, _event_name, line, _dedup_key in events:
            print(line)
    try:
        _write_json_atomic(emitted_state_path, next_emitted)
    except Exception as e:
        print(f"stale_monitor: failed to write {emitted_state_path} ({e})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="One-shot scan for stale orchestrator state.")
    parser.add_argument(
        "--manager",
        default=None,
        help="Scope the scan to this manager's workers. "
             "Omit for global (all managers') behavior.",
    )
    args = parser.parse_args()
    sys.exit(main(manager_name=args.manager))
