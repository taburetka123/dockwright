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
