from pathlib import Path
import pytest
from dockwright import manager_launch

import os


def _assert_rc_parse_safe(argv):
    """--remote-control [name] binds a following NON-dash token as the RC
    session name; the trailing /manager* prompt must never sit there. Guards
    the parsed shape, which exact-argv asserts alone cannot (a reorder
    re-greens them, broken or not)."""
    if "--remote-control" in argv:
        i = argv.index("--remote-control")
        assert i + 1 < len(argv), f"--remote-control is last in {argv!r}"
        assert argv[i + 1].startswith("-"), \
            f"--remote-control followed by non-option {argv[i + 1]!r} in {argv!r}"


class _Result:
    def __init__(self, rc=0, stderr=""):
        self.returncode = rc
        self.stderr = stderr


def _inside_env(monkeypatch, tmp_path, sock="dockwright"):
    """Set $TMUX to the exact path tmux would use for socket `sock` under a
    private TMUX_TMPDIR, so the test controls both sides of the same-server
    comparison."""
    tmpdir = tmp_path / "tmuxtmp"
    (tmpdir / f"tmux-{os.getuid()}").mkdir(parents=True)
    monkeypatch.setenv("TMUX_TMPDIR", str(tmpdir))
    monkeypatch.setenv("TMUX", f"{tmpdir}/tmux-{os.getuid()}/{sock},123,0")


def test_build_command_fresh_new_session_with_conf(monkeypatch, tmp_path):
    conf = tmp_path / "dockwright.tmux.conf"
    conf.write_text("# conf")
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")
    monkeypatch.setattr(manager_launch, "_conf", lambda: conf)
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: False)
    cmd = manager_launch.build_command()
    assert cmd == ["tmux", "-L", "dockwright", "-f", str(conf),
                   "new-session", "-s", "mgr", "--",
                   "claude", "--remote-control", "--model", "opus[1m]", "/manager"]


def test_build_command_without_conf_omits_dash_f(monkeypatch):
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")
    monkeypatch.setattr(manager_launch, "_conf", lambda: None)
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: False)
    cmd = manager_launch.build_command()
    assert "-f" not in cmd


def test_build_command_existing_mgr_session_attaches(monkeypatch):
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")
    monkeypatch.setattr(manager_launch, "_conf", lambda: None)
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: True)
    cmd = manager_launch.build_command()
    assert cmd == ["tmux", "-L", "dockwright", "attach-session", "-t", "mgr"]


def test_runtime_argv_appends_manager_settings_when_deployed(monkeypatch, tmp_path):
    presets = tmp_path / "presets"; presets.mkdir()
    settings = presets / "manager-settings.json"
    settings.write_text("{}")
    monkeypatch.setattr(manager_launch.paths, "PRESETS", presets)
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    argv = manager_launch._runtime_argv()
    assert argv == ["claude", "--remote-control",
                    "--settings", str(settings), "--model", "opus[1m]", "/manager"]
    _assert_rc_parse_safe(argv)


def test_runtime_argv_no_settings_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "nope")
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    assert manager_launch._runtime_argv() == [
        "claude", "--remote-control", "--model", "opus[1m]", "/manager"]
    _assert_rc_parse_safe(manager_launch._runtime_argv())


def test_runtime_argv_rc_opt_out(monkeypatch, tmp_path):
    # DOCKWRIGHT_MANAGER_RC=0 is the public-operator escape hatch (spec
    # Decision 2): RC behavior on an RC-unavailable account is unspiked, so
    # the flag must be omittable without a code change.
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_RC", "0")
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "nope")
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    assert manager_launch._runtime_argv() == ["claude", "--model", "opus[1m]", "/manager"]
    _assert_rc_parse_safe(manager_launch._runtime_argv())


def test_manager_claude_args_rc_then_settings(monkeypatch, tmp_path):
    presets = tmp_path / "presets"; presets.mkdir()
    settings = presets / "manager-settings.json"
    settings.write_text("{}")
    monkeypatch.setattr(manager_launch.paths, "PRESETS", presets)
    assert manager_launch.manager_claude_args() == [
        "--remote-control", "--settings", str(settings)]


