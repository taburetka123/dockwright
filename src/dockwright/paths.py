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
SPEND_LEDGER = ROOT / "spend-ledger.jsonl"

ACCOUNT_ACTIVE = ROOT / "account-active"

TMUX_CONF = ROOT / "dockwright.tmux.conf"

TMUX_CONF_LEGACY = ROOT / "claude-orch.tmux.conf"

ACCOUNT_STATE = ROOT / "account-state.json"

ACCOUNT_REGISTRY = ROOT / "account-registry.json"

SPAWN_COUNTER = ROOT / "spawn-counter.json"

ACCOUNT_USAGE = ROOT / "usage"

ARTIFACTS = ROOT / "artifacts"
ASSIGNMENTS = ROOT / "assignments"
ASSIGNMENTS_PENDING = ASSIGNMENTS / ".pending"
ARTIFACT_RETENTION_DAYS = 30
ASSIGNMENT_RETENTION_DAYS = 30
PENDING_ASSIGNMENT_TTL_SEC = 24 * 3600

ORPHANS = ROOT / "orphans"

LANE_HEALTH = ROOT / "lane-health"

ARCHITECT = ROOT / "architect"

MANAGER_MEMORY = config.manager_memory_root()

CONFIG_HOME = config.claude_config_home()
HOST_CLAUDE_JSON = Path(os.environ.get("HOME", "")) / ".claude.json"


def account_config_dir(letter: str) -> Path:
    override = config.account_config_dir_override(letter)
    if override is not None:
        return override
    return CONFIG_HOME.parent / f".claude-{letter}"


def account_usage_path(letter: str) -> Path:
    return ACCOUNT_USAGE / f"{letter}.json"


def worker_home() -> Path:
    env = os.environ.get("CLAUDE_ORCH_WORKER_HOME", "").strip()
    if env:
        return Path(env)
    return config.worker_home_default()


def ensure_worker_home() -> Path:
    home = worker_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return home


DEFAULT_DOMAIN = "general"

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

DISTILL_ENV_SENTINEL = "CLAUDE_ORCHESTRATOR_DISTILL"

UNSCOPED_BUCKET = "_unscoped"


def manager_memory_domain_dir(domain: str) -> Path:
    return MANAGER_MEMORY / (domain or DEFAULT_DOMAIN)


def architect_dir_for(ticket: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", ticket) or "_ticket"
    return ARCHITECT / safe


def _safe_segment(s: str) -> str:
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
    return ASSIGNMENTS_PENDING / f"{_safe_segment(assignment_id)}.window"


def _event_bucket(parent_manager_name: str | None) -> str:
    if not parent_manager_name:
        return UNSCOPED_BUCKET
    bucket = parent_manager_name.replace("/", "_").replace("\\", "_")
    return f"_{bucket}" if bucket in (".", "..") else bucket


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
    return ROOT / "notify-outbox" / _event_bucket(parent_manager_name)


def ensure_dirs() -> None:
    for d in (ACTIVE, QUESTIONS, ANSWERS, DONE, CLOSED, HANDOFFS, TURN_ENDS, PRESETS, SLOTS, MANAGER_MEMORY, ARCHITECT, ARTIFACTS, ASSIGNMENTS, ASSIGNMENTS_PENDING):
        d.mkdir(parents=True, exist_ok=True)
    (DONE / UNSCOPED_BUCKET).mkdir(parents=True, exist_ok=True)
    (TURN_ENDS / UNSCOPED_BUCKET).mkdir(parents=True, exist_ok=True)
