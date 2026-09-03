import json
from pathlib import Path
from dockwright import doctor

ABS = "/Users/testop/projects/personal/claude-orchestrator/.venv/bin/orchestrator"

def test_mcp_command_extractors():
    assert doctor.mcp_command_claude(
        {"mcpServers": {"claude-orchestrator": {"command": ABS}}}, "claude-orchestrator") == ABS
    assert doctor.mcp_command_codex(
        {"mcp_servers": {"claude-orchestrator": {"command": "orchestrator"}}}, "claude-orchestrator") == "orchestrator"
    assert doctor.mcp_command_claude({}, "claude-orchestrator") is None

def test_check_mcp_pass_fail():
    assert doctor.check_mcp("claude", ABS, ABS).ok
    assert not doctor.check_mcp("codex", "orchestrator", ABS).ok

def test_check_hooks_abspath_flags_bare():
    bare = {"hooks": {"Stop": [{"hooks": [{"command": "bash -c '$PPID orchestrator stop'"}]}]}}
    abss = {"hooks": {"Stop": [{"hooks": [{"command": f"bash -c '$PPID {ABS} stop'"}]}]}}
    assert not doctor.check_hooks_abspath(bare, ABS, "claude").ok
    assert doctor.check_hooks_abspath(abss, ABS, "claude").ok

def test_cli_returns_1_on_failure(tmp_path):
    cj = tmp_path / "claude.json"; cj.write_text(json.dumps({"mcpServers": {"claude-orchestrator": {"command": "orchestrator"}}}))
    rc = doctor.main(["--orch-bin", ABS, "--claude-json", str(cj), "--brew-prefix", str(tmp_path),
                      "--settings", str(tmp_path / "settings.json"),
                      "--codex-hooks", str(tmp_path / "codex-hooks.json"),
                      "--codex-config", str(tmp_path / "codex-config.toml")])
    assert rc == 1

def test_cli_fails_on_unparseable_existing_config(tmp_path):
    bad = tmp_path / "settings.json"; bad.write_text("{ not json")
    rc = doctor.main(["--orch-bin", ABS, "--settings", str(bad), "--brew-prefix", str(tmp_path),
                      "--claude-json", str(tmp_path / "claude.json"),
                      "--codex-hooks", str(tmp_path / "codex-hooks.json"),
                      "--codex-config", str(tmp_path / "codex-config.toml")])
    assert rc == 1

def test_cli_skips_missing_files(tmp_path):
    rc = doctor.main(["--orch-bin", ABS, "--claude-json", str(tmp_path/'absent.json'),
                      "--brew-prefix", str(tmp_path),
                      "--settings", str(tmp_path / "settings.json"),
                      "--codex-hooks", str(tmp_path / "codex-hooks.json"),
                      "--codex-config", str(tmp_path / "codex-config.toml")])
    assert rc in (0, 1)

import sys


def test_default_orch_bin_sits_beside_interpreter():
    expected = str(Path(sys.executable).parent / "dockwright")
    assert doctor._default_orch_bin() == expected


def test_cli_bare_invocation_runs_without_usage_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = doctor.main([])
    assert rc in (0, 1)
    out = capsys.readouterr().out
    assert "venv-import" in out
    assert "accounts:pointer" in out
    assert "accounts:login" in out


def test_account_pointer_check(tmp_path, monkeypatch):
    from dockwright import doctor, paths
    monkeypatch.setattr(paths, "ACCOUNT_ACTIVE", tmp_path / "account-active")
    c = doctor.check_account_pointer()
    assert c.ok and "absent" in c.detail
    (tmp_path / "account-active").write_text("a\n")
    assert doctor.check_account_pointer().ok
    (tmp_path / "account-active").write_text("b\n")
    c = doctor.check_account_pointer()
    assert not c.ok and "silently OFF" in c.detail


def _login_pool(monkeypatch, tmp_path, entries):
    from dockwright import config
    lines = ["[accounts]", f'default = "{entries[0][0]}"']
    for name, cd in entries:
        lines.append("[[accounts.pool]]")
        lines.append(f'name = "{name}"')
        if cd is not None:
            lines.append(f'config_dir = "{cd}"')
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text("\n".join(lines) + "\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


def _seed_host(monkeypatch, tmp_path, payload):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    host = home / ".claude.json"
    if payload is not None:
        host.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return host


def test_accounts_login_passes_when_no_non_default(tmp_path, monkeypatch):
    from dockwright import doctor
    _seed_host(monkeypatch, tmp_path, {"oauthAccount": {"emailAddress": "a@x"}})
    _login_pool(monkeypatch, tmp_path, [("a", None)])
    c = doctor.check_accounts_login()
    assert c.ok and "all 1 pool account(s) carry an oauthAccount marker" in c.detail
    assert "marker presence only" in c.detail


def test_accounts_login_passes_when_marker_present(tmp_path, monkeypatch):
    from dockwright import doctor
    _seed_host(monkeypatch, tmp_path, {"oauthAccount": {"emailAddress": "a@x"}})
    farm = tmp_path / "farm-b"; farm.mkdir()
    (farm / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"accountUuid": "uuid-b", "emailAddress": "b@x"}}))
    _login_pool(monkeypatch, tmp_path, [("a", None), ("b", str(farm))])
    c = doctor.check_accounts_login()
    assert c.ok and "all 2 pool account(s) carry an oauthAccount marker" in c.detail
    assert "an expired token still 401s with the marker intact" in c.detail


