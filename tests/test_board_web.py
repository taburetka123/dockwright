import http.client
import re
import socket
import threading
import time
from html.parser import HTMLParser
from urllib.parse import urlencode

import pytest

from dockwright import board, board_web, config, paths, state, topic_message
from dockwright import mcp_server as ms
from dockwright import notebook_migrate as nm
from tests.test_board import GENERAL, RENEW, _question, _scene, _tree, fresh_orchestrator_dir

ROUTES = ("/", "/?domain=general", "/topics", "/topics?kind=note", "/topics?domain=product&kind=note&status=open",
          "/topic/W1", "/topic/OUT-1", "/topic/note-general-renew-the-cert", "/topic/W1/a/summary.thread", "/new")
FORM = "application/x-www-form-urlencoded"


@pytest.fixture
def server(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _scene(tmp_path, monkeypatch)
    ms.artifact_put_impl("W1", "summary", "thread", "<script>alert(1)</script>", "complete", "sid-q", scope="output")
    srv = board_web.make_server(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _request(srv, path, **kwargs):
    return _exchange(srv, path, **kwargs)[:2]


def _exchange(srv, path, *, method="GET", host=None, body: bytes | None = None, content_type: str | None = None,
              origin: str | None = None, length: int | None = None):
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host or f"127.0.0.1:{port}")
        if content_type is not None:
            conn.putheader("Content-Type", content_type)
        if origin is not None:
            conn.putheader("Origin", origin)
        if length is not None:
            conn.putheader("Content-Length", str(length))
        elif body is not None or method == "POST":
            conn.putheader("Content-Length", str(len(body or b"")))
        conn.endheaders(body)
        response = conn.getresponse()
        return response.status, response.read().decode(), response.headers
    finally:
        conn.close()


def _post(srv, path, fields, **kwargs):
    fields = {"csrf": srv.csrf, **fields}
    return _request(srv, path, method="POST", body=urlencode(fields).encode(), content_type=FORM, **kwargs)


class _Doc(HTMLParser):
    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.controls, self.label_fors, self.tables, self.links, self.forms = [], set(), [], [], []
        self._form = self._select = self._textarea = self._link = None
        self.feed(page)
        self.close()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "label" and a.get("for"):
            self.label_fors.add(a["for"])
        elif tag == "table":
            self.tables.append([])
        elif tag == "th" and self.tables:
            self.tables[-1].append(a.get("scope"))
        elif tag == "a":
            self._link = [a.get("href"), ""]
            self.links.append(self._link)
        elif tag == "form":
            self._form = {"action": a.get("action"), "method": a.get("method"), "fields": {}, "radios": {},
                          "options": {}}
            self.forms.append(self._form)
        elif tag == "option" and self._select is not None:
            self._form["options"][self._select].append(a.get("value"))
            if self._form["fields"][self._select] is None or "selected" in a:
                self._form["fields"][self._select] = a.get("value")
        if tag in ("input", "select", "textarea"):
            if a.get("type") != "hidden":
                self.controls.append(a)
            if self._form is not None and a.get("name"):
                self._field(tag, a)

    def _field(self, tag, a):
        name, fields = a["name"], self._form["fields"]
        if tag == "select":
            self._select, fields[name] = name, None
            self._form["options"][name] = []
        elif tag == "textarea":
            self._textarea, fields[name] = name, ""
        elif a.get("type") == "radio":
            self._form["radios"].setdefault(name, []).append(a.get("value"))
            if "checked" in a:
                fields[name] = a.get("value")
        else:
            fields[name] = a.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "select":
            self._select = None
        elif tag == "textarea":
            self._textarea = None
        elif tag == "a":
            self._link = None

    def handle_data(self, data):
        if self._textarea is not None and self._form is not None:
            self._form["fields"][self._textarea] += data
        if self._link is not None:
            self._link[1] += data


@pytest.fixture
def delivered(monkeypatch):
    calls = []

    async def fake(task_key, text, **kwargs):
        calls.append((task_key, text, kwargs))
        return {"ok": True, "task_key": task_key or "in-20260930T101010-abcd", "record": "decision.msg-1",
                "route": "pane", "delivered": True, "reason": None}

    monkeypatch.setattr(topic_message, "deliver", fake)
    return calls


def test_every_route_answers_and_writes_nothing(server, fresh_orchestrator_dir):
    before = _tree(fresh_orchestrator_dir)
    for path in ROUTES:
        assert _request(server, path)[0] == 200, path
    assert _tree(fresh_orchestrator_dir) == before
    assert paths.artifact_ticket_dir("OLD-1").is_dir()


def test_the_pages_carry_the_screens(server):
    inbox = _request(server, "/")[1]
    assert "Waiting on you" in inbox and 'href="/topic/W2"' in inbox and 'http-equiv="refresh"' in inbox
    assert "Which schema?" in inbox and "Parked at the design gate." in inbox
    topic_page = _request(server, "/topic/W1")[1]
    lists = topic_page.split('id="h-out"')[1].split('id="h-ev"')[0]
    assert "<details>" in lists and 'href="/topic/W1/a/summary.thread"' not in lists
    assert "http-equiv" not in topic_page
    notes = _request(server, "/topics?domain=product&kind=note&status=open")[1]
    assert "not migrated" in notes and "Ship pricing" in notes


LABELLED = ("/", "/topics", "/topics?domain=product&kind=note&status=open", "/topic/W1",
            "/topic/note-general-renew-the-cert", "/new")
TABLED = ("/topics", "/topics?domain=product&kind=note&status=open", "/topic/W1")


def test_every_page_starts_with_a_skip_link_to_main(server):
    for path in ROUTES:
        page = _request(server, path)[1]
        assert _Doc(page).links[0] == ["#main", "Skip to content"] and 'id="main"' in page, path


def test_the_header_marks_the_current_page(server):
    inbox, topics = _request(server, "/")[1], _request(server, "/topics")[1]
    assert '<a href="/" aria-current="page">Inbox</a>' in inbox and '<a href="/topics">Topics</a>' in inbox
    assert '<a href="/topics" aria-current="page">Topics</a>' in topics and '<a href="/">Inbox</a>' in topics


def test_every_visible_field_has_a_label(server):
    for path in LABELLED:
        doc = _Doc(_request(server, path)[1])
        assert doc.controls, path
        assert [c for c in doc.controls if c.get("id") not in doc.label_fors] == [], path


def test_every_table_has_column_headers(server):
    for path in TABLED:
        tables = _Doc(_request(server, path)[1]).tables
        assert tables and all(t and all(scope == "col" for scope in t) for t in tables), path


def test_pausing_stops_the_reload_until_the_engineer_navigates(server):
    live = _request(server, "/?domain=general")[1]
    assert 'http-equiv="refresh"' in live and 'href="/?domain=general&amp;refresh=off"' in live
    paused = _request(server, "/?domain=general&refresh=off")[1]
    resume = {text: href for href, text in _Doc(paused).links}["Resume"]
    assert "http-equiv" not in paused and resume == "/?domain=general"
    assert '<input type="hidden" name="refresh" value="off">' in paused
    topics = _request(server, "/topics?kind=note&refresh=off")[1]
    assert "http-equiv" not in topics and {t: h for h, t in _Doc(topics).links}["Resume"] == "/topics?kind=note"
    assert '<input type="hidden" name="refresh" value="off">' in topics
    assert 'http-equiv="refresh"' in _request(server, "/?refresh=on")[1]


def test_the_domain_filter_lists_the_known_domains(server):
    domains = board.domains()
    filters = next(f for f in _Doc(_request(server, "/")[1]).forms if f["action"] == "/")
    assert domains and filters["options"]["domain"] == ["", *domains]


def test_a_gate_row_opens_its_link_in_a_new_tab_and_a_script_link_links_nowhere(fresh_orchestrator_dir):
    item = {"priority": "P1", "kind": "gate", "topic": "K", "key": "K", "domain": None, "anchor": 0.0, "text": "",
            "note": ""}
    data = {"now": 60.0, "warnings": [], "waiting": [{**item, "link": "https://example.test/gate"}], "stopped": []}
    page = board_web.inbox_page(data)
    assert '<a href="https://example.test/gate" class="item-main" target="_blank" rel="noopener">' in page
    assert "(opens in a new tab)" in page and "Open gate" in page
    data["waiting"] = [{**item, "link": "javascript:alert(1)"}]
    page = board_web.inbox_page(data)
    assert 'href="javascript' not in page and '<a href="/topic/K" class="item-main">' in page


def test_a_card_with_a_non_string_kind_or_status_still_lists(fresh_orchestrator_dir):
    row = {"key": "K", "error": None, "kind": 5, "status": "open", "waiting_on": "none", "title": "",
           "priority_shown": "P2", "domain": None, "activity": None, "last_event": None}
    rows = [row, {**row, "key": "N", "kind": "note", "status": True}]
    page = board_web.topics_page({"now": 60.0, "warnings": [], "rows": rows, "notes": None, "notebook": None}, {})
    assert '<a href="/topic/K" class="key">K</a>' in page and '<span class="chip">True</span>' in page


def test_only_its_own_host_names_are_served(server):
    port = server.server_address[1]
    assert _request(server, "/", host=f"localhost:{port}")[0] == 200
    assert _request(server, "/", host=f"evil.example:{port}")[0] == 403
    assert _request(server, "/", host="127.0.0.1")[0] == 403


def test_head_is_served_and_a_post_without_the_page_secret_is_refused(server, fresh_orchestrator_dir):
    assert _request(server, "/", method="HEAD") == (200, "")
    before = _tree(fresh_orchestrator_dir)
    body = urlencode({"text": "hi"}).encode()
    assert _request(server, "/topic/W1/message", method="POST", body=body, content_type=FORM)[0] == 403
    assert _tree(fresh_orchestrator_dir) == before


@pytest.mark.parametrize("kwargs, code", [
    ({"origin": "http://evil.example"}, 403),
    ({"host": "evil.example:1"}, 403),
])
def test_a_foreign_form_is_refused_and_writes_nothing(server, fresh_orchestrator_dir, delivered, kwargs, code):
    before = _tree(fresh_orchestrator_dir)
    assert _post(server, "/topic/W1/message", {"text": "hi"}, **kwargs)[0] == code
    assert delivered == [] and _tree(fresh_orchestrator_dir) == before


def test_a_wrong_type_or_an_oversized_body_is_refused(server, fresh_orchestrator_dir, delivered):
    before = _tree(fresh_orchestrator_dir)
    body = urlencode({"csrf": server.csrf, "text": "hi"}).encode()
    assert _request(server, "/topic/W1/message", method="POST", body=body, content_type="application/json")[0] == 400
    assert _request(server, "/topic/W1/message", method="POST", body=b"", content_type=FORM,
                    length=64 * 1024 + 1)[0] == 400
    assert delivered == [] and _tree(fresh_orchestrator_dir) == before


def test_a_message_from_the_page_is_the_engineers_and_redirects(server, delivered):
    port = server.server_address[1]
    status, _, headers = _exchange(server, "/topic/W1/message", method="POST",
                                   body=urlencode({"csrf": server.csrf, "kind": "message", "text": "hold on"}).encode(),
                                   content_type=FORM, origin=f"http://127.0.0.1:{port}")
    assert status == 303 and headers["Location"] == "/topic/W1?sent=decision.msg-1"
    assert delivered == [("W1", "hold on", {"kind": "message", "decided_by": "engineer", "via": "board",
                                            "priority": None})]


def test_new_work_from_a_topic_names_the_topic(server, delivered):
    assert _post(server, "/topic/W1/message", {"kind": "new", "text": "follow up"})[0] == 303
    task_key, text, kwargs = delivered[0]
    assert task_key is None and text == "From topic W1\n\nfollow up" and kwargs["kind"] == "new"


def test_a_blank_new_work_text_from_a_topic_starts_nothing(server, delivered):
    status, page = _post(server, "/topic/W1/message", {"kind": "new", "text": "   "})
    assert status == 400 and "the text is empty" in page and ">   </textarea>" in page
    assert delivered == []


def test_new_work_from_the_new_page_goes_to_the_sorters_topic(server, delivered):
    assert board_web.post_route("/new", {"domain": "product", "text": "look into X"}, server.csrf) == (
        303, "", "/topic/in-20260930T101010-abcd?sent=context.ask")
    assert delivered == [(None, "look into X", {"kind": "new", "decided_by": "engineer", "via": "board",
                                                "domain": "product"})]
    assert _post(server, "/new", {"domain": "", "text": "look into Y"})[0] == 303
    assert delivered[-1][2]["domain"] == paths.DEFAULT_DOMAIN


def test_a_failed_write_shows_the_error_and_keeps_the_draft(server, monkeypatch):
    async def refuse(task_key, text, **kwargs):
        raise ValueError("topic_message: the text is empty")

    monkeypatch.setattr(topic_message, "deliver", refuse)
    status, page = _post(server, "/topic/W1/message", {"kind": "message", "text": "  draft  "})
    assert status == 400 and "the text is empty" in page and "  draft  " in page
    status, page = _post(server, "/new", {"domain": "general", "text": "  new draft  "})
    assert status == 400 and "the text is empty" in page and "  new draft  " in page


def test_an_unknown_kind_or_target_writes_nothing(server, fresh_orchestrator_dir, delivered):
    before = _tree(fresh_orchestrator_dir)
    status, page = _post(server, "/topic/W1/message", {"kind": "bogus", "text": "kept"})
    assert status == 400 and "unknown kind bogus" in page and "kept" in page
    for path in ("/topic/NOPE/message", "/topic/W1/nope", "/topic/W1", "/nope"):
        assert _post(server, path, {"text": "hi"})[0] == 404, path
    assert delivered == [] and _tree(fresh_orchestrator_dir) == before


def test_the_topic_page_carries_the_forms_and_its_open_questions(server):
    _, page = _request(server, "/topic/W1")
    assert 'action="/topic/W1/message"' in page and f'value="{server.csrf}"' in page
    assert 'action="/topic/W1/answer"' in page and "http-equiv" not in page
    assert "Which schema?" in page and 'value="q1"' in page and "subworker asks" not in page
    _, page = _request(server, "/new")
    assert 'action="/new"' in page and f'value="{server.csrf}"' in page and "http-equiv" not in page
    inbox = _request(server, "/")[1]
    assert 'href="/new"' in inbox and "<form method=\"post\"" not in inbox


def _form(page, action, n=0):
    return page.split(f'action="{action}"')[1 + n].split("</form>")[0]


def test_a_failed_answer_keeps_the_draft_in_its_question(server):
    draft = "x" * (16 * 1024 + 1)
    status, page = _post(server, "/topic/W1/answer", {"question_id": "q1", "text": draft})
    assert status == 400 and "over 16384 bytes" in page
    assert draft in _form(page, "/topic/W1/answer") and draft not in _form(page, "/topic/W1/message")
    status, page = _post(server, "/topic/W1/answer", {"question_id": "gone", "text": "late answer"})
    assert status == 400 and "no open question gone" in page
    assert "late answer" in _form(page, "/topic/W1/message")


def test_the_priority_form_carries_no_text_and_the_message_form_no_priority(server):
    _, page = _request(server, "/topic/W1")
    message, priority = _form(page, "/topic/W1/message"), _form(page, "/topic/W1/message", 1)
    assert "<textarea" in message and 'name="priority"' not in message and 'value="priority"' not in message
    assert "<textarea" not in priority and '<input type="hidden" name="kind" value="priority">' in priority
    assert "selected" not in priority


def test_a_priority_click_without_a_choice_changes_nothing(server, fresh_orchestrator_dir):
    before = _tree(fresh_orchestrator_dir)
    status, page = _post(server, "/topic/W1/message", {"kind": "priority", "priority": ""})
    assert status == 400 and "needs priority" in page
    assert _tree(fresh_orchestrator_dir) == before


def _rendered(srv, path, action, n=0):
    return [f for f in _Doc(_request(srv, path)[1]).forms if f["action"] == action][n]


def _filled(form, **values):
    missing = sorted(set(values) - set(form["fields"]))
    assert not missing, f"{form['action']} renders no field {missing}"
    return {**form["fields"], **values}


def _send(srv, action, fields):
    return _request(srv, action, method="POST", body=urlencode(fields).encode(), content_type=FORM)[0]


def test_the_rendered_answer_form_answers_the_question(server):
    form = _rendered(server, "/topic/W1", "/topic/W1/answer")
    assert _send(server, "/topic/W1/answer", _filled(form, text="use general-work")) == 303
    record = ms.artifact_get_impl("W1", "decision", "q-q1")
    assert record["decided_by"] == "engineer" and "use general-work" in record["content"]


def test_the_rendered_message_form_sends_a_message_by_default(server, delivered):
    form = _rendered(server, "/topic/W1", "/topic/W1/message")
    assert form["fields"]["kind"] == "message" and form["radios"]["kind"] == ["message", "new"]
    assert _send(server, "/topic/W1/message", _filled(form, text="hold on")) == 303
    assert delivered == [("W1", "hold on", {"kind": "message", "decided_by": "engineer", "via": "board",
                                            "priority": None})]


def test_the_rendered_message_form_starts_new_work_that_names_the_topic(server, delivered):
    form = _rendered(server, "/topic/W1", "/topic/W1/message")
    assert _send(server, "/topic/W1/message", _filled(form, kind="new", text="follow up")) == 303
    task_key, text, kwargs = delivered[0]
    assert task_key is None and text == "From topic W1\n\nfollow up" and kwargs["kind"] == "new"


def test_the_rendered_priority_form_sets_the_priority(server, delivered):
    form = _rendered(server, "/topic/W1", "/topic/W1/message", 1)
    assert form["options"]["priority"] == ["", "P0", "P1", "P2", "P3"] and "text" not in form["fields"]
    assert _send(server, "/topic/W1/message", _filled(form, priority="P1")) == 303
    assert delivered == [("W1", "", {"kind": "priority", "decided_by": "engineer", "via": "board",
                                     "priority": "P1"})]


def test_the_rendered_new_form_starts_work_in_the_domain_typed(server, delivered):
    form = _rendered(server, "/new", "/new")
    assert form["fields"]["domain"] == ""
    assert _send(server, "/new", _filled(form, domain="product", text="look into X")) == 303
    assert delivered == [(None, "look into X", {"kind": "new", "decided_by": "engineer", "via": "board",
                                                "domain": "product"})]
    assert _send(server, "/new", _filled(form, text="look into Y")) == 303
    assert delivered[-1][2]["domain"] == paths.DEFAULT_DOMAIN


def test_the_topic_page_puts_open_questions_before_the_actions_and_the_history(server):
    page = _request(server, "/topic/W1")[1]
    assert page.index('id="questions"') < page.index('action="/topic/W1/message"') < page.index('id="h-sum"')
    assert 'id="q-q1"' in page and '<label for="answer-q1">Your answer</label>' in page


def test_a_failed_send_keeps_the_send_as_choice(server, monkeypatch):
    async def refuse(task_key, text, **kwargs):
        raise ValueError("no live manager for domain general; nothing was written")

    monkeypatch.setattr(topic_message, "deliver", refuse)
    for kind in ("new", "message"):
        status, page = _post(server, "/topic/W1/message", {"kind": kind, "text": "follow up"})
        form = [f for f in _Doc(page).forms if f["action"] == "/topic/W1/message"][0]
        assert status == 400 and form["fields"]["kind"] == kind and form["fields"]["text"] == "follow up", kind


def test_a_hash_that_starts_no_heading_stays_in_the_text(server):
    view = board.topic_view("W1")
    view["ask"] = [{"phase": "context", "name": "ask", "body": "#123 regression in X"}]
    view["decisions"] = [{"phase": "decision", "name": "q-abc", "decided_by": "engineer", "written_at": 0,
                          "body": "# decision abc\n\n## question\nMerged?\n\n## answer\n#4012 is merged, go\n\n"
                                  "asked_by: w (s)"}]
    page = board_web.topic_page(view)
    assert '<p class="sub">#123 regression in X</p>' in page and "<summary>#4012 is merged, go</summary>" in page


def test_a_topic_subtitle_drops_the_markdown_heading_and_checkbox(server):
    assert '<p class="sub">Renew the cert</p>' in _request(server, "/topic/note-general-renew-the-cert")[1]
    view = board.topic_view("W1")
    view["ask"] = [{"phase": "context", "name": "ask", "body": "# Manager notebook — general\n\nbody"}]
    assert '<p class="sub">Manager notebook — general</p>' in board_web.topic_page(view)
    view["ask"][0]["body"] = "## [x] " + "y" * 200
    assert f'<p class="sub">{"y" * 159}…</p>' in board_web.topic_page(view)


def test_a_decision_summary_shows_the_answer_the_answer_form_wrote(server):
    form = _rendered(server, "/topic/W1", "/topic/W1/answer")
    assert _send(server, "/topic/W1/answer", _filled(form, text="use general-work")) == 303
    page = _request(server, "/topic/W1")[1]
    assert '<p class="prose">use general-work</p>' in _section_of(page, "h-thread")
    assert "use general-work" not in _section_of(page, "h-dec")


def test_a_decision_summary_skips_headings_metadata_and_labels(server):
    view = board.topic_view("W1")
    bodies = {
        "msg-1": topic_message.body("reopen", "manager", "cli"),
        "pane-1": ("from: manager\nnode: design\ntime: 2026-09-28T09:20:13Z\ncompletes or replaces: none\n\n"
                   "Verbatim:\n[MANAGER] Reopen the design gate."),
        "pane-2": ("# decision.pane-2\n\n- from: engineer\n- node: pr (index 1)\n\n## Directive, verbatim\n"
                   "1. The amendment:\n«flip it»"),
        "task-1": ("from: manager\nnode: plan\ncompletes/replaces: none\n\nQuestion: silent or loud?\n\n"
                   "Verbatim answer:\n(A)"),
        "q-1": "# decision q1\n\n## question\nWhich?\n\n## answer\n" + "y" * 200 + "\n\nasked_by: w (sid)\nnode: -",
        "pane-3": "from: manager\nnode: plan\n\nVerbatim:\n",
        "q-2": "## answer\nAll four approved as stated. Two conditions:\n(1) keep the step",
    }
    view["decisions"] = [{"phase": "decision", "name": name, "written_at": None, "decided_by": "manager",
                          "body": body} for name, body in bodies.items()]
    page = board_web.topic_page(view)
    for gist in ("reopen", "[MANAGER] Reopen the design gate.", "«flip it»", "Question: silent or loud?",
                 f"{'y' * 159}…", "decision.pane-3", "All four approved as stated. Two conditions:"):
        assert f"<summary>{gist}</summary>" in page, gist


def test_an_external_link_opens_in_a_new_tab(server):
    view = board.topic_view("W1")
    view["header"]["links"] = {"pr": "https://example.test/pull/1", "ticket": None, "worktree": "/tmp/w"}
    view["header"]["prs"] = ["https://example.test/pull/1"]
    page = board_web.topic_page(view)
    assert '<a href="https://example.test/pull/1" target="_blank" rel="noopener">' in page
    assert "(opens in a new tab)" in page and '<code class="mono">/tmp/w</code>' in page


def test_a_body_cut_short_is_refused(server, fresh_orchestrator_dir, delivered):
    before = _tree(fresh_orchestrator_dir)
    port = server.server_address[1]
    body = urlencode({"csrf": server.csrf, "kind": "message", "text": "do NOT merge"}).encode()
    head = (f"POST /topic/W1/message HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: {FORM}\r\n"
            f"Content-Length: {len(body)}\r\n\r\n").encode()
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(head + body[:body.index(b"merge")])
        sock.shutdown(socket.SHUT_WR)
        status_line = sock.makefile("rb").readline()
    assert status_line.split()[1] == b"400"
    assert delivered == [] and _tree(fresh_orchestrator_dir) == before


def test_an_answer_from_the_page_unblocks_the_worker(server):
    status, _ = _post(server, "/topic/W1/answer", {"question_id": "q1", "text": "use general-work"})
    assert status == 303
    assert (paths.ANSWERS / "q1.json").exists()
    record = ms.artifact_get_impl("W1", "decision", "q-q1")
    assert record["decided_by"] == "engineer" and "use general-work" in record["content"]
    assert [q["question_id"] for q in board.topic_view("W1")["questions"]] == []


def test_no_response_may_be_framed(server, delivered):
    form = urlencode({"csrf": server.csrf, "kind": "message", "text": "hi"}).encode()
    exchanges = [_exchange(server, path) for path in ("/new", "/topic/W1", "/nope")]
    exchanges.append(_exchange(server, "/topic/W1/message", method="POST", body=form, content_type=FORM))
    exchanges.append(_exchange(server, "/topic/W1/message", method="POST", body=b"", content_type=FORM))
    exchanges.append(_exchange(server, "/", method="PUT"))
    assert [status for status, _, _ in exchanges] == [200, 200, 404, 303, 403, 501]
    for _, _, headers in exchanges:
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Content-Security-Policy"] == "frame-ancestors 'none'"


def test_unknown_topics_artifacts_and_dot_segments_are_404(server):
    for path in ("/nope", "/topic/NOPE", "/topic/W1/a/nope.nope", "/topic/W1/x/summary.thread", "/topic/%2e%2e",
                 "/topic/%2e%2e/a/context.ask", "/topic/..%2fW1", "/topic/W1/"):
        assert _request(server, path)[0] == 404, path
    assert _request(server, "/topics?status=bogus")[0] == 400


def test_bodies_are_escaped_and_only_http_links_link(server):
    for path in ("/topic/W1", "/topic/W1/a/summary.thread"):
        body = _request(server, path)[1]
        assert "<script>alert(1)" not in body and "&lt;script&gt;alert(1)" in body
    view = board.topic_view("W1")
    view["header"]["links"] = {"pr": "javascript:alert(1)", "ticket": "https://example.test/T-1"}
    page = board_web.topic_page(view)
    assert 'href="javascript' not in page and 'href="https://example.test/T-1"' in page


def test_a_render_failure_is_a_500_and_the_server_lives(server, monkeypatch):
    def broken(domain=None, seen=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(board, "inbox", broken)
    status, body = _request(server, "/")
    assert status == 500 and "RuntimeError" in body and "boom" not in body
    assert _request(server, "/topics")[0] == 200


def test_a_busy_port_exits_1(capsys):
    taken = board_web.make_server(0)
    try:
        assert board_web.serve(taken.server_address[1]) == 1
    finally:
        taken.server_close()
    assert "is busy or unavailable" in capsys.readouterr().err


def test_a_port_another_server_holds_on_every_address_exits_1(capsys, monkeypatch):
    monkeypatch.setattr(board_web.ThreadingHTTPServer, "serve_forever", lambda self, poll_interval=0.5: None)
    other = socket.socket()
    other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    other.bind(("0.0.0.0", 0))
    other.listen()
    try:
        assert board_web.serve(other.getsockname()[1]) == 1
    finally:
        other.close()
    assert "is busy or unavailable" in capsys.readouterr().err


def test_a_port_out_of_range_exits_1(capsys):
    for port in (70000, -1):
        assert board_web.serve(port) == 1
        assert f"port {port} is busy or unavailable" in capsys.readouterr().err


def test_a_peer_off_the_loopback_is_refused(server, monkeypatch):
    accept = server.get_request
    monkeypatch.setattr(server, "get_request", lambda: (accept()[0], ("192.0.2.7", 40000)))
    assert _request(server, "/")[0] == 403


def test_the_cli_serves_on_the_given_port(monkeypatch):
    seen = []
    monkeypatch.setattr(board_web, "serve", lambda port: seen.append(port) or 0)
    assert board.main(["--serve", "--port", "9123"]) == 0 and seen == [9123]
    assert board.main(["--serve"]) == 0 and seen[-1] == board.DEFAULT_PORT



def _row(page, marker):
    return next(r for r in page.split("<tr>")[1:] if marker in r).split("</tr>")[0]


def _cells(page, marker):
    return [c for c in re.findall(r"<td[^>]*>(.*?)</td>", _row(page, marker)) if c]


def _item(page, *markers):
    items = (i.split("</li>")[0] for i in page.split('<li class="item">')[1:])
    return next(i for i in items if all(m in i for m in markers))


def _section_of(page, ident):
    return page.split(f'id="{ident}"')[1].split("</section>")[0]


def test_a_question_without_artifacts_opens_and_answers_from_the_board(server, fresh_orchestrator_dir):
    _question("q6", "sid-o", "Ship it?", 195.0)
    before = _tree(fresh_orchestrator_dir)
    status, page = _request(server, "/topic/OUT-1")
    assert status == 200 and 'id="q-q6"' in page and 'action="/topic/OUT-1/answer"' in page
    inbox = _request(server, "/")[1]
    topic_link, action = _Doc(_item(inbox, "Ship it?")).links
    assert topic_link == ["/topic/OUT-1", "OUT-1"]
    assert action[0] == "/topic/OUT-1#q-q6" and "Ship it?" in action[1] and "Answer" in action[1]
    assert _tree(fresh_orchestrator_dir) == before
    assert _post(server, "/topic/OUT-1/answer", {"question_id": "q6", "text": "ship"})[0] == 303
    assert (paths.ANSWERS / "q6.json").exists()
    record = ms.artifact_get_impl("OUT-1", "decision", "q-q6")
    assert record["decided_by"] == "engineer" and "ship" in record["content"]


def test_a_late_answer_on_a_key_without_artifacts_keeps_the_draft(server):
    status, page = _post(server, "/topic/OUT-1/answer", {"question_id": "gone", "text": "late answer"})
    assert status == 400 and "no open question gone" in page
    assert "late answer" in _form(page, "/topic/OUT-1/message")


def test_every_inbox_row_leads_to_its_action(server):
    inbox = _request(server, "/")[1]
    topic_link, action = _Doc(_item(inbox, "Which schema?")).links
    assert topic_link == ["/topic/W1", "W1"] and action[0] == "/topic/W1#q-q1" and "Answer" in action[1]
    assert 'id="q-q1"' in _request(server, "/topic/W1")[1]
    gate = _item(inbox, "reopen:", ">OUT-1<")
    assert '<p class="item-hint">reopen: <code class="mono">plannotator sessions --open 1</code></p>' in gate
    assert {href for href, _ in _Doc(gate).links} == {"/topic/OUT-1"}
    assert {href for href, _ in _Doc(_item(inbox, "Parked at the design gate.")).links} == {"/topic/W3"}
    assert "product notebook" not in inbox and "Open notes" not in inbox
    assert "href" not in _item(inbox, "Is the rebase safe?")


def test_every_topics_cell_leads_to_the_topic_and_only_the_key_takes_a_tab_stop(server):
    page = _request(server, "/topics")[1]
    cells = _cells(page, '<a href="/topic/W1" class="key">W1</a>')
    assert len(cells) == 6 and all('href="/topic/W1"' in c for c in cells)
    assert all('tabindex="-1"' in c for c in cells[1:]) and cells[0].startswith('<a href="/topic/W1" class="key">')
    notes = _request(server, "/topics?domain=general&kind=note")[1]
    cells = _cells(notes, f'<a href="/topic/{RENEW}" class="key">')
    assert len(cells) == 4 and all(f'href="/topic/{RENEW}"' in c for c in cells)


def test_no_html_screen_shows_a_notebook_warning(server):
    nm.notebook_path("general").write_text(GENERAL.replace("- when: later", "- when: much later"))
    for path in ("/", "/topics", "/topics?domain=general&kind=note"):
        status, page = _request(server, path)
        assert status == 200 and "notebook-migrate" not in page and "⚠ notebook" not in page
    assert "⚠ notebook general changed after migration" in board.render_inbox(board.inbox())


def test_the_topic_header_links_its_lists_waits_and_blockers(server):
    note = _Doc(_request(server, f"/topic/{RENEW}")[1]).links
    assert ["/topics?kind=note", "note"] in note and ["/topics?domain=general", "domain: general"] in note
    page = _request(server, "/topic/W1")[1]
    assert ["#questions", "waiting on you: question"] in _Doc(page).links
    assert '<h2 id="questions">Needs your answer' in page
    view = board.topic_view(RENEW)
    view["header"].update(blocked_by=["W1", "NOPE-9"], waiting_on="human:gate",
                          gate={"state": "open", "url": "http://localhost:1234/"})
    page = board_web.topic_page(view)
    assert '<a href="/topic/W1">W1</a>' in page and "NOPE-9" in page and 'href="/topic/NOPE-9"' not in page
    gate = next(text for href, text in _Doc(page).links if href == "http://localhost:1234/")
    assert gate == "waiting on you: gate (opens in a new tab)"
    assert '<a href="http://localhost:1234/" class="chip-link" target="_blank" rel="noopener">' in page
    view["header"]["gate"] = {"state": "open", "url": "javascript:alert(1)"}
    page = board_web.topic_page(view)
    assert 'href="javascript' not in page and "waiting on you: gate" in page


def test_the_topic_page_links_its_prs_and_its_ticket(server, monkeypatch, tmp_path):
    ms.artifact_put_impl("ABC-1", "implement", "pr", "https://github.com/o/r/pull/7", "complete", "sid-t")
    empty = tmp_path / "empty.toml"
    empty.write_text("")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(empty))
    page = _request(server, "/topic/ABC-1")[1]
    assert 'Pull request: <a href="https://github.com/o/r/pull/7"' in page
    assert "Ticket: ABC-1" in page and "tracker.example" not in page
    for template, linked in (("https://tracker.example/browse/{key}", True), ("javascript:{key}", False)):
        cfg = tmp_path / "board.toml"
        cfg.write_text(f"[task_keys]\nticket_url = '{template}'\n")
        monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
        page = _request(server, "/topic/ABC-1")[1]
        assert (["https://tracker.example/browse/ABC-1", "ABC-1 (opens in a new tab)"] in _Doc(page).links) is linked
        assert 'href="javascript' not in page
    view = board.topic_view("ABC-1")
    view["header"]["links"]["ticket"] = "https://example.test/T-1"
    view["header"]["prs"] = ["javascript:alert(1)"]
    page = board_web.topic_page(view)
    assert 'href="https://example.test/T-1"' in page
    assert "Pull request: javascript:alert(1)" in page and 'href="javascript' not in page


def test_artifact_names_link_and_nothing_else_does(server):
    link = '<a href="/topic/ABC-1/a/implement.notes">implement.notes</a>'
    ms.artifact_put_impl("ABC-1", "context", "ask", "build on implement.notes", "complete", "sid-t", scope="output")
    ms.artifact_put_impl("ABC-1", "summary", "thread", "see implement.notes, board.py:43, javascript:alert(1), "
                         "x.y <b>bold</b>", "complete", "sid-t", scope="output")
    ms.artifact_put_impl("ABC-1", "decision", "pane-1", "per implement.notes", "complete", "sid-t",
                         scope="output", decided_by="manager")
    state.append_event(paths.artifact_events_path("ABC-1"), {"type": "artifact_put", "phase": "context",
                                                              "name": "ask", "ts": 500.0})
    done = paths.done_dir_for("mgr")
    done.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(done / "sid-t-e1.json", {"claude_sid": "sid-t", "completed_at": 400.0,
                                                     "summary": "Wrote implement.notes and stopped. Done."})
    page = _request(server, "/topic/ABC-1")[1]
    summary = _section_of(page, "h-sum")
    assert link in summary and "board.py:43" in summary and "&lt;b&gt;bold&lt;/b&gt;" in summary
    assert 'href="/topic/ABC-1/a/board.py"' not in page and 'href="/topic/ABC-1/a/x.y"' not in page
    assert "<b>bold" not in page and 'href="javascript' not in page
    assert link in _section_of(page, "h-thread")
    ask = _section_of(page, "h-ask")
    assert '<div class="ask-head mono"><a href="/topic/ABC-1/a/context.ask">context.ask</a></div>' in ask
    assert link in ask
    assert link in _section_of(page, "h-ses")
    assert 'artifact_put <a href="/topic/ABC-1/a/context.ask">context.ask</a>' in _section_of(page, "h-ev")
    body = _request(server, "/topic/ABC-1/a/summary.thread")[1]
    assert link in body and "&lt;b&gt;" in body


def _domain_field(page):
    return page.split('<input id="n-dom"')[1].split(">")[0]


def test_the_new_page_labels_its_fields_and_offers_live_domains(server):
    page = _request(server, "/new")[1]
    assert '<label for="n-dom">Domain</label><input id="n-dom" name="domain" list="n-domains"' in page
    assert '<label for="n-text">' in page
    field = _domain_field(page)
    assert ' placeholder="general"' in field and "value=" not in field
    datalist = page.split('<datalist id="n-domains">')[1].split("</datalist>")[0]
    assert datalist == '<option value="general"></option><option value="product"></option>'
    assert "Live now: general, product." in page


def test_a_refused_domain_stays_in_the_field_beside_the_live_domains(server):
    status, page = _post(server, "/new", {"domain": "nobody", "text": "look into Z"})
    assert status == 400 and "no live manager for domain nobody" in page and "look into Z" in page
    assert ' value="nobody"' in _domain_field(page) and "Live now: general, product." in page
    status, page = _post(server, "/new", {"domain": "", "text": "  "})
    field = _domain_field(page)
    assert status == 400 and ' placeholder="general"' in field and "value=" not in field


def test_a_managers_question_is_labelled_with_its_domain(server):
    qid = ms.ask_engineer_impl("sid-mp", "Cut the scope?", task_key="fleet-product")["question_id"]
    _, page = _request(server, "/topic/fleet-product")
    assert "<strong>manager product</strong> asks" in page
    assert f'value="{qid}"' in page and 'action="/topic/fleet-product/answer"' in page


def test_a_workers_question_keeps_its_worker_label(server):
    _, page = _request(server, "/topic/W1")
    assert "<strong>sid-q</strong> asks" in page and "manager" not in _section_of(page, "q-q1")


def test_an_answer_redirects_with_its_decision_record(server):
    location = board_web.post_route("/topic/W1/answer", {"question_id": "q1", "text": "the first"}, server.csrf)[2]
    assert location == "/topic/W1?sent=decision.q-q1"


def test_new_work_from_a_topic_redirects_to_the_new_topic_with_context_ask(server, delivered):
    location = board_web.post_route("/topic/W1/message", {"kind": "new", "text": "follow up"}, server.csrf)[2]
    assert location == "/topic/in-20260930T101010-abcd?sent=context.ask"


def test_a_priority_change_redirects_without_sent(server):
    location = board_web.post_route("/topic/W1/message", {"kind": "priority", "priority": "P1"}, server.csrf)[2]
    assert location == "/topic/W1"


def _delivered_event(key, record, route, ok, reason=None, pane=None, question_id=None, attached=False):
    event = {"type": "message_delivered", "record": record, "kind": "message", "route": route, "ok": ok,
             "reason": reason, "via": "board"}
    if pane:
        event["pane"] = pane
    if attached:
        event["attached"] = True
    if question_id:
        event = {**event, "question_id": question_id, "kind": "answer"}
        event.pop("record")
    ms._append_task_event(key, event)


@pytest.mark.parametrize("event, text, danger", [
    (dict(route="pane", ok=True, pane="w1"),
     "Delivered to pane w1. The node answers in the thread before it goes on.", False),
    (dict(route="pane+resume", ok=True, pane="w1, w2"),
     "Delivered: pane+resume (w1, w2). The answer appears in the thread.", False),
    (dict(route="resume", ok=True, pane="w1"), "Delivered: resume (w1). The answer appears in the thread.", False),
    (dict(route="engine", ok=True), "Delivered: engine (?). The answer appears in the thread.", False),
    (dict(route="replace", ok=True),
     "The node's session was restarted with your message in its inputs. The answer appears in the thread.", False),
    (dict(route="restart", ok=True),
     "The node's session was restarted with your message in its inputs. The answer appears in the thread.", False),
    (dict(route="agent", ok=True), "No live session — the topic agent answers. The answer appears in the thread.",
     False),
    (dict(route="intake", ok=True), "Topic created, intake started.", False),
    (dict(route="pane", ok=False, reason="pane send failed"),
     "Not delivered: pane send failed. Queued; retried every 5 minutes while this domain's manager lives; "
     "the card is in Inbox › Stopped.", True),
    (dict(route="pane", ok=False, reason="human_attached (line: draft)", attached=True),
     "Not delivered: human_attached (line: draft). Queued; retried every minute while this domain's manager lives; "
     "it types once that pane's input line reads idle or the human-attached flag drops; the card is in "
     "Inbox › Stopped.", True),
    (dict(route="held+pane", ok=False, reason="human_attached (line: draft); held"),
     "Not delivered: human_attached (line: draft); held. Queued; retried every 5 minutes while this domain's "
     "manager lives; the card is in Inbox › Stopped.", True),
])
def test_the_sent_banner_reads_the_delivery_event(server, event, text, danger):
    _delivered_event("W1", "decision.msg-20260101T000000", **event)
    status, page = _request(server, "/topic/W1?sent=decision.msg-20260101T000000")
    assert status == 200 and board_web._e(text) in page
    assert ('class="callout callout-danger"' in page) == danger


def test_an_answer_banner_reads_the_answer_event(server):
    _delivered_event("W1", None, "answer", True, question_id="q1")
    status, page = _request(server, "/topic/W1?sent=decision.q-q1")
    assert status == 200 and "Answer recorded, question closed." in page


def test_a_sent_record_without_an_event_shows_the_pending_banner(server):
    status, page = _request(server, "/topic/W1?sent=decision.msg-nope")
    assert status == 200 and "Sent; the delivery result is not recorded yet." in page
    status, page = _request(server, "/topic/W1")
    assert status == 200 and "Sent;" not in page and "Delivered" not in page


def _reply_scene(key="W1"):
    ms.artifact_put_impl(key, "decision", "msg-A", topic_message.body("please hold", "engineer", "board"), "complete",
                         "sid-q", scope="output", decided_by="engineer")
    ms.artifact_put_impl(key, "reply", "E", "re: decision.msg-A\nHolding the merge.", "complete", "sid-q",
                         scope="output")


def _set_cookie(headers):
    return headers.get("Set-Cookie")


def test_a_topic_get_sets_the_seen_cookie_merging_the_sent_pairs(server):
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/topic/W1", headers={"Host": f"127.0.0.1:{port}", "Cookie": "dockwright_seen=OLD-1:100000"})
    response = conn.getresponse()
    cookie = _set_cookie(response.headers)
    response.read()
    conn.close()
    assert cookie.startswith("dockwright_seen=OLD-1:100000|W1:") and "; Path=/; Max-Age=2592000; SameSite=Lax" in cookie
    value = cookie.split(";")[0].removeprefix("dockwright_seen=")
    assert abs(int(value.split("|")[1].split(":")[1]) / 1000 - time.time()) < 5
    status, _, headers = _exchange(server, "/")
    assert status == 200 and _set_cookie(headers) is None
    status, _, headers = _exchange(server, "/topic/W1/a/summary.thread")
    assert status == 200 and _set_cookie(headers) is None


def test_the_seen_cookie_keeps_at_most_fifty_pairs_dropping_the_oldest():
    pairs = "|".join(f"K-{i}:{i * 1000}" for i in range(1, 61))
    seen, cookie = board_web.seen_cookie("/topic/W1", f"dockwright_seen={pairs}")
    kept = cookie.split(";")[0].removeprefix("dockwright_seen=").split("|")
    assert len(kept) == 50 and kept[-1].startswith("W1:") and "K-1:1000" not in kept and "K-12:12000" in kept
    assert seen["K-60"] == 60.0 and "K-1" not in seen


def test_a_malformed_seen_cookie_is_ignored(server):
    _reply_scene()
    seen, cookie = board_web.seen_cookie("/", "dockwright_seen=foo|W1:abc|:5|W1:7000")
    assert seen == {"W1": 7.0} and cookie is None
    assert board_web.seen_cookie("/", "dockwright_seen=foo|W1:abc")[0] == {}
    assert board_web.seen_cookie("/", "")[0] == {}
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/", headers={"Host": f"127.0.0.1:{port}", "Cookie": "dockwright_seen=foo|W1:abc"})
    response = conn.getresponse()
    page = response.read().decode()
    conn.close()
    assert response.status == 200 and "Holding the merge." in page


def test_a_foreign_malformed_cookie_does_not_hide_the_seen_pairs(server):
    _reply_scene()
    now = int(time.time() * 1000) + 5000
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/", headers={"Host": f"127.0.0.1:{port}",
                                      "Cookie": f'a={{"x":1}}; seen=foreign; dockwright_seen=W1:{now}'})
    response = conn.getresponse()
    page = response.read().decode()
    conn.close()
    assert response.status == 200 and "Holding the merge." not in page


