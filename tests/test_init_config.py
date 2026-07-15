"""`orchestrator init` writes the documented-defaults dockwright.toml."""
import tomllib

from dockwright import config, init_config


def test_init_writes_default_toml(tmp_path, capsys):
    target = tmp_path / "dockwright.toml"
    rc = init_config.main(["--path", str(target)])
    assert rc == 0
    assert target.read_text() == config.DEFAULT_TOML
    assert str(target) in capsys.readouterr().out


def test_init_default_path_is_xdg(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    rc = init_config.main([])
    assert rc == 0
    target = tmp_path / "xdg" / "dockwright" / "dockwright.toml"
    assert target.is_file()
    assert tomllib.loads(target.read_text())


def test_init_refuses_overwrite_without_force(tmp_path, capsys):
    target = tmp_path / "dockwright.toml"
    target.write_text("# mine\n")
    rc = init_config.main(["--path", str(target)])
    assert rc == 1
    assert target.read_text() == "# mine\n"
    rc = init_config.main(["--path", str(target), "--force"])
    assert rc == 0
    assert target.read_text() == config.DEFAULT_TOML
