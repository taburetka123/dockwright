import json
import os
import sys
import time
import pytest
from dockwright import paths, state, monitor, terminal


@pytest.fixture
def fresh_orchestrator_dir(tmp_path, monkeypatch):
    # The scans resolve the manager via TMUX_PANE (set below to match the record
    # window_id), which the driver's current_pane_id() reads. setenv overrides
    # any TMUX_PANE the runner inherits from its own tmux server.
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
    monitor.run_done_scan()  # pre-seed: this should emit it once
    capsys.readouterr()  # drain
    monitor.run_done_scan()  # second scan — should be silent
    assert capsys.readouterr().out == ""


def test_monitor_done_does_not_read_unscoped(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    """Critical: Fix #1 + Fix #2 — _unscoped/ events must NOT surface."""
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
                         name: str | None = None):
    record = {"claude_sid": sid, "agent": "worker", "name": name or sid,
              "window_id": "", "pid": os.getpid(), "state": worker_state,
              "parent_manager_name": "test-mgr"}
    if nested:
        record["nested"] = True
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
    """Place a fake Claude transcript where find_session_log resolves it:
    $HOME/.claude/projects/<slug>/<sid>.jsonl (slug is arbitrary)."""
    proj = home / ".claude" / "projects" / "-test-proj"
    proj.mkdir(parents=True, exist_ok=True)
    log = proj / f"{sid}.jsonl"
    log.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return log


# ---- turn-ends scan = silent-finish detector --------------------------------
# Routine turn-ends never reach the manager. A turn-end is held until GRACE
# old, then emitted as FINISHED_SILENTLY only when the worker neither reported
# done, nor kept working, nor has a pending question.

