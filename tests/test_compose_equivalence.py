from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO_ROOT / "deploy" / "agents"


def test_canon_agents_exist():
    assert sorted(p.name for p in AGENTS_DIR.glob("*.md")) == [
        "dockwright-reviewer.core.md", "manager.core.md", "worker.core.md"]
