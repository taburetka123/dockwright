"""Delivery discipline for monitor lanes.

Guards the property the lane-death incident violated:

  No lane may commit consumption of an event it has not proven it delivered,
  and a lane that can no longer deliver must end loudly rather than run
  silently.

Every test here was watched RED against the pre-fix tree — see
docs/specs/2026-08-06-monitor-lane-liveness-design.md.
"""
import json
import os
import pathlib
import pty
import select
import socket
import subprocess
import sys
import time

import pytest

from dockwright import lane_io


# --------------------------------------------------------------------------
# reader_is_dead: the probe must separate "gone" from "busy". A live reader
# that is merely backpressured looks identical to a dead one on a naive
# writability check, and mistaking it for dead would kill healthy lanes under
# load — the exact opposite of the bug being fixed.
# --------------------------------------------------------------------------

def test_probe_live_pipe_is_not_dead():
    r, w = os.pipe()
    try:
        assert lane_io.reader_is_dead(w) is False
    finally:
        os.close(r)
        os.close(w)


def test_probe_closed_pipe_reader_is_dead():
    r, w = os.pipe()
    os.close(r)
    try:
        assert lane_io.reader_is_dead(w) is True
    finally:
        os.close(w)


def test_probe_live_socketpair_is_not_dead():
    a, b = socket.socketpair()
    try:
        assert lane_io.reader_is_dead(a.fileno()) is False
    finally:
        a.close()
        b.close()


def test_probe_closed_socket_peer_is_dead():
    a, b = socket.socketpair()
    b.close()
    try:
        assert lane_io.reader_is_dead(a.fileno()) is True
    finally:
        a.close()


def test_probe_tty_is_not_dead():
    master, slave = pty.openpty()
    try:
        assert lane_io.reader_is_dead(slave) is False
    finally:
        os.close(master)
        os.close(slave)


def test_probe_regular_file_is_not_dead(tmp_path):
    target = tmp_path / "out"
    with target.open("w") as handle:
        assert lane_io.reader_is_dead(handle.fileno()) is False


def test_probe_backpressured_reader_is_not_dead():
    """A live reader that stopped draining is BUSY, not gone.

    This is the false-positive that would turn the guard into an outage
    generator: under a burst the socket buffer fills, POLLOUT clears, and a
    naive "not writable => dead" probe would kill every healthy lane at once.
    """
    a, b = socket.socketpair()
    a.setblocking(False)
    try:
        while True:
            a.send(b"x" * 65536)
    except BlockingIOError:
        pass
    try:
        assert lane_io.reader_is_dead(a.fileno()) is False
    finally:
        a.close()
        b.close()


def test_probe_fails_open_on_a_bad_fd():
    """A probe quirk must never kill a lane: unknown fd reads as alive."""
    assert lane_io.reader_is_dead(-1) is False


# --------------------------------------------------------------------------
# emit: flush per line, so a delivery failure is raised HERE instead of being
# deferred to interpreter exit and swallowed as "Exception ignored".
# --------------------------------------------------------------------------

def test_emit_writes_and_flushes_immediately(tmp_path, monkeypatch, capsys):
    lane_io.emit("hello")
    assert capsys.readouterr().out == "hello\n"


class _DeadStdout:
    """Stands in for a stdout whose reader hung up.

    A real fd is used by the subprocess tests at the bottom of this file; here
    the point is only that emit() converts the OS error into LaneDead rather
    than letting it escape (or, worse, deferring it to interpreter exit).
    """

    def __init__(self, error):
        self.error = error

    def write(self, _text):
        raise self.error

    def flush(self):
        raise self.error


@pytest.mark.parametrize("error", [
    BrokenPipeError(32, "Broken pipe"),
    OSError(5, "Input/output error"),
])
def test_emit_raises_lane_dead_when_the_write_fails(monkeypatch, error):
    monkeypatch.setattr(sys, "stdout", _DeadStdout(error))
    with pytest.raises(lane_io.LaneDead):
        lane_io.emit("into the void")


