import subprocess
import sys
import time

import pytest

from dockwright import lane_io, lanes, monitor, paths, state

_REAL_LANE_PROCESS_ALIVE = lanes._lane_process_alive


@pytest.fixture
def lane_state(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(paths, "DONE", tmp_path / "done")
    monkeypatch.setattr(paths, "QUESTIONS", tmp_path / "questions")
    monkeypatch.setattr(paths, "TURN_ENDS", tmp_path / "turn-ends")
    monkeypatch.setattr(paths, "LANE_HEALTH", tmp_path / "lane-health")
    monkeypatch.setattr(paths, "LANES", tmp_path / "lanes")
    monkeypatch.setattr(lanes, "_lane_process_alive", lambda pid, lane: pid in ALIVE)
    ALIVE.clear()
    _record("mgr", pid=4000)
    return tmp_path


ALIVE: set = set()


def _record(name, *, pid, sid=None):
    (paths.ACTIVE).mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.ACTIVE / f"{sid or name}.json",
                            {"agent": "manager", "name": name, "claude_sid": sid or name, "pid": pid})


def _marker(cli_pid, lane, *, lane_pid, resolved, now, iter_age_sec=0):
    from dockwright import monitor_loop
    state.write_json_atomic(monitor_loop.marker_path(cli_pid, lane), {
        "lane": lane, "lane_pid": lane_pid, "cli_pid": cli_pid,
        "started": now - 100, "last_iter": now - iter_age_sec, "resolved": resolved})


def _row(rows, lane):
    return next(r for r in rows if r["lane"] == lane)


def _heartbeat(manager, lane, *, age_sec, now):
    state.write_json_atomic(lane_io.heartbeat_path(manager, lane), {
        "lane": lane, "manager": manager, "pid": 1,
        "last_scan": now - age_sec, "last_emit": None,
        "interval_hint": lane_io.LANES[lane],
    })


def _done_event(manager, name, *, age_sec, now):
    target = paths.DONE / manager
    target.mkdir(parents=True, exist_ok=True)
    path = target / name
    path.write_text('{"worker_name": "w", "summary": "s"}')
    import os
    os.utime(path, (now - age_sec, now - age_sec))
    return path


def test_never_armed_when_no_heartbeat_and_no_marker(lane_state):
    rows = lanes.inspect("mgr", now=1000.0)
    assert {r["verdict"] for r in rows} == {lanes.NEVER_ARMED}


def test_fresh_heartbeat_and_no_backlog_is_ok(lane_state):
    now = 10_000.0
    for lane in lane_io.LANES:
        _heartbeat("mgr", lane, age_sec=1, now=now)
    rows = lanes.inspect("mgr", now=now)
    assert {r["verdict"] for r in rows} == {lanes.OK}


def test_stale_heartbeat_without_marker_is_never_armed(lane_state):
    now = 10_000.0
    _heartbeat("mgr", "done", age_sec=lane_io.LANES["done"] * 10, now=now)
    assert _row(lanes.inspect("mgr", now=now), "done")["verdict"] == lanes.NEVER_ARMED


def test_stale_heartbeat_with_dead_lane_process_is_dead(lane_state):
    now = 10_000.0
    _heartbeat("mgr", "done", age_sec=lane_io.LANES["done"] * 10, now=now)
    _marker(4000, "done", lane_pid=555, resolved=True, now=now)
    row = _row(lanes.inspect("mgr", now=now), "done")
    assert row["verdict"] == lanes.DEAD and row["pid"] == 555


def test_live_unresolved_marker_is_armed_unresolved(lane_state):
    now = 10_000.0
    ALIVE.add(555)
    _marker(4000, "questions", lane_pid=555, resolved=False, now=now)
    assert _row(lanes.inspect("mgr", now=now), "questions")["verdict"] == lanes.ARMED_UNRESOLVED


def test_live_initial_marker_resolved_none_is_armed_unresolved(lane_state):
    now = 10_000.0
    ALIVE.add(555)
    _marker(4000, "questions", lane_pid=555, resolved=None, now=now)
    assert _row(lanes.inspect("mgr", now=now), "questions")["verdict"] == lanes.ARMED_UNRESOLVED


