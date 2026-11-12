import asyncio
import fcntl
import json
import time

import pytest

from dockwright import board, hooks, paths, pipeline, state, topic, topic_agent, topic_message, topic_outbox
from dockwright import mcp_server as ms
from dockwright import pipeline_reconcile as pr
from dockwright.stale_monitor import HUMAN_ATTACHED_TTL_SEC
from tests.test_board import _tree
from tests.test_human_attached import BUSY_PANE, IDLE_PANE
from tests.test_mcp_tools import _probes
from tests.test_pipeline_core import FORK, SCHEMA
from tests.test_pipeline_reconcile import (MGR, _at_post_merge, _at_pr_with_a_pr_url, _close_session,  # noqa: F401
                                           _closed, _closes, _complete_design_outputs, _done, _fake_registration,
                                           _fake_resume_tab, _fold, _later, _manager, _of, _register_worker,
                                           _started_at_design, clock, fresh_orchestrator_dir, killed, session_logs)
from tests.test_store_gate import VERDICT


def _events(key):
    return [e for e in ms._read_events(paths.artifact_events_path(key)) if e.get("type") == "message_delivered"]


def _pane(monkeypatch, ok=True):
    pane = {"ok": ok, "typed": []}

    def typed(wid, text):
        pane["typed"].append(text)
        return pane["ok"]

    monkeypatch.setattr(ms, "_resolve_live_worker_window", lambda record: "%p1")
    monkeypatch.setattr(ms, "_type_unguarded", typed)
    return pane


def _agent(monkeypatch, result=None):
    calls = []

    async def fake(key, record):
        calls.append((key, record))
        return result or {"route": "agent", "ok": True, "reason": None}

    monkeypatch.setattr(topic_agent, "spawn", fake)
    return calls


def _note(key="N-1", domain="general"):
    state.write_json_atomic(paths.topic_path(key), topic.new_card("note", domain, "a note"))
    return key


def _no_input_wait(monkeypatch):
    async def ready(window_id, runtime):
        return None

    monkeypatch.setattr(ms, "_await_input_ready", ready)


def _entry(key, record):
    return state.read_json(topic_outbox.entry_path(key, record))


def _attach(sid, at=None):
    record = state.read_json(paths.ACTIVE / f"{sid}.json")
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json",
                            {**record, "human_attached_at": time.time() if at is None else at})


def _screen(monkeypatch, text):
    screen = {"text": text}
    monkeypatch.setattr(ms, "_capture_text", lambda wid: screen["text"])
    return screen


def test_a_failed_pane_send_still_leaves_the_record_the_event_and_the_queue(fresh_orchestrator_dir, monkeypatch,
                                                                          tmp_path):
    _started_at_design(monkeypatch, tmp_path)
    _pane(monkeypatch, ok=False)
    out = asyncio.run(topic_message.deliver("K", "stop and wait", decided_by="engineer", via="cli"))
    assert out["ok"] and not out["delivered"] and out["record"].startswith("decision.msg-")
    body = ms.artifact_get_impl("K", "decision", out["record"].split(".", 1)[1])
    assert body["content"].startswith("stop and wait\n\nfrom: engineer\nvia: cli")
    assert body["decided_by"] == "engineer"
    assert [e["ok"] for e in _events("K")] == [False]
    assert [e["record"] for e in topic_outbox.entries()] == [out["record"]]


