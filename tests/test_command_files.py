import re
from pathlib import Path

import pytest

from dockwright import compose

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMANDS = REPO_ROOT / "deploy" / "commands"
SKILLS = REPO_ROOT / "deploy" / "skills"
SCRIPTS = REPO_ROOT / "deploy" / "scripts"
PRESETS = REPO_ROOT / "deploy" / "presets"
AGENTS = REPO_ROOT / "deploy" / "agents"
DEPLOY = REPO_ROOT / "deploy"


FOLDED_SKILLS = [
    "dockwright-orchestrator-guide",
    "dockwright-recap",
    "dockwright-todo",
    "dockwright-dotodo",
    "dockwright-meta-improvement",
]


_RENDERED_GENERIC_FILES = [
    COMMANDS / "dockwright-general-work.md",
    PRESETS / "dockwright-fix-S.md",
    PRESETS / "dockwright-fix-M.md",
    PRESETS / "dockwright-fix-L.md",
    PRESETS / "scratch.md",
]

_UNBOUND_VAR_RE = re.compile(r"\{\{[A-Za-z0-9_]+\}\}")


def _render_with_defaults(path: Path) -> str:
    defaults = compose.load_default_vars(AGENTS)
    rendered, _warnings = compose.compose_text(path.read_text(), [], defaults)
    return rendered


@pytest.mark.parametrize("path", _RENDERED_GENERIC_FILES, ids=lambda p: p.name)
def test_edited_command_preset_has_no_unbound_vars_with_defaults(path):
    rendered = _render_with_defaults(path)
    unbound = _UNBOUND_VAR_RE.findall(rendered)
    assert not unbound, (
        f"{path.name}: {sorted(set(unbound))} left unbound under a defaults-only "
        "render — every {{var}} used in a shipped command/preset needs a generic "
        "default in deploy/agents/vars.defaults.toml")
