import asyncio
import json
import os
import time

import pytest

from dockwright import mcp_server as ms
from dockwright import paths, stale_monitor, state, topic_message, transcript
from dockwright import pipeline_reconcile as pr
from dockwright.mcp_server import (artifact_put_impl, pipeline_advance_impl, resume_worker_impl,
                                   send_manager_to_worker_auto_impl)
from tests.test_pipeline_core import SCHEMA
from tests.test_pipeline_reconcile import (MGR, _close_session, _complete_design_outputs,  # noqa: F401
                                           _fake_registration, _fake_resume_tab, _fold, _of, _register_worker,
                                           _started_at_design, fresh_orchestrator_dir, session_logs)

RESUME_FROM = "resume from: {checkpoints}\n"
NO_CHECKPOINTS = SCHEMA.replace('checkpoints = ["design.explainer"]\n', "")
SYNTHETIC_BANNER = {"type": "assistant", "message": {
    "id": "banner", "model": "<synthetic>", "content": [{"type": "text", "text": "You've reached your limit"}],
    "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
              "cache_read_input_tokens": 0}}}


@pytest.fixture(autouse=True)
def killed(monkeypatch):
    calls = []

    def kill(worker, dry_run=False):
        calls.append(worker)
        (paths.ACTIVE / f"{worker}.json").unlink(missing_ok=True)
        return {}

    monkeypatch.setattr("dockwright.mcp_server.kill_worker_impl", kill)
    return calls


@pytest.fixture
def context(monkeypatch):
    held = {"tokens": 0}
    monkeypatch.setattr(transcript, "context_tokens", lambda sid, runtime="claude": held["tokens"])
    return held


def _log(tmp_path, *usages, sidechain=None, banner=False):
    path = tmp_path / "s.jsonl"
    lines = [json.dumps({"type": "assistant", "message": {"id": f"m{i}", "usage": u}}) for i, u in enumerate(usages)]
    if sidechain is not None:
        lines.append(json.dumps({"type": "assistant", "isSidechain": True,
                                 "message": {"id": "side", "usage": sidechain}}))
    if banner:
        lines.append(json.dumps(SYNTHETIC_BANNER))
    path.write_text("\n".join([json.dumps({"type": "user"}), *lines]) + "\n")
    return path


def _transcript(tmp_path, monkeypatch, sid, banner=False):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "home" / ".claude" / "projects" / "-w"
    project.mkdir(parents=True, exist_ok=True)
    _log(tmp_path, {"input_tokens": 2, "cache_read_input_tokens": 590000, "cache_creation_input_tokens": 9998},
         banner=banner).rename(project / f"{sid}.jsonl")
    assert transcript.context_tokens(sid) == 600000


def _no_resume(monkeypatch):
    resumed = []

    async def no_resume(*args, **kwargs):
        resumed.append(args)
        raise AssertionError("resume attempted")

    monkeypatch.setattr(ms, "resume_worker_impl", no_resume)
    return resumed


def _checkpoint(sid, body="half"):
    return artifact_put_impl("K", "design", "explainer", body, "partial", sid, scope="checkpoint", overwrite=True)


def _set_active(sid, **fields):
    record = state.read_json(paths.ACTIVE / f"{sid}.json")
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {**record, **fields})


def _restarts():
    return [e for e in _of("advance_requested") if e.get("kind") == "restart"]


def test_context_tokens_reads_the_last_assistant_message(tmp_path, monkeypatch):
    path = _log(tmp_path, {"input_tokens": 1, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 3},
                {"input_tokens": 10, "cache_read_input_tokens": 300000, "cache_creation_input_tokens": 14320,
                 "output_tokens": 900},
                sidechain={"input_tokens": 5, "cache_read_input_tokens": 7})
    monkeypatch.setattr("dockwright.transcript.find_session_log", lambda sid, runtime="claude", root=None: path)
    assert transcript.context_tokens("s") == 314330
    monkeypatch.setattr("dockwright.transcript.find_session_log", lambda sid, runtime="claude", root=None: None)
    assert transcript.context_tokens("s") is None


def test_context_tokens_reads_past_a_synthetic_banner(tmp_path, monkeypatch):
    path = _log(tmp_path, {"input_tokens": 2, "cache_read_input_tokens": 590000, "cache_creation_input_tokens": 9998},
                banner=True)
    monkeypatch.setattr("dockwright.transcript.find_session_log", lambda sid, runtime="claude", root=None: path)
    assert transcript.context_tokens("s") == 600000


