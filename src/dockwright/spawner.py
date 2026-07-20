"""Spawn a new terminal window running an agent CLI as a worker.

Routes through the terminal driver (`terminal.get_driver`) — tmux. The new
window is created in the background without yanking focus from the manager
session.
"""
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


# Maximum brick window (seconds): matches stale_monitor.MAX_PLAUSIBLE_RESET_SEC.
# An account whose bricked_at is older than this is no longer considered bricked
# when no explicit reset_ts is stored.
_MAX_BRICK_WINDOW_SEC = 6 * 3600


def _account_is_bricked(letter: str) -> bool:
    """True if the account is currently within its brick window per account-state.json.

    Reads paths.ACCOUNT_STATE at call time so tests can monkeypatch the path.
    Crash-proof: any failure (missing file, corrupt JSON, unexpected shape) returns
    False — fail-open is correct: a falsely-non-bricked account gets a spawn attempt
    that may fail, which the reactive flip will recover from; a falsely-bricked healthy
    account wastes a slot, which is worse.
    """
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
    """resets_at -> epoch seconds (float) or None. Accepts epoch int/float, a
    numeric string, or ISO-8601 ('...Z' or '...+00:00'). None on anything else.
    The wire format of rate_limits.*.resets_at is not spike-confirmed, so accept
    both; an unparseable value simply disables carry-forward for that window."""
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
    """Parsed usage record dict for the account, or None on any failure (fail-open,
    like _account_is_bricked). Reads paths.account_usage_path at call time so tests
    can monkeypatch paths.ACCOUNT_USAGE."""
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
    """One window's near-limit test: at/over `pause` AND either fresh, or
    stale-but-known-hot with resets_at still in the future (carry-forward)."""
    pct = rec.get(pct_key)
    if not (_is_num(pct) and pct >= pause):
        return False
    if fresh:
        return True
    r = _to_epoch(rec.get(reset_key))
    return r is not None and now < r


def _near_limit(letter: str, now: float) -> bool:
    """True if the account is at/over the breaker threshold on EITHER window —
    fresh-and-hot, or stale-but-known-hot with the tripping window's resets_at
    still in the future (carry-forward). Missing record → False (never excludes an
    unknown account). Used by the PICKER's pass-1 skip (both windows)."""
    rec = _read_usage(letter)
    if not isinstance(rec, dict):
        return False
    pause = _usage_pause_pct()
    fresh = _usage_is_fresh(rec, now)
    return (_window_near(rec, now, "five_hour_pct", "five_hour_resets_at", pause, fresh)
            or _window_near(rec, now, "seven_day_pct", "seven_day_resets_at", pause, fresh))


