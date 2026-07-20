import json
import os
from pathlib import Path

import pytest

from dockwright import config, paths, spawner


def test_account_config_dir_is_sibling_of_config_home(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "CONFIG_HOME", tmp_path / ".claude")
    assert paths.account_config_dir("a") == tmp_path / ".claude-a"
    assert paths.account_config_dir("b") == tmp_path / ".claude-b"


def test_config_home_and_host_json_default_to_home():
    # test_paths.py reloads `paths` under a tmp HOME and never restores it, leaking
    # a stale CONFIG_HOME into later tests. Reload against the real env here so this
    # assertion is order-independent (and the reload heals the leaked module state).
    import importlib

    importlib.reload(paths)
    home = Path(os.environ.get("HOME", ""))
    assert paths.CONFIG_HOME == home / ".claude"
    assert paths.HOST_CLAUDE_JSON == home / ".claude.json"


@pytest.fixture
def farm(monkeypatch, tmp_path):
    """Canonical ~/.claude populated with shared assets + a host .claude.json."""
    canonical = tmp_path / ".claude"
    canonical.mkdir()
    for d in ("rules", "agents", "commands", "skills", "flows", "scripts",
              "plugins", "projects", "orchestrator"):
        (canonical / d).mkdir()
    (canonical / "rules" / "a.md").write_text("rule")
    (canonical / "settings.json").write_text("{}")
    (canonical / "settings.local.json").write_text("{}")
    (canonical / "statusline-command.sh").write_text("#!/bin/bash\n")
    (canonical / ".credentials.json").write_text('{"secret":"x"}')
    host_json = tmp_path / ".claude.json"
    host_json.write_text(json.dumps({
        "oauthAccount": {"emailAddress": "host@example.com"},
        "mcpServers": {"claude-orchestrator": {"command": "orchestrator"}},
        "projects": {"/p": {"allowedTools": []}},
    }))
    monkeypatch.setattr(paths, "CONFIG_HOME", canonical)
    monkeypatch.setattr(paths, "HOST_CLAUDE_JSON", host_json)
    monkeypatch.setattr(config, "account_config_dir_override", lambda name: None)
    return tmp_path, canonical, host_json


def test_farm_symlinks_shared_dirs_and_files(farm):
    tmp, canonical, _ = farm
    out = spawner.ensure_account_config_dir("b")
    assert out == tmp / ".claude-b"
    for name in ("rules", "agents", "commands", "skills", "flows", "scripts",
                 "plugins", "projects", "orchestrator",
                 "settings.json", "settings.local.json", "statusline-command.sh"):
        link = out / name
        assert link.is_symlink(), f"{name} should be a symlink"
        assert os.readlink(link) == str(canonical / name)
    assert (out / "rules" / "a.md").read_text() == "rule"


def test_farm_skips_missing_shared_entries(farm):
    tmp, canonical, _ = farm
    (canonical / "plugins").rmdir()
    out = spawner.ensure_account_config_dir("b")
    assert not (out / "plugins").exists()
    assert (out / "rules").is_symlink()


def test_farm_projects_is_symlinked_load_bearing(farm):
    out = spawner.ensure_account_config_dir("b")
    assert (out / "projects").is_symlink()


def test_farm_no_credentials_json(farm):
    out = spawner.ensure_account_config_dir("b")
    assert not (out / ".credentials.json").exists()


def test_claude_json_is_real_file_with_mcp_and_no_oauth(farm):
    out = spawner.ensure_account_config_dir("b")
    cj = out / ".claude.json"
    assert cj.is_file() and not cj.is_symlink()
    data = json.loads(cj.read_text())
    assert "oauthAccount" not in data
    assert "claude-orchestrator" in data["mcpServers"]
    assert data["projects"] == {"/p": {"allowedTools": []}}


def test_idempotent_rerun(farm):
    a = spawner.ensure_account_config_dir("b")
    first = os.readlink(a / "rules")
    spawner.ensure_account_config_dir("b")
    assert os.readlink(a / "rules") == first


def test_self_heal_wrong_target_symlink(farm):
    tmp, canonical, _ = farm
    out = tmp / ".claude-b"
    out.mkdir()
    (out / "rules").symlink_to(tmp / "WRONG")
    spawner.ensure_account_config_dir("b")
    assert os.readlink(out / "rules") == str(canonical / "rules")


def test_self_heal_missing_symlink(farm):
    out = spawner.ensure_account_config_dir("b")
    (out / "rules").unlink()
    spawner.ensure_account_config_dir("b")
    assert (out / "rules").is_symlink()


