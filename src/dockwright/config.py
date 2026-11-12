from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ENV_CONFIG_PATH = "DOCKWRIGHT_CONFIG"

DEFAULT_STATE_ROOT = "~/.claude/dockwright"
LEGACY_STATE_ROOT = "~/.claude/orchestrator"
DEFAULT_CLAUDE_CONFIG_HOME = "~/.claude"
DEFAULT_PIPELINE_SCHEMAS_DIRNAME = "dockwright-schemas"
DEFAULT_ROLES_DIRNAME = "roles"
DEFAULT_WORKER_HOME = "~/projects/work/worker"
DEFAULT_MANAGER_HOME = "~/projects/work/manager"
DEFAULT_MANAGER_MEMORY = "~/.claude/dockwright/manager-memory"
LEGACY_MANAGER_MEMORY = "~/.claude/manager-memory"
DEFAULT_OVERLAY_DIR = "~/.claude/dockwright-overlay"
LEGACY_OVERLAY_DIR = "~/.claude/orchestrator-overlay"
DEFAULT_SUBWORKERS_MAX_DEPTH = 3
MIN_SUBWORKERS_MAX_DEPTH = 2
DEFAULT_SUBWORKERS_PROVIDER = "codex-exec"
DEFAULT_ASK_PARENT_WAIT_SEC = {"codex-exec": 240, "codex-tui": 1500}
DEFAULT_MANAGER_EFFORT = "medium"
DEFAULT_ASSIGN_COMMAND = "/manager-assign"
DEFAULT_WORKTREE_CLEANUP = ""
DEFAULT_LOOP_LABEL_PREFIX = "com.dockwright"
DEFAULT_ACCOUNT_NAME = "a"
DEFAULT_GARDENER_MODULE_ENABLED = True
DEFAULT_DAEMON_PUSH = "macos"
DAEMON_PUSH_CHANNELS = ("macos", "none")
DEFAULT_DAEMON_PUSH_AFTER_SEC = 180
DEFAULT_DAEMON_PUSH_CAP_PER_HOUR = 6
DEFAULT_WORKTREE_ROOTS = "~/worktrees,~/worktrees-personal"
DEFAULT_REPO_ROOTS = "~/projects/work,~/projects/personal"
DEFAULT_GH_BINARY = "gh"
DEFAULT_GH_TOKEN_TTL_SEC = 600
SCHEDULE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
DEFAULT_EVENTS_ENV_FILE = "~/.config/dockwright/events.env"
DEFAULT_EVENT_CADENCE = {"tracker": 300, "deploy": 60, "time": 60, "github": 600, "chat": 30, "exec": 120}
DEFAULT_EVENT_RULE_MAX_OPEN = 2
EVENT_RULE_ACTIONS = ("start", "page", "queue", "reply")
DEFAULT_EVENT_RULE_NUDGE_SEC = 600
DEFAULT_EVENT_RULE_ACK_SEC = 600
DEFAULT_EVENTS_RETENTION_DAYS = 14
WORK_HOURS_RE = re.compile(r"^\d{2}:\d{2}-\d{2}:\d{2}$")
DEFAULT_WORK_HOURS = "09:00-17:00"
DEFAULT_GITHUB_ENVS = ("prod",)
_DEFAULT_POOL = (("a", 1),)


@dataclass(frozen=True)
class Account:
    name: str
    config_dir: Path | None = None
    weight: int = 1


def _home() -> Path:
    return Path(os.environ.get("HOME", ""))


def _expand(raw: str) -> Path:
    if raw == "~" or raw.startswith("~/"):
        return _home() / raw[2:].lstrip("/") if len(raw) > 2 else _home()
    return Path(raw).expanduser()


def config_path() -> Path | None:
    env = os.environ.get(ENV_CONFIG_PATH, "").strip()
    if env:
        p = _expand(env)
        return p if p.is_file() else None
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    xdg_base = _expand(xdg) if xdg else _home() / ".config"
    for candidate in (xdg_base / "dockwright" / "dockwright.toml",
                      _home() / ".claude" / "dockwright.toml"):
        if candidate.is_file():
            return candidate
    return None


def load() -> dict:
    path = config_path()
    if path is None:
        return {}
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def load_error() -> str | None:
    path = config_path()
    if path is None:
        return None
    try:
        with open(path, "rb") as fh:
            tomllib.load(fh)
        return None
    except (OSError, tomllib.TOMLDecodeError) as e:
        return str(e)


def _section(data: dict, name: str) -> dict:
    sec = data.get(name)
    return sec if isinstance(sec, dict) else {}


def _str_key(section: dict, key: str, default: str) -> str:
    val = section.get(key)
    return val if isinstance(val, str) else default


def _path_key(section: dict, key: str, default: str) -> Path:
    return _expand(_str_key(section, key, default))


def _path_key_with_legacy(section: dict, key: str, default_new: str,
                          default_legacy: str) -> Path:
    val = section.get(key)
    if isinstance(val, str):
        return _expand(val)
    new = _expand(default_new)
    if new.exists():
        return new
    legacy = _expand(default_legacy)
    if legacy.exists():
        return legacy
    return new


def state_root() -> Path:
    return _path_key_with_legacy(_section(load(), "paths"), "state_root",
                                 DEFAULT_STATE_ROOT, LEGACY_STATE_ROOT)


def legacy_state_root() -> Path:
    return _expand(LEGACY_STATE_ROOT)


def claude_config_home() -> Path:
    return _path_key(_section(load(), "paths"), "claude_config_home",
                     DEFAULT_CLAUDE_CONFIG_HOME)


def roles_dir() -> Path:
    raw = _section(load(), "paths").get("roles_dir")
    if isinstance(raw, str) and raw.strip():
        return _expand(raw)
    return claude_config_home() / DEFAULT_ROLES_DIRNAME


def pipeline_schemas_dir() -> Path:
    raw = _section(load(), "pipeline").get("schemas_dir")
    if isinstance(raw, str) and raw.strip():
        return _expand(raw)
    return claude_config_home() / DEFAULT_PIPELINE_SCHEMAS_DIRNAME


def pipeline_node_rules() -> Path | None:
    raw = _section(load(), "pipeline").get("node_rules")
    if isinstance(raw, str) and raw.strip():
        return _expand(raw)
    return None


def pipeline_int(key: str, default: int) -> int:
    raw = _section(load(), "pipeline").get(key)
    if isinstance(raw, int) and not isinstance(raw, bool):
        return max(0, raw)
    return default


def daemon_enabled() -> bool:
    val = _section(load(), "daemon").get("enabled")
    return val if isinstance(val, bool) else False


def daemon_push() -> str:
    val = _section(load(), "daemon").get("push")
    return val if isinstance(val, str) and val in DAEMON_PUSH_CHANNELS else DEFAULT_DAEMON_PUSH


def daemon_int(key: str, default: int) -> int:
    raw = _section(load(), "daemon").get(key)
    if isinstance(raw, int) and not isinstance(raw, bool):
        return max(0, raw)
    return default