def _near_limit_5h(letter: str, now: float) -> bool:
    """5h-window-only near-limit — PAUSE eligibility. The full-refusal pause
    must be bounded by the 5h reset horizon; the 7d window participates in the
    breaker's routing preference (_near_limit) but never in a total refusal —
    a weekly-budget hard stop was local policy, not a product invariant."""
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
    """The five_hour_pct usable for headroom weighting, or None when it carries no
    signal. Usable when the record is FRESH, STALE-but-pre-reset (carry forward —
    `now < five_hour_resets_at`), or STALE-post-reset, which reads as 0.0: a 5h
    window that already reset IS empty, and the idle account holding it must get
    full headroom rather than degrade the whole pool (the old None here reset the
    split to base exactly when the idle account had the most headroom). None when
    the pct is missing/non-numeric (UNKNOWN usage — a null 5h pct is the realistic
    statusline value when there is no 5h window) or resets_at is missing/unparseable
    (no basis to carry forward OR to declare the window reset). Mirrors _near_limit's
    per-window carry-forward, keyed on the 5h window because headroom weighting is
    5h-based."""
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
    """Integer counter weights: raw `round(base_i * headroom_i**2)` on the 5h
    window when EVERY pool account yields a usable 5h reading (fresh,
    stale-but-pre-reset carry-forward, or 0-used after its window reset);
    otherwise the raw base weights. An unknown 5h pct (missing/non-numeric) or
    an unparseable reset on ANY account still degrades the whole pick to base —
    an unknown-usage account is never given a headroom advantage.

    The square shifts picks toward the account with headroom much harder than
    the old linear budget-10 form (44%-vs-2% used: ~25:75 instead of 40:60),
    and a zero-headroom account legitimately weighs 0 — the configurable
    near-limit breaker (usage_pause_pct, default 88%) and pause gate own the
    "account is hot" semantics; no floor keeps it in rotation. Weights stay un-normalized (0..10000*base): _pick_by_counter
    distributes any integer vector exactly proportionally over its period.
    (N>2 pools replay `(counter % total) + 1` smooth-WRR steps, so a pick costs
    up to ~N*10000*base iterations — tens of ms at N=3; the default 2-account
    pool uses the O(1) formula.) The sum-guard is on the ROUNDED vector: both
    accounts >~99.3% used round to (0, 0) while the pre-round eff-total is
    still positive. Without the guard a forced spawn (the pause gate blocks
    only un-forced ones) would hit `% 0` in _pick_by_counter, which
    _pick_account's counter try/except swallows into a silent names[0]
    fallback with the counter never advancing — a stuck bias, not a crash."""
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
    """Deterministic smooth weighted round-robin selection from a single
    persisted counter.

    Two-account pools use the ORIGINAL error-diffusion formula VERBATIM —
    `(counter * w0) % total < w0` — so existing pick sequences are
    bit-identical (the pinned tests in test_spawner_account). Other pool
    sizes replay nginx-style smooth WRR `(counter % total) + 1` steps from
    zero state (stateless, same single counter; ties go to the earliest pool
    entry), which reduces to the same interleave ratio per period."""
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
    """Select a pool account for a WORKER spawn via the headroom-weighted
    counter round-robin.

    Pointer-validity is the feature GATE (None => pool off => caller falls back
    to the default login). Selection is the counter, NOT the pointer. Brick-skip
    still applies. NO keychain calls — each account authenticates via its own
    per-CLAUDE_CONFIG_DIR keychain login, so there is nothing to probe.

    The pool comes from the config registry (default: the single account 'a').
    The default account is the primary — it also runs the manager + interactive
    human sessions; an even worker split keeps its headroom for the human
    rather than spending it on workers. Weights come from config, overridable
    via CLAUDE_ORCH_ACCOUNT_WEIGHT_<NAME>. When EVERY pool account has a
    usable 5h reading (fresh, stale-but-pre-reset, or post-reset-as-0), the
    counter weights become squared-headroom-proportional (`_counter_weights`);
    otherwise they degrade to the raw base.

    Counter persistence: paths.SPAWN_COUNTER, fcntl-locked for concurrent
    spawns. Counter corruption/missing → reset to 0 → select the first pool
    account (tiebreak).

    Breaker (two-pass): pass 1 skips bricked AND (unless `force`) near-limit
    accounts over the (selected_by_counter, *rest in pool order) order; pass 2
    is a best-effort fallback that skips only bricked. The breaker NEVER makes
    the picker return None — the all-hot PAUSE is enforced upstream at
    usage_spawn_gate. `force=True` collapses pass 1 to brick-skip only.

    Coexistence with reactive flip: stale_monitor.py still owns account-active
    and flips it when workers brick. _pick_account() reads account-state.json
    directly for brick detection (ignores the pointer), so proactive selection
    and reactive recovery operate independently with no conflict.
    """
    names = list(config.account_names())
    # Feature gate: account-active must exist and hold a valid pool name.
    # rstrip("\n") only — NOT strip(): a whitespace-padded name (" a ") must
    # read as pool-off (the shell $(cat) would word-split it, lying about the
    # account).
    try:
        anchor = paths.ACCOUNT_ACTIVE.read_text().rstrip("\n")
    except Exception:
        return None
    if anchor not in names:
        return None

    # Read weights at call time so env var overrides (e.g. in tests) take
    # effect. Usage-aware when every account has a usable 5h reading;
    # degrades to base otherwise. Guard: a concurrent config edit between the
    # two reads could skew the vector — fall back to even weights on mismatch.
    now = time.time()
    weights = list(_counter_weights(now))
    if len(weights) != len(names):
        weights = [1] * len(names)

    # Atomic counter read-increment-write (fcntl-locked for concurrent spawns).
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
        # Counter unavailable — default to the first pool account (init/tiebreak)
        selected_by_counter = names[0]

    order = (selected_by_counter,
             *[n for n in names if n != selected_by_counter])

    # Pass 1: skip bricked AND (unless forced) near-limit accounts (the breaker).
    for letter in order:
        if _account_is_bricked(letter):
            continue
        if not force and _near_limit(letter, now):
            continue
        return letter
    # Pass 2: best-effort breaker fallback — skip only bricked. The pause is
    # enforced at usage_spawn_gate, NOT here; the picker never returns None
    # solely because of the breaker. None is reserved for pool-off (above) and
    # all-bricked (here).
    for letter in order:
        if not _account_is_bricked(letter):
            return letter
    return None  # every account bricked → caller falls back to the default login


