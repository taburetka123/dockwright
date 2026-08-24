"""Delivery discipline for monitor lanes.

A lane is one `dockwright monitor <lane>` scan wrapped in a shell loop. Each
stdout line becomes a chat notification to the owning manager, and each event
the scan surfaces is durably marked consumed in a cursor so the next scan skips
it.

The property this module installs, stated without reference to any one lane:

    No lane may commit consumption of an event it has not proven it delivered,
    and a lane that can no longer deliver must end loudly rather than run
    silently.

Both halves failed together on 2026-08-05/06. stdout is a pipe, so `print()` is
block-buffered and nothing reaches the fd; the scan committed the cursor; the
buffer flushed at interpreter exit; the reader was gone; Python swallowed the
BrokenPipeError as "Exception ignored" and exited 120; and
`while true; do …; done` discarded that exit code. Result: process alive,
`TaskOutput` reporting `running`, output frozen, and every later event silently
destroyed. Four such loops were found still scanning after 7 days 21 hours.

Two layers, deliberately asymmetric:

* `preflight()` is the cheap PRE-emptive check and FAILS OPEN. It costs one
  `poll()` and, when the reader is provably gone, stops the scan before it
  consumes anything at all. A probe quirk must never kill a working lane, so
  anything it cannot interpret reads as alive.
* `emit()` is the authoritative check and FAILS CLOSED. Flushing per line turns
  the deferred, swallowed exit-time failure into an immediate exception the
  caller can act on before committing anything.

Delivery is therefore AT-LEAST-ONCE. If the second of three lines fails, the
scan aborts before the cursor commit and the first line replays on the next
healthy scan. That is the intended trade and it matches the rest of the lane
code: a duplicate page beats a silenced lull.
"""
from __future__ import annotations

import os
import select
import sys

from . import paths, state

# Exit code meaning "this lane can no longer deliver; stop looping". The armed
# shell loop is `while dockwright monitor <lane> || exit $?; do sleep N; done`,
# so ANY non-zero exit ends the lane AND propagates out of the shell, which is
# what makes the Monitor task's exit surface to the manager as an anomaly
# rather than as a clean finish. This constant exists so the reason is
# greppable, not because the wrapper distinguishes it from exit 2 (identity
# unresolvable) — either code ends the lane.
EXIT_LANE_DEAD = 3

# poll() revents that mean the far end is GONE. POLLOUT's ABSENCE is NOT in
# this set on purpose: a live reader that stopped draining backpressures the
# socket and clears POLLOUT, and treating that as death would kill every
# healthy lane during exactly the burst it exists to report.
_READER_GONE = select.POLLERR | select.POLLHUP | select.POLLNVAL

# The canonical lane set: name -> poll interval the lane is armed with. This is
# the single source of truth. `monitor._MONITOR_SUBCOMMANDS` and the
# `dockwright lanes` report both derive from it, so a fifth lane is covered by
# construction rather than by someone remembering to update a second list.
LANES: dict[str, int] = {
    "questions": 2,
    "done": 2,
    "turn-ends": 5,
    "stale": 60,
}

# A lane is judged dead once its heartbeat is older than this many poll
# intervals. Three tolerates one slow scan plus scheduling jitter without
# waiting so long that a dead lane reads as healthy for a working session.
HEARTBEAT_STALE_INTERVALS = 3

# How long a lane tolerates UNEXPECTED scan errors before giving up, as a TIME
# window rather than a count of scans.
#
# Ending on the first exception would trade the old silent-death bug for a
# noisy one: a momentarily unreadable state dir or an EMFILE previously
# survived because the wrapper retried. Never ending restores the original
# defect in a new shape — a lane crash-looping while `TaskOutput` says
# `running`.
#
# A flat count of 5 was the first answer and it was the wrong axis: it means
# 10s of tolerance on the 2s lanes and 300s on the 60s stale lane, thirty
# times more for the lane that needs it least. A window falls out of the
# interval instead, and it lands the `stale` lane on exactly ONE attempt —
# which is what bounds the blast radius of the one scan that is not
# side-effect-free (it types into panes, unlinks records, closes windows).
MAX_SCAN_ERROR_WINDOW_SEC = 60


