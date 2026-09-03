import json
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from dockwright import config, manager_launch, spawner

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "dockwright"
DEPLOY = REPO / "deploy"
SCRIPTS = DEPLOY / "scripts"
ROSTER_PATH = Path.home() / ".claude" / "rules" / "sdd-model-tiers.md"
BOOTSTRAP = SCRIPTS / "bootstrap-recreate.sh"
STALE_MONITOR_PATH = SRC / "stale_monitor.py"

EXPLICIT_RE = re.compile(r"^claude-[a-z0-9.-]+$")
DISTILL_GRANDFATHER = "claude-sonnet-4-6"


def _strip_1m(model: str) -> str:
    return model[:-4] if model.endswith("[1m]") else model


def _assert_explicit(model: str, where: str, roster: set[str] | None) -> None:
    base = _strip_1m(model)
    assert EXPLICIT_RE.match(base), (
        f"{where}: --model {model!r} is not an explicit claude-* id — bare "
        f"aliases silently change meaning across releases (roster rule 1)")
    assert not base.endswith("-latest"), (
        f"{where}: --model {model!r} is a floating -latest alias — same drift "
        f"class as a bare alias")
    if roster is not None:
        assert base in roster, (
            f"{where}: --model {model!r} is not in the MODEL ROSTER table "
            f"({ROSTER_PATH}); either the roster moved on and this site was "
            f"missed, or a new pin needs a roster decision first")


def _bootstrap_model(tmp_path, with_settings: bool) -> str:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(parents=True)
    (fakebin / "tmux").write_text(
        "#!/bin/bash\ncase \"$*\" in *has-session*) exit 1 ;; esac\nexit 0\n")
    (fakebin / "tmux").chmod(0o755)
    (fakebin / "jq").symlink_to(shutil.which("jq"))
    (fakebin / "uuidgen").symlink_to(shutil.which("uuidgen"))
    home = tmp_path / "home"
    active = home / ".claude" / "dockwright" / "active"
    active.mkdir(parents=True)
    (active / "sid-x.json").write_text(json.dumps(
        {"claude_sid": "sid-x", "agent": "manager", "name": "mighty-demon",
         "domain": "personal", "pid": 4242}))
    if with_settings:
        presets = home / ".claude" / "dockwright" / "presets"
        presets.mkdir(parents=True)
        (presets / "manager-settings.json").write_text("{}")
    env = {**os.environ, "HOME": str(home),
           "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}"}
    env.pop("DOCKWRIGHT_MANAGER_RC", None)
    env.pop("DOCKWRIGHT_MANAGER_SKIP_PERMS", None)
    r = subprocess.run(
        ["bash", str(BOOTSTRAP), "--narrative", "probe", "--from-sid",
         "sid-x", "--dry-run"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    cmd = next(l for l in r.stdout.splitlines() if "cmd=[" in l)
    if with_settings:
        assert "--settings" in cmd, "settings branch not taken — harness bug"
    else:
        assert "--settings" not in cmd, "no-settings branch not taken — harness bug"
    m = re.search(r"--model '([^']+)'", cmd)
    assert m, f"no quoted --model value in dry-run cmd: {cmd}"
    return m.group(1)


@pytest.mark.parametrize("with_settings", [True, False],
                         ids=["settings-branch", "no-settings-branch"])
def test_bootstrap_recreate_lane(tmp_path, with_settings):
    model = _bootstrap_model(tmp_path, with_settings)
    _assert_explicit(model, "bootstrap-recreate.sh RUNTIME_CMD", None)


def test_stale_monitor_recovery_lane(monkeypatch, tmp_path):
    import importlib.util
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "stale_monitor_pin_guard", STALE_MONITOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "_account_config_prefix", lambda letter: "")
    captured = {}

    class FakeDrv:
        async def spawn(self, **kw):
            captured.update(kw)
            return "%9"

    monkeypatch.setattr(mod, "_get_driver", lambda: FakeDrv())
    mod._launch_recovery_manager({"cwd": "/c", "name": "m"}, "sid-1", "a")
    inner = captured["argv"][-1]
    toks = shlex.split(inner)
    assert "--model" in toks, f"no --model in recovery inner cmd: {inner}"
    model = toks[toks.index("--model") + 1]
    _assert_explicit(model, "stale_monitor._launch_recovery_manager", None)


def test_manager_launch_default_lane(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(tmp_path / "absent.toml"))
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "no-presets")
    argv = manager_launch._runtime_argv()
    model = argv[argv.index("--model") + 1]
    _assert_explicit(model, "manager_launch._runtime_argv (DEFAULT_MANAGER_MODEL)",
                     None)


def test_spawner_worker_fallback_lane(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(tmp_path / "absent.toml"))
    cmd = spawner._runtime_command("claude", "hi", None, None)
    toks = shlex.split(cmd)
    model = toks[toks.index("--model") + 1]
    _assert_explicit(model, "spawner._runtime_command (DEFAULT_WORKER_MODEL)",
                     None)


def test_default_toml_template_lane():
    spawn = tomllib.loads(config.DEFAULT_TOML)["spawn"]
    _assert_explicit(spawn["worker_model"], "DEFAULT_TOML worker_model", None)
    _assert_explicit(spawn["manager_model"], "DEFAULT_TOML manager_model", None)
    assert spawn["distill_model"] == DISTILL_GRANDFATHER, (
        "DEFAULT_TOML distill_model moved off the grandfathered "
        f"{DISTILL_GRANDFATHER} — pick a roster model (or add a roster row) "
        "and update this guard deliberately")
    _assert_explicit(spawn["distill_model"], "DEFAULT_TOML distill_model", None)


def test_distill_default_lane(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(tmp_path / "absent.toml"))
    model = config.distill_model()
    assert model == DISTILL_GRANDFATHER, (
        f"distill default moved to {model!r} — grandfather is exact; revisit "
        f"the roster and this guard together")
    _assert_explicit(model, "config.distill_model()", None)


def test_distill_spawn_lane(monkeypatch, tmp_path):
    from dockwright import distill
    log = tmp_path / "t.jsonl"
    log.write_text(
        '{"type": "assistant", "message": {"content": '
        '[{"type": "text", "text": "ok"}]}}\n'
    )
    monkeypatch.setattr(distill, "find_session_log", lambda sid: log)
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(tmp_path / "absent.toml"))
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout=b"distilled", stderr=b"")

    monkeypatch.setattr(distill.subprocess, "run", fake_run)
    distill._distill_manager_session("sid-pin-guard")
    argv = captured["argv"]
    assert "--model" in argv, f"no --model in distill argv: {argv}"
    model = argv[argv.index("--model") + 1]
    assert model == DISTILL_GRANDFATHER, (
        f"distill spawns --model {model!r}; grandfather is exact "
        f"({DISTILL_GRANDFATHER}) — revisit the roster and this guard together")
    _assert_explicit(model, "distill._distill_manager_session argv", None)
