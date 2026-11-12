import asyncio
import importlib.util
import io
import json
import os
import re
import sys
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from dockwright import config, failure_streak, hooks, mcp_server, monitor, paths, pipeline, state
from dockwright import mcp_server as ms
from dockwright import pipeline_reconcile as pr
from dockwright.event_sources import tracker
from dockwright.lane_io import LaneDead
from dockwright.revive import PAUSE_CLOSED_REASON, RESUME_CLOSED_REASON
from dockwright.spawner import UsageCapPaused
from dockwright.mcp_server import (_read_events, _request_advance_locked, _spawn_node, artifact_put_impl,
                                   pipeline_advance_impl, pipeline_start_impl, register_self_impl,
                                   resume_worker_impl, worker_done_impl)
from dockwright.stale_monitor import HUMAN_ATTACHED_TTL_SEC
from tests.test_human_attached import BUSY_PANE, IDLE_PANE
from tests.test_pipeline_core import SCHEMA

MGR = {"name": "mgr", "sid": "m1"}
TWO_PRS = SCHEMA.replace('outputs = ["implement.repo"]', 'outputs = ["implement.repo", "implement.contracts"]')


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
    monkeypatch.setattr(paths, "MANAGER_TRIGGERS_LOG", tmp_path / "manager-triggers.jsonl")
    monkeypatch.setattr(paths, "MANAGER_MEMORY", tmp_path / "manager-memory")
    monkeypatch.setattr(paths, "SLOTS", tmp_path / "slots")
    monkeypatch.setattr(paths, "ARTIFACTS", tmp_path / "artifacts")
    monkeypatch.setattr(paths, "ASSIGNMENTS", tmp_path / "assignments")
    monkeypatch.setattr(paths, "ASSIGNMENTS_PENDING", tmp_path / "assignments" / ".pending")
    monkeypatch.setattr(paths, "PANE_SENDS", tmp_path / "pane-sends")
    monkeypatch.setattr(paths, "SPEND_LEDGER", tmp_path / "spend-ledger.jsonl")
    monkeypatch.setattr(paths, "TURN_ENDS", tmp_path / "turn-ends")
    monkeypatch.setattr(paths, "ARCHITECT", tmp_path / "architect")
    paths.ensure_dirs()
    yield tmp_path


@pytest.fixture
def clock(monkeypatch):
    now = {"t": time.time()}
    monkeypatch.setattr(time, "time", lambda: now["t"])
    return now


@pytest.fixture(autouse=True)
def killed(monkeypatch):
    calls = []
    monkeypatch.setattr("dockwright.mcp_server.kill_worker_impl",
                        lambda worker, dry_run=False: calls.append(worker) or {})
    return calls


@pytest.fixture(autouse=True)
def session_logs(monkeypatch):
    logs = {}
    monkeypatch.setattr("dockwright.pipeline_reconcile.find_session_log",
                        lambda sid, runtime="claude": logs.get((sid, runtime)))
    return logs


def _write_example_schema(tmp_path, text=SCHEMA):
    d = tmp_path / "schemas"
    (d / "briefs").mkdir(parents=True, exist_ok=True)
    (d / "code-ticket.toml").write_text(text)
    for n in ("design", "task", "pr", "post-merge"):
        (d / "briefs" / f"{n}.md").write_text(f"# {n} brief for {{task_key}} at {{cwd}} inputs {{inputs}}\n")
    return d / "code-ticket.toml"


def _manager(sid="m1", name="mgr"):
    register_self_impl(sid, "manager", name, "/tmp", "", pid=None)
    return sid


def _register_worker(name, sid, assignment_id, task_key, cwd="/w", manager_sid="m1"):
    register_self_impl(sid, "worker", name, cwd, "", pid=None, parent_manager_name="mgr", parent_sid=manager_sid)
    state.write_json_atomic(paths.assignment_path(sid), {"assignment_id": assignment_id, "claude_sid": sid,
                                                          "ticket": task_key, "name": name, "manager_sid": manager_sid,
                                                          "parent_name": "mgr", "cwd": cwd, "spawned_at": 1.0})


def _pending_record_for(name):
    for p in paths.ASSIGNMENTS_PENDING.glob("*.json"):
        record = state.read_json(p) or {}
        if record.get("name") == name:
            return p, record
    raise AssertionError(f"no pending assignment named {name}")


def _claim(name, sid):
    pending, record = _pending_record_for(name)
    register_self_impl(sid, "worker", name, record.get("cwd") or "/w", "", pid=None,
                       parent_manager_name=record.get("parent_name"), parent_sid=record.get("manager_sid"))
    os.replace(pending, paths.assignment_path(sid))
    state.write_json_atomic(paths.assignment_path(sid),
                            {**record, "claude_sid": sid, "name": name, "claimed_at": time.time()})


def _fake_registration(monkeypatch, outcomes, spawn_error=None):
    calls = []

    async def fake_spawn(**kw):
        calls.append(kw)
        await asyncio.sleep(0.01)
        if spawn_error is not None:
            raise spawn_error
        return ("999", kw["name"])

    async def fake_confirm(name, timeout, poll):
        if outcomes.pop(0) != "registered":
            return None
        _claim(name, f"sid-{name}")
        return state.read_json(paths.ACTIVE / f"sid-{name}.json")

    monkeypatch.setattr("dockwright.spawner.spawn_worker_tab", fake_spawn)
    monkeypatch.setattr("dockwright.mcp_server._confirm_spawn_registration", fake_confirm)
    return calls


def _events(task_key="K"):
    return _read_events(paths.artifact_events_path(task_key))


def _of(kind, task_key="K"):
    return [e for e in _events(task_key) if e["type"] == kind]


def _fold(task_key="K"):
    return pipeline.fold(_events(task_key))


def _later(sec):
    return time.time() + sec


def _heads(lines):
    return [line.split(" → ", 1)[0] for line in lines]


def _reconciled(task_key="K"):
    return [e["sid"] for e in _of("advance_committed", task_key) if e.get("reconciled")]


def _lane_events(kind, task_key="K"):
    return [e["fields"] for e in _of("lane_event", task_key) if e.get("kind") == kind]


def _without_assignment(kind, task_key="K"):
    return [{k: v for k, v in f.items() if k != "assignment_id"} for f in _lane_events(kind, task_key)]


def _closes(task_key="K"):
    return _without_assignment("HANDED_OFF_CLOSED", task_key)


def _closed(name, assignment_id=None, task_key="K", reason="grace"):
    fields = {"task_key": task_key, "name": name, "reason": reason}
    return fields if assignment_id is None else {**fields, "assignment_id": assignment_id}


def _done(sid, summary="done"):
    return worker_done_impl(sid, summary)["event_id"]


UNREGISTERED = ('ADVANCE_UNREGISTERED K K-task-1 999 → clear the prompt of pane 999 or close the pane; '
                'kill_worker("K-task-1") only once it is dead')
STUCK_NO_REGISTER = ('ADVANCE_STUCK K design no_register → pipeline_status("K"); the session never registered: an '
                     'ADVANCE_UNREGISTERED line names its pane')
STUCK_SPAWN_ERROR = ('ADVANCE_STUCK K task spawn-error → pipeline_status("K"); the engine retries the spawn each '
                     'requested_ttl_sec')
STUCK_PAUSED = ('ADVANCE_STUCK K task paused → pipeline_status("K"); the engine retries the spawn each '
                "requested_ttl_sec; check the usage pause (spawn_worker's paused result)")
PARKED_IDLE = ('NODE_PARKED K design K-design → resume_worker("K-design") once the engineer is back at the gate; if it '
               'waits on someone the engine does not watch: pipeline_advance(claude_sid=<your sid>, '
               'task_key="K", to="?", reason=<what it waits on>) holds it and ends this page — idle>7200s')
UNADVANCED = ('NODE_UNADVANCED K design 1 K-design → send_manager_to_worker("K-design", "continue your node", '
              'auto_resume=True); nothing to resume: pipeline_advance(claude_sid=<your sid>, task_key="K", '
              'to="design", index=1, override=True, reason=<why>)')
POLL_FAILED = ('TRIGGER_POLL_FAILED K https://github.com/o/r/pull/7 → check gh in the work dir (auth, account, '
               'network, a gone dir); once every PR has merged: pipeline_advance(claude_sid=<your sid>, '
               'task_key="K", to=<next node>, reason=<mergedAt>)')
HELD = ('NODE_HELD K design K-design → once the reason clears: pipeline_advance(claude_sid=<your sid>, task_key="K", '
        'to="?") with no reason releases it, or move it; if it will never run again: pipeline_close('
        'manager_sid=<your sid>, task_key="K", reason=<why>); this page does not repeat')
NO_SUMMARY = {"task_key": "K", "node": "design", "name": "K-design"}


def _setup_schema(monkeypatch, tmp_path, design_brief=None, schema_text=SCHEMA):
    schema_path = _write_example_schema(tmp_path, schema_text)
    if design_brief is not None:
        (schema_path.parent / "briefs" / "design.md").write_text(design_brief)
    monkeypatch.setattr("dockwright.config.pipeline_schemas_dir", lambda: schema_path.parent)
    return schema_path


def _started_at_design(monkeypatch, tmp_path, design_brief=None, cwd="/w", schema_text=SCHEMA):
    _setup_schema(monkeypatch, tmp_path, design_brief, schema_text)
    _manager()
    _fake_registration(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_start_impl("m1", "K", "code-ticket", cwd, "ask"))
    return r["next"]["sid"], r["next"]["assignment_id"]


def _summary(sid, task_key="K"):
    artifact_put_impl(task_key, "summary", "thread", "where the work stands", "complete", sid, scope="output",
                      overwrite=True)


def _complete_design_outputs(sid, plan='{"tasks": [{"id": 1}, {"id": 2}]}', summary=True):
    for name in ("context.ask", "design.hld", "plan.tasks"):
        phase, n = name.split(".", 1)
        body = plan if name == "plan.tasks" else "x"
        try:
            artifact_put_impl("K", phase, n, body, "complete", sid, scope="output")
        except ValueError:
            artifact_put_impl("K", phase, n, body, "complete", sid, scope="output", overwrite=True)
    if summary:
        _summary(sid)


def _at_pr_with_a_pr_url(monkeypatch, tmp_path, body="https://github.com/o/r/pull/7", cwd="/w"):
    _started_at_design(monkeypatch, tmp_path, cwd=cwd)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="jump"))
    artifact_put_impl("K", "implement", "repo", body, "complete", "sid-K-pr", scope="output")


def _corrupt(task_key="K"):
    state.append_event(paths.artifact_events_path(task_key), {"type": "advance_committed", "request_id": "zz"})


def test_open_request_past_ttl_without_assignment_is_respawned_once(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                    killed):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["no_register"])
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    assert r["state"] == "pending"
    for p in paths.ASSIGNMENTS_PENDING.glob("*.json"):
        p.unlink()
    assert pr.reconcile(MGR, now=_later(10)) == []
    calls = _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR, now=_later(400)) == [] and _reconciled() == ["sid-K-task-1-r2"]
    assert killed == []
    f = _fold()
    assert f.tokens["t1"].node == "task" and f.open_requests == []
    lines = pr.reconcile(MGR, now=_later(800))
    assert lines == [] and len(calls) == 1 and killed == ["K-design"]
    assert _closes() == [_closed("K-design")]
    committed = _of("advance_committed")
    assert committed[-1]["request_id"] == r["request_id"] and committed[-1]["reconciled"] is True


def test_late_registration_is_settled_not_respawned(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["no_register"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    pend = _of("advance_pending")[0]
    _register_worker("K-task-1", "sid-late", pend["assignment_id"], "K")
    for p in paths.ASSIGNMENTS_PENDING.glob("*.json"):
        p.unlink()
    calls = _fake_registration(monkeypatch, [])
    lines = pr.reconcile(MGR, now=_later(400))
    assert lines == [] and _reconciled() == ["sid-late"] and calls == []
    f = _fold()
    assert f.tokens["t1"].holder == pend["assignment_id"] and f.open_requests == []
    assert [e["assignment_id"] for e in _of("handed_off")] == [aid]
    assert _of("advance_committed")[-1]["reconciled"] is True


def test_unregistered_pending_pane_is_paged_not_closed(fresh_orchestrator_dir, tmp_path, monkeypatch, killed):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["no_register"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    calls = _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR, now=_later(400)) == [UNREGISTERED]
    assert list(paths.ASSIGNMENTS_PENDING.glob("*.json"))
    assert pr.reconcile(MGR, now=_later(500)) == []
    assert pr.reconcile(MGR, now=_later(1100)) == [UNREGISTERED]
    assert calls == [] and killed == [] and len(_fold().open_requests) == 1


def test_dead_holder_on_the_token_node_is_restarted_from_checkpoints(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                     session_logs):
    sid, aid = _started_at_design(monkeypatch, tmp_path, design_brief="resume from: {checkpoints}\n")
    artifact_put_impl("K", "design", "explainer", "half", "partial", sid, scope="checkpoint")
    (paths.ACTIVE / f"{sid}.json").unlink()
    log = tmp_path / "old.jsonl"
    log.write_text("x" * 1234)
    session_logs[(sid, "claude")] = log
    calls = _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR) == [] and _reconciled() == ["sid-K-design-r2"]
    assert calls[-1]["name"] == "K-design-r2"
    assert "resume from: design.explainer" in calls[-1]["initial_prompt"]
    assert "previous session died" in calls[-1]["initial_prompt"]
    requested = _of("advance_requested")[-1]
    assert (requested["kind"], requested["edge"], requested["index_to"], requested["actor_sid"]) == (
        "restart", {"from": "design", "to": "design"}, 1, "m1")
    f = _fold()
    assert f.tokens["t1"].node == "design" and f.tokens["t1"].holder != aid and f.open_requests == []
    restamp = _of("holder_restamped")[-1]
    assert (restamp["request_id"], restamp["old"], restamp["new"], restamp["transcript_bytes"]) == (
        requested["request_id"], aid, f.tokens["t1"].holder, 1234)
    (paths.ACTIVE / "sid-K-design-r2.json").unlink()
    calls = _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR) == [] and _reconciled() == ["sid-K-design-r2", "sid-K-design-r3"]
    assert _of("holder_restamped")[-1]["transcript_bytes"] is None


def test_a_restamp_never_overwrites_a_holder_committed_after_the_restart(fresh_orchestrator_dir, tmp_path,
                                                                        monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    (paths.ACTIVE / f"{sid}.json").unlink()
    real_commit = mcp_server._commit_or_pend

    def commit_then_advance(*args, **kwargs):
        result = real_commit(*args, **kwargs)
        mcp_server._append_task_event("K", {"type": "advance_committed", "token_id": "t1", "request_id": "later",
                                            "node": "task", "index": 1, "holder": "a-later", "sid": "sid-later"})
        return result

    monkeypatch.setattr("dockwright.mcp_server._commit_or_pend", commit_then_advance)
    _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR) == [] and _reconciled() == ["sid-K-design-r2"]
    assert _of("holder_restamped") == [] and _fold().tokens["t1"].holder == "a-later"


@pytest.mark.parametrize("alive,expected", [(False, ["sid-K-design-r2"]), (True, [])])
def test_a_holder_record_left_on_disk_is_judged_by_its_pid(fresh_orchestrator_dir, tmp_path, monkeypatch, alive,
                                                           expected):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    record = state.read_json(paths.ACTIVE / f"{sid}.json")
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {**record, "pid": 4242})
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: alive if pid == 4242 else True)
    calls = _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR) == [] and _reconciled() == expected
    assert len(calls) == len(expected)


def test_a_restart_that_does_not_register_is_paged_then_settled(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    (paths.ACTIVE / f"{sid}.json").unlink()
    _fake_registration(monkeypatch, ["no_register"])
    assert pr.reconcile(MGR) == [STUCK_NO_REGISTER]
    rid = _of("advance_requested")[-1]["request_id"]
    pend = _of("advance_pending")[-1]
    assert pend["request_id"] == rid and pend["reason"] == "no_register" and pend["assignment_id"]
    assert [r["request_id"] for r in _fold().open_requests] == [rid]
    calls = _fake_registration(monkeypatch, [])
    assert _heads(pr.reconcile(MGR, now=_later(400))) == ["ADVANCE_UNREGISTERED K K-design-r2 999"]
    _claim("K-design-r2", "sid-r2")
    assert pr.reconcile(MGR, now=_later(1200)) == [] and _reconciled() == ["sid-r2"]
    assert calls == []
    f = _fold()
    assert f.tokens["t1"].holder == pend["assignment_id"] and f.open_requests == []
    restamp = _of("holder_restamped")[-1]
    assert (restamp["request_id"], restamp["old"], restamp["new"]) == (rid, aid, pend["assignment_id"])


def test_a_session_spawned_before_its_caller_died_is_settled_not_duplicated(fresh_orchestrator_dir, tmp_path,
                                                                           monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    req = _request_advance_locked(sid, "K", "task")
    calls = _fake_registration(monkeypatch, ["registered"])
    spawned = asyncio.run(_spawn_node("K", req["snap"], req["token"], "task", 1, "m1", req["request_id"], "advance",
                                      []))
    assert spawned["status"] == "registered"
    assert state.read_json(paths.assignment_path(spawned["claude_sid"]))["pipeline_request_id"] == req["request_id"]
    lines = pr.reconcile(MGR, now=_later(400))
    assert lines == [] and _reconciled() == [spawned["claude_sid"]] and len(calls) == 1
    f = _fold()
    assert f.tokens["t1"].holder == spawned["assignment_id"] and f.open_requests == []
    assert [e["assignment_id"] for e in _of("handed_off")] == [aid]
    assert _of("holder_restamped") == []


def _pending_request_without_a_pane(monkeypatch, tmp_path):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["no_register"])
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    for p in paths.ASSIGNMENTS_PENDING.glob("*.json"):
        p.unlink()
    return r["request_id"]


def test_a_reconcile_spawn_that_raises_retries_once_per_ttl_and_pages_once_per_window(fresh_orchestrator_dir,
                                                                                      tmp_path, monkeypatch, clock):
    start = clock["t"]
    request_id = _pending_request_without_a_pane(monkeypatch, tmp_path)
    calls = _fake_registration(monkeypatch, [], spawn_error=OSError("tmux down"))
    clock["t"] = start + 400
    assert pr.reconcile(MGR) == [STUCK_SPAWN_ERROR]
    pend = _of("advance_pending")[-1]
    assert (pend["request_id"], pend["reason"], pend["assignment_id"]) == (request_id, "spawn-error", None)
    assert "Could not spawn a tab" in pend["error"]
    clock["t"] = start + 500
    assert pr.reconcile(MGR) == [] and len(_of("advance_pending")) == 2
    clock["t"] = start + 800
    assert pr.reconcile(MGR) == [] and len(_of("advance_pending")) == 3
    clock["t"] = start + 1100
    assert pr.reconcile(MGR) == [STUCK_SPAWN_ERROR] and len(_of("advance_pending")) == 4
    assert [q["request_id"] for q in _fold().open_requests] == [request_id]
    assert [c["name"] for c in calls] == ["K-task-1-r2", "K-task-1-r3", "K-task-1-r4"]


def test_a_restart_respawned_after_its_pane_vanished_counts_the_attempt(fresh_orchestrator_dir, tmp_path,
                                                                        monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    (paths.ACTIVE / f"{sid}.json").unlink()
    calls = _fake_registration(monkeypatch, ["no_register"])
    assert _heads(pr.reconcile(MGR)) == ["ADVANCE_STUCK K design no_register"]
    for p in paths.ASSIGNMENTS_PENDING.glob("*.json"):
        p.unlink()
    calls = _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR, now=_later(400)) == [] and _reconciled() == ["sid-K-design-r3"]
    assert [c["name"] for c in calls] == ["K-design-r3"]


def test_a_paused_reconcile_spawn_retries_once_per_ttl(fresh_orchestrator_dir, tmp_path, monkeypatch, clock):
    start = clock["t"]
    _pending_request_without_a_pane(monkeypatch, tmp_path)
    calls = _fake_registration(monkeypatch, [])
    monkeypatch.setattr("dockwright.spawner.usage_spawn_gate",
                        lambda force=False: {"status": "paused", "reason": "usage limit"})
    clock["t"] = start + 400
    assert pr.reconcile(MGR) == [STUCK_PAUSED]
    assert [e["reason"] for e in _of("advance_pending")] == ["no_register", "paused"]
    clock["t"] = start + 460
    assert pr.reconcile(MGR) == [] and len(_of("advance_pending")) == 2
    clock["t"] = start + 800
    pr.reconcile(MGR)
    assert [e["reason"] for e in _of("advance_pending")] == ["no_register", "paused", "paused"] and calls == []


def test_a_usage_cap_pend_retries_each_ttl_and_never_pages(fresh_orchestrator_dir, tmp_path, monkeypatch, clock):
    start = clock["t"]
    _pending_request_without_a_pane(monkeypatch, tmp_path)
    calls = _fake_registration(monkeypatch, [])
    monkeypatch.setattr("dockwright.spawner.usage_spawn_gate",
                        lambda force=False: {"status": "paused", "pend_reason": "usage-cap",
                                             "reason": "usage cap: b 5h 71% ≥ 70%"})
    clock["t"] = start + 400
    assert pr.reconcile(MGR) == []
    assert [e["reason"] for e in _of("advance_pending")] == ["no_register", "usage-cap"]
    clock["t"] = start + 800
    assert pr.reconcile(MGR) == []
    assert [e["reason"] for e in _of("advance_pending")] == ["no_register", "usage-cap", "usage-cap"]
    assert calls == []


def test_a_usage_cap_refusal_from_the_tab_pends_usage_cap_without_a_page(fresh_orchestrator_dir, tmp_path,
                                                                          monkeypatch, clock):
    start = clock["t"]
    request_id = _pending_request_without_a_pane(monkeypatch, tmp_path)
    _fake_registration(monkeypatch, [], spawn_error=UsageCapPaused("usage cap: b 5h 71% ≥ 70%"))
    clock["t"] = start + 400
    assert pr.reconcile(MGR) == []
    pend = _of("advance_pending")[-1]
    assert (pend["request_id"], pend["reason"], pend["error"]) == (request_id, "usage-cap",
                                                                   "usage cap: b 5h 71% ≥ 70%")


@pytest.mark.parametrize("reason", ["session_end", "usage-cap-node"])
def test_a_cleanly_closed_holder_waits_one_ttl_before_it_is_restarted(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                      reason):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    (paths.ACTIVE / f"{sid}.json").unlink()
    state.write_json_atomic(paths.CLOSED / f"{sid}.json", {"claude_sid": sid, "name": "K-design",
                                                           "closed_reason": reason, "closed_at": time.time() - 10})
    calls = _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR) == [] and calls == []
    assert pr.reconcile(MGR, now=_later(400)) == [] and _reconciled() == ["sid-K-design-r2"]


