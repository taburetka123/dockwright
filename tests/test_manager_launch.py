import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
import pytest
from dockwright import config, manager_launch, paths

MGR_DENY = "--disallowedTools=Artifact,SendFeedback,Workflow,AskUserQuestion,ReportFindings"


def _assert_rc_parse_safe(argv):
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
    monkeypatch.setattr(manager_launch, "_effort", lambda: "high")
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: False)
    cmd = manager_launch.build_command(tmp_path / "mgr-home")
    assert cmd == ["tmux", "-L", "dockwright", "-f", str(conf),
                   "new-session", "-s", "mgr", "-c", str(tmp_path / "mgr-home"), "--",
                   str(paths.dockwright_bin()), "launch", "--remote-control", MGR_DENY, "--effort", "high", "--model", "opus[1m]", "/manager"]


def test_build_command_without_conf_omits_dash_f(monkeypatch, tmp_path):
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")
    monkeypatch.setattr(manager_launch, "_conf", lambda: None)
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    monkeypatch.setattr(manager_launch, "_effort", lambda: "high")
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: False)
    cmd = manager_launch.build_command(tmp_path / "mgr-home-default")
    assert "-f" not in cmd


def test_runtime_argv_appends_manager_settings_when_deployed(monkeypatch, tmp_path):
    presets = tmp_path / "presets"; presets.mkdir()
    settings = presets / "manager-settings.json"
    settings.write_text("{}")
    monkeypatch.setattr(manager_launch.paths, "PRESETS", presets)
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    monkeypatch.setattr(manager_launch, "_effort", lambda: "high")
    argv = manager_launch._runtime_argv()
    assert argv == [str(paths.dockwright_bin()), "launch", "--remote-control",
                    "--settings", str(settings), MGR_DENY, "--effort", "high", "--model", "opus[1m]", "/manager"]
    _assert_rc_parse_safe(argv)


def test_runtime_argv_no_settings_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "nope")
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    monkeypatch.setattr(manager_launch, "_effort", lambda: "high")
    assert manager_launch._runtime_argv() == [
        str(paths.dockwright_bin()), "launch", "--remote-control", MGR_DENY, "--effort", "high", "--model", "opus[1m]", "/manager"]
    _assert_rc_parse_safe(manager_launch._runtime_argv())


def test_runtime_argv_rc_opt_out(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_RC", "0")
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "nope")
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    monkeypatch.setattr(manager_launch, "_effort", lambda: "high")
    assert manager_launch._runtime_argv() == [
        str(paths.dockwright_bin()), "launch", "--settings", '{"remoteControlAtStartup": false}', MGR_DENY,
        "--effort", "high", "--model", "opus[1m]", "/manager"]
    _assert_rc_parse_safe(manager_launch._runtime_argv())


def test_manager_claude_args_rc_then_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(manager_launch, "_effort", lambda: "high")
    presets = tmp_path / "presets"; presets.mkdir()
    settings = presets / "manager-settings.json"
    settings.write_text("{}")
    monkeypatch.setattr(manager_launch.paths, "PRESETS", presets)
    assert manager_launch.manager_claude_args() == [
        "--remote-control", "--settings", str(settings), MGR_DENY, "--effort", "high"]


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
    monkeypatch.setattr(manager_launch, "_effort", lambda: "high")
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: False)
    mgr_home = tmp_path / "mgr-home"
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(mgr_home))
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
        ["tmux", "-L", "dockwright", "new-session", "-d", "-s", "mgr", "-c", str(mgr_home), "--",
         str(paths.dockwright_bin()), "launch", "--remote-control", MGR_DENY, "--effort", "high", "--model", "opus[1m]", "/manager"],
        ["tmux", "-L", "dockwright", "switch-client", "-t", "mgr"],
    ]
    assert pretrusted == [mgr_home]
    assert mgr_home.is_dir()


def test_inside_dockwright_server_realpath_normalizes_symlinked_tmpdir(monkeypatch, tmp_path):
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
    monkeypatch.setattr(manager_launch, "_effort", lambda: "high")
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
    monkeypatch.setattr(manager_launch, "_effort", lambda: "high")
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
                            "new-session", "-s", "mgr", "-c", str(tmp_path / "mgr-home-default"), "--",
                            str(paths.dockwright_bin()), "launch", "--remote-control", MGR_DENY, "--effort", "high", "--model", "opus[1m]", "/manager"]]


