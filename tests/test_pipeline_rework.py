import asyncio
import os
import re
import shutil
import subprocess
import threading
import time
import types
import uuid

import pytest

from dockwright import config, mcp_server, paths, pipeline, state
from dockwright import pipeline_reconcile as pr
from dockwright.mcp_server import (answer_question_impl, artifact_get_impl, artifact_put_impl, ask_manager_impl,
                                   list_pending_questions_impl, pipeline_advance_impl, pipeline_event_impl,
                                   pipeline_rework_impl, pipeline_start_impl, pipeline_status_impl, register_self_impl,
                                   _refuse_handed_off_node, _write_question)
from tests.test_pipeline_core import EXPLAIN_SCHEMA, SCHEMA
from tests.test_pipeline_reconcile import (MGR, TWO_PRS, _at_pr_with_a_pr_url, _claim,  # noqa: F401
                                           _complete_design_outputs, _events, _fake_registration, _fold, _of,
                                           _reconciled, _started_at_design, fresh_orchestrator_dir, killed,
                                           session_logs)

URL = "https://github.com/o/r/pull/7"
URL2 = "https://github.com/o/contracts/pull/8"
ASK = " ".join("".join(chr(0x03b1 + (i + j) % 25) for j in range(8)) for i in range(300))
REAL_PR_STATE = mcp_server._pr_state


@pytest.fixture(autouse=True)
def prs(monkeypatch):
    fake = types.SimpleNamespace(states={}, calls=[])

    def state_of(url, cwd):
        fake.calls.append(url)
        value = fake.states.get(url, "MERGED")
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr("dockwright.mcp_server._pr_state", state_of)
    return fake


def _dead(*sids):
    for sid in sids:
        (paths.ACTIVE / f"{sid}.json").unlink(missing_ok=True)


def _register(monkeypatch, outcomes):
    calls = []

    async def fake_spawn(**kw):
        calls.append(kw)
        return ("999", kw["name"])

    async def fake_confirm(name, timeout, poll):
        if outcomes.pop(0) != "registered":
            return None
        sid = f"sid-{name}-{uuid.uuid4().hex[:6]}"
        _claim(name, sid)
        return state.read_json(paths.ACTIVE / f"{sid}.json")

    monkeypatch.setattr("dockwright.spawner.spawn_worker_tab", fake_spawn)
    monkeypatch.setattr("dockwright.mcp_server._confirm_spawn_registration", fake_confirm)
    return calls


def _flat():
    return sorted(p.name for p in paths.artifact_ticket_dir("K").glob("*.md"))


def _fake_gh(monkeypatch, tmp_path, out, rc=0):
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(f"#!/bin/sh\nprintf '%s' '{out}'\nexit {rc}\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


def _work(tmp_path):
    d = tmp_path / "work"
    d.mkdir(exist_ok=True)
    return str(d)


def _no_round():
    return _fold().round == 1 and not (paths.artifact_ticket_dir("K") / "rounds").exists()


def _helper(sid, ticket, tmp_path):
    register_self_impl(sid, "worker", f"{ticket}-helper", str(tmp_path), "", pid=None, parent_manager_name="mgr",
                       parent_sid="m1")
    state.write_json_atomic(paths.assignment_path(sid), {
        "assignment_id": f"a-{sid}", "claude_sid": sid, "ticket": ticket, "name": f"{ticket}-helper",
        "manager_sid": "m1", "parent_name": "mgr", "spawned_at": time.time()})


def _pending_task_request(monkeypatch, tmp_path):
    sid, _ = _started_at_design(monkeypatch, tmp_path, cwd=_work(tmp_path))
    _complete_design_outputs(sid)
    _register(monkeypatch, ["no_register"])
    assert asyncio.run(pipeline_advance_impl(sid, "K", "task"))["state"] == "pending"
    _dead(sid)


def _finished(monkeypatch, tmp_path, cwd=None, schema_text=SCHEMA):
    _started_at_design(monkeypatch, tmp_path, cwd=cwd or _work(tmp_path), schema_text=schema_text)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="jump"))
    artifact_put_impl("K", "implement", "repo", URL, "complete", "sid-K-pr", scope="output")
    _register(monkeypatch, ["registered"])
    post = asyncio.run(pipeline_advance_impl("m1", "K", "post-merge", reason="2026-09-24T10:00:00Z"))["next"]["sid"]
    artifact_put_impl("K", "summary", "post-merge", "done", "complete", post, scope="output")
    for full, body in (("design.hld", "hld"), ("plan.tasks", '{"tasks": [{"id": 1}]}'), ("design.explainer", "x"),
                       ("review.task-1", "ok"), ("decision.q-hold", "hold before advancing"),
                       ("decision.q-keep", "still binds"), ("investigate.findings", "facts")):
        phase, name = full.split(".", 1)
        artifact_put_impl("K", phase, name, body, "complete", "writer-r1", scope="output")
    artifact_put_impl("K", "design", "hld", "hld revised", "complete", "writer-r1", scope="output", overwrite=True)
    _dead("sid-K-design", "sid-K-pr", post)
    return post


