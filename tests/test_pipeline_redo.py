import asyncio
import json
import os
import re
import types

import pytest

from dockwright import board, mcp_server, paths, pipeline, state
from dockwright import pipeline_reconcile as pr
from dockwright.mcp_server import (_write_question, answer_question_impl, artifact_get_impl, artifact_put_impl,
                                   pipeline_advance_impl, pipeline_close_impl, pipeline_event_impl,
                                   pipeline_rework_impl, pipeline_status_impl)
from tests.test_pipeline_core import FORK_REDO, REDO
from tests.test_pipeline_fork import _design_done
from tests.test_pipeline_reconcile import (MGR, _claim, _fake_registration, _fold, _of,  # noqa: F401
                                           _started_at_design, fresh_orchestrator_dir, killed, session_logs)
from tests.test_pipeline_rework import _dead, _flat, _helper, _no_round, _register, _work

URL = "https://github.com/o/r/pull/7"
REASON = "Review on #7: the edge cannot carry the gate; it breaks decision 2"
QUESTION = f"Redesign request\nReview on {URL}#r1: the edge cannot carry the gate\nbreaks decision 2"
NO_PERMIT = "needs the engineer's explicit permission"


@pytest.fixture(autouse=True)
def prs(monkeypatch):
    fake = types.SimpleNamespace(states={}, drafts={}, calls=[])

    def draft_state(url, cwd):
        fake.calls.append(url)
        value = fake.drafts.get(url, ("OPEN", True))
        if isinstance(value, Exception):
            raise value
        return value

    def state_of(url, cwd):
        fake.calls.append(url)
        value = fake.states.get(url, "OPEN")
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr("dockwright.mcp_server._pr_draft_state", draft_state)
    monkeypatch.setattr("dockwright.mcp_server._pr_state", state_of)
    return fake


def _at_pr(monkeypatch, tmp_path, body=URL):
    design, _ = _started_at_design(monkeypatch, tmp_path, cwd=_work(tmp_path), schema_text=REDO)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="jump"))
    _dead(design)
    artifact_put_impl("K", "implement", "repo", body, "complete", "sid-K-pr", scope="output")
    artifact_put_impl("K", "summary", "thread", "where the work stands", "complete", "sid-K-pr", scope="output",
                      overwrite=True)
    return "sid-K-pr"


def _permit(sid, name="K-pr", answer="redesign: yes\nyes, the gate stays mine", decided_by="engineer",
            question=QUESTION):
    qid = _write_question(sid, name, question, parent_manager_name="mgr", task_key="K")
    answer_question_impl(qid, answer, decided_by=decided_by)
    return qid


def _redo(sid, to="design"):
    return asyncio.run(pipeline_advance_impl(sid, "K", to, reason=REASON))


def _refused(sid, match, to="design"):
    before = _flat()
    with pytest.raises(ValueError, match=match):
        _redo(sid, to)
    assert _no_round() and _flat() == before
    assert _of("advance_refused")[-1]["to"] == to and not _of("round_started")


def _forked(monkeypatch, tmp_path, outcomes):
    design, _ = _started_at_design(monkeypatch, tmp_path, cwd=_work(tmp_path), schema_text=FORK_REDO)
    _design_done(design)
    _fake_registration(monkeypatch, ["registered"])
    contracts = asyncio.run(pipeline_advance_impl(design, "K", "task"))["next"]["sid"]
    _dead(design)
    artifact_put_impl("K", "review", "task-1", "APPROVE", "complete", contracts, scope="output")
    _register(monkeypatch, outcomes)
    rows = asyncio.run(pipeline_advance_impl(contracts, "K", "pr"))["rows"]
    _dead(contracts)
    return rows


def _answered(aid, answer="redesign: yes", decided_by="engineer", question="Redesign request"):
    return {"type": "question_answered", "question_id": "q1", "assignment_id": aid, "decided_by": decided_by,
            "question_head": question, "answer_head": answer}


def test_answer_question_journals_a_reserved_event_for_a_node_of_a_work(fresh_orchestrator_dir, tmp_path,
                                                                        monkeypatch):
    sid = _at_pr(monkeypatch, tmp_path)
    qid = _permit(sid)
    ev = _of("question_answered")[-1]
    assert (ev["question_id"], ev["decided_by"], ev["question_head"], ev["answer_head"]) == (
        qid, "engineer", "Redesign request", "redesign: yes")
    assert ev["assignment_id"] == _fold().tokens["t1"].holder
    with pytest.raises(ValueError, match="reserved"):
        pipeline_event_impl("K", "question_answered")


