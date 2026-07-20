"""Model pins on headless claude spawns.

~/.claude/settings.json's global model default is user-owned and can move to a
2x-price tier at any time; a `claude` spawn without an explicit --model
silently inherits it. The canon scripts must pin their lane's model
(orch-audit model-allocation: retros/digests -> sonnet-5). Repo copies under
deploy/scripts/ are the source of truth; setup.sh deploys them.
"""
import shutil
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "deploy" / "scripts"
SELFFIX_RUN = SCRIPTS / "selffix-run.sh"
GARDENER_RUN = SCRIPTS / "gardener-run.sh"
BOOTSTRAP_RECREATE = SCRIPTS / "bootstrap-recreate.sh"


def test_selffix_retro_pins_sonnet5():
    src = SELFFIX_RUN.read_text()
    assert "--model claude-sonnet-5" in src


def test_gardener_pins_sonnet5_on_both_spawn_paths():
    src = GARDENER_RUN.read_text()
    assert src.count("--model claude-sonnet-5") == 2


def test_gardener_inner_cmd_pin_precedes_prompt_arg():
    # INNER_CMD is a single-line string; the flag must land before the
    # positional prompt or claude treats it as prompt text.
    src = GARDENER_RUN.read_text()
    inner = next(l for l in src.splitlines() if l.startswith("INNER_CMD="))
    assert "--model claude-sonnet-5" in inner
    assert inner.index("--model claude-sonnet-5") < inner.index("cat ")


def test_selffix_run_syntax_ok():
    assert shutil.which("bash")
    r = subprocess.run(["bash", "-n", str(SELFFIX_RUN)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_bootstrap_recreate_pins_manager_opus():
    # Manager lane (orch-audit model-allocation): recreate must never inherit
    # the user's interactive default. Quoted so zsh -ic can't glob the [1m].
    # F-2 split RUNTIME_CMD into a --settings / no-settings if-else pair — both
    # branches must still pin opus[1m] before /manager-resume.
    src = BOOTSTRAP_RECREATE.read_text()
    runtime_cmds = [l for l in src.splitlines()
                    if l.strip().startswith("RUNTIME_CMD=")]
    assert runtime_cmds, "no RUNTIME_CMD assignment found"
    for runtime_cmd in runtime_cmds:
        assert "--model 'opus[1m]'" in runtime_cmd
        assert runtime_cmd.index("--model") < runtime_cmd.index("/manager-resume")


def test_bootstrap_recreate_syntax_ok():
    assert shutil.which("bash")
    r = subprocess.run(["bash", "-n", str(BOOTSTRAP_RECREATE)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
