import asyncio
import html
import re
import secrets
import socket
import sys
import time
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from . import board, board_theme, config, paths, topic, topic_agent, topic_message
from . import mcp_server as ms

REFRESH_SEC = 30
LOOPBACK = "127.0.0.1"
PROBE_SEC = 0.2
FORM_TYPE = "application/x-www-form-urlencoded"
FORM_MAX_BYTES = 64 * 1024
ARTIFACT_NAME = re.compile(r"(?<![\w.-])([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)(?![\w-]|\.[A-Za-z0-9_-])")
CSS = board_theme.css()
NAV = (("inbox", "/", "Inbox"), ("topics", "/topics", "Topics"))
CURRENT = ' aria-current="page"'
NEW_TAB = '<span class="vh"> (opens in a new tab)</span>'
LINK_LABELS = {"pr": "Pull request", "ticket": "Ticket"}
SEP = '<span aria-hidden="true">/</span>'
MD_HEAD = re.compile(r"^\s*#{1,6}(?:\s+|$)(\[[ xX]\]\s*)?")
META_LINE = re.compile(r"^\s*(?:[-*]\s+)?(?:from|via|node|time|completes|replaces|amends|asked_by|asked_at|"
                       r"answered_at|decided_by)(?:\s+or\s+\w+|/\w+)?:")
LABEL_LINE = re.compile(r"^\s*(?:\d+[.)]\s+)?[^:.!?]*:\s*$")
SEEN_COOKIE = "dockwright_seen"
SEEN_MAX_PAIRS = 50
SEEN_MAX_AGE = 30 * 86400
TONES = (("question", " chip-accent"), ("reply", " chip-accent"), ("gate", " chip-attn"), ("dialog", " chip-attn"),
         ("review-by", " chip-danger"), ("stalled:", " chip-danger"), ("undelivered", " chip-danger"))


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _topic_url(key: str) -> str:
    return f"/topic/{quote(key, safe='')}"


def _topic_link(key, label=None) -> str:
    return f'<a href="{_topic_url(key)}">{_e(label or key)}</a>'


def _hidden(csrf: str) -> str:
    return f'<input type="hidden" name="csrf" value="{_e(csrf)}">'


def _artifact_url(key, name) -> str:
    return f"/topic/{quote(key, safe='')}/a/{quote(name, safe='')}"


def _artifact_link(key, stamp) -> str:
    name = f"{stamp['phase']}.{stamp['name']}"
    return f'<a href="{_artifact_url(key, name)}">{_e(name)}</a>'


def _linked(key, text, names) -> str:
    known = set(names)
    return ARTIFACT_NAME.sub(lambda m: (f'<a href="{_artifact_url(key, m.group(1))}">{m.group(1)}</a>'
                                        if m.group(1) in known else m.group(0)), _e(text))


def _notes_url(domain) -> str:
    return f"/topics?domain={quote(str(domain), safe='')}&kind=note"


def _a(href, inner, cls=None) -> str:
    attr = f' class="{cls}"' if cls else ""
    if _is_http(href):
        return f'<a href="{_e(href)}"{attr} target="_blank" rel="noopener">{inner}{NEW_TAB}</a>'
    return f'<a href="{_e(href)}"{attr}>{inner}</a>'


def _cell(inner, href, cls=None) -> str:
    attr = f' class="{cls}"' if cls else ""
    link = f'<a href="{_e(href)}" class="cell" tabindex="-1">{inner}</a>' if href and inner else inner
    return f"<td{attr}>{link}</td>"


def _page(title, body, *, current=None, refresh=False, fresh="", heading=True) -> str:
    meta = f'<meta http-equiv="refresh" content="{REFRESH_SEC}">' if refresh else ""
    nav = "".join(f'<a href="{href}"{CURRENT if name == current else ""}>{label}</a>' for name, href, label in NAV)
    head = f"<h1>{_e(title)}</h1>" if heading else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">{meta}'
            f"<title>{_e(title)}</title><style>{CSS}</style></head>"
            '<body class="board"><a href="#main" class="skip">Skip to content</a>'
            '<header class="top"><div class="top-in"><span class="brand">dockwright</span>'
            f'<nav class="nav" aria-label="Board">{nav}</nav>'
            f'<div class="top-end">{fresh}<a href="/new" class="btn btn-primary">New work</a></div></div></header>'
            f'<main class="page" id="main">{head}{body}</main></body></html>')


def _with(path, params) -> str:
    return f"{path}?{urlencode(params)}" if params else path


def _fresh(path, params, paused, now) -> str:
    kept = {k: v for k, v in params.items() if v and k != "refresh"}
    if paused:
        return f'<span class="fresh">Auto-refresh paused · <a href="{_e(_with(path, kept))}">Resume</a></span>'
    stamp = datetime.fromtimestamp(now).strftime("%H:%M")
    return (f'<span class="fresh">Updated {stamp} · '
            f'<a href="{_e(_with(path, {**kept, "refresh": "off"}))}">Pause auto-refresh</a></span>')


def _is_http(text) -> bool:
    return str(text).startswith(("http://", "https://"))


