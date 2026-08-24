#!/usr/bin/env python3
"""One-shot scan for stale dockwright state.

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
2:20am (Etc/GMT-9)"). When the worker fast-path detects it, a second
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

Account auto-switch (pool of per-config-dir logins): the pointer file
(account-active) names the registry account new spawns authenticate as (the
default account rides ~/.claude, every other account its own CLAUDE_CONFIG_DIR
farm; each config dir has its own keychain login, no injected token). The pool
comes from the package-written registry snapshot (account-registry.json —
names in order, default, config_dir overrides; absent/corrupt falls back to
the historical a/b pair). When a limit banner bricks a worker or the owning
manager on the pointer account, the scan flips the pointer to the first other
registry account not inside its own brick window — guarded by a flip cooldown
(env CLAUDE_ORCH_FLIP_COOLDOWN_MIN, default 30min) and a keychain-unlocked
probe (a recovery tab opening onto a locked keychain would prompt
SecurityAgent on claude's own per-config-dir login read). A single-account
registry has NOWHERE to flip: that lane no-ops with a ledgered flip-skip
(deduped per cooldown) instead of inventing an account. The
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
as with no pool at all. If EVERY account is bricked, an already-flipped
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
import select
import shlex
import shutil
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


def _awake_seconds() -> float:
    """Duplicated from hooks._awake_seconds — this file is standalone/stdlib-only.
    Monotonic seconds that PAUSE during system sleep. macOS: CLOCK_UPTIME_RAW
    (CLOCK_MONOTONIC there keeps ticking through sleep). Linux has no
    CLOCK_UPTIME_RAW; time.monotonic() is CLOCK_MONOTONIC, which excludes
    suspend — the same awake-only semantics."""
    clk = getattr(time, "CLOCK_UPTIME_RAW", None)
    if clk is not None:
        return time.clock_gettime(clk)
    return time.monotonic()


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
# A worker that backgrounded a Bash command ENDS ITS TURN, so the Stop hook
# marks it idle (turn-truth, deliberately) and autoclose reaps the live work at
# the TTL. A live direct child extends that deadline instead of vetoing the
# close: the one real failure is a shell that never exits (tail -f, a poller, a
# docker run waiting on stdin), and without a cap autoclose would be off for
# that worker forever. The marker is the Claude CLI's shell-snapshot path; it is
# ONE of two signals — see _has_live_background_shell for why a vendor path
# alone is not enough.
BUSY_SHELL_MARKER = "shell-snapshots/snapshot-"
BUSY_SHELL_IDLE_MULTIPLIER = 3
# How many autoclose gate RE-OPENINGS can separate two evaluations of one
# record: 1 base + 1 per skew source. Today 2 — the base plus
# _record_action_ahead persisting `last_autoclose_run` when a nudge fires
# early in a scan (before the sweep runs), so a hard kill in between skips a
# full cadence. It is a BUDGET, not a bound: two chained skew events would
# need 3. Named once and derived at both sites — the deadline floor below and
# the floor's test — because it is NOT the same 3 as BUSY_SHELL_IDLE_MULTIPLIER
# and the two were previously indistinguishable literals in two files. A third
# skew source is a one-constant edit here; leave it a bare number and the
# discovery has to reach a literal in a test file no source-side reader opens.
AUTOCLOSE_SKEW_CADENCES = 2
# Orphan-window alarm: a pane in the workers tmux session with no backing
# active record — the report-only sweep's "orphan terminal window" — pages on
# the doubling ladder once continuously orphan past the grace (the VM-E2E
# ghost sat invisible for 22 minutes). Spawn-in-flight windows are protected
# by their assignments/.pending/*.window sidecar until the SessionStart claim
# consumes it. Session name is a literal (standalone script);
# test_stale_monitor pins it to terminal.WORKERS_OS_WINDOW_CLASS.
WORKERS_SESSION_NAME = "claude-workers"
ORPHAN_GRACE_SEC = _env_positive_int("CLAUDE_ORCH_ORPHAN_GRACE_SEC", 120)

# A visible gardener run deliberately never registers an active record; its
# wrapper shields the live pane via gardener/live-windows/<run_id>.window.
# Protection is honored only while the sidecar is mtime-fresh — a crashed
# wrapper's leaked sidecar ages out and the alarm resumes (fail toward
# alarming). TTL covers TIMEOUT+GRACE for both lanes (2700+900 max) + margin.
GARDENER_WINDOW_PROTECT_TTL_SEC = _env_positive_int(
    "CLAUDE_ORCH_GARDENER_WINDOW_PROTECT_TTL_SEC", 7200)

# Approval-prompt stall detection (E2E N-4): a worker pane sitting on a
# permission dialog is invisible for 30min (STALE_PROCESSING) without this.
# A dialog = BOTH a question marker AND an option-row marker in the pane tail
# (the double condition keeps task output that merely PRINTS a marker string
# from paging). Markers are lowercase (matched against text.lower()) and
# version-drifty — the trust dialog reworded between CC releases, so both
# generations ship; extend the tuple when a new wording is first seen.
APPROVAL_QUESTION_MARKERS = (
    "do you want to proceed?",
    "requires approval",
    "do you trust the files in this folder",           # trust dialog, older CC
    "is this a project you created or one you trust",  # trust dialog, ≥2.1.211
)
APPROVAL_OPTION_MARKERS = ("❯ 1.", "1. yes")
APPROVAL_TAIL_LINES = 40
APPROVAL_EXCERPT_MAX = 160
APPROVAL_REPAGE_BASE_MIN = 5

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
ASSIGNMENTS_PENDING = ROOT / "assignments" / ".pending"
GARDENER_LIVE_WINDOWS = ROOT / "gardener" / "live-windows"
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
# Same-account takeover bet: taken ONLY on the episode's FIRST distinct 401
# (a transient server blip has typically cleared by launch time, and the other
# account is equally exposed to it). From the second distinct in-window 401 the
# account is SUSPECT: launch only via _healthy_takeover_target, else promote
# the decision to escalate (2026-07-29: two recover-launches onto currently-
# 401ing accounts made zombie managers — dead on arrival, identity conferred
# by the SessionStart hook before the model ever ran).
AUTH_401_SAME_ACCOUNT_ATTEMPTS = 1
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
# "resets 2:20am (Etc/GMT-9)" — hour, optional :minutes, am/pm, IANA zone.
_RESET_CLAUSE_RE = re.compile(
    r"resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*\(([^)]+)\)", re.IGNORECASE)


# ---- account auto-switch (pool of per-config-dir logins; design 2026-06-15) --
# The pointer selects which registry account new spawns authenticate as (the
# default account rides ~/.claude, every other account a CLAUDE_CONFIG_DIR
# farm) — each config dir has its own keychain login (no injected token). The
# pool itself comes from the package-written registry snapshot (_registry
# below). DORMANCY INVARIANT: every helper below no-ops unless the pointer
# file holds a valid registry name — `rm account-active` is a full disable
# (no state writes, no ledger lines, no flips, no recovery launches).
ACCOUNT_ACTIVE = ROOT / "account-active"
ACCOUNT_LEDGER = ROOT / "account-flips.jsonl"
ACCOUNT_STATE = ROOT / "account-state.json"
ACCOUNT_LOCK = ROOT / ".account-flip.lock"
FLIP_COOLDOWN_SEC = _env_positive_int("CLAUDE_ORCH_FLIP_COOLDOWN_MIN", 30) * 60
TAKEOVER_GUARD_SEC = 300          # recovery tab must take over within this window
BRICK_EPISODE_GAP_SEC = 600       # banner unseen this long ⇒ next sighting is a new episode
ACCOUNT_REGISTRY = ROOT / "account-registry.json"
_LEGACY_REGISTRY = (["a", "b"], "a", {})


def _registry():
    """(pool names in order, default account, {name: config_dir}) from the
    package-written snapshot (spawner.write_registry_snapshot — refreshed on
    every MCP boot, worker spawn, and deploy). Absent/corrupt -> the historical
    a/b pair, byte-for-byte the pre-registry behavior, so a deploy gap can
    never behave worse than today. This standalone script cannot import the
    package (or tomllib on old interpreters) — the snapshot IS the contract."""
    try:
        data = json.loads(ACCOUNT_REGISTRY.read_text())
        names, dirs = [], {}
        for entry in data.get("pool") or []:
            name = entry.get("name") if isinstance(entry, dict) else None
            if not isinstance(name, str) or not name or name in names:
                return _LEGACY_REGISTRY
            names.append(name)
            cd = entry.get("config_dir")
            if isinstance(cd, str) and cd:
                dirs[name] = cd
        if not names:
            return _LEGACY_REGISTRY
        default = data.get("default")
        if default not in names:
            default = names[0]
        return (names, default, dirs)
    except Exception:
        return _LEGACY_REGISTRY


def _pool_account() -> str | None:
    """rstrip("\\n") only — NOT .strip(): must match spawner._pick_account /
    spawner._active_account byte-for-byte so the flip lane and the spawn gate
    agree on which pointer is valid (a whitespace-padded letter would word-split
    inside the shell-side $(cat) and yield a lying account stamp)."""
    try:
        letter = ACCOUNT_ACTIVE.read_text().rstrip("\n")
    except Exception:
        return None
    return letter if letter in _registry()[0] else None


def _account_of(record: dict, pool_letter: str) -> str:
    stamped = record.get("account")
    return stamped if stamped in _registry()[0] else pool_letter


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
    standalone script can't import the package). The registry default account
    -> default ~/.claude (no CLAUDE_CONFIG_DIR); any other registry account ->
    CLAUDE_CONFIG_DIR=<its registry config_dir override, else ~/.claude-<letter>>
    iff its .claude.json is healthy (has the orchestrator MCP), else fall back
    to the default login with a truthful effective stamp of the default account.
    Workers build/maintain the farms; here we only CHECK.

    KNOWN FAILURE MODE: recovery onto a non-default account assumes a worker
    has already built its farm. If the default account bricks before any worker
    built the farm, the flip launches a recovery manager that falls back here
    to the DEFAULT login stamped with the default name — i.e. onto the
    just-bricked account, which may re-brick. The flip now only targets
    *registry* accounts, but farm health stays ungated (gating the flip on it
    would force every flip test to seed a healthy farm). Instead this is
    bounded by the once+once recovery-launch guard and self-heals once a
    worker rebuilds the farm; the fallback emits the stderr warning below so
    the degradation is observable."""
    _names, default, dirs = _registry()
    effective = letter
    config_dir = None
    if letter != default:
        farm = Path(dirs.get(letter) or os.path.expanduser(f"~/.claude-{letter}"))
        cj = farm / ".claude.json"
        try:
            data = json.loads(cj.read_text())
            servers = (data.get("mcpServers") or {}) if isinstance(data, dict) else {}
            # claude-orchestrator: one-release legacy MCP key recognition
            if "dockwright" in servers or "claude-orchestrator" in servers:
                config_dir = farm
            else:
                effective = default
        except Exception:
            effective = default
        if config_dir is None:
            print(f"stale_monitor: account-{letter} farm {farm}/.claude.json "
                  f"not healthy; recovery falls back to the DEFAULT login (stamp "
                  f"{default}) — the recovery tab may land on the bricked account "
                  f"until a worker rebuilds the farm", file=sys.stderr)
    parts = []
    if config_dir is not None:
        parts.append(f"CLAUDE_CONFIG_DIR={shlex.quote(str(config_dir))}")
    parts.append(f"CLAUDE_ORCH_ACCOUNT={shlex.quote(effective)}")
    return " ".join(parts) + " "