def test_a_rework_from_the_terminal_node_archives_the_round_and_spawns_the_entry(fresh_orchestrator_dir, tmp_path,
                                                                                monkeypatch):
    old = _finished(monkeypatch, tmp_path)
    old_aid = _fold().tokens["t1"].holder
    calls = _register(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK, inputs=["investigate.findings", "decision.q-keep"]))
    assert r["state"] == "committed" and r["round"] == 2 and r["next"]["name"] == "K-design"
    assert _flat() == ["context.ask.md", "context.rework-2.md", "decision.q-keep.md", "investigate.findings.md"]
    archive = paths.artifact_ticket_dir("K") / "rounds" / "1"
    assert sorted(p.name for p in archive.glob("*.md")) == [
        "decision.q-hold.md", "design.explainer.md", "design.hld.md", "implement.repo.md", "plan.tasks.md",
        "review.task-1.md", "summary.post-merge.md"]
    assert sorted(p.name for p in archive.glob("*.prev")) == ["design.hld.md.prev"]
    assert not list(paths.artifact_ticket_dir("K").glob("*.prev"))
    assert (archive / "schema.toml").is_file()
    stored = artifact_get_impl("K", "context", "rework-2")["content"]
    assert stored == ASK and len(stored.encode()) > 4096
    started = _of("round_started")[-1]
    assert (started["round"], started["to"], started["ask"]) == (2, "design", "context.rework-2")
    assert started["inputs"] == ["investigate.findings", "decision.q-keep"]
    types_ = [e["type"] for e in _events()]
    tail = types_[types_.index("round_started") - 1:]
    assert tail[:5] == ["schema_snapshot", "round_started", "token_override", "advance_requested", "handed_off"]
    assert _of("token_override")[-1]["via"] == "rework"
    f = _fold()
    assert f.round == 2 and f.tokens["t1"].node == "design" and f.tokens["t1"].holder == r["next"]["assignment_id"]
    assert [e["assignment_id"] for e in _of("handed_off")][-1] == old_aid
    with pytest.raises(ValueError, match="handed off"):
        _refuse_handed_off_node("K-post-merge", old)
    assert not mcp_server._handed_off(r["next"]["sid"], "K")
    brief = calls[-1]["initial_prompt"]
    assert " round=2\n" in brief and "context.rework-2 is this round's ask" in brief
    inputs_line = next(line for line in brief.splitlines() if line.startswith("Inputs"))
    assert inputs_line.count("decision.q-keep = ") == 1
    assert "context.rework-2" in inputs_line and "investigate.findings" in inputs_line
    assert "decision.q-hold" not in brief


def test_a_rework_carries_the_works_running_summary_into_the_new_round(fresh_orchestrator_dir, tmp_path,
                                                                       monkeypatch):
    _finished(monkeypatch, tmp_path)
    artifact_put_impl("K", "summary", "thread", "where the work stands", "complete", "writer-r1", scope="output")
    _register(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK, inputs=["investigate.findings", "decision.q-keep"]))
    assert r["state"] == "committed"
    assert "summary.thread.md" in _flat()
    assert not (paths.artifact_ticket_dir("K") / "rounds" / "1" / "summary.thread.md").exists()
    assert artifact_get_impl("K", "summary", "thread")["content"] == "where the work stands"


def test_a_rework_is_refused_to_anyone_but_a_manager_before_gh_is_asked(fresh_orchestrator_dir, tmp_path,
                                                                        monkeypatch, prs):
    _finished(monkeypatch, tmp_path)
    prs.states[URL] = "OPEN"
    with pytest.raises(ValueError, match="manager-only"):
        asyncio.run(pipeline_rework_impl("sid-K-pr", "K", "design", ASK))
    assert _fold().round == 1 and prs.calls == []


def test_a_rework_is_refused_while_the_holder_is_alive(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _at_pr_with_a_pr_url(monkeypatch, tmp_path, body=URL, cwd=_work(tmp_path))
    with pytest.raises(ValueError, match=r"holder K-pr \(sid-K-pr\) of pr is alive: kill_worker it"):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))
    assert not (paths.artifact_ticket_dir("K") / "rounds").exists()


@pytest.mark.parametrize("record", ["[]", "{not json"], ids=["not-an-object", "not-json"])
def test_a_holder_whose_active_record_cannot_be_read_counts_as_alive(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                     record):
    _at_pr_with_a_pr_url(monkeypatch, tmp_path, body=URL, cwd=_work(tmp_path))
    (paths.ACTIVE / "sid-K-pr.json").write_text(record)
    _register(monkeypatch, ["registered"])
    with pytest.raises(ValueError, match=r"holder K-pr \(sid-K-pr\) of pr is alive: kill_worker it"):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))
    assert _no_round()


