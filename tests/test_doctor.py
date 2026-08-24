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
    # parse-gated checks (mcp:claude, mcp:codex, hooks:*) skip cleanly when
    # their config path is absent; accounts:login is NOT parse-gated — it
    # FAILS loud on the absent claude-json (the D1 fix), which is why rc can
    # be 1 here even though the parse-gated checks all skipped.
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
    # the parse-gated checks (mcp/hooks) skip cleanly on the absent defaulted
    # paths, but accounts:login now FAILS loud on the absent host claude.json
    # (the D1 fix) — that's why rc is accepted in (0, 1) below.
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


def _seed_host(monkeypatch, tmp_path, payload):
    """Fake HOME and (unless payload is None) write <home>/.claude.json.
    Path.home() resolves HOME at call time, so this keeps the default-account
    leg off the operator's real file."""
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
    _login_pool(monkeypatch, tmp_path, [("a", None)])   # solo pool → default leg only
    c = doctor.check_accounts_login()
    assert c.ok and "all 1 pool account(s) carry an oauthAccount marker" in c.detail
    assert "marker presence only" in c.detail   # PASS must not imply liveness


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
    farm = tmp_path / "farm-b"   # never created → no .claude.json
    _login_pool(monkeypatch, tmp_path, [("a", None), ("b", str(farm))])
    c = doctor.check_accounts_login()
    assert not c.ok and "b (" in c.detail and "never logged in" in c.detail


def test_accounts_login_fails_when_marker_absent(tmp_path, monkeypatch):
    from dockwright import doctor
    _seed_host(monkeypatch, tmp_path, {"oauthAccount": {"emailAddress": "a@x"}})
    farm = tmp_path / "farm-b"; farm.mkdir()
    (farm / ".claude.json").write_text(json.dumps({"projects": {}}))  # real json, no marker
    _login_pool(monkeypatch, tmp_path, [("a", None), ("b", str(farm))])
    c = doctor.check_accounts_login()
    assert not c.ok and "b (" in c.detail and "oauthAccount" in c.detail


# ---- D1: the default account is checked against HOME-root ~/.claude.json ----
# (2026-07-29 incident: doctor printed PASS while the default account's login
# was dead — the old check skipped the default account entirely.)

def test_accounts_login_fails_when_default_marker_absent(tmp_path, monkeypatch):
    """A skeleton host file (today's stray shape: valid JSON, no oauthAccount)
    must FAIL and the default's fix command must carry NO CLAUDE_CONFIG_DIR."""
    from dockwright import doctor
    _seed_host(monkeypatch, tmp_path, {"projects": {}})
    _login_pool(monkeypatch, tmp_path, [("a", None)])
    c = doctor.check_accounts_login()
    assert not c.ok and "a (" in c.detail and "oauthAccount" in c.detail
    assert "fix: claude, then /login" in c.detail
    assert "CLAUDE_CONFIG_DIR" not in c.detail
    assert "<farm>" not in c.detail


def test_accounts_login_fails_when_default_marker_falsy(tmp_path, monkeypatch):
    """Falsy sibling (drift-guard-tests.md): a PRESENT-but-null/empty
    oauthAccount is not login evidence — same FAIL path as an absent key."""
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
    """Missing $HOME/.claude.json = no login evidence for the default account:
    FAIL LOUD, never skip."""
    from dockwright import doctor
    _seed_host(monkeypatch, tmp_path, None)   # HOME faked, file never written
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
    """Runtime (spawner._build_account_prefix, stale_monitor._account_config_prefix)
    ignores a registry config_dir on the DEFAULT entry — doctor must mirror
    that: healthy host + default config_dir pointing at an empty dir = PASS."""
    from dockwright import doctor
    _seed_host(monkeypatch, tmp_path, {"oauthAccount": {"emailAddress": "a@x"}})
    stray = tmp_path / "stray-default-farm"; stray.mkdir()   # no .claude.json inside
    _login_pool(monkeypatch, tmp_path, [("a", str(stray))])
    c = doctor.check_accounts_login()
    assert c.ok, c.detail


# ---- D2 (doctor leg): the fix command is the exact resolved command ---------

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
    """main() must hand an EXPLICIT --host-claude-json path to
    check_accounts_login — the FAIL detail names that exact path. Must use an
    explicit non-default path: argparse's default and check_accounts_login's
    own internal fallback are both `Path.home() / ".claude.json"`, so under
    the same faked HOME they're degenerate-identical and a dropped-wiring
    regression (reverting to check_accounts_login() with no args) would still
    resolve to the same path and leave this test green. Only routing a path
    that differs from the fallback proves the wiring."""
    from dockwright import doctor
    _seed_host(monkeypatch, tmp_path, None)
    _login_pool(monkeypatch, tmp_path, [("a", None)])
    rc = doctor.main(["--host-claude-json", str(tmp_path / "elsewhere.json")])
    out = capsys.readouterr().out
    assert rc == 1
    assert f"no {tmp_path / 'elsewhere.json'}" in out


def test_main_claude_json_no_longer_feeds_login_check(tmp_path, monkeypatch, capsys):
    """Decoupling guard: --claude-json serves the mcp:claude parse check only.
    An ad-hoc `doctor --claude-json <account-b farm>` (a plausible way to debug
    b's MCP wiring) must NOT be read as the DEFAULT account's login evidence."""
    from dockwright import doctor
    _seed_host(monkeypatch, tmp_path, None)
    _login_pool(monkeypatch, tmp_path, [("a", None)])
    farm_b = tmp_path / "farm-b.json"
    farm_b.write_text(json.dumps({"oauthAccount": {"emailAddress": "b@x"}}))
    rc = doctor.main(["--claude-json", str(farm_b)])
    out = capsys.readouterr().out
    assert rc == 1
    assert f"no {tmp_path / 'home' / '.claude.json'}" in out   # login leg stayed on the host default
