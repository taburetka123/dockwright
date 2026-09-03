import importlib.util
import json
import os
import shutil
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


def _IDLE_PROC_INDEX():
    return {"command_by_pid": {1: "/sbin/launchd"}, "child_commands": {}}


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
    monkeypatch.setattr(mod, "EMITTED_STATE", mod._emitted_state_path(None), raising=False)
    monkeypatch.setattr(mod, "ASSIGNMENTS_PENDING",
                        tmp_path / "assignments" / ".pending", raising=False)
    monkeypatch.setattr(mod, "GARDENER_LIVE_WINDOWS",
                        tmp_path / "gardener" / "live-windows", raising=False)
    monkeypatch.setattr(mod, "IDLE_THRESHOLD_SEC", 100)
    monkeypatch.setattr(mod, "_process_index", _IDLE_PROC_INDEX)
    for d in ("active", "questions", "closed"):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(mod, "ACCOUNT_ACTIVE", tmp_path / "account-active")
    monkeypatch.setattr(mod, "ACCOUNT_REGISTRY", tmp_path / "account-registry.json",
                        raising=False)
    monkeypatch.setattr(mod, "ACCOUNT_LEDGER", tmp_path / "account-flips.jsonl")
    monkeypatch.setattr(mod, "ACCOUNT_STATE", tmp_path / "account-state.json")
    monkeypatch.setattr(mod, "ACCOUNT_LOCK", tmp_path / ".account-flip.lock")
    monkeypatch.setattr(mod, "_keychain_unlocked", lambda: False)
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
    now = int(time.time())
    stale.EMITTED_STATE.write_text(json.dumps({"last_autoclose_run": now - 600}))
    record_path = _write_record(
        stale, "s1",
        last_turn_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10_000)),
    )
    rc = stale.main()
    assert rc == 0
    assert record_path.exists()
    assert not (stale.CLOSED / "s1.json").exists()
    emitted = json.loads(stale.EMITTED_STATE.read_text())
    assert emitted["last_autoclose_run"] == now - 600

    stale.EMITTED_STATE.write_text(json.dumps({"last_autoclose_run": now - 3700}))
    rc = stale.main()
    assert rc == 0
    assert not record_path.exists()
    assert (stale.CLOSED / "s1.json").exists()
    emitted = json.loads(stale.EMITTED_STATE.read_text())
    assert emitted["last_autoclose_run"] >= now - 1


def test_autoclose_branch_runs_on_first_scan_when_key_absent(stale):
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
    now = int(time.time())
    path = _write_record(
        stale, "mgr1",
        agent="manager",
        state="processing",
        name="manager-tab",
    )
    old_mtime = now - 2700
    os.utime(path, (old_mtime, old_mtime))

    rc = stale.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "STALE_PROCESSING" not in out

    path_w = _write_record(
        stale, "w1",
        agent="worker",
        state="processing",
        name="worker-tab",
    )
    os.utime(path_w, (old_mtime, old_mtime))
    stale.EMITTED_STATE.unlink(missing_ok=True)

    rc = stale.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "STALE_PROCESSING worker-tab" in out
    assert "STALE_PROCESSING manager-tab" not in out


def test_processing_emits_once_per_threshold_crossing(stale, capsys, monkeypatch):
    path = _write_record(stale, "w1", agent="worker", state="processing", name="worker-tab")
    stretch_start = 1_000_000
    os.utime(path, (stretch_start, stretch_start))
    clock = {"now": stretch_start}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])

    def run_at(elapsed_min):
        clock["now"] = stretch_start + elapsed_min * 60
        stale.main()
        return capsys.readouterr().out

    assert "STALE_PROCESSING" not in run_at(29)
    assert "STALE_PROCESSING worker-tab" in run_at(30)
    assert "STALE_PROCESSING" not in run_at(31)
    assert "STALE_PROCESSING worker-tab" in run_at(60)
    assert "STALE_PROCESSING worker-tab" in run_at(121)


def test_processing_realarms_on_new_stretch_without_observed_idle(stale, capsys, monkeypatch):
    path = _write_record(stale, "w1", agent="worker", state="processing", name="worker-tab")
    clock = {"now": 1_000_000}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])

    t0 = 1_000_000
    os.utime(path, (t0, t0))
    clock["now"] = t0 + 121 * 60
    stale.main()
    assert "STALE_PROCESSING worker-tab" in capsys.readouterr().out
    emitted = json.loads(stale.EMITTED_STATE.read_text())
    assert emitted.get(f"processing:w1:{t0}") == 120

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
    path = _write_record(stale, "w1", agent="worker", state="processing", name="worker-tab")
    clock = {"now": 1_000_000}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])

    t0 = 1_000_000
    os.utime(path, (t0, t0))
    clock["now"] = t0 + 35 * 60
    stale.main()
    capsys.readouterr()
    assert json.loads(stale.EMITTED_STATE.read_text()).get(f"processing:w1:{t0}") == 30

    _write_record(stale, "w1", agent="worker", state="idle", name="worker-tab")
    clock["now"] = t0 + 36 * 60
    stale.main()
    capsys.readouterr()
    emitted = json.loads(stale.EMITTED_STATE.read_text())
    assert f"processing:w1:{t0}" not in emitted, (
        "processing key must be pruned once the worker goes idle"
    )

    t1 = t0 + 60 * 60
    _write_record(stale, "w1", agent="worker", state="processing", name="worker-tab")
    os.utime(stale.ACTIVE / "w1.json", (t1, t1))
    clock["now"] = t1 + 30 * 60
    stale.main()
    assert "STALE_PROCESSING worker-tab" in capsys.readouterr().out


def test_elapsed_uses_uptime_when_present(stale, monkeypatch):
    fake_current_uptime = 100_000.0
    record = {
        "last_turn_at_uptime": fake_current_uptime - 1800,
        "last_turn_at": "2026-05-19T00:00:00Z",
        "started_at": time.time() - 86400,
    }
    elapsed = stale._compute_idle_elapsed_sec(record, fake_current_uptime, int(time.time()))
    assert elapsed == 1800


def test_elapsed_falls_back_to_wall_on_reboot(stale):
    now = int(time.time())
    record = {
        "last_turn_at_uptime": 50_000.0,
        "last_turn_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 7200)),
        "started_at": now - 7200,
    }
    elapsed = stale._compute_idle_elapsed_sec(record, current_uptime=120.0, now=now)
    assert elapsed is not None
    assert 7195 <= elapsed <= 7205


def test_elapsed_falls_back_to_wall_when_field_absent(stale):
    now = int(time.time())
    record = {
        "last_turn_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600)),
        "started_at": now - 3600,
    }
    elapsed = stale._compute_idle_elapsed_sec(record, current_uptime=100_000.0, now=now)
    assert elapsed is not None
    assert 3595 <= elapsed <= 3605


def test_elapsed_uses_started_at_when_last_turn_missing(stale):
    now = int(time.time())
    record = {"started_at": now - 500}
    elapsed = stale._compute_idle_elapsed_sec(record, current_uptime=100.0, now=now)
    assert elapsed is not None
    assert 495 <= elapsed <= 505


def test_elapsed_returns_none_when_no_anchor(stale):
    record = {"started_at": None}
    elapsed = stale._compute_idle_elapsed_sec(record, current_uptime=100.0, now=int(time.time()))
    assert elapsed is None


def test_autoclose_closes_window_and_skips_sigterm(no_live_tmux, stale, monkeypatch):
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
    assert not record_path.exists()
    assert (stale.CLOSED / "s1.json").exists()
    assert killed == []
    kill_calls = [
        c for c in no_live_tmux.run
        if c[0] == "tmux" and "kill-pane" in c and "42" in c
    ]
    assert len(kill_calls) == 1, f"expected 1 tmux kill-pane call, got {no_live_tmux.run!r}"
    assert not any(c[0] == "kitty" for c in no_live_tmux.run)
    assert selffix_calls == []


