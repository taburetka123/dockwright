import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from dockwright import lane_lines, paths
from tests.test_mcp_tools import _make_transcript, _patch_spawn_registers_active, _write_closed

MGR_DENY = "--disallowedTools=Artifact,SendFeedback,Workflow,AskUserQuestion,ReportFindings"


REPO_ROOT = Path(__file__).resolve().parent.parent
STALE_MONITOR_PATH = REPO_ROOT / "src" / "dockwright" / "stale_monitor.py"


def _load_stale_monitor():
    spec = importlib.util.spec_from_file_location("stale_monitor_under_test", STALE_MONITOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_codex_sessions_root_honours_codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
    mod = _load_stale_monitor()
    assert mod.CODEX_SESSIONS == tmp_path / "cx" / "sessions"
    monkeypatch.delenv("CODEX_HOME")
    mod = _load_stale_monitor()
    assert mod.CODEX_SESSIONS == tmp_path / "home" / ".codex" / "sessions"


def test_state_root_honours_dockwright_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DOCKWRIGHT_STATE_DIR", str(tmp_path / "scratch"))
    mod = _load_stale_monitor()
    assert mod.ROOT == tmp_path / "scratch"
    assert mod.ACTIVE == tmp_path / "scratch" / "active"
    monkeypatch.delenv("DOCKWRIGHT_STATE_DIR")
    mod = _load_stale_monitor()
    assert mod.ROOT == tmp_path / "home" / ".claude" / "dockwright"


def _IDLE_PROC_INDEX():
    return {"command_by_pid": {1: "/sbin/launchd"}, "child_commands": {}}


_STUB_FAMILIES = {"@opus[1m]": "claude-opus-5-5[1m]", "@sonnet": "claude-sonnet-5", "@opus": "claude-opus-5-5"}


def _stub_resolve_model_token(value):
    return _STUB_FAMILIES[value] if value.startswith("@") else value


@pytest.fixture
def stale(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_STALE_PROCESSING_MIN", raising=False)
    monkeypatch.delenv("CLAUDE_ORCH_AUTONUDGE", raising=False)
    monkeypatch.delenv("CLAUDE_ORCH_MODEL_FALLBACK", raising=False)
    monkeypatch.delenv("CLAUDE_ORCH_IDLE_UNTASKED_MIN", raising=False)
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
    monkeypatch.setattr(mod, "ASSIGNMENTS", tmp_path / "assignments", raising=False)
    monkeypatch.setattr(mod, "PANE_SENDS", tmp_path / "pane-sends", raising=False)
    monkeypatch.setattr(mod, "PERMISSION_PROMPTS", tmp_path / "permission-prompts", raising=False)
    monkeypatch.setattr(mod, "PERMISSION_LEDGER", tmp_path / "permission-answers.jsonl", raising=False)
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
    monkeypatch.setattr(mod, "_resolve_model_token", _stub_resolve_model_token)
    mod.worker_fallback_model.cache_clear()
    mod.manager_fallback_model.cache_clear()
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


def test_a_pending_question_holds_the_stale_page_until_it_is_answered(stale, capsys, monkeypatch):
    path = _write_record(stale, "w1", agent="worker", state="processing", name="worker-tab")
    stretch_start = 1_000_000
    os.utime(path, (stretch_start, stretch_start))
    clock = {"now": stretch_start}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    q = _write_question(stale, "q1", "w1")

    def run_at(elapsed_min):
        clock["now"] = stretch_start + elapsed_min * 60
        stale.main()
        return capsys.readouterr().out

    assert "STALE_PROCESSING" not in run_at(30)
    assert "STALE_PROCESSING" not in run_at(60)
    assert "STALE_PROCESSING" not in run_at(121)
    q.unlink()
    assert "STALE_PROCESSING worker-tab (130min)" in run_at(130)
    assert "STALE_PROCESSING" not in run_at(131)


@pytest.mark.parametrize("stamp", ["tasked_at", "input_at"])
@pytest.mark.parametrize("input_after_question, pages", [(True, True), (False, False)])
def test_only_a_question_of_the_current_input_holds_the_stale_page(stale, capsys, stamp, input_after_question,
                                                                   pages):
    now = int(time.time())
    _write_question(stale, "q1", "w1", asked_at=now - 3600)
    input_at = now - 3000 if input_after_question else now - 4000
    path = _write_record(stale, "w1", agent="worker", state="processing", name="worker-tab",
                         **{stamp: input_at})
    os.utime(path, (now - 2700, now - 2700))
    stale.main()
    assert ("STALE_PROCESSING worker-tab" in capsys.readouterr().out) is pages


def _iso_ts(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _write_jsonl_records(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def test_transcript_activity_is_the_last_timestamped_conversation_record(stale, tmp_path):
    log = tmp_path / "t.jsonl"
    _write_jsonl_records(log, [{"type": "assistant", "timestamp": _iso_ts(1_000_000)},
                               {"type": "last-prompt"}, {"type": "cost-state"},
                               {"type": "file-history-snapshot"}])
    os.utime(log, (1_003_000, 1_003_000))
    assert stale._transcript_activity_ts(log) == 1_000_000


def test_transcript_activity_skips_a_timestamped_record_of_a_service_type(stale, tmp_path):
    log = tmp_path / "t.jsonl"
    _write_jsonl_records(log, [{"type": "user", "timestamp": _iso_ts(1_000_000)},
                               {"type": "ai-title", "timestamp": _iso_ts(1_002_000)}])
    assert stale._transcript_activity_ts(log) == 1_000_000


def test_a_transcript_with_no_timestamps_falls_back_to_its_mtime(stale, tmp_path, monkeypatch):
    log = tmp_path / "t.jsonl"
    _write_jsonl_records(log, [{"type": "cost-state"}])
    os.utime(log, (1_000_500, 1_000_500))
    assert stale._transcript_activity_ts(log) is None
    monkeypatch.setattr(stale, "_resolve_transcript_path", lambda record, *a, **k: log)
    assert stale._last_activity({"claude_sid": "w1"}, 0)[0] == 1_000_500


def test_transcript_activity_reads_a_record_older_than_the_first_tail_chunk(stale, tmp_path):
    log = tmp_path / "t.jsonl"
    filler = [{"type": "cost-state", "pad": "x" * 1000} for _ in range(100)]
    _write_jsonl_records(log, [{"type": "assistant", "timestamp": _iso_ts(1_000_000)}] + filler)
    assert log.stat().st_size > stale.TRANSCRIPT_TAIL_BYTES[0]
    assert stale._transcript_activity_ts(log) == 1_000_000


def test_an_in_place_transcript_rewrite_does_not_rearm_the_processing_page(stale, monkeypatch, capsys):
    path = _write_record(stale, "w1", agent="worker", state="processing", name="worker-tab")
    stretch_start = 1_000_000
    os.utime(path, (stretch_start, stretch_start))
    log = stale.ROOT / "t.jsonl"
    _write_jsonl_records(log, [{"type": "assistant", "timestamp": _iso_ts(stretch_start)}])
    os.utime(log, (stretch_start, stretch_start))
    monkeypatch.setattr(stale, "_resolve_transcript_path", lambda record, *a, **k: log)
    clock = {"now": stretch_start}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])

    def run_at(elapsed_min):
        clock["now"] = stretch_start + elapsed_min * 60
        stale.main()
        return capsys.readouterr().out

    assert "STALE_PROCESSING worker-tab (30min)" in run_at(30)
    with open(log, "a") as f:
        f.write(json.dumps({"type": "cost-state"}) + "\n")
    os.utime(log, (stretch_start + 40 * 60, stretch_start + 40 * 60))
    assert "STALE_PROCESSING" not in run_at(45)
    assert "STALE_PROCESSING worker-tab (60min)" in run_at(60)


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
    assert [(pid, sig) for pid, sig in killed if sig != 0] == [], \
        "autoclose must send no real signal; signal 0 is a liveness probe"
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


def test_a_long_pending_question_pages_no_stale_question(stale, capsys):
    _write_record(stale, "w1")
    _write_question(stale, "q-old", "w1", asked_at=time.time() - 600 * 60)
    assert stale.main() == 0
    assert "STALE_QUESTION" not in capsys.readouterr().out


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

        def send_text_checked(self, window_id, text):
            calls.append(("send_text", window_id, text))
            return True

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


def test_autonudge_replaces_first_stale_processing_with_nudge(nudgy, silent, capsys, monkeypatch):
    calls = _capture_runs(nudgy, monkeypatch)
    now = int(time.time())
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    old = now - 2700
    os.utime(path, (old, old))
    rc = nudgy.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "NUDGED worker-tab (45min)" not in out and _said(silent, "NUDGED worker-tab (45min)")
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


def test_autonudge_repeats_at_each_threshold_crossing_while_wedged(nudgy, silent, capsys, monkeypatch):
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

    assert run_at(30) == "" and _said(silent, "NUDGED worker-tab (30min)")
    assert "NUDGED" not in run_at(31)
    assert run_at(60).startswith('NUDGED worker-tab (60min) → get_worker_tail("worker-tab"); ')
    assert "NUDGED worker-tab (120min)" in run_at(120)
    assert "NUDGED worker-tab (180min)" in run_at(180)
    assert "NUDGED" not in run_at(200)
    assert "NUDGED worker-tab (240min)" in run_at(240)
    assert len(_send_text_calls(calls)) == 5
    os.utime(log, (t0 + 241 * 60, t0 + 241 * 60))
    out = run_at(242)
    assert "NUDGED" not in out
    assert "RESUMED" not in out and _said(silent, "RESUMED worker-tab")
    assert len(_send_text_calls(calls)) == 5
    assert run_at(241 + 30) == "" and _said(silent, "NUDGED worker-tab (30min)")
    assert len(_send_text_calls(calls)) == 6


@pytest.mark.parametrize("banner", [THROTTLE_TEXT, SESSION_LIMIT_TEXT])
def test_autonudge_429_fires_before_threshold(nudgy, silent, capsys, monkeypatch, banner):
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
    assert "NUDGED worker-tab (6min rate-limited)" not in out and _said(silent, "NUDGED worker-tab (6min rate-limited)")
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


def test_429_renudges_on_new_stretch_while_still_throttled(nudgy, silent, capsys, monkeypatch):
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
    assert _silent_only(capsys, silent, "NUDGED worker-tab (6min rate-limited)")

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
    assert "NUDGED worker-tab (6min rate-limited)" not in out and _said(silent, "NUDGED worker-tab (6min rate-limited)"), (
        "a fresh stretch re-arms the 429 nudge — the fleet auto-revival path"
    )
    assert "STALE_PROCESSING" not in out
    assert len(_send_text_calls(calls)) == 2


def test_nudge_rearms_after_idle_observation(nudgy, silent, capsys, monkeypatch):
    calls = _capture_runs(nudgy, monkeypatch)
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    t0 = 1_000_000
    os.utime(path, (t0, t0))
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main()
    assert _silent_only(capsys, silent, "NUDGED worker-tab")

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
    assert _silent_only(capsys, silent, "NUDGED worker-tab")
    assert len(_send_text_calls(calls)) == 2


def test_no_nudge_and_no_stale_page_with_pending_question(nudgy, capsys, monkeypatch):
    calls = _capture_runs(nudgy, monkeypatch)
    now = int(time.time())
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    old = now - 2700
    os.utime(path, (old, old))
    _write_question(nudgy, "q1", "w1")
    nudgy.main()
    out = capsys.readouterr().out
    assert "STALE_PROCESSING" not in out
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


def test_scan_survives_one_workers_poison_transcript(nudgy, silent, capsys, monkeypatch):
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
    assert "NUDGED ok-tab" not in out and _said(silent, "NUDGED ok-tab"), "healthy worker must still be handled"
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


def test_resumed_emitted_once_when_transcript_grows_after_nudge(nudgy, silent, capsys, monkeypatch):
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
    assert _silent_only(capsys, silent, "NUDGED worker-tab")

    os.utime(log, (t0 + 31 * 60, t0 + 31 * 60))
    clock["now"] = t0 + 32 * 60
    nudgy.main()
    assert _silent_only(capsys, silent, "RESUMED worker-tab")
    clock["now"] = t0 + 33 * 60
    nudgy.main()
    out = capsys.readouterr().out
    assert "RESUMED" not in out, "delivery confirmation is one-shot"
    assert "NUDGED" not in out


from datetime import datetime, timedelta, timezone
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


def test_session_limit_banner_schedules_worker_nudge(nudgy, silent, capsys, monkeypatch):
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
    assert _silent_only(capsys, silent, "NUDGED worker-tab (6min rate-limited)")
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
    assert "NUDGED worker-tab (limit-reset)" not in out and _said(silent, "NUDGED worker-tab (limit-reset)")
    assert "NUDGED" not in out and not [k for k, _, _ in silent if k == "NUDGED"], (
        "scheduled fire must not double with the ladder")
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


def test_throttle_banner_without_reset_clause_schedules_nothing(nudgy, silent, capsys, monkeypatch):
    _capture_runs(nudgy, monkeypatch)
    now = int(time.time())
    path = _write_record(nudgy, "w1", agent="worker", state="processing",
                         name="worker-tab", window_id="42")
    old = now - 360
    os.utime(path, (old, old))
    log = _write_transcript(nudgy, "w1", THROTTLE_TEXT)
    os.utime(log, (old, old))
    nudgy.main()
    assert _silent_only(capsys, silent, "NUDGED worker-tab")
    assert "scheduled:w1" not in json.loads(nudgy.EMITTED_STATE.read_text())


def test_armed_schedule_suppresses_5min_relimit_nudges(nudgy, silent, capsys, monkeypatch):
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
    assert _silent_only(capsys, silent, "NUDGED worker-tab (6min rate-limited)")
    assert "scheduled:w1" in json.loads(nudgy.EMITTED_STATE.read_text())
    assert len(_send_text_calls(calls)) == 1

    t1 = t0 + 7 * 60
    os.utime(path, (t1, t1))
    _append_transcript(nudgy, "w1", SESSION_LIMIT_TEXT, t1)
    clock["now"] = t1 + 60
    nudgy.main()
    assert _silent_only(capsys, silent, "RESUMED worker-tab")

    clock["now"] = t1 + 6 * 60
    nudgy.main()
    out = capsys.readouterr().out
    assert "NUDGED" not in out, "5-min lane must be suppressed while the schedule is armed"
    assert len(_send_text_calls(calls)) == 1

    clock["now"] = t1 + 30 * 60
    nudgy.main()
    assert _silent_only(capsys, silent, "NUDGED worker-tab (30min)")
    assert len(_send_text_calls(calls)) == 2


def test_scheduled_nudge_fires_at_reset_despite_failed_retry_growth(nudgy, silent, capsys, monkeypatch):
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
    assert _silent_only(capsys, silent, "RESUMED worker-tab")
    sends_before_due = len(_send_text_calls(calls))

    clock["now"] = sched["at"]
    nudgy.main()
    out = capsys.readouterr().out
    assert "NUDGED worker-tab (limit-reset)" not in out and _said(silent, "NUDGED worker-tab (limit-reset)"), (
        "failed-retry growth must not self-cancel the reset+2min fire"
    )
    assert "NUDGED" not in out and not [k for k, _, _ in silent if k == "NUDGED"], (
        "scheduled fire must not double with the ladder")
    assert len(_send_text_calls(calls)) == sends_before_due + 1
    assert "scheduled:w1" not in json.loads(nudgy.EMITTED_STATE.read_text())


def test_scheduled_nudge_fires_when_growth_is_tool_calls_only(nudgy, silent, capsys, monkeypatch):
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
    assert _silent_only(capsys, silent, "NUDGED worker-tab (limit-reset)")
    assert len(_send_text_calls(calls)) == 2


def test_unparseable_session_limit_reset_keeps_5min_lane(nudgy, silent, capsys, monkeypatch):
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
    assert _silent_only(capsys, silent, "NUDGED worker-tab (6min rate-limited)")
    assert "scheduled:w1" not in json.loads(nudgy.EMITTED_STATE.read_text())

    t1 = t0 + 7 * 60
    os.utime(path, (t1, t1))
    _append_transcript(nudgy, "w1", banner, t1)
    clock["now"] = t1 + 6 * 60
    nudgy.main()
    out = capsys.readouterr().out
    assert "NUDGED worker-tab (6min rate-limited)" not in out and _said(silent, "NUDGED worker-tab (6min rate-limited)")
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
    assert "LIMIT_CLEARED" in out
    assert "while down: 1 workers stalled, 1 nudged, 1 done events" in out
    assert not (nudgy.ROOT / ".manager-limited-mgr-A").exists()
    assert "limited_buffer" not in json.loads(nudgy._emitted_state_path("mgr-A").read_text())

    clock["now"] = t0 + 60 * 60
    nudgy.main(manager_name="mgr-A")
    assert "NUDGED worker-tab (60min)" in capsys.readouterr().out


def test_healthy_manager_never_suppresses(nudgy, silent, capsys, monkeypatch):
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
    assert "NUDGED worker-tab (45min)" not in out and _said(silent, "NUDGED worker-tab (45min)")
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
    assert "LIMIT_CLEARED" in out
    assert "0 workers stalled, 0 nudged" in out
    assert not (nudgy.ROOT / ".manager-limited-mgr-A").exists()


def test_manager_banner_match_is_strict(nudgy, silent, capsys, monkeypatch):
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
    assert "NUDGED worker-tab (30min)" not in out and _said(silent, "NUDGED worker-tab (30min)"), "quoting manager must not suppress events"
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
    assert "LIMIT_CLEARED" in capsys.readouterr().out

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


def test_429_path_resolves_transcript_once_per_scan(nudgy, silent, monkeypatch, capsys):
    _capture_runs(nudgy, monkeypatch)
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
    assert _silent_only(capsys, silent, "NUDGED worker-tab (6min rate-limited)")
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
    assert not (stale.CLOSED / "n1.json").exists()
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


def test_account_effective_accepts_both_generation_mcp_keys(stale, monkeypatch, tmp_path, capsys):
    farm = _seed_farm(monkeypatch, tmp_path, "b")
    assert stale._account_effective("b") == ("b", farm)
    (farm / ".claude.json").write_text(json.dumps({"mcpServers": {"dockwright": {}}}))
    assert stale._account_effective("b") == ("b", farm)
    (farm / ".claude.json").write_text(json.dumps({"mcpServers": {"some-other-tool": {}}}))
    assert stale._account_effective("b") == ("a", None)
    assert "falls back to the DEFAULT login" in capsys.readouterr().err
    assert stale._format_account_prefix("b", farm) == (
        f"CLAUDE_CONFIG_DIR={shlex.quote(str(farm))} CLAUDE_ORCH_ACCOUNT=b ")
    assert stale._format_account_prefix("a", None) == "CLAUDE_ORCH_ACCOUNT=a "


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
    assert "LIMIT_CLEARED" in out
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
    assert "LIMIT_CLEARED" in out
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


def test_worker_banner_while_schedule_armed_still_flips(nudgy, silent, capsys, monkeypatch):
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
    assert _silent_only(capsys, silent, "NUDGED worker-tab (6min rate-limited)")
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


def test_worker_transient_throttle_nudges_but_never_flips(nudgy, silent, capsys, monkeypatch):
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
    assert "NUDGED worker-tab (6min rate-limited)" not in out and _said(silent, "NUDGED worker-tab (6min rate-limited)")
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


def test_worker_529_overloaded_nudges_but_never_flips(nudgy, silent, capsys, monkeypatch):
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
    assert "NUDGED worker-tab (6min rate-limited)" not in out and _said(silent, "NUDGED worker-tab (6min rate-limited)")
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
    assert "AUTH_401_ESCALATED" in out3 and "the engineer must run claude, then /login" in out3
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
    assert "AUTH_401_ESCALATED" in out and "the engineer must run claude, then /login" in out
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
    assert "LIMIT_CLEARED" in out
    assert "AUTH_401_ESCALATED" in out and "the engineer must run claude, then /login" in out
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
    assert "AUTH_401_ESCALATED" in out and "the engineer must run claude, then /login" in out, "page reaches the human"
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
        calls.append(list(a))
        if "capture-pane" in a:
            return _sp.CompletedProcess(a, 0, "\u276f \n", "")
        return _sp.CompletedProcess(a, 0, b"", b"")
    monkeypatch.setattr(_sp, "run", fake)
    assert stale._send_text("%5", "resume your task") is True
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


def test_launch_recovery_manager_pins_cwd_to_manager_home(stale, monkeypatch, tmp_path):
    captured = {}

    class FakeDrv:
        async def spawn(self, **kw):
            captured.update(kw)
            return "%9"

    mgr_home = tmp_path / "mgr-home"
    monkeypatch.setattr(stale, "_get_driver", lambda: FakeDrv())
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(mgr_home))
    rec = {"cwd": "/c", "name": "m", "window_id": "%14"}
    assert stale._launch_recovery_manager(rec, "sid-1", "a") == "%9"
    assert captured["cwd"] == str(mgr_home)
    assert mgr_home.is_dir()


def test_launch_recovery_manager_refuses_when_manager_home_cannot_be_created(stale, monkeypatch, tmp_path, capsys):
    spawned = []

    class FakeDrv:
        async def spawn(self, **kw):
            spawned.append(kw)
            return "%9"

    parent_file = tmp_path / "afile"
    parent_file.write_text("x")
    monkeypatch.setattr(stale, "_get_driver", lambda: FakeDrv())
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(parent_file / "manager"))
    assert stale._launch_recovery_manager({"cwd": "/c", "name": "m"}, "sid-1", "a") is None
    assert spawned == []
    assert "recovery launch refused" in capsys.readouterr().err


def test_launch_recovery_manager_skips_without_manager_home_helper(stale, monkeypatch, capsys):
    spawned = []

    class FakeDrv:
        async def spawn(self, **kw):
            spawned.append(kw)
            return "%9"

    monkeypatch.setattr(stale, "_get_driver", lambda: FakeDrv())
    monkeypatch.setattr(stale, "_ensure_manager_home", None)
    assert stale._launch_recovery_manager({"cwd": "/c", "name": "m"}, "sid-1", "a") is None
    assert spawned == []
    assert "recovery launch skipped" in capsys.readouterr().err


def test_launch_recovery_manager_pins_manager_model(stale, monkeypatch):
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
    assert f"--model {shlex.quote(stale.manager_fallback_model())}" in inner, (
        "the recovery lane must launch on the same manager fallback the credit lane "
        "types, so one family map moves both")
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
    assert inner.index("DOCKWRIGHT_PENDING_TAKEOVER=1") < inner.index(" launch ")


def test_recovery_launch_carries_manager_settings(stale, monkeypatch, tmp_path):
    from dockwright import paths as dw_paths
    presets = tmp_path / "presets"
    presets.mkdir(parents=True, exist_ok=True)
    settings = presets / "manager-settings.json"
    settings.write_text("{}")
    monkeypatch.setattr(dw_paths, "PRESETS", presets)
    captured = {}

    class FakeDrv:
        async def spawn(self, **kw):
            captured.update(kw)
            return "%42"

    monkeypatch.setattr(stale, "_get_driver", lambda: FakeDrv())
    stale._launch_recovery_manager({"cwd": "/c", "name": "mgr-x"}, "sid123", "a")
    inner = captured["argv"][-1]
    assert f" launch --remote-control --settings {str(settings)} {MGR_DENY} --model" in inner, inner
    assert "/manager-takeover-recovery sid123" in inner


def test_recovery_launch_carries_the_manager_role(stale, monkeypatch, tmp_path):
    from dockwright import paths as dw_paths
    role_dir = tmp_path / "roles" / "Manager"
    role_dir.mkdir(parents=True)
    (role_dir / "role.toml").write_text('core = "manager"\nlaunch = ["manager"]\n')
    (role_dir / "role.manager.md").write_text("<!-- dockwright-role: Manager launch=manager -->\n")
    (role_dir / "role.settings.json").write_text('{"skillOverrides": {"skill-x": "name-only"}}')
    monkeypatch.setattr(dw_paths, "ROLES_DIR", tmp_path / "roles")
    captured = {}

    class FakeDrv:
        async def spawn(self, **kw):
            captured.update(kw)
            return "%42"

    monkeypatch.setattr(stale, "_get_driver", lambda: FakeDrv())
    stale._launch_recovery_manager({"cwd": "/c", "name": "m"}, "sid-1", "a")
    inner = captured["argv"][-1]
    assert f"--append-system-prompt-file {role_dir / 'role.manager.md'}" in inner, inner
    assert inner.index("--append-system-prompt-file") < inner.index("/manager-takeover-recovery")
    assert f" {shlex.quote(str(dw_paths.dockwright_bin()))} launch --" in inner, inner
    assert inner.index(" launch ") < inner.index("--append-system-prompt-file")


def test_recovery_launch_without_a_dockwright_binary_falls_back_to_bare_claude(stale, monkeypatch, tmp_path, capsys):
    from dockwright import paths as dw_paths
    role_dir = tmp_path / "roles" / "Manager"
    role_dir.mkdir(parents=True)
    (role_dir / "role.toml").write_text('core = "manager"\nlaunch = ["manager"]\n')
    (role_dir / "role.manager.md").write_text("<!-- dockwright-role: Manager launch=manager -->\n")
    (role_dir / "role.settings.json").write_text("{}")
    (tmp_path / "roles" / "_agents").mkdir()
    (tmp_path / "roles" / "_agents" / "none.json").write_text("{}")
    monkeypatch.setattr(dw_paths, "ROLES_DIR", tmp_path / "roles")
    monkeypatch.setattr(stale, "_dockwright_bin", None)
    monkeypatch.setenv("DOCKWRIGHT_BIN", str(tmp_path / "bin" / "dockwright"))
    captured = {}

    class FakeDrv:
        async def spawn(self, **kw):
            captured.update(kw)
            return "%42"

    monkeypatch.setattr(stale, "_get_driver", lambda: FakeDrv())
    stale._launch_recovery_manager({"cwd": "/c", "name": "m"}, "sid-1", "a")
    inner = captured["argv"][-1]
    assert f"{tmp_path / 'bin' / 'dockwright'} launch " in inner and "--agents-file" in inner
    monkeypatch.delenv("DOCKWRIGHT_BIN")
    stale._launch_recovery_manager({"cwd": "/c", "name": "m"}, "sid-1", "a")
    inner = captured["argv"][-1]
    assert " claude --" in inner and "--agents-file" not in inner and " launch " not in inner
    assert "no dockwright binary" in capsys.readouterr().err


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
    assert f"--remote-control {MGR_DENY} --model" in inner, inner


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
    tokens = shlex.split(captured["argv"][-1])
    assert json.loads(tokens[tokens.index("--settings") + 1])["remoteControlAtStartup"] is False


def test_recovery_launch_skips_without_the_rc_resolver(stale, monkeypatch, capsys):
    monkeypatch.setattr(stale, "_manager_rc_args", None)
    spawned = []

    class FakeDrv:
        async def spawn(self, **kw):
            spawned.append(kw)
            return "%9"

    monkeypatch.setattr(stale, "_get_driver", lambda: FakeDrv())
    assert stale._launch_recovery_manager({"cwd": "/c", "name": "m"}, "sid-1", "a") is None
    assert spawned == []
    assert "dockwright.manager_launch unavailable" in capsys.readouterr().err


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
    assert f"--remote-control {MGR_DENY} --dangerously-skip-permissions --model" in inner, inner
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
        {"line": line, "kind": "gardener", "buffered_at": buffered_at}))
    return outbox / fname


def _make_idle_worker_past_threshold(stale, sid="wkr-idle", manager="mgr", last_turn=None):
    epoch = last_turn if last_turn is not None else int(time.time()) - 10_000
    return _write_record(
        stale, sid, parent_manager_name=manager,
        last_turn_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)))


def _assert_closed_idle(stale, path, sid="wkr-idle"):
    assert not path.exists()
    closed = json.loads((stale.CLOSED / f"{sid}.json").read_text())
    assert closed["closed_reason"].startswith("idle>")


def test_autoclose_is_silent_when_scan_otherwise_silent(stale, capsys):
    path = _make_idle_worker_past_threshold(stale)
    rc = stale.main(manager_name="mgr")
    assert rc == 0
    assert capsys.readouterr().out == ""
    assert _outbox_entries(stale) == []
    _assert_closed_idle(stale, path)


def test_autoclose_stays_silent_when_other_lines_print(stale, capsys):
    path = _make_idle_worker_past_threshold(stale)
    now = int(time.time())
    _stale_processing_worker(stale, "p-worker", "p-tab", "mgr", now)
    stale.main(manager_name="mgr")
    out = capsys.readouterr().out
    assert "STALE_PROCESSING p-tab" in out
    assert all(l.startswith("STALE_PROCESSING") for l in out.splitlines() if l.strip())
    assert _outbox_entries(stale) == []
    _assert_closed_idle(stale, path)


def test_timeout_flush_after_max_hold(stale, capsys):
    now = time.time()
    _seed_outbox(stale, "gardener digest ready: run-lonely", now - stale.OUTBOX_MAX_HOLD_SEC - 60)
    stale.main(manager_name="mgr")
    assert "gardener digest ready: run-lonely" in capsys.readouterr().out
    assert _outbox_entries(stale) == []


def test_no_timeout_flush_before_max_hold(stale, capsys):
    now = time.time()
    entry = _seed_outbox(stale, "gardener digest ready: run-young", now - 60)
    stale.main(manager_name="mgr")
    assert capsys.readouterr().out == ""
    assert entry.exists()


def test_timeout_uses_mtime_when_buffered_at_missing(stale, capsys):
    now = time.time()
    outbox = stale.ROOT / "notify-outbox" / "mgr"
    outbox.mkdir(parents=True, exist_ok=True)
    entry = outbox / "0000000000000-0-0.json"
    entry.write_text(json.dumps({"line": "gardener digest ready: run-legacy", "kind": "gardener"}))
    os.utime(entry, (now - stale.OUTBOX_MAX_HOLD_SEC - 60,) * 2)
    stale.main(manager_name="mgr")
    assert "gardener digest ready: run-legacy" in capsys.readouterr().out