def test_self_heal_missing_claude_json(farm):
    out = spawner.ensure_account_config_dir("b")
    (out / ".claude.json").unlink()
    spawner.ensure_account_config_dir("b")
    assert (out / ".claude.json").is_file()


def test_healthy_claude_json_not_rebuilt(farm):
    out = spawner.ensure_account_config_dir("b")
    cj = out / ".claude.json"
    cj.write_text(json.dumps({
        "mcpServers": {"claude-orchestrator": {"command": "orchestrator"}},
        "marker": "keep-me",
    }))
    spawner.ensure_account_config_dir("b")
    assert json.loads(cj.read_text()).get("marker") == "keep-me"


def test_claude_json_healthy_accepts_both_generation_mcp_keys(tmp_path):
    cj = tmp_path / ".claude.json"
    cj.write_text(json.dumps({"mcpServers": {"dockwright": {"command": "dockwright"}}}))
    assert spawner._claude_json_healthy(cj)
    cj.write_text(json.dumps({"mcpServers": {"claude-orchestrator": {"command": "orchestrator"}}}))
    assert spawner._claude_json_healthy(cj)
    cj.write_text(json.dumps({"mcpServers": {"some-other-tool": {}}}))
    assert not spawner._claude_json_healthy(cj)


def test_dockwright_keyed_claude_json_not_rebuilt(farm):
    out = spawner.ensure_account_config_dir("b")
    cj = out / ".claude.json"
    cj.write_text(json.dumps({
        "mcpServers": {"dockwright": {"command": "dockwright"}},
        "marker": "keep-me",
    }))
    spawner.ensure_account_config_dir("b")
    assert json.loads(cj.read_text()).get("marker") == "keep-me"


def test_claude_json_rebuilt_when_mcp_absent(farm):
    out = spawner.ensure_account_config_dir("b")
    cj = out / ".claude.json"
    cj.write_text(json.dumps({"mcpServers": {}, "marker": "drop-me"}))
    spawner.ensure_account_config_dir("b")
    data = json.loads(cj.read_text())
    assert "claude-orchestrator" in data["mcpServers"]
    assert "marker" not in data


def test_claude_json_symlink_replaced_with_real(farm):
    tmp, canonical, host = farm
    out = tmp / ".claude-b"
    out.mkdir()
    (out / ".claude.json").symlink_to(host)
    spawner.ensure_account_config_dir("b")
    cj = out / ".claude.json"
    assert cj.is_file() and not cj.is_symlink()


def test_real_dir_drift_left_intact(farm):
    tmp, canonical, _ = farm
    out = tmp / ".claude-b"
    out.mkdir()
    real = out / "rules"
    real.mkdir()
    (real / "local.md").write_text("dont-destroy")
    spawner.ensure_account_config_dir("b")
    assert (out / "rules" / "local.md").read_text() == "dont-destroy"
    assert not (out / "rules").is_symlink()


def test_host_claude_json_unreadable_is_best_effort(farm):
    tmp, canonical, host = farm
    host.write_text("not-json{{{")
    out = spawner.ensure_account_config_dir("b")
    assert (out / "rules").is_symlink()
    assert not (out / ".claude.json").exists()


def test_host_claude_json_non_dict_is_best_effort(farm):
    tmp, canonical, host = farm
    host.write_text(json.dumps(["not", "a", "dict"]))
    out = spawner.ensure_account_config_dir("b")  # must not raise
    assert (out / "rules").is_symlink()
    assert not (out / ".claude.json").exists()


def test_prefix_none_is_empty():
    assert spawner._build_account_prefix(None) == ""


def test_prefix_account_a_no_config_dir(monkeypatch):
    # Account 'a' == the default ~/.claude: NO CLAUDE_CONFIG_DIR, NO farm build.
    def _boom(letter):
        raise AssertionError("account 'a' must NOT build a farm")
    monkeypatch.setattr(spawner, "ensure_account_config_dir", _boom)
    out = spawner._build_account_prefix("a")
    assert out == "CLAUDE_ORCH_ACCOUNT=a "
    assert "CLAUDE_CONFIG_DIR" not in out
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in out


