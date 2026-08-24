"""Docs-consistency guards for the two-tier PR-verification gate.

manager.md's verifier discipline is pure prose the manager model executes: the
load-bearing invariants — who OWNS the loop, the first-match-wins
classification, the Tier-1 PR-comment record that makes the light gate
explicit-not-silent, and a Tier-2 fallback that is a real adversarial loop —
live only there. Drift would silently re-open the gap the split closed
(docs/spend-vs-return-baseline-opus.md §6 Escape 1: a prose PR opened with
zero review).

2026-08-01 (tier2-to-worker): ownership moved from the MANAGER to the PR's
AUTHOR. The manager no longer spawns a verifier worker per PR and is no
longer the postman between author and reviewer — routing every round through
it spent ~25 human-facing turns on mechanics. What the manager keeps is
pinned here too, because it is the ONLY counterweight left to an author
grading its own homework: the escalation of any DOWNGRADE/defer/accept call,
reading the reviewer's own verdict rather than the author's summary, and the
merge itself.

2026-08-03 (tier2-to-worker follow-up) carved ARM 1 back out — a behavioural
surface returned to a manager-spawned verifier on the settings preset.
2026-08-05 (retire-arm1) RETIRED that carve-out on the operator's explicit
ruling: the author dispatches its own reviewer for EVERY Tier-2 diff and
states READ-ONLY in the brief. So the classification now decides the TIER
only; it no longer decides WHO.

⚠️ THE TRADEOFF THAT RULING ACCEPTS, recorded here so a later reader does not
"discover" it as a defect and revert it. A reviewer a worker dispatches
cannot be denied write access: on 2026-08-03 one made three mutations to an
author's real tree and one of them (`!=` weakened to `<`) reddened NOTHING,
and on 2026-08-02 a declared read-only `subagent_type` (built-in `Explore`)
wrote and committed anyway. The operator was shown both and chose this. So
"the brief says read-only" is a CONVENTION, and what actually guards the tree
is the author's own `git status --porcelain` after every round plus running
reviewer mutations by absolute path outside the checkout. Those two clauses
are pinned on both sides below; they are no longer a backstop, they are the
whole guard.

The invariants live in CORE, so they are pinned against the GENERIC flavor
(present on every clone, no overlay needed) and must stay a WORKING
procedure there — an operator flow doc is bound through `{{tier2_flow_ref}}`
and a generic clone has none, so a bare pointer would be a hole.

⚠️ BUT GENERIC IS NOT WHAT A MANAGER READS. Every guard here reads
`compose_generic`, which makes the cheapest re-installation of the retired lane
a drop-in — or a single `[agent_vars]` value — under the operator overlay:
invisible to this module and to any grep of this repo, live in the file the
session loads. That is closed in the SIBLING module
`tests/test_two_tier_operator_pins.py`, not here, because its fixtures encode
one machine's operator state and it is therefore publish-excluded. The one
operator leg left in this file is
`test_no_clause_reinstalls_a_manager_review_lane`, which is a denylist of
generic canon vocabulary and so is safe to ship. Same "verify by RENDERING, not
by grepping the source" discipline `~/.claude/rules/sdd-model-tiers.md`
records, one layer down.

The read-only verifier PRESET is no longer part of Tier 2 (the author
dispatches an in-session reviewer subagent, it spawns no worker). It moved to
the one lane where the manager still spawns a verifier WORKER — findings
validation — and is pinned there, so the wiring cannot quietly evaporate with
the lane that used to carry it. That lane is NOT what the 2026-08-05 ruling
retired: its subject is a worker's finding in a domain nobody can check, and
it has no author to delegate to.
"""
import re
from pathlib import Path

from dockwright import compose

from tests.carve_helpers import CORE_DIR, compose_generic, compose_operator

DEPLOYED_VERIFIER_PATH = str(
    Path.home() / ".claude/dockwright/presets/verifier-settings.json")
LEGACY_VERIFIER_PATH = str(
    Path.home() / ".claude/orchestrator/presets/verifier-settings.json")


