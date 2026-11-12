import fcntl
import json
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config, paths

USAGE = "usage: dockwright account-hold [<name> [--reason TEXT] | --release <name>]"
HELD_MESSAGE = ("held; the monitor points account-active away on its next tick (about a minute); "
                "spawns and flips skip it; running sessions finish on it")


@contextmanager
def _lock():
    paths.ACCOUNT_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(paths.ACCOUNT_LOCK, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def _load_state() -> dict:
    try:
        state = json.loads(paths.ACCOUNT_STATE.read_text())
    except FileNotFoundError:
        state = {}
    if not isinstance(state, dict):
        state = {}
    if not isinstance(state.get("accounts"), dict):
        state["accounts"] = {}
    if not isinstance(state.get("held"), dict):
        state["held"] = {}
    return state


def _write_state(state: dict) -> None:
    tmp = paths.ACCOUNT_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(paths.ACCOUNT_STATE)


def _ledger(entry: dict) -> None:
    paths.ACCOUNT_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(paths.ACCOUNT_LEDGER, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _clock(at) -> str:
    if not isinstance(at, (int, float)):
        return "?"
    return datetime.fromtimestamp(at, tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _known(name: str) -> bool:
    if name in config.account_names():
        return True
    pool = ", ".join(config.account_names())
    print(f"unknown account {name!r} (the pool resolves to {pool})", file=sys.stderr)
    return False


def _list() -> int:
    held = _load_state()["held"]
    if not held:
        print("no account on hold")
        return 0
    for name, mark in sorted(held.items()):
        mark = mark if isinstance(mark, dict) else {}
        print(f"{name} since {_clock(mark.get('at'))} reason={mark.get('reason')} source={mark.get('source')}")
    return 0


def _hold(name: str, reason: str) -> int:
    if not _known(name):
        return 2
    now = int(time.time())
    with _lock():
        state = _load_state()
        mark = state["held"].get(name)
        if isinstance(mark, dict):
            print(f"{name} already on hold since {_clock(mark.get('at'))} ({mark.get('reason')})")
            return 0
        state["held"][name] = {"at": now, "reason": reason, "source": "cli"}
        _write_state(state)
        _ledger({"ts": now, "event": "hold", "account": name, "reason": reason, "source": "cli", "by": "cli"})
    print(f"{name} {HELD_MESSAGE}")
    return 0


def _release(name: str) -> int:
    now = int(time.time())
    with _lock():
        state = _load_state()
        if name not in state["held"]:
            if not _known(name):
                return 2
            print(f"{name} is not on hold", file=sys.stderr)
            return 1
        del state["held"][name]
        _write_state(state)
        _ledger({"ts": now, "event": "release", "account": name, "source": "cli", "by": "cli"})
    print(f"{name} released; spawns and flips may pick it again")
    return 0


def main(argv: list) -> int:
    if not argv:
        return _list()
    if argv[0] == "--release":
        if len(argv) != 2:
            print(USAGE, file=sys.stderr)
            return 2
        return _release(argv[1])
    name = argv[0]
    rest = argv[1:]
    reason = "operator"
    if rest == []:
        pass
    elif len(rest) == 2 and rest[0] == "--reason" and rest[1]:
        reason = rest[1]
    else:
        print(USAGE, file=sys.stderr)
        return 2
    return _hold(name, reason)
