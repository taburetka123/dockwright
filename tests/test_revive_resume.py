import json
import os
from pathlib import Path

import pytest

from dockwright import paths, revive, state, terminal

LIVE_SESSION_PID = 777
OTHER_SESSION_PID = 778
IDLE_PANE = "──────────\n❯ \n──────────\n  manager  🎯 sunny-fairy · product\n"
BUSY_PANE = "· Mulling… (9m 27s · ↓ 38.1k tokens)\n──────────\n❯ \n──────────\n"
CLAUDE_COMMAND = "2.1.260"


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    root = tmp_path / "dockwright"
    monkeypatch.setattr(paths, "ROOT", root)
    monkeypatch.setattr(paths, "ACTIVE", root / "active")
    monkeypatch.setattr(paths, "CLOSED", root / "closed")
    monkeypatch.setattr(paths, "HANDOFFS", root / "handoffs")
    monkeypatch.setattr(paths, "QUESTIONS", root / "questions")
    monkeypatch.setattr(paths, "SPEND_LEDGER", root / "spend-ledger.jsonl")
    (root / "active").mkdir(parents=True)
    (root / "closed").mkdir()
    monkeypatch.setattr(revive, "record_pid_is_session",
                        lambda record: record.get("pid") == LIVE_SESSION_PID)
    monkeypatch.setattr(revive, "record_pid_is_any_session",
                        lambda record: record.get("pid") in (LIVE_SESSION_PID, OTHER_SESSION_PID))
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


def _worker(sid, name, started_at, parent, **extra):
    record = {"claude_sid": sid, "agent": "worker", "name": name,
              "started_at": started_at, "pid": 4243, "window_id": "%2",
              "cwd": "/tmp/wt", "runtime": "claude", "account": "b", "kind": "worker",
              "parent_manager_name": parent, "parent_name": parent,
              "last_turn_at": 150.0, "last_summary": "did a thing"}
    record.update(extra)
    return record


def _write_records(root, records):
    for record in records:
        state.write_json_atomic(root / "active" / f"{record['claude_sid']}.json", record)


def _snapshot(directory):
    return {str(p.relative_to(directory)): p.read_bytes()
            for p in sorted(Path(directory).rglob("*")) if p.is_file()}


def _panes(monkeypatch, screen=IDLE_PANE, send=True):
    events = []
    monkeypatch.setattr(terminal.TmuxDriver, "capture_screen_ansi",
                        lambda self, pane: screen)
    monkeypatch.setattr(terminal.TmuxDriver, "pane_current_command",
                        lambda self, pane: CLAUDE_COMMAND)

    def send_text_checked(self, pane, text):
        events.append((pane, text))
        return send

    monkeypatch.setattr(terminal.TmuxDriver, "send_text_checked", send_text_checked)
    return events


def _registering_spawn(monkeypatch, state_root, sids, pid=LIVE_SESSION_PID):
    real_spawn = terminal.TmuxDriver.spawn

    async def spawn(self, **kwargs):
        pane = await real_spawn(self, **kwargs)
        for sid in sids:
            path = state_root / "active" / f"{sid}.json"
            record = state.read_json(path)
            if record is not None:
                record["pid"] = pid
                state.write_json_atomic(path, record)
        return pane

    monkeypatch.setattr(terminal.TmuxDriver, "spawn", spawn)


def _new_windows(no_live_tmux):
    return [" ".join(argv) for argv in no_live_tmux.exec
            if "new-window" in argv or "new-session" in argv]


def _transcript(home, sid, mtime):
    log = Path(home) / ".claude" / "projects" / "-tmp" / f"{sid}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"user","message":{"content":"hi"}}\n')
    os.utime(log, (mtime, mtime))
    return log