SENT_TEXTS = {
    ("pane+resume", True): "Delivered: {route} ({pane}). The answer appears in the thread.",
    ("resume", True): "Delivered: {route} ({pane}). The answer appears in the thread.",
    ("engine", True): "Delivered: {route} ({pane}). The answer appears in the thread.",
    ("replace", True): "The node's session was restarted with your message in its inputs. The answer appears in the thread.",
    ("restart", True): "The node's session was restarted with your message in its inputs. The answer appears in the thread.",
    ("agent", True): "No live session — the topic agent answers. The answer appears in the thread.",
    ("answer", True): "Answer recorded, question closed.",
    ("intake", True): "Topic created, intake started.",
}


def _sent_banner(sent) -> str:
    if not sent:
        return ""
    event = sent.get("event")
    if event is None:
        return _callout(["Sent; the delivery result is not recorded yet."])
    route, pane = str(event.get("route")), str(event.get("pane") or "?")
    if not event.get("ok") and event.get("attached"):
        return _callout([f"Not delivered: {event.get('reason')}. Queued; retried every minute while this domain's "
                         "manager lives; it types once that pane's input line reads idle or the human-attached flag "
                         "drops; the card is in Inbox › Stopped."], danger=True)
    if not event.get("ok"):
        return _callout([f"Not delivered: {event.get('reason')}. Queued; retried every 5 minutes while this "
                         "domain's manager lives; the card is in Inbox › Stopped."], danger=True)
    if route == "pane":
        return _callout([f"Delivered to pane {pane}. The node answers in the thread before it goes on."])
    text = SENT_TEXTS.get((route, True), "Delivered: {route} ({pane}). The answer appears in the thread.")
    return _callout([text.format(route=route, pane=pane)])


def _callout(lines, *, danger=False) -> str:
    if not lines:
        return ""
    items = "".join(f"<li>{_e(line)}</li>" for line in lines)
    kind, role = ("callout callout-danger", "alert") if danger else ("callout", "note")
    return f'<div class="{kind}" role="{role}"><ul>{items}</ul></div>'


def _select(ident, name, label, options, chosen) -> str:
    rendered = "".join(f'<option value="{_e(value)}"{" selected" if value == chosen else ""}>{_e(text)}</option>'
                       for value, text in options)
    return (f'<div class="field"><label for="{ident}">{label}</label>'
            f'<select id="{ident}" name="{name}">{rendered}</select></div>')


def _filters(action, values, *, prefix, topics=False, kinds=(), paused=False) -> str:
    chosen = values.get("domain") or ""
    domains = board.domains()
    if chosen and chosen not in domains:
        domains = [*domains, chosen]
    fields = _select(f"{prefix}-domain", "domain", "Domain", [("", "All domains"), *((d, d) for d in domains)], chosen)
    if topics:
        listed = "".join(f'<option value="{_e(k)}"></option>' for k in kinds)
        fields += (f'<div class="field"><label for="{prefix}-kind">Kind</label>'
                   f'<input id="{prefix}-kind" name="kind" list="{prefix}-kinds" placeholder="any kind" '
                   f'value="{_e(values.get("kind"))}"><datalist id="{prefix}-kinds">{listed}</datalist></div>')
        fields += _select(f"{prefix}-status", "status", "Status",
                          [("", "Any status"), *((s, s) for s in topic.STATUSES)], values.get("status") or "")
    hidden = '<input type="hidden" name="refresh" value="off">' if paused else ""
    return (f'<form class="toolbar" method="get" action="{action}">{fields}{hidden}'
            '<button class="btn" type="submit">Apply</button></form>')


def _prio(value) -> str:
    text = str(value or "-")
    level = f" {text.lower()}" if text in topic.PRIORITIES else ""
    return f'<span class="prio{level}" title="Priority {_e(text)}">{_e(text)}</span>'


def _tone(kind) -> str:
    return next((tone for prefix, tone in TONES if str(kind).startswith(prefix)), "")


def _state_chip(value) -> str:
    if not value:
        return '<span class="chip">-</span>'
    text = str(value)
    if text.startswith("human:"):
        rest = text.removeprefix("human:")
        tone = " chip-accent" if rest == "question" else " chip-attn"
        return f'<span class="chip{tone}">waiting on you: {_e(rest)}</span>'
    tone = " chip-danger" if text.startswith("stalled:") else ""
    return f'<span class="chip{tone}">{_e(text)}</span>'


STATE_WORDS = {"human:question": "waiting on you: question", "human:gate": "waiting on you: gate",
               "human:approval-dialog": "waiting on you: dialog", "none": "closed"}


def _state_words(h, kind, live=None) -> str:
    if kind in topic.TOKENLESS_KINDS:
        return str(h.get("status") or "open")
    value = str(h["waiting_on"])
    if value == "none" and live:
        return f"working: {live.get('name')}"
    if value in STATE_WORDS:
        return STATE_WORDS[value]
    for prefix, word in (("worker:", "working"), ("external:", "waiting"), ("stalled:", "stalled")):
        if value.startswith(prefix):
            return f"{word}: {value[len(prefix):]}"
    return value


def _chip_link(href, chip) -> str:
    return _a(href, chip, "chip-link") if href else chip


