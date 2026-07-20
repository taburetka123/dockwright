"""`dockwright accounts-sync` — reconcile every registered pool account's
config-dir farm (symlink heal + .claude.json mcpServers refresh, both via
spawner.ensure_account_config_dir) and print a parity report per account.

Heals ONLY farms that already exist: the registry fail-opens to a default
a/b pool, so eager provisioning would grow ~/.claude-<name> on installs that
never spawn pool accounts; a never-spawned account builds its farm at first
spawn exactly as before. Exit 2 = bad argv; 0 otherwise — drift is a report,
not an install failure (setup.sh runs this under `set -euo pipefail` right
before doctor)."""
from __future__ import annotations

import sys

from . import config, paths, spawner

USAGE = "Usage: dockwright accounts-sync (no arguments)"

_CLEAN_JSON_STATES = ("in-sync", "unverified")


def main(argv: list[str]) -> int:
    if argv:
        print(USAGE, file=sys.stderr)
        return 2
    default = config.default_account()
    synced = 0
    for account in config.accounts():
        if account.name == default:
            continue
        farm = paths.account_config_dir(account.name)
        if not farm.is_dir():
            print(f"account {account.name}: no config dir at {farm} — "
                  "never provisioned, skipping")
            continue
        try:
            spawner.ensure_account_config_dir(account.name)
        except OSError as exc:
            print(f"account {account.name}: farm not reconcilable ({exc}) — skipped",
                  file=sys.stderr)
            continue
        synced += 1
        report = spawner.farm_parity_report(account.name)
        clean = (not report["drift"] and not report["missing"]
                 and report["claude_json"] in _CLEAN_JSON_STATES)
        if clean:
            print(f"account {account.name}: OK ({report['shared']} shared entries, "
                  f".claude.json {report['claude_json']})")
            continue
        print(f"account {account.name}: reconciled with warnings "
              f"({report['shared']} shared entries)")
        for name in report["drift"]:
            print(f"  ⚠ drift: {name} is a real path where a symlink belongs "
                  "(kept — a live same-account session may own it; migrate manually)")
        for name in report["missing"]:
            print(f"  ⚠ missing: {name} has no farm entry")
        if report["claude_json"] not in _CLEAN_JSON_STATES:
            print(f"  ⚠ .claude.json: {report['claude_json']}")
    if synced == 0:
        print("accounts-sync: no provisioned pool-account farms to reconcile")
    return 0