def test_a_checkpoint_put_over_the_threshold_asks_for_a_fresh_session(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                     context):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    context["tokens"] = 500000
    first = _checkpoint(sid)
    assert first["refresh"] is True and "500000" in first["refresh_note"]
    assert "fresh session" in first["refresh_note"]
    assert [(e["token_id"], e["assignment_id"], e["sid"], e["tokens"]) for e in _of("refresh_requested")] == [
        ("t1", aid, sid, 500000)]
    assert _checkpoint(sid, "more")["refresh"] is True
    assert len(_of("refresh_requested")) == 1
    context["tokens"] = 499999
    assert "refresh" not in _checkpoint(sid, "less")


def test_only_the_holders_declared_checkpoint_asks(fresh_orchestrator_dir, tmp_path, monkeypatch, context):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _register_worker("other", "sid-other", "a-other", "K")
    context["tokens"] = 900000
    assert "refresh" not in artifact_put_impl("K", "design", "explainer", "x", "partial", "sid-other",
                                              scope="checkpoint")
    assert "refresh" not in artifact_put_impl("K", "design", "hld", "x", "complete", sid, scope="output")
    assert "refresh" not in artifact_put_impl("K", "design", "notes", "x", "partial", sid, scope="checkpoint")
    assert "refresh" not in artifact_put_impl("K", "design", "explainer", "x", "partial", sid, overwrite=True)
    assert _of("refresh_requested") == []


def test_an_idle_holder_with_a_refresh_request_is_replaced(fresh_orchestrator_dir, tmp_path, monkeypatch, context,
                                                          killed):
    sid, aid = _started_at_design(monkeypatch, tmp_path, design_brief=RESUME_FROM)
    context["tokens"] = 600000
    _checkpoint(sid)
    _set_active(sid, state="idle")
    calls = _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR) == []
    assert killed == [sid] and [c["name"] for c in calls] == ["K-design-r2"]
    assert "resume from: design.explainer" in calls[0]["initial_prompt"]
    assert "previous session died" in calls[0]["initial_prompt"]
    assert _fold().tokens["t1"].holder not in (None, aid)
    assert [e["old"] for e in _of("holder_restamped")] == [aid]


def test_a_processing_holder_is_left_alone(fresh_orchestrator_dir, tmp_path, monkeypatch, context, killed):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    context["tokens"] = 600000
    _checkpoint(sid)
    _set_active(sid, state="processing")
    calls = _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR) == []
    assert killed == [] and calls == [] and _restarts() == []
    assert _fold().tokens["t1"].holder == aid


def test_an_idle_holder_without_a_refresh_request_is_left_alone(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                               context, killed):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    context["tokens"] = 600000
    _set_active(sid, state="idle")
    calls = _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR) == []
    assert killed == [] and calls == [] and _restarts() == []


def test_a_dead_holder_with_complete_outputs_is_replaced_too(fresh_orchestrator_dir, tmp_path, monkeypatch, context,
                                                            killed):
    sid, aid = _started_at_design(monkeypatch, tmp_path, design_brief=RESUME_FROM)
    _complete_design_outputs(sid)
    context["tokens"] = 600000
    _checkpoint(sid)
    (paths.ACTIVE / f"{sid}.json").unlink()
    calls = _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR) == []
    assert killed == [] and [c["name"] for c in calls] == ["K-design-r2"]
    assert "resume from: design.explainer" in calls[0]["initial_prompt"]
    assert _fold().tokens["t1"].holder not in (None, aid)


def test_an_idle_holder_with_a_live_background_shell_is_replaced(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                 context, killed):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    context["tokens"] = 600000
    _checkpoint(sid)
    pid = os.getpid()
    _set_active(sid, state="idle", pid=pid)
    index = {"command_by_pid": {pid: "/usr/local/bin/claude --resume x", pid + 1: "/bin/zsh -c sleep 600"},
             "child_commands": {pid: ["/bin/zsh -c sleep 600"]}, "parent_by_pid": {pid + 1: pid}}
    monkeypatch.setattr(ms, "_process_index", lambda: index)
    assert stale_monitor._has_live_background_shell(state.read_json(paths.ACTIVE / f"{sid}.json"),
                                                    ms._process_index())
    calls = _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR) == []
    assert killed == [sid] and [c["name"] for c in calls] == ["K-design-r2"]


