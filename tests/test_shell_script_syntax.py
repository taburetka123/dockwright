import shutil
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "deploy" / "scripts"
SELFFIX_RUN = SCRIPTS / "selffix-run.sh"
BOOTSTRAP_RECREATE = SCRIPTS / "bootstrap-recreate.sh"


def test_selffix_run_syntax_ok():
    assert shutil.which("bash")
    r = subprocess.run(["bash", "-n", str(SELFFIX_RUN)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_bootstrap_recreate_syntax_ok():
    assert shutil.which("bash")
    r = subprocess.run(["bash", "-n", str(BOOTSTRAP_RECREATE)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
