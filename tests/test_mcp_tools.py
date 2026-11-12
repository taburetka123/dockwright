import inspect
import os
import shlex
import subprocess
import time
import json as _json
import time as _time
from datetime import datetime, timezone
from pathlib import Path
import pytest
from dockwright import artifact_export, models, notebook_mark, paths, state, waiting_on
from dockwright import config as _config
from dockwright.mcp_server import register_self_impl, list_workers_impl
from tests.conftest import allow_codex

FLAGS = ["--model", "@opus", "--effort", "xhigh"]


@pytest.fixture
def fresh_orchestrator_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(paths, "QUESTIONS", tmp_path / "questions")
    monkeypatch.setattr(paths, "ANSWERS", tmp_path / "answers")
    monkeypatch.setattr(paths, "DONE", tmp_path / "done")
    monkeypatch.setattr(paths, "TURN_ENDS", tmp_path / "turn-ends")
    monkeypatch.setattr(paths, "CLOSED", tmp_path / "closed")
    monkeypatch.setattr(paths, "HANDOFFS", tmp_path / "handoffs")
    monkeypatch.setattr(paths, "PRESETS", tmp_path / "presets")
    monkeypatch.setattr(paths, "MANAGER_TRIGGERS_LOG", tmp_path / "manager-triggers.jsonl")
    monkeypatch.setattr(paths, "MANAGER_MEMORY", tmp_path / "manager-memory")
    monkeypatch.setattr(paths, "SLOTS", tmp_path / "slots")
    monkeypatch.setattr(paths, "ARTIFACTS", tmp_path / "artifacts")
    monkeypatch.setattr(paths, "ASSIGNMENTS", tmp_path / "assignments")
    monkeypatch.setattr(paths, "ASSIGNMENTS_PENDING", tmp_path / "assignments" / ".pending")
    monkeypatch.setattr(paths, "PANE_SENDS", tmp_path / "pane-sends")
    monkeypatch.setattr(paths, "SPEND_LEDGER", tmp_path / "spend-ledger.jsonl")
    monkeypatch.setattr(paths, "CHAT", tmp_path / "chat")
    monkeypatch.setattr(paths, "CHAT_DB", tmp_path / "chat" / "archive.db")
    monkeypatch.setattr(paths, "CHAT_RAW", tmp_path / "chat" / "raw")
    monkeypatch.setattr(paths, "CHAT_TRIGGERS", tmp_path / "chat" / "triggers")
    monkeypatch.setattr(paths, "CHAT_LEDGER", tmp_path / "chat" / "ledger.jsonl")
    monkeypatch.setattr(paths, "CHAT_STARTED", tmp_path / "chat" / "started")
    monkeypatch.setattr(paths, "CHAT_LOCK", tmp_path / "chat" / "pass.lock")
    monkeypatch.setattr(paths, "CHAT_STOP", tmp_path / "chat-archive-stop")
    paths.ensure_dirs()
    yield tmp_path


def _install_two_pool(monkeypatch, tmp_path):
    cfg = tmp_path / "two-pool.toml"
    cfg.write_text('[accounts]\ndefault = "a"\n'
                   '[[accounts.pool]]\nname = "a"\n[[accounts.pool]]\nname = "b"\n')
    monkeypatch.setenv(_config.ENV_CONFIG_PATH, str(cfg))


def test_register_self_writes_active(fresh_orchestrator_dir):
    result = register_self_impl(
        claude_sid="sid-1",
        agent="worker",
        name="rebase-bot",
        cwd="/tmp/work",
        iterm_sid="iterm-9",
    )
    assert result["ok"] is True
    record = state.read_json(paths.ACTIVE / "sid-1.json")
    assert record["name"] == "rebase-bot"
    assert record["agent"] == "worker"
    assert record["cwd"] == "/tmp/work"
    assert record["window_id"] == "iterm-9"
    assert isinstance(record["pid"], int)
    assert "started_at" in record

def test_register_self_default_pid_goes_through_the_cli_pid_funnel(
        fresh_orchestrator_dir, monkeypatch):
    from dockwright import mcp_server

    seen = []

    def _resolve(start):
        seen.append(start)
        return 987654

    monkeypatch.setattr(mcp_server, "resolve_cli_pid", _resolve)
    register_self_impl(claude_sid="sid-funnel", agent="worker", name="funnel-bot",
                       cwd="/x", iterm_sid="i1")
    assert seen == [os.getppid()], (
        "the default pid bypassed the cli-pid funnel, so a session whose MCP "
        "server is not a direct child of the CLI records the wrong pid")
    assert state.read_json(paths.ACTIVE / "sid-funnel.json")["pid"] == 987654


def test_register_self_default_pid_falls_back_when_the_funnel_finds_nothing(
        fresh_orchestrator_dir, monkeypatch):
    from dockwright import mcp_server

    monkeypatch.setattr(mcp_server, "resolve_cli_pid", lambda start: None)
    register_self_impl(claude_sid="sid-fallback", agent="worker", name="fallback-bot",
                       cwd="/x", iterm_sid="i1")
    assert state.read_json(paths.ACTIVE / "sid-fallback.json")["pid"] == os.getppid()


def test_register_self_duplicate_name_rejected(fresh_orchestrator_dir):
    register_self_impl(claude_sid="sid-1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    with pytest.raises(ValueError, match="name 'alpha' is taken"):
        register_self_impl(claude_sid="sid-2", agent="worker", name="alpha", cwd="/y", iterm_sid="i2")

def test_register_self_stamps_account_from_env(fresh_orchestrator_dir, monkeypatch):
    _install_two_pool(monkeypatch, fresh_orchestrator_dir)
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT", "b")
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    record = state.read_json(paths.ACTIVE / "w1.json")
    assert record["account"] == "b"

def test_register_self_preserves_hook_stamp_when_env_absent(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_ACCOUNT", raising=False)
    state.write_json_atomic(paths.ACTIVE / "mgr-1.json", {
        "claude_sid": "mgr-1", "agent": "manager", "name": "spry-walrus",
        "cwd": "/x", "window_id": "i1", "pid": os.getpid(),
        "started_at": time.time(), "state": "idle", "last_turn_at": None,
        "last_summary": None, "domain": "general", "parent_manager_name": None,
        "account": "a",
    })
    register_self_impl(claude_sid="mgr-1", agent="manager", name="spry-walrus",
                       cwd="/x", iterm_sid="i1", pid=os.getpid())
    record = state.read_json(paths.ACTIVE / "mgr-1.json")
    assert record["account"] == "a"

def test_register_self_preserves_started_at_on_reregistration(fresh_orchestrator_dir):
    state.write_json_atomic(paths.ACTIVE / "mgr-1.json", {
        "claude_sid": "mgr-1", "agent": "manager", "name": "spry-walrus",
        "cwd": "/x", "window_id": "i1", "pid": os.getpid(),
        "started_at": 1785000000.0, "state": "idle", "last_turn_at": None,
    })
    register_self_impl(claude_sid="mgr-1", agent="manager", name="manager",
                       cwd="/x", iterm_sid="i1")
    record = state.read_json(paths.ACTIVE / "mgr-1.json")
    assert record["started_at"] == 1785000000.0

def test_register_self_account_none_without_env_or_prior_record(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_ACCOUNT", raising=False)
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    assert state.read_json(paths.ACTIVE / "w1.json")["account"] is None
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT", "z")
    register_self_impl(claude_sid="w2", agent="worker", name="beta", cwd="/y", iterm_sid="i2")
    assert state.read_json(paths.ACTIVE / "w2.json")["account"] is None

def test_register_self_stamps_terminal_backend(fresh_orchestrator_dir):
    register_self_impl(claude_sid="m1", agent="manager", name="mgr-x", cwd="/x", iterm_sid="i1")
    rec = state.read_json(paths.ACTIVE / "m1.json")
    assert rec["terminal"] == "tmux"

def test_register_self_terminal_defaults_tmux(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w9", agent="worker", name="w-x", cwd="/x", iterm_sid="i1")
    rec = state.read_json(paths.ACTIVE / "w9.json")
    assert rec["terminal"] == "tmux"

import threading
from dockwright.mcp_server import (
    ask_manager_impl, answer_question_impl, list_pending_questions_impl,
)

def test_answer_unblocks_ask(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")

    async def run():
        task = _asyncio.create_task(ask_manager_impl(claude_sid="w1", question="ours or theirs?", poll_interval=0.05))
        await _asyncio.sleep(0.2)
        pending = list_pending_questions_impl()
        assert len(pending) == 1
        qid = pending[0]["question_id"]
        answer_question_impl(question_id=qid, text="ours")
        return await _asyncio.wait_for(task, timeout=2.0)

    assert _asyncio.run(run()) == "ours"


def test_list_pending_returns_oldest_first(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    register_self_impl(claude_sid="w2", agent="worker", name="beta", cwd="/y", iterm_sid="i2")
    from dockwright.mcp_server import _write_question
    _write_question(worker_sid="w1", worker_name="alpha", question="q1")
    time.sleep(0.05)
    _write_question(worker_sid="w2", worker_name="beta", question="q2")
    pending = list_pending_questions_impl()
    assert [p["question"] for p in pending] == ["q1", "q2"]


def test_write_question_routes_scoped_questions_to_parent_manager_dir(fresh_orchestrator_dir):
    from dockwright.mcp_server import _write_question

    qid = _write_question(
        worker_sid="w1",
        worker_name="alpha",
        question="scoped?",
        parent_manager_name="manager-a",
    )

    assert (paths.QUESTIONS / "manager-a" / f"{qid}.json").exists()
    assert not (paths.QUESTIONS / f"{qid}.json").exists()
    pending = list_pending_questions_impl(manager_name="manager-a")
    assert [q["question"] for q in pending] == ["scoped?"]


def test_answer_question_finds_scoped_question(fresh_orchestrator_dir):
    qid = "q-scoped"
    state.write_json_atomic(paths.QUESTIONS / "manager-a" / f"{qid}.json", {
        "question_id": qid,
        "worker_sid": "w1",
        "worker_name": "alpha",
        "parent_manager_name": "manager-a",
        "question": "scoped?",
        "asked_at": time.time(),
    })

    result = answer_question_impl(question_id=qid, text="yes")

    assert result["ok"] is True
    assert not (paths.QUESTIONS / "manager-a" / f"{qid}.json").exists()
    assert state.read_json(paths.ANSWERS / f"{qid}.json")["answer"] == "yes"


def test_legacy_flat_question_still_lists_answers_and_drops(fresh_orchestrator_dir):
    qid = "q-flat"
    state.write_json_atomic(paths.QUESTIONS / f"{qid}.json", {
        "question_id": qid,
        "worker_sid": "w1",
        "worker_name": "alpha",
        "parent_manager_name": None,
        "question": "legacy?",
        "asked_at": time.time(),
    })

    assert [q["question"] for q in list_pending_questions_impl()] == ["legacy?"]
    assert list_pending_questions_impl(manager_name="manager-a") == []
    answer_question_impl(question_id=qid, text="ok")
    assert not (paths.QUESTIONS / f"{qid}.json").exists()

    from dockwright.mcp_server import _drop_questions_for_worker
    state.write_json_atomic(paths.QUESTIONS / f"{qid}.json", {
        "question_id": qid,
        "worker_sid": "w1",
        "worker_name": "alpha",
        "parent_manager_name": None,
        "question": "drop me",
        "asked_at": time.time(),
    })
    assert _drop_questions_for_worker("w1") == 1
    assert not (paths.QUESTIONS / f"{qid}.json").exists()


def test_drop_questions_for_worker_removes_scoped_questions(fresh_orchestrator_dir):
    from dockwright.mcp_server import _drop_questions_for_worker
    state.write_json_atomic(paths.QUESTIONS / "manager-a" / "q1.json", {
        "question_id": "q1",
        "worker_sid": "w1",
        "worker_name": "alpha",
        "parent_manager_name": "manager-a",
        "question": "drop scoped",
        "asked_at": time.time(),
    })
    state.write_json_atomic(paths.QUESTIONS / "manager-b" / "q2.json", {
        "question_id": "q2",
        "worker_sid": "w2",
        "worker_name": "beta",
        "parent_manager_name": "manager-b",
        "question": "keep peer",
        "asked_at": time.time(),
    })

    assert _drop_questions_for_worker("w1") == 1

    assert not (paths.QUESTIONS / "manager-a" / "q1.json").exists()
    assert (paths.QUESTIONS / "manager-b" / "q2.json").exists()


from dockwright.mcp_server import (
    send_manager_to_worker_impl, kill_worker_impl, attach_existing_impl,
)
from dockwright import paths as paths_module

def test_send_manager_to_worker_types_content(fresh_orchestrator_dir, monkeypatch):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="42")
    typed = []
    monkeypatch.setattr(
        "dockwright.mcp_server._type_unguarded",
        lambda wid, text: typed.append((wid, text)),
    )
    result = send_manager_to_worker_impl(worker="alpha", text="also check Y")
    assert result["status"] == "delivered" and result["worker"] == "alpha"
    assert typed == [("42", "[MANAGER] also check Y")]

def test_send_manager_to_worker_marker_prepends_once_multiline(fresh_orchestrator_dir, monkeypatch):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="42")
    typed = []
    monkeypatch.setattr(
        "dockwright.mcp_server._type_unguarded",
        lambda wid, text: typed.append(text),
    )
    send_manager_to_worker_impl(worker="alpha", text="line one\nline two")
    assert typed == ["[MANAGER] line one\nline two"]
    assert typed[0].count("[MANAGER] ") == 1

def test_send_manager_to_worker_unknown_worker(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="no worker named 'ghost'"):
        send_manager_to_worker_impl(worker="ghost", text="hi")

def test_send_manager_to_worker_resolves_via_terminal_ls_when_id_empty(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv
    state.write_json_atomic(paths.ACTIVE / "w1.json", {
        "claude_sid": "w1", "agent": "worker", "name": "alpha",
        "cwd": "/tmp/wt", "window_id": "", "runtime": "claude"})
    monkeypatch.setattr(srv, "_terminal_ls", lambda: [
        {"tabs": [{"windows": [
            {"id": 555, "cwd": "/tmp/wt",
             "foreground_processes": [{"cmdline": ["node", "/x/claude", "--resume"]}]}]}]}])
    sent = {}
    monkeypatch.setattr(srv, "_type_unguarded", lambda wid, txt: sent.update(wid=wid, txt=txt))
    monkeypatch.setattr(srv, "_WINDOW_RESOLVE_RETRY_SLEEP", 0)
    result = srv.send_manager_to_worker_impl("alpha", "hi")
    assert result["status"] == "delivered" and sent["wid"] == "555"
    assert state.read_json(paths.ACTIVE / "w1.json")["window_id"] == "555"

def test_send_manager_to_worker_persisted_id_confirmed_live(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv
    state.write_json_atomic(paths.ACTIVE / "w1.json", {
        "claude_sid": "w1", "agent": "worker", "name": "alpha",
        "cwd": "/tmp/wt", "window_id": "555", "runtime": "claude"})
    monkeypatch.setattr(srv, "_terminal_ls", lambda: [
        {"tabs": [{"windows": [
            {"id": 555, "cwd": "/somewhere/else", "foreground_processes": []}]}]}])
    sent = {}
    monkeypatch.setattr(srv, "_type_unguarded", lambda wid, txt: sent.update(wid=wid))
    result = srv.send_manager_to_worker_impl("alpha", "hi")
    assert result["status"] == "delivered" and sent["wid"] == "555"

def test_send_manager_to_worker_no_live_window_raises_loud(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv
    state.write_json_atomic(paths.ACTIVE / "w1.json", {
        "claude_sid": "w1", "agent": "worker", "name": "alpha",
        "cwd": "/tmp/wt", "window_id": "", "runtime": "claude"})
    monkeypatch.setattr(srv, "_terminal_ls", lambda: [])
    monkeypatch.setattr(srv, "_WINDOW_RESOLVE_RETRY_SLEEP", 0)
    with pytest.raises(ValueError, match="no live window"):
        srv.send_manager_to_worker_impl("alpha", "hi")

def test_send_manager_to_worker_ambiguous_cwd_match_raises(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv
    state.write_json_atomic(paths.ACTIVE / "w1.json", {
        "claude_sid": "w1", "agent": "worker", "name": "alpha",
        "cwd": "/tmp/wt", "window_id": "", "runtime": "claude"})
    monkeypatch.setattr(srv, "_terminal_ls", lambda: [
        {"tabs": [{"windows": [
            {"id": 1, "cwd": "/tmp/wt", "foreground_processes": [{"cmdline": ["claude"]}]},
            {"id": 2, "cwd": "/tmp/wt", "foreground_processes": [{"cmdline": ["claude"]}]}]}]}])
    monkeypatch.setattr(srv, "_WINDOW_RESOLVE_RETRY_SLEEP", 0)
    with pytest.raises(ValueError, match="no live window"):
        srv.send_manager_to_worker_impl("alpha", "hi")

def test_send_manager_to_worker_swallows_terminal_failure(fresh_orchestrator_dir, monkeypatch):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="42")

    def boom(args, **kw):
        raise FileNotFoundError("tmux not installed")

    monkeypatch.setattr("dockwright.mcp_server.subprocess.run", boom)
    result = send_manager_to_worker_impl(worker="alpha", text="hi")
    assert result["status"] == "delivered"

def test_kill_worker_marks_terminating(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=12345)
    result = kill_worker_impl(worker="alpha", dry_run=True)
    assert result["would_kill"] == 12345
    assert result["iterm_sid"] == "i1"

def test_attach_existing_returns_workers_and_questions(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    from dockwright.mcp_server import _write_question
    _write_question(worker_sid="w1", worker_name="alpha", question="urgent?")
    result = attach_existing_impl()
    assert len(result["workers"]) == 1
    assert result["workers"][0]["name"] == "alpha"
    assert len(result["orphan_questions"]) == 1
    assert result["orphan_questions"][0]["question"] == "urgent?"

from dockwright.mcp_server import become_manager_impl

def test_become_manager_rolls_funny_name_and_default_domain(fresh_orchestrator_dir):
    result = become_manager_impl(claude_sid="mgr-1", iterm_sid="i9")
    assert result["ok"] is True
    assert "-" in result["name"]
    assert result["domain"] == "general"
    assert result["runtime"] == "claude"
    record = state.read_json(paths.ACTIVE / "mgr-1.json")
    assert record["agent"] == "manager"
    assert record["name"] == result["name"]
    assert record["domain"] == "general"
    assert record["runtime"] == "claude"


def test_become_manager_records_claude_runtime_and_list_managers_exposes_it(fresh_orchestrator_dir):
    result = become_manager_impl(claude_sid="mgr-1", iterm_sid="i9")
    assert result["runtime"] == "claude"
    record = state.read_json(paths.ACTIVE / "mgr-1.json")
    assert record["runtime"] == "claude"

    from dockwright.mcp_server import list_managers
    managers = list_managers()
    assert managers[0]["claude_sid"] == "mgr-1"
    assert managers[0]["runtime"] == "claude"


def test_become_manager_returns_preflight_and_paints(monkeypatch, fresh_orchestrator_dir):
    from dockwright import mcp_server
    calls = {}
    monkeypatch.setattr(mcp_server, "_paint_manager_tab",
                        lambda name, domain: calls.setdefault("paint", (name, domain)))
    monkeypatch.setattr(mcp_server, "_run_preflight_cleanup", lambda: "pruned 2 husks")
    result = mcp_server.become_manager_impl("sid-bm-1", iterm_sid="%5")
    assert result["ok"] is True
    assert result["preflight"] == "pruned 2 husks"
    assert calls["paint"] == (result["name"], result["domain"])


def test_become_manager_paint_preflight_failures_do_not_fail_registration(monkeypatch, fresh_orchestrator_dir):
    from dockwright import mcp_server
    def boom(*a, **k):
        raise RuntimeError("no tmux")
    monkeypatch.setattr(mcp_server, "_paint_manager_tab", boom)
    monkeypatch.setattr(mcp_server, "_run_preflight_cleanup", boom)
    result = mcp_server.become_manager_impl("sid-bm-2", iterm_sid="%5")
    assert result["ok"] is True

def test_prune_stale_active_records_keeps_non_positive_pid(fresh_orchestrator_dir):
    from dockwright.mcp_server import _prune_stale_active_records
    state.write_json_atomic(paths.ACTIVE / "sid-zero.json", {
        "claude_sid": "sid-zero", "agent": "manager", "name": "odd-hydra", "pid": 0,
    })
    state.write_json_atomic(paths.ACTIVE / "sid-neg.json", {
        "claude_sid": "sid-neg", "agent": "worker", "name": "odd-newt", "pid": -5,
    })

    _prune_stale_active_records()

    assert (paths.ACTIVE / "sid-zero.json").exists()
    assert (paths.ACTIVE / "sid-neg.json").exists()


def test_prune_stale_active_records_keeps_pid_beyond_os_range(fresh_orchestrator_dir):
    from dockwright.mcp_server import _prune_stale_active_records
    state.write_json_atomic(paths.ACTIVE / "sid-huge.json", {
        "claude_sid": "sid-huge", "agent": "manager", "name": "huge-golem", "pid": 2**31,
    })

    _prune_stale_active_records()

    assert (paths.ACTIVE / "sid-huge.json").exists()


def test_prune_stale_active_records_ledgers_spend(fresh_orchestrator_dir, monkeypatch):
    import json
    from dockwright.mcp_server import _prune_stale_active_records
    monkeypatch.setattr(paths, "SPEND_LEDGER", fresh_orchestrator_dir / "spend-ledger.jsonl")
    state.write_json_atomic(paths.ACTIVE / "dead.json", {
        "claude_sid": "dead", "agent": "worker", "name": "gone", "pid": 1,
        "nested": True,
        "spend": {"turns": 1, "out_tokens": 5, "in_tokens": 1, "cache_read_tokens": 2},
    })
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: False)

    _prune_stale_active_records()

    assert not (paths.ACTIVE / "dead.json").exists()
    entry = json.loads((fresh_orchestrator_dir / "spend-ledger.jsonl").read_text())
    assert entry["sid"] == "dead"
    assert entry["source"] == "prune"
    assert entry["agent"] == "nested"


def test_become_manager_allows_multiple_managers(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)
    pids = iter([1001, 1002])
    monkeypatch.setattr("dockwright.mcp_server.os.getppid", lambda: next(pids))
    r1 = become_manager_impl(claude_sid="mgr-1", iterm_sid="i9", domain="general")
    r2 = become_manager_impl(claude_sid="mgr-2", iterm_sid="i10", domain="dlq")
    assert r1["ok"] is True
    assert r2["ok"] is True
    assert r1["name"] != r2["name"]
    assert r1["domain"] == "general"
    assert r2["domain"] == "dlq"
    assert state.read_json(paths.ACTIVE / "mgr-1.json") is not None
    assert state.read_json(paths.ACTIVE / "mgr-2.json") is not None


def test_become_manager_prunes_same_pid_ghost(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.mcp_server.os.getppid", lambda: 4242)
    state.write_json_atomic(paths.ACTIVE / "manager-session.json", {
        "claude_sid": "manager-session",
        "agent": "manager",
        "name": "manager",
        "window_id": "i9",
        "pid": 4242,
    })
    real = become_manager_impl(claude_sid="mgr-real", iterm_sid="i9", domain="general")
    assert real["ok"] is True
    assert state.read_json(paths.ACTIVE / "manager-session.json") is None
    assert state.read_json(paths.ACTIVE / "mgr-real.json") is not None
    managers = [r for r in state.list_json_in(paths.ACTIVE) if r.get("agent") == "manager"]
    assert len(managers) == 1
    assert managers[0]["claude_sid"] == "mgr-real"


def test_become_manager_prunes_funny_named_bootstrap_ghost(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.mcp_server.os.getppid", lambda: 4242)
    state.write_json_atomic(paths.ACTIVE / "manager-session.json", {
        "claude_sid": "manager-session",
        "agent": "manager",
        "name": "snug-ibex",
        "window_id": "i9",
        "pid": 4242,
    })
    real = become_manager_impl(claude_sid="mgr-real", iterm_sid="i9", domain="general")
    assert real["ok"] is True
    assert state.read_json(paths.ACTIVE / "manager-session.json") is None
    assert state.read_json(paths.ACTIVE / "mgr-real.json") is not None
    managers = [r for r in state.list_json_in(paths.ACTIVE) if r.get("agent") == "manager"]
    assert len(managers) == 1
    assert managers[0]["claude_sid"] == "mgr-real"


def test_become_manager_tool_exposes_optional_name():
    import inspect
    from dockwright.mcp_server import become_manager

    assert inspect.signature(become_manager).parameters["name"].default is None


def test_become_manager_tool_forwards_name_for_in_place_reboot(fresh_orchestrator_dir, monkeypatch):
    from dockwright.mcp_server import become_manager

    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.mcp_server.os.getppid", lambda: 4242)
    state.write_json_atomic(paths.ACTIVE / "mgr-pre-clear.json", {
        "claude_sid": "mgr-pre-clear",
        "agent": "manager",
        "name": "kept-fox",
        "domain": "general",
        "window_id": "i9",
        "pid": 4242,
    })
    result = become_manager(claude_sid="mgr-post-clear", iterm_sid="i9",
                            domain="general", name="kept-fox")
    assert result["ok"] is True
    assert result["name"] == "kept-fox"
    assert state.read_json(paths.ACTIVE / "mgr-pre-clear.json") is None
    record = state.read_json(paths.ACTIVE / "mgr-post-clear.json")
    assert record["name"] == "kept-fox"
    managers = [r for r in state.list_json_in(paths.ACTIVE) if r.get("agent") == "manager"]
    assert len(managers) == 1


def test_become_manager_tool_without_name_still_rolls_funny_name(fresh_orchestrator_dir, monkeypatch):
    from dockwright.mcp_server import become_manager

    monkeypatch.setattr("dockwright.mcp_server.os.getppid", lambda: 4242)
    result = become_manager(claude_sid="mgr-1", iterm_sid="i9")
    assert result["ok"] is True
    assert "-" in result["name"]


def test_become_manager_tool_suffixes_name_taken_by_different_live_session(fresh_orchestrator_dir, monkeypatch):
    from dockwright.mcp_server import become_manager

    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.mcp_server.os.getppid", lambda: 4242)
    state.write_json_atomic(paths.ACTIVE / "peer-mgr.json", {
        "claude_sid": "peer-mgr",
        "agent": "manager",
        "name": "kept-fox",
        "domain": "general",
        "window_id": "other-window",
        "pid": 9999,
    })
    result = become_manager(claude_sid="mgr-new", iterm_sid="i9",
                            domain="general", name="kept-fox")
    assert result["ok"] is True
    assert result["name"] != "kept-fox"
    assert result["name"].startswith("kept-fox")
    assert state.read_json(paths.ACTIVE / "peer-mgr.json") is not None


def test_prune_same_pid_ghosts_drops_same_window_placeholder_regardless_of_name(fresh_orchestrator_dir):
    from dockwright.mcp_server import _prune_same_pid_ghosts
    paths.ensure_dirs()
    state.write_json_atomic(paths.ACTIVE / "bootstrap.json", {
        "claude_sid": "bootstrap",
        "agent": "manager",
        "name": "manager",
        "window_id": "current-window",
        "pid": 7777,
    })
    state.write_json_atomic(paths.ACTIVE / "bootstrap-suffixed.json", {
        "claude_sid": "bootstrap-suffixed",
        "agent": "manager",
        "name": "manager-2",
        "window_id": "current-window",
        "pid": 7777,
    })
    state.write_json_atomic(paths.ACTIVE / "funny-same-window.json", {
        "claude_sid": "funny-same-window",
        "agent": "manager",
        "name": "snug-ibex",
        "window_id": "current-window",
        "pid": 7777,
    })
    state.write_json_atomic(paths.ACTIVE / "live-peer.json", {
        "claude_sid": "live-peer",
        "agent": "manager",
        "name": "spry-walrus",
        "window_id": "peer-window",
        "pid": 7777,
    })
    state.write_json_atomic(paths.ACTIVE / "keep.json", {
        "claude_sid": "keep",
        "agent": "manager",
        "name": "new-manager",
        "window_id": "current-window",
        "pid": 7777,
    })
    state.write_json_atomic(paths.ACTIVE / "other.json", {"claude_sid": "other", "name": "gamma", "pid": 8888})
    state.write_json_atomic(paths.ACTIVE / "legacy.json", {"claude_sid": "legacy", "name": "delta", "pid": None})

    _prune_same_pid_ghosts(7777, keep_sid="keep", keep_window_id="current-window")

    assert state.read_json(paths.ACTIVE / "bootstrap.json") is None
    assert state.read_json(paths.ACTIVE / "bootstrap-suffixed.json") is None
    assert state.read_json(paths.ACTIVE / "funny-same-window.json") is None
    assert state.read_json(paths.ACTIVE / "live-peer.json") is not None
    assert state.read_json(paths.ACTIVE / "keep.json") is not None
    assert state.read_json(paths.ACTIVE / "other.json") is not None
    assert state.read_json(paths.ACTIVE / "legacy.json") is not None

def test_kill_worker_drops_pending_questions(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=12345)
    from dockwright.mcp_server import _write_question
    _write_question(worker_sid="w1", worker_name="alpha", question="q1")
    _write_question(worker_sid="w1", worker_name="alpha", question="q2")
    assert len(list(paths.QUESTIONS.iterdir())) == 2
    result = kill_worker_impl(worker="alpha", dry_run=True)
    assert len(list(paths.QUESTIONS.iterdir())) == 2
    from dockwright.mcp_server import _drop_questions_for_worker
    dropped = _drop_questions_for_worker("w1")
    assert dropped == 2
    assert len(list(paths.QUESTIONS.iterdir())) == 0

def test_ask_manager_unlinks_answer_after_read(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")

    async def run():
        task = _asyncio.create_task(ask_manager_impl(claude_sid="w1", question="ours or theirs?", poll_interval=0.01))
        await _asyncio.sleep(0.1)
        pending = list_pending_questions_impl()
        qid = pending[0]["question_id"]
        answer_question_impl(question_id=qid, text="ours")
        answer = await _asyncio.wait_for(task, timeout=2.0)
        return qid, answer

    qid, answer = _asyncio.run(run())
    assert answer == "ours"
    assert not (paths.ANSWERS / f"{qid}.json").exists()

def test_answer_question_unknown_qid_raises(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="no pending question"):
        answer_question_impl(question_id="nonexistent", text="x")

def test_list_workers_marks_alive_true_for_live_pid(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    workers = list_workers_impl()
    assert workers[0]["alive"] is True


def test_list_workers_exposes_runtime_with_legacy_default(fresh_orchestrator_dir):
    register_self_impl(
        claude_sid="w-codex",
        agent="worker",
        name="codex-worker",
        cwd="/x",
        iterm_sid="i1",
        pid=os.getpid(),
        runtime="codex",
    )
    state.write_json_atomic(paths.ACTIVE / "legacy.json", {
        "claude_sid": "legacy",
        "agent": "worker",
        "name": "legacy-worker",
        "cwd": "/x",
        "window_id": "i2",
        "pid": os.getpid(),
        "started_at": 0,
    })
    workers = {worker["name"]: worker for worker in list_workers_impl()}
    assert workers["codex-worker"]["runtime"] == "codex"
    assert workers["legacy-worker"]["runtime"] == "claude"


def test_list_workers_excludes_manager_records(fresh_orchestrator_dir):
    register_self_impl(claude_sid="mgr", agent="manager", name="manager", cwd="/x", iterm_sid="i0", pid=os.getpid())
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    workers = list_workers_impl()
    assert len(workers) == 1
    assert workers[0]["name"] == "alpha"

def test_attach_existing_enriches_workers_with_alive_and_transcript_fields(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    result = attach_existing_impl()
    assert len(result["workers"]) == 1
    worker = result["workers"][0]
    assert worker["name"] == "alpha"
    assert worker["alive"] is True
    assert "last_summary" in worker
    assert "last_turn_at" in worker

def test_attach_existing_excludes_manager_records(fresh_orchestrator_dir):
    register_self_impl(claude_sid="mgr", agent="manager", name="manager", cwd="/x", iterm_sid="i0", pid=os.getpid())
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    result = attach_existing_impl()
    assert len(result["workers"]) == 1
    assert result["workers"][0]["name"] == "alpha"

from dockwright.mcp_server import _resolve_unique_name

def test_resolve_unique_name_returns_base_when_free(fresh_orchestrator_dir):
    assert _resolve_unique_name("alpha") == "alpha"

def test_resolve_unique_name_appends_suffix_when_taken(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    assert _resolve_unique_name("alpha") == "alpha-2"

def test_resolve_unique_name_finds_next_free_suffix(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    register_self_impl(claude_sid="w2", agent="worker", name="alpha-2", cwd="/x", iterm_sid="i2")
    assert _resolve_unique_name("alpha") == "alpha-3"

def test_resolve_unique_name_excluding_sid_treats_own_record_as_free(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    assert _resolve_unique_name("alpha", excluding_sid="w1") == "alpha"

def test_ask_manager_recovers_from_corrupt_answer_file(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")

    async def run():
        task = _asyncio.create_task(ask_manager_impl(claude_sid="w1", question="?", poll_interval=0.02))
        await _asyncio.sleep(0.1)
        pending = list_pending_questions_impl()
        qid = pending[0]["question_id"]
        (paths_module.ANSWERS / f"{qid}.json").write_text("{not json")
        await _asyncio.sleep(0.1)
        answer_question_impl(question_id=qid, text="real answer")
        return await _asyncio.wait_for(task, timeout=2.0)

    assert _asyncio.run(run()) == "real answer"

def test_ask_manager_is_async(fresh_orchestrator_dir):
    from dockwright import mcp_server
    assert inspect.iscoroutinefunction(mcp_server.ask_manager_impl)
    assert inspect.iscoroutinefunction(mcp_server.ask_manager)

def test_ask_manager_does_not_starve_event_loop(fresh_orchestrator_dir):
    assert inspect.iscoroutinefunction(ask_manager_impl)

    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    register_self_impl(claude_sid="w2", agent="worker", name="beta", cwd="/y", iterm_sid="i2")
    from dockwright.mcp_server import worker_done_impl

    async def run():
        ask = _asyncio.create_task(ask_manager_impl(claude_sid="w1", question="blocked?", poll_interval=0.02))
        await _asyncio.sleep(0.1)
        assert not ask.done()
        done = worker_done_impl("w2", "victim tool completes while ask_manager waits")
        assert done["ok"] is True
        assert list(paths.DONE.rglob("*.json"))
        assert not ask.done()
        pending = list_pending_questions_impl()
        answer_question_impl(question_id=pending[0]["question_id"], text="unblocked")
        return await _asyncio.wait_for(ask, timeout=2.0)

    assert _asyncio.run(run()) == "unblocked"

def test_ask_manager_timeout_returns_reask_sentinel(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    result = _asyncio.run(ask_manager_impl(
        claude_sid="w1", question="anyone there?", poll_interval=0.01, timeout_sec=0.05))
    assert result.startswith("NO_ANSWER_YET:")
    pending = list_pending_questions_impl()
    assert len(pending) == 1
    assert pending[0]["question_id"] in result


def test_ask_manager_resume_reattaches_without_duplicate_question(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    sentinel = _asyncio.run(ask_manager_impl(
        claude_sid="w1", question="q?", poll_interval=0.01, timeout_sec=0.05))
    qid = list_pending_questions_impl()[0]["question_id"]
    assert qid in sentinel

    async def resume():
        task = _asyncio.create_task(ask_manager_impl(
            claude_sid="w1", question="q?", poll_interval=0.02, resume_question_id=qid))
        await _asyncio.sleep(0.1)
        assert not task.done()
        assert len(list_pending_questions_impl()) == 1
        answer_question_impl(question_id=qid, text="finally")
        return await _asyncio.wait_for(task, timeout=2.0)

    assert _asyncio.run(resume()) == "finally"
    assert len(list_pending_questions_impl()) == 0


def test_ask_manager_resume_returns_answer_written_while_away(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    _asyncio.run(ask_manager_impl(
        claude_sid="w1", question="q?", poll_interval=0.01, timeout_sec=0.05))
    qid = list_pending_questions_impl()[0]["question_id"]
    answer_question_impl(question_id=qid, text="answered while away")
    result = _asyncio.run(ask_manager_impl(
        claude_sid="w1", question="q?", poll_interval=0.01, resume_question_id=qid))
    assert result == "answered while away"
    assert not (paths.ANSWERS / f"{qid}.json").exists()


def test_ask_manager_resume_unknown_qid_raises(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    with pytest.raises(ValueError, match="no pending question or answer"):
        _asyncio.run(ask_manager_impl(
            claude_sid="w1", question="q?", poll_interval=0.01, resume_question_id="deadbeef"))


def test_ask_manager_resume_foreign_question_raises(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    register_self_impl(claude_sid="w2", agent="worker", name="beta", cwd="/y", iterm_sid="i2")
    from dockwright.mcp_server import _write_question
    qid = _write_question(worker_sid="w2", worker_name="beta", question="theirs")
    with pytest.raises(ValueError, match="another worker"):
        _asyncio.run(ask_manager_impl(
            claude_sid="w1", question="q?", poll_interval=0.01, resume_question_id=qid))
    assert len(list_pending_questions_impl()) == 1


def test_ask_manager_resume_unregistered_sid_raises(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="not registered"):
        _asyncio.run(ask_manager_impl(
            claude_sid="ghost", question="q?", poll_interval=0.01, resume_question_id="whatever"))


def test_answer_question_stamps_worker_sid(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    from dockwright.mcp_server import _write_question
    qid = _write_question(worker_sid="w1", worker_name="alpha", question="q?")
    answer_question_impl(question_id=qid, text="ans")
    data = state.read_json(paths.ANSWERS / f"{qid}.json")
    assert data["worker_sid"] == "w1"


def test_answer_question_unreadable_question_record_writes_unstamped(fresh_orchestrator_dir):
    qid = "q-corrupt"
    paths.QUESTIONS.mkdir(parents=True, exist_ok=True)
    (paths.QUESTIONS / f"{qid}.json").write_text("{not json")
    result = answer_question_impl(question_id=qid, text="still delivered")
    assert result == {"ok": True}
    data = state.read_json(paths.ANSWERS / f"{qid}.json")
    assert data["answer"] == "still delivered"
    assert "worker_sid" not in data
    assert not (paths.QUESTIONS / f"{qid}.json").exists()


def test_ask_manager_resume_foreign_stamped_answer_raises_and_preserves(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    qid = "q-foreign"
    state.write_json_atomic(paths.ANSWERS / f"{qid}.json", {
        "question_id": qid, "answer": "not yours", "worker_sid": "w2", "answered_at": time.time(),
    })
    with pytest.raises(ValueError, match="another worker"):
        _asyncio.run(ask_manager_impl(
            claude_sid="w1", question="q?", poll_interval=0.01, resume_question_id=qid))
    assert (paths.ANSWERS / f"{qid}.json").exists()


def test_ask_manager_resume_accepts_legacy_unstamped_answer(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    qid = "q-legacy"
    state.write_json_atomic(paths.ANSWERS / f"{qid}.json", {
        "question_id": qid, "answer": "old-style", "answered_at": time.time(),
    })
    result = _asyncio.run(ask_manager_impl(
        claude_sid="w1", question="q?", poll_interval=0.01, resume_question_id=qid))
    assert result == "old-style"


def test_ask_manager_resume_toctou_recheck_finds_answer(fresh_orchestrator_dir, monkeypatch):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    from dockwright import mcp_server
    from dockwright.mcp_server import _write_question
    qid = _write_question(worker_sid="w1", worker_name="alpha", question="q?")
    real = mcp_server._try_consume_answer
    calls = {"n": 0}

    def racy(q, sid):
        calls["n"] += 1
        if calls["n"] == 1:
            answer_question_impl(question_id=qid, text="landed mid-window")
            return None
        return real(q, sid)

    monkeypatch.setattr(mcp_server, "_try_consume_answer", racy)
    result = _asyncio.run(mcp_server.ask_manager_impl(
        claude_sid="w1", question="q?", poll_interval=0.01, resume_question_id=qid))
    assert result == "landed mid-window"


def test_register_self_name_collision_with_dead_pid_succeeds(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=99999999)
    result = register_self_impl(claude_sid="w2", agent="worker", name="alpha", cwd="/y", iterm_sid="i2", pid=os.getpid())
    assert result["ok"] is True
    assert not (paths.ACTIVE / "w1.json").exists()
    assert (paths.ACTIVE / "w2.json").exists()

def test_become_manager_stale_record_with_dead_pid_succeeds(fresh_orchestrator_dir):
    register_self_impl(claude_sid="old-mgr", agent="manager", name="manager", cwd="/x", iterm_sid="i0", pid=99999999)
    result = become_manager_impl(claude_sid="new-mgr", iterm_sid="i1")
    assert result["ok"] is True
    assert not (paths.ACTIVE / "old-mgr.json").exists()
    assert (paths.ACTIVE / "new-mgr.json").exists()

def test_resolve_unique_name_skips_dead_records(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=99999999)
    assert _resolve_unique_name("alpha") == "alpha"

def test_resolve_unique_name_avoids_funny_name_collision(fresh_orchestrator_dir):
    state.write_json_atomic(paths.ACTIVE / "w1.json", {
        "claude_sid": "w1", "agent": "worker", "name": "task-x",
        "funny_name": "alpha", "window_id": "i1", "pid": os.getpid(),
        "started_at": time.time(), "state": "idle",
    })
    assert _resolve_unique_name("alpha") == "alpha-2"

def test_list_workers_prunes_dead_workers(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w-alive", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    register_self_impl(claude_sid="w-dead", agent="worker", name="beta", cwd="/y", iterm_sid="i2", pid=99999999)
    workers = list_workers_impl()
    assert len(workers) == 1
    assert workers[0]["name"] == "alpha"
    assert not (paths.ACTIVE / "w-dead.json").exists()

def _stamp_delegating_tree(home, sid, *, agent_age_sec=5, log_age_sec=60):
    project_dir = home / ".claude" / "projects" / "-Users-test"
    project_dir.mkdir(parents=True, exist_ok=True)
    log = project_dir / f"{sid}.jsonl"
    log.write_text("")
    now = time.time()
    os.utime(log, (now - log_age_sec, now - log_age_sec))
    subagents = project_dir / sid / "subagents"
    subagents.mkdir(parents=True, exist_ok=True)
    agent = subagents / "agent-aaa.jsonl"
    agent.write_text("{}")
    os.utime(agent, (now - agent_age_sec, now - agent_age_sec))


def test_list_workers_reports_delegating_idle_worker_as_processing(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    register_self_impl(claude_sid="w-del", agent="worker", name="delegator",
                       cwd="/x", iterm_sid="i1", pid=os.getpid())
    _stamp_delegating_tree(tmp_path, "w-del")
    worker = next(w for w in list_workers_impl() if w["name"] == "delegator")
    assert worker["state"] == "processing"
    assert worker["delegating"] is True
    assert state.read_json(paths.ACTIVE / "w-del.json")["state"] == "idle"


def test_list_workers_reports_a_worker_with_a_live_child_as_delegating(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    _stamp_delegating_tree(tmp_path, "w1", agent_age_sec=5000, log_age_sec=10)
    state.write_json_atomic(paths.ACTIVE / "c1.json", {
        "claude_sid": "c1", "agent": "worker", "kind": "subworker", "name": "alpha-review",
        "parent_name": "alpha", "pid": os.getpid(), "runtime": "codex", "state": "processing"})
    worker = next(w for w in list_workers_impl() if w["name"] == "alpha")
    assert worker["state"] == "processing" and worker["delegating"] is True


def test_list_workers_reports_a_live_child_parent_with_no_transcript_as_delegating(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    state.write_json_atomic(paths.ACTIVE / "c1.json", {
        "claude_sid": "c1", "agent": "worker", "kind": "subworker", "name": "alpha-review",
        "parent_name": "alpha", "pid": os.getpid(), "runtime": "codex", "state": "processing"})
    from dockwright.transcript import find_session_log
    assert find_session_log("w1") is None
    worker = next(w for w in list_workers_impl() if w["name"] == "alpha")
    assert worker["state"] == "processing" and worker["delegating"] is True


def test_list_workers_keeps_true_idle_as_idle(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    register_self_impl(claude_sid="w-idle", agent="worker", name="resting",
                       cwd="/x", iterm_sid="i1", pid=os.getpid())
    worker = next(w for w in list_workers_impl() if w["name"] == "resting")
    assert worker["state"] == "idle"
    assert "delegating" not in worker


def test_list_workers_reads_an_unknown_pid_as_alive(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    state.write_json_atomic(paths.ACTIVE / "c1.json", {
        "claude_sid": "c1", "agent": "worker", "kind": "subworker", "name": "alpha-review",
        "parent_name": "alpha", "runtime": "codex", "pid": None})
    state.write_json_atomic(paths.ACTIVE / "c2.json", {
        "claude_sid": "c2", "agent": "worker", "kind": "subworker", "name": "bravo-review",
        "parent_name": "bravo", "runtime": "codex"})
    workers = {w["name"]: w for w in list_workers_impl()}
    assert workers["alpha-review"]["alive"] is True
    assert workers["bravo-review"]["alive"] is True


def test_list_workers_reads_a_dead_pid_as_not_alive(fresh_orchestrator_dir, tmp_path, monkeypatch):
    from dockwright import registry
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(registry, "_live_pane_ids", lambda: {"%20"})
    state.write_json_atomic(paths.ACTIVE / "c1.json", {
        "claude_sid": "c1", "agent": "worker", "kind": "subworker", "name": "alpha-review",
        "parent_name": "alpha", "runtime": "codex", "window_id": "%20", "pid": 2 ** 22 + 7})
    worker = next(w for w in list_workers_impl() if w["name"] == "alpha-review")
    assert worker["alive"] is False


def test_list_workers_skips_delegation_check_for_processing_worker(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    register_self_impl(claude_sid="w-proc", agent="worker", name="midturn",
                       cwd="/x", iterm_sid="i1", pid=os.getpid())
    record = state.read_json(paths.ACTIVE / "w-proc.json")
    record["state"] = "processing"
    state.write_json_atomic(paths.ACTIVE / "w-proc.json", record)
    _stamp_delegating_tree(tmp_path, "w-proc")
    worker = next(w for w in list_workers_impl() if w["name"] == "midturn")
    assert worker["state"] == "processing"
    assert "delegating" not in worker

import json
from dockwright.mcp_server import get_worker_summary_impl, get_worker_tail_impl

def _write_fake_transcript(tmp_path, monkeypatch, sid, lines):
    monkeypatch.setenv("HOME", str(tmp_path))
    projects = tmp_path / ".claude" / "projects" / "-Users-x"
    projects.mkdir(parents=True)
    log = projects / f"{sid}.jsonl"
    log.write_text("\n".join(json.dumps(l) for l in lines))
    return log

def test_get_worker_summary_returns_full_text(fresh_orchestrator_dir, tmp_path, monkeypatch):
    long_text = "x" * 1500
    _write_fake_transcript(tmp_path, monkeypatch, "w1", [
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": long_text}]},
         "timestamp": "2026-05-18T10:00:00Z"},
    ])
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    result = get_worker_summary_impl(worker="alpha")
    assert result["name"] == "alpha"
    assert result["summary"] == long_text
    assert result["source"] == "transcript"
    assert result["last_turn_at"] == "2026-05-18T10:00:00Z"
    assert result["alive"] is True

def test_get_worker_summary_missing_log(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    result = get_worker_summary_impl(worker="alpha")
    assert result["error"] == "transcript not found"
    assert result["source"] == "transcript"
    assert result["summary"] is None
    assert result["last_turn_at"] is None

def test_get_worker_summary_unknown_worker(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="no worker named 'ghost'"):
        get_worker_summary_impl(worker="ghost")

def test_get_worker_summary_reads_an_unknown_pid_as_alive(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    state.write_json_atomic(paths.ACTIVE / "c1.json", {
        "claude_sid": "c1", "agent": "worker", "kind": "subworker", "name": "alpha-review",
        "parent_name": "alpha", "runtime": "codex", "pid": None})
    state.write_json_atomic(paths.ACTIVE / "c2.json", {
        "claude_sid": "c2", "agent": "worker", "kind": "subworker", "name": "bravo-review",
        "parent_name": "bravo", "runtime": "codex"})
    assert get_worker_summary_impl(worker="alpha-review")["alive"] is True
    assert get_worker_summary_impl(worker="bravo-review")["alive"] is True

def test_get_worker_summary_reads_a_dead_pid_as_not_alive(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    state.write_json_atomic(paths.ACTIVE / "c1.json", {
        "claude_sid": "c1", "agent": "worker", "kind": "subworker", "name": "alpha-review",
        "parent_name": "alpha", "runtime": "codex", "pid": 2 ** 22 + 7})
    assert get_worker_summary_impl(worker="alpha-review")["alive"] is False

_INTERIM_LINE = "Card 3: one comment. Next, cards 5 and 6."
_INTERIM_TS = "2026-11-02T16:42:20Z"

def _register_alpha_with_an_interim_line(tmp_path, monkeypatch):
    _write_fake_transcript(tmp_path, monkeypatch, "w1", [
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": _INTERIM_LINE}]},
         "timestamp": _INTERIM_TS},
    ])
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())

def _write_done(sid, event_id, summary, completed_at, bucket="alpha"):
    done_dir = paths.done_dir_for(bucket)
    done_dir.mkdir(parents=True, exist_ok=True)
    event = {"event_id": event_id, "claude_sid": sid, "worker_name": "alpha", "summary": summary}
    if completed_at is not None:
        event["completed_at"] = completed_at
    state.write_json_atomic(done_dir / f"{sid}-{event_id}.json", event)

def test_get_worker_summary_returns_the_worker_done_report_over_an_interim_line(
        fresh_orchestrator_dir, tmp_path, monkeypatch):
    _register_alpha_with_an_interim_line(tmp_path, monkeypatch)
    report = "Report: " + "y" * 1500
    eid = worker_done_impl(claude_sid="w1", summary=report)["event_id"]
    result = get_worker_summary_impl(worker="alpha")
    assert result["summary"] == report
    assert result["source"] == "worker_done"
    assert result["event_id"] == eid
    assert isinstance(result["completed_at"], float)
    assert result["alive"] is True
    assert "last_turn_at" not in result

@pytest.mark.parametrize("field", ["tasked_at", "processing_since"])
def test_get_worker_summary_reads_the_transcript_when_the_done_predates_the_episode(
        fresh_orchestrator_dir, tmp_path, monkeypatch, field):
    _register_alpha_with_an_interim_line(tmp_path, monkeypatch)
    _write_done("w1", "e1", "old report", 1000.0)
    _set_active_stamps("w1", **{field: 1100.0})
    result = get_worker_summary_impl(worker="alpha")
    assert result["summary"] == _INTERIM_LINE
    assert result["source"] == "transcript"
    assert result["last_turn_at"] == _INTERIM_TS
    assert "event_id" not in result

def test_get_worker_summary_counts_a_done_inside_the_retask_grace(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _register_alpha_with_an_interim_line(tmp_path, monkeypatch)
    _write_done("w1", "e1", "report", 1099.0)
    _set_active_stamps("w1", tasked_at=1100.0)
    result = get_worker_summary_impl(worker="alpha")
    assert result["source"] == "worker_done"
    assert result["summary"] == "report"

@pytest.mark.parametrize("older_id, newer_id", [("e1", "e2"), ("e2", "e1")])
def test_get_worker_summary_takes_the_newest_done_of_the_episode(
        fresh_orchestrator_dir, tmp_path, monkeypatch, older_id, newer_id):
    _register_alpha_with_an_interim_line(tmp_path, monkeypatch)
    _set_active_stamps("w1", tasked_at=1000.0)
    _write_done("w1", older_id, "first", 1100.0)
    _write_done("w1", newer_id, "second", 1200.0)
    result = get_worker_summary_impl(worker="alpha")
    assert result["summary"] == "second"
    assert result["event_id"] == newer_id

def _write_turn_end(sid, completed_at, bucket="alpha"):
    turn_ends_dir = paths.turn_ends_dir_for(bucket)
    turn_ends_dir.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(turn_ends_dir / f"{sid}-{int(completed_at * 1000)}.json", {
        "sid": sid, "agent": "worker", "name": "alpha", "completed_at": completed_at})

@pytest.mark.parametrize("stamps", [
    {"processing_since": 1000.0},
    {"processing_since": 1000.0, "tasked_at": 1050.0},
])
def test_get_worker_summary_reads_the_transcript_when_the_turn_ended_past_the_done_lookback(
        fresh_orchestrator_dir, tmp_path, monkeypatch, stamps):
    _register_alpha_with_an_interim_line(tmp_path, monkeypatch)
    _set_active_stamps("w1", **stamps)
    _write_done("w1", "e1", "report", 1060.0)
    _write_turn_end("w1", 1661.0)
    result = get_worker_summary_impl(worker="alpha")
    assert result["source"] == "transcript"
    assert result["summary"] == _INTERIM_LINE

@pytest.mark.parametrize("turn_end, source", [(1660.0, "worker_done"), (1661.0, "transcript")])
def test_get_worker_summary_takes_the_record_turn_end_when_no_turn_ends_file_is_left(
        fresh_orchestrator_dir, tmp_path, monkeypatch, turn_end, source):
    _register_alpha_with_an_interim_line(tmp_path, monkeypatch)
    last_turn_at = datetime.fromtimestamp(turn_end, timezone.utc).isoformat().replace("+00:00", "Z")
    _set_active_stamps("w1", processing_since=1000.0, last_turn_at=last_turn_at)
    _write_done("w1", "e1", "report", 1060.0)
    result = get_worker_summary_impl(worker="alpha")
    assert result["source"] == source

@pytest.mark.parametrize("turn_end", [1070.0, 1660.0])
def test_get_worker_summary_keeps_the_done_when_the_turn_ended_inside_the_done_lookback(
        fresh_orchestrator_dir, tmp_path, monkeypatch, turn_end):
    _register_alpha_with_an_interim_line(tmp_path, monkeypatch)
    _set_active_stamps("w1", processing_since=1000.0)
    _write_done("w1", "e1", "report", 1060.0)
    _write_turn_end("w1", turn_end)
    result = get_worker_summary_impl(worker="alpha")
    assert result["source"] == "worker_done"
    assert result["summary"] == "report"

def test_get_worker_summary_returns_the_done_when_the_transcript_is_missing(
        fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    _write_done("w1", "e1", "report", 1100.0)
    result = get_worker_summary_impl(worker="alpha")
    assert result["summary"] == "report"
    assert result["source"] == "worker_done"
    assert "error" not in result

def test_get_worker_summary_skips_an_unreadable_done_file(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _register_alpha_with_an_interim_line(tmp_path, monkeypatch)
    _set_active_stamps("w1", tasked_at=time.time() - 60)
    done_dir = paths.done_dir_for("alpha")
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / "w1-bad.json").write_text("{")
    _write_done("w1", "e2", "report", None)
    result = get_worker_summary_impl(worker="alpha")
    assert result["summary"] == "report"
    assert result["source"] == "worker_done"

def test_get_worker_tail_returns_last_n_lines(fresh_orchestrator_dir, tmp_path, monkeypatch):
    lines = []
    for i in range(100):
        role = "assistant" if i % 2 == 0 else "user"
        lines.append({"type": role, "message": {"content": f"msg-{i}"}})
    _write_fake_transcript(tmp_path, monkeypatch, "w1", lines)
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    result = get_worker_tail_impl(worker="alpha", lines=10)
    assert result["name"] == "alpha"
    assert result["lines_returned"] == 10
    assert len(result["entries"]) == 10
    assert result["entries"][-1]["role"] == "user"
    assert "msg-99" in result["entries"][-1]["content_preview"]
    assert "msg-90" in result["entries"][0]["content_preview"]

def test_get_worker_tail_reads_codex_payload_content(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    sessions = tmp_path / ".codex" / "sessions" / "2026" / "05" / "26"
    sessions.mkdir(parents=True)
    log = sessions / "rollout-2026-05-26T10-55-35-codex-sid.jsonl"
    log.write_text("\n".join(json.dumps(line) for line in [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "codex hello"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "codex prompt"}],
            },
        },
    ]))
    register_self_impl(
        claude_sid="codex-sid",
        agent="worker",
        name="codex-alpha",
        cwd="/x",
        iterm_sid="i1",
        pid=os.getpid(),
        runtime="codex",
    )
    result = get_worker_tail_impl(worker="codex-alpha", lines=10)
    assert result["entries"][0]["role"] == "assistant"
    assert result["entries"][0]["content_preview"] == "codex hello"
    assert result["entries"][1]["role"] == "user"
    assert result["entries"][1]["content_preview"] == "codex prompt"

def test_get_worker_tail_truncates_content_preview(fresh_orchestrator_dir, tmp_path, monkeypatch):
    long_text = "y" * 500
    _write_fake_transcript(tmp_path, monkeypatch, "w1", [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": long_text}]}},
    ])
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    result = get_worker_tail_impl(worker="alpha", lines=10)
    assert len(result["entries"]) == 1
    assert len(result["entries"][0]["content_preview"]) <= 200

def test_get_worker_tail_missing_log(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    result = get_worker_tail_impl(worker="alpha")
    assert result["error"] == "transcript not found"

def test_get_worker_tail_unknown_worker(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="no worker named 'ghost'"):
        get_worker_tail_impl(worker="ghost")

from dockwright.mcp_server import worker_done_impl

def test_worker_done_writes_event_file(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    result = worker_done_impl(claude_sid="w1", summary="rebase landed; tests green")
    assert result["ok"] is True
    assert "event_id" in result
    done_files = list(paths.DONE.rglob("*.json"))
    assert len(done_files) == 1
    record = state.read_json(done_files[0])
    assert record["claude_sid"] == "w1"
    assert record["worker_name"] == "alpha"
    assert record["summary"] == "rebase landed; tests green"
    assert record["event_id"] == result["event_id"]
    assert isinstance(record["completed_at"], (int, float))
    assert done_files[0].name == f"w1-{result['event_id']}.json"
    assert done_files[0].parent.name == paths.UNSCOPED_BUCKET

def _set_active_stamps(sid, **stamps):
    path = paths.ACTIVE / f"{sid}.json"
    record = state.read_json(path)
    record.update(stamps)
    state.write_json_atomic(path, record)

def _the_done_record():
    done_files = list(paths.DONE.rglob("*.json"))
    assert len(done_files) == 1
    return state.read_json(done_files[0])

def test_worker_done_stamps_the_engineer_input_when_his_line_is_the_latest_input(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    _set_active_stamps("w1", input_at=1234.5, human_attached_at=1234.5, tasked_at=1000.0)
    worker_done_impl(claude_sid="w1", summary="PARKED — x\nanswered him")
    record = _the_done_record()
    assert record["engineer_input_at"] == 1234.5
    assert record["tasked_at"] == 1000.0

@pytest.mark.parametrize("stamps", [
    {"input_at": 2000.0, "human_attached_at": 1000.0},
    {"human_attached_at": 1000.0},
    {},
    {"input_at": 1, "human_attached_at": True},
])
def test_worker_done_stamps_no_engineer_input_after_a_relay_or_a_manual_attach(fresh_orchestrator_dir, stamps):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    _set_active_stamps("w1", **stamps)
    worker_done_impl(claude_sid="w1", summary="PARKED — x\nanswered the manager")
    record = _the_done_record()
    assert "engineer_input_at" not in record
    assert "tasked_at" not in record

def test_worker_done_unknown_sid_rejected(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="session ghost-sid not registered"):
        worker_done_impl(claude_sid="ghost-sid", summary="done")
    assert list(paths.DONE.rglob("*.json")) == []

def test_worker_done_multiple_events_for_same_worker(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    r1 = worker_done_impl(claude_sid="w1", summary="task A done")
    r2 = worker_done_impl(claude_sid="w1", summary="task B done")
    assert r1["event_id"] != r2["event_id"]
    assert len(list(paths.DONE.rglob("*.json"))) == 2

def test_worker_done_scoped_to_parent_manager_subdir(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x",
                       iterm_sid="i1", pid=os.getpid(), parent_manager_name="manager-a")
    worker_done_impl(claude_sid="w1", summary="scoped done")
    scoped = list((paths.DONE / "manager-a").glob("*.json"))
    assert len(scoped) == 1
    assert state.read_json(scoped[0])["summary"] == "scoped done"
    assert list((paths.DONE / paths.UNSCOPED_BUCKET).glob("*.json")) == []


def test_worker_done_writes_scoped_done_event(fresh_orchestrator_dir):
    register_self_impl(
        claude_sid="w1",
        agent="worker",
        name="alpha",
        cwd="/x",
        iterm_sid="i1",
        pid=os.getpid(),
        parent_manager_name="spry-walrus",
    )

    worker_done_impl(claude_sid="w1", summary="done")

    done_files = list((paths.DONE / "spry-walrus").glob("*.json"))
    assert len(done_files) == 1
    assert state.read_json(done_files[0])["summary"] == "done"


def test_worker_done_stamps_ticket_and_artifacts_published(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    paths.ASSIGNMENTS.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.ASSIGNMENTS / "w1.json", {"claude_sid": "w1", "ticket": "TKT-SANDBOX-1"})
    artifact_put_impl("TKT-SANDBOX-1", "spec", "repo", "body", "complete", "w1")
    artifact_put_impl("TKT-SANDBOX-1", "plan", "repo", "body", "complete", "other-sid")
    worker_done_impl(claude_sid="w1", summary="done")
    (event_path,) = list(paths.done_dir_for(None).glob("w1-*.json"))
    event = state.read_json(event_path)
    assert event["ticket"] == "TKT-SANDBOX-1"
    assert event["artifacts_published"] == 1


def test_worker_done_stamps_zero_when_keyed_but_unpublished(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    paths.ASSIGNMENTS.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.ASSIGNMENTS / "w1.json", {"claude_sid": "w1", "ticket": "TKT-SANDBOX-1"})
    worker_done_impl(claude_sid="w1", summary="done")
    (event_path,) = list(paths.done_dir_for(None).glob("w1-*.json"))
    assert state.read_json(event_path)["artifacts_published"] == 0


def test_worker_done_omits_stamp_without_assignment(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    worker_done_impl(claude_sid="w1", summary="done")
    (event_path,) = list(paths.done_dir_for(None).glob("w1-*.json"))
    event = state.read_json(event_path)
    assert "artifacts_published" not in event
    assert "ticket" not in event


def test_worker_done_never_raises_from_stamp(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as _mcp
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    paths.ASSIGNMENTS.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.ASSIGNMENTS / "w1.json", {"claude_sid": "w1", "ticket": "TKT-SANDBOX-1"})
    monkeypatch.setattr(_mcp, "artifact_list_impl",
                        lambda t: (_ for _ in ()).throw(RuntimeError("store down")))
    result = worker_done_impl(claude_sid="w1", summary="done")
    assert result["ok"] is True


def test_worker_done_self_heals_from_claimed_assignment(fresh_orchestrator_dir):
    state.write_json_atomic(paths.assignment_path("w9"), {
        "assignment_id": "a-1", "claude_sid": "w9", "name": "fix-thing",
        "parent_manager_name": "boss", "claimed_at": 1000.0,
    })
    result = worker_done_impl(claude_sid="w9", summary="done anyway")
    assert result["ok"] is True
    assert result["self_healed"] is True
    events = [state.read_json(p) for p in paths.DONE.rglob("w9-*.json")]
    assert len(events) == 1
    event = events[0]
    assert event["worker_name"] == "fix-thing"
    assert event["parent_manager_name"] == "boss"
    assert event["summary"] == "done anyway"
    assert event["self_healed"] is True
    assert event["spend"] is None


def test_worker_done_no_record_no_assignment_still_rejects(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="not registered"):
        worker_done_impl(claude_sid="ghost-sid", summary="done")


def test_worker_done_rejects_assignment_with_foreign_sid_stamp(fresh_orchestrator_dir):
    state.write_json_atomic(paths.assignment_path("w9"),
                            {"claude_sid": "other", "name": "fix-thing"})
    with pytest.raises(ValueError, match="not registered"):
        worker_done_impl(claude_sid="w9", summary="done")


def test_worker_done_active_record_path_has_no_self_healed_marker(fresh_orchestrator_dir):
    state.write_json_atomic(paths.ACTIVE / "w1.json",
                            {"claude_sid": "w1", "agent": "worker", "name": "alpha"})
    result = worker_done_impl(claude_sid="w1", summary="normal done")
    assert result == {"ok": True, "event_id": result["event_id"]}
    event = [state.read_json(p) for p in paths.DONE.rglob("w1-*.json")][0]
    assert "self_healed" not in event


def test_wait_for_worker_harvests_self_healed_done_event(fresh_orchestrator_dir):
    import asyncio
    from dockwright.mcp_server import wait_for_worker_impl
    state.write_json_atomic(paths.assignment_path("w9"), {
        "claude_sid": "w9", "name": "fix-thing", "parent_manager_name": "boss",
    })
    worker_done_impl(claude_sid="w9", summary="ghost finished")
    result = asyncio.run(wait_for_worker_impl("fix-thing", timeout_sec=2,
                                              manager_name="boss"))
    assert result["found"] == "done"
    assert result["summary"] == "ghost finished"


from dockwright.mcp_server import (
    prepare_handoff_impl, become_manager_with_takeover_impl,
    prepare_recovery_handoff_impl,
)

def test_prepare_handoff_writes_file_and_snapshots(fresh_orchestrator_dir):
    mgr_result = become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x",
                       iterm_sid="i1", pid=os.getpid(),
                       parent_manager_name=mgr_result["name"])
    from dockwright.mcp_server import _write_question
    _write_question(worker_sid="w1", worker_name="alpha", question="ours or theirs?",
                    parent_manager_name=mgr_result["name"])

    result = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="working on PR-123", trigger_reason="manual")
    assert "handoff_id" in result
    assert "path" in result

    handoff = state.read_json(paths.HANDOFFS / f"{result['handoff_id']}.json")
    assert handoff["from_sid"] == "mgr-old"
    assert handoff["to_sid"] is None
    assert handoff["consumed_at"] is None
    assert handoff["trigger_reason"] == "manual"
    assert handoff["narrative_summary"] == "working on PR-123"
    assert len(handoff["workers_snapshot"]) == 1
    assert handoff["workers_snapshot"][0]["name"] == "alpha"
    assert len(handoff["questions_snapshot"]) == 1
    assert handoff["questions_snapshot"][0]["question"] == "ours or theirs?"
    assert isinstance(handoff["prepared_at"], (int, float))


def test_prepare_handoff_rejects_non_manager(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    with pytest.raises(ValueError, match="not the current manager"):
        prepare_handoff_impl(claude_sid="w1", narrative_summary="...", trigger_reason="manual")


def test_prepare_handoff_rejects_unknown_sid(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="not the current manager"):
        prepare_handoff_impl(claude_sid="ghost", narrative_summary="...", trigger_reason="manual")


def _open_notebook(domain="general"):
    path = notebook_mark.notebook_file(domain)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# N\n\n## open entry\ncheck: x\n")


def test_prepare_handoff_refuses_without_notebook_mark(fresh_orchestrator_dir):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")
    _open_notebook()
    with pytest.raises(ValueError, match="dockwright notebook mark --sid mgr-old --domain general"):
        prepare_handoff_impl("mgr-old", "n", "manual")
    assert list(paths.HANDOFFS.glob("*.json")) == []


def test_prepare_handoff_passes_with_this_sessions_mark(fresh_orchestrator_dir):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")
    _open_notebook()
    notebook_mark.write("mgr-old", "general")
    result = prepare_handoff_impl("mgr-old", "n", "manual")
    assert "handoff_id" in result
    assert Path(result["path"]).exists()


def test_become_manager_with_takeover_releases_and_acquires(fresh_orchestrator_dir, monkeypatch):
    old_result = become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")

    closed = []
    monkeypatch.setattr(
        "dockwright.mcp_server._close_window",
        lambda window_id: closed.append(window_id),
    )
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)

    result = become_manager_with_takeover_impl(
        claude_sid="mgr-new", takeover_from="mgr-old",
        handoff_id=handoff["handoff_id"], iterm_sid="i1",
    )
    assert result["ok"] is True
    new_record = state.read_json(paths.ACTIVE / "mgr-new.json")
    assert new_record["agent"] == "manager"
    assert new_record["name"] == old_result["name"]
    assert new_record["domain"] == "general"
    assert new_record["runtime"] == "claude"
    assert closed == ["i0"]
    handoff_after = state.read_json(paths.HANDOFFS / f"{handoff['handoff_id']}.json")
    assert handoff_after["consumed_at"] is not None
    assert handoff_after["to_sid"] == "mgr-new"


def test_become_manager_with_takeover_refuses_work_root_before_touching_predecessor(fresh_orchestrator_dir, monkeypatch, tmp_path):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    closed = []
    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda window_id: closed.append(window_id))
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)
    work_root = tmp_path / "work"
    (work_root / "manager").mkdir(parents=True)
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(work_root / "manager"))
    monkeypatch.chdir(work_root)
    with pytest.raises(ValueError, match="refusing to start a manager"):
        become_manager_with_takeover_impl(
            claude_sid="mgr-new", takeover_from="mgr-old",
            handoff_id=handoff["handoff_id"], iterm_sid="i1",
        )
    assert closed == []
    assert (paths.ACTIVE / "mgr-old.json").exists()
    assert not (paths.ACTIVE / "mgr-new.json").exists()
    assert state.read_json(paths.HANDOFFS / f"{handoff['handoff_id']}.json")["consumed_at"] is None


def test_spawn_replacement_manager_refuses_when_manager_home_cannot_be_created(fresh_orchestrator_dir, monkeypatch, tmp_path):
    parent_file = tmp_path / "afile"
    parent_file.write_text("x")
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(parent_file / "manager"))
    become_manager_impl(claude_sid="mgr-old", iterm_sid="42")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    captured = _patch_spawn_worker_tab(monkeypatch)
    _patch_window_id_exists(monkeypatch, True)
    with pytest.raises(ValueError, match="could not be created"):
        _asyncio.run(spawn_replacement_manager_impl(handoff["handoff_id"]))
    assert captured == {}


def test_become_manager_with_takeover_reports_pane_closed_and_reuses_preflight(fresh_orchestrator_dir, monkeypatch):
    from dockwright import mcp_server
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="s", trigger_reason="manual")
    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda w: None)
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)
    monkeypatch.setattr(mcp_server, "_run_preflight_cleanup", lambda: "pruned 1 husk")

    result = mcp_server.become_manager_with_takeover_impl(
        claude_sid="mgr-new", takeover_from="mgr-old",
        handoff_id=handoff["handoff_id"], iterm_sid="i1")
    assert result["predecessor_pane_closed"] is True
    assert result["preflight"] == "pruned 1 husk"


def test_become_manager_with_takeover_flags_predecessor_pane_still_open(fresh_orchestrator_dir, monkeypatch):
    from dockwright import mcp_server
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="s", trigger_reason="manual")
    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda w: None)
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)
    monkeypatch.setattr(mcp_server, "_paint_manager_tab", lambda name, domain: None)

    class _StillOpenDriver:
        def current_pane_id(self):
            return "i1"
        async def pane_exists(self, pane):
            return True

    monkeypatch.setattr(mcp_server, "get_driver", lambda: _StillOpenDriver())

    result = mcp_server.become_manager_with_takeover_impl(
        claude_sid="mgr-new", takeover_from="mgr-old",
        handoff_id=handoff["handoff_id"], iterm_sid="i1")
    assert result["ok"] is True
    assert result["predecessor_pane_closed"] is False


def test_become_manager_with_takeover_verifies_pane_from_inside_running_loop(fresh_orchestrator_dir, monkeypatch):
    import asyncio
    from dockwright import mcp_server
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="s", trigger_reason="manual")
    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda w: None)
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)
    monkeypatch.setattr(mcp_server, "_paint_manager_tab", lambda name, domain: None)

    class _PaneGoneDriver:
        def current_pane_id(self):
            return "i1"
        async def pane_exists(self, pane):
            return False

    monkeypatch.setattr(mcp_server, "get_driver", lambda: _PaneGoneDriver())

    async def _call_from_loop():
        assert asyncio.get_running_loop() is not None
        return mcp_server.become_manager_with_takeover_impl(
            claude_sid="mgr-new", takeover_from="mgr-old",
            handoff_id=handoff["handoff_id"], iterm_sid="i1")

    result = asyncio.run(_call_from_loop())
    assert result["ok"] is True
    assert result["predecessor_pane_closed"] is True


def test_become_manager_with_takeover_stamps_account_from_env(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda window_id: None)
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)
    _install_two_pool(monkeypatch, fresh_orchestrator_dir)
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT", "b")
    become_manager_with_takeover_impl(
        claude_sid="mgr-new", takeover_from="mgr-old",
        handoff_id=handoff["handoff_id"], iterm_sid="i1",
    )
    assert state.read_json(paths.ACTIVE / "mgr-new.json")["account"] == "b"


def test_become_manager_with_takeover_registers_claude_runtime(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")

    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda window_id: None)
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)

    result = become_manager_with_takeover_impl(
        claude_sid="mgr-new",
        takeover_from="mgr-old",
        handoff_id=handoff["handoff_id"],
        iterm_sid="i1",
    )
    assert result["runtime"] == "claude"
    new_record = state.read_json(paths.ACTIVE / "mgr-new.json")
    assert new_record["runtime"] == "claude"


def test_become_manager_with_takeover_appends_trigger_log(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")
    handoff = prepare_handoff_impl(
        claude_sid="mgr-old",
        narrative_summary="x" * 300,
        trigger_reason="mcp-refresh",
    )
    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda w: None)
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)

    become_manager_with_takeover_impl(
        claude_sid="mgr-new", takeover_from="mgr-old",
        handoff_id=handoff["handoff_id"], iterm_sid="i1",
    )

    log_lines = paths.MANAGER_TRIGGERS_LOG.read_text().splitlines()
    assert len(log_lines) == 1
    entry = json.loads(log_lines[0])
    assert entry["from_sid"] == "mgr-old"
    assert entry["to_sid"] == "mgr-new"
    assert entry["handoff_id"] == handoff["handoff_id"]
    assert entry["trigger_reason"] == "mcp-refresh"
    assert entry["narrative_excerpt"] == "x" * 200
    assert isinstance(entry["ts"], (int, float))


def test_become_manager_with_takeover_swallows_terminal_failure(fresh_orchestrator_dir, monkeypatch, tmp_path):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")

    def boom(*a, **k):
        raise OSError("tmux server gone")
    monkeypatch.setattr("dockwright.mcp_server.subprocess.run", boom)
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)

    result = become_manager_with_takeover_impl(
        claude_sid="mgr-new", takeover_from="mgr-old",
        handoff_id=handoff["handoff_id"], iterm_sid="i1",
    )
    assert result["ok"] is True


def test_become_manager_with_takeover_rejects_mismatched_handoff(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="...", trigger_reason="manual")
    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda w: None)
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)

    with pytest.raises(ValueError, match="prepared by mgr-old"):
        become_manager_with_takeover_impl(
            claude_sid="mgr-new", takeover_from="someone-else",
            handoff_id=handoff["handoff_id"], iterm_sid="i1",
        )
    assert state.read_json(paths.ACTIVE / "mgr-old.json") is not None
    assert not (paths.ACTIVE / "mgr-new.json").exists()


def test_bootstrap_recreate_handoff_key_parity(fresh_orchestrator_dir, tmp_path):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    mcp_keys = set(state.read_json(paths.HANDOFFS / f"{handoff['handoff_id']}.json").keys())

    script = Path(__file__).resolve().parent.parent / "deploy" / "scripts" / "bootstrap-recreate.sh"
    home = tmp_path / "script-home"
    active = home / ".claude" / "dockwright" / "active"
    active.mkdir(parents=True)
    (active / "sid-x.json").write_text(_json.dumps({
        "claude_sid": "sid-x", "agent": "manager", "name": "mighty-demon",
        "domain": "personal", "pid": 4242,
    }))
    proc = subprocess.run(
        ["bash", str(script), "--narrative", "probe", "--from-sid", "sid-x", "--dry-run"],
        capture_output=True, text=True, env={**os.environ, "HOME": str(home)},
    )
    assert proc.returncode == 0, proc.stderr
    payload_line = next(l for l in proc.stdout.splitlines() if l.startswith("handoff_payload: "))
    script_keys = set(_json.loads(payload_line[len("handoff_payload: "):]).keys())

    assert mcp_keys == script_keys, (mcp_keys, script_keys)


@pytest.mark.parametrize("missing_mode", ["deleted", "empty"])
def test_takeover_fails_loud_when_handoff_lacks_manager_name(fresh_orchestrator_dir, missing_mode):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0", domain="personal")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    (paths.ACTIVE / "mgr-old.json").unlink()

    from dockwright.mcp_server import _write_question
    _write_question(worker_sid="mgr-old", worker_name="manager", question="dangling?")

    handoff_path = paths.HANDOFFS / f"{handoff['handoff_id']}.json"
    stripped = state.read_json(handoff_path)
    if missing_mode == "deleted":
        del stripped["manager_name"]
    else:
        stripped["manager_name"] = ""
    state.write_json_atomic(handoff_path, stripped)

    with pytest.raises(ValueError) as exc_info:
        become_manager_with_takeover_impl(
            claude_sid="mgr-new", takeover_from="mgr-old",
            handoff_id=handoff["handoff_id"], iterm_sid="i1",
        )
    message = str(exc_info.value)
    assert handoff["handoff_id"] in message, message
    omitted = message.split("omits ", 1)[1].split(".", 1)[0]
    assert omitted == "manager_name", message

    handoff_after = state.read_json(handoff_path)
    assert handoff_after["consumed_at"] is None
    assert handoff_after["to_sid"] is None
    assert not (paths.ACTIVE / "mgr-new.json").exists()
    remaining = [
        state.read_json(q) for q in paths.QUESTIONS.iterdir() if q.suffix == ".json"
    ]
    assert any(r is not None and r.get("worker_sid") == "mgr-old" for r in remaining), \
        "refused takeover must not drop the predecessor's pending question"


@pytest.mark.parametrize("missing_mode", ["deleted", "empty"])
def test_takeover_fails_loud_when_handoff_lacks_domain_never_defaults_general(fresh_orchestrator_dir, missing_mode):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0", domain="personal")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    (paths.ACTIVE / "mgr-old.json").unlink()

    from dockwright.mcp_server import _write_question
    _write_question(worker_sid="mgr-old", worker_name="manager", question="dangling?")

    handoff_path = paths.HANDOFFS / f"{handoff['handoff_id']}.json"
    stripped = state.read_json(handoff_path)
    if missing_mode == "deleted":
        del stripped["domain"]
    else:
        stripped["domain"] = ""
    state.write_json_atomic(handoff_path, stripped)

    with pytest.raises(ValueError) as exc_info:
        become_manager_with_takeover_impl(
            claude_sid="mgr-new", takeover_from="mgr-old",
            handoff_id=handoff["handoff_id"], iterm_sid="i1",
        )
    message = str(exc_info.value)
    assert handoff["handoff_id"] in message, message
    omitted = message.split("omits ", 1)[1].split(".", 1)[0]
    assert omitted == "domain", message

    handoff_after = state.read_json(handoff_path)
    assert handoff_after["consumed_at"] is None
    assert handoff_after["to_sid"] is None
    assert not (paths.ACTIVE / "mgr-new.json").exists()
    remaining = [
        state.read_json(q) for q in paths.QUESTIONS.iterdir() if q.suffix == ".json"
    ]
    assert any(r is not None and r.get("worker_sid") == "mgr-old" for r in remaining), \
        "refused takeover must not drop the predecessor's pending question"


def test_takeover_pre_mutation_when_predecessor_record_on_disk_but_nameless(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0", domain="personal")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")

    old_record_path = paths.ACTIVE / "mgr-old.json"
    old_record = state.read_json(old_record_path)
    del old_record["name"]
    state.write_json_atomic(old_record_path, old_record)

    handoff_path = paths.HANDOFFS / f"{handoff['handoff_id']}.json"
    stripped = state.read_json(handoff_path)
    del stripped["manager_name"]
    state.write_json_atomic(handoff_path, stripped)

    from dockwright.mcp_server import _write_question
    _write_question(worker_sid="mgr-old", worker_name="manager", question="dangling?")

    closed = []
    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda w: closed.append(w))
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)

    with pytest.raises(ValueError) as exc_info:
        become_manager_with_takeover_impl(
            claude_sid="mgr-new", takeover_from="mgr-old",
            handoff_id=handoff["handoff_id"], iterm_sid="i1",
        )
    message = str(exc_info.value)
    omitted = message.split("omits ", 1)[1].split(".", 1)[0]
    assert omitted == "manager_name", message

    assert closed == [], "the predecessor's window must not be closed before the raise"
    assert old_record_path.exists(), "the predecessor's record must not be unlinked before the raise"
    remaining = [
        state.read_json(q) for q in paths.QUESTIONS.iterdir() if q.suffix == ".json"
    ]
    assert any(r is not None and r.get("worker_sid") == "mgr-old" for r in remaining), \
        "refused takeover must not drop the predecessor's pending question"
    handoff_after = state.read_json(handoff_path)
    assert handoff_after["consumed_at"] is None
    assert not (paths.ACTIVE / "mgr-new.json").exists()


def test_takeover_record_gone_complete_handoff_succeeds(fresh_orchestrator_dir, monkeypatch):
    old_result = become_manager_impl(claude_sid="mgr-old", iterm_sid="i0", domain="personal")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    (paths.ACTIVE / "mgr-old.json").unlink()

    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda w: None)
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)

    result = become_manager_with_takeover_impl(
        claude_sid="mgr-new", takeover_from="mgr-old",
        handoff_id=handoff["handoff_id"], iterm_sid="i1",
    )
    assert result["ok"] is True
    assert result["name"] == old_result["name"]
    assert result["domain"] == "personal"
    new_record = state.read_json(paths.ACTIVE / "mgr-new.json")
    assert new_record["name"] == old_result["name"]
    assert new_record["domain"] == "personal"
    assert result["name"] == new_record["name"]


def test_takeover_prunes_dead_corpse_holding_inherited_name(fresh_orchestrator_dir, monkeypatch):
    old_result = become_manager_impl(claude_sid="mgr-old", iterm_sid="i0", domain="personal")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    (paths.ACTIVE / "mgr-old.json").unlink()

    state.write_json_atomic(paths.ACTIVE / "corpse.json", {
        "claude_sid": "corpse", "agent": "manager", "name": old_result["name"],
        "pid": 99999, "domain": "personal",
    })

    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda w: None)
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: pid != 99999)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: pid != 99999)

    result = become_manager_with_takeover_impl(
        claude_sid="mgr-new", takeover_from="mgr-old",
        handoff_id=handoff["handoff_id"], iterm_sid="i1",
    )
    assert result["ok"] is True
    assert result["name"] == old_result["name"]
    assert result["domain"] == "personal"
    assert not (paths.ACTIVE / "corpse.json").exists(), "the dead corpse must have been pruned"
    new_record = state.read_json(paths.ACTIVE / "mgr-new.json")
    assert new_record["name"] == old_result["name"]
    assert new_record["domain"] == "personal"


@pytest.mark.parametrize("collision_field", ["name", "funny_name"])
def test_takeover_fails_loud_when_inherited_name_collides_with_live_session(
    fresh_orchestrator_dir, monkeypatch, collision_field
):
    old_result = become_manager_impl(claude_sid="mgr-old", iterm_sid="i0", domain="personal")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")

    from dockwright.mcp_server import _write_question
    _write_question(worker_sid="mgr-old", worker_name="manager", question="dangling?")

    usurper_record = {
        "claude_sid": "usurper", "agent": "manager", "cwd": "/x",
        "window_id": "i9", "pid": os.getpid(), "domain": "personal", "runtime": "claude",
    }
    if collision_field == "name":
        usurper_record["name"] = old_result["name"]
    else:
        usurper_record["name"] = "usurper-own-name"
        usurper_record["funny_name"] = old_result["name"]
    state.write_json_atomic(paths.ACTIVE / "usurper.json", usurper_record)

    closed = []
    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda w: closed.append(w))
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)

    with pytest.raises(ValueError) as exc_info:
        become_manager_with_takeover_impl(
            claude_sid="mgr-new", takeover_from="mgr-old",
            handoff_id=handoff["handoff_id"], iterm_sid="i1",
        )
    assert closed == [], "the predecessor's window must not be closed before the refusal"
    assert (paths.ACTIVE / "mgr-old.json").exists(), \
        "the predecessor's record must not be unlinked before the refusal"
    remaining = [
        state.read_json(q) for q in paths.QUESTIONS.iterdir() if q.suffix == ".json"
    ]
    assert any(r is not None and r.get("worker_sid") == "mgr-old" for r in remaining), \
        "refused takeover must not drop the predecessor's pending question"

    message = str(exc_info.value)
    assert old_result["name"] in message, message
    assert "stand down" in message, message

    handoff_after = state.read_json(paths.HANDOFFS / f"{handoff['handoff_id']}.json")
    assert handoff_after["consumed_at"] is None
    assert handoff_after["to_sid"] is None
    assert not (paths.ACTIVE / "mgr-new.json").exists()
    assert state.read_json(paths.ACTIVE / "usurper.json") is not None


def test_takeover_raises_and_unregisters_when_registration_races_and_returns_suffixed_name(
    fresh_orchestrator_dir, monkeypatch
):
    from dockwright import mcp_server

    old_result = become_manager_impl(claude_sid="mgr-old", iterm_sid="i0", domain="personal")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    (paths.ACTIVE / "mgr-old.json").unlink()

    register_self_impl(
        claude_sid="legacy-worker", agent="worker", name="legacy-worker-name",
        cwd="/x", iterm_sid="i7", pid=os.getpid(),
    )

    monkeypatch.setattr(mcp_server, "_close_window", lambda w: None)
    monkeypatch.setattr(mcp_server, "_pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)

    raced_name = old_result["name"] + "-2"

    def fake_become_manager_impl(claude_sid, iterm_sid="", domain=None, name=None):
        state.write_json_atomic(paths.ACTIVE / f"{claude_sid}.json", {
            "claude_sid": claude_sid, "agent": "manager", "name": raced_name,
            "cwd": "/x", "window_id": iterm_sid, "pid": os.getpid(),
            "domain": domain, "runtime": "claude",
        })
        legacy = state.read_json(paths.ACTIVE / "legacy-worker.json")
        legacy["parent_manager_name"] = raced_name
        legacy["parent_name"] = raced_name
        legacy["parent_sid"] = claude_sid
        state.write_json_atomic(paths.ACTIVE / "legacy-worker.json", legacy)
        return {"ok": True, "name": raced_name, "domain": domain, "runtime": "claude", "preflight": ""}

    monkeypatch.setattr(mcp_server, "become_manager_impl", fake_become_manager_impl)

    with pytest.raises(ValueError) as exc_info:
        mcp_server.become_manager_with_takeover_impl(
            claude_sid="mgr-new", takeover_from="mgr-old",
            handoff_id=handoff["handoff_id"], iterm_sid="i1",
        )
    message = str(exc_info.value)
    assert "raced" in message, message
    assert raced_name in message, message
    assert old_result["name"] in message, message

    legacy_after = state.read_json(paths.ACTIVE / "legacy-worker.json")
    assert legacy_after["parent_manager_name"] is None, \
        "a worker just backfilled onto the dead suffixed name must be reverted to unowned"
    assert "reverted" in message.lower(), message

    assert not (paths.ACTIVE / "mgr-new.json").exists(), \
        "the wrongly-named successor must be unregistered, not left active"
    handoff_after = state.read_json(paths.HANDOFFS / f"{handoff['handoff_id']}.json")
    assert handoff_after["consumed_at"] is None
    assert handoff_after["to_sid"] is None
    assert legacy_after["parent_name"] is None
    assert legacy_after["parent_sid"] is None


def test_bootstrap_recreate_seam_end_to_end(tmp_path, fresh_orchestrator_dir, monkeypatch):
    import shutil
    script = Path(__file__).resolve().parent.parent / "deploy" / "scripts" / "bootstrap-recreate.sh"
    script_home = tmp_path / "script-home"
    active = script_home / ".claude" / "dockwright" / "active"
    active.mkdir(parents=True)
    (active / "sid-x.json").write_text(_json.dumps({
        "claude_sid": "sid-x", "agent": "manager", "name": "mighty-demon",
        "domain": "personal", "pid": 4242, "window_id": "i0",
    }))
    handoffs_dir = script_home / ".claude" / "dockwright" / "handoffs"

    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    tmux_log = tmp_path / "tmux-invocations.log"
    (fakebin / "tmux").write_text(
        "#!/bin/bash\n"
        f"echo \"$@\" >> {tmux_log}\n"
        "case \"$*\" in *has-session*) exit 1 ;; *new-session*|*new-window*) echo '@1'; exit 0 ;; esac\n"
        "exit 0\n"
    )
    (fakebin / "tmux").chmod(0o755)
    (fakebin / "jq").symlink_to(shutil.which("jq"))
    (fakebin / "uuidgen").symlink_to(shutil.which("uuidgen"))

    sock = f"wt-iso-{os.getpid()}-seam"
    proc = subprocess.run(
        ["bash", str(script), "--narrative", "seam probe", "--from-sid", "sid-x"],
        capture_output=True, text=True,
        env={**os.environ, "HOME": str(script_home),
             "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
             "DOCKWRIGHT_TMUX_SOCKET": sock},
    )
    assert proc.returncode == 0, proc.stderr
    written = list(handoffs_dir.glob("*.json"))
    assert len(written) == 1, written
    handoff_id = written[0].stem

    dw = script_home / ".claude" / "dockwright"
    monkeypatch.setattr(paths, "ROOT", dw)
    monkeypatch.setattr(paths, "ACTIVE", dw / "active")
    monkeypatch.setattr(paths, "QUESTIONS", dw / "questions")
    monkeypatch.setattr(paths, "ANSWERS", dw / "answers")
    monkeypatch.setattr(paths, "DONE", dw / "done")
    monkeypatch.setattr(paths, "CLOSED", dw / "closed")
    monkeypatch.setattr(paths, "HANDOFFS", dw / "handoffs")
    monkeypatch.setattr(paths, "PRESETS", dw / "presets")
    monkeypatch.setattr(paths, "MANAGER_TRIGGERS_LOG", dw / "manager-triggers.jsonl")
    monkeypatch.setattr(paths, "MANAGER_MEMORY", dw / "manager-memory")
    monkeypatch.setattr(paths, "SLOTS", dw / "slots")
    monkeypatch.setattr(paths, "ARTIFACTS", dw / "artifacts")
    monkeypatch.setattr(paths, "ASSIGNMENTS", dw / "assignments")
    monkeypatch.setattr(paths, "ASSIGNMENTS_PENDING", dw / "assignments" / ".pending")
    monkeypatch.setattr(paths, "SPEND_LEDGER", dw / "spend-ledger.jsonl")

    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda w: None)
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)

    result = become_manager_with_takeover_impl(
        claude_sid="mgr-new", takeover_from="sid-x",
        handoff_id=handoff_id, iterm_sid="i1",
    )
    assert result["ok"] is True
    assert result["name"] == "mighty-demon"
    assert result["domain"] == "personal"
    new_record = state.read_json(paths.ACTIVE / "mgr-new.json")
    assert new_record["name"] == "mighty-demon"
    assert new_record["domain"] == "personal"


def _brick_manager_record(sid, age_sec=300):
    p = paths.ACTIVE / f"{sid}.json"
    record = state.read_json(p)
    record["state"] = "processing"
    state.write_json_atomic(p, record)
    old = time.time() - age_sec
    os.utime(p, (old, old))


def test_prepare_recovery_handoff_refuses_live_idle_manager(fresh_orchestrator_dir):
    become_manager_impl(claude_sid="mgr-live", iterm_sid="i0")
    with pytest.raises(ValueError, match="not an active manager"):
        prepare_recovery_handoff_impl("mgr-live")


def test_prepare_recovery_handoff_refuses_live_processing_fresh(fresh_orchestrator_dir):
    become_manager_impl(claude_sid="mgr-live", iterm_sid="i0")
    p = paths.ACTIVE / "mgr-live.json"
    record = state.read_json(p)
    record["state"] = "processing"
    state.write_json_atomic(p, record)
    with pytest.raises(ValueError, match="not an active manager"):
        prepare_recovery_handoff_impl("mgr-live")


def test_prepare_recovery_handoff_refuses_live_idle_stale_record(fresh_orchestrator_dir):
    become_manager_impl(claude_sid="mgr-idle-old", iterm_sid="i0")
    p = paths.ACTIVE / "mgr-idle-old.json"
    old = time.time() - 3600
    os.utime(p, (old, old))
    with pytest.raises(ValueError, match="not an active manager"):
        prepare_recovery_handoff_impl("mgr-idle-old")


def test_prepare_recovery_handoff_allows_bricked_target(fresh_orchestrator_dir):
    become_manager_impl(claude_sid="mgr-bricked", iterm_sid="i0")
    _brick_manager_record("mgr-bricked")
    out = prepare_recovery_handoff_impl("mgr-bricked")
    assert "handoff_id" in out


def test_prepare_recovery_handoff_marks_not_actualized(fresh_orchestrator_dir):
    become_manager_impl(claude_sid="mgr-bricked", iterm_sid="i0")
    _brick_manager_record("mgr-bricked")
    _open_notebook()
    prepare_recovery_handoff_impl("mgr-bricked")
    mark = notebook_mark.read("general")
    assert mark["sid"] == "mgr-bricked"
    assert mark["actualized"] is False
    assert mark["reason"] == "recovery handoff (account-flip-recovery)"


def test_prepare_recovery_handoff_keeps_a_fresh_actualized_mark(fresh_orchestrator_dir):
    become_manager_impl(claude_sid="mgr-bricked", iterm_sid="i0")
    _brick_manager_record("mgr-bricked")
    _open_notebook()
    notebook_mark.write("mgr-peer", "general")
    prepare_recovery_handoff_impl("mgr-bricked")
    mark = notebook_mark.read("general")
    assert mark["sid"] == "mgr-peer"
    assert mark["actualized"] is True


def test_prepare_recovery_handoff_survives_an_unwritable_mark(fresh_orchestrator_dir):
    become_manager_impl(claude_sid="mgr-bricked", iterm_sid="i0")
    _brick_manager_record("mgr-bricked")
    _open_notebook()
    (paths.ROOT / "notebook" / "marks").write_text("not a directory")
    out = prepare_recovery_handoff_impl("mgr-bricked")
    assert "handoff_id" in out
    assert Path(out["path"]).exists()


def test_prepare_recovery_handoff_allows_dead_pid(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="mgr-dead", iterm_sid="i0")
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: False)
    out = prepare_recovery_handoff_impl("mgr-dead")
    assert "handoff_id" in out


def test_prepare_recovery_handoff_allows_pidless_record(fresh_orchestrator_dir):
    state.write_json_atomic(paths.ACTIVE / "mgr-legacy.json", {
        "claude_sid": "mgr-legacy", "agent": "manager", "name": "old-timer",
        "cwd": "/x", "window_id": "i0", "pid": None, "started_at": 1.0,
        "state": "idle", "last_turn_at": None, "last_summary": None,
        "domain": "general", "parent_manager_name": None,
    })
    out = prepare_recovery_handoff_impl("mgr-legacy")
    assert "handoff_id" in out


def test_recovery_takeover_of_a_pidless_predecessor_closes_no_window(fresh_orchestrator_dir, monkeypatch):
    from dockwright import mcp_server
    state.write_json_atomic(paths.ACTIVE / "mgr-legacy.json", {
        "claude_sid": "mgr-legacy", "agent": "manager", "name": "old-timer",
        "cwd": "/x", "window_id": "i0", "pid": None, "started_at": 1.0,
        "state": "idle", "last_turn_at": None, "last_summary": None,
        "domain": "general", "parent_manager_name": None,
    })
    handoff = prepare_recovery_handoff_impl("mgr-legacy")
    closed = []
    monkeypatch.setattr(mcp_server, "_close_window", lambda window_id: closed.append(window_id))
    monkeypatch.setattr(mcp_server, "_paint_manager_tab", lambda name, domain: None)

    result = become_manager_with_takeover_impl(
        claude_sid="mgr-new", takeover_from="mgr-legacy",
        handoff_id=handoff["handoff_id"], iterm_sid="i1",
    )
    assert result["ok"] is True
    assert closed == [], "a predecessor whose pid is unknown must not have its window killed"


def test_prepare_recovery_handoff_refuses_live_manager_mid_long_turn(fresh_orchestrator_dir, tmp_path, monkeypatch):
    become_manager_impl(claude_sid="mgr-live", iterm_sid="i0")
    p = paths.ACTIVE / "mgr-live.json"
    record = state.read_json(p)
    record["state"] = "processing"
    state.write_json_atomic(p, record)
    old = time.time() - 2400
    os.utime(p, (old, old))
    log = tmp_path / "mgr-live.jsonl"
    log.write_text("{}\n")
    monkeypatch.setattr("dockwright.mcp_server.find_session_log",
                        lambda sid, runtime="claude": log)
    with pytest.raises(ValueError, match="not an active manager"):
        prepare_recovery_handoff_impl("mgr-live")


def test_prepare_recovery_handoff_allows_bricked_target_with_stale_transcript(fresh_orchestrator_dir, tmp_path, monkeypatch):
    become_manager_impl(claude_sid="mgr-bricked", iterm_sid="i0")
    _brick_manager_record("mgr-bricked", age_sec=2400)
    log = tmp_path / "mgr-bricked.jsonl"
    log.write_text("{}\n")
    old = time.time() - 2400
    os.utime(log, (old, old))
    monkeypatch.setattr("dockwright.mcp_server.find_session_log",
                        lambda sid, runtime="claude": log)
    out = prepare_recovery_handoff_impl("mgr-bricked")
    assert "handoff_id" in out


def _write_transcript(path, lines):
    path.write_text("".join(_json.dumps(ev) + "\n" for ev in lines))


def _asst(blocks):
    return {"type": "assistant", "message": {"model": "claude-opus-5", "content": blocks}}


_SYNTHETIC_401 = {"type": "assistant", "isApiErrorMessage": True,
                  "message": {"model": "<synthetic>",
                              "content": [{"type": "text", "text": "Login expired · Please run /login"}]}}
_SYNTHETIC_LIMIT = {"type": "assistant", "isApiErrorMessage": True,
                    "message": {"model": "<synthetic>",
                                "content": [{"type": "text", "text": "You've hit your session limit."}]}}


def test_prepare_recovery_handoff_refuses_manager_blocked_on_tool(fresh_orchestrator_dir, tmp_path, monkeypatch):
    become_manager_impl(claude_sid="mgr-modal", iterm_sid="i0")
    _brick_manager_record("mgr-modal", age_sec=2400)
    log = tmp_path / "mgr-modal.jsonl"
    _write_transcript(log, [
        _asst([{"type": "text", "text": "Account a hit your session limit. Flip to b?"},
               {"type": "tool_use", "id": "toolu_ask1", "name": "AskUserQuestion", "input": {}}]),
    ])
    old = time.time() - 2400
    os.utime(log, (old, old))
    monkeypatch.setattr("dockwright.mcp_server.find_session_log",
                        lambda sid, runtime="claude": log)
    with pytest.raises(ValueError, match="not an active manager"):
        prepare_recovery_handoff_impl("mgr-modal")


def test_prepare_recovery_handoff_allows_real_latched_brick_shapes(fresh_orchestrator_dir, tmp_path, monkeypatch):
    for i, latch in enumerate((_SYNTHETIC_401, _SYNTHETIC_LIMIT)):
        sid = f"mgr-latched-{i}"
        become_manager_impl(claude_sid=sid, iterm_sid=f"i{i}")
        _brick_manager_record(sid, age_sec=2400)
        log = tmp_path / f"{sid}.jsonl"
        _write_transcript(log, [
            _asst([{"type": "tool_use", "id": "toolu_old", "name": "Bash", "input": {}}]),
            {"type": "user", "message": {"content": [{"type": "tool_result",
                                                      "tool_use_id": "toolu_old"}]}},
            latch,
        ])
        old = time.time() - 2400
        os.utime(log, (old, old))
        monkeypatch.setattr("dockwright.mcp_server.find_session_log",
                            lambda sid_, runtime="claude", _log=log: _log)
        out = prepare_recovery_handoff_impl(sid)
        assert "handoff_id" in out, sid


def test_recovery_silence_floor_matches_stale_monitor():
    from dockwright import mcp_server, stale_monitor
    assert (mcp_server.RECOVERY_TAKEOVER_MIN_SILENCE_SEC
            == stale_monitor.MANAGER_LIMIT_CHECK_FLOOR_SEC)


def test_liveness_clock_is_the_monitors_quantity(fresh_orchestrator_dir, tmp_path, monkeypatch):
    from dockwright import stale_monitor
    from dockwright.mcp_server import (_recovery_target_liveness,
                                       RECOVERY_TAKEOVER_MIN_SILENCE_SEC)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(stale_monitor, "CLAUDE_PROJECTS", tmp_path / ".claude" / "projects")
    sid = "mgr-parity"
    become_manager_impl(claude_sid=sid, iterm_sid="i0")
    p = paths.ACTIVE / f"{sid}.json"
    record = state.read_json(p)
    record["state"] = "processing"
    state.write_json_atomic(p, record)
    old = time.time() - 2400
    os.utime(p, (old, old))
    proj = tmp_path / ".claude" / "projects" / "-x"
    proj.mkdir(parents=True)
    log = proj / f"{sid}.jsonl"
    log.write_text("{}\n")
    sub_dir = proj / sid / "subagents"
    sub_dir.mkdir(parents=True)
    (sub_dir / "agent-1.jsonl").write_text("{}\n")
    for transcript_age in (0, 60, 300, 2400):
        t = time.time() - transcript_age
        os.utime(log, (t, t))
        rec = state.read_json(p)
        activity, resolved = stale_monitor._last_activity(rec, int(p.stat().st_mtime))
        assert resolved == log
        monitor_says_bricked = (time.time() - activity
                                >= RECOVERY_TAKEOVER_MIN_SILENCE_SEC)
        verdict = _recovery_target_liveness(sid, rec)
        assert (verdict is None) == monitor_says_bricked, (transcript_age, verdict)


def test_takeover_refuses_revived_recovery_target(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="old-sid", iterm_sid="i0")
    _brick_manager_record("old-sid")
    out = prepare_recovery_handoff_impl("old-sid")
    p = paths.ACTIVE / "old-sid.json"
    record = state.read_json(p)
    record["state"] = "idle"
    state.write_json_atomic(p, record)
    closed = []
    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda w: closed.append(w))
    with pytest.raises(ValueError, match="liveness|live"):
        become_manager_with_takeover_impl(
            claude_sid="new-sid", takeover_from="old-sid",
            handoff_id=out["handoff_id"], iterm_sid="i2")
    assert closed == []
    assert state.read_json(paths.ACTIVE / "old-sid.json") is not None
    handoff = state.read_json(paths.HANDOFFS / f"{out['handoff_id']}.json")
    assert handoff["consumed_at"] is None


def test_takeover_refuses_revived_target_gone_idle_and_quiet(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="old-sid", iterm_sid="i0")
    p = paths.ACTIVE / "old-sid.json"
    r = state.read_json(p); r["state"] = "processing"; state.write_json_atomic(p, r)
    os.utime(p, (time.time() - 300,) * 2)
    out = prepare_recovery_handoff_impl("old-sid")
    r = state.read_json(p); r["state"] = "idle"; state.write_json_atomic(p, r)
    os.utime(p, (time.time() - 300,) * 2)
    closed = []
    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda w: closed.append(w))
    with pytest.raises(ValueError, match="liveness|live"):
        become_manager_with_takeover_impl(claude_sid="new-sid", takeover_from="old-sid",
                                          handoff_id=out["handoff_id"], iterm_sid="i2")
    assert closed == []


def test_become_manager_pops_pending_takeover_env(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setenv("DOCKWRIGHT_PENDING_TAKEOVER", "1")
    become_manager_impl(claude_sid="mgr-pop", iterm_sid="i0")
    assert "DOCKWRIGHT_PENDING_TAKEOVER" not in os.environ


def test_takeover_nonrecovery_handoff_live_predecessor_still_works(fresh_orchestrator_dir, monkeypatch):
    result = become_manager_impl(claude_sid="old-sid", iterm_sid="i0")
    state.write_json_atomic(paths.HANDOFFS / "h-recreate.json", {
        "handoff_id": "h-recreate", "from_sid": "old-sid", "to_sid": None,
        "prepared_at": time.time(), "consumed_at": None,
        "trigger_reason": "recreate", "narrative_summary": "n",
        "manager_name": result["name"], "domain": "general",
        "workers_snapshot": [], "questions_snapshot": [],
    })
    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda w: None)
    res = become_manager_with_takeover_impl(
        claude_sid="new-sid", takeover_from="old-sid",
        handoff_id="h-recreate", iterm_sid="i2")
    assert res["name"] == result["name"]


def test_prepare_recovery_handoff_shape(fresh_orchestrator_dir):
    mgr_result = become_manager_impl(claude_sid="mgr-sid-1", iterm_sid="i0")
    register_self_impl(
        claude_sid="w1", agent="worker", name="alpha", cwd="/x",
        iterm_sid="i1", pid=os.getpid(),
        parent_manager_name=mgr_result["name"],
    )
    from dockwright.mcp_server import _write_question
    _write_question(
        worker_sid="w1", worker_name="alpha", question="left or right?",
        parent_manager_name=mgr_result["name"],
    )

    _brick_manager_record("mgr-sid-1")
    out = prepare_recovery_handoff_impl("mgr-sid-1")
    assert "handoff_id" in out
    assert "path" in out

    handoff = state.read_json(paths.HANDOFFS / f"{out['handoff_id']}.json")
    assert handoff["from_sid"] == "mgr-sid-1"
    assert handoff["recovery"] is True
    assert handoff["trigger_reason"] == "account-flip-recovery"
    assert "[auto-recovery]" in handoff["narrative_summary"]
    PARITY_KEYS = {"handoff_id", "from_sid", "to_sid", "prepared_at", "consumed_at",
                   "trigger_reason", "narrative_summary", "manager_name", "domain",
                   "workers_snapshot", "questions_snapshot"}
    assert set(handoff.keys()) == PARITY_KEYS | {"recovery"}, \
        "unexpected keys — was prepare_handoff_impl extended? Mirror in the recovery record"
    assert handoff["to_sid"] is None
    assert handoff["consumed_at"] is None
    assert len(handoff["workers_snapshot"]) == 1
    assert handoff["workers_snapshot"][0]["name"] == "alpha"
    assert len(handoff["questions_snapshot"]) == 1
    assert handoff["questions_snapshot"][0]["question"] == "left or right?"


def test_prepare_recovery_handoff_rejects_non_manager(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="not an active manager"):
        prepare_recovery_handoff_impl("ghost-sid")

    register_self_impl(
        claude_sid="w1", agent="worker", name="beta", cwd="/x", iterm_sid="i1",
        pid=os.getpid(),
    )
    with pytest.raises(ValueError, match="not an active manager"):
        prepare_recovery_handoff_impl("w1")


def test_recovery_handoff_accepted_by_takeover(fresh_orchestrator_dir, monkeypatch):
    old_result = become_manager_impl(claude_sid="old-sid", iterm_sid="i0")
    register_self_impl(
        claude_sid="w1", agent="worker", name="gamma", cwd="/x",
        iterm_sid="i1", pid=os.getpid(),
        parent_manager_name=old_result["name"],
    )

    _brick_manager_record("old-sid")
    out = prepare_recovery_handoff_impl("old-sid")

    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda w: None)
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)

    result = become_manager_with_takeover_impl(
        claude_sid="new-sid",
        takeover_from="old-sid",
        handoff_id=out["handoff_id"],
        iterm_sid="i2",
    )
    assert result["ok"] is True
    assert result["name"] == old_result["name"]
    assert result["domain"] == "general"

    handoff_after = state.read_json(paths.HANDOFFS / f"{out['handoff_id']}.json")
    assert handoff_after["consumed_at"] is not None
    assert handoff_after["to_sid"] == "new-sid"

    new_record = state.read_json(paths.ACTIVE / "new-sid.json")
    assert new_record["agent"] == "manager"
    assert new_record["name"] == old_result["name"]


def test_become_manager_with_takeover_rejects_unknown_handoff(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="no handoff with id"):
        become_manager_with_takeover_impl(
            claude_sid="mgr-new", takeover_from="mgr-old",
            handoff_id="bogus", iterm_sid="i1",
        )


def test_become_manager_with_takeover_drops_old_manager_questions(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    from dockwright.mcp_server import _write_question
    _write_question(worker_sid="mgr-old", worker_name="manager", question="dangling?")

    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda w: None)
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)

    become_manager_with_takeover_impl(
        claude_sid="mgr-new", takeover_from="mgr-old",
        handoff_id=handoff["handoff_id"], iterm_sid="i1",
    )
    remaining = [
        state.read_json(q) for q in paths.QUESTIONS.iterdir() if q.suffix == ".json"
    ]
    assert all(r is None or r.get("worker_sid") != "mgr-old" for r in remaining)


def test_become_manager_with_takeover_rejects_already_consumed(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="...", trigger_reason="manual")
    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda w: None)
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)
    become_manager_with_takeover_impl(
        claude_sid="mgr-new", takeover_from="mgr-old",
        handoff_id=handoff["handoff_id"], iterm_sid="i1",
    )
    with pytest.raises(ValueError, match="already consumed"):
        become_manager_with_takeover_impl(
            claude_sid="mgr-newest", takeover_from="mgr-old",
            handoff_id=handoff["handoff_id"], iterm_sid="i2",
        )


def test_become_manager_auto_suffixes_explicit_name_collision(fresh_orchestrator_dir):
    state.write_json_atomic(paths.ACTIVE / "stale-sid.json", {
        "claude_sid": "stale-sid",
        "agent": "manager",
        "name": "grumpy-yak",
        "cwd": "/x",
        "iterm_sid": "stale-iterm",
        "pid": os.getpid(),
        "started_at": 0,
        "domain": "general",
    })
    result = become_manager_impl(claude_sid="mgr-new", iterm_sid="i-new", name="grumpy-yak")
    assert result["ok"] is True
    assert result["name"] != "grumpy-yak" or state.read_json(paths.ACTIVE / "stale-sid.json") is None


import asyncio as _asyncio
from dockwright import spawner
from dockwright import terminal


class _FakeProc:
    returncode = 0

    async def communicate(self):
        return (b"42\n", b"")


def _patch_exec(monkeypatch):
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(spawner.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(paths, "ACCOUNT_ACTIVE", Path("/nonexistent/__no_account_active__"))
    return captured


def test_spawn_worker_forwards_extra_args(monkeypatch):
    captured = _patch_exec(monkeypatch)
    _asyncio.run(spawner.spawn_worker_tab(
        cwd="/tmp/x",
        initial_prompt="hello",
        name="alpha",
        extra_args=[*FLAGS, "--dangerously-skip-permissions"],
    ))
    inner_cmd = captured["args"][-1]
    assert "--dangerously-skip-permissions" in inner_cmd
    assert inner_cmd.index("--dangerously-skip-permissions") < inner_cmd.index('"$(cat ')
    launch_pos = inner_cmd.rindex(" launch ")
    assert launch_pos < inner_cmd.index("--dangerously-skip-permissions")


def _spawn_fable(monkeypatch, fable_left, extra_args, force=False, pick=None):
    captured = _patch_exec(monkeypatch)
    picks = []
    monkeypatch.setattr(spawner, "_fable_left", fable_left)
    monkeypatch.setattr(spawner, "_pick_account",
                        lambda force=False, fable=False: picks.append(fable) or pick)
    _asyncio.run(spawner.spawn_worker_tab(
        extra_args=extra_args, cwd="/tmp/x", initial_prompt="hi", name="alpha", force=force))
    return captured["args"][-1], picks


FABLE_FLAGS = ["--model", "@fable[1m]", "--effort", "medium"]


def test_spawn_fable_with_no_fable_left_launches_opus_at_xhigh(monkeypatch):
    seen = []
    inner_cmd, picks = _spawn_fable(
        monkeypatch, lambda now, force=False: seen.append(force) or False, FABLE_FLAGS, force=True)
    assert models.resolve_model("@opus[1m]") in inner_cmd
    assert models.families()["fable"] not in inner_cmd
    assert "--effort xhigh" in inner_cmd and "--effort medium" not in inner_cmd
    assert seen == [True]
    assert picks == [False]


def test_spawn_fable_with_fable_left_keeps_fable_and_picks_a_fable_account(monkeypatch):
    inner_cmd, picks = _spawn_fable(monkeypatch, lambda now, force=False: True, FABLE_FLAGS)
    assert models.resolve_model("@fable[1m]") in inner_cmd
    assert "--effort medium" in inner_cmd
    assert picks == [True]


def test_spawn_fable_whose_launch_account_has_spent_fable_launches_opus(monkeypatch):
    monkeypatch.setattr(spawner, "_resolve_account", lambda letter: ("a", None))
    monkeypatch.setattr(spawner, "_fable_spent", lambda letter, now: letter == "a")
    inner_cmd, picks = _spawn_fable(monkeypatch, lambda now, force=False: True, FABLE_FLAGS, pick="b")
    assert "CLAUDE_ORCH_ACCOUNT=a" in inner_cmd
    assert models.resolve_model("@opus[1m]") in inner_cmd
    assert models.families()["fable"] not in inner_cmd
    assert "--effort xhigh" in inner_cmd
    assert picks == [True]


def test_spawn_fable_with_no_pool_pick_checks_the_default_account(monkeypatch):
    default = _config.default_account()
    monkeypatch.setattr(spawner, "_fable_spent", lambda letter, now: letter == default)
    inner_cmd, picks = _spawn_fable(monkeypatch, lambda now, force=False: True, FABLE_FLAGS)
    assert models.resolve_model("@opus[1m]") in inner_cmd
    assert models.families()["fable"] not in inner_cmd
    assert picks == [True]


def test_spawn_opus_never_reads_fable_usage(monkeypatch):
    def _no_read(now, force=False):
        raise AssertionError("a non-Fable spawn must not read the Fable windows")

    inner_cmd, picks = _spawn_fable(monkeypatch, _no_read, FLAGS)
    assert models.resolve_model("@opus") in inner_cmd
    assert picks == [False]


def test_spawn_worker_forwards_env(monkeypatch):
    captured = _patch_exec(monkeypatch)
    _asyncio.run(spawner.spawn_worker_tab(
        extra_args=FLAGS, cwd="/tmp/x",
        initial_prompt="hi",
        name="alpha",
        env={"MY_VAR": "1"},
    ))
    inner_cmd = captured["args"][-1]
    assert "MY_VAR=1" in inner_cmd
    assert "CLAUDE_AGENT=worker" in inner_cmd
    assert "CLAUDE_WORKER_NAME=alpha" in inner_cmd
    assert "CLAUDE_WORKER_RUNTIME=claude" in inner_cmd


def test_spawn_worker_caller_cannot_override_orchestrator_env(monkeypatch):
    captured = _patch_exec(monkeypatch)
    _asyncio.run(spawner.spawn_worker_tab(
        extra_args=FLAGS, cwd="/tmp/x",
        initial_prompt="hi",
        name="alpha",
        env={
            "CLAUDE_AGENT": "manager",
            "CLAUDE_WORKER_NAME": "evil",
            "CLAUDE_WORKER_RUNTIME": "codex",
        },
    ))
    inner_cmd = captured["args"][-1]
    assert "CLAUDE_AGENT=worker" in inner_cmd
    assert "CLAUDE_AGENT=manager" not in inner_cmd
    assert "CLAUDE_WORKER_NAME=alpha" in inner_cmd
    assert "CLAUDE_WORKER_NAME=evil" not in inner_cmd
    assert "CLAUDE_WORKER_RUNTIME=claude" in inner_cmd
    assert "CLAUDE_WORKER_RUNTIME=codex" not in inner_cmd


def test_spawn_worker_defaults_unchanged_when_new_params_omitted(monkeypatch):
    captured = _patch_exec(monkeypatch)
    _asyncio.run(spawner.spawn_worker_tab(
        extra_args=FLAGS, cwd="/tmp/x",
        initial_prompt="hi",
        name="alpha",
    ))
    inner_cmd = captured["args"][-1]
    prompt_file = next(paths.PROMPT_FILES.glob("*.txt"))
    assert inner_cmd.rstrip().endswith(
        f"{shlex.quote(str(paths.dockwright_bin()))} launch "
        "--model " + models.families()["opus"] + " --effort xhigh "
        "--disallowedTools=Artifact,SendFeedback,Workflow,AskUserQuestion,ReportFindings,ScheduleWakeup "
        + f'"$(cat {shlex.quote(str(prompt_file))})"')
    assert prompt_file.read_text() == "hi"


def test_spawn_worker_codex_runtime_builds_codex_command(monkeypatch, tmp_path):
    allow_codex(monkeypatch, tmp_path)
    captured = _patch_exec(monkeypatch)
    _asyncio.run(spawner.spawn_worker_tab(
        cwd="/tmp/x",
        initial_prompt="hello",
        name="alpha",
        runtime="codex",
        extra_args=["--model", "gpt-5.5"],
    ))
    inner_cmd = captured["args"][-1]
    assert "CLAUDE_WORKER_RUNTIME=codex" in inner_cmd
    assert " codex --ask-for-approval never --sandbox danger-full-access --dangerously-bypass-hook-trust --model gpt-5.5 " in inner_cmd
    assert "You are an orchestrator worker running in a separate tmux window" in inner_cmd
    assert "Task:" in inner_cmd
    assert "hello" in inner_cmd
    assert "--settings" not in inner_cmd


def test_spawn_manager_builds_claude_command_without_runtime_env(monkeypatch):
    captured = _patch_exec(monkeypatch)
    _asyncio.run(spawner.spawn_worker_tab(
        cwd="/tmp/x",
        initial_prompt="/manager-resume h1",
        name="manager",
        agent="manager",
    ))
    inner_cmd = captured["args"][-1]
    assert "CLAUDE_AGENT=manager" in inner_cmd
    assert "CLAUDE_MANAGER_RUNTIME" not in inner_cmd
    assert "CLAUDE_WORKER_RUNTIME" not in inner_cmd
    prompt_file = next(paths.PROMPT_FILES.glob("*.txt"))
    assert prompt_file.read_text() == "/manager-resume h1"
    assert f'"$(cat {shlex.quote(str(prompt_file))})"' in inner_cmd
    assert "You are an orchestrator worker running in a separate tmux window" not in inner_cmd


def test_spawn_worker_codex_rejects_claude_only_extra_args(monkeypatch, tmp_path):
    allow_codex(monkeypatch, tmp_path)
    _patch_exec(monkeypatch)
    with pytest.raises(ValueError, match="runtime='codex'.*Claude-only"):
        _asyncio.run(spawner.spawn_worker_tab(
            cwd="/tmp/x",
            initial_prompt="hello",
            name="alpha",
            runtime="codex",
            extra_args=["--settings", "{}"],
        ))


def test_spawn_worker_codex_rejects_default_overrides(monkeypatch, tmp_path):
    allow_codex(monkeypatch, tmp_path)
    _patch_exec(monkeypatch)
    with pytest.raises(ValueError, match="cannot override orchestrator Codex defaults"):
        _asyncio.run(spawner.spawn_worker_tab(
            cwd="/tmp/x",
            initial_prompt="hello",
            name="alpha",
            runtime="codex",
            extra_args=["--sandbox", "workspace-write"],
        ))


@pytest.mark.parametrize("extra_arg", ["-sworkspace-write", "-aon-request"])
def test_spawn_worker_codex_rejects_compact_default_overrides(monkeypatch, tmp_path, extra_arg):
    allow_codex(monkeypatch, tmp_path)
    _patch_exec(monkeypatch)
    with pytest.raises(ValueError, match="cannot override orchestrator Codex defaults"):
        _asyncio.run(spawner.spawn_worker_tab(
            cwd="/tmp/x",
            initial_prompt="hello",
            name="alpha",
            runtime="codex",
            extra_args=[extra_arg],
        ))


def test_spawn_worker_unknown_runtime_rejected(monkeypatch):
    _patch_exec(monkeypatch)
    with pytest.raises(ValueError, match="unsupported runtime"):
        _asyncio.run(spawner.spawn_worker_tab(
            cwd="/tmp/x",
            initial_prompt="hello",
            name="alpha",
            runtime="gemini",
        ))


def test_spawn_worker_target_window_match_adds_match_flag(monkeypatch):
    terminal._DRIVER = None
    captured = _patch_exec(monkeypatch)
    _asyncio.run(spawner.spawn_worker_tab(
        extra_args=FLAGS, cwd="/tmp/x",
        initial_prompt="hi",
        name="alpha",
        target_window_match="window_id:42",
    ))
    argv = list(captured["args"])
    assert "new-window" in argv
    assert "-t" in argv and argv[argv.index("-t") + 1] == "window_id:42"


def test_spawn_worker_no_match_flag_when_target_window_match_unset(monkeypatch):
    terminal._DRIVER = None
    captured = _patch_exec(monkeypatch)
    _asyncio.run(spawner.spawn_worker_tab(
        extra_args=FLAGS, cwd="/tmp/x",
        initial_prompt="hi",
        name="alpha",
    ))
    argv = list(captured["args"])
    assert "new-window" in argv
    assert "-t" not in argv


def test_spawn_worker_route_to_workers_window_ignores_target_window_match(monkeypatch):
    terminal._DRIVER = None
    captured = _patch_exec(monkeypatch)

    async def fake_find(self):
        return "%99"

    monkeypatch.setattr(terminal.TmuxDriver, "find_group_pane", fake_find)
    _asyncio.run(spawner.spawn_worker_tab(
        extra_args=FLAGS, cwd="/tmp/x",
        initial_prompt="hi",
        name="alpha",
        route_to_workers_window=True,
        target_window_match="window_id:42",
    ))
    argv = list(captured["args"])
    assert "new-window" in argv
    assert argv[argv.index("-t") + 1] == terminal.WORKERS_OS_WINDOW_CLASS
    assert "window_id:42" not in argv


def test_spawn_worker_tab_manager_routes_to_mgr_session(monkeypatch):
    import asyncio as _asyncio2
    from dockwright import spawner as _spawner

    monkeypatch.setattr(paths, "ACCOUNT_ACTIVE", Path("/nonexistent/__no_account_active__"))

    captured_spawn_kwargs: dict = {}

    class FakeDrv:
        async def spawn(self, **kw):
            captured_spawn_kwargs.update(kw)
            return "%9"

    monkeypatch.setattr(_spawner, "get_driver", lambda: FakeDrv())

    _asyncio2.run(_spawner.spawn_worker_tab(
        extra_args=FLAGS, cwd="/tmp/x",
        initial_prompt="/manager-resume x",
        name="m",
        agent="manager",
    ))
    assert captured_spawn_kwargs.get("route_to_manager_session") is True, (
        f"expected route_to_manager_session=True for agent='manager', got: {captured_spawn_kwargs}"
    )

    captured_spawn_kwargs.clear()
    _asyncio2.run(_spawner.spawn_worker_tab(
        extra_args=FLAGS, cwd="/tmp/x",
        initial_prompt="do work",
        name="w",
        agent="worker",
    ))
    assert captured_spawn_kwargs.get("route_to_manager_session") is False, (
        f"expected route_to_manager_session=False for agent='worker', got: {captured_spawn_kwargs}"
    )


def _enable_pool(monkeypatch, tmp_path, letter="a"):
    pointer = tmp_path / "account-active"
    pointer.write_text(f"{letter}\n")
    monkeypatch.setattr(paths, "ACCOUNT_ACTIVE", pointer)
    monkeypatch.setattr(paths, "SPAWN_COUNTER", tmp_path / "spawn-counter.json")
    monkeypatch.setattr(paths, "ACCOUNT_STATE", tmp_path / "account-state.json")
    monkeypatch.setattr(paths, "ACCOUNT_USAGE", tmp_path / "usage")
    def _fake_farm(letter):
        d = tmp_path / f".claude-{letter}"
        d.mkdir(parents=True, exist_ok=True)
        (d / ".claude.json").write_text(
            '{"mcpServers": {"claude-orchestrator": {"command": "orchestrator"}}}'
        )
        return d
    monkeypatch.setattr(spawner, "ensure_account_config_dir", _fake_farm)
    monkeypatch.setattr(paths, "account_config_dir", lambda letter: tmp_path / f".claude-{letter}")

    def fake_run(args, **kwargs):
        if isinstance(args, (list, tuple)) and args and args[0] == "security":
            raise AssertionError(f"login model must not call security: {args}")
        return subprocess.CompletedProcess(args, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(spawner.subprocess, "run", fake_run)
    return pointer


def test_spawn_worker_account_a_default(monkeypatch, tmp_path):
    captured = _patch_exec(monkeypatch)
    _enable_pool(monkeypatch, tmp_path, letter="a")
    _asyncio.run(spawner.spawn_worker_tab(
        extra_args=FLAGS, cwd="/tmp/x",
        initial_prompt="hi",
        name="alpha",
    ))
    inner_cmd = captured["args"][-1]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in inner_cmd
    assert "CLAUDE_CONFIG_DIR" not in inner_cmd
    assert "CLAUDE_ORCH_ACCOUNT=a" in inner_cmd


def test_spawn_worker_account_b_config_dir(monkeypatch, tmp_path):
    captured = _patch_exec(monkeypatch)
    _enable_pool(monkeypatch, tmp_path, letter="b")
    monkeypatch.setattr(spawner, "_pick_account", lambda force=False, fable=False: "b")
    _asyncio.run(spawner.spawn_worker_tab(
        extra_args=FLAGS, cwd="/tmp/x",
        initial_prompt="hi",
        name="alpha",
    ))
    inner_cmd = captured["args"][-1]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in inner_cmd
    assert f"CLAUDE_CONFIG_DIR={tmp_path}/.claude-b" in inner_cmd
    assert "CLAUDE_ORCH_ACCOUNT=b" in inner_cmd
    assert "$(security" not in inner_cmd


def test_spawn_manager_rides_pointer_a(monkeypatch, tmp_path):
    captured = _patch_exec(monkeypatch)
    _enable_pool(monkeypatch, tmp_path, letter="a")
    _asyncio.run(spawner.spawn_worker_tab(
        cwd="/tmp/x",
        initial_prompt="hi",
        name="mgr",
        agent="manager",
    ))
    inner_cmd = captured["args"][-1]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in inner_cmd
    assert "CLAUDE_CONFIG_DIR" not in inner_cmd
    assert "CLAUDE_ORCH_ACCOUNT=a" in inner_cmd


def test_spawn_manager_rides_pointer_b(monkeypatch, tmp_path):
    captured = _patch_exec(monkeypatch)
    _enable_pool(monkeypatch, tmp_path, letter="b")
    _install_two_pool(monkeypatch, tmp_path)
    _asyncio.run(spawner.spawn_worker_tab(
        cwd="/tmp/x",
        initial_prompt="hi",
        name="mgr",
        agent="manager",
    ))
    inner_cmd = captured["args"][-1]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in inner_cmd
    assert f"CLAUDE_CONFIG_DIR={tmp_path}/.claude-b" in inner_cmd
    assert "CLAUDE_ORCH_ACCOUNT=b" in inner_cmd


def test_spawn_omits_prefix_without_pointer(monkeypatch):
    captured = _patch_exec(monkeypatch)
    _asyncio.run(spawner.spawn_worker_tab(
        extra_args=FLAGS, cwd="/tmp/x",
        initial_prompt="hi",
        name="alpha",
    ))
    inner_cmd = captured["args"][-1]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in inner_cmd
    assert "CLAUDE_ORCH_ACCOUNT" not in inner_cmd


def test_spawn_omits_prefix_on_invalid_pointer(monkeypatch, tmp_path):
    captured = _patch_exec(monkeypatch)
    _enable_pool(monkeypatch, tmp_path, letter="z")
    _asyncio.run(spawner.spawn_worker_tab(extra_args=FLAGS, cwd="/tmp/x", initial_prompt="hi", name="alpha"))
    inner_cmd_z = captured["args"][-1]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in inner_cmd_z
    assert "CLAUDE_ORCH_ACCOUNT" not in inner_cmd_z


def test_caller_token_disables_pool_injection(monkeypatch, tmp_path):
    captured = _patch_exec(monkeypatch)
    _enable_pool(monkeypatch, tmp_path, letter="a")
    _asyncio.run(spawner.spawn_worker_tab(
        extra_args=FLAGS, cwd="/tmp/x",
        initial_prompt="hi",
        name="alpha",
        env={
            "CLAUDE_ORCH_ACCOUNT": "b",
            "CLAUDE_CODE_OAUTH_TOKEN": "caller-token",
        },
    ))
    inner_cmd = captured["args"][-1]
    assert "CLAUDE_CODE_OAUTH_TOKEN=caller-token" in inner_cmd
    assert "$(security" not in inner_cmd
    assert "CLAUDE_ORCH_ACCOUNT" not in inner_cmd


def test_caller_config_dir_dropped_picker_wins(monkeypatch, tmp_path):
    captured = _patch_exec(monkeypatch)
    _enable_pool(monkeypatch, tmp_path, letter="b")
    monkeypatch.setattr(spawner, "_pick_account", lambda force=False, fable=False: "b")
    _asyncio.run(spawner.spawn_worker_tab(
        extra_args=FLAGS, cwd="/tmp/x",
        initial_prompt="hi",
        name="alpha",
        env={"CLAUDE_CONFIG_DIR": "/tmp/evil"},
    ))
    inner_cmd = captured["args"][-1]
    assert "/tmp/evil" not in inner_cmd, "caller CLAUDE_CONFIG_DIR must be dropped"
    assert f"CLAUDE_CONFIG_DIR={tmp_path}/.claude-b" in inner_cmd, "picker's farm wins"
    assert inner_cmd.count("CLAUDE_CONFIG_DIR=") == 1, "only the picker's assignment survives"
    assert "CLAUDE_ORCH_ACCOUNT=b" in inner_cmd


def _patch_terminal_ls(monkeypatch, stdout: bytes, returncode: int = 0):
    class _LsProc:
        def __init__(self):
            self.returncode = returncode

        async def communicate(self):
            return (stdout, b"")

    async def fake_exec(*args, **kwargs):
        return _LsProc()

    monkeypatch.setattr(spawner.asyncio, "create_subprocess_exec", fake_exec)


def _panes_stdout(pane_ids):
    return ("\n".join(str(p) for p in pane_ids) + "\n").encode()


def test_window_id_exists_true_when_present(monkeypatch):
    terminal._DRIVER = None
    _patch_terminal_ls(monkeypatch, _panes_stdout(["7", "42"]))
    assert _asyncio.run(spawner.window_id_exists("42")) is True


def test_window_id_exists_false_when_absent(monkeypatch):
    _patch_terminal_ls(monkeypatch, _panes_stdout(["7", "8"]))
    assert _asyncio.run(spawner.window_id_exists("42")) is False


def test_window_id_exists_false_on_garbage(monkeypatch):
    _patch_terminal_ls(monkeypatch, b"tmux: command produced no pane ids")
    assert _asyncio.run(spawner.window_id_exists("42")) is False


def test_window_id_exists_false_on_nonzero_returncode(monkeypatch):
    _patch_terminal_ls(monkeypatch, _panes_stdout(["42"]), returncode=1)
    assert _asyncio.run(spawner.window_id_exists("42")) is False


def test_window_id_exists_matches_exact_pane_id(monkeypatch):
    terminal._DRIVER = None
    _patch_terminal_ls(monkeypatch, _panes_stdout(["42"]))
    assert _asyncio.run(spawner.window_id_exists("42")) is True


from dockwright.mcp_server import spawn_worker_impl
from dockwright.mcp_server import _repo_sync_footer


def _patch_spawn_worker_tab(monkeypatch):
    captured: dict = {}

    async def fake_spawn(**kwargs):
        captured.update(kwargs)
        return ("999", kwargs.get("name", ""))

    monkeypatch.setattr(spawner, "spawn_worker_tab", fake_spawn)
    return captured


from dockwright.mcp_server import _confirm_spawn_registration as _confirm_reg


def test_confirm_spawn_registration_finds_worker_by_name(fresh_orchestrator_dir):
    state.write_json_atomic(paths.ACTIVE / "sid-x.json", {
        "claude_sid": "sid-x", "agent": "worker", "name": "needle", "cwd": "/tmp"})
    rec = _asyncio.run(_confirm_reg("needle", timeout_sec=1.0, poll_interval=0.01))
    assert rec is not None and rec["claude_sid"] == "sid-x"


def test_confirm_spawn_registration_times_out_when_absent(fresh_orchestrator_dir):
    rec = _asyncio.run(_confirm_reg("ghost", timeout_sec=0.1, poll_interval=0.01))
    assert rec is None


def test_spawn_worker_impl_reports_registered(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    _patch_spawn_registers_active(monkeypatch)
    result = _asyncio.run(spawn_worker_impl(
        initial_prompt="task", name="reg-worker", cwd="/tmp/x",
        _registration_timeout_sec=2.0, _poll_interval=0.01))
    assert result["status"] == "registered"
    assert result["claude_sid"] == "spawned-reg-worker"
    assert result["window_id"] == "999"
    assert state.read_json(paths.pending_assignment_path(result["assignment_id"]))["name"] == "reg-worker"


def test_spawn_worker_impl_reports_no_register(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    _patch_spawn_worker_tab(monkeypatch)
    result = _asyncio.run(spawn_worker_impl(
        initial_prompt="task", name="lost-worker", cwd="/tmp/x",
        _registration_timeout_sec=0.2, _poll_interval=0.01))
    assert result["status"] == "no_register"
    assert result["window_id"] == "999"
    assert result["assignment_id"]
    assert "did not register" in result["reason"]
    assert paths.pending_assignment_path(result["assignment_id"]).exists()


from dockwright.mcp_server import spawn_replacement_manager_impl


def _patch_window_id_exists(monkeypatch, exists):
    async def fake(_wid):
        return exists

    monkeypatch.setattr(spawner, "window_id_exists", fake)


def test_spawn_replacement_manager_targets_old_manager_window(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="42")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    captured = _patch_spawn_worker_tab(monkeypatch)
    _patch_window_id_exists(monkeypatch, True)
    result = _asyncio.run(spawn_replacement_manager_impl(handoff["handoff_id"]))
    assert captured["target_window_match"] == "window_id:42"
    assert captured["runtime"] == "claude"
    assert result["runtime"] == "claude"


def test_spawn_replacement_manager_pins_cwd_to_manager_home(fresh_orchestrator_dir, monkeypatch, tmp_path):
    mgr_home = tmp_path / "mgr-home"
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(mgr_home))
    become_manager_impl(claude_sid="mgr-old", iterm_sid="42")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    captured = _patch_spawn_worker_tab(monkeypatch)
    _patch_window_id_exists(monkeypatch, True)
    _asyncio.run(spawn_replacement_manager_impl(handoff["handoff_id"]))
    assert captured["cwd"] == str(mgr_home)
    assert mgr_home.is_dir()


def test_become_manager_refuses_parent_of_manager_home(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work_root = tmp_path / "work"
    (work_root / "manager").mkdir(parents=True)
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(work_root / "manager"))
    monkeypatch.chdir(work_root)
    with pytest.raises(ValueError, match="refusing to start a manager"):
        become_manager_impl(claude_sid="mgr-root", iterm_sid="i1")
    assert not (paths.ACTIVE / "mgr-root.json").exists()


def test_become_manager_records_manager_home_cwd(fresh_orchestrator_dir, monkeypatch, tmp_path):
    work_root = tmp_path / "work"
    (work_root / "manager").mkdir(parents=True)
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(work_root / "manager"))
    monkeypatch.chdir(work_root / "manager")
    become_manager_impl(claude_sid="mgr-home", iterm_sid="i1")
    assert state.read_json(paths.ACTIVE / "mgr-home.json")["cwd"] == str(work_root / "manager")


def test_spawn_replacement_manager_pins_manager_model(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="42")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    captured = _patch_spawn_worker_tab(monkeypatch)
    _patch_window_id_exists(monkeypatch, True)
    _asyncio.run(spawn_replacement_manager_impl(handoff["handoff_id"]))
    assert captured["extra_args"] == ["--model", models.families()["fable"] + "[1m]"]


def test_spawn_replacement_manager_reads_a_pin_moved_after_the_server_started(
        fresh_orchestrator_dir, monkeypatch, model_pins):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="42")
    captured = _patch_spawn_worker_tab(monkeypatch)
    _patch_window_id_exists(monkeypatch, True)
    first = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    _asyncio.run(spawn_replacement_manager_impl(first["handoff_id"]))
    assert captured["extra_args"][-2:] == ["--model", "claude-opus-7-1[1m]"]
    model_pins('opus = "claude-opus-7-1"', 'opus = "claude-opus-9-9"')
    second = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    _asyncio.run(spawn_replacement_manager_impl(second["handoff_id"]))
    assert captured["extra_args"][-2:] == ["--model", "claude-opus-9-9[1m]"]


def test_spawn_replacement_manager_inherits_predecessor_funny_name(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="42")
    mgr_name = state.read_json(paths.ACTIVE / "mgr-old.json")["name"]
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    captured = _patch_spawn_worker_tab(monkeypatch)
    _patch_window_id_exists(monkeypatch, True)
    _asyncio.run(spawn_replacement_manager_impl(handoff["handoff_id"]))
    assert captured["name"] == mgr_name
    assert captured["name"] != "manager"
    assert captured["agent"] == "manager"


def test_spawn_replacement_manager_passes_empty_name_when_none_recorded(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="42")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    handoff_path = paths.HANDOFFS / f"{handoff['handoff_id']}.json"
    handoff_record = state.read_json(handoff_path)
    handoff_record["manager_name"] = None
    state.write_json_atomic(handoff_path, handoff_record)
    captured = _patch_spawn_worker_tab(monkeypatch)
    _patch_window_id_exists(monkeypatch, True)
    _asyncio.run(spawn_replacement_manager_impl(handoff["handoff_id"]))
    assert captured["name"] == ""


def test_spawn_replacement_manager_falls_back_when_window_dead(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="42")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    captured = _patch_spawn_worker_tab(monkeypatch)
    _patch_window_id_exists(monkeypatch, False)
    _asyncio.run(spawn_replacement_manager_impl(handoff["handoff_id"]))
    assert captured["target_window_match"] is None


def test_spawn_replacement_manager_falls_back_when_active_record_missing(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="42")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    (paths.ACTIVE / "mgr-old.json").unlink()
    captured = _patch_spawn_worker_tab(monkeypatch)
    _patch_window_id_exists(monkeypatch, True)
    _asyncio.run(spawn_replacement_manager_impl(handoff["handoff_id"]))
    assert captured["target_window_match"] is None


from dockwright.mcp_server import _resolve_old_manager_window_match


def test_resolve_old_manager_window_match_falls_back_when_iterm_sid_empty(fresh_orchestrator_dir):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="42")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    record = state.read_json(paths.ACTIVE / "mgr-old.json")
    record.pop("window_id", None)
    record["iterm_sid"] = ""
    state.write_json_atomic(paths.ACTIVE / "mgr-old.json", record)
    assert _asyncio.run(_resolve_old_manager_window_match(handoff)) is None


def test_spawn_worker_preset_prepended(fresh_orchestrator_dir, monkeypatch):
    paths.PRESETS.mkdir(parents=True, exist_ok=True)
    (paths.PRESETS / "fake.md").write_text("PRESET BODY: rebase first; tests must pass")
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="real task",
        name="preset-test",
        cwd="/tmp/x",
        preset="fake",
    ))
    assembled = captured["initial_prompt"]
    assert assembled.startswith("PRESET BODY: rebase first; tests must pass")
    assert "\n\n---\n\n" in assembled
    assert assembled.endswith("real task" + _repo_sync_footer())


def test_spawn_worker_preset_missing_raises(fresh_orchestrator_dir, monkeypatch):
    paths.PRESETS.mkdir(parents=True, exist_ok=True)
    (paths.PRESETS / "exists.md").write_text("X")
    _patch_spawn_worker_tab(monkeypatch)
    with pytest.raises(ValueError, match="preset 'nonexistent' not found") as exc_info:
        _asyncio.run(spawn_worker_impl(
            initial_prompt="real task",
            name="missing-test",
            cwd="/tmp/x",
            preset="nonexistent",
        ))
    assert "'exists'" in str(exc_info.value)


def test_spawn_worker_preset_none_unchanged(fresh_orchestrator_dir, monkeypatch):
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="real task",
        name="no-preset-test",
        cwd="/tmp/x",
    ))
    assert captured["initial_prompt"] == "real task" + _repo_sync_footer()


REMOTE_OFF_FLAGS = ["--settings", '{"enableAllProjectMcpServers": true, "remoteControlAtStartup": false, "disableRemoteControl": true}']
RC_ON_FLAGS = ["--settings", '{"enableAllProjectMcpServers": true}', "--remote-control"]


def test_spawn_worker_disables_remote_control(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    captured = _patch_spawn_worker_tab(monkeypatch)
    result = _asyncio.run(spawn_worker_impl(
        initial_prompt="task",
        name="remote-off-test",
        cwd="/tmp/x",
    ))
    assert result["runtime"] == "claude"
    assert captured["runtime"] == "claude"
    assert captured["extra_args"][:2] == REMOTE_OFF_FLAGS
    assert "--remote-control" not in captured["extra_args"]


def test_spawn_worker_disables_remote_appends_caller_extra_args(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="task",
        name="remote-off-append-test",
        cwd="/tmp/x",
        extra_args=["--dangerously-skip-permissions"],
    ))
    assert captured["extra_args"] == REMOTE_OFF_FLAGS + ["--dangerously-skip-permissions"]


def test_spawn_worker_impl_codex_runtime_skips_claude_remote_flags(fresh_orchestrator_dir, monkeypatch,
                                                                   tmp_path):
    allow_codex(monkeypatch, tmp_path)
    captured = _patch_spawn_worker_tab(monkeypatch)
    result = _asyncio.run(spawn_worker_impl(
        initial_prompt="task",
        name="codex-runtime-test",
        cwd="/tmp/x",
        runtime="codex",
        extra_args=["--model", "gpt-5.5"],
    ))
    assert result["runtime"] == "codex"
    assert captured["runtime"] == "codex"
    assert captured["extra_args"] == ["--model", "gpt-5.5"]
    assert "--settings" not in captured["extra_args"]


from dockwright.mcp_server import _claude_worker_settings_args


def test_claude_rc_args_default_keeps_remote_off(monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    monkeypatch.setattr(_config, "worker_headless_preset", lambda: False)
    assert _claude_worker_settings_args() == REMOTE_OFF_FLAGS
    assert "--remote-control" not in _claude_worker_settings_args()


@pytest.mark.parametrize("val", ["0", "", "true", "yes", " ", "2", "01", "1x"])
def test_claude_rc_args_non_one_values_keep_remote_off(monkeypatch, val):
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_RC", val)
    monkeypatch.setattr(_config, "worker_headless_preset", lambda: False)
    assert _claude_worker_settings_args() == REMOTE_OFF_FLAGS


@pytest.mark.parametrize("val", ["1", " 1 "])
def test_claude_rc_args_enables_remote_when_opted_in(monkeypatch, val):
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_RC", val)
    monkeypatch.setattr(_config, "worker_headless_preset", lambda: False)
    assert _claude_worker_settings_args() == RC_ON_FLAGS
    assert "--remote-control" in _claude_worker_settings_args()
    assert "remoteControlAtStartup" not in _claude_worker_settings_args()[1]


def test_spawn_worker_opt_in_enables_remote_control(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_RC", "1")
    captured = _patch_spawn_worker_tab(monkeypatch)
    result = _asyncio.run(spawn_worker_impl(
        initial_prompt="task",
        name="rc-on-test",
        cwd="/tmp/x",
    ))
    assert result["runtime"] == "claude"
    assert captured["extra_args"][:3] == RC_ON_FLAGS
    assert "--remote-control" in captured["extra_args"]
    assert "remoteControlAtStartup" not in captured["extra_args"][1]


def test_spawn_worker_opt_in_appends_caller_extra_args(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_RC", "1")
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="task",
        name="rc-on-append",
        cwd="/tmp/x",
        extra_args=["--dangerously-skip-permissions"],
    ))
    assert captured["extra_args"] == RC_ON_FLAGS + ["--dangerously-skip-permissions"]


from dockwright import mcp_server

HEADLESS_PRESET_BODY = {
    "enableAllProjectMcpServers": True,
    "remoteControlAtStartup": False,
    "disableRemoteControl": True,
    "permissions": {"defaultMode": "auto", "allow": ["Bash(printenv:*)"]},
}


def _write_headless_preset(tmp_path, monkeypatch, body=None):
    presets = tmp_path / "presets"
    presets.mkdir(parents=True, exist_ok=True)
    preset = presets / "worker-headless-settings.json"
    preset.write_text(json.dumps(body if body is not None else HEADLESS_PRESET_BODY))
    monkeypatch.setattr(mcp_server.paths, "PRESETS", presets)
    return preset


def test_settings_args_default_uses_preset_path(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    monkeypatch.setattr(mcp_server.config, "worker_headless_preset", lambda: True)
    preset = _write_headless_preset(tmp_path, monkeypatch)
    assert mcp_server._claude_worker_settings_args() == ["--settings", str(preset)]


def test_settings_args_knob_off_falls_back_inline(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    monkeypatch.setattr(mcp_server.config, "worker_headless_preset", lambda: False)
    _write_headless_preset(tmp_path, monkeypatch)
    args = mcp_server._claude_worker_settings_args()
    assert args[0] == "--settings"
    settings = json.loads(args[1])
    assert settings == {"enableAllProjectMcpServers": True,
                        "remoteControlAtStartup": False,
                        "disableRemoteControl": True}


def test_settings_args_missing_preset_falls_back_inline(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    monkeypatch.setattr(mcp_server.config, "worker_headless_preset", lambda: True)
    monkeypatch.setattr(mcp_server.paths, "PRESETS", tmp_path / "nope")
    args = mcp_server._claude_worker_settings_args()
    assert json.loads(args[1])["enableAllProjectMcpServers"] is True


def test_settings_args_unparseable_preset_falls_back_inline(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    monkeypatch.setattr(mcp_server.config, "worker_headless_preset", lambda: True)
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "worker-headless-settings.json").write_text("{corrupt")
    monkeypatch.setattr(mcp_server.paths, "PRESETS", presets)
    args = mcp_server._claude_worker_settings_args()
    assert json.loads(args[1])["enableAllProjectMcpServers"] is True


def test_settings_args_rc_merges_preset_inline(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_RC", "1")
    monkeypatch.setattr(mcp_server.config, "worker_headless_preset", lambda: True)
    _write_headless_preset(tmp_path, monkeypatch)
    args = mcp_server._claude_worker_settings_args()
    assert args[0] == "--settings" and args[2] == "--remote-control"
    merged = json.loads(args[1])
    assert "remoteControlAtStartup" not in merged
    assert "disableRemoteControl" not in merged
    assert merged["permissions"]["defaultMode"] == "auto"


def test_settings_args_rc_with_missing_preset_falls_back_legacy(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_RC", "1")
    monkeypatch.setattr(mcp_server.config, "worker_headless_preset", lambda: True)
    monkeypatch.setattr(mcp_server.paths, "PRESETS", tmp_path / "nope")
    args = mcp_server._claude_worker_settings_args()
    assert args == ["--settings", json.dumps({"enableAllProjectMcpServers": True}),
                    "--remote-control"]


def test_settings_args_caller_settings_suppresses_injection(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    monkeypatch.setattr(mcp_server.config, "worker_headless_preset", lambda: True)
    _write_headless_preset(tmp_path, monkeypatch)
    assert mcp_server._claude_worker_settings_args(["--settings", "/x.json"]) == []


def test_settings_args_caller_settings_keeps_rc_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_RC", "1")
    monkeypatch.setattr(mcp_server.config, "worker_headless_preset", lambda: True)
    _write_headless_preset(tmp_path, monkeypatch)
    assert mcp_server._claude_worker_settings_args(["--settings", "/x.json"]) == ["--remote-control"]


from dockwright.mcp_server import _legacy_inline_settings_args


def test_spawn_persists_verifier_settings_to_assignment(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="verify", name="verifier", cwd="/tmp/x",
        extra_args=["--settings", "/deployed/verifier-settings.json"]))
    (pending,) = list(paths.ASSIGNMENTS_PENDING.glob("*.json"))
    record = state.read_json(pending)
    assert record["spawn_extra_args"] == ["--settings", "/deployed/verifier-settings.json"]
    assert not any("worker-headless-settings.json" in a for a in record["spawn_extra_args"])


def test_spawn_persists_headless_preset_args_to_assignment(fresh_orchestrator_dir, tmp_path,
                                                           monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    monkeypatch.setattr(mcp_server.config, "worker_headless_preset", lambda: True)
    preset = _write_headless_preset(tmp_path, monkeypatch)
    _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(initial_prompt="build", name="w-bare", cwd="/tmp/x"))
    (pending,) = list(paths.ASSIGNMENTS_PENDING.glob("*.json"))
    record = state.read_json(pending)
    assert record["spawn_extra_args"] == ["--settings", str(preset)]


def test_resume_replays_verifier_spawn_extra_args(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    state.write_json_atomic(paths.CLOSED / "vsid.json", {
        "claude_sid": "vsid", "name": "verifier", "cwd": "/x", "runtime": "claude",
        "closed_at": 1.0})
    paths.ASSIGNMENTS.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.ASSIGNMENTS / "vsid.json", {
        "claude_sid": "vsid", "name": "verifier",
        "spawn_extra_args": ["--settings", "/deployed/verifier-settings.json"]})
    captured = {}

    async def fake_spawn(**kwargs):
        captured.update(kwargs)
        register_self_impl(claude_sid="vsid", agent="worker", name="verifier", cwd="/x",
                           iterm_sid="i7")
        return ("win-7", "verifier")

    _asyncio.run(_spawn_and_confirm_resume(
        fake_spawn, paths.CLOSED / "vsid.json", state.read_json(paths.CLOSED / "vsid.json"),
        "verifier", "vsid", "/x", 5.0, 0.05, model="@opus", effort="xhigh"))
    assert captured["extra_args"] == ["--settings", "/deployed/verifier-settings.json", *FLAGS]
    assert not any("worker-headless-settings.json" in a for a in captured["extra_args"])


def test_resume_replays_headless_preset_spawn_args(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    state.write_json_atomic(paths.CLOSED / "hsid.json", {
        "claude_sid": "hsid", "name": "w-bare", "cwd": "/x", "runtime": "claude", "closed_at": 1.0})
    paths.ASSIGNMENTS.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.ASSIGNMENTS / "hsid.json", {
        "claude_sid": "hsid", "name": "w-bare",
        "spawn_extra_args": ["--settings", "/deployed/worker-headless-settings.json"]})
    captured = {}

    async def fake_spawn(**kwargs):
        captured.update(kwargs)
        register_self_impl(claude_sid="hsid", agent="worker", name="w-bare", cwd="/x",
                           iterm_sid="i7")
        return ("win-7", "w-bare")

    _asyncio.run(_spawn_and_confirm_resume(
        fake_spawn, paths.CLOSED / "hsid.json", state.read_json(paths.CLOSED / "hsid.json"),
        "w-bare", "hsid", "/x", 5.0, 0.05, model="@opus", effort="xhigh"))
    assert captured["extra_args"] == ["--settings", "/deployed/worker-headless-settings.json", *FLAGS]


def test_resume_legacy_record_falls_back_to_manual_inline(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    state.write_json_atomic(paths.CLOSED / "lsid.json", {
        "claude_sid": "lsid", "name": "legacy", "cwd": "/x", "runtime": "claude", "closed_at": 1.0})
    captured = {}

    async def fake_spawn(**kwargs):
        captured.update(kwargs)
        register_self_impl(claude_sid="lsid", agent="worker", name="legacy", cwd="/x",
                           iterm_sid="i7")
        return ("win-7", "legacy")

    _asyncio.run(_spawn_and_confirm_resume(
        fake_spawn, paths.CLOSED / "lsid.json", state.read_json(paths.CLOSED / "lsid.json"),
        "legacy", "lsid", "/x", 5.0, 0.05, model="@opus", effort="xhigh"))
    assert captured["extra_args"] == [*_legacy_inline_settings_args(), *FLAGS]
    assert captured["extra_args"][:2] == REMOTE_OFF_FLAGS
    assert "permissions" not in captured["extra_args"][1]
    assert not any("worker-headless-settings.json" in a for a in captured["extra_args"])


def test_legacy_inline_settings_args_matches_old_main_fallback(monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    monkeypatch.setattr(mcp_server.config, "worker_headless_preset", lambda: False)
    assert _legacy_inline_settings_args() == mcp_server._claude_worker_settings_args()
    assert _legacy_inline_settings_args() == REMOTE_OFF_FLAGS


def test_resume_codex_stays_no_extra_args(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    state.write_json_atomic(paths.CLOSED / "cxsid.json", {
        "claude_sid": "cxsid", "name": "cx", "cwd": "/x", "runtime": "codex", "closed_at": 1.0})
    paths.ASSIGNMENTS.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.ASSIGNMENTS / "cxsid.json", {
        "claude_sid": "cxsid", "name": "cx", "spawn_extra_args": ["--model", "gpt-5.5"]})
    captured = {}

    async def fake_spawn(**kwargs):
        captured.update(kwargs)
        register_self_impl(claude_sid="cxsid", agent="worker", name="cx", cwd="/x", iterm_sid="i7")
        return ("win-7", "cx")

    _asyncio.run(_spawn_and_confirm_resume(
        fake_spawn, paths.CLOSED / "cxsid.json", state.read_json(paths.CLOSED / "cxsid.json"),
        "cx", "cxsid", "/x", 5.0, 0.05))
    assert captured["extra_args"] is None


@pytest.mark.parametrize("flag", [None, "1"])
def test_manager_spawn_unaffected_by_worker_rc_flag(fresh_orchestrator_dir, monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_ORCH_WORKER_RC", flag)
    become_manager_impl(claude_sid="mgr-old", iterm_sid="42")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    captured = _patch_spawn_worker_tab(monkeypatch)
    _patch_window_id_exists(monkeypatch, True)
    _asyncio.run(spawn_replacement_manager_impl(handoff["handoff_id"]))
    extra = captured.get("extra_args") or []
    assert extra == ["--model", models.families()["fable"] + "[1m]"]
    assert "--settings" not in extra
    assert all("remoteControlAtStartup" not in str(a) for a in extra)


def _write_usage_mcp(tmp_path, letter, pct5):
    udir = tmp_path / "usage"; udir.mkdir(parents=True, exist_ok=True)
    (udir / f"{letter}.json").write_text(_json.dumps({
        "five_hour_pct": pct5, "seven_day_pct": 0.0,
        "five_hour_resets_at": None, "seven_day_resets_at": None,
        "ts": _time.time()}))


def test_spawn_worker_impl_pauses_when_both_hot(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _enable_pool(monkeypatch, tmp_path, letter="a")
    captured = _patch_spawn_worker_tab(monkeypatch)
    _write_usage_mcp(tmp_path, "a", 96.0)
    _write_usage_mcp(tmp_path, "b", 97.0)
    result = _asyncio.run(spawn_worker_impl("hi", name="paused-one"))
    assert result["status"] == "paused"
    assert captured == {}
    assert not list(paths.ASSIGNMENTS_PENDING.glob("*.json"))


def test_spawn_worker_returns_the_held_gate(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _enable_pool(monkeypatch, tmp_path, letter="a")
    captured = _patch_spawn_worker_tab(monkeypatch)
    monkeypatch.setattr(spawner, "usage_spawn_gate",
                        lambda force=False: {"status": "held", "account": "a"})
    result = _asyncio.run(spawn_worker_impl("hi", name="held-one"))
    assert result == {"status": "held", "account": "a"}
    assert captured == {}
    assert not list(paths.ASSIGNMENTS_PENDING.glob("*.json"))


def test_spawn_worker_impl_force_bypasses_pause(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _enable_pool(monkeypatch, tmp_path, letter="a")
    captured = _patch_spawn_worker_tab(monkeypatch)
    _write_usage_mcp(tmp_path, "a", 96.0)
    _write_usage_mcp(tmp_path, "b", 97.0)
    result = _asyncio.run(spawn_worker_impl("hi", name="forced-one", force=True))
    assert result.get("status") != "paused"
    assert captured.get("force") is True


def test_spawn_worker_impl_default_spawns_without_usage(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _enable_pool(monkeypatch, tmp_path, letter="a")
    captured = _patch_spawn_worker_tab(monkeypatch)
    result = _asyncio.run(spawn_worker_impl("hi", name="normal-one"))
    assert result.get("status") != "paused"
    assert captured.get("name") == "normal-one"
    assert captured.get("force") is False


def test_spawn_worker_writes_window_sidecar(fresh_orchestrator_dir, monkeypatch):
    async def fake_spawn_tab(**kw):
        return ("777", None)
    monkeypatch.setattr(spawner, "spawn_worker_tab", fake_spawn_tab)
    _asyncio.run(spawn_worker_impl(name="w1", initial_prompt="do x", cwd="/tmp/wt"))
    sidecars = list(paths_module.ASSIGNMENTS_PENDING.glob("*.window"))
    assert len(sidecars) == 1 and sidecars[0].read_text() == "777"


def test_spawn_worker_default_cwd_uses_worker_home_when_present(fresh_orchestrator_dir, monkeypatch, tmp_path):
    captured = _patch_spawn_worker_tab(monkeypatch)
    home = tmp_path / "worker-home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_HOME", str(home))
    _asyncio.run(spawn_worker_impl(initial_prompt="poke", name="wh-present"))
    assert captured["cwd"] == str(home)

def test_spawn_worker_default_cwd_creates_worker_home_when_absent(fresh_orchestrator_dir, monkeypatch, tmp_path):
    captured = _patch_spawn_worker_tab(monkeypatch)
    absent = tmp_path / "projects" / "work" / "worker"
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_HOME", str(absent))
    monkeypatch.chdir(tmp_path)
    _asyncio.run(spawn_worker_impl(initial_prompt="poke", name="wh-absent"))
    assert captured["cwd"] == str(absent)
    assert absent.is_dir()


def test_spawn_worker_default_cwd_falls_back_to_getcwd_when_mkdir_fails(fresh_orchestrator_dir, monkeypatch, tmp_path):
    captured = _patch_spawn_worker_tab(monkeypatch)
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_HOME", str(blocker / "worker"))
    monkeypatch.chdir(tmp_path)
    _asyncio.run(spawn_worker_impl(initial_prompt="poke", name="wh-mkdir-fail"))
    assert captured["cwd"] == str(tmp_path)


def test_spawn_worker_explicit_cwd_unaffected_by_worker_home(fresh_orchestrator_dir, monkeypatch, tmp_path):
    captured = _patch_spawn_worker_tab(monkeypatch)
    home = tmp_path / "worker-home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_HOME", str(home))
    _asyncio.run(spawn_worker_impl(initial_prompt="poke", name="wh-explicit", cwd="/tmp/explicit"))
    assert captured["cwd"] == "/tmp/explicit"


def test_spawn_worker_mcp_signature_has_default_runtime():
    import inspect
    from dockwright.mcp_server import spawn_worker

    params = inspect.signature(spawn_worker).parameters
    assert params["runtime"].default == "claude"


def test_manager_mcp_signatures_have_no_runtime_param():
    import inspect
    from dockwright.mcp_server import become_manager, spawn_replacement_manager

    assert "runtime" not in inspect.signature(become_manager).parameters
    assert "runtime" not in inspect.signature(spawn_replacement_manager).parameters


from dockwright.mcp_server import wait_for_worker_impl


def _write_done_event(sid: str, worker_name: str, summary: str, completed_at: float | None = None) -> str:
    import uuid as _uuid
    event_id = _uuid.uuid4().hex
    paths.DONE.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.DONE / f"{sid}-{event_id}.json", {
        "event_id": event_id,
        "claude_sid": sid,
        "worker_name": worker_name,
        "summary": summary,
        "completed_at": completed_at if completed_at is not None else time.time(),
    })
    return event_id


def test_wait_for_worker_returns_existing_done(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="foo", cwd="/x", iterm_sid="i1", pid=os.getpid())
    event_id = _write_done_event(sid="w1", worker_name="foo", summary="task A complete")
    result = _asyncio.run(wait_for_worker_impl("foo", timeout_sec=60, _poll_interval=0.05))
    assert result["found"] == "done"
    assert result["name"] == "foo"
    assert result["sid"] == "w1"
    assert result["summary"] == "task A complete"
    assert result["event_id"] == event_id


def test_wait_for_worker_returns_latest_done_when_multiple(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="foo", cwd="/x", iterm_sid="i1", pid=os.getpid())
    _write_done_event(sid="w1", worker_name="foo", summary="task A", completed_at=100.0)
    latest = _write_done_event(sid="w1", worker_name="foo", summary="task B", completed_at=200.0)
    _write_done_event(sid="w1", worker_name="foo", summary="task A-prime", completed_at=150.0)
    result = _asyncio.run(wait_for_worker_impl("foo", timeout_sec=60, _poll_interval=0.05))
    assert result["found"] == "done"
    assert result["summary"] == "task B"
    assert result["event_id"] == latest


def test_wait_for_worker_blocks_then_unblocks(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="foo", cwd="/x", iterm_sid="i1", pid=os.getpid())

    async def run():
        task = _asyncio.create_task(wait_for_worker_impl("foo", timeout_sec=60, _poll_interval=0.05))
        await _asyncio.sleep(0.15)
        assert not task.done()
        _write_done_event(sid="w1", worker_name="foo", summary="finished")
        return await _asyncio.wait_for(task, timeout=2.0)

    result = _asyncio.run(run())
    assert result["found"] == "done"
    assert result["summary"] == "finished"
    assert result["sid"] == "w1"


def test_wait_for_worker_returns_exited_when_session_ended(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="foo", cwd="/x", iterm_sid="i1", pid=os.getpid())

    async def run():
        task = _asyncio.create_task(wait_for_worker_impl("foo", timeout_sec=60, _poll_interval=0.05))
        await _asyncio.sleep(0.15)
        (paths.ACTIVE / "w1.json").unlink()
        return await _asyncio.wait_for(task, timeout=2.0)

    result = _asyncio.run(run())
    assert result["found"] == "exited"
    assert result["name"] == "foo"
    assert result["sid"] == "w1"
    assert result["reason"] == "session_ended_without_worker_done"


def test_wait_for_worker_raises_on_unknown(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="no worker named 'ghost'"):
        _asyncio.run(wait_for_worker_impl("ghost", timeout_sec=60, _poll_interval=0.05))


def test_wait_for_worker_manager_holder_fails_fast_naming_the_holder(fresh_orchestrator_dir):
    register_self_impl(claude_sid="mgr-1", agent="manager", name="happy-yak", cwd="/x", iterm_sid="i1", pid=os.getpid())
    with pytest.raises(ValueError, match="held by an active manager"):
        _asyncio.run(wait_for_worker_impl("happy-yak", timeout_sec=1, _poll_interval=0.01))


def test_wait_for_worker_done_event_beats_manager_holder_error(fresh_orchestrator_dir):
    register_self_impl(claude_sid="mgr-1", agent="manager", name="happy-yak", cwd="/x", iterm_sid="i1", pid=os.getpid())
    paths.DONE.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.DONE / "w-gone-1.json", {
        "claude_sid": "w-gone", "worker_name": "happy-yak", "event_id": "1",
        "summary": "done before close", "completed_at": time.time(),
    })
    result = _asyncio.run(wait_for_worker_impl("happy-yak", timeout_sec=1, _poll_interval=0.01))
    assert result["found"] == "done"
    assert result["summary"] == "done before close"


def test_wait_for_worker_closed_record_beats_manager_holder_error(fresh_orchestrator_dir):
    register_self_impl(claude_sid="mgr-1", agent="manager", name="happy-yak", cwd="/x", iterm_sid="i1", pid=os.getpid())
    paths.CLOSED.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.CLOSED / "w-closed.json", {
        "claude_sid": "w-closed", "agent": "worker", "name": "happy-yak",
    })
    result = _asyncio.run(wait_for_worker_impl("happy-yak", timeout_sec=1, _poll_interval=0.01))
    assert result["found"] == "exited"
    assert result["sid"] == "w-closed"


def test_wait_for_worker_raises_on_timeout(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="foo", cwd="/x", iterm_sid="i1", pid=os.getpid())
    with pytest.raises(TimeoutError, match="worker 'foo' did not complete within 1s"):
        _asyncio.run(wait_for_worker_impl("foo", timeout_sec=1, _poll_interval=0.05))


def test_wait_for_worker_resolves_sid_via_closed_record(fresh_orchestrator_dir):
    paths.CLOSED.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.CLOSED / "w1.json", {
        "claude_sid": "w1",
        "name": "foo",
        "cwd": "/x",
        "closed_at": time.time(),
    })
    event_id = _write_done_event(sid="w1", worker_name="foo", summary="closed but done")
    result = _asyncio.run(wait_for_worker_impl("foo", timeout_sec=60, _poll_interval=0.05))
    assert result["found"] == "done"
    assert result["event_id"] == event_id


def test_wait_for_worker_closed_without_done_returns_exited(fresh_orchestrator_dir):
    paths.CLOSED.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.CLOSED / "w1.json", {
        "claude_sid": "w1",
        "name": "foo",
        "cwd": "/x",
        "closed_at": time.time(),
    })
    result = _asyncio.run(wait_for_worker_impl("foo", timeout_sec=60, _poll_interval=0.05))
    assert result["found"] == "exited"
    assert result["sid"] == "w1"


def _rewrite_active(sid: str, **fields):
    record = state.read_json(paths.ACTIVE / f"{sid}.json")
    record.update(fields)
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", record)


def test_wait_for_worker_ignores_done_older_than_tasked_at(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="foo", cwd="/x", iterm_sid="i1", pid=os.getpid())
    _write_done_event(sid="w1", worker_name="foo", summary="TASK 1 done", completed_at=time.time() - 1800)
    _rewrite_active("w1", state="processing", tasked_at=time.time() - 60)
    with pytest.raises(TimeoutError):
        _asyncio.run(wait_for_worker_impl("foo", timeout_sec=1, _poll_interval=0.05))


def test_wait_for_worker_retasked_blocks_then_returns_fresh_done(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="foo", cwd="/x", iterm_sid="i1", pid=os.getpid())
    _write_done_event(sid="w1", worker_name="foo", summary="TASK 1 done", completed_at=time.time() - 1800)
    _rewrite_active("w1", state="processing", tasked_at=time.time() - 60)

    async def run():
        task = _asyncio.create_task(wait_for_worker_impl("foo", timeout_sec=60, _poll_interval=0.05))
        await _asyncio.sleep(0.15)
        assert not task.done(), "stale done must not satisfy the wait"
        _write_done_event(sid="w1", worker_name="foo", summary="TASK 2 done")
        return await _asyncio.wait_for(task, timeout=2.0)

    result = _asyncio.run(run())
    assert result["summary"] == "TASK 2 done"


def test_wait_for_worker_returns_done_newer_than_tasked_at_instantly(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="foo", cwd="/x", iterm_sid="i1", pid=os.getpid())
    _rewrite_active("w1", state="idle", tasked_at=time.time() - 60)
    _write_done_event(sid="w1", worker_name="foo", summary="fresh done")
    result = _asyncio.run(wait_for_worker_impl("foo", timeout_sec=60, _poll_interval=0.05))
    assert result["found"] == "done" and result["summary"] == "fresh done"


def test_wait_for_worker_legacy_record_without_stamps_keeps_old_behavior(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="foo", cwd="/x", iterm_sid="i1", pid=os.getpid())
    _write_done_event(sid="w1", worker_name="foo", summary="old done", completed_at=time.time() - 1800)
    result = _asyncio.run(wait_for_worker_impl("foo", timeout_sec=60, _poll_interval=0.05))
    assert result["found"] == "done" and result["summary"] == "old done"


def test_wait_for_worker_grace_window_admits_done_crossing_the_retask(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="foo", cwd="/x", iterm_sid="i1", pid=os.getpid())
    now = time.time()
    _write_done_event(sid="w1", worker_name="foo", summary="crossed done", completed_at=now - 1.0)
    _rewrite_active("w1", state="processing", tasked_at=now)
    result = _asyncio.run(wait_for_worker_impl("foo", timeout_sec=60, _poll_interval=0.05))
    assert result["summary"] == "crossed done"


def test_wait_for_worker_processing_since_gates_without_tasked_at(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="foo", cwd="/x", iterm_sid="i1", pid=os.getpid())
    _write_done_event(sid="w1", worker_name="foo", summary="TASK 1 done", completed_at=time.time() - 1800)
    _rewrite_active("w1", state="processing", processing_since=time.time() - 60)
    with pytest.raises(TimeoutError):
        _asyncio.run(wait_for_worker_impl("foo", timeout_sec=1, _poll_interval=0.05))


def test_wait_for_worker_processing_since_ignored_when_idle(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="foo", cwd="/x", iterm_sid="i1", pid=os.getpid())
    _write_done_event(sid="w1", worker_name="foo", summary="done before idle", completed_at=time.time() - 1800)
    _rewrite_active("w1", state="idle", processing_since=time.time() - 60)
    result = _asyncio.run(wait_for_worker_impl("foo", timeout_sec=60, _poll_interval=0.05))
    assert result["summary"] == "done before idle"


def test_wait_for_worker_unrelated_record_stamps_never_gate_closed_worker(fresh_orchestrator_dir):
    register_self_impl(claude_sid="other", agent="worker", name="bar", cwd="/y", iterm_sid="i2", pid=os.getpid())
    _rewrite_active("other", state="processing", tasked_at=time.time())
    state.write_json_atomic(paths.CLOSED / "w1.json", {
        "claude_sid": "w1", "name": "foo", "cwd": "/x",
    })
    _write_done_event(sid="w1", worker_name="foo", summary="closed worker done", completed_at=time.time() - 1800)
    result = _asyncio.run(wait_for_worker_impl("foo", timeout_sec=60, _poll_interval=0.05))
    assert result["found"] == "done" and result["summary"] == "closed worker done"


def test_send_manager_to_worker_stamps_tasked_at(fresh_orchestrator_dir, monkeypatch):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="42")
    monkeypatch.setattr("dockwright.mcp_server._type_unguarded", lambda wid, text: None)
    before = time.time()
    send_manager_to_worker_impl(worker="alpha", text="new task")
    record = state.read_json(paths.ACTIVE / "w1.json")
    assert record.get("tasked_at") is not None
    assert record["tasked_at"] >= before


class _FakeCompleted:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_prepare_handoff_writes_distill_file(fresh_orchestrator_dir, tmp_path, monkeypatch):
    log = _write_fake_transcript(tmp_path, monkeypatch, "mgr-old", [
        {"type": "user", "message": {"content": "do X"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
    ])
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")

    captured = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["input"] = kwargs.get("input")
        captured["timeout"] = kwargs.get("timeout")
        return _FakeCompleted(stdout=b"## Decisions\nshipped X\n## Open threads\n- review Y\n")

    monkeypatch.setattr("dockwright.mcp_server.subprocess.run", fake_run)

    result = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    assert result["distill_path"] is not None
    memory_file = Path(result["distill_path"])
    assert memory_file.exists()
    assert memory_file.parent == paths.MANAGER_MEMORY / "general"
    assert memory_file.name.endswith("-mgr-old.md")
    assert "shipped X" in memory_file.read_text()
    argv = captured["args"][0]
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4-6"
    assert argv[argv.index("--effort") + 1] == "high"
    assert captured["input"] == (
        b"<<<TRANSCRIPT_DATA_BEGIN>>>\n"
        b"USER: do X\n\nASSISTANT: ok"
        b"\n<<<TRANSCRIPT_DATA_END>>>"
    )
    assert captured["timeout"] == 180
    assert log.exists()


def test_slim_transcript_strips_tool_use_and_tool_result_bulk():
    from dockwright.mcp_server import _slim_transcript
    bloat = "X" * 50_000
    raw_lines = [
        json.dumps({"type": "user", "message": {"content": "kick off"}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "running"},
            {"type": "tool_use", "name": "Bash", "input": {"command": bloat}},
        ]}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": bloat},
            {"type": "text", "text": "ok now next"},
        ]}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "done"},
        ]}}),
    ]
    raw = ("\n".join(raw_lines)).encode("utf-8")
    slim = _slim_transcript(raw)
    decoded = slim.decode("utf-8")
    assert bloat not in decoded
    assert "USER: kick off" in decoded
    assert "ASSISTANT: running\n[tool_use: Bash]" in decoded
    assert "[tool_result elided]" in decoded
    assert "ok now next" in decoded
    assert "ASSISTANT: done" in decoded
    assert len(slim) < len(raw) // 10


def test_slim_transcript_truncates_head_plus_tail_when_over_max_bytes():
    from dockwright.mcp_server import _slim_transcript
    raw_lines = [
        json.dumps({"type": "user", "message": {"content": f"msg-{i:03d}"}})
        for i in range(200)
    ]
    raw = ("\n".join(raw_lines)).encode("utf-8")
    slim = _slim_transcript(raw, max_bytes=500)
    decoded = slim.decode("utf-8")
    assert "[transcript middle truncated]" in decoded
    assert "msg-000" in decoded
    assert "msg-199" in decoded
    assert "msg-100" not in decoded


def test_slim_transcript_keeps_inner_text_of_list_tool_result():
    from dockwright.mcp_server import _slim_transcript
    raw = json.dumps({
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "content": [
                {"type": "text", "text": "shipped commit abc123"},
            ]},
        ]},
    }).encode("utf-8")
    decoded = _slim_transcript(raw).decode("utf-8")
    assert "shipped commit abc123" in decoded
    assert "[tool_result elided]" not in decoded


def test_slim_transcript_falls_back_to_elision_for_string_tool_result():
    from dockwright.mcp_server import _slim_transcript
    raw = json.dumps({
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "content": "X" * 50_000},
        ]},
    }).encode("utf-8")
    decoded = _slim_transcript(raw).decode("utf-8")
    assert "[tool_result elided]" in decoded
    assert "X" * 100 not in decoded


def test_distill_logs_stdout_on_nonzero_exit(fresh_orchestrator_dir, tmp_path, monkeypatch, capsys):
    _write_fake_transcript(tmp_path, monkeypatch, "mgr-old", [
        {"type": "user", "message": {"content": "hi"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
    ])
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")

    def fake_run(*args, **kwargs):
        return _FakeCompleted(stdout=b"Prompt is too long", stderr=b"", returncode=1)

    monkeypatch.setattr("dockwright.mcp_server.subprocess.run", fake_run)

    result = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    assert result["distill_path"] is None
    err = capsys.readouterr().err
    assert "claude -p exit 1" in err
    assert "Prompt is too long" in err


def test_prepare_handoff_distill_failure_does_not_raise(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _write_fake_transcript(tmp_path, monkeypatch, "mgr-old", [
        {"type": "user", "message": {"content": "hi"}},
    ])
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=60)

    monkeypatch.setattr("dockwright.mcp_server.subprocess.run", boom)

    result = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    assert (paths.HANDOFFS / f"{result['handoff_id']}.json").exists()
    assert result["distill_path"] is None
    assert list(paths.MANAGER_MEMORY.iterdir()) == []


def test_prepare_handoff_distill_missing_transcript_skips(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")

    called = []

    def fake_run(*args, **kwargs):
        called.append(args)
        return _FakeCompleted(stdout=b"should not be invoked")

    monkeypatch.setattr("dockwright.mcp_server.subprocess.run", fake_run)

    result = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    assert (paths.HANDOFFS / f"{result['handoff_id']}.json").exists()
    assert result["distill_path"] is None
    assert called == []
    assert list(paths.MANAGER_MEMORY.iterdir()) == []


from dockwright.mcp_server import (
    _matches_manager, list_workers_impl as _lw, list_pending_questions_impl as _lpq,
    _write_question, worker_done_impl as _wd, list_closed_workers_impl as _lcw,
)


def test_routing_filter_isolates_workers_by_parent_manager_name(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w-a", agent="worker", name="worker-a", cwd="/x",
                       iterm_sid="i1", pid=os.getpid(), parent_manager_name="manager-a")
    register_self_impl(claude_sid="w-b", agent="worker", name="worker-b", cwd="/y",
                       iterm_sid="i2", pid=os.getpid(), parent_manager_name="manager-b")
    workers_a = _lw(manager_name="manager-a")
    workers_b = _lw(manager_name="manager-b")
    assert [w["name"] for w in workers_a] == ["worker-a"]
    assert [w["name"] for w in workers_b] == ["worker-b"]


def test_routing_filter_excludes_null_parent_under_strict_scope(fresh_orchestrator_dir):
    register_self_impl(claude_sid="legacy", agent="worker", name="oldie", cwd="/x",
                       iterm_sid="i1", pid=os.getpid())
    register_self_impl(claude_sid="w-a", agent="worker", name="worker-a", cwd="/x",
                       iterm_sid="i2", pid=os.getpid(), parent_manager_name="manager-a")
    a_view = _lw(manager_name="manager-a")
    b_view = _lw(manager_name="manager-b")
    assert "oldie" not in [w["name"] for w in a_view]
    assert "worker-a" in [w["name"] for w in a_view]
    assert "oldie" not in [w["name"] for w in b_view]
    assert "worker-a" not in [w["name"] for w in b_view]
    all_view = _lw(manager_name=None)
    assert {"oldie", "worker-a"} <= {w["name"] for w in all_view}


def test_routing_filter_questions_by_manager(fresh_orchestrator_dir):
    _write_question(worker_sid="w-a", worker_name="worker-a",
                    question="ours?", parent_manager_name="manager-a")
    _write_question(worker_sid="w-b", worker_name="worker-b",
                    question="theirs?", parent_manager_name="manager-b")
    a_qs = _lpq(manager_name="manager-a")
    b_qs = _lpq(manager_name="manager-b")
    assert [q["question"] for q in a_qs] == ["ours?"]
    assert [q["question"] for q in b_qs] == ["theirs?"]


def test_routing_filter_done_events_by_manager(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w-a", agent="worker", name="worker-a", cwd="/x",
                       iterm_sid="i1", pid=os.getpid(), parent_manager_name="manager-a")
    register_self_impl(claude_sid="w-b", agent="worker", name="worker-b", cwd="/y",
                       iterm_sid="i2", pid=os.getpid(), parent_manager_name="manager-b")
    _wd(claude_sid="w-a", summary="A done")
    _wd(claude_sid="w-b", summary="B done")
    result_a = _asyncio.run(wait_for_worker_impl("worker-a", timeout_sec=60,
                                                 _poll_interval=0.05,
                                                 manager_name="manager-a"))
    assert result_a["found"] == "done"
    assert result_a["summary"] == "A done"
    with pytest.raises(ValueError, match="no worker named 'worker-b'"):
        _asyncio.run(wait_for_worker_impl("worker-b", timeout_sec=60,
                                           _poll_interval=0.05,
                                           manager_name="manager-a"))


def test_unscoped_done_event_not_visible_to_per_manager_wait(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w-legacy", agent="worker", name="legacy", cwd="/x",
                       iterm_sid="i1", pid=os.getpid())
    _wd(claude_sid="w-legacy", summary="legacy done")
    unscoped = list((paths.DONE / paths.UNSCOPED_BUCKET).glob("w-legacy-*.json"))
    assert len(unscoped) == 1
    for mgr in ("manager-a", "manager-b"):
        with pytest.raises(ValueError, match="no worker named 'legacy'"):
            _asyncio.run(wait_for_worker_impl("legacy", timeout_sec=60,
                                              _poll_interval=0.05, manager_name=mgr))
    result = _asyncio.run(wait_for_worker_impl("legacy", timeout_sec=60,
                                                _poll_interval=0.05, manager_name=None))
    assert result["found"] == "done"
    assert result["summary"] == "legacy done"


def test_backfill_adopts_orphans_on_single_manager_boot(fresh_orchestrator_dir, monkeypatch):
    register_self_impl(claude_sid="w1", agent="worker", name="orphan-1", cwd="/x",
                       iterm_sid="i1", pid=os.getpid())
    register_self_impl(claude_sid="w2", agent="worker", name="orphan-2", cwd="/y",
                       iterm_sid="i2", pid=os.getpid())
    assert _lw(manager_name="solo") == []
    from dockwright.mcp_server import become_manager_impl
    monkeypatch.setattr("dockwright.mcp_server.names.roll_manager_name",
                        lambda is_taken=None: "solo")
    become_manager_impl(claude_sid="mgr-1", domain="general")
    visible = sorted(w["name"] for w in _lw(manager_name="solo"))
    assert visible == ["orphan-1", "orphan-2"]


def test_become_manager_roll_taken_set_includes_worker_funny_names(fresh_orchestrator_dir, monkeypatch):
    state.write_json_atomic(paths.ACTIVE / "w-1.json", {
        "claude_sid": "w-1", "agent": "worker", "name": "task-x",
        "funny_name": "happy-dragon", "pid": os.getpid(), "window_id": "i-w1",
    })
    captured = {}

    def fake_roll(is_taken):
        captured["is_taken"] = is_taken
        return "calm-ghost"

    monkeypatch.setattr("dockwright.mcp_server.names.roll_manager_name", fake_roll)
    result = become_manager_impl(claude_sid="mgr-1", iterm_sid="i-mgr")
    assert result["name"] == "calm-ghost"
    assert captured["is_taken"]("happy-dragon") is True
    assert captured["is_taken"]("task-x") is True
    assert captured["is_taken"]("free-name") is False


def test_backfill_skips_when_zero_managers_active(fresh_orchestrator_dir, capsys):
    from dockwright.mcp_server import _backfill_legacy_workers
    register_self_impl(claude_sid="w1", agent="worker", name="orphan-1", cwd="/x",
                       iterm_sid="i1", pid=os.getpid())
    count = _backfill_legacy_workers()
    assert count == 0
    err = capsys.readouterr().err
    assert "0 managers active" in err
    record = state.read_json(paths.ACTIVE / "w1.json")
    assert record["parent_manager_name"] is None


def test_backfill_skips_when_two_managers_active(fresh_orchestrator_dir, capsys):
    from dockwright.mcp_server import _backfill_legacy_workers
    for sid, name in [("m1", "mgr-a"), ("m2", "mgr-b")]:
        state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {
            "claude_sid": sid, "agent": "manager", "name": name,
            "pid": os.getpid(), "domain": "general",
        })
    register_self_impl(claude_sid="w1", agent="worker", name="orphan-1", cwd="/x",
                       iterm_sid="i1", pid=os.getpid())
    count = _backfill_legacy_workers()
    assert count == 0
    err = capsys.readouterr().err
    assert "2 managers active" in err
    record = state.read_json(paths.ACTIVE / "w1.json")
    assert record["parent_manager_name"] is None


def test_questions_with_null_parent_invisible_under_strict_scope(fresh_orchestrator_dir):
    _write_question(worker_sid="w-orphan", worker_name="orphan",
                    question="anybody?", parent_manager_name=None)
    assert _lpq(manager_name="manager-a") == []
    assert _lpq(manager_name="manager-b") == []
    wildcard = _lpq(manager_name=None)
    assert [q["question"] for q in wildcard] == ["anybody?"]


def test_routing_filter_closed_workers_by_manager(fresh_orchestrator_dir):
    paths.CLOSED.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.CLOSED / "c-a.json", {
        "claude_sid": "c-a", "name": "alpha", "cwd": "/x",
        "closed_at": 1.0, "parent_manager_name": "manager-a",
    })
    state.write_json_atomic(paths.CLOSED / "c-b.json", {
        "claude_sid": "c-b", "name": "beta", "cwd": "/y",
        "closed_at": 2.0, "parent_manager_name": "manager-b",
    })
    assert [r["name"] for r in _lcw(manager_name="manager-a")] == ["alpha"]
    assert [r["name"] for r in _lcw(manager_name="manager-b")] == ["beta"]


def test_list_closed_workers_default_is_unlimited_newest_first(fresh_orchestrator_dir):
    paths.CLOSED.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.CLOSED / "c-old.json", {
        "claude_sid": "c-old", "name": "old", "cwd": "/x",
        "closed_at": 1.0, "parent_manager_name": "manager-a",
    })
    state.write_json_atomic(paths.CLOSED / "c-new.json", {
        "claude_sid": "c-new", "name": "new", "cwd": "/x",
        "closed_at": 3.0, "parent_manager_name": "manager-a",
    })
    state.write_json_atomic(paths.CLOSED / "c-mid.json", {
        "claude_sid": "c-mid", "name": "mid", "cwd": "/x",
        "closed_at": 2.0, "parent_manager_name": "manager-b",
    })

    assert [r["name"] for r in _lcw()] == ["new", "mid", "old"]


def test_list_closed_workers_exposes_moved(fresh_orchestrator_dir):
    paths.CLOSED.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.CLOSED / "c-moved.json", {
        "claude_sid": "c-moved", "name": "moved", "cwd": "/x", "closed_at": 1.0,
        "closed_reason": "session_end", "moved": "fable", "parent_manager_name": "manager-a",
    })
    state.write_json_atomic(paths.CLOSED / "c-plain.json", {
        "claude_sid": "c-plain", "name": "plain", "cwd": "/x", "closed_at": 2.0,
        "parent_manager_name": "manager-a",
    })
    rows = {r["name"]: r for r in _lcw()}
    assert rows["moved"]["moved"] == "fable" and rows["plain"]["moved"] is None


def test_list_closed_workers_limit_returns_newest_records(fresh_orchestrator_dir):
    paths.CLOSED.mkdir(parents=True, exist_ok=True)
    for name, closed_at in (("old", 1.0), ("newest", 3.0), ("middle", 2.0)):
        state.write_json_atomic(paths.CLOSED / f"{name}.json", {
            "claude_sid": name, "name": name, "cwd": "/x",
            "closed_at": closed_at, "parent_manager_name": "manager-a",
        })

    assert [r["name"] for r in _lcw(limit=2)] == ["newest", "middle"]


def test_list_closed_workers_limit_applies_after_manager_scope_and_order(fresh_orchestrator_dir):
    paths.CLOSED.mkdir(parents=True, exist_ok=True)
    for name, manager_name, closed_at in (
        ("a-old", "manager-a", 1.0),
        ("b-newest", "manager-b", 5.0),
        ("a-newest", "manager-a", 4.0),
        ("a-middle", "manager-a", 2.0),
    ):
        state.write_json_atomic(paths.CLOSED / f"{name}.json", {
            "claude_sid": name, "name": name, "cwd": "/x",
            "closed_at": closed_at, "parent_manager_name": manager_name,
        })

    assert [r["name"] for r in _lcw(manager_name="manager-a", limit=2)] == [
        "a-newest",
        "a-middle",
    ]


@pytest.mark.parametrize("limit", [0, -1])
def test_list_closed_workers_rejects_non_positive_limit(fresh_orchestrator_dir, limit):
    with pytest.raises(ValueError, match="limit must be a positive integer"):
        _lcw(limit=limit)


def test_list_closed_workers_mcp_signature_has_optional_limit():
    import inspect
    from dockwright.mcp_server import list_closed_workers

    params = inspect.signature(list_closed_workers).parameters
    assert params["limit"].default is None


from dockwright.mcp_server import close_manager_self_impl


def test_close_manager_self_runs_distill_and_clears_active(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _write_fake_transcript(tmp_path, monkeypatch, "mgr-old", [
        {"type": "user", "message": {"content": "hello"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
    ])
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i9", domain="general")

    def fake_run(*args, **kwargs):
        if kwargs.get("input") is not None:
            return _FakeCompleted(stdout=b"## Decisions\nshipped\n")
        return _FakeCompleted(stdout=b"")

    monkeypatch.setattr("dockwright.mcp_server.subprocess.run", fake_run)
    result = close_manager_self_impl("mgr-old")
    assert result["ok"] is True
    assert result["distill_path"] is not None
    assert "general" in result["distill_path"]
    assert not (paths.ACTIVE / "mgr-old.json").exists()
    assert Path(result["distill_path"]).exists()


def test_close_manager_self_swallows_distill_failure(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i9", domain="general")

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=10)
    monkeypatch.setattr("dockwright.mcp_server.subprocess.run", boom)

    result = close_manager_self_impl("mgr-old")
    assert result["ok"] is True
    assert result["distill_path"] is None
    assert not (paths.ACTIVE / "mgr-old.json").exists()


def test_close_manager_self_rejects_non_manager(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    with pytest.raises(ValueError, match="not a manager"):
        close_manager_self_impl("w1")


def test_close_manager_self_refuses_without_notebook_mark(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="mgr-c", iterm_sid="i0", domain="general")
    _open_notebook()
    closed, distilled = [], []
    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda *a, **k: closed.append(a))
    monkeypatch.setattr("dockwright.mcp_server.distill_and_write_memory", lambda *a, **k: distilled.append(a))
    with pytest.raises(ValueError, match="dockwright notebook mark"):
        close_manager_self_impl("mgr-c")
    assert (paths.ACTIVE / "mgr-c.json").exists()
    assert closed == []
    assert distilled == []


from dockwright.mcp_server import _migrate_flat_manager_memory


def test_migrate_flat_manager_memory_moves_legacy_files(fresh_orchestrator_dir):
    paths.MANAGER_MEMORY.mkdir(parents=True, exist_ok=True)
    flat_a = paths.MANAGER_MEMORY / "2026-05-01-mgr-old.md"
    flat_a.write_text("# old session A")
    flat_b = paths.MANAGER_MEMORY / "2026-05-02-mgr-older.md"
    flat_b.write_text("# old session B")
    moved = _migrate_flat_manager_memory()
    assert moved == 2
    general = paths.MANAGER_MEMORY / "general"
    assert (general / "2026-05-01-mgr-old.md").exists()
    assert (general / "2026-05-02-mgr-older.md").exists()
    assert not flat_a.exists()
    assert not flat_b.exists()


def test_migrate_flat_manager_memory_is_idempotent(fresh_orchestrator_dir):
    paths.MANAGER_MEMORY.mkdir(parents=True, exist_ok=True)
    (paths.MANAGER_MEMORY / "2026-05-01-x.md").write_text("x")
    assert _migrate_flat_manager_memory() == 1
    assert _migrate_flat_manager_memory() == 0
    assert (paths.MANAGER_MEMORY / "general" / "2026-05-01-x.md").exists()


def test_migrate_flat_manager_memory_ignores_existing_subdirs(fresh_orchestrator_dir):
    (paths.MANAGER_MEMORY / "general").mkdir(parents=True)
    (paths.MANAGER_MEMORY / "general" / "x.md").write_text("kept")
    (paths.MANAGER_MEMORY / "dlq").mkdir()
    (paths.MANAGER_MEMORY / "dlq" / "y.md").write_text("kept too")
    moved = _migrate_flat_manager_memory()
    assert moved == 0
    assert (paths.MANAGER_MEMORY / "general" / "x.md").read_text() == "kept"
    assert (paths.MANAGER_MEMORY / "dlq" / "y.md").read_text() == "kept too"


from dockwright.hooks import session_end as _session_end


def test_session_end_distill_skips_when_memory_already_exists(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _write_fake_transcript(tmp_path, monkeypatch, "mgr-x", [
        {"type": "user", "message": {"content": "go"}},
    ])
    state.write_json_atomic(paths.ACTIVE / "mgr-x.json", {
        "claude_sid": "mgr-x", "agent": "manager", "name": "grumpy-yak",
        "cwd": "/x", "iterm_sid": "i1", "pid": 1, "started_at": 0,
        "domain": "general",
    })
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y-%m-%d")
    general = paths.manager_memory_domain_dir("general")
    general.mkdir(parents=True, exist_ok=True)
    existing = general / f"{today}-mgr-x.md"
    existing.write_text("pre-existing from /manager-close")

    calls = []
    monkeypatch.setattr("dockwright.mcp_server.subprocess.run",
                        lambda *a, **kw: calls.append(("run", a, kw)) or _FakeCompleted(stdout=b"new"))

    import io as _io
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setattr("sys.stdin", _io.StringIO(json.dumps({"session_id": "mgr-x"})))
    _session_end()
    assert not any(args and "claude" in args[0][0] for _label, args, _kw in calls if isinstance(args, tuple) and len(args) > 0 and isinstance(args[0], list))
    assert existing.read_text() == "pre-existing from /manager-close"


def test_session_end_distill_runs_when_no_memory_exists(fresh_orchestrator_dir, tmp_path, monkeypatch):
    state.write_json_atomic(paths.ACTIVE / "mgr-x.json", {
        "claude_sid": "mgr-x", "agent": "manager", "name": "grumpy-yak",
        "cwd": "/x", "iterm_sid": "i1", "pid": 1, "started_at": 0,
        "domain": "general",
    })

    popens = []
    monkeypatch.setattr(
        "dockwright.hooks.subprocess.Popen",
        lambda *a, **kw: popens.append((a, kw)) or _FakeCompleted(),
    )

    import io as _io
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setattr("sys.stdin", _io.StringIO(json.dumps({"session_id": "mgr-x"})))
    _session_end()
    assert len(popens) == 1
    (cmd,), kw = popens[0]
    assert cmd[-4:] == ["distill", "mgr-x", "--domain", "general"]
    assert kw["start_new_session"] is True
    assert kw["stdin"] is subprocess.DEVNULL
    assert kw["stdout"].name.endswith("distill-fallback.log")
    assert kw["stderr"] is kw["stdout"]


def test_distill_subprocess_env_strips_orchestrator_keys(fresh_orchestrator_dir, tmp_path, monkeypatch):
    from dockwright.mcp_server import _distill_manager_session
    _write_fake_transcript(tmp_path, monkeypatch, "mgr-x", [
        {"type": "user", "message": {"content": "go"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
    ])
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "grumpy-yak")
    monkeypatch.setenv("CLAUDE_PARENT_MANAGER", "grumpy-yak")
    monkeypatch.setenv("CLAUDE_WORKER_RUNTIME", "claude")
    monkeypatch.setenv("CLAUDE_PARENT_PID", "1234")

    captured = {}

    def fake_run(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakeCompleted(stdout=b"## Decisions\nok\n")

    monkeypatch.setattr("dockwright.mcp_server.subprocess.run", fake_run)
    assert _distill_manager_session("mgr-x") is not None

    env = captured["env"]
    assert env is not None, "distill subprocess must pass an explicit sanitized env"
    for key in paths.ORCHESTRATOR_ENV_KEYS:
        assert key not in env, f"orchestrator key {key} leaked into the distill child env"
    assert env[paths.DISTILL_ENV_SENTINEL] == "1"
    assert env["CLAUDE_SPEND_CLASS"] == "distill"
    assert env.get("HOME") == str(tmp_path)


def test_session_end_distill_child_never_redistills(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _write_fake_transcript(tmp_path, monkeypatch, "distill-1", [
        {"type": "user", "message": {"content": "go"}},
    ])
    state.write_json_atomic(paths.ACTIVE / "distill-1.json", {
        "claude_sid": "distill-1", "agent": "manager", "name": "phantom",
        "cwd": "/x", "iterm_sid": "i1", "pid": 1, "started_at": 0,
        "domain": "general",
    })

    calls = []
    monkeypatch.setattr("dockwright.mcp_server.subprocess.run",
                        lambda *a, **kw: calls.append((a, kw)) or _FakeCompleted(stdout=b"x"))

    import io as _io
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setenv(paths.DISTILL_ENV_SENTINEL, "1")
    monkeypatch.setattr("sys.stdin", _io.StringIO(json.dumps({"session_id": "distill-1"})))
    _session_end()
    assert calls == []
    assert list(paths.MANAGER_MEMORY.iterdir()) == []


def test_spawn_worker_stamps_parent_manager_env(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="mgr-a", iterm_sid="i0", domain="general")
    mgr_name = state.read_json(paths.ACTIVE / "mgr-a.json")["name"]
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="do X",
        name="worker-x",
        cwd="/tmp/x",
        manager_sid="mgr-a",
    ))
    env = captured.get("env") or {}
    assert env.get("CLAUDE_PARENT_MANAGER") == mgr_name


def test_spawn_worker_no_manager_sid_omits_parent_env(fresh_orchestrator_dir, monkeypatch):
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="do X",
        name="worker-x",
        cwd="/tmp/x",
    ))
    env = captured.get("env") or {}
    assert "CLAUDE_PARENT_MANAGER" not in env


from dockwright.hooks import session_end as _session_end_h
from dockwright.mcp_server import resume_worker_impl as _resume_worker_mcp


def test_parent_manager_preserved_across_close_and_resume(fresh_orchestrator_dir, tmp_path, monkeypatch):
    become_manager_impl(claude_sid="mgr-a", iterm_sid="i0", domain="general")
    mgr_name = state.read_json(paths.ACTIVE / "mgr-a.json")["name"]
    captured_spawn = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        extra_args=FLAGS,
        initial_prompt="task",
        name="worker-fc",
        cwd="/tmp/fc",
        manager_sid="mgr-a",
    ))
    assert (captured_spawn.get("env") or {}).get("CLAUDE_PARENT_MANAGER") == mgr_name

    state.write_json_atomic(paths.ACTIVE / "wfc-sid.json", {
        "claude_sid": "wfc-sid", "agent": "worker", "name": "worker-fc",
        "cwd": "/tmp/fc", "iterm_sid": "iw", "pid": os.getpid(), "started_at": 0,
        "parent_manager_name": mgr_name,
    })

    _make_transcript(tmp_path, monkeypatch, "wfc-sid")
    import io as _io
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setattr("sys.stdin", _io.StringIO(json.dumps({"session_id": "wfc-sid"})))
    _session_end_h()
    closed_record = state.read_json(paths.CLOSED / "wfc-sid.json")
    assert closed_record is not None, "session_end must archive worker to closed/"
    assert closed_record.get("parent_manager_name") == mgr_name, (
        "session_end MUST write parent_manager_name into the closed record so "
        "resume_worker can re-stamp it. This is the regression Important #1 fixes."
    )

    captured_resume = _patch_spawn_registers_active(monkeypatch)
    _asyncio.run(_resume_worker_mcp(name="worker-fc", model="@opus", effort="xhigh", _registration_timeout_sec=2.0,
                                    _poll_interval=0.01))
    resumed_env = captured_resume.get("env") or {}
    assert resumed_env.get("CLAUDE_PARENT_MANAGER") == mgr_name, (
        f"resume_worker MUST pass CLAUDE_PARENT_MANAGER={mgr_name!r} from the closed "
        f"record; got env={resumed_env!r}"
    )


def test_become_manager_backfills_legacy_workers_when_sole_manager(fresh_orchestrator_dir, capsys):
    for i in range(3):
        state.write_json_atomic(paths.ACTIVE / f"legacy-{i}.json", {
            "claude_sid": f"legacy-{i}", "agent": "worker", "name": f"old-{i}",
            "cwd": "/x", "iterm_sid": f"i{i}", "pid": os.getpid(), "started_at": 0,
        })
    result = become_manager_impl(claude_sid="mgr-fresh", iterm_sid="i9", domain="general")
    mgr_name = result["name"]
    for i in range(3):
        record = state.read_json(paths.ACTIVE / f"legacy-{i}.json")
        assert record["parent_manager_name"] == mgr_name, (
            f"legacy-{i} should have been stamped with parent={mgr_name}, "
            f"got {record.get('parent_manager_name')!r}"
        )


def test_become_manager_skips_backfill_when_two_managers_active(fresh_orchestrator_dir, capsys):
    state.write_json_atomic(paths.ACTIVE / "mgr-a.json", {
        "claude_sid": "mgr-a", "agent": "manager", "name": "manager-a",
        "cwd": "/x", "iterm_sid": "i0", "pid": os.getpid(), "started_at": 0,
        "domain": "general",
    })
    for i in range(3):
        state.write_json_atomic(paths.ACTIVE / f"legacy-{i}.json", {
            "claude_sid": f"legacy-{i}", "agent": "worker", "name": f"old-{i}",
            "cwd": "/x", "iterm_sid": f"i{i+1}", "pid": os.getpid(), "started_at": 0,
        })
    become_manager_impl(claude_sid="mgr-b", iterm_sid="i9", domain="general")
    for i in range(3):
        record = state.read_json(paths.ACTIVE / f"legacy-{i}.json")
        assert record.get("parent_manager_name") is None, (
            f"legacy-{i} should remain null when 2+ managers active (ambiguous); "
            f"got {record.get('parent_manager_name')!r}"
        )
    err = capsys.readouterr().err
    assert "backfill" in err and "skipping" in err
    assert "2 managers active" in err


def test_become_manager_backfill_idempotent_on_second_boot(fresh_orchestrator_dir):
    state.write_json_atomic(paths.ACTIVE / "legacy-1.json", {
        "claude_sid": "legacy-1", "agent": "worker", "name": "old-1",
        "cwd": "/x", "iterm_sid": "i1", "pid": os.getpid(), "started_at": 0,
    })
    first = become_manager_impl(claude_sid="mgr-1st", iterm_sid="i9", domain="general")
    stamped_name = state.read_json(paths.ACTIVE / "legacy-1.json")["parent_manager_name"]
    assert stamped_name == first["name"]
    become_manager_impl(claude_sid="mgr-2nd", iterm_sid="i10", domain="dlq")
    assert state.read_json(paths.ACTIVE / "legacy-1.json")["parent_manager_name"] == first["name"]


def test_resume_reclaims_autoclosed_spend_to_ledger(fresh_orchestrator_dir, tmp_path, monkeypatch):
    import json as _json
    monkeypatch.setattr(paths, "SPEND_LEDGER", fresh_orchestrator_dir / "spend-ledger.jsonl")
    _make_transcript(tmp_path, monkeypatch, "idle-sid")
    state.write_json_atomic(paths.CLOSED / "idle-sid.json", {
        "claude_sid": "idle-sid", "name": "idle-worker", "cwd": "/tmp/idle",
        "iterm_sid": "ii", "closed_at": 1.0,
        "closed_reason": "idle>7200s",
        "spend": {"turns": 3, "out_tokens": 100, "in_tokens": 50, "cache_read_tokens": 10},
    })
    _patch_spawn_registers_active(monkeypatch)
    result = _asyncio.run(_resume_worker_mcp(model="@opus", effort="xhigh", name="idle-worker", _registration_timeout_sec=2.0, _poll_interval=0.01))
    assert result["ok"] is True
    assert not (paths.CLOSED / "idle-sid.json").exists()
    ledger_path = fresh_orchestrator_dir / "spend-ledger.jsonl"
    assert ledger_path.exists(), "ledger file must be created"
    entries = [_json.loads(line) for line in ledger_path.read_text().splitlines() if line.strip()]
    assert len(entries) == 1, f"expected 1 ledger entry, got {len(entries)}: {entries}"
    assert entries[0]["sid"] == "idle-sid"
    assert entries[0]["source"] == "resume_reclaim"


def test_resume_does_not_reledger_session_end_closures(fresh_orchestrator_dir, tmp_path, monkeypatch):
    import json as _json
    monkeypatch.setattr(paths, "SPEND_LEDGER", fresh_orchestrator_dir / "spend-ledger.jsonl")
    _make_transcript(tmp_path, monkeypatch, "ended-sid")
    state.write_json_atomic(paths.CLOSED / "ended-sid.json", {
        "claude_sid": "ended-sid", "name": "ended-worker", "cwd": "/tmp/ended",
        "iterm_sid": "ie", "closed_at": 1.0,
        "closed_reason": "session_end",
        "spend": {"turns": 2, "out_tokens": 80, "in_tokens": 40, "cache_read_tokens": 5},
    })
    _patch_spawn_registers_active(monkeypatch)
    result = _asyncio.run(_resume_worker_mcp(model="@opus", effort="xhigh", name="ended-worker", _registration_timeout_sec=2.0, _poll_interval=0.01))
    assert result["ok"] is True
    assert not (paths.CLOSED / "ended-sid.json").exists()
    ledger_path = fresh_orchestrator_dir / "spend-ledger.jsonl"
    if ledger_path.exists():
        entries = [line for line in ledger_path.read_text().splitlines() if line.strip()]
        assert entries == [], f"ledger must be empty for session_end closure; got {entries}"


def test_resume_worker_with_null_parent_omits_env(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _make_transcript(tmp_path, monkeypatch, "legacy-sid")
    state.write_json_atomic(paths.CLOSED / "legacy-sid.json", {
        "claude_sid": "legacy-sid", "name": "legacy-worker", "cwd": "/tmp/l",
        "iterm_sid": "il", "closed_at": 1.0,
    })
    captured = _patch_spawn_registers_active(monkeypatch)
    _asyncio.run(_resume_worker_mcp(model="@opus", effort="xhigh", name="legacy-worker", _registration_timeout_sec=2.0, _poll_interval=0.01))
    env = captured.get("env")
    assert env is None or "CLAUDE_PARENT_MANAGER" not in env


def _make_transcript(tmp_path, monkeypatch, sid, nonempty=True):
    monkeypatch.setenv("HOME", str(tmp_path))
    projects = tmp_path / ".claude" / "projects" / "-Users-x"
    projects.mkdir(parents=True, exist_ok=True)
    log = projects / f"{sid}.jsonl"
    log.write_text(json.dumps({"type": "assistant", "message": {"content": []}}) if nonempty else "")
    return log


def _make_codex_transcript(tmp_path, monkeypatch, sid, nonempty=True):
    monkeypatch.setenv("HOME", str(tmp_path))
    sessions = tmp_path / ".codex" / "sessions" / "2026" / "05" / "26"
    sessions.mkdir(parents=True, exist_ok=True)
    log = sessions / f"rollout-2026-05-26T10-55-35-{sid}.jsonl"
    log.write_text(json.dumps({"type": "session_meta"}) if nonempty else "")
    return log


def _patch_spawn_registers_active(monkeypatch):
    captured: dict = {}

    async def fake_spawn(**kwargs):
        captured.update(kwargs)
        nm = kwargs.get("name", "")
        sid = kwargs.get("resume_sid") or f"spawned-{nm}"
        state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {
            "claude_sid": sid, "agent": "worker", "name": nm,
            "cwd": kwargs.get("cwd", "/x"), "iterm_sid": "ir", "pid": os.getpid(), "started_at": 0,
            "runtime": kwargs.get("runtime", "claude"),
        })
        return ("999", nm)

    monkeypatch.setattr(spawner, "spawn_worker_tab", fake_spawn)
    return captured


from dockwright.mcp_server import _find_closed_record_by_name as _find_closed


def test_find_closed_record_by_name_returns_newest_among_duplicates(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _make_transcript(tmp_path, monkeypatch, "old-sid")
    _make_transcript(tmp_path, monkeypatch, "new-sid")
    state.write_json_atomic(paths.CLOSED / "old-sid.json", {
        "claude_sid": "old-sid", "name": "dup", "cwd": "/x", "closed_at": 100.0})
    state.write_json_atomic(paths.CLOSED / "new-sid.json", {
        "claude_sid": "new-sid", "name": "dup", "cwd": "/x", "closed_at": 200.0})
    _path, record = _find_closed("dup")
    assert record["claude_sid"] == "new-sid"


def test_resume_worker_uses_runtime_from_closed_record(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _make_codex_transcript(tmp_path, monkeypatch, "codex-sid")
    state.write_json_atomic(paths.CLOSED / "codex-sid.json", {
        "claude_sid": "codex-sid",
        "name": "codex-worker",
        "cwd": "/tmp/codex",
        "runtime": "codex",
        "closed_at": 1.0,
    })
    captured = _patch_spawn_registers_active(monkeypatch)
    result = _asyncio.run(_resume_worker_mcp(
        name="codex-worker",
        _registration_timeout_sec=2.0,
        _poll_interval=0.01,
    ))
    assert result["ok"] is True
    assert captured["runtime"] == "codex"
    assert captured["resume_sid"] == "codex-sid"


def test_find_closed_record_prefers_live_transcript_over_newer_junk(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _make_transcript(tmp_path, monkeypatch, "healthy-sid")
    state.write_json_atomic(paths.CLOSED / "junk-sid.json", {
        "claude_sid": "junk-sid", "name": "tkt-8773", "cwd": "/x", "closed_at": 999.0})
    state.write_json_atomic(paths.CLOSED / "healthy-sid.json", {
        "claude_sid": "healthy-sid", "name": "tkt-8773", "cwd": "/x", "closed_at": 100.0})
    _path, record = _find_closed("tkt-8773")
    assert record["claude_sid"] == "healthy-sid"


def test_find_closed_record_raises_when_no_live_transcript(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    state.write_json_atomic(paths.CLOSED / "dead-a.json", {
        "claude_sid": "dead-a", "name": "gone", "cwd": "/x", "closed_at": 1.0})
    state.write_json_atomic(paths.CLOSED / "dead-b.json", {
        "claude_sid": "dead-b", "name": "gone", "cwd": "/x", "closed_at": 2.0})
    with pytest.raises(ValueError) as exc:
        _find_closed("gone")
    msg = str(exc.value)
    assert "dead-a" in msg and "dead-b" in msg


def test_resume_worker_keeps_closed_record_when_registration_times_out(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _make_transcript(tmp_path, monkeypatch, "stuck-sid")
    state.write_json_atomic(paths.CLOSED / "stuck-sid.json", {
        "claude_sid": "stuck-sid", "name": "stuck", "cwd": "/tmp/s", "closed_at": 1.0})
    _patch_spawn_worker_tab(monkeypatch)
    result = _asyncio.run(_resume_worker_mcp(model="@opus", effort="xhigh", 
        name="stuck", _registration_timeout_sec=0.05, _poll_interval=0.01))
    assert result["ok"] is False
    assert "did not register" in result["reason"]
    assert (paths.CLOSED / "stuck-sid.json").exists(), "closed record must be left intact for retry"


def test_resume_worker_unlinks_closed_record_after_registration_confirmed(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _make_transcript(tmp_path, monkeypatch, "good-sid")
    state.write_json_atomic(paths.CLOSED / "good-sid.json", {
        "claude_sid": "good-sid", "name": "good", "cwd": "/tmp/g", "closed_at": 1.0})
    _patch_spawn_registers_active(monkeypatch)
    result = _asyncio.run(_resume_worker_mcp(model="@opus", effort="xhigh", 
        name="good", _registration_timeout_sec=2.0, _poll_interval=0.01))
    assert result["ok"] is True
    assert result["sid"] == "good-sid"
    assert not (paths.CLOSED / "good-sid.json").exists(), "closed record must be deleted on success"


def test_resume_worker_claude_applies_remote_off_settings(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    _make_transcript(tmp_path, monkeypatch, "rc-sid")
    state.write_json_atomic(paths.CLOSED / "rc-sid.json", {
        "claude_sid": "rc-sid", "name": "rc-worker", "cwd": "/tmp/rc", "closed_at": 1.0})
    captured = _patch_spawn_registers_active(monkeypatch)
    result = _asyncio.run(_resume_worker_mcp(model="@opus", effort="xhigh", 
        name="rc-worker", _registration_timeout_sec=2.0, _poll_interval=0.01))
    assert result["ok"] is True
    assert (captured.get("extra_args") or [])[:2] == REMOTE_OFF_FLAGS
    assert "--remote-control" not in (captured.get("extra_args") or [])


def test_resume_worker_claude_honors_remote_control_opt_in(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_RC", "1")
    _make_transcript(tmp_path, monkeypatch, "rc-on-sid")
    state.write_json_atomic(paths.CLOSED / "rc-on-sid.json", {
        "claude_sid": "rc-on-sid", "name": "rc-on-worker", "cwd": "/tmp/rc", "closed_at": 1.0})
    captured = _patch_spawn_registers_active(monkeypatch)
    result = _asyncio.run(_resume_worker_mcp(model="@opus", effort="xhigh", 
        name="rc-on-worker", _registration_timeout_sec=2.0, _poll_interval=0.01))
    assert result["ok"] is True
    assert captured.get("extra_args") == [*RC_ON_FLAGS, *FLAGS]
    assert "--remote-control" in (captured.get("extra_args") or [])


def test_resume_worker_codex_skips_claude_remote_flags(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    _make_codex_transcript(tmp_path, monkeypatch, "cx-sid")
    state.write_json_atomic(paths.CLOSED / "cx-sid.json", {
        "claude_sid": "cx-sid", "name": "cx-worker", "cwd": "/tmp/cx", "runtime": "codex", "closed_at": 1.0})
    captured = _patch_spawn_registers_active(monkeypatch)
    result = _asyncio.run(_resume_worker_mcp(
        name="cx-worker", _registration_timeout_sec=2.0, _poll_interval=0.01))
    assert result["ok"] is True
    assert captured["runtime"] == "codex"
    assert not (captured.get("extra_args") or [])
    assert "--settings" not in (captured.get("extra_args") or [])


def test_resume_worker_rejects_name_already_active(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _make_transcript(tmp_path, monkeypatch, "old-sid")
    state.write_json_atomic(paths.CLOSED / "old-sid.json", {
        "claude_sid": "old-sid", "name": "tkt-1234", "cwd": "/x", "closed_at": 1.0})
    register_self_impl(claude_sid="new-sid", agent="worker", name="tkt-1234", cwd="/x", iterm_sid="i7")
    spawned = _patch_spawn_worker_tab(monkeypatch)
    with pytest.raises(ValueError, match="already active"):
        _asyncio.run(_resume_worker_mcp(
            name="tkt-1234", _registration_timeout_sec=0.05, _poll_interval=0.01))
    assert spawned == {}, "must not spawn a resume tab when the name is already live"
    assert (paths.CLOSED / "old-sid.json").exists(), (
        "closed record must survive — deleting it based on a foreign session's "
        "presence loses the resume pointer"
    )


def test_resume_worker_rejected_for_manager_holder_names_the_manager(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _make_transcript(tmp_path, monkeypatch, "old-sid")
    state.write_json_atomic(paths.CLOSED / "old-sid.json", {
        "claude_sid": "old-sid", "name": "happy-yak", "cwd": "/x", "closed_at": 1.0})
    register_self_impl(claude_sid="mgr-sid", agent="manager", name="happy-yak", cwd="/x", iterm_sid="i7")
    spawned = _patch_spawn_worker_tab(monkeypatch)
    with pytest.raises(ValueError, match="already active") as exc:
        _asyncio.run(_resume_worker_mcp(
            name="happy-yak", _registration_timeout_sec=0.05, _poll_interval=0.01))
    assert "manager" in str(exc.value)
    assert "send_manager_to_worker" not in str(exc.value)
    assert spawned == {}
    assert (paths.CLOSED / "old-sid.json").exists()


def test_resume_worker_ignores_foreign_name_claim_mid_window(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _make_transcript(tmp_path, monkeypatch, "old-sid")
    state.write_json_atomic(paths.CLOSED / "old-sid.json", {
        "claude_sid": "old-sid", "name": "tkt-1234", "cwd": "/x", "closed_at": 1.0})

    async def fake_spawn(**kwargs):
        state.write_json_atomic(paths.ACTIVE / "foreign-sid.json", {
            "claude_sid": "foreign-sid", "agent": "worker", "name": "tkt-1234",
            "cwd": "/x", "iterm_sid": "i9", "pid": os.getpid(), "started_at": 0,
        })
        return ("999", kwargs.get("name", ""))

    monkeypatch.setattr(spawner, "spawn_worker_tab", fake_spawn)
    result = _asyncio.run(_resume_worker_mcp(model="@opus", effort="xhigh", 
        name="tkt-1234", _registration_timeout_sec=0.1, _poll_interval=0.01))
    assert result["ok"] is False, (
        "a foreign session claiming the name mid-window must not confirm the resume"
    )
    assert (paths.CLOSED / "old-sid.json").exists(), (
        "closed record must survive a foreign name claim"
    )


def test_resume_worker_confirms_via_resumed_sid_and_returns_registered_name(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _make_transcript(tmp_path, monkeypatch, "old-sid")
    state.write_json_atomic(paths.CLOSED / "old-sid.json", {
        "claude_sid": "old-sid", "name": "tkt-1234", "cwd": "/x", "closed_at": 1.0})

    async def fake_spawn(**kwargs):
        state.write_json_atomic(paths.ACTIVE / "old-sid.json", {
            "claude_sid": "old-sid", "agent": "worker", "name": "tkt-1234-2",
            "cwd": "/x", "iterm_sid": "ir", "pid": os.getpid(), "started_at": 0,
        })
        return ("999", kwargs.get("name", ""))

    monkeypatch.setattr(spawner, "spawn_worker_tab", fake_spawn)
    result = _asyncio.run(_resume_worker_mcp(model="@opus", effort="xhigh", 
        name="tkt-1234", _registration_timeout_sec=2.0, _poll_interval=0.01))
    assert result["ok"] is True
    assert result["sid"] == "old-sid"
    assert result["name"] == "tkt-1234-2", (
        "must return the registered handle, not the requested name"
    )
    assert not (paths.CLOSED / "old-sid.json").exists()


def test_resume_worker_codex_accepts_new_sid_registration_under_name(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _make_codex_transcript(tmp_path, monkeypatch, "codex-old")
    state.write_json_atomic(paths.CLOSED / "codex-old.json", {
        "claude_sid": "codex-old", "name": "codex-worker", "cwd": "/x",
        "runtime": "codex", "closed_at": 1.0})

    async def fake_spawn(**kwargs):
        state.write_json_atomic(paths.ACTIVE / "codex-new.json", {
            "claude_sid": "codex-new", "agent": "worker", "name": "codex-worker",
            "cwd": "/x", "iterm_sid": "ir", "pid": os.getpid(), "started_at": 0,
            "runtime": "codex",
        })
        return ("999", kwargs.get("name", ""))

    monkeypatch.setattr(spawner, "spawn_worker_tab", fake_spawn)
    result = _asyncio.run(_resume_worker_mcp(
        name="codex-worker", _registration_timeout_sec=2.0, _poll_interval=0.01))
    assert result["ok"] is True
    assert result["sid"] == "codex-new", (
        "the result must point at the session that actually registered"
    )
    assert not (paths.CLOSED / "codex-old.json").exists()


def test_resume_worker_codex_fallback_ignores_non_worker_name_claim(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _make_codex_transcript(tmp_path, monkeypatch, "codex-old")
    state.write_json_atomic(paths.CLOSED / "codex-old.json", {
        "claude_sid": "codex-old", "name": "happy-yak", "cwd": "/x",
        "runtime": "codex", "closed_at": 1.0})

    async def fake_spawn(**kwargs):
        state.write_json_atomic(paths.ACTIVE / "mgr-new.json", {
            "claude_sid": "mgr-new", "agent": "manager", "name": "happy-yak",
            "cwd": "/x", "iterm_sid": "ir", "pid": os.getpid(), "started_at": 0,
            "runtime": "codex",
        })
        return ("999", kwargs.get("name", ""))

    monkeypatch.setattr(spawner, "spawn_worker_tab", fake_spawn)
    result = _asyncio.run(_resume_worker_mcp(
        name="happy-yak", _registration_timeout_sec=0.1, _poll_interval=0.01))
    assert result["ok"] is False
    assert (paths.CLOSED / "codex-old.json").exists()


def test_resume_worker_concurrent_second_call_refused(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _make_transcript(tmp_path, monkeypatch, "dup-sid")
    state.write_json_atomic(paths.CLOSED / "dup-sid.json", {
        "claude_sid": "dup-sid", "name": "dup-task", "cwd": "/x", "closed_at": 1.0})
    _patch_spawn_worker_tab(monkeypatch)

    async def scenario():
        first = _asyncio.create_task(_resume_worker_mcp(model="@opus", effort="xhigh", 
            name="dup-task", _registration_timeout_sec=0.5, _poll_interval=0.01))
        await _asyncio.sleep(0.05)
        with pytest.raises(ValueError, match="already in progress"):
            await _resume_worker_mcp(model="@opus", effort="xhigh", 
                name="dup-task", _registration_timeout_sec=0.5, _poll_interval=0.01)
        return await first

    result = _asyncio.run(scenario())
    assert result["ok"] is False
    assert (paths.CLOSED / "dup-sid.json").exists()


def test_resume_worker_refuses_when_resume_sid_already_active(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _make_transcript(tmp_path, monkeypatch, "old-sid")
    state.write_json_atomic(paths.CLOSED / "old-sid.json", {
        "claude_sid": "old-sid", "name": "tkt-1234", "cwd": "/x", "closed_at": 1.0})
    register_self_impl(claude_sid="old-sid", agent="worker", name="tkt-1234-2", cwd="/x", iterm_sid="i7")
    spawned = _patch_spawn_worker_tab(monkeypatch)
    with pytest.raises(ValueError, match="already active"):
        _asyncio.run(_resume_worker_mcp(
            name="tkt-1234", _registration_timeout_sec=0.05, _poll_interval=0.01))
    assert spawned == {}, "must not spawn a second process onto a live session's transcript"
    assert (paths.CLOSED / "old-sid.json").exists()


def test_kill_worker_closes_window_instead_of_sigterm(fresh_orchestrator_dir, monkeypatch):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=12345)

    closed = []
    monkeypatch.setattr(
        "dockwright.mcp_server._close_window",
        lambda window_id: closed.append(window_id),
    )
    killed = []
    monkeypatch.setattr(
        "dockwright.mcp_server.os.kill",
        lambda pid, sig: killed.append((pid, sig)),
    )
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)

    result = kill_worker_impl(worker="alpha", dry_run=False)

    assert closed == ["i1"], "graceful close must target the worker's pane id"
    assert killed == [], "kill_worker must not SIGTERM — graceful close fires SessionEnd"
    assert result["iterm_sid"] == "i1"
    assert "killed_pid" in result


def test_kill_worker_skips_close_when_pid_already_dead(fresh_orchestrator_dir, monkeypatch):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=12345)

    closed = []
    monkeypatch.setattr(
        "dockwright.mcp_server._close_window",
        lambda window_id: closed.append(window_id),
    )
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: False)

    result = kill_worker_impl(worker="alpha", dry_run=False)

    assert result.get("already_dead") is True
    assert closed == []


def test_kill_worker_swallows_terminal_failure(fresh_orchestrator_dir, monkeypatch):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=12345)

    def boom(*a, **k):
        raise OSError("tmux server gone")
    monkeypatch.setattr("dockwright.mcp_server.subprocess.run", boom)
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)

    result = kill_worker_impl(worker="alpha", dry_run=False)
    assert "killed_pid" in result


def test_become_manager_inherits_pane_id_from_env(fresh_orchestrator_dir, monkeypatch):
    terminal._DRIVER = None
    monkeypatch.setenv("TMUX_PANE", "77")
    become_manager_impl(claude_sid="mgr-env", iterm_sid="")
    record = state.read_json(paths.ACTIVE / "mgr-env.json")
    assert record["window_id"] == "77"


def test_become_manager_explicit_iterm_sid_wins_over_env(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "77")
    become_manager_impl(claude_sid="mgr-explicit", iterm_sid="i9")
    record = state.read_json(paths.ACTIVE / "mgr-explicit.json")
    assert record["window_id"] == "i9"


def test_become_manager_empty_iterm_sid_and_no_env_stays_empty(fresh_orchestrator_dir, monkeypatch):
    terminal._DRIVER = None
    monkeypatch.delenv("TMUX_PANE", raising=False)
    become_manager_impl(claude_sid="mgr-none", iterm_sid="")
    record = state.read_json(paths.ACTIVE / "mgr-none.json")
    assert record["window_id"] == ""


from dockwright.mcp_server import _input_is_idle, _input_state


def test_input_is_idle_empty_bordered_box():
    screen = "some output above\n╭──────────╮\n│ ❯                      │\n╰──────────╯\n  ? for shortcuts"
    assert _input_is_idle(screen) is True


def test_input_is_idle_bare_caret():
    assert _input_is_idle("❯ ") is True


def test_input_is_idle_typed_content_busy():
    assert _input_is_idle("│ ❯ do the migration first │") is False


def test_input_is_idle_queued_messages_busy():
    screen = "│ ❯                      │\n  ⏶ Press up to edit queued messages"
    assert _input_is_idle(screen) is False


def test_input_is_idle_empty_or_none_is_busy():
    assert _input_is_idle("") is False
    assert _input_is_idle(None) is False
    assert _input_is_idle("no caret here at all") is False


def test_input_is_idle_dim_placeholder_is_idle():
    screen = ("output above\n"
              "\x1b[39m❯ \x1b[2mSpawn a worker to investigate the codebase\x1b[0m\n"
              "  ? for shortcuts")
    assert _input_is_idle(screen) is True


def test_input_is_idle_ansi_typed_input_is_busy():
    assert _input_is_idle("\x1b[39m❯ \x1b[39mdo the migration first\x1b[0m") is False


def test_input_is_idle_ansi_empty_box_is_idle():
    assert _input_is_idle("\x1b[39m❯ \x1b[0m") is True


def test_input_is_idle_bare_reset_terminates_dim_span():
    assert _input_is_idle("\x1b[39m❯ \x1b[2msuggestion text\x1b[m") is True


def test_input_is_idle_bare_reset_ends_faint_so_later_text_is_busy():
    assert _input_is_idle("\x1b[39m❯ \x1b[2mghost\x1b[mREAL") is False


@pytest.mark.parametrize("screen, state", [
    (None, "no capture"), ("", "no capture"),
    ("no caret here at all", "no prompt"),
    ("│ ❯                      │\n  ⏶ Press up to edit queued messages", "queued"),
    ("│ ❯ do the migration first │", "draft"),
    ("\x1b[39m❯ \x1b[2mghost\x1b[mREAL", "draft"),
    ("❯ ", "idle"),
    ("\x1b[38;5;246m❯\xa0\x1b[39m", "idle"),
    ("\x1b[39m❯ \x1b[2mSpawn a worker to investigate the codebase\x1b[0m", "idle"),
])
def test_input_state_names_why_a_line_is_not_idle(screen, state):
    assert _input_state(screen) == state


PERMISSION_DIALOG = "Bash command\n  rm -rf build\n\nDo you want to proceed?\n❯ 1. Yes\n  2. No\n"
IDLE_LINE = "│ ❯                      │"
QUEUED_LINE = "│ ❯                      │\n  ⏶ Press up to edit queued messages"
DRAFT_LINE = "│ ❯ do the migration first │"


def test_the_permission_dialog_snapshot_is_not_an_idle_line():
    assert _input_state(PERMISSION_DIALOG) != "idle"


@pytest.mark.parametrize("screen,state", [
    (DRAFT_LINE + "\n  ⏶ Press up to edit queued messages", "draft"),
    (PERMISSION_DIALOG + "  ⏶ Press up to edit queued messages\n", "draft"),
    ("Press up to edit queued messages\n" + DRAFT_LINE, "draft"),
    ("Press up to edit queued messages\nno caret here", "no prompt"),
    (QUEUED_LINE, "queued"),
])
def test_a_non_empty_caret_line_is_a_draft_whatever_the_queued_hint_says(screen, state):
    assert _input_state(screen) == state


@pytest.mark.parametrize("screen,attached,refused", [
    (IDLE_LINE, False, False), (QUEUED_LINE, False, False), (DRAFT_LINE, False, True),
    ("no caret here", False, True), (None, False, True), (PERMISSION_DIALOG, False, True),
    (IDLE_LINE, True, False), (QUEUED_LINE, True, True),
    (DRAFT_LINE + "\n  ⏶ Press up to edit queued messages", False, True),
    (PERMISSION_DIALOG + "  ⏶ Press up to edit queued messages\n", False, True),
    ("Press up to edit queued messages\nno caret here", False, True)])
def test_reply_policy_lets_only_a_free_or_queued_line_through(monkeypatch, screen, attached, refused):
    import dockwright.mcp_server as srv
    monkeypatch.setattr(srv, "_capture_text", lambda window_id: screen)
    monkeypatch.setattr(srv, "is_human_attached", lambda record, now=None: attached)
    monkeypatch.setattr(srv, "_attached_until", lambda record: None)
    monkeypatch.setattr(srv, "_attached_refusal", lambda record: "attached")
    out = srv.reply_policy({"name": "w"}, "w1")
    if refused:
        assert out is not None and out["delivered"] is False and "line" in out
    else:
        assert out is None


def test_capture_text_uses_ansi_capture(monkeypatch):
    import dockwright.mcp_server as srv

    class _FakeDriver:
        def capture_screen(self, wid):
            raise AssertionError("must use ANSI capture, not plain capture_screen")

        def capture_screen_ansi(self, wid):
            return f"ansi:{wid}"

    monkeypatch.setattr(srv, "get_driver", lambda: _FakeDriver())
    assert srv._capture_text("%9") == "ansi:%9"


from dockwright.mcp_server import send_manager_to_manager_impl


def test_send_manager_to_manager_idle_delivers(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv
    state.write_json_atomic(paths.ACTIVE / "m2.json", {
        "claude_sid": "m2", "agent": "manager", "name": "peer", "window_id": "9"})
    monkeypatch.setattr(srv, "_capture_text", lambda wid: "❯ ")
    monkeypatch.setattr(srv, "_type_unguarded", lambda wid, txt: None)
    assert srv.send_manager_to_manager_impl("peer", "hi")["status"] == "delivered_live"


def test_send_manager_to_manager_busy_returns_peer_busy_no_inbox(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv
    state.write_json_atomic(paths.ACTIVE / "m2.json", {
        "claude_sid": "m2", "agent": "manager", "name": "peer", "window_id": "9"})
    monkeypatch.setattr(srv, "_capture_text", lambda wid: "❯ writing a reply...")
    typed = []
    monkeypatch.setattr(srv, "_type_unguarded", lambda wid, txt: typed.append(txt))
    r = srv.send_manager_to_manager_impl("peer", "hi")
    assert r["status"] == "peer_busy" and r["delivered"] is False and typed == []


def test_send_manager_to_manager_no_window_raises(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv
    state.write_json_atomic(paths.ACTIVE / "m2.json", {
        "claude_sid": "m2", "agent": "manager", "name": "peer", "window_id": ""})
    monkeypatch.setattr(srv, "_resolve_manager_window", lambda *a, **k: "")
    with pytest.raises(ValueError, match="no live window"):
        srv.send_manager_to_manager_impl("peer", "hi")


def test_send_manager_to_manager_unreadable_window_raises(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv
    state.write_json_atomic(paths.ACTIVE / "m2.json", {
        "claude_sid": "m2", "agent": "manager", "name": "peer", "window_id": "9"})
    monkeypatch.setattr(srv, "_capture_text", lambda wid: None)
    with pytest.raises(ValueError, match="unreadable"):
        srv.send_manager_to_manager_impl("peer", "hi")


def test_send_manager_to_manager_resolves_and_stamps_back(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv
    state.write_json_atomic(paths.ACTIVE / "m2.json", {
        "claude_sid": "m2", "agent": "manager", "name": "peer", "window_id": ""})
    monkeypatch.setattr(srv, "_resolve_manager_window", lambda *a, **k: "77")
    monkeypatch.setattr(srv, "_capture_text", lambda wid: "❯ ")
    sent = {}
    monkeypatch.setattr(srv, "_type_unguarded", lambda wid, txt: sent.update(wid=wid))
    r = srv.send_manager_to_manager_impl("peer", "hi")
    assert r["status"] == "delivered_live" and sent["wid"] == "77"
    assert state.read_json(paths.ACTIVE / "m2.json")["window_id"] == "77"


def test_send_manager_to_manager_unknown_name_raises(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="no manager named 'ghost'"):
        send_manager_to_manager_impl(name="ghost", text="hi")


def test_send_manager_to_manager_does_not_match_worker(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="not-a-mgr", cwd="/x", iterm_sid="i1")
    with pytest.raises(ValueError, match="no manager named 'not-a-mgr'"):
        send_manager_to_manager_impl(name="not-a-mgr", text="hi")


def test_send_manager_to_manager_dim_placeholder_delivers(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv
    state.write_json_atomic(paths.ACTIVE / "m2.json", {
        "claude_sid": "m2", "agent": "manager", "name": "peer", "window_id": "9"})
    monkeypatch.setattr(srv, "_capture_text",
                        lambda wid: "\x1b[39m❯ \x1b[2mSpawn a worker to investigate\x1b[0m")
    monkeypatch.setattr(srv, "_resolve_sender_manager", lambda: None)
    typed = []
    monkeypatch.setattr(srv, "_type_unguarded", lambda wid, txt: typed.append(txt))
    r = srv.send_manager_to_manager_impl("peer", "hi")
    assert r["status"] == "delivered_live" and typed == ["[MANAGER] hi"]


def test_send_manager_to_manager_ansi_typed_input_is_busy(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv
    state.write_json_atomic(paths.ACTIVE / "m2.json", {
        "claude_sid": "m2", "agent": "manager", "name": "peer", "window_id": "9"})
    monkeypatch.setattr(srv, "_capture_text",
                        lambda wid: "\x1b[39m❯ \x1b[39mhalf a thought\x1b[0m")
    typed = []
    monkeypatch.setattr(srv, "_type_unguarded", lambda wid, txt: typed.append(txt))
    r = srv.send_manager_to_manager_impl("peer", "hi")
    assert r["status"] == "peer_busy" and r["delivered"] is False and typed == []


def _peer_and_sender(monkeypatch, sender):
    import dockwright.mcp_server as srv
    state.write_json_atomic(paths.ACTIVE / "m2.json", {
        "claude_sid": "m2", "agent": "manager", "name": "peer", "window_id": "9"})
    monkeypatch.setattr(srv, "_capture_text", lambda wid: "❯ ")
    monkeypatch.setattr(srv, "_resolve_sender_manager", lambda: sender)
    typed = []
    monkeypatch.setattr(srv, "_type_unguarded", lambda wid, txt: typed.append(txt))
    return srv, typed


def test_send_manager_to_manager_stamps_sender_name_and_domain(fresh_orchestrator_dir, monkeypatch):
    srv, typed = _peer_and_sender(monkeypatch, {"name": "sender-mgr", "domain": "infra"})
    r = srv.send_manager_to_manager_impl("peer", "hi")
    assert typed == ["[MANAGER sender-mgr · infra] hi"]
    assert r["sender"] == "sender-mgr"


def test_send_manager_to_manager_stamp_prepends_once_multiline(fresh_orchestrator_dir, monkeypatch):
    srv, typed = _peer_and_sender(monkeypatch, {"name": "sender-mgr", "domain": "infra"})
    srv.send_manager_to_manager_impl("peer", "line one\nline two")
    assert typed == ["[MANAGER sender-mgr · infra] line one\nline two"]
    assert typed[0].count("[MANAGER ") == 1


def test_send_manager_to_manager_stamp_defaults_domain_when_record_has_none(fresh_orchestrator_dir, monkeypatch):
    srv, typed = _peer_and_sender(monkeypatch, {"name": "sender-mgr"})
    srv.send_manager_to_manager_impl("peer", "hi")
    assert typed == [f"[MANAGER sender-mgr · {srv.DEFAULT_DOMAIN}] hi"]


def test_send_manager_to_manager_unresolved_sender_still_marks_as_manager(fresh_orchestrator_dir, monkeypatch):
    srv, typed = _peer_and_sender(monkeypatch, None)
    r = srv.send_manager_to_manager_impl("peer", "hi")
    assert typed == ["[MANAGER] hi"]
    assert r["sender"] is None


def test_send_manager_to_manager_nameless_sender_record_still_marks_as_manager(fresh_orchestrator_dir, monkeypatch):
    srv, typed = _peer_and_sender(monkeypatch, {"name": "", "domain": "infra"})
    r = srv.send_manager_to_manager_impl("peer", "hi")
    assert typed == ["[MANAGER] hi"]
    assert r["sender"] is None


def test_send_manager_to_manager_leading_slash_arrives_as_plain_text(fresh_orchestrator_dir, monkeypatch):
    srv, typed = _peer_and_sender(monkeypatch, {"name": "sender-mgr", "domain": "infra"})
    srv.send_manager_to_manager_impl("peer", "/manager-close now")
    assert typed == ["[MANAGER sender-mgr · infra] /manager-close now"]
    assert not typed[0].startswith("/")


def test_send_manager_to_manager_peer_busy_stamps_nothing(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv
    state.write_json_atomic(paths.ACTIVE / "m2.json", {
        "claude_sid": "m2", "agent": "manager", "name": "peer", "window_id": "9"})
    monkeypatch.setattr(srv, "_capture_text", lambda wid: "❯ half a thought")
    monkeypatch.setattr(srv, "_resolve_sender_manager",
                        lambda: {"name": "sender-mgr", "domain": "infra"})
    typed = []
    monkeypatch.setattr(srv, "_type_unguarded", lambda wid, txt: typed.append(txt))
    r = srv.send_manager_to_manager_impl("peer", "hi")
    assert r["status"] == "peer_busy" and typed == []
    assert "sender" not in r


def test_send_manager_to_manager_stamps_from_active_record_without_patching_the_resolver(
        fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv
    state.write_json_atomic(paths.ACTIVE / "m1.json", {
        "claude_sid": "m1", "agent": "manager", "name": "sender-mgr",
        "domain": "infra", "window_id": "%7"})
    state.write_json_atomic(paths.ACTIVE / "m2.json", {
        "claude_sid": "m2", "agent": "manager", "name": "peer", "window_id": "9"})
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr(srv, "_capture_text", lambda wid: "❯ ")
    typed = []
    monkeypatch.setattr(srv, "_type_unguarded", lambda wid, txt: typed.append(txt))
    r = srv.send_manager_to_manager_impl("peer", "hi")
    assert typed == ["[MANAGER sender-mgr · infra] hi"]
    assert r["sender"] == "sender-mgr"


def test_send_manager_to_manager_delivers_when_sender_resolution_raises(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv
    from dockwright import identity

    def boom():
        raise ValueError("corrupt active record")

    state.write_json_atomic(paths.ACTIVE / "m2.json", {
        "claude_sid": "m2", "agent": "manager", "name": "peer", "window_id": "9"})
    monkeypatch.setattr(identity, "resolve_manager_record", boom)
    monkeypatch.setattr(srv, "_capture_text", lambda wid: "❯ ")
    typed = []
    monkeypatch.setattr(srv, "_type_unguarded", lambda wid, txt: typed.append(txt))
    r = srv.send_manager_to_manager_impl("peer", "hi")
    assert r["status"] == "delivered_live" and typed == ["[MANAGER] hi"]


def test_resolve_sender_manager_reads_the_identity_resolver(monkeypatch):
    import dockwright.mcp_server as srv
    from dockwright import identity
    monkeypatch.setattr(identity, "resolve_manager_record",
                        lambda: {"name": "who", "domain": "dom"})
    assert srv._resolve_sender_manager() == {"name": "who", "domain": "dom"}


def test_kill_worker_does_not_match_manager(fresh_orchestrator_dir):
    register_self_impl(claude_sid="m1", agent="manager", name="happy-yak", cwd="/x", iterm_sid="i1", pid=12345)
    with pytest.raises(ValueError, match="active manager") as exc:
        kill_worker_impl(worker="happy-yak", dry_run=True)
    assert "no worker named" not in str(exc.value)


def test_send_manager_to_worker_does_not_match_manager(fresh_orchestrator_dir, monkeypatch):
    register_self_impl(claude_sid="m1", agent="manager", name="happy-yak", cwd="/x", iterm_sid="42")
    typed = []
    monkeypatch.setattr(
        "dockwright.mcp_server._type_unguarded",
        lambda wid, text: typed.append((wid, text)),
    )
    with pytest.raises(ValueError, match="active manager") as exc:
        send_manager_to_worker_impl(worker="happy-yak", text="hi")
    assert "send_manager_to_manager" in str(exc.value)
    assert typed == []


def test_worker_finder_names_manager_holder_by_sid_too(fresh_orchestrator_dir):
    register_self_impl(claude_sid="mgr-sid-1", agent="manager", name="happy-yak", cwd="/x", iterm_sid="i1", pid=12345)
    with pytest.raises(ValueError, match="active manager"):
        kill_worker_impl(worker="mgr-sid-1", dry_run=True)
    with pytest.raises(ValueError, match="no worker named 'ghost'"):
        kill_worker_impl(worker="ghost", dry_run=True)


from dockwright.mcp_server import _type_unguarded


def test_send_text_uses_bracketed_paste_then_single_enter(fresh_orchestrator_dir, monkeypatch):
    terminal._DRIVER = None
    calls = []
    monkeypatch.setattr(
        "dockwright.mcp_server.subprocess.run",
        lambda args, **kw: (calls.append((list(args), kw)),
                            subprocess.CompletedProcess(args, 0, b"", b""))[1],
    )
    _type_unguarded("42", "do the migration first")
    assert len(calls) == 4
    load = next(a for a, _ in calls if "load-buffer" in a)
    paste = next(a for a, _ in calls if "paste-buffer" in a)
    enters = [a for a, _ in calls if "send-keys" in a and a[-1] == "Enter"]
    load_kw = next(kw for a, kw in calls if "load-buffer" in a)
    assert load_kw["input"] == b"do the migration first"
    assert "do the migration first" not in load
    assert "-p" in paste and "-t" in paste and paste[paste.index("-t") + 1] == "42"
    assert len(enters) == 1
    assert enters[0][enters[0].index("-t") + 1] == "42"


def test_send_text_multiline_arrives_whole(fresh_orchestrator_dir, monkeypatch):
    terminal._DRIVER = None
    calls = []
    monkeypatch.setattr(
        "dockwright.mcp_server.subprocess.run",
        lambda args, **kw: (calls.append((list(args), kw)),
                            subprocess.CompletedProcess(args, 0, b"", b""))[1],
    )
    multiline = "line one\nline two\n\nline four with trailing"
    _type_unguarded("7", multiline)
    load_kw = next(kw for a, kw in calls if "load-buffer" in a)
    assert load_kw["input"] == multiline.encode("utf-8")
    paste = next(a for a, _ in calls if "paste-buffer" in a)
    assert "-p" in paste
    enter_calls = [a for a, _ in calls if "send-keys" in a and a[-1] == "Enter"]
    assert len(enter_calls) == 1
    assert len(calls) == 4


def test_send_text_swallows_failure(fresh_orchestrator_dir, monkeypatch):
    def boom(args, **kw):
        raise FileNotFoundError("tmux not installed")

    monkeypatch.setattr("dockwright.mcp_server.subprocess.run", boom)
    _type_unguarded("42", "hi")


def test_become_manager_with_takeover_skips_close_when_no_window_resolves(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    become_manager_impl(claude_sid="mgr-old", iterm_sid="")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.mcp_server._terminal_ls", lambda: None)
    calls = []
    monkeypatch.setattr("dockwright.mcp_server.subprocess.run", lambda args, **kw: calls.append(args))
    result = become_manager_with_takeover_impl(
        claude_sid="mgr-new", takeover_from="mgr-old",
        handoff_id=handoff["handoff_id"], iterm_sid="",
    )
    assert result["ok"] is True
    close_calls = [c for c in calls if "close-window" in c]
    assert close_calls == []


def test_become_manager_with_takeover_resolves_window_via_ls_when_no_iterm_sid(fresh_orchestrator_dir, monkeypatch):
    terminal._DRIVER = None
    monkeypatch.delenv("TMUX_PANE", raising=False)
    old = become_manager_impl(claude_sid="mgr-old", iterm_sid="")
    old_name = old["name"]
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.mcp_server._terminal_ls", lambda: [
        {"tabs": [
            {"title": f"{old_name} · general", "windows": [
                {"id": 7, "title": f"{old_name} · general", "env": {}},
            ]},
            {"title": "manager (incoming)", "windows": [
                {"id": 9, "title": "manager (incoming)", "env": {}},
            ]},
        ]},
    ])
    closed = []
    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda wid: closed.append(wid))
    result = become_manager_with_takeover_impl(
        claude_sid="mgr-new", takeover_from="mgr-old",
        handoff_id=handoff["handoff_id"], iterm_sid="9",
    )
    assert result["ok"] is True
    assert closed == ["7"]


def test_resolve_manager_window_matches_session_id_env(fresh_orchestrator_dir, monkeypatch):
    from dockwright.mcp_server import _resolve_manager_window
    monkeypatch.setattr("dockwright.mcp_server._terminal_ls", lambda: [
        {"tabs": [
            {"title": "tab", "windows": [
                {"id": 3, "title": "no match", "env": {"CLAUDE_CODE_SESSION_ID": "mgr-old"}},
            ]},
        ]},
    ])
    assert _resolve_manager_window("mgr-old", "whatever", exclude_id="") == "3"


def test_resolve_manager_window_name_match_runs_unconditionally(fresh_orchestrator_dir, monkeypatch):
    from dockwright.mcp_server import _resolve_manager_window
    tree = [
        {"tabs": [
            {"title": "grumpy-yak · general", "windows": [
                {"id": 5, "title": "grumpy-yak · general", "env": {}},
            ]},
        ]},
    ]
    monkeypatch.setattr("dockwright.mcp_server._terminal_ls", lambda: tree)
    assert _resolve_manager_window("some-sid", "grumpy-yak", exclude_id="") == "5"
    assert _resolve_manager_window("some-sid", "grumpy-yak", exclude_id="9") == "5"


def test_kill_worker_resolves_window_id_records(fresh_orchestrator_dir, monkeypatch):
    state.write_json_atomic(paths.ACTIVE / "new-sid.json", {
        "claude_sid": "new-sid",
        "agent": "worker",
        "name": "new-worker",
        "cwd": "/x",
        "window_id": "new-win-1",
        "pid": os.getpid(),
        "started_at": 0,
        "state": "idle",
        "parent_manager_name": None,
    })
    result = kill_worker_impl(worker="new-worker", dry_run=True)
    assert result["iterm_sid"] == "new-win-1"


def test_kill_worker_resolves_legacy_iterm_sid_records(fresh_orchestrator_dir, monkeypatch):
    state.write_json_atomic(paths.ACTIVE / "legacy-sid.json", {
        "claude_sid": "legacy-sid",
        "agent": "worker",
        "name": "legacy-worker",
        "cwd": "/x",
        "iterm_sid": "leg-win-1",
        "pid": os.getpid(),
        "started_at": 0,
        "state": "idle",
        "parent_manager_name": None,
    })
    result = kill_worker_impl(worker="legacy-worker", dry_run=True)
    assert result["iterm_sid"] == "leg-win-1"


def test_list_managers_returns_iterm_sid_from_window_id_records(fresh_orchestrator_dir):
    from dockwright.mcp_server import list_managers
    become_manager_impl(claude_sid="mgr-1", iterm_sid="win-9", domain="general")
    out = list_managers()
    assert len(out) == 1
    assert out[0]["claude_sid"] == "mgr-1"
    assert out[0]["iterm_sid"] == "win-9"


def test_spawn_worker_rejects_unresolvable_manager_sid(fresh_orchestrator_dir, monkeypatch):
    captured = _patch_spawn_worker_tab(monkeypatch)
    with pytest.raises(ValueError) as exc:
        _asyncio.run(spawn_worker_impl(
            initial_prompt="hi",
            name="worker-rejected",
            manager_sid="snug-ibex",
        ))
    msg = str(exc.value)
    assert "snug-ibex" in msg
    assert "list_managers" in msg
    assert "manager_sid=None" in msg
    assert captured == {}
    assert list(paths.ASSIGNMENTS_PENDING.glob("*.json")) == []


def test_spawn_worker_rejects_worker_sid_as_manager_sid(fresh_orchestrator_dir, monkeypatch):
    register_self_impl(claude_sid="w-sid-1", agent="worker", name="some-worker",
                       cwd="/x", iterm_sid="i1")
    captured = _patch_spawn_worker_tab(monkeypatch)
    with pytest.raises(ValueError) as exc:
        _asyncio.run(spawn_worker_impl(
            initial_prompt="hi", name="worker-x", manager_sid="w-sid-1"))
    msg = str(exc.value)
    assert "active worker record" in msg
    assert "some-worker" in msg
    assert captured == {}
    assert list(paths.ASSIGNMENTS_PENDING.glob("*.json")) == []


def test_spawn_worker_rejects_nested_manager_record(fresh_orchestrator_dir, monkeypatch):
    state.write_json_atomic(paths.ACTIVE / "n-sid-1.json", {
        "claude_sid": "n-sid-1", "agent": "manager", "name": "nested-abc12345",
        "nested": True, "cwd": "/x", "window_id": "i2",
    })
    captured = _patch_spawn_worker_tab(monkeypatch)
    with pytest.raises(ValueError) as exc:
        _asyncio.run(spawn_worker_impl(
            initial_prompt="hi", name="worker-y", manager_sid="n-sid-1"))
    msg = str(exc.value)
    assert "active nested manager-agent record" in msg
    assert "nested-abc12345" in msg
    assert captured == {}
    assert list(paths.ASSIGNMENTS_PENDING.glob("*.json")) == []


def test_spawn_worker_rejects_manager_record_with_falsy_name(fresh_orchestrator_dir, monkeypatch):
    state.write_json_atomic(paths.ACTIVE / "noname-1.json", {
        "claude_sid": "noname-1", "agent": "manager", "name": "",
        "cwd": "/x", "window_id": "i3",
    })
    captured = _patch_spawn_worker_tab(monkeypatch)
    with pytest.raises(ValueError) as exc:
        _asyncio.run(spawn_worker_impl(
            initial_prompt="hi", name="worker-z", manager_sid="noname-1"))
    msg = str(exc.value)
    assert "manager record with no name" in msg
    assert "noname-1" in msg
    assert captured == {}
    assert list(paths.ASSIGNMENTS_PENDING.glob("*.json")) == []


def test_spawn_worker_no_warning_on_none_manager_sid(fresh_orchestrator_dir, monkeypatch):
    _patch_spawn_worker_tab(monkeypatch)
    result = _asyncio.run(spawn_worker_impl(
        initial_prompt="hi",
        name="worker-legacy",
        manager_sid=None,
    ))
    assert result["parent_manager_name"] is None
    assert "warning" not in result


def test_spawn_worker_no_warning_on_resolvable_manager_sid(fresh_orchestrator_dir, monkeypatch):
    become_manager_impl(claude_sid="mgr-a", iterm_sid="i0", domain="general")
    mgr_name = state.read_json(paths.ACTIVE / "mgr-a.json")["name"]
    _patch_spawn_worker_tab(monkeypatch)
    result = _asyncio.run(spawn_worker_impl(
        initial_prompt="hi",
        name="worker-scoped",
        manager_sid="mgr-a",
    ))
    assert result["parent_manager_name"] == mgr_name
    assert "warning" not in result


def test_resolve_parent_manager_branches(fresh_orchestrator_dir):
    from dockwright.mcp_server import _resolve_parent_manager
    become_manager_impl(claude_sid="mgr-a", iterm_sid="i0", domain="general")
    mgr_name = state.read_json(paths.ACTIVE / "mgr-a.json")["name"]
    assert _resolve_parent_manager(None) is None
    assert _resolve_parent_manager("") is None
    assert _resolve_parent_manager("mgr-a") == mgr_name
    with pytest.raises(ValueError, match="snug-ibex"):
        _resolve_parent_manager("snug-ibex")
    register_self_impl(claude_sid="w-1", agent="worker", name="wrk",
                       cwd="/x", iterm_sid="i9")
    with pytest.raises(ValueError, match="active worker record"):
        _resolve_parent_manager("w-1")
    state.write_json_atomic(paths.ACTIVE / "n-1.json", {
        "claude_sid": "n-1", "agent": "manager",
        "name": "nested-deadbeef", "nested": True,
    })
    with pytest.raises(ValueError, match="active nested manager-agent record"):
        _resolve_parent_manager("n-1")
    state.write_json_atomic(paths.ACTIVE / "noname-2.json", {
        "claude_sid": "noname-2", "agent": "manager",
    })
    with pytest.raises(ValueError, match="manager record with no name"):
        _resolve_parent_manager("noname-2")


def test_resolve_manager_name_for_filter_warns_to_stderr_on_unresolvable_sid(fresh_orchestrator_dir, capsys):
    from dockwright.mcp_server import _resolve_manager_name_for_filter
    become_manager_impl(claude_sid="mgr-a", iterm_sid="i0", domain="general")
    mgr_name = state.read_json(paths.ACTIVE / "mgr-a.json")["name"]

    assert _resolve_manager_name_for_filter("mgr-a", "list_workers") == mgr_name
    assert capsys.readouterr().err == ""

    assert _resolve_manager_name_for_filter(None, "list_workers") is None
    assert capsys.readouterr().err == ""

    assert _resolve_manager_name_for_filter("snug-ibex", "list_workers") is None
    err = capsys.readouterr().err
    assert "list_workers" in err
    assert "snug-ibex" in err
    assert "wildcard" in err


def test_takeover_inherits_funny_name_and_preserves_worker_routing(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda *a, **k: None)
    monkeypatch.setattr(
        "dockwright.mcp_server.names.roll_manager_name", lambda is_taken: "happy-otter"
    )
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i-old", domain="general")
    assert state.read_json(paths.ACTIVE / "mgr-old.json")["name"] == "happy-otter"
    register_self_impl(
        claude_sid="w-1", agent="worker", name="task-1", cwd="/x", iterm_sid="iw",
        pid=os.getpid(), parent_manager_name="happy-otter",
    )
    handoff = prepare_handoff_impl(
        claude_sid="mgr-old", narrative_summary="s", trigger_reason="recreate"
    )
    old = state.read_json(paths.ACTIVE / "mgr-old.json")
    old["pid"] = 2
    state.write_json_atomic(paths.ACTIVE / "mgr-old.json", old)
    become_manager_with_takeover_impl(
        claude_sid="mgr-new", takeover_from="mgr-old",
        handoff_id=handoff["handoff_id"], iterm_sid="i-new",
    )
    assert state.read_json(paths.ACTIVE / "mgr-new.json")["name"] == "happy-otter"
    workers = list_workers_impl(manager_name="happy-otter")
    assert [w["name"] for w in workers] == ["task-1"]


from dockwright.mcp_server import (
    artifact_put_impl, artifact_get_impl, artifact_list_impl,
)


def test_artifact_put_get_round_trips(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "spec", "srs", "# The Spec\n", "complete", "sid-w1")
    got = artifact_get_impl("TKT-SANDBOX-1", "spec", "srs")
    assert got["phase"] == "spec" and got["name"] == "srs"
    assert got["status"] == "complete" and got["writer_sid"] == "sid-w1"
    assert got["content"] == "# The Spec\n"
    assert got["read_set"] == [] and got["contract_hash"] is None


from dockwright.mcp_server import (
    artifact_put as _tool_artifact_put, artifact_get as _tool_artifact_get,
    artifact_list as _tool_artifact_list, artifact_view as _tool_artifact_view,
    pipeline_status as _tool_pipeline_status, pipeline_event as _tool_pipeline_event,
)


def test_artifact_put_accepts_task_key_and_ticket_alias(fresh_orchestrator_dir):
    _tool_artifact_put(task_key="K-1", phase="spec", name="srs",
                       content_path=_source(fresh_orchestrator_dir, "a", "a.md"),
                       status="complete", writer_sid="s1")
    assert artifact_get_impl("K-1", "spec", "srs")["content"] == "a"
    _tool_artifact_put(ticket="K-2", phase="spec", name="srs",
                       content_path=_source(fresh_orchestrator_dir, "b", "b.md"),
                       status="complete", writer_sid="s1")
    assert artifact_get_impl("K-2", "spec", "srs")["content"] == "b"
    _tool_artifact_put(task_key="K-3", ticket="K-2", phase="spec", name="srs",
                       content_path=_source(fresh_orchestrator_dir, "c", "c.md"), status="complete",
                       writer_sid="s1")
    assert artifact_get_impl("K-3", "spec", "srs")["content"] == "c"
    with pytest.raises(ValueError):
        _tool_artifact_put(phase="spec", name="srs", content_path=_source(fresh_orchestrator_dir, "d", "d.md"),
                           status="complete", writer_sid="s1")


def test_artifact_read_tools_accept_task_key_and_ticket_alias(fresh_orchestrator_dir):
    artifact_put_impl("K-1", "spec", "srs", "body", "complete", "s1")
    assert Path(_tool_artifact_get(task_key="K-1", phase="spec", name="srs")["content_path"]).read_text(
        encoding="utf-8") == "body"
    assert Path(_tool_artifact_get(ticket="K-1", phase="spec", name="srs")["content_path"]).read_text(
        encoding="utf-8") == "body"
    assert _tool_artifact_list(task_key="K-1")[0]["name"] == "srs"
    assert _tool_artifact_list(ticket="K-1")[0]["name"] == "srs"
    assert "K-1" in Path(_tool_artifact_view(task_key="K-1")["content_path"]).read_text(encoding="utf-8")
    assert "K-1" in _tool_pipeline_status(ticket="K-1")
    for tool in (_tool_artifact_get, _tool_artifact_list, _tool_artifact_view,
                 _tool_pipeline_status):
        with pytest.raises(ValueError):
            tool() if tool in (_tool_artifact_list, _tool_artifact_view,
                               _tool_pipeline_status) else tool(phase="spec", name="srs")


def _source(tmp_path, text, name="source.md"):
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return str(path)


def test_artifact_put_copies_the_file_byte_for_byte_and_outlives_it(fresh_orchestrator_dir):
    text = "título\n\ncuerpo — 1\n"
    source = _source(fresh_orchestrator_dir, text)
    _tool_artifact_put("K", phase="spec", name="a", content_path=source, status="complete", writer_sid="w")
    os.unlink(source)
    assert artifact_get_impl("K", "spec", "a")["content"] == text


@pytest.mark.parametrize("make, reason", [
    (lambda root: "relative.md", "absolute"),
    (lambda root: str(root), "not a regular file"),
    (lambda root: str(root / "missing.md"), "does not exist"),
    (lambda root: _source(root, "x" * (artifact_export.STORE_FILE_MAX + 1), "big.md"), "over the"),
    (lambda root: (root / "bad.md").write_bytes(b"\xff\xfe") and str(root / "bad.md"), "not UTF-8"),
])
def test_artifact_put_refuses_a_path_it_cannot_copy(fresh_orchestrator_dir, make, reason):
    with pytest.raises(ValueError, match=reason):
        _tool_artifact_put("K", phase="spec", name="a", content_path=make(fresh_orchestrator_dir),
                           status="complete", writer_sid="w")
    assert not paths.artifact_path("K", "spec", "a").exists()


def test_artifact_put_refuses_a_file_inside_the_store(fresh_orchestrator_dir):
    artifact_put_impl("K", "spec", "a", "body", "complete", "w")
    with pytest.raises(ValueError, match="inside the store"):
        _tool_artifact_put("K", phase="spec", name="b", content_path=str(paths.artifact_path("K", "spec", "a")),
                           status="complete", writer_sid="w")


@pytest.mark.parametrize("body", ["one line", "x" * 20 * 1024])
def test_artifact_get_returns_a_copy_of_the_body_and_no_text(fresh_orchestrator_dir, body):
    artifact_put_impl("K", "spec", "a", body, "complete", "w")
    got = _tool_artifact_get("K", phase="spec", name="a", reader_sid="r")
    assert "content" not in got
    copy = Path(got["content_path"])
    assert copy.read_bytes() == body.encode("utf-8")
    assert copy.is_relative_to(paths.EXPORTS)
    assert got["path"] == str(paths.artifact_path("K", "spec", "a"))


def test_a_second_read_reuses_the_copy_and_refreshes_its_mtime(fresh_orchestrator_dir):
    artifact_put_impl("K", "spec", "a", "body", "complete", "w")
    first = Path(_tool_artifact_get("K", phase="spec", name="a")["content_path"])
    hour_ago = time.time() - 3600
    os.utime(first, (hour_ago, hour_ago))
    inode = first.stat().st_ino
    second = Path(_tool_artifact_get("K", phase="spec", name="a")["content_path"])
    assert second == first
    assert second.stat().st_ino == inode
    assert second.stat().st_mtime > hour_ago + 1800


def test_one_read_records_one_artifact_read_event(fresh_orchestrator_dir):
    artifact_put_impl("K", "spec", "a", "body", "complete", "w")
    _tool_artifact_get("K", phase="spec", name="a", reader_sid="r")
    lines = paths.artifact_events_path("K").read_text().splitlines()
    assert sum('"artifact_read"' in line for line in lines) == 1


def test_a_copy_older_than_a_day_is_removed_on_the_next_export(fresh_orchestrator_dir):
    artifact_put_impl("K", "spec", "a", "old", "complete", "w")
    artifact_put_impl("K", "spec", "b", "new", "complete", "w")
    old = Path(_tool_artifact_get("K", phase="spec", name="a")["content_path"])
    day_ago = time.time() - artifact_export.EXPORT_MAX_AGE_SEC - 60
    os.utime(old, (day_ago, day_ago))
    _tool_artifact_get("K", phase="spec", name="b")
    assert not old.exists()


@pytest.mark.parametrize("read", [
    lambda: _tool_artifact_get("K", phase="spec", name="a")["content_path"],
    lambda: _tool_artifact_view("K")["content_path"],
])
def test_a_copy_edited_in_place_is_rewritten_from_the_store_on_the_next_read(fresh_orchestrator_dir, read):
    artifact_put_impl("K", "spec", "a", "stored", "complete", "w")
    first = Path(read())
    expected = first.read_bytes()
    first.write_text("edited, never stored", encoding="utf-8")
    again = Path(read())
    assert again == first
    assert again.read_bytes() == expected


def test_the_export_folder_is_private_to_its_user(fresh_orchestrator_dir):
    artifact_put_impl("K", "spec", "a", "body", "complete", "w")
    _tool_artifact_get("K", phase="spec", name="a")
    assert paths.EXPORTS.stat().st_mode & 0o777 == 0o700


def test_an_export_folder_left_open_is_closed_before_use(fresh_orchestrator_dir):
    paths.EXPORTS.mkdir()
    os.chmod(paths.EXPORTS, 0o777)
    artifact_put_impl("K", "spec", "a", "body", "complete", "w")
    _tool_artifact_get("K", phase="spec", name="a")
    assert paths.EXPORTS.stat().st_mode & 0o777 == 0o700


def test_an_export_folder_that_is_a_symlink_is_refused(fresh_orchestrator_dir):
    elsewhere = fresh_orchestrator_dir / "elsewhere"
    elsewhere.mkdir()
    paths.EXPORTS.symlink_to(elsewhere)
    artifact_put_impl("K", "spec", "a", "body", "complete", "w")
    with pytest.raises(ValueError, match="not a folder this user owns"):
        _tool_artifact_get("K", phase="spec", name="a")
    assert list(elsewhere.iterdir()) == []


def test_artifact_view_writes_the_fold_to_a_file(fresh_orchestrator_dir):
    artifact_put_impl("K", "spec", "a", "the body", "complete", "w")
    got = _tool_artifact_view("K")
    assert set(got) == {"content_path"}
    assert "the body" in Path(got["content_path"]).read_text(encoding="utf-8")


def test_pipeline_event_accepts_task_key_and_ticket_alias(fresh_orchestrator_dir):
    _tool_pipeline_event(task_key="K-1", type="note", reason="via task_key")
    _tool_pipeline_event(ticket="K-1", type="note", reason="via ticket")
    lines = [json.loads(l) for l in
             paths.artifact_events_path("K-1").read_text().splitlines()]
    assert [l["reason"] for l in lines if l["type"] == "note"] == ["via task_key", "via ticket"]
    with pytest.raises(ValueError):
        _tool_pipeline_event(type="note")


from dockwright.mcp_server import _read_events, _write_artifact_atomic, _put_clobber_verdict


def test_artifact_put_refuses_clobber_of_hand_authored_file(fresh_orchestrator_dir):
    path = paths.artifact_path("TKT-SANDBOX-9", "review", "report")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# full hand-written review\nlots of detail\n")
    with pytest.raises(ValueError, match="artifact_put refused"):
        artifact_put_impl("TKT-SANDBOX-9", "review", "report", "condensed summary",
                          "complete", "w1")
    assert path.read_text() == "# full hand-written review\nlots of detail\n"
    events = _read_events(paths.artifact_events_path("TKT-SANDBOX-9"))
    assert [e["type"] for e in events] == ["artifact_put_refused"]
    assert events[0]["reason"] == "non_record_file"
    assert events[0]["actor_sid"] == "w1"


def test_artifact_put_refuses_foreign_writer_record(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-9", "spec", "repo", "body A", "partial", "writer-a")
    with pytest.raises(ValueError, match="artifact_put refused"):
        artifact_put_impl("TKT-SANDBOX-9", "spec", "repo", "body B", "complete", "writer-b")
    got = artifact_get_impl("TKT-SANDBOX-9", "spec", "repo")
    assert got["writer_sid"] == "writer-a"
    assert got["content"] == "body A"
    events = _read_events(paths.artifact_events_path("TKT-SANDBOX-9"))
    assert events[-1]["type"] == "artifact_put_refused"
    assert events[-1]["reason"] == "foreign_record"


def test_artifact_put_same_writer_update_flows(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-9", "spec", "repo", "draft", "partial", "w1")
    artifact_put_impl("TKT-SANDBOX-9", "spec", "repo", "final", "complete", "w1")
    got = artifact_get_impl("TKT-SANDBOX-9", "spec", "repo")
    assert got["status"] == "complete"
    assert got["content"] == "final"


def test_artifact_put_overwrite_flag_replaces(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-9", "spec", "repo", "body A", "complete", "writer-a")
    artifact_put_impl("TKT-SANDBOX-9", "spec", "repo", "successor body", "complete",
                      "writer-b", overwrite=True)
    got = artifact_get_impl("TKT-SANDBOX-9", "spec", "repo")
    assert got["writer_sid"] == "writer-b"
    assert got["content"] == "successor body"
    events = _read_events(paths.artifact_events_path("TKT-SANDBOX-9"))
    assert events[-1]["type"] == "artifact_put"
    assert events[-1]["overwrite"] is True


def test_artifact_put_stamps_identical_hand_content(fresh_orchestrator_dir):
    path = paths.artifact_path("TKT-SANDBOX-9", "review", "report")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("full verdict text")
    artifact_put_impl("TKT-SANDBOX-9", "review", "report", "full verdict text",
                      "complete", "w1")
    got = artifact_get_impl("TKT-SANDBOX-9", "review", "report")
    assert got["content"] == "full verdict text"
    assert got["writer_sid"] == "w1"


def test_write_artifact_atomic_exclusive_raises_on_existing(tmp_path):
    p = tmp_path / "a.md"
    p.write_text("occupant")
    with pytest.raises(FileExistsError):
        _write_artifact_atomic(p, "new", exclusive=True)
    assert p.read_text() == "occupant"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_artifact_put_eexist_race_reruns_guard_refuse(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as m
    real = m._write_artifact_atomic
    injected = {"done": False}

    def racing(p, text, exclusive=False):
        if exclusive and not injected["done"]:
            injected["done"] = True
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(state.serialize_artifact(
                {"phase": "spec", "name": "repo", "status": "partial",
                 "writer_sid": "intruder", "contract_hash": None,
                 "written_at": 1.0, "read_set": []}, "foreign body"))
        return real(p, text, exclusive=exclusive)

    monkeypatch.setattr(m, "_write_artifact_atomic", racing)
    with pytest.raises(ValueError, match="artifact_put refused"):
        artifact_put_impl("TKT-SANDBOX-9", "spec", "repo", "mine", "complete", "w1")
    got = artifact_get_impl("TKT-SANDBOX-9", "spec", "repo")
    assert got["writer_sid"] == "intruder"
    assert got["content"] == "foreign body"


def test_artifact_put_eexist_race_allows_same_writer(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as m
    real = m._write_artifact_atomic
    injected = {"done": False}

    def racing(p, text, exclusive=False):
        if exclusive and not injected["done"]:
            injected["done"] = True
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(state.serialize_artifact(
                {"phase": "spec", "name": "repo", "status": "partial",
                 "writer_sid": "w1", "contract_hash": None,
                 "written_at": 1.0, "read_set": []}, "earlier attempt"))
        return real(p, text, exclusive=exclusive)

    monkeypatch.setattr(m, "_write_artifact_atomic", racing)
    res = artifact_put_impl("TKT-SANDBOX-9", "spec", "repo", "mine", "complete", "w1")
    got = artifact_get_impl("TKT-SANDBOX-9", "spec", "repo")
    assert got["content"] == "mine"
    prev = paths.artifact_path("TKT-SANDBOX-9", "spec", "repo").with_name("spec.repo.md.prev")
    _, prev_body = state.parse_artifact(prev.read_text())
    assert prev_body == "earlier attempt"
    assert res["archived_previous"] == str(prev)
    assert _last_event("TKT-SANDBOX-9")["archived_previous"] == str(prev)


def test_artifact_put_second_eexist_propagates(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as m

    def always_eexist(p, text, exclusive=False):
        if exclusive:
            raise FileExistsError(str(p))
        raise AssertionError("non-exclusive write must not be reached")

    monkeypatch.setattr(m, "_write_artifact_atomic", always_eexist)
    with pytest.raises(FileExistsError):
        artifact_put_impl("TKT-SANDBOX-9", "spec", "repo", "mine", "complete", "w1")


def test_artifact_put_eexist_reguard_refuses_own_complete_record(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as m
    real = m._write_artifact_atomic
    injected = {"done": False}

    def racing(p, text, exclusive=False):
        if exclusive and not injected["done"]:
            injected["done"] = True
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(state.serialize_artifact(
                {"phase": "spec", "name": "repo", "status": "complete",
                 "writer_sid": "w1", "contract_hash": None,
                 "written_at": 1.0, "read_set": []}, "earlier final body"))
        return real(p, text, exclusive=exclusive)

    monkeypatch.setattr(m, "_write_artifact_atomic", racing)
    with pytest.raises(ValueError, match="artifact_put refused"):
        artifact_put_impl("TKT-SANDBOX-9", "spec", "repo", "mine", "complete", "w1")
    got = artifact_get_impl("TKT-SANDBOX-9", "spec", "repo")
    assert got["writer_sid"] == "w1"
    assert got["content"] == "earlier final body"
    assert _last_event("TKT-SANDBOX-9")["reason"] == "own_complete_record"


def test_artifact_put_eexist_reguard_refuses_unknown_verdict(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as m
    calls = {"n": 0}

    def fake_verdict(*a, **k):
        calls["n"] += 1
        return "absent" if calls["n"] == 1 else "some_future_verdict"

    def always_eexist(p, text, exclusive=False):
        if exclusive:
            raise FileExistsError(str(p))
        raise AssertionError("non-exclusive write must not be reached")

    monkeypatch.setattr(m, "_put_clobber_verdict", fake_verdict)
    monkeypatch.setattr(m, "_write_artifact_atomic", always_eexist)
    with pytest.raises(ValueError, match="artifact_put refused"):
        artifact_put_impl("TKT-SANDBOX-9", "spec", "repo", "mine", "complete", "w1")
    assert calls["n"] == 2
    assert _last_event("TKT-SANDBOX-9")["reason"] == "some_future_verdict"


def test_artifact_put_tool_threads_overwrite(fresh_orchestrator_dir):
    _tool_artifact_put(task_key="TKT-SANDBOX-9", phase="spec", name="srs",
                       content_path=_source(fresh_orchestrator_dir, "a", "a.md"),
                       status="complete", writer_sid="w1")
    _tool_artifact_put(task_key="TKT-SANDBOX-9", phase="spec", name="srs",
                       content_path=_source(fresh_orchestrator_dir, "b", "b.md"),
                       status="complete", writer_sid="w2", overwrite=True)
    assert Path(_tool_artifact_get(task_key="TKT-SANDBOX-9", phase="spec",
                                   name="srs")["content_path"]).read_text(encoding="utf-8") == "b"


def test_partial_then_complete_same_writer_overwrites(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "spec", "srs", "v1", "partial", "sid-w1")
    artifact_put_impl("TKT-SANDBOX-1", "spec", "srs", "v2", "complete", "sid-w1")
    got = artifact_get_impl("TKT-SANDBOX-1", "spec", "srs")
    assert got["status"] == "complete" and got["content"] == "v2"
    md_files = list(paths.artifact_ticket_dir("TKT-SANDBOX-1").glob("*.md"))
    assert len(md_files) == 1


def test_artifact_list_returns_stamps_no_body_sorted(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "spec", "b-repo", "x", "complete", "s1")
    artifact_put_impl("TKT-SANDBOX-1", "plan", "a-repo", "y", "partial", "s2")
    out = artifact_list_impl("TKT-SANDBOX-1")
    assert [(a["phase"], a["name"]) for a in out] == [("plan", "a-repo"), ("spec", "b-repo")]
    assert all("content" not in a for a in out)


def test_artifact_get_missing_raises(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="no artifact"):
        artifact_get_impl("TKT-SANDBOX-1", "spec", "missing")


def test_invalid_status_rejected(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="status"):
        artifact_put_impl("TKT-SANDBOX-1", "spec", "srs", "x", "done", "s1")


def test_path_traversal_sanitized(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "spec", "../../etc/passwd", "x", "complete", "s1")
    (entry,) = artifact_list_impl("TKT-SANDBOX-1")
    resolved = Path(entry["path"]).resolve()
    assert str(resolved).startswith(str((paths.ARTIFACTS / "TKT-SANDBOX-1").resolve()))


def test_atomic_no_tmp_left(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "spec", "srs", "x", "complete", "s1")
    assert not list(paths.artifact_ticket_dir("TKT-SANDBOX-1").glob("*.tmp"))


def test_concurrent_puts_distinct_names_zero_loss(fresh_orchestrator_dir):
    n = 16
    threads = [threading.Thread(
        target=artifact_put_impl,
        args=("TKT-SANDBOX-1", "implement", f"repo-{i}", f"body-{i}", "complete", f"sid-{i}"))
        for i in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(artifact_list_impl("TKT-SANDBOX-1")) == n


def test_artifact_put_emits_event(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "spec", "srs", "x", "complete", "sid-w1")
    lines = [json.loads(l) for l in paths.artifact_events_path("TKT-SANDBOX-1").read_text().splitlines()]
    (ev,) = [l for l in lines if l["type"] == "artifact_put"]
    assert ev["actor_sid"] == "sid-w1" and ev["status"] == "complete"


def _last_event(task_key):
    return _read_events(paths.artifact_events_path(task_key))[-1]


def _seed_raw_record(task_key, phase, name, body, stamp_overrides=None):
    stamp = {"phase": phase, "name": name, "status": "complete",
             "writer_sid": "sid-w1", "contract_hash": None,
             "written_at": 0.0, "read_set": []}
    stamp.update(stamp_overrides or {})
    p = paths.artifact_path(task_key, phase, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(state.serialize_artifact(stamp, body))
    return p


def test_artifact_put_refuses_own_complete_record_different_content(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "FULL REPORT", "complete", "sid-w1")
    p = paths.artifact_path("TKT-SANDBOX-1", "review", "verdict")
    before = p.read_text()
    with pytest.raises(ValueError, match="final"):
        artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "condensed summary", "complete", "sid-w1")
    assert p.read_text() == before
    ev = _last_event("TKT-SANDBOX-1")
    assert ev["type"] == "artifact_put_refused" and ev["reason"] == "own_complete_record"


def test_artifact_put_refuses_status_demotion_of_own_final_record(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "FULL REPORT", "complete", "sid-w1")
    p = paths.artifact_path("TKT-SANDBOX-1", "review", "verdict")
    before = p.read_text()
    with pytest.raises(ValueError, match="demot"):
        artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "FULL REPORT", "partial", "sid-w1")
    assert p.read_text() == before
    assert _last_event("TKT-SANDBOX-1")["reason"] == "demotes_final"


def test_artifact_put_refuses_partial_stamping_of_hand_file(fresh_orchestrator_dir):
    p = paths.artifact_path("TKT-SANDBOX-1", "review", "verdict")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# hand-written full verdict\n")
    with pytest.raises(ValueError, match="demot"):
        artifact_put_impl("TKT-SANDBOX-1", "review", "verdict",
                          "# hand-written full verdict\n", "partial", "sid-w1")
    assert p.read_text() == "# hand-written full verdict\n"
    assert _last_event("TKT-SANDBOX-1")["reason"] == "demotes_final"


def test_artifact_put_refuses_colliding_record(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "review", "api.v2", "occupant body", "partial", "sid-w1")
    p = paths.artifact_path("TKT-SANDBOX-1", "review", "api.v2")
    assert p == paths.artifact_path("TKT-SANDBOX-1", "review", "api_v2")
    before = p.read_text()
    with pytest.raises(ValueError, match="collid"):
        artifact_put_impl("TKT-SANDBOX-1", "review", "api_v2", "different body", "partial", "sid-w1")
    assert p.read_text() == before
    assert _last_event("TKT-SANDBOX-1")["reason"] == "colliding_record"


def test_artifact_put_own_complete_identical_reput_allowed(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "FINAL BODY", "complete", "sid-w1")
    res = artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "FINAL BODY", "complete", "sid-w1")
    assert res["ok"] is True
    assert artifact_get_impl("TKT-SANDBOX-1", "review", "verdict")["content"] == "FINAL BODY"


def test_artifact_put_overwrite_replaces_own_complete_record(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "FULL REPORT", "complete", "sid-w1")
    res = artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "revised", "complete", "sid-w1",
                            overwrite=True)
    assert res["ok"] is True
    assert artifact_get_impl("TKT-SANDBOX-1", "review", "verdict")["content"] == "revised"
    ev = _last_event("TKT-SANDBOX-1")
    assert ev["type"] == "artifact_put" and ev["overwrite"] is True


def test_artifact_put_rejects_falsy_writer_sid(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="writer_sid"):
        artifact_put_impl("TKT-SANDBOX-1", "spec", "srs", "x", "complete", "")
    with pytest.raises(ValueError, match="writer_sid"):
        artifact_put_impl("TKT-SANDBOX-1", "spec", "srs", "x", "complete", None)
    assert not list(paths.ARTIFACTS.glob("**/*.md"))


def test_put_clobber_verdict_fail_closed_axes(fresh_orchestrator_dir):
    p1 = _seed_raw_record("TKT-SANDBOX-1", "spec", "a", "body", {"writer_sid": ""})
    assert _put_clobber_verdict(p1, "spec", "a", "other", "complete", "") == "foreign_record"
    p2 = _seed_raw_record("TKT-SANDBOX-1", "spec", "b", "body", {"status": None})
    assert _put_clobber_verdict(p2, "spec", "b", "other", "complete", "sid-w1") == "own_complete_record"
    p3 = _seed_raw_record("TKT-SANDBOX-1", "spec", "c", "body", {"phase": None})
    assert _put_clobber_verdict(p3, "spec", "c", "other", "complete", "sid-w1") == "colliding_record"
    p4 = paths.artifact_path("TKT-SANDBOX-1", "spec", "d")
    p4.write_text('---\nphase: "spec"\nname: "d"\nstatus: notjson\n'
                  'writer_sid: "sid-w1"\n---\nbody')
    assert _put_clobber_verdict(p4, "spec", "d", "other", "complete", "sid-w1") == "own_complete_record"


def test_artifact_put_unknown_verdict_fails_closed(fresh_orchestrator_dir, monkeypatch):
    artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "PRECIOUS", "complete", "sid-w1")
    p = paths.artifact_path("TKT-SANDBOX-1", "review", "verdict")
    before = p.read_text()
    monkeypatch.setattr(mcp_server, "_put_clobber_verdict",
                        lambda *a, **k: "some_future_verdict")
    with pytest.raises(ValueError, match="some_future_verdict"):
        artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "stomper", "complete", "sid-OTHER")
    assert p.read_text() == before
    assert _last_event("TKT-SANDBOX-1")["reason"] == "some_future_verdict"


def test_artifact_put_partial_replacement_archives_previous(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "FULL 514-line report", "partial", "sid-w1")
    p = paths.artifact_path("TKT-SANDBOX-1", "review", "verdict")
    original = p.read_text()
    res = artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "summary", "complete", "sid-w1")
    prev = p.with_name(p.name + ".prev")
    assert prev.read_text() == original
    assert res["archived_previous"] == str(prev)
    assert _last_event("TKT-SANDBOX-1")["archived_previous"] == str(prev)
    assert artifact_get_impl("TKT-SANDBOX-1", "review", "verdict")["content"] == "summary"


def test_artifact_put_overwrite_archives_replaced_record(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "FULL REPORT", "complete", "sid-w1")
    p = paths.artifact_path("TKT-SANDBOX-1", "review", "verdict")
    original = p.read_text()
    res = artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "revised", "complete", "sid-w1",
                            overwrite=True)
    prev = p.with_name(p.name + ".prev")
    assert prev.read_text() == original
    assert res["archived_previous"] == str(prev)


def test_artifact_put_idempotent_reput_no_sidecar(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "FINAL BODY", "complete", "sid-w1")
    res = artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "FINAL BODY", "complete", "sid-w1")
    p = paths.artifact_path("TKT-SANDBOX-1", "review", "verdict")
    assert "archived_previous" not in res
    assert not p.with_name(p.name + ".prev").exists()


def test_artifact_put_sidecar_latest_only(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "v1", "partial", "sid-w1")
    artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "v2", "partial", "sid-w1")
    artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "v3", "partial", "sid-w1")
    p = paths.artifact_path("TKT-SANDBOX-1", "review", "verdict")
    prev_text = p.with_name(p.name + ".prev").read_text()
    _, prev_body = state.parse_artifact(prev_text)
    assert prev_body == "v2"


def test_artifact_put_archive_failure_fails_closed(fresh_orchestrator_dir, monkeypatch):
    artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "FULL REPORT", "partial", "sid-w1")
    p = paths.artifact_path("TKT-SANDBOX-1", "review", "verdict")
    before = p.read_text()
    events_before = paths.artifact_events_path("TKT-SANDBOX-1").read_text()

    def boom(path, content):
        raise OSError("disk full")

    monkeypatch.setattr(mcp_server, "_archive_replaced", boom)
    with pytest.raises(OSError):
        artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "summary", "complete", "sid-w1")
    assert p.read_text() == before
    assert paths.artifact_events_path("TKT-SANDBOX-1").read_text() == events_before


def test_artifact_put_real_archive_failure_fails_closed(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "FULL REPORT", "partial", "sid-w1")
    p = paths.artifact_path("TKT-SANDBOX-1", "review", "verdict")
    before = p.read_text()
    events_before = paths.artifact_events_path("TKT-SANDBOX-1").read_text()
    prev = p.with_name(p.name + ".prev")
    prev.mkdir()
    (prev / "blocker").write_text("occupied")
    with pytest.raises(OSError):
        artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "summary", "complete", "sid-w1")
    assert p.read_text() == before
    assert paths.artifact_events_path("TKT-SANDBOX-1").read_text() == events_before


def test_sidecar_invisible_to_readers(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "v1", "partial", "sid-w1")
    artifact_put_impl("TKT-SANDBOX-1", "review", "verdict", "v2", "complete", "sid-w1")
    assert [a["name"] for a in artifact_list_impl("TKT-SANDBOX-1")] == ["verdict"]
    assert not list(paths.artifact_ticket_dir("TKT-SANDBOX-1").glob("*.tmp"))


_INCOMING = "incoming content"
_OTHER_BODY = "previous different body"

_MATRIX_EXPECT = {
    ("absent", "partial", False): ("write", False),
    ("absent", "complete", False): ("write", False),
    ("absent", "partial", True): ("write", False),
    ("absent", "complete", True): ("write", False),
    ("own_partial", "partial", False): ("write", True),
    ("own_partial", "complete", False): ("write", True),
    ("own_partial", "partial", True): ("write", True),
    ("own_partial", "complete", True): ("write", True),
    ("own_final_same_body", "partial", False): "demotes_final",
    ("own_final_same_body", "complete", False): ("write", False),
    ("own_final_same_body", "partial", True): ("write", False),
    ("own_final_same_body", "complete", True): ("write", False),
    ("own_final_diff_body", "partial", False): "own_complete_record",
    ("own_final_diff_body", "complete", False): "own_complete_record",
    ("own_final_diff_body", "partial", True): ("write", True),
    ("own_final_diff_body", "complete", True): ("write", True),
    ("own_status_null", "partial", False): "own_complete_record",
    ("own_status_null", "complete", False): "own_complete_record",
    ("own_status_null", "partial", True): ("write", True),
    ("own_status_null", "complete", True): ("write", True),
    ("own_status_missing_key", "partial", False): "own_complete_record",
    ("own_status_missing_key", "complete", False): "own_complete_record",
    ("own_status_missing_key", "partial", True): ("write", True),
    ("own_status_missing_key", "complete", True): ("write", True),
    ("colliding_record", "partial", False): "colliding_record",
    ("colliding_record", "complete", False): "colliding_record",
    ("colliding_record", "partial", True): ("write", True),
    ("colliding_record", "complete", True): ("write", True),
    ("foreign_record", "partial", False): "foreign_record",
    ("foreign_record", "complete", False): "foreign_record",
    ("foreign_record", "partial", True): ("write", True),
    ("foreign_record", "complete", True): ("write", True),
    ("hand_file_equal", "partial", False): "demotes_final",
    ("hand_file_equal", "complete", False): ("write", False),
    ("hand_file_equal", "partial", True): ("write", False),
    ("hand_file_equal", "complete", True): ("write", False),
    ("hand_file_diff", "partial", False): "non_record_file",
    ("hand_file_diff", "complete", False): "non_record_file",
    ("hand_file_diff", "partial", True): ("write", True),
    ("hand_file_diff", "complete", True): ("write", True),
}


def _seed_occupant(kind, task_key):
    phase, name = "review", "tar_get"
    p = paths.artifact_path(task_key, phase, name)
    if kind == "absent":
        return p
    if kind == "own_partial":
        artifact_put_impl(task_key, phase, name, _OTHER_BODY, "partial", "sid-w1")
    elif kind == "own_final_same_body":
        artifact_put_impl(task_key, phase, name, _INCOMING, "complete", "sid-w1")
    elif kind == "own_final_diff_body":
        artifact_put_impl(task_key, phase, name, _OTHER_BODY, "complete", "sid-w1")
    elif kind == "own_status_null":
        _seed_raw_record(task_key, phase, name, _OTHER_BODY, {"status": None})
    elif kind == "own_status_missing_key":
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('---\nphase: "review"\nname: "tar_get"\nstatus: notjson\n'
                     'writer_sid: "sid-w1"\n---\n' + _OTHER_BODY)
    elif kind == "colliding_record":
        artifact_put_impl(task_key, phase, "tar.get", _OTHER_BODY, "partial", "sid-w1")
        assert paths.artifact_path(task_key, phase, "tar.get") == p
    elif kind == "foreign_record":
        _seed_raw_record(task_key, phase, name, _OTHER_BODY, {"writer_sid": "sid-OTHER"})
    elif kind == "hand_file_equal":
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_INCOMING)
    elif kind == "hand_file_diff":
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_OTHER_BODY)
    else:
        raise AssertionError(f"unknown occupant kind {kind!r}")
    return p


@pytest.mark.parametrize("occupant,incoming_status,overwrite",
                         sorted(_MATRIX_EXPECT))
def test_artifact_put_guard_matrix(fresh_orchestrator_dir, occupant,
                                   incoming_status, overwrite):
    task_key = "TKT-SANDBOX-1"
    p = _seed_occupant(occupant, task_key)
    before = p.read_text() if occupant != "absent" else None
    prev = p.with_name(p.name + ".prev")
    expect = _MATRIX_EXPECT[(occupant, incoming_status, overwrite)]
    if isinstance(expect, tuple):
        _, sidecar_expected = expect
        res = artifact_put_impl(task_key, "review", "tar_get", _INCOMING,
                                incoming_status, "sid-w1", overwrite=overwrite)
        assert res["ok"] is True
        _, body = state.parse_artifact(p.read_text())
        assert body == _INCOMING
        if sidecar_expected:
            assert prev.read_text() == before
            assert res["archived_previous"] == str(prev)
        else:
            assert not prev.exists()
            assert "archived_previous" not in res
    else:
        with pytest.raises(ValueError):
            artifact_put_impl(task_key, "review", "tar_get", _INCOMING,
                              incoming_status, "sid-w1", overwrite=overwrite)
        assert p.read_text() == before
        assert not prev.exists()
        assert _last_event(task_key)["reason"] == expect


def test_artifact_put_signature_pin():
    assert list(inspect.signature(artifact_put_impl).parameters) == [
        "task_key", "phase", "name", "content", "status", "writer_sid",
        "contract_hash", "read_set", "overwrite", "scope", "decided_by"]
    assert list(inspect.signature(_tool_artifact_put).parameters) == [
        "task_key", "phase", "name", "content_path", "status", "writer_sid",
        "contract_hash", "read_set", "overwrite", "scope", "decided_by", "ticket"]


from dockwright.mcp_server import (
    _join_worker_liveness, pipeline_status_impl, artifact_view_impl, pipeline_event_impl,
)


def test_pipeline_status_joins_liveness(fresh_orchestrator_dir, monkeypatch):
    _probes(monkeypatch)
    register_self_impl(claude_sid="sid-live", agent="worker", name="w-live", cwd="/x", iterm_sid="i1", pid=_LIVE)
    artifact_put_impl("TKT-SANDBOX-1", "spec", "a", "x", "complete", "sid-live")
    done_dir = paths.done_dir_for("mgr-name")
    done_dir.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(done_dir / "sid-done-ev1.json",
                            {"event_id": "ev1", "claude_sid": "sid-done", "summary": "ok"})
    artifact_put_impl("TKT-SANDBOX-1", "spec", "b", "x", "complete", "sid-done")
    out = pipeline_status_impl("TKT-SANDBOX-1")
    assert "(active)" in out and "(done)" in out


def test_join_liveness_runtime_from_closed_record(fresh_orchestrator_dir):
    done_dir = paths.done_dir_for("mgr-name")
    done_dir.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(done_dir / "sid-cx-ev1.json",
                            {"event_id": "ev1", "claude_sid": "sid-cx"})
    state.write_json_atomic(paths.CLOSED / "sid-cx.json",
                            {"claude_sid": "sid-cx", "name": "w-cx", "runtime": "codex"})
    liveness, runtime = _join_worker_liveness("sid-cx")
    assert (liveness, runtime) == ("done", "codex")


def test_artifact_view_renders_all(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "spec", "a", "AAA-body", "complete", "s1")
    artifact_put_impl("TKT-SANDBOX-1", "plan", "b", "BBB-body", "partial", "s2")
    out = artifact_view_impl("TKT-SANDBOX-1")
    assert "spec.a" in out and "plan.b" in out
    assert "AAA-body" in out and "BBB-body" in out


def test_artifact_view_survives_corrupt_frontmatter_stamp(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "spec", "good", "GOOD-body", "complete", "s1")
    artifact_put_impl("TKT-SANDBOX-1", "review", "bad", "BAD-body", "complete", "s2")
    bad_path = paths.artifact_path("TKT-SANDBOX-1", "review", "bad")
    text = bad_path.read_text()
    text = text.replace('phase: "review"', "phase: {corrupt")
    text = text.replace('name: "bad"', "name: {corrupt")
    bad_path.write_text(text)
    out = artifact_view_impl("TKT-SANDBOX-1")
    assert "spec.good" in out and "GOOD-body" in out
    assert "review.bad" in out and "BAD-body" in out


def test_events_reader_skips_malformed_trailing_line(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "spec", "a", "x", "complete", "s1")
    with open(paths.artifact_events_path("TKT-SANDBOX-1"), "a") as f:
        f.write('{"type":"note","trunc')
    out = pipeline_status_impl("TKT-SANDBOX-1")
    assert "artifact_put" in out


def test_pipeline_event_appends(fresh_orchestrator_dir):
    pipeline_event_impl("TKT-SANDBOX-1", "dispatch", phase="implement", name="srs",
                        reason="fan-out", actor_sid="mgr-1")
    out = pipeline_status_impl("TKT-SANDBOX-1")
    assert "dispatch" in out and "fan-out" in out


from dockwright.mcp_server import _prune_stale_artifacts


def _age(path, days):
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def test_prune_removes_stale_ticket_dir(fresh_orchestrator_dir):
    artifact_put_impl("TKT-OLD", "spec", "a", "x", "complete", "s1")
    artifact_put_impl("TKT-NEW", "spec", "a", "x", "complete", "s1")
    for p in paths.artifact_ticket_dir("TKT-OLD").rglob("*"):
        _age(p, 31)
    _age(paths.artifact_ticket_dir("TKT-OLD"), 31)
    _prune_stale_artifacts()
    assert not paths.artifact_ticket_dir("TKT-OLD").exists()
    assert paths.artifact_ticket_dir("TKT-NEW").exists()


def test_prune_sweeps_orphan_tmp(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "spec", "a", "x", "complete", "s1")
    d = paths.artifact_ticket_dir("TKT-SANDBOX-1")
    stale_tmp = d / ".spec.a.999.deadbeef.tmp"
    fresh_tmp = d / ".spec.a.999.cafebabe.tmp"
    stale_tmp.write_text("x")
    fresh_tmp.write_text("x")
    _age(stale_tmp, 1)
    _prune_stale_artifacts()
    assert not stale_tmp.exists() and fresh_tmp.exists()


from dockwright.mcp_server import _unkeyed_key_hint, _current_branch


@pytest.fixture
def configured_key_regex(monkeypatch, tmp_path):
    p = tmp_path / "dockwright.toml"
    p.write_text("[task_keys]\nkey_regex = '[A-Za-z]{2,}-\\d+'\n")
    monkeypatch.setenv(_config.ENV_CONFIG_PATH, str(p))
    return p


@pytest.fixture
def no_orch_config(monkeypatch, tmp_path):
    monkeypatch.setenv(_config.ENV_CONFIG_PATH, str(tmp_path / "nope.toml"))


def test_spawn_worker_writes_pending_assignment(fresh_orchestrator_dir, monkeypatch,
                                                configured_key_regex):
    captured = _patch_spawn_worker_tab(monkeypatch)
    register_self_impl(claude_sid="mgr-1", agent="manager", name="boss", cwd="/x", iterm_sid="i9")
    _asyncio.run(spawn_worker_impl(
        initial_prompt="/ticket-start TKT-8353 extra context",
        name="tkt-8353-dlq-fix", cwd="/tmp", manager_sid="mgr-1",
        task_key="TKT-8353"))
    (pending,) = list(paths.ASSIGNMENTS_PENDING.glob("*.json"))
    record = state.read_json(pending)
    assert record["initial_prompt"] == "/ticket-start TKT-8353 extra context"
    assert record["requested_name"] == "tkt-8353-dlq-fix"
    assert record["ticket"] == "TKT-8353"
    assert record["parent_manager_name"] == "boss"
    assert record["manager_sid"] == "mgr-1"
    assert record["runtime"] == "claude"
    assert captured["env"]["CLAUDE_ASSIGNMENT_ID"] == pending.stem


def test_spawn_worker_no_derivation_no_footer_without_config(fresh_orchestrator_dir, monkeypatch,
                                                             no_orch_config):
    captured = _patch_spawn_worker_tab(monkeypatch)
    result = _asyncio.run(spawn_worker_impl(
        initial_prompt="/ticket-start TKT-8353 extra", name="w1", cwd="/tmp"))
    (pending,) = list(paths.ASSIGNMENTS_PENDING.glob("*.json"))
    assert state.read_json(pending)["ticket"] is None
    assert "[orchestrator] Artifact discipline" not in captured["initial_prompt"]
    assert "task_key_hint" not in result


def test_spawn_env_injected_from_config(fresh_orchestrator_dir, monkeypatch, tmp_path):
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text('[spawn.env]\nFOO = "bar"\nSHARED = "from-config"\n')
    monkeypatch.setenv(_config.ENV_CONFIG_PATH, str(cfg))
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="task", name="w1", cwd="/tmp", env={"SHARED": "from-caller"}))
    assert captured["env"]["FOO"] == "bar"
    assert captured["env"]["SHARED"] == "from-caller"


def test_spawn_env_absent_by_default(fresh_orchestrator_dir, monkeypatch, no_orch_config):
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(initial_prompt="task", name="w1", cwd="/tmp"))
    assert "WORKER_AUTONOMOUS" not in captured["env"]


def test_spawn_env_absent_by_default_codex_unaffected(fresh_orchestrator_dir, monkeypatch,
                                                      tmp_path):
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text('[spawn.env]\nFOO = "bar"\n[codex]\nallowed_roots = ["/tmp"]\n')
    monkeypatch.setenv(_config.ENV_CONFIG_PATH, str(cfg))
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(initial_prompt="task", name="cx", cwd="/tmp", runtime="codex"))
    assert "FOO" not in captured["env"]


def test_spawn_worker_pending_prompt_is_pre_preset(fresh_orchestrator_dir, monkeypatch):
    captured = _patch_spawn_worker_tab(monkeypatch)
    paths.PRESETS.mkdir(parents=True, exist_ok=True)
    (paths.PRESETS / "boiler.md").write_text("BOILERPLATE")
    _asyncio.run(spawn_worker_impl(initial_prompt="the ask", name="w1", cwd="/tmp", preset="boiler"))
    (pending,) = list(paths.ASSIGNMENTS_PENDING.glob("*.json"))
    record = state.read_json(pending)
    assert record["initial_prompt"] == "the ask"
    assert record["preset"] == "boiler"
    assert "BOILERPLATE" in captured["initial_prompt"]


def test_spawn_failure_unlinks_pending(fresh_orchestrator_dir, monkeypatch):
    async def boom(**kwargs):
        raise OSError("tmux down")
    monkeypatch.setattr(spawner, "spawn_worker_tab", boom)
    with pytest.raises(RuntimeError):
        _asyncio.run(spawn_worker_impl(initial_prompt="x", name="w1", cwd="/tmp"))
    assert list(paths.ASSIGNMENTS_PENDING.glob("*.json")) == []


def test_unkeyed_hint_fires_on_prompt_or_name_mention(configured_key_regex):
    assert "TKT-4242" in _unkeyed_key_hint("scout", "background: TKT-4242 was affected")
    assert "tkt-4242" in _unkeyed_key_hint("tkt-4242-fix", "free text")
    assert _unkeyed_key_hint("scout", "no key here") is None


def test_unkeyed_hint_is_conditional_never_a_recommendation(configured_key_regex):
    hint = _unkeyed_key_hint("scout", "see TKT-4242 for background")
    assert "mention is not an assignment" in hint
    assert "UNKEYED" in hint


def test_unkeyed_hint_none_without_config(no_orch_config):
    assert _unkeyed_key_hint("scout", "the ask TKT-8353") is None


def test_unkeyed_hint_invalid_regex_falls_to_none(monkeypatch, tmp_path):
    p = tmp_path / "dockwright.toml"
    p.write_text("[task_keys]\nkey_regex = '[A-Za-z'\n")
    monkeypatch.setenv(_config.ENV_CONFIG_PATH, str(p))
    assert _unkeyed_key_hint("w1", "the ask TKT-8353") is None


def test_current_branch_best_effort(tmp_path):
    assert _current_branch(str(tmp_path)) is None
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "my-branch"], cwd=repo, check=True)
    assert _current_branch(str(repo)) == "my-branch"


def test_spawn_footer_injected_with_explicit_task_key(fresh_orchestrator_dir, monkeypatch):
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="build the scraper", name="yt-scraper", cwd="/tmp",
        task_key="demo-task"))
    text = captured["initial_prompt"]
    assert "[orchestrator] Artifact discipline — task_key: `demo-task`" in text
    assert 'artifact_put(task_key="demo-task"' in text
    assert text.startswith("build the scraper")


def test_spawn_footer_absent_on_prompt_mention_without_explicit_key(
        fresh_orchestrator_dir, monkeypatch, configured_key_regex):
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="/ticket-start TKT-8353 extra", name="tkt-8353-fix", cwd="/tmp"))
    assert "[orchestrator] Artifact discipline" not in captured["initial_prompt"]


def test_spawn_footer_absent_when_no_key_resolves(fresh_orchestrator_dir, monkeypatch):
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(initial_prompt="just poke around", name="scout", cwd="/tmp"))
    assert "[orchestrator] Artifact discipline" not in captured["initial_prompt"]


def test_spawn_footer_absent_on_blank_prompt_even_with_key(fresh_orchestrator_dir, monkeypatch):
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(initial_prompt="", name="bare", cwd="/tmp", task_key="TKT-SANDBOX-1"))
    assert captured["initial_prompt"] == ""


def test_spawn_footer_lands_after_preset_boilerplate(fresh_orchestrator_dir, monkeypatch,
                                                     configured_key_regex):
    captured = _patch_spawn_worker_tab(monkeypatch)
    paths.PRESETS.mkdir(parents=True, exist_ok=True)
    (paths.PRESETS / "boiler.md").write_text("BOILERPLATE")
    _asyncio.run(spawn_worker_impl(
        initial_prompt="the ask TKT-9", name="w1", cwd="/tmp", preset="boiler",
        task_key="TKT-9"))
    text = captured["initial_prompt"]
    assert text.index("BOILERPLATE") < text.index("the ask TKT-9") \
        < text.index("[orchestrator] Artifact discipline")


def test_spawn_footer_not_in_assignment_record(fresh_orchestrator_dir, monkeypatch):
    _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(initial_prompt="do X", name="w1", cwd="/tmp", task_key="TKT-SANDBOX-2"))
    (pending,) = list(paths.ASSIGNMENTS_PENDING.glob("*.json"))
    record = state.read_json(pending)
    assert record["initial_prompt"] == "do X"
    assert record["ticket"] == "TKT-SANDBOX-2"


def test_spawn_footer_present_for_codex_runtime(fresh_orchestrator_dir, monkeypatch, tmp_path):
    allow_codex(monkeypatch, tmp_path)
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="port the bot", name="cx-bot", cwd="/tmp",
        task_key="demo-task", runtime="codex"))
    assert "[orchestrator] Artifact discipline" in captured["initial_prompt"]
    assert "Store/publish failures are non-blocking" in captured["initial_prompt"]


def _install_worker_agent(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    agents.mkdir(exist_ok=True)
    (agents / "worker.md").write_text("---\nname: worker\n---\n\n# Worker Agent\n\nbody\n")
    monkeypatch.setattr(paths, "AGENTS_DIR", agents)


def test_spawn_footer_short_when_the_worker_agent_is_in_context(
        fresh_orchestrator_dir, monkeypatch, tmp_path):
    _install_worker_agent(tmp_path, monkeypatch)
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="build the scraper", name="yt-scraper", cwd="/tmp",
        task_key="demo-task"))
    text = captured["initial_prompt"]
    assert "[orchestrator] Artifact discipline — task_key: `demo-task`" in text
    assert 'artifact_put(task_key="demo-task"' in text
    assert "Persist pipeline artifacts" in text
    assert "Store/publish failures are non-blocking" not in text


def test_spawn_footer_long_when_no_worker_agent_file(fresh_orchestrator_dir, monkeypatch):
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="build the scraper", name="yt-scraper", cwd="/tmp",
        task_key="demo-task"))
    assert "Store/publish failures are non-blocking" in captured["initial_prompt"]


def test_spawn_footer_long_when_caller_owns_the_system_prompt(
        fresh_orchestrator_dir, monkeypatch, tmp_path):
    _install_worker_agent(tmp_path, monkeypatch)
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="build the scraper", name="yt-scraper", cwd="/tmp",
        task_key="demo-task", extra_args=["--append-system-prompt", "mine"]))
    assert "Store/publish failures are non-blocking" in captured["initial_prompt"]


def test_repo_sync_footer_injected_without_task_key(fresh_orchestrator_dir, monkeypatch):
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="just poke around", name="scout", cwd="/tmp"))
    text = captured["initial_prompt"]
    assert "[orchestrator] Repo freshness" in text
    assert text.startswith("just poke around")


def test_repo_sync_footer_absent_on_blank_prompt(fresh_orchestrator_dir, monkeypatch):
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(initial_prompt="", name="bare", cwd="/tmp"))
    assert captured["initial_prompt"] == ""


def test_repo_sync_footer_present_for_codex_runtime(fresh_orchestrator_dir, monkeypatch, tmp_path):
    allow_codex(monkeypatch, tmp_path)
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="port the bot", name="cx-scout", cwd="/tmp", runtime="codex"))
    assert "[orchestrator] Repo freshness" in captured["initial_prompt"]


def test_repo_sync_footer_not_in_assignment_record(fresh_orchestrator_dir, monkeypatch):
    _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(initial_prompt="do X", name="w1", cwd="/tmp"))
    (pending,) = list(paths.ASSIGNMENTS_PENDING.glob("*.json"))
    assert state.read_json(pending)["initial_prompt"] == "do X"


def test_repo_sync_footer_lands_after_artifact_footer(fresh_orchestrator_dir, monkeypatch):
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="build the scraper", name="w1", cwd="/tmp",
        task_key="demo-task"))
    text = captured["initial_prompt"]
    assert text.index("[orchestrator] Artifact discipline") \
        < text.index("[orchestrator] Repo freshness")


def test_repo_sync_footer_names_the_git_recipe():
    text = _repo_sync_footer()
    assert "fetch origin main" in text
    assert "merge --ff-only origin/main" in text
    assert "rebase origin/main" in text
    assert "git rebase --abort" in text
    assert "git show origin/main:<path>" in text


def test_repo_sync_footer_is_headless_approvable():
    footer = mcp_server._repo_sync_footer()
    assert "git -C" not in footer
    assert "&&" not in footer
    assert "cd <repo>" in footer


from dockwright import config


def _install_role(tmp_path, monkeypatch, name="GenericWorker", launch=("worker",)):
    roles_dir = tmp_path / "roles"
    role_dir = roles_dir / name
    role_dir.mkdir(parents=True, exist_ok=True)
    launches = ", ".join(f'"{kind}"' for kind in launch)
    (role_dir / "role.toml").write_text(f'core = "{name}"\nlaunch = [{launches}]\n')
    for kind in launch:
        (role_dir / f"role.{kind}.md").write_text(f"<!-- dockwright-role: {name} launch={kind} -->\nBODY\n")
    (role_dir / "role.settings.json").write_text(json.dumps({"skillOverrides": {"skill-x": "name-only"}}))
    monkeypatch.setattr(paths, "ROLES_DIR", roles_dir)
    return role_dir


def _install_profiles(tmp_path, monkeypatch, repos=None, agents=()):
    settings = {"sets": {"none": [], "common": ["acme"], "back": ["acme", "acme-be"]},
                "repos": repos or {}, "work_roots": [str(tmp_path / "work")], "work_default": "back"}
    monkeypatch.setattr(config, "profiles", lambda: settings)
    for profile in agents:
        (paths.ROLES_DIR / "_agents").mkdir(parents=True, exist_ok=True)
        (paths.ROLES_DIR / "_agents" / f"{profile}.json").write_text("{}")
    return settings


def _profile_render(role_dir, launch, profile):
    name = role_dir.name
    path = role_dir / f"role.{launch}.{profile}.md"
    path.write_text(f"<!-- dockwright-role: {name} launch={launch} -->\n<!-- dockwright-profile: {profile} -->\nBODY\n")
    return path


def _git_repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_spawn_worker_takes_the_profile_of_its_cwd_and_its_agents_file(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _install_worker_agent(tmp_path, monkeypatch)
    role_dir = _install_role(tmp_path, monkeypatch)
    render = _profile_render(role_dir, "worker", "back")
    _install_profiles(tmp_path, monkeypatch, repos={"acme-api": "back"}, agents=("back",))
    repo = _git_repo(tmp_path / "work" / "acme-api")
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(initial_prompt="task", name="w", cwd=str(repo), extra_args=FLAGS))
    args = captured["extra_args"]
    assert args[:2] == ["--append-system-prompt-file", str(render)]
    assert args.count("--settings") == 1
    assert args[-2:] == ["--agents-file", str(paths.ROLES_DIR / "_agents" / "back.json")]
    command = spawner._runtime_command("claude", "task", args)
    assert command.startswith(f"{shlex.quote(str(paths.dockwright_bin()))} launch ")
    assert "--agents-file" in command and "--agents " not in command


def test_spawn_worker_without_an_agents_file_carries_no_agents_flag(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _install_worker_agent(tmp_path, monkeypatch)
    role_dir = _install_role(tmp_path, monkeypatch)
    _profile_render(role_dir, "worker", "back")
    _install_profiles(tmp_path, monkeypatch, repos={"acme-api": "back"})
    repo = _git_repo(tmp_path / "work" / "acme-api")
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(initial_prompt="task", name="w", cwd=str(repo)))
    assert "--agents-file" not in captured["extra_args"]


def test_spawn_worker_without_cwd_takes_the_worker_homes_profile(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _install_worker_agent(tmp_path, monkeypatch)
    role_dir = _install_role(tmp_path, monkeypatch)
    render = _profile_render(role_dir, "worker", "back")
    _install_profiles(tmp_path, monkeypatch, repos={"worker": "back"})
    home = tmp_path / "work" / "worker"
    home.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_HOME", str(home))
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(initial_prompt="task", name="w"))
    assert captured["extra_args"][:2] == ["--append-system-prompt-file", str(render)]


def test_the_tmux_command_stays_short_with_a_large_agents_file(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _install_worker_agent(tmp_path, monkeypatch)
    _install_role(tmp_path, monkeypatch)
    _install_profiles(tmp_path, monkeypatch, agents=("none",))
    (paths.ROLES_DIR / "_agents" / "none.json").write_text(json.dumps({"general-purpose": {"prompt": "x" * 200_000}}))
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(initial_prompt="task", name="w", cwd="/tmp", extra_args=FLAGS))
    args = captured["extra_args"]
    assert args[-2:] == ["--agents-file", str(paths.ROLES_DIR / "_agents" / "none.json")]
    assert len(spawner._runtime_command("claude", "task", args)) < 4096


def test_spawn_worker_without_manifests_keeps_todays_command(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _install_worker_agent(tmp_path, monkeypatch)
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(initial_prompt="task", name="w", cwd="/tmp", extra_args=FLAGS))
    assert "--append-system-prompt-file" not in captured["extra_args"]
    assert str(paths.AGENTS_DIR / "worker.md") in spawner._runtime_command("claude", "task", captured["extra_args"])
    assert list(state.list_json_in(paths.ASSIGNMENTS_PENDING))[0]["context_role"] is None


def test_spawn_worker_loads_the_generic_worker_role(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _install_worker_agent(tmp_path, monkeypatch)
    role_dir = _install_role(tmp_path, monkeypatch)
    captured = _patch_spawn_worker_tab(monkeypatch)
    result = _asyncio.run(spawn_worker_impl(initial_prompt="task", name="w", cwd="/tmp", task_key="k-1", extra_args=FLAGS))
    args = captured["extra_args"]
    assert args[:2] == ["--append-system-prompt-file", str(role_dir / "role.worker.md")]
    assert args.count("--settings") == 1
    settings = json.loads(Path(args[args.index("--settings") + 1]).read_text())
    assert settings["skillOverrides"] == {"skill-x": "name-only"}
    assert settings["enableAllProjectMcpServers"] is True
    assert str(paths.AGENTS_DIR / "worker.md") not in spawner._runtime_command("claude", "task", args)
    pending = list(state.list_json_in(paths.ASSIGNMENTS_PENDING))[0]
    assert pending["context_role"] == "GenericWorker" and result["context_role"] == "GenericWorker"
    assert "Store/publish failures are non-blocking" not in captured["initial_prompt"]


def test_spawn_worker_names_its_role(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _install_role(tmp_path, monkeypatch)
    coder = _install_role(tmp_path, monkeypatch, name="Coder")
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(initial_prompt="task", name="w", cwd="/tmp", context_role="Coder"))
    assert captured["extra_args"][:2] == ["--append-system-prompt-file", str(coder / "role.worker.md")]
    assert list(state.list_json_in(paths.ASSIGNMENTS_PENDING))[0]["context_role"] == "Coder"


def test_spawn_worker_refuses_an_unknown_role_once_manifests_exist(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _install_role(tmp_path, monkeypatch)
    captured = _patch_spawn_worker_tab(monkeypatch)
    with pytest.raises(ValueError, match="unknown role 'Nope'"):
        _asyncio.run(spawn_worker_impl(initial_prompt="task", name="w", cwd="/tmp", context_role="Nope"))
    assert captured == {} and list(paths.ASSIGNMENTS_PENDING.glob("*")) == []


def test_spawn_worker_refuses_a_role_beside_its_own_system_prompt(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _install_role(tmp_path, monkeypatch)
    _patch_spawn_worker_tab(monkeypatch)
    with pytest.raises(ValueError, match="exclusive"):
        _asyncio.run(spawn_worker_impl(initial_prompt="task", name="w", cwd="/tmp", context_role="GenericWorker",
                                       extra_args=["--append-system-prompt", "mine"]))


def test_spawn_worker_with_its_own_system_prompt_loads_no_role(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _install_role(tmp_path, monkeypatch)
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(initial_prompt="task", name="w", cwd="/tmp",
                                   extra_args=["--append-system-prompt", "mine"]))
    assert "--append-system-prompt-file" not in captured["extra_args"]
    assert list(state.list_json_in(paths.ASSIGNMENTS_PENDING))[0]["context_role"] is None


@pytest.mark.parametrize("env,launch", [(None, "worker"), ({"CLAUDE_WORKER_KIND": "subworker"}, "child")])
def test_every_claude_worker_launch_loads_the_generic_worker_role_once_manifests_exist(
        monkeypatch, tmp_path, env, launch):
    _install_worker_agent(tmp_path, monkeypatch)
    role_dir = _install_role(tmp_path, monkeypatch, launch=("worker", "child"))
    captured = _patch_exec(monkeypatch)
    _asyncio.run(spawner.spawn_worker_tab(cwd="/tmp/x", initial_prompt="", name="alpha", resume_sid="s1",
                                          extra_args=[*FLAGS, "--settings", '{"enableAllProjectMcpServers": true}'], env=env))
    words = shlex.split(captured["args"][-1].split(" launch ", 1)[1])
    assert words[words.index("--append-system-prompt-file") + 1] == str(role_dir / f"role.{launch}.md")
    assert str(paths.AGENTS_DIR / "worker.md") not in words and words.count("--settings") == 1
    settings = json.loads(Path(words[words.index("--settings") + 1]).read_text())
    assert settings == {"enableAllProjectMcpServers": True, "skillOverrides": {"skill-x": "name-only"}}


def test_a_role_layer_never_loosens_a_skill_the_spawn_settings_restrict(fresh_orchestrator_dir, monkeypatch, tmp_path):
    role_dir = _install_role(tmp_path, monkeypatch)
    (role_dir / "role.settings.json").write_text(json.dumps(
        {"skillOverrides": {"skill-x": "name-only", "skill-y": "name-only", "skill-z": "name-only"}}))
    captured = _patch_spawn_worker_tab(monkeypatch)
    base = {"skillOverrides": {"skill-x": "off", "skill-y": "user-invocable-only", "skill-z": "on"}}
    _asyncio.run(spawn_worker_impl(initial_prompt="task", name="w", cwd="/tmp",
                                   extra_args=["--settings", json.dumps(base)]))
    args = captured["extra_args"]
    settings = json.loads(Path(args[args.index("--settings") + 1]).read_text())
    assert settings["skillOverrides"] == {"skill-x": "off", "skill-y": "user-invocable-only", "skill-z": "name-only"}


def _closed_worker(tmp_path, monkeypatch, sid, spawn_extra_args, kind=None):
    _make_transcript(tmp_path, monkeypatch, sid)
    if kind == "subworker":
        state.write_json_atomic(paths.ACTIVE / "parent1.json", {
            "claude_sid": "parent1", "agent": "worker", "name": "alpha", "cwd": "/tmp", "iterm_sid": "ip",
            "pid": os.getpid(), "started_at": 0})
    state.write_json_atomic(paths.CLOSED / f"{sid}.json", {
        "claude_sid": sid, "name": f"w-{sid}", "cwd": "/tmp", "closed_at": 1.0, "runtime": "claude",
        **({"kind": kind, "parent_name": "alpha", "parent_sid": "parent1"} if kind else {})})
    state.write_json_atomic(paths.assignment_path(sid), {
        "claude_sid": sid, "name": f"w-{sid}", "cwd": "/tmp", "runtime": "claude",
        "spawn_extra_args": spawn_extra_args})


@pytest.mark.parametrize("kind,launch", [(None, "worker"), ("subworker", "child")])
def test_a_worker_spawned_before_roles_resumes_in_the_generic_worker_role(
        fresh_orchestrator_dir, monkeypatch, tmp_path, kind, launch):
    _install_worker_agent(tmp_path, monkeypatch)
    role_dir = _install_role(tmp_path, monkeypatch, launch=("worker", "child"))
    preset = tmp_path / "preset.json"
    preset.write_text(json.dumps({"enableAllProjectMcpServers": True}))
    _closed_worker(tmp_path, monkeypatch, "old1", ["--model", "m", "--settings", str(preset)], kind)
    captured = _patch_spawn_registers_active(monkeypatch)
    _asyncio.run(_resume_worker_mcp(effort="xhigh", name="w-old1", _registration_timeout_sec=2.0, _poll_interval=0.01))
    args = captured["extra_args"]
    assert args[:2] == ["--append-system-prompt-file", str(role_dir / f"role.{launch}.md")]
    assert args[args.index("--model") + 1] == "m" and args.count("--settings") == 1
    settings = json.loads(Path(args[args.index("--settings") + 1]).read_text())
    assert settings == {"enableAllProjectMcpServers": True, "skillOverrides": {"skill-x": "name-only"}}
    assert str(paths.AGENTS_DIR / "worker.md") not in spawner._runtime_command("claude", "", args, resume_sid="old1")
    assert state.read_json(paths.assignment_path("old1"))["context_role"] == "GenericWorker"


def test_a_pre_roles_resume_carries_the_agents_file_of_its_cwd(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _install_worker_agent(tmp_path, monkeypatch)
    _install_role(tmp_path, monkeypatch)
    _install_profiles(tmp_path, monkeypatch, agents=("none",))
    _closed_worker(tmp_path, monkeypatch, "old4", ["--settings", "{}"])
    captured = _patch_spawn_registers_active(monkeypatch)
    _asyncio.run(_resume_worker_mcp(name="w-old4", model="@opus", effort="high", _registration_timeout_sec=2.0, _poll_interval=0.01))
    args = captured["extra_args"]
    assert args[args.index("--agents-file") + 1] == str(paths.ROLES_DIR / "_agents" / "none.json")
    assert args[args.index("--effort") + 1] == "high"


@pytest.mark.parametrize("manifests,spawn_extra_args", [
    (False, ["--settings", "{}"]),
    (True, ["--append-system-prompt", "mine", "--settings", "{}"]),
    (True, ["--append-system-prompt-file", "/r/role.worker.md", "--settings", "/s.json"]),
])
def test_a_resume_replays_spawn_args_that_name_a_prompt_or_predate_no_roles(
        fresh_orchestrator_dir, monkeypatch, tmp_path, manifests, spawn_extra_args):
    if manifests:
        _install_role(tmp_path, monkeypatch)
    else:
        monkeypatch.setattr(paths, "ROLES_DIR", tmp_path / "no-roles")
    _closed_worker(tmp_path, monkeypatch, "old2", spawn_extra_args)
    captured = _patch_spawn_registers_active(monkeypatch)
    _asyncio.run(_resume_worker_mcp(model="@opus", effort="xhigh", name="w-old2", _registration_timeout_sec=2.0, _poll_interval=0.01))
    assert captured["extra_args"] == [*spawn_extra_args, *FLAGS]


def test_a_pre_roles_resume_is_refused_when_the_generic_worker_render_is_missing(
        fresh_orchestrator_dir, monkeypatch, tmp_path):
    role_dir = _install_role(tmp_path, monkeypatch)
    (role_dir / "role.worker.md").unlink()
    _closed_worker(tmp_path, monkeypatch, "old3", ["--settings", "{}"])
    captured = _patch_spawn_registers_active(monkeypatch)
    with pytest.raises(ValueError, match="resume_worker: .*run `dockwright compose`"):
        _asyncio.run(_resume_worker_mcp(name="w-old3", _registration_timeout_sec=2.0, _poll_interval=0.01))
    assert captured == {}


def test_spawn_worker_tier_is_refused_with_the_roster_gone(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="tier is gone: pass --model and --effort in extra_args"):
        _asyncio.run(mcp_server.spawn_worker("do it", tier="REVIEW_TIER2",
                                             extra_args=["--model", "@opus", "--effort", "xhigh"]))


def test_spawn_worker_tool_passes_the_context_role_only(fresh_orchestrator_dir, monkeypatch):
    seen = {}

    async def fake_impl(*args, **kwargs):
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(mcp_server, "spawn_worker_impl", fake_impl)
    _asyncio.run(mcp_server.spawn_worker(initial_prompt="x", role="Coder"))
    assert seen["context_role"] == "Coder" and "role" not in seen


def _closed_worker_for_resume(tmp_path, monkeypatch, spawn_extra_args):
    _install_worker_agent(tmp_path, monkeypatch)
    _closed_worker(tmp_path, monkeypatch, "w1", spawn_extra_args)
    return _patch_spawn_registers_active(monkeypatch)


def test_resume_takes_a_missing_model_or_effort_from_the_call_and_refuses_to_override_the_record(
        fresh_orchestrator_dir, monkeypatch, tmp_path):
    captured = _closed_worker_for_resume(tmp_path, monkeypatch, ["--model", "@opus"])
    with pytest.raises(ValueError, match="resume_worker: the record carries no --effort; pass effort="):
        _asyncio.run(mcp_server.resume_worker("w-w1"))
    with pytest.raises(ValueError, match="the record carries its own --model"):
        _asyncio.run(mcp_server.resume_worker("w-w1", model="@sonnet", effort="xhigh"))
    _asyncio.run(_resume_worker_mcp(name="w-w1", effort="xhigh", _registration_timeout_sec=2.0, _poll_interval=0.01))
    args = captured["extra_args"]
    assert args[args.index("--model") + 1] == "@opus" and args[args.index("--effort") + 1] == "xhigh"


def test_an_auto_resume_from_send_manager_to_worker_reports_the_missing_flag(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _closed_worker_for_resume(tmp_path, monkeypatch, ["--model", "@opus"])
    with pytest.raises(ValueError, match="the record carries no --effort"):
        _asyncio.run(mcp_server.send_manager_to_worker("w-w1", "go on", auto_resume=True))


from dockwright.mcp_server import list_closed_workers_impl as _lcw_impl


def _seed_assignment(sid, prompt="long task " * 40):
    paths.ASSIGNMENTS.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.ASSIGNMENTS / f"{sid}.json",
                            {"claude_sid": sid, "initial_prompt": prompt})


def test_list_workers_surfaces_brief(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    register_self_impl(claude_sid="w2", agent="worker", name="beta", cwd="/x", iterm_sid="i2")
    _seed_assignment("w1", "fix the DLQ handler in the billing service")
    (a, b) = sorted(list_workers_impl(), key=lambda w: w["name"])
    assert a["brief"] == "fix the DLQ handler in the billing service"
    assert b["brief"] is None


def test_list_workers_brief_truncated_to_200(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    _seed_assignment("w1", "z" * 500)
    (w,) = list_workers_impl()
    assert len(w["brief"]) == 200


def test_list_closed_workers_surfaces_brief(fresh_orchestrator_dir):
    state.write_json_atomic(paths.CLOSED / "w9.json",
                            {"claude_sid": "w9", "name": "old", "closed_at": 5.0})
    _seed_assignment("w9", "the original ask")
    (r,) = _lcw_impl()
    assert r["brief"] == "the original ask"


def test_list_closed_workers_tolerates_missing_sid(fresh_orchestrator_dir):
    state.write_json_atomic(paths.CLOSED / "junk.json", {"name": "legacy", "closed_at": 1.0})
    (r,) = _lcw_impl()
    assert r["brief"] is None


def test_pipeline_status_lists_assignment_with_no_artifacts(fresh_orchestrator_dir):
    paths.ASSIGNMENTS.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.ASSIGNMENTS / "w1.json", {
        "claude_sid": "w1", "name": "tkt-sandbox-1-impl", "ticket": "TKT-SANDBOX-1",
        "initial_prompt": "implement the thing", "branch": "TKT-SANDBOX-1-impl",
    })
    state.write_json_atomic(paths.ASSIGNMENTS / "w2.json", {
        "claude_sid": "w2", "name": "other", "ticket": "TKT-SANDBOX-2",
        "initial_prompt": "unrelated",
    })
    out = pipeline_status_impl("TKT-SANDBOX-1")
    assert "tkt-sandbox-1-impl" in out and "implement the thing" in out
    assert "unrelated" not in out


def test_list_workers_and_pipeline_status_carry_both_roles(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    state.write_json_atomic(paths.ASSIGNMENTS / "w1.json", {
        "claude_sid": "w1", "name": "alpha", "ticket": "K-1", "initial_prompt": "do",
        "role": "IMPL_HARD", "context_role": "Coder"})
    (w,) = list_workers_impl()
    assert (w["role"], w["context_role"]) == ("IMPL_HARD", "Coder")
    assert "role=Coder/IMPL_HARD" in pipeline_status_impl("K-1")
    (row,) = _json.loads(pipeline_status_impl("K-1", "json"))["assignments"]
    assert (row["role"], row["context_role"]) == ("IMPL_HARD", "Coder")


def test_pipeline_status_prints_dashes_for_an_assignment_with_no_roles(fresh_orchestrator_dir):
    state.write_json_atomic(paths.ASSIGNMENTS / "w1.json", {
        "claude_sid": "w1", "name": "alpha", "ticket": "K-1", "initial_prompt": "do"})
    assert "role=-/-" in pipeline_status_impl("K-1")


def test_pipeline_status_shows_each_sessions_model(fresh_orchestrator_dir):
    for sid, name, extra in (
            ("w1", "alpha", {"spawn_extra_args": ["--append-system-prompt-file", "/r.md", "--model", "@fable",
                                                  "--effort", "medium"]}),
            ("w2", "beta", {"model": "claude-opus-5-5", "spawn_extra_args": []}),
            ("w3", "gamma", {"spawn_extra_args": ["--model=@sonnet"]}),
            ("w4", "delta", {})):
        state.write_json_atomic(paths.ASSIGNMENTS / f"{sid}.json", {
            "claude_sid": sid, "name": name, "ticket": "K-1", "initial_prompt": "do", **extra})
    board = pipeline_status_impl("K-1").split("## assignments")[1].split("## events")[0]
    rows = {line.split()[1]: line for line in board.splitlines() if line.startswith("- ")}
    assert "  model=@fable  " in rows["alpha"] and "  model=claude-opus-5-5  " in rows["beta"]
    assert "  model=@sonnet  " in rows["gamma"] and "  model=-  " in rows["delta"]
    models = {a["name"]: a["model"] for a in _json.loads(pipeline_status_impl("K-1", "json"))["assignments"]}
    assert models == {"alpha": "@fable", "beta": "claude-opus-5-5", "gamma": "@sonnet", "delta": None}


from dockwright.mcp_server import _spawn_and_confirm_resume


def test_codex_lane_confirm_migrates_assignment(fresh_orchestrator_dir):
    state.write_json_atomic(paths.CLOSED / "old-sid.json", {
        "claude_sid": "old-sid", "name": "cx", "cwd": "/x", "runtime": "codex", "closed_at": 1.0})
    _seed_assignment("old-sid", "codex task")
    closed_path = paths.CLOSED / "old-sid.json"

    async def fake_spawn(**kwargs):
        register_self_impl(claude_sid="new-sid", agent="worker", name="cx", cwd="/x", iterm_sid="i7")
        return ("win-7", "cx")

    result = _asyncio.run(_spawn_and_confirm_resume(
        fake_spawn, closed_path, state.read_json(closed_path), "cx", "old-sid", "/x", 5.0, 0.05))
    assert result["ok"] is True and result["sid"] == "new-sid"
    assert not (paths.ASSIGNMENTS / "old-sid.json").exists()
    migrated = state.read_json(paths.ASSIGNMENTS / "new-sid.json")
    assert migrated["claude_sid"] == "new-sid"
    assert migrated["initial_prompt"] == "codex task"


def test_resume_spawn_passes_no_assignment_env(fresh_orchestrator_dir):
    state.write_json_atomic(paths.CLOSED / "old-sid.json", {
        "claude_sid": "old-sid", "name": "w-res", "cwd": "/x", "runtime": "claude",
        "closed_at": 1.0, "parent_manager_name": "boss"})
    captured = {}

    async def fake_spawn(**kwargs):
        captured.update(kwargs)
        register_self_impl(claude_sid="old-sid", agent="worker", name="w-res", cwd="/x", iterm_sid="i7")
        return ("win-7", "w-res")

    _asyncio.run(_spawn_and_confirm_resume(
        fake_spawn, paths.CLOSED / "old-sid.json", state.read_json(paths.CLOSED / "old-sid.json"),
        "w-res", "old-sid", "/x", 5.0, 0.05, model="@opus", effort="xhigh"))
    assert "CLAUDE_ASSIGNMENT_ID" not in (captured.get("env") or {})


from dockwright.mcp_server import _prune_stale_assignments


def test_prune_assignments_keeps_active_sid(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    _seed_assignment("w1")
    _age(paths.ASSIGNMENTS / "w1.json", 31)
    _prune_stale_assignments()
    assert (paths.ASSIGNMENTS / "w1.json").exists()


def test_prune_assignments_keeps_crash_orphan_within_retention(fresh_orchestrator_dir):
    _seed_assignment("w-crashed")
    _prune_stale_assignments()
    assert (paths.ASSIGNMENTS / "w-crashed.json").exists()


def test_prune_assignments_removes_stale(fresh_orchestrator_dir):
    _seed_assignment("w-old")
    _age(paths.ASSIGNMENTS / "w-old.json", 31)
    _prune_stale_assignments()
    assert not (paths.ASSIGNMENTS / "w-old.json").exists()


def test_prune_pending_sweeps_orphans(fresh_orchestrator_dir):
    paths.ASSIGNMENTS_PENDING.mkdir(parents=True, exist_ok=True)
    stale = paths.ASSIGNMENTS_PENDING / "aid-old.json"
    fresh_p = paths.ASSIGNMENTS_PENDING / "aid-new.json"
    stale.write_text("{}")
    fresh_p.write_text("{}")
    _age(stale, 2)
    _prune_stale_assignments()
    assert not stale.exists() and fresh_p.exists()


def test_prune_sweeps_prompt_files_past_the_assignment_retention(fresh_orchestrator_dir):
    paths.PROMPT_FILES.mkdir(parents=True, exist_ok=True)
    stale = paths.PROMPT_FILES / "old.txt"
    fresh_p = paths.PROMPT_FILES / "new.txt"
    stale.write_text("task")
    fresh_p.write_text("task")
    _age(stale, paths.ASSIGNMENT_RETENTION_DAYS + 1)
    _age(fresh_p, paths.ASSIGNMENT_RETENTION_DAYS - 1)
    _prune_stale_assignments()
    assert not stale.exists() and fresh_p.exists()


def test_prune_pending_sweeps_window_sidecar_orphans(fresh_orchestrator_dir):
    paths.ASSIGNMENTS_PENDING.mkdir(parents=True, exist_ok=True)
    stale = paths.pending_window_path("aid-old")
    fresh_p = paths.pending_window_path("aid-new")
    stale.write_text("777")
    fresh_p.write_text("888")
    _age(stale, 2)
    _prune_stale_assignments()
    assert not stale.exists() and fresh_p.exists()


def test_spawn_path_sweeps_expired_pending_litter(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    _patch_spawn_worker_tab(monkeypatch)
    paths.ASSIGNMENTS_PENDING.mkdir(parents=True, exist_ok=True)
    stale_json = paths.pending_assignment_path("aid-dead-spawn")
    stale_window = paths.pending_window_path("aid-dead-spawn")
    stale_json.write_text("{}")
    stale_window.write_text("777")
    _age(stale_json, 2)
    _age(stale_window, 2)
    result = _asyncio.run(spawn_worker_impl(
        initial_prompt="task", name="sweeper", cwd="/tmp/x",
        _registration_timeout_sec=0.2, _poll_interval=0.01))
    assert not stale_json.exists() and not stale_window.exists()
    assert paths.pending_assignment_path(result["assignment_id"]).exists()


def test_folds_tolerate_corrupted_stamp_lines(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "spec", "srs", "the body", "complete", "sid-1")
    p = paths.artifact_path("TKT-SANDBOX-1", "spec", "srs")
    corrupted = p.read_text().replace('status: "complete"', "status: {broken")
    p.write_text(corrupted)
    status_out = pipeline_status_impl("TKT-SANDBOX-1")
    assert "spec.srs" in status_out
    view_out = artifact_view_impl("TKT-SANDBOX-1")
    assert "the body" in view_out


def test_prune_artifacts_tolerates_vanishing_entries(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "spec", "srs", "x", "complete", "s1")
    (paths.artifact_ticket_dir("TKT-SANDBOX-1") / "dangling").symlink_to(
        fresh_orchestrator_dir / "nope-does-not-exist")
    _prune_stale_artifacts()
    assert paths.artifact_ticket_dir("TKT-SANDBOX-1").exists()


from dockwright import pipeline
from dockwright.mcp_server import _write_question, _open_token_task_keys

_EXAMPLE_SCHEMA = """
schema = "code-ticket"
version = 1

[nodes.design]
role = "SCOUT_DESIGN"
gate = true
brief = "briefs/design.md"
inputs = []
outputs = ["context.ask", "design.hld", "plan.tasks"]
checkpoints = ["design.explainer"]
extra_args = []

[nodes.task]
role = "IMPL_HARD"
repeat = "plan.tasks"
brief = "briefs/task.md"
inputs = ["plan.tasks", "design.hld"]
outputs = ["review.task-{i}"]

[nodes.pr]
role = "IMPL_HARD"
gate = true
brief = "briefs/pr.md"
inputs = ["plan.tasks"]
outputs = ["implement.repo"]

[nodes.post-merge]
role = "SCOUT"
brief = "briefs/post-merge.md"
inputs = ["implement.repo"]
outputs = ["summary.post-merge"]

[[edges]]
from = "design"
to = "task"
exit_checks = [{ type = "artifact", name = "plan.tasks" }]
[[edges]]
from = "task"
to = "task"
[[edges]]
from = "task"
to = "pr"
when = "last"
[[edges]]
from = "pr"
to = "post-merge"
trigger = "merge"
"""


def _write_example_schema(tmp_path, schema_text=_EXAMPLE_SCHEMA):
    d = tmp_path / "schemas"
    (d / "briefs").mkdir(parents=True, exist_ok=True)
    (d / "code-ticket.toml").write_text(schema_text)
    for n in ("design", "task", "pr", "post-merge"):
        (d / "briefs" / f"{n}.md").write_text(f"# {n} brief for {{task_key}} at {{cwd}} inputs {{inputs}}\n")
    return d / "code-ticket.toml"


def _seed_snapshot(task_key, tmp_path, holder="a-design", schema_text=_EXAMPLE_SCHEMA):
    text = pipeline.snapshot(pipeline.load_schema(_write_example_schema(tmp_path, schema_text)))
    d = paths.artifact_ticket_dir(task_key); d.mkdir(parents=True, exist_ok=True)
    (d / "schema.toml").write_text(text)
    state.append_event(paths.artifact_events_path(task_key),
                       {"type": "pipeline_started", "token_id": "t1", "node": "design", "index": 1, "holder": holder,
                        "cwd": "/w", "manager": "mgr", "ts": time.time() - PAST_GRACE})


PAST_GRACE = 2 * waiting_on.STALL_GRACE_SEC


def _commit(task_key, node, index, holder, ts=None):
    state.append_event(paths.artifact_events_path(task_key),
                       {"type": "advance_committed", "request_id": f"r-{node}-{index}", "token_id": "t1",
                        "node": node, "index": index, "holder": holder, "ts": ts or time.time() - PAST_GRACE})


def _append_malformed_commit(task_key):
    state.append_event(paths.artifact_events_path(task_key),
                       {"type": "advance_committed", "request_id": "r-bad", "token_id": "t1",
                        "node": "task", "holder": "a-task"})


def test_artifact_put_scope_default_internal_and_validated(fresh_orchestrator_dir):
    artifact_put_impl("K1", "spec", "x", "b", "complete", "w1")
    assert artifact_get_impl("K1", "spec", "x")["scope"] == "internal"
    artifact_put_impl("K1", "design", "hld", "b", "complete", "w1", scope="output")
    assert artifact_get_impl("K1", "design", "hld")["scope"] == "output"
    with pytest.raises(ValueError):
        artifact_put_impl("K1", "spec", "y", "b", "complete", "w1", scope="public")


def test_artifact_get_with_reader_sid_appends_a_read_event(fresh_orchestrator_dir):
    artifact_put_impl("K2", "design", "hld", "b", "complete", "w1", scope="output")
    artifact_get_impl("K2", "design", "hld", reader_sid="r1")
    events = _read_events(paths.artifact_events_path("K2"))
    assert [e for e in events if e["type"] == "artifact_read"][0]["actor_sid"] == "r1"
    artifact_get_impl("K2", "design", "hld")
    assert len([e for e in _read_events(paths.artifact_events_path("K2")) if e["type"] == "artifact_read"]) == 1


def test_pipeline_event_refuses_reserved_types(fresh_orchestrator_dir):
    with pytest.raises(ValueError):
        pipeline_event_impl("K3", "advance_committed", reason="forged")
    assert pipeline_event_impl("K3", "note", reason="fine")["ok"]
    assert [e["type"] for e in _read_events(paths.artifact_events_path("K3"))] == ["note"]


def test_pipeline_event_refuses_a_caller_written_lane_event(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="reserved"):
        pipeline_event_impl("K3", "lane_event", reason="forged")
    assert _read_events(paths.artifact_events_path("K3")) == []


def test_artifact_tools_pass_scope_and_reader_sid_and_refuse_reserved_events(fresh_orchestrator_dir):
    _tool_artifact_put(task_key="K-T", phase="design", name="hld",
                       content_path=_source(fresh_orchestrator_dir, "b", "hld.md"),
                       status="complete", writer_sid="w1", scope="output")
    got = _tool_artifact_get(task_key="K-T", phase="design", name="hld", reader_sid="r9")
    assert got["scope"] == "output"
    reads = [e for e in _read_events(paths.artifact_events_path("K-T")) if e["type"] == "artifact_read"]
    assert [(e["phase"], e["name"], e["actor_sid"]) for e in reads] == [("design", "hld", "r9")]
    with pytest.raises(ValueError, match="reserved"):
        _tool_pipeline_event(task_key="K-T", type="artifact_read", phase="design", name="hld")


def test_answer_question_writes_a_decision_artifact_for_keyed_workers(fresh_orchestrator_dir):
    state.write_json_atomic(paths.assignment_path("sid-w"), {"assignment_id": "a1", "claude_sid": "sid-w", "ticket": "K4", "name": "w"})
    qid = _write_question("sid-w", "w", "Which colour?", parent_manager_name="mgr")
    answer_question_impl(qid, "blue")
    d = artifact_get_impl("K4", "decision", f"q-{qid[:8]}")
    assert d["scope"] == "output" and d["status"] == "complete" and d["writer_sid"] == f"answer:{qid}"
    assert "Which colour?" in d["content"] and "blue" in d["content"]


def test_answer_question_without_ticket_writes_no_decision(fresh_orchestrator_dir):
    state.write_json_atomic(paths.assignment_path("sid-u"), {"assignment_id": "a2", "claude_sid": "sid-u", "name": "u"})
    qid = _write_question("sid-u", "u", "q?", parent_manager_name="mgr")
    assert answer_question_impl(qid, "a")["ok"]
    assert not list(paths.ARTIFACTS.glob("*/decision.*"))


def test_decision_artifact_names_the_node_the_asker_holds(fresh_orchestrator_dir, tmp_path):
    _seed_snapshot("K4N", tmp_path)
    state.write_json_atomic(paths.assignment_path("sid-d"), {"assignment_id": "a-design", "claude_sid": "sid-d", "ticket": "K4N", "name": "d"})
    state.write_json_atomic(paths.assignment_path("sid-x"), {"assignment_id": "a-x", "claude_sid": "sid-x", "ticket": "K4X", "name": "x"})
    q_held = _write_question("sid-d", "d", "Scope?", parent_manager_name="mgr")
    q_free = _write_question("sid-x", "x", "Name?", parent_manager_name="mgr")
    answer_question_impl(q_held, "narrow")
    answer_question_impl(q_free, "short")
    held = artifact_get_impl("K4N", "decision", f"q-{q_held[:8]}")["content"].splitlines()
    free = artifact_get_impl("K4X", "decision", f"q-{q_free[:8]}")["content"].splitlines()
    assert "node: design" in held
    assert "node: -" in free


def test_answer_question_records_the_manager_as_decider_by_default(fresh_orchestrator_dir):
    state.write_json_atomic(paths.assignment_path("sid-m"), {"assignment_id": "a-m", "claude_sid": "sid-m", "ticket": "KDM", "name": "m"})
    qid = _write_question("sid-m", "m", "Ship?", parent_manager_name="mgr")
    answer_question_impl(qid, "yes")
    d = artifact_get_impl("KDM", "decision", f"q-{qid[:8]}")
    assert d["decided_by"] == "manager"
    assert d["content"].splitlines()[-1] == "decided_by: manager"


def test_answer_question_records_the_engineer_when_told(fresh_orchestrator_dir):
    state.write_json_atomic(paths.assignment_path("sid-e"), {"assignment_id": "a-e", "claude_sid": "sid-e", "ticket": "KDE", "name": "e"})
    qid = _write_question("sid-e", "e", "Ship?", parent_manager_name="mgr")
    answer_question_impl(qid, "yes", decided_by="engineer")
    d = artifact_get_impl("KDE", "decision", f"q-{qid[:8]}")
    assert d["decided_by"] == "engineer"
    assert d["content"].splitlines()[-1] == "decided_by: engineer"


def test_answer_question_refuses_an_unknown_decider_before_touching_anything(fresh_orchestrator_dir):
    from dockwright.mcp_server import _find_question_path
    state.write_json_atomic(paths.assignment_path("sid-b"), {"assignment_id": "a-b", "claude_sid": "sid-b", "ticket": "KDB", "name": "b"})
    qid = _write_question("sid-b", "b", "Ship?", parent_manager_name="mgr")
    with pytest.raises(ValueError, match="decided_by"):
        answer_question_impl(qid, "yes", decided_by="boss")
    q_path = _find_question_path(qid)
    assert q_path is not None and q_path.exists()
    assert not (paths.ANSWERS / f"{qid}.json").exists()
    assert not list(paths.ARTIFACTS.glob("*/decision.*"))


def test_artifact_put_tool_stamps_decided_by_only_when_passed(fresh_orchestrator_dir):
    _tool_artifact_put(task_key="KDP", phase="decision", name="pane-20260929T120000",
                       content_path=_source(fresh_orchestrator_dir, "from: engineer\n", "p1.md"),
                       status="complete", writer_sid="sid-n", scope="output", decided_by="engineer")
    assert artifact_get_impl("KDP", "decision", "pane-20260929T120000")["decided_by"] == "engineer"
    _tool_artifact_put(task_key="KDP", phase="decision", name="pane-20260929T130000",
                       content_path=_source(fresh_orchestrator_dir, "from: engineer\n", "p2.md"),
                       status="complete", writer_sid="sid-n", scope="output")
    assert "decided_by" not in artifact_get_impl("KDP", "decision", "pane-20260929T130000")
    assert "decided_by" not in paths.artifact_path("KDP", "decision", "pane-20260929T130000").read_text()


def test_artifact_put_refuses_an_unknown_decider_and_a_decider_off_a_decision_record(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="decided_by"):
        _tool_artifact_put(task_key="KDR", phase="decision", name="pane-x",
                           content_path=_source(fresh_orchestrator_dir, "b", "x.md"),
                           status="complete", writer_sid="sid-n", scope="output", decided_by="boss")
    with pytest.raises(ValueError, match="decided_by"):
        artifact_put_impl("KDR", "design", "hld", "b", "complete", "sid-n", scope="output", decided_by="manager")
    assert not paths.artifact_path("KDR", "decision", "pane-x").exists()
    assert not paths.artifact_path("KDR", "design", "hld").exists()
    assert _read_events(paths.artifact_events_path("KDR")) == []


def test_pipeline_status_prints_who_decided_on_decision_rows_only(fresh_orchestrator_dir):
    artifact_put_impl("KDS", "context", "ask", "b", "complete", "sid-m", scope="output")
    artifact_put_impl("KDS", "decision", "pane-a", "b", "complete", "sid-n", scope="output", decided_by="engineer")
    artifact_put_impl("KDS", "decision", "q-old", "b", "complete", "answer:1", scope="output")

    def rows():
        lines = pipeline_status_impl("KDS").splitlines()
        return {full: next(line for line in lines if line.startswith(f"- {full} "))
                for full in ("context.ask", "decision.pane-a", "decision.q-old")}

    now = rows()
    assert now["decision.pane-a"].endswith(" input(s)  decided_by=engineer")
    assert now["decision.q-old"].endswith(" input(s)  decided_by=?")
    assert now["context.ask"].endswith(" input(s)")
    state.append_event(paths.artifact_events_path("KDS"), {"type": "round_started", "ts": 10 ** 12})
    carried = rows()
    assert carried["decision.pane-a"].endswith(" input(s)  decided_by=engineer  carried")
    assert carried["context.ask"].endswith(" input(s)  carried")


def test_scope_warnings_reach_any_writer_of_a_declared_output(fresh_orchestrator_dir, tmp_path):
    _seed_snapshot("KW0", tmp_path)
    state.append_event(paths.artifact_events_path("KW0"),
                       {"type": "token_override", "token_id": "t1", "to": "design", "index": 1})
    res = artifact_put_impl("KW0", "design", "hld", "b", "complete", "sid-none", scope="internal")
    assert res["ok"] and len(res["warnings"]) == 1 and "design.hld" in res["warnings"][0]
    assert "warnings" not in artifact_put_impl("KW0", "design", "notes", "b", "complete", "sid-none", scope="internal")


def test_scope_warnings_name_the_holders_declared_output_put_with_a_non_output_scope(fresh_orchestrator_dir, tmp_path):
    _seed_snapshot("KW1", tmp_path)
    _commit("KW1", "task", 2, "a-task")
    state.write_json_atomic(paths.assignment_path("sid-t"), {"assignment_id": "a-task", "claude_sid": "sid-t", "ticket": "KW1", "name": "t"})
    state.write_json_atomic(paths.assignment_path("sid-o"), {"assignment_id": "a-other", "claude_sid": "sid-o", "ticket": "KW1", "name": "o"})
    res = artifact_put_impl("KW1", "review", "task-2", "b", "complete", "sid-t", scope="internal")
    assert len(res["warnings"]) == 1 and "review.task-2" in res["warnings"][0]
    assert "warnings" not in artifact_put_impl("KW1", "review", "task-2", "b", "complete", "sid-t", scope="output")
    assert "warnings" not in artifact_put_impl("KW1", "review", "task-1", "b", "complete", "sid-t", scope="internal")
    assert "warnings" not in artifact_put_impl("KW1", "review", "task-2x", "b", "complete", "sid-o", scope="checkpoint")
    res = artifact_put_impl("KW1", "review", "task-2", "b", "complete", "sid-o", scope="internal", overwrite=True)
    assert len(res["warnings"]) == 1 and "review.task-2" in res["warnings"][0]


def test_artifact_put_survives_a_malformed_fold(fresh_orchestrator_dir, tmp_path):
    _seed_snapshot("KM1", tmp_path)
    _append_malformed_commit("KM1")
    state.write_json_atomic(paths.assignment_path("sid-d"), {"assignment_id": "a-design", "claude_sid": "sid-d", "ticket": "KM1", "name": "d"})
    res = artifact_put_impl("KM1", "design", "hld", "b", "complete", "sid-d", scope="internal")
    assert res["ok"] and "warnings" not in res
    assert artifact_get_impl("KM1", "design", "hld")["content"] == "b"


def test_prune_treats_a_work_whose_fold_fails_as_open(fresh_orchestrator_dir, tmp_path):
    _seed_snapshot("KM2", tmp_path)
    _append_malformed_commit("KM2")
    old = time.time() - 40 * 86400
    for p in paths.artifact_ticket_dir("KM2").rglob("*"):
        os.utime(p, (old, old))
    state.write_json_atomic(paths.assignment_path("sid-m"), {"assignment_id": "a-task", "claude_sid": "sid-m", "ticket": "KM2", "name": "n"})
    os.utime(paths.assignment_path("sid-m"), (old, old))
    _prune_stale_artifacts(); _prune_stale_assignments()
    assert paths.artifact_ticket_dir("KM2").is_dir() and paths.assignment_path("sid-m").exists()


def test_pipeline_status_reports_a_failed_fold_and_renders_the_rest(fresh_orchestrator_dir, tmp_path):
    _seed_snapshot("KM3", tmp_path)
    _append_malformed_commit("KM3")
    state.write_json_atomic(paths.assignment_path("sid-d"), {"assignment_id": "a-design", "claude_sid": "sid-d", "ticket": "KM3", "name": "KM3-design", "spawned_at": 1.0})
    artifact_put_impl("KM3", "summary", "other", "b", "complete", "someone", scope="output")
    artifact_get_impl("KM3", "summary", "other", reader_sid="sid-d")
    out = pipeline_status_impl("KM3")
    token = out.split("## token", 1)[1].split("## reads", 1)[0]
    assert "- fold failed:" in token and "node=" not in token
    assert "- KM3-design (design) read summary.other  OUTSIDE INPUTS" in out
    assert "## events" in out and "artifact_read" in out


def test_pipeline_status_flags_reads_outside_inputs(fresh_orchestrator_dir, tmp_path):
    _seed_snapshot("K5", tmp_path)
    state.write_json_atomic(paths.assignment_path("sid-d"), {"assignment_id": "a-design", "claude_sid": "sid-d", "ticket": "K5", "name": "K5-design", "spawned_at": 1.0})
    artifact_put_impl("K5", "summary", "other", "b", "complete", "someone", scope="output")
    artifact_get_impl("K5", "summary", "other", reader_sid="sid-d")
    out = pipeline_status_impl("K5")
    assert "## token" in out and "node=design" in out and "holder=a-design" in out
    assert "summary.other  OUTSIDE INPUTS" in out


def test_pipeline_status_reads_cover_read_sets_foreign_keys_strangers_and_unattributed(fresh_orchestrator_dir, tmp_path):
    _seed_snapshot("K7", tmp_path)
    _commit("K7", "task", 1, "a-task")
    state.write_json_atomic(paths.assignment_path("sid-t"), {"assignment_id": "a-task", "claude_sid": "sid-t", "ticket": "K7", "name": "K7-task", "spawned_at": 1.0})
    for name in ("hld", "other"):
        artifact_put_impl("K7", "design", name, "b", "complete", "w-design", scope="output")
    artifact_put_impl("K7", "decision", "q-1", "b", "complete", "answer:1", scope="output")
    artifact_put_impl("K7", "review", "task-1", "b", "complete", "sid-t", scope="output",
                      read_set=["junk", {"name": "plan.tasks"}, {"name": "design.hld", "task_key": "OTHER"},
                                {"name": "design.hld", "task_key": "K7"}])
    artifact_get_impl("K7", "decision", "q-1", reader_sid="sid-t")
    artifact_get_impl("K7", "design", "other", reader_sid="sid-t")
    artifact_get_impl("K7", "design", "hld", reader_sid="stranger-sid")
    artifact_get_impl("K7", "decision", "q-1", reader_sid="stranger-sid")
    state.append_event(paths.artifact_events_path("K7"), {"type": "artifact_read", "phase": "design", "name": "hld"})
    out = pipeline_status_impl("K7")
    reads = out.split("## reads\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert reads == [
        "- K7-task (task) read OTHER:design.hld  OUTSIDE INPUTS",
        "- K7-task (task) read decision.q-1",
        "- K7-task (task) read design.hld",
        "- K7-task (task) read design.other  OUTSIDE INPUTS",
        "- K7-task (task) read plan.tasks",
        "- sid=stranger (?) read decision.q-1",
        "- sid=stranger (?) read design.hld  OUTSIDE INPUTS",
        "- unattributed read design.hld",
    ]


def test_prune_skips_works_with_an_open_token(fresh_orchestrator_dir, tmp_path):
    _seed_snapshot("K6", tmp_path)
    old = time.time() - 40 * 86400
    for p in paths.artifact_ticket_dir("K6").rglob("*"):
        os.utime(p, (old, old))
    state.write_json_atomic(paths.assignment_path("sid-old"), {"assignment_id": "a-design", "claude_sid": "sid-old", "ticket": "K6", "name": "n"})
    os.utime(paths.assignment_path("sid-old"), (old, old))
    _prune_stale_artifacts(); _prune_stale_assignments()
    assert paths.artifact_ticket_dir("K6").is_dir() and paths.assignment_path("sid-old").exists()


def test_prune_removes_works_whose_token_sits_on_a_terminal_node(fresh_orchestrator_dir, tmp_path):
    _seed_snapshot("K8", tmp_path)
    _commit("K8", "post-merge", 1, "a-pm")
    old = time.time() - 40 * 86400
    for p in paths.artifact_ticket_dir("K8").rglob("*"):
        os.utime(p, (old, old))
    state.write_json_atomic(paths.assignment_path("sid-pm"), {"assignment_id": "a-pm", "claude_sid": "sid-pm", "ticket": "K8", "name": "n"})
    os.utime(paths.assignment_path("sid-pm"), (old, old))
    _prune_stale_artifacts(); _prune_stale_assignments()
    assert not paths.artifact_ticket_dir("K8").exists() and not paths.assignment_path("sid-pm").exists()


def test_prune_removes_a_closed_work(fresh_orchestrator_dir, tmp_path):
    _seed_snapshot("K9", tmp_path)
    ms._append_task_event("K9", {"type": "work_closed", "token_id": "t1", "node": "design", "reason": "never",
                                 "actor_sid": "m1"})
    old = time.time() - 40 * 86400
    for p in paths.artifact_ticket_dir("K9").rglob("*"):
        os.utime(p, (old, old))
    state.write_json_atomic(paths.assignment_path("sid-cl"), {"assignment_id": "a-design", "claude_sid": "sid-cl", "ticket": "K9", "name": "n"})
    os.utime(paths.assignment_path("sid-cl"), (old, old))
    _prune_stale_artifacts(); _prune_stale_assignments()
    assert not paths.artifact_ticket_dir("K9").exists() and not paths.assignment_path("sid-cl").exists()


def test_open_token_task_keys_lists_works_whose_token_can_still_move(fresh_orchestrator_dir, tmp_path):
    _seed_snapshot("KO-open", tmp_path)
    _seed_snapshot("KO-done", tmp_path)
    _commit("KO-done", "post-merge", 1, "a-pm")
    _seed_snapshot("KO-bad", tmp_path)
    _append_malformed_commit("KO-bad")
    artifact_put_impl("KO-plain", "spec", "a", "x", "complete", "s1")
    assert _open_token_task_keys() == {"KO-open", "KO-bad"}


def test_non_object_journal_lines_break_no_reader(fresh_orchestrator_dir, tmp_path):
    _seed_snapshot("KJ", tmp_path)
    with open(paths.artifact_events_path("KJ"), "a") as f:
        f.write("[1]\nnull\n")
    state.write_json_atomic(paths.assignment_path("sid-d"), {"assignment_id": "a-design", "claude_sid": "sid-d", "ticket": "KJ", "name": "KJ-design", "spawned_at": 1.0})
    res = artifact_put_impl("KJ", "design", "hld", "b", "complete", "sid-d", scope="internal")
    assert res["ok"] and len(res["warnings"]) == 1 and "design.hld" in res["warnings"][0]
    qid = _write_question("sid-d", "KJ-design", "Which?", parent_manager_name="mgr")
    assert answer_question_impl(qid, "this")["ok"]
    assert "node: design" in artifact_get_impl("KJ", "decision", f"q-{qid[:8]}")["content"].splitlines()
    old = time.time() - 40 * 86400
    for p in paths.artifact_ticket_dir("KJ").rglob("*"):
        os.utime(p, (old, old))
    os.utime(paths.assignment_path("sid-d"), (old, old))
    assert artifact_list_impl("OTHER") == []
    _prune_stale_assignments()
    assert paths.artifact_ticket_dir("KJ").is_dir() and paths.assignment_path("sid-d").exists()
    out = pipeline_status_impl("KJ")
    assert "- t1: node=design index=1 holder=a-design" in out and "## reads" in out and "decision.q-" in out


def _reads_section(out):
    return out.split("## reads\n", 1)[1].split("\n\n", 1)[0].splitlines()


def test_handed_names_split_into_events_that_fit_the_journal_line():
    names = [f"decision.q-{i:08d}" for i in range(300)]
    events = ms._handed_events("a1", names)
    assert len(events) > 1
    assert [n for e in events for n in e["names"]] == names
    assert all(e["assignment_id"] == "a1" and e["type"] == "inputs_handed" for e in events)
    assert all(len(state.event_line(ms._fit_task_event(e))) <= state.EVENT_LINE_MAX for e in events)
    assert ms._handed_events("a1", ["context.ask"]) == [
        {"type": "inputs_handed", "assignment_id": "a1", "names": ["context.ask"]}]


def test_reads_attribute_handed_inputs_to_the_holder_by_assignment(fresh_orchestrator_dir, tmp_path):
    _seed_snapshot("KH", tmp_path)
    _commit("KH", "design", 1, "a-design")
    state.write_json_atomic(paths.assignment_path("sid-d"), {"assignment_id": "a-design", "claude_sid": "sid-d",
                                                             "ticket": "KH", "name": "KH-design", "spawned_at": 1.0})
    state.append_event(paths.artifact_events_path("KH"),
                       {"type": "inputs_handed", "assignment_id": "a-design", "names": ["context.ask"]})
    assert _reads_section(pipeline_status_impl("KH")) == ["- KH-design (design) read context.ask"]


def test_handed_inputs_of_a_session_that_never_held_the_token_print_no_read(fresh_orchestrator_dir, tmp_path):
    _seed_snapshot("KH", tmp_path)
    _commit("KH", "design", 1, "a-design")
    state.write_json_atomic(paths.assignment_path("sid-x"), {"assignment_id": "a-superseded", "claude_sid": "sid-x",
                                                             "ticket": "KH", "name": "KH-task-1", "spawned_at": 2.0})
    state.append_event(paths.artifact_events_path("KH"),
                       {"type": "inputs_handed", "assignment_id": "a-superseded", "names": ["design.hld"]})
    assert not any("design.hld" in line for line in _reads_section(pipeline_status_impl("KH")))


def test_handed_inputs_of_an_unknown_assignment_print_no_read(fresh_orchestrator_dir, tmp_path):
    _seed_snapshot("KH", tmp_path)
    _commit("KH", "design", 1, "a-design")
    state.write_json_atomic(paths.assignment_path("sid-d"), {"assignment_id": "a-design", "claude_sid": "sid-d",
                                                             "ticket": "KH", "name": "KH-design", "spawned_at": 1.0})
    state.append_event(paths.artifact_events_path("KH"),
                       {"type": "inputs_handed", "assignment_id": "a-never", "names": ["context.ask"]})
    assert not any("context.ask" in line for line in _reads_section(pipeline_status_impl("KH")))


def test_reads_allow_a_restarted_holders_own_checkpoint(fresh_orchestrator_dir, tmp_path):
    _seed_snapshot("KR", tmp_path)
    _commit("KR", "design", 1, "a-design-r1")
    state.write_json_atomic(paths.assignment_path("sid-r"), {"assignment_id": "a-design-r1", "claude_sid": "sid-r", "ticket": "KR", "name": "KR-design-r1", "spawned_at": 2.0})
    artifact_put_impl("KR", "design", "explainer", "b", "partial", "sid-dead", scope="checkpoint")
    artifact_get_impl("KR", "design", "explainer", reader_sid="sid-r")
    assert _reads_section(pipeline_status_impl("KR")) == ["- KR-design-r1 (design) read design.explainer"]


def test_reads_allow_a_nodes_own_output_at_its_index_and_flag_other_nodes_outputs(fresh_orchestrator_dir, tmp_path):
    _seed_snapshot("KI", tmp_path)
    _commit("KI", "task", 2, "a-task")
    state.write_json_atomic(paths.assignment_path("sid-t"), {"assignment_id": "a-task", "claude_sid": "sid-t", "ticket": "KI", "name": "KI-task-2", "spawned_at": 1.0})
    artifact_put_impl("KI", "review", "task-2", "b", "complete", "sid-t", scope="output")
    artifact_put_impl("KI", "context", "ask", "b", "complete", "mgr", scope="output")
    artifact_get_impl("KI", "review", "task-2", reader_sid="sid-t")
    artifact_get_impl("KI", "context", "ask", reader_sid="sid-t")
    assert _reads_section(pipeline_status_impl("KI")) == [
        "- KI-task-2 (task) read context.ask  OUTSIDE INPUTS",
        "- KI-task-2 (task) read review.task-2",
    ]


def test_reads_never_flag_the_works_running_summary(fresh_orchestrator_dir, tmp_path):
    _seed_snapshot("KS", tmp_path)
    _commit("KS", "task", 2, "a-task")
    state.write_json_atomic(paths.assignment_path("sid-t"), {"assignment_id": "a-task", "claude_sid": "sid-t", "ticket": "KS", "name": "KS-task-2", "spawned_at": 1.0})
    artifact_put_impl("KS", "summary", "thread", "s", "complete", "sid-prev", scope="output")
    artifact_get_impl("KS", "summary", "thread", reader_sid="sid-t")
    assert _reads_section(pipeline_status_impl("KS")) == ["- KS-task-2 (task) read summary.thread"]


def test_reads_judge_a_child_by_its_parents_node(fresh_orchestrator_dir, tmp_path):
    _seed_snapshot("KC", tmp_path)
    _commit("KC", "task", 1, "a-task")
    family = (("sid-t", "a-task", "KC-task-1", "mgr-sid"), ("sid-c", "a-c", "KC-task-1-review", "sid-t"),
              ("sid-g", "a-g", "KC-task-1-review-probe", "sid-c"), ("sid-x", "a-x", "KC-loop-x", "sid-y"),
              ("sid-y", "a-y", "KC-loop-y", "sid-x"))
    for n, (sid, aid, name, parent) in enumerate(family, 1):
        state.write_json_atomic(paths.assignment_path(sid), {"assignment_id": aid, "claude_sid": sid, "ticket": "KC",
                                                              "name": name, "parent_sid": parent, "spawned_at": float(n)})
    artifact_put_impl("KC", "design", "hld", "b", "complete", "w-design", scope="output")
    for sid in ("sid-c", "sid-g", "sid-x"):
        artifact_get_impl("KC", "design", "hld", reader_sid=sid)
    assert _reads_section(pipeline_status_impl("KC")) == [
        "- KC-task-1-review (task) read design.hld",
        "- KC-task-1-review-probe (task) read design.hld",
        "- KC-loop-x (?) read design.hld  OUTSIDE INPUTS",
    ]


def test_answer_question_skips_the_decision_for_a_subworker_asker(fresh_orchestrator_dir):
    for sid, kind in (("sid-sub", "subworker"), ("sid-wk", "worker")):
        state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {"claude_sid": sid, "name": sid, "agent": "worker", "kind": kind})
        state.write_json_atomic(paths.assignment_path(sid), {"assignment_id": f"a-{sid}", "claude_sid": sid, "ticket": "KS", "name": sid})
    q_sub = _write_question("sid-sub", "sid-sub", "Child?", parent_manager_name="mgr")
    q_wk = _write_question("sid-wk", "sid-wk", "Worker?", parent_manager_name="mgr")
    assert answer_question_impl(q_sub, "a")["ok"] and answer_question_impl(q_wk, "b")["ok"]
    assert state.read_json(paths.ANSWERS / f"{q_sub}.json")["answer"] == "a"
    assert sorted(p.name for p in paths.artifact_ticket_dir("KS").glob("decision.*")) == [f"decision.q-{q_wk[:8]}.md"]


def test_prune_deletes_a_stale_assignment_with_a_blank_ticket(fresh_orchestrator_dir):
    state.write_json_atomic(paths.assignment_path("sid-blank"), {"assignment_id": "a-b", "claude_sid": "sid-blank", "ticket": " ", "name": "b"})
    _age(paths.assignment_path("sid-blank"), 31)
    _prune_stale_assignments()
    assert not paths.assignment_path("sid-blank").exists()


def test_answer_question_survives_a_non_string_holder_in_the_journal(fresh_orchestrator_dir, tmp_path):
    _seed_snapshot("KH", tmp_path)
    state.append_event(paths.artifact_events_path("KH"), {"type": "advance_committed", "request_id": "r-h", "token_id": "t1",
                                                           "node": "task", "index": 1, "holder": ["x"]})
    state.write_json_atomic(paths.assignment_path("sid-d"), {"assignment_id": "a-design", "claude_sid": "sid-d", "ticket": "KH", "name": "d"})
    qid = _write_question("sid-d", "d", "Q?", parent_manager_name="mgr")
    assert answer_question_impl(qid, "a")["ok"]
    assert "node: design" in artifact_get_impl("KH", "decision", f"q-{qid[:8]}")["content"].splitlines()


def test_legacy_artifact_without_scope_reads_as_internal(fresh_orchestrator_dir):
    p = paths.artifact_path("KL", "spec", "old")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('---\nphase: "spec"\nname: "old"\nstatus: "complete"\nwriter_sid: "w"\n---\nlegacy body')
    assert artifact_get_impl("KL", "spec", "old")["scope"] == "internal"
    assert [s["scope"] for s in artifact_list_impl("KL")] == ["internal"]


def test_spawn_value_error_unlinks_pending(fresh_orchestrator_dir, monkeypatch):
    async def raise_value_error(**kwargs):
        raise ValueError("disallowed extra args")
    monkeypatch.setattr(spawner, "spawn_worker_tab", raise_value_error)
    with pytest.raises(ValueError):
        _asyncio.run(spawn_worker_impl(initial_prompt="x", name="w1", cwd="/tmp"))
    assert list(paths.ASSIGNMENTS_PENDING.glob("*.json")) == []


def test_spawn_worker_explicit_task_key_wins_over_derivation(fresh_orchestrator_dir, monkeypatch,
                                                             configured_key_regex):
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="build the bot; related cleanup tracked in TKT-999",
        name="demo-worker", cwd="/tmp", task_key="demo-task"))
    (pending,) = list(paths.ASSIGNMENTS_PENDING.glob("*.json"))
    record = state.read_json(pending)
    assert record["ticket"] == "demo-task"


def test_spawn_never_derives_key_from_prompt_prose(fresh_orchestrator_dir, monkeypatch,
                                                   configured_key_regex):
    captured = _patch_spawn_worker_tab(monkeypatch)
    result = _asyncio.run(spawn_worker_impl(
        initial_prompt="investigate the filing bug; background: TKT-4242 was hit",
        name="scout", cwd="/tmp"))
    (pending,) = list(paths.ASSIGNMENTS_PENDING.glob("*.json"))
    assert state.read_json(pending)["ticket"] is None
    assert "[orchestrator] Artifact discipline" not in captured["initial_prompt"]
    assert "TKT-4242" in result["task_key_hint"]


def test_spawn_never_derives_key_from_name(fresh_orchestrator_dir, monkeypatch,
                                           configured_key_regex):
    captured = _patch_spawn_worker_tab(monkeypatch)
    result = _asyncio.run(spawn_worker_impl(
        initial_prompt="free text", name="tkt-4242-fix", cwd="/tmp"))
    (pending,) = list(paths.ASSIGNMENTS_PENDING.glob("*.json"))
    assert state.read_json(pending)["ticket"] is None
    assert "[orchestrator] Artifact discipline" not in captured["initial_prompt"]
    assert "tkt-4242" in result["task_key_hint"]


def test_spawn_hint_absent_with_explicit_task_key(fresh_orchestrator_dir, monkeypatch,
                                                  configured_key_regex):
    _patch_spawn_worker_tab(monkeypatch)
    result = _asyncio.run(spawn_worker_impl(
        initial_prompt="build the bot; cleanup tracked in TKT-999",
        name="demo-worker", cwd="/tmp", task_key="demo-task"))
    assert "task_key_hint" not in result
    (pending,) = list(paths.ASSIGNMENTS_PENDING.glob("*.json"))
    assert state.read_json(pending)["ticket"] == "demo-task"


def test_spawn_hint_absent_without_any_mention(fresh_orchestrator_dir, monkeypatch,
                                               configured_key_regex):
    _patch_spawn_worker_tab(monkeypatch)
    result = _asyncio.run(spawn_worker_impl(
        initial_prompt="just poke around", name="scout", cwd="/tmp"))
    assert "task_key_hint" not in result


def test_spawn_hint_uses_raw_caller_name_not_resolved(fresh_orchestrator_dir, monkeypatch,
                                                      configured_key_regex):
    _patch_spawn_worker_tab(monkeypatch)
    state.write_json_atomic(paths.ACTIVE / "sid-taken.json",
                            {"claude_sid": "sid-taken", "agent": "worker", "name": "scout"})
    result = _asyncio.run(spawn_worker_impl(
        initial_prompt="just poke around", name="scout", cwd="/tmp"))
    assert "task_key_hint" not in result
    result2 = _asyncio.run(spawn_worker_impl(
        initial_prompt="just poke around", name=None, cwd="/tmp"))
    assert "task_key_hint" not in result2


def test_spawn_worker_without_task_key_stays_unkeyed(fresh_orchestrator_dir, monkeypatch,
                                                     configured_key_regex):
    _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="/ticket-start TKT-8353", name="w1", cwd="/tmp"))
    (pending,) = list(paths.ASSIGNMENTS_PENDING.glob("*.json"))
    assert state.read_json(pending)["ticket"] is None


def test_slug_key_round_trips_store_and_joins_assignments(fresh_orchestrator_dir):
    artifact_put_impl("demo-task", "spec", "scraper", "# bot spec", "complete", "sid-bot")
    (entry,) = artifact_list_impl("demo-task")
    assert entry["phase"] == "spec" and entry["name"] == "scraper"
    assert Path(entry["path"]).parent == paths.ARTIFACTS / "demo-task"
    state.write_json_atomic(paths.ASSIGNMENTS / "sid-bot.json", {
        "claude_sid": "sid-bot", "name": "demo-worker", "ticket": "demo-task",
        "initial_prompt": "build the scraper half",
    })
    out = pipeline_status_impl("demo-task")
    assert "spec.scraper" in out
    assert "demo-worker" in out and "build the scraper half" in out


def test_spawn_worker_blank_task_key_rejected(fresh_orchestrator_dir, monkeypatch):
    _patch_spawn_worker_tab(monkeypatch)
    for blank in ("", "   "):
        with pytest.raises(ValueError, match="blank"):
            _asyncio.run(spawn_worker_impl(initial_prompt="x", name="w1", cwd="/tmp",
                                           task_key=blank))
    assert list(paths.ASSIGNMENTS_PENDING.glob("*.json")) == []


def test_spawn_worker_path_hostile_task_key_rejected(fresh_orchestrator_dir, monkeypatch):
    _patch_spawn_worker_tab(monkeypatch)
    with pytest.raises(ValueError, match="slug"):
        _asyncio.run(spawn_worker_impl(initial_prompt="x", name="w1", cwd="/tmp",
                                       task_key="yt bot"))
    assert list(paths.ASSIGNMENTS_PENDING.glob("*.json")) == []


def test_list_workers_renders_compact_spend(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    record = state.read_json(paths.ACTIVE / "w1.json")
    record["spend"] = {"turns": 12, "out_tokens": 340_000, "in_tokens": 900,
                       "cache_read_tokens": 5_100_000, "last_turn_out": 200,
                       "last_msg_id": "msg_z",
                       "by_model": {"claude-opus-5": {
                           "out_tokens": 340_000, "in_tokens": 900,
                           "cache_read_tokens": 5_100_000,
                           "cache_creation_5m_tokens": 0,
                           "cache_creation_1h_tokens": 0}}}
    state.write_json_atomic(paths.ACTIVE / "w1.json", record)
    workers = list_workers_impl()
    assert workers[0]["spend"] == "$11.05 / 340k out / 5.1M cache-rd"


def test_list_workers_spend_none_when_never_metered(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    workers = list_workers_impl()
    assert workers[0]["spend"] is None


def test_list_workers_compact_spend_small_and_large_counts(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    record = state.read_json(paths.ACTIVE / "w1.json")
    record["spend"] = {"turns": 1, "out_tokens": 512, "in_tokens": 0,
                       "cache_read_tokens": 0, "last_turn_out": 512, "last_msg_id": "m"}
    state.write_json_atomic(paths.ACTIVE / "w1.json", record)
    spend_line = list_workers_impl()[0]["spend"]
    assert spend_line == "512 out"
    assert "turn" not in spend_line and "episode" not in spend_line

    record["spend"] = {"turns": 200, "out_tokens": 2_400_000, "in_tokens": 0,
                       "cache_read_tokens": 0, "last_turn_out": 1, "last_msg_id": "m",
                       "by_model": {"claude-mystery-9": {
                           "out_tokens": 2_400_000, "in_tokens": 0,
                           "cache_read_tokens": 0,
                           "cache_creation_5m_tokens": 0,
                           "cache_creation_1h_tokens": 0}}}
    state.write_json_atomic(paths.ACTIVE / "w1.json", record)
    assert list_workers_impl()[0]["spend"] == "≥$0.00 / 2.4M out"


def test_worker_done_stamps_spend_totals(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    record = state.read_json(paths.ACTIVE / "w1.json")
    record["spend"] = {"turns": 12, "out_tokens": 340_000, "in_tokens": 900,
                       "cache_read_tokens": 5_100_000, "cache_creation_tokens": 75_000,
                       "last_turn_out": 200, "last_msg_id": "msg_z"}
    state.write_json_atomic(paths.ACTIVE / "w1.json", record)
    worker_done_impl(claude_sid="w1", summary="done")
    done_files = list(paths.DONE.rglob("*.json"))
    event = state.read_json(done_files[0])
    assert event["spend"] == {"turns": 12, "out_tokens": 340_000,
                              "in_tokens": 900, "cache_read_tokens": 5_100_000,
                              "cache_creation_tokens": 75_000}


def test_worker_done_spend_none_when_never_metered(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    worker_done_impl(claude_sid="w1", summary="done")
    event = state.read_json(list(paths.DONE.rglob("*.json"))[0])
    assert event["spend"] is None


def _write_nested_record(sid="nested-1", name="nested-abcd1234", agent="worker",
                         parent_manager_name="mgr", **overrides):
    record = {
        "claude_sid": sid, "agent": agent, "name": name, "cwd": "/x",
        "window_id": "", "pid": os.getpid(), "started_at": time.time(),
        "state": "idle", "last_summary": None, "last_turn_at": None,
        "nested": True, "nested_parent_sid": "parent-sid",
        "nested_parent_name": "parent-worker",
        "parent_manager_name": parent_manager_name, "runtime": "claude",
    }
    record.update(overrides)
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", record)
    return record


def test_worker_done_nested_suppressed(fresh_orchestrator_dir):
    from dockwright.mcp_server import worker_done_impl
    _write_nested_record()
    result = worker_done_impl("nested-1", "did things")
    assert result["ok"] is False
    assert result["nested"] is True
    assert list(paths.DONE.rglob("*.json")) == []


def test_ask_manager_nested_raises_without_question_file(fresh_orchestrator_dir):
    _write_nested_record()
    with pytest.raises(ValueError, match="nested"):
        _asyncio.run(ask_manager_impl("nested-1", "what now?", poll_interval=0.01))
    assert list(paths.QUESTIONS.rglob("*.json")) == []


def test_ask_manager_resume_nested_raises(fresh_orchestrator_dir):
    from dockwright.mcp_server import _write_question
    qid = _write_question(worker_sid="nested-1", worker_name="nested", question="q?")
    _write_nested_record()
    with pytest.raises(ValueError, match="nested"):
        _asyncio.run(ask_manager_impl("nested-1", "q?", poll_interval=0.01, resume_question_id=qid))
    assert len(list(paths.QUESTIONS.rglob("*.json"))) == 1


def test_kill_worker_nested_refuses(fresh_orchestrator_dir):
    from dockwright.mcp_server import kill_worker_impl
    _write_nested_record()
    with pytest.raises(ValueError, match="nested"):
        kill_worker_impl("nested-abcd1234")
    assert (paths.ACTIVE / "nested-1.json").exists()


def test_send_manager_to_worker_nested_refuses(fresh_orchestrator_dir):
    from dockwright.mcp_server import send_manager_to_worker_impl
    _write_nested_record()
    with pytest.raises(ValueError, match="nested"):
        send_manager_to_worker_impl("nested-abcd1234", "hello")


def test_list_workers_includes_nested_with_flag(fresh_orchestrator_dir):
    _write_nested_record()
    workers = list_workers_impl(manager_name="mgr")
    assert len(workers) == 1
    assert workers[0]["nested"] is True
    assert workers[0]["nested_parent_name"] == "parent-worker"


def test_list_managers_excludes_nested_manager_records(fresh_orchestrator_dir):
    from dockwright.mcp_server import list_managers
    _write_nested_record(sid="nested-mgr", name="nested-ffff0000", agent="manager")
    register_self_impl(claude_sid="mgr-1", agent="manager", name="real-mgr",
                       cwd="/x", iterm_sid="9", pid=os.getpid())
    names = [m["name"] for m in list_managers()]
    assert names == ["real-mgr"]


def test_backfill_ignores_nested_records(fresh_orchestrator_dir, capsys):
    from dockwright.mcp_server import _backfill_legacy_workers
    register_self_impl(claude_sid="mgr-1", agent="manager", name="real-mgr",
                       cwd="/x", iterm_sid="9", pid=os.getpid())
    _write_nested_record(sid="nested-mgr", name="nested-ffff0000", agent="manager",
                         parent_manager_name=None)
    _write_nested_record(sid="nested-w", name="nested-eeee0000", agent="worker",
                         parent_manager_name=None)
    state.write_json_atomic(paths.ACTIVE / "legacy-w.json", {
        "claude_sid": "legacy-w", "agent": "worker", "name": "legacy-worker",
        "cwd": "/x", "window_id": "7", "pid": os.getpid(),
        "parent_manager_name": None,
    })
    assert _backfill_legacy_workers() == 1
    assert state.read_json(paths.ACTIVE / "legacy-w.json")["parent_manager_name"] == "real-mgr"
    assert state.read_json(paths.ACTIVE / "nested-w.json")["parent_manager_name"] is None

def test_spawn_worker_default_title_is_plain_name(monkeypatch):
    terminal._DRIVER = None
    captured = _patch_exec(monkeypatch)
    _asyncio.run(spawner.spawn_worker_tab(
        extra_args=FLAGS, cwd="/tmp/x", initial_prompt="hi", name="alpha"))
    argv = list(captured["args"])
    assert "-n" in argv and argv[argv.index("-n") + 1] == "alpha"
    assert not any("[w]" in str(a) for a in argv)


def test_resolve_manager_window_title_match_without_exclude_id(monkeypatch):
    import dockwright.mcp_server as m
    data = [{"wm_class": "mgr", "tabs": [{"title": "alpha · general",
             "windows": [{"id": "%3", "cwd": "/c", "title": "alpha · general", "pid": "1"}]}]}]
    monkeypatch.setattr("dockwright.mcp_server._terminal_ls", lambda: data)
    assert m._resolve_manager_window("no-such-sid", "alpha") == "%3"


def test_match_worker_by_cwd_uniqueness_on_tmux():
    import dockwright.mcp_server as m
    data = [{"wm_class": "claude-workers", "tabs": [{"title": "w",
             "windows": [{"id": "%6", "cwd": "/work/x", "title": "t", "pid": "2"}]}]}]
    rec = {"cwd": "/work/x", "runtime": "claude"}
    assert m._match_worker_window_by_cwd_runtime(data, rec) == "%6"


def test_mcp_send_and_close_emit_tmux_argv(fresh_orchestrator_dir, monkeypatch):
    import subprocess as _sp
    from dockwright import terminal
    import dockwright.mcp_server as srv

    monkeypatch.setenv("CLAUDE_ORCH_TERMINAL", "tmux")
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    terminal._DRIVER = None

    calls = []

    def _fake_run(args, *pos, **kw):
        calls.append(list(args))
        return _sp.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(_sp, "run", _fake_run)

    srv._type_unguarded("%5", "hello worker")
    srv._close_window("%5")

    assert any(
        "send-keys" in c and c[-1] == "Enter" and "%5" in c
        for c in calls
    ), f"No tmux send-keys Enter found in: {calls}"

    assert any(
        c[0] == "tmux" and "kill-pane" in c and "%5" in c
        for c in calls
    ), f"No tmux kill-pane %5 found in: {calls}"

    assert not any("kitty" in c[0] for c in calls), f"kitty appeared in calls: {calls}"


def test_await_input_ready_returns_when_idle(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv
    calls = {"n": 0}

    def fake_idle(screen):
        calls["n"] += 1
        return calls["n"] >= 3

    monkeypatch.setattr(srv, "_capture_text", lambda wid: "screen")
    monkeypatch.setattr(srv, "_input_is_idle", fake_idle)
    monkeypatch.setattr(srv, "_INPUT_READY_POLL_SEC", 0.0)
    _asyncio.run(srv._await_input_ready("555", "claude"))
    assert calls["n"] == 3

def test_await_input_ready_times_out_without_raising(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv
    monkeypatch.setattr(srv, "_capture_text", lambda wid: None)
    monkeypatch.setattr(srv, "_INPUT_READY_POLL_SEC", 0.0)
    monkeypatch.setattr(srv, "_INPUT_READY_TIMEOUT_SEC", 0.05)
    _asyncio.run(srv._await_input_ready("555", "claude"))

def test_await_input_ready_codex_short_circuits(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv

    def boom(screen):
        raise AssertionError("_input_is_idle must not be called for codex")

    monkeypatch.setattr(srv, "_input_is_idle", boom)
    monkeypatch.setattr(srv, "_INPUT_READY_CODEX_SLEEP_SEC", 0.0)
    _asyncio.run(srv._await_input_ready("555", "codex"))

def test_await_input_ready_no_window_id_returns_immediately(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv
    monkeypatch.setattr(srv, "_capture_text",
                        lambda wid: (_ for _ in ()).throw(AssertionError("no poll expected")))
    _asyncio.run(srv._await_input_ready("", "claude"))


from dockwright.mcp_server import send_manager_to_worker_auto_impl as _auto_send


def _write_closed(name, sid, cwd="/tmp/wt", runtime="claude", closed_at=1.0):
    state.write_json_atomic(paths.CLOSED / f"{sid}.json", {
        "claude_sid": sid, "name": name, "cwd": cwd,
        "runtime": runtime, "closed_at": closed_at,
    })
    state.write_json_atomic(paths.assignment_path(sid), {"claude_sid": sid, "name": name, "cwd": cwd,
                                                        "runtime": runtime, "spawn_extra_args": list(FLAGS)})


def test_auto_send_live_worker_delivers_without_resume(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="42")
    typed = []
    monkeypatch.setattr(srv, "_type_unguarded", lambda wid, text: typed.append(text))

    async def no_resume(*a, **k):
        raise AssertionError("resume must not fire for a live worker")

    monkeypatch.setattr(srv, "resume_worker_impl", no_resume)
    result = _asyncio.run(_auto_send("alpha", "hi"))
    assert result["status"] == "delivered" and "resumed" not in result
    assert typed == ["[MANAGER] hi"]


def test_auto_send_resumes_closed_worker_and_delivers(fresh_orchestrator_dir, tmp_path, monkeypatch):
    import dockwright.mcp_server as srv
    _make_transcript(tmp_path, monkeypatch, "gone-sid")
    _write_closed("alpha", "gone-sid")
    _patch_spawn_registers_active(monkeypatch)
    typed = []
    monkeypatch.setattr(srv, "_type_unguarded", lambda wid, text: typed.append((wid, text)))
    monkeypatch.setattr(srv, "_terminal_ls", lambda: [
        {"tabs": [{"windows": [
            {"id": "999", "cwd": "/tmp/wt",
             "foreground_processes": [{"cmdline": ["claude", "--resume"]}]}]}]}])
    monkeypatch.setattr(srv, "_INPUT_READY_TIMEOUT_SEC", 0.0)
    result = _asyncio.run(_auto_send(
        "alpha", "continue", _registration_timeout_sec=2.0, _poll_interval=0.01))
    assert result["status"] == "delivered"
    assert result["resumed"] is True and result["sid"] == "gone-sid"
    assert result["worker"] == "alpha"
    assert typed == [("999", "[MANAGER] continue")]
    assert typed[0][1].count("[MANAGER] ") == 1
    resumed_record = state.read_json(paths.ACTIVE / "gone-sid.json")
    assert resumed_record.get("tasked_at"), "delivery must stamp the tasking episode"
    assert not (paths.CLOSED / "gone-sid.json").exists()


def test_auto_send_nothing_resumable_raises_combined(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match=r"no worker named 'ghost'.*auto_resume.*no closed worker"):
        _asyncio.run(_auto_send("ghost", "hi"))


def test_auto_send_closed_without_transcript_raises_combined(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_closed("alpha", "dead-sid")
    with pytest.raises(ValueError, match=r"auto_resume.*none have a live transcript"):
        _asyncio.run(_auto_send("alpha", "hi"))


def test_auto_send_registration_timeout_raises_and_keeps_record(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _make_transcript(tmp_path, monkeypatch, "stuck-sid")
    _write_closed("alpha", "stuck-sid")
    _patch_spawn_worker_tab(monkeypatch)
    with pytest.raises(ValueError, match="message NOT delivered"):
        _asyncio.run(_auto_send("alpha", "hi",
                                _registration_timeout_sec=0.05, _poll_interval=0.01))
    assert (paths.CLOSED / "stuck-sid.json").exists()


def test_auto_send_manager_holder_refused(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _make_transcript(tmp_path, monkeypatch, "old-worker-sid")
    state.write_json_atomic(paths.ACTIVE / "mgr1.json", {
        "claude_sid": "mgr1", "agent": "manager", "name": "happy-yak",
        "cwd": "/x", "iterm_sid": "i1", "pid": os.getpid(), "started_at": 0})
    _write_closed("happy-yak", "old-worker-sid")
    with pytest.raises(ValueError, match="already active"):
        _asyncio.run(_auto_send("happy-yak", "hi"))


def test_auto_send_nested_target_raises(fresh_orchestrator_dir):
    state.write_json_atomic(paths.ACTIVE / "nested-abcd.json", {
        "claude_sid": "nested-abcd", "agent": "worker", "name": "nested-abcd1234",
        "cwd": "/x", "pid": os.getpid(), "started_at": 0,
        "nested": True, "nested_parent_name": "alpha"})
    with pytest.raises(ValueError, match="nested sub-session"):
        _asyncio.run(_auto_send("nested-abcd1234", "hi"))


def test_auto_send_codex_lane(fresh_orchestrator_dir, tmp_path, monkeypatch):
    import dockwright.mcp_server as srv
    _make_codex_transcript(tmp_path, monkeypatch, "cx-sid")
    _write_closed("cx", "cx-sid", cwd="/tmp/cx", runtime="codex")
    _patch_spawn_registers_active(monkeypatch)
    typed = []
    monkeypatch.setattr(srv, "_type_unguarded", lambda wid, text: typed.append(text))
    monkeypatch.setattr(srv, "_terminal_ls", lambda: [
        {"tabs": [{"windows": [
            {"id": "999", "cwd": "/tmp/cx",
             "foreground_processes": [{"cmdline": ["codex", "resume"]}]}]}]}])
    monkeypatch.setattr(srv, "_INPUT_READY_CODEX_SLEEP_SEC", 0.0)
    monkeypatch.setattr(
        srv, "_input_is_idle",
        lambda screen: (_ for _ in ()).throw(AssertionError("codex must not poll idle")))
    result = _asyncio.run(_auto_send(
        "cx", "continue", _registration_timeout_sec=2.0, _poll_interval=0.01))
    assert result["resumed"] is True and result["sid"] == "cx-sid"
    assert typed == ["[MANAGER] continue"]


def test_auto_send_concurrent_resume_in_flight_raises(fresh_orchestrator_dir, tmp_path, monkeypatch):
    import dockwright.mcp_server as srv
    _make_transcript(tmp_path, monkeypatch, "rr-sid")
    _write_closed("alpha", "rr-sid")
    srv._RESUMES_IN_FLIGHT.add("alpha")
    try:
        with pytest.raises(ValueError, match="already in progress"):
            _asyncio.run(_auto_send("alpha", "hi"))
    finally:
        srv._RESUMES_IN_FLIGHT.discard("alpha")


def test_auto_send_spawn_failure_propagates_and_keeps_record(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _make_transcript(tmp_path, monkeypatch, "sp-sid")
    _write_closed("alpha", "sp-sid")

    async def broken_spawn(**kwargs):
        raise ConnectionRefusedError("no tmux")

    monkeypatch.setattr(spawner, "spawn_worker_tab", broken_spawn)
    with pytest.raises(RuntimeError, match="Could not spawn tab"):
        _asyncio.run(_auto_send("alpha", "hi"))
    assert (paths.CLOSED / "sp-sid.json").exists()


def test_auto_send_post_resume_delivery_failure_names_resumed_sid(fresh_orchestrator_dir, tmp_path, monkeypatch):
    import dockwright.mcp_server as srv
    _make_transcript(tmp_path, monkeypatch, "pr-sid")
    _write_closed("alpha", "pr-sid")

    async def fake_spawn(**kwargs):
        sid = kwargs.get("resume_sid")
        state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {
            "claude_sid": sid, "agent": "worker", "name": kwargs.get("name"),
            "cwd": kwargs.get("cwd"), "iterm_sid": "", "pid": os.getpid(),
            "started_at": 0, "runtime": "claude"})
        return ("", kwargs.get("name"))

    monkeypatch.setattr(spawner, "spawn_worker_tab", fake_spawn)
    monkeypatch.setattr(srv, "_terminal_ls", lambda: [])
    monkeypatch.setattr(srv, "_WINDOW_RESOLVE_RETRY_SLEEP", 0)
    monkeypatch.setattr(srv, "_INPUT_READY_TIMEOUT_SEC", 0.0)
    with pytest.raises(ValueError, match=r"WAS resumed \(sid=pr-sid\)"):
        _asyncio.run(_auto_send("alpha", "hi",
                                _registration_timeout_sec=2.0, _poll_interval=0.01))


def test_send_tool_default_auto_resume_false_unchanged(fresh_orchestrator_dir, tmp_path, monkeypatch):
    import dockwright.mcp_server as srv
    _make_transcript(tmp_path, monkeypatch, "cl-sid")
    _write_closed("alpha", "cl-sid")

    async def no_resume(*a, **k):
        raise AssertionError("resume must not fire when auto_resume is off")

    monkeypatch.setattr(srv, "resume_worker_impl", no_resume)
    with pytest.raises(ValueError, match="no worker named 'alpha'"):
        _asyncio.run(srv.send_manager_to_worker(worker="alpha", text="hi"))


def _write_manager_record(sid, name, **overrides):
    record = {
        "claude_sid": sid, "agent": "manager", "name": name, "cwd": "/x",
        "window_id": "i0", "pid": os.getpid(), "started_at": time.time(),
        "state": "idle", "last_turn_at": None, "last_summary": None,
        "domain": "general", "parent_manager_name": None, "runtime": "claude",
    }
    record.update(overrides)
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", record)


def test_list_managers_flags_never_took_a_turn(fresh_orchestrator_dir):
    from dockwright.mcp_server import list_managers
    now = time.time()
    _write_manager_record("ghost", "noisy-wizard-2", started_at=now - 700)
    _write_manager_record("booting", "fresh-boot", started_at=now - 30)
    _write_manager_record("turned", "old-faithful", started_at=now - 90000,
                          last_turn_at="2026-07-31T05:00:00.000Z")
    _write_manager_record("uptime-only", "quiet-scribe", started_at=now - 90000,
                          last_turn_at_uptime=12345.6)
    _write_manager_record("no-birth", "ancient-one", started_at=None)
    flags = {m["name"]: m["never_took_a_turn"] for m in list_managers()}
    assert flags == {
        "noisy-wizard-2": True,
        "fresh-boot": False,
        "old-faithful": False,
        "quiet-scribe": False,
        "ancient-one": True,
    }


def _alpha():
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="42")


def test_ask_manager_with_a_keepalive_beats_and_returns_the_answer(fresh_orchestrator_dir):
    _alpha()
    beats = []

    async def keepalive(n):
        beats.append(n)

    async def scenario():
        task = _asyncio.create_task(mcp_server.ask_manager_impl(
            claude_sid="w1", question="q?", poll_interval=0.01,
            keepalive=keepalive, keepalive_sec=0.02))
        await _asyncio.sleep(0.15)
        qid = mcp_server.list_pending_questions_impl()[0]["question_id"]
        mcp_server.answer_question_impl(qid, "yes")
        return await _asyncio.wait_for(task, timeout=2.0)

    assert _asyncio.run(scenario()) == "yes"
    assert len(beats) >= 3 and beats == sorted(beats)


def _spy_timeout(monkeypatch):
    seen = {}
    real_sentinel = mcp_server._reask_sentinel

    def spy(qid, timeout_sec, *a, **k):
        seen["timeout"] = timeout_sec
        return real_sentinel(qid, timeout_sec, *a, **k)

    monkeypatch.setattr(mcp_server, "_reask_sentinel", spy)
    return seen


def test_the_long_wait_needs_a_keepalive(fresh_orchestrator_dir, monkeypatch):
    _alpha()
    seen = _spy_timeout(monkeypatch)
    monkeypatch.setattr(mcp_server, "ASK_MANAGER_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(mcp_server, "ASK_MANAGER_LONG_WAIT_SEC", 99)
    result = _asyncio.run(mcp_server.ask_manager_impl(
        claude_sid="w1", question="q?", poll_interval=0.01))
    assert result.startswith("NO_ANSWER_YET:")
    assert seen["timeout"] == 0.05


def test_a_keepalive_uses_the_long_wait(fresh_orchestrator_dir, monkeypatch):
    _alpha()
    seen = _spy_timeout(monkeypatch)

    async def keepalive(n):
        return None

    monkeypatch.setattr(mcp_server, "ASK_MANAGER_LONG_WAIT_SEC", 0.05)
    result = _asyncio.run(mcp_server.ask_manager_impl(
        claude_sid="w1", question="q?", poll_interval=0.01, keepalive=keepalive,
        keepalive_sec=0.01))
    assert result.startswith("NO_ANSWER_YET:")
    assert seen["timeout"] == 0.05


def test_a_failing_keepalive_ends_the_wait_without_eating_the_answer(fresh_orchestrator_dir):
    _alpha()

    async def broken(n):
        raise OSError("transport closed")

    result = _asyncio.run(_asyncio.wait_for(mcp_server.ask_manager_impl(
        claude_sid="w1", question="q?", poll_interval=0.01, keepalive=broken,
        keepalive_sec=0.01), timeout=5.0))
    assert result.startswith("NO_ANSWER_YET:")
    qid = mcp_server.list_pending_questions_impl()[0]["question_id"]
    mcp_server.answer_question_impl(qid, "later")
    assert (paths.ANSWERS / f"{qid}.json").exists()


def test_codex_tui_keeps_the_old_wait_and_codex_exec_its_own():
    from dockwright import config
    assert config.ask_parent_wait_sec("codex-tui") == 1500
    assert config.ask_parent_wait_sec("codex-exec") == 240
    assert config.ask_parent_wait_sec("claude-tui") is None


def test_the_provider_bound_clamps_the_long_wait(fresh_orchestrator_dir, monkeypatch):
    from dockwright import config
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x",
                       iterm_sid="42", provider="codex-tui")
    monkeypatch.setattr(mcp_server, "ASK_MANAGER_LONG_WAIT_SEC", 10_000)
    monkeypatch.setattr(config, "DEFAULT_ASK_PARENT_WAIT_SEC", {"codex-tui": 0.05})
    seen = []
    real_sentinel = mcp_server._reask_sentinel

    def spy(qid, timeout_sec, *args):
        seen.append(timeout_sec)
        return real_sentinel(qid, timeout_sec, *args)

    monkeypatch.setattr(mcp_server, "_reask_sentinel", spy)

    async def keepalive(n):
        pass

    result = _asyncio.run(_asyncio.wait_for(mcp_server.ask_manager_impl(
        claude_sid="w1", question="q?", poll_interval=0.01, keepalive=keepalive,
        keepalive_sec=0.01), timeout=5.0))
    assert result.startswith("NO_ANSWER_YET:")
    assert seen == [0.05]


def test_the_mcp_app_keeps_ask_manager_alive_with_progress_and_returns_the_answer(
        fresh_orchestrator_dir, monkeypatch):
    from mcp.shared.memory import create_connected_server_and_client_session
    _alpha()
    monkeypatch.setattr(mcp_server, "ASK_KEEPALIVE_SEC", 0.05)
    progress = []

    async def on_progress(value, total, message):
        progress.append(value)

    async def answer_after_two_beats():
        while len(progress) < 2 or not mcp_server.list_pending_questions_impl():
            await _asyncio.sleep(0.02)
        qid = mcp_server.list_pending_questions_impl()[0]["question_id"]
        mcp_server.answer_question_impl(qid, "yes")

    async def scenario():
        async with create_connected_server_and_client_session(mcp_server.mcp) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            answerer = _asyncio.create_task(answer_after_two_beats())
            result = await client.call_tool(
                "ask_manager", {"claude_sid": "w1", "question": "q?"},
                progress_callback=on_progress)
            await answerer
            return tools, result

    tools, result = _asyncio.run(_asyncio.wait_for(scenario(), timeout=10.0))
    assert not result.isError
    assert result.content[0].text == "yes"
    assert len(progress) >= 2
    for name in ("ask_manager", "ask_parent"):
        assert "ctx" not in tools[name].inputSchema.get("properties", {})
        assert "ctx" not in tools[name].inputSchema.get("required", [])


def test_progress_keepalive_needs_a_progress_token():
    class Meta:
        progressToken = None

    class Req:
        meta = Meta()

    class Ctx:
        request_context = Req()

    assert mcp_server._progress_keepalive(None) is None
    assert mcp_server._progress_keepalive(Ctx()) is None
    Meta.progressToken = 7
    assert mcp_server._progress_keepalive(Ctx()) is not None


def test_a_relay_into_a_blocked_worker_names_its_question(fresh_orchestrator_dir, monkeypatch):
    _alpha()
    monkeypatch.setattr("dockwright.mcp_server._type_unguarded", lambda wid, text: None)
    qid = mcp_server._write_question("w1", "alpha", "q?", None)
    result = send_manager_to_worker_impl(worker="alpha", text="do Z instead")
    assert result["status"] == "delivered"
    assert result["blocked_on"] == qid


def test_a_relay_into_an_unblocked_worker_carries_no_blocked_on(fresh_orchestrator_dir, monkeypatch):
    _alpha()
    monkeypatch.setattr("dockwright.mcp_server._type_unguarded", lambda wid, text: None)
    result = send_manager_to_worker_impl(worker="alpha", text="hi")
    assert "blocked_on" not in result


from datetime import datetime
from zoneinfo import ZoneInfo

import dockwright.mcp_server as ms
from dockwright import waiting_on

_LIVE = 424242
_CLAUDE = "claude --settings /w/worker.json"


def _probes(monkeypatch, *, commands=None, parents=None, screen=None, banner=None, ps=True, log=True,
            log_file=None):
    commands = {_LIVE: _CLAUDE} if commands is None else commands
    index = {"command_by_pid": commands, "child_commands": {}, "parent_by_pid": parents or {}}
    monkeypatch.setattr(ms, "_pid_alive", lambda pid: pid in commands)
    monkeypatch.setattr(ms, "_process_index", lambda: index if ps else None)
    monkeypatch.setattr(ms, "_capture_plain", lambda window_id: screen)
    session_log = log_file if log_file is not None else Path("/w/session.jsonl")
    monkeypatch.setattr(ms, "find_session_log",
                        lambda sid, runtime="claude", root=None: session_log if log else None)
    if log_file is None:
        monkeypatch.setattr(ms, "_limit_banner_text", lambda log_path, strict=False: banner)


def _transcript(tmp_path, text, *, quiet_sec=0):
    log = tmp_path / "transcripts" / "session.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n")
    stamp = time.time() - quiet_sec
    os.utime(log, (stamp, stamp))
    return log


def _holder(task_key, sid, assignment_id, *, pid=_LIVE, state_="idle", tasked_at=100.0, input_at=None,
            window_id="%9"):
    state.write_json_atomic(paths.assignment_path(sid), {
        "assignment_id": assignment_id, "claude_sid": sid, "ticket": task_key,
        "name": f"{task_key}-{assignment_id}", "spawned_at": 1.0})
    record = {"claude_sid": sid, "agent": "worker", "name": f"{task_key}-{assignment_id}", "pid": pid,
              "state": state_, "runtime": "claude", "window_id": window_id, "parent_manager_name": "mgr",
              "tasked_at": tasked_at}
    if input_at is not None:
        record["input_at"] = input_at
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", record)


def _reported(sid, completed_at=200.0):
    done = paths.done_dir_for("mgr")
    done.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(done / f"{sid}-e1.json", {"claude_sid": sid, "completed_at": completed_at})


def _ask(sid, qid="q1"):
    qdir = paths.question_dir_for("mgr")
    qdir.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(qdir / f"{qid}.json", {"question_id": qid, "worker_sid": sid, "question": "?",
                                                   "asked_at": 150.0})


def _status(task_key):
    return json.loads(ms.pipeline_status_impl(task_key, "json"))


def _at_pr_with_a_pr_url(task_key, tmp_path, sid="sid-p"):
    _seed_snapshot(task_key, tmp_path)
    _commit(task_key, "pr", 1, "a-pr")
    _holder(task_key, sid, "a-pr")
    artifact_put_impl(task_key, "implement", "repo", "https://github.com/o/r/pull/7", "complete", sid,
                      scope="output")


_WAITING = "waiting 2026-10-08T12:00:00Z — waiting for approval on o/c#8\nmerged https://github.com/o/r/pull/7 abc1234 by node\n"


def _at_post_merge_for_status(task_key, tmp_path, monkeypatch, merge_state=_WAITING):
    from tests.test_pipeline_core import APPROVE
    _seed_snapshot(task_key, tmp_path, schema_text=APPROVE)
    _commit(task_key, "post-merge", 1, "a-pm")
    _holder(task_key, "sid-pm", "a-pm")
    artifact_put_impl(task_key, "implement", "repo", "https://github.com/o/r/pull/7\nhttps://github.com/o/c/pull/8\n",
                      "complete", "sid-p", scope="output")
    if merge_state is not None:
        artifact_put_impl(task_key, "merge", "state", merge_state, "partial", "sid-pm", scope="checkpoint")


def test_waiting_on_is_the_holders_open_question(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch)
    _seed_snapshot("W1", tmp_path)
    _holder("W1", "sid-d", "a-design")
    _ask("sid-d")
    out = _status("W1")
    assert out["waiting_on"] == "human:question" and out["tokens"][0]["waiting_on"] == "human:question"
    assert out["questions"] == [{"question_id": "q1", "worker_sid": "sid-d", "asked_at": 150.0}]


def test_a_dead_holder_pid_reads_stalled_never_worker(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch)
    _seed_snapshot("W2", tmp_path)
    _holder("W2", "sid-d", "a-design", pid=_LIVE + 1)
    out = _status("W2")
    assert out["waiting_on"] == "stalled:design" and out["tokens"][0]["holder_alive"] is False


def test_a_live_pid_that_ps_shows_as_another_program_reads_stalled(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch, commands={_LIVE: "/usr/bin/python3 -m http.server"})
    _seed_snapshot("W3", tmp_path)
    _holder("W3", "sid-d", "a-design")
    assert _status("W3")["waiting_on"] == "stalled:design"


def test_a_live_unreported_holder_reads_worker_at_its_node(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch)
    _seed_snapshot("W4", tmp_path)
    _holder("W4", "sid-d", "a-design")
    out = _status("W4")
    assert out["waiting_on"] == "worker:design"
    assert out["tokens"][0]["holder_sid"] == "sid-d" and out["tokens"][0]["holder_reported"] is False


def test_a_reported_pr_holder_with_a_pr_url_waits_on_the_merge(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch)
    _at_pr_with_a_pr_url("W5", tmp_path)
    _reported("sid-p")
    out = _status("W5")
    assert out["waiting_on"] == "external:merge" and out["tokens"][0]["holder_reported"] is True
    assert "\nwaiting_on: external:merge\n" in ms.pipeline_status_impl("W5")


def test_a_waiting_post_merge_holder_waits_on_the_merge(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch)
    _at_post_merge_for_status("W6", tmp_path, monkeypatch)
    _reported("sid-pm")
    assert _status("W6")["waiting_on"] == "external:merge"


def test_a_waiting_word_older_than_the_holder_does_not_read_as_a_merge_wait(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch)
    from tests.test_pipeline_core import APPROVE
    _seed_snapshot("W8", tmp_path, schema_text=APPROVE)
    artifact_put_impl("W8", "implement", "repo", "https://github.com/o/r/pull/7\n", "complete", "sid-p", scope="output")
    artifact_put_impl("W8", "merge", "state", _WAITING, "partial", "sid-old", scope="checkpoint")
    merge_state = paths.artifact_path("W8", "merge", "state")
    stamp = state.parse_artifact(merge_state.read_text())[0]["written_at"]
    merge_state.write_text(merge_state.read_text().replace(f"written_at: {stamp}", f"written_at: {stamp - 3 * PAST_GRACE}"))
    _commit("W8", "post-merge", 1, "a-pm")
    _holder("W8", "sid-pm", "a-pm")
    _reported("sid-pm")
    assert _status("W8")["waiting_on"] == "stalled:post-merge"


def test_status_holds_between_the_approve_firing_and_the_first_merge_state_put(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch)
    _at_post_merge_for_status("W7", tmp_path, monkeypatch, merge_state=None)
    _reported("sid-pm")
    assert _status("W7")["waiting_on"] == "stalled:post-merge"


def test_new_input_after_the_done_brings_worker_back(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch)
    _at_pr_with_a_pr_url("W6", tmp_path)
    _holder("W6", "sid-p", "a-pr", input_at=300.0)
    _reported("sid-p")
    assert _status("W6")["waiting_on"] == "worker:pr"


def test_a_rate_limit_banner_reads_time_with_its_reset(fresh_orchestrator_dir, tmp_path, monkeypatch):
    hour = (datetime.fromtimestamp(time.time(), ZoneInfo("Etc/UTC")).hour + 2) % 24
    label = f"{hour % 12 or 12}{'am' if hour < 12 else 'pm'}"
    _probes(monkeypatch, banner=f"You've hit your session limit · resets {label} (Etc/UTC)")
    _seed_snapshot("W7", tmp_path)
    _holder("W7", "sid-d", "a-design")
    before = time.time()
    out = _status("W7")
    assert out["waiting_on"] == "external:time"
    reset_at = out["tokens"][0]["reset_at"]
    assert isinstance(reset_at, int) and before < reset_at <= before + 3 * 3600


def test_an_overload_banner_is_not_a_limit(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch, banner="API Error: 529 overloaded")
    _seed_snapshot("W8", tmp_path)
    _holder("W8", "sid-d", "a-design")
    assert _status("W8")["waiting_on"] == "worker:design"


def test_a_paused_request_reads_time_and_a_failed_spawn_reads_stalled(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch)
    for key, reason, expected in (("W9", "paused", "external:time"), ("W10", "spawn-error", "stalled:task")):
        _seed_snapshot(key, tmp_path)
        _holder(key, f"sid-{key}", "a-design")
        _reported(f"sid-{key}")
        events = paths.artifact_events_path(key)
        state.append_event(events, {"type": "advance_requested", "token_id": "t1", "request_id": "r1",
                                    "edge": {"from": "design", "to": "task"}, "index_to": 1, "kind": "advance",
                                    "ts": time.time() - PAST_GRACE})
        state.append_event(events, {"type": "advance_pending", "token_id": "t1", "request_id": "r1",
                                    "reason": reason, "assignment_id": None})
        out = _status(key)
        assert out["waiting_on"] == expected and out["tokens"][0]["waiting_on"] is None
        assert out["open_requests"][0]["waiting_on"] == expected
        assert out["open_requests"][0]["pending_reason"] == reason
        line = next(ln for ln in ms.pipeline_status_impl(key).splitlines() if ln.startswith("- open request r1 "))
        assert line.endswith(f" waiting_on={expected}")


def test_a_forked_work_reads_its_most_urgent_row(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch)
    _seed_snapshot("W11", tmp_path)
    _holder("W11", "sid-a", "a-row-a")
    events = paths.artifact_events_path("W11")
    state.append_event(events, {"type": "token_forked", "token_id": "t1", "node": "task",
                                "rows": [{"row": "a", "stage": 1, "token_id": "t1/a"},
                                         {"row": "b", "stage": 1, "token_id": "t1/b"}]})
    for row in ("a", "b"):
        state.append_event(events, {"type": "advance_requested", "token_id": f"t1/{row}", "request_id": f"r{row}",
                                    "edge": {"from": "design", "to": "task"}, "index_to": 1, "kind": "fork",
                                    "row": row})
    state.append_event(events, {"type": "advance_committed", "token_id": "t1/a", "request_id": "ra",
                                "node": "task", "index": 1, "holder": "a-row-a"})
    state.append_event(events, {"type": "advance_pending", "token_id": "t1/b", "request_id": "rb",
                                "reason": "paused", "assignment_id": None})
    out = _status("W11")
    assert out["waiting_on"] == "worker:task"
    assert {t["token_id"]: t["waiting_on"] for t in out["tokens"]} == {"t1": None, "t1/a": "worker:task"}
    assert [r["waiting_on"] for r in out["open_requests"]] == ["external:time"]
    lines = ms.pipeline_status_impl("W11").splitlines()
    assert any(ln.startswith("- t1: ") and " waiting_on=- " in ln and ln.endswith(" forked") for ln in lines)
    assert any(ln.startswith("- t1/a: ") and ln.endswith(" waiting_on=worker:task row=a") for ln in lines)


def test_a_gate_launched_under_the_holder_reads_human_gate(fresh_orchestrator_dir, tmp_path, monkeypatch):
    shell = "/bin/zsh -c source /u/.claude/shell-snapshots/s.sh && eval 'bash /u/.claude/scripts/plannotator-gate /r/'"
    gate = "bash /u/.claude/scripts/plannotator-gate /r/ --gate --json"
    commands = {_LIVE: _CLAUDE, 500: shell, 501: gate}
    _probes(monkeypatch, commands=commands, parents={500: _LIVE, 501: 500})
    _seed_snapshot("W12", tmp_path)
    _holder("W12", "sid-d", "a-design")
    assert _status("W12")["waiting_on"] == "human:gate"
    _probes(monkeypatch, commands=commands, parents={500: 1, 501: 500})
    assert _status("W12")["waiting_on"] == "worker:design"


def test_an_approval_dialog_on_a_processing_holder_reads_human(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch, screen="Bash command\n  rm -rf build\n\nDo you want to proceed?\n❯ 1. Yes\n  2. No\n")
    _seed_snapshot("W13", tmp_path)
    _holder("W13", "sid-d", "a-design", state_="processing")
    assert _status("W13")["waiting_on"] == "human:approval-dialog"


def test_a_tracker_watcher_on_the_works_key_reads_its_external_value(fresh_orchestrator_dir, tmp_path,
                                                                     monkeypatch):
    watcher = (f"/opt/py/Python.app/Contents/MacOS/Python /u/.claude/scripts/{waiting_on.TRACKER_WATCH_SCRIPT} "
               "W14 --poll 300")
    _probes(monkeypatch, commands={_LIVE: _CLAUDE, 600: watcher})
    _seed_snapshot("W14", tmp_path)
    _holder("W14", "sid-d", "a-design")
    _reported("sid-d")
    assert _status("W14")["waiting_on"] == waiting_on.EXTERNAL_TRACKER
    _probes(monkeypatch, commands={_LIVE: _CLAUDE, 600: watcher.replace("W14", "OTHER-1")})
    assert _status("W14")["waiting_on"] == "stalled:design"


def test_a_finished_terminal_node_reads_none(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch)
    _seed_snapshot("W15", tmp_path)
    _commit("W15", "post-merge", 1, "a-pm")
    _holder("W15", "sid-pm", "a-pm")
    _reported("sid-pm")
    artifact_put_impl("W15", "summary", "post-merge", "done", "complete", "sid-pm", scope="output")
    assert _status("W15")["waiting_on"] == "none"


def test_failed_probes_are_named_and_never_raise(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch, ps=False, log=False)
    _seed_snapshot("W16", tmp_path)
    _holder("W16", "sid-d-123456789", "a-design")
    out = _status("W16")
    assert out["probe_errors"] == ["ps", "transcript:sid-d-12"] and out["waiting_on"] == "worker:design"
    assert "\nwaiting_on: worker:design (probe failed: ps, transcript:sid-d-12)\n" in ms.pipeline_status_impl("W16")


def test_a_work_without_a_snapshot_reads_its_open_question_or_none(fresh_orchestrator_dir, monkeypatch):
    _probes(monkeypatch)
    artifact_put_impl("W17", "spec", "a", "x", "complete", "sid-q")
    assert _status("W17")["waiting_on"] == "none"
    state.write_json_atomic(paths.assignment_path("sid-q"), {"assignment_id": "a-q", "claude_sid": "sid-q",
                                                             "ticket": "W17", "spawned_at": 1.0})
    _ask("sid-q")
    assert _status("W17")["waiting_on"] == "human:question"


def test_a_failed_fold_is_a_probe_error(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch)
    _seed_snapshot("W18", tmp_path)
    _append_malformed_commit("W18")
    out = _status("W18")
    assert out["waiting_on"] == "none" and out["probe_errors"] == ["fold"] and out["tokens"] == []


def test_the_json_status_is_one_versioned_object(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch)
    _seed_snapshot("W19", tmp_path)
    _holder("W19", "sid-d", "a-design")
    artifact_put_impl("W19", "decision", "q-1", "x", "complete", "sid-d", decided_by="manager")
    raw = ms.pipeline_status_impl("W19", "json")
    assert "\n" not in raw
    out = json.loads(raw)
    assert list(out) == ["version", "task_key", "waiting_on", "topic", "gate", "probe_errors", "round", "closed",
                         "carried_prs", "tokens", "open_requests", "questions", "artifacts", "assignments", "events"]
    assert out["version"] == 1 and out["task_key"] == "W19" and out["round"] == 1
    assert out["closed"] is None
    assert set(out["tokens"][0]) == {"token_id", "node", "index", "holder", "holder_sid", "holder_alive",
                                     "holder_reported", "row", "forked", "held", "waiting_on", "reset_at"}
    decision = next(a for a in out["artifacts"] if a["phase"] == "decision")
    assert decision["decided_by"] == "manager" and decision["writer_liveness"] == "active"
    assert set(decision) == {"phase", "name", "status", "scope", "writer_sid", "writer_liveness", "written_at",
                             "decided_by", "read_count", "carried"}
    assert out["assignments"][0] == {"name": "W19-a-design", "assignment_id": "a-design", "claude_sid": "sid-d",
                                     "liveness": "active", "model_requests": None, "branch": None, "role": None,
                                     "context_role": None, "model": None}
    assert [e["type"] for e in out["events"]][0] == "pipeline_started"


def test_pipeline_status_shows_each_sessions_model_request_count(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch)
    _seed_snapshot("W19", tmp_path)
    _holder("W19", "sid-d", "a-design")
    live = state.read_json(paths.ACTIVE / "sid-d.json")
    state.write_json_atomic(paths.ACTIVE / "sid-d.json",
                            {**live, "spend": {"turns": 3, "out_tokens": 9, "model_requests": 122}})
    for sid, assignment_id, spawned_at, spend in (
            ("sid-p", "a-plan", 2.0, {"turns": 2, "out_tokens": 5, "model_requests": 71}),
            ("sid-o", "a-old", 3.0, {"turns": 2, "out_tokens": 5})):
        name = f"W19-{assignment_id}"
        state.write_json_atomic(paths.assignment_path(sid), {
            "assignment_id": assignment_id, "claude_sid": sid, "ticket": "W19", "name": name,
            "spawned_at": spawned_at})
        state.write_json_atomic(paths.CLOSED / f"{sid}.json", {"claude_sid": sid, "name": name, "spend": spend})
    board = ms.pipeline_status_impl("W19").split("## assignments")[1].split("## events")[0]
    rows = {line.split()[1]: line for line in board.splitlines() if line.startswith("- ")}
    assert "  model_requests=122  " in rows["W19-a-design"]
    assert "  model_requests=71  " in rows["W19-a-plan"]
    assert "  model_requests=?  " in rows["W19-a-old"]
    assert {a["name"]: a["model_requests"] for a in _status("W19")["assignments"]} == {
        "W19-a-design": 122, "W19-a-plan": 71, "W19-a-old": None}


def test_an_unknown_status_format_is_refused(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="unknown format 'yaml'"):
        ms.pipeline_status_impl("W20", "yaml")


def test_the_pipeline_status_tool_passes_its_format(fresh_orchestrator_dir, monkeypatch):
    _probes(monkeypatch)
    assert json.loads(ms.pipeline_status(task_key="W21", format="json"))["task_key"] == "W21"
    assert ms.pipeline_status(ticket="W21").startswith("# pipeline_status(W21)\nwaiting_on: none\n")


def test_the_cli_prints_the_status_as_json_or_text(fresh_orchestrator_dir, tmp_path, monkeypatch, capsys):
    _probes(monkeypatch)
    _seed_snapshot("W22", tmp_path)
    _holder("W22", "sid-d", "a-design")
    assert pipeline.main(["status", "W22", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["waiting_on"] == "worker:design"
    assert pipeline.main(["status", "W22"]) == 0
    assert "waiting_on: worker:design" in capsys.readouterr().out
    assert pipeline.main(["status", "  "]) == 2


def test_the_cli_status_leaves_stale_assignments_and_the_tool_prunes_them(fresh_orchestrator_dir, tmp_path, monkeypatch, capsys):
    _probes(monkeypatch)
    _seed_snapshot("W23", tmp_path)
    paths.ASSIGNMENTS_PENDING.mkdir(parents=True, exist_ok=True)
    stale = paths.ASSIGNMENTS_PENDING / "old.json"
    stale.write_text("{}")
    old = time.time() - paths.PENDING_ASSIGNMENT_TTL_SEC - 60
    os.utime(stale, (old, old))
    assert pipeline.main(["status", "W23", "--json"]) == 0
    capsys.readouterr()
    assert stale.exists()
    ms.pipeline_status(task_key="W23")
    assert not stale.exists()


def test_the_cli_status_leaves_stale_works_and_the_tool_prunes_them(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                     capsys):
    _probes(monkeypatch)
    _seed_snapshot("W24", tmp_path)
    _holder("W24", "sid-d", "a-design")
    old = time.time() - paths.ARTIFACT_RETENTION_DAYS * 86400 - 60
    for key in ("OLD-1", "OLD-2"):
        artifact_put_impl(key, "spec", "a", "x", "complete", "sid-o")
        for p in paths.artifact_ticket_dir(key).rglob("*"):
            os.utime(p, (old, old))
    assert pipeline.main(["status", "W24", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["waiting_on"] == "worker:design"
    assert pipeline.main(["status", "OLD-2", "--json"]) == 0
    assert [a["name"] for a in json.loads(capsys.readouterr().out)["artifacts"]] == ["a"]
    assert paths.artifact_ticket_dir("OLD-1").is_dir() and paths.artifact_ticket_dir("OLD-2").is_dir()
    ms.pipeline_status(task_key="W24")
    assert not paths.artifact_ticket_dir("OLD-1").exists() and not paths.artifact_ticket_dir("OLD-2").exists()


def test_a_processing_holder_quiet_on_a_limit_banner_reads_time(fresh_orchestrator_dir, tmp_path, monkeypatch):
    log = _transcript(tmp_path, "You've hit your session limit · resets 3am (Etc/UTC)",
                      quiet_sec=ms.RATE_LIMIT_NUDGE_SEC + 60)
    _probes(monkeypatch, screen="", log_file=log)
    _seed_snapshot("W25", tmp_path)
    _holder("W25", "sid-d", "a-design", state_="processing")
    out = _status("W25")
    assert out["waiting_on"] == "external:time" and out["probe_errors"] == []
    os.utime(log, None)
    assert _status("W25")["waiting_on"] == "worker:design"


def test_a_message_quoting_the_limit_signature_is_no_banner(fresh_orchestrator_dir, tmp_path, monkeypatch):
    log = _transcript(tmp_path, "Checked the stale lane: a worker that says it hit your session limit stays busy.")
    _probes(monkeypatch, log_file=log)
    _seed_snapshot("W26", tmp_path)
    _holder("W26", "sid-d", "a-design")
    assert _status("W26")["waiting_on"] == "worker:design"
    _transcript(tmp_path, "You've hit your session limit · resets 3am (Etc/UTC)")
    assert _status("W26")["waiting_on"] == "external:time"


def test_a_session_whose_process_died_reads_dead_in_text_and_json(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch, commands={_LIVE: _CLAUDE, _LIVE + 2: "/usr/bin/python3 -m http.server"})
    _seed_snapshot("W27", tmp_path)
    _holder("W27", "sid-dead", "a-design", pid=_LIVE + 1)
    _holder("W28", "sid-othr", "a-w28", pid=_LIVE + 2)
    _holder("W29", "sid-live", "a-w29")
    for key, sid in (("W27", "sid-dead"), ("W28", "sid-othr"), ("W29", "sid-live")):
        artifact_put_impl(key, "spec", "a", "x", "complete", sid)
    out = _status("W27")
    assert out["assignments"][0]["liveness"] == "dead" and out["artifacts"][0]["writer_liveness"] == "dead"
    text = ms.pipeline_status_impl("W27")
    assert "writer=sid-dead(dead)" in text and "sid=sid-dead(dead)" in text and "(active)" not in text
    assert _status("W28")["artifacts"][0]["writer_liveness"] == "dead"
    assert _status("W29")["artifacts"][0]["writer_liveness"] == "active"


def test_waiting_on_status_probes_ps_once_unless_given_an_index(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _probes(monkeypatch)
    probe = ms._process_index
    calls = []
    monkeypatch.setattr(ms, "_process_index", lambda: calls.append(1) or probe())
    _seed_snapshot("W40", tmp_path)
    _holder("W40", "sid-d", "a-design")
    snap, events = ms._load_snapshot_for("W40"), ms._read_events(paths.artifact_events_path("W40"))
    assert ms._waiting_on_status("W40", snap, events, {}, time.time())["waiting_on"] == "worker:design"
    assert calls == [1]
    given = ms._waiting_on_status("W40", snap, events, {}, time.time(), index=probe())
    assert calls == [1] and given["waiting_on"] == "worker:design"
    failed = ms._waiting_on_status("W40", snap, events, {}, time.time(), index=None)
    assert calls == [1] and failed["probe_errors"] == ["ps"]


def test_spawn_worker_tab_keeps_a_prompt_far_past_the_tmux_limit_out_of_the_tmux_command(monkeypatch):
    terminal._DRIVER = None
    captured = _patch_exec(monkeypatch)
    prompt = "P" * 100_000
    _asyncio.run(spawner.spawn_worker_tab(extra_args=FLAGS, cwd="/tmp/x", initial_prompt=prompt, name="alpha"))
    tmux_command = list(captured["args"])
    assert sum(len(a.encode()) + 1 for a in tmux_command) < 8_000
    prompt_file = next(paths.PROMPT_FILES.glob("*.txt"))
    assert prompt_file.read_text() == prompt
    assert f'"$(cat {shlex.quote(str(prompt_file))})"' in tmux_command[-1]


from dockwright.mcp_server import get_worker_pane_impl, answer_permission_prompt_impl, set_human_attached_impl

PANE_TALL_DIALOG = """\
⏺ Bash(cp a b)
╭──────────────────────────────────────────────────────────────╮
│ Bash command                                                 │
│                                                              │
│ ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌ │
│ cp a b                                                       │
│ ╌╌╌╌╌╌╌╌╌╌╌╌╌╌ Jump to bottom (click) ↓ ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌ │


"""

CREATE_FILE_DIALOG = """\
⏺ Write(/r/x.md)
╭──────────────────────────────────────────────────────────────╮
│ Create file                                                  │
│ ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌ │
│ /r/x.md                                                      │
│ ╌╌╌╌╌╌╌╌╌╌╌╌╌╌ Jump to bottom (click) ↓ ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌ │
"""

SELF_EDIT_DIALOG = CREATE_FILE_DIALOG + "│   2. Yes, and allow Claude to edit files in its ~/.claude folder for this session\n"

PANE_PROCEED_DIALOG = """\
⏺ Bash(git commit -m x)
╭──────────────────────────────────────────────────────────────╮
│ Bash command                                                 │
│   git commit -m x                                            │
│ Do you want to proceed?                                      │
│ ❯ 1. Yes                                                     │
│   3. No, and tell Claude what to do differently (esc)        │
╰──────────────────────────────────────────────────────────────╯"""


class PromptPane:
    def __init__(self, screen, after_key, on_key=None):
        self.screen = screen
        self.after_key = after_key
        self.on_key = on_key
        self.calls = []

    def capture_screen(self, pane):
        return self.screen

    def capture_screen_ansi(self, pane):
        return self.screen

    def send_key(self, pane, key):
        self.calls.append(("key", pane, key))
        self.screen = self.after_key
        if self.on_key is not None:
            self.on_key()
        return True


def _pane_iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _prompt_worker(tmp_path, monkeypatch, pane, *, marker=True, tool_name="Bash", summary="cp a b", at=None,
                   window="%7"):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    monkeypatch.setattr(ms, "get_driver", lambda: pane)
    monkeypatch.setattr(ms, "_resolve_live_worker_window", lambda r: window)
    monkeypatch.setattr(ms, "SAFEGUARD_SETTLE_STEP_SEC", 0)
    at = time.time() - 30 if at is None else at
    if not marker:
        return at
    log = tmp_path / "w1-transcript.jsonl"
    log.write_text(json.dumps({"type": "assistant", "timestamp": _pane_iso(at), "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": tool_name,
         "input": {"command": summary} if tool_name == "Bash" else {"file_path": summary}}]}}) + "\n")
    _write_prompt_marker(at, tool_name, summary, str(log))
    return at


def _write_prompt_marker(at, tool_name="Bash", summary="cp a b", transcript_path=None, tool_use_id="toolu_1"):
    paths.PERMISSION_PROMPTS.mkdir(parents=True, exist_ok=True)
    (paths.PERMISSION_PROMPTS / "w1.json").write_text(json.dumps({
        "at": at, "sid": "w1", "name": "alpha", "tool_name": tool_name, "summary": summary,
        "transcript_path": transcript_path, "cwd": "/x", "decided": None, "attempts": [],
        "tool_use_id": tool_use_id, "closed_at": None}))


def _prompt_marker():
    return json.loads((paths.PERMISSION_PROMPTS / "w1.json").read_text())


def _last_ledger_line():
    return json.loads(paths.PERMISSION_LEDGER.read_text().splitlines()[-1])


def test_get_worker_pane_returns_the_text_the_signs_and_the_open_marker(fresh_orchestrator_dir, tmp_path, monkeypatch):
    at = _prompt_worker(tmp_path, monkeypatch, PromptPane(PANE_TALL_DIALOG, ""))
    out = get_worker_pane_impl("alpha")
    assert out == {"worker": "alpha", "pane": "%7", "text": PANE_TALL_DIALOG.rstrip(), "classic_dialog": False,
                   "dialog_sign": True, "prompt_at": at,
                   "prompt": {"tool_name": "Bash", "summary": "cp a b", "at": at, "state": "open",
                              "tool_use_id": "toolu_1", "attempts": [], "decided": None},
                   "human_attached": False}


def test_get_worker_pane_without_a_marker_reads_prompt_null(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _prompt_worker(tmp_path, monkeypatch, PromptPane("⏺ done\n", ""), marker=False)
    out = get_worker_pane_impl("alpha")
    assert out["prompt"] is None and out["prompt_at"] is None
    assert out["dialog_sign"] is False and out["classic_dialog"] is False


def test_get_worker_pane_finds_a_pending_spawn_by_name(fresh_orchestrator_dir, tmp_path, monkeypatch):
    pane = PromptPane(PANE_PROCEED_DIALOG, "")
    monkeypatch.setattr(ms, "get_driver", lambda: pane)
    paths.ASSIGNMENTS_PENDING.mkdir(parents=True, exist_ok=True)
    (paths.ASSIGNMENTS_PENDING / "aaff01.window").write_text("%9")
    (paths.ASSIGNMENTS_PENDING / "aaff01.json").write_text(json.dumps({"name": "boot-x", "parent_manager_name": "mgr"}))
    out = get_worker_pane_impl("boot-x")
    assert out["worker"] == "boot-x" and out["pane"] == "%9" and out["classic_dialog"] is True
    assert out["prompt"] is None and out["human_attached"] is False


def test_get_worker_pane_raises_for_an_unknown_name(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "get_driver", lambda: PromptPane("", ""))
    with pytest.raises(ValueError, match="no worker named 'ghost'"):
        get_worker_pane_impl("ghost")


def test_get_worker_pane_raises_when_no_live_window_resolves(fresh_orchestrator_dir, tmp_path, monkeypatch):
    pane = PromptPane(PANE_TALL_DIALOG, "")
    _prompt_worker(tmp_path, monkeypatch, pane, window="")
    monkeypatch.setattr(ms, "_WINDOW_RESOLVE_RETRY_SLEEP", 0)
    with pytest.raises(ValueError, match="no live window"):
        get_worker_pane_impl("alpha")
    with pytest.raises(ValueError, match="no live window"):
        answer_permission_prompt_impl("alpha", "approve")
    assert pane.calls == []


def test_get_worker_pane_skips_a_pending_spawn_with_an_empty_window_sidecar(fresh_orchestrator_dir, tmp_path,
                                                                            monkeypatch):
    monkeypatch.setattr(ms, "get_driver", lambda: PromptPane("", ""))
    paths.ASSIGNMENTS_PENDING.mkdir(parents=True, exist_ok=True)
    (paths.ASSIGNMENTS_PENDING / "aaff01.window").write_text("")
    (paths.ASSIGNMENTS_PENDING / "aaff01.json").write_text(json.dumps({"name": "boot-x"}))
    with pytest.raises(ValueError):
        get_worker_pane_impl("boot-x")


def test_get_worker_pane_trims_to_the_requested_lines(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _prompt_worker(tmp_path, monkeypatch, PromptPane(PANE_TALL_DIALOG, ""))
    out = get_worker_pane_impl("alpha", lines=3)
    assert out["text"] == "\n".join(PANE_TALL_DIALOG.rstrip().splitlines()[-3:])


def test_answer_approve_presses_1_when_the_dialog_sign_is_on_screen(fresh_orchestrator_dir, tmp_path, monkeypatch):
    pane = PromptPane(CREATE_FILE_DIALOG, "⏺ Write(/r/x.md)\n")
    at = _prompt_worker(tmp_path, monkeypatch, pane, tool_name="Write", summary="/r/x.md")
    out = answer_permission_prompt_impl("alpha", "approve", prompt_at=at, manager_sid="m1")
    assert pane.calls == [("key", "%7", "1")]
    assert out == {"status": "answered", "worker": "alpha", "tool_name": "Write", "summary": "/r/x.md"}
    marker = _prompt_marker()
    assert marker["decided"]["by"] == "manager" and marker["decided"]["answer"] == "approve"
    assert marker["decided"]["manager_sid"] == "m1"
    line = _last_ledger_line()
    assert line["answer"] == "approve" and line["by"] == "manager" and line["worker"] == "alpha"
    assert line["sid"] == "w1" and line["manager_sid"] == "m1" and "outcome" not in line


def test_answer_refuse_presses_escape(fresh_orchestrator_dir, tmp_path, monkeypatch):
    pane = PromptPane(CREATE_FILE_DIALOG, "")
    at = _prompt_worker(tmp_path, monkeypatch, pane, tool_name="Write", summary="/r/x.md")
    out = answer_permission_prompt_impl("alpha", "refuse", prompt_at=at)
    assert pane.calls == [("key", "%7", "Escape")] and out["status"] == "answered"
    assert _prompt_marker()["decided"]["answer"] == "refuse"


def test_answer_rejects_an_unknown_answer(fresh_orchestrator_dir, tmp_path, monkeypatch):
    pane = PromptPane(CREATE_FILE_DIALOG, "")
    _prompt_worker(tmp_path, monkeypatch, pane)
    with pytest.raises(ValueError):
        answer_permission_prompt_impl("alpha", "yes")
    assert pane.calls == []


def test_answer_without_a_marker_or_a_classic_block_presses_nothing(fresh_orchestrator_dir, tmp_path, monkeypatch):
    pane = PromptPane("⏺ done\n", "")
    _prompt_worker(tmp_path, monkeypatch, pane, marker=False)
    out = answer_permission_prompt_impl("alpha", "approve")
    assert out == {"status": "no_dialog", "worker": "alpha", "tool_name": None, "summary": None, "prompt": None}
    assert pane.calls == []


def test_answer_with_an_open_marker_but_no_sign_on_screen_presses_nothing(fresh_orchestrator_dir, tmp_path,
                                                                           monkeypatch):
    pane = PromptPane("\n" * 20, "")
    at = _prompt_worker(tmp_path, monkeypatch, pane)
    out = answer_permission_prompt_impl("alpha", "approve", prompt_at=at)
    assert out["status"] == "no_dialog" and out["tool_name"] == "Bash" and out["prompt"]["state"] == "open"
    assert pane.calls == []


def test_answer_refuses_while_a_human_is_attached(fresh_orchestrator_dir, tmp_path, monkeypatch):
    pane = PromptPane(CREATE_FILE_DIALOG, "")
    at = _prompt_worker(tmp_path, monkeypatch, pane)
    set_human_attached_impl("alpha", True)
    out = answer_permission_prompt_impl("alpha", "approve", prompt_at=at)
    assert out["status"] == "human_attached" and pane.calls == []


def test_answer_approve_session_needs_the_self_edit_option_text(fresh_orchestrator_dir, tmp_path, monkeypatch):
    pane = PromptPane(CREATE_FILE_DIALOG, "")
    at = _prompt_worker(tmp_path, monkeypatch, pane, tool_name="Write", summary="/r/x.md")
    out = answer_permission_prompt_impl("alpha", "approve_session", prompt_at=at)
    assert out["status"] == "wrong_dialog" and pane.calls == []
    pane.screen = SELF_EDIT_DIALOG
    out = answer_permission_prompt_impl("alpha", "approve_session", prompt_at=at)
    assert out["status"] == "answered" and pane.calls == [("key", "%7", "2")]


def test_answer_unsettled_when_the_sign_stays_leaves_decided_empty_and_records_the_attempt(fresh_orchestrator_dir,
                                                                                           tmp_path, monkeypatch):
    pane = PromptPane(CREATE_FILE_DIALOG, CREATE_FILE_DIALOG)
    at = _prompt_worker(tmp_path, monkeypatch, pane, tool_name="Write", summary="/r/x.md")
    out = answer_permission_prompt_impl("alpha", "approve", prompt_at=at)
    assert out["status"] == "unsettled" and pane.calls == [("key", "%7", "1")]
    marker = _prompt_marker()
    assert marker["decided"] is None and marker["attempts"][0]["answer"] == "approve"
    assert _last_ledger_line()["outcome"] == "unsettled"


def test_answer_does_not_overwrite_a_newer_marker_written_during_the_settle(fresh_orchestrator_dir, tmp_path,
                                                                             monkeypatch):
    pane = PromptPane(CREATE_FILE_DIALOG, CREATE_FILE_DIALOG)
    at = _prompt_worker(tmp_path, monkeypatch, pane, tool_name="Write", summary="/r/x.md")
    pane.on_key = lambda: _write_prompt_marker(at + 1, "Write", "/r/y.md", str(tmp_path / "w1-transcript.jsonl"))
    out = answer_permission_prompt_impl("alpha", "approve", prompt_at=at)
    assert out["status"] == "answered" and out["summary"] == "/r/x.md"
    marker = _prompt_marker()
    assert marker["at"] == at + 1 and marker["decided"] is None and marker["attempts"] == []
    assert _last_ledger_line()["outcome"] == "superseded"


def test_answer_a_classic_dialog_of_a_pending_spawn_presses_1(fresh_orchestrator_dir, tmp_path, monkeypatch):
    pane = PromptPane(PANE_PROCEED_DIALOG, "")
    monkeypatch.setattr(ms, "get_driver", lambda: pane)
    monkeypatch.setattr(ms, "SAFEGUARD_SETTLE_STEP_SEC", 0)
    paths.ASSIGNMENTS_PENDING.mkdir(parents=True, exist_ok=True)
    (paths.ASSIGNMENTS_PENDING / "aaff01.window").write_text("%9")
    (paths.ASSIGNMENTS_PENDING / "aaff01.json").write_text(json.dumps({"name": "boot-x"}))
    out = answer_permission_prompt_impl("boot-x", "approve")
    assert out == {"status": "answered", "worker": "boot-x", "tool_name": None, "summary": None}
    assert pane.calls == [("key", "%9", "1")]
    assert _last_ledger_line()["sid"] is None and _last_ledger_line()["worker"] == "boot-x"


def test_an_open_marker_on_a_processing_holder_reads_human_approval_dialog(fresh_orchestrator_dir, tmp_path,
                                                                           monkeypatch):
    _probes(monkeypatch, screen=PANE_TALL_DIALOG)
    _seed_snapshot("W13b", tmp_path)
    _holder("W13b", "sid-d", "a-design", state_="processing")
    at = time.time() - 30
    log = tmp_path / "sid-d.jsonl"
    log.write_text(json.dumps({"type": "assistant", "timestamp": _pane_iso(at), "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "cp a b"}}]}}) + "\n")
    paths.PERMISSION_PROMPTS.mkdir(parents=True, exist_ok=True)
    (paths.PERMISSION_PROMPTS / "sid-d.json").write_text(json.dumps({
        "at": at, "sid": "sid-d", "name": "W13b-a-design", "tool_name": "Bash", "summary": "cp a b",
        "transcript_path": str(log), "cwd": "/x", "decided": None, "attempts": [], "tool_use_id": "toolu_1",
        "closed_at": None}))
    assert _status("W13b")["waiting_on"] == "human:approval-dialog"
    _probes(monkeypatch, screen="\n" * 20)
    assert _status("W13b")["waiting_on"] == "worker:design"
    _probes(monkeypatch, screen=None)
    assert _status("W13b")["waiting_on"] == "worker:design"


def test_answer_refuses_a_prompt_at_that_is_not_the_open_markers(fresh_orchestrator_dir, tmp_path, monkeypatch):
    pane = PromptPane(CREATE_FILE_DIALOG, "")
    at = _prompt_worker(tmp_path, monkeypatch, pane, tool_name="Write", summary="/r/x.md")
    out = answer_permission_prompt_impl("alpha", "approve", prompt_at=at - 1)
    assert out["status"] == "changed" and out["prompt_at"] == at and out["prompt"]["at"] == at and pane.calls == []
    out = answer_permission_prompt_impl("alpha", "approve")
    assert out["status"] == "changed" and pane.calls == []


def test_answer_refuses_a_prompt_at_when_no_marker_is_open(fresh_orchestrator_dir, tmp_path, monkeypatch):
    pane = PromptPane(PANE_PROCEED_DIALOG, "")
    _prompt_worker(tmp_path, monkeypatch, pane, marker=False)
    out = answer_permission_prompt_impl("alpha", "approve", prompt_at=123.0)
    assert out["status"] == "changed" and out["prompt_at"] is None and pane.calls == []


def test_answer_a_classic_dialog_that_stays_reads_unsettled(fresh_orchestrator_dir, tmp_path, monkeypatch):
    pane = PromptPane(PANE_PROCEED_DIALOG, PANE_PROCEED_DIALOG)
    monkeypatch.setattr(ms, "get_driver", lambda: pane)
    monkeypatch.setattr(ms, "SAFEGUARD_SETTLE_STEP_SEC", 0)
    paths.ASSIGNMENTS_PENDING.mkdir(parents=True, exist_ok=True)
    (paths.ASSIGNMENTS_PENDING / "aaff01.window").write_text("%9")
    (paths.ASSIGNMENTS_PENDING / "aaff01.json").write_text(json.dumps({"name": "boot-x"}))
    out = answer_permission_prompt_impl("boot-x", "approve")
    assert out["status"] == "unsettled" and pane.calls == [("key", "%9", "1")]
    assert _last_ledger_line()["outcome"] == "unsettled"


def test_answer_on_a_classic_dialog_beside_a_closed_marker_records_no_stale_command(fresh_orchestrator_dir, tmp_path,
                                                                                    monkeypatch):
    pane = PromptPane(PANE_PROCEED_DIALOG, "")
    at = _prompt_worker(tmp_path, monkeypatch, pane, tool_name="Write", summary="/r/old.md")
    marker = _prompt_marker(); marker["closed_at"] = at + 1
    (paths.PERMISSION_PROMPTS / "w1.json").write_text(json.dumps(marker))
    pane_view = get_worker_pane_impl("alpha")
    assert pane_view["prompt"]["state"] == "closed" and pane_view["prompt_at"] is None
    out = answer_permission_prompt_impl("alpha", "approve", prompt_at=pane_view["prompt_at"])
    assert out == {"status": "answered", "worker": "alpha", "tool_name": None, "summary": None}
    line = _last_ledger_line()
    assert line["tool_name"] is None and line["summary"] is None


def test_answer_writes_a_pressed_line_before_the_keypress_and_the_outcome_after(fresh_orchestrator_dir, tmp_path,
                                                                                 monkeypatch):
    pane = PromptPane(CREATE_FILE_DIALOG, "")
    seen = []
    pane.on_key = lambda: seen.append(paths.PERMISSION_LEDGER.read_text().splitlines())
    at = _prompt_worker(tmp_path, monkeypatch, pane, tool_name="Write", summary="/r/x.md")
    answer_permission_prompt_impl("alpha", "approve", prompt_at=at, manager_sid="m1")
    assert len(seen[0]) == 1 and json.loads(seen[0][0])["outcome"] == "pressed"
    lines = [json.loads(l) for l in paths.PERMISSION_LEDGER.read_text().splitlines()]
    assert [l.get("outcome") for l in lines] == ["pressed", None]
    assert lines[0]["summary"] == "/r/x.md" and lines[0]["answer"] == "approve" and lines[1]["answer"] == "approve"
