from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import stat
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config, paths, pipeline, state

SOURCES = ("tracker", "deploy", "time", "chat")
KINDS = {"tracker": ("comment", "status", "sprint-assigned"), "deploy": ("job",), "time": ("deadline",),
         "chat": ("dm", "channel", "dm-thread", "channel-thread")}
ONE_SHOT = {("deploy", "job"), ("time", "deadline")}
CHAT_MATCH_KEYS = ("app", "conversation", "sender", "channel", "thread")
DEPLOY_ENVS = ("staging", "dev", "prod")
EVENT_MARK = "event: "
RETENTION_SEC = 14 * 86400
SUB_ID_BYTES = 4
TRACKER_KEYS = ("TRACKER_URL", "TRACKER_EMAIL", "TRACKER_API_TOKEN")


class PollError(Exception):
    pass


@dataclass
class Event:
    id: str
    ts: float
    source: str
    kind: str
    match: dict[str, str]
    cursor_key: str
    summary: str
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(str(d["id"]), float(d["ts"]), str(d["source"]), str(d["kind"]), dict(d.get("match") or {}),
                   str(d.get("cursor_key") or ""), str(d.get("summary") or ""), dict(d.get("payload") or {}))


@dataclass
class Sub:
    sub_id: str
    task_key: str
    node: str
    source: str
    match: dict[str, str]
    kinds: list[str] | None
    after: str | None
    deadline: float | None
    handler: list[str] | None
    skip_own: str | None
    since: float
    delivered: list | None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Sub":
        return cls(str(d["sub_id"]), str(d["task_key"]), str(d["node"]), str(d["source"]), dict(d.get("match") or {}),
                   list(d["kinds"]) if d.get("kinds") is not None else None,
                   str(d["after"]) if d.get("after") is not None else None,
                   float(d["deadline"]) if d.get("deadline") is not None else None,
                   list(d["handler"]) if d.get("handler") is not None else None,
                   str(d["skip_own"]) if d.get("skip_own") is not None else None,
                   float(d.get("since") or 0), list(d["delivered"]) if d.get("delivered") is not None else None)


def _lock():
    return state.file_lock(paths.EVENTS / ".lock")


def load_env(path: Path) -> tuple[dict[str, str] | None, str | None]:
    try:
        st = path.stat()
    except OSError:
        return None, "missing"
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
        return None, "mode"
    env: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None, "missing"
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() in TRACKER_KEYS:
            env[key.strip()] = value.strip()
    missing = [k for k in TRACKER_KEYS if not env.get(k)]
    if missing:
        return None, f"incomplete (no {', '.join(missing)})"
    return env, None


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _seen_path(source: str) -> Path:
    return paths.EVENTS_SEEN / f"{source}.json"


def _sub_path(sub_id: str) -> Path:
    return paths.EVENTS_SUBS / f"{sub_id}.json"


def retention_sec() -> int:
    return config.events_retention_days() * 86400


def append(event: Event) -> bool:
    with _lock():
        seen = state.read_json(_seen_path(event.source)) or {}
        ids = seen.get("ids") if isinstance(seen.get("ids"), dict) else {}
        if event.id in ids:
            return False
        state.append_event(paths.EVENTS_LOG / f"{_day(event.ts)}.jsonl", event.to_dict())
        recorded = time.time()
        floor = recorded - retention_sec()
        ids = {k: v for k, v in ids.items() if isinstance(v, (int, float)) and v >= floor}
        ids[event.id] = recorded
        state.write_json_atomic(_seen_path(event.source), {"ids": ids})
        return True


def read_since(source: str, after_ts: float) -> list[Event]:
    out: list[Event] = []
    if not paths.EVENTS_LOG.is_dir():
        return out
    first = _day(after_ts)
    for day_file in sorted(paths.EVENTS_LOG.glob("*.jsonl")):
        if day_file.stem < first:
            continue
        try:
            lines = day_file.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                d = json.loads(line)
                event = Event.from_dict(d)
            except (ValueError, KeyError, TypeError):
                continue
            if event.source == source and event.ts >= after_ts:
                out.append(event)
    return out


