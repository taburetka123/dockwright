import asyncio
import json
import re

import pytest

from dockwright import board, paths, pipeline, topic_message
from dockwright import mcp_server as ms
from tests.test_pipeline_core import SCHEMA
from tests.test_pipeline_reconcile import (MGR, _fake_registration, _manager, _register_worker,  # noqa: F401
                                           _started_at_design, clock, fresh_orchestrator_dir, killed, session_logs)

GRANTED = SCHEMA.replace('extra_args = []',
                         'extra_args = ["--allowedTools", "mcp__dockwright__pipeline_start"]')


@pytest.fixture
def started(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_HOME", str(tmp_path / "worker-home"))
    monkeypatch.setattr(topic_message.topic_agent, "live_domain_manager", lambda domain: "m1")

    async def fake_start(manager_sid, task_key, schema, cwd, ask):
        calls.append((manager_sid, task_key, schema, cwd, ask))
        return {"ok": True, "state": "committed"}

    monkeypatch.setattr(ms, "pipeline_start_impl", fake_start)
    return calls


def _k_events():
    return ms._read_events(paths.artifact_events_path("K"))


def test_a_granted_node_starts_a_work_under_its_own_manager(fresh_orchestrator_dir, monkeypatch, tmp_path):
    node_sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=GRANTED)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(ms.pipeline_start_impl(node_sid, "K2", "code-ticket", "/w", "look at X"))
    assert ms._fold_for("K2").tokens[pipeline.DEFAULT_TOKEN_ID].manager == MGR["name"]
    assert ms.artifact_get_impl("K2", "context", "ask")["content"] == "look at X"


def test_a_granted_node_without_a_live_manager_starts_nothing(fresh_orchestrator_dir, monkeypatch, tmp_path):
    node_sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=GRANTED)
    monkeypatch.setattr(ms, "_manager_sid_for", lambda task_key, assignment: None)
    with pytest.raises(ValueError, match="no live manager"):
        asyncio.run(ms.pipeline_start_impl(node_sid, "K2", "code-ticket", "/w", "x"))
    assert not paths.artifact_ticket_dir("K2").exists()


def test_a_node_whose_schema_node_lacks_the_grant_is_refused(fresh_orchestrator_dir, monkeypatch, tmp_path):
    node_sid, _ = _started_at_design(monkeypatch, tmp_path)
    before = _k_events()
    _fake_registration(monkeypatch, ["registered"])
    with pytest.raises(ValueError, match="not a live top-level manager"):
        asyncio.run(ms.pipeline_start_impl(node_sid, "K2", "code-ticket", "/w", "x"))
    assert not paths.artifact_ticket_dir("K2").exists() and _k_events() == before


def test_a_granted_node_whose_snapshot_cannot_be_read_is_refused(fresh_orchestrator_dir, monkeypatch, tmp_path):
    node_sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=GRANTED)
    (paths.artifact_ticket_dir("K") / "schema.toml").write_text("not toml [")
    _fake_registration(monkeypatch, ["registered"])
    with pytest.raises(ValueError, match="not a live top-level manager"):
        asyncio.run(ms.pipeline_start_impl(node_sid, "K2", "code-ticket", "/w", "x"))
    assert not paths.artifact_ticket_dir("K2").exists()


def test_a_handed_off_node_is_refused_and_starts_nothing(fresh_orchestrator_dir, monkeypatch, tmp_path):
    node_sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=GRANTED)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(ms.pipeline_advance_impl("m1", "K", "design", override=True, reason="redo"))
    before = _k_events()
    _fake_registration(monkeypatch, ["registered"])
    with pytest.raises(ValueError, match=f"node {node_sid} .*handed off; call worker_done"):
        asyncio.run(ms.pipeline_start_impl(node_sid, "K2", "code-ticket", "/w", "x"))
    assert not paths.artifact_ticket_dir("K2").exists() and _k_events() == before


def test_a_plain_worker_is_still_refused(fresh_orchestrator_dir):
    _manager()
    _register_worker("plain", "w1", "a1", "K")
    with pytest.raises(ValueError, match="not a live top-level manager"):
        asyncio.run(ms.pipeline_start_impl("w1", "K2", "code-ticket", "/w", "x"))
    assert not paths.artifact_ticket_dir("K2").exists()


def test_new_work_without_a_live_domain_manager_writes_nothing(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="no live manager"):
        asyncio.run(topic_message.deliver(None, "investigate X", kind="new", decided_by="engineer", via="cli",
                                          domain="meta"))
    assert board.keys() == []


def test_new_work_starts_the_intake_sorter(fresh_orchestrator_dir, started, tmp_path):
    out = asyncio.run(topic_message.deliver(None, "investigate X", kind="new", decided_by="engineer", via="board",
                                            domain="meta"))
    assert re.fullmatch(r"in-\d{8}T\d{6}-[0-9a-f]{4}", out["task_key"])
    assert started == [("m1", out["task_key"], "intake", str(tmp_path / "worker-home"), "investigate X")]
    assert out["route"] == "intake" and out["delivered"] and out["record"] == "context.ask"


def test_a_pending_start_is_not_delivered(fresh_orchestrator_dir, monkeypatch, started):
    async def pending(manager_sid, task_key, schema, cwd, ask):
        return {"ok": True, "state": "pending", "reason": "tmux server not running"}

    monkeypatch.setattr(ms, "pipeline_start_impl", pending)
    out = asyncio.run(topic_message.deliver(None, "investigate X", kind="new", via="cli", domain="meta"))
    assert not out["delivered"] and out["reason"] == "tmux server not running"


@pytest.mark.parametrize("domain", [None, "", "../meta"])
def test_new_work_needs_a_domain_slug(fresh_orchestrator_dir, started, domain):
    with pytest.raises(ValueError, match="domain"):
        asyncio.run(topic_message.deliver(None, "investigate X", kind="new", via="cli", domain=domain))
    assert started == []


def test_the_mcp_tool_starts_new_work_inside_a_live_loop(fresh_orchestrator_dir, started):
    asyncio.run(ms.mcp.call_tool("topic_message", {"task_key": "", "text": "x", "kind": "new", "domain": "meta"}))
    assert [(c[0], c[2], c[4]) for c in started] == [("m1", "intake", "x")]


def test_the_cli_starts_new_work(fresh_orchestrator_dir, started, capsys, monkeypatch):
    monkeypatch.delenv("CLAUDE_AGENT", raising=False)
    assert topic_message.main(["new", "--domain", "meta", "--text", "investigate X"]) == 0
    assert json.loads(capsys.readouterr().out)["route"] == "intake"
    assert [(c[2], c[4]) for c in started] == [("intake", "investigate X")]


def test_the_topic_index_prints_as_json(fresh_orchestrator_dir, capsys):
    assert board.main(["topics", "--json"]) == 0
    assert set(json.loads(capsys.readouterr().out)) >= {"now", "rows", "notes", "warnings"}


def test_the_json_index_names_each_running_work(fresh_orchestrator_dir, monkeypatch, tmp_path, capsys):
    _started_at_design(monkeypatch, tmp_path)
    assert board.main(["topics", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["rows"]
    assert [(r["key"], r["kind"], r["title"]) for r in rows] == [("K", "code-ticket", "ask")]
