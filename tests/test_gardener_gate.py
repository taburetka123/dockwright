import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_PATH = REPO_ROOT / "deploy" / "scripts" / "gardener_gate.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("gardener_gate_under_test", GATE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gate(tmp_path, monkeypatch):
    for var in ("GARDENER_FLOOR_DAYS", "GARDENER_MAX_RUNS_PER_WEEK",
                "GARDENER_MIN_RUN_GAP", "GARDENER_FRONTIER_INTERVAL_DAYS",
                "GARDENER_FRONTIER_RETRY_GAP"):
        monkeypatch.delenv(var, raising=False)
    mod = _load_gate()
    gardener_dir = tmp_path / "gardener"
    findings_dir = tmp_path / "selffix-findings"
    monkeypatch.setattr(mod, "GARDENER_DIR", gardener_dir)
    monkeypatch.setattr(mod, "DIGESTS_DIR", gardener_dir / "digests")
    monkeypatch.setattr(mod, "PROPOSALS_DIR", gardener_dir / "proposals")
    monkeypatch.setattr(mod, "LEDGER_PATH", gardener_dir / "ledger.jsonl")
    monkeypatch.setattr(mod, "MARKER_PATH", gardener_dir / "last-digest")
    monkeypatch.setattr(mod, "GATE_LOG_PATH", gardener_dir / "gate.log")
    monkeypatch.setattr(mod, "STOP_PATHS", (tmp_path / "gardener-stop", tmp_path / "legacy-gardener-stop"))
    monkeypatch.setattr(mod, "FINDINGS_DIR", findings_dir)
    monkeypatch.setattr(mod, "RUN_LOCK_DIR", tmp_path / "locks" / "analyst-run.lock")
    monkeypatch.setattr(mod, "RUN_SCRIPT", tmp_path / "gardener-run.sh")
    closed_dir = tmp_path / "orchestrator" / "closed"
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(mod, "CLOSED_DIR", closed_dir)
    monkeypatch.setattr(mod, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(mod, "WARN_MARKER_PATH", gardener_dir / ".producer-warn")
    monkeypatch.setattr(mod, "_notify", lambda message: None)
    monkeypatch.setattr(mod, "RETRY_DIR", tmp_path / "selffix-retry")
    monkeypatch.setattr(mod, "ORCHESTRATOR_DIR", tmp_path / "orchestrator")
    monkeypatch.setattr(mod, "SELFFIX_RUN_SCRIPT", tmp_path / "selffix-run.sh")
    monkeypatch.setattr(mod, "SELFFIX_LOG_PATH", tmp_path / "selffix-trigger.log")
    monkeypatch.setattr(mod, "SELFFIX_DEBUG_PATHS", (tmp_path / "selffix-debug", tmp_path / "legacy-selffix-debug"))
    monkeypatch.setattr(mod, "usage_capped", lambda: False)
    gardener_dir.mkdir(parents=True)
    findings_dir.mkdir(parents=True)
    closed_dir.mkdir(parents=True)
    settings_path.write_text(json.dumps({"hooks": {"SessionEnd": [{"hooks": [
        {"type": "command",
         "command": "bash -c 'CLAUDE_PARENT_PID=$PPID orchestrator session-end'"},
        {"type": "command",
         "command": "bash /Users/testop/.claude/scripts/selffix-trigger.sh"},
    ]}]}}))
    return mod


NOW = time.time()


def _write_finding(gate, name: str, age_sec: float = 60.0, reviewed: bool = False) -> Path:
    path = gate.FINDINGS_DIR / f"{name}.md"
    path.write_text(f"## Self-Fix Findings\n{name}\n")
    stamp = NOW - age_sec
    os.utime(path, (stamp, stamp))
    if reviewed:
        path.with_suffix(".reviewed").touch()
    return path


def _write_marker(gate, age_sec: float) -> None:
    gate.MARKER_PATH.write_text("marker\n")
    stamp = NOW - age_sec
    os.utime(gate.MARKER_PATH, (stamp, stamp))


def _write_ledger_runs(gate, ages_sec: list[float], extra_lines: list[str] | None = None,
                       lane: str | None = None, envelope: str = "event") -> None:
    lines = []
    for i, age in enumerate(ages_sec):
        record = {envelope: "run_start", "ts": NOW - age, "run_id": f"r{i}"}
        if lane is not None:
            record["lane"] = lane
        lines.append(json.dumps(record))
    lines += extra_lines or []
    gate.LEDGER_PATH.write_text("\n".join(lines) + "\n")


def _hold_lock(gate, pid: int) -> None:
    gate.RUN_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    (gate.RUN_LOCK_DIR / "pid").write_text(str(pid))


class TestStopFile:
    def test_stop_file_closes_gate(self, gate):
        gate.STOP_PATHS[0].touch()
        _write_finding(gate, "a")
        decision, _ = gate.decide(NOW, force=False)
        assert decision == "stopped"

    def test_legacy_stop_file_closes_gate(self, gate):
        gate.STOP_PATHS[1].touch()
        _write_finding(gate, "a")
        decision, _ = gate.decide(NOW, force=False)
        assert decision == "stopped"

    def test_force_refuses_under_stop_file(self, gate):
        gate.STOP_PATHS[0].touch()
        decision, _ = gate.decide(NOW, force=True)
        assert decision == "refused_stopped"

    def test_main_exit_code_on_forced_stop(self, gate, monkeypatch):
        gate.STOP_PATHS[0].touch()
        spawned = []
        monkeypatch.setattr(gate, "spawn_run", lambda trigger: spawned.append(trigger))
        rc = gate.main(["--force"])
        assert rc == gate.EXIT_REFUSED_STOPPED
        assert spawned == []


class TestWeeklyInterval:
    def test_recent_marker_is_not_due_even_with_many_findings(self, gate):
        _write_marker(gate, age_sec=3600)
        for i in range(8):
            _write_finding(gate, f"f{i}")
        decision, detail = gate.decide(NOW, force=False)
        assert decision == "not_due"
        assert detail["new_unreviewed"] == 8

    def test_reviewed_findings_do_not_count(self, gate):
        _write_marker(gate, age_sec=8 * 86400)
        for i in range(4):
            _write_finding(gate, f"f{i}", reviewed=True)
        decision, detail = gate.decide(NOW, force=False)
        assert decision == "no_material"
        assert detail["new_unreviewed"] == 0

    def test_findings_older_than_marker_do_not_count(self, gate):
        _write_marker(gate, age_sec=8 * 86400)
        for i in range(8):
            _write_finding(gate, f"old{i}", age_sec=9 * 86400)
        for i in range(3):
            _write_finding(gate, f"new{i}", age_sec=60)
        decision, detail = gate.decide(NOW, force=False)
        assert decision == "floor"
        assert detail["new_unreviewed"] == 3

    def test_failed_run_is_retried_after_cooldown(self, gate):
        _write_marker(gate, age_sec=8 * 86400)
        _write_finding(gate, "fresh", age_sec=60)
        _write_ledger_runs(gate, [7 * 3600])
        decision, detail = gate.decide(NOW, force=False)
        assert decision == "floor"
        assert detail["runs_in_week"] == 1


class TestWeeklyFloor:
    def test_stale_marker_with_one_new_finding_opens(self, gate):
        _write_marker(gate, age_sec=8 * 86400)
        _write_finding(gate, "fresh", age_sec=60)
        decision, _ = gate.decide(NOW, force=False)
        assert decision == "floor"

    def test_stale_marker_with_zero_new_findings_stays_closed(self, gate):
        _write_marker(gate, age_sec=8 * 86400)
        _write_finding(gate, "old", age_sec=9 * 86400)
        decision, _ = gate.decide(NOW, force=False)
        assert decision == "no_material"

    def test_recent_marker_is_not_due(self, gate):
        _write_marker(gate, age_sec=2 * 86400)
        for i in range(3):
            _write_finding(gate, f"f{i}", age_sec=60)
        decision, _ = gate.decide(NOW, force=False)
        assert decision == "not_due"

    def test_no_marker_with_some_material_opens_as_floor(self, gate):
        _write_finding(gate, "only-one", age_sec=60)
        decision, _ = gate.decide(NOW, force=False)
        assert decision == "floor"

    def test_no_marker_no_material_stays_closed(self, gate):
        decision, _ = gate.decide(NOW, force=False)
        assert decision == "no_material"


class TestRunRateCap:
    def test_three_recent_runs_hit_cap(self, gate):
        for i in range(8):
            _write_finding(gate, f"f{i}")
        _write_ledger_runs(gate, [7 * 3600, 86400, 3 * 86400])
        decision, detail = gate.decide(NOW, force=False)
        assert decision == "cap"
        assert detail["runs_in_week"] == 3

    def test_old_runs_do_not_count(self, gate):
        for i in range(8):
            _write_finding(gate, f"f{i}")
        _write_ledger_runs(gate, [8 * 86400, 9 * 86400, 10 * 86400])
        decision, _ = gate.decide(NOW, force=False)
        assert decision == "floor"

    def test_force_bypasses_cap(self, gate):
        _write_ledger_runs(gate, [3600, 7200, 10800])
        decision, _ = gate.decide(NOW, force=True)
        assert decision == "force"

    def test_corrupt_ledger_lines_tolerated(self, gate):
        for i in range(8):
            _write_finding(gate, f"f{i}")
        _write_ledger_runs(
            gate, [7 * 3600],
            extra_lines=["not json at all", '{"event": "run_end"}', '["a-list"]'],
        )
        decision, detail = gate.decide(NOW, force=False)
        assert decision == "floor"
        assert detail["runs_in_week"] == 1


class TestCooldown:
    def test_recent_run_start_triggers_cooldown(self, gate):
        for i in range(8):
            _write_finding(gate, f"f{i}")
        _write_ledger_runs(gate, [3600])
        decision, detail = gate.decide(NOW, force=False)
        assert decision == "cooldown"
        assert detail["last_run_age_sec"] == 3600

    def test_cooldown_preempts_cap(self, gate):
        for i in range(8):
            _write_finding(gate, f"f{i}")
        _write_ledger_runs(gate, [3600, 86400, 2 * 86400])
        decision, _ = gate.decide(NOW, force=False)
        assert decision == "cooldown"

    def test_run_older_than_gap_passes(self, gate):
        for i in range(8):
            _write_finding(gate, f"f{i}")
        _write_ledger_runs(gate, [7 * 3600])
        decision, _ = gate.decide(NOW, force=False)
        assert decision == "floor"

    def test_force_bypasses_cooldown(self, gate):
        _write_ledger_runs(gate, [60])
        decision, _ = gate.decide(NOW, force=True)
        assert decision == "force"

    def test_empty_ledger_means_no_cooldown(self, gate):
        for i in range(8):
            _write_finding(gate, f"f{i}")
        decision, _ = gate.decide(NOW, force=False)
        assert decision == "floor"

    def test_main_does_not_spawn_on_cooldown(self, gate, monkeypatch):
        for i in range(8):
            _write_finding(gate, f"f{i}")
        _write_ledger_runs(gate, [600])
        spawned = []
        monkeypatch.setattr(gate, "spawn_run", lambda trigger: spawned.append(trigger))
        rc = gate.main([])
        assert rc == gate.EXIT_OK
        assert spawned == []
        assert "cooldown" in gate.GATE_LOG_PATH.read_text()


class TestLaneIsolation:

    def test_frontier_runs_do_not_consume_digest_cap(self, gate):
        for i in range(8):
            _write_finding(gate, f"f{i}")
        _write_ledger_runs(gate, [7 * 3600, 86400, 2 * 86400], lane="frontier")
        decision, detail = gate.decide(NOW, force=False)
        assert decision == "floor"
        assert detail["runs_in_week"] == 0

    def test_frontier_run_does_not_arm_digest_cooldown(self, gate):
        for i in range(8):
            _write_finding(gate, f"f{i}")
        _write_ledger_runs(gate, [600], lane="frontier")
        assert gate.decide(NOW, force=False)[0] == "floor"

    def test_legacy_laneless_events_count_as_digest(self, gate):
        for i in range(8):
            _write_finding(gate, f"f{i}")
        _write_ledger_runs(gate, [600], lane=None)
        assert gate.decide(NOW, force=False)[0] == "cooldown"

    def test_explicit_digest_lane_counts(self, gate):
        for i in range(8):
            _write_finding(gate, f"f{i}")
        _write_ledger_runs(gate, [7 * 3600, 86400, 2 * 86400], lane="digest")
        assert gate.decide(NOW, force=False)[0] == "cap"

    def test_type_envelope_tolerated(self, gate):
        for i in range(8):
            _write_finding(gate, f"f{i}")
        _write_ledger_runs(gate, [600], lane="digest", envelope="type")
        assert gate.decide(NOW, force=False)[0] == "cooldown"


class TestRunMutex:
    def test_live_pid_lock_closes_gate(self, gate):
        for i in range(8):
            _write_finding(gate, f"f{i}")
        _hold_lock(gate, os.getpid())
        decision, _ = gate.decide(NOW, force=False)
        assert decision == "locked"

    def test_live_pid_lock_blocks_force_too(self, gate):
        _hold_lock(gate, os.getpid())
        decision, _ = gate.decide(NOW, force=True)
        assert decision == "locked"

    def test_dead_pid_lock_reads_as_free(self, gate):
        for i in range(8):
            _write_finding(gate, f"f{i}")
        _hold_lock(gate, 99999999)
        decision, _ = gate.decide(NOW, force=False)
        assert decision == "floor"

    def test_lock_without_pid_file_reads_as_held(self, gate):
        gate.RUN_LOCK_DIR.mkdir(parents=True, exist_ok=True)
        decision, _ = gate.decide(NOW, force=False)
        assert decision == "locked"


class TestMain:
    def test_open_gate_spawns_and_logs(self, gate, monkeypatch):
        for i in range(8):
            _write_finding(gate, f"f{i}")
        spawned = []
        monkeypatch.setattr(gate, "spawn_run", lambda trigger: spawned.append(trigger))
        rc = gate.main([])
        assert rc == gate.EXIT_OK
        assert spawned == ["floor"]
        log = gate.GATE_LOG_PATH.read_text()
        assert "floor" in log and "spawned=True" in log

    def test_dry_run_never_spawns(self, gate, monkeypatch):
        for i in range(8):
            _write_finding(gate, f"f{i}")
        spawned = []
        monkeypatch.setattr(gate, "spawn_run", lambda trigger: spawned.append(trigger))
        rc = gate.main(["--dry-run"])
        assert rc == gate.EXIT_OK
        assert spawned == []
        assert "spawned=False" in gate.GATE_LOG_PATH.read_text()

    def test_closed_gate_logs_without_spawn(self, gate, monkeypatch):
        spawned = []
        monkeypatch.setattr(gate, "spawn_run", lambda trigger: spawned.append(trigger))
        rc = gate.main([])
        assert rc == gate.EXIT_OK
        assert spawned == []
        assert "no_material" in gate.GATE_LOG_PATH.read_text()

    def test_force_locked_exit_code(self, gate, monkeypatch):
        _hold_lock(gate, os.getpid())
        spawned = []
        monkeypatch.setattr(gate, "spawn_run", lambda trigger: spawned.append(trigger))
        rc = gate.main(["--force"])
        assert rc == gate.EXIT_FORCE_LOCKED
        assert spawned == []

    def test_main_creates_gardener_dirs(self, gate, monkeypatch):
        monkeypatch.setattr(gate, "spawn_run", lambda trigger: None)
        gate.main(["--dry-run"])
        assert gate.DIGESTS_DIR.is_dir()
        assert gate.PROPOSALS_DIR.is_dir()


class TestProducerAsserts:

    def _write_closed(self, gate, name: str, age_sec: float) -> None:
        p = gate.CLOSED_DIR / f"{name}.json"
        p.write_text("{}")
        stamp = NOW - age_sec
        os.utime(p, (stamp, stamp))

    def test_clean_world_no_warnings(self, gate):
        _write_finding(gate, "f1", age_sec=3600)
        self._write_closed(gate, "s1", age_sec=600)
        assert gate.producer_warnings() == []

    def test_missing_selffix_hook_warns(self, gate):
        gate.SETTINGS_PATH.write_text(json.dumps({"hooks": {"SessionEnd": [{"hooks": [
            {"command": "bash -c 'CLAUDE_PARENT_PID=$PPID orchestrator session-end'"},
        ]}]}}))
        warnings = gate.producer_warnings()
        assert any(w.startswith("hooks_missing") and "selffix-trigger.sh" in w
                   for w in warnings)

    def test_unreadable_settings_warns(self, gate):
        gate.SETTINGS_PATH.write_text("{not json")
        assert any(w.startswith("hooks_missing") for w in gate.producer_warnings())

    def test_dockwright_session_end_form_satisfies_hook_assert(self, gate):
        gate.SETTINGS_PATH.write_text(json.dumps({"hooks": {"SessionEnd": [{"hooks": [
            {"command": "bash -c 'CLAUDE_PARENT_PID=$PPID /r/.venv/bin/dockwright session-end'"},
            {"command": "bash /Users/testop/.claude/scripts/selffix-trigger.sh"},
        ]}]}}))
        assert not any(w.startswith("hooks_missing") for w in gate.producer_warnings())

    def test_legacy_orchestrator_session_end_form_satisfies_hook_assert(self, gate):
        gate.SETTINGS_PATH.write_text(json.dumps({"hooks": {"SessionEnd": [{"hooks": [
            {"command": "bash -c 'CLAUDE_PARENT_PID=$PPID /r/.venv/bin/orchestrator session-end'"},
            {"command": "bash /Users/testop/.claude/scripts/selffix-trigger.sh"},
        ]}]}}))
        assert not any(w.startswith("hooks_missing") for w in gate.producer_warnings())

    def test_neither_session_end_binary_form_warns(self, gate):
        gate.SETTINGS_PATH.write_text(json.dumps({"hooks": {"SessionEnd": [{"hooks": [
            {"command": "bash /Users/testop/.claude/scripts/selffix-trigger.sh"},
        ]}]}}))
        warnings = gate.producer_warnings()
        assert any(w.startswith("hooks_missing") and "session-end" in w for w in warnings)

    def test_stale_producer_warns(self, gate):
        _write_finding(gate, "f1", age_sec=10 * 86400)
        self._write_closed(gate, "s1", age_sec=600)
        warnings = gate.producer_warnings()
        assert any(w.startswith("producer_stale") for w in warnings)

    def test_no_findings_at_all_with_activity_warns(self, gate):
        self._write_closed(gate, "s1", age_sec=600)
        assert any(w.startswith("producer_stale") for w in gate.producer_warnings())

    def test_fresh_findings_no_stale_warning(self, gate):
        _write_finding(gate, "f1", age_sec=3600)
        self._write_closed(gate, "s1", age_sec=600)
        assert not any(w.startswith("producer_stale") for w in gate.producer_warnings())

    def test_no_closed_records_no_stale_warning(self, gate):
        _write_finding(gate, "f1", age_sec=30 * 86400)
        assert gate.producer_warnings() == []

    def test_main_logs_warnings_to_gate_log(self, gate, monkeypatch):
        monkeypatch.setattr(gate, "spawn_run", lambda trigger: None)
        self._write_closed(gate, "s1", age_sec=600)
        rc = gate.main(["--dry-run"])
        assert rc == gate.EXIT_OK
        log = gate.GATE_LOG_PATH.read_text()
        assert "producer_stale" in log

    def test_main_notifies_with_throttle(self, gate, monkeypatch):
        monkeypatch.setattr(gate, "spawn_run", lambda trigger: None)
        notified = []
        monkeypatch.setattr(gate, "_notify", lambda message: notified.append(message))
        self._write_closed(gate, "s1", age_sec=600)
        gate.main(["--dry-run"])
        assert len(notified) == 1
        gate.main(["--dry-run"])
        assert len(notified) == 1
        old = NOW - 2 * 86400
        os.utime(gate.WARN_MARKER_PATH, (old, old))
        gate.main(["--dry-run"])
        assert len(notified) == 2

    def test_warnings_never_change_decision_or_exit_code(self, gate, monkeypatch):
        monkeypatch.setattr(gate, "spawn_run", lambda trigger: None)
        self._write_closed(gate, "s1", age_sec=600)
        rc = gate.main(["--dry-run"])
        assert rc == gate.EXIT_OK
        assert "no_material" in gate.GATE_LOG_PATH.read_text()


class TestRetryQueue:
    def _write_entry(self, gate, sid: str, transcript: Path | str | None = None,
                     attempts: int = 0, age_sec: float = 60.0, raw: str | None = None) -> Path:
        gate.RETRY_DIR.mkdir(parents=True, exist_ok=True)
        path = gate.RETRY_DIR / f"{sid}.json"
        if raw is not None:
            path.write_text(raw)
        else:
            path.write_text(json.dumps({
                "sid": sid, "transcript_path": str(transcript),
                "attempts": attempts, "enqueued_at": "2026-06-13T00:00:00Z",
                "reason": "finished-error"}))
        stamp = NOW - age_sec
        os.utime(path, (stamp, stamp))
        return path

    def _transcript(self, tmp_path) -> Path:
        t = tmp_path / "transcript.jsonl"
        t.write_text('{"type":"user","message":{"content":"hi"}}\n')
        return t

    def _capture_spawn(self, gate, monkeypatch):
        calls = []
        monkeypatch.setattr(gate, "spawn_retry", lambda transcript, sid: calls.append((transcript, sid)))
        return calls

    def test_spawns_one_retry_and_reports(self, gate, monkeypatch, tmp_path):
        calls = self._capture_spawn(gate, monkeypatch)
        t = self._transcript(tmp_path)
        entry = self._write_entry(gate, "sid-r1", t)
        assert gate.process_retry_queue(NOW) is True
        assert calls == [(str(t), "sid-r1")]
        assert not entry.exists(), "entry must be deleted before spawn (retry-once)"

    def test_oldest_entry_first_one_per_tick(self, gate, monkeypatch, tmp_path):
        calls = self._capture_spawn(gate, monkeypatch)
        t = self._transcript(tmp_path)
        self._write_entry(gate, "sid-young", t, age_sec=10)
        old = self._write_entry(gate, "sid-old", t, age_sec=600)
        assert gate.process_retry_queue(NOW) is True
        assert [c[1] for c in calls] == ["sid-old"]
        assert not old.exists()
        assert (gate.RETRY_DIR / "sid-young.json").exists(), "second entry waits for next tick"

    def test_transcript_missing_drops_entry(self, gate, monkeypatch, tmp_path):
        calls = self._capture_spawn(gate, monkeypatch)
        entry = self._write_entry(gate, "sid-r2", tmp_path / "gone.jsonl")
        assert gate.process_retry_queue(NOW) is False
        assert calls == []
        assert not entry.exists()

    def test_garbage_entry_dropped_gate_survives(self, gate, monkeypatch, tmp_path):
        calls = self._capture_spawn(gate, monkeypatch)
        garbage = self._write_entry(gate, "sid-bad", raw="{not json")
        t = self._transcript(tmp_path)
        self._write_entry(gate, "sid-good", t, age_sec=10)
        assert gate.process_retry_queue(NOW) is True
        assert not garbage.exists()
        assert [c[1] for c in calls] == ["sid-good"]

    def test_attempts_exhausted_dropped(self, gate, monkeypatch, tmp_path):
        calls = self._capture_spawn(gate, monkeypatch)
        entry = self._write_entry(gate, "sid-r3", self._transcript(tmp_path), attempts=1)
        assert gate.process_retry_queue(NOW) is False
        assert calls == [] and not entry.exists()

    def test_deferred_while_bricked(self, gate, monkeypatch, tmp_path):
        calls = self._capture_spawn(gate, monkeypatch)
        gate.ORCHESTRATOR_DIR.mkdir(parents=True, exist_ok=True)
        flag = gate.ORCHESTRATOR_DIR / ".manager-limited-x"
        flag.touch()
        os.utime(flag, (NOW - 30, NOW - 30))
        entry = self._write_entry(gate, "sid-r4", self._transcript(tmp_path))
        assert gate.process_retry_queue(NOW) is False
        assert calls == [] and entry.exists(), "attempt must not burn into a live brick"

    def test_deferred_while_a_usage_cap_pauses_the_fleet(self, gate, monkeypatch, tmp_path):
        monkeypatch.setenv("SELFFIX_DEBUG", "1")
        calls = self._capture_spawn(gate, monkeypatch)
        monkeypatch.setattr(gate, "usage_capped", lambda: True)
        entry = self._write_entry(gate, "sid-cap", self._transcript(tmp_path))
        assert gate.process_retry_queue(NOW) is False
        assert calls == [] and entry.exists()
        assert "retry:deferred  sid-cap  reason=usage-cap" in gate.SELFFIX_LOG_PATH.read_text()

    def test_stale_brick_flag_does_not_defer(self, gate, monkeypatch, tmp_path):
        calls = self._capture_spawn(gate, monkeypatch)
        gate.ORCHESTRATOR_DIR.mkdir(parents=True, exist_ok=True)
        flag = gate.ORCHESTRATOR_DIR / ".manager-limited-x"
        flag.touch()
        os.utime(flag, (NOW - 900, NOW - 900))
        self._write_entry(gate, "sid-r5", self._transcript(tmp_path))
        assert gate.process_retry_queue(NOW) is True
        assert len(calls) == 1

    def test_spawn_value_error_drops_the_entry_and_the_tick_survives(self, gate, monkeypatch, tmp_path):
        monkeypatch.setenv("SELFFIX_DEBUG", "1")

        def spawn_raises(transcript, sid):
            raise ValueError("embedded null byte")

        monkeypatch.setattr(gate, "spawn_retry", spawn_raises)
        entry = self._write_entry(gate, "sid-r11", self._transcript(tmp_path))
        assert gate.process_retry_queue(NOW) is False
        assert not entry.exists()
        assert "retry:dropped  sid-r11  reason=spawn-failed" in gate.SELFFIX_LOG_PATH.read_text()

    def test_empty_queue_is_noop(self, gate, monkeypatch):
        calls = self._capture_spawn(gate, monkeypatch)
        assert gate.process_retry_queue(NOW) is False
        assert calls == []

    def test_main_retry_suppresses_digest_spawn_this_tick(self, gate, monkeypatch, tmp_path):
        digest_spawns = []
        monkeypatch.setattr(gate, "spawn_run", lambda trigger: digest_spawns.append(trigger))
        retry_calls = self._capture_spawn(gate, monkeypatch)
        for i in range(8):
            _write_finding(gate, f"f{i}")
        self._write_entry(gate, "sid-r6", self._transcript(tmp_path))
        assert gate.main([]) == 0
        assert len(retry_calls) == 1
        assert digest_spawns == []
        log = gate.GATE_LOG_PATH.read_text()
        assert "retry_spawned" in log

    def test_main_stopped_leaves_queue_untouched(self, gate, monkeypatch, tmp_path):
        monkeypatch.setattr(gate, "spawn_run", lambda trigger: None)
        retry_calls = self._capture_spawn(gate, monkeypatch)
        entry = self._write_entry(gate, "sid-r7", self._transcript(tmp_path))
        gate.STOP_PATHS[0].touch()
        gate.main([])
        assert retry_calls == [] and entry.exists()

    def test_main_locked_leaves_queue_untouched(self, gate, monkeypatch, tmp_path):
        monkeypatch.setattr(gate, "spawn_run", lambda trigger: None)
        retry_calls = self._capture_spawn(gate, monkeypatch)
        entry = self._write_entry(gate, "sid-r9", self._transcript(tmp_path))
        _hold_lock(gate, os.getpid())
        gate.main([])
        assert retry_calls == [] and entry.exists()

    def test_main_dry_run_leaves_queue_untouched(self, gate, monkeypatch, tmp_path):
        monkeypatch.setattr(gate, "spawn_run", lambda trigger: None)
        retry_calls = self._capture_spawn(gate, monkeypatch)
        entry = self._write_entry(gate, "sid-r10", self._transcript(tmp_path))
        assert gate.main(["--dry-run"]) == 0
        assert retry_calls == [] and entry.exists()

    def test_main_force_skips_retry_prestep(self, gate, monkeypatch, tmp_path):
        digest_spawns = []
        monkeypatch.setattr(gate, "spawn_run", lambda trigger: digest_spawns.append(trigger))
        retry_calls = self._capture_spawn(gate, monkeypatch)
        entry = self._write_entry(gate, "sid-r8", self._transcript(tmp_path))
        assert gate.main(["--force"]) == 0
        assert retry_calls == [] and entry.exists()
        assert digest_spawns == ["force"]


class TestNotifyPytestGuard:

    def _record_runs(self, monkeypatch):
        calls = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: calls.append([str(x) for x in a[0]]))
        return calls

    def test_notify_suppressed_under_pytest(self, monkeypatch):
        mod = _load_gate()
        calls = self._record_runs(monkeypatch)
        assert os.environ.get("PYTEST_CURRENT_TEST"), "pytest must export the guard var"
        mod._notify("boom")
        assert calls == []

    def test_notify_invokes_real_osascript_outside_pytest(self, monkeypatch):
        mod = _load_gate()
        calls = self._record_runs(monkeypatch)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        mod._notify("hooks_missing settings.json unreadable")
        assert len(calls) == 1
        assert calls[0][0] == "/usr/bin/osascript"
        assert "display notification" in calls[0][2]
        assert 'with title "gardener-gate"' in calls[0][2]


