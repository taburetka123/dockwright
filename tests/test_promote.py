import os
import sys

import pytest

import dockwright.promote as promote
import dockwright.spawner as spawner
from dockwright.promote import resolve_general_manager


def _always_alive(_pid):
    return True


def _pin_tmux(monkeypatch):
    """Legacy manager records carry no `terminal` stamp (= tmux default). Pin
    this process's backend to tmux so resolve_general_manager matches them;
    these tests verify selection/CLI logic, not the default backend."""
    import dockwright.terminal as terminal
    monkeypatch.setenv("CLAUDE_ORCH_TERMINAL", "tmux")
    terminal._DRIVER = None


def _arrange_spawn_failure(monkeypatch, exc):
    """Wire assign_to_manager_cli so it reaches the spawn call, where
    spawn_worker_tab raises `exc`. Uses a live pid so the manager resolves."""
    _pin_tmux(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["orchestrator", "assign-to-manager", "--sid", "abc12345"])
    monkeypatch.setattr(
        promote,
        "_read_active_records",
        lambda: [
            {"agent": "manager", "name": "m1", "pid": os.getpid(), "domain": "general", "started_at": 100}
        ],
    )

    async def _boom(**kwargs):
        raise exc

    monkeypatch.setattr(spawner, "spawn_worker_tab", _boom)


def test_spawn_oserror_exits_cleanly(monkeypatch, capsys):
    # tmux missing from PATH surfaces as FileNotFoundError (an OSError).
    _arrange_spawn_failure(monkeypatch, FileNotFoundError("tmux"))
    with pytest.raises(SystemExit) as exc:
        promote.assign_to_manager_cli()
    assert exc.value.code == 1
    assert "could not launch the worker tab via tmux" in capsys.readouterr().err


def test_spawn_timeout_exits_cleanly(monkeypatch, capsys):
    import asyncio

    _arrange_spawn_failure(monkeypatch, asyncio.TimeoutError())
    with pytest.raises(SystemExit) as exc:
        promote.assign_to_manager_cli()
    assert exc.value.code == 1
    assert "could not launch the worker tab via tmux" in capsys.readouterr().err


def test_returns_error_when_no_managers():
    records = [
        {"agent": "worker", "name": "w1", "pid": 1, "domain": None},
    ]
    chosen, others, error = resolve_general_manager(records, _always_alive)
    assert chosen is None
    assert others == []
    assert error and "No active general-domain manager" in error


def test_picks_single_general_manager():
    records = [
        {"agent": "manager", "name": "m1", "pid": 10, "domain": "general", "started_at": 100},
        {"agent": "worker", "name": "w1", "pid": 11, "domain": None},
    ]
    chosen, others, error = resolve_general_manager(records, _always_alive)
    assert error is None
    assert chosen["name"] == "m1"
    assert others == []


def test_absent_domain_counts_as_general():
    records = [
        {"agent": "manager", "name": "m1", "pid": 10, "started_at": 100},
    ]
    chosen, others, error = resolve_general_manager(records, _always_alive)
    assert error is None
    assert chosen["name"] == "m1"


def test_excludes_non_general_domains():
    records = [
        {"agent": "manager", "name": "frontend", "pid": 10, "domain": "frontend", "started_at": 100},
    ]
    chosen, others, error = resolve_general_manager(records, _always_alive)
    assert chosen is None
    assert error is not None


def test_picks_newest_when_multiple_and_lists_others():
    records = [
        {"agent": "manager", "name": "older", "pid": 10, "domain": "general", "started_at": 100},
        {"agent": "manager", "name": "newest", "pid": 11, "domain": None, "started_at": 300},
        {"agent": "manager", "name": "middle", "pid": 12, "domain": "general", "started_at": 200},
    ]
    chosen, others, error = resolve_general_manager(records, _always_alive)
    assert error is None
    assert chosen["name"] == "newest"
    assert [o["name"] for o in others] == ["middle", "older"]


def test_skips_dead_pid_managers():
    records = [
        {"agent": "manager", "name": "dead", "pid": 10, "domain": "general", "started_at": 300},
        {"agent": "manager", "name": "alive", "pid": 11, "domain": "general", "started_at": 100},
    ]
    chosen, others, error = resolve_general_manager(records, lambda pid: pid != 10)
    assert error is None
    assert chosen["name"] == "alive"
    assert others == []


def test_malformed_pid_does_not_crash_and_keeps_manager():
    records = [
        {"agent": "manager", "name": "m1", "pid": "bogus", "domain": "general", "started_at": 100},
    ]
    chosen, others, error = resolve_general_manager(records, lambda _pid: False)
    assert error is None
    assert chosen["name"] == "m1"