def test_a_fleet_paused_holder_is_not_restarted_after_the_ttl(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    (paths.ACTIVE / f"{sid}.json").unlink()
    state.write_json_atomic(paths.CLOSED / f"{sid}.json", {"claude_sid": sid, "name": "K-design",
                                                           "closed_reason": PAUSE_CLOSED_REASON,
                                                           "closed_at": time.time() - 10})
    calls = _fake_registration(monkeypatch, ["registered"])
    pr.reconcile(MGR)
    assert pr.reconcile(MGR, now=_later(400)) == [] and calls == [] and _reconciled() == []


def test_a_holder_resumed_inside_the_window_is_never_restarted(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    active = state.read_json(paths.ACTIVE / f"{sid}.json")
    (paths.ACTIVE / f"{sid}.json").unlink()
    state.write_json_atomic(paths.CLOSED / f"{sid}.json", {"claude_sid": sid, "name": "K-design",
                                                           "closed_reason": "session_end", "closed_at": time.time() - 10})
    calls = _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR) == []
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", active)
    assert pr.reconcile(MGR, now=_later(400)) == [] and pr.reconcile(MGR, now=_later(4000)) == []
    assert calls == [] and _fold().tokens["t1"].holder == aid


def test_a_holder_lost_in_a_host_reboot_is_parked(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    (paths.ACTIVE / f"{sid}.json").unlink()
    state.write_json_atomic(paths.CLOSED / f"{sid}.json", {"claude_sid": sid, "name": "K-design",
                                                           "closed_reason": RESUME_CLOSED_REASON,
                                                           "closed_at": time.time() - 1000})
    calls = _fake_registration(monkeypatch, [])
    assert _heads(pr.reconcile(MGR)) == ["NODE_PARKED K design K-design"]
    assert pr.reconcile(MGR, now=_later(1800)) == []
    assert calls == [] and _fold().tokens["t1"].holder == aid and _fold().open_requests == []


def _at_post_merge(monkeypatch, tmp_path):
    _started_at_design(monkeypatch, tmp_path)
    _fake_registration(monkeypatch, ["registered"])
    return asyncio.run(pipeline_advance_impl("m1", "K", "post-merge", override=True, reason="jump"))["next"]["sid"]


def test_a_dead_holder_on_a_terminal_node_with_open_outputs_is_restarted(fresh_orchestrator_dir, tmp_path,
                                                                         monkeypatch):
    sid = _at_post_merge(monkeypatch, tmp_path)
    (paths.ACTIVE / f"{sid}.json").unlink()
    calls = _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR) == [] and _reconciled() == ["sid-K-post-merge-r2"] and len(calls) == 1


def test_a_terminal_node_with_complete_outputs_is_left_alone(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid = _at_post_merge(monkeypatch, tmp_path)
    artifact_put_impl("K", "summary", "post-merge", "done", "complete", sid, scope="output")
    (paths.ACTIVE / f"{sid}.json").unlink()
    calls = _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR) == [] and pr.reconcile(MGR, now=_later(400)) == [] and calls == []


def test_autoclosed_parked_node_is_left_for_the_manager(fresh_orchestrator_dir, tmp_path, monkeypatch, capsys):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    (paths.ACTIVE / f"{sid}.json").unlink()
    state.write_json_atomic(paths.CLOSED / f"{sid}.json", {"claude_sid": sid, "name": "K-design",
                                                           "closed_reason": "idle>7200s", "cwd": "/w"})
    calls = _fake_registration(monkeypatch, [])
    capsys.readouterr()
    assert pr.reconcile(MGR) == [PARKED_IDLE]
    assert capsys.readouterr().out == PARKED_IDLE + "\n"
    assert pr.reconcile(MGR, now=_later(1800)) == []
    assert pr.reconcile(MGR, now=_later(3700)) == [PARKED_IDLE]
    assert _fold().tokens["t1"].holder == aid and calls == []


def test_a_hold_ends_the_parked_page_and_pages_held_once(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    (paths.ACTIVE / f"{sid}.json").unlink()
    state.write_json_atomic(paths.CLOSED / f"{sid}.json", {"claude_sid": sid, "name": "K-design",
                                                           "closed_reason": "idle>7200s", "cwd": "/w"})
    calls = _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR) == [PARKED_IDLE]
    asyncio.run(pipeline_advance_impl("m1", "K", "?", reason="waits on a reply"))
    lines = pr.reconcile(MGR)
    assert len(lines) == 1 and lines[0].startswith("NODE_HELD K design K-design → ")
    later = pr.reconcile(MGR, now=_later(3700))
    assert not any(line.startswith(("NODE_PARKED", "NODE_HELD")) for line in later)
    assert calls == []


def _no_summary():
    return _without_assignment("NODE_NO_SUMMARY")


def _design_advanced(monkeypatch, tmp_path, summary):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid, summary=summary)
    _fake_registration(monkeypatch, ["registered"])
    assert asyncio.run(pipeline_advance_impl(sid, "K", "task"))["state"] == "committed"
    return sid


def test_a_node_that_hands_off_without_a_summary_is_paged_once(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _design_advanced(monkeypatch, tmp_path, summary=False)
    assert pr.reconcile(MGR) == [] and _no_summary() == [NO_SUMMARY]
    assert pr.reconcile(MGR) == [] and _no_summary() == [NO_SUMMARY]


def test_a_node_whose_last_write_is_the_summary_is_not_paged(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _design_advanced(monkeypatch, tmp_path, summary=True)
    assert pr.reconcile(MGR) == [] and _no_summary() == []


def test_a_summary_followed_by_other_writes_is_not_fresh(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _summary(sid)
    _complete_design_outputs(sid, summary=False)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    assert pr.reconcile(MGR) == [] and _no_summary() == [NO_SUMMARY]


def test_a_node_the_manager_moves_off_is_not_paged(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "design", override=True, reason="redo"))
    pr.reconcile(MGR)
    assert _no_summary() == []


def test_a_restarted_dead_holder_is_not_paged(fresh_orchestrator_dir, tmp_path, monkeypatch, session_logs):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    (paths.ACTIVE / f"{sid}.json").unlink()
    _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR) == [] and _reconciled() == ["sid-K-design-r2"]
    pr.reconcile(MGR)
    assert _no_summary() == []


def test_a_hand_off_older_than_an_hour_is_not_paged(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _design_advanced(monkeypatch, tmp_path, summary=False)
    pr.reconcile(MGR, now=_later(3700))
    assert _no_summary() == []


def _rotate(old, new):
    record = state.read_json(paths.ACTIVE / f"{old}.json")
    (paths.ACTIVE / f"{old}.json").unlink()
    register_self_impl(new, "worker", record["name"], record.get("cwd") or "/w", "", pid=None,
                       parent_manager_name="mgr", parent_sid="m1")
    state.migrate_assignment(old, new)


def test_a_node_whose_session_rotated_after_its_summary_is_not_paged(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _rotate(sid, "sid-rotated")
    _fake_registration(monkeypatch, ["registered"])
    assert asyncio.run(pipeline_advance_impl("sid-rotated", "K", "task"))["state"] == "committed"
    assert pr.reconcile(MGR) == [] and _no_summary() == []


def test_a_node_whose_summary_came_from_a_session_between_two_rotations_is_not_paged(fresh_orchestrator_dir,
                                                                                     tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid, summary=False)
    _rotate(sid, "sid-middle")
    _summary("sid-middle")
    _rotate("sid-middle", "sid-last")
    _fake_registration(monkeypatch, ["registered"])
    assert asyncio.run(pipeline_advance_impl("sid-last", "K", "task"))["state"] == "committed"
    assert pr.reconcile(MGR) == [] and _no_summary() == []


def test_a_node_whose_middle_session_wrote_after_the_summary_is_paged(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _summary(sid)
    _rotate(sid, "sid-middle")
    _complete_design_outputs("sid-middle", summary=False)
    _rotate("sid-middle", "sid-last")
    _fake_registration(monkeypatch, ["registered"])
    assert asyncio.run(pipeline_advance_impl("sid-last", "K", "task"))["state"] == "committed"
    assert pr.reconcile(MGR) == [] and _no_summary() == [NO_SUMMARY]


def test_a_node_whose_session_rotated_after_its_own_advance_is_paged(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid, summary=False)
    _fake_registration(monkeypatch, ["no_register"])
    assert asyncio.run(pipeline_advance_impl(sid, "K", "task"))["state"] == "pending"
    _rotate(sid, "sid-rotated")
    for p in paths.ASSIGNMENTS_PENDING.glob("*.json"):
        p.unlink()
    _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR, now=_later(400)) == [] and _reconciled() == ["sid-K-task-1-r2"]
    assert _no_summary() == []
    pr.reconcile(MGR, now=_later(460))
    assert _no_summary() == [NO_SUMMARY]


def test_a_throttle_file_that_cannot_be_written_never_suppresses_a_page(fresh_orchestrator_dir, tmp_path,
                                                                        monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    (paths.ACTIVE / f"{sid}.json").unlink()
    state.write_json_atomic(paths.CLOSED / f"{sid}.json", {"claude_sid": sid, "name": "K-design",
                                                           "closed_reason": "idle>7200s"})
    (paths.ROOT / ".pipeline-pages-mgr.json").mkdir()
    assert pr.reconcile(MGR) == [PARKED_IDLE]
    assert pr.reconcile(MGR) == [PARKED_IDLE]


def test_dead_holder_with_an_open_request_is_not_restarted(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["no_register"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    (paths.ACTIVE / f"{sid}.json").unlink()
    calls = _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR, now=_later(10)) == [] and calls == []


def test_token_without_holder_is_never_restarted(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _setup_schema(monkeypatch, tmp_path)
    _manager()
    _fake_registration(monkeypatch, ["no_register"])
    asyncio.run(pipeline_start_impl("m1", "K", "code-ticket", "/w", "ask"))
    calls = _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR, now=_later(10)) == [] and calls == []


def test_dead_session_of_another_node_is_ignored(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _register_worker("K-stray", "sid-stray", "a-stray", "K")
    (paths.ACTIVE / "sid-stray.json").unlink()
    calls = _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR) == [] and calls == []


def test_a_dead_holder_whose_outputs_are_complete_is_paged_not_restarted(fresh_orchestrator_dir, tmp_path,
                                                                          monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    (paths.ACTIVE / f"{sid}.json").unlink()
    calls = _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR) == [UNADVANCED]
    assert pr.reconcile(MGR, now=_later(300)) == []
    assert pr.reconcile(MGR, now=_later(700)) == [UNADVANCED]
    assert calls == [] and _fold().tokens["t1"].holder == aid and _fold().open_requests == []


def test_a_cleanly_closed_holder_whose_outputs_are_complete_waits_one_ttl_then_is_paged(fresh_orchestrator_dir,
                                                                                         tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    (paths.ACTIVE / f"{sid}.json").unlink()
    state.write_json_atomic(paths.CLOSED / f"{sid}.json", {"claude_sid": sid, "name": "K-design",
                                                           "closed_reason": "session_end", "closed_at": time.time() - 10})
    calls = _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR) == []
    assert _heads(pr.reconcile(MGR, now=_later(400))) == ["NODE_UNADVANCED K design 1 K-design"]
    assert calls == [] and _fold().tokens["t1"].holder == aid


@pytest.mark.parametrize("reason", ["idle>7200s", RESUME_CLOSED_REASON, PAUSE_CLOSED_REASON])
def test_a_parked_holder_whose_outputs_are_complete_stays_parked(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                  reason):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    (paths.ACTIVE / f"{sid}.json").unlink()
    state.write_json_atomic(paths.CLOSED / f"{sid}.json", {"claude_sid": sid, "name": "K-design",
                                                           "closed_reason": reason, "closed_at": time.time() - 10})
    calls = _fake_registration(monkeypatch, [])
    assert _heads(pr.reconcile(MGR)) == ["NODE_PARKED K design K-design"]
    assert pr.reconcile(MGR, now=_later(700)) == []
    assert calls == [] and _fold().tokens["t1"].holder == aid


def test_a_redo_holder_after_an_override_back_is_paged_not_restarted(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    _fake_registration(monkeypatch, ["registered"])
    redo = asyncio.run(pipeline_advance_impl("m1", "K", "design", override=True, reason="redesign"))["next"]
    (paths.ACTIVE / f"{redo['sid']}.json").unlink()
    calls = _fake_registration(monkeypatch, [])
    assert _heads(pr.reconcile(MGR)) == [f"NODE_UNADVANCED K design 1 {redo['name']}"]
    assert calls == [] and _fold().tokens["t1"].holder == redo["assignment_id"]


def test_a_dead_task_holder_whose_review_is_complete_is_paged(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    artifact_put_impl("K", "review", "task-1", "ok", "complete", "sid-K-task-1", scope="output")
    (paths.ACTIVE / "sid-K-task-1.json").unlink()
    calls = _fake_registration(monkeypatch, [])
    assert _heads(pr.reconcile(MGR)) == ["NODE_UNADVANCED K task 1 K-task-1"]
    assert calls == [] and _fold().tokens["t1"].node == "task"


def test_an_idle_closed_pr_holder_whose_outputs_are_complete_is_left_to_the_merge_trigger(fresh_orchestrator_dir,
                                                                                         tmp_path, monkeypatch):
    _at_pr_with_a_pr_url(monkeypatch, tmp_path)
    (paths.ACTIVE / "sid-K-pr.json").unlink()
    state.write_json_atomic(paths.CLOSED / "sid-K-pr.json", {"claude_sid": "sid-K-pr", "name": "K-pr",
                                                             "closed_reason": "idle>7200s",
                                                             "closed_at": time.time() - 1000})
    seen = []

    def gh(argv):
        seen.append(argv)
        return json.dumps({"state": "OPEN", "mergedAt": None})

    calls = _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR, run_gh=gh) == []
    assert len(seen) == 2 and calls == [] and _fold().tokens["t1"].node == "pr"


def test_handed_off_session_still_alive_is_closed(fresh_orchestrator_dir, tmp_path, monkeypatch, killed):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    assert pr.reconcile(MGR, now=_later(100)) == [] and killed == []
    assert pr.reconcile(MGR, now=_later(400)) == []
    assert killed == ["K-design"] and _closes() == [_closed("K-design")]
    assert pr.reconcile(MGR, now=_later(500)) == [] and killed == ["K-design"]
    assert _closes() == [_closed("K-design")]


def test_a_handed_off_event_without_a_stamp_closes_the_session_unless_a_human_holds_it(fresh_orchestrator_dir,
                                                                                      tmp_path, monkeypatch, killed):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    assignment = state.read_json(paths.assignment_path(sid))
    assignment.pop("handed_off_at")
    state.write_json_atomic(paths.assignment_path(sid), assignment)
    active = state.read_json(paths.ACTIVE / f"{sid}.json")
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {**active, "human_attached_at": time.time()})
    assert pr.reconcile(MGR, now=_later(400)) == [] and killed == []
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", active)
    assert pr.reconcile(MGR, now=_later(400)) == []
    assert killed == ["K-design"] and _closes() == [_closed("K-design")]


def test_a_handed_off_session_with_a_report_closes_at_the_next_pass(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                     killed, clock):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    clock["t"] += 5
    _done(sid)
    assert pr.reconcile(MGR, now=_later(10)) == []
    assert killed == ["K-design"] and _closes() == [_closed("K-design", reason="done")]


def test_a_report_older_than_the_handoff_does_not_count(fresh_orchestrator_dir, tmp_path, monkeypatch, killed, clock):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _done(sid, "PARKED at the gate")
    clock["t"] += 5
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    assert pr.reconcile(MGR, now=_later(100)) == [] and killed == []
    assert pr.reconcile(MGR, now=_later(400)) == []
    assert killed == ["K-design"] and _closes() == [_closed("K-design", reason="grace")]


def test_a_finished_post_merge_holder_is_stamped_and_closed(fresh_orchestrator_dir, tmp_path, monkeypatch, killed,
                                                            clock):
    sid = _at_post_merge(monkeypatch, tmp_path)
    holder = _fold().tokens["t1"].holder
    artifact_put_impl("K", "summary", "post-merge", "merged", "complete", sid, scope="output")
    clock["t"] += 5
    _done(sid, "merged #7")
    assert pr.reconcile(MGR) == []
    assert killed == ["K-post-merge"]
    assert _closes()[-1] == _closed("K-post-merge", reason="finished")
    assert _of("handed_off")[-1]["assignment_id"] == holder
    assert state.read_json(paths.assignment_path(sid))["handed_off_at"] > 0
    f = _fold()
    assert f.tokens["t1"].node == "post-merge" and f.tokens["t1"].holder == holder and f.closed is None
    assert _of("work_closed") == []


BACK_FROM_POST_MERGE = SCHEMA + '\n[[edges]]\nfrom = "post-merge"\nto = "task"\nback = true\n'


def test_a_back_edge_does_not_keep_a_finished_post_merge_holder_open(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                      killed, clock):
    _started_at_design(monkeypatch, tmp_path, schema_text=BACK_FROM_POST_MERGE)
    _fake_registration(monkeypatch, ["registered"])
    sid = asyncio.run(pipeline_advance_impl("m1", "K", "post-merge", override=True, reason="jump"))["next"]["sid"]
    artifact_put_impl("K", "summary", "post-merge", "merged", "complete", sid, scope="output")
    clock["t"] += 5
    _done(sid, "merged #7")
    assert pr.reconcile(MGR) == []
    assert killed == ["K-post-merge"] and _closes()[-1] == _closed("K-post-merge", reason="finished")


def test_a_report_before_the_last_output_does_not_finish_post_merge(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                     killed, clock):
    sid = _at_post_merge(monkeypatch, tmp_path)
    _done(sid, "waiting for the merge")
    clock["t"] += 5
    artifact_put_impl("K", "summary", "post-merge", "merged", "complete", sid, scope="output")
    assert pr.reconcile(MGR) == [] and killed == []
    assert [e for e in _of("handed_off") if e["sid"] == sid] == []
    clock["t"] += 5
    _done(sid, "merged #7")
    assert pr.reconcile(MGR) == [] and killed == ["K-post-merge"]


def test_a_pr_holder_with_complete_outputs_and_a_report_is_left_alone(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                       killed, clock):
    _at_pr_with_a_pr_url(monkeypatch, tmp_path)
    clock["t"] += 5
    _done("sid-K-pr", "PR opened")
    open_pr = json.dumps({"state": "OPEN", "mergedAt": None})
    assert pr.reconcile(MGR, run_gh=lambda argv: open_pr) == []
    assert "K-pr" not in killed and _fold().tokens["t1"].node == "pr"


def test_an_open_gate_holds_a_released_session_and_its_verdict_releases_it(fresh_orchestrator_dir, tmp_path,
                                                                            monkeypatch, killed, clock):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "task", override=True, reason="move on"))
    clock["t"] += 5
    _done(sid, "PARKED")
    monkeypatch.setattr(mcp_server, "_gate_state", lambda task_key, stamps=None, index=None: {
        "state": "open", "node": "design", "round": 1, "pid": 1, "out": "/o", "url": None, "relaunches": []})
    assert pr.reconcile(MGR, now=_later(400)) == [] and killed == []
    monkeypatch.setattr(mcp_server, "_gate_state", lambda task_key, stamps=None, index=None: None)
    assert pr.reconcile(MGR, now=_later(400)) == []
    assert killed == ["K-design"] and _closes() == [_closed("K-design", reason="done")]


def test_a_pending_question_holds_a_released_session_and_its_answer_releases_it(fresh_orchestrator_dir, tmp_path,
                                                                                 monkeypatch, killed, clock):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "task", override=True, reason="move on"))
    qid = mcp_server._write_question(sid, "K-design", "which branch?", parent_manager_name="mgr", task_key="K")
    clock["t"] += 5
    _done(sid, "asked")
    assert pr.reconcile(MGR, now=_later(400)) == [] and killed == []
    mcp_server.answer_question_impl(qid, "main")
    assert pr.reconcile(MGR, now=_later(400)) == []
    assert killed == ["K-design"] and _closes() == [_closed("K-design", reason="done")]


def test_his_attachment_holds_a_released_session_until_the_flag_drops(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                      killed, clock):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    clock["t"] += 5
    _done(sid)
    active = state.read_json(paths.ACTIVE / f"{sid}.json")
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {**active, "human_attached_at": time.time()})
    assert pr.reconcile(MGR, now=_later(10)) == [] and killed == []
    assert pr.reconcile(MGR, now=_later(HUMAN_ATTACHED_TTL_SEC - 1)) == [] and killed == []
    assert pr.reconcile(MGR, now=_later(HUMAN_ATTACHED_TTL_SEC + 1)) == []
    assert killed == ["K-design"] and _closes() == [_closed("K-design", reason="done")]