def test_limited_manager_holds_outbox_and_closes_silently(stale, capsys, monkeypatch):
    t0 = 1_000_000
    clock = {"now": t0 + 30 * 60}
    held = _seed_outbox(stale, "gardener digest ready: run-preexisting",
                        clock["now"] - stale.OUTBOX_MAX_HOLD_SEC - 60)
    path = _make_idle_worker_past_threshold(stale, last_turn=t0)
    _write_limited_manager(stale, "mgr1", "mgr", t0)
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr")
    assert capsys.readouterr().out == ""
    assert held.exists()
    _assert_closed_idle(stale, path)
    emitted = json.loads((stale.ROOT / ".stale-emitted-mgr.json").read_text())
    assert not emitted["limited_buffer"].get("autoclosed")
    assert not emitted["limited_buffer"].get("orphaned")


def test_recovery_rollup_drains_outbox(stale, capsys):
    now = int(time.time())
    _seed_outbox(stale, "gardener digest ready: run-heldover", now - 120)
    stale._emitted_state_path("mgr").write_text(json.dumps({"limited_buffer": {
        "since": now - 1800, "stalled_names": ["worker-tab"], "nudged": 1,
        "resumed": 0, "questions": 0, "orphaned": 0, "suppressed_keys": []}}))
    (stale.ROOT / ".manager-limited-mgr").touch()
    stale.main(manager_name="mgr")
    out = capsys.readouterr().out
    assert "LIMIT_CLEARED" in out
    assert "gardener digest ready: run-heldover" in out
    assert out.index("LIMIT_CLEARED") < out.index("gardener digest ready: run-heldover")


def test_recovery_rollup_names_orphans_and_never_autoclosed(stale, capsys):
    now = int(time.time())
    stale._emitted_state_path("mgr").write_text(json.dumps({"limited_buffer": {
        "since": now - 1800, "stalled_names": [], "nudged": 0,
        "resumed": 0, "orphaned": 2, "autoclosed": 3, "suppressed_keys": []}}))
    (stale.ROOT / ".manager-limited-mgr").touch()
    stale.main(manager_name="mgr")
    out = capsys.readouterr().out
    assert ", 2 orphaned children closed" in out
    assert "autoclosed" not in out


def test_unscoped_run_autocloses_silently(stale, capsys):
    path = _make_idle_worker_past_threshold(stale, manager=None)
    stale.main(manager_name=None)
    assert capsys.readouterr().out == ""
    assert not (stale.ROOT / "notify-outbox").exists()
    _assert_closed_idle(stale, path)


def test_root_prefers_dockwright_home(tmp_path, monkeypatch):
    monkeypatch.delenv("DOCKWRIGHT_STATE_DIR", raising=False)
    (tmp_path / ".claude" / "dockwright").mkdir(parents=True)
    (tmp_path / ".claude" / "orchestrator").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    mod = _load_stale_monitor()
    assert mod.ROOT == tmp_path / ".claude" / "dockwright"
    assert mod._LEGACY_ROOT == tmp_path / ".claude" / "orchestrator"


def test_root_falls_back_to_legacy_home(tmp_path, monkeypatch):
    monkeypatch.delenv("DOCKWRIGHT_STATE_DIR", raising=False)
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
    assert out.rstrip().endswith(" — no backing record")


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
            "resumed": 0, "questions": 0, "orphaned": 0,
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
    monkeypatch.setattr(stale_monitor, "PERMISSION_PROMPTS", tmp_path / "permission-prompts")
    monkeypatch.setattr(stale_monitor, "CLAUDE_PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(stale_monitor, "_get_driver", lambda: FakeDriver(screens))
    events = []
    def emit(kind, name, lane_kind, fields, dedup_key=None, sid=None):
        events.append((kind, name, stale_monitor._render_line(lane_kind, fields), dedup_key))
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
    assert line.startswith("APPROVAL_PROMPT zeb4 → ")
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


def test_the_classic_page_does_not_repage_on_the_ladder(monkeypatch, tmp_path):
    active, pending, events, next_emitted, emitted, emit, now = _run_approval_scan(
        monkeypatch, tmp_path, {"%7": PROCEED_DIALOG})
    _worker_record(active, "sid1", "zeb4", "%7")
    stale_monitor._scan_approval_prompts("mgr-x", now, {}, next_emitted, emit)
    first_key = events[0][3]
    events.clear()
    later_state = dict(next_emitted); next2 = {}
    stale_monitor._scan_approval_prompts("mgr-x", now + 360, later_state, next2, emit)
    assert events == []
    assert next2[first_key]["paged"] == 1


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
                        lambda record, log=None, records=None: skipped_upstream)
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

    assert _autoclose_orphaned_workers([(path, record)]) == [False]
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
        assert lane(path, record, 150) is False, f"lane {i} was not refused"
        assert path.exists(), f"lane {i} closed a busy worker"


def test_new_close_lane_still_closes_a_worker_that_is_not_busy(stale, monkeypatch):
    _arm_busy_scan(stale, monkeypatch, _proc_index(_MCP_CHILDREN))
    path = _busy_worker(stale, 150)
    record = json.loads(path.read_text())
    assert stale._autoclose_idle_worker(path, record, 150) is True
    assert not path.exists()


def test_new_close_lane_closes_past_the_cap_even_when_busy(stale, monkeypatch):
    _arm_busy_scan(stale, monkeypatch,
                   _proc_index((*_MCP_CHILDREN, _BASH_TOOL_SHELL)))
    path = _busy_worker(stale, 400)
    record = json.loads(path.read_text())
    assert stale._autoclose_idle_worker(path, record, 400) is True
    assert not path.exists()


def test_the_closer_resolves_the_index_itself(stale, monkeypatch):
    calls = []
    _arm_busy_scan(stale, monkeypatch, None)
    monkeypatch.setattr(
        stale, "_process_index",
        lambda: calls.append(1) or _proc_index((*_MCP_CHILDREN, _BASH_TOOL_SHELL)))
    path = _busy_worker(stale, 150)
    record = json.loads(path.read_text())
    assert stale._autoclose_idle_worker(path, record, 150) is False
    assert calls == [1], "the closer must resolve the index itself"
    assert path.exists()


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
    assert index["parent_by_pid"] == {_WORKER_PID: 1, _WORKER_PID + 1: _WORKER_PID}


def test_reported_since_input_reads_the_done_dir_under_the_root_it_is_given(stale, tmp_path):
    record = {"parent_name": "mgr", "tasked_at": 100.0}
    done = stale.ROOT / "done" / "mgr"
    done.mkdir(parents=True, exist_ok=True)
    (done / "w1-e1.json").write_text(json.dumps({"claude_sid": "w1", "completed_at": 200.0}))
    assert stale._reported_since_input(record, "w1") is True
    assert stale._reported_since_input(record, "w1", root=stale.ROOT) is True
    assert stale._reported_since_input(record, "w1", root=tmp_path / "elsewhere") is False
    assert stale._reported_since_input({**record, "input_at": 300.0}, "w1", root=stale.ROOT) is False


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

    assert stale._autoclose_idle_worker(rec, record, inside_cap) is False
    assert rec.exists(), "the closer must refuse a record whose transcript is live"
    assert not (stale.CLOSED / "s1.json").exists()


def test_a_direct_closer_call_still_closes_past_the_cap(stale, tmp_path, monkeypatch):
    rec = _write_record(stale, "s1", transcript_path=str(_transcript(tmp_path, 3.0)))
    record = json.loads(rec.read_text())
    _no_real_windows(stale, monkeypatch)
    past_cap = stale._busy_shell_deadline() + 500

    assert stale._autoclose_idle_worker(rec, record, past_cap) is True
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
            rec, record, stale._busy_shell_deadline() - 500) is False
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
            rec, record, stale._busy_shell_deadline() + 500) is True
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
        rec, record, stale._busy_shell_deadline() - 500) is True
    assert not rec.exists()


def _idle_worker(stale, sid, name, idle_min, uptime=1_000_000, **overrides):
    record = {
        "name": name,
        "state": "idle",
        "parent_manager_name": None,
        "last_turn_at_uptime": uptime - idle_min * 60,
        "processing_since": 1_000_000 - (idle_min + 60) * 60,
        "window_id": "%9",
    }
    record.update(overrides)
    return _write_record(stale, sid, **record)


@pytest.fixture
def idler(nudgy, monkeypatch):
    monkeypatch.setattr(nudgy, "_awake_seconds", lambda: 1_000_000)
    monkeypatch.setattr(nudgy, "IDLE_THRESHOLD_SEC", 7200)
    monkeypatch.setattr(nudgy, "IDLE_UNTASKED_MIN", 15)
    monkeypatch.setattr(nudgy, "IDLE_UNTASKED_SEC", 15 * 60)
    return nudgy


def test_idle_untasked_nudges_once_and_says_so(idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "stalled", 15)
    idler.main()
    out = capsys.readouterr().out
    assert "IDLE_NUDGED stalled" not in out and _said(silent, "IDLE_NUDGED stalled")
    sent = _send_text_calls(calls)
    assert len(sent) == 1, f"expected exactly one typed nudge, got {sent}"
    assert sent[0][0] == "%9"
    assert "worker_done" in sent[0][1], "the text must cover the finished-worker case too"