def test_all_managers_dead_returns_error():
    records = [
        {"agent": "manager", "name": "dead", "pid": 10, "domain": "general", "started_at": 300},
    ]
    chosen, others, error = resolve_general_manager(records, lambda _pid: False)
    assert chosen is None
    assert error is not None


def test_assign_to_manager_writes_assignment(monkeypatch, tmp_path, capsys):
    from dockwright import paths, state

    _pin_tmux(monkeypatch)
    monkeypatch.setattr(paths, "ASSIGNMENTS", tmp_path / "assignments")
    monkeypatch.setattr(paths, "ASSIGNMENTS_PENDING", tmp_path / "assignments" / ".pending")
    monkeypatch.setattr(sys, "argv", ["orchestrator", "assign-to-manager", "--sid", "abc12345"])
    monkeypatch.setattr(
        promote,
        "_read_active_records",
        lambda: [
            {"agent": "manager", "name": "m1", "pid": os.getpid(), "domain": "general", "started_at": 100}
        ],
    )

    async def _ok(**kwargs):
        return ("win-1", kwargs.get("name", ""))

    monkeypatch.setattr(spawner, "spawn_worker_tab", _ok)
    promote.assign_to_manager_cli()
    record = state.read_json(paths.ASSIGNMENTS / "abc12345.json")
    assert record["claude_sid"] == "abc12345"
    assert record["promoted"] is True
    assert record["initial_prompt"] is None
    assert record["parent_manager_name"] == "m1"


def test_assign_to_manager_keeps_existing_assignment(monkeypatch, tmp_path):
    from dockwright import paths, state

    _pin_tmux(monkeypatch)
    monkeypatch.setattr(paths, "ASSIGNMENTS", tmp_path / "assignments")
    paths.ASSIGNMENTS.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.ASSIGNMENTS / "abc12345.json",
                            {"claude_sid": "abc12345", "initial_prompt": "original"})
    monkeypatch.setattr(sys, "argv", ["orchestrator", "assign-to-manager", "--sid", "abc12345"])
    monkeypatch.setattr(
        promote,
        "_read_active_records",
        lambda: [
            {"agent": "manager", "name": "m1", "pid": os.getpid(), "domain": "general", "started_at": 100}
        ],
    )

    async def _ok(**kwargs):
        return ("win-1", kwargs.get("name", ""))

    monkeypatch.setattr(spawner, "spawn_worker_tab", _ok)
    promote.assign_to_manager_cli()
    record = state.read_json(paths.ASSIGNMENTS / "abc12345.json")
    assert record["initial_prompt"] == "original"
    assert "promoted" not in record


def test_assign_to_manager_task_key_stamps_ticket(monkeypatch, tmp_path):
    from dockwright import paths, state

    _pin_tmux(monkeypatch)
    monkeypatch.setattr(paths, "ASSIGNMENTS", tmp_path / "assignments")
    monkeypatch.setattr(paths, "ASSIGNMENTS_PENDING", tmp_path / "assignments" / ".pending")
    monkeypatch.setattr(sys, "argv", ["orchestrator", "assign-to-manager",
                                      "--sid", "abc12345", "--task-key", "yt-bot-public"])
    monkeypatch.setattr(
        promote,
        "_read_active_records",
        lambda: [
            {"agent": "manager", "name": "m1", "pid": os.getpid(), "domain": "general", "started_at": 100}
        ],
    )

    async def _ok(**kwargs):
        return ("win-1", kwargs.get("name", ""))

    monkeypatch.setattr(spawner, "spawn_worker_tab", _ok)
    promote.assign_to_manager_cli()
    record = state.read_json(paths.ASSIGNMENTS / "abc12345.json")
    assert record["ticket"] == "yt-bot-public"


def test_assign_to_manager_rejects_path_hostile_task_key(monkeypatch, tmp_path, capsys):
    from dockwright import paths, state

    monkeypatch.setattr(paths, "ASSIGNMENTS", tmp_path / "assignments")
    monkeypatch.setattr(sys, "argv", ["orchestrator", "assign-to-manager",
                                      "--sid", "abc12345", "--task-key", "yt bot"])
    monkeypatch.setattr(
        promote,
        "_read_active_records",
        lambda: [
            {"agent": "manager", "name": "m1", "pid": os.getpid(), "domain": "general", "started_at": 100}
        ],
    )
    with pytest.raises(SystemExit) as exc:
        promote.assign_to_manager_cli()
    assert exc.value.code == 1
    assert "task-key" in capsys.readouterr().err
    assert not (tmp_path / "assignments" / "abc12345.json").exists()
