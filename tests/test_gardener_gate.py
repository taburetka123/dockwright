import importlib.util
import json
import os
import subprocess
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
    for var in ("GARDENER_K", "GARDENER_FLOOR_DAYS", "GARDENER_MAX_RUNS_PER_WEEK",
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
    gardener_dir.mkdir(parents=True)
    findings_dir.mkdir(parents=True)
    closed_dir.mkdir(parents=True)
    # Healthy default world for the producer asserts: both expected SessionEnd
    # hooks present, no closed-session records (no activity ⇒ no stale signal).
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


class TestAccumulationGate:
    def test_k_fresh_unreviewed_opens(self, gate):
        for i in range(8):
            _write_finding(gate, f"f{i}")
        decision, detail = gate.decide(NOW, force=False)
        assert decision == "accum"
        assert detail["new_unreviewed"] == 8

    def test_below_k_stays_closed(self, gate):
        for i in range(7):
            _write_finding(gate, f"f{i}")
        _write_marker(gate, age_sec=3600)
        decision, _ = gate.decide(NOW, force=False)
        assert decision == "no_material"

    def test_reviewed_findings_do_not_count(self, gate):
        for i in range(8):
            _write_finding(gate, f"f{i}", reviewed=(i < 4))
        _write_marker(gate, age_sec=3600)
        decision, detail = gate.decide(NOW, force=False)
        assert decision == "no_material"
        assert detail["new_unreviewed"] == 4

    def test_findings_older_than_marker_do_not_count(self, gate):
        _write_marker(gate, age_sec=600)
        for i in range(8):
            _write_finding(gate, f"old{i}", age_sec=1200)
        for i in range(3):
            _write_finding(gate, f"new{i}", age_sec=60)
        decision, detail = gate.decide(NOW, force=False)
        assert decision == "no_material"
        assert detail["new_unreviewed"] == 3


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

    def test_recent_marker_below_k_stays_closed(self, gate):
        _write_marker(gate, age_sec=2 * 86400)
        for i in range(3):
            _write_finding(gate, f"f{i}", age_sec=60)
        decision, _ = gate.decide(NOW, force=False)
        assert decision == "no_material"

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
        # newest run is past the 6h cooldown so the cap is what fires
        _write_ledger_runs(gate, [7 * 3600, 86400, 3 * 86400])
        decision, detail = gate.decide(NOW, force=False)
        assert decision == "cap"
        assert detail["runs_in_week"] == 3

    def test_old_runs_do_not_count(self, gate):
        for i in range(8):
            _write_finding(gate, f"f{i}")
        _write_ledger_runs(gate, [8 * 86400, 9 * 86400, 10 * 86400])
        decision, _ = gate.decide(NOW, force=False)
        assert decision == "accum"

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
        assert decision == "accum"
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
        assert decision == "accum"

    def test_force_bypasses_cooldown(self, gate):
        _write_ledger_runs(gate, [60])
        decision, _ = gate.decide(NOW, force=True)
        assert decision == "force"

    def test_empty_ledger_means_no_cooldown(self, gate):
        for i in range(8):
            _write_finding(gate, f"f{i}")
        decision, _ = gate.decide(NOW, force=False)
        assert decision == "accum"

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
    """The shared ledger carries the frontier loop's runs too; the digest
    gate's cap and cooldown must count ONLY the digest lane (arch-soundness
    C2: the lane-blind pool breaks silently)."""

    def test_frontier_runs_do_not_consume_digest_cap(self, gate):
        for i in range(8):
            _write_finding(gate, f"f{i}")
        _write_ledger_runs(gate, [7 * 3600, 86400, 2 * 86400], lane="frontier")
        decision, detail = gate.decide(NOW, force=False)
        assert decision == "accum"
        assert detail["runs_in_week"] == 0

    def test_frontier_run_does_not_arm_digest_cooldown(self, gate):
        for i in range(8):
            _write_finding(gate, f"f{i}")
        _write_ledger_runs(gate, [600], lane="frontier")
        assert gate.decide(NOW, force=False)[0] == "accum"

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
        assert decision == "accum"

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
        assert spawned == ["accum"]
        log = gate.GATE_LOG_PATH.read_text()
        assert "accum" in log and "spawned=True" in log

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
    """Producer-liveness asserts (arch review A1): the hourly gate is the one
    tick that can notice the findings supply died — a severed SessionEnd hook
    or a stale producer must warn, never silently quiesce."""

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
        """Sessions closing for 3 days while the newest finding is older still
        ⇒ the producer is dead, not the workload quiet."""
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
        """No session activity ⇒ no findings expected ⇒ no false positive."""
        _write_finding(gate, "f1", age_sec=30 * 86400)
        assert gate.producer_warnings() == []

    def test_main_logs_warnings_to_gate_log(self, gate, monkeypatch):
        monkeypatch.setattr(gate, "spawn_run", lambda trigger: None)
        self._write_closed(gate, "s1", age_sec=600)  # no findings ⇒ stale
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
        gate.main(["--dry-run"])  # marker is fresh now — throttled
        assert len(notified) == 1
        old = NOW - 2 * 86400
        os.utime(gate.WARN_MARKER_PATH, (old, old))
        gate.main(["--dry-run"])
        assert len(notified) == 2

    def test_warnings_never_change_decision_or_exit_code(self, gate, monkeypatch):
        monkeypatch.setattr(gate, "spawn_run", lambda trigger: None)
        self._write_closed(gate, "s1", age_sec=600)  # stale producer
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

    def test_stale_brick_flag_does_not_defer(self, gate, monkeypatch, tmp_path):
        calls = self._capture_spawn(gate, monkeypatch)
        gate.ORCHESTRATOR_DIR.mkdir(parents=True, exist_ok=True)
        flag = gate.ORCHESTRATOR_DIR / ".manager-limited-x"
        flag.touch()
        os.utime(flag, (NOW - 900, NOW - 900))
        self._write_entry(gate, "sid-r5", self._transcript(tmp_path))
        assert gate.process_retry_queue(NOW) is True
        assert len(calls) == 1

    def test_empty_queue_is_noop(self, gate, monkeypatch):
        calls = self._capture_spawn(gate, monkeypatch)
        assert gate.process_retry_queue(NOW) is False
        assert calls == []

    def test_main_retry_suppresses_digest_spawn_this_tick(self, gate, monkeypatch, tmp_path):
        """Accum is armed AND a retry is queued: the retry wins the tick (the
        two would contend on the analyst-run mutex); digest re-decides next tick."""
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
        """--force is a human-initiated digest; the retry pre-step yields."""
        digest_spawns = []
        monkeypatch.setattr(gate, "spawn_run", lambda trigger: digest_spawns.append(trigger))
        retry_calls = self._capture_spawn(gate, monkeypatch)
        entry = self._write_entry(gate, "sid-r8", self._transcript(tmp_path))
        assert gate.main(["--force"]) == 0
        assert retry_calls == [] and entry.exists()
        assert digest_spawns == ["force"]


class TestNotifyPytestGuard:
    """_notify must never fire a real notification from inside a pytest run.

    test_module_toggle.py execs this script as a REAL subprocess (HOME in the
    pytest tmpdir), where no monkeypatch can reach: main() always runs
    _warn_producer, the bare HOME makes settings.json unreadable
    (hooks_missing), the fresh HOME has no throttle marker — so without a
    guard IN THE SCRIPT every full-suite run fired real 'gardener-gate'
    desktop notifications (2026-07-03 leak). The guard keys on
    PYTEST_CURRENT_TEST, which pytest exports and {**os.environ} child envs
    inherit (inheritance pinned in test_module_toggle.py)."""

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
        """Deterministic repro of the leak mechanics + production-behavior pin:
        outside pytest, _notify really invokes /usr/bin/osascript with a
        'display notification … "gardener-gate"' payload."""
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
