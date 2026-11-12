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
from dockwright.models import UnknownFamily, default_model, resolve_model

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "dockwright"
DEPLOY = REPO / "deploy"
SCRIPTS = DEPLOY / "scripts"
BOOTSTRAP = SCRIPTS / "bootstrap-recreate.sh"
STALE_MONITOR_PATH = SRC / "stale_monitor.py"

EXPLICIT_RE = re.compile(r"^claude-[a-z0-9.-]+$")
DISTILL_GRANDFATHER = "claude-sonnet-4-6"

RESOLVER_PROBE_ID = "claude-opus-5-probe[1m]"

HEADLESS_LIGHT_TOKEN = "@sonnet"


def resolver_stub(bin_dir, prints: str | None, token: str) -> None:
    stub = Path(bin_dir) / "dockwright"
    body = f"echo '{prints}'\n" if prints is not None else "exit 1\n"
    stub.write_text(
        "#!/bin/bash\n"
        '[ "$1" = model ] && [ "$2" = resolve ] || exit 2\n'
        f'if [ "$3" != {shlex.quote(token)} ]; then\n'
        f'  echo "resolver-stub: this lane is pinned to {token}; it asked for $3" >&2\n'
        "  exit 3\n"
        "fi\n"
        f"{body}")
    stub.chmod(0o755)


def _strip_1m(model: str) -> str:
    return model[:-4] if model.endswith("[1m]") else model


def _assert_explicit(model: str, where: str) -> None:
    try:
        resolved = resolve_model(model)
    except UnknownFamily as err:
        raise AssertionError(f"{where}: --model {model!r} is not a resolvable token ({err})")
    base = _strip_1m(resolved)
    assert EXPLICIT_RE.match(base), (
        f"{where}: --model {model!r} is not an explicit claude-* id — bare "
        f"aliases silently change meaning across releases (model rule 1)")
    assert not base.endswith("-latest"), (
        f"{where}: --model {model!r} is a floating -latest alias — same drift "
        f"class as a bare alias")


