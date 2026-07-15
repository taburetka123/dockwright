import shutil, subprocess
from pathlib import Path
SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "scripts" / "gardener-run.sh"

def test_gardener_has_tmux_visible_branch():
    src = SCRIPT.read_text()
    assert 'has-session -t claude-workers' in src
    assert 'new-session -d -s claude-workers' in src
    assert 'new-window -d -t claude-workers' in src

def test_gardener_kitty_branch_is_gone():
    src = SCRIPT.read_text()
    assert 'resolve_kitty_socket' not in src
    assert 'no-kitty-socket' not in src
    assert 'TERMINAL_BACKEND' not in src
    assert 'kitty @' not in src

def test_gardener_script_syntax_ok():
    assert shutil.which("bash")
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_gardener_notify_no_ops_under_pytest():
    """notify() runs real /usr/bin/osascript; tests exec this script for real
    (test_module_toggle), so the guard must live in the script itself — the
    2026-07-03 gardener-gate desktop-notification leak class."""
    src = SCRIPT.read_text()
    assert 'if [ -n "${PYTEST_CURRENT_TEST:-}" ]; then return 0; fi' in src


def test_gardener_tmux_conf_legacy_fallback():
    src = SCRIPT.read_text()
    # New home first, then TWO legacy fallbacks (dockwright-rename, one release).
    assert 'TMUX_CONF_FILE="$HOMEDIR/.claude/dockwright/dockwright.tmux.conf"' in src
    assert 'TMUX_CONF_LEGACY="$HOMEDIR/.claude/orchestrator/dockwright.tmux.conf"' in src
    assert 'TMUX_CONF_LEGACY2="$HOMEDIR/.claude/orchestrator/claude-orch.tmux.conf"' in src
    assert 'elif [ -f "$TMUX_CONF_LEGACY" ]; then FFLAG=(-f "$TMUX_CONF_LEGACY")' in src
    assert 'elif [ -f "$TMUX_CONF_LEGACY2" ]; then FFLAG=(-f "$TMUX_CONF_LEGACY2")' in src
