from pathlib import Path
import os
import re

from . import config

ROOT = config.state_root()
ACTIVE = ROOT / "active"
QUESTIONS = ROOT / "questions"
ANSWERS = ROOT / "answers"
DONE = ROOT / "done"
CLOSED = ROOT / "closed"
HANDOFFS = ROOT / "handoffs"
TURN_ENDS = ROOT / "turn-ends"
PRESETS = ROOT / "presets"
SLOTS = ROOT / "slots"
MANAGER_TRIGGERS_LOG = ROOT / "manager-triggers.jsonl"
# Durable spend archive: one JSONL line per finished spend period (see
# spend_ledger.py). Survives the closed/ 7-day prune — the spend-report's
# history horizon. A file, not a dir — never in ensure_dirs.
SPEND_LEDGER = ROOT / "spend-ledger.jsonl"

# Account auto-switch pointer: one letter (a|b) selecting which account new
# spawns BILL — 'a' = the default ~/.claude login, 'b' = the ~/.claude-b login.
# Missing/invalid file = feature fully off (no account selection, the
# stale_monitor flip lane is inert). A file, not a dir — never in ensure_dirs.
ACCOUNT_ACTIVE = ROOT / "account-active"

# The tmux config the dedicated `tmux -L dockwright` server loads at birth
# (via `tmux -L dockwright -f <this>`), deployed here by setup.sh from
# deploy/tmux/dockwright.conf. Read by terminal._resolve_conf() (existence-
# gated, TMUX_CONF_LEGACY fallback) and by bootstrap-recreate.sh /
# gardener-run.sh. A file, not a dir.
TMUX_CONF = ROOT / "dockwright.tmux.conf"

# Pre-rename conf name (identity rename orchestrator->dockwright): read as a
# fallback by terminal._resolve_conf() and the shell birth lanes when the new
# name isn't deployed yet (setup.sh not re-run since the rename). Deprecated,
# one release — retire together with CLAUDE_ORCH_TMUX_SOCKET. A file, not a
# dir — never in ensure_dirs.
TMUX_CONF_LEGACY = ROOT / "claude-orch.tmux.conf"

# Per-account brick state written by stale_monitor when a worker/manager hits a
# rate-limit banner. Spawner reads this to skip bricked accounts at spawn time
# (proactive round-robin design). A file, not a dir — never in ensure_dirs.
ACCOUNT_STATE = ROOT / "account-state.json"

# Atomic spawn counter for proactive weighted round-robin across accounts.
# One JSON object {"counter": N}; fcntl-locked for concurrent-spawn safety.
# A file, not a dir — never in ensure_dirs.
SPAWN_COUNTER = ROOT / "spawn-counter.json"

# Per-account usage cache written by statusline-command.sh on each render (the
# statusline-tap). One JSON object per account:
#   {five_hour_pct, seven_day_pct, five_hour_resets_at, seven_day_resets_at, ts}
# The picker reads it to bias weights + apply the near-limit breaker. A dir, NOT in
# ensure_dirs — lazy, like ACCOUNT_STATE (the statusline mkdir -p's it on write).
ACCOUNT_USAGE = ROOT / "usage"

# Per-ticket artifact store (document durability) + per-worker assignments
# (ownership durability).
# .pending/ is dot-prefixed so assignments/*.json globs never see pre-claim files.
ARTIFACTS = ROOT / "artifacts"
ASSIGNMENTS = ROOT / "assignments"
ASSIGNMENTS_PENDING = ASSIGNMENTS / ".pending"
ARTIFACT_RETENTION_DAYS = 30
ASSIGNMENT_RETENTION_DAYS = 30
PENDING_ASSIGNMENT_TTL_SEC = 24 * 3600

# Orphan flags: orphans/<manager-name>.json, written by the session_end hook when
# a manager ends with live workers still parented to it (the Boot-lite event half).
# Deliberately NOT in ensure_dirs: the single writer (write_json_atomic) mkdirs on
# demand, readers (bootlite_watchdog.py, future manager-boot surfacing) tolerate a
# missing dir, and keeping it lazy spares every paths-patching test fixture from
# having to know about it. Resolution = unlink by the watchdog's healthy-sweep.
ORPHANS = ROOT / "orphans"

