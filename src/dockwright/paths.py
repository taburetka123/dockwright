from pathlib import Path
import os
import re
import sys
import tempfile

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
ROLE_SETTINGS = ROOT / "role-settings"
PROMPT_FILES = ROOT / "prompt-files"
SLOTS = ROOT / "slots"
MANAGER_TRIGGERS_LOG = ROOT / "manager-triggers.jsonl"
SPEND_LEDGER = ROOT / "spend-ledger.jsonl"

ACCOUNT_ACTIVE = ROOT / "account-active"

TMUX_CONF = ROOT / "dockwright.tmux.conf"

TMUX_CONF_LEGACY = ROOT / "claude-orch.tmux.conf"

ACCOUNT_STATE = ROOT / "account-state.json"
ACCOUNT_LOCK = ROOT / ".account-flip.lock"
ACCOUNT_LEDGER = ROOT / "account-flips.jsonl"

ACCOUNT_REGISTRY = ROOT / "account-registry.json"

SPAWN_COUNTER = ROOT / "spawn-counter.json"

ACCOUNT_USAGE = ROOT / "usage"
USAGE_CAPS = ROOT / "usage-caps.toml"

ARTIFACTS = ROOT / "artifacts"
ASSIGNMENTS = ROOT / "assignments"
ASSIGNMENTS_PENDING = ASSIGNMENTS / ".pending"
PANE_SENDS = ROOT / "pane-sends"
PERMISSION_PROMPTS = ROOT / "permission-prompts"
PERMISSION_LEDGER = ROOT / "permission-answers.jsonl"
ARTIFACT_RETENTION_DAYS = 30
EXPORTS = Path(os.path.realpath(tempfile.gettempdir())) / "dockwright"
ASSIGNMENT_RETENTION_DAYS = 30
PENDING_ASSIGNMENT_TTL_SEC = 24 * 3600

ORPHANS = ROOT / "orphans"

LANE_HEALTH = ROOT / "lane-health"
LANES = ROOT / "lanes"
DAEMON = ROOT / "daemon"
DAEMON_STOP = ROOT / "daemon-stop"
PAUSE = ROOT / "pause"
PAUSE_MANIFEST = PAUSE / "manifest.json"
DAEMON_WAKE = DAEMON / "wake"
CHAT = ROOT / "chat"
CHAT_DB = CHAT / "archive.db"
CHAT_RAW = CHAT / "raw"
CHAT_TRIGGERS = CHAT / "triggers"
CHAT_LEDGER = CHAT / "ledger.jsonl"
CHAT_STARTED = CHAT / "started"
CHAT_LOCK = CHAT / "pass.lock"
CHAT_STOP = ROOT / "chat-archive-stop"
EVENTS = ROOT / "events"
EVENTS_LOG = EVENTS / "log"
EVENTS_SEEN = EVENTS / "seen"
EVENTS_SUBS = EVENTS / "subs"
EVENTS_CURSORS = EVENTS / "cursors"
EVENTS_QUEUES = EVENTS / "queues"

ARCHITECT = ROOT / "architect"

MANAGER_MEMORY = config.manager_memory_root()

CONFIG_HOME = config.claude_config_home()
AGENTS_DIR = CONFIG_HOME / "agents"
ROLES_DIR = config.roles_dir()
HOST_CLAUDE_JSON = Path(os.environ.get("HOME", "")) / ".claude.json"
LAUNCH_AGENTS = Path(os.environ.get("HOME", "")) / "Library" / "LaunchAgents"


def account_config_dir(letter: str) -> Path:
    override = config.account_config_dir_override(letter)
    if override is not None:
        return override
    return CONFIG_HOME.parent / f".claude-{letter}"


def account_usage_path(letter: str) -> Path:
    return ACCOUNT_USAGE / f"{letter}.json"


def dockwright_bin() -> Path:
    return Path(sys.executable).parent / "dockwright"


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


