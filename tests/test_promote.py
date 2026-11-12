import os
import sys

import pytest

import dockwright.promote as promote
import dockwright.spawner as spawner
from dockwright.promote import resolve_general_manager


def _always_alive(_pid):
    return True


def _pin_tmux(monkeypatch):
    import dockwright.terminal as terminal
    monkeypatch.setenv("CLAUDE_ORCH_TERMINAL", "tmux")
    terminal._DRIVER = None


def _arrange_spawn_failure(monkeypatch, exc):
    _pin_tmux(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["orchestrator", "assign-to-manager", "--sid", "abc12345", "--model", "@opus[1m]", "--effort", "xhigh"])
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
    monkeypatch.setattr(sys, "argv", ["orchestrator", "assign-to-manager", "--sid", "abc12345", "--model", "@opus[1m]", "--effort", "xhigh"])
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
    monkeypatch.setattr(sys, "argv", ["orchestrator", "assign-to-manager", "--sid", "abc12345", "--model", "@opus[1m]", "--effort", "xhigh"])
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
                                      "--sid", "abc12345", "--task-key", "demo-task", "--model", "@opus[1m]", "--effort", "xhigh"])
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
    assert record["ticket"] == "demo-task"


def test_assign_to_manager_rejects_path_hostile_task_key(monkeypatch, tmp_path, capsys):
    from dockwright import paths, state

    monkeypatch.setattr(paths, "ASSIGNMENTS", tmp_path / "assignments")
    monkeypatch.setattr(sys, "argv", ["orchestrator", "assign-to-manager",
                                      "--sid", "abc12345", "--task-key", "yt bot", "--model", "@opus[1m]", "--effort", "xhigh"])
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


def test_assign_to_manager_stamps_parent_sid_on_assignment_and_env(monkeypatch, tmp_path):
    from dockwright import paths, state

    _pin_tmux(monkeypatch)
    monkeypatch.setattr(paths, "ASSIGNMENTS", tmp_path / "assignments")
    monkeypatch.setattr(paths, "ASSIGNMENTS_PENDING", tmp_path / "assignments" / ".pending")
    monkeypatch.setattr(sys, "argv", ["orchestrator", "assign-to-manager", "--sid", "abc12345", "--model", "@opus[1m]", "--effort", "xhigh"])
    monkeypatch.setattr(promote, "_read_active_records", lambda: [
        {"agent": "manager", "name": "m1", "claude_sid": "m1-sid", "pid": os.getpid(),
         "domain": "general", "started_at": 100},
    ])
    captured = {}

    async def _ok(**kwargs):
        captured.update(kwargs)
        return ("win-1", kwargs.get("name", ""))

    monkeypatch.setattr(spawner, "spawn_worker_tab", _ok)
    promote.assign_to_manager_cli()
    record = state.read_json(paths.ASSIGNMENTS / "abc12345.json")
    assert record["parent_manager_name"] == "m1"
    assert record["parent_name"] == "m1"
    assert record["parent_sid"] == "m1-sid"
    assert captured["env"] == {"CLAUDE_PARENT_MANAGER": "m1", "CLAUDE_PARENT_SID": "m1-sid"}


def _arrange_adoption(monkeypatch, tmp_path):
    from dockwright import paths

    _pin_tmux(monkeypatch)
    monkeypatch.setattr(paths, "ASSIGNMENTS", tmp_path / "assignments")
    monkeypatch.setattr(paths, "ASSIGNMENTS_PENDING", tmp_path / "assignments" / ".pending")
    monkeypatch.setattr(paths, "ROLE_SETTINGS", tmp_path / "role-settings")
    monkeypatch.setattr(sys, "argv", ["orchestrator", "assign-to-manager", "--sid", "abc12345", "--model", "@opus[1m]", "--effort", "xhigh"])
    monkeypatch.setattr(promote, "_read_active_records", lambda: [
        {"agent": "manager", "name": "m1", "pid": os.getpid(), "domain": "general", "started_at": 100},
    ])
    role_dir = tmp_path / "roles" / "GenericWorker"
    role_dir.mkdir(parents=True)
    (role_dir / "role.toml").write_text('core = "GenericWorker"\nlaunch = ["worker"]\n')
    (role_dir / "role.worker.md").write_text("<!-- dockwright-role: GenericWorker launch=worker -->\nBODY\n")
    (role_dir / "role.settings.json").write_text('{"skillOverrides": {"skill-x": "name-only"}}')
    monkeypatch.setattr(paths, "ROLES_DIR", tmp_path / "roles")
    captured = {}

    async def _ok(**kwargs):
        captured.update(kwargs)
        return ("win-1", kwargs.get("name", ""))

    monkeypatch.setattr(spawner, "spawn_worker_tab", _ok)
    return role_dir, captured


