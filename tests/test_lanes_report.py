import time

import pytest

from dockwright import lane_io, lanes, monitor, paths, state


@pytest.fixture
def lane_state(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "DONE", tmp_path / "done")
    monkeypatch.setattr(paths, "QUESTIONS", tmp_path / "questions")
    monkeypatch.setattr(paths, "TURN_ENDS", tmp_path / "turn-ends")
    monkeypatch.setattr(paths, "LANE_HEALTH", tmp_path / "lane-health")
    return tmp_path


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


def test_never_armed_when_no_heartbeat_was_ever_written(lane_state):
    rows = lanes.inspect("mgr", now=1000.0)
    assert {r["verdict"] for r in rows} == {lanes.NEVER_ARMED}


def test_fresh_heartbeat_and_no_backlog_is_ok(lane_state):
    now = 10_000.0
    for lane in lane_io.LANES:
        _heartbeat("mgr", lane, age_sec=1, now=now)
    rows = lanes.inspect("mgr", now=now)
    assert {r["verdict"] for r in rows} == {lanes.OK}


def test_stale_heartbeat_is_dead(lane_state):
    now = 10_000.0
    _heartbeat("mgr", "done", age_sec=lane_io.LANES["done"] * 10, now=now)
    assert _row(lanes.inspect("mgr", now=now), "done")["verdict"] == lanes.DEAD


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
    assert row["verdict"] == lanes.DEAD, (
        "the rate-limit suspension swallowed a genuinely dead lane")


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