CHAT_ARCHIVE_TOOLS = ("search", "read_conversation", "read_thread", "list_conversations")
CHAT_ARCHIVE_REQUIRED = ("mcp_url", "mcp_server", "keychain_service")
CHAT_ARCHIVE_INTS = {"retention_days": 90, "every_sec": 300, "group_sweep_sec": 3600, "registry_sec": 86400,
                     "search_overlap_sec": 900}


def chat_archive() -> dict:
    raw = _section(load(), "chat_archive")
    tools_raw = _section(raw, "tools")
    enabled = raw.get("enabled")
    out = {"enabled": enabled if isinstance(enabled, bool) else False,
           "hand": raw.get("hand") if raw.get("hand") in ("direct", "claude") else "direct",
           "runner": raw.get("runner") if raw.get("runner") in ("launchd", "manager") else "launchd",
           "model": _str_key(raw, "model", ""),
           "mention_keys": _str_list(raw.get("mention_keys")),
           "write_tools": _str_list(raw.get("write_tools")),
           "tools": {name: _str_key(tools_raw, name, "") for name in CHAT_ARCHIVE_TOOLS}}
    for key in (*CHAT_ARCHIVE_REQUIRED, "self_user_id", "sent_footer"):
        out[key] = _str_key(raw, key, "")
    for key, default in CHAT_ARCHIVE_INTS.items():
        out[key] = _pos_num(raw.get(key), default)
    if not out["mention_keys"] and out["self_user_id"]:
        out["mention_keys"] = [out["self_user_id"]]
    errors = [f"{key} is empty" for key in CHAT_ARCHIVE_REQUIRED if not out[key]]
    errors += [f"tools.{name} is empty" for name in CHAT_ARCHIVE_TOOLS if not out["tools"][name]]
    if not out["write_tools"]:
        errors.append("write_tools is empty")
    if errors:
        out["enabled"] = False
    out["errors"] = errors
    return out


def lanes_in_manager() -> bool:
    val = _section(load(), "lanes").get("in_manager")
    return val if isinstance(val, bool) else True


def lanes_env() -> dict[str, str]:
    section = _section(_section(load(), "lanes"), "env")
    return {k: v for k, v in section.items() if isinstance(k, str) and isinstance(v, str)}


def gh_configured() -> bool:
    return isinstance(load().get("gh"), dict)


def gh_binary() -> str:
    return _str_key(_section(load(), "gh"), "binary", DEFAULT_GH_BINARY)


def gh_logins() -> dict[str, str]:
    section = _section(_section(load(), "gh"), "logins")
    return {k: v for k, v in section.items() if isinstance(k, str) and isinstance(v, str) and v}


def gh_token_ttl_sec() -> int:
    raw = _section(load(), "gh").get("token_ttl_sec")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    return DEFAULT_GH_TOKEN_TTL_SEC


def schedules() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name, raw in _section(load(), "schedules").items():
        if not isinstance(name, str) or not SCHEDULE_NAME_RE.match(name) or not isinstance(raw, dict):
            continue
        errors = [f"{key} is required" for key in ("schema", "cron", "ask")
                  if not (isinstance(raw.get(key), str) and raw.get(key).strip())]
        enabled = raw.get("enabled")
        out[name] = {"schema": raw.get("schema"), "cron": raw.get("cron"), "ask": raw.get("ask"),
                     "cwd": os.path.expanduser(raw["cwd"]) if isinstance(raw.get("cwd"), str) and raw["cwd"] else None,
                     "domain": raw.get("domain") if isinstance(raw.get("domain"), str) else None,
                     "enabled": enabled if isinstance(enabled, bool) else True, "errors": errors}
    return out


def _events() -> dict:
    return _section(load(), "events")


def events_enabled() -> bool:
    val = _events().get("enabled")
    return val if isinstance(val, bool) else False


def events_env_file() -> Path:
    return _path_key(_events(), "env_file", DEFAULT_EVENTS_ENV_FILE)


def events_retention_days() -> int:
    return _pos_int(_events().get("retention_days"), DEFAULT_EVENTS_RETENTION_DAYS)


def events_sent() -> dict:
    return {"tools": _str_list(_section(_events(), "sent").get("tools"))}


def _str_list(raw) -> list[str]:
    return [x for x in raw if isinstance(x, str) and x] if isinstance(raw, list) else []


def _pos_num(raw, default):
    return raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0 else default


def _pos_int(raw, default):
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else default


def _str_table(raw) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    return {k: v for k, v in raw.items()} if all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items()) \
        else None


def _while_table(entry: dict, errors: list[str]) -> dict | None:
    if "while" not in entry:
        return None
    raw_while = entry["while"]
    if isinstance(raw_while, dict) and isinstance(raw_while.get("kind"), str) and raw_while["kind"]:
        raw_match = raw_while.get("match")
        match = {k: v for k, v in raw_match.items() if isinstance(k, str) and isinstance(v, str)} \
            if isinstance(raw_match, dict) else {}
        return {"kind": raw_while["kind"], "match": match}
    errors.append("while must be a table with a string kind")
    return None


def _exec_commands(raw, every: int) -> list[dict]:
    out = []
    for i, entry in enumerate(raw if isinstance(raw, list) else []):
        entry = entry if isinstance(entry, dict) else {}
        errors = []
        argv = entry.get("argv")
        if not (isinstance(argv, list) and argv and all(isinstance(a, str) and a for a in argv)):
            argv = []
            errors.append("argv must be a list of non-empty strings")
        while_ = _while_table(entry, errors)
        stop_file = None
        if "stop_file" in entry:
            if isinstance(entry["stop_file"], str) and entry["stop_file"]:
                stop_file = os.path.expanduser(entry["stop_file"])
            else:
                errors.append("stop_file must be a path")
        out.append({"argv": list(argv), "every_sec": _pos_int(entry.get("every_sec"), every), "while": while_,
                    "stop_file": stop_file, "error": errors[0] if errors else None,
                    "selector": " ".join(argv) if argv else f"commands[{i}]"})
    return out


def _clock_span(window: str) -> bool:
    if not WORK_HOURS_RE.match(window):
        return False
    lo, hi = window.split("-")
    try:
        return time.fromisoformat(lo) < time.fromisoformat(hi)
    except ValueError:
        return False


def _zone_known(name: str) -> bool:
    if not name:
        return True
    try:
        ZoneInfo(name)
    except (ValueError, OSError, ZoneInfoNotFoundError):
        return False
    return True


def events_handler_path() -> list[Path]:
    raw = _events().get("handler_path")
    if not isinstance(raw, list):
        return [claude_config_home() / "scripts"]
    return [_expand(x) for x in raw if isinstance(x, str) and x]


def events_handlers() -> list[str]:
    return _str_list(_events().get("handlers"))