# Per-ticket architect workdir: ~/.claude/dockwright/architect/<ticket>/ holds
# blackboard.db + the rendered contract/slice/gate views. Sibling to active/, done/.
# The architect package (deterministic spine) is the only writer; nothing else
# imports it, so this dir stays empty until the architect CLI/manager is invoked.
ARCHITECT = ROOT / "architect"

# Distilled manager-session journals. Per-domain subdir under MANAGER_MEMORY:
#   manager-memory/<domain>/<YYYY-MM-DD>-<sid>.md
# Under the state root by default; kept as its own config key so operators can
# relocate it independently.
MANAGER_MEMORY = config.manager_memory_root()

# Per-account config-dir farm (worker account-billing fix). Each non-default
# account runs under its own per-config-dir keychain login. CONFIG_HOME is the
# canonical config dir and the symlink source for shared assets; HOST_CLAUDE_JSON
# is the seed for each per-account .claude.json; account_config_dir(letter) is the
# non-default dir the worker's `claude` runs under. Account 'a' uses the default
# ~/.claude (no farm); only non-default letters ('b') get a ~/.claude-<letter> farm.
CONFIG_HOME = config.claude_config_home()
HOST_CLAUDE_JSON = Path(os.environ.get("HOME", "")) / ".claude.json"


def account_config_dir(letter: str) -> Path:
    """Config-dir farm for a non-default account: an explicit registry
    config_dir wins, else the ~/.claude-<name> sibling convention."""
    override = config.account_config_dir_override(letter)
    if override is not None:
        return override
    return CONFIG_HOME.parent / f".claude-{letter}"


def account_usage_path(letter: str) -> Path:
    return ACCOUNT_USAGE / f"{letter}.json"


def worker_home() -> Path:
    """Default cwd for a freshly spawned worker when the caller passes no cwd.
    A generic 'worker home' carrying the operator's full worker MCP/permission
    profile, so a bare spawn_worker() is safe-by-default instead of inheriting
    the manager's cwd (manager MCP profile, blind to the operator data stack). Read
    at spawn time so CLAUDE_ORCH_WORKER_HOME and the caller's HOME both take
    effect live."""
    env = os.environ.get("CLAUDE_ORCH_WORKER_HOME", "").strip()
    if env:
        return Path(env)
    return config.worker_home_default()


def ensure_worker_home() -> Path:
    """worker_home(), created (mkdir -p) so a bare spawn_worker never falls back
    to the manager's cwd on a fresh install. Fail-open: a mkdir failure
    (permissions, or a parent that's a regular file) is swallowed — the caller's
    own is_dir() check then decides, preserving the pre-fix fallback."""
    home = worker_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return home


DEFAULT_DOMAIN = "general"

# Env vars the orchestrator stamps on (or reads from) its own manager/worker
# sessions — via the tmux spawn shell (spawner.py) and the hook commands
# (settings.snippet.json sets CLAUDE_PARENT_PID; hooks.py reads the rest).
# A headless `claude -p` child (the manager-memory distill) must inherit NONE
# of them: an inherited CLAUDE_AGENT=manager makes the child's SessionStart
# hook register it as a manager, and its SessionEnd hook then re-distills,
# spawning another `claude -p` with the same env — infinite fan-out.
ORCHESTRATOR_ENV_KEYS = (
    "CLAUDE_AGENT",
    "CLAUDE_WORKER_NAME",
    "CLAUDE_PARENT_MANAGER",
    "CLAUDE_WORKER_RUNTIME",
    "CLAUDE_PARENT_PID",
    "CLAUDE_DOMAIN",
    "CLAUDE_ITERM_SID",
    "CLAUDE_ASSIGNMENT_ID",
    "CLAUDE_ORCH_ACCOUNT",
)

# Sentinel set on the distill child's env. The hooks short-circuit on it, so a
# distill session can never register as a manager or trigger another distill —
# even if a future spawn path forgets to strip ORCHESTRATOR_ENV_KEYS.
DISTILL_ENV_SENTINEL = "CLAUDE_ORCHESTRATOR_DISTILL"

# Worker events (done/, turn-ends/, questions/) are scoped into a per-manager
# subdir keyed by the worker's parent_manager_name, so each manager's monitor
# watches only its own workers' events instead of every manager's. Null-parent
# done/turn-end events (legacy single-manager era) still WRITE to the shared
# UNSCOPED bucket. Null-parent questions keep the older flat
# questions/<question_id>.json layout so legacy list/answer/drop flows remain
# compatible. Recovery path is `_backfill_legacy_workers` on single-manager
# `become_manager` boot, which rewrites the records' parent_manager_name.
UNSCOPED_BUCKET = "_unscoped"