def test_autoclose_preserves_idle_closed_reason(stale, monkeypatch):
    now = int(time.time())
    record_path = _write_record(
        stale, "s1",
        pid=12345,
        iterm_sid="7",
        last_turn_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10_000)),
    )

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
    now = int(time.time())
    _write_record(
        stale, "s1",
        pid=12345,
        iterm_sid="7",
        account="b",
        transcript_path="/tmp/x/s1.jsonl",
        last_turn_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10_000)),
    )
    monkeypatch.setattr(stale.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(stale.os, "kill", lambda *a, **k: None)
    rc = stale.main()
    assert rc == 0
    closed_data = json.loads((stale.CLOSED / "s1.json").read_text())
    assert closed_data["account"] == "b"
    assert closed_data["transcript_path"] == "/tmp/x/s1.jsonl"


def test_autoclose_swallows_window_close_failure(stale, monkeypatch):
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
    monkeypatch.delenv("CLAUDE_ORCH_IDLE_TTL_HOURS", raising=False)
    mod = _load_stale_monitor()
    assert mod._IDLE_HOURS == 2.0, f"default IDLE_HOURS expected 2.0, got {mod._IDLE_HOURS!r}"
    assert mod.IDLE_THRESHOLD_SEC == 7200, f"expected 7200s (2h), got {mod.IDLE_THRESHOLD_SEC}"


def test_matches_manager_filter_semantics(stale):
    own = {"parent_manager_name": "mgr-A"}
    peer = {"parent_manager_name": "mgr-B"}
    legacy = {"parent_manager_name": None}
    missing = {}
    assert all(stale._matches_manager(r, None) for r in (own, peer, legacy, missing))
    assert stale._matches_manager(own, "mgr-A") is True
    assert stale._matches_manager(legacy, "mgr-A") is False
    assert stale._matches_manager(missing, "mgr-A") is False
    assert stale._matches_manager(peer, "mgr-A") is False


def _stale_processing_worker(stale, sid, name, parent, now):
    path = _write_record(
        stale, sid, agent="worker", state="processing", name=name,
        parent_manager_name=parent,
    )
    old = now - 2700
    os.utime(path, (old, old))
    return path


def test_processing_scan_scoped_skips_peer_and_legacy(stale, capsys):
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
    now = int(time.time())
    _stale_processing_worker(stale, "peer", "peer-tab", "mgr-B", now)
    _stale_processing_worker(stale, "own", "own-tab", "mgr-A", now)

    rc = stale.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "STALE_PROCESSING peer-tab" in out
    assert "STALE_PROCESSING own-tab" in out


def test_idle_autoclose_scoped_skips_peer_and_legacy(stale, monkeypatch):
    now = int(time.time())
    old_turn = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10_000))
    own = _write_record(stale, "own", parent_manager_name="mgr-A", last_turn_at=old_turn)
    peer = _write_record(stale, "peer", parent_manager_name="mgr-B", last_turn_at=old_turn)
    legacy = _write_record(stale, "legacy", parent_manager_name=None, last_turn_at=old_turn)

    monkeypatch.setattr(stale.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(stale.os, "kill", lambda *a, **k: None)

    rc = stale.main(manager_name="mgr-A")
    assert rc == 0
    assert not own.exists() and (stale.CLOSED / "own.json").exists()
    assert legacy.exists(), "null-parent idle worker must NOT be auto-closed under strict routing"
    assert not (stale.CLOSED / "legacy.json").exists()
    assert peer.exists(), "peer manager's idle worker must NOT be auto-closed"
    assert not (stale.CLOSED / "peer.json").exists()


def test_idle_autoclose_global_reaps_peer(stale, monkeypatch):
    now = int(time.time())
    old_turn = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10_000))
    peer = _write_record(stale, "peer", parent_manager_name="mgr-B", last_turn_at=old_turn)
    monkeypatch.setattr(stale.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(stale.os, "kill", lambda *a, **k: None)

    rc = stale.main()
    assert rc == 0
    assert not peer.exists() and (stale.CLOSED / "peer.json").exists()


def test_question_scan_scoped_skips_peer_and_legacy(stale, capsys):
    now = int(time.time())
    asked = now - 600
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
    now = int(time.time())
    asked = now - 600
    _write_record(stale, "peer", parent_manager_name="mgr-B")
    _write_question(stale, "q-peer", "peer", worker_name="peer-w", parent_manager_name="mgr-B", asked_at=asked)

    rc = stale.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "STALE_QUESTION q-peer" in out


def test_scoped_and_global_use_separate_dedup_files(stale, monkeypatch):
    assert stale._emitted_state_path(None) == stale.ROOT / ".stale-emitted.json"
    assert stale._emitted_state_path("mgr-A") == stale.ROOT / ".stale-emitted-mgr-A.json"
    assert stale._emitted_state_path("a/b") == stale.ROOT / ".stale-emitted-a_b.json"

    now = int(time.time())
    _stale_processing_worker(stale, "own", "own-tab", "mgr-A", now)
    stale.main(manager_name="mgr-A")
    assert (stale.ROOT / ".stale-emitted-mgr-A.json").exists()
    assert not (stale.ROOT / ".stale-emitted.json").exists(), (
        "scoped run must not write the global dedup file"
    )


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
    log = tmp_path / "big.jsonl"
    filler = _assistant_line("filler " + "x" * 100)
    log.write_text("\n".join([filler] * 200 + [_assistant_line("THE END")]) + "\n")
    assert stale._last_assistant_text(log, max_bytes=4096) == "THE END"


def test_last_assistant_text_missing_or_empty(stale, tmp_path):
    assert stale._last_assistant_text(tmp_path / "absent.jsonl") is None
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert stale._last_assistant_text(empty) is None


SESSION_LIMIT_TEXT = "You’ve hit your session limit · resets 2:20am (Etc/GMT-9)"


SESSION_LIMIT_NO_RESET = "You’ve hit your session limit · resets soon"


API_529_TEXT = ("API Error: 529 Overloaded. This is a server-side issue, usually "
                "temporary — try again in a moment. If it persists, check "
                "https://status.claude.com.")


def test_is_rate_limited_matches_throttle_transcript(stale):
    _write_transcript(stale, "w1", THROTTLE_TEXT)
    record = {"claude_sid": "w1", "runtime": "claude"}
    assert stale._is_rate_limited(record) is True
    assert stale._is_rate_limited({"claude_sid": "w1"}) is True


def test_is_rate_limited_matches_session_limit_banner(stale):
    _write_transcript(stale, "w1", SESSION_LIMIT_TEXT)
    assert stale._is_rate_limited({"claude_sid": "w1"}) is True
    _write_transcript(stale, "w2", "You've hit your session limit · resets 6pm")
    assert stale._is_rate_limited({"claude_sid": "w2"}) is True


def test_is_rate_limited_negative_cases(stale):
    _write_transcript(stale, "ok", "All done, opening the PR now.")
    assert stale._is_rate_limited({"claude_sid": "ok"}) is False
    _write_transcript(stale, "cdx", THROTTLE_TEXT)
    assert stale._is_rate_limited({"claude_sid": "cdx", "runtime": "codex"}) is False
    assert stale._is_rate_limited({"claude_sid": "ghost"}) is False
    assert stale._is_rate_limited({}) is False


def test_is_transient_throttle(stale):
    assert stale._is_transient_throttle(THROTTLE_TEXT) is True
    assert stale._is_transient_throttle("Server is temporarily limiting requests") is True
    assert stale._is_transient_throttle("anything (not your usage limit) here") is True
    assert stale._is_transient_throttle(SESSION_LIMIT_TEXT) is False
    assert stale._is_transient_throttle("You've hit your session limit · resets soon") is False
    assert stale._is_transient_throttle(None) is False
    assert stale._is_transient_throttle("") is False


def test_is_transient_throttle_matches_529(stale):
    assert stale._is_transient_throttle(API_529_TEXT) is True
    assert stale._is_transient_throttle("api error: 529 overloaded.") is True
    assert stale._is_transient_throttle(SESSION_LIMIT_TEXT) is False


def test_is_rate_limited_matches_529_transcript(stale):
    _write_transcript(stale, "w529", API_529_TEXT)
    assert stale._is_rate_limited({"claude_sid": "w529"}) is True


def test_limit_banner_text_529_strict_detects_real_banner_rejects_quote(stale, tmp_path):
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
    malformed = [
        "[]",
        "123",
        "null",
        '"str"',
        json.dumps({"type": "assistant", "message": None}),
        json.dumps({"type": "assistant", "message": "oops"}),
        json.dumps({"type": "assistant",
                    "message": {"content": [{"type": "text", "text": None}]}}),
        json.dumps({"type": "assistant",
                    "message": {"content": [{"type": "text", "text": 42}]}}),
    ]
    log = tmp_path / "t.jsonl"
    log.write_text("\n".join(malformed) + "\n")
    assert stale._last_assistant_text(log) is None

    log.write_text("\n".join(malformed + [_assistant_line("real text")]) + "\n")
    assert stale._last_assistant_text(log) == "real text"


def test_is_rate_limited_never_raises(stale, monkeypatch):
    _write_transcript(stale, "w1", THROTTLE_TEXT)

    def boom(log_path, max_bytes=65536):
        raise RuntimeError("poison transcript")

    monkeypatch.setattr(stale, "_last_assistant_text", boom)
    assert stale._is_rate_limited({"claude_sid": "w1"}) is False


@pytest.fixture
def nudgy(stale, monkeypatch):
    monkeypatch.setattr(stale, "AUTONUDGE", True)
    return stale


def _capture_runs(stale, monkeypatch):
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
    return [(c[1], c[2]) for c in calls if c[0] == "send_text"]


def test_autonudge_env_gate(monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_AUTONUDGE", raising=False)
    assert _load_stale_monitor().AUTONUDGE is False
    monkeypatch.setenv("CLAUDE_ORCH_AUTONUDGE", "1")
    assert _load_stale_monitor().AUTONUDGE is True
    monkeypatch.setenv("CLAUDE_ORCH_AUTONUDGE", "true")
    assert _load_stale_monitor().AUTONUDGE is False, "gate is the literal string '1'"


def test_autonudge_off_means_no_send_even_when_throttled(stale, capsys, monkeypatch):
    calls = _capture_runs(stale, monkeypatch)
    now = int(time.time())
    path = _write_record(stale, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    old = now - 2700
    os.utime(path, (old, old))
    log = _write_transcript(stale, "w1", THROTTLE_TEXT)
    os.utime(log, (old, old))
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
    old = now - 2700
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
    from dockwright.mcp_server import MANAGER_MARKER
    assert stale.NUDGE_TEXT.startswith(MANAGER_MARKER)
    assert not stale.MANAGER_NUDGE_TEXT.startswith(MANAGER_MARKER), (
        "the manager nudge types into a MANAGER pane. It stays unmarked because "
        "manager.core.md quotes its text verbatim, which identifies it more "
        "precisely than a marker would. Peer messages ARE marked — see "
        "send_manager_to_manager_impl — so re-decide this before changing it")


def test_autonudge_repeats_at_each_threshold_crossing_while_wedged(nudgy, capsys, monkeypatch):
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
    assert "NUDGED" not in run_at(31)
    assert "NUDGED worker-tab (60min)" in run_at(60)
    assert "NUDGED worker-tab (120min)" in run_at(120)
    assert "NUDGED worker-tab (180min)" in run_at(180)
    assert "NUDGED" not in run_at(200)
    assert "NUDGED worker-tab (240min)" in run_at(240)
    assert len(_send_text_calls(calls)) == 5
    os.utime(log, (t0 + 241 * 60, t0 + 241 * 60))
    out = run_at(242)
    assert "NUDGED" not in out
    assert "RESUMED worker-tab" in out
    assert len(_send_text_calls(calls)) == 5
    assert "NUDGED worker-tab (30min)" in run_at(241 + 30)
    assert len(_send_text_calls(calls)) == 6


@pytest.mark.parametrize("banner", [THROTTLE_TEXT, SESSION_LIMIT_TEXT])
def test_autonudge_429_fires_before_threshold(nudgy, capsys, monkeypatch, banner):
    calls = _capture_runs(nudgy, monkeypatch)
    now = int(time.time())
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    old = now - 360
    os.utime(path, (old, old))
    log = _write_transcript(nudgy, "w1", banner)
    os.utime(log, (old, old))
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
    old = now - 120
    os.utime(path, (old, old))
    log = _write_transcript(nudgy, "w1", THROTTLE_TEXT)
    os.utime(log, (old, old))
    nudgy.main()
    out = capsys.readouterr().out
    assert "NUDGED" not in out
    assert "STALE_PROCESSING" not in out
    assert _send_text_calls(calls) == []


def test_429_renudges_on_new_stretch_while_still_throttled(nudgy, capsys, monkeypatch):
    calls = _capture_runs(nudgy, monkeypatch)
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    log = _write_transcript(nudgy, "w1", THROTTLE_TEXT)
    t0 = 1_000_000
    os.utime(path, (t0, t0))
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 6 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main()
    assert "NUDGED worker-tab (6min rate-limited)" in capsys.readouterr().out

    clock["now"] = t0 + 7 * 60
    nudgy.main()
    assert "NUDGED" not in capsys.readouterr().out
    assert len(_send_text_calls(calls)) == 1

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
    calls = _capture_runs(nudgy, monkeypatch)
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    t0 = 1_000_000
    os.utime(path, (t0, t0))
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main()
    assert "NUDGED worker-tab" in capsys.readouterr().out

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

    _write_record(nudgy, "w1", agent="worker", state="processing",
                  name="worker-tab", window_id="42")
    t1 = t0 + 60 * 60
    os.utime(nudgy.ACTIVE / "w1.json", (t1, t1))
    clock["now"] = t1 + 30 * 60
    nudgy.main()
    assert "NUDGED worker-tab" in capsys.readouterr().out
    assert len(_send_text_calls(calls)) == 2


def test_no_nudge_with_pending_question(nudgy, capsys, monkeypatch):
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
                         name="worker-tab")
    old = now - 2700
    os.utime(path, (old, old))
    nudgy.main()
    out = capsys.readouterr().out
    assert "STALE_PROCESSING worker-tab" in out
    assert "NUDGED" not in out
    assert _send_text_calls(calls) == []


def test_no_nudge_for_legacy_iterm_sid_only_records(nudgy, capsys, monkeypatch):
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
    calls = _capture_runs(nudgy, monkeypatch)
    now = int(time.time())
    bad = _write_record(nudgy, "bad", agent="worker", state="processing",
                        name="bad-tab", window_id="41")
    os.utime(bad, (now - 360, now - 360))
    bad_log = _write_transcript(nudgy, "bad", THROTTLE_TEXT)
    os.utime(bad_log, (now - 360, now - 360))
    healthy = _write_record(nudgy, "ok", agent="worker", state="processing",
                            name="ok-tab", window_id="42")
    os.utime(healthy, (now - 2700, now - 2700))

    def boom(log_path, max_bytes=65536):
        raise RuntimeError("poison transcript")

    monkeypatch.setattr(nudgy, "_last_assistant_text", boom)
    rc = nudgy.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "NUDGED ok-tab" in out, "healthy worker must still be handled"
    assert "bad-tab" not in out, "poison worker is silently skipped this scan"


def test_manager_records_never_nudged(nudgy, capsys, monkeypatch):
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


def _write_codex_transcript(stale, sid, mtime=None):
    day_dir = stale.CODEX_SESSIONS / "2026" / "06" / "10"
    day_dir.mkdir(parents=True, exist_ok=True)
    log = day_dir / f"rollout-2026-06-10T01-02-03-{sid}.jsonl"
    log.write_text('{"type":"response_item"}\n')
    if mtime is not None:
        os.utime(log, (mtime, mtime))
    return log


def test_busy_long_turn_with_fresh_transcript_is_not_stale(stale, capsys):
    now = int(time.time())
    path = _write_record(stale, "w1", agent="worker", state="processing", name="worker-tab")
    os.utime(path, (now - 2700, now - 2700))
    _write_transcript(stale, "w1", "still working")
    rc = stale.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "STALE_PROCESSING" not in out
    assert "NUDGED" not in out


def test_busy_long_turn_with_fresh_transcript_is_not_nudged(nudgy, capsys, monkeypatch):
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
    def boom(sid):
        raise RuntimeError("poison resolver")
    monkeypatch.setattr(stale, "_find_claude_session_log", boom)
    assert stale._last_activity_mtime({"claude_sid": "w1"}, 12345) == 12345


def test_highest_nudge_threshold_cadence(stale):
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

    os.utime(log, (t0 + 31 * 60, t0 + 31 * 60))
    clock["now"] = t0 + 32 * 60
    nudgy.main()
    assert "RESUMED worker-tab" in capsys.readouterr().out
    clock["now"] = t0 + 33 * 60
    nudgy.main()
    out = capsys.readouterr().out
    assert "RESUMED" not in out, "delivery confirmation is one-shot"
    assert "NUDGED" not in out


from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def test_parse_limit_reset_ts_valid_variants(stale):
    for text, h24, minute, tzname, now_dt in [
        ("You’ve hit your session limit · resets 2:20am (Etc/GMT-9)",
         2, 20, "Etc/GMT-9", datetime(2026, 6, 11, 1, 0, tzinfo=ZoneInfo("Etc/GMT-9"))),
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
        "resets 2:20am (Mars/Olympus_Mons)",
        "resets 25:99pm (UTC)",
        "resets sometime (UTC)",
        "resets 2:20am",
        "",
    ]:
        assert stale._parse_limit_reset_ts(bad, t0) is None, bad
    assert stale._parse_limit_reset_ts(None, t0) is None


def test_session_limit_banner_schedules_worker_nudge(nudgy, capsys, monkeypatch):
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

    clock["now"] = sched["at"] - 60
    nudgy.main()
    assert "(limit-reset)" not in capsys.readouterr().out
    sends_before_due = len(_send_text_calls(calls))

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

    t1 = t0 + 7 * 60
    os.utime(path, (t1, t1))
    _append_transcript(nudgy, "w1", SESSION_LIMIT_TEXT, t1)
    clock["now"] = t1 + 60
    nudgy.main()
    assert "RESUMED worker-tab" in capsys.readouterr().out

    clock["now"] = t1 + 6 * 60
    nudgy.main()
    out = capsys.readouterr().out
    assert "NUDGED" not in out, "5-min lane must be suppressed while the schedule is armed"
    assert len(_send_text_calls(calls)) == 1

    clock["now"] = t1 + 30 * 60
    nudgy.main()
    assert "NUDGED worker-tab (30min)" in capsys.readouterr().out
    assert len(_send_text_calls(calls)) == 2


def test_scheduled_nudge_fires_at_reset_despite_failed_retry_growth(nudgy, capsys, monkeypatch):
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

    clock["now"] = sched["at"]
    nudgy.main(manager_name="mgr-A")
    sends = _send_text_calls(calls)
    assert len(sends) == 1
    window_id, text = sends[0]
    assert window_id == "9"
    assert text == MANAGER_NUDGE
    sched2 = json.loads(emitted_path.read_text())["scheduled:mgr1"]
    assert sched2["at"] == clock["now"] + 600, "swallowed fire re-arms at flat 10min"

    clock["now"] = sched2["at"]
    nudgy.main(manager_name="mgr-A")
    assert len(_send_text_calls(calls)) == 2


def test_limited_manager_parse_failure_falls_back_to_flat_retry(nudgy, capsys, monkeypatch):
    _capture_runs(nudgy, monkeypatch)
    t0 = 1_000_000
    path = _write_record(nudgy, "mgr1", agent="manager", state="processing",
                         name="mgr-A", window_id="9")
    os.utime(path, (t0, t0))
    log = _write_transcript(nudgy, "mgr1", THROTTLE_TEXT)
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main(manager_name="mgr-A")
    sched = json.loads(nudgy._emitted_state_path("mgr-A").read_text())["scheduled:mgr1"]
    assert sched["at"] == clock["now"] + 600


def test_manager_limited_coalesces_events_then_rolls_up(nudgy, capsys, monkeypatch):
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

    done_dir = nudgy.ROOT / "done" / "mgr-A"
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / "e1.json").write_text("{}")

    mlog2 = _write_transcript(nudgy, "mgr1", "back to work")
    os.utime(mlog2, (t0 + 31 * 60, t0 + 31 * 60))
    clock["now"] = t0 + 31 * 60
    nudgy.main(manager_name="mgr-A")
    out = capsys.readouterr().out
    assert "limit cleared" in out
    assert "while down: 1 workers stalled, 1 nudged, 1 done events" in out
    assert not (nudgy.ROOT / ".manager-limited-mgr-A").exists()
    assert "limited_buffer" not in json.loads(nudgy._emitted_state_path("mgr-A").read_text())

    clock["now"] = t0 + 60 * 60
    nudgy.main(manager_name="mgr-A")
    assert "NUDGED worker-tab (60min)" in capsys.readouterr().out


def test_healthy_manager_never_suppresses(nudgy, capsys, monkeypatch):
    _capture_runs(nudgy, monkeypatch)
    now = int(time.time())
    mpath = _write_record(nudgy, "mgr1", agent="manager", state="processing",
                          name="mgr-A", window_id="9")
    os.utime(mpath, (now - 3600, now - 3600))
    _write_transcript(nudgy, "mgr1", "actively orchestrating")
    w = _write_record(nudgy, "w1", agent="worker", state="processing",
                      name="worker-tab", window_id="42", parent_manager_name="mgr-A")
    os.utime(w, (now - 2700, now - 2700))
    nudgy.main(manager_name="mgr-A")
    out = capsys.readouterr().out
    assert "NUDGED worker-tab (45min)" in out
    assert not (nudgy.ROOT / ".manager-limited-mgr-A").exists()


def test_manager_never_gets_ladder_or_stale_processing(nudgy, capsys, monkeypatch):
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
    _capture_runs(nudgy, monkeypatch)
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

    t1 = t0 + 7 * 60
    os.utime(path, (t1, t1))
    os.utime(log, (t1, t1))
    clock["now"] = t1 + 60
    nudgy.main()
    capsys.readouterr()

    clock["now"] = t1 + 120
    nudgy.main()
    assert "scheduled:w1" in json.loads(nudgy.EMITTED_STATE.read_text())


def test_recovery_rollup_survives_poisoned_buffer(nudgy, capsys, monkeypatch):
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
    _capture_runs(nudgy, monkeypatch)
    t0 = 1_000_000
    mpath = _write_record(nudgy, "mgr1", agent="manager", state="processing",
                          name="mgr-A", window_id="9")
    os.utime(mpath, (t0, t0))
    quoting = ("Worker alpha is stuck: its transcript ends with 'You've hit your "
               "session limit · resets 2:20am (Etc/GMT-9)' — I'll nudge it "
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
    calls = _capture_runs(nudgy, monkeypatch)
    t0 = 1_000_000
    mpath = _write_record(nudgy, "mgr1", agent="manager", state="processing",
                          name="mgr-A", window_id="9")
    os.utime(mpath, (t0, t0))
    relay = "worker-1: You’ve hit your session limit · resets 2:20am (Etc/GMT-9)"
    log = _write_transcript(nudgy, "mgr1", relay)
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main(manager_name="mgr-A")
    assert not (nudgy.ROOT / ".manager-limited-mgr-A").exists()
    assert "scheduled:mgr1" not in json.loads(nudgy._emitted_state_path("mgr-A").read_text())
    assert _send_text_calls(calls) == []


def test_limited_flag_mtime_refreshed_while_limited(nudgy, capsys, monkeypatch):
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
    t0 = 1_000_000
    _write_limited_manager(stale, "mgr1", "mgr-A", t0)
    w = _write_record(stale, "w1", agent="worker", state="processing",
                      name="worker-tab", parent_manager_name="mgr-A")
    os.utime(w, (t0, t0))
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
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


def test_autoclose_resolves_transcript_path_when_record_lacks_it(monkeypatch, tmp_path):
    sm = _load_stale_monitor()
    monkeypatch.setattr(sm, "CLOSED", tmp_path / "closed")
    (tmp_path / "closed").mkdir()
    monkeypatch.setattr(sm, "_close_window", lambda wid: None)
    log = tmp_path / "t.jsonl"
    log.write_text("{}\n")
    monkeypatch.setattr(sm, "_resolve_transcript_path", lambda record: log)
    record = {"claude_sid": "sid-x", "name": "w", "pid": 1,
              "window_id": "w1", "spend": None}
    record_path = tmp_path / "active.json"
    record_path.write_text(json.dumps(record))
    monkeypatch.setattr(sm, "_process_index", _IDLE_PROC_INDEX)
    sm._autoclose_idle_worker(record_path, record, 7300)
    closed = json.loads((tmp_path / "closed" / "sid-x.json").read_text())
    assert closed["transcript_path"] == str(log)


def test_autoclose_transcript_path_none_when_unresolvable(monkeypatch, tmp_path):
    sm = _load_stale_monitor()
    monkeypatch.setattr(sm, "CLOSED", tmp_path / "closed")
    (tmp_path / "closed").mkdir()
    monkeypatch.setattr(sm, "_close_window", lambda wid: None)
    monkeypatch.setattr(sm, "_resolve_transcript_path", lambda record: None)
    record = {"claude_sid": "sid-y", "name": "w", "pid": 1,
              "window_id": "w1", "spend": None}
    record_path = tmp_path / "active.json"
    record_path.write_text(json.dumps(record))
    monkeypatch.setattr(sm, "_process_index", _IDLE_PROC_INDEX)
    sm._autoclose_idle_worker(record_path, record, 7300)
    closed = json.loads((tmp_path / "closed" / "sid-y.json").read_text())
    assert closed["transcript_path"] is None


def test_autoclose_survives_transcript_resolve_raising(monkeypatch, tmp_path):
    sm = _load_stale_monitor()
    monkeypatch.setattr(sm, "CLOSED", tmp_path / "closed")
    (tmp_path / "closed").mkdir()
    monkeypatch.setattr(sm, "_close_window", lambda wid: None)

    def _boom(record):
        raise OSError("projects dir vanished mid-scan")

    monkeypatch.setattr(sm, "_resolve_transcript_path", _boom)
    record = {"claude_sid": "sid-z", "name": "w", "pid": 1,
              "window_id": "w1", "spend": None}
    record_path = tmp_path / "active.json"
    record_path.write_text(json.dumps(record))
    monkeypatch.setattr(sm, "_process_index", _IDLE_PROC_INDEX)
    sm._autoclose_idle_worker(record_path, record, 7300)
    closed = json.loads((tmp_path / "closed" / "sid-z.json").read_text())
    assert closed["transcript_path"] is None
    assert not record_path.exists()


def _arm_pool(stale, letter="a"):
    stale.ACCOUNT_ACTIVE.write_text(f"{letter}\n")


def _seed_farm(monkeypatch, home, letter, *, healthy=True):
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


def test_login_fix_command_exact_per_account(stale, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    stale.ACCOUNT_REGISTRY.write_text(json.dumps({
        "pool": [{"name": "a"}, {"name": "b"},
                 {"name": "c", "config_dir": str(tmp_path / "farm c")}],
        "default": "a"}))
    assert stale._login_fix_command("a") == "claude"
    assert stale._login_fix_command("b") == f"CLAUDE_CONFIG_DIR={tmp_path}/.claude-b claude"
    assert stale._login_fix_command("c") == f"CLAUDE_CONFIG_DIR='{tmp_path}/farm c' claude"


def test_pool_account_reads_pointer(stale):
    assert stale._pool_account() is None
    _arm_pool(stale, "a"); assert stale._pool_account() == "a"
    stale.ACCOUNT_ACTIVE.write_text("z");  assert stale._pool_account() is None
    stale.ACCOUNT_ACTIVE.write_text("b\n"); assert stale._pool_account() == "b"
    stale.ACCOUNT_ACTIVE.write_text(" b \n"); assert stale._pool_account() is None


def test_account_config_prefix_accepts_both_generation_mcp_keys(stale, monkeypatch, tmp_path, capsys):
    farm = _seed_farm(monkeypatch, tmp_path, "b")
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
    stale._record_brick("a", now + 3600, "manager:mgr-A", now + 60)
    assert len(_ledger_events(stale)) == 1
    state = json.loads(stale.ACCOUNT_STATE.read_text())
    assert state["accounts"]["a"]["last_seen"] == now + 60
    stale._record_brick("a", None, "worker:w1", now + 7200)
    assert len(_ledger_events(stale)) == 2
    stale._record_brick("b", now + 4 * 3600, "worker:w2", now)
    stale._record_brick("b", now + 4 * 3600, "worker:w2", now + 1200)
    b_events = [e for e in _ledger_events(stale) if e["account"] == "b"]
    assert len(b_events) == 2


def test_maybe_flip_guards(stale, monkeypatch):
    now = 1_000_000
    monkeypatch.setattr(stale, "_keychain_unlocked",
                        lambda: pytest.fail("keychain probed while pool off"))
    assert stale._maybe_flip_account("a", "r", now) is None
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "b")
    assert stale._maybe_flip_account("a", "r", now) is None
    _arm_pool(stale, "a")
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: False)
    assert stale._maybe_flip_account("a", "r", now) is None
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    stale._record_brick("b", now + 3000, "worker:w9", now - 10)
    assert stale._maybe_flip_account("a", "r", now) is None
    stale._record_brick("b", now - 5, "worker:w9", now - 10)
    assert stale._maybe_flip_account("a", "manager mgr-A limited", now) == "b"
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"
    flips = [e for e in _ledger_events(stale) if e["event"] == "flip"]
    assert flips == [{"event": "flip", "ts": now, "from": "a", "to": "b",
                      "reason": "manager mgr-A limited", "by": "stale_monitor"}]
    stale._record_brick("b", None, "worker:w1", now + 5)
    assert stale._maybe_flip_account("b", "r2", now + 10) is None


def test_other_bricked_unparsed_window(stale):
    now = 1_000_000
    stale._record_brick("b", None, "w", now)
    state = json.loads(stale.ACCOUNT_STATE.read_text())
    assert stale._other_account_bricked(state, "b", now + 3600) is True
    assert stale._other_account_bricked(state, "b", now + 7 * 3600) is False


def _write_registry(stale, names, default=None):
    stale.ACCOUNT_REGISTRY.write_text(json.dumps({
        "version": 1, "default": default or names[0],
        "pool": [{"name": n, "config_dir": None} for n in names]}))


def test_registry_fallback_is_legacy_pair(stale):
    assert stale._registry() == (["a", "b"], "a", {})


def test_registry_rejects_dup_or_empty_names(stale):
    stale.ACCOUNT_REGISTRY.write_text(json.dumps({
        "version": 1, "default": "x",
        "pool": [{"name": "x", "config_dir": None}, {"name": "x", "config_dir": None}]}))
    assert stale._registry() == (["a", "b"], "a", {})
    stale.ACCOUNT_REGISTRY.write_text(json.dumps({
        "version": 1, "default": "x",
        "pool": [{"name": "x", "config_dir": None}, {"name": "", "config_dir": None}]}))
    assert stale._registry() == (["a", "b"], "a", {})
    stale.ACCOUNT_REGISTRY.write_text("{not json")
    assert stale._registry() == (["a", "b"], "a", {})


def test_pool_account_accepts_registry_names(stale):
    _write_registry(stale, ["main", "alt"])
    _arm_pool(stale, "main")
    assert stale._pool_account() == "main"


def test_solo_flip_is_honest_noop(stale, monkeypatch):
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _write_registry(stale, ["a"])
    _arm_pool(stale, "a")
    now = 1_000_000
    assert stale._maybe_flip_account("a", "worker w limited", now) is None
    assert stale.ACCOUNT_ACTIVE.read_text().rstrip("\n") == "a"
    events = _ledger_events(stale)
    assert [e["event"] for e in events] == ["flip-skip"]
    assert events[0]["reason"] == "no other account in registry"
    assert events[0]["account"] == "a"
    assert stale._maybe_flip_account("a", "worker w limited", now + 60) is None
    assert len(_ledger_events(stale)) == 1


def test_custom_name_flip_prefers_pool_order_and_skips_bricked(stale, monkeypatch):
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _write_registry(stale, ["main", "alt", "third"])
    _arm_pool(stale, "main")
    now = 1_000_000
    stale.ACCOUNT_STATE.write_text(json.dumps(
        {"accounts": {"alt": {"bricked_at": now - 60, "last_seen": now - 60}}}))
    assert stale._maybe_flip_account("main", "worker w limited", now) == "third"
    assert stale.ACCOUNT_ACTIVE.read_text().rstrip("\n") == "third"
    flips = [e for e in _ledger_events(stale) if e["event"] == "flip"]
    assert flips and flips[-1]["from"] == "main" and flips[-1]["to"] == "third"


def test_snapshot_roundtrip_matches_config(stale, tmp_path, monkeypatch):
    from dockwright import config as pkg_config, paths as pkg_paths, spawner
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text('[accounts]\ndefault = "main"\n'
                   '[[accounts.pool]]\nname = "main"\n'
                   '[[accounts.pool]]\nname = "alt"\n')
    monkeypatch.setenv(pkg_config.ENV_CONFIG_PATH, str(cfg))
    monkeypatch.setattr(pkg_paths, "ACCOUNT_REGISTRY", tmp_path / "account-registry.json")
    spawner.write_registry_snapshot()
    assert stale._registry() == (["main", "alt"], "main", {})


def test_keychain_unlocked_probes_show_keychain_info_only(monkeypatch):
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


def _flips(stale):
    return [e for e in _ledger_events(stale) if e["event"] == "flip"]


def _launch_calls(calls):
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

    clock["now"] = t_flip + 120
    stale.main(manager_name="mgr-A")
    assert len(_launch_calls(calls)) == 1

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

    clock["now"] = t_flip + 2 * stale.TAKEOVER_GUARD_SEC + 240
    stale.main(manager_name="mgr-A")
    assert len(_launch_calls(calls)) == 2


def test_limited_manager_flip_refuses_401ing_target(stale, capsys, monkeypatch):
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    _write_limited_manager(stale, "mgr1", "mgr-A", t0)
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale._record_auth_401("b", "u-b-401", clock["now"] - 60)
    stale.main(manager_name="mgr-A")
    assert stale.ACCOUNT_ACTIVE.read_text() == "a\n", "no flip onto a 401ing account"
    assert _flips(stale) == []
    assert _launch_calls(calls) == []


def test_rate_limit_flip_unstalls_after_dead_worker_episode_ages_out(
        stale, capsys, monkeypatch):
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    w_path = _write_record(stale, "w1", agent="worker", state="processing",
                           name="worker-b", window_id="42", account="b")
    w_log = _write_auth_401_transcript(stale, "w1", uuid="u-dead")
    m_path, m_log = _write_limited_manager(stale, "mgr1", "mgr-A", t0)
    clock = {"now": t0}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])

    def scan(minute):
        clock["now"] = t0 + minute * 60
        for p in (w_path, w_log, m_path, m_log):
            os.utime(p, (clock["now"] - 31 * 60, clock["now"] - 31 * 60))
        stale.main()
        stale.main(manager_name="mgr-A")

    scan(31)
    assert _flips(stale) == [], "b mid-401: flip refused"
    refusals = [e for e in _ledger_events(stale) if e["event"] == "flip-refused-auth401"]
    assert len(refusals) == 1 and refusals[0]["excluded"] == ["b"], \
        "the refusal leaves a ledger trace (residual 3)"
    scan(32)
    scan(33)
    assert _flips(stale) == [], "still within b's distinct-401 window"
    scan(35)
    scan(38)
    scan(40)
    assert len(_flips(stale)) == 1, "episode aged out: the flip lane unstalls"
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"


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

    (stale.ACTIVE / "mgr1.json").unlink()
    clock["now"] += 60
    stale.main(manager_name="mgr-A")
    out = capsys.readouterr().out
    assert "limit cleared" in out
    assert ", switched account a→b (manager mgr-A limited)" in out
    emitted = json.loads(stale._emitted_state_path("mgr-A").read_text())
    assert "recovery:mgr1" not in emitted


