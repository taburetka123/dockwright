import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _env(tmp_path, path):
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
    repo = _minimal_repo(tmp_path, ">=99.0")
    path = f"{Path(sys.executable).parent}:/usr/bin:/bin"
    r = subprocess.run(["bash", str(repo / "setup.sh")],
                       env=_env(tmp_path, path), capture_output=True,
                       text=True, cwd=str(repo), timeout=180)
    assert r.returncode == 1
    assert "requires Python >= 99.0" in r.stderr
    assert "brew install python@3.13" in r.stderr
    assert "pyenv" in r.stderr
    assert not (repo / ".venv").exists()


def test_range_spec_floor_is_parsed_not_defaulted(tmp_path):
    repo = _minimal_repo(tmp_path, ">=99.0,<100")
    path = f"{Path(sys.executable).parent}:/usr/bin:/bin"
    r = subprocess.run([shutil.which("bash"), str(repo / "setup.sh")],
                       env=_env(tmp_path, path), capture_output=True,
                       text=True, cwd=str(repo), timeout=180)
    assert r.returncode == 1
    assert "requires Python >= 99.0" in r.stderr


def test_missing_python3_fails_with_clear_error(tmp_path):
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
    repo = _minimal_repo(tmp_path, ">=3.0")
    path = f"{Path(sys.executable).parent}:/usr/bin:/bin"
    r = subprocess.run(["bash", str(repo / "setup.sh")],
                       env=_env(tmp_path, path), capture_output=True,
                       text=True, cwd=str(repo), timeout=180)
    assert "requires Python" not in r.stderr
    assert "python3 not found" not in r.stderr
    assert "cannot create virtualenvs" not in r.stderr


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
    repo = _full_tree(tmp_path)
    r = _run_full(tmp_path, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ERROR" not in r.stderr
    assert (repo / ".venv" / "bin" / "dockwright").exists()


def test_stale_venv_is_recreated(tmp_path):
    repo = _full_tree(tmp_path)
    vbin = repo / ".venv" / "bin"
    vbin.mkdir(parents=True)
    old = vbin / "python"
    old.write_text("#!/bin/sh\nexit 1\n")
    old.chmod(0o755)
    r = _run_full(tmp_path, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "stale or broken" in r.stdout
    assert (repo / ".venv" / "bin" / "python").read_text() != "#!/bin/sh\nexit 1\n"
    assert (repo / ".venv" / "bin" / "dockwright").exists()


def test_healthy_venv_is_not_recreated(tmp_path):
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


STUB_PYTHON_NO_ENSUREPIP = """#!/bin/sh
if [ "$1" = "-c" ]; then
    case "$2" in
        *ensurepip*) echo "No module named 'ensurepip'" >&2; exit 1 ;;
        *print*version_info*) echo "3.14"; exit 0 ;;
    esac
fi
exit 0
"""


def _run_full_no_ensurepip(tmp_path, repo):
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    stub = stub_bin / "python3"
    stub.write_text(STUB_PYTHON_NO_ENSUREPIP)
    stub.chmod(0o755)
    path = f"{stub_bin}:/usr/bin:/bin"
    return subprocess.run([shutil.which("bash"), str(repo / "setup.sh")],
                          env=_env(tmp_path, path), capture_output=True,
                          text=True, cwd=str(repo), timeout=180)


def test_missing_ensurepip_fails_fast_before_venv_create(tmp_path):
    repo = _full_tree(tmp_path)
    r = _run_full_no_ensurepip(tmp_path, repo)
    assert r.returncode == 1
    assert "cannot create virtualenvs" in r.stderr
    assert "apt install python3.14-venv" in r.stderr
    assert not (repo / ".venv").exists()


def test_missing_ensurepip_with_healthy_venv_still_passes(tmp_path):
    repo = _full_tree(tmp_path)
    vbin = repo / ".venv" / "bin"
    vbin.mkdir(parents=True)
    for name in ("python", "pip", "dockwright"):
        f = vbin / name
        f.write_text("#!/bin/sh\nexit 0\n")
        f.chmod(0o755)
    r = _run_full_no_ensurepip(tmp_path, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "cannot create virtualenvs" not in r.stderr


def test_missing_ensurepip_with_stale_venv_fails_without_deleting_it(tmp_path):
    repo = _full_tree(tmp_path)
    vbin = repo / ".venv" / "bin"
    vbin.mkdir(parents=True)
    old = vbin / "python"
    old.write_text("#!/bin/sh\nexit 1\n")
    old.chmod(0o755)
    sentinel = repo / ".venv" / "sentinel"
    sentinel.write_text("keep me\n")
    r = _run_full_no_ensurepip(tmp_path, repo)
    assert r.returncode == 1
    assert "cannot create virtualenvs" in r.stderr
    assert "recreating" not in r.stdout
    assert sentinel.read_text() == "keep me\n"
