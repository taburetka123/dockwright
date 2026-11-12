import asyncio
import json
import os
import time

import pytest

from dockwright import board, board_web, lane_lines, paths, state, topic_message, topic_outbox, waiting_on
from dockwright import mcp_server as ms
from dockwright import pipeline_reconcile as pr
from tests.test_mcp_tools import _probes
from tests.test_pipeline_reconcile import (MGR, _fake_registration, _later, _started_at_design, clock,  # noqa: F401
                                           fresh_orchestrator_dir, killed, session_logs)

DEAD = 424299
VERDICT = '{"decision": "approved"}'
URL = "http://127.0.0.1:1234/"
LOST = "gate lost — message the topic to reopen"


def _open_gate(key, pid, *, node="design", round_=1, out="/nonexistent"):
    ms.artifact_put_impl(key, "gate", "open", json.dumps({"url": URL, "port": 1234, "pid": pid,
                                                           "out": out, "task_key": key, "round": round_,
                                                           "node": node, "folder": "/r/", "decisions": [1]}),
                         "complete", writer_sid="node-sid", scope="checkpoint", overwrite=True)


def _delivered(monkeypatch):
    monkeypatch.setattr(topic_message, "_route", lambda *a, **k: asyncio.sleep(0, {"route": "resume", "ok": True,
                                                                                   "reason": None}))


def _verdict(round_=1, text=VERDICT):
    ms.artifact_put_impl("K", "decision", f"verdict-r{round_}", text, "complete", writer_sid="topic:cli",
                         scope="output", decided_by="engineer")


