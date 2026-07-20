"""No pytest subprocess may reach the live tmux socket or a real CLI agent.

2026-07-17 incident: a bootstrap-recreate.sh probe executed from this repo's test
workflow spawned two real `claude /manager-resume <fabricated-id>` manager windows
onto the operator's live `dockwright` socket. conftest's no_live_tmux guards only
python-level entries; these tests pin the SUBPROCESS boundary (PATH shim).

Why the tmux checks go through `bash -c` rather than subprocess.run(["tmux",...]):
conftest.no_live_tmux monkeypatches subprocess.run/create_subprocess_exec and
ABSORBS any argv whose program is "tmux" at the PYTHON layer (returns rc 0, empty
output) — a direct python tmux call never reaches PATH, so the PATH shim would be
invisible to it. The hole the shim closes is a CHILD process (a bash deploy script)
resolving `tmux` via PATH — exactly the incident. Shelling `tmux` through a child
bash process is that boundary, and it is what these tests must exercise. claude and
codex are NOT intercepted by no_live_tmux, so those calls reach the shim directly."""
import os
import subprocess


def _child_tmux(cmd: str) -> subprocess.CompletedProcess:
    """Run `tmux …` as a CHILD bash process so it resolves `tmux` via PATH (the
    shim), bypassing no_live_tmux's python-level absorb. Inherits os.environ, whose
    PATH the autouse shim fixture has fronted with the blocking shim dir."""
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)


def test_tmux_on_path_is_the_shim():
    r = _child_tmux("tmux -V")
    assert "tmux-shim" in r.stdout, (
        "PATH resolves a REAL tmux inside pytest — the CLI shim boundary is gone; "
        "any test that shells a deploy script can spawn onto the live socket")


def test_shim_blocks_live_and_default_sockets():
    for cmd in ("tmux -L dockwright has-session -t mgr",
                "tmux -L claude-orch ls",
                "tmux ls"):
        r = _child_tmux(cmd)
        assert r.returncode == 97 and "BLOCKED" in r.stderr, cmd


def test_shim_names_mgr_on_live_but_allows_it_on_throwaway():
    # A mgr session on a pid-scoped throwaway server is isolated from the live
    # fleet, so the throwaway allowlist wins over the mgr guard — real_tmux tests
    # (test_manager_launch) create and switch a real mgr on their own socket.
    r = _child_tmux(f"tmux -L wt-iso-{os.getpid()}-x has-session -t mgr")
    assert r.returncode != 97 and "BLOCKED" not in r.stderr, r.stderr
    # On the live/default socket, mgr is the operator's manager: blocked, and the
    # message names mgr specifically (the incident's shape). has-session is
    # read-only, so this leg is safe even if the shim were ever off PATH.
    r = _child_tmux("tmux -L dockwright has-session -t mgr")
    assert r.returncode == 97 and "mgr" in r.stderr, r.stderr


def test_shim_allows_throwaway_socket():
    r = _child_tmux(f"tmux -L wt-iso-{os.getpid()}-shimcheck kill-server")
    assert r.returncode != 97 and "BLOCKED" not in r.stderr


def test_cli_agents_blocked():
    # claude/codex are not intercepted by no_live_tmux, so a direct call reaches
    # the PATH shim, which blocks unconditionally.
    for cli in ("claude", "codex"):
        r = subprocess.run([cli, "--version"], capture_output=True, text=True)
        assert r.returncode == 97 and "BLOCKED" in r.stderr, cli


def test_account_registry_path_is_hermetic():
    # In-process sibling of the PATH-shim boundary above: spawn_worker_impl calls
    # spawner.write_registry_snapshot(), which WRITES paths.ACCOUNT_REGISTRY. Left
    # resolving into the real ~/.claude, the ~20+ spawn tests clobber the operator's
    # live registry snapshot (2026-07-17). conftest's autouse hermetic patch must
    # redirect it to tmp — this guard fails RED without that patch.
    from pathlib import Path

    from dockwright import paths
    assert not str(paths.ACCOUNT_REGISTRY).startswith(str(Path.home() / ".claude")), (
        "paths.ACCOUNT_REGISTRY resolves into the real ~/.claude inside pytest — "
        "spawn tests will clobber the operator's live registry snapshot")


def test_account_state_paths_are_hermetic():
    # Same in-process boundary as ACCOUNT_REGISTRY, extended to the whole account
    # lane: the account picker/gate READ ACCOUNT_USAGE (a hot live 5h window paused
    # ~46 spawn tests, Tier-2 on PR #215), READ ACCOUNT_ACTIVE (the operator's live
    # pointer feature-gates the picker inside tests) and ACCOUNT_STATE (live bricks
    # skew selection), and spawn tests WRITE SPAWN_COUNTER (suite runs advanced the
    # fleet's round-robin to 920). conftest's autouse _no_live_account_state patch
    # must redirect all four to tmp — this guard fails RED (all four) without it.
    from pathlib import Path

    from dockwright import paths
    live_root = str(Path.home() / ".claude")
    for attr in ("ACCOUNT_USAGE", "ACCOUNT_ACTIVE", "ACCOUNT_STATE", "SPAWN_COUNTER"):
        assert not str(getattr(paths, attr)).startswith(live_root), (
            f"paths.{attr} resolves into the real ~/.claude inside pytest — spawn "
            f"tests read/write the operator's live account state (time-dependent green)")