def test_main_attaches_to_existing_mgr_session_without_sourcing(monkeypatch, capsys):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")
    monkeypatch.setattr(manager_launch, "_conf", lambda: None)
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    monkeypatch.setattr(manager_launch, "_effort", lambda: "high")
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


def test_main_pretrusts_manager_home_not_launch_cwd(monkeypatch, tmp_path):
    import json
    import os as _os
    from pathlib import Path
    from dockwright import manager_launch, trust
    cfg = tmp_path / "cfg.json"
    mgr_home = tmp_path / "mgr-home"
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(mgr_home))
    monkeypatch.setattr(trust, "_default_config_json", lambda: cfg)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: False)
    monkeypatch.setattr(manager_launch, "_server_alive", lambda: False)
    execs = []
    monkeypatch.setattr(manager_launch.os, "execvp", lambda prog, argv: execs.append(argv))
    manager_launch.main([])
    data = json.loads(cfg.read_text())
    assert data["projects"][str(mgr_home.resolve())]["hasTrustDialogAccepted"] is True
    assert str(Path(_os.getcwd()).resolve()) not in data["projects"]
    assert mgr_home.is_dir()
    assert execs[0][execs[0].index("-c") + 1] == str(mgr_home)


def test_build_command_with_home_never_reads_the_session_or_the_default_home(monkeypatch, tmp_path):
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")
    monkeypatch.setattr(manager_launch, "_conf", lambda: None)
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    monkeypatch.setattr(manager_launch, "_effort", lambda: "high")
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: (_ for _ in ()).throw(AssertionError("session probed")))
    home = tmp_path / "given-home"
    home.mkdir()
    cmd = manager_launch.build_command(home)
    assert cmd[cmd.index("-c") + 1] == str(home)


def test_main_new_session_uses_the_home_it_ensured_even_if_the_session_state_flips(monkeypatch, tmp_path):
    from dockwright import manager_launch as ml
    mgr_home = tmp_path / "mgr-home"
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(mgr_home))
    monkeypatch.delenv("TMUX", raising=False)
    probes = iter([False, True, True])
    monkeypatch.setattr(ml, "_has_mgr_session", lambda: next(probes))
    monkeypatch.setattr(ml, "_server_alive", lambda: False)
    monkeypatch.setattr(ml, "_socket", lambda: "dockwright")
    monkeypatch.setattr(ml, "_conf", lambda: None)
    monkeypatch.setattr(ml.trust, "pretrust_dir", lambda d: True)
    execs = []
    monkeypatch.setattr(ml.os, "execvp", lambda prog, argv: execs.append(argv))
    ml.main([])
    assert "new-session" in execs[0]
    assert execs[0][execs[0].index("-c") + 1] == str(mgr_home)
    assert mgr_home.is_dir()


def test_main_refuses_to_launch_when_manager_home_cannot_be_created(monkeypatch, tmp_path, capsys):
    from dockwright import manager_launch
    parent_file = tmp_path / "afile"
    parent_file.write_text("x")
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(parent_file / "manager"))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: False)
    monkeypatch.setattr(manager_launch, "_server_alive", lambda: False)
    execs = []
    monkeypatch.setattr(manager_launch.os, "execvp", lambda prog, argv: execs.append(argv))
    assert manager_launch.main([]) == 1
    assert execs == []
    assert "refusing to launch" in capsys.readouterr().err


def test_switch_from_inside_refuses_when_manager_home_cannot_be_created(monkeypatch, tmp_path, capsys):
    from dockwright import manager_launch
    parent_file = tmp_path / "afile"
    parent_file.write_text("x")
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(parent_file / "manager"))
    _inside_env(monkeypatch, tmp_path)
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")
    monkeypatch.setattr(manager_launch, "_conf", lambda: None)
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: False)
    calls = []
    monkeypatch.setattr(manager_launch.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _Result())
    assert manager_launch.main([]) == 1
    assert calls == []
    assert "refusing to launch" in capsys.readouterr().err


@pytest.mark.real_tmux
def test_live_inside_tmux_creates_mgr_detached_and_switches(monkeypatch, tmp_path, real_tmux):
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
    monkeypatch.setattr(manager_launch, "_effort", lambda: "high")
    presets = tmp_path / "presets"; presets.mkdir()
    settings = presets / "manager-settings.json"
    settings.write_text("{}")
    monkeypatch.setattr(manager_launch.paths, "PRESETS", presets)
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_SKIP_PERMS", "1")
    assert manager_launch.manager_claude_args() == [
        "--remote-control", "--settings", str(settings), MGR_DENY,
        "--dangerously-skip-permissions", "--effort", "high"]


