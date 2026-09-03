from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import config


def _split_roots(csv: str) -> list[str]:
    return [os.path.expanduser(p.strip()) for p in csv.split(",") if p.strip()]


def _dedupe_nested(dirs: list[str]) -> list[str]:
    out: list[str] = []
    for d in sorted({d.rstrip(os.sep) or os.sep for d in dirs}):
        if not any(d == kept or d.startswith(kept + os.sep) for kept in out):
            out.append(d)
    return out


def headless_additional_dirs() -> list[str]:
    dirs = _split_roots(config.repo_roots()) + _split_roots(config.worktree_roots())
    dirs.append(os.path.expanduser(str(config.worker_home_default())))
    return _dedupe_nested(dirs)


def finalize_headless_settings(path: Path) -> bool:
    data = json.loads(path.read_text())
    perms = data.setdefault("permissions", {})
    if "additionalDirectories" in perms:
        return False
    perms["additionalDirectories"] = headless_additional_dirs()
    path.write_text(json.dumps(data, indent=2) + "\n")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="dockwright finalize-presets",
        description="Inject operator-absolute permissions.additionalDirectories "
                    "into the deployed worker-headless settings preset.")
    parser.add_argument("--file", type=Path, required=True,
                        help="Deployed preset JSON to finalize in place.")
    args = parser.parse_args(argv)
    if not args.file.is_file():
        print(f"finalize-presets: no such file: {args.file}", file=sys.stderr)
        return 1
    if finalize_headless_settings(args.file):
        print(f"finalize-presets: injected additionalDirectories into {args.file}")
    else:
        print(f"finalize-presets: operator additionalDirectories present, "
              f"left untouched: {args.file}")
    return 0
