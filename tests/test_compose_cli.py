import hashlib
import json
import os

import pytest

from dockwright import compose, config


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
    assert not (out / "manager.md").exists()


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
    assert any("manager.md" in p for p in problems)


def test_cli_compose_core_md_naming(core_suffix_dirs):
    core, out, overlay = core_suffix_dirs
    rc = compose.main(["--core-dir", str(core), "--out-dir", str(out),
                       "--overlay-dir", str(overlay)])
    assert rc == 0
    assert (out / "manager.md").is_file()
    assert not (out / "manager.core.md").exists()


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
    core, out, overlay = dirs
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


def _stamp(out_dir):
    return json.loads((out_dir / compose.STAMP_NAME).read_text())


def test_stamp_records_sha256_of_the_bytes_on_disk(dirs):
    core, out, overlay = dirs
    compose.compose_agents(core, out, overlay, {})
    stamp = _stamp(out)
    for name, sha in stamp["outputs"].items():
        assert sha == hashlib.sha256((out / name).read_bytes()).hexdigest()
    assert set(stamp["outputs"]) == set(stamp["core"])


def test_recompose_warns_and_backs_up_a_hand_edited_deployed_file(dirs):
    core, out, overlay = dirs
    compose.compose_agents(core, out, overlay, {})
    target = out / "manager.md"
    hand_edited = target.read_text() + "\nA paragraph that lives ONLY here.\n"
    target.write_text(hand_edited)

    result = compose.compose_agents(core, out, overlay, {})

    assert any("manager.md" in d for d in result["drift"]), result["drift"]
    backup = out / "manager.md.bak"
    assert backup.read_text() == hand_edited
    assert target.read_text() != hand_edited


def test_recompose_is_silent_when_the_deployed_file_matches_the_last_compose(dirs):
    core, out, overlay = dirs
    compose.compose_agents(core, out, overlay, {})
    result = compose.compose_agents(core, out, overlay, {})
    assert result["drift"] == []
    assert not (out / "manager.md.bak").exists()


def test_first_compose_claims_no_drift(dirs):
    core, out, overlay = dirs
    (out).mkdir(parents=True, exist_ok=True)
    (out / "manager.md").write_text("a pre-existing file with no stamp beside it\n")
    result = compose.compose_agents(core, out, overlay, {})
    assert result["drift"] == []
    assert not (out / "manager.md.bak").exists()


def test_pre_outputs_stamp_claims_no_drift(dirs):
    core, out, overlay = dirs
    compose.compose_agents(core, out, overlay, {})
    stamp_path = out / compose.STAMP_NAME
    stamp = json.loads(stamp_path.read_text())
    del stamp["outputs"]
    stamp_path.write_text(json.dumps(stamp))
    (out / "manager.md").write_text("hand edited\n")

    result = compose.compose_agents(core, out, overlay, {})

    assert result["drift"] == []
    assert not (out / "manager.md.bak").exists()


def test_a_second_drift_never_clobbers_the_first_backup(dirs):
    core, out, overlay = dirs
    compose.compose_agents(core, out, overlay, {})
    target = out / "manager.md"
    precious = target.read_text() + "\nPRECIOUS 500-line operator customization\n"
    target.write_text(precious)
    compose.compose_agents(core, out, overlay, {})
    target.write_text(target.read_text() + "\n")

    result = compose.compose_agents(core, out, overlay, {})

    assert (out / "manager.md.bak").read_text() == precious
    assert (out / "manager.md.bak.2").is_file()
    assert any("manager.md.bak.2" in d for d in result["drift"]), result["drift"]


def test_repeated_identical_drift_reuses_the_same_backup(dirs):
    core, out, overlay = dirs
    compose.compose_agents(core, out, overlay, {})
    target = out / "manager.md"
    edited = target.read_text() + "\nsame edit twice\n"
    target.write_text(edited)
    compose.compose_agents(core, out, overlay, {})
    target.write_text(edited)

    compose.compose_agents(core, out, overlay, {})

    assert (out / "manager.md.bak").read_text() == edited
    assert not (out / "manager.md.bak.2").exists()


@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0,
                    reason="root bypasses file permissions")
