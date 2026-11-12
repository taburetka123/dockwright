import asyncio
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from dockwright import config, manager_launch, paths, spawner
from dockwright.models import resolve_model

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "scripts" / "bootstrap-recreate.sh"
STALE_MONITOR_PATH = REPO / "src" / "dockwright" / "stale_monitor.py"

POOL_TOML = """\
[accounts]
default = "a"
[[accounts.pool]]
name = "a"
[[accounts.pool]]
name = "b"
manager_remote_control = false
"""

REGISTRY = {"version": 1, "default": "a",
            "pool": [{"name": "a", "config_dir": None},
                     {"name": "b", "config_dir": None}]}

CASES = [("a", True, "on"), ("b", True, "off"), ("b", False, "on")]
IDS = ["a", "b-healthy-farm", "b-unhealthy-farm-falls-back-to-a"]


@pytest.fixture
def pool(monkeypatch, tmp_path):
    toml = tmp_path / "dockwright.toml"
    toml.write_text(POOL_TOML)
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(toml))
    return toml


def rc_state(tokens: list[str]) -> str:
    at = [i for i, t in enumerate(tokens) if t == "--settings"]
    assert len(at) <= 1, tokens
    settings = tokens[at[0] + 1] if at else None
    if "--remote-control" in tokens:
        assert tokens[tokens.index("--remote-control") + 1].startswith("-"), tokens
        assert settings is None or not settings.startswith("{"), tokens
        return "on"
    assert settings is not None and settings.startswith("{"), tokens
    assert json.loads(settings)["remoteControlAtStartup"] is False, tokens
    return "off"


def _farm(home: Path, healthy: bool) -> Path:
    farm = home / ".claude-b"
    farm.mkdir(parents=True, exist_ok=True)
    servers = {"dockwright": {}} if healthy else {}
    (farm / ".claude.json").write_text(json.dumps({"mcpServers": servers}))
    return farm


def _point(letter: str) -> None:
    paths.ACCOUNT_ACTIVE.parent.mkdir(parents=True, exist_ok=True)
    paths.ACCOUNT_ACTIVE.write_text(f"{letter}\n")


class _FakeDriver:
    def __init__(self):
        self.argv = None

    async def spawn(self, **kw):
        self.argv = kw["argv"]
        return "%1"


def spawn_manager_tokens(monkeypatch, tmp_path, letter, healthy):
    farm = _farm(tmp_path / "home", healthy)
    _point(letter)
    monkeypatch.setattr(spawner, "ensure_account_config_dir", lambda name: farm)
    monkeypatch.setattr(paths, "account_config_dir", lambda name: farm)
    monkeypatch.setattr(spawner.trust, "pretrust_dir", lambda *a, **k: None)
    drv = _FakeDriver()
    monkeypatch.setattr(spawner, "get_driver", lambda: drv)
    asyncio.run(spawner.spawn_worker_tab(
        cwd=str(tmp_path), initial_prompt="/manager-resume h1", name="m",
        agent="manager", runtime="claude", extra_args=["--model", "m1"]))
    return shlex.split(drv.argv[-1])


def lane_spawner(monkeypatch, tmp_path, letter, healthy):
    return rc_state(spawn_manager_tokens(monkeypatch, tmp_path, letter, healthy))


def lane_alias(monkeypatch, tmp_path, letter, healthy):
    _point(letter)
    return rc_state(["claude", *manager_launch.manager_claude_args(), "--model", "m1"])


