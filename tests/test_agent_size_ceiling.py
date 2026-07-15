"""manager core size ceiling (orch-overspend #3: +49% in 19 days, no gate).

Growing past the ceiling requires a DELIBERATE bump of these constants in the
same PR, with the growth justified — never an incidental drift. Post-carve the
gate rides the CORE file (manager.core.md): the operator overlay adds back only
what the original already carried (the lossless pin holds the total fixed),
so core growth is the drift signal worth gating.
"""
from pathlib import Path

MANAGER = Path(__file__).resolve().parents[1] / "deploy" / "agents" / "manager.core.md"
LINE_CEILING = 700
BYTE_CEILING = 95_000


def test_manager_md_line_ceiling():
    lines = len(MANAGER.read_text().splitlines())
    assert lines <= LINE_CEILING, (
        f"manager.core.md is {lines} lines (> {LINE_CEILING}). Growth must be a "
        f"deliberate ceiling bump in the same PR — see orch-overspend #3.")


def test_manager_md_byte_ceiling():
    size = MANAGER.stat().st_size
    assert size <= BYTE_CEILING, (
        f"manager.core.md is {size} bytes (> {BYTE_CEILING}). Growth must be a "
        f"deliberate ceiling bump in the same PR — see orch-overspend #3.")