def _bootstrap_tokens(tmp_path, with_settings: bool, config_path=None) -> list[str]:
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
    if config_path is not None:
        env[config.ENV_CONFIG_PATH] = str(config_path)
    r = subprocess.run(
        ["bash", str(BOOTSTRAP), "--narrative", "probe", "--from-sid",
         "sid-x", "--dry-run"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    cmd = next(l for l in r.stdout.splitlines() if "cmd=[" in l)
    tokens = shlex.split(cmd.split("cmd=[", 1)[1].rsplit("]", 1)[0])
    if with_settings:
        assert "--settings" in tokens, "settings branch not taken — harness bug"
    else:
        assert "--settings" not in tokens, "no-settings branch not taken — harness bug"
    return tokens


def _model_of(tokens: list[str]) -> str:
    assert tokens.count("--model") == 1, f"want exactly one --model: {tokens}"
    return tokens[tokens.index("--model") + 1]


@pytest.mark.parametrize("with_settings", [True, False],
                         ids=["settings-branch", "no-settings-branch"])
def test_bootstrap_recreate_lane(tmp_path, with_settings):
    model = _model_of(_bootstrap_tokens(tmp_path, with_settings))
    _assert_explicit(model, "bootstrap-recreate.sh RUNTIME_CMD")


def _recovery_launch(monkeypatch, tmp_path):
    import importlib.util
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "stale_monitor_pin_guard", STALE_MONITOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "_account_effective", lambda letter: ("a", None))
    monkeypatch.setattr(mod, "_resolve_model_token", resolve_model)
    mod.manager_fallback_model.cache_clear()
    captured = {}

    class FakeDrv:
        async def spawn(self, **kw):
            captured.update(kw)
            return "%9"

    monkeypatch.setattr(mod, "_get_driver", lambda: FakeDrv())
    mod._launch_recovery_manager({"cwd": "/c", "name": "m"}, "sid-1", "a")
    return mod, captured["argv"][-1]


def _recovery_inner(monkeypatch, tmp_path) -> str:
    return _recovery_launch(monkeypatch, tmp_path)[1]


def test_stale_monitor_recovery_lane(monkeypatch, tmp_path):
    inner = _recovery_inner(monkeypatch, tmp_path)
    toks = shlex.split(inner)
    assert "--model" in toks, f"no --model in recovery inner cmd: {inner}"
    model = toks[toks.index("--model") + 1]
    _assert_explicit(model, "stale_monitor._launch_recovery_manager")


def _effort_of(tokens: list[str]) -> str | None:
    return tokens[tokens.index("--effort") + 1] if "--effort" in tokens else None


MANAGER_CONFIGS = {
    "no-config": (None, default_model("manager_model"), config.DEFAULT_MANAGER_EFFORT),
    "configured-pair": ('[spawn]\nmanager_model = "@sonnet"\nmanager_effort = "high"\n',
                        "@sonnet", "high"),
    "empty-effort": ('[spawn]\nmanager_effort = ""\n', default_model("manager_model"), None),
}


@pytest.mark.parametrize("toml,model_token,effort", list(MANAGER_CONFIGS.values()),
                         ids=list(MANAGER_CONFIGS))
def test_bootstrap_launches_on_the_configured_manager_pair(
        monkeypatch, tmp_path, toml, model_token, effort):
    cfg = tmp_path / "dockwright.toml"
    if toml is not None:
        cfg.write_text(toml)
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "no-presets")
    script = _bootstrap_tokens(tmp_path / "sh", True, cfg)
    assert script.count("--effort") <= 1, f"more than one --effort: {script}"
    lanes = {
        "manager_launch.manager_launch_args": manager_launch.manager_launch_args(None),
        "bootstrap-recreate.sh": script,
    }
    want = (resolve_model(model_token), effort)
    off = {name: (_model_of(t), _effort_of(t)) for name, t in lanes.items()
           if (_model_of(t), _effort_of(t)) != want}
    assert not off, (
        f"every manager lane must launch on [spawn] manager_model / manager_effort "
        f"{want}; these disagreed: {off}")


def test_takeover_lane_launches_on_the_rescue_target_pair(monkeypatch, tmp_path):
    mod, inner = _recovery_launch(monkeypatch, tmp_path)
    toks = shlex.split(inner)
    assert toks[toks.index("--model") + 1] == mod.manager_fallback_model()
    assert _effort_of(toks) == mod.MANAGER_MODEL_FALLBACK_EFFORT, (
        "takeover-recovery runs with no human present and the credit-fallback cannot "
        "rescue it before it registers, so it launches on the rescue target's model "
        "AND that target's effort, not on MGR_LANES")


def test_manager_launch_default_lane(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(tmp_path / "absent.toml"))
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "no-presets")
    argv = manager_launch._runtime_argv()
    model = argv[argv.index("--model") + 1]
    _assert_explicit(model, "manager_launch._runtime_argv ([defaults] manager_model)")


def test_default_toml_template_lane():
    spawn = tomllib.loads(config.DEFAULT_TOML)["spawn"]
    _assert_explicit(spawn["manager_model"], "DEFAULT_TOML manager_model")
    assert spawn["distill_model"] == DISTILL_GRANDFATHER, (
        "DEFAULT_TOML distill_model moved off the grandfathered "
        f"{DISTILL_GRANDFATHER} — pick a pinned model "
        "and update this guard deliberately")
    _assert_explicit(spawn["distill_model"], "DEFAULT_TOML distill_model")


