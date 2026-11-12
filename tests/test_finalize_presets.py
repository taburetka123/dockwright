import json
import os
from pathlib import Path

import pytest

from dockwright import config, paths, presets

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "deploy" / "presets" / "worker-headless-settings.json"
REPLY_FIXTURE = REPO / "deploy" / "presets" / "reply-settings.json"


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
    assert not any(d.endswith("/worker") for d in dirs)
    assert not any("~" in d for d in dirs)
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
    data = json.loads(FIXTURE.read_text())
    assert "additionalDirectories" not in data.get("permissions", {})


def _dirs(path):
    return json.loads(path.read_text())["permissions"]["additionalDirectories"]


def test_the_export_folder_follows_the_code_roots(no_config, deployed):
    assert presets.main(["--file", str(deployed)]) == 0
    dirs = _dirs(deployed)
    assert dirs[-1] == str(paths.EXPORTS)
    assert len(dirs) > 1


def test_the_export_folder_joins_an_operator_list(no_config, deployed):
    data = json.loads(deployed.read_text())
    data["permissions"]["additionalDirectories"] = ["/operator/root"]
    deployed.write_text(json.dumps(data))
    presets.main(["--file", str(deployed)])
    assert _dirs(deployed) == ["/operator/root", str(paths.EXPORTS)]


def test_exports_only_adds_the_folder_and_no_code_root(no_config, tmp_path):
    preset = tmp_path / "manager-settings.json"
    preset.write_text(json.dumps({"permissions": {"allow": ["Read"]}}))
    assert presets.main(["--exports-only", "--file", str(preset)]) == 0
    assert _dirs(preset) == [str(paths.EXPORTS)]


def test_a_second_run_lists_the_folder_once(no_config, deployed):
    presets.main(["--file", str(deployed)])
    presets.main(["--file", str(deployed)])
    assert _dirs(deployed).count(str(paths.EXPORTS)) == 1


def _reply_preset(tmp_path):
    out = tmp_path / "deployed" / "reply-settings.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(REPLY_FIXTURE.read_text())
    return out


def _perms(path):
    return json.loads(path.read_text())["permissions"]


def test_the_reply_preset_grants_edits_only_in_the_topic_folder(no_config, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    preset = _reply_preset(tmp_path)
    assert presets.main(["--topic-folder", "--file", str(preset)]) == 0
    perms = _perms(preset)
    assert f"Edit(/{paths.EXPORTS / 'topic'}/**)" in perms["allow"]
    assert not any(rule.startswith(("Write", "Edit")) and rule != f"Edit(/{paths.EXPORTS / 'topic'}/**)"
                   for rule in perms["allow"])
    assert "Edit(~/**)" in perms["deny"]


def test_an_export_folder_under_home_keeps_the_topic_folder_writable(no_config, tmp_path):
    preset = _reply_preset(tmp_path)
    presets.main(["--topic-folder", "--file", str(preset)])
    perms = _perms(preset)
    assert f"Edit(/{paths.EXPORTS / 'topic'}/**)" in perms["allow"]
    assert "Edit(~/**)" not in perms["deny"]


def test_a_second_topic_folder_run_lists_each_rule_once(no_config, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    preset = _reply_preset(tmp_path)
    presets.main(["--topic-folder", "--file", str(preset)])
    presets.main(["--topic-folder", "--file", str(preset)])
    perms = _perms(preset)
    assert perms["allow"].count(f"Edit(/{paths.EXPORTS / 'topic'}/**)") == 1
    assert perms["deny"].count("Edit(~/**)") == 1


def _derive_fixture(root):
    base_dir = root / "presets"
    base_dir.mkdir()
    base = base_dir / "base.json"
    base.write_text(json.dumps({"permissions": {"defaultMode": "auto", "allow": ["A"], "additionalDirectories": ["/r"]},
                                "enableAllProjectMcpServers": True}))
    (root / "src").mkdir()
    child = root / "src" / "child.json"
    child.write_text(json.dumps({"extends": "base.json", "env": {"K": "0"}, "permissions": {"allow": ["B"]}}))
    return base, child


def test_derive_merges_the_base_under_the_source(no_config, tmp_path):
    base, child = _derive_fixture(tmp_path)
    source_bytes = child.read_bytes()
    out = tmp_path / "out" / "child.json"
    presets.derive_preset(child, base.parent, out)
    assert json.loads(out.read_text()) == {
        "permissions": {"defaultMode": "auto", "allow": ["A", "B"], "additionalDirectories": ["/r"]},
        "enableAllProjectMcpServers": True, "env": {"K": "0"}}
    assert child.read_bytes() == source_bytes


def test_a_later_derive_follows_the_base(no_config, tmp_path):
    base, child = _derive_fixture(tmp_path)
    out = tmp_path / "out" / "child.json"
    presets.derive_preset(child, base.parent, out)
    data = json.loads(base.read_text())
    data["permissions"]["allow"].append("C")
    base.write_text(json.dumps(data))
    presets.derive_preset(child, base.parent, out)
    assert json.loads(out.read_text())["permissions"]["allow"] == ["A", "C", "B"]


def _no_extends(base, child):
    child.write_text(json.dumps({"env": {"K": "0"}}))


def _missing_base(base, child):
    child.write_text(json.dumps({"extends": "nope.json", "env": {"K": "0"}}))


def _parent_path(base, child):
    child.write_text(json.dumps({"extends": "../base.json", "env": {"K": "0"}}))


def _chained_base(base, child):
    base.write_text(json.dumps({"extends": "other.json", "permissions": {"allow": ["A"]}}))


@pytest.mark.parametrize("spoil", [_no_extends, _missing_base, _parent_path, _chained_base])
def test_derive_refuses_a_bad_source(no_config, tmp_path, capsys, spoil):
    base, child = _derive_fixture(tmp_path)
    spoil(base, child)
    out = tmp_path / "out" / "child.json"
    out.parent.mkdir()
    out.write_text(json.dumps({"old": True}))
    before = out.read_bytes()
    assert presets.main(["--derive", str(child), "--base-dir", str(base.parent), "--out", str(out)]) == 1
    assert "child.json" in capsys.readouterr().err
    assert out.read_bytes() == before


def _exit_code(argv):
    try:
        return presets.main(argv)
    except SystemExit as err:
        return err.code


def test_the_cli_needs_one_of_file_and_derive(no_config, tmp_path, deployed):
    base, child = _derive_fixture(tmp_path)
    out = tmp_path / "out" / "child.json"
    assert _exit_code([]) not in (0, None)
    assert _exit_code(["--file", str(deployed), "--derive", str(child), "--base-dir", str(base.parent),
                       "--out", str(out)]) not in (0, None)
    assert _exit_code(["--derive", str(child)]) not in (0, None)
    assert not out.exists()
