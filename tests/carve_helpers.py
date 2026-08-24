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
    """True iff the live operator overlay is present: drop-ins on disk for ANY
    rendered agent AND a non-empty parsed [agent_vars]. False on a generic
    clone → the gated tests skip.

    ⚠️ The agent set is DERIVED, never hand-named. This asked only about
    `manager/` until 2026-08-05, so emptying that one directory made every
    consumer of `requires_operator_overlay` skip at exit 0 while the overlay
    was still installed and `worker/` drop-ins kept composing —
    green-because-nothing-was-checked, on a machine that had plenty to check.
    Folding manager drop-ins into the core is an ordinary change, so that is a
    foreseeable state rather than an exotic one.

    ⛔ 2026-08-11 — this helper was briefly widened to OR across both limbs and
    both roots, to match `test_two_tier_operator_pins._overlay_present`. That
    was WRONG and is reverted. The note stays so nobody re-derives it: **the two
    helpers answer different questions and must not converge.**

    - `_overlay_present()` asks *"is there an operator layer to CHECK?"* Either
      limb alone is a yes, because either alone can carry a poisoned render, and
      that module is publish-excluded and operator-only — a false RUN costs a
      loud failure on a machine that has something to look at.
    - THIS helper asks *"is THIS operator's install present, so that CONTENT
      assertions are meaningful?"* Its 15 consumers across three modules assert
      on drop-in-supplied sections AND on `~/.claude/skills/` assets that
      `deploy/skills/` deliberately does not ship. Either limb alone is not
      enough — hence the `and`, and hence reading `OPERATOR_OVERLAY` rather than
      `config.overlay_dir()`, because `compose_operator()` reads that same root
      and a gate admitting a root its own composer cannot read produces failures
      that misdescribe their cause.

    Measured cost of the wide version, on the three gated modules in a
    vars-only state (one `[agent_vars]` entry, no drop-ins): **5 failed / 31
    passed** with the OR, against **21 passed / 15 skipped** with the AND. The
    five demand drop-in-supplied sections and a `~/.claude/skills/` asset
    `deploy/skills/` does not ship, so on a machine in that state they cannot
    be cleared by any action — the permanent false positive
    `~/.claude/rules/drift-guard-tests.md` warns about, which trains the reader
    to shrug.

    ⚠️ An earlier version of this note said that break hit ADOPTERS. It does
    not, and the correction matters more than the claim: `~/.claude/skills/dockwright-publish/excludes.txt`
    lines 8-9 exclude `test_operator_compose.py` and
    `test_architect_pipeline_docs.py`, so 14 of the 15 gated tests never leave
    this repo and the shipped product sees only `test_presets.py`. The wide
    version was wrong for the reason above — a state on THIS machine with no
    clearing action — not for an adopter story that does not hold.
    """
    stems = {Path(compose.output_name(p.name)).stem
             for p in CORE_DIR.glob("*.md")}
    has_dropins = any((OPERATOR_OVERLAY / s).is_dir()
                      and any((OPERATOR_OVERLAY / s).glob("*.md"))
                      for s in stems)
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