def manager_home() -> Path:
    env = os.environ.get("DOCKWRIGHT_MANAGER_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    return config.manager_home_default()


def ensure_manager_home() -> Path:
    home = manager_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        raise ValueError(
            f"manager home {home} could not be created ({err}); a manager launched anyway "
            "would land in $HOME with no project rules. Fix [paths] manager_home or "
            "DOCKWRIGHT_MANAGER_HOME."
        ) from err
    if not home.is_dir():
        raise ValueError(
            f"manager home {home} is not a directory; a manager launched anyway would land "
            "in $HOME with no project rules. Fix [paths] manager_home or DOCKWRIGHT_MANAGER_HOME."
        )
    return home


def refuse_manager_cwd(cwd: str | Path) -> None:
    home = manager_home()
    if Path(cwd).expanduser().resolve() != home.resolve().parent:
        return
    raise ValueError(
        f"refusing to start a manager in {cwd}: the parent of the manager home carries no "
        f"project rules, and a booted session cannot reload them. Start it in {home} "
        "(`dockwright manager` pins that cwd), or set [paths] manager_home."
    )


DEFAULT_DOMAIN = "general"

ORCHESTRATOR_ENV_KEYS = (
    "CLAUDE_AGENT",
    "CLAUDE_WORKER_NAME",
    "CLAUDE_PARENT_MANAGER",
    "CLAUDE_PARENT_SID",
    "CLAUDE_WORKER_RUNTIME",
    "CLAUDE_PARENT_PID",
    "CLAUDE_DOMAIN",
    "CLAUDE_ITERM_SID",
    "CLAUDE_ASSIGNMENT_ID",
    "CLAUDE_ORCH_ACCOUNT",
    "CLAUDE_WORKER_KIND",
    "CLAUDE_WORKER_PROVIDER",
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


def artifact_export_path(ticket: str, phase: str, name: str, digest: str) -> Path:
    return EXPORTS / "artifacts" / _safe_segment(ticket) / f"{_safe_segment(phase)}.{_safe_segment(name)}.{digest}.md"


def view_export_path(ticket: str, digest: str) -> Path:
    return EXPORTS / "views" / f"{_safe_segment(ticket)}.{digest}.md"


def topic_export_dir(ticket: str) -> Path:
    return EXPORTS / "topic" / _safe_segment(ticket)


def topic_path(ticket: str) -> Path:
    return artifact_ticket_dir(ticket) / "topic.json"


def assignment_path(sid: str) -> Path:
    return ASSIGNMENTS / f"{_safe_segment(sid)}.json"


def pending_assignment_path(assignment_id: str) -> Path:
    return ASSIGNMENTS_PENDING / f"{_safe_segment(assignment_id)}.json"


def pending_window_path(assignment_id: str) -> Path:
    return ASSIGNMENTS_PENDING / f"{_safe_segment(assignment_id)}.window"


def child_rounds_dir(assignment_id: str) -> Path:
    return ROOT / "children" / _safe_segment(assignment_id)


def _event_bucket(parent_name: str | None) -> str:
    if not parent_name:
        return UNSCOPED_BUCKET
    bucket = parent_name.replace("/", "_").replace("\\", "_")
    return f"_{bucket}" if bucket in (".", "..") else bucket


def orphan_flag_path(manager_name: str | None) -> Path:
    return ORPHANS / f"{_event_bucket(manager_name)}.json"


def done_dir_for(parent_name: str | None) -> Path:
    return DONE / _event_bucket(parent_name)


def turn_ends_dir_for(parent_name: str | None) -> Path:
    return TURN_ENDS / _event_bucket(parent_name)


QUESTION_BY_TASK = "by-task"


def question_dir_for(parent_name: str | None) -> Path:
    if not parent_name:
        return QUESTIONS
    bucket = _event_bucket(parent_name)
    return QUESTIONS / (f"_{bucket}" if bucket == QUESTION_BY_TASK else bucket)


def question_task_dir(task_key: str) -> Path:
    return QUESTIONS / QUESTION_BY_TASK / _safe_segment(task_key)


def notify_outbox_dir_for(parent_name: str | None) -> Path:
    return ROOT / "notify-outbox" / _event_bucket(parent_name)


def ensure_dirs() -> None:
    for d in (ACTIVE, QUESTIONS, ANSWERS, DONE, CLOSED, HANDOFFS, TURN_ENDS, PRESETS, SLOTS, MANAGER_MEMORY, ARCHITECT, ARTIFACTS, ASSIGNMENTS, ASSIGNMENTS_PENDING, PANE_SENDS, PERMISSION_PROMPTS,
              EVENTS_LOG, EVENTS_SEEN, EVENTS_SUBS, EVENTS_CURSORS, EVENTS_QUEUES):
        d.mkdir(parents=True, exist_ok=True)
    (DONE / UNSCOPED_BUCKET).mkdir(parents=True, exist_ok=True)
    (TURN_ENDS / UNSCOPED_BUCKET).mkdir(parents=True, exist_ok=True)
