import hashlib
import json
import os

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