@pytest.mark.parametrize("older,newer", [("sid-a", "sid-b"), ("sid-b", "sid-a")])
def test_the_holders_newest_assignment_record_decides_whether_it_is_alive(fresh_orchestrator_dir, tmp_path,
                                                                          monkeypatch, older, newer):
    _finished(monkeypatch, tmp_path)
    holder = _fold().tokens["t1"].holder
    path = next(p for p in paths.ASSIGNMENTS.glob("*.json")
                if (state.read_json(p) or {}).get("assignment_id") == holder)
    record = state.read_json(path)
    path.unlink()
    for sid in sorted((older, newer)):
        later = 60 if sid == newer else 0
        state.write_json_atomic(paths.assignment_path(sid),
                                {**record, "claude_sid": sid, "spawned_at": record["spawned_at"] + later})
    register_self_impl(newer, "worker", "K-post-merge", _work(tmp_path), "", pid=None, parent_manager_name="mgr",
                       parent_sid="m1")
    _register(monkeypatch, ["registered"])
    with pytest.raises(ValueError, match=rf"holder K-post-merge \({newer}\) of post-merge is alive"):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))
    assert _no_round()


def test_a_rework_is_refused_while_a_session_of_an_open_request_is_alive(fresh_orchestrator_dir, tmp_path,
                                                                         monkeypatch):
    _pending_task_request(monkeypatch, tmp_path)
    _claim("K-task-1", "sid-late")
    _register(monkeypatch, ["registered"])
    with pytest.raises(ValueError, match=r"session K-task-1 \(sid-late\) of open request \w+ is alive: kill_worker "
                                         r"it, then call pipeline_rework again"):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))
    assert _no_round() and "design.hld.md" in _flat()


def test_a_rework_is_refused_while_a_question_from_a_session_of_the_work_is_pending(fresh_orchestrator_dir, tmp_path,
                                                                                   monkeypatch):
    _finished(monkeypatch, tmp_path)
    _helper("sid-helper", "K", tmp_path)
    _helper("sid-other", "OTHER", tmp_path)
    _write_question("sid-other", "OTHER-helper", "Unrelated?", parent_manager_name="mgr")
    qid = _write_question("sid-helper", "K-helper", "Hold the deploy?", parent_manager_name="mgr")
    with pytest.raises(ValueError, match=rf"pending question\(s\) of K: {qid}\. Answer each with answer_question"):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))
    assert _no_round()
    answer_question_impl(qid, "hold")
    _register(monkeypatch, ["registered"])
    assert asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))["state"] == "committed"
    assert (paths.artifact_ticket_dir("K") / "rounds" / "1" / f"decision.q-{qid[:8]}.md").is_file()


def test_a_rework_whose_worktree_is_gone_is_refused_before_gh_is_asked(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                       prs):
    gone = tmp_path / "pruned-worktree"
    _finished(monkeypatch, tmp_path, cwd=str(gone))
    _register(monkeypatch, ["registered"])
    with pytest.raises(ValueError, match=re.escape(f"the work's cwd {gone} is gone: recreate the worktree, or pass "
                                                   "cwd=<an existing directory> with a `to` that recreates it, then "
                                                   "call pipeline_rework again")):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))
    assert prs.calls == [] and _no_round()


def test_a_pr_published_while_gh_is_asked_refuses_the_rework(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _finished(monkeypatch, tmp_path)

    def publishing_meanwhile(url, cwd):
        artifact_put_impl("K", "implement", "repo", f"new PR {URL2}", "complete", "writer-r1", scope="output",
                          overwrite=True)
        return "MERGED"

    monkeypatch.setattr("dockwright.mcp_server._pr_state", publishing_meanwhile)
    _register(monkeypatch, ["registered"])
    with pytest.raises(ValueError, match="the round's PR changed during the check; call pipeline_rework again"):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))
    assert _no_round()


def test_only_the_first_pr_of_the_merge_nodes_outputs_is_checked(fresh_orchestrator_dir, tmp_path, monkeypatch, prs):
    _finished(monkeypatch, tmp_path)
    artifact_put_impl("K", "implement", "repo", f"{URL}, replacing {URL2}", "complete", "writer-r1", scope="output",
                      overwrite=True)
    artifact_put_impl("K", "implement", "task-1", f"builds on the upstream {URL2}", "complete", "writer-r1",
                      scope="output")
    prs.states[URL2] = "OPEN"
    _register(monkeypatch, ["registered"])
    assert asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))["state"] == "committed"
    assert prs.calls == [URL]


