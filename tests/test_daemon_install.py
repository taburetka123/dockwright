import importlib.util
import json
import os
import plistlib
import subprocess
from pathlib import Path

import pytest

from dockwright import config, daemon, paths

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "deploy" / "scripts" / "daemon-install.sh"
LABEL = "com.dw-test.daemon"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "dockwright.toml"
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(path))

    def write(enabled: bool):
        path.write_text(f'[daemon]\nenabled = {"true" if enabled else "false"}\n'
                        f'[loops]\nlabel_prefix = "com.dw-test"\n')

    write(True)
    return write


class FakeLaunchd:
    def __init__(self):
        self.calls = []
        self.loaded = False
        self.installer_rc = 0

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        if cmd[:2] == ["launchctl", "print"]:
            return 0 if self.loaded else 113
        if cmd[:2] == ["launchctl", "bootout"]:
            self.loaded = False
            return 0
        if cmd[0] == "bash":
            if self.installer_rc:
                return self.installer_rc
            paths.LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
            env = {"PATH": "/usr/bin", **json.loads(Path(cmd[7]).read_text())}
            data = {"Label": cmd[4], "EnvironmentVariables": env, "ProcessType": "Interactive"}
            if cmd[5] == "tick":
                data.update({"ProgramArguments": [cmd[2], "daemon", "tick"], "StartInterval": int(cmd[6])})
            else:
                data.update({"ProgramArguments": [cmd[2], "daemon", "observe"], "WatchPaths": [cmd[6]],
                             "ThrottleInterval": 1})
            with (paths.LAUNCH_AGENTS / f"{cmd[4]}.plist").open("wb") as fh:
                plistlib.dump(data, fh, sort_keys=False)
            self.loaded = True
        return 0

    def installs(self):
        return [c for c in self.calls if c[0] == "bash"]


def _sync(fake, tmp_path, program=None):
    return daemon.sync(run=fake, uid=501, program=program or tmp_path / "venv" / "dockwright",
                       script=tmp_path / "daemon-install.sh")


def _starts():
    return [r for r in daemon.read_jsonl(paths.DAEMON / "ledger.jsonl") if r["type"] == "start"]


def _stub_launchctl(tmp_path):
    stub = tmp_path / "bin"
    stub.mkdir()
    log = tmp_path / "launchctl.log"
    (stub / "launchctl").write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 0\n')
    (stub / "launchctl").chmod(0o755)
    return stub, log