def test_prefix_account_b_exports_config_dir(monkeypatch, tmp_path):
    farm_dir = tmp_path / ".claude-b"
    farm_dir.mkdir()
    (farm_dir / ".claude.json").write_text(
        json.dumps({"mcpServers": {"claude-orchestrator": {"command": "orchestrator"}}})
    )
    monkeypatch.setattr(spawner, "ensure_account_config_dir", lambda letter: farm_dir)
    out = spawner._build_account_prefix("b")
    assert f"CLAUDE_CONFIG_DIR={farm_dir}" in out
    assert "CLAUDE_ORCH_ACCOUNT=b" in out
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in out


def test_prefix_b_unhealthy_farm_falls_back_to_a(monkeypatch, tmp_path):
    # Partial farm: builder returns a dir whose .claude.json is missing → the worker
    # must NOT be pinned to it (would have no orchestrator MCP); fall back to the
    # default login with a TRUTHFUL effective stamp 'a'.
    farm_dir = tmp_path / ".claude-b"
    farm_dir.mkdir()  # built, but no .claude.json written
    monkeypatch.setattr(spawner, "ensure_account_config_dir", lambda letter: farm_dir)
    out = spawner._build_account_prefix("b")
    assert "CLAUDE_CONFIG_DIR" not in out
    assert out == "CLAUDE_ORCH_ACCOUNT=a "  # effective stamp 'a'


def test_prefix_b_unhealthy_farm_non_dict_falls_back_to_a(monkeypatch, tmp_path):
    farm_dir = tmp_path / ".claude-b"
    farm_dir.mkdir()
    (farm_dir / ".claude.json").write_text(json.dumps(["not", "a", "dict"]))
    monkeypatch.setattr(spawner, "ensure_account_config_dir", lambda letter: farm_dir)
    out = spawner._build_account_prefix("b")
    assert "CLAUDE_CONFIG_DIR" not in out
    assert out == "CLAUDE_ORCH_ACCOUNT=a "


def test_prefix_b_unhealthy_farm_lacks_orch_mcp_falls_back_to_a(monkeypatch, tmp_path):
    farm_dir = tmp_path / ".claude-b"
    farm_dir.mkdir()
    (farm_dir / ".claude.json").write_text(json.dumps({"mcpServers": {}}))
    monkeypatch.setattr(spawner, "ensure_account_config_dir", lambda letter: farm_dir)
    out = spawner._build_account_prefix("b")
    assert "CLAUDE_CONFIG_DIR" not in out
    assert out == "CLAUDE_ORCH_ACCOUNT=a "


def test_prefix_b_dockwright_keyed_farm_is_healthy(monkeypatch, tmp_path):
    farm_dir = tmp_path / ".claude-b"
    farm_dir.mkdir()
    (farm_dir / ".claude.json").write_text(
        json.dumps({"mcpServers": {"dockwright": {"command": "dockwright"}}})
    )
    monkeypatch.setattr(spawner, "ensure_account_config_dir", lambda letter: farm_dir)
    out = spawner._build_account_prefix("b")
    assert f"CLAUDE_CONFIG_DIR={farm_dir}" in out
    assert "CLAUDE_ORCH_ACCOUNT=b" in out


def test_prefix_b_build_oserror_falls_back_to_a(monkeypatch):
    def _fail(letter):
        raise OSError("disk full")
    monkeypatch.setattr(spawner, "ensure_account_config_dir", _fail)
    out = spawner._build_account_prefix("b")
    assert "CLAUDE_CONFIG_DIR" not in out
    assert out == "CLAUDE_ORCH_ACCOUNT=a "


def test_farm_new_canonical_entry_auto_symlinks(farm):
    tmp, canonical, _ = farm
    (canonical / "brand-new-dir").mkdir()
    (canonical / "brand-new-dir" / "x.md").write_text("new")
    (canonical / "loops-registry.md").write_text("loops")
    out = spawner.ensure_account_config_dir("b")
    assert (out / "brand-new-dir").is_symlink()
    assert os.readlink(out / "brand-new-dir") == str(canonical / "brand-new-dir")
    assert (out / "brand-new-dir" / "x.md").read_text() == "new"
    assert (out / "loops-registry.md").is_symlink()


