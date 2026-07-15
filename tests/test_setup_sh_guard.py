"""Tests for setup.sh worktree guard logic."""
import os
import stat
import subprocess
from pathlib import Path


# The guard block extracted verbatim from setup.sh (keep in sync).
# Test the exact bash logic, not a paraphrase.
_GUARD = """\
if [ "${DOCKWRIGHT_SETUP_ALLOW_WORKTREE:-}" != "1" ] && [ -f "$REPO_DIR/.git" ]; then
    COMMON_GIT_DIR="$(git -C "$REPO_DIR" rev-parse --git-common-dir 2>/dev/null || true)"
    if [ -z "$COMMON_GIT_DIR" ]; then
        echo "ERROR: Running from a linked worktree but 'git rev-parse --git-common-dir' failed (git not installed or not a git repo?). Run setup.sh directly from the main clone." >&2
        exit 1
    fi
    MAIN_CLONE="$(dirname "$COMMON_GIT_DIR")"
    if [ ! -d "$MAIN_CLONE" ] || [ ! -f "$MAIN_CLONE/setup.sh" ]; then
        echo "ERROR: Running from a linked worktree but could not locate the main clone (resolved '$MAIN_CLONE'). Run setup.sh directly from the main clone." >&2
        exit 1
    fi
    echo "→ Running from linked worktree; self-anchoring install to main clone: $MAIN_CLONE"
    REPO_DIR="$MAIN_CLONE"
fi
"""


def _make_fake_git(tmp_path: Path, common_git_dir: Path) -> Path:
    """Create a fake `git` binary that returns common_git_dir for --git-common-dir."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    fake_git = bin_dir / "git"
    fake_git.write_text(
        "#!/bin/bash\n"
        'if [[ "$*" == *"--git-common-dir"* ]]; then\n'
        f'  echo "{common_git_dir}"\n'
        "else\n"
        '  exec /usr/bin/git "$@"\n'
        "fi\n"
    )
    fake_git.chmod(fake_git.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _run_guard(repo_dir: Path, env: dict) -> subprocess.CompletedProcess:
    script = f'set -euo pipefail\nREPO_DIR="{repo_dir}"\n' + _GUARD + '\necho "FINAL=$REPO_DIR"\n'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)


def test_main_clone_no_redirect(tmp_path):
    """Guard is a no-op when .git is a directory (main clone)."""
    main_clone = tmp_path / "main"
    main_clone.mkdir()
    (main_clone / ".git").mkdir()  # directory = main clone

    env = os.environ.copy()
    result = _run_guard(main_clone, env)

    assert result.returncode == 0, result.stderr
    assert "self-anchoring" not in result.stdout
    assert f"FINAL={main_clone}" in result.stdout


def test_linked_worktree_redirects(tmp_path):
    """Guard redirects REPO_DIR to main clone when .git is a file (linked worktree)."""
    main_clone = tmp_path / "main"
    main_clone.mkdir()
    (main_clone / ".git").mkdir()
    (main_clone / "setup.sh").write_text("#!/bin/bash\n")  # sanity-check file

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {main_clone}/.git/worktrees/test\n")

    bin_dir = _make_fake_git(tmp_path, main_clone / ".git")
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    result = _run_guard(worktree, env)

    assert result.returncode == 0, result.stderr
    assert "self-anchoring" in result.stdout
    assert f"FINAL={main_clone}" in result.stdout


def test_git_failure_exits_with_error(tmp_path):
    """Guard exits 1 with a clear message when git rev-parse fails."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /nonexistent/.git\n")

    # git binary that always fails for --git-common-dir
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    fake_git = bin_dir / "git"
    fake_git.write_text(
        "#!/bin/bash\n"
        'if [[ "$*" == *"--git-common-dir"* ]]; then\n'
        "  exit 1\n"
        "else\n"
        '  exec /usr/bin/git "$@"\n'
        "fi\n"
    )
    fake_git.chmod(fake_git.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    result = _run_guard(worktree, env)

    assert result.returncode == 1
    assert "git rev-parse --git-common-dir" in result.stderr


def test_main_clone_not_found_exits_with_error(tmp_path):
    """Guard exits 1 when resolved main clone path doesn't exist."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /nonexistent/.git\n")

    nonexistent = tmp_path / "nonexistent" / ".git"
    bin_dir = _make_fake_git(tmp_path, nonexistent)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    result = _run_guard(worktree, env)

    assert result.returncode == 1
    assert "could not locate the main clone" in result.stderr


def test_guard_matches_setup_sh():
    """Embedded _GUARD must match the guard block in setup.sh — prevents silent drift."""
    setup_sh = Path(__file__).resolve().parent.parent / "setup.sh"
    content = setup_sh.read_text()
    # Strip the leading/trailing blank lines that _GUARD doesn't include
    guard_stripped = _GUARD.strip()
    assert guard_stripped in content, (
        "The _GUARD string in this test file has drifted from setup.sh. "
        "Update _GUARD to match the current guard block in setup.sh."
    )


_WORKTREE_REFUSAL = """\
if [ "${DOCKWRIGHT_SETUP_ALLOW_WORKTREE:-}" != "1" ]; then
    case "$REPO_DIR" in
        "$HOME"/worktrees*)
            echo "ERROR: refusing to install from a worktree path ($REPO_DIR). Run setup.sh from the main clone." >&2
            exit 1
            ;;
    esac
