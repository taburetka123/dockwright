import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Tuple

from . import config, models, paths, trust
from .codex_translate import parse_frontmatter
from .denied_tools import disallowed_tools_arg
from .terminal import get_driver, WORKERS_OS_WINDOW_CLASS


logger = logging.getLogger(__name__)

DEFAULT_RUNTIME = "claude"
SUPPORTED_RUNTIMES = {"claude", "codex"}
CODEX_DEFAULT_ARGS = [
    "--ask-for-approval",
    "never",
    "--sandbox",
    "danger-full-access",
    "--dangerously-bypass-hook-trust",
]
CODEX_WORKER_BOOTSTRAP_PROMPT = """You are an orchestrator worker running in a separate tmux window. Do not ask the human directly.
If you need a human decision, call `ask_manager(claude_sid, question)`. If it returns a `NO_ANSWER_YET:` sentinel, the question is still pending — call ask_manager again with the same question plus the resume_question_id named in the sentinel; never proceed without the answer.
Use your session id as `claude_sid`; in Codex, run `echo $CODEX_THREAD_ID` if you need to inspect it.
When the task is complete, call `worker_done(claude_sid, summary)` as your final action."""
CODEX_DISALLOWED_EXTRA_ARGS = {
    "--settings",
    "--dangerously-skip-permissions",
    "--permission-mode",
    "--resume",
    "-r",
    "--continue",
}
CODEX_PROTECTED_DEFAULT_ARGS = {
    "--ask-for-approval",
    "-a",
    "--sandbox",
    "-s",
    "--cd",
    "-C",
    "--add-dir",
    "--",
}


_MAX_BRICK_WINDOW_SEC = 6 * 3600


def _account_is_held(letter: str) -> bool:
    try:
        held = json.loads(paths.ACCOUNT_STATE.read_text()).get("held")
        return isinstance(held, dict) and letter in held
    except Exception:
        return False


def _account_is_bricked(letter: str) -> bool:
    if _account_is_held(letter):
        return True
    try:
        data = json.loads(paths.ACCOUNT_STATE.read_text())
        entry = data.get("accounts", {}).get(letter)
        if not isinstance(entry, dict):
            return False
        now = int(time.time())
        reset_ts = entry.get("reset_ts")
        if isinstance(reset_ts, (int, float)):
            return now < reset_ts
        bricked_at = entry.get("bricked_at")
        return isinstance(bricked_at, (int, float)) and now - bricked_at < _MAX_BRICK_WINDOW_SEC
    except Exception:
        return False


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _usage_pause_pct() -> float:
    env = os.environ.get("CLAUDE_ORCH_USAGE_PAUSE_PCT")
    if env is not None:
        try:
            return float(env)
        except (ValueError, TypeError):
            return 88.0
    cfg = config.usage_pause_pct()
    return cfg if cfg is not None else 88.0


def _usage_fresh_ttl() -> float:
    try:
        return float(os.environ.get("CLAUDE_ORCH_USAGE_FRESH_TTL_SEC", "600"))
    except (ValueError, TypeError):
        return 600.0


def _to_epoch(v):
    if _is_num(v):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return None
    return None


