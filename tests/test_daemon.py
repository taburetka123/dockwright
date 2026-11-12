import fcntl
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from dockwright import daemon, lane_io, monitor, paths, state, terminal

MGR = {"name": "mgr", "sid": "mgr-sid"}


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    terminal._DRIVER = None
    for attr, sub in (("ROOT", ""), ("ACTIVE", "active"), ("DONE", "done"), ("TURN_ENDS", "turn-ends"),
                      ("QUESTIONS", "questions"), ("ANSWERS", "answers"), ("CLOSED", "closed"),
                      ("HANDOFFS", "handoffs"), ("PRESETS", "presets"), ("SLOTS", "slots"),
                      ("MANAGER_MEMORY", "manager-memory"), ("ARCHITECT", "architect"),
                      ("ARTIFACTS", "artifacts"), ("ASSIGNMENTS", "assignments"),
                      ("PANE_SENDS", "pane-sends"), ("LANE_HEALTH", "lane-health"), ("DAEMON", "daemon")):
        monkeypatch.setattr(paths, attr, tmp_path / sub if sub else tmp_path)
    monkeypatch.setattr(paths, "ASSIGNMENTS_PENDING", tmp_path / "assignments" / ".pending")
    monkeypatch.setattr(paths, "DAEMON_STOP", tmp_path / "daemon-stop")
    monkeypatch.setattr(paths, "DAEMON_WAKE", tmp_path / "daemon" / "wake")
    paths.ensure_dirs()
    paths.DAEMON.mkdir()
    monkeypatch.setattr(monitor, "_resolve_live_summary", lambda payload: None)
    monkeypatch.setattr(monitor, "_seen_file", lambda kind, name: tmp_path / f".seen-{kind}-{name}")
    monkeypatch.setattr(lane_io, "reader_is_dead", lambda fd=1: False)
    monkeypatch.setenv("CLAUDE_ORCH_SILENT_FINISH_GRACE_SEC", "120")
    state.write_json_atomic(paths.ACTIVE / "mgr-sid.json", {
        "claude_sid": "mgr-sid", "agent": "manager", "name": "mgr", "window_id": "w-mgr",
        "pid": os.getpid(), "domain": "general"})
    return tmp_path


def _no_push(now, start):
    return []


def _touch_wake_now() -> None:
    daemon.wake()
    now = time.time()
    os.utime(paths.DAEMON_WAKE, (now, now))


def _done(bucket, sid, summary="ok"):
    d = paths.DONE / bucket
    d.mkdir(parents=True, exist_ok=True)
    entry = d / f"{sid}-{int(time.time() * 1000)}.json"
    state.write_json_atomic(entry, {"event_id": sid, "claude_sid": sid, "worker_name": sid,
                                    "parent_manager_name": bucket, "summary": summary,
                                    "completed_at": time.time()})
    return entry


def _turn_end(bucket, sid, age_sec=600):
    d = paths.TURN_ENDS / bucket
    d.mkdir(parents=True, exist_ok=True)
    ts = time.time() - age_sec
    entry = d / f"{sid}-{int(ts * 1000)}.json"
    state.write_json_atomic(entry, {"sid": sid, "name": sid, "completed_at": ts, "last_summary": "s"})
    return entry


def _worker(sid, *, kind="worker", parent="mgr"):
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {
        "claude_sid": sid, "agent": "worker", "kind": kind, "name": sid, "window_id": "",
        "pid": os.getpid(), "state": "idle", "parent_manager_name": parent, "parent_name": parent})


def _question(parent, qid, sid, task_key=None):
    record = {"question_id": qid, "worker_sid": sid, "worker_name": sid, "parent_manager_name": parent,
              "parent_name": parent, "question": "help?", "asked_at": time.time(), "task_key": task_key}
    if task_key:
        state.write_json_atomic(paths.question_task_dir(task_key) / f"{qid}.json", record)
    state.write_json_atomic(paths.question_dir_for(parent) / f"{qid}.json", record)


def _ledger():
    return daemon.read_jsonl(paths.DAEMON / "ledger.jsonl")


def _events(bucket=None):
    return {(r["kind"], r["key"], r["outcome"]) for r in _ledger()
            if r["type"] == "event" and (bucket is None or r["bucket"] == bucket)}


def _heartbeat():
    return state.read_json(paths.DAEMON / "heartbeat.json")


def _flag(fleet, monkeypatch, value, extra=""):
    cfg = fleet / "dockwright.toml"
    cfg.write_text(f"[lanes]\nin_manager = {value}\n{extra}")
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))


def _outbox(name):
    d = paths.notify_outbox_dir_for(name)
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.is_dir() else []


def _no_actions(monkeypatch):
    monkeypatch.setattr(daemon, "_act", lambda now, managers, acting: 0)


def _lanes():
    monitor.run_done_scan(dict(MGR))
    monitor.run_turn_ends_scan(dict(MGR))
    monitor.run_questions_scan(dict(MGR))


def test_daemon_and_lane_record_the_same_decisions_for_a_manager_bucket(fleet, capsys):
    _done("mgr", "w1")
    _turn_end("mgr", "w2")
    _worker("w2")
    _question("mgr", "q1", "w3")
    _lanes()
    printed = {line.split(" ", 1)[0] for line in capsys.readouterr().out.splitlines() if line}
    daemon.tick(push=_no_push)
    emitted = {kind for kind, _key, outcome in _events("mgr") if outcome == monitor.OBSERVE_EMIT}
    assert printed == emitted == {"DONE", "FINISHED_SILENTLY", "QUESTION"}


def test_the_daemon_leaves_every_lane_file_alone_and_prints_nothing(fleet, capsys):
    _done("mgr", "w1")
    _turn_end("mgr", "w2")
    _worker("w2")
    _question("mgr", "q1", "w3")
    outbox = paths.notify_outbox_dir_for("mgr")
    outbox.mkdir(parents=True)
    (outbox / "1-1-0.json").write_text(json.dumps({"line": "gardener digest ready: run-w1"}))
    daemon.tick(push=_no_push)
    assert capsys.readouterr().out == ""
    assert list(fleet.glob(".seen-*")) == []
    assert list(fleet.glob(".fs-emitted-*")) == []
    assert (outbox / "1-1-0.json").exists()
    assert not lane_io.heartbeat_path("mgr", "done").exists()
    _lanes()
    out = capsys.readouterr().out
    assert "w1" in out and "FINISHED_SILENTLY w2" in out and "QUESTION w3" in out
    assert "gardener digest ready: run-w1" in out


