from __future__ import annotations

import argparse
import sys

from . import paths, trust


def main(argv=None) -> int:
    argparse.ArgumentParser(
        prog="dockwright ensure-manager-home",
        description="Ensure the manager home directory exists and is "
                    "pre-trusted; print its path.",
    ).parse_args(argv)
    try:
        home = paths.ensure_manager_home()
    except ValueError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    print(str(home))
    if not trust.pretrust_dir(home):
        print("WARNING: could not pre-trust manager home in "
              f"{trust._default_config_json()}", file=sys.stderr)
    return 0
