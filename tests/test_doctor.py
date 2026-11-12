import asyncio
import getpass
import json
import os
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path

import pytest

from dockwright import compose, config, doctor

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
                      "--codex-config", str(tmp_path / "codex-config.toml"),
                      "--codex-dir", str(tmp_path / "codex")])
    assert rc == 1

def test_cli_fails_on_unparseable_existing_config(tmp_path):
    bad = tmp_path / "settings.json"; bad.write_text("{ not json")
    rc = doctor.main(["--orch-bin", ABS, "--settings", str(bad), "--brew-prefix", str(tmp_path),
                      "--claude-json", str(tmp_path / "claude.json"),
                      "--codex-hooks", str(tmp_path / "codex-hooks.json"),
                      "--codex-config", str(tmp_path / "codex-config.toml"),
                      "--codex-dir", str(tmp_path / "codex")])
    assert rc == 1

def test_cli_skips_missing_files(tmp_path):
    rc = doctor.main(["--orch-bin", ABS, "--claude-json", str(tmp_path/'absent.json'),
                      "--brew-prefix", str(tmp_path),
                      "--settings", str(tmp_path / "settings.json"),
                      "--codex-hooks", str(tmp_path / "codex-hooks.json"),
                      "--codex-config", str(tmp_path / "codex-config.toml"),
                      "--codex-dir", str(tmp_path / "codex")])
    assert rc in (0, 1)

import sys


def test_default_orch_bin_sits_beside_interpreter():
    expected = str(Path(sys.executable).parent / "dockwright")
    assert doctor._default_orch_bin() == expected


def test_cli_bare_invocation_runs_without_usage_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
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


def test_account_holds_check(tmp_path, monkeypatch):
    from dockwright import doctor, paths
    monkeypatch.setattr(paths, "ACCOUNT_STATE", tmp_path / "account-state.json")
    c = doctor.check_account_holds()
    assert c.name == "accounts:hold" and c.ok and "no account on hold" in c.detail
    (tmp_path / "account-state.json").write_text(json.dumps(
        {"accounts": {}, "held": {"b": {"at": 1_760_000_000, "reason": "appeal pending", "source": "cli"}}}))
    c = doctor.check_account_holds()
    assert c.ok and "b" in c.detail and "appeal pending" in c.detail and "account-hold --release b" in c.detail


def _login_pool(monkeypatch, tmp_path, entries, required=None):
    from dockwright import config
    lines = ["[accounts]", f'default = "{entries[0][0]}"']
    if required is not None:
        lines.append(f"mcp_oauth_required = {json.dumps(required)}")
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
    monkeypatch.delenv("CODEX_HOME", raising=False)
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
    monkeypatch.delenv("CODEX_HOME", raising=False)
    rc = doctor.main(["--claude-json", str(farm_b)])
    out = capsys.readouterr().out
    assert rc == 1
    assert f"no {tmp_path / 'home' / '.claude.json'}" in out


MCP_NOW = 1_800_000_000.0
A_SERVICE = "Claude Code-credentials"
SERVER_FIX = "in a directory whose .mcp.json declares {s}, then /mcp → {s} → Authenticate)"


def _b_service(tmp_path):
    return A_SERVICE + "-" + sha256(str(tmp_path / "farm-b").encode()).hexdigest()[:8]


def _mcp_pool(monkeypatch, tmp_path, required):
    _login_pool(monkeypatch, tmp_path, [("a", None), ("b", str(tmp_path / "farm-b"))], required=required)


def _keychain(replies):
    calls = []

    def run(argv, **kw):
        calls.append((list(argv), kw.get("timeout")))
        reply = replies[argv[argv.index("-s") + 1]]
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, int):
            return subprocess.CompletedProcess(argv, reply, b"", b"")
        out = reply if isinstance(reply, str) else json.dumps(reply)
        return subprocess.CompletedProcess(argv, 0, out.encode(), b"")

    run.calls = calls
    return run


def _mcp_check(run, which=lambda _name: "/usr/bin/security"):
    return doctor.check_mcp_oauth(run=run, which=which, now=lambda: MCP_NOW)


def _item(entries):
    return {"claudeAiOauth": {}, "mcpOAuth": entries}


def _refresh(token="r"):
    return {"accessToken": "", "refreshToken": token}


def _access(expires_s, token="x"):
    return {"accessToken": token, "expiresAt": expires_s * 1000}


def test_mcp_oauth_off_when_the_key_is_empty(tmp_path, monkeypatch):
    _mcp_pool(monkeypatch, tmp_path, None)
    run = _keychain({})
    c = _mcp_check(run)
    assert c.name == "accounts:mcp-oauth"
    assert c.ok and c.detail == "off ([accounts] mcp_oauth_required is empty)"
    assert run.calls == []


def test_mcp_oauth_fails_without_a_keychain_reader(tmp_path, monkeypatch):
    _mcp_pool(monkeypatch, tmp_path, ["svc"])
    run = _keychain({})
    c = _mcp_check(run, which=lambda _name: None)
    assert not c.ok
    assert c.detail == ("cannot verify: no security binary on PATH (the macOS keychain reader) "
                        "— empty [accounts] mcp_oauth_required on this platform")
    assert run.calls == []


def test_mcp_oauth_reads_each_account_by_its_service(tmp_path, monkeypatch):
    _mcp_pool(monkeypatch, tmp_path, ["svc"])
    item = _item({"svc|k": _refresh()})
    run = _keychain({A_SERVICE: item, _b_service(tmp_path): item})
    _mcp_check(run)
    user = getpass.getuser()
    assert run.calls == [
        (["security", "find-generic-password", "-s", A_SERVICE, "-a", user, "-w"], 10),
        (["security", "find-generic-password", "-s", _b_service(tmp_path), "-a", user, "-w"], 10),
    ]


def test_mcp_oauth_passes_when_every_account_holds_a_token(tmp_path, monkeypatch):
    _mcp_pool(monkeypatch, tmp_path, ["svc"])
    run = _keychain({A_SERVICE: _item({"svc|k1": _refresh()}),
                     _b_service(tmp_path): _item({"svc|k2": _access(MCP_NOW + 3600)})})
    c = _mcp_check(run)
    assert c.ok
    assert c.detail == ("all 2 pool account(s) hold a token for svc (a: svc|k1, b: svc|k2) "
                        "— token presence only; a token the server revoked still answers needs-auth")


def test_mcp_oauth_names_the_account_whose_entry_is_empty(tmp_path, monkeypatch):
    _mcp_pool(monkeypatch, tmp_path, ["svc"])
    run = _keychain({A_SERVICE: _item({"svc|k1": {"accessToken": ""}}),
                     _b_service(tmp_path): _item({"svc|k2": _refresh()})})
    c = _mcp_check(run)
    assert not c.ok
    assert c.detail == "a: svc (no usable token in svc|k1 — fix: claude " + SERVER_FIX.format(s="svc")
    assert "b: svc" not in c.detail


@pytest.mark.parametrize("entries, ok, named", [
    ({"svc|old": _refresh(), "svc|new": {"accessToken": ""}}, True, "a: svc|old"),
    ({"svc|new": {"accessToken": ""}, "svc|old": _refresh()}, True, "a: svc|old"),
    ({"svc|old": _refresh(), "svc|new": _access(MCP_NOW + 3600)}, True, "a: svc|new"),
    ({"svc|old": {"accessToken": ""}, "svc|new": {}}, False, "no usable token in svc|old, svc|new"),
])
def test_mcp_oauth_any_usable_entry_passes(tmp_path, monkeypatch, entries, ok, named):
    _mcp_pool(monkeypatch, tmp_path, ["svc"])
    run = _keychain({A_SERVICE: _item(entries), _b_service(tmp_path): _item({"svc|k2": _refresh()})})
    c = _mcp_check(run)
    assert c.ok is ok
    assert named in c.detail