def test_live_resolved_marker_with_stale_heartbeat_is_stalled(lane_state):
    now = 10_000.0
    ALIVE.add(555)
    _heartbeat("mgr", "questions", age_sec=lane_io.LANES["questions"] * 10, now=now)
    _marker(4000, "questions", lane_pid=555, resolved=True, now=now)
    assert _row(lanes.inspect("mgr", now=now), "questions")["verdict"] == lanes.STALLED


def test_a_lane_that_stopped_iterating_is_hung_not_stalled(lane_state):
    now = 10_000.0
    ALIVE.add(555)
    _heartbeat("mgr", "questions", age_sec=lane_io.LANES["questions"] * 10, now=now)
    _marker(4000, "questions", lane_pid=555, resolved=True, now=now, iter_age_sec=200)
    row = _row(lanes.inspect("mgr", now=now), "questions")
    assert row["verdict"] == lanes.HUNG, (
        "a lane whose scan hangs forever reported STALLED, whose recovery line "
        "promises a self-termination that will never come")
    line = lanes.recovery_line(row["verdict"], row["lane"], row)
    assert "kill 555" in line, "the manager is not told to kill the wedged lane process"
    assert "200s" in line


def test_a_hung_lane_is_hung_whatever_resolved_says(lane_state):
    now = 10_000.0
    ALIVE.add(555)
    _heartbeat("mgr", "questions", age_sec=lane_io.LANES["questions"] * 10, now=now)
    _marker(4000, "questions", lane_pid=555, resolved=False, now=now, iter_age_sec=200)
    assert _row(lanes.inspect("mgr", now=now), "questions")["verdict"] == lanes.HUNG


def test_the_hung_window_follows_the_lane_interval(lane_state):
    now = 10_000.0
    ALIVE.add(555)
    _heartbeat("mgr", "stale", age_sec=lane_io.LANES["stale"] * 10, now=now)
    _marker(4000, "stale", lane_pid=555, resolved=True, now=now, iter_age_sec=100)
    assert _row(lanes.inspect("mgr", now=now), "stale")["verdict"] == lanes.STALLED, (
        "the 60s lane was called hung one interval after its last iteration")
    _marker(4000, "stale", lane_pid=555, resolved=True, now=now, iter_age_sec=300)
    assert _row(lanes.inspect("mgr", now=now), "stale")["verdict"] == lanes.HUNG


def test_a_still_iterating_lane_keeps_its_old_verdicts(lane_state):
    now = 10_000.0
    ALIVE.add(555)
    for lane, resolved, expected in (("questions", True, lanes.STALLED),
                                     ("done", False, lanes.ARMED_UNRESOLVED)):
        _heartbeat("mgr", lane, age_sec=lane_io.LANES[lane] * 10, now=now)
        _marker(4000, lane, lane_pid=555, resolved=resolved, now=now, iter_age_sec=1)
        assert _row(lanes.inspect("mgr", now=now), lane)["verdict"] == expected


def test_hung_sits_between_dead_and_stalled_in_the_severity_order(lane_state):
    assert lanes._worst(lanes.STALLED, lanes.HUNG) == lanes.HUNG
    assert lanes._worst(lanes.ARMED_UNRESOLVED, lanes.HUNG) == lanes.HUNG
    assert lanes._worst(lanes.BACKLOGGED, lanes.HUNG) == lanes.HUNG
    assert lanes._worst(lanes.HUNG, lanes.DEAD) == lanes.DEAD
    assert lanes._worst(lanes.HUNG, lanes.NEVER_ARMED) == lanes.NEVER_ARMED


def test_fresh_heartbeat_is_ok_whatever_the_marker_says(lane_state):
    now = 10_000.0
    _heartbeat("mgr", "questions", age_sec=1, now=now)
    _marker(4000, "questions", lane_pid=555, resolved=False, now=now)
    assert _row(lanes.inspect("mgr", now=now), "questions")["verdict"] == lanes.OK


