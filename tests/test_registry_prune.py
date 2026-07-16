"""Prune pane-liveness gate: a dead-pid record whose tmux pane is alive is NOT
stale (the Linux transient-$PPID shape records a pid that dies while the
session lives on)."""
import json

import pytest

from dockwright import paths, registry, state


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(paths, "QUESTIONS", tmp_path / "questions")
    monkeypatch.setattr(paths, "SPEND_LEDGER", tmp_path / "spend-ledger.jsonl")
    (tmp_path / "active").mkdir()
    # Every pid is dead unless a test overrides.
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: False)
    return tmp_path


def _write_active(reg_dir, sid="s1", **overrides):
    record = {"claude_sid": sid, "agent": "worker", "name": "alpha",
              "pid": 4242, "window_id": "%7"}
    record.update(overrides)
    state.write_json_atomic(reg_dir / "active" / f"{sid}.json", record)
    return reg_dir / "active" / f"{sid}.json"


def test_dead_pid_live_pane_is_kept(reg, monkeypatch):
    monkeypatch.setattr(registry, "_live_pane_ids", lambda: {"%7"})
    path = _write_active(reg)
    registry._prune_stale_active_records()
    assert path.exists()
    assert not (reg / "spend-ledger.jsonl").exists()


def test_dead_pid_absent_pane_is_reaped_with_forensic_line(reg, monkeypatch):
    monkeypatch.setattr(registry, "_live_pane_ids", lambda: set())
    path = _write_active(reg)
    registry._prune_stale_active_records()
    assert not path.exists()
    entry = json.loads((reg / "spend-ledger.jsonl").read_text())
    assert entry["source"] == "prune"
    assert entry["spend"] == {}


def test_dead_pid_tmux_unanswerable_is_kept(reg, monkeypatch):
    # None = error/timeout, distinct from "no server" (empty set): liveness is
    # unknowable, and deletion is irreversible — defer to the next prune.
    monkeypatch.setattr(registry, "_live_pane_ids", lambda: None)
    path = _write_active(reg)
    registry._prune_stale_active_records()
    assert path.exists()


def test_dead_pid_no_window_reaps_without_pane_fetch(reg, monkeypatch):
    def _boom():
        raise AssertionError("pane set must not be fetched for windowless records")
    monkeypatch.setattr(registry, "_live_pane_ids", _boom)
    path = _write_active(reg, window_id="")
    registry._prune_stale_active_records()
    assert not path.exists()


def test_legacy_iterm_sid_key_protects_too(reg, monkeypatch):
    monkeypatch.setattr(registry, "_live_pane_ids", lambda: {"%9"})
    record = {"claude_sid": "s2", "agent": "worker", "name": "beta",
              "pid": 4242, "iterm_sid": "%9"}
    state.write_json_atomic(reg / "active" / "s2.json", record)
    registry._prune_stale_active_records()
    assert (reg / "active" / "s2.json").exists()


def test_live_pid_never_fetches_panes(reg, monkeypatch):
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: True)
    def _boom():
        raise AssertionError("pane set must not be fetched for live pids")
    monkeypatch.setattr(registry, "_live_pane_ids", _boom)
    path = _write_active(reg)
    registry._prune_stale_active_records()
    assert path.exists()


def test_live_pane_ids_extracts_ids_from_driver_shape(monkeypatch):
    shape = [{"wm_class": "claude-workers",
              "tabs": [{"title": "w", "windows": [{"id": "%3"}, {"id": "%4"}]}]},
             {"wm_class": "mgr",
              "tabs": [{"title": "m", "windows": [{"id": "%0"}]}]}]
    from dockwright import terminal
    import types
    monkeypatch.setattr(terminal, "get_driver",
                        lambda: types.SimpleNamespace(ls=lambda: shape))
    assert registry._live_pane_ids() == {"%3", "%4", "%0"}


def test_live_pane_ids_error_reads_as_none(monkeypatch):
    from dockwright import terminal
    import types
    monkeypatch.setattr(terminal, "get_driver",
                        lambda: types.SimpleNamespace(ls=lambda: None))
    assert registry._live_pane_ids() is None
