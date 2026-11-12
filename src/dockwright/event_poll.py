from __future__ import annotations

import fcntl
import sys
import threading
import time
from pathlib import Path

from . import config, event_rules, event_sources, events, failure_streak, lane_lines, paths, state

SOURCE_TIMEOUT_SEC = 30
SOURCE_LOCK = ".poll.lock"
RULES_LOCK = ".rules.lock"
LAST_FILE = ".last"
CURSOR_FILE = "cursor.json"
STREAK_FILE = "failures.json"
PAGE_EVERY_SEC = 3600
PRUNE_EVERY_SEC = 3600
ERROR_CHARS = 200
_PAGED = {"events_poll_failed": ("EVENT_POLL_FAILED", "source"), "events_env_insecure": ("EVENT_ENV_INSECURE", None),
          "event_rule_failed": ("EVENT_RULE_FAILED", "rule")}

_sources = {name: event_sources.module(name) for name in event_sources.NAMES}


load_env = events.load_env


def _last_path(name: str) -> Path:
    return paths.EVENTS_CURSORS / name / LAST_FILE


def _cursor_path(name: str) -> Path:
    return paths.EVENTS_CURSORS / name / CURSOR_FILE


def _streak_path(name: str) -> Path:
    return paths.EVENTS_CURSORS / name / STREAK_FILE


def _due(name: str, now: float) -> bool:
    last = state.read_json(_last_path(name))
    ts = last.get("ts") if isinstance(last, dict) else None
    return not isinstance(ts, (int, float)) or now - ts >= config.events_source(name)["every_sec"]


def _prune_due(now: float) -> bool:
    last = state.read_json(_last_path("prune"))
    ts = last.get("ts") if isinstance(last, dict) else None
    return not isinstance(ts, (int, float)) or now - ts >= PRUNE_EVERY_SEC


def _prune(now: float) -> list[dict]:
    if not _prune_due(now):
        return []
    removed = events.prune(now)
    state.write_json_atomic(_last_path("prune"), {"ts": now})
    return [{"type": "events_pruned", "files": removed}] if removed > 0 else []


def _wanted(name: str, live: list[events.Sub], rules: dict[str, dict]) -> bool:
    if name == "time":
        return any(s.deadline is not None for s in live)
    if name == "chat" and config.chat_archive()["enabled"]:
        return True
    if any(s.source == name for s in live):
        return True
    return any(r.get("source") == name and r.get("enabled") and not r.get("errors") for r in rules.values())


def _failure(name: str, err: Exception) -> dict:
    text = str(err) if isinstance(err, events.PollError) else f"{type(err).__name__}: {err}"
    return {"type": "events_poll_failed", "source": name, "error": text[:ERROR_CHARS]}


def _count(entry: dict, streaks: dict[str, dict], keys: list[str], slept: bool) -> None:
    counted = [streaks[k] for k in keys if k in streaks]
    entry["streak"] = max((e["n"] for e in counted), default=0)
    if slept or not any(failure_streak.due(e) for e in counted):
        entry["held"] = True
    if slept:
        entry["slept"] = True


def _saved(name: str, streaks: dict[str, dict], before: dict[str, dict]) -> bool:
    if streaks == before:
        return True
    try:
        failure_streak.save(_streak_path(name), streaks)
    except OSError as err:
        print(f"events: {name} failure streaks not saved ({type(err).__name__}: {err})", file=sys.stderr)
        return False
    return True


def _deliver(record, entry: dict) -> None:
    if record is None:
        return
    try:
        record(entry)
    except Exception as err:
        print(f"events: record writer failed ({type(err).__name__}: {err})", file=sys.stderr)


class _Lock:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(path, "a+")

    def __enter__(self) -> bool:
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()
        return False