def test_a_held_finished_holder_gets_no_stamp_and_can_still_publish(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                     killed, clock):
    sid = _at_post_merge(monkeypatch, tmp_path)
    artifact_put_impl("K", "summary", "post-merge", "merged", "complete", sid, scope="output")
    clock["t"] += 5
    _done(sid, "merged")
    active = state.read_json(paths.ACTIVE / f"{sid}.json")
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {**active, "human_attached_at": time.time()})
    assert pr.reconcile(MGR) == [] and killed == []
    assert "handed_off_at" not in state.read_json(paths.assignment_path(sid))
    assert artifact_put_impl("K", "summary", "thread", "late note", "complete", sid, scope="output",
                             overwrite=True)["ok"]


def test_subworkers_are_closed_before_their_node(fresh_orchestrator_dir, tmp_path, monkeypatch, killed, clock):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    register_self_impl("sid-child", "worker", "K-design-reviewer", "/w", "", pid=None, parent_manager_name="mgr",
                       parent_sid=sid)
    child = state.read_json(paths.ACTIVE / "sid-child.json")
    state.write_json_atomic(paths.ACTIVE / "sid-child.json", {**child, "kind": "subworker", "parent_name": "K-design"})
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    clock["t"] += 5
    _done(sid)
    assert pr.reconcile(MGR, now=_later(10)) == []
    assert killed == ["K-design-reviewer", "K-design"]


def test_a_pass_that_spawns_still_closes_a_session_released_before_it(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                       killed, clock):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["no_register"])
    r = asyncio.run(pipeline_advance_impl("m1", "K", "task", override=True, reason="jump"))
    assert r["state"] == "pending" and _of("handed_off")[-1]["sid"] == sid
    for p in paths.ASSIGNMENTS_PENDING.glob("*.json"):
        p.unlink()
    clock["t"] += 5
    _done(sid)
    calls = _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR, now=_later(400)) == []
    assert len(calls) == 1 and _reconciled() == ["sid-K-task-1-r2"]
    assert killed == ["K-design"] and _closes() == [_closed("K-design", reason="done")]


def test_a_work_stays_in_the_reconcilers_keys_while_a_released_session_lives(fresh_orchestrator_dir, tmp_path,
                                                                              monkeypatch, killed, clock):
    sid = _at_post_merge(monkeypatch, tmp_path)
    artifact_put_impl("K", "summary", "post-merge", "merged", "complete", sid, scope="output")
    (paths.ACTIVE / f"{sid}.json").unlink()
    assert "K" in pr._reconcilable_task_keys() and not pr.reconcilable("K")
    assert pr.reconcile(MGR, now=_later(400)) == []
    assert killed == ["K-design"] and _closes() == [_closed("K-design", reason="grace")]
    (paths.ACTIVE / "sid-K-design.json").unlink()
    assert "K" not in pr._reconcilable_task_keys()


@pytest.mark.parametrize("via", ["design", "post-merge"])
def test_a_reentered_post_merge_is_left_alone(fresh_orchestrator_dir, tmp_path, monkeypatch, killed, clock, via):
    _started_at_design(monkeypatch, tmp_path, schema_text=BACK_FROM_POST_MERGE)
    _fake_registration(monkeypatch, ["registered"])
    first = asyncio.run(pipeline_advance_impl("m1", "K", "post-merge", override=True, reason="jump"))["next"]["sid"]
    artifact_put_impl("K", "summary", "post-merge", "partial: waiting", "complete", first, scope="output")
    clock["t"] += 5
    _fake_registration(monkeypatch, ["registered"])
    if via == "design":
        asyncio.run(pipeline_advance_impl("m1", "K", "design", override=True, reason="again"))
        clock["t"] += 5
        _fake_registration(monkeypatch, ["registered"])
    second = asyncio.run(pipeline_advance_impl("m1", "K", "post-merge", override=True, reason="forward"))["next"]["sid"]
    clock["t"] += 5
    _done(second, "waiting for the merge")
    assert pr.reconcile(MGR) == []
    assert "K-post-merge-2" not in killed and [e for e in _of("handed_off") if e["sid"] == second] == []
    assert artifact_put_impl("K", "summary", "post-merge", "merged", "complete", second, scope="output",
                             overwrite=True)["ok"]
    clock["t"] += 5
    _done(second, "merged #7")
    assert pr.reconcile(MGR, now=_later(700)) == [] and "K-post-merge-2" not in killed


@pytest.mark.parametrize("branch", ["finished", "done"])
def test_a_session_tasked_after_its_report_is_not_closed_on_that_report(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                       killed, clock, branch):
    if branch == "finished":
        sid, name = _at_post_merge(monkeypatch, tmp_path), "K-post-merge"
        artifact_put_impl("K", "summary", "post-merge", "merged", "complete", sid, scope="output")
    else:
        (sid, _), name = _started_at_design(monkeypatch, tmp_path), "K-design"
        _complete_design_outputs(sid)
        _fake_registration(monkeypatch, ["registered"])
        asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    clock["t"] += 5
    _done(sid, "report")
    clock["t"] += 5
    active = state.read_json(paths.ACTIVE / f"{sid}.json")
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {**active, "tasked_at": time.time(), "state": "processing"})
    assert pr.reconcile(MGR, now=_later(10)) == [] and name not in killed
    clock["t"] += 5
    _done(sid, "answered")
    assert pr.reconcile(MGR, now=_later(20)) == [] and name in killed


def test_resume_of_a_closed_finished_terminal_node_says_it_finished(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                    killed, clock):
    sid = _at_post_merge(monkeypatch, tmp_path)
    artifact_put_impl("K", "summary", "post-merge", "merged", "complete", sid, scope="output")
    clock["t"] += 5
    _done(sid, "merged #7")
    assert pr.reconcile(MGR) == [] and killed == ["K-post-merge"]
    _close_session(sid, "K-post-merge")
    tabs = _fake_resume_tab(monkeypatch)
    with pytest.raises(ValueError, match=r"'K-post-merge' finished post-merge of K") as err:
        asyncio.run(resume_worker_impl("K-post-merge"))
    assert "token has left" not in str(err.value) and "new work" in str(err.value) and tabs == []


SORTER_ONLY = SCHEMA.split("[nodes.task]")[0].replace('outputs = ["context.ask", "design.hld", "plan.tasks"]',
                                                       'outputs = ["context.ask", "design.hld"]')


def test_a_sorter_whose_ask_the_engine_wrote_before_its_commit_still_finishes(fresh_orchestrator_dir, tmp_path,
                                                                              monkeypatch, killed):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=SORTER_ONLY)
    artifact_put_impl("K", "design", "hld", "sorted", "complete", sid, scope="output")
    _done(sid, "sorted")
    assert pr.reconcile(MGR) == []
    assert killed == ["K-design"] and _closes()[-1] == _closed("K-design", reason="finished")


def test_a_restarted_post_merge_holder_still_finishes(fresh_orchestrator_dir, tmp_path, monkeypatch, killed, clock):
    sid = _at_post_merge(monkeypatch, tmp_path)
    (paths.ACTIVE / f"{sid}.json").unlink()
    _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR) == [] and _reconciled() == ["sid-K-post-merge-r2"]
    clock["t"] += 5
    artifact_put_impl("K", "summary", "post-merge", "merged", "complete", "sid-K-post-merge-r2", scope="output")
    clock["t"] += 5
    _done("sid-K-post-merge-r2", "merged #7")
    assert pr.reconcile(MGR, now=_later(700)) == []
    assert killed[-1] == "K-post-merge-r2" and _closes()[-1] == _closed("K-post-merge-r2", reason="finished")


def test_an_attached_subworker_holds_its_released_node_until_its_flag_drops(fresh_orchestrator_dir, tmp_path,
                                                                           monkeypatch, killed, clock):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    register_self_impl("sid-child", "worker", "K-design-reviewer", "/w", "", pid=None, parent_manager_name="mgr",
                       parent_sid=sid)
    child = state.read_json(paths.ACTIVE / "sid-child.json")
    state.write_json_atomic(paths.ACTIVE / "sid-child.json", {**child, "kind": "subworker", "parent_name": "K-design",
                                                               "human_attached_at": time.time()})
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    clock["t"] += 5
    _done(sid)
    assert pr.reconcile(MGR, now=_later(10)) == [] and killed == []
    assert pr.reconcile(MGR, now=_later(HUMAN_ATTACHED_TTL_SEC + 1)) == []
    assert killed == ["K-design-reviewer", "K-design"]


def test_a_finished_holder_whose_assignment_is_gone_is_not_closed_unstamped(fresh_orchestrator_dir, tmp_path,
                                                                            monkeypatch, killed, clock):
    sid = _at_post_merge(monkeypatch, tmp_path)
    artifact_put_impl("K", "summary", "post-merge", "merged", "complete", sid, scope="output")
    clock["t"] += 5
    _done(sid, "merged #7")
    paths.assignment_path(sid).unlink()
    assert pr.reconcile(MGR) == [] and killed == []
    assert [e for e in _of("handed_off") if e["sid"] == sid] == []


def test_the_key_scan_reads_the_assignments_once_for_every_finished_work(fresh_orchestrator_dir, tmp_path,
                                                                          monkeypatch, clock):
    sid = _at_post_merge(monkeypatch, tmp_path)
    artifact_put_impl("K", "summary", "post-merge", "merged", "complete", sid, scope="output")
    (paths.ACTIVE / "sid-K-design.json").unlink()
    for key in ("K2", "K3"):
        shutil.copytree(paths.ARTIFACTS / "K", paths.ARTIFACTS / key)
    reads = []
    real = pr._records
    monkeypatch.setattr(pr, "_records", lambda root: reads.append(root) or real(root))
    keys = pr._reconcilable_task_keys()
    assert keys == {"K", "K2", "K3"}
    assert reads.count(paths.ASSIGNMENTS) <= 1


def test_an_attached_idle_holder_is_not_replaced_and_the_approve_trigger_still_fires(fresh_orchestrator_dir, tmp_path,
                                                                                      monkeypatch):
    _at_pr_on_approve(monkeypatch, tmp_path)
    record = state.read_json(paths.ACTIVE / "sid-K-pr.json")
    state.write_json_atomic(paths.ACTIVE / "sid-K-pr.json", {**record, "state": "idle",
                                                             "human_attached_at": time.time()})
    assert pr._replaceable(pr._Claimed(), "K", _fold().tokens["t1"], by_context=True)[2] == "K-pr is human-attached"
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(), "https://github.com/o/c/pull/8": _pr()})
    assert pr.reconcile(MGR, run_gh=gh) == []
    assert _fold().tokens["t1"].node == "post-merge"


def test_an_attached_waiting_holder_is_still_woken_by_the_merge_poll(fresh_orchestrator_dir, tmp_path, monkeypatch):
    real_idle_or_dead = pr._idle_or_dead
    _at_post_merge_waiting(monkeypatch, tmp_path, FIRST, dead=False)
    monkeypatch.setattr(pr, "_idle_or_dead", real_idle_or_dead)
    record = state.read_json(paths.ACTIVE / "sid-K-post-merge.json")
    state.write_json_atomic(paths.ACTIVE / "sid-K-post-merge.json", {**record, "state": "idle",
                                                                     "human_attached_at": time.time()})
    typed = []
    monkeypatch.setattr(mcp_server, "_resolve_live_worker_window", lambda record: "%p1")
    monkeypatch.setattr(mcp_server, "_type_unguarded", lambda wid, text: typed.append(text) or True)
    monkeypatch.setattr(mcp_server, "_capture_text", lambda wid: IDLE_PANE)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED",
                                                        mergeStateStatus="UNKNOWN"),
                     "https://github.com/o/c/pull/8": _pr(headRefOid="fffffff000")})
    assert pr.reconcile(MGR, run_gh=gh) == []
    assert len(typed) == 1 and typed[0].startswith("[TOPIC decision.msg-") and "event: github.pr" in typed[0]
    assert len(_lane_events("TRIGGER_WAKE")) == 1
    monkeypatch.setattr(mcp_server, "_capture_text", lambda wid: BUSY_PANE)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED",
                                                        mergeStateStatus="UNKNOWN"),
                     "https://github.com/o/c/pull/8": _pr(headRefOid="eeeeeee000")})
    assert pr.reconcile(MGR, now=_later(700), run_gh=gh) == []
    assert len(typed) == 1


def test_a_foreign_override_moves_the_work_to_the_overriding_manager(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    _manager("m2", "mgr2")
    _fake_registration(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_advance_impl("m2", "K", "design", override=True, reason="take over"))
    assert _of("token_override")[-1]["manager"] == "mgr2"
    assert _fold().tokens["t1"].manager == "mgr2"
    (paths.ACTIVE / f"{r['next']['sid']}.json").unlink()
    calls = _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR) == [] and calls == []
    assert pr.reconcile({"name": "mgr2", "sid": "m2"}) == [] and _reconciled() == ["sid-K-design-r2"]
    assert calls[0]["env"]["CLAUDE_PARENT_SID"] == "m2"


def test_an_override_whose_stamp_fails_is_still_handed_off_and_reconciled(fresh_orchestrator_dir, tmp_path,
                                                                          monkeypatch, killed):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    real_write = state.write_json_atomic
    broken = {"on": True}

    def write(path, data):
        if broken["on"] and paths.ASSIGNMENTS in Path(path).parents:
            raise OSError("disk full")
        return real_write(path, data)

    monkeypatch.setattr(state, "write_json_atomic", write)
    calls = _fake_registration(monkeypatch, ["registered"])
    with pytest.raises(OSError, match="disk full"):
        asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="jump"))
    broken["on"] = False
    assert calls == []
    assert [e["type"] for e in _events()][-3:] == ["token_override", "advance_requested", "handed_off"]
    assert "handed_off_at" not in state.read_json(paths.assignment_path(sid))
    with pytest.raises(ValueError, match="node handed off"):
        artifact_put_impl("K", "design", "late", "x", "complete", sid, scope="output")
    assert pr.reconcile(MGR, now=_later(400)) == [] and _reconciled() == ["sid-K-pr-r2"]
    assert len(calls) == 1 and killed == ["K-design"] and _closes() == [_closed("K-design")]
    assert pr.reconcile(MGR, now=_later(800)) == []
    assert killed == ["K-design"] and _closes() == [_closed("K-design")]


def test_merge_trigger_polls_gh_and_advances(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_pr_with_a_pr_url(monkeypatch, tmp_path, body="merged in https://github.com/o/r/pull/7 today")
    seen = []

    def gh(argv):
        seen.append(argv)
        return '{"state":"MERGED","mergedAt":"2026-09-23T10:00:00Z"}'

    _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR, run_gh=gh) == []
    assert seen == [["pr", "view", "https://github.com/o/r/pull/7", "--json", "state,mergedAt"]]
    assert _fold().tokens["t1"].node == "post-merge"
    assert _of("trigger_fired")[-1]["evidence"] == "2026-09-23T10:00:00Z"


def test_an_unmerged_pr_pages_nothing_and_polls_once_per_window(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_pr_with_a_pr_url(monkeypatch, tmp_path)
    seen = []

    def gh(argv):
        seen.append(argv)
        return '{"state":"OPEN","mergedAt":null}'

    calls = _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR, run_gh=gh) == []
    assert pr.reconcile(MGR, now=_later(10), run_gh=gh) == []
    assert len(seen) == 2 and _of("trigger_fired") == [] and calls == []
    assert _fold().tokens["t1"].node == "pr"
    assert not any(line.startswith("TRIGGER_") for line in pr.reconcile(MGR, now=_later(700), run_gh=gh))
    assert len(seen) == 4


@pytest.mark.parametrize("reply", [None, "not json", "[1]"])
def test_a_failed_merge_poll_pages_once_per_window_and_never_advances(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                      reply):
    _at_pr_with_a_pr_url(monkeypatch, tmp_path)
    seen = []

    def gh(argv):
        seen.append(argv)
        return reply

    calls = _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR, run_gh=gh) == [POLL_FAILED]
    assert pr.reconcile(MGR, now=_later(10), run_gh=gh) == [] and len(seen) == 1
    assert POLL_FAILED in pr.reconcile(MGR, now=_later(700), run_gh=gh)
    assert len(seen) == 2 and _of("trigger_fired") == [] and calls == [] and _fold().tokens["t1"].node == "pr"


def test_a_failed_poll_stops_the_poll_at_the_first_failed_pr(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path, schema_text=TWO_PRS)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="jump"))
    artifact_put_impl("K", "implement", "repo", "https://github.com/o/r/pull/7", "complete", "sid-K-pr", scope="output")
    artifact_put_impl("K", "implement", "contracts", "https://github.com/o/c/pull/3", "complete", "sid-K-pr",
                      scope="output")
    seen = []

    def gh(argv):
        seen.append(argv[2])
        return None if argv[2].endswith("/pull/7") else '{"mergedAt": "2026-09-23T10:00:00Z"}'

    assert _heads(pr.reconcile(MGR, run_gh=gh)) == ["TRIGGER_POLL_FAILED K https://github.com/o/r/pull/7"]
    assert seen == ["https://github.com/o/r/pull/7"] and _of("trigger_fired") == []


def test_a_failed_poll_is_paged_even_beside_an_unmerged_pr(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path, schema_text=TWO_PRS)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="jump"))
    artifact_put_impl("K", "implement", "repo", "https://github.com/o/r/pull/7", "complete", "sid-K-pr", scope="output")
    artifact_put_impl("K", "implement", "contracts", "https://github.com/o/c/pull/3", "complete", "sid-K-pr",
                      scope="output")
    replies = {"https://github.com/o/r/pull/7": '{"mergedAt": null}', "https://github.com/o/c/pull/3": None}
    assert _heads(pr.reconcile(MGR, run_gh=lambda argv: replies[argv[2]])) == [
        "TRIGGER_POLL_FAILED K https://github.com/o/c/pull/3"]


def test_the_pr_session_is_closed_after_the_merge_trigger_reaches_the_terminal_node(fresh_orchestrator_dir, tmp_path,
                                                                                   monkeypatch, killed):
    _at_pr_with_a_pr_url(monkeypatch, tmp_path)
    _fake_registration(monkeypatch, ["registered"])
    merged = '{"state":"MERGED","mergedAt":"2026-09-23T10:00:00Z"}'
    assert pr.reconcile(MGR, run_gh=lambda argv: merged) == [] and _of("trigger_fired")
    assert pr.reconcile(MGR, now=_later(400)) == [] and "K-pr" in killed
    assert _closed("K-pr") in _closes()


def test_the_merge_trigger_waits_for_every_pr_listed_one_per_line(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_pr_with_a_pr_url(monkeypatch, tmp_path,
                         body="https://github.com/o/r/pull/7\nhttps://github.com/o/c/pull/8\nbranches ...")
    merged = {"https://github.com/o/r/pull/7": "2026-09-25T10:00:00Z", "https://github.com/o/c/pull/8": None}

    def run_gh(args):
        return json.dumps({"state": "MERGED" if merged[args[2]] else "OPEN", "mergedAt": merged[args[2]]})

    _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR, run_gh=run_gh) == []
    assert _fold().tokens["t1"].node == "pr"
    merged["https://github.com/o/c/pull/8"] = "2026-09-25T11:00:00Z"
    assert pr.reconcile(MGR, now=_later(700), run_gh=run_gh) == [] and _fold().tokens["t1"].node == "post-merge"


def test_the_merge_trigger_waits_for_every_pr_and_cites_the_last_merge(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path, schema_text=TWO_PRS)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="jump"))
    artifact_put_impl("K", "implement", "repo", "https://github.com/o/r/pull/7 after https://github.com/o/r/pull/9",
                      "complete", "sid-K-pr", scope="output")
    artifact_put_impl("K", "implement", "contracts", "https://github.com/o/c/pull/3", "complete", "sid-K-pr",
                      scope="output")
    replies = {"https://github.com/o/r/pull/7": '{"mergedAt": "2026-09-23T10:00:00Z"}',
               "https://github.com/o/c/pull/3": '{"mergedAt": null}'}
    seen = []

    def gh(argv):
        seen.append(argv[2])
        return replies[argv[2]]

    calls = _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR, run_gh=gh) == []
    assert sorted(seen) == ["https://github.com/o/c/pull/3", "https://github.com/o/c/pull/3",
                            "https://github.com/o/r/pull/7", "https://github.com/o/r/pull/7"]
    assert _of("trigger_fired") == [] and calls == []
    replies["https://github.com/o/c/pull/3"] = '{"mergedAt": "2026-09-24T08:00:00Z"}'
    assert pr.reconcile(MGR, now=_later(700), run_gh=gh) == [] and _fold().tokens["t1"].node == "post-merge"
    assert _of("trigger_fired")[-1]["evidence"] == "2026-09-24T08:00:00Z"


def test_the_merge_trigger_waits_for_every_declared_output_of_the_node(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path, schema_text=TWO_PRS)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="jump"))
    artifact_put_impl("K", "implement", "repo", "https://github.com/o/r/pull/7", "complete", "sid-K-pr", scope="output")
    artifact_put_impl("K", "implement", "contracts", "https://github.com/o/c/pull/3", "partial", "sid-K-pr",
                      scope="output")
    seen = []

    def gh(argv):
        seen.append(argv[2])
        return '{"mergedAt": "2026-09-23T10:00:00Z"}'

    calls = _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR, run_gh=gh) == [] and seen == [] and calls == []
    artifact_put_impl("K", "implement", "contracts", "no PR: contracts unchanged", "complete", "sid-K-pr",
                      scope="output", overwrite=True)
    assert pr.reconcile(MGR, now=_later(10), run_gh=gh) == [] and _fold().tokens["t1"].node == "post-merge"
    assert seen == ["https://github.com/o/r/pull/7"]


