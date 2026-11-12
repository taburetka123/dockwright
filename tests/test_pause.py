import asyncio
import json
import multiprocessing
import os
import signal
import time

import pytest

from dockwright import config, daemon, paths, pause, revive, state

IDLE_PANE = "──────────\n❯ \n──────────\n  manager  🎯 sunny-fairy · product\n"
BUSY_PANE = "· Mulling… (9m 27s · ↓ 38.1k tokens)\n──────────\n❯ \n──────────\n"
CLAUDE_COMMAND = "2.1.260"
UID = 501
BASE_TOML = "[lanes]\nin_manager = false\n"


class FakeDriver:
    def __init__(self, live_pids):
        self.calls = []
        self.live_pids = live_pids
        self.pane_pids = {}
        self.screen = IDLE_PANE
        self.send_ok = True
        self.panes = []
        self.on_close = None
        self.on_send = None
        self.dead_panes = set()

    def socket(self):
        return "dockwright"

    def capture_screen_ansi(self, pane):
        self.calls.append(("capture", pane))
        return "some other pane" if not pane else None if pane in self.dead_panes else self.screen

    def pane_current_command(self, pane):
        return None if not pane or pane in self.dead_panes else CLAUDE_COMMAND

    def send_text_checked(self, pane, text):
        self.calls.append(("send", pane, text))
        if self.on_send is not None:
            self.on_send(pane, text)
        return self.send_ok

    def ls(self):
        return self.panes

    def close(self, pane):
        self.calls.append(("close", pane))
        if self.on_close is not None:
            self.on_close(pane)
        self.live_pids.discard(self.pane_pids.get(pane))


class FakeRun:
    def __init__(self):
        self.argv = []
        self.loaded = set()

    def __call__(self, cmd):
        self.argv.append(list(cmd))
        if cmd[:2] == ["launchctl", "print"]:
            return 0 if cmd[2] in self.loaded else 1
        return 0

    def verbs(self):
        return [c[1] for c in self.argv if c[0] == "launchctl"]


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    for attr, sub in (("ROOT", ""), ("ACTIVE", "active"), ("CLOSED", "closed"), ("QUESTIONS", "questions"),
                      ("ANSWERS", "answers"), ("DONE", "done"), ("TURN_ENDS", "turn-ends"), ("HANDOFFS", "handoffs"),
                      ("PRESETS", "presets"), ("SLOTS", "slots"), ("MANAGER_MEMORY", "manager-memory"),
                      ("ARCHITECT", "architect"), ("ARTIFACTS", "artifacts"), ("ASSIGNMENTS", "assignments"),
                      ("PANE_SENDS", "pane-sends"), ("DAEMON", "daemon"), ("PAUSE", "pause"),
                      ("LAUNCH_AGENTS", "LaunchAgents")):
        monkeypatch.setattr(paths, attr, tmp_path / sub if sub else tmp_path)
    monkeypatch.setattr(paths, "ASSIGNMENTS_PENDING", tmp_path / "assignments" / ".pending")
    monkeypatch.setattr(paths, "DAEMON_STOP", tmp_path / "daemon-stop")
    monkeypatch.setattr(paths, "PAUSE_MANIFEST", tmp_path / "pause" / "manifest.json")
    monkeypatch.setattr(paths, "SPEND_LEDGER", tmp_path / "spend-ledger.jsonl")
    paths.ensure_dirs()
    paths.DAEMON.mkdir()
    monkeypatch.setattr(daemon, "_file", lambda name: tmp_path / "daemon" / name)
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(BASE_TOML)
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("DOCKWRIGHT_REVIVE_READY_TIMEOUT_SEC", "5")
    live_pids = set()
    driver = FakeDriver(live_pids)
    monkeypatch.setattr(pause, "get_driver", lambda: driver)
    monkeypatch.setattr(revive, "get_driver", lambda: driver)
    monkeypatch.setattr(revive, "_live_session_command",
                        lambda r: "claude --agents x --model y" if r.get("pid") in live_pids else "")
    monkeypatch.setattr(revive, "READY_POLL_SEC", 0)
    monkeypatch.setattr(pause, "PID_GONE_POLL_SEC", 0)
    monkeypatch.setattr(pause, "PID_GONE_TIMEOUT_SEC", 0.5)
    monkeypatch.setattr(revive, "attach_to_manager_session", lambda: pytest.fail("resume attached the terminal"))
    run = FakeRun()
    monkeypatch.setattr(pause, "_run", run)
    monkeypatch.setattr(pause, "_uid", lambda: UID)

    class Fleet:
        pass

    f = Fleet()
    f.root, f.cfg, f.driver, f.run, f.live_pids = tmp_path, cfg, driver, run, live_pids
    f.counter = [1000]

    def add(record):
        f.counter[0] += 1
        record.setdefault("pid", f.counter[0])
        state.write_json_atomic(paths.ACTIVE / f"{record['claude_sid']}.json", record)
        live_pids.add(record["pid"])
        if record.get("window_id"):
            driver.pane_pids[record["window_id"]] = record["pid"]
        return record

    f.add = add
    return f


def _manager(sid, name, domain, window_id, **extra):
    record = {"claude_sid": sid, "agent": "manager", "name": name, "domain": domain, "started_at": 100.0,
              "window_id": window_id, "cwd": "/tmp/mgr", "runtime": "claude", "account": "b", "state": "idle"}
    record.update(extra)
    return record


