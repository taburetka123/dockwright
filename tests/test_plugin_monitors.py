import json
import shutil
from pathlib import Path

from dockwright import doctor, lane_io
from dockwright.codex_translate import _is_yaml_true, parse_frontmatter

REPO = Path(__file__).resolve().parent.parent
MARKETPLACE_ROOT = REPO / "deploy" / "plugin"
PLUGIN = MARKETPLACE_ROOT / "plugins" / "dockwright"
MARKETPLACE = MARKETPLACE_ROOT / ".claude-plugin" / "marketplace.json"


def _monitors():
    return json.loads((PLUGIN / "monitors" / "monitors.json").read_text())


def _arming_trigger():
    (when,) = {m["when"] for m in _monitors()}
    plugin_name, skill_name = when.removeprefix("on-skill-invoke:").split(":")
    return plugin_name, skill_name


def test_one_monitor_per_lane_plus_the_chat_archive_manager_runner_and_nothing_else():
    monitors = {m["name"]: m for m in _monitors()}
    assert set(monitors) == set(lane_io.LANES) | {"chat-archive"}
    assert monitors["chat-archive"]["command"] == "@@DOCKWRIGHT_BIN@@ chat-archive loop"
    assert monitors["chat-archive"]["description"] == "dockwright chat archive (manager runner)"


def test_every_monitor_is_armed_by_the_arm_lanes_skill():
    assert {m["when"] for m in _monitors()} == {"on-skill-invoke:dockwright:arm-lanes"}


def test_every_lane_monitor_runs_the_lane_loop_through_the_rendered_binary():
    for m in _monitors():
        if m["name"] in lane_io.LANES:
            assert m["command"] == f"@@DOCKWRIGHT_BIN@@ monitor-loop {m['name']}"
            assert m["description"] == f"dockwright {m['name']} lane"


def test_the_arming_trigger_resolves_to_a_model_invocable_skill_of_the_shipped_plugin():
    plugin_name, skill_name = _arming_trigger()
    market = json.loads(MARKETPLACE.read_text())
    (entry,) = [p for p in market["plugins"] if p["name"] == plugin_name]
    plugin_dir = MARKETPLACE_ROOT / entry["source"]
    manifest = json.loads((plugin_dir / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == plugin_name
    meta, _ = parse_frontmatter((plugin_dir / "skills" / skill_name / "SKILL.md").read_text())
    assert meta.get("name") == skill_name
    assert not _is_yaml_true(meta.get("disable-model-invocation", ""))


def test_doctor_accepts_the_rendered_marketplace_registered_under_its_manifest_names(tmp_path):
    plugin_name, _ = _arming_trigger()
    market = json.loads(MARKETPLACE.read_text())
    rendered = tmp_path / "plugin"
    shutil.copytree(MARKETPLACE_ROOT, rendered)
    for monitors in rendered.glob("plugins/*/monitors/monitors.json"):
        monitors.write_text(monitors.read_text().replace("@@DOCKWRIGHT_BIN@@", "/opt/dockwright/bin/dockwright"))
    settings = {
        "enabledPlugins": {f"{plugin_name}@{market['name']}": True},
        "extraKnownMarketplaces": {
            market["name"]: {"source": {"source": "directory", "path": str(rendered)}}},
    }
    assert doctor.check_plugin_registration(settings).ok