def test_emit_raises_lane_dead_when_only_the_flush_fails(monkeypatch):
    """The flush is where the old bug hid: the write succeeded into a buffer
    and only the flush failed, at interpreter exit, where Python swallowed it."""
    class _FlushFails(_DeadStdout):
        def write(self, _text):
            return None

    monkeypatch.setattr(sys, "stdout", _FlushFails(BrokenPipeError(32, "Broken pipe")))
    with pytest.raises(lane_io.LaneDead):
        lane_io.emit("buffered but never delivered")


def test_preflight_raises_on_a_dead_reader(monkeypatch):
    monkeypatch.setattr(lane_io, "reader_is_dead", lambda fd=1: True)
    with pytest.raises(lane_io.LaneDead):
        lane_io.preflight()


def test_preflight_is_silent_on_a_live_reader(monkeypatch):
    monkeypatch.setattr(lane_io, "reader_is_dead", lambda fd=1: False)
    lane_io.preflight()


# --------------------------------------------------------------------------
# The canonical lane set. `dockwright lanes` must cover every lane `monitor`
# dispatches, and it must do so by ITERATING the shared definition rather than
# by carrying a second hand-maintained list that the next lane silently misses.
# --------------------------------------------------------------------------

def test_monitor_subcommands_are_derived_from_the_canonical_lane_set():
    from dockwright import monitor
    assert tuple(lane_io.LANES) == monitor._MONITOR_SUBCOMMANDS


def test_every_lane_declares_a_positive_poll_interval():
    assert lane_io.LANES
    for lane, interval in lane_io.LANES.items():
        assert isinstance(interval, int) and interval > 0, lane


# --------------------------------------------------------------------------
# Heartbeat. Written only by a scan that passed preflight AND flushed every
# line it emitted, so it cannot tick while the lane is broken.
# --------------------------------------------------------------------------

def test_heartbeat_records_scan_time_and_carries_last_emit_forward(tmp_path, monkeypatch):
    monkeypatch.setattr(lane_io.paths, "LANE_HEALTH", tmp_path / "lane-health")
    lane_io.write_heartbeat("mgr", "done", emitted=True, now=100.0)
    lane_io.write_heartbeat("mgr", "done", emitted=False, now=200.0)
    record = json.loads(lane_io.heartbeat_path("mgr", "done").read_text())
    assert record["last_scan"] == 200.0
    assert record["last_emit"] == 100.0, (
        "a quiet scan must not erase the lane's last real delivery")
    assert record["lane"] == "done"
    assert record["interval_hint"] == lane_io.LANES["done"]
    assert record["pid"] == os.getpid()


@pytest.mark.parametrize("hostile", ["evil/../name", "a/b/c", "..", "x\\y"])
def test_heartbeat_cannot_escape_the_lane_health_root(tmp_path, monkeypatch, hostile):
    """A manager name reaches this from a state record, so treat it as input:
    the write must land inside lane-health/ whatever the name contains."""
    root = tmp_path / "lane-health"
    monkeypatch.setattr(lane_io.paths, "LANE_HEALTH", root)
    lane_io.write_heartbeat(hostile, "done", emitted=False, now=1.0)
    written = list(root.rglob("*.json"))
    assert len(written) == 1
    assert written[0].resolve().is_relative_to(root.resolve())
    assert written[0].parent.parent.resolve() == root.resolve(), (
        "the heartbeat nested deeper than lane-health/<manager>/")


# --------------------------------------------------------------------------
# End-to-end: the incident, reproduced. A real `dockwright monitor` process
# whose stdout reader is gone must consume NOTHING and exit EXIT_LANE_DEAD.
# Against the pre-fix tree this test fails on both halves: the cursor names the
# event and the exit code is 120.
# --------------------------------------------------------------------------

