import json
import os
import sys
import time
import types
import pytest
from dockwright import config, lane_io, mcp_server, paths, state, monitor, terminal
from dockwright.lane_io import LaneDead


@pytest.fixture(autouse=True)
def _two_minute_silent_finish_grace(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_SILENT_FINISH_GRACE_SEC", "120")


@pytest.fixture
def fresh_orchestrator_dir(tmp_path, monkeypatch):
    terminal._DRIVER = None
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(paths, "DONE", tmp_path / "done")
    monkeypatch.setattr(paths, "TURN_ENDS", tmp_path / "turn-ends")
    monkeypatch.setattr(paths, "QUESTIONS", tmp_path / "questions")
    monkeypatch.setattr(paths, "ANSWERS", tmp_path / "answers")
    monkeypatch.setattr(paths, "CLOSED", tmp_path / "closed")
    monkeypatch.setattr(paths, "HANDOFFS", tmp_path / "handoffs")
    monkeypatch.setattr(paths, "PRESETS", tmp_path / "presets")
    monkeypatch.setattr(paths, "SLOTS", tmp_path / "slots")
    monkeypatch.setattr(paths, "MANAGER_MEMORY", tmp_path / "manager-memory")
    monkeypatch.setattr(paths, "ARCHITECT", tmp_path / "architect")
    monkeypatch.setattr(paths, "ARTIFACTS", tmp_path / "artifacts")
    monkeypatch.setattr(paths, "ASSIGNMENTS", tmp_path / "assignments")
    monkeypatch.setattr(paths, "ASSIGNMENTS_PENDING", tmp_path / "assignments" / ".pending")
    paths.ensure_dirs()
    state.write_json_atomic(paths.ACTIVE / "mgr-test.json", {
        "claude_sid": "mgr-test",
        "agent": "manager",
        "name": "test-mgr",
        "window_id": "test-win",
        "pid": os.getpid(),
        "domain": "general",
    })
    monkeypatch.setenv("TMUX_PANE", "test-win")
    yield tmp_path


def _write_done(manager_name: str, sid: str, summary: str = "ok"):
    target_dir = paths.DONE / manager_name
    target_dir.mkdir(parents=True, exist_ok=True)
    payload = {"event_id": sid, "claude_sid": sid, "worker_name": sid,
               "parent_manager_name": manager_name, "summary": summary,
               "completed_at": time.time()}
    state.write_json_atomic(target_dir / f"{sid}-{int(time.time()*1000)}.json", payload)


def _write_turn_end(manager_name: str, sid: str):
    target_dir = paths.TURN_ENDS / manager_name
    target_dir.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(target_dir / f"{sid}-{int(time.time()*1000)}.json",
                            {"sid": sid, "name": sid, "completed_at": time.time()})


def _write_question(manager_name: str, qid: str, worker_name: str, question: str = "help?"):
    target_dir = paths.QUESTIONS / manager_name
    target_dir.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(target_dir / f"{qid}.json", {
        "question_id": qid,
        "worker_sid": f"{worker_name}-sid",
        "worker_name": worker_name,
        "parent_manager_name": manager_name,
        "question": question,
        "asked_at": time.time(),
    })


def test_monitor_done_emits_new_events(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_done("test-mgr", "wkr-a", summary="finished")
    monitor.run_done_scan()
    out = capsys.readouterr().out
    assert "wkr-a" in out
    assert "finished" in out


def test_monitor_done_skips_seen_events(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_done("test-mgr", "wkr-pre", summary="old")
    monitor.run_done_scan()
    capsys.readouterr()
    monitor.run_done_scan()
    assert capsys.readouterr().out == ""


def _write_park(name: str, completed_at: float):
    target_dir = paths.DONE / "test-mgr"
    target_dir.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(target_dir / name, {"event_id": name, "claude_sid": "wkr-p", "worker_name": "wkr-p",
                                                "parent_manager_name": "test-mgr", "summary": "PARKED — x",
                                                "completed_at": completed_at})


def test_a_repeated_park_of_a_session_with_no_task_prints(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_park("wkr-p-1.json", 1000.0)
    _write_park("wkr-p-2.json", 2000.0)
    monitor.run_done_scan()
    assert capsys.readouterr().out.count("DONE wkr-p") == 2


def test_an_unreadable_done_beside_a_park_never_stops_the_scan(fresh_orchestrator_dir, capsys, tmp_path,
                                                               monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    (paths.DONE / "test-mgr").mkdir(parents=True, exist_ok=True)
    (paths.DONE / "test-mgr" / "wkr-p-0.json").write_text("not json")
    _write_park("wkr-p-1.json", 1000.0)
    monitor.run_done_scan()
    assert "PARKED — x" in capsys.readouterr().out


def test_monitor_done_does_not_read_unscoped(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_done(paths.UNSCOPED_BUCKET, "wkr-orphan", summary="legacy")
    _write_done("test-mgr", "wkr-mine", summary="mine")
    monitor.run_done_scan()
    out = capsys.readouterr().out
    assert "wkr-mine" in out
    assert "wkr-orphan" not in out
    assert "legacy" not in out


def _write_aged_turn_end(manager_name: str, sid: str, *, age_sec: float = 300,
                         name: str | None = None, summary: str | None = None,
                         agent: str | None = None):
    target_dir = paths.TURN_ENDS / manager_name
    target_dir.mkdir(parents=True, exist_ok=True)
    ts = time.time() - age_sec
    payload = {"sid": sid, "name": name or sid, "completed_at": ts,
               "last_summary": summary}
    if agent is not None:
        payload["agent"] = agent
    entry = target_dir / f"{sid}-{int(ts*1000)}.json"
    state.write_json_atomic(entry, payload)
    return entry


def _write_worker_record(sid: str, *, worker_state: str = "idle", nested: bool = False,
                         name: str | None = None, tasked_at: float | None = None,
                         processing_since: float | None = None,
                         input_at: float | None = None):
    record = {"claude_sid": sid, "agent": "worker", "name": name or sid,
              "window_id": "", "pid": os.getpid(), "state": worker_state,
              "parent_manager_name": "test-mgr"}
    if nested:
        record["nested"] = True
    if tasked_at is not None:
        record["tasked_at"] = tasked_at
    if processing_since is not None:
        record["processing_since"] = processing_since
    if input_at is not None:
        record["input_at"] = input_at
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", record)


def _assistant_event(text, ts="2026-06-18T08:11:00Z", stop_reason="end_turn", tools=None):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for name in (tools or []):
        content.append({"type": "tool_use", "name": name})
    return {"type": "assistant", "timestamp": ts,
            "message": {"content": content, "stop_reason": stop_reason}}


def _write_transcript(home, sid, events):
    proj = home / ".claude" / "projects" / "-test-proj"
    proj.mkdir(parents=True, exist_ok=True)
    log = proj / f"{sid}.jsonl"
    log.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return log


def test_monitor_turn_ends_young_files_stay_pending(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_turn_end("test-mgr", "wkr-young")
    _write_worker_record("wkr-young")
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""
    monkeypatch.setenv("CLAUDE_ORCH_SILENT_FINISH_GRACE_SEC", "0")
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-young" in capsys.readouterr().out


def test_monitor_turn_ends_emits_finished_silently_with_summary_once(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_aged_turn_end("test-mgr", "wkr-1", name="alpha", summary="opened PR #5, all tests green")
    _write_worker_record("wkr-1", name="alpha")
    monitor.run_turn_ends_scan()
    out = capsys.readouterr().out.strip()
    assert out == ('FINISHED_SILENTLY alpha → get_worker_summary("alpha"); rule out a plannotator park '
                   "(§ Design-gate relays), then harvest, re-instruct it with the worker_done reminder, or close it "
                   "— opened PR #5, all tests green")
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""


def test_monitor_turn_ends_suppresses_when_done_event_fresh(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_aged_turn_end("test-mgr", "wkr-1")
    _write_worker_record("wkr-1")
    _write_done("test-mgr", "wkr-1")
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""


def test_monitor_turn_ends_emits_when_done_event_stale(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    entry = _write_aged_turn_end("test-mgr", "wkr-1")
    _write_worker_record("wkr-1")
    done_dir = paths.DONE / "test-mgr"
    done_dir.mkdir(parents=True, exist_ok=True)
    stale_ts = time.time() - 2 * 3600
    state.write_json_atomic(done_dir / "wkr-1-old.json",
                            {"claude_sid": "wkr-1", "completed_at": stale_ts})
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-1" in capsys.readouterr().out


def test_monitor_turn_ends_suppresses_when_worker_processing(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_aged_turn_end("test-mgr", "wkr-1")
    _write_worker_record("wkr-1", worker_state="processing")
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""


def test_monitor_turn_ends_suppresses_when_pending_question(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_aged_turn_end("test-mgr", "wkr-1")
    _write_worker_record("wkr-1")
    _write_question("test-mgr", "q9", "wkr-1")
    questions = list(paths.QUESTIONS.rglob("*.json"))
    payload = state.read_json(questions[0])
    payload["worker_sid"] = "wkr-1"
    state.write_json_atomic(questions[0], payload)
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""


def test_monitor_turn_ends_suppresses_own_sid(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_aged_turn_end("test-mgr", "mgr-test", agent="manager")
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""


def test_monitor_turn_ends_suppresses_nested_record(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_aged_turn_end("test-mgr", "wkr-ghost")
    _write_worker_record("wkr-ghost", nested=True)
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""


def test_monitor_turn_ends_superseded_older_files_emit_once(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_aged_turn_end("test-mgr", "wkr-1", age_sec=1300)
    _write_aged_turn_end("test-mgr", "wkr-1", age_sec=300)
    _write_worker_record("wkr-1")
    monitor.run_turn_ends_scan()
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    assert out[0].startswith("FINISHED_SILENTLY wkr-1")


def test_monitor_turn_ends_exited_variant_when_record_gone(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_aged_turn_end("test-mgr", "wkr-gone", summary="pushed branch")
    monitor.run_turn_ends_scan()
    out = capsys.readouterr().out.strip()
    assert out.startswith('FINISHED_SILENTLY wkr-gone (session exited) → get_worker_summary("wkr-gone"); ')
    assert out.endswith(" — pushed branch")


def test_monitor_turn_ends_grace_env_override(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("CLAUDE_ORCH_SILENT_FINISH_GRACE_SEC", "0")
    _write_turn_end("test-mgr", "wkr-now")
    _write_worker_record("wkr-now")
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-now" in capsys.readouterr().out


def test_monitor_turn_ends_malformed_file_never_crashes(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    target_dir = paths.TURN_ENDS / "test-mgr"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "wkr-bad-123.json").write_text("{{{not json")
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""


def test_monitor_turn_ends_does_not_read_unscoped(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_aged_turn_end(paths.UNSCOPED_BUCKET, "orphan")
    _write_aged_turn_end("test-mgr", "mine")
    monitor.run_turn_ends_scan()
    out = capsys.readouterr().out.strip()
    assert "FINISHED_SILENTLY mine" in out
    assert "orphan" not in out


def test_turn_burst_holds_newest_lull(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    _write_aged_turn_end("test-mgr", "wkr-poll", age_sec=480)
    _write_aged_turn_end("test-mgr", "wkr-poll", age_sec=300)
    _write_worker_record("wkr-poll")
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""
    seen = tmp_path / ".seen-turn-ends-test-mgr"
    if seen.exists():
        assert "wkr-poll" not in seen.read_text() or \
            sum("wkr-poll" in line for line in seen.read_text().splitlines()) == 1, \
            "only the SUPERSEDED older file may be consumed; the newest must stay pending"


def test_turn_burst_fires_once_past_episode_grace(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    _write_aged_turn_end("test-mgr", "wkr-pend", age_sec=1180)
    _write_aged_turn_end("test-mgr", "wkr-pend", age_sec=1000)
    _write_worker_record("wkr-pend")
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-pend" in capsys.readouterr().out
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""


def test_turn_burst_holds_mid_episode_after_first_lull_paged(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    monkeypatch.setenv("CLAUDE_ORCH_FS_LADDER_BASE_SEC", "1")
    _write_aged_turn_end("test-mgr", "wkr-mid", age_sec=1000)
    _write_worker_record("wkr-mid")
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-mid" in capsys.readouterr().out
    _write_aged_turn_end("test-mgr", "wkr-mid", age_sec=300)
    time.sleep(1.1)
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == "", \
        "mid-episode lull must be held at classification even though its sibling was paged+seen"


def test_singleton_turn_end_unaffected_by_burst_hold(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    _write_aged_turn_end("test-mgr", "wkr-solo", age_sec=300)
    _write_worker_record("wkr-solo")
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-solo" in capsys.readouterr().out


def test_distant_sibling_does_not_engage_burst_hold(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    _write_aged_turn_end("test-mgr", "wkr-gap", age_sec=1500)
    _write_aged_turn_end("test-mgr", "wkr-gap", age_sec=300)
    _write_worker_record("wkr-gap")
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-gap" in capsys.readouterr().out


def test_burst_hold_does_not_delay_exited_session(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    _write_aged_turn_end("test-mgr", "wkr-gone", age_sec=480)
    _write_aged_turn_end("test-mgr", "wkr-gone", age_sec=300)
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-gone (session exited)" in capsys.readouterr().out


def test_monitor_questions_emits_only_current_manager_questions(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_question("test-mgr", "q-mine", "mine-worker", question="ours?")
    _write_question("peer-mgr", "q-peer", "peer-worker", question="theirs?")

    monitor.run_questions_scan()

    out = capsys.readouterr().out
    assert ("QUESTION mine-worker → list_pending_questions(manager_sid=<your sid>), relay the oldest verbatim, "
            "answer_question — ours?") in out
    assert "peer-worker" not in out
    assert "theirs?" not in out


def test_monitor_questions_skips_seen_events(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_question("test-mgr", "q-mine", "mine-worker", question="ours?")
    monitor.run_questions_scan()
    capsys.readouterr()

    monitor.run_questions_scan()

    assert capsys.readouterr().out == ""


def test_monitor_stale_invokes_stale_monitor(fresh_orchestrator_dir, monkeypatch, tmp_path):
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        class R: returncode = 0; stdout = b""; stderr = b""
        return R()
    monkeypatch.setattr(monitor.subprocess, "run", fake_run)
    monitor.run_stale_scan()
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[1:3] == ["-m", "dockwright.stale_monitor"]
    assert cmd[3:] == ["--manager", "test-mgr"]


def _stale_child_ok(monkeypatch):
    monkeypatch.setattr("dockwright.monitor.subprocess.run", lambda *a, **k: types.SimpleNamespace(returncode=0))


def test_stale_scan_calls_reconcile_only_when_not_limited(monkeypatch, fresh_orchestrator_dir):
    called = []
    _stale_child_ok(monkeypatch)
    monkeypatch.setattr("dockwright.pipeline_reconcile.reconcile", lambda mgr: called.append(mgr["name"]) or [])
    monkeypatch.setattr("dockwright.monitor._manager_limited", lambda name: False)
    monitor.run_stale_scan({"name": "mgr", "sid": "m1"})
    assert called == ["mgr"]
    monkeypatch.setattr("dockwright.monitor._manager_limited", lambda name: True)
    monitor.run_stale_scan({"name": "mgr", "sid": "m1"})
    assert called == ["mgr"]


def test_a_failing_reconcile_is_one_lane_line_and_the_scan_returns(monkeypatch, fresh_orchestrator_dir):
    lines = []
    _stale_child_ok(monkeypatch)

    def boom(mgr):
        raise RuntimeError("x" * 300)

    monkeypatch.setattr("dockwright.pipeline_reconcile.reconcile", boom)
    monkeypatch.setattr("dockwright.monitor.emit", lines.append)
    assert monitor.run_stale_scan({"name": "mgr", "sid": "m1"}) is None
    assert lines == ["RECONCILE_ERROR * → dockwright lanes — RuntimeError: " + "x" * 200]


def test_a_dead_lane_inside_reconcile_ends_the_scan(monkeypatch, fresh_orchestrator_dir):
    _stale_child_ok(monkeypatch)

    def dead(mgr):
        raise LaneDead("stdout write failed")

    monkeypatch.setattr("dockwright.pipeline_reconcile.reconcile", dead)
    with pytest.raises(LaneDead):
        monitor.run_stale_scan({"name": "mgr", "sid": "m1"})


def test_main_module_dispatches_monitor_done(fresh_orchestrator_dir, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(monitor, "run_done_scan", lambda mgr=None: called.append("done"))
    from dockwright.__main__ import main as dispatcher_main
    monkeypatch.setattr(sys, "argv", ["dockwright", "monitor", "done"])
    dispatcher_main()
    assert called == ["done"]


def test_main_module_dispatches_monitor_questions(fresh_orchestrator_dir, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(monitor, "run_questions_scan", lambda mgr=None: called.append("questions"))
    from dockwright.__main__ import main as dispatcher_main
    monkeypatch.setattr(sys, "argv", ["dockwright", "monitor", "questions"])
    dispatcher_main()
    assert called == ["questions"]


def _set_limited_flag(name="test-mgr"):
    (paths.ROOT / f".manager-limited-{name}").touch()


def test_done_scan_holds_while_manager_limited(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_done("test-mgr", "wkr-a", summary="finished")
    _set_limited_flag()
    monitor.run_done_scan()
    assert capsys.readouterr().out == ""
    assert not (tmp_path / ".seen-done-test-mgr").exists(), "held events must not be marked seen"

    (paths.ROOT / ".manager-limited-test-mgr").unlink()
    monitor.run_done_scan()
    out = capsys.readouterr().out
    assert "wkr-a" in out and "finished" in out


def test_questions_scan_holds_while_manager_limited(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_question("test-mgr", "q1", "wkr-a", question="blocked on creds")
    _set_limited_flag()
    monitor.run_questions_scan()
    assert capsys.readouterr().out == ""

    (paths.ROOT / ".manager-limited-test-mgr").unlink()
    monitor.run_questions_scan()
    out = capsys.readouterr().out
    assert "wkr-a" in out and "blocked on creds" in out


def test_turn_ends_scan_holds_while_manager_limited(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_aged_turn_end("test-mgr", "wkr-a")
    _write_worker_record("wkr-a")
    _set_limited_flag()
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out == ""

    (paths.ROOT / ".manager-limited-test-mgr").unlink()
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-a" in capsys.readouterr().out


def test_stale_limited_flag_is_ignored_and_cleared(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_done("test-mgr", "wkr-a", summary="finished")
    flag = paths.ROOT / ".manager-limited-test-mgr"
    flag.touch()
    old = time.time() - 700
    os.utime(flag, (old, old))
    monitor.run_done_scan()
    out = capsys.readouterr().out
    assert "wkr-a" in out and "finished" in out, "a stale flag must not hold events"
    assert not flag.exists(), "stale flag is best-effort cleared"


def test_monitor_turn_ends_emits_when_done_is_outside_the_lookback(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    entry = _write_aged_turn_end("test-mgr", "wkr-1", age_sec=300)
    _write_worker_record("wkr-1")
    turn_end_ts = time.time() - 300
    done_dir = paths.DONE / "test-mgr"
    done_dir.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(done_dir / "wkr-1-taskA.json",
                            {"claude_sid": "wkr-1", "completed_at": turn_end_ts - 1800})
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-1" in capsys.readouterr().out


def test_monitor_turn_ends_done_within_lookback_still_suppresses(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_aged_turn_end("test-mgr", "wkr-1", age_sec=300)
    _write_worker_record("wkr-1")
    turn_end_ts = time.time() - 300
    done_dir = paths.DONE / "test-mgr"
    done_dir.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(done_dir / "wkr-1-d1.json",
                            {"claude_sid": "wkr-1", "completed_at": turn_end_ts - 240})
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""


def _write_done_at(manager_name: str, sid: str, completed_at: float, tag: str = "d1"):
    done_dir = paths.DONE / manager_name
    done_dir.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(done_dir / f"{sid}-{tag}.json",
                            {"claude_sid": sid, "completed_at": completed_at})


def _write_question_at(manager_name: str, sid: str, asked_at: float, qid: str = "q1"):
    target_dir = paths.QUESTIONS / manager_name
    target_dir.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(target_dir / f"{qid}.json", {
        "question_id": qid, "worker_sid": sid, "worker_name": sid,
        "parent_manager_name": manager_name, "question": "X or Y?",
        "asked_at": asked_at,
    })


def _classify(entry, manager_name="test-mgr", now=None):
    payload = json.loads(entry.read_text())
    return monitor.classify_turn_end(payload, entry, manager_name, None,
                                     now if now is not None else time.time())


def test_retasked_worker_silent_finish_is_not_masked_by_the_previous_done(fresh_orchestrator_dir):
    entry = _write_aged_turn_end("test-mgr", "wkr-1", age_sec=300)
    ts = time.time() - 300
    done_ts = ts - 244.71
    _write_done_at("test-mgr", "wkr-1", done_ts)
    _write_worker_record("wkr-1", tasked_at=done_ts + 90.80,
                         processing_since=done_ts + 90.86)
    assert _classify(entry) == monitor.TURN_END_EMIT


def test_retask_typed_by_a_human_also_unmasks_without_tasked_at(fresh_orchestrator_dir):
    entry = _write_aged_turn_end("test-mgr", "wkr-2", age_sec=300)
    ts = time.time() - 300
    done_ts = ts - 240
    _write_done_at("test-mgr", "wkr-2", done_ts)
    _write_worker_record("wkr-2", processing_since=done_ts + 90)
    assert _classify(entry) == monitor.TURN_END_EMIT


def test_post_done_cleanup_still_suppresses_when_the_turn_predates_the_done(fresh_orchestrator_dir):
    entry = _write_aged_turn_end("test-mgr", "wkr-3", age_sec=300)
    ts = time.time() - 300
    done_ts = ts - 240
    _write_done_at("test-mgr", "wkr-3", done_ts)
    _write_worker_record("wkr-3", tasked_at=done_ts - 61,
                         processing_since=done_ts - 60)
    assert _classify(entry) == monitor.TURN_END_SUPPRESS


def test_done_crossing_a_retask_in_flight_stays_suppressed(fresh_orchestrator_dir):
    entry = _write_aged_turn_end("test-mgr", "wkr-4", age_sec=300)
    ts = time.time() - 300
    done_ts = ts - 240
    _write_done_at("test-mgr", "wkr-4", done_ts)
    _write_worker_record("wkr-4", tasked_at=done_ts + 1.5)
    assert _classify(entry) == monitor.TURN_END_SUPPRESS


def test_record_without_episode_stamps_keeps_the_old_suppression(fresh_orchestrator_dir):
    entry = _write_aged_turn_end("test-mgr", "wkr-5", age_sec=300)
    ts = time.time() - 300
    _write_done_at("test-mgr", "wkr-5", ts - 240)
    _write_worker_record("wkr-5")
    assert _classify(entry) == monitor.TURN_END_SUPPRESS


def test_question_older_than_the_retask_no_longer_masks_the_finish(fresh_orchestrator_dir):
    entry = _write_aged_turn_end("test-mgr", "wkr-6", age_sec=300)
    ts = time.time() - 300
    _write_question_at("test-mgr", "wkr-6", asked_at=ts - 400)
    _write_worker_record("wkr-6", tasked_at=ts - 100)
    assert _classify(entry) == monitor.TURN_END_EMIT


def test_question_newer_than_the_retask_still_suppresses(fresh_orchestrator_dir):
    entry = _write_aged_turn_end("test-mgr", "wkr-7", age_sec=300)
    ts = time.time() - 300
    _write_question_at("test-mgr", "wkr-7", asked_at=ts - 50)
    _write_worker_record("wkr-7", tasked_at=ts - 400, processing_since=ts - 400)
    assert _classify(entry) == monitor.TURN_END_SUPPRESS


def test_absent_record_with_a_fresh_done_suppresses_rather_than_paging_exited(fresh_orchestrator_dir):
    entry = _write_aged_turn_end("test-mgr", "wkr-8", age_sec=300)
    ts = time.time() - 300
    _write_done_at("test-mgr", "wkr-8", ts - 10)
    assert not (paths.ACTIVE / "wkr-8.json").exists()
    assert _classify(entry) == monitor.TURN_END_SUPPRESS


def test_the_done_turns_own_turn_end_does_not_page_after_a_retask(fresh_orchestrator_dir):
    entry = _write_aged_turn_end("test-mgr", "wkr-9", age_sec=300)
    ts = time.time() - 300
    done_ts = ts - 4
    _write_done_at("test-mgr", "wkr-9", done_ts)
    _write_worker_record("wkr-9", worker_state="processing",
                         tasked_at=done_ts + 90, processing_since=done_ts + 90)
    assert _classify(entry) == monitor.TURN_END_SUPPRESS


def test_a_turn_end_29_minutes_old_is_still_pending(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_SILENT_FINISH_GRACE_SEC", raising=False)
    monkeypatch.setenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", "0")
    entry = _write_aged_turn_end("test-mgr", "wkr-g1", age_sec=29 * 60)
    _write_worker_record("wkr-g1")
    assert _classify(entry) == monitor.TURN_END_PENDING


def test_a_turn_end_31_minutes_old_pages(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_SILENT_FINISH_GRACE_SEC", raising=False)
    entry = _write_aged_turn_end("test-mgr", "wkr-g2", age_sec=31 * 60)
    _write_worker_record("wkr-g2")
    assert _classify(entry) == monitor.TURN_END_EMIT


def test_a_background_notification_turn_does_not_unmute_the_question(fresh_orchestrator_dir):
    entry = _write_aged_turn_end("test-mgr", "wkr-q1", age_sec=2000)
    ts = time.time() - 2000
    _write_question_at("test-mgr", "wkr-q1", asked_at=ts - 1500)
    _write_worker_record("wkr-q1", tasked_at=ts - 3000, input_at=ts - 3000,
                         processing_since=ts - 60)
    assert _classify(entry) == monitor.TURN_END_SUPPRESS


def test_his_typed_line_after_the_question_unmutes_it(fresh_orchestrator_dir):
    entry = _write_aged_turn_end("test-mgr", "wkr-q2", age_sec=2000)
    ts = time.time() - 2000
    _write_question_at("test-mgr", "wkr-q2", asked_at=ts - 1500)
    _write_worker_record("wkr-q2", tasked_at=ts - 3000, input_at=ts - 600,
                         processing_since=ts - 600)
    assert _classify(entry) == monitor.TURN_END_EMIT


def test_a_manager_message_after_the_question_unmutes_it_without_input_at(fresh_orchestrator_dir):
    entry = _write_aged_turn_end("test-mgr", "wkr-q3", age_sec=2000)
    ts = time.time() - 2000
    _write_question_at("test-mgr", "wkr-q3", asked_at=ts - 1500)
    _write_worker_record("wkr-q3", tasked_at=ts - 600, processing_since=ts - 600)
    assert _classify(entry) == monitor.TURN_END_EMIT


def test_classify_turn_end_pending_when_timestamp_unresolvable(fresh_orchestrator_dir):
    from pathlib import Path as _P
    verdict = monitor.classify_turn_end({}, _P("/nonexistent/wkr-x-123.json"),
                                        "test-mgr", None, time.time())
    assert verdict == monitor.TURN_END_PENDING


def _make_subagent_tree(home, sid, *, log_age_sec, agent_write_age_sec):
    project_dir = home / ".claude" / "projects" / "-Users-test"
    project_dir.mkdir(parents=True, exist_ok=True)
    log = project_dir / f"{sid}.jsonl"
    log.write_text("")
    now = time.time()
    os.utime(log, (now - log_age_sec, now - log_age_sec))
    if agent_write_age_sec is not None:
        subagents = project_dir / sid / "subagents"
        subagents.mkdir(parents=True, exist_ok=True)
        agent = subagents / "agent-aaa.jsonl"
        agent.write_text("{}")
        os.utime(agent, (now - agent_write_age_sec, now - agent_write_age_sec))
    return log


def test_monitor_turn_ends_holds_while_subagent_grows(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    _write_aged_turn_end("test-mgr", "wkr-del", age_sec=300)
    _write_worker_record("wkr-del")
    _make_subagent_tree(fresh_orchestrator_dir, "wkr-del", log_age_sec=300, agent_write_age_sec=10)
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""
    assert not (tmp_path / ".seen-turn-ends-test-mgr").exists(), \
        "held turn-ends must not be marked seen"


def test_monitor_turn_ends_emits_once_subagent_quiet_past_grace(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    _write_aged_turn_end("test-mgr", "wkr-dead", age_sec=1200)
    _write_worker_record("wkr-dead")
    _make_subagent_tree(fresh_orchestrator_dir, "wkr-dead", log_age_sec=1200, agent_write_age_sec=1000)
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-dead" in capsys.readouterr().out
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""


def test_monitor_turn_ends_emits_when_subagent_writes_predate_turn_end(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    _write_aged_turn_end("test-mgr", "wkr-old", age_sec=180)
    _write_worker_record("wkr-old")
    _make_subagent_tree(fresh_orchestrator_dir, "wkr-old", log_age_sec=180, agent_write_age_sec=240)
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-old" in capsys.readouterr().out


def test_delegation_hold_true_at_exact_turn_end_tie(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    ts = time.time() - 10
    project_dir = fresh_orchestrator_dir / ".claude" / "projects" / "-Users-test"
    project_dir.mkdir(parents=True, exist_ok=True)
    log = project_dir / "wkr-tie.jsonl"
    log.write_text("")
    os.utime(log, (ts - 300, ts - 300))
    subagents = project_dir / "wkr-tie" / "subagents"
    subagents.mkdir(parents=True, exist_ok=True)
    agent = subagents / "agent-aaa.jsonl"
    agent.write_text("{}")
    os.utime(agent, (ts, ts))
    record = {"claude_sid": "wkr-tie", "runtime": "claude"}
    assert monitor._delegation_hold(record, "wkr-tie", ts, time.time()) is True
    os.utime(agent, (ts - 1, ts - 1))
    assert monitor._delegation_hold(record, "wkr-tie", ts, time.time()) is False


def test_monitor_turn_ends_done_beats_delegation_hold(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    entry = _write_aged_turn_end("test-mgr", "wkr-dd", age_sec=300)
    _write_worker_record("wkr-dd")
    _write_done("test-mgr", "wkr-dd")
    _make_subagent_tree(fresh_orchestrator_dir, "wkr-dd", log_age_sec=300, agent_write_age_sec=10)
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""
    seen_path = tmp_path / ".seen-turn-ends-test-mgr"
    assert seen_path.exists(), "SUPPRESS must consume the turn-end"
    assert str(entry) in seen_path.read_text()


def test_monitor_turn_ends_codex_worker_skips_subagent_check(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    _write_aged_turn_end("test-mgr", "wkr-cdx", age_sec=300)
    record = {"claude_sid": "wkr-cdx", "agent": "worker", "name": "wkr-cdx",
              "window_id": "", "pid": os.getpid(), "state": "idle",
              "parent_manager_name": "test-mgr", "runtime": "codex"}
    state.write_json_atomic(paths.ACTIVE / "wkr-cdx.json", record)
    _make_subagent_tree(fresh_orchestrator_dir, "wkr-cdx", log_age_sec=300, agent_write_age_sec=10)
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-cdx" in capsys.readouterr().out


def test_delegation_hold_survives_slow_subagent_write_gap(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    _write_aged_turn_end("test-mgr", "wkr-slow", age_sec=600)
    _write_worker_record("wkr-slow")
    _make_subagent_tree(fresh_orchestrator_dir, "wkr-slow", log_age_sec=600, agent_write_age_sec=300)
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""
    assert not (tmp_path / ".seen-turn-ends-test-mgr").exists()


def test_delegation_hold_expires_past_episode_grace(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    _write_aged_turn_end("test-mgr", "wkr-deadsa", age_sec=1200)
    _write_worker_record("wkr-deadsa")
    _make_subagent_tree(fresh_orchestrator_dir, "wkr-deadsa", log_age_sec=1200, agent_write_age_sec=1000)
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-deadsa" in capsys.readouterr().out


def test_episode_grace_env_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_EPISODE_GRACE_SEC", "1800")
    monkeypatch.delenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", raising=False)
    assert monitor._episode_grace_sec() == 1800


def test_episode_grace_clamps_to_base_grace(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", "1200")
    monkeypatch.delenv("CLAUDE_ORCH_EPISODE_GRACE_SEC", raising=False)
    assert monitor._episode_grace_sec() == 1200


def test_episode_grace_default(monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_EPISODE_GRACE_SEC", raising=False)
    monkeypatch.delenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", raising=False)
    assert monitor._episode_grace_sec() == 900


def test_monitor_turn_ends_live_reread_overrides_stale_marker(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(tmp_path))
    sid = "wkr-stale"
    _write_aged_turn_end("test-mgr", sid, name="beta", summary="STALE mid-read narration")
    _write_worker_record(sid, name="beta")
    _write_transcript(tmp_path, sid, [
        _assistant_event("STALE mid-read narration", ts="2026-06-18T08:11:00Z"),
        _assistant_event("FRESH final: Paused, here is where things stand", ts="2026-06-18T08:11:23Z"),
    ])
    monitor.run_turn_ends_scan()
    out = capsys.readouterr().out.strip()
    assert "FRESH final: Paused" in out
    assert "STALE mid-read narration" not in out


def test_monitor_turn_ends_resumed_worker_processing_suppresses(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(tmp_path))
    sid = "wkr-resumed"
    _write_aged_turn_end("test-mgr", sid, name="delta", summary="old turn final")
    _write_worker_record(sid, name="delta", worker_state="processing")
    _write_transcript(tmp_path, sid, [_assistant_event("NEW turn narration in flight")])
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""


def test_monitor_turn_ends_live_reread_picks_final_text_over_midturn_narration(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(tmp_path))
    sid = "wkr-mid"
    _write_aged_turn_end("test-mgr", sid, name="eps", summary="ignored marker")
    _write_worker_record(sid, name="eps")
    _write_transcript(tmp_path, sid, [
        _assistant_event("midturn: let me read the spec", ts="2026-06-18T08:11:00Z",
                         stop_reason="tool_use", tools=["Read"]),
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
        _assistant_event("FINAL end_turn: Paused", ts="2026-06-18T08:11:23Z"),
        _assistant_event("", ts="2026-06-18T08:11:24Z"),
    ])
    monitor.run_turn_ends_scan()
    out = capsys.readouterr().out.strip()
    assert "FINAL end_turn: Paused" in out
    assert "midturn" not in out


def test_monitor_turn_ends_falls_back_to_marker_when_no_transcript(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_aged_turn_end("test-mgr", "wkr-not", name="gamma", summary="marker fallback text")
    _write_worker_record("wkr-not", name="gamma")
    monitor.run_turn_ends_scan()
    out = capsys.readouterr().out
    assert "FINISHED_SILENTLY gamma → " in out and out.rstrip().endswith(" — marker fallback text")


def test_monitor_turn_ends_falls_back_to_marker_when_transcript_has_no_assistant_text(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(tmp_path))
    sid = "wkr-empty"
    _write_aged_turn_end("test-mgr", sid, name="zeta", summary="marker fallback text")
    _write_worker_record(sid, name="zeta")
    _write_transcript(tmp_path, sid, [{"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}])
    monitor.run_turn_ends_scan()
    out = capsys.readouterr().out
    assert "FINISHED_SILENTLY zeta → " in out and out.rstrip().endswith(" — marker fallback text")


def test_monitor_turn_ends_long_summary_not_cut_at_160(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(tmp_path))
    sid = "wkr-long"
    long_text = "FINAL " + ("x" * 300)
    _write_aged_turn_end("test-mgr", sid, name="eta", summary="short marker")
    _write_worker_record(sid, name="eta")
    _write_transcript(tmp_path, sid, [_assistant_event(long_text)])
    monitor.run_turn_ends_scan()
    out = capsys.readouterr().out
    assert long_text in out
    assert "…" not in out


def test_resolve_live_summary_crashproof(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert monitor._resolve_live_summary({}) is None
    assert monitor._resolve_live_summary({"sid": "nonexistent-sid"}) is None


def _write_outbox_entry(manager_name: str, line: str, buffered_at: float | None = None,
                        filename: str | None = None, raw: bytes | None = None, source: str = "gardener"):
    outbox = paths.notify_outbox_dir_for(manager_name)
    outbox.mkdir(parents=True, exist_ok=True)
    ts = buffered_at if buffered_at is not None else time.time()
    fname = filename or f"{int(ts * 1000)}-{os.getpid()}-{len(list(outbox.glob('*.json')))}.json"
    target = outbox / fname
    if raw is not None:
        target.write_bytes(raw)
    else:
        state.write_json_atomic(target, {"line": line, "kind": "gardener", "source": source,
                                         "buffered_at": ts})
    return target


def test_notify_outbox_dir_derives_from_root_at_call_time(fresh_orchestrator_dir):
    assert str(paths.notify_outbox_dir_for("test-mgr")).startswith(str(fresh_orchestrator_dir))
    assert paths.notify_outbox_dir_for("a/b").name == "a_b"


def test_done_scan_drains_outbox_when_printing(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_done("test-mgr", "wkr-a", summary="finished")
    _write_outbox_entry("test-mgr", "gardener digest ready: run-old-worker (clean)")
    monitor.run_done_scan()
    out = capsys.readouterr().out
    assert ('DONE wkr-a → verify per § Verify before relay, then relay; a cut summary: get_worker_summary("wkr-a"), '
            "never cat the done file; harvest it before any send_manager_to_worker to it — finished") in out
    assert "gardener digest ready: run-old-worker (clean)" in out
    assert out.index("DONE wkr-a") < out.index("gardener digest ready")
    assert list(paths.notify_outbox_dir_for("test-mgr").glob("*.json")) == []


def test_done_scan_leaves_outbox_when_silent(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    entry = _write_outbox_entry("test-mgr", "gardener digest ready: run-old-worker (clean)")
    monitor.run_done_scan()
    assert capsys.readouterr().out == ""
    assert entry.exists()


def test_questions_scan_drains_outbox_when_printing(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_question("test-mgr", "q1", "wkr-b", question="help me")
    _write_outbox_entry("test-mgr", "gardener digest ready: run-old-worker (clean)")
    monitor.run_questions_scan()
    out = capsys.readouterr().out
    assert out.startswith("QUESTION wkr-b → ") and " — help me\n" in out
    assert "gardener digest ready: run-old-worker (clean)" in out
    assert out.index("QUESTION wkr-b") < out.index("gardener digest ready")


def test_drain_scoped_to_own_manager(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_done("test-mgr", "wkr-a")
    peer_entry = _write_outbox_entry("peer-mgr", "gardener digest ready: run-peer-worker (clean)")
    monitor.run_done_scan()
    assert "peer-worker" not in capsys.readouterr().out
    assert peer_entry.exists()


def test_drain_corrupt_entry_is_not_a_poison_pill(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_done("test-mgr", "wkr-a")
    bad = _write_outbox_entry("test-mgr", "", filename="0000000000000-0-0.json", raw=b"{{{not json")
    good = _write_outbox_entry("test-mgr", "gardener digest ready: run-good-worker (clean)")
    monitor.run_done_scan()
    captured = capsys.readouterr()
    assert "gardener digest ready: run-good-worker (clean)" in captured.out
    assert not bad.exists()
    assert not good.exists()


def test_drain_missing_file_race_is_silent(fresh_orchestrator_dir, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_done("test-mgr", "wkr-a")
    entry = _write_outbox_entry("test-mgr", "gardener digest ready: run-gone (clean)")
    real_read_text = type(entry).read_text

    def racing_read_text(self, *a, **kw):
        if self.name == entry.name:
            self.unlink(missing_ok=True)
            raise FileNotFoundError(str(self))
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(type(entry), "read_text", racing_read_text)
    monitor.run_done_scan()
    captured = capsys.readouterr()
    assert "gone" not in captured.out
    assert "outbox drain failed" not in captured.err


def test_concurrent_drainers_lose_nothing(fresh_orchestrator_dir, capsys):
    import threading
    lines = [f"gardener digest ready: run-worker-{i} (clean)" for i in range(20)]
    for i, line in enumerate(lines):
        _write_outbox_entry("test-mgr", line, filename=f"{1000 + i}-0-{i}.json")
    errors = []

    def drain():
        try:
            monitor._drain_notify_outbox("test-mgr")
        except Exception as e:  # pragma: no cover - the assertion is the point
            errors.append(e)

    threads = [threading.Thread(target=drain) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    out = capsys.readouterr().out
    assert errors == []
    for line in lines:
        assert line in out
    assert list(paths.notify_outbox_dir_for("test-mgr").glob("*.json")) == []


def _write_turn_end_at(manager_name: str, sid: str, ts: float):
    target_dir = paths.TURN_ENDS / manager_name
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{sid}-{int(ts * 1000)}.json"
    state.write_json_atomic(path, {"sid": sid, "name": sid, "completed_at": ts,
                                   "last_summary": "did things"})
    return path


def _write_fs_worker_record(sid: str, **overrides):
    record = {"claude_sid": sid, "agent": "worker", "name": sid,
              "state": "idle", "pid": os.getpid()}
    record.update(overrides)
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", record)


def _seed_ladder(manager_name: str, sid: str, last_emit: float, level: int,
                 exited: bool = False):
    state.write_json_atomic(monitor._fs_ladder_path(manager_name),
                            {sid: {"last_emit": last_emit, "level": level,
                                   "exited": exited}})


@pytest.fixture
def fs_scan(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file",
                        lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setattr(monitor, "_resolve_live_summary", lambda payload: None)
    return fresh_orchestrator_dir


def test_fs_first_emit_is_immediate_and_records_ladder(fs_scan, capsys):
    _write_fs_worker_record("wkr-1")
    _write_turn_end_at("test-mgr", "wkr-1", time.time() - 300)
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-1" in capsys.readouterr().out
    ladder = json.loads(monitor._fs_ladder_path("test-mgr").read_text())
    assert ladder["wkr-1"]["level"] == 1


def test_fs_repeat_within_rung_is_held_not_seen(fs_scan, capsys):
    _write_fs_worker_record("wkr-1")
    now = time.time()
    _seed_ladder("test-mgr", "wkr-1", last_emit=now - 300, level=1)
    entry = _write_turn_end_at("test-mgr", "wkr-1", now - 300)
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out == ""
    _seed_ladder("test-mgr", "wkr-1", last_emit=now - 1000, level=1)
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-1" in capsys.readouterr().out
    assert entry.exists()


def test_fs_rung_emission_advances_level(fs_scan, capsys):
    _write_fs_worker_record("wkr-1")
    now = time.time()
    _seed_ladder("test-mgr", "wkr-1", last_emit=now - 1000, level=1)
    _write_turn_end_at("test-mgr", "wkr-1", now - 300)
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-1" in capsys.readouterr().out
    ladder = json.loads(monitor._fs_ladder_path("test-mgr").read_text())
    assert ladder["wkr-1"]["level"] == 2


def test_fs_rung_is_capped_at_four_hours(fs_scan, capsys):
    _write_fs_worker_record("wkr-1")
    now = time.time()
    _seed_ladder("test-mgr", "wkr-1", last_emit=now - (4 * 3600 + 120), level=10)
    _write_turn_end_at("test-mgr", "wkr-1", now - 300)
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-1" in capsys.readouterr().out


def test_fs_reset_on_processing_since(fs_scan, capsys):
    now = time.time()
    _write_fs_worker_record("wkr-1", processing_since=now - 200)
    _seed_ladder("test-mgr", "wkr-1", last_emit=now - 400, level=5)
    _write_turn_end_at("test-mgr", "wkr-1", now - 150)
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-1" in capsys.readouterr().out
    ladder = json.loads(monitor._fs_ladder_path("test-mgr").read_text())
    assert ladder["wkr-1"]["level"] == 1


def test_fs_gate_reset_on_done_after_last_emit(fresh_orchestrator_dir):
    now = time.time()
    _write_done("test-mgr", "wkr-1")
    ladder = {"wkr-1": {"last_emit": now - 3600, "level": 3, "exited": False}}
    assert monitor._fs_ladder_gate(ladder, "wkr-1", monitor.TURN_END_EMIT,
                                   "test-mgr", now) == monitor.FS_EMIT_RESET


def test_fs_gate_exited_transition_resets_once(fresh_orchestrator_dir):
    now = time.time()
    ladder = {"wkr-1": {"last_emit": now - 60, "level": 2, "exited": False}}
    assert monitor._fs_ladder_gate(ladder, "wkr-1", monitor.TURN_END_EMIT_EXITED,
                                   "test-mgr", now) == monitor.FS_EMIT_RESET
    ladder["wkr-1"]["exited"] = True
    assert monitor._fs_ladder_gate(ladder, "wkr-1", monitor.TURN_END_EMIT_EXITED,
                                   "test-mgr", now) == monitor.FS_HOLD


def test_fs_record_reset_clears_sticky_exited_flag(fresh_orchestrator_dir):
    now = time.time()
    sid = "wkr-1"
    ladder = {sid: {"last_emit": now - 400, "level": 2, "exited": True}}
    _write_fs_worker_record(sid, processing_since=now - 200)
    assert monitor._fs_ladder_gate(ladder, sid, monitor.TURN_END_EMIT,
                                   "test-mgr", now) == monitor.FS_EMIT_RESET
    monitor._fs_ladder_record(ladder, sid, monitor.TURN_END_EMIT,
                              monitor.FS_EMIT_RESET, now)
    assert ladder[sid]["exited"] is False
    (paths.ACTIVE / f"{sid}.json").unlink()
    assert monitor._fs_ladder_gate(ladder, sid, monitor.TURN_END_EMIT_EXITED,
                                   "test-mgr", now) == monitor.FS_EMIT_RESET


def test_fs_ladder_entries_pruned_after_ttl(fs_scan, capsys):
    now = time.time()
    _seed_ladder("test-mgr", "wkr-old", last_emit=now - monitor.FS_LADDER_ENTRY_TTL_SEC - 60,
                 level=2)
    _write_fs_worker_record("wkr-1")
    _write_turn_end_at("test-mgr", "wkr-1", now - 300)
    monitor.run_turn_ends_scan()
    capsys.readouterr()
    ladder = json.loads(monitor._fs_ladder_path("test-mgr").read_text())
    assert "wkr-old" not in ladder
    assert "wkr-1" in ladder


def test_fs_ladder_path_sanitizes_manager_name(fresh_orchestrator_dir):
    assert monitor._fs_ladder_path("a/b").name == ".fs-emitted-a_b.json"


def test_fs_ladder_survives_limited_window(fs_scan, capsys):
    now = time.time()
    _write_fs_worker_record("wkr-1")
    _seed_ladder("test-mgr", "wkr-1", last_emit=now - 1000, level=1)
    _write_turn_end_at("test-mgr", "wkr-1", now - 300)
    flag = paths.ROOT / ".manager-limited-test-mgr"
    flag.touch()
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out == ""
    flag.unlink()
    monitor.run_turn_ends_scan()
    out = capsys.readouterr().out
    assert out.count("FINISHED_SILENTLY wkr-1") == 1


def test_fs_crash_before_ladder_write_duplicates_not_loses(fs_scan, capsys, monkeypatch):
    _write_fs_worker_record("wkr-1")
    now = time.time()
    _write_turn_end_at("test-mgr", "wkr-1", now - 1300)
    real_write = state.write_json_atomic

    def failing_ladder_write(path, data):
        if ".fs-emitted-" in str(path):
            raise OSError("simulated crash")
        return real_write(path, data)

    monkeypatch.setattr(monitor.state, "write_json_atomic", failing_ladder_write)
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-1" in capsys.readouterr().out
    monkeypatch.setattr(monitor.state, "write_json_atomic", real_write)
    _write_turn_end_at("test-mgr", "wkr-1", now - 250)
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-1" in capsys.readouterr().out


def test_fs_emit_drains_outbox(fs_scan, capsys):
    _write_fs_worker_record("wkr-1")
    _write_turn_end_at("test-mgr", "wkr-1", time.time() - 300)
    _write_outbox_entry("test-mgr", "gardener digest ready: run-rider (clean)")
    monitor.run_turn_ends_scan()
    out = capsys.readouterr().out
    assert "FINISHED_SILENTLY wkr-1" in out
    assert "gardener digest ready: run-rider (clean)" in out
    assert out.index("FINISHED_SILENTLY") < out.index("gardener digest ready")


def test_load_seen_normalizes_legacy_root_prefix(tmp_path, monkeypatch):
    new_root = tmp_path / ".claude" / "dockwright"
    legacy_root = tmp_path / ".claude" / "orchestrator"
    new_root.mkdir(parents=True)
    monkeypatch.setattr(monitor.paths, "ROOT", new_root)
    monkeypatch.setattr(monitor.config, "legacy_state_root", lambda: legacy_root)
    seen_file = new_root / ".seen-done-mgr"
    seen_file.write_text(
        f"{legacy_root}/done/mgr/abc-1.json\n"
        f"{new_root}/done/mgr/def-2.json\n"
    )
    seen = monitor._load_seen(seen_file)
    assert str(new_root / "done/mgr/abc-1.json") in seen
    assert str(new_root / "done/mgr/def-2.json") in seen


def test_monitor_stale_honors_positional_manager_name(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "bogus-win")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(monitor.subprocess, "run", fake_run)
    monitor.main(["stale", "test-mgr"])
    assert len(calls) == 1
    assert calls[0][3:] == ["--manager", "test-mgr"]


def test_monitor_done_positional_scopes_scan(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "bogus-win")
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_done("test-mgr", "wkr-by-name", summary="done-by-name")
    monitor.main(["done", "test-mgr"])
    out = capsys.readouterr().out
    assert "wkr-by-name" in out


def test_monitor_positional_unknown_name_exits_2(fresh_orchestrator_dir, capsys):
    with pytest.raises(SystemExit) as ei:
        monitor.main(["stale", "no-such-mgr"])
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "no-such-mgr" in err
    assert "test-mgr" in err


def test_monitor_extra_args_exit_2(fresh_orchestrator_dir, capsys):
    with pytest.raises(SystemExit) as ei:
        monitor.main(["done", "test-mgr", "extra"])
    assert ei.value.code == 2


def test_monitor_unresolvable_identity_exits_2(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "bogus-win")
    from dockwright import identity
    monkeypatch.setattr(identity, "_resolve_via_ppid_walk", lambda records: None)
    with pytest.raises(SystemExit) as ei:
        monitor.main(["stale"])
    assert ei.value.code == 2


def test_monitor_positional_ambiguous_name_exits_2(fresh_orchestrator_dir, capsys):
    state.write_json_atomic(paths.ACTIVE / "mgr-test-2.json", {
        "claude_sid": "mgr-test-2",
        "agent": "manager",
        "name": "test-mgr",
        "window_id": "test-win-2",
        "pid": os.getpid(),
        "domain": "general",
    })
    with pytest.raises(SystemExit) as ei:
        monitor.main(["stale", "test-mgr"])
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "ambiguous" in err
    assert "2" in err


def test_monitor_unknown_subcommand_reports_before_name_lookup(fresh_orchestrator_dir, capsys):
    with pytest.raises(SystemExit) as ei:
        monitor.main(["bogus-sub", "no-such-mgr"])
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "Unknown monitor subcommand" in err
    assert "no active manager record" not in err


def test_the_dispatch_table_covers_every_canonical_lane():
    from dockwright import lane_io
    assert set(monitor._SCANS) == set(lane_io.LANES)
    assert set(monitor._SCANS) == set(monitor._MONITOR_SUBCOMMANDS)


def test_every_dispatch_target_actually_exists():
    for lane, target in monitor._SCANS.items():
        assert callable(getattr(monitor, target, None)), (lane, target)


def test_delegation_hold_true_for_a_worker_with_a_live_child(fresh_orchestrator_dir):
    state.write_json_atomic(paths.ACTIVE / "c1.json", {
        "claude_sid": "c1", "agent": "worker", "kind": "subworker", "name": "alpha-review",
        "parent_name": "alpha", "pid": os.getpid()})
    record = {"claude_sid": "w1", "name": "alpha", "runtime": "claude"}
    assert monitor._delegation_hold(record, "w1", time.time() - 10, time.time()) is True


_SNAPSHOT_SHELL = ("/bin/zsh -c source /Users/x/.claude/shell-snapshots/"
                   "snapshot-zsh-1.sh 2>/dev/null || true && sleep 600")
_GATE_SHELL = ("/bin/zsh -c source /Users/x/.claude/shell-snapshots/"
               "snapshot-zsh-1.sh 2>/dev/null || true && "
               "/Users/x/.claude/scripts/plannotator-gate /tmp/review/ --gate --json")


def _proc_index(pid: int, children: list[str]):
    return {"command_by_pid": {pid: "claude --settings /tmp/worker.json"},
            "child_commands": {pid: list(children)}}


def test_turn_end_holds_while_a_background_shell_is_live(fresh_orchestrator_dir):
    entry = _write_aged_turn_end("test-mgr", "wkr-bg-1", age_sec=300)
    _write_worker_record("wkr-bg-1")
    pid = json.loads((paths.ACTIVE / "wkr-bg-1.json").read_text())["pid"]
    verdict = monitor.classify_turn_end(
        json.loads(entry.read_text()), entry, "test-mgr", None, time.time(),
        process_index=lambda: _proc_index(pid, [_SNAPSHOT_SHELL]))
    assert verdict == monitor.TURN_END_PENDING


def test_turn_end_holds_while_a_plannotator_gate_wait_is_live(fresh_orchestrator_dir):
    entry = _write_aged_turn_end("test-mgr", "wkr-bg-2", age_sec=300)
    _write_worker_record("wkr-bg-2")
    pid = json.loads((paths.ACTIVE / "wkr-bg-2.json").read_text())["pid"]
    verdict = monitor.classify_turn_end(
        json.loads(entry.read_text()), entry, "test-mgr", None, time.time(),
        process_index=lambda: _proc_index(pid, [_GATE_SHELL]))
    assert verdict == monitor.TURN_END_PENDING


def test_turn_end_emits_when_no_background_shell_is_live(fresh_orchestrator_dir):
    entry = _write_aged_turn_end("test-mgr", "wkr-bg-3", age_sec=300)
    _write_worker_record("wkr-bg-3")
    pid = json.loads((paths.ACTIVE / "wkr-bg-3.json").read_text())["pid"]
    verdict = monitor.classify_turn_end(
        json.loads(entry.read_text()), entry, "test-mgr", None, time.time(),
        process_index=lambda: _proc_index(pid, []))
    assert verdict == monitor.TURN_END_EMIT


def test_turn_end_emits_once_the_busy_hold_deadline_passes(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TURN_END_BUSY_SEC", "200")
    entry = _write_aged_turn_end("test-mgr", "wkr-bg-4", age_sec=300)
    _write_worker_record("wkr-bg-4")
    pid = json.loads((paths.ACTIVE / "wkr-bg-4.json").read_text())["pid"]
    verdict = monitor.classify_turn_end(
        json.loads(entry.read_text()), entry, "test-mgr", None, time.time(),
        process_index=lambda: _proc_index(pid, [_GATE_SHELL]))
    assert verdict == monitor.TURN_END_EMIT


def test_turn_end_emits_when_the_process_table_is_unreadable(
        fresh_orchestrator_dir, monkeypatch):
    entry = _write_aged_turn_end("test-mgr", "wkr-bg-5", age_sec=300)
    _write_worker_record("wkr-bg-5")
    monkeypatch.setattr(monitor, "_process_index", lambda: None)
    verdict = monitor.classify_turn_end(
        json.loads(entry.read_text()), entry, "test-mgr", None, time.time())
    assert verdict == monitor.TURN_END_EMIT


def test_held_turn_end_is_rescanned_and_pages_once_the_shell_exits(
        fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_aged_turn_end("test-mgr", "wkr-bg-6", age_sec=300)
    _write_worker_record("wkr-bg-6")
    pid = json.loads((paths.ACTIVE / "wkr-bg-6.json").read_text())["pid"]
    monkeypatch.setattr(monitor, "_process_index",
                        lambda: _proc_index(pid, [_GATE_SHELL]))
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""
    monkeypatch.setattr(monitor, "_process_index", lambda: _proc_index(pid, []))
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-bg-6" in capsys.readouterr().out


def test_one_scan_builds_the_process_index_once_for_many_held_entries(
        fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    pids = []
    for sid in ("wkr-bg-7", "wkr-bg-8", "wkr-bg-9"):
        _write_aged_turn_end("test-mgr", sid, age_sec=300)
        _write_worker_record(sid)
        pids.append(json.loads((paths.ACTIVE / f"{sid}.json").read_text())["pid"])
    index = {"command_by_pid": {p: "claude --settings /tmp/worker.json" for p in pids},
             "child_commands": {p: [_GATE_SHELL] for p in pids}}
    builds = []

    def _counting_index():
        builds.append(1)
        return index

    monkeypatch.setattr(monitor, "_process_index", _counting_index)
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""
    assert len(builds) == 1, f"one scan forked {len(builds)} process tables for 3 entries"


def test_the_stale_scan_runs_the_note_check_after_reconcile(monkeypatch, fresh_orchestrator_dir):
    order = []
    _stale_child_ok(monkeypatch)
    monkeypatch.setattr("dockwright.monitor._manager_limited", lambda name: False)
    monkeypatch.setattr("dockwright.pipeline_reconcile.reconcile", lambda mgr: order.append("reconcile") or [])
    monkeypatch.setattr("dockwright.note_check.run", lambda: order.append("notes") or [])
    monitor.run_stale_scan({"name": "mgr", "sid": "m1"})
    assert order == ["reconcile", "notes"]


def test_a_limited_manager_runs_no_note_check(monkeypatch, fresh_orchestrator_dir):
    order = []
    _stale_child_ok(monkeypatch)
    monkeypatch.setattr("dockwright.monitor._manager_limited", lambda name: True)
    monkeypatch.setattr("dockwright.note_check.run", lambda: order.append("notes") or [])
    monitor.run_stale_scan({"name": "mgr", "sid": "m1"})
    assert order == []


def test_a_failing_note_check_pages_once_per_window(monkeypatch, fresh_orchestrator_dir):
    lines = []
    _stale_child_ok(monkeypatch)
    monkeypatch.setattr("dockwright.monitor._manager_limited", lambda name: False)
    monkeypatch.setattr("dockwright.pipeline_reconcile.reconcile", lambda mgr: [])

    def boom():
        raise RuntimeError("bad card")

    monkeypatch.setattr("dockwright.note_check.run", boom)
    monkeypatch.setattr("dockwright.monitor.emit", lines.append)
    monitor.run_stale_scan({"name": "mgr", "sid": "m1"})
    monitor.run_stale_scan({"name": "mgr", "sid": "m1"})
    assert lines == ["RECONCILE_ERROR * → dockwright lanes — note-check RuntimeError: bad card"]


def test_a_dead_lane_inside_the_note_check_ends_the_scan(monkeypatch, fresh_orchestrator_dir):
    _stale_child_ok(monkeypatch)
    monkeypatch.setattr("dockwright.monitor._manager_limited", lambda name: False)
    monkeypatch.setattr("dockwright.pipeline_reconcile.reconcile", lambda mgr: [])

    def dead():
        raise LaneDead("gone")

    monkeypatch.setattr("dockwright.note_check.run", dead)
    with pytest.raises(LaneDead):
        monitor.run_stale_scan({"name": "mgr", "sid": "m1"})


def test_a_lane_writes_no_comparison_line_without_the_daemon_dir(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_done("test-mgr", "wkr-a", summary="finished")
    monitor.run_done_scan()
    assert "wkr-a" in capsys.readouterr().out
    assert not paths.DAEMON.exists()


def test_a_json_non_object_is_dropped_as_unparseable(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    (paths.DONE / "test-mgr").mkdir(parents=True, exist_ok=True)
    bad_done = paths.DONE / "test-mgr" / "wkr-z-1.json"
    bad_done.write_text("[1, 2]")
    paths.question_dir_for("test-mgr").mkdir(parents=True, exist_ok=True)
    (paths.question_dir_for("test-mgr") / "q-bad.json").write_text("[]")
    monitor.run_done_scan()
    monitor.run_questions_scan()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "dropped unparseable done event wkr-z-1.json" in captured.err
    assert "dropped unparseable question q-bad.json" in captured.err
    assert str(bad_done) in (tmp_path / ".seen-done-test-mgr").read_text()
    assert "q-bad" in (tmp_path / ".seen-questions-test-mgr").read_text()


def test_classify_reason_names_the_branch_that_decided(fs_scan):
    now = time.time()
    manager_turn = _write_aged_turn_end("test-mgr", "wkr-m", agent="manager")
    exited = _write_aged_turn_end("test-mgr", "wkr-x")
    idle = _write_aged_turn_end("test-mgr", "wkr-i")
    _write_worker_record("wkr-i")
    busy = _write_aged_turn_end("test-mgr", "wkr-p")
    _write_worker_record("wkr-p", worker_state="processing")
    young = _write_aged_turn_end("test-mgr", "wkr-y", age_sec=1)

    def reason(entry, own=None):
        payload = json.loads(entry.read_text())
        return monitor.classify_turn_end_reason(payload, entry, "test-mgr", own, now)

    assert reason(manager_turn) == (monitor.TURN_END_SUPPRESS, "manager")
    assert reason(exited, own="wkr-x") == (monitor.TURN_END_SUPPRESS, "own-sid")
    assert reason(exited) == (monitor.TURN_END_EMIT_EXITED, "exited")
    assert reason(idle) == (monitor.TURN_END_EMIT, "silent-finish")
    assert reason(busy) == (monitor.TURN_END_SUPPRESS, "processing")
    assert reason(young) == (monitor.TURN_END_PENDING, "grace")
    payload = json.loads(idle.read_text())
    assert monitor.classify_turn_end(payload, idle, "test-mgr", None, now) == monitor.TURN_END_EMIT


def _hand_off(sid: str, *, task_key: str = "K", assignment_id: str = "a-old", stamped: bool = True):
    record = {"assignment_id": assignment_id, "claude_sid": sid, "ticket": task_key}
    if stamped:
        record["handed_off_at"] = time.time()
    state.write_json_atomic(paths.assignment_path(sid), record)
    state.append_event(paths.artifact_events_path(task_key),
                       {"type": "handed_off", "token_id": "t1", "assignment_id": assignment_id, "sid": sid})


def _turn_end_reason(entry, now):
    payload = json.loads(entry.read_text())
    return monitor.classify_turn_end_reason(payload, entry, "test-mgr", None, now)


def test_a_handed_off_session_that_exited_is_suppressed_and_not_paged(fs_scan, capsys):
    entry = _write_aged_turn_end("test-mgr", "wkr-handed")
    _hand_off("wkr-handed")

    assert _turn_end_reason(entry, time.time()) == (monitor.TURN_END_SUPPRESS, "handed-off")
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY" not in capsys.readouterr().out


def test_a_handed_off_session_still_alive_is_suppressed(fs_scan):
    entry = _write_aged_turn_end("test-mgr", "wkr-handed-live")
    _write_worker_record("wkr-handed-live")
    _hand_off("wkr-handed-live")

    assert _turn_end_reason(entry, time.time()) == (monitor.TURN_END_SUPPRESS, "handed-off")


def test_a_session_taken_back_after_its_hand_off_still_pages(fs_scan):
    entry = _write_aged_turn_end("test-mgr", "wkr-back")
    _hand_off("wkr-back", stamped=False)
    state.append_event(paths.artifact_events_path("K"),
                       {"type": "holder_restamped", "token_id": "t1", "request_id": "r-back",
                        "old": None, "new": "a-old", "reason": "back"})

    assert _turn_end_reason(entry, time.time()) == (monitor.TURN_END_EMIT_EXITED, "exited")


def test_a_failing_hand_off_check_pages_as_before(fs_scan, capsys, monkeypatch):
    def broken(sid, task_key):
        raise RuntimeError("engine check broke")

    monkeypatch.setattr(mcp_server, "_handed_off", broken)
    entry = _write_aged_turn_end("test-mgr", "wkr-check-broke")
    _hand_off("wkr-check-broke")

    assert _turn_end_reason(entry, time.time()) == (monitor.TURN_END_EMIT_EXITED, "exited")
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.startswith("FINISHED_SILENTLY wkr-check-broke (session exited)")


def test_a_pipeline_node_nobody_handed_off_still_pages(fs_scan):
    entry = _write_aged_turn_end("test-mgr", "wkr-node")
    _write_worker_record("wkr-node")
    state.write_json_atomic(paths.assignment_path("wkr-node"),
                            {"assignment_id": "a-node", "claude_sid": "wkr-node", "ticket": "K"})
    state.append_event(paths.artifact_events_path("K"),
                       {"type": "handed_off", "token_id": "t1", "assignment_id": "a-sibling", "sid": "wkr-sibling"})

    assert _turn_end_reason(entry, time.time()) == (monitor.TURN_END_EMIT, "silent-finish")


def _pages_fixture(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setattr(paths, "LANE_HEALTH", fresh_orchestrator_dir / "lane-health")
    monkeypatch.setattr(paths, "DAEMON", fresh_orchestrator_dir / "daemon")
    monkeypatch.setattr(paths, "DAEMON_STOP", fresh_orchestrator_dir / "daemon-stop")
    monkeypatch.setattr(lane_io, "reader_is_dead", lambda fd=1: False)
    paths.DAEMON.mkdir()
    outbox = paths.notify_outbox_dir_for("test-mgr")
    outbox.mkdir(parents=True)
    return outbox


def _alive(awake, ts=None, step=None, step_awake=None):
    record = {"ts": ts or time.time(), "awake": awake, "pid": 1}
    if step is not None:
        record["step"] = step
    if step_awake is not None:
        record["step_awake"] = step_awake
    state.write_json_atomic(paths.DAEMON / "alive.json", record)


def test_the_pages_scan_prints_the_outbox_in_file_order_once(fresh_orchestrator_dir, monkeypatch, capsys):
    outbox = _pages_fixture(fresh_orchestrator_dir, monkeypatch)
    (outbox / "2-b.json").write_text(json.dumps({"line": "DONE w2 → x", "source": "daemon"}))
    (outbox / "1-a.json").write_text(json.dumps({"line": "QUESTION w1 → y"}))
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 1000.0)
    _alive(990.0)
    monitor.run_pages_scan({"name": "test-mgr", "sid": "mgr-test"})
    assert capsys.readouterr().out == "QUESTION w1 → y\nDONE w2 → x\n"
    assert list(outbox.glob("*.json")) == []
    beat = state.read_json(lane_io.heartbeat_path("test-mgr", "pages"))
    assert beat["lane"] == "pages" and beat["last_emit"] is not None
    monitor.run_pages_scan({"name": "test-mgr", "sid": "mgr-test"})
    assert capsys.readouterr().out == ""


def test_the_pages_scan_holds_a_non_daemon_line_until_a_daemon_line_or_thirty_minutes(fresh_orchestrator_dir,
                                                                                      monkeypatch, capsys):
    outbox = _pages_fixture(fresh_orchestrator_dir, monkeypatch)
    _alive(1000.0)
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 1000.0)
    mgr = {"name": "test-mgr", "sid": "mgr-test"}
    digest = outbox / "1-a.json"
    digest.write_text(json.dumps({"line": "gardener digest ready: run-1", "kind": "gardener"}))
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out == "" and digest.exists()
    (outbox / "2-b.json").write_text(json.dumps({"line": "QUESTION w1 → y", "source": "daemon"}))
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out == "gardener digest ready: run-1\nQUESTION w1 → y\n"
    digest.write_text(json.dumps({"line": "gardener digest ready: run-2", "kind": "gardener"}))
    os.utime(digest, (time.time() - 1801, time.time() - 1801))
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out == "gardener digest ready: run-2\n"


def test_the_pages_scan_holds_the_outbox_while_the_manager_is_limited(fresh_orchestrator_dir, monkeypatch, capsys):
    outbox = _pages_fixture(fresh_orchestrator_dir, monkeypatch)
    (outbox / "1-a.json").write_text(json.dumps({"line": "QUESTION w1 → y"}))
    (paths.ROOT / ".manager-limited-test-mgr").write_text("")
    monitor.run_pages_scan({"name": "test-mgr", "sid": "mgr-test"})
    assert capsys.readouterr().out == "" and (outbox / "1-a.json").exists()
    assert state.read_json(lane_io.heartbeat_path("test-mgr", "pages"))["last_emit"] is None


def test_the_pages_scan_pages_a_silent_daemon_on_a_ladder_by_the_awake_clock(fresh_orchestrator_dir, monkeypatch,
                                                                             capsys):
    _pages_fixture(fresh_orchestrator_dir, monkeypatch)
    cfg = fresh_orchestrator_dir / "dockwright.toml"
    cfg.write_text("[lanes]\nin_manager = false\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    mgr = {"name": "test-mgr", "sid": "mgr-test"}
    _alive(1000.0)
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 1100.0)
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 1181.0)
    monitor.run_pages_scan(mgr)
    out = capsys.readouterr().out
    assert out.startswith("DAEMON_SILENT 3min → dockwright daemon status")
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 1181.0 + 9 * 60)
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 1181.0 + 11 * 60)
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out.startswith("DAEMON_SILENT 14min")
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 1181.0 + 11 * 60 + 19 * 60)
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 1181.0 + 11 * 60 + 21 * 60)
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out.startswith("DAEMON_SILENT 35min")
    _alive(1181.0 + 11 * 60 + 21 * 60)
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out == ""
    beat = state.read_json(lane_io.heartbeat_path("test-mgr", "pages"))
    assert beat.get("daemon_silent_level") in (None, 0)


def test_the_pages_scan_is_silent_about_the_daemon_when_lanes_run_in_the_tab_or_the_daemon_is_stopped(
        fresh_orchestrator_dir, monkeypatch, capsys):
    _pages_fixture(fresh_orchestrator_dir, monkeypatch)
    mgr = {"name": "test-mgr", "sid": "mgr-test"}
    _alive(0.0)
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 9999.0)
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out == ""
    paths.DAEMON_STOP.write_text("")
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out == ""


def test_the_pages_scan_waits_ten_minutes_for_a_first_stamp_after_the_install(fresh_orchestrator_dir, monkeypatch,
                                                                             capsys):
    _pages_fixture(fresh_orchestrator_dir, monkeypatch)
    cfg = fresh_orchestrator_dir / "dockwright.toml"
    cfg.write_text("[lanes]\nin_manager = false\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    monkeypatch.setattr(paths, "LAUNCH_AGENTS", fresh_orchestrator_dir / "la")
    from dockwright import daemon
    daemon.plist_path().parent.mkdir()
    daemon.plist_path().write_text("x")
    mgr = {"name": "test-mgr", "sid": "mgr-test"}
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out == ""
    os.utime(daemon.plist_path(), (time.time() - 601, time.time() - 601))
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out.startswith("DAEMON_SILENT")


def test_pages_is_a_monitor_subcommand_with_a_scan():
    assert "pages" in monitor._MONITOR_SUBCOMMANDS and monitor._SCANS["pages"] == "run_pages_scan"


def test_a_question_file_that_is_not_an_object_suppresses_nothing(fresh_orchestrator_dir, monkeypatch):
    paths.QUESTIONS.mkdir(parents=True, exist_ok=True)
    (paths.QUESTIONS / "junk.json").write_text("[1, 2]")
    assert monitor._has_pending_question_for_sid("w1", 0.0) is False


def test_a_turn_end_sibling_that_is_not_an_object_is_skipped(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setattr(monitor, "_resolve_live_summary", lambda payload: None)
    entry = _write_aged_turn_end("test-mgr", "w1", age_sec=300)
    (paths.TURN_ENDS / "test-mgr" / "w1-2.json").write_text("[1, 2]")
    payload = state.read_json(entry)
    assert monitor._classify_turn_end(payload, entry, "test-mgr", None, time.time())[1] != "error"


def test_the_pages_scan_pages_a_daemon_whose_stamp_predates_a_reboot(fresh_orchestrator_dir, monkeypatch, capsys):
    _pages_fixture(fresh_orchestrator_dir, monkeypatch)
    cfg = fresh_orchestrator_dir / "dockwright.toml"
    cfg.write_text("[lanes]\nin_manager = false\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    _alive(90_000.0)
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 100.0)
    monitor.run_pages_scan({"name": "test-mgr", "sid": "mgr-test"})
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 400.0)
    monitor.run_pages_scan({"name": "test-mgr", "sid": "mgr-test"})
    assert capsys.readouterr().out.startswith("DAEMON_SILENT 6min")


def test_the_pages_scan_reads_a_stamp_written_after_its_clock_sample_as_fresh(fresh_orchestrator_dir, monkeypatch,
                                                                             capsys):
    _pages_fixture(fresh_orchestrator_dir, monkeypatch)
    cfg = fresh_orchestrator_dir / "dockwright.toml"
    cfg.write_text("[lanes]\nin_manager = false\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    clock = [1_152_460.0]
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: clock[0])
    alive = paths.DAEMON / "alive.json"
    real_read = state.read_json

    def read_after_a_fresh_stamp(path, *args, **kwargs):
        if path == alive:
            clock[0] += 0.004
            _alive(clock[0])
            clock[0] += 0.004
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(state, "read_json", read_after_a_fresh_stamp)
    monitor.run_pages_scan({"name": "test-mgr", "sid": "mgr-test"})
    assert capsys.readouterr().out == ""


def test_the_pages_scan_names_an_unparseable_config_and_keeps_the_daemons_last_mode(fresh_orchestrator_dir, monkeypatch,
                                                                                    capsys):
    _pages_fixture(fresh_orchestrator_dir, monkeypatch)
    cfg = fresh_orchestrator_dir / "dockwright.toml"
    cfg.write_text("[lanes]\nin_manager = false\n[gh.logins]\nacme = alice-unquoted\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    lane_io.remember_lanes_mode(False)
    mgr = {"name": "test-mgr", "sid": "mgr-test"}
    _alive(1000.0)
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 1100.0)
    monitor.run_pages_scan(mgr)
    out = capsys.readouterr().out
    assert out.startswith("CONFIG_UNREADABLE") and "line 4" in out and "DAEMON_SILENT" not in out
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 1100.0 + 5 * 60)
    monitor.run_pages_scan(mgr)
    out = capsys.readouterr().out
    assert out.startswith("DAEMON_SILENT 6min") and "CONFIG_UNREADABLE" not in out
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 1100.0 + 11 * 60)
    monitor.run_pages_scan(mgr)
    out = capsys.readouterr().out
    assert out.startswith("CONFIG_UNREADABLE") and "DAEMON_SILENT" not in out


def test_the_pages_scan_pages_a_stopped_daemon_when_the_lanes_run_in_it(fresh_orchestrator_dir, monkeypatch, capsys):
    _pages_fixture(fresh_orchestrator_dir, monkeypatch)
    cfg = fresh_orchestrator_dir / "dockwright.toml"
    cfg.write_text("[lanes]\nin_manager = false\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    mgr = {"name": "test-mgr", "sid": "mgr-test"}
    _alive(1000.0)
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 1000.0)
    paths.DAEMON_STOP.write_text("")
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out.startswith("DAEMON_STOPPED daemon-stop → rm ")
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 1000.0 + 5 * 60)
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out == ""
    paths.DAEMON_STOP.unlink()
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out == ""

def test_the_pages_scan_emits_no_daemon_stopped_while_the_fleet_is_paused(fresh_orchestrator_dir, monkeypatch, capsys):
    _pages_fixture(fresh_orchestrator_dir, monkeypatch)
    cfg = fresh_orchestrator_dir / "dockwright.toml"
    cfg.write_text("[lanes]\nin_manager = false\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    manifest = fresh_orchestrator_dir / "pause" / "manifest.json"
    monkeypatch.setattr(paths, "PAUSE_MANIFEST", manifest)
    mgr = {"name": "test-mgr", "sid": "mgr-test"}
    _alive(1000.0)
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 1000.0)
    manifest.parent.mkdir()
    manifest.write_text("{}")
    paths.DAEMON_STOP.write_text("")
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out == ""
    manifest.unlink()
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out.startswith("DAEMON_STOPPED daemon-stop → rm ")
    manifest.write_text("{}")
    paths.DAEMON_STOP.unlink()
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 1000.0 + 11 * 60)
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out.startswith("DAEMON_SILENT ")


def test_the_pages_scan_names_a_non_boolean_in_manager(fresh_orchestrator_dir, monkeypatch, capsys):
    _pages_fixture(fresh_orchestrator_dir, monkeypatch)
    cfg = fresh_orchestrator_dir / "dockwright.toml"
    cfg.write_text('[lanes]\nin_manager = "false"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    lane_io.remember_lanes_mode(False)
    _alive(1000.0)
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 1100.0)
    monitor.run_pages_scan({"name": "test-mgr", "sid": "mgr-test"})
    out = capsys.readouterr().out
    assert out.startswith("CONFIG_UNREADABLE") and "is not a boolean" in out


def test_pages_scan_prints_a_board_entry_on_its_first_pass_and_holds_a_batch_entry(fresh_orchestrator_dir, monkeypatch,
                                                                                   capsys):
    _pages_fixture(fresh_orchestrator_dir, monkeypatch)
    _alive(1000.0)
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 1000.0)
    mgr = {"name": "test-mgr", "sid": "mgr-test"}
    digest = _write_outbox_entry("test-mgr", "gardener digest ready: run-1", filename="1-a.json")
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out == "" and digest.exists()
    _write_outbox_entry("test-mgr", "ANSWER K-1 abcdef12 → x — yes", filename="2-b.json", source="board")
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out == "gardener digest ready: run-1\nANSWER K-1 abcdef12 → x — yes\n"
    assert list(paths.notify_outbox_dir_for("test-mgr").glob("*.json")) == []


def test_lanes_backlog_counts_an_unprinted_board_entry_as_stale(fresh_orchestrator_dir, monkeypatch):
    from dockwright import lanes
    now = time.time()
    board_entry = _write_outbox_entry("test-mgr", "ANSWER K-1 abcdef12 → x — yes", filename="1.json", source="board")
    os.utime(board_entry, (now - 10, now - 10))
    assert lanes._backlog("test-mgr", "pages", 3, now) == 1
    board_entry.unlink()
    digest = _write_outbox_entry("test-mgr", "gardener digest ready: run-1", filename="2.json")
    os.utime(digest, (now - 10, now - 10))
    assert lanes._backlog("test-mgr", "pages", 3, now) == 0


def test_the_question_lane_does_not_print_an_engineer_ask(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    from dockwright import mcp_server as ms
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    state.write_json_atomic(paths.ACTIVE / "m1.json", {"claude_sid": "m1", "name": "test-mgr", "agent": "manager",
                                                       "domain": "general", "pid": os.getpid()})
    ms.ask_engineer_impl("m1", "ship?", task_key="K-1")
    _write_question("test-mgr", "q1", "wkr-b", question="help me")
    monitor.run_questions_scan()
    out = capsys.readouterr().out
    assert out.startswith("QUESTION wkr-b → ") and "ship?" not in out and "test-mgr" not in out.split("→")[0]


def test_stale_scan_runs_the_events_step_before_reconcile(monkeypatch, fresh_orchestrator_dir):
    from dockwright import event_poll
    order, lines = [], []
    _stale_child_ok(monkeypatch)

    def fake_step(now, managers, emit):
        order.append(("events", [m["name"] for m in managers]))
        emit("EVENT_STARTED K sprint → x", "proud")
        emit("EVENT_POLL_FAILED tracker → y", None)
        emit("EVENT_STALLED sprint general → z")
        return []

    monkeypatch.setattr(event_poll, "step", fake_step)
    monkeypatch.setattr("dockwright.pipeline_reconcile.reconcile", lambda mgr: order.append(("reconcile", mgr["name"])) or [])
    monkeypatch.setattr("dockwright.monitor._manager_limited", lambda name: False)
    monkeypatch.setattr("dockwright.monitor.emit", lines.append)
    monitor.run_stale_scan({"name": "mgr", "sid": "m1"})
    assert order == [("events", ["mgr"]), ("reconcile", "mgr")]
    assert lines == ["EVENT_POLL_FAILED tracker → y", "EVENT_STALLED sprint general → z"]
    outbox = [state.read_json(p)["line"] for p in sorted(paths.notify_outbox_dir_for("proud").glob("*.json"))]
    assert outbox == ["EVENT_STARTED K sprint → x"]
    monkeypatch.setattr(event_poll, "step", lambda now, managers, emit: emit("EVENT_STARTED K2 sprint → x", "mgr"))
    monitor.run_stale_scan({"name": "mgr", "sid": "m1"})
    assert lines[-1] == "EVENT_STARTED K2 sprint → x"

    def boom(now, managers, emit):
        raise RuntimeError("poll exploded")

    monkeypatch.setattr(event_poll, "step", boom)
    order.clear()
    lines.clear()
    monitor.run_stale_scan({"name": "mgr", "sid": "m1"})
    assert order == [("reconcile", "mgr")]
    assert len(lines) == 1 and lines[0].startswith("EVENT_POLL_FAILED * → ") and "poll exploded" in lines[0]


def _daemon_pages(fresh_orchestrator_dir, monkeypatch):
    _pages_fixture(fresh_orchestrator_dir, monkeypatch)
    cfg = fresh_orchestrator_dir / "dockwright.toml"
    cfg.write_text("[lanes]\nin_manager = false\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    return {"name": "test-mgr", "sid": "mgr-test"}


def _silent_level():
    return state.read_json(lane_io.heartbeat_path("test-mgr", "pages")).get("daemon_silent_level")


def test_a_fresh_stamp_with_a_step_over_ten_minutes_pages_daemon_stuck(fresh_orchestrator_dir, monkeypatch, capsys):
    mgr = _daemon_pages(fresh_orchestrator_dir, monkeypatch)
    _alive(2000.0, step="reconcile:mgr", step_awake=1390.0)
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 2000.0)
    monitor.run_pages_scan(mgr)
    out = capsys.readouterr().out
    assert out.startswith("DAEMON_STUCK reconcile:mgr 10min → dockwright daemon status") and "pid" not in out
    _alive(2540.0, step="reconcile:mgr", step_awake=1390.0)
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 2540.0)
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out == ""
    _alive(2660.0, step="reconcile:mgr", step_awake=1390.0)
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 2660.0)
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out.startswith("DAEMON_STUCK reconcile:mgr 21min")


def test_a_fresh_stamp_with_a_step_under_ten_minutes_pages_nothing(fresh_orchestrator_dir, monkeypatch, capsys):
    mgr = _daemon_pages(fresh_orchestrator_dir, monkeypatch)
    _alive(2000.0, step="reconcile:mgr", step_awake=1401.0)
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 2000.0)
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out == "" and _silent_level() in (None, 0)


def test_a_stale_stamp_pages_daemon_silent_whatever_its_step(fresh_orchestrator_dir, monkeypatch, capsys):
    mgr = _daemon_pages(fresh_orchestrator_dir, monkeypatch)
    _alive(1000.0, step="push", step_awake=100.0)
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 1181.0)
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out.startswith("DAEMON_SILENT 3min")


def test_a_stamp_without_a_step_keeps_the_silent_logic_only(fresh_orchestrator_dir, monkeypatch, capsys):
    mgr = _daemon_pages(fresh_orchestrator_dir, monkeypatch)
    _alive(2000.0)
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 2000.0)
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out == "" and _silent_level() in (None, 0)


@pytest.mark.parametrize("step,step_awake", [("push", "x"), ("push", None), (None, 100.0), (7, 100.0),
                                             ("push", True)])
def test_a_stamp_with_a_malformed_step_pages_nothing(fresh_orchestrator_dir, monkeypatch, capsys, step, step_awake):
    mgr = _daemon_pages(fresh_orchestrator_dir, monkeypatch)
    _alive(2000.0, step=step, step_awake=step_awake)
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 2000.0)
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out == "" and _silent_level() in (None, 0)


def test_a_step_that_moves_on_resets_the_ladder(fresh_orchestrator_dir, monkeypatch, capsys):
    mgr = _daemon_pages(fresh_orchestrator_dir, monkeypatch)
    _alive(2000.0, step="reconcile:mgr", step_awake=1390.0)
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 2000.0)
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out.startswith("DAEMON_STUCK") and _silent_level() == 1
    _alive(2700.0, step="notes", step_awake=2690.0)
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 2700.0)
    monitor.run_pages_scan(mgr)
    assert capsys.readouterr().out == "" and _silent_level() == 0