def recent(source: str, now: float) -> list[Event]:
    return read_since(source, now - retention_sec())


def prune(now: float) -> int:
    cut = _day(now - retention_sec())
    removed = 0
    with _lock():
        for day_file in paths.EVENTS_LOG.glob("*.jsonl") if paths.EVENTS_LOG.is_dir() else []:
            if day_file.stem >= cut:
                continue
            try:
                day_file.unlink()
            except OSError:
                continue
            removed += 1
    return removed


def _journal(task_key: str, event: dict) -> None:
    from . import mcp_server as ms
    ms._append_task_event(task_key, event)


def _read_subs() -> list[Sub]:
    out = []
    for p in sorted(paths.EVENTS_SUBS.glob("*.json")) if paths.EVENTS_SUBS.is_dir() else []:
        d = state.read_json(p)
        if not isinstance(d, dict):
            continue
        try:
            out.append(Sub.from_dict(d))
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(out, key=lambda s: (s.since, s.sub_id))


def subs_for(task_key: str | None = None, source: str | None = None, node: str | None = None) -> list[Sub]:
    return [s for s in _read_subs()
            if (task_key is None or s.task_key == task_key) and (source is None or s.source == source)
            and (node is None or s.node == node)]


def _holder_check(task_key: str, node: str) -> None:
    from . import mcp_server as ms
    try:
        fold_ = ms._fold_for(task_key)
    except (OSError, KeyError, ValueError, TypeError):
        fold_ = None
    token = fold_.tokens.get(pipeline.DEFAULT_TOKEN_ID) if fold_ is not None else None
    if token is None or token.node != node:
        raise ValueError(f"node {node} does not hold token {pipeline.DEFAULT_TOKEN_ID} of {task_key}")
    if token.forked:
        raise ValueError(f"token {pipeline.DEFAULT_TOKEN_ID} of {task_key} is a fork parent: a fork row subscribes to nothing")


def _check_match(source: str, match: dict[str, str]) -> None:
    if source == "tracker" and not match.get("issue"):
        raise ValueError("a tracker subscription needs --match issue=<KEY>")
    if source == "deploy":
        if not match.get("repo"):
            raise ValueError("a deploy subscription needs --match repo=<owner>/<repo>")
        env = match.get("env")
        if not env:
            raise ValueError("a deploy subscription needs --match env=staging|dev|prod")
        if env not in DEPLOY_ENVS:
            raise ValueError(f"a deploy subscription on env {env} is refused: only {', '.join(DEPLOY_ENVS)}")
        if not (match.get("sha") or match.get("run_id")):
            raise ValueError("a deploy subscription needs --match sha=<sha40> or --match run_id=<id>")
        if env == "prod" and (match.get("run_id") or not match.get("sha")):
            raise ValueError("a prod subscription needs sha and no run_id: Deployments are read by commit")
    if source == "chat" and any(k not in CHAT_MATCH_KEYS for k in match):
        raise ValueError("a chat subscription matches only app, conversation, sender, channel and thread")


def _handler_allowed(argv: list[str]) -> bool:
    for entry in config.events_handlers():
        words = str(entry).split()
        if words and argv[:len(words)] == words:
            return True
    return False


