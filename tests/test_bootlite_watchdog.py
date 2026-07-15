"""Boot-lite watchdog (tick fallback) — manager-less worker detection.

Loads the standalone script the same way test_gardener_gate.py loads the gate;
notification/nudge side effects are captured via module-attr monkeypatching, pid
liveness is deterministic via a patched _pid_alive.
"""
import importlib.util
import json
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "deploy" / "scripts" / "bootlite_watchdog.py"

NOW = time.time()
LIVE_PID = 111
DEAD_PID = 222


def _load_watchdog():
    spec = importlib.util.spec_from_file_location("bootlite_watchdog_under_test", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def dog(tmp_path, monkeypatch):
    for var in ("BOOTLITE_RENOTIFY_SEC", "BOOTLITE_MAX_NOTIFY", "CLAUDE_ORCH_AUTONUDGE"):
        monkeypatch.delenv(var, raising=False)
    mod = _load_watchdog()
    orch = tmp_path / "orchestrator"
    bootlite = tmp_path / "bootlite"
    monkeypatch.setattr(mod, "ACTIVE", orch / "active")
    monkeypatch.setattr(mod, "QUESTIONS", orch / "questions")
    monkeypatch.setattr(mod, "ORPHANS", orch / "orphans")
    monkeypatch.setattr(mod, "BOOTLITE_DIR", bootlite)
    monkeypatch.setattr(mod, "STATE_PATH", bootlite / "state.json")
    monkeypatch.setattr(mod, "LEDGER_PATH", bootlite / "ledger.jsonl")
    monkeypatch.setattr(mod, "CHECK_LOG_PATH", bootlite / "check.log")
    monkeypatch.setattr(mod, "STOP_PATHS", (tmp_path / "bootlite-stop", tmp_path / "legacy-bootlite-stop"))
    monkeypatch.setattr(mod, "_pid_alive", lambda pid: pid == LIVE_PID)
    (orch / "active").mkdir(parents=True)
    (orch / "questions").mkdir(parents=True)

    mod._test_notifications = []
    monkeypatch.setattr(mod, "_notify_macos", lambda msg: mod._test_notifications.append(msg))
    mod._test_nudges = []

    class _FakeDriver:
        def send_text(self, window_id, text, submit=True):
            mod._test_nudges.append((window_id, text))

    monkeypatch.setattr(mod, "_resolve_get_driver", lambda: (lambda: _FakeDriver()))
    return mod


def test_notify_macos_suppressed_under_pytest(no_live_tmux):
    """The watchdog is a deployed standalone script (subprocess-runnable, like
    gardener_gate.py — the 2026-07-03 leak): its _notify_macos must no-op under
    PYTEST_CURRENT_TEST itself, not rely on per-test monkeypatching. The
    absorber-presence assert runs first so a regression can't detonate a real
    notification."""
    mod = _load_watchdog()
    assert no_live_tmux.osascript == []
    mod._notify_macos("boom")
    assert no_live_tmux.osascript == []


def test_notify_macos_invokes_real_osascript_outside_pytest(monkeypatch):
    """Production-behavior pin: outside pytest the helper really requests a
    'display notification … "bootlite watchdog"' via osascript (argv recorded
    by a stub — nothing executes)."""
    import subprocess
    mod = _load_watchdog()
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: calls.append([str(x) for x in a[0]]))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    mod._notify_macos("boom")
    assert len(calls) == 1
    assert calls[0][0] == "osascript"
    assert "display notification" in calls[0][2]
    assert 'with title "bootlite watchdog"' in calls[0][2]


def _write_record(dog, sid, **fields):
    record = {"claude_sid": sid, "pid": LIVE_PID, "started_at": 0}
    record.update(fields)
    (dog.ACTIVE / f"{sid}.json").write_text(json.dumps(record))


def _write_manager(dog, name="grumpy-yak", sid="mgr-1", pid=LIVE_PID):
    _write_record(dog, sid, agent="manager", name=name, pid=pid)


def _write_worker(dog, sid, parent, pid=LIVE_PID, window_id="w-1", **extra):
    _write_record(dog, sid, agent="worker", name=f"task-{sid}", pid=pid,
                  parent_manager_name=parent, window_id=window_id,
                  state="processing", **extra)


def _ledger_events(dog):
    if not dog.LEDGER_PATH.is_file():
        return []
    return [json.loads(line) for line in dog.LEDGER_PATH.read_text().splitlines() if line.strip()]


def _state(dog):
    if not dog.STATE_PATH.is_file():
        return {}
    return json.loads(dog.STATE_PATH.read_text())