class TestHomeFallback:
    def test_prefers_dockwright_homes(self, tmp_path, monkeypatch):
        claude = tmp_path / ".claude"
        for rel in ("dockwright/gardener", "gardener",
                    "dockwright/selffix/findings", "selffix-findings",
                    "dockwright/selffix/retry", "selffix-retry",
                    "dockwright", "orchestrator"):
            (claude / rel).mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        mod = _load_gate()
        assert mod.GARDENER_DIR == claude / "dockwright" / "gardener"
        assert mod.FINDINGS_DIR == claude / "dockwright" / "selffix" / "findings"
        assert mod.RETRY_DIR == claude / "dockwright" / "selffix" / "retry"
        assert mod.CLOSED_DIR == claude / "dockwright" / "closed"
        assert mod.ORCHESTRATOR_DIR == claude / "dockwright"
        assert mod.STOP_PATHS[0] == claude / "dockwright" / "gardener-stop"
        assert mod.SELFFIX_DEBUG_PATHS[0] == claude / "dockwright" / "selffix" / "debug"

    def test_falls_back_to_legacy_homes(self, tmp_path, monkeypatch):
        claude = tmp_path / ".claude"
        for rel in ("gardener", "selffix-findings", "selffix-retry", "orchestrator"):
            (claude / rel).mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        mod = _load_gate()
        assert mod.GARDENER_DIR == claude / "gardener"
        assert mod.FINDINGS_DIR == claude / "selffix-findings"
        assert mod.RETRY_DIR == claude / "selffix-retry"
        assert mod.CLOSED_DIR == claude / "orchestrator" / "closed"