def _table(heads, rows) -> str:
    head = "".join(f'<th scope="col">{h}</th>' for h in heads)
    return (f'<div class="table-wrap"><table class="grid"><thead><tr>{head}</tr></thead>'
            f"<tbody>{rows}</tbody></table></div>")


def _row_href(item) -> str | None:
    key, link = item.get("key"), str(item.get("link") or "")
    if key and item.get("question_id"):
        return f"{_topic_url(key)}#q-{quote(str(item['question_id']), safe='')}"
    if key and item.get("kind") == "reply":
        return f"{_topic_url(key)}#thread"
    if _is_http(link):
        return link
    if key:
        return _topic_url(key)
    return None


def _cta(href) -> tuple[str, bool]:
    if _is_http(href):
        return "Open gate", True
    if "#q-" in href:
        return "Answer", False
    return "Open topic", False


def _item_row(item, now) -> str:
    key = item["key"]
    href = _row_href(item)
    if key:
        label = f'<a href="{_topic_url(key)}" class="key">{_e(item["topic"])}</a>'
    elif href:
        label = _a(href, _e(item["topic"]), "key")
    else:
        label = f'<span class="key">{_e(item["topic"])}</span>'
    words = item["text"] or item["note"]
    meta = (f'<span class="item-meta">{_prio(item["priority"])}'
            f'<span class="chip{_tone(item["kind"])}">{_e(item["kind"])}</span>'
            f'<span class="age" title="{_e(board.iso(item["anchor"]))}">{_e(board.age(now, item["anchor"]))}</span>'
            "</span>")
    text = f'<span class="item-text">{_e(words)}</span>' if words else ""
    if href is None:
        main = f'<div class="item-main">{meta}{text}</div>'
    else:
        cta, external = _cta(href)
        tone = " btn-primary" if cta in ("Answer", "Open gate") else ""
        tab = ' target="_blank" rel="noopener"' if external else ""
        main = (f'<a href="{_e(href)}" class="item-main"{tab}>{meta}{text}'
                f'<span class="btn{tone} item-cta">{cta}{NEW_TAB if external else ""}</span></a>')
    link = str(item.get("link") or "")
    hint = "" if not link or _is_http(link) else f'<p class="item-hint">gate: <code class="mono">{_e(link)}</code></p>'
    if item.get("hint"):
        hint += f'<p class="item-hint">reopen: <code class="mono">{_e(item["hint"])}</code></p>'
    return f'<li class="item"><div class="item-head">{label}</div>{main}{hint}</li>'


def inbox_page(data, domain=None, paused=False) -> str:
    waiting, stopped = data["waiting"], data["stopped"]
    body = [f'<div class="head"><div><h1>Inbox</h1><p class="sub">{len(waiting)} waiting on you · '
            f'{len(stopped)} stopped</p></div>{_filters("/", {"domain": domain}, prefix="f", paused=paused)}</div>',
            _callout(data["warnings"])]
    if not waiting and not stopped:
        body.append("<p>Nothing is waiting on you.</p>")
    for ident, title, items in (("h-wait", "Waiting on you", waiting), ("h-stop", "Stopped", stopped)):
        if items:
            rows = "".join(_item_row(i, data["now"]) for i in items)
            body.append(f'<section class="section" aria-labelledby="{ident}"><h2 id="{ident}">{title} '
                        f'<span class="count">{len(items)}</span></h2><ul class="items">{rows}</ul></section>')
    return _page("Inbox", "".join(body), current="inbox", refresh=not paused,
                 fresh=_fresh("/", {"domain": domain}, paused, data["now"]), heading=False)


def _topic_row(r, now) -> str:
    link = f'<a href="{_topic_url(r["key"])}" class="key">{_e(r["key"])}</a>'
    if r.get("error"):
        return f'<tr><td>{link}</td><td colspan="5" class="muted">error: {_e(r["error"])}</td></tr>'
    href = _topic_url(r["key"])
    shown = r["status"] if r["kind"] in topic.TOKENLESS_KINDS else r["waiting_on"]
    title = (f'<a href="{href}" class="cell" tabindex="-1"><span class="title" title="{_e(r["title"])}">'
             f'{_e(r["title"])}</span></a>' if r["title"] else "")
    return (f"<tr><td>{link}{title}</td>{_cell(_state_chip(shown), href)}{_cell(_prio(r['priority_shown']), href)}"
            f"{_cell(_e(r['kind'] or '-'), href)}{_cell(_e(r['domain'] or '-'), href)}"
            + _cell(f'<span class="stack">{_e(board.age(now, r["activity"]))}'
                    f'<span class="muted small">{_e(r["last_event"] or "")}</span></span>', href) + "</tr>")


def _note_row(n, today) -> str:
    href = _topic_url(n["key"]) if n["key"] else None
    where = (f'<a href="{href}" class="key">{_e(n["key"])}</a>' if href
             else '<span class="muted small">not migrated</span>')
    title = (f'<a href="{href}" class="cell" tabindex="-1">{_e(n["title"])}</a>' if href and n["title"]
             else _e(n["title"]))
    due = n["review_by"] is not None and n["review_by"] <= today and n["status"] == "open"
    return (f'<tr><td><span class="stack">{title}{where}</span></td>'
            + _cell(f'<span class="chip{" chip-ok" if n["status"] == "open" else ""}">{_e(n["status"])}</span>', href)
            + _cell(f'<span class="chip{" chip-danger" if due else ""}">{_e(n["review_by"] or "—")}</span>', href)
            + _cell(f'<span class="muted">{_e(board.check_line(n["last_check"]))}</span>', href) + "</tr>")