def manager_memory_domain_dir(domain: str) -> Path:
    return MANAGER_MEMORY / (domain or DEFAULT_DOMAIN)


def architect_dir_for(ticket: str) -> Path:
    """Per-ticket architect workdir. The ticket id is slugified to a single safe
    path segment (matches the store's own _safe slug)."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", ticket) or "_ticket"
    return ARCHITECT / safe


def _safe_segment(s: str) -> str:
    """Sanitize an arbitrary string to one path segment; raise on empty-after-sanitize.

    Deterministic, so artifact_get reconstructs the exact path artifact_put wrote.
    Stricter than _event_bucket / architect_dir_for: rejects instead of defaulting.
    """
    seg = re.sub(r"[^A-Za-z0-9_-]", "_", (s or "").strip())
    if seg in ("", ".", ".."):
        raise ValueError(f"invalid path segment: {s!r}")
    return seg


def artifact_ticket_dir(ticket: str) -> Path:
    return ARTIFACTS / _safe_segment(ticket)


def artifact_path(ticket: str, phase: str, name: str) -> Path:
    return artifact_ticket_dir(ticket) / f"{_safe_segment(phase)}.{_safe_segment(name)}.md"


def artifact_events_path(ticket: str) -> Path:
    return artifact_ticket_dir(ticket) / "events.jsonl"


def assignment_path(sid: str) -> Path:
    return ASSIGNMENTS / f"{_safe_segment(sid)}.json"


def pending_assignment_path(assignment_id: str) -> Path:
    return ASSIGNMENTS_PENDING / f"{_safe_segment(assignment_id)}.json"


def pending_window_path(assignment_id: str) -> Path:
    """Sidecar carrying the spawn-returned tmux pane id for a worker whose
    sid isn't born yet. Claimed/unlinked by the worker's SessionStart override;
    orphans swept by _prune_stale_assignments. `.window` (not `.json`) so the
    assignments/*.json globs and the os.replace claim never see it."""
    return ASSIGNMENTS_PENDING / f"{_safe_segment(assignment_id)}.window"


def _event_bucket(parent_manager_name: str | None) -> str:
    if not parent_manager_name:
        return UNSCOPED_BUCKET
    # A manager name must resolve to a single path segment.
    return parent_manager_name.replace("/", "_").replace("\\", "_")


def orphan_flag_path(manager_name: str | None) -> Path:
    return ORPHANS / f"{_event_bucket(manager_name)}.json"


def done_dir_for(parent_manager_name: str | None) -> Path:
    return DONE / _event_bucket(parent_manager_name)


def turn_ends_dir_for(parent_manager_name: str | None) -> Path:
    return TURN_ENDS / _event_bucket(parent_manager_name)


def question_dir_for(parent_manager_name: str | None) -> Path:
    if not parent_manager_name:
        return QUESTIONS
    return QUESTIONS / _event_bucket(parent_manager_name)


def notify_outbox_dir_for(parent_manager_name: str | None) -> Path:
    """Non-urgent notification outbox: stale_monitor diverts informational
    lines here; monitor.py scans drain them into a wake that is already
    happening. Resolved from ROOT at call time (the monitor._seen_file
    pattern) so tests that monkeypatch paths.ROOT are covered without a
    separate constant to patch. Bucket dirs are created on demand by the
    writer, mirroring done/<manager>/."""
    return ROOT / "notify-outbox" / _event_bucket(parent_manager_name)


def ensure_dirs() -> None:
    for d in (ACTIVE, QUESTIONS, ANSWERS, DONE, CLOSED, HANDOFFS, TURN_ENDS, PRESETS, SLOTS, MANAGER_MEMORY, ARCHITECT, ARTIFACTS, ASSIGNMENTS, ASSIGNMENTS_PENDING):
        d.mkdir(parents=True, exist_ok=True)
    # Always-present unscoped buckets so monitors and the first writer to a new
    # manager never race on a missing base dir.
    (DONE / UNSCOPED_BUCKET).mkdir(parents=True, exist_ok=True)
    (TURN_ENDS / UNSCOPED_BUCKET).mkdir(parents=True, exist_ok=True)