def test_every_listed_pr_of_the_merge_node_blocks_a_rework_while_open(fresh_orchestrator_dir, tmp_path,
                                                                       monkeypatch, prs):
    _finished(monkeypatch, tmp_path)
    artifact_put_impl("K", "implement", "repo", f"{URL}\n{URL2}\nbranches ...", "complete", "writer-r1",
                      scope="output", overwrite=True)
    prs.states[URL2] = "OPEN"
    with pytest.raises(ValueError, match=r"PR https://github.com/o/contracts/pull/8 of this round is open"):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))
    assert prs.calls == [URL, URL2]


def test_a_rework_is_refused_while_a_pr_of_the_round_is_open_or_unreadable(fresh_orchestrator_dir, tmp_path,
                                                                           monkeypatch, prs):
    _finished(monkeypatch, tmp_path, schema_text=TWO_PRS)
    artifact_put_impl("K", "implement", "contracts", f"contracts PR {URL2}", "complete", "writer-r1", scope="output")
    prs.states[URL] = "OPEN"
    with pytest.raises(ValueError, match=r"PR https://github.com/o/r/pull/7 of this round is open"):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))
    prs.states[URL] = "MERGED"
    prs.states[URL2] = "OPEN"
    with pytest.raises(ValueError, match=r"PR https://github.com/o/contracts/pull/8 of this round is open"):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))
    prs.states[URL2] = "CLOSED"
    prs.states[URL] = ValueError("gh: not logged in")
    with pytest.raises(ValueError, match=r"cannot read the state of https://github.com/o/r/pull/7"):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))
    assert _fold().round == 1 and "implement.repo.md" in _flat()
    prs.states[URL] = "CLOSED"
    _register(monkeypatch, ["registered"])
    assert asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))["state"] == "committed"


def test_a_work_with_no_pr_url_never_calls_gh(fresh_orchestrator_dir, tmp_path, monkeypatch, prs):
    sid, _ = _started_at_design(monkeypatch, tmp_path, cwd=_work(tmp_path))
    _dead(sid)
    _register(monkeypatch, ["registered"])
    assert asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))["round"] == 2
    assert prs.calls == []


@pytest.mark.parametrize("to,ask,inputs,match", [
    ("design", "  ", [], "ask must be"),
    ("nowhere", ASK, [], "'nowhere' is not a node of the schema"),
    ("design", ASK, ["investigate.missing"], "input 'investigate.missing' is not in the store"),
    ("task", ASK, ["implement.repo"], "is an output or checkpoint of a node this round redoes"),
    ("design", ASK, [""], "inputs must be artifact names"),
], ids=["blank-ask", "unknown-node", "missing-input", "redone-output-input", "empty-input-name"])
def test_a_bad_rework_request_is_refused_and_changes_nothing(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                             to, ask, inputs, match):
    _finished(monkeypatch, tmp_path)
    before = _flat()
    with pytest.raises(ValueError, match=match):
        asyncio.run(pipeline_rework_impl("m1", "K", to, ask, inputs=inputs))
    assert _flat() == before and _fold().round == 1
    assert not (paths.artifact_ticket_dir("K") / "rounds").exists()


def test_a_rework_that_would_carry_a_gate_verdict_past_its_gate_is_refused(fresh_orchestrator_dir, tmp_path,
                                                                             monkeypatch):
    _finished(monkeypatch, tmp_path)
    artifact_put_impl("K", "gate", "open", '{"round": 1, "node": "design"}', "complete", "writer-r1",
                      scope="checkpoint")
    artifact_put_impl("K", "decision", "verdict-r1", '{"decision": "annotated"}', "complete", "topic:cli",
                      scope="output", decided_by="engineer")
    before = _flat()
    _register(monkeypatch, ["registered"])
    with pytest.raises(ValueError, match=re.escape(
            "decision.verdict-r1 would outlive its gate.open: the next gate restarts at round 1 and its verdict "
            "collides; carry his ruling in the ask or as decision.gate-<N>")):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK, inputs=["decision.verdict-r1"]))
    assert _flat() == before and _fold().round == 1
    assert not (paths.artifact_ticket_dir("K") / "rounds").exists()
    assert asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))["state"] == "committed"
    assert "decision.verdict-r1.md" not in _flat()


def test_a_rework_that_would_archive_a_running_gate_is_refused(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _finished(monkeypatch, tmp_path)
    out = str(tmp_path / "abc.r1.out")
    artifact_put_impl("K", "gate", "open", f'{{"round": 1, "node": "design", "pid": {os.getpid()}, "out": "{out}"}}',
                      "complete", "writer-r1", scope="checkpoint")
    monkeypatch.setattr(mcp_server, "_process_command", lambda pid, index=None: f"perl -e … {out} K 1 dockwright")
    before = _flat()
    _register(monkeypatch, ["registered"])
    with pytest.raises(ValueError, match=re.escape(f"the design gate still runs (supervisor pid {os.getpid()})")):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))
    assert _flat() == before and _fold().round == 1
    monkeypatch.setattr(mcp_server, "_process_command", lambda pid, index=None: "")
    assert asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))["state"] == "committed"


