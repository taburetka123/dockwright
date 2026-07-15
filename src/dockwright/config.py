"""dockwright.toml — dockwright's optional operator config.

Single config surface for everything that used to be hardcoded: state root,
worker home, account registry, model pins, hint strings, pricing overrides.

Contract (Step 1 of the OSS split):
- EVERY key is optional. A missing key — or a missing/corrupt file — yields
  the documented default, which reproduces the pre-config hardcoded behavior
  exactly. Fail-open: a bad config must never take down the fleet; `doctor`
  surfaces the parse error loudly instead (doctor's config:dockwright check).
  Exception (deprecated, one release): the three renamed path keys resolve
  filesystem-dependently to their legacy homes while orchestrator-era state
  migrates — see `_path_key_with_legacy`.
- No caching: readers parse the (tiny) file fresh per call — like the
  existing weight-env reads — so tests and live edits never fight a cache.
- Leaf module: imports nothing from the package (paths.py imports US at
  module level; a reverse import is a cycle).

Discovery order (first existing file wins):
  1. $DOCKWRIGHT_CONFIG — explicit path; when set it is authoritative
     (a missing target means "no config", never a fallback to 2/3)
  2. $XDG_CONFIG_HOME/dockwright/dockwright.toml (XDG_CONFIG_HOME default
     ~/.config)
  3. ~/.claude/dockwright.toml (the operator-overlay home)
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

ENV_CONFIG_PATH = "DOCKWRIGHT_CONFIG"

DEFAULT_STATE_ROOT = "~/.claude/dockwright"
LEGACY_STATE_ROOT = "~/.claude/orchestrator"          # deprecated, one release
DEFAULT_CLAUDE_CONFIG_HOME = "~/.claude"
DEFAULT_WORKER_HOME = "~/projects/work/worker"
DEFAULT_MANAGER_MEMORY = "~/.claude/dockwright/manager-memory"
LEGACY_MANAGER_MEMORY = "~/.claude/manager-memory"    # deprecated, one release
DEFAULT_OVERLAY_DIR = "~/.claude/dockwright-overlay"
LEGACY_OVERLAY_DIR = "~/.claude/orchestrator-overlay" # deprecated, one release
DEFAULT_WORKER_MODEL = "opus[1m]"
DEFAULT_MANAGER_MODEL = "opus[1m]"
DEFAULT_DISTILL_MODEL = "claude-sonnet-4-6"
DEFAULT_ASSIGN_COMMAND = "/manager-assign"
DEFAULT_WORKTREE_CLEANUP = ""
DEFAULT_LOOP_LABEL_PREFIX = "com.dockwright"
DEFAULT_ACCOUNT_NAME = "a"
DEFAULT_GARDENER_MODULE_ENABLED = True
DEFAULT_WORKTREE_ROOTS = "~/worktrees,~/worktrees-personal"
DEFAULT_REPO_ROOTS = "~/projects/work,~/projects/personal"
_DEFAULT_POOL = (("a", 1), ("b", 1))


@dataclass(frozen=True)
class Account:
    """One pool entry. config_dir=None means the ~/.claude-<name> convention
    (the default account runs on the default login and never consults it)."""
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
    """The dockwright.toml this process would read, or None when none exists."""
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
    """Parsed config dict, or {} (missing, unreadable, or corrupt — fail-open)."""
    path = config_path()
    if path is None:
        return {}
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def load_error() -> str | None:
    """Read/parse error text for an EXISTING config file (doctor's loud
    surface pairing with load()'s fail-open), else None."""
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
    """Explicit config value wins verbatim. Otherwise prefer the new default
    path when it exists on disk, fall back to the legacy default when only it
    exists (un-migrated install), else the new default (fresh install). The
    on-disk stat makes the un-pinned resolution filesystem-dependent — a
    deliberate one-release amendment of the parse-or-default contract while
    orchestrator-era state migrates (see migrate.py)."""
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
    """The pre-rename state root — monitor cursor normalization + migration."""
    return _expand(LEGACY_STATE_ROOT)


def claude_config_home() -> Path:
    return _path_key(_section(load(), "paths"), "claude_config_home",
                     DEFAULT_CLAUDE_CONFIG_HOME)


def worker_home_default() -> Path:
    return _path_key(_section(load(), "paths"), "worker_home", DEFAULT_WORKER_HOME)


def manager_memory_root() -> Path:
    return _path_key_with_legacy(_section(load(), "paths"), "manager_memory",
                                 DEFAULT_MANAGER_MEMORY, LEGACY_MANAGER_MEMORY)


def overlay_dir() -> Path:
    """Agent-file overlay drop-ins root (OUTSIDE state_root — that subtree is
    rsync --delete-managed runtime state)."""
    return _path_key_with_legacy(_section(load(), "paths"), "overlay_dir",
                                 DEFAULT_OVERLAY_DIR, LEGACY_OVERLAY_DIR)


def worktree_roots() -> str:
    """[paths] worktree_roots — comma-separated worktree root directories
    searched for existing worktrees (e.g. dockwright-general-work)."""
    return _str_key(_section(load(), "paths"), "worktree_roots", DEFAULT_WORKTREE_ROOTS)


def repo_roots() -> str:
    """[paths] repo_roots — comma-separated repo root directories for the
    same lookup."""
    return _str_key(_section(load(), "paths"), "repo_roots", DEFAULT_REPO_ROOTS)


def dockwright_repo() -> str:
    """[paths] dockwright_repo — this dockwright checkout's own path, for
    self-referential tooling (e.g. the Gardener digest/frontier skills).
    Default: "" (unset). Expanded (~ resolved) when set."""
    raw = _str_key(_section(load(), "paths"), "dockwright_repo", "")
    return str(_expand(raw)) if raw else ""


def agent_vars() -> dict[str, str]:
    """[agent_vars] — `{{name}}` substitutions for composed agent files.
    Non-string values are skipped (fail-open per entry)."""
    section = _section(load(), "agent_vars")
    return {k: v for k, v in section.items()
            if isinstance(k, str) and isinstance(v, str)}


def worker_model() -> str:
    return _str_key(_section(load(), "spawn"), "worker_model", DEFAULT_WORKER_MODEL)


def manager_model() -> str:
    return _str_key(_section(load(), "spawn"), "manager_model", DEFAULT_MANAGER_MODEL)


def distill_model() -> str:
    return _str_key(_section(load(), "spawn"), "distill_model", DEFAULT_DISTILL_MODEL)


def spawn_env() -> dict[str, str]:
    """[spawn.env] — extra environment variables merged into spawned worker
    sessions (caller-supplied env still wins). Non-string values are skipped
    (fail-open per entry)."""
    section = _section(_section(load(), "spawn"), "env")
    return {k: v for k, v in section.items()
            if isinstance(k, str) and isinstance(v, str)}


def assign_command_hint() -> str:
    return _str_key(_section(load(), "hints"), "assign_command", DEFAULT_ASSIGN_COMMAND)


def worktree_cleanup_hint() -> str:
    return _str_key(_section(load(), "hints"), "worktree_cleanup",
                    DEFAULT_WORKTREE_CLEANUP)


def loop_label_prefix() -> str:
    """launchd label namespace for background loops (bootlite-watchdog,
    gardener-gate/-frontier, worktree-prune, dlq-cookie-refresh, ...).
    Labels are rendered as "<prefix>.<loop-name>" at install time
    (deploy/scripts/*-install.sh); see deploy/loops-registry.md."""
    return _str_key(_section(load(), "loops"), "label_prefix",
                    DEFAULT_LOOP_LABEL_PREFIX)


def loop_status_overrides() -> dict[str, dict]:
    """[loops.status_overrides.<loop>] tables: operator's per-loop status/status_why
    overriding the core registry's neutral shipping defaults. {} when unset."""
    sec = _section(_section(load(), "loops"), "status_overrides")
    out = {}
    for name, val in sec.items():
        if isinstance(val, dict):
            ov = {k: v for k, v in val.items() if k in ("status", "status_why") and isinstance(v, str)}
            if ov:
                out[name] = ov
    return out


def gardener_module_enabled() -> bool:
    """[modules] gardener — toggles the Gardener retrospective pipeline
    (selffix/ops-evidence digest -> ranked improvement proposals). Non-bool
    values fall back to the default (enabled)."""
    val = _section(load(), "modules").get("gardener")
    return val if isinstance(val, bool) else DEFAULT_GARDENER_MODULE_ENABLED


def task_key_regex() -> str | None:
    """[task_keys] key_regex — regex matching a valid task key (e.g. an
    issue-tracker key like "ABC-1234"), used to recognize task references in
    free text. Unset, empty, or non-string falls back to None (no key
    derivation)."""
    val = _str_key(_section(load(), "task_keys"), "key_regex", "")
    return val if val else None


def gardener_high_skills() -> tuple[str, ...]:
    """[gardener] high_skills — skill names treated as "high" complexity by
    Gardener's task-triage heuristics. Non-string entries are skipped
    (fail-open per entry); a non-list value falls back to ()."""
    raw = _section(load(), "gardener").get("high_skills")
    if not isinstance(raw, list):
        return ()
    return tuple(x for x in raw if isinstance(x, str))


def _default_pool() -> list[Account]:
    return [Account(name=n, config_dir=None, weight=w) for n, w in _DEFAULT_POOL]


def accounts() -> list[Account]:
    """The account registry, pool order. ANY malformation (missing/empty/dup
    name, weight not a positive int, non-string config_dir) falls back to the
    whole default a/b pool — fail-open, never a half-registry."""
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


def pricing_overrides() -> dict[str, tuple[float, float]]:
    """[pricing.rates] entries as {model_key: (input, output)} USD/MTok.
    Invalid entries are skipped (fail-open per entry)."""
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
# Distilled manager-session journals.
manager_memory = "~/.claude/dockwright/manager-memory"
# Agent-file overlay drop-ins (compose seam). Kept OUTSIDE state_root —
# that subtree is rsync-managed runtime state.
overlay_dir = "~/.claude/dockwright-overlay"
# Comma-separated worktree root directories searched for existing worktrees
# (e.g. dockwright-general-work / ticket tooling).
worktree_roots = "~/worktrees,~/worktrees-personal"
# Comma-separated repo root directories for the same lookup.
repo_roots = "~/projects/work,~/projects/personal"
# This dockwright checkout's own path, for self-referential tooling (e.g.
# the Gardener digest/frontier skills). Default: unset. Example:
# dockwright_repo = "~/projects/personal/claude-orchestrator"

[spawn]
# Default --model for claude worker spawns.
worker_model = "opus[1m]"
# Pinned --model for manager tabs (kept independent of the worker default).
manager_model = "opus[1m]"
# Model for the headless manager-memory distill (`claude -p`).
distill_model = "claude-sonnet-4-6"

[spawn.env]
# Extra environment variables merged into spawned worker sessions (str ->
# str entries only; non-string values are skipped; caller-supplied env
# still wins). Example:
# MY_FLAG = "1"

[accounts]
# The account that runs on the default login (no CLAUDE_CONFIG_DIR).
default = "a"

# The account pool, in round-robin order. For a non-default account,
# config_dir defaults to the "~/.claude-<name>" convention; set it to
# relocate the account's config-dir farm. Weights bias the spawn
# round-robin (env CLAUDE_ORCH_ACCOUNT_WEIGHT_<NAME> still wins).
[[accounts.pool]]
name = "a"
weight = 1

[[accounts.pool]]
name = "b"
weight = 1

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

[modules]
# Toggle optional subsystems. Currently just the Gardener retrospective
# pipeline (selffix/ops-evidence digest -> ranked improvement proposals).
gardener = true

[task_keys]
# Regex matching a valid task key (e.g. an issue-tracker key like "ABC-1234"),
# used to recognize task references in free text. Default: unset — no
# key derivation. Example:
# key_regex = '[A-Za-z]{2,}-\\d+'

[gardener]
# Skill names treated as "high" complexity by Gardener's task-triage
# heuristics. Default: []. Example:
# high_skills = ["my-plugin:big-task-skill", "my-plugin:multi-step-skill"]

[agent_vars]
# {{name}} substitutions applied when composing agent files
# (setup.sh / `dockwright compose`).

[pricing.rates]
# Per-model [input, output] USD/MTok overrides, merged over the built-in
# table in pricing.py (fable/opus/sonnet/haiku). Example:
# opus = [5.0, 25.0]
'''