def test_resume_reopens_the_manager_on_its_own_sid_name_and_domain(
        state_root, monkeypatch, no_live_tmux):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0)])
    _panes(monkeypatch)
    _registering_spawn(monkeypatch, state_root, ["g1"])
    assert revive.main(["--resume", "--no-attach"]) == 0
    windows = _new_windows(no_live_tmux)
    assert len(windows) == 1
    assert "--resume g1" in windows[0]
    assert "CLAUDE_AGENT=manager" in windows[0]
    assert "CLAUDE_WORKER_NAME=ghost-a" in windows[0]
    assert "CLAUDE_DOMAIN=product" in windows[0]
    assert "$(cat" not in windows[0]


def test_resume_moves_the_dead_workers_to_closed_and_names_the_managers_own(
        state_root, monkeypatch, no_live_tmux):
    _write_records(state_root, [
        _manager("g1", "ghost-a", "product", 100.0),
        _worker("w1", "wk-one", 100.0, "ghost-a"),
        _worker("w2", "wk-two", 100.0, "nobody"),
    ])
    manager_before = state.read_json(state_root / "active" / "g1.json")
    sent = _panes(monkeypatch)
    _registering_spawn(monkeypatch, state_root, ["g1"])
    assert revive.main(["--resume", "--no-attach"]) == 0
    assert sorted(p.name for p in (state_root / "active").glob("*.json")) == ["g1.json"]
    manager_after = state.read_json(state_root / "active" / "g1.json")
    manager_after["pid"] = manager_before["pid"]
    assert manager_after == manager_before
    closed = {p.stem: state.read_json(p) for p in (state_root / "closed").glob("*.json")}
    assert set(closed) == {"w1", "w2"}
    one = closed["w1"]
    assert one["closed_reason"] == revive.RESUME_CLOSED_REASON
    assert one["name"] == "wk-one"
    assert one["cwd"] == "/tmp/wt"
    assert one["runtime"] == "claude"
    assert one["kind"] == "worker"
    assert one["parent_manager_name"] == "ghost-a"
    assert one["account"] == "b"
    assert one["closed_at"] >= 200
    assert len(sent) == 1
    text = sent[0][1]
    assert "host reboot" in text
    assert "ghost-a" in text and "product" in text
    assert "wk-one" in text
    assert "wk-two" not in text
    assert "resume_worker" in text
    assert "Skill(dockwright:arm-lanes)" in text
    assert text.index("arm-lanes") < text.index("resume_worker")


def test_resume_leaves_a_worker_with_post_boot_transcript_activity_alone(
        state_root, monkeypatch, no_live_tmux):
    home = os.environ["HOME"]
    dead_log = _transcript(home, "w1", 150)
    live_log = _transcript(home, "w2", 300)
    _write_records(state_root, [
        _manager("g1", "ghost-a", "product", 100.0),
        _worker("w1", "wk-dead", 100.0, "ghost-a", transcript_path=str(dead_log)),
        _worker("w2", "wk-live", 100.0, "ghost-a", transcript_path=str(live_log)),
        _worker("w3", "wk-nested", 100.0, "ghost-a", nested=True),
    ])
    sent = _panes(monkeypatch)
    _registering_spawn(monkeypatch, state_root, ["g1"])
    assert revive.main(["--resume", "--no-attach"]) == 0
    assert sorted(p.stem for p in (state_root / "active").glob("*.json")) == ["g1", "w2", "w3"]
    assert [p.stem for p in (state_root / "closed").glob("*.json")] == ["w1"]
    assert state.read_json(state_root / "closed" / "w1.json")["transcript_path"] == str(dead_log)
    assert "wk-dead" in sent[0][1]
    assert "wk-live" not in sent[0][1]


def test_resume_writes_no_digest_for_a_resumed_manager_but_still_one_for_a_covered_ghost(
        state_root, monkeypatch, no_live_tmux, capsys):
    _write_records(state_root, [
        _manager("g1", "ghost-a", "product", 100.0),
        _manager("g2", "ghost-b", "ops", 100.0),
        _manager("l2", "fresh-ops", "ops", 300.0),
    ])
    _panes(monkeypatch)
    _registering_spawn(monkeypatch, state_root, ["g1"])
    assert revive.main(["--resume", "--no-attach"]) == 0
    assert not revive.digest_path("g1").exists()
    assert revive.digest_path("g2").exists()
    windows = _new_windows(no_live_tmux)
    assert len(windows) == 1 and "--resume g1" in windows[0]
    out = capsys.readouterr().out
    assert "resumed on sid g1 (registered, live pid), notice delivered" in out
    assert "1 ghost record(s) left untouched" in out