@pytest.mark.parametrize("entry, ok", [
    (_access(MCP_NOW - 60), False),
    (_access(MCP_NOW + 60), True),
    ({"accessToken": "x"}, True),
    ({"accessToken": "x", "expiresAt": 0}, True),
    ({"accessToken": "x", "expiresAt": "soon"}, True),
    ({"accessToken": "x", "expiresAt": True}, True),
    ({"refreshToken": "r", "expiresAt": 1}, True),
    ({"refreshToken": ""}, False),
    ("not an entry", False),
])
def test_mcp_oauth_usable_entry_rule(tmp_path, monkeypatch, entry, ok):
    _mcp_pool(monkeypatch, tmp_path, ["svc"])
    run = _keychain({A_SERVICE: _item({"svc|k1": entry}), _b_service(tmp_path): _item({"svc|k2": _refresh()})})
    assert _mcp_check(run).ok is ok


def test_mcp_oauth_fails_on_an_expired_token_without_refresh(tmp_path, monkeypatch):
    _mcp_pool(monkeypatch, tmp_path, ["svc"])
    run = _keychain({A_SERVICE: _item({"svc|k1": _access(MCP_NOW - 60)}),
                     _b_service(tmp_path): _item({"svc|k2": _refresh()})})
    c = _mcp_check(run)
    assert not c.ok
    assert "a: svc (no usable token in svc|k1" in c.detail


@pytest.mark.parametrize("a_item", [
    {"claudeAiOauth": {}},
    {"mcpOAuth": "not a mapping"},
    _item({"other|k": _refresh(), "svcx|k": _refresh()}),
])
def test_mcp_oauth_fails_when_the_server_has_no_entry(tmp_path, monkeypatch, a_item):
    _mcp_pool(monkeypatch, tmp_path, ["svc"])
    run = _keychain({A_SERVICE: a_item, _b_service(tmp_path): _item({"svc|k2": _refresh()})})
    c = _mcp_check(run)
    assert not c.ok
    assert c.detail == "a: svc (no svc entry in mcpOAuth — fix: claude " + SERVER_FIX.format(s="svc")


def test_mcp_oauth_counts_a_bare_server_key(tmp_path, monkeypatch):
    _mcp_pool(monkeypatch, tmp_path, ["svc"])
    run = _keychain({A_SERVICE: _item({"svc": _refresh()}), _b_service(tmp_path): _item({"svc|k2": _refresh()})})
    c = _mcp_check(run)
    assert c.ok and "a: svc," in c.detail


def test_mcp_oauth_reports_each_missing_server(tmp_path, monkeypatch):
    _mcp_pool(monkeypatch, tmp_path, ["svc", "other"])
    run = _keychain({A_SERVICE: _item({"svc|k": _refresh()}),
                     _b_service(tmp_path): _item({"svc|k2": _refresh(), "other|k3": _refresh()})})
    c = _mcp_check(run)
    assert not c.ok
    assert c.detail == "a: other (no other entry in mcpOAuth — fix: claude " + SERVER_FIX.format(s="other")


def test_mcp_oauth_pass_names_every_server_of_every_account(tmp_path, monkeypatch):
    _mcp_pool(monkeypatch, tmp_path, ["svc", "other"])
    item = _item({"svc|k": _refresh(), "other|k3": _refresh()})
    c = _mcp_check(_keychain({A_SERVICE: item, _b_service(tmp_path): item}))
    assert c.ok
    assert "hold a token for svc, other (a: svc|k, a: other|k3, b: svc|k, b: other|k3)" in c.detail


@pytest.mark.parametrize("reply, reason, fix", [
    (44, 'no keychain item "{svc}"', "CLAUDE_CONFIG_DIR={farm} claude, then /login"),
    (51, 'security exited 51 reading "{svc}"', "unlock the login keychain, then re-run dockwright doctor"),
    ("not json", 'keychain item "{svc}" is not JSON', "CLAUDE_CONFIG_DIR={farm} claude, then /login"),
    ("[1, 2]", 'keychain item "{svc}" is not JSON', "CLAUDE_CONFIG_DIR={farm} claude, then /login"),
    (OSError("boom"), "could not run security: OSError",
     "check that the security binary runs, then re-run dockwright doctor"),
])
def test_mcp_oauth_reports_a_missing_or_unreadable_item(tmp_path, monkeypatch, reply, reason, fix):
    _mcp_pool(monkeypatch, tmp_path, ["svc"])
    run = _keychain({A_SERVICE: _item({"svc|k1": _refresh()}), _b_service(tmp_path): reply})
    c = _mcp_check(run)
    params = {"svc": _b_service(tmp_path), "farm": tmp_path / "farm-b"}
    assert not c.ok
    assert c.detail == f"b ({reason.format(**params)} — fix: {fix.format(**params)})"


def test_mcp_oauth_reports_a_keychain_timeout(tmp_path, monkeypatch):
    _mcp_pool(monkeypatch, tmp_path, ["svc"])
    run = _keychain({A_SERVICE: subprocess.TimeoutExpired(cmd="security", timeout=10),
                     _b_service(tmp_path): _item({"svc|k2": _refresh()})})
    c = _mcp_check(run)
    assert not c.ok
    assert c.detail == ("a (keychain read timed out after 10 s — fix: unlock the login keychain, "
                        "then re-run dockwright doctor)")
    assert [argv[3] for argv, _ in run.calls] == [A_SERVICE, _b_service(tmp_path)]


def test_mcp_oauth_detail_never_carries_a_token(tmp_path, monkeypatch):
    _mcp_pool(monkeypatch, tmp_path, ["svc"])
    secret = {"accessToken": "SECRET-ACCESS", "refreshToken": "SECRET-REFRESH", "expiresAt": MCP_NOW * 2000}
    passing = _keychain({A_SERVICE: _item({"svc|k1": secret}), _b_service(tmp_path): _item({"svc|k2": secret})})
    failing = _keychain({
        A_SERVICE: _item({"other|k": secret, "svc|k1": {"accessToken": "SECRET-ACCESS", "expiresAt": 1}}),
        _b_service(tmp_path): "SECRET-ACCESS SECRET-REFRESH",
    })
    details = [_mcp_check(passing), _mcp_check(failing)]
    assert [c.ok for c in details] == [True, False]
    for c in details:
        assert "SECRET-ACCESS" not in c.detail and "SECRET-REFRESH" not in c.detail


def test_main_runs_the_mcp_oauth_check(tmp_path, monkeypatch, capsys):
    _seed_host(monkeypatch, tmp_path, {"oauthAccount": {"emailAddress": "a@x"}})
    _login_pool(monkeypatch, tmp_path, [("a", None)])
    monkeypatch.delenv("CODEX_HOME", raising=False)
    doctor.main([])
    assert "accounts:mcp-oauth: off ([accounts] mcp_oauth_required is empty)" in capsys.readouterr().out


def test_codex_translation_check_four_states(tmp_path, monkeypatch):
    from dockwright import codex_translate as ct
    claude = tmp_path / "claude"; (claude / "rules").mkdir(parents=True)
    (claude / "rules" / "a.md").write_text("# A\n")
    codex = tmp_path / "codex"
    c = doctor.check_codex_translation(claude, codex)
    assert c.ok and "no CODEX_HOME" in c.detail
    codex.mkdir()
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    c = doctor.check_codex_translation(claude, codex)
    assert c.ok and "not on PATH" in c.detail
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/local/bin/codex")
    c = doctor.check_codex_translation(claude, codex)
    assert not c.ok and "never translated" in c.detail
    ct.translate(claude, codex)
    assert doctor.check_codex_translation(claude, codex).ok
    (claude / "rules" / "a.md").write_text("# changed\n")
    c = doctor.check_codex_translation(claude, codex)
    assert not c.ok and "AGENTS.md: differs" in c.detail


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0o000 file, so no OSError is raised")
def test_codex_translation_check_reports_an_unreadable_source_instead_of_crashing(tmp_path, monkeypatch):
    from dockwright import codex_translate as ct
    claude = tmp_path / "claude"
    (claude / "rules").mkdir(parents=True)
    (claude / "rules" / "a.md").write_text("# A\n")
    skill = claude / "skills" / "alpha-one"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: alpha-one\ndescription: one\n---\n# One\n")
    codex = tmp_path / "codex"; codex.mkdir()
    ct.translate(claude, codex)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/local/bin/codex")
    assert doctor.check_codex_translation(claude, codex).ok
    source = skill / "SKILL.md"
    source.chmod(0o000)
    try:
        c = doctor.check_codex_translation(claude, codex)
    finally:
        source.chmod(0o644)
    assert not c.ok and "check failed" in c.detail