RETRY_ENV_TOML = (
    '[gardener]\n'
    'retry_env = ["DOCKWRIGHT_CLAUDE_BIN=/opt/wrapper/claude-wrapper", "EXAMPLE_RETRY_VAR=example-value"]\n'
)


@pytest.fixture(params=["tomllib", "scanner"])
def reader(request, monkeypatch):
    if request.param == "scanner":
        monkeypatch.setitem(sys.modules, "tomllib", None)
    return request.param


def _config(tmp_path, monkeypatch, body: str) -> None:
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(body)
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))


def _capture_popen(gate, monkeypatch) -> list:
    calls = []
    monkeypatch.setattr(gate.subprocess, "Popen", lambda *a, **kw: calls.append(kw))
    return calls


def _gate_warnings(gate) -> str:
    return gate.GATE_LOG_PATH.read_text() if gate.GATE_LOG_PATH.is_file() else ""


def test_retry_spawn_gets_retry_env(gate, tmp_path, monkeypatch, reader):
    _config(tmp_path, monkeypatch, RETRY_ENV_TOML)
    calls = _capture_popen(gate, monkeypatch)
    gate.spawn_retry("/t.jsonl", "sid-1")
    env = calls[0]["env"]
    assert env["DOCKWRIGHT_CLAUDE_BIN"] == "/opt/wrapper/claude-wrapper"
    assert env["EXAMPLE_RETRY_VAR"] == "example-value"
    assert env["SELFFIX_RETRY_ATTEMPT"] == "1"
    assert "WARN" not in _gate_warnings(gate)


