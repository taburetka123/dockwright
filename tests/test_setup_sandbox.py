import json, os, shutil, subprocess, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

def run_sandboxed_setup(tmp_path, extra_env=None, codex=True, repo=REPO):
    claude_dir = tmp_path / "claude"; codex_dir = tmp_path / "codex"
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir(exist_ok=True)
    if codex:
        stub = stub_bin / "codex"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    env = {**os.environ,
           "DOCKWRIGHT_SETUP_ALLOW_WORKTREE": "1",
           "DOCKWRIGHT_SETUP_FILES_ONLY": "1",
           "PATH": f"{stub_bin}:/usr/bin:/bin",
           "CLAUDE_DIR": str(claude_dir), "CODEX_DIR": str(codex_dir),
           **(extra_env or {})}
    r = subprocess.run(["bash", str(repo / "setup.sh")], env=env,
                       capture_output=True, text=True, cwd=str(repo))
    assert r.returncode == 0, r.stdout + r.stderr
    return claude_dir, codex_dir, r

def test_files_only_deploys_commands_and_scripts(tmp_path):
    claude_dir, codex_dir, _ = run_sandboxed_setup(tmp_path)
    assert (claude_dir / "commands" / "dockwright-general-work.md").exists()
    assert (claude_dir / "scripts" / "loops_status.py").exists()
    assert (claude_dir / "dockwright" / "loops-registry.md").exists()

def test_files_only_skips_machine_mutation(tmp_path):
    claude_dir, _, _ = run_sandboxed_setup(tmp_path)
    assert not (claude_dir / "settings.json").exists()

def test_orch_bin_override_renders_transforms_in_files_only(tmp_path):
    overlay = tmp_path / "empty-overlay"; overlay.mkdir()
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(
        f'[paths]\noverlay_dir = "{overlay}"\n'
        '[agent_vars]\nexample_task_key = "TKT-SANDBOX-42"\n')
    claude_dir, codex_dir, _ = run_sandboxed_setup(tmp_path, {
        "DOCKWRIGHT_ORCH_BIN": str(REPO / ".venv" / "bin" / "orchestrator"),
        "DOCKWRIGHT_OVERLAY_DIR": str(overlay),
        "DOCKWRIGHT_CONFIG": str(cfg),
    })
    manager = claude_dir / "agents" / "manager.md"
    assert manager.exists()
    body = manager.read_text()
    assert "{{" not in body
    assert "TKT-SANDBOX-42" in body
    for d in (claude_dir, codex_dir):
        cmd = d / "commands" / "dockwright-general-work.md"
        assert cmd.exists()
        assert "{{" not in cmd.read_text()
    assert (codex_dir / "agents" / "manager.toml").exists()
    stamp = json.loads((codex_dir / ".dockwright-translate.json").read_text())
    wrappers = [codex_dir / rel / "SKILL.md" for rel, meta in stamp["entries"].items() if meta["kind"] == "wrapper"]
    assert wrappers, "no codex skill wrappers generated"
    assert all("{{" not in w.read_text() for w in wrappers)
    copies = [rel for rel, meta in stamp["entries"].items() if meta["kind"] == "copy"]
    assert copies, "no codex skill copies generated"
    for rel in copies:
        assert (codex_dir / rel / "SKILL.md").read_bytes() == (claude_dir / rel / "SKILL.md").read_bytes()


def test_setup_derives_each_derived_preset_from_the_deployed_presets(tmp_path):
    overlay = tmp_path / "empty-overlay"; overlay.mkdir()
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(f'[paths]\noverlay_dir = "{overlay}"\n')
    claude_dir, _, _ = run_sandboxed_setup(tmp_path, {
        "DOCKWRIGHT_ORCH_BIN": str(REPO / ".venv" / "bin" / "orchestrator"),
        "DOCKWRIGHT_OVERLAY_DIR": str(overlay),
        "DOCKWRIGHT_CONFIG": str(cfg),
    }, codex=False)
    headless = json.loads((claude_dir / "dockwright" / "presets" / "worker-headless-settings.json").read_text())
    derived = json.loads(
        (claude_dir / "dockwright" / "derived-presets" / "worker-headless-no-bash-first-settings.json").read_text())
    assert "extends" not in derived
    assert derived["env"] == {"CLAUDE_CODE_THRIFTY_SONIC": "0"}
    assert derived["permissions"] == headless["permissions"]
    assert headless["permissions"]["additionalDirectories"]