MANAGER_SIDS = {"proud-pegasus": "m1", "silly-dragon": "m2"}


def _worker(sid, name, parent, window_id, **extra):
    record = {"claude_sid": sid, "agent": "worker", "name": name, "started_at": 120.0, "window_id": window_id,
              "cwd": "/tmp/wt", "runtime": "claude", "account": "b", "kind": "worker",
              "parent_manager_name": parent, "parent_name": parent, "parent_sid": MANAGER_SIDS.get(parent),
              "last_summary": "did a thing"}
    record.update(extra)
    return record


def _two_managers_one_worker(fleet):
    fleet.add(_manager("m1", "proud-pegasus", "general", "%1"))
    fleet.add(_manager("m2", "silly-dragon", "meta", "%2"))
    fleet.add(_worker("w1", "K-1-ship", "proud-pegasus", "%3"))


def _closed(sid):
    return state.read_json(paths.CLOSED / f"{sid}.json")


def _lock_free():
    fd = daemon._try_lock("tick.lock", blocking=False)
    if fd is None:
        return False
    os.close(fd)
    return True


def test_pause_refuses_when_a_turn_is_running(fleet, capsys):
    _two_managers_one_worker(fleet)
    fleet.driver.screen = BUSY_PANE
    assert pause.main(["pause"]) == 1
    assert "pause: not quiet — proud-pegasus: a turn is running" in capsys.readouterr().out
    assert not paths.PAUSE_MANIFEST.exists() and not paths.DAEMON_STOP.exists()
    assert _lock_free()


def test_pause_refuses_on_pending_worker_question(fleet, capsys):
    _two_managers_one_worker(fleet)
    state.write_json_atomic(paths.QUESTIONS / "q1.json", {"question_id": "q1", "worker_sid": "w1",
                                                         "question": "which one?"})
    assert pause.main(["pause"]) == 1
    assert "pause: not quiet — K-1-ship: pending question" in capsys.readouterr().out


def test_pause_refuses_on_codex_session(fleet, capsys):
    _two_managers_one_worker(fleet)
    fleet.add(_worker("w2", "K-2-fix", "proud-pegasus", "%4", runtime="codex"))
    assert pause.main(["pause"]) == 1
    assert "K-2-fix: codex session" in capsys.readouterr().out


def test_pause_refuses_on_a_live_subworker(fleet, capsys):
    _two_managers_one_worker(fleet)
    fleet.add(_worker("w2", "K-1-ship-reviewer", "proud-pegasus", "%4", parent_sid="w1"))
    assert pause.main(["pause"]) == 1
    assert "K-1-ship: live subworker K-1-ship-reviewer" in capsys.readouterr().out


def test_pause_ignores_a_dead_subworker_record(fleet):
    _two_managers_one_worker(fleet)
    record = fleet.add(_worker("w2", "K-1-ship-reviewer", "proud-pegasus", "%4", parent_sid="w1"))
    fleet.live_pids.discard(record["pid"])
    fleet.driver.dead_panes.add("%4")
    assert pause.main(["pause"]) == 0
    assert _closed("w2")["closed_reason"] == "fleet-pause"


def test_pause_refuses_while_the_lanes_run_in_the_managers(fleet, capsys):
    _two_managers_one_worker(fleet)
    fleet.cfg.write_text("[lanes]\nin_manager = true\n")
    assert pause.main(["pause"]) == 1
    out = capsys.readouterr()
    assert "[lanes] in_manager = false" in out.out + out.err
    assert fleet.driver.calls == [] and not paths.PAUSE_MANIFEST.exists() and _lock_free()


def test_pause_refuses_a_dead_pid_with_a_live_busy_pane(fleet, capsys):
    _two_managers_one_worker(fleet)
    record = state.read_json(paths.ACTIVE / "w1.json")
    fleet.live_pids.discard(record["pid"])
    fleet.driver.screen = BUSY_PANE
    assert pause.main(["pause"]) == 1
    assert "pause: not quiet — K-1-ship: a turn is running" in capsys.readouterr().out
    assert not paths.PAUSE_MANIFEST.exists()


def test_pause_refuses_on_a_misspelled_pause_key(fleet, capsys):
    _two_managers_one_worker(fleet)
    fleet.cfg.write_text(BASE_TOML + '[pause]\nlaunchd_disabled = ["com.example.poller"]\n')
    assert pause.main(["pause"]) == 1
    out = capsys.readouterr()
    assert "pause: [pause] launchd_disabled is not a key" in out.out + out.err


def test_pause_refuses_when_the_config_does_not_parse(fleet, capsys):
    _two_managers_one_worker(fleet)
    fleet.cfg.write_text(BASE_TOML + '[pause]\nlaunchd_disable = ["com.example.poller"\n')
    assert pause.main(["pause"]) == 1
    out = capsys.readouterr()
    assert "pause: dockwright.toml does not parse" in out.out + out.err
    assert fleet.driver.calls == [] and not paths.PAUSE_MANIFEST.exists() and _lock_free()


def test_pause_refuses_when_a_pause_key_has_the_wrong_type(fleet, capsys):
    _two_managers_one_worker(fleet)
    fleet.cfg.write_text(BASE_TOML + '[pause]\nlaunchd_disable = "com.example.poller"\nstop_files = [1]\n')
    assert pause.main(["pause"]) == 1
    out = capsys.readouterr()
    assert "pause: [pause] launchd_disable must be a list of strings" in out.out + out.err
    assert "pause: [pause] stop_files must be a list of strings" in out.out + out.err
    assert not paths.PAUSE_MANIFEST.exists() and not paths.DAEMON_STOP.exists()