def test_the_inbox_hides_a_reply_card_the_cookie_marks_seen(server):
    _reply_scene()
    assert "Holding the merge." in _request(server, "/")[1]
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/topic/W1", headers={"Host": f"127.0.0.1:{port}"})
    response = conn.getresponse()
    cookie = _set_cookie(response.headers).split(";")[0]
    response.read()
    conn.request("GET", "/", headers={"Host": f"127.0.0.1:{port}", "Cookie": cookie})
    response = conn.getresponse()
    page = response.read().decode()
    conn.close()
    assert response.status == 200 and "Holding the merge." not in page


def test_the_reply_card_links_to_the_thread(server):
    _reply_scene()
    inbox = _request(server, "/")[1]
    item = _item(inbox, "Holding the merge.")
    topic_link, action = _Doc(item).links
    assert topic_link == ["/topic/W1", "W1"] and action[0] == "/topic/W1#thread" and "Open topic" in action[1]
    assert "chip-accent" in item and ">reply<" in item


@pytest.mark.parametrize("header, kind, words", [
    ({"waiting_on": "human:question", "status": None}, "work", "waiting on you: question"),
    ({"waiting_on": "human:gate", "status": None}, "work", "waiting on you: gate"),
    ({"waiting_on": "human:approval-dialog", "status": None}, "work", "waiting on you: dialog"),
    ({"waiting_on": "worker:repo", "status": None}, "work", "working: repo"),
    ({"waiting_on": "external:merge", "status": None}, "work", "waiting: merge"),
    ({"waiting_on": "stalled:design", "status": None}, "work", "stalled: design"),
    ({"waiting_on": "none", "status": None}, "work", "closed"),
    ({"waiting_on": "none", "status": "open"}, "fleet", "open"),
    ({"waiting_on": "none", "status": "closed"}, "note", "closed"),
    ({"waiting_on": "none", "status": None}, "lane", "open"),
])
def test_the_state_line_reads_in_words(header, kind, words):
    assert board_web._state_words(header, kind) == words
    assert board_web._state_words({"waiting_on": "none", "status": None}, "work", {"name": "w7"}) == "working: w7"


