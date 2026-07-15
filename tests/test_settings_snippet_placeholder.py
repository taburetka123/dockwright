import json
from pathlib import Path
from dockwright import env_install as ei

SNIPPET = Path(__file__).resolve().parent.parent / "deploy" / "settings.snippet.json"

def test_snippet_uses_placeholder_not_bare_orchestrator():
    data = json.loads(SNIPPET.read_text())
    cmds = [h["command"] for blocks in data["hooks"].values() for b in blocks for h in b["hooks"]]
    assert cmds, "snippet has hook commands"
    for c in cmds:
        assert ei.orch_subcommand(c) is None, f"bare orchestrator still present: {c}"
    assert any(ei.PLACEHOLDER in c for c in cmds), "no placeholdered orchestrator hook in snippet"

def test_render_then_subcommand_resolves(tmp_path):
    data = json.loads(SNIPPET.read_text())
    rendered = ei.render_snippet(data, "/abs/orchestrator")
    subs = {ei.orch_subcommand(h["command"])
            for blocks in rendered["hooks"].values() for b in blocks for h in b["hooks"]}
    subs.discard(None)
    assert subs == set(ei.ORCH_SUBCOMMANDS)
