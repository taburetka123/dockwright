"""Docs-consistency guards for the manager boot loader (all four boot paths).

The /manager, /manager-reboot, /manager-resume, /manager-takeover-recovery
commands are doc-surfaces the model executes as prose. Two silent-truncation
bugs lived here: (A1) the agent file ~/.claude/agents/manager.md is over the
single-Read token cap, so a one-shot Read drops its later half; (A2) the
memory/notebook loader cat'd files inline, overflowing the Bash output cap so
only a ~2KB preview reached context (the "notebook empty" false report).

The zero-touch-headless migration (E2E F-2) then moved the expansion-gated boot
bash off the docs entirely: the inline memory-loader one-liner (with its
$-expansions the permission guard can never allowlist) became
`dockwright boot-brief --domain <d>`, which prints AGENT_LINES + MEMORY/NOTEBOOK
pointers only. The agent-file line count now comes from that AGENT_LINES line
instead of an inline `wc -l`. The newest-5 / 7-day / 4KB-notebook-warn selection
moved into the CLI (enforced + covered by tests/test_boot_brief.py), so the docs
no longer inline it. These pins turn those regressions back into failures.

BOOT_FILES are all four boot surfaces (they share the paging + boot-brief loader
mechanics). NOTEBOOK_PROSE_FILES are the three full boot commands that also carry
the inline notebook-counting prose; takeover-recovery's step 8 is terse and
delegates that semantics to /manager-resume, so it is excluded there.
"""
import re
from pathlib import Path

import pytest

COMMANDS = Path(__file__).resolve().parent.parent / "deploy" / "commands"
BOOT_FILES = [
    "manager.md",
    "manager-reboot.md",
    "manager-resume.md",
    "manager-takeover-recovery.md",
]
NOTEBOOK_PROSE_FILES = ["manager.md", "manager-reboot.md", "manager-resume.md"]


@pytest.mark.parametrize("filename", BOOT_FILES)
def test_agent_file_is_paged_to_eof_not_single_read(filename):
    text = (COMMANDS / filename).read_text()
    # Must teach paging, not a single Read of the oversized agent file.
    assert "single-Read cap" in text, (
        f"{filename}: must explain the agent file exceeds the single-Read cap"
    )
    # The line count now comes from boot-brief's AGENT_LINES, not an inline wc -l.
    assert "AGENT_LINES" in text, (
        f"{filename}: the line count N must come from boot-brief's AGENT_LINES line"
    )
    assert "wc -l ~/.claude/agents/manager.md" not in text, (
        f"{filename}: the inline `wc -l` line-count probe must be gone (AGENT_LINES replaces it)"
    )
    assert "offset=201" in text, f"{filename}: must page in explicit windows"
    assert "Do not act on a partial read" in text, (
        f"{filename}: must forbid acting on a truncated read"
    )


@pytest.mark.parametrize("filename", BOOT_FILES)
def test_memory_loader_uses_boot_brief_not_inline_bash(filename):
    text = (COMMANDS / filename).read_text()
    # The expansion-free CLI loader replaced the inline memory-loader bash.
    assert "dockwright boot-brief --domain" in text, (
        f"{filename}: memory/notebook loader must be `dockwright boot-brief --domain`"
    )
    assert "Read each printed" in text or "`Read` each printed" in text, (
        f"{filename}: prose must tell the manager to Read each printed path"
    )
    # The overflow-causing inline-cat forms must never return.
    assert 'cat "$f"' not in text, f"{filename}: inline cat of memory files reintroduced"
    assert 'cat "$NB"' not in text, f"{filename}: inline cat of the notebook reintroduced"
    # The expansion-gated inline-bash mechanics must all be gone.
    assert 'echo "MEMORY $f"' not in text, f"{filename}: inline MEMORY-echo bash reintroduced"
    assert "stat -f %m" not in text, f"{filename}: inline stat mtime probe reintroduced"
    assert "date -r" not in text, f"{filename}: inline date -r mtime probe reintroduced"
    assert "echo $CLAUDE_CODE_SESSION_ID" not in text, (
        f"{filename}: expansion-gated `echo $CLAUDE_CODE_SESSION_ID` reintroduced"
    )