def test_pause_refuses_when_a_manifest_appears_while_it_waits_for_the_lock(fleet, monkeypatch, capsys):
    _two_managers_one_worker(fleet)
    real_lock = daemon._try_lock

    def lock(name, blocking):
        if name == "tick.lock":
            pause.write_manifest({"paused_at": 1.0, "managers": [{"claude_sid": "other"}]})
        return real_lock(name, blocking)

    monkeypatch.setattr(daemon, "_try_lock", lock)
    assert pause.main(["pause"]) == 1
    out = capsys.readouterr()
    assert "a pause is already in effect" in out.out + out.err
    assert pause.read_manifest()["managers"] == [{"claude_sid": "other"}]
    assert not paths.DAEMON_STOP.exists() and not any(c[0] == "close" for c in fleet.driver.calls)


def test_pause_always_creates_daemon_stop(fleet, monkeypatch):
    _two_managers_one_worker(fleet)
    fleet.cfg.write_text(BASE_TOML + '[pause]\nstop_files = ["chat-archive-stop"]\n')
    assert pause.main(["pause"]) == 0
    assert paths.DAEMON_STOP.exists() and (fleet.root / "chat-archive-stop").exists()
    manifest = pause.read_manifest()
    assert [n for n, s in manifest["stop_files"].items() if pause._stop_path(n) == paths.DAEMON_STOP and s == "created"]
    _resume_fixture(fleet, monkeypatch)
    assert pause.main(["resume", "--no-attach"]) == 0
    assert not paths.DAEMON_STOP.exists()


def test_pause_refuses_inside_the_dockwright_tmux(fleet, monkeypatch, capsys):
    _two_managers_one_worker(fleet)
    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/dockwright,1,0")
    assert pause.main(["pause"]) == 1
    out = capsys.readouterr()
    assert "run me outside the dockwright tmux server" in out.out + out.err
    assert fleet.driver.calls == []


def test_pause_refuses_when_a_manifest_exists(fleet, capsys):
    _two_managers_one_worker(fleet)
    pause.write_manifest({"paused_at": 1.0})
    assert pause.main(["pause"]) == 1
    out = capsys.readouterr()
    assert "a pause is already in effect" in out.out + out.err
    assert fleet.driver.calls == []


def _file_ops(monkeypatch):
    ops = []
    real_touch, real_manifest = pause._touch, pause.write_manifest

    def touch(path):
        ops.append(("touch", path.name))
        real_touch(path)

    def write_manifest(manifest):
        ops.append(("manifest",))
        real_manifest(manifest)

    monkeypatch.setattr(pause, "_touch", touch)
    monkeypatch.setattr(pause, "write_manifest", write_manifest)
    return ops


def test_pause_touches_no_stop_file_before_the_manifest(fleet, monkeypatch):
    _two_managers_one_worker(fleet)
    ops = _file_ops(monkeypatch)
    assert pause.main(["pause", "--dry-run"]) == 0
    assert ops == []
    assert pause.main(["pause"]) == 0
    assert ops.index(("manifest",)) < ops.index(("touch", "daemon-stop"))


def test_pause_holds_tick_lock_until_the_end(fleet):
    _two_managers_one_worker(fleet)
    seen = []
    fleet.driver.on_close = lambda pane: seen.append(daemon._try_lock("tick.lock", blocking=False))
    assert pause.main(["pause"]) == 0
    assert seen and seen[0] is None
    assert _lock_free()


def _pause_until_killed(root, entered, release):
    from dockwright import pause as child_pause
    from dockwright import revive as child_revive

    class BlockingDriver(FakeDriver):
        def capture_screen_ansi(self, pane):
            entered.set()
            release.wait()
            return IDLE_PANE

    driver = BlockingDriver(set())
    child_pause.get_driver = lambda: driver
    child_revive.get_driver = lambda: driver
    child_pause.main(["pause"])


def test_pause_death_before_the_manifest_leaves_nothing(fleet):
    _two_managers_one_worker(fleet)
    ctx = multiprocessing.get_context("fork")
    entered, release = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_pause_until_killed, args=(fleet.root, entered, release), daemon=True)
    child.start()
    try:
        assert entered.wait(10)
        os.kill(child.pid, signal.SIGKILL)
        child.join(10)
    finally:
        child.kill()
        child.join(10)
    assert _lock_free()
    assert not paths.PAUSE_MANIFEST.exists() and not paths.DAEMON_STOP.exists()


def test_pause_archives_workers_then_closes_managers(fleet, capsys):
    _two_managers_one_worker(fleet)
    fleet.add(_worker("w2", "K-2-fix", "silly-dragon", "%4"))
    assert pause.main(["pause"]) == 0
    closes = [c[1] for c in fleet.driver.calls if c[0] == "close"]
    assert closes.index("%3") < closes.index("%1") and closes.index("%4") < closes.index("%2")
    assert closes.index("%4") < closes.index("%1")
    assert _closed("w1")["closed_reason"] == "fleet-pause" and _closed("w2")["closed_reason"] == "fleet-pause"
    assert _closed("m1") is None and _closed("m2") is None
    assert list(paths.ACTIVE.iterdir()) == []
    out = capsys.readouterr().out
    assert "pause: archived K-1-ship sid w1" in out
    assert "pause: closed manager proud-pegasus (general) sid m1" in out