def events_source(name: str) -> dict:
    raw = _section(_section(_events(), "sources"), name)
    every = raw.get("every_sec")
    work_hours = _str_key(raw, "work_hours", DEFAULT_WORK_HOURS)
    tz = _str_key(raw, "tz", "")
    envs = _str_list(raw.get("envs")) or list(DEFAULT_GITHUB_ENVS)
    every_sec = _pos_int(every, DEFAULT_EVENT_CADENCE.get(name, 60))
    return {"every_sec": every_sec,
            "workflows": _str_list(raw.get("workflows")),
            "job_match": _str_key(raw, "job_match", ""),
            "job_skip": _str_key(raw, "job_skip", ""),
            "owners": _str_list(raw.get("owners")),
            "review_after_hours": _pos_num(raw.get("review_after_hours"), 8),
            "approval_after_hours": _pos_num(raw.get("approval_after_hours"), 1),
            "repeat_hours": _pos_num(raw.get("repeat_hours"), 8),
            "work_hours": work_hours if _clock_span(work_hours) else DEFAULT_WORK_HOURS,
            "tz": tz if _zone_known(tz) else "",
            "merged_window_days": _pos_num(raw.get("merged_window_days"), 14),
            "envs": envs,
            "deployed_envs": _str_list(raw.get("deployed_envs")) or list(envs),
            "fallback_envs": _str_list(raw.get("fallback_envs")),
            "fallback_skipped_job": _str_key(raw, "fallback_skipped_job", ""),
            "ignore_reviewers": _str_list(raw.get("ignore_reviewers")),
            "apps": _str_list(raw.get("apps")),
            "sprint_every_sec": _pos_int(raw.get("sprint_every_sec"), 1200),
            "commands": _exec_commands(raw.get("commands"), every_sec)}


def events_rules() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name, raw in _section(_events(), "rules").items():
        if not isinstance(name, str) or not SCHEDULE_NAME_RE.match(name) or not isinstance(raw, dict):
            continue
        action = raw.get("action", "start")
        errors = [f"{key} is required" for key in ("source", "kind")
                  if not (isinstance(raw.get(key), str) and raw.get(key).strip())]
        if action not in EVENT_RULE_ACTIONS:
            errors.append("action must be start, page, queue or reply")
        elif action == "start" and not (isinstance(raw.get("schema"), str) and raw["schema"].strip()):
            errors.append("schema is required")
        enabled = raw.get("enabled")
        max_open = raw.get("max_open")
        match = _str_table(raw.get("match")) if "match" in raw else {}
        if match is None:
            errors.append("match must be a table of strings")
            match = {}
        stop_file = None
        if "stop_file" in raw:
            if isinstance(raw["stop_file"], str) and raw["stop_file"]:
                stop_file = os.path.expanduser(raw["stop_file"])
            else:
                errors.append("stop_file must be a path")
        while_ = _while_table(raw, errors)
        on_change = None
        if "on_change" in raw:
            if isinstance(raw["on_change"], str) and raw["on_change"]:
                on_change = raw["on_change"]
                if action != "queue":
                    errors.append('on_change needs action = "queue"')
            else:
                errors.append("on_change must be a string")
        if "unless_rules" in raw and action != "reply":
            errors.append('unless_rules needs action = "reply"')
        if "confirm_events" in raw and on_change is None:
            errors.append("confirm_events needs on_change")
        if "ack_sec" in raw and action != "reply":
            errors.append('ack_sec needs action = "reply"')
        inbound = raw.get("inbound") is True
        if "inbound" in raw:
            if not isinstance(raw["inbound"], bool):
                errors.append("inbound must be true or false")
            elif action != "reply":
                errors.append('inbound needs action = "reply"')
            elif inbound and raw.get("kind") not in ("dm", "dm-thread"):
                errors.append('inbound needs kind = "dm" or "dm-thread"')
        if "inbound_skip_senders" in raw and not inbound:
            errors.append("inbound_skip_senders needs inbound = true")
        out[name] = {"source": raw.get("source"), "kind": raw.get("kind"), "action": action,
                     "schema": raw.get("schema") if isinstance(raw.get("schema"), str) else None,
                     "domain": raw.get("domain") if isinstance(raw.get("domain"), str) else None,
                     "max_open": max_open if isinstance(max_open, int) and not isinstance(max_open, bool) and max_open > 0
                     else DEFAULT_EVENT_RULE_MAX_OPEN,
                     "types": _str_list(raw.get("types")), "skip_status": _str_list(raw.get("skip_status")),
                     "enabled": enabled if isinstance(enabled, bool) else True,
                     "match": match, "stop_file": stop_file, "while": while_,
                     "nudge_sec": _pos_int(raw.get("nudge_sec"), DEFAULT_EVENT_RULE_NUDGE_SEC),
                     "on_change": on_change, "repeat_sec": _pos_int(raw.get("repeat_sec"), None),
                     "repeat_values": _str_list(raw.get("repeat_values")),
                     "confirm_events": _pos_int(raw.get("confirm_events"), 1),
                     "unless_rules": _str_list(raw.get("unless_rules")),
                     "ack_sec": _pos_int(raw.get("ack_sec"), DEFAULT_EVENT_RULE_ACK_SEC), "inbound": inbound,
                     "inbound_skip_senders": _str_list(raw.get("inbound_skip_senders")), "errors": errors}
    for rule in out.values():
        rule["errors"].extend(f"unless_rules names an unknown rule {other}" for other in rule["unless_rules"]
                              if other not in out)
    return out


def worker_home_default() -> Path:
    return _path_key(_section(load(), "paths"), "worker_home", DEFAULT_WORKER_HOME)


def manager_home_default() -> Path:
    return _path_key(_section(load(), "paths"), "manager_home", DEFAULT_MANAGER_HOME)


def manager_memory_root() -> Path:
    return _path_key_with_legacy(_section(load(), "paths"), "manager_memory",
                                 DEFAULT_MANAGER_MEMORY, LEGACY_MANAGER_MEMORY)


def overlay_dir() -> Path:
    return _path_key_with_legacy(_section(load(), "paths"), "overlay_dir",
                                 DEFAULT_OVERLAY_DIR, LEGACY_OVERLAY_DIR)


def worktree_roots() -> str:
    return _str_key(_section(load(), "paths"), "worktree_roots", DEFAULT_WORKTREE_ROOTS)


def repo_roots() -> str:
    return _str_key(_section(load(), "paths"), "repo_roots", DEFAULT_REPO_ROOTS)


def dockwright_repo() -> str:
    raw = _str_key(_section(load(), "paths"), "dockwright_repo", "")
    return str(_expand(raw)) if raw else ""


def agent_vars() -> dict[str, str]:
    section = _section(load(), "agent_vars")
    return {k: v for k, v in section.items()
            if isinstance(k, str) and isinstance(v, str)}


def tech_debt_systems() -> dict[str, str]:
    section = _section(_section(load(), "tech_debt"), "systems")
    return {k: v for k, v in section.items()
            if isinstance(k, str) and isinstance(v, str)}


def profiles() -> dict:
    section = _section(load(), "profiles")
    sets = {k: [d for d in v if isinstance(d, str)] for k, v in _section(section, "sets").items()
            if isinstance(k, str) and isinstance(v, list)}
    repos = {k: v for k, v in _section(section, "repos").items() if isinstance(k, str) and isinstance(v, str)}
    roots = [r for r in section.get("work_roots", []) if isinstance(r, str)]
    owners = [o for o in section.get("personal_owners", []) if isinstance(o, str)]
    default = section.get("work_default")
    return {"sets": sets, "repos": repos, "work_roots": roots, "personal_owners": owners,
            "work_default": default if isinstance(default, str) and default else "none"}


