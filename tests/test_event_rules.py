import json
import time
import urllib.error

import pytest

from dockwright import config, event_rules, events, paths, pipeline, state, waiting_on
from dockwright.event_sources import tracker
from dockwright.events import Event
from tests.test_mcp_tools import fresh_orchestrator_dir  # noqa: F401

T0 = 1792700000.0
RULE = ('[events]\nenabled = true\n[events.rules.sprint]\nsource = "tracker"\nkind = "sprint-assigned"\naction = "start"\n'
        'schema = "ticket"\ndomain = "general"\nmax_open = 2\ntypes = ["Story"]\nskip_status = ["Blocked"]\n')
ENV = {"TRACKER_URL": "https://j", "TRACKER_EMAIL": "me@x", "TRACKER_API_TOKEN": "tok"}


@pytest.fixture
def rule_cfg(tmp_path, monkeypatch):
    cfg = tmp_path / "events.toml"
    cfg.write_text(RULE)
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))

    def write(text):
        cfg.write_text(text)

    return write


class Fold:
    def __init__(self, closed, open_requests=(), tokens=("t1",)):
        self.closed = closed
        self.open_requests = list(open_requests)
        self.tokens = {tid: pipeline.Token(tid, "intake", 1, None, "", None) for tid in tokens}
        self.held = {}


class Engine:
    def __init__(self, fail=None):
        self.starts = []
        self.journal = []
        self.fail = fail
        self.closed = set()
        self.known = set()
        self.values = {}
        self.requests = {}
        self.questions = {}
        self.statuses = {}
        self.status_reads = []
        self.tracker_down = False
        self.unreadable = set()
        self.probes = 0

    async def pipeline_start_impl(self, manager_sid, task_key, schema, cwd, ask, domain=None):
        self.starts.append((manager_sid, task_key, schema, cwd, ask, domain))
        if self.fail:
            raise ValueError(self.fail)
        self.known.add(task_key)
        return {"state": "committed"}

    def _load_snapshot_for(self, key):
        return object() if key in self.known else None

    def _engine_fold(self, key, tool):
        if key in self.unreadable:
            raise ValueError("bad line")
        return Fold({"reason": "x"} if key in self.closed else None)

    def _append_task_event(self, key, event):
        self.journal.append((key, event))

    def _process_index(self):
        self.probes += 1
        return {}

    def _list_artifacts(self, key):
        return []

    def _read_events(self, path):
        return []

    def _waiting_on_status(self, key, snap, events, stamps, now, index=None, assignments=None):
        closed = key in self.closed
        values = self.values.get(key, "worker:intake")
        values = values if isinstance(values, dict) else {"t1": values}
        questions = self.questions.get(key, [])
        return {"waiting_on": "none" if closed else waiting_on.work_value(list(values.values()), bool(questions)),
                "fold_error": None, "tokens": values, "questions": questions,
                "fold": Fold({"reason": "x"} if closed else None, self.requests.get(key, ()), values)}

    def tracker_call(self, method, url, env, timeout=None):
        key = url.split("?")[0].rsplit("/", 1)[1]
        self.status_reads.append(key)
        if self.tracker_down:
            raise urllib.error.URLError("down")
        if key in self.statuses and self.statuses[key] is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        name, category = self.statuses.get(key, ("To Do", "new"))
        return {"fields": {"status": {"name": name, "statusCategory": {"key": category}}}}


class Known(Engine):
    async def pipeline_start_impl(self, manager_sid, task_key, schema, cwd, ask, domain=None):
        if task_key in self.known:
            raise ValueError(f"{task_key} already has a token")
        return await super().pipeline_start_impl(manager_sid, task_key, schema, cwd, ask, domain)


def _install(monkeypatch, engine=None, managers=None):
    engine = engine or Engine()
    monkeypatch.setattr(event_rules, "_engine", lambda: engine)
    monkeypatch.setattr(event_rules, "_live_managers",
                        lambda: [{"claude_sid": "m1", "name": "proud", "domain": "general"}] if managers is None
                        else managers)
    monkeypatch.setattr(events, "load_env", lambda path: (dict(ENV), None))
    monkeypatch.setattr(tracker, "default_call", engine.tracker_call)
    return engine


def _sprint(key, ts=T0):
    e = Event(f"tracker:{key}:sprint-assigned", ts, "tracker", "sprint-assigned", {"issue": key}, "",
              f"{key} sum {key} (Story, To Do)", {"url": f"https://j/browse/{key}", "type": "Story", "status": "To Do"})
    events.append(e)
    return e


def _cursor():
    return json.loads((paths.EVENTS_CURSORS / "rules" / "sprint.json").read_text())


def _arm():
    event_rules.run(T0 - 1)


def test_a_sprint_assigned_event_starts_the_ticket_schema_under_the_domain_manager(fresh_orchestrator_dir, rule_cfg,
                                                                                   monkeypatch):
    engine = _install(monkeypatch)
    _arm()
    e = _sprint("ABC-9")
    records = event_rules.run(T0 + 1)
    assert engine.starts == [("m1", "ABC-9", "ticket", str(paths.ensure_worker_home()),
                              "ABC-9 sum ABC-9 (Story, To Do)\nhttps://j/browse/ABC-9", "general")]
    assert records == [{"type": "event_started", "rule": "sprint", "task_key": "ABC-9", "event_id": e.id,
                        "manager_sid": "m1", "manager_name": "proud"}]
    assert engine.journal == [("ABC-9", {"type": "event_started", "rule": "sprint", "event_id": e.id})]
    cursor = _cursor()
    assert "ABC-9" in cursor["started"] and cursor["last"] == [e.ts, e.id]