def test_the_daemon_reads_an_orphan_bucket_and_skips_subworkers(fleet):
    orphan = _done("gone-mgr", "w5")
    _worker("parent-w", parent="mgr")
    _worker("sub-1", kind="subworker", parent="parent-w")
    _turn_end("parent-w", "sub-1")
    _question("parent-w", "q-sub", "sub-1")
    _question("mgr", "q-keyed", "w6", task_key="WORK-1")
    daemon.tick(push=_no_push)
    keys = {key for _k, key, _o in _events()}
    assert orphan.name in keys
    assert "q-sub" not in keys and not any(k.startswith("sub-1-") for k in keys)
    assert [r["key"] for r in _ledger() if r["type"] == "event" and r["kind"] == "QUESTION"] == ["q-keyed"]
    keyed = next(r for r in _ledger() if r.get("key") == "q-keyed")
    assert keyed["task_key"] == "WORK-1" and keyed["bucket"] == "mgr"


def test_events_older_than_start_are_marked_pre_start(fleet):
    start = time.time()
    daemon.ensure_start(start)
    old = _done("mgr", "w-old")
    os.utime(old, (start - 100, start - 100))
    new = _done("mgr", "w-new")
    os.utime(new, (start + 5, start + 5))
    daemon.tick(now=start + 10, push=_no_push)
    flags = {r["key"]: r["pre_start"] for r in _ledger() if r["type"] == "event"}
    assert flags == {old.name: True, new.name: False}
    assert [r["type"] for r in _ledger()].count("start") == 1


def test_push_receives_the_start_and_its_records_are_ledgered(fleet):
    seen = []

    def push(now, start):
        seen.append(start)
        return [{"type": "push", "topic": "WORK-1", "kinds": ["question"], "ok": True, "rc": 0}]

    daemon.ensure_start(123.0)
    daemon.tick(push=push)
    assert seen == [123.0]
    assert any(r["type"] == "push" and r["topic"] == "WORK-1" for r in _ledger())
    assert _heartbeat()["pushes"] == 1


def test_push_reads_the_board_inbox(fleet, monkeypatch):
    from dockwright import board
    monkeypatch.setattr(board, "inbox",
                        lambda: {"waiting": [{"kind": "question", "topic": "WORK-1", "anchor": 100.0}]})
    records = daemon._push(1_000.0, 50.0)
    assert [(r["type"], r["topic"], r["kinds"]) for r in records] == [("push", "WORK-1", ["question"])]


def test_the_stop_file_stops_every_scan_and_resume_is_not_a_gap(fleet):
    t = time.time()
    daemon.tick(now=t, push=_no_push)
    paths.DAEMON_STOP.write_text("")
    _done("mgr", "w9")
    daemon.tick(now=t + 60, push=_no_push)
    daemon.tick(now=t + 120, push=_no_push)
    assert [r["type"] for r in _ledger()].count("stopped") == 1
    assert _heartbeat()["stopped"] is True
    assert not any(r.get("key", "").startswith("w9-") for r in _ledger())
    paths.DAEMON_STOP.unlink()
    daemon.tick(now=t + 900, push=_no_push)
    types = [r["type"] for r in _ledger()]
    assert "resumed" in types and "gap" not in types
    assert any(r.get("key", "").startswith("w9-") for r in _ledger())


def test_a_silent_stretch_leaves_one_gap_line(fleet):
    t = time.time()
    daemon.tick(now=t, push=_no_push)
    ended = _heartbeat()["ts"]
    daemon.tick(now=t + 400, push=_no_push)
    assert [r for r in _ledger() if r["type"] == "gap"] == [
        {"ts": t + 400, "type": "gap", "seconds": round(t + 400 - ended), "since": ended}]