def subagents() -> dict[str, dict]:
    section = _section(load(), "subagents")
    return {k: v for k, v in section.items() if isinstance(k, str) and isinstance(v, dict)}


def subagent_default_role() -> str | None:
    value = _section(load(), "subagents").get("default_role")
    return value if isinstance(value, str) and value else None


def project_units_dir() -> Path | None:
    raw = _str_key(_section(load(), "paths"), "project_units_dir", "")
    return _expand(raw) if raw else None


def roles_strict() -> bool:
    return _section(load(), "roles").get("strict") is True


def roles_computed() -> bool:
    return _section(load(), "roles").get("computed") is True


def agent_ceilings() -> dict[str, dict[str, int]]:
    section = _section(load(), "ceilings")
    out: dict[str, dict[str, int]] = {}
    for name, val in section.items():
        if not isinstance(name, str):
            continue
        limits: dict[str, int] = {}
        if isinstance(val, dict):
            for key in ("bytes", "lines", "min_dropins", "min_agent_vars"):
                raw = val.get(key)
                if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                    limits[key] = raw
        out[name] = limits
    return out


def _spawn_model(key: str) -> str:
    value = _section(load(), "spawn").get(key)
    if isinstance(value, str):
        return value
    from .models import default_model
    return default_model(key)


def worker_headless_preset() -> bool:
    val = _section(load(), "spawn").get("worker_headless_preset", True)
    return val if isinstance(val, bool) else True


def manager_model() -> str:
    from .models import resolve_model
    return resolve_model(_spawn_model("manager_model"))


def manager_effort() -> str:
    return _str_key(_section(load(), "spawn"), "manager_effort", DEFAULT_MANAGER_EFFORT)


def distill_model() -> str:
    return _spawn_model("distill_model")


def scout_model() -> str:
    value = chat_archive()["model"]
    if value:
        return value
    from .models import default_model
    return default_model("scout_model")


def spawn_env() -> dict[str, str]:
    section = _section(_section(load(), "spawn"), "env")
    return {k: v for k, v in section.items()
            if isinstance(k, str) and isinstance(v, str)}


def assign_command_hint() -> str:
    return _str_key(_section(load(), "hints"), "assign_command", DEFAULT_ASSIGN_COMMAND)


def worktree_cleanup_hint() -> str:
    return _str_key(_section(load(), "hints"), "worktree_cleanup",
                    DEFAULT_WORKTREE_CLEANUP)


def loop_label_prefix() -> str:
    return _str_key(_section(load(), "loops"), "label_prefix",
                    DEFAULT_LOOP_LABEL_PREFIX)


DEFAULT_PAUSE_STOP_FILES = ("daemon-stop", "chat-archive-stop", "worktree-prune-stop", "bootlite-stop")


def pause_stop_files() -> list[str]:
    raw = _section(load(), "pause").get("stop_files")
    return _str_list(raw) if isinstance(raw, list) else list(DEFAULT_PAUSE_STOP_FILES)


def pause_launchd_disable() -> list[str]:
    return _str_list(_section(load(), "pause").get("launchd_disable"))


def pause_hold() -> dict[str, str]:
    hold = _section(_section(load(), "pause"), "hold")
    return {k: v for k, v in hold.items() if isinstance(k, str) and isinstance(v, str)}


def pause_errors() -> list[str]:
    error = load_error()
    if error is not None:
        return [f"dockwright.toml does not parse: {error}"]
    raw_section = load().get("pause")
    if raw_section is not None and not isinstance(raw_section, dict):
        return ["[pause] must be a table"]
    section = raw_section or {}
    errors = [f"[pause] {key} is not a key; the keys are stop_files, launchd_disable, hold"
              for key in section if key not in ("stop_files", "launchd_disable", "hold")]
    for key in ("stop_files", "launchd_disable"):
        raw = section.get(key)
        if key in section and not (isinstance(raw, list) and all(isinstance(x, str) and x for x in raw)):
            errors.append(f"[pause] {key} must be a list of strings")
    hold = section.get("hold")
    if "hold" in section and not isinstance(hold, dict):
        errors.append("[pause.hold] must be a table")
    for k, v in (hold.items() if isinstance(hold, dict) else ()):
        if not (isinstance(k, str) and isinstance(v, str)):
            errors.append(f'[pause.hold] "{k}" must be a string')
    return errors


def loop_status_overrides() -> dict[str, dict]:
    sec = _section(_section(load(), "loops"), "status_overrides")
    out = {}
    for name, val in sec.items():
        if isinstance(val, dict):
            ov = {k: v for k, v in val.items() if k in ("status", "status_why") and isinstance(v, str)}
            if ov:
                out[name] = ov
    return out


def gardener_module_enabled() -> bool:
    val = _section(load(), "modules").get("gardener")
    return val if isinstance(val, bool) else DEFAULT_GARDENER_MODULE_ENABLED


def task_key_regex() -> str | None:
    val = _str_key(_section(load(), "task_keys"), "key_regex", "")
    return val if val else None


def ticket_url() -> str | None:
    val = _str_key(_section(load(), "task_keys"), "ticket_url", "")
    return val if "{key}" in val else None


def gardener_high_skills() -> tuple[str, ...]:
    raw = _section(load(), "gardener").get("high_skills")
    if not isinstance(raw, list):
        return ()
    return tuple(x for x in raw if isinstance(x, str))


def _default_pool() -> list[Account]:
    return [Account(name=n, config_dir=None, weight=w) for n, w in _DEFAULT_POOL]


def accounts() -> list[Account]:
    raw = _section(load(), "accounts").get("pool")
    if not isinstance(raw, list) or not raw:
        return _default_pool()
    out: list[Account] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            return _default_pool()
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip() or name in seen:
            return _default_pool()
        seen.add(name)
        weight = entry.get("weight", 1)
        if isinstance(weight, bool) or not isinstance(weight, int) or weight < 1:
            return _default_pool()
        config_dir = entry.get("config_dir")
        if config_dir is not None and not isinstance(config_dir, str):
            return _default_pool()
        out.append(Account(
            name=name,
            config_dir=_expand(config_dir) if config_dir else None,
            weight=weight,
        ))
    return out


def account_names() -> tuple[str, ...]:
    return tuple(a.name for a in accounts())


def account_weight(name: str) -> int:
    for a in accounts():
        if a.name == name:
            return a.weight
    return 1


def account_config_dir_override(name: str) -> Path | None:
    for a in accounts():
        if a.name == name:
            return a.config_dir
    return None


def default_account() -> str:
    pool = accounts()
    names = [a.name for a in pool]
    d = _section(load(), "accounts").get("default")
    if isinstance(d, str) and d in names:
        return d
    return DEFAULT_ACCOUNT_NAME if DEFAULT_ACCOUNT_NAME in names else names[0]


def usage_pause_pct() -> float | None:
    raw = _section(load(), "accounts").get("usage_pause_pct")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return None
    return float(raw)


def mcp_oauth_required() -> list[str]:
    return _str_list(_section(load(), "accounts").get("mcp_oauth_required"))


