import importlib.util
import json
import os
import subprocess
import time
import types
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
STALE_MONITOR_PATH = REPO_ROOT / "src" / "dockwright" / "stale_monitor.py"


def _load_stale_monitor():
    spec = importlib.util.spec_from_file_location("stale_monitor_under_test", STALE_MONITOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def stale(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_STALE_PROCESSING_MIN", raising=False)
    monkeypatch.delenv("CLAUDE_ORCH_AUTONUDGE", raising=False)
    mod = _load_stale_monitor()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(mod, "QUESTIONS", tmp_path / "questions")
    monkeypatch.setattr(mod, "CLOSED", tmp_path / "closed")
    monkeypatch.setattr(mod, "CLAUDE_PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(mod, "CODEX_SESSIONS", tmp_path / "codex-sessions")
    # stale_monitor no longer has a module-level EMITTED_STATE — main() resolves
    # the per-manager dedup file via _emitted_state_path(manager_name). Expose the
    # resolved GLOBAL (no --manager) path under the name the existing tests use,
    # tied to the real resolver so a naming change can't silently desync the tests.
    monkeypatch.setattr(mod, "EMITTED_STATE", mod._emitted_state_path(None), raising=False)
    monkeypatch.setattr(mod, "ASSIGNMENTS_PENDING",
                        tmp_path / "assignments" / ".pending", raising=False)
    # Squash IDLE threshold so tests can use small elapsed values.
    monkeypatch.setattr(mod, "IDLE_THRESHOLD_SEC", 100)
    for d in ("active", "questions", "closed"):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(mod, "ACCOUNT_ACTIVE", tmp_path / "account-active")
    monkeypatch.setattr(mod, "ACCOUNT_LEDGER", tmp_path / "account-flips.jsonl")
    monkeypatch.setattr(mod, "ACCOUNT_STATE", tmp_path / "account-state.json")
    monkeypatch.setattr(mod, "ACCOUNT_LOCK", tmp_path / ".account-flip.lock")
    # Default-deny keychain probe: no test ever reaches the real `security`
    # binary; tests exercising flips override with lambda: True.
    monkeypatch.setattr(mod, "_keychain_unlocked", lambda: False)
    # tmux is the sole backend now: pin it so autoclose/autonudge/recovery
    # route through TmuxDriver (close -> kill-pane; send_text -> load/paste/
    # send-keys; spawn -> the mgr session), absorbed by the no_live_tmux fixture.
    from dockwright import terminal
    monkeypatch.setenv("CLAUDE_ORCH_TERMINAL", "tmux")
    terminal._DRIVER = None
    return mod


def _write_record(stale, sid, **overrides):
    record = {
        "claude_sid": sid,
        "agent": "worker",
        "name": f"worker-{sid}",
        "cwd": "/x",
        "iterm_sid": "",
        "pid": 0,
        "started_at": time.time(),
        "state": "idle",
        "last_summary": None,
        "last_turn_at": None,
    }
    record.update(overrides)
    path = stale.ACTIVE / f"{sid}.json"
    path.write_text(json.dumps(record))
    return path


def _write_question(stale, qid, worker_sid, **overrides):
    record = {
        "question_id": qid,
        "worker_sid": worker_sid,
        "worker_name": f"worker-{worker_sid}",
        "parent_manager_name": None,
        "question": "blocked?",
        "asked_at": time.time(),
    }
    record.update(overrides)
    parent = record.get("parent_manager_name")
    path = stale.QUESTIONS / parent / f"{qid}.json" if parent else stale.QUESTIONS / f"{qid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))
    return path


def test_autoclose_branch_gated_by_hourly_cadence(stale, monkeypatch):
    """Within 1h of the last branch run, idle workers are NOT reaped even if elapsed > threshold."""
    now = int(time.time())
    # Record that branch ran 10 min ago.
    stale.EMITTED_STATE.write_text(json.dumps({"last_autoclose_run": now - 600}))
    # Worker is well over the idle threshold by wall-clock.
    record_path = _write_record(
        stale, "s1",
        last_turn_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10_000)),
    )
    rc = stale.main()
    assert rc == 0
    # Worker NOT reaped — branch was gated.
    assert record_path.exists()
    assert not (stale.CLOSED / "s1.json").exists()
    # last_autoclose_run preserved (not bumped).
    emitted = json.loads(stale.EMITTED_STATE.read_text())
    assert emitted["last_autoclose_run"] == now - 600

    # Advance past 1h by rewriting the timestamp.
    stale.EMITTED_STATE.write_text(json.dumps({"last_autoclose_run": now - 3700}))
    rc = stale.main()
    assert rc == 0
    # Worker reaped on this scan.
    assert not record_path.exists()
    assert (stale.CLOSED / "s1.json").exists()
    # last_autoclose_run bumped to ~now.
    emitted = json.loads(stale.EMITTED_STATE.read_text())
    assert emitted["last_autoclose_run"] >= now - 1


def test_autoclose_branch_runs_on_first_scan_when_key_absent(stale):
    """First run after script start (no emitted state) must run the branch immediately
    and persist last_autoclose_run, otherwise a freshly-spawned monitor wouldn't auto-close
    anything for the first hour of its life."""
    now = int(time.time())
    record_path = _write_record(
        stale, "s1",
        last_turn_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10_000)),
    )
    assert not stale.EMITTED_STATE.exists()
    rc = stale.main()
    assert rc == 0
    assert not record_path.exists()
    assert (stale.CLOSED / "s1.json").exists()
    emitted = json.loads(stale.EMITTED_STATE.read_text())
    assert "last_autoclose_run" in emitted


def test_processing_manager_record_does_not_emit_stale(stale, capsys):
    """Manager turns stay state='processing' while AskUserQuestion holds the turn
    open — that's normal, not stale. Only worker processing should page."""
    now = int(time.time())
    # Manager record with stale mtime (well past the 30min threshold).
    path = _write_record(
        stale, "mgr1",
        agent="manager",
        state="processing",
        name="manager-tab",
    )
    old_mtime = now - 2700  # 45 min ago — well past the 30min threshold
    os.utime(path, (old_mtime, old_mtime))

    rc = stale.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "STALE_PROCESSING" not in out

    # And a worker record with the same stale mtime DOES emit, proving the guard
    # isn't a no-op blanket-skip.
    path_w = _write_record(
        stale, "w1",
        agent="worker",
        state="processing",
        name="worker-tab",
    )
    os.utime(path_w, (old_mtime, old_mtime))
    # Reset emitted state so the doubling-threshold debounce doesn't suppress.
    stale.EMITTED_STATE.unlink(missing_ok=True)

    rc = stale.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "STALE_PROCESSING worker-tab" in out
    assert "STALE_PROCESSING manager-tab" not in out


def test_processing_emits_once_per_threshold_crossing(stale, capsys, monkeypatch):
    """STALE_PROCESSING fires on each doubling crossing (30,60,120min) exactly once;
    intermediate scans at the same threshold are suppressed via .stale-emitted.json.
    This is the notification-flood guard: a wedged worker pages at 30min then backs
    off to 60/120, not on every 60s scan loop.

    The active-record mtime (= processing-stretch start) stays FIXED through a stretch
    — only wall-clock advances — so the test pins mtime once and drives a fake clock,
    mirroring how the real monitor sees a single long turn."""
    path = _write_record(stale, "w1", agent="worker", state="processing", name="worker-tab")
    stretch_start = 1_000_000
    os.utime(path, (stretch_start, stretch_start))
    clock = {"now": stretch_start}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])

    def run_at(elapsed_min):
        clock["now"] = stretch_start + elapsed_min * 60
        stale.main()
        return capsys.readouterr().out

    assert "STALE_PROCESSING" not in run_at(29)           # below 30min → silent
    assert "STALE_PROCESSING worker-tab" in run_at(30)    # first fire at 30min → emit
    assert "STALE_PROCESSING" not in run_at(31)           # still [30,60) → suppressed
    assert "STALE_PROCESSING worker-tab" in run_at(60)    # crosses 60min → emit
    assert "STALE_PROCESSING worker-tab" in run_at(121)   # crosses 120min → emit


def test_processing_realarms_on_new_stretch_without_observed_idle(stale, capsys, monkeypatch):
    """A new processing stretch (fresh active-record mtime) re-arms the 30min clock
    even when the monitor never observed the intervening idle state — because the dedup
    key embeds the processing-stretch start (mtime), not just the sid. Without this,
    a stale `threshold=120` from the prior stretch would suppress the new stretch's
    30min alert until it too exceeded 120min."""
    path = _write_record(stale, "w1", agent="worker", state="processing", name="worker-tab")
    clock = {"now": 1_000_000}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])

    # First stretch runs 121min → emits through the 30/60/120 buckets, stores 120.
    t0 = 1_000_000
    os.utime(path, (t0, t0))
    clock["now"] = t0 + 121 * 60
    stale.main()
    assert "STALE_PROCESSING worker-tab" in capsys.readouterr().out
    emitted = json.loads(stale.EMITTED_STATE.read_text())
    assert emitted.get(f"processing:w1:{t0}") == 120

    # New stretch begins: active record rewritten with a fresh mtime; the monitor
    # never saw the idle gap. 30min into the new stretch it must fire again.
    t1 = t0 + 121 * 60 + 5
    os.utime(path, (t1, t1))
    clock["now"] = t1 + 30 * 60
    stale.main()
    assert "STALE_PROCESSING worker-tab" in capsys.readouterr().out, (
        "a fresh processing stretch must re-arm and fire at 30min"
    )
    emitted = json.loads(stale.EMITTED_STATE.read_text())
    assert f"processing:w1:{t0}" not in emitted, "prior stretch key must be pruned"
    assert emitted.get(f"processing:w1:{t1}") == 30


def test_processing_key_pruned_when_worker_goes_idle(stale, capsys, monkeypatch):
    """When a worker transitions out of processing (Stop hook → state=idle), its
    processing alert-state key is dropped, so a later long turn starts the 30min
    clock over."""
    path = _write_record(stale, "w1", agent="worker", state="processing", name="worker-tab")
    clock = {"now": 1_000_000}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])

    t0 = 1_000_000
    os.utime(path, (t0, t0))
    clock["now"] = t0 + 35 * 60          # 35min in → fires the 30 bucket
    stale.main()
    capsys.readouterr()
    assert json.loads(stale.EMITTED_STATE.read_text()).get(f"processing:w1:{t0}") == 30

    # Worker goes idle. last_autoclose_run is recent (set by the prev run) so the
    # hourly-gated idle branch is skipped — the worker is not reaped, we only assert
    # the processing key is pruned.
    _write_record(stale, "w1", agent="worker", state="idle", name="worker-tab")
    clock["now"] = t0 + 36 * 60
    stale.main()
    capsys.readouterr()
    emitted = json.loads(stale.EMITTED_STATE.read_text())
    assert f"processing:w1:{t0}" not in emitted, (
        "processing key must be pruned once the worker goes idle"
    )

    # A fresh processing stretch later re-arms and fires at 30min.
    t1 = t0 + 60 * 60
    _write_record(stale, "w1", agent="worker", state="processing", name="worker-tab")
    os.utime(stale.ACTIVE / "w1.json", (t1, t1))
    clock["now"] = t1 + 30 * 60
    stale.main()
    assert "STALE_PROCESSING worker-tab" in capsys.readouterr().out


def test_elapsed_uses_uptime_when_present(stale, monkeypatch):
    """Uptime delta wins over wall-clock when last_turn_at_uptime is set on the record."""
    fake_current_uptime = 100_000.0
    record = {
        "last_turn_at_uptime": fake_current_uptime - 1800,  # 30min of uptime ago
        "last_turn_at": "2026-05-19T00:00:00Z",  # wall-clock would say many years
        "started_at": time.time() - 86400,
    }
    # The current real now is irrelevant since uptime path wins.
    elapsed = stale._compute_idle_elapsed_sec(record, fake_current_uptime, int(time.time()))
    assert elapsed == 1800


def test_elapsed_falls_back_to_wall_on_reboot(stale):
    """Reboot resets CLOCK_UPTIME_RAW to near-zero. Persisted uptime is larger →
    naive delta is negative → fall back to wall-clock so auto-close still fires."""
    now = int(time.time())
    record = {
        # Persisted uptime from before reboot
        "last_turn_at_uptime": 50_000.0,
        # Wall-clock says 7200s ago
        "last_turn_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 7200)),
        "started_at": now - 7200,
    }
    # current_uptime small (just rebooted)
    elapsed = stale._compute_idle_elapsed_sec(record, current_uptime=120.0, now=now)
    # Falls back to wall: now - last_turn_at ≈ 7200
    assert elapsed is not None
    assert 7195 <= elapsed <= 7205


def test_elapsed_falls_back_to_wall_when_field_absent(stale):
    """Old records (predate the fix) have no last_turn_at_uptime — use wall-clock so
    the deploy gap doesn't strand them in 'no elapsed computable' limbo."""
    now = int(time.time())
    record = {
        "last_turn_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600)),
        "started_at": now - 3600,
    }
    elapsed = stale._compute_idle_elapsed_sec(record, current_uptime=100_000.0, now=now)
    assert elapsed is not None
    assert 3595 <= elapsed <= 3605


def test_elapsed_uses_started_at_when_last_turn_missing(stale):
    """Worker that's never finished a turn falls back to started_at via wall-clock."""
    now = int(time.time())
    record = {"started_at": now - 500}
    elapsed = stale._compute_idle_elapsed_sec(record, current_uptime=100.0, now=now)
    assert elapsed is not None
    assert 495 <= elapsed <= 505


def test_elapsed_returns_none_when_no_anchor(stale):
    """No uptime field, no last_turn_at, no started_at → can't compute elapsed."""
    record = {"started_at": None}
    elapsed = stale._compute_idle_elapsed_sec(record, current_uptime=100.0, now=int(time.time()))
    assert elapsed is None


# --- autoclose graceful-close path ----------------------------------------
# Autoclose closes the worker's tmux window via the terminal driver — Claude
# Code's SessionEnd hook then fires selffix-trigger.sh + orchestrator
# session-end natively. No SIGTERM, no manual selffix trigger. The
# closed/<sid>.json record is written BEFORE active/ is unlinked so the
# orchestrator session-end hook (running inside the closing window) sees no
# active record and skips its own closed/ write — preserving our
# `closed_reason: "idle>...s"` annotation.

def test_autoclose_closes_window_and_skips_sigterm(no_live_tmux, stale, monkeypatch):
    """_autoclose_idle_worker must close the worker's tmux pane via the driver
    (`tmux ... kill-pane -t <pane>`) — not SIGTERM. SessionEnd fires inside the
    closing window.
    """
    now = int(time.time())
    record_path = _write_record(
        stale, "s1",
        pid=12345,
        iterm_sid="42",
        last_turn_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10_000)),
    )

    selffix_calls = []
    _REAL_SP_RUN = stale.subprocess.run
    def watch_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args")
        if (isinstance(cmd, list) and len(cmd) >= 2
                and cmd[0] == "bash" and "selffix-trigger.sh" in str(cmd[1])):
            selffix_calls.append(cmd)
            class R:
                returncode = 0
            return R()
        return _REAL_SP_RUN(*args, **kwargs)
    monkeypatch.setattr(stale.subprocess, "run", watch_run)

    killed = []
    monkeypatch.setattr(stale.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    rc = stale.main()
    assert rc == 0
    # Worker reaped — closed record written, active gone.
    assert not record_path.exists()
    assert (stale.CLOSED / "s1.json").exists()
    # No SIGTERM.
    assert killed == []
    # Exactly one tmux kill-pane call against the worker's window id.
    kill_calls = [
        c for c in no_live_tmux.run
        if c[0] == "tmux" and "kill-pane" in c and "42" in c
    ]
    assert len(kill_calls) == 1, f"expected 1 tmux kill-pane call, got {no_live_tmux.run!r}"
    # No raw kitty close.
    assert not any(c[0] == "kitty" for c in no_live_tmux.run)
    # And no selffix-trigger.sh invocation (SessionEnd will fire it natively).
    assert selffix_calls == []


def test_autoclose_preserves_idle_closed_reason(stale, monkeypatch):
    """The closed/<sid>.json record must carry `closed_reason: "idle>...s"`
    — the orchestrator session-end hook would otherwise overwrite it with
    "session_end". Implementation must order the writes so the idle reason
    wins: write closed first, unlink active before the window close so SessionEnd
    (firing inside the closing window) sees no active record and skips its write.
    """
    now = int(time.time())
    record_path = _write_record(
        stale, "s1",
        pid=12345,
        iterm_sid="7",
        last_turn_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10_000)),
    )

    # The order matters: active/ must be unlinked BEFORE the window close so the
    # in-window SessionEnd hook (which runs as a side effect of the close) finds
    # no record to overwrite from.
    order: list = []
    real_unlink = record_path.__class__.unlink

    def tracking_unlink(self, missing_ok=False):
        if self == record_path:
            order.append("unlink-active")
        return real_unlink(self, missing_ok=missing_ok)
    monkeypatch.setattr(record_path.__class__, "unlink", tracking_unlink)

    real_close = stale._close_window
    def tracking_close(window_id):
        order.append("close-window")
        return real_close(window_id)
    monkeypatch.setattr(stale, "_close_window", tracking_close)
    monkeypatch.setattr(stale.os, "kill", lambda *a, **k: None)

    rc = stale.main()
    assert rc == 0
    closed_data = json.loads((stale.CLOSED / "s1.json").read_text())
    assert closed_data["closed_reason"].startswith("idle>"), (
        f"expected closed_reason to start with 'idle>', got {closed_data['closed_reason']!r}"
    )
    assert order.index("unlink-active") < order.index("close-window"), (
        f"active record must be unlinked before the window close so SessionEnd "
        f"doesn't overwrite the idle-reason closed record; order={order!r}"
    )


def test_autoclose_preserves_runtime_and_parent_manager(stale, monkeypatch):
    now = int(time.time())
    _write_record(
        stale, "s1",
        pid=12345,
        iterm_sid="7",
        runtime="codex",
        parent_manager_name="manager-a",
        last_turn_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10_000)),
    )
    monkeypatch.setattr(stale.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(stale.os, "kill", lambda *a, **k: None)

    rc = stale.main()
    assert rc == 0
    closed_data = json.loads((stale.CLOSED / "s1.json").read_text())
    assert closed_data["runtime"] == "codex"
    assert closed_data["parent_manager_name"] == "manager-a"


def test_autoclose_preserves_account(stale, monkeypatch):
    # D8: the autoclose lane unlinks active/ BEFORE the window close, so
    # session_end never writes for autoclosed workers — this writer must stamp
    # `account` itself or idle-autoclosed spend stays unattributable.
    now = int(time.time())
    _write_record(
        stale, "s1",
        pid=12345,
        iterm_sid="7",
        account="b",
        last_turn_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10_000)),
    )
    monkeypatch.setattr(stale.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(stale.os, "kill", lambda *a, **k: None)
    rc = stale.main()
    assert rc == 0
    closed_data = json.loads((stale.CLOSED / "s1.json").read_text())
    assert closed_data["account"] == "b"


def test_autoclose_swallows_window_close_failure(stale, monkeypatch):
    """A window-close failure must NOT abort the autoclose cleanup —
    the closed record and active unlink already happened.
    """
    now = int(time.time())
    record_path = _write_record(
        stale, "s1",
        pid=12345,
        iterm_sid="9",
        last_turn_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10_000)),
    )

    class BoomDrv:
        def close(self, window_id):
            raise OSError("terminal gone")
    monkeypatch.setattr(stale, "_get_driver", lambda: BoomDrv())
    monkeypatch.setattr(stale.os, "kill", lambda *a, **k: None)

    rc = stale.main()
    assert rc == 0
    assert not record_path.exists()
    assert (stale.CLOSED / "s1.json").exists()


def test_idle_threshold_default_is_2_hours(monkeypatch):
    """The default autoclose threshold MUST be 2h. Working-hours sessions that
    go idle for >2h are almost certainly done; `resume_worker` exists for the
    rare false positive.
    """
    monkeypatch.delenv("CLAUDE_ORCH_IDLE_TTL_HOURS", raising=False)
    mod = _load_stale_monitor()
    assert mod._IDLE_HOURS == 2.0, f"default IDLE_HOURS expected 2.0, got {mod._IDLE_HOURS!r}"
    assert mod.IDLE_THRESHOLD_SEC == 7200, f"expected 7200s (2h), got {mod.IDLE_THRESHOLD_SEC}"


# --- --manager scoping ------------------------------------------------------
# stale_monitor.py --manager NAME scopes all three scan branches strictly to
# records whose parent_manager_name == NAME. Null-parent (legacy) records are
# INVISIBLE to scoped runs — recovery via _backfill_legacy_workers on single-
# manager become_manager boot, or via no --manager (wildcard back-compat). The
# filter mirrors mcp_server._matches_manager. These tests prove each branch
# honors the scope.

def test_matches_manager_filter_semantics(stale):
    """_matches_manager: None → keep all; else keep ONLY ==name (strict)."""
    own = {"parent_manager_name": "mgr-A"}
    peer = {"parent_manager_name": "mgr-B"}
    legacy = {"parent_manager_name": None}
    missing = {}  # field absent entirely == null-parent
    # No scope → everything matches (wildcard back-compat).
    assert all(stale._matches_manager(r, None) for r in (own, peer, legacy, missing))
    # Scoped to mgr-A → ONLY own kept; peer and null-parent both dropped.
    assert stale._matches_manager(own, "mgr-A") is True
    assert stale._matches_manager(legacy, "mgr-A") is False
    assert stale._matches_manager(missing, "mgr-A") is False
    assert stale._matches_manager(peer, "mgr-A") is False


def _stale_processing_worker(stale, sid, name, parent, now):
    path = _write_record(
        stale, sid, agent="worker", state="processing", name=name,
        parent_manager_name=parent,
    )
    old = now - 2700  # 45 min ago — well past the 30min processing threshold
    os.utime(path, (old, old))
    return path


def test_processing_scan_scoped_skips_peer_and_legacy(stale, capsys):
    """Strict routing: scoped scan surfaces ONLY own; peer + null-parent dropped."""
    now = int(time.time())
    _stale_processing_worker(stale, "own", "own-tab", "mgr-A", now)
    _stale_processing_worker(stale, "peer", "peer-tab", "mgr-B", now)
    _stale_processing_worker(stale, "legacy", "legacy-tab", None, now)

    rc = stale.main(manager_name="mgr-A")
    assert rc == 0
    out = capsys.readouterr().out
    assert "STALE_PROCESSING own-tab" in out
    assert "STALE_PROCESSING peer-tab" not in out, "peer manager's worker must be skipped"
    assert "STALE_PROCESSING legacy-tab" not in out, "null-parent worker invisible under strict routing"


