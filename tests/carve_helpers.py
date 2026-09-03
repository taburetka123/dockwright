import tomllib
from pathlib import Path

import pytest

from dockwright import compose

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = REPO_ROOT / "deploy" / "agents"

def _overlay_home() -> Path:
    new = Path.home() / ".claude" / "dockwright-overlay"
    legacy = Path.home() / ".claude" / "orchestrator-overlay"
    return new if new.exists() else legacy


OPERATOR_OVERLAY = _overlay_home()
OPERATOR_CONFIG = Path.home() / ".claude" / "dockwright.toml"


def _core_path(output_name: str) -> Path:
    core = CORE_DIR / (Path(output_name).stem + ".core.md")
    return core if core.is_file() else CORE_DIR / output_name


def operator_agent_vars() -> dict[str, str]:
    if not OPERATOR_CONFIG.is_file():
        return {}
    data = tomllib.loads(OPERATOR_CONFIG.read_text())
    section = data.get("agent_vars", {})
    if not isinstance(section, dict):
        return {}
    assert all(isinstance(k, str) and isinstance(v, str)
               for k, v in section.items()), "agent_vars must be str->str"
    return dict(section)


def operator_overlay_installed() -> bool:
    stems = {Path(compose.output_name(p.name)).stem
             for p in CORE_DIR.glob("*.md")}
    has_dropins = any((OPERATOR_OVERLAY / s).is_dir()
                      and any((OPERATOR_OVERLAY / s).glob("*.md"))
                      for s in stems)
    return bool(has_dropins and operator_agent_vars())


requires_operator_overlay = pytest.mark.skipif(
    not operator_overlay_installed(),
    reason="operator overlay not installed (generic clone)")


def compose_operator_with_warnings(output_name: str) -> tuple[str, list[str]]:
    dropins = compose.load_dropins(OPERATOR_OVERLAY, Path(output_name).stem)
    merged = {**compose.load_default_vars(CORE_DIR), **operator_agent_vars()}
    return compose.compose_text(
        _core_path(output_name).read_text(), dropins, merged)


def compose_operator(output_name: str) -> str:
    text, _ = compose_operator_with_warnings(output_name)
    return text


def compose_generic(output_name: str) -> str:
    text, _ = compose.compose_text(
        _core_path(output_name).read_text(), [],
        compose.load_default_vars(CORE_DIR))
    return text
