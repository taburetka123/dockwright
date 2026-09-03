import json

import pytest

from dockwright import lane_io, stale_monitor


def test_the_lane_table_matches_the_canonical_one():
    assert stale_monitor.LANE_INTERVALS == lane_io.LANES, (
        "stale_monitor's copy of the lane table has drifted from "
        "lane_io.LANES; the peer-liveness check would watch the wrong lanes "
        "or miss one entirely")


def test_the_staleness_window_matches_the_canonical_one():
    assert (stale_monitor.LANE_HEARTBEAT_STALE_INTERVALS
            == lane_io.HEARTBEAT_STALE_INTERVALS), (
        "the two halves would disagree about when a lane is dead: "
        "`dockwright lanes` and the LANE_SILENT page must draw the same line")


def test_the_heartbeat_path_matches_the_canonical_one(tmp_path, monkeypatch):
    monkeypatch.setattr(lane_io.paths, "LANE_HEALTH", tmp_path / "lane-health")
    monkeypatch.setattr(stale_monitor, "ROOT", tmp_path)
    for lane in lane_io.LANES:
        assert (stale_monitor._lane_heartbeat_path("mgr", lane)
                == lane_io.heartbeat_path("mgr", lane)), lane


@pytest.fixture
def sm_root(tmp_path, monkeypatch):
    monkeypatch.setattr(stale_monitor, "ROOT", tmp_path)
    return tmp_path


def _hb(root, lane, *, age_sec, now):
    path = root / "lane-health" / "mgr" / f"{lane}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"lane": lane, "last_scan": now - age_sec}))


def test_a_lane_that_stopped_scanning_is_paged(sm_root):
    now = 10_000.0
    _hb(sm_root, "done", age_sec=3600, now=now)
    pages, _, _ = _run_scans(sm_root, {}, now, 2)
    assert pages[0] == [], "paged on the FIRST observation — a freeze looks like this"
    assert [k for k, _ in pages[1]] == ["lane_silent:done"]
    assert "LANE_SILENT done" in pages[1][0][1]
    assert "NOT reaching you" in pages[1][0][1]


def test_a_freeze_shorter_than_the_observer_gap_check_still_does_not_page(sm_root):
    now = 10_000.0
    for lane in stale_monitor.LANE_INTERVALS:
        _hb(sm_root, lane, age_sec=1, now=now)
    nxt = {}
    stale_monitor._lane_silence_events("mgr", {}, nxt, now)

    frozen = now + 100
    assert stale_monitor._lane_silence_events("mgr", dict(nxt), {}, frozen) == [], (
        "paged during a sub-threshold freeze, on a single stale observation")


def test_a_healthy_lane_is_silent(sm_root):
    now = 10_000.0
    _hb(sm_root, "done", age_sec=1, now=now)
    assert stale_monitor._lane_silence_events("mgr", {}, {}, now) == []


def test_a_never_armed_lane_is_NOT_paged(sm_root):
    now = 10_000.0
    pages, _, _ = _run_scans(sm_root, {}, now, 3)
    assert not any(pages), f"a never-armed lane paged: {pages}"


def _run_scans(root, emitted, start, count, step=60.0):
    pages = []
    now = start
    for _ in range(count):
        nxt = {}
        pages.append(stale_monitor._lane_silence_events("mgr", emitted, nxt, now))
        emitted = dict(nxt)
        now += step
    return pages, emitted, now


def test_repeats_are_laddered_not_every_scan(sm_root):
    now = 10_000.0
    _hb(sm_root, "done", age_sec=3600, now=now)
    pages, emitted, _ = _run_scans(sm_root, {}, now, 3)
    assert pages[0] == [], "paged on a single observation"
    assert pages[1], "the second consecutive stale observation did not page"
    assert pages[2] == [], "paged again 60s later — the ladder is not holding"

    rungs = int(stale_monitor.LANE_SILENT_LADDER_BASE_SEC / 60) + 2
    pages, emitted, _ = _run_scans(sm_root, emitted, now + 120, rungs)
    assert any(pages), (
        "the ladder never matures, so a lane nobody re-armed goes quiet again")