@pytest.mark.parametrize("value", ["", "0", "true", "yes"])
def test_manager_claude_args_skip_perms_strict_opt_in(monkeypatch, tmp_path, value):
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "nope")
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_SKIP_PERMS", value)
    assert "--dangerously-skip-permissions" not in manager_launch.manager_claude_args()


def _write_config(tmp_path, body):
    p = tmp_path / "dockwright.toml"
    p.write_text(body)
    return p


def test_manager_claude_args_omits_effort_when_unset(monkeypatch, tmp_path):
    monkeypatch.setattr(manager_launch, "_effort", lambda: "")
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "nope")
    assert manager_launch.manager_claude_args() == ["--remote-control", MGR_DENY]


def test_manager_effort_reaches_runtime_argv(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(_write_config(tmp_path, '[spawn]\nmanager_effort = "max"\n')))
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "nope")
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    argv = manager_launch._runtime_argv()
    assert argv[argv.index("--effort") + 1] == "max"


def test_manager_claude_args_skip_perms_independent_of_rc(monkeypatch, tmp_path):
    monkeypatch.setattr(manager_launch, "_effort", lambda: "high")
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_RC", "0")
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_SKIP_PERMS", "1")
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "nope")
    assert manager_launch.manager_claude_args() == [
        "--settings", '{"remoteControlAtStartup": false}', MGR_DENY,
        "--dangerously-skip-permissions", "--effort", "high"]


def test_runtime_argv_skip_perms_parse_safe(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_SKIP_PERMS", "1")
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "nope")
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    monkeypatch.setattr(manager_launch, "_effort", lambda: "high")
    argv = manager_launch._runtime_argv()
    assert argv == [str(paths.dockwright_bin()), "launch", "--remote-control", MGR_DENY, "--dangerously-skip-permissions",
                    "--effort", "high", "--model", "opus[1m]", "/manager"]
    _assert_rc_parse_safe(argv)


def test_main_execvp_scrubs_env_after_argv_composition(monkeypatch, tmp_path):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(manager_launch, "_has_mgr_session", lambda: False)
    monkeypatch.setattr(manager_launch, "_server_alive", lambda: False)
    monkeypatch.setattr(manager_launch, "_conf", lambda: None)
    monkeypatch.setattr(manager_launch, "_socket", lambda: "dockwright")
    monkeypatch.setattr(manager_launch, "_model", lambda: "opus[1m]")
    monkeypatch.setattr(manager_launch, "_effort", lambda: "high")
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
    monkeypatch.setattr(manager_launch, "_effort", lambda: "high")
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


RC_POOL = """\
[accounts]
default = "a"
[[accounts.pool]]
name = "a"
[[accounts.pool]]
name = "b"
manager_remote_control = false
[[accounts.pool]]
name = "c"
manager_remote_control = "false"
[[accounts.pool]]
name = "d"
manager_remote_control = true
"""

OFF_ONLY = ["--settings", '{"remoteControlAtStartup": false}', MGR_DENY]


@pytest.fixture
def rc_pool(monkeypatch, tmp_path):
    p = tmp_path / "dockwright.toml"
    p.write_text(RC_POOL)
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "no-presets")
    return p


def _preset(monkeypatch, tmp_path, text):
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "manager-settings.json").write_text(text)
    monkeypatch.setattr(manager_launch.paths, "PRESETS", presets)
    return presets / "manager-settings.json"


@pytest.mark.parametrize("account", ["a", "d", "not-in-pool", None])
def test_manager_rc_args_on(rc_pool, account):
    assert manager_launch.manager_rc_args(account) == ["--remote-control", MGR_DENY]


def test_manager_rc_args_on_carries_the_preset_path(rc_pool, monkeypatch, tmp_path):
    preset = _preset(monkeypatch, tmp_path, '{"permissions": {"allow": []}}')
    assert manager_launch.manager_rc_args("a") == [
        "--remote-control", "--settings", str(preset), MGR_DENY]


@pytest.mark.parametrize("account", ["b", "c"])
def test_manager_rc_args_off_merges_the_preset(rc_pool, monkeypatch, tmp_path, account):
    _preset(monkeypatch, tmp_path, '{"permissions": {"allow": ["Bash(ls:*)"]}}')
    flag, value, deny = manager_launch.manager_rc_args(account)
    assert flag == "--settings"
    assert deny == MGR_DENY
    assert json.loads(value) == {
        "permissions": {"allow": ["Bash(ls:*)"]}, "remoteControlAtStartup": False}