def test_a_verdict_is_recorded_verbatim_as_the_engineers(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _started_at_design(monkeypatch, tmp_path)
    _delivered(monkeypatch)
    out = asyncio.run(topic_message.deliver("K", VERDICT, kind="verdict", decided_by="engineer", via="cli",
                                            gate_round=2))
    record = ms.artifact_get_impl("K", "decision", "verdict-r2")
    assert out["record"] == "decision.verdict-r2" and record["content"] == VERDICT
    assert record["decided_by"] == "engineer"


def test_a_verdict_over_the_message_limit_is_kept_whole(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _started_at_design(monkeypatch, tmp_path)
    _delivered(monkeypatch)
    text = json.dumps({"decision": "annotated", "feedback": "x" * (topic_message.TEXT_MAX_BYTES + 10)})
    asyncio.run(topic_message.deliver("K", text, kind="verdict", decided_by="engineer", via="cli", gate_round=1))
    assert ms.artifact_get_impl("K", "decision", "verdict-r1")["content"] == text


@pytest.mark.parametrize("text, decided_by, gate_round", [
    (VERDICT, "manager", 1), ("not json", "engineer", 1), (VERDICT, "engineer", None), (VERDICT, "engineer", 0),
    (VERDICT, "engineer", True), ('{"feedback": "x"}', "engineer", 1), ('["approved"]', "engineer", 1),
])
def test_a_bad_verdict_writes_nothing(fresh_orchestrator_dir, monkeypatch, tmp_path, text, decided_by, gate_round):
    _started_at_design(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(topic_message.deliver("K", text, kind="verdict", decided_by=decided_by, via="cli",
                                          gate_round=gate_round))
    assert not paths.artifact_path("K", "decision", "verdict-r1").exists()


def test_a_second_different_verdict_for_a_round_is_refused(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _started_at_design(monkeypatch, tmp_path)
    _delivered(monkeypatch)
    asyncio.run(topic_message.deliver("K", VERDICT, kind="verdict", decided_by="engineer", via="cli", gate_round=1))
    with pytest.raises(ValueError):
        asyncio.run(topic_message.deliver("K", '{"decision": "annotated"}', kind="verdict", decided_by="engineer",
                                          via="gate", gate_round=1))
    assert ms.artifact_get_impl("K", "decision", "verdict-r1")["content"] == VERDICT


def _backdate_start(key, seconds):
    events_path = paths.artifact_events_path(key)
    lines = events_path.read_text().splitlines()
    for i, line in enumerate(lines):
        event = json.loads(line)
        event["ts"] = event["ts"] - seconds
        lines[i] = json.dumps(event)
    events_path.write_text("\n".join(lines) + "\n")


def _parked(monkeypatch, tmp_path, out="/nonexistent"):
    holder_sid, _ = _started_at_design(monkeypatch, tmp_path)
    _backdate_start("K", 2 * waiting_on.STALL_GRACE_SEC)
    ms.worker_done_impl(holder_sid, "PARKED at gate — the verdict comes through the store")
    (paths.ACTIVE / f"{holder_sid}.json").unlink()
    state.write_json_atomic(paths.CLOSED / f"{holder_sid}.json", {
        "claude_sid": holder_sid, "name": "K-design", "closed_reason": "idle>7200s", "closed_at": time.time() - 10})
    _probes(monkeypatch)
    monkeypatch.setattr(ms, "_process_command", lambda pid, index=None: f"perl -e … {out} K 1 dockwright")
    return holder_sid


def _row(key):
    return next(r for r in board.topics()["rows"] if r["key"] == key)


def test_an_open_gate_reads_human_gate_without_a_live_holder(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _parked(monkeypatch, tmp_path)
    _open_gate("K", os.getpid())
    assert ms._gate_state("K")["state"] == "open"
    assert _row("K")["waiting_on"] == waiting_on.HUMAN_GATE


def test_an_open_gate_of_another_node_is_not_this_nodes_wait(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _parked(monkeypatch, tmp_path)
    _open_gate("K", os.getpid(), node="plan")
    assert _row("K")["waiting_on"] == "stalled:design"


def test_a_gate_whose_process_died_is_lost_and_says_so(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _parked(monkeypatch, tmp_path)
    _open_gate("K", DEAD)
    assert ms._gate_state("K")["state"] == "lost"
    assert _row("K")["waiting_on"] == "stalled:design"
    texts = [i["text"] for i in board.inbox()["stopped"] if i["key"] == "K"]
    assert texts == [LOST]


def test_a_reused_pid_reads_as_a_lost_gate(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _parked(monkeypatch, tmp_path)
    monkeypatch.setattr(ms, "_process_command", lambda pid, index=None: "/usr/bin/some-other-program")
    _open_gate("K", os.getpid())
    assert ms._gate_state("K")["state"] == "lost"


def test_a_newer_verdict_closes_the_gate(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _parked(monkeypatch, tmp_path)
    _open_gate("K", os.getpid())
    _verdict()
    assert ms._gate_state("K") is None


def test_a_gate_whose_round_has_a_verdict_is_closed_whatever_its_written_at(fresh_orchestrator_dir, monkeypatch,
                                                                            tmp_path):
    _parked(monkeypatch, tmp_path)
    _verdict()
    _open_gate("K", os.getpid(), round_=1)
    assert ms._gate_state("K") is None
    _open_gate("K", os.getpid(), round_=2)
    assert ms._gate_state("K")["state"] == "open"


def test_the_gate_state_carries_the_supervisors_relaunch_lines(fresh_orchestrator_dir, monkeypatch, tmp_path):
    out = tmp_path / "state" / "abc.r1.out"
    out.parent.mkdir()
    out.write_text("")
    out.with_suffix(".log").write_text("relaunched 2026-09-30T01:00:00Z (dismissed)\nperl: some stderr\n"
                                       "relaunched 2026-09-30T02:00:00Z (dismissed)\n")
    _parked(monkeypatch, tmp_path, out=str(out))
    _open_gate("K", os.getpid(), out=str(out))
    assert ms._gate_state("K")["relaunches"] == ["relaunched 2026-09-30T01:00:00Z (dismissed)",
                                                 "relaunched 2026-09-30T02:00:00Z (dismissed)"]
    status = ms.pipeline_status_impl("K")
    assert f"gate: open r1 design {URL} · relaunched 2× (last 2026-09-30T02:00:00Z)" in status.splitlines()
    assert json.loads(ms.pipeline_status_impl("K", "json"))["gate"]["state"] == "open"


def test_the_first_true_row_puts_an_open_gate_after_a_live_gate():
    assert waiting_on.token_value(waiting_on.PositionFacts("design", gate_open=True)) == waiting_on.HUMAN_GATE
    assert waiting_on.token_value(waiting_on.PositionFacts("design", question=True, gate_open=True)) == \
        waiting_on.HUMAN_QUESTION


def _parked_lines(lines):
    return [line for line in lines if line.startswith("NODE_PARKED")]


def test_no_parked_page_while_the_gate_is_open(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _parked(monkeypatch, tmp_path)
    _open_gate("K", os.getpid())
    calls = _fake_registration(monkeypatch, [])
    assert _parked_lines(pr.reconcile(MGR, now=_later(5))) == []
    assert calls == []


def test_a_lost_gate_pages_parked_and_names_the_topic_message(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _parked(monkeypatch, tmp_path)
    _open_gate("K", DEAD)
    lines = _parked_lines(pr.reconcile(MGR, now=_later(5)))
    assert len(lines) == 1
    assert "his message to the topic reopens it" in lines[0]


def test_the_parked_step_names_the_lost_gate_only_when_it_is_lost():
    fields = {"task_key": "K", "node": "design", "name": "K-design", "gate": True, "reason": "idle>7200s"}
    assert "reopens" not in lane_lines.render("NODE_PARKED", **fields, gate_lost=False)
    assert "reopens" in lane_lines.render("NODE_PARKED", **fields, gate_lost=True)


def test_a_message_to_a_node_at_an_open_gate_that_cannot_resume_restarts_it(fresh_orchestrator_dir, monkeypatch,
                                                                            tmp_path):
    _parked(monkeypatch, tmp_path)
    _open_gate("K", os.getpid())
    manager_pid = state.read_json(paths.ACTIVE / "m1.json")["pid"]
    monkeypatch.setattr(ms, "_pid_alive", lambda pid: pid == manager_pid)
    token = ms._engine_fold("K", "test").tokens["t1"]
    calls = _fake_registration(monkeypatch, ["registered"])
    assert asyncio.run(pr.recover_for_message("K", token)) == {"route": "restart", "ok": True, "reason": None}
    assert len(calls) == 1 and calls[0]["name"].startswith("K-design-r")
    assert ms._engine_fold("K", "test").tokens["t1"].holder != token.holder


def test_a_verdict_restarts_a_parked_node_that_cannot_resume(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _parked(monkeypatch, tmp_path)
    _open_gate("K", os.getpid())
    manager_pid = state.read_json(paths.ACTIVE / "m1.json")["pid"]
    monkeypatch.setattr(ms, "_pid_alive", lambda pid: pid == manager_pid)
    monkeypatch.setattr(topic_message, "_resumable", lambda name, sid: False)
    calls = _fake_registration(monkeypatch, ["registered"])
    out = asyncio.run(topic_message.deliver("K", VERDICT, kind="verdict", decided_by="engineer", via="cli",
                                            gate_round=1))
    assert (out["route"], out["delivered"]) == ("restart", True) and len(calls) == 1
    assert not topic_outbox.entries()


def _verdict_out(tmp_path, age_sec, now):
    out = tmp_path / "state" / "abc.r1.out"
    out.parent.mkdir(exist_ok=True)
    out.write_text(VERDICT)
    os.utime(out, (now - age_sec, now - age_sec))
    return out


def test_the_reconcile_harvests_a_verdict_its_wrapper_never_sent(fresh_orchestrator_dir, monkeypatch, tmp_path):
    now = time.time()
    out = _verdict_out(tmp_path, 120, now)
    _parked(monkeypatch, tmp_path, out=str(out))
    _open_gate("K", DEAD, out=str(out))
    _delivered(monkeypatch)
    pr.reconcile(MGR, now=now)
    record = ms.artifact_get_impl("K", "decision", "verdict-r1")
    assert record["content"] == VERDICT and record["decided_by"] == "engineer"
    written = record["written_at"]
    pr.reconcile(MGR, now=now + 400)
    assert ms.artifact_get_impl("K", "decision", "verdict-r1")["written_at"] == written
    deliveries = [e for e in ms._read_events(paths.artifact_events_path("K")) if e.get("type") == "message_delivered"]
    assert [(e["record"], e["via"]) for e in deliveries] == [("decision.verdict-r1", "gate")]


def test_a_fresh_verdict_is_left_to_its_wrapper_for_a_minute(fresh_orchestrator_dir, monkeypatch, tmp_path):
    now = time.time()
    out = _verdict_out(tmp_path, 10, now)
    _parked(monkeypatch, tmp_path, out=str(out))
    _open_gate("K", os.getpid(), out=str(out))
    _delivered(monkeypatch)
    pr.reconcile(MGR, now=now)
    assert not paths.artifact_path("K", "decision", "verdict-r1").exists()
    pr.reconcile(MGR, now=now + 61)
    assert ms.artifact_get_impl("K", "decision", "verdict-r1")["content"] == VERDICT


def test_an_empty_out_file_is_never_harvested(fresh_orchestrator_dir, monkeypatch, tmp_path):
    now = time.time()
    out = _verdict_out(tmp_path, 120, now)
    out.write_text("")
    os.utime(out, (now - 120, now - 120))
    _parked(monkeypatch, tmp_path, out=str(out))
    _open_gate("K", DEAD, out=str(out))
    _delivered(monkeypatch)
    pr.reconcile(MGR, now=now)
    assert not paths.artifact_path("K", "decision", "verdict-r1").exists()


def test_the_inbox_links_the_open_gate(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _parked(monkeypatch, tmp_path)
    _open_gate("K", os.getpid())
    data = board.inbox()
    gate = next(i for i in data["waiting"] if i["key"] == "K" and i["kind"] == "gate")
    assert gate["link"] == URL
    assert f'<a href="{URL}"' in board_web.inbox_page(data)
    assert URL in board.render_inbox(data)


def test_the_cli_takes_a_verdict_file(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _started_at_design(monkeypatch, tmp_path)
    _delivered(monkeypatch)
    monkeypatch.delenv("CLAUDE_AGENT", raising=False)
    path = tmp_path / "verdict.out"
    path.write_text(VERDICT)
    assert topic_message.main(["message", "K", "--kind", "verdict", "--round", "1", "--file", str(path)]) == 0
    assert ms.artifact_get_impl("K", "decision", "verdict-r1")["decided_by"] == "engineer"


def test_the_cli_refuses_a_verdict_from_an_agent_session(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _started_at_design(monkeypatch, tmp_path)
    _delivered(monkeypatch)
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    path = tmp_path / "verdict.out"
    path.write_text(VERDICT)
    assert topic_message.main(["message", "K", "--kind", "verdict", "--round", "1", "--file", str(path)]) == 1
    assert not paths.artifact_path("K", "decision", "verdict-r1").exists()


_SCRIPTS = "/opt/py/Python.app/Contents/MacOS/Python /u/.claude/scripts"


def _author_wait(monkeypatch, key="K", node="design"):
    commands = {600: f"{_SCRIPTS}/{waiting_on.AUTHOR_WAIT_SCRIPT} --task-key {key} --node {node} --issue {key}",
                601: f"{_SCRIPTS}/{waiting_on.TRACKER_WATCH_SCRIPT} {key} --poll 300"}
    index = {"command_by_pid": commands, "child_commands": {}, "parent_by_pid": {600: 1, 601: 600}}
    monkeypatch.setattr(ms, "_process_index", lambda: index)


def test_no_parked_page_while_the_nodes_author_wait_lives(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _parked(monkeypatch, tmp_path)
    _author_wait(monkeypatch)
    calls = _fake_registration(monkeypatch, [])
    assert _parked_lines(pr.reconcile(MGR, now=_later(5))) == []
    assert calls == []


@pytest.mark.parametrize("key, node", [("K", "plan"), ("OTHER", "design")])
def test_an_author_wait_of_another_node_or_work_still_pages_parked(fresh_orchestrator_dir, monkeypatch, tmp_path,
                                                                  key, node):
    _parked(monkeypatch, tmp_path)
    _author_wait(monkeypatch, key, node)
    assert len(_parked_lines(pr.reconcile(MGR, now=_later(5)))) == 1


def test_an_author_wait_reads_the_external_tracker_value_at_its_own_node_only(fresh_orchestrator_dir, monkeypatch,
                                                                              tmp_path):
    _parked(monkeypatch, tmp_path)
    _author_wait(monkeypatch)
    assert _row("K")["waiting_on"] == waiting_on.EXTERNAL_TRACKER
    _author_wait(monkeypatch, node="plan")
    assert _row("K")["waiting_on"] == "stalled:design"


def test_a_lost_gate_at_the_node_still_pages_beside_its_author_wait(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _parked(monkeypatch, tmp_path)
    _open_gate("K", DEAD)
    _author_wait(monkeypatch)
    lines = _parked_lines(pr.reconcile(MGR, now=_later(5)))
    assert len(lines) == 1 and "its gate died" in lines[0]
    assert _row("K")["waiting_on"] == "stalled:design"
    assert [i["text"] for i in board.inbox()["stopped"] if i["key"] == "K"] == [LOST]