def test_a_suspended_host_does_not_page_about_every_healthy_lane(sm_root):
    now = 10_000.0
    for lane in stale_monitor.LANE_INTERVALS:
        _hb(sm_root, lane, age_sec=1, now=now)
    nxt = {}
    assert stale_monitor._lane_silence_events("mgr", {}, nxt, now) == []

    resumed = now + 20 * 60
    assert stale_monitor._lane_silence_events("mgr", dict(nxt), {}, resumed) == [], (
        "paged about every lane after a host suspend")


def test_the_suspend_guard_costs_only_one_cycle(sm_root):
    now = 10_000.0
    _hb(sm_root, "done", age_sec=3600, now=now)
    _, emitted, clock = _run_scans(sm_root, {}, now, 2)
    resumed = clock + 20 * 60
    after_suspend = {}
    assert stale_monitor._lane_silence_events(
        "mgr", dict(emitted), after_suspend, resumed) == [], "paged through the suspend"
    pages, _, _ = _run_scans(sm_root, dict(after_suspend), resumed + 60, 12)
    assert any(pages), (
        "the dead lane stayed hidden after the suspend cycle passed")


def test_the_check_never_reports_the_lane_doing_the_reporting(sm_root):
    now = 1_000_000.0
    for lane in stale_monitor.LANE_INTERVALS:
        _hb(sm_root, lane, age_sec=100_000, now=now)
    all_pages, _, _ = _run_scans(sm_root, {}, now, 2)
    pages = all_pages[1]
    assert pages, "precondition: the other lanes are silent and should page"
    assert not any("stale" in key for key, _ in pages), (
        "the stale lane paged about itself")


def test_recovery_clears_the_ladder(sm_root):
    now = 10_000.0
    _hb(sm_root, "done", age_sec=3600, now=now)
    _, emitted, _ = _run_scans(sm_root, {}, now, 2)
    assert "lane_silent:done" in emitted

    _hb(sm_root, "done", age_sec=1, now=now)
    fresh = {}
    assert stale_monitor._lane_silence_events("mgr", dict(emitted), fresh, now) == []
    assert "lane_stale_seen:done" not in fresh, (
        "the confirmation marker survived recovery, so the next outage would "
        "page on a single observation")
    assert "lane_silent:done" not in fresh, (
        "a recovered lane kept its burnt rung, so the NEXT outage would be "
        "held instead of paging immediately")


def test_a_broken_heartbeat_file_cannot_break_the_scan(sm_root):
    now = 10_000.0
    path = sm_root / "lane-health" / "mgr" / "done.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert stale_monitor._lane_silence_events("mgr", {}, {}, now) == []


@pytest.mark.parametrize("name", ["..", ".", "a/b", "a\\b", "normal-name",
                                  "../../etc", "_unresolved"])
def test_the_bucket_sanitizer_matches_the_canonical_one(name):
    from dockwright import paths
    assert stale_monitor._safe_bucket(name) == paths._event_bucket(name)


def test_a_marker_set_before_a_freeze_does_not_page_on_resume(sm_root):
    now = 10_000.0
    for lane in stale_monitor.LANE_INTERVALS:
        _hb(sm_root, lane, age_sec=1, now=now)

    _hb(sm_root, "done", age_sec=3600, now=now)
    first = {}
    assert stale_monitor._lane_silence_events("mgr", {}, first, now) == []
    assert "lane_stale_seen:done" in first, "precondition: the marker was set"

    resumed = now + 20 * 60
    assert stale_monitor._lane_silence_events(
        "mgr", dict(first), {}, resumed) == [], (
        "paged on a second observation that was never actually taken — the "
        "gap across the freeze is not evidence")
