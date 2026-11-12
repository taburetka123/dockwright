import asyncio
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest

from dockwright import config, mcp_server, paths, spawner, state
from dockwright.mcp_server import (
    _backfill_legacy_workers, _matches_manager, _write_question, answer_child,
    answer_question, ask_manager, ask_manager_impl, ask_parent, become_manager_impl,
    child_done, get_child_tail, get_worker_tail, kill_child, kill_worker, list_children,
    list_closed_workers_impl, list_workers, list_workers_impl, register_self_impl,
    send_manager_to_worker, send_to_child, spawn_child, spawn_worker, spawn_worker_impl,
    wait_for_child, wait_for_child_impl, wait_for_worker, wait_for_worker_impl,
    worker_done, worker_done_impl, resume_worker_impl,
)


@pytest.fixture
def fresh_orchestrator_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(paths, "QUESTIONS", tmp_path / "questions")
    monkeypatch.setattr(paths, "ANSWERS", tmp_path / "answers")
    monkeypatch.setattr(paths, "DONE", tmp_path / "done")
    monkeypatch.setattr(paths, "CLOSED", tmp_path / "closed")
    monkeypatch.setattr(paths, "HANDOFFS", tmp_path / "handoffs")
    monkeypatch.setattr(paths, "PRESETS", tmp_path / "presets")
    monkeypatch.setattr(paths, "TURN_ENDS", tmp_path / "turn-ends")
    monkeypatch.setattr(paths, "MANAGER_TRIGGERS_LOG", tmp_path / "manager-triggers.jsonl")
    monkeypatch.setattr(paths, "MANAGER_MEMORY", tmp_path / "manager-memory")
    monkeypatch.setattr(paths, "SLOTS", tmp_path / "slots")
    monkeypatch.setattr(paths, "ARTIFACTS", tmp_path / "artifacts")
    monkeypatch.setattr(paths, "ASSIGNMENTS", tmp_path / "assignments")
    monkeypatch.setattr(paths, "ASSIGNMENTS_PENDING", tmp_path / "assignments" / ".pending")
    monkeypatch.setattr(paths, "PANE_SENDS", tmp_path / "pane-sends")
    monkeypatch.setattr(paths, "SPEND_LEDGER", tmp_path / "spend-ledger.jsonl")
    monkeypatch.delenv("CLAUDE_PARENT_SID", raising=False)
    monkeypatch.delenv("CLAUDE_PARENT_MANAGER", raising=False)
    paths.ensure_dirs()
    yield tmp_path


def _patch_spawn_worker_tab(monkeypatch):
    captured: dict = {}

    async def fake_spawn(**kwargs):
        captured.update(kwargs)
        return ("999", kwargs.get("name", ""))

    monkeypatch.setattr(spawner, "spawn_worker_tab", fake_spawn)
    return captured


def _worker(sid, name, **fields):
    record = {"claude_sid": sid, "agent": "worker", "name": name, "cwd": "/x",
              "window_id": "", "pid": os.getpid(), "started_at": 0, "state": "idle"}
    record.update(fields)
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", record)
    return record


def test_parent_name_of_prefers_parent_name_then_falls_back():
    assert state.parent_name_of({"parent_name": "new", "parent_manager_name": "old"}) == "new"
    assert state.parent_name_of({"parent_manager_name": "old"}) == "old"
    assert state.parent_name_of({"parent_name": None, "parent_manager_name": "old"}) == "old"
    assert state.parent_name_of({}) is None
    assert state.parent_name_of(None) is None


def test_child_depth_is_parent_plus_one_or_role_default():
    assert state.child_depth({"depth": 0}, "worker") == 1
    assert state.child_depth({"depth": 1}, "worker") == 2
    assert state.child_depth({}, "worker") == 1
    assert state.child_depth(None, "worker") == 1
    assert state.child_depth(None, "manager") == 0


def test_tree_fields_shapes():
    assert state.tree_fields("manager") == {"kind": "manager", "parent_sid": None, "parent_name": None, "depth": 0}
    assert state.tree_fields("worker", parent_name="boss", parent_sid="m1", parent_record={"depth": 0}) == \
        {"kind": "worker", "parent_sid": "m1", "parent_name": "boss", "depth": 1}
    assert state.tree_fields("worker") == {"kind": "worker", "parent_sid": None, "parent_name": None, "depth": 1}
    assert state.tree_fields("worker", parent_name="boss", nested=True) == \
        {"kind": "worker", "parent_sid": None, "parent_name": "boss", "depth": None}


def test_parent_sid_env_key_in_strip_list():
    assert "CLAUDE_PARENT_SID" in paths.ORCHESTRATOR_ENV_KEYS


def test_register_self_stamps_tree_fields(fresh_orchestrator_dir):
    register_self_impl(claude_sid="m1", agent="manager", name="boss", cwd="/x", iterm_sid="i", pid=os.getpid())
    mgr = state.read_json(paths.ACTIVE / "m1.json")
    assert (mgr["kind"], mgr["parent_sid"], mgr["parent_name"], mgr["depth"]) == ("manager", None, None, 0)
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i",
                       pid=os.getpid(), parent_manager_name="boss", parent_sid="m1")
    w = state.read_json(paths.ACTIVE / "w1.json")
    assert (w["kind"], w["parent_sid"], w["parent_name"], w["parent_manager_name"], w["depth"]) == \
        ("worker", "m1", "boss", "boss", 1)


def test_backfill_stamps_parent_name_and_sid(fresh_orchestrator_dir):
    register_self_impl(claude_sid="m1", agent="manager", name="boss", cwd="/x", iterm_sid="i", pid=os.getpid())
    _worker("w1", "legacy", parent_manager_name=None)
    _worker("w2", "new-style", parent_name=None)
    assert _backfill_legacy_workers() == 2
    for sid in ("w1", "w2"):
        rec = state.read_json(paths.ACTIVE / f"{sid}.json")
        assert rec["parent_manager_name"] == "boss"
        assert rec["parent_name"] == "boss"
        assert rec["parent_sid"] == "m1"


def test_backfill_skips_worker_scoped_by_parent_name_only(fresh_orchestrator_dir):
    register_self_impl(claude_sid="m1", agent="manager", name="boss", cwd="/x", iterm_sid="i", pid=os.getpid())
    _worker("w1", "scoped", parent_name="other")
    assert _backfill_legacy_workers() == 0


def test_become_manager_restamps_parent_sid_for_own_workers_only(fresh_orchestrator_dir):
    _worker("w1", "mine", parent_name="boss", parent_manager_name="boss", parent_sid="old-boss-sid")
    _worker("w2", "theirs", parent_name="other", parent_manager_name="other", parent_sid="other-sid")
    _worker("w3", "old-style", parent_manager_name="boss")
    become_manager_impl(claude_sid="boss-new", iterm_sid="i", domain="general", name="boss")
    assert state.read_json(paths.ACTIVE / "w1.json")["parent_sid"] == "boss-new"
    assert state.read_json(paths.ACTIVE / "w3.json")["parent_sid"] == "boss-new"
    assert state.read_json(paths.ACTIVE / "w2.json")["parent_sid"] == "other-sid"


def test_matches_manager_reads_parent_name_only():
    assert _matches_manager({"parent_name": "boss"}, "boss") is True
    assert _matches_manager({"parent_name": "other"}, "boss") is False
    assert _matches_manager({"parent_manager_name": "boss"}, "boss") is True
    assert _matches_manager({"parent_name": None, "parent_manager_name": "boss"}, "boss") is True


def test_list_workers_and_closed_see_parent_name_only_records(fresh_orchestrator_dir):
    _worker("w1", "alpha", parent_name="boss")
    assert [w["name"] for w in list_workers_impl(manager_name="boss")] == ["alpha"]
    state.write_json_atomic(paths.CLOSED / "c1.json", {
        "claude_sid": "c1", "name": "beta", "cwd": "/x", "closed_at": 1.0,
        "parent_name": "boss", "parent_sid": "m1", "kind": "worker", "depth": 1,
    })
    closed = list_closed_workers_impl(manager_name="boss")
    assert [c["name"] for c in closed] == ["beta"]
    assert (closed[0]["parent_name"], closed[0]["parent_sid"], closed[0]["kind"], closed[0]["depth"]) == \
        ("boss", "m1", "worker", 1)


def test_question_and_done_event_carry_parent_name(fresh_orchestrator_dir):
    _worker("w1", "alpha", parent_name="boss")
    qid = _write_question("w1", "alpha", "q?", parent_manager_name="boss")
    question = state.read_json(paths.QUESTIONS / "boss" / f"{qid}.json")
    assert question["parent_name"] == "boss"
    assert question["parent_manager_name"] == "boss"
    result = worker_done_impl("w1", "finished")
    events = list((paths.DONE / "boss").glob("w1-*.json"))
    assert len(events) == 1
    event = state.read_json(events[0])
    assert event["parent_name"] == "boss"
    assert event["parent_manager_name"] == "boss"
    assert result["ok"] is True


def test_ask_manager_routes_question_by_parent_name_only(fresh_orchestrator_dir):
    _worker("w1", "alpha", parent_name="boss")
    result = asyncio.run(ask_manager_impl("w1", "q?", poll_interval=0.01, timeout_sec=0.05))
    assert result.startswith("NO_ANSWER_YET:")
    assert list((paths.QUESTIONS / "boss").glob("*.json"))


def test_wait_for_worker_sees_done_event_with_parent_name_only(fresh_orchestrator_dir):
    _worker("w1", "alpha", parent_name="boss")
    worker_done_impl("w1", "finished")
    result = asyncio.run(wait_for_worker_impl("alpha", timeout_sec=1, _poll_interval=0.01, manager_name="boss"))
    assert result["found"] == "done"


def test_worker_done_self_heal_reads_parent_name_from_assignment(fresh_orchestrator_dir):
    paths.ASSIGNMENTS.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.assignment_path("gone"), {
        "claude_sid": "gone", "name": "ghost", "parent_name": "boss",
    })
    result = worker_done_impl("gone", "late")
    assert result["self_healed"] is True
    assert list((paths.DONE / "boss").glob("gone-*.json"))


def test_spawn_worker_env_carries_parent_sid_and_strips_caller_key(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="mgr-a", iterm_sid="i0", domain="general")
    mgr_name = state.read_json(paths.ACTIVE / "mgr-a.json")["name"]
    captured = _patch_spawn_worker_tab(monkeypatch)
    asyncio.run(spawn_worker_impl(initial_prompt="do X", name="wx", cwd="/tmp/x", manager_sid="mgr-a",
                                  env={"CLAUDE_PARENT_SID": "forged", "MY": "1"},
                                  _registration_timeout_sec=0.01, _poll_interval=0.01))
    env = captured["env"]
    assert env["CLAUDE_PARENT_SID"] == "mgr-a"
    assert env["CLAUDE_PARENT_MANAGER"] == mgr_name
    assert env["MY"] == "1"
    result_pending = list(paths.ASSIGNMENTS_PENDING.glob("*.json"))
    assert len(result_pending) == 1
    pending = state.read_json(result_pending[0])
    assert pending["parent_name"] == mgr_name
    assert pending["parent_sid"] == "mgr-a"


def test_spawn_worker_unscoped_strips_caller_parent_sid_and_env_differs_by_two_keys(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="mgr-a", iterm_sid="i0", domain="general")
    mgr_name = state.read_json(paths.ACTIVE / "mgr-a.json")["name"]
    captured = _patch_spawn_worker_tab(monkeypatch)
    asyncio.run(spawn_worker_impl(initial_prompt="do X", name="w-unscoped", cwd="/tmp/x",
                                  env={"CLAUDE_PARENT_SID": "forged", "MY": "1"},
                                  _registration_timeout_sec=0.01, _poll_interval=0.01))
    unscoped = dict(captured["env"])
    assert "CLAUDE_PARENT_SID" not in unscoped
    captured.clear()
    asyncio.run(spawn_worker_impl(initial_prompt="do X", name="w-scoped", cwd="/tmp/x", manager_sid="mgr-a",
                                  env={"MY": "1"}, _registration_timeout_sec=0.01, _poll_interval=0.01))
    scoped = dict(captured["env"])
    unscoped.pop("CLAUDE_ASSIGNMENT_ID"); scoped.pop("CLAUDE_ASSIGNMENT_ID")
    assert scoped == {**unscoped, "CLAUDE_PARENT_MANAGER": mgr_name, "CLAUDE_PARENT_SID": "mgr-a"}


def test_spawn_worker_result_carries_parent_name_and_sid(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="mgr-a", iterm_sid="i0", domain="general")
    mgr_name = state.read_json(paths.ACTIVE / "mgr-a.json")["name"]
    _patch_spawn_worker_tab(monkeypatch)
    result = asyncio.run(spawn_worker_impl(initial_prompt="do X", name="wx", cwd="/tmp/x", manager_sid="mgr-a",
                                           _registration_timeout_sec=0.01, _poll_interval=0.01))
    assert result["parent_name"] == mgr_name
    assert result["parent_sid"] == "mgr-a"
    assert result["parent_manager_name"] == mgr_name


def test_spawn_worker_tab_command_gains_exactly_one_assignment(monkeypatch):
    captured: dict = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return (b"42\n", b"")

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _Proc()

    monkeypatch.setattr(spawner.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(paths, "ACCOUNT_ACTIVE", Path("/nonexistent/__no_account_active__"))
    monkeypatch.setattr(spawner.trust, "pretrust_dir", lambda *a, **k: None)
    flags = ["--model", "@opus", "--effort", "xhigh"]
    asyncio.run(spawner.spawn_worker_tab(cwd="/tmp/x", initial_prompt="hi", name="alpha", extra_args=flags,
                                         env={"CLAUDE_PARENT_MANAGER": "boss"}))
    without = captured["args"][-1]
    asyncio.run(spawner.spawn_worker_tab(cwd="/tmp/x", initial_prompt="hi", name="alpha", extra_args=flags,
                                         env={"CLAUDE_PARENT_MANAGER": "boss", "CLAUDE_PARENT_SID": "m1"}))
    with_sid = captured["args"][-1]
    assert with_sid.replace(" CLAUDE_PARENT_SID=m1", "", 1) == without
    assert with_sid.count("CLAUDE_PARENT_SID=") == 1


def _patch_spawn_that_registers(monkeypatch, sid):
    captured: dict = {}

    async def fake_spawn(**kwargs):
        captured.update(kwargs)
        state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {
            "claude_sid": sid, "agent": "worker", "name": kwargs["name"], "cwd": kwargs["cwd"],
            "window_id": "999", "pid": os.getpid(), "started_at": 0,
        })
        return ("999", kwargs["name"])

    monkeypatch.setattr(spawner, "spawn_worker_tab", fake_spawn)
    return captured


def test_resume_passes_parent_sid_of_live_parent_by_name(fresh_orchestrator_dir, monkeypatch):
    from dockwright import mcp_server
    become_manager_impl(claude_sid="boss-new", iterm_sid="i0", domain="general", name="boss")
    state.write_json_atomic(paths.CLOSED / "w1.json", {
        "claude_sid": "w1", "name": "alpha", "cwd": "/tmp/x", "closed_at": 1.0, "runtime": "claude",
        "parent_manager_name": "boss", "parent_name": "boss", "parent_sid": "boss-old",
    })
    monkeypatch.setattr(mcp_server, "_has_live_transcript", lambda sid, runtime=None: True)
    captured = _patch_spawn_that_registers(monkeypatch, "w1")
    result = asyncio.run(resume_worker_impl("alpha", _registration_timeout_sec=1.0, _poll_interval=0.01, model="@opus", effort="xhigh"))
    assert result["ok"] is True
    assert captured["env"]["CLAUDE_PARENT_MANAGER"] == "boss"
    assert captured["env"]["CLAUDE_PARENT_SID"] == "boss-new"