def test_an_unreadable_backup_is_never_clobbered(dirs):
    core, out, overlay = dirs
    compose.compose_agents(core, out, overlay, {})
    (out / "manager.md").write_text("first precious\n")
    compose.compose_agents(core, out, overlay, {})
    bak = out / "manager.md.bak"
    bak.chmod(0o000)
    (out / "manager.md").write_text("second precious\n")
    try:
        compose.compose_agents(core, out, overlay, {})
        assert (out / "manager.md.bak.2").read_text() == "second precious\n"
    finally:
        bak.chmod(0o644)
    assert bak.read_text() == "first precious\n"


def test_hand_applied_identical_edit_is_not_drift(dirs):
    core, out, overlay = dirs
    compose.compose_agents(core, out, overlay, {})
    (core / "manager.md").write_text("manager core v2\n<!-- overlay: hook -->\ntail\n")
    (out / "manager.md").write_text("manager core v2\ntail\n")

    result = compose.compose_agents(core, out, overlay, {})

    assert result["drift"] == []
    assert not (out / "manager.md.bak").exists()
    assert (out / "manager.md").read_text() == "manager core v2\ntail\n"


@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0,
                    reason="root bypasses directory permissions")
def test_unwritable_backup_skips_the_write_and_exits_1(dirs, capsys):
    core, out, overlay = dirs
    args = ["--core-dir", str(core), "--out-dir", str(out),
            "--overlay-dir", str(overlay)]
    assert compose.main(args) == 0
    prev_sha = _stamp(out)["outputs"]["manager.md"]
    target = out / "manager.md"
    irreplaceable = target.read_text() + "\nIRREPLACEABLE, lives nowhere else\n"
    target.write_text(irreplaceable)
    out.chmod(0o555)
    try:
        rc = compose.main(args)
    finally:
        out.chmod(0o755)
    captured = capsys.readouterr()
    err = captured.err

    assert target.read_text() == irreplaceable
    assert rc == 1
    assert "manager.md" in err and "NOT rewritten" in err
    assert (out / "worker.md").read_text() == "worker core\n"
    assert "Composed 1 agent file(s)" in captured.out
    assert _stamp(out)["outputs"]["manager.md"] == prev_sha
    assert prev_sha != hashlib.sha256(irreplaceable.encode()).hexdigest()


@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0,
                    reason="root bypasses directory permissions")
def test_the_abort_message_is_usable_without_scrolling_back(dirs, capsys):
    core, out, overlay = dirs
    args = ["--core-dir", str(core), "--out-dir", str(out),
            "--overlay-dir", str(overlay)]
    assert compose.main(args) == 0
    (out / "manager.md").write_text("edits that live nowhere else\n")
    out.chmod(0o555)
    try:
        assert compose.main(args) == 1
    finally:
        out.chmod(0o755)
    abort = [ln for ln in capsys.readouterr().err.splitlines()
             if ln.startswith("compose: ERROR:")]
    assert abort, "no abort block printed"
    block = "\n".join(abort)

    assert "NOTHING WAS OVERWRITTEN" in block
    assert "manager.md" in block
    assert str(out / "manager.md.bak") in block
    assert "Errno" in block
    assert f"make the directory {out} writable" in block
    assert "rerun" in block


@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0,
                    reason="root bypasses file permissions")
def test_the_unblock_action_points_at_the_backup_when_that_is_what_is_blocked(dirs, capsys):
    core, out, overlay = dirs
    args = ["--core-dir", str(core), "--out-dir", str(out),
            "--overlay-dir", str(overlay)]
    assert compose.main(args) == 0
    edit = "edits that live nowhere else\n"
    (out / "manager.md").write_text(edit)
    assert compose.main(args) == 0
    (out / "manager.md.bak").chmod(0o444)
    (out / "manager.md").write_text(edit)
    try:
        assert compose.main(args) == 1
    finally:
        (out / "manager.md.bak").chmod(0o644)
    block = capsys.readouterr().err

    assert f"make {out / 'manager.md.bak'} writable" in block
    assert "make the directory" not in block


@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0,
                    reason="root bypasses file permissions")