def test_the_managers_follow_up_to_a_closed_holder_over_the_threshold_restarts_it_with_the_message(
        fresh_orchestrator_dir, tmp_path, monkeypatch, killed):
    sid, aid = _started_at_design(monkeypatch, tmp_path, design_brief=RESUME_FROM)
    _checkpoint(sid)
    _close_session(sid, "K-design")
    _transcript(tmp_path, monkeypatch, sid, banner=True)
    resumed = _no_resume(monkeypatch)
    calls = _fake_registration(monkeypatch, ["registered"])
    out = asyncio.run(send_manager_to_worker_auto_impl("K-design", "teammate review on #12"))
    assert resumed == [] and killed == [] and [c["name"] for c in calls] == ["K-design-r2"]
    prompt = calls[0]["initial_prompt"]
    assert "teammate review on #12" in prompt and "resume from: design.explainer" in prompt
    assert out["replaced"] is True and out["worker"] == "K-design-r2"
    assert _fold().tokens["t1"].holder not in (None, aid)


def test_a_topic_message_to_a_holder_over_the_threshold_replaces_it(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                    context, killed):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _set_active(sid, state="idle")
    context["tokens"] = 600000
    calls = _fake_registration(monkeypatch, ["registered"])
    events = ms._read_events(paths.artifact_events_path("K"))
    out = asyncio.run(topic_message._to_holder("K", _fold().tokens["t1"], events,
                                               topic_message.marker("decision.msg-1", "engineer"), "hello"))
    assert out["route"] == "replace" and out["ok"] is True
    assert killed == [sid] and [c["name"] for c in calls] == ["K-design-r2"]
    assert "[TOPIC decision.msg-1 from engineer] hello" in calls[0]["initial_prompt"]


@pytest.mark.parametrize("tokens,hold", [(499999, False), (600000, True)])
def test_a_topic_message_the_engine_may_not_replace_for_keeps_the_pane(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                       context, killed, tokens, hold):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    if hold:
        asyncio.run(pipeline_advance_impl("m1", "K", "?", reason="wait for the reporter"))
    _set_active(sid, state="idle")
    context["tokens"] = tokens
    typed = []
    monkeypatch.setattr(ms, "_resolve_live_worker_window", lambda record: "%p1")
    monkeypatch.setattr(ms, "_type_unguarded", lambda wid, text: typed.append(text) or True)
    calls = _fake_registration(monkeypatch, [])
    events = ms._read_events(paths.artifact_events_path("K"))
    out = asyncio.run(topic_message._to_holder("K", _fold().tokens["t1"], events,
                                               topic_message.marker("decision.msg-1", "engineer"), "hello"))
    assert out["route"] == "pane" and typed and killed == [] and calls == []


def test_an_attached_idle_holder_is_not_replaced_and_the_topic_is_queued(fresh_orchestrator_dir, tmp_path,
                                                                           monkeypatch, context, killed):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _set_active(sid, state="idle", human_attached_at=time.time())
    context["tokens"] = 600000
    _checkpoint(sid)
    calls = _fake_registration(monkeypatch, [])
    assert pr.reconcile(MGR) == [] and killed == [] and calls == [] and _restarts() == []
    monkeypatch.setattr(ms, "_resolve_live_worker_window", lambda record: "%p1")
    typed = []
    monkeypatch.setattr(ms, "_type_unguarded", lambda wid, text: typed.append(text) or True)
    events = ms._read_events(paths.artifact_events_path("K"))
    out = asyncio.run(topic_message._to_holder("K", _fold().tokens["t1"], events,
                                               topic_message.marker("decision.msg-1", "manager"), "hello"))
    assert (out["route"], out["ok"], out["reason"]) == ("pane", False, "human_attached")
    assert typed == [] and killed == [] and calls == []


def test_resume_refuses_a_node_over_the_threshold(fresh_orchestrator_dir, tmp_path, monkeypatch, context):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _close_session(sid, "K-design")
    context["tokens"] = 500000
    tabs = _fake_resume_tab(monkeypatch)
    with pytest.raises(ValueError, match=r"'K-design' holds 500000 context tokens, at or over \[pipeline\] "
                                         r"fresh_context_tokens"):
        asyncio.run(resume_worker_impl("K-design"))
    assert tabs == []