def test_pause_writes_closed_before_kill_pane(fleet, monkeypatch):
    _two_managers_one_worker(fleet)
    seen = []
    real_write = state.write_json_atomic

    def write(path, data):
        if path.parent == paths.CLOSED:
            seen.append(("closed", path.stem, (paths.ACTIVE / path.name).exists()))
        real_write(path, data)

    monkeypatch.setattr(state, "write_json_atomic", write)
    fleet.driver.on_close = lambda pane: seen.append(("close", pane, (paths.CLOSED / "w1.json").exists(),
                                                      (paths.ACTIVE / "w1.json").exists()))
    assert pause.main(["pause"]) == 0
    assert ("closed", "w1", True) in seen
    assert ("close", "%3", True, False) in seen


def test_pause_waits_for_a_closed_manager_to_exit_before_unlinking_its_record(fleet, monkeypatch):
    _two_managers_one_worker(fleet)
    polls = {}
    real_command = revive._live_session_command

    def command(record):
        pid = record.get("pid")
        if polls.get(pid) is not None:
            polls[pid] += 1
            if polls[pid] >= 3:
                fleet.live_pids.discard(pid)
        return real_command(record)

    monkeypatch.setattr(revive, "_live_session_command", command)
    fleet.driver.on_close = lambda pane: polls.setdefault(fleet.driver.pane_pids.get(pane), 0)
    fleet.driver.live_pids = set()
    seen = []
    real_unlink = pause._unlink

    def unlink(path, missing_ok=True):
        if path.parent == paths.ACTIVE:
            seen.append((path.stem, any(r["pid"] in fleet.live_pids for r in [state.read_json(path) or {}])))
        real_unlink(path, missing_ok)

    monkeypatch.setattr(pause, "_unlink", unlink)
    assert pause.main(["pause"]) == 0
    assert ("m1", False) in seen and ("m2", False) in seen and polls and all(n >= 3 for n in polls.values())


def test_pause_archives_a_worker_without_a_window_id(fleet):
    _two_managers_one_worker(fleet)
    record = fleet.add(_worker("w2", "K-2-fix", "silly-dragon", ""))
    fleet.live_pids.discard(record["pid"])
    assert pause.main(["pause"]) == 0
    assert _closed("w2")["closed_reason"] == "fleet-pause"
    assert ("close", "") not in fleet.driver.calls


def test_pause_disables_launchd_labels(fleet):
    _two_managers_one_worker(fleet)
    fleet.cfg.write_text(BASE_TOML + '[pause]\nlaunchd_disable = ["com.example.poller", "com.example.other"]\n')
    fleet.run.loaded.add(f"gui/{UID}/com.example.poller")
    assert pause.main(["pause"]) == 0
    verbs = [tuple(c[1:3]) for c in fleet.run.argv if c[0] == "launchctl"]
    poller = f"gui/{UID}/com.example.poller"
    assert verbs.index(("print", poller)) < verbs.index(("bootout", poller)) < verbs.index(("disable", poller))
    manifest = pause.read_manifest()
    assert manifest["launchd"]["com.example.poller"]["was_loaded"] is True
    assert manifest["launchd"]["com.example.other"]["was_loaded"] is False
    assert not [v for v in verbs if v[1] == f"gui/{UID}/com.example.other" and v[0] != "print"]
    assert manifest["launchd"]["com.example.poller"]["plist"] == str(paths.LAUNCH_AGENTS / "com.example.poller.plist")


def test_pause_dry_run_writes_nothing_and_releases(fleet, capsys):
    _two_managers_one_worker(fleet)
    fleet.cfg.write_text(BASE_TOML + '[pause]\nlaunchd_disable = ["com.example.poller"]\n')
    assert pause.main(["pause", "--dry-run"]) == 0
    assert not any(c[0] == "close" for c in fleet.driver.calls)
    assert "bootout" not in fleet.run.verbs()
    assert not paths.PAUSE_MANIFEST.exists() and not paths.DAEMON_STOP.exists()
    assert list(paths.ACTIVE.glob("*.json")) and _lock_free()
    assert "pause: would" in capsys.readouterr().out


def test_pause_warns_when_hold_is_empty_but_rules_have_stop_files(fleet, capsys):
    _two_managers_one_worker(fleet)
    fleet.cfg.write_text(BASE_TOML + '[events.rules.lane-x]\nsource = "tracker"\nkind = "sprint-assigned"\naction = "page"\n'
                         'stop_file = "x-stop"\n'
                         '[[events.sources.exec.commands]]\nargv = ["true"]\nstop_file = "y-stop"\n')
    assert pause.main(["pause"]) == 0
    out = capsys.readouterr().out
    assert "pause: WARNING [pause.hold] is empty — rules with a stop_file stay armed: lane-x, true" in out


def test_pause_refuses_on_a_hold_entry_that_is_not_a_string(fleet, capsys):
    _two_managers_one_worker(fleet)
    fleet.cfg.write_text(BASE_TOML + '[pause.hold]\n"lane-a-stop" = ["/start-a"]\n"lane-b-stop" = "/start-b"\n')
    assert pause.main(["pause"]) == 1
    out = capsys.readouterr()
    assert 'pause: [pause.hold] "lane-a-stop" must be a string' in out.out + out.err
    assert not (fleet.root / "lane-b-stop").exists() and not paths.PAUSE_MANIFEST.exists()