def test_no_flip_when_pool_off_manager_site(nudgy, capsys, monkeypatch):
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
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    _write_limited_manager(stale, "mgr1", "mgr-A", t0, text=SESSION_LIMIT_NO_RESET)
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

    clock["now"] = t_launch + 120
    stale.main(manager_name="mgr-A")
    assert len(_launch_calls(calls)) == 1


def test_unstamped_manager_after_recent_flip_gets_recovery(stale, capsys, monkeypatch, tmp_path):
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
                                  "event": "recovery-launch", "from_sid": "m1"})
    stale._append_account_ledger({"ts": now - 30, "event": "flip", "from": "a", "to": "b"})
    assert stale._ledger_recovery_launches("m1", now) == 2
    assert stale._ledger_recovery_launches("other", now) == 1
    assert stale._ledger_recovery_launches("ghost", now) == 0


def test_ledger_recovery_launches_fail_open(stale):
    assert stale._ledger_recovery_launches("m1", 1_000_000) == 0, "no ledger file ⇒ 0"
    stale.ACCOUNT_LEDGER.write_text("not json\n{broken\n[]\n")
    assert stale._ledger_recovery_launches("m1", 1_000_000) == 0, "garbage lines ⇒ 0"


def test_launch_bound_holds_with_dead_emitted_state(stale, capsys, monkeypatch):
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

    clock["now"] = t0 + 30 * 60 + stale.TAKEOVER_GUARD_SEC + 120
    stale.main(manager_name="mgr-A")
    clock["now"] += 60
    stale.main(manager_name="mgr-A")
    assert len(_launch_calls(calls)) <= 2