def _read_usage(letter: str):
    try:
        data = json.loads(paths.account_usage_path(letter).read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _usage_is_fresh(rec, now: float) -> bool:
    if not isinstance(rec, dict):
        return False
    ts = rec.get("ts")
    return _is_num(ts) and (now - ts) < _usage_fresh_ttl()


def _window_near(rec: dict, now: float, pct_key: str, reset_key: str,
                 pause: float, fresh: bool) -> bool:
    pct = rec.get(pct_key)
    if not (_is_num(pct) and pct >= pause):
        return False
    if fresh:
        return True
    r = _to_epoch(rec.get(reset_key))
    return r is not None and now < r


def _near_limit(letter: str, now: float) -> bool:
    rec = _read_usage(letter)
    if not isinstance(rec, dict):
        return False
    pause = _usage_pause_pct()
    fresh = _usage_is_fresh(rec, now)
    return (_window_near(rec, now, "five_hour_pct", "five_hour_resets_at", pause, fresh)
            or _window_near(rec, now, "seven_day_pct", "seven_day_resets_at", pause, fresh))


def _near_limit_5h(letter: str, now: float) -> bool:
    rec = _read_usage(letter)
    if not isinstance(rec, dict):
        return False
    return _window_near(rec, now, "five_hour_pct", "five_hour_resets_at",
                        _usage_pause_pct(), _usage_is_fresh(rec, now))


FABLE_FAMILY = "fable"
FABLE_WINDOW_SEC = 7 * 86400
FABLE_FALLBACK = "@opus"
FABLE_FALLBACK_EFFORT = "xhigh"


def _names_family(name, family: str) -> bool:
    return isinstance(name, str) and family in re.split(r"[^a-z0-9]+", name.lower())


def _model_spent_at(letter: str, family: str):
    try:
        data = json.loads(paths.ACCOUNT_STATE.read_text())
        entry = data.get("model_spent", {}).get(letter)
    except Exception:
        return None
    if not isinstance(entry, dict):
        return None
    stamps = [float(v) for k, v in entry.items()
              if _names_family(k, family) and _is_num(v)]
    return max(stamps) if stamps else None


def _last_seven_day_reset(rec, now: float) -> float | None:
    r7 = _to_epoch(rec.get("seven_day_resets_at")) if isinstance(rec, dict) else None
    if r7 is None or r7 <= now:
        return r7
    last = r7 - FABLE_WINDOW_SEC
    return last if last <= now else None


def _fable_spent(letter: str, now: float) -> bool:
    rec = _read_usage(letter)
    rows = rec.get("model_scoped") if isinstance(rec, dict) else None
    row = next((r for r in rows if isinstance(r, dict) and _names_family(r.get("name"), FABLE_FAMILY)),
               None) if isinstance(rows, list) else None
    at = rec.get("model_scoped_at") if isinstance(rec, dict) else None
    fresh = _is_num(at) and (now - at) < _usage_fresh_ttl()
    if row is not None and _window_near(row, now, "pct", "resets_at", _usage_pause_pct(), fresh):
        return True
    marked = _model_spent_at(letter, FABLE_FAMILY)
    if marked is None or (_is_num(at) and at > marked) or now - marked >= FABLE_WINDOW_SEC:
        return False
    reset = _to_epoch(row.get("resets_at")) if row is not None else None
    if reset is not None and reset > marked:
        return now < reset
    last_reset = _last_seven_day_reset(rec, now)
    return last_reset is None or last_reset <= marked


def _fable_left(now: float, force: bool = False) -> bool:
    return any(not _account_is_bricked(n) and not _fable_spent(n, now)
               and (force or not _near_limit(n, now))
               for n in config.account_names())


def _fable_as_opus(value: str) -> str:
    resolved = models.resolve_model(value)
    match = models.CONCRETE_MODEL.match(resolved.strip().lower()) if isinstance(resolved, str) else None
    if match is None or match.group(1) != FABLE_FAMILY:
        return value
    return FABLE_FALLBACK + (match.group(2) or "")


def _launches_fable(extra_args) -> bool:
    args = list(extra_args or [])
    return models.map_option_args(args, models.MODEL_OPTIONS, _fable_as_opus) != args


def _opus_effort(effort: str) -> str:
    floor = models.EFFORTS.index(FABLE_FALLBACK_EFFORT)
    if effort in models.EFFORTS and models.EFFORTS.index(effort) > floor:
        return effort
    return FABLE_FALLBACK_EFFORT


def _fable_to_opus_args(extra_args) -> list:
    args = models.map_option_args(list(extra_args or []), models.MODEL_OPTIONS, _fable_as_opus)
    return models.map_option_args(args, models.EFFORT_OPTIONS, _opus_effort)


def _base_weights() -> "tuple[int, ...]":
    names = config.account_names()
    try:
        return tuple(
            max(1, int(os.environ.get(
                f"CLAUDE_ORCH_ACCOUNT_WEIGHT_{n.upper()}",
                str(config.account_weight(n)))))
            for n in names
        )
    except (ValueError, TypeError):
        return tuple(max(1, config.account_weight(n)) for n in names)


def _usable_5h_pct(rec, now: float):
    if not isinstance(rec, dict):
        return None
    pct = rec.get("five_hour_pct")
    if not _is_num(pct):
        return None
    if _usage_is_fresh(rec, now):
        return float(pct)
    r = _to_epoch(rec.get("five_hour_resets_at"))
    if r is None:
        return None
    return float(pct) if now < r else 0.0


def _counter_weights(now: float) -> "tuple[int, ...]":
    names = config.account_names()
    base = _base_weights()
    pcts = [_usable_5h_pct(_read_usage(n), now) for n in names]
    if all(p is not None for p in pcts):
        heads = [max(0.0, 100.0 - p) for p in pcts]
        weights = tuple(round(b * h * h) for b, h in zip(base, heads))
        if sum(weights) > 0:
            return weights
    return base


def _pick_by_counter(names: "list[str]", weights: "list[int]", counter: int) -> str:
    total = sum(weights)
    if len(names) == 2:
        w0 = weights[0]
        return names[0] if (counter * w0) % total < w0 else names[1]
    current = [0] * len(names)
    pick = 0
    for _ in range((counter % total) + 1):
        current = [c + w for c, w in zip(current, weights)]
        pick = max(range(len(names)), key=lambda i: (current[i], -i))
        current[pick] -= total
    return names[pick]


def _pick_account(force: bool = False, fable: bool = False) -> str | None:
    names = list(config.account_names())
    try:
        anchor = paths.ACCOUNT_ACTIVE.read_text().rstrip("\n")
    except Exception:
        return None
    if anchor not in names:
        return None

    now = time.time()

    def blocked(letter: str) -> bool:
        return _account_is_bricked(letter) or (fable and _fable_spent(letter, now))

    unblocked = [n for n in names if not blocked(n)]
    candidates = unblocked if force else [n for n in unblocked if not _near_limit(n, now)]
    candidates = candidates or unblocked
    if not candidates:
        return None

    weights = list(_counter_weights(now))
    if len(weights) != len(names):
        weights = [1] * len(names)
    picked = [weights[names.index(n)] for n in candidates]
    if sum(picked) <= 0:
        base = _base_weights()
        picked = [base[names.index(n)] for n in candidates]

    try:
        counter_path = paths.SPAWN_COUNTER
        counter_path.parent.mkdir(parents=True, exist_ok=True)
        with open(counter_path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.seek(0)
            raw = fh.read()
            try:
                parsed = json.loads(raw)
                counter = int(parsed.get("counter", 0)) if isinstance(parsed, dict) else 0
            except Exception:
                counter = 0
            selected = _pick_by_counter(candidates, picked, counter)
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps({"counter": counter + 1}))
    except Exception:
        selected = candidates[0]
    return selected


def _active_account() -> str | None:
    try:
        letter = paths.ACCOUNT_ACTIVE.read_text().rstrip("\n")
    except Exception:
        return None
    return letter if letter in config.account_names() else None


def _account_used_pct(letter: str):
    rec = _read_usage(letter)
    if not isinstance(rec, dict):
        return None
    nums = [float(rec[k]) for k in ("five_hour_pct", "seven_day_pct") if _is_num(rec.get(k))]
    return max(nums) if nums else None


def _tripping_reset(letter: str, now: float):
    rec = _read_usage(letter)
    if not isinstance(rec, dict):
        return None
    pause = _usage_pause_pct()
    for pct_key, reset_key in (
        ("five_hour_pct", "five_hour_resets_at"),
    ):
        if _is_num(rec.get(pct_key)) and rec[pct_key] >= pause:
            r = _to_epoch(rec.get(reset_key))
            if r is not None:
                return r
    return None


class UsageCapPaused(RuntimeError):
    pass


def usage_spawn_gate(force: bool = False) -> dict:
    from . import usage_cap
    capped = usage_cap.refusal()
    if capped is not None:
        return capped
    if _active_account() is None:
        return {"status": "ok"}
    if force:
        return {"status": "ok", "forced": True}
    now = time.time()
    names = list(config.account_names())
    non_bricked = [n for n in names if not _account_is_bricked(n)]
    if not non_bricked:
        default = config.default_account()
        if _account_is_held(default):
            return {"status": "held", "account": default,
                    "reason": f"account {default} is on hold and no pool account is free",
                    "hint": f"dockwright account-hold --release {default}"}
        return {"status": "ok"}
    near = [n for n in non_bricked if _near_limit_5h(n, now)]
    if len(near) < len(non_bricked):
        return {"status": "ok"}
    resets = [r for r in (_tripping_reset(n, now) for n in near) if r is not None]
    earliest = min(resets) if resets else None
    payload = {
        "status": "paused",
        "reason": (f"every selectable account is at >= {int(_usage_pause_pct())}% "
                   f"of its 5h limit"),
        "hint": ("pass force=true to spawn_worker to bypass; "
                 "the pause lifts when a 5h window resets"),
    }
    for n in names:
        payload[f"{n}_pct"] = _account_used_pct(n)
    payload["earliest_reset_ts"] = earliest
    payload["retry_after_s"] = (max(0.0, earliest - now) if earliest is not None else None)
    return payload


_FARM_NEVER_SYMLINK = frozenset({
    ".credentials.json",
    ".claude.json",
    ".DS_Store",
    ".git",
    "cache", "sessions", "shell-snapshots", "session-env", "paste-cache",
    "file-history", "history.jsonl", "ide", "debug", "backups", "telemetry",
    "mcp-needs-auth-cache.json", "policy-limits.json", "remote-settings.json",
    "stats-cache.json",
    ".last-cleanup", ".last-update-result.json",
})


def _farm_never_symlink(name: str) -> bool:
    if name in _FARM_NEVER_SYMLINK:
        return True
    if name.startswith(".claude-"):
        return True
    if name.startswith("settings.json.bak"):
        return True
    if any(s in name.lower() for s in ("cred", "token", "oauth", "secret")):
        return True
    return False


_warned_drift: set[tuple[str, str]] = set()


def _ensure_symlink(link: Path, target: Path) -> None:
    try:
        if link.is_symlink():
            try:
                current = os.readlink(link)
            except OSError:
                current = None
            if current == str(target):
                return
            link.unlink()
        elif link.exists():
            drift_key = (str(link), str(target))
            if drift_key not in _warned_drift:
                _warned_drift.add(drift_key)
                logger.warning(
                    "farm config-dir drift: %s is a real path, not a symlink to %s; "
                    "left intact (manual migration needed to share it)", link, target,
                )
            return
        link.symlink_to(target)
    except OSError:
        pass


def _claude_json_healthy(path: Path) -> bool:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    servers = data.get("mcpServers") or {}
    return "dockwright" in servers or "claude-orchestrator" in servers


def _read_host_claude_json(_attempts: int = 2):
    for i in range(_attempts):
        try:
            return json.loads(paths.HOST_CLAUDE_JSON.read_text())
        except ValueError:
            if i + 1 < _attempts:
                time.sleep(0.05)
            continue
        except OSError:
            return None
    return None


def _atomic_write_json(target: Path, data: dict) -> None:
    tmp = target.parent / f"{target.name}.tmp.{os.getpid()}"
    try:
        tmp.write_text(json.dumps(data))
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _refresh_farm_mcp_servers(target: Path) -> None:
    host = _read_host_claude_json()
    if not isinstance(host, dict):
        return
    host_servers = host.get("mcpServers")
    if not isinstance(host_servers, dict):
        return
    try:
        with open(target, "rb") as fh:
            snapshot = os.fstat(fh.fileno())
            raw = fh.read()
    except OSError:
        return
    try:
        farm = json.loads(raw)
    except ValueError:
        return
    if not isinstance(farm, dict):
        return
    farm_servers = farm.get("mcpServers")
    if not isinstance(farm_servers, dict):
        return
    merged = dict(farm_servers)
    merged.update(host_servers)
    if ("claude-orchestrator" in merged
            and "claude-orchestrator" not in host_servers
            and "dockwright" in merged):
        del merged["claude-orchestrator"]
    if merged == farm_servers:
        return
    farm["mcpServers"] = merged
    payload = json.dumps(farm)
    tmp = target.parent / f"{target.name}.tmp.{os.getpid()}"
    try:
        tmp.write_text(payload)
        cur = os.stat(target)
        if ((cur.st_ino, cur.st_mtime_ns, cur.st_size)
                != (snapshot.st_ino, snapshot.st_mtime_ns, snapshot.st_size)):
            tmp.unlink()
            return
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _ensure_account_claude_json(farm: Path) -> None:
    target = farm / ".claude.json"
    if target.is_symlink():
        try:
            target.unlink()
        except OSError:
            return
    if _claude_json_healthy(target):
        _refresh_farm_mcp_servers(target)
        return
    data = _read_host_claude_json()
    if not isinstance(data, dict):
        return
    data.pop("oauthAccount", None)
    _atomic_write_json(target, data)


def ensure_account_config_dir(letter: str) -> Path:
    farm = paths.account_config_dir(letter)
    canonical = paths.CONFIG_HOME
    farm_r = farm.resolve()
    canonical_r = canonical.resolve()
    if (farm_r == canonical_r
            or canonical_r.is_relative_to(farm_r)
            or farm_r.is_relative_to(canonical_r)):
        raise OSError(
            f"account config dir {farm} aliases the canonical config home {canonical}")
    farm.mkdir(parents=True, exist_ok=True)
    try:
        entries = sorted(p.name for p in canonical.iterdir())
    except OSError:
        entries = []
    for name in entries:
        if _farm_never_symlink(name):
            continue
        _ensure_symlink(farm / name, canonical / name)
    _ensure_account_claude_json(farm)
    return farm


def write_registry_snapshot() -> None:
    try:
        pool = [{"name": a.name,
                 "config_dir": str(a.config_dir) if a.config_dir else None}
                for a in config.accounts()]
        payload = {"version": 1, "default": config.default_account(), "pool": pool}
        path = paths.ACCOUNT_REGISTRY
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)
    except Exception as e:
        print(f"spawner: registry snapshot write failed ({e})", file=sys.stderr)