def test_farm_denies_runtime_and_junk_entries(farm):
    tmp, canonical, _ = farm
    denied = [
        "cache", "sessions", "shell-snapshots", "session-env", "paste-cache",
        "file-history", "ide", "debug", "backups", "telemetry",
    ]
    for d in denied:
        (canonical / d).mkdir()
    for f in (
        "history.jsonl", "mcp-needs-auth-cache.json", "policy-limits.json",
        "remote-settings.json", "stats-cache.json", ".DS_Store",
        ".last-cleanup", ".last-update-result.json",
        "settings.json.bak.1779107346", "settings.json.bak-distillloop",
    ):
        (canonical / f).write_text("x")
    (canonical / ".git").mkdir()
    out = spawner.ensure_account_config_dir("b")
    for name in denied + [
        "history.jsonl", "mcp-needs-auth-cache.json", "policy-limits.json",
        "remote-settings.json", "stats-cache.json", ".DS_Store", ".git",
        ".last-cleanup", ".last-update-result.json",
        "settings.json.bak.1779107346", "settings.json.bak-distillloop",
    ]:
        assert not (out / name).exists() and not (out / name).is_symlink(), \
            f"{name} must not be symlinked"
    # functional settings.json still shares
    assert (out / "settings.json").is_symlink()


def test_farm_denies_sibling_farm_dirs(farm):
    tmp, canonical, _ = farm
    (canonical / ".claude-x").mkdir()  # defensive: never a child in reality
    out = spawner.ensure_account_config_dir("b")
    assert not (out / ".claude-x").exists() and not (out / ".claude-x").is_symlink()


def test_farm_repairs_dangling_symlink(farm):
    tmp, canonical, _ = farm
    out = tmp / ".claude-b"
    out.mkdir()
    (out / "rules").symlink_to(tmp / "GONE")  # dangling: target absent
    assert (out / "rules").is_symlink() and not (out / "rules").exists()
    spawner.ensure_account_config_dir("b")
    assert os.readlink(out / "rules") == str(canonical / "rules")
    assert (out / "rules").exists()


def test_farm_real_dir_drift_warns(farm, caplog):
    import logging
    tmp, canonical, _ = farm
    out = tmp / ".claude-b"
    out.mkdir()
    real = out / "rules"
    real.mkdir()
    (real / "local.md").write_text("keep")
    spawner._warned_drift.clear()
    with caplog.at_level(logging.WARNING):
        spawner.ensure_account_config_dir("b")
    assert (out / "rules" / "local.md").read_text() == "keep"
    assert not (out / "rules").is_symlink()
    assert any("rules" in r.message for r in caplog.records), \
        "expected a drift warning naming the real path"


def test_ensure_symlink_noop_when_correct_even_if_target_absent(tmp_path):
    # M4(a): a dangling link already pointing at the desired (absent) target hits
    # the no-op branch — left as-is, no churn, no error.
    target = tmp_path / "GONE"
    link = tmp_path / "link"
    link.symlink_to(target)
    spawner._ensure_symlink(link, target)
    assert link.is_symlink()
    assert os.readlink(link) == str(target)


def test_farm_real_file_drift_left_intact_and_warns(farm, caplog):
    # M4(b): a real FILE (not dir) where a symlink belongs is left intact + warned.
    import logging
    tmp, canonical, _ = farm
    out = tmp / ".claude-b"
    out.mkdir()
    (out / "statusline-command.sh").write_text("local-real")  # canonical has it as a file
    spawner._warned_drift.clear()
    with caplog.at_level(logging.WARNING):
        spawner.ensure_account_config_dir("b")
    assert (out / "statusline-command.sh").read_text() == "local-real"
    assert not (out / "statusline-command.sh").is_symlink()
    assert any("statusline-command.sh" in r.message for r in caplog.records)


def test_farm_drift_warning_deduped_across_spawns(farm, caplog):
    # M3: a persistent drift warns exactly once across repeated assembly.
    import logging
    tmp, canonical, _ = farm
    out = tmp / ".claude-b"
    out.mkdir()
    (out / "rules").mkdir()
    (out / "rules" / "x.md").write_text("keep")
    spawner._warned_drift.clear()
    with caplog.at_level(logging.WARNING):
        spawner.ensure_account_config_dir("b")
        spawner.ensure_account_config_dir("b")
    drift = [r for r in caplog.records
             if "config-dir drift" in r.message and "rules" in r.message]
    assert len(drift) == 1, f"expected exactly 1 deduped warning, got {len(drift)}"
    assert (out / "rules" / "x.md").read_text() == "keep"