def test_a_manager_with_no_workers_is_told_there_is_nothing_to_resume(
        state_root, monkeypatch, no_live_tmux):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0)])
    sent = _panes(monkeypatch)
    _registering_spawn(monkeypatch, state_root, ["g1"])
    assert revive.main(["--resume", "--no-attach"]) == 0
    text = sent[0][1]
    assert "No workers were registered to you, so there is nothing to resume." in text
    assert "resume_worker" not in text


def test_a_resume_dry_run_moves_nothing_and_opens_nothing(
        state_root, monkeypatch, no_live_tmux, capsys):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0),
                                _worker("w1", "wk-one", 100.0, "ghost-a")])
    before = _snapshot(state_root)
    _panes(monkeypatch)
    assert revive.main(["--resume", "--dry-run", "--no-attach"]) == 0
    assert _snapshot(state_root) == before
    assert _new_windows(no_live_tmux) == []
    out = capsys.readouterr().out
    assert "would resume ghost-a (product) on sid g1" in out
    assert "would move 1 dead worker record(s)" in out
    assert "wk-one" in out


def test_an_undelivered_resume_notice_is_printed_for_hand_delivery(
        state_root, monkeypatch, no_live_tmux, capsys):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0),
                                _worker("w1", "wk-one", 100.0, "ghost-a")])
    sent = _panes(monkeypatch, screen=BUSY_PANE)
    _registering_spawn(monkeypatch, state_root, ["g1"])
    assert revive.main(["--resume", "--no-attach"]) == 0
    assert sent == []
    out = capsys.readouterr().out
    assert ("resumed on sid g1 (registered, live pid) but the notice was NOT delivered — "
            "a turn is running") in out
    assert "NOT confirmed" not in out
    assert "wk-one" in out
    assert [p.stem for p in (state_root / "closed").glob("*.json")] == ["w1"]


def test_a_window_that_cannot_be_opened_names_the_manual_resume(
        state_root, monkeypatch, no_live_tmux, capsys):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0)])

    async def refuse(candidate, resume=False):
        raise RuntimeError("tmux new-window failed")

    monkeypatch.setattr(revive, "spawn_manager_window", refuse)
    assert revive.main(["--resume", "--no-attach"]) == 1
    out = capsys.readouterr().out
    assert "NOT resumed" in out
    assert "claude --resume g1" in out


def test_the_archived_record_is_what_resume_worker_reads(
        state_root, monkeypatch, no_live_tmux):
    from dockwright import mcp_server
    _transcript(os.environ["HOME"], "w1", 150)
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0),
                                _worker("w1", "wk-one", 100.0, "ghost-a")])
    _panes(monkeypatch)
    _registering_spawn(monkeypatch, state_root, ["g1"])
    assert revive.main(["--resume", "--no-attach"]) == 0
    closed_path, record = mcp_server._find_closed_record_by_name("wk-one")
    assert closed_path == state_root / "closed" / "w1.json"
    assert record["claude_sid"] == "w1"
    assert state.parent_name_of(record) == "ghost-a"


def test_digest_mode_still_moves_no_record(state_root, monkeypatch, no_live_tmux):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0),
                                _worker("w1", "wk-one", 100.0, "ghost-a")])
    before = _snapshot(state_root / "active")
    _panes(monkeypatch)
    assert revive.main(["--no-attach"]) == 0
    assert _snapshot(state_root / "active") == before
    assert list((state_root / "closed").glob("*.json")) == []