def test_codex_skill_wrappers_scoped_to_this_repo(tmp_path):
    codex_dir = tmp_path / "codex"
    (codex_dir / "commands").mkdir(parents=True)
    (codex_dir / "commands" / "foreign-tool.md").write_text(
        "---\ndescription: owned by another deployer\n---\n# foreign command\n")
    foreign_skill = codex_dir / "skills" / "foreign-tool" / "SKILL.md"
    foreign_skill.parent.mkdir(parents=True)
    SENTINEL = "SENTINEL — owned by another deployer, do not touch\n"
    foreign_skill.write_text(SENTINEL)

    overlay = tmp_path / "empty-overlay"; overlay.mkdir()
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(
        f'[paths]\noverlay_dir = "{overlay}"\n'
        '[agent_vars]\nexample_task_key = "TKT-SANDBOX-42"\n')
    run_sandboxed_setup(tmp_path, {
        "DOCKWRIGHT_ORCH_BIN": str(REPO / ".venv" / "bin" / "orchestrator"),
        "DOCKWRIGHT_OVERLAY_DIR": str(overlay),
        "DOCKWRIGHT_CONFIG": str(cfg),
    })
    assert foreign_skill.read_text() == SENTINEL
    assert (codex_dir / "skills" / "dockwright-general-work" / "SKILL.md").exists()


def test_files_only_without_orch_bin_skips_transforms(tmp_path):
    claude_dir, codex_dir, _ = run_sandboxed_setup(tmp_path)
    assert (claude_dir / "commands" / "dockwright-general-work.md").exists()
    assert not (claude_dir / "agents" / "manager.md").exists()
    assert not (codex_dir / "skills").exists()


def test_tmux_conf_renders_the_operator_path_prepend(tmp_path):
    overlay = tmp_path / "empty-overlay"; overlay.mkdir()
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(
        f'[paths]\noverlay_dir = "{overlay}"\n'
        '[agent_vars]\ntmux_path_prepend = "/opt/operator-tools/bin"\n')
    claude_dir, _, _ = run_sandboxed_setup(tmp_path, {
        "DOCKWRIGHT_ORCH_BIN": str(REPO / ".venv" / "bin" / "orchestrator"),
        "DOCKWRIGHT_OVERLAY_DIR": str(overlay),
        "DOCKWRIGHT_CONFIG": str(cfg),
    })
    conf = (claude_dir / "dockwright" / "dockwright.tmux.conf").read_text()
    assert "{{" not in conf
    assert 'set-environment -g PATH "/opt/operator-tools/bin:$PATH"' in conf


def test_files_only_without_orch_bin_deploys_the_conf_as_an_empty_prepend(tmp_path):
    from dockwright import render
    claude_dir, _, _ = run_sandboxed_setup(tmp_path)
    canon = (REPO / "deploy" / "tmux" / "dockwright.conf").read_text()
    deployed = (claude_dir / "dockwright" / "dockwright.tmux.conf").read_text()
    assert deployed == render.render_text(canon, {"tmux_path_prepend": ""})