def test_bp1_to_bp2_upgrade_reassembles_cleanly(farm):
    # M4(c): a pre-existing BP-1 allowlist-era farm (real .claude.json + old symlink
    # set + a real claude-runtime dir) re-assembles cleanly with no data loss.
    tmp, canonical, host = farm
    out = tmp / ".claude-b"
    out.mkdir()
    for name in ("rules", "agents", "commands", "settings.json"):
        (out / name).symlink_to(canonical / name)
    (out / ".claude.json").write_text(json.dumps({
        "mcpServers": {"claude-orchestrator": {"command": "orchestrator"}},
        "marker": "bp1-keep",
    }))
    (out / "sessions").mkdir()  # claude-runtime real dir from a BP-1 worker
    (out / "sessions" / "s.json").write_text("session-data")
    (canonical / "selffix-findings").mkdir()  # NEW functional dir BP-1 omitted
    (canonical / "selffix-findings" / "f.md").write_text("finding")
    spawner.ensure_account_config_dir("b")
    assert (out / "rules").is_symlink()                       # old symlink preserved
    assert json.loads((out / ".claude.json").read_text())["marker"] == "bp1-keep"  # healthy json kept
    assert (out / "sessions" / "s.json").read_text() == "session-data"  # runtime dir untouched
    assert not (out / "sessions").is_symlink()
    assert (out / "selffix-findings").is_symlink()            # new functional dir now shared
    assert (out / "selffix-findings" / "f.md").read_text() == "finding"


def test_farm_never_symlink_credential_pattern_fallback():
    # I1: fail-closed credential boundary denies future secret-named files.
    for denied in ("auth-token.json", ".SECRET", "foo-oauth-bar", "creds.txt", "my-credential"):
        assert spawner._farm_never_symlink(denied), f"{denied} should be denied (credential pattern)"
    for ok in ("rules", "selffix-findings", "loops-registry.md", "manager-memory",
               "gardener", "settings.json", "statusline-command.sh", "orchestrator"):
        assert not spawner._farm_never_symlink(ok), f"{ok} must NOT be denied"


def test_farm_denies_future_credential_named_file(farm):
    # I1 end-to-end: a future credential-named canonical entry is not symlinked.
    tmp, canonical, _ = farm
    creds = ("auth-token.json", ".some-cred", "my-oauth-cache", "app-secret.txt")
    for n in creds:
        (canonical / n).write_text("x")
    out = spawner.ensure_account_config_dir("b")
    for n in creds:
        assert not (out / n).exists() and not (out / n).is_symlink(), \
            f"{n} must be denied by the credential-pattern fallback"


def test_spawn_worker_tab_pretrusts_host_then_farm(monkeypatch, tmp_path):
    """L-11 ordering: host BEFORE _build_account_prefix (a first-build farm
    copies the host file), farm AFTER (and only when healthy)."""
    import asyncio
    import json as _json
    from dockwright import config, paths as dpaths, spawner, trust

    calls = []
    monkeypatch.setattr(
        trust, "pretrust_dir",
        lambda cwd, config_json=None: calls.append((str(cwd), config_json)) or True)
    monkeypatch.setattr(spawner, "_pick_account", lambda force=False: "b")
    monkeypatch.setattr(config, "default_account", lambda: "a")
    farm = tmp_path / "farm-b"
    farm.mkdir()
    (farm / ".claude.json").write_text(_json.dumps({"mcpServers": {"dockwright": {}}}))
    monkeypatch.setattr(dpaths, "account_config_dir", lambda letter: farm)
    prefix_seen = []
    def fake_prefix(letter):
        prefix_seen.append((letter, list(calls)))
        return ""
    monkeypatch.setattr(spawner, "_build_account_prefix", fake_prefix)

    asyncio.run(spawner.spawn_worker_tab(cwd=str(tmp_path), initial_prompt="x", name="w1"))

    assert calls[0] == (str(tmp_path), None), "host pre-trust must come first"
    assert prefix_seen[0][1] == [calls[0]], \
        "host pre-trust must land BEFORE _build_account_prefix builds the farm"
    assert calls[1] == (str(tmp_path), farm / ".claude.json")


def test_spawn_worker_tab_codex_never_pretrusts(monkeypatch, tmp_path):
    import asyncio
    from dockwright import spawner, trust
    calls = []
    monkeypatch.setattr(
        trust, "pretrust_dir",
        lambda cwd, config_json=None: calls.append(str(cwd)) or True)
    monkeypatch.setattr(spawner, "_pick_account", lambda force=False: None)
    asyncio.run(spawner.spawn_worker_tab(
        cwd=str(tmp_path), initial_prompt="x", name="w2", runtime="codex"))
    assert calls == []


def _write_farm_json(out, payload):
    (out / ".claude.json").write_text(json.dumps(payload))


