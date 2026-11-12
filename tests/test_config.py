import os
import tomllib
from pathlib import Path

import pytest

from dockwright import config, models

_REAL_ROLES_DIR = config.roles_dir

@pytest.fixture
def no_config(monkeypatch, tmp_path):
    monkeypatch.delenv(config.ENV_CONFIG_PATH, raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _write(tmp_path, text, name="dockwright.toml"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_pause_defaults_without_a_section(no_config):
    assert config.pause_stop_files() == ["daemon-stop", "chat-archive-stop", "worktree-prune-stop", "bootlite-stop"]
    assert config.pause_launchd_disable() == []
    assert config.pause_hold() == {}


def test_pause_section_is_read_per_key(no_config, monkeypatch, tmp_path):
    cfg = tmp_path / "d.toml"
    cfg.write_text('[pause]\nstop_files = ["daemon-stop", "/abs/x-stop"]\nlaunchd_disable = ["com.example.poller"]\n'
                   '[pause.hold]\n"lane-a-stop" = "/start-a {lane-a-flag:--go}"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    assert config.pause_stop_files() == ["daemon-stop", "/abs/x-stop"]
    assert config.pause_launchd_disable() == ["com.example.poller"]
    assert config.pause_hold() == {"lane-a-stop": "/start-a {lane-a-flag:--go}"}
    assert config.pause_errors() == []
    cfg.write_text('[pause]\nstop_files = "x"\nlaunchd_disable = [1]\n[pause.hold]\n"lane-a-stop" = ["/start-a"]\n'
                   '"lane-b-stop" = "/start-b"\n')
    assert config.pause_hold() == {"lane-b-stop": "/start-b"}
    assert config.pause_errors() == ["[pause] stop_files must be a list of strings",
                                     "[pause] launchd_disable must be a list of strings",
                                     '[pause.hold] "lane-a-stop" must be a string']
    cfg.write_text('[pause\n')
    assert config.pause_errors()[0].startswith("dockwright.toml does not parse: ")
    cfg.write_text('[pause]\nlaunchd_disabled = ["x"]\n')
    assert config.pause_errors() == ["[pause] launchd_disabled is not a key; the keys are stop_files, launchd_disable, hold"]
    cfg.write_text('pause = 1\n')
    assert config.pause_errors() == ["[pause] must be a table"]


def test_defaults_without_any_config_file(no_config):
    home = no_config
    assert config.config_path() is None
    assert config.load() == {}
    assert config.load_error() is None
    assert config.state_root() == home / ".claude" / "dockwright"
    assert config.claude_config_home() == home / ".claude"
    assert config.worker_home_default() == home / "projects" / "work" / "worker"
    assert config.manager_home_default() == home / "projects" / "work" / "manager"
    assert config.manager_memory_root() == home / ".claude" / "dockwright" / "manager-memory"
    assert config.manager_model() == models.families()["fable"] + "[1m]"
    assert config.distill_model() == "claude-sonnet-4-6"
    assert config.assign_command_hint() == "/manager-assign"
    assert config.worktree_cleanup_hint() == ""
    assert config.loop_label_prefix() == "com.dockwright"
    assert config.accounts() == [
        config.Account(name="a", config_dir=None, weight=1),
    ]
    assert config.account_names() == ("a",)
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


def test_env_path_wins(no_config, monkeypatch, tmp_path):
    explicit = _write(tmp_path, '[spawn]\nmanager_model = "sonnet"\n', "explicit.toml")
    xdg = tmp_path / "xdg" / "dockwright"
    xdg.mkdir(parents=True)
    (xdg / "dockwright.toml").write_text('[spawn]\nmanager_model = "haiku"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(explicit))
    assert config.config_path() == explicit
    assert config.manager_model() == "sonnet"


def test_env_path_missing_file_means_no_config(no_config, monkeypatch, tmp_path):
    xdg = tmp_path / "xdg" / "dockwright"
    xdg.mkdir(parents=True)
    (xdg / "dockwright.toml").write_text('[spawn]\nmanager_model = "haiku"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(tmp_path / "missing.toml"))
    assert config.config_path() is None


def test_xdg_beats_claude_home(no_config, monkeypatch):
    home = no_config
    xdg = home / ".config" / "dockwright"
    xdg.mkdir(parents=True)
    (xdg / "dockwright.toml").write_text('[spawn]\nmanager_model = "xdg"\n')
    claude = home / ".claude"
    claude.mkdir(parents=True)
    (claude / "dockwright.toml").write_text('[spawn]\nmanager_model = "claude-home"\n')
    assert config.config_path() == xdg / "dockwright.toml"
    assert config.manager_model() == "xdg"


def test_claude_home_fallback(no_config):
    home = no_config
    claude = home / ".claude"
    claude.mkdir(parents=True)
    (claude / "dockwright.toml").write_text('[spawn]\nmanager_model = "claude-home"\n')
    assert config.config_path() == claude / "dockwright.toml"
    assert config.manager_model() == "claude-home"


def test_corrupt_toml_is_fail_open_but_load_error_reports(no_config, monkeypatch, tmp_path):
    bad = _write(tmp_path, "this is not toml [[[")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(bad))
    assert config.load() == {}
    assert config.load_error() is not None
    assert config.accounts() == [config.Account(name="a")]


def test_wrong_types_fall_back_per_key(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, '[paths]\nstate_root = 42\n[spawn]\nmanager_model = "custom"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.state_root() == no_config / ".claude" / "dockwright"
    assert config.manager_model() == "custom"


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


def test_pipeline_node_rules_is_unset_by_default_and_expands_the_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    assert config.pipeline_node_rules() is None
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text('[pipeline]\nnode_rules = "~/flows/pipeline-node.md"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    assert config.pipeline_node_rules() == tmp_path / "flows" / "pipeline-node.md"
    cfg.write_text('[pipeline]\nnode_rules = "  "\n')
    assert config.pipeline_node_rules() is None


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
    '[[accounts.pool]]\nweight = 1\n',
    '[[accounts.pool]]\nname = ""\n',
    '[[accounts.pool]]\nname = "a"\n[[accounts.pool]]\nname = "a"\n',
    '[[accounts.pool]]\nname = "a"\nweight = 0\n',
    '[[accounts.pool]]\nname = "a"\nweight = "x"\n',
    '[[accounts.pool]]\nname = "a"\nweight = true\n',
    '[[accounts.pool]]\nname = "a"\nconfig_dir = 3\n',
])
def test_malformed_pool_falls_back_whole(no_config, monkeypatch, tmp_path, pool_toml):
    p = _write(tmp_path, pool_toml)
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.accounts() == [config.Account(name="a")]
    assert config.default_account() == "a"


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


def test_default_toml_parses_and_matches_code_defaults(no_config, monkeypatch, tmp_path):
    data = tomllib.loads(config.DEFAULT_TOML)
    assert data
    p = _write(tmp_path, config.DEFAULT_TOML)
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    home = no_config
    assert config.state_root() == home / ".claude" / "dockwright"
    assert config.claude_config_home() == home / ".claude"
    assert config.worker_home_default() == home / "projects" / "work" / "worker"
    assert config.manager_home_default() == home / "projects" / "work" / "manager"
    assert config.manager_memory_root() == home / ".claude" / "dockwright" / "manager-memory"
    assert config.manager_model() == models.families()["fable"] + "[1m]"
    assert config.manager_effort() == "medium"
    assert config.distill_model() == "claude-sonnet-4-6"
    assert config.worker_headless_preset() is True
    assert config.assign_command_hint() == "/manager-assign"
    assert config.worktree_cleanup_hint() == ""
    assert config.loop_label_prefix() == "com.dockwright"
    assert config.accounts() == [
        config.Account(name="a", config_dir=None, weight=1)]
    assert config.default_account() == "a"
    assert config.pricing_overrides() == {}
    assert "events" not in data
    assert config.overlay_dir() == home / ".claude" / "dockwright-overlay"
    assert config.pipeline_schemas_dir() == home / ".claude" / "dockwright-schemas"
    assert config.agent_vars() == {}
    assert config.tech_debt_systems() == {}
    assert config.gardener_module_enabled() is True
    assert config.spawn_env() == {}
    assert config.task_key_regex() is None
    assert config.ticket_url() is None
    assert config.dockwright_repo() == ""
    assert config.gardener_high_skills() == ()
    assert config.worktree_roots() == "~/worktrees,~/worktrees-personal"
    assert config.repo_roots() == "~/projects/work,~/projects/personal"
    assert config.subworkers_max_depth() == 3
    assert config.mcp_oauth_required() == []
    assert config.daemon_enabled() is False
    assert config.daemon_push() == "macos"
    assert config.daemon_int("push_after_sec", config.DEFAULT_DAEMON_PUSH_AFTER_SEC) == 180
    assert config.daemon_int("push_cap_per_hour", config.DEFAULT_DAEMON_PUSH_CAP_PER_HOUR) == 6
    assert config.lanes_in_manager() is True
    assert config.lanes_env() == {}
    assert config.gh_configured() is False and config.gh_binary() == "gh" and config.gh_logins() == {}
    assert config.schedules() == {}
    assert config.codex_allowed_roots() == ()
    assert config.subworkers_default_provider() == "codex-exec"
    assert config.ask_parent_wait_sec("codex-exec") == 240
    assert config.ask_parent_wait_sec("codex-tui") == 1500
    assert config.provider_model("codex-exec") is None


def test_events_defaults_when_the_section_is_absent(no_config):
    assert config.events_enabled() is False
    assert config.events_env_file() == Path("~/.config/dockwright/events.env").expanduser()
    assert config.events_handler_path() == [config.claude_config_home() / "scripts"]
    assert config.events_handlers() == []
    assert config.events_source("tracker") == {"every_sec": 300, "workflows": [], "job_match": "", "job_skip": "",
                                               **GITHUB_DEFAULTS, "apps": [], **SPRINT_DEFAULTS}
    assert config.events_source("deploy")["every_sec"] == 60 and config.events_source("time")["every_sec"] == 60
    assert config.events_source("github")["every_sec"] == 600
    assert config.events_rules() == {}

def test_events_retention_and_sent_tools_default_when_absent(no_config):
    assert config.events_retention_days() == 14
    assert config.events_sent() == {"tools": []}
    assert config.chat_archive()["sent_footer"] == ""


def test_events_retention_sent_tools_and_sent_footer_read_from_the_file(tmp_path, monkeypatch):
    p = _write(tmp_path, '[events]\nretention_days = 3\n[events.sent]\ntools = ["mcp__chat__send_message"]\n'
                         '[chat_archive]\nsent_footer = "*Sent using* <@U01APPAPP|SomeApp>"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.events_retention_days() == 3
    assert config.events_sent()["tools"] == ["mcp__chat__send_message"]
    assert config.chat_archive()["sent_footer"] == "*Sent using* <@U01APPAPP|SomeApp>"


@pytest.mark.parametrize("raw", ["0", "-1", '"7"', "true"])
def test_retention_days_that_is_not_a_positive_int_falls_back_to_fourteen(tmp_path, monkeypatch, raw):
    p = _write(tmp_path, f"[events]\nretention_days = {raw}\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.events_retention_days() == 14


def test_a_reply_rule_is_accepted_and_reads_unless_rules(tmp_path, monkeypatch):
    p = _write(tmp_path, '[events.rules.a]\nsource = "chat"\nkind = "dm-thread"\naction = "reply"\nunless_rules = ["b"]\n'
                         '[events.rules.b]\nsource = "chat"\nkind = "dm-thread"\naction = "queue"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    rules = config.events_rules()
    assert rules["a"]["errors"] == [] and rules["a"]["unless_rules"] == ["b"]
    assert rules["b"]["unless_rules"] == []


def test_unless_rules_on_a_rule_that_is_not_reply_is_an_error(tmp_path, monkeypatch):
    p = _write(tmp_path, '[events.rules.a]\nsource = "chat"\nkind = "dm"\naction = "queue"\nunless_rules = ["b"]\n'
                         '[events.rules.b]\nsource = "chat"\nkind = "dm"\naction = "queue"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.events_rules()["a"]["errors"] == ['unless_rules needs action = "reply"']


def test_unless_rules_naming_an_unknown_rule_is_an_error(tmp_path, monkeypatch):
    p = _write(tmp_path, '[events.rules.a]\nsource = "chat"\nkind = "dm"\naction = "reply"\nunless_rules = ["zzz"]\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert "unless_rules names an unknown rule zzz" in config.events_rules()["a"]["errors"]


def test_a_reply_rule_reads_ack_sec_inbound_and_inbound_skip_senders(tmp_path, monkeypatch):
    p = _write(tmp_path, '[events.rules.a]\nsource = "chat"\nkind = "dm"\naction = "reply"\nack_sec = 120\n'
                         'inbound = true\ninbound_skip_senders = ["U1"]\n'
                         '[events.rules.b]\nsource = "chat"\nkind = "dm-thread"\naction = "reply"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    rules = config.events_rules()
    assert rules["a"]["errors"] == []
    assert (rules["a"]["ack_sec"], rules["a"]["inbound"], rules["a"]["inbound_skip_senders"]) == (120, True, ["U1"])
    assert (rules["b"]["ack_sec"], rules["b"]["inbound"], rules["b"]["inbound_skip_senders"]) == (600, False, [])


@pytest.mark.parametrize("body, error", [
    ('kind = "dm"\naction = "queue"\nack_sec = 120\n', 'ack_sec needs action = "reply"'),
    ('kind = "dm"\naction = "queue"\ninbound = true\n', 'inbound needs action = "reply"'),
    ('kind = "channel-thread"\naction = "reply"\ninbound = true\n', 'inbound needs kind = "dm" or "dm-thread"'),
    ('kind = "dm"\naction = "reply"\ninbound = "yes"\n', "inbound must be true or false"),
    ('kind = "dm"\naction = "reply"\ninbound_skip_senders = ["U1"]\n', "inbound_skip_senders needs inbound = true")])
def test_the_reply_keys_on_the_wrong_rule_are_errors(tmp_path, monkeypatch, body, error):
    p = _write(tmp_path, '[events.rules.a]\nsource = "chat"\n' + body)
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert error in config.events_rules()["a"]["errors"]



GITHUB_DEFAULTS = {"owners": [], "review_after_hours": 8, "approval_after_hours": 1, "repeat_hours": 8,
                   "work_hours": "09:00-17:00", "tz": "", "merged_window_days": 14, "envs": ["prod"],
                   "deployed_envs": ["prod"], "fallback_envs": [], "fallback_skipped_job": "",
                   "ignore_reviewers": [], "commands": []}
SPRINT_DEFAULTS = {"sprint_every_sec": 1200}


def test_events_source_github_reads_its_keys_and_falls_back_on_bad_values(tmp_path, monkeypatch):
    p = _write(tmp_path, '[events.sources.github]\nowners = ["o"]\nreview_after_hours = 4.5\napproval_after_hours = 0\n'
               'repeat_hours = true\nwork_hours = "9-17"\ntz = "Etc/GMT-2"\nmerged_window_days = 7\n'
               'envs = ["prod", "prod-fallback"]\nignore_reviewers = ["bot1"]\n'
               'fallback_envs = ["prod-fallback"]\nfallback_skipped_job = "deploy-prod"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    src = config.events_source("github")
    assert src["fallback_envs"] == ["prod-fallback"] and src["fallback_skipped_job"] == "deploy-prod"
    assert src["owners"] == ["o"] and src["review_after_hours"] == 4.5
    assert src["approval_after_hours"] == 1 and src["repeat_hours"] == 8
    assert src["work_hours"] == "09:00-17:00" and src["tz"] == "Etc/GMT-2"
    assert src["merged_window_days"] == 7 and src["envs"] == ["prod", "prod-fallback"]
    assert src["ignore_reviewers"] == ["bot1"]


def test_events_source_chat_reads_apps_and_defaults_its_cadence_to_30(tmp_path, monkeypatch):
    p = _write(tmp_path, '[events.sources.chat]\napps = ["com.example.chat"]\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.events_source("chat")["apps"] == ["com.example.chat"]
    assert config.events_source("chat")["every_sec"] == 30


def test_events_source_exec_normalizes_commands_and_keeps_a_malformed_entry_with_its_error(tmp_path, monkeypatch):
    p = _write(tmp_path, '[events.sources.exec]\n[[events.sources.exec.commands]]\nargv = ["a", "--json"]\n'
               'every_sec = 3600\nwhile = { kind = "shift" }\nstop_file = "~/s"\n'
               '[[events.sources.exec.commands]]\nargv = ["b"]\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    first, second = config.events_source("exec")["commands"]
    assert first == {"argv": ["a", "--json"], "every_sec": 3600, "while": {"kind": "shift", "match": {}},
                     "stop_file": os.path.expanduser("~/s"), "error": None, "selector": "a --json"}
    assert second["every_sec"] == 120 and second["while"] is None and second["stop_file"] is None
    assert second["error"] is None and second["selector"] == "b"
    p = _write(tmp_path, '[events.sources.exec]\ncommands = ["a --json"]\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    only, = config.events_source("exec")["commands"]
    assert only["argv"] == [] and only["selector"] == "commands[0]"
    assert only["error"] == "argv must be a list of non-empty strings"
    p = _write(tmp_path, '[events.sources.exec]\n[[events.sources.exec.commands]]\nargv = ["a", 1]\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.events_source("exec")["commands"][0]["error"] == "argv must be a list of non-empty strings"
    p = _write(tmp_path, '[events.sources.exec]\n[[events.sources.exec.commands]]\nargv = ["x"]\n'
               'while = { match = { k = "v" } }\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.events_source("exec")["commands"][0]["error"] == "while must be a table with a string kind"
    p = _write(tmp_path, '[events.sources.exec]\nevery_sec = 60\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.events_source("exec")["commands"] == [] and config.events_source("exec")["every_sec"] == 60


def test_chat_archive_model_feeds_scout_model_and_falls_back_on_the_pin(tmp_path, monkeypatch, model_pins):
    p = _write(tmp_path, '[events.sources.chat]\nscout_retry_sec = 0\n[chat_archive]\nmodel = "claude-haiku-7"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert "scout_retry_sec" not in config.events_source("chat")
    assert config.scout_model() == "claude-haiku-7"
    p = _write(tmp_path, '[chat_archive]\nmodel = ""\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.scout_model() == "@haiku"


@pytest.mark.parametrize("work_hours", ["09:00-24:00", "17:00-09:00", "09:60-17:00", "09:00-09:00"])
def test_a_work_hours_window_that_is_not_a_clock_span_falls_back(tmp_path, monkeypatch, work_hours):
    p = _write(tmp_path, f'[events.sources.github]\nwork_hours = "{work_hours}"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.events_source("github")["work_hours"] == "09:00-17:00"


def test_an_unknown_tz_falls_back_to_the_machine_zone(tmp_path, monkeypatch):
    p = _write(tmp_path, '[events.sources.github]\ntz = "Nowhere/Nothing"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.events_source("github")["tz"] == ""
    p = _write(tmp_path, '[events.sources.github]\ntz = "Etc/GMT+5"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.events_source("github")["tz"] == "Etc/GMT+5"


def test_deployed_envs_defaults_to_envs(tmp_path, monkeypatch):
    p = _write(tmp_path, '[events.sources.github]\nenvs = ["prod", "prod-fallback"]\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.events_source("github")["deployed_envs"] == ["prod", "prod-fallback"]
    p = _write(tmp_path, '[events.sources.github]\nenvs = ["prod", "prod-fallback"]\ndeployed_envs = []\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.events_source("github")["deployed_envs"] == ["prod", "prod-fallback"]
    p = _write(tmp_path, '[events.sources.github]\nenvs = ["prod"]\ndeployed_envs = ["prod", "prod-fallback"]\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.events_source("github")["deployed_envs"] == ["prod", "prod-fallback"]


def test_events_section_reads_sources_handlers_and_rules(tmp_path, monkeypatch):
    p = _write(tmp_path, '[events]\nenabled = true\nenv_file = "/e"\nhandler_path = ["/h"]\nhandlers = ["author-wait"]\n'
               '[events.sources.deploy]\nevery_sec = 30\nworkflows = ["W"]\njob_match = "Deploy:"\njob_skip = "prod"\n'
               '[events.rules.sprint]\nsource = "tracker"\nkind = "sprint-assigned"\naction = "start"\nschema = "ticket"\n'
               'domain = "general"\nmax_open = 2\ntypes = ["Story"]\nskip_status = ["Blocked"]\nenabled = false\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.events_enabled() is True and config.events_env_file() == Path("/e")
    assert config.events_handler_path() == [Path("/h")] and config.events_handlers() == ["author-wait"]
    assert config.events_source("deploy") == {"every_sec": 30, "workflows": ["W"], "job_match": "Deploy:", "job_skip": "prod",
                                              **GITHUB_DEFAULTS, "apps": [], **SPRINT_DEFAULTS}
    rule = config.events_rules()["sprint"]
    assert (rule["source"], rule["kind"], rule["action"], rule["schema"], rule["domain"], rule["max_open"],
            rule["types"], rule["skip_status"], rule["enabled"], rule["errors"]) == \
        ("tracker", "sprint-assigned", "start", "ticket", "general", 2, ["Story"], ["Blocked"], False, [])


def test_a_rule_without_source_kind_or_schema_lists_its_errors(tmp_path, monkeypatch):
    p = _write(tmp_path, '[events.rules.x]\naction = "start"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert sorted(config.events_rules()["x"]["errors"]) == ["kind is required", "schema is required", "source is required"]


def test_a_rule_with_an_unknown_action_lists_the_error(tmp_path, monkeypatch):
    p = _write(tmp_path, '[events.rules.x]\nsource = "tracker"\nkind = "comment"\naction = "dance"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.events_rules()["x"]["errors"] == ["action must be start, page, queue or reply"]


def test_a_rule_reads_queue_match_stop_file_nudge_and_on_change_keys(tmp_path, monkeypatch):
    base = '[events.rules.x]\nsource = "chat"\nkind = "channel"\naction = "queue"\n'

    def rule(extra=""):
        p = _write(tmp_path, base + extra)
        monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
        return config.events_rules()["x"]

    assert rule()["errors"] == []
    assert rule('match = "x"\n')["errors"] == ["match must be a table of strings"]
    assert rule('match = { a = 1 }\n')["errors"] == ["match must be a table of strings"]
    assert rule("stop_file = 3\n")["errors"] == ["stop_file must be a path"]
    assert rule("on_change = 5\n")["errors"] == ["on_change must be a string"]
    p = _write(tmp_path, base.replace('action = "queue"', 'action = "page"') + 'on_change = "state"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.events_rules()["x"]["errors"] == ['on_change needs action = "queue"']
    full = rule('match = { conversation = "#x" }\nstop_file = "~/s"\nnudge_sec = 30\non_change = "state"\n'
                'repeat_sec = 3600\nrepeat_values = ["failed"]\nconfirm_events = 2\n')
    assert full["match"] == {"conversation": "#x"} and full["stop_file"] == os.path.expanduser("~/s")
    assert full["nudge_sec"] == 30 and full["on_change"] == "state" and full["repeat_sec"] == 3600
    assert full["repeat_values"] == ["failed"] and full["confirm_events"] == 2 and full["errors"] == []
    bare = rule()
    assert bare["match"] == {} and bare["stop_file"] is None and bare["nudge_sec"] == 600
    assert bare["on_change"] is None and bare["repeat_sec"] is None and bare["repeat_values"] == []
    assert bare["confirm_events"] == 1
    assert rule("confirm_events = 2\n")["errors"] == ["confirm_events needs on_change"]


def test_a_rule_reads_while_like_an_exec_command(tmp_path, monkeypatch):
    base = '[events.rules.x]\nsource = "chat"\nkind = "channel"\naction = "queue"\n'

    def rule(extra=""):
        p = _write(tmp_path, base + extra)
        monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
        return config.events_rules()["x"]

    gated = rule('while = { kind = "shift", match = { user = "U1" } }\n')
    assert gated["while"] == {"kind": "shift", "match": {"user": "U1"}} and gated["errors"] == []
    assert rule()["while"] is None
    assert rule("while = 3\n")["errors"] == ["while must be a table with a string kind"]
    assert rule("while = { match = {} }\n")["errors"] == ["while must be a table with a string kind"]


def test_codex_allowed_roots_expands_and_resolves(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, '[codex]\nallowed_roots = ["~/worktrees", "/tmp"]\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    roots = config.codex_allowed_roots()
    assert roots[0] == (no_config / "worktrees").resolve()
    assert roots[1] == Path("/tmp").resolve()


def test_codex_allowed_roots_absent_or_malformed_is_empty(no_config, monkeypatch, tmp_path):
    assert config.codex_allowed_roots() == ()
    for body in ('[codex]\nallowed_roots = "oops"\n', '[codex]\nallowed_roots = [1]\n', '[codex]\nallowed_roots = [""]\n'):
        p = _write(tmp_path, body)
        monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
        assert config.codex_allowed_roots() == ()


def test_provider_settings(no_config, monkeypatch, tmp_path):
    assert config.subworkers_default_provider() == "codex-exec"
    assert config.ask_parent_wait_sec("codex-exec") == 240
    assert config.ask_parent_wait_sec("claude-tui") is None
    assert config.ask_parent_wait_sec(None) is None
    p = _write(tmp_path, '[subworkers]\ndefault_provider = "claude-exec"\n'
                         '[subworkers.providers.codex-exec]\nmodel = "gpt-5.5"\nask_parent_wait_sec = 1500\n'
                         '[subworkers.providers.claude-exec]\nask_parent_wait_sec = true\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.subworkers_default_provider() == "claude-exec"
    assert config.provider_model("codex-exec") == "gpt-5.5"
    assert config.ask_parent_wait_sec("codex-exec") == 1500
    assert config.ask_parent_wait_sec("claude-exec") is None


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


def test_tech_debt_systems_default_empty_and_parse(no_config, monkeypatch, tmp_path):
    assert config.tech_debt_systems() == {}
    p = _write(tmp_path, "[tech_debt]\nsystems = 3\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.tech_debt_systems() == {}
    p = _write(tmp_path, '[tech_debt.systems]\nengine = "the engine"\nbad = 3\n', name="b.toml")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.tech_debt_systems() == {"engine": "the engine"}


def test_loop_label_prefix_default_and_override(no_config, monkeypatch, tmp_path):
    assert config.loop_label_prefix() == "com.dockwright"
    p = _write(tmp_path, '[loops]\nlabel_prefix = "com.example"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.loop_label_prefix() == "com.example"


def test_loop_label_prefix_wrong_type_falls_back(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, "[loops]\nlabel_prefix = 42\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.loop_label_prefix() == "com.dockwright"


def test_loop_status_overrides(monkeypatch, tmp_path):
    p = tmp_path / "d.toml"
    p.write_text('[loops.status_overrides.selffix]\nstatus = "live"\nstatus_why = "op"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(p))
    assert config.loop_status_overrides() == {"selffix": {"status": "live", "status_why": "op"}}


def test_loop_status_overrides_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "none.toml"))
    assert config.loop_status_overrides() == {}


def test_worktree_cleanup_hint_default_empty(no_config):
    assert config.worktree_cleanup_hint() == ""


def test_worktree_cleanup_hint_operator_override(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, '[hints]\nworktree_cleanup = "~/bin/my-cleanup --dry-run"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.worktree_cleanup_hint() == "~/bin/my-cleanup --dry-run"


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


def test_spawn_env_default_empty(no_config):
    assert config.spawn_env() == {}


def test_spawn_env_reads_table_str_only(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, '[spawn.env]\nWORKER_AUTONOMOUS = "true"\nBAD = 3\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.spawn_env() == {"WORKER_AUTONOMOUS": "true"}


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


def test_ticket_url_default_none(no_config):
    assert config.ticket_url() is None


def test_ticket_url_set(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, "[task_keys]\nticket_url = 'https://tracker.example/browse/{key}'\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.ticket_url() == "https://tracker.example/browse/{key}"


def test_ticket_url_without_a_key_slot_means_none(no_config, monkeypatch, tmp_path):
    for line in ("ticket_url = ''", "ticket_url = 'https://tracker.example/'", "ticket_url = 42"):
        p = _write(tmp_path, f"[task_keys]\n{line}\n")
        monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
        assert config.ticket_url() is None, line


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


def test_worker_headless_preset_default_true(monkeypatch):
    monkeypatch.setattr(config, "load", lambda: {})
    assert config.worker_headless_preset() is True


def test_worker_headless_preset_false(monkeypatch):
    monkeypatch.setattr(config, "load", lambda: {"spawn": {"worker_headless_preset": False}})
    assert config.worker_headless_preset() is False


def test_worker_headless_preset_non_bool_falls_back_true(monkeypatch):
    monkeypatch.setattr(config, "load", lambda: {"spawn": {"worker_headless_preset": "no"}})
    assert config.worker_headless_preset() is True


def test_usage_pause_pct(tmp_path, monkeypatch):
    cfg = tmp_path / "dockwright.toml"
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    assert config.usage_pause_pct() is None
    cfg.write_text("[accounts]\nusage_pause_pct = 72.5\n")
    assert config.usage_pause_pct() == 72.5
    cfg.write_text("[accounts]\nusage_pause_pct = true\n")
    assert config.usage_pause_pct() is None
    cfg.write_text("[accounts]\nusage_pause_pct = -3\n")
    assert config.usage_pause_pct() is None


def test_mcp_oauth_required(tmp_path, monkeypatch):
    cfg = tmp_path / "dockwright.toml"
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    assert config.mcp_oauth_required() == []
    cfg.write_text('[accounts]\nmcp_oauth_required = ["svc", "", 3, "other"]\n')
    assert config.mcp_oauth_required() == ["svc", "other"]
    cfg.write_text('[accounts]\nmcp_oauth_required = "svc"\n')
    assert config.mcp_oauth_required() == []


def test_subworkers_max_depth(tmp_path, monkeypatch):
    cfg = tmp_path / "dockwright.toml"
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    assert config.subworkers_max_depth() == 3
    cfg.write_text("[subworkers]\nmax_depth = 4\n")
    assert config.subworkers_max_depth() == 4
    cfg.write_text("[subworkers]\nmax_depth = 1\n")
    assert config.subworkers_max_depth() == 2
    cfg.write_text("[subworkers]\nmax_depth = 0\n")
    assert config.subworkers_max_depth() == 2
    cfg.write_text("[subworkers]\nmax_depth = true\n")
    assert config.subworkers_max_depth() == 3
    cfg.write_text('[subworkers]\nmax_depth = "deep"\n')
    assert config.subworkers_max_depth() == 3


def test_roles_dir_precedence(no_config, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "roles_dir", _REAL_ROLES_DIR)
    home = no_config
    assert config.roles_dir() == home / ".claude" / "roles"
    p = _write(tmp_path, f'[paths]\nclaude_config_home = "{tmp_path}/cch"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.roles_dir() == tmp_path / "cch" / "roles"
    p = _write(tmp_path, f'[paths]\nclaude_config_home = "{tmp_path}/cch"\nroles_dir = "~/r"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.roles_dir() == home / "r"


def test_agent_ceilings(no_config, monkeypatch):
    assert config.agent_ceilings() == {}
    p = _write(no_config,
               '[ceilings]\n'
               '"manager.md" = { bytes = 140000, lines = 975 }\n'
               '"worker.md" = { bytes = 35000, min_dropins = 3, min_agent_vars = 1 }\n'
               '"typo.md" = { byts = 1 }\n'
               '"nonpositive.md" = { bytes = 0, lines = -3 }\n'
               '"wrongtype.md" = 140000\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.agent_ceilings() == {
        "manager.md": {"bytes": 140_000, "lines": 975},
        "worker.md": {"bytes": 35_000, "min_dropins": 3, "min_agent_vars": 1},
        "typo.md": {},
        "nonpositive.md": {},
        "wrongtype.md": {},
    }


def _claude_home(no_config):
    claude = no_config / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    return claude


def test_the_manager_default_resolves_with_no_spawn_section(no_config):
    assert config.manager_model() == models.families()["fable"] + "[1m]"


def test_worker_model_is_gone():
    assert not hasattr(config, "worker_model") and not hasattr(config, "roster_path")


def test_provider_effort_reads_the_provider_table(tmp_path, monkeypatch):
    cfg = tmp_path / "dw.toml"
    cfg.write_text('[subworkers.providers.claude-exec]\nmodel = "@opus"\neffort = "high"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    assert (config.provider_model("claude-exec"), config.provider_effort("claude-exec")) == ("@opus", "high")
    cfg.write_text('[subworkers.providers.claude-exec]\nmodel = ""\neffort = ""\n')
    assert (config.provider_model("claude-exec"), config.provider_effort("claude-exec")) == (None, None)


def test_a_concrete_spawn_key_still_wins(no_config):
    _write(_claude_home(no_config), '[spawn]\nmanager_model = "claude-opus-5[1m]"\n')
    assert config.manager_model() == "claude-opus-5[1m]"


def test_a_token_in_the_spawn_key_resolves(no_config):
    _write(_claude_home(no_config), '[spawn]\nmanager_model = "@sonnet"\n')
    assert config.manager_model() == models.families()["sonnet"]


def test_pool_ignores_manager_remote_control_key(no_config, monkeypatch, tmp_path):
    p = _write(tmp_path, '''
[accounts]
default = "a"
[[accounts.pool]]
name = "a"
[[accounts.pool]]
name = "b"
manager_remote_control = false
[[accounts.pool]]
name = "c"
manager_remote_control = "false"
''')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.accounts() == [
        config.Account(name="a"),
        config.Account(name="b"),
        config.Account(name="c"),
    ]
    assert config.default_account() == "a"
    assert config.load_error() is None


def test_daemon_defaults_overrides_and_bad_types(tmp_path, monkeypatch):
    cfg = tmp_path / "dockwright.toml"
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    assert config.daemon_enabled() is False
    assert config.daemon_push() == "macos"
    assert config.daemon_int("push_after_sec", 180) == 180
    cfg.write_text('[daemon]\nenabled = true\npush = "none"\npush_after_sec = 60\npush_cap_per_hour = 2\n')
    assert config.daemon_enabled() is True
    assert config.daemon_push() == "none"
    assert config.daemon_int("push_after_sec", 180) == 60
    assert config.daemon_int("push_cap_per_hour", 6) == 2
    cfg.write_text('[daemon]\nenabled = "yes"\npush = "nowhere"\npush_after_sec = true\npush_cap_per_hour = -4\n')
    assert config.daemon_enabled() is False
    assert config.daemon_push() == "macos"
    assert config.daemon_int("push_after_sec", 180) == 180
    assert config.daemon_int("push_cap_per_hour", 6) == 0


def test_profiles_subagents_and_roles_readers(no_config, monkeypatch):
    p = _write(no_config,
               '[profiles]\nwork_default = "back"\nwork_roots = ["~/work"]\n'
               '[profiles.sets]\nnone = []\nback = ["acme", "acme-be"]\n'
               '[profiles.repos]\nacme-web = "front"\n'
               '[subagents]\ndefault_role = "Scout"\ngeneral-purpose = { role = "Scout", core = "general-purpose" }\n'
               '[roles]\nstrict = true\ncomputed = false\n'
               '[paths]\nproject_units_dir = "~/units"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.profiles() == {"sets": {"none": [], "back": ["acme", "acme-be"]}, "repos": {"acme-web": "front"},
                                 "work_roots": ["~/work"], "personal_owners": [], "work_default": "back"}
    assert config.subagents() == {"general-purpose": {"role": "Scout", "core": "general-purpose"}}
    assert config.subagent_default_role() == "Scout"
    assert config.project_units_dir() == no_config / "units"
    assert config.roles_strict() is True and config.roles_computed() is False


def test_readers_are_empty_without_the_tables(no_config, monkeypatch):
    p = _write(no_config, "[daemon]\nenabled = false\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.profiles() == {"sets": {}, "repos": {}, "work_roots": [], "personal_owners": [], "work_default": "none"}
    assert config.subagents() == {} and config.subagent_default_role() is None
    assert config.project_units_dir() is None and config.roles_strict() is False


def test_lanes_defaults_and_overrides(tmp_path, monkeypatch):
    cfg = tmp_path / "dockwright.toml"
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    assert config.lanes_in_manager() is True
    assert config.lanes_env() == {}
    cfg.write_text('[lanes]\nin_manager = false\n[lanes.env]\nCLAUDE_ORCH_AUTONUDGE = "1"\nBAD = 3\n')
    assert config.lanes_in_manager() is False
    assert config.lanes_env() == {"CLAUDE_ORCH_AUTONUDGE": "1"}
    cfg.write_text('[lanes]\nin_manager = "no"\n')
    assert config.lanes_in_manager() is True


def test_gh_defaults_and_overrides(tmp_path, monkeypatch):
    cfg = tmp_path / "dockwright.toml"
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    assert config.gh_configured() is False
    assert config.gh_binary() == "gh"
    assert config.gh_logins() == {}
    assert config.gh_token_ttl_sec() == 600
    cfg.write_text('[gh]\nbinary = "/opt/homebrew/bin/gh"\ntoken_ttl_sec = 30\n[gh.logins]\nacme = "alice"\nbad = 1\n')
    assert config.gh_configured() is True
    assert config.gh_binary() == "/opt/homebrew/bin/gh"
    assert config.gh_logins() == {"acme": "alice"}
    assert config.gh_token_ttl_sec() == 30
    cfg.write_text("[gh]\n")
    assert config.gh_configured() is True and config.gh_logins() == {}


def test_schedules_reader_keeps_valid_names_and_fills_defaults(tmp_path, monkeypatch):
    cfg = tmp_path / "dockwright.toml"
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    assert config.schedules() == {}
    cfg.write_text('[schedules.nightly]\nschema = "x"\ncron = "0 9 * * *"\nask = "go"\n'
                   '[schedules."bad name!"]\nschema = "x"\ncron = "* * * * *"\nask = "no"\n'
                   '[schedules.off]\nschema = "x"\ncron = "* * * * *"\nask = "no"\nenabled = false\n'
                   '[schedules.half]\nschema = "x"\n')
    got = config.schedules()
    assert set(got) == {"nightly", "off", "half"}
    assert got["nightly"] == {"schema": "x", "cron": "0 9 * * *", "ask": "go", "cwd": None, "domain": None,
                              "enabled": True, "errors": []}
    cfg.write_text('[schedules.home]\nschema = "x"\ncron = "* * * * *"\nask = "a"\ncwd = "~/w"\n')
    assert config.schedules()["home"]["cwd"] == str(Path.home() / "w")
    assert got["off"]["enabled"] is False
    assert got["half"]["errors"] == ["cron is required", "ask is required"]


def test_the_sprint_cadence_reads_its_key(tmp_path, monkeypatch):
    p = _write(tmp_path, '[events.sources.tracker]\nsprint_every_sec = 600\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    assert config.events_source("tracker")["sprint_every_sec"] == 600