class TestStopFile:
    def test_stop_file_short_circuits(self, dog):
        dog.STOP_PATHS[0].touch()
        _write_worker(dog, "w1", "grumpy-yak")
        decision, _ = dog.run_tick(NOW)
        assert decision == "stopped"
        assert dog._test_notifications == []
        assert _ledger_events(dog) == []

    def test_legacy_stop_file_short_circuits(self, dog):
        dog.STOP_PATHS[1].touch()
        _write_worker(dog, "w1", "grumpy-yak")
        decision, _ = dog.run_tick(NOW)
        assert decision == "stopped"
        assert not dog.STATE_PATH.exists()


class TestOrphanDetection:
    def test_worker_with_missing_manager_is_orphaned(self, dog):
        _write_worker(dog, "w1", "grumpy-yak")
        decision, detail = dog.run_tick(NOW)
        assert decision == "orphans"
        assert detail["groups"] == {"grumpy-yak": 1}
        assert len(dog._test_notifications) == 1
        assert "grumpy-yak" in dog._test_notifications[0]
        events = [e["event"] for e in _ledger_events(dog)]
        assert "orphan_detected" in events
        assert "notified" in events

    def test_worker_with_dead_manager_pid_is_orphaned(self, dog):
        _write_manager(dog, pid=DEAD_PID)
        _write_worker(dog, "w1", "grumpy-yak")
        decision, detail = dog.run_tick(NOW)
        assert decision == "orphans"
        assert detail["groups"] == {"grumpy-yak": 1}

    def test_worker_with_live_manager_is_not_orphaned(self, dog):
        _write_manager(dog)
        _write_worker(dog, "w1", "grumpy-yak")
        decision, detail = dog.run_tick(NOW)
        assert decision == "ok"
        assert dog._test_notifications == []

    def test_dead_pid_worker_is_ignored(self, dog):
        _write_worker(dog, "w1", "grumpy-yak", pid=DEAD_PID)
        decision, _ = dog.run_tick(NOW)
        assert decision == "ok"

    def test_legacy_worker_orphaned_only_when_no_managers_at_all(self, dog):
        _write_worker(dog, "w1", None)
        decision, detail = dog.run_tick(NOW)
        assert decision == "orphans"
        assert detail["groups"] == {"_unscoped": 1}

    def test_legacy_worker_with_any_live_manager_is_not_orphaned(self, dog):
        _write_manager(dog, name="some-other-manager")
        _write_worker(dog, "w1", None)
        decision, _ = dog.run_tick(NOW)
        assert decision == "ok"

    def test_dead_manager_orphans_despite_other_live_manager(self, dog):
        """Domain #2 (arch review B5): the predicate is per-parent_manager_name —
        one live manager anywhere must NOT mask another domain's dead manager."""
        _write_manager(dog, name="grumpy-yak")                      # domain 1, alive
        _write_manager(dog, name="sly-otter", sid="mgr-2", pid=DEAD_PID)  # domain 2, dead
        _write_worker(dog, "w1", "grumpy-yak")
        _write_worker(dog, "w2", "sly-otter", window_id="w-2")
        decision, detail = dog.run_tick(NOW)
        assert decision == "orphans"
        assert detail["groups"] == {"sly-otter": 1}

    def test_two_orphan_groups_notified_independently(self, dog):
        _write_worker(dog, "w1", "grumpy-yak")
        _write_worker(dog, "w2", "sly-otter", window_id="w-2")
        decision, detail = dog.run_tick(NOW)
        assert decision == "orphans"
        assert detail["groups"] == {"grumpy-yak": 1, "sly-otter": 1}
        assert len(dog._test_notifications) == 2

    def test_nested_records_excluded_from_both_sides(self, dog):
        """Nested sub-sessions inherit CLAUDE_PARENT_MANAGER (and a live pid),
        so without the exclusion a nested ghost would read as an orphaned
        worker — or, as a manager-agent ghost, as a live manager."""
        _write_worker(dog, "ghost", "grumpy-yak", nested=True)
        decision, _ = dog.run_tick(NOW)
        assert decision == "ok"

    def test_corrupt_active_record_and_non_int_pid_skipped(self, dog):
        (dog.ACTIVE / "junk.json").write_text("{not json")
        _write_worker(dog, "w0", "grumpy-yak", pid=None)
        _write_worker(dog, "w1", "grumpy-yak")
        decision, detail = dog.run_tick(NOW)
        assert decision == "orphans"
        assert detail["groups"] == {"grumpy-yak": 1}


