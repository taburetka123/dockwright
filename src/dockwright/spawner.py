import asyncio
import fcntl
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Tuple

from . import config, paths, trust
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
}


_MAX_BRICK_WINDOW_SEC = 6 * 3600


def _account_is_bricked(letter: str) -> bool:
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


def _pick_account(force: bool = False) -> str | None:
    names = list(config.account_names())
    try:
        anchor = paths.ACCOUNT_ACTIVE.read_text().rstrip("\n")
    except Exception:
        return None
    if anchor not in names:
        return None

    now = time.time()
    weights = list(_counter_weights(now))
    if len(weights) != len(names):
        weights = [1] * len(names)

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
            selected_by_counter = _pick_by_counter(names, weights, counter)
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps({"counter": counter + 1}))
    except Exception:
        selected_by_counter = names[0]

    order = (selected_by_counter,
             *[n for n in names if n != selected_by_counter])

    for letter in order:
        if _account_is_bricked(letter):
            continue
        if not force and _near_limit(letter, now):
            continue
        return letter
    for letter in order:
        if not _account_is_bricked(letter):
            return letter
    return None


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


def usage_spawn_gate(force: bool = False) -> dict:
    if _active_account() is None:
        return {"status": "ok"}
    if force:
        return {"status": "ok", "forced": True}
    now = time.time()
    names = list(config.account_names())
    non_bricked = [n for n in names if not _account_is_bricked(n)]
    if not non_bricked:
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


def _build_account_prefix(letter: "str | None") -> str:
    if letter is None:
        return ""
    default = config.default_account()
    config_dir = None
    effective = letter
    if letter != default:
        try:
            farm = ensure_account_config_dir(letter)
            if _claude_json_healthy(farm / ".claude.json"):
                config_dir = farm
            else:
                effective = default
        except OSError:
            effective = default
    parts = []
    if config_dir is not None:
        parts.append(f"CLAUDE_CONFIG_DIR={shlex.quote(str(config_dir))}")
    parts.append(f"CLAUDE_ORCH_ACCOUNT={shlex.quote(effective)}")
    return " ".join(parts) + " "


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
            "defaults (--ask-for-approval never, --sandbox danger-full-access): "
            f"{protected}"
        )


def _shell_join(args: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in args)


def _codex_worker_prompt(initial_prompt: str) -> str:
    if not initial_prompt:
        return CODEX_WORKER_BOOTSTRAP_PROMPT
    return f"{CODEX_WORKER_BOOTSTRAP_PROMPT}\n\nTask:\n{initial_prompt}"


def _interactive_shell() -> str:
    sh = os.environ.get("SHELL", "")
    if os.path.basename(sh) in ("zsh", "bash") and shutil.which(sh):
        return sh
    for cand in ("zsh", "bash"):
        found = shutil.which(cand)
        if found:
            return found
    return "sh"


def _runtime_command(
    runtime: str,
    initial_prompt: str,
    extra_args: list[str] | None = None,
    resume_sid: str | None = None,
    agent: str = "worker",
) -> str:
    runtime = normalize_runtime(runtime)
    selected_extra_args = list(extra_args or [])
    if runtime == "claude":
        args = ["claude", *selected_extra_args]
        if not any(_matches_option(a, {"--model"}) for a in selected_extra_args):
            args.extend(["--model", config.worker_model()])
        if resume_sid:
            args.extend(["--resume", resume_sid])
        elif initial_prompt:
            args.append(initial_prompt)
        return _shell_join(args)

    _validate_codex_extra_args(selected_extra_args)
    args = ["codex", *CODEX_DEFAULT_ARGS, *selected_extra_args]
    if resume_sid:
        args.extend(["resume", resume_sid])
    if initial_prompt:
        args.append(_codex_worker_prompt(initial_prompt) if agent == "worker" else initial_prompt)
    elif agent == "worker" and not resume_sid:
        args.append(_codex_worker_prompt(initial_prompt))
    return _shell_join(args)


async def _find_workers_os_window_match() -> str | None:
    return await get_driver().find_group_pane()


async def window_id_exists(window_id: str) -> bool:
    return await get_driver().pane_exists(window_id)


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
) -> Tuple[str, str]:
    runtime = normalize_runtime(runtime)
    runtime_cmd = _runtime_command(runtime, initial_prompt, extra_args, resume_sid, agent=agent)
    caller_env_parts: list[str] = []
    for k, v in (env or {}).items():
        if k in (
            "CLAUDE_AGENT",
            "CLAUDE_WORKER_NAME",
            "CLAUDE_WORKER_RUNTIME",
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
    caller_owns_auth = "CLAUDE_CODE_OAUTH_TOKEN" in (env or {})
    if caller_owns_auth:
        letter = None
    elif agent == "manager":
        letter = _active_account()
    else:
        letter = _pick_account(force)
    if runtime == "claude":
        trust.pretrust_dir(cwd)
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
    return (window_id or name), name
