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
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from dockwright.terminal import get_driver as _get_driver
except Exception:  # pragma: no cover - venv editable install expected in prod
    _get_driver = None


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
QUESTION_THRESHOLD_MIN = 2
PROCESSING_THRESHOLD_SEC = PROCESSING_THRESHOLD_MIN * 60
QUESTION_THRESHOLD_SEC = QUESTION_THRESHOLD_MIN * 60
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
)
APPROVAL_OPTION_MARKERS = ("❯ 1.", "1. yes")
APPROVAL_TAIL_LINES = 40
APPROVAL_EXCERPT_MAX = 160
APPROVAL_REPAGE_BASE_MIN = 5

HOME = Path(os.environ.get("HOME", ""))


def _prefer_new(new: Path, legacy: Path) -> Path:
    if new.exists():
        return new
    if legacy.exists():
        return legacy
    return new


ROOT = _prefer_new(HOME / ".claude" / "dockwright", HOME / ".claude" / "orchestrator")
_LEGACY_ROOT = HOME / ".claude" / "orchestrator"
ACTIVE = ROOT / "active"
QUESTIONS = ROOT / "questions"
CLOSED = ROOT / "closed"
ASSIGNMENTS_PENDING = ROOT / "assignments" / ".pending"
GARDENER_LIVE_WINDOWS = ROOT / "gardener" / "live-windows"
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CODEX_SESSIONS = HOME / ".codex" / "sessions"
RATE_LIMIT_SIGNATURES = ("temporarily limiting requests", "hit your session limit")
TRANSIENT_THROTTLE_SIGNATURES = ("temporarily limiting requests", "not your usage limit")
TRANSIENT_SERVER_ERROR_SIGNATURES = ("529 overloaded",)
AUTH_FAILURE_SIGNATURES = ("api error: 401", "please run /login")
AUTH_401_WINDOW_SEC = 5 * 60
AUTH_401_MAX_ATTEMPTS = 2
AUTH_401_SAME_ACCOUNT_ATTEMPTS = 1
AUTH_401_REEMIT_SEC = 5 * 60
AUTONUDGE = os.environ.get("CLAUDE_ORCH_AUTONUDGE") == "1"
OUTBOX_DIVERT_KINDS = ("autoclosed",)
OUTBOX_MAX_HOLD_SEC = _env_positive_int("CLAUDE_ORCH_OUTBOX_MAX_HOLD_SEC", 1800)
NUDGE_TEXT = "[MANAGER] resume your task"
MANAGER_NUDGE_TEXT = "rate limit cleared — check list_workers and queued events, resume orchestration"
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


def _account_config_prefix(letter: str) -> str:
    _names, default, dirs = _registry()
    effective = letter
    config_dir = None
    if letter != default:
        farm = Path(dirs.get(letter) or os.path.expanduser(f"~/.claude-{letter}"))
        cj = farm / ".claude.json"
        try:
            data = json.loads(cj.read_text())
            servers = (data.get("mcpServers") or {}) if isinstance(data, dict) else {}
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
def _flip_lock():
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
                and not _entry_bricked(state.get("accounts", {}).get(name), now)
                and not _auth_401_active(name, now, state)):
            return name
    return None


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


def _maybe_flip_account(bricked_account: str, reason: str, now: int) -> str | None:
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


def _launch_recovery_manager(mgr_record: dict, mgr_sid: str, new_letter: str) -> str | None:
    cwd = mgr_record.get("cwd") or os.path.expanduser("~")
    name = mgr_record.get("name") or ""
    settings_path = ROOT / "presets" / "manager-settings.json"
    settings_arg = (f"--settings {shlex.quote(str(settings_path))} "
                    if settings_path.is_file() else "")
    rc_arg = ("--remote-control "
              if os.environ.get("DOCKWRIGHT_MANAGER_RC", "").strip() != "0" else "")
    skip_arg = ("--dangerously-skip-permissions "
                if os.environ.get("DOCKWRIGHT_MANAGER_SKIP_PERMS", "") == "1"
                else "")
    inner = (
        f"{_account_config_prefix(new_letter)}"
        f"CLAUDE_AGENT=manager CLAUDE_WORKER_NAME={shlex.quote(name)} "
        f"DOCKWRIGHT_PENDING_TAKEOVER=1 "
        f"claude {rc_arg}{skip_arg}--model {shlex.quote('claude-opus-5[1m]')} {settings_arg}"
        f"{shlex.quote(f'/manager-takeover-recovery {mgr_sid}')}"
    )
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
    bucket = name.replace("/", "_").replace("\\", "_")
    return f"_{bucket}" if bucket in (".", "..") else bucket