def test_a_yes_counts_only_after_the_event_that_made_the_caller_the_holder():
    took = {"type": "advance_committed", "holder": "a-pr", "node": "pr", "index": 1}
    restamped = {"type": "holder_restamped", "old": "a-pr", "new": "a-pr2"}
    assert mcp_server._redo_permit([_answered("a-pr"), took], "a-pr") is None
    assert mcp_server._redo_permit([took, _answered("a-pr")], "a-pr")["question_id"] == "q1"
    assert mcp_server._redo_permit([took, _answered("a-pr2"), restamped], "a-pr2") is None
    assert mcp_server._redo_permit([took, restamped, _answered("a-pr2")], "a-pr2") is not None
    assert mcp_server._redo_permit([took, _answered("a-pr")], None) is None


def test_a_pr_node_with_his_yes_and_a_draft_pr_opens_round_two_at_design(fresh_orchestrator_dir, tmp_path,
                                                                         monkeypatch, prs):
    sid = _at_pr(monkeypatch, tmp_path)
    qid = _permit(sid)
    calls = _register(monkeypatch, ["registered"])
    r = _redo(sid)
    assert r["state"] == "committed" and r["round"] == 2 and r["next"]["name"] == "K-design"
    assert prs.calls == [URL]
    started = _of("round_started")[-1]
    assert (started["via"], started["from"], started["to"], started["carried_prs"], started["inputs"]) == (
        "redo", "pr", "design", [URL], [])
    override = _of("token_override")[-1]
    assert (override["reason"], override["resnapshot"], override["via"]) == ("redo round 2 from pr", False, "redo")
    requested = _of("advance_requested")[-1]
    assert requested["edge"] == {"from": "pr", "to": "design"} and requested["actor_assignment"]
    ask = artifact_get_impl("K", "context", "rework-2")
    assert ask["writer_sid"] == "redo:pr"
    assert ask["content"].startswith(f"{REASON}\n\nPermitted by question {qid}:\n")
    assert "yes, the gate stays mine" in ask["content"]
    assert ask["content"].rstrip().endswith(f"Open draft PRs this round continues:\n{URL}")
    flat = _flat()
    assert not any(n.startswith("decision.") for n in flat) and "implement.repo.md" not in flat
    archived = {p.name for p in (paths.artifact_ticket_dir("K") / "rounds" / "1").glob("*.md")}
    assert {f"decision.q-{qid[:8]}.md", "implement.repo.md"} <= archived
    assert "Round 2 was opened by the pr node through its redo edge" in calls[-1]["initial_prompt"]
    assert state.read_json(paths.assignment_path(sid))["handed_off_at"] > 0
    assert mcp_server._round_pr_urls("K", mcp_server._load_snapshot_for("K")) == [URL]
    text = pipeline_status_impl("K")
    assert "- round 2: redo from pr at design since " in text and f"carried_prs {URL}" in text
    assert json.loads(pipeline_status_impl("K", "json"))["carried_prs"] == [URL]
    assert board._prs({}, [], [URL]) == [URL]


def test_a_rework_refuses_while_a_draft_the_redo_carried_is_open(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                 prs):
    sid = _at_pr(monkeypatch, tmp_path)
    _permit(sid)
    _register(monkeypatch, ["registered"])
    _redo(sid)
    with pytest.raises(ValueError, match=re.escape(f"PR {URL} is a draft a redo carried: the redesign round "
                                                   "continues it; a rework waits until it closes or merges")):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", "a new ask"))


def test_a_closed_pr_is_not_carried(fresh_orchestrator_dir, tmp_path, monkeypatch, prs):
    sid = _at_pr(monkeypatch, tmp_path)
    _permit(sid)
    prs.drafts[URL] = ("CLOSED", False)
    _register(monkeypatch, ["registered"])
    assert _redo(sid)["round"] == 2
    assert _of("round_started")[-1]["carried_prs"] == []
    assert "Open draft PRs" not in artifact_get_impl("K", "context", "rework-2")["content"]