def _rendered_agents() -> tuple[str, ...]:
    """DERIVED from the core dir, never hand-listed.

    A hand-maintained tuple is unguarded at its next entry by construction:
    add a fourth agent file and it renders, deploys, and is skipped by every
    corpus sweep below — the review+spawn set, the absolute-write-denial
    sweep, and both operator legs. The one `==` check that would have caught
    it lives in `test_agent_size_ceiling.py`, which is publish-excluded, so an
    adopter's clone had nothing.

    ⚠️ ENUMERATE, do not CLASSIFY. The first version of this derivation
    filtered `if p.name.endswith(".core.md")` — and `compose` supports TWO core
    forms: `X.core.md` composes to `X.md`, and a plain `X.md` composes to
    itself unchanged. A plain `deploy/agents/scout.md` carrying a spawn+review
    sentence therefore rendered, deployed, and stayed invisible to all three
    sweeps, and to the overlay manifest that derives its directories from this
    tuple. A derivation that classifies fails open on the shape it does not
    recognise, which is the exact hole this function exists to close
    (`~/.claude/rules/drift-guard-tests.md` § ADD-ONE). `output_name` already
    normalizes both forms, so taking every `*.md` and letting it normalize
    makes over-inclusion the failure mode — the set, because compose fails loud
    if both forms exist for one stem.
    """
    return tuple(sorted({
        compose.output_name(p.name) for p in Path(CORE_DIR).glob("*.md")}))


RENDERED_AGENTS = _rendered_agents()


def _manager_text() -> str:
    # The generic flavor — composable on every clone. The invariants below
    # are core content, identical in both flavors.
    return compose_generic("manager.md")


SECTION_HEAD = "**Two-tier verification —"


def _verifier_block(text: str) -> str:
    # Anchor on the BOLD HEADING, not the bare phrase. The bare phrase is now
    # also a cross-reference in the model-roster row ~150 lines above, and
    # anchoring on it silently extended the block over half the file — every
    # in-block guard kept passing while asserting about the wrong text.
    assert text.count(SECTION_HEAD) == 1, (
        f"expected exactly one {SECTION_HEAD!r} heading, found "
        f"{text.count(SECTION_HEAD)} — the block extractor cannot resolve "
        f"which one the guards below are about"
    )
    block = text.split(SECTION_HEAD, 1)[1]
    # Bound the trailing edge at the next sibling subsection so the guard
    # matches only the two-tier prose — not the following On mismatch / Why
    # paragraphs that share the enclosing ## section.
    for boundary in ("\n**On mismatch:**", "\n## "):
        if boundary in block:
            return block.split(boundary, 1)[0]
    return block


def _findings_validation_block(text: str) -> str:
    """The lane where the manager still spawns a verifier WORKER."""
    marker = "validate, don't relay"
    assert text.count(marker) == 1, (
        "manager.md lost (or duplicated) the findings-validation lane"
    )
    block = text.split(marker, 1)[1]
    return block.split("\n## ", 1)[0]


def test_every_tier2_diff_is_the_authors_and_the_manager_is_not_the_postman():
    block = _verifier_block(_manager_text())
    assert "classification decides the TIER" in block, (
        "after the 2026-08-05 retirement the classification decides the TIER "
        "only — a section that still says it decides WHO has the split back"
    )
    assert "every Tier-2 diff is the AUTHOR's" in block, (
        "the ruling's whole content: no arm, no exception, one owner"
    )
    assert "You own no review lane" in block, (
        "the manager must be told it owns NO review lane — 'the author "
        "usually runs it' is how the lane comes back"
    )
    assert "postman" in block, (
        "the manager must be told explicitly it does not route rounds"
    )
    assert "When YOU open the PR you are the author" in block, (
        "a manager-opened PR still owes the same loop — that carve-in must "
        "survive, or the manager's own PRs become the ungated hole"
    )


CARVE_OUT = "`ready-for-verifier` stays retired"


def test_ready_for_verifier_handoff_is_retired_from_core():
    # The retired shape must not survive anywhere in the core text: a single
    # leftover 'wait for the signal' sentence re-installs the manager as
    # postman for whichever reader hits that line first.
    text = _manager_text()
    # Pin the carve-out itself. Without this the strip target can be reworded
    # in a later PR, after which the test asserts against its OWN legal
    # occurrence and goes red for a reason that has nothing to do with drift.
    assert text.count(CARVE_OUT) == 1, (
        "the one legal `ready-for-verifier` mention (the retirement notice) "
        "moved or was reworded — re-point CARVE_OUT in the same edit"
    )
    assert "ready-for-verifier" not in text.replace(CARVE_OUT, ""), (
        "core still describes the retired ready-for-verifier handoff"
    )