def _login_fix_command(letter: str) -> str:
    """Exact re-login command for `letter`. The default account rides the
    HOME-root login: pointing the CLI at ~/.claude via CLAUDE_CONFIG_DIR
    reads a directory that never held the config and reports a healthy login
    as dead (2026-07-29 incident) — so the default's command carries NO
    CLAUDE_CONFIG_DIR. Every other account: its registry config_dir override,
    else the ~/.claude-<letter> convention — the same resolution as
    _account_config_prefix, minus the farm-health fallback (a human
    re-logging-in must target the farm even when it is unhealthy)."""
    _names, default, dirs = _registry()
    if letter == default:
        return "claude"
    farm = dirs.get(letter) or os.path.expanduser(f"~/.claude-{letter}")
    return f"CLAUDE_CONFIG_DIR={shlex.quote(str(farm))} claude"


def _notify_macos(message: str) -> None:
    """Best-effort direct-to-human notification (inline copy of
    hooks._notify_macos — standalone stdlib-only file, kept parity-tested by
    test_notify_macos_matches_hooks_inline_copy). Used ONLY where no
    successor manager will ever exist to relay a buffered page: an
    outcome-derived auth-401 recover/escalate notification. Every failure
    (no osascript, sandbox, timeout) is swallowed — the caller never sees an
    exception — but, unlike hooks' 5s-SessionEnd-budget copy, this one warns
    on stderr (every other best-effort helper in this file does; the
    likeliest real failure — notification permissions denied — is a
    non-zero exit that `check=False` would otherwise discard silently).
    No-ops under pytest (PYTEST_CURRENT_TEST) so suites never fire real
    desktop notifications."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        sanitized = message.replace('"', "")
        result = subprocess.run(
            ["osascript", "-e",
             f'display notification "{sanitized}" with title "dockwright"'],
            capture_output=True, timeout=2, check=False,
        )
        if result.returncode != 0:
            print(f"stale_monitor: notify failed (osascript rc="
                  f"{result.returncode})", file=sys.stderr)
    except Exception as e:
        print(f"stale_monitor: notify failed ({e})", file=sys.stderr)


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


def _auth_401_active(account: str, now: int, state: dict | None = None) -> bool:
    """True iff `account` has a live in-window auth-401 episode — within
    AUTH_401_WINDOW_SEC of the last DISTINCT 401 (`last_distinct`; falls back
    to `last_seen` for entries read before any new-code write touches them —
    a legacy-format read, not a live gap: `_record_auth_401` backfills
    `last_distinct` via `setdefault` on its very next write to that account).
    A live episode disqualifies an account as a takeover TARGET or flip
    DESTINATION: launching onto a mid-401 account is the zombie factory one
    account over (2026-07-29: a 401'd in the morning, b in the afternoon — a
    flip a→b at 15:24 would have launched the takeover onto 401ing b).
    Keying on `last_distinct` rather than `last_seen` (Tier-2 round-2 B-1) is
    load-bearing: the worker lane calls `_record_auth_401` unconditionally
    every scan, so a dead unreaped 401'd worker re-presenting the SAME uuid
    would otherwise refresh `last_seen` forever and permanently disqualify
    its account as a flip destination — measured as a permanent rate-limit
    flip stall. Read outside the flip lock on purpose (read-only heuristic;
    _write_json_atomic keeps snapshots consistent). Crash/absent state reads
    False — fall back to the pre-guard behavior rather than dead-ending
    every flip on a broken state file. Consulted for the attempt-1
    same-account bet too (Tier-2 round-2 residual 1): the suspect's own
    episode is live by definition when `pool == account`, so that case
    bypasses this check by construction; a prior flip leaving the pointer on
    a DIFFERENT, currently-401ing account no longer launches onto it
    blindly."""
    try:
        if state is None:
            state = _load_account_state()
        entry = (state.get("auth_401") or {}).get(account)
        last_distinct = entry.get("last_distinct", entry.get("last_seen")) \
            if isinstance(entry, dict) else None
        return (isinstance(entry, dict)
                and isinstance(last_distinct, (int, float))
                and now - last_distinct <= AUTH_401_WINDOW_SEC)
    except Exception:
        return False


def _flip_target(pointer: str, state: dict, now: int) -> str | None:
    """First registry account != pointer, pool order, not currently bricked,
    and not carrying a live auth-401 episode (_auth_401_active) — flipping
    the pointer onto a mid-401 account would spawn every subsequent worker
    dead, and a takeover launched there is born dead. Applies to BOTH the
    rate-limit flip lane and the auth-401 escalate flip. None => nowhere to
    flip."""
    for name in _registry()[0]:
        if (name != pointer
                and not _entry_bricked(state.get("accounts", {}).get(name), now)
                and not _auth_401_active(name, now, state)):
            return name
    return None


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


def _record_auth_401(account: str, uuid: str | None, now: int) -> tuple[str, int]:
    """Per-account auth-401 attempt counter (uuid-deduped). Returns
    (action, attempts). action: "duplicate" (this exact 401 was already acted
    on — the resume hasn't fired/cleared yet, so don't re-trigger or inflate
    the count), "recover" (a fresh 401 within the bound — trigger a
    SAME-account kill+resume), or "escalate" (more than AUTH_401_MAX_ATTEMPTS
    failed same-account resumes inside AUTH_401_WINDOW_SEC — the credential is
    suspect, flip + page). attempts is the in-window distinct-401 count
    backing the action — the manager lane uses it to bound the same-account
    takeover bet to the episode's FIRST attempt
    (AUTH_401_SAME_ACCOUNT_ATTEMPTS).

    Per-ACCOUNT (not per-session) aggregation is the right grain for "is this
    token dead": two sessions on one account 401'ing in lockstep is strong
    evidence the token — the only thing they share — is the problem, while the
    incident's one-each across two accounts is a shared server blip that
    same-account recovery clears. State lives in its own ACCOUNT_STATE namespace
    (`auth_401`) so it never perturbs the rate-limit brick guards (`accounts`).
    Crash-proof: any failure reads as ("recover", 1) — act, don't escalate,
    and attempts=1 keeps the first-attempt bet (an unreadable state file must
    not dead-end recovery); mirrors _record_brick's flat _flip_lock usage (no
    nesting: the caller does _maybe_flip_account separately on escalate).

    Two clocks, two meanings (Tier-2 round-2 B-1): `last_seen` bounds
    attempt-count rollover and is refreshed by BOTH fresh attempts and
    duplicates — a persistent unhandled 401 must keep the episode's attempt
    count alive. `last_distinct` bounds destination-health suspicion
    (consulted by `_auth_401_active`) and is advanced ONLY by fresh distinct
    uuids — a dead session re-presenting the SAME banner forever must not
    hold its account hostage as a flip/takeover destination. Measured
    pre-fix: a permanent rate-limit-flip stall (a dead unreaped 401'd worker
    re-reporting the same uuid kept its account "active" indefinitely)."""
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
                    # One-time legacy backfill: an entry written before this
                    # code existed has no last_distinct — the pre-refresh
                    # last_seen is the best estimate of its last distinct 401.
                    entry.setdefault("last_distinct", entry.get("last_seen"))
                    entry["last_seen"] = now
                    namespace[account] = entry
                    _write_json_atomic(ACCOUNT_STATE, state)
                    return "duplicate", _safe_int(entry.get("attempts"))
                attempts = _safe_int(entry.get("attempts")) + 1
                uuids = (seen + [uuid])[-8:] if uuid is not None else seen
            else:
                attempts = 1
                uuids = [uuid] if uuid is not None else []
            namespace[account] = {"attempts": attempts, "last_seen": now,
                                  "last_distinct": now, "uuids": uuids}
            _write_json_atomic(ACCOUNT_STATE, state)
            return ("recover" if attempts <= AUTH_401_MAX_ATTEMPTS else "escalate"), attempts
    except Exception as e:
        print(f"stale_monitor: auth-401 record failed ({e})", file=sys.stderr)
        return "recover", 1


def _healthy_takeover_target(suspect: str, pool: str,
                             new_letter: str | None = None,
                             pool_suspect: bool = False) -> str | None:
    """Shared invariant of the manager auth-401 recover and escalate branches:
    a suspect account is never SELECTED as the takeover target (the
    pre-existing _account_config_prefix farm-health fallback can still land
    the spawn on the default login — which may be the suspect account itself
    — see its KNOWN FAILURE MODE docstring). The healthy
    target is the just-flipped letter, or (if a flip
    already landed earlier) the current pointer when it differs from the
    suspect account. Neither ⇒ None: the caller must not launch (escalate /
    page instead — never wait for a launch slot on the suspect account).
    Extracted from the escalate branch so the recover branch shares it
    verbatim: on 2026-07-29 the recover branch, lacking this guard, launched
    two takeovers onto currently-401ing accounts — both born dead with a
    manager identity conferred by the SessionStart hook (the zombie factory).
    `pool_suspect` is the caller's `_auth_401_active(pool, now)` — a pointer
    that is itself mid-401 is not healthy; the `new_letter` arm needs no such
    flag because `_flip_target` already refuses 401-active destinations.
    """
    if new_letter is not None:
        return new_letter
    if pool != suspect and not pool_suspect:
        return pool
    return None


def _maybe_flip_account(bricked_account: str, reason: str, now: int) -> str | None:
    """Flip the pointer to another registry account iff ALL guards pass.
    Returns the new name, or None (already flipped / cooling down / keychain
    locked / nowhere to flip). A single-account registry can NEVER flip — that
    lane no-ops with a ledgered flip-skip instead of inventing an account."""
    try:
        with _flip_lock():
            pointer = _pool_account()
            if pointer is None or pointer != bricked_account:
                return None
            state = _load_account_state()
            last_flip = state.get("last_flip") or {}
            last_ts = last_flip.get("ts")
            if isinstance(last_ts, (int, float)) and now - last_ts < FLIP_COOLDOWN_SEC:
                return None
            if not _keychain_unlocked():
                return None
            other = _flip_target(pointer, state, now)
            if other is None:
                if len(_registry()[0]) <= 1:
                    _ledger_flip_skip(state, pointer, now)
                else:
                    excluded = [n for n in _registry()[0]
                                if n != pointer and _auth_401_active(n, now, state)]
                    if excluded:
                        _ledger_flip_refused_auth401(state, pointer, excluded, now)
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


def _ledger_flip_skip(state: dict, pointer: str, now: int) -> None:
    """Once per FLIP_COOLDOWN_SEC per account. The flip lane re-attempts every
    ~60s scan for the whole brick episode; an unthrottled skip line would
    flood account-flips.jsonl, whose last-64KB tail backs the recovery-launch
    once+once bound (_ledger_recovery_launches)."""
    last = state.get("last_flip_skip") or {}
    if (last.get("account") == pointer
            and isinstance(last.get("ts"), (int, float))
            and now - last["ts"] < FLIP_COOLDOWN_SEC):
        return
    try:
        state["last_flip_skip"] = {"ts": now, "account": pointer}
        _write_json_atomic(ACCOUNT_STATE, state)
        _append_account_ledger({"ts": now, "event": "flip-skip",
                                "reason": "no other account in registry",
                                "account": pointer, "by": "stale_monitor"})
        print(f"stale_monitor: account {pointer} bricked; no other account in "
              f"registry — flip skipped", file=sys.stderr)
    except Exception as e:
        print(f"stale_monitor: flip-skip bookkeeping failed ({e})", file=sys.stderr)


def _ledger_flip_refused_auth401(state: dict, pointer: str,
                                 excluded: list, now: int) -> None:
    """Once per FLIP_COOLDOWN_SEC per pointer, mirroring _ledger_flip_skip:
    the flip lane re-attempts every scan for the whole episode and an
    unthrottled line would flood the ledger tail that backs the launch
    bounds. A 401-active refusal can hold for a while (B-1: bounded by
    last_distinct aging) — it must be diagnosable from the ledger, not
    reconstructed by a forensics worker (Tier-2 residual 3). Deliberate
    mirror deviation from _ledger_flip_skip: no success-path stderr line —
    a 401-active refusal is a routine, frequent occurrence during an
    aging-out episode (unlike a single-account-registry skip), and the
    ledger line is already the durable record; a matching stderr print
    would just be per-scan noise."""
    last = state.get("last_flip_refused_auth401") or {}
    if (last.get("account") == pointer
            and isinstance(last.get("ts"), (int, float))
            and now - last["ts"] < FLIP_COOLDOWN_SEC):
        return
    try:
        state["last_flip_refused_auth401"] = {"account": pointer, "ts": now}
        _write_json_atomic(ACCOUNT_STATE, state)
        _append_account_ledger({"ts": now, "event": "flip-refused-auth401",
                                "from": pointer, "excluded": excluded,
                                "by": "stale_monitor"})
    except Exception as e:
        print(f"stale_monitor: flip-refused-auth401 bookkeeping failed ({e})",
              file=sys.stderr)


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


def _interactive_shell() -> str:
    """Duplicated from spawner._interactive_shell — this file is standalone/stdlib-only.
    Shell for the spawn `-ic` argv. The inner command uses POSIX `K=v cmd`
    env-prefix syntax, so an exotic $SHELL (fish, nushell) can't run it —
    honor $SHELL only when it's zsh/bash; otherwise fall down a fixed
    POSIX-family order. `-i` is load-bearing: the interactive rc is what puts
    the user's `claude`/`codex` on PATH. Stock Ubuntu ships no zsh — a
    hardcoded zsh argv made every spawn die at exec (empty dead pane)."""
    sh = os.environ.get("SHELL", "")
    if os.path.basename(sh) in ("zsh", "bash") and shutil.which(sh):
        return sh
    for cand in ("zsh", "bash"):
        found = shutil.which(cand)
        if found:
            return found
    return "sh"


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
    # E2E F-2: ride the deployed manager allowlist so the autonomous recovery
    # boot doesn't stall on approval prompts. Composed from the module ROOT
    # (stdlib-only — this file can't import paths). Absent = old behavior.
    settings_path = ROOT / "presets" / "manager-settings.json"
    settings_arg = (f"--settings {shlex.quote(str(settings_path))} "
                    if settings_path.is_file() else "")
    # Same argv tail as manager_launch.manager_claude_args() (inline copy —
    # this module is standalone stdlib-only and can't import the package):
    # remote control default-ON via the reliable --remote-control flag;
    # DOCKWRIGHT_MANAGER_RC=0 opts out. Keep in sync with the helper.
    rc_arg = ("--remote-control "
              if os.environ.get("DOCKWRIGHT_MANAGER_RC", "").strip() != "0" else "")
    # OPT-IN, default OFF (inline copy of manager_claude_args(), keep in
    # sync): DOCKWRIGHT_MANAGER_SKIP_PERMS=1 removes the Bash safety
    # classifier for the recovered manager — sanctioned only for
    # manager.core.md's two named uses. Bare flag: parse-safe before --model.
    # EXACT compare, deliberately no .strip(): bootstrap-recreate.sh is the
    # reference lane and bash's `=` never normalizes.
    skip_arg = ("--dangerously-skip-permissions "
                if os.environ.get("DOCKWRIGHT_MANAGER_SKIP_PERMS", "") == "1"
                else "")
    inner = (
        f"{_account_config_prefix(new_letter)}"
        f"CLAUDE_AGENT=manager CLAUDE_WORKER_NAME={shlex.quote(name)} "
        # Identity is acquired by become_manager_with_takeover, never by the
        # SessionStart hook: a tab whose first API call 401s must not mint a
        # ghost manager record it can never use. Command-string only on the
        # daemon side (never daemon os.environ); the successor's MCP server
        # pops it at registration (become_manager_impl) so its own later
        # spawns can't birth a tmux server with the var sticky in global env.
        f"DOCKWRIGHT_PENDING_TAKEOVER=1 "
        # Manager lane is pinned (orch-audit model-allocation): never inherit
        # the user's interactive model default. Quoted so the -ic shell can't glob [1m].
        # rc_arg BEFORE --model: --remote-control [name] would otherwise bind the
        # trailing /manager-takeover-recovery prompt as the RC session name (see
        # manager_claude_args docstring). --model interposes a dash-option.
        f"claude {rc_arg}{skip_arg}--model {shlex.quote('claude-opus-5[1m]')} {settings_arg}"
        f"{shlex.quote(f'/manager-takeover-recovery {mgr_sid}')}"
    )
    # One-shot guarantee: `inner` already carries the flag; TmuxDriver.spawn
    # can BIRTH the server (new-session branch) with this daemon's env, which
    # would make the var sticky for every future window. Compose-then-pop.
    os.environ.pop("DOCKWRIGHT_MANAGER_SKIP_PERMS", None)
    if _get_driver is None:
        print("stale_monitor: recovery launch skipped (driver unavailable)", file=sys.stderr)
        return None
    try:
        return asyncio.run(asyncio.wait_for(
            _get_driver().spawn(
                cwd=cwd, title="manager (recovery)", argv=[_interactive_shell(), "-ic", inner],
                route_to_manager_session=True),
            timeout=10)) or None
    except Exception as e:
        print(f"stale_monitor: recovery launch failed ({e})", file=sys.stderr)
        return None


def _safe_bucket(name: str) -> str:
    """Mirror of paths._event_bucket, including the "." / ".." guard.

    Those two survive the separator swap and are TRAVERSAL, not names, so a
    bucket named ".." lands outside the tree meant to contain it. Duplicated
    because this file ships standalone; pinned by test_lane_liveness_mirror.py
    so the two copies cannot disagree about what a legal bucket name is.
    """
    bucket = name.replace("/", "_").replace("\\", "_")
    return f"_{bucket}" if bucket in (".", "..") else bucket


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
    return ROOT / f".stale-emitted-{_safe_bucket(manager_name)}.json"


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


def _busy_shell_deadline() -> int:
    """Idle seconds a worker with a live background shell is allowed.

    max(3x TTL, TTL + (SKEW+1) cadences). The floor is not spare margin: bare
    3x gives a window of 2*TTL, and a small TTL makes that window narrower than
    the gap between two autoclose evaluations — consecutive passes step OVER it
    and the guard is a silent no-op with every test green.

    The gap the floor must outrun is AUTOCLOSE_SKEW_CADENCES * (CADENCE + 60),
    today 2 * 3660 = 7320s. Each re-opening of the hourly gate lands on the
    first 60s tick at or after `last + CADENCE`, and under one skew event there
    are TWO such re-openings, so the scan step is charged twice, not once.
    Today's single skew source is _record_action_ahead (see its docstring,
    "Known shape, safe direction"). A floor of two cadences (7200s) does not
    cover 7320s; (SKEW+1) = three does, at 10800s.

    On the default 2h TTL the floor is a no-op: max(21600, 18000) = 21600,
    i.e. the plain 3x.

    ⚠️ The floor only governs the distance between evaluations a record
    actually REACHES. Two `continue`s upstream of the cap — the blocked-sids
    skip and `_is_delegation_live` — remove a record from evaluation entirely
    and are bounded by no number of cadences, so no floor value can cover them.
    A worker holding BOTH a live subagent and a background shell can therefore
    still be reaped: the delegation hold keeps it out of the sweep for the
    subagent's whole run, and it can surface past the cap. Not a regression
    (pre-guard it died at the bare TTL), but "the floor makes the window
    reachable" means reachable ONCE THE RECORD REACHES THE CHECK.

    TTL <= 0 is an operator saying "close immediately" (the env var is parsed
    with a bare float(), so 0 and negatives arrive here). The floor would
    overrule that with a multi-hour hold, so the deadline collapses to the
    threshold and the window is empty by design.
    """
    if IDLE_THRESHOLD_SEC <= 0:
        return IDLE_THRESHOLD_SEC
    return max(IDLE_THRESHOLD_SEC * BUSY_SHELL_IDLE_MULTIPLIER,
               IDLE_THRESHOLD_SEC
               + AUTOCLOSE_CADENCE_SEC * (AUTOCLOSE_SKEW_CADENCES + 1))


def _process_index() -> dict | None:
    """One `ps` per scan -> {"command_by_pid": {...}, "child_commands": {...}}.

    `pgrep -P <pid>` is the obvious form and was measured to LIE: it hides the
    caller's own ancestors, so a live shell present in `ps` was absent from
    `pgrep`. One `ps` per scan also beats one `pgrep` per worker, and argv[0]
    (the pid-recycling check) comes free in the same snapshot.

    errors="replace" is load-bearing: text=True decodes strictly, and a single
    live process with non-UTF-8 bytes in argv raises UnicodeDecodeError — a
    ValueError, not an OSError. Likewise `except Exception`, not a list of
    types: the same call is stubbed in tests with objects that have no
    .stdout (AttributeError). Either escape would propagate out of main() and
    kill EVERY scan while that process lives — no stale pages, no nudges, no
    autoclose. Same contract _last_activity states: one poison path must never
    abort monitoring for the rest.

    timeout: a hung `ps` would block the whole scan, not just this branch.
    """
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            capture_output=True, text=True, errors="replace", timeout=10,
        )
        if proc.returncode != 0:
            return None
        command_by_pid: dict[int, str] = {}
        child_commands: dict[int, list[str]] = {}
        for line in proc.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) != 3:
                continue
            pid_s, ppid_s, command = parts
            if not (pid_s.isdigit() and ppid_s.isdigit()):
                continue
            command_by_pid[int(pid_s)] = command
            child_commands.setdefault(int(ppid_s), []).append(command)
        if not command_by_pid:
            # rc=0 with nothing parseable must degrade, not read as "no
            # children anywhere" — real ps always lists at least itself.
            return None
        return {"command_by_pid": command_by_pid, "child_commands": child_commands}
    except Exception:
        return None


def _looks_like_session(command: str) -> bool:
    """argv[0]'s basename only. A claude/codex-shaped token elsewhere in the
    command line (a path arg ending in /claude, a container --name claude)
    must not read as a session — it would hide a genuine orphan, and the
    preflight mirror would trust a recycled pid. Every real session's argv[0]
    is literally `claude`/`codex` or an absolute path to it (the `zsh -ic
    claude ...` spawn wrapper never matters: the claude child it forks is
    always the process actually checked or walked through)."""
    tokens = command.split()
    return bool(tokens) and os.path.basename(tokens[0]) in ("claude", "codex")


def _has_live_background_shell(record: dict, index: dict | None) -> bool:
    """True when this idle worker's pid still has a live Bash-tool child.

    All four must hold:
    1. runtime is claude — the marker is a Claude CLI detail, as in
       _is_delegation_live. A future runtime is excluded here and its
       background work is killed silently; that is today's behavior, pinned
       by a test so adding a runtime cannot pass green.
    2. the recorded pid is a positive int. _write_record defaults it to 0 and
       0 is a valid int, so the threshold states the intent instead of leaning
       on pid 0 being absent from the process table.
    3. that pid's own argv[0] basename is `claude` — a dead record must not
       inherit a recycled pid's children. `codex` is deliberately NOT accepted
       here: condition 1 already excluded non-claude records, so codex could
       only be reached by a claude record whose pid a codex process recycled,
       which is exactly what this condition exists to catch.
    4. some DIRECT child is not itself a session and either carries the
       snapshot marker OR has the `sh -c` shape.

    The nested-session exclusion in (4) is a measured class, not a hypothesis:
    of 28 marker-carrying rows on a live fleet, one had argv[0] = claude with
    the marker at offset 1865 — a session's argv carries arbitrary prompt text.

    The OR in (4) is deliberate. The marker is someone else's path: rename the
    directory in a CLI release and a marker-only predicate returns False
    forever, the fleet goes back to killing live work, and every test stays
    green because the fixtures feed the marker by hand. No honest unit test
    exists for that, so the second branch keys on the SHAPE instead of the
    vendor path. It only ADDS cases, which is the direction that fails safely:
    a spurious hit costs a few hours of delay under the cap, a miss costs
    killed work.

    ⚠️ Do not read that as "derived", though — ("zsh", "bash", "sh") is a
    hand-maintained list of three names, unguarded by construction, and
    nothing pins it (appending "fish" leaves the whole suite green). It is
    deliberately NOT `==`-pinned: adding a shell only widens the safe
    direction. The residual worth knowing is that the two branches share a
    failure only under a compound change — a renamed marker directory AND a
    login shell outside these three defeat both at once.

    index is None (broken ps) -> False HERE, but the closer reads a falsy index
    as busy and holds the worker until the cap. So a broken `ps` delays
    autoclose from the TTL to _busy_shell_deadline(); it can never make the
    fleet uncloseable, because the cap closes regardless.
    """
    if (record.get("runtime") or "claude") != "claude":
        return False
    if not index:
        # "cannot tell" from HERE is not "close" — the closer treats any falsy
        # index as busy and holds the worker to the cap. Kept as a short-circuit
        # so `index.get(...)` never runs on 0/False/{}; the close/hold decision
        # is _autoclose_idle_worker's, not this predicate's.
        return False
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    own = index.get("command_by_pid", {}).get(pid)
    if not own:
        return False
    own_tokens = own.split()
    if not own_tokens or os.path.basename(own_tokens[0]) != "claude":
        return False
    for command in index.get("child_commands", {}).get(pid, ()):
        if _looks_like_session(command):
            continue
        if BUSY_SHELL_MARKER in command:
            return True
        # `" -c " in command` is a SUBSTRING test, looser than "the sh -c
        # shape" sounds: it fires wherever that text appears, including inside
        # a payload, and it misses combined flags like `zsh -ic`. Both errors
        # ADD cases or fall back on the marker branch, so both are the safe
        # direction — this branch only ever runs when the marker is ABSENT.
        # Both error modes are live, not hypothetical: one fleet sample of 24
        # marker rows held 22 `zsh -c`, one `zsh -ic`, and one `claude
        # --settings` (excluded as a nested session). Treat any such count as a
        # moment, not a property — the mix changes with what the fleet is doing.
        tokens = command.split()
        if (tokens and os.path.basename(tokens[0]) in ("zsh", "bash", "sh")
                and " -c " in command):
            return True
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
    return ROOT / f".manager-limited-{_safe_bucket(manager_name)}"


def _outbox_dir(manager_name: str) -> Path:
    # Sanitization mirrors paths._event_bucket via the shared _safe_bucket.
    return ROOT / "notify-outbox" / _safe_bucket(manager_name)


# --- lane delivery discipline (stdlib copy of dockwright.lane_io) ----------
# This file is deployed standalone to ~/.claude/scripts/ and CANNOT import the
# package, so the emit guard is duplicated here — the same trade already made
# for _write_json_atomic. Keep the two in step; lane_io owns the rationale.
#
# This process inherits fd 1 from `dockwright monitor stale`, so its lines go
# straight to the manager's Monitor task. Without the per-line flush a dead
# reader would let it write its emitted-state cursor and unlink outbox entries
# for pages that never arrived — the exact loss the package-side fix removes.
EXIT_LANE_DEAD = 3
_READER_GONE = select.POLLERR | select.POLLHUP | select.POLLNVAL


class LaneDead(Exception):
    """The reader of this lane's stdout is gone."""


