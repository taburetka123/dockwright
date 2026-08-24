"""`stale_monitor` duplicates the lane table; the copy must not drift.

`stale_monitor.py` ships standalone to `~/.claude/scripts/` and is stdlib-only,
so it cannot import `dockwright.lane_io`. The lane names, their poll intervals
and the staleness window are therefore duplicated there — the same trade the
file already makes for `_write_json_atomic`.

A duplicated constant is a hand-maintained set, which `drift-guard-tests.md`
§ ADD-ONE says is unguarded by construction: add a fifth lane to `lane_io` and
the peer-liveness cross-check silently stops covering it. These tests are what
makes that impossible — they compare the FULL mappings with `==`, never a
subset, so both a missing lane and a changed interval go red.
"""
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
    """Same file, computed twice. If these diverge the cross-check reads a
    path nothing writes and reports every lane healthy forever — the exact
    'green because I found nothing' failure the report exists to prevent."""
    monkeypatch.setattr(lane_io.paths, "LANE_HEALTH", tmp_path / "lane-health")
    monkeypatch.setattr(stale_monitor, "ROOT", tmp_path)
    for lane in lane_io.LANES:
        assert (stale_monitor._lane_heartbeat_path("mgr", lane)
                == lane_io.heartbeat_path("mgr", lane)), lane


def test_a_new_lane_is_covered_or_the_suite_goes_red(monkeypatch):
    """ADD-ONE, the direction that actually happens: someone adds a lane to
    the canonical table and forgets the standalone copy."""
    extended = dict(lane_io.LANES)
    extended["brand-new"] = 7
    monkeypatch.setattr(lane_io, "LANES", extended)
    assert stale_monitor.LANE_INTERVALS != lane_io.LANES, (
        "the mirror test cannot detect a new lane — it is comparing "
        "something other than the live tables")


# The cross-check itself: the stale lane pages the manager about its PEERS.
# A dead lane already ends its own task and notifies ONCE; the incident was
# that one notification going unnoticed for hours, so this is the nag.

@pytest.fixture
def sm_root(tmp_path, monkeypatch):
    monkeypatch.setattr(stale_monitor, "ROOT", tmp_path)
    return tmp_path


def _hb(root, lane, *, age_sec, now):
    path = root / "lane-health" / "mgr" / f"{lane}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"lane": lane, "last_scan": now - age_sec}))


def test_a_lane_that_stopped_scanning_is_paged(sm_root):
    """Two consecutive stale observations, then the page."""
    now = 10_000.0
    _hb(sm_root, "done", age_sec=3600, now=now)
    pages, _, _ = _run_scans(sm_root, {}, now, 2)
    assert pages[0] == [], "paged on the FIRST observation — a freeze looks like this"
    assert [k for k, _ in pages[1]] == ["lane_silent:done"]
    assert "LANE_SILENT done" in pages[1][0][1]
    assert "NOT reaching you" in pages[1][0][1]


def test_a_freeze_shorter_than_the_observer_gap_check_still_does_not_page(sm_root):
    """The band the gap check cannot see.

    A 100s freeze clears the observer's own 180s threshold, but it is far past
    the 6s window of the 2s lanes, so every one of them reads stale. One
    observation is not evidence; the second, taken at a normal cadence after
    the lanes resumed, shows them fresh.
    """
    now = 10_000.0
    for lane in stale_monitor.LANE_INTERVALS:
        _hb(sm_root, lane, age_sec=1, now=now)
    nxt = {}
    stale_monitor._lane_silence_events("mgr", {}, nxt, now)

    frozen = now + 100                       # the observer was away 100s
    assert stale_monitor._lane_silence_events("mgr", dict(nxt), {}, frozen) == [], (
        "paged during a sub-threshold freeze, on a single stale observation")


def test_a_healthy_lane_is_silent(sm_root):
    now = 10_000.0
    _hb(sm_root, "done", age_sec=1, now=now)
    assert stale_monitor._lane_silence_events("mgr", {}, {}, now) == []


def test_a_never_armed_lane_is_NOT_paged(sm_root):
    """At boot the lanes are armed seconds after this scan could first run, and
    a manager that never arms lanes would be paged forever. 'Never armed' is a
    question for `dockwright lanes`; this reports only the incident shape.

    Driven for THREE rounds, not one: with the two-observation rule a single
    call returns nothing for any lane, so a one-shot assertion would pass even
    with the never-armed guard deleted."""
    now = 10_000.0
    pages, _, _ = _run_scans(sm_root, {}, now, 3)
    assert not any(pages), f"a never-armed lane paged: {pages}"


def _run_scans(root, emitted, start, count, step=60.0):
    """Drive the check the way the stale lane really does — once per cadence.

    Calling it twice ten minutes apart would skip the intervening scans, and
    the suspend detector would correctly read that as a suspended host. The
    ladder must be exercised against a running clock, not a teleporting one.
    """
    pages = []
    now = start
    for _ in range(count):
        nxt = {}
        pages.append(stale_monitor._lane_silence_events("mgr", emitted, nxt, now))
        emitted = dict(nxt)
        now += step
    return pages, emitted, now