def test_processing_scan_global_surfaces_peer(stale, capsys):
    """No --manager → peer worker is surfaced (exact global back-compat)."""
    now = int(time.time())
    _stale_processing_worker(stale, "peer", "peer-tab", "mgr-B", now)
    _stale_processing_worker(stale, "own", "own-tab", "mgr-A", now)

    rc = stale.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "STALE_PROCESSING peer-tab" in out
    assert "STALE_PROCESSING own-tab" in out


def test_idle_autoclose_scoped_skips_peer_and_legacy(stale, monkeypatch):
    """Strict routing: scoped autoclose reaps ONLY own; peer + null-parent untouched."""
    now = int(time.time())
    old_turn = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10_000))
    own = _write_record(stale, "own", parent_manager_name="mgr-A", last_turn_at=old_turn)
    peer = _write_record(stale, "peer", parent_manager_name="mgr-B", last_turn_at=old_turn)
    legacy = _write_record(stale, "legacy", parent_manager_name=None, last_turn_at=old_turn)

    # Stub the driver close so no real window is touched.
    monkeypatch.setattr(stale.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(stale.os, "kill", lambda *a, **k: None)

    rc = stale.main(manager_name="mgr-A")
    assert rc == 0
    # Only own reaped; peer + null-parent untouched.
    assert not own.exists() and (stale.CLOSED / "own.json").exists()
    assert legacy.exists(), "null-parent idle worker must NOT be auto-closed under strict routing"
    assert not (stale.CLOSED / "legacy.json").exists()
    assert peer.exists(), "peer manager's idle worker must NOT be auto-closed"
    assert not (stale.CLOSED / "peer.json").exists()


def test_idle_autoclose_global_reaps_peer(stale, monkeypatch):
    """No --manager → peer idle worker is auto-closed (exact global back-compat)."""
    now = int(time.time())
    old_turn = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10_000))
    peer = _write_record(stale, "peer", parent_manager_name="mgr-B", last_turn_at=old_turn)
    monkeypatch.setattr(stale.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(stale.os, "kill", lambda *a, **k: None)

    rc = stale.main()
    assert rc == 0
    assert not peer.exists() and (stale.CLOSED / "peer.json").exists()


def test_question_scan_scoped_skips_peer_and_legacy(stale, capsys):
    """Strict routing: scoped scan surfaces ONLY own question; peer + null-parent dropped."""
    now = int(time.time())
    asked = now - 600  # 10 min ago — past the 2min question threshold
    # Each question's worker must be present in active/ (the scan ignores
    # questions whose worker is gone).
    _write_record(stale, "own", parent_manager_name="mgr-A")
    _write_record(stale, "peer", parent_manager_name="mgr-B")
    _write_record(stale, "legacy", parent_manager_name=None)
    _write_question(stale, "q-own", "own", worker_name="own-w", parent_manager_name="mgr-A", asked_at=asked)
    _write_question(stale, "q-peer", "peer", worker_name="peer-w", parent_manager_name="mgr-B", asked_at=asked)
    _write_question(stale, "q-legacy", "legacy", worker_name="legacy-w", parent_manager_name=None, asked_at=asked)

    rc = stale.main(manager_name="mgr-A")
    assert rc == 0
    out = capsys.readouterr().out
    assert "STALE_QUESTION q-own" in out
    assert "STALE_QUESTION q-peer" not in out, "peer manager's question must be skipped"
    assert "STALE_QUESTION q-legacy" not in out, "null-parent question invisible under strict routing"


def test_question_scan_global_surfaces_peer(stale, capsys):
    """No --manager → peer question is surfaced (exact global back-compat)."""
    now = int(time.time())
    asked = now - 600
    _write_record(stale, "peer", parent_manager_name="mgr-B")
    _write_question(stale, "q-peer", "peer", worker_name="peer-w", parent_manager_name="mgr-B", asked_at=asked)

    rc = stale.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "STALE_QUESTION q-peer" in out


def test_scoped_and_global_use_separate_dedup_files(stale, monkeypatch):
    """A scoped run writes .stale-emitted-<name>.json; the global run writes
    .stale-emitted.json — so concurrent scoped scans don't clobber each other's
    edge-trigger thresholds or share the autoclose gate."""
    assert stale._emitted_state_path(None) == stale.ROOT / ".stale-emitted.json"
    assert stale._emitted_state_path("mgr-A") == stale.ROOT / ".stale-emitted-mgr-A.json"
    # A slash in a manager name is sanitized so it can't escape ROOT.
    assert stale._emitted_state_path("a/b") == stale.ROOT / ".stale-emitted-a_b.json"

    now = int(time.time())
    _stale_processing_worker(stale, "own", "own-tab", "mgr-A", now)
    stale.main(manager_name="mgr-A")
    assert (stale.ROOT / ".stale-emitted-mgr-A.json").exists()
    assert not (stale.ROOT / ".stale-emitted.json").exists(), (
        "scoped run must not write the global dedup file"
    )


# --- processing-threshold default + env override -----------------------------

def test_processing_threshold_default_is_30_min(monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_STALE_PROCESSING_MIN", raising=False)
    mod = _load_stale_monitor()
    assert mod.PROCESSING_THRESHOLD_MIN == 30
    assert mod.PROCESSING_THRESHOLD_SEC == 1800


def test_processing_threshold_env_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_STALE_PROCESSING_MIN", "45")
    mod = _load_stale_monitor()
    assert mod.PROCESSING_THRESHOLD_MIN == 45
    assert mod.PROCESSING_THRESHOLD_SEC == 2700


def test_processing_threshold_bad_env_falls_back_to_default(monkeypatch):
    for bad in ("abc", "0", "-5", ""):
        monkeypatch.setenv("CLAUDE_ORCH_STALE_PROCESSING_MIN", bad)
        mod = _load_stale_monitor()
        assert mod.PROCESSING_THRESHOLD_MIN == 30, f"env={bad!r} must fall back to 30"


# --- transcript tail-read + 429 signature ------------------------------------

def _assistant_line(text):
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    )


def _write_transcript(stale, sid, text):
    project_dir = stale.CLAUDE_PROJECTS / "proj"
    project_dir.mkdir(parents=True, exist_ok=True)
    log = project_dir / f"{sid}.jsonl"
    log.write_text(_assistant_line(text) + "\n")
    return log


def _append_transcript(stale, sid, text, mtime):
    log = stale.CLAUDE_PROJECTS / "proj" / f"{sid}.jsonl"
    with open(log, "a") as f:
        f.write(_assistant_line(text) + "\n")
    os.utime(log, (mtime, mtime))
    return log


THROTTLE_TEXT = "Server is temporarily limiting requests (not your usage limit) · Rate limited"


def test_last_assistant_text_last_wins_and_skips_garbage(stale, tmp_path):
    log = tmp_path / "t.jsonl"
    lines = [
        _assistant_line("first"),
        "not json",
        json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "user text"}]}}),
        _assistant_line("second"),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}),
    ]
    log.write_text("\n".join(lines) + "\n")
    assert stale._last_assistant_text(log) == "second"


def test_last_assistant_text_reads_only_the_tail(stale, tmp_path):
    """A multi-MB transcript must not be read whole: only the tail window is parsed,
    and the first (possibly partial) line of the window is dropped."""
    log = tmp_path / "big.jsonl"
    filler = _assistant_line("filler " + "x" * 100)
    log.write_text("\n".join([filler] * 200 + [_assistant_line("THE END")]) + "\n")
    assert stale._last_assistant_text(log, max_bytes=4096) == "THE END"


def test_last_assistant_text_missing_or_empty(stale, tmp_path):
    assert stale._last_assistant_text(tmp_path / "absent.jsonl") is None
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert stale._last_assistant_text(empty) is None


SESSION_LIMIT_TEXT = "You’ve hit your session limit · resets 2:20am (Asia/Novosibirsk)"


# Genuine session-limit banner whose reset clause does not parse (drifted/absent
# wording) — still a brick, but exercises the unparsed-banner ledger path.
SESSION_LIMIT_NO_RESET = "You’ve hit your session limit · resets soon"


# The exact CC CLI wording for a transient HTTP 529 server-overload error.
API_529_TEXT = ("API Error: 529 Overloaded. This is a server-side issue, usually "
                "temporary — try again in a moment. If it persists, check "
                "https://status.claude.com.")


def test_is_rate_limited_matches_throttle_transcript(stale):
    _write_transcript(stale, "w1", THROTTLE_TEXT)
    record = {"claude_sid": "w1", "runtime": "claude"}
    assert stale._is_rate_limited(record) is True
    # runtime defaults to claude when absent
    assert stale._is_rate_limited({"claude_sid": "w1"}) is True


def test_is_rate_limited_matches_session_limit_banner(stale):
    """The session-limit banner ("You've hit your session limit · resets …")
    bricks a worker exactly like a 429 but carries no "limiting requests" text —
    it must hit the rate-limit fast-path too. The banner uses a typographic
    apostrophe; the signature must not depend on the apostrophe variant."""
    _write_transcript(stale, "w1", SESSION_LIMIT_TEXT)
    assert stale._is_rate_limited({"claude_sid": "w1"}) is True
    _write_transcript(stale, "w2", "You've hit your session limit · resets 6pm")
    assert stale._is_rate_limited({"claude_sid": "w2"}) is True


def test_is_rate_limited_negative_cases(stale):
    _write_transcript(stale, "ok", "All done, opening the PR now.")
    assert stale._is_rate_limited({"claude_sid": "ok"}) is False
    # codex runtime: signature check skipped entirely
    _write_transcript(stale, "cdx", THROTTLE_TEXT)
    assert stale._is_rate_limited({"claude_sid": "cdx", "runtime": "codex"}) is False
    # no transcript at all
    assert stale._is_rate_limited({"claude_sid": "ghost"}) is False
    # no sid
    assert stale._is_rate_limited({}) is False


def test_is_transient_throttle(stale):
    """The server-side 429 throttle is transient (no brick/flip); the genuine
    session-limit banner is not. None/empty are safe."""
    assert stale._is_transient_throttle(THROTTLE_TEXT) is True
    assert stale._is_transient_throttle("Server is temporarily limiting requests") is True
    assert stale._is_transient_throttle("anything (not your usage limit) here") is True
    assert stale._is_transient_throttle(SESSION_LIMIT_TEXT) is False
    assert stale._is_transient_throttle("You've hit your session limit · resets soon") is False
    assert stale._is_transient_throttle(None) is False
    assert stale._is_transient_throttle("") is False


def test_is_transient_throttle_matches_529(stale):
    """A 529 Overloaded banner is a transient server-side error: it must be
    classified transient so the brick gate takes its never-brick branch. Case-
    insensitive (matched against text.lower())."""
    assert stale._is_transient_throttle(API_529_TEXT) is True
    assert stale._is_transient_throttle("api error: 529 overloaded.") is True
    # still disjoint from the genuine usage limit
    assert stale._is_transient_throttle(SESSION_LIMIT_TEXT) is False


def test_is_rate_limited_matches_529_transcript(stale):
    """A transcript ending on the 529 banner is detected (nudge-eligible) by the
    shared _limit_banner_text detector — same as a 429."""
    _write_transcript(stale, "w529", API_529_TEXT)
    assert stale._is_rate_limited({"claude_sid": "w529"}) is True


def test_limit_banner_text_529_strict_detects_real_banner_rejects_quote(stale, tmp_path):
    """Manager strict path: the genuine short 529 banner (signature at offset 11,
    <=12) is detected; a long manager message merely QUOTING it (signature deep
    in the text) is rejected by the len/offset guards — exactly as for the 429."""
    real = tmp_path / "real.jsonl"
    real.write_text(_assistant_line(API_529_TEXT) + "\n")
    assert stale._limit_banner_text(real, strict=True) == API_529_TEXT
    quote = tmp_path / "quote.jsonl"
    quote.write_text(_assistant_line(
        "worker-3 reported that it saw API Error: 529 Overloaded and is wedged; "
        "I am relaying this to you so you can decide whether to resume it now.")
        + "\n")
    assert stale._limit_banner_text(quote, strict=True) is None


def test_last_assistant_text_survives_malformed_json_shapes(stale, tmp_path):
    """Valid-JSON-but-wrong-shape lines must be skipped, not crash the scan —
    the transcript is another process's output and any shape can appear."""
    malformed = [
        "[]",                                                  # non-dict event: list
        "123",                                                 # non-dict event: int
        "null",                                                # non-dict event: null
        '"str"',                                               # non-dict event: string
        json.dumps({"type": "assistant", "message": None}),    # null message
        json.dumps({"type": "assistant", "message": "oops"}),  # non-dict message
        json.dumps({"type": "assistant",
                    "message": {"content": [{"type": "text", "text": None}]}}),  # null text
        json.dumps({"type": "assistant",
                    "message": {"content": [{"type": "text", "text": 42}]}}),    # non-str text
    ]
    log = tmp_path / "t.jsonl"
    log.write_text("\n".join(malformed) + "\n")
    assert stale._last_assistant_text(log) is None

    # And a real assistant line after the garbage still wins.
    log.write_text("\n".join(malformed + [_assistant_line("real text")]) + "\n")
    assert stale._last_assistant_text(log) == "real text"


def test_is_rate_limited_never_raises(stale, monkeypatch):
    """_is_rate_limited runs bare inside main()'s scan loop — any unexpected
    failure must read as 'not throttled' (logged to stderr), never propagate."""
    _write_transcript(stale, "w1", THROTTLE_TEXT)

    def boom(log_path, max_bytes=65536):
        raise RuntimeError("poison transcript")

    monkeypatch.setattr(stale, "_last_assistant_text", boom)
    assert stale._is_rate_limited({"claude_sid": "w1"}) is False


# --- auto-nudge (CLAUDE_ORCH_AUTONUDGE=1) -------------------------------------
# Gated OFF by default. When on: every stall detection for an un-question-
# blocked worker with a window id types "resume your task" into its pane (same
# bracketed-paste send-text + Enter mechanism as send_manager_to_worker) and
# emits NUDGED instead of STALE_PROCESSING. Nudges REPEAT while the worker
# stays silent — at each ladder crossing (30/60/120min, then every 60min
# beyond), and the early 429 path once per processing stretch (a delivered
# nudge submits a prompt → fresh stretch → ~5min of new silence re-arms it).
# Safe because staleness is transcript-activity age: busy workers are never
# stale, so repeated nudges only ever hit silent ones, and the first nudge
# after an org-wide 429 resets auto-revives the fleet with no human in the
# loop. Ineligible workers (no window id, pending question, autonudge off)
# page STALE_PROCESSING as before.


@pytest.fixture
def nudgy(stale, monkeypatch):
    monkeypatch.setattr(stale, "AUTONUDGE", True)
    return stale


def _capture_runs(stale, monkeypatch):
    """Record driver send_text / close calls instead of raw terminal argv.

    Returns a list of (kind, window_id, text) tuples — kind is "send_text" or
    "close". The driver is now the single seam for typing into / closing a pane,
    so capturing at _get_driver decouples the tests from tmux argv."""
    calls = []

    class RecordingDriver:
        def send_text(self, window_id, text, submit=True):
            calls.append(("send_text", window_id, text))

        def close(self, window_id):
            calls.append(("close", window_id))

        async def spawn(self, **kw):
            calls.append(("spawn", kw))
            return "%recovery"

    monkeypatch.setattr(stale, "_get_driver", lambda: RecordingDriver())
    return calls


def _send_text_calls(calls):
    """(window_id, text) for each recorded send_text — preserves the prior
    helper's "one entry per typed nudge" length semantics."""
    return [(c[1], c[2]) for c in calls if c[0] == "send_text"]


def test_autonudge_env_gate(monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_AUTONUDGE", raising=False)
    assert _load_stale_monitor().AUTONUDGE is False
    monkeypatch.setenv("CLAUDE_ORCH_AUTONUDGE", "1")
    assert _load_stale_monitor().AUTONUDGE is True
    monkeypatch.setenv("CLAUDE_ORCH_AUTONUDGE", "true")
    assert _load_stale_monitor().AUTONUDGE is False, "gate is the literal string '1'"


def test_autonudge_off_means_no_send_even_when_throttled(stale, capsys, monkeypatch):
    """Default-off: stale + throttled worker still pages STALE_PROCESSING and no
    send-text ever fires (byte-identical to today's behavior)."""
    calls = _capture_runs(stale, monkeypatch)
    now = int(time.time())
    path = _write_record(stale, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    old = now - 2700
    os.utime(path, (old, old))
    log = _write_transcript(stale, "w1", THROTTLE_TEXT)
    os.utime(log, (old, old))  # throttle appended at turn start; silent since
    rc = stale.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "STALE_PROCESSING worker-tab" in out
    assert "NUDGED" not in out
    assert _send_text_calls(calls) == []


def test_autonudge_replaces_first_stale_processing_with_nudge(nudgy, capsys, monkeypatch):
    calls = _capture_runs(nudgy, monkeypatch)
    now = int(time.time())
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    old = now - 2700  # 45min — past the 30min threshold
    os.utime(path, (old, old))
    rc = nudgy.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "NUDGED worker-tab (45min)" in out
    assert "STALE_PROCESSING" not in out
    send_text = _send_text_calls(calls)
    assert len(send_text) == 1
    window_id, text = send_text[0]
    assert window_id == "42"
    assert text == "[MANAGER] resume your task"
    emitted = json.loads(nudgy.EMITTED_STATE.read_text())
    assert emitted.get(f"processing:w1:{old}") == 30, (
        "the crossing must still be recorded so the ladder's cadence math is untouched"
    )


def test_worker_nudge_marked_manager_nudge_unmarked(stale):
    """WORKER-pane nudges carry the manager marker: worker.core.md reads an
    UNMARKED pane message as engineer-direct, so an unmarked daemon nudge would
    masquerade as the human (Tier-2 review I-1). The MANAGER-pane nudge stays
    unmarked — that pane is the human's own console, no attribution rule there.
    stale_monitor is stdlib-only and cannot import the constant, so the literal
    is pinned here against mcp_server.MANAGER_MARKER to catch drift."""
    from dockwright.mcp_server import MANAGER_MARKER
    assert stale.NUDGE_TEXT.startswith(MANAGER_MARKER)
    assert not stale.MANAGER_NUDGE_TEXT.startswith(MANAGER_MARKER)


def test_autonudge_repeats_at_each_threshold_crossing_while_wedged(nudgy, capsys, monkeypatch):
    """Wedged turn (typed nudges never submit, transcript silent): every ladder
    crossing nudges again — 30/60/120min, then every 60min beyond — and the
    moment the transcript grows, nudging stops. An org-wide 429 that outlives
    one nudge is re-kicked shortly after the limit resets, no human needed."""
    calls = _capture_runs(nudgy, monkeypatch)
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    t0 = 1_000_000
    os.utime(path, (t0, t0))
    log = _write_transcript(nudgy, "w1", "working before the wedge")
    os.utime(log, (t0, t0))
    clock = {"now": t0}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])

    def run_at(minutes):
        clock["now"] = t0 + minutes * 60
        nudgy.main()
        return capsys.readouterr().out

    assert "NUDGED worker-tab (30min)" in run_at(30)
    assert "NUDGED" not in run_at(31)                  # [30,60) → dedup
    assert "NUDGED worker-tab (60min)" in run_at(60)
    assert "NUDGED worker-tab (120min)" in run_at(120)
    assert "NUDGED worker-tab (180min)" in run_at(180)  # +60min cadence past 120
    assert "NUDGED" not in run_at(200)                  # [180,240) → dedup
    assert "NUDGED worker-tab (240min)" in run_at(240)
    assert len(_send_text_calls(calls)) == 5
    # Transcript grows (worker revived): nudging stops immediately and the
    # delivery is confirmed as RESUMED — growth is the only confirmation.
    os.utime(log, (t0 + 241 * 60, t0 + 241 * 60))
    out = run_at(242)
    assert "NUDGED" not in out
    assert "RESUMED worker-tab" in out
    assert len(_send_text_calls(calls)) == 5
    # …and a fresh silence window re-arms from the 30min rung.
    assert "NUDGED worker-tab (30min)" in run_at(241 + 30)
    assert len(_send_text_calls(calls)) == 6


@pytest.mark.parametrize("banner", [THROTTLE_TEXT, SESSION_LIMIT_TEXT])
def test_autonudge_429_fires_before_threshold(nudgy, capsys, monkeypatch, banner):
    calls = _capture_runs(nudgy, monkeypatch)
    now = int(time.time())
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    old = now - 360  # 6min — past the 5min floor, far below the 30min threshold
    os.utime(path, (old, old))
    log = _write_transcript(nudgy, "w1", banner)
    os.utime(log, (old, old))  # 6min of transcript silence since the throttle
    rc = nudgy.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "NUDGED worker-tab (6min rate-limited)" in out
    assert "STALE_PROCESSING" not in out
    assert len(_send_text_calls(calls)) == 1
    assert f"nudged:w1:{old}" in json.loads(nudgy.EMITTED_STATE.read_text()), (
        "the 429 nudge dedup is stretch-scoped — keyed on the record mtime"
    )


def test_autonudge_429_below_floor_does_nothing(nudgy, capsys, monkeypatch):
    calls = _capture_runs(nudgy, monkeypatch)
    now = int(time.time())
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    old = now - 120  # 2min — below the 5min floor
    os.utime(path, (old, old))
    log = _write_transcript(nudgy, "w1", THROTTLE_TEXT)
    os.utime(log, (old, old))
    nudgy.main()
    out = capsys.readouterr().out
    assert "NUDGED" not in out
    assert "STALE_PROCESSING" not in out
    assert _send_text_calls(calls) == []