def test_manager_rc_args_off_without_preset(rc_pool):
    assert manager_launch.manager_rc_args("b") == OFF_ONLY


@pytest.mark.parametrize("text", ["not json", "[1, 2]"])
def test_manager_rc_args_off_with_unusable_preset(rc_pool, monkeypatch, tmp_path, text):
    _preset(monkeypatch, tmp_path, text)
    assert manager_launch.manager_rc_args("b") == OFF_ONLY


@pytest.mark.parametrize("account", ["a", "d", None])
def test_env_opt_out_switches_every_account_off(rc_pool, monkeypatch, account):
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_RC", " 0 ")
    assert manager_launch.manager_rc_args(account) == OFF_ONLY


def test_none_takes_the_default_accounts_value(monkeypatch, tmp_path):
    p = tmp_path / "dockwright.toml"
    p.write_text(RC_POOL.replace('default = "a"', 'default = "b"'))
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "no-presets")
    assert manager_launch.manager_rc_args(None) == OFF_ONLY


def test_no_pool_keeps_rc_on(monkeypatch, tmp_path):
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "no-presets")
    assert manager_launch.manager_rc_args("b") == ["--remote-control", MGR_DENY]


def test_manager_claude_args_without_account_reads_the_pointer(rc_pool, monkeypatch):
    monkeypatch.setattr(manager_launch, "_effort", lambda: "")
    paths.ACCOUNT_ACTIVE.parent.mkdir(parents=True, exist_ok=True)
    paths.ACCOUNT_ACTIVE.write_text("b\n")
    assert manager_launch.manager_claude_args() == OFF_ONLY
    paths.ACCOUNT_ACTIVE.write_text("a\n")
    assert manager_launch.manager_claude_args() == ["--remote-control", MGR_DENY]
    paths.ACCOUNT_ACTIVE.unlink()
    assert manager_launch.manager_claude_args() == ["--remote-control", MGR_DENY]


@pytest.mark.parametrize("pointer", ["a", "b"])
def test_dockwright_manager_follows_the_default_account(rc_pool, monkeypatch, pointer):
    monkeypatch.setattr(manager_launch, "_model", lambda: "m1")
    monkeypatch.setattr(manager_launch, "_effort", lambda: "")
    paths.ACCOUNT_ACTIVE.parent.mkdir(parents=True, exist_ok=True)
    paths.ACCOUNT_ACTIVE.write_text(f"{pointer}\n")
    assert manager_launch._runtime_argv() == [
        str(paths.dockwright_bin()), "launch", "--remote-control", MGR_DENY, "--model", "m1", "/manager"]


def _rc_cli(tmp_path, *args):
    env = {**os.environ, "HOME": str(tmp_path / "cli-home")}
    return subprocess.run(
        [sys.executable, "-m", "dockwright", "manager-rc-args", *args],
        capture_output=True, text=True, env=env)


@pytest.mark.parametrize("args,account", [(["--account", "b"], "b"),
                                          (["--account", "a"], "a"),
                                          ([], None)])
def test_manager_rc_args_cli_round_trips(rc_pool, tmp_path, args, account):
    r = _rc_cli(tmp_path, *args)
    assert r.returncode == 0, r.stderr
    assert shlex.split(r.stdout) == manager_launch.manager_rc_args(account)


@pytest.mark.parametrize("args", [["--account"], ["--account", ""], ["--bogus"],
                                  ["--account", "b", "extra"]])
def test_manager_rc_args_cli_refuses_bad_args(rc_pool, tmp_path, args):
    r = _rc_cli(tmp_path, *args)
    assert r.returncode == 2
    assert r.stdout == ""
    assert "usage: dockwright manager-rc-args" in r.stderr


CONFIGURED_SPAWN = '[spawn]\nmanager_model = "@opus[1m]"\nmanager_effort = "high"\n'


def _launch_cli(tmp_path, *args):
    env = {**os.environ, "HOME": str(tmp_path / "cli-home")}
    return subprocess.run(
        [sys.executable, "-m", "dockwright", "manager-launch-args", *args],
        capture_output=True, text=True, env=env)


@pytest.mark.parametrize("args,account", [(["--account", "b"], "b"),
                                          (["--account", "a"], "a"),
                                          ([], None)])
