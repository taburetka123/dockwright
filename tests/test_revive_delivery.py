import asyncio
import json
import os
import shlex
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from dockwright import paths, revive, spawner, state, terminal

LIVE_PANE = ("⏺ Background command completed (exit code 0)\n"
             "· Mulling… (9m 27s · ↓ 38.1k tokens)\n"
             "──────────\n❯ \n──────────\n  repo  branch\n")
IDLE_PANE = "──────────\n❯ \n──────────\n  manager  🎯 sunny-fairy · product\n"
TYPING_PANE = ("──────────\n❯ where did the deploy go\n──────────\n"
               "  manager  🎯 sunny-fairy · product\n")
DIM_PLACEHOLDER_PANE = ("──────────\n❯ \x1b[2mTry \"fix the build\"\x1b[0m\n"
                        "──────────\n  manager  🎯 sunny-fairy · product\n")
STRIPPED_PLACEHOLDER_PANE = ("──────────\n❯ Try \"fix the build\"\n──────────\n"
                             "  manager  🎯 sunny-fairy · product\n")
CLAUDE_COMMAND = "2.1.260"
DEAD_PID = 999999


def test_shell_prompt_is_not_ready():
    assert revive.is_ready("~/projects/x on main\n❯ \n", "zsh") is False


def test_busy_claude_pane_is_not_ready():
    assert revive.is_ready(LIVE_PANE, "2.1.260") is False


def test_unreadable_pane_command_is_not_ready():
    assert revive.is_ready(IDLE_PANE, "") is False


def test_a_half_typed_input_is_not_ready():
    assert revive.is_ready(TYPING_PANE, "2.1.260") is False


def test_idle_claude_pane_is_ready():
    assert revive.is_ready(IDLE_PANE, "2.1.260") is True


def _tmux_results(monkeypatch, **by_verb):
    calls = []

    def fake_run(argv, *pargs, **kwargs):
        argv = [str(a) for a in argv]
        calls.append(argv)
        for verb, (code, err) in by_verb.items():
            if verb in argv:
                return subprocess.CompletedProcess(argv, code, "", err)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(revive.subprocess, "run", fake_run)
    return calls


def test_a_switch_client_that_fails_leaves_a_delivered_revive_successful(
        monkeypatch, capsys):
    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/default,4242,0")
    calls = _tmux_results(monkeypatch,
                          **{"switch-client": (1, "no current client")})
    assert revive.attach_to_manager_session() == 0
    err = capsys.readouterr().err
    assert "no current client" in err
    assert "attach -t" in err
    assert any("switch-client" in argv for argv in calls)


def test_a_missing_manager_session_is_still_a_failure(monkeypatch, capsys):
    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/default,4242,0")
    _tmux_results(monkeypatch, **{"has-session": (1, "")})
    assert revive.attach_to_manager_session() == 1
    assert "no 'mgr' tmux session" in capsys.readouterr().err


class _DeliveryDriver:
    def __init__(self, send):
        self.send = send
        self.sent = []

    def capture_screen_ansi(self, pane):
        return IDLE_PANE

    def pane_current_command(self, pane):
        return CLAUDE_COMMAND

    def send_text_checked(self, pane, text):
        self.sent.append((pane, text))
        return self.send


@pytest.mark.parametrize("send", [True, False])
def test_deliver_marks_a_delivered_window(monkeypatch, tmp_path, capsys, send):
    fake = _DeliveryDriver(send)
    monkeypatch.setattr(revive, "get_driver", lambda: fake)
    monkeypatch.setattr(revive, "READY_POLL_SEC", 0)
    monkeypatch.setenv("DOCKWRIGHT_REVIVE_READY_TIMEOUT_SEC", "0")
    digest = tmp_path / "d.md"
    digest.write_text("digest")
    candidate = revive.Candidate(record={}, sid="s1", name="mgr-a", domain="general",
                                 verdict="revive", reason="x")
    w = revive.Window(candidate, digest=digest, pane="%3", text="hello", resume=False)
    asyncio.run(revive.deliver([w], prefix="resume"))
    assert w.delivered is send
    assert fake.sent == [("%3", "hello")]
    assert capsys.readouterr().out.startswith("resume: ")


