import os
import sys
import pytest
from dockwright import paths, state, identity, terminal


@pytest.fixture
def fresh_orchestrator_dir(tmp_path, monkeypatch):
    # These tests resolve the manager via TMUX_PANE, which the driver's
    # current_pane_id() reads. Reset the cached driver so each test gets a clean
    # TmuxDriver; tests that exercise the pane path setenv TMUX_PANE explicitly
    # (overriding any value the runner inherits from its own tmux server) and
    # the PPID-walk tests delenv it.
    terminal._DRIVER = None
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "ACTIVE", tmp_path / "active")
    paths.ACTIVE.mkdir(parents=True, exist_ok=True)
    yield tmp_path


def _write_manager(sid: str, name: str, window_id: str = "", pid: int | None = None):
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {
        "claude_sid": sid,
        "agent": "manager",
        "name": name,
        "window_id": window_id,
        "pid": pid or os.getpid(),
        "domain": "general",
    })


def test_resolve_via_pane_id(fresh_orchestrator_dir, monkeypatch):
    _write_manager("mgr-1", "happy-otter", window_id="42")
    monkeypatch.setenv("TMUX_PANE", "42")
    result = identity.resolve_manager()
    assert result == {"name": "happy-otter", "sid": "mgr-1"}


def test_resolve_via_pane_id_handles_legacy_iterm_sid(fresh_orchestrator_dir, monkeypatch):
    """A manager whose record was written before the window_id rename still
    resolves via state.window_id_of (which reads either key)."""
    state.write_json_atomic(paths.ACTIVE / "mgr-old.json", {
        "claude_sid": "mgr-old",
        "agent": "manager",
        "name": "legacy-fox",
        "iterm_sid": "77",  # legacy field
        "pid": os.getpid(),
        "domain": "general",
    })
    monkeypatch.setenv("TMUX_PANE", "77")
    result = identity.resolve_manager()
    assert result == {"name": "legacy-fox", "sid": "mgr-old"}


def test_resolve_via_ppid_walk(fresh_orchestrator_dir, monkeypatch):
    """When TMUX_PANE misses, walk up PPID looking for a manager pid.

    PPID-walk starts from os.getppid() and walks UP — so the manager record's
    pid must be in the test runner's ancestry. os.getppid() is the first hop,
    which is the cheapest deterministic anchor we can rely on in a unit test.
    """
    monkeypatch.delenv("TMUX_PANE", raising=False)
    _write_manager("mgr-2", "calm-bear", pid=os.getppid())
    result = identity.resolve_manager()
    assert result == {"name": "calm-bear", "sid": "mgr-2"}


def test_resolve_via_ppid_walk_multi_hop(fresh_orchestrator_dir, monkeypatch):
    """Production chain is python → shell → claude (≥3 hops). Simulate by
    chaining `_ppid_of` so the manager pid is N hops above os.getppid()."""
    monkeypatch.delenv("TMUX_PANE", raising=False)
    manager_pid = 99999  # sentinel — never matches a real pid in the test runner ancestry
    _write_manager("mgr-deep", "deep-otter", pid=manager_pid)
    chain = {os.getppid(): 11111, 11111: 22222, 22222: manager_pid}
    monkeypatch.setattr("dockwright.identity._ppid_of",
                        lambda pid: chain.get(pid))
    result = identity.resolve_manager()
    assert result == {"name": "deep-otter", "sid": "mgr-deep"}


def test_resolve_fails_loudly_on_no_match(fresh_orchestrator_dir, monkeypatch, capsys):
    """No active manager records at all → SystemExit with helpful stderr."""
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setattr("dockwright.identity._resolve_via_ppid_walk",
                        lambda *a, **k: None)
    with pytest.raises(SystemExit) as excinfo:
        identity.resolve_manager()
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "cannot resolve owning manager" in err
    assert "Active manager records:" in err


def test_resolve_handles_pane_id_collision(fresh_orchestrator_dir, monkeypatch):
    """Two records share the same TMUX_PANE → fall through to PPID-walk."""
    _write_manager("mgr-3a", "alpha", window_id="99")
    _write_manager("mgr-3b", "beta", window_id="99")
    _write_manager("mgr-3c", "owns-pid", window_id="other", pid=os.getppid())
    monkeypatch.setenv("TMUX_PANE", "99")
    result = identity.resolve_manager()
    # Collision on 99 → fallthrough → PPID-walk finds the os.getppid() owner.
    assert result == {"name": "owns-pid", "sid": "mgr-3c"}


def test_resolve_never_returns_nested_manager_record(fresh_orchestrator_dir, monkeypatch):
    """A nested manager-agent ghost (claude -p child of a manager) must never
    resolve as THE manager — even when its pid sits on the walk chain and no
    real manager record exists. Failing loudly beats scoping monitors to a
    ghost."""
    monkeypatch.delenv("TMUX_PANE", raising=False)
    state.write_json_atomic(paths.ACTIVE / "nested-mgr.json", {
        "claude_sid": "nested-mgr", "agent": "manager", "name": "nested-aaaa0000",
        "nested": True, "window_id": "", "pid": os.getppid(), "domain": "general",
    })
    with pytest.raises(SystemExit):
        identity.resolve_manager()