def _reader_is_dead(fd: int = 1) -> bool:
    """True only on a HUNG-UP far end. Fails OPEN — a backpressured reader
    (POLLOUT clear, no error bits) is BUSY, not gone."""
    try:
        poller = select.poll()
        poller.register(fd, select.POLLOUT)
        for _fd, revents in poller.poll(0):
            return bool(revents & _READER_GONE)
        return False
    except Exception:
        return False


def _lane_preflight(fd: int = 1) -> None:
    if _reader_is_dead(fd):
        raise LaneDead("stdout reader is gone (poll reports HUP/ERR/NVAL)")


def _emit(line: str) -> None:
    """Write one event line and flush, so failure surfaces here rather than
    being swallowed at interpreter exit."""
    try:
        sys.stdout.write(f"{line}\n")
        sys.stdout.flush()
    except (BrokenPipeError, OSError) as exc:
        raise LaneDead(f"stdout write failed: {exc}") from exc


def _detach_stdout() -> None:
    """Re-point fd 1 at /dev/null so interpreter shutdown cannot override the
    exit status with 120. See lane_io.detach_stdout — load-bearing."""
    try:
        sys.stdout.flush()
    except Exception:
        pass
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, 1)
        finally:
            os.close(devnull)
    except OSError:
        pass
# --- emitted-state key classes --------------------------------------------
# `next_emitted` is TWO ledgers wearing one dict, and a failed delivery must
# treat them oppositely:
#
#   ACTION keys record something already DONE TO THE WORLD — a nudge typed into
#   a pane, a recovery session launched, the autoclose gate advanced. Dropping
#   one does not un-do the act; it makes the NEXT scan do it again. Measured by
#   a reviewer: worker nudged, reader dies, lane re-armed, `resume your task`
#   typed into the pane TWICE.
#
#   PAGE keys record that a LINE was shown. Dropping one is correct when the
#   line never arrived — that is the at-least-once discipline working.
#
# So when delivery fails the action keys are committed and the page keys are
# discarded. The prefix lists are hand-maintained, which drift-guard-tests
# calls unguarded by construction, so tests/test_emitted_key_classes.py parses
# every key literal in this file and fails on one matching neither class.
ACTION_KEY_PREFIXES = (
    "nudge_sent:", "nudged:", "scheduled:", "recovery:", "auth-recovery:",
)
ACTION_KEY_EXACT = ("last_autoclose_run", "codex_log_cache", "limited_buffer",
                    "lane_check_tick")
