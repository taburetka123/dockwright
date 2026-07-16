"""Tests for deploy/scripts/runlock.sh — the shared analyst-run mutex.

Contract (arch-soundness review A5 / counterfactual F1): the old selffix
2h valve `rm -rf`'d LIVE holders' locks after a long wait; the evicted
holder's EXIT trap then deleted the thief's lock and mutual exclusion
collapsed under retro storms. The extracted lib must:

  - steal ONLY dead holders (pid gone) or over-aged ones (lock-dir mtime
    beyond RUNLOCK_MAX_HOLD_SEC — a live-but-wedged holder whose own
    watchdog failed);
  - bound the wait queue: a waiter that exhausts its budget gives up
    (exit 1) and never breaks a live, in-budget holder;
  - release only when <lock>/pid is still the releasing process's pid, so
    an evicted holder can never delete a successor's lock;
  - treat a lock dir with no readable pid as held (mid-acquisition).

Each test drives the sourced bash lib via a subprocess snippet.
"""
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNLOCK = REPO_ROOT / "deploy" / "scripts" / "runlock.sh"


def _bash(snippet: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'source "{RUNLOCK}"\n{snippet}'],
        capture_output=True, text=True, timeout=timeout,
    )


@pytest.fixture
def lock(tmp_path) -> Path:
    return tmp_path / "locks" / "analyst-run.lock"


def _hold(lock: Path, pid: int, age_sec: float = 0.0) -> None:
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(pid))
    if age_sec:
        stamp = time.time() - age_sec
        os.utime(lock, (stamp, stamp))


def test_acquire_free_lock(lock):
    r = _bash(f'runlock_acquire "{lock}" try && cat "{lock}/pid"')
    assert r.returncode == 0
    assert r.stdout.strip().isdigit()
    assert lock.is_dir()


def test_try_yields_to_live_holder(lock):
    _hold(lock, os.getpid())
    r = _bash(f'runlock_acquire "{lock}" try')
    assert r.returncode == 1
    assert (lock / "pid").read_text() == str(os.getpid()), "live holder's lock was broken"


def test_steals_dead_holder(lock):
    proc = subprocess.Popen(["sleep", "0.05"])
    proc.wait()
    _hold(lock, proc.pid)
    r = _bash(f'runlock_acquire "{lock}" try && cat "{lock}/pid"')
    assert r.returncode == 0
    assert r.stdout.strip() != str(proc.pid)


def test_wait_gives_up_without_breaking_live_holder(lock):
    """The bounded queue: budget exhaustion returns failure; the live,
    in-budget holder keeps its lock — the old valve broke it here."""
    _hold(lock, os.getpid())
    r = _bash(f'RUNLOCK_POLL_SEC=1 runlock_acquire "{lock}" wait 2', timeout=30)
    assert r.returncode == 1
    assert (lock / "pid").read_text() == str(os.getpid()), "live holder's lock was broken by a timed-out waiter"


def test_steals_overaged_live_holder(lock):
    """A live pid holding past RUNLOCK_MAX_HOLD_SEC is wedged (its own
    watchdog failed) — stealing it is the valve's legitimate residue."""
    _hold(lock, os.getpid(), age_sec=3 * 3600)
    r = _bash(f'RUNLOCK_MAX_HOLD_SEC=7200 runlock_acquire "{lock}" try && cat "{lock}/pid"')
    assert r.returncode == 0
    assert r.stdout.strip() != str(os.getpid())


def test_young_live_holder_not_overaged(lock):
    _hold(lock, os.getpid(), age_sec=60)
    r = _bash(f'RUNLOCK_MAX_HOLD_SEC=7200 runlock_acquire "{lock}" try')
    assert r.returncode == 1


def test_midacquisition_lock_treated_as_held(lock):
    """Lock dir with no pid file = a holder between mkdir and pid-write.
    Never stolen while fresh."""
    lock.mkdir(parents=True)
    r = _bash(f'runlock_acquire "{lock}" try')
    assert r.returncode == 1
    assert lock.is_dir()


def test_release_only_by_owner(lock):
    """An evicted holder's release must not delete the current owner's lock
    — this is the cascade half of the old valve bug."""
    _hold(lock, os.getpid())  # current owner: this test process
    r = _bash(
        # Simulate the evicted holder: it believes it held the lock
        # (RUNLOCK_HELD=1, RUNLOCK_DIR set) but the pid file is no longer its own.
        f'RUNLOCK_DIR="{lock}"; RUNLOCK_HELD=1; runlock_release'
    )
    assert r.returncode == 0
    assert lock.is_dir(), "release by a non-owner deleted the lock"
    assert (lock / "pid").read_text() == str(os.getpid())


def test_release_by_owner_frees_lock(lock):
    r = _bash(f'runlock_acquire "{lock}" try && runlock_release')
    assert r.returncode == 0
    assert not lock.exists()


def test_release_without_hold_is_noop(lock):
    r = _bash('runlock_release')
    assert r.returncode == 0


def test_wait_acquires_after_holder_releases(lock):
    """End-to-end: a waiter polls while a real holder finishes, then wins."""
    holder = subprocess.Popen(
        ["bash", "-c",
         f'source "{RUNLOCK}"; runlock_acquire "{lock}" try; sleep 2; runlock_release'],
    )
    time.sleep(0.5)  # let the holder win first
    r = _bash(f'RUNLOCK_POLL_SEC=1 runlock_acquire "{lock}" wait 30 && cat "{lock}/pid"', timeout=60)
    holder.wait()
    assert r.returncode == 0
    assert r.stdout.strip().isdigit()


def test_exit_trap_pattern_releases(lock):
    """The consumers install `trap runlock_release EXIT` — verify the lock
    frees on normal exit through the trap."""
    r = _bash(f'trap runlock_release EXIT; runlock_acquire "{lock}" try')
    assert r.returncode == 0
    assert not lock.exists()


def test_dir_age_correct_under_gnu_stat_personality(lock, tmp_path):
    # N-1 sibling: on GNU coreutils the old BSD-first probe poisoned $mtime,
    # _runlock_dir_age printed nothing, and the over-age steal branch became
    # unreachable — a wedged holder then blocked selffix/gardener forever.
    from tests.stat_shims import write_gnu_stat_shim
    _hold(lock, os.getpid(), age_sec=7300)
    env = dict(os.environ)
    env["PATH"] = f"{write_gnu_stat_shim(tmp_path / 'shims')}:{env['PATH']}"
    r = subprocess.run(
        ["bash", "-c", f'source "{RUNLOCK}"\nRUNLOCK_DIR="{lock}"\n_runlock_dir_age'],
        capture_output=True, text=True, env=env, timeout=30,
    )
    age = r.stdout.strip()
    assert age.isdigit(), f"age not a number: stdout={r.stdout!r} stderr={r.stderr!r}"
    assert 7200 <= int(age) <= 7400