def test_deliver_skips_the_confirmation_of_a_window_marked_confirmed(monkeypatch, tmp_path):
    fake = _DeliveryDriver(True)
    monkeypatch.setattr(revive, "get_driver", lambda: fake)
    monkeypatch.setattr(revive, "READY_POLL_SEC", 0)
    monkeypatch.setattr(paths, "ACTIVE", tmp_path / "no-active")
    monkeypatch.setenv("DOCKWRIGHT_REVIVE_READY_TIMEOUT_SEC", "0")
    candidate = revive.Candidate(record={}, sid="s1", name="mgr-a", domain="general", verdict="revive", reason="x")
    w = revive.Window(candidate, digest=None, pane="%3", text="hello", resume=True, registered=True)
    asyncio.run(revive.deliver([w]))
    assert w.delivered is True and fake.sent == [("%3", "hello")]


def test_deliver_rechecks_the_registration_on_the_confirming_poll(monkeypatch, capsys):
    fake = _DeliveryDriver(True)
    monkeypatch.setattr(revive, "get_driver", lambda: fake)
    monkeypatch.setattr(revive, "READY_POLL_SEC", 0)
    monkeypatch.setenv("DOCKWRIGHT_REVIVE_READY_TIMEOUT_SEC", "0")
    answers = iter([None, "the registered pid is gone"])
    monkeypatch.setattr(revive, "resume_not_confirmed_reason", lambda sid: next(answers))
    candidate = revive.Candidate(record={}, sid="s1", name="mgr-a", domain="general", verdict="revive", reason="x")
    w = revive.Window(candidate, digest=None, pane="%3", text="hello", resume=True)
    asyncio.run(revive.deliver([w]))
    assert w.delivered is False and fake.sent == []
    assert "the registered pid is gone on the confirming poll" in capsys.readouterr().out


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    root = tmp_path / "dockwright"
    monkeypatch.setattr(paths, "ROOT", root)
    monkeypatch.setattr(paths, "ACTIVE", root / "active")
    monkeypatch.setattr(paths, "CLOSED", root / "closed")
    monkeypatch.setattr(paths, "HANDOFFS", root / "handoffs")
    monkeypatch.setattr(revive, "record_pid_is_session", lambda record: False)
    monkeypatch.setattr(revive, "record_pid_is_any_session", lambda record: False)
    (root / "active").mkdir(parents=True)
    (root / "closed").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DOCKWRIGHT_BOOT_EPOCH", "200")
    monkeypatch.setenv("DOCKWRIGHT_REVIVE_READY_TIMEOUT_SEC", "0")
    monkeypatch.setattr(revive, "READY_POLL_SEC", 0)
    monkeypatch.setattr(revive, "attach_to_manager_session",
                        lambda: pytest.fail("revive attached the terminal"))
    return root


def _manager(sid, name, domain, started_at, **extra):
    record = {"claude_sid": sid, "agent": "manager", "name": name, "domain": domain,
              "started_at": started_at, "pid": 4242, "window_id": "%1",
              "cwd": "/tmp", "runtime": "claude", "account": "a", "state": "idle"}
    record.update(extra)
    return record


def _write_records(root, records):
    for record in records:
        state.write_json_atomic(root / "active" / f"{record['claude_sid']}.json", record)


def _snapshot(directory):
    snap = {}
    for path in sorted(Path(directory).rglob("*")):
        if path.is_file():
            snap[str(path.relative_to(directory))] = path.read_bytes()
    return snap


def _panes(monkeypatch, screen=IDLE_PANE, command=CLAUDE_COMMAND, send=None,
           plain=None):
    events = []

    def capture_screen_ansi(self, pane):
        events.append(("screen", pane))
        return screen(pane) if callable(screen) else screen

    def capture_screen(self, pane):
        if plain is None:
            pytest.fail("revive read the plain capture instead of the ansi one")
        return plain

    def pane_current_command(self, pane):
        return command(pane) if callable(command) else command

    monkeypatch.setattr(terminal.TmuxDriver, "capture_screen_ansi", capture_screen_ansi)
    monkeypatch.setattr(terminal.TmuxDriver, "capture_screen", capture_screen)
    monkeypatch.setattr(terminal.TmuxDriver, "pane_current_command",
                        pane_current_command)
    if send is not None:
        def send_text_checked(self, pane, text):
            events.append(("send", pane, text))
            return send
        monkeypatch.setattr(terminal.TmuxDriver, "send_text_checked", send_text_checked)
    return events