def test_merge_trigger_without_a_pr_url_never_calls_gh(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_pr_with_a_pr_url(monkeypatch, tmp_path, body="no link yet")
    seen = []
    assert pr.reconcile(MGR, run_gh=lambda argv: seen.append(argv)) == [] and seen == []


def test_the_default_gh_runs_in_the_works_directory(fresh_orchestrator_dir, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _at_pr_with_a_pr_url(monkeypatch, tmp_path, cwd=str(repo))
    runs = []

    def fake_run(argv, **kw):
        runs.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, stdout='{"mergedAt": null}', stderr="")

    monkeypatch.setattr("dockwright.gh_cli.subprocess.run", fake_run)
    assert pr.reconcile(MGR) == []
    assert runs[0][0] == ["gh", "pr", "view", "https://github.com/o/r/pull/7", "--json", "state,mergedAt"]
    assert runs[0][1]["cwd"] == str(repo) and runs[0][1]["timeout"] == 30 and runs[0][1]["env"] is None


def test_the_default_gh_takes_the_owner_token_and_no_cwd_once_gh_is_configured(fresh_orchestrator_dir, tmp_path,
                                                                              monkeypatch):
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text('[gh]\n[gh.logins]\no = "me"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    _at_pr_with_a_pr_url(monkeypatch, tmp_path, cwd="/Users/x/projects/work/worker")
    runs = []

    def fake_run(argv, **kw):
        runs.append((argv, kw))
        if argv[1:3] == ["auth", "token"]:
            return subprocess.CompletedProcess(argv, 0, stdout="t-me\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout='{"mergedAt": null}', stderr="")

    monkeypatch.setattr("dockwright.gh_cli.subprocess.run", fake_run)
    assert pr.reconcile(MGR) == []
    view = [kw for argv, kw in runs if argv[1:3] == ["pr", "view"]]
    assert view and view[0]["cwd"] is None and view[0]["env"]["GH_TOKEN"] == "t-me"


def test_reconcile_emits_through_the_callable_it_is_given(fresh_orchestrator_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pr, "_reconcilable_task_keys", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    got = []
    lines = pr.reconcile(MGR, emit=got.append)
    assert got == lines and len(got) == 1 and got[0].startswith("RECONCILE_ERROR *")
    assert capsys.readouterr().out == ""


def _pr(**fields):
    base = {"state": "OPEN", "mergedAt": None, "isDraft": False, "reviewDecision": "APPROVED",
            "mergeStateStatus": "CLEAN", "headRefOid": "abc1234def", "statusCheckRollup": [],
            "author": {"login": "me"}, "latestReviews": [], "closedAt": None}
    return {**base, **fields}


def _review(login, at, state="COMMENTED"):
    return {"author": {"login": login}, "state": state, "submittedAt": at}


LATER = "2099-01-01T00:00:00Z"
EARLIER = "2000-01-01T00:00:00Z"


@pytest.mark.parametrize("data,phase", [
    (_pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED"), "merged"),
    (_pr(state="CLOSED"), "closed"),
    (_pr(mergeStateStatus="DIRTY"), "conflict"),
    (_pr(reviewDecision="CHANGES_REQUESTED"), "changes"),
    (_pr(), "ready"),
    (_pr(reviewDecision=""), "ready"),
    (_pr(mergeStateStatus="HAS_HOOKS"), "ready"),
    (_pr(mergeStateStatus="UNSTABLE"), "ready"),
    (_pr(isDraft=True), "open"),
    (_pr(reviewDecision="REVIEW_REQUIRED"), "open"),
    (_pr(mergeStateStatus="BLOCKED"), "open"),
    (_pr(mergeStateStatus="BEHIND"), "open"),
    (_pr(mergeStateStatus="UNKNOWN"), "open"),
])
def test_the_pr_phase(data, phase):
    assert pr._phase(data) == phase


@pytest.mark.parametrize("rollup,summary", [
    ([], "none"),
    ([{"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": ""}], "pending"),
    ([{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}], "passed"),
    ([{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
      {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": ""}], "pending"),
    ([{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SKIPPED"},
      {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "TIMED_OUT"}], "failed"),
    ([{"__typename": "StatusContext", "state": "PENDING"}], "pending"),
    ([{"__typename": "StatusContext", "state": "ERROR"}], "failed"),
    ([{"__typename": "StatusContext", "state": "SUCCESS"}], "passed"),
])
def test_the_check_summary(rollup, summary):
    token = pr._checks(rollup)
    assert token.split("/")[0] == summary and " " not in token


GATE = {"__typename": "CheckRun", "name": "review / Gate", "status": "COMPLETED", "conclusion": "SUCCESS",
        "startedAt": "2026-11-04T05:29:40Z", "completedAt": "2026-11-04T05:29:45Z", "workflowName": "review"}
TESTS = {"__typename": "CheckRun", "name": "service / Test", "status": "COMPLETED", "conclusion": "SUCCESS",
         "startedAt": "2026-11-04T05:29:40Z", "completedAt": "2026-11-04T05:34:01Z", "workflowName": "service"}
GATE_AGAIN = {**GATE, "startedAt": "2026-11-04T05:40:32Z", "completedAt": "2026-11-04T05:40:36Z"}
QUEUED = {**TESTS, "status": "QUEUED", "conclusion": "", "startedAt": "2026-11-04T05:41:00Z", "completedAt": None}
RUNNING = {**QUEUED, "status": "IN_PROGRESS"}
CLA = {"__typename": "StatusContext", "context": "CLA", "state": "SUCCESS", "startedAt": "2026-11-04T05:29:40Z",
       "targetUrl": ""}


@pytest.mark.parametrize("before,after", [
    ([GATE, TESTS], [GATE, TESTS, GATE_AGAIN]),
    ([GATE, TESTS], [GATE_AGAIN, TESTS]),
    ([CLA], [{**CLA, "startedAt": "2026-11-04T05:40:36Z"}]),
    ([GATE, QUEUED], [GATE, QUEUED, GATE_AGAIN]),
])
def test_a_check_finished_between_polls_changes_the_check_token(before, after):
    assert pr._checks(before).split("/")[0] == pr._checks(after).split("/")[0]
    assert pr._checks(before) != pr._checks(after)


@pytest.mark.parametrize("before,after", [
    ([GATE, TESTS], [TESTS, GATE]),
    ([GATE, QUEUED], [GATE, RUNNING]),
])
def test_the_check_token_ignores_the_rollup_order_and_unfinished_runs(before, after):
    assert pr._checks(before) == pr._checks(after)


def test_the_observation_line_is_one_compact_line_per_pr():
    line = pr._pr_line("https://github.com/o/r/pull/7", _pr(reviewDecision=""))
    assert line == "o/r#7 ready abc1234 - CLEAN none -"


def test_ten_prs_observe_under_the_event_text_budget():
    urls = [f"https://github.com/owner-name/repo-name-{i}/pull/{100 + i}" for i in range(10)]
    runs = [{**GATE, "name": f"job {n}", "conclusion": "FAILURE"} for n in range(150)]
    lines = [pr._pr_line(u, _pr(mergeStateStatus="UNSTABLE", latestReviews=[_review("reviewer-a", LATER)],
                                statusCheckRollup=runs)) for u in urls]
    text = "\n".join(lines)
    assert ms._fit_event_text(text) == text


def test_an_unknown_merge_status_is_reread_once_then_the_last_line_is_kept(monkeypatch):
    answers = iter([json.dumps(_pr(mergeStateStatus="UNKNOWN")), json.dumps(_pr(mergeStateStatus="UNKNOWN"))])
    slept = []
    monkeypatch.setattr(pr, "_sleep", slept.append)
    previous = {"o/r#7": "o/r#7 ready abc1234 APPROVED CLEAN passed"}
    lines, failed, _ = pr._observe(lambda argv: next(answers), ["https://github.com/o/r/pull/7"], previous)
    assert failed is None and slept == [pr.UNKNOWN_RETRY_SEC]
    assert lines == ["o/r#7 ready abc1234 APPROVED CLEAN passed -"]


def test_an_unknown_merge_status_with_no_earlier_line_reads_open(monkeypatch):
    monkeypatch.setattr(pr, "_sleep", lambda s: None)
    lines, failed, _ = pr._observe(lambda argv: json.dumps(_pr(mergeStateStatus="UNKNOWN")),
                                ["https://github.com/o/r/pull/7"], {})
    assert failed is None and lines == ["o/r#7 open abc1234 APPROVED - none -"]


def test_a_closed_pr_with_an_unknown_merge_status_reads_closed_without_a_retry(monkeypatch):
    slept = []
    monkeypatch.setattr(pr, "_sleep", slept.append)
    previous = {"o/c#8": "o/c#8 open abc1234 REVIEW_REQUIRED CLEAN none"}
    lines, failed, _ = pr._observe(lambda argv: json.dumps(_pr(state="CLOSED", mergeStateStatus="UNKNOWN", reviewDecision="")),
                                ["https://github.com/o/c/pull/8"], previous)
    assert failed is None and slept == [] and lines == ["o/c#8 closed abc1234 - UNKNOWN none -"]


def test_a_reopened_pr_with_an_unknown_merge_status_does_not_keep_its_closed_phase(monkeypatch):
    monkeypatch.setattr(pr, "_sleep", lambda s: None)
    previous = {"o/r#7": "o/r#7 closed abc1234 - UNKNOWN none -"}
    lines, failed, ends = pr._observe(lambda argv: json.dumps(_pr(mergeStateStatus="UNKNOWN", reviewDecision="")),
                                      ["https://github.com/o/r/pull/7"], previous)
    assert failed is None and lines == ["o/r#7 open abc1234 - - none -"] and ends == {"o/r#7": None}


def test_an_unreadable_pr_names_the_failed_url():
    lines, failed, _ = pr._observe(lambda argv: None, ["https://github.com/o/r/pull/7"], {})
    assert lines is None and failed == "https://github.com/o/r/pull/7"


def test_the_observation_line_ends_with_the_latest_review_not_by_the_author():
    data = _pr(latestReviews=[_review("me", LATER), _review("reviewer-a", "2030-05-02T08:00:00Z"),
                              _review("reviewer-b", "2030-05-01T08:00:00Z", "APPROVED")])
    assert pr._pr_line("https://github.com/o/r/pull/7", data) == \
        "o/r#7 ready abc1234 APPROVED CLEAN none 2030-05-02T08:00:00Z"


def test_an_unknown_merge_status_keeps_the_last_line_with_the_fresh_review(monkeypatch):
    monkeypatch.setattr(pr, "_sleep", lambda s: None)
    unknown = json.dumps(_pr(mergeStateStatus="UNKNOWN", latestReviews=[_review("reviewer-a", LATER)]))
    previous = {"o/r#7": "o/r#7 ready abc1234 APPROVED CLEAN passed -"}
    lines, failed, _ = pr._observe(lambda argv: unknown, ["https://github.com/o/r/pull/7"], previous)
    assert failed is None and lines == [f"o/r#7 ready abc1234 APPROVED CLEAN passed {LATER}"]


APPROVE = SCHEMA.replace('trigger = "merge"', 'trigger = "approve"').replace(
    'outputs = ["summary.post-merge"]', 'outputs = ["summary.post-merge"]\ncheckpoints = ["merge.state"]')
TWO = "https://github.com/o/r/pull/7\nhttps://github.com/o/c/pull/8\n"


def _at_pr_on_approve(monkeypatch, tmp_path, body=TWO):
    _started_at_design(monkeypatch, tmp_path, cwd="/w", schema_text=APPROVE)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="jump"))
    artifact_put_impl("K", "implement", "repo", body, "complete", "sid-K-pr", scope="output")
    _fake_registration(monkeypatch, ["registered"])


def _gh_by_url(answers: dict):
    def gh(argv):
        return json.dumps(answers[argv[2]]) if argv[2] in answers else None
    return gh


def test_the_approve_trigger_fires_when_the_first_unmerged_pr_is_ready(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_pr_on_approve(monkeypatch, tmp_path)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda sid: True)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(), "https://github.com/o/c/pull/8": _pr(reviewDecision="REVIEW_REQUIRED")})
    assert pr.reconcile(MGR, run_gh=gh) == []
    assert _fold().tokens["t1"].node == "post-merge"
    assert _of("trigger_fired")[-1]["evidence"] == "o/r#7 ready abc1234 APPROVED CLEAN none -\no/c#8 open abc1234 REVIEW_REQUIRED CLEAN none -"


def test_the_approve_trigger_fires_on_a_ready_pr_with_finished_checks(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_pr_on_approve(monkeypatch, tmp_path)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda sid: True)
    ready = _pr(statusCheckRollup=[GATE, TESTS])
    gh = _gh_by_url({"https://github.com/o/r/pull/7": ready, "https://github.com/o/c/pull/8": _pr(reviewDecision="REVIEW_REQUIRED")})
    assert pr.reconcile(MGR, run_gh=gh) == []
    assert _fold().tokens["t1"].node == "post-merge"
    assert _of("trigger_fired")[-1]["evidence"].startswith(
        f"o/r#7 ready abc1234 APPROVED CLEAN {pr._checks([GATE, TESTS])} -\n")


def test_the_approve_trigger_waits_while_only_a_later_pr_is_ready(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_pr_on_approve(monkeypatch, tmp_path)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda sid: True)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(reviewDecision="REVIEW_REQUIRED"), "https://github.com/o/c/pull/8": _pr()})
    assert pr.reconcile(MGR, run_gh=gh) == []
    assert _fold().tokens["t1"].node == "pr" and _of("trigger_fired") == []


@pytest.mark.parametrize("second", [_pr(mergedAt=LATER, state="MERGED"), _pr(state="CLOSED", closedAt=LATER),
                                    _pr(state="CLOSED")])
def test_the_approve_trigger_fires_on_a_pr_merged_or_closed_after_the_token_entered(fresh_orchestrator_dir, tmp_path,
                                                                                    monkeypatch, second):
    _at_pr_on_approve(monkeypatch, tmp_path)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda sid: True)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(reviewDecision="REVIEW_REQUIRED"), "https://github.com/o/c/pull/8": second})
    assert pr.reconcile(MGR, run_gh=gh) == []
    assert _fold().tokens["t1"].node == "post-merge"


@pytest.mark.parametrize("second", [_pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED"),
                                    _pr(state="CLOSED", closedAt="2026-10-08T10:00:00Z")])
def test_a_pr_merged_or_closed_before_the_token_entered_the_source_does_not_fire(fresh_orchestrator_dir, tmp_path,
                                                                                 monkeypatch, second):
    _at_pr_on_approve(monkeypatch, tmp_path)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda sid: True)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(reviewDecision="REVIEW_REQUIRED"),
                     "https://github.com/o/c/pull/8": second})
    assert pr.reconcile(MGR, run_gh=gh) == []
    assert _fold().tokens["t1"].node == "pr" and _of("trigger_fired") == []


def test_the_approve_trigger_fires_when_every_pr_has_ended(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_pr_on_approve(monkeypatch, tmp_path)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda sid: True)
    ended = _pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED")
    gh = _gh_by_url({"https://github.com/o/r/pull/7": ended, "https://github.com/o/c/pull/8": ended})
    assert pr.reconcile(MGR, run_gh=gh) == []
    assert _fold().tokens["t1"].node == "post-merge"


def test_a_pr_closed_before_the_entry_does_not_hide_a_ready_one_behind_it(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_pr_on_approve(monkeypatch, tmp_path)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda sid: True)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(state="CLOSED", closedAt="2026-10-08T10:00:00Z"),
                     "https://github.com/o/c/pull/8": _pr()})
    assert pr.reconcile(MGR, run_gh=gh) == []
    assert _fold().tokens["t1"].node == "post-merge"


def test_the_approve_trigger_waits_while_the_source_session_is_in_a_turn(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_pr_on_approve(monkeypatch, tmp_path)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda sid: False)
    assert pr.reconcile(MGR, run_gh=_gh_by_url({"https://github.com/o/r/pull/7": _pr(), "https://github.com/o/c/pull/8": _pr()})) == []
    assert _fold().tokens["t1"].node == "pr"


