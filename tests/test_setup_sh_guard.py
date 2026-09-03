import os
import stat
import subprocess
from pathlib import Path

import pytest


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
    main_clone = tmp_path / "main"
    main_clone.mkdir()
    (main_clone / ".git").mkdir()

    env = os.environ.copy()
    result = _run_guard(main_clone, env)

    assert result.returncode == 0, result.stderr
    assert "self-anchoring" not in result.stdout
    assert f"FINAL={main_clone}" in result.stdout


def test_linked_worktree_redirects(tmp_path):
    main_clone = tmp_path / "main"
    main_clone.mkdir()
    (main_clone / ".git").mkdir()
    (main_clone / "setup.sh").write_text("#!/bin/bash\n")

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
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /nonexistent/.git\n")

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
    home = tmp_path
    script = (f'set -euo pipefail\nHOME="{home}"\n'
              'DOCKWRIGHT_SETUP_ALLOW_WORKTREE=1\n'
              f'REPO_DIR="{home}/worktrees-personal/x/claude-orchestrator"\n'
              + _WORKTREE_REFUSAL + '\necho "PASSED=$REPO_DIR"\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "PASSED=" in r.stdout
    assert "refusing" not in r.stderr


_REPO = Path(__file__).resolve().parent.parent

_ACTIVE_RECORD = (
    '{"claude_sid": "test-sid-%d", "agent": "worker", "name": "t",'
    ' "pid": 1, "state": "processing"}'
)


def _run_setup_with_fleet(tmp_path, n_records=0, extra_env=None, plant=None,
                          create_active=True):
    claude_dir = tmp_path / "claude"
    if create_active:
        active = claude_dir / "dockwright" / "active"
        active.mkdir(parents=True)
        for i in range(n_records):
            (active / f"session-{i}.json").write_text(_ACTIVE_RECORD % i)
        if plant is not None:
            plant(active)
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir(exist_ok=True)
    env = {**os.environ,
           "DOCKWRIGHT_SETUP_ALLOW_WORKTREE": "1",
           "DOCKWRIGHT_SETUP_FILES_ONLY": "1",
           "PATH": f"{stub_bin}:/usr/bin:/bin",
           "CLAUDE_DIR": str(claude_dir),
           "CODEX_DIR": str(tmp_path / "codex")}
    env.pop("DOCKWRIGHT_SETUP_FORCE", None)
    env.update(extra_env or {})
    r = subprocess.run(["bash", str(_REPO / "setup.sh")], env=env,
                       capture_output=True, text=True, cwd=str(_REPO))
    return claude_dir, r


def test_fleet_gate_refuses_with_active_session(tmp_path):
    claude_dir, r = _run_setup_with_fleet(tmp_path, n_records=1)
    assert r.returncode == 4
    assert "1 active worker/manager session(s)" in r.stderr
    assert str(claude_dir / "dockwright" / "active") in r.stderr
    assert "DOCKWRIGHT_SETUP_FORCE=1" in r.stderr
    assert not (claude_dir / "commands").exists()


def test_fleet_gate_names_the_live_count(tmp_path):
    _, r = _run_setup_with_fleet(tmp_path, n_records=3)
    assert r.returncode == 4
    assert "3 active worker/manager session(s)" in r.stderr


def test_fleet_gate_ignores_non_json_entries(tmp_path):
    def plant(active):
        (active / "not-a-session.tmp").write_text("x")
        (active / "dir-named.json").mkdir()
        (active / "nested").mkdir()
        (active / "nested" / "deep.json").write_text("{}")
    claude_dir, r = _run_setup_with_fleet(tmp_path, plant=plant)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (claude_dir / "commands").exists()


def test_fleet_gate_absent_active_dir_proceeds(tmp_path):
    claude_dir, r = _run_setup_with_fleet(tmp_path, create_active=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (claude_dir / "commands").exists()


def test_fleet_gate_force_overrides(tmp_path):
    claude_dir, r = _run_setup_with_fleet(
        tmp_path, n_records=2, extra_env={"DOCKWRIGHT_SETUP_FORCE": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (claude_dir / "commands" / "dockwright-general-work.md").exists()


def test_fleet_gate_unreadable_active_dir_fails_closed(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("permission bits don't bind as root")
    def plant(active):
        (active / "session-x.json").write_text("{}")
        active.chmod(0o000)
    claude_dir, r = _run_setup_with_fleet(tmp_path, plant=plant)
    (claude_dir / "dockwright" / "active").chmod(0o755)
    assert r.returncode == 4
    assert "cannot enumerate" in r.stderr
    assert "DOCKWRIGHT_SETUP_FORCE=1" in r.stderr
    assert not (claude_dir / "commands").exists()


def test_fleet_gate_counts_legacy_orchestrator_active(tmp_path):
    legacy = tmp_path / "claude" / "orchestrator" / "active"
    legacy.mkdir(parents=True)
    (legacy / "session-0.json").write_text(_ACTIVE_RECORD % 0)
    claude_dir, r = _run_setup_with_fleet(tmp_path, create_active=False)
    assert r.returncode == 4
    assert "1 active worker/manager session(s)" in r.stderr
    assert str(legacy) in r.stderr
    assert not (claude_dir / "commands").exists()


def test_fleet_gate_migrated_symlink_counts_once(tmp_path):
    def plant(active):
        (tmp_path / "claude" / "orchestrator").symlink_to(
            tmp_path / "claude" / "dockwright")
    _, r = _run_setup_with_fleet(tmp_path, n_records=1, plant=plant)
    assert r.returncode == 4
    assert "1 active worker/manager session(s)" in r.stderr


def test_fleet_gate_dockwright_registry_takes_precedence(tmp_path):
    def plant(active):
        (tmp_path / "claude" / "orchestrator" / "active").mkdir(parents=True)
    claude_dir, r = _run_setup_with_fleet(tmp_path, n_records=2, plant=plant)
    assert r.returncode == 4
    assert "2 active worker/manager session(s)" in r.stderr
    assert str(claude_dir / "dockwright" / "active") in r.stderr
    assert not (claude_dir / "commands").exists()


def test_fleet_gate_follows_symlinked_active_dir(tmp_path):
    real = tmp_path / "real-active"
    real.mkdir()
    (real / "session-0.json").write_text(_ACTIVE_RECORD % 0)
    def plant(active):
        active.rmdir()
        active.symlink_to(real)
    claude_dir, r = _run_setup_with_fleet(tmp_path, plant=plant)
    assert r.returncode == 4
    assert "1 active worker/manager session(s)" in r.stderr
    assert not (claude_dir / "commands").exists()