def test_digest_spawn_never_gets_retry_env(gate, tmp_path, monkeypatch, reader):
    _config(tmp_path, monkeypatch, RETRY_ENV_TOML)
    monkeypatch.delenv("DOCKWRIGHT_CLAUDE_BIN", raising=False)
    monkeypatch.delenv("EXAMPLE_RETRY_VAR", raising=False)
    calls = _capture_popen(gate, monkeypatch)
    gate.spawn_run("accum")
    env = calls[0].get("env") or dict(os.environ)
    assert "DOCKWRIGHT_CLAUDE_BIN" not in env
    assert "EXAMPLE_RETRY_VAR" not in env


def test_absent_retry_env_spawns_plainly(gate, tmp_path, monkeypatch, reader):
    _config(tmp_path, monkeypatch, '[gardener]\nhigh_skills = []\n')
    monkeypatch.delenv("DOCKWRIGHT_CLAUDE_BIN", raising=False)
    calls = _capture_popen(gate, monkeypatch)
    gate.spawn_retry("/t.jsonl", "sid-2")
    assert "DOCKWRIGHT_CLAUDE_BIN" not in calls[0]["env"]
    assert "WARN" not in _gate_warnings(gate)


@pytest.mark.parametrize("entry", ["SELFFIX_RETRY_ATTEMPT=0", "HOME=/tmp/x", "PATH=/tmp/x"])
def test_reserved_key_is_refused_with_a_warning(gate, tmp_path, monkeypatch, reader, entry):
    _config(tmp_path, monkeypatch, f'[gardener]\nretry_env = ["DOCKWRIGHT_CLAUDE_BIN=/opt/wrapper/claude-wrapper", "{entry}"]\n')
    monkeypatch.delenv("DOCKWRIGHT_CLAUDE_BIN", raising=False)
    calls = _capture_popen(gate, monkeypatch)
    gate.spawn_retry("/t.jsonl", "sid-3")
    env = calls[0]["env"]
    assert env["SELFFIX_RETRY_ATTEMPT"] == "1"
    assert "DOCKWRIGHT_CLAUDE_BIN" not in env
    assert "WARN" in _gate_warnings(gate)