def test_the_installer_writes_the_interval_it_is_given_and_bootstraps_it(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    stub, log = _stub_launchctl(tmp_path)
    program = tmp_path / "dockwright"
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    daemon_dir = tmp_path / "state" / "daemon"
    env_file = tmp_path / "env.json"
    env_file.write_text("{}")
    env = {**os.environ, "HOME": str(home), "PATH": f"{stub}:/usr/bin:/bin"}
    r = subprocess.run(["bash", str(INSTALLER), str(program), str(daemon_dir), LABEL, "tick", "45", str(env_file)],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    plist = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    with plist.open("rb") as fh:
        data = plistlib.load(fh)
    assert data["Label"] == LABEL
    assert data["ProgramArguments"] == [str(program), "daemon", "tick"]
    assert data["StartInterval"] == 45 and data["RunAtLoad"] is True
    assert data["StandardErrorPath"] == str(daemon_dir / "launchd-err.log")
    assert data["ProcessType"] == "Interactive"
    assert daemon_dir.is_dir()
    assert f"bootstrap gui/{os.getuid()} {plist}" in log.read_text()


def test_the_installer_refuses_a_binary_that_is_not_there(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    stub, log = _stub_launchctl(tmp_path)
    env = {**os.environ, "HOME": str(home), "PATH": f"{stub}:/usr/bin:/bin"}
    env_file = tmp_path / "env.json"
    env_file.write_text("{}")
    r = subprocess.run(["bash", str(INSTALLER), str(tmp_path / "nope"), str(tmp_path / "d"), LABEL, "tick", "60",
                        str(env_file)], env=env, capture_output=True, text=True)
    assert r.returncode == 1
    assert not (home / "Library" / "LaunchAgents" / f"{LABEL}.plist").exists()
    assert not log.exists()


def test_sync_installs_both_jobs_once_and_writes_one_start(cfg, tmp_path):
    fake = FakeLaunchd()
    program = tmp_path / "venv" / "dockwright"
    assert _sync(fake, tmp_path) == 0
    installs = fake.installs()
    assert [c[4:7] for c in installs] == [[LABEL, "tick", str(daemon.TICK_INTERVAL_SEC)],
                                          ["com.dw-test.daemon-wake", "observe", str(paths.DAEMON_WAKE)]]
    assert all(c[2] == str(program) and c[3] == str(paths.DAEMON) for c in installs)
    assert _sync(fake, tmp_path) == 0 and len(fake.installs()) == 2
    assert len(_starts()) == 1
    assert paths.DAEMON_WAKE.is_file()


def test_sync_reinstalls_when_the_lanes_env_changes(cfg, tmp_path, monkeypatch):
    fake = FakeLaunchd()
    _sync(fake, tmp_path)
    cfg_path = Path(os.environ[config.ENV_CONFIG_PATH])
    cfg_path.write_text(cfg_path.read_text() + '[lanes.env]\nCLAUDE_ORCH_AUTONUDGE = "1"\n')
    _sync(fake, tmp_path)
    assert len(fake.installs()) == 4
    with daemon.plist_path().open("rb") as fh:
        assert plistlib.load(fh)["EnvironmentVariables"]["CLAUDE_ORCH_AUTONUDGE"] == "1"
    with daemon.wake_plist_path().open("rb") as fh:
        assert plistlib.load(fh)["EnvironmentVariables"]["CLAUDE_ORCH_AUTONUDGE"] == "1"
    _sync(fake, tmp_path)
    assert len(fake.installs()) == 4


def test_sync_reinstalls_a_plist_that_runs_another_binary(cfg, tmp_path):
    fake = FakeLaunchd()
    _sync(fake, tmp_path, program=tmp_path / "old" / "dockwright")
    _sync(fake, tmp_path)
    old, new = str(tmp_path / "old" / "dockwright"), str(tmp_path / "venv" / "dockwright")
    assert [c[2] for c in fake.installs()] == [old, old, new, new]


def test_sync_removes_both_jobs_when_disabled(cfg, tmp_path):
    fake = FakeLaunchd()
    _sync(fake, tmp_path)
    cfg(False)
    assert _sync(fake, tmp_path) == 0
    assert ["launchctl", "bootout", f"gui/501/{LABEL}"] in fake.calls
    assert ["launchctl", "bootout", "gui/501/com.dw-test.daemon-wake"] in fake.calls
    assert not daemon.plist_path().exists() and not daemon.wake_plist_path().exists()


def test_plist_matches_rejects_a_wake_plist_without_watch_paths(cfg, tmp_path):
    target = paths.LAUNCH_AGENTS / "x.plist"
    target.parent.mkdir(parents=True)
    with target.open("wb") as fh:
        plistlib.dump({"Label": daemon.wake_label(), "ProgramArguments": ["/p", "daemon", "observe"],
                       "EnvironmentVariables": {"PATH": "/usr/bin"}}, fh)
    assert not daemon._plist_matches(target, Path("/p"), "observe", {})


def test_sync_refuses_to_run_from_a_worktree(cfg, tmp_path, capsys, monkeypatch):
    fake = FakeLaunchd()
    monkeypatch.setattr(config, "worktree_roots", lambda: str(tmp_path / "wt"))
    rc = _sync(fake, tmp_path, program=tmp_path / "wt" / "repo" / ".venv" / "bin" / "dockwright")
    assert rc == 1 and fake.installs() == []
    assert "run daemon sync from the main clone" in capsys.readouterr().err


def test_the_installer_writes_a_wake_plist_with_watch_paths_and_the_env_file(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    stub, log = _stub_launchctl(tmp_path)
    program = tmp_path / "dockwright"
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    env_file = tmp_path / "env.json"
    env_file.write_text('{"CLAUDE_ORCH_AUTONUDGE": "1", "A_B": "x y"}')
    daemon_dir = tmp_path / "state" / "daemon"
    env = {**os.environ, "HOME": str(home), "PATH": f"{stub}:/usr/bin:/bin"}
    r = subprocess.run(["bash", str(INSTALLER), str(program), str(daemon_dir), "com.dw-test.daemon-wake", "observe",
                        str(daemon_dir / "wake"), str(env_file)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    with (home / "Library" / "LaunchAgents" / "com.dw-test.daemon-wake.plist").open("rb") as fh:
        data = plistlib.load(fh)
    assert data["ProgramArguments"] == [str(program), "daemon", "observe"]
    assert data["WatchPaths"] == [str(daemon_dir / "wake")] and data["ThrottleInterval"] == 1
    assert data["RunAtLoad"] is False and "StartInterval" not in data
    assert data["EnvironmentVariables"]["CLAUDE_ORCH_AUTONUDGE"] == "1" and data["EnvironmentVariables"]["A_B"] == "x y"
    assert list(data["EnvironmentVariables"])[0] == "PATH"
    assert data["StandardOutPath"] == str(daemon_dir / "launchd-wake-out.log")
    assert data["ProcessType"] == "Interactive"


@pytest.mark.parametrize("mode", ["tick", "observe"])
def test_a_plist_without_the_process_type_does_not_match(cfg, mode):
    target = paths.LAUNCH_AGENTS / "x.plist"
    target.parent.mkdir(parents=True)
    data = {"ProgramArguments": ["/p", "daemon", mode], "EnvironmentVariables": {"PATH": "/usr/bin"}}
    if mode == "tick":
        data.update({"Label": daemon.label(), "StartInterval": daemon.TICK_INTERVAL_SEC})
    else:
        data.update({"Label": daemon.wake_label(), "WatchPaths": [str(paths.DAEMON_WAKE)], "ThrottleInterval": 1})
    with target.open("wb") as fh:
        plistlib.dump(data, fh)
    assert daemon._plist_matches(target, Path("/p"), mode, {}) is False
    with target.open("wb") as fh:
        plistlib.dump({**data, "ProcessType": "Interactive"}, fh)
    assert daemon._plist_matches(target, Path("/p"), mode, {}) is True


def test_sync_touches_nothing_when_disabled_and_absent(cfg, tmp_path):
    cfg(False)
    fake = FakeLaunchd()
    assert _sync(fake, tmp_path) == 0
    assert fake.installs() == [] and not any(c[:2] == ["launchctl", "bootout"] for c in fake.calls)


def test_sync_reports_a_failed_installer_and_writes_no_start(cfg, tmp_path, capsys):
    fake = FakeLaunchd()
    fake.installer_rc = 1
    assert _sync(fake, tmp_path) == 1
    assert "not installed" in capsys.readouterr().err
    assert _starts() == []


def test_status_reports_the_install(cfg, tmp_path):
    fake = FakeLaunchd()
    _sync(fake, tmp_path)
    s = daemon.status(run=fake, uid=501)
    assert (s["label"], s["enabled"], s["installed"], s["loaded"], s["stopped"]) == (LABEL, True, True, True, False)
    assert isinstance(s["start"], float) and s["heartbeat"] is None


def test_the_registry_block_names_the_label_sync_installs(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("loops_status_for_daemon",
                                                  REPO / "deploy" / "scripts" / "loops_status.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    loops = mod.load_all_loops(cli_arg=str(REPO / "deploy" / "loops-registry.md"), overlay_dir=tmp_path,
                               prefix=config.loop_label_prefix(), status_overrides={})
    block = next(loop for loop in loops if loop["name"] == "daemon")
    assert block["label"] == daemon.label()
    wake = next(loop for loop in loops if loop["name"] == "daemon-wake")
    assert wake["label"] == daemon.wake_label()