def _farm_claude_json_status(target: Path) -> str:
    try:
        data = json.loads(target.read_text())
    except FileNotFoundError:
        return "missing"
    except (OSError, ValueError):
        return "unhealthy"
    if not isinstance(data, dict):
        return "unhealthy"
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return "unhealthy"
    if "dockwright" not in servers and "claude-orchestrator" not in servers:
        return "unhealthy"
    host = _read_host_claude_json()
    host_servers = host.get("mcpServers") if isinstance(host, dict) else None
    if not isinstance(host_servers, dict):
        return "unverified"
    if "dockwright" in host_servers and "dockwright" not in servers:
        return "legacy-keyed"
    for key, value in host_servers.items():
        if servers.get(key) != value:
            return "stale"
    if "claude-orchestrator" in servers and "claude-orchestrator" not in host_servers:
        return "stale"
    return "in-sync"


def farm_parity_report(letter: str) -> dict:
    farm = paths.account_config_dir(letter)
    canonical = paths.CONFIG_HOME
    report: dict = {"config_dir": str(farm), "exists": farm.is_dir(),
                    "shared": 0, "drift": [], "missing": [],
                    "claude_json": "missing"}
    if not report["exists"]:
        return report
    try:
        entries = sorted(p.name for p in canonical.iterdir())
    except OSError:
        entries = []
    for name in entries:
        if _farm_never_symlink(name):
            continue
        link = farm / name
        if link.is_symlink():
            try:
                current = os.readlink(link)
            except OSError:
                current = None
            if current == str(canonical / name):
                report["shared"] += 1
            else:
                report["drift"].append(name)
        elif link.exists():
            report["drift"].append(name)
        else:
            report["missing"].append(name)
    report["claude_json"] = _farm_claude_json_status(farm / ".claude.json")
    return report