@pytest.mark.parametrize("filename", BOOT_FILES)
def test_loader_delegates_selection_caps_to_boot_brief_cli(filename):
    # newest-5 / 7-day-window / 4KB-notebook-warn selection moved into the
    # boot-brief CLI (enforced + covered by tests/test_boot_brief.py). The docs
    # must no longer inline those mechanics; they delegate to the CLI.
    text = (COMMANDS / filename).read_text()
    assert "dockwright boot-brief --domain" in text, (
        f"{filename}: selection must delegate to boot-brief"
    )
    assert "head -5" not in text, f"{filename}: inline newest-5 bash must be gone"
    assert "-le 7" not in text, f"{filename}: inline 7-day-window bash must be gone"


@pytest.mark.parametrize("filename", NOTEBOOK_PROSE_FILES)
def test_loader_keeps_notebook_counting_prose(filename):
    text = (COMMANDS / filename).read_text()
    assert "## [ ]" in text, f"{filename}: notebook open-entry counting dropped"
    assert "review-by" in text, f"{filename}: review-by triage dropped"


# --- Design-gate park: docs that NAME the silent-finish signal ---
#
# Since 2026-08-11 a worker can be legitimately parked at a plannotator design
# gate, blocked on the engineer. It runs `--gate` backgrounded, so it goes
# `state=idle` with no pending question and pages FINISHED_SILENTLY exactly like
# a worker that finished and forgot `worker_done`. Closing it kills the
# backgrounded plannotator and the engineer's verdict with it.
#
# ⛔ READ THE LIMIT BELOW BEFORE TRUSTING THIS. Four versions of a stronger
# guard were built here on 2026-08-11 and every one failed open on ADD-ONE,
# each caught by an adversarial reviewer rather than by its author:
#   v1 required the substring "Design-gate relays" — also the SECTION HEADING in
#      manager.core.md, so the file that owns the caveat could not fail, and the
#      delete-one proof removed a string the heading guarantees;
#   v2 selected docs by `get_worker_summary`, missing any close instruction that
#      never names that tool;
#   v3 selected on BLOCKS and then asserted on the whole FILE — it computed the
#      hot blocks and discarded them;
#   v4 split selection and assertion across two units that fail open TOGETHER.
# One mistake in four costumes: approximating a BEHAVIOURAL invariant with a
# TEXTUAL one. The invariant wanted is "a manager reading an instruction that
# can close a worker has, at that moment, been told a park looks like this" —
# and text matching cannot express "at that moment", only whether two strings
# co-occur in some window. Every window was wrong in one direction.
#
# ⛔ KNOWN LIMIT, stated so nobody reads this as coverage it does not have:
# **a SECOND close instruction added to a doc that already carries the caveat is
# NOT caught.** Measured — one sibling bullet "when that tail is empty and the
# worker does not answer a nudge, close it" appended to an existing list in
# manager.core.md leaves this module fully green. That is the most ordinary edit
# possible to that file. The behavioural guard belongs in the close path itself
# (`_close_window` / autoclose refusing to kill a pane with a live backgrounded
# `plannotator --gate`) — see the follow-up issue referenced from PR #265.
#
# ⛔ SECOND KNOWN LIMIT, named rather than left implicit in a positive clause:
# selection is by the signal literal, so a doc that instructs a close WITHOUT
# naming FINISHED_SILENTLY is never selected at all. Measured — 22 passed. Both
# limits have the same root and the same fix, #266.
#
# What this DOES cover, and it is worth having: any doc that names the signal at
# all must carry the caveat, whatever verb it uses; a NEW such doc must declare
# itself in the pinned set; and a doc reworded out of the set fails as loudly as
# one added in.
SIGNAL = "FINISHED_SILENTLY"
# ⛔ The EXACT caveat phrase, nothing looser. An earlier version also accepted a
# bare `plannotator`, matched against the whole file — and `plannotator` appears
# in manager.core.md seven times for unrelated reasons, so neutering the entire
# § Design-gate relays explanation AND the :292 caveat still left this module at
# 22 passed. That is v1's failure shape one level down: v1 could not fail because
# a HEADING guaranteed its string; that version could not fail because an
# UNRELATED SECTION did. All five pinned docs carry this exact phrase today.
MARKER = re.compile(r"design-gate park", re.I)
DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
MONITOR_SRC = (Path(__file__).resolve().parent.parent / "src" / "dockwright"
               / "monitor.py")