def test_an_unregistered_resume_is_not_confirmed_and_receives_nothing(
        state_root, monkeypatch, no_live_tmux, capsys):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0)])
    sent = _panes(monkeypatch)
    assert revive.main(["--resume", "--no-attach"]) == 0
    assert sent == []
    out = capsys.readouterr().out
    assert "resume of sid g1 NOT confirmed" in out
    assert "not a live session" in out


def test_a_manager_whose_pid_is_a_live_session_is_not_resumed_again(
        state_root, monkeypatch, no_live_tmux, capsys):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0,
                                         pid=LIVE_SESSION_PID)])
    _panes(monkeypatch)
    assert revive.main(["--resume", "--no-attach"]) == 0
    assert _new_windows(no_live_tmux) == []
    out = capsys.readouterr().out
    assert "skip-alive ghost-a (product) — pid 777 is a live session" in out


def test_dead_workers_are_archived_even_when_no_manager_is_revivable(
        state_root, monkeypatch, no_live_tmux, capsys):
    _write_records(state_root, [_manager("l1", "fresh", "product", 300.0),
                                _worker("w1", "wk-one", 100.0, "ghost-a"),
                                _worker("w2", "wk-two", 100.0, "ghost-a")])
    _panes(monkeypatch)
    assert revive.main(["--resume", "--dry-run", "--no-attach"]) == 0
    assert "would move 2 dead worker record(s)" in capsys.readouterr().out
    assert revive.main(["--resume", "--no-attach"]) == 0
    assert sorted(p.stem for p in (state_root / "closed").glob("*.json")) == ["w1", "w2"]
    assert [p.stem for p in (state_root / "active").glob("*.json")] == ["l1"]
    assert _new_windows(no_live_tmux) == []
    assert "2 dead worker record(s) moved to closed/" in capsys.readouterr().out


def test_archiving_drops_the_workers_pending_questions_and_writes_no_ledger_line(
        state_root, monkeypatch, no_live_tmux):
    questions = state_root / "questions" / "ghost-a"
    questions.mkdir(parents=True)
    (questions / "q1.json").write_text(json.dumps({"worker_sid": "w1", "question": "?"}))
    (questions / "q2.json").write_text(json.dumps({"worker_sid": "other", "question": "?"}))
    _write_records(state_root, [
        _manager("g1", "ghost-a", "product", 100.0),
        _worker("w1", "wk-one", 100.0, "ghost-a",
                spend={"in_tokens": 1, "out_tokens": 1000, "cache_read_tokens": 0}),
    ])
    _panes(monkeypatch)
    _registering_spawn(monkeypatch, state_root, ["g1"])
    assert revive.main(["--resume", "--no-attach"]) == 0
    assert sorted(p.name for p in questions.glob("*.json")) == ["q2.json"]
    assert not (state_root / "spend-ledger.jsonl").exists()
    assert state.read_json(state_root / "closed" / "w1.json")["spend"]["out_tokens"] == 1000


def test_a_sid_less_worker_record_is_counted_by_neither_run(
        state_root, monkeypatch, no_live_tmux, capsys):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0)])
    state.write_json_atomic(state_root / "active" / "nosid.json",
                            {"agent": "worker", "name": "wk-nosid", "started_at": 100.0})
    _panes(monkeypatch)
    _registering_spawn(monkeypatch, state_root, ["g1"])
    assert revive.main(["--resume", "--dry-run", "--no-attach"]) == 0
    assert "would move 0 dead worker record(s)" in capsys.readouterr().out
    assert revive.main(["--resume", "--no-attach"]) == 0
    assert (state_root / "active" / "nosid.json").exists()
    assert "0 dead worker record(s) moved" in capsys.readouterr().out


def test_list_closed_workers_returns_the_closed_reason(
        state_root, monkeypatch, no_live_tmux):
    from dockwright import mcp_server
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0),
                                _worker("w1", "wk-one", 100.0, "ghost-a")])
    _panes(monkeypatch)
    _registering_spawn(monkeypatch, state_root, ["g1"])
    assert revive.main(["--resume", "--no-attach"]) == 0
    rows = mcp_server.list_closed_workers_impl()
    assert [r["closed_reason"] for r in rows if r["name"] == "wk-one"] == ["host-reboot"]