def test_429_renudges_on_new_stretch_while_still_throttled(nudgy, capsys, monkeypatch):
    """Org-wide 429: the first nudge submits a prompt (fresh stretch), the CLI
    retries, hits the same 429, goes silent again. The stretch-scoped dedup
    suppresses re-nudges within a stretch but re-arms on the next one — so the
    first nudge after the limit resets revives the worker, no human needed."""
    calls = _capture_runs(nudgy, monkeypatch)
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    log = _write_transcript(nudgy, "w1", THROTTLE_TEXT)
    t0 = 1_000_000
    os.utime(path, (t0, t0))
    os.utime(log, (t0, t0))  # keep the transcript on the fake-clock timeline
    clock = {"now": t0 + 6 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main()
    assert "NUDGED worker-tab (6min rate-limited)" in capsys.readouterr().out

    # Same stretch one minute later: stretch dedup suppresses a re-type.
    clock["now"] = t0 + 7 * 60
    nudgy.main()
    assert "NUDGED" not in capsys.readouterr().out
    assert len(_send_text_calls(calls)) == 1

    # Nudge submitted: record rewritten + transcript appended (fresh stretch);
    # the CLI retried, hit the same 429, went silent again.
    t1 = t0 + 8 * 60
    os.utime(path, (t1, t1))
    os.utime(log, (t1, t1))
    clock["now"] = t1 + 6 * 60
    nudgy.main()
    out = capsys.readouterr().out
    assert "NUDGED worker-tab (6min rate-limited)" in out, (
        "a fresh stretch re-arms the 429 nudge — the fleet auto-revival path"
    )
    assert "STALE_PROCESSING" not in out
    assert len(_send_text_calls(calls)) == 2


def test_nudge_rearms_after_idle_observation(nudgy, capsys, monkeypatch):
    """A scan observing the worker idle prunes its stale-state keys; a later
    stall must nudge again from the 30min rung."""
    calls = _capture_runs(nudgy, monkeypatch)
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    t0 = 1_000_000
    os.utime(path, (t0, t0))
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main()
    assert "NUDGED worker-tab" in capsys.readouterr().out

    # Worker recovers; a scan observes it idle (autoclose branch is gated by the
    # recent last_autoclose_run from the previous run, so it is not reaped).
    _write_record(nudgy, "w1", agent="worker", state="idle",
                  name="worker-tab", window_id="42")
    clock["now"] = t0 + 31 * 60
    nudgy.main()
    capsys.readouterr()
    emitted = json.loads(nudgy.EMITTED_STATE.read_text())
    assert not any(k.startswith(("nudged:w1", "processing:w1", "nudge_sent:w1"))
                   for k in emitted), (
        "idle observation must prune the worker's stale-state keys"
    )

    # New stall later → nudge re-arms.
    _write_record(nudgy, "w1", agent="worker", state="processing",
                  name="worker-tab", window_id="42")
    t1 = t0 + 60 * 60
    os.utime(nudgy.ACTIVE / "w1.json", (t1, t1))
    clock["now"] = t1 + 30 * 60
    nudgy.main()
    assert "NUDGED worker-tab" in capsys.readouterr().out
    assert len(_send_text_calls(calls)) == 2


def test_no_nudge_with_pending_question(nudgy, capsys, monkeypatch):
    """ask_manager-blocked workers stay state=processing; they must never be
    nudged — the manager pages via STALE_QUESTION/STALE_PROCESSING as today."""
    calls = _capture_runs(nudgy, monkeypatch)
    now = int(time.time())
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    old = now - 2700
    os.utime(path, (old, old))
    _write_question(nudgy, "q1", "w1")
    nudgy.main()
    out = capsys.readouterr().out
    assert "STALE_PROCESSING worker-tab" in out
    assert "NUDGED" not in out
    assert _send_text_calls(calls) == []


def test_no_nudge_without_window_id(nudgy, capsys, monkeypatch):
    calls = _capture_runs(nudgy, monkeypatch)
    now = int(time.time())
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab")  # iterm_sid defaults to ""
    old = now - 2700
    os.utime(path, (old, old))
    nudgy.main()
    out = capsys.readouterr().out
    assert "STALE_PROCESSING worker-tab" in out
    assert "NUDGED" not in out
    assert _send_text_calls(calls) == []


def test_no_nudge_for_legacy_iterm_sid_only_records(nudgy, capsys, monkeypatch):
    """An iTerm sid never matches a tmux pane id — 'nudging' it would no-op
    silently while suppressing the human page (or, on a numeric collision, type
    into a foreign tmux pane). Legacy records fall through to STALE_PROCESSING."""
    calls = _capture_runs(nudgy, monkeypatch)
    now = int(time.time())
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", iterm_sid="7")
    old = now - 2700
    os.utime(path, (old, old))
    nudgy.main()
    out = capsys.readouterr().out
    assert "STALE_PROCESSING worker-tab" in out
    assert "NUDGED" not in out
    assert _send_text_calls(calls) == []


def test_scan_survives_one_workers_poison_transcript(nudgy, capsys, monkeypatch):
    """One worker's poison transcript must never abort the scan loop — the
    other workers' staleness still gets detected and reported in the same run."""
    calls = _capture_runs(nudgy, monkeypatch)
    now = int(time.time())
    bad = _write_record(nudgy, "bad", agent="worker", state="processing",
                        name="bad-tab", window_id="41")
    os.utime(bad, (now - 360, now - 360))        # 6min → hits the 429 check
    bad_log = _write_transcript(nudgy, "bad", THROTTLE_TEXT)  # transcript exists → reader runs
    os.utime(bad_log, (now - 360, now - 360))    # 6min silent → 429 path reached
    healthy = _write_record(nudgy, "ok", agent="worker", state="processing",
                            name="ok-tab", window_id="42")
    os.utime(healthy, (now - 2700, now - 2700))  # 45min → threshold path

    def boom(log_path, max_bytes=65536):
        raise RuntimeError("poison transcript")

    monkeypatch.setattr(nudgy, "_last_assistant_text", boom)
    rc = nudgy.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "NUDGED ok-tab" in out, "healthy worker must still be handled"
    assert "bad-tab" not in out, "poison worker is silently skipped this scan"


def test_manager_records_never_nudged(nudgy, capsys, monkeypatch):
    """Stale-looking manager records are skipped before any nudge logic — even
    with autonudge on and a window id present."""
    calls = _capture_runs(nudgy, monkeypatch)
    now = int(time.time())
    path = _write_record(nudgy, "m1", agent="manager", state="processing",
                         name="manager-tab", window_id="42")
    old = now - 2700
    os.utime(path, (old, old))
    nudgy.main()
    out = capsys.readouterr().out
    assert "NUDGED" not in out
    assert "STALE_PROCESSING" not in out
    assert _send_text_calls(calls) == []


# --- transcript-activity-age staleness (long-turn false-positive fix) ---------
# Staleness keys on transcript silence — now - max(record mtime, transcript
# mtime) — not turn length. A busy worker mid-long-turn keeps appending to its
# jsonl and must neither page nor be nudged; a wedged worker goes silent. No
# resolvable transcript → turn-age fallback (old behavior, never blind).


def _write_codex_transcript(stale, sid, mtime=None):
    day_dir = stale.CODEX_SESSIONS / "2026" / "06" / "10"
    day_dir.mkdir(parents=True, exist_ok=True)
    log = day_dir / f"rollout-2026-06-10T01-02-03-{sid}.jsonl"
    log.write_text('{"type":"response_item"}\n')
    if mtime is not None:
        os.utime(log, (mtime, mtime))
    return log


def test_busy_long_turn_with_fresh_transcript_is_not_stale(stale, capsys):
    """The incident case: 45min into a legitimate turn, transcript still growing."""
    now = int(time.time())
    path = _write_record(stale, "w1", agent="worker", state="processing", name="worker-tab")
    os.utime(path, (now - 2700, now - 2700))
    _write_transcript(stale, "w1", "still working")  # fresh mtime = active now
    rc = stale.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "STALE_PROCESSING" not in out
    assert "NUDGED" not in out


def test_busy_long_turn_with_fresh_transcript_is_not_nudged(nudgy, capsys, monkeypatch):
    """The autonudge must key on the same activity metric — no 'resume your
    task' typed into a busy pane."""
    calls = _capture_runs(nudgy, monkeypatch)
    now = int(time.time())
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    os.utime(path, (now - 2700, now - 2700))
    _write_transcript(nudgy, "w1", "still working")
    nudgy.main()
    out = capsys.readouterr().out
    assert "NUDGED" not in out
    assert "STALE_PROCESSING" not in out
    assert _send_text_calls(calls) == []


def test_silent_transcript_is_stale(stale, capsys):
    now = int(time.time())
    path = _write_record(stale, "w1", agent="worker", state="processing", name="worker-tab")
    os.utime(path, (now - 2700, now - 2700))
    log = _write_transcript(stale, "w1", "last words before the wedge")
    os.utime(log, (now - 2700, now - 2700))
    stale.main()
    assert "STALE_PROCESSING worker-tab (45min)" in capsys.readouterr().out


def test_missing_transcript_falls_back_to_turn_age(stale, capsys):
    now = int(time.time())
    path = _write_record(stale, "w1", agent="worker", state="processing", name="worker-tab")
    os.utime(path, (now - 2700, now - 2700))
    assert not stale.CLAUDE_PROJECTS.exists()
    stale.main()
    assert "STALE_PROCESSING worker-tab (45min)" in capsys.readouterr().out


def test_codex_fresh_rollout_suppresses_stale(stale, capsys):
    now = int(time.time())
    path = _write_record(stale, "cdx1", agent="worker", state="processing",
                         name="codex-tab", runtime="codex")
    os.utime(path, (now - 2700, now - 2700))
    _write_codex_transcript(stale, "cdx1")
    stale.main()
    assert "STALE_PROCESSING" not in capsys.readouterr().out


def test_codex_silent_rollout_is_stale(stale, capsys):
    now = int(time.time())
    path = _write_record(stale, "cdx1", agent="worker", state="processing",
                         name="codex-tab", runtime="codex")
    os.utime(path, (now - 2700, now - 2700))
    _write_codex_transcript(stale, "cdx1", mtime=now - 2700)
    stale.main()
    assert "STALE_PROCESSING codex-tab (45min)" in capsys.readouterr().out


def test_codex_unresolvable_falls_back_to_turn_age(stale, capsys):
    now = int(time.time())
    path = _write_record(stale, "cdx1", agent="worker", state="processing",
                         name="codex-tab", runtime="codex")
    os.utime(path, (now - 2700, now - 2700))
    stale.main()
    assert "STALE_PROCESSING codex-tab (45min)" in capsys.readouterr().out


def test_intra_turn_silence_rearms_after_activity_resumes(stale, capsys, monkeypatch):
    """One long turn (fixed record mtime): silence pages at 30min; transcript
    activity resumes (key pruned); a second silence episode in the same turn
    pages again — each new silence deserves a page."""
    path = _write_record(stale, "w1", agent="worker", state="processing", name="worker-tab")
    t0 = 1_000_000
    os.utime(path, (t0, t0))
    log = _write_transcript(stale, "w1", "working")
    os.utime(log, (t0, t0))
    clock = {"now": t0}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])

    clock["now"] = t0 + 30 * 60
    stale.main()
    assert "STALE_PROCESSING worker-tab (30min)" in capsys.readouterr().out

    t1 = t0 + 31 * 60
    os.utime(log, (t1, t1))
    clock["now"] = t1 + 60
    stale.main()
    assert "STALE_PROCESSING" not in capsys.readouterr().out
    emitted = json.loads(stale.EMITTED_STATE.read_text())
    assert f"processing:w1:{t0}" not in emitted, "key pruned when activity resumes"

    clock["now"] = t1 + 30 * 60
    stale.main()
    assert "STALE_PROCESSING worker-tab (30min)" in capsys.readouterr().out


def test_last_activity_mtime_crash_proof(stale, monkeypatch):
    """Resolver blowing up must read as 'no transcript' (turn-age fallback),
    never propagate into the scan loop."""
    def boom(sid):
        raise RuntimeError("poison resolver")
    monkeypatch.setattr(stale, "_find_claude_session_log", boom)
    assert stale._last_activity_mtime({"claude_sid": "w1"}, 12345) == 12345


def test_highest_nudge_threshold_cadence(stale):
    """Doubling to 4x base (30/60/120 by default), then flat 60min steps —
    including env-overridden bases."""
    assert stale._highest_nudge_threshold(29, 30) is None
    assert stale._highest_nudge_threshold(30, 30) == 30
    assert stale._highest_nudge_threshold(59, 30) == 30
    assert stale._highest_nudge_threshold(60, 30) == 60
    assert stale._highest_nudge_threshold(119, 30) == 60
    assert stale._highest_nudge_threshold(120, 30) == 120
    assert stale._highest_nudge_threshold(179, 30) == 120
    assert stale._highest_nudge_threshold(180, 30) == 180
    assert stale._highest_nudge_threshold(240, 30) == 240
    assert stale._highest_nudge_threshold(299, 30) == 240
    assert stale._highest_nudge_threshold(90, 45) == 90
    assert stale._highest_nudge_threshold(180, 45) == 180
    assert stale._highest_nudge_threshold(239, 45) == 180
    assert stale._highest_nudge_threshold(240, 45) == 240
    assert stale._highest_nudge_threshold(4, 1) == 4
    assert stale._highest_nudge_threshold(63, 1) == 4
    assert stale._highest_nudge_threshold(64, 1) == 64


# --- RESUMED: nudge delivery confirmation --------------------------------------
# A typed nudge is an attempt, not a delivery: a CLI sitting on a limit banner
# swallows input without starting a turn (verified against incident transcripts
# — NUDGED lines with zero "resume your task" user messages). Transcript growth
# after the nudge is the ONLY delivery confirmation; it surfaces as RESUMED
# (once), and until it happens the ladder keeps re-nudging.


def test_resumed_emitted_once_when_transcript_grows_after_nudge(nudgy, capsys, monkeypatch):
    _capture_runs(nudgy, monkeypatch)
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    t0 = 1_000_000
    os.utime(path, (t0, t0))
    log = _write_transcript(nudgy, "w1", "working")
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main()
    assert "NUDGED worker-tab" in capsys.readouterr().out

    # Nudge delivered: transcript grew. Next scan reports RESUMED exactly once.
    os.utime(log, (t0 + 31 * 60, t0 + 31 * 60))
    clock["now"] = t0 + 32 * 60
    nudgy.main()
    assert "RESUMED worker-tab" in capsys.readouterr().out
    clock["now"] = t0 + 33 * 60
    nudgy.main()
    out = capsys.readouterr().out
    assert "RESUMED" not in out, "delivery confirmation is one-shot"
    assert "NUDGED" not in out


# --- banner-parsed scheduled nudges + limit-aware manager handling ------------
# The session-limit banner carries a reset time ("resets 2:20am (Asia/Novosibirsk)").
# Parsing is best-effort (wording is fragile): success schedules a nudge at
# reset+2min; failure falls back to the ladder (workers) or a flat 10min retry
# (managers — they have no ladder). The owning manager, when itself limited,
# buffers all event lines and emits one rollup on recovery.


from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def test_parse_limit_reset_ts_valid_variants(stale):
    # Each variant's `now` sits a couple of hours before the reset wall-time —
    # a parse further out than the 5h session window is a stale banner (None).
    for text, h24, minute, tzname, now_dt in [
        ("You’ve hit your session limit · resets 2:20am (Asia/Novosibirsk)",
         2, 20, "Asia/Novosibirsk", datetime(2026, 6, 11, 1, 0, tzinfo=ZoneInfo("Asia/Novosibirsk"))),
        ("You've hit your session limit · resets 6pm (UTC)",
         18, 0, "UTC", datetime(2026, 6, 11, 15, 0, tzinfo=ZoneInfo("UTC"))),
        ("hit your session limit · resets 12:05am (UTC)",
         0, 5, "UTC", datetime(2026, 6, 10, 22, 0, tzinfo=ZoneInfo("UTC"))),
        ("hit your session limit · resets 12:05pm (UTC)",
         12, 5, "UTC", datetime(2026, 6, 11, 9, 0, tzinfo=ZoneInfo("UTC"))),
    ]:
        now = int(now_dt.timestamp())
        ts = stale._parse_limit_reset_ts(text, now)
        assert isinstance(ts, int), text
        fire_dt = datetime.fromtimestamp(ts - 120, ZoneInfo(tzname))
        assert (fire_dt.hour, fire_dt.minute) == (h24, minute), text
        assert now < ts - 120 <= now + 6 * 3600, "next occurrence within the plausible window"


def test_parse_limit_reset_ts_rolls_to_next_day_when_past(stale):
    tz = ZoneInfo("UTC")
    now = int(datetime(2026, 6, 11, 23, 0, tzinfo=tz).timestamp())
    ts = stale._parse_limit_reset_ts("hit your session limit · resets 2:20am (UTC)", now)
    fire_dt = datetime.fromtimestamp(ts - 120, tz)
    assert fire_dt.day == 12, "02:20 already past at 23:00 → tomorrow (3.3h out)"


def test_parse_limit_reset_ts_stale_banner_returns_none(stale):
    """A parse landing >6h out means the banner's wall-time already passed and
    rolled to tomorrow (session-limit windows are 5h) — the banner is stale.
    Treat as unparseable: workers fall back to the ladder, managers to the
    flat retry. Without this, a limit hit minutes before its own reset
    boundary schedules the recovery nudge ~24h out."""
    tz = ZoneInfo("UTC")
    just_past = int(datetime(2026, 6, 11, 2, 21, tzinfo=tz).timestamp())
    assert stale._parse_limit_reset_ts(
        "hit your session limit · resets 2:20am (UTC)", just_past) is None
    still_ahead = int(datetime(2026, 6, 11, 0, 0, tzinfo=tz).timestamp())
    assert stale._parse_limit_reset_ts(
        "hit your session limit · resets 2:20am (UTC)", still_ahead) is not None


def test_parse_limit_reset_ts_defensive(stale):
    t0 = 1_000_000
    for bad in [
        "no reset clause at all",
        "resets 2:20am (Mars/Olympus_Mons)",          # unknown zone
        "resets 25:99pm (UTC)",                        # nonsense time
        "resets sometime (UTC)",                       # no time
        "resets 2:20am",                               # no zone
        "",
    ]:
        assert stale._parse_limit_reset_ts(bad, t0) is None, bad
    assert stale._parse_limit_reset_ts(None, t0) is None


def test_session_limit_banner_schedules_worker_nudge(nudgy, capsys, monkeypatch):
    """Fast-path detection with a parseable banner: immediate nudge + a second
    one scheduled for reset+2min; the scheduled fire stamps nudge_sent and
    drops the key."""
    calls = _capture_runs(nudgy, monkeypatch)
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    log = _write_transcript(nudgy, "w1", SESSION_LIMIT_TEXT)
    t0 = 1_000_000
    os.utime(path, (t0, t0))
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 6 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main()
    assert "NUDGED worker-tab (6min rate-limited)" in capsys.readouterr().out
    emitted = json.loads(nudgy.EMITTED_STATE.read_text())
    sched = emitted.get("scheduled:w1")
    assert isinstance(sched, dict) and sched["at"] > clock["now"], (
        "parseable banner must schedule a post-reset nudge"
    )

    # Not due yet → no limit-reset fire (the silence ladder may still nudge on
    # its own cadence — it stays the catch-all and is asserted elsewhere).
    clock["now"] = sched["at"] - 60
    nudgy.main()
    assert "(limit-reset)" not in capsys.readouterr().out
    sends_before_due = len(_send_text_calls(calls))

    # Due → fires once with the limit-reset tag, key dropped.
    clock["now"] = sched["at"]
    nudgy.main()
    out = capsys.readouterr().out
    assert "NUDGED worker-tab (limit-reset)" in out
    assert out.count("NUDGED") == 1, "scheduled fire must not double with the ladder"
    assert len(_send_text_calls(calls)) == sends_before_due + 1
    emitted = json.loads(nudgy.EMITTED_STATE.read_text())
    assert "scheduled:w1" not in emitted
    assert "nudge_sent:w1" in emitted


def test_scheduled_worker_nudge_selfcancels_when_worker_moves(nudgy, capsys, monkeypatch):
    """Genuine resume = activity past the baseline AND the transcript no longer
    ends on a limit banner. (Banner-still-last-text growth is the failed-retry
    signature and must NOT cancel — asserted in
    test_scheduled_nudge_fires_at_reset_despite_failed_retry_growth.)"""
    calls = _capture_runs(nudgy, monkeypatch)
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    log = _write_transcript(nudgy, "w1", SESSION_LIMIT_TEXT)
    t0 = 1_000_000
    os.utime(path, (t0, t0))
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 6 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main()
    capsys.readouterr()
    sched = json.loads(nudgy.EMITTED_STATE.read_text())["scheduled:w1"]

    # Worker moves on its own just before the fire time — real assistant text
    # past the banner (recent enough that the ladder is also quiet at the fire
    # scan).
    _append_transcript(nudgy, "w1", "limit lifted early — resuming the task",
                       sched["at"] - 60)
    clock["now"] = sched["at"]
    nudgy.main()
    out = capsys.readouterr().out
    assert "NUDGED" not in out, "moved worker must not get the scheduled nudge"
    assert len(_send_text_calls(calls)) == 1
    assert "scheduled:w1" not in json.loads(nudgy.EMITTED_STATE.read_text())


def test_throttle_banner_without_reset_clause_schedules_nothing(nudgy, capsys, monkeypatch):
    _capture_runs(nudgy, monkeypatch)
    now = int(time.time())
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    old = now - 360
    os.utime(path, (old, old))
    log = _write_transcript(nudgy, "w1", THROTTLE_TEXT)
    os.utime(log, (old, old))
    nudgy.main()
    assert "NUDGED worker-tab" in capsys.readouterr().out
    assert "scheduled:w1" not in json.loads(nudgy.EMITTED_STATE.read_text())


def test_armed_schedule_suppresses_5min_relimit_nudges(nudgy, capsys, monkeypatch):
    """Session-limit storm (2026-06-11: 226 NUDGED / 192 RESUMED over one 3.3h
    window): each delivered 5-min nudge retried into the same hard limit, the
    failed retry grew the transcript (fresh stretch + false RESUMED), silence
    resumed, and 5min later the lane re-fired. While a banner-scheduled nudge
    is armed the 5-min lane must stay quiet; the 30/60/120 ladder remains the
    catch-all for wrong parses and limits clearing early."""
    calls = _capture_runs(nudgy, monkeypatch)
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    log = _write_transcript(nudgy, "w1", SESSION_LIMIT_TEXT)
    t0 = 1_000_000
    os.utime(path, (t0, t0))
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 6 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main()
    assert "NUDGED worker-tab (6min rate-limited)" in capsys.readouterr().out
    assert "scheduled:w1" in json.loads(nudgy.EMITTED_STATE.read_text())
    assert len(_send_text_calls(calls)) == 1

    # Nudge delivered: prompt submit rewrote the record (fresh stretch), the
    # CLI retried into the same limit, appended a fresh banner, went silent.
    t1 = t0 + 7 * 60
    os.utime(path, (t1, t1))
    _append_transcript(nudgy, "w1", SESSION_LIMIT_TEXT, t1)
    clock["now"] = t1 + 60
    nudgy.main()
    assert "RESUMED worker-tab" in capsys.readouterr().out

    # 5+min of new silence in the fresh stretch: the lane must NOT re-fire.
    clock["now"] = t1 + 6 * 60
    nudgy.main()
    out = capsys.readouterr().out
    assert "NUDGED" not in out, "5-min lane must be suppressed while the schedule is armed"
    assert len(_send_text_calls(calls)) == 1

    # The 30min ladder stays live as the catch-all.
    clock["now"] = t1 + 30 * 60
    nudgy.main()
    assert "NUDGED worker-tab (30min)" in capsys.readouterr().out
    assert len(_send_text_calls(calls)) == 2


