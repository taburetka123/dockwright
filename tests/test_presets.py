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
    # ~/.claude/dockwright/presets/, and neither the spawn shell (shlex-quoted
    # args) nor claude's --settings expands `~`.
    assert DEPLOYED_VERIFIER_PATH in text
    # The orchestrator-era home retired with the compat symlink — an operator
    # toml re-pinning it must fail HERE, not resolve silently at spawn time.
    assert LEGACY_VERIFIER_PATH not in text
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
    # auto, not the old manual-approval mode: under manual approval, a
    # non-allowlisted `${…}` Bash command hits the un-allowlistable
    # expansion-guard dialog and headless workers stall on turn 1 (spec
    # Decision 1). Explicit on purpose — the preset must pin a headless-safe
    # mode regardless of the operator's global.
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
    # The read-only construction must survive the additions.
    deny = settings["permissions"]["deny"]
    for tool in ("Write", "Edit", "NotebookEdit"):
        assert tool in deny


def test_headless_preset_path_var_defined_and_wired():
    # Source-shape pin: the default value must stay HOME-relative via the
    # <absolute-home> token (compose expands it per-machine; a hardcoded
    # /Users/... here would ship one operator's home to every install).
    # Resolution behavior is pinned by
    # test_composed_generic_resolves_preset_paths_no_overlay below.
    vars_toml = (REPO_ROOT / "deploy" / "agents" / "vars.defaults.toml").read_text()
    assert ("worker_headless_settings_path = "
            "'<absolute-home>/.claude/dockwright/presets/worker-headless-settings.json'") in vars_toml
    core = (REPO_ROOT / "deploy" / "agents" / "manager.core.md").read_text()
    assert "{{worker_headless_settings_path}}" in core


def test_composed_generic_resolves_preset_paths_no_overlay():
    # The OSS-path guard: runs on EVERY clone (no overlay, no operator toml
    # needed) — exactly the installs where the operator-gated test above
    # skips. A fresh install must never deploy the literal token.
    text = compose_generic("manager.md")
    home = str(Path.home())
    assert "<absolute-home>" not in text
    assert f"{home}/.claude/dockwright/presets/verifier-settings.json" in text
    assert f"{home}/.claude/dockwright/presets/worker-headless-settings.json" in text


SETUP_SH = REPO_ROOT / "setup.sh"


def test_setup_finalizes_headless_preset_after_overlay():
    text = SETUP_SH.read_text()
    finalize = text.index("finalize-presets")
    overlay_copy = text.index('cp "$OVERLAY_DIR/presets/"')
    rsync_presets = text.index('rsync -a --delete "$REPO_DIR/deploy/presets/"')
    # Order: rsync fixture → overlay copy → finalize. Finalize AFTER overlay so
    # an operator preset lacking the key still gets the injection, while one
    # that pins it (even []) is respected (finalize is inject-only-if-absent).
    assert rsync_presets < overlay_copy < finalize
    assert 'finalize-presets --file "$CLAUDE_DIR/dockwright/presets/worker-headless-settings.json"' in text
    # The old "fixtures stay verbatim" comment is false once finalize exists.
    assert "stay verbatim" not in text


def test_manager_core_documents_additional_directories_gotcha():
    core = (REPO_ROOT / "deploy" / "agents" / "manager.core.md").read_text()
    idx = core.index("{{worker_headless_settings_path}}")
    window = core[idx:idx + 2500]
    assert "additionalDirectories" in window, (
        "headless-spawn rule must name the directory-access gate and require "
        "composed preset copies to keep permissions.additionalDirectories")


# Verbs safe with the wildcard `Bash(git <verb>:*)` form — their blast radius is
# the worker's own worktree/index. stash/branch/worktree are NOT here: they reach
# the SHARED .git and are narrowed below (test_headless_preset_gates_destructive_shared_git_verbs).
GIT_WILDCARD_VERBS = ("status", "diff", "log", "show", "add", "commit", "init",
                      "checkout", "switch", "restore", "rev-parse",
                      "fetch", "pull", "merge", "rebase")