def test_recovery_launch_gated_on_keychain_even_when_already_flipped(stale, capsys, monkeypatch):
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

    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    clock["now"] += 60
    stale.main(manager_name="mgr-A")
    assert len(_launch_calls(calls)) == 1


def test_relaunch_gated_on_keychain(stale, capsys, monkeypatch):
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

    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: False)
    clock["now"] = t_flip + stale.TAKEOVER_GUARD_SEC + 120
    stale.main(manager_name="mgr-A")
    assert len(_launch_calls(calls)) == 1
    emitted = json.loads(stale._emitted_state_path("mgr-A").read_text())
    assert emitted["recovery:mgr1"]["relaunched"] is False

    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    clock["now"] += 60
    stale.main(manager_name="mgr-A")
    assert len(_launch_calls(calls)) == 2
    emitted = json.loads(stale._emitted_state_path("mgr-A").read_text())
    assert emitted["recovery:mgr1"]["relaunched"] is True


def test_unstamped_manager_stale_flip_no_recovery(stale, capsys, monkeypatch):
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


def test_worker_banner_flips_and_emits_switched_live(stale, capsys, monkeypatch):
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
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
    assert "NUDGED" not in out


def test_worker_banner_past_processing_threshold_still_flips(stale, capsys, monkeypatch):
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
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
    _capture_runs(nudgy, monkeypatch)
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
    sched = json.loads(nudgy.EMITTED_STATE.read_text())["scheduled:w1"]
    assert _flips(nudgy) == []

    monkeypatch.setattr(nudgy, "_keychain_unlocked", lambda: True)
    _arm_pool(nudgy, "a")
    clock["now"] = t0 + 8 * 60
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
    assert "b" not in json.loads(stale.ACCOUNT_STATE.read_text())["accounts"]


