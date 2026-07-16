"""E2E: sandboxed setup.sh file-deploy into a tmp prefix (spec S6)."""
import os, subprocess, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

def run_sandboxed_setup(tmp_path, extra_env=None, codex=True):
    """Sandboxed FILES_ONLY setup.sh run with DETERMINISTIC codex presence.

    PATH is pinned to "<stub-bin>:/usr/bin:/bin" in both modes so results never
    depend on whether the host machine has codex installed: codex=True plants an
    executable stub; codex=False leaves the stub dir empty.
    """
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
    r = subprocess.run(["bash", str(REPO / "setup.sh")], env=env,
                       capture_output=True, text=True, cwd=str(REPO))
    assert r.returncode == 0, r.stdout + r.stderr
    return claude_dir, codex_dir

def test_files_only_deploys_commands_and_scripts(tmp_path):
    claude_dir, codex_dir = run_sandboxed_setup(tmp_path)
    assert (claude_dir / "commands" / "dockwright-general-work.md").exists()
    assert (claude_dir / "scripts" / "loops_status.py").exists()
    assert (claude_dir / "dockwright" / "loops-registry.md").exists()

def test_files_only_skips_machine_mutation(tmp_path):
    claude_dir, _ = run_sandboxed_setup(tmp_path)
    # settings.json (hook install) must NOT be created in FILES_ONLY mode
    assert not (claude_dir / "settings.json").exists()

def test_orch_bin_override_renders_transforms_in_files_only(tmp_path):
    """DOCKWRIGHT_ORCH_BIN drives the deploy-time file transforms (compose,
    command/preset render, codex mirror + skill wrappers) even in FILES_ONLY —
    the seam the Task-10 byte-equivalence gate relies on. Hermetic: an empty
    overlay + a fixture config point compose at the defaults-only (generic)
    flavor, with an operator agent_var overriding one default."""
    overlay = tmp_path / "empty-overlay"; overlay.mkdir()
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(
        f'[paths]\noverlay_dir = "{overlay}"\n'
        '[agent_vars]\nexample_task_key = "TKT-SANDBOX-42"\n')
    claude_dir, codex_dir = run_sandboxed_setup(tmp_path, {
        "DOCKWRIGHT_ORCH_BIN": str(REPO / ".venv" / "bin" / "orchestrator"),
        "DOCKWRIGHT_OVERLAY_DIR": str(overlay),
        "DOCKWRIGHT_CONFIG": str(cfg),
    })
    # Agents composed + rendered: the {{example_task_key}} var is substituted
    # by the operator value (config wins over the vars.defaults.toml default),
    # and NO literal {{ survives.
    manager = claude_dir / "agents" / "manager.md"
    assert manager.exists()
    body = manager.read_text()
    assert "{{" not in body
    assert "TKT-SANDBOX-42" in body
    # Commands rendered to both dests (var-free today → byte-stable), no {{.
    for d in (claude_dir, codex_dir):
        cmd = d / "commands" / "dockwright-general-work.md"
        assert cmd.exists()
        assert "{{" not in cmd.read_text()
    # Codex agent mirror + skill wrappers generated (only with a render binary),
    # from the rendered content — no literal {{.
    assert (codex_dir / "agents" / "manager.toml").exists()
    skills = list((codex_dir / "skills").glob("*/SKILL.md"))
    assert skills, "no codex skill wrappers generated"
    assert all("{{" not in s.read_text() for s in skills)


def test_codex_skill_wrappers_scoped_to_this_repo(tmp_path):
    """setup.sh must generate codex skill wrappers ONLY for the commands THIS
    repo deploys — never for FOREIGN commands other deployers dropped into
    $CODEX_DIR/commands. install-codex-skills globs its source dir and clobbers
    <stem>/SKILL.md, so pointing it at the deployed commands dir (which on an
    operator machine holds ~25 foreign commands) would overwrite foreign
    wrappers. The fix stages this repo's command sources in a temp dir."""
    codex_dir = tmp_path / "codex"
    # Plant a FOREIGN command + its existing wrapper (sentinel content).
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
    # Foreign wrapper untouched (byte-for-byte) — no wrapper regenerated for it.
    assert foreign_skill.read_text() == SENTINEL
    # This repo's own commands DID get wrappers.
    assert (codex_dir / "skills" / "dockwright-general-work" / "SKILL.md").exists()