def _sent_texts(no_live_tmux):
    return [payload for argv, payload in zip(no_live_tmux.run, no_live_tmux.run_input)
            if "load-buffer" in argv and payload]


def _new_windows(no_live_tmux):
    return [argv for argv in no_live_tmux.exec
            if "new-window" in argv or "new-session" in argv]


def test_every_revived_manager_receives_its_own_digest_path(
        state_root, monkeypatch, capsys, no_live_tmux):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0),
                                _manager("g2", "ghost-b", "ops", 100.0)])
    _panes(monkeypatch)
    assert revive.main(["--no-attach"]) == 0
    sent = _sent_texts(no_live_tmux)
    assert len(sent) == 2
    out = capsys.readouterr().out
    for sid, name, domain in (("g1", "ghost-a", "product"),
                              ("g2", "ghost-b", "ops")):
        digest = revive.digest_path(sid)
        assert any(name in text and str(digest) in text for text in sent), sent
        assert f"{name} ({domain}) — delivered → {digest}" in out, out
    assert revive.digest_path("g1").is_file()
    assert revive.digest_path("g2").is_file()


def test_a_pane_whose_placeholder_is_dim_is_idle_and_receives_the_instruction(
        state_root, monkeypatch, no_live_tmux):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0)])
    _panes(monkeypatch, screen=DIM_PLACEHOLDER_PANE,
           plain=STRIPPED_PLACEHOLDER_PANE)
    assert revive.main(["--no-attach"]) == 0
    assert len(_sent_texts(no_live_tmux)) == 1
    assert revive.is_ready(STRIPPED_PLACEHOLDER_PANE, CLAUDE_COMMAND) is False


def test_a_manager_still_registering_its_domain_stops_the_whole_run(
        state_root, monkeypatch, capsys, no_live_tmux):
    _write_records(state_root, [
        _manager("b1", "will-be-product", None, 300.0, pid=os.getpid(),
                 window_id="%7", cwd="/tmp/booting"),
        _manager("g1", "dead-general", "general", 100.0),
    ])
    _panes(monkeypatch)
    assert revive.main(["--no-attach"]) == 4
    assert _new_windows(no_live_tmux) == []
    assert _sent_texts(no_live_tmux) == []
    assert not revive.digest_dir().exists()
    captured = capsys.readouterr()
    assert "will-be-product has not registered a domain" in captured.err
    assert "pane %7" in captured.err
    assert "cwd /tmp/booting" in captured.err
    assert "(general)" not in captured.err
    assert "still loading, re-run in a few seconds" in captured.err
    assert "not going to run /manager, finish or close it" in captured.err
    assert "already covered" not in captured.out
    assert "skip-covered" not in captured.out


def test_a_dry_run_still_prints_the_plan_while_a_manager_registers(
        state_root, monkeypatch, capsys, no_live_tmux):
    _write_records(state_root, [
        _manager("b1", "will-be-product", None, 300.0, pid=os.getpid()),
        _manager("g1", "dead-general", "general", 100.0),
    ])
    _panes(monkeypatch, send=True)
    assert revive.main(["--dry-run"]) == 4
    assert not revive.digest_dir().exists()
    assert _new_windows(no_live_tmux) == []
    captured = capsys.readouterr()
    assert "has not registered a domain" in captured.err
    assert "NOT trustworthy" in captured.out
    assert "revive dead-general (general)" in captured.out
    assert "1 ghost record(s) left untouched" in captured.out