def test_worker_unstamped_record_uses_pointer_stamped_uses_stamp(stale, capsys, monkeypatch):
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
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

    stamped.unlink()
    t1 = t0 + 7 * 3600
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
    now = int(time.time())
    record_path = _write_record(
        stale, "s1",
        pid=12345,
        iterm_sid="7",
        last_turn_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 200)),
    )
    log = _write_transcript(stale, "s1", "doing stuff")
    os.utime(log, (now - 10_000, now - 10_000))
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
    now = int(time.time())
    record_path = _write_record(
        stale, "s2",
        pid=12345,
        iterm_sid="7",
        last_turn_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 200)),
    )
    log = _write_transcript(stale, "s2", "doing stuff")
    os.utime(log, (now - 10_000, now - 10_000))
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
    now = int(time.time())
    record_path = _write_record(
        stale, "s3",
        pid=12345,
        iterm_sid="7",
        last_turn_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 200)),
    )
    log = _write_transcript(stale, "s3", "doing stuff")
    os.utime(log, (now - 50, now - 50))
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
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    path = _write_record(stale, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    os.utime(path, (t0, t0))
    log = _write_transcript(stale, "w1", SESSION_LIMIT_NO_RESET)
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


def test_is_auth_401_event_matches_stable_signal_and_text_fallback(stale):
    assert stale._is_auth_401_event(_auth_401_event(text=AUTH_401_INCIDENT_TEXT)) is True
    assert stale._is_auth_401_event(_auth_401_event(text=AUTH_401_BEARER_TEXT)) is True
    assert stale._is_auth_401_event(
        _auth_401_event(text="totally new wording", uuid="u")) is True
    assert stale._is_auth_401_event(
        _auth_401_event(text="totally new wording", uuid="u", api_error_status="401")) is True
    assert stale._is_auth_401_event(
        _auth_401_event(text=AUTH_401_INCIDENT_TEXT, api_error_status=None)) is True
    assert stale._is_auth_401_event(
        _auth_401_event(text="Please run /login now", api_error_status=None)) is True


def test_is_auth_401_event_negative_cases(stale):
    assert stale._is_auth_401_event(
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": "All done, opening the PR"}]}}) is False
    assert stale._is_auth_401_event(
        {"type": "assistant", "isApiErrorMessage": True, "apiErrorStatus": 429,
         "message": {"content": [{"type": "text", "text": SESSION_LIMIT_TEXT}]}}) is False
    assert stale._is_auth_401_event(
        _auth_401_event(text="API Error: 500 server error", api_error_status=500, uuid="u")) is False
    assert stale._is_auth_401_event(
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": "discussing API Error: 401 in passing"}]}}) is False
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
    log.write_text(_assistant_line("All done, opening the PR") + "\n")
    assert stale._auth_failure_signature(log) is None
    assert stale._auth_failure_signature(tmp_path / "absent.jsonl") is None


def test_auth_failure_signature_only_last_assistant_matters(stale, tmp_path):
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


def test_record_auth_401_bounded_attempts_and_dedup(stale):
    now = 1_000_000
    assert stale._record_auth_401("a", "u-1", now) == ("recover", 1)
    assert stale._record_auth_401("a", "u-1", now + 30) == ("duplicate", 1)
    assert stale._record_auth_401("a", "u-2", now + 60) == ("recover", 2)
    assert stale._record_auth_401("a", "u-3", now + 120) == ("escalate", 3)


def test_record_auth_401_window_resets(stale):
    now = 1_000_000
    assert stale._record_auth_401("a", "u-1", now) == ("recover", 1)
    last_seen = now + 60
    assert stale._record_auth_401("a", "u-2", last_seen) == ("recover", 2)
    later = last_seen + stale.AUTH_401_WINDOW_SEC + 1
    assert stale._record_auth_401("a", "u-3", later) == ("recover", 1)
    assert stale._record_auth_401("a", "u-4", later + 60) == ("recover", 2)
    assert stale.AUTH_401_WINDOW_SEC == 300, \
        "absolute pin: a widened window silently extends cross-incident suspicion"


def test_record_auth_401_per_account_independent(stale):
    now = 1_000_000
    assert stale._record_auth_401("a", "u-1", now) == ("recover", 1)
    assert stale._record_auth_401("b", "u-2", now) == ("recover", 1)
    assert stale._record_auth_401("a", "u-3", now + 10) == ("recover", 2)
    assert stale._record_auth_401("a", "u-4", now + 20) == ("escalate", 3)
    assert stale._record_auth_401("b", "u-5", now + 20) == ("recover", 2)


def test_healthy_takeover_target(stale):
    assert stale._healthy_takeover_target("a", "a", "b") == "b"
    assert stale._healthy_takeover_target("a", "b", None) == "b"
    assert stale._healthy_takeover_target("b", "a", None) == "a"
    assert stale._healthy_takeover_target("a", "a", None) is None
    assert stale._healthy_takeover_target("a", "b", "c") == "c"
    assert stale._healthy_takeover_target("a", "b", None, pool_suspect=True) is None, \
        "a pointer that is itself mid-401 is not a healthy target"
    assert stale._healthy_takeover_target("a", "a", "c", pool_suspect=True) == "c", \
        "the flipped letter arm is _flip_target-vetted; pool_suspect does not gate it"


def test_auth_401_active(stale):
    now = 1_000_000
    assert stale._auth_401_active("b", now) is False, "no state file ⇒ not active"
    stale._record_auth_401("b", "u-b", now - 60)
    assert stale._auth_401_active("b", now) is True, "in-window episode ⇒ active"
    assert stale._auth_401_active("a", now) is False, "other account unaffected"
    assert stale._auth_401_active(
        "b", now + stale.AUTH_401_WINDOW_SEC + 61) is False, "expired episode ⇒ inactive"


def test_auth_401_active_ages_out_despite_duplicate_reports(stale):
    t0 = 1_000_000
    assert stale._record_auth_401("b", "u-dead", t0) == ("recover", 1)
    for minute in range(1, 11):
        assert stale._record_auth_401("b", "u-dead", t0 + minute * 60)[0] == "duplicate"
    late = t0 + 10 * 60
    assert stale._auth_401_active("b", late) is False, \
        "liveness keys on last_distinct: aged out despite fresh last_seen"
    assert stale._record_auth_401("b", "u-new", late + 1) == ("recover", 2)
    assert stale._auth_401_active("b", late + 2) is True, \
        "a fresh distinct 401 re-arms liveness"


def test_flip_target_excludes_auth_401_active_account(stale):
    now = 1_000_000
    stale._record_auth_401("b", "u-b", now - 30)
    state = stale._load_account_state()
    assert stale._flip_target("a", state, now) is None, \
        "an account with a live 401 episode is not a flip destination"
    later = now + stale.AUTH_401_WINDOW_SEC + 31
    assert stale._flip_target("a", state, later) == "b", \
        "an expired episode no longer disqualifies"


def test_flip_refusal_auth401_writes_throttled_ledger_line(stale, monkeypatch):
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    now = 1_000_000
    stale._record_auth_401("b", "u-b", now - 30)
    assert stale._maybe_flip_account("a", "test refusal", now) is None
    refusals = [e for e in _ledger_events(stale) if e["event"] == "flip-refused-auth401"]
    assert len(refusals) == 1
    assert refusals[0]["from"] == "a" and refusals[0]["excluded"] == ["b"]
    assert stale._maybe_flip_account("a", "test refusal", now + 60) is None
    assert len([e for e in _ledger_events(stale)
                if e["event"] == "flip-refused-auth401"]) == 1


def test_notify_macos_shape_and_pytest_guard(stale, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(stale.subprocess, "run",
                        lambda argv, **kw: calls.append((argv, kw)) or None)
    stale._notify_macos('hello "quoted" world')
    assert calls == [], "no-op under pytest (PYTEST_CURRENT_TEST guard)"
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    stale._notify_macos('hello "quoted" world')
    assert len(calls) == 1
    assert calls[0][0][0] == "osascript"
    assert 'hello quoted world' in calls[0][0][2], "double quotes stripped"
    assert calls[0][1].get("timeout") == 2
    assert calls[0][1].get("check") is False
    assert calls[0][1].get("capture_output") is True
    monkeypatch.setattr(stale.subprocess, "run",
                        lambda argv, **kw: (_ for _ in ()).throw(OSError("boom")))
    stale._notify_macos("x")
    err = capsys.readouterr().err
    assert "notify" in err and "boom" in err, "failure leaves a stderr trace"
    class _R:
        returncode = 1
        stderr = b"not allowed"
    monkeypatch.setattr(stale.subprocess, "run", lambda argv, **kw: _R())
    stale._notify_macos("x")
    assert "notify" in capsys.readouterr().err


def test_worker_auth_401_recovers_same_account_no_flip(stale, capsys, monkeypatch):
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
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
    assert "AUTH_401 worker-tab" in out
    assert "SWITCHED" not in out and "AUTH_401_ESCALATED" not in out
    assert stale.ACCOUNT_ACTIVE.read_text() == "a\n"
    assert _flips(stale) == []
    auth = _auth_events(stale)
    assert len(auth) == 1
    assert auth[0]["action"] == "recover" and auth[0]["account"] == "a"
    assert auth[0]["source"] == "worker:worker-tab"


def test_worker_auth_401_does_not_re_emit_on_same_401(stale, capsys, monkeypatch):
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
    assert len(_auth_events(stale)) == 1
    assert _flips(stale) == []


def test_worker_auth_401_reemits_trigger_after_cadence(stale, capsys, monkeypatch):
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

    assert "AUTH_401 worker-tab" in scan(6)
    assert "AUTH_401" not in scan(7)
    assert "AUTH_401 worker-tab" in scan(6 + 6)
    assert [e["action"] for e in _auth_events(stale)] == ["recover"]
    assert _flips(stale) == []


def test_worker_auth_401_escalates_after_bound(stale, capsys, monkeypatch):
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
        log = _write_auth_401_transcript(stale, "w1", uuid=uuid)
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
    assert "AUTH_401_ESCALATED" in out3 and "PAGE: run claude, then /login" in out3
    assert "CLAUDE_CONFIG_DIR" not in out3
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"
    assert len(_flips(stale)) == 1
    actions = [e["action"] for e in _auth_events(stale)]
    assert actions == ["recover", "recover", "escalate"]


def test_worker_auth_401_dormant_when_pool_off(stale, capsys, monkeypatch):
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
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    _write_auth_401_manager(stale, "mgr1", "mgr-A", t0, uuid="u-1")
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    assert stale.ACCOUNT_ACTIVE.read_text() == "a\n"
    assert _flips(stale) == []
    launches = _launch_calls(calls)
    assert len(launches) == 1
    inner = launches[0][0][-1]
    assert "/manager-takeover-recovery mgr1" in inner
    assert "CLAUDE_ORCH_ACCOUNT=a" in inner
    auth = _auth_events(stale)
    assert len(auth) == 1 and auth[0]["action"] == "recover"
    launch_events = [e for e in _ledger_events(stale) if e["event"] == "recovery-launch"]
    assert len(launch_events) == 1 and launch_events[0]["from_sid"] == "mgr1"
    clock["now"] += 60
    stale.main(manager_name="mgr-A")
    assert len(_launch_calls(calls)) == 1


def test_manager_auth_401_second_attempt_no_healthy_target_escalates_now(
        stale, capsys, monkeypatch, tmp_path):
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _seed_farm(monkeypatch, tmp_path, "b")
    _arm_pool(stale, "a")
    t0 = 1_000_000
    stale._record_auth_401("a", "u-prev", t0)
    path, log = _write_auth_401_manager(stale, "mgr1", "mgr-A", t0, uuid="u-fresh",
                                        account="a")
    clock = {"now": t0 + 130}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n", "promoted escalate flips off the suspect account"
    assert len(_flips(stale)) == 1
    launches = _launch_calls(calls)
    assert len(launches) == 1, "exactly one launch — and not on the suspect account"
    inner = launches[0][0][-1]
    assert "/manager-takeover-recovery mgr1" in inner
    assert "CLAUDE_ORCH_ACCOUNT=b" in inner
    assert f"CLAUDE_CONFIG_DIR={tmp_path}/.claude-b" in inner
    assert "CLAUDE_ORCH_ACCOUNT=a" not in inner
    assert [e["action"] for e in _auth_events(stale)] == ["escalate"], \
        "the recover decision is PROMOTED — no recover ledger line"
    clock["now"] += 60
    os.utime(log, (clock["now"] - 130, clock["now"] - 130))
    os.utime(path, (clock["now"] - 130, clock["now"] - 130))
    stale.main(manager_name="mgr-A")
    assert len(_launch_calls(calls)) == 1
    assert len(_flips(stale)) == 1
    assert [e["action"] for e in _auth_events(stale)] == ["escalate"]


def test_manager_auth_401_promoted_escalate_never_flips_to_401ing_account(
        stale, capsys, monkeypatch, tmp_path):
    calls = _capture_runs(stale, monkeypatch)
    notified = []
    monkeypatch.setattr(stale, "_notify_macos", lambda text: notified.append(text))
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _seed_farm(monkeypatch, tmp_path, "b")
    _arm_pool(stale, "a")
    t0 = 1_000_000
    stale._record_auth_401("b", "u-b-401", t0)
    stale._record_auth_401("a", "u-prev", t0)
    _write_auth_401_manager(stale, "mgr1", "mgr-A", t0, uuid="u-fresh",
                            account="a")
    clock = {"now": t0 + 130}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    assert stale.ACCOUNT_ACTIVE.read_text() == "a\n", "no flip onto a 401ing account"
    assert _flips(stale) == []
    assert _launch_calls(calls) == [], "no takeover launch anywhere — b is mid-401"
    assert [e["action"] for e in _auth_events(stale)
            if e["account"] == "a"] == ["escalate"]
    assert len(notified) == 1 and "AUTH_401_ESCALATED a" in notified[0]


def test_manager_auth_401_second_attempt_pointer_401ing_pages_no_launch(
        stale, capsys, monkeypatch, tmp_path):
    calls = _capture_runs(stale, monkeypatch)
    notified = []
    monkeypatch.setattr(stale, "_notify_macos", lambda text: notified.append(text))
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _seed_farm(monkeypatch, tmp_path, "b")
    _arm_pool(stale, "b")
    t0 = 1_000_000
    stale._record_auth_401("b", "u-b-401", t0)
    stale._record_auth_401("a", "u-prev", t0)
    _write_auth_401_manager(stale, "mgr1", "mgr-A", t0, uuid="u-fresh",
                            account="a")
    clock = {"now": t0 + 130}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    assert _launch_calls(calls) == [], "pointer b is mid-401 — not a launch target"
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"
    assert _flips(stale) == []
    assert [e["action"] for e in _auth_events(stale)
            if e["account"] == "a"] == ["escalate"]
    assert len(notified) == 1 and "AUTH_401_ESCALATED a" in notified[0]


def test_manager_auth_401_escalate_both_no_target_and_keychain_locked_notifies(
        stale, capsys, monkeypatch, tmp_path):
    calls = _capture_runs(stale, monkeypatch)
    notified = []
    monkeypatch.setattr(stale, "_notify_macos", lambda text: notified.append(text))
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: False)
    _seed_farm(monkeypatch, tmp_path, "b")
    _arm_pool(stale, "b")
    t0 = 1_000_000
    stale._record_auth_401("b", "u-b-401", t0)
    stale._record_auth_401("a", "u-prev", t0)
    _write_auth_401_manager(stale, "mgr1", "mgr-A", t0, uuid="u-fresh",
                            account="a")
    clock = {"now": t0 + 130}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    assert _launch_calls(calls) == [], "no healthy target AND keychain locked — no launch"
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"
    assert _flips(stale) == []
    assert [e["action"] for e in _auth_events(stale)
            if e["account"] == "a"] == ["escalate"]
    assert len(notified) == 1
    assert "no healthy account" in notified[0] and "keychain locked" in notified[0]


def test_manager_auth_401_first_attempt_never_launches_onto_401ing_pointer(
        stale, capsys, monkeypatch, tmp_path):
    calls = _capture_runs(stale, monkeypatch)
    notified = []
    monkeypatch.setattr(stale, "_notify_macos", lambda text: notified.append(text))
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _seed_farm(monkeypatch, tmp_path, "b")
    _arm_pool(stale, "b")
    t0 = 1_000_000
    stale._record_auth_401("b", "u-b-401", t0)
    _write_auth_401_manager(stale, "mgr1", "mgr-A", t0, uuid="u-first",
                            account="a")
    clock = {"now": t0 + 130}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    assert _launch_calls(calls) == [], "attempt 1 must not launch onto mid-401 b"
    assert [e["action"] for e in _auth_events(stale)
            if e["account"] == "a"] == ["escalate"], "promoted, not recover"
    assert _flips(stale) == []
    assert len(notified) == 1


def test_manager_auth_401_incident_replay_worker_then_manager(
        stale, capsys, monkeypatch, tmp_path):
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _seed_farm(monkeypatch, tmp_path, "b")
    _arm_pool(stale, "a")
    t0 = 1_000_000
    w_path = _write_record(stale, "w1", agent="worker", state="processing",
                           name="worker-tab", window_id="42")
    os.utime(w_path, (t0, t0))
    w_log = _write_auth_401_transcript(stale, "w1", uuid="u-worker")
    os.utime(w_log, (t0, t0))
    clock = {"now": t0 + 6 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main()
    assert "AUTH_401 worker-tab" in capsys.readouterr().out
    _write_auth_401_manager(stale, "mgr1", "mgr-A", clock["now"], uuid="u-mgr")
    clock["now"] += 2 * 60 + 10
    stale.main(manager_name="mgr-A")
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"
    assert len(_flips(stale)) == 1
    launches = _launch_calls(calls)
    assert len(launches) == 1
    inner = launches[0][0][-1]
    assert "/manager-takeover-recovery mgr1" in inner
    assert "CLAUDE_ORCH_ACCOUNT=b" in inner
    assert "CLAUDE_ORCH_ACCOUNT=a" not in inner, "today's zombie: takeover onto the 401ing account"
    assert [e["action"] for e in _auth_events(stale)] == ["recover", "escalate"]
    assert _auth_events(stale)[0]["source"] == "worker:worker-tab"


def test_manager_auth_401_second_attempt_launches_on_healthy_pointer(
        stale, capsys, monkeypatch, tmp_path):
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _seed_farm(monkeypatch, tmp_path, "b")
    _arm_pool(stale, "b")
    t0 = 1_000_000
    stale._record_auth_401("a", "u-prev", t0)
    _write_auth_401_manager(stale, "mgr1", "mgr-A", t0, uuid="u-fresh",
                            account="a")
    clock = {"now": t0 + 130}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n", "pointer untouched"
    assert _flips(stale) == [], "no new flip — the pointer is already healthy"
    launches = _launch_calls(calls)
    assert len(launches) == 1
    inner = launches[0][0][-1]
    assert "/manager-takeover-recovery mgr1" in inner
    assert "CLAUDE_ORCH_ACCOUNT=b" in inner
    assert "CLAUDE_ORCH_ACCOUNT=a" not in inner
    assert [e["action"] for e in _auth_events(stale)] == ["recover"], \
        "healthy-pointer attempt-2 stays a recover, not an escalate"


def test_manager_auth_401_second_attempt_no_target_notifies_human_directly(
        stale, capsys, monkeypatch):
    calls = _capture_runs(stale, monkeypatch)
    notified = []
    monkeypatch.setattr(stale, "_notify_macos", lambda text: notified.append(text))
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    stale.ACCOUNT_STATE.write_text(json.dumps(
        {"accounts": {}, "last_flip": {"ts": t0 - 30, "from": "b", "to": "a"}}))
    stale._record_auth_401("a", "u-prev", t0)
    path, log = _write_auth_401_manager(stale, "mgr1", "mgr-A", t0, uuid="u-fresh")
    clock = {"now": t0 + 130}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    assert stale.ACCOUNT_ACTIVE.read_text() == "a\n", "flip blocked by cooldown"
    assert _flips(stale) == []
    assert _launch_calls(calls) == []
    assert [e["action"] for e in _auth_events(stale)] == ["escalate"]
    assert len(notified) == 1, "the human is paged directly, no takeover required"
    assert "AUTH_401_ESCALATED a" in notified[0] and "/login" in notified[0]
    assert capsys.readouterr().out == "", "manager-stream page still buffered"
    clock["now"] += 60
    os.utime(log, (clock["now"] - 130, clock["now"] - 130))
    os.utime(path, (clock["now"] - 130, clock["now"] - 130))
    stale.main(manager_name="mgr-A")
    assert len(notified) == 1
    assert _launch_calls(calls) == []
    assert [e["action"] for e in _auth_events(stale)] == ["escalate"]
    (stale.ACTIVE / "mgr1.json").unlink()
    clock["now"] += 60
    stale.main(manager_name="mgr-A")
    out = capsys.readouterr().out
    assert "AUTH_401_ESCALATED" in out and "PAGE: run claude, then /login" in out
    assert len(notified) == 1, "replay is the manager-stream channel, not a re-notification"


def test_manager_auth_401_escalate_keychain_locked_notifies(
        stale, capsys, monkeypatch, tmp_path):
    calls = _capture_runs(stale, monkeypatch)
    notified = []
    monkeypatch.setattr(stale, "_notify_macos", lambda text: notified.append(text))
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: False)
    _seed_farm(monkeypatch, tmp_path, "b")
    _arm_pool(stale, "a")
    t0 = 1_000_000
    stale._record_auth_401("a", "u-prev1", t0)
    stale._record_auth_401("a", "u-prev2", t0 + 10)
    path, log = _write_auth_401_manager(stale, "mgr1", "mgr-A", t0, uuid="u-fresh",
                                        account="a")
    clock = {"now": t0 + 130}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    assert _launch_calls(calls) == [], "keychain locked: no launch"
    assert len(notified) == 1 and "keychain" in notified[0], \
        "…but never silent: outcome-derived notification names the reason"
    clock["now"] += 60
    os.utime(log, (clock["now"] - 130, clock["now"] - 130))
    os.utime(path, (clock["now"] - 130, clock["now"] - 130))
    stale.main(manager_name="mgr-A")
    assert len(notified) == 1 and _launch_calls(calls) == []


def test_manager_auth_401_recover_keychain_locked_notifies(stale, capsys, monkeypatch):
    calls = _capture_runs(stale, monkeypatch)
    notified = []
    monkeypatch.setattr(stale, "_notify_macos", lambda text: notified.append(text))
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: False)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    _write_auth_401_manager(stale, "mgr1", "mgr-A", t0, uuid="u-1")
    clock = {"now": t0 + 130}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    assert _launch_calls(calls) == []
    assert [e["action"] for e in _auth_events(stale)] == ["recover"]
    assert len(notified) == 1 and "keychain" in notified[0]


def test_manager_auth_401_escalates_after_bound(stale, capsys, monkeypatch):
    calls = _capture_runs(stale, monkeypatch)
    notified = []
    monkeypatch.setattr(stale, "_notify_macos", lambda text: notified.append(text))
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

    scan_401("u-1", 1)
    launches = _launch_calls(calls)
    assert len(launches) == 1 and "CLAUDE_ORCH_ACCOUNT=a" in launches[0][0][-1]
    scan_401("u-2", 2)
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"
    assert len(_flips(stale)) == 1
    assert len(_launch_calls(calls)) == 1
    scan_401("u-3", 3)
    assert len(_launch_calls(calls)) == 1, "exactly one launch across the episode"
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"
    assert len(_flips(stale)) == 1
    capsys.readouterr()
    assert [(e["account"], e["action"]) for e in _auth_events(stale)] == \
        [("a", "recover"), ("a", "escalate"), ("b", "recover")]
    assert notified == [], "successor launched at scan 1 — no notification"


def test_manager_auth_401_escalate_launches_on_flipped_account(stale, capsys, monkeypatch, tmp_path):
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _seed_farm(monkeypatch, tmp_path, "b")
    _arm_pool(stale, "a")
    t0 = 1_000_000
    stale._record_auth_401("a", "u-prev1", t0)
    stale._record_auth_401("a", "u-prev2", t0 + 10)
    _write_auth_401_manager(stale, "mgrX", "mgr-A", t0, uuid="u-fresh")
    clock = {"now": t0 + 130}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"
    assert len(_flips(stale)) == 1
    launches = _launch_calls(calls)
    assert len(launches) == 1
    inner = launches[0][0][-1]
    assert "/manager-takeover-recovery mgrX" in inner
    assert f"CLAUDE_CONFIG_DIR={tmp_path}/.claude-b" in inner
    assert "CLAUDE_ORCH_ACCOUNT=b" in inner
    assert [e["action"] for e in _auth_events(stale)] == ["escalate"]


def test_manager_auth_401_escalate_page_survives_recovery_rollup(stale, capsys, monkeypatch):
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    stale._record_auth_401("a", "u-prev1", t0)
    stale._record_auth_401("a", "u-prev2", t0 + 10)
    _write_auth_401_manager(stale, "mgr1", "mgr-A", t0, uuid="u-fresh")
    clock = {"now": t0 + 130}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    assert capsys.readouterr().out == "", "page buffered while the manager is bricked"
    (stale.ACTIVE / "mgr1.json").unlink()
    clock["now"] += 60
    stale.main(manager_name="mgr-A")
    out = capsys.readouterr().out
    assert "limit cleared" in out
    assert "AUTH_401_ESCALATED" in out and "PAGE: run claude, then /login" in out
    assert "CLAUDE_CONFIG_DIR" not in out and "rides ~/.claude" not in out


def test_manager_auth_401_escalate_page_only_when_flip_blocked(stale, capsys, monkeypatch):
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    t0 = 1_000_000
    stale.ACCOUNT_STATE.write_text(json.dumps(
        {"accounts": {}, "last_flip": {"ts": t0 - 30, "from": "b", "to": "a"}}))
    stale._record_auth_401("a", "u-prev1", t0)
    stale._record_auth_401("a", "u-prev2", t0 + 10)
    _write_auth_401_manager(stale, "mgr1", "mgr-A", t0, uuid="u-fresh")
    clock = {"now": t0 + 130}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    assert stale.ACCOUNT_ACTIVE.read_text() == "a\n", "flip blocked by cooldown, pointer unchanged"
    assert _flips(stale) == [], "no new flip — within cooldown"
    assert _launch_calls(calls) == [], "page only — never relaunch on the suspect account"
    assert [e["action"] for e in _auth_events(stale)] == ["escalate"]
    assert capsys.readouterr().out == "", "page buffered while the manager is bricked"
    (stale.ACTIVE / "mgr1.json").unlink()
    clock["now"] += 60
    stale.main(manager_name="mgr-A")
    out = capsys.readouterr().out
    assert "AUTH_401_ESCALATED" in out and "PAGE: run claude, then /login" in out, "page reaches the human"
    assert "CLAUDE_CONFIG_DIR" not in out and "rides ~/.claude" not in out
    assert _launch_calls(calls) == [], "still no recovery launch on the suspect account"


def test_manager_auth_401_dormant_when_pool_off(nudgy, capsys, monkeypatch):
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


def test_recovery_manager_routes_to_mgr_on_tmux(stale, monkeypatch):
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
    from dockwright import terminal
    monkeypatch.setenv("CLAUDE_ORCH_TERMINAL", "tmux")
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "claude-orch")
    terminal._DRIVER = None

    rec = {"cwd": "/c", "name": "m", "window_id": "%14"}
    out = stale._launch_recovery_manager(rec, "sid-9", "a")

    assert out == "%no-live-tmux", "recovery spawn returned a real pane — NOT absorbed"
    assert any("/manager-takeover-recovery sid-9" in " ".join(a) for a in no_live_tmux.exec), \
        "the recovery command was not the one intercepted"


def test_launch_recovery_manager_pins_manager_opus(stale, monkeypatch):
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
    assert "--model 'claude-opus-5[1m]'" in inner
    assert inner.index("--model") < inner.index("/manager-takeover-recovery")


def test_launch_recovery_manager_marks_pending_takeover(stale, monkeypatch):
    captured = {}

    class FakeDrv:
        async def spawn(self, **kw):
            captured.update(kw)
            return "%9"

    monkeypatch.setattr(stale, "_get_driver", lambda: FakeDrv())
    rec = {"cwd": "/c", "name": "m", "window_id": "%14"}
    assert stale._launch_recovery_manager(rec, "sid-1", "a") == "%9"
    inner = captured["argv"][-1]
    assert "DOCKWRIGHT_PENDING_TAKEOVER=1 " in inner
    assert inner.index("DOCKWRIGHT_PENDING_TAKEOVER=1") < inner.index("claude ")


def test_recovery_launch_carries_manager_settings(stale, monkeypatch):
    (stale.ROOT / "presets").mkdir(parents=True, exist_ok=True)
    settings = stale.ROOT / "presets" / "manager-settings.json"
    settings.write_text("{}")
    captured = {}

    class FakeDrv:
        async def spawn(self, **kw):
            captured.update(kw)
            return "%42"

    monkeypatch.setattr(stale, "_get_driver", lambda: FakeDrv())
    stale._launch_recovery_manager({"cwd": "/c", "name": "mgr-x"}, "sid123", "a")
    inner = captured["argv"][-1]
    assert f"--settings {str(settings)}" in inner
    assert "/manager-takeover-recovery sid123" in inner


def test_recovery_launch_omits_settings_when_preset_absent(stale, monkeypatch):
    captured = {}

    class FakeDrv:
        async def spawn(self, **kw):
            captured.update(kw)
            return "%42"

    monkeypatch.setattr(stale, "_get_driver", lambda: FakeDrv())
    stale._launch_recovery_manager({"cwd": "/c", "name": "m"}, "sid-1", "a")
    inner = captured["argv"][-1]
    assert "--settings" not in inner
    assert "/manager-takeover-recovery sid-1" in inner


def test_recovery_launch_carries_remote_control(stale, monkeypatch):
    monkeypatch.delenv("DOCKWRIGHT_MANAGER_RC", raising=False)
    captured = {}

    class FakeDrv:
        async def spawn(self, **kw):
            captured.update(kw)
            return "%9"

    monkeypatch.setattr(stale, "_get_driver", lambda: FakeDrv())
    stale._launch_recovery_manager({"cwd": "/c", "name": "m"}, "sid-1", "a")
    inner = captured["argv"][-1]
    assert "--remote-control" in inner
    assert inner.index("--remote-control") < inner.index("/manager-takeover-recovery")
    assert "--remote-control --model" in inner, inner


def test_recovery_launch_rc_opt_out(stale, monkeypatch):
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_RC", "0")
    captured = {}

    class FakeDrv:
        async def spawn(self, **kw):
            captured.update(kw)
            return "%9"

    monkeypatch.setattr(stale, "_get_driver", lambda: FakeDrv())
    stale._launch_recovery_manager({"cwd": "/c", "name": "m"}, "sid-1", "a")
    assert "--remote-control" not in captured["argv"][-1]


def test_recovery_launch_skip_perms_opt_in_and_scrub(stale, monkeypatch):
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_SKIP_PERMS", "1")
    captured = {}

    class FakeDrv:
        async def spawn(self, **kw):
            captured.update(kw)
            return "%9"

    monkeypatch.setattr(stale, "_get_driver", lambda: FakeDrv())
    stale._launch_recovery_manager({"cwd": "/c", "name": "m"}, "sid-1", "a")
    inner = captured["argv"][-1]
    assert "--remote-control --dangerously-skip-permissions --model" in inner, inner
    assert inner.index("--dangerously-skip-permissions") < inner.index("/manager-takeover-recovery")
    assert "DOCKWRIGHT_MANAGER_SKIP_PERMS" not in os.environ


def test_recovery_launch_skip_perms_default_off(stale, monkeypatch):
    captured = {}

    class FakeDrv:
        async def spawn(self, **kw):
            captured.update(kw)
            return "%9"

    monkeypatch.setattr(stale, "_get_driver", lambda: FakeDrv())
    stale._launch_recovery_manager({"cwd": "/c", "name": "m"}, "sid-1", "a")
    assert "--dangerously-skip-permissions" not in captured["argv"][-1]


def test_write_json_atomic_unique_tmp_per_invocation(tmp_path, monkeypatch):
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
    _make_idle_worker_past_threshold(stale)
    now = int(time.time())
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
    stale.main(manager_name="mgr")
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
    _make_idle_worker_past_threshold(stale, manager=None)
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


def _ls_shape(panes):
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


def test_orphan_window_protected_by_fresh_gardener_sidecar(stale, tmp_path, monkeypatch, capsys):
    _arm_driver(stale, monkeypatch, [("claude-workers", "🌱 gardener r1", "%7")])
    live = tmp_path / "gardener" / "live-windows"
    live.mkdir(parents=True)
    (live / "r1.window").write_text("%7")
    _seed_orphan_state(stale, "%7", time.time() - 700)
    stale.main()
    assert "ORPHAN_WINDOW" not in capsys.readouterr().out


def test_orphan_window_stale_gardener_sidecar_does_not_protect(stale, tmp_path, monkeypatch, capsys):
    _arm_driver(stale, monkeypatch, [("claude-workers", "🌱 gardener r1", "%7")])
    live = tmp_path / "gardener" / "live-windows"
    live.mkdir(parents=True)
    sidecar = live / "r1.window"
    sidecar.write_text("%7")
    old = time.time() - stale.GARDENER_WINDOW_PROTECT_TTL_SEC - 60
    os.utime(sidecar, (old, old))
    _seed_orphan_state(stale, "%7", time.time() - 700)
    stale.main()
    assert "ORPHAN_WINDOW %7" in capsys.readouterr().out


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
    _arm_driver(stale, monkeypatch, [("claude-workers", "w", "%5")])
    (tmp_path / "active" / "s1.json").write_text(json.dumps(
        {"claude_sid": "s1", "agent": "worker", "name": "nested-x",
         "state": "idle", "window_id": "", "nested": True}))
    _seed_orphan_state(stale, "%5", time.time() - 130)
    stale.main()
    assert "ORPHAN_WINDOW %5" in capsys.readouterr().out


def test_orphan_window_ladder_dedups_and_repages(stale, monkeypatch, capsys):
    _arm_driver(stale, monkeypatch, [("claude-workers", "w", "%5")])
    _seed_orphan_state(stale, "%5", time.time() - 150, paged=2)
    stale.main()
    assert "ORPHAN_WINDOW" not in capsys.readouterr().out
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
    _arm_driver(stale, monkeypatch, [("claude-workers", "w", "%5")])
    (tmp_path / "active" / "s1.json").write_text(json.dumps(
        {"claude_sid": "s1", "agent": "worker", "name": "alpha", "state": "idle",
         "window_id": "%5", "parent_manager_name": "other-mgr"}))
    _seed_orphan_state(stale, "%5", time.time() - 700, manager_name="my-mgr")
    stale.main(manager_name="my-mgr")
    assert "ORPHAN_WINDOW" not in capsys.readouterr().out


def test_recovery_flush_unburns_orphan_ladder_without_resetting_clock(
        stale, tmp_path, monkeypatch, capsys):
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


stale_monitor = _load_stale_monitor()


PROCEED_DIALOG = """\
⏺ Bash(git -C /home/user/projects/work/zeb4-recipes commit -m "add recipes")
╭──────────────────────────────────────────────────────────────╮
│ Bash command                                                 │
│   git -C /home/user/projects/work/zeb4-recipes commit -m "…" │
│ Do you want to proceed?                                      │
│ ❯ 1. Yes                                                     │
│   2. Yes, and don't ask again for git commit in this session │
│   3. No, and tell Claude what to do differently (esc)        │
╰──────────────────────────────────────────────────────────────╯"""

TRUST_DIALOG_NEW = """\
 Accessing workspace:
 /home/user/projects/work/worker
 Quick safety check: Is this a project you created or one you trust?
 ❯ 1. Yes, I trust this folder
   2. No, exit
 Enter to confirm · Esc to cancel"""

PLAIN_OUTPUT_WITH_QUESTION = """\
⏺ The test suite prints "Do you want to proceed?" in its usage banner.
  All 12 tests passed.
"""


class FakeDriver:
    def __init__(self, screens):
        self.screens = screens
    def capture_screen(self, pane):
        val = self.screens.get(pane)
        if isinstance(val, Exception):
            raise val
        return val


def _worker_record(tmp_active, sid, name, wid, state="processing", runtime="claude",
                   manager="mgr-x"):
    rec = {"claude_sid": sid, "agent": "worker", "name": name, "window_id": wid,
           "state": state, "runtime": runtime, "parent_manager_name": manager}
    (tmp_active / f"{sid}.json").write_text(json.dumps(rec))
    return rec


def _run_approval_scan(monkeypatch, tmp_path, screens, emitted=None,
                       manager="mgr-x", now=1_000_000):
    active = tmp_path / "active"; active.mkdir(exist_ok=True)
    pending = tmp_path / "pending"; pending.mkdir(exist_ok=True)
    monkeypatch.setattr(stale_monitor, "ACTIVE", active)
    monkeypatch.setattr(stale_monitor, "ASSIGNMENTS_PENDING", pending)
    monkeypatch.setattr(stale_monitor, "_get_driver", lambda: FakeDriver(screens))
    events = []
    def emit(kind, name, line, dedup_key=None):
        events.append((kind, name, line, dedup_key))
    next_emitted = {}
    return active, pending, events, next_emitted, (emitted or {}), emit, now


def test_approval_prompt_detected_and_paged(monkeypatch, tmp_path):
    active, pending, events, next_emitted, emitted, emit, now = _run_approval_scan(
        monkeypatch, tmp_path, {"%7": PROCEED_DIALOG})
    _worker_record(active, "sid1", "zeb4", "%7")
    stale_monitor._scan_approval_prompts("mgr-x", now, emitted, next_emitted, emit)
    assert len(events) == 1
    kind, name, line, key = events[0]
    assert kind == "approval" and name == "zeb4"
    assert line.startswith("APPROVAL_PROMPT zeb4: ")
    assert key.startswith("approval:sid1:")
    assert next_emitted[key]["paged"] == 1


def test_approval_same_prompt_not_repaged_within_rung(monkeypatch, tmp_path):
    active, pending, events, next_emitted, emitted, emit, now = _run_approval_scan(
        monkeypatch, tmp_path, {"%7": PROCEED_DIALOG})
    _worker_record(active, "sid1", "zeb4", "%7")
    stale_monitor._scan_approval_prompts("mgr-x", now, {}, next_emitted, emit)
    first_key = events[0][3]
    events.clear()
    later = dict(next_emitted); next2 = {}
    stale_monitor._scan_approval_prompts("mgr-x", now + 120, later, next2, emit)
    assert events == []
    assert next2[first_key]["paged"] == 1


def test_approval_repages_on_nudge_ladder(monkeypatch, tmp_path):
    active, pending, events, next_emitted, emitted, emit, now = _run_approval_scan(
        monkeypatch, tmp_path, {"%7": PROCEED_DIALOG})
    _worker_record(active, "sid1", "zeb4", "%7")
    stale_monitor._scan_approval_prompts("mgr-x", now, {}, next_emitted, emit)
    events.clear()
    later_state = dict(next_emitted); next2 = {}
    stale_monitor._scan_approval_prompts("mgr-x", now + 360, later_state, next2, emit)
    assert len(events) == 1
    assert next2[events[0][3]]["paged"] == 5


def test_approval_cleared_prompt_drops_state(monkeypatch, tmp_path):
    active, pending, events, next_emitted, emitted, emit, now = _run_approval_scan(
        monkeypatch, tmp_path, {"%7": "⏺ done\n"})
    _worker_record(active, "sid1", "zeb4", "%7")
    prior = {"approval:sid1:abc123def456": {"first_seen": now - 60, "paged": 1}}
    stale_monitor._scan_approval_prompts("mgr-x", now, prior, next_emitted, emit)
    assert events == []
    assert not any(k.startswith("approval:sid1:") for k in next_emitted)


def test_approval_negative_cases(monkeypatch, tmp_path):
    active, pending, events, next_emitted, emitted, emit, now = _run_approval_scan(
        monkeypatch, tmp_path, {"%1": PLAIN_OUTPUT_WITH_QUESTION,
                                "%2": PROCEED_DIALOG, "%3": PROCEED_DIALOG,
                                "%4": PROCEED_DIALOG})
    _worker_record(active, "s1", "plain", "%1")
    _worker_record(active, "s2", "idle", "%2", state="idle")
    _worker_record(active, "s3", "codex", "%3", runtime="codex")
    _worker_record(active, "s4", "foreign", "%4", manager="other")
    stale_monitor._scan_approval_prompts("mgr-x", now, emitted, next_emitted, emit)
    assert events == []


def test_approval_pending_sidecar_trust_dialog(monkeypatch, tmp_path):
    active, pending, events, next_emitted, emitted, emit, now = _run_approval_scan(
        monkeypatch, tmp_path, {"%9": TRUST_DIALOG_NEW})
    (pending / "aaff01.window").write_text("%9")
    (pending / "aaff01.json").write_text(json.dumps(
        {"assignment_id": "aaff01", "name": "fresh-spawn",
         "parent_manager_name": "mgr-x"}))
    stale_monitor._scan_approval_prompts("mgr-x", now, emitted, next_emitted, emit)
    assert len(events) == 1
    assert events[0][1] == "fresh-spawn"
    assert events[0][3].startswith("approval:aaff01:")


def test_approval_capture_failure_is_no_event(monkeypatch, tmp_path):
    active, pending, events, next_emitted, emitted, emit, now = _run_approval_scan(
        monkeypatch, tmp_path, {"%7": RuntimeError("tmux down")})
    _worker_record(active, "sid1", "zeb4", "%7")
    stale_monitor._scan_approval_prompts("mgr-x", now, emitted, next_emitted, emit)
    assert events == []


def test_approval_driver_absent_is_noop(monkeypatch, tmp_path):
    active = tmp_path / "active"; active.mkdir()
    monkeypatch.setattr(stale_monitor, "ACTIVE", active)
    monkeypatch.setattr(stale_monitor, "_get_driver", None)
    stale_monitor._scan_approval_prompts("mgr-x", 1_000_000, {}, {}, lambda *a, **k: None)


_MCP_CHILDREN = (
    "caffeinate -i -t 300",
    "/opt/homebrew/bin/uv tool uvx elasticsearch-mcp-server",
    "npm exec @playwright/mcp@latest --extension",
    "docker run -i --rm --init crystaldba/postgres-mcp postgresql://USER:PASS@host:5432/db",
    "npm exec nx mcp",
)
_BASH_TOOL_SHELL = (
    "/bin/zsh -c source /Users/dev/.claude/shell-snapshots/"
    "snapshot-zsh-1786677333041-byreh7.sh 2>/dev/null || true && setopt NO_EXTENDED_GLOB "
    "&& eval 'uv run pytest -q' < /dev/null && pwd -P >| /tmp/claude-92e4-cwd"
)
_NESTED_SESSION_CHILD = (
    "claude -p check that /Users/dev/.claude/shell-snapshots/"
    "snapshot-zsh-1786677333041-byreh7.sh is quoted verbatim in this prompt"
)
_WORKER_ARGV = "claude --resume 0f3a1c7e-worker"
_WORKER_PID = 4242
_BUSY_T0 = 1_000_000


def _proc_index(children=(), own=_WORKER_ARGV, pid=_WORKER_PID):
    command_by_pid = {pid: own}
    for offset, command in enumerate(children, start=1):
        command_by_pid[pid + offset] = command
    return {"command_by_pid": command_by_pid,
            "child_commands": {pid: list(children)} if children else {}}


def _arm_busy_scan(stale, monkeypatch, index):
    monkeypatch.setattr(stale, "AUTOCLOSE_CADENCE_SEC", 60)
    monkeypatch.setattr(stale.time, "time", lambda: _BUSY_T0)
    monkeypatch.setattr(stale, "_process_index", lambda: index)


def _busy_worker(stale, elapsed, sid="busy-1", **overrides):
    overrides.setdefault("pid", _WORKER_PID)
    overrides.setdefault("iterm_sid", "7")
    return _write_record(
        stale, sid,
        last_turn_at=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                   time.gmtime(_BUSY_T0 - elapsed)),
        **overrides)