def test_a_rework_never_carries_a_named_gate_record(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _finished(monkeypatch, tmp_path)
    artifact_put_impl("K", "gate", "open", '{"round": 1, "node": "design"}', "complete", "writer-r1",
                      scope="checkpoint")
    before = _flat()
    _register(monkeypatch, ["registered"])
    with pytest.raises(ValueError, match=re.escape("input 'gate.open' never rides a rework")):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK, inputs=["gate.open"]))
    assert _flat() == before and _fold().round == 1


def test_a_rework_past_the_gated_node_carries_its_verdict_with_its_gate_record(fresh_orchestrator_dir, tmp_path,
                                                                               monkeypatch):
    _finished(monkeypatch, tmp_path, schema_text=SCHEMA.replace('checkpoints = ["design.explainer"]',
                                                                'checkpoints = ["design.explainer", "gate.open"]'))
    artifact_put_impl("K", "gate", "open", '{"round": 1, "node": "design"}', "complete", "writer-r1",
                      scope="checkpoint")
    artifact_put_impl("K", "decision", "verdict-r1", '{"decision": "approved"}', "complete", "topic:cli",
                      scope="output", decided_by="engineer")
    _register(monkeypatch, ["registered"])
    assert asyncio.run(pipeline_rework_impl("m1", "K", "task", ASK))["state"] == "committed"
    assert {"gate.open.md", "decision.verdict-r1.md"} <= set(_flat())
    assert mcp_server._gate_state("K") is None


def test_a_rework_on_a_newer_schema_refuses_an_entry_whose_input_the_old_round_never_wrote(fresh_orchestrator_dir,
                                                                                         tmp_path, monkeypatch):
    _finished(monkeypatch, tmp_path)
    schema = tmp_path / "schemas" / "code-ticket.toml"
    schema.write_text(EXPLAIN_SCHEMA)
    (schema.parent / "briefs" / "explain.md").write_text("# explain brief for {task_key} inputs {inputs}\n")
    with pytest.raises(ValueError, match="enter at 'explain' or earlier"):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))
    _register(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_rework_impl("m1", "K", "explain", ASK))
    assert r["next"]["name"] == "K-explain"
    snap = pipeline.load_snapshot((paths.artifact_ticket_dir("K") / "schema.toml").read_text())
    assert "explain" in snap.nodes


def test_a_rework_mid_flight_supersedes_the_open_request_and_shuts_its_late_session_out(fresh_orchestrator_dir,
                                                                                       tmp_path, monkeypatch):
    _pending_task_request(monkeypatch, tmp_path)
    _register(monkeypatch, ["registered"])
    assert asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))["state"] == "committed"
    assert _fold().open_requests == []
    _claim("K-task-1", "sid-late")
    with pytest.raises(ValueError, match="node handed off"):
        artifact_put_impl("K", "review", "task-1", "stale", "complete", "sid-late", scope="output")
    with pytest.raises(ValueError, match="ask_manager refused: the pipeline request of this session was superseded"):
        asyncio.run(ask_manager_impl("sid-late", "Which plan do I build?", timeout_sec=0))
    assert list_pending_questions_impl() == []


def test_a_displaced_holder_that_asks_while_the_rework_runs_is_refused_once_the_round_opens(fresh_orchestrator_dir,
                                                                                            tmp_path, monkeypatch):
    d1, _ = _started_at_design(monkeypatch, tmp_path, cwd=_work(tmp_path))
    _complete_design_outputs(d1)
    _register(monkeypatch, ["registered"])
    _dead(asyncio.run(pipeline_advance_impl(d1, "K", "task"))["next"]["sid"])
    real_archive = mcp_server._archive_round
    asked, threads = {}, []

    def ask():
        try:
            asked["out"] = asyncio.run(ask_manager_impl(d1, "Round-1 question: hold the deploy?", timeout_sec=0))
        except ValueError as err:
            asked["refused"] = str(err)

    def archive_while_the_holder_asks(task_key, closing, keep):
        real_archive(task_key, closing, keep)
        threads.append(threading.Thread(target=ask))
        threads[0].start()
        threads[0].join(timeout=0.5)

    monkeypatch.setattr(mcp_server, "_archive_round", archive_while_the_holder_asks)
    _register(monkeypatch, ["registered"])
    assert asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))["state"] == "committed"
    threads[0].join()
    assert "superseded" in asked.get("refused", "") and list_pending_questions_impl() == []


