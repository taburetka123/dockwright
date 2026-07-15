"""Shape guards for the worker-spawn settings presets (deploy/presets/*.json).

The verifier preset (frontier proposal 20260612T110210Z-70817-3) makes the
fleet's most safety-critical role — the verifier worker that reviews edits to
rules/agents/MCP code — read-only by construction instead of by prompt. Two
non-obvious constraints this file pins:

- **Repeated `--settings` flags are last-wins, not merged** (verified
  empirically on claude 2.1.175: `--settings '{bad json' --settings '{}'`
  succeeds, `--settings '{}' --settings '{bad json'` fails). spawn_worker
  prepends its remote-control-off settings as a `--settings` flag
  (mcp_server.py), so a caller preset REPLACES that flag slot — the preset
  must therefore re-assert the remote-control-off keys itself.
- **Permission deny rules are not the only guard.** The PreToolUse hook denies
  file-mutation tools decisively regardless of allow rules (same rationale as
  gardener-write-guard.py), so Write/Edit/NotebookEdit stay denied even on a
  `--dangerously-skip-permissions` spawn. Bash mutations are covered only by
  the deny rules, which assume a normal permission mode — do not spawn
  verifiers with bypass.
"""
import json
import subprocess
from pathlib import Path

from tests.carve_helpers import compose_operator, requires_operator_overlay

REPO_ROOT = Path(__file__).resolve().parent.parent
PRESETS = REPO_ROOT / "deploy" / "presets"
VERIFIER_PRESET = PRESETS / "verifier-settings.json"

DEPLOYED_VERIFIER_PATHS = tuple(
    str(Path.home() / p / "presets" / "verifier-settings.json")
    for p in (".claude/dockwright", ".claude/orchestrator")
)


def test_all_preset_json_files_parse():
    json_presets = sorted(PRESETS.glob("*.json"))
    assert len(json_presets) >= 2, "expected at least gardener + verifier settings presets"
    for preset in json_presets:
        json.loads(preset.read_text())


def test_verifier_preset_denies_file_mutation_tools():
    deny = json.loads(VERIFIER_PRESET.read_text())["permissions"]["deny"]
    for tool in ("Write", "Edit", "NotebookEdit"):
        assert tool in deny, f"verifier preset must deny {tool} outright"


def test_verifier_preset_denies_mutating_git_gh_bash():
    deny = json.loads(VERIFIER_PRESET.read_text())["permissions"]["deny"]
    for rule in (
        "Bash(git commit:*)",
        "Bash(git push:*)",
        "Bash(git rebase:*)",
        "Bash(git reset:*)",
        "Bash(git checkout:*)",
        "Bash(gh pr merge:*)",
        "Bash(gh pr edit:*)",
        "Bash(rm:*)",
    ):
        assert rule in deny, f"verifier preset must deny {rule}"


def test_verifier_preset_reasserts_remote_control_off():
    # spawn_worker's remote-control-off `--settings` flag is REPLACED (last-wins)
    # by the caller's preset flag, so the preset carries the keys itself.
    settings = json.loads(VERIFIER_PRESET.read_text())
    assert settings.get("remoteControlAtStartup") is False
    assert settings.get("disableRemoteControl") is True


def test_verifier_preset_pretooluse_guard_emits_deny_json():
    settings = json.loads(VERIFIER_PRESET.read_text())
    matchers = settings["hooks"]["PreToolUse"]
    assert len(matchers) == 1
    matcher = matchers[0]["matcher"]
    for tool in ("Write", "Edit", "NotebookEdit"):
        assert tool in matcher, f"PreToolUse guard must match {tool}"
    command = matchers[0]["hooks"][0]["command"]
    # The guard is an inline constant — run it and assert the decision payload
    # claude expects (same shape gardener-write-guard.py emits).
    result = subprocess.run(
        ["sh", "-c", command], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"]


@requires_operator_overlay
def test_manager_agent_wires_verifier_preset_on_verifier_spawns():
    # Post-carve the absolute path is the {{verifier_settings_path}} var; the
    # deployed operator flavor is what must carry it, so pin the composed
    # operator text (core + operator overlay + agent_vars).
    text = compose_operator("manager.md")
    # The wiring must use the deployed ABSOLUTE path: setup.sh rsyncs presets to
    # ~/.claude/dockwright/presets/ (legacy operators may still pin
    # ~/.claude/orchestrator/presets/ in their dockwright.toml agent_vars until
    # they update — accept either), and neither the spawn shell (shlex-quoted
    # args) nor claude's --settings expands `~`.
    assert any(p in text for p in DEPLOYED_VERIFIER_PATHS)
    assert "read-only by construction" in text
    # Guard against the tempting-but-broken tilde form reappearing.
    assert "~/.claude/dockwright/presets/verifier-settings.json" not in text
    assert "~/.claude/orchestrator/presets/verifier-settings.json" not in text
    assert "~/.claude/presets/verifier-settings.json" not in text


HEADLESS_PRESET = PRESETS / "worker-headless-settings.json"


def test_headless_preset_reasserts_spawner_settings_keys():
    # A caller --settings REPLACES spawn_worker's own settings flag (last-wins),
    # so the preset must re-assert all three keys itself; dropping
    # enableAllProjectMcpServers would reintroduce the pre-registration
    # "N new MCP servers found" startup stall the preset exists to kill.
    settings = json.loads(HEADLESS_PRESET.read_text())
    assert settings.get("enableAllProjectMcpServers") is True
    assert settings.get("remoteControlAtStartup") is False
    assert settings.get("disableRemoteControl") is True


def test_headless_preset_allows_worker_protocol_tools():
    perms = json.loads(HEADLESS_PRESET.read_text())["permissions"]
    assert perms.get("defaultMode") == "acceptEdits"
    allow = perms["allow"]
    for rule in (
        "mcp__dockwright__worker_done",
        "mcp__dockwright__ask_manager",
        "mcp__dockwright__artifact_put",
        "Bash(printenv:*)",
    ):
        assert rule in allow, f"headless preset must allow {rule}"


def test_verifier_preset_allows_worker_protocol_tools():
    settings = json.loads(VERIFIER_PRESET.read_text())
    allow = settings["permissions"]["allow"]
    for rule in ("mcp__dockwright__worker_done", "mcp__dockwright__ask_manager",
                 "Bash(printenv:*)"):
        assert rule in allow, f"verifier preset must allow {rule}"
    assert settings.get("enableAllProjectMcpServers") is True
    # The read-only construction must survive the additions.
    deny = settings["permissions"]["deny"]
    for tool in ("Write", "Edit", "NotebookEdit"):
        assert tool in deny


def test_headless_preset_path_var_defined_and_wired():
    vars_toml = (REPO_ROOT / "deploy" / "agents" / "vars.defaults.toml").read_text()
    assert ("worker_headless_settings_path = "
            "'<absolute-home>/.claude/dockwright/presets/worker-headless-settings.json'") in vars_toml
    core = (REPO_ROOT / "deploy" / "agents" / "manager.core.md").read_text()
    assert "{{worker_headless_settings_path}}" in core
