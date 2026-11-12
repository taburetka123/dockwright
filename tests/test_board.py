import hashlib
import json
import os
import time
from datetime import date, timedelta

import pytest

from dockwright import board, paths, state, topic, waiting_on
from dockwright import mcp_server as ms
from dockwright import notebook_migrate as nm
from tests.test_mcp_tools import _CLAUDE, _holder, _probes, _seed_snapshot, fresh_orchestrator_dir

DIALOG = "Bash command\n  rm -rf build\n\nDo you want to proceed?\n❯ 1. Yes\n  2. No\n"
SHELL = "/bin/zsh -c eval 'bash /u/.claude/scripts/plannotator-gate /r/'"
GATE = "bash /u/.claude/scripts/plannotator-gate /r/ --gate --json"
P_Q, P_G, P_T, P_N, P_O, P_D, P_SUB, P_X, P_M, P_MP = range(424250, 424260)
DEAD = 424299
TODAY = date.today()
YESTERDAY, NEXT_WEEK = TODAY - timedelta(days=1), TODAY + timedelta(days=7)
LONG = "Long title word " * 12 + "end"
GENERAL = (f"# Notebook — general\n\n## [ ] Renew the cert\n- review-by: {YESTERDAY}\n\n"
           f"## [ ] Plan the move\n- review-by: {NEXT_WEEK}\n\n## [ ] {LONG}\n- when: later\n")
PRODUCT = (f"# Notebook — product\n\n## [ ] Ship pricing\n- **when:** the PR merges · **review-by:** {YESTERDAY}\n\n"
           "## [x] Old pricing idea\n- resolved\n")
RENEW = "note-general-renew-the-cert"
NOTES = {RENEW, "note-general-plan-the-move", "note-general-long-title-word-long"}


def _session(sid, pid, *, ticket=None, parent="mgr", state_="idle", agent="worker", kind=None, domain=None,
             name=None, tasked_at=100.0):
    if ticket:
        state.write_json_atomic(paths.assignment_path(sid), {"assignment_id": f"a-{sid}", "claude_sid": sid,
                                                             "ticket": ticket, "name": name or sid,
                                                             "spawned_at": 1.0})
    record = {"claude_sid": sid, "agent": agent, "name": name or sid, "pid": pid, "state": state_,
              "runtime": "claude", "window_id": f"%{pid}", "parent_manager_name": parent, "tasked_at": tasked_at}
    if kind:
        record["kind"] = kind
    if domain:
        record["domain"] = domain
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", record)


def _question(qid, sid, text, asked_at, *, manager="mgr", name=None):
    qdir = paths.question_dir_for(manager)
    qdir.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(qdir / f"{qid}.json", {"question_id": qid, "worker_sid": sid,
                                                   "worker_name": name or sid, "parent_manager_name": manager,
                                                   "parent_name": manager, "question": text,
                                                   "asked_at": asked_at})


def _scene(tmp_path, monkeypatch):
    live = (P_Q, P_G, P_T, P_N, P_O, P_D, P_SUB, P_X, P_M, P_MP)
    commands = {pid: _CLAUDE for pid in live} | {500: SHELL, 501: GATE, 510: SHELL, 511: GATE}
    _probes(monkeypatch, commands=commands, parents={500: P_G, 501: 500, 510: P_O, 511: 510}, screen=DIALOG)
    _session("sid-m", P_M, agent="manager", parent=None, domain="general", name="mgr")
    _session("sid-mp", P_MP, agent="manager", parent=None, domain="product", name="mgr-p")
    _seed_snapshot("W1", tmp_path, holder="a-w1")
    _holder("W1", "sid-q", "a-w1", pid=P_Q)
    _question("q1", "sid-q", "Which schema?\nsecond line", 150.0)
    _seed_snapshot("W2", tmp_path, holder="a-w2")
    _holder("W2", "sid-g", "a-w2", pid=P_G)
    _seed_snapshot("W3", tmp_path, holder="a-w3")
    _holder("W3", "sid-s", "a-w3", pid=DEAD)
    done = paths.done_dir_for("mgr")
    done.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(done / "sid-s-e1.json", {"claude_sid": "sid-s", "completed_at": 300.0,
                                                     "summary": "Parked at the design gate. The hour ran out."})
    ms.artifact_put_impl("ABC-1", "implement", "notes", "body", "partial", "sid-t")
    _session("sid-t", P_T, ticket="ABC-1")
    _question("q2", "sid-t", "Which port should the board use?", 160.0)
    _session("sid-n", P_N, name="loose-worker")
    _question("q3", "sid-n", "Is the rebase safe?", 170.0, name="loose-worker")
    _session("sid-x", P_X, parent="gone", name="orphan")
    _question("q4", "sid-x", "Anyone there?", 180.0, manager="gone", name="orphan")
    _session("sid-sub", P_SUB, ticket="W1", kind="subworker")
    _question("q5", "sid-sub", "subworker asks", 140.0)
    _session("sid-o", P_O, ticket="OUT-1", tasked_at=190.0)
    _session("sid-dlg", P_D, parent="mgr-p", state_="processing", name="pricing-worker", tasked_at=200.0)
    nm.notebook_dir().mkdir(parents=True, exist_ok=True)
    nm.notebook_path("general").write_text(GENERAL)
    assert nm.migrate("general") == 0
    nm.notebook_path("product").write_text(PRODUCT)
    nm.notebook_path("x-archive").write_text("## [ ] Archived thing\n- review-by: 2000-01-01\n")
    ms.artifact_put_impl("OLD-1", "implement", "old", "body", "complete", "sid-old")
    old = time.time() - 40 * 86400
    old_dir = paths.artifact_ticket_dir("OLD-1")
    for p in [*old_dir.iterdir(), old_dir]:
        os.utime(p, (old, old))


