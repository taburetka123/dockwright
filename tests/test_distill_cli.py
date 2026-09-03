import json
import sys

import pytest

from dockwright import distill


def _transcript(tmp_path, monkeypatch, events):
    log = tmp_path / "transcript.jsonl"
    log.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    monkeypatch.setattr(distill, "find_session_log", lambda sid: log)
    return log


def test_distill_cli_exits_0_when_the_session_never_ran(tmp_path, monkeypatch, capsys):
    _transcript(tmp_path, monkeypatch, [
        {"type": "user", "message": {"content": "/manager-takeover-recovery ..."}},
        {"type": "assistant", "isApiErrorMessage": True, "message": {
            "content": [{"type": "text", "text": "Login expired · Please run /login"}]}},
    ])

    def fail_if_called(*a, **kw):
        raise AssertionError("must not attempt to distill a session that never ran")

    monkeypatch.setattr(distill, "_distill_manager_session", fail_if_called)

    assert distill.main(["sid-zombie"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("skipped:"), f"expected a skipped: line, got {out!r}"
    assert "no model turn" in out


def test_distill_cli_still_exits_1_on_a_real_failure(tmp_path, monkeypatch, capsys):
    _transcript(tmp_path, monkeypatch, [
        {"type": "user", "message": {"content": "go"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
    ])
    monkeypatch.setattr(distill, "distill_and_write_memory", lambda sid, domain=None: None)

    assert distill.main(["sid-ran"]) == 1
    assert capsys.readouterr().out == ""


def test_distill_cli_success(monkeypatch, capsys):
    monkeypatch.setattr(distill, "distill_and_write_memory",
                        lambda sid, domain=None: f"/mem/{domain or 'auto'}/{sid}.md")
    assert distill.main(["sid-123", "--domain", "general"]) == 0
    assert "/mem/general/sid-123.md" in capsys.readouterr().out


def test_distill_cli_failure_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(distill, "distill_and_write_memory", lambda sid, domain=None: None)
    assert distill.main(["sid-123"]) == 1
    assert capsys.readouterr().out == ""


def test_distill_cli_requires_sid():
    with pytest.raises(SystemExit):
        distill.main([])


def test_cli_dispatch_wired():
    from dockwright import __main__ as cli
    import dockwright.distill as distill_mod
    called = {}
    orig = distill_mod.main

    def fake_main(argv):
        called["argv"] = argv
        return 0

    try:
        distill_mod.main = fake_main
        sys_argv = sys.argv
        sys.argv = ["orchestrator", "distill", "sid-123", "--domain", "general"]
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        assert called["argv"] == ["sid-123", "--domain", "general"]
    finally:
        distill_mod.main = orig
        sys.argv = sys_argv
