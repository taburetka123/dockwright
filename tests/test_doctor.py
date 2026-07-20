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
    assert rc == 1   # bare reg fails (venv-import also fails since ABS python absent — both FAIL)

def test_cli_fails_on_unparseable_existing_config(tmp_path):
    # An existing-but-malformed settings.json must FAIL the fail-loud gate, not skip vacuously.
    bad = tmp_path / "settings.json"; bad.write_text("{ not json")
    rc = doctor.main(["--orch-bin", ABS, "--settings", str(bad), "--brew-prefix", str(tmp_path),
                      "--claude-json", str(tmp_path / "claude.json"),
                      "--codex-hooks", str(tmp_path / "codex-hooks.json"),
                      "--codex-config", str(tmp_path / "codex-config.toml")])
    assert rc == 1

def test_cli_skips_missing_files(tmp_path):
    # only no-brew-editable + venv-import run; missing claude/codex/settings paths are skipped
    rc = doctor.main(["--orch-bin", ABS, "--claude-json", str(tmp_path/'absent.json'),
                      "--brew-prefix", str(tmp_path),
                      "--settings", str(tmp_path / "settings.json"),
                      "--codex-hooks", str(tmp_path / "codex-hooks.json"),
                      "--codex-config", str(tmp_path / "codex-config.toml")])
    # venv-import fails (ABS not real here) but missing claude-json must not raise
    assert rc in (0, 1)

import sys


def test_default_orch_bin_sits_beside_interpreter():
    expected = str(Path(sys.executable).parent / "dockwright")
    assert doctor._default_orch_bin() == expected


def test_cli_bare_invocation_runs_without_usage_error(tmp_path, monkeypatch, capsys):
    # README documents bare `dockwright doctor`; argparse must not exit(2).
    # HOME is faked so the test never reads the developer's real ~/.claude.json —
    # the defaulted config paths are absent under tmp_path and skip cleanly.
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = doctor.main([])
    assert rc in (0, 1)
    out = capsys.readouterr().out
    assert "venv-import" in out  # checks actually ran
    assert "accounts:pointer" in out  # check_account_pointer is wired into main()'s checks list
    assert "accounts:login" in out    # check_accounts_login is wired into main()'s checks list


def test_account_pointer_check(tmp_path, monkeypatch):
    from dockwright import doctor, paths
    monkeypatch.setattr(paths, "ACCOUNT_ACTIVE", tmp_path / "account-active")
    c = doctor.check_account_pointer()
    assert c.ok and "absent" in c.detail                      # no pointer = pool off = fine
    (tmp_path / "account-active").write_text("a\n")
    assert doctor.check_account_pointer().ok
    (tmp_path / "account-active").write_text("b\n")           # default registry is now len-1
    c = doctor.check_account_pointer()
    assert not c.ok and "silently OFF" in c.detail


# ---- accounts:login — every declared NON-DEFAULT pool account should show login
# evidence (its farm .claude.json carrying oauthAccount, which farm assembly pops
# on every rebuild so its presence can only come from a real /login). Each pool
# below routes its non-default account's config_dir into tmp so the check never
# reads the operator's real ~/.claude-<name>.

def _login_pool(monkeypatch, tmp_path, entries):
    """entries: [(name, config_dir_or_None), ...]; first is the default."""
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


def test_accounts_login_passes_when_no_non_default(tmp_path, monkeypatch):
    from dockwright import doctor
    _login_pool(monkeypatch, tmp_path, [("a", None)])   # solo pool → nothing to check
    c = doctor.check_accounts_login()
    assert c.ok and "all non-default" in c.detail


def test_accounts_login_passes_when_marker_present(tmp_path, monkeypatch):
    from dockwright import doctor
    farm = tmp_path / "farm-b"; farm.mkdir()
    (farm / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"accountUuid": "uuid-b", "emailAddress": "b@x"}}))
    _login_pool(monkeypatch, tmp_path, [("a", None), ("b", str(farm))])
    c = doctor.check_accounts_login()
    assert c.ok and "all non-default" in c.detail


def test_accounts_login_fails_when_farm_missing(tmp_path, monkeypatch):
    from dockwright import doctor
    farm = tmp_path / "farm-b"   # never created → no .claude.json
    _login_pool(monkeypatch, tmp_path, [("a", None), ("b", str(farm))])
    c = doctor.check_accounts_login()
    assert not c.ok and "b (" in c.detail and "never logged in" in c.detail


def test_accounts_login_fails_when_marker_absent(tmp_path, monkeypatch):
    from dockwright import doctor
    farm = tmp_path / "farm-b"; farm.mkdir()
    (farm / ".claude.json").write_text(json.dumps({"projects": {}}))  # real json, no marker
    _login_pool(monkeypatch, tmp_path, [("a", None), ("b", str(farm))])
    c = doctor.check_accounts_login()
    assert not c.ok and "b (" in c.detail and "oauthAccount" in c.detail
