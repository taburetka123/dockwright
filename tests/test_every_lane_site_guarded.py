import json
import sys
import time

import pytest

from dockwright import lane_io, monitor, paths, stale_monitor, state


class _DeadStdout:
    def write(self, _text):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")


@pytest.fixture
def lane_state(tmp_path, monkeypatch):
    for attr, sub in (("ROOT", ""), ("ACTIVE", "active"), ("DONE", "done"),
                      ("QUESTIONS", "questions"), ("TURN_ENDS", "turn-ends"),
                      ("LANE_HEALTH", "lane-health")):
        monkeypatch.setattr(paths, attr, tmp_path / sub if sub else tmp_path)
    monkeypatch.setattr(stale_monitor, "ROOT", tmp_path)
    monkeypatch.setattr(lane_io, "reader_is_dead", lambda fd=1: False)
    return tmp_path


def _kill_stdout(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _DeadStdout())


MGR = {"name": "mgr", "sid": "mgr-sid"}


def test_turn_ends_commits_neither_cursor_nor_ladder_to_a_dead_reader(
        lane_state, monkeypatch):
    bucket = lane_state / "turn-ends" / "mgr"
    bucket.mkdir(parents=True)
    state.write_json_atomic(bucket / "w1-1.json", {
        "sid": "w1", "name": "w1", "completed_at": time.time() - 100_000,
        "last_summary": "done-ish"})

    _kill_stdout(monkeypatch)
    with pytest.raises(lane_io.LaneDead):
        monitor.run_turn_ends_scan(dict(MGR))

    cursor = lane_state / ".seen-turn-ends-mgr"
    assert not cursor.exists() or "w1-1.json" not in cursor.read_text(), (
        "the turn-end was marked seen for a FINISHED_SILENTLY nobody received")
    ladder = lane_state / ".fs-emitted-mgr.json"
    assert not ladder.exists() or "w1" not in ladder.read_text(), (
        "the emit ladder burnt a rung for a page that was never delivered — "
        "the next lull for this worker would be held instead of paged")
    assert not lane_io.heartbeat_path("mgr", "turn-ends").exists()


def test_the_outbox_drain_does_not_unlink_what_it_could_not_deliver(
        lane_state, monkeypatch):
    outbox = paths.notify_outbox_dir_for("mgr")
    outbox.mkdir(parents=True)
    entry = outbox / "1-1-0.json"
    entry.write_text(json.dumps(
        {"line": "AUTOCLOSED w1 idle 120min", "kind": "autoclosed",
         "buffered_at": 1.0}))

    _kill_stdout(monkeypatch)
    with pytest.raises(lane_io.LaneDead):
        monitor._drain_notify_outbox("mgr")

    assert entry.exists(), (
        "the outbox entry was unlinked although its line never reached the "
        "manager; the outbox is the only durable copy")


def test_the_outbox_drain_does_unlink_what_it_did_deliver(lane_state, capsys):
    outbox = paths.notify_outbox_dir_for("mgr")
    outbox.mkdir(parents=True)
    entry = outbox / "1-1-0.json"
    entry.write_text(json.dumps({"line": "AUTOCLOSED w1", "kind": "autoclosed"}))

    monitor._drain_notify_outbox("mgr")
    assert "AUTOCLOSED w1" in capsys.readouterr().out
    assert not entry.exists(), "a delivered entry was left to replay forever"


def test_the_standalone_emit_flushes(monkeypatch):
    class _FlushOnlyFails:
        def write(self, _text):
            return None

        def flush(self):
            raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(sys, "stdout", _FlushOnlyFails())
    with pytest.raises(stale_monitor.LaneDead):
        stale_monitor._emit("STALE_PROCESSING w1 (30min)")


def test_the_standalone_preflight_matches_the_package_one(monkeypatch):
    monkeypatch.setattr(stale_monitor, "_reader_is_dead", lambda fd=1: True)
    with pytest.raises(stale_monitor.LaneDead):
        stale_monitor._lane_preflight()


@pytest.mark.parametrize("lane", ["questions", "done"])
def test_the_simple_lanes_commit_nothing_to_a_dead_reader(
        lane_state, monkeypatch, lane):
    if lane == "done":
        bucket = paths.DONE / "mgr"
        payload = {"worker_name": "w", "summary": "s"}
        name = "w-1.json"
        scan = monitor.run_done_scan
    else:
        bucket = paths.question_dir_for("mgr")
        payload = {"worker_name": "w", "question": "q?"}
        name = "q1.json"
        scan = monitor.run_questions_scan
    bucket.mkdir(parents=True)
    state.write_json_atomic(bucket / name, payload)

    _kill_stdout(monkeypatch)
    with pytest.raises(lane_io.LaneDead):
        scan(dict(MGR))

    cursor = lane_state / f".seen-{lane}-mgr"
    assert not cursor.exists() or name not in cursor.read_text()
    assert not lane_io.heartbeat_path("mgr", lane).exists()


@pytest.mark.parametrize("lane", sorted(lane_io.LANES))
def test_every_lane_writes_a_heartbeat_on_a_healthy_scan(lane, lane_state, monkeypatch):
    if lane == "stale":
        stale_monitor._write_lane_heartbeat("mgr", "stale", time.time())
    else:
        scan = getattr(monitor, monitor._SCANS[lane])
        scan(dict(MGR))

    path = lane_io.heartbeat_path("mgr", lane)
    assert path.exists(), (
        f"the {lane} lane completed a healthy scan without writing a "
        f"heartbeat, so `dockwright lanes` reports it NEVER-ARMED forever and "
        f"the LANE_SILENT cross-check is blind to it")
    record = json.loads(path.read_text())
    assert record["lane"] == lane
    assert record["interval_hint"] == lane_io.LANES[lane]


@pytest.mark.parametrize("lane", ["questions", "done", "turn-ends"])
def test_a_quiet_scan_still_heartbeats(lane, lane_state, monkeypatch):
    scan = getattr(monitor, monitor._SCANS[lane])
    scan(dict(MGR))
    record = json.loads(lane_io.heartbeat_path("mgr", lane).read_text())
    assert record["last_emit"] is None, "precondition: nothing was emitted"
    assert record["last_scan"] > 0