def _thread_page(server):
    from tests.test_board import _thread_scene
    return _request(server, "/topic/W1")[1]


def test_the_topic_page_order_is_state_banner_latest_questions_message_thread(server, fresh_orchestrator_dir,
                                                                             tmp_path, monkeypatch):
    from tests.test_board import _put
    _put("W1", "decision", "msg-A", topic_message.body("please hold", "engineer", "board"), decided_by="engineer")
    _put("W1", "reply", "E", "re: decision.msg-A\nHolding the merge.")
    ms._append_task_event("W1", {"type": "message_delivered", "record": "decision.msg-A", "kind": "message",
                                 "route": "pane", "ok": True, "reason": None, "via": "board", "pane": "w1"})
    page = _request(server, "/topic/W1?sent=decision.msg-A")[1]
    marks = ['<p class="state">', "Delivered to pane w1.", "Latest for you", 'id="q-q1"', 'action="/topic/W1/message"',
             'id="thread"', '<div class="facts">', 'id="h-sum"']
    positions = [page.index(m) for m in marks]
    assert positions == sorted(positions), list(zip(marks, positions))
    assert '<p class="state">waiting on you: question</p>' in page


def test_the_message_card_on_a_closed_topic_says_the_agent_answers(server):
    page = _request(server, "/topic/OLD-1")[1]
    assert '<p class="state">closed</p>' in page and "No live session: the topic agent answers." in page
    page = _request(server, "/topic/W1")[1]
    assert "the topic agent answers" not in page