def test_a_resumed_manager_with_no_domain_halts_the_run_too(
        state_root, monkeypatch, capsys, no_live_tmux):
    log = state_root / "resumed.jsonl"
    log.write_text("{}\n")
    os.utime(log, (300.0, 300.0))
    _write_records(state_root, [
        _manager("r1", "resumed-no-domain", None, 100.0, pid=os.getpid(),
                 transcript_path=str(log)),
        _manager("g1", "dead-product", "product", 100.0),
    ])
    _panes(monkeypatch)
    assert revive.main(["--no-attach"]) == 4
    assert _new_windows(no_live_tmux) == []
    assert not revive.digest_dir().exists()
    assert "resumed-no-domain has not registered a domain" in capsys.readouterr().err


def test_a_dead_record_with_no_domain_blocks_nothing(
        state_root, monkeypatch, capsys, no_live_tmux):
    _write_records(state_root, [
        _manager("b1", "never-registered", None, 300.0, pid=DEAD_PID),
        _manager("g1", "dead-general", "general", 100.0),
    ])
    _panes(monkeypatch)
    assert revive.main(["--no-attach"]) == 0
    assert len(_new_windows(no_live_tmux)) == 1
    assert revive.digest_path("g1").is_file()
    assert "dead-general (general) — delivered" in capsys.readouterr().out


def test_a_pane_that_goes_busy_on_the_confirming_poll_receives_nothing(
        state_root, monkeypatch, capsys, no_live_tmux):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0)])
    polls = []

    def screen(pane):
        polls.append(pane)
        return IDLE_PANE if len(polls) == 1 else LIVE_PANE

    _panes(monkeypatch, screen=screen)
    assert revive.main(["--no-attach"]) == 0
    assert _sent_texts(no_live_tmux) == []
    assert len(polls) >= 2
    out = capsys.readouterr().out
    assert "NOT delivered" in out
    assert "a turn is running on the confirming poll" in out


def test_the_boot_line_and_the_delivery_line_carry_what_the_reader_needs(
        state_root, monkeypatch, capsys, no_live_tmux):
    _write_records(state_root, [
        _manager("g1", "ghost-a", "product", 100.0),
        {"claude_sid": "w1", "agent": "worker", "name": "wk-one", "started_at": 100.0,
         "cwd": "/tmp/wt", "parent_manager_name": "ghost-a"},
    ])
    _panes(monkeypatch)
    assert revive.main(["--no-attach"]) == 0
    out = capsys.readouterr().out
    stamp = datetime.fromtimestamp(200).strftime("%Y-%m-%d %H:%M:%S")
    assert f"revive: boot epoch 200 ({stamp})" in out
    digest = revive.digest_path("g1")
    assert (f"delivered → {digest} ({digest.stat().st_size / 1024:.1f} KB)") in out
    assert "revive: 1 dead worker(s) reported inside the digests" in out


def test_a_busy_pane_receives_nothing_and_is_reported_undelivered(
        state_root, monkeypatch, capsys, no_live_tmux):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0)])
    _panes(monkeypatch, screen=LIVE_PANE)
    assert revive.main(["--no-attach"]) == 0
    assert _sent_texts(no_live_tmux) == []
    assert len(_new_windows(no_live_tmux)) == 1
    out = capsys.readouterr().out
    assert "NOT delivered" in out
    assert "a turn is running" in out
    assert str(revive.digest_path("g1")) in out


def test_a_shell_pane_receives_nothing_and_names_the_signal_that_failed(
        state_root, monkeypatch, capsys, no_live_tmux):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0)])
    _panes(monkeypatch, screen="~/projects/x on main\n❯ \n", command="zsh")
    assert revive.main(["--no-attach"]) == 0
    assert _sent_texts(no_live_tmux) == []
    out = capsys.readouterr().out
    assert "NOT delivered" in out
    assert "not Claude Code" in out


def test_a_refused_send_is_reported_not_swallowed(
        state_root, monkeypatch, capsys, no_live_tmux):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0)])
    events = _panes(monkeypatch, send=False)
    assert revive.main(["--no-attach"]) == 0
    assert sum(1 for event in events if event[0] == "send") == 1
    out = capsys.readouterr().out
    assert "NOT delivered" in out
    assert "tmux refused the paste" in out
    assert str(revive.digest_path("g1")) in out