def _poll_source(name: str, now: float, live: list[events.Sub], rules: dict[str, dict], record=None) -> list[dict]:
    out: list[dict] = []

    def put(entry: dict) -> None:
        out.append(entry)
        _deliver(record, entry)

    with _Lock(paths.EVENTS_CURSORS / name / SOURCE_LOCK) as held:
        if not held:
            put({"type": "events_poll_busy", "source": name})
            return out
        if not _due(name, now):
            return out
        env: dict[str, str] = {}
        if name == "tracker":
            loaded, reason = load_env(config.events_env_file())
            if loaded is None:
                put({"type": "events_env_insecure", "path": str(config.events_env_file()), "reason": reason})
                return out
            env = loaded
        subs = live if name == "time" else [s for s in live if s.source == name]
        cursor = state.read_json(_cursor_path(name))
        cursor = cursor if isinstance(cursor, dict) else {}
        source = _sources[name]
        streaks = failure_streak.load(_streak_path(name))
        before = {key: dict(entry) for key, entry in streaks.items()}
        begun = failure_streak.mark()
        try:
            new_events, new_cursor = source.poll(subs, rules, dict(cursor), env, now,
                                                until=time.time() + SOURCE_TIMEOUT_SEC)
        except Exception as err:
            slept = failure_streak.slept(begun)
            failure = _failure(name, err)
            if not slept:
                failure_streak.fail(streaks, "*", now)
            _count(failure, streaks, ["*"], slept)
            if not _saved(name, streaks, before):
                failure.pop("held", None)
            put(failure)
            state.write_json_atomic(_last_path(name), {"ts": now})
            return out
        slept = failure_streak.slept(begun)
        notes = new_cursor.pop(event_sources.NOTES, None)
        attempted = new_cursor.pop(event_sources.ATTEMPTED, None)
        attempted = attempted if isinstance(attempted, list) else None
        failure_streak.clear(streaks, "*")
        polled = {"type": "events_polled", "source": name, "n": len(new_events)}
        spent = new_cursor.get(event_sources.BUDGET_SPENT) or []
        if spent:
            polled["partial"] = list(spent)
        failed = [f for f in new_cursor.get(event_sources.FAILED) or [] if isinstance(f, list) and len(f) == 2]
        retried = getattr(source, "FAILED_RETRIED", True)
        failure = None
        if failed:
            text = "; ".join(f"{selector}: {error}" for selector, error in failed)
            failure = {"type": "events_poll_failed", "source": name, "error": text[:ERROR_CHARS]}
            if not retried:
                failure["skipped"] = True
        if retried:
            keys = list(dict.fromkeys(str(selector) for selector, _ in failed))
            for key in list(streaks):
                if key not in keys and key not in spent and (attempted is None or key in attempted):
                    failure_streak.clear(streaks, key)
            if not slept:
                for key in keys:
                    failure_streak.fail(streaks, key, now)
            if failure is not None:
                _count(failure, streaks, keys, slept)
        if not _saved(name, streaks, before) and failure is not None:
            failure.pop("held", None)
        put(polled)
        if failure is not None:
            put(failure)
        for note in notes if isinstance(notes, list) else []:
            if isinstance(note, dict) and note.get("type"):
                put(note)
        for event in sorted(new_events, key=lambda e: (e.ts, e.id)):
            events.append(event)
        state.write_json_atomic(_cursor_path(name), new_cursor)
        state.write_json_atomic(_last_path(name), {"ts": now})
    return out


def _poll_all(now: float, record=None) -> list[dict]:
    if not config.events_enabled():
        return []
    live = events.subs_for()
    rules = config.events_rules()
    names = [n for n in event_sources.NAMES if _due(n, now) and _wanted(n, live, rules)]
    results: dict[str, list[dict]] = {}

    def target(name: str) -> None:
        got: list[dict] = []

        def put(entry: dict) -> None:
            got.append(entry)
            _deliver(record, entry)

        try:
            _poll_source(name, now, live, rules, put)
        except Exception as err:
            put(_failure(name, err))
        finally:
            results[name] = got

    threads = [threading.Thread(target=target, args=(name,), name=f"events-{name}") for name in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return [entry for name in names for entry in results.get(name, [])]


def run(now: float, record=None) -> list[dict]:
    return _poll_all(now, record)


def _rules(now: float) -> list[dict]:
    with _Lock(paths.EVENTS / RULES_LOCK) as held:
        if not held:
            return [{"type": "events_rules_busy"}]
        return event_rules.run(now)


def _page(record: dict, now: float, emit) -> None:
    from .pipeline_reconcile import _Pages
    kind = record.get("type")
    if kind == "events_poll_failed" and record.get("held"):
        return
    if kind in _PAGED:
        lane_kind, key_field = _PAGED[kind]
        key = f"{kind}:{record.get(key_field) if key_field else 'env'}" + (":skipped" if record.get("skipped") else "")
        if _Pages("events").throttled(("events", key), PAGE_EVERY_SEC, now):
            return
        fields = {k: v for k, v in record.items() if k != "type"}
        emit(lane_lines.render(lane_kind, **fields), None)
    elif kind == "event_started":
        emit(lane_lines.render("EVENT_STARTED", task_key=record.get("task_key"), rule=record.get("rule"),
                               event_id=record.get("event_id")), record.get("manager_name"))
    elif kind == "event_stalled":
        emit(lane_lines.render("EVENT_STALLED", rule=record.get("rule"), domain=record.get("domain"),
                               since=record.get("since")), None)
    elif kind == "event_page":
        emit(lane_lines.render("EVENT_PAGE", rule=record.get("rule"), line=record.get("line"),
                               event_id=record.get("event_id")), record.get("manager_name"))
    elif kind == "event_queue_page":
        emit(lane_lines.render("EVENT_QUEUE", rule=record.get("rule"), count=record.get("count"),
                               line=record.get("line"), event_id=record.get("event_id")), record.get("manager_name"))


def step(now: float, managers: list[dict], emit, record=None) -> list[dict]:
    records = _poll_all(now, record)
    for part in (_prune, _rules):
        got = part(now)
        for entry in got:
            _deliver(record, entry)
        records.extend(got)
    for entry in records:
        _page(entry, now, emit)
    return records
