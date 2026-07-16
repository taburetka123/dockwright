"""Regression guards for the slash-command markdown files.

The `/manager`, `/manager-resume`, and `/manager-takeover-recovery` commands
paint the manager's own tmux tab with a literal `tmux rename-window` +
`set-window-option window-status-*-style` invocation (the SessionStart hook
can't — user-launched managers have no CLAUDE_AGENT env). The command-layer
shapes MUST mirror `TmuxDriver.set_tab_title`/`set_tab_color` in terminal.py:
key off `$TMUX_PANE`, use the `dockwright` socket via `-L "$SOCK"`, and carry
the MANAGER_TAB_COLOR backgrounds (#aa0066 active / #440022 inactive) with a
`fg=#ffffff`. The legacy kitty `kitty @ set-tab-*` form (and its
`window_id:`-vs-`id:` match caveat) is gone — tmux has no tab/window id-space
collision, so the guard now asserts the tmux shape and that no kitty residue
returns.
"""
import re
from pathlib import Path

import pytest

from dockwright import compose

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMANDS = REPO_ROOT / "deploy" / "commands"
SKILLS = REPO_ROOT / "deploy" / "skills"
SCRIPTS = REPO_ROOT / "deploy" / "scripts"
PRESETS = REPO_ROOT / "deploy" / "presets"
AGENTS = REPO_ROOT / "deploy" / "agents"
DEPLOY = REPO_ROOT / "deploy"


