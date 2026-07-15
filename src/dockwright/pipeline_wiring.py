"""`dockwright selffix|gardener enable|disable` — opt-in wiring for the
self-improvement pipeline (ships OFF on a fresh install).

selffix = the SessionEnd hook that fires selffix-trigger.sh (the per-session
retro producer). gardener = the launchd loops that digest findings into ranked
proposals (the consumer). One module because gardener's dependency gate reads
selffix's settings.json state and its disable shares launchd-label helpers.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config
from .env_install import prune_backups

SELFFIX_TRIGGER = "selffix-trigger.sh"
SELFFIX_HOOK_TIMEOUT = 30
GARDENER_LANES = ("digest", "frontier", "all")
_LANE_LOOPS = {
    "digest": ("gardener-gate",),
    "frontier": ("gardener-frontier",),
    "all": ("gardener-gate", "gardener-frontier"),
}


# --- path resolution (overridable via CLI flags for tests) -------------------

def _settings_path() -> Path:
    return config.claude_config_home() / "settings.json"


def _scripts_dir() -> Path:
    return config.claude_config_home() / "scripts"


def _launch_agents_dir() -> Path:
    return Path(os.environ.get("HOME", "")) / "Library" / "LaunchAgents"


def _gardener_labels(lane: str, label_prefix: str) -> list[str]:
    return [f"{label_prefix}.{loop}" for loop in _LANE_LOOPS[lane]]


def _gardener_installed(launch_agents_dir: Path, label_prefix: str) -> bool:
    return any((launch_agents_dir / f"{label}.plist").exists()
               for label in _gardener_labels("all", label_prefix))


# --- selffix (SessionEnd hook wiring) ----------------------------------------

def selffix_hook_command(scripts_dir: Path) -> str:
    return f"bash {shlex.quote(str(scripts_dir / SELFFIX_TRIGGER))}"


def is_selffix_wired(settings_path: Path) -> bool:
    if not settings_path.exists():
        return False
    try:
        settings = json.loads(settings_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    for block in settings.get("hooks", {}).get("SessionEnd", []):
        for hook in block.get("hooks", []):
            if SELFFIX_TRIGGER in hook.get("command", ""):
                return True
    return False


def _write_settings_atomic(path: Path, settings: dict, previous_text: str | None) -> bool:
    new_text = json.dumps(settings, indent=2) + "\n"
    if new_text == previous_text:
        return False
    if previous_text is not None:
        path.with_name(path.name + f".bak.{time.time_ns()}").write_text(previous_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text)
    prune_backups(path)
    return True


def enable_selffix(settings_path: Path, scripts_dir: Path) -> bool:
    previous_text = settings_path.read_text() if settings_path.exists() else None
    settings = json.loads(previous_text) if previous_text else {}
    session_end = settings.setdefault("hooks", {}).setdefault("SessionEnd", [])
    already = any(SELFFIX_TRIGGER in h.get("command", "")
                  for b in session_end for h in b.get("hooks", []))
    if already:
        return False
    session_end.append({"hooks": [{
        "type": "command",
        "command": selffix_hook_command(scripts_dir),
        "timeout": SELFFIX_HOOK_TIMEOUT,
    }]})
    return _write_settings_atomic(settings_path, settings, previous_text)


def disable_selffix(settings_path: Path) -> bool:
    if not settings_path.exists():
        return False
    previous_text = settings_path.read_text()
    settings = json.loads(previous_text)
    present = any(SELFFIX_TRIGGER in h.get("command", "")
                  for b in settings.get("hooks", {}).get("SessionEnd", [])
                  for h in b.get("hooks", []))
    if not present:
        return False   # strict no-op when not wired (never reformats/backs up)
    hooks = settings.get("hooks", {})
    new_blocks = []
    for block in hooks.get("SessionEnd", []):
        kept = [h for h in block.get("hooks", [])
                if SELFFIX_TRIGGER not in h.get("command", "")]
        if kept:
            new_blocks.append({**block, "hooks": kept})
    if new_blocks:
        hooks["SessionEnd"] = new_blocks
    elif "SessionEnd" in hooks:
        del hooks["SessionEnd"]
    if "hooks" in settings and not settings["hooks"]:
        del settings["hooks"]
    return _write_settings_atomic(settings_path, settings, previous_text)


def selffix_main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="dockwright selffix")
    p.add_argument("action", choices=("enable", "disable"))
    p.add_argument("--settings", type=Path, default=None)
    p.add_argument("--scripts-dir", type=Path, default=None)
    args = p.parse_args(argv)
    settings_path = args.settings or _settings_path()
    scripts_dir = args.scripts_dir or _scripts_dir()
    try:
        if args.action == "enable":
            changed = enable_selffix(settings_path, scripts_dir)
            print("selffix enabled (SessionEnd hook wired)." if changed
                  else "selffix already enabled.")
        else:
            changed = disable_selffix(settings_path)
            print("selffix disabled (SessionEnd hook removed)." if changed
                  else "selffix already disabled.")
            if _gardener_installed(_launch_agents_dir(), config.loop_label_prefix()):
                print("  note: gardener loops still installed — they log an hourly "
                      "'producer missing' warning until you also run "
                      "`dockwright gardener disable` (or re-enable selffix).",
                      file=sys.stderr)
    except json.JSONDecodeError:
        print(f"ERROR: {settings_path} is not valid JSON; fix it first.", file=sys.stderr)
        return 1
    return 0


# --- gardener (launchd loops) ------------------------------------------------

def _default_run(cmd: list[str]) -> int:
    return subprocess.run(cmd, check=False).returncode


def _gardener_installer(scripts_dir: Path) -> Path:
    return scripts_dir / "gardener-install.sh"


def enable_gardener(lane: str, *, settings_path: Path, installer_path: Path, run) -> int:
    if shutil.which("launchctl") is None:
        print("ERROR: gardener loops are scheduled via launchd (macOS-only); "
              "launchctl not found on this system.", file=sys.stderr)
        return 2
    if not installer_path.is_file():
        print(f"ERROR: {installer_path} not found — run setup.sh first.", file=sys.stderr)
        return 2
    if lane in ("digest", "all") and not is_selffix_wired(settings_path):
        print("gardener's digest lane consumes selffix findings, but selffix is not "
              "enabled. Run `dockwright selffix enable` first "
              "(or `dockwright gardener enable --lane frontier` for the research lane only).",
              file=sys.stderr)
        return 1
    return run(["bash", str(installer_path), "--lane", lane])


def disable_gardener(lane: str, *, launch_agents_dir: Path, label_prefix: str, run, uid: int) -> int:
    launchctl_available = shutil.which("launchctl") is not None
    if not launchctl_available:
        print("gardener disable: launchctl not found — removing plist files only.",
              file=sys.stderr)
    for label in _gardener_labels(lane, label_prefix):
        if launchctl_available:
            run(["launchctl", "bootout", f"gui/{uid}/{label}"])   # best-effort
        (launch_agents_dir / f"{label}.plist").unlink(missing_ok=True)
    return 0


def gardener_main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="dockwright gardener")
    p.add_argument("action", choices=("enable", "disable"))
    p.add_argument("--lane", choices=GARDENER_LANES, default=None)
    p.add_argument("--settings", type=Path, default=None)
    p.add_argument("--scripts-dir", type=Path, default=None)
    args = p.parse_args(argv)
    # Conservative per-action defaults: minimal install, full teardown.
    lane = args.lane or ("digest" if args.action == "enable" else "all")
    settings_path = args.settings or _settings_path()
    scripts_dir = args.scripts_dir or _scripts_dir()
    if args.action == "enable":
        rc = enable_gardener(lane, settings_path=settings_path,
                             installer_path=_gardener_installer(scripts_dir),
                             run=_default_run)
        if rc == 0:
            print(f"gardener enabled (--lane {lane}).")
            if lane == "digest":
                print("  (frontier web-research sweep NOT installed; add it with --lane all.)")
        return rc
    disable_gardener(lane, launch_agents_dir=_launch_agents_dir(),
                     label_prefix=config.loop_label_prefix(),
                     run=_default_run, uid=os.getuid())
    print(f"gardener disabled (--lane {lane}: launchd jobs removed).")
    print("  note: if a [loops.status_overrides] entry marks these loops 'live', "
          "flip it back or test_loops_registry.py reconciliation will fail.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(selffix_main())