def subworkers_max_depth() -> int:
    raw = _section(load(), "subworkers").get("max_depth")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return DEFAULT_SUBWORKERS_MAX_DEPTH
    return max(raw, MIN_SUBWORKERS_MAX_DEPTH)


def codex_allowed_roots() -> tuple[Path, ...]:
    raw = _section(load(), "codex").get("allowed_roots")
    if not isinstance(raw, list):
        return ()
    out: list[Path] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            return ()
        out.append(_expand(entry).resolve())
    return tuple(out)


def subworkers_default_provider() -> str:
    raw = _section(load(), "subworkers").get("default_provider")
    return raw if isinstance(raw, str) and raw.strip() else DEFAULT_SUBWORKERS_PROVIDER


def provider_setting(name: str, key: str):
    providers = _section(_section(load(), "subworkers"), "providers")
    return _section(providers, name).get(key)


def provider_model(name: str) -> str | None:
    raw = provider_setting(name, "model")
    return raw if isinstance(raw, str) and raw.strip() else None


def provider_effort(name: str) -> str | None:
    raw = provider_setting(name, "effort")
    return raw if isinstance(raw, str) and raw.strip() else None


def ask_parent_wait_sec(name: str | None) -> int | None:
    if not name:
        return None
    raw = provider_setting(name, "ask_parent_wait_sec")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    return DEFAULT_ASK_PARENT_WAIT_SEC.get(name)


def pricing_overrides() -> dict[str, tuple[float, float]]:
    rates = _section(_section(load(), "pricing"), "rates")
    out: dict[str, tuple[float, float]] = {}
    for key, val in rates.items():
        if (isinstance(key, str) and isinstance(val, list) and len(val) == 2
                and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                        for x in val)):
            out[key] = (float(val[0]), float(val[1]))
    return out


