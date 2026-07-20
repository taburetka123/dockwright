"""The statusline usage-tap must write a usage record for ANY safe account
name — headroom weighting and the pause threshold are silently dead for an
account whose name never lands in usage/<name>.json (F4)."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "statusline-command.sh"
pytestmark = pytest.mark.skipif(shutil.which("jq") is None,
                                reason="statusline-command.sh requires jq")


def _run(tmp_path, acct):
    env = {**os.environ, "HOME": str(tmp_path), "CLAUDE_ORCH_ACCOUNT": acct}
    env.pop("CLAUDE_CONFIG_DIR", None)
    payload = json.dumps({
        "model": {"display_name": "opus"},
        "workspace": {"current_dir": str(tmp_path)},
        "rate_limits": {
            "five_hour": {"used_percentage": 42, "resets_at": 1000},
            "seven_day": {"used_percentage": 7, "resets_at": 2000}}})
    return subprocess.run(["bash", str(SCRIPT)], input=payload,
                          capture_output=True, text=True, env=env)


def test_custom_account_name_writes_usage(tmp_path):
    r = _run(tmp_path, "main")
    assert r.returncode == 0
    rec = json.loads(
        (tmp_path / ".claude" / "dockwright" / "usage" / "main.json").read_text())
    assert rec["five_hour_pct"] == 42


def test_ab_names_still_write(tmp_path):
    _run(tmp_path, "b")
    assert (tmp_path / ".claude" / "dockwright" / "usage" / "b.json").exists()


def test_unsafe_names_never_write(tmp_path):
    for bad in ("../evil", ".hidden", "x/y"):
        r = _run(tmp_path, bad)
        assert r.returncode == 0, r.stderr
    root = tmp_path / ".claude" / "dockwright" / "usage"
    assert not root.exists() or list(root.iterdir()) == []
    assert not (tmp_path / ".claude" / "dockwright" / "evil.json").exists()
