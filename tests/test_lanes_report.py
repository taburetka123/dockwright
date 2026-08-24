"""`dockwright lanes` — the check that must not be able to say OK while broken.

A manager cannot distinguish "no events" from "my lane is dead": silence is
both. This report answers it from two signals that fail independently — the
heartbeat (which a broken lane cannot write) and the backlog (derived from the
event directory and the cursor, never from the heartbeat).

The load-bearing test here is
test_fresh_heartbeat_plus_backlog_is_not_ok: delete the backlog arm and it goes
red. Everything else is scaffolding around that one.
"""
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
    """One slow scan must not read as death — the window is 3 intervals."""
    now = 10_000.0
    _heartbeat("mgr", "done", age_sec=lane_io.LANES["done"] * 2, now=now)
    assert _row(lanes.inspect("mgr", now=now), "done")["verdict"] == lanes.OK


def test_fresh_heartbeat_plus_backlog_is_not_ok(lane_state):
    """THE anti-'healthy while broken' test.

    A lane can heartbeat honestly (its reader is alive, it flushed everything
    it emitted) and still not be draining — a duplicate lane consuming the
    events elsewhere, a cursor that raced, a scan crashing after preflight.
    The backlog arm is derived from the event dir and the cursor, so it sees
    that independently. Delete the arm and this goes red.
    """
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
    """An event that landed a second ago has not been missed, it is in flight."""
    now = 10_000.0
    _heartbeat("mgr", "done", age_sec=1, now=now)
    _done_event("mgr", "sid-a-1.json", age_sec=1, now=now)
    assert _row(lanes.inspect("mgr", now=now), "done")["verdict"] == lanes.OK


def test_hold_semantics_lanes_report_backlog_as_not_applicable(lane_state):
    """`turn-ends` HOLDS events without consuming them (delegation hold,
    turn-burst hold, FS-ladder rungs to 4h) and `stale` has no per-event
    cursor. Counting a backlog over them would cry wolf on healthy lanes, so
    the report says n/a instead of claiming a check it never ran."""
    now = 10_000.0
    for lane in lane_io.LANES:
        _heartbeat("mgr", lane, age_sec=1, now=now)
    rows = lanes.inspect("mgr", now=now)
    assert _row(rows, "turn-ends")["backlog"] is None
    assert _row(rows, "stale")["backlog"] is None
    assert _row(rows, "done")["backlog"] == 0
    assert _row(rows, "questions")["backlog"] == 0


def test_report_covers_every_lane_the_dispatcher_knows(lane_state):
    """Set equality, not a subset: a fifth lane must be reported or go red."""
    rows = lanes.inspect("mgr", now=1000.0)
    assert {r["lane"] for r in rows} == set(monitor._MONITOR_SUBCOMMANDS)


def test_report_iterates_the_lane_set_rather_than_a_hardcoded_list(
        lane_state, monkeypatch):
    """ADD-ONE: introduce a lane that did not exist when this was written."""
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


# While stale_monitor flags the manager as rate-limited, every lane HOLDS by
# design: prints nothing, marks nothing seen. Events pile up on purpose. The
# backlog arm must not report that as a fault at the one moment the manager
# can least act on it — but the heartbeat arm must keep running, because a
# held lane still scans and still has to prove it is alive.

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
    """The suspension is scoped to the backlog arm only. A lane that stopped
    scanning during a limit window is still dead, and must still say so."""
    now = 10_000.0
    _limit_manager(lane_state)
    _heartbeat("mgr", "done", age_sec=lane_io.LANES["done"] * 10, now=now)
    row = _row(lanes.inspect("mgr", now=now), "done")
    assert row["limited"] is True
    assert row["verdict"] == lanes.DEAD, (
        "the rate-limit suspension swallowed a genuinely dead lane")


def test_an_expired_limit_flag_restores_the_backlog_arm(lane_state):
    """The flag is fail-closed and its only writer is the 60s stale loop; an
    mtime past the TTL means that loop died, and monitor._manager_limited
    already treats it as clear. This report must inherit that, or a stale flag
    would blind the backlog check forever."""
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


# "not checked" must never skim as "checked and fine". That substitution is the
# whole failure this command exists to end, so the two renderings must not be
# confusable — required explicitly, and checked here rather than eyeballed once.

def _render(lane_state, lane, now):
    rows = lanes.inspect("mgr", now=now)
    return lanes._format(_row(rows, lane))


def test_an_unchecked_backlog_does_not_render_like_a_clean_one(lane_state):
    now = 10_000.0
    for lane in lane_io.LANES:
        _heartbeat("mgr", lane, age_sec=1, now=now)
    checked = _render(lane_state, "done", now)        # backlog arm ran, found 0
    unchecked = _render(lane_state, "turn-ends", now)  # backlog arm did not run

    assert "backlog 0" in checked
    assert "backlog 0" not in unchecked, (
        "an unchecked lane renders a zero — a reader sees 'no backlog' for a "
        "check that never ran")
    assert "NOT CHECKED" in unchecked, (
        "the unchecked row must SAY it was not checked, in words a tired "
        "reader cannot skim past")
    # The distinguishing token must not be a single character's difference.
    assert abs(len(checked) - len(unchecked)) > 10, (
        "the two rows are near-identical in shape; they must not be "
        "confusable at a glance")


def test_the_two_unchecked_reasons_are_distinguishable(lane_state):
    """'held by design' and 'held because the manager is rate-limited' are
    different situations and only one of them is temporary."""
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
    """The cursor can hold absolute paths written under the PRE-RENAME state
    root; `monitor._load_seen` normalizes them. Reading the cursor raw meant
    every legacy line failed to match mid-migration and the lane reported a
    BACKLOGGED it did not have — a false alarm from the one check whose value
    is being trustworthy when it fires.
    """
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