def test_pause_kills_the_tmux_server_and_names_foreign_panes(fleet, capsys):
    _two_managers_one_worker(fleet)
    fleet.driver.panes = [{"wm_class": "mgr", "tabs": [
        {"title": "mgr", "windows": [{"id": "%1", "title": "proud-pegasus"}]},
        {"title": "shell", "windows": [{"id": "%9", "title": "zsh at home"}]}]}]
    assert pause.main(["pause"]) == 0
    out = capsys.readouterr().out
    assert "pause: killing tmux with a pane not in the manifest: zsh at home" in out
    assert "proud-pegasus" not in [l for l in out.splitlines() if "not in the manifest" in l][0]
    assert ["tmux", "-L", "dockwright", "kill-server"] in fleet.run.argv


def test_pause_manifest_records_hold_and_flags(fleet):
    _two_managers_one_worker(fleet)
    fleet.cfg.write_text(BASE_TOML + '[pause.hold]\n"lane-a-stop" = "/start-a {lane-a-flag:--go}"\n'
                         '"lane-b-stop" = "/start-b"\n')
    (fleet.root / "lane-a-flag").write_text("")
    (fleet.root / "lane-b-stop").write_text("")
    assert pause.main(["pause"]) == 0
    manifest = pause.read_manifest()
    assert manifest["hold"] == {"lane-a-stop": "created", "lane-b-stop": "existed"}
    assert manifest["flags"] == {"lane-a-flag": True}
    assert manifest["stop_files"]["daemon-stop"] == "created"
    assert (fleet.root / "lane-a-stop").exists() and paths.DAEMON_STOP.exists()
    assert manifest["managers"][0]["name"] == "proud-pegasus" and manifest["managers"][0]["resume_pane"] is None
    assert manifest["workers"][0]["pipeline_node"] is False
    assert manifest["tmux_socket"] == "dockwright"


def test_pause_keeps_a_hold_file_out_of_stop_files(fleet, monkeypatch, capsys):
    _two_managers_one_worker(fleet)
    fleet.cfg.write_text(BASE_TOML + '[pause]\nstop_files = ["daemon-stop", "lane-a-stop"]\n'
                         '[pause.hold]\n"lane-a-stop" = "/start-a"\n')
    assert pause.main(["pause"]) == 0
    manifest = pause.read_manifest()
    assert "lane-a-stop" not in manifest["stop_files"] and manifest["hold"] == {"lane-a-stop": "created"}
    _resume_fixture(fleet, monkeypatch)
    assert pause.main(["resume", "--no-attach"]) == 0
    assert (fleet.root / "lane-a-stop").exists()
    out = capsys.readouterr().out
    assert "resume: stop files removed: daemon-stop\n" in out and "resume: kept: lane-a-stop" in out


def test_pause_marks_pipeline_nodes_in_the_manifest(fleet):
    _two_managers_one_worker(fleet)
    state.write_json_atomic(paths.assignment_path("w1"), {"ticket": "K", "pipeline_request_id": "r1"})
    assert pause.main(["pause"]) == 0
    assert pause.read_manifest()["workers"][0]["pipeline_node"] is True


def _manifest(fleet, managers, workers=(), **extra):
    manifest = {"paused_at": 500.0, "resumed_at": None,
                "managers": [{"claude_sid": sid, "name": name, "domain": domain, "account": "b", "cwd": "/tmp/mgr",
                              "pid": 1, "window_id": "%old", "started_at": 100.0, "resume_pane": None,
                              "notice_delivered_at": None, "record": _manager(sid, name, domain, "%old")}
                             for sid, name, domain in managers],
                "workers": [{"claude_sid": sid, "name": name, "parent_manager_name": parent,
                             "pipeline_node": node, "record": _worker(sid, name, parent, "%w")}
                            for sid, name, parent, node in workers],
                "stop_files": {"daemon-stop": "created"}, "hold": {}, "flags": {}, "launchd": {},
                "tmux_socket": "dockwright"}
    manifest.update(extra)
    paths.PAUSE.mkdir(exist_ok=True)
    pause.write_manifest(manifest)
    for name in list(manifest["stop_files"]) + list(manifest["hold"]):
        (fleet.root / name).write_text("")
    return manifest


def _resume_fixture(fleet, monkeypatch, register_after=3, never=()):
    ops = []
    polls = {}

    async def spawn(candidate, resume=True):
        record = dict(candidate.record)
        record["pid"] = 9000 + len(polls)
        record["window_id"] = f"%n{candidate.sid}"
        state.write_json_atomic(paths.ACTIVE / f"{candidate.sid}.json", record)
        ops.append(("spawn", candidate.sid))
        return record["window_id"]

    def pid_is_session(record):
        sid = record.get("claude_sid")
        if sid in never:
            return False
        polls[sid] = polls.get(sid, 0) + 1
        if polls[sid] == register_after:
            ops.append(("registered", sid))
        return polls[sid] >= register_after

    real_unlink, real_rename = pause._unlink, pause._rename

    def unlink(path, missing_ok=True):
        ops.append(("unlink", path.name))
        real_unlink(path, missing_ok)

    def rename(src, dst):
        ops.append(("rename", "manifest" if src == paths.PAUSE_MANIFEST else src.name))
        real_rename(src, dst)

    monkeypatch.setattr(pause, "spawn_manager_window", spawn)
    monkeypatch.setattr(revive, "record_pid_is_session", pid_is_session)
    monkeypatch.setattr(pause, "_unlink", unlink)
    monkeypatch.setattr(pause, "_rename", rename)
    return ops


