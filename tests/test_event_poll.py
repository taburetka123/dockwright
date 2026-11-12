import copy
import fcntl
import json
import os
import time

import pytest

from dockwright import config, event_poll, event_rules, event_sources, events, failure_streak, paths
from dockwright.events import Event
from tests.test_mcp_tools import fresh_orchestrator_dir  # noqa: F401

T0 = 1792700000.0
K = "K"


@pytest.fixture
def events_cfg(tmp_path, monkeypatch):
    cfg = tmp_path / "events.toml"
    env_file = tmp_path / "events.env"
    cfg.write_text(f'[events]\nenabled = true\nenv_file = "{env_file}"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    env_file.write_text("TRACKER_URL=https://j\nTRACKER_EMAIL=me@x\nTRACKER_API_TOKEN=tok\nOTHER=1\n")
    env_file.chmod(0o600)
    return cfg


@pytest.fixture
def awake(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(failure_streak, "awake_seconds", lambda: clock["t"])
    return clock


def _streaks(source):
    path = paths.EVENTS_CURSORS / source / "failures.json"
    return json.loads(path.read_text()) if path.exists() else {}


class FakeSource:
    def __init__(self, result=None, raises=None, sleep=0.0):
        self.calls = []
        self.result = result if result is not None else ([], {})
        self.raises = raises
        self.sleep = sleep

    def poll(self, subs, rules, cursor, env, now, call=None, until=None):
        self.calls.append({"subs": list(subs), "rules": rules, "cursor": dict(cursor), "env": dict(env), "now": now,
                           "until": until, "at": time.time()})
        if self.sleep:
            time.sleep(self.sleep)
        if self.raises is not None:
            raise self.raises
        if callable(self.result):
            return self.result(subs, cursor)
        return self.result


def _install(monkeypatch, **sources):
    fakes = {name: FakeSource() for name in ("tracker", "deploy", "time", "github", "chat", "exec")}
    fakes.update(sources)
    monkeypatch.setattr(event_poll, "_sources", {k: v for k, v in fakes.items()})
    return fakes


def _sub(sub_id="s1", source="tracker", deadline=None, match=None):
    sub = events.Sub(sub_id, K, "design", source, match or {"issue": "ABC-1"}, None, "1", deadline, None, None, T0,
                     None)
    paths.EVENTS_SUBS.mkdir(parents=True, exist_ok=True)
    (paths.EVENTS_SUBS / f"{sub_id}.json").write_text(json.dumps(sub.to_dict()))
    return sub


def _event(id="tracker:ABC-1:comment:2", source="tracker", kind="comment", ts=T0):
    return Event(id, ts, source, kind, {"issue": "ABC-1"}, "2", "ABC-1 #2 by A: hi", {})


def _cursor(source):
    return json.loads((paths.EVENTS_CURSORS / source / "cursor.json").read_text())


def test_a_disabled_layer_polls_nothing(fresh_orchestrator_dir, tmp_path, monkeypatch):
    cfg = tmp_path / "events.toml"
    cfg.write_text("[events]\nenabled = false\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    fakes = _install(monkeypatch)
    _sub()
    assert event_poll.run(T0) == []
    assert all(f.calls == [] for f in fakes.values())


def test_step_prunes_once_an_hour_from_either_caller(fresh_orchestrator_dir, events_cfg, monkeypatch):
    _install(monkeypatch)
    calls = []

    def prune(now):
        calls.append(now)
        return 2 if len(calls) == 1 else 0

    monkeypatch.setattr(events, "prune", prune)
    emit = lambda line, manager_name=None: None  # noqa: E731
    records = event_poll.step(T0, [], emit)
    assert calls == [T0] and {"type": "events_pruned", "files": 2} in records
    records = event_poll.step(T0 + 10, [], emit)
    assert calls == [T0] and not any(r.get("type") == "events_pruned" for r in records)
    records = event_poll.step(T0 + 3601, [], emit)
    assert calls == [T0, T0 + 3601] and not any(r.get("type") == "events_pruned" for r in records)


def test_each_source_runs_on_its_own_cadence(fresh_orchestrator_dir, events_cfg, monkeypatch):
    fakes = _install(monkeypatch)
    _sub("s1", "tracker")
    _sub("s2", "deploy", match={"repo": "o/r", "env": "staging", "sha": "a" * 40})
    for now in (T0, T0 + 70, T0 + 310):
        event_poll.run(now)
    assert [c["now"] for c in fakes["tracker"].calls] == [T0, T0 + 310]
    assert [c["now"] for c in fakes["deploy"].calls] == [T0, T0 + 70, T0 + 310]
    assert fakes["time"].calls == []
    assert json.loads((paths.EVENTS_CURSORS / "tracker" / ".last").read_text())["ts"] == T0 + 310
    assert json.loads((paths.EVENTS_CURSORS / "deploy" / ".last").read_text())["ts"] == T0 + 310


def test_only_sources_with_a_live_subscription_or_an_enabled_rule_are_called(fresh_orchestrator_dir, events_cfg,
                                                                            tmp_path, monkeypatch):
    fakes = _install(monkeypatch)
    assert event_poll.run(T0) == []
    assert all(f.calls == [] for f in fakes.values())
    _sub("s2", "deploy", match={"repo": "o/r", "env": "staging", "sha": "a" * 40})
    event_poll.run(T0 + 60)
    assert len(fakes["deploy"].calls) == 1 and fakes["tracker"].calls == []
    assert [s.sub_id for s in fakes["deploy"].calls[0]["subs"]] == ["s2"]
    cfg = tmp_path / "events.toml"
    cfg.write_text(cfg.read_text() + '[events.rules.sprint]\nsource = "tracker"\nkind = "sprint-assigned"\n'
                                     'schema = "ticket"\n')
    event_poll.run(T0 + 400)
    assert len(fakes["tracker"].calls) == 1 and fakes["tracker"].calls[0]["subs"] == []
    assert fakes["tracker"].calls[0]["rules"]["sprint"]["kind"] == "sprint-assigned"
    cfg.write_text(cfg.read_text() + "enabled = false\n")
    event_poll.run(T0 + 800)
    assert len(fakes["tracker"].calls) == 1


def test_a_tracker_subs_deadline_is_polled_by_the_time_source(fresh_orchestrator_dir, events_cfg, monkeypatch):
    from dockwright.event_sources import time as time_source
    fakes = _install(monkeypatch, time=time_source)
    sub = _sub("s1", "tracker", deadline=T0 - 5)
    records = event_poll.run(T0)
    assert {"type": "events_polled", "source": "time", "n": 1} in records
    assert [e.id for e in events.recent("time", T0)] == [f"time:{sub.sub_id}:deadline"]
    assert len(fakes["tracker"].calls) == 1


def test_events_are_appended_and_the_cursor_moves_only_after_a_clean_poll(fresh_orchestrator_dir, events_cfg,
                                                                           monkeypatch):
    two = [_event("tracker:ABC-1:comment:2"), _event("tracker:ABC-1:comment:3")]
    fakes = _install(monkeypatch, tracker=FakeSource((two, {"comments": {"ABC-1": "3"}})))
    _sub()
    records = event_poll.run(T0)
    assert records == [{"type": "events_polled", "source": "tracker", "n": 2}]
    assert [e.id for e in events.recent("tracker", T0)] == ["tracker:ABC-1:comment:2", "tracker:ABC-1:comment:3"]
    assert _cursor("tracker") == {"comments": {"ABC-1": "3"}}
    _install(monkeypatch, tracker=FakeSource(raises=events.PollError("boom")))
    records = event_poll.run(T0 + 400)
    assert records == [{"type": "events_poll_failed", "source": "tracker", "error": "boom", "streak": 1, "held": True}]
    assert _cursor("tracker") == {"comments": {"ABC-1": "3"}}


def test_sources_poll_in_parallel_each_with_its_own_budget(fresh_orchestrator_dir, events_cfg, monkeypatch):
    fakes = _install(monkeypatch, tracker=FakeSource(sleep=0.3), deploy=FakeSource(sleep=0.3))
    _sub("s1", "tracker")
    _sub("s2", "deploy", match={"repo": "o/r", "env": "staging", "sha": "a" * 40})
    started = time.time()
    event_poll.run(T0)
    assert time.time() - started < 0.5
    calls = fakes["tracker"].calls + fakes["deploy"].calls
    assert len(calls) == 2
    for c in calls:
        assert abs(c["until"] - (c["at"] + event_poll.SOURCE_TIMEOUT_SEC)) < 0.1
        assert c["until"] > c["now"] + event_poll.SOURCE_TIMEOUT_SEC + 60


def test_a_partial_poll_keeps_its_events_and_ledgers_partial(fresh_orchestrator_dir, events_cfg, monkeypatch):
    partial = ([_event()], {"comments": {"ABC-1": "2"}, "budget_spent": ["ABC-2"]})
    _install(monkeypatch, tracker=FakeSource(partial))
    _sub()
    records = event_poll.run(T0)
    assert records == [{"type": "events_polled", "source": "tracker", "n": 1, "partial": ["ABC-2"]}]
    assert [e.id for e in events.recent("tracker", T0)] == ["tracker:ABC-1:comment:2"]
    assert _cursor("tracker") == {"comments": {"ABC-1": "2"}, "budget_spent": ["ABC-2"]}


def test_a_poll_with_failed_selectors_keeps_its_events_and_cursor_and_pages_the_failures(fresh_orchestrator_dir,
                                                                                        events_cfg, monkeypatch):
    part = ([_event()], {"comments": {"ABC-1": "2"}, "failed": [["ABC-2", "tracker ABC-2: HTTP 404"]]})
    _install(monkeypatch, tracker=FakeSource(part))
    _sub()
    records = event_poll.run(T0)
    assert records == [{"type": "events_polled", "source": "tracker", "n": 1},
                       {"type": "events_poll_failed", "source": "tracker", "error": "ABC-2: tracker ABC-2: HTTP 404",
                        "streak": 1, "held": True}]
    assert [e.id for e in events.recent("tracker", T0)] == ["tracker:ABC-1:comment:2"]
    assert _cursor("tracker") == {"comments": {"ABC-1": "2"}, "failed": [["ABC-2", "tracker ABC-2: HTTP 404"]]}


class SkippingSource(FakeSource):
    FAILED_RETRIED = False


def test_a_source_that_skips_its_failed_items_pages_them_as_skipped_and_not_retried(fresh_orchestrator_dir,
                                                                                   events_cfg, monkeypatch):
    _install(monkeypatch, chat=SkippingSource(([], {"seen": [], "failed": [["notification k1", "no subtitle"]]})))
    _sub(source="chat", match={"sender": "Ann Lee"})
    monkeypatch.setattr(event_rules, "run", lambda now: [])
    pages = []
    records = event_poll.step(T0, [], lambda line, manager_name=None: pages.append(line))
    assert records[-1] == {"type": "events_poll_failed", "source": "chat", "error": "notification k1: no subtitle",
                           "skipped": True}
    assert len(pages) == 1 and "not retried" in pages[0] and "credential" not in pages[0]

class FirstFailsThenSkips(SkippingSource):
    def poll(self, subs, rules, cursor, env, now, call=None, until=None):
        self.calls.append(now)
        if len(self.calls) == 1:
            raise events.PollError("no store")
        return [], {"seen": [], "failed": [["notification k1", "no subtitle"]]}


def test_a_skipped_item_pages_while_a_whole_source_failure_is_held(fresh_orchestrator_dir, events_cfg, monkeypatch,
                                                                    awake):
    _install(monkeypatch, chat=FirstFailsThenSkips())
    _sub(source="chat", match={"sender": "Ann Lee"})
    monkeypatch.setattr(event_rules, "run", lambda now: [])
    pages = []
    records = event_poll.step(T0, [], lambda line, manager_name=None: pages.append(line))
    assert records[0]["error"] == "no store" and records[0]["held"] is True
    assert pages == []
    event_poll.step(T0 + 60, [], lambda line, manager_name=None: pages.append(line))
    assert len(pages) == 1 and "not retried" in pages[0]


def test_an_insecure_env_file_skips_the_tracker_source_and_pages_once(fresh_orchestrator_dir, events_cfg, tmp_path,
                                                                   monkeypatch):
    env_file = tmp_path / "events.env"
    env_file.chmod(0o644)
    fakes = _install(monkeypatch)
    _sub("s1", "tracker")
    _sub("s2", "deploy", match={"repo": "o/r", "env": "staging", "sha": "a" * 40})
    records = event_poll.run(T0)
    assert records == [{"type": "events_env_insecure", "path": str(env_file), "reason": "mode"},
                       {"type": "events_polled", "source": "deploy", "n": 0}]
    assert fakes["tracker"].calls == [] and len(fakes["deploy"].calls) == 1
    env_file.unlink()
    records = event_poll.run(T0 + 400)
    assert records[0] == {"type": "events_env_insecure", "path": str(env_file), "reason": "missing"}
    env_file.write_text("TRACKER_URL=https://j\nTRACKER_EMAIL=me@x\nTRACKER_API_TOKEN=tok\nOTHER=1\n# c\n\nBAD LINE\n")
    env_file.chmod(0o600)
    event_poll.run(T0 + 800)
    assert fakes["tracker"].calls[0]["env"] == {"TRACKER_URL": "https://j", "TRACKER_EMAIL": "me@x", "TRACKER_API_TOKEN": "tok"}
    assert fakes["deploy"].calls[-1]["env"] == {}
    assert event_poll.load_env(env_file) == ({"TRACKER_URL": "https://j", "TRACKER_EMAIL": "me@x", "TRACKER_API_TOKEN": "tok"},
                                             None)
    assert event_poll.load_env(tmp_path / "nope") == (None, "missing")


def test_a_busy_source_lock_skips_that_source_only(fresh_orchestrator_dir, events_cfg, monkeypatch):
    fakes = _install(monkeypatch)
    _sub("s1", "tracker")
    _sub("s2", "deploy", match={"repo": "o/r", "env": "staging", "sha": "a" * 40})
    lock = paths.EVENTS_CURSORS / "tracker" / ".poll.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert event_poll.run(T0) == [{"type": "events_poll_busy", "source": "tracker"},
                                      {"type": "events_polled", "source": "deploy", "n": 0}]
    assert fakes["tracker"].calls == []
    event_poll.run(T0 + 400)
    assert len(fakes["tracker"].calls) == 1


def test_the_ledger_records_one_line_per_polled_source(fresh_orchestrator_dir, events_cfg, monkeypatch):
    two = [_event("deploy:o/r:a:staging:1", "deploy", "job"), _event("deploy:o/r:a:staging:2", "deploy", "job")]
    _install(monkeypatch, deploy=FakeSource((two, {})))
    _sub("s2", "deploy", match={"repo": "o/r", "env": "staging", "sha": "a" * 40})
    assert event_poll.run(T0) == [{"type": "events_polled", "source": "deploy", "n": 2}]


def test_step_pages_a_failure_after_three_polls_and_ten_awake_minutes_and_a_start_to_its_manager(
        fresh_orchestrator_dir, events_cfg, monkeypatch, awake):
    _install(monkeypatch, tracker=FakeSource(raises=events.PollError("boom")))
    _sub()
    pages = []
    emit = lambda line, manager_name=None: pages.append((line, manager_name))
    monkeypatch.setattr(event_rules, "run", lambda now: [])
    managers = [{"sid": "m1", "name": "proud"}]
    records = event_poll.step(T0, managers, emit)
    assert records == [{"type": "events_poll_failed", "source": "tracker", "error": "boom", "streak": 1, "held": True}]
    assert pages == []
    awake["t"] = 300.0
    records = event_poll.step(T0 + 300, managers, emit)
    assert records[0]["streak"] == 2 and records[0]["held"] is True and pages == []
    awake["t"] = 600.0
    records = event_poll.step(T0 + 600, managers, emit)
    assert records == [{"type": "events_poll_failed", "source": "tracker", "error": "boom", "streak": 3}]
    assert len(pages) == 1 and pages[0][1] is None and pages[0][0].startswith("EVENT_POLL_FAILED tracker → ")
    assert pages[0][0].endswith(" — boom")
    awake["t"] = 900.0
    event_poll.step(T0 + 900, managers, emit)
    assert len(pages) == 1
    awake["t"] = 4300.0
    event_poll.step(T0 + 4300, managers, emit)
    assert len(pages) == 2
    awake["t"] = 7400.0
    paged = {"type": "event_page", "rule": "review-ping",
             "line": "o/r#1 waits for review 9 business hours (ping 1) https://gh/o/r/pull/1",
             "event_id": "github:o/r:1:review-wait:1:1", "kind": "review-wait", "manager_name": "proud"}
    started = [{"type": "event_started", "rule": "sprint", "task_key": "ABC-9", "event_id": "e", "manager_sid": "m1",
                "manager_name": "proud"},
               {"type": "event_stalled", "rule": "sprint", "domain": "general", "since": "2026-10-23T10:00:00Z"}, paged]
    monkeypatch.setattr(event_rules, "run", lambda now: started)
    records = event_poll.step(T0 + 7400, managers, emit)
    assert records[-3:] == started
    assert pages[-3][0].startswith("EVENT_STARTED ABC-9 sprint → ") and pages[-3][1] == "proud"
    assert pages[-2][0].startswith("EVENT_STALLED sprint general → ") and pages[-2][1] is None
    assert pages[-1][0].startswith("EVENT_PAGE review-ping → o/r#1 waits for review 9 business hours (ping 1) "
                                   "https://gh/o/r/pull/1; act on it as your rule for [events.rules.review-ping] says")
    assert pages[-1][0].endswith(" — github:o/r:1:review-wait:1:1") and pages[-1][1] == "proud"
    awake["t"] = 7500.0
    event_poll.step(T0 + 7500, managers, emit)
    assert pages[-1][0].startswith("EVENT_PAGE review-ping → ") and pages[-4][0].startswith("EVENT_PAGE review-ping → ")
    monkeypatch.setattr(event_rules, "run", lambda now: [])
    env_file = fresh_orchestrator_dir / "events.env"
    env_file.chmod(0o644)
    records = event_poll.step(T0 + 11000, managers, emit)
    assert records[0]["type"] == "events_env_insecure"
    assert pages[-1][0].startswith(f"EVENT_ENV_INSECURE {env_file} mode → ")


def test_step_pages_a_queue_reminder_to_its_manager(fresh_orchestrator_dir, events_cfg, monkeypatch):
    _install(monkeypatch)
    page = {"type": "event_queue_page", "rule": "prod-x", "count": 2, "line": "INCIDENT Q1 t",
            "event_id": "exec:oncall:Q1:1.0:x", "manager_name": "proud"}
    monkeypatch.setattr(event_rules, "run", lambda now: [page])
    pages = []
    emit = lambda line, manager_name=None: pages.append((line, manager_name))
    managers = [{"sid": "m1", "name": "proud"}]
    event_poll.step(T0, managers, emit)
    assert pages[-1][0] == ('EVENT_QUEUE prod-x → 2 pending, oldest INCIDENT Q1 t; event_queue("prod-x") lists them, '
                            'event_ack closes each — exec:oncall:Q1:1.0:x')
    assert pages[-1][1] == "proud"
    event_poll.step(T0 + 10, managers, emit)
    assert len(pages) == 2


def _exec_rule(events_cfg):
    events_cfg.write_text(events_cfg.read_text() + '[events.rules.r]\nsource = "exec"\nkind = "incident"\naction = "page"\n')


def test_a_failed_exec_command_keeps_its_events_and_pages_after_three_failed_runs(fresh_orchestrator_dir, events_cfg,
                                                                                   monkeypatch, awake):
    result = lambda subs, cursor: ([_event("exec:oncall:Q1:1.0:x", "exec", "incident")],  # noqa: E731
                                   {event_sources.FAILED: [["cmd --json", "exit 1: boom"]],
                                    event_sources.ATTEMPTED: ["cmd --json"]})
    _install(monkeypatch, exec=FakeSource(result=result))
    _exec_rule(events_cfg)
    monkeypatch.setattr(event_rules, "run", lambda now: [])
    pages = []
    emit = lambda line, manager_name=None: pages.append((line, manager_name))
    managers = [{"sid": "m1", "name": "proud"}]
    records = event_poll.step(T0, managers, emit)
    assert [e.id for e in events.read_since("exec", 0)] == ["exec:oncall:Q1:1.0:x"]
    assert {"type": "events_poll_failed", "source": "exec", "error": "cmd --json: exit 1: boom", "streak": 1,
            "held": True} in records
    assert pages == []
    awake["t"] = 300.0
    event_poll.step(T0 + 300, managers, emit)
    assert pages == []
    awake["t"] = 600.0
    event_poll.step(T0 + 600, managers, emit)
    assert len(pages) == 1 and pages[0][0].startswith("EVENT_POLL_FAILED exec → ")
    assert "attempted" not in _cursor("exec")
    awake["t"] = 800.0
    event_poll.step(T0 + 800, managers, emit)
    assert len(pages) == 1
    awake["t"] = 4300.0
    event_poll.step(T0 + 4300, managers, emit)
    assert len(pages) == 2


def _sequence(*results):
    left = list(results)
    return lambda subs, cursor: copy.deepcopy(left.pop(0))


def _failed(*selectors, attempted=None, budget_spent=None):
    cursor = {event_sources.FAILED: [[s, "exit 1: boom"] for s in selectors]}
    if attempted is not None:
        cursor[event_sources.ATTEMPTED] = list(attempted)
    if budget_spent is not None:
        cursor[event_sources.BUDGET_SPENT] = list(budget_spent)
    return [], cursor


def _steps(times, awake, pages):
    emit = lambda line, manager_name=None: pages.append(line)  # noqa: E731
    out = []
    for t in times:
        awake["t"] = float(t)
        out.append(event_poll.step(T0 + t, [], emit))
    return out


def test_polls_that_run_no_exec_command_keep_its_streak(fresh_orchestrator_dir, events_cfg, monkeypatch, awake):
    ran, idle = _failed("cmd --json", attempted=["cmd --json"]), _failed(attempted=[])
    _install(monkeypatch, exec=FakeSource(result=_sequence(ran, idle, ran, idle, ran)))
    _exec_rule(events_cfg)
    monkeypatch.setattr(event_rules, "run", lambda now: [])
    pages = []
    _steps([0, 150, 300, 450], awake, pages)
    assert pages == []
    assert _streaks("exec")["cmd --json"]["n"] == 2
    _steps([600], awake, pages)
    assert len(pages) == 1 and pages[0].startswith("EVENT_POLL_FAILED exec → ")


def test_a_clean_poll_clears_the_streak(fresh_orchestrator_dir, events_cfg, monkeypatch, awake):
    fakes = _install(monkeypatch, tracker=FakeSource(raises=events.PollError("boom")))
    _sub()
    monkeypatch.setattr(event_rules, "run", lambda now: [])
    pages = []
    _steps([0, 300], awake, pages)
    assert _streaks("tracker")["*"]["n"] == 2
    fakes["tracker"].raises = None
    _steps([600], awake, pages)
    assert "*" not in _streaks("tracker")
    fakes["tracker"].raises = events.PollError("boom")
    [records] = _steps([900], awake, pages)
    assert records[0]["streak"] == 1 and records[0]["held"] is True
    assert pages == []


def test_a_budget_cut_selector_keeps_its_streak_and_a_succeeding_one_clears_it(fresh_orchestrator_dir, events_cfg,
                                                                              monkeypatch, awake):
    _install(monkeypatch, tracker=FakeSource(result=_sequence(_failed("ABC-2"), _failed(budget_spent=["ABC-2"]),
                                                              _failed("ABC-2"), _failed())))
    _sub()
    monkeypatch.setattr(event_rules, "run", lambda now: [])
    pages = []
    _steps([0, 300], awake, pages)
    assert _streaks("tracker")["ABC-2"]["n"] == 1
    [records] = _steps([600], awake, pages)
    assert next(r for r in records if r["type"] == "events_poll_failed")["streak"] == 2
    _steps([900], awake, pages)
    assert "ABC-2" not in _streaks("tracker")


def test_failures_of_alternating_selectors_never_page(fresh_orchestrator_dir, events_cfg, monkeypatch, awake):
    _install(monkeypatch, tracker=FakeSource(result=_sequence(*[_failed("ABC-1" if i % 2 == 0 else "ABC-2")
                                                                for i in range(8)])))
    _sub()
    monkeypatch.setattr(event_rules, "run", lambda now: [])
    pages = []
    every = _steps([300 * i for i in range(8)], awake, pages)
    assert pages == []
    assert [next(r for r in rs if r["type"] == "events_poll_failed")["streak"] for rs in every] == [1] * 8


def test_a_sleep_between_failures_does_not_count_toward_ten_minutes(fresh_orchestrator_dir, events_cfg, monkeypatch,
                                                                    awake):
    _install(monkeypatch, tracker=FakeSource(raises=events.PollError("boom")))
    _sub()
    monkeypatch.setattr(event_rules, "run", lambda now: [])
    pages = []
    emit = lambda line, manager_name=None: pages.append(line)  # noqa: E731
    held = []
    for now, at in ((T0, 0.0), (T0 + 3600, 100.0), (T0 + 7200, 200.0)):
        awake["t"] = at
        held.append(event_poll.step(now, [], emit)[0].get("held"))
    assert held == [True, True, True] and pages == []


class SleepingSource(FakeSource):
    def __init__(self, wall, **kw):
        super().__init__(**kw)
        self.wall = wall

    def poll(self, subs, rules, cursor, env, now, call=None, until=None):
        self.wall["t"] += 30
        return super().poll(subs, rules, cursor, env, now, call, until)


def test_a_poll_the_machine_slept_through_neither_counts_nor_pages(fresh_orchestrator_dir, events_cfg, monkeypatch,
                                                                  awake):
    wall = {"t": T0}
    monkeypatch.setattr(failure_streak, "wall_seconds", lambda: wall["t"])
    fakes = _install(monkeypatch, tracker=FakeSource(raises=events.PollError("boom")))
    _sub()
    monkeypatch.setattr(event_rules, "run", lambda now: [])
    pages = []
    _steps([0], awake, pages)
    _install(monkeypatch, tracker=SleepingSource(wall, raises=events.PollError("boom")))
    [records] = _steps([300], awake, pages)
    assert records[0]["slept"] is True and records[0]["held"] is True and records[0]["streak"] == 1
    assert _streaks("tracker")["*"]["n"] == 1
    failure_streak.save(paths.EVENTS_CURSORS / "tracker" / "failures.json", {"*": {"n": 5, "since": T0, "awake": 0}})
    [records] = _steps([2000], awake, pages)
    assert records[0]["slept"] is True and pages == []
    _install(monkeypatch, tracker=fakes["tracker"])
    _steps([2300], awake, pages)
    assert len(pages) == 1


def test_a_corrupt_streak_file_starts_a_fresh_count(fresh_orchestrator_dir, events_cfg, monkeypatch, awake):
    (paths.EVENTS_CURSORS / "tracker").mkdir(parents=True)
    (paths.EVENTS_CURSORS / "tracker" / "failures.json").write_text("not json")
    _install(monkeypatch, tracker=FakeSource(raises=events.PollError("boom")))
    _sub()
    records = event_poll.run(T0)
    assert records[0]["streak"] == 1 and records[0]["held"] is True


def test_an_unwritable_streak_file_pages_the_failure_and_still_delivers(fresh_orchestrator_dir, events_cfg,
                                                                        monkeypatch, awake):
    (paths.EVENTS_CURSORS / "tracker" / "failures.json").mkdir(parents=True)
    _install(monkeypatch, tracker=FakeSource(([_event()], {"comments": {"ABC-1": "2"},
                                                           event_sources.FAILED: [["ABC-2", "x"]]})))
    _sub()
    monkeypatch.setattr(event_rules, "run", lambda now: [])
    pages = []
    records = event_poll.step(T0, [], lambda line, manager_name=None: pages.append(line))
    failed = next(r for r in records if r["type"] == "events_poll_failed")
    assert "held" not in failed and len(pages) == 1 and pages[0].startswith("EVENT_POLL_FAILED tracker → ")
    assert [e.id for e in events.recent("tracker", T0)] == ["tracker:ABC-1:comment:2"]
    assert _cursor("tracker")["comments"] == {"ABC-1": "2"}
    assert (paths.EVENTS_CURSORS / "tracker" / ".last").exists()


def _lock_state(path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "held"
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return "free"


def test_step_runs_the_rules_under_the_rules_lock_and_no_source_lock(fresh_orchestrator_dir, events_cfg,
                                                                     monkeypatch):
    _install(monkeypatch)
    _sub()
    seen = []

    def rules_run(now):
        seen.append(_lock_state(paths.EVENTS / ".rules.lock"))
        seen.append(_lock_state(paths.EVENTS_CURSORS / "tracker" / ".poll.lock"))
        return [{"type": "event_page", "rule": "r", "task_key": None, "line": "x"}]

    monkeypatch.setattr(event_rules, "run", rules_run)
    records = event_poll.step(T0, [], lambda line, manager_name=None: None)
    assert seen == ["held", "free"]
    assert records == [{"type": "events_polled", "source": "tracker", "n": 0},
                       {"type": "event_page", "rule": "r", "task_key": None, "line": "x"}]


def test_a_slow_source_does_not_delay_another(fresh_orchestrator_dir, events_cfg, monkeypatch):
    fakes = _install(monkeypatch, tracker=FakeSource(sleep=0.5))
    _sub("s1", "tracker")
    _sub("s2", "deploy", match={"repo": "o/r", "env": "staging", "sha": "a" * 40})
    started = time.time()
    records = event_poll.run(T0)
    assert time.time() - started >= 0.5
    assert fakes["deploy"].calls[0]["at"] - started < 0.1
    assert {"type": "events_polled", "source": "tracker", "n": 0} in records
    assert {"type": "events_polled", "source": "deploy", "n": 0} in records


def _append_fails_for(monkeypatch, source):
    real = events.append

    def append(event):
        if event.source == source:
            raise OSError("disk full")
        return real(event)

    monkeypatch.setattr(events, "append", append)


def test_an_exception_outside_the_poll_fails_only_its_source(fresh_orchestrator_dir, events_cfg, monkeypatch):
    fakes = _install(monkeypatch, deploy=FakeSource(([_event("deploy:o/r:a:staging:1", "deploy", "job")], {})))
    _sub("s1", "tracker")
    _sub("s2", "deploy", match={"repo": "o/r", "env": "staging", "sha": "a" * 40})
    _append_fails_for(monkeypatch, "deploy")
    records = event_poll.run(T0)
    assert records == [{"type": "events_polled", "source": "tracker", "n": 0},
                       {"type": "events_polled", "source": "deploy", "n": 1},
                       {"type": "events_poll_failed", "source": "deploy", "error": "OSError: disk full"}]
    assert len(fakes["tracker"].calls) == 1


def test_a_source_no_longer_due_under_its_lock_is_skipped(fresh_orchestrator_dir, events_cfg, monkeypatch):
    fakes = _install(monkeypatch)
    live = [_sub("s1", "tracker")]
    (paths.EVENTS_CURSORS / "tracker").mkdir(parents=True, exist_ok=True)
    (paths.EVENTS_CURSORS / "tracker" / ".last").write_text(json.dumps({"ts": T0}))
    assert event_poll._poll_source("tracker", T0, live, config.events_rules()) == []
    assert fakes["tracker"].calls == []


def test_a_batch_is_appended_in_ts_and_id_order(fresh_orchestrator_dir, events_cfg, monkeypatch):
    batch = [_event("tracker:ABC-1:comment:b"), _event("tracker:ABC-1:comment:a")]
    _install(monkeypatch, tracker=FakeSource((batch, {})))
    _sub()
    event_poll.run(T0)
    lines = [json.loads(line)["id"] for f in sorted(paths.EVENTS_LOG.glob("*.jsonl")) for line in
             f.read_text().splitlines()]
    assert lines == ["tracker:ABC-1:comment:a", "tracker:ABC-1:comment:b"]


def test_a_busy_rules_lock_skips_the_rules_and_keeps_the_polls(fresh_orchestrator_dir, events_cfg, monkeypatch):
    _install(monkeypatch)
    _sub()
    calls = []
    monkeypatch.setattr(event_rules, "run", lambda now: calls.append(now) or [])
    paths.EVENTS.mkdir(parents=True, exist_ok=True)
    with open(paths.EVENTS / ".rules.lock", "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        records = event_poll.step(T0, [], lambda line, manager_name=None: None)
    assert records == [{"type": "events_polled", "source": "tracker", "n": 0}, {"type": "events_rules_busy"}]
    assert calls == []


def test_a_failing_rule_is_paged_once_an_hour(fresh_orchestrator_dir, events_cfg, monkeypatch):
    _install(monkeypatch)
    failed = {"type": "event_rule_failed", "rule": "sprint", "error": "RuntimeError: x"}
    monkeypatch.setattr(event_rules, "run", lambda now: [dict(failed)])
    pages = []
    emit = lambda line, manager_name=None: pages.append(line)  # noqa: E731
    assert failed in event_poll.step(T0, [], emit)
    assert len(pages) == 1 and pages[0].startswith("EVENT_RULE_FAILED sprint → ")
    event_poll.step(T0 + 200, [], emit)
    assert len(pages) == 1
    event_poll.step(T0 + 3700, [], emit)
    assert len(pages) == 2


def test_two_page_kinds_of_one_name_throttle_apart(fresh_orchestrator_dir, events_cfg, monkeypatch):
    fakes = _install(monkeypatch, chat=SkippingSource(([], {"seen": [], "failed": [["notification k1", "no subtitle"]]})))
    _sub(source="chat", match={"sender": "Ann Lee"})
    monkeypatch.setattr(event_rules, "run", lambda now: [])
    pages = []
    emit = lambda line, manager_name=None: pages.append(line)  # noqa: E731
    event_poll.step(T0, [], emit)
    fakes["chat"].result = ([], {})
    monkeypatch.setattr(event_rules, "run",
                        lambda now: [{"type": "event_rule_failed", "rule": "chat", "error": "RuntimeError: x"}])
    event_poll.step(T0 + 200, [], emit)
    assert [p.split(" → ")[0] for p in pages] == ["EVENT_POLL_FAILED chat", "EVENT_RULE_FAILED chat"]


def test_a_source_writes_its_records_before_its_cursor(fresh_orchestrator_dir, events_cfg, monkeypatch):
    note = {"type": "events_caught_up", "source": "deploy", "n": 1}
    _install(monkeypatch, deploy=FakeSource(([], {"x": 1, "notes": [note]})))
    _sub("s2", "deploy", match={"repo": "o/r", "env": "staging", "sha": "a" * 40})
    written = []

    def record(r):
        written.append((r["type"], (paths.EVENTS_CURSORS / "deploy" / "cursor.json").exists()))

    event_poll.run(T0, record=record)
    assert written == [("events_polled", False), ("events_caught_up", False)]
    assert _cursor("deploy") == {"x": 1}


def test_a_thread_exception_reaches_the_writer(fresh_orchestrator_dir, events_cfg, monkeypatch):
    _install(monkeypatch, deploy=FakeSource(([_event("deploy:o/r:a:staging:1", "deploy", "job")], {})))
    _sub("s2", "deploy", match={"repo": "o/r", "env": "staging", "sha": "a" * 40})
    _append_fails_for(monkeypatch, "deploy")
    written = []
    event_poll.run(T0, record=written.append)
    assert written == [{"type": "events_polled", "source": "deploy", "n": 1},
                       {"type": "events_poll_failed", "source": "deploy", "error": "OSError: disk full"}]


def test_a_source_hands_its_records_to_the_writer_before_it_appends_its_events(fresh_orchestrator_dir, events_cfg,
                                                                              monkeypatch):
    note = {"type": "events_caught_up", "source": "deploy", "n": 1}
    _install(monkeypatch, deploy=FakeSource(([_event("deploy:o/r:a:staging:1", "deploy", "job")], {"notes": [note]})))
    _sub("s2", "deploy", match={"repo": "o/r", "env": "staging", "sha": "a" * 40})
    written = []
    event_poll.run(T0, record=lambda r: written.append((r["type"], len(events.recent("deploy", T0)))))
    assert written == [("events_polled", 0), ("events_caught_up", 0)]
    assert [e.id for e in events.recent("deploy", T0)] == ["deploy:o/r:a:staging:1"]


class Stop(BaseException):
    pass


def test_a_stop_after_the_writer_and_before_the_append_repeats_the_records_on_the_retry(fresh_orchestrator_dir,
                                                                                       events_cfg, monkeypatch):
    import threading
    note = {"type": "events_caught_up", "source": "deploy", "n": 1}
    batch = lambda subs, cursor: ([_event("deploy:o/r:a:staging:1", "deploy", "job")], {"notes": [dict(note)]})  # noqa: E731
    _install(monkeypatch, deploy=FakeSource(batch))
    _sub("s2", "deploy", match={"repo": "o/r", "env": "staging", "sha": "a" * 40})
    monkeypatch.setattr(threading, "excepthook", lambda args: None)

    def stop(r):
        if r["type"] == "events_caught_up":
            raise Stop()

    event_poll.run(T0, record=stop)
    assert events.recent("deploy", T0) == []
    assert not (paths.EVENTS_CURSORS / "deploy" / "cursor.json").exists()
    written = []
    event_poll.run(T0, record=written.append)
    assert written == [{"type": "events_polled", "source": "deploy", "n": 1}, note]


@pytest.mark.parametrize("error", [OSError("ledger gone"), TypeError("not serializable")])
def test_a_writer_error_does_not_hold_the_cursor(fresh_orchestrator_dir, events_cfg, monkeypatch, error, capsys):
    _install(monkeypatch, deploy=FakeSource(([], {"x": 1})))
    _sub("s2", "deploy", match={"repo": "o/r", "env": "staging", "sha": "a" * 40})

    def record(r):
        raise error

    assert event_poll.run(T0, record=record) == [{"type": "events_polled", "source": "deploy", "n": 0}]
    assert _cursor("deploy") == {"x": 1}
    assert json.loads((paths.EVENTS_CURSORS / "deploy" / ".last").read_text()) == {"ts": T0}
    assert f"record writer failed ({type(error).__name__}: {error})" in capsys.readouterr().err


def test_a_failing_prune_keeps_the_written_poll_records(fresh_orchestrator_dir, events_cfg, monkeypatch):
    note = {"type": "events_caught_up", "source": "deploy", "n": 1}
    _install(monkeypatch, deploy=FakeSource(([], {"notes": [note]})))
    _sub("s2", "deploy", match={"repo": "o/r", "env": "staging", "sha": "a" * 40})

    def prune(now):
        raise OSError("prune failed")

    monkeypatch.setattr(event_poll, "_prune", prune)
    written = []
    with pytest.raises(OSError):
        event_poll.step(T0, [], lambda line, manager_name=None: None, record=written.append)
    assert written == [{"type": "events_polled", "source": "deploy", "n": 0}, note]


def test_source_notes_become_records_and_leave_the_cursor(fresh_orchestrator_dir, events_cfg, monkeypatch):
    note = {"type": "events_caught_up", "source": "tracker"}
    _install(monkeypatch, tracker=FakeSource(([], {"x": 1, "notes": [note]})))
    _sub()
    assert event_poll.run(T0) == [{"type": "events_polled", "source": "tracker", "n": 0}, note]
    assert _cursor("tracker") == {"x": 1}