def test_main_refuses_inside_foreign_tmux_without_tmux_calls(monkeypatch, capsys):
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    monkeypatch.setenv("TMUX", "/tmp/some-other-server,1,0")
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")

    def _no_run(cmd, **kwargs):
        raise AssertionError(f"no tmux call expected on the refusal path: {cmd}")

    monkeypatch.setattr(manager_launch.subprocess, "run", _no_run)
    assert manager_launch.main([]) == 2
    assert "different tmux server" in capsys.readouterr().err


def test_main_switches_inside_dockwright_server_with_existing_mgr(monkeypatch, tmp_path):
    _inside_env(monkeypatch, tmp_path)
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: True)
    calls = []

    def _run(cmd, **kwargs):
        calls.append([str(a) for a in cmd])
        return _Result()

    monkeypatch.setattr(manager_launch.subprocess, "run", _run)
    monkeypatch.setattr(manager_launch.os, "execvp",
                        lambda prog, argv: (_ for _ in ()).throw(AssertionError("must not exec")))
    assert manager_launch.main([]) == 0
    assert calls == [["tmux", "-L", "dockwright", "switch-client", "-t", "mgr"]]


def test_main_creates_detached_then_switches_when_mgr_missing(monkeypatch, tmp_path):
    _inside_env(monkeypatch, tmp_path)
    conf = tmp_path / "dockwright.tmux.conf"
    conf.write_text("# conf")
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")
    monkeypatch.setattr(manager_launch, "_conf", lambda: conf)
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: False)
    pretrusted = []
    monkeypatch.setattr(manager_launch.trust, "pretrust_dir", lambda d: pretrusted.append(d))
    calls = []

    def _run(cmd, **kwargs):
        calls.append([str(a) for a in cmd])
        return _Result()

    monkeypatch.setattr(manager_launch.subprocess, "run", _run)
    assert manager_launch.main([]) == 0
    assert calls == [
        ["tmux", "-L", "dockwright", "source-file", str(conf)],
        ["tmux", "-L", "dockwright", "new-session", "-d", "-s", "mgr", "--",
         "claude", "--remote-control", "--model", "opus[1m]", "/manager"],
        ["tmux", "-L", "dockwright", "switch-client", "-t", "mgr"],
    ]
    assert pretrusted == [manager_launch.os.getcwd()]


def test_inside_dockwright_server_realpath_normalizes_symlinked_tmpdir(monkeypatch, tmp_path):
    # macOS: tmux realpaths the socket dir (/tmp -> /private/tmp), so $TMUX
    # carries the resolved path while TMUX_TMPDIR may be the symlinked one.
    real = tmp_path / "real"
    (real / f"tmux-{os.getuid()}").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv("TMUX_TMPDIR", str(link))
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")
    tmux_env = f"{real}/tmux-{os.getuid()}/dockwright,9,0"
    assert manager_launch._inside_dockwright_server(tmux_env) is True


def test_main_switch_client_failure_returns_1(monkeypatch, tmp_path, capsys):
    _inside_env(monkeypatch, tmp_path)
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: True)
    monkeypatch.setattr(manager_launch.subprocess, "run",
                        lambda cmd, **kw: _Result(rc=1, stderr="no current client"))
    assert manager_launch.main([]) == 1
    assert "switch-client failed" in capsys.readouterr().err


def test_main_new_session_failure_skips_switch(monkeypatch, tmp_path, capsys):
    _inside_env(monkeypatch, tmp_path)
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")
    monkeypatch.setattr(manager_launch, "_conf", lambda: None)
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: False)
    monkeypatch.setattr(manager_launch.trust, "pretrust_dir", lambda d: None)
    calls = []

    def _run(cmd, **kwargs):
        calls.append([str(a) for a in cmd])
        return _Result(rc=1, stderr="boom")

    monkeypatch.setattr(manager_launch.subprocess, "run", _run)
    assert manager_launch.main([]) == 1
    assert not any("switch-client" in c for c in calls)
    assert "failed to create manager session" in capsys.readouterr().err