def test_marker_under_another_live_cli_pid_does_not_count(lane_state):
    now = 10_000.0
    _record("other", pid=7000)
    ALIVE.update({7000, 777})
    _marker(7000, "questions", lane_pid=777, resolved=True, now=now)
    assert _row(lanes.inspect("mgr", now=now), "questions")["verdict"] == lanes.NEVER_ARMED


def test_orphan_marker_under_dead_cli_pid_does_not_count(lane_state):
    now = 10_000.0
    ALIVE.add(777)
    _marker(8000, "questions", lane_pid=777, resolved=True, now=now)
    assert _row(lanes.inspect("mgr", now=now), "questions")["verdict"] == lanes.NEVER_ARMED


def test_no_active_record_gives_heartbeat_only_verdicts(lane_state):
    now = 10_000.0
    (paths.ACTIVE / "mgr.json").unlink()
    _heartbeat("mgr", "done", age_sec=1, now=now)
    _heartbeat("mgr", "stale", age_sec=lane_io.LANES["stale"] * 10, now=now)
    rows = lanes.inspect("mgr", now=now)
    assert _row(rows, "done")["verdict"] == lanes.OK
    assert _row(rows, "stale")["verdict"] == lanes.DEAD
    assert _row(rows, "questions")["verdict"] == lanes.NEVER_ARMED
    assert all(r["heartbeat_only"] for r in rows)


def test_recovery_lines_per_verdict():
    assert "Skill(dockwright:arm-lanes)" in lanes.recovery_line(lanes.NEVER_ARMED, "done")
    assert "setup.sh" in lanes.recovery_line(lanes.NEVER_ARMED, "done")
    assert "dockwright monitor-loop done" in lanes.recovery_line(lanes.DEAD, "done")
    assert "1800000" in lanes.recovery_line(lanes.DEAD, "done")
    assert "10 s" in lanes.recovery_line(lanes.NEVER_ARMED, "done"), (
        "the NEVER-ARMED line carries no clock, so a manager re-reading inside "
        "the arming window concludes the plugin is not installed")
    assert "arm nothing" in lanes.recovery_line(lanes.ARMED_UNRESOLVED, "done")
    assert "max_consecutive_errors" in lanes.recovery_line(lanes.STALLED, "done")
    assert "dockwright monitor-loop done" in lanes.recovery_line(lanes.HUNG, "done")
    assert lanes.recovery_line(lanes.OK, "done") == ""


def test_format_marks_heartbeat_only_rows(lane_state):
    now = 10_000.0
    (paths.ACTIVE / "mgr.json").unlink()
    _heartbeat("mgr", "done", age_sec=1, now=now)
    line = lanes._format(_row(lanes.inspect("mgr", now=now), "done"))
    assert line.startswith("OK (heartbeat-only)")


def test_heartbeat_within_the_grace_window_is_still_ok(lane_state):
    now = 10_000.0
    _heartbeat("mgr", "done", age_sec=lane_io.LANES["done"] * 2, now=now)
    assert _row(lanes.inspect("mgr", now=now), "done")["verdict"] == lanes.OK


def test_fresh_heartbeat_plus_backlog_is_not_ok(lane_state):
    now = 10_000.0
    _heartbeat("mgr", "done", age_sec=1, now=now)
    _done_event("mgr", "sid-a-1.json", age_sec=600, now=now)
    row = _row(lanes.inspect("mgr", now=now), "done")
    assert row["heartbeat"] == lanes.OK, "precondition: the heartbeat looks fine"
    assert row["backlog"] == 1
    assert row["verdict"] == lanes.BACKLOGGED


def test_a_consumed_event_is_not_backlog(lane_state):
    now = 10_000.0
    _heartbeat("mgr", "done", age_sec=1, now=now)
    path = _done_event("mgr", "sid-a-1.json", age_sec=600, now=now)
    (paths.ROOT / ".seen-done-mgr").write_text(f"{path}\n")
    assert _row(lanes.inspect("mgr", now=now), "done")["verdict"] == lanes.OK