def test_monitor_turn_ends_young_files_stay_pending(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_turn_end("test-mgr", "wkr-young")            # completed_at = now
    _write_worker_record("wkr-young")
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""
    # Not marked seen: with the grace dropped to 0 the SAME file now emits.
    monkeypatch.setenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", "0")
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-young" in capsys.readouterr().out


def test_monitor_turn_ends_emits_finished_silently_with_summary_once(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_aged_turn_end("test-mgr", "wkr-1", name="alpha", summary="opened PR #5, all tests green")
    _write_worker_record("wkr-1", name="alpha")
    monitor.run_turn_ends_scan()
    out = capsys.readouterr().out.strip()
    assert out == "FINISHED_SILENTLY alpha: opened PR #5, all tests green"
    monitor.run_turn_ends_scan()                        # edge-triggered: once
    assert capsys.readouterr().out.strip() == ""


def test_monitor_turn_ends_suppresses_when_done_event_fresh(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_aged_turn_end("test-mgr", "wkr-1")
    _write_worker_record("wkr-1")
    _write_done("test-mgr", "wkr-1")
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""


def test_monitor_turn_ends_emits_when_done_event_stale(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    """A done event from a PREVIOUS task iteration (older than the lookback)
    must not mask a fresh silent finish."""
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
    """The manager's own aged turn-end must not page itself."""
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_aged_turn_end("test-mgr", "mgr-test", agent="manager")
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""


def test_monitor_turn_ends_suppresses_nested_record(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    """Pre-fix leftover turn-end files from nested ghosts must stay silent."""
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_aged_turn_end("test-mgr", "wkr-ghost")
    _write_worker_record("wkr-ghost", nested=True)
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""


def test_monitor_turn_ends_superseded_older_files_emit_once(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    """Two aged turn-ends for the same worker → exactly one line (the lull)."""
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_aged_turn_end("test-mgr", "wkr-1", age_sec=1300)  # gap > episode grace: not burst evidence
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
    assert out == "FINISHED_SILENTLY wkr-gone (session exited): pushed branch"


def test_monitor_turn_ends_grace_env_override(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", "0")
    _write_turn_end("test-mgr", "wkr-now")              # completed_at = now
    _write_worker_record("wkr-now")
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-now" in capsys.readouterr().out


def test_monitor_turn_ends_malformed_file_never_crashes(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    target_dir = paths.TURN_ENDS / "test-mgr"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "wkr-bad-123.json").write_text("{{{not json")
    monitor.run_turn_ends_scan()                        # must not raise
    assert capsys.readouterr().out.strip() == ""        # young by mtime → pending


def test_monitor_turn_ends_does_not_read_unscoped(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    """Same _unscoped invisibility contract for turn-ends."""
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_aged_turn_end(paths.UNSCOPED_BUCKET, "orphan")
    _write_aged_turn_end("test-mgr", "mine")
    monitor.run_turn_ends_scan()
    out = capsys.readouterr().out.strip()
    assert "FINISHED_SILENTLY mine" in out
    assert "orphan" not in out


# ---- turn-burst (episode) hold: closely-spaced turn-ends are a poll/wait ----
# cadence, not a finish. The newest lull of a burst is held PENDING until the
# episode grace, so a polling worker pages at most once per episode boundary
# (observed 2026-07-16: macos-vm-spike paged once per poll cycle; tkt-1234
# paged while waiting on a background CI-poll Bash task).

def test_turn_burst_holds_newest_lull(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    _write_aged_turn_end("test-mgr", "wkr-poll", age_sec=480)   # prior poll turn
    _write_aged_turn_end("test-mgr", "wkr-poll", age_sec=300)   # newest, 3min later
    _write_worker_record("wkr-poll")
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""
    seen = tmp_path / ".seen-turn-ends-test-mgr"
    if seen.exists():
        assert "wkr-poll" not in seen.read_text() or \
            sum("wkr-poll" in line for line in seen.read_text().splitlines()) == 1, \
            "only the SUPERSEDED older file may be consumed; the newest must stay pending"


def test_turn_burst_fires_once_past_episode_grace(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    """End of episode: the last turn-end of a burst ages past the episode
    grace and fires exactly once — delayed, never swallowed."""
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    _write_aged_turn_end("test-mgr", "wkr-pend", age_sec=1180)
    _write_aged_turn_end("test-mgr", "wkr-pend", age_sec=1000)  # newest, past 900s
    _write_worker_record("wkr-pend")
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-pend" in capsys.readouterr().out
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""


def test_turn_burst_holds_mid_episode_after_first_lull_paged(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    """The first lull of an episode pages once and gets seen-marked; a
    mid-episode lull must STILL be burst-held — an already-resolved sibling
    is burst evidence, not closed business. (Guards against filtering
    prior_ts by the seen set, which would revive the per-lull flood.)
    Ladder base is forced to 1s so only classification can hold the second
    lull — otherwise FS_HOLD masks the difference."""
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
    """No sibling within the episode window → the common genuine case pages
    at the base grace exactly as before."""
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    _write_aged_turn_end("test-mgr", "wkr-solo", age_sec=300)
    _write_worker_record("wkr-solo")
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-solo" in capsys.readouterr().out


def test_distant_sibling_does_not_engage_burst_hold(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    """A sibling from a PREVIOUS episode (gap > episode grace) is not burst
    evidence."""
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    _write_aged_turn_end("test-mgr", "wkr-gap", age_sec=1500)   # >900s before newest
    _write_aged_turn_end("test-mgr", "wkr-gap", age_sec=300)
    _write_worker_record("wkr-gap")
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-gap" in capsys.readouterr().out


def test_burst_hold_does_not_delay_exited_session(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    """A gone session (no active record) pages at the base grace even inside
    a burst — new information beats episode patience."""
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    _write_aged_turn_end("test-mgr", "wkr-gone", age_sec=480)
    _write_aged_turn_end("test-mgr", "wkr-gone", age_sec=300)
    # no _write_worker_record → active record missing → EMIT_EXITED path
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-gone (session exited)" in capsys.readouterr().out


def test_monitor_questions_emits_only_current_manager_questions(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_question("test-mgr", "q-mine", "mine-worker", question="ours?")
    _write_question("peer-mgr", "q-peer", "peer-worker", question="theirs?")

    monitor.run_questions_scan()

    out = capsys.readouterr().out
    assert "mine-worker asks: ours?" in out
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
    """monitor stale runs the packaged module with the resolved manager name."""
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


def test_main_module_dispatches_monitor_done(fresh_orchestrator_dir, monkeypatch, capsys):
    """End-to-end: invoking `python -m dockwright monitor done` via
    the main dispatcher reaches run_done_scan."""
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


# --- manager-limited hold: flag file pauses event surfacing -------------------
# While the owning manager is bricked on a rate limit (stale_monitor maintains
# ROOT/.manager-limited-<name>), printing an event line = a task-notification =
# a failed wake attempt. The scans hold EVERYTHING — nothing printed, nothing
# marked seen — so events replay in full once the flag clears.


def _set_limited_flag(name="test-mgr"):
    (paths.ROOT / f".manager-limited-{name}").touch()


def test_done_scan_holds_while_manager_limited(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_done("test-mgr", "wkr-a", summary="finished")
    _set_limited_flag()
    monitor.run_done_scan()
    assert capsys.readouterr().out == ""
    assert not (tmp_path / ".seen-done-test-mgr").exists(), "held events must not be marked seen"

    # Flag cleared → the held event replays in full.
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
    """The flag is fail-closed with the sleep-60 stale loop as its only writer:
    if that loop dies mid-outage, the reader-side TTL keeps the manager from
    going permanently deaf. Stale mtime → ignore + best-effort unlink."""
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_done("test-mgr", "wkr-a", summary="finished")
    flag = paths.ROOT / ".manager-limited-test-mgr"
    flag.touch()
    old = time.time() - 700  # past the 10min TTL
    os.utime(flag, (old, old))
    monitor.run_done_scan()
    out = capsys.readouterr().out
    assert "wkr-a" in out and "finished" in out, "a stale flag must not hold events"
    assert not flag.exists(), "stale flag is best-effort cleared"


def test_monitor_turn_ends_emits_when_done_is_from_previous_task(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    """Re-task timeline (verifier Important-2 on #62): done(A), manager
    re-tasks the worker, task B finishes silently 30min later — the old done
    event must not mask B's silent finish forever."""
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
    """Post-done cleanup inside the same turn: done a few minutes before the
    turn-end is the normal worker_done shape — suppressed."""
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


def test_classify_turn_end_pending_when_timestamp_unresolvable(fresh_orchestrator_dir):
    """File pruned between glob and read → no payload ts, stat fails. ts=0
    must not bypass the grace window (verifier minor-5 on #62)."""
    from pathlib import Path as _P
    verdict = monitor.classify_turn_end({}, _P("/nonexistent/wkr-x-123.json"),
                                        "test-mgr", None, time.time())
    assert verdict == monitor.TURN_END_PENDING


# ---- delegation hold: fresh subagent transcripts suppress the silent-finish --
# A worker that dispatched a background subagent ends its TURN but not its
# WORK. While the subagent transcript keeps growing, the turn-end is held
# (PENDING, never marked seen); once it goes quiet past grace the alert fires.

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
    # subagent wrote 10s ago — after the 300s-old turn-end, within grace
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
    # subagent grew after turn-end (1000 < 1200) but has been quiet 1000s > episode grace
    _make_subagent_tree(fresh_orchestrator_dir, "wkr-dead", log_age_sec=1200, agent_write_age_sec=1000)
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-dead" in capsys.readouterr().out
    monitor.run_turn_ends_scan()                          # edge-triggered: once
    assert capsys.readouterr().out.strip() == ""


def test_monitor_turn_ends_emits_when_subagent_writes_predate_turn_end(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    """Subagent activity from EARLIER in the turn (already consumed) must not hold."""
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    _write_aged_turn_end("test-mgr", "wkr-old", age_sec=180)
    _write_worker_record("wkr-old")
    # last subagent write 240s ago — BEFORE the 180s-old turn-end
    _make_subagent_tree(fresh_orchestrator_dir, "wkr-old", log_age_sec=180, agent_write_age_sec=240)
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-old" in capsys.readouterr().out


def test_delegation_hold_true_at_exact_turn_end_tie(fresh_orchestrator_dir, monkeypatch):
    """Subagent write exactly AT the turn-end ts counts as at/after (>=, not
    strict >) — a hold, when fresh. Pinned on _delegation_hold directly; since
    the hold freshness split to the episode grace, the classifier CAN surface
    a tie as PENDING once the file is past the base grace."""
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    ts = time.time() - 10                                 # one captured instant
    project_dir = fresh_orchestrator_dir / ".claude" / "projects" / "-Users-test"
    project_dir.mkdir(parents=True, exist_ok=True)
    log = project_dir / "wkr-tie.jsonl"
    log.write_text("")
    os.utime(log, (ts - 300, ts - 300))
    subagents = project_dir / "wkr-tie" / "subagents"
    subagents.mkdir(parents=True, exist_ok=True)
    agent = subagents / "agent-aaa.jsonl"
    agent.write_text("{}")
    os.utime(agent, (ts, ts))                             # mtime == turn-end ts exactly
    record = {"claude_sid": "wkr-tie", "runtime": "claude"}
    assert monitor._delegation_hold(record, "wkr-tie", ts, time.time()) is True
    # One second earlier than the turn-end → not at/after → no hold.
    os.utime(agent, (ts - 1, ts - 1))
    assert monitor._delegation_hold(record, "wkr-tie", ts, time.time()) is False


def test_monitor_turn_ends_done_beats_delegation_hold(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    """A fresh done event suppresses (and consumes) the turn-end even while a
    background subagent still writes: the done check fires before the
    delegation hold, so the file is marked seen instead of lingering PENDING."""
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    entry = _write_aged_turn_end("test-mgr", "wkr-dd", age_sec=300)
    _write_worker_record("wkr-dd")
    _write_done("test-mgr", "wkr-dd")                     # fresh: within lookback
    # subagent wrote 10s ago — after the turn-end, fresh within grace
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
    # a (bogus) fresh claude-layout subagent tree must NOT hold a codex worker
    _make_subagent_tree(fresh_orchestrator_dir, "wkr-cdx", log_age_sec=300, agent_write_age_sec=10)
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-cdx" in capsys.readouterr().out


def test_delegation_hold_survives_slow_subagent_write_gap(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    """The confirmed 2026-07-16 false-fire class: a LIVE reviewer subagent
    thinking/reading 3-4min between transcript writes (observed gaps
    208s/239s/165s) aged out of the old 120s freshness and paged the manager.
    The hold now ages on the 900s episode grace."""
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    _write_aged_turn_end("test-mgr", "wkr-slow", age_sec=600)
    _write_worker_record("wkr-slow")
    # subagent wrote 300s ago — after the 600s-old turn-end; 300s > old 120s
    # grace but well inside the 900s episode grace → still held
    _make_subagent_tree(fresh_orchestrator_dir, "wkr-slow", log_age_sec=600, agent_write_age_sec=300)
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out.strip() == ""
    assert not (tmp_path / ".seen-turn-ends-test-mgr").exists()


def test_delegation_hold_expires_past_episode_grace(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    """A dead subagent still fires once — at the episode grace instead of the
    old 120s (the spec's accepted latency growth)."""
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(fresh_orchestrator_dir))
    _write_aged_turn_end("test-mgr", "wkr-deadsa", age_sec=1200)
    _write_worker_record("wkr-deadsa")
    # subagent grew after turn-end (1000 < 1200) but quiet 1000s > 900s
    _make_subagent_tree(fresh_orchestrator_dir, "wkr-deadsa", log_age_sec=1200, agent_write_age_sec=1000)
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-deadsa" in capsys.readouterr().out


def test_episode_grace_env_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_EPISODE_GRACE_SEC", "1800")
    monkeypatch.delenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", raising=False)
    assert monitor._episode_grace_sec() == 1800


def test_episode_grace_clamps_to_base_grace(monkeypatch):
    """Invariant episode_grace >= base_grace: an operator raising the shared
    turn-end grace past 900 must not invert the two."""
    monkeypatch.setenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", "1200")
    monkeypatch.delenv("CLAUDE_ORCH_EPISODE_GRACE_SEC", raising=False)
    assert monitor._episode_grace_sec() == 1200


def test_episode_grace_default(monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_EPISODE_GRACE_SEC", raising=False)
    monkeypatch.delenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", raising=False)
    assert monitor._episode_grace_sec() == 900


# ---- live transcript re-read at emit time -----------------------------------
# The marker's last_summary is a Stop-hook snapshot that can freeze on a
# mid-turn narration (a transcript flush race). At emit time the turn-end is
# >= grace old → fully flushed, so we re-read the LIVE transcript and fall back
# to the marker only when the live read yields nothing.

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
    """A resumed worker (state=processing) is suppressed at the classify layer,
    so the live re-read never surfaces a new-turn narration."""
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
        _assistant_event("", ts="2026-06-18T08:11:24Z"),  # empty trailing end_turn
    ])
    monitor.run_turn_ends_scan()
    out = capsys.readouterr().out.strip()
    assert "FINAL end_turn: Paused" in out
    assert "midturn" not in out


def test_monitor_turn_ends_falls_back_to_marker_when_no_transcript(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(tmp_path))  # no $HOME/.claude/projects created
    _write_aged_turn_end("test-mgr", "wkr-not", name="gamma", summary="marker fallback text")
    _write_worker_record("wkr-not", name="gamma")
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY gamma: marker fallback text" in capsys.readouterr().out


def test_monitor_turn_ends_falls_back_to_marker_when_transcript_has_no_assistant_text(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(tmp_path))
    sid = "wkr-empty"
    _write_aged_turn_end("test-mgr", sid, name="zeta", summary="marker fallback text")
    _write_worker_record(sid, name="zeta")
    _write_transcript(tmp_path, sid, [{"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}])
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY zeta: marker fallback text" in capsys.readouterr().out


def test_monitor_turn_ends_long_summary_not_cut_at_160(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setenv("HOME", str(tmp_path))
    sid = "wkr-long"
    long_text = "FINAL " + ("x" * 300)  # 306 chars: > old 160 cap, < new 400 cap
    _write_aged_turn_end("test-mgr", sid, name="eta", summary="short marker")
    _write_worker_record(sid, name="eta")
    _write_transcript(tmp_path, sid, [_assistant_event(long_text)])
    monitor.run_turn_ends_scan()
    out = capsys.readouterr().out
    assert long_text in out                  # full message survives (not cut at 160)
    assert "…" not in out                    # 306 < 400 → no ellipsis


def test_resolve_live_summary_crashproof(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert monitor._resolve_live_summary({}) is None                       # no sid
    assert monitor._resolve_live_summary({"sid": "nonexistent-sid"}) is None  # no transcript


# ---------------------------------------------------------------------------
# notify-outbox drain (cross-lane piggyback)


def _write_outbox_entry(manager_name: str, line: str, buffered_at: float | None = None,
                        filename: str | None = None, raw: bytes | None = None):
    outbox = paths.notify_outbox_dir_for(manager_name)
    outbox.mkdir(parents=True, exist_ok=True)
    ts = buffered_at if buffered_at is not None else time.time()
    fname = filename or f"{int(ts * 1000)}-{os.getpid()}-{len(list(outbox.glob('*.json')))}.json"
    target = outbox / fname
    if raw is not None:
        target.write_bytes(raw)
    else:
        state.write_json_atomic(target, {"line": line, "kind": "autoclosed",
                                         "buffered_at": ts})
    return target


def test_notify_outbox_dir_derives_from_root_at_call_time(fresh_orchestrator_dir):
    # Fixture patched paths.ROOT to tmp_path; the helper must follow it so a
    # fixture omission can never touch the real ~/.claude/dockwright.
    assert str(paths.notify_outbox_dir_for("test-mgr")).startswith(str(fresh_orchestrator_dir))
    assert paths.notify_outbox_dir_for("a/b").name == "a_b"


def test_done_scan_drains_outbox_when_printing(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_done("test-mgr", "wkr-a", summary="finished")
    _write_outbox_entry("test-mgr", "AUTOCLOSED old-worker idle 130min")
    monitor.run_done_scan()
    out = capsys.readouterr().out
    assert "wkr-a done: finished" in out
    assert "AUTOCLOSED old-worker idle 130min" in out
    # Urgent line first, piggybacked line after.
    assert out.index("wkr-a done") < out.index("AUTOCLOSED")
    assert list(paths.notify_outbox_dir_for("test-mgr").glob("*.json")) == []


def test_done_scan_leaves_outbox_when_silent(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    entry = _write_outbox_entry("test-mgr", "AUTOCLOSED old-worker idle 130min")
    monitor.run_done_scan()
    assert capsys.readouterr().out == ""
    assert entry.exists()


def test_questions_scan_drains_outbox_when_printing(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_question("test-mgr", "q1", "wkr-b", question="help me")
    _write_outbox_entry("test-mgr", "AUTOCLOSED old-worker idle 130min")
    monitor.run_questions_scan()
    out = capsys.readouterr().out
    assert "wkr-b asks: help me" in out
    assert "AUTOCLOSED old-worker idle 130min" in out
    assert out.index("asks:") < out.index("AUTOCLOSED")


def test_drain_scoped_to_own_manager(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_done("test-mgr", "wkr-a")
    peer_entry = _write_outbox_entry("peer-mgr", "AUTOCLOSED peer-worker idle 130min")
    monitor.run_done_scan()
    assert "peer-worker" not in capsys.readouterr().out
    assert peer_entry.exists()


def test_drain_corrupt_entry_is_not_a_poison_pill(fresh_orchestrator_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_done("test-mgr", "wkr-a")
    # Sorts FIRST; undecodable; must be unlinked and must not block the good entry.
    bad = _write_outbox_entry("test-mgr", "", filename="0000000000000-0-0.json", raw=b"{{{not json")
    good = _write_outbox_entry("test-mgr", "AUTOCLOSED good-worker idle 130min")
    monitor.run_done_scan()
    captured = capsys.readouterr()
    assert "AUTOCLOSED good-worker idle 130min" in captured.out
    assert not bad.exists()
    assert not good.exists()


def test_drain_missing_file_race_is_silent(fresh_orchestrator_dir, capsys, monkeypatch, tmp_path):
    # FileNotFoundError on read = a concurrent drainer won the race — skip, no stderr noise.
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    _write_done("test-mgr", "wkr-a")
    entry = _write_outbox_entry("test-mgr", "AUTOCLOSED gone idle 130min")
    real_read_text = type(entry).read_text

    def racing_read_text(self, *a, **kw):
        if self.name == entry.name:
            self.unlink(missing_ok=True)
            raise FileNotFoundError(str(self))
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(type(entry), "read_text", racing_read_text)
    monitor.run_done_scan()  # must not raise
    captured = capsys.readouterr()
    assert "gone" not in captured.out
    assert "outbox drain failed" not in captured.err


def test_concurrent_drainers_lose_nothing(fresh_orchestrator_dir, capsys):
    # I1: two lanes draining the same outbox concurrently. Worst case is a
    # duplicate print, NEVER a lost line or a crash.
    import threading
    lines = [f"AUTOCLOSED worker-{i} idle 130min" for i in range(20)]
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
        assert line in out  # every line delivered at least once
    assert list(paths.notify_outbox_dir_for("test-mgr").glob("*.json")) == []


# ---------------------------------------------------------------------------
# FINISHED_SILENTLY per-sid emit ladder


def _write_turn_end_at(manager_name: str, sid: str, ts: float):
    target_dir = paths.TURN_ENDS / manager_name
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{sid}-{int(ts * 1000)}.json"
    state.write_json_atomic(path, {"sid": sid, "name": sid, "completed_at": ts,
                                   "last_summary": "did things"})
    return path


def _write_fs_worker_record(sid: str, **overrides):
    # NOTE: tests/test_monitor_cli.py ALREADY defines _write_worker_record
    # (line ~114, keyword-only signature) used by ~22 existing call sites —
    # this helper is deliberately named differently to avoid rebinding it.
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
    # The live-transcript re-read needs no real transcript: force the marker
    # fallback so the emitted line is deterministic.
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
    _seed_ladder("test-mgr", "wkr-1", last_emit=now - 300, level=1)  # rung 900s
    entry = _write_turn_end_at("test-mgr", "wkr-1", now - 300)
    monitor.run_turn_ends_scan()
    assert capsys.readouterr().out == ""
    # HOLD must not mark seen: force the rung to mature and re-scan the SAME file.
    _seed_ladder("test-mgr", "wkr-1", last_emit=now - 1000, level=1)
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-1" in capsys.readouterr().out
    assert entry.exists()  # seen-marking, not deletion, is the consumption mechanism


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
    # Level 10 uncapped would be 15min * 2^9 = 128h; the cap keeps every rung
    # far below the 24h turn-end file TTL (spec C1).
    _write_fs_worker_record("wkr-1")
    now = time.time()
    _seed_ladder("test-mgr", "wkr-1", last_emit=now - (4 * 3600 + 120), level=10)
    _write_turn_end_at("test-mgr", "wkr-1", now - 300)
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-1" in capsys.readouterr().out


def test_fs_reset_on_processing_since(fs_scan, capsys):
    # Manager re-instructed the worker after the last page -> a new lull pages
    # immediately and the ladder restarts at level 1.
    now = time.time()
    _write_fs_worker_record("wkr-1", processing_since=now - 200)
    _seed_ladder("test-mgr", "wkr-1", last_emit=now - 400, level=5)
    _write_turn_end_at("test-mgr", "wkr-1", now - 150)
    monitor.run_turn_ends_scan()
    assert "FINISHED_SILENTLY wkr-1" in capsys.readouterr().out
    ladder = json.loads(monitor._fs_ladder_path("test-mgr").read_text())
    assert ladder["wkr-1"]["level"] == 1


def test_fs_gate_reset_on_done_after_last_emit(fresh_orchestrator_dir):
    # Unit-level: an intervening done (outside classify's 600s lookback of the
    # NEXT turn-end) resets the ladder episode.
    now = time.time()
    _write_done("test-mgr", "wkr-1")  # completed_at = now > last_emit
    ladder = {"wkr-1": {"last_emit": now - 3600, "level": 3, "exited": False}}
    assert monitor._fs_ladder_gate(ladder, "wkr-1", monitor.TURN_END_EMIT,
                                   "test-mgr", now) == monitor.FS_EMIT_RESET


def test_fs_gate_exited_transition_resets_once(fresh_orchestrator_dir):
    now = time.time()
    ladder = {"wkr-1": {"last_emit": now - 60, "level": 2, "exited": False}}
    # No active record on disk: the gate must tolerate that without raising.
    assert monitor._fs_ladder_gate(ladder, "wkr-1", monitor.TURN_END_EMIT_EXITED,
                                   "test-mgr", now) == monitor.FS_EMIT_RESET
    ladder["wkr-1"]["exited"] = True
    assert monitor._fs_ladder_gate(ladder, "wkr-1", monitor.TURN_END_EMIT_EXITED,
                                   "test-mgr", now) == monitor.FS_HOLD


def test_fs_record_reset_clears_sticky_exited_flag(fresh_orchestrator_dir):
    # A RESET must clear a stale "exited" flag to the CURRENT verdict, not
    # inherit it from the prior episode — otherwise a resumed-then-re-exited
    # worker's real exit gets rung-delayed instead of paging immediately.
    now = time.time()
    sid = "wkr-1"
    ladder = {sid: {"last_emit": now - 400, "level": 2, "exited": True}}
    _write_fs_worker_record(sid, processing_since=now - 200)
    assert monitor._fs_ladder_gate(ladder, sid, monitor.TURN_END_EMIT,
                                   "test-mgr", now) == monitor.FS_EMIT_RESET
    monitor._fs_ladder_record(ladder, sid, monitor.TURN_END_EMIT,
                              monitor.FS_EMIT_RESET, now)
    assert ladder[sid]["exited"] is False
    # With the sticky flag gone, a later real exit must re-fire immediately.
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
    # M1b: flag set -> scan early-returns (nothing printed, nothing seen,
    # ladder untouched); flag cleared with rung due -> emits exactly once.
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
    # I4a: print happened, ladder write failed -> the NEXT turn-end pages
    # again promptly (duplicate) instead of being silenced.
    _write_fs_worker_record("wkr-1")
    now = time.time()
    # first turn-end sits OUTSIDE the episode grace of the retry (gap > 900s),
    # so the burst hold cannot interfere with the at-least-once check; a
    # mid-burst retry is covered by
    # test_turn_burst_holds_mid_episode_after_first_lull_paged (delayed
    # <= episode grace, never silenced).
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
    # Ladder never recorded the first page -> no entry -> emits again (at-least-once).
    assert "FINISHED_SILENTLY wkr-1" in capsys.readouterr().out


def test_fs_emit_drains_outbox(fs_scan, capsys):
    _write_fs_worker_record("wkr-1")
    _write_turn_end_at("test-mgr", "wkr-1", time.time() - 300)
    _write_outbox_entry("test-mgr", "AUTOCLOSED rider idle 130min")
    monitor.run_turn_ends_scan()
    out = capsys.readouterr().out
    assert "FINISHED_SILENTLY wkr-1" in out
    assert "AUTOCLOSED rider idle 130min" in out
    assert out.index("FINISHED_SILENTLY") < out.index("AUTOCLOSED")


# ---- legacy state-root prefix normalization in SEEN cursors -----------------
# Pre-rename cursor files persist ABSOLUTE event paths under the old state
# root (~/.claude/orchestrator/...). After the root flips to ~/.claude/
# dockwright, those lines would never match new-code glob results, replaying
# already-delivered events. _load_seen normalizes any legacy-rooted line to
# the new root so migrated events dedupe correctly.

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


# --- N-5: positional manager name -------------------------------------------
# `dockwright monitor <sub> [manager-name]` must honor the positional name
# (identity resolution via TMUX_PANE/PPID is the fallback, not the only path)
# and fail LOUDLY (rc=2) when neither resolves.


def test_monitor_stale_honors_positional_manager_name(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "bogus-win")  # pane resolution must fail
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
    assert "test-mgr" in err  # lists the active managers


def test_monitor_extra_args_exit_2(fresh_orchestrator_dir, capsys):
    with pytest.raises(SystemExit) as ei:
        monitor.main(["done", "test-mgr", "extra"])
    assert ei.value.code == 2


def test_monitor_unresolvable_identity_exits_2(fresh_orchestrator_dir, monkeypatch):
    """Regression pin for the E2E N-5 rc claim: a resolution failure must be
    NON-ZERO (the observed rc=0 was a driver measurement artifact)."""
    monkeypatch.setenv("TMUX_PANE", "bogus-win")
    from dockwright import identity
    monkeypatch.setattr(identity, "_resolve_via_ppid_walk", lambda records: None)
    with pytest.raises(SystemExit) as ei:
        monitor.main(["stale"])
    assert ei.value.code == 2


def test_monitor_positional_ambiguous_name_exits_2(fresh_orchestrator_dir, capsys):
    """Two active manager records sharing the same name: the failure message
    must say the name is ambiguous (with the match count), not the generic
    no-record-found message."""
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
    """Fix D: an unknown subcommand must be reported as such even when the
    positional manager name is also bogus — never misreported as a
    no-such-manager-record failure."""
    with pytest.raises(SystemExit) as ei:
        monitor.main(["bogus-sub", "no-such-mgr"])
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "Unknown monitor subcommand" in err
    assert "no active manager record" not in err
