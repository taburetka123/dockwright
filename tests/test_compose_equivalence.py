"""Step-2's identity gate, retired into a canon-shape guard.

Both canon agents are now carved (Step 3): manager.core.md (Task 2) and
worker.core.md (Task 3) composed with the operator overlay drop-ins +
agent_vars into the byte-pinned pre-carve originals modulo the enumerated
intended changes — that transition-era byte pin (tests/test_controlled_diff.py)
retired at Step 6 once the carve/rename transition was verified stable
across 5 post-merge sittings. Its forward successor is the lighter
tests/test_operator_compose.py smoke (compose still succeeds, no stray
syntax/warnings — not a content pin), alongside tests/test_core_genericness.py
for the defaults-composed OSS flavor. Compose's byte-identity property on
marker-less/var-less text lives in tests/test_compose.py.

What remains here is the canon-shape pin: exactly the two .core.md sources
exist. A stray plain X.md beside X.core.md would be an ambiguous-compose
ComposeError at deploy time — catch it in CI first — and a NEW un-carved
agent file showing up must be added to this pin.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO_ROOT / "deploy" / "agents"


def test_canon_agents_exist():
    assert sorted(p.name for p in AGENTS_DIR.glob("*.md")) == [
        "manager.core.md", "worker.core.md"]