def test_every_manager_in_the_plan_gets_a_delivery_line(
        state_root, monkeypatch, capsys, no_live_tmux):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0),
                                _manager("g2", "ghost-b", "ops", 100.0),
                                _manager("g3", "ghost-c", "sales", 100.0)])

    delivered = []

    def screen(pane):
        return IDLE_PANE if len(delivered) < 2 else LIVE_PANE

    def send_text_checked(self, pane, text):
        delivered.append(text)
        return True

    monkeypatch.setattr(terminal.TmuxDriver, "send_text_checked", send_text_checked)
    _panes(monkeypatch, screen=screen)
    assert revive.main(["--no-attach"]) == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if "delivered" in line]
    assert len(lines) == 3
    assert sum(1 for line in lines if "NOT delivered" in line) == 1
    for name in ("ghost-a", "ghost-b", "ghost-c"):
        assert any(name in line for line in lines), out


def test_the_absorbed_spawn_argv_carries_the_domain(
        state_root, monkeypatch, no_live_tmux):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0)])
    _panes(monkeypatch)
    assert revive.main(["--no-attach"]) == 0
    windows = _new_windows(no_live_tmux)
    assert len(windows) == 1
    assert "CLAUDE_DOMAIN=product" in " ".join(windows[0])
    prompt_files = list(paths.PROMPT_FILES.glob("*.txt"))
    assert [p.read_text() for p in prompt_files] == ["/manager product"]
    assert f'"$(cat {shlex.quote(str(prompt_files[0]))})"' in " ".join(windows[0])


def test_all_windows_are_spawned_before_any_pane_is_polled(
        state_root, monkeypatch, no_live_tmux):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0),
                                _manager("g2", "ghost-b", "ops", 100.0)])
    order = []
    real_spawn = terminal.TmuxDriver.spawn

    async def spawn(self, **kwargs):
        order.append("spawn")
        return await real_spawn(self, **kwargs)

    monkeypatch.setattr(terminal.TmuxDriver, "spawn", spawn)

    def screen(pane):
        order.append("poll")
        return IDLE_PANE

    _panes(monkeypatch, screen=screen)
    assert revive.main(["--no-attach"]) == 0
    assert order[:2] == ["spawn", "spawn"]
    assert "poll" in order


def test_an_unresolved_transcript_warns_and_still_opens_the_window(
        state_root, monkeypatch, capsys, no_live_tmux):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0)])
    _panes(monkeypatch)
    assert revive.main(["--no-attach"]) == 0
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "ghost-a" in captured.err
    assert len(_new_windows(no_live_tmux)) == 1
    assert "NO TRANSCRIPT RESOLVED" in revive.digest_path("g1").read_text()


def test_a_resolved_transcript_is_reduced_into_the_digest(
        state_root, tmp_path, monkeypatch, no_live_tmux):
    log = tmp_path / "g1.jsonl"
    log.write_text(json.dumps({"type": "user", "message": {"content": "pick this up"}})
                   + "\n")
    os.utime(log, (100.0, 100.0))
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0,
                                         transcript_path=str(log))])
    _panes(monkeypatch)
    assert revive.main(["--no-attach"]) == 0
    digest = revive.digest_path("g1").read_text()
    assert "pick this up" in digest
    assert "NO TRANSCRIPT RESOLVED" not in digest


def test_a_truncated_digest_says_so_on_the_console_not_only_in_its_header(
        state_root, tmp_path, monkeypatch, capsys, no_live_tmux):
    log = tmp_path / "g1.jsonl"
    log.write_text("".join(
        json.dumps({"type": "user", "message": {"content": "turn " + "x" * 200}}) + "\n"
        for _ in range(60)))
    os.utime(log, (100.0, 100.0))
    monkeypatch.setenv("DOCKWRIGHT_REVIVE_DIGEST_MAX_BYTES", "3000")
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0,
                                         transcript_path=str(log))])
    _panes(monkeypatch)
    assert revive.main(["--no-attach"]) == 0
    out = capsys.readouterr().out
    assert "TRUNCATED" in revive.digest_path("g1").read_text()
    assert "truncated," in out
    assert "KB dropped)" in out


