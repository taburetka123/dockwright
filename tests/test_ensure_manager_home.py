import json

import pytest

from dockwright import config, paths, trust
from dockwright import ensure_manager_home as emh


def test_manager_home_defaults_under_home(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKWRIGHT_MANAGER_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.manager_home() == tmp_path / "projects" / "work" / "manager"


def test_manager_home_reads_paths_section(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKWRIGHT_MANAGER_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text('[paths]\nmanager_home = "~/mh"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    assert paths.manager_home() == tmp_path / "mh"


def test_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(tmp_path / "env-home"))
    assert paths.manager_home() == tmp_path / "env-home"


def test_ensure_manager_home_creates_absent_dir(monkeypatch, tmp_path):
    home = tmp_path / "projects" / "work" / "manager"
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(home))
    assert not home.exists()
    assert paths.ensure_manager_home() == home
    assert home.is_dir()


def test_ensure_manager_home_raises_when_it_cannot_be_created(monkeypatch, tmp_path):
    parent_file = tmp_path / "afile"
    parent_file.write_text("x")
    home = parent_file / "manager"
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(home))
    with pytest.raises(ValueError, match="could not be created"):
        paths.ensure_manager_home()
    assert not home.exists()


def test_ensure_manager_home_raises_when_path_is_a_file(monkeypatch, tmp_path):
    home = tmp_path / "afile"
    home.write_text("x")
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(home))
    with pytest.raises(ValueError, match="could not be created|not a directory"):
        paths.ensure_manager_home()


def test_env_override_expands_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", "~/mh-tilde")
    assert paths.manager_home() == tmp_path / "mh-tilde"
    assert paths.ensure_manager_home() == tmp_path / "mh-tilde"
    assert (tmp_path / "mh-tilde").is_dir()
    with pytest.raises(ValueError):
        paths.refuse_manager_cwd(tmp_path)


def test_cli_fails_loudly_when_home_cannot_be_created(monkeypatch, tmp_path, capsys):
    parent_file = tmp_path / "afile"
    parent_file.write_text("x")
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(parent_file / "manager"))
    monkeypatch.setattr(trust, "_default_config_json", lambda: tmp_path / "cfg.json")
    assert emh.main([]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "ERROR: manager home" in out.err


def test_refuse_manager_cwd_rejects_parent_of_home(monkeypatch, tmp_path):
    home = tmp_path / "work" / "manager"
    home.mkdir(parents=True)
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(home))
    with pytest.raises(ValueError) as err:
        paths.refuse_manager_cwd(tmp_path / "work")
    assert str(home) in str(err.value)
    assert "dockwright manager" in str(err.value)


def test_refuse_manager_cwd_rejects_parent_through_symlink(monkeypatch, tmp_path):
    real = tmp_path / "real-work"
    (real / "manager").mkdir(parents=True)
    link = tmp_path / "work-link"
    link.symlink_to(real)
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(link / "manager"))
    with pytest.raises(ValueError):
        paths.refuse_manager_cwd(real)


@pytest.mark.parametrize("sub", ["manager", "manager/deeper", "worker", "some-repo"])
def test_refuse_manager_cwd_allows_other_dirs(monkeypatch, tmp_path, sub):
    home = tmp_path / "work" / "manager"
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(home))
    paths.refuse_manager_cwd(tmp_path / "work" / sub)
    paths.refuse_manager_cwd(tmp_path)


def test_cli_prints_created_path(monkeypatch, tmp_path, capsys):
    home = tmp_path / "mh-cli"
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(home))
    monkeypatch.setattr(trust, "_default_config_json", lambda: tmp_path / "cfg.json")
    assert emh.main([]) == 0
    assert capsys.readouterr().out.strip() == str(home)
    assert home.is_dir()


def test_cli_pretrusts_manager_home(monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "cfg.json"
    monkeypatch.setattr(trust, "_default_config_json", lambda: cfg)
    home = tmp_path / "mh-trust"
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(home))
    assert emh.main([]) == 0
    assert capsys.readouterr().out.strip() == str(home), \
        "stdout must stay EXACTLY the path — setup.sh and bootstrap-recreate.sh command-substitute it"
    data = json.loads(cfg.read_text())
    assert data["projects"][str(home.resolve())]["hasTrustDialogAccepted"] is True


def test_cli_dispatches_ensure_manager_home(monkeypatch, tmp_path, capsys):
    import sys
    from dockwright import __main__ as cli
    home = tmp_path / "mh-dispatch"
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(home))
    monkeypatch.setattr(trust, "_default_config_json", lambda: tmp_path / "cfg.json")
    monkeypatch.setattr(sys, "argv", ["dockwright", "ensure-manager-home"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == str(home)