def test_scheduled_nudge_fires_at_reset_despite_failed_retry_growth(nudgy, capsys, monkeypatch):
    """The schedule's baseline is captured pre-nudge; a delivered nudge's failed
    retry appends a fresh banner and advances activity past it. At reset+2min
    the worker is still bricked — banner as the transcript's final text — so
    the scheduled nudge must fire, not self-cancel."""
    calls = _capture_runs(nudgy, monkeypatch)
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    log = _write_transcript(nudgy, "w1", SESSION_LIMIT_TEXT)
    t0 = 1_000_000
    os.utime(path, (t0, t0))
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 6 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main()
    capsys.readouterr()
    sched = json.loads(nudgy.EMITTED_STATE.read_text())["scheduled:w1"]

    # Delivered nudge → fresh stretch + failed-retry banner append → silence.
    t1 = t0 + 7 * 60
    os.utime(path, (t1, t1))
    _append_transcript(nudgy, "w1", SESSION_LIMIT_TEXT, t1)
    clock["now"] = t1 + 60
    nudgy.main()
    assert "RESUMED worker-tab" in capsys.readouterr().out
    sends_before_due = len(_send_text_calls(calls))

    clock["now"] = sched["at"]
    nudgy.main()
    out = capsys.readouterr().out
    assert "NUDGED worker-tab (limit-reset)" in out, (
        "failed-retry growth must not self-cancel the reset+2min fire"
    )
    assert out.count("NUDGED") == 1, "scheduled fire must not double with the ladder"
    assert len(_send_text_calls(calls)) == sends_before_due + 1
    assert "scheduled:w1" not in json.loads(nudgy.EMITTED_STATE.read_text())


def test_scheduled_nudge_fires_when_growth_is_tool_calls_only(nudgy, capsys, monkeypatch):
    """Deliberate tradeoff pin: transcript growth whose assistant turns carry no
    text (tool-calls only) leaves the banner as the last assistant TEXT, so the
    due fire treats the worker as still bricked and nudges. A genuinely-resumed
    worker in a tool-only stretch gets one redundant queued prompt — benign by
    the module's own stance, and strictly better than missing the post-reset
    revival."""
    calls = _capture_runs(nudgy, monkeypatch)
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    log = _write_transcript(nudgy, "w1", SESSION_LIMIT_TEXT)
    t0 = 1_000_000
    os.utime(path, (t0, t0))
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 6 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main()
    capsys.readouterr()
    sched = json.loads(nudgy.EMITTED_STATE.read_text())["scheduled:w1"]

    tool_only = json.dumps({"type": "assistant",
                            "message": {"content": [{"type": "tool_use", "name": "Bash"}]}})
    with open(log, "a") as f:
        f.write(tool_only + "\n")
    os.utime(log, (sched["at"] - 60, sched["at"] - 60))
    clock["now"] = sched["at"]
    nudgy.main()
    assert "NUDGED worker-tab (limit-reset)" in capsys.readouterr().out
    assert len(_send_text_calls(calls)) == 2


def test_unparseable_session_limit_reset_keeps_5min_lane(nudgy, capsys, monkeypatch):
    """No schedule armed (reset clause unparseable) → the per-stretch 5-min lane
    stays live: it is the early-clear revival path for org-429s and bad parses.
    Regression pin for the suppression change — must hold before and after."""
    calls = _capture_runs(nudgy, monkeypatch)
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    banner = "You've hit your session limit · resets soon"
    log = _write_transcript(nudgy, "w1", banner)
    t0 = 1_000_000
    os.utime(path, (t0, t0))
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 6 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main()
    assert "NUDGED worker-tab (6min rate-limited)" in capsys.readouterr().out
    assert "scheduled:w1" not in json.loads(nudgy.EMITTED_STATE.read_text())

    # Delivered, retried, failed, silent again — fresh stretch re-arms the lane.
    t1 = t0 + 7 * 60
    os.utime(path, (t1, t1))
    _append_transcript(nudgy, "w1", banner, t1)
    clock["now"] = t1 + 6 * 60
    nudgy.main()
    out = capsys.readouterr().out
    assert "NUDGED worker-tab (6min rate-limited)" in out
    assert len(_send_text_calls(calls)) == 2


def _write_limited_manager(stale, sid, manager_name, t0, window_id="9",
                           text=SESSION_LIMIT_TEXT, **overrides):
    path = _write_record(stale, sid, agent="manager", state="processing",
                         name=manager_name, window_id=window_id,
                         parent_manager_name=None, **overrides)
    os.utime(path, (t0, t0))
    log = _write_transcript(stale, sid, text)
    os.utime(log, (t0, t0))
    return path, log


MANAGER_NUDGE = "rate limit cleared — check list_workers and queued events, resume orchestration"


def test_limited_manager_gets_scheduled_nudge_and_flat_rearm(nudgy, capsys, monkeypatch):
    """Managers get ONLY the limit-recovery nudge: scheduled at reset+2min,
    re-armed at a flat 10min cadence while detection holds (never re-parsed
    from the stale banner — that would resolve to tomorrow)."""
    calls = _capture_runs(nudgy, monkeypatch)
    t0 = 1_000_000
    _write_limited_manager(nudgy, "mgr1", "mgr-A", t0)
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])

    nudgy.main(manager_name="mgr-A")
    capsys.readouterr()
    emitted_path = nudgy._emitted_state_path("mgr-A")
    sched = json.loads(emitted_path.read_text())["scheduled:mgr1"]
    assert sched["at"] > clock["now"]
    assert _send_text_calls(calls) == []

    # Due: fires the manager-specific text into the manager's window, re-arms +10min.
    clock["now"] = sched["at"]
    nudgy.main(manager_name="mgr-A")
    sends = _send_text_calls(calls)
    assert len(sends) == 1
    window_id, text = sends[0]
    assert window_id == "9"
    assert text == MANAGER_NUDGE
    sched2 = json.loads(emitted_path.read_text())["scheduled:mgr1"]
    assert sched2["at"] == clock["now"] + 600, "swallowed fire re-arms at flat 10min"

    # Still limited at the re-armed time → fires again.
    clock["now"] = sched2["at"]
    nudgy.main(manager_name="mgr-A")
    assert len(_send_text_calls(calls)) == 2


def test_limited_manager_parse_failure_falls_back_to_flat_retry(nudgy, capsys, monkeypatch):
    """No ladder for managers — an unparseable banner must still schedule the
    flat 10min retry, or buffered events would be held until a human unbricks."""
    _capture_runs(nudgy, monkeypatch)
    t0 = 1_000_000
    path = _write_record(nudgy, "mgr1", agent="manager", state="processing",
                         name="mgr-A", window_id="9")
    os.utime(path, (t0, t0))
    log = _write_transcript(nudgy, "mgr1", THROTTLE_TEXT)  # transient throttle, no parseable reset
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main(manager_name="mgr-A")
    sched = json.loads(nudgy._emitted_state_path("mgr-A").read_text())["scheduled:mgr1"]
    assert sched["at"] == clock["now"] + 600


def test_manager_limited_coalesces_events_then_rolls_up(nudgy, capsys, monkeypatch):
    """While the owning manager is limited: no lines printed (each would be a
    wasted wake attempt), actions still proceed, flag file set. On recovery:
    one rollup line with counts, then the normal stream."""
    calls = _capture_runs(nudgy, monkeypatch)
    t0 = 1_000_000
    _, mlog = _write_limited_manager(nudgy, "mgr1", "mgr-A", t0)
    w = _write_record(nudgy, "w1", agent="worker", state="processing",
                      name="worker-tab", window_id="42", parent_manager_name="mgr-A")
    os.utime(w, (t0, t0))
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])

    nudgy.main(manager_name="mgr-A")
    assert capsys.readouterr().out == "", "everything buffered while manager limited"
    assert (nudgy.ROOT / ".manager-limited-mgr-A").exists()
    assert len(_send_text_calls(calls)) == 1, "worker nudge ACTION still proceeds"

    # A done event lands while down (counted in the rollup as not-yet-surfaced).
    done_dir = nudgy.ROOT / "done" / "mgr-A"
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / "e1.json").write_text("{}")

    # Recovery: manager transcript ends on a normal message.
    mlog2 = _write_transcript(nudgy, "mgr1", "back to work")
    os.utime(mlog2, (t0 + 31 * 60, t0 + 31 * 60))
    clock["now"] = t0 + 31 * 60
    nudgy.main(manager_name="mgr-A")
    out = capsys.readouterr().out
    assert "limit cleared" in out
    assert "while down: 1 workers stalled, 1 nudged, 1 done events" in out
    assert not (nudgy.ROOT / ".manager-limited-mgr-A").exists()
    assert "limited_buffer" not in json.loads(nudgy._emitted_state_path("mgr-A").read_text())

    # Stream back to normal: a fresh stale crossing prints live.
    clock["now"] = t0 + 60 * 60  # worker silent 60min → 60min rung
    nudgy.main(manager_name="mgr-A")
    assert "NUDGED worker-tab (60min)" in capsys.readouterr().out


def test_healthy_manager_never_suppresses(nudgy, capsys, monkeypatch):
    """Suppression ONLY on positive limited-detection: an active manager (fresh
    transcript) must not delay worker events."""
    _capture_runs(nudgy, monkeypatch)
    now = int(time.time())
    mpath = _write_record(nudgy, "mgr1", agent="manager", state="processing",
                          name="mgr-A", window_id="9")
    os.utime(mpath, (now - 3600, now - 3600))
    _write_transcript(nudgy, "mgr1", "actively orchestrating")  # fresh mtime
    w = _write_record(nudgy, "w1", agent="worker", state="processing",
                      name="worker-tab", window_id="42", parent_manager_name="mgr-A")
    os.utime(w, (now - 2700, now - 2700))
    nudgy.main(manager_name="mgr-A")
    out = capsys.readouterr().out
    assert "NUDGED worker-tab (45min)" in out
    assert not (nudgy.ROOT / ".manager-limited-mgr-A").exists()


def test_manager_never_gets_ladder_or_stale_processing(nudgy, capsys, monkeypatch):
    """The limit-recovery path is the ONLY manager touchpoint — a silent
    processing manager with a non-limit transcript gets nothing."""
    calls = _capture_runs(nudgy, monkeypatch)
    t0 = 1_000_000
    mpath = _write_record(nudgy, "mgr1", agent="manager", state="processing",
                          name="mgr-A", window_id="9")
    os.utime(mpath, (t0, t0))
    log = _write_transcript(nudgy, "mgr1", "thinking about life")
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 120 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main(manager_name="mgr-A")
    out = capsys.readouterr().out
    assert "NUDGED" not in out
    assert "STALE_PROCESSING" not in out
    assert _send_text_calls(calls) == []


def test_scheduled_fire_skipped_when_question_arrives(nudgy, capsys, monkeypatch):
    """Eligibility is re-checked at fire time: a pending question that arrived
    after scheduling drops the due key without typing into the pane."""
    calls = _capture_runs(nudgy, monkeypatch)
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    log = _write_transcript(nudgy, "w1", SESSION_LIMIT_TEXT)
    t0 = 1_000_000
    os.utime(path, (t0, t0))
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 6 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main()
    capsys.readouterr()
    sched = json.loads(nudgy.EMITTED_STATE.read_text())["scheduled:w1"]

    _write_question(nudgy, "q1", "w1")
    clock["now"] = sched["at"]
    nudgy.main()
    out = capsys.readouterr().out
    assert "(limit-reset)" not in out, "question-blocked worker must not be typed into"
    assert len(_send_text_calls(calls)) == 1
    assert "scheduled:w1" not in json.loads(nudgy.EMITTED_STATE.read_text())


def test_scheduled_key_survives_gated_young_turn_scan(nudgy, capsys, monkeypatch):
    """A scan where the worker's turn is too young for the lazy gate must still
    carry a not-yet-due scheduled key — next_emitted is a full rewrite, so the
    carry has to happen before the gate's `continue`."""
    _capture_runs(nudgy, monkeypatch)
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    log = _write_transcript(nudgy, "w1", SESSION_LIMIT_TEXT)
    t0 = 1_000_000
    os.utime(path, (t0, t0))
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 6 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main()  # immediate nudge + schedule
    capsys.readouterr()

    # The nudge delivered: fresh stretch, transcript grew (RESUMED scan).
    t1 = t0 + 7 * 60
    os.utime(path, (t1, t1))
    os.utime(log, (t1, t1))
    clock["now"] = t1 + 60
    nudgy.main()
    capsys.readouterr()

    # Young-turn gated scan (no nudge_sent marker anymore, schedule not due).
    clock["now"] = t1 + 120
    nudgy.main()
    assert "scheduled:w1" in json.loads(nudgy.EMITTED_STATE.read_text())


def test_recovery_rollup_survives_poisoned_buffer(nudgy, capsys, monkeypatch):
    """Malformed limited_buffer counters (hand-edit/corruption) must read as 0,
    not crash — a crash here fires before the flag unlink and would crash-loop
    every scan with the monitor.py scans held indefinitely."""
    t0 = 1_000_000
    clock = {"now": t0}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy._emitted_state_path("mgr-A").write_text(json.dumps({"limited_buffer": {
        "since": t0, "stalled_names": "oops", "nudged": "oops", "resumed": None}}))
    (nudgy.ROOT / ".manager-limited-mgr-A").touch()
    rc = nudgy.main(manager_name="mgr-A")
    assert rc == 0
    out = capsys.readouterr().out
    assert "limit cleared" in out
    assert "0 workers stalled, 0 nudged" in out
    assert not (nudgy.ROOT / ".manager-limited-mgr-A").exists()


def test_manager_banner_match_is_strict(nudgy, capsys, monkeypatch):
    """A manager whose last message merely QUOTES a banner (long text, the
    signature mid-sentence) must not read as limited — the blast radius would
    be suppressed events plus text typed into a live AskUserQuestion pane."""
    _capture_runs(nudgy, monkeypatch)
    t0 = 1_000_000
    mpath = _write_record(nudgy, "mgr1", agent="manager", state="processing",
                          name="mgr-A", window_id="9")
    os.utime(mpath, (t0, t0))
    quoting = ("Worker alpha is stuck: its transcript ends with 'You've hit your "
               "session limit · resets 2:20am (Asia/Novosibirsk)' — I'll nudge it "
               "once the limit clears. Meanwhile, which PR should beta pick up next?")
    log = _write_transcript(nudgy, "mgr1", quoting)
    os.utime(log, (t0, t0))
    w = _write_record(nudgy, "w1", agent="worker", state="processing",
                      name="worker-tab", window_id="42", parent_manager_name="mgr-A")
    os.utime(w, (t0, t0))
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main(manager_name="mgr-A")
    out = capsys.readouterr().out
    assert "NUDGED worker-tab (30min)" in out, "quoting manager must not suppress events"
    assert not (nudgy.ROOT / ".manager-limited-mgr-A").exists()


def test_manager_short_relay_quote_is_not_limited(nudgy, capsys, monkeypatch):
    """A SHORT relay line quoting a worker banner ("worker-1: You've hit your
    session limit …", signature offset 17) must not pass the strict match —
    only genuine banner starts do (offsets 7/10 in the real banners). A false
    positive here types MANAGER_NUDGE_TEXT into a healthy manager's pane every
    10min until a human responds."""
    calls = _capture_runs(nudgy, monkeypatch)
    t0 = 1_000_000
    mpath = _write_record(nudgy, "mgr1", agent="manager", state="processing",
                          name="mgr-A", window_id="9")
    os.utime(mpath, (t0, t0))
    relay = "worker-1: You’ve hit your session limit · resets 2:20am (Asia/Novosibirsk)"
    log = _write_transcript(nudgy, "mgr1", relay)
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main(manager_name="mgr-A")
    assert not (nudgy.ROOT / ".manager-limited-mgr-A").exists()
    assert "scheduled:mgr1" not in json.loads(nudgy._emitted_state_path("mgr-A").read_text())
    assert _send_text_calls(calls) == []