def test_codex_translation_check_reports_a_non_utf8_source_instead_of_crashing(tmp_path, monkeypatch):
    from dockwright import codex_translate as ct
    claude = tmp_path / "claude"
    (claude / "rules").mkdir(parents=True)
    (claude / "rules" / "a.md").write_text("# A\n")
    codex = tmp_path / "codex"; codex.mkdir()
    ct.translate(claude, codex)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/local/bin/codex")
    assert doctor.check_codex_translation(claude, codex).ok
    (claude / "rules" / "b.md").write_bytes(b"\xff\xfe")
    c = doctor.check_codex_translation(claude, codex)
    assert not c.ok and "check failed" in c.detail


def test_codex_translation_check_reports_a_malformed_stamp_instead_of_crashing(tmp_path, monkeypatch):
    from dockwright import codex_translate as ct
    claude = tmp_path / "claude"
    (claude / "rules").mkdir(parents=True)
    (claude / "rules" / "a.md").write_text("# A\n")
    codex = tmp_path / "codex"; codex.mkdir()
    ct.translate(claude, codex)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/local/bin/codex")
    assert doctor.check_codex_translation(claude, codex).ok
    (codex / ct.STAMP_NAME).write_text('{"version": 1, "entries": {"AGENTS.md": "agents_md"}}')
    c = doctor.check_codex_translation(claude, codex)
    assert not c.ok and "unreadable stamp" in c.detail


def test_cli_reports_codex_translation(tmp_path, capsys):
    rc = doctor.main(["--orch-bin", ABS, "--claude-json", str(tmp_path / "absent.json"),
                      "--brew-prefix", str(tmp_path), "--settings", str(tmp_path / "settings.json"),
                      "--codex-hooks", str(tmp_path / "codex-hooks.json"),
                      "--codex-config", str(tmp_path / "codex-config.toml"),
                      "--claude-dir", str(tmp_path / "claude"), "--codex-dir", str(tmp_path / "codex")])
    assert rc in (0, 1)
    assert "[PASS] codex:translation: no CODEX_HOME" in capsys.readouterr().out


