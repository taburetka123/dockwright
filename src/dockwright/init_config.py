"""`dockwright init` — write the documented-defaults dockwright.toml.

The written file reproduces every default explicitly (the drift test in
tests/test_config.py guarantees template values == code defaults), so a fresh
operator gets a self-documenting config that changes nothing until edited.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import config


def _default_target() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path(os.environ.get("HOME", "")) / ".config"
    return base / "dockwright" / "dockwright.toml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dockwright init",
        description="Write a dockwright.toml with the documented defaults.")
    parser.add_argument("--path", type=Path, default=None,
                        help="Target file (default: $XDG_CONFIG_HOME/dockwright/dockwright.toml)")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite an existing file.")
    args = parser.parse_args(argv)

    target = args.path or _default_target()
    if target.exists() and not args.force:
        print(f"ERROR: {target} already exists (use --force to overwrite)",
              file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(config.DEFAULT_TOML)
    print(f"Wrote {target}")
    active = config.config_path()
    if active is not None and active != target:
        print(f"note: this process would read {active} (discovery order: "
              f"$DOCKWRIGHT_CONFIG, XDG, ~/.claude/dockwright.toml)")
    return 0