@pytest.fixture
def lane_sandbox(tmp_path):
    """An isolated state root reachable by a child process via DOCKWRIGHT_CONFIG."""
    state_root = tmp_path / "state"
    (state_root / "active").mkdir(parents=True)
    (state_root / "done" / "testmgr").mkdir(parents=True)
    (tmp_path / "dockwright.toml").write_text(
        f'[paths]\nstate_root = "{state_root}"\n')
    (state_root / "active" / "sid-mgr.json").write_text(json.dumps({
        "agent": "manager", "name": "testmgr", "claude_sid": "sid-mgr",
        "pid": 999999, "window_id": "%99999",
    }))
    (state_root / "done" / "testmgr" / "sid-w-e1.json").write_text(json.dumps({
        "worker_name": "repro-worker", "claude_sid": "sid-w",
        "summary": "the night's most important fact", "completed_at": 1,
    }))
    return tmp_path, state_root


# The child must import the tree THIS TEST FILE belongs to. `-m dockwright`
# otherwise resolves through the editable install, so a subprocess test run
# from a copied tree silently exercises the original — which makes it blind to
# any change in the copy. A mutation sweep caught exactly that: mutating the
# CLI's exit code left this file's assertions green because the child was
# running unmutated code. PYTHONPATH here binds the child to the tree under
# test; it is not the "borrow a sibling venv" anti-pattern.
_TREE_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


def _run_monitor(config_dir, *, stdout, args=("monitor", "done", "testmgr")):
    env = dict(os.environ,
               DOCKWRIGHT_CONFIG=str(config_dir / "dockwright.toml"),
               PYTHONPATH=str(_TREE_SRC))
    return subprocess.run(
        [sys.executable, "-m", "dockwright", *args],
        stdout=stdout, stderr=subprocess.PIPE, env=env, check=False)


def test_the_child_process_runs_the_tree_under_test(lane_sandbox):
    """Without this, every subprocess assertion below could be describing a
    different checkout — the failure mode is invisible because the other tree
    is usually correct."""
    config_dir, _ = lane_sandbox
    result = subprocess.run(
        [sys.executable, "-c", "import dockwright; print(dockwright.__file__)"],
        env=dict(os.environ, PYTHONPATH=str(_TREE_SRC)),
        capture_output=True, text=True, check=False)
    assert result.stdout.strip().startswith(str(_TREE_SRC)), (
        f"the child imported {result.stdout.strip()!r}, not this tree's "
        f"{_TREE_SRC}")


def test_dead_reader_consumes_nothing_and_exits_lane_dead(lane_sandbox):
    config_dir, state_root = lane_sandbox
    r, w = os.pipe()
    os.close(r)                      # reader gone before the child can write
    try:
        result = _run_monitor(config_dir, stdout=w)
    finally:
        os.close(w)
    cursor = state_root / ".seen-done-testmgr"
    assert result.returncode == lane_io.EXIT_LANE_DEAD, (
        f"expected EXIT_LANE_DEAD, got {result.returncode} "
        f"(120 = the old swallowed BrokenPipeError); stderr={result.stderr!r}")
    assert not cursor.exists() or "sid-w-e1.json" not in cursor.read_text(), (
        "the event was marked delivered although it never reached a reader")
    assert not (state_root / "lane-health" / "testmgr" / "done.json").exists(), (
        "the heartbeat ticked for a scan that delivered nothing")


def test_event_survives_a_dead_scan_and_lands_on_the_next_healthy_one(lane_sandbox):
    config_dir, state_root = lane_sandbox
    r, w = os.pipe()
    os.close(r)
    try:
        _run_monitor(config_dir, stdout=w)
    finally:
        os.close(w)
    healthy = _run_monitor(config_dir, stdout=subprocess.PIPE)
    assert healthy.returncode == 0
    assert b"the night's most important fact" in healthy.stdout, (
        "an event lost to a dead reader must replay, not vanish")


class _FlushOnlyFails(_DeadStdout):
    """Write succeeds into a buffer; only the flush fails.

    This is the REAL shape of the incident — the line never hit the fd, the
    failure was deferred, and the scan committed the cursor in between. A stub
    that raises on write() cannot distinguish a missing flush from a present
    one, so it proves nothing about the flush.
    """

    def write(self, _text):
        return None


@pytest.mark.parametrize("stdout_factory", [_DeadStdout, _FlushOnlyFails],
                         ids=["write-fails", "flush-only-fails"])