def test_a_manager_without_a_domain_counts_as_general(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    engine = _install(monkeypatch, managers=[{"claude_sid": "m1", "name": "proud"}])
    _arm()
    _sprint("ABC-9")
    records = event_rules.run(T0 + 1)
    assert [s[1] for s in engine.starts] == ["ABC-9"] and records[0]["manager_name"] == "proud"
    engine = _install(monkeypatch, managers=[{"claude_sid": "m2", "name": "other", "domain": "ops"}])
    _sprint("ABC-10", T0 + 1)
    records = event_rules.run(T0 + 2)
    assert engine.starts == [] and records[0]["type"] == "event_stalled"


def test_a_disabled_rule_starts_nothing_and_moves_no_cursor(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    rule_cfg(RULE + "enabled = false\n")
    engine = _install(monkeypatch)
    _sprint("ABC-9")
    assert event_rules.run(T0 + 1) == [] and engine.starts == []
    assert not (paths.EVENTS_CURSORS / "rules" / "sprint.json").exists()
    rule_cfg('[events]\nenabled = true\n[events.rules.bad]\nsource = "tracker"\nkind = "sprint-assigned"\naction = "start"\n')
    assert event_rules.run(T0 + 2) == [] and engine.starts == []


def test_no_live_manager_of_the_domain_waits_and_pages_once_an_hour(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    engine = _install(monkeypatch, managers=[])
    _arm()
    _sprint("ABC-9")
    records = event_rules.run(T0 + 1)
    assert engine.starts == [] and records == [{"type": "event_stalled", "rule": "sprint", "domain": "general",
                                                "since": "2026-10-22T20:13:21Z"}]
    assert _cursor()["last"] == [T0 - 1, ""]
    assert event_rules.run(T0 + 100) == []
    assert event_rules.run(T0 + 3700) == [{"type": "event_stalled", "rule": "sprint", "domain": "general",
                                           "since": "2026-10-22T20:13:21Z"}]
    assert event_rules.run(T0 + 3760) == [] and event_rules.run(T0 + 7200) == []
    assert len(event_rules.run(T0 + 7400)) == 1 and event_rules.run(T0 + 7460) == []
    _install(monkeypatch, engine)
    records = event_rules.run(T0 + 4000)
    assert [s[1] for s in engine.starts] == ["ABC-9"] and records[0]["type"] == "event_started"
    assert _cursor()["stalled_since"] is None


def test_the_cap_holds_back_a_third_open_work(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    engine = _install(monkeypatch)
    _arm()
    _sprint("ABC-1", T0)
    _sprint("ABC-2", T0 + 1)
    _sprint("ABC-3", T0 + 2)
    records = event_rules.run(T0 + 10)
    assert [s[1] for s in engine.starts] == ["ABC-1", "ABC-2"]
    assert [r["type"] for r in records] == ["event_started", "event_started"]
    assert _cursor()["last"] == [T0 + 1, "tracker:ABC-2:sprint-assigned"]
    assert event_rules.run(T0 + 20) == [] and len(engine.starts) == 2
    engine.closed.add("ABC-1")
    records = event_rules.run(T0 + 30)
    assert [s[1] for s in engine.starts] == ["ABC-1", "ABC-2", "ABC-3"] and records[0]["task_key"] == "ABC-3"


def test_a_key_that_already_has_a_token_moves_the_cursor_without_a_start(fresh_orchestrator_dir, rule_cfg,
                                                                         monkeypatch):
    engine = _install(monkeypatch, Engine(fail="ABC-9 already has a token"))
    _arm()
    e = _sprint("ABC-9")
    records = event_rules.run(T0 + 1)
    assert records == [{"type": "event_rule_skipped", "rule": "sprint", "task_key": "ABC-9", "event_id": e.id,
                        "reason": "ABC-9 already has a token"}]
    assert _cursor()["last"] == [e.ts, e.id] and "ABC-9" not in _cursor()["started"]
    assert event_rules.run(T0 + 2) == [] and len(engine.starts) == 1


def test_a_start_that_raises_mid_batch_keeps_the_earlier_starts_on_disk_and_the_cap_holds(fresh_orchestrator_dir,
                                                                                          rule_cfg, monkeypatch):
    class Boom(Engine):
        async def pipeline_start_impl(self, manager_sid, task_key, schema, cwd, ask, domain=None):
            if task_key in self.known:
                raise ValueError("token exists")
            if task_key == "ABC-2" and not getattr(self, "boomed", False):
                self.boomed = True
                raise RuntimeError("spawn failed")
            return await super().pipeline_start_impl(manager_sid, task_key, schema, cwd, ask, domain)

    engine = _install(monkeypatch, Boom())
    _arm()
    for i, k in enumerate(["ABC-1", "ABC-2", "ABC-3"]):
        _sprint(k, ts=T0 + i)
    records = event_rules.run(T0 + 10)
    assert [(r["type"], r.get("task_key")) for r in records] == [("event_started", "ABC-1"),
                                                                ("event_rule_failed", None)]
    assert records[-1] == {"type": "event_rule_failed", "rule": "sprint", "error": "RuntimeError: spawn failed"}
    assert "ABC-1" in _cursor()["started"] and _cursor()["last"] == [T0, "tracker:ABC-1:sprint-assigned"]
    records = event_rules.run(T0 + 20)
    assert [r["task_key"] for r in records] == ["ABC-2"]
    assert sorted(engine.known) == ["ABC-1", "ABC-2"]
    assert sorted(_cursor()["started"]) == ["ABC-1", "ABC-2"]


def test_a_work_started_before_a_stop_still_holds_its_slot(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    class Stops(Known):
        async def pipeline_start_impl(self, manager_sid, task_key, schema, cwd, ask, domain=None):
            result = await super().pipeline_start_impl(manager_sid, task_key, schema, cwd, ask, domain)
            if task_key == "ABC-1" and not getattr(self, "stopped", False):
                self.stopped = True
                raise RuntimeError("stopped after the start")
            return result

    engine = _install(monkeypatch, Stops())
    _arm()
    _three()
    records = event_rules.run(T0 + 10)
    assert records == [{"type": "event_rule_failed", "rule": "sprint", "error": "RuntimeError: stopped after the start"}]
    assert list(_cursor()["started"]) == ["ABC-1"]
    records = event_rules.run(T0 + 20)
    assert [(r["type"], r["task_key"]) for r in records] == [("event_rule_skipped", "ABC-1"),
                                                             ("event_started", "ABC-2")]
    assert sorted(_cursor()["started"]) == ["ABC-1", "ABC-2"] and "ABC-3" not in engine.known
    engine.closed.add("ABC-1")
    assert [r["task_key"] for r in event_rules.run(T0 + 30)] == ["ABC-3"]


def _sprint_unit(key, ts, **related):
    e = Event(f"tracker:{key}:sprint-assigned", ts, "tracker", "sprint-assigned", {"issue": key}, "",
              f"{key} sum {key}", {"url": f"https://j/browse/{key}", "type": "Story", "status": "To Do", **related})
    events.append(e)
    return e


def test_an_event_whose_parent_or_sub_task_has_an_open_work_starts_nothing(fresh_orchestrator_dir, rule_cfg,
                                                                           monkeypatch):
    engine = _install(monkeypatch)
    engine.known.update({"ABC-2", "ABC-5", "ABC-7"})
    engine.closed.add("ABC-5")
    _arm()
    p = _sprint_unit("ABC-1", T0, subtasks=["ABC-2", "ABC-3"])
    s = _sprint_unit("ABC-8", T0 + 1, parent="ABC-7")
    ok = _sprint_unit("ABC-6", T0 + 2, parent="ABC-5")
    records = event_rules.run(T0 + 10)
    assert [s[1] for s in engine.starts] == ["ABC-6"]
    assert records[:2] == [
        {"type": "event_rule_skipped", "rule": "sprint", "task_key": "ABC-1", "event_id": p.id,
         "reason": "ABC-2 has an open work"},
        {"type": "event_rule_skipped", "rule": "sprint", "task_key": "ABC-8", "event_id": s.id,
         "reason": "ABC-7 has an open work"}]
    assert records[2]["type"] == "event_started" and records[2]["task_key"] == "ABC-6"
    assert sorted(_cursor()["started"]) == ["ABC-6"] and _cursor()["last"] == [ok.ts, ok.id]


def test_a_payload_whose_parent_or_subtasks_is_not_a_key_list_starts_as_before(fresh_orchestrator_dir, rule_cfg,
                                                                               monkeypatch):
    engine = _install(monkeypatch)
    engine.known.add("ABC-2")
    _arm()
    _sprint_unit("ABC-1", T0, subtasks=5, parent=["ABC-2"])
    records = event_rules.run(T0 + 10)
    assert [s[1] for s in engine.starts] == ["ABC-1"] and records[0]["type"] == "event_started"


def _three():
    for i, k in enumerate(["ABC-1", "ABC-2", "ABC-3"]):
        _sprint(k, ts=T0 + i)


def _sprint_cursor(keys):
    path = paths.EVENTS_CURSORS / "tracker" / "cursor.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sprint": {"keys": [], "shown": keys}}))


@pytest.mark.parametrize("value,held", [("worker:design", True), ("stalled:plan", True), ("human:question", True),
                                        ("human:approval-dialog", True), ("human:gate", False),
                                        ("external:time", False), ("external:merge", False), ("none", False)])
def test_a_work_holds_its_slot_while_it_works_or_waits_on_a_short_answer(fresh_orchestrator_dir, rule_cfg,
                                                                         monkeypatch, value, held):
    engine = _install(monkeypatch)
    _arm()
    _three()
    event_rules.run(T0 + 10)
    engine.values["ABC-1"] = value
    assert [r["task_key"] for r in event_rules.run(T0 + 20)] == ([] if held else ["ABC-3"])


def test_a_work_with_an_open_request_holds_its_slot(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    engine = _install(monkeypatch)
    _arm()
    _three()
    event_rules.run(T0 + 10)
    engine.values["ABC-1"] = "external:time"
    engine.requests["ABC-1"] = [{"request_id": "r1", "pending": {"reason": "paused"}}]
    assert event_rules.run(T0 + 20) == []
    engine.requests.clear()
    assert [r["task_key"] for r in event_rules.run(T0 + 30)] == ["ABC-3"]


def test_a_refused_start_takes_no_slot(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    engine = _install(monkeypatch, Known())
    engine.known.update({"ABC-1", "ABC-2"})
    _arm()
    _three()
    records = event_rules.run(T0 + 10)
    assert [(r["type"], r["task_key"]) for r in records] == [
        ("event_rule_skipped", "ABC-1"), ("event_rule_skipped", "ABC-2"), ("event_started", "ABC-3")]
    assert sorted(_cursor()["started"]) == ["ABC-3"] and engine.probes == 1


def test_a_parent_work_at_a_gate_still_skips_its_sub_task_without_a_read(fresh_orchestrator_dir, rule_cfg,
                                                                          monkeypatch):
    engine = _install(monkeypatch)
    engine.known.add("ABC-7")
    engine.values["ABC-7"] = "human:gate"
    _arm()
    s = _sprint_unit("ABC-8", T0, parent="ABC-7")
    assert event_rules.run(T0 + 10) == [{"type": "event_rule_skipped", "rule": "sprint", "task_key": "ABC-8",
                                         "event_id": s.id, "reason": "ABC-7 has an open work"}]
    assert engine.status_reads == []


def test_a_parent_work_whose_log_does_not_read_still_skips_its_sub_task(fresh_orchestrator_dir, rule_cfg,
                                                                        monkeypatch):
    engine = _install(monkeypatch)
    engine.known.add("ABC-7")
    engine.unreadable.add("ABC-7")
    _arm()
    s = _sprint_unit("ABC-8", T0, parent="ABC-7")
    assert event_rules.run(T0 + 10) == [{"type": "event_rule_skipped", "rule": "sprint", "task_key": "ABC-8",
                                         "event_id": s.id, "reason": "ABC-7 has an open work"}]


def test_a_work_on_a_sprint_key_counts_whoever_started_it(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    engine = _install(monkeypatch)
    engine.known.update({"ABC-5", "ABC-6"})
    _arm()
    _sprint_cursor(["ABC-5", "ABC-6", "ABC-1"])
    _sprint("ABC-1")
    assert event_rules.run(T0 + 10) == [] and engine.starts == []
    engine.values["ABC-5"] = "human:gate"
    assert [r["task_key"] for r in event_rules.run(T0 + 20)] == ["ABC-1"]


def test_a_sprint_event_whose_issue_left_to_do_while_it_waited_starts_nothing(fresh_orchestrator_dir, rule_cfg,
                                                                             monkeypatch):
    engine = _install(monkeypatch)
    _arm()
    _three()
    event_rules.run(T0 + 10)
    engine.statuses["ABC-3"] = ("In Development", "indeterminate")
    engine.values["ABC-1"] = "human:gate"
    e3 = "tracker:ABC-3:sprint-assigned"
    assert event_rules.run(T0 + 20) == [{"type": "event_rule_skipped", "rule": "sprint", "task_key": "ABC-3",
                                         "event_id": e3, "reason": "ABC-3 is past To Do"}]
    assert "ABC-3" not in _cursor()["started"] and _cursor()["last"][1] == e3


def test_a_failed_status_read_holds_the_event(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    engine = _install(monkeypatch)
    _arm()
    _sprint("ABC-1")
    engine.tracker_down = True
    assert event_rules.run(T0 + 10) == [] and engine.starts == [] and _cursor()["last"] == [T0 - 1, ""]
    engine.tracker_down = False
    monkeypatch.setattr(events, "load_env", lambda path: (None, "missing"))
    assert event_rules.run(T0 + 20) == [] and engine.starts == []
    monkeypatch.setattr(events, "load_env", lambda path: (dict(ENV), None))
    assert [r["type"] for r in event_rules.run(T0 + 30)] == ["event_started"]


@pytest.mark.parametrize("statuses,reason", [
    ({"ABC-2": ("In Development", "indeterminate")}, "ABC-2 is past To Do"),
    ({"ABC-1": ("In Development", "indeterminate")}, "ABC-1 is past To Do"),
    ({"ABC-2": ("X", "")}, "ABC-2 is past To Do"),
    ({"ABC-1": ("Blocked", "indeterminate")}, "ABC-1 is in Blocked"),
    ({"ABC-1": None}, "ABC-1 was not found"),
    ({"ABC-1": ("New", "new"), "ABC-3": ("Blocked", "indeterminate"), "ABC-2": None}, None)])
def test_a_parent_unit_starts_only_while_it_and_its_sub_tasks_are_in_to_do(fresh_orchestrator_dir, rule_cfg,
                                                                           monkeypatch, statuses, reason):
    engine = _install(monkeypatch)
    engine.statuses.update(statuses)
    _arm()
    e = _sprint_unit("ABC-1", T0, subtasks=["ABC-2", "ABC-3"])
    records = event_rules.run(T0 + 10)
    if reason:
        assert engine.starts == [] and records == [{"type": "event_rule_skipped", "rule": "sprint",
                                                    "task_key": "ABC-1", "event_id": e.id, "reason": reason}]
    else:
        assert [s[1] for s in engine.starts] == ["ABC-1"]


def test_a_sub_task_that_starts_alone_is_tested_on_itself(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    engine = _install(monkeypatch)
    engine.statuses["ABC-7"] = ("In Development", "indeterminate")
    _arm()
    _sprint_unit("ABC-8", T0, parent="ABC-7")
    assert [r["type"] for r in event_rules.run(T0 + 10)] == ["event_started"]
    assert engine.status_reads == ["ABC-8"]


def test_the_slot_count_reads_the_engines_state(fresh_orchestrator_dir, monkeypatch, tmp_path):
    from dockwright import mcp_server
    from tests.test_mcp_tools import _append_malformed_commit, _seed_snapshot
    from tests.test_topic_questions import _manager
    monkeypatch.setattr(mcp_server, "_process_index", lambda: {})
    _seed_snapshot("K-9", tmp_path)
    assert event_rules._slots_held(mcp_server, ["K-9", "K-0"], time.time()) == 1
    _seed_snapshot("K-8", tmp_path)
    _append_malformed_commit("K-8")
    assert event_rules._slots_held(mcp_server, ["K-8"], time.time()) == 1
    _seed_snapshot("K-7", tmp_path)
    with open(paths.artifact_events_path("K-7"), "ab") as f:
        f.write('{"type": "note", "text": "é'.encode()[:-1])
    assert event_rules._slots_held(mcp_server, ["K-7"], time.time()) == 1
    _seed_snapshot("K-6", tmp_path)
    state.append_event(paths.artifact_events_path("K-6"), {"type": "token_held", "token_id": "t1", "node": "design",
                                                           "reason": "blocked on a vendor", "actor_sid": "m1"})
    assert event_rules._slots_held(mcp_server, ["K-6"], time.time()) == 0
    state.append_event(paths.artifact_events_path("K-9"), {"type": "work_closed", "token_id": "t1", "node": "design",
                                                           "reason": "done", "actor_sid": "m1"})
    assert event_rules._slots_held(mcp_server, ["K-9"], time.time()) == 0
    _manager("m1", "mgr")
    mcp_server.ask_engineer_impl("m1", "archive it?", task_key="K-9")
    assert event_rules._slots_held(mcp_server, ["K-9"], time.time()) == 0


def test_a_page_rule_records_a_page_for_the_domain_manager(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    rule_cfg(RULE.replace('action = "start"', 'action = "page"'))
    engine = _install(monkeypatch)
    _arm()
    e = _sprint("ABC-9")
    records = event_rules.run(T0 + 1)
    assert engine.starts == [] and records == [{"type": "event_page", "rule": "sprint", "line": e.summary,
                                                "event_id": e.id, "kind": "sprint-assigned", "manager_name": "proud"}]
    assert _cursor()["last"] == [e.ts, e.id]


def test_a_page_rule_without_a_manager_stalls_like_start(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    rule_cfg(RULE.replace('action = "start"', 'action = "page"'))
    _install(monkeypatch, managers=[])
    _arm()
    e = _sprint("ABC-9")
    first = event_rules.run(T0 + 1)
    assert [r["type"] for r in first] == ["event_stalled"] and _cursor()["last"] == [T0 - 1, ""]
    assert event_rules.run(T0 + 100) == []
    assert [r["type"] for r in event_rules.run(T0 + event_rules.STALL_PAGE_SEC + 1)] == ["event_stalled"]
    _install(monkeypatch)
    assert [r["event_id"] for r in event_rules.run(T0 + 8000)] == [e.id]
    assert _cursor()["last"] == [e.ts, e.id] and _cursor()["stalled_since"] is None


def test_a_page_rule_pages_only_the_newest_pending_event_of_a_wait(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    rule_cfg(RULE.replace('action = "start"', 'action = "page"'))
    _install(monkeypatch, managers=[])
    old = Event("tracker:ABC-9:sprint-assigned:1", T0, "tracker", "sprint-assigned", {"issue": "ABC-9"}, "", "ping 1", {})
    new = Event("tracker:ABC-9:sprint-assigned:2", T0 + 10, "tracker", "sprint-assigned", {"issue": "ABC-9"}, "", "ping 2", {})
    other = Event("tracker:ABC-8:sprint-assigned:1", T0 + 5, "tracker", "sprint-assigned", {"issue": "ABC-8"}, "", "p", {})
    _arm()
    for e in (old, new, other):
        events.append(e)
    assert [r["type"] for r in event_rules.run(T0 + 20)] == ["event_stalled"]
    _install(monkeypatch)
    records = event_rules.run(T0 + 30)
    assert [r["event_id"] for r in records] == [other.id, new.id]
    assert _cursor()["last"] == [new.ts, new.id] and event_rules.run(T0 + 40) == []


def test_a_page_rule_keeps_distinct_events_of_one_issue_apart_by_cursor_key(fresh_orchestrator_dir, rule_cfg,
                                                                            monkeypatch):
    rule_cfg(RULE.replace('action = "start"', 'action = "page"').replace('kind = "sprint-assigned"', 'kind = "comment"'))
    _install(monkeypatch)
    first = Event("tracker:ABC-9:comment:1", T0, "tracker", "comment", {"issue": "ABC-9"}, "1", "c1", {})
    second = Event("tracker:ABC-9:comment:2", T0 + 10, "tracker", "comment", {"issue": "ABC-9"}, "2", "c2", {})
    _arm()
    for e in (first, second):
        events.append(e)
    assert [r["event_id"] for r in event_rules.run(T0 + 20)] == [first.id, second.id]


def test_each_event_is_handled_once(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    engine = _install(monkeypatch)
    _arm()
    _sprint("ABC-9")
    event_rules.run(T0 + 1)
    assert event_rules.run(T0 + 2) == [] and len(engine.starts) == 1
    _sprint("ABC-10", T0 + 5)
    assert [r["task_key"] for r in event_rules.run(T0 + 6)] == ["ABC-10"]


PAGE = RULE.replace('action = "start"', 'action = "page"')
QUEUE = RULE.replace('action = "start"', 'action = "queue"')
STATE = (QUEUE.replace('source = "tracker"', 'source = "exec"').replace('kind = "sprint-assigned"', 'kind = "oncall-check"')
         + 'on_change = "state"\nrepeat_sec = 3600\nrepeat_values = ["failed"]\n')


def _queue():
    return json.loads((paths.EVENTS_QUEUES / "sprint.json").read_text())


def _queued_ids():
    return [x["id"] for x in _queue()["pending"]]


def _state(state, ts):
    e = Event(f"exec:oncall-check:{int(ts)}:{state}", ts, "exec", "oncall-check", {"state": state}, "",
              f"ONCALL_CHECK_{state.upper()}", {})
    return events.append(e), e


def test_a_rule_seen_for_the_first_time_starts_its_cursor_at_now_and_pages_no_backlog(fresh_orchestrator_dir, rule_cfg,
                                                                                      monkeypatch):
    rule_cfg(PAGE)
    _install(monkeypatch)
    _sprint("ABC-1", T0 - 86400 * 10)
    assert event_rules.run(T0) == []
    assert _cursor()["last"] == [T0, ""]
    _sprint("ABC-2", T0 + 5)
    assert [r["event_id"] for r in event_rules.run(T0 + 6)] == ["tracker:ABC-2:sprint-assigned"]


def test_an_event_of_the_seeding_tick_passes(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    rule_cfg(PAGE)
    _install(monkeypatch)
    _sprint("ABC-1", T0)
    assert [r["type"] for r in event_rules.run(T0)] == ["event_page"]


def test_a_rule_with_match_pages_only_events_whose_match_fields_equal_it(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    rule_cfg(PAGE + 'match = { issue = "ABC-9" }\n')
    _install(monkeypatch)
    _arm()
    _sprint("ABC-9")
    _sprint("ABC-8", T0 + 1)
    records = event_rules.run(T0 + 2)
    assert [r["event_id"] for r in records] == ["tracker:ABC-9:sprint-assigned"]
    assert _cursor()["last"][1] == "tracker:ABC-9:sprint-assigned"


def test_a_present_stop_file_moves_the_cursor_and_records_no_stall(fresh_orchestrator_dir, rule_cfg, tmp_path,
                                                                   monkeypatch):
    stop = tmp_path / "stop"
    rule_cfg(PAGE + f'stop_file = "{stop}"\n')
    _install(monkeypatch, managers=[])
    _arm()
    stop.write_text("")
    _sprint("ABC-9")
    assert event_rules.run(T0 + 1) == []
    assert _cursor()["last"] == [T0, "tracker:ABC-9:sprint-assigned"] and _cursor()["stalled_since"] is None


def test_a_removed_stop_file_pages_only_what_came_after_it(fresh_orchestrator_dir, rule_cfg, tmp_path, monkeypatch):
    stop = tmp_path / "stop"
    rule_cfg(PAGE + f'stop_file = "{stop}"\n')
    _install(monkeypatch)
    _arm()
    stop.write_text("")
    _sprint("ABC-9")
    assert event_rules.run(T0 + 1) == []
    stop.unlink()
    _sprint("ABC-10", T0 + 5)
    assert [r["event_id"] for r in event_rules.run(T0 + 6)] == ["tracker:ABC-10:sprint-assigned"]


def test_a_queue_rule_stores_the_event_and_moves_the_cursor_with_no_manager(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    rule_cfg(QUEUE)
    _install(monkeypatch, managers=[])
    _arm()
    e = _sprint("ABC-9")
    records = event_rules.run(T0 + 1)
    assert [r["type"] for r in records] == ["event_stalled"]
    assert _queued_ids() == [e.id] and _cursor()["last"] == [e.ts, e.id]
    assert event_rules.run(T0 + 100) == []
    assert [r["type"] for r in event_rules.run(T0 + 3700)] == ["event_stalled"]


def test_a_queue_rule_pages_a_live_manager_once_per_new_event_and_again_after_nudge_sec(fresh_orchestrator_dir, rule_cfg,
                                                                                        monkeypatch):
    rule_cfg(QUEUE)
    _install(monkeypatch)
    _arm()
    e = _sprint("ABC-9")
    assert event_rules.run(T0 + 1) == [{"type": "event_queue_page", "rule": "sprint", "count": 1, "line": e.summary,
                                        "event_id": e.id, "manager_name": "proud"}]
    assert event_rules.run(T0 + 2) == []
    again = event_rules.run(T0 + 601)
    assert len(again) == 1 and again[0]["count"] == 1 and again[0]["type"] == "event_queue_page"
    e2 = _sprint("ABC-10", T0 + 700)
    records = event_rules.run(T0 + 701)
    assert records[0]["count"] == 2 and records[0]["line"] == e.summary and records[0]["event_id"] == e2.id
    assert _cursor()["stalled_since"] is None


def test_an_acked_queue_sends_no_reminder(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    from dockwright import event_queue
    rule_cfg(QUEUE)
    _install(monkeypatch)
    _arm()
    _sprint("ABC-9")
    assert len(event_rules.run(T0 + 1)) == 1
    event_queue.ack("sprint", "*")
    assert event_rules.run(T0 + 1300) == []


def test_a_queue_rule_under_a_stop_file_queues_nothing(fresh_orchestrator_dir, rule_cfg, tmp_path, monkeypatch):
    stop = tmp_path / "stop"
    rule_cfg(QUEUE + f'stop_file = "{stop}"\n')
    _install(monkeypatch)
    _arm()
    stop.write_text("")
    e = _sprint("ABC-9")
    assert event_rules.run(T0 + 1) == []
    assert not (paths.EVENTS_QUEUES / "sprint.json").exists() or _queued_ids() == []
    assert _cursor()["last"] == [e.ts, e.id]


GATED = QUEUE + 'while = { kind = "shift" }\n'


def _utc(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _window(start, end, issued, ts=T0 - 100):
    events.append(Event(f"exec:shift:{start}:{end}:{issued}", ts, "exec", "shift", {}, "", "SHIFT",
                        {"start": _utc(start), "end": _utc(end), "issued": issued}))


def _nothing_queued():
    return not (paths.EVENTS_QUEUES / "sprint.json").exists() or _queued_ids() == []


def _dropped(records):
    return [r for r in records if r["type"] == "event_queue_dropped"]


def test_a_queue_rule_inside_its_while_window_queues_the_event(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    rule_cfg(GATED)
    _install(monkeypatch)
    _arm()
    _window(T0 - 3600, T0 + 3600, "h1")
    _sprint("ABC-9")
    event_rules.run(T0 + 1)
    assert _queued_ids() == ["tracker:ABC-9:sprint-assigned"]


def test_a_queue_rule_outside_its_while_window_moves_the_cursor_and_queues_nothing(fresh_orchestrator_dir, rule_cfg,
                                                                                   monkeypatch):
    rule_cfg(GATED)
    _install(monkeypatch)
    _arm()
    _window(T0 - 7200, T0 - 3600, "h1")
    e = _sprint("ABC-9")
    assert event_rules.run(T0 + 1) == []
    assert _nothing_queued() and _cursor()["last"] == [e.ts, e.id]


def test_a_queue_rule_with_while_and_no_stored_window_queues_nothing(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    rule_cfg(GATED)
    _install(monkeypatch)
    _arm()
    e = _sprint("ABC-9")
    assert event_rules.run(T0 + 1) == []
    assert _nothing_queued() and _cursor()["last"] == [e.ts, e.id]


def test_a_newer_issued_set_without_the_window_closes_the_rule(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    rule_cfg(GATED)
    _install(monkeypatch)
    _arm()
    _window(T0 - 3600, T0 + 3600, "h1")
    events.append(Event("exec:shift:none:h2", T0 - 50, "exec", "shift", {}, "", "SHIFT none", {"issued": "h2"}))
    _sprint("ABC-9")
    assert event_rules.run(T0 + 1) == []
    assert _nothing_queued()


def test_a_message_sent_before_the_window_is_not_queued_when_it_arrives_inside_it(fresh_orchestrator_dir, rule_cfg,
                                                                                   monkeypatch):
    rule_cfg(GATED)
    _install(monkeypatch)
    _arm()
    _window(T0 - 3600, T0 + 3600, "h1")
    for key, delivered in (("OLD-1", T0 - 7200), ("NEW-1", T0 - 60)):
        events.append(Event(f"tracker:{key}:sprint-assigned", T0, "tracker", "sprint-assigned", {"issue": key}, "",
                            f"{key} sum", {"delivered": delivered}))
    records = event_rules.run(T0 + 1)
    assert _queued_ids() == ["tracker:NEW-1:sprint-assigned"]
    assert {"type": "event_queue_dropped", "rule": "sprint", "count": 1, "reason": "window"} in records
    assert _cursor()["last"] == [T0, "tracker:OLD-1:sprint-assigned"]


def test_a_record_queued_in_one_window_is_dropped_before_a_later_window_reminds(fresh_orchestrator_dir, rule_cfg,
                                                                               monkeypatch):
    rule_cfg(GATED)
    _install(monkeypatch)
    _arm()
    _window(T0 - 3600, T0 + 3600, "h1")
    _sprint("ABC-9")
    event_rules.run(T0 + 1)
    assert _queued_ids() == ["tracker:ABC-9:sprint-assigned"]
    closed = event_rules.run(T0 + 3601)
    assert _queued_ids() == ["tracker:ABC-9:sprint-assigned"] and _pages(closed) == []
    _window(T0 + 7200, T0 + 10800, "h2", ts=T0 + 7000)
    records = event_rules.run(T0 + 7201)
    assert _queued_ids() == []
    assert {"type": "event_queue_dropped", "rule": "sprint", "count": 1, "reason": "window"} in records
    assert _pages(records) == []
    assert _dropped(event_rules.run(T0 + 7202)) == []


def test_a_record_from_an_earlier_window_never_reminds_after_a_stop_and_a_gap(fresh_orchestrator_dir, rule_cfg, tmp_path,
                                                                             monkeypatch):
    stop = tmp_path / "stop"
    rule_cfg(GATED + f'stop_file = "{stop}"\n')
    _install(monkeypatch)
    _arm()
    _window(T0 - 3600, T0 + 3600, "h1")
    _sprint("ABC-9")
    event_rules.run(T0 + 1)
    assert _queued_ids() == ["tracker:ABC-9:sprint-assigned"]
    stop.write_text("")
    _window(T0 + 7200, T0 + 10800, "h2", ts=T0 + 7000)
    stopped = event_rules.run(T0 + 7201)
    assert _queued_ids() == ["tracker:ABC-9:sprint-assigned"] and _pages(stopped) == []
    stop.unlink()
    records = event_rules.run(T0 + 7202)
    assert _queued_ids() == []
    assert {"type": "event_queue_dropped", "rule": "sprint", "count": 1, "reason": "window"} in records
    assert _pages(records) == []


def _late(key, ts, delivered):
    events.append(Event(f"tracker:{key}:sprint-assigned", ts, "tracker", "sprint-assigned", {"issue": key}, "",
                        f"{key} sum", {"delivered": delivered}))
    return f"tracker:{key}:sprint-assigned"


def test_adjacent_windows_of_one_issued_set_count_as_one_window(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    rule_cfg(GATED)
    _install(monkeypatch)
    _arm()
    _window(T0 - 7200, T0 - 1800, "h1")
    _window(T0 - 1800, T0 + 3600, "h1")
    late = _late("LATE-1", T0, T0 - 3000)
    event_rules.run(T0 + 1)
    assert _queued_ids() == [late]


def test_the_first_set_after_a_change_does_not_reopen_the_window_it_replaced(fresh_orchestrator_dir, rule_cfg,
                                                                            monkeypatch):
    day = 86400
    b = T0 + 10
    rule_cfg(GATED)
    _install(monkeypatch)
    _arm()
    _window(b, b + 13 * day, "h1", ts=b)
    _sprint("ABC-9", b + 100)
    event_rules.run(b + 101)
    assert _queued_ids() == ["tracker:ABC-9:sprint-assigned"]
    _window(b + 10 * day, b + 13 * day, "h2", ts=b + 10 * day)
    _late("LATE-1", b + 10 * day + 40, b + 8 * day)
    records = event_rules.run(b + 10 * day + 41)
    assert _queued_ids() == [] and _pages(records) == [] and _dropped(records)[0]["count"] == 2


@pytest.mark.parametrize("case, rule, rc, line", [
    ("open", "sprint", 0, {"ok": True, "open": True, "rule": "sprint", "start": "NOW-3600"}),
    ("no-window", "sprint", 1, {"ok": True, "open": False, "reason": "window", "rule": "sprint"}),
    ("stopped", "sprint", 1, {"ok": True, "open": False, "reason": "stop_file", "rule": "sprint"}),
    ("disabled", "sprint", 1, {"ok": True, "open": False, "reason": "disabled", "rule": "sprint"}),
    ("unknown", "nope", 3, {"ok": False, "reason": "unknown rule nope"}),
])
def test_event_gate_reports_open_inside_the_window_and_the_reason_outside(rule_cfg, tmp_path, capsys, case, rule, rc,
                                                                          line):
    now = float(int(time.time()))
    stop = tmp_path / "stop"
    rule_cfg({"open": GATED, "no-window": GATED, "stopped": QUEUE + f'stop_file = "{stop}"\n',
              "disabled": QUEUE + "enabled = false\n", "unknown": GATED}[case])
    if case == "open":
        _window(now - 3600, now + 3600, "h1", ts=now - 100)
    if case == "stopped":
        stop.write_text("")
    assert events.main(["gate", rule]) == rc
    assert json.loads(capsys.readouterr().out.strip()) == {k: now - 3600 if v == "NOW-3600" else v
                                                            for k, v in line.items()}


def test_event_gate_prints_no_start_for_an_open_rule_without_while(rule_cfg, capsys):
    rule_cfg(QUEUE)
    assert events.main(["gate", "sprint"]) == 0
    assert json.loads(capsys.readouterr().out.strip()) == {"ok": True, "open": True, "rule": "sprint"}


def test_on_change_queues_the_first_event_a_changed_value_and_a_repeat_of_the_named_values_after_repeat_sec(
        fresh_orchestrator_dir, rule_cfg, monkeypatch):
    rule_cfg(STATE)
    _install(monkeypatch)
    _arm()
    _, ok0 = _state("ok", T0)
    event_rules.run(T0 + 1)
    assert _queued_ids() == [ok0.id]
    _state("ok", T0 + 120)
    event_rules.run(T0 + 121)
    assert _queued_ids() == [ok0.id] and _cursor()["last"][0] == T0 + 120
    _, f1 = _state("failed", T0 + 240)
    event_rules.run(T0 + 241)
    assert _queued_ids() == [ok0.id, f1.id]
    _state("failed", T0 + 360)
    event_rules.run(T0 + 361)
    assert _queued_ids() == [ok0.id, f1.id]
    _, f2 = _state("failed", T0 + 3900)
    event_rules.run(T0 + 3901)
    assert _queued_ids() == [ok0.id, f1.id, f2.id]
    _, ok1 = _state("ok", T0 + 4020)
    event_rules.run(T0 + 4021)
    assert _queued_ids() == [ok0.id, f1.id, f2.id, ok1.id]
    _state("ok", T0 + 8000)
    event_rules.run(T0 + 8001)
    assert _queued_ids() == [ok0.id, f1.id, f2.id, ok1.id]
    assert _cursor()["last_value"] == "ok" and _cursor()["last_queued_at"] == T0 + 4021


def test_two_state_lines_of_one_second_and_one_state_collapse_to_one_event(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    rule_cfg(STATE)
    _install(monkeypatch)
    _arm()
    first, e = _state("ok", T0)
    second, _ = _state("ok", T0)
    assert first is True and second is False
    event_rules.run(T0 + 1)
    assert _queued_ids() == [e.id]


def test_on_change_handles_ok_then_failed_of_one_round_in_command_order(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    rule_cfg(STATE)
    _install(monkeypatch)
    _arm()
    _, ok = _state("ok", T0)
    _, failed = _state("failed", T0 + 0.001)
    event_rules.run(T0 + 1)
    assert _queued_ids() == [ok.id, failed.id] and _cursor()["last_value"] == "failed"
    assert event_rules.run(T0 + 2) == [] and _queued_ids() == [ok.id, failed.id]


def test_last_value_survives_an_empty_queue_after_ack_all(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    from dockwright import event_queue
    rule_cfg(STATE)
    _install(monkeypatch)
    _arm()
    _state("failed", T0)
    event_rules.run(T0 + 1)
    event_queue.ack("sprint", "*")
    _state("failed", T0 + 100)
    assert event_rules.run(T0 + 101) == []
    assert _queued_ids() == [] and _cursor()["last_value"] == "failed"


CONFIRMED = STATE + "confirm_events = 2\n"


def test_confirm_events_queues_a_change_only_once_two_events_in_a_row_carry_it(fresh_orchestrator_dir, rule_cfg,
                                                                               monkeypatch):
    rule_cfg(CONFIRMED)
    _install(monkeypatch)
    _arm()

    def step(state, offset):
        _, e = _state(state, T0 + offset)
        event_rules.run(T0 + offset + 1)
        return e

    step("ok", 0)
    assert not (paths.EVENTS_QUEUES / "sprint.json").exists()
    ok0 = step("ok", 300)
    assert _queued_ids() == [ok0.id]
    step("failed", 600)
    assert _queued_ids() == [ok0.id]
    step("ok", 900)
    assert _queued_ids() == [ok0.id] and _cursor()["pending_count"] == 0
    step("failed", 1200)
    assert _queued_ids() == [ok0.id]
    f1 = step("failed", 1500)
    assert _queued_ids() == [ok0.id, f1.id]
    f2 = step("failed", 5200)
    assert _queued_ids() == [ok0.id, f1.id, f2.id]
    step("ok", 5500)
    assert _queued_ids() == [ok0.id, f1.id, f2.id]
    ok1 = step("ok", 5800)
    assert _queued_ids() == [ok0.id, f1.id, f2.id, ok1.id]
    assert _cursor()["last_value"] == "ok"


def test_confirm_events_counts_two_agreeing_lines_of_one_pass(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    rule_cfg(CONFIRMED)
    _install(monkeypatch)
    _arm()
    _state("ok", T0)
    event_rules.run(T0 + 1)
    _, ok = _state("ok", T0 + 300)
    event_rules.run(T0 + 301)
    _state("failed", T0 + 600)
    _, failed = _state("failed", T0 + 601)
    event_rules.run(T0 + 602)
    assert _queued_ids() == [ok.id, failed.id]
    _state("ok", T0 + 900)
    _state("failed", T0 + 901)
    event_rules.run(T0 + 902)
    assert _queued_ids() == [ok.id, failed.id]


def test_a_manager_renamed_between_ticks_gets_the_next_reminder(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    rule_cfg(QUEUE)
    _install(monkeypatch)
    _arm()
    _sprint("ABC-9")
    assert event_rules.run(T0 + 1)[0]["manager_name"] == "proud"
    _install(monkeypatch, managers=[{"claude_sid": "m2", "name": "brave", "domain": "general"}])
    assert event_rules.run(T0 + 601)[0]["manager_name"] == "brave"


def test_an_idle_queue_rule_writes_no_cursor(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    from dockwright import state
    rule_cfg(QUEUE)
    _install(monkeypatch)
    _arm()
    _sprint("ABC-9")
    event_rules.run(T0 + 1)
    writes = []
    real = state.write_json_atomic
    monkeypatch.setattr(state, "write_json_atomic", lambda path, data: writes.append(path) or real(path, data))
    assert event_rules.run(T0 + 2) == []
    assert [p for p in writes if p == event_rules._cursor_path("sprint")] == []
    assert len(event_rules.run(T0 + 601)) == 1
    assert [p for p in writes if p == event_rules._cursor_path("sprint")] == []


# the reply action

from dockwright import chat_archive, event_queue, state

D = "D01ABCDEF"
C = "C01ABCDEF"
G = "G01ABCDEF"
OTHER = "U01OTHER1"
W = "sid-w1"
M = "sid-m1"
T = "1792700005.000100"
TZ = "1792700006.843940"
R = "1792700100.000200"
NOW = T0 + 200
FOOTER = "*Sent using* <@U01APPAPP|SomeApp>"
SELF = "U01ME00000"
LINE = (f"Ada Example replied in Ada Example ({D}, thread {T}) to brave-otter's message of {T}: "
        f"chat_read(conversation=\"{D}\", after=\"{T}\")")


def _reply_rules(tmp_path, team_enabled=True):
    return ('[events]\nenabled = true\n'
            '[events.rules.reply-dm]\nsource = "chat"\nkind = "dm"\naction = "reply"\n'
            '[events.rules.reply-dm-thread]\nsource = "chat"\nkind = "dm-thread"\naction = "reply"\n'
            '[events.rules.reply-channel-thread]\nsource = "chat"\nkind = "channel-thread"\naction = "reply"\n'
            'unless_rules = ["team-thread"]\n'
            '[events.rules.team-thread]\nsource = "chat"\nkind = "channel-thread"\naction = "queue"\n'
            f'match = {{ conversation = "#team-x" }}\nstop_file = "{tmp_path}/team-stop"\nenabled = {str(team_enabled).lower()}\n')


class Wire:
    def __init__(self, deliver):
        self.deliver = deliver
        self.calls = []
        self.reply_policy = object()

    def deliver_to_pane(self, worker, text, marker, policy):
        self.calls.append((worker, text, marker, policy))
        return self.deliver(worker)

    def _session_alive(self, sid):
        return (paths.ACTIVE / f"{sid}.json").exists()


def _active(sid, agent="worker", name="brave-otter", domain=None, parent=None, pid=1):
    paths.ACTIVE.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {
        "claude_sid": sid, "agent": agent, "name": name, "pid": pid, "started_at": 0, "domain": domain,
        "parent_manager_name": parent})


def _wire(monkeypatch, *, deliver=lambda worker: {"status": "delivered", "confirmed": True}, managers=None):
    engine = Wire(deliver)
    monkeypatch.setattr(event_rules, "_engine", lambda: engine)
    monkeypatch.setattr(event_rules, "_live_managers",
                        lambda: [{"claude_sid": M, "name": "proud", "domain": "general"}] if managers is None
                        else managers)
    return engine


def _sent(channel, ts, *, thread="", sid=W, name="brave-otter", agent="worker", domain="general"):
    e = Event(f"sent:{channel}:{ts}", float(ts), "sent", "chat",
              {"channel": channel, "thread": thread, "sid": sid, "name": name, "agent": agent, "domain": domain,
               "tool": "mcp__chat__send_message", "pid": "1"}, ts, f"{name} → {channel}: hi",
              {"to": "U01OTHER1", "link": "https://chat.example/p/1", "text": "hi", "thread_ts": thread})
    events.append(e)
    return e


def _reply(kind, channel, thread, ref_ts, conversation="Ada Example", sender="Ada Example", scout="ok", ts=NOW,
           sender_id=OTHER):
    e = Event(f"chat:msg:{channel}:{ref_ts}", ts, "chat", kind,
              {"app": "com.example.chat", "conversation": conversation, "sender": sender, "channel": channel,
               "thread": thread}, "", f"{sender} · ref archive_{ref_ts}",
              {"ref": f"archive_{ref_ts}", "scout": scout, "sender_id": sender_id, "link": "https://chat.example/p/2"})
    events.append(e)
    return e


def _rcursor(name):
    return json.loads((paths.EVENTS_CURSORS / "rules" / f"{name}.json").read_text())


def _hint(ids, rule="reply-dm-thread", channel=D, thread=T, msg_ts=R):
    return event_rules._hint(rule, ids, channel, thread, msg_ts)


def _inbound_rules(tmp_path):
    head = '[events.rules.reply-dm]\nsource = "chat"\nkind = "dm"\naction = "reply"\n'
    thread = '[events.rules.reply-dm-thread]\nsource = "chat"\nkind = "dm-thread"\naction = "reply"\n'
    extra = 'inbound = true\ninbound_skip_senders = ["U04APP"]\n'
    return _reply_rules(tmp_path).replace(head, head + extra).replace(thread, thread + extra)


def _rec(rule, event_id):
    return next(q for q in event_queue.read(rule) if q.id == event_id).payload["reply"]


def _ask(first_line, qid="q1"):
    d = paths.question_dir_for("proud")
    d.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(d / f"{qid}.json", {"question_id": qid, "question": first_line + "\nwho, link, draft",
                                                 "asked_by": "manager", "domain": "general", "asked_at": NOW})
    return d / f"{qid}.json"


def _pages(records):
    return [r for r in records if r["type"] == "event_queue_page"]


def _old_record(event_id, channel, thread):
    return Event(event_id, NOW - 700, "chat", "dm-thread" if thread else "dm", {"channel": channel, "thread": thread},
                 "", LINE, {"reply": {"channel": channel, "thread": thread, "sent_ts": T, "sender_name": "brave-otter",
                                      "sender_sid": W, "domain": "general", "link": "", "text": LINE}})


@pytest.fixture
def reply_cfg(fresh_orchestrator_dir, tmp_path, monkeypatch):
    cfg = tmp_path / "events.toml"
    cfg.write_text(_reply_rules(tmp_path))
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    event_rules.run(T0 - 1)

    def write(text):
        cfg.write_text(text)

    return write


def test_a_thread_reply_to_a_recorded_send_queues_a_record_and_types_one_line_into_the_live_senders_pane(
        reply_cfg, monkeypatch):
    engine = _wire(monkeypatch)
    _active(W)
    _sent(D, T)
    e = _reply("dm-thread", D, T, R)
    records = event_rules.run(NOW + 1)
    assert engine.calls == [(W, events.EVENT_MARK + "sent.reply — " + LINE + _hint([e.id]), "", engine.reply_policy)]
    assert {"type": "event_reply_wake", "rule": "reply-dm-thread", "event_id": e.id, "worker": "brave-otter"} in records
    assert {"type": "event_reply_queued", "rule": "reply-dm-thread", "event_id": e.id, "inbound": False, "channel": D,
            "thread": T, "msg_ts": R} in records
    assert _rcursor("reply-dm-thread")["last"] == [NOW, e.id]
    r = _rec("reply-dm-thread", e.id)
    assert r["delivered_at"] == NOW + 1 and r["delivered_sid"] == W and r["escalated_at"] is None
    assert r["inbound"] is False and r["msg_ts"] == R and r["sender_id"] == OTHER and r["queued_at"] == NOW + 1
    assert _pages(records) == []


def test_a_thread_reply_matches_a_record_whose_ts_ends_in_zero(reply_cfg, monkeypatch):
    engine = _wire(monkeypatch)
    _active(W)
    _sent(D, TZ)
    _reply("dm-thread", D, TZ, R)
    event_rules.run(NOW + 1)
    assert len(engine.calls) == 1
    text = engine.calls[0][1]
    assert f"message of {TZ}:" in text and f'after="{TZ}"' in text and "84394\"" not in text


@pytest.mark.parametrize("deliver", [
    lambda worker: {"status": "human_attached", "delivered": False},
    lambda worker: {"status": "delivered", "confirmed": False},
    lambda worker: (_ for _ in ()).throw(ValueError("no live window"))])
def test_a_refused_or_unconfirmed_delivery_leaves_the_records_undelivered_and_the_next_pass_retries(
        reply_cfg, monkeypatch, deliver):
    _wire(monkeypatch, deliver=deliver)
    _active(W)
    _sent(D, T)
    e1 = _reply("dm-thread", D, T, R)
    e2 = _reply("dm-thread", D, T, "1792700101.000300", ts=NOW + 1)
    records = event_rules.run(NOW + 2)
    queued = event_queue.read("reply-dm-thread")
    assert [q.id for q in queued] == [e1.id, e2.id]
    assert _rcursor("reply-dm-thread")["last"] == [NOW + 1, e2.id]
    assert queued[0].summary == LINE
    assert queued[0].payload["reply"] == {
        "channel": D, "thread": T, "sent_ts": T, "sender_name": "brave-otter", "sender_sid": W,
        "sender_agent": "worker", "sender_pid": "1", "domain": "general", "link": "https://chat.example/p/1",
        "text": LINE + _hint([e1.id]), "msg_ts": R, "sender_id": OTHER, "inbound": False, "queued_at": NOW + 2,
        "delivered_at": None, "delivered_sid": "", "escalated_at": None}
    assert _pages(records) == [] and not any(r["type"] == "event_reply_wake" for r in records)
    _wire(monkeypatch)
    event_rules.run(NOW + 3)
    assert [_rec("reply-dm-thread", i)["delivered_at"] for i in (e1.id, e2.id)] == [NOW + 3, NOW + 3]


def test_three_replies_in_one_thread_queue_three_records_and_type_one_line_that_names_them_all(reply_cfg, monkeypatch):
    engine = _wire(monkeypatch)
    _active(W)
    _sent(D, T)
    last = "1792700102.000200"
    ids = sorted(_reply("dm-thread", D, T, ref, ts=NOW).id for ref in (R, "1792700101.000200", last))
    records = event_rules.run(NOW + 1)
    assert [c[1] for c in engine.calls] == [events.EVENT_MARK + "sent.reply — " + LINE + _hint(ids, msg_ts=last)]
    assert [r for r in records if r["type"] == "event_reply_wake"] == [
        {"type": "event_reply_wake", "rule": "reply-dm-thread", "event_id": i, "worker": "brave-otter"} for i in ids]
    assert _rcursor("reply-dm-thread")["last"] == [NOW, ids[-1]]
    assert [q.id for q in event_queue.read("reply-dm-thread")] == ids
    assert [_rec("reply-dm-thread", i)["delivered_at"] for i in ids] == [NOW + 1] * 3


def test_replies_in_two_threads_type_two_lines(reply_cfg, monkeypatch):
    engine = _wire(monkeypatch)
    _active(W)
    _sent(D, T)
    _sent(D, TZ)
    _reply("dm-thread", D, T, R, ts=NOW)
    _reply("dm-thread", D, TZ, "1792700101.000200", ts=NOW)
    event_rules.run(NOW + 1)
    assert len(engine.calls) == 2 and len(event_queue.read("reply-dm-thread")) == 2
    assert sorted(f"thread {t})" in c[1] for c in engine.calls for t in (T, TZ)) == [False, False, True, True]


def test_replies_for_two_live_sessions_in_one_conversation_type_one_line_each(reply_cfg, monkeypatch):
    engine = _wire(monkeypatch)
    _active(W)
    _active("sid-w2", name="calm-heron")
    _sent(C, T, thread=T, sid=W)
    _reply("channel-thread", C, T, "1792700050.000100", ts=NOW)
    _sent(C, "1792700060.000100", thread=T, sid="sid-w2", name="calm-heron")
    _reply("channel-thread", C, T, R, ts=NOW)
    event_rules.run(NOW + 1)
    assert sorted(c[0] for c in engine.calls) == sorted([W, "sid-w2"])
    assert len(event_queue.read("reply-channel-thread")) == 2


def test_three_replies_in_one_thread_with_a_refused_delivery_queue_three_records_after_one_attempt(
        reply_cfg, monkeypatch):
    engine = _wire(monkeypatch, deliver=lambda worker: {"status": "human_attached", "delivered": False})
    _active(W)
    _sent(D, T)
    ids = sorted(_reply("dm-thread", D, T, ref, ts=NOW).id
                 for ref in (R, "1792700101.000200", "1792700102.000200"))
    records = event_rules.run(NOW + 1)
    assert [q.id for q in event_queue.read("reply-dm-thread")] == ids and len(engine.calls) == 1
    assert [_rec("reply-dm-thread", i)["delivered_at"] for i in ids] == [None] * 3 and _pages(records) == []


def test_a_reply_in_the_next_pass_types_a_new_line_that_names_only_the_new_record(reply_cfg, monkeypatch):
    engine = _wire(monkeypatch)
    _active(W)
    _sent(D, T)
    _reply("dm-thread", D, T, R, ts=NOW)
    event_rules.run(NOW + 1)
    e2 = _reply("dm-thread", D, T, "1792700101.000200", ts=NOW + 5)
    event_rules.run(NOW + 6)
    assert len(engine.calls) == 2 and len(event_queue.read("reply-dm-thread")) == 2
    assert engine.calls[1][1].endswith(_hint([e2.id], msg_ts="1792700101.000200"))


def test_a_reply_reaches_the_live_worker_of_the_senders_name_after_its_sid_rotated(reply_cfg, monkeypatch):
    engine = _wire(monkeypatch)
    _active("sid-w1-rotated", name="brave-otter")
    _sent(D, T, sid=W)
    e = _reply("dm-thread", D, T, R)
    records = event_rules.run(NOW + 1)
    assert [c[0] for c in engine.calls] == ["sid-w1-rotated"]
    assert any(r["type"] == "event_reply_wake" and r["event_id"] == e.id for r in records)
    assert _rec("reply-dm-thread", e.id)["delivered_sid"] == "sid-w1-rotated"


def test_a_queued_reply_to_a_rotated_sender_carries_the_live_sid(reply_cfg, monkeypatch):
    _wire(monkeypatch, deliver=lambda worker: {"status": "delivered", "confirmed": False})
    _active("sid-w1-rotated", name="brave-otter")
    _sent(D, T, sid=W)
    _reply("dm-thread", D, T, R)
    event_rules.run(NOW + 1)
    queued, = event_queue.read("reply-dm-thread")
    assert queued.payload["reply"]["sender_sid"] == "sid-w1-rotated"


def test_a_new_worker_that_reused_the_senders_name_under_another_pid_is_not_woken(reply_cfg, monkeypatch):
    engine = _wire(monkeypatch)
    _active("sid-new", name="brave-otter", pid=2)
    _sent(D, T, sid=W)
    e = _reply("dm-thread", D, T, R)
    assert _pages(event_rules.run(NOW + 1)) == []
    assert engine.calls == []
    assert [q.id for q in event_queue.read("reply-dm-thread")] == [e.id]
    page, = _pages(event_rules.run(NOW + 602))
    assert page["count"] == 1 and engine.calls == []


def test_a_reply_to_a_dead_senders_message_pages_its_domain_manager_after_ack_sec(reply_cfg, monkeypatch):
    billing = {"claude_sid": "sid-m2", "name": "bill", "domain": "billing"}
    engine = _wire(monkeypatch, managers=[{"claude_sid": M, "name": "proud", "domain": "general"}, billing])
    _sent(D, T, domain="billing")
    e = _reply("dm-thread", D, T, R)
    assert _pages(event_rules.run(NOW + 1)) == []
    assert engine.calls == []
    assert [q.id for q in event_queue.read("reply-dm-thread")] == [e.id]
    assert _rcursor("reply-dm-thread")["last"] == [NOW, e.id]
    page, = _pages(event_rules.run(NOW + 602))
    assert page["manager_name"] == "bill" and page["count"] == 1


def test_a_reply_to_a_managers_own_message_pages_that_manager_after_ack_sec(reply_cfg, monkeypatch):
    engine = _wire(monkeypatch)
    _active(M, agent="manager", name="proud", domain="general")
    _sent(D, T, sid=M, name="proud", agent="manager")
    e = _reply("dm-thread", D, T, R)
    assert _pages(event_rules.run(NOW + 1)) == []
    assert engine.calls == []
    assert [q.id for q in event_queue.read("reply-dm-thread")] == [e.id]
    page, = _pages(event_rules.run(NOW + 602))
    assert page["manager_name"] == "proud" and "proud's message" in page["line"]
    assert _rec("reply-dm-thread", e.id + ":esc")["sender_agent"] == "manager" and engine.calls == []


def test_a_ripe_reply_with_no_domain_manager_stalls_once_an_hour_and_stays_queued(reply_cfg, monkeypatch):
    _wire(monkeypatch, managers=[])
    _sent(D, T)
    e = _reply("dm-thread", D, T, R)
    assert event_rules.run(NOW + 1) == [
        {"type": "event_reply_queued", "rule": "reply-dm-thread", "event_id": e.id, "inbound": False, "channel": D,
         "thread": T, "msg_ts": R}]
    assert _rcursor("reply-dm-thread")["last"] == [NOW, e.id]
    records = event_rules.run(NOW + 602)
    assert [r for r in records if r["type"] == "event_stalled"] == [
        {"type": "event_stalled", "rule": "reply-dm-thread", "domain": "general", "since": event_rules._iso(NOW + 602)}]
    assert not any(r["type"] == "event_stalled" for r in event_rules.run(NOW + 700))
    assert any(r["type"] == "event_stalled" for r in event_rules.run(NOW + 4203))
    assert [q.id for q in event_queue.read("reply-dm-thread")] == [e.id + ":esc"]


def test_two_domains_in_the_queue_page_each_manager_and_a_missing_one_stalls(reply_cfg, monkeypatch):
    _wire(monkeypatch)
    _sent(D, T)
    _sent("D01OTHER", T, domain="billing", name="other-worker")
    e1 = _reply("dm-thread", D, T, R)
    e2 = _reply("dm-thread", "D01OTHER", T, "1792700101.000300", ts=NOW + 1)
    assert _pages(event_rules.run(NOW + 2)) == []
    records = event_rules.run(NOW + 602)
    assert [(p["manager_name"], p["count"], p["event_id"]) for p in _pages(records)] == [("proud", 1, e1.id + ":esc")]
    assert [r["domain"] for r in records if r["type"] == "event_stalled"] == ["billing"]
    assert [q.id for q in event_queue.read("reply-dm-thread")] == [e1.id + ":esc", e2.id + ":esc"]
    billing = {"claude_sid": "sid-m2", "name": "bill", "domain": "billing"}
    _wire(monkeypatch, managers=[{"claude_sid": M, "name": "proud", "domain": "general"}, billing])
    records = event_rules.run(NOW + 3700)
    assert sorted((p["manager_name"], p["count"]) for p in _pages(records)) == [("bill", 1), ("proud", 1)]
    assert not any(r["type"] == "event_stalled" for r in records)
    assert _rcursor("reply-dm-thread")["stalled_since"] is None


def _archive(messages):
    con = chat_archive.open_db()
    for cid, ts, sender, text, thread in messages:
        con.execute("insert into messages (conversation_id, ts, thread_ts, sender_id, sender, bot, text, link, source, "
                    "captured_at, edited_at, raw_ref) values (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, ts, thread, sender, "x", 0, text, "", "sweep", NOW, None, ""))
    con.commit()
    con.close()


def _with_footer(reply_cfg, tmp_path, rules=_reply_rules):
    reply_cfg(rules(tmp_path) + f'[chat_archive]\nsent_footer = "{FOOTER}"\nself_user_id = "{SELF}"\n')


def _taken_over_dm(reply_cfg, monkeypatch, tmp_path, rules):
    _with_footer(reply_cfg, tmp_path, rules=rules)
    engine = _wire(monkeypatch)
    _active(W)
    _sent(D, T)
    _archive([(D, T, SELF, "hi " + FOOTER, None), (D, "1792700050.000000", SELF, "I'll take it", None),
              (D, R, "U01OTHER1", "thanks", None)])
    return engine, _reply("dm", D, "", R)


def test_a_dm_reply_after_his_own_unsigned_message_wakes_nobody(reply_cfg, monkeypatch, tmp_path):
    engine, e = _taken_over_dm(reply_cfg, monkeypatch, tmp_path, _reply_rules)
    records = event_rules.run(NOW + 1)
    assert engine.calls == [] and event_queue.read("reply-dm") == []
    assert not any(r["type"] in ("event_reply_wake", "event_queue_page") for r in records)
    assert _rcursor("reply-dm")["last"] == [NOW, e.id]


def test_a_dm_reply_after_his_own_unsigned_message_is_an_inbound_record_under_an_inbound_rule(
        reply_cfg, monkeypatch, tmp_path):
    engine, e = _taken_over_dm(reply_cfg, monkeypatch, tmp_path, _inbound_rules)
    event_rules.run(NOW + 1)
    assert engine.calls == [] and _rec("reply-dm", e.id)["inbound"] is True


def test_a_dm_reply_after_our_signed_message_still_wakes(reply_cfg, monkeypatch, tmp_path):
    _with_footer(reply_cfg, tmp_path)
    engine = _wire(monkeypatch)
    _active(W)
    _sent(D, T)
    _archive([(D, T, SELF, "hi " + FOOTER, None), (D, "1792700050.000000", SELF, "again " + FOOTER, None),
              (D, R, "U01OTHER1", "thanks", None)])
    _reply("dm", D, "", R)
    event_rules.run(NOW + 1)
    assert len(engine.calls) == 1


def test_a_listed_rule_takes_the_event_only_while_it_is_enabled_and_not_stopped(reply_cfg, monkeypatch, tmp_path):
    engine = _wire(monkeypatch)
    _active(W)
    _sent(C, T)
    e = _reply("channel-thread", C, T, R, conversation="#team-x", sender="Bob Ray")
    event_rules.run(NOW + 1)
    assert engine.calls == [] and _rcursor("reply-channel-thread")["last"] == [NOW, e.id]
    reply_cfg(_reply_rules(tmp_path, team_enabled=False))
    e2 = _reply("channel-thread", C, T, "1792700101.000300", conversation="#team-x", sender="Bob Ray", ts=NOW + 1)
    event_rules.run(NOW + 2)
    assert len(engine.calls) == 1 and _rcursor("reply-channel-thread")["last"] == [NOW + 1, e2.id]
    reply_cfg(_reply_rules(tmp_path))
    (tmp_path / "team-stop").write_text("")
    _reply("channel-thread", C, T, "1792700102.000300", conversation="#team-x", sender="Bob Ray", ts=NOW + 2)
    event_rules.run(NOW + 3)
    assert len(engine.calls) == 2


def test_a_listed_rule_outside_its_window_leaves_the_event_to_the_reply_rule(reply_cfg, monkeypatch, tmp_path):
    reply_cfg(_reply_rules(tmp_path) + 'while = { kind = "shift" }\n')
    engine = _wire(monkeypatch)
    _active(W)
    _sent(C, T)
    _reply("channel-thread", C, T, R, conversation="#team-x", sender="Bob Ray")
    event_rules.run(NOW + 1)
    assert len(engine.calls) == 1
    _window(NOW - 3600, NOW + 3600, "h1", ts=NOW)
    _reply("channel-thread", C, T, "1792700101.000300", conversation="#team-x", sender="Bob Ray", ts=NOW + 1)
    event_rules.run(NOW + 2)
    assert len(engine.calls) == 1


def test_a_listed_rule_leaves_a_message_sent_before_its_window_to_the_reply_rule(reply_cfg, monkeypatch, tmp_path):
    reply_cfg(_reply_rules(tmp_path) + 'while = { kind = "shift" }\n')
    engine = _wire(monkeypatch)
    _active(W)
    _sent(C, T)
    _window(NOW - 60, NOW + 3600, "h1", ts=NOW)
    ref = "1792700101.000300"
    events.append(Event(f"chat:msg:{C}:{ref}", NOW + 1, "chat", "channel-thread",
                        {"app": "com.example.chat", "conversation": "#team-x", "sender": "Bob Ray", "channel": C,
                         "thread": T}, "", f"Bob Ray · ref archive_{ref}",
                        {"ref": f"archive_{ref}", "scout": "ok", "sender_id": OTHER, "delivered": float(ref),
                         "link": "https://chat.example/p/2"}))
    event_rules.run(NOW + 2)
    assert len(engine.calls) == 1


def test_a_reply_event_without_a_channel_is_skipped(reply_cfg, monkeypatch):
    engine = _wire(monkeypatch)
    _active(W)
    _sent(D, T)
    e = _reply("dm", "", "", R, scout="off")
    records = event_rules.run(NOW + 1)
    assert engine.calls == [] and event_queue.read("reply-dm") == []
    assert _rcursor("reply-dm")["last"] == [NOW, e.id] and records == []


def test_a_reply_without_a_record_but_with_our_signed_message_queues_for_general(reply_cfg, monkeypatch, tmp_path):
    _with_footer(reply_cfg, tmp_path)
    engine = _wire(monkeypatch)
    _archive([(D, T, SELF, "hi " + FOOTER, None), (D, R, "U01OTHER1", "thanks", None)])
    e = _reply("dm", D, "", R)
    assert _pages(event_rules.run(NOW + 1)) == []
    assert engine.calls == []
    queued, = event_queue.read("reply-dm")
    assert queued.id == e.id and queued.payload["reply"]["sender_name"] == "a session (footer match)"
    assert queued.payload["reply"]["sent_ts"] == T and queued.payload["reply"]["sender_sid"] == ""
    assert queued.payload["reply"]["domain"] == "general"
    assert queued.payload["reply"]["link"] == "https://chat.example/p/2"
    assert "a session (footer match)'s message of " + T in queued.summary
    page, = _pages(event_rules.run(NOW + 602))
    assert page["manager_name"] == "proud"


def test_a_reply_without_a_record_or_a_signed_message_is_skipped(reply_cfg, monkeypatch, tmp_path):
    _with_footer(reply_cfg, tmp_path)
    engine = _wire(monkeypatch)
    _archive([(D, T, SELF, "hi, no footer", None), (D, R, "U01OTHER1", "thanks", None)])
    e = _reply("dm", D, "", R)
    records = event_rules.run(NOW + 1)
    assert engine.calls == [] and event_queue.read("reply-dm") == [] and records == []
    assert _rcursor("reply-dm")["last"] == [NOW, e.id]


def test_a_dm_reply_matches_only_a_top_level_record_and_a_thread_reply_only_its_thread(reply_cfg, monkeypatch):
    engine = _wire(monkeypatch)
    _active(W)
    _sent(D, T, thread="1792700000.000100")
    _reply("dm", D, "", R)
    event_rules.run(NOW + 1)
    assert engine.calls == []
    _reply("dm-thread", D, "1792700000.000100", "1792700101.000300", ts=NOW + 1)
    event_rules.run(NOW + 2)
    assert len(engine.calls) == 1 and "thread 1792700000.000100) to brave-otter's message of " + T in engine.calls[0][1]
    _sent(D, "1792700090.000000", name="late-otter")
    _sent(D, "1792700120.000000", name="future-otter")
    _reply("dm", D, "", "1792700110.000000", ts=NOW + 2)
    event_rules.run(NOW + 3)
    assert len(engine.calls) == 2 and "to late-otter's message of 1792700090.000000" in engine.calls[1][1]


def test_the_reply_line_without_a_thread_names_only_the_conversation(reply_cfg, monkeypatch):
    engine = _wire(monkeypatch)
    _active(W)
    _sent(D, T)
    _reply("dm", D, "", R)
    event_rules.run(NOW + 1)
    assert f"replied in Ada Example ({D}) to brave-otter's message of {T}" in engine.calls[0][1]
    assert ", thread" not in engine.calls[0][1]


def test_a_worker_ack_before_ack_sec_closes_the_record_and_pages_nobody(reply_cfg, monkeypatch):
    engine = _wire(monkeypatch)
    _active(W)
    _sent(D, T)
    e = _reply("dm-thread", D, T, R)
    event_rules.run(NOW + 1)
    assert event_queue.ack("reply-dm-thread", e.id) == (1, 0)
    records = event_rules.run(NOW + 700)
    assert event_queue.read("reply-dm-thread") == [] and len(engine.calls) == 1 and _pages(records) == []


def test_escalation_rekeys_the_record_and_a_late_ack_of_the_old_id_removes_nothing(reply_cfg, monkeypatch):
    engine = _wire(monkeypatch)
    _active(W)
    _sent(D, T)
    e = _reply("dm-thread", D, T, R)
    event_rules.run(NOW + 1)
    assert _pages(event_rules.run(NOW + 300)) == []
    records = event_rules.run(NOW + 602)
    esc = e.id + ":esc"
    assert {"type": "event_reply_escalated", "rule": "reply-dm-thread", "event_id": e.id, "new_event_id": esc} in records
    assert _pages(records) == [{"type": "event_queue_page", "rule": "reply-dm-thread", "count": 1, "line": LINE,
                                "event_id": esc, "manager_name": "proud"}]
    assert _rec("reply-dm-thread", esc)["escalated_at"] == NOW + 602
    assert event_queue.ack("reply-dm-thread", e.id) == (0, 1)
    assert _pages(event_rules.run(NOW + 700)) == [] and len(_pages(event_rules.run(NOW + 1210))) == 1
    assert len(engine.calls) == 1


def test_an_undelivered_reply_is_retried_each_pass_until_it_escalates(reply_cfg, monkeypatch):
    engine = _wire(monkeypatch, deliver=lambda worker: {"status": "delivered", "confirmed": False})
    _active(W)
    _sent(D, T)
    _reply("dm-thread", D, T, R)
    event_rules.run(NOW + 1)
    event_rules.run(NOW + 300)
    assert len(engine.calls) == 2
    assert len(_pages(event_rules.run(NOW + 602))) == 1 and len(engine.calls) == 3
    event_rules.run(NOW + 700)
    assert len(engine.calls) == 3


def test_the_ack_clock_starts_at_the_delivery_not_at_the_queueing(reply_cfg, monkeypatch):
    _wire(monkeypatch, deliver=lambda worker: {"status": "delivered", "confirmed": False})
    _active(W)
    _sent(D, T)
    e = _reply("dm-thread", D, T, R)
    event_rules.run(NOW + 1)
    _wire(monkeypatch)
    event_rules.run(NOW + 300)
    assert _rec("reply-dm-thread", e.id)["delivered_at"] == NOW + 300
    assert _pages(event_rules.run(NOW + 602)) == []
    assert len(_pages(event_rules.run(NOW + 901))) == 1


def test_a_record_named_by_an_open_engineer_ask_pages_nobody_and_a_sibling_record_still_pages(reply_cfg, monkeypatch):
    _wire(monkeypatch)
    _sent(D, T, sid="sid-dead")
    e1 = _reply("dm-thread", D, T, R)
    q = _ask(f"dm-reply: reply-dm-thread {e1.id} {D} {T} {R}")
    event_rules.run(NOW + 1)
    assert _pages(event_rules.run(NOW + 602)) == [] and event_queue.read("reply-dm-thread")[0].id == e1.id + ":esc"
    assert _pages(event_rules.run(NOW + 1300)) == []
    R2 = "1792700150.000300"
    e2 = _reply("dm-thread", D, T, R2, ts=NOW + 1301)
    event_rules.run(NOW + 1302)
    page, = _pages(event_rules.run(NOW + 1903))
    assert page["count"] == 1 and page["event_id"] == e2.id + ":esc"
    q.unlink()
    assert _pages(event_rules.run(NOW + 2600))[0]["count"] == 2


def test_an_ask_that_names_another_rule_or_comes_from_a_worker_does_not_silence_the_record(reply_cfg, monkeypatch):
    _wire(monkeypatch)
    _sent(D, T, sid="sid-dead")
    e = _reply("dm-thread", D, T, R)
    _ask(f"dm-reply: reply-dm {e.id} {D} {T} {R}", qid="q1")
    worker_ask = _ask(f"dm-reply: reply-dm-thread {e.id} {D} {T} {R}", qid="q2")
    state.write_json_atomic(worker_ask, {**json.loads(worker_ask.read_text()), "asked_by": None})
    event_rules.run(NOW + 1)
    assert len(_pages(event_rules.run(NOW + 602))) == 1


def test_an_ask_opened_before_escalation_keeps_the_record_from_paging(reply_cfg, monkeypatch):
    _wire(monkeypatch)
    _sent(D, T, sid="sid-dead")
    e = _reply("dm-thread", D, T, R)
    event_rules.run(NOW + 1)
    _ask(f"dm-reply: reply-dm-thread {e.id} {D} {T} {R}")
    assert _pages(event_rules.run(NOW + 602)) == []
    assert _rec("reply-dm-thread", e.id + ":esc")["escalated_at"] == NOW + 602


def test_a_record_queued_by_an_older_daemon_ripens_at_once(reply_cfg, monkeypatch):
    _wire(monkeypatch)
    old = _old_record("chat:kold:scout", D, T)
    event_queue.push("reply-dm-thread", [old])
    records = event_rules.run(NOW + 1)
    assert _pages(records)[0]["count"] == 1 and _rec("reply-dm-thread", old.id + ":esc")["escalated_at"] == NOW + 1


def test_a_page_repeats_after_nudge_sec_and_no_pane_line_follows_escalation(reply_cfg, monkeypatch):
    engine = _wire(monkeypatch)
    _active(W)
    _sent(D, T)
    _reply("dm-thread", D, T, R)
    event_rules.run(NOW + 1)
    assert len(_pages(event_rules.run(NOW + 602))) == 1
    assert _pages(event_rules.run(NOW + 900)) == []
    assert len(_pages(event_rules.run(NOW + 1203))) == 1
    assert len(engine.calls) == 1


def test_a_record_without_msg_ts_skips_the_answered_check(reply_cfg, monkeypatch, tmp_path):
    _with_footer(reply_cfg, tmp_path)
    _wire(monkeypatch)
    _archive([(D, "1792700900.000000", SELF, "yes", None)])
    old = _old_record("chat:kold:scout", D, "")
    event_queue.push("reply-dm", [old])
    records = event_rules.run(NOW + 1)
    assert not any(r["type"] in ("event_reply_dropped", "event_rule_failed") for r in records)
    assert [q.id for q in event_queue.read("reply-dm")] == [old.id + ":esc"]


def test_a_colleagues_dm_with_no_message_of_ours_is_queued_inbound_and_pages_after_ack_sec(reply_cfg, monkeypatch, tmp_path):
    _with_footer(reply_cfg, tmp_path, rules=_inbound_rules)
    engine = _wire(monkeypatch)
    _active(W)
    e = _reply("dm", D, "", R)
    records = event_rules.run(NOW + 1)
    line = event_rules._inbound_line("Ada Example", "Ada Example", D, "", R)
    assert engine.calls == [] and _pages(records) == []
    assert {"type": "event_reply_queued", "rule": "reply-dm", "event_id": e.id, "inbound": True, "channel": D,
            "thread": "", "msg_ts": R} in records
    queued, = event_queue.read("reply-dm")
    assert queued.summary == line and queued.payload["reply"] == {
        "channel": D, "thread": "", "sent_ts": "", "sender_name": "", "sender_sid": "", "sender_agent": "",
        "sender_pid": "", "domain": "general", "link": "https://chat.example/p/2", "text": line, "msg_ts": R,
        "sender_id": OTHER, "inbound": True, "queued_at": NOW + 1, "delivered_at": None, "delivered_sid": "",
        "escalated_at": None}
    page, = _pages(event_rules.run(NOW + 602))
    assert page["line"] == line and page["event_id"] == e.id + ":esc"
    assert engine.calls == []


def test_his_answer_before_ack_sec_drops_the_record_when_it_ripens(reply_cfg, monkeypatch, tmp_path):
    _with_footer(reply_cfg, tmp_path, rules=_inbound_rules)
    _wire(monkeypatch)
    _archive([(D, R, OTHER, "is it safe?", None), (D, "1792700150.000000", SELF, "yes", None)])
    e = _reply("dm", D, "", R)
    event_rules.run(NOW + 1)
    records = event_rules.run(NOW + 602)
    assert event_queue.read("reply-dm") == [] and _pages(records) == []
    assert {"type": "event_reply_dropped", "rule": "reply-dm", "event_id": e.id, "reason": "answered"} in records


def test_his_answer_after_escalation_drops_the_record_on_the_next_pass(reply_cfg, monkeypatch, tmp_path):
    _with_footer(reply_cfg, tmp_path, rules=_inbound_rules)
    _wire(monkeypatch)
    _archive([(D, R, OTHER, "is it safe?", None)])
    e = _reply("dm", D, "", R)
    event_rules.run(NOW + 1)
    assert len(_pages(event_rules.run(NOW + 602))) == 1
    _archive([(D, "1792700900.000000", SELF, "yes", None)])
    records = event_rules.run(NOW + 700)
    assert event_queue.read("reply-dm") == []
    assert {"type": "event_reply_dropped", "rule": "reply-dm", "event_id": e.id + ":esc", "reason": "answered"} in records


@pytest.mark.parametrize("channel", [C, G])
def test_an_inbound_message_in_a_group_or_channel_is_skipped(reply_cfg, monkeypatch, tmp_path, channel):
    _with_footer(reply_cfg, tmp_path, rules=_inbound_rules)
    engine = _wire(monkeypatch)
    e = _reply("dm", channel, "", R)
    records = event_rules.run(NOW + 1)
    assert engine.calls == [] and event_queue.read("reply-dm") == [] and records == []
    assert _rcursor("reply-dm")["last"] == [NOW, e.id]


def test_a_second_message_after_his_answer_stays_a_record(reply_cfg, monkeypatch, tmp_path):
    _with_footer(reply_cfg, tmp_path, rules=_inbound_rules)
    _wire(monkeypatch)
    R2 = "1792700200.000000"
    _archive([(D, R, OTHER, "is it safe?", None), (D, "1792700150.000000", SELF, "yes", None),
              (D, R2, OTHER, "and the flag?", None)])
    e1 = _reply("dm", D, "", R)
    e2 = _reply("dm", D, "", R2)
    event_rules.run(NOW + 1)
    records = event_rules.run(NOW + 602)
    assert {"type": "event_reply_dropped", "rule": "reply-dm", "event_id": e1.id, "reason": "answered"} in records
    page, = _pages(records)
    assert page["count"] == 1 and page["event_id"] == e2.id + ":esc"
    assert [q.id for q in event_queue.read("reply-dm")] == [e2.id + ":esc"]


def test_a_listed_sender_is_dropped_as_skip(reply_cfg, monkeypatch, tmp_path):
    _with_footer(reply_cfg, tmp_path, rules=_inbound_rules)
    _wire(monkeypatch)
    e = _reply("dm", D, "", R, sender_id="U04APP")
    event_rules.run(NOW + 1)
    records = event_rules.run(NOW + 602)
    assert {"type": "event_reply_dropped", "rule": "reply-dm", "event_id": e.id, "reason": "skip"} in records
    assert event_queue.read("reply-dm") == [] and _pages(records) == []


def test_his_answer_does_not_close_a_record_its_live_worker_has_not_received(reply_cfg, monkeypatch, tmp_path):
    _with_footer(reply_cfg, tmp_path, rules=_inbound_rules)
    _wire(monkeypatch, deliver=lambda worker: {"status": "delivered", "confirmed": False})
    _active(W)
    _sent(D, T)
    _archive([(D, T, SELF, "hi " + FOOTER, None), (D, R, OTHER, "is it safe?", None),
              (D, "1792700150.000000", SELF, "yes", None)])
    e = _reply("dm", D, "", R)
    event_rules.run(NOW + 1)
    records = event_rules.run(NOW + 602)
    assert not any(r["type"] == "event_reply_dropped" for r in records) and len(_pages(records)) == 1
    assert not any(r["type"] == "event_reply_dropped" for r in event_rules.run(NOW + 700))
    assert [q.id for q in event_queue.read("reply-dm")] == [e.id + ":esc"]


def test_his_answer_does_not_close_a_thread_record(reply_cfg, monkeypatch, tmp_path):
    _with_footer(reply_cfg, tmp_path, rules=_inbound_rules)
    _wire(monkeypatch)
    _sent(D, T, sid="sid-dead")
    _archive([(D, R, OTHER, "is it safe?", T), (D, "1792700150.000000", SELF, "yes", T)])
    e = _reply("dm-thread", D, T, R)
    event_rules.run(NOW + 1)
    records = event_rules.run(NOW + 602)
    assert not any(r["type"] == "event_reply_dropped" for r in records)
    assert [q.id for q in event_queue.read("reply-dm-thread")] == [e.id + ":esc"]


def test_a_colleagues_message_in_a_dm_thread_with_no_message_of_ours_is_queued_inbound(reply_cfg, monkeypatch, tmp_path):
    _with_footer(reply_cfg, tmp_path, rules=_inbound_rules)
    _wire(monkeypatch)
    e = _reply("dm-thread", D, T, R)
    event_rules.run(NOW + 1)
    r = _rec("reply-dm-thread", e.id)
    assert r["inbound"] is True and r["thread"] == T
    assert r["text"] == event_rules._inbound_line("Ada Example", "Ada Example", D, T, R)


def test_an_event_a_listed_rule_takes_is_not_queued_inbound(reply_cfg, monkeypatch, tmp_path):
    rules = _reply_rules(tmp_path).replace(
        '[events.rules.reply-dm]\nsource = "chat"\nkind = "dm"\naction = "reply"\n',
        '[events.rules.reply-dm]\nsource = "chat"\nkind = "dm"\naction = "reply"\ninbound = true\n'
        'unless_rules = ["own-dm"]\n'
        '[events.rules.own-dm]\nsource = "chat"\nkind = "dm"\naction = "queue"\n')
    reply_cfg(rules)
    _wire(monkeypatch)
    _reply("dm", D, "", R)
    event_rules.run(NOW + 1)
    assert event_queue.read("reply-dm") == []


def test_a_forked_work_whose_rows_the_manager_holds_frees_its_slot(fresh_orchestrator_dir, monkeypatch, tmp_path):
    from dockwright import mcp_server
    from tests.test_mcp_tools import _seed_snapshot
    monkeypatch.setattr(mcp_server, "_process_index", lambda: {})
    _seed_snapshot("K-4", tmp_path)
    log = paths.artifact_events_path("K-4")
    state.append_event(log, {"type": "token_forked", "token_id": "t1", "node": "task",
                             "rows": [{"row": "a", "stage": 1, "token_id": "t1/a"},
                                      {"row": "b", "stage": 1, "token_id": "t1/b"}]})
    for row in ("a", "b"):
        state.append_event(log, {"type": "advance_requested", "token_id": f"t1/{row}", "request_id": f"r{row}",
                                 "edge": {"from": "design", "to": "task"}, "index_to": 1, "kind": "fork", "row": row})
        state.append_event(log, {"type": "advance_committed", "token_id": f"t1/{row}", "request_id": f"r{row}",
                                 "node": "task", "index": 1, "holder": f"a-row-{row}"})
    assert event_rules._slots_held(mcp_server, ["K-4"], time.time()) == 1
    for row, count in (("a", 1), ("b", 0)):
        state.append_event(log, {"type": "token_held", "token_id": f"t1/{row}", "node": "task",
                                 "reason": "blocked on a vendor", "actor_sid": "m1"})
        assert event_rules._slots_held(mcp_server, ["K-4"], time.time()) == count
    state.append_event(log, {"type": "advance_requested", "token_id": "t1/c", "request_id": "rc",
                             "edge": {"from": "design", "to": "task"}, "index_to": 1, "kind": "fork", "row": "c"})
    assert event_rules._slots_held(mcp_server, ["K-4"], time.time()) == 1


def test_a_forked_work_holds_its_slot_while_any_row_works_or_a_question_waits(fresh_orchestrator_dir, rule_cfg,
                                                                              monkeypatch):
    engine = _install(monkeypatch)
    _arm()
    _three()
    event_rules.run(T0 + 10)
    engine.values["ABC-1"] = {"t1/a": "human:gate", "t1/b": "worker:task"}
    assert event_rules.run(T0 + 20) == []
    engine.values["ABC-1"] = {"t1/a": "human:gate", "t1/b": "external:merge"}
    engine.questions["ABC-1"] = [{"question_id": "q1"}]
    assert event_rules.run(T0 + 30) == []
    engine.questions.clear()
    assert [r["task_key"] for r in event_rules.run(T0 + 40)] == ["ABC-3"]


def test_a_work_whose_state_does_not_read_holds_its_slot_and_counts_as_open(fresh_orchestrator_dir, monkeypatch,
                                                                           tmp_path):
    from dockwright import mcp_server
    from tests.test_mcp_tools import _seed_snapshot
    monkeypatch.setattr(mcp_server, "_process_index", lambda: {})
    _seed_snapshot("K-3", tmp_path)
    (paths.artifact_ticket_dir("K-3") / "schema.toml").write_text("not [toml")
    _seed_snapshot("K-2", tmp_path)
    read_events = mcp_server._read_events

    def denied(path):
        if path == paths.artifact_events_path("K-2"):
            raise PermissionError(13, "Permission denied", str(path))
        return read_events(path)

    monkeypatch.setattr(mcp_server, "_read_events", denied)
    for key in ("K-3", "K-2"):
        assert event_rules._slots_held(mcp_server, [key], time.time()) == 1
        assert event_rules._open_works(mcp_server, {key: 0}) == [key]
    paths.artifact_ticket_dir("K-1").mkdir(parents=True)
    assert event_rules._slots_held(mcp_server, ["K-1"], time.time()) == 0
    assert event_rules._open_works(mcp_server, {"K-1": 0}) == []


def test_any_error_while_a_works_state_is_read_holds_its_slot(fresh_orchestrator_dir, monkeypatch, tmp_path):
    from dockwright import mcp_server
    from tests.test_mcp_tools import _seed_snapshot
    monkeypatch.setattr(mcp_server, "_process_index", lambda: {})
    _seed_snapshot("K-5", tmp_path)
    state.write_json_atomic(paths.SLOTS / "mvn.json", {"waiters": ["junk"]})
    assert event_rules._slots_held(mcp_server, ["K-5"], time.time()) == 1
    (paths.SLOTS / "mvn.json").unlink()
    _seed_snapshot("K-4", tmp_path)
    log = paths.artifact_events_path("K-4")
    state.append_event(log, {"type": "advance_requested", "request_id": "r1", "edge": {"from": "design", "to": "task"},
                             "index_to": 1})
    state.append_event(log, {"type": "advance_committed", "request_id": "r1", "node": "task", "index": 1,
                             "holder": "a-task", "ts": time.time()})
    assert event_rules._slots_held(mcp_server, ["K-4"], time.time()) == 1


def test_a_rule_that_raises_leaves_its_records_and_the_other_rules_run(fresh_orchestrator_dir, rule_cfg, monkeypatch):
    rule_cfg(RULE + '[events.rules.other]\nsource = "tracker"\nkind = "comment"\naction = "page"\n')
    assert config.events_rules()["other"]["errors"] == []

    def run_rule(name, rule, now, records):
        records.append({"type": "x", "rule": name})
        if name == "sprint":
            raise RuntimeError("boom")
        return records

    monkeypatch.setattr(event_rules, "_run_rule", run_rule)
    assert event_rules.run(T0) == [{"type": "x", "rule": "sprint"},
                                   {"type": "event_rule_failed", "rule": "sprint", "error": "RuntimeError: boom"},
                                   {"type": "x", "rule": "other"}]