# Colon-less page keys would live here. Empty today; present so a new one has
# an obvious home rather than being wedged into the prefix tuple.
PAGE_KEY_EXACT = ()
PAGE_KEY_PREFIXES = (
    "processing:", "question:", "orphan:", "approval:", "auth-emit:",
    "lane_silent:", "lane_stale_seen:",
)


def _is_action_key(key: str) -> bool:
    return (key in ACTION_KEY_EXACT
            or any(key.startswith(p) for p in ACTION_KEY_PREFIXES))


def _record_action_ahead(emitted_state_path: Path, emitted: dict,
                         next_emitted: dict, key: str, value) -> None:
    """Persist an action key BEFORE the act it guards.

    Recording after the act is only safe against exceptions. A hard kill —
    SIGKILL, OOM, the machine going down — between typing a nudge into a pane
    and reaching the end-of-scan ledger write runs no handler at all, so the
    retried scan sees no record and types it again. Write-ahead inverts which
    way the crash fails: at worst a recorded nudge that never happened, which
    the ladder re-fires on its own schedule, instead of an unrecorded nudge
    that did, which lands in a worker's pane a second time.

    ⚠️ THIS PUTS THE RECORD AHEAD OF THE ACT. Whether the recorded VALUE is
    one the next scan reads as "already done" is a separate property, and it
    held at only ONE of the four call sites when measured 2026-08-06 (re-derive,
    do not inherit) — elsewhere the suppressor
    is a flag nothing reads, an omission the ledger merge resurrects, or a page
    key this helper deliberately drops. Not a regression at any of them (the
    pre-write-ahead order re-nudged identically), but do not read placement as
    effectiveness. A behavioural per-site check is owed; see the cursor
    follow-up scope.

    Best-effort on the write: an unpersistable ledger must not stop the nudge
    the fleet is waiting on. The in-memory record still holds for this scan.

    ⚠️ Known shape, safe direction: this persists the WHOLE action half, so a
    nudge firing early in a scan also commits `last_autoclose_run`, which is
    set before the autoclose sweep runs. A hard kill in between therefore skips
    autoclose for one cadence (1 h) — a 2 h-idle worker closes at 3 h instead.
    That is a missed close, never a duplicated one, and the same skew already
    existed on the LaneDead path; it is recorded so it reads as known rather
    than as a surprise.
    """
    next_emitted[key] = value
    _commit_actions_only(emitted_state_path, emitted, next_emitted)