def _emitted_state_path(manager_name: str | None) -> Path:
    if not manager_name:
        return ROOT / ".stale-emitted.json"
    return ROOT / f".stale-emitted-{_safe_bucket(manager_name)}.json"


def _matches_manager(record: dict, manager_name: str | None) -> bool:
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
    if not window_id or _get_driver is None:
        return
    try:
        _get_driver().send_text(window_id, text)
    except Exception:
        pass


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


def _resolve_transcript_path(record: dict, codex_log_cache: dict | None = None) -> Path | None:
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


def _is_delegation_live(record: dict, log: Path | None = None) -> bool:
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
            return None
        return {"command_by_pid": command_by_pid, "child_commands": child_commands}
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
                   codex_log_cache: dict | None = None) -> tuple[int, Path | None]:
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


def _last_assistant_event(log_path: Path, max_bytes: int = 65536) -> dict | None:
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
)
ACTION_KEY_EXACT = ("last_autoclose_run", "codex_log_cache", "limited_buffer",
                    "lane_check_tick")
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


LANE_INTERVALS = {"questions": 2, "done": 2, "turn-ends": 5, "stale": 60}
LANE_HEARTBEAT_STALE_INTERVALS = 3
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
                         now: float) -> list[tuple[str, str]]:
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

        for lane, interval in LANE_INTERVALS.items():
            if lane == "stale":
                continue
            record = _load(_lane_heartbeat_path(manager_name, lane))
            last_scan = record.get("last_scan") if isinstance(record, dict) else None
            if not isinstance(last_scan, (int, float)) or last_scan <= 0:
                continue
            silent = now - last_scan
            if silent <= interval * LANE_HEARTBEAT_STALE_INTERVALS:
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
            out.append((key,
                        f"LANE_SILENT {lane} — no scan for {int(silent // 60)}min "
                        f"(expected every {interval}s). Events are NOT reaching you "
                        f"on that lane. Re-arm it and kill the old loop process."))
    except Exception as e:
        print(f"stale_monitor: lane liveness check failed ({e})", file=sys.stderr)
    return out


def _outbox_write(manager_name: str, kind: str, line: str, now: float, seq: int) -> None:
    try:
        target = _outbox_dir(manager_name) / f"{int(now * 1000)}-{os.getpid()}-{seq}.json"
        _write_json_atomic(target, {"line": line, "kind": kind, "buffered_at": now})
    except Exception as e:
        _emit(line)
        print(f"stale_monitor: outbox write failed ({e}); printed instead",
              file=sys.stderr)


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


def _autoclose_idle_worker(record_path: Path, record: dict,
                           elapsed_sec: int) -> str | None:
    if elapsed_sec <= _busy_shell_deadline():
        if _is_transcript_live(record) or _transcript_unreadable(record):
            return None
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
    record_path.unlink(missing_ok=True)
    _close_window(window_id)
    return f"AUTOCLOSED {name} idle {elapsed_sec // 60}min"


def _scan_orphan_windows(now: int, emitted: dict, next_emitted: dict, emit) -> None:
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
                emit("orphan-window", pane_id,
                     f"ORPHAN_WINDOW {pane_id} tab={title!r} ({elapsed // 60}min) "
                     f"— no backing active record",
                     key)
        next_emitted[key] = entry


def _approval_dialog_block(pane_text: str) -> str | None:
    lines = pane_text.splitlines()[-APPROVAL_TAIL_LINES:]
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


