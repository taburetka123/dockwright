import json
import subprocess
from pathlib import Path

from tests.carve_helpers import compose_generic, compose_operator, requires_operator_overlay

REPO_ROOT = Path(__file__).resolve().parent.parent
PRESETS = REPO_ROOT / "deploy" / "presets"
VERIFIER_PRESET = PRESETS / "verifier-settings.json"

DEPLOYED_VERIFIER_PATH = str(
    Path.home() / ".claude/dockwright/presets/verifier-settings.json")
LEGACY_VERIFIER_PATH = str(
    Path.home() / ".claude/orchestrator/presets/verifier-settings.json")


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
    text = compose_operator("manager.md")
    assert DEPLOYED_VERIFIER_PATH in text
    assert LEGACY_VERIFIER_PATH not in text
    assert "read-only by settings" in text
    assert "~/.claude/dockwright/presets/verifier-settings.json" not in text
    assert "~/.claude/orchestrator/presets/verifier-settings.json" not in text
    assert "~/.claude/presets/verifier-settings.json" not in text


HEADLESS_PRESET = PRESETS / "worker-headless-settings.json"


def test_headless_preset_reasserts_spawner_settings_keys():
    settings = json.loads(HEADLESS_PRESET.read_text())
    assert settings.get("enableAllProjectMcpServers") is True
    assert settings.get("remoteControlAtStartup") is False
    assert settings.get("disableRemoteControl") is True


def test_headless_preset_allows_worker_protocol_tools():
    perms = json.loads(HEADLESS_PRESET.read_text())["permissions"]
    assert perms.get("defaultMode") == "auto"
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
    deny = settings["permissions"]["deny"]
    for tool in ("Write", "Edit", "NotebookEdit"):
        assert tool in deny


def test_composed_generic_resolves_preset_paths_no_overlay():
    text = compose_generic("manager.md")
    home = str(Path.home())
    assert "<absolute-home>" not in text
    assert f"{home}/.claude/dockwright/presets/verifier-settings.json" in text
    assert f"{home}/.claude/dockwright/presets/worker-headless-settings.json" in text


GIT_WILDCARD_VERBS = ("status", "diff", "log", "show", "add", "commit", "init",
                      "checkout", "switch", "restore", "rev-parse",
                      "fetch", "pull", "merge", "rebase")

GIT_NARROW_RULES = (
    "Bash(git stash)",
    "Bash(git stash push:*)",
    "Bash(git stash pop:*)",
    "Bash(git stash apply:*)",
    "Bash(git stash list:*)",
    "Bash(git stash show:*)",
    "Bash(git branch)",
    "Bash(git branch --show-current)",
    "Bash(git worktree add:*)",
    "Bash(git worktree list:*)",
)


def test_headless_preset_ships_local_git_allowlist():
    data = json.loads(HEADLESS_PRESET.read_text())
    allow = data["permissions"]["allow"]
    assert "Bash(cd:*)" in allow
    for verb in GIT_WILDCARD_VERBS:
        assert f"Bash(git {verb}:*)" in allow, verb
    for rule in GIT_NARROW_RULES:
        assert rule in allow, rule


def test_headless_preset_gates_network_write_git():
    data = json.loads(HEADLESS_PRESET.read_text())
    allow = data["permissions"]["allow"]
    for banned in ("Bash(git push:*)", "Bash(git remote:*)", "Bash(git reset:*)",
                   "Bash(git -C:*)"):
        assert banned not in allow, banned


def test_headless_preset_gates_destructive_shared_git_verbs():
    allow = json.loads(HEADLESS_PRESET.read_text())["permissions"]["allow"]
    for banned in ("Bash(git stash:*)", "Bash(git branch:*)", "Bash(git worktree:*)"):
        assert banned not in allow, banned
    for uncovered in ("Bash(git stash drop:*)", "Bash(git stash clear:*)",
                      "Bash(git branch -D:*)", "Bash(git worktree remove:*)"):
        assert uncovered not in allow, uncovered


MANAGER_SETTINGS_PRESET = PRESETS / "manager-settings.json"


def test_manager_settings_preset_allows_boot_mcp_tools():
    allow = json.loads(MANAGER_SETTINGS_PRESET.read_text())["permissions"]["allow"]
    for rule in (
        "mcp__dockwright__become_manager",
        "mcp__dockwright__become_manager_with_takeover",
        "mcp__dockwright__prepare_recovery_handoff",
        "mcp__dockwright__attach_existing",
        "mcp__dockwright__list_workers",
        "mcp__dockwright__list_pending_questions",
        "mcp__dockwright__list_managers",
        "mcp__dockwright__list_closed_workers",
        "mcp__dockwright__get_worker_summary",
        "mcp__dockwright__get_worker_tail",
        "Bash(printenv:*)",
        "Bash(dockwright boot-brief:*)",
    ):
        assert rule in allow, f"manager preset must allow {rule}"


def test_manager_settings_preset_excludes_monitor_ungoverned_shell():
    allow = json.loads(MANAGER_SETTINGS_PRESET.read_text())["permissions"]["allow"]
    assert "Monitor" not in allow


def test_manager_settings_preset_is_allowlist_only_no_default_mode():
    settings = json.loads(MANAGER_SETTINGS_PRESET.read_text())
    perms = settings["permissions"]
    assert "defaultMode" not in perms
    assert "deny" not in perms


def test_manager_settings_preset_excludes_mutating_fleet_tools():
    allow = json.loads(MANAGER_SETTINGS_PRESET.read_text())["permissions"]["allow"]
    for banned in (
        "mcp__dockwright__spawn_worker",
        "mcp__dockwright__kill_worker",
        "mcp__dockwright__resume_worker",
        "mcp__dockwright__answer_question",
        "mcp__dockwright__send_manager_to_worker",
    ):
        assert banned not in allow, f"{banned} must stay off the manager allowlist"


def test_headless_preset_allows_the_ISOLATED_playwright_server_only():
    settings = json.loads((PRESETS / "worker-headless-settings.json").read_text())
    allow = settings["permissions"]["allow"]

    assert "mcp__playwright-isolated__*" in allow, (
        "headless preset must allow the isolated playwright server, or spawned workers "
        "stall on a permission prompt they cannot answer"
    )

    def _mcp_server(rule):
        return rule[len("mcp__"):].split("__", 1)[0] if rule.startswith("mcp__") else None

    permitted_servers = {"dockwright", "playwright-isolated"}
    ungoverned = sorted({
        _mcp_server(r) for r in allow if _mcp_server(r) not in permitted_servers | {None}
    })
    assert not ungoverned, (
        "headless preset grants MCP servers this test does not govern: "
        f"{ungoverned}. If that is intended, add it to permitted_servers ON PURPOSE — and "
        "never add `playwright`, which is the engineer's real single-client browser."
    )