# The handoff is a SHAPE, not a token: "wait for the worker's `review-ready`
# signal and then dispatch the reviewer for it" re-installs the manager as
# postman without ever typing the retired string. Anything that puts the
# manager in a waiting-for-a-signal or dispatching-on-the-worker's-behalf
# posture is the same defect under a new name.
HANDOFF_SHAPES = (
    "wait for the worker's",
    "waits for the worker's",
    "signal and then dispatch",
    "dispatch the reviewer for it",
    "dispatch a reviewer for the worker",
    "spawn the reviewer for it",
    "run the rounds yourself",
    "review-ready",
    "ready-for-review signal",
)


def test_no_reworded_postman_handoff_anywhere_in_core():
    text = _manager_text().lower()
    for shape in HANDOFF_SHAPES:
        assert shape not in text, (
            f"core reads as the retired postman handoff ({shape!r}) under a "
            f"different wording — the author runs its own rounds"
        )


def test_classification_is_first_match_wins_over_all_three_triggers():
    block = _verifier_block(_manager_text())
    assert "first match wins" in block.lower()
    assert "behavioral surface" in block.lower()
    assert "code or config file" in block.lower()
    assert "exceeds **100 LOC**" in block
    assert "git diff --shortstat origin/main..HEAD" in block
    assert "Otherwise **Tier 1**" in block, (
        "the prose-only fallback must be the LAST arm, never a first-class "
        "alternative a code diff could match"
    )


def test_the_unknown_case_still_fails_closed():
    # `unsure ⇒ arm 1` was the classifier's default-deny fallback. Retiring
    # arm 1 by DELETING that phrase, rather than replacing it, would leave the
    # classifier failing OPEN on a shape it does not recognise — the exact
    # inversion `~/.claude/rules/investigation-evidence.md` forbids for a
    # guard on an irreversible axis.
    classify = _bullet(_verifier_block(_manager_text()), "- **Classify")
    assert "unsure ⇒ Tier 2" in classify, (
        "the classify bullet lost its default-deny fallback — an unrecognised "
        "surface must escalate to Tier 2, never fall through to Tier 1"
    )
    worker = compose_generic("worker.md")
    assert "unsure ⇒ Tier 2" in worker, (
        "worker.md is the file that DOES the classifying, so its fallback is "
        "the one that decides"
    )


def test_size_can_never_downgrade_a_code_or_behavioral_diff():
    block = _verifier_block(_manager_text())
    assert "Never downgrade a code or behavioral-surface diff to Tier 1" in block
    assert "fires only on what the first two already cleared" in block, (
        "the ORDER guarantee is the guard; without it 'first match wins' is "
        "decoration and a 20-line .py PR reads as Tier 1"
    )


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


def test_behavioral_surface_and_code_extension_lists_present():
    block = _verifier_block(_manager_text())
    assert "deploy/**" in block
    assert "src/dockwright/**" in block
    for ext in (".py", ".kt", ".ts", ".sh"):
        assert ext in block, f"code extension {ext} missing from Tier-2 trigger list"
    # Inherited from the retired test_tier2_references_classification_not_a_
    # second_list: one surface list, never two that can drift apart.
    assert block.count("src/dockwright/**") == 1, (
        "the behavioral-surface list appears twice — a second copy drifts "
        "from the first and the reader obeys whichever it reads"
    )


def test_manager_keeps_the_downgrade_verdict_and_merge_duties():
    # The three counterweights to an author running its own gate. Each is
    # separately load-bearing: drop the first and a self-approved downgrade
    # never surfaces; drop the second and "Tier-2 APPROVE" is unfalsifiable;
    # drop the third and the author merges its own work. With arm 1 retired
    # these are the ONLY things the manager still holds over a PR.
    block = _verifier_block(_manager_text())
    assert "DOWNGRADE, defer, or accept as residual" in block
    assert "never self-approve" in block
    assert "read the reviewer's OWN verdict" in block
    assert "not the summary" in block, (
        "the summary is written by the same session that ran the gate — it "
        "cannot be the proof the gate ran"
    )
    assert "a worker never merges its own PR" in block


