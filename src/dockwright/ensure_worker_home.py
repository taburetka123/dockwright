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
    print(str(home))
    if not home.is_dir():
        print(f"WARNING: could not create worker home: {home}", file=sys.stderr)
        return 1
    if not trust.pretrust_dir(home):
        print("WARNING: could not pre-trust worker home in "
              f"{trust._default_config_json()}", file=sys.stderr)
    return 0