def test_main_sources_conf_before_new_session_on_bare_alive_server(monkeypatch, tmp_path):
    conf = tmp_path / "dockwright.tmux.conf"
    conf.write_text("# conf")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")
    monkeypatch.setattr(manager_launch, "_conf", lambda: conf)
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: False)
    monkeypatch.setattr(manager_launch, "_server_alive", lambda: True)

    run_calls = []

    def _fake_run(cmd, **kwargs):
        run_calls.append(cmd)
        class _Result:
            returncode = 0
        return _Result()

    monkeypatch.setattr(manager_launch.subprocess, "run", _fake_run)

    exec_calls = []
    monkeypatch.setattr(manager_launch.os, "execvp",
                         lambda prog, cmd: exec_calls.append(cmd))

    manager_launch.main([])

    assert ["tmux", "-L", "dockwright", "source-file", str(conf)] in run_calls
    assert exec_calls == [["tmux", "-L", "dockwright", "-f", str(conf),
                            "new-session", "-s", "mgr", "--",
                            "claude", "--remote-control", "--model", "opus[1m]", "/manager"]]


def test_main_attaches_to_existing_mgr_session_without_sourcing(monkeypatch, capsys):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")
    monkeypatch.setattr(manager_launch, "_conf", lambda: None)
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: True)

    def _unexpected_run(cmd, **kwargs):
        raise AssertionError(f"subprocess.run should not be called: {cmd}")

    monkeypatch.setattr(manager_launch.subprocess, "run", _unexpected_run)

    exec_calls = []
    monkeypatch.setattr(manager_launch.os, "execvp",
                         lambda prog, cmd: exec_calls.append(cmd))

    manager_launch.main([])

    assert exec_calls == [["tmux", "-L", "dockwright", "attach-session", "-t", "mgr"]]
    assert "attaching to existing manager session" in capsys.readouterr().err


def test_main_pretrusts_launch_cwd(monkeypatch, tmp_path):
    import json
    import os as _os
    from pathlib import Path
    from dockwright import manager_launch, trust
    cfg = tmp_path / "cfg.json"
    monkeypatch.setattr(trust, "_default_config_json", lambda: cfg)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(manager_launch.os, "execvp", lambda prog, argv: None)
    manager_launch.main([])
    data = json.loads(cfg.read_text())
    key = str(Path(_os.getcwd()).resolve())
    assert data["projects"][key]["hasTrustDialogAccepted"] is True


@pytest.mark.real_tmux
def test_live_inside_tmux_creates_mgr_detached_and_switches(monkeypatch, tmp_path, real_tmux):
    """F-1 end-to-end on a real throwaway server: `dockwright manager` typed in
    a window of the dockwright server creates mgr DETACHED (claude shimmed via
    PATH) and switch-clients the pty client onto it. The test process itself
    never passes `-t mgr`/`-s mgr` to tmux (conftest hard-fails that in
    real_tmux tests) — the child process inside the pane does, unpatched, on
    the throwaway socket."""
    import fcntl
    import pty
    import select
    import struct
    import subprocess
    import sys
    import termios
    import time

    sock = real_tmux
    home = tmp_path / "home"
    home.mkdir()
    shim = tmp_path / "shim"
    shim.mkdir()
    claude = shim / "claude"
    claude.write_text("#!/bin/sh\nsleep 300\n")
    claude.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    subprocess.run(["tmux", "-L", sock, "new-session", "-d", "-s", "alpha",
                    "-x", "120", "-y", "30", "/bin/sh"], check=True)

    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
        os.environ["TERM"] = "xterm-256color"
        os.environ.pop("TMUX", None)
        os.environ.pop("TMUX_PANE", None)
        os.environ["HOME"] = str(home)
        os.execvp("tmux", ["tmux", "-L", sock, "attach", "-t", "alpha"])
        os._exit(127)
    os.set_blocking(fd, False)

    def drain(secs):
        end = time.time() + secs
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                try:
                    if not os.read(fd, 65536):
                        return
                except OSError:
                    return

    def sessions():
        return subprocess.run(
            ["tmux", "-L", sock, "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True).stdout.split()

    def client_session():
        return subprocess.run(
            ["tmux", "-L", sock, "list-clients", "-F", "#{session_name}"],
            capture_output=True, text=True).stdout.strip()

    ok = False
    try:
        drain(1.5)
        assert client_session() == "alpha"
        cmd = (f"DOCKWRIGHT_TMUX_SOCKET={sock} PATH={shim}:$PATH HOME={home} "
               f"{sys.executable} -m dockwright manager")
        subprocess.run(["tmux", "-L", sock, "send-keys", "-t", "alpha", cmd, "Enter"],
                       check=True)
        deadline = time.time() + 15
        while time.time() < deadline:
            drain(0.2)
            if "mgr" in sessions() and client_session() == "mgr":
                ok = True
                break
    finally:
        os.kill(pid, 9)
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)

    assert ok, (f"expected the client switched onto a freshly created mgr session; "
                f"sessions={sessions()!r}")


def test_manager_claude_args_skip_perms_opt_in(monkeypatch, tmp_path):
    presets = tmp_path / "presets"; presets.mkdir()
    settings = presets / "manager-settings.json"
    settings.write_text("{}")
    monkeypatch.setattr(manager_launch.paths, "PRESETS", presets)
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_SKIP_PERMS", "1")
    assert manager_launch.manager_claude_args() == [
        "--remote-control", "--dangerously-skip-permissions",
        "--settings", str(settings)]


@pytest.mark.parametrize("value", ["", "0", "true", "yes"])
def test_manager_claude_args_skip_perms_strict_opt_in(monkeypatch, tmp_path, value):
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "nope")
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_SKIP_PERMS", value)
    assert "--dangerously-skip-permissions" not in manager_launch.manager_claude_args()


