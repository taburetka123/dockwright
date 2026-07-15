import pytest
from dockwright import __main__ as m

@pytest.mark.parametrize("cmd,modattr", [
    ("install-hooks", "env_install"),
    ("clean-homebrew", "homebrew_cleanup"),
    ("doctor", "doctor"),
    ("uninstall", "uninstall"),
    ("migrate-state", "migrate"),
    ("manager", "manager_launch"),
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