def test_a_redo_without_his_answer_is_refused_and_changes_nothing(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _refused(_at_pr(monkeypatch, tmp_path), NO_PERMIT)


@pytest.mark.parametrize("answer,decided_by", [
    ("redesign: yes\nok", "manager"),
    ("ok", "engineer"),
    ("Redesign: Yes", "engineer"),
    ("yes", "engineer"),
])
def test_an_answer_that_is_not_his_exact_yes_is_refused(fresh_orchestrator_dir, tmp_path, monkeypatch, answer,
                                                        decided_by):
    sid = _at_pr(monkeypatch, tmp_path)
    _permit(sid, answer=answer, decided_by=decided_by)
    _refused(sid, NO_PERMIT)


def test_a_decision_record_the_node_writes_itself_does_not_count(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid = _at_pr(monkeypatch, tmp_path)
    artifact_put_impl("K", "decision", "q-forged1", "## question\nRedesign request\n\n## answer\nredesign: yes",
                      "complete", sid, scope="output", decided_by="engineer")
    _refused(sid, NO_PERMIT)


def test_a_later_no_withdraws_his_yes(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid = _at_pr(monkeypatch, tmp_path)
    _permit(sid)
    _permit(sid, answer="redesign: no\nwait for the review")
    _refused(sid, NO_PERMIT)


def test_another_question_answered_after_his_yes_keeps_it(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid = _at_pr(monkeypatch, tmp_path)
    _permit(sid)
    _permit(sid, question="Which branch?", answer="main", decided_by="manager")
    _register(monkeypatch, ["registered"])
    assert _redo(sid)["round"] == 2


def test_a_yes_given_to_another_session_of_the_work_does_not_count(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid = _at_pr(monkeypatch, tmp_path)
    _helper("sid-helper", "K", tmp_path)
    _permit("sid-helper", name="K-helper")
    _refused(sid, NO_PERMIT)


@pytest.mark.parametrize("value,match", [
    (("OPEN", False), re.escape(f"PR {URL} is open and not a draft: convert it to a draft first: "
                                f"gh pr ready --undo {URL}")),
    (("MERGED", False), re.escape(f"PR {URL} has merged; a redesign after a merge is not a redo: ask_manager")),
    (ValueError("gh exited 1: boom"), re.escape(f"cannot read the state of {URL}")),
])
def test_a_redo_is_refused_unless_every_open_pr_of_the_round_is_a_draft(fresh_orchestrator_dir, tmp_path,
                                                                        monkeypatch, prs, value, match):
    sid = _at_pr(monkeypatch, tmp_path)
    _permit(sid)
    prs.drafts[URL] = value
    _refused(sid, match)


def test_a_redo_is_refused_when_the_rounds_prs_changed_during_the_check(fresh_orchestrator_dir, tmp_path,
                                                                        monkeypatch):
    sid = _at_pr(monkeypatch, tmp_path)
    _permit(sid)
    monkeypatch.setattr(mcp_server, "_redo_pr_check", lambda *a: ([], []))
    _refused(sid, "the round's PRs changed during the check")


def test_a_redo_is_refused_while_a_question_of_the_work_is_pending(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid = _at_pr(monkeypatch, tmp_path)
    _permit(sid)
    _helper("sid-helper", "K", tmp_path)
    _write_question("sid-helper", "K-helper", "Hold the deploy?", parent_manager_name="mgr")
    _refused(sid, r"pending question\(s\) of K")


def test_a_redo_is_refused_while_a_gate_still_runs(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid = _at_pr(monkeypatch, tmp_path)
    _permit(sid)
    out = str(tmp_path / "abc.r1.out")
    artifact_put_impl("K", "gate", "open", f'{{"round": 1, "node": "design", "pid": {os.getpid()}, "out": "{out}"}}',
                      "complete", "writer-r1", scope="checkpoint")
    monkeypatch.setattr(mcp_server, "_process_command", lambda pid, index=None: f"perl -e … {out} K 1 dockwright")
    _refused(sid, re.escape(f"the design gate still runs (supervisor pid {os.getpid()})"))


def test_the_manager_cannot_walk_a_redo_edge(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid = _at_pr(monkeypatch, tmp_path)
    _permit(sid)
    with pytest.raises(ValueError, match="the manager reworks"):
        asyncio.run(pipeline_advance_impl("m1", "K", "design", reason=REASON))
    assert _no_round()


def test_a_row_redo_is_refused_while_another_rows_session_lives(fresh_orchestrator_dir, tmp_path, monkeypatch):
    rows = _forked(monkeypatch, tmp_path, ["registered", "registered"])
    svc = rows[0]["next"]["sid"]
    _permit(svc, name="K-task-2")
    _refused(svc, r"holder K-task-3 \(sid-K-task-3-\w+\) of task is alive: ask_manager")


def test_a_row_redo_is_refused_while_a_session_of_an_open_request_lives(fresh_orchestrator_dir, tmp_path,
                                                                        monkeypatch):
    rows = _forked(monkeypatch, tmp_path, ["registered", "no_register"])
    svc = rows[0]["next"]["sid"]
    _claim("K-task-3", "sid-late")
    _permit(svc, name="K-task-2")
    _refused(svc, r"session K-task-3 \(sid-late\) of open request \w+ is alive")


def test_a_row_redo_removes_every_row_and_round_two_forks_again(fresh_orchestrator_dir, tmp_path, monkeypatch):
    rows = _forked(monkeypatch, tmp_path, ["registered", "registered"])
    svc, web = (r["next"]["sid"] for r in rows)
    _dead(web)
    _permit(svc, name="K-task-2")
    _register(monkeypatch, ["registered"])
    r = _redo(svc)
    assert r["round"] == 2
    f = _fold()
    assert list(f.tokens) == ["t1"] and f.tokens["t1"].node == "design" and not f.tokens["t1"].forked
    assert f.fork is None
    design2 = r["next"]["sid"]
    _design_done(design2)
    _register(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(design2, "K", "task"))
    assert set(_fold().tokens) == {"t1", "t1/contracts"}


def test_a_dead_node_of_a_redo_round_is_restarted(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid = _at_pr(monkeypatch, tmp_path)
    _permit(sid)
    _register(monkeypatch, ["registered"])
    design2 = _redo(sid)["next"]["sid"]
    _dead(sid, design2)
    calls = _fake_registration(monkeypatch, ["registered"])
    pr.reconcile(MGR)
    assert calls and "previous session died" in calls[-1]["initial_prompt"]
    assert _of("advance_requested")[-1]["kind"] == "restart"


def test_the_merge_trigger_never_polls_the_archived_pr_of_a_redo_round(fresh_orchestrator_dir, tmp_path,
                                                                       monkeypatch):
    sid = _at_pr(monkeypatch, tmp_path)
    _permit(sid)
    _register(monkeypatch, ["registered"])
    design2 = _redo(sid)["next"]["sid"]
    _register(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="jump"))
    _dead(design2)
    polled = []
    lines = pr.reconcile(MGR, run_gh=lambda argv: polled.append(argv))
    assert polled == [] and not any(line.startswith("TRIGGER_") for line in lines)


def test_a_redo_is_refused_before_gh_is_asked_without_his_answer_or_a_reason(fresh_orchestrator_dir, tmp_path,
                                                                            monkeypatch, prs):
    sid = _at_pr(monkeypatch, tmp_path)
    prs.drafts[URL] = ("OPEN", False)
    _refused(sid, NO_PERMIT)
    _permit(sid)
    with pytest.raises(ValueError, match="needs reason"):
        asyncio.run(pipeline_advance_impl(sid, "K", "design"))
    assert prs.calls == [] and _no_round()


def test_a_redo_round_after_a_rework_names_the_reworks_ask_beside_context_ask(fresh_orchestrator_dir, tmp_path,
                                                                             monkeypatch):
    design, _ = _started_at_design(monkeypatch, tmp_path, cwd=_work(tmp_path), schema_text=REDO)
    _dead(design)
    _register(monkeypatch, ["registered"])
    design2 = asyncio.run(pipeline_rework_impl("m1", "K", "design", "drop the board change"))["next"]["sid"]
    _register(monkeypatch, ["registered"])
    pr_node = asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="jump"))["next"]
    _dead(design2)
    artifact_put_impl("K", "implement", "repo", URL, "complete", pr_node["sid"], scope="output")
    _permit(pr_node["sid"], name=pr_node["name"])
    calls = _register(monkeypatch, ["registered"])
    assert _redo(pr_node["sid"])["round"] == 3
    assert ("context.rework-3 is its reason. context.ask and context.rework-2 still bind; where they differ, "
            "context.rework-2 wins; where the reason conflicts with them, ask_manager.") in calls[-1]["initial_prompt"]


def test_a_redo_edge_on_a_closed_work_is_refused(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid = _at_pr(monkeypatch, tmp_path)
    _permit(sid)
    _dead(sid)
    pipeline_close_impl("m1", "K", "absorbed by repo-node")
    calls = _register(monkeypatch, ["registered"])
    with pytest.raises(ValueError, match=r"pipeline_advance refused: K was closed at \S+ \S+ on pr: absorbed by "
                                         r"repo-node; pipeline_rework reopens it"):
        _redo(sid)
    assert calls == [] and _fold().round == 1