class TestNotifyDedup:
    def test_second_tick_within_renotify_window_does_not_renotify(self, dog):
        _write_worker(dog, "w1", "grumpy-yak")
        dog.run_tick(NOW)
        dog.run_tick(NOW + 3600)
        assert len(dog._test_notifications) == 1

    def test_renotifies_past_window(self, dog):
        _write_worker(dog, "w1", "grumpy-yak")
        dog.run_tick(NOW)
        dog.run_tick(NOW + dog.RENOTIFY_SEC + 1)
        assert len(dog._test_notifications) == 2

    def test_notify_cap_stops_notifications(self, dog):
        _write_worker(dog, "w1", "grumpy-yak")
        tick = NOW
        for _ in range(dog.MAX_NOTIFY_PER_STRETCH + 3):
            dog.run_tick(tick)
            tick += dog.RENOTIFY_SEC + 1
        assert len(dog._test_notifications) == dog.MAX_NOTIFY_PER_STRETCH

    def test_first_seen_and_last_notified_adopted_from_session_end_flag(self, dog):
        orphaned_at = NOW - 600
        dog.ORPHANS.mkdir(parents=True)
        (dog.ORPHANS / "grumpy-yak.json").write_text(json.dumps({
            "manager_name": "grumpy-yak", "manager_sid": "mgr-1",
            "orphaned_at": orphaned_at, "source": "session_end", "workers": [],
        }))
        _write_worker(dog, "w1", "grumpy-yak")
        dog.run_tick(NOW)
        # The hook already notified at orphaned_at — no double-notify inside the window.
        assert dog._test_notifications == []
        entry = _state(dog)["grumpy-yak"]
        assert entry["first_seen"] == orphaned_at
        assert entry["last_notified"] == orphaned_at
        assert entry["notify_count"] == 1
        # Past the window the tick takes over.
        dog.run_tick(orphaned_at + dog.RENOTIFY_SEC + 1)
        assert len(dog._test_notifications) == 1


class TestResolution:
    def test_recovered_manager_clears_state_and_flag(self, dog):
        _write_worker(dog, "w1", "grumpy-yak")
        dog.run_tick(NOW)
        assert "grumpy-yak" in _state(dog)
        dog.ORPHANS.mkdir(parents=True, exist_ok=True)
        (dog.ORPHANS / "grumpy-yak.json").write_text("{}")
        _write_manager(dog)                      # manager came back (takeover)
        decision, _ = dog.run_tick(NOW + 60)
        assert decision == "ok"
        assert "grumpy-yak" not in _state(dog)
        assert not (dog.ORPHANS / "grumpy-yak.json").exists()
        assert "orphan_cleared" in [e["event"] for e in _ledger_events(dog)]

    def test_stale_flag_without_state_entry_is_unlinked(self, dog):
        """Important-2: a flag whose stretch resolved before any tick saw it
        orphaned (takeover race, fast /manager-resume) must not leak."""
        dog.ORPHANS.mkdir(parents=True)
        (dog.ORPHANS / "grumpy-yak.json").write_text(json.dumps({
            "manager_name": "grumpy-yak", "orphaned_at": NOW - 60,
            "source": "session_end", "workers": [],
        }))
        _write_manager(dog)
        decision, _ = dog.run_tick(NOW)
        assert decision == "ok"
        assert not (dog.ORPHANS / "grumpy-yak.json").exists()

    def test_corrupt_state_file_treated_as_empty(self, dog):
        dog.BOOTLITE_DIR.mkdir(parents=True)
        dog.STATE_PATH.write_text("{corrupt")
        _write_worker(dog, "w1", "grumpy-yak")
        decision, _ = dog.run_tick(NOW)          # must not raise
        assert decision == "orphans"
        assert "grumpy-yak" in _state(dog)

    def test_shape_corrupt_state_entries_dropped_not_fatal(self, dog):
        """Verifier Important-1: valid JSON, wrong shape ({"yak": 5}) must not
        wedge the tick — and since nothing else repairs state.json, the loader
        is the only repair point."""
        dog.BOOTLITE_DIR.mkdir(parents=True)
        dog.STATE_PATH.write_text(json.dumps({"grumpy-yak": 5, "other": "junk"}))
        _write_worker(dog, "w1", "grumpy-yak")
        decision, _ = dog.run_tick(NOW)          # must not raise
        assert decision == "orphans"
        entry = _state(dog)["grumpy-yak"]        # rebuilt as a fresh dict entry
        assert entry["notify_count"] == 1