def test_readonly_verifier_preset_lives_in_the_findings_validation_lane():
    # Post-2026-08-01 this is the ONLY lane where the manager still spawns a
    # verifier WORKER, so the read-only wiring must ride here — not in Tier 2,
    # which no longer spawns anything. The 2026-08-05 arm-1 retirement did NOT
    # touch this lane: its subject is a worker's FINDING, not a PR diff, and
    # it has no author to delegate the dispatch to.
    text = _manager_text()
    lane = _findings_validation_block(text)
    assert "read-only by settings" in lane
    assert DEPLOYED_VERIFIER_PATH in lane, (
        "the findings-validation verifier must keep the absolute "
        "verifier-settings preset path"
    )
    # Generic-flavor pins only — this module reads compose_generic, which
    # never sees operator [agent_vars]. The OPERATOR flavor's legacy-path and
    # tilde guards live in tests/test_presets.py::
    # test_manager_agent_wires_verifier_preset_on_verifier_spawns; a toml
    # re-pin to the orchestrator-era home fails THERE, not here.
    assert LEGACY_VERIFIER_PATH not in text
    assert "~/.claude/dockwright/presets/verifier-settings.json" not in text
    assert DEPLOYED_VERIFIER_PATH not in _verifier_block(text), (
        "Tier 2 dispatches an in-session reviewer subagent and spawns no "
        "worker — a --settings preset there is stale wiring"
    )


SPAWN_MECHANICS = ("spawn_worker", "resume_worker", "send_manager_to_worker",
                   "extra_args", "--settings", "spawn a", "spawn the",
                   "spawn it", "spawn that", "spawns a", "spawns the",
                   "ask_manager", "hand it over", "hands it over")
# Deliberately BARE. An earlier version listed phrasings ("review the diff",
# "review pass", "code-review", …) and missed "review the PR" — the most
# natural wording of the forbidden action — so `Spawn a fresh worker to
# review the PR on the author's behalf.` named both vocabularies in one
# sentence and was still invisible. Widening to the stem costs nothing: it
# extracts the SAME legal sentences pinned below, so over-inclusion here
# is free and under-inclusion is what fails open.
#
# 2026-08-05: the mechanic list gained `ask_manager`, `hands it over`,
# `spawns a`, `spawns the`. Retiring arm 1 moved the danger from the manager's
# side to the WORKER's — the cheapest re-install is now the author asking for
# a lane rather than the manager offering one — and `ask_manager` matched
# none of the previous stems. `spawns a` is here because "the manager spawns a
# verifier" does not contain "spawn a".
REVIEW_WORDS = ("review", "verifier", "tier 2", "tier-2", "adversarial")

# The COMPLETE set of sentences ACROSS ALL THREE rendered agents that pair a
# review word with a spawn mechanic, pinned by `==` and normalized for $HOME.
#
# This replaced a pair of allowlisted LANES. A lane is a window, and a window
# grows: the previous guard exempted the whole findings-validation lane and
# the whole `## Headless / no-human spawns` section (~4.5 KB) in two
# str.replace calls, so planting a per-PR reviewer-spawn recipe inside either
# one fired nothing. An enumerated sentence cannot grow. A new review+spawn
# sentence ANYWHERE — including in those two lanes — now fails until someone
# adds it here deliberately.
#
# 2026-08-05: the arm-1 spawn sentence LEFT this set, which is the mechanical
# statement of the retirement. It is also why the set is now scanned over
# worker.md and dockwright-reviewer.md too: with the lane gone from the
# manager, the re-install has to enter through whichever file still describes
# a dispatch.
LEGAL_REVIEW_SPAWN_SENTENCES = frozenset({
    # The lane where the manager still spawns a verifier WORKER for findings.
    'Spawn it read-only by settings — `extra_args=["--settings", '
    '"~/.claude/dockwright/presets/verifier-settings.json"]` (absolute — '
    '`--settings` never expands `~`; denies Write/Edit/NotebookEdit + '
    'mutating git/gh Bash, though tool-scoped).',
    # Not a review dispatch: the worker is the manager's reviewer-of-DESIGN,
    # and this sentence is about amending a spec, not about a PR diff. It
    # enters the set only because `ask_manager` joined SPAWN_MECHANICS.
    "The worker is your independent reviewer-of-design, not a mechanical "
    "executor: it re-opens the design on the received spec (challenging YAGNI "
    "cuts, surfacing holes, proposing alternatives — amending via "
    "`ask_manager` if material), plans before coding, writes tests first, "
    "verifies each \"phase done\" claim by actually running the verification "
    "command, and requests a code review at high-risk checkpoints, not only "
    "at PR-open",
    # Preset mechanics, not a review dispatch: explains that a resumed
    # read-only verifier stays read-only.
    "Gotchas, still load-bearing: (1) a caller `--settings` REPLACES the "
    "spawner's default (last-wins, not merged), so any composed copy must "
    "keep the preset's top-level keys (`enableAllProjectMcpServers`, the "
    "remote-control-off pair); (2) the deployed preset carries "
    "`permissions.additionalDirectories` (setup.sh resolves your `[paths]` "
    "code roots to absolute paths) — that is what clears the out-of-cwd "
    "first-write gate for a task repo/worktree outside the spawn cwd, so "
    "composed copies must keep it too; (3) commands containing `$…`, "
    "heredocs, or `&&` chains can never be ALLOWLISTED (the permission "
    "system's expansion guard) — under the preset's auto mode the safety "
    "classifier vets them at runtime instead (this is what keeps headless "
    "workers from stalling), so expansion-free prompts (`printenv`, not "
    "`echo $VAR`) are a fast-path preference, not a survival requirement; "
    "(4) a composed copy IS sticky across `resume_worker` — the final "
    "composed extra_args are persisted on the assignment record at spawn and "
    "replayed verbatim on resume, so the resumed worker comes back on the "
    "SAME settings it was born with (a read-only verifier resumes read-only, "
    "never widened onto the auto headless default)",
})


