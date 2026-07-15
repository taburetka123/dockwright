"""`dockwright manager` — one-command manager launch.

Encapsulates the quickstart dance: dedicated tmux server (`-L <socket>`) born
with the deployed conf, running `claude` with `/manager` as the initial
prompt. `-f` is read only at server birth, so a bare-born server (e.g. a
manual `tmux -L dockwright new-session` predating this subcommand) needs a
best-effort `source-file` before a fresh `new-session` lands on it. Re-running
against an existing `mgr` session reattaches instead of spawning a second
manager: `new-window` from outside tmux creates the window server-side and
exits immediately, leaving the caller in a bare shell while a second
/manager claude runs invisibly.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from . import config
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


def _runtime_argv() -> list[str]:
    return ["claude", "--model", _model(), "/manager"]


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
    if os.environ.get("TMUX"):
        print("dockwright manager: already inside tmux — run it from a plain "
              "terminal, or open a new window here and run `claude` + "
              "`/manager` yourself.", file=sys.stderr)
        return 2
    if _has_mgr_session():
        print("dockwright manager: attaching to existing manager session",
              file=sys.stderr)
    elif _server_alive() and _conf() is not None:
        # -f on this new-session call is a no-op against a live server, so
        # source the conf now or the new manager window never gets the 2-row
        # status bar. Never source when no server is up: source-file would
        # spin up a conf-less server of its own.
        _source_conf_best_effort()
    cmd = build_command()
    try:
        os.execvp(cmd[0], cmd)
    except OSError as e:
        print(f"dockwright manager: failed to exec tmux: {e}", file=sys.stderr)
        return 1