def _scan_approval_prompts(manager_name, now, emitted, next_emitted, emit) -> None:
    if _get_driver is None:
        return
    targets: list[tuple[str, str, str]] = []
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
            continue
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
            entry["paged"] = 1
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
    _lane_preflight()
    now = int(time.time())
    emitted_state_path = _emitted_state_path(manager_name)
    emitted = _load_emitted_state(emitted_state_path)
    next_emitted: dict = {}
    blocked_sids = _pending_question_sids()
    events: list[tuple[str, str, str, str | None]] = []

    def emit(kind: str, name: str, line: str, dedup_key: str | None = None) -> None:
        events.append((kind, name, line, dedup_key))

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
    pool = _pool_account()
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
                    manager_limited = True
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
                            new_letter = (_maybe_flip_account(
                                account, f"manager {manager_name} limited", now)
                                if account == pool else None)
                            already_flipped = account != pool
                            if new_letter is None and not already_flipped:
                                already_flipped = _recent_flip_landed_on(pool, now)
                            if new_letter is not None or already_flipped:
                                if new_letter is not None:
                                    emit("switched", manager_name,
                                         f"SWITCHED account {account}→{new_letter} "
                                         f"(manager {manager_name} limited)")
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
                            _send_text(mgr_window, MANAGER_NUDGE_TEXT)
                            emit("manager-nudged", manager_name,
                                 f"NUDGED {manager_name} (limit-reset)")
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
                            emit("switched", manager_name,
                                 f"SWITCHED account {account}→{new_letter} "
                                 f"(manager {manager_name} auth-401 credential suspect)")
                        emit("auth-escalate", manager_name,
                             f"AUTH_401_ESCALATED {account} (manager {manager_name}) — "
                             f"login suspect after repeated 401s; PAGE: run "
                             f"{_login_fix_command(account)}, then /login")
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
    if ACTIVE.is_dir():
        for p in ACTIVE.iterdir():
            if p.suffix != ".json":
                continue
            record = _load(p)
            if record is None:
                continue
            if record.get("nested"):
                continue
            if not _matches_manager(record, manager_name):
                continue
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
                nudge_eligible = (
                    AUTONUDGE
                    and sid not in blocked_sids
                    and bool(window_id)
                )
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
                activity, log = _last_activity(record, mtime, codex_log_cache)
                if sent_at is not None:
                    if activity > sent_at:
                        emit("resumed", name, f"RESUMED {name}")
                    else:
                        next_emitted[nudge_sent_key] = sent_at
                fired_scheduled = False
                if sched_due:
                    still_bannered = _limit_banner_text(log) is not None
                    if (activity <= sched["baseline"] or still_bannered) and nudge_eligible:
                        _record_action_ahead(emitted_state_path, emitted,
                                             next_emitted, nudge_sent_key, now)
                        _send_text(window_id, NUDGE_TEXT)
                        emit("nudged", name, f"NUDGED {name} (limit-reset)")
                        fired_scheduled = True
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
                            emit("switched", name,
                                 f"SWITCHED account {account}→{new_letter} "
                                 f"(worker {name} limited)")
                    else:
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
                                next_emitted[auth_emit_key] = last_emit
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
                            if nudge_eligible:
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
                line = _autoclose_idle_worker(p, record, elapsed)
                if line is None:
                    continue
                emit("autoclosed", record.get("name") or "", line)
    if QUESTIONS.is_dir():
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
    if manager_name:
        for lane_key, lane_line in _lane_silence_events(
                manager_name, emitted, next_emitted, now):
            emit("lane_silent", lane_key, lane_line, lane_key)
    pruned_cache = {s: p for s, p in codex_log_cache.items() if s in seen_codex_sids}
    if pruned_cache:
        next_emitted["codex_log_cache"] = pruned_cache
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
                    if kind in ("stalled", "nudged") and event_name:
                        if event_name not in buf["stalled_names"] and len(buf["stalled_names"]) < 50:
                            buf["stalled_names"].append(event_name)
                    if kind == "switched":
                        buf["switched"] = line.removeprefix("SWITCHED ")
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
                    _emit(rollup)
                    flag_path.unlink(missing_ok=True)
                    printed_any = True
                    for page in (buffer or {}).get("auth_pages") or []:
                        if isinstance(page, str):
                            _emit(page)
                direct = [e for e in events if e[0] not in OUTBOX_DIVERT_KINDS]
                diverted = [e for e in events if e[0] in OUTBOX_DIVERT_KINDS]
                for _kind, _event_name, line, _dedup_key in direct:
                    _emit(line)
                    printed_any = True
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