def test_the_approve_trigger_reads_every_field_once_per_window(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_pr_on_approve(monkeypatch, tmp_path, body="https://github.com/o/r/pull/7\n")
    seen = []

    def gh(argv):
        seen.append(argv)
        return json.dumps(_pr(reviewDecision="REVIEW_REQUIRED"))

    assert pr.reconcile(MGR, run_gh=gh) == [] and pr.reconcile(MGR, run_gh=gh) == []
    assert seen == [["pr", "view", "https://github.com/o/r/pull/7", "--json", pr.PR_FIELDS]]


def test_a_failed_approve_poll_pages_once_per_window_and_never_advances(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_pr_on_approve(monkeypatch, tmp_path, body="https://github.com/o/r/pull/7\n")
    lines = pr.reconcile(MGR, run_gh=lambda argv: None)
    assert _heads(lines) == ["TRIGGER_POLL_FAILED K https://github.com/o/r/pull/7"]
    assert _fold().tokens["t1"].node == "pr"


RELEASE = '''[nodes.release]
role = "SCOUT"
model = "@sonnet"
effort = "xhigh"
brief = "briefs/post-merge.md"
inputs = ["summary.post-merge"]
outputs = ["summary.release"]

[[edges]]
from = "post-merge"
to = "release"
trigger = "status"
status = ["Verified", "category:done"]
exit_checks = [{ type = "artifact", name = "summary.post-merge" }]
[[edges]]
from = "pr"'''
STATUS_EDGE = SCHEMA.replace('[[edges]]\nfrom = "pr"', RELEASE, 1)
STATUS_AFTER_APPROVE = APPROVE.replace('[[edges]]\nfrom = "pr"', RELEASE, 1)
VERIFIED = ("Verified", "indeterminate")
IN_VERIFICATION = ("In Verification", "indeterminate")
STREAK = lambda: failure_streak.load(paths.EVENTS_CURSORS / "tracker" / pr.STATUS_STREAK)


def _at_post_merge_on_status(monkeypatch, tmp_path, summary=True, key_ok=True):
    _started_at_design(monkeypatch, tmp_path, schema_text=STATUS_EDGE)
    _fake_registration(monkeypatch, ["registered"])
    sid = asyncio.run(pipeline_advance_impl("m1", "K", "post-merge", override=True, reason="jump"))["next"]["sid"]
    if summary:
        artifact_put_impl("K", "summary", "post-merge", "merged", "complete", sid, scope="output")
    monkeypatch.setattr(pr, "_idle_or_dead", lambda sid: True)
    if key_ok:
        monkeypatch.setattr(pr, "STATUS_KEY", re.compile(r"^K$"))
    _fake_registration(monkeypatch, ["registered"])
    return sid


def _failing(key):
    raise tracker.SelectorFailed(OSError("down"))


@pytest.mark.parametrize("answer", [VERIFIED, ("Done", "done"), ("Closed", "done")])
def test_the_status_trigger_fires_on_a_name_or_a_category_match(fresh_orchestrator_dir, tmp_path, monkeypatch, answer):
    _at_post_merge_on_status(monkeypatch, tmp_path)
    assert pr.reconcile(MGR, read_status=lambda key: answer) == []
    assert _fold().tokens["t1"].node == "release"
    assert _of("trigger_fired")[-1]["evidence"] == f"tracker status {answer[0]} ({answer[1]})"


def test_the_status_trigger_does_not_fire_on_in_verification(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_on_status(monkeypatch, tmp_path)
    assert pr.reconcile(MGR, read_status=lambda key: IN_VERIFICATION) == []
    assert _fold().tokens["t1"].node == "post-merge" and _of("trigger_fired") == []


def test_the_status_trigger_waits_for_the_exit_check_without_a_refusal_event(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_on_status(monkeypatch, tmp_path, summary=False)
    assert pr.reconcile(MGR, read_status=lambda key: VERIFIED) == []
    assert _fold().tokens["t1"].node == "post-merge" and _of("advance_refused") == []


def test_the_status_trigger_waits_while_the_holder_is_in_a_turn(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_on_status(monkeypatch, tmp_path)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda sid: False)
    assert pr.reconcile(MGR, read_status=lambda key: VERIFIED) == []
    assert _fold().tokens["t1"].node == "post-merge"


def test_the_status_trigger_waits_while_the_work_has_a_pending_question(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid = _at_post_merge_on_status(monkeypatch, tmp_path)
    monkeypatch.setattr(ms, "_oldest_pending_question", lambda s, manager_name=None: {"id": "q1"} if s == sid else None)
    assert pr.reconcile(MGR, read_status=lambda key: VERIFIED) == []
    assert _fold().tokens["t1"].node == "post-merge"


def test_the_status_trigger_reads_once_per_window(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_on_status(monkeypatch, tmp_path)
    seen = []
    read = lambda key: seen.append(key) or IN_VERIFICATION
    assert pr.reconcile(MGR, read_status=read) == [] and pr.reconcile(MGR, now=_later(10), read_status=read) == []
    assert seen == ["K"]
    pr.reconcile(MGR, now=_later(700), read_status=read)
    assert seen == ["K", "K"]


def test_the_status_trigger_never_reads_a_key_that_is_not_a_tracker_key(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_on_status(monkeypatch, tmp_path, key_ok=False)
    seen = []
    assert pr.reconcile(MGR, read_status=lambda key: seen.append(key) or VERIFIED) == []
    assert seen == [] and _fold().tokens["t1"].node == "post-merge"


@pytest.mark.parametrize("key,tracker", [("ABC-123", True), ("AB2-12", True), ("K", False), ("abc-1", False),
                                         ("demo-task", False)])
def test_the_tracker_key_shape_matches_the_topic_links(key, tracker):
    assert bool(pr.STATUS_KEY.match(key)) is tracker


def test_the_status_trigger_rechecks_the_holder_after_the_read(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_on_status(monkeypatch, tmp_path)
    busy = {"on": False}
    monkeypatch.setattr(pr, "_idle_or_dead", lambda sid: not busy["on"])

    def read(key):
        busy["on"] = True
        return VERIFIED

    assert pr.reconcile(MGR, read_status=read) == []
    assert _fold().tokens["t1"].node == "post-merge" and _of("trigger_fired") == []


def test_the_status_trigger_rechecks_the_fold_after_the_read(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_on_status(monkeypatch, tmp_path)

    def read(key):
        for name in ("sid-K-design.json", "sid-K-post-merge.json"):
            (paths.ACTIVE / name).unlink(missing_ok=True)
        mcp_server.pipeline_close_impl("m1", "K", "closed during the read")
        return VERIFIED

    assert pr.reconcile(MGR, read_status=read) == []
    assert _of("trigger_fired") == [] and _of("advance_refused") == []


def test_the_status_trigger_reads_nothing_before_the_exit_check_or_a_busy_holder_passes(fresh_orchestrator_dir,
                                                                                         tmp_path, monkeypatch):
    _at_post_merge_on_status(monkeypatch, tmp_path, summary=False)
    seen = []
    assert pr.reconcile(MGR, read_status=lambda key: seen.append(key) or VERIFIED) == []
    assert seen == []
    artifact_put_impl("K", "summary", "post-merge", "merged", "complete", "sid-K-post-merge", scope="output")
    monkeypatch.setattr(pr, "_idle_or_dead", lambda sid: False)
    assert pr.reconcile(MGR, now=_later(700), read_status=lambda key: seen.append(key) or VERIFIED) == []
    assert seen == [] and _fold().tokens["t1"].node == "post-merge"


def test_a_single_failed_status_read_pages_nothing(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_on_status(monkeypatch, tmp_path)
    assert pr.reconcile(MGR, read_status=_failing) == []
    assert _fold().tokens["t1"].node == "post-merge"


@pytest.mark.parametrize("answer", [None, ("", "")])
def test_a_404_or_an_empty_status_counts_as_a_failed_status_read(fresh_orchestrator_dir, tmp_path, monkeypatch, answer):
    _at_post_merge_on_status(monkeypatch, tmp_path)
    assert pr.reconcile(MGR, read_status=lambda key: answer) == []
    assert _fold().tokens["t1"].node == "post-merge" and _of("trigger_fired") == []
    assert STREAK()["K"]["n"] == 1


def test_the_status_streak_file_is_written_under_its_lock(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_on_status(monkeypatch, tmp_path)
    locked = []
    real = state.file_lock

    def spy(path):
        locked.append(path.name)
        return real(path)

    monkeypatch.setattr(state, "file_lock", spy)
    assert pr.reconcile(MGR, read_status=_failing) == []
    assert pr.STATUS_LOCK in locked and STREAK()["K"]["n"] == 1


def test_a_failed_status_read_pages_once_after_three_failures_over_ten_minutes(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_on_status(monkeypatch, tmp_path)
    awake = {"t": 0.0}
    monkeypatch.setattr(failure_streak, "awake_seconds", lambda: awake["t"])
    monkeypatch.setattr(failure_streak, "wall_seconds", lambda: awake["t"])
    assert pr.reconcile(MGR, read_status=_failing) == []
    awake["t"] = 700.0
    assert pr.reconcile(MGR, now=_later(700), read_status=_failing) == []
    awake["t"] = 1400.0
    lines = pr.reconcile(MGR, now=_later(1400), read_status=_failing)
    assert _heads(lines) == ["TRIGGER_POLL_FAILED K K"]
    assert _fold().tokens["t1"].node == "post-merge"
    awake["t"] = 2100.0
    assert pr.reconcile(MGR, now=_later(2100), read_status=_failing) == []


def test_a_status_read_the_machine_slept_through_counts_nothing(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_on_status(monkeypatch, tmp_path)
    monkeypatch.setattr(failure_streak, "awake_seconds", lambda: 0.0)
    wall = iter([0.0, 100.0])
    monkeypatch.setattr(failure_streak, "wall_seconds", lambda: next(wall, 100.0))
    assert pr.reconcile(MGR, read_status=_failing) == []
    assert STREAK() == {}


def test_a_successful_status_read_clears_the_failure_streak(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_on_status(monkeypatch, tmp_path)
    assert pr.reconcile(MGR, read_status=_failing) == []
    assert STREAK()["K"]["n"] == 1
    assert pr.reconcile(MGR, now=_later(700), read_status=lambda key: IN_VERIFICATION) == []
    assert STREAK() == {}


def test_a_status_edge_keeps_the_pr_wake_of_a_waiting_post_merge(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path, cwd="/w", schema_text=STATUS_AFTER_APPROVE)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="jump"))
    artifact_put_impl("K", "implement", "repo", TWO, "complete", "sid-K-pr", scope="output")
    _fake_registration(monkeypatch, ["registered"])
    monkeypatch.setattr(pr, "_idle_or_dead", lambda sid: True)
    asyncio.run(pipeline_advance_impl("m1", "K", "post-merge", reason=FIRST))
    artifact_put_impl("K", "merge", "state", WAITING, "partial", "sid-K-post-merge", scope="checkpoint")
    artifact_put_impl("K", "summary", "post-merge", "merged", "complete", "sid-K-post-merge", scope="output")
    (paths.ACTIVE / "sid-K-post-merge.json").unlink()
    monkeypatch.setattr(pr, "STATUS_KEY", re.compile(r"^K$"))
    sent = _delivered(monkeypatch)
    seen = []
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED", mergeStateStatus="UNKNOWN"),
                     "https://github.com/o/c/pull/8": _pr(reviewDecision="APPROVED")})
    assert pr.reconcile(MGR, run_gh=gh, read_status=lambda key: seen.append(key) or IN_VERIFICATION) == []
    assert len(sent) == 1 and sent[0][1].startswith("event: github.pr — ") and len(_lane_events("TRIGGER_WAKE")) == 1
    assert seen == ["K"] and _fold().tokens["t1"].node == "post-merge"


ONE = "https://github.com/o/r/pull/7\n"
QUIET = 1900


def _iso(after_sec):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + after_sec))


def _source_parked(monkeypatch, tmp_path, idle=True):
    _at_pr_on_approve(monkeypatch, tmp_path, body=ONE)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda sid: idle)
    return _delivered(monkeypatch)


def _feedback_gh(**fields):
    return _gh_by_url({"https://github.com/o/r/pull/7": _pr(reviewDecision="REVIEW_REQUIRED", **fields)})


@pytest.mark.parametrize("state", ["COMMENTED", "CHANGES_REQUESTED", "APPROVED"])
def test_a_new_review_wakes_the_parked_source_holder_once_its_pr_is_quiet(fresh_orchestrator_dir, tmp_path,
                                                                          monkeypatch, state):
    sent = _source_parked(monkeypatch, tmp_path)
    at = _iso(60)
    assert pr.reconcile(MGR, now=_later(QUIET), run_gh=_feedback_gh(latestReviews=[_review("reviewer-a", at, state)])) == []
    assert len(sent) == 1
    assert sent[0][1] == f"event: github.review — o/r#7 open abc1234 REVIEW_REQUIRED CLEAN none {at}"
    assert sent[0][2] == {"kind": "message", "decided_by": "manager", "via": "reconcile"}
    assert _fold().tokens["t1"].node == "pr" and len(_lane_events("TRIGGER_WAKE")) == 1


def test_reviews_posted_one_by_one_wake_once_after_the_last_is_quiet(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sent = _source_parked(monkeypatch, tmp_path)
    first, last = _iso(60), _iso(1500)
    gh = _feedback_gh(latestReviews=[_review("reviewer-a", first)])
    assert pr.reconcile(MGR, now=_later(1000), run_gh=gh) == [] and sent == []
    gh = _feedback_gh(latestReviews=[_review("reviewer-a", first), _review("reviewer-b", last)])
    assert pr.reconcile(MGR, now=_later(1900), run_gh=gh) == [] and sent == []
    assert pr.reconcile(MGR, now=_later(2600), run_gh=gh) == [] and sent == []
    assert pr.reconcile(MGR, now=_later(3400), run_gh=gh) == [] and len(sent) == 1 and sent[0][1].endswith(last)
    _wake_record(sent[0][1].split(" — ", 1)[1], mark="review")
    assert pr.reconcile(MGR, now=_later(4100), run_gh=gh) == [] and len(sent) == 1


def test_a_review_older_than_the_node_entry_wakes_nothing(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sent = _source_parked(monkeypatch, tmp_path)
    assert pr.reconcile(MGR, run_gh=_feedback_gh(latestReviews=[_review("reviewer-a", EARLIER)])) == [] and sent == []


def test_the_authors_own_review_wakes_nothing(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sent = _source_parked(monkeypatch, tmp_path)
    gh = _feedback_gh(latestReviews=[_review("me", _iso(60))])
    assert pr.reconcile(MGR, now=_later(QUIET), run_gh=gh) == [] and sent == []


def test_a_conflict_wakes_the_source_holder_once(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sent = _source_parked(monkeypatch, tmp_path)
    gh = _feedback_gh(mergeStateStatus="DIRTY")
    assert pr.reconcile(MGR, run_gh=gh) == [] and len(sent) == 1
    assert sent[0][1] == "event: github.conflict — o/r#7 conflict abc1234 REVIEW_REQUIRED DIRTY none -"
    _wake_record(sent[0][1].split(" — ", 1)[1], mark="conflict")
    assert pr.reconcile(MGR, run_gh=gh, now=_later(700)) == [] and len(sent) == 1


def test_a_conflict_on_a_new_head_wakes_the_holder_again(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sent = _source_parked(monkeypatch, tmp_path)
    _wake_record("o/r#7 conflict abc1234 REVIEW_REQUIRED DIRTY none -", mark="conflict")
    gh = _feedback_gh(mergeStateStatus="DIRTY", headRefOid="fff9999000")
    assert pr.reconcile(MGR, run_gh=gh) == [] and len(sent) == 1
    assert sent[0][1] == "event: github.conflict — o/r#7 conflict fff9999 REVIEW_REQUIRED DIRTY none -"


def test_a_conflict_recorded_before_the_node_entry_is_not_the_unknown_fallback(fresh_orchestrator_dir, tmp_path,
                                                                              monkeypatch):
    _started_at_design(monkeypatch, tmp_path, cwd="/w", schema_text=APPROVE)
    _wake_record("o/r#7 conflict abc1234 REVIEW_REQUIRED DIRTY none -", stamp="20261001T000000", mark="conflict")
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="jump"))
    artifact_put_impl("K", "implement", "repo", ONE, "complete", "sid-K-pr", scope="output")
    monkeypatch.setattr(pr, "_idle_or_dead", lambda sid: True)
    monkeypatch.setattr(pr, "_sleep", lambda s: None)
    sent = _delivered(monkeypatch)
    gh = _feedback_gh(mergeStateStatus="UNKNOWN", headRefOid="fff9999000")
    assert pr.reconcile(MGR, run_gh=gh) == [] and sent == []


def test_a_conflict_wakes_at_once_while_a_review_is_inside_its_quiet_window(fresh_orchestrator_dir, tmp_path,
                                                                           monkeypatch):
    sent = _source_parked(monkeypatch, tmp_path)
    at = _iso(60)
    assert pr.reconcile(MGR, run_gh=_feedback_gh(mergeStateStatus="DIRTY", latestReviews=[_review("reviewer-a", at)])) == []
    assert len(sent) == 1 and sent[0][1].endswith(f"conflict abc1234 REVIEW_REQUIRED DIRTY none {at}")


def test_the_last_feedback_record_is_the_baseline(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sent = _source_parked(monkeypatch, tmp_path)
    handled, newer = _iso(60), _iso(120)
    _wake_record(f"o/r#7 open abc1234 REVIEW_REQUIRED CLEAN none {handled}", mark="review")
    gh = _feedback_gh(latestReviews=[_review("reviewer-a", handled)])
    assert pr.reconcile(MGR, now=_later(QUIET), run_gh=gh) == [] and sent == []
    gh = _feedback_gh(latestReviews=[_review("reviewer-a", newer)])
    assert pr.reconcile(MGR, now=_later(QUIET + 700), run_gh=gh) == []
    assert len(sent) == 1 and sent[0][1].endswith(newer)


def test_a_source_holder_in_a_turn_is_not_woken(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sent = _source_parked(monkeypatch, tmp_path, idle=False)
    gh = _feedback_gh(latestReviews=[_review("reviewer-a", _iso(60))])
    assert pr.reconcile(MGR, now=_later(QUIET), run_gh=gh) == [] and sent == []


def test_a_merge_edge_source_is_woken_on_a_new_review(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_pr_with_a_pr_url(monkeypatch, tmp_path)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda sid: True)
    sent = _delivered(monkeypatch)
    review = [_review("reviewer-a", _iso(60))]
    gh = lambda argv: json.dumps(_pr(reviewDecision="REVIEW_REQUIRED", latestReviews=review))
    assert pr.reconcile(MGR, now=_later(QUIET), run_gh=gh) == [] and len(sent) == 1
    assert sent[0][1].startswith("event: github.review — o/r#7 open ") and _fold().tokens["t1"].node == "pr"


def test_the_fire_pass_sends_a_due_review_to_the_new_holder(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sent = _source_parked(monkeypatch, tmp_path)
    at = _iso(60)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(latestReviews=[_review("reviewer-a", at)])})
    assert pr.reconcile(MGR, now=_later(QUIET), run_gh=gh) == []
    assert _fold().tokens["t1"].node == "post-merge"
    assert [s[1] for s in sent] == [f"event: github.pr — o/r#7 ready abc1234 APPROVED CLEAN none {at}"]
    assert _lane_events("TRIGGER_WAKE")[-1]["node"] == "post-merge"


def test_the_fire_pass_holds_a_review_inside_its_quiet_window(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sent = _source_parked(monkeypatch, tmp_path)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(latestReviews=[_review("reviewer-a", _iso(60))])})
    assert pr.reconcile(MGR, run_gh=gh) == []
    assert _fold().tokens["t1"].node == "post-merge" and sent == []


WAITING = "waiting 2026-10-08T12:00:00Z — waiting for approval on o/c#8\nmerged https://github.com/o/r/pull/7 abc1234 by node\n"


def _at_post_merge_waiting(monkeypatch, tmp_path, observation, dead=True, sent_before: str | None = None,
                           entered_ago: float = 0):
    real = time.time
    if entered_ago:
        monkeypatch.setattr(time, "time", lambda: real() - entered_ago)
    _at_pr_on_approve(monkeypatch, tmp_path)
    monkeypatch.setattr(time, "time", real)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda sid: True)
    if sent_before is not None:
        _wake_record(sent_before, mark="review")
    asyncio.run(pipeline_advance_impl("m1", "K", "post-merge", reason=observation))
    artifact_put_impl("K", "merge", "state", WAITING, "partial", "sid-K-post-merge", scope="checkpoint")
    if dead:
        (paths.ACTIVE / "sid-K-post-merge.json").unlink()


def _delivered(monkeypatch):
    sent = []

    async def deliver(task_key, text, **kw):
        sent.append((task_key, text, kw))
        return {"ok": True, "delivered": True, "record": f"decision.msg-{len(sent)}"}

    monkeypatch.setattr("dockwright.topic_message.deliver", deliver)
    return sent


def _wake_record(observation, stamp="20261008T120000", mark="pr", old=False):
    from dockwright import topic_message
    text = f"trigger: {mark} — {observation}" if old else f"event: github.{mark} — {observation}"
    artifact_put_impl("K", "decision", f"msg-{stamp}", topic_message.body(text, "manager", "reconcile"),
                      "complete", "topic:reconcile", scope="output", decided_by="manager")


FIRST = "o/r#7 merged abc1234 APPROVED UNKNOWN none -\no/c#8 open abc1234 REVIEW_REQUIRED CLEAN none -"


@pytest.mark.parametrize("change", [
    _pr(reviewDecision="APPROVED"),
    _pr(reviewDecision="REVIEW_REQUIRED", headRefOid="fffffff000"),
    _pr(reviewDecision="REVIEW_REQUIRED", mergeStateStatus="BLOCKED",
        statusCheckRollup=[{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}]),
    _pr(reviewDecision="REVIEW_REQUIRED", state="CLOSED"),
])
def test_a_changed_observation_wakes_the_waiting_node(fresh_orchestrator_dir, tmp_path, monkeypatch, change):
    _at_post_merge_waiting(monkeypatch, tmp_path, FIRST)
    sent = _delivered(monkeypatch)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED", mergeStateStatus="UNKNOWN"),
                     "https://github.com/o/c/pull/8": change})
    assert pr.reconcile(MGR, run_gh=gh) == []
    assert len(sent) == 1 and sent[0][1].startswith("event: github.pr — o/r#7 merged abc1234 APPROVED UNKNOWN none -\no/c#8 ")
    assert sent[0][2] == {"kind": "message", "decided_by": "manager", "via": "reconcile"}


def test_a_review_alone_wakes_the_waiting_node_once_its_pr_is_quiet(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_waiting(monkeypatch, tmp_path, FIRST)
    sent = _delivered(monkeypatch)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED", mergeStateStatus="UNKNOWN"),
                     "https://github.com/o/c/pull/8": _pr(reviewDecision="REVIEW_REQUIRED",
                                                          latestReviews=[_review("reviewer-a", _iso(60))])})
    assert pr.reconcile(MGR, run_gh=gh) == [] and sent == []
    assert pr.reconcile(MGR, now=_later(QUIET), run_gh=gh) == [] and len(sent) == 1
    assert sent[0][1].startswith("event: github.pr — o/r#7 merged ")


MERGED_7 = _pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED", mergeStateStatus="UNKNOWN")


def _fired_with_review(at):
    return f"o/r#7 merged abc1234 APPROVED UNKNOWN none -\no/c#8 open abc1234 REVIEW_REQUIRED CLEAN none {at}"


def _reviewed_gh(at):
    return _gh_by_url({"https://github.com/o/r/pull/7": MERGED_7,
                       "https://github.com/o/c/pull/8": _pr(reviewDecision="REVIEW_REQUIRED",
                                                            latestReviews=[_review("reviewer-a", at)])})


def test_a_review_the_fire_carried_wakes_the_waiting_target_once_quiet(fresh_orchestrator_dir, tmp_path, monkeypatch):
    at = _iso(60)
    _at_post_merge_waiting(monkeypatch, tmp_path, _fired_with_review(at))
    sent = _delivered(monkeypatch)
    gh = _reviewed_gh(at)
    assert pr.reconcile(MGR, run_gh=gh) == [] and sent == []
    assert pr.reconcile(MGR, now=_later(QUIET), run_gh=gh) == []
    assert [s[1] for s in sent] == [f"event: github.pr — {_fired_with_review(at)}"]


def test_a_review_sent_to_the_source_before_the_fire_wakes_no_target(fresh_orchestrator_dir, tmp_path, monkeypatch):
    at = _iso(60)
    _at_post_merge_waiting(monkeypatch, tmp_path, _fired_with_review(at), sent_before=_fired_with_review(at))
    sent = _delivered(monkeypatch)
    assert pr.reconcile(MGR, now=_later(QUIET), run_gh=_reviewed_gh(at)) == [] and sent == []


def test_a_review_older_than_the_source_entry_in_the_fire_wakes_no_target(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_waiting(monkeypatch, tmp_path, _fired_with_review(EARLIER))
    sent = _delivered(monkeypatch)
    assert pr.reconcile(MGR, now=_later(QUIET), run_gh=_reviewed_gh(EARLIER)) == [] and sent == []


def test_a_review_between_the_source_entry_and_the_fire_wakes_the_target(fresh_orchestrator_dir, tmp_path, monkeypatch):
    at = _iso(-3600)
    _at_post_merge_waiting(monkeypatch, tmp_path, _fired_with_review(at), entered_ago=7200)
    sent = _delivered(monkeypatch)
    assert pr.reconcile(MGR, run_gh=_reviewed_gh(at)) == []
    assert [s[1] for s in sent] == [f"event: github.pr — {_fired_with_review(at)}"]


def test_another_change_wakes_the_waiting_node_at_once_beside_a_fresh_review(fresh_orchestrator_dir, tmp_path,
                                                                            monkeypatch):
    _at_post_merge_waiting(monkeypatch, tmp_path, FIRST)
    sent = _delivered(monkeypatch)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED", mergeStateStatus="UNKNOWN"),
                     "https://github.com/o/c/pull/8": _pr(latestReviews=[_review("reviewer-a", _iso(60))])})
    assert pr.reconcile(MGR, run_gh=gh) == [] and len(sent) == 1


def test_an_observation_recorded_before_the_review_column_wakes_nothing(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_waiting(monkeypatch, tmp_path, "o/r#7 merged abc1234 APPROVED UNKNOWN none\n"
                                                  "o/c#8 open abc1234 REVIEW_REQUIRED CLEAN none")
    sent = _delivered(monkeypatch)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED", mergeStateStatus="UNKNOWN"),
                     "https://github.com/o/c/pull/8": _pr(reviewDecision="REVIEW_REQUIRED")})
    assert pr.reconcile(MGR, run_gh=gh) == [] and sent == []


def test_an_equal_observation_wakes_nothing(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_waiting(monkeypatch, tmp_path, FIRST)
    sent = _delivered(monkeypatch)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED", mergeStateStatus="UNKNOWN"),
                     "https://github.com/o/c/pull/8": _pr(reviewDecision="REVIEW_REQUIRED")})
    assert pr.reconcile(MGR, run_gh=gh) == [] and sent == []


def _checks_waiting(monkeypatch, tmp_path, before, after):
    _at_post_merge_waiting(monkeypatch, tmp_path, FIRST)
    merged = pr._pr_line("https://github.com/o/r/pull/7", MERGED_7)
    _wake_record(f"{merged}\n{pr._pr_line('https://github.com/o/c/pull/8', _pr(statusCheckRollup=before))}")
    sent = _delivered(monkeypatch)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": MERGED_7,
                     "https://github.com/o/c/pull/8": _pr(statusCheckRollup=after)})
    assert pr.reconcile(MGR, run_gh=gh) == []
    return sent, f"{merged}\n{pr._pr_line('https://github.com/o/c/pull/8', _pr(statusCheckRollup=after))}"


@pytest.mark.parametrize("before,after", [([GATE, TESTS], [GATE, TESTS, GATE_AGAIN]),
                                          ([GATE, TESTS], [GATE_AGAIN, TESTS])])
def test_a_check_run_finished_between_polls_wakes_the_waiting_node(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                  before, after):
    sent, observation = _checks_waiting(monkeypatch, tmp_path, before, after)
    assert [s[1] for s in sent] == [f"event: github.pr — {observation}"]


@pytest.mark.parametrize("after", [[GATE, TESTS], [TESTS, GATE]])
def test_the_same_finished_checks_wake_nothing(fresh_orchestrator_dir, tmp_path, monkeypatch, after):
    sent, _ = _checks_waiting(monkeypatch, tmp_path, [GATE, TESTS], after)
    assert sent == []


def test_the_last_wake_record_is_the_comparison_after_the_first_wake(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_waiting(monkeypatch, tmp_path, FIRST)
    _wake_record("o/r#7 merged abc1234 APPROVED UNKNOWN none -\no/c#8 ready abc1234 APPROVED CLEAN none -")
    sent = _delivered(monkeypatch)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED", mergeStateStatus="UNKNOWN"),
                     "https://github.com/o/c/pull/8": _pr()})
    assert pr.reconcile(MGR, run_gh=gh, now=_later(700)) == [] and sent == []