def test_the_resumed_window_opens_under_the_managers_own_title(
        state_root, monkeypatch, no_live_tmux):
    _write_records(state_root, [_manager("g1", "ghost-a", "product", 100.0)])
    _panes(monkeypatch)
    _registering_spawn(monkeypatch, state_root, ["g1"])
    assert revive.main(["--resume", "--no-attach"]) == 0
    assert any("ghost-a · product" in " ".join(argv) for argv in no_live_tmux.exec)


def test_a_worker_whose_pid_is_a_live_session_is_not_archived(
        state_root, monkeypatch, no_live_tmux, capsys):
    _write_records(state_root, [
        _manager("g1", "ghost-a", "product", 100.0),
        _worker("w1", "wk-dead", 100.0, "ghost-a"),
        _worker("w2", "wk-held", 100.0, "ghost-a", pid=OTHER_SESSION_PID),
    ])
    sent = _panes(monkeypatch)
    _registering_spawn(monkeypatch, state_root, ["g1"])
    assert revive.main(["--resume", "--dry-run", "--no-attach"]) == 0
    dry = capsys.readouterr()
    assert "would move 1 dead worker record(s)" in dry.out and "wk-dead" in dry.out
    assert "wk-held NOT archived — pid 778 is a live session" in dry.err
    assert revive.main(["--resume", "--no-attach"]) == 0
    real = capsys.readouterr()
    assert sorted(p.stem for p in (state_root / "active").glob("*.json")) == ["g1", "w2"]
    assert [p.stem for p in (state_root / "closed").glob("*.json")] == ["w1"]
    assert "wk-held NOT archived — pid 778 is a live session" in real.err
    assert "wk-dead" in sent[0][1] and "wk-held" not in sent[0][1]


def test_the_manager_liveness_check_is_anchored_on_its_own_sid(monkeypatch):
    monkeypatch.setattr(revive.state, "_pid_alive", lambda pid: pid == 570)
    commands = {570: "claude --dangerously-skip-permissions --resume 1220da23-1757"}
    monkeypatch.setattr(revive, "pid_command", lambda pid: commands.get(pid, ""))
    mine = {"claude_sid": "1220da23-1757", "pid": 570}
    someone_elses = {"claude_sid": "97ae2ad0-3628", "pid": 570}
    dead = {"claude_sid": "1220da23-1757", "pid": 571}
    assert revive.record_pid_is_session(mine) is True
    assert revive.record_pid_is_session(someone_elses) is False
    assert revive.record_pid_is_session(dead) is False
    assert revive.record_pid_is_any_session(someone_elses) is True
    assert revive.record_pid_is_any_session(dead) is False


def test_a_pid_collision_does_not_cover_the_domain(monkeypatch):
    monkeypatch.setattr(revive.state, "_pid_alive", lambda pid: pid == 570)
    monkeypatch.setattr(revive, "pid_command",
                        lambda pid: "claude --resume somebody-else" if pid == 570 else "")
    plan = revive.build_plan([_manager("g1", "ghost-a", "product", 100.0, pid=570),
                              _manager("g2", "ghost-b", "product", 90.0, pid=571)],
                             200, transcript_mtime=lambda record: None)
    assert [(c.name, c.verdict) for c in plan] == [("ghost-a", "revive"), ("ghost-b", "skip-covered")]


def test_an_implausible_boot_epoch_override_is_warned_about(
        state_root, monkeypatch, no_live_tmux, capsys):
    _panes(monkeypatch)
    assert revive.main(["--resume", "--dry-run", "--no-attach"]) == 0
    assert "WARNING boot epoch 200 comes from DOCKWRIGHT_BOOT_EPOCH" in capsys.readouterr().err