def test_files_only_without_orch_bin_skips_transforms(tmp_path):
    """The other side of the seam: bare FILES_ONLY (no DOCKWRIGHT_ORCH_BIN) still
    deploys commands verbatim but skips the binary-driven transforms — no
    composed agents, no codex skill wrappers (Task 1 behavior preserved)."""
    claude_dir, codex_dir = run_sandboxed_setup(tmp_path)
    assert (claude_dir / "commands" / "dockwright-general-work.md").exists()
    assert not (claude_dir / "agents" / "manager.md").exists()
    assert not (codex_dir / "skills").exists()


def test_overlay_payload_deploys(tmp_path):
    overlay = tmp_path / "overlay"
    (overlay / "commands").mkdir(parents=True)
    (overlay / "commands" / "op-only.md").write_text("# operator command\n")
    (overlay / "presets").mkdir()
    (overlay / "presets" / "op-preset.md").write_text("# operator preset\n")
    (overlay / "scripts").mkdir()
    (overlay / "scripts" / "op.sh").write_text("#!/bin/bash\ntrue\n")
    claude_dir, codex_dir = run_sandboxed_setup(
        tmp_path, {"DOCKWRIGHT_OVERLAY_DIR": str(overlay)})
    assert (claude_dir / "commands" / "op-only.md").exists()
    assert (codex_dir / "commands" / "op-only.md").exists()
    assert (claude_dir / "dockwright" / "presets" / "op-preset.md").exists()
    deployed = claude_dir / "scripts" / "op.sh"
    assert deployed.exists() and os.access(deployed, os.X_OK)


def test_migration_relocates_preseeded_orchestrator_state(tmp_path):
    """A pre-existing legacy orchestrator/ state dir is migrated to dockwright/
    with a compat symlink left at the old path — setup.sh runs `migrate-state`
    before any deploy copy. RENDER_BIN-gated, so it fires only with a render
    binary (DOCKWRIGHT_ORCH_BIN in the sandbox)."""
    claude_dir = tmp_path / "claude"
    (claude_dir / "orchestrator" / "active").mkdir(parents=True)
    (claude_dir / "orchestrator" / "active" / "seed.json").write_text('{"seed": 1}')
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
    # Legacy path is now a compat symlink resolving into the new dockwright/ home…
    assert orch.is_symlink()
    assert orch.resolve() == (claude_dir / "dockwright").resolve()
    # …and the pre-seeded state moved with it.
    assert (claude_dir / "dockwright" / "active" / "seed.json").read_text() == '{"seed": 1}'


def test_no_codex_on_path_never_creates_codex_dir(tmp_path):
    claude_dir, codex_dir = run_sandboxed_setup(tmp_path, codex=False)
    assert (claude_dir / "commands" / "dockwright-general-work.md").exists()
    assert (claude_dir / "scripts" / "loops_status.py").exists()
    assert not codex_dir.exists()

def test_no_codex_with_render_bin_still_skips_codex_dir(tmp_path):
    overlay = tmp_path / "empty-overlay"; overlay.mkdir()
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(
        f'[paths]\noverlay_dir = "{overlay}"\n'
        '[agent_vars]\nexample_task_key = "TKT-SANDBOX-42"\n')
    claude_dir, codex_dir = run_sandboxed_setup(tmp_path, {
        "DOCKWRIGHT_ORCH_BIN": str(REPO / ".venv" / "bin" / "orchestrator"),
        "DOCKWRIGHT_OVERLAY_DIR": str(overlay),
        "DOCKWRIGHT_CONFIG": str(cfg),
    }, codex=False)
    # Claude-side transforms all present…
    assert (claude_dir / "agents" / "manager.md").exists()
    assert (claude_dir / "commands" / "dockwright-general-work.md").exists()
    # …codex side entirely untouched.
    assert not codex_dir.exists()


def test_files_only_does_not_create_worker_home(tmp_path):
    # The worker-home step is FILES_ONLY-gated; a sandbox run must not mkdir any
    # worker home. Point the config at a sentinel under tmp and assert absence.
    marker = tmp_path / "sentinel-worker-home"
    claude_dir, _ = run_sandboxed_setup(
        tmp_path, {"CLAUDE_ORCH_WORKER_HOME": str(marker)})
    assert not marker.exists()