def test_resume_keeps_daemon_stop_until_every_manager_is_notified(fleet, monkeypatch):
    _manifest(fleet, [("m1", "proud-pegasus", "general"), ("m2", "silly-dragon", "meta")])
    ops = _resume_fixture(fleet, monkeypatch)
    fleet.driver.on_send = lambda pane, text: ops.append(("send", pane, paths.DAEMON_STOP.exists()))
    assert pause.main(["resume", "--no-attach"]) == 0
    lift = ops.index(("unlink", "daemon-stop"))
    for sid in ("m1", "m2"):
        assert ops.index(("registered", sid)) < lift
        active = state.read_json(paths.ACTIVE / f"{sid}.json")
        assert active["name"] in ("proud-pegasus", "silly-dragon") and active["domain"] in ("general", "meta")
    assert [op for op in ops if op[0] == "send"] == [("send", "%nm1", True), ("send", "%nm2", True)]
    assert lift < ops.index(("rename", "manifest"))
    assert not paths.PAUSE_MANIFEST.exists() and not paths.DAEMON_STOP.exists()
    assert (paths.PAUSE / "19700101T000820Z-resumed.json").exists()


def test_resume_stops_before_daemon_stop_when_a_manager_does_not_register(fleet, monkeypatch, capsys):
    _manifest(fleet, [("m1", "proud-pegasus", "general"), ("m2", "silly-dragon", "meta")])
    monkeypatch.setenv("DOCKWRIGHT_REVIVE_READY_TIMEOUT_SEC", "0.2")
    ops = _resume_fixture(fleet, monkeypatch, never=("m2",))
    assert pause.main(["resume", "--no-attach"]) == 1
    assert paths.DAEMON_STOP.exists() and ("unlink", "daemon-stop") not in ops
    manifest = pause.read_manifest()
    assert manifest["managers"][0]["notice_delivered_at"] and manifest["managers"][1]["notice_delivered_at"] is None
    assert manifest["managers"][1]["resume_pane"] == "%nm2"
    out = capsys.readouterr().out
    assert "silly-dragon (meta) — resume of sid m2 NOT confirmed" in out
    assert "resume: daemon-stop stays until every manager is notified; run dockwright resume again" in out
    assert "undo the pause yourself" not in out and "launchctl" not in out


def test_resume_reports_a_spawn_failure_and_still_delivers_to_the_rest(fleet, monkeypatch, capsys):
    _manifest(fleet, [("m1", "proud-pegasus", "general"), ("m2", "silly-dragon", "meta")])
    ops = _resume_fixture(fleet, monkeypatch)
    real_spawn = pause.spawn_manager_window

    async def spawn(candidate, resume=True):
        if candidate.sid == "m2":
            raise TimeoutError("spawn timed out")
        return await real_spawn(candidate, resume=resume)

    monkeypatch.setattr(pause, "spawn_manager_window", spawn)
    assert pause.main(["resume", "--no-attach"]) == 1
    out = capsys.readouterr().out
    assert "resume: silly-dragon (meta) — NOT resumed — TimeoutError: spawn timed out" in out
    assert pause.read_manifest()["managers"][0]["notice_delivered_at"] and ("registered", "m1") in ops
    assert paths.DAEMON_STOP.exists()


def test_resume_names_a_live_manager_that_refused_the_paste_as_not_delivered(fleet, monkeypatch, capsys):
    _manifest(fleet, [("m1", "proud-pegasus", "general")])
    fleet.add(_manager("m1", "proud-pegasus", "general", "%5"))
    fleet.driver.send_ok = False
    _resume_fixture(fleet, monkeypatch)
    assert pause.main(["resume", "--no-attach"]) == 1
    out = capsys.readouterr().out
    assert "but the notice was NOT delivered" in out and "NOT confirmed" not in out


def test_resume_delivers_to_a_live_manager_without_a_window(fleet, monkeypatch):
    _manifest(fleet, [("m1", "proud-pegasus", "general")])
    fleet.add(_manager("m1", "proud-pegasus", "general", "%5"))
    ops = _resume_fixture(fleet, monkeypatch)
    assert pause.main(["resume", "--no-attach"]) == 0
    assert ("spawn", "m1") not in ops
    assert [c for c in fleet.driver.calls if c[0] == "send"][0][1] == "%5"
    assert (paths.PAUSE / "19700101T000820Z-resumed.json").exists()
    archived = json.loads((paths.PAUSE / "19700101T000820Z-resumed.json").read_text())
    assert archived["managers"][0]["notice_delivered_at"]


def test_resume_repeat_skips_delivered_managers(fleet, monkeypatch):
    manifest = _manifest(fleet, [("m1", "proud-pegasus", "general"), ("m2", "silly-dragon", "meta")])
    manifest["managers"][0]["notice_delivered_at"] = 600.0
    pause.write_manifest(manifest)
    ops = _resume_fixture(fleet, monkeypatch)
    assert pause.main(["resume", "--no-attach"]) == 0
    sends = [c for c in fleet.driver.calls if c[0] == "send"]
    assert len(sends) == 1 and sends[0][1] == "%nm2" and ("spawn", "m1") not in ops