def test_a_held_lock_skips_the_pass(fleet):
    paths.DAEMON.mkdir(parents=True, exist_ok=True)
    fd = os.open(paths.DAEMON / "tick.lock", os.O_WRONLY | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert daemon.tick(push=_no_push) == 0
    finally:
        os.close(fd)
    assert _heartbeat() is None


def test_a_failing_scan_is_ledgered_once_an_hour_and_the_others_still_run(fleet, monkeypatch):
    def boom(scope, sink):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(monitor, "observe_done", boom)
    _question("mgr", "q1", "w1")
    t = time.time()
    daemon.tick(now=t, push=_no_push)
    daemon.tick(now=t + 60, push=_no_push)
    daemon.tick(now=t + 3_700, push=_no_push)
    assert [r["scan"] for r in _ledger() if r["type"] == "error"] == ["done", "done"]
    assert ("QUESTION", "q1", "emit") in _events()
    assert _heartbeat()["errors"] == 1


@pytest.mark.parametrize("daemon_first", [True, False])
def test_an_advanced_done_is_journaled_once_in_either_order(fleet, monkeypatch, capsys, daemon_first):
    monkeypatch.setattr(monitor, "_advanced_node", lambda payload: "WORK-1")
    journal = paths.artifact_events_path("WORK-1")
    journal.parent.mkdir(parents=True)
    journal.write_text("")
    _done("mgr", "w1")
    runs = [lambda: daemon.tick(push=_no_push), lambda: monitor.run_done_scan(dict(MGR))]
    for run in (runs if daemon_first else runs[::-1]):
        run()
    lines = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [line["kind"] for line in lines if line.get("type") == "lane_event"] == ["DONE"]
    assert capsys.readouterr().out == ""
    assert [r["outcome"] for r in _ledger() if r["type"] == "event"] == ["silent"]


def test_a_done_older_than_start_keeps_its_outcome_and_is_never_journaled(fleet, monkeypatch):
    keys = {"w-old": "WORK-1", "w-new": "WORK-1", "w-bare": "WORK-2"}
    monkeypatch.setattr(monitor, "_advanced_node", lambda payload: keys[payload["claude_sid"]])
    journal = paths.artifact_events_path("WORK-1")
    journal.parent.mkdir(parents=True)
    journal.write_text("")
    start = time.time()
    daemon.ensure_start(start)
    entries = {}
    for sid, mtime in (("w-old", start - 100), ("w-new", start + 5), ("w-bare", start - 100)):
        entries[sid] = _done("mgr", sid)
        os.utime(entries[sid], (mtime, mtime))
    daemon.tick(now=start + 10, push=_no_push)
    lines = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [line["fields"]["event_id"] for line in lines if line.get("type") == "lane_event"] == ["w-new"]
    rows = {r["key"]: (r["outcome"], r["reason"], r["pre_start"]) for r in _ledger() if r["type"] == "event"}
    assert rows == {entries["w-old"].name: ("silent", "advanced", True),
                    entries["w-new"].name: ("silent", "advanced", False),
                    entries["w-bare"].name: ("emit", "done", True)}
    assert not paths.artifact_events_path("WORK-2").parent.exists()


def test_a_silent_finish_row_names_the_ladder_gate(fleet):
    _turn_end("mgr", "w2")
    _worker("w2")
    daemon.tick(push=_no_push)
    row = next(r for r in _ledger() if r.get("kind") == "FINISHED_SILENTLY")
    assert (row["outcome"], row["gate"]) == ("emit", monitor.FS_EMIT_RESET)


def test_an_unreadable_start_is_restored_from_the_ledger_and_reported(fleet):
    daemon.ensure_start(100.0)
    (paths.DAEMON / "start.json").write_text("")
    assert daemon.ensure_start(500.0) == 100.0
    assert daemon.ensure_start(900.0) == 100.0
    types = [r["type"] for r in _ledger()]
    assert types.count("start") == 1
    assert [r["scan"] for r in _ledger() if r["type"] == "error"] == ["start"]


def test_an_unreadable_start_with_no_ledger_line_starts_now(fleet):
    (paths.DAEMON / "start.json").write_text("{")
    assert daemon.ensure_start(500.0) == 500.0
    assert daemon.ensure_start(900.0) == 500.0
    assert [r["type"] for r in _ledger()].count("start") == 1


def test_the_heartbeat_is_stamped_when_the_pass_ends_so_a_long_pass_is_not_a_gap(fleet, monkeypatch):
    clock = [0.0]

    def slow_scan(scope, sink):
        clock[0] += 300

    monkeypatch.setattr(daemon.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(monitor, "observe_done", slow_scan)
    t = time.time()
    daemon.tick(now=t, push=_no_push)
    assert _heartbeat()["ts"] == t + 300
    daemon.tick(now=t + 400, push=_no_push)
    assert [r for r in _ledger() if r["type"] == "gap"] == []


def test_a_question_from_a_closed_subworker_is_ledgered_as_the_board_lists_it(fleet):
    state.write_json_atomic(paths.CLOSED / "sub-2.json", {
        "claude_sid": "sub-2", "agent": "worker", "kind": "subworker", "name": "sub-2",
        "parent_name": "parent-w"})
    _question("parent-w", "q-closed-sub", "sub-2")
    daemon.tick(push=_no_push)
    assert ("QUESTION", "q-closed-sub", "emit") in _events("parent-w")


def test_heartbeat_and_tick_line_describe_the_pass(fleet):
    monitor.run_questions_scan(dict(MGR))
    daemon.tick(push=_no_push)
    assert set(_heartbeat()) == {"ts", "pid", "duration_sec", "events", "pushes", "errors", "acting", "rounds"}
    tick = daemon.read_jsonl(paths.DAEMON / "ticks.jsonl")[-1]
    assert tick["trigger"] == "interval" and "lanes_fresh" not in tick
    assert all(isinstance(sec, float) for sec in tick["steps"].values())
    assert {"observe", "push"} <= set(tick["steps"]) and "finish" not in tick["steps"]
    assert "steps" not in _heartbeat()


def test_ticks_past_eight_days_are_dropped_once_the_first_is_nine_days_old(fleet):
    t = time.time()
    paths.DAEMON.mkdir(parents=True, exist_ok=True)
    path = paths.DAEMON / "ticks.jsonl"
    path.write_text(json.dumps({"ts": t - 10 * 86_400}) + "\n" + json.dumps({"ts": t - 7 * 86_400}) + "\n")
    daemon.tick(now=t, push=_no_push)
    assert [round(r["ts"]) for r in daemon.read_jsonl(path)] == [round(t - 7 * 86_400), round(t)]


def test_a_corrupt_first_tick_line_does_not_stop_the_prune(fleet):
    t = time.time()
    path = paths.DAEMON / "ticks.jsonl"
    path.write_text("{\"ts\": 1\n" + json.dumps({"ts": t - 10 * 86_400}) + "\n"
                    + json.dumps({"ts": t - 7 * 86_400}) + "\n")
    daemon.tick(now=t, push=_no_push)
    assert [round(r["ts"]) for r in daemon.read_jsonl(path)] == [round(t - 7 * 86_400), round(t)]


def test_the_cli_runs_one_pass(fleet, monkeypatch):
    monkeypatch.setattr(daemon, "_push", _no_push)
    assert daemon.main(["tick"]) == 0
    assert _heartbeat() is not None
    assert daemon.main([]) == 2
    assert daemon.main(["tock"]) == 2


def test_the_top_level_cli_dispatches_daemon(fleet, monkeypatch):
    calls = []
    monkeypatch.setattr(daemon, "main", lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(sys, "argv", ["dockwright", "daemon", "tick"])
    from dockwright.__main__ import main as dispatcher_main
    with pytest.raises(SystemExit) as exit_info:
        dispatcher_main()
    assert exit_info.value.code == 0 and calls == [["tick"]]


def test_wake_touches_the_wake_file_and_swallows_errors(fleet, monkeypatch):
    assert not paths.DAEMON_WAKE.exists()
    daemon.wake()
    first = paths.DAEMON_WAKE.stat().st_mtime
    time.sleep(0.01)
    daemon.wake()
    assert paths.DAEMON_WAKE.stat().st_mtime >= first
    monkeypatch.setattr(paths, "DAEMON_WAKE", fleet / "nope" / "wake")
    daemon.wake()


def test_observe_runs_the_three_scans_and_writes_a_wake_tick(fleet):
    _done("mgr", "w1")
    _question("mgr", "q1", "w3")
    assert daemon.observe() == 0
    assert {k for k, _, _ in _events("mgr")} == {"DONE", "QUESTION"}
    tick = daemon.read_jsonl(paths.DAEMON / "ticks.jsonl")[-1]
    assert tick["trigger"] == "wake" and tick["rounds"] == 1
    beat = state.read_json(paths.DAEMON / "wake-heartbeat.json")
    assert set(beat) == {"ts", "pid", "duration_sec", "events", "rounds"}


def test_observe_repeats_while_the_wake_file_is_touched_during_the_round_at_most_three_times(fleet, monkeypatch):
    rounds = []

    def scan(scope, sink):
        rounds.append(1)
        _touch_wake_now()

    monkeypatch.setattr(monitor, "observe_done", scan)
    monkeypatch.setattr(monitor, "observe_turn_ends", lambda s, k: None)
    monkeypatch.setattr(monitor, "observe_questions", lambda s, k: None)
    assert daemon.observe() == 0
    assert len(rounds) == 3
    assert daemon.read_jsonl(paths.DAEMON / "ticks.jsonl")[-1]["rounds"] == 3


def test_observe_stamps_each_lane_it_scanned_and_keeps_a_failed_lanes_last_stamp(fleet, monkeypatch):
    lane_io.remember_observed({"questions": 1.0})

    def boom(scope, sink):
        raise RuntimeError("boom")

    monkeypatch.setattr(monitor, "observe_done", lambda s, k: None)
    monkeypatch.setattr(monitor, "observe_turn_ends", lambda s, k: None)
    monkeypatch.setattr(monitor, "observe_questions", boom)
    before = time.time()
    assert daemon.observe() == 0
    assert lane_io.observed_at("done") >= before and lane_io.observed_at("turn-ends") >= before
    assert lane_io.observed_at("questions") == 1.0


def test_observe_exits_zero_on_the_stop_file_and_on_a_held_observe_lock(fleet):
    paths.DAEMON_STOP.write_text("")
    _done("mgr", "w1")
    assert daemon.observe() == 0 and _events() == set()
    paths.DAEMON_STOP.unlink()
    fd = os.open(paths.DAEMON / "observe.lock", os.O_WRONLY | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert daemon.observe() == 0 and _events() == set()
    finally:
        os.close(fd)


def test_the_daemon_writes_pages_to_every_managers_outbox_only_when_lanes_run_in_the_daemon(fleet, monkeypatch):
    _done("mgr", "w1")
    _question("mgr", "q1", "w3")
    daemon.tick(push=_no_push)
    assert _outbox("mgr") == []
    _flag(fleet, monkeypatch, "false")
    _done("mgr", "w2")
    _question("mgr", "q2", "w4")
    _done("ghost", "w9")
    _done(paths.UNSCOPED_BUCKET, "w8")
    _done("daemon", "w7")
    _no_actions(monkeypatch)
    daemon.tick(push=_no_push)
    lines = [e["line"] for e in _outbox("mgr")]
    assert len(lines) == 2 and lines[0].startswith("DONE w2") and lines[1].startswith("QUESTION w4")
    assert all(e["source"] == "daemon" for e in _outbox("mgr"))
    assert [e["line"].split(" ")[1] for e in _outbox("ghost")] == ["w9"]
    assert _outbox(paths.UNSCOPED_BUCKET) == [] and _outbox("daemon") == []
    assert ("DONE", next(iter(paths.DONE.glob("daemon/*.json"))).name, monitor.OBSERVE_EMIT) in _events("daemon")


def test_a_limited_managers_bucket_is_not_judged_and_its_stems_stay_in_the_bookmarks(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    _no_actions(monkeypatch)
    _question("mgr", "q1", "w3")
    daemon.tick(push=_no_push)
    assert len(_outbox("mgr")) == 1
    (fleet / ".manager-limited-mgr").write_text("")
    _question("mgr", "q2", "w4")
    daemon.tick(push=_no_push)
    assert len(_outbox("mgr")) == 1 and "q2" not in (paths.DAEMON / "seen-questions").read_text()
    assert "q1" in (paths.DAEMON / "seen-questions").read_text()
    (fleet / ".manager-limited-mgr").unlink()
    daemon.tick(push=_no_push)
    assert [e["line"].split(" ")[1] for e in _outbox("mgr")] == ["w3", "w4"]


def test_tick_stamps_alive_before_observing_and_between_action_steps(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    stamps = []
    real = daemon.alive_stamp

    def stamp(now):
        stamps.append(state.read_json(paths.DAEMON / "alive.json") is not None)
        return real(now)

    monkeypatch.setattr(daemon, "alive_stamp", stamp)
    monkeypatch.setattr(daemon, "_managers", lambda: [])
    monkeypatch.setattr(daemon, "_reconcile_daemon_works", lambda now, emit, task_keys=None: None)
    monkeypatch.setattr(daemon, "_run_notes", lambda now, managers: None)
    monkeypatch.setattr(daemon, "_run_schedules", lambda now: [])
    monkeypatch.setattr(daemon, "_pages_silent", lambda now, managers: [])
    daemon.tick(push=_no_push)
    assert stamps[0] is False and len(stamps) >= 4
    beat = state.read_json(paths.DAEMON / "alive.json")
    assert set(beat) == {"ts", "awake", "pid", "pass_at", "step", "step_awake"} and beat["ts"] >= beat["pass_at"]
    assert isinstance(beat["step"], str) and isinstance(beat["step_awake"], float)
    assert state.read_json(paths.DAEMON / "heartbeat.json")["acting"] is True


def test_the_acting_pass_calls_stale_and_reconcile_per_live_manager_with_an_outbox_emit(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    state.write_json_atomic(paths.ACTIVE / "m2.json", {"claude_sid": "m2", "agent": "manager", "name": "two",
                                                      "pid": os.getpid(), "domain": "general"})
    state.write_json_atomic(paths.ACTIVE / "dead.json", {"claude_sid": "dead", "agent": "manager", "name": "dead",
                                                        "pid": 2_000_000_000})
    calls = []

    def fake_stale(name, emit=None, lanes_in_manager=True):
        calls.append(("stale", name, lanes_in_manager))
        emit(f"STALE_PROCESSING {name}-w (30min) → x")
        return 0

    def fake_reconcile(mgr, now=None, run_gh=None, emit=None, task_keys=None):
        calls.append(("reconcile", mgr["name"], mgr["sid"]))
        emit(f"ADVANCE_STUCK K-{mgr['name']} task x → y")
        return []

    notes = []
    monkeypatch.setattr("dockwright.stale_monitor.main", fake_stale)
    monkeypatch.setattr("dockwright.pipeline_reconcile.reconcile", fake_reconcile)
    monkeypatch.setattr("dockwright.note_check.run", lambda: notes.append(1) or [])
    monkeypatch.setattr(daemon, "_run_schedules", lambda now: [])
    daemon.tick(push=_no_push)
    assert ("stale", "mgr", False) in calls and ("stale", "two", False) in calls
    assert ("reconcile", "mgr", "mgr-sid") in calls and ("reconcile", "two", "m2") in calls
    assert ("reconcile", "daemon", "daemon") in calls
    assert not any(c[1] == "dead" for c in calls)
    assert notes == [1]
    assert [e["line"].split(" ")[0] for e in _outbox("mgr")] == ["STALE_PROCESSING", "ADVANCE_STUCK"]
    assert [e["line"].split(" ")[0] for e in _outbox("two")] == ["STALE_PROCESSING", "ADVANCE_STUCK"]
    daemon_lines = [r["line"].split(" ")[0] for r in _ledger() if r["type"] == "line" and r["bucket"] == "daemon"]
    assert daemon_lines == ["STALE_PROCESSING", "ADVANCE_STUCK"]


def test_a_failing_manager_step_does_not_stop_the_next_one(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    state.write_json_atomic(paths.ACTIVE / "m2.json", {"claude_sid": "m2", "agent": "manager", "name": "two",
                                                      "pid": os.getpid(), "domain": "general"})
    seen = []

    def fake_stale(name, emit=None, lanes_in_manager=True):
        seen.append(name)
        if name == "mgr":
            raise RuntimeError("boom")
        return 0

    monkeypatch.setattr("dockwright.stale_monitor.main", fake_stale)
    monkeypatch.setattr("dockwright.pipeline_reconcile.reconcile", lambda mgr, now=None, run_gh=None, emit=None, task_keys=None: [])
    monkeypatch.setattr("dockwright.note_check.run", lambda: [])
    monkeypatch.setattr(daemon, "_run_schedules", lambda now: [])
    daemon.tick(push=_no_push)
    assert seen == ["mgr", "two", "daemon"]
    errors = [r for r in _ledger() if r["type"] == "error"]
    assert [e["scan"] for e in errors] == ["stale:mgr"]


def test_a_note_check_failure_pages_every_live_manager_once_and_is_ledgered(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    monkeypatch.setattr("dockwright.stale_monitor.main", lambda name, emit=None, lanes_in_manager=True: 0)
    monkeypatch.setattr("dockwright.pipeline_reconcile.reconcile", lambda mgr, now=None, run_gh=None, emit=None, task_keys=None: [])
    monkeypatch.setattr("dockwright.note_check.run", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(daemon, "_run_schedules", lambda now: [])
    daemon.tick(push=_no_push)
    daemon.tick(push=_no_push)
    lines = [e["line"] for e in _outbox("mgr")]
    assert len(lines) == 1 and lines[0].startswith("RECONCILE_ERROR *") and "note-check RuntimeError: boom" in lines[0]
    assert [r["scan"] for r in _ledger() if r["type"] == "error"] == ["notes"]


def test_a_limited_manager_is_skipped_by_reconcile_but_not_by_stale(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    (fleet / ".manager-limited-mgr").write_text("")
    calls = []
    monkeypatch.setattr("dockwright.stale_monitor.main",
                        lambda name, emit=None, lanes_in_manager=True: calls.append(("stale", name)) or 0)
    monkeypatch.setattr("dockwright.pipeline_reconcile.reconcile",
                        lambda mgr, now=None, run_gh=None, emit=None, task_keys=None: calls.append(("reconcile", mgr["name"])) or [])
    monkeypatch.setattr("dockwright.note_check.run", lambda: [])
    monkeypatch.setattr(daemon, "_run_schedules", lambda now: [])
    daemon.tick(push=_no_push)
    assert ("stale", "mgr") in calls and ("reconcile", "mgr") not in calls and ("reconcile", "daemon") in calls


def test_the_acting_pass_is_skipped_while_lanes_run_in_the_tab(fleet, monkeypatch):
    monkeypatch.setattr("dockwright.stale_monitor.main",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("acted")))
    daemon.tick(push=_no_push)
    assert state.read_json(paths.DAEMON / "heartbeat.json")["acting"] is False


def test_a_silent_pages_monitor_of_a_live_manager_is_notified_on_a_ladder_within_the_cap(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false", "[daemon]\npush_cap_per_hour = 2\n")
    sent = []
    monkeypatch.setattr("dockwright.notify.macos", lambda title, text: sent.append(text) or 0)
    for step in ("_run_stale", "_run_reconcile", "_reconcile_daemon_works", "_run_notes"):
        monkeypatch.setattr(daemon, step, lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_run_schedules", lambda now: [])
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 5000.0)
    daemon.tick(push=_no_push)
    assert sent == []
    state.write_json_atomic(lane_io.heartbeat_path("mgr", "pages"), {"last_scan": 1.0, "awake": 4000.0})
    daemon.tick(push=_no_push)
    assert sent == ["mgr: pages monitor silent 16 min"]
    assert any(r["type"] == "pages_silent" and r["manager"] == "mgr" for r in _ledger())
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 5000.0 + 9 * 60)
    daemon.tick(push=_no_push)
    assert len(sent) == 1
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 5000.0 + 11 * 60)
    daemon.tick(push=_no_push)
    assert len(sent) == 2


def test_observe_completes_while_tick_sits_in_an_action_step(fleet, monkeypatch):
    import threading
    _flag(fleet, monkeypatch, "false")
    entered, release = threading.Event(), threading.Event()

    def slow_stale(name, emit=None, lanes_in_manager=True):
        entered.set()
        release.wait(5)
        return 0

    monkeypatch.setattr("dockwright.stale_monitor.main", slow_stale)
    monkeypatch.setattr("dockwright.pipeline_reconcile.reconcile", lambda mgr, now=None, run_gh=None, emit=None, task_keys=None: [])
    monkeypatch.setattr("dockwright.note_check.run", lambda: [])
    monkeypatch.setattr(daemon, "_run_schedules", lambda now: [])
    worker = threading.Thread(target=daemon.tick, kwargs={"push": _no_push})
    worker.start()
    assert entered.wait(5)
    _done("mgr", "w1")
    started = time.monotonic()
    assert daemon.observe() == 0
    assert time.monotonic() - started < 2
    assert ("DONE", next(iter(paths.DONE.glob("mgr/*.json"))).name, monitor.OBSERVE_EMIT) in _events("mgr")
    release.set()
    worker.join(5)


def test_a_trim_and_the_append_run_in_one_critical_section_and_a_held_lock_skips_the_write(fleet, monkeypatch,
                                                                                            capsys):
    now = time.time()
    for i in range(3):
        daemon.append_jsonl(paths.DAEMON / "ticks.jsonl", {"ts": now - 10 * 24 * 3600 + i, "pid": 1})
    daemon._append_tick({"ts": now, "pid": 1, "trigger": "interval"}, now)
    assert [r["ts"] for r in daemon.read_jsonl(paths.DAEMON / "ticks.jsonl")] == [now]
    monkeypatch.setattr(daemon, "STATE_LOCK_ATTEMPTS", 1)
    with state.file_lock_nb(paths.DAEMON / "state.lock"):
        daemon._append_tick({"ts": now + 1, "pid": 1, "trigger": "wake"}, now + 1)
    assert [r["ts"] for r in daemon.read_jsonl(paths.DAEMON / "ticks.jsonl")] == [now]
    assert "state.lock busy" in capsys.readouterr().err


def test_the_ticks_and_errors_files_are_written_under_the_state_lock(fleet, monkeypatch):
    held = []
    real = state.file_lock_nb

    def spy(path):
        held.append(path.name)
        return real(path)

    monkeypatch.setattr(daemon.state, "file_lock_nb", spy)
    daemon.tick(push=_no_push)
    assert "state.lock" in held


def test_the_shadow_report_is_a_stub_that_exits_two(fleet, capsys):
    assert daemon.main(["shadow-report", "--since", "1"]) == 2
    assert "shadow-report was removed after the shadow week" in capsys.readouterr().err


def test_the_stale_step_waits_until_the_daemon_runs_with_the_lanes_env(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false", '[lanes.env]\nCLAUDE_ORCH_IDLE_TTL_HOURS = "8"\n')
    monkeypatch.delenv("CLAUDE_ORCH_IDLE_TTL_HOURS", raising=False)
    calls = []
    monkeypatch.setattr("dockwright.stale_monitor.main",
                        lambda name, emit=None, lanes_in_manager=True: calls.append(name) or 0)
    monkeypatch.setattr("dockwright.pipeline_reconcile.reconcile", lambda mgr, now=None, run_gh=None, emit=None, task_keys=None: [])
    monkeypatch.setattr("dockwright.note_check.run", lambda: [])
    monkeypatch.setattr(daemon, "_run_schedules", lambda now: [])
    daemon.tick(push=_no_push)
    assert calls == []
    lines = [e["line"] for e in _outbox("mgr")]
    assert len(lines) == 1 and lines[0].startswith("RECONCILE_ERROR *") and "CLAUDE_ORCH_IDLE_TTL_HOURS" in lines[0]
    assert [r["scan"] for r in _ledger() if r["type"] == "error"] == ["env"]
    monkeypatch.setenv("CLAUDE_ORCH_IDLE_TTL_HOURS", "8")
    daemon.tick(push=_no_push)
    assert calls == ["mgr", "daemon"]


def test_a_pages_heartbeat_from_before_a_reboot_counts_as_silent_since_boot(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    sent = []
    monkeypatch.setattr("dockwright.notify.macos", lambda title, text: sent.append(text) or 0)
    for step in ("_run_stale", "_run_reconcile", "_reconcile_daemon_works", "_run_notes"):
        monkeypatch.setattr(daemon, step, lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_run_schedules", lambda now: [])
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: 400.0)
    state.write_json_atomic(lane_io.heartbeat_path("mgr", "pages"), {"last_scan": 1.0, "awake": 90_000.0})
    daemon.tick(push=_no_push)
    assert sent == ["mgr: pages monitor silent 6 min"]


def test_a_pages_heartbeat_written_after_the_clock_sample_is_not_silent(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    sent = []
    monkeypatch.setattr("dockwright.notify.macos", lambda title, text: sent.append(text) or 0)
    for step in ("_run_stale", "_run_reconcile", "_reconcile_daemon_works", "_run_notes"):
        monkeypatch.setattr(daemon, step, lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_run_schedules", lambda now: [])
    clock = [1_152_460.0]
    monkeypatch.setattr(lane_io, "awake_seconds", lambda: clock[0])
    pages = lane_io.heartbeat_path("mgr", "pages")
    real_read = state.read_json

    def read_after_a_fresh_beat(path, *args, **kwargs):
        if path == pages:
            clock[0] += 0.004
            state.write_json_atomic(pages, {"last_scan": time.time(), "awake": clock[0]})
            clock[0] += 0.004
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(state, "read_json", read_after_a_fresh_beat)
    daemon.tick(push=_no_push)
    assert sent == []


def test_a_live_manager_that_never_wrote_a_pages_heartbeat_is_notified_after_the_grace(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    sent = []
    monkeypatch.setattr("dockwright.notify.macos", lambda title, text: sent.append(text) or 0)
    for step in ("_run_stale", "_run_reconcile", "_reconcile_daemon_works", "_run_notes"):
        monkeypatch.setattr(daemon, step, lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_run_schedules", lambda now: [])
    record = state.read_json(paths.ACTIVE / "mgr-sid.json")
    state.write_json_atomic(paths.ACTIVE / "mgr-sid.json", {**record, "started_at": time.time() - 60})
    daemon.tick(push=_no_push)
    assert sent == []
    state.write_json_atomic(paths.ACTIVE / "mgr-sid.json", {**record, "started_at": time.time() - 400})
    daemon.tick(push=_no_push)
    assert sent == ["mgr: pages monitor silent 6 min"]


def test_a_schedule_that_cannot_start_or_judge_its_last_work_pages_every_live_manager(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    for step in ("_run_stale", "_run_reconcile", "_reconcile_daemon_works", "_run_notes"):
        monkeypatch.setattr(daemon, step, lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_run_schedules", lambda now: [
        {"type": "schedule_skipped", "name": "q", "slot": 1.0, "reason": "unreadable", "task_key": "q-1",
         "detail": "ValueError: bad line"},
        {"type": "schedule_failed", "name": "r", "slot": 1.0, "reason": "no manager"},
        {"type": "schedule_started", "name": "s", "slot": 1.0, "task_key": "s-1"}])
    daemon.tick(push=_no_push)
    lines = [e["line"] for e in _outbox("mgr")]
    assert len(lines) == 2 and all(line.startswith("RECONCILE_ERROR *") for line in lines)
    assert "schedule q" in lines[0] and "unreadable" in lines[0] and "schedule r" in lines[1]
    assert [r["type"] for r in _ledger() if r["type"].startswith("schedule_")] == [
        "schedule_skipped", "schedule_failed", "schedule_started"]


def test_the_daemon_keeps_acting_and_paging_while_the_config_does_not_parse(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    monkeypatch.setattr("dockwright.stale_monitor.main", lambda name, emit=None, lanes_in_manager=True: 0)
    monkeypatch.setattr("dockwright.pipeline_reconcile.reconcile", lambda mgr, now=None, run_gh=None, emit=None, task_keys=None: [])
    monkeypatch.setattr("dockwright.note_check.run", lambda: [])
    monkeypatch.setattr(daemon, "_run_schedules", lambda now: [])
    daemon.tick(push=_no_push)
    assert state.read_json(paths.DAEMON / "mode.json")["in_manager"] is False
    (fleet / "dockwright.toml").write_text("[lanes]\nin_manager = false\n[gh.logins]\nacme = alice-unquoted\n")
    _question("mgr", "q1", "w3")
    assert daemon.observe() == 0
    assert [e["line"].split(" ")[1] for e in _outbox("mgr")] == ["w3"]
    daemon.tick(push=_no_push)
    assert state.read_json(paths.DAEMON / "heartbeat.json")["acting"] is True


def test_the_pass_repeats_its_observe_while_the_wake_file_is_touched(fleet, monkeypatch):
    rounds = []

    def scan(scope, sink):
        rounds.append(1)
        _touch_wake_now()

    monkeypatch.setattr(monitor, "observe_done", scan)
    monkeypatch.setattr(monitor, "observe_turn_ends", lambda s, k: None)
    monkeypatch.setattr(monitor, "observe_questions", lambda s, k: None)
    daemon.tick(push=_no_push)
    assert len(rounds) == 3 and _heartbeat()["rounds"] == 3


def test_the_acting_pass_runs_the_stale_checks_for_the_daemons_own_workers(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    calls = []
    monkeypatch.setattr("dockwright.stale_monitor.main",
                        lambda name, emit=None, lanes_in_manager=True: calls.append((name, emit("x") or "emitted")) or 0)
    monkeypatch.setattr("dockwright.pipeline_reconcile.reconcile", lambda mgr, now=None, run_gh=None, emit=None, task_keys=None: [])
    monkeypatch.setattr("dockwright.note_check.run", lambda: [])
    monkeypatch.setattr(daemon, "_run_schedules", lambda now: [])
    daemon.tick(push=_no_push)
    assert ("mgr", "emitted") in calls and ("daemon", "emitted") in calls
    assert [r["line"] for r in _ledger() if r["type"] == "line" and r["bucket"] == "daemon"] == ["x"]
    assert _outbox("daemon") == []


def test_the_acting_pass_runs_the_events_step_before_reconcile_and_routes_its_pages(fleet, monkeypatch):
    from dockwright import event_poll
    _flag(fleet, monkeypatch, "false")
    state.write_json_atomic(paths.ACTIVE / "m2.json", {"claude_sid": "m2", "agent": "manager", "name": "two",
                                                      "pid": os.getpid(), "domain": "general"})
    order = []

    def fake_step(now, managers, emit, record=None):
        order.append(("events", sorted(m["name"] for m in managers)))
        emit("EVENT_STARTED K sprint → x", "two")
        emit("EVENT_POLL_FAILED tracker → y", None)
        polled = [{"type": "events_polled", "source": "tracker", "n": 1}]
        for r in polled:
            record(r)
        return polled

    def fake_reconcile(mgr, now=None, run_gh=None, emit=None, task_keys=None):
        order.append(("reconcile", mgr["name"]))
        return []

    monkeypatch.setattr(event_poll, "step", fake_step)
    monkeypatch.setattr("dockwright.stale_monitor.main", lambda name, emit=None, lanes_in_manager=True: 0)
    monkeypatch.setattr("dockwright.pipeline_reconcile.reconcile", fake_reconcile)
    monkeypatch.setattr("dockwright.note_check.run", lambda: [])
    monkeypatch.setattr(daemon, "_run_schedules", lambda now: [])
    daemon.tick(push=_no_push)
    assert order[0] == ("events", ["mgr", "two"]) and ("reconcile", "mgr") in order and ("reconcile", "two") in order
    assert [e["line"] for e in _outbox("two")] == ["EVENT_STARTED K sprint → x", "EVENT_POLL_FAILED tracker → y"]
    assert [e["line"] for e in _outbox("mgr")] == ["EVENT_POLL_FAILED tracker → y"]
    assert [r for r in _ledger() if r["type"] == "events_polled"] == [
        {"ts": r["ts"], "type": "events_polled", "source": "tracker", "n": 1} for r in _ledger() if r["type"] == "events_polled"]
    assert len([r for r in _ledger() if r["type"] == "events_polled"]) == 1

    def boom(now, managers, emit, record=None):
        raise RuntimeError("poll exploded")

    monkeypatch.setattr(event_poll, "step", boom)
    order.clear()
    daemon.tick(push=_no_push)
    assert ("reconcile", "mgr") in order
    assert state.read_json(paths.DAEMON / "heartbeat.json")["errors"] >= 1


def _quiet_steps(monkeypatch):
    monkeypatch.setattr(daemon, "_managers", lambda: [])
    monkeypatch.setattr(daemon, "_run_stale_daemon", lambda now: None)
    monkeypatch.setattr(daemon, "_run_events", lambda now, managers: None)
    monkeypatch.setattr(daemon, "_reconcile_daemon_works", lambda now, emit, task_keys=None: None)
    monkeypatch.setattr(daemon, "_run_notes", lambda now, managers: None)
    monkeypatch.setattr(daemon, "_run_schedules", lambda now: [])
    monkeypatch.setattr(daemon, "_pages_silent", lambda now, managers: [])


def _alive():
    return state.read_json(paths.DAEMON / "alive.json") or {}


def _wait_for(check, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = check()
        if got:
            return got
        time.sleep(0.005)
    return None


def test_the_beat_thread_stamps_alive_during_a_blocked_step(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    _quiet_steps(monkeypatch)
    monkeypatch.setattr(daemon, "ALIVE_BEAT_SEC", 0.02)
    seen = []

    def notes(now, managers):
        first = _wait_for(lambda: _alive().get("ts") if _alive().get("step") == "notes" else None)
        if first is None:
            return
        seen.append(first)
        later = _wait_for(lambda: _alive().get("ts") if _alive().get("ts", 0) > first else None)
        if later is not None:
            seen.append(later)

    monkeypatch.setattr(daemon, "_run_notes", notes)
    daemon.tick(push=_no_push)
    assert len(seen) == 2 and seen[1] > seen[0]


def test_a_tick_waiting_on_the_observe_lock_stamps_the_observe_step(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    _quiet_steps(monkeypatch)
    monkeypatch.setattr(daemon, "ALIVE_BEAT_SEC", 0.02)
    held = daemon._try_lock("observe.lock", blocking=True)
    worker = threading.Thread(target=daemon.tick, kwargs={"push": _no_push})
    worker.start()
    try:
        first = _wait_for(lambda: _alive() if _alive().get("step") == "observe" else None)
        assert first is not None and isinstance(first["step_awake"], float)
        later = _wait_for(lambda: _alive() if _alive().get("ts", 0) > first["ts"] else None)
        assert later is not None and later["step"] == "observe" and worker.is_alive()
    finally:
        os.close(held)
        worker.join(5)
    assert not worker.is_alive() and _heartbeat() is not None


def test_the_beat_thread_and_the_memo_end_with_a_pass_that_raised(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    _quiet_steps(monkeypatch)
    record = paths.ASSIGNMENTS / "s1.json"
    state.write_json_atomic(record, {"v": 1})
    monkeypatch.setattr(daemon, "_run_notes", lambda now, managers: list(state.list_json_in(paths.ASSIGNMENTS)))
    real = state.write_json_atomic

    def write(path, data):
        if Path(path).name == "heartbeat.json":
            raise OSError("disk full")
        return real(path, data)

    monkeypatch.setattr(state, "write_json_atomic", write)
    with pytest.raises(OSError):
        daemon.tick(push=_no_push)
    assert not any(t.name == "daemon-alive-beat" and t.is_alive() for t in threading.enumerate())
    record.write_text(json.dumps({"v": 2}))
    assert list(state.list_json_in(paths.ASSIGNMENTS)) == [{"v": 2}]
    from dockwright import stale_monitor
    assert stale_monitor._DIR_MEMO is None


def test_a_second_pass_in_one_process_starts_its_steps_clean(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    _quiet_steps(monkeypatch)
    daemon.tick(push=_no_push)
    daemon.tick(push=_no_push)
    steps = daemon.read_jsonl(paths.DAEMON / "ticks.jsonl")[-1]["steps"]
    assert "finish" not in steps and "observe" in steps


def test_a_failed_beat_does_not_stop_the_thread_or_the_pass(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    _quiet_steps(monkeypatch)
    monkeypatch.setattr(daemon, "ALIVE_BEAT_SEC", 0.02)
    real = daemon.alive_stamp
    off_main = []

    def stamp(now):
        if threading.current_thread() is not threading.main_thread():
            off_main.append(1)
            if len(off_main) <= 2:
                raise OSError("no space")
        return real(now)

    monkeypatch.setattr(daemon, "alive_stamp", stamp)
    monkeypatch.setattr(daemon, "_run_notes", lambda now, managers: _wait_for(lambda: len(off_main) >= 3))
    assert daemon.tick(push=_no_push) == 0
    assert len(off_main) >= 3 and _heartbeat() is not None


def test_push_is_due_by_the_current_time(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    _quiet_steps(monkeypatch)
    monkeypatch.setattr(daemon, "_run_notes", lambda now, managers: time.sleep(0.2))
    seen = []

    def push(now, start):
        seen.append(now)
        return []

    daemon.tick(now=1000.0, push=push)
    assert 1000.2 <= seen[0] < 1005.0


def test_the_pass_reads_the_assignments_directory_from_disk_once(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    _quiet_steps(monkeypatch)
    for sid in ("s1", "s2", "s3"):
        state.write_json_atomic(paths.ASSIGNMENTS / f"{sid}.json", {"claude_sid": sid})
    reads, sizes = [], []
    real = Path.read_text

    def read_text(path, *args, **kwargs):
        if paths.ASSIGNMENTS in Path(path).parents:
            reads.append(Path(path).name)
        return real(path, *args, **kwargs)

    def scan(*args, **kwargs):
        sizes.append(len(list(state.list_json_in(paths.ASSIGNMENTS))))

    monkeypatch.setattr(Path, "read_text", read_text)
    for step in ("_run_stale_daemon", "_run_events", "_reconcile_daemon_works", "_run_notes"):
        monkeypatch.setattr(daemon, step, scan)
    daemon.tick(push=_no_push)
    assert sizes == [3, 3, 3, 3] and sorted(reads) == ["s1.json", "s2.json", "s3.json"]
    from dockwright import stale_monitor
    assert stale_monitor._DIR_MEMO is None


def _two_managers_and_stubbed_steps(monkeypatch, calls):
    state.write_json_atomic(paths.ACTIVE / "m2.json", {"claude_sid": "m2", "agent": "manager", "name": "two",
                                                      "pid": os.getpid(), "domain": "general"})

    def fake_reconcile(mgr, now=None, run_gh=None, emit=None, task_keys=None):
        calls.append((mgr["name"], task_keys))
        return []

    monkeypatch.setattr("dockwright.stale_monitor.main", lambda name, emit=None, lanes_in_manager=True: 0)
    monkeypatch.setattr("dockwright.pipeline_reconcile.reconcile", fake_reconcile)
    monkeypatch.setattr("dockwright.note_check.run", lambda: [])
    monkeypatch.setattr(daemon, "_run_events", lambda now, managers: None)
    monkeypatch.setattr(daemon, "_run_schedules", lambda now: [])


def test_the_pass_scans_artifacts_once_and_hands_each_owner_its_works(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    calls, scans = [], []
    _two_managers_and_stubbed_steps(monkeypatch, calls)

    def sight():
        scans.append(1)
        return {"mgr": ["A"], "two": ["B"], "daemon": ["D"], None: ["X"]}

    monkeypatch.setattr("dockwright.pipeline_reconcile.sight", sight)
    daemon.tick(push=_no_push)
    assert scans == [1]
    assert sorted(calls) == [("daemon", ["D", "X"]), ("mgr", ["A", "X"]), ("two", ["B", "X"])]


def test_the_tick_line_names_every_acting_step(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    monkeypatch.setattr("dockwright.stale_monitor.main", lambda name, emit=None, lanes_in_manager=True: 0)
    monkeypatch.setattr("dockwright.pipeline_reconcile.sight", lambda: {})
    monkeypatch.setattr("dockwright.pipeline_reconcile.reconcile",
                        lambda mgr, now=None, run_gh=None, emit=None, task_keys=None: [])
    monkeypatch.setattr("dockwright.note_check.run", lambda: [])
    monkeypatch.setattr(daemon, "_run_events", lambda now, managers: None)
    monkeypatch.setattr(daemon, "_run_schedules", lambda now: [])
    daemon.tick(push=_no_push)
    tick = daemon.read_jsonl(paths.DAEMON / "ticks.jsonl")[-1]
    assert set(tick["steps"]) == {"observe", "stale:mgr", "stale:daemon", "events", "reconcile:scan", "reconcile:mgr",
                                  "reconcile:daemon", "notes", "schedules", "pages", "push"}


def test_a_failed_scan_falls_back_to_a_reconcile_that_scans(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    calls = []
    _two_managers_and_stubbed_steps(monkeypatch, calls)

    def sight():
        raise RuntimeError("artifacts unreadable")

    monkeypatch.setattr("dockwright.pipeline_reconcile.sight", sight)
    daemon.tick(push=_no_push)
    assert sorted(calls) == [("daemon", None), ("mgr", None), ("two", None)]
    assert _heartbeat()["errors"] >= 1
    assert [r["scan"] for r in _ledger() if r["type"] == "error"] == ["reconcile:scan"]


def test_a_stamp_names_the_step_that_runs_and_never_the_one_that_ended(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    _quiet_steps(monkeypatch)
    seen = {}
    monkeypatch.setattr(daemon, "_run_notes", lambda now, managers: seen.update(notes=_alive().get("step")))

    def push(now, start):
        seen["push"] = _alive().get("step")
        return []

    daemon.tick(push=push)
    assert seen == {"notes": "notes", "push": "push"}


def test_a_stale_monitor_that_does_not_import_fails_its_steps_and_not_the_pass(fleet, monkeypatch):
    import dockwright
    _flag(fleet, monkeypatch, "false")
    _quiet_steps(monkeypatch)
    monkeypatch.setattr(daemon, "_run_stale_daemon", lambda now: __import__("dockwright.stale_monitor"))
    monkeypatch.delattr(dockwright, "stale_monitor", raising=False)
    monkeypatch.setitem(sys.modules, "dockwright.stale_monitor", None)
    assert daemon.tick(push=_no_push) == 0
    assert _heartbeat()["errors"] == 1
    assert [r["scan"] for r in _ledger() if r["type"] == "error"] == ["stale:daemon"]
    assert "push" in daemon.read_jsonl(paths.DAEMON / "ticks.jsonl")[-1]["steps"]


def test_the_stamp_left_between_passes_does_not_name_the_push_step(fleet, monkeypatch):
    _flag(fleet, monkeypatch, "false")
    _quiet_steps(monkeypatch)
    daemon.tick(push=_no_push)
    assert _alive()["step"] == "finish"
