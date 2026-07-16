"""Deploy-time finalization of worker-spawn settings presets.

The shipped worker-headless-settings.json stays generic; a headless worker
still needs out-of-cwd access to the operator's code roots or its first
task-repo write stalls on the directory-access gate (E2E rc.2 N-3).
`additionalDirectories` values must be ABSOLUTE — tilde expansion there is
undocumented — so setup.sh injects the resolved [paths] roots into the
DEPLOYED copy after the overlay step. An operator-set key (even []) is
respected verbatim.
"""
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
    """Inject permissions.additionalDirectories into the deployed preset.

    Returns True when injected; False when the key already exists (operator
    intent — including an explicit [] — wins verbatim, file untouched).
    """
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