def test_busy_shell_under_cap_keeps_worker(stale, monkeypatch):
    _arm_busy_scan(stale, monkeypatch,
                   _proc_index((*_MCP_CHILDREN, _BASH_TOOL_SHELL)))
    path = _busy_worker(stale, 150)
    stale.main()
    assert path.exists(), "a live Bash-tool shell under the cap must not be reaped"


def _world(stale):
    return {str(p.relative_to(stale.ROOT)): p.read_bytes()
            for p in sorted(stale.ROOT.rglob("*")) if p.is_file()}


def _busy_scan_world(stale, monkeypatch, capsys, manager_name, index, *,
                     skipped_upstream):
    for child in list(stale.ROOT.iterdir()):
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    for d in ("active", "questions", "closed"):
        (stale.ROOT / d).mkdir()
    _arm_busy_scan(stale, monkeypatch, index)
    monkeypatch.setattr(stale, "_is_delegation_live",
                        lambda record, log=None: skipped_upstream)
    terminal = _capture_runs(stale, monkeypatch)
    if manager_name:
        _write_limited_manager(stale, "mgr-sid", manager_name, _BUSY_T0 - 600)
    _busy_worker(stale, 150, sid="quiet", parent_manager_name=manager_name)
    capsys.readouterr()
    rc = stale.main(manager_name=manager_name)
    return _world(stale), capsys.readouterr().out, rc, list(terminal)


