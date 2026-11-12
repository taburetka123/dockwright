import json
import os

import pytest

from dockwright import lane_io, monitor_loop, paths, state


@pytest.fixture
def lane_root(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "LANES", tmp_path / "lanes")
    monkeypatch.delenv("CLAUDE_AGENT", raising=False)
    monkeypatch.delenv("CLAUDE_ORCH_LANE_RESOLVE_GRACE_SEC", raising=False)
    return tmp_path


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, sec):
        self.slept.append(sec)
        self.now += sec


def _runner(codes):
    calls = []

    def run(lane):
        calls.append(lane)
        return codes.pop(0) if codes else None

    return run, calls


def _marker(cli_pid, lane):
    return json.loads(monitor_loop.marker_path(cli_pid, lane).read_text())


def test_zero_exit_continues_writes_marker_and_sleeps_lane_interval(lane_root):
    clock = Clock()
    run, calls = _runner([0, 0, 3])
    rc = monitor_loop.loop("questions", cli_pid=4000, run=run, sleep=clock.sleep,
                           clock=clock.time, grace=600, worker=False)
    assert rc == lane_io.EXIT_LANE_DEAD
    assert calls == ["questions", "questions", "questions"]
    assert clock.slept == [lane_io.LANES["questions"]] * 2
    m = _marker(4000, "questions")
    assert m["lane"] == "questions" and m["cli_pid"] == 4000 and m["lane_pid"] == os.getpid()
    assert m["resolved"] is True


def test_initial_marker_has_resolved_none_then_reflects_scan(lane_root):
    clock = Clock()
    seen = []

    def run(lane):
        seen.append(_marker(4000, lane)["resolved"])
        return [2, 4][len(seen) - 1]

    monitor_loop.loop("done", cli_pid=4000, run=run, sleep=clock.sleep,
                      clock=clock.time, grace=600, worker=False)
    assert seen == [None, False]


def test_unresolved_within_grace_continues_then_exits_seven(lane_root):
    clock = Clock()
    run, calls = _runner([2] * 200)
    rc = monitor_loop.loop("stale", cli_pid=4000, run=run, sleep=clock.sleep,
                           clock=clock.time, grace=600, worker=False)
    assert rc == lane_io.EXIT_LANE_UNRESOLVED
    assert clock.slept[0] == lane_io.LANES["stale"]
    assert clock.slept[-1] == monitor_loop.BACKOFF_INTERVAL_SEC
    assert 600 <= clock.now - 1000.0 < 600 + monitor_loop.BACKOFF_INTERVAL_SEC + lane_io.LANES["stale"]
    assert _marker(4000, "stale")["resolved"] is False


def test_fast_lane_backs_off_to_fifteen_seconds_after_a_minute(lane_root):
    clock = Clock()
    run, _ = _runner([2] * 200)
    monitor_loop.loop("questions", cli_pid=4000, run=run, sleep=clock.sleep,
                      clock=clock.time, grace=600, worker=False)
    first_minute = [s for s in clock.slept if s == lane_io.LANES["questions"]]
    assert len(first_minute) == 30
    assert set(clock.slept[30:]) == {monitor_loop.BACKOFF_INTERVAL_SEC}


def test_resolved_scan_resets_the_grace_clock(lane_root):
    clock = Clock()
    run, _ = _runner([2] * 20 + [0] + [2] * 400)
    rc = monitor_loop.loop("questions", cli_pid=4000, run=run, sleep=clock.sleep,
                           clock=clock.time, grace=600, worker=False)
    assert rc == lane_io.EXIT_LANE_UNRESOLVED
    assert clock.now - 1000.0 > 600 + 20 * lane_io.LANES["questions"]


def test_worker_session_exits_seven_on_first_unresolved_scan(lane_root):
    clock = Clock()
    run, calls = _runner([2, 2])
    rc = monitor_loop.loop("questions", cli_pid=4000, run=run, sleep=clock.sleep,
                           clock=clock.time, grace=600, worker=True)
    assert rc == lane_io.EXIT_LANE_UNRESOLVED
    assert calls == ["questions"] and clock.slept == []


@pytest.mark.parametrize("code", [lane_io.EXIT_LANE_DEAD, lane_io.EXIT_LANE_WEDGED])
def test_dead_and_wedged_exit_immediately_with_the_same_code(lane_root, code):
    clock = Clock()
    run, calls = _runner([code, 0])
    rc = monitor_loop.loop("done", cli_pid=4000, run=run, sleep=clock.sleep,
                           clock=clock.time, grace=600, worker=False)
    assert rc == code and calls == ["done"]


def test_unknown_code_twice_continues_three_times_exits_with_it(lane_root):
    clock = Clock()
    run, calls = _runner([1, 1, 0, 1, 1, 1, 0])
    rc = monitor_loop.loop("done", cli_pid=4000, run=run, sleep=clock.sleep,
                           clock=clock.time, grace=600, worker=False)
    assert rc == 1 and len(calls) == 6


