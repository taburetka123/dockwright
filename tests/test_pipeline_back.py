import asyncio
import json
import time
import types

import pytest

from dockwright import config, mcp_server, paths, pipeline, state, transcript
from dockwright import pipeline_reconcile as pr
from dockwright.mcp_server import (_handed_off, artifact_get_impl, artifact_put_impl, pipeline_advance_impl,
                                   pipeline_rework_impl, pipeline_start_impl)
from tests.test_pipeline_core import FORK, SCHEMA
from tests.test_pipeline_fork import PLAN, _design_done
from tests.test_pipeline_reconcile import (MGR, _close_session, _events, _fake_registration, _fold,  # noqa: F401
                                           _later, _manager, _of, _setup_schema, _started_at_design,
                                           fresh_orchestrator_dir, killed, session_logs)
from tests.test_pipeline_rework import _dead, _register, _work

BACK = (FORK.replace('version = 1\n', 'version = 1\nrework_at = "design"\n', 1)
        .replace('outputs = ["review.task-{i}"]',
                 'outputs = ["review.task-{i}", "implement.task-{i}"]\ncheckpoints = ["implement.state-{i}"]')
        .replace('outputs = ["implement.repo"]', 'outputs = ["implement.repo"]\ncheckpoints = ["ship.notes"]')
        + '\n[[edges]]\nfrom = "pr"\nto = "task"\nback = true\n')
INTAKE = (SCHEMA.replace('version = 1\n', 'version = 1\nrework_at = "design"\n\n[nodes.intake]\nrole = "SCOUT"\nmodel = "@sonnet"\neffort = "xhigh"\n'
                                          'brief = "briefs/intake.md"\ninputs = []\noutputs = ["context.ticket"]\n', 1)
          + '\n[[edges]]\nfrom = "intake"\nto = "design"\n')
SHORT = (FORK.replace('version = 1\n', 'version = 1\n\n[nodes.intake]\nrole = "SCOUT"\nmodel = "@sonnet"\neffort = "xhigh"\nbrief = "briefs/intake.md"\n'
                                       'inputs = []\noutputs = ["context.ask"]\n', 1)
         + '\n[[edges]]\nfrom = "intake"\nto = "design"\n[[edges]]\nfrom = "intake"\nto = "task"\nfork = true\n'
           'exit_checks = [{ type = "artifact", name = "plan.tasks" }]\n')
REASON = "staging: 500 on /x"
BACK_LINE = f"The pr node returned the work to this node: {REASON}. Your checkpoints: implement.state-3."
RETURN_LINE = "The work came back to this node after a return; continue from your checkpoints: ship.notes"
ONE_ROW = '{"tasks": [{"id": 1, "cwd": "/w1"}]}'


def _url(i):
    return f"https://github.com/o/r{i}/pull/{i}"


@pytest.fixture(autouse=True)
def prs(monkeypatch):
    fake = types.SimpleNamespace(states={}, drafts={}, calls=[])

    def draft_state(url, cwd):
        fake.calls.append(url)
        return fake.drafts.get(url, ("OPEN", True))

    def state_of(url, cwd):
        fake.calls.append(url)
        return fake.states.get(url, "MERGED")

    monkeypatch.setattr("dockwright.mcp_server._pr_draft_state", draft_state)
    monkeypatch.setattr("dockwright.mcp_server._pr_state", state_of)
    return fake


@pytest.fixture
def context(monkeypatch):
    held = {}
    monkeypatch.setattr(transcript, "context_tokens", lambda sid, runtime="claude": held.get(sid))
    return held


@pytest.fixture
def sends(monkeypatch):
    fake = types.SimpleNamespace(calls=[], result=None, error=None, refused=[])

    async def send(worker, text, *args, **kwargs):
        fake.calls.append((worker, text))
        record = state.read_json(paths.CLOSED / f"{fake.sid}.json") or {}
        try:
            mcp_server._refuse_handed_off_node(record.get("name") or worker, fake.sid)
        except ValueError as err:
            fake.refused.append(str(err))
        if fake.error is not None:
            raise fake.error
        return fake.result or {"status": "delivered", "worker": worker, "confirmed": True, "resumed": True}

    monkeypatch.setattr("dockwright.mcp_server.send_manager_to_worker_auto_impl", send)
    fake.sid = None
    return fake


def _finish(sid, i):
    artifact_put_impl("K", "implement", f"state-{i}", "half", "partial", sid, scope="checkpoint", overwrite=True)
    artifact_put_impl("K", "review", f"task-{i}", "APPROVE", "complete", sid, scope="output", overwrite=True)
    artifact_put_impl("K", "implement", f"task-{i}", _url(i), "complete", sid, scope="output", overwrite=True)


