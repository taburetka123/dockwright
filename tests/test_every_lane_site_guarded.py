"""Per-site proof that no lane commits what it did not deliver.

The mutation sweep read 23/23 RED while three consuming sites had no
behavioural guard at all, because the only end-to-end fixture drove the `done`
lane. A sweep proves the sites it exercises; it says nothing about the ones it
does not reach, and "applied at every consuming site" was a claim about the
code rather than about the tests.

One test per site, each asserting the same property against a broken reader:
**nothing durable moves.** Each was watched RED with the site's flush or
ordering reverted.
"""
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


# --- site: turn-ends -------------------------------------------------------

def test_turn_ends_commits_neither_cursor_nor_ladder_to_a_dead_reader(
        lane_state, monkeypatch):
    """The most intricate scan in the file, and the one the sweep never drove.

    It commits TWO things — the seen-cursor and the FS emit ladder — and the
    ladder is recorded inside the emit loop, so a naive ordering burns a rung
    for a page nobody saw. A burnt rung is worse than a lost line: it silences
    the NEXT page for that worker too, for up to four hours.
    """
    bucket = lane_state / "turn-ends" / "mgr"
    bucket.mkdir(parents=True)
    # Old enough to clear the turn-end grace, with no active record, so the
    # classifier reaches EMIT_EXITED rather than holding it PENDING.
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


# --- site: monitor._drain_notify_outbox ------------------------------------

def test_the_outbox_drain_does_not_unlink_what_it_could_not_deliver(
        lane_state, monkeypatch):
    """Unlink-before-emit destroys the entry outright: the outbox IS the
    durable copy, so there is nothing to replay from."""
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
    """The other direction — otherwise 'never unlink' would pass this file."""
    outbox = paths.notify_outbox_dir_for("mgr")
    outbox.mkdir(parents=True)
    entry = outbox / "1-1-0.json"
    entry.write_text(json.dumps({"line": "AUTOCLOSED w1", "kind": "autoclosed"}))

    monitor._drain_notify_outbox("mgr")
    assert "AUTOCLOSED w1" in capsys.readouterr().out
    assert not entry.exists(), "a delivered entry was left to replay forever"


# --- site: stale_monitor._emit --------------------------------------------

def test_the_standalone_emit_flushes(monkeypatch):
    """`stale_monitor` keeps its own copy of the helper because it ships
    standalone. A copy that writes without flushing defers the failure to
    interpreter exit exactly as the original bug did — and its caller has
    already advanced the ladder by then."""
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
    """Kept alongside the others so the file covers every lane, not just the
    ones that were broken."""
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


def test_this_file_covers_every_lane():
    """Derived from the canonical set: a fifth lane makes this fail rather
    than silently going unexercised, which is how the three sites above were
    missed in the first place."""
    covered = {"questions", "done", "turn-ends", "stale"}
    assert covered == set(lane_io.LANES), (
        f"lanes without a per-site delivery guard in this file: "
        f"{set(lane_io.LANES) - covered}")


def test_no_diverted_line_is_emitted_without_a_durable_copy():
    """Every divert-kind line must reach disk BEFORE stdout.

    The `printed_any` branch used to emit diverted lines straight out, on the
    reasoning that a wake was already happening. That left them with no durable
    copy: a reader dying mid-burst destroyed an AUTOCLOSED whose worker was
    already archived and whose pane was already closed — no cursor, no replay.

    ⚠️ WHAT THIS CATCHES, MEASURED — do not read it as a general guard.
    It matches ONE SHAPE: an `_emit` inside a `for … in diverted` loop. Six
    legal rewrites of the drain were tried and the results are:

        for-loop over `diverted`      caught
        while-loop over an index      MISSED
        list comprehension            MISSED
        map()                         MISSED
        a helper called with the list MISSED
        the variable renamed          MISSED

    So it is a tripwire on the exact regression that occurred, not a property
    check. **The real coverage is behavioural and lives elsewhere**:
    `test_stale_monitor.py::test_autoclosed_diverts_to_outbox_when_scan_otherwise_silent`
    and `::test_outbox_write_failure_falls_back_to_print` caught all six.

    Making THIS assertion behavioural needs `_flush_events(...)` extracted from
    a ~900-line `main()` that it closes a dozen locals over — attempted in this
    PR, reverted at 61 broken tests, deferred by the operator to its own change.
    Until then the two tests named above are what hold the property, and
    test_the_behavioural_coverage_still_exists below fails if they are removed.
    """
    import ast
    import pathlib

    source = pathlib.Path(stale_monitor.__file__).read_text(encoding="utf-8")
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.For):
            continue
        iterated = ast.unparse(node.iter)
        if "diverted" not in iterated:
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id == "_emit"):
                offenders.append(f"line {inner.lineno}: _emit over {iterated}")
    assert offenders == [], (
        "a diverted line is emitted directly instead of via _drain_outbox, so "
        f"it has no durable copy if the reader dies: {offenders}")


def test_the_behavioural_coverage_still_exists():
    """The tripwire above is blind to five of six rewrites; these two tests are
    what actually hold the property. If they are ever deleted or renamed, the
    property is unguarded and the docstring above becomes a lie — so their
    absence has to fail something."""
    import pathlib

    source = pathlib.Path(__file__).resolve().parents[0] / "test_stale_monitor.py"
    text = source.read_text(encoding="utf-8")
    for name in ("test_autoclosed_diverts_to_outbox_when_scan_otherwise_silent",
                 "test_outbox_write_failure_falls_back_to_print"):
        assert f"def {name}(" in text, (
            f"{name} is gone — it was the behavioural coverage for the "
            f"divert-before-emit property, which the structural tripwire in "
            f"this file cannot replace")


def test_the_structural_guard_would_notice_a_direct_emit():
    """ADD-ONE: prove the parser fires on the shape it forbids."""
    import ast

    source = ("for _k, _n, line, _d in diverted:\n"
              "    _emit(line)\n")
    hits = [n for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.For) and "diverted" in ast.unparse(node.iter)
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "_emit"]
    assert hits, "the structural guard cannot see a direct emit over diverted"


# The other half of every site: a lane that DELIVERS must also prove it is
# alive. Deleting the terminal write_heartbeat from `questions` and from
# `turn-ends` was GREEN on the full suite — the end-to-end fixture drove `done`
# only, and the `==` pin on the lane table does not help, because the table is
# not what lets `dockwright lanes` see a lane. A heartbeat is.

@pytest.mark.parametrize("lane", sorted(lane_io.LANES))
def test_every_lane_writes_a_heartbeat_on_a_healthy_scan(lane, lane_state, monkeypatch):
    """Parametrized over the canonical set, so a fifth lane is covered by
    construction rather than by someone remembering to add a case."""
    if lane == "stale":
        # Its scan is a child process that writes its own heartbeat; the parent
        # deliberately does not. Drive the child's helper directly rather than
        # spawning, and assert the same file appears.
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
    """Nothing to emit is the COMMON case. A lane that only heartbeats when it
    has news would read as dead across every quiet stretch — which is exactly
    the confusion this whole change removes."""
    scan = getattr(monitor, monitor._SCANS[lane])
    scan(dict(MGR))
    record = json.loads(lane_io.heartbeat_path("mgr", lane).read_text())
    assert record["last_emit"] is None, "precondition: nothing was emitted"
    assert record["last_scan"] > 0