def test_entry_without_equals_is_refused_with_a_warning(gate, tmp_path, monkeypatch, reader):
    _config(tmp_path, monkeypatch, '[gardener]\nretry_env = ["DOCKWRIGHT_CLAUDE_BIN"]\n')
    monkeypatch.delenv("DOCKWRIGHT_CLAUDE_BIN", raising=False)
    calls = _capture_popen(gate, monkeypatch)
    gate.spawn_retry("/t.jsonl", "sid-4")
    assert "DOCKWRIGHT_CLAUDE_BIN" not in calls[0]["env"]
    assert "WARN" in _gate_warnings(gate)


def test_multiline_retry_env(gate, tmp_path, monkeypatch, reader):
    _config(tmp_path, monkeypatch, '[gardener]\nretry_env = [\n  "DOCKWRIGHT_CLAUDE_BIN=/opt/wrapper/claude-wrapper",\n]\n')
    monkeypatch.delenv("DOCKWRIGHT_CLAUDE_BIN", raising=False)
    calls = _capture_popen(gate, monkeypatch)
    gate.spawn_retry("/t.jsonl", "sid-5")
    env = calls[0]["env"]
    if reader == "tomllib":
        assert env["DOCKWRIGHT_CLAUDE_BIN"] == "/opt/wrapper/claude-wrapper"
    else:
        assert "DOCKWRIGHT_CLAUDE_BIN" not in env
        assert "WARN" in _gate_warnings(gate)


