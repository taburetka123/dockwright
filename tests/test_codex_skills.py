from pathlib import Path

from dockwright import codex_skills


REPO_ROOT = Path(__file__).resolve().parent.parent
COMMANDS = REPO_ROOT / "deploy" / "commands"


def test_render_manager_resume_codex_skill_uses_current_command_body():
    text = codex_skills.render_skill_from_command(COMMANDS / "manager-resume.md")

    assert 'name: "manager-resume"' in text
    assert 'description: "Resume an orchestrator manager session from a handoff' in text
    assert 'argument-hint: "<handoff_id>"' in text
    assert "Resolve `<your sid>` from the session id" in text
    # Managers are Claude-only — the body arms Monitors with no codex-push bridge.
    assert "Arm the four Monitor tasks" in text
    assert "codex-push-watch" not in text
    assert "inotifywait -m -e create ~/.claude/orchestrator/questions/" not in text


def test_install_codex_skills_overwrites_stale_skill_from_command(tmp_path):
    stale_skill = tmp_path / "manager-resume" / "SKILL.md"
    stale_skill.parent.mkdir()
    stale_skill.write_text("old Monitor task text\n", encoding="utf-8")

    installed = codex_skills.install_codex_skills(COMMANDS, tmp_path)

    assert stale_skill in installed
    text = stale_skill.read_text(encoding="utf-8")
    assert "old Monitor task text" not in text
    assert "Arm the four Monitor tasks" in text
    assert "codex-push-watch" not in text


def test_setup_installs_codex_skills_from_commands():
    setup = (REPO_ROOT / "setup.sh").read_text(encoding="utf-8")

    assert "install-codex-skills" in setup
    assert "$CODEX_DIR/skills" in setup