def test_a_holder_the_kill_does_not_stop_is_paged_and_not_restarted(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                    context):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    context["tokens"] = 600000
    _checkpoint(sid)
    _set_active(sid, state="idle")
    monkeypatch.setattr("dockwright.mcp_server.kill_worker_impl", lambda worker, dry_run=False: {})
    monkeypatch.setattr(pr, "_STOP_WAIT_SEC", 0)
    calls = _fake_registration(monkeypatch, [])
    lines = pr.reconcile(MGR)
    assert len(lines) == 1 and lines[0].startswith("RECONCILE_ERROR K") and "still runs after the kill" in lines[0]
    assert calls == [] and _restarts() == [] and _fold().tokens["t1"].holder == aid


def test_a_refused_replace_names_the_hold(fresh_orchestrator_dir, tmp_path, monkeypatch, killed):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    asyncio.run(pipeline_advance_impl("m1", "K", "?", reason="wait for the reporter"))
    _close_session(sid, "K-design")
    _transcript(tmp_path, monkeypatch, sid)
    _no_resume(monkeypatch)
    with pytest.raises(ValueError, match=r"the token is held: release it"):
        asyncio.run(send_manager_to_worker_auto_impl("K-design", "the reporter answered"))
    assert _restarts() == [] and killed == []


def test_the_managers_message_survives_a_restart_the_engine_has_to_spawn_again(fresh_orchestrator_dir, tmp_path,
                                                                               monkeypatch, killed):
    sid, aid = _started_at_design(monkeypatch, tmp_path, design_brief=RESUME_FROM)
    _close_session(sid, "K-design")
    _transcript(tmp_path, monkeypatch, sid)
    _no_resume(monkeypatch)
    _fake_registration(monkeypatch, ["no_register"])
    out = asyncio.run(send_manager_to_worker_auto_impl("K-design", "teammate review on #12"))
    assert out["status"] == "replace_pending" and out["replaced"] is False
    for pending in paths.ASSIGNMENTS_PENDING.glob("*.json"):
        pending.unlink()
    calls = _fake_registration(monkeypatch, ["registered"])
    assert pr.reconcile(MGR, now=time.time() + 400) == []
    assert len(calls) == 1 and "teammate review on #12" in calls[0]["initial_prompt"]
    assert _fold().tokens["t1"].holder not in (None, aid) and len(_restarts()) == 1


def test_resume_keeps_a_node_without_checkpoints_over_the_threshold(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                     context):
    sid, aid = _started_at_design(monkeypatch, tmp_path, schema_text=NO_CHECKPOINTS)
    _close_session(sid, "K-design")
    context["tokens"] = 600000
    tabs = _fake_resume_tab(monkeypatch)
    asyncio.run(resume_worker_impl("K-design"))
    assert len(tabs) == 1


def test_the_managers_follow_up_to_a_node_without_checkpoints_over_the_threshold_resumes_it(
        fresh_orchestrator_dir, tmp_path, monkeypatch, killed):
    sid, aid = _started_at_design(monkeypatch, tmp_path, schema_text=NO_CHECKPOINTS)
    _close_session(sid, "K-design")
    _transcript(tmp_path, monkeypatch, sid)
    resumed = _no_resume(monkeypatch)
    calls = _fake_registration(monkeypatch, [])
    with pytest.raises(AssertionError, match="resume attempted"):
        asyncio.run(send_manager_to_worker_auto_impl("K-design", "teammate review on #12"))
    assert len(resumed) == 1 and calls == [] and _restarts() == [] and killed == []


def test_a_topic_message_to_a_node_without_checkpoints_over_the_threshold_keeps_the_pane(
        fresh_orchestrator_dir, tmp_path, monkeypatch, context, killed):
    sid, aid = _started_at_design(monkeypatch, tmp_path, schema_text=NO_CHECKPOINTS)
    _set_active(sid, state="idle")
    context["tokens"] = 600000
    typed = []
    monkeypatch.setattr(ms, "_resolve_live_worker_window", lambda record: "%p1")
    monkeypatch.setattr(ms, "_type_unguarded", lambda wid, text: typed.append(text) or True)
    calls = _fake_registration(monkeypatch, [])
    events = ms._read_events(paths.artifact_events_path("K"))
    out = asyncio.run(topic_message._to_holder("K", _fold().tokens["t1"], events,
                                               topic_message.marker("decision.msg-1", "engineer"), "hello"))
    assert out["route"] == "pane" and typed and killed == [] and calls == []
    replaced = asyncio.run(pr.replace_for_message("K", _fold().tokens["t1"], "hello"))
    assert replaced["ok"] is False and "declares no checkpoint" in replaced["reason"] and killed == []
