"""Docs-consistency guards for the two-tier PR-verification gate.

manager.md's verifier discipline is pure prose the manager model executes: the
load-bearing invariants — the first-match-wins classification order, the Tier-1
PR-comment record that makes the light gate explicit-not-silent, and the
preserved read-only Tier-2 spawn — live only there. Drift would silently
re-open the gap the split closed (docs/spend-vs-return-baseline-opus.md §6 Escape 1:
a prose PR opened with zero review).

Post-Step-7c the section splits two ways (locked decision #6 superpowers-shed):
the invariants live in CORE, so they are pinned against the GENERIC flavor
(present on every clone, no overlay needed); the operator Tier-2 binding
(`superpowers:code-reviewer` + the absolute verifier-settings preset path) is
an `[agent_vars]` value, so those pins compose the OPERATOR flavor and skip on
a generic clone. The generic flavor must still carry a WORKING Tier-2 fallback
(a plain read-only reviewer worker), never a hole.
"""
from pathlib import Path

from tests.carve_helpers import (
    compose_generic, compose_operator, requires_operator_overlay,
)

DEPLOYED_VERIFIER_PATH = str(
    Path.home() / ".claude/dockwright/presets/verifier-settings.json")
LEGACY_VERIFIER_PATH = str(
    Path.home() / ".claude/orchestrator/presets/verifier-settings.json")


def _manager_text() -> str:
    # The generic flavor — composable on every clone. The invariants below
    # are core content, identical in both flavors.
    return compose_generic("manager.md")


def _verifier_block(text: str) -> str:
    assert "Two-tier verification" in text, (
        "manager.md lost the 'Two-tier verification' section"
    )
    block = text.split("Two-tier verification", 1)[1]
    # Bound the trailing edge at the next sibling subsection so the guard
    # matches only the two-tier prose — not the following On mismatch / Why
    # paragraphs that share the enclosing ## section.
    for boundary in ("\n**On mismatch:**", "\n## "):
        if boundary in block:
            return block.split(boundary, 1)[0]
    return block


def test_classification_is_first_match_wins_with_four_steps():
    block = _verifier_block(_manager_text())
    assert "first match wins" in block.lower()
    for marker in ("1.", "2.", "3.", "4."):
        assert marker in block, f"classification step {marker} missing"
    assert "behavioral surface" in block.lower()
    assert "exceeds **100 LOC**" in block
    assert "git diff --shortstat origin/main..HEAD" in block


def test_tier1_is_explicit_never_a_silent_skip():
    block = _verifier_block(_manager_text())
    assert "Tier 1" in block
    assert "PR comment" in block, "Tier 1 must record a PR comment"
    assert "no comment" in block.lower(), (
        "the 'no comment => gate did not run' invariant must survive"
    )
    assert "never a silent skip" in block.lower()


def test_tier1_runs_the_four_inline_checks():
    block = _verifier_block(_manager_text()).lower()
    for check in ("no longer exists", "contradict", "structural break", "scope creep"):
        assert check in block, f"Tier 1 inline check '{check}' missing"


def test_tier2_generic_fallback_is_a_working_reviewer_spawn():
    # Locked decision #6: the generic core sheds the superpowers binding but
    # Tier 2 must keep a WORKING fallback, not a hole.
    block = _verifier_block(_manager_text())
    assert "read-only reviewer worker" in block
    assert "full-diff read" in block
    assert "no write tools" in block
    assert "superpowers" not in block.lower()
    assert "read-only by construction" in block, (
        "the read-only verifier-settings preset wiring is core, not operator"
    )


def test_behavioral_surface_and_code_extension_lists_present():
    block = _verifier_block(_manager_text())
    assert "deploy/**" in block
    assert "src/dockwright/**" in block
    for ext in (".py", ".kt", ".ts", ".sh"):
        assert ext in block, f"code extension {ext} missing from Tier-2 trigger list"


def test_tier2_references_classification_not_a_second_list():
    block = _verifier_block(_manager_text())
    assert "classification above" in block.lower(), (
        "the Tier-2 spawn paragraph must reference the classification, not "
        "re-enumerate the surface list (drift guard)"
    )


@requires_operator_overlay
def test_tier2_operator_binding_preserves_readonly_verifier_spawn():
    # The operator binding — superpowers:code-reviewer + the deployed ABSOLUTE
    # preset path (setup.sh rsyncs presets to ~/.claude/dockwright/presets/,
    # and neither the spawn shell nor claude's --settings expands `~`) — lives
    # in [agent_vars], so pin the composed operator flavor.
    block = _verifier_block(compose_operator("manager.md"))
    assert "superpowers:code-reviewer" in block
    assert DEPLOYED_VERIFIER_PATH in block, (
        "Tier 2 must keep the absolute verifier-settings preset path"
    )
    # Retired with the compat symlink: a toml re-pin of the orchestrator-era
    # home must fail here, not spawn a verifier off a dead path.
    assert LEGACY_VERIFIER_PATH not in block
    assert "read-only" in block.lower()
