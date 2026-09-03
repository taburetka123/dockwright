#!/usr/bin/env python3
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


LAUNCHCTL_LIST_HEADER = ["PID", "Status", "Label"]


def launchctl_states() -> dict[str, str] | None:
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
        if len(parts) == 3 and parts != LAUNCHCTL_LIST_HEADER:
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
