import json
import os
from pathlib import Path

import pytest

from dockwright import paths, spawner


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
