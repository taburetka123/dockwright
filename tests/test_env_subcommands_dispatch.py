import re

import pytest
from dockwright import __main__ as m


def _usage_entries(text):
    """Names enumerated as USAGE entry lines (first column, two-space indent).

    A bare substring check is defeatable: with the `manager` entry line
    deleted, "manager" still appears in USAGE via `[manager-name]`,
    `assign-to-manager`, and a description — so the net anchors on the entry
    column. The shared internal-hooks line is `|`-split into its tokens.
    """
    entries = set()
    for line in text.splitlines():
        match = re.match(r"^  ([a-z][a-z0-9 |\-]*?)(?:\s{2,}|$)", line)
        if not match:
            continue
        for token in match.group(1).split("|"):
            if token.strip():
                entries.add(token.strip())
    return entries


@pytest.mark.parametrize("cmd,modattr", [
    ("install-hooks", "env_install"),
    ("clean-homebrew", "homebrew_cleanup"),
    ("doctor", "doctor"),
    ("uninstall", "uninstall"),
    ("migrate-state", "migrate"),
    ("manager", "manager_launch"),
    ("ensure-worker-home", "ensure_worker_home"),
    ("boot-brief", "boot_brief"),
    ("accounts-sync", "accounts_sync"),
])
def test_dispatch_routes_to_module_main(monkeypatch, cmd, modattr):
    import importlib
    mod = importlib.import_module(f"dockwright.{modattr}")
    called = {}
    def _fake_main(argv):
        called["argv"] = argv
        return 0
    monkeypatch.setattr(mod, "main", _fake_main)
    monkeypatch.setattr(m.sys, "argv", ["orchestrator", cmd, "--x", "1"])
    with pytest.raises(SystemExit) as ei:
        m.main()
    assert ei.value.code == 0
    assert called["argv"] == ["--x", "1"]


@pytest.mark.parametrize("flag", ["--help", "-h", "help"])
def test_help_prints_usage_rc0(monkeypatch, capsys, flag):
    monkeypatch.setattr(m.sys, "argv", ["dockwright", flag])
    with pytest.raises(SystemExit) as ei:
        m.main()
    assert ei.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    entries = _usage_entries(captured.out)
    for sub in ("doctor", "manager", "monitor", "selffix", "gardener", "uninstall"):
        assert sub in entries
    # Internal hook plumbing is enumerated too, under its own group header.
    assert "session-start" in entries
    assert "Internal" in captured.out


def test_bare_invocation_usage_rc2(monkeypatch, capsys):
    monkeypatch.setattr(m.sys, "argv", ["dockwright"])
    with pytest.raises(SystemExit) as ei:
        m.main()
    assert ei.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Usage: dockwright" in captured.err
    assert "doctor" in _usage_entries(captured.err)  # bare usage is enumerated as well


def test_unknown_subcommand_rc2_with_help_hint(monkeypatch, capsys):
    monkeypatch.setattr(m.sys, "argv", ["dockwright", "frobnicate"])
    with pytest.raises(SystemExit) as ei:
        m.main()
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "Unknown subcommand: frobnicate" in err
    assert "--help" in err


def test_usage_enumerates_every_dispatched_subcommand(monkeypatch):
    """Drift net: every `elif cmd == "x"` target must be enumerated as its own
    USAGE entry LINE — see _usage_entries for why substring matching is not
    enough."""
    import inspect
    src = inspect.getsource(m)
    subs = re.findall(r'cmd == "([a-z-]+)"', src)
    assert len(subs) >= 20, f"dispatcher parse broke: {subs}"
    entries = _usage_entries(m.USAGE)
    for sub in subs:
        assert sub in entries, f"subcommand {sub!r} has no USAGE entry line"
