import asyncio
import os

import pytest

from dockwright import config, mcp_server, paths, state
from dockwright import pipeline_reconcile as pr
from dockwright.mcp_server import (_append_task_event, artifact_put_impl, pipeline_advance_impl, pipeline_rework_impl,
                                   pipeline_status_impl, send_manager_to_worker, send_to_child, spawn_child)
from dockwright.pipeline_child import pipeline_child_impl, refuse_spawn_child
from tests.test_pipeline_core import FORK, SCHEMA
from tests.test_pipeline_fork import PLAN, _design_done, _review
from tests.test_pipeline_reconcile import (MGR, _claim, _complete_design_outputs, _fake_registration, _later, _of,
                                           _started_at_design, fresh_orchestrator_dir)  # noqa: F401

SLOTS = '''
[nodes.design.children.reviewer]
rounds = 2
[nodes.design.children.reviewer.lanes.document]
role = "DocReviewer"
mode = "report"
model = "@opus"
effort = "xhigh"
[nodes.design.children.reviewer.lanes.tier2]
role = "CodeReviewer"
mode = "report"
model = "@opus"
effort = "xhigh"
codex = "gpt-5.6-sol"

[nodes.design.children.once]
warm = false
rounds = 3
role = "DocReviewer"
mode = "report"
model = "@sonnet"
effort = "xhigh"

[nodes.design.children.warm]
rounds = 4
role = "DocReviewer"
mode = "report"
model = "@opus"
effort = "xhigh"

[nodes.task.children.reviewer]
rounds = 2
role = "CodeReviewer"
mode = "report"
model = "@opus"
effort = "xhigh"
'''
DECLARED = SCHEMA + SLOTS
ROW_SLOT = ('\n[nodes.task.children.reviewer]\nrounds = 2\nrole = "CodeReviewer"\nmode = "report"\n'
            'model = "@opus"\neffort = "xhigh"\n')
OLD_SLOTS = '\n[nodes.design.children.reviewer]\nroles = ["SCOUT"]\nrounds = 2\n'


@pytest.fixture
def legs(monkeypatch):
    calls = {"spawn": [], "send": [], "kill": [], "codex": "not-configured", "send_result": None, "send_errors": {},
             "kill_errors": {}, "kill_results": {}}
    real_spawn = mcp_server.spawn_worker_impl

    async def spawn(initial_prompt, **kw):
        if kw.get("_tool") != "spawn_child":
            return await real_spawn(initial_prompt, **kw)
        n = len(calls["spawn"]) + 1
        name = f"child-{n}"
        calls["spawn"].append({"prompt": initial_prompt, "name": name, **kw})
        if kw.get("provider") == "codex-exec" and calls["codex"] == "not-configured":
            return {"status": "not-configured", "note": "no codex leg for this cwd"}
        if kw.get("provider") == "codex-exec" and calls["codex"] == "provider-error":
            return {"status": "provider-error", "name": name, "reason": "codex never started", "window_id": "%5"}
        state.write_json_atomic(paths.ACTIVE / f"sid-{name}.json",
                                {"claude_sid": f"sid-{name}", "agent": "worker", "name": name, "pid": os.getpid()})
        state.write_json_atomic(paths.assignment_path(f"sid-{name}"),
                                {"assignment_id": f"a-{name}", "name": name, "ticket": kw.get("task_key"),
                                 "parent_sid": kw.get("manager_sid")})
        return {"status": "registered", "name": name, "claude_sid": f"sid-{name}"}

    async def send(name, text, **kw):
        errors = calls["send_errors"].get(name) or []
        if errors:
            raise errors.pop(0)
        calls["send"].append((name, text))
        return calls["send_result"] or {"status": "delivered", "worker": name, "confirmed": True}

    def kill(worker, dry_run=False):
        if worker in calls["kill_errors"]:
            raise calls["kill_errors"][worker]
        calls["kill"].append(worker)
        if worker in calls["kill_results"]:
            return calls["kill_results"][worker]
        (paths.ACTIVE / f"sid-{worker}.json").unlink(missing_ok=True)
        return {}

    monkeypatch.setattr("dockwright.mcp_server.spawn_worker_impl", spawn)
    monkeypatch.setattr("dockwright.mcp_server.send_to_child_impl", send)
    monkeypatch.setattr("dockwright.mcp_server.kill_worker_impl", kill)
    return calls