def test_non_table_gardener_value_spawns_plainly_with_a_warning(gate, tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, 'gardener = 5\n')
    monkeypatch.delenv("DOCKWRIGHT_CLAUDE_BIN", raising=False)
    monkeypatch.delenv("EXAMPLE_RETRY_VAR", raising=False)
    calls = _capture_popen(gate, monkeypatch)
    gate.spawn_retry("/t.jsonl", "sid-6")
    env = calls[0]["env"]
    assert "DOCKWRIGHT_CLAUDE_BIN" not in env
    assert "EXAMPLE_RETRY_VAR" not in env
    assert env["SELFFIX_RETRY_ATTEMPT"] == "1"
    assert "WARN  [gardener] retry_env unreadable:" in _gate_warnings(gate)


WRAPPER = "/opt/wrapper/claude-wrapper"


def unparsed(where):
    return ({}, [f"[gardener] retry_env present in a shape this reader cannot parse, on {where}"])


@pytest.mark.parametrize("body, expected", [
    (f'[gardener]  # retro settings\nretry_env = ["DOCKWRIGHT_CLAUDE_BIN={WRAPPER}"]\n',
     ({"DOCKWRIGHT_CLAUDE_BIN": WRAPPER}, [])),
    (f'gardener.retry_env = ["DOCKWRIGHT_CLAUDE_BIN={WRAPPER}"]\n', unparsed("line 1")),
    (f'[gardener]\n"retry_env" = ["DOCKWRIGHT_CLAUDE_BIN={WRAPPER}"]\n', unparsed("line 2")),
    (f'gardener = {{ retry_env = ["DOCKWRIGHT_CLAUDE_BIN={WRAPPER}"] }}\n', unparsed("line 1")),
    (f'[gardener.retry_env]\nDOCKWRIGHT_CLAUDE_BIN = "{WRAPPER}"\n', unparsed("line 1")),
    (f'[[gardener]]\nretry_env = ["DOCKWRIGHT_CLAUDE_BIN={WRAPPER}"]\n', unparsed("line 2")),
    ('[gardener]\nretry_env = ["EXAMPLE_RETRY_VAR=/x/w"]  # was ["EXAMPLE_RETRY_VAR=/old"]\n',
     ({"EXAMPLE_RETRY_VAR": "/x/w"}, [])),
], ids=["header-comment", "dotted-key", "quoted-key", "inline-table", "sub-table",
        "array-of-tables", "trailing-comment"])
