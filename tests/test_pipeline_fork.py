import asyncio
import time

import pytest

from dockwright import monitor, paths, pipeline, state
from dockwright import pipeline_reconcile as pr
from dockwright.mcp_server import (artifact_put_impl, pipeline_advance_impl, pipeline_rework_impl,
                                   pipeline_status_impl, worker_done_impl)
from tests.test_pipeline_core import FORK
from tests.test_pipeline_reconcile import (MGR, _claim, _events, _fake_registration, _fold, _later,
                                           _started_at_design, clock, fresh_orchestrator_dir, killed,
                                           session_logs)  # noqa: F401

PLAN = ('{"tasks": [{"id": 1, "row": "contracts", "cwd": "/c"}, '
        '{"id": 2, "row": "svc", "stage": 2, "cwd": "/s"}, '
        '{"id": 3, "row": "web", "stage": 2, "cwd": "/f"}, '
        '{"id": 4, "row": "svc", "stage": 2, "cwd": "/s"}]}')


def _design_done(sid, plan=PLAN):
    for name, body in (("context.ask", "x"), ("design.hld", "x"), ("plan.tasks", plan)):
        phase, n = name.split(".", 1)
        artifact_put_impl("K", phase, n, body, "complete", sid, scope="output", overwrite=True)


def _review(sid, i):
    artifact_put_impl("K", "review", f"task-{i}", "APPROVE", "complete", sid, scope="output")


def _dead(*sids):
    for sid in sids:
        (paths.ACTIVE / f"{sid}.json").unlink(missing_ok=True)


TWO_JOINS = FORK + '''
[nodes.pr-internal]
role = "IMPL_HARD"
model = "@opus"
effort = "xhigh"
brief = "briefs/pr.md"
inputs = ["plan.tasks"]
outputs = ["implement.repo"]

[[edges]]
from = "task"
to = "pr-internal"
when = "last"
join = true
exit_checks = [{ type = "when", input = "context.ask", line = "internal", equals = "yes", unless = "design.explainer" }]
'''


@pytest.mark.parametrize("ask,gated,taken", [
    ("internal: yes — no ticket", False, True),
    ("internal: no — the reason", False, False),
    ("internal: yes — no ticket", True, False),
])
def test_a_second_join_edge_is_taken_only_when_its_when_check_holds(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                     ask, gated, taken):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=TWO_JOINS)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b"}]}')
    artifact_put_impl("K", "context", "ask", ask, "complete", sid, scope="output", overwrite=True)
    if gated:
        artifact_put_impl("K", "design", "explainer", "x", "complete", sid, scope="checkpoint")
    _fake_registration(monkeypatch, ["registered"] * 3)
    a, b = (x["next"]["sid"] for x in asyncio.run(pipeline_advance_impl(sid, "K", "task"))["rows"])
    _review(a, 1)
    _review(b, 2)
    if not taken:
        with pytest.raises(ValueError, match="exit_check failed"):
            asyncio.run(pipeline_advance_impl(a, "K", "pr-internal"))
        assert asyncio.run(pipeline_advance_impl(a, "K", "pr"))["state"] == "joined"
        return
    assert asyncio.run(pipeline_advance_impl(a, "K", "pr-internal"))["state"] == "joined"
    assert asyncio.run(pipeline_advance_impl(b, "K", "pr-internal"))["next"]["name"] == "K-pr-internal"


def test_a_two_stage_fork_runs_rows_in_parallel_and_spawns_the_join_target_once(fresh_orchestrator_dir, tmp_path,
                                                                             monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid)
    calls = _fake_registration(monkeypatch, ["registered"] * 6)
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    assert r["state"] == "committed" and r["next"]["name"] == "K-task-1" and calls[-1]["cwd"] == "/c"
    f = _fold()
    assert f.tokens["t1"].forked and set(f.tokens) == {"t1", "t1/contracts"}
    contracts = r["next"]["sid"]
    _review(contracts, 1)
    r = asyncio.run(pipeline_advance_impl(contracts, "K", "pr"))
    assert [x["next"]["name"] for x in r["rows"]] == ["K-task-2", "K-task-3"]
    assert [c["cwd"] for c in calls[-2:]] == ["/s", "/f"]
    svc, web = (x["next"]["sid"] for x in r["rows"])
    _review(svc, 2)
    r = asyncio.run(pipeline_advance_impl(svc, "K", "task"))
    assert r["next"]["name"] == "K-task-4"
    svc4 = r["next"]["sid"]
    _review(web, 3)
    assert asyncio.run(pipeline_advance_impl(web, "K", "pr")) == {"ok": True, "state": "joined", "request_id": None,
                                                                  "next": None}
    assert state.read_json(paths.assignment_path(web))["handed_off_at"] > 0
    _review(svc4, 4)
    r = asyncio.run(pipeline_advance_impl(svc4, "K", "pr"))
    assert r["next"]["name"] == "K-pr"
    f = _fold()
    assert list(f.tokens) == ["t1"] and f.tokens["t1"].node == "pr" and not f.tokens["t1"].forked
    assert [e["type"] for e in _events()].count("token_retired") == 3