def test_accounts_login_fails_when_farm_missing(tmp_path, monkeypatch):
    from dockwright import doctor
    _seed_host(monkeypatch, tmp_path, {"oauthAccount": {"emailAddress": "a@x"}})
    farm = tmp_path / "farm-b"
    _login_pool(monkeypatch, tmp_path, [("a", None), ("b", str(farm))])
    c = doctor.check_accounts_login()
    assert not c.ok and "b (" in c.detail and "never logged in" in c.detail


def test_accounts_login_fails_when_marker_absent(tmp_path, monkeypatch):
    from dockwright import doctor
    _seed_host(monkeypatch, tmp_path, {"oauthAccount": {"emailAddress": "a@x"}})
    farm = tmp_path / "farm-b"; farm.mkdir()
    (farm / ".claude.json").write_text(json.dumps({"projects": {}}))
    _login_pool(monkeypatch, tmp_path, [("a", None), ("b", str(farm))])
    c = doctor.check_accounts_login()
    assert not c.ok and "b (" in c.detail and "oauthAccount" in c.detail


def test_accounts_login_fails_when_default_marker_absent(tmp_path, monkeypatch):
    from dockwright import doctor
    _seed_host(monkeypatch, tmp_path, {"projects": {}})
    _login_pool(monkeypatch, tmp_path, [("a", None)])
    c = doctor.check_accounts_login()
    assert not c.ok and "a (" in c.detail and "oauthAccount" in c.detail
    assert "fix: claude, then /login" in c.detail
    assert "CLAUDE_CONFIG_DIR" not in c.detail
    assert "<farm>" not in c.detail


def test_accounts_login_fails_when_default_marker_falsy(tmp_path, monkeypatch):
    from dockwright import doctor
    _seed_host(monkeypatch, tmp_path, {"oauthAccount": None})
    _login_pool(monkeypatch, tmp_path, [("a", None)])
    c = doctor.check_accounts_login()
    assert not c.ok and "a (" in c.detail and "fix: claude, then /login" in c.detail


def test_accounts_login_fails_when_farm_marker_falsy(tmp_path, monkeypatch):
    from dockwright import doctor
    _seed_host(monkeypatch, tmp_path, {"oauthAccount": {"emailAddress": "a@x"}})
    farm = tmp_path / "farm-b"; farm.mkdir()
    (farm / ".claude.json").write_text(json.dumps({"oauthAccount": {}}))
    _login_pool(monkeypatch, tmp_path, [("a", None), ("b", str(farm))])
    c = doctor.check_accounts_login()
    assert not c.ok and "b (" in c.detail


def test_accounts_login_fails_when_host_missing(tmp_path, monkeypatch):
    from dockwright import doctor
    _seed_host(monkeypatch, tmp_path, None)
    _login_pool(monkeypatch, tmp_path, [("a", None)])
    c = doctor.check_accounts_login()
    assert not c.ok and "a (" in c.detail and "never logged in" in c.detail
    assert "fix: claude, then /login" in c.detail


def test_accounts_login_fails_when_host_unparseable(tmp_path, monkeypatch):
    from dockwright import doctor
    _seed_host(monkeypatch, tmp_path, "{not-json")
    _login_pool(monkeypatch, tmp_path, [("a", None)])
    c = doctor.check_accounts_login()
    assert not c.ok and "a (" in c.detail and "unreadable" in c.detail


def test_accounts_login_default_ignores_registry_config_dir(tmp_path, monkeypatch):
    from dockwright import doctor
    _seed_host(monkeypatch, tmp_path, {"oauthAccount": {"emailAddress": "a@x"}})
    stray = tmp_path / "stray-default-farm"; stray.mkdir()
    _login_pool(monkeypatch, tmp_path, [("a", str(stray))])
    c = doctor.check_accounts_login()
    assert c.ok, c.detail


def test_accounts_login_fix_command_exact_for_farm(tmp_path, monkeypatch):
    from dockwright import doctor
    _seed_host(monkeypatch, tmp_path, {"oauthAccount": {"emailAddress": "a@x"}})
    farm = tmp_path / "farm-b"; farm.mkdir()
    (farm / ".claude.json").write_text(json.dumps({"projects": {}}))
    _login_pool(monkeypatch, tmp_path, [("a", None), ("b", str(farm))])
    c = doctor.check_accounts_login()
    assert not c.ok
    assert f"fix: CLAUDE_CONFIG_DIR={farm} claude, then /login" in c.detail
    assert "<farm>" not in c.detail


def test_main_routes_host_claude_json_into_login_check(tmp_path, monkeypatch, capsys):
    from dockwright import doctor
    _seed_host(monkeypatch, tmp_path, None)
    _login_pool(monkeypatch, tmp_path, [("a", None)])
    rc = doctor.main(["--host-claude-json", str(tmp_path / "elsewhere.json")])
    out = capsys.readouterr().out
    assert rc == 1
    assert f"no {tmp_path / 'elsewhere.json'}" in out


def test_main_claude_json_no_longer_feeds_login_check(tmp_path, monkeypatch, capsys):
    from dockwright import doctor
    _seed_host(monkeypatch, tmp_path, None)
    _login_pool(monkeypatch, tmp_path, [("a", None)])
    farm_b = tmp_path / "farm-b.json"
    farm_b.write_text(json.dumps({"oauthAccount": {"emailAddress": "b@x"}}))
    rc = doctor.main(["--claude-json", str(farm_b)])
    out = capsys.readouterr().out
    assert rc == 1
    assert f"no {tmp_path / 'home' / '.claude.json'}" in out