def test_scanner_parses_or_refuses_each_retry_env_shape(gate, tmp_path, monkeypatch, body, expected):
    monkeypatch.setitem(sys.modules, "tomllib", None)
    _config(tmp_path, monkeypatch, body)
    assert gate.retry_env() == expected


@pytest.mark.parametrize("body", [
    "[gardener]\nnote = '''\nretry_env = [\"EXAMPLE_RETRY_VAR=from-a-string\"]\n'''\n",
    '[gardener]\nnote = """a \\"""\nretry_env = ["EXAMPLE_RETRY_VAR=from-a-string"]\n"""\n',
    "[other]\nx = [\n  ['''\n[gardener]\nretry_env = [\"EXAMPLE_RETRY_VAR=from-a-string\"]\n'''],\n]\n",
    '[gardener]\n"retry\\u005fenv" = ["EXAMPLE_RETRY_VAR=x"]\n',
    '["gardener"]\nretry_env = ["EXAMPLE_RETRY_VAR=x"]\n',
    '[gardener]\nretry_env.sub = "x"\n',
    '[gardener.retry_env.sub]\nx = 1\n',
    '[gardener]\nretry_env = ["EXAMPLE_RETRY_VAR=1"]\n[gardener]\nretry_env = ["EXAMPLE_RETRY_VAR=2"]\n',
], ids=["inside-literal-string", "inside-basic-string", "inside-nested-array-string",
        "escaped-key", "quoted-header", "dotted-sub-key", "nested-sub-table", "second-appearance"])
