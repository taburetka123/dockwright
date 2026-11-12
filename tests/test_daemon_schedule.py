import asyncio
import time
from datetime import datetime

import pytest

from dockwright import config, daemon_schedule, paths, state
from tests.test_mcp_tools import fresh_orchestrator_dir  # noqa: F401


@pytest.fixture
def sched(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DAEMON", tmp_path / "daemon")
    monkeypatch.setattr(paths, "DAEMON_STOP", tmp_path / "daemon-stop")
    monkeypatch.setattr(paths, "ARTIFACTS", tmp_path / "artifacts")
    paths.DAEMON.mkdir()
    cfg = tmp_path / "dockwright.toml"
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))

    def write(body):
        cfg.write_text(body)

    write('[schedules.nightly]\nschema = "general-work"\ncron = "0 9 * * *"\nask = "sweep"\ndomain = "ops"\n'
          'cwd = "/w"\n')
    return write


class Engine:
    def __init__(self, fail=None, running=()):
        self.starts = []
        self.fail = fail
        self.running = set(running)

    async def pipeline_start_impl(self, manager_sid, task_key, schema, cwd, ask, domain=None):
        self.starts.append((manager_sid, task_key, schema, cwd, ask, domain))
        if self.fail:
            raise ValueError(self.fail)
        return {"state": "committed"}

    def _load_snapshot_for(self, key):
        return None

    def _read_events(self, path):
        return []

    def _list_artifacts(self, key):
        return []

    def _process_index(self):
        return {}

    def _waiting_on_status(self, key, snap, events, stamps, now, index=None):
        return {"waiting_on": "external:merge" if key in self.running else "none"}


