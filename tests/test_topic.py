import asyncio
import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

from dockwright import mcp_server as ms
from dockwright import paths, state, topic
from dockwright.mcp_server import (_prune_stale_artifacts, _read_events, artifact_put_impl, pipeline_advance_impl,
                                   pipeline_rework_impl, pipeline_start_impl, register_self_impl)
from tests.test_pipeline_advance import (_complete_design_outputs, _events, _fake_registration, _fold, _setup_schema,
                                         fresh_orchestrator_dir)
from tests.test_pipeline_rework import _dead, _other_schema, _register


def _start(monkeypatch, tmp_path, task_key="KEY-12", domain="meta", ask="# Fix the thing\n\nmore", cwd="/w"):
    _setup_schema(monkeypatch, tmp_path)
    register_self_impl("m1", "manager", "mgr", "/tmp", "", pid=None, domain=domain)
    _fake_registration(monkeypatch, ["registered"])
    return asyncio.run(pipeline_start_impl("m1", task_key, "code-ticket", cwd, ask))


def _topic_events(task_key):
    return [e for e in _read_events(paths.artifact_events_path(task_key)) if e["type"] == "topic_set"]


def test_a_started_work_gets_its_card(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _start(monkeypatch, tmp_path)
    card = topic.read("KEY-12")
    assert card == {"kind": "code-ticket", "domain": "meta", "title": "Fix the thing", "priority": "P2",
                    "blocked_by": [], "links": {"pr": None, "ticket": "KEY-12", "worktree": "/w"},
                    "created_at": card["created_at"]}
    assert isinstance(card["created_at"], float)


@pytest.mark.parametrize("task_key,ticket", [("KEY-12", "KEY-12"), ("A1-7", "A1-7"), ("dash-a1", None),
                                             ("key-12", None), ("KEY-12-x", None)])
def test_only_a_ticket_shaped_key_gets_a_ticket_link(fresh_orchestrator_dir, tmp_path, monkeypatch, task_key, ticket):
    _start(monkeypatch, tmp_path, task_key=task_key)
    assert topic.read(task_key)["links"]["ticket"] == ticket


@pytest.mark.parametrize("kind", topic.TOKENLESS_KINDS)
def test_a_schema_named_like_a_tokenless_kind_is_refused_before_any_write(fresh_orchestrator_dir, tmp_path,
                                                                         monkeypatch, kind):
    schema_path = _setup_schema(monkeypatch, tmp_path)
    (schema_path.parent / f"{kind}.toml").write_text(schema_path.read_text())
    register_self_impl("m1", "manager", "mgr", "/tmp", "", pid=None, domain="meta")
    with pytest.raises(ValueError, match="tokenless"):
        asyncio.run(pipeline_start_impl("m1", "K", kind, "/w", "ask"))
    assert not paths.artifact_ticket_dir("K").exists()


def test_an_unregistered_caller_is_refused_before_the_schema_is_read(fresh_orchestrator_dir, monkeypatch):
    def unreachable(schema, cwd):
        raise AssertionError("schema read before the caller check")

    monkeypatch.setattr(ms, "_schema_snapshot_text", unreachable)
    with pytest.raises(ValueError, match="does not match any active"):
        asyncio.run(pipeline_start_impl("nobody", "K", "note", "/w", "ask"))


def test_a_failed_card_write_does_not_fail_the_start(fresh_orchestrator_dir, tmp_path, monkeypatch):
    def boom(task_key, fn):
        raise OSError("disk full")

    monkeypatch.setattr(topic, "update", boom)
    result = _start(monkeypatch, tmp_path)
    assert result["ok"] is True and result["state"] == "committed"
    assert topic.read("KEY-12") is None


def test_the_start_keeps_an_existing_cards_priority_and_title(fresh_orchestrator_dir, tmp_path, monkeypatch):
    state.write_json_atomic(paths.topic_path("KEY-12"), {**topic.new_card("code-ticket", "x", "old"), "priority": "P0"})
    _start(monkeypatch, tmp_path)
    assert topic.read("KEY-12")["priority"] == "P0" and topic.read("KEY-12")["title"] == "old"


def test_the_start_turns_an_existing_tokenless_card_into_a_work_card(fresh_orchestrator_dir, tmp_path, monkeypatch):
    note = topic.new_card("note", "x", "old", worktree="/elsewhere")
    state.write_json_atomic(paths.topic_path("KEY-12"), {**note, "priority": "P0"})
    _start(monkeypatch, tmp_path)
    card = topic.read("KEY-12")
    assert (card["kind"], card["links"]["worktree"], card["priority"], card["title"]) == ("code-ticket", "/w", "P0",
                                                                                          "old")
    assert "status" not in card


def test_a_work_without_a_card_file_reads_a_built_card(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _start(monkeypatch, tmp_path)
    paths.topic_path("KEY-12").unlink()
    card = topic.load("KEY-12")
    assert card["stored"] is False
    assert (card["kind"], card["domain"], card["title"], card["priority"]) == ("code-ticket", "meta", "Fix the thing",
                                                                              "P2")
    assert card["links"] == {"pr": None, "ticket": "KEY-12", "worktree": "/w"}
    assert topic.load("NOPE-1") is None
    assert topic.load("KEY-12") is not None and not paths.topic_path("KEY-12").exists()


def test_a_stored_card_loads_as_stored(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _start(monkeypatch, tmp_path)
    assert topic.load("KEY-12")["stored"] is True


@pytest.mark.parametrize("priority,waiting,shown", [("P2", "human:question", "P1"), ("P1", "human:gate", "P0"),
                                                    ("P0", "human:approval-dialog", "P0"), ("P2", "worker:design", "P2"),
                                                    ("P3", None, "P3"), ("P3", "external:merge", "P3")])
def test_waiting_on_a_human_shows_one_step_higher(priority, waiting, shown):
    assert topic.shown_priority(priority, waiting) == shown


def test_the_topic_line():
    view = {"kind": "k", "domain": "d", "priority": "P2", "priority_shown": "P1", "title": "t"}
    assert ms._topic_line(view) == "topic: k · d · P2 (shown P1) · t"
    assert ms._topic_line({**view, "priority_shown": "P2"}) == "topic: k · d · P2 · t"
    assert ms._topic_line(None) == "topic: none"


def test_status_prints_the_card_in_text_and_json(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _start(monkeypatch, tmp_path)
    assert ms.pipeline_status_impl("KEY-12").splitlines()[2] == "topic: code-ticket · meta · P2 · Fix the thing"
    out = json.loads(ms.pipeline_status_impl("KEY-12", "json"))
    assert out["version"] == 1 and list(out)[:4] == ["version", "task_key", "waiting_on", "topic"]
    assert out["topic"]["priority_shown"] == "P2" and out["topic"]["stored"] is True
    assert out["topic"]["links"]["ticket"] == "KEY-12"


def test_a_work_waiting_on_a_human_shows_the_bump_and_keeps_the_file(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _start(monkeypatch, tmp_path)
    real = ms._waiting_on_status
    monkeypatch.setattr(ms, "_waiting_on_status", lambda *a: {**real(*a), "waiting_on": "human:question"})
    assert ms.pipeline_status_impl("KEY-12").splitlines()[2] == ("topic: code-ticket · meta · P2 (shown P1) · "
                                                                 "Fix the thing")
    assert json.loads(ms.pipeline_status_impl("KEY-12", "json"))["topic"]["priority_shown"] == "P1"
    assert topic.read("KEY-12")["priority"] == "P2"


def test_status_of_a_key_with_no_card_and_no_start_says_none(fresh_orchestrator_dir):
    artifact_put_impl("LOOSE", "spec", "a", "x", "complete", "s1")
    assert ms.pipeline_status_impl("LOOSE").splitlines()[2] == "topic: none"
    assert json.loads(ms.pipeline_status_impl("LOOSE", "json"))["topic"] is None


def test_topic_set_changes_fields_and_logs_the_change(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _start(monkeypatch, tmp_path)
    r = ms.topic_set_impl("KEY-12", "m1", priority="P1", links={"pr": "https://example.test/pull/3"},
                          blocked_by=["dash-a2", "dash-a2"])
    assert r["ok"] is True and r["changed"] == ["blocked_by", "links", "priority"]
    card = topic.read("KEY-12")
    assert card["priority"] == "P1" and card["blocked_by"] == ["dash-a2"]
    assert card["links"] == {"pr": "https://example.test/pull/3", "ticket": "KEY-12", "worktree": "/w"}
    assert r["topic"] == card
    assert [(e["actor_sid"], e["changed"]) for e in _topic_events("KEY-12")] == [
        ("m1", ["blocked_by", "links", "priority"])]
    assert ms.topic_set_impl("KEY-12", "m1", links={"pr": None})["changed"] == ["links"]
    assert topic.read("KEY-12")["links"]["pr"] is None


def test_topic_set_with_no_change_writes_no_event(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _start(monkeypatch, tmp_path)
    assert ms.topic_set_impl("KEY-12", "m1", priority="P2")["changed"] == []
    assert _topic_events("KEY-12") == []


@pytest.mark.parametrize("kwargs,match", [
    ({"priority": "P4"}, "priority must be one of"),
    ({"title": "  "}, "title must not be blank"),
    ({"domain": "a/b"}, "domain"),
    ({"status": "closed"}, "status belongs to"),
    ({"blocked_by": ["a b"]}, "blocked_by"),
    ({"blocked_by": "dash-a2"}, "blocked_by"),
    ({"links": {"board": "X"}}, "unknown link"),
    ({"links": {"pr": 3}}, "link pr"),
    ({"links": {"worktree": "/elsewhere"}}, "comes from its token"),
])
def test_topic_set_refuses_a_bad_field(fresh_orchestrator_dir, tmp_path, monkeypatch, kwargs, match):
    _start(monkeypatch, tmp_path)
    before = topic.read("KEY-12")
    with pytest.raises(ValueError, match=match):
        ms.topic_set_impl("KEY-12", "m1", **kwargs)
    assert topic.read("KEY-12") == before and _topic_events("KEY-12") == []


def test_topic_set_refuses_an_unknown_key_and_a_missing_actor(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="no topic card"):
        ms.topic_set_impl("NOPE-1", "m1", priority="P1")
    with pytest.raises(ValueError, match="actor_sid"):
        ms.topic_set_impl("NOPE-1", "", priority="P1")


def test_topic_set_writes_a_built_card_with_the_change(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _start(monkeypatch, tmp_path)
    paths.topic_path("KEY-12").unlink()
    ms.topic_set_impl("KEY-12", "m1", priority="P0")
    card = json.loads(paths.topic_path("KEY-12").read_text())
    assert card["priority"] == "P0" and card["kind"] == "code-ticket" and "stored" not in card


def test_topic_set_closes_a_note(fresh_orchestrator_dir):
    state.write_json_atomic(paths.topic_path("note-meta-a"), topic.new_card("note", "meta", "a"))
    assert ms.topic_set_impl("note-meta-a", "m1", status="closed")["changed"] == ["status"]
    assert topic.read("note-meta-a")["status"] == "closed"


def test_topic_set_sets_a_notes_worktree_link(fresh_orchestrator_dir):
    state.write_json_atomic(paths.topic_path("note-meta-a"), topic.new_card("note", "meta", "a"))
    assert ms.topic_set_impl("note-meta-a", "m1", links={"worktree": "/repo"})["changed"] == ["links"]
    assert topic.read("note-meta-a")["links"]["worktree"] == "/repo"


def test_a_title_is_kept_on_one_line(fresh_orchestrator_dir, tmp_path, monkeypatch):
    assert topic.new_card("note", "meta", " a\n\tb  c ")["title"] == "a b c"
    _start(monkeypatch, tmp_path)
    ms.topic_set_impl("KEY-12", "m1", title="Fix X\nsee the PR")
    assert topic.read("KEY-12")["title"] == "Fix X see the PR"
    assert ms.pipeline_status_impl("KEY-12").splitlines()[2:4] == ["topic: code-ticket · meta · P2 · Fix X see the PR",
                                                                  ""]


@pytest.mark.parametrize("status", ["done", "Closed", ""])
def test_a_tokenless_card_refuses_an_unknown_status(status):
    with pytest.raises(ValueError, match="status must be one of"):
        topic.new_card("note", "meta", "t", status=status)


def test_a_card_missing_a_link_key_reports_no_change_on_a_no_op(fresh_orchestrator_dir):
    state.write_json_atomic(paths.topic_path("K"), {**topic.new_card("note", "meta", "t"), "links": {"pr": None}})
    assert ms.topic_set_impl("K", "m1", priority="P2")["changed"] == []
    assert _topic_events("K") == []


def test_a_topic_cannot_block_itself(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _start(monkeypatch, tmp_path)
    before = topic.read("KEY-12")
    with pytest.raises(ValueError, match="cannot block itself"):
        ms.topic_set_impl("KEY-12", "m1", blocked_by=["dash-a2", "KEY-12"])
    assert topic.read("KEY-12") == before and _topic_events("KEY-12") == []


def test_the_topic_set_tool_passes_its_fields(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _start(monkeypatch, tmp_path)
    assert ms.topic_set(task_key="KEY-12", actor_sid="m1", title="New")["changed"] == ["title"]


def test_pipeline_event_refuses_the_topic_set_type(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="reserved"):
        ms.pipeline_event_impl("KEY-12", "topic_set")


def _dir(tmp_path, name="moved"):
    d = tmp_path / name
    d.mkdir()
    return str(d)


def _dressed():
    ms.topic_set_impl("K", "m1", priority="P1", title="Kept", blocked_by=["dash-a2"],
                      links={"pr": "https://example.test/pull/3"})
    return topic.read("K")


def _worker_advance(sid, cwd):
    _complete_design_outputs(sid)
    return asyncio.run(pipeline_advance_impl(sid, "K", "task", cwd=cwd))


def _override_advance(sid, cwd):
    return asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="move", cwd=cwd))


@pytest.mark.parametrize("advance", [_worker_advance, _override_advance])
def test_an_advance_that_moves_the_cwd_moves_only_the_card_worktree(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                    advance):
    sid = _start(monkeypatch, tmp_path, task_key="K")["next"]["sid"]
    before = _dressed()
    moved = _dir(tmp_path)
    _fake_registration(monkeypatch, ["registered"])
    assert advance(sid, moved)["state"] == "committed"
    assert topic.read("K") == {**before, "links": {**before["links"], "worktree": moved}}


def test_a_pending_cwd_move_reaches_the_card_only_when_it_commits(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid = _start(monkeypatch, tmp_path, task_key="K")["next"]["sid"]
    before = _dressed()
    moved = _dir(tmp_path)
    _fake_registration(monkeypatch, ["no_register"])
    r = _worker_advance(sid, moved)
    assert r["state"] == "pending" and topic.read("K") == before
    late = {"status": "registered", "assignment_id": "a-late", "claude_sid": "sid-late", "name": "late"}
    assert ms._commit_or_pend("K", r["request_id"], "t1", "task", 1, late, None, reconciled=True)["state"] == "committed"
    assert _fold().tokens["t1"].cwd == moved
    assert topic.read("K") == {**before, "links": {**before["links"], "worktree": moved}}


def test_a_cwd_move_an_override_supersedes_never_reaches_the_card(fresh_orchestrator_dir, tmp_path, monkeypatch):
    work = _dir(tmp_path, "work")
    sid = _start(monkeypatch, tmp_path, task_key="K", cwd=work)["next"]["sid"]
    before = _dressed()
    _fake_registration(monkeypatch, ["no_register"])
    assert _worker_advance(sid, _dir(tmp_path))["state"] == "pending"
    _fake_registration(monkeypatch, ["registered"])
    assert _override_advance(sid, None)["state"] == "committed"
    assert _fold().tokens["t1"].cwd == work and topic.read("K") == before
    paths.topic_path("K").unlink()
    assert topic.load("K")["links"]["worktree"] == work


def test_a_refused_advance_with_a_cwd_leaves_the_card_alone(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid = _start(monkeypatch, tmp_path, task_key="K")["next"]["sid"]
    before = _dressed()
    _fake_registration(monkeypatch, [])
    with pytest.raises(ValueError, match="pipeline_advance refused"):
        asyncio.run(pipeline_advance_impl(sid, "K", "task", cwd=_dir(tmp_path)))
    assert _events()[-1]["type"] == "advance_refused" and topic.read("K") == before


def test_an_advance_never_creates_a_card_and_the_built_card_follows_the_move(fresh_orchestrator_dir, tmp_path,
                                                                            monkeypatch):
    sid = _start(monkeypatch, tmp_path, task_key="K")["next"]["sid"]
    paths.topic_path("K").unlink()
    moved = _dir(tmp_path)
    _fake_registration(monkeypatch, ["registered"])
    _worker_advance(sid, moved)
    assert not paths.topic_path("K").exists()
    card = topic.load("K")
    assert (card["stored"], card["kind"], card["links"]["worktree"]) == (False, "code-ticket", moved)
    assert _fold().tokens["t1"].cwd == moved


def test_a_failed_card_write_does_not_fail_the_advance(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid = _start(monkeypatch, tmp_path, task_key="K")["next"]["sid"]
    moved = _dir(tmp_path)

    def boom(task_key, fn):
        raise OSError("disk full")

    monkeypatch.setattr(topic, "update", boom)
    _fake_registration(monkeypatch, ["registered"])
    assert _worker_advance(sid, moved)["state"] == "committed"
    assert _fold().tokens["t1"].cwd == moved and topic.read("K")["links"]["worktree"] == "/w"


def _started_for_rework(monkeypatch, tmp_path):
    work = _dir(tmp_path, "work")
    _start(monkeypatch, tmp_path, task_key="K", cwd=work)
    _other_schema(tmp_path)
    _dead("sid-K-design")
    return work


@pytest.mark.parametrize("schema,to,move", [("other", "explain", True), ("other", "explain", False),
                                            (None, "design", True)])
def test_a_rework_moves_the_card_kind_and_worktree(fresh_orchestrator_dir, tmp_path, monkeypatch, schema, to, move):
    work = _started_for_rework(monkeypatch, tmp_path)
    before = _dressed()
    cwd = _dir(tmp_path) if move else None
    _register(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_rework_impl("m1", "K", to, "redo it", schema=schema, cwd=cwd))
    assert r["state"] == "committed" and r["round"] == 2
    assert topic.read("K") == {**before, "kind": schema or "code-ticket",
                               "links": {**before["links"], "worktree": cwd or work}}


def test_a_pending_rework_moves_the_card_kind_with_the_token(fresh_orchestrator_dir, tmp_path, monkeypatch):
    work = _started_for_rework(monkeypatch, tmp_path)
    before = _dressed()
    _register(monkeypatch, ["no_register"])
    r = asyncio.run(pipeline_rework_impl("m1", "K", "explain", "redo it", schema="other", cwd=_dir(tmp_path)))
    assert r["state"] == "pending" and _fold().tokens["t1"].cwd == work
    assert topic.read("K") == {**before, "kind": "other"}


def test_a_refused_rework_leaves_the_card_alone(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_for_rework(monkeypatch, tmp_path)
    before = _dressed()
    _register(monkeypatch, [])
    with pytest.raises(ValueError, match="needs 'plan.tasks'"):
        asyncio.run(pipeline_rework_impl("m1", "K", "task", "redo it", schema="other", cwd=_dir(tmp_path)))
    assert topic.read("K") == before


def test_a_rework_never_creates_a_card_and_the_built_card_follows_it(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_for_rework(monkeypatch, tmp_path)
    paths.topic_path("K").unlink()
    moved = _dir(tmp_path)
    _register(monkeypatch, ["registered"])
    asyncio.run(pipeline_rework_impl("m1", "K", "explain", "redo it", schema="other", cwd=moved))
    assert not paths.topic_path("K").exists()
    card = topic.load("K")
    assert (card["stored"], card["kind"], card["links"]["worktree"]) == (False, "other", moved)


def test_a_failed_card_write_does_not_fail_the_rework(fresh_orchestrator_dir, tmp_path, monkeypatch):
    work = _started_for_rework(monkeypatch, tmp_path)

    def boom(task_key, fn):
        raise OSError("disk full")

    monkeypatch.setattr(topic, "update", boom)
    _register(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_rework_impl("m1", "K", "explain", "redo it", schema="other", cwd=_dir(tmp_path)))
    assert r["state"] == "committed" and r["round"] == 2
    assert topic.read("K")["kind"] == "code-ticket" and topic.read("K")["links"]["worktree"] == work


def _schema_named(tmp_path, name, source):
    d = tmp_path / "schemas"
    (d / f"{name}.toml").write_text((d / f"{source}.toml").read_text())


def _tree(task_key="K"):
    d = paths.artifact_ticket_dir(task_key)
    return {str(p.relative_to(d)): p.read_bytes() if p.is_file() else None for p in sorted(d.rglob("*"))
            if p.name != "events.jsonl"}


@pytest.mark.parametrize("kind", topic.TOKENLESS_KINDS)
def test_a_rework_onto_a_schema_named_like_a_tokenless_kind_is_refused_before_any_write(fresh_orchestrator_dir,
                                                                                        tmp_path, monkeypatch, kind):
    _started_for_rework(monkeypatch, tmp_path)
    _schema_named(tmp_path, kind, "other")
    before, tree, seen = _dressed(), _tree(), len(_events())
    _register(monkeypatch, ["registered"])
    with pytest.raises(ValueError, match="tokenless"):
        asyncio.run(pipeline_rework_impl("m1", "K", "explain", "redo it", schema=kind, cwd=_dir(tmp_path)))
    assert [e["type"] for e in _events()[seen:]] == ["advance_refused"]
    assert _tree() == tree and topic.read("K") == before and _fold().round == 1


@pytest.mark.parametrize("kind", topic.TOKENLESS_KINDS)
def test_a_resnapshot_of_a_work_started_on_a_tokenless_name_is_refused_before_any_write(fresh_orchestrator_dir,
                                                                                        tmp_path, monkeypatch, kind):
    _start(monkeypatch, tmp_path, task_key="K")
    _schema_named(tmp_path, kind, "code-ticket")
    journal = paths.artifact_events_path("K")
    journal.write_text("".join(json.dumps({**e, "schema": kind} if e["type"] == "pipeline_started" else e) + "\n"
                               for e in _events()))
    before, tree, seen = _dressed(), _tree(), len(_events())
    _fake_registration(monkeypatch, ["registered"])
    with pytest.raises(ValueError, match="tokenless"):
        asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="move", resnapshot=True))
    assert [e["type"] for e in _events()[seen:]] == ["advance_refused"]
    assert _tree() == tree and topic.read("K") == before


def _edit_in_child(artifacts, field, value, barrier):
    paths.ARTIFACTS = Path(artifacts)

    def slow(card):
        time.sleep(0.3)
        return {**card, field: value}

    barrier.wait(30)
    topic.update("K", slow)


def test_two_processes_editing_one_card_keep_both_changes(fresh_orchestrator_dir):
    state.write_json_atomic(paths.topic_path("K"), topic.new_card("note", "meta", "t"))
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    procs = [ctx.Process(target=_edit_in_child, args=(str(paths.ARTIFACTS), field, value, barrier))
             for field, value in (("priority", "P1"), ("title", "u"))]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    card = topic.read("K")
    assert (card["priority"], card["title"]) == ("P1", "u")


def _age(task_key, days):
    old = time.time() - days * 86400
    d = paths.artifact_ticket_dir(task_key)
    for p in [*d.rglob("*"), d]:
        os.utime(p, (old, old))


def test_prune_keeps_open_tokenless_topics_and_drops_closed_ones(fresh_orchestrator_dir):
    for key, kind, status in (("note-a", "note", "open"), ("fleet-a", "fleet", "open"), ("note-b", "note", "closed"),
                              ("plain", "code-ticket", None)):
        artifact_put_impl(key, "context", "ask", "x", "complete", "cli:test", scope="output")
        state.write_json_atomic(paths.topic_path(key), topic.new_card(kind, "a", "t", status=status))
        _age(key, 31)
    _prune_stale_artifacts()
    assert paths.artifact_ticket_dir("note-a").exists() and paths.artifact_ticket_dir("fleet-a").exists()
    assert not paths.artifact_ticket_dir("note-b").exists() and not paths.artifact_ticket_dir("plain").exists()
