import importlib.util
import json
import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PREFLIGHT_PATH = REPO_ROOT / "deploy" / "scripts" / "preflight_cleanup.py"


def test_preflight_fresh_load_lands_in_env_state_dir():
    spec = importlib.util.spec_from_file_location("preflight_fresh_meta", PREFLIGHT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    env_root = Path(os.environ["DOCKWRIGHT_STATE_DIR"])
    assert mod.ROOT == env_root
    assert mod.SPEND_LEDGER == env_root / "spend-ledger.jsonl"
    mod._append_spend_drop(
        {"claude_sid": "meta-preflight", "spend": {"turns": 1, "out_tokens": 2}},
        "preflight_prune")
    rows = [json.loads(l) for l in mod.SPEND_LEDGER.read_text().splitlines()]
    assert [r["sid"] for r in rows] == ["meta-preflight"]


def test_preflight_append_creates_missing_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKWRIGHT_STATE_DIR", str(tmp_path / "never-created"))
    spec = importlib.util.spec_from_file_location("preflight_mkdir_meta", PREFLIGHT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._append_spend_drop(
        {"claude_sid": "meta-mkdir", "spend": {"turns": 1, "out_tokens": 3}},
        "preflight_prune")
    rows = [json.loads(l) for l in mod.SPEND_LEDGER.read_text().splitlines()]
    assert [r["sid"] for r in rows] == ["meta-mkdir"]