def _active_account() -> str | None:
    """The account-active pointer name or None (pool off). Used for the
    MANAGER's account (rides the pointer; the default account -> default
    login, others -> their config-dir farm)."""
    try:
        letter = paths.ACCOUNT_ACTIVE.read_text().rstrip("\n")
    except Exception:
        return None
    return letter if letter in config.account_names() else None


def _account_used_pct(letter: str):
    """max(5h, 7d) last-known used-% from the record (fresh OR stale), or None if no
    record / no numeric pct. Stale values are still informative in a paused payload —
    on a carry-forward pause they are exactly WHY the account is paused, so the manager
    can surface the real number instead of a misleading null."""
    rec = _read_usage(letter)
    if not isinstance(rec, dict):
        return None
    nums = [float(rec[k]) for k in ("five_hour_pct", "seven_day_pct") if _is_num(rec.get(k))]
    return max(nums) if nums else None


def _tripping_reset(letter: str, now: float):
    """resets_at (epoch) of the 5h window that tripped the pause, or None."""
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
    """Pre-spawn pause gate for WORKER spawns (called by spawn_worker_impl
    before any side effects). Returns {"status":"ok"[, "forced":True]} or a
    {"status":"paused", …} payload. Pauses ONLY when every non-bricked pool
    account is near-limit ON ITS 5h WINDOW (the 7d window biases the picker's
    routing but never drives a total refusal). Threshold is [accounts]
    usage_pause_pct (default 88; env CLAUDE_ORCH_USAGE_PAUSE_PCT wins).
    Pool-off, all-bricked, and force all fall through to ok — the existing
    None→default-login→flip backstop owns those, and changing them would be
    'worse than today'."""
    if _active_account() is None:
        return {"status": "ok"}
    if force:
        return {"status": "ok", "forced": True}
    now = time.time()
    names = list(config.account_names())
    non_bricked = [n for n in names if not _account_is_bricked(n)]
    if not non_bricked:
        return {"status": "ok"}  # all bricked → existing condition, not a pause
    near = [n for n in non_bricked if _near_limit_5h(n, now)]
    if len(near) < len(non_bricked):
        return {"status": "ok"}  # at least one selectable, non-near-limit account
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


# Per-account config-dir farm: every top-level entry of canonical ~/.claude is
# symlinked into the per-account farm EXCEPT the names/patterns below. A denylist
# (not an allowlist) so a NEW canonical asset auto-includes on the next spawn with
# no code change. A whole-dir symlink (~/.claude-b -> ~/.claude) is NOT viable: it
# would expose .credentials.json + the shared .claude.json through the link,
# re-breaking per-account auth/identity — hence per-entry symlinking.
_FARM_NEVER_SYMLINK = frozenset({
    # Security / identity — the farm's reason to exist:
    ".credentials.json",   # must stay ABSENT so the farm never inherits the canonical dir's file-based credentials; ~/.claude-b authenticates via its own per-config-dir keychain login
    ".claude.json",        # per-account REAL file (built by _ensure_account_claude_json), not a link to host
    # Junk:
    ".DS_Store",
    ".git",                # config-repo git dir — a farm link would let git ops mutate the canonical repo
    # claude per-config-dir runtime/session state — claude recreates these itself
    # under whatever CLAUDE_CONFIG_DIR it runs in; sharing would cross-pollute
    # sessions / auth / rate-limits / telemetry between accounts:
    "cache", "sessions", "shell-snapshots", "session-env", "paste-cache",
    "file-history", "history.jsonl", "ide", "debug", "backups", "telemetry",
    "mcp-needs-auth-cache.json", "policy-limits.json", "remote-settings.json",
    "stats-cache.json",
    # claude per-config-dir self-update / cleanup markers (written per config dir):
    ".last-cleanup", ".last-update-result.json",
})


def _farm_never_symlink(name: str) -> bool:
    """True if a canonical top-level entry must NOT be symlinked into a farm."""
    if name in _FARM_NEVER_SYMLINK:
        return True
    # The per-account farm dirs themselves. They are SIBLINGS under ~ (see
    # paths.account_config_dir), so they are never children of ~/.claude and never
    # iterated — guarded anyway, belt-and-suspenders.
    if name.startswith(".claude-"):
        return True
    # ~100 timestamped settings backups (settings.json.bak.<ts> / .bak-<label>).
    if name.startswith("settings.json.bak"):
        return True
    # Fail-CLOSED at the credential boundary: any future claude-written file whose
    # name suggests a secret is denied rather than auto-shared until hand-added.
    if any(s in name.lower() for s in ("cred", "token", "oauth", "secret")):
        return True
    return False