def _commit_actions_only(emitted_state_path: Path, emitted: dict,
                         next_emitted: dict) -> None:
    """Persist the acts, drop the pages. Called when delivery failed."""
    try:
        keep = {k: v for k, v in next_emitted.items() if _is_action_key(k)}
        _write_json_atomic(emitted_state_path, {**emitted, **keep})
    except Exception as e:
        print(f"stale_monitor: action-ledger commit failed ({e})",
              file=sys.stderr)


# --- lane liveness cross-check --------------------------------------------
# A dead lane already ends its own Monitor task, which notifies the manager
# ONCE. The incident this whole change came from is that nobody noticed for
# hours — so one notification that can be missed is not enough, and an
# instruction to run `dockwright lanes` is exactly as strong as a manager
# remembering to run it.
#
# This scan is the unconditional hook: it already runs every 60s and already
# pages the manager, so it reports on its PEERS' heartbeats. Each lane is
# therefore watched by a process other than itself. The stale lane's own death
# is still covered the other way — it exits non-zero and its task exits.
#
# Deliberately silent on a lane with NO heartbeat at all: at boot the lanes are
# armed seconds after this scan could first run, and a manager that never arms
# lanes (codex) would be paged forever. "Never armed" is a question for
# `dockwright lanes`; this reports only the incident shape — a lane that WAS
# working and stopped.
#
# Mirrors dockwright.lane_io.LANES and HEARTBEAT_STALE_INTERVALS, which are
# canonical; this file cannot import the package. test_lane_liveness_mirror.py
# pins the two together so the copy cannot drift.
LANE_INTERVALS = {"questions": 2, "done": 2, "turn-ends": 5, "stale": 60}
LANE_HEARTBEAT_STALE_INTERVALS = 3
# First page is immediate; repeats double from here so a lane the manager has
# not re-armed keeps nagging without becoming the noise it is warning about.
LANE_SILENT_LADDER_BASE_SEC = 600
LANE_SILENT_LADDER_CAP_SEC = 4 * 3600


def _lane_heartbeat_path(manager_name: str, lane: str) -> Path:
    return ROOT / "lane-health" / _safe_bucket(manager_name) / f"{_safe_bucket(lane)}.json"


def _write_lane_heartbeat(manager_name: str, lane: str, now: float) -> None:
    """Mirror of lane_io.write_heartbeat for the standalone copy.

    Best-effort: a heartbeat that cannot be written must not take down a lane
    that is otherwise delivering fine. Carries `last_emit` forward across quiet
    scans — a lane with nothing to say has not stopped working.
    """
    try:
        path = _lane_heartbeat_path(manager_name, lane)
        prior = _load(path)
        prior = prior if isinstance(prior, dict) else {}
        last_emit = prior.get("last_emit")
        _write_json_atomic(path, {
            "lane": lane,
            "manager": manager_name,
            "pid": os.getpid(),
            "last_scan": now,
            "last_emit": last_emit if isinstance(last_emit, (int, float)) else None,
            "interval_hint": LANE_INTERVALS.get(lane, 0),
            "consecutive_errors": 0,
        })
    except Exception as e:
        print(f"stale_monitor: heartbeat write failed for {lane} ({e})",
              file=sys.stderr)


def _lane_silence_events(manager_name: str, emitted: dict, next_emitted: dict,
                         now: float) -> list[tuple[str, str]]:
    """(dedup_key, line) for every lane whose heartbeat has gone stale.

    Crash-proof: any failure yields no events. This is a safety net over a
    signal that already fired once; it must never be the thing that breaks the
    scan that carries the real alarms.
    """
    out = []
    try:
        # A host suspend freezes every lane at once and then hands this scan a
        # wall clock that jumped. Every heartbeat reads stale, so the check
        # pages about all four healthy lanes and tells the manager to kill
        # four healthy loops — a false alarm that is worse than no check,
        # because it is the one that teaches the reader to ignore the real
        # one. This scan's OWN cadence is the evidence: if more time passed
        # since the previous scan than any lane's window allows, the machine
        # was asleep, not the lanes. Re-baseline and stay quiet for one cycle.
        prior_tick = emitted.get("lane_check_tick")
        next_emitted["lane_check_tick"] = now
        if isinstance(prior_tick, (int, float)):
            gap = now - prior_tick
            # Baseline is the OBSERVER's own cadence — this scan runs on the
            # stale lane's interval, so a gap far past it means this process
            # was frozen and every heartbeat it is about to read is stale for a
            # reason that is not the lanes' fault. (min() across lanes is NOT
            # the baseline: 6s is smaller than the observer's own 60s period,
            # so it would call every normal scan a suspend.)
            #
            # ⚠️ Naming `stale` rather than max() is a behavioural NO-OP today
            # — `stale` IS the max — and must not be recorded as the fix for
            # the false-page band. It is kept because the two diverge the
            # moment anyone adds a lane slower than 60s, and this spelling is
            # the one that matches the stated intent. The band itself (a 100s
            # freeze clears 180s but is far past the 6s window of the 2s lanes)
            # is closed entirely by the two-observation rule below.
            if gap > LANE_INTERVALS["stale"] * LANE_HEARTBEAT_STALE_INTERVALS:
                for lane in LANE_INTERVALS:
                    carried = emitted.get(f"lane_silent:{lane}")
                    if carried is not None:
                        next_emitted[f"lane_silent:{lane}"] = carried
                return []

        for lane, interval in LANE_INTERVALS.items():
            # Never report the lane doing the reporting. Its heartbeat is
            # written at the END of this scan, so it is always one cadence old
            # here — and a page whose own delivery disproves it is noise.
            if lane == "stale":
                continue
            record = _load(_lane_heartbeat_path(manager_name, lane))
            last_scan = record.get("last_scan") if isinstance(record, dict) else None
            if not isinstance(last_scan, (int, float)) or last_scan <= 0:
                continue                       # never armed — not our alarm
            silent = now - last_scan
            if silent <= interval * LANE_HEARTBEAT_STALE_INTERVALS:
                # Recovery resets both the rung and the confirmation marker by
                # OMISSION: next_emitted is built
                # fresh each scan and replaces emitted wholesale, so simply not
                # carrying the key forward is the reset. An explicit pop() here
                # looked like the reset and was dead code — a mutation sweep
                # caught it by removing it and nothing went red.
                continue
            # TWO consecutive stale observations before the first page. One
            # freeze of ANY length makes a single round read stale; a lane that
            # is genuinely dead still reads stale on the next round, observed
            # at a normal cadence. Costs one cycle of latency on a real death
            # and removes every freeze-induced false page, including the band
            # the gap check above cannot see.
            confirm_key = f"lane_stale_seen:{lane}"
            key = f"lane_silent:{lane}"
            prior = emitted.get(key)
            if not isinstance(prior, dict) and not emitted.get(confirm_key):
                next_emitted[confirm_key] = now
                continue
            next_emitted[confirm_key] = emitted.get(confirm_key) or now
            prior = prior if isinstance(prior, dict) else {}
            last_paged = prior.get("at")
            level = prior.get("level")
            level = level if isinstance(level, int) and level > 0 else 0
            if isinstance(last_paged, (int, float)):
                rung = min(LANE_SILENT_LADDER_BASE_SEC * (2 ** min(level - 1, 16)),
                           LANE_SILENT_LADDER_CAP_SEC) if level else 0
                if now - last_paged < rung:
                    next_emitted[key] = prior      # hold, keep the rung
                    continue
            next_emitted[key] = {"at": now, "level": level + 1}
            out.append((key,
                        f"LANE_SILENT {lane} — no scan for {int(silent // 60)}min "
                        f"(expected every {interval}s). Events are NOT reaching you "
                        f"on that lane. Re-arm it and kill the old loop process."))
    except Exception as e:
        print(f"stale_monitor: lane liveness check failed ({e})", file=sys.stderr)
    return out