@pytest.mark.parametrize("manager_name", [None, "mgr"],
                         ids=["global", "manager-limited"])
def test_a_refused_worker_is_indistinguishable_from_a_never_candidate(
        stale, monkeypatch, capsys, manager_name):
    index = _proc_index((*_MCP_CHILDREN, _BASH_TOOL_SHELL))
    reached = []
    real_closer = stale._autoclose_idle_worker

    def counting_closer(*args, **kwargs):
        reached.append(1)
        return real_closer(*args, **kwargs)

    monkeypatch.setattr(stale, "_autoclose_idle_worker", counting_closer)
    control_files, control_out, control_rc, control_term = _busy_scan_world(
        stale, monkeypatch, capsys, manager_name, index, skipped_upstream=True)
    assert reached == [], "the control must be dropped BEFORE the closer"
    subject_files, subject_out, subject_rc, subject_term = _busy_scan_world(
        stale, monkeypatch, capsys, manager_name, index, skipped_upstream=False)
    assert reached == [1], "the subject must reach the closer and be refused"

    changed = sorted(k for k in set(control_files) | set(subject_files)
                     if control_files.get(k) != subject_files.get(k))
    assert changed == [], f"a refusal left a trace under ROOT: {changed}"
    assert subject_out == control_out, f"a refusal reached stdout: {subject_out!r}"
    assert subject_term == control_term, f"a refusal drove the terminal: {subject_term}"
    assert subject_rc == control_rc


def test_busy_shell_over_cap_reaps_worker(stale, monkeypatch):
    _arm_busy_scan(stale, monkeypatch,
                   _proc_index((*_MCP_CHILDREN, _BASH_TOOL_SHELL)))
    path = _busy_worker(stale, 400)
    stale.main()
    assert not path.exists(), "the cap must still reap a permanently-busy worker"


def test_busy_shell_exactly_at_cap_keeps_worker(stale, monkeypatch):
    _arm_busy_scan(stale, monkeypatch,
                   _proc_index((*_MCP_CHILDREN, _BASH_TOOL_SHELL)))
    path = _busy_worker(stale, 300)
    stale.main()
    assert path.exists(), "the cap is inclusive (elapsed <= deadline)"


def test_busy_shell_no_children_reaps_worker(stale, monkeypatch):
    _arm_busy_scan(stale, monkeypatch, _proc_index())
    path = _busy_worker(stale, 150)
    stale.main()
    assert not path.exists()


def test_busy_shell_only_mcp_children_reaps_worker(stale, monkeypatch):
    _arm_busy_scan(stale, monkeypatch, _proc_index(_MCP_CHILDREN))
    path = _busy_worker(stale, 150)
    stale.main()
    assert not path.exists(), "MCP servers and caffeinate are not background work"


def test_busy_shell_codex_runtime_reaps_worker(stale, monkeypatch):
    _arm_busy_scan(stale, monkeypatch,
                   _proc_index((*_MCP_CHILDREN, _BASH_TOOL_SHELL)))
    path = _busy_worker(stale, 150, runtime="codex")
    stale.main()
    assert not path.exists(), "the snapshot marker is a Claude CLI detail"


def test_busy_shell_future_runtime_reaps_worker(stale, monkeypatch):
    _arm_busy_scan(stale, monkeypatch,
                   _proc_index((*_MCP_CHILDREN, _BASH_TOOL_SHELL)))
    path = _busy_worker(stale, 150, runtime="gemini")
    stale.main()
    assert not path.exists()


def test_busy_shell_non_int_pid_reaps_worker(stale, monkeypatch):
    _arm_busy_scan(stale, monkeypatch,
                   _proc_index((*_MCP_CHILDREN, _BASH_TOOL_SHELL)))
    path = _busy_worker(stale, 150, pid="4242")
    stale.main()
    assert not path.exists()


def test_busy_shell_zero_pid_reaps_worker(stale, monkeypatch):
    _arm_busy_scan(stale, monkeypatch,
                   {"command_by_pid": {0: _WORKER_ARGV},
                    "child_commands": {0: [_BASH_TOOL_SHELL]}})
    path = _busy_worker(stale, 150, pid=0)
    stale.main()
    assert not path.exists()


def test_busy_shell_recycled_pid_reaps_worker(stale, monkeypatch):
    _arm_busy_scan(stale, monkeypatch,
                   _proc_index((*_MCP_CHILDREN, _BASH_TOOL_SHELL),
                               own="/opt/homebrew/bin/uv tool uvx elasticsearch-mcp-server"))
    path = _busy_worker(stale, 150)
    stale.main()
    assert not path.exists(), "a dead record must not inherit a recycled pid's children"


def test_busy_shell_nested_session_child_reaps_worker(stale, monkeypatch):
    _arm_busy_scan(stale, monkeypatch,
                   _proc_index((*_MCP_CHILDREN, _NESTED_SESSION_CHILD)))
    path = _busy_worker(stale, 150)
    stale.main()
    assert not path.exists(), "a claude child carries prompt text, marker included"


def test_busy_shell_sh_dash_c_without_marker_keeps_worker(stale, monkeypatch):
    _arm_busy_scan(stale, monkeypatch,
                   _proc_index((*_MCP_CHILDREN,
                                "/bin/zsh -c until curl -sf http://localhost:8080; do sleep 5; done")))
    path = _busy_worker(stale, 150)
    stale.main()
    assert path.exists()


def test_busy_shell_shell_without_dash_c_reaps_worker(stale, monkeypatch):
    _arm_busy_scan(stale, monkeypatch, _proc_index((*_MCP_CHILDREN, "/bin/zsh -i")))
    path = _busy_worker(stale, 150)
    stale.main()
    assert not path.exists(), "an interactive shell is not a running command"


def test_busy_shell_broken_process_index_holds_worker_to_the_cap(stale, monkeypatch):
    _arm_busy_scan(stale, monkeypatch, None)
    under = _busy_worker(stale, 150, sid="under-cap")
    over = _busy_worker(stale, 400, sid="over-cap", pid=_WORKER_PID + 10)
    stale.main()
    assert under.exists(), "a broken ps must hold, not close, under the cap"
    assert not over.exists(), "the cap still closes regardless — hold is bounded"


def test_a_raising_process_index_holds_the_worker_and_never_aborts_the_scan(
        stale, monkeypatch, capsys):
    _arm_busy_scan(stale, monkeypatch, None)
    monkeypatch.setattr(stale, "_process_index",
                        lambda: (_ for _ in ()).throw(RuntimeError("ps exploded")))
    busy = _busy_worker(stale, 150, sid="raiser")
    later = _busy_worker(stale, 400, sid="later", pid=_WORKER_PID + 10)
    assert stale.main() == 0, "a raising ps must not abort the scan"
    assert busy.exists(), "a raise is 'cannot tell' and must hold"
    assert not later.exists(), "the scan continued and the over-cap worker closed"


def test_busy_shell_marker_on_grandchild_reaps_worker(stale, monkeypatch):
    kid = _WORKER_PID + 1
    _arm_busy_scan(stale, monkeypatch,
                   {"command_by_pid": {_WORKER_PID: _WORKER_ARGV,
                                       kid: "npm exec nx mcp",
                                       kid + 1: _BASH_TOOL_SHELL},
                    "child_commands": {_WORKER_PID: ["npm exec nx mcp"],
                                       kid: [_BASH_TOOL_SHELL]}})
    path = _busy_worker(stale, 150)
    stale.main()
    assert not path.exists(), "only DIRECT children count"


def test_busy_shell_process_index_not_taken_past_cap(stale, monkeypatch):
    calls = []
    _arm_busy_scan(stale, monkeypatch, None)
    monkeypatch.setattr(stale, "_process_index",
                        lambda: calls.append(1) or _proc_index((_BASH_TOOL_SHELL,)))
    path = _busy_worker(stale, 400)
    stale.main()
    assert calls == [], "the cap is checked before the snapshot is taken"
    assert not path.exists()


def test_busy_shell_deadline_floor_outruns_autoclose_sampling_gap(stale, monkeypatch):
    max_gap = stale.AUTOCLOSE_SKEW_CADENCES * (stale.AUTOCLOSE_CADENCE_SEC + 60)
    for idle in range(60, 3 * stale.AUTOCLOSE_CADENCE_SEC + 60, 60):
        monkeypatch.setattr(stale, "IDLE_THRESHOLD_SEC", idle)
        window = stale._busy_shell_deadline() - idle
        assert window > max_gap, f"TTL={idle}s: window of {window}s can be stepped over"


def test_busy_shell_deadline_zero_ttl_leaves_window_empty(stale, monkeypatch):
    for idle in (0, -3600):
        monkeypatch.setattr(stale, "IDLE_THRESHOLD_SEC", idle)
        assert stale._busy_shell_deadline() == idle


def test_busy_shell_zero_ttl_reaps_worker_with_live_shell(stale, monkeypatch):
    monkeypatch.setattr(stale.time, "time", lambda: _BUSY_T0)
    monkeypatch.setattr(stale, "_process_index",
                        lambda: _proc_index((*_MCP_CHILDREN, _BASH_TOOL_SHELL)))
    monkeypatch.setattr(stale, "IDLE_THRESHOLD_SEC", 0)
    path = _busy_worker(stale, 150)
    stale.main()
    assert not path.exists()


