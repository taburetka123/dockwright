from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from . import config, paths, trust
from .denied_tools import disallowed_tools_arg
from .terminal import TmuxDriver, MANAGER_SESSION


def _socket() -> str:
    return TmuxDriver().socket()


def _conf() -> Path | None:
    return TmuxDriver()._resolve_conf()


def _model() -> str:
    return config.manager_model()


def _effort() -> str:
    return config.manager_effort()


def _has_mgr_session() -> bool:
    try:
        proc = subprocess.run(
            ["tmux", "-L", _socket(), "has-session", "-t", MANAGER_SESSION],
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _server_alive() -> bool:
    try:
        proc = subprocess.run(
            ["tmux", "-L", _socket(), "list-sessions"],
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _inside_dockwright_server(tmux_env: str) -> bool:
    sock = tmux_env.split(",", 1)[0]
    tmpdir = os.environ.get("TMUX_TMPDIR") or "/tmp"
    expected = os.path.join(tmpdir, f"tmux-{os.getuid()}", _socket())
    return os.path.realpath(sock) == os.path.realpath(expected)


def _switch_from_inside() -> int:
    if not _has_mgr_session():
        _source_conf_best_effort()
        home = _pretrusted_manager_home()
        if home is None:
            return 1
        argv = _runtime_argv()
        _scrub_skip_perms_env()
        try:
            proc = subprocess.run(
                ["tmux", "-L", _socket(), "new-session", "-d", "-s", MANAGER_SESSION,
                 "-c", str(home), "--", *argv],
                capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"dockwright manager: failed to create manager session: {e}",
                  file=sys.stderr)
            return 1
        if proc.returncode != 0:
            print("dockwright manager: failed to create manager session: "
                  f"{proc.stderr.strip()}", file=sys.stderr)
            return 1
    try:
        proc = subprocess.run(
            ["tmux", "-L", _socket(), "switch-client", "-t", MANAGER_SESSION],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"dockwright manager: switch-client failed: {e}", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        print(f"dockwright manager: switch-client failed: {proc.stderr.strip()}",
              file=sys.stderr)
        return 1
    return 0


MANAGER_SETTINGS_PRESET = "manager-settings.json"
MANAGER_RC_ARGS_USAGE = "usage: dockwright manager-rc-args [--account NAME]"
MANAGER_LAUNCH_ARGS_USAGE = "usage: dockwright manager-launch-args [--account NAME]"
_FROM_POINTER = object()


def _pool_entry(name: str) -> dict | None:
    accounts = config.load().get("accounts")
    pool = accounts.get("pool") if isinstance(accounts, dict) else None
    if not isinstance(pool, list):
        return None
    for entry in pool:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return None


def _account_rc_on(account: str | None) -> bool:
    if config.load_error() is not None:
        return False
    name = account if account is not None else config.default_account()
    entry = _pool_entry(name)
    return entry is None or entry.get("manager_remote_control", True) is True


def _preset_settings(preset: Path) -> dict:
    try:
        data = json.loads(preset.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _manager_role() -> tuple[str, dict, str] | None:
    from . import roles
    from .profiles import profile_for
    try:
        profile = profile_for(paths.manager_home())
        files = roles.launch_files("Manager", "manager", profile=profile)
        layer = None if files is None else roles.read_role_layer(files[1])
    except roles.RoleError as err:
        print(f"dockwright: manager launches without its role: {err}", file=sys.stderr)
        return None
    if files is None:
        return None
    return str(files[0]), layer, profile


def manager_rc_args(account: str | None) -> list[str]:
    return [*_rc_and_settings_args(account), disallowed_tools_arg(manager=True)]


def _rc_and_settings_args(account: str | None) -> list[str]:
    from . import roles
    preset = paths.PRESETS / MANAGER_SETTINGS_PRESET
    role = _manager_role()
    env_on = os.environ.get("DOCKWRIGHT_MANAGER_RC", "").strip() != "0"
    if env_on and _account_rc_on(account):
        if role is not None:
            merged = roles.apply_layer(_preset_settings(preset), role[1])
            try:
                settings = roles.write_spawn_settings(merged)
            except OSError as err:
                print(f"dockwright: manager launches without its role: {err}", file=sys.stderr)
            else:
                return ["--remote-control", "--append-system-prompt-file", role[0], "--settings", str(settings),
                        *roles.agents_arg(role[2])]
        if preset.is_file():
            return ["--remote-control", "--settings", str(preset)]
        return ["--remote-control"]
    base = _preset_settings(preset)
    if role is not None:
        base = roles.apply_layer(base, role[1])
    return [*(["--append-system-prompt-file", role[0]] if role is not None else []),
            "--settings", json.dumps({**base, "remoteControlAtStartup": False}),
            *(roles.agents_arg(role[2]) if role is not None else [])]


def _pointer_account() -> str | None:
    from .spawner import _active_account
    return _active_account()


def manager_claude_args(account: "str | None | object" = _FROM_POINTER) -> list[str]:
    if account is _FROM_POINTER:
        account = _pointer_account()
    args = manager_rc_args(account)
    if os.environ.get("DOCKWRIGHT_MANAGER_SKIP_PERMS", "") == "1":
        args.append("--dangerously-skip-permissions")
    effort = _effort().strip()
    if effort:
        args += ["--effort", effort]
    return args


def _account_argv(argv: list[str]) -> tuple[bool, str | None]:
    if not argv:
        return True, None
    if len(argv) == 2 and argv[0] == "--account" and argv[1]:
        return True, argv[1]
    return False, None


def manager_rc_args_main(argv: list[str]) -> int:
    ok, account = _account_argv(argv)
    if not ok:
        print(MANAGER_RC_ARGS_USAGE, file=sys.stderr)
        return 2
    print(shlex.join(manager_rc_args(account)))
    return 0


def manager_launch_args(account: str | None) -> list[str]:
    return [*manager_claude_args(account), "--model", _model()]


def manager_launch_args_main(argv: list[str]) -> int:
    from .models import UnknownFamily
    ok, account = _account_argv(argv)
    if not ok:
        print(MANAGER_LAUNCH_ARGS_USAGE, file=sys.stderr)
        return 2
    try:
        args = manager_launch_args(account)
    except UnknownFamily as err:
        print(str(err), file=sys.stderr)
        return 1
    print(shlex.join(args))
    return 0


def _scrub_skip_perms_env() -> None:
    os.environ.pop("DOCKWRIGHT_MANAGER_SKIP_PERMS", None)


def _runtime_argv() -> list[str]:
    return [str(paths.dockwright_bin()), "launch", *manager_launch_args(None), "/manager"]


def _pretrusted_manager_home() -> Path | None:
    try:
        home = paths.ensure_manager_home()
    except ValueError as err:
        print(f"dockwright manager: refusing to launch: {err}", file=sys.stderr)
        return None
    trust.pretrust_dir(home)
    return home


def build_command(home: Path) -> list[str]:
    conf = _conf()
    conf_args = ["-f", str(conf)] if conf is not None else []
    return ["tmux", "-L", _socket(), *conf_args,
            "new-session", "-s", MANAGER_SESSION, "-c", str(home),
            "--", *_runtime_argv()]


def _source_conf_best_effort() -> None:
    conf = _conf()
    if conf is None:
        return
    try:
        subprocess.run(["tmux", "-L", _socket(), "source-file", str(conf)],
                       capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dockwright manager",
        description="Launch the dockwright tmux server and start a claude "
                    "session in /manager mode, or reattach to the existing "
                    "manager session if one is already running.",
    )
    parser.parse_args(argv)
    tmux_env = os.environ.get("TMUX")
    if tmux_env:
        if _inside_dockwright_server(tmux_env):
            return _switch_from_inside()
        print("dockwright manager: inside a different tmux server — run it "
              "from a plain terminal, or from a window on the dockwright "
              "server (where it switches in place).", file=sys.stderr)
        return 2
    if _has_mgr_session():
        print("dockwright manager: attaching to existing manager session",
              file=sys.stderr)
        cmd = ["tmux", "-L", _socket(), "attach-session", "-t", MANAGER_SESSION]
    else:
        if _server_alive() and _conf() is not None:
            _source_conf_best_effort()
        home = _pretrusted_manager_home()
        if home is None:
            return 1
        cmd = build_command(home)
    _scrub_skip_perms_env()
    try:
        os.execvp(cmd[0], cmd)
    except OSError as e:
        print(f"dockwright manager: failed to exec tmux: {e}", file=sys.stderr)
        return 1