fi
"""

def _run_refusal(repo_dir, home):
    script = (f'set -euo pipefail\nHOME="{home}"\nREPO_DIR="{repo_dir}"\n'
              + _WORKTREE_REFUSAL + '\necho "PASSED=$REPO_DIR"\n')
    import subprocess
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

def test_refuses_worktree_path(tmp_path):
    home = tmp_path
    r = _run_refusal(f"{home}/worktrees-personal/x/claude-orchestrator", home)
    assert r.returncode == 1
    assert "refusing to install from a worktree" in r.stderr

def test_allows_canonical_path(tmp_path):
    home = tmp_path
    r = _run_refusal(f"{home}/projects/personal/claude-orchestrator", home)
    assert r.returncode == 0
    assert "PASSED=" in r.stdout

def test_allow_worktree_env_bypasses_refusal(tmp_path):
    """DOCKWRIGHT_SETUP_ALLOW_WORKTREE=1 lets a worktree path through the refusal
    (the S6 sandbox escape) — while the default (no env) still refuses above."""
    home = tmp_path
    script = (f'set -euo pipefail\nHOME="{home}"\n'
              'DOCKWRIGHT_SETUP_ALLOW_WORKTREE=1\n'
              f'REPO_DIR="{home}/worktrees-personal/x/claude-orchestrator"\n'
              + _WORKTREE_REFUSAL + '\necho "PASSED=$REPO_DIR"\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "PASSED=" in r.stdout
    assert "refusing" not in r.stderr

def test_worktree_refusal_matches_setup_sh():
    setup_sh = Path(__file__).resolve().parent.parent / "setup.sh"
    assert _WORKTREE_REFUSAL.strip() in setup_sh.read_text(), \
        "_WORKTREE_REFUSAL drifted from setup.sh — update it to match."


# The GitHub https->ssh url-rewrite block moved OUT of setup.sh into the
# operator overlay (~/.claude/dockwright-overlay/setup.d/10-ssh-rewrites.sh)
# at the dockwright OSS split -- it is operator-personal (per-org SSH-host
# scoping), not product core. Its former drift-pin string and the per-org
# live-value resolution assertions retired with it; the
# setup.d step runs last and is skipped in the FILES_ONLY sandbox.


def test_setup_composes_agents_instead_of_cp():
    text = (Path(__file__).resolve().parent.parent / "setup.sh").read_text()
    # Compose runs via $RENDER_BIN — the render binary ($ORCH_BIN in a normal
    # install; DOCKWRIGHT_ORCH_BIN in the FILES_ONLY sandbox). Gating on
    # RENDER_BIN is what lets the byte-equivalence gate compose from a worktree.
    assert '"$RENDER_BIN" compose --core-dir "$REPO_DIR/deploy/agents" --out-dir "$CLAUDE_DIR/agents"' in text
    assert 'RENDER_BIN="$DOCKWRIGHT_BIN"' in text
    assert 'cp "$REPO_DIR/deploy/agents/"*.md' not in text
    # Codex mirrors are generated from the COMPOSED files, not canon
    assert "src_dir = Path('$CLAUDE_DIR') / 'agents'" in text
    # doctor verifies compose freshness on every setup run
    assert "--compose-core-dir" in text and "--compose-out-dir" in text
    # codex mirror scoped to composed core files via the stamp — never the
    # whole ~/.claude/agents/ dir, which may hold foreign agent files
    assert ".compose-stamp.json" in text
    assert "src_dir.glob('*.md')" not in text


def test_setup_stamps_deployed_script_provenance():
    """Deployed .py/.sh script copies get a `# deployed-from:` provenance header;
    .md files (commands, agents) are exempt — a header line would enter agent/
    command context, and agents already carry the compose-stamp sidecar."""
    text = (Path(__file__).resolve().parent.parent / "setup.sh").read_text()

    # sha resolved once, up front
    assert 'DEPLOY_SHA_SHORT="$(git -C "$REPO_DIR" rev-parse --short HEAD' in text

    # the stamping function exists and produces the exact provenance format
    assert "stamp_provenance() {" in text
    assert (
        'header = "# deployed-from: dockwright@" + sha + '
        '" — do not edit here; edit " + source_rel + " in the repo\\n"'
    ) in text

    # idempotent: a prior header is replaced in place, not duplicated
    assert 'lines[insert_at].startswith("# deployed-from:")' in text
    assert "lines[insert_at] = header" in text
    assert "lines.insert(insert_at, header)" in text

    # wired to BOTH deployed-script sources: deploy/scripts/*.{py,sh} and the
    # stale_monitor.py cp from src/dockwright/
    assert 'stamp_provenance "$CLAUDE_DIR/scripts/$name" "deploy/scripts/$name"' in text
    assert 'stamp_provenance "$CLAUDE_DIR/scripts/stale_monitor.py" "src/dockwright/stale_monitor.py"' in text

    # CONTRACT: the stamping loop iterates SOURCE basenames (repo deploy/scripts
    # globs), NEVER the target dir — ~/.claude/scripts/ also holds operator-
    # personal scripts deployed by other repos (claude-config's archive-dialog.py,
    # auto-commit-on-edit.sh, ...); a target-dir glob would stamp those with
    # false provenance pointing at deploy/scripts/ paths that don't exist.
    assert 'for f in "$REPO_DIR/deploy/scripts/"*.py "$REPO_DIR/deploy/scripts/"*.sh; do' in text
    assert '"$CLAUDE_DIR/scripts/"*.py "$CLAUDE_DIR/scripts/"*.sh; do' not in text

    # only ever stamps .py/.sh — never .md, and never the commands/ or agents/
    # deploy targets
    assert text.count("stamp_provenance") == 4  # function def + three call sites (core loop / stale_monitor / overlay loop)
    assert 'stamp_provenance "$f" "deploy/commands' not in text
    assert 'stamp_provenance "$f" "deploy/agents' not in text
    stamp_block = text.split("stamp_provenance() {", 1)[1]
    stamp_block = stamp_block[: stamp_block.index("src/dockwright/stale_monitor.py")]
    assert ".md" not in stamp_block


def test_setup_backup_helper_and_dockwright_identity():
    """B2 backup helper + the dockwright identity pass on setup.sh: the
    user-visible statusline deploy backs up before overwrite; the one-release
    pip sweep drops the pre-rename dist; the MCP registers as `dockwright`
    (removing BOTH keys first); no stale @@ORCH_BIN@@ placeholder survives; and
    the closing echo dropped the retired ccm attach helper (publish patch 04)."""
    text = (Path(__file__).resolve().parent.parent / "setup.sh").read_text()

    # B2 backup-before-overwrite helper defined + used on the statusline cp.
    assert "backup_then_cp() {" in text
    assert ('backup_then_cp "$REPO_DIR/deploy/statusline-command.sh" '
            '"$CLAUDE_DIR/statusline-command.sh"') in text

    # one-release sweep of the pre-rename distribution before the editable reinstall.
    assert "-m pip uninstall -y claude-orchestrator" in text

    # venv-bin var renamed to DOCKWRIGHT_BIN (dockwright console script).
    assert 'DOCKWRIGHT_BIN="$REPO_DIR/.venv/bin/dockwright"' in text
    assert "@@ORCH_BIN@@" not in text

    # MCP registers under the new `dockwright` server name; BOTH keys removed first.
    assert 'claude mcp add --scope user dockwright "$DOCKWRIGHT_BIN" mcp-server' in text
    assert "claude mcp remove --scope user dockwright" in text
    assert "claude mcp remove --scope user claude-orchestrator" in text  # legacy key still swept

    # closing echo: retired ccm attach helper (publish patch 04), leads with the
    # `dockwright manager` one-liner (the manual tmux dance is now a parenthetical).
    assert " ccm " not in text
    assert 'echo "    dockwright manager"' in text
    assert 'tmux -L dockwright -f ~/.claude/dockwright/dockwright.tmux.conf new-session' in text
    assert 'echo "    tmux -L dockwright new-session"' not in text


def test_setup_backs_up_command_copies():
    """The shell-cp command deploy sites into the user-visible Claude + Codex
    command dirs go through per-file backup_then_cp (not glob-cp), so an operator
    hand-edit survives a re-run — both the no-render verbatim fallback and the
    overlay path (which runs on EVERY overlay install). The mktemp skill-wrapper
    staging cp stays a plain cp (not a user-visible surface)."""
    text = (Path(__file__).resolve().parent.parent / "setup.sh").read_text()

    # per-file backup on both user-visible command dests
    assert 'backup_then_cp "$f" "$CLAUDE_DIR/commands/$(basename "$f")"' in text
    assert 'backup_then_cp "$f" "$CODEX_DIR/commands/$(basename "$f")"' in text

    # the old glob-cp sites into the user-visible command dirs are gone
    assert 'cp "$REPO_DIR/deploy/commands/"*.md "$CLAUDE_DIR/commands/"' not in text
    assert 'cp "$REPO_DIR/deploy/commands/"*.md "$CODEX_DIR/commands/"' not in text
    assert 'cp "$OVERLAY_DIR/commands/"*.md "$CLAUDE_DIR/commands/"' not in text
    assert 'cp "$OVERLAY_DIR/commands/"*.md "$CODEX_DIR/commands/"' not in text

    # the codex skill-wrapper STAGING cp (into a mktemp dir) stays a plain cp
    assert 'cp "$OVERLAY_DIR/commands/"*.md "$CODEX_SKILL_SRC/"' in text