def topics_page(data, query, paused=False) -> str:
    notes = data["notes"]
    kinds = sorted({str(r["kind"]) for r in data["rows"] or [] if r.get("kind")} | set(topic.TOKENLESS_KINDS))
    filters = _filters("/topics", query, prefix="t", topics=True, kinds=kinds, paused=paused)
    if notes is not None:
        domain = query.get("domain") or ""
        title, sub = (f"Notes · {domain}" if domain else "Notes"), f"{len(notes)} notes"
        notice = (f'<p class="muted small">from notebook {_e(data["notebook"])}, not migrated</p>'
                  if data["notebook"] and notes else "")
        today = date.today()
        table = (_table(("Note", "Status", "Review by", "Last check"), "".join(_note_row(n, today) for n in notes))
                 if notes else "<p>no notes</p>")
    else:
        rows = data["rows"]
        title, sub, notice = "Topics", f"{len(rows)} topics · freshest activity first", ""
        table = (_table(("Topic", "State", "Priority", "Kind", "Domain", "Last activity"),
                        "".join(_topic_row(r, data["now"]) for r in rows)) if rows else "<p>no topics</p>")
    body = (f'<div class="head"><div><h1>{_e(title)}</h1><p class="sub">{_e(sub)}</p></div></div>'
            f'{filters}{_callout(data["warnings"])}{notice}{table}')
    return _page("Topics", body, current="topics", refresh=not paused,
                 fresh=_fresh("/topics", query, paused, data["now"]), heading=False)


def _crumbs(*parts) -> str:
    return f'<nav class="crumbs" aria-label="Breadcrumb">{SEP.join(p for p in parts if p)}</nav>'


def _section(ident, title, inner) -> str:
    return f'<section class="section" aria-labelledby="{ident}"><h2 id="{ident}">{title}</h2>{inner}</section>'


def _ext_link(label, value) -> str:
    shown = _a(value, _e(value)) if _is_http(value) else _e(value)
    return f"<span>{_e(label)}: {shown}</span>"


def _ticket(value) -> str:
    text = str(value)
    template = config.ticket_url()
    href = template.replace("{key}", quote(text, safe="")) if template else ""
    if not _is_http(text) and _is_http(href):
        return f"<span>{LINK_LABELS['ticket']}: {_a(href, _e(text))}</span>"
    return _ext_link(LINK_LABELS["ticket"], text)


def _clip(text, limit=160) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _sub(view) -> str:
    lines = (view["ask"][0]["body"] or "").strip().splitlines() if view["ask"] else []
    text = MD_HEAD.sub("", lines[0]).strip() if lines else ""
    return f'<p class="sub">{_e(_clip(text))}</p>' if text else ""


def _waiting_href(h, questions) -> str | None:
    human = board.HUMAN_KINDS.get(str(h["waiting_on"]))
    gate = h.get("gate") or {}
    url = str(gate.get("url") or "")
    if human == "question" and questions:
        return "#questions"
    if human == "gate" and gate.get("state") == "open" and _is_http(url):
        return url
    return None


def _facts(h, questions) -> str:
    set_to = (f'<span class="muted small">set {_e(h["priority"] or "-")}</span>'
              if h["priority_shown"] != h["priority"] else "")
    kind = _chip_link(f"/topics?kind={quote(str(h['kind']), safe='')}" if h["kind"] else None,
                      f'<span class="chip">{_e(h["kind"] or "-")}</span>')
    domain = _chip_link(f"/topics?domain={quote(str(h['domain']), safe='')}" if h["domain"] else None,
                        f'<span class="chip">domain: {_e(h["domain"] or "-")}</span>')
    state = _chip_link(_waiting_href(h, questions), _state_chip(h["waiting_on"]))
    return f'<div class="facts">{kind}{domain}{_prio(h["priority_shown"])}{set_to}{state}</div>'


def _links(h) -> str:
    parts = []
    for name, value in h["links"].items():
        if not value or name == "pr":
            continue
        if name == "worktree":
            parts.append(f'<span>Worktree: <code class="mono">{_e(value)}</code></span>')
        else:
            parts.append(_ticket(value) if name == "ticket" else _ext_link(LINK_LABELS.get(name, name), value))
    parts += [_ext_link(LINK_LABELS["pr"], url) for url in h.get("prs") or []]
    if h["blocked_by"]:
        known = set(board.keys())
        blocked = ", ".join(_topic_link(b) if isinstance(b, str) and b in known else _e(b) for b in h["blocked_by"])
        parts.append(f"<span>Blocked by: {blocked}</span>")
    if h["kind"] in topic.TOKENLESS_KINDS:
        parts += [f"<span>Status: {_e(h['status'] or '-')}</span>",
                  f"<span>Review by: {_e(h['review_by'] or '-')}</span>",
                  f"<span>Last check: {_e(board.check_line(h['last_check']))}</span>"]
    return f'<div class="links">{"".join(parts)}</div>' if parts else ""


