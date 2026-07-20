"""trust.pretrust_dir: official project pre-trust in a Claude config JSON.

Evidence (VM E2E 2026-07-16 B2, both runs): flags WRITTEN to ~/.claude.json
persist across concurrent sessions and exits and suppress the trust dialog;
interactive accepts do not reliably persist (finding L-11)."""
import json
from pathlib import Path

from dockwright import trust


def _key(p) -> str:
    return str(Path(p).resolve())


def test_creates_missing_file(tmp_path):
    cfg = tmp_path / "claude.json"
    proj = tmp_path / "proj"
    assert trust.pretrust_dir(proj, config_json=cfg) is True
    data = json.loads(cfg.read_text())
    assert data["projects"][_key(proj)]["hasTrustDialogAccepted"] is True


def test_merges_preserving_existing_content(tmp_path):
    cfg = tmp_path / "claude.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"dockwright": {"command": "x"}},
        "projects": {"/other": {"hasTrustDialogAccepted": True,
                                "allowedTools": ["Bash"]}},
    }))
    proj = tmp_path / "proj"
    assert trust.pretrust_dir(proj, config_json=cfg) is True
    data = json.loads(cfg.read_text())
    assert data["mcpServers"] == {"dockwright": {"command": "x"}}
    assert data["projects"]["/other"]["allowedTools"] == ["Bash"]
    assert data["projects"][_key(proj)]["hasTrustDialogAccepted"] is True


def test_existing_entry_gains_flag_without_losing_keys(tmp_path):
    cfg = tmp_path / "claude.json"
    proj = tmp_path / "proj"
    cfg.write_text(json.dumps({
        "projects": {_key(proj): {"allowedTools": ["Read"]}}}))
    assert trust.pretrust_dir(proj, config_json=cfg) is True
    entry = json.loads(cfg.read_text())["projects"][_key(proj)]
    assert entry["allowedTools"] == ["Read"]
    assert entry["hasTrustDialogAccepted"] is True


def test_already_trusted_is_zero_mutation(tmp_path):
    cfg = tmp_path / "claude.json"
    proj = tmp_path / "proj"
    cfg.write_text(json.dumps({
        "projects": {_key(proj): {"hasTrustDialogAccepted": True}}}))
    ino = cfg.stat().st_ino
    assert trust.pretrust_dir(proj, config_json=cfg) is True
    assert cfg.stat().st_ino == ino, "already-trusted path must not rewrite the file"


def test_corrupt_file_left_untouched(tmp_path):
    cfg = tmp_path / "claude.json"
    cfg.write_text("{definitely not json")
    assert trust.pretrust_dir(tmp_path / "proj", config_json=cfg) is False
    assert cfg.read_text() == "{definitely not json"


def test_non_dict_projects_bails(tmp_path):
    cfg = tmp_path / "claude.json"
    cfg.write_text(json.dumps({"projects": ["weird"]}))
    assert trust.pretrust_dir(tmp_path / "proj", config_json=cfg) is False
    assert json.loads(cfg.read_text())["projects"] == ["weird"]


def test_empty_file_treated_as_corrupt_never_clobbered(tmp_path):
    cfg = tmp_path / "claude.json"
    cfg.write_text("")
    assert trust.pretrust_dir(tmp_path / "proj", config_json=cfg) is False
    assert cfg.read_text() == ""