def test_a_young_unconsumed_event_is_not_backlog(lane_state):
    now = 10_000.0
    _heartbeat("mgr", "done", age_sec=1, now=now)
    _done_event("mgr", "sid-a-1.json", age_sec=1, now=now)
    assert _row(lanes.inspect("mgr", now=now), "done")["verdict"] == lanes.OK


def test_hold_semantics_lanes_report_backlog_as_not_applicable(lane_state):
    now = 10_000.0
    for lane in lane_io.LANES:
        _heartbeat("mgr", lane, age_sec=1, now=now)
    rows = lanes.inspect("mgr", now=now)
    assert _row(rows, "turn-ends")["backlog"] is None
    assert _row(rows, "stale")["backlog"] is None
    assert _row(rows, "done")["backlog"] == 0
    assert _row(rows, "questions")["backlog"] == 0


def test_report_covers_every_lane_the_dispatcher_knows(lane_state):
    rows = lanes.inspect("mgr", now=1000.0)
    assert {r["lane"] for r in rows} == set(monitor._MONITOR_SUBCOMMANDS)


def test_report_iterates_the_lane_set_rather_than_a_hardcoded_list(
        lane_state, monkeypatch):
    extended = dict(lane_io.LANES)
    extended["brand-new"] = 7
    monkeypatch.setattr(lane_io, "LANES", extended)
    rows = lanes.inspect("mgr", now=1000.0)
    assert "brand-new" in {r["lane"] for r in rows}, (
        "the report carries its own list of lanes; a new lane would be "
        "silently unmonitored")


def test_cli_exit_code_is_non_zero_when_a_lane_is_broken(lane_state, capsys):
    now = time.time()
    for lane in lane_io.LANES:
        _heartbeat("mgr", lane, age_sec=1, now=now)
    assert lanes.main(["mgr"]) == 0
    _heartbeat("mgr", "questions", age_sec=10_000, now=now)
    assert lanes.main(["mgr"]) == 1
    assert "questions" in capsys.readouterr().err


def test_cli_prints_a_failed_tmux_env_check_and_keeps_the_exit_code(lane_state, capsys, monkeypatch):
    from dockwright import doctor
    now = time.time()
    for lane in lane_io.LANES:
        _heartbeat("mgr", lane, age_sec=1, now=now)
    monkeypatch.setattr(doctor, "check_tmux_env",
                        lambda: doctor.Check("tmux:env", False, "the global environment carries GH_TOKEN"))
    assert lanes.main(["mgr"]) == 0
    assert "  WARNING tmux:env — the global environment carries GH_TOKEN" in capsys.readouterr().out
    monkeypatch.setattr(doctor, "check_tmux_env", lambda: doctor.Check("tmux:env", True, "clean"))
    assert lanes.main(["mgr"]) == 0
    assert "WARNING tmux:env" not in capsys.readouterr().out


def test_cli_rejects_extra_arguments(lane_state):
    assert lanes.main(["a", "b"]) == 2


def _limit_manager(tmp_path, name="mgr"):
    (tmp_path / f".manager-limited-{name}").write_text("")


def test_a_rate_limit_hold_is_not_reported_as_a_backlog(lane_state):
    now = 10_000.0
    _heartbeat("mgr", "done", age_sec=1, now=now)
    _done_event("mgr", "sid-a-1.json", age_sec=600, now=now)
    assert _row(lanes.inspect("mgr", now=now), "done")["verdict"] == lanes.BACKLOGGED

    _limit_manager(lane_state)
    row = _row(lanes.inspect("mgr", now=now), "done")
    assert row["limited"] is True
    assert row["backlog"] is None
    assert row["verdict"] == lanes.OK, (
        "a deliberate rate-limit hold was reported as a lane fault")


def test_a_rate_limit_hold_does_NOT_suppress_the_heartbeat_arm(lane_state):
    now = 10_000.0
    _limit_manager(lane_state)
    _heartbeat("mgr", "done", age_sec=lane_io.LANES["done"] * 10, now=now)
    row = _row(lanes.inspect("mgr", now=now), "done")
    assert row["limited"] is True
    assert row["verdict"] == lanes.NEVER_ARMED, (
        "the rate-limit suspension swallowed a lane that was never armed")


