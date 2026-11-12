import contextlib
import fcntl
import os
import re
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

from . import config, paths, topic

CHECK_EVERY_SEC = 3600
BUDGET_SEC = 20
SEGMENT_TIMEOUT_SEC = 15
MAX_SEGMENTS = 5
MAX_STAGES = 4
OUTPUT_LIMIT = 64 * 1024
RECORD_CHARS = 500
LOCK_NAME = ".check.lock"
PAGER_ENV = {"GH_PAGER": "cat", "GIT_PAGER": "cat", "PAGER": "cat", "GH_PROMPT_DISABLED": "1",
             "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_ALLOW_PROTOCOL": "file:git:http:https:ssh"}
_CHECK_LINE = re.compile(r"^[ \t]*(?:-[ \t]*)?(?:\*\*)?check:(?:\*\*)?(.*)$", re.MULTILINE)
_SEGMENT = re.compile(r"`([^`\n]+)`")
_METACHARS = frozenset(";&><$()\n\r")
_DIGITS = re.compile(r"^-\d+$")
_API_PATH = re.compile(r"^[A-Za-z0-9_./-]+$")
_REMOTE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_NOT_STARTED = {"status": "timeout", "exit": None, "output": "not started: the scan's time budget ran out"}


class Refused(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    bare: frozenset
    valued: frozenset
    digits: bool = False


def _rule(bare: str = "", valued: str = "", digits: bool = False) -> Rule:
    return Rule(frozenset(bare.split()), frozenset(valued.split()), digits)


_GH_VIEW = _rule(valued="--repo -R --json --jq -q")
_GH_LIST = _rule(valued="--repo -R --json --jq -q --state -s --author -A --limit -L --head -H --base -B")
GH_RULES = {("pr", "view"): _GH_VIEW, ("pr", "checks"): _GH_VIEW, ("issue", "view"): _GH_VIEW,
            ("repo", "view"): _GH_VIEW, ("run", "view"): _GH_VIEW,
            ("pr", "list"): _GH_LIST, ("issue", "list"): _GH_LIST, ("run", "list"): _GH_LIST}
GH_API_RULE = _rule(bare="--paginate", valued="--jq -q")
GIT_RULES = {"log": _rule(bare="--oneline", valued="-n --format --since --until --author", digits=True),
             "status": _rule(bare="--porcelain --short -s --branch -b"),
             "rev-parse": _rule(bare="--abbrev-ref --short --show-toplevel --verify"),
             "ls-remote": _rule(bare="--heads --tags --refs"),
             "grep": _rule(bare="-c -l -n -i -F -E -w -q", valued="-e")}
PLAIN_RULES = {"ls": _rule(bare="-l -a -1 -t -r -d -h"), "cat": _rule(), "test": _rule(bare="-e -f -d -s"),
               "wc": _rule(bare="-l -c -w"), "head": _rule(valued="-n -c", digits=True),
               "tail": _rule(valued="-n -c", digits=True),
               "grep": _rule(bare="-c -l -L -n -i -F -E -w -x -v -q -r -s -h", valued="-e")}
STATUS_RULE = _rule(bare="--json")


def commands_of(text: str) -> list[str]:
    m = _CHECK_LINE.search(text or "")
    return _SEGMENT.findall(m.group(1))[:MAX_SEGMENTS] if m else []


def parse(segment: str, cwd: str | None = None) -> list[list[str]]:
    bad = sorted(set(segment) & _METACHARS)
    if bad:
        raise Refused(f"shell metacharacter {bad[0]!r}")
    try:
        tokens = [_expand(t) for t in shlex.split(segment, posix=True)]
    except ValueError as err:
        raise Refused(f"cannot split: {err}") from err
    stages: list[list[str]] = [[]]
    for token in tokens:
        if token == "|":
            stages.append([])
        else:
            stages[-1].append(token)
    if any(not stage for stage in stages):
        raise Refused("empty command or pipeline stage")
    if len(stages) > MAX_STAGES:
        raise Refused(f"more than {MAX_STAGES} pipeline stages")
    return [_stage(stage, cwd) for stage in stages]


def _expand(token: str) -> str:
    return os.path.expanduser("~") + token[1:] if token == "~" or token.startswith("~/") else token


def _stage(argv: list[str], cwd: str | None) -> list[str]:
    if argv[0] == "pipeline_status":
        if len(argv) != 2:
            raise Refused("pipeline_status takes exactly one work key")
        argv = ["dockwright", "pipeline", "status", argv[1], "--json"]
    head = argv[0]
    if head == "gh" and tuple(argv[1:3]) in GH_RULES:
        _vet(argv[3:], GH_RULES[tuple(argv[1:3])], cwd)
        return argv
    if head == "gh" and argv[1:2] == ["api"]:
        path = _vet(argv[2:], GH_API_RULE, cwd)
        if len(path) != 1 or not _API_PATH.match(path[0]):
            raise Refused("gh api takes exactly one path of [A-Za-z0-9_./-]")
        return argv
    if head == "git":
        return _git(argv, cwd)
    if head == "dockwright" and argv[1:3] == ["pipeline", "status"]:
        keys = _vet(argv[3:], STATUS_RULE, cwd)
        if len(keys) != 1 or not topic.is_key(keys[0]):
            raise Refused("dockwright pipeline status takes exactly one work key")
        return [sys.executable, "-P", "-m", "dockwright", *argv[1:]]
    if head in PLAIN_RULES:
        _vet(argv[1:], PLAIN_RULES[head], cwd)
        return argv
    raise Refused(f"{' '.join(argv[:3])} is not on the read-only list")


def _git(argv: list[str], cwd: str | None) -> list[str]:
    i = 1
    while i < len(argv) and argv[i] == "-C":
        i = _value(argv, i + 1, "-C")
    sub = argv[i] if i < len(argv) else ""
    if sub not in GIT_RULES:
        raise Refused(f"git {sub or '(no subcommand)'} is not on the read-only list")
    positionals = _vet(argv[i + 1:], GIT_RULES[sub], cwd)
    if sub == "ls-remote" and positionals and not _REMOTE_NAME.match(positionals[0]):
        raise Refused("git ls-remote takes a name of [A-Za-z0-9_.-], never a URL")
    return argv


def _vet(args: list[str], rule: Rule, cwd: str | None) -> list[str]:
    positionals: list[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        i += 1
        if token.startswith("--"):
            name, eq, value = token.partition("=")
            if name not in rule.bare and name not in rule.valued:
                raise Refused(f"flag {name} is not on the list for this command")
            if eq:
                _plain(value, name)
            elif name in rule.valued:
                i = _value(args, i, name)
        elif token.startswith("-"):
            if token in rule.valued:
                i = _value(args, i, token)
            elif not (token in rule.bare or (rule.digits and _DIGITS.match(token))
                      or (len(token) > 1 and all(f"-{c}" in rule.bare for c in token[1:]))):
                raise Refused(f"flag {token} is not on the list for this command")
        elif _under_dev(token, cwd):
            raise Refused(f"{token} is under /dev")
        else:
            positionals.append(token)
    return positionals


def _value(args: list[str], i: int, name: str) -> int:
    if i >= len(args):
        raise Refused(f"flag {name} needs a value")
    _plain(args[i], name)
    return i + 1


def _plain(value: str, name: str) -> None:
    if value.startswith("-"):
        raise Refused(f"the value of {name} starts with '-'")


def _under_dev(token: str, cwd: str | None) -> bool:
    joined = os.path.join(cwd or os.getcwd(), token)
    folded = (os.path.normpath(joined).lower(), os.path.realpath(joined).lower())
    return any(p == "/dev" or p.startswith("/dev/") for p in folded)


def _gh_owner(argv: list[str]) -> str | None:
    from . import gh_cli
    for i, token in enumerate(argv[1:], 1):
        if token in ("--repo", "-R") and i + 1 < len(argv):
            return argv[i + 1].split("/")[0] or None
        if token.startswith("--repo="):
            return token.partition("=")[2].split("/")[0] or None
    for token in argv[1:]:
        parts = token.lstrip("/").split("/")
        if parts[0] == "repos" and len(parts) > 2:
            return parts[1] or None
        owner = gh_cli.owner_of(token)
        if owner:
            return owner
    return None


def _gh_launch(argv: list[str], env: dict) -> tuple[str, dict]:
    if argv[0] != "gh" or not config.gh_configured():
        return _program(argv[0], env), env
    owner = _gh_owner(argv)
    if owner is None:
        return _program(argv[0], env), env
    from . import gh_cli
    gh_env = gh_cli.env_for_owner(owner)
    return _program(config.gh_binary(), env), {**env, "GH_TOKEN": gh_env["GH_TOKEN"]}


def execute(argvs: list[list[str]], cwd: str | None,
            timeout: float = SEGMENT_TIMEOUT_SEC) -> tuple[str, int | None, str]:
    deadline = time.monotonic() + timeout
    env = {**os.environ, **PAGER_ENV}
    env["PATH"] = os.pathsep.join([folder for folder in os.get_exec_path(env) if os.path.isabs(folder)]
                                  or [folder for folder in os.defpath.split(os.pathsep) if os.path.isabs(folder)])
    procs: list[subprocess.Popen] = []
    errs = []
    try:
        upstream = subprocess.DEVNULL
        for argv in argvs:
            errs.append(tempfile.TemporaryFile())
            program, launch_env = _gh_launch(argv, env)
            procs.append(subprocess.Popen(argv, executable=program, stdin=upstream,
                                          stdout=subprocess.PIPE, stderr=errs[-1], cwd=cwd, env=launch_env,
                                          start_new_session=True))
            if upstream is not subprocess.DEVNULL:
                upstream.close()
            upstream = procs[-1].stdout
        out, ended = _read_capped(procs[-1].stdout, deadline)
        if ended == "timeout":
            return "timeout", None, _output(out, errs)
        capped = {i for i, proc in enumerate(procs) if proc.poll() is None} if ended == "limit" else set()
        for i in capped:
            _kill(procs[i])
        codes = [_wait(proc, deadline) for proc in procs]
        if None in codes:
            return "timeout", None, _output(out, errs)
        bad = next((code for i, code in enumerate(codes)
                    if code != 0 and not _ended_by_executor(i, code, capped, len(codes))), 0)
        return ("failed" if bad else "ok"), bad, _output(out, errs)
    except OSError as err:
        return "error", None, str(err)[:RECORD_CHARS]
    finally:
        for proc in procs:
            if proc.poll() is None:
                _kill(proc)
            proc.stdout.close()
        for err in errs:
            err.close()


def _program(name: str, env: dict) -> str:
    if os.path.isabs(name):
        return name
    for folder in os.get_exec_path(env):
        path = os.path.join(folder, name)
        if os.path.isabs(folder) and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise FileNotFoundError(f"{name}: not found on an absolute PATH entry")


def _ended_by_executor(i: int, code: int, capped: set, stages: int) -> bool:
    return i in capped or (i < stages - 1 and code == -signal.SIGPIPE)


def _read_capped(stream, deadline: float) -> tuple[bytes, str]:
    chunks: list[bytes] = []
    size = 0
    fd = stream.fileno()
    while size < OUTPUT_LIMIT:
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            return b"".join(chunks), "timeout"
        chunk = os.read(fd, OUTPUT_LIMIT - size)
        if not chunk:
            return b"".join(chunks), "eof"
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks), "limit"


def _wait(proc: subprocess.Popen, deadline: float) -> int | None:
    try:
        return proc.wait(timeout=max(deadline - time.monotonic(), 0.01))
    except subprocess.TimeoutExpired:
        return None


def _kill(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=1)


def _output(out: bytes, errs) -> str:
    parts = [out]
    for err in errs:
        err.seek(0)
        parts.append(err.read(OUTPUT_LIMIT))
    return b"".join(parts).decode(errors="replace")[:RECORD_CHARS]


def _at(card: dict) -> float | None:
    record = card.get("last_check")
    at = record.get("at") if isinstance(record, dict) else None
    return at if isinstance(at, (int, float)) and not isinstance(at, bool) else None


def _is_due(card, now: float) -> bool:
    if not card or card.get("kind") != "note" or card.get("status") == "closed":
        return False
    at = _at(card)
    return at is None or now - at >= CHECK_EVERY_SEC


def due(now: float) -> list[str]:
    if not paths.ARTIFACTS.is_dir():
        return []
    found = []
    for d in paths.ARTIFACTS.iterdir():
        card = topic.read(d.name) if d.is_dir() else None
        if _is_due(card, now):
            found.append((_at(card) or 0, d.name))
    return [key for _, key in sorted(found)]


def run(now: float | None = None, budget_sec: float = BUDGET_SEC, runner=None, clock=time.monotonic) -> list[str]:
    now = time.time() if now is None else now
    runner = runner or execute
    started = clock()

    def spent() -> bool:
        return clock() - started >= budget_sec

    checked, failed = [], []
    for key in due(now):
        if spent():
            break
        try:
            if _check_note(key, now, runner, spent):
                checked.append(key)
        except Exception as err:
            failed.append(f"{key}: {type(err).__name__}: {err}")
    if failed:
        raise RuntimeError("; ".join(failed))
    return checked


def _check_note(key: str, now: float, runner, spent) -> bool:
    with open(paths.artifact_ticket_dir(key) / LOCK_NAME, "a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        card = topic.read(key)
        if not _is_due(card, now):
            return False
        worktree = (card.get("links") or {}).get("worktree")
        cwd = worktree if isinstance(worktree, str) and os.path.isdir(worktree) else None
        runs = [{"cmd": s, **_NOT_STARTED} if i and spent() else _run_segment(s, cwd, runner)
                for i, s in enumerate(commands_of(topic.artifact_body(key, "context", "ask")))]
        record = {"at": now, "runs": runs}
        topic.update(key, lambda current: {**current, "last_check": record} if current else None)
        return True


def _run_segment(segment: str, cwd: str | None, runner) -> dict:
    try:
        argvs = parse(segment, cwd)
        status, code, output = runner(argvs, cwd)
    except Refused as err:
        return {"cmd": segment, "status": "refused", "exit": None, "output": str(err)[:RECORD_CHARS]}
    except Exception as err:
        return {"cmd": segment, "status": "error", "exit": None,
                "output": f"{type(err).__name__}: {err}"[:RECORD_CHARS]}
    return {"cmd": segment, "status": status, "exit": code, "output": output[:RECORD_CHARS]}


def run_segment(segment: str, cwd: str | None) -> dict:
    return _run_segment(segment, cwd, execute)
