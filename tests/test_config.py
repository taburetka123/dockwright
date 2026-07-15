"""config.py — dockwright.toml loader. Defaults must reproduce every current
hardcode; missing/corrupt file is fail-open to defaults."""
import tomllib
from pathlib import Path

import pytest

from dockwright import config


@pytest.fixture
def no_config(monkeypatch, tmp_path):
    """No dockwright.toml anywhere: env unset, HOME pointed at an empty tree."""
    monkeypatch.delenv(config.ENV_CONFIG_PATH, raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _write(tmp_path, text, name="dockwright.toml"):
    p = tmp_path / name
    p.write_text(text)
    return p


# --- defaults (the zero-regression contract) ---

def test_defaults_without_any_config_file(no_config):
    home = no_config
    assert config.config_path() is None
    assert config.load() == {}
    assert config.load_error() is None
    assert config.state_root() == home / ".claude" / "dockwright"
    assert config.claude_config_home() == home / ".claude"
    assert config.worker_home_default() == home / "projects" / "work" / "worker"
    assert config.manager_memory_root() == home / ".claude" / "dockwright" / "manager-memory"
    assert config.worker_model() == "opus[1m]"
    assert config.manager_model() == "opus[1m]"
    assert config.distill_model() == "claude-sonnet-4-6"
    assert config.assign_command_hint() == "/manager-assign"
    assert config.worktree_cleanup_hint() == ""
    assert config.loop_label_prefix() == "com.dockwright"
    assert config.accounts() == [
        config.Account(name="a", config_dir=None, weight=1),
        config.Account(name="b", config_dir=None, weight=1),
    ]
    assert config.account_names() == ("a", "b")
    assert config.default_account() == "a"
    assert config.account_weight("a") == 1
    assert config.account_weight("nope") == 1
    assert config.account_config_dir_override("b") is None
    assert config.pricing_overrides() == {}
    assert config.gardener_module_enabled() is True
    assert config.spawn_env() == {}
    assert config.task_key_regex() is None
    assert config.dockwright_repo() == ""
    assert config.gardener_high_skills() == ()
    assert config.worktree_roots() == "~/worktrees,~/worktrees-personal"
    assert config.repo_roots() == "~/projects/work,~/projects/personal"


# --- discovery order ---

def test_env_path_wins(no_config, monkeypatch, tmp_path):
    explicit = _write(tmp_path, '[spawn]\nworker_model = "sonnet"\n', "explicit.toml")
    xdg = tmp_path / "xdg" / "dockwright"
    xdg.mkdir(parents=True)
    (xdg / "dockwright.toml").write_text('[spawn]\nworker_model = "haiku"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(explicit))
    assert config.config_path() == explicit
    assert config.worker_model() == "sonnet"


def test_env_path_missing_file_means_no_config(no_config, monkeypatch, tmp_path):
    """An explicit $DOCKWRIGHT_CONFIG pointing nowhere is authoritative: no fallback."""
    xdg = tmp_path / "xdg" / "dockwright"
    xdg.mkdir(parents=True)
    (xdg / "dockwright.toml").write_text('[spawn]\nworker_model = "haiku"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(tmp_path / "missing.toml"))
    assert config.config_path() is None
    assert config.worker_model() == "opus[1m]"


def test_xdg_beats_claude_home(no_config, monkeypatch):
    home = no_config
    xdg = home / ".config" / "dockwright"
    xdg.mkdir(parents=True)
    (xdg / "dockwright.toml").write_text('[spawn]\nworker_model = "xdg"\n')
    claude = home / ".claude"
    claude.mkdir(parents=True)
    (claude / "dockwright.toml").write_text('[spawn]\nworker_model = "claude-home"\n')
    assert config.config_path() == xdg / "dockwright.toml"
    assert config.worker_model() == "xdg"


def test_claude_home_fallback(no_config):
    home = no_config
    claude = home / ".claude"
    claude.mkdir(parents=True)
    (claude / "dockwright.toml").write_text('[spawn]\nworker_model = "claude-home"\n')
    assert config.config_path() == claude / "dockwright.toml"
    assert config.worker_model() == "claude-home"


# --- fail-open ---

def test_corrupt_toml_is_fail_open_but_load_error_reports(no_config, monkeypatch, tmp_path):
    bad = _write(tmp_path, "this is not toml [[[")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(bad))
    assert config.load() == {}
    assert config.load_error() is not None
    assert config.worker_model() == "opus[1m]"
    assert config.accounts() == [
        config.Account(name="a"), config.Account(name="b")]


def test_wrong_types_fall_back_per_key(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, '[paths]\nstate_root = 42\n[spawn]\nworker_model = "custom"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.state_root() == no_config / ".claude" / "dockwright"
    assert config.worker_model() == "custom"


# --- values + expansion ---

def test_path_values_are_tilde_expanded(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, '[paths]\nstate_root = "~/custom/orch"\nworker_home = "~/w"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.state_root() == no_config / "custom" / "orch"
    assert config.worker_home_default() == no_config / "w"


def test_double_slash_after_tilde_stays_under_home(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, '[paths]\nstate_root = "~//custom"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.state_root() == no_config / "custom"
    assert config.state_root() != Path("/custom")


# --- legacy fallback (dockwright-rename, deprecated one release) ---

def test_state_root_default_fresh_home_is_dockwright(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    assert config.state_root() == tmp_path / ".claude" / "dockwright"


def test_state_root_falls_back_to_legacy_when_only_legacy_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    (tmp_path / ".claude" / "orchestrator").mkdir(parents=True)
    assert config.state_root() == tmp_path / ".claude" / "orchestrator"


def test_state_root_prefers_new_when_both_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    (tmp_path / ".claude" / "orchestrator").mkdir(parents=True)
    (tmp_path / ".claude" / "dockwright").mkdir(parents=True)
    assert config.state_root() == tmp_path / ".claude" / "dockwright"


def test_state_root_explicit_toml_is_verbatim_no_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text('[paths]\nstate_root = "~/custom-state"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    assert config.state_root() == tmp_path / "custom-state"


def test_manager_memory_root_default_fresh_home_is_dockwright(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    assert config.manager_memory_root() == tmp_path / ".claude" / "dockwright" / "manager-memory"


def test_manager_memory_root_falls_back_to_legacy_when_only_legacy_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    (tmp_path / ".claude" / "manager-memory").mkdir(parents=True)
    assert config.manager_memory_root() == tmp_path / ".claude" / "manager-memory"


def test_manager_memory_root_prefers_new_when_both_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    (tmp_path / ".claude" / "manager-memory").mkdir(parents=True)
    (tmp_path / ".claude" / "dockwright" / "manager-memory").mkdir(parents=True)
    assert config.manager_memory_root() == tmp_path / ".claude" / "dockwright" / "manager-memory"


def test_manager_memory_root_explicit_toml_is_verbatim_no_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text('[paths]\nmanager_memory = "~/custom-memory"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    assert config.manager_memory_root() == tmp_path / "custom-memory"


def test_overlay_dir_default_fresh_home_is_dockwright(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    assert config.overlay_dir() == tmp_path / ".claude" / "dockwright-overlay"


def test_overlay_dir_falls_back_to_legacy_when_only_legacy_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    (tmp_path / ".claude" / "orchestrator-overlay").mkdir(parents=True)
    assert config.overlay_dir() == tmp_path / ".claude" / "orchestrator-overlay"


def test_overlay_dir_prefers_new_when_both_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    (tmp_path / ".claude" / "orchestrator-overlay").mkdir(parents=True)
    (tmp_path / ".claude" / "dockwright-overlay").mkdir(parents=True)
    assert config.overlay_dir() == tmp_path / ".claude" / "dockwright-overlay"


def test_overlay_dir_explicit_toml_is_verbatim_no_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text('[paths]\noverlay_dir = "~/custom-overlay"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    assert config.overlay_dir() == tmp_path / "custom-overlay"


def test_legacy_state_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert config.legacy_state_root() == tmp_path / ".claude" / "orchestrator"


# --- account registry ---

def test_pool_parses_names_weights_config_dirs(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, '''
[accounts]
default = "main"
[[accounts.pool]]
name = "main"
weight = 3
[[accounts.pool]]
name = "alt"
weight = 2
config_dir = "~/.claude-alt-custom"
''')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.accounts() == [
        config.Account(name="main", config_dir=None, weight=3),
        config.Account(name="alt",
                       config_dir=no_config / ".claude-alt-custom", weight=2),
    ]
    assert config.account_names() == ("main", "alt")
    assert config.default_account() == "main"
    assert config.account_weight("alt") == 2
    assert config.account_config_dir_override("alt") == no_config / ".claude-alt-custom"


def test_default_not_in_pool_falls_back_to_first(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, '''
[accounts]
default = "ghost"
[[accounts.pool]]
name = "x"
[[accounts.pool]]
name = "y"
''')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.default_account() == "x"


@pytest.mark.parametrize("pool_toml", [
    '[[accounts.pool]]\nweight = 1\n',                                  # missing name
    '[[accounts.pool]]\nname = ""\n',                                   # empty name
    '[[accounts.pool]]\nname = "a"\n[[accounts.pool]]\nname = "a"\n',   # dup name
    '[[accounts.pool]]\nname = "a"\nweight = 0\n',                      # weight < 1
    '[[accounts.pool]]\nname = "a"\nweight = "x"\n',                    # weight not int
    '[[accounts.pool]]\nname = "a"\nweight = true\n',                   # bool weight
    '[[accounts.pool]]\nname = "a"\nconfig_dir = 3\n',                  # bad config_dir
])
def test_malformed_pool_falls_back_whole(no_config, monkeypatch, tmp_path, pool_toml):
    p = _write(tmp_path, pool_toml)
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.accounts() == [
        config.Account(name="a"), config.Account(name="b")]
    assert config.default_account() == "a"


# --- pricing overrides ---

def test_pricing_overrides_parse_and_skip_invalid(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, '''
[pricing.rates]
opus = [6.0, 30.0]
sonnet = [3]
haiku = "cheap"
custom = [1, 2]
''')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.pricing_overrides() == {
        "opus": (6.0, 30.0), "custom": (1.0, 2.0)}


# --- DEFAULT_TOML drift guard ---

def test_default_toml_parses_and_matches_code_defaults(no_config, monkeypatch, tmp_path):
    data = tomllib.loads(config.DEFAULT_TOML)
    assert data  # non-empty: the template carries explicit values
    p = _write(tmp_path, config.DEFAULT_TOML)
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    home = no_config
    assert config.state_root() == home / ".claude" / "dockwright"
    assert config.claude_config_home() == home / ".claude"
    assert config.worker_home_default() == home / "projects" / "work" / "worker"
    assert config.manager_memory_root() == home / ".claude" / "dockwright" / "manager-memory"
    assert config.worker_model() == "opus[1m]"
    assert config.manager_model() == "opus[1m]"
    assert config.distill_model() == "claude-sonnet-4-6"
    assert config.assign_command_hint() == "/manager-assign"
    assert config.worktree_cleanup_hint() == ""
    assert config.loop_label_prefix() == "com.dockwright"
    assert config.accounts() == [
        config.Account(name="a", config_dir=None, weight=1),
        config.Account(name="b", config_dir=None, weight=1)]
    assert config.default_account() == "a"
    assert config.pricing_overrides() == {}
    assert config.overlay_dir() == home / ".claude" / "dockwright-overlay"
    assert config.agent_vars() == {}
    assert config.gardener_module_enabled() is True
    assert config.spawn_env() == {}
    assert config.task_key_regex() is None
    assert config.dockwright_repo() == ""
    assert config.gardener_high_skills() == ()
    assert config.worktree_roots() == "~/worktrees,~/worktrees-personal"
    assert config.repo_roots() == "~/projects/work,~/projects/personal"


# --- overlay_dir / agent_vars ---

def test_overlay_dir_default_and_override(no_config, monkeypatch, tmp_path):
    assert config.overlay_dir() == no_config / ".claude" / "dockwright-overlay"
    p = _write(tmp_path, f'[paths]\noverlay_dir = "{tmp_path}/my-overlay"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.overlay_dir() == tmp_path / "my-overlay"


def test_agent_vars_default_empty_and_parse(no_config, monkeypatch, tmp_path):
    assert config.agent_vars() == {}
    p = _write(tmp_path, '[agent_vars]\nticket_regex = "[A-Z]+-1"\nbad = 3\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.agent_vars() == {"ticket_regex": "[A-Z]+-1"}


# --- loop_label_prefix ---

def test_loop_label_prefix_default_and_override(no_config, monkeypatch, tmp_path):
    assert config.loop_label_prefix() == "com.dockwright"
    p = _write(tmp_path, '[loops]\nlabel_prefix = "com.example"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.loop_label_prefix() == "com.example"


def test_loop_label_prefix_wrong_type_falls_back(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, "[loops]\nlabel_prefix = 42\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.loop_label_prefix() == "com.dockwright"


# --- loop_status_overrides ---

def test_loop_status_overrides(monkeypatch, tmp_path):
    p = tmp_path / "d.toml"
    p.write_text('[loops.status_overrides.selffix]\nstatus = "live"\nstatus_why = "op"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(p))
    assert config.loop_status_overrides() == {"selffix": {"status": "live", "status_why": "op"}}


def test_loop_status_overrides_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "none.toml"))
    assert config.loop_status_overrides() == {}


# --- worktree_cleanup_hint (DEFAULT_WORKTREE_CLEANUP flipped to "") ---

def test_worktree_cleanup_hint_default_empty(no_config):
    assert config.worktree_cleanup_hint() == ""


def test_worktree_cleanup_hint_operator_override(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, '[hints]\nworktree_cleanup = "~/bin/my-cleanup --dry-run"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.worktree_cleanup_hint() == "~/bin/my-cleanup --dry-run"


# --- modules.gardener ---

def test_gardener_module_enabled_default_true(no_config):
    assert config.gardener_module_enabled() is True


def test_gardener_module_disabled(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, "[modules]\ngardener = false\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.gardener_module_enabled() is False


def test_gardener_module_wrong_type_falls_back(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, '[modules]\ngardener = "nope"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.gardener_module_enabled() is True


# --- spawn.env ---

def test_spawn_env_default_empty(no_config):
    assert config.spawn_env() == {}


def test_spawn_env_reads_table_str_only(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, '[spawn.env]\nSUPERPOWERS_AUTONOMOUS = "true"\nBAD = 3\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.spawn_env() == {"SUPERPOWERS_AUTONOMOUS": "true"}


# --- task_keys.key_regex ---

def test_task_key_regex_default_none(no_config):
    assert config.task_key_regex() is None


def test_task_key_regex_set(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, "[task_keys]\nkey_regex = '[A-Za-z]{2,}-\\d+'\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.task_key_regex() == "[A-Za-z]{2,}-\\d+"


def test_task_key_regex_empty_means_none(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, '[task_keys]\nkey_regex = ""\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.task_key_regex() is None


def test_task_key_regex_wrong_type_falls_back(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, "[task_keys]\nkey_regex = 42\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.task_key_regex() is None


# --- gardener.high_skills ---

def test_gardener_high_skills_default_empty(no_config):
    assert config.gardener_high_skills() == ()


def test_gardener_high_skills_filters_non_str(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, '[gardener]\nhigh_skills = ["a:b", 3, "c:d"]\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.gardener_high_skills() == ("a:b", "c:d")


def test_gardener_high_skills_wrong_type_falls_back(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, '[gardener]\nhigh_skills = "not-a-list"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.gardener_high_skills() == ()


# --- paths.dockwright_repo / worktree_roots / repo_roots ---

def test_dockwright_repo_default_empty(no_config):
    assert config.dockwright_repo() == ""


def test_dockwright_repo_expands_tilde(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path,
              '[paths]\ndockwright_repo = "~/projects/personal/claude-orchestrator"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.dockwright_repo() == \
        str(no_config / "projects" / "personal" / "claude-orchestrator")


def test_dockwright_repo_wrong_type_falls_back(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, "[paths]\ndockwright_repo = 42\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.dockwright_repo() == ""


def test_worktree_roots_default(no_config):
    assert config.worktree_roots() == "~/worktrees,~/worktrees-personal"


def test_worktree_roots_override(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, '[paths]\nworktree_roots = "~/w1,~/w2"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.worktree_roots() == "~/w1,~/w2"


def test_repo_roots_default(no_config):
    assert config.repo_roots() == "~/projects/work,~/projects/personal"


def test_repo_roots_override(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, '[paths]\nrepo_roots = "~/r1,~/r2"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.repo_roots() == "~/r1,~/r2"
