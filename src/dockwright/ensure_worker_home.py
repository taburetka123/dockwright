"""CLI: `dockwright ensure-worker-home` — create the configured/default worker
home so a bare spawn_worker never falls back to the manager's (untrusted) cwd on
a fresh install, and pre-trust it in the Claude config (E2E L-11) so the first
spawn never blocks on the interactive trust dialog. Prints the resolved path.
setup.sh calls this at install time."""
from __future__ import annotations

import argparse
import sys

from . import paths, trust


def main(argv=None) -> int:
    argparse.ArgumentParser(
        prog="dockwright ensure-worker-home",
        description="Ensure the worker home directory exists and is "
                    "pre-trusted; print its path.",
    ).parse_args(argv)
    home = paths.ensure_worker_home()
    # stdout carries EXACTLY the path: setup.sh captures it via command
    # substitution. Anything else goes to stderr.
    print(str(home))
    if not home.is_dir():
        print(f"WARNING: could not create worker home: {home}", file=sys.stderr)
        return 1
    if not trust.pretrust_dir(home):
        # Best-effort: a failed pre-trust degrades to the interactive dialog.
        print("WARNING: could not pre-trust worker home in "
              f"{trust._default_config_json()}", file=sys.stderr)
    return 0