def _frontmatter_name(text: str) -> str | None:
    """Extract the `name:` value from a leading `---` frontmatter block."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    for line in text[4:end].splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    return None


@pytest.mark.parametrize(
    "filename", ["manager.md", "manager-resume.md", "manager-takeover-recovery.md"]
)
def test_manager_tab_paint_mirrors_tmux_driver(filename):
    text = (COMMANDS / filename).read_text()
    # The tab-paint block must key off the tmux pane and mirror TmuxDriver.
    assert "[ -n \"$TMUX_PANE\" ]" in text, (
        f"{filename}: manager tab paint must gate on $TMUX_PANE"
    )
    assert 'tmux -L "$SOCK" rename-window -t "$TMUX_PANE"' in text, (
        f"{filename}: tab title must use tmux rename-window (TmuxDriver.set_tab_title)"
    )
    assert (
        'set-window-option -t "$TMUX_PANE" window-status-current-style "bg=#aa0066,fg=#ffffff"'
        in text
    ), f"{filename}: active tab color must mirror TmuxDriver.set_tab_color"
    assert (
        'set-window-option -t "$TMUX_PANE" window-status-style "bg=#440022,fg=#ffffff"'
        in text
    ), f"{filename}: inactive tab color must mirror TmuxDriver.set_tab_color"
    # The legacy kitty form must never reappear.
    assert "kitty @" not in text, f"{filename}: kitty residue must not return"
    assert "--match=window_id:$KITTY_WINDOW_ID" not in text, (
        f"{filename}: kitty tab-paint match arg must not return"
    )


@pytest.mark.parametrize("filename", ["manager.md", "manager-resume.md"])
def test_manager_commands_resolve_claude_session_id(filename):
    # Managers are Claude-only — own-sid resolution uses CLAUDE_CODE_SESSION_ID,
    # with no Codex ($CODEX_THREAD_ID) branch.
    text = (COMMANDS / filename).read_text()
    assert "CLAUDE_CODE_SESSION_ID" in text
    assert "Do not invent or synthesize a sid" in text
    assert "CODEX_THREAD_ID" not in text


@pytest.mark.parametrize("filename", ["manager.md", "manager-resume.md"])
def test_manager_commands_arm_four_monitors_unconditionally(filename):
    text = (COMMANDS / filename).read_text()
    # The Monitor-vs-bridge runtime fork is gone — always arm the four Monitors.
    assert "Arm the four Monitor tasks" in text
    assert "dockwright monitor questions" in text
    assert "dockwright monitor turn-ends" in text
    assert "dockwright monitor done" in text
    assert "dockwright monitor stale" in text
    # No Codex manager branch / push bridge survives.
    assert "If `<runtime>` is `codex`" not in text
    assert "codex-push-watch" not in text


def test_manager_command_become_manager_has_no_runtime_arg():
    text = (COMMANDS / "manager.md").read_text()
    assert "runtime=<runtime>" not in text
    assert "Resolve `<runtime>`" not in text
    assert "{ok, name, domain, runtime}" in text


def test_manager_agent_has_no_codex_manager_push():
    text = (REPO_ROOT / "deploy" / "agents" / "manager.core.md").read_text()
    assert "Codex manager push" not in text
    assert "codex-push-watch" not in text
    assert "stale-health/autoclose push is not implemented for Codex managers" not in text
    # The generic wake-up reaction guidance survives (now Monitor-only).
    assert "wake-up" in text.lower()
    assert "get_worker_summary" in text


def test_manager_agent_monitor_sections_are_claude_only():
    text = (REPO_ROOT / "deploy" / "agents" / "manager.core.md").read_text()
    assert "For Claude managers, the `/manager` startup arms a Monitor" in text
    assert "For Claude managers, the `/manager` startup also arms a Monitor" in text
    assert "For Claude managers, a fourth Monitor" in text


def test_manager_agent_prefers_done_event_payload_over_get_worker_summary():
    text = (REPO_ROOT / "deploy" / "agents" / "manager.core.md").read_text()
    assert "prefer the explicit done event payload/file for `dockwright: worker <worker> done`" in text
    assert "`get_worker_summary` is secondary context, not a replacement for the `worker_done` summary" in text


def test_recreate_manager_command_is_claude_only():
    text = (COMMANDS / "recreate-manager.md").read_text()
    assert "spawn_replacement_manager(handoff_id=<id>)" in text
    assert "CLAUDE_CODE_SESSION_ID" in text
    # No Codex runtime selector / bootstrap-codex fallback survives.
    assert "[claude|codex]" not in text
    assert "--runtime" not in text
    assert "CODEX_THREAD_ID" not in text
    assert "codex" not in text.lower()


def test_bootstrap_recreate_is_claude_only():
    text = (SCRIPTS / "bootstrap-recreate.sh").read_text()
    # Manager lane is pinned to opus[1m] (orch-audit model-allocation) — see
    # tests/test_model_pins.py::test_bootstrap_recreate_pins_manager_opus for
    # the dedicated pin assertion; this test only guards claude-only-ness.
    assert "claude --model 'opus[1m]' '/manager-resume $HANDOFF_ID'" in text
    # No manager-runtime / codex plumbing survives.
    assert "MANAGER_RUNTIME" not in text
    assert "manager_runtime" not in text
    assert "replacement_runtime" not in text
    assert "--runtime" not in text
    assert "codex" not in text.lower()


def test_setup_bootstraps_pip_when_reusing_existing_virtualenv():
    text = (REPO_ROOT / "setup.sh").read_text()
    assert '[ ! -x "$REPO_DIR/.venv/bin/pip" ]' in text
    assert '"$REPO_DIR/.venv/bin/python" -m ensurepip --upgrade' in text
    assert '"$REPO_DIR/.venv/bin/python" -m pip install -e "$REPO_DIR"' in text


def test_worker_agent_bakes_auto_publish_discipline():
    text = (REPO_ROOT / "deploy" / "agents" / "worker.core.md").read_text()
    assert "without waiting for the manager to ask" in text
    assert "~/.claude/dockwright/assignments/" in text       # key-discovery fallback
    assert "must never fail your task" in text                 # non-blocking rule
    assert "artifacts_published" in text                       # done-event tie-in


def test_manager_agent_consumes_not_instructs_auto_publish():
    text = (REPO_ROOT / "deploy" / "agents" / "manager.core.md").read_text()
    assert "Multi-phase work auto-publishes" in text
    assert "pass the SAME `task_key`" in text
    # The pre-auto-publish manual phrasing must not survive — it would tell
    # managers to re-instruct what the spawn path now injects.
    assert "tell workers to persist phase outputs" not in text


def test_bootstrap_recreate_uses_login_config_dir():
    """The recreated manager rides the active pointer via a per-config-dir
    keychain login — no token is injected. a -> default ~/.claude (no
    CLAUDE_CONFIG_DIR); b -> ~/.claude-b iff its farm carries the orchestrator
    MCP, else fall back to the default login."""
    text = (SCRIPTS / "bootstrap-recreate.sh").read_text()
    assert "claude-orch-token" not in text
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in text
    assert "CONFIG_PREFIX" in text
    assert "CLAUDE_CONFIG_DIR=$FARM" in text
    # Farm-health gate uses jq membership (not a loose grep substring), and accepts
    # EITHER generation's server key — `dockwright` (new) OR `claude-orchestrator`
    # (old) — so a farm registered under either name is recognized during migration.
    assert 'jq -e \'.mcpServers["dockwright"] // .mcpServers["claude-orchestrator"]\'' in text
    assert 'grep -q \'"claude-orchestrator"\'' not in text


def test_bootstrap_recreate_tmux_conf_legacy_fallback():
    import shutil, subprocess
    text = (SCRIPTS / "bootstrap-recreate.sh").read_text()
    # New home first, then TWO legacy fallbacks (dockwright-rename, one release).
    assert 'TMUX_CONF_FILE="$HOME/.claude/dockwright/dockwright.tmux.conf"' in text
    assert 'TMUX_CONF_LEGACY="$HOME/.claude/orchestrator/dockwright.tmux.conf"' in text
    assert 'TMUX_CONF_LEGACY2="$HOME/.claude/orchestrator/claude-orch.tmux.conf"' in text
    assert 'elif [ -f "$TMUX_CONF_LEGACY" ]; then FFLAG=(-f "$TMUX_CONF_LEGACY")' in text
    assert 'elif [ -f "$TMUX_CONF_LEGACY2" ]; then FFLAG=(-f "$TMUX_CONF_LEGACY2")' in text
    assert shutil.which("bash")
    r = subprocess.run(["bash", "-n", str(SCRIPTS / "bootstrap-recreate.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_manager_takeover_recovery_command():
    text = (REPO_ROOT / "deploy" / "commands" / "manager-takeover-recovery.md").read_text()
    for needle in (
        "prepare_recovery_handoff",
        "become_manager_with_takeover",
        "list_managers",
        "become_manager",
        "dockwright monitor questions",
        "dockwright monitor turn-ends",
        "dockwright monitor done",
        "dockwright monitor stale",
        "kill_worker",
        "list_closed_workers",
        "resume_worker",
        "dockwright distill",
        "account-flips.jsonl",
    ):
        assert needle in text, needle


# --- Step-4b folded operator assets (dockwright-* skills + command) --------
# Five operator skills + one operator command were folded into the product
# payload as dockwright-prefixed, genericized copies. Guard their existence,
# their frontmatter `name:` (skills), and their operator-token cleanliness.

FOLDED_SKILLS = [
    "dockwright-orchestrator-guide",
    "dockwright-recap",
    "dockwright-todo",
    "dockwright-dotodo",
    "dockwright-meta-improvement",
]


@pytest.mark.parametrize("skill", FOLDED_SKILLS)
def test_folded_skill_exists_and_names_itself(skill):
    path = SKILLS / skill / "SKILL.md"
    assert path.is_file(), f"{skill}: SKILL.md missing at {path}"
    name = _frontmatter_name(path.read_text())
    assert name == skill, (
        f"{skill}: frontmatter name is {name!r}, expected {skill!r}"
    )


def test_folded_threads_command_exists_with_frontmatter():
    path = COMMANDS / "dockwright-threads.md"
    assert path.is_file(), f"dockwright-threads.md missing at {path}"
    text = path.read_text()
    # Commands carry a `description:` frontmatter, not a `name:` field.
    assert text.startswith("---\n"), "dockwright-threads.md must open with frontmatter"
    assert "\ndescription:" in text.split("\n---\n", 1)[0], (
        "dockwright-threads.md frontmatter must carry a description"
    )


# --- Step-7c: rendered-generic sweep over the edited commands + presets -------
# The command/preset render seam (setup.sh → `orchestrator render`) resolves
# {{vars}} from `deploy/agents/vars.defaults.toml` ⊕ `dockwright.toml
# [agent_vars]`. A fresh OSS checkout ships DEFAULTS ONLY, so the defaults-render
# (the generic flavor) MUST be free of every operator token AND leave no {{var}}
# unbound — a var with no generic default would ship a literal `{{…}}` to a user.
# (The OPERATOR flavor's byte-identity to the pre-Step-7 bytes is verified out of
# band by the render byte-equivalence gate, which needs the live dockwright.toml.)

_RENDERED_GENERIC_FILES = [
    COMMANDS / "dockwright-general-work.md",
    PRESETS / "dockwright-fix-S.md",
    PRESETS / "dockwright-fix-M.md",
    PRESETS / "dockwright-fix-L.md",
    PRESETS / "scratch.md",
]

_UNBOUND_VAR_RE = re.compile(r"\{\{[A-Za-z0-9_]+\}\}")


def _render_with_defaults(path: Path) -> str:
    """Render `path` with the generic DEFAULTS layer only (no operator toml)."""
    defaults = compose.load_default_vars(AGENTS)
    rendered, _warnings = compose.compose_text(path.read_text(), [], defaults)
    return rendered


@pytest.mark.parametrize("path", _RENDERED_GENERIC_FILES, ids=lambda p: p.name)
def test_edited_command_preset_has_no_unbound_vars_with_defaults(path):
    rendered = _render_with_defaults(path)
    unbound = _UNBOUND_VAR_RE.findall(rendered)
    assert not unbound, (
        f"{path.name}: {sorted(set(unbound))} left unbound under a defaults-only "
        "render — every {{var}} used in a shipped command/preset needs a generic "
        "default in deploy/agents/vars.defaults.toml")


@pytest.mark.parametrize("relpath", [
    "agents/manager.core.md",
    "agents/worker.core.md",
    "commands/dockwright-general-work.md",
    "commands/manager-takeover-recovery.md",
])
def test_headless_lanes_have_no_expansion_sid_recipes(relpath):
    # Claude Code's "Contains expansion" permission guard fires on any `$…`
    # command and no allowlist can cover it — a headless session instructed to
    # run one stalls deterministically (VM E2E L-2). These four files are the
    # machine-driven lanes; interactive manager boot commands are exempt (F-2).
    text = (DEPLOY / relpath).read_text()
    assert "${CLAUDE_CODE_SESSION_ID" not in text
    assert "echo $CLAUDE_CODE_SESSION_ID" not in text
    assert 'grep -l "\\"name\\": \\"$CLAUDE_WORKER_NAME\\""' not in text
    assert "printenv" in text  # the sanctioned expansion-free fallback