def test_emit_is_the_authoritative_guard_when_the_probe_fails_open(
        tmp_path, monkeypatch, stdout_factory):
    """Isolates the EMIT layer from the probe layer.

    The probe is documented to fail OPEN, so the guard that actually has to
    hold is emit(). The subprocess tests are caught by preflight and therefore
    prove nothing about it — this one disables the probe, breaks delivery, and
    asserts the cursor still does not advance. Remove the flush from emit() and
    the `flush-only-fails` case goes red.
    """
    from dockwright import monitor, paths, state

    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "DONE", tmp_path / "done")
    monkeypatch.setattr(paths, "LANE_HEALTH", tmp_path / "lane-health")
    bucket = tmp_path / "done" / "mgr"
    bucket.mkdir(parents=True)
    state.write_json_atomic(bucket / "sid-1.json",
                            {"worker_name": "w", "summary": "must not vanish"})

    monkeypatch.setattr(lane_io, "reader_is_dead", lambda fd=1: False)
    monkeypatch.setattr(sys, "stdout",
                        stdout_factory(BrokenPipeError(32, "Broken pipe")))

    with pytest.raises(lane_io.LaneDead):
        monitor.run_done_scan({"name": "mgr", "sid": "mgr-sid"})

    cursor = tmp_path / ".seen-done-mgr"
    assert not cursor.exists() or "sid-1.json" not in cursor.read_text(), (
        "the cursor advanced for a line whose delivery failed")
    assert not lane_io.heartbeat_path("mgr", "done").exists(), (
        "the heartbeat ticked for a scan that delivered nothing")


def _exit_code_of(body: str) -> int:
    """Run `body` in a child whose stdout is a pipe with no reader."""
    script = ("import sys, os\n"
              "sys.path.insert(0, %r)\n"
              "from dockwright import lane_io\n" % str(
                  __import__("pathlib").Path(__file__).resolve().parents[1] / "src")
              + body)
    r, w = os.pipe()
    os.close(r)
    try:
        return subprocess.run([sys.executable, "-c", script],
                              stdout=w, stderr=subprocess.DEVNULL,
                              check=False).returncode
    finally:
        os.close(w)


def test_detach_stdout_keeps_the_chosen_exit_code():
    """Without it the shutdown flush overrides the exit status with 120.

    That matters because 120 is indistinguishable from the pre-fix behaviour:
    the lane would still end, but the REASON would be destroyed on its way out,
    which is the failure this whole change is about. The control case below
    measures the override rather than asserting it from memory.
    """
    control = _exit_code_of(
        "sys.stdout.write('x' * 10)\n"
        "sys.exit(lane_io.EXIT_LANE_DEAD)\n")
    assert control == 120, (
        f"expected the swallowed-flush override, got {control}; if CPython "
        f"stopped doing this, detach_stdout may no longer be needed")

    guarded = _exit_code_of(
        "sys.stdout.write('x' * 10)\n"
        "lane_io.detach_stdout()\n"
        "sys.exit(lane_io.EXIT_LANE_DEAD)\n")
    assert guarded == lane_io.EXIT_LANE_DEAD, (
        f"detach_stdout did not protect the exit code (got {guarded})")


# --------------------------------------------------------------------------
# A transient fault must not kill a healthy lane, and a persistent one must
# still end it. The wrapper now ends the lane on ANY non-zero exit, so getting
# this wrong trades the old silent-death bug for a noisy-death bug.
# --------------------------------------------------------------------------

def _wedge_run(config_dir, *, times):
    """Run the done scan `times` times with a scan that always raises."""
    codes = []
    for _ in range(times):
        env = dict(os.environ,
                   DOCKWRIGHT_CONFIG=str(config_dir / "dockwright.toml"),
                   PYTHONPATH=str(_TREE_SRC))
        script = (
            "import sys\n"
            "from dockwright import monitor\n"
            "monitor.run_done_scan = lambda mgr=None: (_ for _ in ()).throw("
            "OSError(5, 'transient I/O error'))\n"
            "monitor.main(['done', 'testmgr'])\n")
        codes.append(subprocess.run(
            [sys.executable, "-c", script], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False).returncode)
    return codes