def test_a_helpers_answer_after_the_rework_is_a_round_two_decision(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _finished(monkeypatch, tmp_path)
    _helper("sid-helper", "K", tmp_path)
    _register(monkeypatch, ["registered"])
    asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))
    qid = _write_question("sid-helper", "K-helper", "Should round 2 also cover Y?", parent_manager_name="mgr")
    answer_question_impl(qid, "yes, Y too")
    assert "yes, Y too" in artifact_get_impl("K", "decision", f"q-{qid[:8]}")["content"]
    assert f"decision.q-{qid[:8]}.md" in _flat()


def test_a_carried_artifacts_previous_version_is_archived_with_its_round(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _finished(monkeypatch, tmp_path)
    original = artifact_get_impl("K", "context", "ask")["content"]
    artifact_put_impl("K", "context", "ask", "round-1 enriched ask", "complete", "writer-r1", scope="output",
                      overwrite=True)
    _register(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))
    artifact_put_impl("K", "context", "ask", "round-2 enriched ask", "complete", r["next"]["sid"], scope="output",
                      overwrite=True)
    archived = paths.artifact_ticket_dir("K") / "rounds" / "1" / "context.ask.md.prev"
    assert state.parse_artifact(archived.read_text())[1] == original


@pytest.mark.parametrize("displaced_by", ["restart", "advance", "override"])
def test_a_round_one_holders_question_is_a_decision_after_the_holder_is_displaced(fresh_orchestrator_dir, tmp_path,
                                                                                  monkeypatch, displaced_by):
    sid, _ = _started_at_design(monkeypatch, tmp_path, cwd=_work(tmp_path))
    qid = _write_question(sid, "K-design", "Deploy now or hold?", parent_manager_name="mgr")
    _register(monkeypatch, ["registered"])
    if displaced_by == "restart":
        _dead(sid)
        assert pr.reconcile(MGR) == [] and len(_reconciled()) == 1
    elif displaced_by == "advance":
        _complete_design_outputs(sid)
        assert asyncio.run(pipeline_advance_impl(sid, "K", "task"))["state"] == "committed"
    else:
        asyncio.run(pipeline_advance_impl("m1", "K", "design", override=True, reason="redo the design"))
    answer_question_impl(qid, "Don't deploy yet")
    assert artifact_get_impl("K", "decision", f"q-{qid[:8]}")["status"] == "complete"


@pytest.mark.parametrize("then", ["stays", "is-restarted"])
def test_a_question_from_a_session_spawned_in_the_new_round_is_a_decision(fresh_orchestrator_dir, tmp_path,
                                                                          monkeypatch, then):
    _finished(monkeypatch, tmp_path)
    _register(monkeypatch, ["registered"])
    sid = asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))["next"]["sid"]
    qid = _write_question(sid, "K-design", "Keep the old column name?", parent_manager_name="mgr")
    if then == "is-restarted":
        _dead(sid)
        _register(monkeypatch, ["registered"])
        assert pr.reconcile(MGR, now=pr.time.time() + 3600) == [] and len(_reconciled()) == 1
    answer_question_impl(qid, "yes")
    assert artifact_get_impl("K", "decision", f"q-{qid[:8]}")["status"] == "complete"


def test_a_late_session_of_a_request_a_plain_override_superseded_cannot_write_or_advance(fresh_orchestrator_dir,
                                                                                        tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _register(monkeypatch, ["no_register"])
    assert asyncio.run(pipeline_advance_impl(sid, "K", "task"))["state"] == "pending"
    _register(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "design", override=True, reason="redo the design"))
    _claim("K-task-1", "sid-late")
    with pytest.raises(ValueError, match="node handed off"):
        artifact_put_impl("K", "review", "task-1", "stale", "complete", "sid-late", scope="output")
    with pytest.raises(ValueError, match="node handed off"):
        asyncio.run(pipeline_advance_impl("sid-late", "K", "task"))
    with pytest.raises(ValueError, match="handed off"):
        _refuse_handed_off_node("K-task-1", "sid-late")
    with pytest.raises(ValueError, match="ask_manager refused: the pipeline request of this session was superseded"):
        asyncio.run(ask_manager_impl("sid-late", "Which task do I build?", timeout_sec=0))
    assert list_pending_questions_impl() == []


def test_the_merge_trigger_ignores_the_archived_pr_of_an_earlier_round(fresh_orchestrator_dir, tmp_path,
                                                                     monkeypatch):
    _finished(monkeypatch, tmp_path)
    _register(monkeypatch, ["registered"])
    asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))
    _register(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="jump to pr in round 2"))
    polled = []

    def gh(argv):
        polled.append(argv)
        return '{"state":"MERGED","mergedAt":"2026-09-24T10:00:00Z"}'

    assert pr.reconcile(MGR, run_gh=gh) == []
    assert polled == [] and _fold().tokens["t1"].node == "pr"