def _resolve_account(letter: "str | None") -> "tuple[str | None, Path | None]":
    if letter is None:
        return None, None
    default = config.default_account()
    if letter == default:
        return letter, None
    try:
        farm = ensure_account_config_dir(letter)
        if _claude_json_healthy(farm / ".claude.json"):
            return letter, farm
    except OSError:
        pass
    return default, None


def _format_account_prefix(effective: "str | None", config_dir: "Path | None") -> str:
    if effective is None:
        return ""
    parts = []
    if config_dir is not None:
        parts.append(f"CLAUDE_CONFIG_DIR={shlex.quote(str(config_dir))}")
    parts.append(f"CLAUDE_ORCH_ACCOUNT={shlex.quote(effective)}")
    return " ".join(parts) + " "


def _build_account_prefix(letter: "str | None") -> str:
    return _format_account_prefix(*_resolve_account(letter))


def normalize_runtime(runtime: str | None) -> str:
    selected = runtime or DEFAULT_RUNTIME
    if selected not in SUPPORTED_RUNTIMES:
        allowed = ", ".join(sorted(SUPPORTED_RUNTIMES))
        raise ValueError(f"unsupported runtime {selected!r}; expected one of: {allowed}")
    return selected


def _matches_option(arg: str, options: set[str]) -> bool:
    for option in options:
        if arg == option or arg.startswith(f"{option}="):
            return True
        if (
            option.startswith("-")
            and not option.startswith("--")
            and arg.startswith(option)
            and len(arg) > len(option)
        ):
            return True
    return False