def test_busy_shell_idle_multiplier_is_three(stale):
    assert stale.BUSY_SHELL_IDLE_MULTIPLIER == 3


def test_a_new_close_lane_inherits_the_guard_by_construction(stale, monkeypatch):
    index = _proc_index((*_MCP_CHILDREN, _BASH_TOOL_SHELL))
    _arm_busy_scan(stale, monkeypatch, index)
    path = _busy_worker(stale, 150)
    record = json.loads(path.read_text())

    def _autoclose_orphaned_workers(pairs):
        return [stale._autoclose_idle_worker(rp, rec, 150) for rp, rec in pairs]

    assert _autoclose_orphaned_workers([(path, record)]) == [None]
    assert path.exists(), "a new lane must be refused without opting in"


def test_new_lane_refused_through_every_shape_that_defeated_the_ast_guard(
        stale, monkeypatch):
    index = _proc_index((*_MCP_CHILDREN, _BASH_TOOL_SHELL))
    _arm_busy_scan(stale, monkeypatch, index)

    closer = stale._autoclose_idle_worker

    def one_line_wrapper(rp, rec, el):
        return stale._autoclose_idle_worker(rp, rec, el)

    def via_globals(rp, rec, el):
        return vars(stale)["_autoclose" + "_idle_worker"](rp, rec, el)

    def choke_point(fn, rp, rec, el):
        return fn(rp, rec, el)

    for i, lane in enumerate((closer, one_line_wrapper, via_globals,
                              lambda rp, rec, el: choke_point(
                                  stale._autoclose_idle_worker, rp, rec, el))):
        path = _busy_worker(stale, 150, sid=f"lane-{i}")
        record = json.loads(path.read_text())
        assert lane(path, record, 150) is None, f"lane {i} was not refused"
        assert path.exists(), f"lane {i} closed a busy worker"


def test_new_close_lane_still_closes_a_worker_that_is_not_busy(stale, monkeypatch):
    _arm_busy_scan(stale, monkeypatch, _proc_index(_MCP_CHILDREN))
    path = _busy_worker(stale, 150)
    record = json.loads(path.read_text())
    assert stale._autoclose_idle_worker(path, record, 150) is not None
    assert not path.exists()


def test_new_close_lane_closes_past_the_cap_even_when_busy(stale, monkeypatch):
    _arm_busy_scan(stale, monkeypatch,
                   _proc_index((*_MCP_CHILDREN, _BASH_TOOL_SHELL)))
    path = _busy_worker(stale, 400)
    record = json.loads(path.read_text())
    assert stale._autoclose_idle_worker(path, record, 400) is not None
    assert not path.exists()


def test_the_closer_resolves_the_index_itself(stale, monkeypatch):
    calls = []
    _arm_busy_scan(stale, monkeypatch, None)
    monkeypatch.setattr(
        stale, "_process_index",
        lambda: calls.append(1) or _proc_index((*_MCP_CHILDREN, _BASH_TOOL_SHELL)))
    path = _busy_worker(stale, 150)
    record = json.loads(path.read_text())
    assert stale._autoclose_idle_worker(path, record, 150) is None
    assert calls == [1], "the closer must resolve the index itself"
    assert path.exists()


def test_the_closer_takes_no_index_from_its_caller(stale):
    import inspect
    params = list(inspect.signature(stale._autoclose_idle_worker).parameters)
    assert params == ["record_path", "record", "elapsed_sec"], params


def test_process_index_parses_ps_rows(monkeypatch):
    mod = _load_stale_monitor()
    captured = {}
    rows = "\n".join((
        f" {_WORKER_PID}     1 {_WORKER_ARGV}",
        f" {_WORKER_PID + 1}  {_WORKER_PID} {_BASH_TOOL_SHELL}",
        "not-a-row",
        "  77  xx  ppid is not a number",
    ))

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        return types.SimpleNamespace(returncode=0, stdout=rows)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    index = mod._process_index()
    assert captured["argv"] == ["ps", "-axo", "pid=,ppid=,command="]
    assert captured["kw"]["capture_output"] is True
    assert captured["kw"]["text"] is True
    assert captured["kw"]["errors"] == "replace"
    assert captured["kw"]["timeout"] == 10
    assert index["command_by_pid"][_WORKER_PID] == _WORKER_ARGV
    assert index["command_by_pid"][_WORKER_PID + 1] == _BASH_TOOL_SHELL
    assert index["child_commands"][_WORKER_PID] == [_BASH_TOOL_SHELL]
    assert 77 not in index["command_by_pid"], "a non-numeric ppid row is skipped"


@pytest.mark.parametrize("failure", [
    "nonzero", "oserror", "timeout", "unicode", "empty", "unparseable", "no_stdout",
])
def test_process_index_returns_none_on_failure(monkeypatch, failure):
    mod = _load_stale_monitor()

    def fake_run(argv, **kw):
        if failure == "nonzero":
            return types.SimpleNamespace(returncode=1, stdout="")
        if failure == "oserror":
            raise OSError("ps: command not found")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd=argv, timeout=10)
        if failure == "unicode":
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        if failure == "empty":
            return types.SimpleNamespace(returncode=0, stdout="")
        if failure == "unparseable":
            return types.SimpleNamespace(returncode=0, stdout="garbage\nrows only\n")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod._process_index() is None


def test_process_index_real_ps_lists_own_pid():
    mod = _load_stale_monitor()
    index = mod._process_index()
    assert index is not None, "real ps must parse"
    assert os.getpid() in index["command_by_pid"]


def _transcript(tmp_path, age_sec, name="w.jsonl"):
    log = tmp_path / "transcripts" / name
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("")
    stamp = time.time() - age_sec
    os.utime(log, (stamp, stamp))
    return log


def _no_real_windows(stale, monkeypatch):
    monkeypatch.setattr(stale.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(stale.os, "kill", lambda *a, **k: None)


def test_idle_worker_with_a_live_transcript_is_not_autoclosed(stale, tmp_path, monkeypatch):
    now = int(time.time())
    inside_cap = stale._busy_shell_deadline() - 500
    old_turn = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - inside_cap))
    rec = _write_record(stale, "s1", last_turn_at=old_turn,
                        transcript_path=str(_transcript(tmp_path, 3.0)))
    _no_real_windows(stale, monkeypatch)

    assert stale.main() == 0
    assert rec.exists(), "a worker still writing its transcript must survive the scan"
    assert not (stale.CLOSED / "s1.json").exists()


def test_a_live_transcript_stops_holding_a_worker_past_the_busy_shell_deadline(
        stale, tmp_path, monkeypatch):
    now = int(time.time())
    past_cap = stale._busy_shell_deadline() + 500
    old_turn = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - past_cap))
    rec = _write_record(stale, "s1", last_turn_at=old_turn,
                        transcript_path=str(_transcript(tmp_path, 3.0)))
    _no_real_windows(stale, monkeypatch)

    assert stale.main() == 0
    assert not rec.exists(), "past the cap a worker closes regardless of transcript freshness"
    assert (stale.CLOSED / "s1.json").exists()


def test_idle_worker_with_an_old_transcript_is_still_autoclosed(stale, tmp_path, monkeypatch):
    now = int(time.time())
    old_turn = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10_000))
    rec = _write_record(stale, "s1", last_turn_at=old_turn,
                        transcript_path=str(_transcript(tmp_path, 4000.0)))
    _no_real_windows(stale, monkeypatch)

    assert stale.main() == 0
    assert not rec.exists()
    assert (stale.CLOSED / "s1.json").exists()


def test_autoclose_is_unchanged_when_the_transcript_path_is_absent_or_unusable(
        stale, tmp_path, monkeypatch):
    now = int(time.time())
    old_turn = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10_000))
    absent = _write_record(stale, "absent", last_turn_at=old_turn)
    empty = _write_record(stale, "empty", last_turn_at=old_turn, transcript_path="")
    wrong = _write_record(stale, "wrong", last_turn_at=old_turn, transcript_path=17)
    gone = _write_record(stale, "gone", last_turn_at=old_turn,
                         transcript_path=str(tmp_path / "nope" / "missing.jsonl"))
    _no_real_windows(stale, monkeypatch)

    assert stale.main() == 0
    for sid, rec in (("absent", absent), ("empty", empty), ("wrong", wrong), ("gone", gone)):
        assert not rec.exists(), f"{sid} must fall back to today's autoclose behaviour"
        assert (stale.CLOSED / f"{sid}.json").exists()


def test_a_live_transcript_does_not_rescue_a_peer_managers_worker(stale, tmp_path, monkeypatch):
    now = int(time.time())
    old_turn = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10_000))
    own = _write_record(stale, "own", parent_manager_name="mgr-A", last_turn_at=old_turn,
                        transcript_path=str(_transcript(tmp_path, 3.0, "own.jsonl")))
    peer = _write_record(stale, "peer", parent_manager_name="mgr-B", last_turn_at=old_turn,
                         transcript_path=str(_transcript(tmp_path, 4000.0, "peer.jsonl")))
    _no_real_windows(stale, monkeypatch)

    assert stale.main(manager_name="mgr-A") == 0
    assert own.exists(), "own worker is transcript-live and must survive"
    assert peer.exists(), "peer worker stays out of scope, freshness irrelevant"
    assert not (stale.CLOSED / "peer.json").exists()


def test_autoclose_freshness_window_moves_with_the_turn_end_grace_env(
        stale, tmp_path, monkeypatch):
    now = int(time.time())
    old_turn = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10_000))
    rec = _write_record(stale, "s1", last_turn_at=old_turn,
                        transcript_path=str(_transcript(tmp_path, 200.0)))
    _no_real_windows(stale, monkeypatch)
    monkeypatch.setenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", "600")

    assert stale.main() == 0
    assert rec.exists(), "a 200s-old transcript is live under a 600s grace"
    assert not (stale.CLOSED / "s1.json").exists()


def test_a_zero_grace_turns_the_freshness_gate_off(stale, tmp_path, monkeypatch):
    now = int(time.time())
    old_turn = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10_000))
    rec = _write_record(stale, "s1", last_turn_at=old_turn,
                        transcript_path=str(_transcript(tmp_path, 3.0)))
    _no_real_windows(stale, monkeypatch)
    monkeypatch.setenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", "0")

    assert stale.main() == 0
    assert not rec.exists()
    assert (stale.CLOSED / "s1.json").exists()


def test_is_transcript_live_honours_a_now_passed_by_the_caller(stale, tmp_path):
    rec = {"transcript_path": str(_transcript(tmp_path, 3.0))}
    now = time.time()
    assert stale._is_transcript_live(rec, now) is True
    assert stale._is_transcript_live(rec, now + 10_000) is False


def test_is_transcript_live_returns_false_rather_than_raising(stale, tmp_path):
    for rec in ({}, {"transcript_path": None}, {"transcript_path": 17},
                {"transcript_path": []}, {"transcript_path": ""},
                {"transcript_path": "\0bad"},
                {"transcript_path": str(tmp_path / "gone" / "missing.jsonl")}):
        assert stale._is_transcript_live(rec) is False


def test_a_direct_closer_call_refuses_a_worker_with_a_live_transcript(
        stale, tmp_path, monkeypatch):
    rec = _write_record(stale, "s1", transcript_path=str(_transcript(tmp_path, 3.0)))
    record = json.loads(rec.read_text())
    _no_real_windows(stale, monkeypatch)
    inside_cap = stale._busy_shell_deadline() - 500

    assert stale._autoclose_idle_worker(rec, record, inside_cap) is None
    assert rec.exists(), "the closer must refuse a record whose transcript is live"
    assert not (stale.CLOSED / "s1.json").exists()


def test_a_direct_closer_call_still_closes_past_the_cap(stale, tmp_path, monkeypatch):
    rec = _write_record(stale, "s1", transcript_path=str(_transcript(tmp_path, 3.0)))
    record = json.loads(rec.read_text())
    _no_real_windows(stale, monkeypatch)
    past_cap = stale._busy_shell_deadline() + 500

    assert stale._autoclose_idle_worker(rec, record, past_cap) is not None
    assert not rec.exists()
    assert (stale.CLOSED / "s1.json").exists()


def test_the_closer_holds_a_record_whose_transcript_cannot_be_read(
        stale, tmp_path, monkeypatch):
    log = _transcript(tmp_path, 3.0)
    rec = _write_record(stale, "s1", transcript_path=str(log))
    record = json.loads(rec.read_text())
    _no_real_windows(stale, monkeypatch)
    log.parent.chmod(0o000)
    try:
        assert stale._is_transcript_live(record) is False, "stat must fail for this test to mean anything"
        assert stale._transcript_unreadable(record) is True
        assert stale._autoclose_idle_worker(
            rec, record, stale._busy_shell_deadline() - 500) is None
        assert rec.exists()
    finally:
        log.parent.chmod(0o755)


def test_an_unreadable_transcript_is_still_closed_past_the_cap(
        stale, tmp_path, monkeypatch):
    log = _transcript(tmp_path, 3.0)
    rec = _write_record(stale, "s1", transcript_path=str(log))
    record = json.loads(rec.read_text())
    _no_real_windows(stale, monkeypatch)
    log.parent.chmod(0o000)
    try:
        assert stale._autoclose_idle_worker(
            rec, record, stale._busy_shell_deadline() + 500) is not None
        assert not rec.exists()
    finally:
        if log.parent.exists():
            log.parent.chmod(0o755)


def test_a_record_with_no_transcript_path_stays_reapable(stale, monkeypatch):
    _no_real_windows(stale, monkeypatch)
    for value in ({}, {"transcript_path": None}, {"transcript_path": ""},
                  {"transcript_path": 17}, {"transcript_path": []}):
        assert stale._transcript_unreadable(value) is False
    for hostile in ([], "x", 5, None):
        assert stale._transcript_unreadable(hostile) is False
        assert stale._is_transcript_live(hostile) is False


def test_a_recorded_transcript_that_resolves_to_nothing_stays_reapable(
        stale, tmp_path, monkeypatch):
    _no_real_windows(stale, monkeypatch)
    missing_file = {"transcript_path": str(tmp_path / "gone" / "missing.jsonl")}
    not_a_dir = {"transcript_path": str(_transcript(tmp_path, 3.0)) + "/nested.jsonl"}
    assert stale._transcript_unreadable(missing_file) is False
    assert stale._transcript_unreadable(not_a_dir) is False

    rec = _write_record(stale, "s1", transcript_path=missing_file["transcript_path"])
    record = json.loads(rec.read_text())
    assert stale._autoclose_idle_worker(
        rec, record, stale._busy_shell_deadline() - 500) is not None
    assert not rec.exists()