def _at(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp()


def test_the_first_pass_records_the_clock_and_starts_nothing(sched, monkeypatch):
    engine = Engine()
    monkeypatch.setattr(daemon_schedule, "_engine", lambda: engine)
    assert daemon_schedule.run(_at("2026-10-13 09:00")) == []
    assert engine.starts == []
    assert state.read_json(paths.DAEMON / "schedules.json")["nightly"]["last_slot"] == _at("2026-10-13 09:00")


def test_a_slot_starts_once_with_a_slot_shaped_key_and_the_configured_domain(sched, monkeypatch):
    engine = Engine()
    monkeypatch.setattr(daemon_schedule, "_engine", lambda: engine)
    daemon_schedule.run(_at("2026-10-13 08:00"))
    records = daemon_schedule.run(_at("2026-10-13 09:00"))
    assert engine.starts == [("daemon", "nightly-20261013T0900", "general-work", "/w", "sweep", "ops")]
    assert records == [{"type": "schedule_started", "name": "nightly", "slot": _at("2026-10-13 09:00"),
                        "task_key": "nightly-20261013T0900"}]
    assert daemon_schedule.run(_at("2026-10-13 09:00")) == [] and len(engine.starts) == 1
    assert daemon_schedule.run(_at("2026-10-13 09:30")) == [] and len(engine.starts) == 1


def test_a_running_previous_work_skips_the_slot(sched, monkeypatch):
    engine = Engine(running={"nightly-20261013T0900"})
    monkeypatch.setattr(daemon_schedule, "_engine", lambda: engine)
    daemon_schedule.run(_at("2026-10-13 08:00"))
    daemon_schedule.run(_at("2026-10-13 09:00"))
    records = daemon_schedule.run(_at("2026-10-14 09:00"))
    assert records == [{"type": "schedule_skipped", "name": "nightly", "slot": _at("2026-10-14 09:00"),
                        "reason": "running", "task_key": "nightly-20261013T0900"}]
    assert len(engine.starts) == 1
    assert state.read_json(paths.DAEMON / "schedules.json")["nightly"]["last_slot"] == _at("2026-10-14 09:00")


def test_a_failed_start_burns_the_slot(sched, monkeypatch):
    engine = Engine(fail="no manager")
    monkeypatch.setattr(daemon_schedule, "_engine", lambda: engine)
    daemon_schedule.run(_at("2026-10-13 08:00"))
    records = daemon_schedule.run(_at("2026-10-13 09:00"))
    assert records[0]["type"] == "schedule_failed" and records[0]["reason"] == "no manager"
    assert daemon_schedule.run(_at("2026-10-13 09:01")) == [] and len(engine.starts) == 1


def test_after_a_sleep_only_the_last_missed_slot_starts(sched, monkeypatch):
    sched('[schedules.q]\nschema = "s"\ncron = "*/15 * * * *"\nask = "a"\n')
    engine = Engine()
    monkeypatch.setattr(daemon_schedule, "_engine", lambda: engine)
    daemon_schedule.run(_at("2026-10-13 08:00"))
    daemon_schedule.run(_at("2026-10-13 18:07"))
    assert [s[1] for s in engine.starts] == ["q-20261013T1800"]


def test_disabled_schedules_and_the_stop_file_start_nothing(sched, monkeypatch):
    sched('[schedules.q]\nschema = "s"\ncron = "* * * * *"\nask = "a"\nenabled = false\n')
    engine = Engine()
    monkeypatch.setattr(daemon_schedule, "_engine", lambda: engine)
    daemon_schedule.run(_at("2026-10-13 08:00"))
    assert daemon_schedule.run(_at("2026-10-13 08:01")) == [] and engine.starts == []
    sched('[schedules.q]\nschema = "s"\ncron = "* * * * *"\nask = "a"\n')
    paths.DAEMON_STOP.write_text("")
    assert daemon_schedule.run(_at("2026-10-13 08:02")) == [] and engine.starts == []


def test_re_enabling_a_schedule_does_not_start_the_slot_that_passed_while_it_was_disabled(sched, monkeypatch):
    sched('[schedules.q]\nschema = "s"\ncron = "0 9 * * *"\nask = "a"\nenabled = false\n')
    engine = Engine()
    monkeypatch.setattr(daemon_schedule, "_engine", lambda: engine)
    daemon_schedule.run(_at("2026-10-13 08:30"))
    daemon_schedule.run(_at("2026-10-13 09:01"))
    sched('[schedules.q]\nschema = "s"\ncron = "0 9 * * *"\nask = "a"\n')
    assert daemon_schedule.run(_at("2026-10-13 15:00")) == [] and engine.starts == []
    assert [s[1] for s in engine.starts] == []
    daemon_schedule.run(_at("2026-10-14 09:00"))
    assert [s[1] for s in engine.starts] == ["q-20261014T0900"]


def test_a_bad_cron_or_name_is_reported_once_an_hour(sched, monkeypatch):
    sched('[schedules.q]\nschema = "s"\ncron = "nope"\nask = "a"\n[schedules."bad name"]\nschema = "s"\n'
          'cron = "* * * * *"\nask = "a"\n')
    engine = Engine()
    monkeypatch.setattr(daemon_schedule, "_engine", lambda: engine)
    records = daemon_schedule.run(_at("2026-10-13 08:00"))
    assert sorted(r["name"] for r in records) == ["bad name", "q"]
    assert all(r["type"] == "schedule_invalid" for r in records)
    assert daemon_schedule.run(_at("2026-10-13 08:30")) == []
    assert len(daemon_schedule.run(_at("2026-10-13 09:01"))) == 2


def test_describe_lists_each_schedule_with_its_next_slot_and_last_work(sched, monkeypatch):
    engine = Engine()
    monkeypatch.setattr(daemon_schedule, "_engine", lambda: engine)
    daemon_schedule.run(_at("2026-10-13 08:00"))
    lines = daemon_schedule.describe(_at("2026-10-13 08:30"))
    assert lines == ["nightly  0 9 * * *  next 2026-10-13 09:00  last -  state -"]


@pytest.mark.parametrize("value,running", [("none", False), ("external:merge", True), ("human:gate", True),
                                           ("stalled:pr", True), ("worker:design", True)])
def test_a_work_counts_as_running_unless_its_waiting_on_is_none(monkeypatch, value, running):
    from dockwright import mcp_server
    monkeypatch.setattr(mcp_server, "_list_artifacts", lambda key: [])
    monkeypatch.setattr(mcp_server, "_load_snapshot_for", lambda key: None)
    monkeypatch.setattr(mcp_server, "_read_events", lambda path: [])
    monkeypatch.setattr(mcp_server, "_process_index", lambda: {})
    monkeypatch.setattr(mcp_server, "_waiting_on_status", lambda *a, **k: {"waiting_on": value})
    assert daemon_schedule._work_state("k", 1.0) == ("running" if running else "closed", None)


def test_an_unreadable_work_is_reported_as_such(monkeypatch):
    from dockwright import mcp_server
    monkeypatch.setattr(mcp_server, "_list_artifacts", lambda key: (_ for _ in ()).throw(ValueError("x")))
    assert daemon_schedule._work_state("k", 1.0) == ("unreadable", "ValueError: x")


def test_an_unreadable_last_work_skips_the_slot_with_its_own_reason(sched, monkeypatch):
    engine = Engine()
    monkeypatch.setattr(daemon_schedule, "_engine", lambda: engine)
    daemon_schedule.run(_at("2026-10-13 08:00"))
    daemon_schedule.run(_at("2026-10-13 09:00"))
    engine._list_artifacts = lambda key: (_ for _ in ()).throw(ValueError("bad line"))
    records = daemon_schedule.run(_at("2026-10-14 09:00"))
    assert records == [{"type": "schedule_skipped", "name": "nightly", "slot": _at("2026-10-14 09:00"),
                        "reason": "unreadable", "task_key": "nightly-20261013T0900", "detail": "ValueError: bad line"}]
    assert len(engine.starts) == 1
    assert daemon_schedule.describe(_at("2026-10-14 09:30"))[0].endswith("state unreadable")


def test_a_closed_work_with_an_open_engineer_ask_still_counts_as_closed(fresh_orchestrator_dir, monkeypatch, tmp_path):
    from dockwright import mcp_server
    from tests.test_mcp_tools import _seed_snapshot
    from tests.test_topic_questions import _manager
    _seed_snapshot("K-9", tmp_path)
    state.append_event(paths.artifact_events_path("K-9"), {"type": "work_closed", "token_id": "t1", "node": "design",
                                                           "reason": "done", "actor_sid": "m1"})
    monkeypatch.setattr(mcp_server, "_process_index", lambda: {})
    assert daemon_schedule._work_state("K-9", time.time()) == ("closed", None)
    _manager("m1", "mgr")
    mcp_server.ask_engineer_impl("m1", "archive it?", task_key="K-9")
    assert daemon_schedule._work_state("K-9", time.time()) == ("closed", None)