def lane_bash(monkeypatch, tmp_path, letter, healthy):
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "tmux").write_text(
        "#!/bin/bash\ncase \"$*\" in *has-session*) exit 1 ;; esac\nexit 0\n")
    (fakebin / "tmux").chmod(0o755)
    (fakebin / "jq").symlink_to(shutil.which("jq"))
    (fakebin / "uuidgen").symlink_to(shutil.which("uuidgen"))
    home = tmp_path / "home"
    state = home / ".claude" / "dockwright"
    (state / "active").mkdir(parents=True)
    (state / "active" / "sid-x.json").write_text(json.dumps(
        {"claude_sid": "sid-x", "agent": "manager", "name": "mighty-demon",
         "domain": "personal", "pid": 4242}))
    (state / "account-active").write_text(f"{letter}\n")
    (state / "account-registry.json").write_text(json.dumps(REGISTRY))
    _farm(home, healthy)
    env = {**os.environ, "HOME": str(home),
           "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}"}
    for var in ("DOCKWRIGHT_MANAGER_RC", "DOCKWRIGHT_MANAGER_SKIP_PERMS", "DOCKWRIGHT_BIN"):
        env.pop(var, None)
    r = subprocess.run(
        ["bash", str(SCRIPT), "--narrative", "probe", "--from-sid", "sid-x", "--dry-run"],
        capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    line = next(l for l in r.stdout.splitlines() if "cmd=[" in l)
    return rc_state(shlex.split(line.split("cmd=[", 1)[1][:-1]))


def lane_stale_monitor(monkeypatch, tmp_path, letter, healthy):
    home = tmp_path / "home"
    _farm(home, healthy)
    monkeypatch.setenv("HOME", str(home))
    spec = importlib.util.spec_from_file_location("stale_monitor_rc_parity", STALE_MONITOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    registry = tmp_path / "account-registry.json"
    registry.write_text(json.dumps(REGISTRY))
    monkeypatch.setattr(mod, "ROOT", tmp_path / "sm")
    monkeypatch.setattr(mod, "ACCOUNT_REGISTRY", registry)
    monkeypatch.setattr(mod, "_resolve_model_token", resolve_model)
    mod.manager_fallback_model.cache_clear()
    drv = _FakeDriver()
    monkeypatch.setattr(mod, "_get_driver", lambda: drv)
    mod._launch_recovery_manager({"cwd": "/c", "name": "m"}, "sid-1", letter)
    return rc_state(shlex.split(drv.argv[-1]))


LANES = {"spawner": lane_spawner, "alias": lane_alias,
         "bash": lane_bash, "stale_monitor": lane_stale_monitor}


@pytest.mark.parametrize("letter,healthy,expected", CASES, ids=IDS)
def test_every_manager_launch_lane_gives_the_same_answer(
        pool, monkeypatch, tmp_path, letter, healthy, expected):
    lanes = {n: f for n, f in LANES.items()
             if not (n == "alias" and not healthy)}
    got = {}
    for name, lane in lanes.items():
        sub = tmp_path / name
        sub.mkdir()
        got[name] = lane(monkeypatch, sub, letter, healthy)
    assert got == {name: expected for name in lanes}


def test_alias_follows_the_pointer_and_misses_the_farm_fallback(pool):
    _point("b")
    assert rc_state(["claude", *manager_launch.manager_claude_args(), "--model", "m1"]) == "off"


def test_manager_spawn_adds_its_own_flags(pool, monkeypatch, tmp_path):
    presets = tmp_path / "presets"
    presets.mkdir()
    preset = presets / "manager-settings.json"
    preset.write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}))
    monkeypatch.setattr(paths, "PRESETS", presets)
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_SKIP_PERMS", "1")
    on = spawn_manager_tokens(monkeypatch, tmp_path / "on", "a", True)
    rc = on.index("--remote-control")
    assert on[rc:rc + 3] == ["--remote-control", "--settings", str(preset)]
    assert "--dangerously-skip-permissions" in on
    assert on[on.index("--effort") + 1] == config.manager_effort()
    assert on[on.index("--model") + 1] == "m1"
    off = spawn_manager_tokens(monkeypatch, tmp_path / "off", "b", True)
    assert "--remote-control" not in off
    assert json.loads(off[off.index("--settings") + 1]) == {
        "permissions": {"allow": ["Bash(ls:*)"]}, "remoteControlAtStartup": False}