# --- end lane liveness cross-check ----------------------------------------
# --- end lane delivery discipline -----------------------------------------


def _outbox_write(manager_name: str, kind: str, line: str, now: float, seq: int) -> None:
    """Divert one informational line. ANY failure falls back to printing —
    today's dedicated-wake behavior is the floor; a swallowed write would be
    a true event loss (spec I9)."""
    try:
        target = _outbox_dir(manager_name) / f"{int(now * 1000)}-{os.getpid()}-{seq}.json"
        _write_json_atomic(target, {"line": line, "kind": kind, "buffered_at": now})
    except Exception as e:
        # FLUSHED, not printed: this fallback exists because losing the line is
        # a true event loss (an AUTOCLOSED whose window and active record are
        # already gone has no cursor and no replay). A bare print here would
        # sit in the buffer while main() went on to commit the ladder and stamp
        # a heartbeat — the exact shape this change removes everywhere else.
        # LaneDead propagates: main never reaches its emitted-state write, so
        # the rung stays un-burnt and the line re-fires on a live lane.
        _emit(line)
        print(f"stale_monitor: outbox write failed ({e}); printed instead",
              file=sys.stderr)


def _drain_outbox(manager_name: str) -> None:
    """Emit-then-unlink; same at-least-once discipline and per-entry failure
    policy as monitor._drain_notify_outbox (FileNotFoundError = a concurrent
    drainer won the race; undecodable = unlink so it can't block the rest).

    The emit flushes before the unlink, so a dead reader leaves the entry on
    disk instead of destroying it — LaneDead is re-raised past the blanket
    except rather than swallowed."""
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
                _emit(line)
            p.unlink(missing_ok=True)
    except LaneDead:
        raise
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

    Prefer awake-clock delta (CLOCK_UPTIME_RAW on macOS, CLOCK_MONOTONIC on
    Linux — both pause during sleep/suspend), so an 8h sleep doesn't burn the
    worker's 2h idle grace. Wall-clock keeps ticking through sleep and would
    falsely reap a freshly-idled worker.

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


def _autoclose_idle_worker(record_path: Path, record: dict,
                           elapsed_sec: int) -> str | None:
    """Archive an idle worker's record and close its window. None = REFUSED.

    ⛔ THE BUSY-SHELL GUARD LIVES HERE, not at the call site, and that placement
    is the whole point. The property is "no lane may close a worker that still
    has live background work", and it holds at the closer — so a lane added
    later inherits it BY CONSTRUCTION instead of having to remember it.

    It sat at the call site for four review rounds. The AST test that tried to
    keep it honest there was a classifier over syntax and was defeated three
    times by ordinary refactors — an alias binding, then a one-line wrapper,
    then `globals()[...]` with a concatenated name — each shipping a second
    unguarded lane with the whole suite green. Worse, it went RED on a wrapper
    that ENFORCED the check and GREEN on one that did not: it punished the
    correct shape and rewarded the hole. Deleted, not patched a fourth time.

    ⛔ THE CLOSER RESOLVES THE INDEX ITSELF. There is deliberately no parameter
    for passing one in, and that absence is the guard. Six review rounds were
    spent defending against a malformed caller index — `0`, a truthy
    non-mapping, `{}`, then three ordinary mistakes (forgot `child_commands`,
    forgot `command_by_pid`, keyed by ppid), each of which closed a worker
    holding a live test suite. Every fix bounded the VALUE and was beaten by
    the next shape, because the ambiguity is not in the value: five of
    `_has_live_background_shell`'s six False exits mean "cannot tell" and read
    identically to "not busy" from outside.

    Deleting the parameter deletes that entire class. A new lane cannot pass a
    wrong index because it cannot pass one at all, and there is no trust
    question left between the producer and the consumer.

    The parameter bought ONE thing: a single `ps` per scan instead of one per
    candidate. That optimisation is what cost seven review rounds, so it is
    gone. A `ps` is ~45ms, the autoclose gate is hourly, and the candidate set
    is a handful of idle workers.
    """
    if elapsed_sec <= _busy_shell_deadline():
        # Cap first: past the deadline a worker closes regardless, and pays for
        # no snapshot. A live shell EXTENDS the deadline; it never vetoes.
        #
        # A broken `ps` HOLDS rather than closes, bounded by that same cap: the
        # worst case is autoclose delayed from the TTL to the deadline, never a
        # worker that cannot be closed. The reverse default is what let every
        # unenumerated shape above reach a close.
        try:
            index = _process_index()
            busy = not index or _has_live_background_shell(record, index)
        except Exception:
            busy = True
        if busy:
            return None
    sid = record.get("claude_sid")
    name = record.get("name") or ""
    window_id = record.get("window_id") or record.get("iterm_sid") or ""
    transcript_path = record.get("transcript_path")
    if not transcript_path:
        # Fallback resolve: a worker autoclosed before its first Stop never had
        # the path cached, and autoclose unlinks active/ before the window
        # close, so session_end's own fallback is skipped for this lane.
        # Best-effort like the hooks.py sibling — an OSError mid-scan must not
        # abort the autoclose and strand the record in active/.
        try:
            resolved = _resolve_transcript_path(record)
        except Exception:
            resolved = None
        transcript_path = str(resolved) if resolved else None
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
        "transcript_path": transcript_path,
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