def test_setup_renders_with_the_defaults_of_the_repo_it_deploys(tmp_path):
    stale = tmp_path / "stale"
    shutil.copytree(REPO / "src" / "dockwright", stale / "src" / "dockwright",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (stale / "deploy" / "agents").mkdir(parents=True)
    (stale / "deploy" / "agents" / "vars.defaults.toml").write_text("[agent_vars]\n")
    stub = tmp_path / "stale-orchestrator"
    stub.write_text(f'#!/bin/sh\nPYTHONPATH="{stale / "src"}" exec "{REPO / ".venv" / "bin" / "python"}" -m dockwright "$@"\n')
    stub.chmod(0o755)
    overlay = tmp_path / "empty-overlay"; overlay.mkdir()
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(f'[paths]\noverlay_dir = "{overlay}"\n')
    claude_dir, _, _ = run_sandboxed_setup(tmp_path, {
        "DOCKWRIGHT_ORCH_BIN": str(stub),
        "DOCKWRIGHT_OVERLAY_DIR": str(overlay),
        "DOCKWRIGHT_CONFIG": str(cfg),
    })
    assert "{{" not in (claude_dir / "dockwright" / "dockwright.tmux.conf").read_text()
    assert "{{" not in (claude_dir / "commands" / "dockwright-general-work.md").read_text()


def test_overlay_payload_deploys(tmp_path):
    overlay = tmp_path / "overlay"
    (overlay / "commands").mkdir(parents=True)
    (overlay / "commands" / "op-only.md").write_text("# operator command\n")
    (overlay / "presets").mkdir()
    (overlay / "presets" / "op-preset.md").write_text("# operator preset\n")
    (overlay / "scripts").mkdir()
    (overlay / "scripts" / "op.sh").write_text("#!/bin/bash\ntrue\n")
    claude_dir, codex_dir, _ = run_sandboxed_setup(
        tmp_path, {"DOCKWRIGHT_OVERLAY_DIR": str(overlay)})
    assert (claude_dir / "commands" / "op-only.md").exists()
    assert (codex_dir / "commands" / "op-only.md").exists()
    assert (claude_dir / "dockwright" / "presets" / "op-preset.md").exists()
    deployed = claude_dir / "scripts" / "op.sh"
    assert deployed.exists() and os.access(deployed, os.X_OK)


def test_migration_relocates_preseeded_orchestrator_state(tmp_path):
    claude_dir = tmp_path / "claude"
    (claude_dir / "orchestrator" / "done").mkdir(parents=True)
    (claude_dir / "orchestrator" / "done" / "seed.json").write_text('{"seed": 1}')
    overlay = tmp_path / "empty-overlay"; overlay.mkdir()
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(
        f'[paths]\noverlay_dir = "{overlay}"\n'
        '[agent_vars]\nexample_task_key = "TKT-SANDBOX-42"\n')
    run_sandboxed_setup(tmp_path, {
        "DOCKWRIGHT_ORCH_BIN": str(REPO / ".venv" / "bin" / "orchestrator"),
        "DOCKWRIGHT_OVERLAY_DIR": str(overlay),
        "DOCKWRIGHT_CONFIG": str(cfg),
    })
    orch = claude_dir / "orchestrator"
    assert orch.is_symlink()
    assert orch.resolve() == (claude_dir / "dockwright").resolve()
    assert (claude_dir / "dockwright" / "done" / "seed.json").read_text() == '{"seed": 1}'


def test_no_codex_on_path_never_creates_codex_dir(tmp_path):
    claude_dir, codex_dir, _ = run_sandboxed_setup(tmp_path, codex=False)
    assert (claude_dir / "commands" / "dockwright-general-work.md").exists()
    assert (claude_dir / "scripts" / "loops_status.py").exists()
    assert not codex_dir.exists()

def test_no_codex_with_render_bin_still_skips_codex_dir(tmp_path):
    overlay = tmp_path / "empty-overlay"; overlay.mkdir()
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(
        f'[paths]\noverlay_dir = "{overlay}"\n'
        '[agent_vars]\nexample_task_key = "TKT-SANDBOX-42"\n')
    claude_dir, codex_dir, _ = run_sandboxed_setup(tmp_path, {
        "DOCKWRIGHT_ORCH_BIN": str(REPO / ".venv" / "bin" / "orchestrator"),
        "DOCKWRIGHT_OVERLAY_DIR": str(overlay),
        "DOCKWRIGHT_CONFIG": str(cfg),
    }, codex=False)
    assert (claude_dir / "agents" / "manager.md").exists()
    assert (claude_dir / "commands" / "dockwright-general-work.md").exists()
    assert not codex_dir.exists()


def test_files_only_does_not_create_worker_home(tmp_path):
    marker = tmp_path / "sentinel-worker-home"
    claude_dir, _, _ = run_sandboxed_setup(
        tmp_path, {"CLAUDE_ORCH_WORKER_HOME": str(marker)})
    assert not marker.exists()


def _git(clone, *args):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.invalid"}
    subprocess.run(["git", *args], cwd=str(clone), env=env,
                   check=True, capture_output=True)


def _snapshot_repo(tmp_path):
    clone = tmp_path / "repo"
    shutil.copytree(REPO, clone, ignore=shutil.ignore_patterns(
        ".git", ".venv", "__pycache__", ".pytest_cache", "docs", "evals",
        "node_modules"))
    _git(clone, "init", "-q")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-qm", "snapshot")
    return clone


def test_release_tag_checkout_prints_tag_not_warning(tmp_path):
    clone = _snapshot_repo(tmp_path)
    _git(clone, "tag", "v9.9.9-test")
    _git(clone, "checkout", "-q", "--detach", "v9.9.9-test")
    _, _, r = run_sandboxed_setup(tmp_path, repo=clone)
    assert "Deploying from release tag 'v9.9.9-test'" in r.stdout
    combined = r.stdout + r.stderr
    assert "deploying from branch" not in combined
    assert "detached HEAD" not in combined


def test_detached_non_tag_warns_detached_head(tmp_path):
    clone = _snapshot_repo(tmp_path)
    _git(clone, "checkout", "-q", "--detach", "HEAD")
    _, _, r = run_sandboxed_setup(tmp_path, repo=clone)
    assert "detached HEAD" in r.stderr
    assert "deploying from branch 'HEAD'" not in r.stderr


def test_branch_checkout_still_warns(tmp_path):
    clone = _snapshot_repo(tmp_path)
    _git(clone, "checkout", "-q", "-b", "feature-x")
    _, _, r = run_sandboxed_setup(tmp_path, repo=clone)
    assert "deploying from branch 'feature-x'" in r.stderr


def test_files_only_without_orch_bin_writes_no_roster(tmp_path):
    claude_dir, _, _ = run_sandboxed_setup(tmp_path)
    assert not (claude_dir / "dockwright-roster.toml").exists()


def test_translate_codex_step_builds_agents_md_and_removes_hand_written_rules(tmp_path):
    claude_dir = tmp_path / "claude"; codex_dir = tmp_path / "codex"
    (claude_dir / "rules").mkdir(parents=True)
    (claude_dir / "rules" / "x-rule.md").write_text("# X\nTRIGGER: sandbox\n")
    (codex_dir / "rules").mkdir(parents=True)
    (codex_dir / "rules" / "old.md").write_text("hand-written\n")
    (codex_dir / "rules" / "default.rules").write_text('prefix_rule(pattern=["ls"], decision="allow")\n')
    overlay = tmp_path / "empty-overlay"; overlay.mkdir()
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(
        f'[paths]\noverlay_dir = "{overlay}"\n'
        '[agent_vars]\nexample_task_key = "TKT-SANDBOX-42"\n')
    _, _, r = run_sandboxed_setup(tmp_path, {
        "DOCKWRIGHT_ORCH_BIN": str(REPO / ".venv" / "bin" / "orchestrator"),
        "DOCKWRIGHT_OVERLAY_DIR": str(overlay),
        "DOCKWRIGHT_CONFIG": str(cfg),
    })
    agents_md = (codex_dir / "AGENTS.md").read_text()
    assert agents_md.startswith("<!-- dockwright:translate-codex")
    assert "<!-- BEGIN rules/x-rule.md -->\n# X\nTRIGGER: sandbox\n<!-- END rules/x-rule.md -->" in agents_md
    assert not (codex_dir / "rules" / "old.md").exists()
    assert (codex_dir / "rules" / "default.rules").is_file()
    assert (codex_dir / ".dockwright-translate.json").is_file()
    assert (codex_dir / "agents" / "manager.toml").is_file()
    assert (codex_dir / "skills" / "dockwright-general-work" / "SKILL.md").is_file()
    assert "translated: 1 rules -> AGENTS.md" in r.stdout
    assert "removed 1 markdown rule(s) from rules/" in r.stdout


def test_files_only_run_never_clobbers_an_already_rendered_plugin(tmp_path):
    claude_dir = tmp_path / "claude"
    monitors = (claude_dir / "dockwright" / "plugin" / "plugins" / "dockwright"
                / "monitors" / "monitors.json")
    monitors.parent.mkdir(parents=True)
    rendered = '[{"command": "/real/venv/bin/dockwright monitor-loop done"}]'
    monitors.write_text(rendered)

    run_sandboxed_setup(tmp_path)

    assert monitors.read_text() == rendered
    assert "@@DOCKWRIGHT_BIN@@" not in monitors.read_text()


def test_first_install_with_a_role_manifest_passes_roles_fresh(tmp_path, monkeypatch):
    from dockwright import doctor
    role = tmp_path / "claude" / "roles" / "GenericWorker"
    role.mkdir(parents=True)
    (role / "role.toml").write_text('core = "GenericWorker"\nlaunch = ["worker"]\n')
    overlay = tmp_path / "empty-overlay"; overlay.mkdir()
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(
        f'[paths]\noverlay_dir = "{overlay}"\n'
        '[agent_vars]\nexample_task_key = "TKT-SANDBOX-42"\n')
    claude_dir, _, _ = run_sandboxed_setup(tmp_path, {
        "DOCKWRIGHT_ORCH_BIN": str(REPO / ".venv" / "bin" / "orchestrator"),
        "DOCKWRIGHT_OVERLAY_DIR": str(overlay),
        "DOCKWRIGHT_CONFIG": str(cfg),
    })
    assert (claude_dir / "commands" / "dockwright-general-work.md").exists()
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    c = doctor.check_roles_fresh(REPO / "deploy" / "agents", claude_dir / "roles", claude_dir)
    assert c.ok and "match recompose" in c.detail, c.detail