def _asker(q) -> str:
    if q.get("asked_by") == "manager":
        return f"manager {q.get('domain') or ''}".strip()
    return str(q["worker_name"])


def _question(key, csrf, q, draft) -> str:
    qid = _e(q["question_id"])
    return (f'<article class="question" id="q-{qid}"><div class="q-meta"><strong>{_e(_asker(q))}</strong> '
            f'asks · {_e(board.iso(q["asked_at"]))}</div><p class="q-text">{_e(q["text"])}</p>'
            f'<form method="post" action="{_topic_url(key)}/answer">{_hidden(csrf)}'
            f'<input type="hidden" name="question_id" value="{qid}">'
            f'<label for="answer-{qid}">Your answer</label>'
            f'<textarea id="answer-{qid}" name="text" rows="4">{_e(draft)}</textarea>'
            '<div class="form-row"><button class="btn btn-primary" type="submit">Send answer</button></div>'
            "</form></article>")


def _questions(key, csrf, questions, draft, answered) -> str:
    if not questions:
        return ""
    cards = "".join(_question(key, csrf, q, draft if q["question_id"] == answered else "") for q in questions)
    return (f'<section class="attention" aria-labelledby="questions"><h2 id="questions">Needs your answer '
            f'<span class="count">{len(questions)}</span></h2>{cards}</section>')


def _message_card(key, csrf, draft, kind=None, closed=False) -> str:
    new = kind == "new"
    note = '<p class="muted small">No live session: the topic agent answers.</p>' if closed else ""
    return ('<section class="card" aria-labelledby="h-msg"><h2 id="h-msg">Message</h2>'
            f'<form method="post" action="{_topic_url(key)}/message">{_hidden(csrf)}'
            '<label for="msg">Message to this topic</label>'
            f'<textarea id="msg" name="text" rows="4">{_e(draft)}</textarea>'
            '<fieldset class="radios"><legend class="small">Send as</legend>'
            '<label for="kind-message"><input id="kind-message" type="radio" name="kind" value="message"'
            f'{"" if new else " checked"}> Message to the work</label>'
            '<label for="kind-new"><input id="kind-new" type="radio" name="kind" value="new"'
            f'{" checked" if new else ""}> New work from this topic</label></fieldset>'
            f'<div class="form-row"><button class="btn btn-primary" type="submit">Send</button></div></form>{note}'
            "</section>")


def _latest(key, view, names) -> str:
    latest = next((t for t in view.get("thread") or () if str(t["name"]).startswith("reply.")), None)
    if latest is None:
        return ""
    stamp = {"phase": "reply", "name": latest["name"].split(".", 1)[1]}
    return _section("h-latest", "Latest for you",
                    f'<div class="tl-head"><time class="muted">{_e(board.iso(latest["written_at"]))}</time>'
                    f'<span class="chip">{_e(latest["who"])}</span>{_artifact_link(key, stamp)}</div>'
                    f'<p class="prose">{_linked(key, latest["text"], names)}</p>')


def _priority_card(key, csrf) -> str:
    choice = _select("prio", "priority", "Set priority", [("", "Choose…"), *((p, p) for p in topic.PRIORITIES)], None)
    return ('<section class="card" aria-labelledby="h-prio"><h2 id="h-prio">Priority</h2>'
            f'<form class="inline-form" method="post" action="{_topic_url(key)}/message">{_hidden(csrf)}'
            f'<input type="hidden" name="kind" value="priority">{choice}'
            '<button class="btn" type="submit">Set</button></form></section>')


def _session(key, s, names) -> str:
    report = f'<span class="muted small">{_linked(key, s["report"], names)}</span>' if s["report"] else ""
    return (f'<li><span class="stack"><span>{_e(s["name"])} <span class="muted mono">'
            f'{_e(str(s["sid"] or "")[:8])}</span></span>{report}</span>'
            f'<span class="chip">{_e(s["liveness"])}</span></li>')


def _sessions_card(key, sessions, names) -> str:
    inner = (f'<ul class="list sessions">{"".join(_session(key, s, names) for s in sessions)}</ul>' if sessions
             else '<p class="muted">No sessions.</p>')
    return f'<section class="card" aria-labelledby="h-ses"><h2 id="h-ses">Sessions</h2>{inner}</section>'


def _gist(d) -> str:
    lines = (d["body"] or "").splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if line.strip() == "## answer"), 0)
    text = next((line.strip() for line in lines[start:] if line.strip()
                 and not (MD_HEAD.match(line) or META_LINE.match(line) or LABEL_LINE.match(line))), "")
    return _clip(text) if text else f"{d['phase']}.{d['name']}"


def _decision(key, d, names) -> str:
    who = d["decided_by"] or "?"
    return (f'<li><div class="tl-head"><time class="muted">{_e(board.iso(d["written_at"]))}</time>'
            f'{_artifact_link(key, d)}<span class="chip{" chip-accent" if who == "engineer" else ""}">{_e(who)}</span>'
            f'</div><details><summary>{_e(_gist(d))}</summary>'
            f'<pre class="mono">{_linked(key, d["body"] or "", names)}</pre></details></li>')