DEFAULT_TOML = '''\
# dockwright.toml — dockwright (claude-orchestrator) operator config.
#
# EVERY key is optional. A missing key — or a missing file — yields the
# documented default below, which reproduces the behavior that used to be
# hardcoded. A corrupt file is fail-open to defaults; `dockwright doctor`
# surfaces the parse error.
#
# Discovery order (first existing file wins):
#   1. $DOCKWRIGHT_CONFIG (explicit path; authoritative when set)
#   2. $XDG_CONFIG_HOME/dockwright/dockwright.toml   (~/.config/dockwright/...)
#   3. ~/.claude/dockwright.toml

[paths]
# dockwright runtime state (active/, questions/, done/, artifacts/, ...).
state_root = "~/.claude/dockwright"
# Canonical Claude config dir — the account-farm symlink source.
claude_config_home = "~/.claude"
# Default cwd for spawned workers (env CLAUDE_ORCH_WORKER_HOME still wins).
worker_home = "~/projects/work/worker"
# The cwd every manager launch pins (env DOCKWRIGHT_MANAGER_HOME still wins).
# A manager is refused in this directory's PARENT: that directory carries no
# project rules, and a session cannot reload rules after it has booted.
manager_home = "~/projects/work/manager"
# Distilled manager-session journals.
manager_memory = "~/.claude/dockwright/manager-memory"
# Agent-file overlay drop-ins (compose seam). Kept OUTSIDE state_root —
# that subtree is rsync-managed runtime state.
overlay_dir = "~/.claude/dockwright-overlay"
# Role manifests — one ~/.claude/roles/<Role>/role.toml per role, rendered by
# `dockwright compose` into role.<launch>.md and role.settings.json beside it.
# Default: roles/ in claude_config_home.
# roles_dir = "~/.claude/roles"
# Comma-separated worktree root directories searched for existing worktrees
# (e.g. dockwright-general-work / ticket tooling).
worktree_roots = "~/worktrees,~/worktrees-personal"
# Comma-separated repo root directories for the same lookup.
repo_roots = "~/projects/work,~/projects/personal"
# This dockwright checkout's own path, for self-referential tooling (e.g.
# the Gardener digest/frontier skills). Default: unset. Example:
# dockwright_repo = "~/projects/personal/claude-orchestrator"

[spawn]
# Pinned --model for manager tabs. A worker spawn names its own --model and
# --effort in extra_args; a Claude worker launch without both is refused.
manager_model = "@fable[1m]"
# --effort for manager launches: `dockwright manager`, spawn_replacement_manager,
# revive and bootstrap-recreate.sh (through `dockwright manager-launch-args`).
# stale_monitor.py's crash recovery ignores this key and manager_model and
# launches on its rescue pair.
# The session effortLevel is one global value, so the per-model level rides the
# spawn — move this whenever you move manager_model.
manager_effort = "medium"
# Model for the headless manager-memory distill (`claude -p`).
distill_model = "claude-sonnet-4-6"
# When true (default), claude worker spawns/resumes default to the deployed
# worker-headless-settings.json preset (auto permission mode, protocol
# allowlist, local git verbs, code-root additionalDirectories). Set false to
# opt out fleet-wide. Default: true (unset behaves the same).
# worker_headless_preset = true

[spawn.env]
# Extra environment variables merged into spawned worker sessions (str ->
# str entries only; non-string values are skipped; caller-supplied env
# still wins). Example:
# MY_FLAG = "1"

[codex]
# Directories dockwright may launch Codex under — as a child (spawn_child), as
# spawn_worker(runtime="codex"), or through resume_worker. Empty (the default)
# means Codex is disabled everywhere. `~` expands; symlinks resolve on both
# sides. A malformed value also means disabled, never the default.
allowed_roots = []

[subworkers]
# The first depth spawn_child refuses to create. Depths: manager 0, worker 1,
# subworker 2 — so 3 lets a worker spawn children and refuses a subworker's;
# 2 disables subworkers. Values below 2 clamp to 2; a non-integer means 3.
max_depth = 3
# Provider for a WORKER's child when spawn_child gets no `provider`. A
# manager's child is always claude-tui.
default_provider = "codex-exec"

[subworkers.providers.codex-exec]
# One ask_parent wait; the Codex MCP client drops a call at 300 s unless the
# child's argv raises mcp_servers.dockwright.tool_timeout_sec, which it does to
# this value + 60.
ask_parent_wait_sec = 240

[subworkers.providers.codex-tui]
# One ask_parent wait; the progress keepalive that lifts a call to 12 h is
# proven only for the Claude client, so a Codex TUI keeps this bound.
ask_parent_wait_sec = 1500

[subworkers.providers.claude-exec]
# The topic agent's model and effort. Empty refuses the agent at spawn: this is a
# headless `claude -p`, which never rides the CLI's global default model.
model = "@opus"
effort = "high"

[accounts]
# The account that runs on the default login (no CLAUDE_CONFIG_DIR).
default = "a"

# Worker-spawn pause threshold, as a percent of the 5h usage window. When every
# selectable account is at/above this on its 5h window, spawn_worker refuses
# (returns {"status":"paused", ...}) instead of spawning; pass force=True to
# bypass. Default 88 (env CLAUDE_ORCH_USAGE_PAUSE_PCT wins). Set above 100 to
# disable the pause (used pcts never exceed 100).
# usage_pause_pct = 88

# MCP servers every pool account must hold an OAuth token for. dockwright doctor
# reads each account's keychain item and fails per account without one (macOS).
# mcp_oauth_required = ["<server name>"]

# The account pool, in round-robin order. Default: just the default account —
# the pool feature stays dormant until <state-root>/account-active exists, and
# a single-account pool always routes to your real login. To add a second
# account: uncomment below, pick a name, and /login its config dir ONCE
# (CLAUDE_CONFIG_DIR=~/.claude-<name> claude, then /login). For a non-default
# account, config_dir defaults to the "~/.claude-<name>" convention; set it to
# relocate. Weights bias the spawn round-robin
# (env CLAUDE_ORCH_ACCOUNT_WEIGHT_<NAME> still wins).
# manager_remote_control: a manager on this account starts Remote Control only
# when the key is absent or exactly true.
[[accounts.pool]]
name = "a"
weight = 1

# [[accounts.pool]]
# name = "b"
# weight = 1
# manager_remote_control = false

[hints]
# Command named in promote.py's "run this from a live session" hint.
assign_command = "/manager-assign"
# Command named in `dockwright sweep`'s worktree-pruning hint. Empty
# (the default) omits the hint line entirely. Example:
# worktree_cleanup = "<your-cleanup-command> --dry-run"

[loops]
# launchd label namespace for background loops (deploy/loops-registry.md).
# Labels are rendered as "<label_prefix>.<loop-name>" by
# deploy/scripts/*-install.sh. Changing this between installs of the SAME
# loop creates a NEW plist under the new label — the old one is not removed
# automatically (launchctl bootout + rm it by hand).
label_prefix = "com.dockwright"

# [pause]
# Stop files `dockwright pause` creates (relative to the state root, or absolute)
# and `dockwright resume` removes if they did not exist before the pause.
# daemon-stop is always created, listed or not.
# stop_files = ["daemon-stop", "chat-archive-stop", "worktree-prune-stop", "bootlite-stop"]
# launchd labels pause unloads and disables; resume enables and bootstraps them. A label
# that is not loaded at pause time is left alone on both sides.
# launchd_disable = []
# [pause.hold]
# Stop files pause creates and resume KEEPS, with the command that lifts each one;
# `{<file>:<text>}` expands to <text> when <file> existed under the state root before the pause.
# "my-lane-stop" = "/start-my-lane {my-lane-flag:--act}"

[modules]
# Toggle optional subsystems. Currently just the Gardener retrospective
# pipeline (selffix/ops-evidence digest -> ranked improvement proposals).
gardener = true

[task_keys]
# Regex matching a valid task key (e.g. an issue-tracker key like "ABC-1234"),
# used to RECOGNIZE task references (the unkeyed-spawn task_key_hint,
# doc-level tooling). Never used to derive a filing key — keying is explicit
# task_key= at spawn. Default: unset — no recognition. Example:
# key_regex = '[A-Za-z]{2,}-\\d+'
#
# URL template that makes a ticket key a link on the board; {key} is replaced
# by the key. Default: unset — the key shows as text. Example:
# ticket_url = 'https://tracker.example/browse/{key}'

[pipeline]
# The pipeline engine: where operator schemas live (<schemas_dir>/<schema>.toml,
# checked by `dockwright pipeline validate <schema>`) and its timings, in seconds.
# schemas_dir = "~/.claude/dockwright-schemas"
# requested_ttl_sec = 300
# trigger_poll_sec = 600
# 0 (default) never wakes on the clock; set it to wake a waiting post-merge node
# at least this often.
# trigger_heartbeat_sec = 0
# A new review wakes the PR holder once its PR has had no newer review for this
# many seconds, so reviews posted one by one reach the holder as one batch.
# review_quiet_sec = 1800
# A displaced node that never reports worker_done is closed after this many
# seconds; one with a report closes at the next reconcile pass.
# handed_off_grace_sec = 300
# exit_check_timeout_sec = 600
# The context size, in tokens, at which a node's checkpoint put ends the session
# and the engine continues the node in a fresh one from its checkpoints.
# fresh_context_tokens = 500000
# How many times a node may send the work back along a back edge to one row
# of plan.tasks in one round; past it, the node asks the manager.
# back_cap = 10
# A work-owned slot (an environment lease) expires after this many seconds.
# lease_ttl_sec = 7200
# A file of node rules with the headings "## decided_by on decision records"
# and "## The running summary". Set: a node's footer names the file first and
# points at those sections. Unset, or a file that is missing or lacks either
# heading: the footer carries both texts in full.
# node_rules = "~/.claude/pipeline-node-rules.md"

[daemon]
# The fleet daemon: a launchd job that runs `dockwright daemon tick` once a
# minute. Each pass reads every manager's done, turn-end and question queues
# with its own bookmarks and writes <state_root>/daemon/ledger.jsonl. setup.sh
# installs the job while enabled = true and removes it while false. The file
# <state_root>/daemon-stop pauses it without unloading it; with [lanes]
# in_manager = false that also stops every manager page, and the pages
# monitor says so (DAEMON_STOPPED).
enabled = false
# Where "a topic waits on you" goes: "macos" (a notification) or "none".
push = "macos"
# Seconds a topic waits on you before its one notification for that wait.
push_after_sec = 180
# Notifications per rolling hour; the rest are counted into the next one.
push_cap_per_hour = 6

[lanes]
# true (default): the manager tab runs the questions, done, turn-ends and stale
# lane monitors and the daemon only observes. false: those four lanes do one
# last scan and exit, the daemon writes QUESTION/DONE/FINISHED_SILENTLY lines
# into each manager's notify-outbox, runs the stale checks, the reconciler and
# the note checks for every live manager, and starts [schedules]. The pages
# lane monitor runs in the tab either way. The daemon remembers the last value
# it read (daemon/mode.json) and keeps it while this table is absent or the
# file does not parse: to return the lanes to the tabs write true here.
in_manager = true

[lanes.env]
# CLAUDE_ORCH_* knobs the scans read from their environment. The daemon runs
# under launchd, which gives it PATH and nothing else, so `dockwright daemon
# sync` copies these pairs into both plists' EnvironmentVariables; a change
# here plus setup.sh reinstalls the jobs. Example:
# CLAUDE_ORCH_AUTONUDGE = "1"
# CLAUDE_ORCH_IDLE_TTL_HOURS = "8"

# [gh]
# How the engine runs gh for a PR URL (merge trigger, pipeline_rework). With
# this table present the engine takes the URL's owner, looks up its login in
# [gh.logins], asks `<binary> auth token -u <login>` for the token and runs gh
# with that GH_TOKEN and no cwd; an owner not in the table is refused by name.
# Without this table gh runs as today, from the work's directory. Both headers
# stay commented until you fill the logins. Example:
# binary = "gh"
# token_ttl_sec = 600
# [gh.logins]
# <github owner> = "<gh login that can see it>", e.g. acme = "alice"

[schedules]
# Works the daemon starts on a cron slot, under the parent `daemon`:
# [schedules.<name>]   name: [A-Za-z0-9_-]{1,32}
# schema = "<schema>"   cron = "*/15 * * * *"   ask = "<the ask>"
# cwd = "~/projects/work/worker"   domain = "meta"   enabled = true
# Five cron fields (min hour dom mon dow); *, n, a-b, */n, a-b/n, lists.
# A slot is started once; while the previous start of a schedule is still
# running its slot is skipped; after a sleep only the LAST missed slot starts.

# [events]
# The event layer: the daemon pass (and the manager's stale lane) polls the
# sources a live subscription or an enabled rule names, stores the events under
# <state_root>/events/ and wakes a subscribed node with one `event: ...` line.
# Off (the default), `event subscribe` is refused and nothing is polled.
# enabled = false
# KEY=VALUE file, mode 0600, with TRACKER_URL, TRACKER_EMAIL and TRACKER_API_TOKEN for
# the tracker source; a missing or group-readable file keeps that source idle.
# env_file = "~/.config/dockwright/events.env"
# Directories a subscription's handler is looked up in, and the only handler
# argv a subscription may carry: each entry is a name, or a name followed by the
# leading arguments it pins ("author-wait close"); a path or another argv is refused.
# handler_path = ["~/.claude/scripts"]
# handlers = []
# day files of events/log older than this are deleted hourly by the events
# step; the read window and the dedup follow it.
# retention_days = 14
# [events.sources.tracker]
# every_sec = 300
# the sprint search of an enabled sprint rule runs on a tracker poll at most this often; issue comments and status keep every_sec.
# sprint_every_sec = 1200
# [events.sources.deploy]
# every_sec = 60
# The GitHub Actions workflows whose runs carry a deploy job, and the regexes
# that pick (job_match) and skip (job_skip) a job by name; the environment is
# read from the job name in parentheses. All three are yours: none is built in.
# env=prod is read from GitHub Deployments of [events.sources.github] deployed_envs, never from a job.
# workflows = []
# job_match = ""
# job_skip = ""
# [events.sources.time]
# every_sec = 60
# [events.sources.github]
# every_sec = 600
# owners = []                   # GitHub owners to watch; each resolves to a login through [gh.logins]
# review_after_hours = 8        # business hours without a human review before a review-wait event
# approval_after_hours = 1      # business hours a run waits for approval before an approval-wait event
# repeat_hours = 8              # business hours between repeated events for the same wait
# work_hours = "09:00-17:00"    # the business-hours window, weekdays only
# tz = ""                       # zone of the window; "" is the machine zone
# merged_window_days = 14       # repos our account merged into within this many days are watched for runs
# envs = ["prod"]               # environments whose pending approval counts
# deployed_envs = ["prod"]      # a successful deployment here means our merge is already deployed; defaults to envs
# fallback_envs = []            # environments whose pending approval counts only when no envs approval is pending
#                               # and every job matching fallback_skipped_job ended skipped (a hotfix gate after a skipped prod job)
# fallback_skipped_job = ""     # regex over the run's job names; empty: fallback_envs never page
# ignore_reviewers = []         # logins whose review does not count (bots with a user login)
# [events.sources.chat]
# every_sec = 30
# macOS only: reads the system notification store read-only; the process that
# polls (the daemon, or the manager's terminal) needs Full Disk Access. apps
# names the bundle ids whose notifications are read; none is built in.
# Kinds dm, channel, dm-thread, channel-thread; match app, conversation (#name
# for every channel), sender, channel (conversation id), thread (parent ts).
# While the chat archive ([chat_archive] below) is on, each archived message
# of a person is one chat event, with or without a notification; a
# notification is the archive's trigger, and one whose message has no
# archive event for 300 s (not archived, or a bot's) gives an event classed
# from the banner text. While the archive is off, or [chat_archive]
# self_user_id is empty, every notification gives a banner-text event at once.
# apps = []
# [events.sent]
# every call of a listed tool from a manager or worker session is stored as a
# sent.chat event by the post-tool-use hook when the tool's response carries
# message_context.message_ts and channel_id; a listed tool whose response
# lacks them stores sent.unparsed; queue it with a rule.
# tools = ["mcp__chat__send_message"]
# [events.sources.exec]
# every_sec = 120
# [[events.sources.exec.commands]]
# argv = ["/path/to/an-on-call-cli", "detect", "--json"]
# every_sec = 120                 # this command's own cadence; defaults to the source's
# while = { kind = "shift" }      # run only inside a window: a stored exec.<kind> event whose
#                                 # payload.start <= now < payload.end, taken from the newest
#                                 # payload.issued set of that kind (match narrows the set)
# stop_file = "~/.claude/dockwright/<lane>-stop"   # while it exists the command is not run
# the daemon runs each argv while an enabled rule names source "exec"; each non-empty
# stdout line is a JSON object {id, kind, match?, summary?, payload?} and becomes an
# exec.<kind> event with id "exec:<id>" (a repeated id within 14 days is dropped, so a
# line printed twice in one second is one event); lines are kept on a non-zero exit, a
# malformed line or a 30 s timeout, and the command is paged as EVENT_POLL_FAILED once it has
# failed 3 runs in a row over 10 minutes of machine-awake time, then hourly.
# [chat_archive]
# A background pass keeps the operator's direct and group conversations and
# the whole threads that mention them on disk under <state_root>/chat for
# retention_days, read through the chat_read, chat_search and
# chat_conversations tools and `dockwright chat-archive`. enabled is forced
# off while mcp_url, mcp_server, keychain_service, write_tools or any of the
# four tool names is empty. tool names are the chat app's MCP tool names
# without the mcp__<server>__ prefix. hand "direct" reads the chat app's MCP
# over HTTP with an OAuth token under the mcpOAuth key mcp_server, from the
# grant Claude Code keeps in the Keychain for every account of [accounts]
# pool: the item keychain_service for the default account, and
# keychain_service-<first 8 hex digits of the sha256 of the account's config
# dir> for each other account. It takes the token with the greatest
# expiresAt. Every account's grant must belong to the same chat user.
# "claude" presses the same read tools through a headless claude -p on model
# (default: models.toml scout_model). runner "launchd" installs the agent
# (dockwright chat-archive sync); "manager" runs the pass from the manager's
# arm-lanes monitor. write_tools lists the app's writing tools, disallowed on
# every claude -p; both claude -p runs drop CLAUDE_CONFIG_DIR and use the
# default account. An empty mention_keys means [self_user_id].
# enabled = false
# hand = "direct"
# runner = "launchd"
# mcp_url = ""
# mcp_server = ""
# keychain_service = ""
# model = ""
# self_user_id = ""
# the text your chat app appends to every message a session sends; a channel
# thread the archive does not track joins it when a notification resolves a
# reply there and the thread holds a message of ours ending with it; empty
# turns that, the reply takeover check and the footer backstop off.
# sent_footer = ""
# mention_keys = []
# retention_days = 90
# every_sec = 300
# group_sweep_sec = 3600
# registry_sec = 86400
# search_overlap_sec = 900
# write_tools = []
# [chat_archive.tools]
# search = ""
# read_conversation = ""
# read_thread = ""
# list_conversations = ""
# [events.rules.<name>]   name: [A-Za-z0-9_-]{1,32}
# source = "tracker"   kind = "sprint-assigned"   action = "start"   schema = "ticket"
# domain = "general"   max_open = 2   types = ["Story", "Bug"]   skip_status = ["Blocked"]
# enabled = true
# source = "github"   kind = "review-wait"   action = "page"   domain = "general"
# source = "exec"   kind = "incident"   action = "queue"   domain = "general"
# source = "chat"   kind = "dm-thread"   action = "reply"
# match = { conversation = "#name" }   stop_file = "~/.claude/dockwright/<rule>-stop"   nudge_sec = 600
# while = { kind = "shift" }
# on_change = "state"   repeat_sec = 3600   repeat_values = ["failed"]   confirm_events = 2
# unless_rules = ["other-rule"]   reply only: an event an enabled listed rule that is not off (stop_file, while)
#   also matches is left to it, unless that queue rule's while keeps it out by its own time
# ack_sec = 600   inbound = true   inbound_skip_senders = ["U…"]   reply only; inbound: kind dm or dm-thread
# action "start" begins a work of <schema> under a live manager of <domain> from
# each new event of (source, kind) while fewer than max_open of its works hold a
# slot (for the tracker sprint rule, every work on a sprint key counts; a work
# holds one while it has an open request, a pending question or a schema or log
# that does not load, or while a token the manager does not hold runs or waits
# on a question or a dialog; a refused start takes none); "page" pages the live
# manager of domain with EVENT_PAGE and stalls like start without one. "queue"
# stores each event in events/queues/<name>.json and reminds the live manager of
# domain with EVENT_QUEUE on a new event and every nudge_sec until event_ack
# closes it; without a manager it stalls like start, and the queue keeps
# filling. match passes only events whose match fields equal it; while stop_file
# exists, or while is set and no stored exec window of that kind covers now, the
# rule moves its cursor and queues, pages or starts nothing; inside its window a
# queue rule with while queues only events whose own time (payload.delivered when
# it is a number, else ts) is at or after the window's start, and before it
# reminds it drops each queued record whose own time is earlier. The window's
# start is the start of the unbroken coverage inside the newest issued set: it
# steps back through that set's earlier-starting windows that reach it; earlier
# sets never move it. dockwright event gate <rule> exits 0 while the rule acts
# (printing that start for a rule with while) and 1 with the reason (disabled,
# error, stop_file, window) while it does not. A rule
# seen for the first time starts its cursor at now. on_change (queue rules only)
# queues an event when its match value differs from the last queued one, or
# again after repeat_sec for a value in repeat_values (empty: any value).
# confirm_events queues a changed value only once that many consecutive events
# of the rule carry it.
# "reply" stores one record in events/queues/<name>.json for each event that
# answers a chat message a session sent (a sent.chat event of the same
# conversation or thread). While the sender is a live worker, each pass types
# one pane line per conversation and thread that names the record ids, until
# the line is typed or the records ripen. event_ack closes a record. A record
# with no ack ripens ack_sec after the pane line, or after it was queued when no
# line was typed: it gets the id <id>:esc, and EVENT_QUEUE reminds the live
# manager of the sender's domain then and every nudge_sec. inbound also stores
# a message in a one-to-one conversation (channel id D…) that answers no
# message of ours, for the manager of domain; a sender in
# inbound_skip_senders, and a top-level message the operator answered himself,
# are dropped when they ripen. types and skip_status
# filter the tracker sprint search, and test a sub-task by its parent. A sprint
# start reads the status live and skips an issue past To Do or in skip_status,
# or a sub-task past To Do; a failed read holds it. A sub-task of yours starts
# alone while its parent is not yours or another sub-task of the parent is
# someone else's or open and unassigned, else the parent starts once. A rule
# missing source, kind or (for start) schema is reported by doctor and never
# runs.

[gardener]
# Skill names treated as "high" complexity by Gardener's task-triage
# heuristics. Default: []. Example:
# high_skills = ["my-plugin:big-task-skill", "my-plugin:multi-step-skill"]
# Extra environment for the selffix retry spawn ONLY, as KEY=VALUE strings in a
# one-line list. Write it as in the example: a bare key under this header.
# SELFFIX_RETRY_ATTEMPT, HOME and PATH are refused. Values may not use TOML
# escape sequences. Only the selffix retro reads DOCKWRIGHT_CLAUDE_BIN, and it
# takes an absolute path. Default: unset. Example:
# retry_env = ["DOCKWRIGHT_CLAUDE_BIN=/path/to/claude-wrapper"]

[agent_vars]
# {{name}} substitutions applied when composing agent files
# (setup.sh / `dockwright compose`).
# tmux_path_prepend = "<absolute-home>/bin" — absolute directories, colon-joined,
# that the dockwright tmux server's global PATH starts with; setup.sh renders it
# into dockwright.tmux.conf. Empty (the default) leaves PATH alone. Use plain
# directory names: a `,` `}` `"` `$` or glob character breaks the conf's tmux
# format, and `dockwright doctor` fails on the PATH that results.

[ceilings]
# Size budget for the render setup.sh is about to deploy — core + your overlay
# drop-ins + your [agent_vars], composed fresh — checked by `dockwright doctor`
# (and so by setup.sh's doctor gate). It does not measure the deployed file,
# which reflects the last compose, not the one about to run.
# Unset (the default) means no budget is enforced and doctor says so.
# The numbers are yours: they bound core + YOUR overlay drop-ins + YOUR
# [agent_vars], so no shipped default could be right for them. The repo's own CI
# bounds the published core separately (tests/test_agent_size_ceiling.py).
# min_dropins and min_agent_vars are FLOORS on the LAYERS, not on the size:
# they catch a render composed with your overlay or your [agent_vars] missing,
# which measures smaller and would otherwise pass every ceiling. They are
# counts, so render growth does not widen them — but OVERLAY growth does, so
# raise a floor when you add drop-ins. Set them just under your real counts: a
# floor of 1 catches only a total wipeout, and a partial loss that leaves one
# drop-in or one var behind renders smaller and still passes.
# "manager.md" = { bytes = 140000, lines = 975, min_dropins = 10, min_agent_vars = 60 }
# "worker.md" = { bytes = 35000, lines = 320, min_dropins = 2 }
# "dockwright-reviewer.md" = { bytes = 7000, lines = 130 }

[tech_debt.systems]
# The systems a tech-debt entry's `- tags:` line may name, as name = definition.
# `dockwright tech-debt tags` prints them beside the four kinds. Left empty, a
# kind alone tags an entry. Example:
# dockwright = "dockwright itself: the engine, the MCP server, hooks, monitors, pipelines, setup.sh, deploy/"

[pricing.rates]
# Per-model [input, output] USD/MTok overrides, merged over the built-in
# table in pricing.py (fable/opus/sonnet/haiku). Example:
# opus = [5.0, 25.0]
# Keys may also be full stripped model ids (e.g. "claude-newfamily-7" = [2.0, 4.0]).
# An id key wins over its family key, and MODEL_RATES in pricing.py keys some
# ids itself (e.g. claude-opus-5-5): override such a model by its id.
'''