def test_resume_repeat_reuses_an_open_window(fleet, monkeypatch):
    manifest = _manifest(fleet, [("m1", "proud-pegasus", "general")])
    manifest["managers"][0]["resume_pane"] = "%7"
    pause.write_manifest(manifest)
    fleet.driver.panes = [{"wm_class": "mgr", "tabs": [{"title": "x", "windows": [
        {"id": "%7", "title": "proud-pegasus", "start_command": "claude --resume m1 --agents ..."}]}]}]
    state.write_json_atomic(paths.ACTIVE / "m1.json", _manager("m1", "proud-pegasus", "general", "%7", pid=1))
    ops = _resume_fixture(fleet, monkeypatch)
    assert pause.main(["resume", "--no-attach"]) == 0
    assert ("spawn", "m1") not in ops
    assert [c for c in fleet.driver.calls if c[0] == "send"][0][1] == "%7"


def test_resume_reopens_when_the_remembered_pane_runs_something_else(fleet, monkeypatch):
    manifest = _manifest(fleet, [("m1", "proud-pegasus", "general")])
    manifest["managers"][0]["resume_pane"] = "%7"
    pause.write_manifest(manifest)
    fleet.driver.panes = [{"wm_class": "mgr", "tabs": [{"title": "x", "windows": [
        {"id": "%7", "title": "zsh", "start_command": "zsh"}]}]}]
    ops = _resume_fixture(fleet, monkeypatch)
    assert pause.main(["resume", "--no-attach"]) == 0
    assert ("spawn", "m1") in ops


@pytest.mark.parametrize("flag,hold_b,expect_a,expect_b", [
    (True, "created", "/start-a --go", True),
    (False, "created", "/start-a", True),
    (True, "existed", "/start-a --go", False),
])
def test_resume_prints_hold_commands_and_keeps_hold_files(fleet, monkeypatch, capsys, flag, hold_b, expect_a,
                                                          expect_b):
    fleet.cfg.write_text(BASE_TOML + '[pause.hold]\n"lane-a-stop" = "/start-a {lane-a-flag:--go}"\n'
                         '"lane-b-stop" = "/start-b"\n')
    _manifest(fleet, [("m1", "proud-pegasus", "general")], hold={"lane-a-stop": "created", "lane-b-stop": hold_b},
              flags={"lane-a-flag": flag})
    _resume_fixture(fleet, monkeypatch)
    assert pause.main(["resume", "--no-attach"]) == 0
    out = capsys.readouterr().out
    assert (fleet.root / "lane-a-stop").exists() and (fleet.root / "lane-b-stop").exists()
    assert f"resume:   {expect_a}\n" in out and "  --go" not in out
    assert ("/start-b" in out) is expect_b
    assert ("lane-b-stop" in out.split("arm them yourself")[0].rsplit("\n", 1)[-1]) is expect_b


def test_resume_removes_only_created_stop_files_and_enables_loaded_labels(fleet, monkeypatch):
    plist = fleet.root / "LaunchAgents" / "com.example.poller.plist"
    plist.parent.mkdir()
    plist.write_text("")
    other = fleet.root / "LaunchAgents" / "com.example.other.plist"
    other.write_text("")
    _manifest(fleet, [("m1", "proud-pegasus", "general")],
              stop_files={"daemon-stop": "created", "chat-archive-stop": "existed"},
              launchd={"com.example.poller": {"was_loaded": True, "plist": str(plist)},
                       "com.example.other": {"was_loaded": False, "plist": str(other)}})
    _resume_fixture(fleet, monkeypatch)
    assert pause.main(["resume", "--no-attach"]) == 0
    assert (fleet.root / "chat-archive-stop").exists() and not paths.DAEMON_STOP.exists()
    launchctl = [c[1:] for c in fleet.run.argv if c[0] == "launchctl"]
    assert launchctl == [["enable", f"gui/{UID}/com.example.poller"], ["bootstrap", f"gui/{UID}", str(plist)]]


def test_resume_reports_a_missing_plist_and_enables_the_rest(fleet, monkeypatch, capsys):
    missing = fleet.root / "LaunchAgents" / "com.example.poller.plist"
    other = fleet.root / "LaunchAgents" / "com.example.other.plist"
    other.parent.mkdir()
    other.write_text("")
    _manifest(fleet, [("m1", "proud-pegasus", "general")],
              launchd={"com.example.poller": {"was_loaded": True, "plist": str(missing)},
                       "com.example.other": {"was_loaded": True, "plist": str(other)}})
    _resume_fixture(fleet, monkeypatch)
    assert pause.main(["resume", "--no-attach"]) == 0
    out = capsys.readouterr().out
    assert "com.example.poller" in out and str(missing) in out
    assert ["launchctl", "bootstrap", f"gui/{UID}", str(other)] in fleet.run.argv