def _thread(key, view, names) -> str:
    items = []
    for t in view.get("thread") or ():
        tone = " chip-accent" if str(t["who"]).startswith("you") else ""
        stamp = {"phase": t["name"].split(".", 1)[0], "name": t["name"].split(".", 1)[1]}
        re_ = (f'<div class="muted small">re: <a href="{_artifact_url(key, t["re"])}">{_e(t["re"])}</a></div>'
               if t.get("re") else "")
        items.append(f'<li><div class="tl-head"><time class="muted">{_e(board.iso(t["written_at"]))}</time>'
                     f'<span class="chip{tone}">{_e(t["who"])}</span>{_artifact_link(key, stamp)}</div>'
                     f'{re_}<p class="prose">{_linked(key, t["text"], names)}</p></li>')
    inner = f'<ol class="timeline" id="thread">{"".join(items)}</ol>' if items else '<p class="muted" id="thread">No messages.</p>'
    return _section("h-thread", f'Thread <span class="count">{len(items)}</span>', inner)


def _history(key, view, names) -> str:
    summary = _linked(key, view["summary"], names) if view["summary"] is not None else "no summary"
    decisions = view["decisions"]
    timeline = (f'<ol class="timeline">{"".join(_decision(key, d, names) for d in decisions)}</ol>' if decisions
                else '<p class="muted">No decisions.</p>')
    rulings = f'<p class="muted">Domain rulings: {_e(view["rulings"])}</p>' if view["rulings"] else ""
    asks = "".join(f'<div class="ask-head mono">{_artifact_link(key, a)}</div>'
                   f'<p class="prose">{_linked(key, a["body"] or "", names)}</p>' for a in view["ask"])
    outputs = "".join(f'<li>{_artifact_link(key, a)}<span class="chip{" chip-ok" if a["status"] == "complete" else ""}">'
                      f'{_e(a["status"])}</span><span class="muted small">{_e(board.iso(a["written_at"]))}</span></li>'
                      for a in view["outputs"])
    scopes = [a["scope"] for a in view["kitchen"]]
    kitchen = (f"<details><summary>Kitchen: {len(scopes)} records ({scopes.count('checkpoint')} checkpoint, "
               f"{scopes.count('internal')} internal)</summary><ul class=\"list\">"
               + "".join(f'<li>{_artifact_link(key, a)}<span class="chip">{_e(a["scope"])}</span></li>'
                         for a in view["kitchen"]) + "</ul></details>")
    events = "".join(f'<tr><td class="muted">{_e(board.iso(e.get("ts")))}</td>'
                     f'<td>{_linked(key, board.event_label(e), names)}</td>'
                     f'<td class="muted">{_e(e.get("reason") or "")}</td></tr>' for e in view["events"])
    return (_section("h-sum", "Summary", f'<p class="prose">{summary}</p>')
            + _section("h-dec", f'Decisions <span class="count">{len(decisions)}</span>', timeline) + rulings
            + _section("h-ask", "Ask", asks or '<p class="muted">No ask.</p>')
            + _section("h-out", "Outputs", f'<ul class="list">{outputs}</ul>{kitchen}')
            + f'<details id="h-ev"><summary>Events (last {board.EVENTS_SHOWN})</summary>'
              f'{_table(("Time", "Event", "Detail"), events)}</details>')


def topic_page(view, csrf="", error=None, draft="", draft_for=None, kind=None, sent=None) -> str:
    h, key, names = view["header"], view["key"], view.get("names") or ()
    answered = draft_for if draft_for in {q["question_id"] for q in view["questions"]} else None
    domain = f"<span>{_e(h['domain'])}</span>" if h["domain"] else None
    crumbs = _crumbs("<span>Topics</span>", domain, f'<span aria-current="page">{_e(key)}</span>')
    probe = [f"probe failed: {', '.join(view['probe_errors'])}"] if view["probe_errors"] else []
    tokenless = h["kind"] in topic.TOKENLESS_KINDS
    live = topic_message._live_topic_worker(key) if h["waiting_on"] == "none" and not tokenless else None
    closed = h.get("status") == "closed" or (h["waiting_on"] == "none" and live is None and not tokenless)
    side = (f'<aside class="side" aria-label="Actions">{_priority_card(key, csrf)}'
            f'{_sessions_card(key, view["sessions"], names)}</aside>')
    main = (f"{_thread(key, view, names)}{_facts(h, view['questions'])}{_links(h)}"
            f'<div class="main-history">{_history(key, view, names)}</div>')
    body = (f"{crumbs}<h1>{_e(key)}</h1>{_sub(view)}<p class=\"state\">{_e(_state_words(h, h['kind'], live))}</p>"
            f"{_sent_banner(sent)}{_callout(probe, danger=True)}{_callout([error] if error else [], danger=True)}"
            f"{_latest(key, view, names)}{_questions(key, csrf, view['questions'], draft, answered)}"
            f'{_message_card(key, csrf, "" if answered else draft, kind, closed=closed)}'
            f'<div class="cols"><div class="main">{main}</div>{side}</div>')
    return _page(key, body, heading=False)