def _at_ship(monkeypatch, tmp_path, schema_text=BACK):
    design, _ = _started_at_design(monkeypatch, tmp_path, cwd=_work(tmp_path), schema_text=schema_text)
    _design_done(design)
    calls = _register(monkeypatch, ["registered"] * 5)
    sids = {"design": design, "calls": calls}
    r = asyncio.run(pipeline_advance_impl(design, "K", "task"))
    sids[1] = r["next"]["sid"]
    _finish(sids[1], 1)
    r = asyncio.run(pipeline_advance_impl(sids[1], "K", "pr"))
    sids[2], sids[3] = (x["next"]["sid"] for x in r["rows"])
    _finish(sids[2], 2)
    sids[4] = asyncio.run(pipeline_advance_impl(sids[2], "K", "task"))["next"]["sid"]
    _finish(sids[3], 3)
    assert asyncio.run(pipeline_advance_impl(sids[3], "K", "pr"))["state"] == "joined"
    _finish(sids[4], 4)
    r = asyncio.run(pipeline_advance_impl(sids[4], "K", "pr"))
    assert r["next"]["name"] == "K-pr"
    sids["pr"], sids["pr_aid"] = r["next"]["sid"], r["next"]["assignment_id"]
    artifact_put_impl("K", "ship", "notes", "staging run 1", "partial", sids["pr"], scope="checkpoint")
    _dead(design, sids[1], sids[2], sids[4])
    return sids


def _aid(sid):
    return state.read_json(paths.assignment_path(sid))["assignment_id"]


def _back(sid, row="web", reason=REASON):
    return asyncio.run(pipeline_advance_impl(sid, "K", "task", row=row, reason=reason))


def _prompt(calls, name):
    return next(c["initial_prompt"] for c in calls if c["name"] == name)


def test_the_ship_holder_sends_one_row_back_and_the_other_rows_stay_done(fresh_orchestrator_dir, tmp_path,
                                                                         monkeypatch):
    sids = _at_ship(monkeypatch, tmp_path)
    _dead(sids[3])
    calls = _register(monkeypatch, ["registered"])
    r = _back(sids["pr"])
    assert r["state"] == "committed" and r["next"]["name"] == "K-task-3"
    [back] = _of("token_back")
    assert (back["token_id"], back["from"], back["to"], back["row"], back["index"], back["reason"]) == (
        "t1", "pr", "task", "web", 3, REASON)
    f = _fold()
    assert f.tokens["t1"].forked and f.tokens["t1"].holder is None
    assert f.tokens["t1/web"].holder == r["next"]["assignment_id"] and f.tokens["t1/web"].index == 3
    assert {n: row["state"] for n, row in f.fork["rows"].items()} == {"contracts": "done", "svc": "done",
                                                                       "web": "started"}
    prompt = _prompt(calls, "K-task-3")
    assert BACK_LINE in prompt and calls[0]["cwd"] == "/f"
    assert _handed_off(sids["pr"], "K")


def test_a_failed_resume_falls_back_to_a_fresh_session_under_the_same_request(fresh_orchestrator_dir, tmp_path,
                                                                             monkeypatch, context, sends, killed):
    sids = _at_ship(monkeypatch, tmp_path)
    _close_session(sids[3], "K-task-3")
    context[sids[3]] = 120000
    sends.sid = sids[3]
    sends.error = RuntimeError("tab did not open")
    calls = _register(monkeypatch, ["registered"])
    r = _back(sids["pr"])
    assert sends.calls and r["state"] == "committed" and r["next"]["name"] == "K-task-3"
    assert [c["name"] for c in calls] == ["K-task-3"] and _fold().superseded == set()
    assert _fold().tokens["t1/web"].holder == r["next"]["assignment_id"] != _aid(sids[3])
    assert killed == []


def test_an_unconfirmed_resume_kills_the_old_session_and_spawns_a_fresh_one(fresh_orchestrator_dir, tmp_path,
                                                                           monkeypatch, context, sends, killed):
    sids = _at_ship(monkeypatch, tmp_path)
    _close_session(sids[3], "K-task-3")
    context[sids[3]] = 120000
    sends.sid = sids[3]
    sends.result = {"status": "delivered", "worker": "K-task-3-2", "confirmed": False, "resumed": True,
                    "sid": sids[3]}
    calls = _register(monkeypatch, ["registered"])
    r = _back(sids["pr"])
    assert killed == [sids[3]] and [c["name"] for c in calls] == ["K-task-3"]
    assert _fold().tokens["t1/web"].holder == r["next"]["assignment_id"] != _aid(sids[3])


def test_the_previous_row_session_below_the_threshold_is_resumed(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                context, sends):
    sids = _at_ship(monkeypatch, tmp_path)
    old = sids[3]
    _close_session(old, "K-task-3")
    context[old] = 120000
    sends.sid = old
    calls = _register(monkeypatch, [])
    r = _back(sids["pr"])
    assert calls == [] and r["state"] == "committed" and r.get("resumed") is True
    assert sends.calls == [("K-task-3", f"The pr node returned the work to this node: {REASON}")]
    assert sends.refused == []
    assert _fold().tokens["t1/web"].holder == _aid(old) and not _handed_off(old, "K")
    assert "handed_off_at" not in state.read_json(paths.assignment_path(old))
    assert _of("holder_restamped")[-1]["new"] == _aid(old)
    assert _of("advance_committed")[-1]["resumed"] is True


