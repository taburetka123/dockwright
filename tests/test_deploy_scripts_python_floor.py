import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SYSTEM_PYTHON = Path("/usr/bin/python3")
LOADER = """
import importlib.machinery, importlib.util, os, sys
path = sys.argv[1]
sys.path.insert(0, os.path.dirname(path))
name = "_floor_" + os.path.basename(path).replace(".", "_").replace("-", "_")
loader = importlib.machinery.SourceFileLoader(name, path)
spec = importlib.util.spec_from_file_location(name, path, loader=loader)
module = importlib.util.module_from_spec(spec)
sys.modules[name] = module
loader.exec_module(module)
"""


def _shipped_python_scripts():
    scripts = sorted(p for p in (REPO / "deploy" / "scripts").glob("*.py") if not p.name.startswith("test_"))
    scripts += sorted((REPO / "deploy" / "tmux").glob("*.py"))
    scripts.append(REPO / "src" / "dockwright" / "stale_monitor.py")
    return scripts


@pytest.mark.skipif(not SYSTEM_PYTHON.exists(), reason="no system python3")
@pytest.mark.parametrize("script", _shipped_python_scripts(), ids=lambda p: p.relative_to(REPO).as_posix())
def test_shipped_script_imports_under_the_system_python3(script, tmp_path):
    r = subprocess.run([str(SYSTEM_PYTHON), "-B", "-c", LOADER, str(script)],
                       capture_output=True, text=True, env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}, timeout=60)
    assert r.returncode == 0, r.stderr