def test_manager_claude_args_skip_perms_independent_of_rc(monkeypatch, tmp_path):
    # RC=0 + skip=1 (public RC-unavailable operator, sanctioned driver run)
    # must still emit — guards against nesting the skip append inside the RC
    # branch, which every other test in this file would fail to catch.
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_RC", "0")
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_SKIP_PERMS", "1")
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "nope")
    assert manager_launch.manager_claude_args() == ["--dangerously-skip-permissions"]


def test_runtime_argv_skip_perms_parse_safe(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_SKIP_PERMS", "1")
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "nope")
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    argv = manager_launch._runtime_argv()
    assert argv == ["claude", "--remote-control", "--dangerously-skip-permissions",
                    "--model", "opus[1m]", "/manager"]
    _assert_rc_parse_safe(argv)


def test_main_execvp_scrubs_env_after_argv_composition(monkeypatch, tmp_path):
    """Server-birth stickiness guard (spec § Server-birth scrub): execvp'd tmux
    inherits os.environ — a server born with the var would hand it to every
    future window, making the opt-in sticky across recreates. The composed argv
    must still carry the one-shot flag (pop-before-compose would silently
    disable the feature)."""
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: False)
    monkeypatch.setattr(manager_launch, "_server_alive", lambda: False)
    monkeypatch.setattr(manager_launch, "_conf", lambda: None)
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "nope")
    monkeypatch.setattr(manager_launch.trust, "pretrust_dir", lambda d: None)
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_SKIP_PERMS", "1")
    captured = {}

    def fake_execvp(prog, args):
        captured["argv"] = list(args)
        captured["env_still_has_var"] = "DOCKWRIGHT_MANAGER_SKIP_PERMS" in os.environ

    monkeypatch.setattr(manager_launch.os, "execvp", fake_execvp)
    manager_launch.main([])
    assert "--dangerously-skip-permissions" in captured["argv"], captured
    assert captured["env_still_has_var"] is False, \
        "the tmux server would inherit the var and make the opt-in sticky"


def test_switch_from_inside_scrubs_env_after_argv_composition(monkeypatch, tmp_path):
    _inside_env(monkeypatch, tmp_path)
    conf = tmp_path / "dockwright.tmux.conf"
    conf.write_text("# conf")
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")
    monkeypatch.setattr(manager_launch, "_conf", lambda: conf)
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: False)
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "nope")
    monkeypatch.setattr(manager_launch.trust, "pretrust_dir", lambda d: None)
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_SKIP_PERMS", "1")
    seen = []

    def _run(cmd, **kwargs):
        seen.append(([str(a) for a in cmd],
                     "DOCKWRIGHT_MANAGER_SKIP_PERMS" in os.environ))
        return _Result()

    monkeypatch.setattr(manager_launch.subprocess, "run", _run)
    assert manager_launch.main([]) == 0
    new_session_cmd, env_had_var = next(
        (c, has) for c, has in seen if "new-session" in c)
    assert "--dangerously-skip-permissions" in new_session_cmd
    assert env_had_var is False