def test_dry_run_writes_nothing_and_spawns_nothing(
        state_root, monkeypatch, capsys, no_live_tmux):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0),
                                _manager("g2", "ghost-b", "ops", 100.0)])
    before = _snapshot(state_root)
    events = _panes(monkeypatch, send=True)
    assert revive.main(["--dry-run"]) == 0
    assert _snapshot(state_root) == before
    assert not revive.digest_dir().exists()
    assert _new_windows(no_live_tmux) == []
    assert events == []
    out = capsys.readouterr().out
    assert "ghost-a" in out and "ghost-b" in out


def test_a_window_that_cannot_be_opened_fails_the_run_and_names_the_digest(
        state_root, monkeypatch, capsys, no_live_tmux):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0)])

    async def refuse(candidate):
        raise RuntimeError("tmux new-window failed")

    monkeypatch.setattr(revive, "spawn_manager_window", refuse)
    assert revive.main(["--no-attach"]) == 1
    out = capsys.readouterr().out
    assert "NOT delivered" in out
    assert "tmux new-window failed" in out
    assert str(revive.digest_path("g1")) in out


def test_a_wedged_spawn_is_bounded_and_names_the_digest(
        state_root, monkeypatch, capsys, no_live_tmux):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0)])

    async def hang(**kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(spawner, "spawn_worker_tab", hang)
    monkeypatch.setattr(revive, "SPAWN_TIMEOUT_SEC", 0.05)
    _panes(monkeypatch)
    assert revive.main(["--no-attach"]) == 1
    out = capsys.readouterr().out
    assert "NOT delivered" in out
    assert "TimeoutError" in out
    assert str(revive.digest_path("g1")) in out


def test_a_no_ghost_run_exits_zero_and_never_reaches_for_the_terminal(
        state_root, monkeypatch, capsys, no_live_tmux):
    _write_records(state_root, [_manager("l1", "fresh", "product", 300.0)])
    attempted = []

    def attach():
        attempted.append(True)
        return 1

    monkeypatch.setattr(revive, "attach_to_manager_session", attach)
    assert revive.main([]) == 0
    assert attempted == []
    assert _new_windows(no_live_tmux) == []
    assert "no pre-reboot ghosts" in capsys.readouterr().out


def test_an_unresolvable_boot_epoch_exits_three_and_spawns_nothing(
        state_root, monkeypatch, capsys, no_live_tmux):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0)])
    monkeypatch.setenv("DOCKWRIGHT_BOOT_EPOCH", "not-a-number")
    _panes(monkeypatch, send=True)
    assert revive.main(["--no-attach"]) == 3
    assert _new_windows(no_live_tmux) == []
    assert not revive.digest_dir().exists()
    assert "not-a-number" in capsys.readouterr().err


def test_a_covered_domain_is_digested_but_never_spawned(
        state_root, monkeypatch, capsys, no_live_tmux):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0),
                                _manager("l1", "fresh", "product", 300.0)])
    _panes(monkeypatch)
    assert revive.main(["--no-attach"]) == 0
    assert _new_windows(no_live_tmux) == []
    assert revive.digest_path("g1").is_file()
    assert not revive.digest_path("l1").exists()
    out = capsys.readouterr().out
    assert "skip-covered ghost-a (product) — already covered by fresh" in out
    assert f"ghost-a (product) — its transcript → {revive.digest_path('g1')}" in out


def test_the_ghost_that_loses_a_shared_domain_is_still_digested_and_named(
        state_root, monkeypatch, capsys, no_live_tmux):
    _write_records(state_root, [_manager("g1", "old-crash", "product", 100.0),
                                _manager("g2", "died-in-reboot", "product", 190.0)])
    _panes(monkeypatch)
    assert revive.main(["--no-attach"]) == 0
    assert len(_new_windows(no_live_tmux)) == 1
    assert revive.digest_path("g1").is_file()
    assert revive.digest_path("g2").is_file()
    out = capsys.readouterr().out
    assert "already covered by died-in-reboot" in out
    assert f"old-crash (product) — its transcript → {revive.digest_path('g1')}" in out
    assert "revive: 2 ghost record(s) left untouched" in out
    sent = _sent_texts(no_live_tmux)
    assert len(sent) == 1
    assert str(revive.digest_path("g2")) in sent[0]


