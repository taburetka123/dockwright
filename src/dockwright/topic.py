import json
import re
import time

from . import paths, pipeline, state

PRIORITIES = ("P0", "P1", "P2", "P3")
DEFAULT_PRIORITY = "P2"
TOKENLESS_KINDS = ("note", "lane", "fleet")
STATUSES = ("open", "closed")
LINK_KEYS = ("pr", "ticket", "worktree")
TITLE_MAX = 120
LOCK_NAME = ".topic.lock"
DECISIONS_PHASE = "decisions"
_TICKET_KEY = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")


def is_key(value) -> bool:
    try:
        return isinstance(value, str) and paths._safe_segment(value) == value
    except ValueError:
        return False


def title_of(text) -> str:
    for line in (text or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:TITLE_MAX]
    return ""


def ticket_link(task_key) -> str | None:
    return task_key if _TICKET_KEY.match(task_key or "") else None


def fleet_key(domain) -> str:
    return f"fleet-{domain}"


def _one_line(text) -> str:
    return " ".join(str(text or "").split())[:TITLE_MAX].rstrip()


def new_card(kind, domain, title, *, worktree=None, ticket=None, status=None, created_at=None) -> dict:
    card = {"kind": kind, "domain": domain, "title": _one_line(title), "priority": DEFAULT_PRIORITY,
            "blocked_by": [], "links": {"pr": None, "ticket": ticket, "worktree": worktree},
            "created_at": time.time() if created_at is None else created_at}
    if kind in TOKENLESS_KINDS:
        status = "open" if status is None else status
        if status not in STATUSES:
            raise ValueError(f"topic: status must be one of {', '.join(STATUSES)}, got {status!r}")
        card["status"] = status
    return card


def read(task_key) -> dict | None:
    card = state.read_json(paths.topic_path(task_key))
    return card if isinstance(card, dict) else None


def update(task_key, fn) -> dict | None:
    path = paths.topic_path(task_key)
    with state.file_lock(path.parent / LOCK_NAME):
        current = read(task_key)
        new = fn(current)
        if new is None or new == current:
            return current
        state.write_json_atomic(path, new)
        return new


def load(task_key) -> dict | None:
    card = read(task_key)
    if card is not None:
        return {**card, "stored": True}
    tid = pipeline.DEFAULT_TOKEN_ID
    events = _events(task_key)
    started = next((e for e in events if e.get("type") == "pipeline_started" and (e.get("token_id") or tid) == tid),
                   None)
    if started is None or not (paths.artifact_ticket_dir(task_key) / "schema.toml").is_file():
        return None
    built = new_card(pipeline.running_schema(events, tid, ""), _manager_domain(started.get("manager")),
                     title_of(artifact_body(task_key, "context", "ask")), worktree=_token_cwd(events) or None,
                     ticket=ticket_link(task_key), created_at=started.get("ts"))
    return {**built, "stored": False}


def follow(card, *, kind=None, worktree=None) -> dict | None:
    if card is None:
        return None
    new = dict(card)
    if kind is not None:
        new["kind"] = kind
        if kind not in TOKENLESS_KINDS:
            new.pop("status", None)
    if worktree is not None:
        links = card.get("links")
        new["links"] = {**(links if isinstance(links, dict) else {}), "worktree": worktree}
    return new


def edit(card, *, priority=None, title=None, domain=None, status=None, blocked_by=None, links=None):
    base = {k: v for k, v in card.items() if k != "stored"}
    base["links"] = {**{k: None for k in LINK_KEYS}, **(base.get("links") or {})}
    new = {**base, "links": dict(base["links"])}
    if priority is not None:
        if priority not in PRIORITIES:
            raise ValueError(f"topic_set: priority must be one of {', '.join(PRIORITIES)}, got {priority!r}")
        new["priority"] = priority
    if title is not None:
        if not title.strip():
            raise ValueError("topic_set: title must not be blank")
        new["title"] = _one_line(title)
    if domain is not None:
        if not is_key(domain):
            raise ValueError(f"topic_set: domain {domain!r} is not a slug; use only [A-Za-z0-9_-]")
        new["domain"] = domain
    if status is not None:
        if new.get("kind") not in TOKENLESS_KINDS:
            raise ValueError(f"topic_set: status belongs to {', '.join(TOKENLESS_KINDS)} topics; a work's state "
                             "comes from its token")
        if status not in STATUSES:
            raise ValueError(f"topic_set: status must be one of {', '.join(STATUSES)}, got {status!r}")
        new["status"] = status
    if blocked_by is not None:
        if not isinstance(blocked_by, list) or not all(is_key(k) for k in blocked_by):
            raise ValueError(f"topic_set: blocked_by must be a list of topic keys ([A-Za-z0-9_-]), got {blocked_by!r}")
        new["blocked_by"] = list(dict.fromkeys(blocked_by))
    if links is not None:
        unknown = sorted(set(links) - set(LINK_KEYS))
        if unknown:
            raise ValueError(f"topic_set: unknown link(s) {', '.join(unknown)}; use {', '.join(LINK_KEYS)}")
        if "worktree" in links and new.get("kind") not in TOKENLESS_KINDS:
            raise ValueError("topic_set: a work's worktree comes from its token; move it with pipeline_advance(cwd=) "
                             "or pipeline_rework(cwd=)")
        for key, value in links.items():
            if value is not None and not isinstance(value, str):
                raise ValueError(f"topic_set: link {key} must be a string or null")
            new["links"][key] = value or None
    changed = sorted(k for k in set(new) | set(base) if new.get(k) != base.get(k))
    return new, changed


def shown_priority(priority, waiting_on) -> str:
    if priority in PRIORITIES and str(waiting_on or "").startswith("human:"):
        return PRIORITIES[max(PRIORITIES.index(priority) - 1, 0)]
    return priority


def keeps(task_key) -> bool:
    card = read(task_key)
    return card is not None and card.get("kind") in TOKENLESS_KINDS and card.get("status") != "closed"


def artifact_body(task_key, phase, name) -> str:
    try:
        return state.parse_artifact(paths.artifact_path(task_key, phase, name).read_text())[1]
    except (OSError, ValueError):
        return ""


def _events(task_key) -> list[dict]:
    try:
        lines = paths.artifact_events_path(task_key).read_text().splitlines()
    except (OSError, ValueError):
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _token_cwd(events) -> str | None:
    try:
        token = pipeline.fold(events).tokens.get(pipeline.DEFAULT_TOKEN_ID)
    except (KeyError, ValueError, TypeError):
        return None
    return token.cwd if token is not None else None


def _manager_domain(name) -> str | None:
    if not name:
        return None
    if name == "daemon":
        return paths.DEFAULT_DOMAIN
    for record in state.list_json_in(paths.ACTIVE):
        if (isinstance(record, dict) and record.get("agent") == "manager" and not record.get("nested")
                and record.get("name") == name):
            return record.get("domain")
    return None