@pytest.mark.parametrize("tokens", [500000, None])
def test_the_previous_row_session_at_the_threshold_or_without_a_transcript_gives_a_fresh_one(
        fresh_orchestrator_dir, tmp_path, monkeypatch, context, sends, tokens):
    sids = _at_ship(monkeypatch, tmp_path)
    _close_session(sids[3], "K-task-3")
    context[sids[3]] = tokens
    calls = _register(monkeypatch, ["registered"])
    r = _back(sids["pr"])
    assert sends.calls == [] and [c["name"] for c in calls] == ["K-task-3"]
    assert "resume_candidate" not in _of("advance_requested")[-1]
    assert _fold().tokens["t1/web"].holder == r["next"]["assignment_id"]


def test_the_handed_off_sweep_spares_the_session_being_resumed(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                              context, killed):
    sids = _at_ship(monkeypatch, tmp_path)
    context[sids[3]] = 120000
    monkeypatch.setattr("dockwright.mcp_server._manager_sid_for", lambda task_key, assignment: None)
    assert _back(sids["pr"])["state"] == "pending"
    assert _fold().open_requests[0]["resume_candidate"] == _aid(sids[3])
    _dead(sids["pr"])
    real = config.pipeline_int
    monkeypatch.setattr(config, "pipeline_int", lambda key, default: 0 if key == "handed_off_grace_sec"
                        else real(key, default))
    pr.reconcile(MGR)
    assert killed == []


def test_a_pended_back_spawn_keeps_its_line_when_the_reconciler_retries_it(fresh_orchestrator_dir, tmp_path,
                                                                           monkeypatch):
    sids = _at_ship(monkeypatch, tmp_path)
    _dead(sids[3])
    _register(monkeypatch, ["timeout"])
    assert _back(sids["pr"])["state"] == "pending"
    for p in paths.ASSIGNMENTS_PENDING.glob("*.json"):
        p.unlink()
    calls = _register(monkeypatch, ["registered"])
    pr.reconcile(MGR, now=_later(400))
    assert [c["name"] for c in calls] == ["K-task-3-r2"]
    assert BACK_LINE in calls[0]["initial_prompt"]
    assert _fold().tokens["t1/web"].row == "web"


def test_a_returned_row_replaced_at_the_threshold_keeps_the_return_line(fresh_orchestrator_dir, tmp_path,
                                                                        monkeypatch, context):
    sids = _at_ship(monkeypatch, tmp_path)
    _dead(sids[3])
    _register(monkeypatch, ["registered"])
    row = _back(sids["pr"])["next"]["sid"]
    context[row] = 600000
    assert artifact_put_impl("K", "implement", "state-3", "more", "partial", row, scope="checkpoint",
                             overwrite=True)["refresh"] is True
    _dead(row)
    calls = _register(monkeypatch, ["registered"])
    pr.reconcile(MGR)
    assert [c["name"] for c in calls] == ["K-task-3-r2"]
    prompt = calls[0]["initial_prompt"]
    assert BACK_LINE in prompt and prompt.index(mcp_server._RESTART_LINE) < prompt.index(BACK_LINE)


def test_a_row_that_was_never_returned_restarts_without_the_return_line(fresh_orchestrator_dir, tmp_path,
                                                                        monkeypatch, context):
    design, _ = _started_at_design(monkeypatch, tmp_path, cwd=_work(tmp_path), schema_text=BACK)
    _design_done(design)
    _register(monkeypatch, ["registered"])
    row = asyncio.run(pipeline_advance_impl(design, "K", "task"))["next"]["sid"]
    context[row] = 600000
    assert artifact_put_impl("K", "implement", "state-1", "half", "partial", row, scope="checkpoint")["refresh"]
    _dead(design, row)
    calls = _register(monkeypatch, ["registered"])
    pr.reconcile(MGR)
    assert [c["name"] for c in calls] == ["K-task-1-r2"]
    prompt = calls[0]["initial_prompt"]
    assert mcp_server._RESTART_LINE in prompt and "returned the work" not in prompt


def _pended_back(monkeypatch, tmp_path, context, sends):
    sids = _at_ship(monkeypatch, tmp_path)
    _close_session(sids[3], "K-task-3")
    context[sids[3]] = 120000
    sends.sid = sids[3]
    with monkeypatch.context() as m:
        m.setattr("dockwright.mcp_server._manager_sid_for", lambda task_key, assignment: None)
        assert _back(sids["pr"])["state"] == "pending"
    assert _fold().open_requests[0]["resume_candidate"] == _aid(sids[3]) and sends.calls == []
    return sids[3]