def test_assign_to_manager_relaunches_in_the_generic_worker_role(monkeypatch, tmp_path):
    import json

    from dockwright import paths, state

    role_dir, captured = _arrange_adoption(monkeypatch, tmp_path)
    promote.assign_to_manager_cli()
    args = captured["extra_args"]
    assert args[:2] == ["--append-system-prompt-file", str(role_dir / "role.worker.md")]
    settings = json.loads(open(args[args.index("--settings") + 1]).read())
    assert settings["skillOverrides"] == {"skill-x": "name-only"} and settings["disableRemoteControl"] is True
    assert state.read_json(paths.ASSIGNMENTS / "abc12345.json")["context_role"] == "GenericWorker"


def test_assign_to_manager_carries_the_agents_file_of_its_cwd(monkeypatch, tmp_path):
    from dockwright import config

    role_dir, captured = _arrange_adoption(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "profiles", lambda: {"sets": {"none": []}, "repos": {}, "work_roots": [], "work_default": "none"})
    (role_dir.parent / "_agents").mkdir()
    (role_dir.parent / "_agents" / "none.json").write_text("{}")
    promote.assign_to_manager_cli()
    assert captured["extra_args"][-2:] == ["--agents-file", str(role_dir.parent / "_agents" / "none.json")]


def test_assign_to_manager_refuses_when_the_generic_worker_render_is_missing(monkeypatch, tmp_path, capsys):
    role_dir, captured = _arrange_adoption(monkeypatch, tmp_path)
    (role_dir / "role.worker.md").unlink()
    with pytest.raises(SystemExit) as exc:
        promote.assign_to_manager_cli()
    assert exc.value.code == 1 and captured == {}
    assert "run `dockwright compose`" in capsys.readouterr().err


def test_assign_to_manager_without_model_or_effort_exits_before_spawning(monkeypatch, tmp_path, capsys):
    _arrange_adoption(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["orchestrator", "assign-to-manager", "--sid", "abc12345", "--model", "@opus"])
    with pytest.raises(SystemExit):
        promote.assign_to_manager_cli()
    assert "--effort" in capsys.readouterr().err


def test_assign_to_manager_passes_model_and_effort_to_the_spawn(monkeypatch, tmp_path):
    _, captured = _arrange_adoption(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["orchestrator", "assign-to-manager", "--sid", "abc12345", "--model", "@opus[1m]",
                                      "--effort", "xhigh"])
    promote.assign_to_manager_cli()
    args = captured["extra_args"]
    assert args[args.index("--model") + 1] == "@opus[1m]" and args[args.index("--effort") + 1] == "xhigh"


def test_assign_to_manager_records_the_spawn_args_so_a_resume_replays_them(monkeypatch, tmp_path):
    _, captured = _arrange_adoption(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["orchestrator", "assign-to-manager", "--sid", "abc12345", "--model", "@opus[1m]",
                                      "--effort", "xhigh"])
    promote.assign_to_manager_cli()
    from dockwright import paths, state
    record = state.read_json(paths.ASSIGNMENTS / "abc12345.json")
    assert record["spawn_extra_args"] == list(captured["extra_args"])
    assert "--model" in record["spawn_extra_args"] and "--effort" in record["spawn_extra_args"]
