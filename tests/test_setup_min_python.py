"""setup.sh fails fast on a missing/too-old python3 (E2E finding I-1) and
self-recovers a stale/broken .venv (N-6) — macOS rc.3 fixes.

All hermetic: tmp HOME/CLAUDE_DIR/CODEX_DIR, pinned PATH, stub interpreters;
no real-machine mutation. The fail-fast tests use a REAL python against an
impossible floor (">=99.0") so the actual code path runs unstubbed.
test_adequate_python_passes_the_check runs a real `pip install -e` that may
reach PyPI when online — its assertions are offline-robust either way, so
that one test's network use doesn't affect the outcome."""
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _env(tmp_path, path):
    """Hermetic setup.sh env. Fresh dict (not os.environ): scrubs operator
    config discovery; ALLOW_WORKTREE=1 keeps the linked-worktree self-anchor
    from redirecting a tmp copy to the operator's real main clone."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "PATH": path,
        "CLAUDE_DIR": str(tmp_path / "claude"),
        "CODEX_DIR": str(tmp_path / "codex"),
        "DOCKWRIGHT_SETUP_ALLOW_WORKTREE": "1",
    }


def _minimal_repo(tmp_path, requires_python):
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy(REPO / "setup.sh", repo / "setup.sh")
    (repo / "pyproject.toml").write_text(
        f'[project]\nrequires-python = "{requires_python}"\n')
    return repo


def test_too_old_python_fails_fast_with_actionable_message(tmp_path):
    """A python3 below the pyproject floor must die BEFORE venv/pip with a
    dockwright-level message, not pip's raw PEP-660 error (A0 verbatim)."""
    repo = _minimal_repo(tmp_path, ">=99.0")
    # Deterministic real python3: the running interpreter's bin dir first.
    path = f"{Path(sys.executable).parent}:/usr/bin:/bin"
    r = subprocess.run(["bash", str(repo / "setup.sh")],
                       env=_env(tmp_path, path), capture_output=True,
                       text=True, cwd=str(repo), timeout=180)
    assert r.returncode == 1
    assert "requires Python >= 99.0" in r.stderr
    assert "brew install python@3.13" in r.stderr   # macOS hint
    assert "pyenv" in r.stderr                      # linux hint
    assert not (repo / ".venv").exists()            # fail-fast: no mutation


def test_range_spec_floor_is_parsed_not_defaulted(tmp_path):
    """A range spec (">=X,<Y") must parse the floor, not silently fall back
    to 3.11 — the fallback would let a future floor bump silently regress to
    the raw pip error this check exists to prevent."""
    repo = _minimal_repo(tmp_path, ">=99.0,<100")
    path = f"{Path(sys.executable).parent}:/usr/bin:/bin"
    r = subprocess.run([shutil.which("bash"), str(repo / "setup.sh")],
                       env=_env(tmp_path, path), capture_output=True,
                       text=True, cwd=str(repo), timeout=180)
    assert r.returncode == 1
    assert "requires Python >= 99.0" in r.stderr


def test_missing_python3_fails_with_clear_error(tmp_path):
    """No python3 on PATH at all (genuinely fresh box) → clear error, not a
    bash 'command not found'. PATH holds ONLY the pre-check tools.

    The bash interpreter itself is resolved via shutil.which() here (the test
    harness's own ambient PATH) rather than left as a bare "bash" argv[0]:
    subprocess.run's executable lookup uses the *given* env's PATH (confirmed
    via os.get_exec_path semantics), so a bare "bash" would fail to launch at
    all against a PATH containing only dirname/sed/head — before setup.sh's
    own shell logic ever runs. Resolving it here keeps the child's PATH true
    to "holds ONLY the pre-check tools" for the script's internal lookups."""
    repo = _minimal_repo(tmp_path, ">=3.11")
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    for tool in ("dirname", "sed", "head"):
        (stub_bin / tool).symlink_to(shutil.which(tool))
    r = subprocess.run([shutil.which("bash"), str(repo / "setup.sh")],
                       env=_env(tmp_path, str(stub_bin)), capture_output=True,
                       text=True, cwd=str(repo), timeout=180)
    assert r.returncode == 1
    assert "ERROR: python3 not found on PATH" in r.stderr
    assert "brew install python@3.13" in r.stderr