def test_the_reconciler_resumes_a_pended_back_request_below_the_threshold(fresh_orchestrator_dir, tmp_path,
                                                                          monkeypatch, context, sends):
    old = _pended_back(monkeypatch, tmp_path, context, sends)
    calls = _register(monkeypatch, [])
    pr.reconcile(MGR, now=_later(400))
    assert calls == []
    assert sends.calls == [("K-task-3", f"The pr node returned the work to this node: {REASON}")]
    assert sends.refused == []
    assert _fold().tokens["t1/web"].holder == _aid(old) and not _handed_off(old, "K")
    assert _of("advance_committed")[-1]["resumed"] is True


def test_the_reconciler_starts_a_fresh_session_when_the_candidate_no_longer_qualifies(
        fresh_orchestrator_dir, tmp_path, monkeypatch, context, sends):
    old = _pended_back(monkeypatch, tmp_path, context, sends)
    context[old] = 600000
    calls = _register(monkeypatch, ["registered"])
    pr.reconcile(MGR, now=_later(400))
    assert sends.calls == [] and [c["name"] for c in calls] == ["K-task-3-r2"]
    assert BACK_LINE in calls[0]["initial_prompt"]
    assert _fold().tokens["t1/web"].holder not in (None, _aid(old))


def test_an_attached_live_row_session_is_never_the_resume_candidate(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                     context, sends, killed):
    sids = _at_ship(monkeypatch, tmp_path)
    old = sids[3]
    record = state.read_json(paths.ACTIVE / f"{old}.json")
    state.write_json_atomic(paths.ACTIVE / f"{old}.json", {**record, "human_attached_at": time.time()})
    context[old] = 120000
    calls = _register(monkeypatch, ["registered"])
    r = _back(sids["pr"])
    assert r["state"] == "committed" and sends.calls == [] and [c["name"] for c in calls] == ["K-task-3-2"]
    assert "resume_candidate" not in _of("advance_requested")[-1]
    assert killed == [] and _fold().tokens["t1/web"].holder not in (None, _aid(old))


def test_an_attachment_during_a_pended_back_request_gives_the_row_a_fresh_session(fresh_orchestrator_dir, tmp_path,
                                                                                    monkeypatch, context, sends,
                                                                                    killed):
    sids = _at_ship(monkeypatch, tmp_path)
    old = sids[3]
    context[old] = 120000
    with monkeypatch.context() as m:
        m.setattr("dockwright.mcp_server._manager_sid_for", lambda task_key, assignment: None)
        assert _back(sids["pr"])["state"] == "pending"
    assert _fold().open_requests[0]["resume_candidate"] == _aid(old)
    record = state.read_json(paths.ACTIVE / f"{old}.json")
    state.write_json_atomic(paths.ACTIVE / f"{old}.json", {**record, "human_attached_at": time.time()})
    calls = _register(monkeypatch, ["registered"])
    pr.reconcile(MGR, now=_later(400))
    assert sends.calls == [] and [c["name"] for c in calls] == ["K-task-3-r2"]
    assert "K-task-3" not in killed and _fold().tokens["t1/web"].holder not in (None, _aid(old))


