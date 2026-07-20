"""Deploy-time injection of permissions.additionalDirectories into the
DEPLOYED worker-headless preset (E2E rc.2 N-3).

Why deploy-time and absolute: the preset's permission mode is documented to
auto-accept edits in additionalDirectories, but tilde expansion in the settings
VALUE is undocumented — so the shipped fixture stays generic and setup.sh
resolves the operator's [paths] code roots to absolute paths at deploy. An
operator-set key (even []) is intent and must survive verbatim.
"""
import json
import os
from pathlib import Path

import pytest

from dockwright import config, presets

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "deploy" / "presets" / "worker-headless-settings.json"


@pytest.fixture
def no_config(monkeypatch, tmp_path):
    monkeypatch.delenv(config.ENV_CONFIG_PATH, raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def deployed(tmp_path):
    out = tmp_path / "deployed" / "worker-headless-settings.json"
    out.parent.mkdir(parents=True)
    out.write_text(FIXTURE.read_text())
    return out


def test_default_roots_injected_absolute_and_deduped(no_config, deployed):
    home = no_config
    assert presets.finalize_headless_settings(deployed) is True
    data = json.loads(deployed.read_text())
    dirs = data["permissions"]["additionalDirectories"]
    assert dirs == sorted([
        str(home / "projects" / "personal"),
        str(home / "projects" / "work"),
        str(home / "worktrees"),
        str(home / "worktrees-personal"),
    ])
    # worker_home (~/projects/work/worker) is nested under a repo root → deduped.
    assert not any(d.endswith("/worker") for d in dirs)
    assert not any("~" in d for d in dirs)
    # All other keys survive value-identically.
    original = json.loads(FIXTURE.read_text())
    data["permissions"].pop("additionalDirectories")
    assert data == original


def test_second_run_is_a_noop(no_config, deployed):
    presets.finalize_headless_settings(deployed)
    before = deployed.read_text()
    assert presets.finalize_headless_settings(deployed) is False
    assert deployed.read_text() == before


def test_operator_explicit_key_respected_even_empty(no_config, deployed):
    data = json.loads(deployed.read_text())
    data["permissions"]["additionalDirectories"] = []
    deployed.write_text(json.dumps(data, indent=2) + "\n")
    before = deployed.read_text()
    assert presets.finalize_headless_settings(deployed) is False
    assert deployed.read_text() == before


def test_operator_paths_override(no_config, deployed, monkeypatch, tmp_path):
    toml = tmp_path / "dockwright.toml"
    toml.write_text(
        '[paths]\nrepo_roots = "/srv/code"\nworktree_roots = ""\n'
        'worker_home = "/srv/code/worker"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(toml))
    presets.finalize_headless_settings(deployed)
    dirs = json.loads(deployed.read_text())["permissions"]["additionalDirectories"]
    assert dirs == ["/srv/code"]


def test_dedupe_does_not_swallow_sibling_prefix():
    assert presets._dedupe_nested(["/a/b", "/a/bc", "/a/b/c"]) == ["/a/b", "/a/bc"]


def test_cli_injects_and_reports(no_config, deployed, capsys):
    assert presets.main(["--file", str(deployed)]) == 0
    assert "additionalDirectories" in json.loads(deployed.read_text())["permissions"]


def test_cli_missing_file_fails(no_config, tmp_path, capsys):
    assert presets.main(["--file", str(tmp_path / "nope.json")]) == 1


def test_repo_fixture_stays_generic():
    # The fixture ships without the key — injection is deploy-time only.
    data = json.loads(FIXTURE.read_text())
    assert "additionalDirectories" not in data.get("permissions", {})