def test_a_covered_ghosts_digest_names_its_dead_worker_rather_than_none(
        state_root, monkeypatch, no_live_tmux):
    _write_records(state_root, [
        _manager("g1", "old-crash", "product", 100.0),
        _manager("g2", "died-in-reboot", "product", 190.0),
        {"claude_sid": "w1", "agent": "worker", "name": "wk-one", "started_at": 100.0,
         "cwd": "/tmp/wt", "parent_manager_name": "old-crash"},
    ])
    _panes(monkeypatch)
    assert revive.main(["--no-attach"]) == 0
    covered = revive.digest_path("g1").read_text()
    assert "## Dead workers of old-crash" in covered
    assert "wk-one" in covered
    assert "(none)" not in covered
    assert "(none)" in revive.digest_path("g2").read_text()
    assert not revive.unattributed_digest_path().exists()


def test_a_covered_ghosts_truncation_is_named_on_the_console(
        state_root, tmp_path, monkeypatch, capsys, no_live_tmux):
    log = tmp_path / "g1.jsonl"
    log.write_text("".join(
        json.dumps({"type": "user", "message": {"content": "turn " + "x" * 200}}) + "\n"
        for _ in range(60)))
    os.utime(log, (100.0, 100.0))
    monkeypatch.setenv("DOCKWRIGHT_REVIVE_DIGEST_MAX_BYTES", "3000")
    _write_records(state_root, [
        _manager("g1", "old-crash", "product", 100.0, transcript_path=str(log)),
        _manager("g2", "died-in-reboot", "product", 190.0),
    ])
    _panes(monkeypatch)
    assert revive.main(["--no-attach"]) == 0
    lines = capsys.readouterr().out.splitlines()
    pointer = next(line for line in lines if "its transcript" in line)
    assert str(revive.digest_path("g1")) in pointer
    assert "truncated," in pointer
    assert "KB dropped)" in pointer
    verdict = next(line for line in lines if "skip-covered" in line)
    assert str(revive.digest_path("g1")) not in verdict
    assert lines.index(verdict) < lines.index(pointer)
    assert "TRUNCATED" in revive.digest_path("g1").read_text()


def test_a_dry_run_over_a_covered_domain_writes_nothing(
        state_root, monkeypatch, capsys, no_live_tmux):
    _write_records(state_root, [_manager("g1", "old-crash", "product", 100.0),
                                _manager("g2", "died-in-reboot", "product", 190.0)])
    before = _snapshot(state_root)
    _panes(monkeypatch, send=True)
    assert revive.main(["--dry-run"]) == 0
    assert _snapshot(state_root) == before
    assert not revive.digest_dir().exists()
    assert _new_windows(no_live_tmux) == []
    out = capsys.readouterr().out
    assert f"would write {revive.digest_path('g1')} for old-crash (product)" in out


def test_dead_workers_with_no_revived_manager_land_in_the_unattributed_file(
        state_root, monkeypatch, capsys, no_live_tmux):
    _write_records(state_root, [
        _manager("g1", "ghost-a", "product", 100.0),
        {"claude_sid": "w1", "agent": "worker", "name": "wk-one", "started_at": 100.0,
         "cwd": "/tmp/wt", "parent_manager_name": "nobody"},
    ])
    _panes(monkeypatch)
    assert revive.main(["--no-attach"]) == 0
    body = revive.unattributed_digest_path().read_text()
    assert "wk-one" in body
    assert str(revive.unattributed_digest_path()) in capsys.readouterr().out


def test_revive_removes_no_active_record(state_root, monkeypatch, no_live_tmux):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0),
                                _manager("l1", "fresh", "general", 300.0)])
    before = _snapshot(state_root / "active")
    _panes(monkeypatch)
    assert revive.main(["--no-attach"]) == 0
    assert _snapshot(state_root / "active") == before
