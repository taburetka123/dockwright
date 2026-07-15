"""No test may fire a real desktop notification (osascript) — in-process or not.

2026-07-03 leak: test_module_toggle's subprocess-exec'd gardener_gate.py fired
real 'gardener-gate: hooks_missing …' notifications on every full-suite run.
Two closing layers, each pinned here or next to its module:
  * conftest's no_live_tmux absorber intercepts IN-PROCESS osascript
    subprocess.run calls — a forgotten _notify monkeypatch can't leak.
  * the notify helpers in deployed scripts / hooks no-op under
    PYTEST_CURRENT_TEST (exported by pytest, inherited by {**os.environ}
    child envs) — a subprocess-exec'd script can't leak either.

The absorber-presence asserts run BEFORE any notify call on purpose: if the
absorber regresses, the test fails without detonating a real notification.
"""
import subprocess

from dockwright import hooks


def test_osascript_absorbed_in_process(no_live_tmux):
    assert no_live_tmux.osascript == []
    r = subprocess.run(["osascript", "-e", "return 1"], capture_output=True, text=True)
    assert r.returncode == 0
    assert no_live_tmux.osascript == [["osascript", "-e", "return 1"]]


def test_absolute_path_osascript_absorbed(no_live_tmux):
    assert no_live_tmux.osascript == []
    r = subprocess.run(["/usr/bin/osascript", "-e", "return 1"], capture_output=True)
    assert r.returncode == 0
    assert len(no_live_tmux.osascript) == 1


def test_hooks_notify_suppressed_under_pytest(no_live_tmux):
    """hooks._notify_macos carries its own PYTEST_CURRENT_TEST guard (the
    orchestrator CLI can be exec'd as a subprocess where the absorber can't
    reach): under pytest it must not even attempt the osascript call."""
    assert no_live_tmux.osascript == []
    hooks._notify_macos("boom")
    assert no_live_tmux.osascript == []


def test_hooks_notify_invokes_real_osascript_outside_pytest(monkeypatch):
    """Production-behavior pin: outside pytest the helper really requests a
    'display notification … "orchestrator"' via osascript (argv recorded by a
    stub — nothing executes)."""
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: calls.append([str(x) for x in a[0]]))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    hooks._notify_macos("boom")
    assert len(calls) == 1
    assert calls[0][0] == "osascript"
    assert "display notification" in calls[0][2]
    assert 'with title "orchestrator"' in calls[0][2]
