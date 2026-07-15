import sys

import pytest

from dockwright import distill


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
