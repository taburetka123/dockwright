import asyncio
import json
import os
import re
import time

import pytest

from dockwright import mcp_server, paths
from dockwright import pipeline_reconcile as pr
from dockwright.mcp_server import (_commit_or_pend, _has_open_token, _write_question, answer_question_impl,
                                   artifact_put_impl, pipeline_advance_impl, pipeline_close_impl,
                                   pipeline_rework_impl, pipeline_status_impl)
from tests.test_pipeline_reconcile import (MGR, _at_pr_with_a_pr_url, _claim, _complete_design_outputs,  # noqa: F401
                                           _events, _fake_registration, _fold, _later, _manager, _of,
                                           _started_at_design, fresh_orchestrator_dir, killed, session_logs)
from tests.test_pipeline_rework import ASK, _dead, _helper, _pending_task_request, _register, _work, prs  # noqa: F401

CLOSED = (r"K was closed at \S+ \S+ on design: absorbed by repo-node; pipeline_rework reopens it once its round PR "
          r"is merged or closed")


def test_close_is_manager_only_and_writes_nothing(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    before = _events()
    with pytest.raises(ValueError, match="pipeline_close refused: pipeline_close is manager-only"):
        pipeline_close_impl(sid, "K", "absorbed by repo-node")
    assert _events() == before


def test_close_is_refused_for_a_key_with_no_token(fresh_orchestrator_dir):
    _manager()
    with pytest.raises(ValueError, match="pipeline_close refused: NOPE has no pipeline token; start a work with "
                                         "pipeline_start"):
        pipeline_close_impl("m1", "NOPE", "absorbed by repo-node")
    assert not paths.artifact_events_path("NOPE").exists()


def test_close_is_refused_while_the_holder_is_alive_and_for_an_empty_reason(fresh_orchestrator_dir, tmp_path,
                                                                            monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match=r"holder K-design \(sid-K-design\) of design is alive: kill_worker it, "
                                         r"then call pipeline_close again"):
        pipeline_close_impl("m1", "K", "absorbed by repo-node")
    _dead(sid)
    with pytest.raises(ValueError, match="pipeline_close refused: reason must say why the work will never run again"):
        pipeline_close_impl("m1", "K", "  ")
    assert _of("work_closed") == [] and _of("advance_refused") == [] and _fold().closed is None


def test_close_is_refused_while_a_session_of_an_open_request_is_alive(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _pending_task_request(monkeypatch, tmp_path)
    _claim("K-task-1", "sid-late")
    with pytest.raises(ValueError, match=r"session K-task-1 \(sid-late\) of open request \w+ is alive: kill_worker "
                                         r"it, then call pipeline_close again"):
        pipeline_close_impl("m1", "K", "absorbed by repo-node")
    assert _fold().closed is None


def test_close_is_refused_while_a_handed_off_session_of_the_work_is_alive(fresh_orchestrator_dir, tmp_path,
                                                                         monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    _dead("sid-K-task-1")
    with pytest.raises(ValueError, match=r"pipeline_close refused: handed-off session K-design \(sid-K-design\) of K "
                                         r"is alive: kill_worker it, then call pipeline_close again"):
        pipeline_close_impl("m1", "K", "absorbed by repo-node")
    assert _of("work_closed") == []
    _dead(sid)
    assert pipeline_close_impl("m1", "K", "absorbed by repo-node")["state"] == "closed"


def test_close_is_refused_while_a_question_of_the_work_is_pending(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _dead(sid)
    _helper("sid-helper", "K", tmp_path)
    qid = _write_question("sid-helper", "K-helper", "Hold the deploy?", parent_manager_name="mgr")
    with pytest.raises(ValueError, match=rf"pending question\(s\) of K: {qid}\. Answer each with answer_question, "
                                         rf"then call pipeline_close again"):
        pipeline_close_impl("m1", "K", "absorbed by repo-node")
    answer_question_impl(qid, "no")
    assert pipeline_close_impl("m1", "K", "absorbed by repo-node")["state"] == "closed"


def test_close_is_refused_while_a_gate_of_the_work_runs(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _dead(sid)
    out = str(tmp_path / "abc.r1.out")
    artifact_put_impl("K", "gate", "open", f'{{"round": 1, "node": "design", "pid": {os.getpid()}, "out": "{out}"}}',
                      "complete", "writer-r1", scope="checkpoint")
    monkeypatch.setattr(mcp_server, "_process_command", lambda pid, index=None: f"perl -e … {out} K 1 dockwright")
    with pytest.raises(ValueError, match=re.escape(f"the design gate still runs (supervisor pid {os.getpid()}): end "
                                                   f"it with kill -TERM -- -{os.getpid()}, then call pipeline_close "
                                                   "again")):
        pipeline_close_impl("m1", "K", "absorbed by repo-node")
    monkeypatch.setattr(mcp_server, "_process_command", lambda pid, index=None: "")
    assert pipeline_close_impl("m1", "K", "absorbed by repo-node")["state"] == "closed"


def test_closing_a_held_work_ends_it_and_every_move_on_it_is_refused(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    asyncio.run(pipeline_advance_impl("m1", "K", "?", reason="wait for the reporter"))
    _dead(sid)
    r = pipeline_close_impl("m1", "K", "absorbed by repo-node")
    assert r == {"ok": True, "state": "closed", "task_key": "K", "node": "design", "round": 1, "superseded": []}
    folded = _fold()
    assert folded.closed["node"] == "design" and folded.closed["reason"] == "absorbed by repo-node"
    assert folded.held == {} and folded.tokens["t1"].holder == aid
    assert _of("handed_off")[-1]["assignment_id"] == aid
    calls = _fake_registration(monkeypatch, ["registered"] * 5)
    for to, kw in (("task", {}), ("design", {"override": True, "reason": "redo"}), ("?", {"reason": "wait"}),
                   ("?", {})):
        with pytest.raises(ValueError, match="pipeline_advance refused: " + CLOSED):
            asyncio.run(pipeline_advance_impl("m1", "K", to, **kw))
    with pytest.raises(ValueError, match="pipeline_advance refused: " + CLOSED):
        asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    with pytest.raises(ValueError, match="pipeline_close refused: " + CLOSED):
        pipeline_close_impl("m1", "K", "again")
    assert calls == [] and _fold().tokens["t1"].node == "design"


def test_the_reconciler_neither_restarts_nor_pages_a_closed_work(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    asyncio.run(pipeline_advance_impl("m1", "K", "?", reason="wait for the reporter"))
    _dead(sid)
    pipeline_close_impl("m1", "K", "absorbed by repo-node")
    calls = _fake_registration(monkeypatch, [])
    before = len(_events())
    assert pr.reconcile(MGR) == [] and pr.reconcile(MGR, now=_later(3700)) == []
    out = []
    pr._reconcile_work(MGR, "K", time.time(), pr._Pages(MGR["name"]), None, out)
    assert out == [] and calls == [] and len(_events()) == before
    assert pr._restart_request("K", _fold().tokens["t1"], MGR) is None
    assert not pr.reconcilable("K") and "K" not in pr._reconcilable_task_keys() and not _has_open_token("K")


def test_the_reconciler_never_polls_the_pr_of_a_closed_work(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_pr_with_a_pr_url(monkeypatch, tmp_path)
    _dead("sid-K-design")
    _dead("sid-K-pr")
    pipeline_close_impl("m1", "K", "absorbed by repo-node")
    seen, out = [], []
    calls = _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR, run_gh=seen.append) == []
    pr._reconcile_work(MGR, "K", time.time(), pr._Pages(MGR["name"]), seen.append, out)
    assert out == [] and seen == [] and calls == [] and _fold().tokens["t1"].node == "pr"


def test_a_closed_work_on_a_terminal_node_is_not_reconcilable(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "post-merge", override=True, reason="jump"))
    _dead("sid-K-design")
    _dead("sid-K-post-merge")
    assert pr.reconcilable("K")
    pipeline_close_impl("m1", "K", "absorbed by repo-node")
    assert not pr.reconcilable("K") and "K" not in pr._reconcilable_task_keys()


def test_closing_a_work_supersedes_its_pending_request_and_nothing_settles_it(fresh_orchestrator_dir, tmp_path,
                                                                             monkeypatch):
    _pending_task_request(monkeypatch, tmp_path)
    request = _fold().open_requests[0]
    r = pipeline_close_impl("m1", "K", "absorbed by repo-node")
    assert r["superseded"] == [{"request_id": request["request_id"],
                                "assignment_id": request["pending"]["assignment_id"]}]
    assert _fold().open_requests == []
    calls = _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR) == [] and pr.reconcile(MGR, now=_later(700)) == [] and calls == []


def test_a_spawn_that_answers_after_the_close_closes_its_own_session(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                     killed):
    _pending_task_request(monkeypatch, tmp_path)
    request_id = _fold().open_requests[0]["request_id"]
    pipeline_close_impl("m1", "K", "absorbed by repo-node")
    out = _commit_or_pend("K", request_id, "t1", "task", 1,
                          {"status": "registered", "claude_sid": "sid-late", "assignment_id": "a-late"}, None)
    assert out["state"] == "superseded" and killed == ["sid-late"]
    assert _fold().tokens["t1"].node == "design"
    assert not any(e.get("request_id") == request_id for e in _of("advance_committed"))


def test_rework_reopens_a_closed_work(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path, cwd=_work(tmp_path))
    _dead(sid)
    pipeline_close_impl("m1", "K", "closed by mistake")
    _register(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))
    assert r["state"] == "committed" and r["round"] == 2
    assert _fold().closed is None and _fold().tokens["t1"].node == "design" and _has_open_token("K")


def test_status_of_a_closed_work_prints_the_close_and_waits_on_nothing(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _dead(sid)
    pipeline_close_impl("m1", "K", "absorbed\n  by repo-node")
    assert re.search(r"^- closed: \S+ \S+ on design — absorbed by repo-node$", pipeline_status_impl("K"), re.M)
    out = json.loads(pipeline_status_impl("K", "json"))
    assert out["closed"]["node"] == "design" and out["closed"]["reason"] == "absorbed\n  by repo-node"
    assert out["waiting_on"] == "none"


def test_a_close_during_a_reconcile_pass_stops_the_rest_of_that_pass(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_pr_with_a_pr_url(monkeypatch, tmp_path)
    _dead("sid-K-design")
    _dead("sid-K-pr")
    settle = pr._settle_requests

    def settle_then_close(*args, **kwargs):
        settled = settle(*args, **kwargs)
        pipeline_close_impl("m1", "K", "absorbed by repo-node")
        return settled

    monkeypatch.setattr(pr, "_settle_requests", settle_then_close)
    calls = _fake_registration(monkeypatch, [])
    seen, out = [], []
    pr._reconcile_work(MGR, "K", time.time(), pr._Pages(MGR["name"]), seen.append, out)
    assert out == [] and seen == [] and calls == [] and _fold().closed is not None


def test_a_restart_whose_spawn_fails_after_the_close_pages_nothing(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _dead(sid)

    async def close_then_fail(*args, **kwargs):
        pipeline_close_impl("m1", "K", "absorbed by repo-node")
        raise RuntimeError("tmux is gone")

    monkeypatch.setattr(mcp_server, "_spawn_node", close_then_fail)
    out = []
    pr._reconcile_work(MGR, "K", time.time(), pr._Pages(MGR["name"]), None, out)
    assert out == [] and _fold().closed is not None and _fold().open_requests == []
    assert [e["type"] for e in _events()][-4:] == ["advance_requested", "work_closed", "handed_off", "advance_pending"]


def test_a_close_during_the_merge_poll_pages_nothing(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_pr_with_a_pr_url(monkeypatch, tmp_path)
    _dead("sid-K-design", "sid-K-pr")
    calls = _fake_registration(monkeypatch, [])

    def close_then_report_merged(args):
        pipeline_close_impl("m1", "K", "absorbed by repo-node")
        return json.dumps({"state": "MERGED", "mergedAt": "2026-10-05T10:00:00Z"})

    assert pr.reconcile(MGR, run_gh=close_then_report_merged) == []
    assert calls == [] and _of("advance_refused") == [] and _fold().tokens["t1"].node == "pr"


def test_close_releases_the_work_s_leases_before_work_closed(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    mcp_server.acquire_worker_slot_impl(claude_sid=sid, category="env:example/svc-a", max_concurrent=1, task_key="K",
                                        node="ship", wait=False)
    mcp_server.acquire_worker_slot_impl(claude_sid=sid, category="env:example/svc-a", max_concurrent=1, task_key="L",
                                        node="post-merge", wait=False)
    with pytest.raises(ValueError, match="is alive"):
        pipeline_close_impl("m1", "K", "absorbed by repo-node")
    assert mcp_server.lease_waits("L")[0]["free"] is False
    _dead(sid)
    pipeline_close_impl("m1", "K", "absorbed by repo-node")
    assert _of("work_closed") and mcp_server.lease_waits("L")[0]["free"] is True


def test_close_passes_with_an_open_engineer_ask_on_the_work(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _dead(sid)
    qid = mcp_server.ask_engineer_impl("m1", "Keep the branch?", task_key="K")["question_id"]
    assert pipeline_close_impl("m1", "K", "absorbed by repo-node")["state"] == "closed"
    assert mcp_server._find_question_paths(qid) != []