def test_resume_omits_parent_sid_when_parent_is_not_live(fresh_orchestrator_dir, monkeypatch):
    from dockwright import mcp_server
    state.write_json_atomic(paths.CLOSED / "w1.json", {
        "claude_sid": "w1", "name": "alpha", "cwd": "/tmp/x", "closed_at": 1.0, "runtime": "claude",
        "parent_manager_name": "boss", "parent_sid": "boss-old",
    })
    monkeypatch.setattr(mcp_server, "_has_live_transcript", lambda sid, runtime=None: True)
    captured = _patch_spawn_that_registers(monkeypatch, "w1")
    asyncio.run(resume_worker_impl("alpha", _registration_timeout_sec=1.0, _poll_interval=0.01, model="@opus", effort="xhigh"))
    assert captured["env"]["CLAUDE_PARENT_MANAGER"] == "boss"
    assert "CLAUDE_PARENT_SID" not in captured["env"]


def _manager(sid="mgr-a"):
    become_manager_impl(claude_sid=sid, iterm_sid="i0", domain="general")
    return state.read_json(paths.ACTIVE / f"{sid}.json")["name"]


def test_spawn_child_requires_parent_sid(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="spawn_child requires parent_sid"):
        asyncio.run(spawn_child(parent_sid="", brief="do X"))