def test_the_abort_block_never_claims_nothing_was_overwritten_when_something_was(dirs, capsys):
    core, out, overlay = dirs
    args = ["--core-dir", str(core), "--out-dir", str(out),
            "--overlay-dir", str(overlay)]
    assert compose.main(args) == 0
    manager_edit = "MANAGER edits that live nowhere else\n"
    (out / "manager.md").write_text(manager_edit)
    assert compose.main(args) == 0
    (out / "manager.md.bak").chmod(0o444)
    (out / "manager.md").write_text(manager_edit)
    worker_edit = "WORKER edits that live nowhere else\n"
    (out / "worker.md").write_text(worker_edit)
    try:
        assert compose.main(args) == 1
    finally:
        (out / "manager.md.bak").chmod(0o644)
    abort = [ln for ln in capsys.readouterr().err.splitlines()
             if ln.startswith("compose: ERROR:")]
    assert abort, "no abort block printed"
    block = "\n".join(abort)

    assert (out / "manager.md").read_text() == manager_edit
    assert (out / "worker.md").read_text() == "worker core\n"
    assert (out / "worker.md.bak").read_text() == worker_edit

    assert "NOTHING WAS OVERWRITTEN" not in block
    assert "WERE rewritten" in block
    assert "manager.md: NOT rewritten" in block
    assert str(out / "worker.md.bak") in block
    assert "rerun" in block


def test_cli_prints_drift_to_stderr_with_the_overlay_remedy(dirs, capsys):
    core, out, overlay = dirs
    args = ["--core-dir", str(core), "--out-dir", str(out),
            "--overlay-dir", str(overlay)]
    assert compose.main(args) == 0
    (out / "manager.md").write_text("hand edited\n")

    assert compose.main(args) == 0

    err = capsys.readouterr().err
    assert "DRIFT:" in err
    assert "manager.md.bak" in err
    assert f"{overlay / 'manager'}/*.md" in err


def _include_tree(tmp_path):
    core = tmp_path / "deploy" / "agents"
    frags = tmp_path / "deploy" / "roles" / "fragments"
    core.mkdir(parents=True)
    frags.mkdir(parents=True)
    return core, frags


def test_include_inlines_a_fragment_whose_marker_takes_a_dropin(tmp_path):
    core, frags = _include_tree(tmp_path)
    (frags / "proto.md").write_text("PROTO\n<!-- overlay: hook -->\n")
    (core / "worker.core.md").write_text("head\n\n<!-- include: proto -->\n\ntail\n")
    overlay = tmp_path / "overlay"
    (overlay / "worker").mkdir(parents=True)
    (overlay / "worker" / "10-x.md").write_text("---\ninsert_at: hook\n---\nHOOKED\n")
    compose.compose_agents(core, tmp_path / "out", overlay, {})
    assert (tmp_path / "out" / "worker.md").read_text() == "head\n\nPROTO\nHOOKED\n\ntail\n"


def test_an_unknown_include_is_a_compose_error(tmp_path):
    core, _ = _include_tree(tmp_path)
    (core / "worker.core.md").write_text("<!-- include: nope -->\n")
    with pytest.raises(compose.ComposeError, match="nope"):
        compose.compose_agents(core, tmp_path / "out", tmp_path / "overlay", {})


def test_an_included_fragment_may_not_include_another(tmp_path):
    core, frags = _include_tree(tmp_path)
    (frags / "a.md").write_text("<!-- include: b -->\n")
    (frags / "b.md").write_text("B\n")
    (core / "worker.core.md").write_text("<!-- include: a -->\n")
    with pytest.raises(compose.ComposeError, match="nested include"):
        compose.compose_agents(core, tmp_path / "out", tmp_path / "overlay", {})


def test_check_agents_reports_a_fragment_edit_as_stale(tmp_path):
    core, frags = _include_tree(tmp_path)
    (frags / "proto.md").write_text("PROTO\n")
    (core / "worker.core.md").write_text("<!-- include: proto -->\n")
    compose.compose_agents(core, tmp_path / "out", tmp_path / "overlay", {})
    (frags / "proto.md").write_text("PROTO v2\n")
    ok, problems = compose.check_agents(core, tmp_path / "out", tmp_path / "overlay", {})
    assert not ok and problems == ["worker.md: deployed differs from composed"]