def _validate_codex_extra_args(extra_args: list[str]) -> None:
    disallowed = [
        arg
        for arg in extra_args
        if _matches_option(arg, CODEX_DISALLOWED_EXTRA_ARGS)
    ]
    if disallowed:
        raise ValueError(
            "extra_args for runtime='codex' include Claude-only or unsupported "
            f"flag(s): {disallowed}"
        )
    protected = [
        arg
        for arg in extra_args
        if _matches_option(arg, CODEX_PROTECTED_DEFAULT_ARGS)
    ]
    if protected:
        raise ValueError(
            "extra_args for runtime='codex' cannot override orchestrator Codex "
            "defaults (--ask-for-approval never, --sandbox danger-full-access) or "
            "the working root the [codex] allowed_roots gate checked (-C/--cd, "
            f"--add-dir) or end the options with '--': {protected}"
        )


def _shell_join(args: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in args)


def _codex_worker_prompt(initial_prompt: str, role_text: str = "") -> str:
    head = (f"{CODEX_WORKER_BOOTSTRAP_PROMPT}\n\n{role_text.strip()}" if role_text.strip()
            else CODEX_WORKER_BOOTSTRAP_PROMPT)
    if not initial_prompt:
        return head
    return f"{head}\n\nTask:\n{initial_prompt}"


