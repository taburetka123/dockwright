"""Shared helpers for the docs tests that pin composed OPERATOR agent
content, and for the forward operator-compose smoke
(tests/test_operator_compose.py).

Two composition flavors:

- `compose_operator(name)` — core + the LIVE operator overlay drop-ins + the LIVE
  operator agent_vars (the operator flavor). Docs tests pinning operator
  content (Copilot flow, verifier preset path, architect pipeline) read this
  text. `compose_operator_with_warnings(name)` is the sibling that also returns
  compose_text's warnings (e.g. unbound `{{vars}}`), for the smoke gate.
  The transition-era controlled-diff gate that once asserted this equals the
  pre-carve original modulo enumerated intended changes retired at Step 6
  (git history: tests/test_controlled_diff.py, 8425665 pins) — the carve/
  rename transition completed and was verified stable across 5 post-merge
  sittings.
- `compose_generic(name)` — core + vars.defaults.toml only, no overlay (the
  OSS flavor). The genericness gate runs the forbidden-token sweep on it.

WHY THESE READ THE LIVE OPERATOR STATE (not config.overlay_dir() /
config.agent_vars()):

The overlay retired its in-repo copy in Step 4c — the operator overlay now
lives ONLY at ~/.claude/dockwright-overlay/ (legacy fallback:
~/.claude/orchestrator-overlay/) with its vars in ~/.claude/dockwright.toml
[agent_vars]. But tests/conftest.py installs an
AUTOUSE hermetic fixture (`_dockwright_config_hermetic`) that points
DOCKWRIGHT_CONFIG at a nonexistent path for EVERY test, so `config.agent_vars()`
returns {} and `config.overlay_dir()` yields the default inside the suite. The
operator-content helpers must therefore read the live operator state EXPLICITLY
— OPERATOR_OVERLAY + the parsed dockwright.toml — bypassing config. That makes
these operator-machine-only tests: on a generic clone the overlay is absent, so
`operator_overlay_installed()` is False and `requires_operator_overlay` skips
them (the genericness gate, which needs no overlay, stays unconditional).
"""
import tomllib
from pathlib import Path

import pytest

from dockwright import compose

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = REPO_ROOT / "deploy" / "agents"

def _overlay_home() -> Path:
    # deprecated, one release: legacy fallback while orchestrator-era overlay
    # installs migrate to the dockwright-named home.
    new = Path.home() / ".claude" / "dockwright-overlay"
    legacy = Path.home() / ".claude" / "orchestrator-overlay"
    return new if new.exists() else legacy


# The LIVE operator state, read explicitly (Path.home() honors $HOME, so the
# generic-clone simulation `HOME=$(mktemp -d) pytest …` correctly sees no
# overlay). NOT config.overlay_dir() / config.agent_vars() — conftest's
# hermetic fixture blanks those inside the suite (see module docstring).
OPERATOR_OVERLAY = _overlay_home()
OPERATOR_CONFIG = Path.home() / ".claude" / "dockwright.toml"


def _core_path(output_name: str) -> Path:
    """Resolve the core source for a composed OUTPUT name (X.core.md wins)."""
    core = CORE_DIR / (Path(output_name).stem + ".core.md")
    return core if core.is_file() else CORE_DIR / output_name


def operator_agent_vars() -> dict[str, str]:
    """The operator's `[agent_vars]` parsed straight from the live
    ~/.claude/dockwright.toml — {} when the file or the section is absent
    (generic clone)."""
    if not OPERATOR_CONFIG.is_file():
        return {}
    data = tomllib.loads(OPERATOR_CONFIG.read_text())
    section = data.get("agent_vars", {})
    if not isinstance(section, dict):
        return {}
    assert all(isinstance(k, str) and isinstance(v, str)
               for k, v in section.items()), "agent_vars must be str->str"
    return dict(section)


def operator_forbidden_tokens() -> tuple[str, ...]:
    """[genericness].extra_forbidden_tokens from the LIVE ~/.claude/dockwright.toml
    (module-scope live read — conftest blanks config.*): operator-real identity
    tokens the shipped tree must never contain. () on a generic clone, so
    consumers must stay valid (vacuous) with an empty list."""
    cfg = Path.home() / ".claude" / "dockwright.toml"
    if cfg.is_file():
        try:
            val = tomllib.loads(cfg.read_text()).get("genericness", {}).get("extra_forbidden_tokens", [])
            return tuple(t for t in val if isinstance(t, str) and t)
        except (tomllib.TOMLDecodeError, OSError):
            pass
    return ()


def operator_overlay_installed() -> bool:
    """True iff the live operator overlay is present: manager drop-ins on disk
    AND a non-empty parsed [agent_vars]. False on a generic clone → the gated
    tests skip."""
    manager_dir = OPERATOR_OVERLAY / "manager"
    has_dropins = manager_dir.is_dir() and any(manager_dir.glob("*.md"))
    return bool(has_dropins and operator_agent_vars())


# Shared skip marker for the operator-content tests (composed-operator docs
# pins, the operator-compose smoke). Evaluated once at import per pytest
# process.
requires_operator_overlay = pytest.mark.skipif(
    not operator_overlay_installed(),
    reason="operator overlay not installed (generic clone)")


def compose_operator_with_warnings(output_name: str) -> tuple[str, list[str]]:
    """Same composition as `compose_operator`, but also surfaces
    compose_text's warnings (e.g. unbound `{{vars}}` left literal) — the
    operator-compose smoke asserts these are empty."""
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