def test_an_expired_limit_flag_restores_the_backlog_arm(lane_state):
    import os
    now = time.time()
    flag = lane_state / ".manager-limited-mgr"
    flag.write_text("")
    old = now - (monitor.MANAGER_LIMITED_FLAG_TTL_SEC + 60)
    os.utime(flag, (old, old))
    _heartbeat("mgr", "done", age_sec=1, now=now)
    _done_event("mgr", "sid-a-1.json", age_sec=600, now=now)
    row = _row(lanes.inspect("mgr", now=now), "done")
    assert row["limited"] is False
    assert row["verdict"] == lanes.BACKLOGGED


def _render(lane_state, lane, now):
    rows = lanes.inspect("mgr", now=now)
    return lanes._format(_row(rows, lane))


def test_an_unchecked_backlog_does_not_render_like_a_clean_one(lane_state):
    now = 10_000.0
    for lane in lane_io.LANES:
        _heartbeat("mgr", lane, age_sec=1, now=now)
    checked = _render(lane_state, "done", now)
    unchecked = _render(lane_state, "turn-ends", now)

    assert "backlog 0" in checked
    assert "backlog 0" not in unchecked, (
        "an unchecked lane renders a zero — a reader sees 'no backlog' for a "
        "check that never ran")
    assert "NOT CHECKED" in unchecked, (
        "the unchecked row must SAY it was not checked, in words a tired "
        "reader cannot skim past")
    assert abs(len(checked) - len(unchecked)) > 10, (
        "the two rows are near-identical in shape; they must not be "
        "confusable at a glance")


def test_the_two_unchecked_reasons_are_distinguishable(lane_state):
    now = 10_000.0
    for lane in lane_io.LANES:
        _heartbeat("mgr", lane, age_sec=1, now=now)
    by_design = _render(lane_state, "turn-ends", now)
    _limit_manager(lane_state)
    while_limited = _render(lane_state, "done", now)
    assert "by design" in by_design
    assert "rate-limited" in while_limited
    assert by_design != while_limited


def test_a_legacy_prefixed_cursor_line_is_not_a_phantom_backlog(
        lane_state, monkeypatch):
    from dockwright import config, monitor

    now = 10_000.0
    _heartbeat("mgr", "done", age_sec=1, now=now)
    path = _done_event("mgr", "sid-a-1.json", age_sec=600, now=now)

    legacy_line = str(path).replace(str(paths.ROOT), str(config.legacy_state_root()), 1)
    assert legacy_line != str(path), "precondition: the rewrite produced a legacy path"
    (paths.ROOT / ".seen-done-mgr").write_text(f"{legacy_line}\n")

    assert legacy_line in monitor._load_seen(paths.ROOT / ".seen-done-mgr") or \
        str(path) in monitor._load_seen(paths.ROOT / ".seen-done-mgr")
    row = _row(lanes.inspect("mgr", now=now), "done")
    assert row["verdict"] == lanes.OK, (
        "a legacy-prefixed cursor line read as unconsumed — phantom backlog")


def _use_real_identity_probe(monkeypatch, *, alive, command):
    monkeypatch.setattr(lanes, "_lane_process_alive", _REAL_LANE_PROCESS_ALIVE)
    monkeypatch.setattr(lanes, "_pid_alive", lambda pid: alive)
    monkeypatch.setattr(lanes, "_lane_command", lambda pid: command)


def test_an_alive_pid_running_something_else_is_dead_not_hung(lane_state, monkeypatch):
    now = 10_000.0
    _use_real_identity_probe(monkeypatch, alive=True, command="/bin/sleep 20")
    _heartbeat("mgr", "questions", age_sec=lane_io.LANES["questions"] * 10, now=now)
    _marker(4000, "questions", lane_pid=555, resolved=True, now=now, iter_age_sec=200)
    row = _row(lanes.inspect("mgr", now=now), "questions")
    assert row["verdict"] == lanes.DEAD, (
        "a recycled pid belonging to an unrelated process read as a live lane; "
        "the HUNG recovery line then orders `kill` on that process")
    assert "kill 555" not in lanes.recovery_line(row["verdict"], row["lane"], row)


