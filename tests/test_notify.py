import subprocess

from dockwright import notify


def test_macos_is_silent_under_pytest(no_live_tmux):
    assert notify.macos("dockwright", "hello") is None
    assert no_live_tmux.osascript == []


def test_macos_runs_one_osascript_and_returns_its_code(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert notify.macos('dock"wright', 'a "b" \\c') == 0
    argv, kwargs = calls[0]
    assert argv == ["osascript", "-e", 'display notification "a b c" with title "dockwright"']
    assert kwargs["timeout"] == 2 and kwargs["check"] is False and kwargs["capture_output"] is True


def test_macos_reports_a_failure(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, b"", b"denied"))
    assert notify.macos("t", "x") == 1

    def boom(argv, **kw):
        raise OSError("no osascript")

    monkeypatch.setattr(subprocess, "run", boom)
    assert notify.macos("t", "x") == -1