def test_limited_flag_mtime_refreshed_while_limited(nudgy, capsys, monkeypatch):
    """Each limited scan must refresh the flag mtime — the monitor.py readers
    treat an old mtime as a dead stale loop and fail open."""
    _capture_runs(nudgy, monkeypatch)
    t0 = 1_000_000
    _write_limited_manager(nudgy, "mgr1", "mgr-A", t0)
    clock = {"now": t0 + 5 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main(manager_name="mgr-A")
    flag = nudgy.ROOT / ".manager-limited-mgr-A"
    assert flag.exists()
    os.utime(flag, (1000, 1000))
    clock["now"] = t0 + 6 * 60
    nudgy.main(manager_name="mgr-A")
    assert flag.stat().st_mtime > 1000


def test_buffered_page_rung_refires_live_after_recovery(stale, capsys, monkeypatch):
    """A STALE_PROCESSING rung that fired into the buffer must re-fire live
    after recovery — a count in the rollup alone must not burn the rung, or
    the first post-recovery reminder waits for the NEXT doubling."""
    t0 = 1_000_000
    _write_limited_manager(stale, "mgr1", "mgr-A", t0)
    w = _write_record(stale, "w1", agent="worker", state="processing",
                      name="worker-tab", parent_manager_name="mgr-A")
    os.utime(w, (t0, t0))
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")  # 30min rung fires into the buffer
    assert capsys.readouterr().out == ""

    mlog = _write_transcript(stale, "mgr1", "back to work")
    os.utime(mlog, (t0 + 31 * 60, t0 + 31 * 60))
    clock["now"] = t0 + 31 * 60
    stale.main(manager_name="mgr-A")
    assert "limit cleared" in capsys.readouterr().out

    clock["now"] = t0 + 32 * 60
    stale.main(manager_name="mgr-A")
    assert "STALE_PROCESSING worker-tab (32min)" in capsys.readouterr().out, (
        "the buffered 30min rung must re-fire live, not wait for 60min"
    )


def test_limited_manager_coalesces_but_never_nudged_when_autonudge_off(stale, capsys, monkeypatch):
    """Coalescing is unconditional (the suppressed lines were wasted wake
    attempts regardless); typed manager nudges belong to the opt-in autonudge
    feature like worker nudges do."""
    calls = _capture_runs(stale, monkeypatch)
    t0 = 1_000_000
    _write_limited_manager(stale, "mgr1", "mgr-A", t0)
    w = _write_record(stale, "w1", agent="worker", state="processing",
                      name="worker-tab", parent_manager_name="mgr-A")
    os.utime(w, (t0, t0))
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    assert capsys.readouterr().out == "", "STALE_PROCESSING buffered while limited"
    assert (stale.ROOT / ".manager-limited-mgr-A").exists()
    assert _send_text_calls(calls) == []
    assert "scheduled:mgr1" not in json.loads(stale._emitted_state_path("mgr-A").read_text())


# --- verifier minors: codex path cache + single transcript resolution ---------


def test_codex_transcript_path_cached_across_scans(stale, capsys, monkeypatch):
    now = int(time.time())
    path = _write_record(stale, "cdx1", agent="worker", state="processing",
                         name="codex-tab", runtime="codex")
    os.utime(path, (now - 2700, now - 2700))
    _write_codex_transcript(stale, "cdx1", mtime=now - 2700)

    real_resolver = stale._find_codex_session_log
    counter = {"n": 0}

    def counting_resolver(sid):
        counter["n"] += 1
        return real_resolver(sid)

    monkeypatch.setattr(stale, "_find_codex_session_log", counting_resolver)
    stale.main()
    assert counter["n"] == 1
    emitted = json.loads(stale.EMITTED_STATE.read_text())
    assert "cdx1" in (emitted.get("codex_log_cache") or {})

    stale.main()
    assert counter["n"] == 1, "second scan must hit the cached path, not re-rglob"


def test_429_path_resolves_transcript_once_per_scan(nudgy, monkeypatch, capsys):
    now = int(time.time())
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    old = now - 360
    os.utime(path, (old, old))
    log = _write_transcript(nudgy, "w1", THROTTLE_TEXT)
    os.utime(log, (old, old))

    real_resolver = nudgy._find_claude_session_log
    counter = {"n": 0}

    def counting_resolver(sid):
        counter["n"] += 1
        return real_resolver(sid)

    monkeypatch.setattr(nudgy, "_find_claude_session_log", counting_resolver)
    nudgy.main()
    assert "NUDGED worker-tab (6min rate-limited)" in capsys.readouterr().out
    assert counter["n"] == 1, "the 429 banner read must reuse the activity resolution"


def test_undelivered_nudge_keeps_renudging_without_resumed(nudgy, capsys, monkeypatch):
    """Swallowed nudge (limit banner): transcript stays silent → no RESUMED,
    and the ladder keeps re-nudging."""
    calls = _capture_runs(nudgy, monkeypatch)
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    t0 = 1_000_000
    os.utime(path, (t0, t0))
    log = _write_transcript(nudgy, "w1", "working")
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main()
    capsys.readouterr()

    # The scan right after the swallowed nudge (sub-threshold, marker
    # outstanding): no RESUMED, no NUDGED, marker carried.
    clock["now"] = t0 + 31 * 60
    nudgy.main()
    out = capsys.readouterr().out
    assert "RESUMED" not in out
    assert "NUDGED" not in out
    assert "nudge_sent:w1" in json.loads(nudgy.EMITTED_STATE.read_text())

    clock["now"] = t0 + 60 * 60
    nudgy.main()
    out = capsys.readouterr().out
    assert "RESUMED" not in out
    assert "NUDGED worker-tab (60min)" in out
    assert len(_send_text_calls(calls)) == 2


# ---- nested sub-sessions are invisible to the stale monitor ----------------

def test_nested_processing_record_never_pages(stale, tmp_path, capsys):
    path = _write_record(stale, "n1", state="processing", nested=True,
                         name="nested-abcd1234")
    old = time.time() - 3600
    os.utime(path, (old, old))
    stale.main()
    assert "STALE_PROCESSING" not in capsys.readouterr().out


def test_nested_idle_record_never_autoclosed(stale, tmp_path, capsys):
    path = _write_record(stale, "n1", state="idle", nested=True,
                         name="nested-abcd1234",
                         last_turn_at="2020-01-01T00:00:00+00:00")
    stale.main()
    assert "AUTOCLOSED" not in capsys.readouterr().out
    assert path.exists()


def test_autoclose_closed_record_carries_spend(stale, monkeypatch):
    """_autoclose_idle_worker must copy the active record's spend dict into
    the closed record — autoclosed workers are the dominant closure path and
    must not lose accumulated token/cost data.
    """
    now = int(time.time())
    spend = {
        "in_tokens": 12345,
        "out_tokens": 6789,
        "cost_usd": 0.042,
        "last_turn_out": 500,
        "last_msg_id": "msg_abc",
    }
    _write_record(
        stale, "s1",
        pid=12345,
        iterm_sid="7",
        last_turn_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10_000)),
        spend=spend,
    )
    monkeypatch.setattr(stale.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(stale.os, "kill", lambda *a, **k: None)

    rc = stale.main()
    assert rc == 0
    closed_data = json.loads((stale.CLOSED / "s1.json").read_text())
    assert closed_data.get("spend") == spend, (
        f"closed record must carry the full spend dict; got spend={closed_data.get('spend')!r}"
    )


# ---- account auto-switch pool helpers ---------------------------------------

def _arm_pool(stale, letter="a"):
    stale.ACCOUNT_ACTIVE.write_text(f"{letter}\n")


def _seed_farm(monkeypatch, home, letter, *, healthy=True):
    """Point `os.path.expanduser("~/.claude-<letter>")` at <home>/.claude-<letter>
    and seed a (by default healthy, i.e. MCP-bearing) .claude.json so
    `_account_config_prefix(letter)` resolves CLAUDE_CONFIG_DIR onto it."""
    monkeypatch.setenv("HOME", str(home))
    farm = home / f".claude-{letter}"
    farm.mkdir(parents=True, exist_ok=True)
    cj = {"mcpServers": {"claude-orchestrator": {}}} if healthy else {"mcpServers": {}}
    (farm / ".claude.json").write_text(json.dumps(cj))
    return farm


def _ledger_events(stale):
    if not stale.ACCOUNT_LEDGER.exists():
        return []
    return [json.loads(l) for l in stale.ACCOUNT_LEDGER.read_text().splitlines() if l]


def test_pool_account_reads_pointer(stale):
    assert stale._pool_account() is None                     # missing file
    _arm_pool(stale, "a"); assert stale._pool_account() == "a"
    stale.ACCOUNT_ACTIVE.write_text("z");  assert stale._pool_account() is None
    stale.ACCOUNT_ACTIVE.write_text("b\n"); assert stale._pool_account() == "b"
    # Whitespace-padded letters are POOL-OFF, matching spawner._pick_account /
    # spawner._active_account exactly (rstrip("\n") only): the monitor must never
    # flip a pointer the spawner reads as invalid, and vice versa.
    stale.ACCOUNT_ACTIVE.write_text(" b \n"); assert stale._pool_account() is None


def test_account_config_prefix_accepts_both_generation_mcp_keys(stale, monkeypatch, tmp_path, capsys):
    farm = _seed_farm(monkeypatch, tmp_path, "b")  # legacy claude-orchestrator key
    assert f"CLAUDE_CONFIG_DIR={farm}" in stale._account_config_prefix("b")
    (farm / ".claude.json").write_text(json.dumps({"mcpServers": {"dockwright": {}}}))
    assert f"CLAUDE_CONFIG_DIR={farm}" in stale._account_config_prefix("b")
    (farm / ".claude.json").write_text(json.dumps({"mcpServers": {"some-other-tool": {}}}))
    out = stale._account_config_prefix("b")
    assert "CLAUDE_CONFIG_DIR" not in out
    assert "CLAUDE_ORCH_ACCOUNT=a" in out


def test_account_of_prefers_record_stamp(stale):
    assert stale._account_of({"account": "b"}, "a") == "b"
    assert stale._account_of({}, "a") == "a"
    assert stale._account_of({"account": "junk"}, "a") == "a"


def test_record_brick_episodes_and_ledger(stale):
    now = 1_000_000
    stale._record_brick("a", now + 3600, "manager:mgr-A", now)
    events = _ledger_events(stale)
    assert len(events) == 1 and events[0]["event"] == "brick" and events[0]["account"] == "a"
    # refresh within the episode gap: state updates, no new ledger line
    stale._record_brick("a", now + 3600, "manager:mgr-A", now + 60)
    assert len(_ledger_events(stale)) == 1
    state = json.loads(stale.ACCOUNT_STATE.read_text())
    assert state["accounts"]["a"]["last_seen"] == now + 60
    # reset passed → new episode → second ledger line
    stale._record_brick("a", None, "worker:w1", now + 7200)
    assert len(_ledger_events(stale)) == 2
    # gap-trigger ALONE: reset still in the future, but the banner went unseen
    # longer than BRICK_EPISODE_GAP_SEC → new episode even inside the window
    stale._record_brick("b", now + 4 * 3600, "worker:w2", now)
    stale._record_brick("b", now + 4 * 3600, "worker:w2", now + 1200)
    b_events = [e for e in _ledger_events(stale) if e["account"] == "b"]
    assert len(b_events) == 2


def test_maybe_flip_guards(stale, monkeypatch):
    now = 1_000_000
    # Pool off: the pointer guard must short-circuit BEFORE any keychain probe
    # (pytest.fail raises a BaseException, so the helper's catch-all `except
    # Exception` can't swallow it into a false None).
    monkeypatch.setattr(stale, "_keychain_unlocked",
                        lambda: pytest.fail("keychain probed while pool off"))
    assert stale._maybe_flip_account("a", "r", now) is None          # pool off
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "b")
    assert stale._maybe_flip_account("a", "r", now) is None          # pointer != bricked
    _arm_pool(stale, "a")
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: False)
    assert stale._maybe_flip_account("a", "r", now) is None          # keychain locked
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    stale._record_brick("b", now + 3000, "worker:w9", now - 10)      # other bricked, in window
    assert stale._maybe_flip_account("a", "r", now) is None
    # clear b's window → flip succeeds
    stale._record_brick("b", now - 5, "worker:w9", now - 10)         # reset already passed
    assert stale._maybe_flip_account("a", "manager mgr-A limited", now) == "b"
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"
    flips = [e for e in _ledger_events(stale) if e["event"] == "flip"]
    assert flips == [{"event": "flip", "ts": now, "from": "a", "to": "b",
                      "reason": "manager mgr-A limited", "by": "stale_monitor"}]
    # cooldown: immediate flip-back blocked even though pointer==b now
    stale._record_brick("b", None, "worker:w1", now + 5)
    assert stale._maybe_flip_account("b", "r2", now + 10) is None


def test_other_bricked_unparsed_window(stale):
    now = 1_000_000
    stale._record_brick("b", None, "w", now)
    state = json.loads(stale.ACCOUNT_STATE.read_text())
    assert stale._other_account_bricked(state, "b", now + 3600) is True       # < 6h
    assert stale._other_account_bricked(state, "b", now + 7 * 3600) is False  # > 6h


def test_keychain_unlocked_probes_show_keychain_info_only(monkeypatch):
    """`_keychain_unlocked` is a single locked-state probe: rc==0 ⇒ True,
    rc!=0 ⇒ False. No per-letter item probe — `find-generic-password` must
    never run (the token machinery is gone; claude reads its own per-config-dir
    login). Fresh module load — the `stale` fixture default-denies the helper."""
    mod = _load_stale_monitor()
    calls = []

    def fake_run(rc):
        def _run(args, **kwargs):
            calls.append(args)
            assert "-w" not in args, "Python must never read a secret"
            assert "find-generic-password" not in args, "no item probe"
            return subprocess.CompletedProcess(args, returncode=rc, stdout=b"", stderr=b"")
        return _run

    monkeypatch.setattr(mod.subprocess, "run", fake_run(1))
    assert mod._keychain_unlocked() is False
    assert calls == [["security", "show-keychain-info"]], "locked ⇒ False"

    calls.clear()
    monkeypatch.setattr(mod.subprocess, "run", fake_run(0))
    assert mod._keychain_unlocked() is True
    assert calls == [["security", "show-keychain-info"]], "unlocked ⇒ True, only one probe"


# ---- account auto-switch: manager-limited flip lane + recovery launch --------

def _flips(stale):
    return [e for e in _ledger_events(stale) if e["event"] == "flip"]


def _launch_calls(calls):
    """Recovery-manager spawns, as (argv, kwargs) — argv is the driver.spawn
    argv list ([_interactive_shell(), "-ic", inner]) so launches[i][0][-1] is
    still `inner`."""
    return [(c[1]["argv"], c[1]) for c in calls if c[0] == "spawn"]


def test_limited_manager_flips_and_launches_recovery(stale, capsys, monkeypatch, tmp_path):
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _seed_farm(monkeypatch, tmp_path, "b")
    _arm_pool(stale, "a")
    t0 = 1_000_000
    _write_limited_manager(stale, "mgr1", "mgr-A", t0)
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    t_flip = clock["now"]

    stale.main(manager_name="mgr-A")
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"
    flips = _flips(stale)
    assert len(flips) == 1
    assert flips[0]["reason"] == "manager mgr-A limited"
    launches = _launch_calls(calls)
    assert len(launches) == 1
    argv = launches[0][0]
    inner = argv[-1]
    assert "/manager-takeover-recovery mgr1" in inner
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in inner, "no token injection on the login model"
    assert f"CLAUDE_CONFIG_DIR={tmp_path}/.claude-b" in inner
    assert "CLAUDE_ORCH_ACCOUNT=b" in inner
    assert "CLAUDE_AGENT=manager" in inner
    assert launches[0][1].get("route_to_manager_session") is True
    assert capsys.readouterr().out == "", "SWITCHED buffered while the manager is limited"
    emitted = json.loads(stale._emitted_state_path("mgr-A").read_text())
    assert emitted["recovery:mgr1"] == {"at": t_flip, "relaunched": False}

    # Within the takeover guard window: no second launch.
    clock["now"] = t_flip + 120
    stale.main(manager_name="mgr-A")
    assert len(_launch_calls(calls)) == 1

    # Guard expired without takeover (predecessor record still here): exactly
    # ONE relaunch — stamped with the CURRENT pointer letter, not the letter
    # captured at flip time (simulate an interim manual rollback to "a").
    _arm_pool(stale, "a")
    clock["now"] = t_flip + stale.TAKEOVER_GUARD_SEC + 120
    stale.main(manager_name="mgr-A")
    launches = _launch_calls(calls)
    assert len(launches) == 2
    relaunch_inner = launches[1][0][-1]
    assert "CLAUDE_ORCH_ACCOUNT=a" in relaunch_inner
    assert "CLAUDE_CONFIG_DIR" not in relaunch_inner, "account a rides the default login"
    events = _ledger_events(stale)
    assert len([e for e in events if e["event"] == "recovery-launch"]) == 1
    assert len([e for e in events if e["event"] == "recovery-relaunch"]) == 1

    # And never a third, however long the brick persists.
    clock["now"] = t_flip + 2 * stale.TAKEOVER_GUARD_SEC + 240
    stale.main(manager_name="mgr-A")
    assert len(_launch_calls(calls)) == 2


def test_recovery_rollup_mentions_switch(stale, capsys, monkeypatch):
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    _write_limited_manager(stale, "mgr1", "mgr-A", t0)
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    assert capsys.readouterr().out == ""

    # Takeover: the recovery session unlinks the predecessor's record. With the
    # record gone, detection no longer holds → ONE rollup mentioning the switch,
    # and the recovery guard key drops out of the emitted state naturally.
    (stale.ACTIVE / "mgr1.json").unlink()
    clock["now"] += 60
    stale.main(manager_name="mgr-A")
    out = capsys.readouterr().out
    assert "limit cleared" in out
    assert ", switched account a→b (manager mgr-A limited)" in out
    emitted = json.loads(stale._emitted_state_path("mgr-A").read_text())
    assert "recovery:mgr1" not in emitted


def test_no_flip_when_pool_off_manager_site(nudgy, capsys, monkeypatch):
    """Dormancy invariant at the manager site: no pointer ⇒ zero account-state
    writes, zero ledger lines, zero launches — and the pre-existing AUTONUDGE
    reset-time schedule still arms exactly as today."""
    calls = _capture_runs(nudgy, monkeypatch)
    t0 = 1_000_000
    _write_limited_manager(nudgy, "mgr1", "mgr-A", t0)
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main(manager_name="mgr-A")
    assert not nudgy.ACCOUNT_STATE.exists()
    assert not nudgy.ACCOUNT_LEDGER.exists()
    assert _launch_calls(calls) == []
    emitted = json.loads(nudgy._emitted_state_path("mgr-A").read_text())
    assert "scheduled:mgr1" in emitted, "nudge catch-all must stay armed with pool off"
    assert "recovery:mgr1" not in emitted


def test_manager_existing_nudge_schedule_still_arms_with_pool_on(nudgy, capsys, monkeypatch):
    """The flip lane runs ALONGSIDE the nudge schedule, not instead of it — the
    scheduled nudge stays the catch-all if recovery dies."""
    _capture_runs(nudgy, monkeypatch)
    monkeypatch.setattr(nudgy, "_keychain_unlocked", lambda: True)
    _arm_pool(nudgy, "a")
    t0 = 1_000_000
    _write_limited_manager(nudgy, "mgr1", "mgr-A", t0)
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main(manager_name="mgr-A")
    assert len(_flips(nudgy)) == 1
    emitted = json.loads(nudgy._emitted_state_path("mgr-A").read_text())
    assert "scheduled:mgr1" in emitted, "flip lane must not displace the nudge catch-all"


def test_manager_transient_throttle_no_flip_keeps_nudge(nudgy, capsys, monkeypatch):
    """Server-side 429 throttle at the manager site (pool armed): no flip, no
    brick, no recovery launch — only a transient-throttle ledger line. The
    recovery-nudge schedule still arms (the manager is still wedged; the nudge
    revives it once the throttle eases)."""
    calls = _capture_runs(nudgy, monkeypatch)
    monkeypatch.setattr(nudgy, "_keychain_unlocked", lambda: True)
    _arm_pool(nudgy, "a")
    t0 = 1_000_000
    _write_limited_manager(nudgy, "mgr1", "mgr-A", t0, text=THROTTLE_TEXT)
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main(manager_name="mgr-A")
    assert _flips(nudgy) == []
    assert nudgy.ACCOUNT_ACTIVE.read_text() == "a\n"
    assert _launch_calls(calls) == []
    events = _ledger_events(nudgy)
    assert [e for e in events if e["event"] in ("brick", "flip", "recovery-launch")] == []
    transient = [e for e in events if e["event"] == "transient-throttle"]
    assert len(transient) == 1
    assert transient[0]["source"] == "manager:mgr-A"
    emitted = json.loads(nudgy._emitted_state_path("mgr-A").read_text())
    assert "scheduled:mgr1" in emitted, "transient manager keeps the recovery-nudge catch-all"
    assert "recovery:mgr1" not in emitted, "no recovery launch for a transient throttle"


def test_manager_529_overloaded_no_flip_keeps_nudge(nudgy, capsys, monkeypatch):
    """A 529 Overloaded at the manager site (pool armed): no flip, no brick, no
    recovery launch — only a transient-throttle ledger line; the recovery-nudge
    schedule still arms (the manager is still wedged; the nudge revives it once the
    529 eases)."""
    calls = _capture_runs(nudgy, monkeypatch)
    monkeypatch.setattr(nudgy, "_keychain_unlocked", lambda: True)
    _arm_pool(nudgy, "a")
    t0 = 1_000_000
    _write_limited_manager(nudgy, "mgr1", "mgr-A", t0, text=API_529_TEXT)
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main(manager_name="mgr-A")
    assert _flips(nudgy) == []
    assert nudgy.ACCOUNT_ACTIVE.read_text() == "a\n"
    assert _launch_calls(calls) == []
    events = _ledger_events(nudgy)
    assert [e for e in events if e["event"] in ("brick", "flip", "recovery-launch")] == []
    transient = [e for e in events if e["event"] == "transient-throttle"]
    assert len(transient) == 1
    assert transient[0]["source"] == "manager:mgr-A"
    emitted = json.loads(nudgy._emitted_state_path("mgr-A").read_text())
    assert "scheduled:mgr1" in emitted, "transient manager keeps the recovery-nudge catch-all"
    assert "recovery:mgr1" not in emitted, "no recovery launch for a transient 529"


def test_unparsed_manager_banner_ledgered_once(stale, capsys, monkeypatch):
    """Banner matched the signatures but its reset clause didn't parse:
    capture-when-seen, once per distinct text per limited episode (the dedup
    key rides the emitted state while the banner keeps being seen)."""
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    _write_limited_manager(stale, "mgr1", "mgr-A", t0, text=SESSION_LIMIT_NO_RESET)  # genuine, no parseable reset
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    clock["now"] += 60
    stale.main(manager_name="mgr-A")
    unparsed = [e for e in _ledger_events(stale) if e["event"] == "unparsed-banner"]
    assert len(unparsed) == 1
    assert unparsed[0]["text"] == SESSION_LIMIT_NO_RESET
    assert unparsed[0]["source"] == "manager:mgr-A"


def test_recovery_launch_failure_still_writes_guard_key(stale, capsys, monkeypatch):
    """A failed launch (driver.spawn returns None) must not crash the scan or
    skip the guard: _launch_recovery_manager returns None, the recovery-launch
    ledger line carries window_id None, and the guard key is still written (the
    relaunch path is the retry)."""
    calls = []

    class FailingDriver:
        def send_text(self, window_id, text, submit=True):
            calls.append(("send_text", window_id, text))

        def close(self, window_id):
            calls.append(("close", window_id))

        async def spawn(self, **kw):
            calls.append(("spawn", kw))
            return None

    monkeypatch.setattr(stale, "_get_driver", lambda: FailingDriver())
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    _write_limited_manager(stale, "mgr1", "mgr-A", t0)
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    rc = stale.main(manager_name="mgr-A")
    assert rc == 0
    assert len(_launch_calls(calls)) == 1
    launch_events = [e for e in _ledger_events(stale) if e["event"] == "recovery-launch"]
    assert len(launch_events) == 1
    assert launch_events[0]["window_id"] is None
    emitted = json.loads(stale._emitted_state_path("mgr-A").read_text())
    assert emitted["recovery:mgr1"] == {"at": clock["now"], "relaunched": False}


def test_banner_clear_drops_recovery_guard_key(stale, capsys, monkeypatch):
    """Guard teardown's second leg: the banner clearing (record still present)
    also stops the block from running — the guard key drops out of the emitted
    state and the rollup prints."""
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    _write_limited_manager(stale, "mgr1", "mgr-A", t0)
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    assert capsys.readouterr().out == ""
    assert "recovery:mgr1" in json.loads(stale._emitted_state_path("mgr-A").read_text())

    log = _write_transcript(stale, "mgr1", "back to orchestrating")
    os.utime(log, (t0 + 31 * 60, t0 + 31 * 60))
    clock["now"] = t0 + 31 * 60
    stale.main(manager_name="mgr-A")
    out = capsys.readouterr().out
    assert "limit cleared" in out
    emitted = json.loads(stale._emitted_state_path("mgr-A").read_text())
    assert "recovery:mgr1" not in emitted


def test_manager_bricked_after_worker_flip_still_gets_recovery(stale, capsys, monkeypatch, tmp_path):
    """Headline ordering gap: account a bricks, a WORKER's flip moves the
    pointer a→b first, then the manager (stamped account=a) bricks a scan
    later. No new flip is possible (pointer already b) — the recovery tab must
    launch anyway, targeting the already-healthy pointer letter, with no
    SWITCHED line (the original flip emitted its own)."""
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _seed_farm(monkeypatch, tmp_path, "b")
    _arm_pool(stale, "b")
    t0 = 1_000_000
    _write_limited_manager(stale, "mgr1", "mgr-A", t0, account="a")
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    t_launch = clock["now"]

    stale.main(manager_name="mgr-A")
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n", "pointer untouched"
    assert _flips(stale) == []
    launches = _launch_calls(calls)
    assert len(launches) == 1
    inner = launches[0][0][-1]
    assert "/manager-takeover-recovery mgr1" in inner
    assert f"CLAUDE_CONFIG_DIR={tmp_path}/.claude-b" in inner
    assert "CLAUDE_ORCH_ACCOUNT=b" in inner
    assert capsys.readouterr().out == ""
    emitted = json.loads(stale._emitted_state_path("mgr-A").read_text())
    assert emitted["recovery:mgr1"] == {"at": t_launch, "relaunched": False}
    assert "switched" not in (emitted.get("limited_buffer") or {}), (
        "no SWITCHED — the worker's flip already emitted its own")
    events = _ledger_events(stale)
    assert len([e for e in events if e["event"] == "recovery-launch"]) == 1
    bricks = [e for e in events if e["event"] == "brick"]
    assert bricks and bricks[-1]["account"] == "a", "brick attributed to the REAL account"

    # Within the takeover guard window: no second launch.
    clock["now"] = t_launch + 120
    stale.main(manager_name="mgr-A")
    assert len(_launch_calls(calls)) == 1


def test_unstamped_manager_after_recent_flip_gets_recovery(stale, capsys, monkeypatch, tmp_path):
    """Day-one dead-fleet fix: every manager alive at pool activation is
    UNSTAMPED, so after a worker's flip a→b lands, the bricked manager
    resolves account == pool ("b") and the flip attempt returns None
    (cooldown). A flip that recently landed ON the current pointer means the
    manager is presumed bricked on the PRE-flip account — recovery must
    launch onto the pointer, with no new flip and no SWITCHED."""
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _seed_farm(monkeypatch, tmp_path, "b")
    _arm_pool(stale, "b")
    t0 = 1_000_000
    _write_limited_manager(stale, "mgr1", "mgr-A", t0)
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.ACCOUNT_STATE.write_text(json.dumps(
        {"accounts": {}, "last_flip": {"ts": clock["now"] - 60, "from": "a", "to": "b"}}))

    rc = stale.main(manager_name="mgr-A")
    assert rc == 0
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n", "pointer untouched"
    assert _flips(stale) == [], "no new flip — recovery rides the one that already landed"
    launches = _launch_calls(calls)
    assert len(launches) == 1
    inner = launches[0][0][-1]
    assert "/manager-takeover-recovery mgr1" in inner
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in inner, "no token injection on the login model"
    assert f"CLAUDE_CONFIG_DIR={tmp_path}/.claude-b" in inner
    assert "CLAUDE_ORCH_ACCOUNT=b" in inner
    launch_events = [e for e in _ledger_events(stale) if e["event"] == "recovery-launch"]
    assert len(launch_events) == 1
    emitted = json.loads(stale._emitted_state_path("mgr-A").read_text())
    assert emitted["recovery:mgr1"] == {"at": clock["now"], "relaunched": False}
    assert "switched" not in (emitted.get("limited_buffer") or {}), (
        "no SWITCHED — the flip that landed earlier emitted its own")