def test_an_alive_pid_running_this_lane_keeps_the_live_verdicts(lane_state, monkeypatch):
    now = 10_000.0
    _use_real_identity_probe(
        monkeypatch, alive=True, command="/v/bin/dockwright monitor-loop done")
    _heartbeat("mgr", "done", age_sec=lane_io.LANES["done"] * 10, now=now)
    _marker(4000, "done", lane_pid=555, resolved=True, now=now, iter_age_sec=1)
    assert _row(lanes.inspect("mgr", now=now), "done")["verdict"] == lanes.STALLED
    _marker(4000, "done", lane_pid=555, resolved=True, now=now, iter_age_sec=200)
    assert _row(lanes.inspect("mgr", now=now), "done")["verdict"] == lanes.HUNG
    _marker(4000, "done", lane_pid=555, resolved=False, now=now, iter_age_sec=1)
    assert _row(lanes.inspect("mgr", now=now), "done")["verdict"] == lanes.ARMED_UNRESOLVED


def test_an_alive_pid_running_a_different_lane_is_dead(lane_state, monkeypatch):
    now = 10_000.0
    _use_real_identity_probe(
        monkeypatch, alive=True, command="/v/bin/dockwright monitor-loop turn-ends")
    _heartbeat("mgr", "done", age_sec=lane_io.LANES["done"] * 10, now=now)
    _marker(4000, "done", lane_pid=555, resolved=True, now=now, iter_age_sec=1)
    assert _row(lanes.inspect("mgr", now=now), "done")["verdict"] == lanes.DEAD, (
        "one lane's live process vouched for a different lane's dead marker")


def test_a_dead_pid_is_dead_without_consulting_the_command(lane_state, monkeypatch):
    monkeypatch.setattr(lanes, "_lane_process_alive", _REAL_LANE_PROCESS_ALIVE)
    monkeypatch.setattr(lanes, "_pid_alive", lambda pid: False)

    def _refuse(pid):
        raise AssertionError("ps was run for a pid already known to be dead")

    monkeypatch.setattr(lanes, "_lane_command", _refuse)
    assert lanes._lane_process_alive(555, "done") is False


def test_the_identity_probe_rejects_a_real_unrelated_process():
    proc = subprocess.Popen(["/bin/sleep", "20"])
    try:
        assert lanes._lane_command(proc.pid)
        assert _REAL_LANE_PROCESS_ALIVE(proc.pid, "questions") is False
    finally:
        proc.kill()
        proc.wait()


def test_the_identity_probe_accepts_a_real_process_with_the_lane_command_line():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(20)", "monitor-loop", "done"])
    try:
        assert "monitor-loop done" in lanes._lane_command(proc.pid)
        assert _REAL_LANE_PROCESS_ALIVE(proc.pid, "done") is True
        assert _REAL_LANE_PROCESS_ALIVE(proc.pid, "questions") is False
    finally:
        proc.kill()
        proc.wait()


def test_the_probe_survives_a_ps_that_cannot_answer(lane_state, monkeypatch):
    monkeypatch.setattr(lanes, "_lane_process_alive", _REAL_LANE_PROCESS_ALIVE)
    monkeypatch.setattr(lanes, "_pid_alive", lambda pid: True)

    def _boom(pid):
        raise OSError("ps unavailable")

    monkeypatch.setattr(lanes, "subprocess", _FailingSubprocess(_boom))
    assert lanes._lane_command(555) == ""
    assert _REAL_LANE_PROCESS_ALIVE(555, "done") is False


class _FailingSubprocess:
    def __init__(self, boom):
        self._boom = boom
        self.TimeoutExpired = subprocess.TimeoutExpired

    def run(self, *args, **kwargs):
        self._boom(args)