def test_a_dead_round_two_holder_is_restarted_without_the_earlier_rounds_checkpoints(fresh_orchestrator_dir,
                                                                                    tmp_path, monkeypatch):
    _finished(monkeypatch, tmp_path)
    (tmp_path / "schemas" / "briefs" / "design.md").write_text("# design present={checkpoints} inputs {inputs}\n")
    _register(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))
    _dead(r["next"]["sid"])
    calls = _register(monkeypatch, ["registered"])
    lines = pr.reconcile(MGR, now=pr.time.time() + 3600)
    assert lines == [] and len(_reconciled()) == 1 and _of("holder_restamped")
    assert calls[-1]["initial_prompt"].startswith("# design present=(none) inputs ")


def test_a_second_rework_archives_round_two_and_keeps_every_rework_ask(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _finished(monkeypatch, tmp_path)
    _register(monkeypatch, ["registered"])
    r2 = asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK, inputs=["investigate.findings"]))
    artifact_get_impl("K", "investigate", "findings", reader_sid=r2["next"]["sid"])
    _dead(r2["next"]["sid"])
    calls = _register(monkeypatch, ["registered"])
    r3 = asyncio.run(pipeline_rework_impl("m1", "K", "design", "third ask"))
    assert r3["round"] == 3
    assert _flat() == ["context.ask.md", "context.rework-2.md", "context.rework-3.md"]
    assert (paths.artifact_ticket_dir("K") / "rounds" / "2" / "investigate.findings.md").is_file()
    inputs_line = next(line for line in calls[-1]["initial_prompt"].splitlines() if line.startswith("Inputs"))
    assert "context.rework-2 = " in inputs_line and "context.rework-3 = " in inputs_line
    reads = pipeline_status_impl("K").split("## reads", 1)[1].split("\n## ", 1)[0]
    assert "read investigate.findings" not in reads


@pytest.mark.parametrize("out,expected", [('{"state":"OPEN"}', "OPEN"), ('{"state":"MERGED"}', "MERGED"),
                                          ('{"state":"CLOSED"}', "CLOSED")], ids=["open", "merged", "closed"])
def test_the_pr_state_is_what_gh_reports(tmp_path, monkeypatch, out, expected):
    _fake_gh(monkeypatch, tmp_path, out)
    assert REAL_PR_STATE(URL, None) == expected


@pytest.mark.parametrize("out,rc", [("{}", 0), ("[]", 0), ("null", 0), ("not json", 0), ('{"state":"OPEN"}', 1),
                                    ('{"state":"DRAFT"}', 0)],
                         ids=["empty-object", "list", "null", "not-json", "gh-failed", "unknown-state"])
def test_a_pr_state_gh_cannot_vouch_for_raises(tmp_path, monkeypatch, out, rc):
    _fake_gh(monkeypatch, tmp_path, out, rc)
    with pytest.raises(ValueError):
        REAL_PR_STATE(URL, None)


def test_a_rework_through_the_real_pr_state_refuses_an_unreadable_state(fresh_orchestrator_dir, tmp_path,
                                                                       monkeypatch):
    _finished(monkeypatch, tmp_path)
    monkeypatch.setattr("dockwright.mcp_server._pr_state", REAL_PR_STATE)
    _fake_gh(monkeypatch, tmp_path, "{}")
    with pytest.raises(ValueError, match="cannot read the state of https://github.com/o/r/pull/7"):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))
    assert _fold().round == 1


