"""Config consumers: hints, pricing overrides, distill model, doctor check."""
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


# --- sweep hint ---

def test_sweep_hint_default_is_none(cfg_env):
    """DEFAULT_WORKTREE_CLEANUP is "" — no operator config means no hint line."""
    assert sweep._ticket_cleanup_hint() is None


def test_sweep_hint_from_config_and_suppression(cfg_env):
    cfg_env('[hints]\nworktree_cleanup = "my-prune --dry-run"\n')
    assert "`my-prune --dry-run`" in sweep._ticket_cleanup_hint()
    cfg_env('[hints]\nworktree_cleanup = ""\n')
    assert sweep._ticket_cleanup_hint() is None
    report = sweep.format_report([], [], None, [], [], [], None, None)
    assert "worktree pruning" not in report


# --- pricing ---

def test_pricing_default_rates_unchanged(cfg_env):
    assert pricing.get_rates() == pricing.MODEL_RATES
    assert pricing.cost_breakdown("claude-opus-4-8", output_tokens=1_000_000)["output"] == 25.0


def test_pricing_config_override(cfg_env):
    cfg_env('[pricing.rates]\nopus = [10.0, 50.0]\n')
    assert pricing.get_rates()["opus"] == (10.0, 50.0)
    assert pricing.cost_breakdown("claude-opus-4-8", output_tokens=1_000_000)["output"] == 50.0
    # built-ins not named in the override are untouched
    assert pricing.get_rates()["haiku"] == (1.0, 5.0)


# --- distill model ---

def test_distill_model_source():
    import inspect
    from dockwright import distill
    src = inspect.getsource(distill)
    assert '"claude-sonnet-4-6"' not in src
    assert "config.distill_model()" in src


# --- promote hint ---

def test_promote_hint_source():
    import inspect
    from dockwright import promote
    src = inspect.getsource(promote)
    assert "/manager-assign" not in src
    assert "config.assign_command_hint()" in src


# --- doctor ---

def test_doctor_config_check_pass_when_absent(cfg_env):
    c = doctor.check_config()
    assert c.ok


def test_doctor_config_check_fails_on_corrupt(cfg_env):
    cfg_env("not [ valid { toml")
    c = doctor.check_config()
    assert not c.ok
    assert "dockwright" in c.name


# --- doctor compose:fresh ---

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
    (out / "manager.md").write_text("core text\n")  # deployed but no stamp
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
    """No --compose-out-dir → no compose:fresh line (hermetic ad-hoc doctor)."""
    orch_bin = tmp_path / "orch"
    doctor.main(["--orch-bin", str(orch_bin)])
    assert "compose:fresh" not in capsys.readouterr().out
