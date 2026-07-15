#!/usr/bin/env python3
"""loops-status — read-only fleet health report over the loops registry.

Reads the structured ```loop blocks from the loops registry (deployed at
~/.claude/dockwright/loops-registry.md by setup.sh; source deploy/loops-registry.md
in taburetka123/claude-orchestrator) and reconciles each loop's INTENDED state
(the block's `status` field) against the machine:

  - launchd: label loaded? last exit status (launchctl list)
  - hook loops: hook_command wired into ~/.claude/settings.json?
  - stop file present? (kill_switch fields that are paths)
  - newest event_paths mtime age vs max_silence_hours → fresh/STALE
  - runtime_program_path exists?

Pure report: never mutates anything, always exits 0 (2 on usage/registry-missing
errors). The ENFORCEMENT lives in tests/test_loops_registry.py, which imports
parse_registry from this file so the registry format has exactly one parser.

The same architecture review that mandated this tool found two loops dark for
weeks (ticket-cleanup 8wk, pr-review-poller 17d) because nothing read launchd's
own records — this is the thing that reads them.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("HOME", ""))


def _prefer_new(new: Path, legacy: Path) -> Path:
    # deprecated, one release: legacy fallback while orchestrator-era state migrates
    if new.exists():
        return new
    if legacy.exists():
        return legacy
    return new


DEPLOYED_REGISTRY = _prefer_new(HOME / ".claude" / "dockwright" / "loops-registry.md", HOME / ".claude" / "loops-registry.md")
REPO_REGISTRY = Path(__file__).resolve().parent.parent / "loops-registry.md"
SETTINGS_PATH = HOME / ".claude" / "settings.json"
DEFAULT_LABEL_PREFIX = "com.dockwright"
DEFAULT_OVERLAY_DIR = _prefer_new(HOME / ".claude" / "dockwright-overlay", HOME / ".claude" / "orchestrator-overlay")

LOOP_BLOCK_RE = re.compile(r"```loop\n(.*?)```", re.DOTALL)


def _resolve_config():
    """Best-effort import of dockwright.config so a deployed standalone
    copy under /usr/bin/python3 (no package on PATH) can still resolve the
    operator's [loops].label_prefix + [paths].overlay_dir. config is a stdlib-only
    leaf module, so a sys.path insert of the repo src suffices. Mirrors
    bootlite_watchdog._resolve_get_driver; returns the module or None."""
    try:
        from dockwright import config
        return config
    except Exception:
        pass
    try:
        src = os.environ.get("CLAUDE_ORCH_SRC") or str(
            HOME / "projects" / "personal" / "claude-orchestrator" / "src")
        if Path(src).is_dir() and src not in sys.path:
            sys.path.insert(0, src)
        from dockwright import config
        return config
    except Exception:
        return None


_CONFIG = _resolve_config()


def loop_label_prefix() -> str:
    """The operator's launchd label namespace ([loops].label_prefix), or the
    com.dockwright product default when config is unavailable (fail-open)."""
    if _CONFIG is not None:
        try:
            return _CONFIG.loop_label_prefix()
        except Exception:
            pass
    return DEFAULT_LABEL_PREFIX


def _default_overlay_dir() -> Path:
    if _CONFIG is not None:
        try:
            return _CONFIG.overlay_dir()
        except Exception:
            pass
    return DEFAULT_OVERLAY_DIR


def _config_status_overrides() -> dict[str, dict]:
    """The operator's per-loop status/status_why overrides
    ([loops.status_overrides.<name>]), or {} when config is unavailable
    (fail-open, mirroring loop_label_prefix)."""
    if _CONFIG is not None:
        try:
            return _CONFIG.loop_status_overrides()
        except Exception:
            pass
    return {}

REQUIRED_FIELDS = (
    "name", "label", "status", "status_why", "trigger", "gate", "run_contract",
    "permissions_mode", "ledger_path", "kill_switch", "runtime_program_path",
    "source_path", "deploy_mechanism", "log_paths", "event_paths",
    "max_silence_hours", "last_verified",
)
VALID_STATUSES = ("live", "paused", "retiring", "retired", "pending-install")


def parse_registry(text: str) -> list[dict]:
    """Parse ```loop fenced blocks of `key: value` lines into dicts.

    Values are plain strings (no quoting/nesting); unknown keys are kept so the
    schema can grow without touching the parser."""
    loops = []
    for match in LOOP_BLOCK_RE.finditer(text):
        block: dict = {}
        for line in match.group(1).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition(":")
            if not sep:
                continue
            block[key.strip()] = value.strip()
        if block:
            loops.append(block)
    return loops


def registry_path(cli_arg: str | None) -> Path | None:
    candidates = []
    if cli_arg:
        candidates.append(Path(cli_arg).expanduser())
    env = os.environ.get("LOOPS_REGISTRY")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.extend([DEPLOYED_REGISTRY, REPO_REGISTRY])
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def registry_paths(cli_arg: str | None = None,
                   overlay_dir: str | Path | None = None) -> list[Path]:
    """Every registry file to union: the resolved core registry (deployed-or-repo)
    first, then every `<overlay>/loops/*.md` (sorted). Missing pieces are simply
    omitted — a single-file install with no overlay yields just the core path."""
    paths: list[Path] = []
    core = registry_path(cli_arg)
    if core is not None:
        paths.append(core)
    root = Path(overlay_dir).expanduser() if overlay_dir is not None \
        else _default_overlay_dir()
    loops_dir = root / "loops"
    if loops_dir.is_dir():
        paths.extend(sorted(loops_dir.glob("*.md")))
    return paths


def load_all_loops(cli_arg: str | None = None,
                  overlay_dir: str | Path | None = None,
                  prefix: str | None = None,
                  status_overrides: dict | None = None) -> list[dict]:
    """Parse + union the blocks from every registry_paths() file, expanding any
    literal `{prefix}` in each block's `label` to the resolved label prefix. The
    core registry ships `{prefix}.<name>` templates (product-generic); the overlay
    keeps the operator's literal labels. `prefix` overrides the config-resolved
    default (tests pass the live operator prefix; production leaves it None).
    `status_overrides` ({name: {"status": …, "status_why": …}}) replaces those two
    keys on the matching-name block after parsing — the core registry ships
    neutral pending-install statuses; the operator flips loops live via
    [loops.status_overrides] in dockwright.toml. None → config-resolved."""
    if prefix is None:
        prefix = loop_label_prefix()
    if status_overrides is None:
        status_overrides = _config_status_overrides()
    loops: list[dict] = []
    for path in registry_paths(cli_arg, overlay_dir):
        for block in parse_registry(path.read_text()):
            if "label" in block:
                block["label"] = block["label"].replace("{prefix}", prefix)
            override = status_overrides.get(block.get("name"))
            if isinstance(override, dict):
                block.update({k: v for k, v in override.items()
                              if k in ("status", "status_why")})
            loops.append(block)
    return loops


def launchctl_states() -> dict[str, str] | None:
    """Map launchd label -> last-exit-status string, None if launchctl unavailable."""
    try:
        result = subprocess.run(["launchctl", "list"], capture_output=True,
                                timeout=10, check=False, text=True)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    states = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2].startswith("com."):
            states[parts[2]] = parts[1]
    return states


def hook_wired(hook_command: str) -> bool | None:
    try:
        return hook_command in SETTINGS_PATH.read_text()
    except OSError:
        return None


def _expand(path_str: str) -> Path:
    return Path(path_str.replace("~", str(HOME), 1)) if path_str.startswith("~") \
        else Path(path_str)


def newest_event_age_hours(event_paths: str, now: float) -> float | None:
    newest = None
    for raw in event_paths.split(","):
        raw = raw.strip()
        if not raw or raw == "none":
            continue
        target = _expand(raw)
        try:
            mtime = target.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    if newest is None:
        return None
    return (now - newest) / 3600


def loop_report(loop: dict, launchd: dict[str, str] | None, now: float) -> dict:
    report = {"name": loop.get("name"), "status": loop.get("status")}
    label = loop.get("label", "none")
    if label != "none":
        if launchd is None:
            report["launchd"] = "unknown (launchctl unavailable)"
        elif label in launchd:
            report["launchd"] = f"loaded, last exit {launchd[label]}"
        else:
            report["launchd"] = "not loaded"
    hook_command = loop.get("hook_command")
    if hook_command:
        wired = hook_wired(hook_command)
        report["hook"] = "unknown" if wired is None else ("wired" if wired else "unwired")

    kill_switch = loop.get("kill_switch", "")
    if kill_switch.startswith("~") or kill_switch.startswith("/"):
        report["stop_file"] = "PRESENT" if _expand(kill_switch).exists() else "absent"

    program = loop.get("runtime_program_path", "")
    if program and program != "none":
        report["program"] = "ok" if _expand(program).exists() else "MISSING"

    max_silence = loop.get("max_silence_hours", "none")
    if loop.get("status") == "live" and max_silence != "none":
        age = newest_event_age_hours(loop.get("event_paths", ""), now)
        try:
            limit = float(max_silence)
        except ValueError:
            limit = None
        if age is None:
            report["freshness"] = "STALE (no events found)"
        elif limit is not None and age > limit:
            report["freshness"] = f"STALE ({age:.0f}h since last event, limit {limit:.0f}h)"
        else:
            report["freshness"] = f"fresh ({age:.1f}h ago)"

    flags = []
    status = loop.get("status")
    if status == "live" and report.get("stop_file") == "PRESENT":
        flags.append("DRIFT: intended live but stop file present")
    if label != "none" and launchd is not None:
        loaded = label in launchd
        if status == "live" and not loaded:
            flags.append("DRIFT: intended live but not loaded")
        if status in ("paused", "retired") and loaded:
            flags.append(f"DRIFT: intended {status} but loaded")
    if hook_command and report.get("hook") == "wired" and status == "paused":
        flags.append("DRIFT: intended paused but hook wired")
    if hook_command and report.get("hook") == "unwired" and status == "live":
        flags.append("DRIFT: intended live but hook unwired")
    if report.get("program") == "MISSING" and status in ("live", "paused"):
        flags.append("DRIFT: program path missing")
    if "STALE" in report.get("freshness", ""):
        flags.append("STALE")
    report["flags"] = flags
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only loops fleet health report.")
    parser.add_argument("--registry", help="Registry path (default: deployed copy, then repo).")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = parser.parse_args(argv)

    path = registry_path(args.registry)
    if path is None:
        print("loops-status: no registry found (deploy via setup.sh or pass --registry)",
              file=sys.stderr)
        return 2
    loops = load_all_loops(cli_arg=args.registry)
    launchd = launchctl_states()
    now = time.time()
    reports = [loop_report(loop, launchd, now) for loop in loops]

    if args.json:
        print(json.dumps(reports, indent=2))
        return 0
    print(f"loops-status — {len(reports)} loops ({path})")
    for report in reports:
        flags = " ".join(report["flags"]) if report["flags"] else "ok"
        details = "  ".join(
            f"{key}={value}" for key, value in report.items()
            if key not in ("name", "flags") and value is not None
        )
        print(f"  {report['name']:20} [{flags}]  {details}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