def test_a_transient_scan_failure_keeps_the_lane_alive(lane_sandbox):
    config_dir, _ = lane_sandbox
    codes = _wedge_run(config_dir, times=1)
    assert codes == [0], (
        f"one transient failure ended the lane (exit {codes}); the wrapper "
        f"treats any non-zero as death, so this permanently deafens a healthy "
        f"manager over a momentary I/O error")


def test_a_persistent_scan_failure_eventually_ends_the_lane(lane_sandbox):
    config_dir, state_root = lane_sandbox
    codes = _wedge_run(config_dir, times=lane_io.max_consecutive_errors("done"))
    assert codes[:-1] == [0] * (lane_io.max_consecutive_errors("done") - 1)
    assert codes[-1] == lane_io.EXIT_LANE_WEDGED, (
        f"a lane crash-looping forever is the original defect in a new shape; "
        f"got {codes}")
    record = json.loads(
        (state_root / "lane-health" / "testmgr" / "done.json").read_text())
    assert record["consecutive_errors"] == lane_io.max_consecutive_errors("done")


def test_a_failing_scan_does_not_refresh_the_heartbeat(lane_sandbox):
    """The error counter must not double as a liveness signal — a scan that
    failed has proved nothing about delivery, so `last_scan` must keep ageing
    and `dockwright lanes` must keep seeing it go stale."""
    config_dir, state_root = lane_sandbox
    _run_monitor(config_dir, stdout=subprocess.PIPE)          # one healthy scan
    hb = state_root / "lane-health" / "testmgr" / "done.json"
    before = json.loads(hb.read_text())["last_scan"]
    _wedge_run(config_dir, times=2)
    after = json.loads(hb.read_text())
    assert after["last_scan"] == before, "a failed scan refreshed the heartbeat"
    assert after["consecutive_errors"] == 2


def test_a_successful_scan_clears_the_error_run(lane_sandbox):
    config_dir, state_root = lane_sandbox
    _wedge_run(config_dir, times=lane_io.max_consecutive_errors("done") - 1)
    hb = state_root / "lane-health" / "testmgr" / "done.json"
    assert json.loads(hb.read_text())["consecutive_errors"] > 0
    assert _run_monitor(config_dir, stdout=subprocess.PIPE).returncode == 0
    assert json.loads(hb.read_text())["consecutive_errors"] == 0, (
        "one good scan between bad ones must forget the run — only "
        "CONSECUTIVE failures mean the lane is wedged")


def test_an_unwritable_state_root_ends_the_lane_instead_of_spinning(
        tmp_path, monkeypatch):
    """The counter cannot be the thing that keeps a doomed lane alive.

    It lives under the same state root as the cursor, so if it cannot be
    written the lane cannot mark anything seen either — it fails every scan,
    and a count that never advances never reaches the cap. Returning a low
    number here would restore crash-loop-forever, the original defect wearing
    a different hat.
    """
    monkeypatch.setattr(lane_io.paths, "LANE_HEALTH", tmp_path / "lane-health")

    def _refuse(*_a, **_kw):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(lane_io.state, "write_json_atomic", _refuse)
    assert lane_io.record_scan_error("mgr", "done") >= \
        lane_io.max_consecutive_errors("done")


def test_healthy_scan_commits_the_cursor_and_writes_the_heartbeat(lane_sandbox):
    config_dir, state_root = lane_sandbox
    result = _run_monitor(config_dir, stdout=subprocess.PIPE)
    assert result.returncode == 0
    assert "sid-w-e1.json" in (state_root / ".seen-done-testmgr").read_text()
    record = json.loads(
        (state_root / "lane-health" / "testmgr" / "done.json").read_text())
    assert record["last_emit"] is not None
    assert time.time() - record["last_scan"] < 120


# --------------------------------------------------------------------------
# Exit paths that bypass the retry ladder. All three were found by an
# adversarial reviewer, not by the sweep — a mutation set only contains the
# sites the author already knew about.
# --------------------------------------------------------------------------