def test_a_newer_trigger_fired_outranks_an_older_wake_record(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path, cwd="/w", schema_text=APPROVE)
    _wake_record("o/r#7 merged abc1234 APPROVED UNKNOWN none -\no/c#8 ready abc1234 APPROVED CLEAN none -",
                 stamp="20261001T000000")
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="jump"))
    artifact_put_impl("K", "implement", "repo", TWO, "complete", "sid-K-pr", scope="output")
    _fake_registration(monkeypatch, ["registered"])
    monkeypatch.setattr(pr, "_idle_or_dead", lambda sid: True)
    asyncio.run(pipeline_advance_impl("m1", "K", "post-merge", reason=FIRST))
    artifact_put_impl("K", "merge", "state", WAITING, "partial", "sid-K-post-merge", scope="checkpoint")
    (paths.ACTIVE / "sid-K-post-merge.json").unlink()
    sent = _delivered(monkeypatch)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED", mergeStateStatus="UNKNOWN"),
                     "https://github.com/o/c/pull/8": _pr(reviewDecision="REVIEW_REQUIRED")})
    assert pr.reconcile(MGR, run_gh=gh) == [] and sent == []


def test_a_truncated_trigger_evidence_wakes_once(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_waiting(monkeypatch, tmp_path, "x" * 1200)
    sent = _delivered(monkeypatch)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED", mergeStateStatus="UNKNOWN"),
                     "https://github.com/o/c/pull/8": _pr(reviewDecision="REVIEW_REQUIRED")})
    assert pr.reconcile(MGR, run_gh=gh) == [] and len(sent) == 1


def test_no_heartbeat_wakes_by_default(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_waiting(monkeypatch, tmp_path, FIRST)
    sent = _delivered(monkeypatch)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED", mergeStateStatus="UNKNOWN"),
                     "https://github.com/o/c/pull/8": _pr(reviewDecision="REVIEW_REQUIRED")})
    assert pr.reconcile(MGR, run_gh=gh, now=_later(21000)) == [] and sent == []
    assert pr.reconcile(MGR, run_gh=gh, now=_later(90000)) == [] and sent == []


def test_a_configured_heartbeat_wakes_the_waiting_node(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_waiting(monkeypatch, tmp_path, FIRST)
    cfg = tmp_path / "hb.toml"
    cfg.write_text("[pipeline]\ntrigger_heartbeat_sec = 21600\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    sent = _delivered(monkeypatch)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED", mergeStateStatus="UNKNOWN"),
                     "https://github.com/o/c/pull/8": _pr(reviewDecision="REVIEW_REQUIRED")})
    assert pr.reconcile(MGR, run_gh=gh, now=_later(21000)) == [] and sent == []
    assert pr.reconcile(MGR, run_gh=gh, now=_later(21700)) == []
    assert len(sent) == 1 and sent[0][1].startswith("event: github.pr — o/r#7 merged ")


def test_an_old_trigger_record_still_counts_as_the_last_observation(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_waiting(monkeypatch, tmp_path, FIRST)
    _wake_record("o/r#7 merged abc1234 APPROVED UNKNOWN none -\no/c#8 ready abc1234 APPROVED CLEAN none -",
                 mark="approve", old=True)
    sent = _delivered(monkeypatch)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED", mergeStateStatus="UNKNOWN"),
                     "https://github.com/o/c/pull/8": _pr()})
    assert pr.reconcile(MGR, run_gh=gh, now=_later(700)) == [] and sent == []


def test_a_failed_poll_on_the_wake_side_names_the_waiting_node(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_waiting(monkeypatch, tmp_path, FIRST)
    lines = pr.reconcile(MGR, run_gh=lambda argv: None)
    assert _heads(lines) == ["TRIGGER_POLL_FAILED K https://github.com/o/r/pull/7"]
    assert "post-merge" in lines[0] and "pipeline_advance" not in lines[0]


def test_each_wake_journals_its_own_lane_event(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_waiting(monkeypatch, tmp_path, FIRST)
    sent = _delivered(monkeypatch)
    answers = {"https://github.com/o/r/pull/7": _pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED", mergeStateStatus="UNKNOWN"),
               "https://github.com/o/c/pull/8": _pr()}
    assert pr.reconcile(MGR, run_gh=_gh_by_url(answers)) == []
    answers["https://github.com/o/c/pull/8"] = _pr(headRefOid="fffffff000")
    assert pr.reconcile(MGR, now=_later(700), run_gh=_gh_by_url(answers)) == []
    assert len(sent) == 2 and len(_lane_events("TRIGGER_WAKE")) == 2


def test_a_waiting_holder_in_a_turn_is_not_woken(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_waiting(monkeypatch, tmp_path, FIRST, dead=False)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda sid: False)
    sent = _delivered(monkeypatch)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED", mergeStateStatus="UNKNOWN"),
                     "https://github.com/o/c/pull/8": _pr()})
    assert pr.reconcile(MGR, run_gh=gh) == [] and sent == []


def test_a_waiting_word_older_than_the_holder_is_not_the_holders_state(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_pr_on_approve(monkeypatch, tmp_path)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda sid: True)
    artifact_put_impl("K", "merge", "state", WAITING, "partial", "sid-K-pr", scope="checkpoint")
    asyncio.run(pipeline_advance_impl("m1", "K", "post-merge", reason=FIRST))
    (paths.ACTIVE / "sid-K-post-merge.json").unlink()
    sent = _delivered(monkeypatch)
    calls = _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR, now=_later(800), run_gh=EQUAL) == [] and sent == []
    assert [c["name"] for c in calls] == ["K-post-merge-r2"] and _of("advance_requested")[-1]["kind"] == "restart"