def _scan_orphan_windows(now: int, emitted: dict, next_emitted: dict, emit) -> None:
    """Page ORPHAN_WINDOW for workers-session panes with no backing record.

    Protection is FLEET-GLOBAL regardless of --manager scoping (orphan-ness is
    a fleet property): every active record's window id, closed records with a
    pending question (sweep's invariant), and spawn-in-flight .window sidecars.
    Fail-safe: one non-nested worker record with an EMPTY window id makes pane
    attribution unreliable — skip the whole scan (stderr note) rather than
    false-page; nested records carry window_id="" by design and don't count.
    Crash-proof: driver absent or unanswerable → no scan, never a raise."""
    if _get_driver is None:
        return
    try:
        os_windows = _get_driver().ls()
    except Exception:
        return
    if os_windows is None:
        return
    candidates: dict = {}
    for osw in os_windows:
        if not isinstance(osw, dict) or osw.get("wm_class") != WORKERS_SESSION_NAME:
            continue
        tabs = osw.get("tabs")
        if not isinstance(tabs, list):
            continue
        for tab in tabs:
            if not isinstance(tab, dict):
                continue
            windows = tab.get("windows")
            if not isinstance(windows, list):
                continue
            for win in windows:
                if isinstance(win, dict) and win.get("id") is not None:
                    candidates[str(win["id"])] = str(tab.get("title") or "?")
    if not candidates:
        return
    protected: set = set()
    if ACTIVE.is_dir():
        for p in ACTIVE.iterdir():
            if p.suffix != ".json":
                continue
            record = _load(p)
            if record is None:
                continue
            wid = record.get("window_id") or record.get("iterm_sid") or ""
            if wid:
                protected.add(str(wid))
            elif record.get("agent") == "worker" and not record.get("nested"):
                print(f"stale_monitor: orphan scan skipped (worker "
                      f"{record.get('name')} has no window id — pane "
                      f"attribution unreliable)", file=sys.stderr)
                return
    if CLOSED.is_dir():
        pending_sids = _pending_question_sids()
        for p in CLOSED.iterdir():
            if p.suffix != ".json":
                continue
            record = _load(p)
            if record is None or record.get("claude_sid") not in pending_sids:
                continue
            wid = record.get("window_id") or record.get("iterm_sid") or ""
            if wid:
                protected.add(str(wid))
    if ASSIGNMENTS_PENDING.is_dir():
        for sidecar in ASSIGNMENTS_PENDING.glob("*.window"):
            try:
                wid = sidecar.read_text().strip()
            except OSError:
                continue
            if wid:
                protected.add(wid)
    if GARDENER_LIVE_WINDOWS.is_dir():
        cutoff = now - GARDENER_WINDOW_PROTECT_TTL_SEC
        for sidecar in GARDENER_LIVE_WINDOWS.glob("*.window"):
            try:
                if sidecar.stat().st_mtime < cutoff:
                    continue
                wid = sidecar.read_text().strip()
            except OSError:
                continue
            if wid:
                protected.add(wid)
    base_min = max(1, ORPHAN_GRACE_SEC // 60)
    for pane_id, title in candidates.items():
        if pane_id in protected:
            continue          # key (if any) not carried forward → clock resets
        key = f"orphan:{pane_id}"
        prev = emitted.get(key)
        prev = prev if isinstance(prev, dict) else {}
        first_seen = prev.get("first_seen")
        if not isinstance(first_seen, (int, float)):
            first_seen = now
        paged = prev.get("paged")
        paged = paged if isinstance(paged, int) else 0
        entry = {"first_seen": first_seen, "paged": paged}
        elapsed = int(now - first_seen)
        if elapsed >= ORPHAN_GRACE_SEC:
            threshold = _highest_threshold(max(elapsed // 60, base_min), base_min)
            if threshold is not None and threshold > paged:
                entry["paged"] = threshold
                emit("orphan-window", pane_id,
                     f"ORPHAN_WINDOW {pane_id} tab={title!r} ({elapsed // 60}min) "
                     f"— no backing active record",
                     key)
        next_emitted[key] = entry


def _approval_dialog_block(pane_text: str) -> str | None:
    """The pane tail iff it currently shows a permission dialog, else None."""
    lines = pane_text.splitlines()[-APPROVAL_TAIL_LINES:]
    tail = "\n".join(ln.rstrip() for ln in lines)
    low = tail.lower()
    if not any(m in low for m in APPROVAL_QUESTION_MARKERS):
        return None
    if not any(m in low for m in APPROVAL_OPTION_MARKERS):
        return None
    return tail


def _approval_excerpt(block: str) -> str:
    """The dialog's gist: the question-marker line plus up to two non-empty
    lines above it (usually the gated command), flattened and truncated."""
    lines = block.splitlines()
    idx = None
    for i, ln in enumerate(lines):
        if any(m in ln.lower() for m in APPROVAL_QUESTION_MARKERS):
            idx = i
            break
    if idx is None:
        return block[-APPROVAL_EXCERPT_MAX:]
    context = [ln.strip(" │╭╮╰╯─") for ln in lines[max(0, idx - 2):idx + 1]]
    excerpt = " · ".join(part.strip() for part in context if part.strip())
    if len(excerpt) > APPROVAL_EXCERPT_MAX:
        excerpt = excerpt[:APPROVAL_EXCERPT_MAX - 1] + "…"
    return excerpt


def _scan_approval_prompts(manager_name, now, emitted, next_emitted, emit) -> None:
    """Page APPROVAL_PROMPT for own workers' panes sitting on a permission
    dialog. Two populations: registered processing claude workers (mid-task
    prompts), and spawn-in-flight .pending/*.window sidecar panes (the trust /
    MCP-approval dialogs fire BEFORE SessionStart registers the worker, and the
    sidecar shields those panes from the orphan alarm — without this leg a
    boot-block is invisible). First sighting pages immediately; the SAME dialog
    re-pages on the nudge-threshold ladder (5/10/20min, then hourly); a
    DIFFERENT dialog is a new key and pages immediately; a cleared dialog's key
    is simply not carried forward. Crash-proof: any capture/classify failure
    reads as no-event."""
    if _get_driver is None:
        return
    targets: list[tuple[str, str, str]] = []   # (dedup_id, display_name, window_id)
    if ACTIVE.is_dir():
        for p in ACTIVE.iterdir():
            if p.suffix != ".json":
                continue
            record = _load(p)
            if record is None or record.get("agent") != "worker":
                continue
            if not _matches_manager(record, manager_name):
                continue
            if (record.get("runtime") or "claude") != "claude":
                continue
            if record.get("state") != "processing" or record.get("nested"):
                continue
            wid = record.get("window_id") or record.get("iterm_sid") or ""
            if not wid:
                continue
            sid = record.get("claude_sid") or p.stem
            targets.append((sid, record.get("name") or sid, str(wid)))
    if ASSIGNMENTS_PENDING.is_dir():
        for sidecar in ASSIGNMENTS_PENDING.glob("*.window"):
            try:
                wid = sidecar.read_text().strip()
            except OSError:
                continue
            if not wid:
                continue
            pending = _load(sidecar.with_suffix(".json")) or {}
            if manager_name is not None and pending.get("parent_manager_name") != manager_name:
                continue
            targets.append((sidecar.stem, pending.get("name") or sidecar.stem, wid))
    if not targets:
        return
    try:
        driver = _get_driver()
    except Exception:
        return
    for dedup_id, display, wid in targets:
        try:
            pane_text = driver.capture_screen(wid)
        except Exception:
            continue
        if not pane_text:
            continue
        block = _approval_dialog_block(pane_text)
        if block is None:
            continue   # cleared/no dialog: keys not carried forward reset naturally
        digest = hashlib.sha1(block.encode("utf-8", "replace")).hexdigest()[:12]
        key = f"approval:{dedup_id}:{digest}"
        prev = emitted.get(key)
        prev = prev if isinstance(prev, dict) else {}
        first_seen = prev.get("first_seen")
        if not isinstance(first_seen, (int, float)):
            first_seen = now
        paged = prev.get("paged")
        paged = paged if isinstance(paged, int) else 0
        entry = {"first_seen": first_seen, "paged": paged}
        if paged == 0:
            entry["paged"] = 1   # sentinel: immediate first page burned
            emit("approval", display,
                 f"APPROVAL_PROMPT {display}: {_approval_excerpt(block)}", key)
        else:
            elapsed_min = int(now - first_seen) // 60
            threshold = _highest_nudge_threshold(elapsed_min, APPROVAL_REPAGE_BASE_MIN)
            if threshold is not None and threshold > paged:
                entry["paged"] = threshold
                emit("approval", display,
                     f"APPROVAL_PROMPT {display} (still waiting, {elapsed_min}min): "
                     f"{_approval_excerpt(block)}", key)
        next_emitted[key] = entry


def main(manager_name: str | None = None) -> int:
    # Before ANY side effect (nudges, autoclose, account flips) and before the
    # emitted-state cursor is written: if the manager can no longer hear this
    # lane there is nothing to page and the scan must not burn its rungs.
    _lane_preflight()
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
    current_uptime = _awake_seconds()
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
                            _record_action_ahead(
                                emitted_state_path, emitted, next_emitted,
                                mgr_sched_key, {"at": now, "baseline": mgr_activity,
                                                "fired": True})
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
                    # monitor launches a takeover (a fresh process re-reads the
                    # keychain login). The SAME-account bet is taken only on
                    # the episode's first distinct 401 (see
                    # AUTH_401_SAME_ACCOUNT_ATTEMPTS); afterwards both branches
                    # below share one invariant via _healthy_takeover_target —
                    # a suspect account is never SELECTED as the takeover
                    # target (the pre-existing _account_config_prefix
                    # farm-health fallback can still land the spawn on the
                    # default login — see its KNOWN FAILURE MODE docstring) —
                    # and a recover with no healthy target is PROMOTED to escalate
                    # rather than left waiting for attempt 3, which may never
                    # come (a deaf manager re-presents the SAME uuid every
                    # scan; the duplicate path freezes the window). On escalate
                    # we STOP launching dead takeover tabs and flip + page;
                    # each takeover is a fresh sid with its own guard key, so
                    # the takeover count stays bounded.
                    manager_limited = True
                    account = _account_of(mgr_record, pool)
                    auth_uuid, _auth_text = auth_fail
                    auth_key = f"auth-recovery:{mgr_sid}"
                    if auth_key in emitted:
                        # Persist the once-per-sid launch guard while the manager
                        # stays bricked; it drops naturally once the record is
                        # taken over or the 401 clears (guard teardown).
                        next_emitted[auth_key] = emitted[auth_key]
                    decision, attempts = _record_auth_401(account, auth_uuid, now)
                    # Computed AFTER _record_auth_401, not before: at attempt
                    # 1 with pool == account, this now reads the manager's
                    # OWN just-recorded episode as active, making the
                    # `pool == account` disjunct below load-bearing (dead
                    # code otherwise — computed pre-record, pool_suspect for
                    # the suspect's own account could never be True at
                    # attempt 1). For pool != account, recording `account`'s
                    # 401 never touches pool's own state entry, so this
                    # yields the identical value pre-record would have —
                    # the healthy-pointer/escalate cells are unaffected.
                    pool_suspect = _auth_401_active(pool, now)
                    healthy_target = _healthy_takeover_target(
                        account, pool, pool_suspect=pool_suspect)
                    if (attempts <= AUTH_401_SAME_ACCOUNT_ATTEMPTS
                            and (pool == account or not pool_suspect)):
                        # The transient-blip bet: first distinct 401, onto the
                        # pointer — which may BE the suspect (pool == account;
                        # pool_suspect now reads True by construction the
                        # moment _record_auth_401 above stamps this account's
                        # own episode — the `pool == account` disjunct is
                        # what keeps the bet alive against that). A FOREIGN
                        # pointer that is itself mid-401 is not a bet, it is
                        # the zombie factory one account over (Tier-2
                        # residual 1, measured reachable) — falls through.
                        recover_target = pool
                    else:
                        recover_target = healthy_target
                    if decision == "recover" and recover_target is None:
                        # A recover decision with no launchable target IS the
                        # credential-suspect determination — escalate now
                        # (flip attempt + page + notification), never wait
                        # for an attempt 3 a deaf manager may not produce.
                        decision = "escalate"
                    if decision == "recover":
                        _append_account_ledger({
                            "ts": now, "event": "auth-401", "account": account,
                            "action": "recover", "source": f"manager:{manager_name}",
                            "from_sid": mgr_sid, "by": "stale_monitor"})
                        target = recover_target
                        launched = False
                        if (auth_key not in emitted
                                and _keychain_unlocked()
                                and _ledger_recovery_launches(mgr_sid, now) == 0):
                            wid = _launch_recovery_manager(mgr_record, mgr_sid, target)
                            _append_account_ledger({
                                "ts": now, "event": "recovery-launch",
                                "manager": manager_name, "from_sid": mgr_sid,
                                "window_id": wid, "by": "stale_monitor"})
                            next_emitted[auth_key] = now
                            launched = True
                        if (not launched and auth_key not in emitted
                                and _ledger_recovery_launches(mgr_sid, now) == 0):
                            # Outcome-derived, not cause-derived (Tier-2 B-2 +
                            # drift-guard ADD-ONE): a wanted launch that did
                            # not happen, with no successor in flight, must
                            # reach the human — silence here is the 6h16m
                            # mode. The launch itself resumes only on the
                            # next fresh 401 or human action.
                            _notify_macos(
                                f"AUTH_401 {account}: recovery launch blocked "
                                f"(keychain locked) — run "
                                f"{_login_fix_command(account)}, then /login "
                                f"(manager {manager_name})")
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
                             f"login suspect after repeated 401s; PAGE: run "
                             f"{_login_fix_command(account)}, then /login")
                        # Healthy-target-only launch — the invariant lives in
                        # _healthy_takeover_target (shared with the recover
                        # branch above). No healthy target (flip blocked,
                        # pointer still on the suspect account) ⇒ page only —
                        # the human must /login. Guarded once per sid like the
                        # recover launch.
                        target = _healthy_takeover_target(account, pool, new_letter,
                                                          pool_suspect=pool_suspect)
                        # Read ONCE and reused below (launch gate + reason):
                        # a second call could observe a mid-scan unlock,
                        # making the gate and the notify text disagree about
                        # which cause fired — and it's a real subprocess
                        # call, not free to repeat.
                        keychain_ok = _keychain_unlocked()
                        launched = False
                        if (target is not None
                                and auth_key not in emitted
                                and keychain_ok
                                and _ledger_recovery_launches(mgr_sid, now) == 0):
                            wid = _launch_recovery_manager(mgr_record, mgr_sid, target)
                            _append_account_ledger({
                                "ts": now, "event": "recovery-launch",
                                "manager": manager_name, "from_sid": mgr_sid,
                                "window_id": wid, "by": "stale_monitor"})
                            next_emitted[auth_key] = now
                            launched = True
                        if (not launched and auth_key not in emitted
                                and _ledger_recovery_launches(mgr_sid, now) == 0):
                            # No successor will ever exist to replay the
                            # buffered auth-escalate page (nothing launched ⇒
                            # nothing takes the record over ⇒ manager_limited
                            # holds the buffer forever) — the OS notification
                            # is the only channel that still reaches the
                            # human. Outcome-derived, not cause-derived
                            # (Tier-2 B-2 + drift-guard ADD-ONE): ANY wanted
                            # launch that didn't happen notifies, not just
                            # the no-healthy-target cause the author first
                            # hand-picked (the keychain-locked cause was the
                            # proof this needed to be outcome-derived).
                            # A foreign mid-401 pointer and a locked keychain
                            # are independent causes — when both hold, name
                            # both; naming only one would hide the other
                            # from the human.
                            reasons = []
                            if target is None:
                                reasons.append("no healthy account")
                            if not keychain_ok:
                                reasons.append("keychain locked")
                            reason = " + ".join(reasons) or "launch guard"
                            _notify_macos(
                                f"AUTH_401_ESCALATED {account}: {reason} — run "
                                f"{_login_fix_command(account)}, then /login "
                                f"(manager {manager_name})")
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
                        _record_action_ahead(emitted_state_path, emitted,
                                             next_emitted, nudge_sent_key, now)
                        _send_text(window_id, NUDGE_TEXT)
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
                            decision, _ = _record_auth_401(account, auth_uuid, now)
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
                                     f"login suspect after repeated 401s; PAGE: run "
                                     f"{_login_fix_command(account)}, then /login")
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
                                    _record_action_ahead(emitted_state_path, emitted,
                                                         next_emitted, nudge_sent_key, now)
                                    _send_text(window_id, NUDGE_TEXT)
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
                        _record_action_ahead(emitted_state_path, emitted,
                                             next_emitted, stretch_nudge_key, now)
                        _record_action_ahead(emitted_state_path, emitted,
                                             next_emitted, nudge_sent_key, now)
                        _send_text(window_id, NUDGE_TEXT)
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
                # The busy-shell guard is INSIDE _autoclose_idle_worker, which
                # resolves its own process index — so a lane added here or
                # anywhere else inherits it and has no index to get wrong.
                # None means it refused: the worker still has live background
                # work and is under the deadline.
                line = _autoclose_idle_worker(p, record, elapsed)
                if line is None:
                    continue
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
    _scan_orphan_windows(now, emitted, next_emitted, emit)
    _scan_approval_prompts(manager_name, now, emitted, next_emitted, emit)
    # Peer-lane liveness. Scoped runs only: a global scan has no manager whose
    # lanes these would be. Emitted as a normal urgent line so it rides the
    # existing limited-buffer coalescing rather than inventing a second path.
    if manager_name:
        for lane_key, lane_line in _lane_silence_events(
                manager_name, emitted, next_emitted, now):
            emit("lane_silent", lane_key, lane_line, lane_key)
    pruned_cache = {s: p for s, p in codex_log_cache.items() if s in seen_codex_sids}
    if pruned_cache:
        next_emitted["codex_log_cache"] = pruned_cache
    # ---- flush: coalesce while the owning manager is limited -----------------
    # LaneDead from any _emit below unwinds past the state write, so every
    # key would be lost — including the record that a nudge was already
    # TYPED into a pane. Committing the ACTION half here is what stops the
    # next scan repeating the act; the PAGE half is correctly discarded.
    try:
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
                    # next scan instead of waiting for the next doubling. Dict-valued
                    # ladders (orphan:<pane>) keep their own first_seen clock inside
                    # that same entry — popping the whole dict would erase it and
                    # restart the grace window from scratch, so only their "paged"
                    # rung resets in place; int-valued STALE_* keys still pop outright.
                    suppressed = (buffer or {}).get("suppressed_keys")
                    if isinstance(suppressed, list):
                        for suppressed_key in suppressed:
                            if not isinstance(suppressed_key, str):
                                continue
                            entry = next_emitted.get(suppressed_key)
                            if isinstance(entry, dict) and "paged" in entry:
                                entry["paged"] = 0
                            else:
                                next_emitted.pop(suppressed_key, None)
                    # Emit BEFORE clearing the flag: the flag is the only record
                    # that these lines are still owed. Unlinking first meant a dead
                    # reader destroyed the whole limited-window rollup.
                    _emit(rollup)
                    flag_path.unlink(missing_ok=True)
                    printed_any = True
                    # Replay any buffered auth-401 /login pages — the rollup itself
                    # only summarizes counts; the page text must reach the human.
                    for page in (buffer or {}).get("auth_pages") or []:
                        if isinstance(page, str):
                            _emit(page)
                # Urgent kinds print live; informational kinds (OUTBOX_DIVERT_KINDS)
                # ride a wake that is already happening — printed alongside other
                # lines, or buffered to notify-outbox/<mgr>/ for monitor.py's
                # scans to piggyback, with the timeout flush as the latency bound.
                direct = [e for e in events if e[0] not in OUTBOX_DIVERT_KINDS]
                diverted = [e for e in events if e[0] in OUTBOX_DIVERT_KINDS]
                for _kind, _event_name, line, _dedup_key in direct:
                    _emit(line)
                    printed_any = True
                # Divert kinds go to disk FIRST in both branches. Emitting one
                # straight to stdout looked like a shortcut when a wake was already
                # happening, but it left the line with NO durable copy: a reader
                # that died mid-burst destroyed an AUTOCLOSED whose worker was
                # already archived and whose pane was already closed — no cursor,
                # no replay, exactly the loss this change exists to stop. The drain
                # flushes before it unlinks, so the branches now differ only in
                # WHEN they drain.
                for seq, (kind, _event_name, line, _dedup_key) in enumerate(diverted):
                    _outbox_write(manager_name, kind, line, now, seq)
                if printed_any:
                    _drain_outbox(manager_name)
                else:
                    oldest = _outbox_oldest_ts(manager_name)
                    if oldest is not None and now - oldest >= OUTBOX_MAX_HOLD_SEC:
                        _drain_outbox(manager_name)
        else:
            for _kind, _event_name, line, _dedup_key in events:
                _emit(line)
    except LaneDead:
        _commit_actions_only(emitted_state_path, emitted, next_emitted)
        raise
    cursor_written = True
    try:
        _write_json_atomic(emitted_state_path, next_emitted)
    except Exception as e:
        cursor_written = False
        print(f"stale_monitor: failed to write {emitted_state_path} ({e})", file=sys.stderr)
    # The heartbeat is written HERE, by the process that actually emitted, and
    # only after every _emit above flushed. Written by the PARENT it would have
    # certified this process's exit CODE rather than delivery — and `stale` has
    # no backlog arm to cross-check that proxy against. Unreachable from a scan
    # that raised LaneDead, which is the whole point. Scoped runs only: a global
    # scan has no manager whose lane this would be.
    # No heartbeat when the cursor write failed. The heartbeat means "this
    # lane is delivering"; a scan that could not persist its own dedup state is
    # about to re-page everything it just paged, and `dockwright lanes` reading
    # OK through that is the report claiming health while broken.
    if manager_name and cursor_written:
        _write_lane_heartbeat(manager_name, "stale", now)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="One-shot scan for stale dockwright state.")
    parser.add_argument(
        "--manager",
        default=None,
        help="Scope the scan to this manager's workers. "
             "Omit for global (all managers') behavior.",
    )
    args = parser.parse_args()
    try:
        sys.exit(main(manager_name=args.manager))
    except LaneDead as exc:
        print(f"stale_monitor: lane is dead ({exc}); ending the lane so its "
              f"Monitor task exits and the manager is told.", file=sys.stderr)
        _detach_stdout()
        sys.exit(EXIT_LANE_DEAD)