def test_refresh_merges_new_host_server_into_healthy_farm(farm):
    tmp, canonical, host = farm
    out = spawner.ensure_account_config_dir("b")
    _write_farm_json(out, {
        "mcpServers": {"claude-orchestrator": {"command": "orchestrator"}},
        "oauthAccount": {"emailAddress": "pool-b@example.com"},
        "marker": "keep-me",
    })
    host.write_text(json.dumps({
        "oauthAccount": {"emailAddress": "host@example.com"},
        "mcpServers": {
            "claude-orchestrator": {"command": "orchestrator"},
            "figma": {"command": "figma-mcp"},
        },
    }))
    spawner.ensure_account_config_dir("b")
    data = json.loads((out / ".claude.json").read_text())
    assert data["mcpServers"]["figma"] == {"command": "figma-mcp"}
    assert data["marker"] == "keep-me"
    assert data["oauthAccount"] == {"emailAddress": "pool-b@example.com"}


def test_refresh_updates_changed_host_server_command(farm):
    tmp, canonical, host = farm
    out = spawner.ensure_account_config_dir("b")
    _write_farm_json(out, {
        "mcpServers": {"claude-orchestrator": {"command": "/old/venv/bin/orchestrator"}},
    })
    host.write_text(json.dumps({
        "mcpServers": {"claude-orchestrator": {"command": "/new/venv/bin/orchestrator"}},
    }))
    spawner.ensure_account_config_dir("b")
    data = json.loads((out / ".claude.json").read_text())
    assert data["mcpServers"]["claude-orchestrator"]["command"] == "/new/venv/bin/orchestrator"


def test_refresh_preserves_farm_only_server(farm):
    tmp, canonical, host = farm
    out = spawner.ensure_account_config_dir("b")
    _write_farm_json(out, {
        "mcpServers": {
            "claude-orchestrator": {"command": "orchestrator"},
            "farm-local": {"command": "special"},
        },
    })
    spawner.ensure_account_config_dir("b")
    data = json.loads((out / ".claude.json").read_text())
    assert data["mcpServers"]["farm-local"] == {"command": "special"}


def test_refresh_drops_legacy_key_after_host_rename(farm):
    tmp, canonical, host = farm
    out = spawner.ensure_account_config_dir("b")
    _write_farm_json(out, {
        "mcpServers": {"claude-orchestrator": {"command": "orchestrator"}},
    })
    host.write_text(json.dumps({
        "mcpServers": {"dockwright": {"command": "dockwright"}},
    }))
    spawner.ensure_account_config_dir("b")
    servers = json.loads((out / ".claude.json").read_text())["mcpServers"]
    assert "dockwright" in servers
    assert "claude-orchestrator" not in servers


def test_refresh_keeps_legacy_key_while_host_still_has_it(farm):
    tmp, canonical, host = farm
    out = spawner.ensure_account_config_dir("b")
    _write_farm_json(out, {
        "mcpServers": {"claude-orchestrator": {"command": "orchestrator"}},
    })
    host.write_text(json.dumps({
        "mcpServers": {
            "claude-orchestrator": {"command": "orchestrator"},
            "dockwright": {"command": "dockwright"},
        },
    }))
    spawner.ensure_account_config_dir("b")
    servers = json.loads((out / ".claude.json").read_text())["mcpServers"]
    assert "claude-orchestrator" in servers
    assert "dockwright" in servers


def test_refresh_never_strips_a_legacy_only_farm(farm):
    # Guard protecting farm health: host has NEITHER key (empty servers) →
    # the legacy key is the farm's ONLY orchestrator registration; dropping it
    # would flip the farm unhealthy. Own red→green case per drift-guard rule.
    tmp, canonical, host = farm
    out = spawner.ensure_account_config_dir("b")
    _write_farm_json(out, {
        "mcpServers": {"claude-orchestrator": {"command": "orchestrator"}},
    })
    host.write_text(json.dumps({"mcpServers": {}}))
    spawner.ensure_account_config_dir("b")
    servers = json.loads((out / ".claude.json").read_text())["mcpServers"]
    assert servers == {"claude-orchestrator": {"command": "orchestrator"}}


def test_refresh_vacuous_when_host_mcpservers_missing(farm):
    tmp, canonical, host = farm
    out = spawner.ensure_account_config_dir("b")
    before = {
        "mcpServers": {"claude-orchestrator": {"command": "orchestrator"}},
        "marker": "keep-me",
    }
    _write_farm_json(out, before)
    host.write_text(json.dumps({"oauthAccount": {"emailAddress": "h@example.com"}}))
    spawner.ensure_account_config_dir("b")
    assert json.loads((out / ".claude.json").read_text()) == before