def test_ledger_recovery_launches_counting(stale):
    now = 1_000_000
    stale._append_account_ledger({"ts": now - 100, "event": "recovery-launch", "from_sid": "m1"})
    stale._append_account_ledger({"ts": now - 50, "event": "recovery-relaunch", "from_sid": "m1"})
    stale._append_account_ledger({"ts": now - 50, "event": "recovery-launch", "from_sid": "other"})
    stale._append_account_ledger({"ts": now - stale.MAX_PLAUSIBLE_RESET_SEC - 1,
                                  "event": "recovery-launch", "from_sid": "m1"})  # outside window
    stale._append_account_ledger({"ts": now - 30, "event": "flip", "from": "a", "to": "b"})
    assert stale._ledger_recovery_launches("m1", now) == 2
    assert stale._ledger_recovery_launches("other", now) == 1
    assert stale._ledger_recovery_launches("ghost", now) == 0


def test_ledger_recovery_launches_fail_open(stale):
    assert stale._ledger_recovery_launches("m1", 1_000_000) == 0, "no ledger file ⇒ 0"
    stale.ACCOUNT_LEDGER.write_text("not json\n{broken\n[]\n")
    assert stale._ledger_recovery_launches("m1", 1_000_000) == 0, "garbage lines ⇒ 0"


def test_launch_bound_holds_with_dead_emitted_state(stale, capsys, monkeypatch):
    """m3: the once+once launch bound must survive persistent emitted-state
    write failure (disk full) — without the ledger backstop, the guard key
    never persists and every 60s scan would open a fresh recovery tab.
    Only the emitted-state write fails; account-state writes ride through
    (the flip lane keeps its cooldown/last_flip bookkeeping)."""
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    _write_limited_manager(stale, "mgr1", "mgr-A", t0)
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])

    emitted_path = stale._emitted_state_path("mgr-A")
    real_write = stale._write_json_atomic

    def selective_write(path, data):
        if path == emitted_path:
            raise OSError(28, "No space left on device")
        real_write(path, data)

    monkeypatch.setattr(stale, "_write_json_atomic", selective_write)

    for minute in (0, 1, 2):
        clock["now"] = t0 + 30 * 60 + minute * 60
        rc = stale.main(manager_name="mgr-A")
        assert rc == 0
    assert len(_launch_calls(calls)) == 1, "ledger backstop caps the storm at ONE launch"

    # Past the takeover guard: at most one relaunch, never a per-scan storm.
    clock["now"] = t0 + 30 * 60 + stale.TAKEOVER_GUARD_SEC + 120
    stale.main(manager_name="mgr-A")
    clock["now"] += 60
    stale.main(manager_name="mgr-A")
    assert len(_launch_calls(calls)) <= 2


def test_recovery_launch_gated_on_keychain_even_when_already_flipped(stale, capsys, monkeypatch):
    """already_flipped (here via the recent-flip heuristic; the fixture's
    default-deny _keychain_unlocked plays the locked keychain) ⇒
    NO recovery launch and no crash — a tab spawned against a locked keychain
    freezes pre-claude on the SecurityAgent dialog. The guard key stays
    unwritten, so the launch retries the moment the keychain is usable."""
    calls = _capture_runs(stale, monkeypatch)
    _arm_pool(stale, "b")
    t0 = 1_000_000
    _write_limited_manager(stale, "mgr1", "mgr-A", t0)
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.ACCOUNT_STATE.write_text(json.dumps(
        {"accounts": {}, "last_flip": {"ts": clock["now"] - 60, "from": "a", "to": "b"}}))

    rc = stale.main(manager_name="mgr-A")
    assert rc == 0
    assert _launch_calls(calls) == []
    emitted = json.loads(stale._emitted_state_path("mgr-A").read_text())
    assert "recovery:mgr1" not in emitted

    # Keychain unlocks → the deferred launch fires on the next scan.
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    clock["now"] += 60
    stale.main(manager_name="mgr-A")
    assert len(_launch_calls(calls)) == 1


def test_relaunch_gated_on_keychain(stale, capsys, monkeypatch):
    """The relaunch branch is keychain-gated too: a locked keychain at
    guard-expiry defers the relaunch (relaunched stays False) instead of
    burning the once-only retry on a tab that would freeze pre-claude."""
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    _write_limited_manager(stale, "mgr1", "mgr-A", t0)
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    t_flip = clock["now"]
    stale.main(manager_name="mgr-A")
    assert len(_launch_calls(calls)) == 1

    # Guard expired but keychain now locked ⇒ no relaunch, retry stays armed.
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: False)
    clock["now"] = t_flip + stale.TAKEOVER_GUARD_SEC + 120
    stale.main(manager_name="mgr-A")
    assert len(_launch_calls(calls)) == 1
    emitted = json.loads(stale._emitted_state_path("mgr-A").read_text())
    assert emitted["recovery:mgr1"]["relaunched"] is False

    # Keychain unlocks → the single relaunch fires.
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    clock["now"] += 60
    stale.main(manager_name="mgr-A")
    assert len(_launch_calls(calls)) == 2
    emitted = json.loads(stale._emitted_state_path("mgr-A").read_text())
    assert emitted["recovery:mgr1"]["relaunched"] is True


def test_unstamped_manager_stale_flip_no_recovery(stale, capsys, monkeypatch):
    """Heuristic staleness cutoff: a last_flip older than
    MAX_PLAUSIBLE_RESET_SEC proves nothing about why the manager bricked —
    no recovery launch. (Guard 4 blocks the flip here: the other account is
    inside its own brick window, the both-limited shape.)"""
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "b")
    t0 = 1_000_000
    _write_limited_manager(stale, "mgr1", "mgr-A", t0)
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.ACCOUNT_STATE.write_text(json.dumps({
        "accounts": {"a": {"bricked_at": clock["now"] - 100, "last_seen": clock["now"] - 100}},
        "last_flip": {"ts": clock["now"] - stale.MAX_PLAUSIBLE_RESET_SEC - 60,
                      "from": "a", "to": "b"},
    }))

    rc = stale.main(manager_name="mgr-A")
    assert rc == 0
    assert _flips(stale) == []
    assert _launch_calls(calls) == []
    emitted = json.loads(stale._emitted_state_path("mgr-A").read_text())
    assert "recovery:mgr1" not in emitted


def test_unstamped_manager_no_flip_history_no_recovery(stale, capsys, monkeypatch):
    """No last_flip at all: an unstamped manager bricked on the pointer with
    the flip blocked (other account bricked-in-window, guard 4) gets NO
    recovery tab — both-limited rides the AUTONUDGE lane, unchanged."""
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    _write_limited_manager(stale, "mgr1", "mgr-A", t0)
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale._record_brick("b", clock["now"] + 3000, "worker:w9", clock["now"] - 10)

    rc = stale.main(manager_name="mgr-A")
    assert rc == 0
    assert stale.ACCOUNT_ACTIVE.read_text() == "a\n"
    assert _flips(stale) == []
    assert _launch_calls(calls) == []
    emitted = json.loads(stale._emitted_state_path("mgr-A").read_text())
    assert "recovery:mgr1" not in emitted


# ---- account auto-switch: worker-site flip with a hoisted banner read --------