class TestAutonudge:
    def test_autonudge_off_by_default(self, dog):
        _write_worker(dog, "w1", "grumpy-yak")
        dog.run_tick(NOW)
        assert dog._test_nudges == []

    def test_autonudge_nudges_once_per_worker_per_stretch(self, dog, monkeypatch):
        monkeypatch.setattr(dog, "AUTONUDGE", True)
        _write_worker(dog, "w1", "grumpy-yak", window_id="w-1")
        _write_worker(dog, "w2", "grumpy-yak", window_id="w-2")
        dog.run_tick(NOW)
        dog.run_tick(NOW + 3600)
        assert sorted(w for w, _ in dog._test_nudges) == ["w-1", "w-2"]
        for _, text in dog._test_nudges:
            assert "worker_done" in text         # checkpoint-and-finish, not "resume your task"
            assert "resume your task" not in text

    def test_autonudge_skips_pending_question_and_missing_window(self, dog, monkeypatch):
        monkeypatch.setattr(dog, "AUTONUDGE", True)
        _write_worker(dog, "w1", "grumpy-yak", window_id="w-1")
        _write_worker(dog, "w2", "grumpy-yak", window_id="w-2")
        _write_worker(dog, "w3", "grumpy-yak", window_id="")
        (dog.QUESTIONS / "grumpy-yak").mkdir(parents=True)
        (dog.QUESTIONS / "grumpy-yak" / "q1.json").write_text(json.dumps({
            "question_id": "q1", "worker_sid": "w1", "asked_at": NOW,
        }))
        dog.run_tick(NOW)
        assert [w for w, _ in dog._test_nudges] == ["w-2"]

    def test_autonudge_without_driver_still_notifies(self, dog, monkeypatch):
        monkeypatch.setattr(dog, "AUTONUDGE", True)
        monkeypatch.setattr(dog, "_resolve_get_driver", lambda: None)
        _write_worker(dog, "w1", "grumpy-yak")
        decision, _ = dog.run_tick(NOW)          # must not raise
        assert decision == "orphans"
        assert len(dog._test_notifications) == 1
        assert dog._test_nudges == []

    def test_vanished_sid_pruned_from_nudged_map(self, dog, monkeypatch):
        monkeypatch.setattr(dog, "AUTONUDGE", True)
        _write_worker(dog, "w1", "grumpy-yak", window_id="w-1")
        _write_worker(dog, "w2", "grumpy-yak", window_id="w-2")
        dog.run_tick(NOW)
        (dog.ACTIVE / "w2.json").unlink()        # w2 session ended
        dog.run_tick(NOW + 3600)
        assert set(_state(dog)["grumpy-yak"]["nudged"]) == {"w1"}


class TestCheckLog:
    def test_every_tick_writes_exactly_one_check_line(self, dog):
        dog.run_tick(NOW)
        _write_worker(dog, "w1", "grumpy-yak")
        dog.run_tick(NOW + 60)
        lines = [l for l in dog.CHECK_LOG_PATH.read_text().splitlines() if l.strip()]
        assert len(lines) == 2
        assert "ok" in lines[0]
        assert "orphans" in lines[1]

    def test_dry_run_writes_nothing(self, dog):
        _write_worker(dog, "w1", "grumpy-yak")
        decision, detail = dog.run_tick(NOW, dry_run=True)
        assert decision == "orphans"
        assert dog._test_notifications == []
        assert _ledger_events(dog) == []
        assert not dog.STATE_PATH.exists()
        assert not dog.CHECK_LOG_PATH.exists()


class TestAutonudgeTmux:
    def test_tmux_nudge_routes_through_driver(self, dog, monkeypatch):
        monkeypatch.setattr(dog, "AUTONUDGE", True)
        sent = []

        class FakeDrv:
            def send_text(self, pane, text):
                sent.append((pane, text))

        monkeypatch.setattr(dog, "_resolve_get_driver", lambda: (lambda: FakeDrv()))
        _write_worker(dog, "w-1", "grumpy-yak", window_id="w-1")
        _write_worker(dog, "w-2", "grumpy-yak", window_id="w-2")
        dog.run_tick(NOW)
        assert sent, "driver send_text was called with pane ids"

    def test_tmux_nudge_skipped_when_driver_missing(self, dog, monkeypatch):
        monkeypatch.setattr(dog, "AUTONUDGE", True)
        monkeypatch.setattr(dog, "_resolve_get_driver", lambda: None)
        _write_worker(dog, "w-1", "grumpy-yak", window_id="w-1")
        _write_worker(dog, "w-2", "grumpy-yak", window_id="w-2")
        decision, _ = dog.run_tick(NOW)
        assert decision == "orphans"     # detection intact
        assert dog._test_nudges == []    # no nudge, no crash


class TestHomeFallback:
    def test_prefers_dockwright_homes(self, tmp_path, monkeypatch):
        claude = tmp_path / ".claude"
        for rel in ("dockwright", "orchestrator", "dockwright/bootlite", "bootlite"):
            (claude / rel).mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        mod = _load_watchdog()
        assert mod.ORCH_ROOT == claude / "dockwright"
        assert mod.BOOTLITE_DIR == claude / "dockwright" / "bootlite"
        assert mod.STOP_PATHS[0] == claude / "dockwright" / "bootlite-stop"

    def test_falls_back_to_legacy_homes(self, tmp_path, monkeypatch):
        claude = tmp_path / ".claude"
        for rel in ("orchestrator", "bootlite"):
            (claude / rel).mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        mod = _load_watchdog()
        assert mod.ORCH_ROOT == claude / "orchestrator"
        assert mod.BOOTLITE_DIR == claude / "bootlite"