def _at_design(monkeypatch, tmp_path, schema_text=DECLARED):
    return _started_at_design(monkeypatch, tmp_path, schema_text=schema_text)


def _at_old_snapshot(monkeypatch, tmp_path, snapshot_text):
    started = _started_at_design(monkeypatch, tmp_path)
    (paths.artifact_ticket_dir("K") / "schema.toml").write_text(snapshot_text)
    return started


def _run(sid, **kw):
    return asyncio.run(pipeline_child_impl(sid, "K", **kw))


def _rounds():
    return _of("child_round")


def _to_closed(name):
    record = state.read_json(paths.ACTIVE / f"sid-{name}.json")
    state.write_json_atomic(paths.CLOSED / f"sid-{name}.json", record)
    (paths.ACTIVE / f"sid-{name}.json").unlink()


def test_an_undeclared_slot_a_foreign_role_and_a_non_holder_are_refused(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                        legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match=r"\[nodes.design\] declares no child slot 'nope'; declared: reviewer, once, warm"):
        _run(sid, child="nope", message="m")
    with pytest.raises(ValueError, match="NOT_IN_SLOT"):
        _run(sid, child="reviewer", lane="NOT_IN_SLOT", message="m")
    with pytest.raises(ValueError, match="you hold no token of K"):
        _run("m1", child="reviewer", message="m")
    assert legs["spawn"] == [] and _rounds() == []


def test_round_one_starts_both_legs_and_journals_the_round(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    r = _run(sid, child="reviewer", message="review this")
    assert (r["ok"], r["round"], r["entry"]) == (True, 1, 1)
    assert r["legs"] == [{"name": "child-1", "provider": "claude-tui", "action": "spawned", "confirmed": True}]
    assert r["codex"] == "no codex lane"
    assert [(c["provider"], c["_tool"], c["manager_sid"], c["context_role"], c["task_key"]) for c in legs["spawn"]] == [
        ("claude-tui", "spawn_child", sid, "DocReviewer", "K")]
    assert all("cwd" not in c for c in legs["spawn"])
    [event] = _rounds()
    assert (event["round"], event["slot"], event["legs"], event["entry"], event["node"]) == (
        1, "reviewer", ["child-1"], 1, "design")


def test_a_row_childs_spawn_lands_in_the_row_worktree(fresh_orchestrator_dir, tmp_path, monkeypatch):
    paths.PRESETS.mkdir(parents=True, exist_ok=True)
    (paths.PRESETS / "verifier-settings.json").write_text('{"permissions": {"deny": ["Write"]}}')
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=FORK + ROW_SLOT)
    _design_done(sid)
    calls = _fake_registration(monkeypatch, ["registered"] * 4)
    contracts = asyncio.run(pipeline_advance_impl(sid, "K", "task"))["next"]["sid"]
    _review(contracts, 1)
    svc = asyncio.run(pipeline_advance_impl(contracts, "K", "pr"))["rows"][0]["next"]["sid"]
    r = _run(svc, child="reviewer", message="review row 2")
    assert r["round"] == 1 and calls[-1]["cwd"] == "/s"
    assert _rounds()[-1]["row"] == "svc" and _rounds()[-1]["token_id"] == "t1/svc"


