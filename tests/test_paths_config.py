"""paths.py routes its roots through config (defaults == today)."""
import importlib

import pytest

from dockwright import config, paths


@pytest.fixture
def reloading_paths(monkeypatch):
    """Yield monkeypatch; on teardown UNDO the env patches FIRST, then reload
    paths — a reload under a still-patched HOME would leave paths.* pointed
    at the test tmp dir for every later test in the session."""
    yield monkeypatch
    monkeypatch.undo()
    importlib.reload(paths)


def test_defaults_equal_today(reloading_paths, tmp_path):
    monkeypatch = reloading_paths
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(config.ENV_CONFIG_PATH, raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_HOME", raising=False)
    importlib.reload(paths)
    assert paths.ROOT == tmp_path / ".claude" / "dockwright"
    assert paths.CONFIG_HOME == tmp_path / ".claude"
    assert paths.MANAGER_MEMORY == tmp_path / ".claude" / "dockwright" / "manager-memory"
    assert paths.HOST_CLAUDE_JSON == tmp_path / ".claude.json"
    assert paths.worker_home() == tmp_path / "projects" / "work" / "worker"
    assert paths.account_config_dir("b") == tmp_path / ".claude-b"


def test_config_overrides_state_root_and_worker_home(reloading_paths, tmp_path):
    monkeypatch = reloading_paths
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(
        '[paths]\n'
        f'state_root = "{tmp_path}/orch-state"\n'
        f'worker_home = "{tmp_path}/whome"\n'
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    importlib.reload(paths)
    assert paths.ROOT == tmp_path / "orch-state"
    assert paths.ACTIVE == tmp_path / "orch-state" / "active"
    assert paths.worker_home() == tmp_path / "whome"


def test_worker_home_env_still_wins(monkeypatch, tmp_path):
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(f'[paths]\nworker_home = "{tmp_path}/from-config"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_HOME", str(tmp_path / "from-env"))
    assert paths.worker_home() == tmp_path / "from-env"


def test_account_config_dir_registry_override(monkeypatch, tmp_path):
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(
        '[[accounts.pool]]\nname = "a"\n'
        '[[accounts.pool]]\nname = "b"\n'
        f'config_dir = "{tmp_path}/farm-b"\n'
    )
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    assert paths.account_config_dir("b") == tmp_path / "farm-b"
    assert paths.account_config_dir("c") == paths.CONFIG_HOME.parent / ".claude-c"