# Drift warnings are deduped per (link, target) so a persistent real-dir drift
# logs once per process lifetime, not on every spawn (spawner is MCP-resident).
_warned_drift: set[tuple[str, str]] = set()


def _ensure_symlink(link: Path, target: Path) -> None:
    """Idempotently make `link` a symlink to `target`; self-heal a broken/wrong
    link; never destroy a real (non-symlink) path. Best-effort (swallow OSError)."""
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
            # External drift: a real file/dir where a symlink belongs. Never
            # destroy it (a live same-account worker may have written real data
            # here, e.g. a SessionEnd hook). Leave intact + warn ONCE per
            # (link, target) so a persistent drift doesn't spam a warning every
            # spawn; migrate manually when no same-account worker is live.
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
    # claude-orchestrator: one-release legacy MCP key recognition
    return "dockwright" in servers or "claude-orchestrator" in servers


def _read_host_claude_json(_attempts: int = 2):
    """Parse the host $HOME/.claude.json, retrying once on a partial concurrent
    write. None on persistent failure (best-effort)."""
    for i in range(_attempts):
        try:
            return json.loads(paths.HOST_CLAUDE_JSON.read_text())
        except ValueError:
            if i + 1 < _attempts:
                time.sleep(0.05)  # brief blocking retry — only on a corrupt/partial read of the host config, first build per account
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
    """Host-wins refresh of a healthy farm .claude.json's mcpServers: every host
    server key overwrites/creates its farm entry (farm-only keys are preserved),
    plus a targeted drop of the legacy 'claude-orchestrator' alias once the host
    has dropped it — guarded on the farm carrying the current 'dockwright' key,
    so a legacy-only farm is never stripped unhealthy. Only mcpServers is ever
    touched; writes only on change (atomic). Best-effort: unreadable host or a
    non-dict mcpServers shape on either side skips the refresh.

    Concurrency: a live same-account claude rewrites this file WHOLESALE via
    atomic replace (measured on-host: the inode flips on every write) and takes
    no lock this code could share — so no lock is taken here either (it would
    only serialize dockwright's own writers, and the guard below already
    handles those by abort-and-retry). Instead the write is identity-guarded:
    the tmp payload is written first, then the target's (inode, mtime_ns,
    size) is re-checked against the pre-read snapshot immediately before the
    rename, so ANY competing write in the read->write window aborts this
    refresh — the live session's own state (oauthAccount, projects) always
    wins, and the refresh retries on a later ensure. Residual exposure is the
    stat->rename syscall gap only."""
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
            tmp.unlink()  # a competing writer landed in our window — theirs wins
            return
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _ensure_account_claude_json(farm: Path) -> None:
    """Ensure farm/.claude.json is a real per-account file carrying the
    orchestrator MCP + project trust with oauthAccount stripped. Rebuilds from the
    host config only when missing/corrupt/MCP-less. A HEALTHY file is never
    rebuilt; it gets a host-wins refresh of its mcpServers key ONLY, on every
    ensure (see _refresh_farm_mcp_servers) so MCP registration tracks the host.
    That refresh is identity-guarded and aborts if a live same-account session
    writes the file concurrently — the session's own state (oauthAccount,
    projects, non-mcpServers keys) always wins and never propagates from the
    host after the first build. Best-effort: a host-read failure leaves the
    spawn to proceed degraded."""
    target = farm / ".claude.json"
    if target.is_symlink():
        try:
            target.unlink()  # must be a real file, never a link to the shared host config
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
    """Build/self-heal the per-account CLAUDE_CONFIG_DIR symlink-farm; return it.

    Idempotent — safe on every spawn. Raises OSError if the farm dir aliases the
    canonical config home (registry misconfig) or the farm root can't be created;
    symlink and .claude.json steps are best-effort.
    """
    farm = paths.account_config_dir(letter)
    canonical = paths.CONFIG_HOME
    farm_r = farm.resolve()
    canonical_r = canonical.resolve()
    if (farm_r == canonical_r
            or canonical_r.is_relative_to(farm_r)
            or farm_r.is_relative_to(canonical_r)):
        # A registry config_dir override aliasing the canonical config home
        # (~/.claude itself, $HOME above it, or a path inside it) must never be
        # farm-assembled: symlink healing on an aliased tree can replace an
        # operator's own symlinked entry with a self-loop. OSError is the
        # contract both callers already handle (spawn falls back to the
        # default login; accounts-sync skips the account with a note).
        raise OSError(
            f"account config dir {farm} aliases the canonical config home {canonical}")
    farm.mkdir(parents=True, exist_ok=True)
    try:
        entries = sorted(p.name for p in canonical.iterdir())
    except OSError:
        entries = []  # best-effort: unreadable canonical → no symlinks this spawn
    for name in entries:
        if _farm_never_symlink(name):
            continue
        _ensure_symlink(farm / name, canonical / name)
    _ensure_account_claude_json(farm)
    return farm