def test_refresh_skips_non_dict_farm_mcpservers(farm):
    # _claude_json_healthy accepts a LIST containing "dockwright" (`in` works on
    # lists), so a corrupt-but-"healthy" farm reaches the refresh — it must
    # type-guard and skip, not raise.
    tmp, canonical, host = farm
    out = spawner.ensure_account_config_dir("b")
    _write_farm_json(out, {"mcpServers": ["dockwright"]})
    spawner.ensure_account_config_dir("b")  # must not raise
    assert json.loads((out / ".claude.json").read_text()) == {"mcpServers": ["dockwright"]}


def test_refresh_skips_unreadable_host(farm):
    tmp, canonical, host = farm
    out = spawner.ensure_account_config_dir("b")
    before = {"mcpServers": {"claude-orchestrator": {"command": "orchestrator"}}}
    _write_farm_json(out, before)
    host.write_text("not-json{{{")
    spawner.ensure_account_config_dir("b")  # must not raise
    assert json.loads((out / ".claude.json").read_text()) == before


def test_refresh_no_change_no_rewrite(farm, monkeypatch):
    tmp, canonical, host = farm
    out = spawner.ensure_account_config_dir("b")
    _write_farm_json(out, {
        "mcpServers": {"claude-orchestrator": {"command": "orchestrator"}},
    })
    writes = []
    real_write = spawner._atomic_write_json
    monkeypatch.setattr(spawner, "_atomic_write_json",
                        lambda t, d: writes.append(str(t)) or real_write(t, d))
    spawner.ensure_account_config_dir("b")
    assert writes == [], "converged farm .claude.json must not be rewritten"


def test_parity_report_clean_farm(farm):
    tmp, canonical, host = farm
    spawner.ensure_account_config_dir("b")
    report = spawner.farm_parity_report("b")
    assert report["exists"] is True
    assert report["config_dir"] == str(tmp / ".claude-b")
    assert report["drift"] == [] and report["missing"] == []
    # fixture canonical has 12 non-denied entries (9 dirs + 3 files)
    assert report["shared"] == 12
    assert report["claude_json"] == "in-sync"


def test_parity_report_nonexistent_farm(farm):
    report = spawner.farm_parity_report("b")
    assert report["exists"] is False
    assert report["claude_json"] == "missing"


def test_parity_report_names_real_path_drift(farm):
    tmp, canonical, _ = farm
    out = tmp / ".claude-b"
    out.mkdir()
    (out / "rules").mkdir()
    (out / "rules" / "local.md").write_text("keep")
    spawner.ensure_account_config_dir("b")
    report = spawner.farm_parity_report("b")
    assert report["drift"] == ["rules"]


def test_parity_report_is_stateless_despite_warned_drift_dedup(farm):
    # The report must re-scan; it must NOT consult spawner._warned_drift (the
    # MCP-resident spawner's once-per-process log dedup) — a prior warn must
    # not suppress a drift line.
    tmp, canonical, _ = farm
    out = tmp / ".claude-b"
    out.mkdir()
    (out / "rules").mkdir()
    spawner._warned_drift.clear()
    spawner.ensure_account_config_dir("b")   # logs + dedups the drift
    spawner.ensure_account_config_dir("b")   # suppressed log — drift persists
    r1 = spawner.farm_parity_report("b")
    r2 = spawner.farm_parity_report("b")
    assert r1["drift"] == ["rules"] and r2["drift"] == ["rules"]


def test_parity_report_legacy_keyed_claude_json(farm):
    tmp, canonical, host = farm
    out = spawner.ensure_account_config_dir("b")
    host.write_text(json.dumps({"mcpServers": {"dockwright": {"command": "dockwright"}}}))
    _write_farm_json(out, {"mcpServers": {"claude-orchestrator": {"command": "x"}}})
    assert spawner.farm_parity_report("b")["claude_json"] == "legacy-keyed"


def test_parity_report_stale_claude_json(farm):
    tmp, canonical, host = farm
    out = spawner.ensure_account_config_dir("b")
    host.write_text(json.dumps({"mcpServers": {"dockwright": {"command": "/new/bin"}}}))
    _write_farm_json(out, {"mcpServers": {"dockwright": {"command": "/old/bin"}}})
    assert spawner.farm_parity_report("b")["claude_json"] == "stale"