def _tree(root):
    out = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_dir():
            out[rel + "/"] = None
        else:
            st = p.stat()
            out[rel] = (st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def test_no_screen_writes_anything(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _scene(tmp_path, monkeypatch)
    before = _tree(fresh_orchestrator_dir)
    board.render_inbox(board.inbox())
    board.render_inbox(board.inbox("general"))
    board.render_topics(board.topics())
    board.render_topics(board.topics(kind="note"))
    board.render_topics(board.topics("general", "note", "open"))
    board.render_topics(board.topics("product", "note", "open"))
    for key in board.keys():
        board.render_topic(board.topic_view(key))
    assert _tree(fresh_orchestrator_dir) == before
    assert paths.artifact_ticket_dir("OLD-1").is_dir()


def test_the_inbox_holds_every_wait_in_its_block_in_order(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _scene(tmp_path, monkeypatch)
    data = board.inbox()
    assert [(i["kind"], i["topic"]) for i in data["waiting"]] == [
        ("gate", "W2"), ("question", "W1"), ("question", "ABC-1"), ("question", "loose-worker"),
        ("question", "orphan"), ("gate", "OUT-1"), ("dialog", "pricing-worker")]
    assert [i["priority"] for i in data["waiting"][:3]] == ["P1", "P1", "P2"]
    assert [(i["kind"], i["topic"], i["text"]) for i in data["stopped"]] == [
        ("stalled:design", "W3", "Parked at the design gate.")]
    assert data["warnings"] == []
    text = board.render_inbox(data)
    assert text.splitlines()[0] == "▶ Waiting on you" and "▶ Stopped" in text
    assert 'P1  question  W1 · ' in text and '— "Which schema?"' in text
    assert 'P2  stalled:design  W3 · ' in text and '— "Parked at the design gate."' in text
    for absent in ("subworker asks", "Plan the move", "Old pricing", "Archived thing", "second line"):
        assert absent not in text


def test_the_domain_filter_keeps_its_own_and_the_unknown_domain(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _scene(tmp_path, monkeypatch)
    general = board.inbox("general")
    assert [i["topic"] for i in general["waiting"]] == ["W2", "W1", "ABC-1", "loose-worker", "orphan", "OUT-1"]
    assert [i["topic"] for i in general["stopped"]] == ["W3"]
    product = board.inbox("product")
    assert [i["topic"] for i in product["waiting"]] == ["orphan", "pricing-worker"]
    assert product["stopped"] == []


def test_every_work_waiting_on_a_human_has_an_inbox_line(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _scene(tmp_path, monkeypatch)
    human = {k for k in board.keys()
             if json.loads(ms.pipeline_status_impl(k, "json"))["waiting_on"].startswith("human:")}
    assert human == {"W1", "W2", "ABC-1"}
    assert human <= {i["key"] for i in board.inbox()["waiting"]}
    monkeypatch.setattr(board, "_sessions", lambda index, errors: [])
    assert ("gate", "W2") in [(i["kind"], i["topic"]) for i in board.inbox()["waiting"]]


def test_any_human_value_gets_an_inbox_line(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch)
    _seed_snapshot("W11", tmp_path)
    _holder("W11", "sid-h", "a-design")
    monkeypatch.setattr(waiting_on, "token_value", lambda facts, now=None: "human:review")
    assert [(i["kind"], i["topic"]) for i in board.inbox()["waiting"]] == [("review", "W11")]


def test_each_screen_takes_one_process_snapshot(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _scene(tmp_path, monkeypatch)
    probe = ms._process_index
    calls = []
    monkeypatch.setattr(ms, "_process_index", lambda: calls.append(1) or probe())
    for screen in (board.inbox, board.topics, lambda: board.topic_view("W1")):
        calls.clear()
        screen()
        assert calls == [1]


def test_a_failed_ps_warns_and_an_empty_store_says_so(fresh_orchestrator_dir, monkeypatch):
    _probes(monkeypatch)
    assert board.render_inbox(board.inbox()) == "Nothing is waiting on you."
    assert board.render_topics(board.topics()) == "no topics"
    _probes(monkeypatch, ps=False)
    assert board.render_inbox(board.inbox()).splitlines() == [board.PS_WARNING, "Nothing is waiting on you."]


def test_topics_run_freshest_first_and_a_note_check_moves_nothing(fresh_orchestrator_dir, monkeypatch):
    _probes(monkeypatch)
    for key, ts in (("A", 1000.0), ("B", 3000.0), ("D", 500.0)):
        state.append_event(paths.artifact_events_path(key), {"type": "note", "ts": ts})
    paths.artifact_ticket_dir("C").mkdir(parents=True)
    card = topic.new_card("note", "general", "D note", created_at=400.0)
    state.write_json_atomic(paths.topic_path("D"), {**card, "last_check": {"at": 9e9, "runs": []}})
    assert [r["key"] for r in board.topics()["rows"]] == ["B", "A", "D", "C"]


def test_a_lane_event_does_not_move_a_works_activity(fresh_orchestrator_dir, monkeypatch):
    _probes(monkeypatch)
    for key, ts in (("A", 1000.0), ("B", 3000.0)):
        state.append_event(paths.artifact_events_path(key), {"type": "note", "ts": ts})
    state.append_event(paths.artifact_events_path("A"), {"type": "lane_event", "kind": "NUDGED", "ts": 5000.0,
                                                         "fields": {"name": "A-task", "why": "30min"}})
    rows = board.topics()["rows"]
    assert [(r["key"], r["activity"]) for r in rows] == [("B", 3000.0), ("A", 1000.0)]


def test_a_handed_inputs_event_leaves_the_last_event_on_the_node_move(fresh_orchestrator_dir, monkeypatch):
    _probes(monkeypatch)
    state.append_event(paths.artifact_events_path("A"), {"type": "advance_requested", "to": "task", "ts": 1000.0})
    state.append_event(paths.artifact_events_path("A"), {"type": "inputs_handed", "assignment_id": "a1",
                                                         "names": ["design.hld"], "ts": 1001.0})
    assert board.topics()["rows"][0]["last_event"] == "advance_requested → task"


def test_a_topic_that_fails_to_read_prints_an_error_line(fresh_orchestrator_dir, monkeypatch):
    _probes(monkeypatch)
    for key in ("BAD", "GOOD"):
        paths.artifact_ticket_dir(key).mkdir(parents=True)
    real = topic.load

    def load(key):
        if key == "BAD":
            raise RuntimeError("unreadable")
        return real(key)

    monkeypatch.setattr(topic, "load", load)
    text = board.render_topics(board.topics())
    assert "BAD  error: RuntimeError" in text and "GOOD" in text
    assert "⚠" not in text
    assert board.render_topics(board.topics(status="open")).splitlines() == ["⚠ BAD: error: RuntimeError", "no topics"]


def test_topic_filters_need_a_card_and_notes_read_their_own_source(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _scene(tmp_path, monkeypatch)
    notes = board.topics(kind="note")["rows"]
    assert {r["key"] for r in notes} == NOTES
    long_row = next(r for r in notes if r["key"] == "note-general-long-title-word-long")
    assert long_row["title"] == f"[ ] {LONG}" and len(long_row["title"]) > topic.TITLE_MAX
    opened = {r["key"] for r in board.topics(status="open")["rows"]}
    assert {"W1", "W2", "W3", "fleet-general", *NOTES} <= opened and not opened & {"ABC-1", "OLD-1"}
    general = board.topics("general", "note", "open")
    assert general["notebook"] is None and {n["key"] for n in general["notes"]} == NOTES
    assert f"[ ] {LONG}" in board.render_topics(general)
    assert board.render_topics(board.topics("product", "note", "open")).splitlines() == [
        f"from notebook {nm.notebook_path('product')}, not migrated", "open  [ ] Ship pricing"]
    assert board.render_topics(board.topics("product", "note", "closed")).splitlines()[-1] == (
        "closed  [x] Old pricing idea")
    assert board.render_topics(board.topics(kind="nothing")) == "no topics"


def test_an_edited_notebook_warns_until_migrated_again(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _scene(tmp_path, monkeypatch)
    nm.notebook_path("general").write_text(GENERAL.replace("- when: later", "- when: much later"))
    line = "⚠ notebook general changed after migration: 1 — run dockwright notebook-migrate general"
    assert line in board.render_inbox(board.inbox()).splitlines()
    assert line in board.render_topics(board.topics()).splitlines()
    assert line not in board.render_inbox(board.inbox("product"))
    assert nm.migrate("general") == 0
    assert line not in board.render_inbox(board.inbox())


def test_the_topic_screen_shows_every_section(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _scene(tmp_path, monkeypatch)
    ms.artifact_put_impl("W1", "summary", "thread", "W1 summary line", "complete", "sid-q", scope="output")
    ms.artifact_put_impl("W1", "decision", "pane-1", "hold the merge", "complete", "sid-q", scope="output",
                         decided_by="manager")
    ms.artifact_put_impl("W1", "design", "hld", "HLD body", "complete", "sid-q", scope="output")
    ms.artifact_put_impl("W1", "review", "round-1", "APPROVE", "complete", "sid-q")
    ms.artifact_put_impl("W1", "context", "ask", "Build the board", "complete", "mgr", scope="output")
    ms.artifact_put_impl("fleet-general", "decisions", "general", "- rule", "complete", "cli:test", scope="output")
    text = board.render_topic(board.topic_view("W1"))
    for part in ("# W1", "priority: P2 (shown P1) · waiting_on: human:question", "## Summary\nW1 summary line",
                 "  manager (pane): hold the merge", "## Decisions\ndomain rulings",
                 "domain rulings: fleet-general → decisions.general", "### context.ask\nBuild the board",
                 "- design.hld [complete]", "kitchen: 1 (checkpoint 0, internal 1)", "- W1-a-w1 sid-q (",
                 "## Events (last 40)"):
        assert part in text, part
    assert "HLD body" not in text and "APPROVE" not in text
    assert "## Summary\nno summary" in board.render_topic(board.topic_view("W2"))


def test_a_note_topic_shows_its_status_deadline_and_last_check(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _scene(tmp_path, monkeypatch)
    topic.update(RENEW, lambda c: {**c, "last_check": {"at": 1000.0, "runs": [
        {"cmd": "ls", "status": "ok", "exit": 0, "output": ""}]}})
    text = board.render_topic(board.topic_view(RENEW))
    assert f"status: open · review-by: {YESTERDAY} · last check: " in text and "— ok `ls`" in text


def test_the_cli_prints_the_screens_and_exits_by_code(fresh_orchestrator_dir, tmp_path, monkeypatch, capsys):
    _scene(tmp_path, monkeypatch)
    capsys.readouterr()
    assert board.main([]) == 0 and capsys.readouterr().out.startswith("▶ Waiting on you")
    assert board.main(["--domain", "product"]) == 0 and "W2" not in capsys.readouterr().out
    assert board.main(["topics", "--kind", "note"]) == 0 and RENEW in capsys.readouterr().out
    assert board.main(["topic", "W1"]) == 0 and capsys.readouterr().out.startswith("# W1")
    assert board.main(["topic", "NOPE"]) == 1 and capsys.readouterr().err == "no topic NOPE\n"
    with pytest.raises(SystemExit) as exit_:
        board.main(["topics", "--status", "bogus"])
    assert exit_.value.code == 2


def test_notes_of_a_longer_domain_stay_out(fresh_orchestrator_dir, monkeypatch):
    _probes(monkeypatch)
    nm.notebook_dir().mkdir(parents=True, exist_ok=True)
    for domain in ("meta", "meta-x"):
        nm.notebook_path(domain).write_text(f"# {domain}\n\n## [ ] Same words here\n")
        assert nm.migrate(domain) == 0
    assert [n["key"] for n in board.domain_notes("meta")["notes"]] == ["note-meta-same-words-here"]


def test_migrated_notes_keep_the_notebook_order(fresh_orchestrator_dir, monkeypatch):
    _probes(monkeypatch)
    nm.notebook_dir().mkdir(parents=True, exist_ok=True)
    nm.notebook_path("meta").write_text("# meta\n\n## [ ] Zulu first\n\n## [ ] Alpha gone\n\n## [ ] Mike last\n")
    assert nm.migrate("meta") == 0
    nm.notebook_path("meta").write_text("# meta\n\n## [ ] Zulu first\n\n## [ ] Mike last\n")
    assert nm.migrate("meta") == 0
    assert [n["key"] for n in board.domain_notes("meta")["notes"]] == [
        "note-meta-zulu-first", "note-meta-mike-last", "note-meta-alpha-gone"]
    assert board.render_topics(board.topics("meta", "note", "open")).splitlines() == [
        "open  note-meta-zulu-first  [ ] Zulu first", "open  note-meta-mike-last  [ ] Mike last"]


def test_notes_whose_notebook_maps_two_sections_to_one_key_keep_key_order(fresh_orchestrator_dir, monkeypatch):
    _probes(monkeypatch)
    nm.notebook_dir().mkdir(parents=True, exist_ok=True)
    nm.notebook_path("meta").write_text("# meta\n\n## [ ] Zulu first\n\n## [ ] Mike last\n")
    assert nm.migrate("meta") == 0
    sha = hashlib.sha1(b"Same words here").hexdigest()[:6]
    nm.notebook_path("meta").write_text("# meta\n\n## [ ] Zulu first\n\n## [ ] Mike last\n\n## [ ] Same words here\n\n"
                                        f"## [ ] Same words here\n\n## [ ] Same words here {sha}\n")
    assert [n["key"] for n in board.domain_notes("meta")["notes"]] == ["note-meta-mike-last", "note-meta-zulu-first"]
    assert board.inbox()["notebook_warnings"][0].startswith("⚠ notebook meta cannot migrate: two sections map to one topic key")


def test_a_work_stalled_on_its_request_names_the_holder_report(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch)
    _seed_snapshot("W10", tmp_path)
    _holder("W10", "sid-r", "a-design")
    done = paths.done_dir_for("mgr")
    done.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(done / "sid-r-e1.json", {"claude_sid": "sid-r", "completed_at": 300.0,
                                                     "summary": "Design done. Advancing to task."})
    events = paths.artifact_events_path("W10")
    state.append_event(events, {"type": "advance_requested", "token_id": "t1", "request_id": "r1",
                                "edge": {"from": "design", "to": "task"}, "index_to": 1, "kind": "advance",
                                "ts": time.time() - 2 * waiting_on.STALL_GRACE_SEC})
    state.append_event(events, {"type": "advance_pending", "token_id": "t1", "request_id": "r1",
                                "reason": "spawn-error", "assignment_id": None})
    [item] = board.inbox()["stopped"]
    assert (item["kind"], item["topic"], item["text"]) == ("stalled:task", "W10", "Design done.")


def test_a_notebook_that_cannot_migrate_says_why(fresh_orchestrator_dir, monkeypatch):
    _probes(monkeypatch)
    nm.notebook_dir().mkdir(parents=True, exist_ok=True)
    nm.notebook_path("meta").write_text("# meta\n\n## [ ] Task\n\n## RULINGS\n- rule\n")
    assert nm.migrate("meta", "RULINGS") == 0
    nm.notebook_path("meta").write_text("# meta\n\n## [ ] Task\n")
    assert nm.migrate("meta") == 1
    [line] = board.inbox()["notebook_warnings"]
    assert line.startswith("⚠ notebook meta cannot migrate: no section heading holds 'RULINGS'")


def test_an_unreadable_notebook_warns_and_the_screens_still_render(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                   capsys):
    _scene(tmp_path, monkeypatch)
    for domain in ("general", "product"):
        nm.notebook_path(domain).write_bytes(b"## [ ] ok\n\xff\xfe")
    warnings = board.inbox()["notebook_warnings"]
    assert any(w.startswith("⚠ notebook general cannot migrate: cannot read ") for w in warnings)
    assert any(w.startswith("⚠ notebook product: cannot read ") for w in warnings)
    assert board.topics("product", "note", "open")["notes"] == []
    assert {n["key"] for n in board.topics("general", "note", "open")["notes"]} == NOTES
    capsys.readouterr()
    assert board.main([]) == 0 and "⚠ notebook product: cannot read " in capsys.readouterr().out


def test_a_lane_event_is_labelled_with_its_kind():
    event = {"type": "lane_event", "kind": "NODE_NO_SUMMARY",
             "fields": {"task_key": "K", "node": "design", "name": "K-design"}}
    assert board.event_label(event) == "lane_event NODE_NO_SUMMARY K design K-design"


def _keyed_question(qid, sid, task_key, text, asked_at, *, manager="mgr"):
    qdir = paths.question_dir_for(manager)
    qdir.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(qdir / f"{qid}.json", {"question_id": qid, "worker_sid": sid, "worker_name": sid,
                                                   "parent_manager_name": manager, "parent_name": manager,
                                                   "question": text, "asked_at": asked_at, "task_key": task_key})


def test_keys_hold_every_keyed_question_and_assignment_and_write_nothing(fresh_orchestrator_dir, tmp_path,
                                                                        monkeypatch):
    _scene(tmp_path, monkeypatch)
    _keyed_question("q7", "sid-free", "REC-1", "Keyed by the record?", 199.0)
    _keyed_question("q8", "sid-sub", "SUB-9", "subworker keyed", 198.0)
    state.write_json_atomic(paths.assignment_path("sid-ended"), {"assignment_id": "a-ended", "claude_sid": "sid-ended",
                                                                 "ticket": "END-1", "name": "ended",
                                                                 "spawned_at": 1.0})
    state.write_json_atomic(paths.pending_assignment_path("a-pend"), {"assignment_id": "a-pend", "ticket": "PEND-1"})
    state.write_json_atomic(paths.assignment_path("sid-bad"), {"assignment_id": "a-bad", "claude_sid": "sid-bad",
                                                               "ticket": "../x"})
    before = _tree(fresh_orchestrator_dir)
    got = board.keys()
    assert got == sorted(got)
    assert {"OUT-1", "REC-1", "END-1", "W1", "ABC-1", "OLD-1"} <= set(got)
    assert not {"SUB-9", "PEND-1", "../x"} & set(got)
    assert not paths.artifact_ticket_dir("OUT-1").exists()
    assert _tree(fresh_orchestrator_dir) == before


def test_a_question_on_a_key_without_artifacts_carries_its_key_and_id(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _scene(tmp_path, monkeypatch)
    _question("q6", "sid-o", "Ship it?", 195.0)
    data = board.inbox()
    item = next(i for i in data["waiting"] if i["kind"] == "question" and i["topic"] == "OUT-1")
    assert (item["key"], item["question_id"]) == ("OUT-1", "q6")
    assert next(i for i in data["waiting"] if i["topic"] == "W1")["question_id"] == "q1"
    gate = next(i for i in data["waiting"] if i["kind"] == "gate" and i["topic"] == "OUT-1")
    assert gate["key"] == "OUT-1" and gate["hint"] == board.GATE_HINT and "link" not in gate
    assert f"  reopen: {board.GATE_HINT}" in board.render_inbox(data)
    view = board.topic_view("OUT-1")
    assert [q["question_id"] for q in view["questions"]] == ["q6"]
    assert view["header"]["waiting_on"] == "human:question"
    assert not paths.artifact_ticket_dir("OUT-1").exists()


def test_the_topic_view_finds_prs_the_ticket_and_artifact_names(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _scene(tmp_path, monkeypatch)
    ms.artifact_put_impl("ABC-1", "implement", "pr", "PR: https://github.com/o/r/pull/7\n"
                         "also https://example.test/o/r/pull/8\njavascript:alert(1)", "complete", "sid-t")
    view = board.topic_view("ABC-1")
    assert view["header"]["prs"] == ["https://github.com/o/r/pull/7"]
    assert view["header"]["links"]["ticket"] == "ABC-1"
    assert {"implement.pr", "implement.notes"} <= set(view["names"])
    text = board.render_topic(view)
    assert "PR: https://github.com/o/r/pull/7" in text and "ticket=ABC-1" in text
    w1 = board.topic_view("W1")["header"]
    assert w1["prs"] == [] and not w1["links"].get("ticket")
    ms.artifact_put_impl("PR-1", "implement", "pr", "https://github.com/o/r/pull/7", "complete", "sid-p")
    card = topic.new_card("note", "general", "pr work")
    state.write_json_atomic(paths.topic_path("PR-1"), {**card, "links": {**card["links"],
                                                                         "pr": "https://github.com/o/r/pull/5"}})
    assert board.topic_view("PR-1")["header"]["prs"] == ["https://github.com/o/r/pull/5",
                                                         "https://github.com/o/r/pull/7"]


def test_the_topic_view_finds_every_pr_on_one_line(fresh_orchestrator_dir):
    ms.artifact_put_impl("TWO-1", "implement", "pr", "Opened https://github.com/o/a/pull/1 and "
                         "https://github.com/o/b/pull/2, again https://github.com/o/a/pull/1", "complete", "sid-2")
    assert board.topic_view("TWO-1")["header"]["prs"] == ["https://github.com/o/a/pull/1",
                                                          "https://github.com/o/b/pull/2"]


def test_inbox_reads_the_assignments_directory_once_whatever_the_number_of_works(fresh_orchestrator_dir, tmp_path,
                                                                                monkeypatch):
    for n in (1, 2, 3):
        _seed_snapshot(f"K-{n}", tmp_path, holder=f"a{n}")
        _holder(f"K-{n}", f"s{n}", f"a{n}")
    reads = []
    real = state.list_json_in

    def spy(directory):
        if directory == paths.ASSIGNMENTS:
            reads.append(directory)
        return real(directory)

    monkeypatch.setattr(ms.state, "list_json_in", spy)
    monkeypatch.setattr(board.state, "list_json_in", spy)
    result = board.inbox()
    assert len(reads) == 2
    assert {w["key"] for w in result["waiting"]} == set() or all(w["key"].startswith("K-") for w in result["waiting"])


def _manager(sid, name, domain, pid=P_M):
    _session(sid, pid, agent="manager", parent=None, domain=domain, name=name)


def test_a_managers_question_is_an_inbox_card_with_its_domain(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch, commands={P_M: _CLAUDE})
    _manager("sid-m", "mgr", "product")
    qid = ms.ask_engineer_impl("sid-m", "ship?\nmore", task_key="fleet-product")["question_id"]
    q = board._questions()[0]
    assert q["asked_by"] == "manager" and q["domain"] == "product"
    item = [i for i in board.inbox()["waiting"] if i["kind"] == "question"][0]
    assert item["domain"] == "product" and item["text"] == "ship?" and item["question_id"] == qid
    assert item["priority"] == "P2" and item["topic"] == "fleet-product"
    assert board.topic_view("fleet-product")["questions"][0]["asked_by"] == "manager"


def test_a_managers_question_without_a_live_manager_keeps_the_recorded_domain(fresh_orchestrator_dir, tmp_path,
                                                                              monkeypatch):
    _probes(monkeypatch, commands={})
    _manager("sid-m", "mgr", "product")
    ms.ask_engineer_impl("sid-m", "ship?", task_key="fleet-product")
    (paths.ACTIVE / "sid-m.json").unlink()
    assert board.row_domain("fleet-product", "mgr") is None
    item = [i for i in board.inbox()["waiting"] if i["kind"] == "question"][0]
    assert item["domain"] == "product"


def _put(key, phase, name, body, writer="sid-q", **kw):
    time.sleep(0.002)
    return ms.artifact_put_impl(key, phase, name, body, "complete", writer, scope="output", **kw)["written_at"]


def _thread_scene(tmp_path, monkeypatch):
    from dockwright import topic_message
    _scene(tmp_path, monkeypatch)
    state.write_json_atomic(paths.assignment_path("sid-agent"), {"assignment_id": "a-agent", "claude_sid": "sid-agent",
                                                                 "ticket": "W1", "name": "W1-agent", "spawned_at": 2.0})
    _put("W1", "decision", "gate-1", '{"decision": "approved"}', decided_by="engineer")
    _put("W1", "decision", "msg-A", topic_message.body("please hold\nsecond", "engineer", "board"), decided_by="engineer")
    _put("W1", "decision", "msg-B", topic_message.body("noted", "manager", "mcp"), decided_by="manager")
    _put("W1", "decision", "msg-C", topic_message.body("author-wait: ticket author replied", "engineer", "reconcile"),
         decided_by="engineer")
    _put("W1", "decision", "msg-C2", topic_message.body("event: tracker.comment — K #1 by X (sub a1b2c3d4)", "manager",
                                                       "reconcile"), decided_by="manager")
    _put("W1", "decision", "pane-D", "from: engineer\nnode: design\ntime: 2026-10-20T10:00:00Z\n\nflip it",
         decided_by="engineer")
    _put("W1", "reply", "E", "re: decision.msg-A\nHolding the merge.\nmore")
    _put("W1", "reply", "F", "re: decision.msg-B\nThe agent says hi.", writer="sid-agent")
    _put("W1", "decision", "q-G", "# decision q-G\n\n## question\nWhich?\n\n## answer\nthe first\n\nasked_by: w1 (sid-q)\n"
         "node: design\nasked_at: 1\nanswered_at: 2\ndecided_by: engineer", decided_by="engineer")
    _put("W1", "decision", "verdict-r1", '{"decision": "approved", "feedback": ""}', decided_by="engineer")


def test_the_thread_lists_five_record_classes_newest_first_with_their_authors(fresh_orchestrator_dir, tmp_path,
                                                                            monkeypatch):
    _thread_scene(tmp_path, monkeypatch)
    thread = board.topic_view("W1")["thread"]
    assert [t["who"] for t in thread] == ["you: gate verdict r1", "you answered w1", "topic agent", "node design",
                                          "you (pane)", "automation", "automation", "manager", "you"]
    assert [t["name"] for t in thread] == ["decision.verdict-r1", "decision.q-G", "reply.F", "reply.E",
                                           "decision.pane-D", "decision.msg-C2", "decision.msg-C", "decision.msg-B",
                                           "decision.msg-A"]
    by_name = {t["name"]: t for t in thread}
    assert by_name["reply.E"]["text"] == "Holding the merge.\nmore" and by_name["reply.E"]["re"] == "decision.msg-A"
    assert by_name["decision.msg-A"]["text"] == "please hold\nsecond" and by_name["decision.msg-A"]["re"] is None
    assert by_name["decision.q-G"]["text"] == "the first"
    assert by_name["decision.pane-D"]["text"] == "flip it"
    assert by_name["decision.verdict-r1"]["text"] == '{"decision": "approved", "feedback": ""}'


def test_decisions_and_outputs_no_longer_carry_the_thread_classes(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _thread_scene(tmp_path, monkeypatch)
    view = board.topic_view("W1")
    assert [d["name"] for d in view["decisions"]] == ["gate-1"]
    assert not [a for a in view["outputs"] if a["phase"] == "reply"]


def test_render_topic_prints_the_thread(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _thread_scene(tmp_path, monkeypatch)
    text = board.render_topic(board.topic_view("W1"))
    assert text.index("## Thread") < text.index("## Summary")
    thread = text.split("## Thread\n")[1].split("\n## Summary")[0].splitlines()
    assert thread[0].endswith("  you: gate verdict r1: {\"decision\": \"approved\", \"feedback\": \"\"}")
    assert thread[-1].endswith("  you: please hold")
    assert "  node design: Holding the merge." in text and "  topic agent: The agent says hi." in text


def test_inbox_lists_a_reply_to_the_engineers_message_until_seen(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _thread_scene(tmp_path, monkeypatch)
    replies = [i for i in board.inbox()["waiting"] if i["kind"] == "reply"]
    assert [(i["key"], i["text"], i["priority"]) for i in replies] == [("W1", "Holding the merge.", "P1")]
    seen_at = ms.artifact_get_impl("W1", "reply", "E")["written_at"]
    assert [i for i in board.inbox(seen={"W1": seen_at + 1})["waiting"] if i["kind"] == "reply"] == []
    assert [i for i in board.inbox(seen={"W1": seen_at - 1})["waiting"] if i["kind"] == "reply"] != []
    old = time.time() - 8 * 86400
    path = paths.artifact_path("W1", "reply", "E")
    path.write_text(path.read_text().replace(f"written_at: {seen_at}", f"written_at: {old}"))
    assert [i for i in board.inbox()["waiting"] if i["kind"] == "reply"] == []


def test_a_due_note_is_not_an_inbox_item(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _scene(tmp_path, monkeypatch)
    kinds = {i["kind"] for i in board.inbox()["waiting"]}
    assert not any(k.startswith("review-by") for k in kinds)
    assert RENEW in {n["key"] for n in board.topics("general", "note", "open")["notes"]}
    assert "Renew the cert" not in board.render_inbox(board.inbox())


def test_notebook_warnings_come_separately_from_probe_warnings(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _scene(tmp_path, monkeypatch)
    nm.notebook_path("general").write_text(GENERAL.replace("- when: later", "- when: much later"))
    line = "⚠ notebook general changed after migration: 1 — run dockwright notebook-migrate general"
    for data in (board.inbox(), board.topics()):
        assert line in data["notebook_warnings"] and not any("notebook" in w for w in data["warnings"])
    monkeypatch.setattr(board, "_process_index", lambda: None, raising=False)


def test_render_inbox_and_render_topics_print_both_warning_lists(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _scene(tmp_path, monkeypatch)
    data = board.inbox()
    data["warnings"], data["notebook_warnings"] = ["⚠ probe failed: ps"], ["⚠ notebook general: x"]
    text = board.render_inbox(data)
    assert text.splitlines()[:2] == ["⚠ probe failed: ps", "⚠ notebook general: x"]
    data = board.topics()
    data["warnings"], data["notebook_warnings"] = ["⚠ probe failed: ps"], ["⚠ notebook general: x"]
    assert board.render_topics(data).splitlines()[:2] == ["⚠ probe failed: ps", "⚠ notebook general: x"]


def test_domains_are_the_live_managers_and_the_cards_touched_in_fourteen_days(fresh_orchestrator_dir, tmp_path,
                                                                             monkeypatch):
    _scene(tmp_path, monkeypatch)
    _session("sid-ops", P_MP + 50, agent="manager", parent=None, domain="ops", name="mgr-ops")
    monkeypatch.setattr(ms, "_pid_reads_alive", lambda pid: pid in (P_M, P_MP, P_MP + 50))
    for key, domain, days in (("OLD-D", "old", 15), ("FRESH-D", "fresh", 13)):
        state.write_json_atomic(paths.topic_path(key), topic.new_card("note", domain, "t"))
        state.append_event(paths.artifact_events_path(key), {"type": "artifact_put", "phase": "x", "name": "y"})
        at = time.time() - days * 86400
        os.utime(paths.artifact_events_path(key), (at, at))
    nm.notebook_path("stale").write_text("# stale\n")
    found = board.domains()
    assert "ops" in found and "fresh" in found and "general" in found and "product" in found
    assert "old" not in found and "stale" not in found and "x-archive" not in found


def test_a_reply_put_with_the_default_scope_still_gets_its_inbox_card(fresh_orchestrator_dir, tmp_path, monkeypatch):
    from dockwright import topic_message
    _scene(tmp_path, monkeypatch)
    _put("W1", "decision", "msg-A", topic_message.body("please hold", "engineer", "board"), decided_by="engineer")
    time.sleep(0.002)
    ms.artifact_put_impl("W1", "reply", "E", "re: decision.msg-A\nHolding the merge.", "complete", "sid-q")
    assert [i["text"] for i in board.inbox()["waiting"] if i["kind"] == "reply"] == ["Holding the merge."]


def test_a_reply_card_does_not_hide_the_rows_own_human_wait(fresh_orchestrator_dir, tmp_path, monkeypatch):
    from dockwright import topic_message
    _probes(monkeypatch)
    _seed_snapshot("W11", tmp_path)
    _holder("W11", "sid-h", "a-design")
    monkeypatch.setattr(waiting_on, "token_value", lambda facts, now=None: "human:review")
    _put("W11", "decision", "msg-A", topic_message.body("please hold", "engineer", "board"), writer="sid-h",
         decided_by="engineer")
    _put("W11", "reply", "E", "re: decision.msg-A\nHolding.", writer="sid-h")
    kinds = [(i["kind"], i["topic"]) for i in board.inbox()["waiting"]]
    assert ("review", "W11") in kinds and ("reply", "W11") in kinds
