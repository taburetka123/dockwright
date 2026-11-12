import argparse
import json
import math
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from . import mcp_server as ms
from . import lane_lines, notebook_migrate, paths, pipeline, state, topic, topic_agent, topic_message, topic_outbox

FRESHNESS_FIELDS = ("last_turn_at", "processing_since", "tasked_at", "started_at")
REVIEW_BY = re.compile(r"review-by\W{0,4}(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
TEXT_MAX = 160
REPORT_MAX = 200
EVENTS_SHOWN = 40
DEFAULT_PORT = 8740
PS_WARNING = "⚠ probe failed: ps — gates and liveness may be wrong"
SHOWN_PHASES = ("context", "summary", "decision")
GATE_LOST = "gate lost — message the topic to reopen"
GATE_HINT = "plannotator sessions --open 1"
MIGRATE_COMMAND = "dockwright notebook-migrate"
HUMAN_KINDS = {"human:question": "question", "human:gate": "gate", "human:approval-dialog": "dialog"}
THREAD_PREFIXES = ("msg-", "pane-", "q-", "verdict-r")
AUTOMATION_PREFIXES = ("author-wait:", "lease:", "trigger:", "event:")
REPLY_WINDOW_SEC = 7 * 86400
META_LINE = re.compile(r"^\s*(?:[-*]\s+)?(?:from|via|node|time|completes|replaces|amends|asked_by|asked_at|"
                       r"answered_at|decided_by)(?:\s+or\s+\w+|/\w+)?:")


@dataclass
class Snapshot:
    now: float
    index: dict | None
    questions: list = field(default_factory=list)
    sessions: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    assignments: list = field(default_factory=list)


def snapshot(*, sessions: bool = True) -> Snapshot:
    snap = Snapshot(time.time(), ms._process_index())
    snap.assignments = [r for r in state.list_json_in(paths.ASSIGNMENTS) if isinstance(r, dict)]
    if sessions:
        snap.questions = _questions()
        snap.sessions = _sessions(snap.index, snap.errors)
    return snap


def keys() -> list[str]:
    found = {d.name for d in paths.ARTIFACTS.iterdir() if d.is_dir()} if paths.ARTIFACTS.is_dir() else set()
    found |= {q["ticket"] for q in _questions() if isinstance(q["ticket"], str)}
    found |= {r["ticket"] for r in state.list_json_in(paths.ASSIGNMENTS)
              if isinstance(r, dict) and isinstance(r.get("ticket"), str)}
    return sorted(k for k in found if topic.is_key(k))


def _assignment(sid) -> dict:
    if not sid:
        return {}
    try:
        record = state.read_json(paths.assignment_path(sid))
    except (ValueError, OSError):
        return {}
    return record if isinstance(record, dict) else {}


def _questions() -> list[dict]:
    out = []
    for q_path in ms.unique_question_paths():
        question = state.read_json(q_path)
        if not isinstance(question, dict):
            continue
        sid = question.get("worker_sid")
        if sid and ms._asked_by_subworker(sid):
            continue
        out.append({"sid": sid, "name": question.get("worker_name"),
                    "question_id": question.get("question_id") or Path(q_path).stem,
                    "ticket": question.get("task_key") or _assignment(sid).get("ticket"),
                    "manager": state.parent_name_of(question), "asked_at": _epoch(question.get("asked_at")),
                    "text": str(question.get("question") or ""), "asked_by": question.get("asked_by"),
                    "domain": question.get("domain")})
    return out


def _sessions(index, errors) -> list[dict]:
    out = []
    for record in state.list_json_in(paths.ACTIVE):
        sid = record.get("claude_sid") if isinstance(record, dict) else None
        if not sid:
            continue
        session = ms._session_state(sid, index, record.get("runtime") or "claude")
        approval, gate = ms._session_waits(session, index, errors)
        out.append({"sid": sid, "name": record.get("name") or sid[:8], "agent": record.get("agent"),
                    "domain": record.get("domain"), "parent": state.parent_name_of(record),
                    "ticket": _assignment(sid).get("ticket"), "alive": session["alive"], "approval": approval,
                    "gate": gate, "fresh_at": _freshest(record)})
    return out


def row(key: str, snap: Snapshot) -> dict:
    try:
        return _row(key, snap)
    except Exception as err:
        return {"key": key, "error": type(err).__name__, "activity": None}


def _row(key, snap) -> dict:
    artifacts = ms._list_artifacts(key)
    events = ms._read_events(paths.artifact_events_path(key))
    schema = ms._load_snapshot_for(key)
    stamps = {f"{a.get('phase')}.{a.get('name')}": a for a in artifacts}
    status = ms._waiting_on_status(key, schema, events, stamps, snap.now, index=snap.index,
                                   assignments=snap.assignments)
    card = topic.load(key)
    waiting = status["waiting_on"]
    priority = (card or {}).get("priority") or topic.DEFAULT_PRIORITY
    fold = status["fold"]
    tokens = [] if fold is None else [_token(tid, t, status["tokens"].get(tid), status["holders"].get(tid))
                                      for tid, t in fold.tokens.items()]
    requests = [] if fold is None else [{"token_id": r.get("token_id"),
                                         "waiting_on": status["requests"].get(r.get("request_id"))}
                                        for r in fold.open_requests]
    return {"key": key, "error": None, "card": card is not None, "pipeline": schema is not None,
            "kind": (card or {}).get("kind"), "domain": (card or {}).get("domain"),
            "status": (card or {}).get("status"), "priority": priority,
            "priority_shown": topic.shown_priority(priority, waiting) if card else topic.DEFAULT_PRIORITY,
            "title": _title(key, card, topic.artifact_body(key, "context", "ask")), "waiting_on": waiting,
            "tokens": tokens, "requests": requests, "gate": status["gate"],
            "probe_errors": [e for e in status["probe_errors"] if e != "ps"],
            "activity": _activity(events, artifacts, card),
            "last_event": _last_label(events)}


def _last_label(events) -> str | None:
    last = next((e for e in reversed(events) if e.get("type") != "inputs_handed"), None)
    return event_label(last) if last is not None else None


def _token(token_id, token, waiting, holder) -> dict:
    return {"token_id": token_id, "node": token.node, "waiting_on": waiting,
            "holder_sid": (holder or {}).get("sid"), "holder_alive": bool((holder or {}).get("alive"))}


def _title(key, card, ask) -> str:
    if card and card.get("kind") == "note":
        first = (ask or "").split("\n", 1)[0].rstrip("\r")
        return first[3:] if first.startswith("## ") else first
    if card and card.get("title"):
        return card["title"]
    return topic.title_of(ask) or key


def _activity(events, artifacts, card) -> float | None:
    stamps = [_epoch(e.get("ts")) for e in events if e.get("type") != "lane_event"]
    stamps += [_epoch(a.get("written_at")) for a in artifacts]
    stamps.append(_epoch((card or {}).get("created_at")))
    return max((t for t in stamps if t is not None), default=None)


def event_label(event) -> str:
    if event.get("type") == "lane_event":
        return f"lane_event {lane_lines.label(event.get('kind'), event.get('fields'))}"
    if event.get("phase"):
        return f"{event.get('type')} {event.get('phase')}.{event.get('name')}"
    target = event.get("to") or event.get("node")
    return f"{event.get('type')} → {target}" if target else str(event.get("type"))


def _card_of(key):
    if not key:
        return None
    try:
        return topic.load(key)
    except (ValueError, OSError):
        return None


def row_domain(ticket=None, manager=None, own=None) -> str | None:
    card = _card_of(ticket)
    if card and card.get("domain"):
        return card["domain"]
    return own or topic._manager_domain(manager)


def _is_fleet(key) -> bool:
    return (topic.read(key) or {}).get("kind") == "fleet"


RECENT_CARD_SEC = 14 * 86400


def domains(known: list[str] | None = None) -> list[str]:
    found = set(topic_agent.live_domains())
    now = time.time()
    for key in (keys() if known is None else known):
        p = paths.artifact_events_path(key)
        try:
            if not p.is_file() or now - p.stat().st_mtime > RECENT_CARD_SEC:
                continue
        except OSError:
            continue
        d = (topic.load(key) or {}).get("domain")
        if topic.is_key(d):
            found.add(d)
    return sorted(found)


def _notebook_domains() -> list[str]:
    if not notebook_migrate.notebook_dir().is_dir():
        return []
    return sorted(p.stem for p in notebook_migrate.notebook_dir().glob("*.md")
                  if not p.stem.endswith("-archive") and topic.is_key(p.stem))


def domain_notes(domain: str) -> dict:
    if not topic.is_key(domain):
        return {"source": None, "path": None, "notes": []}
    if _is_fleet(topic.fleet_key(domain)):
        notes = []
        for key in keys():
            card = topic.read(key) if key.startswith(f"note-{domain}-") else None
            if card and card.get("kind") == "note" and card.get("domain") == domain:
                body = topic.artifact_body(key, "context", "ask")
                notes.append(_note(domain, key, _title(key, card, body), body, card.get("status") or "open",
                                   card.get("last_check")))
        return {"source": "topics", "path": None, "notes": sorted(notes, key=_notebook_order(domain))}
    path = notebook_migrate.notebook_path(domain)
    if path.is_file():
        try:
            _, sections = notebook_migrate.split(notebook_migrate.read_notebook(path))
        except ValueError:
            sections = []
        return {"source": "notebook", "path": path,
                "notes": [_note(domain, None, s.heading[3:], s.text, "closed" if s.closed else "open", None)
                          for s in sections]}
    return {"source": None, "path": None, "notes": []}


def _notebook_order(domain):
    path = notebook_migrate.notebook_path(domain)
    try:
        text = notebook_migrate.read_notebook(path) if path.is_file() else ""
        order = notebook_migrate.keys_for(domain, notebook_migrate.split(text)[1])
    except ValueError:
        order = []
    rank = {key: i for i, key in enumerate(order)}
    return lambda note: (rank.get(note["key"], len(rank)), note["key"])


def _note(domain, key, title, body, status, last_check) -> dict:
    return {"domain": domain, "key": key, "title": title, "body": body, "status": status,
            "last_check": last_check, "review_by": _review_by(body)}


def _review_by(body) -> date | None:
    match = REVIEW_BY.search(body or "")
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def notebook_warnings(domain=None, known=None) -> list[str]:
    out = []
    for d in _notebook_domains():
        if domain and d != domain:
            continue
        if not _is_fleet(topic.fleet_key(d)):
            path = notebook_migrate.notebook_path(d)
            try:
                if path.is_file():
                    notebook_migrate.read_notebook(path)
            except ValueError as err:
                out.append(f"⚠ notebook {d}: {err}")
            continue
        try:
            count = notebook_migrate.pending(d)
        except ValueError as err:
            out.append(f"⚠ notebook {d} cannot migrate: {err}")
            continue
        if count:
            out.append(f"⚠ notebook {d} changed after migration: {count} — run {MIGRATE_COMMAND} {d}")
    return out


def _warnings(snap, rows, domain, *, row_errors, known=None) -> list[str]:
    out = [PS_WARNING] if snap.index is None else []
    if snap.errors:
        out.append(f"⚠ probe failed: {', '.join(snap.errors)}")
    for r in rows:
        if r.get("error"):
            if row_errors:
                out.append(f"⚠ {r['key']}: error: {r['error']}")
        elif r["probe_errors"]:
            out.append(f"⚠ {r['key']}: probe failed: {', '.join(r['probe_errors'])}")
    return out


def _item(rows, key, label, kind, domain, anchor, *, text="", note="", question_id=None) -> dict:
    r = rows.get(key) if key else None
    shown = r["priority_shown"] if r and not r.get("error") else topic.DEFAULT_PRIORITY
    return {"priority": shown, "kind": kind, "topic": label, "key": key if r else None, "domain": domain,
            "anchor": anchor, "text": text, "note": note, "question_id": question_id}


def _reply_items(rows, seen, now) -> list[dict]:
    out = []
    for key, r in rows.items():
        artifacts = None
        for a in ms._list_artifacts(key):
            if a.get("phase") != "reply":
                continue
            written_at = float(a.get("written_at") or 0)
            if written_at <= max(seen.get(key, 0), now - REPLY_WINDOW_SEC):
                continue
            re_, text = _split_reply(artifact_body(a) or "")
            if not re_.startswith("decision.msg-"):
                continue
            if artifacts is None:
                artifacts = {f"{x.get('phase')}.{x.get('name')}": x for x in ms._list_artifacts(key)}
            if (artifacts.get(re_) or {}).get("decided_by") != "engineer":
                continue
            out.append(_item(rows, key, key, "reply", r.get("domain"), written_at, text=_first_line(text)))
    return out


def _split_reply(body: str) -> tuple[str, str]:
    first, _, rest = body.partition("\n")
    if first.startswith("re: "):
        return first[4:].strip(), rest
    return "", body


def inbox(domain: str | None = None, seen: dict | None = None) -> dict:
    snap = snapshot()
    rows = {key: row(key, snap) for key in keys()}
    waiting = []
    for q in snap.questions:
        label = q["ticket"] or q["name"] or str(q["sid"] or "?")[:8]
        waiting.append(_item(rows, q["ticket"], label, "question",
                             row_domain(q["ticket"], q["manager"]) or q["domain"],
                             q["asked_at"], text=_first_line(q["text"]), question_id=q["question_id"]))
    for s in snap.sessions:
        own = s["domain"] if s["agent"] == "manager" else None
        for kind, on in (("gate", s["gate"]), ("dialog", s["approval"])):
            if s["alive"] and on:
                waiting.append(_item(rows, s["ticket"], s["ticket"] or s["name"], kind,
                                     row_domain(s["ticket"], s["parent"], own), s["fresh_at"]))
    listed = {i["key"] for i in waiting}
    for r in rows.values():
        kind = None if r.get("error") else _human_kind(str(r["waiting_on"]))
        if kind and r["key"] not in listed:
            waiting.append(_item(rows, r["key"], r["key"], kind, r["domain"], r["activity"]))
    waiting += _reply_items(rows, seen or {}, snap.now)
    for item in waiting:
        gate = (rows.get(item["key"]) or {}).get("gate") if item["key"] else None
        if item["kind"] == "gate" and gate and gate["state"] == "open" and gate.get("url"):
            item["link"] = gate["url"]
        elif item["kind"] == "gate":
            item["hint"] = GATE_HINT
    stopped = [_stopped(r) for r in rows.values()
               if not r.get("error") and str(r["waiting_on"]).startswith("stalled:")]
    stopped += [_item(rows, entry["key"], entry["key"], "undelivered", entry.get("domain"),
                      entry.get("first_failed_at"), text=entry["record"], note=entry.get("reason") or "")
                for entry in topic_outbox.entries()]
    return {"now": snap.now, "warnings": _warnings(snap, rows.values(), domain, row_errors=True, known=list(rows)),
            "notebook_warnings": notebook_warnings(domain),
            "waiting": sorted(_in_domain(waiting, domain), key=_order),
            "stopped": sorted(_in_domain(stopped, domain), key=_order)}


def _human_kind(value: str) -> str | None:
    if not value.startswith("human:"):
        return None
    return HUMAN_KINDS.get(value) or value.removeprefix("human:") or value


def _stopped(r) -> dict:
    token = next((t for t in r["tokens"] if t["waiting_on"] == r["waiting_on"]), None)
    if token is None:
        requested = next((q["token_id"] for q in r["requests"] if q["waiting_on"] == r["waiting_on"]), None)
        token = next((t for t in r["tokens"] if t["token_id"] == requested), None)
    sid = token["holder_sid"] if token else None
    report = _report(sid) if sid else None
    holder = "none" if not sid else ("alive" if token["holder_alive"] else "dead")
    lost = token is not None and ms._gate_at(r.get("gate"), token["node"], "lost")
    return {"priority": r["priority_shown"], "kind": r["waiting_on"], "topic": r["key"], "key": r["key"],
            "domain": r["domain"], "anchor": r["activity"], "text": GATE_LOST if lost else report or "",
            "note": "" if lost or report else f"no report, holder {holder}"}


def _in_domain(items, domain) -> list[dict]:
    return [i for i in items if domain is None or i["domain"] in (None, domain)]


def _order(item) -> tuple:
    priority = item["priority"]
    rank = topic.PRIORITIES.index(priority) if priority in topic.PRIORITIES else len(topic.PRIORITIES)
    return (rank, item["anchor"] if item["anchor"] is not None else math.inf, item["topic"])


def _report(sid) -> str | None:
    best = None
    for path in paths.DONE.rglob(f"{sid}-*.json") if paths.DONE.is_dir() else []:
        record = state.read_json(path)
        if isinstance(record, dict) and (best is None or (_epoch(record.get("completed_at")) or 0)
                                         > (_epoch(best.get("completed_at")) or 0)):
            best = record
    summary = " ".join(str((best or {}).get("summary") or "").split())
    return re.split(r"(?<=[.!?])\s", summary, maxsplit=1)[0][:REPORT_MAX] if summary else None


def topics(domain=None, kind=None, status=None) -> dict:
    if kind == "note" and domain:
        found = domain_notes(domain)
        notes = [n for n in found["notes"] if status is None or n["status"] == status]
        return {"now": time.time(), "warnings": [], "notebook_warnings": notebook_warnings(domain), "rows": None,
                "notes": notes, "notebook": found["path"] if found["source"] == "notebook" else None}
    snap = snapshot(sessions=False)
    rows = [row(key, snap) for key in keys()]
    filtered = bool(domain or kind or status)
    failed = [r for r in rows if r.get("error")] if filtered else []
    if filtered:
        rows = [r for r in rows if not r.get("error") and r["card"] and _matches(r, domain, kind, status)]
    rows.sort(key=_freshest_first)
    return {"now": snap.now, "warnings": _warnings(snap, [*failed, *rows], domain, row_errors=filtered),
            "notebook_warnings": notebook_warnings(domain), "rows": rows, "notes": None, "notebook": None}


def _matches(r, domain, kind, status) -> bool:
    if (domain and r["domain"] != domain) or (kind and r["kind"] != kind):
        return False
    if status:
        current = r["status"] if r["kind"] in topic.TOKENLESS_KINDS else (
            "closed" if r["waiting_on"] == "none" else "open")
        return current == status
    return True


def _freshest_first(r) -> tuple:
    return (1, 0.0, r["key"]) if r["activity"] is None else (0, -r["activity"], r["key"])


def artifact_body(artifact: dict) -> str | None:
    try:
        return state.parse_artifact(Path(artifact["path"]).read_text())[1]
    except (KeyError, OSError, ValueError):
        return None


def _stamp(a) -> dict:
    return {"phase": a.get("phase"), "name": a.get("name"), "status": a.get("status"),
            "scope": a.get("scope") or "internal", "written_at": a.get("written_at")}


def _prs(links, artifacts, carried=()) -> list[str]:
    found = dict.fromkeys([str(links["pr"])] if links.get("pr") else [])
    for a in sorted((a for a in artifacts if a.get("phase") == "implement"), key=lambda a: a.get("written_at") or 0):
        found |= dict.fromkeys(pipeline.PR_URL.findall(artifact_body(a) or ""))
    found |= dict.fromkeys(carried)
    return list(found)


def topic_view(key: str) -> dict | None:
    if key not in keys():
        return None
    status = json.loads(ms.pipeline_status_impl(key, "json"))
    artifacts = ms._list_artifacts(key)
    view = status.get("topic") or {}
    domain = view.get("domain")
    links = dict(view.get("links") or {})
    if not links.get("ticket") and topic.ticket_link(key):
        links["ticket"] = topic.ticket_link(key)
    registry = (paths.artifact_path(topic.fleet_key(domain), topic.DECISIONS_PHASE, domain)
                if domain and topic.is_key(domain) else None)
    summary = next((a for a in artifacts if a.get("phase") == "summary" and a.get("name") == "thread"), None)
    decisions = sorted((a for a in artifacts if a.get("phase") == "decision"
                        and not str(a.get("name")).startswith(THREAD_PREFIXES)),
                       key=lambda a: a.get("written_at") or 0)
    return {
        "key": key,
        "names": sorted({f"{a.get('phase')}.{a.get('name')}" for a in artifacts}),
        "header": {"kind": view.get("kind"), "domain": domain, "priority": view.get("priority"),
                   "priority_shown": view.get("priority_shown"), "waiting_on": status["waiting_on"],
                   "links": links, "blocked_by": view.get("blocked_by") or [],
                   "status": view.get("status"),
                   "review_by": (_review_by(topic.artifact_body(key, "context", "ask"))
                                 if view.get("kind") == "note" else None),
                   "last_check": view.get("last_check"), "gate": status.get("gate"), "prs": _prs(links, artifacts, status.get("carried_prs") or [])},
        "probe_errors": status["probe_errors"],
        "summary": artifact_body(summary) if summary else None,
        "thread": _thread(key, artifacts, status["events"]),
        "decisions": [{**_stamp(a), "decided_by": a.get("decided_by"), "body": artifact_body(a)} for a in decisions],
        "rulings": (f"{topic.fleet_key(domain)} → {topic.DECISIONS_PHASE}.{domain}"
                    if registry is not None and registry.is_file() else None),
        "ask": [{**_stamp(a), "body": artifact_body(a)} for a in artifacts
                if a.get("phase") == "context" and (a.get("name") == "ask" or str(a.get("name")).startswith("rework-"))],
        "outputs": [_stamp(a) for a in artifacts
                    if (a.get("scope") or "internal") == "output" and a.get("phase") not in SHOWN_PHASES
                    and a.get("phase") != "reply"],
        "kitchen": [_stamp(a) for a in artifacts if (a.get("scope") or "internal") in ("checkpoint", "internal")],
        "sessions": [{"name": s.get("name"), "sid": s.get("claude_sid"), "liveness": s.get("liveness"),
                      "report": _report(s.get("claude_sid")) if s.get("claude_sid") else None}
                     for s in status["assignments"]],
        "events": status["events"][-EVENTS_SHOWN:],
        "questions": [{"question_id": q.get("question_id"), "worker_name": q.get("worker_name"),
                       "text": str(q.get("question") or ""), "asked_at": q.get("asked_at"),
                       "asked_by": q.get("asked_by"), "domain": q.get("domain")}
                      for q in ms._topic_questions(key)],
    }


def _thread(key, artifacts, events) -> list[dict]:
    out = []
    for a in artifacts:
        phase, name = str(a.get("phase")), str(a.get("name"))
        body = artifact_body(a) or ""
        if phase == "reply":
            re_, text = _split_reply(body)
            entry = {"who": _reply_author(a.get("writer_sid"), events), "text": text, "re": re_ or None}
        elif phase == "decision" and name.startswith("msg-"):
            text = topic_message.text_of(body)
            who = ("automation" if text.startswith(AUTOMATION_PREFIXES)
                   else "you" if a.get("decided_by") == "engineer" else "manager")
            entry = {"who": who, "text": text, "re": None}
        elif phase == "decision" and name.startswith("pane-"):
            lines = body.splitlines()
            who = "you (pane)" if any(re.match(r"^\s*(?:[-*]\s+)?from:\s*engineer\b", l) for l in lines) \
                else "manager (pane)"
            entry = {"who": who, "text": "\n".join(l for l in lines if not META_LINE.match(l)).strip(), "re": None}
        elif phase == "decision" and name.startswith("q-"):
            entry = {"who": _answer_author(a.get("decided_by"), body), "text": _answer_text(body), "re": None}
        elif phase == "decision" and name.startswith("verdict-r"):
            entry = {"who": f"you: gate verdict {name.removeprefix('verdict-')}", "text": _first_line(body),
                     "re": None}
        else:
            continue
        out.append({"name": f"{phase}.{name}", "written_at": a.get("written_at"), **entry})
    return sorted(out, key=lambda t: (-(t["written_at"] or 0), t["name"]))


def _reply_author(writer_sid, events) -> str:
    assignment = _assignment(writer_sid)
    if str(assignment.get("name") or "").endswith(topic_agent.AGENT_SUFFIX):
        return "topic agent"
    node = ms._positions_by_holder(events).get(assignment.get("assignment_id"))
    return f"node {node[0]}" if node else "session"


def _answer_author(decided_by, body) -> str:
    asked = next((l.split(":", 1)[1].strip() for l in body.splitlines() if l.startswith("asked_by:")), "")
    name = asked.split(" (", 1)[0].strip() or "?"
    return f"{'you' if decided_by == 'engineer' else 'manager'} answered {name}"


def _answer_text(body) -> str:
    lines = body.splitlines()
    start = next((i + 1 for i, l in enumerate(lines) if l.strip() == "## answer"), None)
    if start is None:
        return body.strip()
    out = []
    for l in lines[start:]:
        if META_LINE.match(l):
            break
        out.append(l)
    return "\n".join(out).strip()


def delivery_of(key: str, record: str) -> dict | None:
    events = [e for e in ms._read_events(paths.artifact_events_path(key)) if e.get("type") == "message_delivered"]
    if record.startswith("decision.q-"):
        qid = record[len("decision.q-"):]
        matches = [e for e in events if str(e.get("question_id") or "")[:8] == qid]
    else:
        matches = [e for e in events if e.get("record") == record]
    return matches[-1] if matches else None


def _epoch(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return None
        return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()
    return None


def _freshest(record) -> float | None:
    return max((t for t in (_epoch(record.get(f)) for f in FRESHNESS_FIELDS) if t is not None), default=None)


def _midnight(day: date) -> float:
    return datetime.combine(day, datetime.min.time()).timestamp()


def _first_line(text) -> str:
    lines = str(text or "").strip().splitlines()
    return lines[0].strip()[:TEXT_MAX] if lines else ""


def age(now, at) -> str:
    if at is None:
        return "?"
    seconds = max(0, int(now - at))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def iso(ts) -> str:
    at = _epoch(ts)
    return datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M") if at is not None else "?"


def check_line(record) -> str:
    if not isinstance(record, dict):
        return "never"
    runs = ", ".join(f"{r.get('status')} `{r.get('cmd')}`" for r in record.get("runs") or [] if isinstance(r, dict))
    return f"{iso(record.get('at'))} — {runs or 'no runs'}"


def _item_line(item, now) -> str:
    tail = f' — "{item["text"]}"' if item["text"] else (f" — {item['note']}" if item["note"] else "")
    tail += f"  {item['link']}" if item.get("link") else ""
    tail += f"  reopen: {item['hint']}" if item.get("hint") else ""
    return f"{item['priority']}  {item['kind']}  {item['topic']} · {age(now, item['anchor'])}{tail}"


def render_inbox(data: dict) -> str:
    lines = [*data["warnings"], *data.get("notebook_warnings", [])]
    if not data["waiting"] and not data["stopped"]:
        return "\n".join([*lines, "Nothing is waiting on you."])
    if data["waiting"]:
        lines += ["▶ Waiting on you", *(_item_line(i, data["now"]) for i in data["waiting"])]
    if data["stopped"]:
        lines += ["▶ Stopped", *(_item_line(i, data["now"]) for i in data["stopped"])]
    return "\n".join(lines)


def _row_line(r, now) -> str:
    if r.get("error"):
        return f"   ?  {r['key']}  error: {r['error']}"
    shown_state = r["status"] if r["kind"] in topic.TOKENLESS_KINDS else r["waiting_on"]
    line = (f"{age(now, r['activity']):>4}  {r['kind'] or '-'}  {r['domain'] or '-'}  {r['priority_shown']}  "
            f"{shown_state or '-'}  {r['key']}  {r['title']}")
    return line + (f"  · {r['last_event']}" if r["last_event"] else "")


def _note_line(note) -> str:
    return "  ".join(part for part in (note["status"], note["key"], note["title"]) if part)


def render_topics(data: dict) -> str:
    lines = [*data["warnings"], *data.get("notebook_warnings", [])]
    if data["notes"] is not None:
        if not data["notes"]:
            return "\n".join([*lines, "no topics"])
        if data["notebook"]:
            lines.append(f"from notebook {data['notebook']}, not migrated")
        return "\n".join([*lines, *(_note_line(n) for n in data["notes"])])
    if not data["rows"]:
        return "\n".join([*lines, "no topics"])
    return "\n".join([*lines, *(_row_line(r, data["now"]) for r in data["rows"])])


def render_topic(view: dict) -> str:
    h = view["header"]
    shown = f" (shown {h['priority_shown']})" if h["priority_shown"] != h["priority"] else ""
    lines = [f"# {view['key']}",
             f"kind: {h['kind'] or '-'} · domain: {h['domain'] or '-'} · priority: {h['priority'] or '-'}{shown} · "
             f"waiting_on: {h['waiting_on']}"]
    links = " ".join(f"{k}={v}" for k, v in h["links"].items() if v and k != "pr")
    if links or h["blocked_by"]:
        lines.append(f"links: {links or '-'} · blocked_by: {', '.join(h['blocked_by']) or '-'}")
    if h["prs"]:
        lines.append(f"PR: {' '.join(h['prs'])}")
    if h["kind"] in topic.TOKENLESS_KINDS:
        lines.append(f"status: {h['status'] or '-'} · review-by: {h['review_by'] or '-'} · "
                     f"last check: {check_line(h['last_check'])}")
    if view["probe_errors"]:
        lines.append(f"⚠ probe failed: {', '.join(view['probe_errors'])}")
    lines += ["", "## Thread", *(f"{iso(t['written_at'])}  {t['who']}: {_first_line(t['text'])}"
                                  for t in view["thread"])]
    lines += ["", "## Summary", view["summary"] if view["summary"] is not None else "no summary", "", "## Decisions"]
    for d in view["decisions"]:
        lines.append(f"- {iso(d['written_at'])} decision.{d['name']} (decided_by {d['decided_by'] or '?'})")
        lines += [f"  {line}" for line in (d["body"] or "").splitlines()]
    if view["rulings"]:
        lines.append(f"domain rulings: {view['rulings']}")
    lines += ["", "## Ask"]
    for a in view["ask"]:
        lines += [f"### {a['phase']}.{a['name']}", a["body"] or ""]
    lines += ["", "## Outputs", *(f"- {a['phase']}.{a['name']} [{a['status']}] @{iso(a['written_at'])}"
                                  for a in view["outputs"])]
    scopes = [a["scope"] for a in view["kitchen"]]
    lines += ["", f"kitchen: {len(scopes)} (checkpoint {scopes.count('checkpoint')}, "
                  f"internal {scopes.count('internal')})", "", "## Sessions"]
    for s in view["sessions"]:
        lines.append(f"- {s['name']} {str(s['sid'] or '')[:8]} ({s['liveness']})"
                     + (f' — "{s["report"]}"' if s["report"] else ""))
    lines += ["", f"## Events (last {EVENTS_SHOWN})"]
    for e in view["events"]:
        lines.append(f"- {iso(e.get('ts'))} {event_label(e)}" + (f" — {e['reason']}" if e.get("reason") else ""))
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dockwright board",
                                     description="Board: what waits on you, every topic, one topic; --serve also takes your messages.")
    parser.add_argument("--serve", action="store_true", help="Serve the screens and their forms on http://127.0.0.1:PORT/.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"The port for --serve (default {DEFAULT_PORT}).")
    sub = parser.add_subparsers(dest="screen")
    screen = sub.add_parser("inbox", help="What waits on you and what stopped (the default screen).")
    screen.add_argument("--domain", help="Keep lines of this domain and lines whose domain is unknown.")
    screen = sub.add_parser("topics", help="Every topic, freshest first.")
    screen.add_argument("--domain")
    screen.add_argument("--kind", help="A card kind: note, lane, fleet or a schema name.")
    screen.add_argument("--status", choices=topic.STATUSES)
    screen.add_argument("--json", action="store_true", help="Print the index as JSON.")
    screen = sub.add_parser("topic", help="One topic: card, summary, decisions, ask, outputs, sessions, journal.")
    screen.add_argument("key")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--serve" not in argv and (not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help"))):
        argv.insert(0, "inbox")
    args = _parser().parse_args(argv)
    if args.serve:
        from . import board_web
        return board_web.serve(args.port)
    if args.screen == "topic":
        view = topic_view(args.key)
        if view is None:
            print(f"no topic {args.key}", file=sys.stderr)
            return 1
        print(render_topic(view))
    elif args.screen == "topics" and args.json:
        print(json.dumps(topics(args.domain, args.kind, args.status), default=str))
    elif args.screen == "topics":
        print(render_topics(topics(args.domain, args.kind, args.status)))
    else:
        print(render_inbox(inbox(args.domain)))
    return 0