def _sentences(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        for part in line.replace("! ", ". ").split(". "):
            if part.strip():
                out.append(part)
    return out


def _review_spawn_sentences(text: str) -> set[str]:
    home = str(Path.home())
    return {
        s.strip().replace(home, "~")
        for s in _sentences(text)
        if any(m in s.lower() for m in SPAWN_MECHANICS)
        and any(w in s.lower() for w in REVIEW_WORDS)
    }


def test_review_plus_spawn_sentences_are_exactly_the_pinned_set():
    # ADD-ONE, not delete-one: the retirement does not die by someone
    # deleting the flip sentences (the guards above catch that) — it dies by
    # someone ADDING a spawn back, so the file reads "the author runs it" AND
    # hands the manager a way to run it instead.
    #
    # Residual, stated rather than papered over: extraction still depends on
    # the two vocabularies above, so a recipe using none of their words, or
    # split across two sentences ("Spawn a worker. Tell it to review the
    # PR."), is not seen. The prose guard in the section itself is what
    # covers those; this closes every shape that names what it is doing.
    found = set()
    for name in RENDERED_AGENTS:
        found |= _review_spawn_sentences(compose_generic(name))
    assert found == LEGAL_REVIEW_SPAWN_SENTENCES, (
        "the set of sentences pairing a reviewer/verifier with a spawn "
        "mechanic changed.\nADDED (each one hands someone a way to run a "
        f"review the retirement says the author runs):\n"
        + "\n".join(sorted(found - LEGAL_REVIEW_SPAWN_SENTENCES))
        + "\nMISSING (a legal spawn site was reworded or removed — re-pin "
          "deliberately):\n"
        + "\n".join(sorted(LEGAL_REVIEW_SPAWN_SENTENCES - found))
    )


# The OPERATOR render is what a live manager loads; the generic one is what
# every guard above reads. A drop-in under the operator overlay is therefore
# the cheapest way to re-install the retired lane: it never touches this repo.
# These are the sentences the operator layer legitimately adds on top of the
# generic set — enumerated, so a new one fails until someone pins it.
def test_the_classification_grows_no_second_tier1_arm():
    # I4: the classify bullet is a free-form disjunction, so a new arm is an
    # append away — "Tier 1 if the author reports the change is mechanical"
    # passed every other guard here. Exactly one arm may route to Tier 1.
    block = _verifier_block(_manager_text())
    classify = block.split("- **Classify", 1)[1].split("\n- ", 1)[0]
    assert classify.count("**Tier 1**") == 1, (
        "the classification grew a second Tier-1 arm — Tier 1 is the "
        "'otherwise' fallback and must be reachable exactly one way"
    )
    assert classify.count("**Tier 2**") == 1, (
        "the classification grew a second Tier-2 arm — one disjunction, or "
        "the two lists drift apart"
    )
    assert "on size grounds or any other" in classify, (
        "the anti-downgrade guard must not be scoped to SIZE alone: the "
        "cheapest new exemption is a non-size one ('mechanical', 'already "
        "covered by tests')"
    )
    assert "size, context or urgency exemption" in classify, (
        "the text must state the exemption does not exist, so that adding "
        "one is a visible self-contradiction rather than a quiet append — "
        "no substring test can enumerate every phrasing of an exemption"
    )


def _bullet(block: str, head: str) -> str:
    assert block.count(head) == 1, f"expected exactly one {head!r} bullet"
    return block.split(head, 1)[1].split("\n- ", 1)[0].split("\n\n", 1)[0]


def test_the_worker_publishes_the_verdict_the_manager_is_told_to_read():
    # The producer half of manager.md's "read the reviewer's OWN verdict".
    # Shipping only the consumer half leaves the manager reading something
    # nobody is told to write — it then merges on the summary (the exact
    # failure that section names) or re-opens the postman loop.
    worker = compose_generic("worker.md")
    manager = _manager_text()
    assert 'artifact_put(phase="review"' in worker, (
        "the worker must publish the reviewer's verdict, not just its own "
        "summary of it"
    )
    assert "in the reviewer's OWN words" in worker
    assert "or a PR comment with no task key" in worker, (
        "keyless one-off workers have no artifact store — without the PR "
        "comment fallback the verdict is unwritable for a whole class of "
        "workers and the manager's check has nothing to read"
    )
    assert "no record ⇒ the gate did not run" in worker
    assert "no record ⇒ the gate did not run" in manager, (
        "producer and consumer must state the same rule, or one side's "
        "wording drifts and the pair stops meaning anything"
    )


# ⚠️ THE OPERATOR-FLAVOUR PINS LIVE IN A SIBLING MODULE, deliberately:
# `tests/test_two_tier_operator_pins.py`. Everything here reads
# `compose_generic` — the render this repo owns and every clone can compose —
# and is therefore blind to the overlay drop-ins and `[agent_vars]` that a live
# session actually loads. That blind spot was measured live (two green
# re-install routes; see that module's docstring), so it is closed there rather
# than here: its fixtures encode ONE machine's operator state, which would
# false-fail for any adopter, so it is publish-excluded while this module ships.


# --- the 2026-08-05 retirement: one owner, no arm, no exception --------------
# What replaced the split. These stay PRESENCE pins deliberately: converting
# them into absence assertions ("the manager has no lane") would leave this
# axis guarded by three denylists and no positive statement at all, which is
# the coincidence-detector shape ~/.claude/rules/drift-guard-tests.md opens
# with. A denylist cannot enumerate the phrasings; a presence pin cannot be
# satisfied by silence.
ONE_OWNER_MANAGER_SIDE = (
    "classification decides the TIER",
    "every Tier-2 diff is the AUTHOR's",
    "You own no review lane",
    "dispatch your own reviewer, saying READ-ONLY in the brief",
    "A convention, not a boundary",
    "reviewer mutations by absolute path outside the checkout",
    "`git status --porcelain` every round",
)
ONE_OWNER_WORKER_SIDE = (
    "CLASSIFY before you dispatch",
    "the reviewer is always yours, the manager owns no review lane",
    "told READ-ONLY in the brief",
    "a convention, not a mechanism",
    "one reviewer wrote into an author's tree three times",
    "absolute path OUTSIDE your checkout",
)
# Phrasings that hand a PR review back to the manager. A denylist cannot be
# complete — the paragraph-identity and growth pins are what refuse the
# unknown wording — but these are the shapes the retired lane actually used,
# so a copy-paste revival trips here first.
#
# ⚠️ NOT on this list, and measured: "not yours". worker.md uses it legitimately
# for repo scope ("is NOT yours to pick up"), so denying it would fire on
# unrelated text — the false-positive that trains a reader to shrug.
LANE_REINSTALL_ESCAPES = (
    "arm 1",
    "arm-1",
    "isolated verifier",
    "ask_manager` for an isolated verifier",
    "ask the manager for a verifier",
    "hand it over to the manager",
    "the manager spawns a verifier",
    "the manager spawns the verifier",
    "you `spawn_worker` a verifier",
    "behavioral-surface diff is yours",
    "behavioural-surface diff is yours",
    "behavioral surfaces go to the manager",
    "behavioral surfaces are reviewed by the manager",
    "hands it over; you",
)


def test_one_owner_is_stated_positively_on_both_sides():
    manager = _verifier_block(_manager_text())
    for clause in ONE_OWNER_MANAGER_SIDE:
        assert clause in manager, (
            f"manager.md lost {clause!r} — the 2026-08-05 ruling is that every "
            f"Tier-2 diff is the author's and the brief says read-only; the "
            f"reason has to travel with it or the next reader re-splits it"
        )
    worker = compose_generic("worker.md")
    for clause in ONE_OWNER_WORKER_SIDE:
        assert clause in worker, (
            f"worker.md lost {clause!r} — the author is the one that "
            f"classifies AND the one that dispatches, so its copy is the one "
            f"that decides what actually happens"
        )


def test_no_clause_reinstalls_a_manager_review_lane():
    # Both flavors, all three agents: the generic text is what this repo owns,
    # the operator render is what a live session reads, and the retired lane
    # can come back through either.
    for flavor, fn in (("generic", compose_generic), ("operator", compose_operator)):
        for name in RENDERED_AGENTS:
            low = fn(name).lower()
            for escape in LANE_REINSTALL_ESCAPES:
                assert escape not in low, (
                    f"{flavor}/{name} re-installs the retired manager review "
                    f"lane ({escape!r}). The operator retired it on "
                    f"2026-08-05: the author dispatches its own reviewer for "
                    f"EVERY diff and says read-only in the brief"
                )


# C2 (Tier-2 round on PR #256): the extension list is 20 entries and only four
# were pinned (.py .kt .ts .sh). Gutting it to those four was full-suite GREEN,
# and a tree-wide grep found no other test referencing .go/.java/.rs/.swift/
# Dockerfile/Makefile. A later corpus-diet trim would then let a 40-LOC .yml or
# Dockerfile diff classify Tier 1 — no adversarial review at all. Pin all 20.
CODE_EXTENSIONS = (
    ".py", ".sh", ".bash", ".groovy", ".kt", ".java", ".ts", ".tsx", ".js",
    ".jsx", ".go", ".rb", ".rs", ".c", ".cpp", ".swift", ".json", ".yml",
    ".yaml", ".toml",
)
EXTENSIONLESS_INFRA = ("Dockerfile", "Makefile")


def test_every_code_extension_trigger_is_pinned():
    classify = _bullet(_verifier_block(_manager_text()), "- **Classify")
    for ext in CODE_EXTENSIONS:
        assert f"{ext} " in classify or f"{ext}`" in classify, (
            f"the code-extension trigger list lost {ext!r}. A diff in that "
            f"language then falls through the code arm and, under 100 LOC, "
            f"classifies Tier 1 — a PR comment instead of an adversarial "
            f"review. Removing one entry must never be a silent trim"
        )
    for infra in EXTENSIONLESS_INFRA:
        assert infra in classify, (
            f"the extensionless infra trigger lost {infra!r} — it has no "
            f"extension, so nothing else in the classifier catches it"
        )
    # Meta-assertion: the docstring above claims 20 entries, so count them
    # rather than trusting the list here to have stayed in step with the prose.
    assert len(CODE_EXTENSIONS) == 20, (
        "CODE_EXTENSIONS drifted from the 20 entries the classifier ships"
    )


# The behavioural-surface list no longer decides WHO reviews (2026-08-05) —
# it decides the TIER, and that job is load-bearing on its own: `uv.lock`
# (.lock), `publish/**`, `deploy/agents/*.core.md` (markdown) and
# `deploy/tmux/*.conf` carry NONE of the 20 code extensions, so a 40-line diff to any of them classifies Tier 1 without this
# list. `tests/**` is on it because the 2026-08-03 reviewer mutated GUARD
# FILES — the incident sat outside the original list.
BEHAVIORAL_SURFACES = ("deploy/**", "src/dockwright/**", "tests/**", "evals/**",
                       "scripts/**", "publish/**", ".github/**", "setup.sh",
                       "pyproject.toml", "uv.lock")


def test_every_behavioral_surface_is_pinned():
    classify = _bullet(_verifier_block(_manager_text()), "- **Classify")
    worker = compose_generic("worker.md")
    for surface in BEHAVIORAL_SURFACES:
        assert surface in classify, (
            f"manager.md dropped behavioral surface {surface!r} — a diff "
            f"there would classify Tier 1 under 100 LOC, since it carries no "
            f"code extension"
        )
        assert surface in worker, (
            f"worker.md dropped behavioral surface {surface!r} — the worker "
            f"is the one that classifies, so its list is the one that decides"
        )


def test_the_worker_keeps_the_tree_verification_check():
    # The author dispatches its own reviewer, and that reviewer can write.
    # These two clauses are the ONLY mechanical detection of the 2026-08-03
    # incident, and the tree check deleted green before this test existed.
    # After the 2026-08-05 retirement they cover EVERY diff, behavioural
    # surfaces included — there is no preset-spawned verifier behind them.
    worker = compose_generic("worker.md")
    assert "git status --porcelain` is clean" in worker, (
        "worker.md lost the post-round tree check — nothing else would "
        "notice a reviewer that wrote into the checkout"
    )
    assert "never trust" in worker, (
        "the reason must travel with the check: the 2026-08-03 reviewer "
        "claimed read-only and had written three times"
    )
    assert "absolute path OUTSIDE your checkout" in worker, (
        "the sibling convention: the 2026-08-03 mutations landed in the real "
        "tree because a helper script used RELATIVE paths and agent Bash "
        "resets cwd to the checkout"
    )


# N3 (round 2): the write-denial overstatement had a THIRD instance, in the
# `description:` frontmatter a dispatching model actually reads — an absolute
# claim about the very frontmatter this PR measured as NOT binding, refuted by
# the agent's own body thirty lines later. Two guards, because one is not
# enough: identity on the line itself, and a tripwire on the CLAIM SHAPE
# across the whole rendered corpus.
#
# The tripwire is a denylist and cannot be complete — that is why the identity
# pin carries the weight. It exists to catch the phrasings this corpus has
# actually produced three times.
REVIEWER_DESCRIPTION = (
    "description: Adversarial code reviewer for the Tier-2 review of a PR "
    "whose author is the dispatching session. Reads the diff, runs tests, "
    "returns a verdict. Its `tools:` withholds Edit/Write/NotebookEdit, but "
    "that was measured NOT to bind (see body)."
)
ABSOLUTE_WRITE_DENIAL_CLAIMS = (
    "cannot edit, write",
    "cannot edit or write",
    "it cannot write",
    "read-only by construction",
    "write-denied adversarial",
    "the only isolation here that is actually enforced",
    '"fix it myself" is impossible',
    "denies writes as a mechanism",
)


def test_the_reviewer_description_makes_no_absolute_claim():
    line = next(l for l in compose_generic("dockwright-reviewer.md").splitlines()
                if l.startswith("description:"))
    assert line == REVIEWER_DESCRIPTION, (
        "the reviewer's `description:` changed. A dispatching model reads this "
        "line and little else, so an absolute write-denial claim here is the "
        "worst-placed one in the corpus — and it was measured false.\n"
        f"pinned : {REVIEWER_DESCRIPTION}\nfound  : {line}"
    )


def test_the_reviewer_tools_line_matches_what_the_description_claims():
    # The description above PROMISES `tools:` withholds Edit/Write/
    # NotebookEdit, and until 2026-08-05 nothing checked the `tools:` line
    # itself: appending `, Edit, Write, NotebookEdit` to it was full-suite
    # GREEN. A pinned promise over an unpinned fact is worse than either alone,
    # because the dispatching model reads the promise. So derive the claim from
    # the thing it describes rather than restating it.
    tools_line = next(l for l in compose_generic("dockwright-reviewer.md").splitlines()
                      if l.startswith("tools:"))
    granted = {t.strip() for t in tools_line.split(":", 1)[1].split(",")}
    withheld = {"Edit", "Write", "NotebookEdit"}
    assert not (granted & withheld), (
        f"`tools:` grants {sorted(granted & withheld)} while the pinned "
        f"`description:` tells every dispatching model it withholds them. "
        f"The frontmatter was measured NOT to bind, so this is not a "
        f"safety mechanism either way — but a description that lies about "
        f"its own file is the one claim a reader has no way to check.\n"
        f"found: {tools_line}"
    )
    assert granted, "the `tools:` line parsed to nothing — check its format"


def test_no_absolute_write_denial_claim_in_the_rendered_corpus():
    for name in RENDERED_AGENTS:
        low = compose_generic(name).lower()
        for claim in ABSOLUTE_WRITE_DENIAL_CLAIMS:
            assert claim not in low, (
                f"{name} asserts an absolute write-denial ({claim!r}). "
                f"Measured 2026-08-01/03: the `tools:` frontmatter does not "
                f"bind, and the settings preset's deny list is tool-scoped, so "
                f"a Bash `python3` writes through it. State the limit, never "
                f"the absolute"
            )
