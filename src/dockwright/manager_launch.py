"""`dockwright manager` — one-command manager launch.

Encapsulates the quickstart dance: dedicated tmux server (`-L <socket>`) born
with the deployed conf, running `claude` with `/manager` as the initial
prompt. `-f` is read only at server birth, so a bare-born server (e.g. a
manual `tmux -L dockwright new-session` predating this subcommand) needs a
best-effort `source-file` before a fresh `new-session` lands on it. Re-running
against an existing `mgr` session reattaches instead of spawning a second
manager: `new-window` from outside tmux creates the window server-side and
exits immediately, leaving the caller in a bare shell while a second
/manager claude runs invisibly. From INSIDE a window of the dockwright server
the command switches the client to `mgr` (creating it detached first when
missing) — tmux's non-nesting jump; inside a FOREIGN tmux server it refuses,
because there is no client on the dockwright server to switch.
"""
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
    """True when $TMUX points at OUR server. Field 1 of $TMUX is the socket
    path, realpath'd by tmux (macOS /tmp -> /private/tmp), so both sides are
    realpath-normalized before comparing."""
    sock = tmux_env.split(",", 1)[0]
    tmpdir = os.environ.get("TMUX_TMPDIR") or "/tmp"
    expected = os.path.join(tmpdir, f"tmux-{os.getuid()}", _socket())
    return os.path.realpath(sock) == os.path.realpath(expected)


def _switch_from_inside() -> int:
    """Non-nesting jump to the manager session from a window of our own
    server. An attached new-session cannot be issued from inside tmux, so a
    missing mgr is created detached and then switched to — same end state as
    the outside path's attach. The issuing client resolves via the inherited
    $TMUX."""
    if not _has_mgr_session():
        # Server alive by definition (we are inside it), so -f on new-session
        # would be a no-op: source the conf like the outside bare-server branch.
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
    """Shared argv tail for every manager `claude` launch — fresh boot here,
    recreate lane in mcp_server.spawn_replacement_manager_impl — so the two
    importable paths cannot drift. bootstrap-recreate.sh and
    stale_monitor._launch_recovery_manager are standalone by design and
    compose this same tail inline; keep all four in sync.

    --settings: E2E F-2 — the deployed manager allowlist takes a fresh boot
    from ~11 approval prompts to ~0. Absent file (setup.sh not run) = old
    behavior.
    --remote-control: default-ON by design and NOT gated on the settings
    file — the global remoteControlAtStartup key is unreliable for a spawned
    session; the flag is the reliable enrollment path (anthropics/claude-code
    #54527/#29929/#41036). DOCKWRIGHT_MANAGER_RC=0 opts out (public-operator
    escape hatch: flag behavior on an RC-unavailable account is unverified).

    ORDER IS LOAD-BEARING: `--remote-control [name]` takes an OPTIONAL value,
    so a following non-dash token (the trailing `/manager*` prompt) is bound
    as the RC session NAME and the prompt is lost — the manager then boots a
    bare session that never enters manager mode. `--remote-control` is
    therefore emitted FIRST, and callers keep `--model` (a required-value
    flag) between this tail and the trailing prompt, so the token after
    `--remote-control` is always another option. Do NOT move the prompt
    adjacent to `--remote-control`. (Verified: `claude -p --remote-control
    "/x"` errors "Input must be provided"; with `--model` interposed the
    prompt survives.)
    """
    args: list[str] = []
    if os.environ.get("DOCKWRIGHT_MANAGER_RC", "").strip() != "0":
        args.append("--remote-control")
    # OPT-IN, default OFF — DOCKWRIGHT_MANAGER_SKIP_PERMS=1 (strict) removes
    # the Bash safety classifier for the launched manager. Sanctioned ONLY for
    # manager.core.md's two named uses (classifier outage; sandbox-E2E/publish
    # host DRIVER); never a routine mode. Independent of the RC gate above —
    # RC=0 + skip=1 must still emit. Bare flag: parse-safe anywhere before the
    # trailing prompt; placed here it also keeps the token after
    # --remote-control a dash-option. One-shot: launch call sites scrub the
    # var from the environment after composing argv (_scrub_skip_perms_env),
    # so a tmux server born by this launch cannot inherit it and turn the
    # opt-in into a sticky mode. Set it per-invocation; never export it in a
    # shell profile — the recreate lanes run `-ic` shells that source rc
    # files, so a profile export re-enters the environment past the scrub.
    if os.environ.get("DOCKWRIGHT_MANAGER_SKIP_PERMS", "").strip() == "1":
        args.append("--dangerously-skip-permissions")
    settings = paths.PRESETS / "manager-settings.json"
    if settings.is_file():
        args += ["--settings", str(settings)]
    return args


def _scrub_skip_perms_env() -> None:
    """One-shot guarantee for DOCKWRIGHT_MANAGER_SKIP_PERMS: tmux windows
    inherit the SERVER's environment, so a server born with the var would feed
    "1" to the manager claude process, its MCP server, and thus
    manager_claude_args() on every later recreate. Call AFTER argv composition
    (the flag is already baked into the argv), immediately before a tmux
    invocation that can birth the server."""
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
        # -f on this new-session call is a no-op against a live server, so
        # source the conf now or the new manager window never gets the 2-row
        # status bar. Never source when no server is up: source-file would
        # spin up a conf-less server of its own.
        _source_conf_best_effort()
    # L-11: running `dockwright manager` from this directory is deliberate
    # consent — pre-trust it so the boot never re-prompts (interactive accepts
    # don't persist; file-written flags do).
    trust.pretrust_dir(os.getcwd())
    cmd = build_command()
    _scrub_skip_perms_env()
    try:
        os.execvp(cmd[0], cmd)
    except OSError as e:
        print(f"dockwright manager: failed to exec tmux: {e}", file=sys.stderr)
        return 1