def test_round_two_goes_to_the_same_live_child(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    _run(sid, child="reviewer", message="round 1")
    r = _run(sid, child="reviewer", message="round 2")
    assert r["round"] == 2 and r["legs"][0]["action"] == "sent"
    assert legs["send"] == [("child-1", "round 2")] and len(legs["spawn"]) == 1 and legs["kill"] == []


def test_a_busy_child_refuses_the_round_and_spends_nothing(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    _run(sid, child="reviewer", message="round 1")
    legs["send_result"] = {"status": "human_attached", "delivered": False}
    with pytest.raises(ValueError, match="child-1 is human-attached"):
        _run(sid, child="reviewer", message="round 2")
    assert len(_rounds()) == 1
    legs["send_result"] = None
    assert _run(sid, child="reviewer", message="round 2")["round"] == 2


def test_an_unconfirmed_send_is_journaled_and_flagged(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    _run(sid, child="reviewer", message="round 1")
    legs["send_result"] = {"status": "delivered", "confirmed": False}
    r = _run(sid, child="reviewer", message="round 2")
    assert r["legs"][0]["confirmed"] is False and "get_child_tail(child-1)" in r["note"]
    assert [e["round"] for e in _rounds()] == [1, 2]


def test_a_warm_round_whose_child_is_gone_spends_nothing_and_the_next_call_starts_a_new_child(
        fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    _run(sid, child="reviewer", message="round 1")
    (paths.ACTIVE / "sid-child-1.json").unlink()
    with pytest.raises(ValueError, match="child-1 is gone; nothing was sent and no round was spent"):
        _run(sid, child="reviewer", message="re-check finding 2")
    assert len(_rounds()) == 1 and _of("child_closed")[-1]["names"] == ["child-1"]
    r = _run(sid, child="reviewer", message="full brief with the verdict of round 1")
    assert (r["round"], r["legs"][0]["name"], r["legs"][0]["action"]) == (2, "child-2", "spawned")
    assert legs["send"] == []


def test_a_codex_leg_that_is_shutting_down_is_retried_not_respawned(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                    legs):
    monkeypatch.setattr("dockwright.pipeline_child._SHUTDOWN_RETRY_SEC", 0)
    legs["codex"] = "live"
    sid, _ = _at_design(monkeypatch, tmp_path)
    _run(sid, child="reviewer", lane="tier2", message="1")
    legs["send_errors"]["child-2"] = [ValueError("'child-2' has written its round-1 report and is shutting down; "
                                                 "retry send_to_child in a few seconds")]
    r = _run(sid, child="reviewer", lane="tier2", message="2")
    assert [(leg["name"], leg["action"]) for leg in r["legs"]] == [("child-1", "sent"), ("child-2", "sent")]
    assert legs["kill"] == [] and len(legs["spawn"]) == 2


def test_a_holder_whose_sid_rotated_keeps_its_children(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    _run(sid, child="reviewer", message="1")
    record = state.read_json(paths.ACTIVE / f"{sid}.json")
    state.write_json_atomic(paths.ACTIVE / "sid-rotated.json", {**record, "claude_sid": "sid-rotated"})
    (paths.ACTIVE / f"{sid}.json").unlink()
    state.migrate_assignment(sid, "sid-rotated")
    r = _run("sid-rotated", child="reviewer", message="2")
    assert (r["round"], r["legs"][0]["name"], r["legs"][0]["action"]) == (2, "child-1", "sent")
    _complete_design_outputs("sid-rotated")
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("sid-rotated", "K", "task"))
    assert legs["kill"] == ["child-1"]


def test_a_leg_whose_kill_fails_stays_open(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    _run(sid, child="reviewer", message="1")
    legs["kill_errors"]["child-1"] = ValueError("pane busy")
    assert _run(sid, child="reviewer", close=True) == {"ok": True, "closed": []}
    assert "legs child-1 (alive)" in pipeline_status_impl("K")
    del legs["kill_errors"]["child-1"]
    assert _run(sid, child="reviewer", close=True) == {"ok": True, "closed": ["child-1"]}


def test_a_gone_child_whose_kill_fails_still_lets_the_next_call_start_a_new_child(fresh_orchestrator_dir, tmp_path,
                                                                                   monkeypatch, legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    _run(sid, child="reviewer", message="round 1")
    (paths.ACTIVE / "sid-child-1.json").unlink()
    legs["kill_errors"]["child-1"] = RuntimeError("tmux unreachable")
    with pytest.raises(ValueError, match="child-1 is gone"):
        _run(sid, child="reviewer", message="re-check")
    r = _run(sid, child="reviewer", message="full brief")
    assert (r["round"], r["legs"][0]["name"], r["legs"][0]["action"]) == (2, "child-2", "spawned")


def test_a_pane_that_is_still_live_after_the_kill_stays_open(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    _run(sid, child="reviewer", message="1")
    legs["kill_results"]["child-1"] = {"closed_window": None, "already_dead": False,
                                       "note": "'child-1' never registered and its pane is live — not closed"}
    assert _run(sid, child="reviewer", close=True) == {"ok": True, "closed": []}
    del legs["kill_results"]["child-1"]
    assert _run(sid, child="reviewer", close=True) == {"ok": True, "closed": ["child-1"]}


def test_a_gone_leg_counts_as_closed(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    _run(sid, child="reviewer", message="1")
    legs["kill_errors"]["child-1"] = ValueError("no worker named 'child-1'")
    assert _run(sid, child="reviewer", close=True) == {"ok": True, "closed": ["child-1"]}


def test_a_codex_provider_error_is_closed_and_reported(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    legs["codex"] = "provider-error"
    sid, _ = _at_design(monkeypatch, tmp_path)
    r = _run(sid, child="reviewer", lane="tier2", message="1")
    assert [leg["name"] for leg in r["legs"]] == ["child-1"] and "codex never started" in r["codex"]
    assert legs["kill"] == ["child-2"]


def test_a_lane_change_closes_the_old_child_and_starts_a_new_one(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    _run(sid, child="reviewer", message="round 1")
    r = _run(sid, child="reviewer", lane="tier2", message="round 2")
    assert legs["kill"] == ["child-1"] and legs["send"] == []
    assert legs["spawn"][-2]["context_role"] == "CodeReviewer" and r["legs"][0]["name"] == "child-2"
    assert r["round"] == 2 and _of("child_closed")[-1]["names"] == ["child-1"]


def test_a_lane_starts_the_claude_leg_with_its_role_mode_model_and_effort(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    out = asyncio.run(pipeline_child_impl(sid, "K", "reviewer", lane="document", message="review it"))
    claude = legs["spawn"][0]
    assert (claude["provider"], claude["context_role"], claude["mode"], claude["model"], claude["effort"]) == (
        "claude-tui", "DocReviewer", "report", "@opus", "xhigh")
    assert "role" not in claude
    assert out["codex"] == "no codex lane" and len(legs["spawn"]) == 1


def test_a_lane_with_a_codex_model_starts_the_codex_leg_on_it(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    legs["codex"] = "registered"
    sid, _ = _at_design(monkeypatch, tmp_path)
    out = asyncio.run(pipeline_child_impl(sid, "K", "reviewer", lane="tier2", message="review it"))
    codex = legs["spawn"][1]
    assert (codex["provider"], codex["model"], codex["mode"], codex["context_role"]) == ("codex-exec", "gpt-5.6-sol", "report", "CodeReviewer")
    assert codex.get("effort") is None
    assert out["codex"] == "registered"


def test_a_codex_leg_that_refuses_a_round_is_restarted_from_the_lane(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    legs["codex"] = "registered"
    sid, _ = _at_design(monkeypatch, tmp_path)
    asyncio.run(pipeline_child_impl(sid, "K", "reviewer", lane="tier2", message="r1"))
    legs["send_errors"]["child-2"] = [ValueError("mid-round; wait_for_child first")]
    out = asyncio.run(pipeline_child_impl(sid, "K", "reviewer", lane="tier2", message="r2"))
    assert legs["send"][-1][0] == "child-1" and "child-2" in legs["kill"]
    fresh = legs["spawn"][2]
    assert (fresh["provider"], fresh["model"]) == ("codex-exec", "gpt-5.6-sol") and out["codex"] == "registered"


def test_the_first_lane_is_the_default(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    asyncio.run(pipeline_child_impl(sid, "K", "reviewer", message="review it"))
    assert legs["spawn"][0]["context_role"] == "DocReviewer"


def test_an_unknown_lane_and_a_role_argument_are_refused(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="lane 'final' is not a lane of slot reviewer; its lanes: document, tier2"):
        asyncio.run(pipeline_child_impl(sid, "K", "reviewer", lane="final", message="x"))
    with pytest.raises(ValueError, match="role names a roster row; this slot has lanes: document, tier2"):
        asyncio.run(pipeline_child_impl(sid, "K", "reviewer", role="REVIEW_SPEC", message="x"))
    assert legs["spawn"] == []


def test_a_warm_round_matches_the_lane_and_an_old_round_recorded_as_role(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    asyncio.run(pipeline_child_impl(sid, "K", "warm", message="r1"))
    events = _rounds()
    assert events[-1]["lane"] == "default" and "role" not in events[-1]
    asyncio.run(pipeline_child_impl(sid, "K", "warm", message="r2"))
    assert len(legs["spawn"]) == 1 and legs["send"][0][0] == "child-1"
    old = {**events[-1], "role": "default", "round": 3}
    del old["lane"]
    _append_task_event("K", old)
    asyncio.run(pipeline_child_impl(sid, "K", "warm", message="r4"))
    assert len(legs["spawn"]) == 1


def test_an_old_snapshot_slot_takes_role_from_the_legacy_file_and_starts_no_codex_leg(fresh_orchestrator_dir, tmp_path,
                                                                                       monkeypatch, legs):
    home = tmp_path / "claude-home"
    home.mkdir()
    (home / "dockwright-roster.toml").write_text(
        '[roles.SCOUT]\nproviders = ["claude", "codex"]\n[roles.SCOUT.claude]\nmodel = "@sonnet"\neffort = "xhigh"\n'
        'context = "DocReviewer"\n[roles.SCOUT.codex]\nmodel = "gpt-5.6-sol"\n')
    monkeypatch.setattr(config, "claude_config_home", lambda: home)
    legs["codex"] = "registered"
    sid, _ = _at_old_snapshot(monkeypatch, tmp_path, SCHEMA + OLD_SLOTS)
    out = asyncio.run(pipeline_child_impl(sid, "K", "reviewer", role="SCOUT", message="r1"))
    claude = legs["spawn"][0]
    assert (claude["context_role"], claude["model"], claude["effort"], claude["mode"]) == ("DocReviewer", "@sonnet", "xhigh", "report")
    assert out["codex"] == "old snapshot: no codex leg" and len(legs["spawn"]) == 1
    with pytest.raises(ValueError, match="lane 'x' is not a row of slot reviewer"):
        asyncio.run(pipeline_child_impl(sid, "K", "reviewer", lane="x", message="r2"))
    with pytest.raises(ValueError, match="pass role= or lane=, not both"):
        asyncio.run(pipeline_child_impl(sid, "K", "reviewer", role="SCOUT", lane="SCOUT", message="r2"))


def test_a_cold_slot_starts_a_new_child_every_round(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    _run(sid, child="once", message="round 1")
    r = _run(sid, child="once", message="round 2")
    assert [c["provider"] for c in legs["spawn"]].count("claude-tui") == 2
    assert legs["kill"] == ["child-1"] and legs["send"] == [] and r["round"] == 2


def test_a_round_past_the_recommendation_runs_and_the_result_says_so(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    assert _run(sid, child="reviewer", message="1")["recommended_rounds"] == 2
    r2 = _run(sid, child="reviewer", message="2")
    assert "note" not in r2
    r = _run(sid, child="reviewer", message="3", cut="q1")
    assert (r["round"], r["recommended_rounds"]) == (3, 2)
    assert "round 3 is past the 2 rounds recommended for slot reviewer" in r["note"]
    assert "cut" not in _rounds()[-1]


def test_close_kills_the_legs_and_journals_it(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    sid, aid = _at_design(monkeypatch, tmp_path)
    _run(sid, child="reviewer", message="1")
    assert _run(sid, child="reviewer", close=True) == {"ok": True, "closed": ["child-1"]}
    assert legs["kill"] == ["child-1"]
    closed = _of("child_closed")[-1]
    assert (closed["assignment_id"], closed["names"]) == (aid, ["child-1"])
    assert _run(sid, child="reviewer", close=True) == {"ok": True, "closed": []}


def test_close_after_two_live_legs_kills_both(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    legs["codex"] = "live"
    sid, _ = _at_design(monkeypatch, tmp_path)
    r = _run(sid, child="reviewer", lane="tier2", message="1", codex_message="codex 1")
    assert [leg["name"] for leg in r["legs"]] == ["child-1", "child-2"]
    assert legs["spawn"][1]["prompt"] == "codex 1"
    _run(sid, child="reviewer", lane="tier2", message="2", codex_message="codex 2")
    assert legs["send"] == [("child-1", "2"), ("child-2", "codex 2")]
    _run(sid, child="reviewer", close=True)
    assert sorted(legs["kill"]) == ["child-1", "child-2"]


def test_moving_the_token_closes_the_holders_children(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    sid, aid = _at_design(monkeypatch, tmp_path)
    _run(sid, child="reviewer", message="1")
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["registered"])
    assert asyncio.run(pipeline_advance_impl(sid, "K", "task"))["state"] == "committed"
    assert legs["kill"] == ["child-1"]
    assert _of("child_closed")[-1]["assignment_id"] == aid


def test_a_restart_closes_the_old_childs_and_the_new_session_continues_the_budget(fresh_orchestrator_dir, tmp_path,
                                                                                  monkeypatch, legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    _run(sid, child="reviewer", message="1")
    (paths.ACTIVE / f"{sid}.json").unlink()
    _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR) == []
    assert legs["kill"] == ["child-1"]
    r = _run("sid-K-design-r2", child="reviewer", message="2 with the verdict of round 1")
    assert (r["round"], r["entry"], r["legs"][0]["action"]) == (2, 1, "spawned")
    assert legs["send"] == [] and r["legs"][0]["name"] == "child-2"


def test_a_gate_verdict_opens_a_new_entry_on_the_same_child(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    _run(sid, child="reviewer", message="1")
    _run(sid, child="reviewer", message="2")
    artifact_put_impl("K", "decision", "verdict-r1", "{}", "complete", "topic:cli", scope="output")
    r = _run(sid, child="reviewer", message="after the gate")
    assert (r["round"], r["entry"]) == (1, 2)
    assert legs["send"][-1] == ("child-1", "after the gate") and len(legs["spawn"]) == 1


def test_a_rework_that_archives_the_verdicts_gives_the_next_entry_a_fresh_budget(fresh_orchestrator_dir, tmp_path,
                                                                                 monkeypatch, legs):
    sid, _ = _started_at_design(monkeypatch, tmp_path, cwd=str(tmp_path), schema_text=DECLARED)
    artifact_put_impl("K", "decision", "verdict-r1", "{}", "complete", "topic:cli", scope="output")
    _run(sid, child="reviewer", message="1")
    _run(sid, child="reviewer", message="2")
    (paths.ACTIVE / f"{sid}.json").unlink()
    _fake_registration(monkeypatch, ["registered"])
    assert asyncio.run(pipeline_rework_impl("m1", "K", "design", "again"))["state"] == "committed"
    r = _run("sid-K-design", child="reviewer", message="first round of the rework")
    assert (r["entry"], r["round"]) == (2, 1)


def test_a_rework_round_and_a_return_name_their_children_with_the_round_and_the_entry(fresh_orchestrator_dir, tmp_path,
                                                                                      monkeypatch, legs):
    sid, _ = _started_at_design(monkeypatch, tmp_path, cwd=str(tmp_path), schema_text=DECLARED)
    claude = lambda: [c for c in legs["spawn"] if c["provider"] == "claude-tui"][-1]["name"]
    r = _run(sid, child="reviewer", message="1")
    assert (r["entry"], r["round"]) == (1, 1) and claude() == "child-1"
    artifact_put_impl("K", "decision", "verdict-r1", "{}", "complete", "topic:cli", scope="output")
    r = _run(sid, child="reviewer", lane="tier2", message="2")
    assert (r["entry"], r["round"]) == (2, 1) and claude().endswith("-codereviewer-e2-r1")
    (paths.ACTIVE / f"{sid}.json").unlink()
    _fake_registration(monkeypatch, ["registered"])
    assert asyncio.run(pipeline_rework_impl("m1", "K", "design", "again"))["state"] == "committed"
    r = _run("sid-K-design", child="reviewer", message="first round of the rework")
    assert (r["entry"], r["round"]) == (2, 1) and claude().endswith("-docreviewer-w2-e2-r1")
    artifact_put_impl("K", "decision", "verdict-r2", "{}", "complete", "topic:cli", scope="output")
    r = _run("sid-K-design", child="reviewer", lane="tier2", message="after the verdict")
    assert (r["entry"], r["round"]) == (3, 1) and claude().endswith("-codereviewer-w2-e3-r1")


def test_a_pended_advance_settled_by_the_reconciler_is_an_arrival(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                  legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["no_register"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    _claim("K-task-1", "sid-t1")
    _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR, now=_later(400)) == []
    assert [e.get("reconciled") for e in _of("advance_committed")][-1] is True
    assert _run("sid-t1", child="reviewer", message="1")["entry"] == 1
    (paths.ACTIVE / "sid-t1.json").unlink()
    _fake_registration(monkeypatch, ["no_register"])
    pr.reconcile(MGR, now=_later(800))
    _claim("K-task-1-r2", "sid-t1-r2")
    _fake_registration(monkeypatch, [])
    pr.reconcile(MGR, now=_later(1600))
    restart = [e for e in _of("advance_requested") if e.get("kind") == "restart"]
    assert restart and _of("advance_committed")[-1]["request_id"] == restart[-1]["request_id"]
    assert _of("advance_committed")[-1]["reconciled"] is True
    r = _run("sid-t1-r2", child="reviewer", message="2")
    assert (r["entry"], r["round"]) == (1, 2)


def test_spawn_child_is_closed_to_a_node_of_a_declared_work_and_its_children(fresh_orchestrator_dir, tmp_path,
                                                                             monkeypatch, legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="start children only through pipeline_child"):
        asyncio.run(spawn_child(parent_sid=sid, brief="x", provider="claude-tui"))
    state.write_json_atomic(paths.ACTIVE / "sub.json", {"claude_sid": "sub", "agent": "worker", "name": "sub",
                                                        "kind": "subworker", "parent_sid": sid, "pid": os.getpid()})
    with pytest.raises(ValueError, match="the nodes of K start children only through pipeline_child"):
        refuse_spawn_child("sub")
    assert legs["spawn"] == []


def test_spawn_child_stays_open_to_a_node_of_a_schema_without_children(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                        legs):
    sid, _ = _at_design(monkeypatch, tmp_path, schema_text=SCHEMA)
    refuse_spawn_child(sid)
    r = asyncio.run(spawn_child(parent_sid=sid, brief="x", provider="claude-tui"))
    assert r["status"] == "registered" and legs["spawn"][-1]["manager_sid"] == sid


def test_send_to_child_refuses_a_leg_pipeline_child_started(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    _run(sid, child="reviewer", message="1")
    monkeypatch.setattr("dockwright.mcp_server.send_manager_to_worker_impl",
                        lambda worker, text, marker="": legs["send"].append((worker, text)) or {"status": "delivered"})
    for target in ("child-1", "sid-child-1"):
        with pytest.raises(ValueError, match="send_to_child: child-1 is a child pipeline_child started for K"):
            asyncio.run(send_to_child(target, "one more round"))
    for auto_resume in (False, True):
        with pytest.raises(ValueError, match="send_manager_to_worker: child-1 is a child pipeline_child started"):
            asyncio.run(send_manager_to_worker("child-1", "one more round", auto_resume=auto_resume))
    assert legs["send"] == [] and len(_rounds()) == 1


def test_a_codex_leg_between_rounds_is_refused_until_its_slot_closes(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                     legs):
    legs["codex"] = "live"
    sid, _ = _at_design(monkeypatch, tmp_path)
    _run(sid, child="reviewer", lane="tier2", message="1")
    _to_closed("child-2")
    with pytest.raises(ValueError, match="child-2 is a child pipeline_child started for K.*pipeline_child"):
        asyncio.run(send_to_child("child-2", "codex round 2"))
    assert legs["send"] == []
    _run(sid, child="reviewer", close=True)
    assert asyncio.run(send_to_child("child-2", "after the slot closed"))["status"] == "delivered"
    assert legs["send"] == [("child-2", "after the slot closed")]


def test_send_to_child_leaves_a_child_the_engine_did_not_start_alone(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                    legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    _run(sid, child="reviewer", message="1")
    state.write_json_atomic(paths.ACTIVE / "sid-own.json",
                            {"claude_sid": "sid-own", "agent": "worker", "name": "own", "pid": os.getpid()})
    state.write_json_atomic(paths.assignment_path("sid-own"), {"assignment_id": "a-own", "ticket": "K"})
    assert asyncio.run(send_to_child("own", "hello"))["status"] == "delivered"
    assert legs["send"] == [("own", "hello")]


def test_status_lists_the_children(fresh_orchestrator_dir, tmp_path, monkeypatch, legs):
    sid, _ = _at_design(monkeypatch, tmp_path)
    _run(sid, child="reviewer", message="1")
    text = pipeline_status_impl("K")
    assert "## children" in text
    assert "- t1 design reviewer entry 1: round 1/2 lane document legs child-1 (alive)" in text
    _run(sid, child="reviewer", close=True)
    assert "legs child-1 (closed)" in pipeline_status_impl("K")