def test_idle_untasked_never_repeats_for_the_same_worker(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    path = _idle_worker(idler, "w1", "stalled", 15)
    idler.main()
    capsys.readouterr()
    for later in (16, 30, 61, 200):
        _idle_worker(idler, "w1", "stalled", later)
        idler.main()
        assert "IDLE_NUDGED" not in capsys.readouterr().out, f"re-paged at {later}min"
    assert len(_send_text_calls(calls)) == 1, "one stall must buy exactly one nudge"
    assert path.exists()


def test_idle_untasked_silent_before_the_threshold(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "stalled", 14)
    idler.main()
    assert "IDLE_NUDGED" not in capsys.readouterr().out
    assert _send_text_calls(calls) == []


def test_idle_untasked_re_arms_only_after_a_real_retask(idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    clock = {"now": 2_000_000}
    monkeypatch.setattr(idler.time, "time", lambda: clock["now"])

    _idle_worker(idler, "w1", "stalled", 15, processing_since=1_900_000)
    idler.main()
    assert _silent_only(capsys, silent, "IDLE_NUDGED stalled")

    clock["now"] = 2_000_600
    _idle_worker(idler, "w1", "stalled", 25, processing_since=1_900_000)
    idler.main()
    assert "IDLE_NUDGED" not in capsys.readouterr().out, "same stall re-paged"

    clock["now"] = 2_001_200
    _idle_worker(idler, "w1", "stalled", 15, tasked_at=2_000_900,
                 processing_since=1_900_000)
    idler.main()
    assert _silent_only(capsys, silent, "IDLE_NUDGED stalled"), (
        "a real re-task must re-arm the one-nudge budget")
    assert len(_send_text_calls(calls)) == 2


def test_idle_untasked_skips_a_worker_with_a_pending_question(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "stalled", 30)
    _write_question(idler, "q1", "w1")
    idler.main()
    assert "IDLE_NUDGED" not in capsys.readouterr().out
    assert _send_text_calls(calls) == []


def test_idle_untasked_skips_a_just_retasked_worker(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "stalled", 30, tasked_at=time.time())
    idler.main()
    assert "IDLE_NUDGED" not in capsys.readouterr().out
    assert _send_text_calls(calls) == []


def test_idle_untasked_skips_a_processing_worker(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "stalled", 30, state="processing")
    idler.main()
    assert "IDLE_NUDGED" not in capsys.readouterr().out
    assert _send_text_calls(calls) == []


def test_idle_untasked_skips_a_manager_record(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "m1", "boss", 30, agent="manager")
    idler.main()
    assert "IDLE_NUDGED" not in capsys.readouterr().out
    assert _send_text_calls(calls) == []


def _busy_index(pid):
    return {
        "command_by_pid": {pid: "claude --model x"},
        "child_commands": {pid: ["/bin/zsh -c source /x/shell-snapshots/snapshot-a.sh && run"]},
    }


def test_idle_untasked_holds_a_worker_with_live_background_work(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    monkeypatch.setattr(idler, "_process_index", lambda: _busy_index(4242))
    _idle_worker(idler, "w1", "parked", 30, pid=4242)
    idler.main()
    assert "IDLE_NUDGED" not in capsys.readouterr().out
    assert _send_text_calls(calls) == [], "a design-gate park must not be nudged"


def test_idle_untasked_wakes_a_background_worker_past_the_ceiling(idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    monkeypatch.setattr(idler, "_process_index", lambda: _busy_index(4242))
    _idle_worker(idler, "w1", "stuckshell", 121, pid=4242)
    idler.main()
    assert _silent_only(capsys, silent, "IDLE_NUDGED stuckshell")
    assert len(_send_text_calls(calls)) == 1


def test_idle_untasked_holds_when_the_process_table_is_unreadable(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    monkeypatch.setattr(idler, "_process_index", lambda: None)
    _idle_worker(idler, "w1", "unknown", 30, pid=4242)
    idler.main()
    assert "IDLE_NUDGED" not in capsys.readouterr().out
    assert _send_text_calls(calls) == []


def test_idle_untasked_skips_a_rate_limited_worker(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    monkeypatch.setattr(idler, "_is_rate_limited", lambda record: True)
    _idle_worker(idler, "w1", "bricked", 30)
    idler.main()
    assert "IDLE_NUDGED" not in capsys.readouterr().out
    assert _send_text_calls(calls) == [], "a session on a limit banner cannot resume"


def test_idle_untasked_pages_when_there_is_no_window(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "windowless", 30, window_id="")
    idler.main()
    out = capsys.readouterr().out
    assert "IDLE_UNTASKED windowless" in out
    assert _send_text_calls(calls) == []


def test_idle_untasked_pages_instead_of_typing_when_autonudge_is_off(stale, monkeypatch, capsys):
    monkeypatch.setattr(stale, "_awake_seconds", lambda: 1_000_000)
    monkeypatch.setattr(stale, "IDLE_THRESHOLD_SEC", 7200)
    calls = _capture_runs(stale, monkeypatch)
    _idle_worker(stale, "w1", "quiet", 46)
    stale.main()
    out = capsys.readouterr().out
    assert "IDLE_UNTASKED quiet" in out
    assert _send_text_calls(calls) == [], "autonudge off must never type"


def _write_done_event(stale, sid, completed_at, parent=None):
    bucket = parent or "_unscoped"
    done_dir = stale.ROOT / "done" / bucket
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / f"{sid}-e1.json").write_text(json.dumps(
        {"claude_sid": sid, "completed_at": completed_at}))


def test_a_worker_that_reported_done_after_its_last_task_is_not_nudged(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "parked", 60, tasked_at=time.time() - 7200)
    _write_done_event(idler, "w1", time.time() - 3600)
    idler.main()
    out = capsys.readouterr().out
    assert "IDLE_NUDGED" not in out and "IDLE_UNTASKED" not in out
    assert _send_text_calls(calls) == []


def test_a_managed_worker_that_reported_done_is_not_nudged(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "parked", 60, parent_manager_name="mgr",
                 tasked_at=time.time() - 7200)
    _write_done_event(idler, "w1", time.time() - 3600, parent="mgr")
    idler.main()
    assert _send_text_calls(calls) == []


def test_a_message_after_the_done_brings_the_nudge_back(idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _write_done_event(idler, "w1", time.time() - 7200)
    _idle_worker(idler, "w1", "parked", 60, tasked_at=time.time() - 3600)
    idler.main()
    assert _silent_only(capsys, silent, "IDLE_NUDGED parked")
    assert len(_send_text_calls(calls)) == 1


def test_his_typed_line_after_the_done_brings_the_nudge_back(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _write_done_event(idler, "w1", time.time() - 7200)
    _idle_worker(idler, "w1", "parked", 60, tasked_at=time.time() - 9000,
                 input_at=time.time() - 3600)
    idler.main()
    assert len(_send_text_calls(calls)) == 1


def test_a_spawned_worker_with_no_tasked_at_that_reported_done_is_not_nudged(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "parked", 60)
    _write_done_event(idler, "w1", time.time() - 3600)
    idler.main()
    assert _send_text_calls(calls) == []


def test_a_blocked_worker_past_the_ttl_is_not_autoclosed(idler, monkeypatch, capsys):
    _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "asker", 200)
    _write_question(idler, "q1", "w1")
    idler.main()
    assert (idler.ACTIVE / "w1.json").exists()


def test_a_reported_worker_is_still_autoclosed_at_the_ttl(idler, monkeypatch, capsys):
    _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "parked", 200)
    _write_done_event(idler, "w1", time.time() - 3 * 3600)
    idler.main()
    assert not (idler.ACTIVE / "w1.json").exists()


def test_the_idle_nudge_waits_45_minutes_by_default(stale, monkeypatch, capsys):
    monkeypatch.setattr(stale, "AUTONUDGE", True)
    monkeypatch.setattr(stale, "_awake_seconds", lambda: 1_000_000)
    monkeypatch.setattr(stale, "IDLE_THRESHOLD_SEC", 7200)
    calls = _capture_runs(stale, monkeypatch)
    _idle_worker(stale, "w1", "stalled", 44)
    stale.main()
    assert _send_text_calls(calls) == []
    _idle_worker(stale, "w1", "stalled", 46)
    stale.main()
    assert len(_send_text_calls(calls)) == 1


def test_idle_untasked_survives_a_suppressed_scan_without_repaging(idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "flappy", 30, pid=4242)
    idler.main()
    assert _silent_only(capsys, silent, "IDLE_NUDGED flappy")
    monkeypatch.setattr(idler, "_process_index", lambda: _busy_index(4242))
    idler.main()
    capsys.readouterr()
    monkeypatch.setattr(idler, "_process_index", _IDLE_PROC_INDEX)
    idler.main()
    assert "IDLE_NUDGED" not in capsys.readouterr().out, "a flapping suppressor re-paged"
    assert len(_send_text_calls(calls)) == 1


def test_idle_untasked_yields_to_autoclose_in_the_same_scan(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    monkeypatch.setattr(idler, "IDLE_THRESHOLD_SEC", 600)
    _idle_worker(idler, "w1", "doomed", 30)
    idler.main()
    out = capsys.readouterr().out
    assert (idler.CLOSED / "w1.json").exists()
    assert "IDLE_NUDGED" not in out, "do not wake a worker this scan will close"


def test_idle_untasked_ignores_the_hourly_autoclose_cadence(idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    idler.EMITTED_STATE.write_text(json.dumps({"last_autoclose_run": time.time()}))
    _idle_worker(idler, "w1", "stalled", 15)
    idler.main()
    assert _silent_only(capsys, silent, "IDLE_NUDGED stalled")
    assert len(_send_text_calls(calls)) == 1


def test_idle_untasked_keys_two_workers_separately(idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "first", 15, window_id="%1")
    _idle_worker(idler, "w2", "second", 15, window_id="%2")
    idler.main()
    assert "IDLE_NUDGED" not in capsys.readouterr().out
    assert sorted(f["name"] for kind, f, _ in silent if kind == "IDLE_NUDGED") == ["first", "second"]
    assert sorted(w for w, _ in _send_text_calls(calls)) == ["%1", "%2"]


def test_idle_untasked_does_not_loop_on_its_own_echo(idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    clock = {"now": 3_000_000}
    uptime = {"now": 900_000}
    monkeypatch.setattr(idler.time, "time", lambda: clock["now"])
    monkeypatch.setattr(idler, "_awake_seconds", lambda: uptime["now"])

    _idle_worker(idler, "w1", "loopy", 15, uptime=uptime["now"],
                 processing_since=2_990_000)
    idler.main()
    assert _silent_only(capsys, silent, "IDLE_NUDGED loopy")

    echo_lands_3s_after_the_send_so_the_grace_needs_headroom = clock["now"] + 3
    for step in range(1, 11):
        clock["now"] = 3_000_000 + step * 15 * 60
        uptime["now"] = 900_000 + step * 15 * 60
        _idle_worker(idler, "w1", "loopy", 15, uptime=uptime["now"],
                     processing_since=echo_lands_3s_after_the_send_so_the_grace_needs_headroom)
        idler.main()
        capsys.readouterr()

    assert len(_send_text_calls(calls)) == 1, (
        f"the nudge re-armed on its own echo: {len(_send_text_calls(calls))} sends. "
        "Enter submits, so the nudge moves processing_since and the reply resets the "
        "idle anchor; neither is a re-task and neither may buy a second send")


def test_idle_untasked_keeps_the_budget_across_a_sub_threshold_scan(idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "quick", 15)
    idler.main()
    assert _silent_only(capsys, silent, "IDLE_NUDGED quick")
    _idle_worker(idler, "w1", "quick", 1)
    idler.main()
    capsys.readouterr()
    _idle_worker(idler, "w1", "quick", 20)
    idler.main()
    assert "IDLE_NUDGED" not in capsys.readouterr().out, (
        "a dip below the threshold pruned the key and re-armed the nudge")
    assert len(_send_text_calls(calls)) == 1


def test_idle_untasked_pages_when_the_pane_refuses_input(idler, monkeypatch, capsys):
    class DeadPane:
        def send_text_checked(self, window_id, text):
            return False

        def send_text(self, window_id, text, submit=True):
            raise AssertionError("must not fall back to an unchecked send")

        def close(self, window_id):
            pass

    monkeypatch.setattr(idler, "_get_driver", lambda: DeadPane())
    _idle_worker(idler, "w1", "gone", 30)
    idler.main()
    out = capsys.readouterr().out
    assert "IDLE_UNTASKED gone" in out
    assert "typed resume-or-finish" not in out, "claimed a send the pane refused"


def test_idle_untasked_page_survives_a_manager_limit_window(idler, monkeypatch, capsys):
    t0 = 1_000_000
    _write_limited_manager(idler, "mgr1", "mgr-A", t0)
    monkeypatch.setattr(idler, "_awake_seconds", lambda: 900_000)
    _idle_worker(idler, "w1", "quiet", 30, uptime=900_000, window_id="",
                 parent_manager_name="mgr-A", processing_since=t0 - 3600)
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(idler.time, "time", lambda: clock["now"])

    idler.main(manager_name="mgr-A")
    assert "IDLE_UNTASKED" not in capsys.readouterr().out, "not buffered while limited"

    ledger = json.loads(idler._emitted_state_path("mgr-A").read_text())
    buffered = (ledger.get("limited_buffer") or {}).get("suppressed_keys") or []
    assert any(k.startswith("untasked-page:") for k in buffered), (
        f"the page was not buffered for replay: {buffered}")
    assert all(k in ledger for k in buffered if k.startswith("untasked-page:")), (
        "a buffered key the ledger does not hold pops nothing on replay")


def test_idle_untasked_records_the_send_before_making_it(idler, monkeypatch, capsys):
    seen_on_disk = {}

    class WriteAheadProbe:
        def send_text_checked(self, window_id, text):
            try:
                seen_on_disk["cursor"] = json.loads(idler.EMITTED_STATE.read_text())
            except OSError:
                seen_on_disk["cursor"] = None
            return True

        def send_text(self, window_id, text, submit=True):
            raise AssertionError("must use the checked send")

        def close(self, window_id):
            pass

    monkeypatch.setattr(idler, "_get_driver", lambda: WriteAheadProbe())
    _idle_worker(idler, "w1", "ahead", 15)
    idler.main()
    assert "cursor" in seen_on_disk, "send never happened"
    assert any(k.startswith("untasked:") for k in (seen_on_disk["cursor"] or {})), (
        "the nudge was typed before its key reached disk — a kill here types twice")


def test_idle_untasked_falls_back_to_the_legacy_pane_field(idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "legacy", 15, window_id="", iterm_sid="%legacy")
    idler.main()
    assert _silent_only(capsys, silent, "IDLE_NUDGED legacy")
    assert _send_text_calls(calls) == [("%legacy", idler.IDLE_NUDGE_TEXT)]



def test_idle_untasked_refused_pane_releases_the_budget_and_records_the_page(idler, monkeypatch, capsys):
    class DeadPane:
        def send_text_checked(self, window_id, text):
            return False

        def send_text(self, window_id, text, submit=True):
            raise AssertionError("must use the checked send")

        def close(self, window_id):
            pass

    monkeypatch.setattr(idler, "_get_driver", lambda: DeadPane())
    _idle_worker(idler, "w1", "deadpane", 30)
    idler.main()
    assert "IDLE_UNTASKED deadpane" in capsys.readouterr().out
    ledger = json.loads(idler.EMITTED_STATE.read_text())
    assert "untasked:w1" not in ledger, (
        "a refused send spent the nudge budget: nothing typed and nobody can retry")
    assert ledger.get("untasked-page:w1") is not None, (
        "the page was emitted with a dedup key the ledger never recorded")


def test_idle_untasked_re_arms_immediately_on_a_manager_retask(idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    clock = {"now": 4_000_000}
    uptime = {"now": 900_000}
    monkeypatch.setattr(idler.time, "time", lambda: clock["now"])
    monkeypatch.setattr(idler, "_awake_seconds", lambda: uptime["now"])

    _idle_worker(idler, "w1", "retasked", 15, uptime=uptime["now"],
                 processing_since=3_990_000)
    idler.main()
    assert _silent_only(capsys, silent, "IDLE_NUDGED retasked")

    clock["now"] = 4_000_060 + 30 * 60
    uptime["now"] = 900_000 + 30 * 60
    _idle_worker(idler, "w1", "retasked", 20, uptime=uptime["now"],
                 tasked_at=4_000_060, processing_since=4_000_061)
    idler.main()
    assert _silent_only(capsys, silent, "IDLE_NUDGED retasked"), (
        "a real re-task inside the grace window lost the whole episode")
    assert len(_send_text_calls(calls)) == 2


def test_idle_untasked_holds_through_the_retask_delivery_race(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    clock = {"now": 6_000_000}
    monkeypatch.setattr(idler.time, "time", lambda: clock["now"])
    _idle_worker(idler, "w1", "justtasked", 40,
                 tasked_at=clock["now"] - 60, processing_since=5_900_000)
    idler.main()
    assert "IDLE_NUDGED" not in capsys.readouterr().out, (
        "nudged a worker the manager re-tasked 60s ago: send_manager_to_worker stamps "
        "tasked_at before the CLI fires UserPromptSubmit, so the record still reads idle "
        "and the episode guard needs width to cover that gap")
    assert _send_text_calls(calls) == []


def test_idle_untasked_commits_the_budget_release_before_returning(idler, monkeypatch, capsys):
    commits = []

    class DeadPane:
        def send_text_checked(self, window_id, text):
            return False

        def send_text(self, window_id, text, submit=True):
            raise AssertionError("must use the checked send")

        def close(self, window_id):
            pass

    real_commit = idler._commit_actions_only

    def watching_commit(path, emitted, next_emitted):
        real_commit(path, emitted, next_emitted)
        try:
            commits.append(json.loads(path.read_text()))
        except OSError:
            pass

    monkeypatch.setattr(idler, "_get_driver", lambda: DeadPane())
    monkeypatch.setattr(idler, "_commit_actions_only", watching_commit)
    _idle_worker(idler, "w1", "deadpane", 30)
    idler.main()

    ledger = json.loads(idler.EMITTED_STATE.read_text())
    assert "untasked:w1" not in ledger
    assert ledger.get("untasked-page:w1") is not None
    assert len(commits) >= 2, (
        f"expected a write-ahead then a durable release, saw {len(commits)} commits")
    assert "untasked:w1" in commits[0], "the send was not recorded before it was attempted"
    assert "untasked:w1" not in commits[-1], (
        "the release never reached disk. Two windows differ and only one is safe: a kill "
        "between the pop and this commit leaves the action key on disk, so the budget is "
        "spent with nothing typed and nobody told; a kill after it leaves neither key, so "
        "the next scan re-pages. This commit is what moves us to the second, and the "
        "page key is absent from it by design: _commit_actions_only filters PAGE keys, "
        "so the page lands at the end-of-scan write asserted above")


def test_idle_untasked_caps_typed_sends_per_scan(idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    monkeypatch.setattr(idler, "IDLE_UNTASKED_MAX_SENDS", 3)
    for n in range(5):
        _idle_worker(idler, f"w{n}", f"idle-{n}", 20, window_id=f"%{n}")
    idler.main()
    out = capsys.readouterr().out
    assert len(_send_text_calls(calls)) == 3, (
        f"cap ignored: {len(_send_text_calls(calls))} panes typed into in one scan")
    assert [kind for kind, _, _ in silent].count("IDLE_NUDGED") == 3, (
        "a line was recorded for a send that never happened")


def test_idle_untasked_capped_workers_fire_on_the_next_scan(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    monkeypatch.setattr(idler, "IDLE_UNTASKED_MAX_SENDS", 2)
    for n in range(5):
        _idle_worker(idler, f"w{n}", f"idle-{n}", 20, window_id=f"%{n}")
    for _ in range(3):
        idler.main()
        capsys.readouterr()
    panes = sorted(w for w, _ in _send_text_calls(calls))
    assert panes == ["%0", "%1", "%2", "%3", "%4"], (
        f"a capped worker lost its turn instead of deferring it: {panes}")


def _scan_order(stale):
    return [p.stem for p in stale.ACTIVE.iterdir() if p.suffix == ".json"]


def test_idle_untasked_cap_does_not_gate_pages(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    monkeypatch.setattr(idler, "IDLE_UNTASKED_MAX_SENDS", 1)
    for n in range(3):
        _idle_worker(idler, f"w{n}", f"w{n}", 20)
    served_first, *paged = _scan_order(idler)
    _idle_worker(idler, served_first, "paned", 20, window_id="%paned")
    for sid in paged:
        _idle_worker(idler, sid, f"paged-{sid}", 20, window_id="")

    idler.main()
    out = capsys.readouterr().out
    assert len(_send_text_calls(calls)) == 1, "the cap on typed sends did not hold"
    assert out.count("IDLE_UNTASKED") == 2, (
        "pages were charged against the send cap: the budget was already spent by the "
        "paned worker this scan, and a page costs nothing to deliver")


def test_idle_untasked_send_cap_default_is_three(monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_IDLE_UNTASKED_MAX_SENDS", raising=False)
    assert _load_stale_monitor().IDLE_UNTASKED_MAX_SENDS == 3
    monkeypatch.setenv("CLAUDE_ORCH_IDLE_UNTASKED_MAX_SENDS", "7")
    assert _load_stale_monitor().IDLE_UNTASKED_MAX_SENDS == 7


def test_idle_untasked_a_refused_pane_does_not_spend_a_send_slot(idler, monkeypatch, capsys):
    typed = []

    monkeypatch.setattr(idler, "IDLE_UNTASKED_MAX_SENDS", 1)
    for sid in ("a", "b"):
        _idle_worker(idler, sid, sid, 20)
    refuser, live = _scan_order(idler)
    _idle_worker(idler, refuser, "refuser", 20, window_id="%dead")
    _idle_worker(idler, live, "live", 20, window_id="%live")

    class OnePaneRefuses:
        def send_text_checked(self, window_id, text):
            if window_id == "%dead":
                return False
            typed.append(window_id)
            return True

        def send_text(self, window_id, text, submit=True):
            raise AssertionError("must use the checked send")

        def close(self, window_id):
            pass

    monkeypatch.setattr(idler, "_get_driver", lambda: OnePaneRefuses())
    idler.main()
    assert typed == ["%live"], (
        f"the refused pane burned the only send slot, so the live worker went "
        f"un-woken: {typed}")

def test_matches_manager_reads_parent_name_only(stale):
    assert stale._matches_manager({"parent_name": "mgr-A"}, "mgr-A") is True
    assert stale._matches_manager({"parent_name": "mgr-B"}, "mgr-A") is False
    assert stale._matches_manager({"parent_name": None, "parent_manager_name": "mgr-A"}, "mgr-A") is True


def test_autoclose_closed_record_carries_tree_fields(stale, monkeypatch):
    now = int(time.time())
    _write_record(
        stale, "s1",
        pid=12345,
        iterm_sid="7",
        runtime="claude",
        parent_name="manager-a",
        parent_sid="mgr-sid",
        kind="worker",
        depth=1,
        last_turn_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10_000)),
    )
    monkeypatch.setattr(stale.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(stale.os, "kill", lambda *a, **k: None)
    assert stale.main() == 0
    closed_data = json.loads((stale.CLOSED / "s1.json").read_text())
    assert closed_data["parent_manager_name"] == "manager-a"
    assert closed_data["parent_name"] == "manager-a"
    assert closed_data["parent_sid"] == "mgr-sid"
    assert closed_data["kind"] == "worker"
    assert closed_data["depth"] == 1


def _child_record(stale, sid, parent, **overrides):
    record = {"kind": "subworker", "parent_name": parent, "parent_sid": "w-" + parent,
              "depth": 2, "runtime": "codex", "provider": "codex-exec", "pid": os.getpid(),
              "window_id": "%20", "name": f"{parent}-review"}
    record.update(overrides)
    return _write_record(stale, sid, **record)


def _parent_worker_record(stale, sid, name, manager, **overrides):
    record = {"name": name, "parent_name": manager, "parent_manager_name": manager,
              "kind": "worker", "depth": 1, "pid": os.getpid(), "window_id": "%10"}
    record.update(overrides)
    return _write_record(stale, sid, **record)


def _manager_record(stale, sid, name):
    return _write_record(stale, sid, agent="manager", name=name, kind="manager",
                         depth=0, pid=os.getpid(), window_id="%1")


def _old(minutes):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes * 60))


def _assignment(stale, sid, aid, round_no, rounds_dir=None):
    rounds_dir = rounds_dir or (stale.ROOT / "children" / aid)
    rounds_dir.mkdir(parents=True, exist_ok=True)
    stale.ASSIGNMENTS.mkdir(parents=True, exist_ok=True)
    payload = {"assignment_id": aid, "round": round_no, "rounds_dir": str(rounds_dir),
               "log_path": str(rounds_dir / f"r{round_no}.log"),
               "report_path": str(rounds_dir / f"r{round_no}.report.md")}
    (stale.ASSIGNMENTS / f"{sid}.json").write_text(json.dumps(payload))
    return rounds_dir


def test_unscoped_scan_leaves_an_idle_subworker_past_the_idle_threshold(stale, monkeypatch):
    calls = _capture_runs(stale, monkeypatch)
    _parent_worker_record(stale, "w1", "alpha", "boss")
    path = _child_record(stale, "c1", "alpha", last_turn_at=_old(600))
    stale.main()
    assert path.exists()
    assert not (stale.CLOSED / "c1.json").exists()
    assert _send_text_calls(calls) == []


def test_subworker_never_pages_stale_processing(stale, monkeypatch, capsys):
    _parent_worker_record(stale, "w1", "alpha", "boss")
    path = _child_record(stale, "c1", "alpha", state="processing")
    os.utime(path, (time.time() - 7200, time.time() - 7200))
    stale.main()
    assert "STALE_PROCESSING" not in capsys.readouterr().out


def test_scoped_scan_takes_the_child_of_its_own_worker_and_leaves_a_peers(stale):
    _parent_worker_record(stale, "w1", "alpha", "boss")
    _parent_worker_record(stale, "w2", "bravo", "other")
    manager_b = _manager_record(stale, "m2", "other")
    child_a = json.loads(_child_record(stale, "c1", "alpha").read_text())
    child_b = json.loads(_child_record(stale, "c2", "bravo", name="bravo-review").read_text())
    records = [r for _, r in stale._load_active_records()]
    assert stale._in_scope(child_a, "boss", records) is True
    assert stale._in_scope(child_b, "boss", records) is False
    assert stale._in_scope(child_a, None, records) is True
    manager_b.unlink()
    records = [r for _, r in stale._load_active_records()]
    assert stale._in_scope(child_b, "boss", records) is True


def test_owning_manager_is_read_through_the_closed_parent_record(stale):
    (stale.CLOSED / "w1.json").write_text(json.dumps(
        {"claude_sid": "w1", "name": "alpha", "parent_name": "boss", "closed_at": 5.0}))
    (stale.CLOSED / "w1-old.json").write_text(json.dumps(
        {"claude_sid": "w1-old", "name": "alpha", "parent_name": "older-boss", "closed_at": 1.0}))
    child = json.loads(_child_record(stale, "c1", "alpha").read_text())
    records = [r for _, r in stale._load_active_records()]
    assert stale._owning_manager(child, records) == "boss"
    _manager_record(stale, "m1", "boss")
    records = [r for _, r in stale._load_active_records()]
    assert stale._in_scope(child, "boss", records) is True
    assert stale._in_scope(child, "other", records) is False


def test_child_with_no_parent_anywhere_is_in_every_scope(stale):
    child = json.loads(_child_record(stale, "c1", "ghost").read_text())
    records = [r for _, r in stale._load_active_records()]
    assert stale._owning_manager(child, records) is None
    assert stale._in_scope(child, "boss", records) is True


def test_child_of_a_dead_owner_manager_is_in_every_scope(stale, monkeypatch):
    _manager_record(stale, "m1", "boss")
    _parent_worker_record(stale, "w1", "alpha", "boss")
    child = json.loads(_child_record(stale, "c1", "alpha").read_text())
    monkeypatch.setattr(stale, "_pid_alive", lambda pid: False)
    records = [r for _, r in stale._load_active_records()]
    assert stale._in_scope(child, "other", records) is True


def test_a_live_child_writing_its_log_holds_the_parents_stall_clock(stale, monkeypatch, capsys):
    now = time.time()
    parent = _parent_worker_record(stale, "w1", "alpha", "boss", state="processing")
    os.utime(parent, (now - 2400, now - 2400))
    child = _child_record(stale, "c1", "alpha", state="processing")
    os.utime(child, (now - 2400, now - 2400))
    rounds = _assignment(stale, "c1", "aid1", 1)
    log = rounds / "r1.log"
    log.write_text('{"type":"item.started"}\n')
    os.utime(log, (now - 60, now - 60))
    stale.main(manager_name="boss")
    assert "STALE_PROCESSING alpha" not in capsys.readouterr().out


def test_a_silent_child_does_not_hold_the_parents_stall_clock(stale, monkeypatch, capsys):
    now = time.time()
    parent = _parent_worker_record(stale, "w1", "alpha", "boss", state="processing")
    os.utime(parent, (now - 2400, now - 2400))
    child = _child_record(stale, "c1", "alpha", state="processing")
    os.utime(child, (now - 2400, now - 2400))
    rounds = _assignment(stale, "c1", "aid1", 1)
    log = rounds / "r1.log"
    log.write_text('{"type":"item.started"}\n')
    os.utime(log, (now - 2400, now - 2400))
    stale.main(manager_name="boss")
    assert "STALE_PROCESSING alpha" in capsys.readouterr().out


def _pane(pane_id, start_command, dead="1", session="claude-workers"):
    return {"session": session, "id": pane_id, "start_command": start_command, "dead": dead}


def _child_driver(stale, monkeypatch, panes, closed_dir_at_close=None):
    closed = []
    sessions = {}
    for p in panes:
        sessions.setdefault(p["session"], []).append(
            {"title": "t", "windows": [{"id": p["id"], "dead": p["dead"],
                                        "start_command": p["start_command"],
                                        "cwd": "/x", "title": "t", "pid": "1"}]})
    tree = [{"wm_class": s, "tabs": tabs} for s, tabs in sessions.items()]

    def close(pane):
        if closed_dir_at_close is not None:
            closed_dir_at_close.append(
                sorted(p.name for p in stale.CLOSED.iterdir()) if stale.CLOSED.is_dir() else [])
        closed.append(pane)

    driver = types.SimpleNamespace(ls=lambda: tree, close=close,
                                   send_text=lambda *a, **k: None,
                                   send_text_checked=lambda *a, **k: True,
                                   capture_screen=lambda pane: "")
    monkeypatch.setattr(stale, "_get_driver", lambda: driver)
    return closed


def _round_cmd(root, aid, n):
    return (f'/bin/zsh -ic "cd /x && CLAUDE_AGENT=worker codex exec -C /x --json '
            f'-o {root}/children/{aid}/r{n}.report.md.tmp - < {root}/children/{aid}/r{n}.brief.md '
            f'2>&1 | tee {root}/children/{aid}/r{n}.log"')


def _seed(stale, key, first_seen, manager_name=None):
    path = stale._emitted_state_path(manager_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = json.loads(path.read_text()) if path.exists() else {}
    current[key] = ({"first_seen": first_seen, "paged": 0}
                    if key.startswith("orphan:") else first_seen)
    path.write_text(json.dumps(current))


def test_orphaned_child_is_left_alone_inside_the_grace(stale, monkeypatch, capsys):
    closed = _child_driver(stale, monkeypatch, [])
    path = _child_record(stale, "c1", "alpha")
    stale.main()
    assert path.exists() and closed == []
    assert "ORPHANED_CHILD" not in capsys.readouterr().out
    assert "orphan-child:c1" in json.loads(stale._emitted_state_path(None).read_text())


def test_orphaned_child_is_archived_and_closed_after_the_grace(stale, silent, monkeypatch, capsys):
    at_close = []
    closed = _child_driver(stale, monkeypatch, [], closed_dir_at_close=at_close)
    path = _child_record(stale, "c1", "alpha", spend={"out_tokens": 9})
    _write_question(stale, "q1", "c1", parent_manager_name="alpha")
    _seed(stale, "orphan-child:c1", time.time() - 130)
    stale.main()
    out = capsys.readouterr().out
    assert "ORPHANED_CHILD alpha-review (parent alpha gone) — closed" not in out and _said(silent, "ORPHANED_CHILD alpha-review (parent alpha gone)")
    assert not path.exists()
    archived = json.loads((stale.CLOSED / "c1.json").read_text())
    assert archived["closed_reason"] == "orphaned:alpha"
    assert archived["kind"] == "subworker" and archived["provider"] == "codex-exec"
    assert archived["spend"] == {"out_tokens": 9}
    assert closed == ["%20"]
    assert at_close == [["c1.json"]], "the closed record must exist before the pane is killed"
    assert not list(stale.QUESTIONS.rglob("q1.json"))


def test_orphan_key_is_dropped_when_the_parent_comes_back(stale, monkeypatch, capsys):
    closed = _child_driver(stale, monkeypatch, [])
    path = _child_record(stale, "c1", "alpha")
    _seed(stale, "orphan-child:c1", time.time() - 130)
    _parent_worker_record(stale, "w1", "alpha", "boss")
    stale.main()
    assert path.exists() and closed == []
    assert "orphan-child:c1" not in json.loads(stale._emitted_state_path(None).read_text())


def test_parent_record_with_a_dead_pid_still_orphans_the_child(stale, monkeypatch, capsys):
    closed = _child_driver(stale, monkeypatch, [])
    _parent_worker_record(stale, "w1", "alpha", "boss", pid=2 ** 22 + 7)
    monkeypatch.setattr(stale, "_pid_alive", lambda pid: pid == os.getpid())
    path = _child_record(stale, "c1", "alpha")
    _seed(stale, "orphan-child:c1", time.time() - 130)
    stale.main()
    assert not path.exists() and closed == ["%20"]


def test_orphan_cascade_runs_only_in_the_owners_scan_while_that_manager_lives(stale, silent, monkeypatch, capsys):
    closed = _child_driver(stale, monkeypatch, [])
    _manager_record(stale, "m1", "boss")
    (stale.CLOSED / "w1.json").write_text(json.dumps(
        {"claude_sid": "w1", "name": "alpha", "parent_name": "boss", "closed_at": 5.0}))
    path = _child_record(stale, "c1", "alpha")
    _seed(stale, "orphan-child:c1", time.time() - 130, manager_name="other")
    stale.main(manager_name="other")
    assert path.exists() and closed == []
    _seed(stale, "orphan-child:c1", time.time() - 130, manager_name="boss")
    stale.main(manager_name="boss")
    assert not path.exists() and closed == ["%20"]
    assert _outbox_entries(stale, manager="boss") == [] and _said(silent, "ORPHANED_CHILD alpha-review")


def test_superseded_round_window_is_closed_without_a_lane_line(stale, monkeypatch, capsys):
    root = str(stale.ROOT)
    closed = _child_driver(stale, monkeypatch, [
        _pane("%31", _round_cmd(root, "aid1", 1)),
        _pane("%32", _round_cmd(root, "aid1", 2)),
    ])
    _parent_worker_record(stale, "w1", "alpha", "boss")
    _child_record(stale, "c1", "alpha", window_id="%32")
    _assignment(stale, "c1", "aid1", 2)
    stale.main()
    out = capsys.readouterr()
    assert closed == ["%31"]
    assert "%31" in out.err and "ORPHAN_WINDOW" not in out.out


def test_latest_round_window_of_a_live_parent_stays_for_kill_child(stale, monkeypatch, capsys):
    root = str(stale.ROOT)
    closed = _child_driver(stale, monkeypatch, [_pane("%32", _round_cmd(root, "aid1", 2))])
    _parent_worker_record(stale, "w1", "alpha", "boss")
    (stale.CLOSED / "c1.json").write_text(json.dumps(
        {"claude_sid": "c1", "name": "alpha-review", "kind": "subworker", "parent_name": "alpha",
         "window_id": "%32", "closed_at": 5.0, "closed_reason": "session_end"}))
    _assignment(stale, "c1", "aid1", 2)
    _seed(stale, "orphan:%32", time.time() - 700)
    stale.main()
    assert closed == []
    assert "ORPHAN_WINDOW" not in capsys.readouterr().out


def test_dead_windows_of_a_closed_child_whose_parent_is_gone_are_closed_after_the_grace(stale, monkeypatch, capsys):
    root = str(stale.ROOT)
    closed = _child_driver(stale, monkeypatch, [_pane("%32", _round_cmd(root, "aid1", 2))])
    (stale.CLOSED / "c1.json").write_text(json.dumps(
        {"claude_sid": "c1", "name": "alpha-review", "kind": "subworker", "parent_name": "alpha",
         "window_id": "%32", "closed_at": 5.0, "closed_reason": "session_end"}))
    _assignment(stale, "c1", "aid1", 2)
    stale.main()
    assert closed == []
    _seed(stale, "dead-child-window:%32", time.time() - 130)
    stale.main()
    assert closed == ["%32"]


def test_dead_child_window_without_any_assignment_is_closed_after_the_grace(stale, monkeypatch):
    root = str(stale.ROOT)
    closed = _child_driver(stale, monkeypatch, [_pane("%33", _round_cmd(root, "gone", 1))])
    _seed(stale, "dead-child-window:%33", time.time() - 130)
    stale.main()
    assert closed == ["%33"]


def test_dead_child_window_with_a_pending_sidecar_is_protected(stale, monkeypatch):
    root = str(stale.ROOT)
    closed = _child_driver(stale, monkeypatch, [_pane("%33", _round_cmd(root, "pend", 1))])
    stale.ASSIGNMENTS_PENDING.mkdir(parents=True, exist_ok=True)
    (stale.ASSIGNMENTS_PENDING / "pend.window").write_text("%33")
    _seed(stale, "dead-child-window:%33", time.time() - 130)
    stale.main()
    assert closed == []


def _closed_child(stale, sid, parent, window_id):
    (stale.CLOSED / f"{sid}.json").write_text(json.dumps(
        {"claude_sid": sid, "name": f"{parent}-review", "kind": "subworker",
         "parent_name": parent, "window_id": window_id, "closed_at": 5.0,
         "closed_reason": "session_end"}))


def test_a_round_pane_with_no_assignment_is_kept_while_its_parent_lives(stale, monkeypatch):
    root = str(stale.ROOT)
    closed = _child_driver(stale, monkeypatch, [_pane("%32", _round_cmd(root, "aid1", 2))])
    _parent_worker_record(stale, "w1", "alpha", "boss")
    _closed_child(stale, "c1", "alpha", "%32")
    _seed(stale, "dead-child-window:%32", time.time() - 130)
    stale.main()
    assert closed == []


def test_a_round_pane_with_no_assignment_is_closed_once_its_parent_is_gone(stale, monkeypatch):
    root = str(stale.ROOT)
    closed = _child_driver(stale, monkeypatch, [_pane("%32", _round_cmd(root, "aid1", 2))])
    _closed_child(stale, "c1", "alpha", "%32")
    stale.main()
    assert closed == []
    _seed(stale, "dead-child-window:%32", time.time() - 130)
    stale.main()
    assert closed == ["%32"]


def test_child_window_of_a_foreign_state_root_is_never_touched_or_paged(stale, monkeypatch, capsys):
    closed = _child_driver(stale, monkeypatch, [
        _pane("%40", _round_cmd("/private/tmp/scratch/e2e/state", "aid9", 1)),
        _pane("%41", _round_cmd("/private/tmp/scratch/e2e/state", "aid9", 1), dead="0"),
    ])
    _seed(stale, "orphan:%40", time.time() - 700)
    _seed(stale, "orphan:%41", time.time() - 700)
    _seed(stale, "dead-child-window:%40", time.time() - 700)
    stale.main()
    assert closed == []
    assert "ORPHAN_WINDOW" not in capsys.readouterr().out


def test_live_own_child_window_without_a_record_pages_as_a_child_pane(stale, monkeypatch, capsys):
    root = str(stale.ROOT)
    _child_driver(stale, monkeypatch, [_pane("%42", _round_cmd(root, "aid7", 1), dead="0")])
    _seed(stale, "orphan:%42", time.time() - 700)
    stale.main()
    out = capsys.readouterr().out
    assert "ORPHAN_WINDOW %42 child round pane r1 of assignment aid7" in out
    assert "no backing record" in out


def test_child_round_regex_reads_its_own_root_not_the_shell_prefix(stale):
    cmd = _round_cmd("/Users/user/.claude/dockwright", "ab12", 3)
    assert stale._child_round_of(cmd) == ("/Users/user/.claude/dockwright", "ab12", 3)
    assert stale._child_round_of("claude --resume x") is None


def _reroot(stale, monkeypatch, root):
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(stale, "ROOT", root)
    monkeypatch.setattr(stale, "ACTIVE", root / "active")
    monkeypatch.setattr(stale, "QUESTIONS", root / "questions")
    monkeypatch.setattr(stale, "CLOSED", root / "closed")
    monkeypatch.setattr(stale, "ASSIGNMENTS", root / "assignments", raising=False)
    monkeypatch.setattr(stale, "ASSIGNMENTS_PENDING", root / "assignments" / ".pending",
                        raising=False)
    monkeypatch.setattr(stale, "GARDENER_LIVE_WINDOWS", root / "gardener" / "live-windows",
                        raising=False)
    monkeypatch.setattr(stale, "ACCOUNT_ACTIVE", root / "account-active")
    monkeypatch.setattr(stale, "ACCOUNT_REGISTRY", root / "account-registry.json", raising=False)
    monkeypatch.setattr(stale, "ACCOUNT_LEDGER", root / "account-flips.jsonl")
    monkeypatch.setattr(stale, "ACCOUNT_STATE", root / "account-state.json")
    monkeypatch.setattr(stale, "ACCOUNT_LOCK", root / ".account-flip.lock")
    monkeypatch.setattr(stale, "EMITTED_STATE", stale._emitted_state_path(None), raising=False)
    for name in ("active", "questions", "closed"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return str(root)


def test_a_spaced_state_root_still_sweeps_and_pages_its_own_round_panes(stale, monkeypatch, tmp_path, capsys):
    root = _reroot(stale, monkeypatch, tmp_path / "my root" / "state")
    closed = _child_driver(stale, monkeypatch, [
        _pane("%31", _round_cmd(root, "aid1", 1)),
        _pane("%32", _round_cmd(root, "aid1", 2)),
        _pane("%42", _round_cmd(root, "aid7", 1), dead="0"),
    ])
    _parent_worker_record(stale, "w1", "alpha", "boss")
    _child_record(stale, "c1", "alpha", window_id="%32")
    _assignment(stale, "c1", "aid1", 2)
    _seed(stale, "orphan:%42", time.time() - 700)
    stale.main()
    out = capsys.readouterr()
    assert closed == ["%31"], "the superseded round pane of a spaced root must still be swept"
    assert "ORPHAN_WINDOW %42 child round pane r1 of assignment aid7" in out.out
    assert "%32" not in out.err


def test_a_spaced_foreign_state_root_is_never_touched_or_paged(stale, monkeypatch, tmp_path, capsys):
    _reroot(stale, monkeypatch, tmp_path / "my root" / "state")
    closed = _child_driver(stale, monkeypatch, [
        _pane("%40", _round_cmd("/private/tmp/their scratch/state", "aid9", 1)),
        _pane("%41", _round_cmd("/private/tmp/their scratch/state", "aid9", 1), dead="0"),
    ])
    _seed(stale, "orphan:%40", time.time() - 700)
    _seed(stale, "orphan:%41", time.time() - 700)
    _seed(stale, "dead-child-window:%40", time.time() - 700)
    stale.main()
    assert closed == []
    assert "ORPHAN_WINDOW" not in capsys.readouterr().out


def test_a_foreign_root_ending_with_our_root_is_never_touched_or_paged(stale, monkeypatch, tmp_path, capsys):
    root = _reroot(stale, monkeypatch, tmp_path / "state")
    foreign = "/private/tmp/theirs" + root
    closed = _child_driver(stale, monkeypatch, [
        _pane("%70", _round_cmd(foreign, "aid9", 1)),
        _pane("%71", _round_cmd(foreign, "aid9", 1), dead="0"),
    ])
    _seed(stale, "orphan:%70", time.time() - 700)
    _seed(stale, "orphan:%71", time.time() - 700)
    _seed(stale, "dead-child-window:%70", time.time() - 700)
    stale.main()
    assert closed == [], "a foreign root that merely ends with our root path is not ours"
    assert "ORPHAN_WINDOW" not in capsys.readouterr().out


def test_a_config_path_before_our_root_does_not_steal_the_attribution(stale, monkeypatch, tmp_path, capsys):
    root = _reroot(stale, monkeypatch, tmp_path / "my root" / "state")
    cmd = ("env DOCKWRIGHT_CONFIG=/private/tmp/scratch/e2e/dockwright.toml "
           + _round_cmd(root, "aid7", 1))
    _child_driver(stale, monkeypatch, [_pane("%42", cmd, dead="0")])
    _seed(stale, "orphan:%42", time.time() - 700)
    stale.main()
    out = capsys.readouterr().out
    assert "ORPHAN_WINDOW %42 child round pane r1 of assignment aid7" in out
    assert "no backing record" in out


class _CapturingDriver:
    def __init__(self, screens):
        self.screens = screens
        self.captured = []

    def capture_screen(self, pane):
        self.captured.append(pane)
        return self.screens.get(pane)


def test_approval_scan_skips_a_subworker_but_still_pages_a_worker(monkeypatch, tmp_path):
    active, pending, events, next_emitted, emitted, emit, now = _run_approval_scan(
        monkeypatch, tmp_path, {})
    driver = _CapturingDriver({"%7": PROCEED_DIALOG})
    monkeypatch.setattr(stale_monitor, "_get_driver", lambda: driver)
    record = _worker_record(active, "sid1", "zeb4", "%7")
    (active / "sid1.json").write_text(json.dumps({**record, "kind": "subworker"}))
    stale_monitor._scan_approval_prompts("mgr-x", now, emitted, next_emitted, emit)
    assert events == []
    assert driver.captured == []
    (active / "sid1.json").write_text(json.dumps({**record, "kind": "worker"}))
    stale_monitor._scan_approval_prompts("mgr-x", now, emitted, next_emitted, emit)
    assert driver.captured == ["%7"]
    assert [e[0] for e in events] == ["approval"]
    assert events[0][2].startswith("APPROVAL_PROMPT zeb4 → ")


def test_a_live_childs_fresh_transcript_holds_the_parents_stall_clock(stale, monkeypatch, capsys):
    now = time.time()
    parent = _parent_worker_record(stale, "w1", "alpha", "boss", state="processing")
    os.utime(parent, (now - 2400, now - 2400))
    child = _child_record(stale, "c1", "alpha", state="processing", runtime="claude")
    os.utime(child, (now - 2400, now - 2400))
    rounds = _assignment(stale, "c1", "aid1", 1)
    log = rounds / "r1.log"
    log.write_text('{"type":"item.started"}\n')
    os.utime(log, (now - 2400, now - 2400))
    transcript = _write_transcript(stale, "c1", "child still working")
    os.utime(transcript, (now - 60, now - 60))
    stale.main(manager_name="boss")
    assert "STALE_PROCESSING alpha" not in capsys.readouterr().out


def test_a_child_whose_record_carries_no_pid_is_never_cascaded(stale, monkeypatch, capsys):
    closed = _child_driver(stale, monkeypatch, [])
    path = _child_record(stale, "c1", "alpha")
    parent = _parent_worker_record(stale, "w1", "alpha", "boss")
    parent.write_text(json.dumps({k: v for k, v in json.loads(parent.read_text()).items()
                                  if k != "pid"}))
    _seed(stale, "orphan-child:c1", time.time() - 130)
    stale.main()
    assert path.exists() and closed == []
    assert "ORPHANED_CHILD" not in capsys.readouterr().out


def test_a_live_codex_childs_log_stays_in_the_cache_across_a_scan(stale, monkeypatch):
    now = time.time()
    parent = _parent_worker_record(stale, "w1", "alpha", "boss", state="processing")
    os.utime(parent, (now - 2400, now - 2400))
    child = _child_record(stale, "c1", "alpha", state="processing")
    os.utime(child, (now - 2400, now - 2400))
    _write_codex_transcript(stale, "c1", mtime=now - 60)
    stale.main(manager_name="boss")
    cache = json.loads(stale._emitted_state_path("boss").read_text())["codex_log_cache"]
    assert "c1" in cache


LIVE_PANE = ("⏺ Background command completed (exit code 0)\n"
             "· Mulling… (9m 27s · ↓ 38.1k tokens)\n"
             "──────────\n❯ \n──────────\n  repo  branch\n")


def _pane_driver(stale, monkeypatch, text, calls):
    supplier = text if callable(text) else (lambda: text)

    class Driver:
        def capture_screen(self, pane):
            calls.append(pane)
            return supplier()

        def send_text(self, window_id, txt, submit=True):
            calls.append(("send_text", window_id))

        def send_text_checked(self, window_id, txt):
            calls.append(("send_text", window_id))
            return True

        def close(self, window_id):
            calls.append(("close", window_id))

        def ls(self):
            return []

    monkeypatch.setattr(stale, "_get_driver", lambda: Driver())


def test_live_turn_regex_matches_the_measured_spinner_lines(stale):
    for line in ("· Mulling… (9m 27s · ↓ 38.1k tokens)", "Simmering… (4m 46s)",
                 "Thinking… (10m 25s · ↓ 46.8k tokens)", "✻ Cooking… (1h 2m 3s)",
                 "Elapsed… (1d 2h 3m 4s)"):
        assert stale.LIVE_TURN_RE.search(line), line
    for line in ("esc to interrupt", "❯ ", "done in 45s", "waited (3m) for it"):
        assert not stale.LIVE_TURN_RE.search(line), line


def test_idle_untasked_does_not_type_into_a_pane_with_a_live_turn(idler, monkeypatch, capsys):
    calls = []
    _pane_driver(idler, monkeypatch, LIVE_PANE, calls)
    _idle_worker(idler, "w1", "stalled", 20)
    idler.main()
    assert "IDLE_NUDGED" not in capsys.readouterr().out
    assert not [c for c in calls if isinstance(c, tuple) and c[0] == "send_text"]
    assert calls.count("%9") == 1


def test_idle_untasked_still_types_when_the_pane_shows_no_spinner(idler, silent, monkeypatch, capsys):
    calls = []
    _pane_driver(idler, monkeypatch, "──────\n❯ \n", calls)
    _idle_worker(idler, "w1", "stalled", 20)
    idler.main()
    assert _silent_only(capsys, silent, "IDLE_NUDGED stalled")
    assert "%9" in calls, "the pane must be read before the nudge is typed"


def test_autoclose_holds_a_pane_with_a_live_turn_even_past_the_busy_shell_ceiling(stale, monkeypatch):
    calls = []
    _pane_driver(stale, monkeypatch, LIVE_PANE, calls)
    monkeypatch.setattr(stale, "IDLE_THRESHOLD_SEC", 100)
    path = _write_record(stale, "s1", pid=os.getpid(), window_id="%9",
                         last_turn_at=_old(400), state="idle")
    stale.main()
    assert path.exists()
    assert not (stale.CLOSED / "s1.json").exists()
    assert calls.count("%9") == 1


def test_autoclose_reaps_when_the_pane_shows_no_spinner(stale, monkeypatch):
    calls = []
    _pane_driver(stale, monkeypatch, "──────\n❯ \n", calls)
    monkeypatch.setattr(stale, "IDLE_THRESHOLD_SEC", 100)
    path = _write_record(stale, "s1", pid=os.getpid(), window_id="%9",
                         last_turn_at=_old(400), state="idle")
    stale.main()
    assert not path.exists()


def test_pane_memo_is_fresh_on_every_scan(stale, monkeypatch):
    calls = []
    _pane_driver(stale, monkeypatch, LIVE_PANE, calls)
    monkeypatch.setattr(stale, "IDLE_THRESHOLD_SEC", 100)
    _write_record(stale, "s1", pid=os.getpid(), window_id="%9", last_turn_at=_old(400), state="idle")
    stale.main()
    stale.EMITTED_STATE.write_text(json.dumps({}))
    stale.main()
    assert calls.count("%9") == 2


def test_codex_tui_panes_are_not_captured(stale, monkeypatch):
    calls = []
    _pane_driver(stale, monkeypatch, LIVE_PANE, calls)
    monkeypatch.setattr(stale, "IDLE_THRESHOLD_SEC", 100)
    path = _write_record(stale, "s1", pid=os.getpid(), window_id="%9",
                         last_turn_at=_old(400), state="idle", runtime="codex")
    stale.main()
    assert "%9" not in calls
    assert not path.exists(), "the codex record must still reach the reaper"


def _rearm_autoclose(stale):
    state = json.loads(stale.EMITTED_STATE.read_text())
    state.pop("last_autoclose_run", None)
    stale.EMITTED_STATE.write_text(json.dumps(state))


def test_a_frozen_spinner_stops_holding_the_worker_on_the_next_scan(stale, monkeypatch):
    calls = []
    _pane_driver(stale, monkeypatch, LIVE_PANE, calls)
    monkeypatch.setattr(stale, "IDLE_THRESHOLD_SEC", 100)
    path = _write_record(stale, "s1", pid=os.getpid(), window_id="%9",
                         last_turn_at=_old(400), state="idle")
    stale.main()
    assert path.exists(), "the first sight of a spinner must hold the worker"
    _rearm_autoclose(stale)
    stale.main()
    assert not path.exists(), "an unchanged spinner line must stop holding the worker"


def test_a_ticking_spinner_keeps_holding_the_worker_across_scans(stale, monkeypatch):
    calls = []
    box = ["· Mulling… (9m 27s · ↓ 38.1k tokens)\n❯ \n"]
    _pane_driver(stale, monkeypatch, lambda: box[0], calls)
    monkeypatch.setattr(stale, "IDLE_THRESHOLD_SEC", 100)
    path = _write_record(stale, "s1", pid=os.getpid(), window_id="%9",
                         last_turn_at=_old(400), state="idle")
    stale.main()
    assert path.exists()
    box[0] = "· Mulling… (10m 27s · ↓ 38.1k tokens)\n❯ \n"
    _rearm_autoclose(stale)
    stale.main()
    assert path.exists(), "a ticking elapsed counter must keep holding the worker"
    assert not (stale.CLOSED / "s1.json").exists()


def test_a_stale_spinner_above_the_live_one_does_not_release_the_worker(stale, monkeypatch):
    calls = []
    stale_line = "✻ Thinking… (2m 14s · ↓ 12.4k tokens)"
    box = [f"{stale_line}\n· Mulling… (9m 27s · ↓ 38.1k tokens)\n❯ \n"]
    _pane_driver(stale, monkeypatch, lambda: box[0], calls)
    monkeypatch.setattr(stale, "IDLE_THRESHOLD_SEC", 100)
    path = _write_record(stale, "s1", pid=os.getpid(), window_id="%9",
                         last_turn_at=_old(400), state="idle")
    stale.main()
    assert path.exists()
    box[0] = f"{stale_line}\n· Mulling… (10m 27s · ↓ 38.1k tokens)\n❯ \n"
    _rearm_autoclose(stale)
    stale.main()
    assert path.exists(), "the bottom-most spinner is the live one and it ticked"
    assert not (stale.CLOSED / "s1.json").exists()


def test_a_frozen_spinner_stops_suppressing_the_nudge_on_the_next_scan(idler, silent, monkeypatch, capsys):
    calls = []
    _pane_driver(idler, monkeypatch, LIVE_PANE, calls)
    _idle_worker(idler, "w1", "stalled", 20)
    idler.main()
    assert "IDLE_NUDGED" not in capsys.readouterr().out
    idler.main()
    assert _silent_only(capsys, silent, "IDLE_NUDGED stalled")


def test_idle_untasked_skips_a_worker_with_a_live_child(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "alpha", 20)
    _child_record(idler, "c1", "alpha")
    idler.main()
    assert "IDLE_NUDGED" not in capsys.readouterr().out and _send_text_calls(calls) == []


def test_autoclose_skips_a_worker_with_a_live_child(stale, monkeypatch):
    _capture_runs(stale, monkeypatch)
    path = _write_record(stale, "w1", name="alpha", pid=os.getpid(), window_id="%9",
                         last_turn_at=_old(400), state="idle")
    _child_record(stale, "c1", "alpha")
    stale.main()
    assert path.exists()


def test_a_dead_pid_child_does_not_hold_its_parent(stale, monkeypatch):
    _capture_runs(stale, monkeypatch)
    path = _write_record(stale, "w1", name="alpha", pid=os.getpid(), window_id="%9",
                         last_turn_at=_old(400), state="idle")
    _child_record(stale, "c1", "alpha", pid=2 ** 22 + 11)
    stale.main()
    assert not path.exists()


def test_limited_manager_folds_the_orphan_cascade_into_the_orphaned_rollup(
        stale, monkeypatch, capsys):
    now = time.time()
    _child_driver(stale, monkeypatch, [])
    _write_limited_manager(stale, "mgr1", "boss", now - 1800)
    path = _child_record(stale, "c1", "alpha")
    _seed(stale, "orphan-child:c1", now - 130, manager_name="boss")
    stale.main(manager_name="boss")
    assert "ORPHANED_CHILD" not in capsys.readouterr().out
    assert not path.exists()
    buffer = json.loads(stale._emitted_state_path("boss").read_text())["limited_buffer"]
    assert buffer["orphaned"] == 1


def test_an_orphaned_child_is_silent_and_never_reaches_the_outbox(stale, silent, monkeypatch, capsys):
    _child_driver(stale, monkeypatch, [])
    _child_record(stale, "c1", "alpha")
    _seed(stale, "orphan-child:c1", time.time() - 130, manager_name="mgr")
    assert stale.main(manager_name="mgr") == 0
    assert "ORPHANED_CHILD" not in capsys.readouterr().out
    assert _outbox_entries(stale) == []
    assert silent == [("ORPHANED_CHILD", {"name": "alpha-review", "parent": "alpha"}, "c1")]


def test_an_orphaned_child_stays_silent_when_other_lines_print(stale, silent, monkeypatch, capsys):
    now = int(time.time())
    _child_driver(stale, monkeypatch, [])
    _child_record(stale, "c1", "alpha")
    _seed(stale, "orphan-child:c1", now - 130, manager_name="mgr")
    _stale_processing_worker(stale, "p-worker", "p-tab", "mgr", now)
    stale.main(manager_name="mgr")
    out = capsys.readouterr().out
    assert "STALE_PROCESSING p-tab" in out
    assert "ORPHANED_CHILD" not in out and _said(silent, "ORPHANED_CHILD alpha-review")
    assert _outbox_entries(stale) == []


def test_dead_child_window_is_closed_when_no_record_survives_its_assignment(
        stale, monkeypatch, capsys):
    root = str(stale.ROOT)
    closed = _child_driver(stale, monkeypatch, [_pane("%34", _round_cmd(root, "aid9", 1))])
    _assignment(stale, "ghost", "aid9", 1)
    _seed(stale, "dead-child-window:%34", time.time() - 130)
    stale.main()
    assert closed == ["%34"]


def test_a_child_without_a_parent_name_is_not_owned_by_a_nameless_record(stale):
    nameless = {"claude_sid": "n1", "parent_name": "other", "pid": os.getpid()}
    manager = {"claude_sid": "m1", "agent": "manager", "name": "other", "pid": os.getpid()}
    child = {"claude_sid": "c1", "kind": "subworker", "depth": 2}
    records = [nameless, manager, child]
    assert stale._owning_manager(child, records) is None
    assert stale._in_scope(child, "boss", records) is True


def test_one_pane_listing_serves_both_window_passes(stale, monkeypatch):
    calls = {"n": 0}

    def ls():
        calls["n"] += 1
        return []

    driver = types.SimpleNamespace(ls=ls, close=lambda pane: None,
                                   send_text=lambda *a, **k: None,
                                   send_text_checked=lambda *a, **k: True,
                                   capture_screen=lambda pane: "")
    monkeypatch.setattr(stale, "_get_driver", lambda: driver)
    stale.main()
    assert calls["n"] == 1


def test_an_orphaned_codex_child_records_its_resolved_transcript(stale, monkeypatch, capsys):
    _child_driver(stale, monkeypatch, [])
    rollout = stale.CODEX_SESSIONS / "2026" / "09" / "rollout-2026-09-01T00-00-00-c1.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text("{}\n")
    _child_record(stale, "c1", "alpha")
    _seed(stale, "orphan-child:c1", time.time() - 130)
    stale.main()
    archived = json.loads((stale.CLOSED / "c1.json").read_text())
    assert archived["transcript_path"] == str(rollout)


def test_a_real_provider_spawn_command_is_attributed_to_our_own_root(stale, monkeypatch, tmp_path):
    from dockwright.providers import REPORT_MODE, get_provider
    from dockwright.providers.base import ChildRound
    root = _reroot(stale, monkeypatch, tmp_path / "my root" / "state")
    round_dir = Path(root) / "children" / "aid1"
    child_round = ChildRound(mode=REPORT_MODE, cwd=str(tmp_path), brief_path=round_dir / "r1.brief.md",
                             report_path=round_dir / "r1.report.md", log_path=round_dir / "r1.log",
                             model="@opus", effort="high")
    for provider in ("codex-exec", "claude-exec"):
        found = stale._child_round_of(get_provider(provider).spawn_command(child_round))
        assert found is not None and found[1:] == ("aid1", 1), provider
        assert stale._own_root(found[0]), provider


CREDIT_BANNER_TEXT = ("You've reached your Fable limit. Run /usage-credits to "
                      "continue or switch models with /model.")


def _banner_line(text=CREDIT_BANNER_TEXT, timestamp=None):
    event = {
        "type": "assistant", "isApiErrorMessage": True, "apiErrorStatus": 429,
        "error": "rate_limit",
        "message": {"model": "<synthetic>",
                    "content": [{"type": "text", "text": text}]},
    }
    if timestamp is not None:
        event["timestamp"] = timestamp
    return json.dumps(event)


def _tool_only_line():
    return json.dumps({
        "type": "assistant",
        "message": {"model": "claude-fable-5-1",
                    "content": [{"type": "tool_use", "name": "Bash", "input": {}}]},
    })


def _write_banner(stale, sid, *extra_lines, banner_text=CREDIT_BANNER_TEXT, timestamp=None):
    project_dir = stale.CLAUDE_PROJECTS / "proj"
    project_dir.mkdir(parents=True, exist_ok=True)
    log = project_dir / f"{sid}.jsonl"
    log.write_text("\n".join((_banner_line(banner_text, timestamp),) + extra_lines) + "\n")
    return log


def test_credit_banner_switches_the_model_and_tells_the_worker_to_resume(
        idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "spent", 3)
    _write_banner(idler, "w1")
    idler.main()
    out = capsys.readouterr().out
    assert "MODEL_FALLBACK spent" not in out and _said(silent, "MODEL_FALLBACK spent")
    assert [t for _, t in _send_text_calls(calls)] == [
        f"/model {idler.worker_fallback_model()}",
        f"/effort {idler.MODEL_FALLBACK_EFFORT}",
        idler.NUDGE_TEXT,
    ]


BANNER_AT = "2026-10-14T12:02:37.887Z"


def _model_spent(stale):
    if not stale.ACCOUNT_STATE.exists():
        return {}
    return json.loads(stale.ACCOUNT_STATE.read_text()).get("model_spent", {})


@pytest.mark.parametrize("kind", [None, "subworker"])
def test_a_fable_banner_marks_its_account_spent_for_workers_and_subworkers(
        idler, silent, monkeypatch, kind):
    _capture_runs(idler, monkeypatch)
    extra = {"kind": kind} if kind else {}
    _idle_worker(idler, "w1", "spent", 3, account="c",
                 started_at=idler._parse_iso(BANNER_AT) - 60, **extra)
    _write_banner(idler, "w1", timestamp=BANNER_AT)
    idler.main()
    assert _model_spent(idler) == {"c": {"fable": idler._parse_iso(BANNER_AT)}}


def test_a_manager_banner_marks_nothing(idler, silent, monkeypatch):
    _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "m1", "mgr", 3, agent="manager", account="c",
                 started_at=idler._parse_iso(BANNER_AT) - 60)
    _write_banner(idler, "m1", timestamp=BANNER_AT)
    idler.main()
    idler.main(manager_name="mgr")
    assert _model_spent(idler) == {}


def test_a_banner_from_before_the_current_launch_marks_nothing(stale):
    banner = stale._parse_iso(BANNER_AT)
    resumed = json.loads(_write_record(stale, "w1", account="c", started_at=banner + 60).read_text())
    _write_banner(stale, "w1", timestamp=BANNER_AT)
    stale._note_model_spent(resumed, {})
    assert _model_spent(stale) == {}
    launched = json.loads(_write_record(stale, "w1", account="c", started_at=banner).read_text())
    stale._note_model_spent(launched, {})
    assert _model_spent(stale) == {"c": {"fable": banner}}


def _launch_before_banner(stale):
    banner = stale._parse_iso(BANNER_AT)
    return banner, json.loads(_write_record(stale, "w1", account="c", started_at=banner - 60).read_text())


def test_a_banner_followed_by_a_normal_turn_still_marks(stale):
    banner, record = _launch_before_banner(stale)
    log = _write_banner(stale, "w1", _assistant_line("back to work"), timestamp=BANNER_AT)
    stale._note_model_spent(record, {})
    assert _model_spent(stale) == {"c": {"fable": banner}}
    assert stale._model_credit_banner(log) is None


def test_a_banner_behind_a_large_request_blob_marks(stale):
    banner, record = _launch_before_banner(stale)
    blob = json.dumps({"type": "api-request-blob", "data": "x" * 5000})
    log = _write_banner(stale, "w1", *[blob] * 30, timestamp=BANNER_AT)
    assert stale._last_assistant_event(log) is None
    stale._note_model_spent(record, {})
    assert _model_spent(stale) == {"c": {"fable": banner}}


def test_the_newest_named_banner_of_the_launch_marks(stale):
    banner, record = _launch_before_banner(stale)
    newer = _iso_at(banner + 5)
    unnamed = _iso_at(banner + 9)
    _write_banner(stale, "w1", _banner_line(timestamp=newer),
                  _banner_line(UNNAMED_CREDIT_BANNER_TEXT, unnamed), timestamp=BANNER_AT)
    stale._note_model_spent(record, {})
    assert _model_spent(stale) == {"c": {"fable": stale._parse_iso(newer)}}


def test_the_print_mode_banner_wording_marks(stale):
    banner, record = _launch_before_banner(stale)
    _write_banner(stale, "w1", timestamp=BANNER_AT, banner_text=(
        "You've reached your Fable limit. Switch to another model, or manage usage credits at "
        "https://claude.ai/settings/usage?from=cc_cli_limit_message, to continue."))
    stale._note_model_spent(record, {})
    assert _model_spent(stale) == {"c": {"fable": banner}}


def test_quoted_banner_text_marks_nothing(stale):
    _banner, record = _launch_before_banner(stale)
    quoted = json.dumps({**json.loads(_assistant_line(CREDIT_BANNER_TEXT)), "timestamp": BANNER_AT})
    project_dir = stale.CLAUDE_PROJECTS / "proj"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "w1.jsonl").write_text(quoted + "\n")
    stale._note_model_spent(record, {})
    assert _model_spent(stale) == {}


def test_an_old_banner_behind_a_newer_launch_marks_nothing(stale):
    banner = stale._parse_iso(BANNER_AT)
    resumed = json.loads(_write_record(stale, "w1", account="b", started_at=banner + 60).read_text())
    _write_banner(stale, "w1", _assistant_line("back to work"), timestamp=BANNER_AT)
    stale._note_model_spent(resumed, {})
    assert _model_spent(stale) == {}


def test_a_banner_past_the_scan_tail_marks_nothing(stale):
    banner, _record = _launch_before_banner(stale)
    log = _write_banner(stale, "w1", _assistant_line("x" * 4096), timestamp=BANNER_AT)
    assert stale._newest_model_credit_banner(log, banner - 60, max_bytes=1024) is None
    assert stale._newest_model_credit_banner(log, banner - 60) == ("fable", banner)


def test_record_model_spent_reads_only_its_arguments(stale, tmp_path):
    other = tmp_path / "other"
    state_path, lock_path, ledger_path = other / "state.json", other / ".lock", other / "ledger.jsonl"
    stale.record_model_spent("c", "fable", 100.0, state_path, lock_path, ledger_path)
    stale.record_model_spent("c", "fable", 100.0, state_path, lock_path, ledger_path)
    assert json.loads(state_path.read_text())["model_spent"] == {"c": {"fable": 100.0}}
    assert [json.loads(l)["event"] for l in ledger_path.read_text().splitlines()] == ["model-spent"]
    assert not stale.ACCOUNT_STATE.exists()


def test_the_mark_keeps_the_newest_banner_and_logs_each_new_one(stale):
    stale._record_model_spent("c", "fable", 200.0)
    stale._record_model_spent("c", "fable", 100.0)
    stale._record_model_spent("c", "fable", 200.0)
    assert _model_spent(stale) == {"c": {"fable": 200.0}}
    ledger = [json.loads(l) for l in stale.ACCOUNT_LEDGER.read_text().splitlines() if l]
    assert [(e["event"], e["account"], e["model"], e["by"]) for e in ledger] == [
        ("model-spent", "c", "fable", "stale_monitor")]


def test_the_mark_keeps_the_rest_of_account_state(stale):
    now = int(time.time())
    stale._record_brick("a", now + 3600, "worker:w1", now)
    stale._record_model_spent("c", "fable", 100.0)
    state = json.loads(stale.ACCOUNT_STATE.read_text())
    assert state["accounts"]["a"]["reset_ts"] == now + 3600
    assert state["model_spent"] == {"c": {"fable": 100.0}}


def test_no_mark_without_a_banner_time_or_an_account(stale):
    before = stale._parse_iso(BANNER_AT) - 60
    rec = json.loads(_write_record(stale, "w1", account="c", started_at=before).read_text())
    _write_banner(stale, "w1")
    stale._note_model_spent(rec, {})
    rec2 = json.loads(_write_record(stale, "w2", started_at=before).read_text())
    _write_banner(stale, "w2", timestamp=BANNER_AT)
    stale._note_model_spent(rec2, {})
    assert _model_spent(stale) == {}


def _banner_naming(family):
    return (f"You've reached your {family} limit. Run /usage-credits to "
            "continue or switch models with /model.")


def _worker_on_banner(stale, family):
    _idle_worker(stale, "w1", "spent", 3)
    _write_banner(stale, "w1", banner_text=_banner_naming(family))


def test_a_rescue_onto_the_family_just_spent_is_refused_not_typed(
        idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    monkeypatch.setattr(idler, "worker_fallback_model", lambda: "claude-sonnet-5")
    _worker_on_banner(idler, "Sonnet")
    idler.main()
    out = capsys.readouterr().out
    assert _send_text_calls(calls) == [], (
        "the lane typed at a worker the family it had just spent; that is a no-op "
        "that costs a send-budget unit, three pastes and the session's context window")
    assert "MODEL_CREDIT_EXHAUSTED spent" in out
    assert "Sonnet" in out and "no rescue target" in out, (
        "the page must name the spent family and say no target is left, so the human "
        "sees a real state instead of 600s of silence")


def test_the_family_guard_reads_a_context_variant_as_the_same_family(
        idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    monkeypatch.setattr(idler, "worker_fallback_model", lambda: "claude-opus-5[1m]")
    _worker_on_banner(idler, "Opus")
    idler.main()
    assert _send_text_calls(calls) == [], (
        "claude-opus-5[1m] and claude-opus-5 are one family and one quota; a guard "
        "comparing literal ids would have typed here")
    assert "MODEL_CREDIT_EXHAUSTED spent" in capsys.readouterr().out


def test_the_family_guard_covers_a_family_no_constant_names(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    monkeypatch.setattr(idler, "worker_fallback_model", lambda: "claude-haiku-4-5")
    _worker_on_banner(idler, "Haiku")
    idler.main()
    assert _send_text_calls(calls) == [], (
        "a family nothing in the roster or the constants names must still be "
        "refused. This one resolves through pricing, so it exercises the resolved "
        "path; the unresolvable path is the fallback branch of _same_model_family "
        "and this test does not reach it")
    assert "MODEL_CREDIT_EXHAUSTED spent" in capsys.readouterr().out


@pytest.mark.parametrize("named,target", [
    ("Claude Opus", "claude-opus-5[1m]"),
    ("Opus 5", "claude-opus-5[1m]"),
    ("Opus 4.5", "claude-opus-5"),
    ("Claude Sonnet 5", "claude-sonnet-5"),
    ("Mythos", "claude-mythos-5-1"),
])
def test_the_family_guard_holds_however_the_banner_words_the_model(
        idler, monkeypatch, capsys, named, target):
    calls = _capture_runs(idler, monkeypatch)
    monkeypatch.setattr(idler, "worker_fallback_model", lambda: target)
    _worker_on_banner(idler, named)
    idler.main()
    assert _send_text_calls(calls) == [], (
        f"banner named {named!r} and the fallback is {target!r} — the same quota. The "
        "guard must key on the family the banner names, not on the exact word: the "
        "wording is vendor text we have captured exactly once, and DETECTION already "
        "refuses a wording dependency")
    assert "MODEL_CREDIT_EXHAUSTED spent" in capsys.readouterr().out


def test_a_credit_dead_manager_is_not_told_to_set_a_variable_that_cannot_reach_it(
        nudgy, monkeypatch, capsys):
    calls = _capture_runs(nudgy, monkeypatch)
    _write_record(nudgy, "mgr1", agent="manager", state="idle", name="mgr-A",
                  window_id="%3", parent_manager_name=None)
    _write_banner(nudgy, "mgr1",
                  banner_text=_banner_naming("Opus"))
    nudgy.main(manager_name="mgr-A")
    out = capsys.readouterr().out
    assert _send_text_calls(calls) == [], (
        "MANAGER_MODEL_FALLBACK_MODEL_TOKEN resolves the @opus family, so an Opus "
        "banner leaves no target")
    assert "MODEL_CREDIT_EXHAUSTED mgr-A" in out
    assert "CLAUDE_ORCH_MODEL_FALLBACK" not in out, (
        "MANAGER_MODEL_FALLBACK_MODEL_TOKEN is hardcoded and the guide says that variable "
        "retunes workers only — naming it here sends the manager to export something "
        "that cannot reach its own lane, and it gets the same page next scan")
    assert "by hand" in out and "YOUR OWN pane" in out, (
        "a manager reads this in its own pane, so the only remedy it can act on is "
        "switching that pane itself")


def test_a_rescue_onto_a_different_family_still_types(idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    monkeypatch.setattr(idler, "worker_fallback_model", lambda: "claude-sonnet-5")
    _worker_on_banner(idler, "Opus")
    idler.main()
    assert _silent_only(capsys, silent, "MODEL_FALLBACK spent")
    assert [t for _, t in _send_text_calls(calls)] == [
        "/model claude-sonnet-5",
        f"/effort {idler.MODEL_FALLBACK_EFFORT}",
        idler.NUDGE_TEXT,
    ], "an Opus-spent worker still has Sonnet credits; this rescue must still fire"


def test_credit_banner_switches_once_and_does_not_type_again(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "spent", 3)
    _write_banner(idler, "w1")
    idler.main()
    capsys.readouterr()
    before = len(_send_text_calls(calls))
    idler.main()
    assert "MODEL_FALLBACK" not in capsys.readouterr().out
    assert len(_send_text_calls(calls)) == before, "the same banner switched twice"


def test_credit_banner_switch_is_recorded_in_the_account_ledger(idler, monkeypatch, capsys):
    _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "spent", 3)
    _write_banner(idler, "w1")
    idler.main()
    capsys.readouterr()
    rows = [json.loads(line) for line in
            idler.ACCOUNT_LEDGER.read_text().splitlines() if line.strip()]
    switches = [r for r in rows if r.get("event") == "model-fallback"]
    assert len(switches) == 1
    assert switches[0]["model"] == idler.worker_fallback_model()
    assert switches[0]["effort"] == idler.MODEL_FALLBACK_EFFORT
    assert switches[0]["worker"] == "spent"


def test_a_worker_that_recovered_with_a_tool_call_is_not_switched(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "recovered", 3)
    _write_banner(idler, "w1", _tool_only_line())
    idler.main()
    assert "MODEL_FALLBACK" not in capsys.readouterr().out
    assert _send_text_calls(calls) == [], "a prose-free recovery turn still counts"


def test_a_worker_writing_the_banner_text_itself_is_not_switched(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "talker", 3)
    _write_transcript(idler, "w1",
                      "Done: it matches `reached your` plus `/usage-credits`.")
    idler.main()
    assert "MODEL_FALLBACK" not in capsys.readouterr().out
    assert _send_text_calls(calls) == [], "a real model wrote that, not the CLI"


def test_another_cli_error_banner_does_not_reach_the_model_switch(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "limited", 3)
    _write_banner(idler, "w1")
    log = idler.CLAUDE_PROJECTS / "proj" / "w1.jsonl"
    log.write_text(_banner_line("You've hit your session limit. Resets at 3pm.") + "\n")
    idler.main()
    out = capsys.readouterr().out
    assert "MODEL_FALLBACK" not in out
    assert [t for _, t in _send_text_calls(calls)
            if t.startswith("/model")] == [], "a session limit is not a credit limit"


def test_credit_banner_pages_when_autonudge_is_off(stale, monkeypatch, capsys):
    monkeypatch.setattr(stale, "_awake_seconds", lambda: 1_000_000)
    monkeypatch.setattr(stale, "IDLE_THRESHOLD_SEC", 7200)
    calls = _capture_runs(stale, monkeypatch)
    _idle_worker(stale, "w1", "spent", 3)
    _write_banner(stale, "w1")
    stale.main()
    out = capsys.readouterr().out
    assert "MODEL_CREDIT_EXHAUSTED spent" in out
    assert _send_text_calls(calls) == [], "autonudge off means nothing is typed"


def test_a_switch_that_did_not_take_pages_instead_of_staying_quiet(idler, silent, monkeypatch, capsys):
    _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "spent", 3)
    _write_banner(idler, "w1")
    idler.main()
    assert _silent_only(capsys, silent, "MODEL_FALLBACK spent")
    monkeypatch.setattr(idler, "MODEL_FALLBACK_CONFIRM_SEC", 0)
    idler.main()
    assert "MODEL_CREDIT_EXHAUSTED spent" in capsys.readouterr().out


def test_the_switch_does_not_wipe_the_idle_nudge_memo(idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "stalled", 15)
    idler.main()
    assert _silent_only(capsys, silent, "IDLE_NUDGED stalled")
    _write_banner(idler, "w1")
    idler.main()
    assert _silent_only(capsys, silent, "MODEL_FALLBACK stalled")
    typed = [t for _, t in _send_text_calls(calls)]
    idler.main()
    capsys.readouterr()
    assert [t for _, t in _send_text_calls(calls)] == typed, (
        "the idle nudge fired a second time after the switch wiped its memo")


def test_the_switch_is_held_while_the_pane_shows_a_live_turn(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    monkeypatch.setattr(idler, "_pane_shows_live_turn",
                        lambda window_id, memo=None: True)
    _idle_worker(idler, "w1", "working", 3)
    _write_banner(idler, "w1")
    idler.main()
    assert "MODEL_FALLBACK" not in capsys.readouterr().out
    assert _send_text_calls(calls) == [], "never type into a pane mid-turn"


def test_a_worker_that_recovered_then_exhausts_again_is_not_switched_twice(
        idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "spent", 3)
    _write_banner(idler, "w1")
    idler.main()
    assert _silent_only(capsys, silent, "MODEL_FALLBACK spent")
    typed = len(_send_text_calls(calls))
    log = idler.CLAUDE_PROJECTS / "proj" / "w1.jsonl"
    log.write_text(_assistant_line("Back on it, ran the suite.") + "\n")
    idler.main()
    capsys.readouterr()
    log.write_text(_banner_line() + "\n")
    idler.main()
    capsys.readouterr()
    assert len(_send_text_calls(calls)) == typed, (
        "a second exhaustion means the FALLBACK model ran out, so retyping it is a no-op")


def test_a_page_flushed_out_of_a_manager_limit_window_does_not_retype_the_switch(
        idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    t0 = 1_000_000
    clock = {"now": t0}
    monkeypatch.setattr(idler.time, "time", lambda: clock["now"])
    _write_limited_manager(idler, "mgr1", "mgr-A", t0)
    _idle_worker(idler, "w1", "spent", 3, parent_manager_name="mgr-A")
    _write_banner(idler, "w1")
    idler.main(manager_name="mgr-A")
    capsys.readouterr()
    typed = len(_send_text_calls(calls))
    assert typed == 3, f"the first scan should type exactly the three lines, got {typed}"
    clock["now"] = t0 + idler.MODEL_FALLBACK_CONFIRM_SEC + 1
    idler.main(manager_name="mgr-A")
    capsys.readouterr()
    cleared = _write_transcript(idler, "mgr1", "back to work")
    os.utime(cleared, (clock["now"] + 1, clock["now"] + 1))
    clock["now"] += 2
    idler.main(manager_name="mgr-A")
    capsys.readouterr()
    clock["now"] += 1
    idler.main(manager_name="mgr-A")
    capsys.readouterr()
    assert len(_send_text_calls(calls)) == typed, (
        "a page key popped by the limit flush re-armed the typed action")


def test_a_credit_dead_worker_is_not_autoclosed_in_the_scan_that_rescues_it(
        idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "spent", 130)
    _write_banner(idler, "w1")
    idler.main()
    out = capsys.readouterr().out
    assert "MODEL_FALLBACK spent" not in out and _said(silent, "MODEL_FALLBACK spent")
    assert (idler.ACTIVE / "w1.json").exists(), "closed a worker the same scan rescues"
    assert [c for c in calls if c[0] == "close"] == []


def test_the_idle_nudge_does_not_type_into_a_credit_dead_pane_later(
        idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "spent", 20)
    _write_banner(idler, "w1")
    idler.main()
    assert _silent_only(capsys, silent, "MODEL_FALLBACK spent")
    typed = [t for _, t in _send_text_calls(calls)]
    idler.main()
    out = capsys.readouterr().out
    assert "IDLE_NUDGED" not in out, "a credit-dead pane cannot answer a nudge"
    assert [t for _, t in _send_text_calls(calls)] == typed


def test_a_turn_between_scans_does_not_retype_the_switch(idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    path = _idle_worker(idler, "w1", "spent", 3)
    _write_banner(idler, "w1")
    idler.main()
    assert _silent_only(capsys, silent, "MODEL_FALLBACK spent")
    typed = len(_send_text_calls(calls))
    rec = json.loads(path.read_text())
    rec["state"] = "processing"
    path.write_text(json.dumps(rec))
    idler.main()
    capsys.readouterr()
    rec["state"] = "idle"
    path.write_text(json.dumps(rec))
    idler.main()
    out = capsys.readouterr().out
    assert "MODEL_FALLBACK" not in out, (
        "the resume nudge makes the worker processing; that must not re-arm the switch")
    assert len(_send_text_calls(calls)) == typed


def test_a_turn_between_scans_still_lets_the_did_not_take_page_fire(
        idler, monkeypatch, capsys):
    _capture_runs(idler, monkeypatch)
    path = _idle_worker(idler, "w1", "spent", 3)
    _write_banner(idler, "w1")
    idler.main()
    capsys.readouterr()
    rec = json.loads(path.read_text())
    rec["state"] = "processing"
    path.write_text(json.dumps(rec))
    idler.main()
    capsys.readouterr()
    rec["state"] = "idle"
    path.write_text(json.dumps(rec))
    monkeypatch.setattr(idler, "MODEL_FALLBACK_CONFIRM_SEC", 0)
    idler.main()
    assert "MODEL_CREDIT_EXHAUSTED spent" in capsys.readouterr().out


def test_a_record_rewritten_under_the_same_sid_is_not_switched_twice(
        idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    path = _idle_worker(idler, "w1", "spent", 3)
    _write_banner(idler, "w1")
    idler.main()
    assert _silent_only(capsys, silent, "MODEL_FALLBACK spent")
    typed = len(_send_text_calls(calls))
    rec = json.loads(path.read_text())
    rec["started_at"] = time.time() + 3600
    rec["pid"] = 999999
    rec["window_id"] = "%77"
    path.write_text(json.dumps(rec))
    idler.main()
    capsys.readouterr()
    assert len(_send_text_calls(calls)) == typed, (
        "the fire is once per record and no clock or pid change re-arms it")


def test_a_dead_workers_key_does_not_outlive_its_record(idler, monkeypatch, capsys):
    _capture_runs(idler, monkeypatch)
    path = _idle_worker(idler, "w1", "spent", 3)
    _write_banner(idler, "w1")
    idler.main()
    capsys.readouterr()
    assert "model-fallback:w1" in json.loads(idler._emitted_state_path(None).read_text())
    path.unlink()
    idler.main()
    capsys.readouterr()
    left = json.loads(idler._emitted_state_path(None).read_text())
    assert not any(k.startswith("model-fallback") for k in left), (
        "a closed worker must not keep state alive in the ledger")


def test_a_pane_that_refused_the_send_is_not_retried(idler, monkeypatch, capsys):
    calls = []
    accepting = {"ok": False}

    class Driver:
        def send_text_checked(self, window_id, text):
            calls.append((window_id, text))
            return accepting["ok"]

        def send_text(self, window_id, text, submit=True):
            calls.append((window_id, text))

        def close(self, window_id):
            pass

    monkeypatch.setattr(idler, "_get_driver", lambda: Driver())
    _idle_worker(idler, "w1", "spent", 3)
    _write_banner(idler, "w1")
    idler.main()
    out = capsys.readouterr().out
    assert "MODEL_CREDIT_EXHAUSTED spent" in out
    assert "unsubmitted on the input line" in out, (
        "a refused send can leave pasted text on the pane; the page must say so")
    sent = len(calls)
    accepting["ok"] = True
    idler.main()
    capsys.readouterr()
    assert len(calls) == sent, (
        "retrying would paste on top of the residue and submit a concatenated line")


def test_the_confirm_page_does_not_claim_the_switch_was_typed(idler, monkeypatch, capsys):
    _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "spent", 3)
    _write_banner(idler, "w1")
    idler.main()
    capsys.readouterr()
    monkeypatch.setattr(idler, "MODEL_FALLBACK_CONFIRM_SEC", 0)
    idler.main()
    out = capsys.readouterr().out
    assert "MODEL_CREDIT_EXHAUSTED spent" in out
    assert "the switch was typed" not in out, (
        "delivery is not proof of execution, and a refused send reaches this page too")


def test_the_confirm_page_names_the_model_the_banner_reports(idler, monkeypatch, capsys):
    _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w1", "spent", 3)
    _write_banner(idler, "w1")
    idler.main()
    capsys.readouterr()
    monkeypatch.setattr(idler, "MODEL_FALLBACK_CONFIRM_SEC", 0)
    idler.main()
    out = capsys.readouterr().out
    assert "MODEL_CREDIT_EXHAUSTED spent" in out
    assert "names Fable" in out, (
        "the reader needs the banner's own model name to tell the two states apart")


def test_every_credit_page_names_both_actions(idler, monkeypatch, capsys):
    class Refusing:
        def send_text_checked(self, window_id, text):
            return False

        def send_text(self, window_id, text, submit=True):
            pass

        def close(self, window_id):
            pass

    monkeypatch.setattr(idler, "_get_driver", lambda: Refusing())
    _idle_worker(idler, "w1", "refused", 3)
    _write_banner(idler, "w1")
    idler.main()
    refusal = capsys.readouterr().out
    assert "MODEL_CREDIT_EXHAUSTED refused" in refusal
    assert "tell it to resume" in refusal, "the refusal page must name the resume step"

    _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "w2", "spent", 3)
    _write_banner(idler, "w2")
    idler.main()
    capsys.readouterr()
    monkeypatch.setattr(idler, "MODEL_FALLBACK_CONFIRM_SEC", 0)
    idler.main()
    confirm = capsys.readouterr().out
    assert "MODEL_CREDIT_EXHAUSTED spent" in confirm
    assert "tell it to resume" in confirm, "the confirm page must name the resume step"
    assert "every worker this lane rescues will die the same way" in confirm, (
        "a worker's fallback IS the model every other worker is rescued onto, so a "
        "spent one really does doom the lane")



def _bannered_worker(stale, sid, name, state, **overrides):
    record = {"name": name, "state": state, "window_id": "%9",
              "parent_manager_name": None}
    record.update(overrides)
    path = _write_record(stale, sid, **record)
    log = _write_banner(stale, sid)
    return path, log


def _age(*paths, seconds):
    stamp = int(time.time()) - seconds
    for path in paths:
        os.utime(path, (stamp, stamp))
    return stamp


def test_a_state_the_lane_has_never_seen_still_reaches_the_credit_action(
        idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _bannered_worker(idler, "w1", "quiescer", "quiescing")
    idler.main()
    assert _silent_only(capsys, silent, "MODEL_FALLBACK quiescer")
    assert [t for _, t in _send_text_calls(calls)] == [
        f"/model {idler.worker_fallback_model()}",
        f"/effort {idler.MODEL_FALLBACK_EFFORT}",
        idler.NUDGE_TEXT,
    ], "a state nobody wrote a branch for must not skip the credit check"


def test_a_processing_worker_on_the_banner_is_switched_and_ledgered(
        idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _bannered_worker(idler, "w1", "bricked", "processing")
    idler.main()
    assert _silent_only(capsys, silent, "MODEL_FALLBACK bricked")
    assert [t for _, t in _send_text_calls(calls)] == [
        f"/model {idler.worker_fallback_model()}",
        f"/effort {idler.MODEL_FALLBACK_EFFORT}",
        idler.NUDGE_TEXT,
    ]
    rows = [json.loads(line) for line in
            idler.ACCOUNT_LEDGER.read_text().splitlines() if line.strip()]
    switches = [r for r in rows if r.get("event") == "model-fallback"]
    assert len(switches) == 1
    assert switches[0]["worker"] == "bricked"
    assert switches[0]["model"] == idler.worker_fallback_model()
    assert switches[0]["effort"] == idler.MODEL_FALLBACK_EFFORT


def test_a_processing_worker_on_the_banner_pages_when_autonudge_is_off(
        stale, monkeypatch, capsys):
    monkeypatch.setattr(stale, "_awake_seconds", lambda: 1_000_000)
    calls = _capture_runs(stale, monkeypatch)
    _bannered_worker(stale, "w1", "bricked", "processing")
    stale.main()
    assert "MODEL_CREDIT_EXHAUSTED bricked" in capsys.readouterr().out
    assert _send_text_calls(calls) == [], "autonudge off means nothing is typed"


def test_a_bricked_processing_worker_is_never_nudged_onto_its_dead_model(
        idler, silent, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    path, log = _bannered_worker(idler, "w1", "bricked", "processing")
    _age(path, log, seconds=2700)
    idler.main()
    out = capsys.readouterr().out
    assert [t for _, t in _send_text_calls(calls)] == [
        f"/model {idler.worker_fallback_model()}",
        f"/effort {idler.MODEL_FALLBACK_EFFORT}",
        idler.NUDGE_TEXT,
    ], "the switch is the only thing typed; a bare nudge re-submits on the dead model"
    assert "MODEL_FALLBACK bricked" not in out and _said(silent, "MODEL_FALLBACK bricked")
    assert "NUDGED bricked" not in out, (
        "the stale-processing ladder typed a resume onto a model that is out of credits")


def test_a_bricked_processing_worker_is_still_paged_when_the_switch_is_blocked(
        idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    path, log = _bannered_worker(idler, "w1", "bricked", "processing")
    _write_question(idler, "q1", "w1")
    _age(path, log, seconds=2700)
    idler.main()
    out = capsys.readouterr().out
    assert "STALE_PROCESSING bricked" in out, (
        "a worker whose switch the lane could not type must still reach the manager")
    assert _send_text_calls(calls) == [], "a blocked pane takes no text"


def test_a_subworker_pane_is_never_typed_the_switch(idler, monkeypatch, capsys):
    calls = _capture_runs(idler, monkeypatch)
    _bannered_worker(idler, "c1", "child", "processing",
                     kind="subworker", runtime="claude", provider="claude-exec")
    idler.main()
    assert "MODEL_FALLBACK" not in capsys.readouterr().out
    assert _send_text_calls(calls) == [], (
        "a subworker pane runs a one-shot CLI invocation; /model typed there is not a switch")


def test_a_credit_dead_managers_confirm_page_does_not_claim_a_fleet_outage(
        nudgy, monkeypatch, capsys):
    _capture_runs(nudgy, monkeypatch)
    _idle_manager_on_banner(nudgy)
    nudgy.main(manager_name="mgr-A")
    capsys.readouterr()
    monkeypatch.setattr(nudgy, "MODEL_FALLBACK_CONFIRM_SEC", 0)
    nudgy.main(manager_name="mgr-A")
    confirm = capsys.readouterr().out
    assert "MODEL_CREDIT_EXHAUSTED mgr-A" in confirm
    assert nudgy.manager_fallback_model() != nudgy.worker_fallback_model(), (
        "this test is only meaningful while the two lanes rescue onto different pools")
    assert "every worker this lane rescues will die the same way" not in confirm, (
        "the manager falls back to a different pool than the workers do, so its spent "
        "fallback says nothing about them — the engineer reading this page decides "
        "between a fleet outage and one dead manager")


def _idle_manager_on_banner(stale, sid="mgr1", name="mgr-A"):
    path = _write_record(stale, sid, agent="manager", state="idle", name=name,
                         window_id="%3", parent_manager_name=None)
    _write_banner(stale, sid)
    return path


def test_a_credit_dead_manager_is_switched_and_told_to_resume_orchestration(
        nudgy, silent, monkeypatch, capsys):
    calls = _capture_runs(nudgy, monkeypatch)
    _idle_manager_on_banner(nudgy)
    nudgy.main(manager_name="mgr-A")
    out = capsys.readouterr().out
    assert "MODEL_FALLBACK mgr-A" not in out and _said(silent, "MODEL_FALLBACK mgr-A")
    typed = [t for _, t in _send_text_calls(calls)]
    assert typed[0] == f"/model {nudgy.manager_fallback_model()}"
    assert "[1m]" in nudgy.manager_fallback_model(), "a manager keeps a 1M window"
    assert "resume orchestration" in typed[2]
    assert "The banner named Fable" in typed[2]
    assert typed[1] == f"/effort {nudgy.MANAGER_MODEL_FALLBACK_EFFORT}", (
        f"the lane typed {typed[1]!r}; the rescue lands the manager on Opus, so it types "
        f"the effort paired with that target ({nudgy.MANAGER_MODEL_FALLBACK_EFFORT!r}), "
        "and the 600s confirm page tells the engineer to reproduce whatever it typed by hand")
    spent = nudgy._model_credit_banner_model(CREDIT_BANNER_TEXT)
    assert spent and spent.lower() not in typed[0].lower(), (
        f"the lane typed {typed[0]!r} at a manager whose banner named {spent!r} — "
        "a rescue onto the model that just ran out is a no-op, and the manager "
        "stays dead until the 600s confirm page")
    assert "on every spawn" not in typed[2], (
        "the manager's model and the workers' are set independently, so a manager "
        "credit banner says nothing about what the workers should spawn on")


def test_a_credit_dead_manager_is_switched_once_across_scans(nudgy, monkeypatch, capsys):
    calls = _capture_runs(nudgy, monkeypatch)
    _idle_manager_on_banner(nudgy)
    nudgy.main(manager_name="mgr-A")
    capsys.readouterr()
    typed = len(_send_text_calls(calls))
    nudgy.main(manager_name="mgr-A")
    nudgy.main(manager_name="mgr-A")
    assert "MODEL_FALLBACK" not in capsys.readouterr().out
    assert len(_send_text_calls(calls)) == typed, "the manager's key must survive its own scans"


def test_a_credit_dead_manager_switch_is_recorded_as_a_manager(nudgy, monkeypatch, capsys):
    _capture_runs(nudgy, monkeypatch)
    _idle_manager_on_banner(nudgy)
    nudgy.main(manager_name="mgr-A")
    capsys.readouterr()
    rows = [json.loads(l) for l in nudgy.ACCOUNT_LEDGER.read_text().splitlines() if l.strip()]
    switches = [r for r in rows if r.get("event") == "model-fallback"]
    assert len(switches) == 1 and switches[0]["agent"] == "manager"
    assert switches[0]["worker"] == "mgr-A"


def test_a_credit_dead_manager_pages_when_autonudge_is_off(stale, monkeypatch, capsys):
    calls = _capture_runs(stale, monkeypatch)
    _idle_manager_on_banner(stale)
    stale.main(manager_name="mgr-A")
    out = capsys.readouterr().out
    assert "MODEL_CREDIT_EXHAUSTED mgr-A" in out
    assert "YOUR OWN pane" in out, "a manager's own page must not read as a worker's"
    assert _send_text_calls(calls) == []


def test_a_credit_banner_on_a_manager_never_flips_the_account(nudgy, monkeypatch, capsys):
    calls = _capture_runs(nudgy, monkeypatch)
    monkeypatch.setattr(nudgy, "_keychain_unlocked", lambda: True)
    _arm_pool(nudgy, "a")
    _idle_manager_on_banner(nudgy)
    nudgy.main(manager_name="mgr-A")
    out = capsys.readouterr().out
    assert "SWITCHED" not in out
    assert not [c for c in calls if c[0] == "spawn"], "no recovery tab for a credit banner"
    assert not (nudgy.ROOT / ".manager-limited-mgr-A").exists()


def test_a_session_limited_manager_still_takes_the_flip_path(nudgy, monkeypatch, capsys):
    calls = _capture_runs(nudgy, monkeypatch)
    monkeypatch.setattr(nudgy, "_keychain_unlocked", lambda: True)
    _arm_pool(nudgy, "a")
    t0 = 1_000_000
    _write_limited_manager(nudgy, "mgr1", "mgr-A", t0)
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main(manager_name="mgr-A")
    assert not [t for _, t in _send_text_calls(calls) if t.startswith("/model")]
    assert (nudgy.ROOT / ".manager-limited-mgr-A").exists()


def test_the_manager_success_record_is_addressed_to_the_manager(nudgy, silent, monkeypatch, capsys):
    _capture_runs(nudgy, monkeypatch)
    _idle_manager_on_banner(nudgy)
    nudgy.main(manager_name="mgr-A")
    assert "MODEL_FALLBACK" not in capsys.readouterr().out
    fields = next(f for kind, f, _ in silent if kind == "MODEL_FALLBACK")
    assert fields["name"] == "mgr-A" and fields["target"] == "YOUR OWN pane"
    assert fields["told"] == "and left you a resume line"


def test_the_env_var_still_steers_the_worker_fallback(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_MODEL_FALLBACK", "@haiku")
    assert _load_stale_monitor().MODEL_FALLBACK_MODEL_TOKEN == "@haiku"
    monkeypatch.delenv("CLAUDE_ORCH_MODEL_FALLBACK")
    assert _load_stale_monitor().MODEL_FALLBACK_MODEL_TOKEN == "@sonnet"


@pytest.mark.parametrize("returncode,stdout,expected", [
    (0, "claude-opus-5-5[1m]\n", "claude-opus-5-5[1m]"),
    (1, "", None),
    (0, "", None),
    (0, "   \n", None),
], ids=["resolved", "unknown-family", "empty-stdout", "blank-stdout"])
def test_resolve_model_token_reads_the_cli(monkeypatch, returncode, stdout, expected):
    mod = _load_stale_monitor()
    argvs = []

    def fake_run(argv, **kw):
        argvs.append(argv)
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod._resolve_model_token("@opus[1m]") == expected
    assert argvs == [[sys.executable, "-m", "dockwright", "model", "resolve", "@opus[1m]"]], (
        "the resolver must run the same tree as the monitor code asking it; a bare "
        "`dockwright` on PATH is a different install that can predate the model subcommand")


@pytest.mark.parametrize("boom", [
    OSError("no python on PATH"),
    subprocess.TimeoutExpired(cmd="python -m dockwright", timeout=30),
], ids=["oserror", "timeout"])
def test_resolve_model_token_is_none_when_the_cli_cannot_run(monkeypatch, boom):
    mod = _load_stale_monitor()

    def fake_run(argv, **kw):
        raise boom

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod._resolve_model_token("@opus[1m]") is None


def test_a_concrete_id_never_shells_out(monkeypatch):
    mod = _load_stale_monitor()

    def fake_run(argv, **kw):
        raise AssertionError(f"a concrete id must not reach the CLI: {argv}")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod._resolve_model_token("claude-haiku-4-5") == "claude-haiku-4-5"


def test_no_subprocess_runs_at_import(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    _load_stale_monitor()
    assert calls == [], "resolution must be lazy; hooks.py imports this on every hook fire"


def test_manager_fallback_resolves_to_a_concrete_1m_id(nudgy, monkeypatch):
    asked = []
    monkeypatch.setattr(nudgy, "_resolve_model_token",
                        lambda v: asked.append(v) or "claude-opus-5-5[1m]")
    nudgy.manager_fallback_model.cache_clear()
    got = nudgy.manager_fallback_model()
    assert asked == ["@opus[1m]"], "the manager lane rescues onto the opus family, 1M variant"
    assert "@" not in got and got.endswith("[1m]"), "a manager keeps a 1M window"


def test_manager_and_worker_fallbacks_stay_different(nudgy, monkeypatch):
    monkeypatch.setattr(nudgy, "_resolve_model_token",
                        lambda v: v.replace("@opus", "claude-opus-5-5").replace(
                            "@sonnet", "claude-sonnet-5"))
    nudgy.manager_fallback_model.cache_clear()
    nudgy.worker_fallback_model.cache_clear()
    assert nudgy.manager_fallback_model() != nudgy.worker_fallback_model()


def test_same_family_refusal_still_fires_for_a_credit_dead_opus_manager(nudgy, monkeypatch):
    monkeypatch.setattr(nudgy, "_resolve_model_token", lambda v: "claude-opus-5-5[1m]")
    nudgy.manager_fallback_model.cache_clear()
    assert nudgy._same_model_family("claude-opus-5-5", nudgy.manager_fallback_model()) is True


def _drive_manager_on_banner(stale, monkeypatch, capsys):
    calls = _capture_runs(stale, monkeypatch)
    _idle_manager_on_banner(stale)
    stale.main(manager_name="mgr-A")
    return capsys.readouterr().out.splitlines(), _send_text_calls(calls)


def _drive_worker_confirm_recheck(stale, monkeypatch, capsys):
    _capture_runs(stale, monkeypatch)
    _bannered_worker(stale, "w1", "spent", "processing")
    stale.main()
    capsys.readouterr()
    monkeypatch.setattr(stale, "MODEL_FALLBACK_CONFIRM_SEC", 0)
    stale.main()
    return capsys.readouterr().out.splitlines()


def test_a_failed_manager_resolve_pages_and_types_nothing(nudgy, monkeypatch, capsys):
    monkeypatch.setattr(nudgy, "_resolve_model_token", lambda v: None)
    nudgy.manager_fallback_model.cache_clear()
    pages, sends = _drive_manager_on_banner(nudgy, monkeypatch, capsys)
    assert sends == [], "nothing may be typed when the target is unknown"
    assert any("could not be resolved" in p for p in pages)
    assert any("`python -m dockwright model resolve` failed" in p for p in pages), (
        "the page fires exactly when an operator is diagnosing why nothing was typed, so it "
        "must name the invocation the resolver actually runs — a bare `dockwright` on PATH is "
        "a different install")
    assert any("MODEL_CREDIT_EXHAUSTED mgr-A" in p for p in pages), (
        "a credit-dead manager with no reachable target must still reach the human")
    assert not any("None" in p for p in pages), (
        "the page must not hand the manager an unresolved target to switch to")


def test_a_failed_resolve_refuses_the_recovery_launch(nudgy, monkeypatch, capsys):
    monkeypatch.setattr(nudgy, "_resolve_model_token", lambda v: None)
    nudgy.manager_fallback_model.cache_clear()
    spawned = []

    class FakeDrv:
        async def spawn(self, **kw):
            spawned.append(kw)
            return "%9"

    monkeypatch.setattr(nudgy, "_get_driver", lambda: FakeDrv())
    assert nudgy._launch_recovery_manager({"cwd": "/c", "name": "m"}, "sid-1", "a") is None
    assert spawned == [], "a recovery manager must not launch onto an unknown model"
    assert "recovery launch refused" in capsys.readouterr().err


def test_the_fallback_itself_is_out_clause_still_fires(idler, monkeypatch, capsys):
    monkeypatch.setattr(idler, "_resolve_model_token", lambda v: "claude-sonnet-5")
    idler.worker_fallback_model.cache_clear()
    pages = _drive_worker_confirm_recheck(idler, monkeypatch, capsys)
    assert any("the fallback itself is out" in p for p in pages)
    assert any("every worker this lane rescues will die the same way" in p for p in pages), (
        "the worker lane's own target is what it just spent, so the cascade clause "
        "must survive the identity test moving behind a function")


UNNAMED_CREDIT_BANNER_TEXT = ("You've reached your usage cap. Run /usage-credits to "
                              "continue or switch models with /model.")


def test_a_manager_whose_resolve_fails_never_inherits_the_worker_target(
        nudgy, monkeypatch, capsys):
    monkeypatch.setattr(nudgy, "_resolve_model_token",
                        lambda v: None if v == "@opus[1m]" else "claude-sonnet-5")
    nudgy.manager_fallback_model.cache_clear()
    nudgy.worker_fallback_model.cache_clear()
    pages, sends = _drive_manager_on_banner(nudgy, monkeypatch, capsys)
    assert sends == [], "a manager must never be typed the worker's pool"
    assert not any("claude-sonnet-5" in p for p in pages), (
        "one token resolving while the other fails is what the _UNSET sentinel exists for; "
        "an `or` here hands the manager the worker's target and loses its 1M window")
    assert any("could not be resolved" in p for p in pages)


def test_a_failed_worker_resolve_pages_and_holds_the_resume_ladder(idler, monkeypatch, capsys):
    monkeypatch.setattr(idler, "_resolve_model_token", lambda v: None)
    idler.worker_fallback_model.cache_clear()
    calls = _capture_runs(idler, monkeypatch)
    path, log = _bannered_worker(idler, "w1", "spent", "processing")
    _age(path, log, seconds=2700)
    idler.main()
    out = capsys.readouterr().out
    assert _send_text_calls(calls) == [], "nothing may be typed when the target is unknown"
    assert "MODEL_CREDIT_EXHAUSTED spent" in out and "could not be resolved" in out
    assert "NUDGED spent" not in out, (
        "page() returns MODEL_CREDIT_HELD, so pane_held stays truthy and the "
        "bare-resume ladder must not type onto a model that is out of credits")


def test_the_unresolved_page_survives_a_banner_it_cannot_name(nudgy, monkeypatch, capsys):
    assert nudgy._model_credit_banner_model(UNNAMED_CREDIT_BANNER_TEXT) is None, (
        "this fixture must defeat MODEL_CREDIT_NAME_RE or the test proves nothing")
    monkeypatch.setattr(nudgy, "_resolve_model_token", lambda v: None)
    nudgy.manager_fallback_model.cache_clear()
    calls = _capture_runs(nudgy, monkeypatch)
    _write_record(nudgy, "mgr1", agent="manager", state="idle", name="mgr-A",
                  window_id="%3", parent_manager_name=None)
    _write_banner(nudgy, "mgr1", banner_text=UNNAMED_CREDIT_BANNER_TEXT)
    nudgy.main(manager_name="mgr-A")
    out = capsys.readouterr().out
    assert "MODEL_CREDIT_EXHAUSTED mgr-A" in out
    assert "out of credits for its model" in out, (
        "detection needs two signatures, the NAME needs a stricter regex — a reworded "
        "banner that keeps both and drops ` limit` must not print `for None`")
    assert "None" not in out
    assert _send_text_calls(calls) == []


def test_a_worker_with_no_banner_never_resolves_a_fallback(stale, monkeypatch):
    def boom(value):
        raise AssertionError(f"the no-banner path must not resolve anything: {value}")

    monkeypatch.setattr(stale, "_resolve_model_token", boom)
    stale.worker_fallback_model.cache_clear()
    _write_record(stale, "w1", agent="worker", state="idle", name="quiet", window_id="%9")
    _write_transcript(stale, "w1", "all done")
    assert stale.main() == 0, (
        "the common path pays a subprocess for a value it never reads; the bind belongs "
        "below the banner check")


@pytest.fixture
def silent(stale, monkeypatch):
    record = []
    monkeypatch.setattr(stale, "_journal_silent",
                        lambda lane_kind, fields, sid: record.append((lane_kind, fields, sid)))
    return record


def _ticketed(stale, sid, ticket):
    stale.ASSIGNMENTS.mkdir(parents=True, exist_ok=True)
    (stale.ASSIGNMENTS / f"{sid}.json").write_text(json.dumps({"claude_sid": sid, "ticket": ticket}))
    journal = stale.ROOT / "artifacts" / ticket / "events.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.touch()
    return journal


def _lane_events_in(journal):
    events = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
    return [(e["kind"], e["fields"]) for e in events if e.get("type") == "lane_event"]


def test_a_first_nudge_is_silent_an_unanswered_repeat_wakes_and_a_resumed_resets(nudgy, silent, capsys,
                                                                               monkeypatch):
    calls = _capture_runs(nudgy, monkeypatch)
    path = _write_record(nudgy, "w1", agent="worker", state="processing", name="worker-tab", window_id="42")
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

    assert run_at(30) == ""
    assert silent == [("NUDGED", {"name": "worker-tab", "why": "30min", "repeat": False}, "w1")]
    assert run_at(60).startswith('NUDGED worker-tab (60min) → get_worker_tail("worker-tab"); ')
    os.utime(log, (t0 + 61 * 60, t0 + 61 * 60))
    assert run_at(62) == ""
    assert silent[-1] == ("RESUMED", {"name": "worker-tab"}, "w1")
    assert run_at(61 + 30) == ""
    assert silent[-1] == ("NUDGED", {"name": "worker-tab", "why": "30min", "repeat": False}, "w1")
    assert len(_send_text_calls(calls)) == 3


def test_a_first_nudge_lands_in_the_journal_of_a_ticketed_workers_work(nudgy, capsys, monkeypatch):
    _capture_runs(nudgy, monkeypatch)
    now = int(time.time())
    for sid, name in (("w1", "tab-one"), ("w2", "tab-two")):
        path = _write_record(nudgy, sid, agent="worker", state="processing", name=name, window_id=f"%{sid}")
        os.utime(path, (now - 2700, now - 2700))
    journal = _ticketed(nudgy, "w1", "K")
    nudgy.main()
    assert capsys.readouterr().out == ""
    assert _lane_events_in(journal) == [("NUDGED", {"name": "tab-one", "why": "45min", "repeat": False})]
    assert [p.parent.name for p in (nudgy.ROOT / "artifacts").glob("*/events.jsonl")] == ["K"]


def test_a_silent_line_is_journaled_while_the_manager_is_limited(nudgy, capsys, monkeypatch):
    _capture_runs(nudgy, monkeypatch)
    t0 = 1_000_000
    _write_limited_manager(nudgy, "mgr1", "mgr-A", t0)
    w = _write_record(nudgy, "w1", agent="worker", state="processing", name="worker-tab", window_id="42",
                      parent_manager_name="mgr-A")
    os.utime(w, (t0, t0))
    journal = _ticketed(nudgy, "w1", "K")
    monkeypatch.setattr(nudgy.time, "time", lambda: t0 + 30 * 60)
    nudgy.main(manager_name="mgr-A")
    assert capsys.readouterr().out == ""
    assert _lane_events_in(journal) == [("NUDGED", {"name": "worker-tab", "why": "30min", "repeat": False})]
    buffer = json.loads(nudgy._emitted_state_path("mgr-A").read_text())["limited_buffer"]
    assert buffer["nudged"] == 1 and buffer["stalled_names"] == ["worker-tab"]


def test_an_idle_nudge_lands_in_its_own_workers_journal(idler, capsys, monkeypatch):
    _capture_runs(idler, monkeypatch)
    busy = _write_record(idler, "a", agent="worker", state="processing", name="busy", window_id="%1")
    os.utime(busy, (int(time.time()) - 2700,) * 2)
    journal_a = _ticketed(idler, "a", "A")
    _idle_worker(idler, "b", "stalled", 15)
    journal_b = _ticketed(idler, "b", "B")
    idler.main()
    assert "IDLE_NUDGED" not in capsys.readouterr().out
    assert _lane_events_in(journal_b) == [("IDLE_NUDGED", {"name": "stalled", "minutes": 15})]
    assert [kind for kind, _ in _lane_events_in(journal_a)] == ["NUDGED"]


def test_an_idle_nudge_of_the_only_record_does_not_raise(idler, capsys, monkeypatch):
    _capture_runs(idler, monkeypatch)
    _idle_worker(idler, "b", "stalled", 15)
    journal_b = _ticketed(idler, "b", "B")
    assert idler.main() == 0
    assert _lane_events_in(journal_b) == [("IDLE_NUDGED", {"name": "stalled", "minutes": 15})]


def test_the_limit_reset_nudge_prints_nothing_and_is_recorded_silent(nudgy, silent, capsys, monkeypatch):
    calls = _capture_runs(nudgy, monkeypatch)
    path = _write_record(nudgy, "w1", agent="worker", state="processing", name="worker-tab", window_id="42")
    log = _write_transcript(nudgy, "w1", SESSION_LIMIT_TEXT)
    t0 = 1_000_000
    os.utime(path, (t0, t0))
    os.utime(log, (t0, t0))
    clock = {"now": t0 + 6 * 60}
    monkeypatch.setattr(nudgy.time, "time", lambda: clock["now"])
    nudgy.main()
    sched = json.loads(nudgy.EMITTED_STATE.read_text())["scheduled:w1"]
    clock["now"] = sched["at"]
    capsys.readouterr()
    sends = len(_send_text_calls(calls))
    nudgy.main()
    assert capsys.readouterr().out == ""
    assert len(_send_text_calls(calls)) == sends + 1
    assert silent[-1] == ("NUDGED", {"name": "worker-tab", "why": "limit-reset", "repeat": False}, "w1")


def test_a_waking_switched_line_lands_in_the_limit_rollup(nudgy, capsys, monkeypatch):
    t0 = 1_000_000
    buffer = {"since": t0, "stalled_names": [], "nudged": 0, "resumed": 0, "orphaned": 0,
              "switched": nudgy._head_of("SWITCHED", {"account": "a", "new": "b",
                                                      "subject": "manager mgr-A limited"}),
              "suppressed_keys": []}
    nudgy._emitted_state_path("mgr-A").write_text(json.dumps({"limited_buffer": buffer}))
    (nudgy.ROOT / ".manager-limited-mgr-A").touch()
    monkeypatch.setattr(nudgy.time, "time", lambda: t0 + 600)
    nudgy.main(manager_name="mgr-A")
    out = capsys.readouterr().out
    assert out.startswith("LIMIT_CLEARED ") and " → list_workers(manager_sid=<your sid>)" in out
    assert ", switched account a→b (manager mgr-A limited)" in out


def test_without_the_table_a_silent_kind_prints_its_kind_and_values(stale, monkeypatch, capsys):
    monkeypatch.setattr(stale, "_lane_lines", None)
    now = int(time.time())
    _stale_processing_worker(stale, "w1", "worker-tab", "mgr", now)
    assert stale._render_line("RESUMED", {"name": "worker-tab"}) == "RESUMED worker-tab"
    assert stale._head_of("SWITCHED", {"account": "a", "new": "b", "subject": "s"}) == "a b s"
    stale._journal_silent("RESUMED", {"name": "worker-tab"}, "w1")
    assert stale.main(manager_name="mgr") == 0
    assert capsys.readouterr().out.startswith("STALE_PROCESSING worker-tab 45")


def _said(silent, text):
    for i, (kind, fields, _sid) in enumerate(silent):
        if f"{kind} {lane_lines.head(kind, **fields)}".startswith(text):
            del silent[: i + 1]
            return True
    return False


def _silent_only(capsys, silent, text):
    return text not in capsys.readouterr().out and _said(silent, text)


def test_main_with_an_emit_callable_writes_nothing_to_stdout_and_drains_no_outbox(stale, tmp_path, monkeypatch,
                                                                                    capsys):
    (stale.ACTIVE).mkdir(parents=True, exist_ok=True)
    stale._write_json_atomic(stale.ACTIVE / "m.json", {"claude_sid": "m", "agent": "manager", "name": "mgr",
                                                       "pid": os.getpid(), "window_id": "w"})
    outbox = stale.ROOT / "notify-outbox" / "mgr"
    outbox.mkdir(parents=True)
    (outbox / "1.json").write_text('{"line": "gardener digest ready: run-1"}')
    monkeypatch.setattr(stale, "_lane_preflight", lambda fd=1: (_ for _ in ()).throw(AssertionError("preflight ran")))
    got = []
    assert stale.main("mgr", emit=got.append, lanes_in_manager=False) == 0
    assert capsys.readouterr().out == ""
    assert (outbox / "1.json").exists()
    assert (stale.ROOT / "lane-health" / "mgr" / "stale.json").exists()


def test_lane_silence_checks_only_the_pages_lane_when_lanes_run_in_the_daemon(stale, tmp_path):
    now = 10_000.0
    for lane in ("questions", "done", "turn-ends", "pages"):
        stale._write_json_atomic(stale._lane_heartbeat_path("mgr", lane), {"last_scan": now - 600})
    emitted = {"lane_check_tick": now - 60, "lane_stale_seen:pages": now - 60, "lane_stale_seen:questions": now - 60}
    out = stale._lane_silence_events("mgr", emitted, {}, now, lanes_in_manager=False)
    assert [fields["lane"] for _, fields in out] == ["pages"]
    out = stale._lane_silence_events("mgr", emitted, {}, now, lanes_in_manager=True)
    assert sorted(fields["lane"] for _, fields in out) == ["pages", "questions"]


def test_lane_silence_gives_the_pages_lane_at_least_six_seconds(stale):
    now = 10_000.0
    stale._write_json_atomic(stale._lane_heartbeat_path("mgr", "pages"), {"last_scan": now - 5})
    emitted = {"lane_check_tick": now - 60, "lane_stale_seen:pages": now - 60}
    assert stale._lane_silence_events("mgr", emitted, {}, now, lanes_in_manager=False) == []
    stale._write_json_atomic(stale._lane_heartbeat_path("mgr", "pages"), {"last_scan": now - 7})
    out = stale._lane_silence_events("mgr", emitted, {}, now, lanes_in_manager=False)
    assert [fields["lane"] for _, fields in out] == ["pages"]


def test_a_subworker_of_a_daemon_node_is_adopted_by_no_manager_while_the_daemon_is_alive(stale, tmp_path):
    (stale.ROOT / "daemon").mkdir(parents=True, exist_ok=True)
    stale._write_json_atomic(stale.ROOT / "daemon" / "alive.json", {"ts": time.time(), "awake": 1.0, "pid": 1})
    node = {"claude_sid": "n1", "agent": "worker", "kind": "worker", "name": "K-design", "parent_name": "daemon",
            "parent_manager_name": "daemon", "pid": os.getpid()}
    sub = {"claude_sid": "s1", "agent": "worker", "kind": "subworker", "name": "K-design-rev", "parent_name": "K-design",
           "pid": os.getpid()}
    records = [node, sub]
    assert stale._in_scope(node, "daemon", records) is True
    assert stale._in_scope(node, "mgr", records) is False
    assert stale._in_scope(sub, "mgr", records) is False
    assert stale._in_scope(sub, "daemon", records) is True
    stale._write_json_atomic(stale.ROOT / "daemon" / "alive.json", {"ts": time.time() - 1000, "awake": 1.0, "pid": 1})
    assert stale._in_scope(sub, "mgr", records) is True


SAFEGUARD_DIALOG = """\
⏺ Bash(cat docs/explainer.md)
  ⎿  Interrupted
✻ Waiting for API response · will retry in 2m 40s
╭──────────────────────────────────────────────────────────────╮
│ Session paused                                               │
│                                                              │
│ Fable 5.1's safeguards flagged this message. This sometimes  │
│ happens with safe, normal conversations.                     │
│ Details: `[cyber]`                                           │
│                                                              │
│ ❯ 1. Switch to Opus 4.8                                      │
│   2. Edit prompt and retry with Fable 5.1                    │
╰──────────────────────────────────────────────────────────────╯"""

SAFEGUARD_DIALOG_EDIT_FIRST = """\
╭──────────────────────────────────────────────────────────────╮
│ Session paused                                               │
│ Fable 5.1's safeguards flagged this message.                 │
│ ❯ 1. Edit prompt and retry with Fable 5.1                    │
│   2. Switch to Opus 4.8                                      │
╰──────────────────────────────────────────────────────────────╯"""

SAFEGUARD_EMPTY_INPUT = """\
⏺ Bash(cat docs/explainer.md)
  ⎿  Interrupted
╭──────────────────────────────────────────────────────────────╮
│ ❯                                                            │
╰──────────────────────────────────────────────────────────────╯"""

SAFEGUARD_TEXT_ABOVE_INPUT = """\
⏺ Update(deploy/skills/dockwright-orchestrator-guide/SKILL.md)
  ⎿  + Claude Code can pause a session with a "Session paused" dialog:
     + "1. Switch to <fallback model>" and "2. Edit prompt and retry with <model>".
     + ❯ 1. Switch to Opus 4.8
✻ Thinking… (13s · esc to interrupt)
╭──────────────────────────────────────────────────────────────╮
│ ❯                                                            │
╰──────────────────────────────────────────────────────────────╯"""

SAFEGUARD_RESTORED_INPUT = """\
⏺ Bash(cat docs/explainer.md)
  ⎿  Interrupted
╭──────────────────────────────────────────────────────────────╮
│ ❯ Read the ticket and write the design                       │
╰──────────────────────────────────────────────────────────────╯"""


def _safeguard_stop_lines(uuid="stop-1"):
    return (
        json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
             "content": "Not run: the response that made this tool call was stopped by a safety classifier."}]}}),
        json.dumps({"type": "system", "subtype": "informational", "level": "notice",
                    "content": "Fable 5.1's safeguards stopped the response above · continuing once with that noted"}),
        json.dumps({"type": "user", "isMeta": True, "uuid": uuid, "message": {
            "role": "user",
            "content": "Your response above was stopped by a safety classifier — this is not a tool or API error."}}),
        json.dumps({"type": "file-history-snapshot"}),
    )


def _quoted_stop_line():
    return json.dumps({"type": "user", "uuid": "relay-1", "message": {
        "role": "user", "content": "[MANAGER] it said: stopped by a safety classifier — retry"}})


def _write_session(stale, sid, *lines):
    project_dir = stale.CLAUDE_PROJECTS / "proj"
    project_dir.mkdir(parents=True, exist_ok=True)
    log = project_dir / f"{sid}.jsonl"
    log.write_text("\n".join(lines) + "\n")
    return log


class SafeguardPane:
    def __init__(self, screen, after_key, accepts_text):
        self.screen = screen
        self.after_key = after_key
        self.accepts_text = accepts_text
        self.calls = []

    def capture_screen(self, pane):
        return self.screen

    def capture_screen_ansi(self, pane):
        return self.screen

    def send_key(self, pane, key):
        self.calls.append(("key", pane, key))
        if self.after_key is not None:
            self.screen = self.after_key
        return True

    def send_text_checked(self, pane, text):
        self.calls.append(("text", pane, text))
        return self.accepts_text

    def send_text(self, pane, text, submit=True):
        self.calls.append(("text", pane, text))
        return self.accepts_text

    def close(self, pane):
        self.calls.append(("close", pane))


def _paused_worker(stale, monkeypatch, screen=SAFEGUARD_DIALOG, minutes=3, after_key=SAFEGUARD_EMPTY_INPUT,
                   accepts_text=True, lines=None, **record):
    pane = SafeguardPane(screen, after_key, accepts_text)
    monkeypatch.setattr(stale, "_get_driver", lambda: pane)
    monkeypatch.setattr(stale, "SAFEGUARD_SETTLE_STEP_SEC", 0)
    path = _write_record(stale, "w1", state="processing", name="fable-design", window_id="%227", **record)
    log = _write_session(stale, "w1", *(lines if lines is not None else _safeguard_stop_lines()))
    old = int(time.time()) - minutes * 60
    os.utime(path, (old, old))
    os.utime(log, (old, old))
    return pane


def test_a_safeguard_pause_is_cleared_with_edit_and_retry_then_continue(nudgy, monkeypatch, capsys):
    pane = _paused_worker(nudgy, monkeypatch)
    nudgy.main()
    out = capsys.readouterr().out
    assert pane.calls == [("key", "%227", "2"), ("text", "%227", "continue")]
    assert 'SAFEGUARD_PAUSE fable-design → get_worker_tail("fable-design")' in out


def test_the_tick_that_clears_a_long_safeguard_pause_pages_no_stale_processing_and_types_no_nudge(
        nudgy, monkeypatch, capsys):
    pane = _paused_worker(nudgy, monkeypatch, minutes=45)
    nudgy.main()
    out = capsys.readouterr().out
    assert pane.calls == [("key", "%227", "2"), ("text", "%227", "continue")]
    assert "STALE_PROCESSING" not in out and "NUDGED" not in out


@pytest.mark.parametrize("screen,lines", [
    (SAFEGUARD_DIALOG, [_tool_only_line()]),
    (SAFEGUARD_DIALOG, [_quoted_stop_line()]),
    (SAFEGUARD_EMPTY_INPUT, None),
    (SAFEGUARD_TEXT_ABOVE_INPUT, None),
], ids=["no-stop-note", "quoted-stop-note", "no-dialog", "dialog-text-above-the-input-box"])
def test_the_safeguard_lane_acts_only_when_the_transcript_and_the_pane_both_show_the_pause(
        nudgy, monkeypatch, capsys, screen, lines):
    pane = _paused_worker(nudgy, monkeypatch, screen=screen, lines=lines)
    nudgy.main()
    assert [c for c in pane.calls if c[0] == "key"] == []
    assert "SAFEGUARD_PAUSE" not in capsys.readouterr().out


def test_the_safeguard_lane_presses_the_number_the_dialog_gives_edit_and_retry(nudgy, monkeypatch, capsys):
    pane = _paused_worker(nudgy, monkeypatch, screen=SAFEGUARD_DIALOG_EDIT_FIRST)
    nudgy.main()
    capsys.readouterr()
    assert pane.calls[0] == ("key", "%227", "1")


def test_with_autonudge_off_a_safeguard_pause_pages_the_manual_steps(stale, monkeypatch, capsys):
    pane = _paused_worker(stale, monkeypatch)
    stale.main()
    out = capsys.readouterr().out
    assert pane.calls == []
    assert "SAFEGUARD_PAUSE fable-design → tell the engineer to press 2 (Edit prompt and retry) in pane %227" in out


def test_a_safeguard_pause_with_a_human_at_the_pane_is_left_to_the_human(nudgy, monkeypatch, capsys):
    pane = _paused_worker(nudgy, monkeypatch, human_attached_at=time.time())
    nudgy.main()
    assert pane.calls == []
    assert "SAFEGUARD_PAUSE" not in capsys.readouterr().out


def test_a_second_safeguard_flag_inside_the_retry_window_pages_instead_of_retrying(nudgy, monkeypatch, capsys):
    pane = _paused_worker(nudgy, monkeypatch)
    nudgy.main()
    capsys.readouterr()
    pane.screen = SAFEGUARD_DIALOG
    _write_session(nudgy, "w1", *_safeguard_stop_lines("stop-2"))
    nudgy.main()
    out = capsys.readouterr().out
    assert pane.calls == [("key", "%227", "2"), ("text", "%227", "continue")]
    assert "SAFEGUARD_PAUSE fable-design → tell the engineer it flagged twice" in out


@pytest.mark.parametrize("after_key", [None, SAFEGUARD_RESTORED_INPUT], ids=["dialog-stays", "prompt-restored"])
def test_a_safeguard_pane_that_does_not_come_back_empty_after_the_keypress_is_not_typed_into(
        nudgy, monkeypatch, capsys, after_key):
    pane = _paused_worker(nudgy, monkeypatch, after_key=after_key)
    nudgy.main()
    out = capsys.readouterr().out
    assert pane.calls == [("key", "%227", "2")]
    assert "SAFEGUARD_PAUSE fable-design → tell the engineer to look at pane %227" in out


def test_a_safeguard_continue_that_did_not_submit_pages(nudgy, monkeypatch, capsys):
    pane = _paused_worker(nudgy, monkeypatch, accepts_text=False)
    nudgy.main()
    out = capsys.readouterr().out
    assert pane.calls == [("key", "%227", "2"), ("text", "%227", "continue")]
    assert "continue did not submit" in out


def test_one_safeguard_flag_pages_once(stale, monkeypatch, capsys):
    _paused_worker(stale, monkeypatch)
    stale.main()
    stale.main()
    assert capsys.readouterr().out.count("SAFEGUARD_PAUSE fable-design") == 1


def _age_the_safeguard_retry(stale):
    state = json.loads(stale.EMITTED_STATE.read_text())
    state["safeguard:w1"]["at"] -= stale.SAFEGUARD_RETRY_WINDOW_SEC + 1
    stale.EMITTED_STATE.write_text(json.dumps(state))


def test_a_safeguard_flag_paged_as_again_stays_with_the_engineer_after_the_retry_window(nudgy, monkeypatch, capsys):
    pane = _paused_worker(nudgy, monkeypatch)
    nudgy.main()
    pane.screen = SAFEGUARD_DIALOG
    _write_session(nudgy, "w1", *_safeguard_stop_lines("stop-2"))
    nudgy.main()
    capsys.readouterr()
    _age_the_safeguard_retry(nudgy)
    nudgy.main()
    assert pane.calls == [("key", "%227", "2"), ("text", "%227", "continue")]
    assert "SAFEGUARD_PAUSE" not in capsys.readouterr().out


def test_a_safeguard_dialog_paged_as_unsettled_is_not_pressed_again_after_the_retry_window(nudgy, monkeypatch, capsys):
    pane = _paused_worker(nudgy, monkeypatch, after_key=None)
    nudgy.main()
    capsys.readouterr()
    _age_the_safeguard_retry(nudgy)
    nudgy.main()
    assert pane.calls == [("key", "%227", "2")]
    assert "SAFEGUARD_PAUSE" not in capsys.readouterr().out


def test_the_safeguard_lane_sees_a_dialog_drawn_above_blank_pane_rows(nudgy, monkeypatch, capsys):
    pane = _paused_worker(nudgy, monkeypatch, screen=SAFEGUARD_DIALOG + "\n" * 67)
    nudgy.main()
    capsys.readouterr()
    assert pane.calls == [("key", "%227", "2"), ("text", "%227", "continue")]


TALL_DIALOG = """\
⏺ Bash(cp /home/user/.claude-b/projects/p/s/tool-results/mcp.txt /home/user/.claude/scratch/k/x)
╭──────────────────────────────────────────────────────────────╮
│ Bash command                                                 │
│                                                              │
│ ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌ │
│ cp /home/user/.claude-b/projects/p/s/tool-results/mcp.txt    │
│ /home/user/.claude/scratch/k/x; head -c 400 x                │
│ ╌╌╌╌╌╌╌╌╌╌╌╌╌╌ Jump to bottom (click) ↓ ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌ │
"""

OLD_BOX = """\
⏺ Read(docs/plan.md)
  ⎿  Read 40 lines
╭──────────────────────────────────────────────────────────────╮
│ ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌ │
│ - old: a                                                     │
│ + new: b                                                     │
│ ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌ │
╰──────────────────────────────────────────────────────────────╯
╭──────────────────────────────────────────────────────────────╮
│ ❯                                                            │
╰──────────────────────────────────────────────────────────────╯
"""


def _marker(prompts_dir, sid, at, tool_name="Bash", summary="cp a b", decided=None, tool_use_id=None,
            closed_at=None, transcript_path=None, name="zeb4"):
    prompts_dir.mkdir(parents=True, exist_ok=True)
    path = prompts_dir / f"{sid}.json"
    path.write_text(json.dumps({
        "at": at, "sid": sid, "name": name, "tool_name": tool_name, "summary": summary,
        "transcript_path": transcript_path, "cwd": "/x", "decided": decided, "attempts": [],
        "tool_use_id": tool_use_id, "closed_at": closed_at}))
    return path


def _tool_use_record(ts, tool_use_id, command, name="Bash"):
    return {"type": "assistant", "timestamp": _iso_ts(ts), "message": {"content": [
        {"type": "tool_use", "id": tool_use_id, "name": name, "input": {"command": command}}]}}


def _tool_result_record(ts, tool_use_id):
    return {"type": "user", "timestamp": _iso_ts(ts), "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_use_id}]}}


def _text_record(ts, text="ok", type_="assistant"):
    return {"type": type_, "timestamp": _iso_ts(ts), "message": {"content": [{"type": "text", "text": text}]}}


def _open_scan(monkeypatch, tmp_path, screens, records, **marker_kw):
    active, pending, events, next_emitted, emitted, emit, now = _run_approval_scan(monkeypatch, tmp_path, screens)
    _worker_record(active, "sid1", "zeb4", "%7")
    log = tmp_path / "t-sid1.jsonl"
    _write_jsonl_records(log, records)
    kw = {"at": now - 30, "transcript_path": str(log)}
    kw.update(marker_kw)
    path = _marker(stale_monitor.PERMISSION_PROMPTS, "sid1", **kw)
    return active, events, next_emitted, emitted, emit, now, log, path


def _rescan(events, next_emitted, emit, later):
    events.clear()
    carried = dict(next_emitted)
    next_emitted.clear()
    stale_monitor._scan_approval_prompts("mgr-x", later, carried, next_emitted, emit)


def test_an_open_marker_pages_approval_prompt_once_with_the_command_and_no_repage(monkeypatch, tmp_path):
    active, events, next_emitted, emitted, emit, now, log, path = _open_scan(
        monkeypatch, tmp_path, {"%7": TALL_DIALOG}, [_tool_use_record(1_000_000 - 30, "toolu_1", "cp a b")])
    stale_monitor._scan_approval_prompts("mgr-x", now, emitted, next_emitted, emit)
    assert len(events) == 1 and events[0][2].startswith("APPROVAL_PROMPT zeb4 → ") and "Bash: cp a b" in events[0][2]
    key = events[0][3]
    assert key.startswith("approval:sid1:") and next_emitted[key] == {"first_seen": now, "paged": 1}
    for later in (now + 300, now + 600, now + 1200, now + 3600):
        _rescan(events, next_emitted, emit, later)
        assert events == [] and next_emitted[key]["paged"] == 1


def test_an_open_marker_pages_on_an_empty_screen(monkeypatch, tmp_path):
    active, events, next_emitted, emitted, emit, now, log, path = _open_scan(
        monkeypatch, tmp_path, {"%7": "\n" * 20},
        [{"type": "assistant", "timestamp": _iso_ts(1_000_000 - 30), "message": {"content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Write", "input": {"file_path": "/r/x.md"}}]}}],
        tool_name="Write", summary="/r/x.md")
    stale_monitor._scan_approval_prompts("mgr-x", now, emitted, next_emitted, emit)
    assert len(events) == 1 and "Write: /r/x.md" in events[0][2]


def test_the_scan_fills_tool_use_id_from_the_second_to_last_assistant_record(monkeypatch, tmp_path):
    active, events, next_emitted, emitted, emit, now, log, path = _open_scan(
        monkeypatch, tmp_path, {"%7": TALL_DIALOG},
        [_tool_use_record(1_000_000 - 30, "toolu_1", "cp a b"), _text_record(1_000_000 - 29)])
    stale_monitor._scan_approval_prompts("mgr-x", now, emitted, next_emitted, emit)
    assert json.loads(path.read_text())["tool_use_id"] == "toolu_1"
    assert len(events) == 1


def test_a_tool_result_of_the_markers_id_closes_it_and_persists_closed_at(monkeypatch, tmp_path):
    active, events, next_emitted, emitted, emit, now, log, path = _open_scan(
        monkeypatch, tmp_path, {"%7": TALL_DIALOG},
        [_tool_use_record(1_000_000 - 30, "toolu_1", "cp a b"), _tool_result_record(1_000_000 - 10, "toolu_1")],
        tool_use_id="toolu_1")
    stale_monitor._scan_approval_prompts("mgr-x", now, emitted, next_emitted, emit)
    assert events == [] and not any(k.startswith("approval:sid1:") for k in next_emitted)
    assert json.loads(path.read_text())["closed_at"] == now
    log.unlink()
    assert stale_monitor._prompt_status({"claude_sid": "sid1"}, now + 60)[1] == "closed"


def test_an_id_less_close_is_not_persisted(monkeypatch, tmp_path):
    at = 1_000_000 - 30
    active, events, next_emitted, emitted, emit, now, log, path = _open_scan(
        monkeypatch, tmp_path, {"%7": TALL_DIALOG}, [_text_record(at + 3)])
    assert stale_monitor._prompt_status({"claude_sid": "sid1"}, now)[1] == "closed"
    assert json.loads(path.read_text())["closed_at"] is None
    with open(log, "a") as f:
        f.write(json.dumps(_tool_use_record(at + 4, "toolu_1", "cp a b")) + "\n")
    stale_monitor._scan_approval_prompts("mgr-x", now, emitted, next_emitted, emit)
    assert stale_monitor._prompt_status({"claude_sid": "sid1"}, now)[1] == "open"
    assert len(events) == 1


def test_a_foreign_tool_result_leaves_the_marker_open(monkeypatch, tmp_path):
    active, events, next_emitted, emitted, emit, now, log, path = _open_scan(
        monkeypatch, tmp_path, {"%7": TALL_DIALOG},
        [_tool_use_record(1_000_000 - 30, "toolu_1", "cp a b"), _tool_result_record(1_000_000 - 10, "toolu_9")],
        tool_use_id="toolu_1")
    stale_monitor._scan_approval_prompts("mgr-x", now, emitted, next_emitted, emit)
    assert len(events) == 1


def test_a_manager_decided_marker_pages_nothing(monkeypatch, tmp_path):
    active, events, next_emitted, emitted, emit, now, log, path = _open_scan(
        monkeypatch, tmp_path, {"%7": TALL_DIALOG}, [_tool_use_record(1_000_000 - 30, "toolu_1", "cp a b")],
        decided={"by": "manager", "answer": "approve", "at": 1_000_000 - 5, "manager_sid": "m1"})
    stale_monitor._scan_approval_prompts("mgr-x", now, emitted, next_emitted, emit)
    assert events == []


def test_a_hook_decided_marker_is_closed_for_the_hint_grace_then_follows_the_transcript(monkeypatch, tmp_path):
    active, events, next_emitted, emitted, emit, now, log, path = _open_scan(
        monkeypatch, tmp_path, {"%7": TALL_DIALOG}, [_tool_use_record(1_000_000 - 5, "toolu_1", "cp a b")],
        at=1_000_000 - 5, decided={"by": "hook", "answer": "deny-hint", "at": 1_000_000 - 5})
    assert stale_monitor._prompt_status({"claude_sid": "sid1"}, now)[1] == "closed"
    old = now - stale_monitor.MARKER_HINT_GRACE_SEC - 1
    _marker(stale_monitor.PERMISSION_PROMPTS, "sid1", at=old, transcript_path=str(log),
            decided={"by": "hook", "answer": "deny-hint", "at": old})
    _write_jsonl_records(log, [_tool_use_record(old, "toolu_1", "cp a b")])
    assert stale_monitor._prompt_status({"claude_sid": "sid1"}, now)[1] == "open"
    stale_monitor._scan_approval_prompts("mgr-x", now, emitted, next_emitted, emit)
    assert len(events) == 1
    with open(log, "a") as f:
        f.write(json.dumps(_tool_result_record(old + 1, "toolu_1")) + "\n")
    assert stale_monitor._prompt_status({"claude_sid": "sid1"}, now)[1] == "closed"


def test_a_string_content_user_record_does_not_abort_the_scan(monkeypatch, tmp_path):
    active, events, next_emitted, emitted, emit, now, log, path = _open_scan(
        monkeypatch, tmp_path, {"%7": TALL_DIALOG},
        [_tool_use_record(1_000_000 - 30, "toolu_1", "cp a b"),
         {"type": "user", "timestamp": _iso_ts(1_000_000 - 20), "message": {"content": "interrupted"}}])
    assert stale_monitor._prompt_status({"claude_sid": "sid1"}, now)[1] == "open"
    stale_monitor._scan_approval_prompts("mgr-x", now, emitted, next_emitted, emit)
    assert len(events) == 1


def test_a_malformed_marker_reads_unknown(monkeypatch, tmp_path):
    active, pending, events, next_emitted, emitted, emit, now = _run_approval_scan(
        monkeypatch, tmp_path, {"%7": PROCEED_DIALOG})
    _worker_record(active, "sid1", "zeb4", "%7")
    stale_monitor.PERMISSION_PROMPTS.mkdir(parents=True)
    (stale_monitor.PERMISSION_PROMPTS / "sid1.json").write_text("{not json")
    assert stale_monitor._prompt_status({"claude_sid": "sid1"}, now) == (None, "unknown")
    stale_monitor._scan_approval_prompts("mgr-x", now, emitted, next_emitted, emit)
    assert len(events) == 1 and "Do you want to proceed?" in events[0][2]


def test_an_unreadable_transcript_pages_nothing_from_the_marker(monkeypatch, tmp_path):
    active, pending, events, next_emitted, emitted, emit, now = _run_approval_scan(
        monkeypatch, tmp_path, {"%7": TALL_DIALOG})
    _worker_record(active, "sid1", "zeb4", "%7")
    _marker(stale_monitor.PERMISSION_PROMPTS, "sid1", at=now - 30, transcript_path=str(tmp_path / "gone.jsonl"))
    assert stale_monitor._prompt_status({"claude_sid": "sid1"}, now)[1] == "unknown"
    stale_monitor._scan_approval_prompts("mgr-x", now, emitted, next_emitted, emit)
    assert events == []


def test_a_dialog_above_blank_rows_pages_without_a_marker(monkeypatch, tmp_path):
    active, pending, events, next_emitted, emitted, emit, now = _run_approval_scan(
        monkeypatch, tmp_path, {"%7": PROCEED_DIALOG + "\n" * 67})
    _worker_record(active, "sid1", "zeb4", "%7")
    stale_monitor._scan_approval_prompts("mgr-x", now, emitted, next_emitted, emit)
    assert len(events) == 1 and events[0][2].startswith("APPROVAL_PROMPT zeb4 → ")


def test_a_box_rule_without_a_marker_or_a_classic_block_pages_nothing(monkeypatch, tmp_path):
    active, pending, events, next_emitted, emitted, emit, now = _run_approval_scan(
        monkeypatch, tmp_path, {"%7": OLD_BOX})
    _worker_record(active, "sid1", "zeb4", "%7")
    stale_monitor._scan_approval_prompts("mgr-x", now, emitted, next_emitted, emit)
    assert events == []


def test_a_new_marker_at_is_a_new_page(monkeypatch, tmp_path):
    active, events, next_emitted, emitted, emit, now, log, path = _open_scan(
        monkeypatch, tmp_path, {"%7": TALL_DIALOG}, [_tool_use_record(1_000_000 - 30, "toolu_1", "cp a b")])
    stale_monitor._scan_approval_prompts("mgr-x", now, emitted, next_emitted, emit)
    first_key = events[0][3]
    _write_jsonl_records(log, [_tool_use_record(now + 100, "toolu_2", "rm x")])
    _marker(stale_monitor.PERMISSION_PROMPTS, "sid1", at=now + 100, summary="rm x", transcript_path=str(log))
    _rescan(events, next_emitted, emit, now + 130)
    assert len(events) == 1 and events[0][3] != first_key and "Bash: rm x" in events[0][2]


def test_update_marker_writes_only_while_at_is_unchanged(monkeypatch, tmp_path):
    path = _marker(tmp_path / "prompts", "sid1", at=1.0)
    assert stale_monitor._update_marker(path, 1.0, lambda m: m.update(tool_use_id="toolu_1")) is True
    assert json.loads(path.read_text())["tool_use_id"] == "toolu_1"
    assert stale_monitor._update_marker(path, 0.5, lambda m: m.update(closed_at=9.0)) is False
    assert json.loads(path.read_text())["closed_at"] is None
    assert stale_monitor._update_marker(path.with_name("gone.json"), 1.0, lambda m: None) is False


def test_dialog_sign_needs_a_rule_and_a_title_within_three_lines():
    assert stale_monitor._dialog_sign(TALL_DIALOG) is True
    assert stale_monitor._dialog_sign(OLD_BOX) is False
    assert stale_monitor._dialog_sign("Create file\n\n╌╌╌╌\n") is True
    assert stale_monitor._dialog_sign("│ Bash command (unsandboxed)\n│ ╌╌╌╌ │\n") is True
    assert stale_monitor._dialog_sign("Jump to bottom (click) ↓\n") is False
    assert stale_monitor._dialog_sign("") is False


def test_a_processing_worker_on_an_open_marker_gets_no_stale_processing_but_keeps_its_key(stale, monkeypatch, capsys):
    _processing_on_open_marker(stale, monkeypatch, TALL_DIALOG)
    stale.main()
    out = capsys.readouterr().out
    assert "STALE_PROCESSING" not in out
    assert any(k.startswith("processing:w1:") for k in json.loads(stale.EMITTED_STATE.read_text()))


def test_a_marker_with_an_unreadable_transcript_does_not_suppress_stale_processing(stale, monkeypatch, capsys):
    path = _write_record(stale, "w1", agent="worker", state="processing", name="worker-tab")
    t0 = 1_000_000
    os.utime(path, (t0, t0))
    monkeypatch.setattr(stale, "_resolve_transcript_path", lambda record, *a, **k: None)
    _marker(stale.PERMISSION_PROMPTS, "w1", at=t0, transcript_path=str(stale.ROOT / "gone.jsonl"))
    monkeypatch.setattr(stale.time, "time", lambda: t0 + 30 * 60)
    stale.main()
    assert "STALE_PROCESSING worker-tab (30min)" in capsys.readouterr().out


def test_registry_rewrites_do_not_close_an_open_marker(monkeypatch, tmp_path):
    active, events, next_emitted, emitted, emit, now, log, path = _open_scan(
        monkeypatch, tmp_path, {"%7": TALL_DIALOG}, [_tool_use_record(1_000_000 - 30, "toolu_1", "cp a b")])
    assert stale_monitor._prompt_status({"claude_sid": "sid1"}, now)[1] == "open"
    record = json.loads((active / "sid1.json").read_text())
    record.update(human_attached_at=None, tasked_at=now)
    (active / "sid1.json").write_text(json.dumps(record))
    assert stale_monitor._prompt_status(record, now + 5)[1] == "open"


LIVE_BASH_DIALOG = """\
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
 Bash command
 Tip: auto mode handles these prompts for you — choose "switch to auto mode" below
 Copy ~/.claude-b/settings.json to /tmp/ppc-e2e-b.txt
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 cp ~/.claude-b/settings.json /tmp/ppc-e2e-b.txt
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 │ Claude requested permissions to write to /home/user/.claude/settings.json, but you haven't granted it yet.
 │ /home/user/.claude-b/settings.json resolves through a symlink to /home/user/.claude/settings.json, which is
 │ outside the allowed working directories.

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and always allow access to /home/user/.claude-b from this project
   3. Yes, and switch to auto mode · auto mode handles these prompts for you
   4. No

 Esc to cancel · Tab to amend
"""

LIVE_EDIT_DIALOG = """\
⏺ Update(~/.claude-b/rules/e2e-ppc.md)

────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
 Edit file
 ../../../../../../../home/user/.claude-b/rules/e2e-ppc.md
 This will modify /home/user/.claude/rules/e2e-ppc.md (outside working directory) via a symlink

╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 1 -one line to change
 1 +one line changed
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to make this edit to e2e-ppc.md?
 ❯ 1. Yes
   2. Yes, and always allow access to /home/user/.claude-b/rules, /home/user/.claude/rules for this session
      (shift+tab)
   3. No
"""


def _tall(dialog):
    head, rule, rest = dialog.partition("╌")
    return head + rule + rest.split("\n", 1)[0] + "\n ╌╌╌╌╌╌ Jump to bottom (click) ↓ ╌╌╌╌╌╌\n"


def test_dialog_sign_accepts_a_title_anywhere_under_the_top_border():
    assert stale_monitor._dialog_sign(LIVE_BASH_DIALOG) is True
    assert stale_monitor._dialog_sign(LIVE_EDIT_DIALOG) is True
    wrapped = LIVE_BASH_DIALOG.replace(" Copy ~/.claude-b/settings.json to /tmp/ppc-e2e-b.txt\n",
                                       " Copy ~/.claude-b/settings.json to /tmp/ppc-e2e-b.txt, a long\n"
                                       " description that wrapped onto a second line\n")
    assert stale_monitor._dialog_sign(_tall(wrapped)) is True
    assert stale_monitor._dialog_sign(_tall(LIVE_EDIT_DIALOG)) is True
    no_title = LIVE_BASH_DIALOG.replace(" Bash command\n", " Ran 1 shell command\n")
    assert stale_monitor._dialog_sign(_tall(no_title)) is False
    assert stale_monitor._dialog_sign("│ Bash command\n────────\n" + ("│ more text\n" * 50) + "│ ╌╌╌╌ │\n") is False


def test_the_hook_page_marks_a_cut_command_with_an_ellipsis(monkeypatch, tmp_path):
    long_cmd = "cd /x/" + "y" * 200 + " && rm -rf ~/data"
    active, events, next_emitted, emitted, emit, now, log, path = _open_scan(
        monkeypatch, tmp_path, {"%7": TALL_DIALOG}, [_tool_use_record(1_000_000 - 30, "toolu_1", long_cmd)],
        summary=long_cmd)
    stale_monitor._scan_approval_prompts("mgr-x", now, emitted, next_emitted, emit)
    tail = events[0][2].split(" — ", 1)[1]
    assert tail.endswith("…") and len(tail) <= stale_monitor.APPROVAL_EXCERPT_MAX
    short = "cp a b"
    _write_jsonl_records(log, [_tool_use_record(now + 100, "toolu_2", short)])
    _marker(stale_monitor.PERMISSION_PROMPTS, "sid1", at=now + 100, summary=short, transcript_path=str(log))
    _rescan(events, next_emitted, emit, now + 130)
    assert events[0][2].endswith(" — Bash: cp a b")


def _processing_on_open_marker(stale, monkeypatch, screen):
    t0 = 1_000_000
    path = _write_record(stale, "w1", agent="worker", state="processing", name="worker-tab", window_id="%7")
    os.utime(path, (t0, t0))
    log = stale.ROOT / "t.jsonl"
    _write_jsonl_records(log, [_tool_use_record(t0, "toolu_1", "cp a b")])
    monkeypatch.setattr(stale, "_resolve_transcript_path", lambda record, *a, **k: log)
    _marker(stale.PERMISSION_PROMPTS, "w1", at=t0, transcript_path=str(log))
    monkeypatch.setattr(stale, "_get_driver", lambda: FakeDriver({"%7": screen}))
    monkeypatch.setattr(stale.time, "time", lambda: t0 + 30 * 60)


def test_an_open_marker_holds_stale_processing_only_while_the_pane_shows_a_dialog(stale, monkeypatch, capsys):
    _processing_on_open_marker(stale, monkeypatch, "⏺ Bash(cp a b)\n✻ Running… (29m 10s · esc to interrupt)\n")
    stale.main()
    assert "STALE_PROCESSING worker-tab (30min)" in capsys.readouterr().out


def test_an_open_marker_with_the_dialog_on_the_pane_holds_stale_processing(stale, monkeypatch, capsys):
    _processing_on_open_marker(stale, monkeypatch, TALL_DIALOG)
    stale.main()
    assert "STALE_PROCESSING" not in capsys.readouterr().out


def test_an_open_marker_with_a_failed_capture_does_not_hold_stale_processing(stale, monkeypatch, capsys):
    _processing_on_open_marker(stale, monkeypatch, RuntimeError("tmux down"))
    stale.main()
    assert "STALE_PROCESSING worker-tab (30min)" in capsys.readouterr().out


@pytest.mark.parametrize("name, tool_input", [
    ("Bash", {"command": "cp a b"}),
    ("Write", {"file_path": "/r/x.md", "content": "c"}),
    ("WebFetch", {"url": "https://x.test/", "prompt": "p"}),
    ("Grep", {"pattern": "x", "path": "/r/.claude-b/projects/p"}),
    ("mcp__github__create_issue", {"title": "t", "body": "b"}),
])
def test_the_lane_matches_every_tool_shape_the_hook_summarises(monkeypatch, tmp_path, name, tool_input):
    summary = stale_monitor.permission_summary(tool_input)
    at = 1_000_000 - 30
    record = {"type": "assistant", "timestamp": _iso_ts(at), "message": {"content": [
        {"type": "tool_use", "id": "toolu_A", "name": name, "input": tool_input}]}}
    active, events, next_emitted, emitted, emit, now, log, path = _open_scan(
        monkeypatch, tmp_path, {"%7": "\n" * 20}, [record, _tool_result_record(at + 8, "toolu_sibling")],
        tool_name=name, summary=summary)
    marker, status = stale_monitor._prompt_status({"claude_sid": "sid1"}, now)
    assert (marker["tool_use_id"], status) == ("toolu_A", "open")


def test_dialog_sign_reads_a_tall_dialog_on_a_tall_pane():
    head = "────────────\n Bash command\n Copy a file\n╌╌╌╌╌╌╌╌╌╌╌╌\n"
    body = "".join(f" line {i} of the command\n" for i in range(52))
    assert stale_monitor._dialog_sign(head + body + "╌╌╌ Jump to bottom (click) ↓ ╌╌╌\n") is True
    assert stale_monitor._dialog_sign(head + body) is True


def test_an_interactive_child_on_the_safeguard_dialog_is_cleared_like_a_worker(nudgy, monkeypatch, capsys):
    _parent_worker_record(nudgy, "w-alpha", "alpha", "boss")
    pane = _paused_worker(nudgy, monkeypatch, kind="subworker", parent_name="alpha", parent_sid="w-alpha",
                          depth=2, provider="claude-tui", runtime="claude")
    nudgy.main()
    out = capsys.readouterr().out
    assert pane.calls == [("key", "%227", "2"), ("text", "%227", "continue")]
    assert 'SAFEGUARD_PAUSE fable-design (child of alpha) → get_child_tail("fable-design")' in out


def test_a_codex_child_on_a_paused_transcript_is_left_alone(nudgy, monkeypatch, capsys):
    _parent_worker_record(nudgy, "w-alpha", "alpha", "boss")
    pane = _paused_worker(nudgy, monkeypatch, kind="subworker", parent_name="alpha", parent_sid="w-alpha",
                          depth=2, provider="codex-exec", runtime="codex")
    nudgy.main()
    assert pane.calls == []
    assert "SAFEGUARD_PAUSE" not in capsys.readouterr().out


HOLD_BANNER_TEXT = ("Your account is on hold and can't use Claude Code. "
                    "View details or appeal: https://claude.ai/restricted")


def _iso_at(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _hold_worker(stale, sid="w1", name="worker-tab", age_sec=360, banner_offset=10, **overrides):
    now = int(time.time())
    record = {"agent": "worker", "state": "processing", "name": name, "window_id": "42",
              "account": "a", "started_at": now - age_sec}
    record.update(overrides)
    path = _write_record(stale, sid, **record)
    log = _write_banner(stale, sid, banner_text=HOLD_BANNER_TEXT,
                        timestamp=_iso_at(record["started_at"] + banner_offset))
    os.utime(path, (now - age_sec, now - age_sec))
    os.utime(log, (now - age_sec, now - age_sec))
    return path, log


def _held_state(stale, **held):
    state = json.loads(stale.ACCOUNT_STATE.read_text()) if stale.ACCOUNT_STATE.exists() else {"accounts": {}}
    state["held"] = held
    stale.ACCOUNT_STATE.write_text(json.dumps(state))


def _held(stale):
    if not stale.ACCOUNT_STATE.exists():
        return {}
    return json.loads(stale.ACCOUNT_STATE.read_text()).get("held", {})


def test_a_hold_banner_in_the_transcript_marks_the_account_held_without_a_reset_and_flips_at_once(
        stale, capsys, monkeypatch):
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    now = int(time.time())
    stale.ACCOUNT_STATE.write_text(json.dumps({"accounts": {}, "last_flip": {"ts": now - 600, "from": "b", "to": "a"}}))
    _hold_worker(stale)
    stale.main()
    state = json.loads(stale.ACCOUNT_STATE.read_text())
    assert state["held"]["a"]["reason"] == "banner" and state["held"]["a"]["source"] == "worker:worker-tab"
    assert "reset_ts" not in state["held"]["a"]
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"
    out = capsys.readouterr().out
    assert "SWITCHED account a→b (account on hold)" in out
    assert "ACCOUNT_HOLD a since" in out
    assert json.loads((stale.CLOSED / "w1.json").read_text())["closed_reason"] == "account-hold"
    assert [e["event"] for e in _ledger_events(stale)] == ["hold", "flip"]


def test_a_quoted_hold_text_in_a_normal_reply_holds_nothing(stale, capsys, monkeypatch):
    _capture_runs(stale, monkeypatch)
    _arm_pool(stale, "a")
    now = int(time.time())
    path = _write_record(stale, "w1", agent="worker", state="processing", name="worker-tab",
                         window_id="42", account="a")
    log = _write_transcript(stale, "w1", HOLD_BANNER_TEXT)
    os.utime(path, (now - 360, now - 360))
    os.utime(log, (now - 360, now - 360))
    stale.main()
    assert _held(stale) == {}
    assert "ACCOUNT_HOLD" not in capsys.readouterr().out


def test_a_hold_banner_older_than_the_launch_holds_nothing(stale, capsys, monkeypatch):
    _capture_runs(stale, monkeypatch)
    _arm_pool(stale, "a")
    _hold_worker(stale, banner_offset=-60)
    stale.main()
    assert _held(stale) == {}
    assert (stale.ACTIVE / "w1.json").exists()
    assert "ACCOUNT_HOLD" not in capsys.readouterr().out


def test_a_held_account_never_expires(stale):
    now = 1_000_000
    assert stale._record_hold("b", "banner", "worker:x", now) is True
    state = json.loads(stale.ACCOUNT_STATE.read_text())
    assert stale._entry_bricked(state["accounts"].get("b"), now + 7 * 86400, held="b" in state["held"])
    assert not stale._entry_bricked(state["accounts"].get("b"), now + 7 * 86400)


def test_a_record_brick_on_a_held_account_keeps_the_hold(stale):
    now = 1_000_000
    stale._record_hold("b", "operator", "cli", now)
    stale._record_brick("b", now + 3600, "worker:x", now + 10)
    assert json.loads(stale.ACCOUNT_STATE.read_text())["held"]["b"]["at"] == now


def test_a_second_hold_mark_is_kept_unless_a_banner_upgrades_an_operator_mark(stale):
    now = 1_000_000
    assert stale._record_hold("b", "appeal pending", "cli", now) is True
    assert stale._record_hold("b", "appeal pending", "cli", now + 5) is False
    assert stale._record_hold("b", "banner", "worker:x", now + 10) is True
    mark = json.loads(stale.ACCOUNT_STATE.read_text())["held"]["b"]
    assert mark == {"at": now, "reason": "banner", "source": "worker:x", "banner_at": now + 10}
    assert stale._record_hold("b", "operator", "cli", now + 20) is False
    assert json.loads(stale.ACCOUNT_STATE.read_text())["held"]["b"] == mark
    assert [e["event"] for e in _ledger_events(stale)] == ["hold", "hold"]


def test_every_stamped_session_on_a_banner_held_account_is_closed_whatever_the_pointer(stale, capsys, monkeypatch):
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    stale._record_hold("b", "banner", "worker:x", int(time.time()))
    _parent_worker_record(stale, "w-b", "beta", "boss", account="b")
    _child_record(stale, "c-b", "beta", runtime="claude", provider="claude-tui", account="b")
    _parent_worker_record(stale, "w-a", "alpha", "boss", account="a")
    _child_record(stale, "c-x", "alpha", account=None)
    stale.main()
    assert (stale.CLOSED / "w-b.json").exists() and (stale.CLOSED / "c-b.json").exists()
    assert json.loads((stale.CLOSED / "c-b.json").read_text())["closed_reason"] == "account-hold"
    assert (stale.ACTIVE / "w-a.json").exists() and (stale.ACTIVE / "c-x.json").exists()
    out = capsys.readouterr().out
    assert "SWITCHED" not in out and "ACCOUNT_HOLD b since" in out
    assert stale.ACCOUNT_ACTIVE.read_text() == "a\n"


def test_an_unstamped_session_on_the_pointer_is_not_closed_by_a_hold_on_the_pointer(stale, capsys, monkeypatch):
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: False)
    _arm_pool(stale, "a")
    stale._record_hold("a", "banner", "worker:x", int(time.time()))
    _write_record(stale, "w1", agent="worker", runtime="codex", name="cx", window_id="7", account=None)
    stale.main()
    stale.main()
    assert (stale.ACTIVE / "w1.json").exists()
    assert stale.ACCOUNT_ACTIVE.read_text() == "a\n"


def test_the_hold_page_fires_once_per_manager_and_again_after_release(stale, capsys, monkeypatch):
    _capture_runs(stale, monkeypatch)
    _arm_pool(stale, "a")
    now = int(time.time())
    stale._record_hold("b", "banner", "worker:x", now)
    stale.main()
    stale.main()
    assert capsys.readouterr().out.count("ACCOUNT_HOLD b since") == 1
    state = json.loads(stale.ACCOUNT_STATE.read_text())
    del state["held"]["b"]
    stale.ACCOUNT_STATE.write_text(json.dumps(state))
    stale.main()
    assert "ACCOUNT_HOLD" not in capsys.readouterr().out
    stale._record_hold("b", "banner", "worker:x", now + 100)
    stale.main()
    assert capsys.readouterr().out.count("ACCOUNT_HOLD b since") == 1


def _hold_manager(stale, sid, manager_name, t0, **overrides):
    path = _write_record(stale, sid, agent="manager", state="processing", name=manager_name,
                         window_id="9", parent_manager_name=None, started_at=t0 - 60, **overrides)
    os.utime(path, (t0, t0))
    log = _write_banner(stale, sid, banner_text=HOLD_BANNER_TEXT, timestamp=_iso_at(t0 - 30))
    os.utime(log, (t0, t0))
    return path, log


def test_a_held_manager_gets_a_recovery_tab_whatever_the_pointer(stale, capsys, monkeypatch, tmp_path):
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _seed_farm(monkeypatch, tmp_path, "b")
    _arm_pool(stale, "a")
    t0 = 1_000_000
    _hold_manager(stale, "mgr1", "mgr-A", t0)
    clock = {"now": t0 + 30 * 60}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    t_flip = clock["now"]
    stale.main(manager_name="mgr-A")
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"
    held = json.loads(stale.ACCOUNT_STATE.read_text())["held"]["a"]
    assert held["reason"] == "banner" and held["source"] == "manager:mgr-A"
    assert [f["reason"] for f in _flips(stale)] == ["manager mgr-A on hold"]
    launches = _launch_calls(calls)
    assert len(launches) == 1
    assert "/manager-takeover-recovery mgr1" in launches[0][0][-1]
    assert "CLAUDE_ORCH_ACCOUNT=b" in launches[0][0][-1]
    assert capsys.readouterr().out == "", "buffered while the manager is on hold"
    assert stale._limited_flag_path("mgr-A").exists()
    emitted = json.loads(stale._emitted_state_path("mgr-A").read_text())
    assert emitted["recovery:mgr1"] == {"at": t_flip, "relaunched": False}
    clock["now"] = t_flip + stale.TAKEOVER_GUARD_SEC + 120
    stale.main(manager_name="mgr-A")
    assert len(_launch_calls(calls)) == 2
    assert [e["event"] for e in _ledger_events(stale) if e["event"].startswith("recovery")] == [
        "recovery-launch", "recovery-relaunch"]


def test_a_banner_after_an_operator_hold_upgrades_the_mark_and_recovers_the_manager(
        stale, capsys, monkeypatch, tmp_path):
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _seed_farm(monkeypatch, tmp_path, "b")
    _arm_pool(stale, "a")
    t0 = 1_000_000
    _held_state(stale, a={"at": t0, "reason": "appeal pending", "source": "cli"})
    _write_record(stale, "mgr1", agent="manager", state="idle", name="mgr-A", window_id="9",
                  parent_manager_name=None)
    clock = {"now": t0 + 600}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    out = capsys.readouterr().out
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"
    assert out.count("ACCOUNT_HOLD a since") == 1 and "appeal pending" in out
    assert _launch_calls(calls) == []
    t1 = clock["now"]
    _hold_manager(stale, "mgr1", "mgr-A", t1)
    clock["now"] = t1 + 600
    stale.main(manager_name="mgr-A")
    held = json.loads(stale.ACCOUNT_STATE.read_text())["held"]["a"]
    assert held["at"] == t0 and held["reason"] == "banner" and held["banner_at"] == clock["now"]
    assert len(_launch_calls(calls)) == 1
    assert capsys.readouterr().out == ""
    emitted = json.loads(stale._emitted_state_path("mgr-A").read_text())
    assert "account-hold:a:banner" in emitted and "account-hold:a:appeal pending" not in emitted


def test_a_cli_hold_is_applied_by_the_next_scan(stale, capsys, monkeypatch):
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _arm_pool(stale, "a")
    _held_state(stale, a={"at": int(time.time()), "reason": "operator", "source": "cli"})
    _write_record(stale, "w1", agent="worker", name="alpha", window_id="7", account="a")
    stale.main()
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"
    assert (stale.ACTIVE / "w1.json").exists()
    out = capsys.readouterr().out
    assert "ACCOUNT_HOLD a since" in out and "appeal" not in out and "running sessions finish on it" in out
    assert "SWITCHED account a→b (account on hold)" in out


def test_a_hold_with_no_registry_marks_nothing(stale, capsys, monkeypatch):
    _capture_runs(stale, monkeypatch)
    t0 = 1_000_000
    _hold_manager(stale, "mgr1", "mgr-A", t0)
    monkeypatch.setattr(stale.time, "time", lambda: t0 + 600)
    stale.main(manager_name="mgr-A")
    assert _held(stale) == {}
    assert "ACCOUNT_HOLD" not in capsys.readouterr().out


def test_an_unstamped_worker_on_the_hold_banner_holds_the_default_account_not_the_pointer(
        stale, capsys, monkeypatch):
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    stale.ACCOUNT_REGISTRY.write_text(json.dumps({"pool": [{"name": "a"}, {"name": "b"}], "default": "a"}))
    _arm_pool(stale, "b")
    _hold_worker(stale, account=None)
    stale.main()
    assert list(_held(stale)) == ["a"]
    assert json.loads((stale.CLOSED / "w1.json").read_text())["closed_reason"] == "account-hold"
    assert stale.ACCOUNT_ACTIVE.read_text() == "b\n"


def test_an_idle_manager_on_a_held_account_is_neither_recovered_nor_muted(stale, capsys, monkeypatch, tmp_path):
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _seed_farm(monkeypatch, tmp_path, "b")
    _arm_pool(stale, "a")
    _write_record(stale, "mgr1", agent="manager", state="idle", name="mgr-A", window_id="9",
                  parent_manager_name=None, account="a")
    _hold_worker(stale, parent_manager_name="mgr-A")
    stale.main(manager_name="mgr-A")
    out = capsys.readouterr().out
    assert "ACCOUNT_HOLD a since" in out
    assert _launch_calls(calls) == []
    assert not stale._limited_flag_path("mgr-A").exists()
    assert "recovery:mgr1" not in json.loads(stale._emitted_state_path("mgr-A").read_text())
    assert (stale.ACTIVE / "mgr1.json").exists()


def _fable_worker(stale, sid, name, minutes=6, banner_age_sec=60, gone=False, **overrides):
    now = int(time.time())
    record = {"name": name, "state": "processing", "window_id": "%9", "account": "a", "pid": 4242,
              "started_at": now - minutes * 60, "parent_manager_name": None}
    record.update(overrides)
    path = _write_record(stale, sid, **record)
    extra = (_assistant_line("back to work"),) if gone else ()
    log = _write_banner(stale, sid, *extra, timestamp=_iso_at(now - banner_age_sec))
    old = now - minutes * 60
    os.utime(path, (old, old))
    os.utime(log, (old, old))
    return path, log


def _pid_mock(monkeypatch, stale, alive=()):
    polls = []

    def kill(pid, sig):
        polls.append((pid, (stale.ACTIVE / "w1.json").exists()))
        if alive == "all" or pid in alive:
            return None
        raise ProcessLookupError(pid)

    monkeypatch.setattr(stale.os, "kill", kill)
    return polls


def _auto_impl_mock(monkeypatch, result=None, raises=None):
    import dockwright.mcp_server as srv
    calls = []

    async def fake(worker, text, *args, marker="[MANAGER] ", **kwargs):
        calls.append((worker, text, marker))
        if raises is not None:
            raise raises
        return dict(result)

    monkeypatch.setattr(srv, "send_manager_to_worker_auto_impl", fake)
    return calls


DELIVERED = {"status": "delivered", "worker": "alpha", "confirmed": True, "resumed": True, "sid": "w1"}


def _move_setup(nudgy, monkeypatch, result=DELIVERED, raises=None, alive=(), elsewhere=True, wait=0):
    calls = _capture_runs(nudgy, monkeypatch)
    _arm_pool(nudgy, "a")
    if elsewhere is not NotImplemented:
        monkeypatch.setattr(nudgy, "_fable_elsewhere", lambda account, now: elsewhere)
    polls = _pid_mock(monkeypatch, nudgy, alive)
    monkeypatch.setattr(nudgy, "FABLE_MOVE_PID_WAIT_SEC", wait)
    mock_calls = _auto_impl_mock(monkeypatch, result, raises)
    return calls, polls, mock_calls


def _moves(stale):
    state = json.loads(stale.EMITTED_STATE.read_text()) if stale.EMITTED_STATE.exists() else {}
    return {k: v for k, v in state.items() if k.startswith("fable-move:")}


def test_a_fable_banner_moves_the_worker_when_another_account_has_fable(nudgy, monkeypatch, capsys):
    calls, polls, mock_calls = _move_setup(nudgy, monkeypatch)
    path, _log = _fable_worker(nudgy, "w1", "alpha")
    record = json.loads(path.read_text())
    nudgy.main()
    key = nudgy._fable_move_key(record, path)
    emitted = json.loads(nudgy.EMITTED_STATE.read_text())
    assert emitted[key]["stage"] == "done"
    assert ("close", "%9") in calls
    assert json.loads((nudgy.CLOSED / "w1.json").read_text())["closed_reason"] == "fable-moved"
    assert mock_calls == [("alpha", nudgy.NUDGE_TEXT, "")]
    assert "FABLE_LIMIT" not in capsys.readouterr().out
    assert _send_text_calls(calls) == []
    assert [e["event"] for e in _ledger_events(nudgy) if e["event"] != "model-spent"] == ["fable-moved"]


def test_a_move_resumes_only_after_the_old_process_exits(nudgy, monkeypatch):
    seen = {"n": 0}

    def kill(pid, sig):
        seen["n"] += 1
        seen.setdefault("active", []).append((nudgy.ACTIVE / "w1.json").exists())
        seen.setdefault("called", []).append(len(mock_calls))
        if seen["n"] <= 2:
            return None
        raise ProcessLookupError(pid)

    calls, _polls, mock_calls = _move_setup(nudgy, monkeypatch, wait=5)
    monkeypatch.setattr(nudgy.os, "kill", kill)
    _fable_worker(nudgy, "w1", "alpha")
    nudgy.main()
    assert seen["active"][:3] == [True, True, True] and seen["called"][:3] == [0, 0, 0]
    assert seen["n"] >= 3 and all(c == 1 for c in seen["called"][3:])
    assert len(mock_calls) == 1
    assert list(_moves(nudgy).values())[0]["stage"] == "done"
    assert not (nudgy.ACTIVE / "w1.json").exists()


def test_a_resume_that_raises_is_retried_and_paged_on_the_third_tick(nudgy, monkeypatch, capsys):
    calls, _polls, mock_calls = _move_setup(nudgy, monkeypatch, raises=RuntimeError("no tab"))
    _fable_worker(nudgy, "w1", "alpha")
    stages = []
    for _ in range(3):
        nudgy.main()
        move = list(_moves(nudgy).values())[0]
        stages.append((move["stage"], move["attempts"]))
    assert stages == [("resuming", 1), ("resuming", 2), ("paged", 3)]
    out = capsys.readouterr().out
    assert out.count('FABLE_LIMIT alpha → resume_worker("alpha")') == 1
    assert "last refusal: no tab" in out and "refused 3 times" in out
    assert len(mock_calls) == 3
    assert (nudgy.CLOSED / "w1.json").exists()
    assert list(_moves(nudgy).values())[0]["variant"] == "resume-refused"


def test_a_close_that_leaves_the_process_alive_is_paged_at_once(nudgy, monkeypatch, capsys):
    calls, _polls, mock_calls = _move_setup(nudgy, monkeypatch, alive="all")
    _fable_worker(nudgy, "w1", "alpha")
    nudgy.main()
    move = list(_moves(nudgy).values())[0]
    assert move["stage"] == "paged" and move["variant"] == "close-refused"
    assert calls.count(("close", "%9")) == 1
    out = capsys.readouterr().out
    assert out.count("FABLE_LIMIT alpha") == 1 and 'kill_worker("alpha")' in out and "pid 4242 still runs" in out
    assert mock_calls == [] and (nudgy.ACTIVE / "w1.json").exists()
    nudgy.main()
    assert calls.count(("close", "%9")) == 1
    assert "FABLE_LIMIT" not in capsys.readouterr().out
    assert list(_moves(nudgy).values())[0]["stage"] == "paged"


def test_a_paged_move_pages_again_after_the_page_key_is_lost(nudgy, monkeypatch, capsys):
    _move_setup(nudgy, monkeypatch)
    path, _log = _fable_worker(nudgy, "w1", "alpha")
    started = json.loads(path.read_text())["started_at"]
    now = int(time.time())
    nudgy.EMITTED_STATE.write_text(json.dumps({f"fable-move:w1:{int(started)}": {
        "at": now - 60, "sid": "w1", "name": "alpha", "pid": 4242, "window_id": "%9", "started_at": started,
        "stage": "paged", "variant": "resume-refused", "reason": "no tab", "attempts": 3}}))
    nudgy.main()
    assert capsys.readouterr().out.count("FABLE_LIMIT alpha") == 1
    nudgy.main()
    assert "FABLE_LIMIT" not in capsys.readouterr().out
    state = json.loads(nudgy.EMITTED_STATE.read_text())
    del state[f"fable-limit-page:w1:{int(started)}"]
    nudgy.EMITTED_STATE.write_text(json.dumps(state))
    nudgy.main()
    assert capsys.readouterr().out.count("FABLE_LIMIT alpha") == 1


def test_a_replaced_node_counts_as_moved(nudgy, monkeypatch, capsys):
    _calls, _polls, mock_calls = _move_setup(
        nudgy, monkeypatch, result={"status": "replaced", "worker": "alpha", "replaced": True, "sid": "w1b"})
    _fable_worker(nudgy, "w1", "alpha")
    nudgy.main()
    assert list(_moves(nudgy).values())[0]["stage"] == "done"
    assert len(mock_calls) == 1
    assert "FABLE_LIMIT" not in capsys.readouterr().out


def test_a_human_attached_delivery_pages_once_and_never_resends(nudgy, monkeypatch, capsys):
    _calls, _polls, mock_calls = _move_setup(
        nudgy, monkeypatch,
        result={"status": "human_attached", "delivered": False, "resumed": True, "sid": "w1", "worker": "alpha"})
    _fable_worker(nudgy, "w1", "alpha")
    nudgy.main()
    move = list(_moves(nudgy).values())[0]
    assert move["stage"] == "paged" and move["variant"] == "human"
    out = capsys.readouterr().out
    assert out.count("FABLE_LIMIT alpha") == 1 and 'send_manager_to_worker("alpha", "resume your task")' in out
    nudgy.main()
    assert len(mock_calls) == 1
    assert "FABLE_LIMIT" not in capsys.readouterr().out


def test_an_unstamped_worker_on_the_fable_banner_switches_to_opus_in_place(nudgy, monkeypatch, capsys):
    calls, _polls, mock_calls = _move_setup(nudgy, monkeypatch, elsewhere=NotImplemented)
    _fable_worker(nudgy, "w1", "alpha", account=None)
    nudgy.main()
    assert [t for _, t in _send_text_calls(calls)][:2] == ["/model claude-opus-5-5", "/effort xhigh"]
    assert ("close", "%9") not in calls and _moves(nudgy) == {} and mock_calls == []
    assert (nudgy.ACTIVE / "w1.json").exists()


def test_a_stage_survives_a_stopped_tick(nudgy, monkeypatch):
    import dockwright.mcp_server as srv
    _move_setup(nudgy, monkeypatch)
    path, _log = _fable_worker(nudgy, "w1", "alpha")
    key = nudgy._fable_move_key(json.loads(path.read_text()), path)
    seen = []

    async def fake(worker, text, *args, **kwargs):
        seen.append(json.loads(nudgy.EMITTED_STATE.read_text())[key]["stage"])
        return dict(DELIVERED)

    monkeypatch.setattr(srv, "send_manager_to_worker_auto_impl", fake)
    nudgy.main()
    assert seen == ["resuming"]
    assert json.loads(nudgy.EMITTED_STATE.read_text())[key]["stage"] == "done"


def _guard_rows():
    rows = []
    for stage in ("closing", "resuming", "paged:resume-refused", "paged:human", "done"):
        paged_stage = stage.startswith("paged")
        for incarnation in ("same", "new"):
            if incarnation == "same":
                if stage == "closing":
                    expect = dict(restarted=False, reclosed=True, paged=True, nudged=False)
                elif stage == "resuming":
                    expect = dict(restarted=False, reclosed=False, paged=False, nudged=True)
                elif paged_stage:
                    expect = dict(restarted=False, reclosed=False, paged=True, nudged=False)
                else:
                    expect = dict(restarted=True, reclosed=True, paged=False, nudged=True)
            else:
                expect = dict(restarted=True, reclosed=True, paged=paged_stage, nudged=True)
            rows.append((stage, incarnation, "last", expect))
            rows.append((stage, incarnation, "gone",
                         dict(restarted=False, reclosed=False, paged=paged_stage, nudged=False)))
    return rows


MOVE_GUARD_TABLE = _guard_rows()


@pytest.mark.parametrize("stage,incarnation,banner,expect", MOVE_GUARD_TABLE,
                         ids=[f"{s}-{i}-{b}" for s, i, b, _ in MOVE_GUARD_TABLE])
def test_the_move_guard_table(nudgy, monkeypatch, capsys, stage, incarnation, banner, expect):
    calls, _polls, mock_calls = _move_setup(nudgy, monkeypatch, alive=(4141,))
    path, _log = _fable_worker(nudgy, "w1", "alpha", gone=(banner == "gone"))
    started = int(json.loads(path.read_text())["started_at"])
    key_started = started if incarnation == "same" else started - 600
    seeded = {"at": started + 30 if incarnation == "same" else started - 30, "sid": "w1", "name": "alpha",
              "pid": 4141, "window_id": "%9", "started_at": key_started, "attempts": 0}
    if stage.startswith("paged"):
        seeded.update(stage="paged", variant=stage.split(":")[1], reason="no tab", attempts=3)
    else:
        seeded["stage"] = stage
    nudgy.EMITTED_STATE.write_text(json.dumps({f"fable-move:w1:{key_started}": seeded}))
    nudgy.main()
    state = json.loads(nudgy.EMITTED_STATE.read_text())
    this = state.get(f"fable-move:w1:{started}")
    observed = dict(
        restarted=this is not None and this["at"] >= started + 300,
        reclosed=("close", "%9") in calls,
        paged="FABLE_LIMIT" in capsys.readouterr().out,
        nudged=len(mock_calls) > 0,
    )
    assert observed == expect


def test_a_fable_banner_with_no_fable_left_switches_to_opus_in_place(nudgy, monkeypatch, capsys):
    calls, _polls, mock_calls = _move_setup(nudgy, monkeypatch, elsewhere=False)
    _fable_worker(nudgy, "w1", "alpha")
    nudgy.main()
    assert [t for _, t in _send_text_calls(calls)][:2] == ["/model claude-opus-5-5", "/effort xhigh"]
    assert (nudgy.ACTIVE / "w1.json").exists() and _moves(nudgy) == {} and mock_calls == []
    assert "FABLE_LIMIT" not in capsys.readouterr().out


def test_a_fable_banner_older_than_the_launch_moves_nothing(nudgy, monkeypatch, capsys):
    calls, _polls, mock_calls = _move_setup(nudgy, monkeypatch)
    _fable_worker(nudgy, "w1", "alpha", minutes=3, banner_age_sec=4 * 60)
    nudgy.main()
    out = capsys.readouterr().out
    assert ("close", "%9") not in calls and _send_text_calls(calls) == [] and mock_calls == []
    assert _moves(nudgy) == {} and "FABLE_LIMIT" not in out and "MODEL_FALLBACK" not in out
    assert (nudgy.ACTIVE / "w1.json").exists()


def test_an_interactive_child_on_the_fable_banner_is_closed_with_fable_spent_and_not_typed(
        nudgy, monkeypatch, capsys):
    calls, _polls, mock_calls = _move_setup(nudgy, monkeypatch)
    _parent_worker_record(nudgy, "w-alpha", "alpha", "boss")
    _fable_worker(nudgy, "c1", "alpha-review", kind="subworker", runtime="claude", provider="claude-tui",
                  parent_name="alpha", parent_sid="w-alpha", depth=2)
    nudgy.main()
    assert json.loads((nudgy.CLOSED / "c1.json").read_text())["closed_reason"] == "fable-spent"
    events = [e for e in _ledger_events(nudgy) if e["event"] != "model-spent"]
    assert [e["event"] for e in events] == ["fable-spent-child"]
    assert events[0]["worker"] == "alpha-review" and events[0]["parent"] == "alpha"
    assert _send_text_calls(calls) == [] and mock_calls == [] and _moves(nudgy) == {}
    assert "FABLE_LIMIT" not in capsys.readouterr().out


def test_a_codex_child_on_the_fable_banner_is_left_alone(nudgy, monkeypatch, capsys):
    calls, _polls, mock_calls = _move_setup(nudgy, monkeypatch)
    _parent_worker_record(nudgy, "w-alpha", "alpha", "boss")
    _fable_worker(nudgy, "c1", "alpha-review", kind="subworker", runtime="codex", provider="codex-exec",
                  parent_name="alpha", parent_sid="w-alpha", depth=2)
    nudgy.main()
    assert (nudgy.ACTIVE / "c1.json").exists() and calls == [] and mock_calls == []


def test_a_non_fable_credit_banner_keeps_the_sonnet_rescue(nudgy, monkeypatch, capsys):
    calls, _polls, mock_calls = _move_setup(nudgy, monkeypatch)
    now = int(time.time())
    path = _write_record(nudgy, "w1", name="alpha", state="processing", window_id="%9", account="a",
                         parent_manager_name=None, started_at=now - 360)
    log = _write_banner(nudgy, "w1", banner_text="You've reached your Opus limit. Run /usage-credits to continue "
                                                   "or switch models with /model.", timestamp=_iso_at(now - 60))
    os.utime(path, (now - 360, now - 360))
    os.utime(log, (now - 360, now - 360))
    nudgy.main()
    assert [t for _, t in _send_text_calls(calls)][:1] == ["/model claude-sonnet-5"]
    assert _moves(nudgy) == {} and mock_calls == []


def test_the_standalone_lane_without_spawner_pages_instead_of_moving(nudgy, monkeypatch, capsys):
    calls, _polls, mock_calls = _move_setup(nudgy, monkeypatch, elsewhere=None)
    _fable_worker(nudgy, "w1", "alpha")
    nudgy.main()
    out = capsys.readouterr().out
    assert out.count("FABLE_LIMIT alpha") == 1
    assert "standalone lane cannot move a session" in out
    assert "switch it to claude-opus-5-5 at effort xhigh" in out and "sonnet" not in out
    assert calls == [] and mock_calls == [] and (nudgy.ACTIVE / "w1.json").exists()
    nudgy.main()
    assert "FABLE_LIMIT" not in capsys.readouterr().out


@pytest.fixture
def real_resume(nudgy, tmp_path, monkeypatch):
    for attr, sub in (("ACTIVE", "active"), ("CLOSED", "closed"), ("ASSIGNMENTS", "assignments"),
                      ("ASSIGNMENTS_PENDING", "assignments/.pending"), ("PANE_SENDS", "pane-sends"),
                      ("SPEND_LEDGER", "spend-ledger.jsonl"), ("QUESTIONS", "questions"), ("ANSWERS", "answers"),
                      ("DONE", "done"), ("HANDOFFS", "handoffs"), ("SLOTS", "slots"), ("ARTIFACTS", "artifacts"),
                      ("PRESETS", "presets"), ("TURN_ENDS", "turn-ends"), ("ARCHITECT", "architect"),
                      ("PERMISSION_PROMPTS", "permission-prompts"),
                      ("MANAGER_TRIGGERS_LOG", "manager-triggers.jsonl"), ("MANAGER_MEMORY", "manager-memory")):
        monkeypatch.setattr(paths, attr, tmp_path / sub)
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    paths.ensure_dirs()
    monkeypatch.setattr(nudgy, "CLAUDE_PROJECTS", tmp_path / ".claude" / "projects")
    _capture_runs(nudgy, monkeypatch)
    _arm_pool(nudgy, "a")
    return nudgy, tmp_path


def test_the_move_drives_the_real_auto_resume_once(real_resume, monkeypatch, capsys):
    stale, tmp_path = real_resume
    import dockwright.mcp_server as srv
    _make_transcript(tmp_path, monkeypatch, "w1")
    _write_closed("alpha", "w1")
    _patch_spawn_registers_active(monkeypatch)
    typed = []
    monkeypatch.setattr(srv, "_type_unguarded", lambda wid, text: typed.append((wid, text)))
    monkeypatch.setattr(srv, "_terminal_ls", lambda: [{"tabs": [{"windows": [
        {"id": "999", "cwd": "/tmp/wt", "foreground_processes": [{"cmdline": ["claude", "--resume"]}]}]}]}])
    monkeypatch.setattr(srv, "_INPUT_READY_TIMEOUT_SEC", 0.0)
    now = int(time.time())
    key = f"fable-move:w1:{now - 600}"
    stale.EMITTED_STATE.write_text(json.dumps({key: {
        "at": now - 30, "sid": "w1", "name": "alpha", "pid": 1, "window_id": "%9",
        "started_at": now - 600, "stage": "resuming", "attempts": 0}}))
    stale.main()
    assert typed == [("999", "[MANAGER] resume your task")]
    assert json.loads(stale.EMITTED_STATE.read_text())[key]["stage"] == "done"
    assert "FABLE_LIMIT" not in capsys.readouterr().out
    stale.main()
    assert len(typed) == 1


def test_a_resume_that_fails_with_any_exception_counts_as_an_attempt_and_the_tick_goes_on(
        nudgy, monkeypatch, capsys):
    calls, _polls, mock_calls = _move_setup(nudgy, monkeypatch, raises=KeyError("window_id"))
    _fable_worker(nudgy, "w2", "alpha")
    _parent_worker_record(nudgy, "w-beta", "beta", "boss")
    _paused_child = _paused_worker(nudgy, monkeypatch, kind="subworker", parent_name="beta", parent_sid="w-beta",
                                   depth=2, provider="claude-tui", runtime="claude")
    monkeypatch.setattr(nudgy.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError(pid)))
    nudgy.main()
    move = list(_moves(nudgy).values())[0]
    assert move["stage"] == "resuming" and move["attempts"] == 1 and "KeyError" in move["reason"]
    assert ("key", "%227", "2") in _paused_child.calls, "the record loop went on after the failed move"
    nudgy.main()
    nudgy.main()
    move = list(_moves(nudgy).values())[0]
    assert move["stage"] == "paged" and move["variant"] == "resume-refused" and move["attempts"] == 3
    assert len(mock_calls) == 3
    assert capsys.readouterr().out.count("FABLE_LIMIT alpha") == 1


def test_a_stale_resuming_record_sends_nothing_when_the_new_incarnation_has_its_own_fresh_banner(
        nudgy, monkeypatch, capsys):
    calls, _polls, mock_calls = _move_setup(nudgy, monkeypatch)
    path, _log = _fable_worker(nudgy, "w1", "alpha")
    started = int(json.loads(path.read_text())["started_at"])
    nudgy.EMITTED_STATE.write_text(json.dumps({f"fable-move:w1:{started - 600}": {
        "at": started - 30, "sid": "w1", "name": "alpha", "pid": 4141, "window_id": "%9",
        "started_at": started - 600, "stage": "resuming", "attempts": 0}}))
    nudgy.main()
    assert len(mock_calls) == 1
    assert [e["event"] for e in _ledger_events(nudgy) if e["event"] == "fable-moved"] == ["fable-moved"]
    state = json.loads(nudgy.EMITTED_STATE.read_text())
    assert state[f"fable-move:w1:{started - 600}"]["stage"] == "done"
    assert state[f"fable-move:w1:{started}"]["stage"] == "done"


def test_the_moved_stamp_lands_when_session_end_wins_the_archive_race(nudgy, monkeypatch):
    _calls, _polls, mock_calls = _move_setup(nudgy, monkeypatch)
    path, _log = _fable_worker(nudgy, "w1", "alpha")
    real_archive = nudgy._archive_child

    def racing_archive(record_path, record, reason, *, claim=False):
        record_path.unlink()
        nudgy._write_json_atomic(nudgy.CLOSED / "w1.json", {**record, "closed_reason": "session_end"})
        return real_archive(record_path, record, reason, claim=claim)

    monkeypatch.setattr(nudgy, "_archive_child", racing_archive)
    nudgy.main()
    closed = json.loads((nudgy.CLOSED / "w1.json").read_text())
    assert closed["closed_reason"] == "session_end" and closed["moved"] == "fable" and closed["moved_at"]
    assert len(mock_calls) == 1 and list(_moves(nudgy).values())[0]["stage"] == "done"


def test_a_move_is_held_while_text_sits_unsubmitted_in_the_pane(nudgy, monkeypatch, capsys):
    calls, _polls, mock_calls = _move_setup(nudgy, monkeypatch)
    monkeypatch.setattr(nudgy, "_pane_input_idle", lambda pane, memo, runtime="claude": False)
    _fable_worker(nudgy, "w1", "alpha")
    nudgy.main()
    assert ("close", "%9") not in calls and mock_calls == [] and _moves(nudgy) == {}
    out = capsys.readouterr().out
    assert out.count("FABLE_LIMIT alpha") == 1 and "unsubmitted" in out
    assert (nudgy.ACTIVE / "w1.json").exists()


def test_the_hold_page_reaches_the_recovery_manager_after_an_idle_manager_dies_on_the_hold(
        stale, capsys, monkeypatch, tmp_path):
    calls = _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    _seed_farm(monkeypatch, tmp_path, "b")
    _arm_pool(stale, "a")
    _write_record(stale, "mgr1", agent="manager", state="idle", name="mgr-A", window_id="9",
                  parent_manager_name=None, account="a")
    _hold_worker(stale, parent_manager_name="mgr-A")
    stale.main(manager_name="mgr-A")
    assert "ACCOUNT_HOLD a since" in capsys.readouterr().out
    assert (stale.CLOSED / "w1.json").exists()
    t0 = int(time.time())
    _hold_manager(stale, "mgr1", "mgr-A", t0, account="a")
    clock = {"now": t0 + 600}
    monkeypatch.setattr(stale.time, "time", lambda: clock["now"])
    stale.main(manager_name="mgr-A")
    assert capsys.readouterr().out == "" and len(_launch_calls(calls)) == 1
    (stale.ACTIVE / "mgr1.json").unlink()
    _write_record(stale, "mgr2", agent="manager", state="idle", name="mgr-A", window_id="10",
                  parent_manager_name=None, account="b")
    outs = []
    for _ in range(3):
        clock["now"] += 60
        stale.main(manager_name="mgr-A")
        outs.append(capsys.readouterr().out)
    assert sum(o.count("ACCOUNT_HOLD a since") for o in outs) == 1
    assert "LIMIT_CLEARED" in outs[0]


def test_a_second_incarnations_failed_move_pages_within_the_first_ones_keep_window(nudgy, monkeypatch, capsys):
    calls, _polls, mock_calls = _move_setup(nudgy, monkeypatch, alive=(4242,))
    path, _log = _fable_worker(nudgy, "w1", "alpha")
    started = int(json.loads(path.read_text())["started_at"])
    prior = started - 3600
    nudgy.EMITTED_STATE.write_text(json.dumps({
        f"fable-move:w1:{prior}": {"at": prior + 600, "sid": "w1", "name": "alpha", "pid": 4141,
                                   "window_id": "%8", "started_at": prior, "stage": "paged",
                                   "variant": "human", "reason": "human attached", "attempts": 0},
        f"fable-limit-page:w1:{prior}": {"first_seen": prior + 600, "paged": 1}}))
    nudgy.main()
    this = json.loads(nudgy.EMITTED_STATE.read_text())[f"fable-move:w1:{started}"]
    assert this["stage"] == "paged" and this["variant"] == "close-refused"
    assert capsys.readouterr().out.count("FABLE_LIMIT alpha") == 1


def test_a_stamped_account_missing_from_the_registry_is_held_itself_not_the_default(stale, capsys, monkeypatch):
    _capture_runs(stale, monkeypatch)
    monkeypatch.setattr(stale, "_keychain_unlocked", lambda: True)
    stale.ACCOUNT_REGISTRY.write_text(json.dumps({"pool": [{"name": "a"}, {"name": "b"}], "default": "a"}))
    _arm_pool(stale, "b")
    _hold_worker(stale, account="c")
    _parent_worker_record(stale, "w-a", "alpha", "boss", account="a")
    stale.main()
    assert list(_held(stale)) == ["c"]
    assert (stale.ACTIVE / "w-a.json").exists()
    assert json.loads((stale.CLOSED / "w1.json").read_text())["closed_reason"] == "account-hold"


def test_a_move_held_on_unsubmitted_text_sends_the_engineer_to_the_pane_and_names_no_switch(
        nudgy, monkeypatch, capsys):
    calls, _polls, mock_calls = _move_setup(nudgy, monkeypatch)
    monkeypatch.setattr(nudgy, "_pane_input_idle", lambda pane, memo, runtime="claude": False)
    _fable_worker(nudgy, "w1", "alpha")
    nudgy.main()
    out = capsys.readouterr().out
    assert "FABLE_LIMIT alpha → tell the engineer to clear the input line of pane %9" in out
    assert "/model" not in out and "switch it to" not in out and "never relay" in out


def test_a_hold_banner_whose_mark_write_failed_closes_nothing(stale, capsys, monkeypatch):
    _capture_runs(stale, monkeypatch)
    _arm_pool(stale, "a")
    monkeypatch.setattr(stale, "_record_hold", lambda account, reason, source, now: False)
    _hold_worker(stale)
    stale.main()
    assert (stale.ACTIVE / "w1.json").exists() and not (stale.CLOSED / "w1.json").exists()
    assert "ACCOUNT_HOLD" not in capsys.readouterr().out


def test_a_closing_move_whose_session_is_gone_and_process_dead_does_not_close_the_pane_again(nudgy, monkeypatch):
    calls, _polls, mock_calls = _move_setup(nudgy, monkeypatch)
    now = int(time.time())
    nudgy.EMITTED_STATE.write_text(json.dumps({f"fable-move:w1:{now - 600}": {
        "at": now - 30, "sid": "w1", "name": "alpha", "pid": 4242, "window_id": "%9",
        "started_at": now - 600, "stage": "closing", "attempts": 0}}))
    nudgy.main()
    assert ("close", "%9") not in calls
    assert list(_moves(nudgy).values())[0]["stage"] == "done" and len(mock_calls) == 1


def test_a_fable_elsewhere_probe_that_raises_pages_instead_of_aborting_the_tick(nudgy, monkeypatch, capsys):
    calls, _polls, mock_calls = _move_setup(nudgy, monkeypatch)

    def boom(account, now):
        raise RuntimeError("usage file unreadable")

    monkeypatch.setattr(nudgy, "_fable_elsewhere", boom)
    _fable_worker(nudgy, "w1", "alpha")
    nudgy.main()
    out = capsys.readouterr().out
    assert out.count("FABLE_LIMIT alpha") == 1 and "usage file unreadable" in out
    assert calls == [] and mock_calls == [] and (nudgy.ACTIVE / "w1.json").exists()


def _dir_records(stale, directory):
    return sorted(stale._dir_json(directory), key=lambda pair: pair[0])


def test_dir_json_reads_the_disk_while_the_memo_is_off(stale):
    stale._write_json_atomic(stale.CLOSED / "s1.json", {"v": 1})
    assert _dir_records(stale, stale.CLOSED) == [("s1", {"v": 1})]
    (stale.CLOSED / "s1.json").write_text(json.dumps({"v": 2}))
    assert _dir_records(stale, stale.CLOSED) == [("s1", {"v": 2})]


def test_dir_json_serves_an_unchanged_directory_from_memory(stale, monkeypatch):
    import builtins
    stale._write_json_atomic(stale.CLOSED / "s1.json", {"v": 1})
    stale._write_json_atomic(stale.CLOSED / "s2.json", {"v": 2})
    monkeypatch.setattr(stale, "_DIR_MEMO", {})
    first = _dir_records(stale, stale.CLOSED)
    real_open = builtins.open

    def no_open(path, *args, **kwargs):
        if isinstance(path, (str, Path)) and stale.CLOSED in Path(path).parents:
            raise AssertionError(f"opened {path}")
        return real_open(path, *args, **kwargs)

    def no_load(path):
        raise AssertionError(f"loaded {path}")

    monkeypatch.setattr(builtins, "open", no_open)
    monkeypatch.setattr(stale, "_load", no_load)
    assert _dir_records(stale, stale.CLOSED) == first == [("s1", {"v": 1}), ("s2", {"v": 2})]


def test_dir_json_sees_a_new_a_replaced_and_a_removed_file(stale, monkeypatch):
    monkeypatch.setattr(stale, "_DIR_MEMO", {})
    stale._write_json_atomic(stale.CLOSED / "s1.json", {"v": 1})
    assert _dir_records(stale, stale.CLOSED) == [("s1", {"v": 1})]
    stale._write_json_atomic(stale.CLOSED / "s2.json", {"v": 2})
    assert _dir_records(stale, stale.CLOSED) == [("s1", {"v": 1}), ("s2", {"v": 2})]
    stale._write_json_atomic(stale.CLOSED / "s1.json", {"v": 3})
    assert _dir_records(stale, stale.CLOSED) == [("s1", {"v": 3}), ("s2", {"v": 2})]
    (stale.CLOSED / "s2.json").unlink()
    assert _dir_records(stale, stale.CLOSED) == [("s1", {"v": 3})]


@pytest.mark.parametrize("memo", [None, {}])
def test_dir_json_gives_the_file_stem_with_each_record(stale, monkeypatch, memo):
    monkeypatch.setattr(stale, "_DIR_MEMO", memo)
    stale._write_json_atomic(stale.CLOSED / "s1.json", {"claude_sid": "other"})
    (stale.CLOSED / "notes.txt").write_text("{}")
    (stale.CLOSED / "bad.json").write_text("{")
    assert list(stale._dir_json(stale.CLOSED)) == [("s1", {"claude_sid": "other"})]
    assert list(stale._dir_json(stale.CLOSED / "absent")) == []


def _closed_cap_record(stale, sid, closed_at):
    path = stale.CLOSED / f"{sid}.json"
    stale._write_json_atomic(path, {"claude_sid": sid, "closed_reason": stale.USAGE_CAP_REASON,
                                    "parent_manager_name": "mgr", "closed_at": closed_at})
    os.utime(path, (closed_at, closed_at))
    return path


def test_usage_cap_owed_does_not_open_an_old_closed_file_while_the_memo_is_off(stale, monkeypatch):
    old = _closed_cap_record(stale, "old", 1_000)
    new = _closed_cap_record(stale, "new", 3_000)
    loaded = []
    real = stale._load
    monkeypatch.setattr(stale, "_load", lambda path: loaded.append(Path(path)) or real(path))
    assert stale._DIR_MEMO is None
    assert stale._usage_cap_owed("mgr", 2_000.0) is True
    assert loaded == [new] and old not in loaded


def test_the_assignment_index_takes_the_sid_from_the_file_name_under_the_memo(stale, monkeypatch):
    stale._write_json_atomic(stale.ASSIGNMENTS / "sid-on-disk.json",
                             {"assignment_id": "aid-1", "claude_sid": "sid-in-record", "round": 1})
    off = stale._assignment_index()
    monkeypatch.setattr(stale, "_DIR_MEMO", {})
    assert stale._assignment_index() == off == {"aid-1": {"sid": "sid-on-disk", "round": 1}}


@pytest.mark.parametrize("memo", [None, {}])
def test_usage_cap_owed_still_ignores_a_closed_record_older_than_since_under_the_memo(stale, monkeypatch, memo):
    monkeypatch.setattr(stale, "_DIR_MEMO", memo)
    _closed_cap_record(stale, "old", 1_000)
    assert stale._usage_cap_owed("mgr", 2_000.0) is False
    _closed_cap_record(stale, "new", 3_000)
    assert stale._usage_cap_owed("mgr", 2_000.0) is True
    assert stale._usage_cap_owed("someone-else", 2_000.0) is False