def test_a_rework_with_the_cwd_gone_still_reads_the_pr_under_the_owner_token(fresh_orchestrator_dir, tmp_path,
                                                                             monkeypatch):
    _finished(monkeypatch, tmp_path)
    monkeypatch.setattr("dockwright.mcp_server._pr_state", REAL_PR_STATE)
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text('[gh]\n[gh.logins]\no = "me"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    seen = []

    def fake_run(argv, **kw):
        seen.append((argv, kw))
        if argv[1:3] == ["auth", "token"]:
            return subprocess.CompletedProcess(argv, 0, stdout="t-me\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout='{"state": "MERGED"}', stderr="")

    monkeypatch.setattr("dockwright.gh_cli.subprocess.run", fake_run)
    shutil.rmtree(_work(tmp_path))
    _register(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK, cwd=str(tmp_path)))
    assert r["round"] == 2
    view = [kw for argv, kw in seen if argv[1:3] == ["pr", "view"]]
    assert view[0]["cwd"] is None and view[0]["env"]["GH_TOKEN"] == "t-me"


def test_pipeline_start_on_a_finished_work_names_the_rework_tool(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _finished(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="pipeline_rework"):
        asyncio.run(pipeline_start_impl("m1", "K", "code-ticket", "/w", "again"))


def test_pipeline_status_shows_the_round_the_archive_and_only_this_rounds_reads(fresh_orchestrator_dir, tmp_path,
                                                                               monkeypatch):
    _finished(monkeypatch, tmp_path)
    artifact_get_impl("K", "design", "hld", reader_sid="sid-K-pr")
    _register(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK, inputs=["investigate.findings"]))
    artifact_get_impl("K", "context", "rework-2", reader_sid=r["next"]["sid"])
    artifact_get_impl("K", "investigate", "findings", reader_sid=r["next"]["sid"])
    out = pipeline_status_impl("K")
    assert "- round 2: rework at design since " in out and "ask context.rework-2, inputs investigate.findings" in out
    assert "\n## rounds\n- round 1: 8 archived artifact(s) in " in out
    ask_line = next(line for line in out.splitlines() if line.startswith("- context.ask "))
    assert ask_line.endswith("  carried")
    rework_line = next(line for line in out.splitlines() if line.startswith("- context.rework-2 "))
    assert not rework_line.endswith("carried")
    reads = out.split("## reads", 1)[1].split("\n## ", 1)[0]
    assert "read design.hld" not in reads
    assert "read context.rework-2" in reads and "read investigate.findings" in reads
    assert "OUTSIDE INPUTS" not in reads


def test_the_journal_refuses_a_forged_round_started_event(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="reserved"):
        pipeline_event_impl("K", "round_started", reason="forged")
    assert _events() == []


def _other_schema(tmp_path, text=EXPLAIN_SCHEMA):
    d = tmp_path / "schemas"
    (d / "other.toml").write_text(text)
    (d / "briefs" / "explain.md").write_text("# explain brief for {task_key} inputs {inputs}\n")


def test_a_rework_with_a_schema_moves_the_work_and_later_reworks_read_it(fresh_orchestrator_dir, tmp_path,
                                                                        monkeypatch):
    _finished(monkeypatch, tmp_path)
    _other_schema(tmp_path)
    _register(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_rework_impl("m1", "K", "explain", ASK, schema="other"))
    assert r["next"]["name"] == "K-explain" and r["round"] == 2
    snap = pipeline.load_snapshot((paths.artifact_ticket_dir("K") / "schema.toml").read_text())
    assert "explain" in snap.nodes
    assert _of("schema_snapshot")[-1]["schema"] == "other"
    assert mcp_server._started_schema_name("K", "t1", "code-ticket") == "other"
    _dead(r["next"]["sid"])
    (tmp_path / "schemas" / "code-ticket.toml").unlink()
    _register(monkeypatch, ["registered"])
    assert asyncio.run(pipeline_rework_impl("m1", "K", "explain", ASK))["round"] == 3


@pytest.mark.parametrize("schema,refusal", [("absent", "no schema file"), ("", "is not a bare schema name")])
def test_a_rework_onto_a_missing_or_empty_schema_is_refused_and_changes_nothing(fresh_orchestrator_dir, tmp_path,
                                                                               monkeypatch, schema, refusal):
    _finished(monkeypatch, tmp_path)
    before = _flat()
    snapshot = (paths.artifact_ticket_dir("K") / "schema.toml").read_text()
    snapshots = len(_of("schema_snapshot"))
    with pytest.raises(ValueError, match=refusal):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK, schema=schema))
    assert _flat() == before and _fold().round == 1
    assert (paths.artifact_ticket_dir("K") / "schema.toml").read_text() == snapshot
    assert len(_of("schema_snapshot")) == snapshots


def test_a_rework_with_a_cwd_runs_a_work_whose_worktree_is_gone(fresh_orchestrator_dir, tmp_path, monkeypatch, prs):
    gone = tmp_path / "pruned-worktree"
    gone.mkdir()
    _finished(monkeypatch, tmp_path, cwd=str(gone))
    gone.rmdir()
    home = tmp_path / "home-dir"
    home.mkdir()
    calls = _register(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK, cwd=str(home)))
    assert r["round"] == 2 and calls[0]["cwd"] == str(home)
    assert _of("advance_requested")[-1]["cwd"] == str(home) and _fold().tokens["t1"].cwd == str(home)


@pytest.mark.parametrize("bad", ["relative/dir", "/no/such/dir/anywhere", ""])
def test_a_rework_with_a_bad_cwd_is_refused_and_changes_nothing(fresh_orchestrator_dir, tmp_path, monkeypatch, prs,
                                                                bad):
    _finished(monkeypatch, tmp_path)
    before = _flat()
    with pytest.raises(ValueError, match="must be an absolute path to an existing directory"):
        asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK, cwd=bad))
    assert _flat() == before and _no_round() and prs.calls == []


def test_a_rework_passes_with_an_open_engineer_ask_on_the_work(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _finished(monkeypatch, tmp_path)
    qid = mcp_server.ask_engineer_impl("m1", "Keep the branch?", task_key="K")["question_id"]
    _register(monkeypatch, ["registered"])
    assert asyncio.run(pipeline_rework_impl("m1", "K", "design", ASK))["state"] == "committed"
    assert mcp_server._find_question_paths(qid) != []