def test_adequate_python_passes_the_check(tmp_path):
    """A real python3 >= the real floor sails past the check (it must fail
    LATER, at the deploy copies the minimal repo lacks — never with the
    min-python error). Full happy-path rc==0 is Task 2's full-tree test."""
    repo = _minimal_repo(tmp_path, ">=3.0")
    path = f"{Path(sys.executable).parent}:/usr/bin:/bin"
    r = subprocess.run(["bash", str(repo / "setup.sh")],
                       env=_env(tmp_path, path), capture_output=True,
                       text=True, cwd=str(repo), timeout=180)
    assert "requires Python" not in r.stderr
    assert "python3 not found" not in r.stderr


# --- N-6: stale/broken .venv self-recovery (full-tree hermetic runs) --------

# Fake adequate python3: -c probes pass; -m venv fabricates a minimal venv
# (python -> copy of itself, pip/dockwright exit-0 stubs); EVERYTHING else
# (pip install/uninstall, ensurepip, stamp_provenance one-liners) exits 0 —
# that default is what lets the whole non-FILES_ONLY setup.sh path run
# hermetically: claude/codex are absent from PATH so MCP/hook registration
# degrades to the existing skip/warn branches, and every binary-driven
# transform no-ops through the fabricated .venv/bin/dockwright stub.
STUB_PYTHON = """#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
    mkdir -p "$3/bin"
    cp "$0" "$3/bin/python"
    printf '#!/bin/sh\\nexit 0\\n' > "$3/bin/pip"
    printf '#!/bin/sh\\nexit 0\\n' > "$3/bin/dockwright"
    chmod +x "$3/bin/python" "$3/bin/pip" "$3/bin/dockwright"
fi
exit 0
"""


def _full_tree(tmp_path):
    """Copy of the real repo tree. EXCLUDES .git (this checkout is a linked
    worktree — with .git present and no ALLOW_WORKTREE the self-anchor would
    redirect the run to the operator's REAL main clone) and .venv (huge, and
    the object under test)."""
    repo = tmp_path / "repo"
    shutil.copytree(REPO, repo, ignore=shutil.ignore_patterns(
        ".git", ".venv", "__pycache__", ".pytest_cache", "node_modules"))
    return repo


def _stub_bin(tmp_path):
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    stub = stub_bin / "python3"
    stub.write_text(STUB_PYTHON)
    stub.chmod(0o755)
    return stub_bin


def _run_full(tmp_path, repo):
    path = f"{_stub_bin(tmp_path)}:/usr/bin:/bin"
    return subprocess.run([shutil.which("bash"), str(repo / "setup.sh")],
                          env=_env(tmp_path, path), capture_output=True,
                          text=True, cwd=str(repo), timeout=180)


def test_full_tree_setup_completes_with_adequate_python(tmp_path):
    """Happy path end-to-end: adequate (stub) python, no pre-existing venv →
    rc 0 and the fabricated venv is in place. Proves the new checks add no
    failure to a healthy install on either platform."""
    repo = _full_tree(tmp_path)
    r = _run_full(tmp_path, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ERROR" not in r.stderr
    assert (repo / ".venv" / "bin" / "dockwright").exists()


def test_stale_venv_is_recreated(tmp_path):
    """The N-6 trap: a .venv whose python fails the version probe (built by an
    old python / interpreter broken) must be recreated, not reused into the
    identical failure."""
    repo = _full_tree(tmp_path)
    vbin = repo / ".venv" / "bin"
    vbin.mkdir(parents=True)
    old = vbin / "python"
    old.write_text("#!/bin/sh\nexit 1\n")   # fails every version probe
    old.chmod(0o755)
    r = _run_full(tmp_path, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "stale or broken" in r.stdout
    # Recreated: the old always-fail interpreter is gone, stub venv in place.
    assert (repo / ".venv" / "bin" / "python").read_text() != "#!/bin/sh\nexit 1\n"
    assert (repo / ".venv" / "bin" / "dockwright").exists()


def test_healthy_venv_is_not_recreated(tmp_path):
    """Idempotency guard: a venv whose python passes the probe is NEVER
    destroyed (the operator's real .venv on every re-run). Needs pip and
    dockwright stubs too — setup.sh pip-installs into and hard-requires
    .venv/bin/dockwright from an existing venv."""
    repo = _full_tree(tmp_path)
    vbin = repo / ".venv" / "bin"
    vbin.mkdir(parents=True)
    for name in ("python", "pip", "dockwright"):
        f = vbin / name
        f.write_text("#!/bin/sh\nexit 0\n")
        f.chmod(0o755)
    sentinel = repo / ".venv" / "sentinel"
    sentinel.write_text("keep me\n")
    r = _run_full(tmp_path, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "recreating" not in r.stdout
    assert sentinel.read_text() == "keep me\n"