def test_spawn_child_and_spawn_worker_build_the_same_spawn(fresh_orchestrator_dir, monkeypatch):
    mgr_name = _manager()
    captured = _patch_spawn_worker_tab(monkeypatch)
    monkeypatch.setattr(mcp_server, "_DEFAULT_REGISTRATION_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(mcp_server, "_DEFAULT_REGISTRATION_POLL_SEC", 0.01)
    child = asyncio.run(spawn_child(parent_sid="mgr-a", brief="do X", name="w-child", cwd="/tmp/x", task_key="t", provider="claude-tui", mode="interactive", model="@opus", effort="xhigh"))
    child_kwargs = dict(captured)
    captured.clear()
    worker = asyncio.run(spawn_worker(initial_prompt="do X", name="w-worker", cwd="/tmp/x", manager_sid="mgr-a", task_key="t",
                                      extra_args=["--model", "@opus", "--effort", "xhigh"]))
    worker_kwargs = dict(captured)
    for kwargs in (child_kwargs, worker_kwargs):
        kwargs["env"].pop("CLAUDE_ASSIGNMENT_ID")
    child_kwargs["name"] = worker_kwargs["name"] = "same"
    assert child_kwargs == worker_kwargs
    assert child["parent_name"] == worker["parent_name"] == mgr_name
    assert child["parent_sid"] == worker["parent_sid"] == "mgr-a"


def test_spawn_child_accepts_a_worker_parent(fresh_orchestrator_dir, monkeypatch):
    mgr_name = _manager()
    _worker("w1", "alpha", parent_name=mgr_name, parent_sid="mgr-a", kind="worker", depth=1, cwd="/tmp/wt")
    captured = _patch_spawn_worker_tab(monkeypatch)
    monkeypatch.setattr(mcp_server, "_DEFAULT_REGISTRATION_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(mcp_server, "_DEFAULT_REGISTRATION_POLL_SEC", 0.01)
    result = asyncio.run(spawn_child(parent_sid="w1", brief="review it", provider="claude-tui", mode="interactive", model="@opus", effort="xhigh"))
    assert result["parent_name"] == "alpha"
    assert result["parent_sid"] == "w1"
    assert result["kind"] == "subworker"
    assert captured["env"]["CLAUDE_PARENT_MANAGER"] == "alpha"


def test_spawn_worker_refusal_texts_are_unchanged(fresh_orchestrator_dir):
    with pytest.raises(ValueError) as exc:
        asyncio.run(spawn_worker(initial_prompt="do X", manager_sid="nope"))
    assert str(exc.value) == (
        "spawn_worker: manager_sid 'nope' does not match any active manager record — the "
        "worker would register UNSCOPED (parent_manager_name=null) and its done/question/"
        "turn-end events would never route back to you. Pass your own session UUID (the "
        "claude_sid from your boot context), not the funny name; list_managers() shows live "
        "manager sids — if it shows none, your own registration is gone: re-run "
        "become_manager. For a deliberately unscoped spawn pass manager_sid=None."
    )


def test_ask_parent_and_ask_manager_share_the_question_and_differ_in_sentinel(fresh_orchestrator_dir):
    _worker("w1", "alpha", parent_name="boss")
    parent = asyncio.run(mcp_server.ask_manager_impl("w1", "q?", poll_interval=0.01, timeout_sec=0.05,
                                                     tool_name="ask_parent", sid_param="child_sid"))
    manager = asyncio.run(mcp_server.ask_manager_impl("w1", "q?", poll_interval=0.01, timeout_sec=0.05))
    assert parent.startswith("NO_ANSWER_YET:") and manager.startswith("NO_ANSWER_YET:")
    assert "call ask_parent again with the same child_sid" in parent
    assert "call ask_manager again with the same claude_sid" in manager
    assert "ask_manager" not in parent
    assert len(list((paths.QUESTIONS / "boss").glob("*.json"))) == 2


def test_ask_parent_nested_refusal_names_the_tool(fresh_orchestrator_dir):
    _worker("n1", "nested-x", nested=True, nested_parent_name="alpha")
    with pytest.raises(ValueError, match="ask_parent is disabled for nested sessions"):
        asyncio.run(ask_parent("n1", "q?"))
    with pytest.raises(ValueError, match="ask_manager is disabled for nested sessions"):
        asyncio.run(ask_manager("n1", "q?"))


def test_answer_child_answers_a_question(fresh_orchestrator_dir):
    _worker("w1", "alpha", parent_name="boss")
    qid = _write_question("w1", "alpha", "q?", parent_manager_name="boss")
    assert answer_child(qid, "yes") == {"ok": True}
    assert state.read_json(paths.ANSWERS / f"{qid}.json")["answer"] == "yes"


def test_answer_child_and_its_alias_record_the_decider_they_are_given(fresh_orchestrator_dir):
    from dockwright.mcp_server import artifact_get_impl
    for sid in ("w1", "w2"):
        _worker(sid, sid, parent_name="boss")
        state.write_json_atomic(paths.assignment_path(sid), {"assignment_id": f"a-{sid}", "claude_sid": sid, "ticket": "KC", "name": sid})
    q1 = _write_question("w1", "w1", "q1?", parent_manager_name="boss")
    q2 = _write_question("w2", "w2", "q2?", parent_manager_name="boss")
    assert answer_child(q1, "yes", decided_by="engineer") == {"ok": True}
    assert answer_question(q2, "no", decided_by="engineer") == {"ok": True}
    assert artifact_get_impl("KC", "decision", f"q-{q1[:8]}")["decided_by"] == "engineer"
    assert artifact_get_impl("KC", "decision", f"q-{q2[:8]}")["decided_by"] == "engineer"


def test_child_done_writes_the_same_event_as_worker_done(fresh_orchestrator_dir):
    _worker("w1", "alpha", parent_name="boss")
    _worker("w2", "beta", parent_name="boss")
    assert child_done("w1", "done")["ok"] is True
    assert worker_done("w2", "done")["ok"] is True
    events = sorted(state.read_json(p)["worker_name"] for p in (paths.DONE / "boss").glob("*.json"))
    assert events == ["alpha", "beta"]


def test_wait_for_child_returns_timeout_dict_and_alias_raises(fresh_orchestrator_dir):
    _manager()
    _worker("w1", "alpha", parent_name=state.read_json(paths.ACTIVE / "mgr-a.json")["name"])
    result = asyncio.run(wait_for_child_impl("alpha", timeout_sec=0.05, _poll_interval=0.01))
    assert result["found"] == "timeout"
    assert result["sid"] == "w1"
    assert result["timeout_sec"] == 0.05
    assert "wait_for_child" in result["hint"]
    with pytest.raises(TimeoutError, match="worker 'alpha' did not complete within 1s"):
        asyncio.run(wait_for_worker_impl("alpha", timeout_sec=1, _poll_interval=0.01))


def test_wait_for_child_finds_a_child_by_sid(fresh_orchestrator_dir):
    _manager()
    mgr_name = state.read_json(paths.ACTIVE / "mgr-a.json")["name"]
    _worker("w1", "alpha", parent_name=mgr_name)
    worker_done_impl("w1", "finished")
    result = asyncio.run(wait_for_child("w1", parent_sid="mgr-a", timeout_sec=1))
    assert result["found"] == "done"
    assert result["sid"] == "w1"
    assert result["name"] == "alpha"


def test_wait_for_child_refuses_empty_or_dead_parent_sid(fresh_orchestrator_dir):
    _worker("w1", "alpha")
    with pytest.raises(ValueError, match="wait_for_child requires a live parent_sid"):
        asyncio.run(wait_for_child("alpha", parent_sid=""))
    with pytest.raises(ValueError, match=r"wait_for_child: parent_sid 'nope' does not match any active parent record") as exc:
        asyncio.run(wait_for_child("alpha", parent_sid="nope"))
    assert "parent_sid on your own active record" in str(exc.value)
    assert "re-run become_manager" not in str(exc.value)


def test_wait_for_child_non_worker_holder_names_the_tool(fresh_orchestrator_dir):
    _manager()
    with pytest.raises(ValueError, match="wait_for_child only waits on workers"):
        asyncio.run(wait_for_child(state.read_json(paths.ACTIVE / "mgr-a.json")["name"], parent_sid="mgr-a", timeout_sec=1))
    with pytest.raises(ValueError, match="wait_for_worker only waits on workers"):
        asyncio.run(wait_for_worker(state.read_json(paths.ACTIVE / "mgr-a.json")["name"], timeout_sec=1))


def test_wait_for_child_scopes_by_parent(fresh_orchestrator_dir):
    _manager()
    _worker("w1", "alpha", parent_name="someone-else")
    worker_done_impl("w1", "finished")
    with pytest.raises(ValueError, match="no worker named 'alpha'"):
        asyncio.run(wait_for_child("alpha", parent_sid="mgr-a", timeout_sec=1))


def test_list_children_equals_scoped_list_workers(fresh_orchestrator_dir):
    mgr_name = _manager()
    _worker("w1", "alpha", parent_name=mgr_name)
    _worker("w2", "theirs", parent_name="other")
    assert [w["name"] for w in list_children(parent_sid="mgr-a")] == ["alpha"]
    assert list_children(parent_sid="mgr-a") == list_workers(manager_sid="mgr-a")


def test_list_children_refuses_empty_parent_sid(fresh_orchestrator_dir):
    _worker("w1", "alpha", parent_name="boss")
    with pytest.raises(ValueError, match="list_children requires parent_sid"):
        list_children(parent_sid="")


def test_list_children_refuses_dead_parent_sid(fresh_orchestrator_dir):
    _worker("w1", "alpha", parent_name="boss")
    with pytest.raises(ValueError,
                       match=r"list_children: parent_sid 'nope' does not match any active parent record") as exc:
        list_children(parent_sid="nope")
    assert "its siblings only through its manager" in str(exc.value)
    assert "on your own active record" not in str(exc.value)


def test_list_children_lists_a_workers_own_children(fresh_orchestrator_dir):
    mgr_name = _manager()
    _worker("w1", "alpha", parent_name=mgr_name, kind="worker", depth=1)
    assert list_children(parent_sid="w1") == []
    _worker("k1", "alpha-child", parent_name="alpha", parent_sid="w1", kind="subworker", depth=2)
    assert [c["name"] for c in list_children(parent_sid="w1")] == ["alpha-child"]
    assert [c["name"] for c in list_children(parent_sid="mgr-a")] == ["alpha"]


def test_send_kill_tail_aliases_delegate(fresh_orchestrator_dir, monkeypatch):
    _worker("w1", "alpha", parent_name="boss", window_id="%9")
    sent = []
    monkeypatch.setattr(mcp_server, "_resolve_live_worker_window", lambda record: "%9")
    monkeypatch.setattr(mcp_server, "_type_unguarded", lambda wid, text: sent.append((wid, text)) or True)
    delivered = {"status": "delivered", "worker": "alpha", "confirmed": True}
    assert asyncio.run(send_to_child("alpha", "go")) == delivered
    assert asyncio.run(send_manager_to_worker("alpha", "go")) == delivered
    assert sent[0] == sent[1] == ("%9", "[MANAGER] go")
    assert get_child_tail("alpha", lines=5) == get_worker_tail("alpha", lines=5)
    closed = []
    monkeypatch.setattr(mcp_server, "_close_window", lambda wid: closed.append(wid))
    monkeypatch.setattr(mcp_server, "_pid_alive", lambda pid: True)
    assert kill_child("alpha")["killed_pid"] == os.getpid()
    assert closed == ["%9"]


def test_old_tool_docstrings_are_marked_deprecated():
    import inspect
    pairs = [(spawn_worker, "spawn_child"), (ask_manager, "ask_parent"), (answer_question, "answer_child"),
             (worker_done, "child_done"), (wait_for_worker, "wait_for_child"), (list_workers, "list_children"),
             (send_manager_to_worker, "send_to_child"), (kill_worker, "kill_child"), (get_worker_tail, "get_child_tail")]
    for old, new in pairs:
        assert inspect.getdoc(old).startswith(f"[DEPRECATED alias for `{new}` — kept until the alias-deletion design runs]"), old.__name__
    assert inspect.signature(wait_for_child).parameters["timeout_sec"].default == 1500
    assert inspect.signature(wait_for_worker).parameters["timeout_sec"].default == 3600


def test_spawn_worker_wrong_kind_and_unnamed_texts_are_unchanged(fresh_orchestrator_dir):
    from dockwright.mcp_server import _resolve_parent_manager
    _worker("w-1", "wrk", kind="worker")
    state.write_json_atomic(paths.ACTIVE / "n-1.json", {
        "claude_sid": "n-1", "agent": "manager", "name": "nested-deadbeef", "nested": True})
    state.write_json_atomic(paths.ACTIVE / "nw-1.json", {
        "claude_sid": "nw-1", "agent": "worker", "name": "nested-cafe", "nested": True, "kind": "worker"})
    state.write_json_atomic(paths.ACTIVE / "noname-2.json", {"claude_sid": "noname-2", "agent": "manager"})
    expected = {
        "w-1": ("spawn_worker: manager_sid 'w-1' belongs to an active worker record ('wrk'), not a "
                "live top-level manager — the worker would be parented to a name no manager watches. "
                "Pass YOUR manager session UUID (list_managers() shows live sids), or manager_sid=None "
                "for a deliberately unscoped spawn."),
        "n-1": ("spawn_worker: manager_sid 'n-1' belongs to an active nested manager-agent record "
                "('nested-deadbeef'), not a live top-level manager — the worker would be parented to a "
                "name no manager watches. Pass YOUR manager session UUID (list_managers() shows live "
                "sids), or manager_sid=None for a deliberately unscoped spawn."),
        "nw-1": ("spawn_worker: manager_sid 'nw-1' belongs to an active worker record ('nested-cafe'), "
                 "not a live top-level manager — the worker would be parented to a name no manager "
                 "watches. Pass YOUR manager session UUID (list_managers() shows live sids), or "
                 "manager_sid=None for a deliberately unscoped spawn."),
        "noname-2": ("spawn_worker: manager_sid 'noname-2' resolves to a manager record with no name "
                     "(None) — the registration is incomplete or corrupt, and the worker would register "
                     "UNSCOPED (parent_manager_name=null) with its events routed to buckets no manager "
                     "watches. Re-run become_manager to rewrite your registration, or pass "
                     "manager_sid=None for a deliberately unscoped spawn."),
    }
    for sid, text in expected.items():
        with pytest.raises(ValueError) as exc:
            _resolve_parent_manager(sid)
        assert str(exc.value) == text, sid


def test_child_depth_is_role_aware_for_a_depthless_dict_parent():
    assert state.child_depth({"agent": "worker"}, "worker") == 2
    assert state.child_depth({"agent": "manager"}, "worker") == 1
    assert state.child_depth({}, "worker") == 1
    assert state.child_depth({"depth": 1, "agent": "worker"}, "worker") == 2
    assert state.child_depth(None, "worker", kind="subworker") == 2
    assert state.child_depth(None, "worker", kind="worker") == 1
    assert state.child_depth(None, "manager") == 0


def test_tree_fields_carries_kind_override():
    fields = state.tree_fields("worker", kind="subworker", parent_name="alpha", parent_sid="w1",
                               parent_record={"depth": 1, "agent": "worker"})
    assert fields == {"kind": "subworker", "parent_sid": "w1", "parent_name": "alpha", "depth": 2}
    assert state.tree_fields("worker", kind=None, parent_name="boss")["kind"] == "worker"
    assert state.tree_fields("worker", kind="subworker", parent_name="boss", nested=True)["kind"] == "worker"


_CHILD_TOOL_CALLS = {
    "spawn_child": lambda sid: asyncio.run(spawn_child(parent_sid=sid, brief="x", mode="report", model="gpt-5.5")),
    "wait_for_child": lambda sid: asyncio.run(wait_for_child("anyone", parent_sid=sid, timeout_sec=1)),
    "list_children": lambda sid: list_children(parent_sid=sid),
}


@pytest.mark.parametrize("tool", sorted(_CHILD_TOOL_CALLS))
def test_child_tools_refuse_a_nested_parent_with_their_own_text(fresh_orchestrator_dir, tool):
    _worker("n1", "nested-cafe", nested=True, nested_parent_name="alpha", kind="worker")
    state.write_json_atomic(paths.ACTIVE / "n2.json", {
        "claude_sid": "n2", "agent": "manager", "name": "nested-mgr", "nested": True, "kind": "manager"})
    with pytest.raises(ValueError) as exc:
        _CHILD_TOOL_CALLS[tool]("n1")
    text = str(exc.value)
    assert text.startswith(f"{tool}: parent_sid 'n1' belongs to an active nested worker-agent record ('nested-cafe'), "
                           "not a live parent session (a top-level manager or a worker)")
    assert "with THAT session's UUID" in text
    assert "Pass YOUR manager session UUID" not in text
    assert "active manager record" not in text
    with pytest.raises(ValueError, match="active nested manager-agent record"):
        _CHILD_TOOL_CALLS[tool]("n2")


@pytest.mark.parametrize("tool", sorted(_CHILD_TOOL_CALLS))
def test_child_tools_missing_parent_says_parent_record(fresh_orchestrator_dir, tool):
    with pytest.raises(ValueError) as exc:
        _CHILD_TOOL_CALLS[tool]("nope")
    assert f"{tool}: parent_sid 'nope' does not match any active parent record" in str(exc.value)
    assert "active manager record" not in str(exc.value)


def test_child_tools_unnamed_parent_advice_names_both_roles(fresh_orchestrator_dir):
    state.write_json_atomic(paths.ACTIVE / "noname-w.json", {"claude_sid": "noname-w", "agent": "worker", "kind": "worker"})
    with pytest.raises(ValueError) as exc:
        list_children(parent_sid="noname-w")
    assert "resolves to a worker record with no name" in str(exc.value)
    assert "the next SessionStart for a worker" in str(exc.value)


def test_resolve_parent_record_returns_the_record_for_manager_and_worker(fresh_orchestrator_dir):
    from dockwright.mcp_server import _resolve_parent_record
    mgr_name = _manager()
    _worker("w1", "alpha", parent_name=mgr_name, kind="worker", depth=1)
    assert _resolve_parent_record("mgr-a", tool="spawn_child")["name"] == mgr_name
    assert _resolve_parent_record("w1", tool="spawn_child")["name"] == "alpha"
    assert _resolve_parent_record(None, tool="spawn_child") is None
    with pytest.raises(ValueError, match="active worker record"):
        _resolve_parent_record("w1", tool="spawn_worker")


_SPAWN_TAB_OWNED_KEYS = {"CLAUDE_AGENT", "CLAUDE_WORKER_NAME", "CLAUDE_WORKER_RUNTIME",
                         "CLAUDE_WORKER_PROVIDER"}


def _fast_registration(monkeypatch):
    monkeypatch.setattr(mcp_server, "_DEFAULT_REGISTRATION_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(mcp_server, "_DEFAULT_REGISTRATION_POLL_SEC", 0.01)


def test_spawn_child_from_worker_sets_kind_env_and_defaults(fresh_orchestrator_dir, monkeypatch):
    mgr_name = _manager()
    _worker("w1", "alpha", parent_name=mgr_name, parent_sid="mgr-a", kind="worker", depth=1, cwd="/tmp/wt")
    captured = _patch_spawn_that_registers(monkeypatch, "k1")
    _fast_registration(monkeypatch)
    result = asyncio.run(spawn_child(parent_sid="w1", brief="review", env={"MY": "1"},
                                     provider="claude-tui", mode="interactive", model="@opus", effort="xhigh"))
    env = captured["env"]
    assert env["CLAUDE_PARENT_MANAGER"] == "alpha"
    assert env["CLAUDE_PARENT_SID"] == "w1"
    assert env["CLAUDE_WORKER_KIND"] == "subworker"
    assert (env["CLAUDE_PARENT_PID"], env["CLAUDE_DOMAIN"], env["CLAUDE_ITERM_SID"]) == ("", "", "")
    assert env["MY"] == "1"
    assert captured["cwd"] == "/tmp/wt"
    assert captured["name"] == "alpha-child"
    assert result["status"] == "registered"
    assert result["kind"] == "subworker"
    assert result["name"] == "alpha-child"
    assignment = state.read_json(paths.ASSIGNMENTS_PENDING / f"{env['CLAUDE_ASSIGNMENT_ID']}.json")
    assert assignment["parent_name"] == "alpha"
    assert assignment["parent_sid"] == "w1"


def test_spawn_child_from_worker_covers_every_orchestrator_env_key_but_orch_account(fresh_orchestrator_dir, monkeypatch):
    mgr_name = _manager()
    _worker("w1", "alpha", parent_name=mgr_name, kind="worker", depth=1)
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    asyncio.run(spawn_child(parent_sid="w1", brief="review", cwd="/tmp/x", provider="claude-tui", mode="interactive", model="@opus", effort="xhigh"))
    covered = set(captured["env"]) | _SPAWN_TAB_OWNED_KEYS
    assert covered >= set(paths.ORCHESTRATOR_ENV_KEYS) - {"CLAUDE_ORCH_ACCOUNT"}


def test_spawn_child_from_manager_sets_no_kind_or_cleared_keys(fresh_orchestrator_dir, monkeypatch):
    _manager()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    result = asyncio.run(spawn_child(parent_sid="mgr-a", brief="do X", cwd="/tmp/x", provider="claude-tui", mode="interactive", model="@opus", effort="xhigh"))
    for key in ("CLAUDE_WORKER_KIND", "CLAUDE_PARENT_PID", "CLAUDE_DOMAIN", "CLAUDE_ITERM_SID"):
        assert key not in captured["env"], key
    assert result["kind"] == "worker"
    assert result["name"].startswith("worker-")


def test_spawn_child_strips_caller_worker_kind(fresh_orchestrator_dir, monkeypatch):
    _manager()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    asyncio.run(spawn_child(parent_sid="mgr-a", brief="do X", cwd="/tmp/x", env={"CLAUDE_WORKER_KIND": "subworker"}, provider="claude-tui", mode="interactive", model="@opus", effort="xhigh"))
    assert "CLAUDE_WORKER_KIND" not in captured["env"]


def test_spawn_child_refuses_at_max_depth_before_pending_assignment(fresh_orchestrator_dir, monkeypatch):
    mgr_name = _manager()
    _worker("w1", "alpha", parent_name=mgr_name, kind="worker", depth=1)
    _worker("k1", "alpha-child", parent_name="alpha", parent_sid="w1", kind="subworker", depth=2)
    captured = _patch_spawn_worker_tab(monkeypatch)
    with pytest.raises(ValueError) as exc:
        asyncio.run(spawn_child(parent_sid="k1", brief="deeper", mode="report", model="gpt-5.5"))
    assert "spawn_child: parent_sid 'k1' ('alpha-child') is at depth 2; its child would be at depth 3, which reaches [subworkers] max_depth = 3" in str(exc.value)
    assert captured == {}
    assert list(paths.ASSIGNMENTS_PENDING.glob("*.json")) == []


def test_spawn_child_allows_a_third_layer_when_max_depth_is_four(fresh_orchestrator_dir, monkeypatch):
    mgr_name = _manager()
    _worker("w1", "alpha", parent_name=mgr_name, kind="worker", depth=1)
    _worker("k1", "alpha-child", parent_name="alpha", parent_sid="w1", kind="subworker", depth=2, cwd="/tmp/wt")
    monkeypatch.setattr(mcp_server.config, "subworkers_max_depth", lambda: 4)
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    result = asyncio.run(spawn_child(parent_sid="k1", brief="deeper", provider="claude-tui", mode="interactive", model="@opus", effort="xhigh"))
    assert result["kind"] == "subworker"
    assert captured["env"]["CLAUDE_WORKER_KIND"] == "subworker"
    assert captured["env"]["CLAUDE_PARENT_MANAGER"] == "alpha-child"


def test_spawn_child_from_depthless_worker_counts_it_as_depth_one(fresh_orchestrator_dir, monkeypatch):
    mgr_name = _manager()
    _worker("w1", "alpha", parent_name=mgr_name)
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    monkeypatch.setattr(mcp_server.config, "subworkers_max_depth", lambda: 2)
    with pytest.raises(ValueError, match="is at depth 1; its child would be at depth 2, which reaches \\[subworkers\\] max_depth = 2"):
        asyncio.run(spawn_child(parent_sid="w1", brief="review", cwd="/tmp/x", mode="report", model="gpt-5.5"))
    monkeypatch.setattr(mcp_server.config, "subworkers_max_depth", lambda: 3)
    result = asyncio.run(spawn_child(parent_sid="w1", brief="review", cwd="/tmp/x", provider="claude-tui", mode="interactive", model="@opus", effort="xhigh"))
    assert result["kind"] == "subworker"
    assert captured["env"]["CLAUDE_WORKER_KIND"] == "subworker"


def test_register_self_stamps_subworker_kind(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i", pid=os.getpid())
    register_self_impl(claude_sid="k1", agent="worker", name="alpha-child", cwd="/x", iterm_sid="i",
                       pid=os.getpid(), parent_manager_name="alpha", parent_sid="w1", kind="subworker")
    child = state.read_json(paths.ACTIVE / "k1.json")
    assert (child["agent"], child["kind"], child["depth"], child["parent_name"]) == ("worker", "subworker", 2, "alpha")


def test_child_events_route_to_the_worker_and_the_manager_is_blind(fresh_orchestrator_dir):
    mgr_name = _manager()
    _worker("w1", "alpha", parent_name=mgr_name, parent_sid="mgr-a", kind="worker", depth=1)
    _worker("k1", "alpha-child", parent_name="alpha", parent_sid="w1", kind="subworker", depth=2)
    assert child_done("k1", "reviewed")["ok"] is True
    assert list((paths.DONE / "alpha").glob("k1-*.json"))
    assert not (paths.DONE / mgr_name).exists() or not list((paths.DONE / mgr_name).glob("*.json"))
    result = asyncio.run(mcp_server.ask_manager_impl("k1", "q?", poll_interval=0.01, timeout_sec=0.05,
                                                     tool_name="ask_parent", sid_param="child_sid"))
    assert result.startswith("NO_ANSWER_YET:")
    assert list((paths.QUESTIONS / "alpha").glob("*.json"))
    assert [w["name"] for w in list_workers_impl(manager_name=mgr_name)] == ["alpha"]
    assert [w["name"] for w in list_workers_impl(manager_name="alpha")] == ["alpha-child"]
    assert mcp_server.list_pending_questions_impl(manager_name=mgr_name) == []
    assert [q["worker_name"] for q in mcp_server.list_pending_questions_impl(manager_name="alpha")] == ["alpha-child"]
    with pytest.raises(ValueError, match="no worker named 'alpha-child'"):
        asyncio.run(wait_for_worker_impl("alpha-child", timeout_sec=1, _poll_interval=0.01, manager_name=mgr_name))
    assert asyncio.run(wait_for_child("alpha-child", parent_sid="w1", timeout_sec=1))["found"] == "done"


def test_send_to_child_keeps_the_manager_marker_for_a_subworker(fresh_orchestrator_dir, monkeypatch):
    _worker("k1", "alpha-child", parent_name="alpha", kind="subworker", depth=2, window_id="%9")
    sent = []
    monkeypatch.setattr(mcp_server, "_resolve_live_worker_window", lambda record: "%9")
    monkeypatch.setattr(mcp_server, "_type_unguarded", lambda wid, text: sent.append((wid, text)) or True)
    assert asyncio.run(send_to_child("alpha-child", "go")) == {"status": "delivered", "worker": "alpha-child",
                                                               "confirmed": True}
    assert sent == [("%9", "[MANAGER] go")]


def _child_of_worker(mgr_name):
    _worker("w1", "alpha", parent_name=mgr_name, parent_sid="mgr-a", kind="worker", depth=1)
    _worker("k1", "alpha-child", parent_name="alpha", parent_sid="w1", kind="subworker", depth=2)


def test_wait_for_child_returns_the_question_then_done_after_answer(fresh_orchestrator_dir):
    _child_of_worker(_manager())
    qid = _write_question("k1", "alpha-child", "which branch?", parent_manager_name="alpha")
    result = asyncio.run(wait_for_child("alpha-child", parent_sid="w1", timeout_sec=1))
    assert result["found"] == "question"
    assert (result["name"], result["sid"], result["question_id"], result["question"]) == \
        ("alpha-child", "k1", qid, "which branch?")
    assert "answer_child" in result["hint"]
    assert answer_child(qid, "main") == {"ok": True}
    worker_done_impl("k1", "reviewed")
    assert asyncio.run(wait_for_child("alpha-child", parent_sid="w1", timeout_sec=1))["found"] == "done"


def test_wait_for_child_surfaces_a_question_asked_mid_wait(fresh_orchestrator_dir):
    _child_of_worker(_manager())

    async def scenario():
        waiter = asyncio.create_task(wait_for_child_impl("alpha-child", timeout_sec=2, _poll_interval=0.01,
                                                         manager_name="alpha"))
        await asyncio.sleep(0.05)
        _write_question("k1", "alpha-child", "late?", parent_manager_name="alpha")
        return await waiter

    result = asyncio.run(scenario())
    assert result["found"] == "question"
    assert result["question"] == "late?"


def test_wait_for_child_prefers_done_over_a_stale_question(fresh_orchestrator_dir):
    _child_of_worker(_manager())
    _write_question("k1", "alpha-child", "q?", parent_manager_name="alpha")
    worker_done_impl("k1", "finished")
    assert asyncio.run(wait_for_child("alpha-child", parent_sid="w1", timeout_sec=1))["found"] == "done"


def test_a_wait_after_a_new_message_returns_only_the_next_report(fresh_orchestrator_dir, monkeypatch):
    _child_of_worker(_manager())
    monkeypatch.setattr(mcp_server, "_resolve_live_worker_window", lambda record: "%9")
    monkeypatch.setattr(mcp_server, "_type_unguarded", lambda wid, text: True)
    child_done("k1", "round 1 verdict")
    first = asyncio.run(wait_for_child("alpha-child", parent_sid="w1", timeout_sec=1))
    assert (first["found"], first["summary"]) == ("done", "round 1 verdict")
    for p in paths.DONE.rglob("*.json"):
        event = state.read_json(p)
        state.write_json_atomic(p, {**event, "completed_at": event["completed_at"] - 10})
    assert asyncio.run(send_to_child("alpha-child", "round 2"))["status"] == "delivered"
    assert asyncio.run(wait_for_child("alpha-child", parent_sid="w1", timeout_sec=1))["found"] == "timeout"
    child_done("k1", "round 2 verdict")
    second = asyncio.run(wait_for_child("alpha-child", parent_sid="w1", timeout_sec=1))
    assert second["summary"] == "round 2 verdict" and second["event_id"] != first["event_id"]


def test_wait_for_worker_alias_does_not_surface_questions(fresh_orchestrator_dir):
    _child_of_worker(_manager())
    _write_question("k1", "alpha-child", "q?", parent_manager_name="alpha")
    with pytest.raises(TimeoutError):
        asyncio.run(wait_for_worker_impl("alpha-child", timeout_sec=0.05, _poll_interval=0.01, manager_name="alpha"))
    result = asyncio.run(wait_for_child_impl("alpha-child", timeout_sec=0.05, _poll_interval=0.01,
                                             manager_name="alpha", surface_questions=False))
    assert result["found"] == "timeout"


def test_wait_for_child_on_an_exited_child_is_exited_even_with_a_stray_question(fresh_orchestrator_dir):
    _manager()
    _worker("w1", "alpha", parent_name=state.read_json(paths.ACTIVE / "mgr-a.json")["name"], kind="worker", depth=1)
    state.write_json_atomic(paths.CLOSED / "k1.json", {
        "claude_sid": "k1", "name": "alpha-child", "cwd": "/x", "closed_at": 1.0,
        "parent_name": "alpha", "parent_sid": "w1", "kind": "subworker", "depth": 2,
    })
    _write_question("k1", "alpha-child", "orphan?", parent_manager_name="alpha")
    result = asyncio.run(wait_for_child("alpha-child", parent_sid="w1", timeout_sec=1))
    assert result["found"] == "exited"


def test_list_children_reports_the_oldest_pending_question(fresh_orchestrator_dir):
    _child_of_worker(_manager())
    assert list_children(parent_sid="w1")[0]["question_pending"] is None
    first = _write_question("k1", "alpha-child", "first?", parent_manager_name="alpha")
    q_path = paths.QUESTIONS / "alpha" / f"{first}.json"
    record = state.read_json(q_path)
    record["asked_at"] = 1.0
    state.write_json_atomic(q_path, record)
    _write_question("k1", "alpha-child", "second?", parent_manager_name="alpha")
    assert list_children(parent_sid="w1")[0]["question_pending"] == first


def test_wait_for_child_returns_question_to_a_manager_parent(fresh_orchestrator_dir):
    mgr_name = _manager()
    _worker("w1", "alpha", parent_name=mgr_name, parent_sid="mgr-a", kind="worker", depth=1)
    _write_question("w1", "alpha", "q?", parent_manager_name=mgr_name)
    result = asyncio.run(wait_for_child("alpha", parent_sid="mgr-a", timeout_sec=1))
    assert result["found"] == "question"
    assert (result["name"], result["sid"], result["question"]) == ("alpha", "w1", "q?")


def test_wait_for_child_is_exited_when_the_child_leaves_between_the_question_and_the_return(
        fresh_orchestrator_dir, monkeypatch):
    _child_of_worker(_manager())
    _write_question("k1", "alpha-child", "racing?", parent_manager_name="alpha")
    real = mcp_server._oldest_pending_question
    calls = []

    def racing(sid, manager_name=None):
        if not calls:
            calls.append(sid)
            (paths.ACTIVE / "k1.json").unlink()
        return real(sid, manager_name)

    monkeypatch.setattr(mcp_server, "_oldest_pending_question", racing)
    result = asyncio.run(wait_for_child_impl("alpha-child", timeout_sec=1, _poll_interval=0.01,
                                             manager_name="alpha"))
    assert calls == ["k1"]
    assert result["found"] == "exited"


def test_resume_of_a_subworker_restamps_its_kind(fresh_orchestrator_dir, monkeypatch):
    _worker("w1", "alpha", parent_name="boss", kind="worker", depth=1)
    state.write_json_atomic(paths.CLOSED / "k1.json", {
        "claude_sid": "k1", "name": "alpha-child", "cwd": "/tmp/x", "closed_at": 1.0,
        "runtime": "claude", "parent_name": "alpha", "parent_sid": "w1",
        "kind": "subworker", "depth": 2,
    })
    monkeypatch.setattr(mcp_server, "_has_live_transcript", lambda sid, runtime=None: True)
    captured = _patch_spawn_that_registers(monkeypatch, "k1")
    assert asyncio.run(resume_worker_impl("alpha-child", _registration_timeout_sec=1.0, model="@opus", effort="xhigh",
                                          _poll_interval=0.01))["ok"] is True
    assert captured["env"]["CLAUDE_WORKER_KIND"] == "subworker"
    assert captured["env"]["CLAUDE_PARENT_SID"] == "w1"
    assert captured["env"]["CLAUDE_PARENT_MANAGER"] == "alpha"


def test_resume_of_a_plain_worker_sends_no_worker_kind(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="boss-new", iterm_sid="i0", domain="general", name="boss")
    state.write_json_atomic(paths.CLOSED / "w1.json", {
        "claude_sid": "w1", "name": "alpha", "cwd": "/tmp/x", "closed_at": 1.0, "runtime": "claude",
        "parent_name": "boss", "parent_sid": "boss-old", "kind": "worker", "depth": 1,
    })
    monkeypatch.setattr(mcp_server, "_has_live_transcript", lambda sid, runtime=None: True)
    captured = _patch_spawn_that_registers(monkeypatch, "w1")
    assert asyncio.run(resume_worker_impl("alpha", _registration_timeout_sec=1.0, model="@opus", effort="xhigh",
                                          _poll_interval=0.01))["ok"] is True
    assert "CLAUDE_WORKER_KIND" not in captured["env"]


from dockwright.providers import get_provider
from dockwright.mcp_server import send_to_child_impl


def _manager_and_worker():
    register_self_impl(claude_sid="mgr-a", agent="manager", name="boss", cwd="/x", iterm_sid="", pid=os.getpid())
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/tmp/x", iterm_sid="", pid=os.getpid(),
                       parent_manager_name="boss", parent_sid="mgr-a")


def test_spawn_child_from_worker_defaults_to_codex_exec_and_is_gated(fresh_orchestrator_dir, monkeypatch):
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    with pytest.raises(ValueError, match="codex is disabled"):
        asyncio.run(spawn_child(parent_sid="w1", brief="review", mode="report", model="gpt-5.5"))
    with pytest.raises(ValueError, match='provider="claude-tui"'):
        asyncio.run(spawn_child(parent_sid="w1", brief="review", mode="report", model="gpt-5.5"))
    assert captured == {}
    assert list(paths.ASSIGNMENTS_PENDING.glob("*")) == []


def test_spawn_child_with_an_explicit_provider_keeps_the_bare_gate_refusal(fresh_orchestrator_dir, monkeypatch):
    _manager_and_worker()
    _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    with pytest.raises(ValueError) as exc:
        asyncio.run(spawn_child(parent_sid="w1", brief="review", provider="codex-exec", mode="report", model="gpt-5.5"))
    assert "codex is disabled" in str(exc.value)
    assert 'provider="claude-tui"' not in str(exc.value)


def test_spawn_child_from_manager_defaults_to_claude_tui_and_builds_the_old_spawn(fresh_orchestrator_dir, monkeypatch):
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    result = asyncio.run(spawn_child(parent_sid="mgr-a", brief="do X", cwd="/tmp/x", provider="claude-tui", mode="interactive", model="@opus", effort="xhigh"))
    assert captured["provider"] is None and captured["child_round"] is None
    assert captured["runtime"] == "claude" and captured["initial_prompt"].startswith("do X")
    assert result["provider"] is None and "report_path" not in result


def test_spawn_child_with_codex_exec_writes_round_files_assignment_and_env(fresh_orchestrator_dir, monkeypatch, tmp_path):
    from tests.conftest import allow_codex
    allow_codex(monkeypatch, tmp_path, "/tmp")
    _manager_and_worker()
    captured = _patch_spawn_that_registers(monkeypatch, "c1")
    _fast_registration(monkeypatch)
    gates = []
    monkeypatch.setattr(spawner, "usage_spawn_gate", lambda force=False: gates.append(force) or {"status": "ok"})
    result = asyncio.run(spawn_child(parent_sid="w1", brief="review it", provider="codex-exec", mode="report", model="gpt-5.5",
                                     env={"CODEX_HOME": "/h", "CLAUDE_WORKER_PROVIDER": "evil", "CLAUDE_PARENT_SID": "evil"}))
    assert gates == []
    assert captured["provider"].name == "codex-exec"
    rnd = captured["child_round"]
    assert rnd.mode == "report" and rnd.cwd == "/tmp/x" and rnd.resume_sid is None and rnd.session_id is None
    assert rnd.brief_path.read_text().startswith(get_provider("codex-exec").bootstrap("report"))
    assert "review it" in rnd.brief_path.read_text()
    assert captured["tab_title"] == "alpha/alpha-child r1" and captured["initial_prompt"] == ""
    assert captured["env"]["CODEX_HOME"] == "/h" and captured["env"]["CLAUDE_WORKER_KIND"] == "subworker"
    assert "CLAUDE_WORKER_PROVIDER" not in captured["env"]
    pending = list(state.list_json_in(paths.ASSIGNMENTS_PENDING))[0]
    assert pending["provider"] == "codex-exec" and pending["mode"] == "report" and pending["round"] == 1
    assert pending["report_path"] == str(rnd.report_path) and pending["log_path"] == str(rnd.log_path)
    assert pending["rounds_dir"] == str(paths.child_rounds_dir(pending["assignment_id"]))
    assert pending["spawn_env"] == {"CODEX_HOME": "/h"}
    assert pending["round_started_at"] > 0
    assert result["provider"] == "codex-exec" and result["mode"] == "report" and result["round"] == 1
    assert result["report_path"] == str(rnd.report_path) and result["runtime"] == "codex"


def test_a_codex_child_started_during_a_usage_cap_pause_fails_naming_the_cap(fresh_orchestrator_dir, monkeypatch,
                                                                              tmp_path):
    from tests.conftest import allow_codex
    allow_codex(monkeypatch, tmp_path, "/tmp")
    _manager_and_worker()
    _fast_registration(monkeypatch)
    monkeypatch.setattr(paths, "USAGE_CAPS", tmp_path / "usage-caps.toml")
    monkeypatch.setattr(paths, "ACCOUNT_USAGE", tmp_path / "usage")
    monkeypatch.setattr(config, "account_names", lambda: ("a", "b"))
    (tmp_path / "usage").mkdir()
    (tmp_path / "usage-caps.toml").write_text("[b]\nfive_hour = 70\n")
    now = time.time()
    (tmp_path / "usage" / "b.json").write_text(json.dumps(
        {"five_hour_pct": 71, "five_hour_resets_at": now + 3600, "ts": now}))
    with pytest.raises(spawner.UsageCapPaused, match="usage cap: b 5h 71% ≥ 70%; force does not lift"):
        asyncio.run(spawn_child(parent_sid="w1", brief="review it", provider="codex-exec", mode="report", model="gpt-5.5"))
    assert list(paths.ASSIGNMENTS_PENDING.glob("*")) == []


def test_report_mode_child_brief_carries_no_orchestrator_footers(fresh_orchestrator_dir, monkeypatch, tmp_path):
    from tests.conftest import allow_codex
    allow_codex(monkeypatch, tmp_path, "/tmp")
    _manager_and_worker()
    captured = _patch_spawn_that_registers(monkeypatch, "c1")
    _fast_registration(monkeypatch)
    asyncio.run(spawn_child(parent_sid="w1", brief="review it", provider="codex-exec", mode="report", model="gpt-5.5", task_key="T-1"))
    brief = captured["child_round"].brief_path.read_text()
    assert "review it" in brief and "[orchestrator]" not in brief
    assert "Artifact discipline" not in brief and "Repo freshness" not in brief
    captured.clear()
    asyncio.run(spawn_child(parent_sid="w1", brief="review it", provider="claude-exec", mode="report", model="claude-sonnet-5", effort="high", task_key="T-1"))
    brief = captured["child_round"].brief_path.read_text()
    assert "[orchestrator]" not in brief
    assert "Artifact discipline" not in brief and "Repo freshness" not in brief


def test_interactive_child_brief_keeps_the_orchestrator_footers(fresh_orchestrator_dir, monkeypatch):
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    asyncio.run(spawn_child(parent_sid="w1", brief="ask", provider="claude-exec", mode="interactive", model="claude-sonnet-5", effort="high", task_key="T-1"))
    brief = captured["child_round"].brief_path.read_text()
    assert "Artifact discipline" in brief and "Repo freshness" in brief


def _pending_only_child(name="alpha-review-tier2-r1", window="%7"):
    aid = "aid-pending-1"
    state.write_json_atomic(paths.pending_assignment_path(aid), {
        "assignment_id": aid, "requested_name": name, "name": name, "provider": "codex-exec",
        "mode": "report", "parent_name": "alpha", "parent_sid": "w1", "cwd": "/tmp/x"})
    if window:
        paths.pending_window_path(aid).write_text(window)
    return aid


def test_kill_child_closes_the_dead_window_of_a_never_registered_child(fresh_orchestrator_dir, monkeypatch):
    _manager_and_worker()
    aid = _pending_only_child()
    _pane_state(monkeypatch, "dead")
    closed = []
    monkeypatch.setattr(mcp_server, "_close_window", lambda wid: closed.append(wid))
    assert kill_child("alpha-review-tier2-r1") == {"closed_window": "%7", "already_dead": True, "pending_assignment": aid}
    assert closed == ["%7"]
    assert not paths.pending_assignment_path(aid).exists() and not paths.pending_window_path(aid).exists()


def test_kill_child_drops_the_files_of_a_never_registered_child_whose_pane_is_gone(fresh_orchestrator_dir, monkeypatch):
    _manager_and_worker()
    aid = _pending_only_child()
    _pane_state(monkeypatch, "gone")
    closed = []
    monkeypatch.setattr(mcp_server, "_close_window", lambda wid: closed.append(wid))
    assert kill_child("alpha-review-tier2-r1") == {"closed_window": None, "already_dead": True, "pending_assignment": aid}
    assert closed == [] and not paths.pending_assignment_path(aid).exists()


@pytest.mark.parametrize("pane", ["alive", "unknown"])
def test_kill_child_leaves_a_never_registered_child_alone_while_its_pane_may_still_register(fresh_orchestrator_dir, monkeypatch, pane):
    _manager_and_worker()
    aid = _pending_only_child()
    _pane_state(monkeypatch, pane)
    closed = []
    monkeypatch.setattr(mcp_server, "_close_window", lambda wid: closed.append(wid))
    result = kill_child("alpha-review-tier2-r1")
    assert result["closed_window"] is None and result["already_dead"] is False and result["pending_assignment"] == aid
    assert pane in result["note"]
    assert closed == [] and paths.pending_assignment_path(aid).exists() and paths.pending_window_path(aid).exists()
    with pytest.raises(ValueError, match="no worker named 'ghost'"):
        kill_child("ghost")


def test_kill_child_leaves_a_never_registered_child_whose_window_sidecar_is_absent(fresh_orchestrator_dir, monkeypatch):
    _manager_and_worker()
    aid = _pending_only_child(window=None)
    _pane_state(monkeypatch, "dead")
    closed = []
    monkeypatch.setattr(mcp_server, "_close_window", lambda wid: closed.append(wid))
    result = kill_child("alpha-review-tier2-r1")
    assert result["closed_window"] is None and result["already_dead"] is False
    assert result["pending_assignment"] == aid and "unknown" in result["note"]
    assert closed == [] and paths.pending_assignment_path(aid).exists()


def test_kill_child_skips_a_pending_file_that_is_not_a_dict(fresh_orchestrator_dir, monkeypatch):
    _manager_and_worker()
    aid = _pending_only_child()
    paths.pending_assignment_path("aid-corrupt").write_text('["not", "a", "dict"]')
    _pane_state(monkeypatch, "gone")
    closed = []
    monkeypatch.setattr(mcp_server, "_close_window", lambda wid: closed.append(wid))
    assert kill_child("alpha-review-tier2-r1") == {"closed_window": None, "already_dead": True,
                                               "pending_assignment": aid}
    with pytest.raises(ValueError, match="no worker named 'ghost'"):
        kill_child("ghost")


def test_spawn_child_interactive_claude_exec_gets_session_id_and_model_default(fresh_orchestrator_dir, monkeypatch):
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    asyncio.run(spawn_child(parent_sid="w1", brief="ask", provider="claude-exec", mode="interactive", model="claude-sonnet-5", effort="high"))
    rnd = captured["child_round"]
    assert rnd.mode == "interactive" and rnd.model == "claude-sonnet-5"
    assert rnd.session_id and len(rnd.session_id) == 36
    assert rnd.brief_path.read_text().startswith("\n\nask")
    assert captured["runtime"] == "claude" and captured["extra_args"] == []


def test_spawn_child_rejects_unknown_provider_and_mode_before_pending(fresh_orchestrator_dir, monkeypatch):
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    with pytest.raises(ValueError, match="unknown provider 'nope'"):
        asyncio.run(spawn_child(parent_sid="w1", brief="x", provider="nope"))
    with pytest.raises(ValueError, match="mode must be one of"):
        asyncio.run(spawn_child(parent_sid="w1", brief="x", provider="claude-exec", mode="review", model="claude-sonnet-5", effort="high"))
    with pytest.raises(ValueError, match="mode must be one of"):
        asyncio.run(spawn_child(parent_sid="mgr-a", brief="x", cwd="/tmp/x", mode="nonsense", model="@opus", effort="xhigh"))
    assert captured == {} and list(paths.ASSIGNMENTS_PENDING.glob("*")) == []


def test_spawn_child_provider_error_when_codex_never_started(fresh_orchestrator_dir, monkeypatch, tmp_path):
    from tests.conftest import allow_codex
    allow_codex(monkeypatch, tmp_path, "/tmp")
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)

    async def dead_spawn(**kwargs):
        captured.update(kwargs)
        kwargs["child_round"].log_path.write_text("hook: SessionStart Completed\nError: Failed to load cloud requirements (402)\n")
        return ("999", kwargs["name"])

    monkeypatch.setattr(spawner, "spawn_worker_tab", dead_spawn)
    result = asyncio.run(spawn_child(parent_sid="w1", brief="x", provider="codex-exec", mode="report", model="gpt-5.5"))
    assert result["status"] == "provider-error"
    assert "402" in result["reason"]
    pending = list(state.list_json_in(paths.ASSIGNMENTS_PENDING))
    assert [p["assignment_id"] for p in pending] == [result["assignment_id"]]
    assert pending[0]["provider"] == "codex-exec" and pending[0]["round"] == 1


def _claude_exec_spawn_with_pane(monkeypatch, pane):
    from dockwright import terminal
    _manager_and_worker()
    _fast_registration(monkeypatch)

    async def spawn(**kwargs):
        kwargs["child_round"].log_path.write_text("Error: settings file not found\n")
        return ("%5", kwargs["name"])

    monkeypatch.setattr(spawner, "spawn_worker_tab", spawn)
    monkeypatch.setattr(terminal.TmuxDriver, "pane_state", lambda self, target: pane if target == "%5" else "gone")
    return asyncio.run(spawn_child(parent_sid="w1", brief="x", provider="claude-exec", mode="report", model="claude-sonnet-5", effort="high"))


def test_spawn_child_provider_error_when_the_claude_exec_pane_died(fresh_orchestrator_dir, monkeypatch):
    result = _claude_exec_spawn_with_pane(monkeypatch, "dead")
    assert result["status"] == "provider-error"
    assert result["reason"] == "Error: settings file not found"
    pending = list(state.list_json_in(paths.ASSIGNMENTS_PENDING))
    assert [p["assignment_id"] for p in pending] == [result["assignment_id"]]


def test_spawn_child_no_register_while_the_claude_exec_pane_lives(fresh_orchestrator_dir, monkeypatch):
    assert _claude_exec_spawn_with_pane(monkeypatch, "alive")["status"] == "no_register"


def _headless_child(monkeypatch, tmp_path, sid="c1", name="alpha-child", provider="codex-exec", active=True, round_no=1):
    rounds = tmp_path / "rounds"
    rounds.mkdir(exist_ok=True)
    report = rounds / f"r{round_no}.report.md"
    record = {"claude_sid": sid, "agent": "worker", "kind": "subworker", "name": name, "cwd": "/tmp/x",
              "window_id": "%7", "pid": os.getpid(), "started_at": 0, "state": "idle", "runtime": "codex",
              "provider": provider, "parent_manager_name": "alpha", "parent_name": "alpha", "parent_sid": "w1", "depth": 2}
    if active:
        state.write_json_atomic(paths.ACTIVE / f"{sid}.json", record)
    else:
        record.update({"closed_at": 1.0, "closed_reason": "session_end"})
        state.write_json_atomic(paths.CLOSED / f"{sid}.json", record)
    state.write_json_atomic(paths.assignment_path(sid), {
        "assignment_id": "asg1", "claude_sid": sid, "name": name, "provider": provider, "mode": "report",
        "model": None, "round": round_no, "round_started_at": time.time() - 100, "report_path": str(report),
        "log_path": str(rounds / f"r{round_no}.log"), "rounds_dir": str(rounds), "spawn_env": {"CODEX_HOME": "/h"},
        "spawn_extra_args": ["--x"], "parent_name": "alpha", "parent_sid": "w1", "cwd": "/tmp/x"})
    return report


def test_wait_for_child_returns_done_from_the_report_file_before_events(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    report = _headless_child(monkeypatch, tmp_path)
    _write_question("c1", "alpha-child", "q?", parent_manager_name="alpha")
    report.write_text("REVIEW: fine")
    r = asyncio.run(wait_for_child_impl("alpha-child", timeout_sec=2, _poll_interval=0.05, manager_name="alpha"))
    assert r["found"] == "done" and r["summary"] == "REVIEW: fine" and r["report_path"] == str(report)
    assert r["round"] == 1 and r["event_id"] is None and r["completed_at"] > 0
    alias = asyncio.run(wait_for_worker_impl("alpha-child", timeout_sec=2, _poll_interval=0.05, manager_name="alpha"))
    assert alias["found"] == "done" and alias["summary"] == "REVIEW: fine"


def test_wait_for_child_ignores_an_empty_report_file(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    report = _headless_child(monkeypatch, tmp_path)
    report.write_text("")
    r = asyncio.run(wait_for_child_impl("alpha-child", timeout_sec=0.3, _poll_interval=0.05, manager_name="alpha"))
    assert r["found"] == "timeout"
    report.write_text("REVIEW: fine")
    r2 = asyncio.run(wait_for_child_impl("alpha-child", timeout_sec=0.3, _poll_interval=0.05, manager_name="alpha"))
    assert r2["found"] == "done" and r2["summary"] == "REVIEW: fine"


def test_wait_for_child_waits_five_seconds_for_the_report_after_the_record_vanishes(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    report = _headless_child(monkeypatch, tmp_path, active=False)
    monkeypatch.setattr(mcp_server, "_REPORT_GRACE_SEC", 1.0)

    async def run():
        task = asyncio.create_task(wait_for_child_impl("alpha-child", timeout_sec=5, _poll_interval=0.05, manager_name="alpha"))
        await asyncio.sleep(0.3)
        report.write_text("late report")
        return await task

    r = asyncio.run(run())
    assert r["found"] == "done" and r["summary"] == "late report"


def test_wait_for_child_exited_without_report_names_the_reason_on_both_paths(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path, active=False)
    monkeypatch.setattr(mcp_server, "_REPORT_GRACE_SEC", 0.2)
    r = asyncio.run(wait_for_child_impl("alpha-child", timeout_sec=2, _poll_interval=0.05, manager_name="alpha"))
    assert r == {"found": "exited", "name": "alpha-child", "sid": "c1", "reason": "session_ended_without_report"}
    _headless_child(monkeypatch, tmp_path, sid="c2", name="alpha-child-2", active=True)

    async def run():
        task = asyncio.create_task(wait_for_child_impl("alpha-child-2", timeout_sec=5, _poll_interval=0.05, manager_name="alpha"))
        await asyncio.sleep(0.2)
        (paths.ACTIVE / "c2.json").unlink()
        return await task

    r2 = asyncio.run(run())
    assert r2["found"] == "exited" and r2["reason"] == "session_ended_without_report"
    _worker("t1", "tui-child", parent_name="alpha", parent_manager_name="alpha")
    (paths.ACTIVE / "t1.json").unlink()
    state.write_json_atomic(paths.CLOSED / "t1.json", {"claude_sid": "t1", "agent": "worker", "name": "tui-child",
                                                       "parent_name": "alpha", "parent_manager_name": "alpha", "closed_at": 1.0})
    r3 = asyncio.run(wait_for_child_impl("tui-child", timeout_sec=2, _poll_interval=0.05, manager_name="alpha"))
    assert r3["reason"] == "session_ended_without_worker_done"


def test_wait_for_child_ignores_the_previous_rounds_done_event(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path, round_no=2)
    done_dir = paths.done_dir_for("alpha")
    done_dir.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(done_dir / "c1-old.json", {
        "claude_sid": "c1", "worker_name": "alpha-child", "event_id": "old", "summary": "round one",
        "parent_manager_name": "alpha", "parent_name": "alpha", "completed_at": time.time() - 1000})
    r = asyncio.run(wait_for_child_impl("alpha-child", timeout_sec=0.3, _poll_interval=0.05, manager_name="alpha"))
    assert r["found"] == "timeout"


def test_wait_for_child_ignores_a_done_event_written_just_before_the_round_started(
        fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path, round_no=2)
    started = time.time()
    assignment = state.read_json(paths.assignment_path("c1"))
    assignment["round_started_at"] = started
    state.write_json_atomic(paths.assignment_path("c1"), assignment)
    done_dir = paths.done_dir_for("alpha")
    done_dir.mkdir(parents=True, exist_ok=True)
    event = {"claude_sid": "c1", "worker_name": "alpha-child", "event_id": "round-one",
             "summary": "ROUND ONE SUMMARY", "parent_manager_name": "alpha", "parent_name": "alpha",
             "completed_at": started - 1.0}
    state.write_json_atomic(done_dir / "c1-round-one.json", event)
    r = asyncio.run(wait_for_child_impl("alpha-child", timeout_sec=0.3, _poll_interval=0.05, manager_name="alpha"))
    assert r["found"] == "timeout"
    state.write_json_atomic(done_dir / "c1-round-two.json",
                            {**event, "event_id": "round-two", "summary": "ROUND TWO SUMMARY",
                             "completed_at": started + 0.1})
    r2 = asyncio.run(wait_for_child_impl("alpha-child", timeout_sec=0.3, _poll_interval=0.05, manager_name="alpha"))
    assert r2["found"] == "done" and r2["summary"] == "ROUND TWO SUMMARY"


def test_list_children_rows_carry_provider_mode_report_and_round(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    report = _headless_child(monkeypatch, tmp_path)
    rows = {r["name"]: r for r in list_workers_impl(manager_name="alpha")}
    row = rows["alpha-child"]
    assert (row["provider"], row["mode"], row["report_path"], row["round"]) == ("codex-exec", "report", str(report), 1)
    assert {r["name"]: r["provider"] for r in list_workers_impl(manager_name="boss")} == {"alpha": "claude-tui"}


def test_ask_parent_wait_is_capped_per_provider(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path)
    cfg = tmp_path / "dw.toml"
    cfg.write_text('[subworkers.providers.codex-exec]\nask_parent_wait_sec = 1\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    started = time.monotonic()
    answer = asyncio.run(ask_manager_impl("c1", "q?", poll_interval=0.05, tool_name="ask_parent", sid_param="child_sid"))
    assert answer.startswith("NO_ANSWER_YET") and time.monotonic() - started < 3
    started = time.monotonic()
    answer = asyncio.run(ask_manager_impl("w1", "q?", poll_interval=0.05, timeout_sec=0.5))
    assert answer.startswith("NO_ANSWER_YET") and time.monotonic() - started >= 0.4


def test_send_to_child_refuses_a_headless_child_mid_round(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="mid-round"):
        asyncio.run(send_to_child_impl("alpha-child", "more"))


def test_send_to_child_says_shutting_down_once_the_round_report_has_content(
        fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    report = _headless_child(monkeypatch, tmp_path)
    report.write_text("REVIEW: fine")
    with pytest.raises(ValueError, match="shutting down"):
        asyncio.run(send_to_child_impl("alpha-child", "more"))


def test_send_to_child_refuses_a_round_with_no_assignment_record(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path, active=False)
    monkeypatch.setattr(mcp_server, "_has_live_transcript", lambda sid, runtime=None: True)
    paths.assignment_path("c1").unlink()
    with pytest.raises(ValueError, match="no assignment record"):
        asyncio.run(send_to_child_impl("alpha-child", "more"))


def test_send_to_child_rolls_the_assignment_back_when_the_round_spawn_raises(
        fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path, active=False)
    monkeypatch.setattr(mcp_server, "_has_live_transcript", lambda sid, runtime=None: True)
    before = state.read_json(paths.assignment_path("c1"))

    async def boom(**kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(spawner, "spawn_worker_tab", boom)
    with pytest.raises(ValueError, match="boom"):
        asyncio.run(send_to_child_impl("alpha-child", "more"))
    assert state.read_json(paths.assignment_path("c1")) == before
    assert not (tmp_path / "rounds" / "r2.brief.md").exists()
    assert not mcp_server._RESUMES_IN_FLIGHT


def test_send_to_child_round_refuses_extra_args_that_move_the_codex_working_root(
        fresh_orchestrator_dir, monkeypatch, tmp_path):
    from tests.conftest import allow_codex
    allow_codex(monkeypatch, tmp_path, "/tmp")
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path, active=False)
    monkeypatch.setattr(mcp_server, "_has_live_transcript", lambda sid, runtime=None: True)
    assignment = state.read_json(paths.assignment_path("c1"))
    assignment["spawn_extra_args"] = ["-C", "/elsewhere"]
    state.write_json_atomic(paths.assignment_path("c1"), assignment)
    with pytest.raises(ValueError, match="cannot override"):
        asyncio.run(send_to_child_impl("alpha-child", "more"))
    assert state.read_json(paths.assignment_path("c1"))["round"] == 1
    assert not (tmp_path / "rounds" / "r2.brief.md").exists()


def test_send_to_child_opens_the_next_round_for_a_closed_headless_child(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path, active=False)
    captured = _patch_spawn_that_registers(monkeypatch, "c1")
    monkeypatch.setattr(mcp_server, "_has_live_transcript", lambda sid, runtime=None: True)
    result = asyncio.run(send_to_child_impl("alpha-child", "add a fourth point"))
    assert result["ok"] is True and result["round"] == 2
    rnd = captured["child_round"]
    assert rnd.resume_sid == "c1" and rnd.brief_path.read_text() == "add a fourth point"
    assert rnd.report_path == tmp_path / "rounds" / "r2.report.md" and rnd.extra_args == ("--x",)
    assert captured["provider"].name == "codex-exec" and captured["tab_title"] == "alpha/alpha-child r2"
    assert captured["env"]["CODEX_HOME"] == "/h" and captured["env"]["CLAUDE_WORKER_KIND"] == "subworker"
    assert captured["env"]["CLAUDE_PARENT_MANAGER"] == "alpha" and captured["env"]["CLAUDE_PARENT_SID"] == "w1"
    assert captured["extra_args"] is None
    assignment = state.read_json(paths.assignment_path("c1"))
    assert assignment["round"] == 2 and assignment["report_path"] == str(rnd.report_path)
    assert assignment["round_started_at"] > time.time() - 10
    assert not (paths.CLOSED / "c1.json").exists()


def test_send_to_child_round_counts_a_report_written_before_registration_as_success(
        fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path, active=False)
    monkeypatch.setattr(mcp_server, "_has_live_transcript", lambda sid, runtime=None: True)
    closed_before = state.read_json(paths.CLOSED / "c1.json")

    async def fast_spawn(**kwargs):
        kwargs["child_round"].report_path.write_text("fast report")
        state.write_json_atomic(paths.CLOSED / "c1.json",
                                {**closed_before, "closed_at": time.time() + 1})
        return ("999", kwargs["name"])

    monkeypatch.setattr(spawner, "spawn_worker_tab", fast_spawn)
    result = asyncio.run(send_to_child_impl("alpha-child", "go",
                                            _registration_timeout_sec=1.0, _poll_interval=0.05))
    assert result["ok"] is True and result["round"] == 2 and result.get("completed_early") is True
    assert (paths.CLOSED / "c1.json").exists()
    assert not (paths.ACTIVE / "c1.json").exists()
    done = asyncio.run(wait_for_child_impl("alpha-child", timeout_sec=2, _poll_interval=0.05, manager_name="alpha"))
    assert done["found"] == "done" and done["summary"] == "fast report" and done["round"] == 2


def test_send_to_child_round_ignores_an_empty_report_written_before_registration(
        fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path, active=False)
    monkeypatch.setattr(mcp_server, "_has_live_transcript", lambda sid, runtime=None: True)

    async def empty_report_spawn(**kwargs):
        kwargs["child_round"].report_path.write_text("")
        return ("999", kwargs["name"])

    monkeypatch.setattr(spawner, "spawn_worker_tab", empty_report_spawn)
    result = asyncio.run(send_to_child_impl("alpha-child", "go",
                                            _registration_timeout_sec=0.3, _poll_interval=0.05))
    assert result["ok"] is False and "completed_early" not in result


def test_send_to_child_reports_a_headless_child_without_a_live_transcript(
        fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path, active=False)
    monkeypatch.setattr(mcp_server, "_has_live_transcript", lambda sid, runtime=None: False)
    with pytest.raises(ValueError, match="live transcript"):
        asyncio.run(send_to_child_impl("alpha-child", "go"))


def test_send_to_child_round_is_guarded_by_the_resume_in_flight_set(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path, active=False)
    monkeypatch.setattr(mcp_server, "_has_live_transcript", lambda sid, runtime=None: True)
    mcp_server._RESUMES_IN_FLIGHT.add("alpha-child")
    try:
        with pytest.raises(ValueError, match="already in progress"):
            asyncio.run(send_to_child_impl("alpha-child", "again"))
    finally:
        mcp_server._RESUMES_IN_FLIGHT.discard("alpha-child")


def test_send_to_child_keeps_the_tui_paths(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    with pytest.raises(ValueError, match="no worker named 'ghost'"):
        asyncio.run(send_to_child_impl("ghost", "x"))
    state.write_json_atomic(paths.CLOSED / "t1.json", {"claude_sid": "t1", "agent": "worker", "name": "tui-child",
                                                       "runtime": "claude", "cwd": "/x", "closed_at": 1.0})
    with pytest.raises(ValueError, match="no worker named 'tui-child'"):
        asyncio.run(send_to_child_impl("tui-child", "x"))
    with pytest.raises(ValueError, match="no worker named 'tui-child'"):
        asyncio.run(send_manager_to_worker("tui-child", "x"))


def test_resume_worker_refuses_a_headless_child(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path, active=False)
    monkeypatch.setattr(mcp_server, "_has_live_transcript", lambda sid, runtime=None: True)
    with pytest.raises(ValueError, match="use send_to_child"):
        asyncio.run(resume_worker_impl("alpha-child"))
    assert (paths.CLOSED / "c1.json").exists()


def _pane_state(monkeypatch, value="dead"):
    from dockwright import terminal
    monkeypatch.setattr(terminal.TmuxDriver, "pane_state", lambda self, pane: value)


def test_kill_child_closes_the_dead_window_of_a_closed_headless_child(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path, active=False)
    _pane_state(monkeypatch)
    closed = []
    monkeypatch.setattr(mcp_server, "_close_window", lambda wid: closed.append(wid))
    assert kill_child("alpha-child") == {"closed_window": "%7", "already_dead": True}
    assert closed == ["%7"]
    with pytest.raises(ValueError, match="no worker named 'ghost'"):
        kill_child("ghost")


def test_kill_child_picks_the_newest_closed_record_of_that_name(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path, sid="old", active=False)
    stale = state.read_json(paths.CLOSED / "old.json")
    state.write_json_atomic(paths.CLOSED / "old.json", {**stale, "window_id": "%7", "closed_at": 1.0})
    _headless_child(monkeypatch, tmp_path, sid="new", active=False)
    fresh = state.read_json(paths.CLOSED / "new.json")
    state.write_json_atomic(paths.CLOSED / "new.json", {**fresh, "window_id": "%9", "closed_at": 9000.0})
    _pane_state(monkeypatch)
    real_list_json_in = state.list_json_in

    def stale_first(directory):
        records = list(real_list_json_in(directory))
        if directory == paths.CLOSED:
            records.sort(key=lambda r: r.get("closed_at") or 0)
        return records

    monkeypatch.setattr(mcp_server.state, "list_json_in", stale_first)
    closed = []
    monkeypatch.setattr(mcp_server, "_close_window", lambda wid: closed.append(wid))
    assert kill_child("alpha-child")["closed_window"] == "%9"
    assert closed == ["%9"]


def test_kill_child_leaves_a_live_pane_alone(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path, active=False)
    _pane_state(monkeypatch, "alive")
    closed = []
    monkeypatch.setattr(mcp_server, "_close_window", lambda wid: closed.append(wid))
    result = kill_child("alpha-child")
    assert result["closed_window"] is None and result["already_dead"] is True
    assert result["note"] == "the recorded window is live and not this child's — not closed"
    assert closed == []


def test_kill_child_says_nothing_to_close_when_the_pane_is_gone(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path, active=False)
    _pane_state(monkeypatch, "gone")
    closed = []
    monkeypatch.setattr(mcp_server, "_close_window", lambda wid: closed.append(wid))
    result = kill_child("alpha-child")
    assert result["closed_window"] is None and result["already_dead"] is True
    assert result["note"] == "the recorded window no longer exists — nothing to close"
    assert closed == []


def test_tmux_pane_state_reads_gone_dead_and_alive(monkeypatch):
    from dockwright import terminal
    driver = terminal.TmuxDriver()
    outputs = {"%gone": (0, b""), "%dead": (0, b"1\n"), "%alive": (0, b"0\n"), "%err": (1, b"1\n")}

    def fake_run(args, **kwargs):
        pane = args[args.index("-t") + 1]
        if pane == "%slow":
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))
        code, out = outputs[pane]
        return subprocess.CompletedProcess(args, returncode=code, stdout=out, stderr=b"")

    monkeypatch.setattr(terminal.subprocess, "run", fake_run)
    assert driver.pane_state("%gone") == "gone"
    assert driver.pane_state("%dead") == "dead"
    assert driver.pane_state("%alive") == "alive"
    assert driver.pane_state("%err") == "gone"
    assert driver.pane_state("%slow") == "unknown"
    assert driver.pane_state("") == "gone"


def test_kill_child_does_not_close_when_the_pane_state_cannot_be_read(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path, active=False)
    _pane_state(monkeypatch, "unknown")
    closed = []
    monkeypatch.setattr(mcp_server, "_close_window", lambda wid: closed.append(wid))
    result = kill_child("alpha-child")
    assert result["closed_window"] is None and result["already_dead"] is True
    assert result["note"] == "the pane state could not be read — not closed"
    assert closed == []


def test_has_live_child_reads_a_live_subworker_by_parent_name(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path)
    worker = state.read_json(paths.ACTIVE / "w1.json")
    assert state.has_live_child(worker) is True
    assert state.has_live_child({"name": "nobody"}) is False
    child = state.read_json(paths.ACTIVE / "c1.json")
    child["pid"] = 2 ** 22 + 9
    state.write_json_atomic(paths.ACTIVE / "c1.json", child)
    assert state.has_live_child(worker) is False


def test_live_children_of_counts_a_child_with_no_pid(fresh_orchestrator_dir):
    state.write_json_atomic(paths.ACTIVE / "c1.json", {
        "claude_sid": "c1", "agent": "worker", "kind": "subworker",
        "name": "alpha-review", "parent_name": "alpha"})
    assert [c["name"] for c in state.live_children_of("alpha")] == ["alpha-review"]
    state.write_json_atomic(paths.ACTIVE / "c2.json", {
        "claude_sid": "c2", "agent": "worker", "kind": "subworker",
        "name": "bravo-review", "parent_name": "bravo", "pid": 2 ** 22 + 13})
    assert state.live_children_of("bravo") == []


CODEX_TURN = ('{"type":"turn.completed","usage":{"input_tokens":212735,"cached_input_tokens":153088,'
              '"cache_write_input_tokens":0,"output_tokens":255,"reasoning_output_tokens":101}}')


def test_wait_for_child_writes_the_round_spend_when_the_report_lands(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    report = _headless_child(monkeypatch, tmp_path, active=False)
    (tmp_path / "rounds" / "r1.log").write_text(CODEX_TURN + "\n")
    report.write_text("REVIEW: fine")
    r = asyncio.run(wait_for_child_impl("alpha-child", timeout_sec=2, _poll_interval=0.05, manager_name="alpha"))
    assert r["found"] == "done"
    closed = state.read_json(paths.CLOSED / "c1.json")
    assert closed["spend"]["out_tokens"] == 255 and closed["spend"]["turns"] == 1


def test_wait_for_child_writes_the_round_spend_into_a_still_active_record(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    report = _headless_child(monkeypatch, tmp_path, active=True)
    (tmp_path / "rounds" / "r1.log").write_text(CODEX_TURN + "\n")
    report.write_text("REVIEW: fine")
    asyncio.run(wait_for_child_impl("alpha-child", timeout_sec=2, _poll_interval=0.05, manager_name="alpha"))
    assert state.read_json(paths.ACTIVE / "c1.json")["spend"]["out_tokens"] == 255


def test_wait_for_child_leaves_a_claude_exec_childs_spend_alone(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    report = _headless_child(monkeypatch, tmp_path, active=False, provider="claude-exec")
    closed = state.read_json(paths.CLOSED / "c1.json")
    closed["runtime"] = "claude"
    closed["spend"] = {"turns": 3, "out_tokens": 7}
    state.write_json_atomic(paths.CLOSED / "c1.json", closed)
    (tmp_path / "rounds" / "r1.log").write_text(CODEX_TURN + "\n")
    report.write_text("REVIEW: fine")
    asyncio.run(wait_for_child_impl("alpha-child", timeout_sec=2, _poll_interval=0.05, manager_name="alpha"))
    assert state.read_json(paths.CLOSED / "c1.json")["spend"] == {"turns": 3, "out_tokens": 7}


def test_send_to_child_refuses_a_round_when_the_parent_worker_is_gone(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path, active=False)
    (paths.ACTIVE / "w1.json").unlink()
    monkeypatch.setattr(mcp_server, "_has_live_transcript", lambda sid, runtime=None: True)
    with pytest.raises(ValueError, match="child of worker 'alpha', which is not live"):
        asyncio.run(send_to_child_impl("alpha-child", "again"))
    assert (paths.CLOSED / "c1.json").exists()
    assert state.read_json(paths.assignment_path("c1"))["round"] == 1


def test_send_to_child_refuses_when_the_parent_record_has_a_dead_pid(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path, active=False)
    parent = state.read_json(paths.ACTIVE / "w1.json")
    parent["pid"] = 2 ** 22 + 3
    state.write_json_atomic(paths.ACTIVE / "w1.json", parent)
    monkeypatch.setattr(mcp_server, "_has_live_transcript", lambda sid, runtime=None: True)
    with pytest.raises(ValueError, match="not live"):
        asyncio.run(send_to_child_impl("alpha-child", "again"))


def test_resume_worker_refuses_an_orphaned_tui_child(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    state.write_json_atomic(paths.CLOSED / "t1.json", {
        "claude_sid": "t1", "name": "alpha-tui", "kind": "subworker", "parent_name": "alpha",
        "runtime": "claude", "provider": "claude-tui", "cwd": "/tmp/x", "closed_at": 1.0})
    (paths.ACTIVE / "w1.json").unlink()
    monkeypatch.setattr(mcp_server, "_has_live_transcript", lambda sid, runtime=None: True)
    with pytest.raises(ValueError, match="child of worker 'alpha', which is not live"):
        asyncio.run(resume_worker_impl("alpha-tui"))


def test_resume_worker_still_resumes_a_worker_whose_manager_is_gone(fresh_orchestrator_dir, monkeypatch, tmp_path):
    state.write_json_atomic(paths.CLOSED / "w9.json", {
        "claude_sid": "w9", "name": "loner", "kind": "worker", "parent_name": "gone-boss",
        "runtime": "claude", "cwd": "/tmp/x", "closed_at": 1.0})
    captured = _patch_spawn_that_registers(monkeypatch, "w9")
    monkeypatch.setattr(mcp_server, "_has_live_transcript", lambda sid, runtime=None: True)
    result = asyncio.run(resume_worker_impl("loner", model="@opus", effort="xhigh"))
    assert result["ok"] is True and captured


@pytest.mark.parametrize("pid", [None, "x", 0, -1, True])
def test_send_to_child_reads_a_parent_with_an_unknown_pid_as_live(fresh_orchestrator_dir, monkeypatch, tmp_path, pid):
    _manager_and_worker()
    _headless_child(monkeypatch, tmp_path, active=False)
    parent = state.read_json(paths.ACTIVE / "w1.json")
    if pid is None:
        parent.pop("pid", None)
    else:
        parent["pid"] = pid
    state.write_json_atomic(paths.ACTIVE / "w1.json", parent)
    _patch_spawn_that_registers(monkeypatch, "c1")
    monkeypatch.setattr(mcp_server, "_has_live_transcript", lambda sid, runtime=None: True)
    result = asyncio.run(send_to_child_impl("alpha-child", "again"))
    assert result["ok"] is True and result["round"] == 2


def test_live_parent_sid_reads_a_parent_with_an_unknown_pid_as_live(fresh_orchestrator_dir):
    _manager_and_worker()
    parent = state.read_json(paths.ACTIVE / "w1.json")
    parent.pop("pid", None)
    state.write_json_atomic(paths.ACTIVE / "w1.json", parent)
    assert mcp_server._live_parent_sid("alpha") == "w1"
    assert mcp_server._live_parent_record("alpha")["claude_sid"] == "w1"


def test_live_parent_sid_and_record_agree_that_a_dead_pid_is_not_live(fresh_orchestrator_dir):
    _manager_and_worker()
    parent = state.read_json(paths.ACTIVE / "w1.json")
    parent["pid"] = 2 ** 22 + 3
    state.write_json_atomic(paths.ACTIVE / "w1.json", parent)
    assert mcp_server._live_parent_sid("alpha") is None
    assert mcp_server._live_parent_record("alpha") is None


def test_wait_for_child_does_not_resurrect_an_active_record_unlinked_mid_recount(
        fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager_and_worker()
    report = _headless_child(monkeypatch, tmp_path, active=True)
    (tmp_path / "rounds" / "r1.log").write_text(CODEX_TURN + "\n")
    report.write_text("REVIEW: fine")
    from dockwright import transcript as tr
    active_path, closed_path = paths.ACTIVE / "c1.json", paths.CLOSED / "c1.json"
    real_recount = tr.recount_codex_spend
    fired = []

    def vanish_mid_recount(log_path, prior):
        if not fired:
            fired.append(True)
            record = state.read_json(active_path)
            active_path.unlink(missing_ok=True)
            state.write_json_atomic(closed_path,
                                    {**record, "closed_at": 1.0, "closed_reason": "session_end"})
        return real_recount(log_path, prior)

    monkeypatch.setattr(tr, "recount_codex_spend", vanish_mid_recount)
    asyncio.run(wait_for_child_impl("alpha-child", timeout_sec=2, _poll_interval=0.05, manager_name="alpha"))
    assert fired and not active_path.exists()
    assert state.read_json(closed_path)["spend"]["out_tokens"] == 255


REPO_PRESETS = Path(__file__).resolve().parent.parent / "deploy" / "presets"
CHILD = dict(provider="claude-tui", mode="report", model="claude-opus-5", effort="xhigh")
CODEX = dict(provider="codex-exec", mode="report", model="gpt-5.5")


def _launched_model(captured: dict) -> str:
    command = spawner._runtime_command(captured["runtime"], captured["initial_prompt"],
                                       captured["extra_args"], captured.get("resume_sid"))
    tokens = shlex.split(command)
    return tokens[tokens.index("--model") + 1]


def _work_dir(monkeypatch, tmp_path, allowed=None):
    from tests.conftest import allow_codex
    home = tmp_path / "home"
    work = home / "work" / "repo"
    work.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    allow_codex(monkeypatch, tmp_path, str(home / "work") if allowed is None else allowed)
    monkeypatch.setattr(paths, "PRESETS", REPO_PRESETS)
    return work


def _install_role(tmp_path, monkeypatch, name="GenericWorker", launch=("worker",)):
    roles_dir = tmp_path / "roles"
    role_dir = roles_dir / name
    role_dir.mkdir(parents=True, exist_ok=True)
    launches = ", ".join(f'"{kind}"' for kind in launch)
    (role_dir / "role.toml").write_text(f'core = "{name}"\nlaunch = [{launches}]\n')
    for kind in launch:
        (role_dir / f"role.{kind}.md").write_text(f"<!-- dockwright-role: {name} launch={kind} -->\nBODY\n")
    (role_dir / "role.settings.json").write_text(json.dumps({"skillOverrides": {"skill-x": "name-only"}}))
    monkeypatch.setattr(paths, "ROLES_DIR", roles_dir)
    return role_dir


def _settings_of(args):
    assert args.count("--settings") == 1, args
    return json.loads(Path(args[args.index("--settings") + 1]).read_text())


def _codex_render(role_dir, text):
    (role_dir / "role.codex.md").write_text(text)


def test_a_claude_tui_child_runs_as_a_full_session_with_the_verifier_preset(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    gates = []
    real_gate = spawner.usage_spawn_gate
    monkeypatch.setattr(spawner, "usage_spawn_gate", lambda force=False: gates.append(1) or real_gate(force=force))
    result = asyncio.run(spawn_child(parent_sid="w1", brief="review", cwd=str(work), **CHILD))
    assert captured["provider"] is None and captured["child_round"] is None and captured["runtime"] == "claude"
    assert captured["extra_args"] == ["--model", "claude-opus-5", "--effort", "xhigh", "--settings",
                                      str(REPO_PRESETS / "verifier-settings.json")]
    assert captured["env"]["CLAUDE_WORKER_KIND"] == "subworker"
    assert result["mode"] == "report" and result["kind"] == "subworker" and "execution" not in result
    assert "role" not in result and result["name"] == "alpha-child"
    pending = list(state.list_json_in(paths.ASSIGNMENTS_PENDING))[0]
    assert pending["spawn_extra_args"] == captured["extra_args"] and "role" not in pending
    assert gates == [1]


def test_an_interactive_claude_tui_child_is_a_plain_worker_on_its_model(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    result = asyncio.run(spawn_child(parent_sid="w1", brief="implement", cwd=str(work), provider="claude-tui",
                                     mode="interactive", model="claude-sonnet-5", effort="xhigh"))
    assert captured["extra_args"] == [*mcp_server._claude_worker_settings_args(None), "--model", "claude-sonnet-5",
                                      "--effort", "xhigh"]
    assert "verifier-settings.json" not in " ".join(captured["extra_args"])
    assert result["mode"] == "interactive"


def test_a_read_only_claude_tui_child_refuses_without_its_preset(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(paths, "PRESETS", tmp_path / "no-such-presets")
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    snapshots = []
    monkeypatch.setattr(spawner, "write_registry_snapshot", lambda: snapshots.append(1))
    with pytest.raises(ValueError, match="no-such-presets/verifier-settings.json missing; refusing to spawn a read-only child"):
        asyncio.run(spawn_child(parent_sid="w1", brief="review", cwd=str(work), **CHILD))
    assert captured == {} and snapshots == [] and list(paths.ASSIGNMENTS_PENDING.glob("*")) == []


def test_a_managers_claude_tui_child_registers_as_a_worker_and_loads_the_child_render(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    role_dir = _install_role(tmp_path, monkeypatch, name="GenericWorker", launch=("worker", "child"))
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    result = asyncio.run(spawn_child(parent_sid="mgr-a", brief="review", cwd=str(work), **CHILD))
    assert captured["extra_args"][:2] == ["--append-system-prompt-file", str(role_dir / "role.child.md")]
    assert result["kind"] == "worker" and result["mode"] == "report"


def test_a_claude_tui_child_resolves_its_model_token(fresh_orchestrator_dir, monkeypatch, tmp_path):
    from dockwright import models
    work = _work_dir(monkeypatch, tmp_path)
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    asyncio.run(spawn_child(parent_sid="w1", brief="review", cwd=str(work), **{**CHILD, "model": "@opus[1m]"}))
    assert _launched_model(captured) == models.families()["opus"] + "[1m]"


def test_a_resumed_claude_tui_child_launches_on_a_pin_moved_after_its_spawn(
        fresh_orchestrator_dir, monkeypatch, tmp_path, model_pins):
    work = _work_dir(monkeypatch, tmp_path)
    _manager_and_worker()
    spawned = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    asyncio.run(spawn_child(parent_sid="w1", brief="review", cwd=str(work), **{**CHILD, "model": "@opus[1m]"}))
    assert _launched_model(spawned) == "claude-opus-7-1[1m]"
    pending = state.read_json(list(paths.ASSIGNMENTS_PENDING.glob("*.json"))[0])
    state.write_json_atomic(paths.assignment_path("sub1"), {**pending, "claude_sid": "sub1"})
    state.write_json_atomic(paths.CLOSED / "sub1.json", {
        "claude_sid": "sub1", "name": spawned["name"], "cwd": str(work), "closed_at": 1.0,
        "runtime": "claude", "parent_name": "alpha", "parent_sid": "w1", "kind": "subworker", "depth": 2,
    })
    model_pins('opus = "claude-opus-7-1"', 'opus = "claude-opus-9-9"')
    monkeypatch.setattr(mcp_server, "_has_live_transcript", lambda sid, runtime=None: True)
    resumed = _patch_spawn_that_registers(monkeypatch, "sub1")
    assert asyncio.run(resume_worker_impl(spawned["name"], _registration_timeout_sec=1.0,
                                          _poll_interval=0.01))["ok"] is True
    assert resumed["resume_sid"] == "sub1"
    assert _launched_model(resumed) == "claude-opus-9-9[1m]"


def test_a_claude_tui_child_naming_an_unknown_family_is_refused_at_spawn(fresh_orchestrator_dir, monkeypatch, tmp_path):
    from dockwright import models
    work = _work_dir(monkeypatch, tmp_path)
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    with pytest.raises(models.UnknownFamily, match="nosuch"):
        asyncio.run(spawn_child(parent_sid="w1", brief="review", cwd=str(work), **{**CHILD, "model": "@nosuch"}))
    assert captured == {} and list(paths.ASSIGNMENTS_PENDING.glob("*")) == []


def test_resume_replays_a_claude_tui_childs_spawn_args(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    asyncio.run(spawn_child(parent_sid="w1", brief="review", cwd=str(work), **CHILD))
    pending_path = list(paths.ASSIGNMENTS_PENDING.glob("*.json"))[0]
    paths.ASSIGNMENTS.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.ASSIGNMENTS / "sub1.json", {**state.read_json(pending_path), "claude_sid": "sub1"})
    assert mcp_server._resume_settings_args("sub1") == captured["extra_args"]


def test_a_report_child_gets_no_worker_footers_and_an_interactive_one_keeps_them(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    asyncio.run(spawn_child(parent_sid="w1", brief="Review the diff.", cwd=str(work), task_key="T-1", **CHILD))
    assert captured["initial_prompt"] == "Review the diff."
    assert "[orchestrator]" not in captured["initial_prompt"]
    asyncio.run(spawn_child(parent_sid="w1", brief="Implement it.", cwd=str(work), task_key="T-1",
                            **{**CHILD, "mode": "interactive"}))
    assert captured["initial_prompt"].startswith("Implement it.")
    assert "git fetch origin main" in captured["initial_prompt"]
    assert "Artifact discipline" in captured["initial_prompt"]


def test_a_claude_tui_child_loads_its_role_and_effort_over_the_verifier_preset(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    role_dir = _install_role(tmp_path, monkeypatch, name="CodeReviewer", launch=("child",))
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    result = asyncio.run(spawn_child(parent_sid="w1", brief="review", cwd=str(work), context="CodeReviewer",
                                     **{**CHILD, "effort": "high"}))
    args = captured["extra_args"]
    assert args[:2] == ["--append-system-prompt-file", str(role_dir / "role.child.md")]
    assert args.count("--effort") == 1 and args[args.index("--effort") + 1] == "high"
    settings = _settings_of(args)
    preset = json.loads((REPO_PRESETS / "verifier-settings.json").read_text())
    assert settings["permissions"] == preset["permissions"]
    assert settings["skillOverrides"] == {"skill-x": "name-only"}
    assert result["context_role"] == "CodeReviewer" and result["name"] == "alpha-codereviewer-r1"
    pending = list(state.list_json_in(paths.ASSIGNMENTS_PENDING))[0]
    assert pending["context_role"] == "CodeReviewer" and "role" not in pending
    assert pending["spawn_extra_args"] == args


def _profiles_for(monkeypatch, work):
    from dockwright import config as _config
    settings = {"sets": {"none": [], "back": ["acme", "acme-be"]}, "repos": {work.name: "back"},
                "work_roots": [str(work.parent)], "work_default": "back"}
    monkeypatch.setattr(_config, "profiles", lambda: settings)
    (paths.ROLES_DIR / "_agents").mkdir(parents=True, exist_ok=True)
    (paths.ROLES_DIR / "_agents" / "back.json").write_text("{}")


def test_a_subworker_without_cwd_takes_its_parents_cwd_and_so_its_profile(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    role_dir = _install_role(tmp_path, monkeypatch, name="CodeReviewer", launch=("child",))
    (role_dir / "role.child.back.md").write_text(
        "<!-- dockwright-role: CodeReviewer launch=child -->\n<!-- dockwright-profile: back -->\nBODY\n")
    _profiles_for(monkeypatch, work)
    register_self_impl(claude_sid="mgr-a", agent="manager", name="boss", cwd="/x", iterm_sid="", pid=os.getpid())
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd=str(work), iterm_sid="", pid=os.getpid(),
                       parent_manager_name="boss", parent_sid="mgr-a")
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    asyncio.run(spawn_child(parent_sid="w1", brief="review", context="CodeReviewer", **CHILD))
    args = captured["extra_args"]
    assert captured["cwd"] == str(work)
    assert args[:2] == ["--append-system-prompt-file", str(role_dir / "role.child.back.md")]
    assert args[-2:] == ["--agents-file", str(paths.ROLES_DIR / "_agents" / "back.json")]


def test_a_codex_exec_child_carries_its_profiles_codex_render(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    role_dir = _install_role(tmp_path, monkeypatch, name="CodeReviewer", launch=("child",))
    _codex_render(role_dir, "CODEREVIEWER CODEX\n")
    (role_dir / "role.codex.back.md").write_text("CODEREVIEWER CODEX BACK\n")
    _profiles_for(monkeypatch, work)
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    asyncio.run(spawn_child(parent_sid="w1", brief="review this", cwd=str(work), provider="codex-exec", mode="report",
                            model="gpt-x", context="CodeReviewer"))
    brief = captured["child_round"].brief_path.read_text()
    assert "CODEREVIEWER CODEX BACK" in brief and "CODEREVIEWER CODEX\n" not in brief


def test_a_context_childs_counter_advances_past_closed_records(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    _install_role(tmp_path, monkeypatch, name="CodeReviewer", launch=("child",))
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    state.write_json_atomic(paths.CLOSED / "old.json", {"claude_sid": "old", "name": "alpha-codereviewer-r1", "closed_at": 1.0})
    state.write_json_atomic(paths.CLOSED / "odd.json", {"claude_sid": "odd", "name": "alpha-codereviewer-r1-2", "closed_at": 1.0})
    asyncio.run(spawn_child(parent_sid="w1", brief="review", cwd=str(work), context="CodeReviewer", **CHILD))
    assert captured["name"] == "alpha-codereviewer-r2"
    asyncio.run(spawn_child(parent_sid="w1", brief="review", cwd=str(work), context="CodeReviewer", **CHILD))
    assert captured["name"] == "alpha-codereviewer-r3"


def test_a_roleless_claude_tui_child_loads_the_generic_worker_child_render(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    role_dir = _install_role(tmp_path, monkeypatch, name="GenericWorker", launch=("worker", "child"))
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    asyncio.run(spawn_child(parent_sid="w1", brief="x", cwd=str(work), **CHILD))
    assert captured["extra_args"][:2] == ["--append-system-prompt-file", str(role_dir / "role.child.md")]


def test_a_subworker_names_its_role_with_context(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    _install_role(tmp_path, monkeypatch, name="GenericWorker", launch=("worker", "child"))
    coder = _install_role(tmp_path, monkeypatch, name="Coder", launch=("child",))
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    asyncio.run(spawn_child(parent_sid="w1", brief="x", cwd=str(work), context="Coder", **CHILD))
    assert captured["extra_args"][:2] == ["--append-system-prompt-file", str(coder / "role.child.md")]


def test_a_headless_claude_child_loads_its_context_role(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    _install_role(tmp_path, monkeypatch, name="CodeReviewer", launch=("child",))
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    result = asyncio.run(spawn_child(parent_sid="w1", brief="x", cwd=str(work), provider="claude-exec", mode="report",
                                     model="claude-opus-5", effort="high", context="CodeReviewer"))
    assert result["context_role"] == "CodeReviewer"
    assert captured["child_round"].context_role == "CodeReviewer"
    pending = list(state.list_json_in(paths.ASSIGNMENTS_PENDING))[0]
    assert pending["child_context_role"] == "CodeReviewer"
    for path in paths.ASSIGNMENTS_PENDING.glob("*"):
        path.unlink()
    captured.clear()
    _install_role(tmp_path, monkeypatch, name="GenericWorker", launch=("child",))
    result = asyncio.run(spawn_child(parent_sid="w1", brief="x", cwd=str(work), provider="claude-exec", mode="report",
                                     model="claude-opus-5", effort="high"))
    assert result["context_role"] == "GenericWorker" and "warnings" not in result
    assert captured["child_round"].context_role == "GenericWorker"
    assert captured["child_round"].effort == "high"
    pending = list(state.list_json_in(paths.ASSIGNMENTS_PENDING))[0]
    assert pending["child_context_role"] == "GenericWorker"


def test_a_role_settings_file_outside_the_profile_keys_is_refused_before_any_side_effect(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    role_dir = _install_role(tmp_path, monkeypatch, name="CodeReviewer", launch=("child",))
    (role_dir / "role.settings.json").write_text(json.dumps({"bogus": 1}))
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    snapshots = []
    monkeypatch.setattr(spawner, "write_registry_snapshot", lambda: snapshots.append(1))
    with pytest.raises(ValueError, match="unknown profile key 'bogus'"):
        asyncio.run(spawn_child(parent_sid="w1", brief="x", cwd=str(work), context="CodeReviewer", **CHILD))
    (role_dir / "role.settings.json").write_text("{not json")
    with pytest.raises(ValueError, match="role.settings.json"):
        asyncio.run(spawn_child(parent_sid="w1", brief="x", cwd=str(work), context="CodeReviewer", **CHILD))
    assert captured == {} and snapshots == [] and list(paths.ASSIGNMENTS_PENDING.glob("*")) == []


def test_a_subworker_whose_role_lacks_the_child_launch_is_refused(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    _install_role(tmp_path, monkeypatch, name="CodeReviewer", launch=("worker",))
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    with pytest.raises(ValueError, match="does not declare launch 'child'"):
        asyncio.run(spawn_child(parent_sid="w1", brief="x", cwd=str(work), context="CodeReviewer", **CHILD))
    assert captured == {} and list(paths.ASSIGNMENTS_PENDING.glob("*")) == []


def test_a_codex_exec_child_carries_its_context_role_between_bootstrap_and_brief(fresh_orchestrator_dir, monkeypatch, tmp_path):
    from dockwright.providers import get_provider
    work = _work_dir(monkeypatch, tmp_path)
    role_dir = _install_role(tmp_path, monkeypatch, name="CodeReviewer", launch=("child",))
    _codex_render(role_dir, "CODEREVIEWER CODEX TEXT\n")
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    result = asyncio.run(spawn_child(parent_sid="w1", brief="review this", cwd=str(work), context="CodeReviewer",
                                     **CODEX))
    brief = captured["child_round"].brief_path.read_text()
    bootstrap = get_provider("codex-exec").bootstrap("report")
    assert brief.index(bootstrap) < brief.index("CODEREVIEWER CODEX TEXT") < brief.index("review this")
    assert result["context_role"] == "CodeReviewer" and captured["child_round"].model == "gpt-5.5"


def test_a_codex_exec_child_without_role_manifests_keeps_todays_brief(fresh_orchestrator_dir, monkeypatch, tmp_path):
    from dockwright.providers import get_provider
    work = _work_dir(monkeypatch, tmp_path)
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    asyncio.run(spawn_child(parent_sid="w1", brief="review this", cwd=str(work), context="CodeReviewer", **CODEX))
    assert captured["child_round"].brief_path.read_text().startswith(
        f"{get_provider('codex-exec').bootstrap('report')}\n\nreview this")


def test_a_codex_exec_child_whose_role_has_no_codex_render_is_refused_before_side_effects(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    _install_role(tmp_path, monkeypatch, name="CodeReviewer", launch=("child",))
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    snapshots = []
    monkeypatch.setattr(spawner, "write_registry_snapshot", lambda: snapshots.append(1))
    with pytest.raises(ValueError, match=r"spawn_child: role CodeReviewer: .*role\.codex\.md missing"):
        asyncio.run(spawn_child(parent_sid="w1", brief="x", cwd=str(work), context="CodeReviewer", **CODEX))
    assert captured == {} and snapshots == [] and list(paths.ASSIGNMENTS_PENDING.glob("*")) == []
    assert not paths.ROOT.joinpath("children").exists() or not any(paths.ROOT.joinpath("children").iterdir())


def test_a_workers_implicit_codex_exec_child_carries_the_generic_worker_codex_render(fresh_orchestrator_dir, monkeypatch, tmp_path):
    from dockwright.providers import get_provider
    work = _work_dir(monkeypatch, tmp_path)
    generic = _install_role(tmp_path, monkeypatch, name="GenericWorker", launch=("worker", "child"))
    _codex_render(generic, "GENERIC CODEX\n")
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    result = asyncio.run(spawn_child(parent_sid="w1", brief="review this", cwd=str(work), mode="report", model="gpt-5.5"))
    assert captured["provider"].name == "codex-exec" and captured["name"] == "alpha-child"
    brief = captured["child_round"].brief_path.read_text()
    bootstrap = get_provider("codex-exec").bootstrap("report")
    assert brief == f"{bootstrap}\n\nGENERIC CODEX\n\nreview this"
    assert result["context_role"] == "GenericWorker" and "role" not in result
    pending = list(state.list_json_in(paths.ASSIGNMENTS_PENDING))[0]
    assert pending["context_role"] == "GenericWorker" and "role" not in pending


def test_a_workers_implicit_codex_exec_child_without_role_manifests_keeps_todays_brief(fresh_orchestrator_dir, monkeypatch, tmp_path):
    from dockwright.providers import get_provider
    work = _work_dir(monkeypatch, tmp_path)
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    result = asyncio.run(spawn_child(parent_sid="w1", brief="review this", cwd=str(work), mode="report", model="gpt-5.5"))
    assert captured["provider"].name == "codex-exec"
    assert captured["child_round"].brief_path.read_text() == (
        f"{get_provider('codex-exec').bootstrap('report')}\n\nreview this")
    assert result["context_role"] is None


def test_a_workers_implicit_codex_exec_child_without_the_generic_codex_render_is_refused_before_side_effects(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    _install_role(tmp_path, monkeypatch, name="GenericWorker", launch=("worker", "child"))
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    snapshots = []
    monkeypatch.setattr(spawner, "write_registry_snapshot", lambda: snapshots.append(1))
    with pytest.raises(ValueError, match=r"spawn_child: role GenericWorker: .*GenericWorker/role\.codex\.md missing"):
        asyncio.run(spawn_child(parent_sid="w1", brief="x", cwd=str(work), mode="report", model="gpt-5.5"))
    assert captured == {} and snapshots == [] and list(paths.ASSIGNMENTS_PENDING.glob("*")) == []
    assert not paths.ROOT.joinpath("children").exists() or not any(paths.ROOT.joinpath("children").iterdir())


def test_a_codex_child_outside_allowed_roots_hits_the_bare_gate(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path, allowed="/nowhere")
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    with pytest.raises(ValueError) as exc:
        asyncio.run(spawn_child(parent_sid="w1", brief="x", cwd=str(work), **CODEX))
    assert "codex is not allowed under" in str(exc.value) and 'provider="claude-tui"' not in str(exc.value)
    assert captured == {} and list(paths.ASSIGNMENTS_PENDING.glob("*")) == []


def test_a_bad_mode_is_refused_before_any_side_effect(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    with pytest.raises(ValueError, match="mode must be one of"):
        asyncio.run(spawn_child(parent_sid="w1", brief="x", cwd=str(work), **{**CHILD, "mode": "nonsense"}))
    with pytest.raises(ValueError, match="mode 'reply' is the topic agent's"):
        asyncio.run(spawn_child(parent_sid="w1", brief="x", cwd=str(work), **{**CHILD, "mode": "reply"}))
    with pytest.raises(ValueError, match="mode must be one of"):
        asyncio.run(spawn_child(parent_sid="w1", brief="x", cwd=str(work), **{**CODEX, "mode": "nonsense"}))
    assert captured == {} and list(paths.ASSIGNMENTS_PENDING.glob("*")) == []


def test_a_codex_worker_carries_its_roles_codex_text(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    generic = _install_role(tmp_path, monkeypatch, name="GenericWorker", launch=("worker", "child"))
    coder = _install_role(tmp_path, monkeypatch, name="Coder", launch=("worker",))
    _codex_render(generic, "GENERIC CODEX\n")
    _codex_render(coder, "CODER CODEX\n")
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    coder = asyncio.run(spawn_worker_impl("task", manager_sid="mgr-a", cwd=str(work), runtime="codex",
                                          context_role="Coder"))
    assert captured["role_text"] == "CODER CODEX\n" and coder["context_role"] == "Coder"
    pending = [r for r in state.list_json_in(paths.ASSIGNMENTS_PENDING) if r["name"] == coder["name"]]
    assert pending[0]["context_role"] == "Coder"
    captured.clear()
    generic = asyncio.run(spawn_worker_impl("task", manager_sid="mgr-a", cwd=str(work), runtime="codex"))
    assert captured["role_text"] == "GENERIC CODEX\n" and generic["context_role"] == "GenericWorker"


def test_a_codex_worker_without_role_manifests_gets_no_role_text(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    result = asyncio.run(spawn_worker_impl("task", manager_sid="mgr-a", cwd=str(work), runtime="codex"))
    assert captured["role_text"] == "" and result["context_role"] is None


def test_a_codex_worker_in_an_unknown_role_is_refused_before_side_effects(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    _install_role(tmp_path, monkeypatch, name="GenericWorker", launch=("worker", "child"))
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    with pytest.raises(ValueError, match="spawn_worker: unknown role 'Nope'"):
        asyncio.run(spawn_worker_impl("task", manager_sid="mgr-a", cwd=str(work), runtime="codex",
                                      context_role="Nope"))
    assert captured == {} and list(paths.ASSIGNMENTS_PENDING.glob("*")) == []


def test_a_claude_tui_child_refuses_extra_args(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    with pytest.raises(ValueError, match="a claude-tui child takes no extra_args"):
        asyncio.run(spawn_worker_impl("x", manager_sid="w1", cwd=str(work), extra_args=["--model", "sneaky"],
                                      _tool="spawn_child", **CHILD))
    assert captured == {} and list(paths.ASSIGNMENTS_PENDING.glob("*")) == []


def test_spawn_child_refuses_a_fable_model_on_the_headless_claude_provider(fresh_orchestrator_dir, monkeypatch, tmp_path):
    from dockwright.providers import get_provider
    work = _work_dir(monkeypatch, tmp_path)
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    asyncio.run(spawn_child(parent_sid="w1", brief="scout", cwd=str(work), provider="claude-exec", mode="report",
                            model="claude-fable-5-1", effort="high"))
    fable_round = captured["child_round"]
    assert fable_round.model == "claude-fable-5-1"
    with pytest.raises(ValueError, match="claude-exec refuses --model"):
        get_provider("claude-exec").spawn_command(fable_round)
    asyncio.run(spawn_child(parent_sid="w1", brief="verify", cwd=str(work), provider="claude-exec", mode="report",
                            model="claude-sonnet-5", effort="high"))
    ok_round = captured["child_round"]
    assert ok_round.model == "claude-sonnet-5"
    assert "--model" in get_provider("claude-exec").spawn_command(ok_round)


def test_a_refused_child_spawn_leaves_no_round_directory(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    _manager_and_worker()
    _fast_registration(monkeypatch)
    children = paths.ROOT / "children"
    before = sorted(children.glob("*")) if children.exists() else []
    with pytest.raises(ValueError, match="claude-exec refuses --model"):
        asyncio.run(spawn_child(parent_sid="w1", brief="scout", cwd=str(work), provider="claude-exec", mode="report",
                                model="claude-fable-5-1", effort="high"))
    after = sorted(children.glob("*")) if children.exists() else []
    assert after == before, f"refused spawn leaked {set(after) - set(before)}"


def test_a_headless_round_two_keeps_the_childs_effort(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work = _work_dir(monkeypatch, tmp_path)
    _manager_and_worker()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _fast_registration(monkeypatch)
    asyncio.run(spawn_child(parent_sid="w1", brief="scout", cwd=str(work), provider="claude-exec",
                            mode="report", model="claude-sonnet-5", effort="high"))
    assert captured["child_round"].effort == "high"
    pending = list(state.list_json_in(paths.ASSIGNMENTS_PENDING))[0]
    assert (pending["effort"], pending["model"]) == ("high", "claude-sonnet-5")
    report = _headless_child(monkeypatch, tmp_path, provider="claude-exec", active=False)
    assignment = state.read_json(paths.assignment_path("c1"))
    state.write_json_atomic(paths.assignment_path("c1"), {**assignment, "model": "claude-sonnet-5", "effort": "high"})
    report.write_text("round 1 report")
    resumed = _patch_spawn_that_registers(monkeypatch, "c1")
    monkeypatch.setattr(mcp_server, "_has_live_transcript", lambda sid, runtime=None: True)
    asyncio.run(send_to_child_impl("alpha-child", "round two"))
    assert resumed["child_round"].effort == "high" and resumed["child_round"].model == "claude-sonnet-5"