def new_page(csrf, error=None, draft="", domain=None) -> str:
    live = topic_agent.live_domains()
    options = "".join(f'<option value="{_e(d)}"></option>' for d in live)
    value = f' value="{_e(domain)}"' if domain else f' placeholder="{_e(paths.DEFAULT_DOMAIN)}"'
    shown = f" Live now: {_e(', '.join(live))}." if live else ""
    body = ('<div class="head"><div><h1>New work</h1><p class="sub">Starts an intake. '
            "The domain's manager sorts the ask into a work.</p></div></div>"
            f"{_callout([error] if error else [], danger=True)}"
            f'<div class="form-card"><form class="card" method="post" action="/new">{_hidden(csrf)}'
            '<div class="field"><label for="n-dom">Domain</label>'
            f'<input id="n-dom" name="domain" list="n-domains"{value}><datalist id="n-domains">{options}</datalist>'
            f'<p class="hint">The manager of this domain takes the work; empty means {_e(paths.DEFAULT_DOMAIN)}.'
            f"{shown}</p></div>"
            '<div class="field"><label for="n-text">What should happen</label>'
            f'<textarea id="n-text" name="text" rows="8">{_e(draft)}</textarea>'
            '<p class="hint">Plain words are enough. The intake decides the route.</p></div>'
            '<div class="form-row"><button class="btn btn-primary" type="submit">Start work</button></div>'
            "</form></div>")
    return _page("New work", body, heading=False)


def artifact_page(key, name, body, names=()) -> str:
    crumbs = _crumbs("<span>Topics</span>", _topic_link(key), f'<span aria-current="page">{_e(name)}</span>')
    shown = _linked(key, body, names) if body is not None else "unreadable"
    return _page(f"{key} · {name}", f'{crumbs}<h1>{_e(name)}</h1><pre class="mono">{shown}</pre>',
                 heading=False)


def _not_found() -> tuple[int, str]:
    return 404, _page("Not found", "<p>not found</p>")


def seen_cookie(path: str, cookie_header: str) -> tuple[dict, str | None]:
    prefix = f"{SEEN_COOKIE}="
    raw = next((part.strip()[len(prefix):] for part in str(cookie_header or "").split(";")
                if part.strip().startswith(prefix)), "")
    stamps = {}
    for pair in raw.split("|"):
        key, sep, stamp = pair.partition(":")
        if not sep or not topic.is_key(key):
            continue
        try:
            stamps[key] = int(stamp)
        except ValueError:
            continue
    seen = {k: ms_ / 1000 for k, ms_ in stamps.items()}
    segments = path.split("/")[1:]
    if len(segments) != 2 or segments[0] != "topic":
        return seen, None
    key = unquote(segments[1])
    if not topic.is_key(key):
        return seen, None
    stamps[key] = int(time.time() * 1000)
    kept = sorted(stamps.items(), key=lambda kv: kv[1])[-SEEN_MAX_PAIRS:]
    seen = {k: ms_ / 1000 for k, ms_ in kept}
    value = "|".join(f"{k}:{t}" for k, t in kept)
    return seen, f"{SEEN_COOKIE}={value}; Path=/; Max-Age={SEEN_MAX_AGE}; SameSite=Lax"


def route(target: str, csrf: str = "", seen: dict | None = None) -> tuple[int, str]:
    parts = urlsplit(target)
    query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
    paused = query.get("refresh") == "off"
    if parts.path == "/":
        domain = query.get("domain") or None
        return 200, inbox_page(board.inbox(domain, seen=seen), domain, paused=paused)
    if parts.path == "/new":
        return 200, new_page(csrf)
    if parts.path == "/topics":
        status = query.get("status") or None
        if status is not None and status not in topic.STATUSES:
            return 400, _page("Bad request", f"<p>unknown status {_e(status)}</p>")
        return 200, topics_page(board.topics(query.get("domain") or None, query.get("kind") or None, status), query,
                                paused=paused)
    segments = [unquote(s) for s in parts.path.split("/")[1:]]
    if len(segments) not in (2, 4) or segments[0] != "topic" or segments[1] not in board.keys():
        return _not_found()
    key = segments[1]
    if len(segments) == 2:
        sent = query.get("sent")
        return 200, topic_page(board.topic_view(key), csrf,
                               sent={"record": sent, "event": board.delivery_of(key, sent)} if sent else None)
    if segments[2] != "a":
        return _not_found()
    artifacts = ms._list_artifacts(key)
    found = next((a for a in artifacts if f"{a.get('phase')}.{a.get('name')}" == segments[3]), None)
    if found is None:
        return _not_found()
    names = [f"{a.get('phase')}.{a.get('name')}" for a in artifacts]
    return 200, artifact_page(key, segments[3], board.artifact_body(found), names)