def write_registry_snapshot() -> None:
    """Mirror config.accounts() to paths.ACCOUNT_REGISTRY for consumers that
    cannot import the package (standalone stale_monitor, bootstrap-recreate.sh).
    Best-effort by contract: a snapshot failure must never block a spawn or
    MCP boot — those consumers fall back to the legacy a/b pair."""
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
    """Report-grade status of a farm .claude.json vs the host. Stricter than
    _claude_json_healthy on purpose: a list-shaped mcpServers spawns fine but
    IS a broken shape, so a report calls it unhealthy. 'legacy-keyed' only
    fires when the HOST has renamed to 'dockwright' and the farm hasn't caught
    up — a farm matching a still-legacy host is in-sync, not legacy-keyed."""
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
    """Read-only parity scan of an account's config-dir farm vs canonical.

    Stateless re-scan — deliberately independent of _warned_drift (that set
    dedups the resident spawner's LOG lines; a report must always name every
    current drift). Keys: config_dir, exists, shared (count of correct
    symlinks), drift (real paths / wrong-target symlinks where a canonical
    symlink belongs), missing (canonical entries with no farm entry),
    claude_json (see _farm_claude_json_status)."""
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
    """Shell env-assignment prefix. Empty when letter is None.

    letter == the registry default account -> the default ~/.claude: NO
    CLAUDE_CONFIG_DIR (no farm built), stamp the default name. The default
    login authenticates the session. Any other letter -> build/assemble its
    config-dir farm and pin CLAUDE_CONFIG_DIR iff its .claude.json is healthy
    (parses as a dict WITH the orchestrator MCP); an unhealthy/failed farm
    falls back to the default login with a TRUTHFUL effective stamp of the
    default name (a worker pinned to an MCP-less farm would lose
    ask_manager/worker_done). No token is ever injected — auth is the
    per-config-dir keychain login.
    """
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
    """Shell for the spawn `-ic` argv. The inner command uses POSIX `K=v cmd`
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
    """Return a pane id inside the workers OS window (used with `--match
    window_id:<id>`), or None if no such OS window currently exists.

    Opening a new tab places it inside the OS window that contains the window
    selected by `--match`. So we just need to hand back any window id from
    inside the target OS window.

    Callers MUST select it with `window_id:<id>`, NOT `id:<id>`. For `launch`,
    `--match=id:N` resolves a *tab* with id N first (only falling back to the
    window with id N if no tab matches). Tab-ids and window-ids are SEPARATE,
    overlapping id-spaces, so a window id N usually also exists as an unrelated
    tab N — in a *different* OS window — and `id:N` would land the new tab there.
    `window_id:N` unambiguously selects the tab that contains window N.
    """
    return await get_driver().find_group_pane()


async def window_id_exists(window_id: str) -> bool:
    """True if `window_id` is a live pane id (appears in tmux list-panes).

    Used to decide whether the old manager's OS-window is still around to host
    the recreated manager tab. Returns False on any error (terminal down, bad
    json, non-zero exit) so callers fall back to focus-follows behavior rather
    than hard-failing the recreate.
    """
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
    """Open a new tmux window in the background; return (tmux pane id / window id, tab_title).

    The returned id is the terminal driver's pane handle (a tmux pane id). It's
    used by other tools (kill_worker, etc.) only as a display handle.

    `runtime` selects the CLI: "claude" (default, backward compatible) or
    "codex". When `resume_sid` is set, the tab resumes through the selected
    runtime instead of starting a fresh session. `initial_prompt` is ignored
    by Claude resumes; Codex accepts it as an optional resume prompt.

    When `target_window_match` is set (and `route_to_workers_window` is False),
    the new tab opens in the OS-window containing the matched window (e.g.
    `"window_id:42"`) instead of the currently-focused OS-window. This is what
    keeps a recreated manager tab in the OLD manager's OS-window rather than
    wherever keyboard focus happens to be. Use a `window_id:` selector, not
    `id:` — `launch --match=id:N` resolves a tab id first and only falls back to
    a window id, and tab-ids and window-ids are separate overlapping id-spaces,
    so a window id N usually also exists as an unrelated tab N and `id:N` would
    land the tab in the wrong OS-window.
    `route_to_workers_window=True` takes priority and ignores `target_window_match`.

    `extra_args` are CLI flags spliced between the selected runtime and the
    prompt / resume arg (e.g. `["--model", "gpt-5.5"]`). Codex sessions always
    receive `--ask-for-approval never --sandbox danger-full-access
    --dangerously-bypass-hook-trust`; Codex workers also receive a
    worker-protocol bootstrap prompt. Caller extra_args cannot override those
    defaults or pass known Claude-only flags.
    `env` are extra env vars exported in the spawned shell before the runtime
    runs; they are applied BEFORE the orchestrator-controlled `CLAUDE_AGENT` /
    `CLAUDE_WORKER_NAME` / runtime marker env so those cannot be overridden by
    callers. Auth is the per-`CLAUDE_CONFIG_DIR` keychain login — no token is
    ever injected. A caller-passed `CLAUDE_CODE_OAUTH_TOKEN` DISABLES pool
    routing for that spawn — no `CLAUDE_CONFIG_DIR` farm and no
    `CLAUDE_ORCH_ACCOUNT` stamp (the caller owns auth; the record staying
    unstamped is truthful). Note the default `~/.claude` keychain login now
    OUTRANKS the token, so a caller token no longer reliably forces a token
    identity; it is kept as a defensive escape hatch. The caller's raw token
    value is visible in ps / the shell cmdline, their informed choice.
    `CLAUDE_ORCH_ACCOUNT` and `CLAUDE_CONFIG_DIR` are orchestrator-controlled and
    cannot be overridden by callers — `CLAUDE_CONFIG_DIR` is the sole billing
    lever, so a caller-passed value is dropped to keep the picker's account
    selection authoritative.
    `force` is worker-only: it bypasses the usage near-limit breaker in
    `_pick_account` for this spawn (still skips bricked accounts). The
    manager / caller-owns-auth branches ride the pointer and ignore it.
    """
    runtime = normalize_runtime(runtime)
    runtime_cmd = _runtime_command(runtime, initial_prompt, extra_args, resume_sid, agent=agent)
    # Build the shell command the spawned tab will run.
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
    # Managers are Claude-only and carry no runtime marker env; only workers
    # stamp CLAUDE_WORKER_RUNTIME (which may be "codex").
    if agent == "worker":
        runtime_env = f"CLAUDE_WORKER_RUNTIME={shlex.quote(runtime)} "
    else:
        runtime_env = ""
    # A caller-passed CLAUDE_CODE_OAUTH_TOKEN means the caller owns auth for
    # this spawn: skip pool routing entirely (no farm, no CLAUDE_ORCH_ACCOUNT
    # stamp). Note the default ~/.claude keychain login now OUTRANKS the token,
    # so a caller token no longer reliably forces a token identity — it's kept
    # as a defensive escape hatch and the record staying unstamped is truthful.
    # The raw caller token rides caller_env_parts below, shlex-quoted but VISIBLE
    # in ps: the caller's informed choice.
    caller_owns_auth = "CLAUDE_CODE_OAUTH_TOKEN" in (env or {})
    if caller_owns_auth:
        letter = None
    elif agent == "manager":
        letter = _active_account()   # manager rides the pointer (a->default, b->~/.claude-b)
    else:
        letter = _pick_account(force)     # worker usage-weighted picker (force bypasses breaker)
    if runtime == "claude":
        # L-11 official pre-trust. Host config FIRST: a first-build farm
        # copies the host file inside _build_account_prefix, so the entry
        # rides the copy — a farm-first write would be a minimal MCP-less
        # file that _claude_json_healthy rejects and rebuilds over.
        # Best-effort: a failed write degrades to the interactive dialog.
        trust.pretrust_dir(cwd)
    account_prefix = _build_account_prefix(letter)
    if runtime == "claude" and letter is not None and letter != config.default_account():
        farm_json = paths.account_config_dir(letter) / ".claude.json"
        # Healthy = the farm file spawn actually reads; an unhealthy farm
        # fell back to the default login and must not be planted with a
        # minimal file (it would mask the rebuild path).
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