def test_manager_launch_args_cli_prints_the_configured_launch(rc_pool, tmp_path, args, account):
    rc_pool.write_text(RC_POOL + CONFIGURED_SPAWN)
    r = _launch_cli(tmp_path, *args)
    assert r.returncode == 0, r.stderr
    model = config.manager_model()
    assert r.stdout == shlex.join(
        [*manager_launch.manager_claude_args(account), "--model", model]) + "\n"
    assert f"--effort high --model '{model}'" in r.stdout, (
        f"the model must come single-quoted against zsh NOMATCH on the [1m] variant: {r.stdout!r}")


@pytest.mark.parametrize("args", [["--account"], ["--account", ""], ["--bogus"],
                                  ["--account", "b", "extra"]])
def test_manager_launch_args_cli_refuses_bad_args(rc_pool, tmp_path, args):
    r = _launch_cli(tmp_path, *args)
    assert r.returncode == 2
    assert r.stdout == ""
    assert "usage: dockwright manager-launch-args" in r.stderr


def test_manager_launch_args_cli_refuses_an_unknown_model_family(rc_pool, tmp_path):
    rc_pool.write_text(RC_POOL + '[spawn]\nmanager_model = "@nosuchfamily"\n')
    r = _launch_cli(tmp_path)
    assert r.returncode == 1
    assert r.stdout == ""
    assert "nosuchfamily" in r.stderr


@pytest.mark.parametrize("account", ["a", "b", None])
def test_unparseable_config_switches_every_account_off(monkeypatch, tmp_path, account):
    p = tmp_path / "dockwright.toml"
    p.write_text(RC_POOL + 'weight = "oops\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(p))
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "no-presets")
    assert config.load_error() is not None
    assert manager_launch.manager_rc_args(account) == OFF_ONLY


def _manager_role(tmp_path, monkeypatch, settings='{"skillOverrides": {"skill-x": "name-only"}}'):
    role_dir = tmp_path / "roles" / "Manager"
    role_dir.mkdir(parents=True)
    (role_dir / "role.toml").write_text('core = "manager"\nlaunch = ["manager"]\n')
    (role_dir / "role.manager.md").write_text("<!-- dockwright-role: Manager launch=manager -->\n")
    (role_dir / "role.settings.json").write_text(settings)
    monkeypatch.setattr(paths, "ROLES_DIR", tmp_path / "roles")
    return role_dir


@pytest.mark.parametrize("account,expected", [("a", ["--remote-control", MGR_DENY]), ("b", OFF_ONLY)])
def test_manager_rc_args_without_role_manifests_is_unchanged(rc_pool, account, expected, capsys):
    assert manager_launch.manager_rc_args(account) == expected
    assert capsys.readouterr().err == ""


def test_manager_rc_args_rc_on_carries_the_manager_role(rc_pool, monkeypatch, tmp_path):
    _preset(monkeypatch, tmp_path, '{"permissions": {"allow": ["Bash(ls:*)"]}}')
    role_dir = _manager_role(tmp_path, monkeypatch)
    args = manager_launch.manager_rc_args("a")
    assert args[:3] == ["--remote-control", "--append-system-prompt-file", str(role_dir / "role.manager.md")]
    assert args[3] == "--settings" and len(args) == 6 and args[5] == MGR_DENY
    assert json.loads(Path(args[4]).read_text()) == {
        "permissions": {"allow": ["Bash(ls:*)"]}, "skillOverrides": {"skill-x": "name-only"}}
    _assert_rc_parse_safe(args)


def test_manager_rc_args_rc_off_merges_the_manager_role_inline(rc_pool, monkeypatch, tmp_path):
    _preset(monkeypatch, tmp_path, '{"permissions": {"allow": ["Bash(ls:*)"]}}')
    role_dir = _manager_role(tmp_path, monkeypatch)
    args = manager_launch.manager_rc_args("b")
    assert args[:3] == ["--append-system-prompt-file", str(role_dir / "role.manager.md"), "--settings"]
    assert json.loads(args[3]) == {"permissions": {"allow": ["Bash(ls:*)"]},
                                   "skillOverrides": {"skill-x": "name-only"},
                                   "remoteControlAtStartup": False}


def test_manager_rc_args_without_a_manager_manifest_launches_legacy_and_says_why(rc_pool, tmp_path, monkeypatch,
                                                                                 capsys):
    other = tmp_path / "roles" / "Coder"
    other.mkdir(parents=True)
    (other / "role.toml").write_text('core = "worker"\nlaunch = ["worker"]\n')
    monkeypatch.setattr(paths, "ROLES_DIR", tmp_path / "roles")
    assert manager_launch.manager_rc_args("a") == ["--remote-control", MGR_DENY]
    assert manager_launch.manager_rc_args("b") == OFF_ONLY
    err = capsys.readouterr().err.splitlines()
    assert err and all(line.startswith("dockwright: manager launches without its role:") for line in err)