LINUX_ARG_STRLEN_MAX = 131072


def _write_prompt_file(prompt: str) -> Path:
    target = paths.PROMPT_FILES / f"{hashlib.sha256(prompt.encode()).hexdigest()[:16]}.txt"
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        tmp.write_text(prompt, encoding="utf-8")
        tmp.replace(target)
    return target


def _interactive_shell() -> str:
    sh = os.environ.get("SHELL", "")
    if os.path.basename(sh) in ("zsh", "bash") and shutil.which(sh):
        return sh
    for cand in ("zsh", "bash"):
        found = shutil.which(cand)
        if found:
            return found
    return "sh"


WORKER_AGENT_FILE = "worker.md"
SYSTEM_PROMPT_OPTIONS = {"--system-prompt", "--system-prompt-file",
                         "--append-system-prompt", "--append-system-prompt-file"}


def worker_agent_file() -> Path | None:
    path = paths.AGENTS_DIR / WORKER_AGENT_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    _meta, body = parse_frontmatter(text)
    return path if body.strip() else None


def _runtime_command(
    runtime: str,
    initial_prompt: str,
    extra_args: list[str] | None = None,
    resume_sid: str | None = None,
    agent: str = "worker",
    role_text: str = "",
) -> str:
    runtime = normalize_runtime(runtime)
    selected_extra_args = models.resolve_model_args(extra_args or [])
    if runtime == "claude":
        args = [str(paths.dockwright_bin()), "launch", *selected_extra_args]
        if agent != "manager":
            args.append(disallowed_tools_arg())
        if agent == "worker" and not (
            any(_matches_option(a, set(models.MODEL_OPTIONS)) for a in selected_extra_args)
            and any(_matches_option(a, {"--effort"}) for a in selected_extra_args)
        ):
            raise ValueError("claude worker launch without --model / --effort: pass both in extra_args")
        if agent == "worker" and not any(
            _matches_option(a, SYSTEM_PROMPT_OPTIONS) for a in selected_extra_args
        ):
            agent_file = worker_agent_file()
            if agent_file is not None:
                args.extend(["--append-system-prompt-file", str(agent_file)])
        if resume_sid:
            args.extend(["--resume", resume_sid])
        elif initial_prompt:
            return _shell_join(args) + f' "$(cat {shlex.quote(str(_write_prompt_file(initial_prompt)))})"'
        return _shell_join(args)

    _validate_codex_extra_args(selected_extra_args)
    args = ["codex", *CODEX_DEFAULT_ARGS, *selected_extra_args]
    if resume_sid:
        args.extend(["resume", resume_sid])
    if agent == "worker" and (initial_prompt or not resume_sid):
        prompt = _codex_worker_prompt(initial_prompt, role_text)
        if role_text.strip():
            size = len(prompt.encode())
            if sys.platform.startswith("linux") and size >= LINUX_ARG_STRLEN_MAX:
                raise ValueError(f"the Codex worker prompt is {size} B; Linux passes at most "
                                 f"{LINUX_ARG_STRLEN_MAX - 1} B in one argument, so codex would not start. "
                                 "Shorten the role's Codex render or the task")
            return _shell_join(args) + f' "$(cat {shlex.quote(str(_write_prompt_file(prompt)))})"'
        args.append(prompt)
    elif initial_prompt:
        args.append(initial_prompt)
    return _shell_join(args)