def test_a_different_unknown_code_resets_the_streak(lane_root):
    clock = Clock()
    run, calls = _runner([1, 9, 1, lane_io.EXIT_LANE_DEAD])
    rc = monitor_loop.loop("done", cli_pid=4000, run=run, sleep=clock.sleep,
                           clock=clock.time, grace=600, worker=False)
    assert rc == lane_io.EXIT_LANE_DEAD and len(calls) == 4, (
        "three different unknown codes ended the lane and reported the last one "
        "as having occurred three times in a row")


def test_the_same_unknown_code_three_times_still_ends_the_lane(lane_root):
    clock = Clock()
    run, calls = _runner([1, 1, 1, 0])
    rc = monitor_loop.loop("done", cli_pid=4000, run=run, sleep=clock.sleep,
                           clock=clock.time, grace=600, worker=False)
    assert rc == 1 and len(calls) == 3


def test_busy_lock_exits_five_without_scanning(lane_root, monkeypatch):
    clock = Clock()
    run, calls = _runner([0])
    monitor_loop.lock_path(4000, "questions").parent.mkdir(parents=True)
    held = monitor_loop.acquire_lock(4000, "questions")
    assert held is not None
    rc = monitor_loop.loop("questions", cli_pid=4000, run=run, sleep=clock.sleep,
                           clock=clock.time, grace=600, worker=False)
    assert rc == lane_io.EXIT_LANE_DUPLICATE and calls == []


def test_main_rejects_unknown_lane_with_usage(lane_root, capsys):
    assert monitor_loop.main(["bogus"]) == 2
    assert "Usage: dockwright monitor-loop" in capsys.readouterr().err


def test_main_exits_six_when_no_cli_ancestor(lane_root, monkeypatch):
    monkeypatch.setattr(monitor_loop, "resolve_cli_pid", lambda start: None)
    assert monitor_loop.main(["questions"]) == lane_io.EXIT_NO_CLI


def test_main_reads_grace_and_worker_from_env(lane_root, monkeypatch):
    captured = {}

    def fake_loop(lane, **kw):
        captured.update(kw)
        return 0

    monkeypatch.setattr(monitor_loop, "resolve_cli_pid", lambda start: 4000)
    monkeypatch.setattr(monitor_loop, "loop", fake_loop)
    monkeypatch.setenv("CLAUDE_ORCH_LANE_RESOLVE_GRACE_SEC", "30")
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    assert monitor_loop.main(["done"]) == 0
    assert captured["cli_pid"] == 4000 and captured["grace"] == 30 and captured["worker"] is True


def _flag(tmp_path, monkeypatch, value):
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(f"[lanes]\nin_manager = {value}\n")
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))


def test_an_in_daemon_lane_does_one_last_scan_and_exits_zero(lane_root, monkeypatch, capsys):
    _flag(lane_root, monkeypatch, "false")
    clock = Clock()
    run, calls = _runner([0, 0, 0])
    rc = monitor_loop.loop("questions", cli_pid=4000, run=run, sleep=clock.sleep, clock=clock.time, grace=600,
                           worker=False)
    assert rc == 0 and calls == ["questions"] and clock.slept == []
    assert "questions lane runs in the daemon ([lanes] in_manager = false)" in capsys.readouterr().err


def test_the_pages_lane_loops_whatever_the_flag_says(lane_root, monkeypatch):
    _flag(lane_root, monkeypatch, "false")
    clock = Clock()
    run, calls = _runner([0, 0, 3])
    rc = monitor_loop.loop("pages", cli_pid=4000, run=run, sleep=clock.sleep, clock=clock.time, grace=600,
                           worker=False)
    assert rc == lane_io.EXIT_LANE_DEAD and calls == ["pages"] * 3 and clock.slept == [1, 1]


def test_the_flag_is_reread_before_every_scan(lane_root, monkeypatch):
    clock = Clock()
    calls = []

    def run(lane):
        calls.append(lane)
        if len(calls) == 2:
            _flag(lane_root, monkeypatch, "false")
        return 0

    rc = monitor_loop.loop("done", cli_pid=4000, run=run, sleep=clock.sleep, clock=clock.time, grace=600,
                           worker=False)
    assert rc == 0 and calls == ["done", "done", "done"]


def test_main_exits_zero_without_locking_when_the_lane_runs_in_the_daemon(lane_root, monkeypatch, capsys):
    _flag(lane_root, monkeypatch, "false")
    monkeypatch.setattr(monitor_loop, "resolve_cli_pid", lambda ppid: 4000)
    assert monitor_loop.main(["stale"]) == 0
    assert not monitor_loop.lock_path(4000, "stale").exists()