def max_consecutive_errors(lane: str) -> int:
    """Retries this lane tolerates: enough to cover MAX_SCAN_ERROR_WINDOW_SEC.

    Floors at 1, so a lane slower than the window ends on its first failure
    rather than never ending.
    """
    interval = LANES.get(lane, 0)
    if interval <= 0:
        return 1
    return max(1, -(-MAX_SCAN_ERROR_WINDOW_SEC // interval))

# Exit code for that case, kept distinct from EXIT_LANE_DEAD so the stderr line
# and the task exit say WHICH way the lane died.
EXIT_LANE_WEDGED = 4

# Lanes whose cursor semantics make an unconsumed event NORMAL, so a backlog
# count over them would cry wolf. `turn-ends` deliberately HOLDS events without
# marking them seen (delegation hold, turn-burst hold, and the FS ladder, whose
# rungs reach 4h), and `stale` keeps a threshold-crossing ladder rather than a
# per-event cursor. For these the report says `backlog=n/a` instead of quietly
# claiming a clean check it never performed.
BACKLOG_CHECKED_LANES = ("questions", "done")

# Where the consecutive-error counter goes when identity resolution ITSELF
# failed, so there is no manager name to charge it to.
#
# It is `None`, not a reserved string: `_event_bucket` maps "/" to "_", and
# `become_manager(name=…)` and `_resolve_named` both accept arbitrary strings,
# so "funny-names are two dictionary words" describes the GENERATOR and not
# the only writers. Any sentinel NAME is collidable; the absence of a name is
# not. Kept as a constant only so callers read clearly.
UNRESOLVED_BUCKET = None


class LaneDead(Exception):
    """The reader of this lane's stdout is gone."""


def reader_is_dead(fd: int = 1) -> bool:
    """True only when poll reports the far end HUNG UP.

    Fails OPEN — an unregisterable fd, a platform without poll, or any other
    surprise reads as alive. This check exists to save events, never to end
    lanes; `emit()` is what actually decides a lane is dead.
    """
    try:
        poller = select.poll()
        poller.register(fd, select.POLLOUT)
        for _fd, revents in poller.poll(0):
            return bool(revents & _READER_GONE)
        return False
    except Exception:
        return False


def preflight(fd: int = 1) -> None:
    """Raise LaneDead before the scan consumes anything, if the reader is gone."""
    if reader_is_dead(fd):
        raise LaneDead("stdout reader is gone (poll reports HUP/ERR/NVAL)")


def emit(line: str) -> None:
    """Write one event line and FLUSH it, so failure surfaces here.

    Without the flush the write sits in a block buffer until interpreter exit,
    where Python reports `BrokenPipeError` as "Exception ignored" and the
    caller has already committed the cursor.
    """
    try:
        sys.stdout.write(f"{line}\n")
        sys.stdout.flush()
    except (BrokenPipeError, OSError) as exc:
        raise LaneDead(f"stdout write failed: {exc}") from exc


def detach_stdout() -> None:
    """Point fd 1 at /dev/null so interpreter shutdown cannot re-raise.

    Load-bearing, not tidiness. After a LaneDead the buffer may still hold the
    line whose write failed, and CPython flushes stdout during shutdown: that
    flush raises BrokenPipeError, gets reported as "Exception ignored", and
    **overrides the process exit status with 120**. Which is precisely the
    signal-destroying behaviour this module exists to remove — the lane would
    exit 120 instead of EXIT_LANE_DEAD and the reason would be lost again.

    Re-pointing the fd makes the shutdown flush land in /dev/null and succeed,
    so the exit code we chose is the exit code the wrapper sees.
    """
    try:
        sys.stdout.flush()
    except Exception:
        pass
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, 1)
        finally:
            os.close(devnull)
    except OSError:
        pass


def error_counter_path(manager_name: str | None, lane: str):
    """Where record_scan_error keeps its run length.

    Normally the lane's own heartbeat. When identity failed there is no
    manager to charge it to, so it goes to a flat sibling file OUTSIDE the
    per-manager namespace.

    Dispatch is on `None`, NOT on a reserved name. A sentinel string is
    collidable by construction: `become_manager(name=…)` and `_resolve_named`
    both accept arbitrary strings, so a manager could be called whatever the
    sentinel is and share the counter. `None` is not a name.
    """
    if manager_name is None:
        return paths.ROOT / f"lane-errors-unresolved-{paths._event_bucket(lane)}.json"
    return heartbeat_path(manager_name, lane)


def heartbeat_path(manager_name: str, lane: str):
    """lane-health/<manager>/<lane>.json.

    Both components go through paths._event_bucket — the codebase's single
    answer to "this name must be exactly one path segment" — rather than a
    private copy that would drift from it.
    """
    return (paths.LANE_HEALTH / paths._event_bucket(manager_name)
            / f"{paths._event_bucket(lane)}.json")


def write_heartbeat(manager_name: str, lane: str, *, emitted: bool,
                    now: float | None = None) -> None:
    """Record that a scan completed with a reader that was still there.

    Call this LAST, after every emit has flushed. A scan that raises LaneDead
    never reaches it, which is the whole point: the heartbeat cannot tick while
    the lane is broken. `last_emit` carries forward across quiet scans — a lane
    with nothing to say has not stopped working.

    Best-effort by design: a heartbeat that cannot be written must not take
    down a lane that is otherwise delivering fine.
    """
    import time
    now = time.time() if now is None else now
    path = heartbeat_path(manager_name, lane)
    prior = state.read_json(path) or {}
    last_emit = now if emitted else prior.get("last_emit")
    try:
        state.write_json_atomic(path, {
            "lane": lane,
            "manager": manager_name,
            "pid": os.getpid(),
            "last_scan": now,
            "last_emit": last_emit if isinstance(last_emit, (int, float)) else None,
            "interval_hint": LANES.get(lane, 0),
            # A successful scan clears the error run. Only CONSECUTIVE failures
            # end a lane, so one bad scan between two good ones is forgotten.
            "consecutive_errors": 0,
        })
    except OSError as exc:
        print(f"monitor: heartbeat write failed for {lane} ({exc})",
              file=sys.stderr)


def record_scan_error(manager_name: str | None, lane: str) -> int:
    """Count one unexpected scan failure; return the consecutive run length.

    Deliberately does NOT touch `last_scan`: a failing scan has proved nothing
    about delivery, so the heartbeat must keep ageing and `dockwright lanes`
    must keep seeing it go stale. This counter is the second, faster signal —
    it ends the lane before the staleness window would, and says why.

    ⚠️ If the counter write ITSELF fails, the lane ends immediately rather than
    retrying. That looks inconsistent with the retry policy and is not: the
    counter lives under the same state root as the cursor, so an unwritable
    root means the lane cannot mark anything seen either — it fails every scan
    forever, and a count that can never advance would never reach the cap. The
    circular dependency is the whole problem: returning a low number here is
    what would restore the crash-loop-forever defect in a new shape. A
    transient version of this costs one re-arm, which is visible; the
    alternative is invisible.
    """
    path = error_counter_path(manager_name, lane)
    prior = state.read_json(path) or {}
    count = prior.get("consecutive_errors")
    count = (count if isinstance(count, int) and count > 0 else 0) + 1
    try:
        state.write_json_atomic(path, {**prior, "consecutive_errors": count})
    except OSError as exc:
        print(f"monitor: cannot record the error count for {lane} ({exc}); "
              f"the state root is unwritable, so this lane cannot function — "
              f"ending it rather than retrying blind.", file=sys.stderr)
        return max_consecutive_errors(lane)
    return count
