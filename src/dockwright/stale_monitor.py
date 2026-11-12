#!/usr/bin/env python3
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
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from dockwright.terminal import get_driver as _get_driver
    from dockwright.paths import ensure_manager_home as _ensure_manager_home
except Exception:  # pragma: no cover - venv editable install expected in prod
    _get_driver = None
    _ensure_manager_home = None

try:
    from dockwright.pricing import normalize_model as _normalize_model
except Exception:  # pragma: no cover - venv editable install expected in prod
    _normalize_model = None

try:
    from dockwright import lane_lines as _lane_lines
except Exception:  # pragma: no cover - venv editable install expected in prod
    _lane_lines = None

try:
    from dockwright.state import append_event as _append_event
    from dockwright.paths import _safe_segment
except Exception:  # pragma: no cover - venv editable install expected in prod
    _append_event = None
    _safe_segment = None

try:
    from dockwright.manager_launch import manager_rc_args as _manager_rc_args
except Exception:  # pragma: no cover - venv editable install expected in prod
    _manager_rc_args = None

try:
    from dockwright.paths import dockwright_bin as _dockwright_bin
except Exception:  # pragma: no cover - venv editable install expected in prod
    _dockwright_bin = None

try:
    from dockwright import usage_cap as _usage_cap
except Exception:  # pragma: no cover - venv editable install expected in prod
    _usage_cap = None


def _awake_seconds() -> float:
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
PROCESSING_THRESHOLD_SEC = PROCESSING_THRESHOLD_MIN * 60
try:
    _IDLE_HOURS = float(os.environ.get("CLAUDE_ORCH_IDLE_TTL_HOURS", "2"))
except ValueError:
    _IDLE_HOURS = 2.0
IDLE_THRESHOLD_SEC = int(_IDLE_HOURS * 3600)
AUTOCLOSE_CADENCE_SEC = 3600
BUSY_SHELL_MARKER = "shell-snapshots/snapshot-"
BUSY_SHELL_IDLE_MULTIPLIER = 3
AUTOCLOSE_SKEW_CADENCES = 2
WORKERS_SESSION_NAME = "claude-workers"
ORPHAN_GRACE_SEC = _env_positive_int("CLAUDE_ORCH_ORPHAN_GRACE_SEC", 120)

TURN_END_GRACE_SEC_DEFAULT = 120
TURN_END_GRACE_ENV = "CLAUDE_ORCH_TURN_END_GRACE_SEC"


def _turn_end_grace_sec() -> int:
    try:
        value = int(os.environ.get(TURN_END_GRACE_ENV, str(TURN_END_GRACE_SEC_DEFAULT)))
    except ValueError:
        return TURN_END_GRACE_SEC_DEFAULT
    return value if value >= 0 else TURN_END_GRACE_SEC_DEFAULT

GARDENER_WINDOW_PROTECT_TTL_SEC = _env_positive_int(
    "CLAUDE_ORCH_GARDENER_WINDOW_PROTECT_TTL_SEC", 7200)

APPROVAL_QUESTION_MARKERS = (
    "do you want to proceed?",
    "requires approval",
    "do you trust the files in this folder",
    "is this a project you created or one you trust",
    "do you want to make this edit",
)
APPROVAL_OPTION_MARKERS = ("❯ 1.", "1. yes")
APPROVAL_TAIL_LINES = 40
APPROVAL_EXCERPT_MAX = 160
MARKER_SUMMARY_MAX = 2000
TRANSCRIPT_TAIL_BYTES = (65_536, 1_048_576)
CONVERSATION_RECORD_TYPES = ("user", "assistant", "attachment", "system", "api-request", "api-request-shape")
DIALOG_RULE_CHAR = "╌"
DIALOG_RULE_EXTRA_CHARS = " \t│─"
DIALOG_BORDER_CHARS = set("─╭╮╰╯│ \t")
DIALOG_TITLES = ("Bash command", "Edit file", "Create file", "Fetch", "Edit notebook", "Tool use")
MARKER_MATCH_WINDOW_SEC = 5
MARKER_MATCH_ASSISTANT_RECORDS = 10
MARKER_CLOSE_GRACE_SEC = 2
MARKER_HINT_GRACE_SEC = 30
SELF_EDIT_OPTION_TEXT = "allow Claude to edit files in its ~/.claude folder"
SAFEGUARD_STOP_SIGNATURES = ("stopped by a safety classifier", "safeguards stopped the response")
SAFEGUARD_EVENT_TYPES = ("user", "assistant", "system")
SAFEGUARD_TITLE_MARKER = "session paused"
SAFEGUARD_EDIT_OPTION_RE = re.compile(r"(\d)\.\s+Edit prompt and retry", re.IGNORECASE)
SAFEGUARD_SELECTOR_RE = re.compile(r"❯\s*\d\.\s+(?:Switch to|Edit prompt and retry)", re.IGNORECASE)
SAFEGUARD_OPTION_SPAN = 2
SAFEGUARD_CONTINUE_TEXT = "continue"
SAFEGUARD_RETRY_WINDOW_SEC = 1800
SAFEGUARD_SETTLE_POLLS = 10
SAFEGUARD_SETTLE_STEP_SEC = 0.5

LIVE_TURN_RE = re.compile(r"…\s*\((?:\d+d\s*)?(?:\d+h\s*)?(?:\d+m\s*)?\d+s\b")
LIVE_TURN_TAIL_LINES = 15

HOME = Path(os.environ.get("HOME", ""))


def _prefer_new(new: Path, legacy: Path) -> Path:
    if new.exists():
        return new
    if legacy.exists():
        return legacy
    return new


_ENV_STATE_DIR = os.environ.get("DOCKWRIGHT_STATE_DIR", "").strip()
ROOT = (Path(_ENV_STATE_DIR) if _ENV_STATE_DIR
        else _prefer_new(HOME / ".claude" / "dockwright", HOME / ".claude" / "orchestrator"))
_LEGACY_ROOT = HOME / ".claude" / "orchestrator"
ACTIVE = ROOT / "active"
QUESTIONS = ROOT / "questions"
CLOSED = ROOT / "closed"
ASSIGNMENTS_PENDING = ROOT / "assignments" / ".pending"
ASSIGNMENTS = ROOT / "assignments"
PANE_SENDS = ROOT / "pane-sends"
PERMISSION_PROMPTS = ROOT / "permission-prompts"
PERMISSION_LEDGER = ROOT / "permission-answers.jsonl"
CHILD_ROUND_RE = re.compile(r"/children/(?P<aid>[A-Za-z0-9_-]+)/r(?P<n>\d+)\.")
CHILD_ROUND_FOREIGN_ROOT_RE = re.compile(r"/\S*$")
GARDENER_LIVE_WINDOWS = ROOT / "gardener" / "live-windows"
USAGE_CONTEXT = _prefer_new(HOME / ".claude" / "dockwright" / "usage" / "context",
                            HOME / ".claude" / "orchestrator" / "usage" / "context")
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CODEX_SESSIONS = Path(os.environ.get("CODEX_HOME", "").strip() or (HOME / ".codex")).expanduser() / "sessions"
RATE_LIMIT_SIGNATURES = ("temporarily limiting requests", "hit your session limit")
MODEL_CREDIT_SIGNATURES = ("reached your", "/usage-credits")
HOLD_SIGNATURES = ("account is on hold",)
MODEL_CREDIT_SYNTHETIC_MODEL = "<synthetic>"
MODEL_CREDIT_SCAN_BYTES = 16 * 1024 * 1024
MODEL_FALLBACK_CONFIRM_SEC = _env_positive_int(
    "CLAUDE_ORCH_MODEL_FALLBACK_CONFIRM_SEC", 600)
MODEL_FALLBACK_MODEL_TOKEN = os.environ.get(
    "CLAUDE_ORCH_MODEL_FALLBACK", "").strip() or "@sonnet"
MODEL_FALLBACK_EFFORT = os.environ.get(
    "CLAUDE_ORCH_MODEL_FALLBACK_EFFORT", "").strip() or "high"
MANAGER_MODEL_FALLBACK_MODEL_TOKEN = "@opus[1m]"
MANAGER_MODEL_FALLBACK_EFFORT = "xhigh"
FABLE_FAMILY = "fable"
FABLE_FALLBACK_TOKEN = "@opus"
FABLE_FALLBACK_EFFORT = "xhigh"
FABLE_MOVE_PID_WAIT_SEC = 15
FABLE_MOVE_PID_POLL_SEC = 0.5
FABLE_MOVE_MAX_ATTEMPTS = 3
FABLE_MOVE_KEEP_SEC = 86400
FABLE_MOVE_SUCCESS_STATUSES = ("delivered", "replaced", "replace_pending")