def handler_ok(argv: list[str]) -> str | None:
    if not argv:
        return None
    name = argv[0]
    if "/" in name or not _handler_allowed(argv):
        return None
    for directory in config.events_handler_path():
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def subscribe(task_key: str, node: str, source: str, match: dict[str, str], *, kinds: list[str] | None = None,
              after: str | None = None, deadline: float | None = None, handler: list[str] | None = None,
              skip_own: str | None = None) -> Sub:
    if not config.events_enabled():
        raise ValueError("events layer is off: set [events] enabled = true")
    if source not in SOURCES:
        raise ValueError(f"unknown source {source}: one of {', '.join(SOURCES)}")
    if kinds is not None:
        bad = [k for k in kinds if k not in KINDS[source]]
        if bad:
            raise ValueError(f"unknown kind {', '.join(bad)} for {source}: one of {', '.join(KINDS[source])}")
    match = {str(k): str(v) for k, v in (match or {}).items()}
    _check_match(source, match)
    if source == "tracker" and not after:
        raise ValueError("a tracker subscription needs --after <comment id>: the cursor it starts from")
    if source == "chat" and after:
        raise ValueError("a chat subscription takes no after: it wakes on messages sent from the subscription on")
    if source == "chat" and not config.events_source("chat")["apps"]:
        raise ValueError("a chat subscription needs [events.sources.chat] apps: the bundle ids of the chat app")
    if source == "time" and deadline is None:
        raise ValueError("a time subscription needs --deadline <unix ts>: nothing else fires it")
    if source == "tracker":
        env_file = config.events_env_file()
        env, reason = load_env(env_file)
        if env is None:
            raise ValueError(f"tracker source cannot poll: env_file {env_file} is {reason}; create it with mode 0600 "
                             "and TRACKER_URL, TRACKER_EMAIL, TRACKER_API_TOKEN")
    if handler is not None and handler_ok(handler) is None:
        raise ValueError(f"handler {' '.join(handler)!r} does not start with an entry of [events] handlers found in "
                         "handler_path (a path is refused)")
    _holder_check(task_key, node)
    since = time.time()
    if source == "chat":
        after = chat_time(since)
    sub = Sub(secrets.token_hex(SUB_ID_BYTES), task_key, node, source, match, list(kinds) if kinds else None,
              str(after) if after else None, float(deadline) if deadline is not None else None,
              list(handler) if handler else None, skip_own or None, since, None)
    replaced: list[Sub] = []
    with _lock():
        for old in _read_subs():
            if (old.task_key, old.node, old.source, old.match) == (task_key, node, source, match):
                _sub_path(old.sub_id).unlink(missing_ok=True)
                replaced.append(old)
        if source == "chat" and replaced:
            sub.since, sub.after, sub.delivered = replaced[0].since, replaced[0].after, replaced[0].delivered
        state.write_json_atomic(_sub_path(sub.sub_id), sub.to_dict())
    for old in replaced:
        _journal(task_key, {"type": "unsubscribed", "sub_id": old.sub_id, "node": old.node, "reason": "replaced"})
    _journal(task_key, {"type": "subscribed", "sub_id": sub.sub_id, "node": node, "source": source,
                        "match": json.dumps(match, sort_keys=True), "kinds": ",".join(kinds or []),
                        "after": sub.after or "", "deadline": sub.deadline, "handler": " ".join(handler or [])})
    return sub


def _remove(selected: list[Sub], reason: str) -> int:
    for s in selected:
        _sub_path(s.sub_id).unlink(missing_ok=True)
    return len(selected)


def unsubscribe(sub_id: str | None = None, *, task_key: str | None = None, node: str | None = None,
                reason: str = "") -> int:
    with _lock():
        selected = [s for s in _read_subs()
                    if (sub_id is None or s.sub_id == sub_id) and (task_key is None or s.task_key == task_key)
                    and (node is None or s.node == node)]
        n = _remove(selected, reason)
    for s in selected:
        _journal(s.task_key, {"type": "unsubscribed", "sub_id": s.sub_id, "node": s.node, "reason": reason})
    return n


def mark_delivered(sub_ids: list[str], event: Event) -> None:
    with _lock():
        for sub_id in sub_ids:
            d = state.read_json(_sub_path(sub_id))
            if isinstance(d, dict):
                state.write_json_atomic(_sub_path(sub_id), {**d, "delivered": [event.ts, event.id]})


def drop_left(task_key: str, fold_) -> list[str]:
    token = fold_.tokens.get(pipeline.DEFAULT_TOKEN_ID) if fold_ is not None else None
    with _lock():
        gone = [s for s in _read_subs() if s.task_key == task_key and (token is None or s.node != token.node)]
        _remove(gone, "left")
    for s in gone:
        _journal(task_key, {"type": "unsubscribed", "sub_id": s.sub_id, "node": s.node, "reason": "left"})
    return [s.sub_id for s in gone]


