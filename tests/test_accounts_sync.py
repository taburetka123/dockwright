import json

from dockwright import accounts_sync, config, paths, spawner
from dockwright.config import Account


def _registry(monkeypatch, tmp_path, names=("a", "b", "c"), default="a"):
    accounts = [Account(name=n, config_dir=None, weight=1) for n in names]
    monkeypatch.setattr(config, "accounts", lambda: accounts)
    monkeypatch.setattr(config, "default_account", lambda: default)
    monkeypatch.setattr(config, "account_config_dir_override", lambda name: None)
    canonical = tmp_path / ".claude"
    canonical.mkdir(exist_ok=True)
    (canonical / "rules").mkdir(exist_ok=True)
    (canonical / "settings.json").write_text("{}")
    host = tmp_path / ".claude.json"
    host.write_text(json.dumps({
        "oauthAccount": {"emailAddress": "host@example.com"},
        "mcpServers": {"dockwright": {"command": "dockwright"}},
    }))
    monkeypatch.setattr(paths, "CONFIG_HOME", canonical)
    monkeypatch.setattr(paths, "HOST_CLAUDE_JSON", host)
    return tmp_path


def test_bad_argv_exits_2(capsys):
    assert accounts_sync.main(["--frobnicate"]) == 2
    assert "Usage" in capsys.readouterr().err


def test_syncs_existing_farm_skips_default_and_unprovisioned(monkeypatch, tmp_path, capsys):
    _registry(monkeypatch, tmp_path)
    (tmp_path / ".claude-b").mkdir()  # b provisioned, c not, a default
    assert accounts_sync.main([]) == 0
    out = capsys.readouterr().out
    assert "account b: OK" in out
    assert "account c: no config dir" in out and "skipping" in out
    assert "account a" not in out
    # reconcile really ran: settings.json now shared
    assert (tmp_path / ".claude-b" / "settings.json").is_symlink()
    # and never provisioned c
    assert not (tmp_path / ".claude-c").exists()


def test_reports_real_path_drift(monkeypatch, tmp_path, capsys):
    _registry(monkeypatch, tmp_path)
    b = tmp_path / ".claude-b"
    b.mkdir()
    (b / "rules").mkdir()
    (b / "rules" / "local.md").write_text("keep")
    assert accounts_sync.main([]) == 0
    out = capsys.readouterr().out
    assert "drift: rules" in out
    assert (b / "rules" / "local.md").read_text() == "keep"


def test_refresh_heals_legacy_farm_json(monkeypatch, tmp_path, capsys):
    _registry(monkeypatch, tmp_path)
    b = tmp_path / ".claude-b"
    b.mkdir()
    (b / ".claude.json").write_text(json.dumps({
        "mcpServers": {"claude-orchestrator": {"command": "old"}},
        "oauthAccount": {"emailAddress": "pool-b@example.com"},
    }))
    assert accounts_sync.main([]) == 0
    data = json.loads((b / ".claude.json").read_text())
    assert "dockwright" in data["mcpServers"]
    assert "claude-orchestrator" not in data["mcpServers"]
    assert data["oauthAccount"] == {"emailAddress": "pool-b@example.com"}
    assert "account b: OK" in capsys.readouterr().out


def test_no_provisioned_farms_message(monkeypatch, tmp_path, capsys):
    _registry(monkeypatch, tmp_path)
    assert accounts_sync.main([]) == 0
    assert "no provisioned pool-account farms" in capsys.readouterr().out


def test_setup_sh_runs_accounts_sync_inside_third_guard_before_doctor():
    """Drift guard, anchored to EXECUTED lines (comments stripped): setup.sh must
    invoke `"$DOCKWRIGHT_BIN" accounts-sync` inside the THIRD FILES_ONLY guard
    block (venv=1st, MCP+hooks=2nd, worker-home/doctor=3rd), before the doctor
    gate. Proven RED by deleting the invocation line."""
    from pathlib import Path

    setup = (Path(__file__).resolve().parents[1] / "setup.sh").read_text()
    code = [ln for ln in setup.splitlines() if not ln.lstrip().startswith("#")]
    invocations = [ln for ln in code if '"$DOCKWRIGHT_BIN" accounts-sync' in ln]
    assert len(invocations) == 1, f"expected exactly 1 invocation, got {invocations}"
    guards = [i for i, ln in enumerate(code)
              if "DOCKWRIGHT_SETUP_FILES_ONLY" in ln and ln.lstrip().startswith("if ")]
    assert len(guards) == 3, f"expected exactly 3 FILES_ONLY guard ifs, got {len(guards)}"
    sync_idx = next(i for i, ln in enumerate(code) if '"$DOCKWRIGHT_BIN" accounts-sync' in ln)
    doctor_idx = next(i for i, ln in enumerate(code) if '" doctor "' in ln)
    assert guards[2] < sync_idx < doctor_idx, (
        "accounts-sync must run inside the third (worker-home/doctor) "
        "FILES_ONLY-guarded block, before the doctor gate")


def test_warning_lines_for_missing_and_bad_claude_json(monkeypatch, tmp_path, capsys):
    _registry(monkeypatch, tmp_path)
    (tmp_path / ".claude-b").mkdir()
    monkeypatch.setattr(accounts_sync.spawner, "ensure_account_config_dir",
                        lambda name: tmp_path / f".claude-{name}")
    monkeypatch.setattr(accounts_sync.spawner, "farm_parity_report", lambda name: {
        "config_dir": str(tmp_path / ".claude-b"), "exists": True, "shared": 3,
        "drift": [], "missing": ["rules"], "claude_json": "legacy-keyed"})
    assert accounts_sync.main([]) == 0
    out = capsys.readouterr().out
    assert "missing: rules" in out
    assert ".claude.json: legacy-keyed" in out
