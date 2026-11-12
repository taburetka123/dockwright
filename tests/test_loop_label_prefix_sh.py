import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "scripts" / "loop-label-prefix.sh"


def _deployed_copy(tmp_path, label_prefix):
    home = tmp_path / "home"
    scripts = home / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(SCRIPT, scripts / "loop-label-prefix.sh")
    (home / ".claude" / "dockwright.toml").write_text(f'[loops]\nlabel_prefix = "{label_prefix}"\n')
    return home, scripts / "loop-label-prefix.sh"


def _prefix_under(python3, tmp_path, home, script):
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "python3").symlink_to(python3)
    r = subprocess.run(["bash", "-c", f"source '{script}'; dockwright_loop_label_prefix"],
                       env={"HOME": str(home), "PATH": f"{fakebin}:/usr/bin:/bin"},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="no system python3")
def test_deployed_copy_reads_label_prefix_under_the_system_python3(tmp_path):
    home, script = _deployed_copy(tmp_path, "com.example")
    assert _prefix_under("/usr/bin/python3", tmp_path, home, script) == "com.example"


def test_deployed_copy_reads_label_prefix_under_the_test_python(tmp_path):
    home, script = _deployed_copy(tmp_path, "com.example")
    assert _prefix_under(sys.executable, tmp_path, home, script) == "com.example"