def test_worker_banner_flips_and_emits_switched_live(stale, capsys, monkeypatch):
    """Healthy manager + bricked worker on the pointer account ⇒ flip + LIVE
    SWITCHED line (the manager's wake-up). AUTONUDGE OFF — flips are decoupled."""
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    # real clock: SESSION_LIMIT_TEXT's reset clause may or may not parse
    # depending on wall time; no assertion here may depend on reset_ts/ledger
    # reset values — use a fake clock if you ever need one.
    now = int(time.time())
    path = _write_record(stale, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    os.utime(path, (now - 360, now - 360))
    log = _write_transcript(stale, "w1", SESSION_LIMIT_TEXT)
    os.utime(log, (now - 360, now - 360))
    stale.main()
    out = capsys.readouterr().out
    assert "SWITCHED account a→b (worker worker-tab limited)" in out
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"
    assert "NUDGED" not in out        # autonudge off — nudge lane untouched


def test_worker_banner_past_processing_threshold_still_flips(stale, capsys, monkeypatch):
    """45min of silence is past PROCESSING_THRESHOLD_SEC, where the 5-min elif
    is unreachable — the hoisted banner read must still see the banner and
    flip. AUTONUDGE off ⇒ the ladder's STALE_PROCESSING page prints exactly as
    today, alongside the flip."""
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    # real clock: SESSION_LIMIT_TEXT's reset clause may or may not parse
    # depending on wall time; no assertion here may depend on reset_ts/ledger
    # reset values — use a fake clock if you ever need one.
    now = int(time.time())
    path = _write_record(stale, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    os.utime(path, (now - 2700, now - 2700))
    log = _write_transcript(stale, "w1", SESSION_LIMIT_TEXT)
    os.utime(log, (now - 2700, now - 2700))
    stale.main()
    out = capsys.readouterr().out
    assert "SWITCHED account a→b (worker worker-tab limited)" in out
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"
    assert "STALE_PROCESSING worker-tab (45min)" in out


def test_worker_banner_while_schedule_armed_still_flips(nudgy, capsys, monkeypatch):
    """While a banner-scheduled nudge is armed the 5-min lane is suppressed —
    the hoisted read must still see the banner and flip, leaving the armed
    scheduled:<sid> carried untouched (not consumed, not duplicated)."""
    _capture_runs(nudgy, monkeypatch)
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    log = _write_transcript(nudgy, "w1", SESSION_LIMIT_TEXT)
    t0 = 1_000_000
    os.utime(path, (t0, t0))
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 6 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main()  # pool OFF: 5-min lane nudges + arms scheduled:w1, no flip
    assert "NUDGED worker-tab (6min rate-limited)" in capsys.readouterr().out
    sched = json.loads(nudgy.EMITTED_STATE.read_text())["scheduled:w1"]
    assert _flips(nudgy) == []

    monkeypatch.setattr(nudgy, "_keychain_unlocked", lambda: True)
    _arm_pool(nudgy, "a")
    clock["now"] = t0 + 8 * 60  # inside the schedule window, still bannered
    nudgy.main()
    out = capsys.readouterr().out
    assert "SWITCHED account a→b (worker worker-tab limited)" in out
    assert nudgy.ACCOUNT_ACTIVE.read_text() == "b\n"
    assert len(_flips(nudgy)) == 1
    assert "NUDGED" not in out, "the suppressed 5-min lane must stay suppressed"
    assert json.loads(nudgy.EMITTED_STATE.read_text())["scheduled:w1"] == sched, (
        "the armed schedule is carried, not consumed or duplicated"
    )


def test_worker_lane_pool_off_no_account_writes(stale, capsys, monkeypatch):
    """Dormancy invariant at the worker site: no pointer ⇒ zero account-state
    writes, zero ledger lines, no SWITCHED — the lane behaves exactly as with
    no pool at all."""
    _capture_runs(stale, monkeypatch)
    now = int(time.time())
    path = _write_record(stale, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    os.utime(path, (now - 2700, now - 2700))
    log = _write_transcript(stale, "w1", SESSION_LIMIT_TEXT)
    os.utime(log, (now - 2700, now - 2700))
    stale.main()
    out = capsys.readouterr().out
    assert "STALE_PROCESSING worker-tab (45min)" in out
    assert "SWITCHED" not in out
    assert not stale.ACCOUNT_STATE.exists()
    assert not stale.ACCOUNT_LEDGER.exists()


def test_same_scan_cascade_single_flip(stale, capsys, monkeypatch):
    """Limited manager + two bricked workers in ONE scoped scan ⇒ exactly one
    flip. With `pool` hoisted once per scan, the workers resolve their
    bricked-account to the PRE-flip letter "a" while the pointer is already
    "b", so guard 1 (pointer == bricked) blocks the flip-back; cooldown +
    other-account-bricked are defense-in-depth behind it."""
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    _write_limited_manager(stale, "mgr1", "mgr-A", t0)
    for sid in ("w1", "w2"):
        w = _write_record(stale, sid, agent="worker", state="processing",
                          window_id="42", parent_manager_name="mgr-A")
        os.utime(w, (t0, t0))
        wlog = _write_transcript(stale, sid, SESSION_LIMIT_TEXT)
        os.utime(wlog, (t0, t0))
    clock = {"now": t0 + 6 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"
    assert len(_flips(stale)) == 1
    # Pins the hoist mechanism specifically: with a per-record pool re-read
    # the workers would resolve "b" post-flip and record bricks against it;
    # with the hoist they re-record against the pre-flip "a" only.
    assert "b" not in json.loads(stale.ACCOUNT_STATE.read_text())["accounts"]


def test_worker_unstamped_record_uses_pointer_stamped_uses_stamp(stale, capsys, monkeypatch):
    """Bricked-account resolution: a record stamped account="b" while the
    pointer is "a" must NOT flip (guard 1: pointer != bricked account); an
    unstamped record resolves to the pointer letter and flips."""
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    # fake clock on purpose: scan 2 depends on the brick window recorded for
    # "b" having expired, and SESSION_LIMIT_TEXT's reset clause parses
    # deterministically only on a pinned clock (on the real clock it may or
    # may not parse depending on wall time).
    t0 = 1_000_000
    clock = {"now": t0 + 6 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stamped = _write_record(stale, "w1", agent="worker", state="processing",
                            name="worker-tab", window_id="42", account="b")
    os.utime(stamped, (t0, t0))
    log = _write_transcript(stale, "w1", SESSION_LIMIT_TEXT)
    os.utime(log, (t0, t0))
    stale.main()
    assert _flips(stale) == []
    assert stale.ACCOUNT_ACTIVE.read_text() == "a\n"

    # The stamped worker closes; its recorded brick window on "b" expires. A
    # second, unstamped record resolves to the pointer letter "a" and flips.
    stamped.unlink()
    t1 = t0 + 7 * 3600  # past MAX_PLAUSIBLE_RESET_SEC / any recorded reset_ts
    plain = _write_record(stale, "w2", agent="worker", state="processing",
                          name="worker-tab2", window_id="43")
    os.utime(plain, (t1, t1))
    log2 = _write_transcript(stale, "w2", SESSION_LIMIT_TEXT)
    os.utime(log2, (t1, t1))
    clock["now"] = t1 + 6 * 60
    stale.main()
    flips = _flips(stale)
    assert len(flips) == 1 and flips[0]["from"] == "a" and flips[0]["to"] == "b"
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"


def test_autoclose_skips_worker_with_live_delegation(stale, monkeypatch):
    """A worker past the idle TTL must NOT be reaped when its background subagent
    is still writing: newest agent-*.jsonl mtime is fresher than the main log
    (growth predicate) AND within IDLE_THRESHOLD_SEC of now (freshness cap)."""
    now = int(time.time())
    record_path = _write_record(
        stale, "s1",
        pid=12345,
        iterm_sid="7",
        last_turn_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 200)),
    )
    # Main log written long ago — subagent is newer (background delegation).
    log = _write_transcript(stale, "s1", "doing stuff")
    os.utime(log, (now - 10_000, now - 10_000))
    # Subagent file: newer than main log AND fresh (< IDLE_THRESHOLD_SEC=100).
    subagents_dir = stale.CLAUDE_PROJECTS / "proj" / "s1" / "subagents"
    subagents_dir.mkdir(parents=True)
    agent_file = subagents_dir / "agent-0.jsonl"
    agent_file.write_text("{}\n")
    os.utime(agent_file, (now - 60, now - 60))

    monkeypatch.setattr(stale.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(stale.os, "kill", lambda *a, **k: None)

    stale.main()
    assert record_path.exists(), "worker with live delegation must not be reaped"


def test_autoclose_reaps_worker_with_silent_delegation(stale, monkeypatch):
    """A worker past the idle TTL MUST be reaped when the background subagent
    has gone silent: newest agent-*.jsonl mtime is older than IDLE_THRESHOLD_SEC."""
    now = int(time.time())
    record_path = _write_record(
        stale, "s2",
        pid=12345,
        iterm_sid="7",
        last_turn_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 200)),
    )
    log = _write_transcript(stale, "s2", "doing stuff")
    os.utime(log, (now - 10_000, now - 10_000))
    # Subagent file: newer than main log BUT stale (> IDLE_THRESHOLD_SEC=100).
    subagents_dir = stale.CLAUDE_PROJECTS / "proj" / "s2" / "subagents"
    subagents_dir.mkdir(parents=True)
    agent_file = subagents_dir / "agent-0.jsonl"
    agent_file.write_text("{}\n")
    os.utime(agent_file, (now - 200, now - 200))

    monkeypatch.setattr(stale.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(stale.os, "kill", lambda *a, **k: None)

    stale.main()
    assert not record_path.exists(), "worker with hung (silent) delegation must be reaped"


def test_autoclose_reaps_worker_with_consumed_foreground_agent(stale, monkeypatch):
    """A worker past the idle TTL MUST be reaped when the subagent JSONL is older
    than the main log: the subagent was consumed in-turn (foreground), not running
    as a background delegation."""
    now = int(time.time())
    record_path = _write_record(
        stale, "s3",
        pid=12345,
        iterm_sid="7",
        last_turn_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 200)),
    )
    # Main log is RECENT — newer than the subagent file.
    log = _write_transcript(stale, "s3", "doing stuff")
    os.utime(log, (now - 50, now - 50))
    # Subagent file: OLDER than main log → growth predicate fails.
    subagents_dir = stale.CLAUDE_PROJECTS / "proj" / "s3" / "subagents"
    subagents_dir.mkdir(parents=True)
    agent_file = subagents_dir / "agent-0.jsonl"
    agent_file.write_text("{}\n")
    os.utime(agent_file, (now - 200, now - 200))

    monkeypatch.setattr(stale.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(stale.os, "kill", lambda *a, **k: None)

    stale.main()
    assert not record_path.exists(), "worker with foreground-consumed agent must be reaped"


def test_worker_transient_throttle_nudges_but_never_flips(nudgy, capsys, monkeypatch):
    """Server-side 429 throttle at the worker site (pool armed, autonudge on): the
    nudge lane still revives it, but the flip lane recognizes it as transient — no
    brick, no flip, only a dedup'd transient-throttle observability line; the
    account pointer is untouched."""
    _capture_runs(nudgy, monkeypatch)
    monkeypatch.setattr(nudgy, "_keychain_unlocked", lambda: True)
    _arm_pool(nudgy, "a")
    t0 = 1_000_000
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    os.utime(path, (t0, t0))
    log = _write_transcript(nudgy, "w1", THROTTLE_TEXT)
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 6 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main()
    out = capsys.readouterr().out
    assert "NUDGED worker-tab (6min rate-limited)" in out
    assert "SWITCHED" not in out
    assert _flips(nudgy) == []
    assert nudgy.ACCOUNT_ACTIVE.read_text() == "a\n"
    events = _ledger_events(nudgy)
    assert [e for e in events if e["event"] in ("brick", "flip")] == []
    assert [e for e in events if e["event"] == "unparsed-banner"] == []
    transient = [e for e in events if e["event"] == "transient-throttle"]
    assert len(transient) == 1
    assert transient[0]["text"] == THROTTLE_TEXT
    assert transient[0]["source"] == "worker:worker-tab"


def test_worker_529_overloaded_nudges_but_never_flips(nudgy, capsys, monkeypatch):
    """A worker wedged on a transient 529 Overloaded (pool armed, autonudge on):
    the 5-min fast lane revives it (NUDGED at 6min), but the flip lane recognizes
    it as a transient server error — no brick, no flip, only a transient-throttle
    observability line; the account pointer is untouched. This is the whole point:
    529 drives nudge recovery but NEVER a brick/flip."""
    _capture_runs(nudgy, monkeypatch)
    monkeypatch.setattr(nudgy, "_keychain_unlocked", lambda: True)
    _arm_pool(nudgy, "a")
    t0 = 1_000_000
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    os.utime(path, (t0, t0))
    log = _write_transcript(nudgy, "w1", API_529_TEXT)
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 6 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main()
    out = capsys.readouterr().out
    assert "NUDGED worker-tab (6min rate-limited)" in out
    assert "SWITCHED" not in out
    assert _flips(nudgy) == []
    assert nudgy.ACCOUNT_ACTIVE.read_text() == "a\n"
    events = _ledger_events(nudgy)
    assert [e for e in events if e["event"] in ("brick", "flip")] == []
    assert [e for e in events if e["event"] == "unparsed-banner"] == []
    transient = [e for e in events if e["event"] == "transient-throttle"]
    assert len(transient) == 1
    assert transient[0]["text"] == API_529_TEXT
    assert transient[0]["source"] == "worker:worker-tab"


def test_worker_unparsed_banner_ledgered(stale, capsys, monkeypatch):
    """A worker banner matching the signatures but with no parseable reset
    clause is ledgered once per distinct text (worker-site source tag), and
    the flip is still attempted — a parse failure never blocks the lane."""
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    path = _write_record(stale, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    os.utime(path, (t0, t0))
    log = _write_transcript(stale, "w1", SESSION_LIMIT_NO_RESET)  # genuine limit, no parseable reset
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 6 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main()
    clock["now"] += 60
    stale.main()
    unparsed = [e for e in _ledger_events(stale) if e["event"] == "unparsed-banner"]
    assert len(unparsed) == 1
    assert unparsed[0]["text"] == SESSION_LIMIT_NO_RESET
    assert unparsed[0]["source"] == "worker:worker-tab"
    assert len(_flips(stale)) == 1, "unparsable reset clause must not block the flip"


# ---- auth-401 self-heal (detection + bounded same-account kill+resume) --------
# CC persists an auth-401 as an assistant event with TOP-LEVEL isApiErrorMessage,
# apiErrorStatus, and error fields — identical in TUI and headless transcripts
# (spike-confirmed against the real 2026-06-14 10:48Z incident transcript). The
# human text drifts ("Invalid authentication credentials" on a server reject vs
# "Invalid bearer token" on a malformed token), so detection keys on the STABLE
# apiErrorStatus==401, with the text as a drift-proof fallback. A rate-limit
# banner is also an isApiErrorMessage message but carries NO 401 status/text, so
# the two signature classes never overlap. The event uuid is the attempt key: a
# fresh uuid is a fresh 401 (a resume that 401'd again); the same uuid still
# showing means the resume hasn't happened/cleared yet.

AUTH_401_INCIDENT_TEXT = "Please run /login · API Error: 401 Invalid authentication credentials"
AUTH_401_BEARER_TEXT = "Failed to authenticate. API Error: 401 Invalid bearer token"


def _auth_401_event(text=AUTH_401_INCIDENT_TEXT, uuid="u-1", api_error_status=401,
                    is_api_error=True):
    event = {
        "type": "assistant",
        "isApiErrorMessage": is_api_error,
        "error": "authentication_failed",
        "uuid": uuid,
        "message": {"content": [{"type": "text", "text": text}]},
    }
    if api_error_status is not None:
        event["apiErrorStatus"] = api_error_status
    return event


def _auth_401_line(**kwargs):
    return json.dumps(_auth_401_event(**kwargs))


def _write_auth_401_transcript(stale, sid, **kwargs):
    project_dir = stale.CLAUDE_PROJECTS / "proj"
    project_dir.mkdir(parents=True, exist_ok=True)
    log = project_dir / f"{sid}.jsonl"
    log.write_text(_auth_401_line(**kwargs) + "\n")
    return log


def _auth_events(stale):
    return [e for e in _ledger_events(stale) if e["event"] == "auth-401"]


# -- detection (pure + IO) -----------------------------------------------------

def test_is_auth_401_event_matches_stable_signal_and_text_fallback(stale):
    # Real incident form (structured signal + text both present).
    assert stale._is_auth_401_event(_auth_401_event(text=AUTH_401_INCIDENT_TEXT)) is True
    # Malformed-token form: different human text, same structured signal.
    assert stale._is_auth_401_event(_auth_401_event(text=AUTH_401_BEARER_TEXT)) is True
    # Structured signal alone — apiErrorStatus==401 with unrecognized phrasing.
    assert stale._is_auth_401_event(
        _auth_401_event(text="totally new wording", uuid="u")) is True
    # apiErrorStatus serialized as a string still reads as the structured signal.
    assert stale._is_auth_401_event(
        _auth_401_event(text="totally new wording", uuid="u", api_error_status="401")) is True
    # Text fallback alone — a build that omits apiErrorStatus but writes the phrase.
    assert stale._is_auth_401_event(
        _auth_401_event(text=AUTH_401_INCIDENT_TEXT, api_error_status=None)) is True
    assert stale._is_auth_401_event(
        _auth_401_event(text="Please run /login now", api_error_status=None)) is True


def test_is_auth_401_event_negative_cases(stale):
    # Normal assistant turn.
    assert stale._is_auth_401_event(
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": "All done, opening the PR"}]}}) is False
    # Rate-limit banner IS an api-error message but is NOT a 401 — must not match
    # (keeps the auth class disjoint from RATE_LIMIT_SIGNATURES).
    assert stale._is_auth_401_event(
        {"type": "assistant", "isApiErrorMessage": True, "apiErrorStatus": 429,
         "message": {"content": [{"type": "text", "text": SESSION_LIMIT_TEXT}]}}) is False
    # A non-401 api error with no 401 text.
    assert stale._is_auth_401_event(
        _auth_401_event(text="API Error: 500 server error", api_error_status=500, uuid="u")) is False
    # isApiErrorMessage gate closed: prose mentioning "API Error: 401" must NOT match.
    assert stale._is_auth_401_event(
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": "discussing API Error: 401 in passing"}]}}) is False
    # Malformed / wrong-type shapes never crash, never match.
    assert stale._is_auth_401_event(None) is False
    assert stale._is_auth_401_event({"type": "user"}) is False
    assert stale._is_auth_401_event([]) is False


def test_auth_failure_signature_reads_last_event(stale, tmp_path):
    log = tmp_path / "t.jsonl"
    log.write_text(_auth_401_line(uuid="u-42") + "\n")
    sig = stale._auth_failure_signature(log)
    assert sig is not None
    uuid, text = sig
    assert uuid == "u-42"
    assert text == AUTH_401_INCIDENT_TEXT
    # A normal final turn ⇒ None.
    log.write_text(_assistant_line("All done, opening the PR") + "\n")
    assert stale._auth_failure_signature(log) is None
    # Missing file ⇒ None (crash-proof).
    assert stale._auth_failure_signature(tmp_path / "absent.jsonl") is None


def test_auth_failure_signature_only_last_assistant_matters(stale, tmp_path):
    # A 401 earlier, then a successful recovery turn ⇒ no auth failure NOW.
    log = tmp_path / "t.jsonl"
    log.write_text("\n".join([_auth_401_line(uuid="u-1"),
                              _assistant_line("recovered, continuing")]) + "\n")
    assert stale._auth_failure_signature(log) is None


def test_auth_failure_signature_never_raises(stale, tmp_path, monkeypatch):
    log = tmp_path / "t.jsonl"
    log.write_text(_auth_401_line() + "\n")

    def boom(log_path, max_bytes=65536):
        raise RuntimeError("poison transcript")

    monkeypatch.setattr(stale, "_last_assistant_event", boom)
    assert stale._auth_failure_signature(log) is None


# -- bounded per-account attempt counter ---------------------------------------

def test_record_auth_401_bounded_attempts_and_dedup(stale):
    now = 1_000_000
    assert stale._record_auth_401("a", "u-1", now) == "recover"
    # Same uuid (the resume hasn't fired yet) ⇒ duplicate, no count bump.
    assert stale._record_auth_401("a", "u-1", now + 30) == "duplicate"
    # Fresh uuid within the window ⇒ attempt 2, still <= N.
    assert stale._record_auth_401("a", "u-2", now + 60) == "recover"
    # Fresh uuid within the window ⇒ attempt 3 > N ⇒ escalate.
    assert stale._record_auth_401("a", "u-3", now + 120) == "escalate"


def test_record_auth_401_window_resets(stale):
    now = 1_000_000
    assert stale._record_auth_401("a", "u-1", now) == "recover"
    last_seen = now + 60
    assert stale._record_auth_401("a", "u-2", last_seen) == "recover"
    # Beyond the M-window since the LAST sighting ⇒ a fresh incident; counter resets.
    later = last_seen + stale.AUTH_401_WINDOW_SEC + 1
    assert stale._record_auth_401("a", "u-3", later) == "recover"
    assert stale._record_auth_401("a", "u-4", later + 60) == "recover"


def test_record_auth_401_per_account_independent(stale):
    now = 1_000_000
    assert stale._record_auth_401("a", "u-1", now) == "recover"
    assert stale._record_auth_401("b", "u-2", now) == "recover"
    assert stale._record_auth_401("a", "u-3", now + 10) == "recover"
    assert stale._record_auth_401("a", "u-4", now + 20) == "escalate"   # a's 3rd
    assert stale._record_auth_401("b", "u-5", now + 20) == "recover"    # b's 2nd


# -- worker path ---------------------------------------------------------------

def test_worker_auth_401_recovers_same_account_no_flip(stale, capsys, monkeypatch):
    """The first response to a worker auth-401 is a SAME-account kill+resume
    trigger (AUTH_401 event for the manager's duty), NOT a flip: a transient
    server-side 401 hits both accounts equally, so flipping is wrong and would
    burn the flip cooldown."""
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    path = _write_record(stale, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    os.utime(path, (t0, t0))
    log = _write_auth_401_transcript(stale, "w1", uuid="u-1")
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 6 * 60}   # past the 5-min pool-lane floor
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main()
    out = capsys.readouterr().out
    assert "AUTH_401 worker-tab" in out
    assert "SWITCHED" not in out and "AUTH_401_ESCALATED" not in out
    # NO flip: pointer unchanged, no flip ledger line.
    assert stale.ACCOUNT_ACTIVE.read_text() == "a\n"
    assert _flips(stale) == []
    auth = _auth_events(stale)
    assert len(auth) == 1
    assert auth[0]["action"] == "recover" and auth[0]["account"] == "a"
    assert auth[0]["source"] == "worker:worker-tab"


def test_worker_auth_401_does_not_re_emit_on_same_401(stale, capsys, monkeypatch):
    """The same 401 (same uuid) still showing on the next scan is a duplicate —
    don't re-page the manager or inflate the attempt count."""
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    path = _write_record(stale, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    log = _write_auth_401_transcript(stale, "w1", uuid="u-1")
    clock = {"now": t0}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    for minute in (6, 7, 8):
        clock["now"] = t0 + minute * 60
        os.utime(path, (clock["now"] - 6 * 60, clock["now"] - 6 * 60))
        os.utime(log, (clock["now"] - 6 * 60, clock["now"] - 6 * 60))
        stale.main()
    # Three scans, the SAME 401 throughout, all within the re-emit cadence ⇒
    # exactly one recover action.
    assert len(_auth_events(stale)) == 1
    assert _flips(stale) == []


def test_worker_auth_401_reemits_trigger_after_cadence(stale, capsys, monkeypatch):
    """The AUTH_401 trigger re-fires while the worker stays 401'd (same uuid) so
    a missed or coalesced-then-recovered event reaches a live manager — but the
    uuid-deduped attempt count does NOT inflate (no spurious escalation)."""
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    path = _write_record(stale, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    log = _write_auth_401_transcript(stale, "w1", uuid="u-1")
    clock = {"now": t0}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])

    def scan(minute):
        clock["now"] = t0 + minute * 60
        os.utime(log, (clock["now"] - 6 * 60, clock["now"] - 6 * 60))
        os.utime(path, (clock["now"] - 6 * 60, clock["now"] - 6 * 60))
        stale.main()
        return capsys.readouterr().out

    assert "AUTH_401 worker-tab" in scan(6)             # first trigger
    assert "AUTH_401" not in scan(7)                    # within cadence → no re-emit
    assert "AUTH_401 worker-tab" in scan(6 + 6)         # past the cadence, same uuid → re-emit
    # Same 401 throughout: exactly ONE attempt recorded, never escalates.
    assert [e["action"] for e in _auth_events(stale)] == ["recover"]
    assert _flips(stale) == []


def test_worker_auth_401_escalates_after_bound(stale, capsys, monkeypatch):
    """After N=2 failed SAME-account resume attempts within M minutes the login
    is suspect: escalate — flip to the other account (existing SWITCHED ⇒ the
    manager's new-account kill+resume duty) and PAGE the human to /login."""
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    path = _write_record(stale, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    clock = {"now": t0}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])

    def scan_401(uuid, minute):
        clock["now"] = t0 + minute * 60
        log = _write_auth_401_transcript(stale, "w1", uuid=uuid)   # fresh 401 = a resume that 401'd again
        os.utime(log, (clock["now"] - 6 * 60, clock["now"] - 6 * 60))
        os.utime(path, (clock["now"] - 6 * 60, clock["now"] - 6 * 60))
        stale.main()
        return capsys.readouterr().out

    out1 = scan_401("u-1", 6)
    assert "AUTH_401 worker-tab" in out1 and "SWITCHED" not in out1
    out2 = scan_401("u-2", 8)
    assert "AUTH_401 worker-tab" in out2 and "SWITCHED" not in out2
    out3 = scan_401("u-3", 10)
    assert "SWITCHED account a→b" in out3
    assert "AUTH_401_ESCALATED" in out3
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"
    assert len(_flips(stale)) == 1
    actions = [e["action"] for e in _auth_events(stale)]
    assert actions == ["recover", "recover", "escalate"]


def test_worker_auth_401_dormant_when_pool_off(stale, capsys, monkeypatch):
    """Dormancy invariant: no pointer ⇒ zero account-state writes, zero ledger
    lines, no flip — the auth-401 self-heal is gated on the pool like the
    rate-limit flip lane."""
    _capture_runs(stale, monkeypatch)
    t0 = 1_000_000
    path = _write_record(stale, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    os.utime(path, (t0, t0))
    log = _write_auth_401_transcript(stale, "w1", uuid="u-1")
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 6 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main()
    out = capsys.readouterr().out
    assert "AUTH_401" not in out
    assert not stale.ACCOUNT_STATE.exists()
    assert not stale.ACCOUNT_LEDGER.exists()


# -- manager path --------------------------------------------------------------

def _write_auth_401_manager(stale, sid, manager_name, t0, window_id="9", uuid="u-1",
                            **overrides):
    path = _write_record(stale, sid, agent="manager", state="processing",
                         name=manager_name, window_id=window_id,
                         parent_manager_name=None, **overrides)
    os.utime(path, (t0, t0))
    log = _write_auth_401_transcript(stale, sid, uuid=uuid)
    os.utime(log, (t0, t0))
    return path, log


def test_manager_auth_401_recovers_same_account_no_flip(stale, capsys, monkeypatch):
    """A 401'd manager is deaf — the monitor launches a SAME-account takeover
    (a fresh process re-reads the keychain login). No flip: the other account's
    login is equally exposed to a server blip."""
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    _write_auth_401_manager(stale, "mgr1", "mgr-A", t0, uuid="u-1")
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    # SAME account: pointer unchanged, no flip.
    assert stale.ACCOUNT_ACTIVE.read_text() == "a\n"
    assert _flips(stale) == []
    launches = _launch_calls(calls)
    assert len(launches) == 1
    inner = launches[0][0][-1]
    assert "/manager-takeover-recovery mgr1" in inner
    assert "CLAUDE_ORCH_ACCOUNT=a" in inner   # SAME account, not flipped to b
    auth = _auth_events(stale)
    assert len(auth) == 1 and auth[0]["action"] == "recover"
    launch_events = [e for e in _ledger_events(stale) if e["event"] == "recovery-launch"]
    assert len(launch_events) == 1 and launch_events[0]["from_sid"] == "mgr1"
    # The recovery guard suppresses a second launch for the SAME sid next scan.
    clock["now"] += 60
    stale.main(manager_name="mgr-A")
    assert len(_launch_calls(calls)) == 1


def test_manager_auth_401_escalates_after_bound(stale, capsys, monkeypatch):
    """When the same-account takeover keeps 401'ing (credential genuinely
    suspect), escalation STOPS launching dead takeover tabs and instead flips +
    pages."""
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    path, log = _write_auth_401_manager(stale, "mgr1", "mgr-A", t0, uuid="u-1")
    clock = {"now": t0}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])

    def scan_401(uuid, minute):
        clock["now"] = t0 + minute * 60
        log.write_text(_auth_401_line(uuid=uuid) + "\n")
        os.utime(log, (clock["now"] - 20 * 60, clock["now"] - 20 * 60))
        os.utime(path, (clock["now"] - 20 * 60, clock["now"] - 20 * 60))
        stale.main(manager_name="mgr-A")

    scan_401("u-1", 1)     # recover → 1 launch
    scan_401("u-2", 2)     # recover (guarded sid) → no new launch
    scan_401("u-3", 3)     # escalate → flip + page, NO launch
    # Exactly one launch across the episode (the escalate path does not relaunch).
    assert len(_launch_calls(calls)) == 1
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"
    assert len(_flips(stale)) == 1
    out = capsys.readouterr().out  # manager_limited buffers events; rollup not asserted here
    actions = [e["action"] for e in _auth_events(stale)]
    assert actions == ["recover", "recover", "escalate"]


def test_manager_auth_401_escalate_launches_on_flipped_account(stale, capsys, monkeypatch, tmp_path):
    """When escalation fires for a manager sid with no prior recovery launch
    (the real case — each same-account takeover rolls a fresh sid), the monitor
    flips AND launches the takeover on the HEALTHY account so the manager
    actually recovers; the suspect login is left for the human to /login."""
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _seed_farm(monkeypatch, tmp_path, "b")
    _arm_pool(stale, "a")
    t0 = 1_000_000
    # Pre-seed account a at the attempt ceiling so this scan's 401 escalates.
    stale._record_auth_401("a", "u-prev1", t0)
    stale._record_auth_401("a", "u-prev2", t0 + 10)   # attempts == AUTH_401_MAX_ATTEMPTS
    _write_auth_401_manager(stale, "mgrX", "mgr-A", t0, uuid="u-fresh")
    clock = {"now": t0 + 130}   # past the manager silence floor
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"          # flipped
    assert len(_flips(stale)) == 1
    launches = _launch_calls(calls)
    assert len(launches) == 1
    inner = launches[0][0][-1]
    assert "/manager-takeover-recovery mgrX" in inner
    assert f"CLAUDE_CONFIG_DIR={tmp_path}/.claude-b" in inner  # recovery on the HEALTHY farm
    assert "CLAUDE_ORCH_ACCOUNT=b" in inner                   # recovery on the HEALTHY account
    assert [e["action"] for e in _auth_events(stale)] == ["escalate"]


def test_manager_auth_401_escalate_page_survives_recovery_rollup(stale, capsys, monkeypatch):
    """A manager bricked on its OWN 401 buffers its events while down; the
    escalation /login PAGE is human-facing and must NOT be coalesced away — it
    replays after the recovery rollup once the bricked record is taken over."""
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    stale._record_auth_401("a", "u-prev1", t0)
    stale._record_auth_401("a", "u-prev2", t0 + 10)           # account a at the ceiling
    _write_auth_401_manager(stale, "mgr1", "mgr-A", t0, uuid="u-fresh")
    clock = {"now": t0 + 130}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    assert capsys.readouterr().out == "", "page buffered while the manager is bricked"
    # Takeover unlinks the predecessor record → next scan flushes the rollup and
    # replays the buffered /login page.
    (stale.ACTIVE / "mgr1.json").unlink()
    clock["now"] += 60
    stale.main(manager_name="mgr-A")
    out = capsys.readouterr().out
    assert "limit cleared" in out
    assert "AUTH_401_ESCALATED" in out and "/login" in out


def test_manager_auth_401_escalate_page_only_when_flip_blocked(stale, capsys, monkeypatch):
    """Escalation while the flip is BLOCKED (here by the cooldown guard — a flip
    landed within FLIP_COOLDOWN_SEC) and the manager is still on the suspect
    account: target resolves to None → page ONLY, no recovery tab launched (a
    relaunch on the suspect account would just 401 again). Pointer unchanged;
    the AUTH_401_ESCALATED /login page still reaches the human. Replaces the
    deleted token-item-probe driver of this branch with a credential-agnostic
    guard (cooldown), since _keychain_unlocked has no per-letter semantics."""
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    # A flip landed seconds ago → the cooldown guard blocks any new flip.
    stale.ACCOUNT_STATE.write_text(json.dumps(
        {"accounts": {}, "last_flip": {"ts": t0 - 30, "from": "b", "to": "a"}}))
    stale._record_auth_401("a", "u-prev1", t0)
    stale._record_auth_401("a", "u-prev2", t0 + 10)           # account a at the ceiling
    _write_auth_401_manager(stale, "mgr1", "mgr-A", t0, uuid="u-fresh")
    clock = {"now": t0 + 130}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    assert stale.ACCOUNT_ACTIVE.read_text() == "a\n", "flip blocked by cooldown, pointer unchanged"
    assert _flips(stale) == [], "no new flip — within cooldown"
    assert _launch_calls(calls) == [], "page only — never relaunch on the suspect account"
    assert [e["action"] for e in _auth_events(stale)] == ["escalate"]
    assert capsys.readouterr().out == "", "page buffered while the manager is bricked"
    # Takeover unlinks the predecessor record → next scan flushes the rollup and
    # replays the buffered /login page; still no launch.
    (stale.ACTIVE / "mgr1.json").unlink()
    clock["now"] += 60
    stale.main(manager_name="mgr-A")
    out = capsys.readouterr().out
    assert "AUTH_401_ESCALATED" in out and "/login" in out, "page reaches the human"
    assert _launch_calls(calls) == [], "still no recovery launch on the suspect account"


def test_manager_auth_401_dormant_when_pool_off(nudgy, capsys, monkeypatch):
    """Dormancy invariant at the manager site for auth-401: no pointer ⇒ no
    state, no ledger, no launches."""
    calls = _capture_runs(nudgy, monkeypatch)
    t0 = 1_000_000
    _write_auth_401_manager(nudgy, "mgr1", "mgr-A", t0, uuid="u-1")
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main(manager_name="mgr-A")
    assert not nudgy.ACCOUNT_STATE.exists()
    assert not nudgy.ACCOUNT_LEDGER.exists()
    assert _launch_calls(calls) == []


def test_manager_limit_path_unaffected_by_auth_branch(stale, capsys, monkeypatch):
    """Guardrail: a real limit banner still flips + launches recovery exactly as
    before — the auth-401 branch is a sibling that must not perturb it."""
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    _write_limited_manager(stale, "mgr1", "mgr-A", t0)
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"
    assert len(_flips(stale)) == 1
    assert _auth_events(stale) == [], "a limit banner must never be read as auth-401"
    assert len(_launch_calls(calls)) == 1


# --- A1: stale_monitor autoclose routes through driver (zombie fix) ----------

def test_autoclose_routes_through_driver_on_tmux(stale, monkeypatch):
    import subprocess as _sp
    from dockwright import terminal
    monkeypatch.setenv("CLAUDE_ORCH_TERMINAL", "tmux")
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    terminal._DRIVER = None
    calls = []
    monkeypatch.setattr(_sp, "run", lambda a, *p, **k: (calls.append(list(a)),
        _sp.CompletedProcess(a, 0, b"", b""))[1])
    stale._close_window("%5")
    assert ["tmux", "-L", "S", "kill-pane", "-t", "%5"] in calls
    assert not any(c[0] == "kitty" for c in calls)


def test_autonudge_routes_through_driver_on_tmux(stale, monkeypatch):
    import subprocess as _sp
    from dockwright import terminal
    monkeypatch.setenv("CLAUDE_ORCH_TERMINAL", "tmux")
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    terminal._DRIVER = None
    calls = []
    def fake(a, *p, **k):
        calls.append(list(a)); return _sp.CompletedProcess(a, 0, b"", b"")
    monkeypatch.setattr(_sp, "run", fake)
    stale._send_text("%5", "resume your task")
    assert any("load-buffer" in c for c in calls)
    assert any("paste-buffer" in c and "%5" in c for c in calls)
    assert any("send-keys" in c and c[-1] == "Enter" for c in calls)
    assert not any(c[0] == "kitty" for c in calls)


def test_autoclose_tmux_on_default_backend(stale, monkeypatch):
    # Default backend is tmux, so the unset-env default routes autoclose
    # through TmuxDriver.close -> kill-pane.
    import subprocess as _sp
    from dockwright import terminal
    monkeypatch.delenv("CLAUDE_ORCH_TERMINAL", raising=False)
    terminal._DRIVER = None
    calls = []
    monkeypatch.setattr(_sp, "run", lambda a, *p, **k: (calls.append(list(a)),
        _sp.CompletedProcess(a, 0, b"", b""))[1])
    stale._close_window("42")
    assert ["tmux", "-L", "dockwright", "kill-pane", "-t", "42"] in calls
    assert not any(c[0] == "kitty" for c in calls)


# ---- A3: recovery-manager spawn routes through driver on tmux backend ----------

def test_recovery_manager_routes_to_mgr_on_tmux(stale, monkeypatch):
    """tmux backend: _launch_recovery_manager delegates to driver.spawn with
    route_to_manager_session=True and the correct argv/title. The driver routes
    to the mgr session unconditionally (no target_window_match)."""
    captured = {}

    class FakeDrv:
        async def spawn(self, **kw):
            captured.update(kw)
            return "%9"

    monkeypatch.setattr(stale, "_get_driver", lambda: FakeDrv())
    rec = {"cwd": "/c", "name": "m", "window_id": "%14"}
    out = stale._launch_recovery_manager(rec, "sid-1", "a")
    assert captured.get("route_to_manager_session") is True
    assert out == "%9"
    assert captured["title"].startswith("manager (recovery)")
    assert "/manager-takeover-recovery sid-1" in " ".join(captured["argv"])
    assert "target_window_match" not in captured


def test_recovery_manager_on_tmux_is_absorbed_not_spawned(no_live_tmux, stale, monkeypatch):
    """Full detonation entry: _launch_recovery_manager with backend=tmux and the
    REAL driver (no FakeDrv). The `claude /manager-takeover-recovery` spawn must be
    absorbed by no_live_tmux — sentinel pane, never executed into the live mgr."""
    from dockwright import terminal
    monkeypatch.setenv("CLAUDE_ORCH_TERMINAL", "tmux")
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "claude-orch")  # live socket — still absorbed
    terminal._DRIVER = None

    rec = {"cwd": "/c", "name": "m", "window_id": "%14"}
    out = stale._launch_recovery_manager(rec, "sid-9", "a")

    assert out == "%no-live-tmux", "recovery spawn returned a real pane — NOT absorbed"
    assert any("/manager-takeover-recovery sid-9" in " ".join(a) for a in no_live_tmux.exec), \
        "the recovery command was not the one intercepted"


def test_launch_recovery_manager_pins_manager_opus(stale, monkeypatch):
    """The recovery tab must never inherit the user's interactive model default
    (orch-audit model-allocation: manager lane = opus 1M)."""
    captured = {}

    class FakeDrv:
        async def spawn(self, **kw):
            captured.update(kw)
            return "%9"

    monkeypatch.setattr(stale, "_get_driver", lambda: FakeDrv())
    rec = {"cwd": "/c", "name": "m", "window_id": "%14"}
    out = stale._launch_recovery_manager(rec, "sid-1", "a")
    assert out == "%9"
    inner = captured["argv"][-1]
    assert "--model 'opus[1m]'" in inner
    assert inner.index("--model") < inner.index("/manager-takeover-recovery")


def test_write_json_atomic_unique_tmp_per_invocation(tmp_path, monkeypatch):
    # stale_monitor's private copy shares the closed/<sid>.json target with
    # hooks.session_end on the autoclose path - same shared-tmp race class.
    sm = _load_stale_monitor()
    target = tmp_path / "sid.json"
    srcs = []
    real_replace = os.replace
    def recording_replace(src, dst):
        srcs.append(str(src))
        real_replace(src, dst)
    monkeypatch.setattr(sm.os, "replace", recording_replace)
    sm._write_json_atomic(target, {"a": 1})
    sm._write_json_atomic(target, {"a": 2})
    assert len(srcs) == 2 and srcs[0] != srcs[1]


# ---------------------------------------------------------------------------
# notify-outbox divert (autoclosed) + timeout flush


def _outbox_entries(stale, manager="mgr"):
    return sorted((stale.ROOT / "notify-outbox" / manager).glob("*.json"))


def _seed_outbox(stale, line, buffered_at, manager="mgr", filename=None):
    outbox = stale.ROOT / "notify-outbox" / manager
    outbox.mkdir(parents=True, exist_ok=True)
    fname = filename or f"{int(buffered_at * 1000)}-0-0.json"
    (outbox / fname).write_text(json.dumps(
        {"line": line, "kind": "autoclosed", "buffered_at": buffered_at}))
    return outbox / fname


def _make_idle_worker_past_threshold(stale, sid="wkr-idle", manager="mgr", last_turn=None):
    # state=idle (record default), last turn older than IDLE_THRESHOLD_SEC (the
    # fixture squashes it to 100s), no pending question -> the autoclose branch
    # reaps it. Same record shape as the existing autoclose tests (wall-clock
    # last_turn_at; no last_turn_at_uptime so the ISO fallback is used).
    epoch = last_turn if last_turn is not None else int(time.time()) - 10_000
    return _write_record(
        stale, sid, parent_manager_name=manager,
        last_turn_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)))