def test_the_live_holder_gets_the_marked_line(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _started_at_design(monkeypatch, tmp_path)
    pane = _pane(monkeypatch)
    out = asyncio.run(topic_message.deliver("K", "hello", decided_by="engineer", via="board"))
    assert out["delivered"] and out["route"] == "pane"
    assert pane["typed"] == [f"[TOPIC {out['record']} from engineer] hello"]
    assert topic_outbox.entries() == []


def test_a_long_text_is_cut_in_the_pane_and_kept_whole_in_the_record(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _started_at_design(monkeypatch, tmp_path)
    pane = _pane(monkeypatch)
    out = asyncio.run(topic_message.deliver("K", "x" * 2500, decided_by="engineer", via="cli"))
    assert pane["typed"][0].endswith(f"x … (full text: artifact_get {out['record']})")
    assert "x" * 2500 in ms.artifact_get_impl("K", "decision", out["record"].split(".", 1)[1])["content"]


def test_a_second_message_in_the_same_second_gets_a_suffix(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _started_at_design(monkeypatch, tmp_path)
    monkeypatch.setattr(topic_message.time, "time", lambda: 1_790_000_000.0)
    monkeypatch.setattr(topic_message, "_route", lambda *a, **k: asyncio.sleep(0, {"route": "pane", "ok": True,
                                                                                   "reason": None}))
    first = asyncio.run(topic_message.deliver("K", "one", via="cli"))
    second = asyncio.run(topic_message.deliver("K", "two", via="cli"))
    assert second["record"] == first["record"] + "-2"


def test_the_topic_marker_is_automation_not_the_engineer_typing():
    assert hooks._looks_automated("[TOPIC decision.msg-20260930T101010 from engineer] hi")


def test_the_delivery_event_is_reserved_and_folds_to_nothing(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _started_at_design(monkeypatch, tmp_path)
    assert "message_delivered" in pipeline.RESERVED_EVENT_TYPES
    events = ms._read_events(paths.artifact_events_path("K"))
    extra = events + [{"type": "message_delivered", "record": "decision.msg-1", "kind": "message",
                       "route": "pane", "ok": True, "reason": None, "via": "cli"}]
    assert pipeline.fold(extra).tokens == pipeline.fold(events).tokens
    with pytest.raises(ValueError):
        ms.pipeline_event_impl("K", "message_delivered")


def test_the_cli_writes_as_the_engineer_only_outside_an_agent_session(monkeypatch):
    monkeypatch.delenv("CLAUDE_AGENT", raising=False)
    assert topic_message.decider() == "engineer"
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    assert topic_message.decider() == "manager"


def test_the_mcp_tool_runs_inside_a_live_event_loop(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _started_at_design(monkeypatch, tmp_path)
    _pane(monkeypatch)
    asyncio.run(ms.mcp.call_tool("topic_message", {"task_key": "K", "text": "hi"}))
    assert _events("K")[0]["via"] == "mcp"
    assert ms.artifact_get_impl("K", "decision", _events("K")[0]["record"].split(".", 1)[1])["decided_by"] == "manager"


def test_a_closed_holder_with_a_transcript_is_resumed(fresh_orchestrator_dir, monkeypatch, tmp_path):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _close_session(sid, "K-design")
    tabs = _fake_resume_tab(monkeypatch)
    _no_input_wait(monkeypatch)
    pane = _pane(monkeypatch)
    asyncio.run(ms.mcp.call_tool("topic_message", {"task_key": "K", "text": "wake up"}))
    [event] = _events("K")
    assert (event["route"], event["ok"]) == ("resume", True)
    assert [t["resume_sid"] for t in tabs] == [sid]
    assert pane["typed"] == [f"[TOPIC {event['record']} from manager] wake up"]


def test_a_dead_holder_without_a_resume_is_restarted(fresh_orchestrator_dir, monkeypatch, tmp_path):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    (paths.ACTIVE / f"{sid}.json").unlink()
    state.write_json_atomic(paths.CLOSED / f"{sid}.json", {"claude_sid": sid, "name": "K-design",
                                                           "closed_reason": "session_end",
                                                           "closed_at": time.time() - 1000})
    monkeypatch.setattr(ms, "_has_live_transcript", lambda sid, runtime="claude": False)
    calls = _fake_registration(monkeypatch, ["registered"])
    asyncio.run(ms.mcp.call_tool("topic_message", {"task_key": "K", "text": "carry on"}))
    [event] = _events("K")
    assert (event["route"], event["ok"]) == ("restart", True)
    restarts = [e for e in ms._read_events(paths.artifact_events_path("K"))
                if e.get("type") == "advance_requested" and e.get("kind") == "restart"]
    assert len(restarts) == 1
    assert event["record"] in calls[-1]["initial_prompt"]
    assert _fold().tokens["t1"].holder != _ and topic_outbox.entries() == []


def test_a_held_token_queues_the_message(fresh_orchestrator_dir, monkeypatch, tmp_path):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    asyncio.run(ms.pipeline_advance_impl("m1", "K", "?", reason="engineer away"))
    (paths.ACTIVE / f"{sid}.json").unlink()
    calls = _fake_registration(monkeypatch, [])
    out = asyncio.run(topic_message.deliver("K", "hi", via="cli"))
    assert (out["delivered"], out["reason"]) == (False, "held")
    assert _entry("K", out["record"])["reason"] == "held" and calls == []


def test_a_node_with_its_outputs_done_is_unadvanced(fresh_orchestrator_dir, monkeypatch, tmp_path):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    (paths.ACTIVE / f"{sid}.json").unlink()
    calls = _fake_registration(monkeypatch, [])
    out = asyncio.run(topic_message.deliver("K", "hi", via="cli"))
    assert (out["delivered"], out["reason"]) == (False, "unadvanced")
    assert _entry("K", out["record"])["reason"] == "unadvanced" and calls == []


def _forked(monkeypatch, tmp_path, outcomes):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    for name, body in (("context.ask", "x"), ("design.hld", "x"),
                       ("plan.tasks", '{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b"}]}')):
        phase, n = name.split(".", 1)
        ms.artifact_put_impl("K", phase, n, body, "complete", sid, scope="output", overwrite=True)
    _fake_registration(monkeypatch, outcomes)
    rows = asyncio.run(ms.pipeline_advance_impl(sid, "K", "task"))["rows"]
    (paths.ACTIVE / f"{sid}.json").unlink()
    return [r["next"]["sid"] for r in rows]


def test_every_row_holder_of_a_fork_gets_the_line(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _forked(monkeypatch, tmp_path, ["registered", "registered"])
    pane = _pane(monkeypatch)
    out = asyncio.run(topic_message.deliver("K", "both rows", via="cli"))
    assert out["delivered"] and len(pane["typed"]) == 2 and len(_events("K")) == 1


def test_a_partial_fork_delivery_never_types_twice(fresh_orchestrator_dir, monkeypatch, tmp_path, clock):
    a_sid, b_sid = _forked(monkeypatch, tmp_path, ["registered", "registered"])
    tokens = {t.holder: t.token_id for t in _fold().tokens.values() if t.row}
    a_token = next(tid for tid in tokens.values() if tid.endswith("/a"))
    b_token = next(tid for tid in tokens.values() if tid.endswith("/b"))
    (paths.ACTIVE / f"{b_sid}.json").unlink()
    asyncio.run(ms.pipeline_advance_impl("m1", "K", "?", reason="engineer away", token_id=b_token))
    pane = _pane(monkeypatch)
    first = clock["t"]
    out = asyncio.run(topic_message.deliver("K", "rows", via="cli"))
    assert not out["delivered"] and len(pane["typed"]) == 1
    assert _entry("K", out["record"])["delivered_tokens"] == [a_token]
    clock["t"] = first + 301
    pr.reconcile(MGR, now=first + 301)
    assert len(pane["typed"]) == 1 and _entry("K", out["record"]) is not None
    asyncio.run(ms.pipeline_advance_impl("m1", "K", "?", token_id=b_token))
    calls = _fake_registration(monkeypatch, ["registered"])
    clock["t"] = first + 700
    pr.reconcile(MGR, now=first + 700)
    assert len(pane["typed"]) == 1 and len(calls) == 1 and calls[0]["name"].startswith("K-task-2")
    assert _entry("K", out["record"]) is None
    assert [e["ok"] for e in _events("K")][-1] is True


@pytest.mark.parametrize("variant", ["busy", "unconfirmed"])
def test_a_resumed_busy_or_unconfirmed_pane_is_not_delivered(fresh_orchestrator_dir, monkeypatch, tmp_path, variant):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _close_session(sid, "K-design")
    tabs = []

    async def fake_tab(**kw):
        tabs.append(kw)
        ms.register_self_impl(kw["resume_sid"], "worker", kw["name"], kw["cwd"], "", pid=None)
        if variant == "busy":
            record = state.read_json(paths.ACTIVE / f"{kw['resume_sid']}.json")
            state.write_json_atomic(paths.ACTIVE / f"{kw['resume_sid']}.json",
                                    {**record, "human_attached_at": time.time()})
        return ("998", kw["name"])

    monkeypatch.setattr("dockwright.spawner.spawn_worker_tab", fake_tab)
    monkeypatch.setattr(ms, "_has_live_transcript", lambda sid, runtime="claude": True)
    monkeypatch.setattr(ms, "_capture_text", lambda wid: BUSY_PANE)
    _no_input_wait(monkeypatch)
    _pane(monkeypatch, ok=variant == "busy")
    out = asyncio.run(topic_message.deliver("K", "hi", via="cli"))
    reason = "human_attached" if variant == "busy" else "pane send failed"
    assert (out["route"], out["delivered"], out["reason"]) == ("resume", False, reason)
    assert _entry("K", out["record"])["reason"] == reason and len(tabs) == 1


def test_a_live_terminal_holder_gets_the_line(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _at_post_merge(monkeypatch, tmp_path)
    pane = _pane(monkeypatch)
    out = asyncio.run(topic_message.deliver("K", "hi", via="cli"))
    assert (out["route"], out["delivered"]) == ("pane", True) and len(pane["typed"]) == 1


def test_a_live_worker_of_a_tokenless_topic_gets_the_line(fresh_orchestrator_dir, monkeypatch):
    _manager()
    _register_worker("w1", "sid-w1", "a-w1", _note())
    pane = _pane(monkeypatch)
    calls = _agent(monkeypatch)
    out = asyncio.run(topic_message.deliver("N-1", "hi", via="cli"))
    assert (out["route"], out["delivered"]) == ("pane", True) and len(pane["typed"]) == 1 and calls == []


@pytest.mark.parametrize("field", [{"kind": "subworker"}, {"provider": "claude-exec"}])
def test_a_subworker_or_headless_child_is_never_the_pane(fresh_orchestrator_dir, monkeypatch, field):
    _manager()
    _register_worker("w1", "sid-w1", "a-w1", _note())
    record = state.read_json(paths.ACTIVE / "sid-w1.json")
    state.write_json_atomic(paths.ACTIVE / "sid-w1.json", {**record, **field})
    pane = _pane(monkeypatch)
    calls = _agent(monkeypatch)
    out = asyncio.run(topic_message.deliver("N-1", "hi", via="cli"))
    assert out["route"] == "agent" and pane["typed"] == [] and len(calls) == 1


def test_a_topic_with_no_session_goes_to_the_agent(fresh_orchestrator_dir, monkeypatch):
    calls = _agent(monkeypatch)
    out = asyncio.run(topic_message.deliver(_note(), "what is left?", decided_by="engineer", via="board"))
    assert (out["route"], out["delivered"]) == ("agent", True)
    assert calls == [("N-1", out["record"])]


def test_a_managers_message_to_an_attached_pane_is_queued(fresh_orchestrator_dir, monkeypatch, tmp_path):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    record = state.read_json(paths.ACTIVE / f"{sid}.json")
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {**record, "human_attached_at": time.time()})
    monkeypatch.setattr(ms, "_capture_text", lambda wid: IDLE_PANE)
    pane = _pane(monkeypatch)
    out = asyncio.run(topic_message.deliver("K", "hi", via="cli"))
    assert (out["delivered"], out["reason"]) == (False, "human_attached") and pane["typed"] == []
    assert _entry("K", out["record"])["reason"] == "human_attached"


@pytest.mark.parametrize("screen,delivered", [(IDLE_PANE, True), (BUSY_PANE, False)])
def test_his_own_message_to_an_attached_pane_keeps_todays_rule(fresh_orchestrator_dir, monkeypatch, tmp_path, screen,
                                                                delivered):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    record = state.read_json(paths.ACTIVE / f"{sid}.json")
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {**record, "human_attached_at": time.time()})
    monkeypatch.setattr(ms, "_capture_text", lambda wid: screen)
    pane = _pane(monkeypatch)
    out = asyncio.run(topic_message.deliver("K", "hi", decided_by="engineer", via="board"))
    assert out["delivered"] is delivered and (len(pane["typed"]) == 1) is delivered
    if not delivered:
        assert out["reason"] == "human_attached (line: draft)"


def test_an_attached_refusal_lands_on_the_first_pass_after_the_line_clears(fresh_orchestrator_dir, monkeypatch,
                                                                          tmp_path, clock):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _attach(sid)
    screen = _screen(monkeypatch, BUSY_PANE)
    pane = _pane(monkeypatch)
    first = clock["t"]
    out = asyncio.run(topic_message.deliver("K", VERDICT, kind="verdict", decided_by="engineer", via="cli",
                                            gate_round=1))
    assert out["reason"] == "human_attached (line: draft)" and pane["typed"] == []
    assert _entry("K", "decision.verdict-r1")["attached"] is True
    assert _events("K")[0]["attached"] is True
    clock["t"] = first + 60
    pr.reconcile(MGR, now=first + 60)
    assert pane["typed"] == [] and _entry("K", "decision.verdict-r1")["last_try_at"] == first + 60
    assert len(_events("K")) == 1
    screen["text"] = IDLE_PANE
    clock["t"] = first + 120
    pr.reconcile(MGR, now=first + 120)
    assert len(pane["typed"]) == 1 and pane["typed"][0].startswith("[TOPIC decision.verdict-r1 from engineer] ")
    assert _entry("K", "decision.verdict-r1") is None
    assert [e["ok"] for e in _events("K")] == [False, True]


def test_a_managers_queued_message_lands_once_the_flag_drops(fresh_orchestrator_dir, monkeypatch, tmp_path, clock):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _attach(sid)
    _screen(monkeypatch, IDLE_PANE)
    pane = _pane(monkeypatch)
    first = clock["t"]
    out = asyncio.run(topic_message.deliver("K", "hi", via="cli"))
    entry = _entry("K", out["record"])
    assert (entry["reason"], entry["attached"]) == ("human_attached", True)
    clock["t"] = first + 60
    pr.reconcile(MGR, now=first + 60)
    assert pane["typed"] == []
    _attach(sid, at=first - HUMAN_ATTACHED_TTL_SEC - 1)
    clock["t"] = first + 120
    pr.reconcile(MGR, now=first + 120)
    assert len(pane["typed"]) == 1 and _entry("K", out["record"]) is None


def test_a_fork_with_a_second_failure_reason_keeps_the_five_minute_retry(fresh_orchestrator_dir, monkeypatch,
                                                                         tmp_path, clock):
    a_sid, b_sid = _forked(monkeypatch, tmp_path, ["registered", "registered"])
    b_token = next(t.token_id for t in _fold().tokens.values() if t.row and t.token_id.endswith("/b"))
    _attach(a_sid)
    screen = _screen(monkeypatch, BUSY_PANE)
    (paths.ACTIVE / f"{b_sid}.json").unlink()
    asyncio.run(ms.pipeline_advance_impl("m1", "K", "?", reason="engineer away", token_id=b_token))
    pane = _pane(monkeypatch)
    first = clock["t"]
    out = asyncio.run(topic_message.deliver("K", "rows", decided_by="engineer", via="board"))
    assert _entry("K", out["record"])["attached"] is False
    assert "attached" not in _events("K")[0]
    screen["text"] = IDLE_PANE
    clock["t"] = first + 60
    pr.reconcile(MGR, now=first + 60)
    assert pane["typed"] == [] and _entry("K", out["record"])["last_try_at"] == first


@pytest.mark.parametrize("entry, age, due", [
    ({"attached": True}, 29, False),
    ({"attached": True}, 30, True),
    ({"attached": False}, 299, False),
    ({}, 299, False),
    ({}, 300, True),
])
def test_retry_due_waits_per_kind(entry, age, due):
    assert topic_outbox.retry_due({**entry, "last_try_at": 1000.0}, 1000.0 + age) is due


def test_no_live_manager_queues_a_restart(fresh_orchestrator_dir, monkeypatch, tmp_path):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    (paths.ACTIVE / f"{sid}.json").unlink()
    (paths.ACTIVE / "m1.json").unlink()
    calls = _fake_registration(monkeypatch, [])
    out = asyncio.run(topic_message.deliver("K", "hi", via="cli"))
    assert (out["route"], out["delivered"], out["reason"]) == ("restart", False, "no manager") and calls == []
    assert _entry("K", out["record"])["reason"] == "no manager"


def test_a_paused_agent_is_queued(fresh_orchestrator_dir, monkeypatch, tmp_path):
    cfg = tmp_path / "dw.toml"
    cfg.write_text('[subworkers.providers.claude-exec]\nmodel = "@opus"\neffort = "high"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    _manager()

    async def paused(*a, **k):
        return {"status": "paused"}

    monkeypatch.setattr(ms, "spawn_worker_impl", paused)
    out = asyncio.run(topic_message.deliver(_note(), "hi", via="cli"))
    assert (out["route"], out["delivered"], out["reason"]) == ("agent", False, "paused")
    assert _entry("N-1", out["record"])["reason"] == "paused"


def test_an_answer_records_then_unblocks(fresh_orchestrator_dir, monkeypatch, tmp_path):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    qid = ms._write_question(sid, "K-design", "which one?", "mgr", task_key="K")
    out = asyncio.run(topic_message.deliver("K", "the second", kind="answer", question_id=qid,
                                            decided_by="engineer", via="board"))
    assert out["record"] == f"decision.q-{qid[:8]}" and out["delivered"]
    assert ms.artifact_get_impl("K", "decision", f"q-{qid[:8]}")["decided_by"] == "engineer"
    assert state.read_json(paths.ANSWERS / f"{qid}.json")["answer"] == "the second"
    assert ms._topic_questions("K") == [] and list(paths.QUESTIONS.rglob(f"{qid}.json")) == []
    assert [(e["question_id"], e["kind"], e["ok"]) for e in _events("K")] == [(qid, "answer", True)]


def test_an_answer_to_another_topics_question_is_refused(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _started_at_design(monkeypatch, tmp_path)
    qid = ms._write_question("sid-x", "x", "which one?", "mgr", task_key="K-2")
    with pytest.raises(ValueError):
        asyncio.run(topic_message.deliver("K", "the second", kind="answer", question_id=qid, via="board"))
    assert not (paths.ANSWERS / f"{qid}.json").exists() and list(paths.QUESTIONS.rglob(f"{qid}.json"))
    assert not paths.artifact_path("K", "decision", f"q-{qid[:8]}").exists() and _events("K") == []


def test_a_priority_edits_the_card(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _started_at_design(monkeypatch, tmp_path)
    out = asyncio.run(topic_message.deliver("K", "", kind="priority", priority="P0", via="cli"))
    assert out["delivered"] and topic.load("K")["priority"] == "P0"
    sets = [e for e in ms._read_events(paths.artifact_events_path("K")) if e.get("type") == "topic_set"]
    assert [e["actor_sid"] for e in sets] == ["topic:cli"]


@pytest.mark.parametrize("key, text, kw", [
    ("K", "   ", {}),
    ("K", "x" * 16385, {}),
    ("NOPE", "hi", {}),
    ("K", "hi", {"kind": "shout"}),
    ("K", "hi", {"decided_by": "boss"}),
])
def test_bad_input_writes_nothing(fresh_orchestrator_dir, monkeypatch, tmp_path, key, text, kw):
    _started_at_design(monkeypatch, tmp_path)
    _pane(monkeypatch)
    before = _tree(fresh_orchestrator_dir)
    with pytest.raises(ValueError):
        asyncio.run(topic_message.deliver(key, text, via="cli", **kw))
    assert _tree(fresh_orchestrator_dir) == before


def test_the_queue_retries_after_five_minutes(fresh_orchestrator_dir, monkeypatch, tmp_path, clock):
    _started_at_design(monkeypatch, tmp_path)
    pane = _pane(monkeypatch, ok=False)
    first = clock["t"]
    out = asyncio.run(topic_message.deliver("K", "hi", via="cli"))
    clock["t"] = first + 100
    pr.reconcile(MGR, now=first + 100)
    assert len(pane["typed"]) == 1
    pane["ok"] = True
    clock["t"] = first + 301
    pr.reconcile(MGR, now=first + 301)
    assert len(pane["typed"]) == 2 and _entry("K", out["record"]) is None
    assert [e["ok"] for e in _events("K")] == [False, True]


def test_a_retry_that_fails_again_adds_no_journal_event(fresh_orchestrator_dir, monkeypatch, tmp_path, clock):
    _started_at_design(monkeypatch, tmp_path)
    pane = _pane(monkeypatch, ok=False)
    first = clock["t"]
    out = asyncio.run(topic_message.deliver("K", "hi", via="cli"))
    for later in (301, 602, 903):
        clock["t"] = first + later
        pr.reconcile(MGR, now=first + later)
    assert len(pane["typed"]) == 4 and _entry("K", out["record"])["last_try_at"] == first + 903
    assert [e["ok"] for e in _events("K")] == [False]


def test_an_hour_undelivered_pages_once(fresh_orchestrator_dir, monkeypatch, tmp_path, clock):
    _started_at_design(monkeypatch, tmp_path)
    _pane(monkeypatch, ok=False)
    first = clock["t"]
    out = asyncio.run(topic_message.deliver("K", "hi", via="cli"))
    lines = []
    for later in (3601, 7300):
        clock["t"] = first + later
        lines += pr.reconcile(MGR, now=first + later)
    pages = [line for line in lines if line.startswith("MESSAGE_UNDELIVERED")]
    assert [line.split(" → ", 1)[0] for line in pages] == [f"MESSAGE_UNDELIVERED K {out['record']}"]
    assert pages[0].endswith(" — pane send failed")


def _queued_then_owned_by(monkeypatch, tmp_path, clock, manager, domain):
    _started_at_design(monkeypatch, tmp_path)
    pane = _pane(monkeypatch, ok=False)
    out = asyncio.run(topic_message.deliver("K", "hi", via="cli"))
    topic_outbox.write({**_entry("K", out["record"]), "manager": manager, "domain": domain})
    pane["ok"] = True
    clock["t"] += 400
    pr.reconcile(MGR, now=clock["t"])
    return pane, out


def test_another_domains_entry_is_left_alone(fresh_orchestrator_dir, monkeypatch, tmp_path, clock):
    ms.register_self_impl("m2", "manager", "other", "/tmp", "", pid=None, domain="product")
    pane, out = _queued_then_owned_by(monkeypatch, tmp_path, clock, "other", "product")
    assert len(pane["typed"]) == 1 and _entry("K", out["record"]) is not None


def test_an_orphan_entry_goes_to_a_manager_of_its_domain(fresh_orchestrator_dir, monkeypatch, tmp_path, clock):
    pane, out = _queued_then_owned_by(monkeypatch, tmp_path, clock, "ghost", "general")
    assert len(pane["typed"]) == 2 and _entry("K", out["record"]) is None


def test_a_drain_another_manager_holds_retries_nothing(fresh_orchestrator_dir, monkeypatch, tmp_path, clock):
    _started_at_design(monkeypatch, tmp_path)
    pane = _pane(monkeypatch, ok=False)
    out = asyncio.run(topic_message.deliver("K", "hi", via="cli"))
    pane["ok"] = True
    topic_outbox.drain_lock_path().parent.mkdir(parents=True, exist_ok=True)
    with open(topic_outbox.drain_lock_path(), "a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        clock["t"] += 400
        pr.reconcile(MGR, now=clock["t"])
        assert len(pane["typed"]) == 1 and _entry("K", out["record"]) is not None
    clock["t"] += 400
    pr.reconcile(MGR, now=clock["t"])
    assert len(pane["typed"]) == 2 and _entry("K", out["record"]) is None


def test_a_cross_process_resume_is_refused(fresh_orchestrator_dir, monkeypatch, tmp_path):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _close_session(sid, "K-design")
    tabs = _fake_resume_tab(monkeypatch)
    lock = paths.ROOT / "resume-locks" / f"{paths._safe_segment(sid)}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        with pytest.raises(ValueError, match="already in progress"):
            asyncio.run(ms.resume_worker_impl("K-design"))
    assert tabs == []


def test_the_inbox_shows_an_undelivered_message(fresh_orchestrator_dir, monkeypatch):
    _probes(monkeypatch)
    topic_outbox.write({"key": _note(), "record": "decision.msg-20260930T101010", "kind": "message",
                        "decided_by": "engineer", "via": "cli", "domain": "general", "manager": "mgr",
                        "first_failed_at": 100.0, "last_try_at": 100.0, "reason": "no manager", "paged": False,
                        "delivered_tokens": []})
    items = [i for i in board.inbox()["stopped"] if i["kind"] == "undelivered"]
    assert [(i["key"], i["text"], i["note"]) for i in items] == [("N-1", "decision.msg-20260930T101010",
                                                                   "no manager")]
    assert board.inbox(domain="product")["stopped"] == []


def test_the_footer_ranks_decisions_in_three_tiers():
    line = pipeline._decision_line()
    assert "\n" not in line
    for phrase in ("written_at never ranks across tiers", "decided_by: manager", "ask_manager",
                   "[TOPIC … from engineer]", "decision.verdict-r<n>", "publish no decision.pane-*",
                   "read <record>", "naming both records",
                   "settles the point over the records its question names",
                   "author-wait:, lease:, trigger: or event: is an automated wake, never a decision",
                   "starting with author-wait:, lease:, trigger: or event: get no reply",
                   "reply.", "[TOPIC … from engineer] message gets a reply."):
        assert phrase in line, phrase
    assert "tier 2 of the decision tiers" in pipeline._rulings_line("fleet-meta", "meta")


def test_a_message_to_a_node_waiting_on_a_trigger_queues_with_a_readable_reason(fresh_orchestrator_dir, monkeypatch,
                                                                                tmp_path):
    _at_pr_with_a_pr_url(monkeypatch, tmp_path)
    (paths.ACTIVE / "sid-K-pr.json").unlink()
    state.write_json_atomic(paths.CLOSED / "sid-K-pr.json", {"claude_sid": "sid-K-pr", "name": "K-pr",
                                                             "closed_reason": "idle>7200s",
                                                             "closed_at": time.time() - 8000})
    monkeypatch.setattr(ms, "_has_live_transcript", lambda sid, runtime="claude": False)
    pane = _pane(monkeypatch)
    out = asyncio.run(topic_message.deliver("K", "after the merge, bump the changelog", decided_by="engineer",
                                            via="cli"))
    reason = "node pr finished; waits on the merge trigger; the next node reads the record as an input"
    assert (out["route"], out["delivered"], out["reason"]) == ("done", False, reason)
    assert [(e["record"], e["reason"]) for e in topic_outbox.entries()] == [(out["record"], reason)]
    assert pane["typed"] == []
    calls = _fake_registration(monkeypatch, ["registered"])
    asyncio.run(ms.pipeline_advance_impl("m1", "K", "post-merge", reason="2026-09-30T00:00:00Z"))
    assert out["record"] in calls[-1]["initial_prompt"]


def _finished_trigger_source(monkeypatch, tmp_path):
    _at_pr_with_a_pr_url(monkeypatch, tmp_path)
    (paths.ACTIVE / "sid-K-pr.json").unlink()
    state.write_json_atomic(paths.CLOSED / "sid-K-pr.json", {"claude_sid": "sid-K-pr", "name": "K-pr",
                                                             "closed_reason": "idle>7200s",
                                                             "closed_at": time.time() - 8000})
    monkeypatch.setattr(ms, "_has_live_transcript", lambda sid, runtime="claude": False)
    return _fake_registration(monkeypatch, ["registered"])


@pytest.mark.parametrize("text", ["event: github.review — o/r#7 open abc1234 REVIEW_REQUIRED CLEAN none -",
                                  "event: github.conflict — o/r#7 conflict abc1234 REVIEW_REQUIRED DIRTY none -"])
def test_a_feedback_wake_restarts_a_finished_trigger_source(fresh_orchestrator_dir, monkeypatch, tmp_path, text):
    calls = _finished_trigger_source(monkeypatch, tmp_path)
    out = asyncio.run(topic_message.deliver("K", text, decided_by="manager", via="reconcile"))
    assert (out["route"], out["delivered"]) == ("restart", True)
    assert topic_outbox.entries() == [] and [c["name"] for c in calls] == ["K-pr-r2"]
    assert out["record"] in calls[-1]["initial_prompt"]


def test_an_old_feedback_text_still_restarts_the_finished_source(fresh_orchestrator_dir, monkeypatch, tmp_path):
    calls = _finished_trigger_source(monkeypatch, tmp_path)
    out = asyncio.run(topic_message.deliver("K", "trigger: feedback — o/r#7 open abc1234 REVIEW_REQUIRED CLEAN none -",
                                            decided_by="manager", via="reconcile"))
    assert (out["route"], out["delivered"]) == ("restart", True) and [c["name"] for c in calls] == ["K-pr-r2"]


def test_a_tracker_event_does_not_restart_a_finished_node(fresh_orchestrator_dir, monkeypatch, tmp_path):
    calls = _finished_trigger_source(monkeypatch, tmp_path)
    out = asyncio.run(topic_message.deliver("K", "event: tracker.comment — ABC-1 #12 by Name: hi (sub a1b2c3d4)",
                                            decided_by="manager", via="reconcile"))
    assert (out["route"], out["delivered"]) == ("done", False) and calls == []


def test_a_feedback_text_from_another_via_keeps_the_done_route(fresh_orchestrator_dir, monkeypatch, tmp_path):
    calls = _finished_trigger_source(monkeypatch, tmp_path)
    out = asyncio.run(topic_message.deliver("K", "trigger: feedback — o/r#7 open abc1234 REVIEW_REQUIRED CLEAN none -",
                                            decided_by="manager", via="mcp"))
    assert (out["route"], out["delivered"]) == ("done", False) and calls == []


def test_reconcile_is_an_accepted_via(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    assert topic_message._check("K", "trigger: x", "message", "manager", "reconcile") is None
    with pytest.raises(ValueError, match="via must be one of"):
        topic_message._check("K", "trigger: x", "message", "manager", "nope")


ACCEPTANCE = SCHEMA.replace('outputs = ["summary.post-merge"]',
                            'outputs = ["summary.post-merge", "summary.acceptance"]\ncheckpoints = ["wait.acceptance"]')
WATCH = '{"pid": 7, "handled": []}'
CLOSED_WATCH = '{"pid": 7, "handled": [], "closed": "done"}'
CLOSE_TEXT = "watcher: T-1 reached Done; closed with no wake"
CLOSE_SUMMARY = "closed: done - T-1 Done\n"


def _watching(monkeypatch, tmp_path, clock, post_merge=True):
    _started_at_design(monkeypatch, tmp_path, schema_text=ACCEPTANCE)
    _fake_registration(monkeypatch, ["registered"])
    sid = asyncio.run(ms.pipeline_advance_impl("m1", "K", "post-merge", override=True, reason="jump"))["next"]["sid"]
    holder = _fold().tokens["t1"].holder
    if post_merge:
        ms.artifact_put_impl("K", "summary", "post-merge", "merged", "complete", sid, scope="output")
    wa = ms.artifact_put_impl("K", "wait", "acceptance", WATCH, "complete", sid, scope="checkpoint")["written_at"]
    clock["t"] += 5
    _done(sid, "PARKED watching T-1")
    return sid, holder, wa


def _close(wa, **over):
    args = {"task_key": "K", "node": "post-merge", "text": CLOSE_TEXT,
            "checkpoints": {"wait.acceptance": CLOSED_WATCH}, "outputs": {"summary.acceptance": CLOSE_SUMMARY},
            "expect": {"wait.acceptance": wa}}
    return topic_message.close_node(**{**args, **over})


def test_close_finishes_a_parked_terminal_node_without_a_line(fresh_orchestrator_dir, monkeypatch, tmp_path, clock,
                                                              killed):
    sid, holder, wa = _watching(monkeypatch, tmp_path, clock)
    pane = _pane(monkeypatch)
    out = _close(wa)
    assert out["closed"] and out["session"] == "closed"
    assert pane["typed"] == []
    watch = ms.artifact_get_impl("K", "wait", "acceptance")
    assert watch["content"].endswith('"closed": "done"}')
    assert (watch["status"], watch["scope"]) == ("complete", "checkpoint")
    summary = ms.artifact_get_impl("K", "summary", "acceptance")
    assert (summary["status"], summary["scope"]) == ("complete", "output")
    assert ms.artifact_get_impl("K", "decision", out["record"].split(".", 1)[1])["content"].startswith(CLOSE_TEXT)
    assert killed == ["K-post-merge"]
    assert _closes()[-1] == _closed("K-post-merge", reason="finished")
    assert _of("handed_off")[-1]["assignment_id"] == holder
    last = _events("K")[-1]
    assert (last["route"], last["ok"], last["kind"]) == ("closed", True, "close")
    assert _fold().closed is None and _of("work_closed") == []
    clock["t"] += 400
    assert pr.reconcile(MGR) == [] and killed.count("K-post-merge") == 1


def _busy(monkeypatch, sid, wa, clock):
    _pane(monkeypatch)
    _no_input_wait(monkeypatch)
    clock["t"] += 1
    asyncio.run(topic_message.deliver("K", "stop waiting", via="cli"))
    return {}


def _crashed_busy(monkeypatch, sid, wa, clock):
    _busy(monkeypatch, sid, wa, clock)
    monkeypatch.setattr(ms, "_session_alive", lambda s: False)
    return {}


def _complete_summary(monkeypatch, sid, wa, clock):
    ms.artifact_put_impl("K", "summary", "acceptance", "closed by hand", "complete", sid, scope="output")
    return {}


def _attached(monkeypatch, sid, wa, clock):
    path = paths.ACTIVE / f"{sid}.json"
    state.write_json_atomic(path, {**state.read_json(path), "human_attached_at": time.time()})
    return {}


def _held(monkeypatch, sid, wa, clock):
    asyncio.run(ms.pipeline_advance_impl("m1", "K", "?", reason="hold"))
    return {}


def _reentered(monkeypatch, sid, wa, clock):
    _fake_registration(monkeypatch, ["registered"])
    second = asyncio.run(ms.pipeline_advance_impl("m1", "K", "post-merge", override=True,
                                                  reason="again"))["next"]["sid"]
    again = ms.artifact_put_impl("K", "wait", "acceptance", WATCH, "complete", second, scope="checkpoint",
                                 overwrite=True)["written_at"]
    clock["t"] += 5
    _done(second, "PARKED watching T-1")
    return {"expect": {"wait.acceptance": again}}


def _restart_requested(monkeypatch, sid, wa, clock):
    (paths.ACTIVE / f"{sid}.json").unlink()
    _fake_registration(monkeypatch, ["unregistered"])
    pr.reconcile(MGR)
    assert _fold().open_requests != []
    return {}


@pytest.mark.parametrize("setup,match,post_merge", [
    (_busy, "not parked", True),
    (_crashed_busy, "not parked", True),
    (lambda m, s, w, c: {"node": "pr"}, "not pr", True),
    (lambda m, s, w, c: {"checkpoints": {"wait.other": "x"}}, "not a declared", True),
    (_complete_summary, "already complete", True),
    (lambda m, s, w, c: {}, "summary.post-merge is not complete", False),
    (lambda m, s, w, c: {"expect": {"wait.acceptance": w + 1}}, "changed", True),
    (_attached, "held open", True),
    (_held, "is held: hold", True),
    (_reentered, "no closable terminal holder", True),
    (_restart_requested, "no closable terminal holder", True),
], ids=["busy", "crashed-busy", "node", "undeclared", "complete", "missing", "changed", "attached", "held", "reentered", "request"])
def test_close_refuses_and_writes_nothing(fresh_orchestrator_dir, monkeypatch, tmp_path, clock, killed, setup, match,
                                          post_merge):
    sid, holder, wa = _watching(monkeypatch, tmp_path, clock, post_merge=post_merge)
    over = setup(monkeypatch, sid, wa, clock)
    named = [*over.get("checkpoints", {"wait.acceptance": None}), *over.get("outputs", {"summary.acceptance": None})]

    def contents():
        found = {}
        for full in named:
            path = paths.artifact_path("K", *full.split(".", 1))
            found[full] = path.read_text() if path.exists() else None
        return found

    def messages():
        return sorted(p.name for p in paths.artifact_ticket_dir("K").glob("decision.msg-*"))

    before, sent, handed = contents(), messages(), len(_of("handed_off"))
    with pytest.raises(pr.CloseRefused, match=match):
        _close(wa, **over)
    assert contents() == before and messages() == sent
    assert killed == [] and len(_of("handed_off")) == handed


def test_close_of_a_dead_holder_stamps_without_a_kill(fresh_orchestrator_dir, monkeypatch, tmp_path, clock, killed):
    sid, holder, wa = _watching(monkeypatch, tmp_path, clock)
    (paths.ACTIVE / f"{sid}.json").unlink()
    out = _close(wa)
    assert out["session"] == "dead" and killed == []
    assert [e for e in _of("handed_off") if e["assignment_id"] == holder]
    calls = _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR) == [] and calls == []


def test_close_leaves_a_session_tasked_after_the_check(fresh_orchestrator_dir, monkeypatch, tmp_path, clock, killed):
    sid, holder, wa = _watching(monkeypatch, tmp_path, clock)
    real = ms._reported_since_input
    answers = iter([True])
    monkeypatch.setattr(ms, "_reported_since_input", lambda *a, **k: next(answers, False))
    out = _close(wa)
    assert out["session"] == "left" and killed == []
    clock["t"] += 5
    _done(sid, "closed")
    monkeypatch.setattr(ms, "_reported_since_input", real)
    assert pr.reconcile(MGR) == [] and killed == ["K-post-merge"]


def test_close_after_the_stamp_reports_a_kill_error_and_still_closes(fresh_orchestrator_dir, monkeypatch, tmp_path,
                                                                    clock, killed):
    sid, holder, wa = _watching(monkeypatch, tmp_path, clock)

    def gone(*args, **kwargs):
        raise OSError("tmux gone")

    monkeypatch.setattr(pr, "_close_session", gone)
    out = _close(wa)
    assert out["closed"] is True and "tmux gone" in out["error"]
    assert [e for e in _of("handed_off") if e["assignment_id"] == holder]
    assert ms.artifact_get_impl("K", "summary", "acceptance")["status"] == "complete"


def test_topic_record_prints_the_stamp_and_the_body(fresh_orchestrator_dir, monkeypatch, tmp_path, clock, capsys):
    _, _, wa = _watching(monkeypatch, tmp_path, clock)
    capsys.readouterr()
    assert topic_message.main(["record", "K", "wait.acceptance"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert (printed["status"], printed["written_at"], printed["content"]) == ("complete", wa, WATCH)
    assert topic_message.main(["record", "K", "wait.none"]) == 1


def test_topic_close_cli_exit_codes(fresh_orchestrator_dir, monkeypatch, tmp_path, clock, capsys):
    _, _, wa = _watching(monkeypatch, tmp_path, clock)
    files = tmp_path / "close-files"
    files.mkdir()
    cp, sm = files / "wait.acceptance.json", files / "summary.acceptance.md"
    cp.write_text(CLOSED_WATCH)
    sm.write_text(CLOSE_SUMMARY)
    argv = ["close", "K", "--node", "post-merge", "--text", "t", "--checkpoint", f"wait.acceptance={cp}",
            "--output", f"summary.acceptance={sm}", "--expect", f"wait.acceptance={wa!r}"]
    capsys.readouterr()
    assert topic_message.main(argv) == 0
    assert '"closed": true' in capsys.readouterr().out
    assert topic_message.main(argv) == 3
    again = json.loads(capsys.readouterr().out)
    assert again["closed"] is False and again["reason"]
    with pytest.raises(SystemExit) as exc:
        topic_message.main(["close", "K", "--node", "post-merge", "--text", "t", "--checkpoint", "wait.acceptance"])
    assert exc.value.code == 2


def test_a_pane_delivery_names_the_pane_in_the_result_and_the_event(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _started_at_design(monkeypatch, tmp_path)
    _pane(monkeypatch)
    out = asyncio.run(topic_message.deliver("K", "hello", decided_by="engineer", via="board"))
    assert out["delivered"] and out["pane"] == "K-design"
    assert _events("K")[-1]["pane"] == "K-design"


def test_close_parked_drops_the_nodes_subscriptions(fresh_orchestrator_dir, monkeypatch, tmp_path, clock, killed):
    from dockwright import config, events
    sid, holder, wa = _watching(monkeypatch, tmp_path, clock)
    cfg = tmp_path / "events.toml"
    env_file = tmp_path / "events.env"
    env_file.write_text("TRACKER_URL=https://j\nTRACKER_EMAIL=me@x\nTRACKER_API_TOKEN=tok\n")
    env_file.chmod(0o600)
    cfg.write_text(f'[events]\nenabled = true\nenv_file = "{env_file}"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    sub = events.subscribe("K", "post-merge", "tracker", {"issue": "T-1"}, after="1")
    (paths.ACTIVE / f"{sid}.json").unlink()
    out = _close(wa)
    assert out["session"] == "dead" and events.subs_for("K") == []
    assert [(e["sub_id"], e["reason"]) for e in _of("unsubscribed")] == [(sub.sub_id, "closed")]