@pytest.fixture
def hermetic_config(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(tmp_path / "absent.toml"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _core(tmp_path, body="manager core\n"):
    core = tmp_path / "core"
    core.mkdir()
    (core / "manager.core.md").write_text(body)
    return core


def test_agent_sizes_without_ceilings_says_it_is_unbudgeted(tmp_path, hermetic_config):
    c = doctor.check_agent_sizes(_core(tmp_path), tmp_path / "overlay", {})
    assert c.ok and "NOT size-budgeted" in c.detail


def test_agent_sizes_over_budget_fails_with_the_measured_number(tmp_path, hermetic_config):
    core = _core(tmp_path, "y" * 500 + "\n")
    c = doctor.check_agent_sizes(core, tmp_path / "overlay", {},
                                 ceilings={"manager.md": {"bytes": 100, "lines": 9}})
    assert not c.ok
    assert "501 bytes > 100" in c.detail


def test_agent_sizes_under_budget_passes_and_reports_the_measurement(tmp_path, hermetic_config):
    core = _core(tmp_path, "y" * 500 + "\n")
    c = doctor.check_agent_sizes(core, tmp_path / "overlay", {},
                                 ceilings={"manager.md": {"bytes": 900, "lines": 9}})
    assert c.ok and "manager.md 501B/1L" in c.detail


def test_agent_sizes_entry_without_a_usable_limit_fails(tmp_path, hermetic_config):
    c = doctor.check_agent_sizes(_core(tmp_path), tmp_path / "overlay", {},
                                 ceilings={"manager.md": {}})
    assert not c.ok and "names no positive limit" in c.detail


def test_agent_sizes_refuses_a_render_with_an_unbound_var(tmp_path, hermetic_config):
    core = _core(tmp_path, "manager {{nosuchvar}} core\n")
    c = doctor.check_agent_sizes(core, tmp_path / "overlay", {},
                                 ceilings={"manager.md": {"bytes": 900}})
    assert not c.ok and "nosuchvar" in c.detail


def test_agent_sizes_without_a_core_dir_fails_rather_than_passing_blind(tmp_path, hermetic_config):
    c = doctor.check_agent_sizes(tmp_path / "gone", tmp_path / "overlay", {},
                                 ceilings={"manager.md": {"bytes": 900}})
    assert not c.ok and "no agent core dir" in c.detail


def test_agent_sizes_refuses_to_report_unbudgeted_on_a_corrupt_config(tmp_path, monkeypatch):
    bad = tmp_path / "dockwright.toml"
    bad.write_text("[ceilings\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(bad))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    c = doctor.check_agent_sizes(_core(tmp_path), tmp_path / "overlay", {})
    assert not c.ok and "does not parse" in c.detail


def test_agent_sizes_floor_catches_a_lost_overlay_whatever_the_render_weighs(
        tmp_path, hermetic_config):
    core = _core(tmp_path, "y" * 500 + "\n")
    limits = {"manager.md": {"bytes": 900, "min_dropins": 1}}
    gone = doctor.check_agent_sizes(core, tmp_path / "gone", {}, ceilings=limits)
    assert not gone.ok
    assert "0 dropins < min_dropins 1" in gone.detail
    assert str(tmp_path / "gone") in gone.detail

    overlay = tmp_path / "overlay" / "manager"
    overlay.mkdir(parents=True)
    (overlay / "10-extra.md").write_text("z" * 300 + "\n")
    present = doctor.check_agent_sizes(core, tmp_path / "overlay", {},
                                       ceilings=limits)
    assert present.ok, present.detail


def test_agent_sizes_floor_catches_lost_agent_vars(tmp_path, hermetic_config):
    core = _core(tmp_path, "manager {{opvar}} core\n")
    (core / "vars.defaults.toml").write_text("[agent_vars]\nopvar = ''\n")
    limits = {"manager.md": {"bytes": 900, "min_agent_vars": 1}}
    present = doctor.check_agent_sizes(core, tmp_path / "overlay",
                                       {"opvar": "operator text"},
                                       ceilings=limits)
    assert present.ok, present.detail
    c = doctor.check_agent_sizes(core, tmp_path / "overlay", {},
                                 ceilings=limits)
    assert not c.ok
    assert "0 agent_vars < min_agent_vars 1" in c.detail


def test_agent_sizes_names_a_composed_agent_that_no_ceiling_covers(tmp_path, hermetic_config):
    core = _core(tmp_path)
    (core / "scout.core.md").write_text("scout core\n")
    c = doctor.check_agent_sizes(core, tmp_path / "overlay", {},
                                 ceilings={"manager.md": {"bytes": 900}})
    assert c.ok
    assert "scout.md 11B/1L UNBUDGETED" in c.detail


def test_agent_sizes_reports_the_layer_census_on_the_passing_path(tmp_path, hermetic_config):
    overlay = tmp_path / "overlay" / "manager"
    overlay.mkdir(parents=True)
    (overlay / "10-extra.md").write_text("operator paragraph\n")
    c = doctor.check_agent_sizes(_core(tmp_path), tmp_path / "overlay",
                                 {"k": "v"}, ceilings={"manager.md": {"bytes": 900}})
    assert c.ok
    assert "layers: 1 drop-in(s) under " in c.detail
    assert "1 [agent_vars] key(s) from " in c.detail


def test_check_plugin_registration(tmp_path):
    root = tmp_path / "plugin"
    mon = root / "plugins" / "dockwright" / "monitors"
    mon.mkdir(parents=True)
    good = {"enabledPlugins": {"dockwright@dockwright": True},
            "extraKnownMarketplaces": {"dockwright": {"source": {"source": "directory", "path": str(root)}}}}
    assert not doctor.check_plugin_registration({}).ok
    (mon / "monitors.json").write_text('[{"command": "@@DOCKWRIGHT_BIN@@ monitor-loop done"}]')
    assert not doctor.check_plugin_registration(good).ok
    (mon / "monitors.json").write_text('[{"command": "/v/bin/dockwright monitor-loop done"}]')
    assert doctor.check_plugin_registration(good).ok
    assert not doctor.check_plugin_registration(
        {**good, "enabledPlugins": {"dockwright@dockwright": False}}).ok


def _role_canon(tmp_path):
    agents = tmp_path / "deploy" / "agents"
    agents.mkdir(parents=True)
    (agents / "manager.core.md").write_text("manager core\n<!-- overlay: hook -->\n")
    fragments = tmp_path / "deploy" / "roles" / "fragments"
    fragments.mkdir(parents=True)
    (tmp_path / "deploy" / "roles" / "GenericWorker.core.md").write_text("# GW\n")
    (fragments / "worker-protocol.md").write_text("WP\n")
    return agents


def test_roles_fresh_passes_without_manifests(tmp_path, hermetic_config):
    c = doctor.check_roles_fresh(_role_canon(tmp_path), tmp_path / "claude" / "roles", tmp_path / "claude")
    assert c.name == "roles:fresh" and c.ok and "no role manifests" in c.detail


def test_roles_fresh_fails_on_a_manifest_with_no_render(tmp_path, hermetic_config):
    role = tmp_path / "claude" / "roles" / "GenericWorker"
    role.mkdir(parents=True)
    (role / "role.toml").write_text('core = "GenericWorker"\nlaunch = ["worker"]\n')
    c = doctor.check_roles_fresh(_role_canon(tmp_path), tmp_path / "claude" / "roles", tmp_path / "claude")
    assert not c.ok and "(d) roles/GenericWorker/role.worker.md is not rendered" in c.detail


def test_roles_fresh_passes_after_compose(tmp_path, hermetic_config):
    agents = _role_canon(tmp_path)
    role = tmp_path / "claude" / "roles" / "GenericWorker"
    role.mkdir(parents=True)
    (role / "role.toml").write_text('core = "GenericWorker"\nlaunch = ["worker"]\n')
    assert compose.main(["--core-dir", str(agents), "--out-dir", str(tmp_path / "claude" / "agents"),
                         "--overlay-dir", str(tmp_path / "ov"), "--roles-dir", str(tmp_path / "claude" / "roles"),
                         "--claude-dir", str(tmp_path / "claude")]) == 0
    c = doctor.check_roles_fresh(agents, tmp_path / "claude" / "roles", tmp_path / "claude")
    assert c.ok, c.detail


def test_compose_fresh_and_sizes_count_the_manager_role_overlay(tmp_path, hermetic_config):
    agents = _role_canon(tmp_path)
    roles_dir = tmp_path / "claude" / "roles"
    (roles_dir / "Manager" / "overlay").mkdir(parents=True)
    (roles_dir / "Manager" / "overlay" / "10-x.md").write_text("---\ninsert_at: hook\n---\nFROM ROLE\n")
    out = tmp_path / "claude" / "agents"
    compose.compose_agents(agents, out, tmp_path / "ov", {}, roles_dir=roles_dir)
    assert doctor.check_compose_fresh(agents, out, tmp_path / "ov", roles_dir).ok
    assert not doctor.check_compose_fresh(agents, out, tmp_path / "ov").ok
    limits = {"manager.md": {"bytes": 900, "min_dropins": 1}}
    assert doctor.check_agent_sizes(agents, tmp_path / "ov", {}, ceilings=limits, roles_dir=roles_dir).ok
    assert not doctor.check_agent_sizes(agents, tmp_path / "ov", {}, ceilings=limits).ok


def test_agent_sizes_leaves_a_role_ceiling_to_roles_fresh(tmp_path, hermetic_config):
    c = doctor.check_agent_sizes(_core(tmp_path), tmp_path / "overlay", {},
                                 ceilings={"manager.md": {"bytes": 900},
                                           "roles/Coder/role.child.md": {"bytes": 10}})
    assert c.ok, c.detail


def test_doctor_main_checks_the_roles_named_by_its_flag(tmp_path, hermetic_config, capsys):
    agents = _role_canon(tmp_path)
    role = tmp_path / "claude" / "roles" / "GenericWorker"
    role.mkdir(parents=True)
    (role / "role.toml").write_text('core = "GenericWorker"\nlaunch = ["worker"]\n')
    doctor.main(["--orch-bin", ABS, "--claude-json", str(tmp_path / "absent.json"),
                 "--brew-prefix", str(tmp_path), "--settings", str(tmp_path / "settings.json"),
                 "--codex-hooks", str(tmp_path / "codex-hooks.json"),
                 "--codex-config", str(tmp_path / "codex-config.toml"),
                 "--claude-dir", str(tmp_path / "claude"), "--codex-dir", str(tmp_path / "codex"),
                 "--compose-core-dir", str(agents), "--compose-out-dir", str(tmp_path / "claude" / "agents"),
                 "--compose-roles-dir", str(tmp_path / "claude" / "roles")])
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "roles:fresh" in ln]
    assert len(lines) == 1 and "[FAIL]" in lines[0] and "role.worker.md is not rendered" in lines[0], lines


def test_roles_fresh_fails_on_a_role_ceiling_without_manifests(tmp_path, hermetic_config):
    (tmp_path / "absent.toml").write_text('[ceilings]\n"roles/Nope/role.worker.md" = {bytes = 1}\n')
    c = doctor.check_roles_fresh(_role_canon(tmp_path), tmp_path / "claude" / "roles", tmp_path / "claude")
    assert not c.ok and "(f) roles/Nope/role.worker.md: named in [ceilings] but no role renders it" in c.detail


def test_roles_fresh_fail_text_shows_the_failures_before_the_warnings(tmp_path, hermetic_config):
    from dockwright import roles
    guards = tmp_path / "claude" / "roles" / "_shared" / "fragments" / "git"
    guards.mkdir(parents=True)
    for name in ("twin-a", "twin-b"):
        (guards / f"{name}.md").write_text("---\nkind: guard\nbinds: [merge, push, tag]\ndomain: git\n---\nBODY\n")
    role = tmp_path / "claude" / "roles" / "GenericWorker"
    role.mkdir(parents=True)
    (role / "role.toml").write_text('core = "GenericWorker"\nlaunch = ["worker"]\nfragments = ["twin-a", "twin-b"]\n')
    agents = _role_canon(tmp_path)
    roles.compose_roles(roles.Layout(role.parent, roles.core_dirs_for(agents), tmp_path / "claude"),
                        compose.load_default_vars(agents))
    (tmp_path / "absent.toml").write_text('[ceilings]\n"roles/Nope/role.worker.md" = {bytes = 1}\n')
    c = doctor.check_roles_fresh(agents, role.parent, tmp_path / "claude")
    twin = "(i) guards ['twin-a', 'twin-b'] share bind '{}' in domain 'git' with no `cites`"
    assert not c.ok and c.detail == ("4 problem(s): (f) roles/Nope/role.worker.md: named in [ceilings] but no role "
                                     f"renders it; {twin.format('merge')}; {twin.format('push')} "
                                     "(+1 more; run `dockwright compose --check`)")


def test_doctor_main_reads_roles_against_its_claude_dir(tmp_path, hermetic_config, capsys):
    agents = _role_canon(tmp_path)
    claude = tmp_path / "claude"
    (claude / "skills" / "skill-x").mkdir(parents=True)
    (claude / "skills" / "skill-x" / "SKILL.md").write_text("---\nname: skill-x\n---\n")
    role = claude / "roles" / "GenericWorker"
    role.mkdir(parents=True)
    (role / "role.toml").write_text('core = "GenericWorker"\nlaunch = ["worker"]\nskills = ["skill-x"]\n')
    assert compose.main(["--core-dir", str(agents), "--out-dir", str(claude / "agents"), "--overlay-dir",
                         str(tmp_path / "ov"), "--roles-dir", str(claude / "roles"), "--claude-dir", str(claude)]) == 0
    capsys.readouterr()
    doctor.main(["--orch-bin", ABS, "--claude-json", str(tmp_path / "absent.json"),
                 "--brew-prefix", str(tmp_path), "--settings", str(tmp_path / "settings.json"),
                 "--codex-hooks", str(tmp_path / "codex-hooks.json"),
                 "--codex-config", str(tmp_path / "codex-config.toml"),
                 "--claude-dir", str(claude), "--codex-dir", str(tmp_path / "codex"),
                 "--compose-core-dir", str(agents), "--compose-out-dir", str(tmp_path / "elsewhere" / "agents"),
                 "--compose-roles-dir", str(claude / "roles")])
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "roles:fresh" in ln]
    assert len(lines) == 1 and "[PASS]" in lines[0], lines


def test_daemon_alive_check(tmp_path):
    from dockwright import doctor, paths, state
    c = doctor.check_daemon_alive(now=1_000.0)
    assert c.name == "daemon:alive" and c.ok and "not installed" in c.detail
    plist = paths.LAUNCH_AGENTS / "com.dockwright.daemon.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("x")
    os.utime(plist, (900.0, 900.0))
    assert doctor.check_daemon_alive(now=1_000.0).ok
    c = doctor.check_daemon_alive(now=1_200.0)
    assert not c.ok and "fix:" in c.detail
    paths.DAEMON.mkdir(parents=True)
    state.write_json_atomic(paths.DAEMON / "heartbeat.json", {"ts": 1_150.0, "errors": 2})
    c = doctor.check_daemon_alive(now=1_200.0)
    assert c.ok and "2 scan error(s)" in c.detail
    c = doctor.check_daemon_alive(now=1_400.0)
    assert not c.ok and "fix:" in c.detail
    paths.DAEMON_STOP.write_text("")
    c = doctor.check_daemon_alive(now=1_400.0)
    assert c.ok and "stopped" in c.detail


def test_daemon_alive_fails_when_enabled_and_not_installed(tmp_path, monkeypatch):
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text("[daemon]\nenabled = true\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    c = doctor.check_daemon_alive(now=1_000.0)
    assert c.name == "daemon:alive" and c.ok is False
    assert "daemon sync" in c.detail and "\n" not in c.detail


def test_daemon_alive_gives_a_reinstall_over_a_stale_heartbeat_the_install_grace(tmp_path):
    from dockwright import paths, state
    plist = paths.LAUNCH_AGENTS / "com.dockwright.daemon.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("x")
    os.utime(plist, (10_000.0, 10_000.0))
    paths.DAEMON.mkdir(parents=True)
    state.write_json_atomic(paths.DAEMON / "heartbeat.json", {"ts": 6_400.0, "errors": 0})
    c = doctor.check_daemon_alive(now=10_005.0)
    assert c.ok and "no pass yet" in c.detail
    c = doctor.check_daemon_alive(now=10_200.0)
    assert not c.ok and "no pass 200s after install" in c.detail


def test_daemon_alive_fix_names_status_and_sync_for_a_job_that_is_not_loaded(tmp_path):
    from dockwright import paths
    plist = paths.LAUNCH_AGENTS / "com.dockwright.daemon.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("x")
    os.utime(plist, (900.0, 900.0))
    c = doctor.check_daemon_alive(now=1_200.0)
    assert not c.ok
    assert "`dockwright daemon status`" in c.detail and "`dockwright daemon sync`" in c.detail


@pytest.mark.parametrize("raw", ['"true"', "1", '"yes"'])
def test_daemon_alive_fails_on_an_enabled_value_that_is_not_a_boolean(tmp_path, monkeypatch, raw):
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(f"[daemon]\nenabled = {raw}\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    c = doctor.check_daemon_alive(now=1_000.0)
    assert c.ok is False
    assert f"enabled = {raw}" in c.detail and "enabled = true" in c.detail and "\n" not in c.detail


def test_check_legacy_roster_pass_absent_pass_present_fail_unparseable(tmp_path, monkeypatch):
    from dockwright import config
    home = tmp_path / "claude"
    home.mkdir()
    monkeypatch.setattr(config, "claude_config_home", lambda: home)
    absent = doctor.check_legacy_roster()
    assert absent.ok and absent.name == "config:legacy-roster" and "no legacy roster file" in absent.detail
    (home / "dockwright-roster.toml").write_text('[roles.X]\nproviders = ["claude"]\n')
    present = doctor.check_legacy_roster()
    assert present.ok and "read only for snapshots started before roster-removal" in present.detail
    (home / "dockwright-roster.toml").write_text("[roles.X\n")
    assert not doctor.check_legacy_roster().ok


def test_check_manifest_tier_names_each_manifest_once_and_reports_a_broken_one(tmp_path, monkeypatch):
    from dockwright import roles
    root = tmp_path / "roles"
    for name, text in (("A", 'core = "a"\ntier = "X"\nlaunch = ["child"]\n'), ("B", 'core = "b"\nlaunch = ["child"]\n')):
        (root / name).mkdir(parents=True)
        (root / name / "role.toml").write_text(text)
    monkeypatch.setattr(roles, "IGNORED_TIER", set())
    clean = doctor.check_manifest_tier(root)
    assert clean.ok and clean.name == "roles:tier"
    assert clean.detail.count(str(root / "A" / "role.toml")) == 1 and "B" not in clean.detail.replace(str(root), "")
    (root / "C").mkdir()
    (root / "C" / "role.toml").write_text('core = "c"\nnope = 1\n')
    broken = doctor.check_manifest_tier(root)
    assert not broken.ok and "nope" in broken.detail
    monkeypatch.setattr(roles, "IGNORED_TIER", set())
    (root / "A" / "role.toml").write_text('core = "a"\nlaunch = ["child"]\n')
    (root / "C" / "role.toml").write_text('core = "c"\nlaunch = ["child"]\n')
    assert doctor.check_manifest_tier(root).detail == "no manifest carries tier"


def test_agent_sizes_leaves_a_profile_ceiling_to_roles_fresh(tmp_path, hermetic_config):
    c = doctor.check_agent_sizes(_core(tmp_path), tmp_path / "overlay", {},
                                 ceilings={"manager.md": {"bytes": 900}, "profiles/back.md": {"bytes": 10}})
    assert c.ok, c.detail


def test_compose_fresh_and_sizes_measure_a_composed_subagent(tmp_path, hermetic_config, monkeypatch):
    agents = _role_canon(tmp_path)
    (tmp_path / "deploy" / "roles" / "subagents").mkdir()
    (tmp_path / "deploy" / "roles" / "subagents" / "general-purpose.core.md").write_text(
        "---\nname: general-purpose\ndescription: d\n---\n# Scout\n")
    roles_dir = tmp_path / "claude" / "roles"
    (roles_dir / "Scout").mkdir(parents=True)
    (roles_dir / "Scout" / "role.toml").write_text('core = "general-purpose"\nlaunch = ["agent"]\nfragments = []\n')
    monkeypatch.setattr(config, "subagents", lambda: {"general-purpose": {"role": "Scout", "core": "general-purpose"}})
    out = tmp_path / "claude" / "agents"
    assert compose.main(["--core-dir", str(agents), "--out-dir", str(out), "--overlay-dir", str(tmp_path / "ov"),
                         "--roles-dir", str(roles_dir), "--claude-dir", str(tmp_path / "claude")]) == 0
    assert (out / "general-purpose.md").is_file()
    assert doctor.check_compose_fresh(agents, out, tmp_path / "ov", roles_dir).ok
    c = doctor.check_agent_sizes(agents, tmp_path / "ov", {}, ceilings={"general-purpose.md": {"bytes": 10}}, roles_dir=roles_dir)
    assert not c.ok and "general-purpose.md" in c.detail and "bytes > 10" in c.detail


def test_compose_fresh_reports_a_missing_project_layer_instead_of_raising(tmp_path, hermetic_config, monkeypatch):
    agents = _role_canon(tmp_path)
    role = tmp_path / "claude" / "roles" / "GenericWorker"
    role.mkdir(parents=True)
    (role / "role.toml").write_text('core = "GenericWorker"\nlaunch = ["worker"]\n')
    out = tmp_path / "claude" / "agents"
    compose.compose_agents(agents, out, tmp_path / "ov", {}, roles_dir=role.parent)
    from dockwright import roles

    def missing_layer(*args, **kwargs):
        raise roles.RoleError(f"(g) [paths] project_units_dir {tmp_path / 'gone'} is not a directory")

    monkeypatch.setattr(compose, "check_agents", missing_layer)
    c = doctor.check_compose_fresh(agents, out, tmp_path / "ov", role.parent, tmp_path / "claude")
    assert not c.ok and "project_units_dir" in c.detail


def _cfg(tmp_path, monkeypatch, body):
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(body)
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))


def test_lanes_mode_fails_when_lanes_are_off_and_the_daemon_is_disabled(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch, "[lanes]\nin_manager = false\n[daemon]\nenabled = false\n")
    c = doctor.check_lanes_mode()
    assert c.name == "lanes:mode" and not c.ok and "nothing pages the managers" in c.detail
    _cfg(tmp_path, monkeypatch, "[lanes]\nin_manager = false\n[daemon]\nenabled = true\n")
    assert doctor.check_lanes_mode().ok
    _cfg(tmp_path, monkeypatch, "")
    assert doctor.check_lanes_mode().ok


def test_lanes_mode_passes_while_the_fleet_is_paused(tmp_path, monkeypatch):
    from dockwright import paths
    _cfg(tmp_path, monkeypatch, "[lanes]\nin_manager = false\n")
    monkeypatch.setattr(paths, "DAEMON_STOP", tmp_path / "daemon-stop")
    monkeypatch.setattr(paths, "PAUSE_MANIFEST", tmp_path / "pause" / "manifest.json")
    paths.DAEMON_STOP.write_text("")
    assert not doctor.check_lanes_mode().ok
    paths.PAUSE_MANIFEST.parent.mkdir()
    paths.PAUSE_MANIFEST.write_text("{}")
    c = doctor.check_lanes_mode()
    assert c.ok and "fleet paused" in c.detail and "dockwright resume" in c.detail
    paths.DAEMON_STOP.unlink()
    _cfg(tmp_path, monkeypatch, "[lanes]\nin_manager = false\n[daemon]\nenabled = false\n")
    assert not doctor.check_lanes_mode().ok


def test_lanes_env_compares_the_table_with_the_tmux_environment(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch, '[lanes.env]\nCLAUDE_ORCH_AUTONUDGE = "1"\n')
    monkeypatch.setattr(doctor, "_tmux_global_env", lambda: {"CLAUDE_ORCH_IDLE_TTL_HOURS": "8"})
    assert doctor.check_lanes_env().ok and "nothing to compare" in doctor.check_lanes_env().detail
    _cfg(tmp_path, monkeypatch, '[lanes]\nin_manager = false\n[lanes.env]\nCLAUDE_ORCH_AUTONUDGE = "1"\n')
    monkeypatch.setattr(doctor, "_tmux_global_env", lambda: {"CLAUDE_ORCH_AUTONUDGE": "1", "CLAUDE_ORCH_IDLE_TTL_HOURS": "8"})
    c = doctor.check_lanes_env()
    assert not c.ok and "CLAUDE_ORCH_IDLE_TTL_HOURS" in c.detail and "the daemon will run without it" in c.detail
    monkeypatch.setattr(doctor, "_tmux_global_env", lambda: {"CLAUDE_ORCH_AUTONUDGE": "0"})
    assert not doctor.check_lanes_env().ok
    monkeypatch.setattr(doctor, "_tmux_global_env", lambda: {"CLAUDE_ORCH_AUTONUDGE": "1", "PATH": "/usr/bin"})
    assert doctor.check_lanes_env().ok
    monkeypatch.setattr(doctor, "_tmux_global_env", lambda: None)
    c = doctor.check_lanes_env()
    assert c.ok and "tmux not reachable" in c.detail


def _tmux_env(tmp_path, monkeypatch, live, panes=(), envs=None, prepend="", ps_failed=False):
    _cfg(tmp_path, monkeypatch, "")
    core = tmp_path / "core"
    core.mkdir(exist_ok=True)
    (core / "vars.defaults.toml").write_text(f"[agent_vars]\ntmux_path_prepend = '{prepend}'\n")
    monkeypatch.setattr(doctor, "_tmux_global_env", lambda: live)
    monkeypatch.setattr(doctor, "_tmux_panes", lambda: list(panes))
    monkeypatch.setattr(doctor, "_process_envs", lambda pids: None if ps_failed else dict(envs or {}))
    return core


def test_tmux_env_flags_a_token_or_direnv_state_in_the_global_environment(tmp_path, monkeypatch):
    core = _tmux_env(tmp_path, monkeypatch, {"GH_TOKEN": "x", "DIRENV_DIFF": "y", "PATH": "/usr/bin"})
    c = doctor.check_tmux_env(core)
    assert c.name == "tmux:env" and not c.ok
    assert "set-environment -gu GH_TOKEN" in c.detail and "set-environment -gu DIRENV_DIFF" in c.detail
    assert "GITHUB_TOKEN" not in c.detail and "DIRENV_FILE" not in c.detail


def test_tmux_env_compares_the_global_path_with_the_prepend(tmp_path, monkeypatch):
    core = _tmux_env(tmp_path, monkeypatch, {"PATH": "/opt/homebrew/bin:/opt/operator-tools/bin"},
                     prepend="/opt/operator-tools/bin")
    c = doctor.check_tmux_env(core)
    assert not c.ok
    assert "set-environment -g PATH /opt/operator-tools/bin:/opt/homebrew/bin:/opt/operator-tools/bin" in c.detail
    core = _tmux_env(tmp_path, monkeypatch, {"PATH": "/opt/operator-tools/bin:/usr/bin"},
                     prepend="/opt/operator-tools/bin")
    c = doctor.check_tmux_env(core)
    assert c.ok and "PATH starts with /opt/operator-tools/bin" in c.detail


def test_tmux_env_flags_a_global_path_that_repeats_the_prepend(tmp_path, monkeypatch):
    prepend = "/opt/[ab]/bin"
    core = _tmux_env(tmp_path, monkeypatch, {"PATH": f"{prepend}:{prepend}:{prepend}:/usr/bin"}, prepend=prepend)
    c = doctor.check_tmux_env(core)
    assert not c.ok and "starts with /opt/[ab]/bin 3 times" in c.detail and "the shell that started the server" in c.detail
    assert "set-environment -g PATH '/opt/[ab]/bin:/usr/bin'" in c.detail


def test_tmux_env_expands_the_home_token_in_the_prepend(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    core = _tmux_env(tmp_path, monkeypatch, {"PATH": f"{tmp_path}/tools:/usr/bin"},
                     prepend="<absolute-home>/tools")
    assert doctor.check_tmux_env(core).ok


def test_tmux_env_flags_only_a_pane_token_its_own_envrc_did_not_set(tmp_path, monkeypatch):
    work = tmp_path / "work"
    panes = [("101", "w:leak", str(tmp_path / "personal" / "repo")),
             ("102", "w:own", str(work / "repo"))]
    envs = {"101": {"GH_TOKEN": "x", "DIRENV_FILE": str(work / ".envrc")},
            "102": {"GH_TOKEN": "x", "DIRENV_FILE": str(work / ".envrc")}}
    core = _tmux_env(tmp_path, monkeypatch, {"PATH": "/usr/bin"}, panes, envs)
    c = doctor.check_tmux_env(core)
    assert not c.ok
    assert "w:leak (GH_TOKEN)" in c.detail and "w:own" not in c.detail
    assert "/recreate-manager" in c.detail


def test_tmux_env_counts_a_pane_whose_environment_it_cannot_read(tmp_path, monkeypatch):
    core = _tmux_env(tmp_path, monkeypatch, {"PATH": "/usr/bin"},
                     [("103", "w:shell", "/x"), ("104", "w:claude", "/y")], {"104": {"HOME": "/home/u"}})
    c = doctor.check_tmux_env(core)
    assert c.ok and "1 pane(s) read, 1 unreadable" in c.detail


def test_tmux_env_fails_when_ps_cannot_read_the_panes(tmp_path, monkeypatch):
    core = _tmux_env(tmp_path, monkeypatch, {"PATH": "/usr/bin"}, [("103", "w:shell", "/x")], ps_failed=True)
    c = doctor.check_tmux_env(core)
    assert not c.ok and "ps failed or timed out" in c.detail and "platform binary" not in c.detail


@pytest.mark.skipif(Path("/proc").is_dir(), reason="reads /proc, not ps")
def test_process_envs_returns_none_when_ps_cannot_run(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert doctor._process_envs(["1"]) is None


def test_tmux_env_passes_when_tmux_is_unreachable(tmp_path, monkeypatch):
    core = _tmux_env(tmp_path, monkeypatch, None)
    c = doctor.check_tmux_env(core)
    assert c.ok and "tmux not reachable" in c.detail


def test_parse_ps_env_keeps_a_value_with_spaces():
    env = doctor._parse_ps_env(" HOME=/Users/a b DIRENV_FILE=/Users/a b/w/.envrc GH_TOKEN=abc")
    assert env == {"HOME": "/Users/a b", "DIRENV_FILE": "/Users/a b/w/.envrc", "GH_TOKEN": "abc"}


def test_process_envs_reads_the_environment_of_a_live_process_and_not_its_arguments(tmp_path):
    home = tmp_path / "a b"
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "DIRENV_FILE": str(home / "w" / ".envrc")}
    proc = subprocess.Popen([sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)",
                             "GH_TOKEN=argv"], env=env, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        got = doctor._process_envs([str(proc.pid)])[str(proc.pid)]
    finally:
        proc.kill()
        proc.wait()
    assert "GH_TOKEN" not in got
    assert got["HOME"] == str(home) and got["DIRENV_FILE"] == str(home / "w" / ".envrc")


def test_process_envs_reads_a_process_whose_environment_is_not_utf8(tmp_path):
    env = {"PATH": os.environ["PATH"], "NOT_UTF8": os.fsdecode(b"caf\xe9"), "DIRENV_FILE": str(tmp_path / ".envrc")}
    proc = subprocess.Popen([sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)"],
                            env=env, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        got = doctor._process_envs([str(proc.pid)])[str(proc.pid)]
    finally:
        proc.kill()
        proc.wait()
    assert got["DIRENV_FILE"] == str(tmp_path / ".envrc")


@pytest.mark.real_tmux
def test_tmux_env_reads_the_environment_of_real_panes(real_tmux, monkeypatch, tmp_path):
    from dockwright import terminal
    for var in (*doctor.TOKEN_VARS, *doctor.DIRENV_VARS):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NOT_UTF8", os.fsdecode(b"caf\xe9"))
    sleeper = [sys.executable, "-c", "import time; time.sleep(600)"]
    core = tmp_path / "core"
    core.mkdir()
    (core / "vars.defaults.toml").write_text("[agent_vars]\ntmux_path_prepend = ''\n")
    _cfg(tmp_path, monkeypatch, "")
    own = tmp_path / "own"
    own.mkdir()
    drv = terminal.TmuxDriver()
    asyncio.run(drv.spawn(cwd=str(tmp_path), title="leak",
                          argv=["env", "GH_TOKEN=leak", *sleeper], route_to_workers_window=True))
    asyncio.run(drv.spawn(cwd=str(own), title="own",
                          argv=["env", "GH_TOKEN=own", f"DIRENV_FILE={own / '.envrc'}", *sleeper],
                          route_to_workers_window=True))
    deadline = time.time() + 5
    c = doctor.check_tmux_env(core)
    while ("claude-workers:leak" not in c.detail or "unreadable" in c.detail) and time.time() < deadline:
        time.sleep(0.1)
        c = doctor.check_tmux_env(core)
    assert not c.ok
    assert "claude-workers:leak (GH_TOKEN)" in c.detail
    assert "claude-workers:own" not in c.detail
    assert "unreadable" not in c.detail


def test_gh_logins_passes_unset_and_checks_each_login(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch, "")
    assert doctor.check_gh_logins().ok and "unset" in doctor.check_gh_logins().detail
    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\n[ \"$6\" = alice ] && { echo ghp-secret-xyz; exit 0; }\nexit 1\n")
    gh.chmod(0o755)
    _cfg(tmp_path, monkeypatch, f'[gh]\nbinary = "{gh}"\n[gh.logins]\nacme = "alice"\nwork = "bob"\n')
    c = doctor.check_gh_logins()
    assert not c.ok and "bob" in c.detail and "ghp-secret-xyz" not in c.detail
    _cfg(tmp_path, monkeypatch, f'[gh]\nbinary = "{gh}"\n[gh.logins]\nacme = "alice"\n')
    assert doctor.check_gh_logins().ok
    _cfg(tmp_path, monkeypatch, f'[gh]\nbinary = "{tmp_path / "nope"}"\n')
    assert not doctor.check_gh_logins().ok


def test_schedules_config_reports_a_bad_cron_a_missing_cwd_and_an_unknown_schema(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch, f'[schedules.a]\nschema = "x"\ncron = "nope"\nask = "a"\ncwd = "{tmp_path}"\n')
    monkeypatch.setattr(doctor, "_schema_loads", lambda name: name == "general-work")
    c = doctor.check_schedules_config()
    assert not c.ok and "a: cron" in c.detail and "schema x" in c.detail
    _cfg(tmp_path, monkeypatch, f'[schedules.a]\nschema = "general-work"\ncron = "0 9 * * *"\nask = "a"\n'
                                f'cwd = "{tmp_path / "gone"}"\n')
    assert not doctor.check_schedules_config().ok
    _cfg(tmp_path, monkeypatch, f'[schedules.a]\nschema = "general-work"\ncron = "0 9 * * *"\nask = "a"\n'
                                f'cwd = "{tmp_path}"\n')
    assert doctor.check_schedules_config().ok
    _cfg(tmp_path, monkeypatch, "")
    assert doctor.check_schedules_config().ok


@pytest.mark.parametrize("body,ok", [("[daemon]\npush = \"macos\"\n", True), ("[daemon]\npush = \"pager\"\n", False),
                                     ("[daemon]\npush_after_sec = -1\n", False), ("[daemon]\npush_cap_per_hour = 0\n", True)])
def test_daemon_push_check(tmp_path, monkeypatch, body, ok):
    _cfg(tmp_path, monkeypatch, body)
    assert doctor.check_daemon_push().ok is ok


def test_daemon_alive_also_wants_the_wake_plist_when_enabled(tmp_path, monkeypatch):
    from dockwright import daemon, paths, state
    _cfg(tmp_path, monkeypatch, "[daemon]\nenabled = true\n")
    daemon.plist_path().parent.mkdir(parents=True)
    daemon.plist_path().write_text("x")
    paths.DAEMON.mkdir(parents=True)
    state.write_json_atomic(paths.DAEMON / "heartbeat.json", {"ts": time.time(), "errors": 0})
    c = doctor.check_daemon_alive()
    assert not c.ok and "daemon-wake" in c.detail and "daemon sync" in c.detail
    daemon.wake_plist_path().write_text("x")
    assert doctor.check_daemon_alive().ok


def test_lanes_mode_fails_on_an_unparseable_config_and_on_the_stop_file_in_daemon_mode(tmp_path, monkeypatch):
    from dockwright import paths
    _cfg(tmp_path, monkeypatch, "[lanes]\nin_manager = false\n[daemon]\nenabled = true\n[gh.logins]\nacme = alice-unquoted\n")
    c = doctor.check_lanes_mode()
    assert not c.ok and "does not parse" in c.detail
    _cfg(tmp_path, monkeypatch, "[lanes]\nin_manager = false\n[daemon]\nenabled = true\n")
    paths.DAEMON_STOP.parent.mkdir(parents=True, exist_ok=True)
    paths.DAEMON_STOP.write_text("")
    c = doctor.check_lanes_mode()
    assert not c.ok and "daemon-stop" in c.detail and "nothing pages the managers" in c.detail
    paths.DAEMON_STOP.unlink()
    assert doctor.check_lanes_mode().ok

def test_lanes_mode_and_lanes_env_read_the_effective_mode(tmp_path, monkeypatch):
    from dockwright import lane_io, paths
    _cfg(tmp_path, monkeypatch, "[daemon]\nenabled = true\n")
    lane_io.remember_lanes_mode(False)
    c = doctor.check_lanes_mode()
    assert c.ok and "the daemon" in c.detail
    monkeypatch.setattr(doctor, "_tmux_global_env", lambda: {"CLAUDE_ORCH_AUTONUDGE": "1"})
    assert not doctor.check_lanes_env().ok
    paths.DAEMON_STOP.parent.mkdir(parents=True, exist_ok=True)
    paths.DAEMON_STOP.write_text("")
    assert not doctor.check_lanes_mode().ok and "daemon-stop" in doctor.check_lanes_mode().detail
    paths.DAEMON_STOP.unlink()
    _cfg(tmp_path, monkeypatch, '[lanes]\nin_manager = "false"\n[daemon]\nenabled = true\n')
    c = doctor.check_lanes_mode()
    assert not c.ok and "is not a boolean" in c.detail


def test_check_chat_archive_reports_disabled_missing_keys_ignored_scout_keys_a_stale_ledger_and_repeated_errors(tmp_path, monkeypatch):
    import importlib
    import json
    from dockwright import paths
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    importlib.reload(paths)
    try:
        _cfg(tmp_path, monkeypatch, "")
        c = doctor.check_chat_archive()
        assert c.ok and c.name == "chat-archive:config" and c.detail == "chat-archive: off"
        _cfg(tmp_path, monkeypatch, '[chat_archive]\nenabled = false\n')
        assert doctor.check_chat_archive().ok
        _cfg(tmp_path, monkeypatch, '[chat_archive]\nenabled = true\nmcp_url = "https://x"\n')
        c = doctor.check_chat_archive()
        assert not c.ok and "mcp_server is empty" in c.detail and "tools.search is empty" in c.detail
        _cfg(tmp_path, monkeypatch, '[events.sources.chat]\nscout_tools = ["a"]\n[events.sources.chat.scout_mcp.x]\nurl = "u"\n')
        c = doctor.check_chat_archive()
        assert not c.ok and "scout_tools is no longer read" in c.detail and "scout_mcp is no longer read" in c.detail
        full = ('[chat_archive]\nenabled = true\nmcp_url = "https://x"\nmcp_server = "s"\nkeychain_service = "k"\n'
                'self_user_id = "U1"\nwrite_tools = ["w"]\n[chat_archive.tools]\nsearch = "a"\nread_conversation = "b"\nread_thread = "c"\n'
                'list_conversations = "d"\n')
        _cfg(tmp_path, monkeypatch, full)
        c = doctor.check_chat_archive()
        assert not c.ok and "no ledger" in c.detail
        paths.CHAT.mkdir(parents=True, exist_ok=True)
        now = 1793106700.0
        monkeypatch.setattr(doctor.time, "time", lambda: now)
        paths.CHAT_LEDGER.write_text(json.dumps({"ts": now - 1000, "kind": "pass", "calls": 0, "errors": []}) + "\n")
        c = doctor.check_chat_archive()
        assert not c.ok and "old" in c.detail
        records = [{"ts": now - 400, "kind": "pass", "calls": 2, "errors": ["HTTPError 503"]}] + [
            {"ts": now - 300 + i, "kind": "pass", "calls": 0, "errors": []} for i in range(3)]
        paths.CHAT_LEDGER.write_text("".join(json.dumps(r) + "\n" for r in records))
        c = doctor.check_chat_archive()
        assert not c.ok and "last pass with calls: HTTPError 503" in c.detail
        paths.CHAT_LEDGER.write_text(json.dumps({"ts": now - 60, "kind": "pass", "calls": 2, "errors": []}) + "\n")
        c = doctor.check_chat_archive()
        assert c.ok and "chat-archive: on" in c.detail
        _cfg(tmp_path, monkeypatch, full.replace('runner', 'x') + 'runner = "manager"\n')
        assert doctor.check_chat_archive().ok
    finally:
        monkeypatch.undo()
        importlib.reload(paths)


def test_check_chat_archive_names_an_unset_sent_footer_in_the_detail_of_a_passing_check(tmp_path, monkeypatch):
    import importlib
    import json
    from dockwright import paths
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    importlib.reload(paths)
    try:
        full = ('[chat_archive]\nenabled = true\nmcp_url = "https://x"\nmcp_server = "s"\nkeychain_service = "k"\n'
                'self_user_id = "U1"\nwrite_tools = ["w"]\n[chat_archive.tools]\nsearch = "a"\nread_conversation = "b"\n'
                'read_thread = "c"\nlist_conversations = "d"\n')
        now = 1793106700.0
        monkeypatch.setattr(doctor.time, "time", lambda: now)
        paths.CHAT.mkdir(parents=True, exist_ok=True)
        paths.CHAT_LEDGER.write_text(json.dumps({"ts": now - 60, "kind": "pass", "calls": 2, "errors": []}) + "\n")
        _cfg(tmp_path, monkeypatch, full)
        c = doctor.check_chat_archive()
        assert c.ok and "sent_footer unset: reply takeover and the footer backstop are off" in c.detail
        _cfg(tmp_path, monkeypatch, full.replace('self_user_id = "U1"\n',
                                                 'self_user_id = "U1"\nsent_footer = "*Sent using* <@U01APPAPP|SomeApp>"\n'))
        c = doctor.check_chat_archive()
        assert c.ok and "sent_footer unset" not in c.detail
    finally:
        monkeypatch.undo()
        importlib.reload(paths)


def test_check_chat_archive_fails_on_an_empty_self_user_id_while_enabled(tmp_path, monkeypatch):
    import importlib
    import json
    from dockwright import paths
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    importlib.reload(paths)
    try:
        full = ('[chat_archive]\nenabled = true\nmcp_url = "https://x"\nmcp_server = "s"\nkeychain_service = "k"\n'
                'self_user_id = "U1"\nwrite_tools = ["w"]\n[chat_archive.tools]\nsearch = "a"\nread_conversation = "b"\n'
                'read_thread = "c"\nlist_conversations = "d"\n')
        now = 1793106700.0
        monkeypatch.setattr(doctor.time, "time", lambda: now)
        paths.CHAT.mkdir(parents=True, exist_ok=True)
        paths.CHAT_LEDGER.write_text(json.dumps({"ts": now - 60, "kind": "pass", "calls": 2, "errors": []}) + "\n")
        _cfg(tmp_path, monkeypatch, full)
        assert doctor.check_chat_archive().ok
        _cfg(tmp_path, monkeypatch, full.replace('self_user_id = "U1"\n', ""))
        c = doctor.check_chat_archive()
        assert not c.ok and "self_user_id is empty: no chat event is built from the archive" in c.detail
        _cfg(tmp_path, monkeypatch, full.replace('self_user_id = "U1"\n', "").replace("enabled = true", "enabled = false"))
        assert doctor.check_chat_archive().ok
    finally:
        monkeypatch.undo()
        importlib.reload(paths)


def test_events_config_reports_the_env_file_an_orphaned_subscription_and_a_bad_rule(tmp_path, monkeypatch):
    from dockwright import paths
    env = tmp_path / "missing"
    _cfg(tmp_path, monkeypatch, f'[events]\nenabled = true\nenv_file = "{env}"\n')
    c = doctor.check_events_config()
    assert not c.ok and str(env) in c.detail and "0600" in c.detail
    env.write_text("TRACKER_URL=x\n")
    env.chmod(0o644)
    assert not doctor.check_events_config().ok
    env.chmod(0o600)
    c = doctor.check_events_config()
    assert not c.ok and "incomplete (no TRACKER_EMAIL, TRACKER_API_TOKEN)" in c.detail
    env.write_text("TRACKER_URL=x\nTRACKER_EMAIL=e\nTRACKER_API_TOKEN=t\n")
    assert doctor.check_events_config().ok and "events: on" in doctor.check_events_config().detail
    _cfg(tmp_path, monkeypatch, f'[events]\nenabled = true\nenv_file = "{env}"\n[events.rules.x]\naction = "start"\n')
    c = doctor.check_events_config()
    assert not c.ok and "x: source is required" in c.detail
    _cfg(tmp_path, monkeypatch, f'[events]\nenabled = true\nenv_file = "{env}"\n[events.sources.exec]\n'
         '[[events.sources.exec.commands]]\nargv = ["a", "--json"]\nstop_file = ""\n'
         '[[events.sources.exec.commands]]\nargv = "b"\n')
    c = doctor.check_events_config()
    assert not c.ok and "exec a --json: stop_file must be a path" in c.detail
    assert "exec commands[1]: argv must be a list of non-empty strings" in c.detail
    _cfg(tmp_path, monkeypatch, '[events]\nenabled = false\n')
    paths.EVENTS_SUBS.mkdir(parents=True, exist_ok=True)
    (paths.EVENTS_SUBS / "abcd1234.json").write_text("{}")
    c = doctor.check_events_config()
    assert not c.ok and "EVENTS_SUBS_ORPHANED" in c.detail and "abcd1234" in c.detail
    (paths.EVENTS_SUBS / "abcd1234.json").unlink()
    c = doctor.check_events_config()
    assert c.ok and c.detail == "events: off"
    _cfg(tmp_path, monkeypatch, "")
    assert doctor.check_events_config().ok
