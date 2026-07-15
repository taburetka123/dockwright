"""compose_agents / check_agents / stamp / CLI."""
import json

import pytest

from dockwright import compose


@pytest.fixture
def dirs(tmp_path):
    core = tmp_path / "core"
    out = tmp_path / "out"
    overlay = tmp_path / "overlay"
    core.mkdir()
    (core / "manager.md").write_text("manager core\n<!-- overlay: hook -->\ntail\n")
    (core / "worker.md").write_text("worker core\n")
    return core, out, overlay


def test_compose_agents_writes_files_and_stamp(dirs):
    core, out, overlay = dirs
    (overlay / "manager").mkdir(parents=True)
    (overlay / "manager" / "10-x.md").write_text("---\ninsert_at: hook\n---\nHOOKED\n")
    result = compose.compose_agents(core, out, overlay, {})
    assert sorted(result["files"]) == ["manager.md", "worker.md"]
    assert (out / "manager.md").read_text() == "manager core\nHOOKED\ntail\n"
    assert (out / "worker.md").read_text() == "worker core\n"
    stamp = json.loads((out / compose.STAMP_NAME).read_text())
    assert set(stamp["core"]) == {"manager.md", "worker.md"}
    assert set(stamp["overlay"]) == {"manager/10-x.md"}
    assert "composed_at" in stamp and "vars_sha256" in stamp


def test_compose_agents_no_overlay_is_identity_minus_markers(dirs):
    core, out, overlay = dirs
    compose.compose_agents(core, out, overlay, {})
    assert (out / "manager.md").read_text() == "manager core\ntail\n"
    assert (out / "worker.md").read_text() == "worker core\n"


def test_compose_agents_empty_core_dir_fails(tmp_path):
    (tmp_path / "core").mkdir()
    with pytest.raises(compose.ComposeError):
        compose.compose_agents(tmp_path / "core", tmp_path / "out",
                               tmp_path / "overlay", {})


def test_check_agents_fresh_and_stale(dirs):
    core, out, overlay = dirs
    compose.compose_agents(core, out, overlay, {})
    ok, problems = compose.check_agents(core, out, overlay, {})
    assert ok and problems == []
    (core / "manager.md").write_text("manager core CHANGED\n")
    ok, problems = compose.check_agents(core, out, overlay, {})
    assert not ok
    assert any("manager.md" in p for p in problems)


def test_check_agents_missing_deployed(dirs):
    core, out, overlay = dirs
    ok, problems = compose.check_agents(core, out, overlay, {})
    assert not ok and any("not deployed" in p for p in problems)


def test_cli_compose_and_check(dirs, capsys):
    core, out, overlay = dirs
    rc = compose.main(["--core-dir", str(core), "--out-dir", str(out),
                       "--overlay-dir", str(overlay)])
    assert rc == 0
    assert (out / "manager.md").is_file()
    rc = compose.main(["--core-dir", str(core), "--out-dir", str(out),
                       "--overlay-dir", str(overlay), "--check"])
    assert rc == 0
    (core / "worker.md").write_text("worker core v2\n")
    rc = compose.main(["--core-dir", str(core), "--out-dir", str(out),
                       "--overlay-dir", str(overlay), "--check"])
    assert rc == 1


def test_cli_compose_error_exits_1(dirs, capsys):
    core, out, overlay = dirs
    (overlay / "manager").mkdir(parents=True)
    (overlay / "manager" / "10-x.md").write_text("---\ninsert_at: ghost\n---\nX\n")
    rc = compose.main(["--core-dir", str(core), "--out-dir", str(out),
                       "--overlay-dir", str(overlay)])
    assert rc == 1
    assert "ghost" in capsys.readouterr().err
    assert not (out / "manager.md").exists()  # nothing half-deployed


# --- .core.md naming: output name, dropin-dir resolution, ambiguity, stamp ---

@pytest.fixture
def core_suffix_dirs(tmp_path):
    core = tmp_path / "core"
    out = tmp_path / "out"
    overlay = tmp_path / "overlay"
    core.mkdir()
    (core / "manager.core.md").write_text("manager core\n<!-- overlay: hook -->\ntail\n")
    (core / "worker.md").write_text("worker core\n")
    return core, out, overlay


def test_compose_agents_core_md_outputs_stripped_name(core_suffix_dirs):
    core, out, overlay = core_suffix_dirs
    result = compose.compose_agents(core, out, overlay, {})
    assert sorted(result["files"]) == ["manager.md", "worker.md"]
    assert (out / "manager.md").is_file()
    assert not (out / "manager.core.md").exists()


