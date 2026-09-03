from dockwright import config


def test_default_names(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(tmp_path / "no-config.toml"))
    assert config.account_names() == ("a",)