@pytest.mark.parametrize("line", [
    "<!-- include: proto --> ",
    "<!-- include: proto.md -->",
    "<!-- include:proto -->",
    "<!-- Include: proto -->",
    "  <!-- include: proto -->",
])
def test_a_malformed_include_line_is_a_compose_error(tmp_path, line):
    core, frags = _include_tree(tmp_path)
    (frags / "proto.md").write_text("PROTO\n")
    (core / "worker.core.md").write_text(f"head\n{line}\ntail\n")
    with pytest.raises(compose.ComposeError, match="malformed include"):
        compose.compose_agents(core, tmp_path / "out", tmp_path / "overlay", {})


def test_a_fragment_holding_a_malformed_include_is_refused(tmp_path):
    core, frags = _include_tree(tmp_path)
    (frags / "a.md").write_text("<!-- include:b -->\n")
    (core / "worker.core.md").write_text("<!-- include: a -->\n")
    with pytest.raises(compose.ComposeError, match="nested include"):
        compose.compose_agents(core, tmp_path / "out", tmp_path / "overlay", {})


def test_a_marker_in_two_fragments_names_the_expanded_core(tmp_path):
    core, frags = _include_tree(tmp_path)
    (frags / "a.md").write_text("<!-- overlay: hook -->\n")
    (frags / "b.md").write_text("<!-- overlay: hook -->\n")
    (core / "worker.core.md").write_text("<!-- include: a -->\n<!-- include: b -->\n")
    with pytest.raises(compose.ComposeError, match="after includes are expanded"):
        compose.compose_agents(core, tmp_path / "out", tmp_path / "overlay", {})