def _key_after(cursor_key: str, after: str) -> bool:
    if cursor_key.isdigit() and after.isdigit():
        return int(cursor_key) > int(after)
    return cursor_key > after


def chat_time(ts: float) -> str:
    return f"{ts:017.6f}"


def matches(sub: Sub, event: Event) -> bool:
    if sub.delivered is not None and (event.ts, event.id) <= (float(sub.delivered[0]), str(sub.delivered[1])):
        return False
    if "sub_id" in event.match:
        return event.match["sub_id"] == sub.sub_id
    if event.source != sub.source:
        return False
    if any(event.match.get(k) != v for k, v in sub.match.items()):
        return False
    if sub.kinds is not None and event.kind not in sub.kinds:
        return False
    if sub.after and event.cursor_key:
        if not _key_after(event.cursor_key, sub.after):
            return False
    elif event.ts < sub.since:
        return False
    if sub.skip_own and event.payload.get("author_id") == sub.skip_own:
        return False
    return True


def line(event: Event, sub: Sub) -> str:
    return f"{EVENT_MARK}{event.source}.{event.kind} — {event.summary} (sub {sub.sub_id})"


def _parse_match(pairs: list[str]) -> dict[str, str]:
    out = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"--match takes key=value, got {pair!r}")
        out[key] = value
    return out


def _print(obj) -> None:
    print(json.dumps(obj, sort_keys=True))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dockwright event")
    subs = parser.add_subparsers(dest="cmd", required=True)
    s = subs.add_parser("subscribe")
    s.add_argument("--task-key", required=True)
    s.add_argument("--node", required=True)
    s.add_argument("--source", required=True)
    s.add_argument("--match", action="append", default=[])
    s.add_argument("--kinds")
    s.add_argument("--after")
    s.add_argument("--deadline", type=float)
    s.add_argument("--skip-own")
    s.add_argument("--handler")
    u = subs.add_parser("unsubscribe")
    u.add_argument("sub_id", nargs="?")
    u.add_argument("--task-key")
    u.add_argument("--node")
    ls = subs.add_parser("list")
    ls.add_argument("task_key", nargs="?")
    em = subs.add_parser("emit")
    em.add_argument("--file", required=True)
    g = subs.add_parser("gate")
    g.add_argument("rule")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "subscribe":
            sub = subscribe(args.task_key, args.node, args.source, _parse_match(args.match),
                            kinds=[k for k in args.kinds.split(",") if k] if args.kinds else None,
                            after=args.after, deadline=args.deadline,
                            handler=shlex.split(args.handler) if args.handler else None, skip_own=args.skip_own)
            _print({"ok": True, "sub_id": sub.sub_id, "source": sub.source, "after": sub.after,
                    "deadline": sub.deadline})
        elif args.cmd == "unsubscribe":
            if not args.sub_id and not (args.task_key and args.node):
                raise ValueError("unsubscribe takes <sub_id> or --task-key K --node N")
            _print({"ok": True, "removed": unsubscribe(args.sub_id or None, task_key=args.task_key, node=args.node,
                                                       reason="unsubscribed")})
        elif args.cmd == "list":
            for sub in subs_for(args.task_key):
                _print(sub.to_dict())
        elif args.cmd == "emit":
            with open(args.file, encoding="utf-8") as fh:
                _print({"ok": True, "appended": append(Event.from_dict(json.load(fh)))})
        elif args.cmd == "gate":
            from . import event_rules
            rule = config.events_rules().get(args.rule)
            if rule is None:
                raise ValueError(f"unknown rule {args.rule}")
            now = time.time()
            pool = recent("exec", now) if rule["while"] else None
            reason = event_rules.rule_off(rule, now, pool)
            if reason is not None:
                _print({"ok": True, "open": False, "reason": reason, "rule": args.rule})
                return 1
            start = event_rules.rule_window_start(rule, now, pool) if rule["while"] else None
            _print({"ok": True, "open": True, "rule": args.rule, **({"start": start} if start is not None else {})})
    except (ValueError, OSError, KeyError, TypeError) as err:
        _print({"ok": False, "reason": str(err)})
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