def test_a_back_edge_needs_a_reason_a_row_and_the_holder(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sids = _at_ship(monkeypatch, tmp_path)
    calls = _register(monkeypatch, [])
    for sid, row, reason, match in (
            (sids["pr"], "web", None, "back edge pr -> task needs reason: what failed and its evidence"),
            (sids["pr"], None, REASON, "back edge pr -> task needs row: one of contracts, svc, web"),
            (sids["pr"], "nope", REASON, "row 'nope' is not a row of plan.tasks; its rows are contracts, svc, web"),
            ("m1", "web", REASON, "pr -> task is a back edge: a back edge is the holder's")):
        with pytest.raises(ValueError, match=match):
            asyncio.run(pipeline_advance_impl(sid, "K", "task", row=row, reason=reason))
    assert calls == [] and _of("token_back") == [] and _fold().tokens["t1"].node == "pr"


def _return(row_sid):
    r = asyncio.run(pipeline_advance_impl(row_sid, "K", "pr"))
    return r["next"]["sid"]


def test_the_eleventh_return_of_a_row_is_refused(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sids = _at_ship(monkeypatch, tmp_path)
    ship = sids["pr"]
    _dead(sids[3])
    for _ in range(10):
        _register(monkeypatch, ["registered", "registered"])
        row = _back(ship)["next"]["sid"]
        _dead(ship)
        ship = _return(row)
        _dead(row)
    with pytest.raises(ValueError, match="the work went back to web 10 times this round; ask_manager"):
        _back(ship)
    assert len(_of("token_back")) == 10


def test_a_merged_row_pr_refuses_the_return(fresh_orchestrator_dir, tmp_path, monkeypatch, prs):
    sids = _at_ship(monkeypatch, tmp_path)
    prs.drafts[_url(3)] = ("MERGED", False)
    calls = _register(monkeypatch, [])
    with pytest.raises(ValueError, match=f"PR {_url(3)} of row web has merged; a return after a merge is new work: "
                                         "ask_manager"):
        _back(sids["pr"])
    assert calls == [] and _of("token_back") == [] and prs.calls == [_url(3)]


def test_the_returned_row_joins_and_the_ship_node_comes_back_as_a_return(fresh_orchestrator_dir, tmp_path,
                                                                         monkeypatch):
    sids = _at_ship(monkeypatch, tmp_path)
    _dead(sids[3])
    calls = _register(monkeypatch, ["registered", "registered"])
    row = _back(sids["pr"])["next"]["sid"]
    _dead(sids["pr"])
    r = asyncio.run(pipeline_advance_impl(row, "K", "pr"))
    assert r["next"]["name"] == "K-pr"
    assert RETURN_LINE in _prompt(calls, "K-pr")
    f = _fold()
    assert list(f.tokens) == ["t1"] and f.tokens["t1"].node == "pr" and not f.tokens["t1"].forked


MERGER = (BACK.replace('trigger = "merge"', 'trigger = "approve"')
          .replace('outputs = ["summary.post-merge"]', 'outputs = ["summary.post-merge"]\ncheckpoints = ["merge.state"]')
          + '\n[[edges]]\nfrom = "post-merge"\nto = "task"\nback = true\n')


MERGER_RETURN_LINE = "The work came back to this node after a return; continue from your checkpoints: merge.state"
APPROVED = "o/r3#3 ready abc1234 APPROVED CLEAN none"


def _merger_sent_web_back(monkeypatch, tmp_path, outcomes):
    sids = _at_ship(monkeypatch, tmp_path, schema_text=MERGER)
    _register(monkeypatch, ["registered"])
    merger = asyncio.run(pipeline_advance_impl("m1", "K", "post-merge", reason=APPROVED))["next"]["sid"]
    artifact_put_impl("K", "merge", "state", "waiting", "partial", merger, scope="checkpoint")
    _dead(sids["pr"], sids[3])
    calls = _register(monkeypatch, outcomes)
    row = _back(merger)["next"]["sid"]
    _dead(merger)
    return row, calls


def test_a_return_from_the_approve_target_comes_back_to_the_approve_target(fresh_orchestrator_dir, tmp_path,
                                                                           monkeypatch):
    row, calls = _merger_sent_web_back(monkeypatch, tmp_path, ["registered", "registered"])
    r = asyncio.run(pipeline_advance_impl(row, "K", "pr"))
    assert r["next"]["name"] == "K-post-merge"
    assert MERGER_RETURN_LINE in _prompt(calls, "K-post-merge")
    f = _fold()
    assert list(f.tokens) == ["t1"] and f.tokens["t1"].node == "post-merge" and not f.tokens["t1"].forked
    assert _of("token_back")[-1]["from_index"] == 1
    requested = _of("advance_requested")[-1]
    assert requested["edge"] == {"from": "task", "to": "post-merge"} and requested["return"] is True


def test_a_single_row_work_returns_to_the_approve_target(fresh_orchestrator_dir, tmp_path, monkeypatch):
    design, _ = _started_at_design(monkeypatch, tmp_path, cwd=_work(tmp_path), schema_text=MERGER)
    _design_done(design, plan=ONE_ROW)
    _register(monkeypatch, ["registered", "registered", "registered", "registered", "registered"])
    task = asyncio.run(pipeline_advance_impl(design, "K", "task"))["next"]["sid"]
    _finish(task, 1)
    ship = asyncio.run(pipeline_advance_impl(task, "K", "pr"))["next"]["sid"]
    _dead(design, task)
    merger = asyncio.run(pipeline_advance_impl("m1", "K", "post-merge", reason=APPROVED))["next"]["sid"]
    _dead(ship)
    task = asyncio.run(pipeline_advance_impl(merger, "K", "task", reason=REASON))["next"]["sid"]
    _dead(merger)
    r = asyncio.run(pipeline_advance_impl(task, "K", "pr"))
    assert r["next"]["name"] == "K-post-merge" and _fold().tokens["t1"].node == "post-merge"


def test_a_restarted_returned_row_still_returns_to_its_sender(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                              context):
    row, _ = _merger_sent_web_back(monkeypatch, tmp_path, ["registered"])
    context[row] = 600000
    assert artifact_put_impl("K", "implement", "state-3", "more", "partial", row, scope="checkpoint",
                             overwrite=True)["refresh"] is True
    _dead(row)
    calls = _register(monkeypatch, ["registered", "registered"])
    pr.reconcile(MGR)
    assert [c["name"] for c in calls] == ["K-task-3-r2"]
    restarted = _of("advance_committed")[-1]["sid"]
    assert asyncio.run(pipeline_advance_impl(restarted, "K", "pr"))["next"]["name"] == "K-post-merge"


def test_a_pended_return_is_retried_at_its_sender(fresh_orchestrator_dir, tmp_path, monkeypatch):
    row, _ = _merger_sent_web_back(monkeypatch, tmp_path, ["registered", "timeout"])
    assert asyncio.run(pipeline_advance_impl(row, "K", "pr"))["state"] == "pending"
    for p in paths.ASSIGNMENTS_PENDING.glob("*.json"):
        p.unlink()
    calls = _register(monkeypatch, ["registered"])
    pr.reconcile(MGR, now=_later(400))
    assert [c["name"] for c in calls] == ["K-post-merge-r2"]
    assert _fold().tokens["t1"].node == "post-merge"


CLOSER = (SCHEMA.replace('trigger = "merge"', 'trigger = "approve"')
          .replace('outputs = ["summary.post-merge"]', 'outputs = ["summary.post-merge"]\ncheckpoints = ["merge.state"]')
          + '\n[nodes.close]\nrole = "SCOUT"\nmodel = "@sonnet"\neffort = "xhigh"\nbrief = "briefs/close.md"\n'
            'inputs = []\noutputs = []\n'
          + '\n[[edges]]\nfrom = "post-merge"\nto = "close"\n[[edges]]\nfrom = "close"\nto = "pr"\nback = true\n')


def test_a_trigger_out_of_a_back_stay_goes_to_its_own_target(fresh_orchestrator_dir, tmp_path, monkeypatch):
    schema_path = _setup_schema(monkeypatch, tmp_path, schema_text=CLOSER)
    (schema_path.parent / "briefs" / "close.md").write_text("# close brief for {task_key}\n")
    _manager()
    _register(monkeypatch, ["registered"] * 7)
    design = asyncio.run(pipeline_start_impl("m1", "K", "code-ticket", _work(tmp_path), "ask"))["next"]["sid"]
    _design_done(design, plan=ONE_ROW)
    task = asyncio.run(pipeline_advance_impl(design, "K", "task"))["next"]["sid"]
    artifact_put_impl("K", "review", "task-1", "APPROVE", "complete", task, scope="output")
    ship = asyncio.run(pipeline_advance_impl(task, "K", "pr"))["next"]["sid"]
    _dead(design, task)
    merger = asyncio.run(pipeline_advance_impl("m1", "K", "post-merge", reason=APPROVED))["next"]["sid"]
    _dead(ship)
    artifact_put_impl("K", "summary", "post-merge", "merged", "complete", merger, scope="output")
    closer = asyncio.run(pipeline_advance_impl(merger, "K", "close"))["next"]["sid"]
    _dead(merger)
    ship = asyncio.run(pipeline_advance_impl(closer, "K", "pr", reason=REASON))["next"]["sid"]
    _dead(closer, ship)
    asyncio.run(pipeline_advance_impl("m1", "K", "post-merge", reason=APPROVED))
    assert _fold().tokens["t1"].node == "post-merge"
    assert "return" not in _of("advance_requested")[-1]


_STAY_TOKEN = pipeline.Token("t1/web", "task", 3, None, "", None, "web")
_SENT_BACK = {"type": "token_back", "token_id": "t1", "from": "post-merge", "to": "task", "row": "web"}
_ROW_AT_TASK = {"type": "advance_committed", "token_id": "t1/web", "node": "task"}


@pytest.mark.parametrize("ender", [{"type": "token_override", "token_id": "t1", "to": "design"},
                                   {"type": "token_forked", "token_id": "t1", "node": "task"}])
def test_an_override_and_a_refork_end_a_back_stay(ender):
    assert mcp_server._back_stay([_SENT_BACK, ender, _ROW_AT_TASK], _STAY_TOKEN, "task") is None
    assert mcp_server._back_stay([_SENT_BACK, _ROW_AT_TASK], _STAY_TOKEN, "task") == _SENT_BACK


def test_a_single_row_work_goes_back_and_returns(fresh_orchestrator_dir, tmp_path, monkeypatch):
    design, _ = _started_at_design(monkeypatch, tmp_path, cwd=_work(tmp_path), schema_text=BACK)
    _design_done(design, plan=ONE_ROW)
    _register(monkeypatch, ["registered", "registered"])
    task = asyncio.run(pipeline_advance_impl(design, "K", "task"))["next"]["sid"]
    _finish(task, 1)
    ship = asyncio.run(pipeline_advance_impl(task, "K", "pr"))["next"]["sid"]
    artifact_put_impl("K", "ship", "notes", "staging run 1", "partial", ship, scope="checkpoint")
    _dead(design, task)
    calls = _register(monkeypatch, ["registered", "registered"])
    r = asyncio.run(pipeline_advance_impl(ship, "K", "task", reason=REASON))
    _dead(ship)
    assert r["next"]["name"] == "K-task-1" and _fold().tokens["t1"].node == "task"
    assert (f"The pr node returned the work to this node: {REASON}. Your checkpoints: implement.state-1."
            in _prompt(calls, "K-task-1"))
    assert asyncio.run(pipeline_advance_impl(r["next"]["sid"], "K", "pr"))["next"]["name"] == "K-pr"
    assert RETURN_LINE in _prompt(calls, "K-pr")
    assert [e["type"] for e in _events()].count("handed_off") == len(set(
        e["assignment_id"] for e in _events() if e["type"] == "handed_off"))


def test_the_merge_trigger_fires_after_a_return(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sids = _at_ship(monkeypatch, tmp_path)
    _dead(sids[3])
    _register(monkeypatch, ["registered", "registered", "registered"])
    row = _back(sids["pr"])["next"]["sid"]
    _dead(sids["pr"])
    ship = _return(row)
    urls = "\n".join(_url(i) for i in (1, 2, 3, 4))
    artifact_put_impl("K", "implement", "repo", urls, "complete", ship, scope="output")
    merged = json.dumps({"state": "MERGED", "mergedAt": "2026-10-05T10:00:00Z"})
    pr.reconcile(MGR, run_gh=lambda argv: merged)
    assert [e["edge"]["to"] for e in _of("trigger_fired")] == ["post-merge"]
    assert _fold().tokens["t1"].node == "post-merge"


def test_the_footer_names_the_back_edges(fresh_orchestrator_dir, tmp_path, monkeypatch):
    design, _ = _started_at_design(monkeypatch, tmp_path, cwd=_work(tmp_path), schema_text=BACK)
    _design_done(design, plan=ONE_ROW)
    calls = _register(monkeypatch, ["registered", "registered"])
    task = asyncio.run(pipeline_advance_impl(design, "K", "task"))["next"]["sid"]
    _finish(task, 1)
    asyncio.run(pipeline_advance_impl(task, "K", "pr"))
    prompt = _prompt(calls, "K-pr")
    assert "\nBack edges: task\n" in prompt
    assert ("To send the work back to a repo row: pipeline_advance(claude_sid=<your sid>, task_key=..., to=<node>, "
            "row=<row>, reason=<what failed and its evidence>). At most 10 returns per row per round; past that, "
            "ask_manager.") in prompt
    assert "call pipeline_advance only along a back edge." in prompt
    assert "Back edges:" not in _prompt(calls, "K-task-1")


def test_the_footer_names_the_rows_of_a_forked_back_edge_target(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sids = _at_ship(monkeypatch, tmp_path)
    assert "\nBack edges: task (rows: contracts, svc, web)\n" in _prompt(sids["calls"], "K-pr")


def test_a_rework_without_to_enters_rework_at(fresh_orchestrator_dir, tmp_path, monkeypatch):
    design, _ = _started_at_design(monkeypatch, tmp_path, cwd=_work(tmp_path), schema_text=BACK)
    _dead(design)
    _register(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_rework_impl("m1", "K", None, "redo the design"))
    assert r["round"] == 2 and r["next"]["name"] == "K-design" and _fold().tokens["t1"].node == "design"


def _at_intake(monkeypatch, tmp_path):
    schema_path = _setup_schema(monkeypatch, tmp_path, schema_text=INTAKE)
    (schema_path.parent / "briefs" / "intake.md").write_text("# intake brief for {task_key}\n")
    _manager()
    _register(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_start_impl("m1", "K", "code-ticket", _work(tmp_path), "ask"))
    return r["next"]["sid"]


def test_a_rework_into_the_start_node_needs_ticket_changed(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _dead(_at_intake(monkeypatch, tmp_path))
    calls = _register(monkeypatch, ["registered"])
    with pytest.raises(ValueError, match="a rework enters the start node intake only with ticket_changed=True; this "
                                         "schema's rework entry is design"):
        asyncio.run(pipeline_rework_impl("m1", "K", "intake", "the ticket moved"))
    assert calls == [] and _fold().round == 1
    r = asyncio.run(pipeline_rework_impl("m1", "K", "intake", "the ticket moved", ticket_changed=True))
    assert r["round"] == 2 and r["next"]["name"] == "K-intake"


def test_ticket_changed_marks_the_rework_ask(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _dead(_at_intake(monkeypatch, tmp_path))
    _register(monkeypatch, ["registered"])
    asyncio.run(pipeline_rework_impl("m1", "K", "intake", "the ticket moved", ticket_changed=True))
    assert artifact_get_impl("K", "context", "rework-2")["content"] == "ticket_changed: yes\nthe ticket moved"


def test_ticket_changed_needs_a_start_node_that_outputs_the_ticket(fresh_orchestrator_dir, tmp_path, monkeypatch):
    design, _ = _started_at_design(monkeypatch, tmp_path, cwd=_work(tmp_path), schema_text=BACK)
    _dead(design)
    calls = _register(monkeypatch, ["registered"])
    with pytest.raises(ValueError, match="ticket_changed needs a start node that outputs context.ticket; design "
                                         "does not"):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", "redo", ticket_changed=True))
    assert calls == [] and _fold().round == 1


def test_ticket_changed_is_refused_on_a_schema_without_rework_at_whose_start_outputs_no_ticket(fresh_orchestrator_dir,
                                                                                               tmp_path, monkeypatch):
    design, _ = _started_at_design(monkeypatch, tmp_path, cwd=_work(tmp_path),
                                   schema_text=BACK.replace('rework_at = "design"\n', ""))
    _dead(design)
    calls = _register(monkeypatch, ["registered"])
    with pytest.raises(ValueError, match="ticket_changed needs a start node that outputs context.ticket"):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", "redo", ticket_changed=True))
    assert calls == [] and _fold().round == 1


def test_ticket_changed_prefixes_the_ask_on_a_schema_without_rework_at(fresh_orchestrator_dir, tmp_path, monkeypatch):
    schema = BACK.replace('rework_at = "design"\n', "").replace(
        'outputs = ["context.ask", "design.hld", "plan.tasks"]', 'outputs = ["context.ask", "design.hld", "plan.tasks", "context.ticket"]', 1)
    design, _ = _started_at_design(monkeypatch, tmp_path, cwd=_work(tmp_path), schema_text=schema)
    _dead(design)
    _register(monkeypatch, ["registered"])
    asyncio.run(pipeline_rework_impl("m1", "K", "design", "redo", ticket_changed=True))
    assert artifact_get_impl("K", "context", "rework-2")["content"] == "ticket_changed: yes\nredo"


def test_the_rework_tool_schema_states_the_entry_rule_by_property():
    from tests.test_tool_interfaces import _listed
    tool = _listed()["pipeline_rework"]
    props = tool.inputSchema["properties"]
    assert "On a schema with rework_at the start node is open only with ticket_changed=True" in props["to"]["description"]
    assert "Refused on a schema whose start node does not output context.ticket" in props["ticket_changed"]["description"]
    assert "when ticket_changed=True names a start node without context.ticket" in tool.description


def test_an_old_schema_rework_is_unchanged(fresh_orchestrator_dir, tmp_path, monkeypatch):
    design, _ = _started_at_design(monkeypatch, tmp_path, cwd=_work(tmp_path))
    _dead(design)
    calls = _register(monkeypatch, ["registered"])
    with pytest.raises(ValueError, match="name to: the schema declares no rework_at"):
        asyncio.run(pipeline_rework_impl("m1", "K", None, "redo"))
    assert calls == [] and _fold().round == 1
    r = asyncio.run(pipeline_rework_impl("m1", "K", "design", "redo"))
    assert r["round"] == 2 and r["next"]["name"] == "K-design"
    assert artifact_get_impl("K", "context", "rework-2")["content"] == "redo"


def test_an_override_into_the_start_node_after_it_left_is_refused(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _dead(_at_intake(monkeypatch, tmp_path))
    _register(monkeypatch, ["registered", "registered"])
    intake = asyncio.run(pipeline_advance_impl("m1", "K", "intake", override=True, reason="again"))["next"]["sid"]
    assert "via" not in _of("token_override")[-1]
    artifact_put_impl("K", "context", "ticket", "the ticket", "complete", intake, scope="output")
    assert asyncio.run(pipeline_advance_impl(intake, "K", "design"))["next"]["name"] == "K-design"
    calls = _register(monkeypatch, [])
    with pytest.raises(ValueError, match="override into the start node intake after it left: use pipeline_rework"):
        asyncio.run(pipeline_advance_impl("m1", "K", "intake", override=True, reason="again"))
    assert calls == [] and _fold().tokens["t1"].node == "design"


def test_round_pr_urls_cover_every_rows_implement_output(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_ship(monkeypatch, tmp_path)
    urls = mcp_server._round_pr_urls("K", mcp_server._load_snapshot_for("K"))
    assert urls == [_url(i) for i in (1, 2, 3, 4)]


def test_a_rework_while_a_row_holds_an_open_pr_names_the_token_holder(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                      prs):
    sids = _at_ship(monkeypatch, tmp_path)
    _dead(sids[3], sids["pr"])
    prs.states[_url(3)] = "OPEN"
    calls = _register(monkeypatch, [])
    with pytest.raises(ValueError, match=rf"pipeline_rework refused: PR {_url(3)} of this round is open\. Changes on "
                                         r"it go through the node that holds the work's token \(resume it and send "
                                         r"the ask\); a rework needs the PR merged or closed"):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", "redo"))
    assert calls == [] and _fold().round == 1


def test_the_start_node_forks_on_an_internal_plan_tasks(fresh_orchestrator_dir, tmp_path, monkeypatch):
    schema_path = _setup_schema(monkeypatch, tmp_path, schema_text=SHORT)
    (schema_path.parent / "briefs" / "intake.md").write_text("# intake brief for {task_key}\n")
    _manager()
    _register(monkeypatch, ["registered", "registered", "registered"])
    intake = asyncio.run(pipeline_start_impl("m1", "K", "code-ticket", _work(tmp_path), "ask"))["next"]["sid"]
    artifact_put_impl("K", "plan", "tasks", '{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b"}]}',
                      "complete", intake, scope="internal")
    r = asyncio.run(pipeline_advance_impl(intake, "K", "task"))
    assert [x["next"]["name"] for x in r["rows"]] == ["K-task-1", "K-task-2"]
    assert set(_fold().tokens) == {"t1", "t1/a", "t1/b"} and _fold().tokens["t1"].forked