def test_legacy_manager_render_takes_dropins_from_the_role_overlay(tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    (core / "manager.core.md").write_text("m\n<!-- overlay: hook -->\n")
    roles_dir = tmp_path / "roles"
    (roles_dir / "Manager" / "overlay").mkdir(parents=True)
    (roles_dir / "Manager" / "overlay" / "10-x.md").write_text("---\ninsert_at: hook\n---\nFROM ROLE\n")
    compose.compose_agents(core, tmp_path / "out", tmp_path / "overlay", {}, roles_dir=roles_dir)
    assert (tmp_path / "out" / "manager.md").read_text() == "m\nFROM ROLE\n"
    stamp = json.loads((tmp_path / "out" / compose.STAMP_NAME).read_text())
    assert set(stamp["overlay"]) == {"manager/10-x.md"}


def test_a_dropin_name_in_both_overlays_is_a_compose_error(tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    (core / "manager.core.md").write_text("m\n")
    for base in (tmp_path / "overlay" / "manager", tmp_path / "roles" / "Manager" / "overlay"):
        base.mkdir(parents=True)
        (base / "10-x.md").write_text("X\n")
    with pytest.raises(compose.ComposeError, match="10-x.md"):
        compose.compose_agents(core, tmp_path / "out", tmp_path / "overlay", {}, roles_dir=tmp_path / "roles")


def test_compose_cli_renders_roles_and_check_reports_them(tmp_path, capsys):
    core = tmp_path / "deploy" / "agents"
    core.mkdir(parents=True)
    (core / "worker.core.md").write_text("w\n")
    canon = tmp_path / "deploy" / "roles"
    (canon / "fragments").mkdir(parents=True)
    (canon / "GenericWorker.core.md").write_text("# GW\n")
    (canon / "fragments" / "worker-protocol.md").write_text("WP\n")
    claude = tmp_path / "claude"
    (claude / "roles" / "GenericWorker").mkdir(parents=True)
    (claude / "roles" / "GenericWorker" / "role.toml").write_text('core = "GenericWorker"\nlaunch = ["worker"]\n')
    argv = ["--core-dir", str(core), "--out-dir", str(claude / "agents"), "--overlay-dir", str(tmp_path / "ov"),
            "--roles-dir", str(claude / "roles"), "--claude-dir", str(claude)]
    assert compose.main(argv) == 0
    assert (claude / "roles" / "GenericWorker" / "role.worker.md").read_text().endswith("# GW\n\nWP\n")
    assert compose.main([*argv, "--check"]) == 0
    (claude / "roles" / "GenericWorker" / "role.worker.md").write_text("hand edit\n")
    assert compose.main([*argv, "--check"]) == 1
    assert "roles: (d) roles/GenericWorker/role.worker.md is stale" in capsys.readouterr().out


def test_compose_with_its_own_dirs_leaves_the_configured_roles_alone(tmp_path, monkeypatch):
    live = tmp_path / "live-roles"
    (live / "Broken").mkdir(parents=True)
    (live / "Broken" / "role.toml").write_text("bogus = 1\n")
    monkeypatch.setattr(config, "roles_dir", lambda: live)
    core = tmp_path / "core"
    core.mkdir()
    (core / "worker.core.md").write_text("w\n")
    argv = ["--core-dir", str(core), "--out-dir", str(tmp_path / "out"), "--overlay-dir", str(tmp_path / "ov")]
    assert compose.main(argv) == 0
    assert compose.main([*argv, "--check"]) == 0
    assert sorted(p.name for p in (live / "Broken").iterdir()) == ["role.toml"]


def _role_install(tmp_path, manifests=("Coder", "Manager")):
    core = tmp_path / "deploy" / "agents"
    core.mkdir(parents=True)
    (core / "manager.core.md").write_text("m\n<!-- overlay: mgr-hook -->\n")
    (core / "worker.core.md").write_text("w\n<!-- overlay: wk-hook -->\n")
    (core / "dockwright-reviewer.core.md").write_text("r\n")
    canon = tmp_path / "deploy" / "roles"
    (canon / "fragments").mkdir(parents=True)
    (canon / "Coder.core.md").write_text("# Coder\n<!-- overlay: coder-hook -->\n")
    (canon / "fragments" / "worker-protocol.md").write_text("WP\n")
    claude = tmp_path / "claude"
    bodies = {"Coder": 'core = "Coder"\nlaunch = ["worker"]\n',
              "Manager": 'core = "manager"\nlaunch = ["manager"]\n',
              "CodeReviewer": 'core = "dockwright-reviewer"\nlaunch = ["lane"]\n'}
    for name in manifests:
        (claude / "roles" / name).mkdir(parents=True)
        (claude / "roles" / name / "role.toml").write_text(bodies[name])
    (claude / "roles").mkdir(parents=True, exist_ok=True)
    argv = ["--core-dir", str(core), "--out-dir", str(claude / "agents"), "--overlay-dir", str(tmp_path / "ov"),
            "--roles-dir", str(claude / "roles"), "--claude-dir", str(claude)]
    return claude, argv


def _dropin(claude, owner, name, insert_at, body):
    front = f"---\ninsert_at: {insert_at}\n---\n" if insert_at else ""
    path = claude / "roles" / owner / "overlay" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(front + body)


def test_a_manager_role_overlay_aimed_at_a_role_fragment_lands_in_the_role_only(tmp_path):
    claude, argv = _role_install(tmp_path)
    (claude / "roles" / "Manager" / "role.toml").write_text(
        'core = "manager"\nlaunch = ["manager"]\nfragments = ["mgr-extra"]\n')
    (claude / "roles" / "Manager" / "fragments").mkdir()
    (claude / "roles" / "Manager" / "fragments" / "mgr-extra.md").write_text("MGR EXTRA\n<!-- overlay: foo-hook -->\n")
    _dropin(claude, "Manager", "10-foo.md", "foo-hook", "FOO DROP\n")
    assert compose.main(argv) == 0
    assert "FOO DROP" in (claude / "roles" / "Manager" / "role.manager.md").read_text()
    assert "FOO DROP" not in (claude / "agents" / "manager.md").read_text()
    assert compose.main([*argv, "--check"]) == 0


def test_a_shared_role_overlay_aimed_at_a_role_core_marker_lands_in_the_role_only(tmp_path):
    claude, argv = _role_install(tmp_path)
    _dropin(claude, "_shared", "10-y.md", "coder-hook", "Y DROP\n")
    assert compose.main(argv) == 0
    assert "Y DROP" in (claude / "roles" / "Coder" / "role.worker.md").read_text()
    assert "Y DROP" not in (claude / "agents" / "worker.md").read_text()
    assert compose.main([*argv, "--check"]) == 0


@pytest.mark.parametrize("owner,name,insert_at,problem", [
    ("Manager", "99-probe.md", "no-such-marker-typo",
     "(d) role Manager: overlay/99-probe.md targets marker 'no-such-marker-typo', which no render of the role carries"),
    ("_shared", "99-append.md", None, "(d) roles/_shared/overlay/99-append.md lands in no role render"),
])
def test_plain_compose_refuses_a_role_overlay_dropin_that_lands_in_no_role_render(
        tmp_path, capsys, owner, name, insert_at, problem):
    claude, argv = _role_install(tmp_path)
    _dropin(claude, owner, name, insert_at, "PROBE-LINE-XYZ\n")
    assert compose.main(argv) == 1
    assert problem in capsys.readouterr().err
    assert not (claude / "agents").exists()
    assert not list((claude / "roles").glob("*/role.*.md"))
    assert compose.main([*argv, "--check"]) == 1
    assert f"roles: {problem}" in capsys.readouterr().out


@pytest.mark.parametrize("stem,role", [("manager", "Manager"), ("worker", "Coder"),
                                       ("dockwright-reviewer", "CodeReviewer")])
def test_plain_compose_refuses_a_legacy_overlay_file_once_its_role_has_a_manifest(tmp_path, capsys, stem, role):
    claude, argv = _role_install(tmp_path, ("Coder", "Manager", "CodeReviewer"))
    legacy = tmp_path / "ov" / stem / "10-old.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("OLD HOME\n")
    assert compose.main(argv) == 1
    err = capsys.readouterr().err
    assert f"{tmp_path / 'ov' / stem}" in err and "10-old.md" in err
    assert not (claude / "agents").exists()
    assert compose.main([*argv, "--check"]) == 1


def test_a_legacy_overlay_file_still_lands_while_its_role_has_no_manifest(tmp_path):
    claude, argv = _role_install(tmp_path, ("Coder",))
    legacy = tmp_path / "ov" / "manager" / "10-old.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("OLD HOME\n")
    assert compose.main(argv) == 0
    assert (claude / "agents" / "manager.md").read_text().endswith("OLD HOME\n")


@pytest.mark.parametrize("owner,manifests", [("Manager", ("Coder",)), ("CodeReviewer", ("Coder",)),
                                             ("_shared", ())])
def test_a_role_overlay_with_no_role_render_to_land_in_stays_strict(tmp_path, capsys, owner, manifests):
    claude, argv = _role_install(tmp_path, manifests)
    _dropin(claude, owner, "10-x.md", "nowhere", "X\n")
    assert compose.main(argv) == 1
    assert "match no marker" in capsys.readouterr().err


@pytest.mark.parametrize("manifests", [(), ("Coder", "Manager")])
@pytest.mark.parametrize("owner", ["Coder", "Manager", "_shared", "CodeReviewer", "Ghost"])
@pytest.mark.parametrize("insert_at", [None, "coder-hook", "mgr-hook", "wk-hook", "nowhere"])
def test_a_new_role_overlay_dropin_lands_or_plain_compose_aborts(tmp_path, capsys, owner, insert_at, manifests):
    claude, argv = _role_install(tmp_path, manifests)
    _dropin(claude, owner, "50-probe.md", insert_at, "PROBE BODY\n")
    if compose.main(argv) != 0:
        assert "compose: ERROR" in capsys.readouterr().err
        return
    rendered = [*(claude / "agents").glob("*.md"), *(claude / "roles").glob("*/role.*.md")]
    assert any("PROBE BODY" in p.read_text() for p in rendered)
    capsys.readouterr()
    assert compose.main([*argv, "--check"]) == 0


def test_expand_includes_strips_a_fragments_frontmatter(tmp_path):
    frag = tmp_path / "fragments"
    frag.mkdir()
    (frag / "f.md").write_text("---\nkind: fact\nbinds: [build]\ndomain: build\n---\nFRAG BODY\n")
    assert compose.expand_includes("A\n<!-- include: f -->\nB\n", frag) == "A\nFRAG BODY\nB\n"


def _roles_tree(tmp_path):
    core_dir = tmp_path / "deploy" / "agents"
    core_dir.mkdir(parents=True)
    (core_dir / "manager.core.md").write_text("---\nname: manager\n---\nm\n")
    overlay = tmp_path / "ov"
    overlay.mkdir()
    roles_dir = tmp_path / "claude" / "roles"
    (roles_dir / "_shared" / "fragments" / "git").mkdir(parents=True)
    (roles_dir / "_shared" / "fragments" / "git" / "commit.md").write_text(
        "---\nkind: guard\nbinds: [push]\ndomain: git\n---\nCOMMIT RULE\n")
    (roles_dir / "actions.toml").write_text('[actions]\npush = "push"\n[domains]\ngit = "git"\n')
    (roles_dir / "Manager").mkdir()
    (roles_dir / "Manager" / "role.toml").write_text('core = "manager"\nlaunch = ["manager"]\nfragments = ["commit"]\n')
    return core_dir, tmp_path / "claude" / "agents", overlay, roles_dir, tmp_path / "claude"


def test_compose_renders_a_subagent_from_its_roles_agent_render(tmp_path, monkeypatch):
    core_dir, out_dir, overlay, roles_dir, claude = _roles_tree(tmp_path)
    (core_dir.parent / "roles" / "subagents").mkdir(parents=True)
    (core_dir.parent / "roles" / "subagents" / "general-purpose.core.md").write_text(
        "---\nname: general-purpose\ndescription: Finds things.\n---\n# Scout core\n")
    (roles_dir / "Scout").mkdir()
    (roles_dir / "Scout" / "role.toml").write_text('core = "general-purpose"\nlaunch = ["agent"]\nfragments = ["commit"]\n')
    monkeypatch.setattr(compose.config, "subagents", lambda: {"general-purpose": {"role": "Scout", "core": "general-purpose"}})
    assert compose.main(["--core-dir", str(core_dir), "--out-dir", str(out_dir), "--overlay-dir", str(overlay),
                         "--roles-dir", str(roles_dir), "--claude-dir", str(claude)]) == 0
    text = (out_dir / "general-purpose.md").read_text()
    assert text.startswith("---\nname: general-purpose\ndescription: Finds things.\n---\n<!-- dockwright-role: Scout launch=agent -->\n")
    assert "COMMIT RULE" in text
    stamp = json.loads((out_dir / compose.STAMP_NAME).read_text())
    assert stamp["core_sources"]["general-purpose.md"] == "general-purpose.core.md"
    assert compose.main(["--check", "--core-dir", str(core_dir), "--out-dir", str(out_dir), "--overlay-dir", str(overlay),
                         "--roles-dir", str(roles_dir), "--claude-dir", str(claude)]) == 0


def test_compose_without_a_subagents_table_renders_no_general_purpose(tmp_path, monkeypatch):
    core_dir, out_dir, overlay, roles_dir, claude = _roles_tree(tmp_path)
    monkeypatch.setattr(compose.config, "subagents", lambda: {})
    compose.main(["--core-dir", str(core_dir), "--out-dir", str(out_dir), "--overlay-dir", str(overlay),
                  "--roles-dir", str(roles_dir), "--claude-dir", str(claude)])
    assert not (out_dir / "general-purpose.md").exists()


def test_compose_check_counts_role_warnings_while_fresh(tmp_path, monkeypatch, capsys):
    core_dir, out_dir, overlay, roles_dir, claude = _roles_tree(tmp_path)
    (roles_dir / "_shared" / "fragments" / "git" / "commit2.md").write_text(
        "---\nkind: guard\nbinds: [push]\ndomain: git\n---\nTWIN\n")
    (roles_dir / "Manager" / "role.toml").write_text('core = "manager"\nlaunch = ["manager"]\nfragments = ["commit", "commit2"]\n')
    monkeypatch.setattr(compose.config, "subagents", lambda: {})
    argv = ["--core-dir", str(core_dir), "--out-dir", str(out_dir), "--overlay-dir", str(overlay),
            "--roles-dir", str(roles_dir), "--claude-dir", str(claude)]
    assert compose.main(argv) == 0
    assert compose.main(["--check", *argv]) == 0
    out = capsys.readouterr().out
    assert "roles: (i) guards" in out and "compose check: fresh (1 warning(s))" in out