# Pinned as a SET: a new doc naming the signal must declare itself here, and one
# reworded OUT must fail rather than quietly drop its caveat requirement.
EXPECTED_SIGNAL_DOCS = {
    "deploy/agents/manager.core.md",
    "deploy/commands/manager-resume.md",
    "deploy/commands/manager.md",
    "deploy/skills/dockwright-orchestrator-guide/SKILL.md",
    "deploy/skills/dockwright-recap/SKILL.md",
}


def _docs_naming_the_signal() -> set[str]:
    """Selection is the SIGNAL alone — no verb matching anywhere.

    A verb list is a classifier over spellings and fails open on the first
    wording nobody showed it ("retire the worker", "wind it down"). Prose verbs
    cannot be derived from code the way SIGNAL is derived from monitor.py, so
    the verb dimension is dropped rather than approximated. Over-inclusion costs
    a caveat in a doc that arguably did not need one; under-inclusion is
    invisible and ships."""
    root = Path(__file__).resolve().parent.parent
    return {str(p.relative_to(root)) for p in sorted(DEPLOY.rglob("*.md"))
            if SIGNAL in p.read_text()}


def test_the_signal_literal_still_exists_in_the_monitor():
    """The selector is derived from the code, not from a spelling I chose. If
    the emitted line is renamed, this fails LOUD instead of quietly selecting no
    documents at all."""
    assert SIGNAL in MONITOR_SRC.read_text(), (
        f"{SIGNAL} no longer appears in monitor.py — the guard below selects "
        f"documents by that literal and would silently match nothing. Update "
        f"both together.")


def test_the_signal_doc_set_is_exactly_the_pinned_one():
    """`==`, not `>=`. Both directions matter: a NEW doc naming the signal must
    declare itself, and one reworded OUT must fail rather than silently losing
    its caveat requirement."""
    assert _docs_naming_the_signal() == EXPECTED_SIGNAL_DOCS, (
        f"docs naming {SIGNAL} changed: "
        f"{sorted(_docs_naming_the_signal() ^ EXPECTED_SIGNAL_DOCS)}. A new one "
        f"needs the design-gate caveat and an entry here; one that dropped out "
        f"was probably reworded — check it still carries the caveat first.")


@pytest.mark.parametrize("relpath", sorted(EXPECTED_SIGNAL_DOCS))
def test_docs_naming_the_signal_carry_the_park_caveat(relpath):
    """Doc-level, and doc-level ONLY — see the KNOWN LIMIT above. This asserts
    that a document naming the signal mentions the park SOMEWHERE in it. It does
    NOT assert that every instruction inside that document is next to the
    caveat, and four attempts to make it do so all failed open."""
    root = Path(__file__).resolve().parent.parent
    assert MARKER.search((root / relpath).read_text()), (
        f"{relpath} names {SIGNAL} but never mentions a design-gate park or "
        f"plannotator anywhere. A parked worker pages identically to a finished "
        f"one; a doc that discusses that page without the caveat teaches the "
        f"manager to kill the engineer's live gate.")