def post_route(target: str, fields: dict, csrf: str) -> tuple[int, str, str | None]:
    segments = [unquote(s) for s in urlsplit(target).path.split("/")[1:]]
    text = fields.get("text", "")
    if segments == ["new"]:
        return _submit(lambda: asyncio.run(topic_message.deliver(
            None, text, kind="new", decided_by="engineer", via="board",
            domain=fields.get("domain") or paths.DEFAULT_DOMAIN)), None, fields, csrf)
    if len(segments) != 3 or segments[0] != "topic" or segments[1] not in board.keys():
        return (*_not_found(), None)
    key, action = segments[1], segments[2]
    if action == "answer":
        return _submit(lambda: asyncio.run(topic_message.deliver(
            key, text, kind="answer", decided_by="engineer", via="board",
            question_id=fields.get("question_id"))), key, fields, csrf)
    if action != "message":
        return (*_not_found(), None)
    kind = fields.get("kind") or "message"
    if kind == "new":
        domain = (topic.load(key) or {}).get("domain") or paths.DEFAULT_DOMAIN

        def start():
            topic_message._check(None, text, "new", "engineer", "board")
            return asyncio.run(topic_message.deliver(
                None, f"From topic {key}\n\n{text}", kind="new", decided_by="engineer", via="board", domain=domain))

        return _submit(start, key, fields, csrf)
    if kind not in ("message", "priority"):
        return 400, topic_page(board.topic_view(key), csrf, error=f"unknown kind {kind}", draft=text), None
    return _submit(lambda: asyncio.run(topic_message.deliver(
        key, text, kind=kind, decided_by="engineer", via="board",
        priority=fields.get("priority") or None)), key, fields, csrf)


def _submit(write, key, fields, csrf) -> tuple[int, str, str | None]:
    try:
        result = write()
    except ValueError as err:
        draft = fields.get("text", "")
        page = (topic_page(board.topic_view(key), csrf, error=str(err), draft=draft,
                           draft_for=fields.get("question_id"), kind=fields.get("kind")) if key
                else new_page(csrf, error=str(err), draft=draft, domain=fields.get("domain")))
        return 400, page, None
    kind = fields.get("kind")
    if kind == "priority":
        return 303, "", _topic_url(result.get("task_key") or key)
    if kind == "new" or key is None:
        return 303, "", _topic_url(result["task_key"]) + "?sent=context.ask"
    return 303, "", _topic_url(result.get("task_key") or key) + "?sent=" + quote(str(result.get("record")), safe="")


def _forbidden(reason: str) -> tuple[int, str, None]:
    return 403, _page("Forbidden", f"<p>{_e(reason)}</p>"), None


def _bad_request(reason: str) -> tuple[int, str, None]:
    return 400, _page("Bad request", f"<p>{_e(reason)}</p>"), None


class Handler(BaseHTTPRequestHandler):
    server_version = "dockwright-board"

    def do_GET(self):
        self._respond(send_body=True)

    def do_HEAD(self):
        self._respond(send_body=False)

    def do_POST(self):
        code, text, location = self._post()
        self._send(code, text, location, send_body=True)

    def log_message(self, format, *args):
        pass

    def send_response(self, code, message=None):
        super().send_response(code, message)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")

    def _local(self) -> bool:
        port = self.server.server_address[1]
        return (self.client_address[0] == LOOPBACK
                and self.headers.get("Host") in (f"{LOOPBACK}:{port}", f"localhost:{port}"))

    def _respond(self, send_body):
        set_cookie = None
        if not self._local():
            code, text = 403, _page("Forbidden", "<p>This board answers only its own local address.</p>")
        else:
            try:
                seen, set_cookie = seen_cookie(urlsplit(self.path).path, self.headers.get("Cookie") or "")
                code, text = route(self.path, self.server.csrf, seen=seen)
            except Exception as err:
                code, text = 500, _page("Error", f"<p>render failed: {_e(type(err).__name__)}</p>")
        self._send(code, text, None, send_body, set_cookie=set_cookie)

    def _post(self) -> tuple[int, str, str | None]:
        if not self._local():
            return _forbidden("This board answers only its own local address.")
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{self.headers.get('Host')}":
            return _forbidden("This board takes forms only from its own pages.")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != FORM_TYPE:
            return _bad_request("forms only")
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            return _bad_request("no length")
        if length < 0 or length > FORM_MAX_BYTES:
            return _bad_request("the form is too large")
        data = self.rfile.read(length)
        if len(data) != length:
            return _bad_request("the form was cut short")
        try:
            raw = data.decode("utf-8")
        except UnicodeDecodeError:
            return _bad_request("not UTF-8")
        fields = {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}
        if not secrets.compare_digest(fields.get("csrf", "").encode("utf-8"), self.server.csrf.encode("utf-8")):
            return _forbidden("This form is not from this board's pages. "
                              "Go back, copy your text, reload that page and send it again.")
        try:
            return post_route(self.path, fields, self.server.csrf)
        except Exception as err:
            return 500, _page("Error", f"<p>write failed: {_e(type(err).__name__)}</p>"), None

    def _send(self, code, text, location, send_body, set_cookie=None):
        data = text.encode("utf-8")
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if send_body:
            self.wfile.write(data)


def make_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((LOOPBACK, port), Handler)
    server.csrf = secrets.token_urlsafe(32)
    return server


def _answers(port: int) -> bool:
    try:
        with socket.create_connection((LOOPBACK, port), timeout=PROBE_SEC):
            return True
    except OSError:
        return False


def serve(port: int = board.DEFAULT_PORT) -> int:
    try:
        server = None if _answers(port) else make_server(port)
    except (OSError, OverflowError):
        server = None
    if server is None:
        print(f"port {port} is busy or unavailable", file=sys.stderr)
        return 1
    print(f"board: http://{LOOPBACK}:{server.server_address[1]}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
