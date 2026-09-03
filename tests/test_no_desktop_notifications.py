import subprocess

from dockwright import hooks


def test_hooks_notify_suppressed_under_pytest(no_live_tmux):
    assert no_live_tmux.osascript == []
    hooks._notify_macos("boom")
    assert no_live_tmux.osascript == []


def test_hooks_notify_invokes_real_osascript_outside_pytest(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: calls.append([str(x) for x in a[0]]))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    hooks._notify_macos("boom")
    assert len(calls) == 1
    assert calls[0][0] == "osascript"
    assert "display notification" in calls[0][2]
    assert 'with title "dockwright"' in calls[0][2]