async def _find_workers_os_window_match() -> str | None:
    return await get_driver().find_group_pane()


async def window_id_exists(window_id: str) -> bool:
    return await get_driver().pane_exists(window_id)


def codex_gate(cwd: str) -> None:
    target = Path(cwd).expanduser().resolve()
    roots = config.codex_allowed_roots()
    if not roots:
        raise ValueError(
            "codex is disabled: [codex] allowed_roots is empty in dockwright.toml — list the "
            'directories Codex may run under, e.g. allowed_roots = ["~/projects/work", "~/worktrees"]'
        )
    for root in roots:
        if target == root or root in target.parents:
            return
    raise ValueError(
        f"codex is not allowed under {cwd}: [codex] allowed_roots = {[str(r) for r in roots]}"
    )


def _needs_codex_gate(runtime: str, provider) -> bool:
    return runtime == "codex" or (provider is not None and provider.family == "codex")


async def spawn_worker_tab(
    cwd: str,
    initial_prompt: str,
    name: str,
    agent: str = "worker",
    tab_title: str | None = None,
    resume_sid: str | None = None,
    route_to_workers_window: bool = False,
    target_window_match: str | None = None,
    extra_args: list[str] | None = None,
    env: dict[str, str] | None = None,
    runtime: str = DEFAULT_RUNTIME,
    force: bool = False,
    provider=None,
    child_round=None,
    role_text: str = "",
) -> Tuple[str, str]:
    runtime = provider.runtime if provider is not None else normalize_runtime(runtime)
    if _needs_codex_gate(runtime, provider):
        codex_gate(cwd)
        if child_round is not None and child_round.cwd != cwd:
            codex_gate(child_round.cwd)
    headless = provider is not None and provider.headless
    codex_family = provider is not None and provider.family == "codex"
    caller_owns_auth = "CLAUDE_CODE_OAUTH_TOKEN" in (env or {})
    if agent != "manager":
        from . import usage_cap
        capped = usage_cap.refusal()
        if capped is not None:
            raise UsageCapPaused(f"{capped['reason']}; {capped['hint']}")
    manager_letter = None
    manager_account = None
    if agent == "manager" and not (caller_owns_auth or codex_family):
        manager_letter = _active_account()
        manager_account = _resolve_account(manager_letter)
    if agent == "manager" and runtime == "claude" and not headless:
        from .manager_launch import manager_claude_args
        effective = manager_account[0] if manager_account is not None else None
        extra_args = [*manager_claude_args(effective), *(extra_args or [])]
    if agent == "worker" and runtime == "claude" and not headless and not any(
            _matches_option(a, SYSTEM_PROMPT_OPTIONS) for a in (extra_args or [])):
        from . import roles
        subworker = (env or {}).get("CLAUDE_WORKER_KIND") == "subworker"
        extra_args = roles.default_role_args(extra_args or [], "child" if subworker else "worker", cwd=cwd)
    wants_fable = (runtime == "claude" and not headless and agent != "manager" and not caller_owns_auth
                   and _launches_fable(extra_args))
    fable = wants_fable and _fable_left(time.time(), force)
    if caller_owns_auth or codex_family:
        letter = None
    elif agent == "manager":
        letter = manager_letter
    else:
        letter = _pick_account(force, fable=fable)
        if letter is None and runtime == "claude" and _account_is_held(config.default_account()):
            raise ValueError(f"account {config.default_account()} is on hold; no pool account is free")
    if wants_fable and (not fable or _fable_spent(_resolve_account(letter)[0] or config.default_account(),
                                                  time.time())):
        logger.warning("no Fable left on the launch account: %s launches on Opus", name)
        extra_args = _fable_to_opus_args(extra_args)
    if headless:
        if child_round is None:
            raise ValueError(f"provider {provider.name!r} needs a child_round")
        runtime_cmd = (provider.resume_command(child_round) if child_round.resume_sid
                       else provider.spawn_command(child_round))
    else:
        runtime_cmd = _runtime_command(runtime, initial_prompt, extra_args, resume_sid, agent=agent,
                                       role_text=role_text)
    caller_env_parts: list[str] = []
    for k, v in (env or {}).items():
        if k in (
            "CLAUDE_AGENT",
            "CLAUDE_WORKER_NAME",
            "CLAUDE_WORKER_RUNTIME",
            "CLAUDE_WORKER_PROVIDER",
            "CLAUDE_ORCH_ACCOUNT",
            "CLAUDE_CONFIG_DIR",
        ):
            continue
        caller_env_parts.append(f"{k}={shlex.quote(v)}")
    caller_env_prefix = (" ".join(caller_env_parts) + " ") if caller_env_parts else ""
    if agent == "worker":
        runtime_env = f"CLAUDE_WORKER_RUNTIME={shlex.quote(runtime)} "
    else:
        runtime_env = ""
    if headless:
        runtime_env += f"CLAUDE_WORKER_PROVIDER={shlex.quote(provider.name)} "
        for k, v in provider.env(child_round.mode).items():
            runtime_env += f"{k}={shlex.quote(v)} "
    if runtime == "claude":
        trust.pretrust_dir(cwd)
    if codex_family:
        account_prefix = "CLAUDE_ORCH_ACCOUNT='' "
    elif manager_account is not None:
        account_prefix = _format_account_prefix(*manager_account)
    else:
        account_prefix = _build_account_prefix(letter)
    if runtime == "claude" and letter is not None and letter != config.default_account():
        farm_json = paths.account_config_dir(letter) / ".claude.json"
        if _claude_json_healthy(farm_json):
            trust.pretrust_dir(cwd, config_json=farm_json)
    inner_cmd = (
        f"cd {shlex.quote(cwd)} && "
        f"{account_prefix}"
        f"{caller_env_prefix}"
        f"CLAUDE_AGENT={shlex.quote(agent)} "
        f"CLAUDE_WORKER_NAME={shlex.quote(name)} "
        f"{runtime_env}"
        f"{runtime_cmd}"
    )
    title = tab_title if tab_title is not None else name
    window_id = await get_driver().spawn(
        cwd=cwd, title=title, argv=[_interactive_shell(), "-ic", inner_cmd],
        route_to_workers_window=route_to_workers_window,
        route_to_manager_session=(agent == "manager"),
        target_window_match=target_window_match,
    )
    if headless and window_id:
        get_driver().set_window_option(window_id, "remain-on-exit", "on")
    return (window_id or name), name
