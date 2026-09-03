from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from . import config, paths, trust
from .terminal import TmuxDriver, MANAGER_SESSION


def _socket() -> str:
    return TmuxDriver().socket()


def _conf() -> Path | None:
    return TmuxDriver()._resolve_conf()


def _model() -> str:
    return config.manager_model()


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
        trust.pretrust_dir(os.getcwd())
        argv = _runtime_argv()
        _scrub_skip_perms_env()
        try:
            proc = subprocess.run(
                ["tmux", "-L", _socket(), "new-session", "-d", "-s", MANAGER_SESSION,
                 "--", *argv],
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


def manager_claude_args() -> list[str]:
    args: list[str] = []
    if os.environ.get("DOCKWRIGHT_MANAGER_RC", "").strip() != "0":
        args.append("--remote-control")
    if os.environ.get("DOCKWRIGHT_MANAGER_SKIP_PERMS", "") == "1":
        args.append("--dangerously-skip-permissions")
    settings = paths.PRESETS / "manager-settings.json"
    if settings.is_file():
        args += ["--settings", str(settings)]
    return args


def _scrub_skip_perms_env() -> None:
    os.environ.pop("DOCKWRIGHT_MANAGER_SKIP_PERMS", None)


def _runtime_argv() -> list[str]:
    return ["claude", *manager_claude_args(), "--model", _model(), "/manager"]


def build_command() -> list[str]:
    if _has_mgr_session():
        return ["tmux", "-L", _socket(), "attach-session", "-t", MANAGER_SESSION]
    conf = _conf()
    conf_args = ["-f", str(conf)] if conf is not None else []
    return ["tmux", "-L", _socket(), *conf_args,
            "new-session", "-s", MANAGER_SESSION, "--", *_runtime_argv()]


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
    elif _server_alive() and _conf() is not None:
        _source_conf_best_effort()
    trust.pretrust_dir(os.getcwd())
    cmd = build_command()
    _scrub_skip_perms_env()
    try:
        os.execvp(cmd[0], cmd)
    except OSError as e:
        print(f"dockwright manager: failed to exec tmux: {e}", file=sys.stderr)
        return 1
