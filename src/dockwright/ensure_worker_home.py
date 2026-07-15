"""CLI: `dockwright ensure-worker-home` — create the configured/default worker
home so a bare spawn_worker never falls back to the manager's (untrusted) cwd on
a fresh install. Prints the resolved path. setup.sh calls this at install time."""
from __future__ import annotations

import argparse
import sys

from . import paths


def main(argv=None) -> int:
    argparse.ArgumentParser(
        prog="dockwright ensure-worker-home",
        description="Ensure the worker home directory exists; print its path.",
    ).parse_args(argv)
    home = paths.ensure_worker_home()
    print(str(home))
    if home.is_dir():
        return 0
    print(f"WARNING: could not create worker home: {home}", file=sys.stderr)
    return 1
