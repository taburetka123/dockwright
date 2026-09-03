import pytest

from dockwright import config, doctor, pricing, sweep


@pytest.fixture
def cfg_env(monkeypatch, tmp_path):
    def _install(text):
        p = tmp_path / "dockwright.toml"
        p.write_text(text)
        monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
        return p
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(tmp_path / "no-config.toml"))
    return _install


def test_sweep_hint_default_is_none(cfg_env):
    assert sweep._ticket_cleanup_hint() is None


def test_sweep_hint_from_config_and_suppression(cfg_env):
    cfg_env('[hints]\nworktree_cleanup = "my-prune --dry-run"\n')
    assert "`my-prune --dry-run`" in sweep._ticket_cleanup_hint()
    cfg_env('[hints]\nworktree_cleanup = ""\n')
    assert sweep._ticket_cleanup_hint() is None
    report = sweep.format_report([], [], None, [], [], [], None, None)
    assert "worktree pruning" not in report


def test_pricing_default_rates_unchanged(cfg_env):
    assert pricing.get_rates() == pricing.MODEL_RATES
    assert pricing.cost_breakdown("claude-opus-4-8", output_tokens=1_000_000)["output"] == 25.0


def test_pricing_config_override(cfg_env):
    cfg_env('[pricing.rates]\nopus = [10.0, 50.0]\n')
    assert pricing.get_rates()["opus"] == (10.0, 50.0)
    assert pricing.cost_breakdown("claude-opus-4-8", output_tokens=1_000_000)["output"] == 50.0
    assert pricing.get_rates()["haiku"] == (1.0, 5.0)


def test_doctor_config_check_pass_when_absent(cfg_env):
    c = doctor.check_config()
    assert c.ok


def test_doctor_config_check_fails_on_corrupt(cfg_env):
    cfg_env("not [ valid { toml")
    c = doctor.check_config()
    assert not c.ok
    assert "dockwright" in c.name


def _compose_dirs(tmp_path):
    core = tmp_path / "core"
    out = tmp_path / "out"
    overlay = tmp_path / "overlay"
    core.mkdir()
    (core / "manager.md").write_text("core text\n")
    return core, out, overlay


def test_doctor_compose_fresh_nothing_deployed(tmp_path):
    core, out, overlay = _compose_dirs(tmp_path)
    c = doctor.check_compose_fresh(core, out, overlay)
    assert c.ok and "nothing deployed" in c.detail


def test_doctor_compose_fresh_legacy_deploy_fails(tmp_path):
    core, out, overlay = _compose_dirs(tmp_path)
    out.mkdir()
    (out / "manager.md").write_text("core text\n")
    c = doctor.check_compose_fresh(core, out, overlay)
    assert not c.ok and "legacy" in c.detail


def test_doctor_compose_fresh_and_stale(tmp_path):
    from dockwright import compose
    core, out, overlay = _compose_dirs(tmp_path)
    compose.compose_agents(core, out, overlay, {})
    c = doctor.check_compose_fresh(core, out, overlay)
    assert c.ok
    (core / "manager.md").write_text("core text v2\n")
    c = doctor.check_compose_fresh(core, out, overlay)
    assert not c.ok and "manager.md" in c.detail


def test_doctor_main_runs_compose_check_only_with_flag(tmp_path, capsys):
    orch_bin = tmp_path / "orch"
    doctor.main(["--orch-bin", str(orch_bin),
                 "--claude-json", str(tmp_path / "claude.json"),
                 "--settings", str(tmp_path / "settings.json"),
                 "--codex-hooks", str(tmp_path / "codex-hooks.json"),
                 "--codex-config", str(tmp_path / "codex-config.toml")])
    assert "compose:fresh" not in capsys.readouterr().out
