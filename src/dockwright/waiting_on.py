import os
import re
from dataclasses import dataclass

HUMAN_QUESTION = "human:question"
HUMAN_APPROVAL = "human:approval-dialog"
HUMAN_GATE = "human:gate"
EXTERNAL_TIME = "external:time"
EXTERNAL_EVENT = "external:event"
EXTERNAL_TRACKER = "external:jira"
EXTERNAL_LEASE = "external:lease"
EXTERNAL_MERGE = "external:merge"
NONE = "none"
TRACKER_WATCH_SCRIPT = "jira-watch-comments.py"
AUTHOR_WAIT_SCRIPT = "author-wait"
_AUTHOR_WAIT_FLAGS = ("--task-key", "--node")
_TRACKER_VALUE_OPTIONS = frozenset({"--poll"})
PAUSED_REASONS = frozenset({"paused", "manager-limited", "usage-cap", "held"})
_ORDER = (HUMAN_QUESTION, HUMAN_APPROVAL, HUMAN_GATE, "stalled", "worker", EXTERNAL_TIME, EXTERNAL_EVENT,
          EXTERNAL_TRACKER, EXTERNAL_LEASE, EXTERNAL_MERGE, NONE)
_SHELLS = frozenset({"bash", "sh", "zsh"})
_GATE_NON_WAITERS = frozenset({"sessions", "--help", "-h"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_ANCESTRY_LIMIT = 16


def worker(node: str) -> str:
    return f"worker:{node}"


def stalled(node: str) -> str:
    return f"stalled:{node}"


STALL_GRACE_SEC = 60


@dataclass(frozen=True)
class PositionFacts:
    node: str
    question: bool = False
    alive: bool = False
    approval_dialog: bool = False
    gate: bool = False
    limited: bool = False
    reported: bool = False
    lease_wait: bool = False
    merge_wait: bool = False
    tracker_watch: bool = False
    finished: bool = False
    gate_open: bool = False
    started_at: float | None = None
    event_subs: tuple[str, ...] = ()


def _fresh(now, since) -> bool:
    return now is not None and since is not None and now - since < STALL_GRACE_SEC


def token_value(facts: PositionFacts, now: float | None = None) -> str:
    if facts.question:
        return HUMAN_QUESTION
    if facts.alive and facts.approval_dialog:
        return HUMAN_APPROVAL
    if facts.alive and facts.gate:
        return HUMAN_GATE
    if facts.gate_open:
        return HUMAN_GATE
    if facts.alive and facts.limited and not facts.reported:
        return EXTERNAL_TIME
    if facts.alive and not facts.reported:
        return worker(facts.node)
    if facts.event_subs:
        return f"external:{facts.event_subs[0]}"
    if facts.lease_wait:
        return EXTERNAL_LEASE
    if facts.merge_wait:
        return EXTERNAL_MERGE
    if facts.tracker_watch:
        return EXTERNAL_TRACKER
    if facts.finished:
        return NONE
    if _fresh(now, facts.started_at):
        return worker(facts.node)
    return stalled(facts.node)


def request_value(request: dict, now: float | None = None) -> str:
    if (request.get("pending") or {}).get("reason") in PAUSED_REASONS:
        return EXTERNAL_TIME
    to = (request.get("edge") or {}).get("to") or "?"
    if _fresh(now, request.get("ts") or 0):
        return worker(to)
    return stalled(to)


def rank(value: str) -> int:
    head = value.split(":", 1)[0] if value.startswith(("stalled:", "worker:")) else value
    if head in _ORDER:
        return _ORDER.index(head)
    return _ORDER.index(EXTERNAL_EVENT) if head.startswith("external:") else len(_ORDER)


def work_value(values, any_question: bool) -> str:
    if any_question:
        return HUMAN_QUESTION
    return min(values, key=rank, default=NONE)


def evaluate(fold_, value_of, any_question: bool, now: float | None = None,
             started_at_of=None) -> tuple[str, dict, dict]:
    if fold_.closed is not None:
        return work_value([], any_question), {}, {}
    requested = {r.get("token_id") for r in fold_.open_requests}
    tokens = {tid: None if t.forked or tid in requested else value_of(t) for tid, t in fold_.tokens.items()}
    requests = {r.get("request_id"): request_value(r, now) for r in fold_.open_requests}
    values = [v for v in tokens.values() if v is not None] + list(requests.values())
    if not values and fold_.tokens:
        tok = next(iter(fold_.tokens.values()))
        started = started_at_of(tok) if started_at_of else None
        values = [worker(tok.node) if _fresh(now, started) else stalled(tok.node)]
    return work_value(values, any_question), tokens, requests


def _argv(command) -> list[str]:
    return command.split() if isinstance(command, str) else []


def is_gate_launch(command: str) -> bool:
    tokens = _argv(command)
    while tokens and _ASSIGNMENT.match(tokens[0]):
        tokens.pop(0)
    if tokens and os.path.basename(tokens[0]) in _SHELLS:
        tokens.pop(0)
    return (bool(tokens) and os.path.basename(tokens[0]) == "plannotator-gate"
            and not _GATE_NON_WAITERS & set(tokens))


def _script_args(tokens: list[str], script: str) -> list[str] | None:
    if tokens and os.path.basename(tokens[0]) == script:
        return tokens[1:]
    if (len(tokens) > 1 and os.path.basename(tokens[0]).lower().startswith("python")
            and os.path.basename(tokens[1]) == script):
        return tokens[2:]
    return None


def tracker_watch_key(command: str) -> str | None:
    rest = _script_args(_argv(command), TRACKER_WATCH_SCRIPT)
    if rest is None:
        return None
    for i, t in enumerate(rest):
        if not t.startswith("-") and (i == 0 or rest[i - 1] not in _TRACKER_VALUE_OPTIONS):
            return t
    return None


def author_wait_key(command: str) -> tuple[str, str] | None:
    rest = _script_args(_argv(command), AUTHOR_WAIT_SCRIPT)
    if rest is None:
        return None
    values: dict[str, str] = {}
    for i, t in enumerate(rest):
        flag, eq, value = t.partition("=")
        if flag not in _AUTHOR_WAIT_FLAGS:
            continue
        if eq:
            values[flag] = value
        elif i + 1 < len(rest):
            values[flag] = rest[i + 1]
    task_key, node = values.get("--task-key"), values.get("--node")
    return (task_key, node) if task_key and node else None


def process_waits(index) -> tuple[set[str], set[tuple[str, str]]]:
    commands = (index or {}).get("command_by_pid") or {}
    parents = (index or {}).get("parent_by_pid") or {}
    authors = {pid: pair for pid, pair in ((pid, author_wait_key(c)) for pid, c in commands.items()) if pair}
    trackers = {key for pid, key in ((pid, tracker_watch_key(c)) for pid, c in commands.items())
                if key and not any(descends_from(pid, author, parents) for author in authors)}
    return trackers, set(authors.values())


def descends_from(pid: int, ancestor: int, parent_by_pid: dict) -> bool:
    seen = {pid}
    current = pid
    for _ in range(_ANCESTRY_LIMIT):
        parent = parent_by_pid.get(current)
        if parent is None or parent in seen:
            return False
        if parent == ancestor:
            return True
        seen.add(parent)
        current = parent
    return False