def test_resume_restamps_closed_at(fleet, monkeypatch):
    _manifest(fleet, [("m1", "proud-pegasus", "general")], workers=[("w1", "K-1-ship", "proud-pegasus", True)])
    state.write_json_atomic(paths.CLOSED / "w1.json", {"claude_sid": "w1", "closed_reason": "fleet-pause",
                                                      "closed_at": 500.0})
    _resume_fixture(fleet, monkeypatch)
    before = time.time()
    assert pause.main(["resume", "--no-attach"]) == 0
    closed = _closed("w1")
    assert closed["paused_at"] == 500.0
    assert closed["closed_at"] == closed["resumed_at"] >= before


def test_resume_reports_lost_gates(fleet, monkeypatch, capsys):
    _manifest(fleet, [("m1", "proud-pegasus", "general")])
    (paths.ARTIFACTS / "K").mkdir()
    (paths.ARTIFACTS / "K" / "gate.open.md").write_text("{}")
    (paths.ARTIFACTS / "L").mkdir()
    monkeypatch.setattr(pause, "_gate_state", lambda key: {"state": "lost", "node": "design"})
    _resume_fixture(fleet, monkeypatch)
    assert pause.main(["resume", "--no-attach"]) == 0
    out = capsys.readouterr().out
    assert "resume: gate of K/design is lost" in out and "L/" not in out


def test_notice_names_plain_workers_only(fleet):
    manifest = _manifest(fleet, [("m1", "proud-pegasus", "general")],
                         workers=[("w1", "K-1-ship", "proud-pegasus", True),
                                  ("w2", "scout-a", "proud-pegasus", False),
                                  ("w3", "scout-b", "proud-pegasus", False),
                                  ("w4", "scout-c", "silly-dragon", False)])
    for sid in ("w1", "w2", "w4"):
        state.write_json_atomic(paths.CLOSED / f"{sid}.json", {"claude_sid": sid, "closed_reason": "fleet-pause"})
    text = pause.pause_notice(manifest["managers"][0], manifest, 900.0)
    assert "scout-a" in text and "K-1-ship" not in text and "scout-b" not in text and "scout-c" not in text
    assert "manager proud-pegasus on domain general" in text and "never remove it" in text
    assert "do not resume them by hand" not in text and "NODE_PARKED" in text
    bare = _manifest(fleet, [("m1", "proud-pegasus", "general")])
    assert pause.PAUSE_NO_WORKERS_SENTENCE in pause.pause_notice(bare["managers"][0], bare, 900.0)


def test_notice_reads_pipeline_node_from_the_manifest(fleet):
    manifest = _manifest(fleet, [("m1", "proud-pegasus", "general")],
                         workers=[("w1", "K-1-ship", "proud-pegasus", True)])
    state.write_json_atomic(paths.CLOSED / "w1.json", {"claude_sid": "w1", "closed_reason": "fleet-pause"})
    assert "K-1-ship" not in pause.pause_notice(manifest["managers"][0], manifest, 900.0)
    manifest["workers"][0]["pipeline_node"] = False
    assert "K-1-ship" in pause.pause_notice(manifest["managers"][0], manifest, 900.0)


def test_notice_names_the_held_lanes(fleet):
    manifest = _manifest(fleet, [("m1", "proud-pegasus", "general")], hold={"lane-a-stop": "created"})
    text = pause.pause_notice(manifest["managers"][0], manifest, 900.0)
    assert "The lanes behind lane-a-stop stay off until the engineer lifts them himself." in text


def test_resume_refuses_without_a_manifest(fleet, capsys):
    assert pause.main(["resume", "--no-attach"]) == 1
    out = capsys.readouterr()
    assert "no pause manifest" in out.out + out.err


def test_resume_dry_run_touches_nothing(fleet, monkeypatch, capsys):
    _manifest(fleet, [("m1", "proud-pegasus", "general")])
    ops = _resume_fixture(fleet, monkeypatch)
    assert pause.main(["resume", "--dry-run"]) == 0
    assert ops == [] and paths.DAEMON_STOP.exists() and paths.PAUSE_MANIFEST.exists()
    assert "resume: would" in capsys.readouterr().out


def test_resume_survives_a_pause_that_died_before_stop_loops(fleet, monkeypatch):
    _manifest(fleet, [("m1", "proud-pegasus", "general")])
    paths.DAEMON_STOP.unlink()
    _resume_fixture(fleet, monkeypatch)
    assert pause.main(["resume", "--no-attach"]) == 0
    assert not paths.PAUSE_MANIFEST.exists()


def test_resume_attaches_unless_told_not_to(fleet, monkeypatch):
    _manifest(fleet, [("m1", "proud-pegasus", "general")])
    _resume_fixture(fleet, monkeypatch)
    monkeypatch.setattr(revive, "attach_to_manager_session", lambda: 7)
    assert pause.main(["resume"]) == 7


def test_resume_passes_the_old_name_and_domain_to_the_spawn(fleet, monkeypatch):
    _manifest(fleet, [("m1", "proud-pegasus", "general")])
    seen = {}

    async def spawn(candidate, resume=False):
        seen.update(sid=candidate.sid, name=candidate.name, domain=candidate.domain, resume=resume)
        state.write_json_atomic(paths.ACTIVE / "m1.json", _manager("m1", "proud-pegasus", "general", "%n", pid=1))
        fleet.live_pids.add(1)
        return "%n"

    _resume_fixture(fleet, monkeypatch)
    monkeypatch.setattr(pause, "spawn_manager_window", spawn)
    assert pause.main(["resume", "--no-attach"]) == 0
    assert seen == {"sid": "m1", "name": "proud-pegasus", "domain": "general", "resume": True}
