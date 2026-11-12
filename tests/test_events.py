import asyncio
import json
import os
import stat
import time

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from dockwright import config, events, paths
from dockwright import mcp_server as ms
from dockwright.events import Event
from tests.test_mcp_tools import fresh_orchestrator_dir  # noqa: F401
from tests.test_pipeline_reconcile import _started_at_design, _register_worker

T0 = 1792700000.0


@pytest.fixture
def events_cfg(tmp_path, monkeypatch):
    handlers = tmp_path / "h"
    handlers.mkdir()
    cfg = tmp_path / "events.toml"
    env_file = tmp_path / "events.env"
    env_file.write_text("TRACKER_URL=https://j\nTRACKER_EMAIL=me@x\nTRACKER_API_TOKEN=tok\n")
    env_file.chmod(0o600)
    cfg.write_text(f'[events]\nenabled = true\nhandler_path = ["{handlers}"]\nhandlers = ["author-wait"]\n'
                   f'env_file = "{env_file}"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    return cfg


def _work(monkeypatch, tmp_path):
    return _started_at_design(monkeypatch, tmp_path)


def _event(id="tracker:K:comment:1", ts=T0, source="tracker", kind="comment", match=None, cursor_key="1",
           summary="K #1 by A: hi", payload=None):
    return Event(id, ts, source, kind, match if match is not None else {"issue": "K"}, cursor_key, summary,
                 payload or {})


def _journal(task_key="K", kind=None):
    out = ms._read_events(paths.artifact_events_path(task_key))
    return [e for e in out if kind is None or e["type"] == kind]


def _handler(tmp_path, name="author-wait", body="#!/bin/sh\nexit 0\n"):
    p = tmp_path / "h" / name
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def test_append_writes_one_jsonl_line_per_day_and_dedups_by_id(fresh_orchestrator_dir, events_cfg):
    e = _event()
    assert events.append(e) is True and events.append(e) is False
    assert [x.id for x in events.read_since("tracker", 0)] == ["tracker:K:comment:1"]
    assert (paths.EVENTS_LOG / "2026-10-22.jsonl").exists()
    assert events.read_since("tracker", 0)[0] == e


def test_recent_reads_the_last_fourteen_days_only(fresh_orchestrator_dir, events_cfg):
    now = T0
    events.append(_event(id="old", ts=now - 15 * 86400))
    events.append(_event(id="fresh", ts=now - 13 * 86400))
    events.append(_event(id="deploy", ts=now, source="deploy", kind="job"))
    assert [e.id for e in events.recent("tracker", now)] == ["fresh"]


def test_prune_deletes_day_files_older_than_retention_and_keeps_the_boundary_day(fresh_orchestrator_dir, events_cfg):
    now = T0
    events.append(_event(id="gone", ts=now - 20 * 86400))
    events.append(_event(id="edge", ts=now - 14 * 86400))
    events.append(_event(id="today", ts=now))
    assert len(list(paths.EVENTS_LOG.glob("*.jsonl"))) == 3
    assert events.prune(now) == 1
    assert sorted(p.stem for p in paths.EVENTS_LOG.glob("*.jsonl")) == ["2026-10-08", "2026-10-22"]
    assert events.prune(now) == 0


def test_retention_days_from_config_sets_the_read_window_and_the_prune_cut(fresh_orchestrator_dir, events_cfg):
    events_cfg.write_text(events_cfg.read_text().replace("[events]\n", "[events]\nretention_days = 3\n"))
    now = T0
    events.append(_event(id="old", ts=now - 4 * 86400))
    events.append(_event(id="fresh", ts=now - 2 * 86400))
    assert [e.id for e in events.recent("tracker", now)] == ["fresh"]
    assert events.prune(now) == 1
    assert [e.id for e in events.read_since("tracker", 0)] == ["fresh"]


def test_a_sent_event_round_trips_through_append_and_recent(fresh_orchestrator_dir, events_cfg):
    e = Event("sent:D01ABCDEF:1792700001.500000", 1792700001.5, "sent", "chat",
              {"channel": "D01ABCDEF", "thread": "", "sid": "s1", "name": "brave-otter", "agent": "worker",
               "domain": "general", "tool": "mcp__chat__send_message"},
              "1792700001.500000", "brave-otter → D01ABCDEF: hi", {"to": "U01OTHER1", "text": "hi"})
    assert events.append(e) is True
    got = events.recent("sent", T0 + 10)
    assert got == [e] and got[0].match["sid"] == "s1" and got[0].cursor_key == "1792700001.500000"


def test_subscribe_records_the_sub_and_journals_it(fresh_orchestrator_dir, events_cfg, tmp_path, monkeypatch):
    _work(monkeypatch, tmp_path)
    sub = events.subscribe("K", "design", "tracker", {"issue": "ABC-1"}, kinds=["comment", "status"], after="12345")
    assert len(sub.sub_id) == 8 and sub.after == "12345" and sub.delivered is None
    assert [s.sub_id for s in events.subs_for("K")] == [sub.sub_id]
    assert [e["type"] for e in _journal(kind="subscribed")] == ["subscribed"]
    assert _journal(kind="subscribed")[0]["sub_id"] == sub.sub_id
    assert events.subs_for("K", source="deploy") == [] and events.subs_for("K", node="design") == [sub]


def test_a_second_subscribe_with_the_same_key_replaces_the_first(fresh_orchestrator_dir, events_cfg, tmp_path,
                                                                monkeypatch):
    _work(monkeypatch, tmp_path)
    first = events.subscribe("K", "design", "tracker", {"issue": "ABC-1"}, after="1")
    second = events.subscribe("K", "design", "tracker", {"issue": "ABC-1"}, after="2")
    assert first.sub_id != second.sub_id
    assert [s.sub_id for s in events.subs_for("K")] == [second.sub_id]
    gone = _journal(kind="unsubscribed")
    assert [(e["sub_id"], e["reason"]) for e in gone] == [(first.sub_id, "replaced")]


def test_subscribe_refuses_when_the_layer_is_off(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _work(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="events layer is off"):
        events.subscribe("K", "design", "tracker", {"issue": "ABC-1"}, after="1")


def test_subscribe_refuses_a_node_that_does_not_hold_t1(fresh_orchestrator_dir, events_cfg, tmp_path, monkeypatch):
    _work(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="does not hold"):
        events.subscribe("K", "plan", "tracker", {"issue": "ABC-1"}, after="1")
    with pytest.raises(ValueError, match="does not hold"):
        events.subscribe("NOPE", "design", "tracker", {"issue": "ABC-1"}, after="1")


def test_subscribe_refuses_a_forked_token(fresh_orchestrator_dir, events_cfg, tmp_path, monkeypatch):
    _work(monkeypatch, tmp_path)
    ms._append_task_event("K", {"type": "token_forked", "token_id": "t1", "node": "design",
                                "rows": [{"row": "a", "stage": 1, "token_id": "t1/a"}]})
    with pytest.raises(ValueError, match="fork"):
        events.subscribe("K", "design", "tracker", {"issue": "ABC-1"}, after="1")


def test_subscribe_takes_prod_with_sha_and_refuses_prod_with_run_id(fresh_orchestrator_dir, events_cfg, tmp_path,
                                                                     monkeypatch):
    _work(monkeypatch, tmp_path)
    sub = events.subscribe("K", "design", "deploy", {"repo": "o/r", "env": "prod", "sha": "a" * 40})
    assert sub.match == {"repo": "o/r", "env": "prod", "sha": "a" * 40}
    with pytest.raises(ValueError, match="a prod subscription needs sha and no run_id"):
        events.subscribe("K", "design", "deploy", {"repo": "o/r", "env": "prod", "run_id": "7"})
    with pytest.raises(ValueError, match="a prod subscription needs sha and no run_id"):
        events.subscribe("K", "design", "deploy", {"repo": "o/r", "env": "prod", "sha": "a" * 40, "run_id": "7"})
    with pytest.raises(ValueError, match="only staging, dev, prod"):
        events.subscribe("K", "design", "deploy", {"repo": "o/r", "env": "qa", "sha": "a" * 40})


def test_subscribe_refuses_an_unknown_source_or_kind(fresh_orchestrator_dir, events_cfg, tmp_path, monkeypatch):
    _work(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="unknown source"):
        events.subscribe("K", "design", "mail", {"channel": "c"})
    with pytest.raises(ValueError, match="unknown kind"):
        events.subscribe("K", "design", "tracker", {"issue": "ABC-1"}, kinds=["x"], after="1")


def test_a_tracker_subscription_needs_a_loadable_env_file(fresh_orchestrator_dir, events_cfg, tmp_path, monkeypatch):
    _work(monkeypatch, tmp_path)
    env_file = tmp_path / "events.env"
    env_file.chmod(0o644)
    with pytest.raises(ValueError, match="tracker source cannot poll.*mode"):
        events.subscribe("K", "design", "tracker", {"issue": "ABC-1"}, after="1")
    env_file.unlink()
    with pytest.raises(ValueError, match="tracker source cannot poll.*missing"):
        events.subscribe("K", "design", "tracker", {"issue": "ABC-1"}, after="1")
    sub = events.subscribe("K", "design", "deploy", {"repo": "o/r", "env": "staging", "sha": "a" * 40})
    assert sub.source == "deploy"
    assert events.load_env(env_file) == (None, "missing")
    env_file.write_text("TRACKER_URL=https://j\nTRACKER_EMAIL=me@x\n")
    env_file.chmod(0o600)
    assert events.load_env(env_file) == (None, "incomplete (no TRACKER_API_TOKEN)")
    with pytest.raises(ValueError, match="tracker source cannot poll.*incomplete \\(no TRACKER_API_TOKEN\\)"):
        events.subscribe("K", "design", "tracker", {"issue": "ABC-1"}, after="1")


def test_a_tracker_subscription_needs_after(fresh_orchestrator_dir, events_cfg, tmp_path, monkeypatch):
    _work(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="after"):
        events.subscribe("K", "design", "tracker", {"issue": "ABC-1"})
    with pytest.raises(ValueError, match="issue"):
        events.subscribe("K", "design", "tracker", {}, after="1")


def test_a_deploy_subscription_needs_repo_env_and_sha_or_run_id(fresh_orchestrator_dir, events_cfg, tmp_path,
                                                                monkeypatch):
    _work(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="repo"):
        events.subscribe("K", "design", "deploy", {"env": "staging", "sha": "a" * 40})
    with pytest.raises(ValueError, match="env"):
        events.subscribe("K", "design", "deploy", {"repo": "o/r", "sha": "a" * 40})
    with pytest.raises(ValueError, match="sha"):
        events.subscribe("K", "design", "deploy", {"repo": "o/r", "env": "staging"})
    sub = events.subscribe("K", "design", "deploy", {"repo": "o/r", "env": "dev", "run_id": "77"})
    assert sub.source == "deploy" and sub.kinds is None


def test_a_handler_must_be_a_listed_name_found_in_handler_path(fresh_orchestrator_dir, events_cfg, tmp_path,
                                                               monkeypatch):
    _work(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="handler"):
        events.subscribe("K", "design", "tracker", {"issue": "ABC-1"}, after="1", handler=["author-wait", "close"])
    path = _handler(tmp_path)
    sub = events.subscribe("K", "design", "tracker", {"issue": "ABC-1"}, after="1", handler=["author-wait", "close"])
    assert sub.handler == ["author-wait", "close"]
    with pytest.raises(ValueError, match="handler"):
        events.subscribe("K", "design", "tracker", {"issue": "ABC-2"}, after="1", handler=["other"])
    with pytest.raises(ValueError, match="handler"):
        events.subscribe("K", "design", "tracker", {"issue": "ABC-2"}, after="1", handler=[str(path)])


def test_handler_ok_resolves_the_path(fresh_orchestrator_dir, events_cfg, tmp_path):
    assert events.handler_ok(["author-wait"]) is None
    path = _handler(tmp_path)
    assert events.handler_ok(["author-wait", "close"]) == str(path)
    assert events.handler_ok(["x"]) is None and events.handler_ok([str(path)]) is None and events.handler_ok([]) is None


def test_unsubscribe_by_id_and_by_task_key_and_node(fresh_orchestrator_dir, events_cfg, tmp_path, monkeypatch):
    _work(monkeypatch, tmp_path)
    a = events.subscribe("K", "design", "tracker", {"issue": "ABC-1"}, after="1")
    b = events.subscribe("K", "design", "tracker", {"issue": "ABC-2"}, after="1")
    c = events.subscribe("K", "design", "deploy", {"repo": "o/r", "env": "staging", "sha": "a" * 40})
    assert events.unsubscribe(a.sub_id, reason="fired") == 1
    assert events.unsubscribe(task_key="K", node="design", reason="closed") == 2
    assert events.unsubscribe(task_key="K", node="design") == 0
    assert events.subs_for("K") == []
    assert sorted((e["sub_id"], e["reason"]) for e in _journal(kind="unsubscribed")) == sorted(
        [(a.sub_id, "fired"), (b.sub_id, "closed"), (c.sub_id, "closed")])


def test_mark_delivered_stores_the_events_ts_and_id(fresh_orchestrator_dir, events_cfg, tmp_path, monkeypatch):
    _work(monkeypatch, tmp_path)
    sub = events.subscribe("K", "design", "tracker", {"issue": "K"}, after="0")
    e = _event(ts=T0 + 5)
    events.mark_delivered([sub.sub_id, "missing"], e)
    assert events.subs_for("K")[0].delivered == [T0 + 5, "tracker:K:comment:1"]


def test_drop_left_removes_subs_of_nodes_the_token_left(fresh_orchestrator_dir, events_cfg, tmp_path, monkeypatch):
    _work(monkeypatch, tmp_path)
    kept = events.subscribe("K", "design", "tracker", {"issue": "K"}, after="0")
    fold_ = ms._fold_for("K")
    stray = events.Sub("deadbeef", "K", "plan", "tracker", {"issue": "K"}, None, "0", None, None, None, T0, None)
    (paths.EVENTS_SUBS / "deadbeef.json").write_text(json.dumps(stray.to_dict()))
    assert events.drop_left("K", fold_) == ["deadbeef"]
    assert [s.sub_id for s in events.subs_for("K")] == [kept.sub_id]
    assert [(e["sub_id"], e["reason"]) for e in _journal(kind="unsubscribed")] == [("deadbeef", "left")]


def _sub(**kw):
    base = dict(sub_id="a1b2c3d4", task_key="K", node="design", source="tracker", match={"issue": "K"}, kinds=None,
                after=None, deadline=None, handler=None, skip_own=None, since=T0, delivered=None)
    return events.Sub(**{**base, **kw})


def test_matches_checks_source_match_subset_kinds_after_and_delivered():
    sub = _sub(kinds=["comment"], after="10")
    assert events.matches(sub, _event(cursor_key="11"))
    assert not events.matches(sub, _event(cursor_key="10"))
    assert not events.matches(sub, _event(cursor_key="11", kind="status"))
    assert not events.matches(sub, _event(cursor_key="11", source="deploy"))
    assert not events.matches(sub, _event(cursor_key="11", match={"issue": "OTHER"}))
    assert events.matches(sub, _event(cursor_key="11", match={"issue": "K", "extra": "x"}))
    assert events.matches(_sub(kinds=None, after=None), _event(kind="status", cursor_key=""))
    assert not events.matches(_sub(delivered=[T0, "tracker:K:comment:1"]), _event())
    assert events.matches(_sub(delivered=[T0, "tracker:K:comment:1"]), _event(id="tracker:K:comment:2", cursor_key="2"))


def test_matches_drops_an_event_by_the_subs_skip_own_author():
    sub = _sub(skip_own="acc-1")
    assert not events.matches(sub, _event(payload={"author_id": "acc-1"}))
    assert events.matches(sub, _event(payload={"author_id": "acc-2"}))
    assert events.matches(_sub(), _event(payload={"author_id": "acc-1"}))


def test_a_cursor_key_compares_numerically_when_both_sides_are_digits():
    assert events.matches(_sub(after="999"), _event(cursor_key="1000"))
    assert not events.matches(_sub(after="1000"), _event(cursor_key="999"))
    assert events.matches(_sub(after="abc"), _event(cursor_key="abd"))
    assert not events.matches(_sub(after="abd"), _event(cursor_key="abc"))


def test_delivered_compares_by_ts_then_id_not_by_id_alone():
    sub = _sub(delivered=[10.0, "tracker:K:comment:999"], since=0)
    assert events.matches(sub, _event(id="tracker:K:comment:1000", ts=11.0, cursor_key="1000"))
    assert not events.matches(sub, _event(id="tracker:K:comment:1000", ts=9.0, cursor_key="1000"))
    assert events.matches(sub, _event(id="tracker:K:comment:9999", ts=10.0, cursor_key="9999"))


def test_an_event_not_keyed_by_after_matches_only_from_the_subscription_on():
    sub = _sub(kinds=None, after=None, since=T0)
    assert not events.matches(sub, _event(kind="status", cursor_key="", ts=T0 - 1))
    assert events.matches(sub, _event(kind="status", cursor_key="", ts=T0))
    deploy = _sub(source="deploy", match={"repo": "o/r", "env": "staging", "sha": "a" * 40}, since=T0)
    stale = _event(id="deploy:o/r:a:staging:1", source="deploy", kind="job", match=dict(deploy.match), cursor_key="1",
                   ts=T0 - 3600)
    assert not events.matches(deploy, stale)
    keyed = _sub(kinds=["comment"], after="10", since=T0)
    assert events.matches(keyed, _event(cursor_key="11", ts=T0 - 3600))


def test_a_handler_entry_may_pin_the_leading_arguments(fresh_orchestrator_dir, events_cfg, tmp_path, monkeypatch):
    _work(monkeypatch, tmp_path)
    path = _handler(tmp_path)
    events_cfg.write_text(events_cfg.read_text().replace('handlers = ["author-wait"]', 'handlers = ["author-wait close"]'))
    assert events.handler_ok(["author-wait", "close", "--task-key", "K"]) == str(path)
    assert events.handler_ok(["author-wait", "--task-key", "K", "--questions", "f"]) is None
    assert events.handler_ok(["author-wait"]) is None
    with pytest.raises(ValueError, match="handler"):
        events.subscribe("K", "design", "tracker", {"issue": "ABC-1"}, after="1", handler=["author-wait", "--since", "0"])
    sub = events.subscribe("K", "design", "tracker", {"issue": "ABC-1"}, after="1", handler=["author-wait", "close", "-x"])
    assert sub.handler == ["author-wait", "close", "-x"]


def test_a_time_subscription_needs_a_deadline(fresh_orchestrator_dir, events_cfg, tmp_path, monkeypatch):
    _work(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="deadline"):
        events.subscribe("K", "design", "time", {})
    assert events.subscribe("K", "design", "time", {}, deadline=T0 + 10).deadline == T0 + 10


def test_the_seen_index_keeps_an_id_for_fourteen_days_after_it_was_recorded(fresh_orchestrator_dir, events_cfg):
    import time as clock
    old = _event(id="tracker:K:comment:11", ts=clock.time() - 15 * 86400, cursor_key="11")
    assert events.append(old) is True
    assert events.append(_event(id="tracker:K:comment:99", ts=clock.time(), cursor_key="99")) is True
    assert events.append(old) is False
    assert len([e for e in events.read_since("tracker", 0) if e.id == old.id]) == 1


def test_a_time_event_matches_only_its_sub():
    deadline = _event(id="time:a1b2c3d4:deadline", source="time", kind="deadline", match={"sub_id": "a1b2c3d4"},
                      cursor_key="")
    assert events.matches(_sub(), deadline)
    assert not events.matches(_sub(sub_id="other"), deadline)
    assert not events.matches(_sub(sub_id="other", source="time"), deadline)


def test_line_renders_the_wake_text():
    assert events.line(_event(), _sub()) == "event: tracker.comment — K #1 by A: hi (sub a1b2c3d4)"
    assert events.EVENT_MARK == "event: "


def test_the_cli_subscribe_prints_json_and_exits_3_on_refusal(fresh_orchestrator_dir, events_cfg, tmp_path,
                                                            monkeypatch, capsys):
    _work(monkeypatch, tmp_path)
    rc = events.main(["subscribe", "--task-key", "K", "--node", "design", "--source", "tracker", "--match",
                      "issue=ABC-1", "--kinds", "comment,status", "--after", "12", "--deadline", "1792700100",
                      "--skip-own", "acc-1"])
    out = json.loads(capsys.readouterr().out.strip())
    assert rc == 0 and out == {"ok": True, "sub_id": out["sub_id"], "source": "tracker", "after": "12",
                               "deadline": 1792700100.0}
    sub = events.subs_for("K")[0]
    assert sub.kinds == ["comment", "status"] and sub.skip_own == "acc-1" and sub.deadline == 1792700100.0
    assert events.main(["list", "K"]) == 0
    assert json.loads(capsys.readouterr().out.strip())["sub_id"] == out["sub_id"]
    rc = events.main(["subscribe", "--task-key", "K", "--node", "plan", "--source", "tracker", "--match", "issue=X",
                      "--after", "1"])
    assert rc == 3 and json.loads(capsys.readouterr().out.strip()) == {
        "ok": False, "reason": "node plan does not hold token t1 of K"}
    assert events.main(["unsubscribe", "--task-key", "K", "--node", "design"]) == 0
    assert json.loads(capsys.readouterr().out.strip()) == {"ok": True, "removed": 1}


def test_the_cli_emit_appends_an_event_from_a_file(fresh_orchestrator_dir, events_cfg, tmp_path, capsys):
    f = tmp_path / "e.json"
    f.write_text(json.dumps(_event().to_dict()))
    assert events.main(["emit", "--file", str(f)]) == 0
    assert json.loads(capsys.readouterr().out.strip()) == {"ok": True, "appended": True}
    assert [e.id for e in events.read_since("tracker", 0)] == ["tracker:K:comment:1"]
    assert events.main(["emit", "--file", str(f)]) == 0
    assert json.loads(capsys.readouterr().out.strip()) == {"ok": True, "appended": False}


def test_event_and_sub_round_trip_through_dicts():
    e = _event(payload={"a": 1})
    assert Event.from_dict(e.to_dict()) == e
    s = _sub(handler=["author-wait", "close"], delivered=[1.0, "x"])
    assert events.Sub.from_dict(s.to_dict()) == s


# MCP


def test_event_subscribe_tool_requires_the_caller_to_hold_the_token(fresh_orchestrator_dir, events_cfg, tmp_path,
                                                                    monkeypatch):
    _work(monkeypatch, tmp_path)
    r = ms.event_subscribe_impl("sid-K-design", "K", "design", "tracker", {"issue": "K"}, after="1")
    assert r == {"ok": True, "sub_id": r["sub_id"], "source": "tracker", "after": "1", "deadline": None}
    _register_worker("K-other", "sid-other", "a-other", "K")
    with pytest.raises(ValueError, match="hold no token"):
        ms.event_subscribe_impl("sid-other", "K", "design", "tracker", {"issue": "K"}, after="1")
    with pytest.raises(ValueError, match="hold no token"):
        ms.event_subscribe_impl("sid-K-design", "K", "plan", "tracker", {"issue": "K"}, after="1")


def test_event_unsubscribe_tool_removes_the_callers_subs(fresh_orchestrator_dir, events_cfg, tmp_path, monkeypatch):
    _work(monkeypatch, tmp_path)
    a = ms.event_subscribe_impl("sid-K-design", "K", "design", "tracker", {"issue": "K"}, after="1")
    ms.event_subscribe_impl("sid-K-design", "K", "design", "tracker", {"issue": "K2"}, after="1")
    assert ms.event_unsubscribe_impl("sid-K-design", "K", "design", sub_id=a["sub_id"]) == {"ok": True, "removed": 1}
    assert ms.event_unsubscribe_impl("sid-K-design", "K", "design") == {"ok": True, "removed": 1}
    _register_worker("K-other", "sid-other", "a-other", "K")
    with pytest.raises(ValueError, match="hold no token"):
        ms.event_unsubscribe_impl("sid-other", "K", "design")


def test_event_queue_and_event_ack_tools_read_and_close_a_rule_queue(fresh_orchestrator_dir, events_cfg):
    from dockwright import event_queue
    events_cfg.write_text(events_cfg.read_text() + '[events.rules.r]\nsource = "exec"\nkind = "incident"\naction = "queue"\n')
    e1, e2 = _event("exec:a", source="exec", kind="incident"), _event("exec:b", T0 + 1, source="exec", kind="incident")
    event_queue.push("r", [e1, e2])
    assert ms.event_queue_impl("r") == {"ok": True, "rule": "r", "pending": [e1.to_dict(), e2.to_dict()]}
    assert ms.event_ack_impl("r", e1.id) == {"ok": True, "rule": "r", "removed": 1, "remaining": 1}
    assert ms.event_ack_impl("r", "*") == {"ok": True, "rule": "r", "removed": 1, "remaining": 0}
    assert ms.event_ack_impl("r", "zzz")["removed"] == 0
    with pytest.raises(ValueError, match="unknown rule"):
        ms.event_queue_impl("nope")
    with pytest.raises(ValueError, match="unknown rule"):
        ms.event_ack_impl("nope", "*")
    events_cfg.write_text(events_cfg.read_text() + '[events.rules.p]\nsource = "exec"\nkind = "incident"\naction = "page"\n')
    with pytest.raises(ValueError, match="not a queue or reply rule"):
        ms.event_queue_impl("p")
    with pytest.raises(ValueError, match="not a queue or reply rule"):
        ms.event_ack_impl("p", "*")
    events_cfg.write_text(events_cfg.read_text() + '[events.rules.q]\nsource = "chat"\nkind = "dm"\naction = "reply"\n')
    r1, r2 = _event("chat:a", source="chat", kind="dm"), _event("chat:b", T0 + 1, source="chat", kind="dm")
    event_queue.push("q", [r1, r2])
    assert ms.event_queue_impl("q")["ok"] is True
    with pytest.raises(ValueError, match="one record at a time"):
        ms.event_ack_impl("q", "*")
    assert [e.id for e in event_queue.read("q")] == [r1.id, r2.id]
    assert ms.event_ack_impl("q", r1.id) == {"ok": True, "rule": "q", "removed": 1, "remaining": 1}
    event_queue.push("r", [e1])
    out = asyncio.run(ms.mcp.call_tool("event_queue", {"rule": "r"}))
    content = out[0] if isinstance(out, tuple) else out
    assert '"pending"' in content[0].text and '"exec:a"' in content[0].text


# chat


def _chat_apps(cfg):
    cfg.write_text(cfg.read_text() + '[events.sources.chat]\napps = ["com.example.chat"]\n')


def test_a_chat_subscription_takes_the_five_keys_and_refuses_after_and_other_keys(
        fresh_orchestrator_dir, events_cfg, tmp_path, monkeypatch):
    _chat_apps(events_cfg)
    _work(monkeypatch, tmp_path)
    sub = events.subscribe("K", "design", "chat", {"sender": "Ann Lee"}, kinds=["dm"])
    assert sub.source == "chat" and sub.after == events.chat_time(sub.since)
    thread = events.subscribe("K", "design", "chat", {"channel": "C1", "thread": "1792700000.000050"},
                              kinds=["channel-thread", "dm-thread"])
    assert thread.kinds == ["channel-thread", "dm-thread"] and thread.match == {"channel": "C1",
                                                                              "thread": "1792700000.000050"}
    assert sorted(s.sub_id for s in events.subs_for("K", source="chat")) == sorted([sub.sub_id, thread.sub_id])
    with pytest.raises(ValueError, match="takes no after"):
        events.subscribe("K", "design", "chat", {"sender": "Ann Lee"}, after="1")
    with pytest.raises(ValueError, match="app, conversation, sender, channel and thread"):
        events.subscribe("K", "design", "chat", {"issue": "c"})
    with pytest.raises(ValueError, match="unknown kind"):
        events.subscribe("K", "design", "chat", {"sender": "Ann Lee"}, kinds=["mention"])


def test_a_repeat_chat_subscription_keeps_its_window_and_its_delivered_mark(fresh_orchestrator_dir, events_cfg,
                                                                           tmp_path, monkeypatch):
    _chat_apps(events_cfg)
    _work(monkeypatch, tmp_path)
    since = time.time() - 60
    old = events.Sub("deadbeef", "K", "design", "chat", {"sender": "Ann Lee"}, ["dm"], events.chat_time(since), None,
                     None, None, since, [since + 10, "chat:a"])
    paths.EVENTS_SUBS.mkdir(parents=True, exist_ok=True)
    (paths.EVENTS_SUBS / "deadbeef.json").write_text(json.dumps(old.to_dict()))
    sub = events.subscribe("K", "design", "chat", {"sender": "Ann Lee"})
    assert (sub.since, sub.after, sub.delivered, sub.kinds) == (since, old.after, old.delivered, None)
    assert events.subs_for("K", source="chat") == [sub]
    match = {"app": "com.example.chat", "conversation": "Ann Lee", "sender": "Ann Lee"}
    pending = _event(id="chat:b", source="chat", kind="dm", ts=since + 20, match=match,
                     cursor_key=events.chat_time(since + 5))
    delivered = _event(id="chat:a", source="chat", kind="dm", ts=since + 10, match=match,
                       cursor_key=events.chat_time(since + 2))
    assert events.matches(sub, pending) and not events.matches(sub, delivered)

def test_a_chat_subscription_needs_apps(fresh_orchestrator_dir, events_cfg, tmp_path, monkeypatch):
    _work(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match=r"\[events.sources.chat\] apps"):
        events.subscribe("K", "design", "chat", {"sender": "Ann Lee"})


def test_a_chat_event_matches_by_subset_and_by_delivery_time_against_after():
    assert events.chat_time(T0) == "1792700000.000000" and events.chat_time(1.5) == "0000000001.500000"
    sub = _sub(source="chat", match={"conversation": "#team-x"}, after=events.chat_time(T0))

    def channel(cursor_key, conversation="#team-x"):
        return _event(id="chat:x", source="chat", kind="channel", ts=T0 + 100, cursor_key=cursor_key,
                      match={"app": "com.example.chat", "conversation": conversation, "sender": "Bob Ray"},
                      summary=f"Bob Ray in {conversation}")

    assert events.matches(sub, channel(events.chat_time(T0 + 1)))
    assert not events.matches(sub, channel(events.chat_time(T0 - 1)))
    assert not events.matches(sub, channel(events.chat_time(T0 + 1), "#other"))


def test_a_thread_subscription_does_not_wake_on_a_fallback_event_but_a_sender_subscription_does():
    thread_sub = _sub(source="chat", match={"thread": "1792700000.000050"}, after=events.chat_time(T0))
    sender_sub = _sub(source="chat", match={"sender": "Bob Ray"}, after=events.chat_time(T0))
    base = {"app": "com.example.chat", "conversation": "#team-x", "sender": "Bob Ray"}
    fallback = _event(id="chat:x", source="chat", kind="channel", ts=T0 + 100, cursor_key=events.chat_time(T0 + 1),
                      match={**base, "channel": "", "thread": ""})
    enriched = _event(id="chat:msg:C1:1792700000.000100", source="chat", kind="channel-thread", ts=T0 + 100,
                      cursor_key=events.chat_time(T0 + 1), match={**base, "channel": "C1", "thread": "1792700000.000050"})
    assert events.matches(sender_sub, fallback) and not events.matches(thread_sub, fallback)
    assert events.matches(sender_sub, enriched) and events.matches(thread_sub, enriched)


def test_the_event_subscribe_tool_accepts_the_chat_source(fresh_orchestrator_dir, events_cfg, tmp_path, monkeypatch):
    _chat_apps(events_cfg)
    _work(monkeypatch, tmp_path)
    args = {"claude_sid": "sid-K-design", "task_key": "K", "node": "design", "source": "chat",
            "match": {"sender": "Ann Lee"}, "kinds": ["dm"]}
    out = asyncio.run(ms.mcp.call_tool("event_subscribe", args))
    content = out[0] if isinstance(out, tuple) else out
    assert '"source": "chat"' in content[0].text
    with pytest.raises(ToolError):
        asyncio.run(ms.mcp.call_tool("event_subscribe", {**args, "source": "mail"}))


# time


def test_a_passed_deadline_of_any_source_emits_one_deadline_event_for_its_sub(fresh_orchestrator_dir, events_cfg):
    from dockwright.event_sources import time as time_source
    tracker_sub = _sub(sub_id="j1", deadline=T0 - 1)
    deploy_sub = _sub(sub_id="d1", source="deploy", match={"repo": "o/r", "env": "staging", "sha": "a" * 40},
                      deadline=T0 - 100)
    evs, cursor = time_source.poll([tracker_sub, deploy_sub, _sub(sub_id="n1")], {}, {"x": 1}, {}, T0)
    assert [(e.id, e.source, e.kind, e.match, e.ts) for e in evs] == [
        ("time:j1:deadline", "time", "deadline", {"sub_id": "j1"}, T0),
        ("time:d1:deadline", "time", "deadline", {"sub_id": "d1"}, T0)]
    assert evs[0].summary == "deadline 2026-10-22T20:13:19Z passed for tracker {'issue': 'K'}"
    assert evs[1].summary.startswith("deadline 2026-10-22T20:11:40Z passed for deploy {'repo': 'o/r'")
    assert cursor == {"x": 1}
    assert [events.append(e) for e in evs] == [True, True]
    again, _ = time_source.poll([tracker_sub, deploy_sub], {}, {}, {}, T0 + 60)
    assert [events.append(e) for e in again] == [False, False]
    assert events.matches(tracker_sub, evs[0]) and not events.matches(deploy_sub, evs[0])


def test_a_future_deadline_emits_nothing(fresh_orchestrator_dir, events_cfg):
    from dockwright.event_sources import time as time_source
    assert time_source.poll([_sub(deadline=T0 + 1)], {}, {}, {}, T0) == ([], {})