def test_a_failed_merge_poll_keeps_the_merge_edge_step(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_pr_with_a_pr_url(monkeypatch, tmp_path)
    lines = pr.reconcile(MGR, run_gh=lambda argv: None)
    assert "once every PR has merged" in lines[0] and "reason=<mergedAt>" in lines[0]


def test_a_live_node_without_the_waiting_word_is_not_polled(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_waiting(monkeypatch, tmp_path, FIRST, dead=False)
    artifact_put_impl("K", "merge", "state", "merging 2026-10-08T12:00:00Z\n", "partial", "sid-K-post-merge", scope="checkpoint")
    seen = []
    assert pr.reconcile(MGR, run_gh=lambda argv: seen.append(argv)) == [] and seen == []


def test_a_live_waiting_node_is_polled_and_woken_in_its_pane(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_waiting(monkeypatch, tmp_path, FIRST, dead=False)
    sent = _delivered(monkeypatch)
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED", mergeStateStatus="UNKNOWN"),
                     "https://github.com/o/c/pull/8": _pr()})
    assert pr.reconcile(MGR, run_gh=gh) == [] and len(sent) == 1


EQUAL = _gh_by_url({"https://github.com/o/r/pull/7": _pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED", mergeStateStatus="UNKNOWN"),
                    "https://github.com/o/c/pull/8": _pr(reviewDecision="REVIEW_REQUIRED")})


def test_a_dead_waiting_holder_is_neither_paged_nor_restarted(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_waiting(monkeypatch, tmp_path, FIRST)
    calls = _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR, now=_later(800), run_gh=EQUAL) == []
    assert calls == [] and _of("advance_requested")[-1].get("kind") != "restart"


def test_a_dead_holder_without_the_waiting_word_is_restarted_as_today(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_waiting(monkeypatch, tmp_path, FIRST)
    artifact_put_impl("K", "merge", "state", "merging 2026-10-08T12:00:00Z\n", "partial", "sid-K-post-merge", scope="checkpoint")
    calls = _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR, now=_later(800), run_gh=EQUAL) == []
    assert [c["name"] for c in calls] == ["K-post-merge-r2"] and _of("advance_requested")[-1]["kind"] == "restart"


def test_a_wake_restarts_a_dead_waiting_holder_with_no_resumable_session(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_waiting(monkeypatch, tmp_path, FIRST)
    calls = _fake_registration(monkeypatch, ["registered"])
    token = _fold().tokens["t1"]
    assert asyncio.run(pr.recover_for_message("K", token)) == {"route": "restart", "ok": True, "reason": None}
    assert [c["name"] for c in calls] == ["K-post-merge-r2"]


def test_an_override_without_a_manager_keeps_the_start_manager_as_owner(fresh_orchestrator_dir, tmp_path,
                                                                      monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    for event in ({"type": "token_override", "token_id": "t1", "from": "design", "to": "pr", "index": 1,
                   "reason": "an engine without the manager field", "actor_sid": "m1"},
                  {"type": "advance_requested", "token_id": "t1", "request_id": "rq-old",
                   "edge": {"from": "design", "to": "pr"}, "index_to": 1, "kind": "advance", "actor_sid": "m1"}):
        mcp_server._append_task_event("K", event)
    assert _fold().tokens["t1"].manager == "mgr"
    _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile({"name": "other", "sid": "m9"}, now=_later(400)) == []
    assert pr.reconcile(MGR, now=_later(400)) == [] and _reconciled() == ["sid-K-pr-r2"]


def test_the_owner_is_the_folded_manager_of_the_work_token(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    mcp_server._append_task_event("K", {"type": "token_override", "token_id": "t2", "from": None, "to": "design",
                                        "index": 1, "reason": "a second token", "actor_sid": "m9",
                                        "manager": "other"})
    assert _fold().tokens["t1"].manager == "mgr"
    (paths.ACTIVE / f"{sid}.json").unlink()
    _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile({"name": "other", "sid": "m9"}) == []
    assert pr.reconcile(MGR) == [] and _reconciled() == ["sid-K-design-r2"]


def test_reconcile_skips_other_managers_works_and_never_raises(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    (paths.ACTIVE / f"{sid}.json").unlink()
    assert pr.reconcile({"name": "other", "sid": "m9"}) == []
    monkeypatch.setattr("dockwright.mcp_server._spawn_node", None)
    lines = pr.reconcile(MGR)
    assert len(lines) == 1 and lines[0].startswith('RECONCILE_ERROR K → pipeline_status("K") — TypeError: ')


def test_an_unreadable_journal_of_this_managers_work_pages_once_per_window(fresh_orchestrator_dir, tmp_path,
                                                                           monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    _corrupt()
    lines = pr.reconcile(MGR)
    assert len(lines) == 1 and lines[0].startswith('RECONCILE_ERROR K → pipeline_status("K") — ValueError: ')
    assert pr.reconcile(MGR, now=_later(100)) == []
    assert pr.reconcile(MGR, now=_later(700))[0].startswith('RECONCILE_ERROR K → pipeline_status("K") — ValueError: ')


def test_another_managers_unreadable_work_is_skipped_silently(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    d = paths.artifact_ticket_dir("J")
    d.mkdir(parents=True)
    (d / "schema.toml").write_text((paths.artifact_ticket_dir("K") / "schema.toml").read_text())
    state.append_event(paths.artifact_events_path("J"), {"type": "pipeline_started", "token_id": "t1",
                                                         "node": "design", "index": 1, "holder": None, "cwd": "/w",
                                                         "manager": "other"})
    _corrupt("J")
    assert pr.reconcile(MGR) == []
    assert pr.reconcile({"name": "other", "sid": "m9"})[0].startswith('RECONCILE_ERROR J → pipeline_status("J") — ValueError: ')


def test_a_journal_that_cannot_be_read_pages_its_error(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    real_read = Path.read_bytes

    def deny(self):
        if self.name == "events.jsonl":
            raise PermissionError(13, "Permission denied")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", deny)
    lines = pr.reconcile(MGR)
    assert len(lines) == 1 and lines[0].startswith('RECONCILE_ERROR K → pipeline_status("K") — PermissionError: ')
    assert pr.reconcile(MGR, now=_later(100)) == []


def test_a_failed_listing_of_the_works_pages_once_per_window(fresh_orchestrator_dir, monkeypatch, capsys):
    class Unreadable(type(paths.ARTIFACTS)):
        def iterdir(self):
            raise OSError("artifacts unreadable")

    monkeypatch.setattr(paths, "ARTIFACTS", Unreadable(paths.ARTIFACTS))
    capsys.readouterr()
    error = "RECONCILE_ERROR * → dockwright lanes — OSError: artifacts unreadable"
    assert pr.reconcile(MGR) == [error]
    assert capsys.readouterr().out == error + "\n"
    assert pr.reconcile(MGR, now=_later(100)) == []
    assert pr.reconcile(MGR, now=_later(700)) == [error]


def _lane_journal_fails(monkeypatch):
    real = mcp_server._append_task_event

    def append(task_key, event):
        if event.get("type") == "lane_event":
            raise OSError("disk full")
        return real(task_key, event)

    monkeypatch.setattr("dockwright.mcp_server._append_task_event", append)


def test_a_failed_lane_journal_write_pages_the_works_error_once_per_window(fresh_orchestrator_dir, tmp_path,
                                                                          monkeypatch, capsys, killed):
    _design_advanced(monkeypatch, tmp_path, summary=False)
    _lane_journal_fails(monkeypatch)
    capsys.readouterr()
    error = 'RECONCILE_ERROR K → pipeline_status("K") — OSError: disk full'
    assert pr.reconcile(MGR) == [error]
    assert capsys.readouterr().out == error + "\n"
    assert pr.reconcile(MGR, now=_later(400)) == [] and killed == ["K-design"]
    assert _no_summary() == [] and _closes() == []


def test_a_failed_lane_journal_write_leaves_the_later_works_reconciled(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _design_advanced(monkeypatch, tmp_path, summary=False)
    d = paths.artifact_ticket_dir("L")
    d.mkdir(parents=True)
    (d / "schema.toml").write_text((paths.artifact_ticket_dir("K") / "schema.toml").read_text())
    state.append_event(paths.artifact_events_path("L"), {"type": "pipeline_started", "token_id": "t1",
                                                         "node": "design", "index": 1, "holder": None, "cwd": "/w",
                                                         "manager": "mgr"})
    _corrupt("L")
    _lane_journal_fails(monkeypatch)
    lines = pr.reconcile(MGR)
    assert lines[0] == 'RECONCILE_ERROR K → pipeline_status("K") — OSError: disk full'
    assert len(lines) == 2 and lines[1].startswith('RECONCILE_ERROR L → pipeline_status("L") — ValueError: ')


def _close_session(sid, name):
    (paths.ACTIVE / f"{sid}.json").unlink()
    state.write_json_atomic(paths.CLOSED / f"{sid}.json", {"claude_sid": sid, "name": name, "cwd": "/w",
                                                           "closed_reason": "session_end", "closed_at": time.time(),
                                                           "runtime": "claude"})


def _fake_resume_tab(monkeypatch):
    tabs = []

    async def fake_tab(**kw):
        tabs.append(kw)
        register_self_impl(kw["resume_sid"], "worker", kw["name"], kw["cwd"], "", pid=None)
        return ("998", kw["name"])

    monkeypatch.setattr("dockwright.spawner.spawn_worker_tab", fake_tab)
    monkeypatch.setattr("dockwright.mcp_server._has_live_transcript", lambda sid, runtime="claude": True)
    return tabs


@pytest.mark.parametrize("mark", ["stamp", "event"])
def test_resume_refuses_a_node_the_token_has_left(fresh_orchestrator_dir, tmp_path, monkeypatch, mark):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    if mark == "stamp":
        kept = [e for e in _events() if e["type"] != "handed_off"]
        paths.artifact_events_path("K").write_text("".join(json.dumps(e) + "\n" for e in kept))
        assert _of("handed_off") == []
    else:
        assignment = state.read_json(paths.assignment_path(sid))
        assignment.pop("handed_off_at")
        state.write_json_atomic(paths.assignment_path(sid), assignment)
    _close_session(sid, "K-design")
    tabs = _fake_resume_tab(monkeypatch)
    with pytest.raises(ValueError, match=r"'K-design' is a pipeline node of K that the token has left \(handed off\); "
                                         r"resuming it would make a second owner"):
        asyncio.run(resume_worker_impl("K-design"))
    assert tabs == [] and not (paths.ACTIVE / f"{sid}.json").exists()


def test_resume_brings_back_a_closed_holder_of_the_token(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _close_session(sid, "K-design")
    tabs = _fake_resume_tab(monkeypatch)
    r = asyncio.run(resume_worker_impl("K-design", _registration_timeout_sec=1.0, _poll_interval=0.01))
    assert r["ok"] is True and r["sid"] == sid and [t["resume_sid"] for t in tabs] == [sid]
    assert pr.reconcile(MGR, now=_later(400)) == []


def test_a_holder_whose_session_rotates_is_read_alive_and_not_restarted(fresh_orchestrator_dir, tmp_path,
                                                                        monkeypatch, capsys):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    own_pid = os.getpid()
    active = state.read_json(paths.ACTIVE / f"{sid}.json")
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {**active, "pid": own_pid})
    monkeypatch.setattr("dockwright.hooks._resolve_session_pid", lambda: own_pid)
    monkeypatch.delenv("CLAUDE_ASSIGNMENT_ID", raising=False)
    monkeypatch.delenv("CLAUDE_WORKER_RUNTIME", raising=False)
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_ITERM_SID", "998")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "sid-rotated", "cwd": "/w"})))
    hooks.session_start()
    assert not (paths.ACTIVE / f"{sid}.json").exists() and (paths.ACTIVE / "sid-rotated.json").exists()
    calls = _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR) == [] and pr.reconcile(MGR, now=_later(400)) == [] and calls == []
    assert _fold().tokens["t1"].holder == aid
    assert state.read_json(paths.assignment_path("sid-rotated"))["assignment_id"] == aid


def test_a_dead_lane_ends_the_reconcile(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    _corrupt()

    def dead(line):
        raise LaneDead("stdout write failed")

    monkeypatch.setattr("dockwright.pipeline_reconcile.emit", dead)
    with pytest.raises(LaneDead):
        pr.reconcile(MGR)


def test_a_pending_request_with_a_cwd_is_respawned_there_and_moves_the_work(fresh_orchestrator_dir, tmp_path,
                                                                           monkeypatch, killed):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    moved = tmp_path / "worktree"
    moved.mkdir()
    _fake_registration(monkeypatch, ["no_register"])
    assert asyncio.run(pipeline_advance_impl(sid, "K", "task", cwd=str(moved)))["state"] == "pending"
    for p in paths.ASSIGNMENTS_PENDING.glob("*.json"):
        p.unlink()
    assert _fold().tokens["t1"].cwd == "/w"
    calls = _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR, now=_later(400)) == [] and _reconciled() == ["sid-K-task-1-r2"]
    assert calls[0]["cwd"] == str(moved) and _fold().tokens["t1"].cwd == str(moved)


def test_a_held_token_pages_node_held_once_and_its_dead_holder_is_never_restarted(fresh_orchestrator_dir, tmp_path,
                                                                                  monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    asyncio.run(pipeline_advance_impl("m1", "K", "?", reason="wait for the reporter"))
    (paths.ACTIVE / f"{sid}.json").unlink()
    calls = _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR) == [HELD + " — wait for the reporter"]
    assert pr.reconcile(MGR, now=_later(700)) == []
    assert pr.reconcile(MGR, now=_later(3700)) == []
    assert calls == [] and _fold().tokens["t1"].holder == aid
    assert [f["reason"] for f in _lane_events("NODE_HELD")] == ["wait for the reporter"]


def test_each_new_hold_on_the_token_pages_node_held_once_more(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    asyncio.run(pipeline_advance_impl("m1", "K", "?", reason="wait for the reporter"))
    (paths.ACTIVE / f"{sid}.json").unlink()
    calls = _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR) == [HELD + " — wait for the reporter"]
    asyncio.run(pipeline_advance_impl("m1", "K", "?", reason="wait for legal"))
    assert pr.reconcile(MGR, now=_later(60)) == [HELD + " — wait for legal"]
    asyncio.run(pipeline_advance_impl("m1", "K", "?"))
    asyncio.run(pipeline_advance_impl("m1", "K", "?", reason="wait for QA"))
    assert pr.reconcile(MGR, now=_later(120)) == [HELD + " — wait for QA"]
    assert pr.reconcile(MGR, now=_later(3800)) == [] and calls == []
    assert [f["reason"] for f in _lane_events("NODE_HELD")] == ["wait for the reporter", "wait for legal",
                                                                 "wait for QA"]


def test_a_hold_that_lands_after_the_scan_folded_pages_its_own_reason(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    asyncio.run(pipeline_advance_impl("m1", "K", "?", reason="wait for the reporter"))
    (paths.ACTIVE / f"{sid}.json").unlink()
    _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR) == [HELD + " — wait for the reporter"]
    folded = _fold().tokens["t1"]
    asyncio.run(pipeline_advance_impl("m1", "K", "?", reason="wait for legal"))
    out: list = []
    snap = mcp_server._load_snapshot_for("K")
    pr._recover_holder(pr._Claimed(), MGR, "K", snap, folded, time.time(), pr._Pages(MGR["name"]), out,
                       held="wait for the reporter")
    assert [fields["reason"] for _, fields in out] == ["wait for legal"]
    assert [f["reason"] for f in _lane_events("NODE_HELD")] == ["wait for the reporter", "wait for legal"]


def test_a_held_token_whose_holder_idled_out_pages_node_held_not_node_parked(fresh_orchestrator_dir, tmp_path,
                                                                           monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    asyncio.run(pipeline_advance_impl("m1", "K", "?", reason="engineer away"))
    (paths.ACTIVE / f"{sid}.json").unlink()
    state.write_json_atomic(paths.CLOSED / f"{sid}.json", {"claude_sid": sid, "name": "K-design",
                                                           "closed_reason": "idle>7200s", "closed_at": time.time() - 10})
    calls = _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR) == [HELD + " — engineer away"]
    assert calls == [] and _fold().tokens["t1"].holder == aid


def test_a_hold_that_lands_after_the_scan_folded_is_not_restarted_over(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    (paths.ACTIVE / f"{sid}.json").unlink()
    folded = _fold().tokens["t1"]
    asyncio.run(pipeline_advance_impl("m1", "K", "?", reason="engineer away"))
    calls = _fake_registration(monkeypatch, [])
    out: list[str] = []
    snap = mcp_server._load_snapshot_for("K")
    assert pr._recover_holder(pr._Claimed(), MGR, "K", snap, folded, time.time(), pr._Pages(MGR["name"]), out) is False
    assert calls == [] and _fold().held == {"t1": "engineer away"} and _fold().tokens["t1"].holder == aid


def test_a_released_token_whose_holder_is_dead_is_restarted(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    asyncio.run(pipeline_advance_impl("m1", "K", "?", reason="engineer away"))
    asyncio.run(pipeline_advance_impl("m1", "K", "?"))
    (paths.ACTIVE / f"{sid}.json").unlink()
    _fake_registration(monkeypatch, ["registered"])
    pr.reconcile(MGR)
    assert _reconciled() == ["sid-K-design-r2"]


def test_a_silent_line_never_reaches_stdout_and_is_journaled_once(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                   killed, capsys):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid, summary=False)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    capsys.readouterr()
    assert pr.reconcile(MGR, now=_later(400)) == []
    assert pr.reconcile(MGR, now=_later(400 + 86400 + 1)) == []
    assert capsys.readouterr().out == ""
    assert killed == ["K-design", "K-design"]
    assert _lane_events("HANDED_OFF_CLOSED") == [_closed("K-design", aid)]
    assert _lane_events("NODE_NO_SUMMARY") == [{**NO_SUMMARY, "assignment_id": aid}]
    assert _fold().tokens["t1"].node == "task"


def test_a_later_holder_under_the_same_name_journals_its_own_close(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                    killed):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    assert pr.reconcile(MGR, now=_later(400)) == []
    (paths.ACTIVE / f"{sid}.json").unlink()
    register_self_impl("sid-round-2", "worker", "K-design", "/w", "", pid=None, parent_manager_name="mgr",
                       parent_sid="m1")
    mcp_server._append_task_event("K", {"type": "handed_off", "token_id": "t1", "assignment_id": "a-round-2",
                                        "sid": "sid-round-2"})
    assert pr.reconcile(MGR, now=_later(800)) == []
    assert killed == ["K-design", "K-design"]
    assert _lane_events("HANDED_OFF_CLOSED") == [_closed("K-design", aid), _closed("K-design", "a-round-2")]


def test_a_waking_line_is_printed_with_its_step(fresh_orchestrator_dir, tmp_path, monkeypatch, capsys):
    _at_pr_with_a_pr_url(monkeypatch, tmp_path)
    capsys.readouterr()
    assert pr.reconcile(MGR, run_gh=lambda argv: None) == [POLL_FAILED]
    assert capsys.readouterr().out == POLL_FAILED + "\n"
    assert _of("lane_event") == []


DONE_STEP = ('verify per § Verify before relay, then relay; a cut summary: get_worker_summary("K-design"), never cat '
             "the done file; harvest it before any send_manager_to_worker to it")


def _done_lines(capsys):
    capsys.readouterr()
    monitor.run_done_scan(MGR)
    return capsys.readouterr().out.splitlines()


def test_the_done_of_a_node_whose_advance_committed_goes_to_the_journal(fresh_orchestrator_dir, tmp_path,
                                                                         monkeypatch, capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["registered"])
    assert asyncio.run(pipeline_advance_impl(sid, "K", "task"))["state"] == "committed"
    event_id = worker_done_impl(sid, "design done")["event_id"]
    assert _done_lines(capsys) == []
    assert _lane_events("DONE") == [{"worker": "K-design", "event_id": event_id, "advanced": True}]
    assert len(list((paths.DONE / "mgr").glob("*.json"))) == 1
    assert _done_lines(capsys) == [] and len(_lane_events("DONE")) == 1


def test_the_done_of_a_node_whose_advance_is_pending_prints(fresh_orchestrator_dir, tmp_path, monkeypatch, capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["no_register"])
    assert asyncio.run(pipeline_advance_impl(sid, "K", "task"))["state"] == "pending"
    worker_done_impl(sid, "advance pending")
    assert _done_lines(capsys) == [f"DONE K-design → {DONE_STEP} — advance pending"]
    assert _lane_events("DONE") == []


def test_the_done_of_the_token_holder_prints(fresh_orchestrator_dir, tmp_path, monkeypatch, capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    worker_done_impl(sid, "parked at the gate")
    assert _done_lines(capsys) == [f"DONE K-design → {DONE_STEP} — parked at the gate"]
    assert _lane_events("DONE") == []


def test_a_park_that_repeats_the_previous_park_goes_to_the_journal(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                    capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    worker_done_impl(sid, "PARKED — the gate is open\nfirst report")
    assert _done_lines(capsys) == [f"DONE K-design → {DONE_STEP} — PARKED — the gate is open first report"]
    event_id = worker_done_impl(sid, " PARKED —  the gate is open \n")["event_id"]
    assert _done_lines(capsys) == []
    assert _lane_events("DONE") == [{"worker": "K-design", "event_id": event_id, "repeat": True}]
    assert len(list((paths.DONE / "mgr").glob("*.json"))) == 2


def test_a_repeated_park_line_with_more_lines_prints(fresh_orchestrator_dir, tmp_path, monkeypatch, capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    worker_done_impl(sid, "PARKED — x\nA")
    assert _done_lines(capsys) == [f"DONE K-design → {DONE_STEP} — PARKED — x A"]
    worker_done_impl(sid, "PARKED — x\nposted comment 2")
    assert _done_lines(capsys) == [f"DONE K-design → {DONE_STEP} — PARKED — x posted comment 2"]
    assert _lane_events("DONE") == []


def _pane_stamps(sid, **stamps):
    path = paths.ACTIVE / f"{sid}.json"
    record = state.read_json(path)
    record.update(stamps)
    state.write_json_atomic(path, record)


def _he_typed(sid, at):
    _pane_stamps(sid, input_at=at, human_attached_at=at)


def _completed_at(sid, event_id):
    return state.read_json(paths.DONE / "mgr" / f"{sid}-{event_id}.json")["completed_at"]


def test_a_park_with_more_lines_that_answers_his_pane_line_goes_to_the_journal(fresh_orchestrator_dir, tmp_path,
                                                                                monkeypatch, capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    first = worker_done_impl(sid, "PARKED — the gate is open\nfirst report")["event_id"]
    assert _done_lines(capsys) == [f"DONE K-design → {DONE_STEP} — PARKED — the gate is open first report"]
    previous = _completed_at(sid, first)
    _pane_stamps(sid, tasked_at=previous - 1)
    _he_typed(sid, previous + 0.001)
    event_id = worker_done_impl(sid, "PARKED — the gate is open\nanswered his question in the pane")["event_id"]
    assert _done_lines(capsys) == []
    assert _lane_events("DONE") == [{"worker": "K-design", "event_id": event_id, "repeat": True}]
    assert len(list((paths.DONE / "mgr").glob("*.json"))) == 2


def test_a_park_answering_a_relay_and_his_later_line_prints(fresh_orchestrator_dir, tmp_path, monkeypatch, capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    first = worker_done_impl(sid, "PARKED — x\nA")["event_id"]
    assert _done_lines(capsys) == [f"DONE K-design → {DONE_STEP} — PARKED — x A"]
    previous = _completed_at(sid, first)
    _pane_stamps(sid, tasked_at=previous + 0.001)
    _he_typed(sid, previous + 0.002)
    worker_done_impl(sid, "PARKED — x\nanswered the relay and him")
    assert _done_lines(capsys) == [f"DONE K-design → {DONE_STEP} — PARKED — x answered the relay and him"]
    assert _lane_events("DONE") == []


def test_a_park_answering_his_line_queued_before_the_previous_done_goes_to_the_journal(fresh_orchestrator_dir,
                                                                                         tmp_path, monkeypatch,
                                                                                         capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _he_typed(sid, time.time() - 60)
    worker_done_impl(sid, "PARKED — x\nA")
    assert _done_lines(capsys) == [f"DONE K-design → {DONE_STEP} — PARKED — x A"]
    event_id = worker_done_impl(sid, "PARKED — x\nB")["event_id"]
    assert _done_lines(capsys) == []
    assert _lane_events("DONE") == [{"worker": "K-design", "event_id": event_id, "repeat": True}]


def _sweep_done_files(sid):
    spec = importlib.util.spec_from_file_location(
        "preflight_under_lane_test", Path(__file__).resolve().parent.parent / "deploy" / "scripts" / "preflight_cleanup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.DONE, mod.ACTIVE = paths.DONE, paths.ACTIVE
    for done_file in (paths.DONE / "mgr").glob(f"{sid}-*.json"):
        old = time.time() - mod.STALE_DONE_SEC - 60
        os.utime(done_file, (old, old))
    return mod._prune_done(time.time())


def test_a_repeated_park_after_the_sweep_goes_to_the_journal(fresh_orchestrator_dir, tmp_path, monkeypatch, capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    worker_done_impl(sid, "PARKED — the gate is open\nfirst report")
    assert _done_lines(capsys) == [f"DONE K-design → {DONE_STEP} — PARKED — the gate is open first report"]
    assert _sweep_done_files(sid) == 0
    assert len(list((paths.DONE / "mgr").glob(f"{sid}-*.json"))) == 1
    _he_typed(sid, time.time())
    event_id = worker_done_impl(sid, "PARKED — the gate is open\nanswered him after the boot")["event_id"]
    assert _done_lines(capsys) == []
    assert _lane_events("DONE") == [{"worker": "K-design", "event_id": event_id, "repeat": True}]


def test_a_changed_park_line_after_his_pane_line_prints(fresh_orchestrator_dir, tmp_path, monkeypatch, capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    first = worker_done_impl(sid, "PARKED at gate round 1 — x\nA")["event_id"]
    assert _done_lines(capsys) == [f"DONE K-design → {DONE_STEP} — PARKED at gate round 1 — x A"]
    _he_typed(sid, _completed_at(sid, first) + 0.001)
    worker_done_impl(sid, "PARKED at gate round 2 — x\nrebuilt the gate")
    assert _done_lines(capsys) == [f"DONE K-design → {DONE_STEP} — PARKED at gate round 2 — x rebuilt the gate"]
    assert _lane_events("DONE") == []


def test_a_park_whose_first_line_changed_prints(fresh_orchestrator_dir, tmp_path, monkeypatch, capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    worker_done_impl(sid, "PARKED — held\nA")
    assert _done_lines(capsys) == [f"DONE K-design → {DONE_STEP} — PARKED — held A"]
    worker_done_impl(sid, "PARKED — checks pending")
    assert _done_lines(capsys) == [f"DONE K-design → {DONE_STEP} — PARKED — checks pending"]
    assert _lane_events("DONE") == []


def test_a_repeated_first_line_that_is_not_a_park_prints(fresh_orchestrator_dir, tmp_path, monkeypatch, capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    worker_done_impl(sid, "still waiting")
    assert _done_lines(capsys) == [f"DONE K-design → {DONE_STEP} — still waiting"]
    worker_done_impl(sid, "still waiting")
    assert _done_lines(capsys) == [f"DONE K-design → {DONE_STEP} — still waiting"]
    assert _lane_events("DONE") == []


def test_two_repeated_parks_in_one_scan_print_the_earlier_and_journal_the_later(fresh_orchestrator_dir, tmp_path,
                                                                                  monkeypatch, capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    worker_done_impl(sid, "PARKED — x\nfirst")
    event_id = worker_done_impl(sid, "PARKED — x")["event_id"]
    lines = _done_lines(capsys)
    assert len(lines) == 1 and lines[0].endswith("— PARKED — x first")
    assert _lane_events("DONE") == [{"worker": "K-design", "event_id": event_id, "repeat": True}]


def test_a_repeated_park_prints_when_the_journal_cannot_be_written(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                    capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    worker_done_impl(sid, "PARKED — x\nfirst")
    assert _done_lines(capsys) == [f"DONE K-design → {DONE_STEP} — PARKED — x first"]
    worker_done_impl(sid, "PARKED — x")

    def full(path, event):
        raise OSError("disk full")

    monkeypatch.setattr("dockwright.state.append_event", full)
    assert _done_lines(capsys) == [f"DONE K-design → {DONE_STEP} — PARKED — x"]


def test_the_done_of_a_holder_the_manager_overrode_prints(fresh_orchestrator_dir, tmp_path, monkeypatch, capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["registered"])
    assert asyncio.run(pipeline_advance_impl("m1", "K", "task", override=True, reason="redirect"))["state"] == "committed"
    worker_done_impl(sid, "displaced but finished")
    assert _done_lines(capsys) == [f"DONE K-design → {DONE_STEP} — displaced but finished"]
    assert _lane_events("DONE") == []


def test_the_done_of_a_node_whose_own_advance_was_superseded_prints(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                    capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["no_register", "registered"])
    assert asyncio.run(pipeline_advance_impl(sid, "K", "task"))["state"] == "pending"
    assert asyncio.run(pipeline_advance_impl("m1", "K", "task", override=True, reason="redirect"))["state"] == "committed"
    worker_done_impl(sid, "my advance was superseded")
    assert _done_lines(capsys) == [f"DONE K-design → {DONE_STEP} — my advance was superseded"]
    assert _lane_events("DONE") == []


def test_the_done_of_an_advanced_node_prints_when_the_journal_cannot_be_written(fresh_orchestrator_dir, tmp_path,
                                                                                monkeypatch, capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    worker_done_impl(sid, "design done")

    def full(path, event):
        raise OSError("disk full")

    monkeypatch.setattr("dockwright.state.append_event", full)
    assert _done_lines(capsys) == [f"DONE K-design → {DONE_STEP} — design done"]


def test_pipeline_status_text_names_a_journaled_done(fresh_orchestrator_dir, tmp_path, monkeypatch, capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    worker_done_impl(sid, "design done")
    assert _done_lines(capsys) == []
    assert " lane_event DONE K-design" in mcp_server.pipeline_status_impl("K")


def test_reconcile_as_the_daemon_restarts_a_daemon_owned_node_and_leaves_a_managers_work_alone(
        fresh_orchestrator_dir, tmp_path, monkeypatch, killed):
    _setup_schema(monkeypatch, tmp_path)
    monkeypatch.setattr(paths, "DAEMON", tmp_path / "daemon")
    monkeypatch.setattr(paths, "DAEMON_STOP", tmp_path / "daemon-stop")
    paths.DAEMON.mkdir()
    state.write_json_atomic(paths.DAEMON / "alive.json", {"ts": time.time(), "awake": 1.0, "pid": 1})
    _fake_registration(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_start_impl("daemon", "K", "code-ticket", "/w", "ask"))
    _manager()
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_start_impl("m1", "J", "code-ticket", "/w", "ask"))
    dead = r["next"]["sid"]
    (paths.ACTIVE / f"{dead}.json").unlink()
    calls = _fake_registration(monkeypatch, ["registered"])
    got = []
    pr.reconcile({"sid": "daemon", "name": "daemon"}, now=_later(10), emit=got.append)
    assert [c["name"] for c in calls] == ["K-design-r2"] and calls[0]["env"]["CLAUDE_PARENT_MANAGER"] == "daemon"
    assert got == []


def test_the_daemons_reconcile_leaves_the_topic_outbox_to_the_managers(fresh_orchestrator_dir, tmp_path, monkeypatch):
    drained = []
    monkeypatch.setattr(pr, "_drain_topic_outbox", lambda *a, **k: drained.append(a[0]["sid"]))
    monkeypatch.setattr(pr, "_reconcilable_task_keys", lambda: set())
    pr.reconcile({"sid": "daemon", "name": "daemon"}, emit=lambda line: None)
    pr.reconcile(MGR, emit=lambda line: None)
    assert drained == ["m1"]


LEASE = "env:example/svc-a"


def _lease_held_by_another_work(sid_other="sid-other"):
    mcp_server.acquire_worker_slot_impl(claude_sid=sid_other, category=LEASE, max_concurrent=1, task_key="OTHER",
                                        node="ship", wait=False)


def _ship_waits_on_the_lease(sid):
    mcp_server.acquire_worker_slot_impl(claude_sid=sid, category=LEASE, max_concurrent=1, task_key="K", node="design",
                                        wait=False)


def test_a_parked_node_waiting_on_a_lease_is_waiting_not_parked(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _lease_held_by_another_work()
    _ship_waits_on_the_lease(sid)
    (paths.ACTIVE / f"{sid}.json").unlink()
    state.write_json_atomic(paths.CLOSED / f"{sid}.json", {"claude_sid": sid, "name": "K-design",
                                                           "closed_reason": "idle>7200s", "cwd": "/w"})
    calls = _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR) == [] and pr.reconcile(MGR, now=_later(3700)) == []
    assert _fold().tokens["t1"].holder == aid and calls == []


def test_a_freed_lease_wakes_the_idle_waiter_once(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _lease_held_by_another_work()
    _ship_waits_on_the_lease(sid)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda s: True)
    sent = _delivered(monkeypatch)
    assert pr.reconcile(MGR) == [] and sent == []
    mcp_server.release_work_slots("OTHER")
    assert pr.reconcile(MGR) == []
    assert [(k, t) for k, t, _ in sent] == [("K", f"event: lease.free — {LEASE}")]
    assert _lane_events("EVENT_WAKE") == [{"task_key": "K", "node": "design", "record": "decision.msg-1", "events": 1}]
    assert mcp_server.lease_waits("K") == []
    assert pr.reconcile(MGR, now=_later(60)) == [] and len(sent) == 1


def test_a_working_waiter_is_not_woken(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _lease_held_by_another_work()
    _ship_waits_on_the_lease(sid)
    mcp_server.release_work_slots("OTHER")
    monkeypatch.setattr(pr, "_idle_or_dead", lambda s: False)
    sent = _delivered(monkeypatch)
    assert pr.reconcile(MGR) == [] and sent == [] and _lane_events("EVENT_WAKE") == []
    assert [w["node"] for w in mcp_server.lease_waits("K")] == ["design"]


def test_a_waiter_of_a_node_the_token_left_is_dropped_without_a_wake(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    mcp_server.acquire_worker_slot_impl(claude_sid="sid-other", category=LEASE, max_concurrent=1, task_key="OTHER",
                                        wait=False)
    mcp_server.acquire_worker_slot_impl(claude_sid=sid, category=LEASE, max_concurrent=1, task_key="K", node="ship",
                                        wait=False)
    mcp_server.release_work_slots("OTHER")
    monkeypatch.setattr(pr, "_idle_or_dead", lambda s: True)
    sent = _delivered(monkeypatch)
    assert pr.reconcile(MGR) == [] and sent == []
    assert mcp_server.lease_waits("K") == []


def test_an_expired_lease_is_evicted_and_the_waiter_is_woken(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    mcp_server.acquire_worker_slot_impl(claude_sid="sid-other", category=LEASE, max_concurrent=1, task_key="OTHER",
                                        wait=False, ttl_sec=60)
    _ship_waits_on_the_lease(sid)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda s: True)
    sent = _delivered(monkeypatch)
    assert pr.reconcile(MGR) == [] and sent == []
    assert pr.reconcile(MGR, now=_later(61)) == [] and len(sent) == 1
    assert [e["events"] for e in _lane_events("EVENT_WAKE")] == [1]


def test_a_lease_wait_holds_only_the_token_the_wake_serves(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _lease_held_by_another_work()
    _ship_waits_on_the_lease(sid)
    (paths.ACTIVE / f"{sid}.json").unlink()
    state.write_json_atomic(paths.CLOSED / f"{sid}.json", {"claude_sid": sid, "name": "K-design",
                                                           "closed_reason": "idle>7200s", "cwd": "/w"})
    snap = mcp_server._load_snapshot_for("K")
    token = _fold().tokens["t1"]
    import dataclasses
    assert pr._holder_verdict(pr._Claimed(), "K", snap, token, time.time(), None)[0] == "waiting"
    row = dataclasses.replace(token, token_id="t1/1", row="1")
    assert pr._holder_verdict(pr._Claimed(), "K", snap, row, time.time(), None)[0] == "parked"


def test_a_restart_for_a_message_puts_the_marked_line_in_the_new_sessions_inputs(fresh_orchestrator_dir, tmp_path,
                                                                                  monkeypatch):
    _at_post_merge_waiting(monkeypatch, tmp_path, FIRST)
    _fake_registration(monkeypatch, ["registered"])
    token = _fold().tokens["t1"]
    out = asyncio.run(pr.recover_for_message("K", token, message="[TOPIC decision.msg-1 from engineer] hold on"))
    assert out == {"route": "restart", "ok": True, "reason": None}
    request = _of("advance_requested")[-1]
    assert request["kind"] == "restart"
    assert mcp_server._request_message_path("K", request["request_id"]).read_text() == (
        "[TOPIC decision.msg-1 from engineer] hold on")


# the event wake


def _events_on(tmp_path, monkeypatch, env_file=True):
    from dockwright import events  # noqa: F401
    handlers = tmp_path / "h"
    handlers.mkdir(exist_ok=True)
    cfg = tmp_path / "events.toml"
    env = tmp_path / "events.env"
    cfg.write_text(f'[events]\nenabled = true\nhandler_path = ["{handlers}"]\nhandlers = ["author-wait"]\n'
                   f'env_file = "{env}"\n')
    if env_file:
        env.write_text("TRACKER_URL=https://j\nTRACKER_EMAIL=me@x\nTRACKER_API_TOKEN=tok\n")
        env.chmod(0o600)
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    return handlers


def _subscribed(task_key, node, source, match, **kw):
    from dockwright import events
    return events.subscribe(task_key, node, source, match, **kw)


def _event(id="tracker:ABC-1:comment:11", ts=None, source="tracker", kind="comment", match=None, cursor_key="11",
           summary="ABC-1 #11 by A: hi", payload=None):
    from dockwright import events
    e = events.Event(id, time.time() if ts is None else ts, source, kind, match or {"issue": "ABC-1"}, cursor_key,
                     summary, payload or {})
    events.append(e)
    return e


def _subs(task_key="K"):
    from dockwright import events
    return events.subs_for(task_key)


def _unsubscribed(task_key="K"):
    return [(e["sub_id"], e["reason"]) for e in _of("unsubscribed", task_key)]


def _handler_script(handlers, body):
    p = handlers / "author-wait"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return p


def test_an_event_wakes_the_idle_subscribed_holder_with_one_line_and_moves_delivered(fresh_orchestrator_dir, tmp_path,
                                                                                    monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    _events_on(tmp_path, monkeypatch)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda s: True)
    sent = _delivered(monkeypatch)
    sub = _subscribed("K", "design", "tracker", {"issue": "ABC-1"}, after="10")
    e = _event()
    assert pr.reconcile(MGR) == []
    assert sent == [("K", f"event: tracker.comment — ABC-1 #11 by A: hi (sub {sub.sub_id})",
                     {"kind": "message", "decided_by": "manager", "via": "reconcile"})]
    assert _lane_events("EVENT_WAKE") == [{"task_key": "K", "node": "design", "record": "decision.msg-1", "events": 1}]
    assert _subs()[0].delivered == [e.ts, e.id]
    assert [(x["record"], x["sub_ids"], x["event_ids"]) for x in _of("event_delivered")] == [
        ("decision.msg-1", [sub.sub_id], [e.id])]
    assert pr.reconcile(MGR, now=_later(60)) == [] and len(sent) == 1


def test_an_event_logged_before_a_resubscribe_still_reaches_the_new_sub(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    _events_on(tmp_path, monkeypatch)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda s: False)
    sent = _delivered(monkeypatch)
    _subscribed("K", "design", "tracker", {"issue": "ABC-1"}, after="21")
    _event("tracker:ABC-1:comment:22", cursor_key="22", summary="ABC-1 #22 by A: hi")
    assert pr.reconcile(MGR) == [] and sent == []
    time.sleep(0.01)
    new = _subscribed("K", "design", "tracker", {"issue": "ABC-1"}, after="21")
    monkeypatch.setattr(pr, "_idle_or_dead", lambda s: True)
    assert pr.reconcile(MGR, now=_later(5)) == []
    assert [t for _, t, _ in sent] == [f"event: tracker.comment — ABC-1 #22 by A: hi (sub {new.sub_id})"]


def test_two_events_of_one_holder_in_a_pass_make_one_delivery(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    _events_on(tmp_path, monkeypatch)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda s: True)
    sent = _delivered(monkeypatch)
    sub = _subscribed("K", "design", "tracker", {"issue": "ABC-1"}, after="10", deadline=time.time() - 1)
    _event(ts=time.time() - 2)
    _event(f"time:{sub.sub_id}:deadline", source="time", kind="deadline", match={"sub_id": sub.sub_id}, cursor_key="",
           summary="deadline passed")
    assert pr.reconcile(MGR) == []
    assert [t for _, t, _ in sent] == [f"event: tracker.comment — ABC-1 #11 by A: hi (sub {sub.sub_id}) · "
                                       f"event: time.deadline — deadline passed (sub {sub.sub_id})"]
    assert _lane_events("EVENT_WAKE") == [{"task_key": "K", "node": "design", "record": "decision.msg-1", "events": 2}]


def test_a_lease_line_rides_the_same_batch(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _events_on(tmp_path, monkeypatch)
    _lease_held_by_another_work()
    _ship_waits_on_the_lease(sid)
    mcp_server.release_work_slots("OTHER")
    monkeypatch.setattr(pr, "_idle_or_dead", lambda s: True)
    sent = _delivered(monkeypatch)
    sub = _subscribed("K", "design", "tracker", {"issue": "ABC-1"}, after="10")
    _event()
    assert pr.reconcile(MGR) == []
    assert [t for _, t, _ in sent] == [f"event: lease.free — {LEASE} · event: tracker.comment — ABC-1 #11 by A: hi "
                                       f"(sub {sub.sub_id})"]
    assert mcp_server.lease_waits("K") == [] and _subs()[0].delivered is not None
    assert _lane_events("EVENT_WAKE") == [{"task_key": "K", "node": "design", "record": "decision.msg-1", "events": 2}]


def test_a_deadline_or_a_deploy_job_ends_its_subscription(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    _events_on(tmp_path, monkeypatch)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda s: True)
    sent = _delivered(monkeypatch)
    timed = _subscribed("K", "design", "tracker", {"issue": "ABC-2"}, after="10", deadline=time.time() - 1)
    deploy = _subscribed("K", "design", "deploy", {"repo": "o/r", "env": "staging", "sha": "a" * 40})
    kept = _subscribed("K", "design", "tracker", {"issue": "ABC-1"}, after="10")
    _event(f"time:{timed.sub_id}:deadline", source="time", kind="deadline", match={"sub_id": timed.sub_id},
           cursor_key="", summary="deadline passed")
    _event("deploy:o/r:a:staging:1", source="deploy", kind="job", match=deploy.match, cursor_key="1",
           summary="o/r staging success")
    _event()
    assert pr.reconcile(MGR) == [] and len(sent) == 1
    assert [s.sub_id for s in _subs()] == [kept.sub_id]
    assert sorted(_unsubscribed()) == sorted([(timed.sub_id, "fired"), (deploy.sub_id, "fired")])


def test_a_busy_holder_is_not_woken_and_delivered_does_not_move(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    _events_on(tmp_path, monkeypatch)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda s: False)
    sent = _delivered(monkeypatch)
    _subscribed("K", "design", "tracker", {"issue": "ABC-1"}, after="10")
    _event()
    assert pr.reconcile(MGR) == [] and sent == [] and _subs()[0].delivered is None
    monkeypatch.setattr(pr, "_idle_or_dead", lambda s: True)
    assert pr.reconcile(MGR, now=_later(5)) == [] and len(sent) == 1 and _subs()[0].delivered is not None


def test_a_done_holder_gets_no_record_and_loses_its_subscription(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid = _at_post_merge(monkeypatch, tmp_path)
    _events_on(tmp_path, monkeypatch)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda s: True)
    sent = _delivered(monkeypatch)
    sub = _subscribed("K", "post-merge", "tracker", {"issue": "ABC-1"}, after="10")
    artifact_put_impl("K", "summary", "post-merge", "done", "complete", sid, scope="output")
    _event()
    assert pr.reconcile(MGR) == [] and sent == []
    assert _subs() == [] and _unsubscribed() == [(sub.sub_id, "node done")]
    assert pr.reconcile(MGR, now=_later(5)) == [] and sent == []


def test_a_subscription_of_a_left_node_is_dropped_without_a_wake(fresh_orchestrator_dir, tmp_path, monkeypatch):
    from dockwright import events
    _started_at_design(monkeypatch, tmp_path)
    _events_on(tmp_path, monkeypatch)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda s: True)
    sent = _delivered(monkeypatch)
    stray = events.Sub("deadbeef", "K", "plan", "tracker", {"issue": "ABC-1"}, None, "10", None, None, None, time.time(),
                       None)
    (paths.EVENTS_SUBS / "deadbeef.json").write_text(json.dumps(stray.to_dict()))
    _event()
    assert pr.reconcile(MGR) == [] and sent == []
    assert _subs() == [] and _unsubscribed() == [("deadbeef", "left")]
    assert len(events.recent("tracker", time.time())) == 1


def test_a_closed_works_subscriptions_are_removed(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _events_on(tmp_path, monkeypatch)
    sub = _subscribed("K", "design", "tracker", {"issue": "ABC-1"}, after="10")
    (paths.ACTIVE / f"{sid}.json").unlink()
    mcp_server.pipeline_close_impl("m1", "K", "stop")
    assert _subs() == [] and _unsubscribed() == [(sub.sub_id, "work closed")]
    sent = _delivered(monkeypatch)
    _event()
    before = len(_events())
    assert pr.reconcile(MGR) == [] and sent == [] and len(_events()) == before


def test_a_handler_that_exits_0_marks_delivered_without_a_line(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    handlers = _events_on(tmp_path, monkeypatch)
    sink = tmp_path / "sink.json"
    _handler_script(handlers, f'[ "$1" = close ] || exit 9\ncat > "{sink}"\nexit 0\n')
    monkeypatch.setattr(pr, "_idle_or_dead", lambda s: True)
    sent = _delivered(monkeypatch)
    sub = _subscribed("K", "design", "tracker", {"issue": "ABC-1"}, after="10", handler=["author-wait", "close"])
    e = _event()
    assert pr.reconcile(MGR) == [] and sent == []
    assert [(x["sub_id"], x["event_ids"]) for x in _of("event_handled")] == [(sub.sub_id, [e.id])]
    assert json.loads(sink.read_text()) == [e.to_dict()]
    assert _subs()[0].delivered == [e.ts, e.id] and _lane_events("EVENT_WAKE") == []
    assert pr.reconcile(MGR, now=_later(5)) == [] and len(_of("event_handled")) == 1
    timed = _subscribed("K", "design", "tracker", {"issue": "ABC-2"}, after="10", handler=["author-wait", "close"],
                        deadline=time.time() - 1)
    _event(f"time:{timed.sub_id}:deadline", source="time", kind="deadline", match={"sub_id": timed.sub_id},
           cursor_key="", summary="deadline passed")
    assert pr.reconcile(MGR, now=_later(10)) == [] and sent == []
    assert [s.sub_id for s in _subs()] == [sub.sub_id] and (timed.sub_id, "fired") in _unsubscribed()


def test_a_failing_handler_wakes_with_the_failure_tail(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    handlers = _events_on(tmp_path, monkeypatch)
    _handler_script(handlers, "exit 7\n")
    monkeypatch.setattr(pr, "_idle_or_dead", lambda s: True)
    sent = _delivered(monkeypatch)
    sub = _subscribed("K", "design", "tracker", {"issue": "ABC-1"}, after="10", handler=["author-wait", "close"])
    _event()
    assert pr.reconcile(MGR) == []
    assert [t for _, t, _ in sent] == [f"event: tracker.comment — ABC-1 #11 by A: hi (sub {sub.sub_id}) (handler failed: 7)"]
    assert _of("event_handled") == [] and _subs()[0].delivered is not None
    _handler_script(handlers, "sleep 2\nexit 0\n")
    monkeypatch.setattr(pr, "_HANDLER_TIMEOUT_SEC", 0.1)
    _event("tracker:ABC-1:comment:12", cursor_key="12", summary="ABC-1 #12 by A: hi")
    assert pr.reconcile(MGR, now=_later(5)) == []
    assert sent[-1][1].endswith("(handler failed: timeout)")


def test_a_handler_gets_only_its_sources_variables(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    handlers = _events_on(tmp_path, monkeypatch)
    _handler_script(handlers, f'env | sort > "{tmp_path}/env-$1.txt"\nexit 0\n')
    monkeypatch.setattr(pr, "_idle_or_dead", lambda s: True)
    _delivered(monkeypatch)
    _subscribed("K", "design", "tracker", {"issue": "ABC-1"}, after="10", handler=["author-wait", "tracker"])
    deploy = _subscribed("K", "design", "deploy", {"repo": "o/r", "env": "staging", "sha": "a" * 40},
                         handler=["author-wait", "deploy"])
    _event()
    _event("deploy:o/r:a:staging:1", source="deploy", kind="job", match={**deploy.match, "sub_id": deploy.sub_id},
           cursor_key="1", summary="ok")
    assert pr.reconcile(MGR) == []
    tracker_env = dict(l.split("=", 1) for l in (tmp_path / "env-tracker.txt").read_text().splitlines() if "=" in l)
    deploy_env = dict(l.split("=", 1) for l in (tmp_path / "env-deploy.txt").read_text().splitlines() if "=" in l)
    assert {k for k in tracker_env if k.startswith("TRACKER_")} == {"TRACKER_URL", "TRACKER_EMAIL", "TRACKER_API_TOKEN"}
    assert tracker_env["TRACKER_API_TOKEN"] == "tok" and tracker_env["HOME"] == os.environ["HOME"]
    assert tracker_env["PATH"].startswith(os.path.dirname(sys.executable) + ":") and "TMPDIR" not in tracker_env
    assert tracker_env[config.ENV_CONFIG_PATH] == os.environ[config.ENV_CONFIG_PATH]
    shell_own = {"PWD", "SHLVL", "_"}
    assert not {k for k in deploy_env if k.startswith("TRACKER_")}
    assert set(deploy_env) - shell_own == {"PATH", "HOME", config.ENV_CONFIG_PATH}


def test_a_deploy_decision_logged_before_the_subscription_does_not_wake_it(fresh_orchestrator_dir, tmp_path,
                                                                          monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    _events_on(tmp_path, monkeypatch)
    monkeypatch.setattr(pr, "_idle_or_dead", lambda s: True)
    sent = _delivered(monkeypatch)
    match = {"repo": "o/r", "env": "staging", "sha": "a" * 40}
    _event("deploy:o/r:a:staging:1", ts=time.time() - 3 * 3600, source="deploy", kind="job", match=dict(match),
           cursor_key="1", summary="o/r staging failure")
    sub = _subscribed("K", "design", "deploy", match, deadline=time.time() + 1800)
    assert pr.reconcile(MGR) == [] and sent == []
    assert [s.sub_id for s in _subs()] == [sub.sub_id]
    _event("deploy:o/r:a:staging:2:" + sub.sub_id, source="deploy", kind="job", match={**match, "sub_id": sub.sub_id},
           cursor_key="2", summary="o/r staging success")
    assert pr.reconcile(MGR, now=_later(5)) == []
    assert [t for _, t, _ in sent] == [f"event: deploy.job — o/r staging success (sub {sub.sub_id})"] and _subs() == []


def test_a_handler_edited_to_a_path_is_refused_at_wake(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    handlers = _events_on(tmp_path, monkeypatch)
    script = _handler_script(handlers, f'touch "{tmp_path}/ran"\nexit 0\n')
    monkeypatch.setattr(pr, "_idle_or_dead", lambda s: True)
    sent = _delivered(monkeypatch)
    sub = _subscribed("K", "design", "tracker", {"issue": "ABC-1"}, after="10", handler=["author-wait"])
    path = paths.EVENTS_SUBS / f"{sub.sub_id}.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "handler": [str(script)]}))
    _event()
    assert pr.reconcile(MGR) == []
    assert [t for _, t, _ in sent] == [f"event: tracker.comment — ABC-1 #11 by A: hi (sub {sub.sub_id}) (handler refused)"]
    assert not (tmp_path / "ran").exists()


def test_a_wake_this_pass_from_the_pr_poll_defers_the_event_batch(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_post_merge_waiting(monkeypatch, tmp_path, FIRST)
    _events_on(tmp_path, monkeypatch)
    sent = _delivered(monkeypatch)
    deploy = _subscribed("K", "post-merge", "deploy", {"repo": "o/r", "env": "staging", "sha": "a" * 40})
    _event("deploy:o/r:a:staging:1", source="deploy", kind="job", match=deploy.match, cursor_key="1",
           summary="o/r staging success")
    gh = _gh_by_url({"https://github.com/o/r/pull/7": _pr(mergedAt="2026-10-08T10:00:00Z", state="MERGED", mergeStateStatus="UNKNOWN"),
                     "https://github.com/o/c/pull/8": _pr(reviewDecision="APPROVED")})
    assert pr.reconcile(MGR, run_gh=gh) == []
    assert len(sent) == 1 and sent[0][1].startswith("event: github.pr — ")
    _wake_record(sent[0][1].split(" — ", 1)[1])
    assert pr.reconcile(MGR, run_gh=gh, now=_later(700)) == []
    assert len(sent) == 2 and sent[1][1] == f"event: deploy.job — o/r staging success (sub {deploy.sub_id})"


def test_a_dead_unfinished_holder_is_restarted_by_the_event_line(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _events_on(tmp_path, monkeypatch)
    sub = _subscribed("K", "design", "tracker", {"issue": "ABC-1"}, after="10")
    (paths.ACTIVE / f"{sid}.json").unlink()
    state.write_json_atomic(paths.CLOSED / f"{sid}.json", {"claude_sid": sid, "name": "K-design",
                                                           "closed_reason": "idle>7200s", "cwd": "/w"})
    monkeypatch.setattr("dockwright.topic_message._resumable", lambda name, sid: False)
    calls = _fake_registration(monkeypatch, ["registered"])
    _event()
    assert pr.reconcile(MGR) == []
    assert [c["name"] for c in calls] == ["K-design-r2"]
    assert f"event: tracker.comment — ABC-1 #11 by A: hi (sub {sub.sub_id})" in calls[-1]["initial_prompt"]
    assert _fold().tokens["t1"].node == "design" and len(_lane_events("EVENT_WAKE")) == 1


def test_a_subscribed_dead_holder_is_gate_not_parked(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _events_on(tmp_path, monkeypatch)
    (paths.ACTIVE / f"{sid}.json").unlink()
    state.write_json_atomic(paths.CLOSED / f"{sid}.json", {"claude_sid": sid, "name": "K-design",
                                                           "closed_reason": "idle>7200s", "cwd": "/w"})
    calls = _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR) == [PARKED_IDLE]
    _subscribed("K", "design", "tracker", {"issue": "ABC-1"}, after="10")
    assert pr.reconcile(MGR, now=_later(3700)) == [] and pr.reconcile(MGR, now=_later(7400)) == []
    assert calls == [] and _fold().tokens["t1"].holder == aid


def _daemon_alive(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DAEMON", tmp_path / "daemon")
    monkeypatch.setattr(paths, "DAEMON_STOP", tmp_path / "daemon-stop")
    paths.DAEMON.mkdir()
    state.write_json_atomic(paths.DAEMON / "alive.json", {"ts": time.time(), "awake": 1.0, "pid": 1})


def test_sight_groups_the_works_in_sight_by_owner(fresh_orchestrator_dir, tmp_path, monkeypatch):
    assert pr.sight() == {}
    _started_at_design(monkeypatch, tmp_path)
    assert pr.sight() == {"mgr": ["K"]}
    _daemon_alive(tmp_path, monkeypatch)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_start_impl("daemon", "K2", "code-ticket", "/w", "ask"))
    assert pr.sight() == {"mgr": ["K"], "daemon": ["K2"]}
    assert pr._reconcilable_task_keys() == {"K", "K2"}


def test_sight_puts_a_work_whose_journal_does_not_fold_under_none(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)

    def broken(events):
        raise ValueError("bad journal")

    monkeypatch.setattr(pipeline, "fold", broken)
    assert pr.sight() == {None: ["K"]}


def _scan_raises(monkeypatch):
    def scan():
        raise AssertionError("scanned artifacts/")

    monkeypatch.setattr(pr, "_reconcilable_task_keys", scan)


def test_reconcile_with_task_keys_does_not_scan_artifacts(fresh_orchestrator_dir, tmp_path, monkeypatch, killed):
    sid, _aid = _started_at_design(monkeypatch, tmp_path)
    (paths.ACTIVE / f"{sid}.json").unlink()
    calls = _fake_registration(monkeypatch, ["registered"])
    _scan_raises(monkeypatch)
    lines = pr.reconcile(MGR, now=_later(10), task_keys=["K"])
    assert not any(line.startswith("RECONCILE_ERROR") for line in lines)
    assert [c["name"] for c in calls] == ["K-design-r2"]


def test_reconcile_with_task_keys_leaves_another_owners_work_alone(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                   killed):
    sid, _aid = _started_at_design(monkeypatch, tmp_path)
    (paths.ACTIVE / f"{sid}.json").unlink()
    calls = _fake_registration(monkeypatch, ["registered"])
    before = paths.artifact_events_path("K").read_bytes()
    assert pr.reconcile({"name": "other", "sid": "m9"}, now=_later(10), task_keys=["K"]) == []
    assert paths.artifact_events_path("K").read_bytes() == before and calls == []


def test_reconcile_with_empty_task_keys_reconciles_nothing(fresh_orchestrator_dir, tmp_path, monkeypatch, killed):
    sid, _aid = _started_at_design(monkeypatch, tmp_path)
    (paths.ACTIVE / f"{sid}.json").unlink()
    calls = _fake_registration(monkeypatch, ["registered"])
    _scan_raises(monkeypatch)
    assert pr.reconcile(MGR, now=_later(10), task_keys=[]) == [] and calls == []
