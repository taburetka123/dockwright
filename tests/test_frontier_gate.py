import importlib.util
import json
import os
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_PATH = REPO_ROOT / "deploy" / "scripts" / "frontier_gate.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("frontier_gate_under_test", GATE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gate(tmp_path, monkeypatch):
    for var in ("GARDENER_FRONTIER_INTERVAL_DAYS", "GARDENER_FRONTIER_RETRY_GAP"):
        monkeypatch.delenv(var, raising=False)
    mod = _load_gate()
    gardener_dir = tmp_path / "gardener"
    monkeypatch.setattr(mod, "GARDENER_DIR", gardener_dir)
    monkeypatch.setattr(mod, "LEDGER_PATH", gardener_dir / "ledger.jsonl")
    monkeypatch.setattr(mod, "MARKER_PATH", gardener_dir / "last-frontier-run")
    monkeypatch.setattr(mod, "GATE_LOG_PATH", gardener_dir / "frontier-gate.log")
    monkeypatch.setattr(mod, "STOP_PATHS", (tmp_path / "frontier-stop", tmp_path / "legacy-frontier-stop"))
    monkeypatch.setattr(mod, "RUN_LOCK_DIR", tmp_path / "locks" / "analyst-run.lock")
    monkeypatch.setattr(mod, "RUN_SCRIPT", tmp_path / "gardener-run.sh")
    gardener_dir.mkdir(parents=True)
    return mod


NOW = time.time()


def _arm_marker(gate, age_days: float) -> None:
    gate.MARKER_PATH.write_text("frontier marker\n")
    stamp = NOW - age_days * 86400
    os.utime(gate.MARKER_PATH, (stamp, stamp))


def _ledger_run_start(gate, age_sec: float, lane: str | None = "frontier",
                      envelope: str = "type") -> None:
    record = {envelope: "run_start", "ts": NOW - age_sec, "run_id": "r"}
    if lane is not None:
        record["lane"] = lane
    with gate.LEDGER_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")


class TestStopAndLock:
    def test_stop_file_closes_gate(self, gate):
        gate.STOP_PATHS[0].touch()
        _arm_marker(gate, age_days=40)
        decision, _ = gate.decide(NOW, force=False)
        assert decision == "stopped"

    def test_legacy_stop_file_closes_gate(self, gate):
        gate.STOP_PATHS[1].touch()
        _arm_marker(gate, age_days=40)
        decision, _ = gate.decide(NOW, force=False)
        assert decision == "stopped"

    def test_force_refuses_under_stop_file(self, gate, monkeypatch):
        gate.STOP_PATHS[0].touch()
        spawned = []
        monkeypatch.setattr(gate, "spawn_run", lambda trigger: spawned.append(trigger))
        rc = gate.main(["--force"])
        assert rc == gate.EXIT_REFUSED_STOPPED
        assert spawned == []

    def test_live_lock_blocks_even_force(self, gate, monkeypatch):
        gate.RUN_LOCK_DIR.mkdir(parents=True)
        (gate.RUN_LOCK_DIR / "pid").write_text(str(os.getpid()))
        _arm_marker(gate, age_days=40)
        assert gate.decide(NOW, force=False)[0] == "locked"
        spawned = []
        monkeypatch.setattr(gate, "spawn_run", lambda trigger: spawned.append(trigger))
        rc = gate.main(["--force"])
        assert rc == gate.EXIT_FORCE_LOCKED
        assert spawned == []

    def test_dead_lock_holder_reads_as_free(self, gate):
        gate.RUN_LOCK_DIR.mkdir(parents=True)
        (gate.RUN_LOCK_DIR / "pid").write_text("99999999")
        _arm_marker(gate, age_days=40)
        assert gate.decide(NOW, force=False)[0] == "frontier"


class TestIntervalAndMarker:
    def test_absent_marker_is_not_armed(self, gate):
        decision, _ = gate.decide(NOW, force=False)
        assert decision == "not_armed"

    def test_marker_younger_than_interval_not_due(self, gate):
        _arm_marker(gate, age_days=6)
        decision, detail = gate.decide(NOW, force=False)
        assert decision == "not_due"
        assert detail["marker_age_days"] == pytest.approx(6, abs=0.1)

    def test_marker_past_interval_fires(self, gate):
        _arm_marker(gate, age_days=8)
        decision, _ = gate.decide(NOW, force=False)
        assert decision == "frontier"

    def test_marker_just_past_boundary_fires(self, gate):
        # 0.01d above the boundary, not exactly 7.0 — utime/float round-trip
        # noise at the exact boundary would make that assertion flaky.
        _arm_marker(gate, age_days=7.01)
        assert gate.decide(NOW, force=False)[0] == "frontier"

    def test_default_interval_is_weekly(self, gate):
        assert gate.INTERVAL_DAYS == 7.0

    def test_interval_env_override(self, gate, monkeypatch, tmp_path):
        # Override must differ from the 7d default to discriminate: an 8d-old
        # marker fires under the default but not under a 28d override.
        monkeypatch.setenv("GARDENER_FRONTIER_INTERVAL_DAYS", "28")
        mod = _load_gate()
        monkeypatch.setattr(mod, "GARDENER_DIR", gate.GARDENER_DIR)
        monkeypatch.setattr(mod, "LEDGER_PATH", gate.LEDGER_PATH)
        monkeypatch.setattr(mod, "MARKER_PATH", gate.MARKER_PATH)
        monkeypatch.setattr(mod, "STOP_PATHS", gate.STOP_PATHS)
        monkeypatch.setattr(mod, "RUN_LOCK_DIR", gate.RUN_LOCK_DIR)
        _arm_marker(gate, age_days=8)
        assert mod.decide(NOW, force=False)[0] == "not_due"