def test_manager_rc_args_with_unreadable_role_settings_launches_legacy(rc_pool, tmp_path, monkeypatch, capsys):
    _manager_role(tmp_path, monkeypatch, settings="not json")
    assert manager_launch.manager_rc_args("a") == ["--remote-control", MGR_DENY]
    assert capsys.readouterr().err.startswith("dockwright: manager launches without its role:")


def test_manager_rc_args_cli_prints_the_manager_role(rc_pool, tmp_path, monkeypatch, capsys):
    role_dir = _manager_role(tmp_path, monkeypatch)
    assert manager_launch.manager_rc_args_main(["--account", "a"]) == 0
    printed = shlex.split(capsys.readouterr().out)
    assert printed[:3] == ["--remote-control", "--append-system-prompt-file", str(role_dir / "role.manager.md")]
    assert printed[3] == "--settings" and Path(printed[4]).is_file()


def test_runtime_argv_carries_the_manager_role_once(rc_pool, tmp_path, monkeypatch):
    role_dir = _manager_role(tmp_path, monkeypatch)
    monkeypatch.setattr(manager_launch, "_model", lambda: "m1")
    monkeypatch.setattr(manager_launch, "_effort", lambda: "")
    argv = manager_launch._runtime_argv()
    assert argv.count("--append-system-prompt-file") == 1 and argv.count("--settings") == 1
    assert argv[argv.index("--append-system-prompt-file") + 1] == str(role_dir / "role.manager.md")
    assert argv[-1] == "/manager"


def _manager_profile(tmp_path, monkeypatch, role_dir):
    home = tmp_path / "work" / "manager"
    home.mkdir(parents=True)
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(home))
    settings = {"sets": {"none": [], "common": ["acme"]}, "repos": {"manager": "common"},
                "work_roots": [str(tmp_path / "work")], "work_default": "none"}
    monkeypatch.setattr(config, "profiles", lambda: settings)
    (role_dir / "role.manager.common.md").write_text(
        "<!-- dockwright-role: Manager launch=manager -->\n<!-- dockwright-profile: common -->\n")
    (role_dir.parent / "_agents").mkdir()
    (role_dir.parent / "_agents" / "common.json").write_text("{}")
    return home


def test_manager_rc_args_take_the_manager_homes_profile_and_its_agents_file(rc_pool, monkeypatch, tmp_path):
    _preset(monkeypatch, tmp_path, '{"permissions": {"allow": ["Bash(ls:*)"]}}')
    role_dir = _manager_role(tmp_path, monkeypatch)
    _manager_profile(tmp_path, monkeypatch, role_dir)
    args = manager_launch.manager_rc_args("a")
    assert args[:3] == ["--remote-control", "--append-system-prompt-file", str(role_dir / "role.manager.common.md")]
    assert args[5:7] == ["--agents-file", str(role_dir.parent / "_agents" / "common.json")] and args[-1] == MGR_DENY
    off = manager_launch.manager_rc_args("b")
    assert off[1] == str(role_dir / "role.manager.common.md")
    assert off[-3:] == ["--agents-file", str(role_dir.parent / "_agents" / "common.json"), MGR_DENY]
    _assert_rc_parse_safe(args)


def test_runtime_argv_starts_with_dockwright_launch(rc_pool, tmp_path, monkeypatch):
    _manager_role(tmp_path, monkeypatch)
    monkeypatch.setattr(manager_launch, "_model", lambda: "m1")
    monkeypatch.setattr(manager_launch, "_effort", lambda: "")
    argv = manager_launch._runtime_argv()
    assert argv[:2] == [str(paths.dockwright_bin()), "launch"] and argv[-1] == "/manager"


def test_manager_rc_args_falls_back_when_the_role_settings_cannot_be_written(rc_pool, tmp_path, monkeypatch, capsys):
    from dockwright import roles
    _manager_role(tmp_path, monkeypatch)

    def refuse(settings):
        raise PermissionError("role-settings is read-only")

    monkeypatch.setattr(roles, "write_spawn_settings", refuse)
    assert manager_launch.manager_rc_args("a") == ["--remote-control", MGR_DENY]
    assert capsys.readouterr().err.startswith(
        "dockwright: manager launches without its role: role-settings is read-only")
