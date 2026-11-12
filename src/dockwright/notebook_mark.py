import argparse
import shlex
import sys
import time
from pathlib import Path

from . import paths, state

MARK_MAX_AGE_SEC = 3600


def notebook_file(domain: str) -> Path:
    return paths.ROOT / "notebook" / f"{domain}.md"


def mark_path(domain: str) -> Path:
    return paths.ROOT / "notebook" / "marks" / f"{paths._safe_segment(domain)}.json"


def _has_notebook(domain: str) -> bool:
    path = notebook_file(domain)
    return path.is_file() and path.stat().st_size > 0


def read(domain: str) -> dict | None:
    mark = state.read_json(mark_path(domain))
    if not isinstance(mark, dict):
        return None
    sid, at, reason = mark.get("sid"), mark.get("at"), mark.get("reason")
    if not (isinstance(sid, str) and sid
            and isinstance(at, (int, float)) and not isinstance(at, bool)
            and isinstance(mark.get("actualized"), bool)
            and (reason is None or isinstance(reason, str))):
        return None
    try:
        time.gmtime(at)
    except (OverflowError, ValueError, OSError):
        return None
    return mark


def write(sid: str, domain: str, reason: str | None = None, now: float | None = None) -> Path:
    path = mark_path(domain)
    state.write_json_atomic(path, {"sid": sid, "at": now if now is not None else time.time(),
                                   "actualized": reason is None, "reason": reason})
    return path


def write_unless_fresh(sid: str, domain: str, reason: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    try:
        if not _has_notebook(domain):
            return False
        mark = read(domain)
        if mark and mark["actualized"] and abs(now - mark["at"]) <= MARK_MAX_AGE_SEC:
            return False
        write(sid, domain, reason, now)
    except (OSError, ValueError):
        return False
    return True


def refusal(sid: str, domain: str, now: float | None = None) -> str | None:
    if not _has_notebook(domain):
        return None
    now = time.time() if now is None else now
    mark = read(domain)
    if mark is None:
        why = "no mark"
    elif mark["sid"] != sid:
        why = f"the mark is from session {mark['sid']}"
    elif now - mark["at"] > MARK_MAX_AGE_SEC:
        why = f"the mark is {int((now - mark['at']) // 60)} minutes old"
    elif mark["at"] - now > MARK_MAX_AGE_SEC:
        why = "the mark is dated in the future"
    else:
        return None
    command = f"dockwright notebook mark {_option('--sid', sid)} {_option('--domain', domain)}"
    return (f"notebook {notebook_file(domain)} carries no mark of session {sid} from the last "
            f"{MARK_MAX_AGE_SEC // 60} minutes ({why}). Run the pass (Skill: dockwright-notebook-hygiene), then "
            f"`{command}`. Context exhausted or an urgent recreate: `{command} --not-actualized '<reason>'`.")


def _option(flag: str, value: str) -> str:
    quoted = shlex.quote(value)
    return f"{flag}={quoted}" if quoted.startswith("-") else f"{flag} {quoted}"


def _record_domain(sid: str) -> str | None:
    record = state.read_json(paths.ACTIVE / f"{sid}.json")
    if not isinstance(record, dict) or record.get("agent") != "manager":
        return None
    return record.get("domain") or paths.DEFAULT_DOMAIN


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dockwright notebook",
                                     description="The mark a manager writes after its notebook pass; "
                                                 "prepare_handoff, close_manager_self and bootstrap-recreate.sh "
                                                 "check it.")
    subs = parser.add_subparsers(dest="cmd", required=True)
    mark = subs.add_parser("mark", help="Write this session's mark for the domain's notebook.")
    check = subs.add_parser("check", help="Exit 0 when a handoff of this session would pass, else 1.")
    for sub in (mark, check):
        sub.add_argument("--sid", required=True, help="Your session id.")
        sub.add_argument("--domain", help="Default: the domain on the session's active manager record.")
    mark.add_argument("--not-actualized", metavar="REASON",
                      help="Mark the notebook as not actualized, with the reason the pass could not run.")
    args = parser.parse_args(argv)
    domain = args.domain if args.domain is not None else _record_domain(args.sid)
    if domain is None:
        print(f"session {args.sid} has no active manager record; pass --domain", file=sys.stderr)
        return 2
    try:
        path = mark_path(domain)
    except ValueError as err:
        print(f"dockwright notebook: {err}", file=sys.stderr)
        return 2
    if args.cmd == "check":
        why = refusal(args.sid, domain)
        if why is not None:
            print(why, file=sys.stderr)
            return 1
        print("ok")
        return 0
    reason = args.not_actualized
    if reason is not None:
        reason = reason.strip()
        if not reason:
            print("dockwright notebook: --not-actualized needs a reason", file=sys.stderr)
            return 2
    write(args.sid, domain, reason)
    print(f"marked {path}: " + ("actualized" if reason is None else f"not actualized — {reason}"))
    return 0