def _resolve_model_token(value):
    if not value.startswith("@"):
        return value
    try:
        out = subprocess.run(
            [sys.executable, "-m", "dockwright", "model", "resolve", value],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    resolved = out.stdout.strip()
    return resolved if out.returncode == 0 and resolved else None


@lru_cache(maxsize=None)
def worker_fallback_model():
    return _resolve_model_token(MODEL_FALLBACK_MODEL_TOKEN)


@lru_cache(maxsize=None)
def manager_fallback_model():
    return _resolve_model_token(MANAGER_MODEL_FALLBACK_MODEL_TOKEN)


@lru_cache(maxsize=None)
def fable_fallback_model():
    return _resolve_model_token(FABLE_FALLBACK_TOKEN)


TRANSIENT_THROTTLE_SIGNATURES = ("temporarily limiting requests", "not your usage limit")
TRANSIENT_SERVER_ERROR_SIGNATURES = ("529 overloaded",)
AUTH_FAILURE_SIGNATURES = ("api error: 401", "please run /login")
AUTH_401_WINDOW_SEC = 5 * 60
AUTH_401_MAX_ATTEMPTS = 2
AUTH_401_SAME_ACCOUNT_ATTEMPTS = 1
AUTH_401_REEMIT_SEC = 5 * 60
CONTEXT_PAGE_STEPS = (70, 80, 90)
AUTONUDGE = os.environ.get("CLAUDE_ORCH_AUTONUDGE") == "1"
HUMAN_ATTACHED_TTL_SEC = _env_positive_int("CLAUDE_ORCH_HUMAN_ATTACHED_TTL_SEC", 40 * 60)
AUTOMATION_STAMP_TTL_SEC = _env_positive_int(
    "CLAUDE_ORCH_AUTOMATION_STAMP_TTL_SEC", 1800)
AUTOMATION_STAMP_MAX = 8
HANDBACK_TOKEN = "handback"
OUTBOX_MAX_HOLD_SEC = _env_positive_int("CLAUDE_ORCH_OUTBOX_MAX_HOLD_SEC", 1800)
NUDGE_TEXT = "[MANAGER] resume your task"
IDLE_UNTASKED_MIN = _env_positive_int("CLAUDE_ORCH_IDLE_UNTASKED_MIN", 45)
IDLE_UNTASKED_SEC = IDLE_UNTASKED_MIN * 60
IDLE_UNTASKED_BUSY_MIN = _env_positive_int("CLAUDE_ORCH_IDLE_UNTASKED_BUSY_MIN", 120)
IDLE_UNTASKED_BUSY_SEC = IDLE_UNTASKED_BUSY_MIN * 60
IDLE_UNTASKED_MAX_SENDS = _env_positive_int(
    "CLAUDE_ORCH_IDLE_UNTASKED_MAX_SENDS", 3)
IDLE_NUDGE_TEXT = ("[MANAGER] idle with no new instruction — if your task is "
                   "unfinished, continue it; if it is finished, call worker_done "
                   "and stop.")
MANAGER_NUDGE_TEXT = "rate limit cleared — check list_workers and queued events, resume orchestration"
def _manager_model_fallback_resume_text(landed: str, reported: str | None) -> str:
    banner = f" The banner named {reported}." if reported else ""
    return (f"the lane typed /model {landed} into this pane after a credit banner.{banner} The "
            f"banner is about THIS session's model, not the fleet's: workers spawn on "
            f"the --model each dispatch names and this does not change it. Anything you dispatch on the "
            f"spent model fails the same way. Check list_workers and queued "
            f"events, then resume orchestration")


RATE_LIMIT_NUDGE_MIN = 5
RATE_LIMIT_NUDGE_SEC = RATE_LIMIT_NUDGE_MIN * 60
NUDGE_REPEAT_INTERVAL_MIN = 60
SCHEDULED_NUDGE_DELAY_SEC = 120
MAX_PLAUSIBLE_RESET_SEC = 6 * 3600
MAX_BANNER_LEN = 200
MAX_BANNER_SIG_OFFSET = 12
MANAGER_NUDGE_RETRY_SEC = 10 * 60
MANAGER_LIMIT_CHECK_FLOOR_SEC = 120
_RESET_CLAUSE_RE = re.compile(
    r"resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*\(([^)]+)\)", re.IGNORECASE)
ACCOUNT_ACTIVE = ROOT / "account-active"
ACCOUNT_LEDGER = ROOT / "account-flips.jsonl"
ACCOUNT_STATE = ROOT / "account-state.json"
ACCOUNT_LOCK = ROOT / ".account-flip.lock"
FLIP_COOLDOWN_SEC = _env_positive_int("CLAUDE_ORCH_FLIP_COOLDOWN_MIN", 30) * 60
TAKEOVER_GUARD_SEC = 300
BRICK_EPISODE_GAP_SEC = 600
ACCOUNT_REGISTRY = ROOT / "account-registry.json"
_LEGACY_REGISTRY = (["a", "b"], "a", {})


def _registry():
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
    try:
        letter = ACCOUNT_ACTIVE.read_text().rstrip("\n")
    except Exception:
        return None
    return letter if letter in _registry()[0] else None


def _account_of(record: dict, pool_letter: str) -> str:
    stamped = record.get("account")
    return stamped if stamped in _registry()[0] else pool_letter


def _keychain_unlocked() -> bool:
    try:
        return subprocess.run(["security", "show-keychain-info"],
                              capture_output=True, timeout=5, check=False).returncode == 0
    except Exception:
        return False


def _account_effective(letter: str) -> tuple[str, Path | None]:
    _names, default, dirs = _registry()
    if letter == default:
        return letter, None
    farm = Path(dirs.get(letter) or os.path.expanduser(f"~/.claude-{letter}"))
    try:
        data = json.loads((farm / ".claude.json").read_text())
        servers = (data.get("mcpServers") or {}) if isinstance(data, dict) else {}
        if "dockwright" in servers or "claude-orchestrator" in servers:
            return letter, farm
    except Exception:
        pass
    print(f"stale_monitor: account-{letter} farm {farm}/.claude.json "
          f"not healthy; recovery falls back to the DEFAULT login (stamp "
          f"{default}) — the recovery tab may land on the bricked account "
          f"until a worker rebuilds the farm", file=sys.stderr)
    return default, None


def _format_account_prefix(effective: str, config_dir: Path | None) -> str:
    parts = []
    if config_dir is not None:
        parts.append(f"CLAUDE_CONFIG_DIR={shlex.quote(str(config_dir))}")
    parts.append(f"CLAUDE_ORCH_ACCOUNT={shlex.quote(effective)}")
    return " ".join(parts) + " "


def _login_fix_command(letter: str) -> str:
    _names, default, dirs = _registry()
    if letter == default:
        return "claude"
    farm = dirs.get(letter) or os.path.expanduser(f"~/.claude-{letter}")
    return f"CLAUDE_CONFIG_DIR={shlex.quote(str(farm))} claude"


def _notify_macos(message: str) -> None:
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
def _flip_lock(lock_path: Path | None = None):
    lock_path = ACCOUNT_LOCK if lock_path is None else lock_path
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def _load_account_state(path: Path | None = None) -> dict:
    state = _load(ACCOUNT_STATE if path is None else path) or {}
    if not isinstance(state.get("accounts"), dict):
        state["accounts"] = {}
    return state


def _append_account_ledger(entry: dict, path: Path | None = None) -> None:
    path = ACCOUNT_LEDGER if path is None else path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        print(f"stale_monitor: account ledger append failed ({e})", file=sys.stderr)


def permission_summary(tool_input) -> str:
    if isinstance(tool_input, dict):
        for key in ("command", "file_path", "url"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                return value[:MARKER_SUMMARY_MAX]
    return json.dumps(tool_input, sort_keys=True)[:MARKER_SUMMARY_MAX]


def marker_lock(prompts_dir: Path, sid: str):
    return pane_send_lock(prompts_dir, sid)


def _append_permission_ledger(entry: dict, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        print(f"stale_monitor: permission ledger append failed ({e})", file=sys.stderr)


def _entry_bricked(entry, now: int, held: bool = False) -> bool:
    if held:
        return True
    if not isinstance(entry, dict):
        return False
    reset_ts = entry.get("reset_ts")
    if isinstance(reset_ts, (int, float)):
        return now < reset_ts
    bricked_at = entry.get("bricked_at")
    return isinstance(bricked_at, (int, float)) and now - bricked_at < MAX_PLAUSIBLE_RESET_SEC


def _other_account_bricked(state: dict, other: str, now: int) -> bool:
    return _entry_bricked(state.get("accounts", {}).get(other), now, held=other in _held_marks(state))


def _held_marks(state: dict) -> dict:
    held = state.get("held")
    return held if isinstance(held, dict) else {}


def _auth_401_active(account: str, now: int, state: dict | None = None) -> bool:
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
    for name in _registry()[0]:
        if (name != pointer
                and not _entry_bricked(state.get("accounts", {}).get(name), now,
                                       held=name in _held_marks(state))
                and not _auth_401_active(name, now, state)):
            return name
    return None


def _record_hold(account: str, reason: str, source: str, now: int) -> bool:
    try:
        with _flip_lock():
            state = _load_account_state()
            held = _held_marks(state)
            mark = held.get(account)
            if mark is None:
                mark = {"at": now, "reason": reason, "source": source}
            elif reason == "banner" and mark.get("reason") != "banner":
                mark = {**mark, "reason": reason, "source": source, "banner_at": now}
            else:
                return False
            held[account] = mark
            state["held"] = held
            _write_json_atomic(ACCOUNT_STATE, state)
            _append_account_ledger({"ts": now, "event": "hold", "account": account,
                                    "reason": reason, "source": source, "by": "stale_monitor"})
            return True
    except Exception as e:
        print(f"stale_monitor: hold recording failed ({e})", file=sys.stderr)
        return False


def _held_account_of(record: dict) -> str | None:
    names, default, _dirs = _registry()
    if not names:
        return None
    stamped = record.get("account")
    return stamped if isinstance(stamped, str) and stamped else default


def _record_brick(account: str, reset_ts, source: str, now: int) -> None:
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
            _write_json_atomic(ACCOUNT_STATE, state)
            if new_episode:
                _append_account_ledger({"ts": now, "event": "brick", "account": account,
                                        "reset_ts": reset_ts, "source": source,
                                        "by": "stale_monitor"})
    except Exception as e:
        print(f"stale_monitor: brick recording failed ({e})", file=sys.stderr)


def record_model_spent(account: str, model: str, at: float, state_path: Path, lock_path: Path,
                       ledger_path: Path, by: str = "stale_monitor") -> None:
    try:
        with _flip_lock(lock_path):
            state = _load_account_state(state_path)
            spent = state.get("model_spent")
            spent = spent if isinstance(spent, dict) else {}
            entry = spent.get(account)
            entry = entry if isinstance(entry, dict) else {}
            prev = entry.get(model)
            if isinstance(prev, (int, float)) and prev >= at:
                return
            entry[model] = at
            spent[account] = entry
            state["model_spent"] = spent
            _write_json_atomic(state_path, state)
        _append_account_ledger({"ts": int(at), "event": "model-spent", "account": account,
                                "model": model, "by": by}, ledger_path)
    except Exception as e:
        print(f"stale_monitor: model-spent recording failed ({e})", file=sys.stderr)


def _record_model_spent(account: str, model: str, at: float) -> None:
    record_model_spent(account, model, at, ACCOUNT_STATE, ACCOUNT_LOCK, ACCOUNT_LEDGER)


def _record_auth_401(account: str, uuid: str | None, now: int) -> tuple[str, int]:
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
    if new_letter is not None:
        return new_letter
    if pool != suspect and not pool_suspect:
        return pool
    return None


def _maybe_flip_account(bricked_account: str, reason: str, now: int, cooldown: bool = True) -> str | None:
    try:
        with _flip_lock():
            pointer = _pool_account()
            if pointer is None or pointer != bricked_account:
                return None
            state = _load_account_state()
            last_flip = state.get("last_flip") or {}
            last_ts = last_flip.get("ts")
            if cooldown and isinstance(last_ts, (int, float)) and now - last_ts < FLIP_COOLDOWN_SEC:
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
    key = f"{event}:{hashlib.sha1(banner.encode('utf-8', 'replace')).hexdigest()[:12]}"
    if key not in emitted:
        _append_account_ledger({"ts": now, "event": event,
                                "text": banner[:200], "source": source,
                                "by": "stale_monitor"})
    next_emitted[key] = now


def _interactive_shell() -> str:
    sh = os.environ.get("SHELL", "")
    if os.path.basename(sh) in ("zsh", "bash") and shutil.which(sh):
        return sh
    for cand in ("zsh", "bash"):
        found = shutil.which(cand)
        if found:
            return found
    return "sh"


def _dockwright_launch_prefix() -> str:
    if _dockwright_bin is not None:
        return f"{shlex.quote(str(_dockwright_bin()))} launch "
    env_bin = os.environ.get("DOCKWRIGHT_BIN", "")
    if env_bin:
        return f"{shlex.quote(env_bin)} launch "
    print("stale_monitor: no dockwright binary; the recovery manager starts without --agents", file=sys.stderr)
    return ""


def _without_agents_file(args: list) -> list:
    out: list = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg == "--agents-file":
            skip = True
            continue
        if isinstance(arg, str) and arg.startswith("--agents-file="):
            continue
        out.append(arg)
    return out


def _launch_recovery_manager(mgr_record: dict, mgr_sid: str, new_letter: str) -> str | None:
    name = mgr_record.get("name") or ""
    recovery_model = manager_fallback_model()
    if not recovery_model:
        print("stale_monitor: recovery launch refused (model resolve failed)", file=sys.stderr)
        return None
    if _manager_rc_args is None:
        print("stale_monitor: recovery launch skipped (dockwright.manager_launch unavailable)",
              file=sys.stderr)
        return None
    effective, config_dir = _account_effective(new_letter)
    launcher = _dockwright_launch_prefix()
    rc_list = _manager_rc_args(effective)
    if not launcher:
        rc_list = _without_agents_file(rc_list)
    rc_args = shlex.join(rc_list) + " "
    skip_arg = ("--dangerously-skip-permissions "
                if os.environ.get("DOCKWRIGHT_MANAGER_SKIP_PERMS", "") == "1"
                else "")
    inner = (
        f"{_format_account_prefix(effective, config_dir)}"
        f"CLAUDE_AGENT=manager CLAUDE_WORKER_NAME={shlex.quote(name)} "
        f"DOCKWRIGHT_PENDING_TAKEOVER=1 "
        f"{launcher or 'claude '}{rc_args}{skip_arg}--model {shlex.quote(recovery_model)} "
        f"--effort {shlex.quote(MANAGER_MODEL_FALLBACK_EFFORT)} "
        f"{shlex.quote(f'/manager-takeover-recovery {mgr_sid}')}"
    )
    os.environ.pop("DOCKWRIGHT_MANAGER_SKIP_PERMS", None)
    if _get_driver is None or _ensure_manager_home is None:
        print("stale_monitor: recovery launch skipped (driver unavailable)", file=sys.stderr)
        return None
    try:
        cwd = str(_ensure_manager_home())
    except ValueError as e:
        print(f"stale_monitor: recovery launch refused ({e})", file=sys.stderr)
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
    bucket = name.replace("/", "_").replace("\\", "_")
    return f"_{bucket}" if bucket in (".", "..") else bucket


EMITTED_STATE_PREFIX = ".stale-emitted"


def _emitted_state_path(manager_name: str | None) -> Path:
    if not manager_name:
        return ROOT / f"{EMITTED_STATE_PREFIX}.json"
    return ROOT / f"{EMITTED_STATE_PREFIX}-{_safe_bucket(manager_name)}.json"


def _parent_name_of(record) -> str | None:
    if not isinstance(record, dict):
        return None
    name = record.get("parent_name")
    if name is not None:
        return name
    return record.get("parent_manager_name")


def _matches_manager(record: dict, manager_name: str | None) -> bool:
    if manager_name is None:
        return True
    return _parent_name_of(record) == manager_name


def _pid_alive(pid) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _load_active_records() -> list:
    out = []
    if ACTIVE.is_dir():
        for p in ACTIVE.iterdir():
            if p.suffix != ".json":
                continue
            record = _load(p)
            if record is not None:
                out.append((p, record))
    return out


def _is_subworker(record) -> bool:
    return isinstance(record, dict) and record.get("kind") == "subworker" and not record.get("nested")


def _is_interactive_child(record) -> bool:
    return (_is_subworker(record) and (record.get("runtime") or "claude") == "claude"
            and record.get("provider") == "claude-tui" and bool(record.get("window_id")))


def _live_record_named(name, records) -> dict | None:
    if not name:
        return None
    for record in records:
        if record.get("nested") or record.get("name") != name:
            continue
        if _pid_alive(record.get("pid")):
            return record
    return None


def _live_children_of(name, records) -> list:
    if not name:
        return []
    return [r for r in records
            if _is_subworker(r) and _parent_name_of(r) == name and _pid_alive(r.get("pid"))]


def _closed_record_named(name) -> dict | None:
    best = None
    if not name:
        return None
    for _stem, record in _dir_json(CLOSED):
        if record.get("name") != name:
            continue
        if best is None or _safe_int(record.get("closed_at")) > _safe_int(best.get("closed_at")):
            best = record
    return best


def _owning_manager(record, records) -> str | None:
    if not _is_subworker(record):
        return _parent_name_of(record)
    parent_name = _parent_name_of(record)
    if not parent_name:
        return None
    parent = next((r for r in records if not r.get("nested") and r.get("name") == parent_name), None)
    if parent is None:
        parent = _closed_record_named(parent_name)
    if parent is None:
        return None
    return _parent_name_of(parent)


DAEMON_PARENT = "daemon"
DAEMON_HEARTBEAT_STALE_SEC = 180


def _daemon_alive() -> bool:
    if (ROOT / "daemon-stop").exists():
        return False
    beat = _load(ROOT / "daemon" / "alive.json")
    ts = beat.get("ts") if isinstance(beat, dict) else None
    return isinstance(ts, (int, float)) and time.time() - ts <= DAEMON_HEARTBEAT_STALE_SEC


def _live_manager_named(name, records) -> bool:
    if name == DAEMON_PARENT:
        return _daemon_alive()
    return any(r.get("agent") == "manager" and r.get("name") == name and not r.get("nested")
               and _pid_alive(r.get("pid")) for r in records)


def _in_scope(record, manager_name, records) -> bool:
    if manager_name is None:
        return True
    if not _is_subworker(record):
        return _parent_name_of(record) == manager_name
    owner = _owning_manager(record, records)
    if owner is None or owner == manager_name:
        return True
    return not _live_manager_named(owner, records)


def _children_activity(record, records, codex_log_cache=None, seen_codex_sids=None) -> int:
    newest = 0
    for child in _live_children_of(record.get("name"), records):
        sid = child.get("claude_sid")
        if not sid:
            continue
        if seen_codex_sids is not None and (child.get("runtime") or "claude") == "codex":
            seen_codex_sids.add(sid)
        try:
            newest = max(newest, int((ACTIVE / f"{sid}.json").stat().st_mtime))
        except OSError:
            pass
        assignment = _load(ASSIGNMENTS / f"{sid}.json") or {}
        log_path = assignment.get("log_path")
        if isinstance(log_path, str) and log_path:
            try:
                newest = max(newest, _activity_of_log(Path(log_path)))
            except OSError:
                pass
        newest = max(newest, _last_activity(child, 0, codex_log_cache)[0])
    return newest


def _read_transcript_tail(log: Path, stop):
    try:
        size = log.stat().st_size
        covered = size
        with open(log, "rb") as f:
            for chunk in (*TRANSCRIPT_TAIL_BYTES, size):
                start = max(0, size - chunk)
                if start >= covered:
                    continue
                f.seek(start)
                data = f.read(covered - start)
                lines = data.split(b"\n")
                region_start = start
                if start > 0:
                    region_start = start + len(lines[0]) + 1
                    lines = lines[1:]
                for raw in reversed(lines):
                    try:
                        record = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    yield record
                    if stop(record):
                        return
                covered = region_start
                if start == 0:
                    return
    except OSError:
        return


def _transcript_activity_ts(log: Path) -> int | None:
    def _conversation(record: dict) -> bool:
        return record.get("type") in CONVERSATION_RECORD_TYPES and _parse_iso(record.get("timestamp")) is not None

    for record in _read_transcript_tail(log, _conversation):
        if _conversation(record):
            return int(_parse_iso(record["timestamp"]))
    return None


def _activity_of_log(log: Path) -> int:
    ts = _transcript_activity_ts(log)
    return ts if ts is not None else int(log.stat().st_mtime)


def _load(p: Path) -> dict | None:
    try:
        return json.load(open(p))
    except Exception:
        return None


_DIR_MEMO: dict | None = None


def _dir_json(directory: Path):
    memo = _DIR_MEMO
    if memo is None:
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        for p in entries:
            if p.suffix != ".json":
                continue
            record = _load(p)
            if record is not None:
                yield p.stem, record
        return
    try:
        mtime_ns = os.stat(directory).st_mtime_ns
    except OSError:
        return
    saved = memo.get(directory)
    if saved is None or saved[0] != mtime_ns:
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        texts = []
        for p in entries:
            if p.suffix != ".json":
                continue
            try:
                with open(p) as fh:
                    text = fh.read()
                if json.loads(text) is not None:
                    texts.append((p.stem, text))
            except Exception:
                continue
        saved = memo[directory] = (mtime_ns, texts)
    for stem, text in saved[1]:
        yield stem, json.loads(text)


def _parse_iso(s) -> float | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
    if elapsed_min < base_min:
        return None
    t = base_min
    while t * 2 <= elapsed_min:
        t *= 2
    return t


def _highest_nudge_threshold(elapsed_min: int, base_min: int) -> int | None:
    if elapsed_min < base_min:
        return None
    cap = base_min * 4
    if elapsed_min < cap:
        return _highest_threshold(elapsed_min, base_min)
    extra_steps = (elapsed_min - cap) // NUDGE_REPEAT_INTERVAL_MIN
    return cap + extra_steps * NUDGE_REPEAT_INTERVAL_MIN


def _pending_question_times() -> dict:
    asked: dict = {}
    if not QUESTIONS.is_dir():
        return asked
    for p in QUESTIONS.rglob("*.json"):
        record = _load(p)
        if record is None or record.get("asked_by") == "manager":
            continue
        sid = record.get("worker_sid")
        if not sid:
            continue
        times = asked.setdefault(sid, [])
        at = record.get("asked_at")
        if isinstance(at, (int, float)) and not isinstance(at, bool):
            times.append(float(at))
    return {sid: max(times, default=None) for sid, times in asked.items()}


def _pending_question_sids() -> set:
    return set(_pending_question_times())


def _input_bound(record: dict) -> float:
    stamps = [record.get("tasked_at"), record.get("input_at")] if isinstance(record, dict) else []
    return max((s for s in stamps if isinstance(s, (int, float)) and not isinstance(s, bool)),
               default=0.0)


def _reported_since_input(record: dict, sid: str, root: Path | None = None) -> bool:
    if not sid:
        return False
    tasked = _input_bound(record)
    parent = _parent_name_of(record)
    done_dir = (ROOT if root is None else root) / "done" / (_safe_bucket(parent) if parent else "_unscoped")
    try:
        entries = list(done_dir.glob(f"{sid}-*.json"))
    except OSError:
        return False
    for entry in entries:
        done = _load(entry)
        completed = done.get("completed_at") if isinstance(done, dict) else None
        if isinstance(completed, (int, float)) and not isinstance(completed, bool) and completed > tasked:
            return True
    return False


def _close_window(window_id: str) -> None:
    if not window_id or _get_driver is None:
        return
    try:
        _get_driver().close(window_id)
    except Exception:
        pass


_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")


def _strip_ansi(text: str) -> str:
    return _SGR_RE.sub("", text)


def _strip_dim_spans(text: str) -> str:
    out: list[str] = []
    pos = 0
    faint = False
    for m in _SGR_RE.finditer(text):
        if not faint:
            out.append(text[pos:m.start()])
        for param in m.group(1).split(";"):
            if param == "2":
                faint = True
            elif param in ("", "0", "22"):
                faint = False
        pos = m.end()
    if not faint:
        out.append(text[pos:])
    return "".join(out)


def _input_state(screen_text: str | None) -> str:
    if not screen_text:
        return "no capture"
    queued = "Press up to edit queued messages" in _strip_ansi(screen_text)
    caret_lines = [line for line in screen_text.splitlines() if "❯" in line]
    if not caret_lines:
        return "no prompt"
    after_caret = caret_lines[-1].split("❯", 1)[1]
    after_caret = _strip_dim_spans(after_caret)
    after_caret = after_caret.strip().strip("│|").strip()
    if after_caret != "":
        return "draft"
    return "queued" if queued else "idle"


def _input_is_idle(screen_text: str | None) -> bool:
    return _input_state(screen_text) == "idle"


def _pane_input_idle(window_id: str, memo: dict | None = None,
                     runtime: str = "claude") -> bool:
    if (runtime or "claude") != "claude":
        return True
    if not window_id or _get_driver is None:
        return True
    cache = memo.get("input") if memo is not None else None
    if cache is not None and window_id in cache:
        return cache[window_id]
    try:
        capture = getattr(_get_driver(), "capture_screen_ansi", None)
    except Exception:
        capture = None
    if capture is None:
        return True
    try:
        screen = capture(window_id)
    except Exception:
        screen = None
    verdict = _input_is_idle(screen)
    if cache is not None:
        cache[window_id] = verdict
    return verdict


def _live_stamps(path: Path, now: float) -> list:
    data = _load(path)
    pending = data.get("pending") if isinstance(data, dict) else None
    if not isinstance(pending, list):
        return []
    return [s for s in pending
            if isinstance(s, (int, float)) and not isinstance(s, bool)
            and now - s < AUTOMATION_STAMP_TTL_SEC]


@contextmanager
def pane_send_lock(root: Path, sid: str):
    """Four processes read-modify-write this file; an unlocked write loses stamps,
    and a lost stamp arms the mode on a worker nobody is at."""
    root.mkdir(parents=True, exist_ok=True)
    lock = root / f"{sid}.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _stamp_pane_send(sid: str, now: float | None = None, root: Path | None = None) -> None:
    if not sid:
        return
    now = time.time() if now is None else now
    root = PANE_SENDS if root is None else root
    path = root / f"{sid}.json"
    try:
        with pane_send_lock(root, sid):
            pending = (_live_stamps(path, now) + [now])[-AUTOMATION_STAMP_MAX:]
            _write_json_atomic(path, {"pending": pending})
    except Exception:
        pass


def is_human_attached(record: dict, now: float | None = None) -> bool:
    if not isinstance(record, dict):
        return False
    stamp = record.get("human_attached_at")
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or stamp <= 0:
        return False
    now = time.time() if now is None else now
    return (now - stamp) < HUMAN_ATTACHED_TTL_SEC


HOOKLESS_PREFIXES = ("/model ", "/effort ")


def _stampable(text: str) -> bool:
    """Default-deny: a send is stamped unless it is one of the built-in control
    commands this lane types itself, which fire no UserPromptSubmit. A custom or
    skill slash command DOES fire one, so shape alone cannot decide this."""
    return not text.lstrip().startswith(HOOKLESS_PREFIXES)


def _send_text_checked(window_id: str, text: str, memo: dict | None = None,
                       runtime: str = "claude", sid: str | None = None) -> bool:
    if not window_id or _get_driver is None:
        return False
    if not _pane_input_idle(window_id, memo, runtime):
        return False
    try:
        sent = bool(_get_driver().send_text_checked(window_id, text))
    except Exception:
        return False
    if sent and sid and _stampable(text):
        _stamp_pane_send(sid)
    return sent


def _send_text(window_id: str, text: str, memo: dict | None = None,
               runtime: str = "claude", sid: str | None = None) -> bool:
    """Delegates to the checked send so the stamp records a CONFIRMED submit, not
    an attempt. `TmuxDriver.send_text` swallows its own failures, and a stamp with
    no submit behind it eats the engineer's next message."""
    return _send_text_checked(window_id, text, memo, runtime, sid)


def _pane_shows_live_turn(window_id: str, memo: dict | None = None) -> bool:
    if not window_id or _get_driver is None:
        return False
    seen = memo.get("seen") if memo is not None else None
    if seen is not None and window_id in seen:
        line = seen[window_id]
    else:
        try:
            text = _get_driver().capture_screen(window_id)
        except Exception:
            text = None
        tail = [ln for ln in (text or "").splitlines() if ln.strip()][-LIVE_TURN_TAIL_LINES:]
        line = next((ln for ln in reversed(tail) if LIVE_TURN_RE.search(ln)), None)
        if seen is not None:
            seen[window_id] = line
    if line is None:
        return False
    prev = (memo.get("prev") if memo is not None else None) or {}
    return line != prev.get(window_id)


def _claude_pane_of(record: dict) -> str:
    if (record.get("runtime") or "claude") != "claude":
        return ""
    return record.get("window_id") or record.get("iterm_sid") or ""


def _find_claude_session_log(sid: str) -> Path | None:
    if not sid or not CLAUDE_PROJECTS.is_dir():
        return None
    for project_dir in CLAUDE_PROJECTS.iterdir():
        candidate = project_dir / f"{sid}.jsonl"
        if candidate.is_file():
            return candidate
    return None


def _find_codex_session_log(sid: str) -> Path | None:
    if not sid or not CODEX_SESSIONS.is_dir():
        return None
    matches = sorted(
        CODEX_SESSIONS.rglob(f"rollout-*-{sid}.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def _resolve_transcript_path(record: dict, codex_log_cache: dict | None = None,
                             claude_log_cache: dict | None = None) -> Path | None:
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
    memo = (claude_log_cache or {}).get(sid)
    if isinstance(memo, str) and memo:
        memo_path = Path(memo)
        if memo_path.is_file():
            return memo_path
    found = _find_claude_session_log(sid)
    if found is not None and claude_log_cache is not None:
        claude_log_cache[sid] = str(found)
    return found


def _latest_subagent_mtime(log: Path, sid: str) -> float:
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


def _is_transcript_live(record: dict, now: float | None = None) -> bool:
    try:
        path = record.get("transcript_path")
        if not isinstance(path, str) or not path:
            return False
        mtime = Path(path).stat().st_mtime
        if now is None:
            now = time.time()
        return now - mtime < _turn_end_grace_sec()
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _transcript_unreadable(record: dict) -> bool:
    try:
        path = record.get("transcript_path")
        if not isinstance(path, str) or not path:
            return False
        Path(path).stat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    except (AttributeError, TypeError, ValueError):
        return False
    return False


def _is_delegation_live(record: dict, log: Path | None = None, records=None) -> bool:
    try:
        if records is None:
            records = [r for _, r in _load_active_records()]
        if _live_children_of(record.get("name"), records):
            return True
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
    if IDLE_THRESHOLD_SEC <= 0:
        return IDLE_THRESHOLD_SEC
    return max(IDLE_THRESHOLD_SEC * BUSY_SHELL_IDLE_MULTIPLIER,
               IDLE_THRESHOLD_SEC
               + AUTOCLOSE_CADENCE_SEC * (AUTOCLOSE_SKEW_CADENCES + 1))


def _process_index() -> dict | None:
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            capture_output=True, text=True, errors="replace", timeout=10,
        )
        if proc.returncode != 0:
            return None
        command_by_pid: dict[int, str] = {}
        child_commands: dict[int, list[str]] = {}
        parent_by_pid: dict[int, int] = {}
        for line in proc.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) != 3:
                continue
            pid_s, ppid_s, command = parts
            if not (pid_s.isdigit() and ppid_s.isdigit()):
                continue
            command_by_pid[int(pid_s)] = command
            parent_by_pid[int(pid_s)] = int(ppid_s)
            child_commands.setdefault(int(ppid_s), []).append(command)
        if not command_by_pid:
            return None
        return {"command_by_pid": command_by_pid, "child_commands": child_commands, "parent_by_pid": parent_by_pid}
    except Exception:
        return None


def _looks_like_session(command: str) -> bool:
    tokens = command.split()
    return bool(tokens) and os.path.basename(tokens[0]) in ("claude", "codex")


def _has_live_background_shell(record: dict, index: dict | None) -> bool:
    if (record.get("runtime") or "claude") != "claude":
        return False
    if not index:
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
        tokens = command.split()
        if (tokens and os.path.basename(tokens[0]) in ("zsh", "bash", "sh")
                and " -c " in command):
            return True
    return False


def _last_activity(record: dict, record_mtime: int,
                   codex_log_cache: dict | None = None,
                   claude_log_cache: dict | None = None) -> tuple[int, Path | None]:
    try:
        log = _resolve_transcript_path(record, codex_log_cache, claude_log_cache)
        if log is None:
            return record_mtime, None
        return max(record_mtime, _activity_of_log(log)), log
    except Exception as e:
        print(f"stale_monitor: transcript-activity check failed for {record.get('claude_sid')} ({e})",
              file=sys.stderr)
        return record_mtime, None


def _last_activity_mtime(record: dict, record_mtime: int) -> int:
    return _last_activity(record, record_mtime)[0]


def _limit_banner_text(log_path: Path | None, strict: bool = False) -> str | None:
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


MODEL_CREDIT_NAME_RE = re.compile(r"reached your\s+(.{1,40}?)\s+limit", re.IGNORECASE)


def _model_credit_banner_model(banner: str | None) -> str | None:
    if not banner:
        return None
    match = MODEL_CREDIT_NAME_RE.search(banner)
    return match.group(1).strip() if match else None


def _same_model_family(spent: str | None, target: str | None) -> bool:
    if not spent or not target:
        return False
    spent_key = spent.strip().lower()
    target_key = target.lower()
    if _normalize_model is not None:
        family = _normalize_model(target)
        if family and (family in spent_key or spent_key in family):
            return True
    return spent_key in target_key


def _api_error_hit(event: dict, signatures: tuple) -> tuple[str, float | None] | None:
    if not isinstance(event, dict):
        return None
    message = event.get("message")
    message = message if isinstance(message, dict) else {}
    if not (event.get("isApiErrorMessage") is True
            or message.get("model") == MODEL_CREDIT_SYNTHETIC_MODEL):
        return None
    text = _assistant_event_text(event)
    if not text:
        return None
    lowered = text.lower()
    if all(signature in lowered for signature in signatures):
        return text, _parse_iso(event.get("timestamp"))
    return None


def _api_error_banner(log_path: Path | None, signatures: tuple) -> tuple[str, float | None] | None:
    try:
        if log_path is None:
            return None
        return _api_error_hit(_last_assistant_event(log_path), signatures)
    except Exception as e:
        print(f"stale_monitor: api-error banner check failed for {log_path} ({e})",
              file=sys.stderr)
        return None


def _model_credit_banner(log_path: Path | None) -> tuple[str, float | None] | None:
    return _api_error_banner(log_path, MODEL_CREDIT_SIGNATURES)


def _hold_banner(log_path: Path | None) -> tuple[str, float | None] | None:
    return _api_error_banner(log_path, HOLD_SIGNATURES)


def _newest_model_credit_banner(log_path: Path | None, not_before: float,
                                max_bytes: int = MODEL_CREDIT_SCAN_BYTES) -> tuple[str, float] | None:
    try:
        if log_path is None:
            return None
        size = log_path.stat().st_size
        with open(log_path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read(max_bytes)
        if b"reached your" not in data:
            return None
        lines = data.split(b"\n")
        if size > max_bytes:
            lines = lines[1:]
        newest = None
        for line in lines:
            if b"reached your" not in line:
                continue
            try:
                event = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "assistant":
                continue
            hit = _api_error_hit(event, ("reached your",))
            if hit is None or hit[1] is None or hit[1] < not_before:
                continue
            name = _model_credit_banner_model(hit[0])
            if name and (newest is None or hit[1] > newest[1]):
                newest = (name.lower(), hit[1])
        return newest
    except Exception as e:
        print(f"stale_monitor: model-credit banner scan failed for {log_path} ({e})",
              file=sys.stderr)
        return None


def _banner_after_launch(record: dict, hit) -> bool:
    return (hit is not None and hit[1] is not None
            and hit[1] >= (record.get("started_at") or 0))


def _model_credit_banner_text(log_path: Path | None) -> str | None:
    hit = _model_credit_banner(log_path)
    return hit[0] if hit is not None else None


def _fable_family_banner(banner_text: str | None) -> bool:
    model = _model_credit_banner_model(banner_text)
    return isinstance(model, str) and FABLE_FAMILY in re.split(r"[^a-z0-9]+", model.lower())


def _fable_banner_ts(record: dict, claude_log_cache: dict | None = None) -> float | None:
    hit = _model_credit_banner(_resolve_transcript_path(record, None, claude_log_cache))
    if hit is None or hit[1] is None or not _fable_family_banner(hit[0]):
        return None
    return hit[1] if hit[1] >= (record.get("started_at") or 0) else None


def _ends_with_fable_banner(record: dict) -> bool:
    try:
        hit = _model_credit_banner(_resolve_transcript_path(record))
    except Exception:
        return False
    return hit is not None and _fable_family_banner(hit[0])


def _note_model_spent(record: dict, claude_log_cache: dict | None = None) -> None:
    account = record.get("account")
    started = record.get("started_at")
    if (record.get("agent") == "manager" or (record.get("runtime") or "claude") != "claude"
            or not isinstance(account, str) or not account or not isinstance(started, (int, float))):
        return
    hit = _newest_model_credit_banner(_resolve_transcript_path(record, None, claude_log_cache), started)
    if hit is not None:
        _record_model_spent(account, *hit)


def _is_transient_throttle(banner: str | None) -> bool:
    if not banner:
        return False
    lowered = banner.lower()
    return any(sig in lowered
               for sig in TRANSIENT_THROTTLE_SIGNATURES + TRANSIENT_SERVER_ERROR_SIGNATURES)


def _is_auth_401_event(event) -> bool:
    if not isinstance(event, dict) or event.get("type") != "assistant":
        return False
    if not event.get("isApiErrorMessage"):
        return False
    if _safe_int(event.get("apiErrorStatus")) == 401:
        return True
    lowered = _assistant_event_text(event).lower()
    return any(signature in lowered for signature in AUTH_FAILURE_SIGNATURES)


def _auth_failure_signature(log_path: Path | None) -> tuple[str | None, str] | None:
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
            return None
        return reset_ts
    except Exception:
        return None


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _load_scheduled(emitted: dict, key: str) -> dict | None:
    value = emitted.get(key)
    if (isinstance(value, dict)
            and isinstance(value.get("at"), (int, float))
            and isinstance(value.get("baseline"), (int, float))):
        return value
    return None


def _last_assistant_text(log_path: Path, max_bytes: int = 65536) -> str | None:
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


def _last_event(log_path: Path, types: tuple, max_bytes: int = 65536) -> dict | None:
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
        if isinstance(event, dict) and event.get("type") in types:
            last_event = event
    return last_event


def _last_assistant_event(log_path: Path, max_bytes: int = 65536) -> dict | None:
    return _last_event(log_path, ("assistant",), max_bytes)


def _is_rate_limited(record: dict) -> bool:
    try:
        if (record.get("runtime") or "claude") != "claude":
            return False
        return _limit_banner_text(_resolve_transcript_path(record)) is not None
    except Exception as e:
        print(f"stale_monitor: rate-limit check failed for {record.get('claude_sid')} ({e})",
              file=sys.stderr)
        return False


def _count_unseen_done_events(manager_name: str) -> int:
    try:
        done_dir = ROOT / "done" / manager_name
        if not done_dir.is_dir():
            return 0
        seen_path = ROOT / f".seen-done-{manager_name}"
        seen = set()
        if seen_path.exists():
            seen = {line for line in seen_path.read_text().splitlines() if line}
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
    line = f"while down: {stalled} workers stalled, {nudged} nudged, {done} done events"
    resumed = _safe_int(buffer.get("resumed"))
    orphaned = _safe_int(buffer.get("orphaned"))
    if resumed:
        line += f", {resumed} resumed"
    if orphaned:
        line += f", {orphaned} orphaned children closed"
    switched = buffer.get("switched")
    if isinstance(switched, str) and switched:
        line += f", switched {switched}"
    since = _safe_int(buffer.get("since"))
    if since and now > since:
        line += f", down {(now - since) // 60}min"
    return _render_line("LIMIT_CLEARED", {"clock": datetime.fromtimestamp(now).strftime("%H:%M"), "summary": line})


def _limited_flag_path(manager_name: str) -> Path:
    return ROOT / f".manager-limited-{_safe_bucket(manager_name)}"


def _outbox_dir(manager_name: str) -> Path:
    return ROOT / "notify-outbox" / _safe_bucket(manager_name)


EXIT_LANE_DEAD = 3
_READER_GONE = select.POLLERR | select.POLLHUP | select.POLLNVAL


class LaneDead(Exception):
    pass


def _reader_is_dead(fd: int = 1) -> bool:
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
    try:
        sys.stdout.write(f"{line}\n")
        sys.stdout.flush()
    except (BrokenPipeError, OSError) as exc:
        raise LaneDead(f"stdout write failed: {exc}") from exc


def _render_line(lane_kind: str, fields: dict) -> str | None:
    if _lane_lines is None:
        return " ".join([lane_kind, *(str(v) for v in fields.values())])
    return _lane_lines.render(lane_kind, **fields)


def _head_of(lane_kind: str, fields: dict) -> str:
    if _lane_lines is None:
        return " ".join(str(v) for v in fields.values())
    return _lane_lines.head(lane_kind, **fields)


def _journal_silent(lane_kind: str, fields: dict, sid: str | None) -> None:
    if _lane_lines is None or _append_event is None or not sid or not _lane_lines.journals(lane_kind):
        return
    ticket = (_load(ASSIGNMENTS / f"{sid}.json") or {}).get("ticket")
    if not isinstance(ticket, str) or not ticket:
        return
    try:
        path = ROOT / "artifacts" / _safe_segment(ticket) / "events.jsonl"
        if path.exists():
            _append_event(path, {"type": "lane_event", "kind": lane_kind, "fields": fields})
    except (OSError, ValueError):
        return


def _detach_stdout() -> None:
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
ACTION_KEY_PREFIXES = (
    "nudge_sent:", "nudged:", "scheduled:", "recovery:", "auth-recovery:",
    "untasked:", "model-fallback:", "safeguard:", "fable-move:",
)
ACTION_KEY_EXACT = ("last_autoclose_run", "codex_log_cache", "limited_buffer",
                    "lane_check_tick")
PAGE_KEY_EXACT = ()
PAGE_KEY_PREFIXES = (
    "processing:", "orphan:", "approval:", "auth-emit:", "model-fallback-page:",
    "lane_silent:", "lane_stale_seen:", "untasked-page:",
    "orphan-child:", "dead-child-window:", "pane-turn:", "context:",
    "usage-cap:", "safeguard-page:", "fable-limit-page:", "account-hold:",
)
PANE_TURN_PREFIX = "pane-turn:"
REPLAYED_PAGE_PREFIXES = ("account-hold:", "fable-limit-page:")


IDLE_BRANCH_KEY_PREFIXES = ("untasked:", "untasked-page:",
                            "model-fallback:", "model-fallback-page:")


def _carry_idle_keys_when_the_idle_branch_did_not_run(
        emitted: dict, next_emitted: dict, idle_ran: set, live_sids: set) -> None:
    for key, value in emitted.items():
        if key in next_emitted:
            continue
        if not any(key.startswith(pref) for pref in IDLE_BRANCH_KEY_PREFIXES):
            continue
        parts = key.split(":")
        if len(parts) < 2:
            continue
        sid = parts[1]
        if sid in idle_ran or sid not in live_sids:
            continue
        next_emitted[key] = value


def _is_action_key(key: str) -> bool:
    return (key in ACTION_KEY_EXACT
            or any(key.startswith(p) for p in ACTION_KEY_PREFIXES))


def _record_action_ahead(emitted_state_path: Path, emitted: dict,
                         next_emitted: dict, key: str, value) -> None:
    next_emitted[key] = value
    _commit_actions_only(emitted_state_path, emitted, next_emitted)


def _commit_actions_only(emitted_state_path: Path, emitted: dict,
                         next_emitted: dict) -> None:
    try:
        keep = {k: v for k, v in next_emitted.items() if _is_action_key(k)}
        _write_json_atomic(emitted_state_path, {**emitted, **keep})
    except Exception as e:
        print(f"stale_monitor: action-ledger commit failed ({e})",
              file=sys.stderr)


LANE_INTERVALS = {"questions": 2, "done": 2, "turn-ends": 5, "stale": 60, "pages": 1}
LANE_HEARTBEAT_STALE_INTERVALS = 3
LANE_HEARTBEAT_STALE_MIN_SEC = 6
LANE_SILENT_LADDER_BASE_SEC = 600
LANE_SILENT_LADDER_CAP_SEC = 4 * 3600


def _lane_heartbeat_path(manager_name: str, lane: str) -> Path:
    return ROOT / "lane-health" / _safe_bucket(manager_name) / f"{_safe_bucket(lane)}.json"


def _write_lane_heartbeat(manager_name: str, lane: str, now: float) -> None:
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
                         now: float, lanes_in_manager: bool = True) -> list[tuple[str, dict]]:
    out = []
    try:
        prior_tick = emitted.get("lane_check_tick")
        next_emitted["lane_check_tick"] = now
        if isinstance(prior_tick, (int, float)):
            gap = now - prior_tick
            if gap > LANE_INTERVALS["stale"] * LANE_HEARTBEAT_STALE_INTERVALS:
                for lane in LANE_INTERVALS:
                    carried = emitted.get(f"lane_silent:{lane}")
                    if carried is not None:
                        next_emitted[f"lane_silent:{lane}"] = carried
                return []

        lanes = ("pages",) if not lanes_in_manager else tuple(l for l in LANE_INTERVALS if l != "stale")
        for lane in lanes:
            interval = LANE_INTERVALS[lane]
            record = _load(_lane_heartbeat_path(manager_name, lane))
            last_scan = record.get("last_scan") if isinstance(record, dict) else None
            if not isinstance(last_scan, (int, float)) or last_scan <= 0:
                continue
            silent = now - last_scan
            if silent <= max(interval * LANE_HEARTBEAT_STALE_INTERVALS, LANE_HEARTBEAT_STALE_MIN_SEC):
                continue
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
                    next_emitted[key] = prior
                    continue
            next_emitted[key] = {"at": now, "level": level + 1}
            out.append((key, {"lane": lane, "minutes": int(silent // 60), "interval": interval}))
    except Exception as e:
        print(f"stale_monitor: lane liveness check failed ({e})", file=sys.stderr)
    return out


def _drain_outbox(manager_name: str) -> None:
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
    persisted_uptime = record.get("last_turn_at_uptime")
    if isinstance(persisted_uptime, (int, float)) and current_uptime >= persisted_uptime:
        return int(current_uptime - persisted_uptime)
    last_turn = _parse_iso(record.get("last_turn_at"))
    if last_turn is None:
        started = record.get("started_at")
        last_turn = started if isinstance(started, (int, float)) and started > 0 else None
    if last_turn is None:
        return None
    return now - int(last_turn)


def _idle_untasked_action(record: dict, path: Path, blocked: set,
                          current_uptime: float, process_index, now: int,
                          pane_memo=None, records=None):
    sid = record.get("claude_sid") or path.stem
    elapsed_sec = _compute_idle_elapsed_sec(record, current_uptime, now)
    if elapsed_sec is None:
        return None
    key = f"untasked:{sid}"
    tasked_at = record.get("tasked_at")
    tasked_at = float(tasked_at) if isinstance(tasked_at, (int, float)) else 0.0
    prompted_at = record.get("processing_since")
    prompted_at = float(prompted_at) if isinstance(prompted_at, (int, float)) else 0.0
    episode_start = max(tasked_at, prompted_at)
    stamps = (tasked_at, prompted_at)
    if elapsed_sec < IDLE_UNTASKED_SEC:
        return key, None, stamps
    if sid in blocked:
        return key, None, stamps
    if _reported_since_input(record, sid):
        return key, None, stamps
    if now - episode_start < _turn_end_grace_sec():
        return key, None, stamps
    if _is_transcript_live(record, now) or _is_delegation_live(record, records=records):
        return key, None, stamps
    if _is_rate_limited(record):
        return key, None, stamps
    if _model_credit_banner_text(_resolve_transcript_path(record)) is not None:
        return key, None, stamps
    if elapsed_sec <= IDLE_UNTASKED_BUSY_SEC:
        index = process_index()
        if not index or _has_live_background_shell(record, index):
            return key, None, stamps
    if _pane_shows_live_turn(_claude_pane_of(record), pane_memo):
        return key, None, stamps
    return key, int(elapsed_sec // 60), stamps


MODEL_CREDIT_NONE = ""
MODEL_CREDIT_HELD = "held"
MODEL_CREDIT_TYPED = "typed"
_UNSET = object()


def _fable_move_key(record: dict, path: Path) -> str:
    sid = record.get("claude_sid") or path.stem
    return f"fable-move:{sid}:{int(record.get('started_at') or 0)}"


def fable_move_closing(state_dir: Path, record: dict, path: Path) -> bool:
    try:
        key = _fable_move_key(record, path)
        for ledger_path in sorted(state_dir.glob(f"{EMITTED_STATE_PREFIX}*.json")):
            entry = _load_emitted_state(ledger_path).get(key)
            if isinstance(entry, dict) and entry.get("stage") == "closing":
                return True
    except Exception:
        return False
    return False


def _fable_elsewhere(account: str | None, now: float) -> bool | None:
    if not account:
        return False
    try:
        from dockwright import config
        from dockwright.spawner import _fable_left, _fable_spent
    except Exception:
        return None
    return bool(_fable_left(now)) and any(
        name != account and not _fable_spent(name, now) for name in config.account_names())


def _stamp_moved(sid: str, now: int) -> None:
    path = CLOSED / f"{sid}.json"
    record = _load(path)
    if not isinstance(record, dict):
        return
    record["moved"] = "fable"
    record["moved_at"] = now
    _write_json_atomic(path, record)


def _start_fable_move(record: dict, path: Path, emitted: dict, next_emitted: dict,
                      emitted_state_path: Path, now: int) -> tuple[str, dict]:
    key = _fable_move_key(record, path)
    move = {"at": now, "sid": record.get("claude_sid") or path.stem, "name": record.get("name") or "",
            "pid": record.get("pid"), "window_id": record.get("window_id") or "",
            "started_at": int(record.get("started_at") or 0), "stage": "closing", "attempts": 0}
    _record_action_ahead(emitted_state_path, emitted, next_emitted, key, move)
    return key, move


def _fable_move_page_fields(move: dict) -> dict:
    return {"name": move.get("name") or "", "variant": move.get("variant"), "reason": move.get("reason"),
            "attempts": move.get("attempts"), "pid": move.get("pid"), "pane": move.get("window_id")}


def _drive_fable_move(key: str, move: dict, now: int, emitted: dict, next_emitted: dict,
                      emitted_state_path: Path, emit) -> None:
    sid = str(move.get("sid") or "")
    name = move.get("name") or ""
    active_path = ACTIVE / f"{sid}.json"

    def commit(**changes) -> None:
        move.update(changes)
        _record_action_ahead(emitted_state_path, emitted, next_emitted, key, move)

    if move.get("stage") == "closing":
        active = _load(active_path)
        if isinstance(active, dict):
            if int(active.get("started_at") or 0) != int(move.get("started_at") or 0):
                commit(stage="done")
                return
            if not _ends_with_fable_banner(active):
                commit(stage="done")
                return
        pid = move.get("pid")

        def alive_now() -> bool:
            return isinstance(pid, int) and pid > 0 and _pid_alive(pid)

        alive = isinstance(active, dict) or alive_now()
        if alive:
            _close_window(move.get("window_id") or "")
            polls = max(1, int(FABLE_MOVE_PID_WAIT_SEC / FABLE_MOVE_PID_POLL_SEC) + 1)
            for i in range(polls):
                alive = alive_now()
                if not alive:
                    break
                if i < polls - 1:
                    time.sleep(FABLE_MOVE_PID_POLL_SEC)
        if alive:
            commit(stage="paged", variant="close-refused",
                   reason=f"pid {pid} still runs {FABLE_MOVE_PID_WAIT_SEC} s after the window close")
        else:
            active = _load(active_path)
            if not isinstance(active, dict) or not _archive_child(active_path, active, "fable-moved", claim=True):
                _stamp_moved(sid, now)
            commit(stage="resuming", attempts=0)
    if move.get("stage") == "resuming":
        active = _load(active_path)
        if isinstance(active, dict) and (
                not _ends_with_fable_banner(active)
                or (int(active.get("started_at") or 0) != int(move.get("started_at") or 0)
                    and _fable_banner_ts(active) is not None)):
            commit(stage="done")
            return
        try:
            from dockwright import mcp_server
        except Exception:
            commit(stage="paged", variant="resume-refused", reason="standalone lane cannot resume a session")
        else:
            try:
                result = asyncio.run(mcp_server.send_manager_to_worker_auto_impl(name, NUDGE_TEXT, marker=""))
            except Exception as err:
                result = None
                reason = (str(err) if isinstance(err, (ValueError, RuntimeError))
                          else f"{type(err).__name__}: {err}")[:200]
            status = result.get("status") if isinstance(result, dict) else None
            if status in FABLE_MOVE_SUCCESS_STATUSES:
                commit(stage="done", status=status)
                emit("fable-limit", name, "FABLE_LIMIT", {"name": name, "variant": "moved", "status": status},
                     sid=sid)
                _append_account_ledger({"ts": now, "event": "fable-moved", "worker": name, "from_sid": sid,
                                        "status": status, "by": "stale_monitor"})
                return
            if status == "human_attached":
                commit(stage="paged", variant="human", reason="human attached")
            else:
                if result is not None:
                    reason = str(result.get("reason") or result.get("status"))[:200]
                attempts = int(move.get("attempts") or 0) + 1
                if attempts >= FABLE_MOVE_MAX_ATTEMPTS:
                    commit(stage="paged", variant="resume-refused", reason=reason, attempts=attempts)
                else:
                    commit(attempts=attempts, reason=reason)
                    return
    if move.get("stage") == "paged":
        pkey = key.replace("fable-move:", "fable-limit-page:", 1)
        if _carry_page(emitted, next_emitted, pkey, now):
            emit("fable-limit", name, "FABLE_LIMIT", _fable_move_page_fields(move), pkey, sid=sid)


def _scan_fable_moves(now: int, emitted: dict, next_emitted: dict, emitted_state_path: Path, emit) -> None:
    moves = {}
    for key, move in emitted.items():
        if not key.startswith("fable-move:") or not isinstance(move, dict):
            continue
        stage = move.get("stage")
        if stage in ("done", "paged") and now - _safe_int(move.get("at")) >= FABLE_MOVE_KEEP_SEC:
            continue
        next_emitted[key] = dict(move)
        if stage != "done":
            moves[key] = next_emitted[key]
    for key, move in moves.items():
        try:
            _drive_fable_move(key, move, now, emitted, next_emitted, emitted_state_path, emit)
        except Exception as e:
            print(f"stale_monitor: fable-move {key} failed ({e})", file=sys.stderr)


def _fable_limit_action(record: dict, path: Path, banner_ts: float | None, emitted: dict,
                        next_emitted: dict, emitted_state_path: Path, now: int, emit_fn,
                        blocked: set | None, pane_memo, sends_left: int,
                        claude_log_cache: dict | None = None) -> str | None:
    sid = record.get("claude_sid") or path.stem
    name = record.get("name") or ""
    key = _fable_move_key(record, path)
    in_flight = emitted.get(key)
    if isinstance(in_flight, dict) and in_flight.get("stage") != "done":
        return MODEL_CREDIT_HELD
    if banner_ts is None:
        return MODEL_CREDIT_NONE
    switch = f"switch it to {fable_fallback_model()} at effort {FABLE_FALLBACK_EFFORT}"
    resume = "tell it to resume"

    def page(variant: str, reason: str) -> str:
        pkey = f"fable-limit-page:{sid}:{variant}"
        last_page = emitted.get(pkey)
        if isinstance(last_page, (int, float)) and now - last_page < MODEL_FALLBACK_CONFIRM_SEC:
            next_emitted[pkey] = last_page
            return MODEL_CREDIT_HELD
        next_emitted[pkey] = now
        emit_fn("fable-limit", name, "FABLE_LIMIT",
                {"name": name, "variant": variant, "reason": reason, "switch": switch, "resume": resume,
                 "pane": _claude_pane_of(record), "parent": record.get("parent_name") or ""},
                pkey, sid=sid)
        return MODEL_CREDIT_HELD

    if is_human_attached(record, now):
        return page("not-typed", "a human is attached to the pane")
    if not AUTONUDGE:
        return page("not-typed", "autonudge is off")
    if (sid in (blocked or set()) or sends_left <= 0 or _is_transcript_live(record, now)
            or _pane_shows_live_turn(_claude_pane_of(record), pane_memo)):
        return MODEL_CREDIT_HELD
    if not _pane_input_idle(_claude_pane_of(record), pane_memo, record.get("runtime") or "claude"):
        return page("unsent-text", "text sits unsubmitted in the pane")
    if _is_interactive_child(record):
        _archive_child(path, record, "fable-spent", claim=True)
        _append_account_ledger({"ts": now, "event": "fable-spent-child", "worker": name,
                                "parent": record.get("parent_name"), "account": record.get("account"),
                                "by": "stale_monitor"})
        emit_fn("fable-limit", name, "FABLE_LIMIT",
                {"name": name, "variant": "child-closed", "parent": record.get("parent_name")}, sid=sid)
        return MODEL_CREDIT_TYPED
    if _is_subworker(record):
        return MODEL_CREDIT_NONE
    try:
        elsewhere = _fable_elsewhere(record.get("account"), now)
    except Exception as err:
        return page("not-typed", f"the Fable probe failed: {err}"[:200])
    if elsewhere is None:
        return page("not-typed", "standalone lane cannot move a session")
    if not elsewhere:
        return None
    key, move = _start_fable_move(record, path, emitted, next_emitted, emitted_state_path, now)
    try:
        _drive_fable_move(key, move, now, emitted, next_emitted, emitted_state_path, emit_fn)
    except Exception as e:
        print(f"stale_monitor: fable-move {key} failed ({e})", file=sys.stderr)
    return MODEL_CREDIT_TYPED


def _model_credit_action(record: dict, path: Path, emitted: dict, next_emitted: dict,
                         emitted_state_path: Path, now: int, emit_fn,
                         blocked: set | None = None, pane_memo=None,
                         sends_left: int = 1, resume_text: str = NUDGE_TEXT,
                         model=_UNSET, own_pane: bool = False,
                         effort: str | None = None,
                         claude_log_cache: dict | None = None) -> str:
    effort = effort or MODEL_FALLBACK_EFFORT
    sid = record.get("claude_sid") or path.stem
    akey = f"model-fallback:{sid}"
    pkey = f"model-fallback-page:{sid}"
    if (record.get("runtime") or "claude") != "claude":
        return MODEL_CREDIT_NONE
    hit = _model_credit_banner(_resolve_transcript_path(record, None, claude_log_cache))
    banner_text = hit[0] if hit is not None else None
    banner = banner_text is not None
    fired_at = emitted.get(akey)
    if isinstance(fired_at, (int, float)):
        next_emitted[akey] = fired_at
    if not banner:
        return MODEL_CREDIT_NONE
    if model is _UNSET and not own_pane and hit[1] is not None and _fable_family_banner(banner_text):
        verdict = _fable_limit_action(
            record, path, _fable_banner_ts(record, claude_log_cache), emitted, next_emitted,
            emitted_state_path, now, emit_fn, blocked, pane_memo, sends_left, claude_log_cache)
        if verdict is not None:
            return verdict
        model = fable_fallback_model()
        effort = FABLE_FALLBACK_EFFORT
    model = worker_fallback_model() if model is _UNSET else model
    name = record.get("name") or ""
    pane = _claude_pane_of(record)
    whom = "YOUR OWN pane, the one you read this in," if own_pane else "it"
    resume = "resume orchestration yourself" if own_pane else "tell it to resume"
    switch = f"switch {whom} to {model} at effort {effort}"

    def page(variant: str, **fields) -> str:
        last_page = emitted.get(pkey)
        if isinstance(last_page, (int, float)) and now - last_page < MODEL_FALLBACK_CONFIRM_SEC:
            next_emitted[pkey] = last_page
            return MODEL_CREDIT_HELD
        next_emitted[pkey] = now
        emit_fn("model-fallback", name, "MODEL_CREDIT_EXHAUSTED", {"name": name, "variant": variant, **fields},
                pkey, sid=sid)
        return MODEL_CREDIT_HELD

    spent = _model_credit_banner_model(banner_text)
    if not model:
        remedy = (f"switch {whom} to a model you still have credits on by hand"
                  if own_pane else
                  "point CLAUDE_ORCH_MODEL_FALLBACK at a family with credits")
        return page("unresolved", spent=spent or "its model", remedy=remedy, resume=resume)
    if _same_model_family(spent, model):
        remedy = (f"switch {whom} to a model you still have credits on by hand"
                  if own_pane else
                  "point CLAUDE_ORCH_MODEL_FALLBACK at a family with credits")
        return page("same-family", spent=spent, model=model, remedy=remedy, resume=resume)

    if isinstance(fired_at, (int, float)):
        if now - fired_at < MODEL_FALLBACK_CONFIRM_SEC:
            return MODEL_CREDIT_HELD
        reported = _model_credit_banner_model(banner_text)
        named = f" The banner now names {reported}." if reported else ""
        cascade = (" and every worker this lane rescues will die the same way"
                   if model == worker_fallback_model() else "")
        return page("still-out", confirm_sec=MODEL_FALLBACK_CONFIRM_SEC, named=named, model=model,
                    cascade=cascade, switch=switch, resume=resume)
    if not (AUTONUDGE and pane) or is_human_attached(record, now):
        return page("not-typed", switch=switch, resume=resume)
    if sid in (blocked or set()):
        return MODEL_CREDIT_HELD
    if sends_left <= 0:
        return MODEL_CREDIT_HELD
    if _is_transcript_live(record, now) or _pane_shows_live_turn(pane, pane_memo):
        return MODEL_CREDIT_HELD
    if not _pane_input_idle(pane, pane_memo, record.get("runtime") or "claude"):
        return page("unsent-text", switch=switch, resume=resume)
    _record_action_ahead(emitted_state_path, emitted, next_emitted, akey, now)
    if own_pane:
        resume_text = _manager_model_fallback_resume_text(
            model, _model_credit_banner_model(banner_text))
    for text in (f"/model {model}",
                 f"/effort {effort}",
                 resume_text):
        if not _send_text_checked(pane, text, pane_memo,
                                  record.get("runtime") or "claude", sid):
            return page("partial-switch", switch=switch, resume=resume)
    _append_account_ledger({"ts": now, "event": "model-fallback", "worker": name,
                            "agent": record.get("agent") or "worker",
                            "from_sid": sid, "model": model,
                            "effort": effort, "by": "stale_monitor"})
    target = "YOUR OWN pane" if own_pane else "its pane"
    told = "and left you a resume line" if own_pane else "and told it to resume"
    emit_fn("model-fallback", name, "MODEL_FALLBACK",
            {"name": name, "model": model, "effort": effort, "target": target, "told": told}, sid=sid)
    return MODEL_CREDIT_TYPED


SAFEGUARD_NONE = ""
SAFEGUARD_HELD = "held"
SAFEGUARD_TYPED = "typed"


def _safeguard_stop_event(log_path: Path | None) -> str | None:
    if log_path is None:
        return None
    event = _last_event(log_path, SAFEGUARD_EVENT_TYPES)
    if not isinstance(event, dict):
        return None
    if event.get("type") == "system":
        text = event.get("content")
    elif event.get("type") == "user" and event.get("isMeta") is True:
        message = event.get("message")
        text = message.get("content") if isinstance(message, dict) else None
    else:
        return None
    if not isinstance(text, str) or not any(sig in text.lower() for sig in SAFEGUARD_STOP_SIGNATURES):
        return None
    return str(event.get("uuid") or event.get("timestamp") or "?")


def _pane_tail(pane_text: str) -> list[str]:
    return pane_text.rstrip().splitlines()[-APPROVAL_TAIL_LINES:]


def _safeguard_dialog_key(pane_text: str | None) -> str | None:
    if not pane_text:
        return None
    lines = _pane_tail(pane_text)
    carets = [i for i, ln in enumerate(lines) if "❯" in ln]
    if not carets or not SAFEGUARD_SELECTOR_RE.search(lines[carets[-1]]):
        return None
    selector = carets[-1]
    titles = [i for i, ln in enumerate(lines[:selector]) if SAFEGUARD_TITLE_MARKER in ln.lower()]
    if not titles:
        return None
    for i in range(max(titles[-1] + 1, selector - SAFEGUARD_OPTION_SPAN),
                   min(len(lines), selector + SAFEGUARD_OPTION_SPAN + 1)):
        match = SAFEGUARD_EDIT_OPTION_RE.search(lines[i])
        if match:
            return match.group(1)
    return None


def _press_key(window_id: str, key: str) -> bool:
    if not window_id or _get_driver is None:
        return False
    try:
        press = getattr(_get_driver(), "send_key", None)
        return bool(press(window_id, key)) if press is not None else False
    except Exception:
        return False


def _safeguard_settled(window_id: str) -> bool:
    for _ in range(SAFEGUARD_SETTLE_POLLS):
        time.sleep(SAFEGUARD_SETTLE_STEP_SEC)
        try:
            ansi = _get_driver().capture_screen_ansi(window_id)
        except Exception:
            continue
        if not ansi:
            continue
        if _safeguard_dialog_key(_strip_ansi(ansi)) is None and _input_is_idle(ansi):
            return True
    return False


def _safeguard_pause_action(record: dict, path: Path, emitted: dict, next_emitted: dict,
                            emitted_state_path: Path, now: int, emit_fn,
                            blocked: set | None = None, sends_left: int = 1,
                            claude_log_cache: dict | None = None) -> str:
    sid = record.get("claude_sid") or path.stem
    akey = f"safeguard:{sid}"
    acted = emitted.get(akey)
    if not (isinstance(acted, dict) and isinstance(acted.get("at"), (int, float))
            and now - acted["at"] < SAFEGUARD_RETRY_WINDOW_SEC):
        acted = None
    if acted is not None:
        next_emitted[akey] = acted
    pane = _claude_pane_of(record)
    if not pane or _get_driver is None:
        return SAFEGUARD_NONE
    event = _safeguard_stop_event(_resolve_transcript_path(record, None, claude_log_cache))
    if event is None:
        return SAFEGUARD_NONE
    pkey = f"safeguard-page:{sid}:{event}"
    paged = pkey in emitted
    if paged:
        next_emitted[pkey] = emitted[pkey]
    try:
        screen = _get_driver().capture_screen(pane)
    except Exception:
        return SAFEGUARD_NONE
    key = _safeguard_dialog_key(screen)
    if key is None:
        return SAFEGUARD_NONE
    name = record.get("name") or ""
    fields = {"name": name, "pane": pane, "key": key, "window": SAFEGUARD_RETRY_WINDOW_SEC // 60,
              "parent": record.get("parent_name") or ""}

    def page(variant: str) -> str:
        next_emitted[pkey] = now
        emit_fn("safeguard", name, "SAFEGUARD_PAUSE", {**fields, "variant": variant}, pkey, sid=sid)
        return SAFEGUARD_HELD

    if paged or is_human_attached(record, now):
        return SAFEGUARD_HELD
    if acted is not None:
        return page("unsettled" if acted.get("event") == event else "again")
    if not AUTONUDGE:
        return page("not-typed")
    if sid in (blocked or set()) or sends_left <= 0:
        return SAFEGUARD_HELD
    _record_action_ahead(emitted_state_path, emitted, next_emitted, akey, {"at": now, "event": event})
    if not (_press_key(pane, key) and _safeguard_settled(pane)):
        return page("unsettled")
    if not _send_text_checked(pane, SAFEGUARD_CONTINUE_TEXT, None, record.get("runtime") or "claude", sid):
        return page("unsent")
    emit_fn("safeguard", name, "SAFEGUARD_PAUSE", {**fields, "variant": "continued"}, sid=sid)
    return SAFEGUARD_TYPED


def _autoclose_held(record: dict, elapsed_sec: int, process_index=None,
                    pane_memo=None) -> bool:
    if _pane_shows_live_turn(_claude_pane_of(record), pane_memo):
        return True
    if elapsed_sec > _busy_shell_deadline():
        return False
    if _is_transcript_live(record) or _transcript_unreadable(record):
        return True
    try:
        index = (process_index or _process_index)()
        return not index or _has_live_background_shell(record, index)
    except Exception:
        return True


def _closed_record(record: dict, closed_reason: str, window_id: str, transcript_path) -> dict:
    return {
        "claude_sid": record.get("claude_sid"),
        "name": record.get("name") or "",
        "cwd": record.get("cwd"),
        "window_id": window_id,
        "last_summary": record.get("last_summary"),
        "last_turn_at": record.get("last_turn_at"),
        "spend": record.get("spend"),
        "started_at": record.get("started_at"),
        "closed_at": time.time(),
        "closed_reason": closed_reason,
        "parent_manager_name": _parent_name_of(record),
        "parent_name": _parent_name_of(record),
        "parent_sid": record.get("parent_sid"),
        "kind": record.get("kind") or record.get("agent"),
        "depth": record.get("depth"),
        "runtime": record.get("runtime") or "claude",
        "provider": record.get("provider"),
        "account": record.get("account"),
        "transcript_path": transcript_path,
    }


def _drop_questions_of(sid: str) -> None:
    if not sid or not QUESTIONS.is_dir():
        return
    for p in QUESTIONS.rglob("*.json"):
        record = _load(p)
        if record is not None and record.get("worker_sid") == sid and record.get("asked_by") != "manager":
            p.unlink(missing_ok=True)


def _archive_child(record_path: Path, record: dict, closed_reason: str, *, claim: bool = False) -> bool:
    sid = record.get("claude_sid") or record_path.stem
    window_id = record.get("window_id") or record.get("iterm_sid") or ""
    transcript_path = record.get("transcript_path")
    if not transcript_path:
        try:
            resolved = _resolve_transcript_path(record)
        except Exception:
            resolved = None
        transcript_path = str(resolved) if resolved else None
    if claim:
        try:
            record_path.unlink()
        except FileNotFoundError:
            return False
    else:
        record_path.unlink(missing_ok=True)
    try:
        _write_json_atomic(
            CLOSED / f"{sid}.json",
            _closed_record(record, closed_reason, window_id, transcript_path))
    except OSError as e:
        print(f"stale_monitor: closed-record write failed for child {sid} ({e})",
              file=sys.stderr)
    _close_window(window_id)
    _drop_questions_of(sid)
    return True


def _child_round_of(start_command):
    command = start_command or ""
    match = CHILD_ROUND_RE.search(command)
    if match is None:
        return None
    prefix = command[:match.start()]
    return (_root_in(prefix), match.group("aid"), int(match.group("n")))


def _root_in(prefix: str) -> str:
    for i, char in enumerate(prefix):
        if char != "/":
            continue
        before = prefix[i - 1] if i else ""
        if before and not (before.isspace() or before in "='\""):
            continue
        if _own_root(prefix[i:]):
            return prefix[i:]
    foreign = CHILD_ROUND_FOREIGN_ROOT_RE.search(prefix)
    return foreign.group(0) if foreign is not None else prefix


def _own_root(root: str) -> bool:
    try:
        return Path(root).resolve() == ROOT.resolve()
    except OSError:
        return False


def _workers_panes(os_windows) -> list:
    panes = []
    for osw in os_windows or []:
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
                    panes.append({**win, "id": str(win["id"]), "tab_title": str(tab.get("title") or "?")})
    return panes


def _assignment_index() -> dict:
    index = {}
    for stem, assignment in _dir_json(ASSIGNMENTS):
        aid = assignment.get("assignment_id")
        if aid:
            index[str(aid)] = {"sid": stem, "round": max(1, _safe_int(assignment.get("round")))}
    return index


def _window_held_by_a_live_parent(pane_id: str, records) -> bool:
    def held(record) -> bool:
        return ((record.get("window_id") or record.get("iterm_sid") or "") == pane_id
                and _live_record_named(_parent_name_of(record), records) is not None)

    if any(held(r) for r in records):
        return True
    return any(held(record) for _stem, record in _dir_json(CLOSED))


def _pending_window_ids() -> set:
    ids = set()
    if ASSIGNMENTS_PENDING.is_dir():
        for sidecar in ASSIGNMENTS_PENDING.glob("*.window"):
            try:
                wid = sidecar.read_text().strip()
            except OSError:
                continue
            if wid:
                ids.add(wid)
    return ids


def _grace_elapsed(key: str, now: int, emitted: dict, next_emitted: dict) -> bool:
    prev = emitted.get(key)
    first_seen = prev if isinstance(prev, (int, float)) else now
    if now - first_seen < ORPHAN_GRACE_SEC:
        next_emitted[key] = first_seen
        return False
    return True


def _list_worker_windows():
    if _get_driver is None:
        return None
    try:
        return _get_driver().ls()
    except Exception:
        return None


def _scan_children(now, emitted, next_emitted, emit, manager_name, records_with_paths,
                   os_windows) -> None:
    records = [r for _, r in records_with_paths]
    for path, record in records_with_paths:
        if not _is_subworker(record) or not _in_scope(record, manager_name, records):
            continue
        parent_name = _parent_name_of(record)
        if _live_record_named(parent_name, records) is not None:
            continue
        sid = record.get("claude_sid") or path.stem
        if not _grace_elapsed(f"orphan-child:{sid}", now, emitted, next_emitted):
            continue
        name = record.get("name") or sid
        _archive_child(path, record, f"orphaned:{parent_name}")
        emit("orphaned-child", name, "ORPHANED_CHILD", {"name": name, "parent": parent_name},
             f"orphan-child:{sid}", sid=sid)
    if not os_windows:
        return
    index = _assignment_index()
    pending = _pending_window_ids()
    for pane in _workers_panes(os_windows):
        parsed = _child_round_of(pane.get("start_command", ""))
        if parsed is None or pane.get("dead") != "1":
            continue
        root, aid, n = parsed
        if not _own_root(root):
            continue
        pane_id = pane["id"]
        entry = index.get(aid)
        if entry is not None:
            if n < entry["round"]:
                _close_window(pane_id)
                print(f"stale_monitor: closed superseded round window {pane_id} "
                      f"(r{n} of assignment {aid}, current r{entry['round']})", file=sys.stderr)
                continue
            child_sid = entry["sid"]
            child = next((r for r in records if r.get("claude_sid") == child_sid), None)
            child_is_active = child is not None
            if child is None:
                child = _load(CLOSED / f"{child_sid}.json")
            if child is not None:
                if _live_record_named(_parent_name_of(child), records) is not None:
                    continue
                if child_is_active:
                    continue
                if not _in_scope(child, manager_name, records):
                    continue
        elif pane_id in pending:
            continue
        elif _window_held_by_a_live_parent(pane_id, records):
            continue
        if not _grace_elapsed(f"dead-child-window:{pane_id}", now, emitted, next_emitted):
            continue
        _close_window(pane_id)
        print(f"stale_monitor: closed dead child window {pane_id} (r{n} of assignment {aid}, "
              f"no live parent)", file=sys.stderr)


def _autoclose_idle_worker(record_path: Path, record: dict,
                           elapsed_sec: int, pane_memo=None) -> bool:
    if _autoclose_held(record, elapsed_sec, pane_memo=pane_memo):
        return False
    sid = record.get("claude_sid")
    window_id = record.get("window_id") or record.get("iterm_sid") or ""
    transcript_path = record.get("transcript_path")
    if not transcript_path:
        try:
            resolved = _resolve_transcript_path(record)
        except Exception:
            resolved = None
        transcript_path = str(resolved) if resolved else None
    closed_record = _closed_record(record, f"idle>{IDLE_THRESHOLD_SEC}s",
                                   window_id, transcript_path)
    if sid:
        _write_json_atomic(CLOSED / f"{sid}.json", closed_record)
    record_path.unlink(missing_ok=True)
    _close_window(window_id)
    return True


def _humanize_window(size: int) -> str:
    if size % 1000000 == 0:
        return f"{size // 1000000}M"
    if size % 1000 == 0:
        return f"{size // 1000}k"
    return str(size)


def _context_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _scan_context(now: int, emitted: dict, next_emitted: dict, emit,
                  records_with_paths, manager_name: str | None,
                  context_dir=None) -> None:
    root = Path(context_dir) if context_dir is not None else USAGE_CONTEXT
    index = [r for _, r in records_with_paths if isinstance(r, dict)]
    for path, record in records_with_paths:
        if not isinstance(record, dict) or record.get("nested") or _is_subworker(record):
            continue
        own_manager = (record.get("agent") == "manager"
                       and record.get("name") == manager_name
                       and manager_name is not None)
        if not (own_manager or _in_scope(record, manager_name, index)):
            continue
        if not _pid_alive(record.get("pid")):
            continue
        sid = record.get("claude_sid") or path.stem
        if not sid:
            continue
        key = f"context:{sid}"
        prior = emitted.get(key)
        data = _load(root / f"{sid}.json")
        used = _context_number(data.get("used_tokens")) if isinstance(data, dict) else None
        size = _context_number(data.get("context_window_size")) if isinstance(data, dict) else None
        pct = _context_number(data.get("used_percentage")) if isinstance(data, dict) else None
        if used is None or size is None or pct is None or size <= 0:
            if prior is not None:
                next_emitted[key] = prior
            continue
        crossed = [step for step in CONTEXT_PAGE_STEPS if used * 100 >= step * size]
        if not crossed:
            continue
        next_emitted[key] = {"step": crossed[-1]}
        prior_step = _safe_int(prior.get("step")) if isinstance(prior, dict) else 0
        name = record.get("funny_name") or record.get("name") or sid
        for step in crossed:
            if step > prior_step:
                emit("context", name, "CONTEXT",
                     {"name": name, "used": (int(used) + 500) // 1000, "window": _humanize_window(int(size)),
                      "pct": int(pct), "threshold": step, "own": own_manager}, key, sid=sid)


USAGE_CAP_REASON = "usage-cap"
USAGE_CAP_NODE_REASON = "usage-cap-node"
USAGE_CAP_FLEET_KEY = "usage-cap:fleet"
USAGE_CAP_FILE_KEY = "usage-cap:file"
USAGE_CAP_CLEARED_KEEP_SEC = 86400


def _is_pipeline_node(sid: str) -> bool:
    assignment = _load(ASSIGNMENTS / f"{sid}.json")
    return isinstance(assignment, dict) and bool(assignment.get("ticket") and assignment.get("pipeline_request_id"))


def _stop_fleet_workers() -> None:
    for record_path, record in _load_active_records():
        if record.get("agent") == "worker" and not record.get("nested"):
            sid = record.get("claude_sid") or record_path.stem
            reason = USAGE_CAP_NODE_REASON if _is_pipeline_node(sid) else USAGE_CAP_REASON
            _archive_child(record_path, record, reason, claim=True)


def _usage_cap_owed(manager_name: str | None, since: float) -> bool:
    if manager_name is None or not CLOSED.is_dir():
        return False
    if _DIR_MEMO is not None:
        return any(isinstance(record, dict) and record.get("closed_reason") == USAGE_CAP_REASON
                   and record.get("parent_manager_name") == manager_name
                   and _safe_int(record.get("closed_at")) > since
                   for _stem, record in _dir_json(CLOSED))
    for p in CLOSED.iterdir():
        try:
            if p.suffix != ".json" or p.stat().st_mtime <= since:
                continue
        except OSError:
            continue
        record = _load(p)
        if (isinstance(record, dict) and record.get("closed_reason") == USAGE_CAP_REASON
                and record.get("parent_manager_name") == manager_name
                and _safe_int(record.get("closed_at")) > since):
            return True
    return False


def _manager_recovery_step(mgr_record: dict, mgr_sid: str, account: str, pool: str, now: int,
                           emitted: dict, next_emitted: dict, emit, manager_name: str | None,
                           subject: str, cooldown: bool = True) -> None:
    recovery_key = f"recovery:{mgr_sid}"
    recovery = emitted.get(recovery_key)
    if not isinstance(recovery, dict):
        new_letter = (_maybe_flip_account(
            account, subject, now, cooldown=cooldown)
            if account == pool else None)
        already_flipped = account != pool
        if new_letter is None and not already_flipped:
            already_flipped = _recent_flip_landed_on(pool, now)
        if new_letter is not None or already_flipped:
            if new_letter is not None:
                emit("switched", manager_name, "SWITCHED",
                     {"account": account, "new": new_letter,
                      "subject": subject})
            target = new_letter or pool
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


def _scan_usage_cap(now: int, emitted: dict, next_emitted: dict, emit, manager_name: str | None = None) -> None:
    if _usage_cap is None:
        return
    verdict = _usage_cap.evaluate(now)
    prior = emitted.get(USAGE_CAP_FLEET_KEY)
    fleet = prior if isinstance(prior, dict) else {}
    if verdict.breaches:
        _stop_fleet_workers()
    fields = _usage_cap.page_fields(verdict, now) if verdict.breaches or verdict.error is not None else {}
    if verdict.error is not None:
        next_emitted[USAGE_CAP_FILE_KEY] = verdict.error
        if emitted.get(USAGE_CAP_FILE_KEY) != verdict.error:
            emit("usage-cap", "fleet", "USAGE_CAP_FILE", fields, USAGE_CAP_FILE_KEY)
    if verdict.breaches:
        paused = fleet.get("state") == "paused"
        if paused and fleet.get("paged"):
            next_emitted[USAGE_CAP_FLEET_KEY] = fleet
            return
        next_emitted[USAGE_CAP_FLEET_KEY] = {"state": "paused", "at": fleet.get("at") if paused else now,
                                             "paged": now}
        emit("usage-cap", "fleet", "USAGE_CAP", fields, USAGE_CAP_FLEET_KEY)
        return
    if verdict.error is not None:
        if fleet:
            next_emitted[USAGE_CAP_FLEET_KEY] = fleet
        return
    kept = fleet.get("state") == "cleared" and now - _safe_int(fleet.get("at")) < USAGE_CAP_CLEARED_KEEP_SEC
    if fleet.get("state") == "paused":
        cleared = {"state": "cleared", "at": now, "paged": now}
    elif kept and not fleet.get("paged"):
        cleared = {**fleet, "paged": now}
    elif _usage_cap_owed(manager_name, max(now - USAGE_CAP_CLEARED_KEEP_SEC, _safe_int(fleet.get("paged")))):
        cleared = {"state": "cleared", "at": now, "paged": now}
    elif kept:
        next_emitted[USAGE_CAP_FLEET_KEY] = fleet
        return
    else:
        return
    next_emitted[USAGE_CAP_FLEET_KEY] = cleared
    emit("usage-cap", "fleet", "USAGE_CAP_CLEARED", {}, USAGE_CAP_FLEET_KEY)


def _scan_orphan_windows(now: int, emitted: dict, next_emitted: dict, emit, os_windows) -> None:
    if not os_windows:
        return
    candidates: dict = {}
    for pane in _workers_panes(os_windows):
        candidates[pane["id"]] = pane
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
        for _stem, record in _dir_json(CLOSED):
            if record.get("claude_sid") not in pending_sids:
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
    for pane_id, pane in candidates.items():
        if pane_id in protected:
            continue
        title = pane["tab_title"]
        child = _child_round_of(pane.get("start_command", ""))
        if child is not None and (not _own_root(child[0]) or pane.get("dead") == "1"):
            continue
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
                if child is not None:
                    _root, aid, n = child
                    what = f"child round pane r{n} of assignment {aid}"
                else:
                    what = f"tab={title!r}"
                emit("orphan-window", pane_id, "ORPHAN_WINDOW",
                     {"pane": pane_id, "what": what, "minutes": elapsed // 60}, key)
        next_emitted[key] = entry


def _approval_dialog_block(pane_text: str) -> str | None:
    lines = _pane_tail(pane_text)
    tail = "\n".join(ln.rstrip() for ln in lines)
    low = tail.lower()
    if not any(m in low for m in APPROVAL_QUESTION_MARKERS):
        return None
    if not any(m in low for m in APPROVAL_OPTION_MARKERS):
        return None
    return tail


def _approval_excerpt(block: str) -> str:
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


def _is_dialog_rule(line: str) -> bool:
    stripped = line.strip(DIALOG_RULE_EXTRA_CHARS)
    return bool(stripped) and set(stripped) == {DIALOG_RULE_CHAR}


def _dialog_title_line(line: str) -> bool:
    text = line.strip(DIALOG_RULE_EXTRA_CHARS).strip()
    return any(text == title or text.startswith(title + " (") for title in DIALOG_TITLES)


def _is_dialog_border(line: str) -> bool:
    return "─" in line and set(line) <= DIALOG_BORDER_CHARS


def _dialog_sign(pane_text: str) -> bool:
    if not pane_text:
        return False
    lines = [ln for ln in pane_text.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        if not _is_dialog_rule(line):
            continue
        for above in reversed(lines[:i]):
            if _is_dialog_border(above):
                break
            if _dialog_title_line(above):
                return True
    return False


def _dialog_on_pane(pane: str, memo: dict | None = None) -> bool | None:
    if not pane or _get_driver is None:
        return None
    screens = memo.get("screens") if memo is not None else None
    if screens is not None and pane in screens:
        text = screens[pane]
    else:
        try:
            text = _get_driver().capture_screen(pane)
        except Exception:
            text = None
        if screens is not None:
            screens[pane] = text
    if text is None:
        return None
    return _dialog_sign(text) or _approval_dialog_block(text) is not None


def _prompt_holds(record: dict, now: float, pane_memo=None, claude_log_cache=None) -> bool:
    if _open_prompt(record, now, claude_log_cache=claude_log_cache) is None:
        return False
    return _dialog_on_pane(_claude_pane_of(record), pane_memo) is True


def _content_blocks(record: dict) -> list[dict]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _block_summary(block: dict) -> str | None:
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        return None
    return permission_summary(tool_input)


def _match_tool_use(log: Path, marker: dict) -> str | None:
    floor = float(marker.get("at") or 0) - MARKER_MATCH_WINDOW_SEC
    seen = {"assistant": 0}

    def _stop(record: dict) -> bool:
        ts = _parse_iso(record.get("timestamp"))
        if ts is not None and ts < floor:
            return True
        if record.get("type") == "assistant":
            seen["assistant"] += 1
        return seen["assistant"] >= MARKER_MATCH_ASSISTANT_RECORDS

    for record in _read_transcript_tail(log, _stop):
        if record.get("type") != "assistant":
            continue
        ts = _parse_iso(record.get("timestamp"))
        if ts is not None and ts < floor:
            return None
        for block in _content_blocks(record):
            if (block.get("type") == "tool_use" and block.get("name") == marker.get("tool_name")
                    and _block_summary(block) == marker.get("summary") and isinstance(block.get("id"), str)):
                return block["id"]
    return None


def _tool_result_seen(log: Path, tool_use_id: str, since: float) -> bool:
    floor = since - MARKER_MATCH_WINDOW_SEC

    def _stop(record: dict) -> bool:
        ts = _parse_iso(record.get("timestamp"))
        return ts is not None and ts < floor

    for record in _read_transcript_tail(log, _stop):
        if record.get("type") != "user":
            continue
        for block in _content_blocks(record):
            if block.get("type") == "tool_result" and block.get("tool_use_id") == tool_use_id:
                return True
    return False


def _conversation_after(log: Path, after: float) -> bool:
    def _stop(record: dict) -> bool:
        ts = _parse_iso(record.get("timestamp"))
        return ts is not None and ts <= after

    for record in _read_transcript_tail(log, _stop):
        if record.get("type") not in ("user", "assistant"):
            continue
        ts = _parse_iso(record.get("timestamp"))
        if ts is not None and ts > after:
            return True
    return False


def _update_marker(path: Path, at: float, mutate) -> bool:
    try:
        with marker_lock(path.parent, path.stem):
            marker = _load(path)
            if not isinstance(marker, dict) or marker.get("at") != at:
                return False
            mutate(marker)
            _write_json_atomic(path, marker)
    except OSError:
        return False
    return True


def _marker_transcript(marker: dict, record: dict, claude_log_cache) -> Path | None:
    named = marker.get("transcript_path")
    if isinstance(named, str) and named and Path(named).is_file():
        return Path(named)
    return _resolve_transcript_path(record, None, claude_log_cache)


def _prompt_status(record: dict, now: float, prompts_dir: Path | None = None,
                   claude_log_cache=None) -> tuple[dict | None, str]:
    marker = None
    try:
        sid = record.get("claude_sid")
        path = (prompts_dir or PERMISSION_PROMPTS) / f"{sid}.json"
        if not sid or not path.exists():
            return None, "none"
        marker = _load(path)
        if not isinstance(marker, dict) or not isinstance(marker.get("at"), (int, float)):
            return None, "unknown"
        at = float(marker["at"])
        decided = marker.get("decided")
        if marker.get("closed_at"):
            return marker, "closed"
        if isinstance(decided, dict):
            if decided.get("by") == "manager":
                return marker, "closed"
            if decided.get("by") == "hook" and now - at < MARKER_HINT_GRACE_SEC:
                return marker, "closed"
        log = _marker_transcript(marker, record, claude_log_cache)
        if log is None or not log.is_file():
            return marker, "unknown"
        tool_use_id = marker.get("tool_use_id")
        if not tool_use_id:
            found = _match_tool_use(log, marker)
            if found:
                _update_marker(path, marker["at"], lambda m: m.update(tool_use_id=found))
                marker["tool_use_id"] = tool_use_id = found
        if tool_use_id:
            if _tool_result_seen(log, tool_use_id, at):
                _update_marker(path, marker["at"], lambda m: m.update(closed_at=now))
                marker["closed_at"] = now
                return marker, "closed"
            return marker, "open"
        if _conversation_after(log, at + MARKER_CLOSE_GRACE_SEC):
            return marker, "closed"
        return marker, "open"
    except Exception:
        return marker, "unknown"


def _open_prompt(record: dict, now: float, prompts_dir: Path | None = None, claude_log_cache=None) -> dict | None:
    marker, status = _prompt_status(record, now, prompts_dir, claude_log_cache)
    return marker if status == "open" else None


def _marker_key(sid: str, marker: dict) -> str:
    digest = hashlib.sha1(str(marker.get("at")).encode("utf-8", "replace")).hexdigest()[:12]
    return f"approval:{sid}:{digest}"


def _scan_account_holds(now: int, emitted: dict, next_emitted: dict, emit, manager_name: str | None,
                        pool: str | None) -> None:
    held = _held_marks(_load_account_state())
    for account, mark in held.items():
        if not isinstance(mark, dict):
            continue
        reason = str(mark.get("reason") or "operator")
        if pool == account:
            new_letter = _maybe_flip_account(account, f"account {account} on hold", now, cooldown=False)
            if new_letter is not None:
                emit("switched", account, "SWITCHED",
                     {"account": account, "new": new_letter, "subject": "account on hold"})
        if reason == "banner":
            records = _load_active_records()
            for record_path, record in records:
                if (record.get("agent") == "worker" and not record.get("nested")
                        and record.get("account") == account
                        and (record.get("runtime") or "claude") == "claude"
                        and _in_scope(record, manager_name, [r for _, r in records])):
                    _archive_child(record_path, record, "account-hold", claim=True)
        key = f"account-hold:{account}:{reason}"
        if _carry_page(emitted, next_emitted, key, now):
            since = datetime.fromtimestamp(_safe_int(mark.get("at")), tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
            emit("account-hold", account, "ACCOUNT_HOLD",
                 {"account": account, "since": since, "reason": reason}, key)
            _notify_macos(f"account {account} on hold")


def _carry_page(emitted: dict, next_emitted: dict, key: str, now) -> bool:
    prev = emitted.get(key)
    prev = prev if isinstance(prev, dict) else {}
    first_seen = prev.get("first_seen")
    if not isinstance(first_seen, (int, float)):
        first_seen = now
    paged = prev.get("paged")
    fresh = not (isinstance(paged, int) and paged > 0)
    next_emitted[key] = {"first_seen": first_seen, "paged": 1}
    return fresh


def _scan_approval_prompts(manager_name, now, emitted, next_emitted, emit) -> None:
    if _get_driver is None:
        return
    targets: list[tuple[str, str, str, dict | None]] = []
    if ACTIVE.is_dir():
        for p in ACTIVE.iterdir():
            if p.suffix != ".json":
                continue
            record = _load(p)
            if record is None or record.get("agent") != "worker":
                continue
            if _is_subworker(record):
                continue
            if not _matches_manager(record, manager_name):
                continue
            if (record.get("runtime") or "claude") != "claude":
                continue
            if record.get("state") != "processing" or record.get("nested"):
                continue
            if is_human_attached(record, now):
                continue
            wid = record.get("window_id") or record.get("iterm_sid") or ""
            if not wid:
                continue
            sid = record.get("claude_sid") or p.stem
            targets.append((sid, record.get("name") or sid, str(wid), record))
    if ASSIGNMENTS_PENDING.is_dir():
        for sidecar in ASSIGNMENTS_PENDING.glob("*.window"):
            try:
                wid = sidecar.read_text().strip()
            except OSError:
                continue
            if not wid:
                continue
            pending = _load(sidecar.with_suffix(".json")) or {}
            if manager_name is not None and _parent_name_of(pending) != manager_name:
                continue
            targets.append((sidecar.stem, pending.get("name") or sidecar.stem, wid, None))
    if not targets:
        return
    try:
        driver = _get_driver()
    except Exception:
        return
    claude_log_cache: dict = {}
    for dedup_id, display, wid, record in targets:
        marker = _open_prompt(record, now, claude_log_cache=claude_log_cache) if record is not None else None
        if marker is not None:
            key = _marker_key(dedup_id, marker)
            if _carry_page(emitted, next_emitted, key, now):
                excerpt = f"{marker.get('tool_name')}: {marker.get('summary')}"
                if len(excerpt) > APPROVAL_EXCERPT_MAX:
                    excerpt = excerpt[:APPROVAL_EXCERPT_MAX - 1] + "…"
                emit("approval", display, "APPROVAL_PROMPT",
                     {"name": display, "excerpt": excerpt, "source": "hook"}, key)
            continue
        try:
            pane_text = driver.capture_screen(wid)
        except Exception:
            continue
        if not pane_text:
            continue
        block = _approval_dialog_block(pane_text)
        if block is None:
            continue
        digest = hashlib.sha1(block.encode("utf-8", "replace")).hexdigest()[:12]
        key = f"approval:{dedup_id}:{digest}"
        if _carry_page(emitted, next_emitted, key, now):
            emit("approval", display, "APPROVAL_PROMPT", {"name": display, "excerpt": _approval_excerpt(block)}, key)


def main(manager_name: str | None = None, emit=None, lanes_in_manager: bool = True) -> int:
    external = emit is not None
    sink = emit if external else _emit
    if not external:
        _lane_preflight()
    now = int(time.time())
    emitted_state_path = _emitted_state_path(manager_name)
    emitted = _load_emitted_state(emitted_state_path)
    next_emitted: dict = {}
    question_asked_at = _pending_question_times()
    blocked_sids = set(question_asked_at)
    events: list[tuple[str, str, str | None, str | None, dict]] = []

    def emit(kind: str, name: str, lane_kind: str, fields: dict, dedup_key: str | None = None,
             sid: str | None = None) -> None:
        line = _render_line(lane_kind, fields)
        if line is None:
            _journal_silent(lane_kind, fields, sid)
        events.append((kind, name, line, dedup_key, fields))

    claude_log_cache: dict = {}
    codex_log_cache = emitted.get("codex_log_cache")
    codex_log_cache = dict(codex_log_cache) if isinstance(codex_log_cache, dict) else {}
    seen_codex_sids: set[str] = set()
    last_autoclose = emitted.get("last_autoclose_run")
    if isinstance(last_autoclose, (int, float)) and now - last_autoclose < AUTOCLOSE_CADENCE_SEC:
        should_run_autoclose = False
        next_emitted["last_autoclose_run"] = last_autoclose
    else:
        should_run_autoclose = True
        next_emitted["last_autoclose_run"] = now
    current_uptime = _awake_seconds()
    idle_sends_left = IDLE_UNTASKED_MAX_SENDS
    pane_memo: dict = {
        "prev": {k[len(PANE_TURN_PREFIX):]: v for k, v in emitted.items()
                 if k.startswith(PANE_TURN_PREFIX) and isinstance(v, str)},
        "seen": {},
        "input": {},
        "screens": {},
    }
    _index_memo: list = []

    def process_index():
        if not _index_memo:
            try:
                _index_memo.append(_process_index())
            except Exception:
                _index_memo.append(None)
        return _index_memo[0]

    idle_branch_ran: set = set()
    pool = _pool_account()
    try:
        _scan_usage_cap(now, emitted, next_emitted, emit, manager_name)
    except Exception as e:
        print(f"stale_monitor: usage-cap scan failed ({e})", file=sys.stderr)
    try:
        _scan_fable_moves(now, emitted, next_emitted, emitted_state_path, emit)
    except Exception as e:
        print(f"stale_monitor: fable-move scan failed ({e})", file=sys.stderr)
    active_records = _load_active_records()
    active_index = [r for _, r in active_records]
    live_sids = {
        (r.get("claude_sid") or rp.stem)
        for rp, r in active_records
        if isinstance(r, dict) and not r.get("nested") and not _is_subworker(r)
        and _in_scope(r, manager_name, active_index)
    }
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
        if mgr_record is not None:
            _model_credit_action(
                mgr_record, mgr_path, emitted, next_emitted, emitted_state_path, now,
                emit, None, pane_memo, 1, NUDGE_TEXT, manager_fallback_model(), True,
                effort=MANAGER_MODEL_FALLBACK_EFFORT,
                claude_log_cache=claude_log_cache)
        if (mgr_record is not None and mgr_record.get("state") == "processing"
                and (mgr_record.get("runtime") or "claude") == "claude"):
            try:
                mgr_mtime = int(mgr_path.stat().st_mtime)
            except OSError:
                mgr_mtime = None
            if mgr_mtime is not None:
                mgr_sid = mgr_record.get("claude_sid") or mgr_path.stem
                mgr_sched_key = f"scheduled:{mgr_sid}"
                mgr_activity, mgr_log = _last_activity(mgr_record, mgr_mtime, codex_log_cache,
                                                       claude_log_cache)
                banner = None
                auth_fail = None
                hold = None
                if now - mgr_activity >= MANAGER_LIMIT_CHECK_FLOOR_SEC:
                    banner = _limit_banner_text(mgr_log, strict=True)
                    if banner is None:
                        auth_fail = _auth_failure_signature(mgr_log)
                    if banner is None and auth_fail is None and pool is not None:
                        hold = _hold_banner(mgr_log)
                        if not _banner_after_launch(mgr_record, hold):
                            hold = None
                if banner is not None:
                    manager_limited = True
                    if pool is not None and not _is_transient_throttle(banner):
                        account = _account_of(mgr_record, pool)
                        reset_ts = _parse_limit_reset_ts(banner, now)
                        if reset_ts is None:
                            _ledger_banner_event("unparsed-banner", banner,
                                                 f"manager:{manager_name}",
                                                 now, emitted, next_emitted)
                        _record_brick(account, reset_ts, f"manager:{manager_name}", now)
                        _manager_recovery_step(mgr_record, mgr_sid, account, pool, now, emitted,
                                               next_emitted, emit, manager_name,
                                               subject=f"manager {manager_name} limited")
                    elif pool is not None:
                        _ledger_banner_event("transient-throttle", banner,
                                             f"manager:{manager_name}",
                                             now, emitted, next_emitted)
                    sched = _load_scheduled(emitted, mgr_sched_key) if AUTONUDGE else None
                    if AUTONUDGE and sched is None:
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
                            if _send_text(mgr_window, MANAGER_NUDGE_TEXT, pane_memo):
                                emit("manager-nudged", manager_name, "NUDGED",
                                     {"name": manager_name, "why": "limit-reset", "repeat": False})
                        next_emitted[mgr_sched_key] = {
                            "at": now + MANAGER_NUDGE_RETRY_SEC,
                            "baseline": mgr_activity,
                        }
                    elif sched is not None:
                        next_emitted[mgr_sched_key] = sched
                elif auth_fail is not None and pool is not None:
                    manager_limited = True
                    account = _account_of(mgr_record, pool)
                    auth_uuid, _auth_text = auth_fail
                    auth_key = f"auth-recovery:{mgr_sid}"
                    if auth_key in emitted:
                        next_emitted[auth_key] = emitted[auth_key]
                    decision, attempts = _record_auth_401(account, auth_uuid, now)
                    pool_suspect = _auth_401_active(pool, now)
                    healthy_target = _healthy_takeover_target(
                        account, pool, pool_suspect=pool_suspect)
                    if (attempts <= AUTH_401_SAME_ACCOUNT_ATTEMPTS
                            and (pool == account or not pool_suspect)):
                        recover_target = pool
                    else:
                        recover_target = healthy_target
                    if decision == "recover" and recover_target is None:
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
                            emit("switched", manager_name, "SWITCHED",
                                 {"account": account, "new": new_letter,
                                  "subject": f"manager {manager_name} auth-401 credential suspect"})
                        emit("auth-escalate", manager_name, "AUTH_401_ESCALATED",
                             {"account": account, "subject": f"manager {manager_name}",
                              "fix": _login_fix_command(account)})
                        target = _healthy_takeover_target(account, pool, new_letter,
                                                          pool_suspect=pool_suspect)
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
                elif hold is not None:
                    account = _held_account_of(mgr_record)
                    if account is not None:
                        manager_limited = True
                        _record_hold(account, "banner", f"manager:{manager_name}", now)
                        _manager_recovery_step(mgr_record, mgr_sid, account, pool, now, emitted,
                                               next_emitted, emit, manager_name,
                                               subject=f"manager {manager_name} on hold", cooldown=False)
    if ACTIVE.is_dir():
        for p in ACTIVE.iterdir():
            if p.suffix != ".json":
                continue
            record = _load(p)
            if record is None:
                continue
            if record.get("nested"):
                continue
            if not _in_scope(record, manager_name, active_index):
                continue
            _note_model_spent(record, claude_log_cache)
            if _is_subworker(record):
                if _is_interactive_child(record):
                    if _fable_banner_ts(record, claude_log_cache) is not None:
                        credit = _fable_limit_action(
                            record, p, _fable_banner_ts(record, claude_log_cache), emitted, next_emitted,
                            emitted_state_path, now, emit, blocked_sids, pane_memo, idle_sends_left,
                            claude_log_cache)
                        if credit == MODEL_CREDIT_TYPED:
                            idle_sends_left = max(idle_sends_left - 1, 0)
                            continue
                    safeguard = _safeguard_pause_action(
                        record, p, emitted, next_emitted, emitted_state_path, now, emit,
                        blocked_sids, idle_sends_left, claude_log_cache=claude_log_cache)
                    if safeguard == SAFEGUARD_TYPED:
                        idle_sends_left = max(idle_sends_left - 1, 0)
                continue
            credit = MODEL_CREDIT_NONE
            safeguard = SAFEGUARD_NONE
            if record.get("agent") != "manager":
                credit = _model_credit_action(
                    record, p, emitted, next_emitted, emitted_state_path, now, emit,
                    blocked_sids, pane_memo, idle_sends_left,
                    claude_log_cache=claude_log_cache)
                if credit == MODEL_CREDIT_TYPED:
                    idle_sends_left = max(idle_sends_left - 1, 0)
                safeguard = _safeguard_pause_action(
                    record, p, emitted, next_emitted, emitted_state_path, now, emit,
                    blocked_sids, idle_sends_left, claude_log_cache=claude_log_cache)
                if safeguard == SAFEGUARD_TYPED:
                    idle_sends_left = max(idle_sends_left - 1, 0)
            pane_held = bool(credit) or bool(safeguard)
            state = record.get("state")
            if state == "processing":
                if record.get("agent") != "worker":
                    continue
                try:
                    mtime = int(p.stat().st_mtime)
                except OSError:
                    continue
                sid = record.get("claude_sid") or p.stem
                stretch_nudge_key = f"nudged:{sid}:{mtime}"
                if stretch_nudge_key in emitted:
                    next_emitted[stretch_nudge_key] = emitted[stretch_nudge_key]
                nudge_sent_key = f"nudge_sent:{sid}"
                sent_at = emitted.get(nudge_sent_key)
                if not isinstance(sent_at, (int, float)):
                    sent_at = None
                sched_key = f"scheduled:{sid}"
                sched = _load_scheduled(emitted, sched_key)
                sched_due = sched is not None and now >= sched["at"]
                if sched is not None and not sched_due:
                    next_emitted[sched_key] = sched
                if (record.get("runtime") or "claude") == "codex":
                    seen_codex_sids.add(sid)
                name = record.get("name", "")
                window_id = record.get("window_id") or ""
                runtime = record.get("runtime") or "claude"
                attached = is_human_attached(record, now)
                nudge_eligible = (
                    AUTONUDGE
                    and sid not in blocked_sids
                    and bool(window_id)
                    and not attached
                )
                may_type = nudge_eligible and not pane_held
                turn_elapsed = now - mtime
                pool_needs_transcript = (
                    pool is not None
                    and (record.get("runtime") or "claude") == "claude"
                )
                activity_gate = (min(PROCESSING_THRESHOLD_SEC, RATE_LIMIT_NUDGE_SEC)
                                 if nudge_eligible or pool_needs_transcript
                                 else PROCESSING_THRESHOLD_SEC)
                if sent_at is None and not sched_due and turn_elapsed < activity_gate:
                    continue
                activity, log = _last_activity(record, mtime, codex_log_cache,
                                               claude_log_cache)
                child_activity = _children_activity(record, active_index, codex_log_cache,
                                                    seen_codex_sids)
                if child_activity > activity:
                    activity = child_activity
                unanswered = False
                if sent_at is not None:
                    if activity > sent_at:
                        emit("resumed", name, "RESUMED", {"name": name}, sid=sid)
                    else:
                        next_emitted[nudge_sent_key] = sent_at
                        unanswered = True
                fired_scheduled = False
                if sched_due:
                    still_bannered = _limit_banner_text(log) is not None
                    if (activity <= sched["baseline"] or still_bannered) and may_type:
                        if _pane_input_idle(window_id, pane_memo, runtime):
                            _record_action_ahead(emitted_state_path, emitted,
                                                 next_emitted, nudge_sent_key, now)
                            if _send_text(window_id, NUDGE_TEXT, pane_memo, runtime, sid):
                                emit("nudged", name, "NUDGED",
                                     {"name": name, "why": "limit-reset", "repeat": False}, sid=sid)
                                fired_scheduled = True
                            else:
                                next_emitted[sched_key] = sched
                        else:
                            next_emitted[sched_key] = sched
                elapsed = now - activity
                banner = None
                banner_read = False
                if pool_needs_transcript and elapsed >= RATE_LIMIT_NUDGE_SEC:
                    banner = _limit_banner_text(log)
                    banner_read = True
                    if banner is not None and _is_transient_throttle(banner):
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
                            emit("switched", name, "SWITCHED",
                                 {"account": account, "new": new_letter, "subject": f"worker {name} limited"})
                    else:
                        auth_sig = _auth_failure_signature(log)
                        if auth_sig is None:
                            hold = _hold_banner(log)
                            held_account = (_held_account_of(record)
                                            if _banner_after_launch(record, hold) else None)
                            if held_account is not None:
                                _record_hold(held_account, "banner", f"worker:{name}", now)
                                if held_account in _held_marks(_load_account_state()):
                                    _archive_child(p, record, "account-hold", claim=True)
                                    continue
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
                                    emit("switched", name, "SWITCHED",
                                         {"account": account, "new": new_letter,
                                          "subject": f"worker {name} auth-401 credential suspect"})
                                emit("auth-escalate", name, "AUTH_401_ESCALATED",
                                     {"account": account, "subject": f"worker {name}",
                                      "fix": _login_fix_command(account)})
                                next_emitted[auth_emit_key] = now
                            elif decision == "recover" or reemit_due:
                                if decision == "recover":
                                    _append_account_ledger({
                                        "ts": now, "event": "auth-401", "account": account,
                                        "action": "recover", "source": f"worker:{name}",
                                        "from_sid": sid, "by": "stale_monitor"})
                                emit("auth-recover", name, "AUTH_401", {"name": name, "account": account},
                                     auth_emit_key, sid=sid)
                                next_emitted[auth_emit_key] = now
                            elif isinstance(last_emit, (int, float)):
                                next_emitted[auth_emit_key] = last_emit
                if attached:
                    continue
                asked_at = question_asked_at.get(sid)
                if not pane_held and asked_at is not None and asked_at >= _input_bound(record):
                    continue
                if elapsed >= PROCESSING_THRESHOLD_SEC:
                    elapsed_min = elapsed // 60
                    threshold = (_highest_nudge_threshold(elapsed_min, PROCESSING_THRESHOLD_MIN)
                                 if nudge_eligible
                                 else _highest_threshold(elapsed_min, PROCESSING_THRESHOLD_MIN))
                    if threshold is not None:
                        key = f"processing:{sid}:{mtime}"
                        next_emitted[key] = threshold
                        last = emitted.get(key)
                        if not (isinstance(last, int) and last >= threshold):
                            if _prompt_holds(record, now, pane_memo, claude_log_cache):
                                continue
                            typed = False
                            if may_type and not fired_scheduled:
                                if _pane_input_idle(window_id, pane_memo, runtime):
                                    _record_action_ahead(emitted_state_path, emitted,
                                                         next_emitted, nudge_sent_key, now)
                                    typed = _send_text(window_id, NUDGE_TEXT, pane_memo,
                                                       runtime, sid)
                                    if typed:
                                        emit("nudged", name, "NUDGED",
                                             {"name": name, "why": f"{elapsed_min}min", "repeat": unanswered},
                                             sid=sid)
                            if not (may_type and (typed or fired_scheduled)) and safeguard != SAFEGUARD_TYPED:
                                emit("stalled", name, "STALE_PROCESSING", {"name": name, "minutes": elapsed_min},
                                     key, sid=sid)
                elif (may_type and not fired_scheduled and sched is None
                      and elapsed >= RATE_LIMIT_NUDGE_SEC
                      and stretch_nudge_key not in emitted
                      and (record.get("runtime") or "claude") == "claude"):
                    if not banner_read:
                        banner = _limit_banner_text(log)
                    if banner is not None and _pane_input_idle(window_id, pane_memo, runtime):
                        _record_action_ahead(emitted_state_path, emitted,
                                             next_emitted, stretch_nudge_key, now)
                        _record_action_ahead(emitted_state_path, emitted,
                                             next_emitted, nudge_sent_key, now)
                        if _send_text(window_id, NUDGE_TEXT, pane_memo, runtime, sid):
                            emit("nudged", name, "NUDGED",
                                 {"name": name, "why": f"{elapsed // 60}min rate-limited", "repeat": unanswered},
                                 sid=sid)
                            reset_ts = _parse_limit_reset_ts(banner, now)
                            if reset_ts is not None:
                                next_emitted[sched_key] = {"at": reset_ts,
                                                           "baseline": activity}
                continue
            if state != "idle" or record.get("agent") != "worker":
                continue
            if is_human_attached(record, now):
                continue
            idle_branch_ran.add(record.get("claude_sid") or p.stem)
            idle_elapsed = _compute_idle_elapsed_sec(record, current_uptime, now)
            reaping = (not pane_held and should_run_autoclose and idle_elapsed is not None
                       and idle_elapsed > IDLE_THRESHOLD_SEC
                       and (record.get("claude_sid") or p.stem) not in blocked_sids
                       and not _is_delegation_live(record, records=active_index)
                       and not _autoclose_held(record, idle_elapsed, process_index,
                                               pane_memo))
            tracked = (None if reaping else
                       _idle_untasked_action(record, p, blocked_sids,
                                             current_uptime, process_index, now,
                                             pane_memo, records=active_index))
            if tracked is not None:
                untasked_key, untasked_min, untasked_stamps = tracked
                untasked_tasked_at, untasked_prompted_at = untasked_stamps
                page_key = f"untasked-page:{untasked_key.split(':', 1)[1]}"

                def _still_spent(value) -> bool:
                    if not isinstance(value, (int, float)):
                        return False
                    if untasked_tasked_at > value:
                        return False
                    return untasked_prompted_at <= value + _turn_end_grace_sec()

                spent = [(k, emitted.get(k)) for k in (untasked_key, page_key)
                         if _still_spent(emitted.get(k))]
                for carry_key, carry_value in spent:
                    next_emitted[carry_key] = carry_value
                if not spent and untasked_min is not None:
                    worker_name = record.get("name") or ""
                    pane = record.get("window_id") or record.get("iterm_sid") or ""
                    runtime = record.get("runtime") or "claude"
                    if (AUTONUDGE and pane and idle_sends_left > 0
                            and _pane_input_idle(pane, pane_memo, runtime)):
                        _record_action_ahead(emitted_state_path, emitted,
                                             next_emitted, untasked_key, now)
                        if _send_text_checked(pane, IDLE_NUDGE_TEXT, pane_memo,
                                              runtime, record.get("claude_sid") or p.stem):
                            idle_sends_left -= 1
                            emit("untasked", worker_name, "IDLE_NUDGED",
                                 {"name": worker_name, "minutes": untasked_min},
                                 sid=record.get("claude_sid") or p.stem)
                        else:
                            next_emitted.pop(untasked_key, None)
                            next_emitted[page_key] = now
                            _commit_actions_only(emitted_state_path, emitted,
                                                 next_emitted)
                            emit("untasked", worker_name, "IDLE_UNTASKED",
                                 {"name": worker_name, "minutes": untasked_min, "variant": "no-input"},
                                 page_key, sid=record.get("claude_sid") or p.stem)
                    elif AUTONUDGE and pane and not _pane_input_idle(pane, pane_memo, runtime):
                        next_emitted[page_key] = now
                        emit("untasked", worker_name, "IDLE_UNTASKED",
                             {"name": worker_name, "minutes": untasked_min, "variant": "unsent-text"},
                             page_key, sid=record.get("claude_sid") or p.stem)
                    elif not (AUTONUDGE and pane):
                        next_emitted[page_key] = now
                        emit("untasked", worker_name, "IDLE_UNTASKED",
                             {"name": worker_name, "minutes": untasked_min, "variant": "nothing-typed"},
                             page_key, sid=record.get("claude_sid") or p.stem)
            if pane_held or not should_run_autoclose:
                continue
            sid = record.get("claude_sid")
            if sid in blocked_sids:
                continue
            elapsed = _compute_idle_elapsed_sec(record, current_uptime, now)
            if elapsed is None:
                continue
            if elapsed > IDLE_THRESHOLD_SEC:
                if _is_delegation_live(record, records=active_index):
                    continue
                _autoclose_idle_worker(p, record, elapsed, pane_memo)
    os_windows = _list_worker_windows()
    _scan_orphan_windows(now, emitted, next_emitted, emit, os_windows)
    try:
        _scan_children(now, emitted, next_emitted, emit, manager_name, _load_active_records(),
                       os_windows)
    except Exception as e:
        print(f"stale_monitor: child scan failed ({e})", file=sys.stderr)
    try:
        _scan_context(now, emitted, next_emitted, emit, active_records, manager_name)
    except Exception as e:
        print(f"stale_monitor: context scan failed ({e})", file=sys.stderr)
    try:
        _scan_approval_prompts(manager_name, now, emitted, next_emitted, emit)
    except Exception as e:
        print(f"stale_monitor: approval scan failed ({e})", file=sys.stderr)
    if manager_name:
        for lane_key, lane_fields in _lane_silence_events(
                manager_name, emitted, next_emitted, now, lanes_in_manager=lanes_in_manager):
            emit("lane_silent", lane_key, "LANE_SILENT", lane_fields, lane_key)
    pruned_cache = {s: p for s, p in codex_log_cache.items() if s in seen_codex_sids}
    if pruned_cache:
        next_emitted["codex_log_cache"] = pruned_cache
    try:
        _scan_account_holds(now, emitted, next_emitted, emit, manager_name, pool)
    except Exception as e:
        print(f"stale_monitor: account-hold scan failed ({e})", file=sys.stderr)
    try:
        if manager_name:
            flag_path = _limited_flag_path(manager_name)
            buffer = emitted.get("limited_buffer")
            if not isinstance(buffer, dict):
                buffer = None
            if manager_limited:
                buf = buffer or {"since": now, "stalled_names": [], "nudged": 0,
                                 "resumed": 0, "orphaned": 0}
                if not isinstance(buf.get("stalled_names"), list):
                    buf["stalled_names"] = []
                if not isinstance(buf.get("suppressed_keys"), list):
                    buf["suppressed_keys"] = []
                for kind, event_name, line, dedup_key, fields in events:
                    if kind in ("stalled", "nudged") and event_name:
                        if event_name not in buf["stalled_names"] and len(buf["stalled_names"]) < 50:
                            buf["stalled_names"].append(event_name)
                    if kind == "switched":
                        buf["switched"] = _head_of("SWITCHED", fields)
                    if kind == "auth-escalate" and line is not None:
                        pages = buf.setdefault("auth_pages", [])
                        if isinstance(pages, list) and line not in pages and len(pages) < 20:
                            pages.append(line)
                    if (line is not None and dedup_key and dedup_key not in buf["suppressed_keys"]
                            and len(buf["suppressed_keys"]) < 200):
                        buf["suppressed_keys"].append(dedup_key)
                    counter = {"nudged": "nudged", "resumed": "resumed",
                               "orphaned-child": "orphaned"}.get(kind)
                    if counter:
                        buf[counter] = _safe_int(buf.get(counter)) + 1
                for carried_key in next_emitted:
                    if (carried_key.startswith(REPLAYED_PAGE_PREFIXES) and carried_key not in buf["suppressed_keys"]
                            and len(buf["suppressed_keys"]) < 200):
                        buf["suppressed_keys"].append(carried_key)
                next_emitted["limited_buffer"] = buf
                try:
                    flag_path.touch()
                except OSError:
                    pass
            else:
                printed_any = False
                if buffer is not None or flag_path.exists():
                    rollup = _build_rollup_line(buffer or {}, manager_name, now)
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
                    sink(rollup)
                    flag_path.unlink(missing_ok=True)
                    printed_any = True
                    for page in (buffer or {}).get("auth_pages") or []:
                        if isinstance(page, str):
                            sink(page)
                for _kind, _event_name, line, _dedup_key, _fields in events:
                    if line is None:
                        continue
                    sink(line)
                    printed_any = True
                if not external:
                    if printed_any:
                        _drain_outbox(manager_name)
                    else:
                        oldest = _outbox_oldest_ts(manager_name)
                        if oldest is not None and now - oldest >= OUTBOX_MAX_HOLD_SEC:
                            _drain_outbox(manager_name)
        else:
            for _kind, _event_name, line, _dedup_key, _fields in events:
                if line is not None:
                    sink(line)
    except LaneDead:
        _commit_actions_only(emitted_state_path, emitted, next_emitted)
        raise
    for pane_id, pane_line in pane_memo["seen"].items():
        if pane_line is not None:
            next_emitted[f"{PANE_TURN_PREFIX}{pane_id}"] = pane_line
    _carry_idle_keys_when_the_idle_branch_did_not_run(
        emitted, next_emitted, idle_branch_ran, live_sids)
    cursor_written = True
    try:
        _write_json_atomic(emitted_state_path, next_emitted)
    except Exception as e:
        cursor_written = False
        print(f"stale_monitor: failed to write {emitted_state_path} ({e})", file=sys.stderr)
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
