"""Docs-consistency guards for the manager boot loader (all four boot paths).

The /manager, /manager-reboot, /manager-resume, /manager-takeover-recovery
commands are doc-surfaces the model executes as prose. Two silent-truncation
bugs lived here: (A1) the agent file ~/.claude/agents/manager.md is over the
single-Read token cap, so a one-shot Read drops its later half; (A2) the
memory/notebook loader cat'd files inline, overflowing the Bash output cap so
only a ~2KB preview reached context (the "notebook empty" false report). These
pins turn regressions into test failures.

BOOT_FILES are all four boot surfaces (they share the paging + path-only loader
mechanics). NOTEBOOK_PROSE_FILES are the three full boot commands that also carry
the inline notebook-counting prose; takeover-recovery's step 8 is terse and
delegates that semantics to /manager-resume step 7, so it is excluded there.
"""
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
    assert "wc -l ~/.claude/agents/manager.md" in text, (
        f"{filename}: must get the line count to page to EOF"
    )
    assert "offset=201" in text, f"{filename}: must page in explicit windows"
    assert "Do not act on a partial read" in text, (
        f"{filename}: must forbid acting on a truncated read"
    )


@pytest.mark.parametrize("filename", BOOT_FILES)
def test_memory_loader_prints_paths_not_inline_cat(filename):
    text = (COMMANDS / filename).read_text()
    # Path-only print form present; manager Reads each (Read paginates).
    assert 'echo "MEMORY $f"' in text, f"{filename}: loader must print MEMORY paths"
    assert 'echo "NOTEBOOK $NB' in text, f"{filename}: loader must print NOTEBOOK path"
    assert "Read each printed" in text or "`Read` each printed" in text, (
        f"{filename}: prose must tell the manager to Read each printed path"
    )
    # The overflow-causing inline-cat forms must be gone.
    assert 'cat "$f"' not in text, f"{filename}: inline cat of memory files reintroduced"
    assert 'cat "$NB"' not in text, f"{filename}: inline cat of the notebook reintroduced"


@pytest.mark.parametrize("filename", BOOT_FILES)
def test_loader_preserves_newest5_within7d_selection(filename):
    text = (COMMANDS / filename).read_text()
    assert "head -5" in text, f"{filename}: newest-5 cap dropped"
    assert "-le 7" in text, f"{filename}: 7-day recency window dropped"
    assert "4096" in text, f"{filename}: notebook >4KB archive warning dropped"


@pytest.mark.parametrize("filename", NOTEBOOK_PROSE_FILES)
def test_loader_keeps_notebook_counting_prose(filename):
    text = (COMMANDS / filename).read_text()
    assert "## [ ]" in text, f"{filename}: notebook open-entry counting dropped"
    assert "review-by" in text, f"{filename}: review-by triage dropped"