def test_parity_report_stale_when_legacy_lingers(farm):
    tmp, canonical, host = farm
    out = spawner.ensure_account_config_dir("b")
    host.write_text(json.dumps({"mcpServers": {"dockwright": {"command": "d"}}}))
    _write_farm_json(out, {"mcpServers": {
        "dockwright": {"command": "d"},
        "claude-orchestrator": {"command": "x"},
    }})
    assert spawner.farm_parity_report("b")["claude_json"] == "stale"


def test_parity_report_unverified_when_host_unreadable(farm):
    tmp, canonical, host = farm
    out = spawner.ensure_account_config_dir("b")
    _write_farm_json(out, {"mcpServers": {"dockwright": {"command": "d"}}})
    host.write_text("not-json{{{")
    assert spawner.farm_parity_report("b")["claude_json"] == "unverified"


def test_parity_report_unhealthy_claude_json(farm):
    tmp, canonical, host = farm
    out = spawner.ensure_account_config_dir("b")
    _write_farm_json(out, {"mcpServers": {}})
    assert spawner.farm_parity_report("b")["claude_json"] == "unhealthy"


def test_ensure_refuses_farm_aliasing_canonical(farm, monkeypatch):
    tmp, canonical, _ = farm
    monkeypatch.setattr(paths, "account_config_dir", lambda letter: canonical)
    with pytest.raises(OSError, match="aliases"):
        spawner.ensure_account_config_dir("b")


def test_ensure_refuses_farm_containing_canonical(farm, monkeypatch):
    tmp, canonical, _ = farm
    monkeypatch.setattr(paths, "account_config_dir", lambda letter: tmp)
    with pytest.raises(OSError, match="aliases"):
        spawner.ensure_account_config_dir("b")


def test_ensure_refuses_farm_inside_canonical(farm, monkeypatch):
    tmp, canonical, _ = farm
    monkeypatch.setattr(paths, "account_config_dir", lambda letter: canonical / "sub")
    with pytest.raises(OSError, match="aliases"):
        spawner.ensure_account_config_dir("b")


def test_refresh_aborts_when_live_session_writes_midwindow(farm, monkeypatch):
    # Tier-2 I-1 regression: a live same-account claude rewrites .claude.json
    # WHOLESALE via atomic replace (measured on-host: inode flips every write)
    # and takes no lock. If such a write lands between the refresh's read and
    # its replace, the refresh must ABORT — landing a stale full-file snapshot
    # reverts oauthAccount/accountUuid and can brick the account's login.
    # Injection is deterministic: the 2nd parse of the marked farm content is
    # the refresh's own read (the 1st is _claude_json_healthy), so the
    # competing write lands exactly inside the read->write window.
    tmp, canonical, host = farm
    out = spawner.ensure_account_config_dir("b")
    _write_farm_json(out, {
        "mcpServers": {"claude-orchestrator": {"command": "orchestrator"}},
        "oauthAccount": {"accountUuid": "stale-uuid-farm-marker"},
    })
    host.write_text(json.dumps({"mcpServers": {"dockwright": {"command": "dockwright"}}}))
    competing = {
        "mcpServers": {"claude-orchestrator": {"command": "orchestrator"}},
        "oauthAccount": {"accountUuid": "fresh-uuid-after-reauth"},
    }
    real_loads = json.loads
    marked_parses = {"n": 0}

    def inject_then_parse(s, *a, **k):
        text = s.decode() if isinstance(s, (bytes, bytearray)) else s
        if "stale-uuid-farm-marker" in text:
            marked_parses["n"] += 1
            if marked_parses["n"] == 2:
                (out / ".claude.json").write_text(json.dumps(competing))
        return real_loads(s, *a, **k)

    monkeypatch.setattr(spawner.json, "loads", inject_then_parse)
    spawner.ensure_account_config_dir("b")
    data = real_loads((out / ".claude.json").read_text())
    assert data["oauthAccount"] == {"accountUuid": "fresh-uuid-after-reauth"}, \
        "competing live-session write must win; the refresh must never revert oauthAccount"
    assert data["mcpServers"] == {"claude-orchestrator": {"command": "orchestrator"}}, \
        "aborted refresh must leave the competing snapshot byte-identical"
    assert not list(out.glob(".claude.json.tmp.*")), "aborted refresh must clean its tmp file"
    # self-heals on the next (quiet) ensure, preserving the fresh identity:
    spawner.ensure_account_config_dir("b")
    data = real_loads((out / ".claude.json").read_text())
    assert data["oauthAccount"] == {"accountUuid": "fresh-uuid-after-reauth"}
    assert "dockwright" in data["mcpServers"]