def test_identity_failure_that_is_not_a_deliberate_exit_is_laddered(
        lane_sandbox, monkeypatch):
    """`resolve_manager` deliberately `sys.exit(2)`s when the owning manager is
    gone — that is the orphan signal and must stay fatal. But an UNREADABLE
    `active/` dir raises PermissionError straight through it (`is_dir()` is
    True for a dir you cannot read, and `list_json_in`'s `iterdir` has no
    guard). Resolution used to sit outside the try, so that transient ended a
    healthy lane on the first occurrence.
    """
    config_dir, state_root = lane_sandbox
    env = dict(os.environ,
               DOCKWRIGHT_CONFIG=str(config_dir / "dockwright.toml"),
               PYTHONPATH=str(_TREE_SRC))
    script = ("import sys\n"
              "from dockwright import monitor\n"
              "def boom(): raise PermissionError(13, 'Permission denied')\n"
              "monitor._resolve = boom\n"
              "monitor.main(['done'])\n")
    codes = [subprocess.run([sys.executable, "-c", script], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            check=False).returncode
             for _ in range(lane_io.max_consecutive_errors("done"))]
    assert codes[0] == 0, (
        f"one unreadable-active/ blip ended the lane (exit {codes[0]}); "
        f"resolution is outside the retry ladder")
    assert codes[-1] == lane_io.EXIT_LANE_WEDGED, (
        f"a PERSISTENT resolution failure never ends the lane: {codes}")
    counter = state_root / "lane-errors-unresolved-done.json"
    assert counter.exists(), (
        "the error run was not recorded anywhere — with no manager name there "
        "is nothing to charge it to unless the sibling file is used")
    assert not list((state_root / "lane-health").glob("*unresolved*")), (
        "the counter is back inside the per-manager namespace, where a "
        "manager named into the collision would share it")


def _stale_scan_with_child_code(code, monkeypatch, tmp_path):
    from dockwright import monitor, paths

    monkeypatch.setattr(paths, "LANE_HEALTH", tmp_path / "lane-health")
    monkeypatch.setattr(lane_io, "reader_is_dead", lambda fd=1: False)

    class _Result:
        returncode = code

    monkeypatch.setattr(monitor.subprocess, "run", lambda *a, **k: _Result())
    return monitor


@pytest.mark.parametrize("code", [1, 2, 70])
def test_a_stale_child_failing_transiently_is_laddered_not_fatal(
        code, monkeypatch, tmp_path):
    """`sys.exit(child_code)` raises SystemExit, which main() re-raises PAST
    the ladder — so blanket-forwarding ended the lane with the largest failure
    surface on its first hiccup. A `-m dockwright.stale_monitor` import failure
    during a `pip install -e .` would have ended every stale lane at once.
    """
    monitor = _stale_scan_with_child_code(code, monkeypatch, tmp_path)
    with pytest.raises(RuntimeError):
        monitor.run_stale_scan({"name": "mgr", "sid": "s"})


def test_a_stale_child_reporting_lane_dead_is_fatal(monkeypatch, tmp_path):
    monitor = _stale_scan_with_child_code(
        lane_io.EXIT_LANE_DEAD, monkeypatch, tmp_path)
    with pytest.raises(lane_io.LaneDead):
        monitor.run_stale_scan({"name": "mgr", "sid": "s"})