def test_one_row_behaves_as_today(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1}, {"id": 2}]}')
    _fake_registration(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    assert r["next"]["name"] == "K-task-1" and "token_forked" not in [e["type"] for e in _events()]
    assert _fold().tokens["t1"].node == "task" and not _fold().tokens["t1"].forked


def test_a_rework_waits_for_live_rows_then_drops_them_and_their_pended_requests(fresh_orchestrator_dir, tmp_path,
                                                                               monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path, cwd=str(tmp_path), schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b"}]}')
    _fake_registration(monkeypatch, ["registered", "no_register"])
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    live = r["rows"][0]["next"]["sid"]
    _dead(sid)
    with pytest.raises(ValueError, match="is alive: kill_worker it"):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", "redo"))
    _dead(live)
    for p in paths.ASSIGNMENTS_PENDING.glob("*.json"):
        p.unlink()
    _fake_registration(monkeypatch, ["registered"])
    assert asyncio.run(pipeline_rework_impl("m1", "K", "design", "redo"))["round"] == 2
    f = _fold()
    assert list(f.tokens) == ["t1"] and f.open_requests == [] and f.fork is None


def test_a_rework_is_refused_while_a_session_of_a_rows_open_request_is_alive(fresh_orchestrator_dir, tmp_path,
                                                                              monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path, cwd=str(tmp_path), schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b"}]}')
    _fake_registration(monkeypatch, ["registered", "no_register"])
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    _dead(sid, r["rows"][0]["next"]["sid"])
    _claim("K-task-2", "sid-late")
    with pytest.raises(ValueError, match=r"session K-task-2 \(sid-late\) of open request \w+ is alive"):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", "redo"))
    assert _fold().round == 1


def test_an_override_is_refused_while_rows_live(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b"}]}')
    _fake_registration(monkeypatch, ["registered", "registered"])
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    with pytest.raises(ValueError, match=r"rows of a fork are live: resume a dead row \(send_manager_to_worker with "
                                         r"auto_resume=true\); to redo the fork, kill_worker every row and "
                                         r"pipeline_rework at design$"):
        asyncio.run(pipeline_advance_impl("m1", "K", "design", override=True, reason="jump"))
    with pytest.raises(ValueError, match="override is manager-only"):
        asyncio.run(pipeline_advance_impl(r["rows"][0]["next"]["sid"], "K", "design", override=True, reason="jump"))


def test_an_override_into_the_fork_target_with_two_rows_is_refused(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b"}]}')
    calls = _fake_registration(monkeypatch, [])
    with pytest.raises(ValueError, match="task is entered by a fork edge and plan.tasks has 2 rows: enter at design "
                                         "or earlier"):
        asyncio.run(pipeline_advance_impl("m1", "K", "task", override=True, reason="jump"))
    assert calls == [] and _fold().tokens["t1"].node == "design"


def test_a_rework_into_the_fork_target_with_two_rows_is_refused(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path, cwd=str(tmp_path), schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b"}]}')
    _fake_registration(monkeypatch, ["registered", "registered"])
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    _dead(sid, *(x["next"]["sid"] for x in r["rows"]))
    with pytest.raises(ValueError, match="task is entered by a fork edge and plan.tasks has 2 rows: enter at design "
                                         "or earlier"):
        asyncio.run(pipeline_rework_impl("m1", "K", "task", "redo"))
    assert _fold().round == 1 and set(_fold().tokens) == {"t1", "t1/a", "t1/b"}


def test_the_forking_caller_and_each_joining_row_are_handed_off_once(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b"}, {"id": 3, "row": "c"}]}')
    _fake_registration(monkeypatch, ["registered"] * 4)
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    rows = [(x["next"]["sid"], x["next"]["assignment_id"]) for x in r["rows"]]
    for i, (row_sid, _) in enumerate(rows, 1):
        _review(row_sid, i)
        asyncio.run(pipeline_advance_impl(row_sid, "K", "pr"))
    handed = [e["assignment_id"] for e in _events() if e["type"] == "handed_off"]
    assert handed.count(aid) == 1 and [handed.count(a) for _, a in rows] == [1, 1, 1]
    assert _fold().tokens["t1"].node == "pr"


def test_a_row_that_joins_while_another_row_lives_is_paged_for_a_missing_summary(fresh_orchestrator_dir, tmp_path,
                                                                                  monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b"}]}')
    _fake_registration(monkeypatch, ["registered"] * 2)
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    row_a = r["rows"][0]["next"]["sid"]
    _review(row_a, 1)
    assert asyncio.run(pipeline_advance_impl(row_a, "K", "pr"))["state"] == "joined"
    assert not any(line.startswith("NODE_NO_SUMMARY") for line in pr.reconcile(MGR))
    assert sorted((e["fields"]["node"], e["fields"]["name"]) for e in _events()
                  if e["type"] == "lane_event" and e["kind"] == "NODE_NO_SUMMARY") == [
        ("design", "K-design"), ("task", "K-task-1")]


def test_a_cwd_on_a_fork_advance_is_refused_and_changes_nothing(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b"}]}')
    calls = _fake_registration(monkeypatch, [])
    with pytest.raises(ValueError, match="cwd moves a single token"):
        asyncio.run(pipeline_advance_impl(sid, "K", "task", cwd=str(tmp_path)))
    assert calls == [] and "token_forked" not in [e["type"] for e in _events()]
    assert _fold().tokens["t1"].node == "design"


def test_a_hold_on_a_forked_work_names_its_rows_and_a_row_hold_is_recorded(fresh_orchestrator_dir, tmp_path,
                                                                            monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b"}]}')
    _fake_registration(monkeypatch, ["registered", "registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    with pytest.raises(ValueError, match=r"t1 is forked: hold or release each row with token_id= one of t1/a, t1/b$"):
        asyncio.run(pipeline_advance_impl("m1", "K", "?", reason="wait"))
    assert asyncio.run(pipeline_advance_impl("m1", "K", "?", reason="wait", token_id="t1/a"))["state"] == "held"
    assert _fold().held == {"t1/a": "wait"}


def test_a_hold_or_release_on_a_retired_row_says_the_row_token_is_gone(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b"}]}')
    _fake_registration(monkeypatch, ["registered", "registered"])
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    a = r["rows"][0]["next"]["sid"]
    _review(a, 1)
    assert asyncio.run(pipeline_advance_impl(a, "K", "pr"))["state"] == "joined"
    for reason in ("wait", None):
        with pytest.raises(ValueError, match=r"row token t1/a is not live \(not started yet, retired, or removed by a "
                                             r"rework\); read pipeline_status$"):
            asyncio.run(pipeline_advance_impl("m1", "K", "?", reason=reason, token_id="t1/a"))
    assert _fold().held == {} and set(_fold().tokens) == {"t1", "t1/b"}


def test_pipeline_status_prints_each_row_token(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ =_started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b"}]}')
    _fake_registration(monkeypatch, ["registered", "registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    lines = pipeline_status_impl("K").splitlines()
    assert any(line.startswith("- t1: node=task") and line.endswith(" forked") for line in lines)
    assert any(line.startswith("- t1/a: node=task index=1") and line.endswith(" row=a") for line in lines)


def test_a_pended_fork_request_is_respawned_for_its_row(fresh_orchestrator_dir, tmp_path, monkeypatch, killed):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a", "cwd": "/a"}, {"id": 2, "row": "b", "cwd": "/b"}]}')
    _fake_registration(monkeypatch, ["registered", "no_register"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    for p in paths.ASSIGNMENTS_PENDING.glob("*.json"):
        p.unlink()
    calls = _fake_registration(monkeypatch, ["registered"])
    lines = pr.reconcile(MGR, now=_later(400))
    assert lines == [] and calls[0]["cwd"] == "/b"
    assert [e["sid"] for e in _events() if e["type"] == "advance_committed" and e.get("reconciled")] == [
        "sid-K-task-2-r2"]
    assert _fold().tokens["t1/b"].row == "b"


def test_a_dead_row_holder_is_restarted_and_a_forked_t1_is_never_paged(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                      killed):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a", "cwd": "/a"}, {"id": 2, "row": "b", "cwd": "/b"}]}')
    _fake_registration(monkeypatch, ["registered", "registered"])
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    _dead(sid, r["rows"][0]["next"]["sid"])
    calls = _fake_registration(monkeypatch, ["registered"])
    lines = pr.reconcile(MGR)
    assert lines == [] and calls[0]["cwd"] == "/a"
    assert [e["sid"] for e in _events() if e["type"] == "advance_committed" and e.get("reconciled")] == [
        "sid-K-task-1-r2"]
    assert _fold().tokens["t1/a"].holder != r["rows"][0]["next"]["assignment_id"]


def test_a_dead_row_is_restarted_while_another_rows_fork_request_is_pending(fresh_orchestrator_dir, tmp_path,
                                                                            monkeypatch, killed):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a", "cwd": "/a"}, {"id": 2, "row": "b", "cwd": "/b"}]}')
    _fake_registration(monkeypatch, ["registered", "no_register"])
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    _dead(sid, r["rows"][0]["next"]["sid"])
    calls = _fake_registration(monkeypatch, ["registered"])
    lines = pr.reconcile(MGR, now=_later(100))
    assert lines == [] and calls[0]["cwd"] == "/a"
    assert [e["sid"] for e in _events() if e["type"] == "advance_committed" and e.get("reconciled")] == [
        "sid-K-task-1-r2"]
    assert any(e["token_id"] == "t1/b" for e in _fold().open_requests)


def test_each_parked_row_is_paged(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b"}]}')
    _fake_registration(monkeypatch, ["registered", "registered"])
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    _dead(sid)
    for x in r["rows"]:
        row_sid = x["next"]["sid"]
        (paths.ACTIVE / f"{row_sid}.json").unlink()
        state.write_json_atomic(paths.CLOSED / f"{row_sid}.json", {"claude_sid": row_sid, "name": x["next"]["name"],
                                                                   "closed_reason": "idle>7200s",
                                                                   "closed_at": time.time() - 10})
    calls = _fake_registration(monkeypatch, [])
    lines = pr.reconcile(MGR)
    assert sorted(line.split(" → ")[0] for line in lines) == ["NODE_PARKED K task K-task-1",
                                                              "NODE_PARKED K task K-task-2"] and calls == []
    by_name = {line.split(" → ")[0]: line for line in lines}
    assert 'token_id="t1/a"' in by_name["NODE_PARKED K task K-task-1"]
    assert 'token_id="t1/b"' in by_name["NODE_PARKED K task K-task-2"]


def test_an_inconsistent_row_list_is_refused_with_the_row_and_stage_rule(fresh_orchestrator_dir, tmp_path,
                                                                          monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a"}, {"id": 2}]}')
    calls = _fake_registration(monkeypatch, [])
    with pytest.raises(ValueError) as err:
        asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    reason = str(err.value)
    assert reason.startswith("pipeline_advance refused: plan.tasks holds no task list the engine can read")
    assert '"row" (a non-empty string, on every entry or on none)' in reason
    assert '"stage" (an integer >= 1, the same on every entry of a row)' in reason
    assert calls == [] and _fold().tokens["t1"].node == "design"


def test_a_join_whose_pending_row_left_plan_tasks_is_refused_and_changes_nothing(fresh_orchestrator_dir, tmp_path,
                                                                                monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b", "stage": 2}]}')
    _fake_registration(monkeypatch, ["registered"])
    row = asyncio.run(pipeline_advance_impl(sid, "K", "task"))["next"]["sid"]
    artifact_put_impl("K", "plan", "tasks", '{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "c", "stage": 2}]}',
                      "complete", "m1", scope="output", overwrite=True)
    _review(row, 1)
    calls = _fake_registration(monkeypatch, [])
    with pytest.raises(ValueError, match=r"plan.tasks no longer lists row 'b' of the fork at task; ask_manager to "
                                         r"pipeline_rework at design$"):
        asyncio.run(pipeline_advance_impl(row, "K", "pr"))
    assert calls == [] and set(_fold().tokens) == {"t1", "t1/a"}
    assert "token_retired" not in [e["type"] for e in _events()]


def test_a_hold_on_a_forked_work_whose_rows_all_pend_says_no_row_has_started(fresh_orchestrator_dir, tmp_path,
                                                                              monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b"}]}')
    _fake_registration(monkeypatch, ["no_register", "no_register"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    for reason in ("wait", None):
        with pytest.raises(ValueError, match=r"t1 is forked and no row token exists yet: once a row's spawn commits, "
                                             r"hold or release it with its token_id \(pipeline_status lists it\)$"):
            asyncio.run(pipeline_advance_impl("m1", "K", "?", reason=reason))
    assert _fold().held == {}


def test_a_cwd_on_a_rows_self_loop_is_refused_and_changes_nothing(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b"}, {"id": 3, "row": "a"}]}')
    _fake_registration(monkeypatch, ["registered", "registered"])
    row = asyncio.run(pipeline_advance_impl(sid, "K", "task"))["rows"][0]["next"]["sid"]
    _review(row, 1)
    calls = _fake_registration(monkeypatch, [])
    with pytest.raises(ValueError, match="cwd moves a single token"):
        asyncio.run(pipeline_advance_impl(row, "K", "task", cwd=str(tmp_path)))
    f = _fold()
    assert calls == [] and f.tokens["t1/a"].index == 1 and f.open_requests == []


def test_pipeline_status_names_the_row_token_of_an_open_request(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b"}]}')
    _fake_registration(monkeypatch, ["registered", "no_register"])
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    request_id = r["rows"][1]["request_id"]
    lines = pipeline_status_impl("K").splitlines()
    assert any(line.startswith(f"- open request {request_id} token=t1/b design->task kind=fork") for line in lines)


def test_a_second_stuck_row_is_paged_inside_the_first_rows_window(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                   clock):
    start = clock["t"]
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b"}]}')
    _fake_registration(monkeypatch, ["no_register", "no_register"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    for p in paths.ASSIGNMENTS_PENDING.glob("*.json"):
        p.unlink()
    calls = _fake_registration(monkeypatch, [], spawn_error=OSError("tmux down"))
    clock["t"] = start + 400
    stuck = ('ADVANCE_STUCK K task spawn-error → pipeline_status("K"); the engine retries the spawn each '
             "requested_ttl_sec")
    assert pr.reconcile(MGR) == [stuck]
    clock["t"] = start + 410
    assert pr.reconcile(MGR) == [stuck]
    assert [c["name"] for c in calls] == ["K-task-1-r2", "K-task-2-r2"]


def test_each_held_row_is_paged_and_neither_is_restarted(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b"}]}')
    _fake_registration(monkeypatch, ["registered", "registered"])
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    for token_id in ("t1/a", "t1/b"):
        asyncio.run(pipeline_advance_impl("m1", "K", "?", reason="wait for the contracts", token_id=token_id))
    _dead(sid, *(x["next"]["sid"] for x in r["rows"]))
    calls = _fake_registration(monkeypatch, [])
    lines = pr.reconcile(MGR)
    assert ('NODE_HELD K task K-task-1 → once the reason clears: pipeline_advance(claude_sid=<your sid>, '
            'task_key="K", to="?", token_id="t1/a") with no reason releases it, or move it; if it will never run '
            'again: pipeline_close(manager_sid=<your sid>, task_key="K", reason=<why>); this page does not repeat — '
            'wait for the contracts') in lines
    assert any(line.startswith("NODE_HELD K task K-task-2 → ") and 'token_id="t1/b"' in line for line in lines)
    assert calls == [] and not any(line.startswith("NODE_RESTARTED") for line in lines)
    assert not any(line.startswith("NODE_HELD") for line in pr.reconcile(MGR, now=_later(3700)))


def _done_heads(capsys):
    capsys.readouterr()
    monitor.run_done_scan(MGR)
    return [line.split(" → ", 1)[0] for line in capsys.readouterr().out.splitlines()]


def _done_lane_events():
    return [e["fields"]["worker"] for e in _events() if e["type"] == "lane_event" and e["kind"] == "DONE"]


def test_the_done_of_a_forking_node_prints_while_one_of_its_rows_pends(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                      capsys):
    sid, aid = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b"}]}')
    _fake_registration(monkeypatch, ["registered", "no_register"])
    assert asyncio.run(pipeline_advance_impl(sid, "K", "task"))["state"] == "committed"
    assert state.read_json(paths.assignment_path(sid))["handed_off_at"] > 0 and len(_fold().open_requests) == 1
    worker_done_impl(sid, "forked")
    assert _done_heads(capsys) == ["DONE K-design"] and _done_lane_events() == []


def test_the_done_of_a_row_that_joined_while_another_row_lives_goes_to_the_journal(fresh_orchestrator_dir, tmp_path,
                                                                                  monkeypatch, capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=FORK)
    _design_done(sid, plan='{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b"}]}')
    _fake_registration(monkeypatch, ["registered"] * 2)
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    row_a = r["rows"][0]["next"]["sid"]
    _review(row_a, 1)
    assert asyncio.run(pipeline_advance_impl(row_a, "K", "pr"))["state"] == "joined"
    worker_done_impl(sid, "forked")
    worker_done_impl(row_a, "row a done")
    assert _done_heads(capsys) == [] and sorted(_done_lane_events()) == ["K-design", "K-task-1"]