def test_latest_for_you_is_the_newest_reply(server):
    from tests.test_board import _put
    _put("W1", "reply", "E", "re: decision.msg-A\nHolding the merge.")
    _put("W1", "decision", "msg-C", topic_message.body("trigger: heartbeat", "engineer", "reconcile"),
         decided_by="engineer")
    _put("W1", "decision", "msg-B", topic_message.body("noted", "manager", "mcp"), decided_by="manager")
    _put("W1", "decision", "verdict-r1", '{"decision": "approved"}', decided_by="engineer")
    page = _request(server, "/topic/W1")[1]
    latest = _section_of(page, "h-latest")
    assert "Holding the merge." in latest and "noted" not in latest and "heartbeat" not in latest
    page = _request(server, "/topic/W2")[1]
    assert 'id="h-latest"' not in page


def test_a_topic_without_a_pipeline_but_with_a_live_worker_is_not_closed(server, fresh_orchestrator_dir, monkeypatch):
    page = _request(server, "/topic/OUT-1")[1]
    assert '<p class="state">working: sid-o</p>' in page and "closed" not in page
    assert "the topic agent answers" not in page
    (paths.ACTIVE / "sid-o.json").unlink()
    page = _request(server, "/topic/OUT-1")[1]
    assert '<p class="state">closed</p>' in page and "the topic agent answers" in page


def test_a_reply_written_after_the_render_in_the_same_second_still_gets_its_card(server):
    _reply_scene()
    time.sleep(0.002)
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/topic/W1", headers={"Host": f"127.0.0.1:{port}"})
    response = conn.getresponse()
    cookie = _set_cookie(response.headers).split(";")[0]
    response.read()
    time.sleep(0.01)
    ms.artifact_put_impl("W1", "reply", "F", "re: decision.msg-A\nA second reply.", "complete", "sid-q",
                         scope="output")
    conn.request("GET", "/", headers={"Host": f"127.0.0.1:{port}", "Cookie": cookie})
    response = conn.getresponse()
    page = response.read().decode()
    conn.close()
    assert response.status == 200 and "A second reply." in page and "Holding the merge." not in page