def test_a_stale_child_reporting_wedged_is_not_re_laddered(monkeypatch, tmp_path):
    """The child ran its own ladder; re-laddering its verdict would multiply
    the two caps together."""
    monitor = _stale_scan_with_child_code(
        lane_io.EXIT_LANE_WEDGED, monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as exc:
        monitor.run_stale_scan({"name": "mgr", "sid": "s"})
    assert exc.value.code == lane_io.EXIT_LANE_WEDGED


def test_the_parent_does_not_stamp_the_stale_heartbeat(monkeypatch, tmp_path):
    """It would certify the child's exit CODE, not delivery — and `stale` has
    no backlog arm to cross-check that proxy against. The child writes it."""
    monitor = _stale_scan_with_child_code(0, monkeypatch, tmp_path)
    monitor.run_stale_scan({"name": "mgr", "sid": "s"})
    assert not lane_io.heartbeat_path("mgr", "stale").exists(), (
        "the parent stamped a heartbeat for emits it never performed")


def test_the_orphan_signal_survives_the_retry_ladder(lane_sandbox):
    """Moving identity resolution INSIDE the try must not swallow exit 2.

    Exit 2 is what makes an orphaned loop terminate itself instead of scanning
    for days — the durable half of this change. It survives structurally
    (`SystemExit` derives from `BaseException`, so `except Exception` cannot
    catch it) but that is a property of the language, not of this file, so it
    is pinned here rather than argued in a comment.
    """
    config_dir, state_root = lane_sandbox
    (state_root / "active" / "sid-mgr.json").unlink()   # the manager is gone
    result = _run_monitor(config_dir, stdout=subprocess.PIPE)
    assert result.returncode == 2, (
        f"an orphaned lane exited {result.returncode} instead of 2; if this is "
        f"0 the loop keeps scanning forever, which is the defect this change "
        f"exists to retire")
    assert not (state_root / "lane-errors-unresolved-done.json").exists(), (
        "the orphan signal was counted as a retryable error")


def test_the_unresolved_counter_lives_outside_the_manager_namespace():
    """Names reach `_event_bucket` from `become_manager(name=…)` and
    `_resolve_named`, both of which accept arbitrary strings — so "funny-names
    are two dictionary words" describes the GENERATOR, not the only writers. A
    reserved bucket inside lane-health/ was therefore collidable; a sibling
    file outside that namespace is not."""
    from dockwright import names, paths

    unresolved = lane_io.error_counter_path(None, "done")
    assert unresolved.parent == paths.ROOT
    assert lane_io.UNRESOLVED_BUCKET is None, (
        "the sentinel is a NAME again, and any name is collidable")
    for hostile in ("_unresolved", "/_unresolved", "_unresolved/x", "None"):
        assert lane_io.error_counter_path(hostile, "done") != unresolved, (
            f"a manager named {hostile!r} shares the unresolved counter")
    # Derived from the generator's OWN vocabulary rather than a sample: a
    # sampled check would pass by luck on a name it never happened to roll.
    every_possible = {f"{adj}-{noun}"
                      for adj in names.ADJECTIVES
                      for noun in names.MANAGER_NOUNS + names.WORKER_NOUNS}
    assert every_possible, "the generator vocabulary is empty"
    assert lane_io.UNRESOLVED_BUCKET not in every_possible
    assert paths.UNSCOPED_BUCKET not in every_possible


@pytest.mark.parametrize("lane", sorted(lane_io.LANES))
def test_the_retry_allowance_is_a_time_window_not_a_scan_count(lane):
    """A flat count means a different tolerance on every lane.

    Five scans is 10s on the 2s lanes and 300s on the 60s stale lane — thirty
    times more for the lane that needs it least, and that lane is the one whose
    scan is NOT side-effect-free. Deriving the allowance from the interval
    gives every lane the same seconds of patience, and lands `stale` on exactly
    one attempt, which is what bounds how often its side effects can repeat.
    """
    retries = lane_io.max_consecutive_errors(lane)
    tolerated = retries * lane_io.LANES[lane]
    assert retries >= 1
    assert abs(tolerated - lane_io.MAX_SCAN_ERROR_WINDOW_SEC) < lane_io.LANES[lane], (
        f"{lane} tolerates {tolerated}s, not ~{lane_io.MAX_SCAN_ERROR_WINDOW_SEC}s")


def test_the_side_effecting_lane_gets_exactly_one_attempt():
    """Pinned separately from the formula, because THIS is the property that
    bounds the blast radius: a `stale` scan that already typed into a pane must
    not be retried."""
    assert lane_io.max_consecutive_errors("stale") == 1


def test_a_lane_slower_than_the_window_still_ends():
    """Floors at 1 rather than 0 — a zero allowance would never end."""
    assert lane_io.max_consecutive_errors("nonexistent-lane") == 1
