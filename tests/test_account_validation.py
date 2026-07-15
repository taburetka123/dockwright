"""Account-stamp validation follows the config registry (default: a/b)."""
from dockwright import config


def test_default_names(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(tmp_path / "no-config.toml"))
    assert config.account_names() == ("a", "b")


def test_hooks_and_mcp_validate_against_registry():
    """The literal ("a", "b") tuples are gone from the validation sites."""
    import inspect
    from dockwright import hooks, mcp_server
    for mod in (hooks, mcp_server):
        src = inspect.getsource(mod)
        assert 'in ("a", "b")' not in src, mod.__name__


def test_mcp_manager_model_pin_from_config():
    import inspect
    from dockwright import mcp_server
    src = inspect.getsource(mcp_server)
    assert '"--model", "opus[1m]"' not in src
    assert "config.manager_model()" in src