# Narrow rules for the shared-.git verbs: the destructive/deletion forms
# (stash drop|clear, branch -D, worktree remove --force) are deliberately excluded.
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
    # The stash stack, branches, and worktree list all live in the SHARED .git,
    # so a wildcard `Bash(git stash:*)` admits `stash drop`/`clear` (can wipe
    # another worktree's WIP), `Bash(git branch:*)` admits `branch -D`, and
    # `Bash(git worktree:*)` admits `worktree remove --force`. The narrow rules
    # above replace those wildcards; assert the wildcards are ABSENT so nobody
    # re-widens, and that the destructive stash forms are not otherwise covered.
    allow = json.loads(HEADLESS_PRESET.read_text())["permissions"]["allow"]
    for banned in ("Bash(git stash:*)", "Bash(git branch:*)", "Bash(git worktree:*)"):
        assert banned not in allow, banned
    for uncovered in ("Bash(git stash drop:*)", "Bash(git stash clear:*)",
                      "Bash(git branch -D:*)", "Bash(git worktree remove:*)"):
        assert uncovered not in allow, uncovered


MANAGER_SETTINGS_PRESET = PRESETS / "manager-settings.json"


def test_manager_settings_preset_allows_boot_mcp_tools():
    # F-2: every read-only MCP tool a manager invokes DURING boot
    # (manager/manager-resume/manager-reboot/manager-takeover-recovery) plus the
    # two boot Bash verbs must be pre-allowed so a fresh boot clears most of its
    # approval prompts without a human. Monitor is deliberately NOT here — see
    # test_manager_settings_preset_excludes_monitor_ungoverned_shell.
    allow = json.loads(MANAGER_SETTINGS_PRESET.read_text())["permissions"]["allow"]
    for rule in (
        "mcp__dockwright__become_manager",
        "mcp__dockwright__become_manager_with_takeover",
        # Recovery-boot step 3 (manager-takeover-recovery.md) — the recovery lane
        # is UNATTENDED by definition (replaces a bricked manager), so its boot
        # must be zero-touch. Same registration/state-plane trust class as
        # become_manager_with_takeover.
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
    # Monitor's `command` is arbitrary shell that NONE of the Bash(...) rules
    # govern — allowlisting it hands out an ungoverned, unprompted shell, and its
    # scoping syntax is unverifiable (the host can't reproduce the prompts). So it
    # is deliberately OFF the allowlist: the four boot Monitor arms still prompt
    # (one per arm) — the accepted price for not shipping a blanket shell grant.
    allow = json.loads(MANAGER_SETTINGS_PRESET.read_text())["permissions"]["allow"]
    assert "Monitor" not in allow


def test_manager_settings_preset_is_allowlist_only_no_default_mode():
    # No defaultMode escalation: the manager keeps the session's default
    # (interactive) mode — this preset is allowlist-only, not a bypass.
    settings = json.loads(MANAGER_SETTINGS_PRESET.read_text())
    perms = settings["permissions"]
    assert "defaultMode" not in perms
    assert "deny" not in perms


def test_manager_settings_preset_excludes_mutating_fleet_tools():
    # spawn/kill/send/answer/resume stay OFF deliberately: these mutate the fleet
    # and must still prompt even in the one trusted, human-attended manager tab.
    allow = json.loads(MANAGER_SETTINGS_PRESET.read_text())["permissions"]["allow"]
    for banned in (
        "mcp__dockwright__spawn_worker",
        "mcp__dockwright__kill_worker",
        "mcp__dockwright__resume_worker",
        "mcp__dockwright__answer_question",
        "mcp__dockwright__send_manager_to_worker",
    ):
        assert banned not in allow, f"{banned} must stay off the manager allowlist"