def test_distill_default_lane(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(tmp_path / "absent.toml"))
    model = config.distill_model()
    assert model == DISTILL_GRANDFATHER, (
        f"distill default moved to {model!r} — grandfather is exact; revisit "
        f"the pins and this guard together")
    _assert_explicit(model, "config.distill_model()")


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
    from dockwright import paths
    role = tmp_path / "roles" / "Blank"
    role.mkdir(parents=True)
    (role / "role.toml").write_text('core = "Blank"\nlaunch = ["lane"]\n')
    (role / "role.lane.md").write_text("<!-- dockwright-role: Blank launch=lane -->\n")
    (role / "role.settings.json").write_text("{}")
    monkeypatch.setattr(paths, "ROLES_DIR", tmp_path / "roles")
    distill._distill_manager_session("sid-pin-guard")
    argv = captured["argv"]
    assert argv[argv.index("--append-system-prompt-file") + 1] == str(role / "role.lane.md")
    assert "--settings" not in argv and "--agents" not in argv
    assert "--model" in argv, f"no --model in distill argv: {argv}"
    model = argv[argv.index("--model") + 1]
    assert model == DISTILL_GRANDFATHER, (
        f"distill spawns --model {model!r}; grandfather is exact "
        f"({DISTILL_GRANDFATHER}) — revisit the pins and this guard together")
    _assert_explicit(model, "distill._distill_manager_session argv")


def test_chat_scout_default_lane(model_pins):
    _assert_explicit(config.scout_model(), "config.scout_model()")


def test_chat_archive_spawn_lanes(monkeypatch, tmp_path, model_pins):
    from dockwright import chat_archive, paths
    from tests.lockdown_argv import permission_surface_widened
    cfg = tmp_path / "archive.toml"
    cfg.write_text('[chat_archive]\nenabled = true\nmcp_url = "https://chat.example/mcp"\nmcp_server = "chat"\n'
                   'keychain_service = "kc"\nself_user_id = "U1"\nwrite_tools = ["send"]\n[chat_archive.tools]\n'
                   'search = "s"\nread_conversation = "rc"\nread_thread = "rt"\nlist_conversations = "lc"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    captured = []

    def fake_run(argv, **kw):
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(chat_archive.shutil, "which", lambda name: "/bin/claude")
    role = tmp_path / "roles" / "Blank"
    role.mkdir(parents=True)
    (role / "role.toml").write_text('core = "Blank"\nlaunch = ["lane"]\n')
    (role / "role.lane.md").write_text("<!-- dockwright-role: Blank launch=lane -->\n")
    (role / "role.settings.json").write_text("{}")
    monkeypatch.setattr(paths, "ROLES_DIR", tmp_path / "roles")
    chat_archive.DirectHand(config.chat_archive(), run=fake_run).nudge()
    with pytest.raises(chat_archive.HandError):
        chat_archive.ClaudeHand(config.chat_archive(), run=fake_run).call("search", {})
    assert len(captured) == 2
    for argv, name in zip(captured, ("DirectHand.nudge argv", "ClaudeHand argv")):
        assert "--model" in argv, f"no --model in {name}: {argv}"
        _assert_explicit(argv[argv.index("--model") + 1], name)
        assert argv[argv.index("--append-system-prompt-file") + 1] == str(role / "role.lane.md")
        assert "--settings" not in argv and "--agents" not in argv
        assert permission_surface_widened(argv) == []


def test_distill_falls_back_to_generic_worker_lane(monkeypatch, tmp_path, capsys):
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
    from dockwright import paths
    role = tmp_path / "roles" / "GenericWorker"
    role.mkdir(parents=True)
    (role / "role.toml").write_text('core = "GenericWorker"\nlaunch = ["lane"]\n')
    (role / "role.lane.md").write_text("<!-- dockwright-role: GenericWorker launch=lane -->\n")
    (role / "role.settings.json").write_text("{}")
    monkeypatch.setattr(paths, "ROLES_DIR", tmp_path / "roles")
    distill._distill_manager_session("sid-pin-guard")
    argv = captured["argv"]
    assert argv[argv.index("--append-system-prompt-file") + 1] == str(role / "role.lane.md")
    err = capsys.readouterr().err
    assert "manager-memory: role Blank unavailable (" in err
    assert "distill uses the GenericWorker lane render" in err