class TestRetryGap:
    def test_recent_frontier_run_start_cools(self, gate):
        _arm_marker(gate, age_days=40)
        _ledger_run_start(gate, age_sec=24 * 3600)
        decision, detail = gate.decide(NOW, force=False)
        assert decision == "cooldown"
        assert detail["last_run_age_sec"] == pytest.approx(24 * 3600, abs=5)

    def test_frontier_run_past_gap_fires(self, gate):
        _arm_marker(gate, age_days=40)
        _ledger_run_start(gate, age_sec=49 * 3600)
        assert gate.decide(NOW, force=False)[0] == "frontier"

    def test_digest_lane_runs_do_not_cool_frontier(self, gate):
        _arm_marker(gate, age_days=40)
        _ledger_run_start(gate, age_sec=3600, lane="digest")
        _ledger_run_start(gate, age_sec=7200, lane=None)  # legacy = digest
        assert gate.decide(NOW, force=False)[0] == "frontier"

    def test_legacy_event_envelope_tolerated(self, gate):
        _arm_marker(gate, age_days=40)
        _ledger_run_start(gate, age_sec=3600, envelope="event")
        assert gate.decide(NOW, force=False)[0] == "cooldown"

    def test_force_bypasses_cooldown_and_interval(self, gate):
        _ledger_run_start(gate, age_sec=60)
        assert gate.decide(NOW, force=True)[0] == "force"


class TestHostileInputs:
    def test_future_dated_marker_is_not_due(self, gate):
        # negative age (clock skew / restored backup) must read as benign
        gate.MARKER_PATH.write_text("frontier marker\n")
        future = NOW + 3 * 86400
        os.utime(gate.MARKER_PATH, (future, future))
        decision, detail = gate.decide(NOW, force=False)
        assert decision == "not_due"
        assert detail["marker_age_days"] < 0

    def test_corrupt_ledger_lines_tolerated(self, gate):
        _arm_marker(gate, age_days=40)
        gate.LEDGER_PATH.write_text("not json\n[1,2]\n{\"type\": \"run_start\"}\n")
        assert gate.decide(NOW, force=False)[0] == "frontier"


class TestMain:
    def test_due_marker_spawns_with_frontier_lane(self, gate, monkeypatch):
        _arm_marker(gate, age_days=30)
        calls = []
        monkeypatch.setattr(gate.subprocess, "Popen",
                            lambda cmd, **kw: calls.append(cmd) or type("P", (), {})())
        rc = gate.main([])
        assert rc == gate.EXIT_OK
        assert len(calls) == 1
        assert "--lane" in calls[0] and "frontier" in calls[0]
        assert "frontier" in gate.GATE_LOG_PATH.read_text()

    def test_dry_run_never_spawns(self, gate, monkeypatch):
        _arm_marker(gate, age_days=30)
        spawned = []
        monkeypatch.setattr(gate, "spawn_run", lambda trigger: spawned.append(trigger))
        rc = gate.main(["--dry-run"])
        assert rc == gate.EXIT_OK
        assert spawned == []
        assert "spawned=False" in gate.GATE_LOG_PATH.read_text()

    def test_not_armed_never_spawns(self, gate, monkeypatch):
        spawned = []
        monkeypatch.setattr(gate, "spawn_run", lambda trigger: spawned.append(trigger))
        rc = gate.main([])
        assert rc == gate.EXIT_OK
        assert spawned == []
        assert "not_armed" in gate.GATE_LOG_PATH.read_text()


class TestHomeFallback:
    def test_prefers_dockwright_gardener_home(self, tmp_path, monkeypatch):
        (tmp_path / ".claude" / "dockwright" / "gardener").mkdir(parents=True)
        (tmp_path / ".claude" / "gardener").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        mod = _load_gate()
        assert mod.GARDENER_DIR == tmp_path / ".claude" / "dockwright" / "gardener"
        assert mod.STOP_PATHS[0] == tmp_path / ".claude" / "dockwright" / "frontier-stop"

    def test_falls_back_to_legacy_gardener_home(self, tmp_path, monkeypatch):
        (tmp_path / ".claude" / "gardener").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        mod = _load_gate()
        assert mod.GARDENER_DIR == tmp_path / ".claude" / "gardener"