def test_scanner_agrees_with_tomllib_or_warns(gate, tmp_path, monkeypatch, body):
    _config(tmp_path, monkeypatch, body)
    full = gate.retry_env()
    monkeypatch.setitem(sys.modules, "tomllib", None)
    scanned = gate.retry_env()
    assert scanned == full or (scanned[0] == {} and scanned[1]), (full, scanned)


def test_a_second_mention_refuses_the_key_and_names_every_mention(gate, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "tomllib", None)
    _config(tmp_path, monkeypatch,
            '[gardener]\nretry_env = ["EXAMPLE_RETRY_VAR=/x/w"]\n\n[agent_vars]\n'
            'note = "the retry reads [gardener] retry_env."\n')
    assert gate.retry_env() == ({}, [
        "[gardener] retry_env refused: this reader needs its own line to be the only one "
        "that mentions retry_env outside whole-line comments; mentioned on lines 2, 5"])


def test_comma_inside_a_quoted_value(gate, tmp_path, monkeypatch, reader):
    _config(tmp_path, monkeypatch,
            '[gardener]\nretry_env = ["EXAMPLE_RETRY_VAR=a,b", \'OTHER_RETRY_VAR=c, d\']\n')
    assert gate.retry_env() == ({"EXAMPLE_RETRY_VAR": "a,b", "OTHER_RETRY_VAR": "c, d"}, [])


def test_starter_template_with_commented_out_retry_env_is_silent(gate, tmp_path, monkeypatch, reader):
    from dockwright.config import DEFAULT_TOML
    assert "# retry_env = [" in DEFAULT_TOML
    _config(tmp_path, monkeypatch, DEFAULT_TOML)
    assert gate.retry_env() == ({}, [])


def test_nul_in_retry_env_entry_spawns_plainly_with_a_warning(gate, tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, '[gardener]\nretry_env = ["DOCKWRIGHT_CLAUDE_BIN=/opt/wrapper\\u0000/claude"]\n')
    monkeypatch.delenv("DOCKWRIGHT_CLAUDE_BIN", raising=False)
    gate.SELFFIX_RUN_SCRIPT.write_text("exit 0\n")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"type":"user","message":{"content":"hi"}}\n')
    gate.RETRY_DIR.mkdir(parents=True)
    (gate.RETRY_DIR / "sid-nul.json").write_text(json.dumps(
        {"sid": "sid-nul", "transcript_path": str(transcript), "attempts": 0}))
    real_popen = subprocess.Popen
    spawned = []

    def popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append((kwargs, proc))
        return proc

    monkeypatch.setattr(gate.subprocess, "Popen", popen)
    assert gate.main([]) == 0
    for _, proc in spawned:
        proc.wait(timeout=10)
    assert len(spawned) == 1
    assert "DOCKWRIGHT_CLAUDE_BIN" not in spawned[0][0]["env"]
    assert "WARN  [gardener] retry_env" in _gate_warnings(gate)


@pytest.mark.parametrize("body, expected", [("exit 0", True), ("exit 1", False), ("exit 2", False)])
def test_usage_capped_asks_the_usage_cap_command(tmp_path, monkeypatch, body, expected):
    stub = tmp_path / "dockwright"
    stub.write_text(f'#!/usr/bin/env bash\n[ "$*" = "usage-cap --paused" ] || exit 9\n{body}\n')
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    assert _load_gate().usage_capped() is expected


def test_usage_capped_without_the_command_does_not_hold_the_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _load_gate().usage_capped() is False