def test_a_record_without_a_pid_is_named_apart_from_a_missing_record(lane_state, capsys):
    now = time.time()
    _heartbeat("mgr", "done", age_sec=1, now=now)
    (paths.ACTIVE / "mgr.json").unlink()
    _record("mgr", pid=None)
    rows = lanes.inspect("mgr", now=now)
    assert all(r["heartbeat_only"] for r in rows)
    lanes.main(["mgr"])
    out = capsys.readouterr().out
    assert "active record for mgr has no pid; marker lookup skipped" in out, (
        "a record with no usable pid reported as no record at all, sending the "
        "manager to hunt for a record that is right there")
    assert "no active record for mgr" not in out


def test_a_missing_record_still_says_no_active_record(lane_state, capsys):
    now = time.time()
    _heartbeat("mgr", "done", age_sec=1, now=now)
    (paths.ACTIVE / "mgr.json").unlink()
    lanes.main(["mgr"])
    assert "no active record for mgr; marker lookup skipped" in capsys.readouterr().out


def test_in_daemon_lanes_read_in_daemon_and_pages_decides_the_exit(lane_state, monkeypatch, capsys):
    cfg = lane_state / "dockwright.toml"
    cfg.write_text("[lanes]\nin_manager = false\n")
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    now = time.time()
    _heartbeat("mgr", "pages", age_sec=1, now=now)
    rows = {r["lane"]: r for r in lanes.inspect("mgr", now=now, record_pid=4000)}
    assert rows["pages"]["verdict"] == lanes.OK
    assert {rows[l]["verdict"] for l in ("questions", "done", "turn-ends", "stale")} == {lanes.IN_DAEMON}
    assert lanes.main(["mgr"]) == 0
    assert "IN-DAEMON" in capsys.readouterr().out


def test_in_daemon_rows_show_the_daemons_last_scan_and_not_the_tab_lanes_last_beat(lane_state, monkeypatch):
    cfg = lane_state / "dockwright.toml"
    cfg.write_text("[lanes]\nin_manager = false\n")
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    now = time.time()
    for lane in ("questions", "done", "turn-ends"):
        _heartbeat("mgr", lane, age_sec=927, now=now)
    _heartbeat("mgr", "stale", age_sec=4, now=now)
    paths.DAEMON.mkdir(parents=True, exist_ok=True)
    lane_io.remember_observed({"questions": now - 2, "done": now - 2, "turn-ends": now - 3})
    rows = {r["lane"]: r for r in lanes.inspect("mgr", now=now, record_pid=4000)}
    daemon_lanes = ("questions", "done", "turn-ends", "stale")
    assert {lane: round(rows[lane]["age_sec"]) for lane in daemon_lanes} == {
        "questions": 2, "done": 2, "turn-ends": 3, "stale": 4}
    assert {rows[lane]["verdict"] for lane in daemon_lanes} == {lanes.IN_DAEMON}
    assert "last-scan=2s ago" in lanes._format(rows["done"])


def test_the_pages_backlog_counts_daemon_lines_older_than_three_seconds_and_held_digests_only_after_the_hold(lane_state):
    import os
    now = time.time()
    outbox = paths.notify_outbox_dir_for("mgr")
    outbox.mkdir(parents=True)
    (outbox / "3.json").write_text('{"line": "x", "source": "daemon"}')
    old = outbox / "1.json"
    old.write_text('{"line": "y", "source": "daemon"}')
    os.utime(old, (now - 10, now - 10))
    digest = outbox / "2.json"
    digest.write_text('{"line": "gardener digest ready: run-1", "kind": "gardener"}')
    os.utime(digest, (now - 10, now - 10))
    assert lanes._backlog("mgr", "pages", 3, now) == 1
    os.utime(digest, (now - 1801, now - 1801))
    assert lanes._backlog("mgr", "pages", 3, now) == 2


def test_the_pages_lane_gets_at_least_a_six_second_window(lane_state):
    now = time.time()
    _heartbeat("mgr", "pages", age_sec=5, now=now)
    rows = {r["lane"]: r for r in lanes.inspect("mgr", now=now, record_pid=4000)}
    assert rows["pages"]["heartbeat"] == lanes.OK
    _heartbeat("mgr", "pages", age_sec=7, now=now)
    rows = {r["lane"]: r for r in lanes.inspect("mgr", now=now, record_pid=4000)}
    assert rows["pages"]["heartbeat"] != lanes.OK