def test_autoclosed_diverts_to_outbox_when_scan_otherwise_silent(stale, capsys):
    _make_idle_worker_past_threshold(stale)
    rc = stale.main(manager_name="mgr")
    assert rc == 0
    out = capsys.readouterr().out
    assert "AUTOCLOSED" not in out
    entries = _outbox_entries(stale)
    assert len(entries) == 1
    payload = json.loads(entries[0].read_text())
    assert payload["line"].startswith("AUTOCLOSED") and "wkr-idle" in payload["line"]
    assert payload["kind"] == "autoclosed"
    assert isinstance(payload["buffered_at"], (int, float))


def test_autoclosed_prints_when_other_lines_print(stale, capsys):
    # A stale question makes the scan print anyway -> autoclosed rides the
    # same burst instead of being buffered.
    _make_idle_worker_past_threshold(stale)
    now = int(time.time())
    # STALE_QUESTION only emits when an ACTIVE record exists for its worker_sid;
    # this fresh idle record is question-blocked, so autoclose skips it.
    _write_record(stale, "q-worker", parent_manager_name="mgr")
    _write_question(stale, "q-1", "q-worker", parent_manager_name="mgr",
                    asked_at=now - 600)
    stale.main(manager_name="mgr")
    out = capsys.readouterr().out
    assert "STALE_QUESTION" in out
    assert "AUTOCLOSED" in out and "wkr-idle" in out
    assert out.index("STALE_QUESTION") < out.index("AUTOCLOSED")
    assert _outbox_entries(stale) == []


def test_outbox_write_failure_falls_back_to_print(stale, capsys, monkeypatch):
    # I9: a swallowed divert-write would be a true loss. Fallback floor: print.
    # Fail ONLY the outbox write — _write_json_atomic also writes the
    # closed/<sid>.json record inside _autoclose_idle_worker, which must
    # keep working for the autoclose to happen at all.
    _make_idle_worker_past_threshold(stale)
    real_write = stale._write_json_atomic

    def boom(path, data):
        if "notify-outbox" in str(path):
            raise OSError("disk full")
        return real_write(path, data)

    monkeypatch.setattr(stale, "_write_json_atomic", boom)
    stale.main(manager_name="mgr")
    captured = capsys.readouterr()
    assert "AUTOCLOSED" in captured.out and "wkr-idle" in captured.out
    assert "outbox write failed" in captured.err


def test_timeout_flush_after_max_hold(stale, capsys):
    now = time.time()
    _seed_outbox(stale, "AUTOCLOSED lonely idle 130min", now - stale.OUTBOX_MAX_HOLD_SEC - 60)
    stale.main(manager_name="mgr")  # nothing else stale
    assert "AUTOCLOSED lonely idle 130min" in capsys.readouterr().out
    assert _outbox_entries(stale) == []


def test_no_timeout_flush_before_max_hold(stale, capsys):
    now = time.time()
    entry = _seed_outbox(stale, "AUTOCLOSED young idle 130min", now - 60)
    stale.main(manager_name="mgr")
    assert capsys.readouterr().out == ""
    assert entry.exists()


def test_timeout_uses_mtime_when_buffered_at_missing(stale, capsys):
    now = time.time()
    outbox = stale.ROOT / "notify-outbox" / "mgr"
    outbox.mkdir(parents=True, exist_ok=True)
    entry = outbox / "0000000000000-0-0.json"
    entry.write_text(json.dumps({"line": "AUTOCLOSED legacy idle 130min", "kind": "autoclosed"}))
    os.utime(entry, (now - stale.OUTBOX_MAX_HOLD_SEC - 60,) * 2)
    stale.main(manager_name="mgr")
    assert "AUTOCLOSED legacy idle 130min" in capsys.readouterr().out


def test_limited_manager_holds_outbox_and_buffers_autoclose(stale, capsys, monkeypatch):
    # While limited: no prints at all, pre-existing outbox entries stay (even
    # past max hold), the fresh autoclose lands in limited_buffer counters.
    t0 = 1_000_000
    clock = {"now": t0 + 30 * 60}
    held = _seed_outbox(stale, "AUTOCLOSED preexisting idle 130min",
                        clock["now"] - stale.OUTBOX_MAX_HOLD_SEC - 60)
    _make_idle_worker_past_threshold(stale, last_turn=t0)
    _write_limited_manager(stale, "mgr1", "mgr", t0)
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr")
    assert capsys.readouterr().out == ""
    assert held.exists()
    emitted = json.loads((stale.ROOT / ".stale-emitted-mgr.json").read_text())
    assert emitted["limited_buffer"]["autoclosed"] == 1


def test_recovery_rollup_drains_outbox(stale, capsys):
    # Manager no longer limited but flag/buffer exist -> rollup prints, then
    # the held outbox entries ride the same burst. Arrange mirrors
    # test_recovery_rollup_survives_poisoned_buffer: buffer in the emitted
    # state + flag file, no limited manager record.
    now = int(time.time())
    _seed_outbox(stale, "AUTOCLOSED heldover idle 130min", now - 120)
    stale._emitted_state_path("mgr").write_text(json.dumps({"limited_buffer": {
        "since": now - 1800, "stalled_names": ["worker-tab"], "nudged": 1,
        "resumed": 0, "questions": 0, "autoclosed": 0, "suppressed_keys": []}}))
    (stale.ROOT / ".manager-limited-mgr").touch()
    stale.main(manager_name="mgr")
    out = capsys.readouterr().out
    assert "limit cleared" in out
    assert "AUTOCLOSED heldover idle 130min" in out
    assert out.index("limit cleared") < out.index("AUTOCLOSED heldover")


def test_unscoped_run_prints_autoclosed_directly(stale, capsys):
    _make_idle_worker_past_threshold(stale, manager=None)  # null-parent record
    stale.main(manager_name=None)
    assert "AUTOCLOSED" in capsys.readouterr().out
    assert not (stale.ROOT / "notify-outbox").exists()


def test_root_prefers_dockwright_home(tmp_path, monkeypatch):
    (tmp_path / ".claude" / "dockwright").mkdir(parents=True)
    (tmp_path / ".claude" / "orchestrator").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    mod = _load_stale_monitor()
    assert mod.ROOT == tmp_path / ".claude" / "dockwright"
    assert mod._LEGACY_ROOT == tmp_path / ".claude" / "orchestrator"


def test_root_falls_back_to_legacy_home(tmp_path, monkeypatch):
    (tmp_path / ".claude" / "orchestrator").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    mod = _load_stale_monitor()
    assert mod.ROOT == tmp_path / ".claude" / "orchestrator"


def test_count_unseen_done_normalizes_legacy_cursor(stale, tmp_path, monkeypatch):
    # A pre-rename cursor stored the done-event's absolute path under the OLD
    # state root; after the rename the done files live under the new ROOT, so the
    # cursor line must be normalized or the event is miscounted as unseen forever.
    legacy_root = tmp_path.parent / "legacy-orch"
    monkeypatch.setattr(stale, "_LEGACY_ROOT", legacy_root)
    done_dir = stale.ROOT / "done" / "mgr-A"
    done_dir.mkdir(parents=True)
    (done_dir / "e1.json").write_text("{}")
    legacy_line = str(legacy_root / "done" / "mgr-A" / "e1.json")
    (stale.ROOT / ".seen-done-mgr-A").write_text(legacy_line + "\n")
    assert stale._count_unseen_done_events("mgr-A") == 0


def test_count_unseen_done_counts_genuinely_unseen(stale, tmp_path, monkeypatch):
    monkeypatch.setattr(stale, "_LEGACY_ROOT", tmp_path.parent / "legacy-orch")
    done_dir = stale.ROOT / "done" / "mgr-A"
    done_dir.mkdir(parents=True)
    (done_dir / "e1.json").write_text("{}")
    assert stale._count_unseen_done_events("mgr-A") == 1


# ---- orphan-window alarm -------------------------------------------------

def _ls_shape(panes):
    """Parsed-driver shape: [(session, window_title, pane_id), ...] →
    [{'wm_class': s, 'tabs': [{'title': t, 'windows': [{'id': p}]}]}]"""
    sessions = {}
    for session, title, pane in panes:
        sessions.setdefault(session, []).append(
            {"title": title, "windows": [{"id": pane}]})
    return [{"wm_class": s, "tabs": tabs} for s, tabs in sessions.items()]


def _arm_driver(stale, monkeypatch, panes):
    driver = types.SimpleNamespace(ls=lambda: _ls_shape(panes))
    monkeypatch.setattr(stale, "_get_driver", lambda: driver)


def _seed_orphan_state(stale, pane_id, first_seen, paged=0, manager_name=None):
    state_path = stale._emitted_state_path(manager_name)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(
        {f"orphan:{pane_id}": {"first_seen": first_seen, "paged": paged}}))


def test_orphan_window_pages_after_grace(stale, monkeypatch, capsys):
    _arm_driver(stale, monkeypatch, [("claude-workers", "dead-worker", "%5")])
    _seed_orphan_state(stale, "%5", time.time() - 130)
    stale.main()
    out = capsys.readouterr().out
    assert "ORPHAN_WINDOW %5" in out
    assert "no backing active record" in out


def test_orphan_window_quiet_within_grace(stale, monkeypatch, capsys):
    _arm_driver(stale, monkeypatch, [("claude-workers", "w", "%5")])
    stale.main()
    assert "ORPHAN_WINDOW" not in capsys.readouterr().out
    # first_seen tracked so a later scan can page
    emitted = json.loads(stale._emitted_state_path(None).read_text())
    assert "orphan:%5" in emitted


def test_orphan_window_protected_by_active_record(stale, tmp_path, monkeypatch, capsys):
    _arm_driver(stale, monkeypatch, [("claude-workers", "w", "%5")])
    (tmp_path / "active" / "s1.json").write_text(json.dumps(
        {"claude_sid": "s1", "agent": "worker", "name": "alpha",
         "state": "idle", "window_id": "%5"}))
    _seed_orphan_state(stale, "%5", time.time() - 700)
    stale.main()
    assert "ORPHAN_WINDOW" not in capsys.readouterr().out


def test_orphan_window_protected_by_legacy_iterm_sid(stale, tmp_path, monkeypatch, capsys):
    _arm_driver(stale, monkeypatch, [("claude-workers", "w", "%5")])
    (tmp_path / "active" / "s1.json").write_text(json.dumps(
        {"claude_sid": "s1", "agent": "worker", "name": "alpha",
         "state": "idle", "iterm_sid": "%5"}))
    _seed_orphan_state(stale, "%5", time.time() - 700)
    stale.main()
    assert "ORPHAN_WINDOW" not in capsys.readouterr().out


def test_orphan_window_protected_by_pending_spawn_sidecar(stale, tmp_path, monkeypatch, capsys):
    _arm_driver(stale, monkeypatch, [("claude-workers", "w", "%5")])
    pending = tmp_path / "assignments" / ".pending"
    pending.mkdir(parents=True)
    (pending / "a-1.window").write_text("%5\n")
    _seed_orphan_state(stale, "%5", time.time() - 700)
    stale.main()
    assert "ORPHAN_WINDOW" not in capsys.readouterr().out


def test_orphan_window_protected_by_closed_record_with_pending_question(
        stale, tmp_path, monkeypatch, capsys):
    _arm_driver(stale, monkeypatch, [("claude-workers", "w", "%5")])
    (tmp_path / "closed" / "s1.json").write_text(json.dumps(
        {"claude_sid": "s1", "name": "alpha", "window_id": "%5"}))
    (tmp_path / "questions").mkdir(exist_ok=True)
    (tmp_path / "questions" / "q1.json").write_text(json.dumps(
        {"question_id": "q1", "worker_sid": "s1", "question": "?",
         "asked_at": time.time()}))
    _seed_orphan_state(stale, "%5", time.time() - 700)
    stale.main()
    assert "ORPHAN_WINDOW" not in capsys.readouterr().out


def test_orphan_scan_skips_when_a_worker_lacks_window_id(
        stale, tmp_path, monkeypatch, capsys):
    # One capture-failed worker makes pane attribution unreliable fleet-wide:
    # never false-page, skip the whole scan (stderr note).
    _arm_driver(stale, monkeypatch, [("claude-workers", "w", "%5")])
    (tmp_path / "active" / "s1.json").write_text(json.dumps(
        {"claude_sid": "s1", "agent": "worker", "name": "alpha",
         "state": "idle", "window_id": ""}))
    _seed_orphan_state(stale, "%5", time.time() - 700)
    stale.main()
    captured = capsys.readouterr()
    assert "ORPHAN_WINDOW" not in captured.out
    assert "orphan scan skipped" in captured.err


def test_orphan_scan_ignores_windowless_nested_records(
        stale, tmp_path, monkeypatch, capsys):
    # Nested records carry window_id="" by design and own no worker window —
    # they must not disable the alarm.
    _arm_driver(stale, monkeypatch, [("claude-workers", "w", "%5")])
    (tmp_path / "active" / "s1.json").write_text(json.dumps(
        {"claude_sid": "s1", "agent": "worker", "name": "nested-x",
         "state": "idle", "window_id": "", "nested": True}))
    _seed_orphan_state(stale, "%5", time.time() - 130)
    stale.main()
    assert "ORPHAN_WINDOW %5" in capsys.readouterr().out


def test_orphan_window_ladder_dedups_and_repages(stale, monkeypatch, capsys):
    _arm_driver(stale, monkeypatch, [("claude-workers", "w", "%5")])
    # Paged at the 2min rung already; 2.5min elapsed → same rung → quiet.
    _seed_orphan_state(stale, "%5", time.time() - 150, paged=2)
    stale.main()
    assert "ORPHAN_WINDOW" not in capsys.readouterr().out
    # 4.5min elapsed → 4min rung > paged 2 → re-page.
    _seed_orphan_state(stale, "%5", time.time() - 270, paged=2)
    stale.main()
    assert "ORPHAN_WINDOW %5" in capsys.readouterr().out


def test_orphan_state_key_dropped_when_pane_disappears(stale, monkeypatch, capsys):
    _arm_driver(stale, monkeypatch, [])
    _seed_orphan_state(stale, "%5", time.time() - 700, paged=2)
    stale.main()
    emitted = json.loads(stale._emitted_state_path(None).read_text())
    assert "orphan:%5" not in emitted


def test_orphan_scan_survives_driver_none_and_ls_none(stale, monkeypatch, capsys):
    monkeypatch.setattr(stale, "_get_driver", None)
    assert stale.main() == 0
    monkeypatch.setattr(stale, "_get_driver",
                        lambda: types.SimpleNamespace(ls=lambda: None))
    assert stale.main() == 0


def test_orphan_protection_is_fleet_global_in_scoped_runs(
        stale, tmp_path, monkeypatch, capsys):
    # Another manager's worker record still protects its pane in a scoped scan.
    _arm_driver(stale, monkeypatch, [("claude-workers", "w", "%5")])
    (tmp_path / "active" / "s1.json").write_text(json.dumps(
        {"claude_sid": "s1", "agent": "worker", "name": "alpha", "state": "idle",
         "window_id": "%5", "parent_manager_name": "other-mgr"}))
    # Seed the SCOPED emitted-state file the scan under test actually reads
    # (manager_name="my-mgr") — seeding the global file here would leave the
    # scoped scan with no first_seen, so it'd start its own clock at "now"
    # and stay quiet within grace regardless of protection, proving nothing.
    _seed_orphan_state(stale, "%5", time.time() - 700, manager_name="my-mgr")
    stale.main(manager_name="my-mgr")
    assert "ORPHAN_WINDOW" not in capsys.readouterr().out


def test_recovery_flush_unburns_orphan_ladder_without_resetting_clock(
        stale, tmp_path, monkeypatch, capsys):
    # A manager recovering from a limited stretch un-burns the dedup keys
    # whose lines only ever reached the buffer. For a dict-valued orphan
    # ladder, that must reset just the "paged" rung in place — popping the
    # whole entry would also erase first_seen and restart the grace window.
    _arm_driver(stale, monkeypatch, [("claude-workers", "w", "%5")])
    first_seen = time.time() - 700
    stale._emitted_state_path("mgr-A").write_text(json.dumps({
        "orphan:%5": {"first_seen": first_seen, "paged": 2},
        "limited_buffer": {
            "since": first_seen, "stalled_names": [], "nudged": 0,
            "resumed": 0, "questions": 0, "autoclosed": 0,
            "suppressed_keys": ["orphan:%5"]},
    }))
    (tmp_path / ".manager-limited-mgr-A").touch()

    stale.main(manager_name="mgr-A")

    emitted = json.loads(stale._emitted_state_path("mgr-A").read_text())
    entry = emitted["orphan:%5"]
    assert entry["first_seen"] == first_seen, (
        "un-burn must preserve the ladder's original first_seen — popping "
        "the whole dict restarts the grace window instead of re-arming")
    assert entry["paged"] == 0


def test_orphan_session_name_matches_terminal_constant(stale):
    from dockwright.terminal import WORKERS_OS_WINDOW_CLASS
    assert stale.WORKERS_SESSION_NAME == WORKERS_OS_WINDOW_CLASS


def test_interactive_shell_duplicate_no_zsh_falls_back_to_bash(stale, monkeypatch):
    monkeypatch.setenv("SHELL", "/usr/bin/fish")
    monkeypatch.setattr(stale.shutil, "which",
                        lambda cmd: {"bash": "/usr/bin/bash"}.get(cmd))
    assert stale._interactive_shell() == "/usr/bin/bash"


def test_awake_seconds_duplicate_works_without_clock_uptime_raw(stale, monkeypatch):
    monkeypatch.delattr(time, "CLOCK_UPTIME_RAW", raising=False)
    v = stale._awake_seconds()
    assert isinstance(v, float) and v > 0.0