def test_compose_agents_core_md_dropin_dir_keyed_by_output_stem(core_suffix_dirs):
    core, out, overlay = core_suffix_dirs
    # Drop-in dir is "manager" (the OUTPUT stem), not "manager.core".
    (overlay / "manager").mkdir(parents=True)
    (overlay / "manager" / "10-x.md").write_text("---\ninsert_at: hook\n---\nHOOKED\n")
    compose.compose_agents(core, out, overlay, {})
    assert (out / "manager.md").read_text() == "manager core\nHOOKED\ntail\n"


def test_compose_agents_core_md_stamp_uses_output_keys_and_records_source(core_suffix_dirs):
    core, out, overlay = core_suffix_dirs
    (overlay / "manager").mkdir(parents=True)
    (overlay / "manager" / "10-x.md").write_text("---\ninsert_at: hook\n---\nHOOKED\n")
    compose.compose_agents(core, out, overlay, {})
    stamp = json.loads((out / compose.STAMP_NAME).read_text())
    assert set(stamp["core"]) == {"manager.md", "worker.md"}
    assert stamp["core_sources"] == {
        "manager.md": "manager.core.md", "worker.md": "worker.md"}
    assert set(stamp["overlay"]) == {"manager/10-x.md"}


def test_compose_agents_ambiguous_core_and_plain_md_raises(tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    (core / "manager.core.md").write_text("core version\n")
    (core / "manager.md").write_text("plain version\n")
    with pytest.raises(compose.ComposeError) as exc:
        compose.compose_agents(core, tmp_path / "out", tmp_path / "overlay", {})
    assert "manager.md" in str(exc.value)
    assert "manager.core.md" in str(exc.value)


def test_check_agents_fresh_and_stale_with_core_md_naming(core_suffix_dirs):
    core, out, overlay = core_suffix_dirs
    compose.compose_agents(core, out, overlay, {})
    ok, problems = compose.check_agents(core, out, overlay, {})
    assert ok and problems == []
    (core / "manager.core.md").write_text(
        "manager core CHANGED\n<!-- overlay: hook -->\ntail\n")
    ok, problems = compose.check_agents(core, out, overlay, {})
    assert not ok
    assert any("manager.md" in p for p in problems)  # compared by OUTPUT name


def test_cli_compose_core_md_naming(core_suffix_dirs):
    core, out, overlay = core_suffix_dirs
    rc = compose.main(["--core-dir", str(core), "--out-dir", str(out),
                       "--overlay-dir", str(overlay)])
    assert rc == 0
    assert (out / "manager.md").is_file()
    assert not (out / "manager.core.md").exists()


# --- vars.defaults.toml (defaults layer merge) ---

def test_defaults_layer_used_when_no_operator_var(tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    (core / "manager.md").write_text("regex: {{ticket}}\n")
    (core / "vars.defaults.toml").write_text('[agent_vars]\nticket = "DEFAULT-1"\n')
    out, overlay = tmp_path / "out", tmp_path / "overlay"
    compose.compose_agents(core, out, overlay, {})
    assert (out / "manager.md").read_text() == "regex: DEFAULT-1\n"


def test_defaults_layer_operator_var_wins_per_key(tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    (core / "manager.md").write_text("regex: {{ticket}}\nother: {{extra}}\n")
    (core / "vars.defaults.toml").write_text(
        '[agent_vars]\nticket = "DEFAULT-1"\nextra = "DEFAULT-2"\n')
    out, overlay = tmp_path / "out", tmp_path / "overlay"
    compose.compose_agents(core, out, overlay, {"ticket": "OPERATOR-1"})
    assert (out / "manager.md").read_text() == "regex: OPERATOR-1\nother: DEFAULT-2\n"


def test_defaults_layer_absent_file_behaves_as_today(dirs):
    core, out, overlay = dirs  # no vars.defaults.toml in this core dir
    result = compose.compose_agents(core, out, overlay, {})
    assert sorted(result["files"]) == ["manager.md", "worker.md"]


def test_check_agents_uses_defaults_layer(tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    (core / "manager.md").write_text("regex: {{ticket}}\n")
    (core / "vars.defaults.toml").write_text('[agent_vars]\nticket = "DEFAULT-1"\n')
    out, overlay = tmp_path / "out", tmp_path / "overlay"
    compose.compose_agents(core, out, overlay, {})
    ok, problems = compose.check_agents(core, out, overlay, {})
    assert ok and problems == []