def test_repeats_are_laddered_not_every_scan(sm_root):
    """This check must not become the noise it is warning about."""
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
    """A 20-minute suspend freezes all four lanes at once and hands the next
    scan a jumped clock. Paging then tells the manager to kill four healthy
    loops — the false positive that teaches a reader to ignore the real one.
    This scan's own cadence is the evidence: the gap is the tell."""
    now = 10_000.0
    for lane in stale_monitor.LANE_INTERVALS:
        _hb(sm_root, lane, age_sec=1, now=now)
    nxt = {}
    assert stale_monitor._lane_silence_events("mgr", {}, nxt, now) == []

    resumed = now + 20 * 60           # host asleep for 20 minutes
    assert stale_monitor._lane_silence_events("mgr", dict(nxt), {}, resumed) == [], (
        "paged about every lane after a host suspend")


def test_the_suspend_guard_costs_only_one_cycle(sm_root):
    """It must not become a way for a genuinely dead lane to hide forever."""
    now = 10_000.0
    _hb(sm_root, "done", age_sec=3600, now=now)
    _, emitted, clock = _run_scans(sm_root, {}, now, 2)   # confirm + page
    resumed = clock + 20 * 60
    after_suspend = {}
    assert stale_monitor._lane_silence_events(
        "mgr", dict(emitted), after_suspend, resumed) == [], "paged through the suspend"
    pages, _, _ = _run_scans(sm_root, dict(after_suspend), resumed + 60, 12)
    assert any(pages), (
        "the dead lane stayed hidden after the suspend cycle passed")


def test_the_check_never_reports_the_lane_doing_the_reporting(sm_root):
    """`stale`'s heartbeat is written at the END of this scan, so it is always
    one cadence old here. A page whose own delivery disproves it is noise."""
    # A clock large enough that `now - age` stays positive: a non-positive
    # last_scan reads as "never armed" and is skipped by design.
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

    _hb(sm_root, "done", age_sec=1, now=now)      # lane re-armed
    fresh = {}
    assert stale_monitor._lane_silence_events("mgr", dict(emitted), fresh, now) == []
    assert "lane_stale_seen:done" not in fresh, (
        "the confirmation marker survived recovery, so the next outage would "
        "page on a single observation")
    assert "lane_silent:done" not in fresh, (
        "a recovered lane kept its burnt rung, so the NEXT outage would be "
        "held instead of paging immediately")


def test_a_broken_heartbeat_file_cannot_break_the_scan(sm_root):
    """This is a safety net over a signal that already fired. It must never be
    the thing that kills the scan carrying the real alarms."""
    now = 10_000.0
    path = sm_root / "lane-health" / "mgr" / "done.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert stale_monitor._lane_silence_events("mgr", {}, {}, now) == []


# A3 — the traversal guard landed in paths._event_bucket; the standalone copies
# must agree with it, or the two halves disagree about a legal bucket name and
# a manager's events land in one place while its heartbeat lands in another.

@pytest.mark.parametrize("name", ["..", ".", "a/b", "a\\b", "normal-name",
                                  "../../etc", "_unresolved"])
def test_the_bucket_sanitizer_matches_the_canonical_one(name):
    from dockwright import paths
    assert stale_monitor._safe_bucket(name) == paths._event_bucket(name)


@pytest.mark.parametrize("module_name", ["stale_monitor", "monitor"])
def test_no_path_helper_sanitizes_a_name_inline(module_name):
    """ADD-ONE in the shape that actually happens: a NEW path helper with its
    own inline `.replace()` pair, which would carry no '.'/'..' guard and
    disagree with every other copy about a legal bucket name.

    Both lane modules, derived by parsing — an earlier version walked only
    stale_monitor and left the copies in monitor.py unguarded, which is the
    same hand-maintained-list failure one level up.
    """
    import ast
    import importlib
    import pathlib
    module = importlib.import_module(f"dockwright.{module_name}")
    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name in ("_safe_bucket", "_event_bucket"):
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "replace"
                    and len(call.args) == 2
                    and isinstance(call.args[0], ast.Constant)
                    and call.args[0].value in ("/", "\\")):
                offenders.append(f"{node.name}:{call.lineno}")
    assert offenders == [], (
        f"these {module_name} functions sanitize a name inline instead of via "
        f"the shared bucket helper, so they carry no '.'/'..' guard and "
        f"disagree with the other copies: {offenders}")


def test_a_marker_set_before_a_freeze_does_not_page_on_resume(sm_root):
    """Where the observer-gap check earns its place, separately from the
    two-observation rule.

    The two rules cover different paths and a mutation sweep showed it: with a
    CLEAN slate the two-observation rule alone absorbs a suspend, so deleting
    the gap check looked harmless. But when a lane was already marked stale
    once BEFORE the freeze, the round after resume is the second observation —
    and it would page on evidence gathered across a gap in which nothing was
    actually observed. The gap check is what refuses that second observation.
    """
    now = 10_000.0
    for lane in stale_monitor.LANE_INTERVALS:
        _hb(sm_root, lane, age_sec=1, now=now)

    # One legitimately stale observation for `done`, at a normal cadence.
    _hb(sm_root, "done", age_sec=3600, now=now)
    first = {}
    assert stale_monitor._lane_silence_events("mgr", {}, first, now) == []
    assert "lane_stale_seen:done" in first, "precondition: the marker was set"

    # The host freezes. On resume the observer runs BEFORE the lanes have had
    # a chance to write their next heartbeat, so every one still reads stale —
    # this round is the second observation on paper, but nothing was observed
    # during the gap.
    resumed = now + 20 * 60
    assert stale_monitor._lane_silence_events(
        "mgr", dict(first), {}, resumed) == [], (
        "paged on a second observation that was never actually taken — the "
        "gap across the freeze is not evidence")
