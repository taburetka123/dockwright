import inspect
import os
import subprocess
import time
import json as _json
import time as _time
from pathlib import Path
import pytest
from dockwright import paths, state
from dockwright import config as _config
from dockwright.mcp_server import register_self_impl, list_workers_impl

@pytest.fixture
def fresh_orchestrator_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(paths, "QUESTIONS", tmp_path / "questions")
    monkeypatch.setattr(paths, "ANSWERS", tmp_path / "answers")
    monkeypatch.setattr(paths, "DONE", tmp_path / "done")
    monkeypatch.setattr(paths, "CLOSED", tmp_path / "closed")
    monkeypatch.setattr(paths, "HANDOFFS", tmp_path / "handoffs")
    monkeypatch.setattr(paths, "PRESETS", tmp_path / "presets")
    monkeypatch.setattr(paths, "MANAGER_TRIGGERS_LOG", tmp_path / "manager-triggers.jsonl")
    monkeypatch.setattr(paths, "MANAGER_MEMORY", tmp_path / "manager-memory")
    monkeypatch.setattr(paths, "SLOTS", tmp_path / "slots")
    monkeypatch.setattr(paths, "ARTIFACTS", tmp_path / "artifacts")
    monkeypatch.setattr(paths, "ASSIGNMENTS", tmp_path / "assignments")
    monkeypatch.setattr(paths, "ASSIGNMENTS_PENDING", tmp_path / "assignments" / ".pending")
    paths.ensure_dirs()
    yield tmp_path

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

def test_register_self_duplicate_name_rejected(fresh_orchestrator_dir):
    register_self_impl(claude_sid="sid-1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    with pytest.raises(ValueError, match="name 'alpha' is taken"):
        register_self_impl(claude_sid="sid-2", agent="worker", name="alpha", cwd="/y", iterm_sid="i2")

def test_register_self_stamps_account_from_env(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT", "b")
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    record = state.read_json(paths.ACTIVE / "w1.json")
    assert record["account"] == "b"

def test_register_self_preserves_hook_stamp_when_env_absent(fresh_orchestrator_dir, monkeypatch):
    """The SessionStart hook stamps `account` on the active record (hooks.py);
    become_manager routes /manager boots through register_self_impl minutes
    later — the rebuilt record must keep the stamp when the MCP server's env
    doesn't carry CLAUDE_ORCH_ACCOUNT."""
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

def test_register_self_account_none_without_env_or_prior_record(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_ACCOUNT", raising=False)
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    assert state.read_json(paths.ACTIVE / "w1.json")["account"] is None
    # An invalid letter is not a stamp either.
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
    # Write two questions directly via the helper (not blocking)
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


# Append to tests/test_mcp_tools.py
from dockwright.mcp_server import (
    send_manager_to_worker_impl, kill_worker_impl, attach_existing_impl,
)
from dockwright import paths as paths_module

def test_send_manager_to_worker_types_content(fresh_orchestrator_dir, monkeypatch):
    """Happy path: type the message CONTENT directly into the worker's window
    (direct delivery — tmux buffers it if the worker is mid-turn)."""
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="42")
    typed = []
    monkeypatch.setattr(
        "dockwright.mcp_server._send_text",
        lambda wid, text: typed.append((wid, text)),
    )
    result = send_manager_to_worker_impl(worker="alpha", text="also check Y")
    assert result["status"] == "delivered" and result["worker"] == "alpha"
    assert typed == [("42", "[MANAGER] also check Y")]

def test_send_manager_to_worker_marker_prepends_once_multiline(fresh_orchestrator_dir, monkeypatch):
    """The marker rides INSIDE the paste body: prefix on the first line only,
    later lines untouched (bracketed-paste + single-Enter mechanics unchanged)."""
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="42")
    typed = []
    monkeypatch.setattr(
        "dockwright.mcp_server._send_text",
        lambda wid, text: typed.append(text),
    )
    send_manager_to_worker_impl(worker="alpha", text="line one\nline two")
    assert typed == ["[MANAGER] line one\nline two"]
    assert typed[0].count("[MANAGER] ") == 1

def test_send_manager_to_worker_unknown_worker(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="no worker named 'ghost'"):
        send_manager_to_worker_impl(worker="ghost", text="hi")

def test_send_manager_to_worker_resolves_via_terminal_ls_when_id_empty(fresh_orchestrator_dir, monkeypatch):
    """A3: persisted window_id empty → match a live window by cwd+runtime,
    stamp it back, deliver."""
    import dockwright.mcp_server as srv
    state.write_json_atomic(paths.ACTIVE / "w1.json", {
        "claude_sid": "w1", "agent": "worker", "name": "alpha",
        "cwd": "/tmp/wt", "window_id": "", "runtime": "claude"})
    monkeypatch.setattr(srv, "_terminal_ls", lambda: [
        {"tabs": [{"windows": [
            {"id": 555, "cwd": "/tmp/wt",
             "foreground_processes": [{"cmdline": ["node", "/x/claude", "--resume"]}]}]}]}])
    sent = {}
    monkeypatch.setattr(srv, "_send_text", lambda wid, txt: sent.update(wid=wid, txt=txt))
    monkeypatch.setattr(srv, "_WINDOW_RESOLVE_RETRY_SLEEP", 0)
    result = srv.send_manager_to_worker_impl("alpha", "hi")
    assert result["status"] == "delivered" and sent["wid"] == "555"
    assert state.read_json(paths.ACTIVE / "w1.json")["window_id"] == "555"  # stamped back

def test_send_manager_to_worker_persisted_id_confirmed_live(fresh_orchestrator_dir, monkeypatch):
    """A3: persisted window_id appears live in `tmux list-panes` → use it as-is (early
    return), never enter the cwd-match path — even when its cwd differs."""
    import dockwright.mcp_server as srv
    state.write_json_atomic(paths.ACTIVE / "w1.json", {
        "claude_sid": "w1", "agent": "worker", "name": "alpha",
        "cwd": "/tmp/wt", "window_id": "555", "runtime": "claude"})
    monkeypatch.setattr(srv, "_terminal_ls", lambda: [
        {"tabs": [{"windows": [
            {"id": 555, "cwd": "/somewhere/else", "foreground_processes": []}]}]}])
    sent = {}
    monkeypatch.setattr(srv, "_send_text", lambda wid, txt: sent.update(wid=wid))
    result = srv.send_manager_to_worker_impl("alpha", "hi")
    assert result["status"] == "delivered" and sent["wid"] == "555"

def test_send_manager_to_worker_no_live_window_raises_loud(fresh_orchestrator_dir, monkeypatch):
    """A3: genuinely no live window (retries exhausted) → raise, never a file."""
    import dockwright.mcp_server as srv
    state.write_json_atomic(paths.ACTIVE / "w1.json", {
        "claude_sid": "w1", "agent": "worker", "name": "alpha",
        "cwd": "/tmp/wt", "window_id": "", "runtime": "claude"})
    monkeypatch.setattr(srv, "_terminal_ls", lambda: [])   # no windows
    monkeypatch.setattr(srv, "_WINDOW_RESOLVE_RETRY_SLEEP", 0)
    with pytest.raises(ValueError, match="no live window"):
        srv.send_manager_to_worker_impl("alpha", "hi")

def test_send_manager_to_worker_ambiguous_cwd_match_raises(fresh_orchestrator_dir, monkeypatch):
    """A3: >1 cwd match → unresolvable, never guess → raise."""
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
    # Must not raise — direct typing is best-effort.
    result = send_manager_to_worker_impl(worker="alpha", text="hi")
    assert result["status"] == "delivered"

def test_kill_worker_marks_terminating(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=12345)
    # Use dry_run so we don't actually SIGTERM pid 12345
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
    assert "-" in result["name"]  # funny: <adj>-<animal>
    assert result["domain"] == "general"
    assert result["runtime"] == "claude"
    record = state.read_json(paths.ACTIVE / "mgr-1.json")
    assert record["agent"] == "manager"
    assert record["name"] == result["name"]
    assert record["domain"] == "general"
    assert record["runtime"] == "claude"


def test_become_manager_records_claude_runtime_and_list_managers_exposes_it(fresh_orchestrator_dir):
    # Managers are Claude-only; the record/list always reports runtime="claude".
    result = become_manager_impl(claude_sid="mgr-1", iterm_sid="i9")
    assert result["runtime"] == "claude"
    record = state.read_json(paths.ACTIVE / "mgr-1.json")
    assert record["runtime"] == "claude"

    from dockwright.mcp_server import list_managers
    managers = list_managers()
    assert managers[0]["claude_sid"] == "mgr-1"
    assert managers[0]["runtime"] == "claude"

# --- PID-test reaper ordering ----------------------------------------------
# register_self_impl / become_manager_impl run two prunes in order:
#   1. _prune_stale_active_records() — the dead-PID reaper. ANY record whose pid
#      is an int that _pid_alive() reports as dead is dropped. Synthetic test pids
#      (e.g. 99999999) are dead, so they get reaped HERE, before step 2.
#   2. _prune_same_pid_ghosts() — drops live-pid records under a different sid.
# Consequence for tests: to exercise the SAME-PID ghost logic, the pid must look
# alive — either patch `_pid_alive` -> True (so the synthetic pid survives step 1)
# or use a real live pid (os.getpid()). A bare synthetic pid never reaches step 2;
# it's already gone. Tests below that need step 2 patch _pid_alive accordingly.
# _prune_stale_active_records now lives in registry.py with its own _pid_alive
# binding — patch BOTH "dockwright.mcp_server._pid_alive" AND
# "dockwright.registry._pid_alive" (see the tests below).
def test_prune_stale_active_records_keeps_non_positive_pid(fresh_orchestrator_dir):
    """A pid of 0 / negative can't prove the session dead (_pid_alive returns
    False for non-positive pids without ever signalling) — such records must be
    kept, not reaped. Mirrors preflight_cleanup._prune_active's guard."""
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
    """os.kill raises OverflowError (not OSError) above the C int range — a poisoned
    record must not traceback the reaper (it runs on become_manager / spawn_worker /
    register_self paths fleet-wide) and must be kept, not reaped."""
    from dockwright.mcp_server import _prune_stale_active_records
    state.write_json_atomic(paths.ACTIVE / "sid-huge.json", {
        "claude_sid": "sid-huge", "agent": "manager", "name": "huge-golem", "pid": 2**31,
    })

    _prune_stale_active_records()

    assert (paths.ACTIVE / "sid-huge.json").exists()


def test_prune_stale_active_records_ledgers_spend(fresh_orchestrator_dir, monkeypatch):
    """Dead-pid prune appends the record's spend to the ledger before unlinking."""
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
    """Multi-manager: a second manager session is permitted (different name + domain).

    In production each manager runs in its own tmux window = its own Claude Code
    process = its own pid, so the same-pid ghost prune never touches the other.
    The test process shares one pid, so we patch os.getppid to two distinct
    values to model two windows (otherwise the prune would wrongly drop mgr-1).
    _pid_alive is patched True so the distinct fake pids aren't reaped as stale.
    """
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
    # Both records survive — distinct pids, no false-positive prune.
    assert state.read_json(paths.ACTIVE / "mgr-1.json") is not None
    assert state.read_json(paths.ACTIVE / "mgr-2.json") is not None


def test_become_manager_prunes_same_pid_ghost(fresh_orchestrator_dir, monkeypatch):
    """Ghost-record bug: /manager calls become_manager with a placeholder sid before
    the real sid is in context. The SessionStart placeholder has name="manager";
    the real become_manager call must prune that same-window placeholder, leaving
    only the real one.

    _pid_alive is patched True so the ghost is NOT reaped as a dead-pid stale
    record — that forces the new same-pid prune (not stale-pruning) to be what
    removes it, matching production where the pid is the very live session.
    """
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
    # Only the real record survives; the placeholder ghost is gone.
    assert state.read_json(paths.ACTIVE / "manager-session.json") is None
    assert state.read_json(paths.ACTIVE / "mgr-real.json") is not None
    managers = [r for r in state.list_json_in(paths.ACTIVE) if r.get("agent") == "manager"]
    assert len(managers) == 1
    assert managers[0]["claude_sid"] == "mgr-real"


def test_become_manager_prunes_funny_named_bootstrap_ghost(fresh_orchestrator_dir, monkeypatch):
    """Regression for the funny-name SessionStart change: the bootstrap placeholder is
    now registered under a funny <adjective>-<animal> name, not the literal "manager".
    The same-window/same-pid placeholder under a different sid must still be pruned so
    no stale duplicate manager survives to make resolve_manager() window/pid-ambiguous.
    """
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.mcp_server.os.getppid", lambda: 4242)
    state.write_json_atomic(paths.ACTIVE / "manager-session.json", {
        "claude_sid": "manager-session",
        "agent": "manager",
        "name": "snug-ibex",  # funny name, as SessionStart now writes
        "window_id": "i9",
        "pid": 4242,
    })
    real = become_manager_impl(claude_sid="mgr-real", iterm_sid="i9", domain="general")
    assert real["ok"] is True
    # The funny-named placeholder ghost is gone; only the real record survives.
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
    """/manager-reboot re-registers post-/clear under the SAME name via the MCP
    tool wrapper (the impl always took `name`; the wrapper didn't forward it —
    that gap re-rolled a fresh name and broke event-bucket routing). The
    pre-clear record shares this process's pid, so the same-pid prune must
    drop it and the kept name must come back un-suffixed."""
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
    assert result["name"] == "kept-fox"  # preserved, NOT auto-suffixed
    assert state.read_json(paths.ACTIVE / "mgr-pre-clear.json") is None
    record = state.read_json(paths.ACTIVE / "mgr-post-clear.json")
    assert record["name"] == "kept-fox"
    managers = [r for r in state.list_json_in(paths.ACTIVE) if r.get("agent") == "manager"]
    assert len(managers) == 1


def test_become_manager_tool_without_name_still_rolls_funny_name(fresh_orchestrator_dir, monkeypatch):
    """None default = auto-roll, exactly as before the param existed."""
    from dockwright.mcp_server import become_manager

    monkeypatch.setattr("dockwright.mcp_server.os.getppid", lambda: 4242)
    result = become_manager(claude_sid="mgr-1", iterm_sid="i9")
    assert result["ok"] is True
    assert "-" in result["name"]


def test_become_manager_tool_suffixes_name_taken_by_different_live_session(fresh_orchestrator_dir, monkeypatch):
    """The docstring's auto-suffix claim: a passed name held by a DIFFERENT live
    session (different pid — not the same-tab ghost the prune drops) must come
    back suffixed instead of clobbering the peer."""
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
    assert state.read_json(paths.ACTIVE / "peer-mgr.json") is not None  # peer untouched


def test_prune_same_pid_ghosts_drops_same_window_placeholder_regardless_of_name(fresh_orchestrator_dir):
    """Helper contract: prune this tab's own stale manager identity structurally.

    The discriminator is the tmux window, NOT the name: a same-pid + same-window
    record under a different sid can only be a prior identity of the very session
    re-registering (SessionStart placeholder / two-phase become_manager first call),
    so it is pruned whether named "manager", "manager-2", or a funny name. A live
    peer manager lives in its own window, so the same-window guard spares it.
    """
    from dockwright.mcp_server import _prune_same_pid_ghosts
    # Write records directly so register_self_impl's dead-pid stale-pruning doesn't
    # reap the fake pids first — we want to exercise the helper in isolation.
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
    # Funny-named same-window placeholder — the case the funny-name SessionStart
    # change produces. The old literal-"manager" check missed this; the structural
    # check prunes it.
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
    # Legacy record with no pid (None) — must survive.
    state.write_json_atomic(paths.ACTIVE / "legacy.json", {"claude_sid": "legacy", "name": "delta", "pid": None})

    _prune_same_pid_ghosts(7777, keep_sid="keep", keep_window_id="current-window")

    assert state.read_json(paths.ACTIVE / "bootstrap.json") is None          # literal "manager" → pruned
    assert state.read_json(paths.ACTIVE / "bootstrap-suffixed.json") is None  # "manager-2" → pruned
    assert state.read_json(paths.ACTIVE / "funny-same-window.json") is None   # funny name, same window → pruned
    assert state.read_json(paths.ACTIVE / "live-peer.json") is not None       # different window → kept
    assert state.read_json(paths.ACTIVE / "keep.json") is not None            # caller's own sid → kept
    assert state.read_json(paths.ACTIVE / "other.json") is not None           # different pid → kept
    assert state.read_json(paths.ACTIVE / "legacy.json") is not None          # non-int pid → kept

def test_kill_worker_drops_pending_questions(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=12345)
    from dockwright.mcp_server import _write_question
    _write_question(worker_sid="w1", worker_name="alpha", question="q1")
    _write_question(worker_sid="w1", worker_name="alpha", question="q2")
    assert len(list(paths.QUESTIONS.iterdir())) == 2
    result = kill_worker_impl(worker="alpha", dry_run=True)
    # dry_run does not drop
    assert len(list(paths.QUESTIONS.iterdir())) == 2
    # Non-dry-run would drop, but we can't actually SIGTERM pid 12345 in test;
    # call the helper directly to verify cleanup logic
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
    # If we're re-resolving for the same session, our own name should be free
    assert _resolve_unique_name("alpha", excluding_sid="w1") == "alpha"

def test_ask_manager_recovers_from_corrupt_answer_file(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    # Corrupt answer appears first; ask_manager must remove it and keep polling
    # until a valid answer arrives.

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
    """Pins the fix for the event-loop-starvation wedge: a sync ask_manager
    blocks the worker's single-threaded MCP event loop; FastMCP's @tool()
    returns the function unchanged, so the module-level names must both be
    coroutine functions."""
    from dockwright import mcp_server
    assert inspect.iscoroutinefunction(mcp_server.ask_manager_impl)
    assert inspect.iscoroutinefunction(mcp_server.ask_manager)

def test_ask_manager_does_not_starve_event_loop(fresh_orchestrator_dir):
    """The incident: a pending ask_manager starved the worker's single-threaded
    MCP loop, so a later worker_done hung forever. The new impl must yield
    between polls so other tool calls are serviced while it waits."""
    # Guard FIRST: against a fully sync impl the async body below would HANG the
    # suite rather than fail — create_task(sync_call(...)) evaluates the blocking
    # call eagerly and a monopolized loop never runs wait_for's timer. This assert
    # turns that hang into an instant red. (It does not catch an async def that
    # blocks internally — only the sync-regression case.)
    assert inspect.iscoroutinefunction(ask_manager_impl)

    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    register_self_impl(claude_sid="w2", agent="worker", name="beta", cwd="/y", iterm_sid="i2")
    from dockwright.mcp_server import worker_done_impl

    async def run():
        ask = _asyncio.create_task(ask_manager_impl(claude_sid="w1", question="blocked?", poll_interval=0.02))
        # In a single-threaded loop, this sleep returning at all while the ask
        # task is alive proves the poll loop yields control.
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
    # The question survives the timeout — the manager still sees ONE stable
    # pending question (stale_monitor nudge-skip/autoclose key off it too).
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
        assert len(list_pending_questions_impl()) == 1  # no duplicate question
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
    assert len(list_pending_questions_impl()) == 1  # question untouched


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
    """Corrupt question record → the answer is still written, just without the
    worker_sid stamp (never block an answer on stamping)."""
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
    # The foreign answer must NOT be consumed — it belongs to the other worker.
    assert (paths.ANSWERS / f"{qid}.json").exists()


def test_ask_manager_resume_accepts_legacy_unstamped_answer(fresh_orchestrator_dir):
    """Manager and worker run separate server processes with independent restart
    times — an old manager server writes unstamped answers. Tolerate absence."""
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    qid = "q-legacy"
    state.write_json_atomic(paths.ANSWERS / f"{qid}.json", {
        "question_id": qid, "answer": "old-style", "answered_at": time.time(),
    })
    result = _asyncio.run(ask_manager_impl(
        claude_sid="w1", question="q?", poll_interval=0.01, resume_question_id=qid))
    assert result == "old-style"


def test_ask_manager_resume_toctou_recheck_finds_answer(fresh_orchestrator_dir, monkeypatch):
    """The resume path's final answer re-check: if the manager's answer lands
    between resume's first answer check and the question-file check, resume
    must return it — not raise the fail-fast ValueError."""
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    from dockwright import mcp_server
    from dockwright.mcp_server import _write_question
    qid = _write_question(worker_sid="w1", worker_name="alpha", question="q?")
    real = mcp_server._try_consume_answer
    calls = {"n": 0}

    def racy(q, sid):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate the TOCTOU window: the first answer check sees nothing;
            # the manager answers (write answer THEN unlink question) before
            # the question-file check runs.
            answer_question_impl(question_id=qid, text="landed mid-window")
            return None
        return real(q, sid)

    monkeypatch.setattr(mcp_server, "_try_consume_answer", racy)
    result = _asyncio.run(mcp_server.ask_manager_impl(
        claude_sid="w1", question="q?", poll_interval=0.01, resume_question_id=qid))
    assert result == "landed mid-window"


def test_register_self_name_collision_with_dead_pid_succeeds(fresh_orchestrator_dir):
    """A stale record (dead pid) holding the same name must be pruned, not block re-registration."""
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=99999999)
    result = register_self_impl(claude_sid="w2", agent="worker", name="alpha", cwd="/y", iterm_sid="i2", pid=os.getpid())
    assert result["ok"] is True
    assert not (paths.ACTIVE / "w1.json").exists()
    assert (paths.ACTIVE / "w2.json").exists()

def test_become_manager_stale_record_with_dead_pid_succeeds(fresh_orchestrator_dir):
    """Closing a manager tab (SIGHUP) leaves a stale active/<sid>.json. The next /manager must succeed."""
    register_self_impl(claude_sid="old-mgr", agent="manager", name="manager", cwd="/x", iterm_sid="i0", pid=99999999)
    result = become_manager_impl(claude_sid="new-mgr", iterm_sid="i1")
    assert result["ok"] is True
    assert not (paths.ACTIVE / "old-mgr.json").exists()
    assert (paths.ACTIVE / "new-mgr.json").exists()

def test_resolve_unique_name_skips_dead_records(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=99999999)
    assert _resolve_unique_name("alpha") == "alpha"

def test_resolve_unique_name_avoids_funny_name_collision(fresh_orchestrator_dir):
    """A caller-passed name colliding with an active record's funny_name would
    give two live sessions the same display handle — suffix it like a routing
    name collision."""
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


def test_list_workers_keeps_true_idle_as_idle(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    register_self_impl(claude_sid="w-idle", agent="worker", name="resting",
                       cwd="/x", iterm_sid="i1", pid=os.getpid())
    worker = next(w for w in list_workers_impl() if w["name"] == "resting")
    assert worker["state"] == "idle"
    assert "delegating" not in worker


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
    assert result["last_turn_at"] == "2026-05-18T10:00:00Z"
    assert result["alive"] is True

def test_get_worker_summary_missing_log(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    result = get_worker_summary_impl(worker="alpha")
    assert result["error"] == "transcript not found"
    assert result["summary"] is None
    assert result["last_turn_at"] is None

def test_get_worker_summary_unknown_worker(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="no worker named 'ghost'"):
        get_worker_summary_impl(worker="ghost")

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
    # last entry is index 99 (role=user, content=msg-99)
    assert result["entries"][-1]["role"] == "user"
    assert "msg-99" in result["entries"][-1]["content_preview"]
    # first returned entry is index 90
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
    # filename encodes both sid and event id
    assert done_files[0].name == f"w1-{result['event_id']}.json"
    # null-parent worker → written to the shared _unscoped bucket
    assert done_files[0].parent.name == paths.UNSCOPED_BUCKET

def test_worker_done_unknown_sid_rejected(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="session ghost-sid not registered"):
        worker_done_impl(claude_sid="ghost-sid", summary="done")
    # No file should have been written
    assert list(paths.DONE.rglob("*.json")) == []

def test_worker_done_multiple_events_for_same_worker(fresh_orchestrator_dir):
    """A worker may signal done more than once across multiple tasks in one session."""
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    r1 = worker_done_impl(claude_sid="w1", summary="task A done")
    r2 = worker_done_impl(claude_sid="w1", summary="task B done")
    assert r1["event_id"] != r2["event_id"]
    assert len(list(paths.DONE.rglob("*.json"))) == 2

def test_worker_done_scoped_to_parent_manager_subdir(fresh_orchestrator_dir):
    """A worker with a parent manager writes its done event into done/<manager>/."""
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x",
                       iterm_sid="i1", pid=os.getpid(), parent_manager_name="manager-a")
    worker_done_impl(claude_sid="w1", summary="scoped done")
    scoped = list((paths.DONE / "manager-a").glob("*.json"))
    assert len(scoped) == 1
    assert state.read_json(scoped[0])["summary"] == "scoped done"
    # nothing leaked into the unscoped bucket
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
    artifact_put_impl("TKT-SANDBOX-1", "plan", "repo", "body", "complete", "other-sid")  # foreign writer
    worker_done_impl(claude_sid="w1", summary="done")
    (event_path,) = list(paths.done_dir_for(None).glob("w1-*.json"))
    event = state.read_json(event_path)
    assert event["ticket"] == "TKT-SANDBOX-1"
    assert event["artifacts_published"] == 1            # own writes only


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
    # The ticket seed is what forces _published_count to reach artifact_list_impl —
    # without it the except branch would never be exercised.
    state.write_json_atomic(paths.ASSIGNMENTS / "w1.json", {"claude_sid": "w1", "ticket": "TKT-SANDBOX-1"})
    monkeypatch.setattr(_mcp, "artifact_list_impl",
                        lambda t: (_ for _ in ()).throw(RuntimeError("store down")))
    result = worker_done_impl(claude_sid="w1", summary="done")
    assert result["ok"] is True                          # done event survives a broken store


import signal
from dockwright.mcp_server import (
    prepare_handoff_impl, become_manager_with_takeover_impl,
    prepare_recovery_handoff_impl,
)

def test_prepare_handoff_writes_file_and_snapshots(fresh_orchestrator_dir):
    # Register a manager and one worker; queue one pending question.
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


def test_become_manager_with_takeover_releases_and_acquires(fresh_orchestrator_dir, monkeypatch):
    # Register old manager + prepare handoff.
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
    # New manager record exists and inherited the predecessor's name + domain.
    new_record = state.read_json(paths.ACTIVE / "mgr-new.json")
    assert new_record["agent"] == "manager"
    assert new_record["name"] == old_result["name"]
    assert new_record["domain"] == "general"
    assert new_record["runtime"] == "claude"
    # Old manager's tmux window was closed gracefully (no SIGTERM — SessionEnd
    # needs to fire for the outgoing session's retro + memory distill).
    assert closed == ["i0"]
    # Handoff is marked consumed.
    handoff_after = state.read_json(paths.HANDOFFS / f"{handoff['handoff_id']}.json")
    assert handoff_after["consumed_at"] is not None
    assert handoff_after["to_sid"] == "mgr-new"


def test_become_manager_with_takeover_stamps_account_from_env(fresh_orchestrator_dir, monkeypatch):
    """A recovery tab spawns with CLAUDE_ORCH_ACCOUNT in its env; the takeover's
    re-registration must stamp the new manager's record with that letter so the
    flip lane attributes a later brick to the real account."""
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda window_id: None)
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)
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
    # Managers are Claude-only — the resumed manager always registers as claude.
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
    """A terminal close-window subprocess failure must NOT abort the takeover —
    `_close_window` swallows internally; the new manager registers and
    consumes the handoff regardless.
    """
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
    # The old manager's active record must not have been touched.
    assert state.read_json(paths.ACTIVE / "mgr-old.json") is not None
    assert not (paths.ACTIVE / "mgr-new.json").exists()


# --- prepare_recovery_handoff ---

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

    out = prepare_recovery_handoff_impl("mgr-sid-1")
    assert "handoff_id" in out
    assert "path" in out

    handoff = state.read_json(paths.HANDOFFS / f"{out['handoff_id']}.json")
    assert handoff["from_sid"] == "mgr-sid-1"
    assert handoff["recovery"] is True
    assert handoff["trigger_reason"] == "account-flip-recovery"
    assert "[auto-recovery]" in handoff["narrative_summary"]
    # Exact key-parity with prepare_handoff (same schema consumers) — fires in
    # both directions: a key added to prepare_handoff_impl but not mirrored here,
    # or a stray key added only to the recovery record.
    PARITY_KEYS = {"handoff_id", "from_sid", "to_sid", "prepared_at", "consumed_at",
                   "trigger_reason", "narrative_summary", "manager_name", "domain",
                   "workers_snapshot", "questions_snapshot"}
    assert set(handoff.keys()) == PARITY_KEYS | {"recovery"}, \
        "unexpected keys — was prepare_handoff_impl extended? Mirror in the recovery record"
    assert handoff["to_sid"] is None
    assert handoff["consumed_at"] is None
    # snapshots are populated
    assert len(handoff["workers_snapshot"]) == 1
    assert handoff["workers_snapshot"][0]["name"] == "alpha"
    assert len(handoff["questions_snapshot"]) == 1
    assert handoff["questions_snapshot"][0]["question"] == "left or right?"


def test_prepare_recovery_handoff_rejects_non_manager(fresh_orchestrator_dir):
    # No record at all → ValueError
    with pytest.raises(ValueError, match="not an active manager"):
        prepare_recovery_handoff_impl("ghost-sid")

    # A worker record → ValueError
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

    # Handoff is consumed and bound to new-sid
    handoff_after = state.read_json(paths.HANDOFFS / f"{out['handoff_id']}.json")
    assert handoff_after["consumed_at"] is not None
    assert handoff_after["to_sid"] == "new-sid"

    # New manager record exists
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
    """Takeover must not orphan questions addressed to the old sid."""
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    # Manager rarely receives questions but the consistency invariant matters.
    from dockwright.mcp_server import _write_question
    _write_question(worker_sid="mgr-old", worker_name="manager", question="dangling?")

    monkeypatch.setattr("dockwright.mcp_server._close_window", lambda w: None)
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)

    become_manager_with_takeover_impl(
        claude_sid="mgr-new", takeover_from="mgr-old",
        handoff_id=handoff["handoff_id"], iterm_sid="i1",
    )
    # All old-sid questions are gone
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
    # Second takeover with the same handoff must fail.
    with pytest.raises(ValueError, match="already consumed"):
        become_manager_with_takeover_impl(
            claude_sid="mgr-newest", takeover_from="mgr-old",
            handoff_id=handoff["handoff_id"], iterm_sid="i2",
        )


def test_become_manager_auto_suffixes_explicit_name_collision(fresh_orchestrator_dir):
    """When an explicit name collides with a live active record, auto-suffix it
    (e.g. /manager-resume preserving the predecessor name during a brief overlap).
    """
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
    # Either inherited the suffix flow OR the rerolled name; never collides.
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
    # Always isolate the account-active pointer so legacy tests (which never call
    # _enable_pool) stay pool-off even if ~/.claude/dockwright/account-active
    # exists on the machine running the tests. Pool tests call _enable_pool AFTER
    # this, whose setattr overwrites the sentinel.
    monkeypatch.setattr(paths, "ACCOUNT_ACTIVE", Path("/nonexistent/__no_account_active__"))
    return captured


def test_spawn_worker_forwards_extra_args(monkeypatch):
    captured = _patch_exec(monkeypatch)
    _asyncio.run(spawner.spawn_worker_tab(
        cwd="/tmp/x",
        initial_prompt="hello",
        name="alpha",
        extra_args=["--dangerously-skip-permissions"],
    ))
    inner_cmd = captured["args"][-1]
    assert "--dangerously-skip-permissions" in inner_cmd
    # extra_args must appear before the prompt
    assert inner_cmd.index("--dangerously-skip-permissions") < inner_cmd.index("hello")
    # And after `claude `
    claude_pos = inner_cmd.rindex("claude ")
    assert claude_pos < inner_cmd.index("--dangerously-skip-permissions")


def test_spawn_worker_forwards_env(monkeypatch):
    captured = _patch_exec(monkeypatch)
    _asyncio.run(spawner.spawn_worker_tab(
        cwd="/tmp/x",
        initial_prompt="hi",
        name="alpha",
        env={"MY_VAR": "1"},
    ))
    inner_cmd = captured["args"][-1]
    assert "MY_VAR=1" in inner_cmd
    # Orchestrator-controlled keys still present and not overridden
    assert "CLAUDE_AGENT=worker" in inner_cmd
    assert "CLAUDE_WORKER_NAME=alpha" in inner_cmd
    assert "CLAUDE_WORKER_RUNTIME=claude" in inner_cmd


def test_spawn_worker_caller_cannot_override_orchestrator_env(monkeypatch):
    captured = _patch_exec(monkeypatch)
    _asyncio.run(spawner.spawn_worker_tab(
        cwd="/tmp/x",
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
        cwd="/tmp/x",
        initial_prompt="hi",
        name="alpha",
    ))
    inner_cmd = captured["args"][-1]
    # No model passed → orchestrator appends its opus[1m] default before the prompt
    assert inner_cmd.rstrip().endswith("claude --model 'opus[1m]' hi")


def test_spawn_worker_codex_runtime_builds_codex_command(monkeypatch):
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
    # Managers are Claude-only — a manager spawn carries no runtime marker env.
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
    assert "/manager-resume h1" in inner_cmd
    assert "You are an orchestrator worker running in a separate tmux window" not in inner_cmd


def test_spawn_worker_codex_rejects_claude_only_extra_args(monkeypatch):
    _patch_exec(monkeypatch)
    with pytest.raises(ValueError, match="runtime='codex'.*Claude-only"):
        _asyncio.run(spawner.spawn_worker_tab(
            cwd="/tmp/x",
            initial_prompt="hello",
            name="alpha",
            runtime="codex",
            extra_args=["--settings", "{}"],
        ))


def test_spawn_worker_codex_rejects_default_overrides(monkeypatch):
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
def test_spawn_worker_codex_rejects_compact_default_overrides(monkeypatch, extra_arg):
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
        cwd="/tmp/x",
        initial_prompt="hi",
        name="alpha",
        target_window_match="window_id:42",
    ))
    argv = list(captured["args"])
    assert "new-window" in argv
    # Forwarded verbatim as the new-window target so the worker lands in the
    # named window/session, not a fresh detached one.
    assert "-t" in argv and argv[argv.index("-t") + 1] == "window_id:42"


def test_spawn_worker_no_match_flag_when_target_window_match_unset(monkeypatch):
    terminal._DRIVER = None
    captured = _patch_exec(monkeypatch)
    _asyncio.run(spawner.spawn_worker_tab(
        cwd="/tmp/x",
        initial_prompt="hi",
        name="alpha",
    ))
    argv = list(captured["args"])
    assert "new-window" in argv
    # Default mode: no -t target (a fresh detached window).
    assert "-t" not in argv


def test_spawn_worker_route_to_workers_window_ignores_target_window_match(monkeypatch):
    terminal._DRIVER = None
    captured = _patch_exec(monkeypatch)

    async def fake_find(self):   # now a method → takes self
        return "%99"

    monkeypatch.setattr(terminal.TmuxDriver, "find_group_pane", fake_find)
    _asyncio.run(spawner.spawn_worker_tab(
        cwd="/tmp/x",
        initial_prompt="hi",
        name="alpha",
        route_to_workers_window=True,
        target_window_match="window_id:42",
    ))
    argv = list(captured["args"])
    # An existing workers window/session means new-window into the workers group,
    # NOT the caller-supplied target_window_match.
    assert "new-window" in argv
    assert argv[argv.index("-t") + 1] == terminal.WORKERS_OS_WINDOW_CLASS
    assert "window_id:42" not in argv


def test_spawn_worker_tab_manager_routes_to_mgr_session(monkeypatch):
    """agent="manager" must call get_driver().spawn with route_to_manager_session=True;
    agent="worker" (default) must call it with route_to_manager_session=False."""
    import asyncio as _asyncio2
    from dockwright import spawner as _spawner

    # Isolate account selection: point ACCOUNT_ACTIVE at a nonexistent file so
    # the pool is off and _active_account() also returns None (same guard as
    # _patch_exec uses).
    monkeypatch.setattr(paths, "ACCOUNT_ACTIVE", Path("/nonexistent/__no_account_active__"))

    captured_spawn_kwargs: dict = {}

    class FakeDrv:
        async def spawn(self, **kw):
            captured_spawn_kwargs.update(kw)
            return "%9"

    monkeypatch.setattr(_spawner, "get_driver", lambda: FakeDrv())

    # manager spawn → route_to_manager_session=True
    _asyncio2.run(_spawner.spawn_worker_tab(
        cwd="/tmp/x",
        initial_prompt="/manager-resume x",
        name="m",
        agent="manager",
    ))
    assert captured_spawn_kwargs.get("route_to_manager_session") is True, (
        f"expected route_to_manager_session=True for agent='manager', got: {captured_spawn_kwargs}"
    )

    # worker spawn → route_to_manager_session=False
    captured_spawn_kwargs.clear()
    _asyncio2.run(_spawner.spawn_worker_tab(
        cwd="/tmp/x",
        initial_prompt="do work",
        name="w",
        agent="worker",
    ))
    assert captured_spawn_kwargs.get("route_to_manager_session") is False, (
        f"expected route_to_manager_session=False for agent='worker', got: {captured_spawn_kwargs}"
    )


# --- account pool login model (per-CLAUDE_CONFIG_DIR keychain login) ---

def _enable_pool(monkeypatch, tmp_path, letter="a"):
    """Fake pointer + isolated counter/state. The login-model picker NEVER calls
    `security`; this guard fails the test if it ever does.

    Patches SPAWN_COUNTER and ACCOUNT_STATE to tmp_path so _pick_account() never
    touches ~/.claude/dockwright/ state during tests. Counter starts at 0
    (a-slot by default weights 1:1), which is why letter='a' is the default.
    """
    pointer = tmp_path / "account-active"
    pointer.write_text(f"{letter}\n")
    monkeypatch.setattr(paths, "ACCOUNT_ACTIVE", pointer)
    monkeypatch.setattr(paths, "SPAWN_COUNTER", tmp_path / "spawn-counter.json")
    monkeypatch.setattr(paths, "ACCOUNT_STATE", tmp_path / "account-state.json")
    monkeypatch.setattr(paths, "ACCOUNT_USAGE", tmp_path / "usage")
    # Keep spawn tests hermetic: never build a real ~/.claude-<letter> farm in HOME.
    # Return a deterministic tmp-based dir so worker-prefix assertions still see
    # CLAUDE_CONFIG_DIR without touching the real filesystem.
    def _fake_farm(letter):
        d = tmp_path / f".claude-{letter}"
        d.mkdir(parents=True, exist_ok=True)
        (d / ".claude.json").write_text(
            '{"mcpServers": {"claude-orchestrator": {"command": "orchestrator"}}}'
        )
        return d
    monkeypatch.setattr(spawner, "ensure_account_config_dir", _fake_farm)

    def fake_run(args, **kwargs):
        if isinstance(args, (list, tuple)) and args and args[0] == "security":
            raise AssertionError(f"login model must not call security: {args}")
        return subprocess.CompletedProcess(args, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(spawner.subprocess, "run", fake_run)
    return pointer


def test_spawn_worker_account_a_default(monkeypatch, tmp_path):
    """Worker on account 'a' (the default ~/.claude): no token, no CLAUDE_CONFIG_DIR,
    stamp 'a'. counter=0 with 1:1 weights → 'a'."""
    captured = _patch_exec(monkeypatch)
    # _enable_pool must come AFTER _patch_exec so its setattr wins.
    _enable_pool(monkeypatch, tmp_path, letter="a")
    _asyncio.run(spawner.spawn_worker_tab(
        cwd="/tmp/x",
        initial_prompt="hi",
        name="alpha",
    ))
    inner_cmd = captured["args"][-1]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in inner_cmd
    assert "CLAUDE_CONFIG_DIR" not in inner_cmd
    assert "CLAUDE_ORCH_ACCOUNT=a" in inner_cmd


def test_spawn_worker_account_b_config_dir(monkeypatch, tmp_path):
    """Worker on account 'b': CLAUDE_CONFIG_DIR=.../.claude-b, no token, stamp 'b'.
    Force the picker to 'b' directly (W_A=0 would still clamp to 'a')."""
    captured = _patch_exec(monkeypatch)
    _enable_pool(monkeypatch, tmp_path, letter="b")
    monkeypatch.setattr(spawner, "_pick_account", lambda force=False: "b")
    _asyncio.run(spawner.spawn_worker_tab(
        cwd="/tmp/x",
        initial_prompt="hi",
        name="alpha",
    ))
    inner_cmd = captured["args"][-1]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in inner_cmd
    assert f"CLAUDE_CONFIG_DIR={tmp_path}/.claude-b" in inner_cmd
    assert "CLAUDE_ORCH_ACCOUNT=b" in inner_cmd
    # The login authenticates the session — no keychain reads in the cmdline.
    assert "$(security" not in inner_cmd


def test_spawn_manager_rides_pointer_a(monkeypatch, tmp_path):
    """Manager rides the pointer: pointer=a → default ~/.claude, no token, no
    CLAUDE_CONFIG_DIR, stamp 'a'."""
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
    """Manager rides the pointer: pointer=b → CLAUDE_CONFIG_DIR=.../.claude-b,
    no token, stamp 'b'."""
    captured = _patch_exec(monkeypatch)
    _enable_pool(monkeypatch, tmp_path, letter="b")
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
    # _patch_exec already points ACCOUNT_ACTIVE at a nonexistent path; no _enable_pool
    _asyncio.run(spawner.spawn_worker_tab(
        cwd="/tmp/x",
        initial_prompt="hi",
        name="alpha",
    ))
    inner_cmd = captured["args"][-1]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in inner_cmd
    assert "CLAUDE_ORCH_ACCOUNT" not in inner_cmd


def test_spawn_omits_prefix_on_invalid_pointer(monkeypatch, tmp_path):
    # letter 'z' is not valid (not a|b) → pool off, no stamp.
    captured = _patch_exec(monkeypatch)
    _enable_pool(monkeypatch, tmp_path, letter="z")
    _asyncio.run(spawner.spawn_worker_tab(cwd="/tmp/x", initial_prompt="hi", name="alpha"))
    inner_cmd_z = captured["args"][-1]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in inner_cmd_z
    assert "CLAUDE_ORCH_ACCOUNT" not in inner_cmd_z


def test_caller_token_disables_pool_injection(monkeypatch, tmp_path):
    """A caller-supplied CLAUDE_CODE_OAUTH_TOKEN disables pool routing for this
    spawn: no CLAUDE_CONFIG_DIR farm and no CLAUDE_ORCH_ACCOUNT stamp. Note the
    default ~/.claude keychain login now OUTRANKS the caller token, so the token
    no longer reliably forces a token identity — it's kept as a defensive escape
    hatch and the session record staying unstamped is truthful. The caller's raw
    token rides the worker cmdline (visible in ps — their informed choice). A
    forged CLAUDE_ORCH_ACCOUNT is still dropped."""
    captured = _patch_exec(monkeypatch)
    _enable_pool(monkeypatch, tmp_path, letter="a")
    _asyncio.run(spawner.spawn_worker_tab(
        cwd="/tmp/x",
        initial_prompt="hi",
        name="alpha",
        env={
            "CLAUDE_ORCH_ACCOUNT": "b",          # caller tries to forge account
            "CLAUDE_CODE_OAUTH_TOKEN": "caller-token",  # caller owns auth
        },
    ))
    inner_cmd = captured["args"][-1]
    # Caller token rides through as plain caller env…
    assert "CLAUDE_CODE_OAUTH_TOKEN=caller-token" in inner_cmd
    # …and the pool prefix is fully absent: no substitution, no stamp at all.
    assert "$(security" not in inner_cmd
    assert "CLAUDE_ORCH_ACCOUNT" not in inner_cmd


def test_caller_config_dir_dropped_picker_wins(monkeypatch, tmp_path):
    """CLAUDE_CONFIG_DIR is the sole billing lever, so a caller-passed value must
    NOT override the picker's account-derived farm — otherwise a caller could
    mis-bill by pinning the spawn to another account's config dir. The forged
    value is dropped from the caller-env section and the picker's
    CLAUDE_CONFIG_DIR (=.../.claude-b) is the only one in the inner cmd."""
    captured = _patch_exec(monkeypatch)
    _enable_pool(monkeypatch, tmp_path, letter="b")
    monkeypatch.setattr(spawner, "_pick_account", lambda force=False: "b")
    _asyncio.run(spawner.spawn_worker_tab(
        cwd="/tmp/x",
        initial_prompt="hi",
        name="alpha",
        env={"CLAUDE_CONFIG_DIR": "/tmp/evil"},   # caller tries to override billing
    ))
    inner_cmd = captured["args"][-1]
    assert "/tmp/evil" not in inner_cmd, "caller CLAUDE_CONFIG_DIR must be dropped"
    assert f"CLAUDE_CONFIG_DIR={tmp_path}/.claude-b" in inner_cmd, "picker's farm wins"
    assert inner_cmd.count("CLAUDE_CONFIG_DIR=") == 1, "only the picker's assignment survives"
    assert "CLAUDE_ORCH_ACCOUNT=b" in inner_cmd


# --- window_id_exists parsing ---


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
    # tmux list-panes -F "#{pane_id}" emits one pane id per line.
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
    # the window_id arg arrives as a str; pane_exists compares it against the
    # list-panes output verbatim.
    terminal._DRIVER = None
    _patch_terminal_ls(monkeypatch, _panes_stdout(["42"]))
    assert _asyncio.run(spawner.window_id_exists("42")) is True


# --- Preset support on spawn_worker ---

from dockwright.mcp_server import spawn_worker_impl
from dockwright.mcp_server import _repo_sync_footer


def _patch_spawn_worker_tab(monkeypatch):
    """Replace spawner.spawn_worker_tab with a recorder. Returns the captured dict."""
    captured: dict = {}

    async def fake_spawn(**kwargs):
        captured.update(kwargs)
        return ("999", kwargs.get("name", ""))

    # spawn_worker_impl does a lazy `from .spawner import spawn_worker_tab`, so we
    # patch the source module's attribute, which is what the lazy import resolves to.
    monkeypatch.setattr(spawner, "spawn_worker_tab", fake_spawn)
    return captured


# --- spawn registration detection net ---

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
    _patch_spawn_registers_active(monkeypatch)  # writes active/spawned-<name>.json, agent=worker
    result = _asyncio.run(spawn_worker_impl(
        initial_prompt="task", name="reg-worker", cwd="/tmp/x",
        _registration_timeout_sec=2.0, _poll_interval=0.01))
    assert result["status"] == "registered"
    assert result["claude_sid"] == "spawned-reg-worker"
    assert result["window_id"] == "999"


def test_spawn_worker_impl_reports_no_register(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    _patch_spawn_worker_tab(monkeypatch)  # never registers
    result = _asyncio.run(spawn_worker_impl(
        initial_prompt="task", name="lost-worker", cwd="/tmp/x",
        _registration_timeout_sec=0.2, _poll_interval=0.01))
    assert result["status"] == "no_register"
    assert result["window_id"] == "999"
    assert result["assignment_id"]
    assert "did not register" in result["reason"]
    assert paths.pending_assignment_path(result["assignment_id"]).exists()


# --- spawn_replacement_manager OS-window targeting ---

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
    # window_id: (not id:) so the old manager's window id can't collide with an
    # unrelated tab id and recreate the manager in the wrong OS-window.
    assert captured["target_window_match"] == "window_id:42"
    assert captured["runtime"] == "claude"
    assert result["runtime"] == "claude"


def test_spawn_replacement_manager_pins_opus_model(fresh_orchestrator_dir, monkeypatch):
    # The recreate lane must pin the manager model explicitly: without
    # extra_args it rides the spawner's WORKER default, so a future worker
    # default change would silently move the manager lane too.
    become_manager_impl(claude_sid="mgr-old", iterm_sid="42")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    captured = _patch_spawn_worker_tab(monkeypatch)
    _patch_window_id_exists(monkeypatch, True)
    _asyncio.run(spawn_replacement_manager_impl(handoff["handoff_id"]))
    assert captured["extra_args"] == ["--model", "opus[1m]"]


def test_spawn_replacement_manager_inherits_predecessor_funny_name(fresh_orchestrator_dir, monkeypatch):
    """The incoming tab inherits the predecessor's funny name via CLAUDE_WORKER_NAME,
    so its SessionStart placeholder IS the eventual name (become_manager_with_takeover
    does the authoritative rename). Passing "manager" here would defeat the funny-name
    identity hardening."""
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
    """Legacy handoff with no recorded manager_name → CLAUDE_WORKER_NAME="" so the
    SessionStart hook rolls a fresh funny name instead of the literal "manager"."""
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
    _patch_window_id_exists(monkeypatch, True)  # should not even be consulted
    _asyncio.run(spawn_replacement_manager_impl(handoff["handoff_id"]))
    assert captured["target_window_match"] is None


from dockwright.mcp_server import _resolve_old_manager_window_match


def test_resolve_old_manager_window_match_falls_back_when_iterm_sid_empty(fresh_orchestrator_dir):
    # Legacy active record predating managers storing iterm_sid: a falsy ""
    # must fall back to bare --type=tab (resolver returns None) without ever
    # consulting tmux list-panes.
    become_manager_impl(claude_sid="mgr-old", iterm_sid="42")
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    record = state.read_json(paths.ACTIVE / "mgr-old.json")
    # Simulate a legacy active record with no window id: clear both the new
    # and legacy keys. The helper falls back to "" when neither is set.
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
    """Calling without preset must pass initial_prompt through unmodified
    (modulo the universal repo-sync footer every non-blank prompt gets)."""
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="real task",
        name="no-preset-test",
        cwd="/tmp/x",
    ))
    assert captured["initial_prompt"] == "real task" + _repo_sync_footer()


# --- Remote Control disabled on workers ---

REMOTE_OFF_FLAGS = ["--settings", '{"enableAllProjectMcpServers": true, "remoteControlAtStartup": false, "disableRemoteControl": true}']
RC_ON_FLAGS = ["--settings", '{"enableAllProjectMcpServers": true}', "--remote-control"]


def test_spawn_worker_disables_remote_control(fresh_orchestrator_dir, monkeypatch):
    """Workers must auto-prepend --settings flags that disable Claude Code Remote."""
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
    """Caller-supplied extra_args must be APPENDED after the remote-off flags, not replaced."""
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="task",
        name="remote-off-append-test",
        cwd="/tmp/x",
        extra_args=["--dangerously-skip-permissions"],
    ))
    assert captured["extra_args"] == REMOTE_OFF_FLAGS + ["--dangerously-skip-permissions"]


def test_spawn_worker_impl_codex_runtime_skips_claude_remote_flags(fresh_orchestrator_dir, monkeypatch):
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


# --- Remote Control opt-in (CLAUDE_ORCH_WORKER_RC=1) ---

from dockwright.mcp_server import _claude_worker_settings_args


def test_claude_rc_args_default_keeps_remote_off(monkeypatch):
    """Flag unset → the legacy RC-off --settings, no --remote-control."""
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    assert _claude_worker_settings_args() == REMOTE_OFF_FLAGS
    assert "--remote-control" not in _claude_worker_settings_args()


@pytest.mark.parametrize("val", ["0", "", "true", "yes", " ", "2", "01", "1x"])
def test_claude_rc_args_non_one_values_keep_remote_off(monkeypatch, val):
    """Any value other than "1" preserves the byte-identical RC-off default."""
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_RC", val)
    assert _claude_worker_settings_args() == REMOTE_OFF_FLAGS


@pytest.mark.parametrize("val", ["1", " 1 "])
def test_claude_rc_args_enables_remote_when_opted_in(monkeypatch, val):
    """CLAUDE_ORCH_WORKER_RC=1 → --remote-control, and NOT the RC-off --settings."""
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_RC", val)
    assert _claude_worker_settings_args() == RC_ON_FLAGS
    assert "--remote-control" in _claude_worker_settings_args()
    assert "remoteControlAtStartup" not in _claude_worker_settings_args()[1]


def test_spawn_worker_opt_in_enables_remote_control(fresh_orchestrator_dir, monkeypatch):
    """flag=1 → worker extra_args contain --remote-control and NOT the RC-off settings."""
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
    """flag=1 → --remote-control prepended, caller extra_args still appended after."""
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_RC", "1")
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="task",
        name="rc-on-append",
        cwd="/tmp/x",
        extra_args=["--dangerously-skip-permissions"],
    ))
    assert captured["extra_args"] == RC_ON_FLAGS + ["--dangerously-skip-permissions"]


@pytest.mark.parametrize("flag", [None, "1"])
def test_manager_spawn_unaffected_by_worker_rc_flag(fresh_orchestrator_dir, monkeypatch, flag):
    """The worker RC opt-in must never leak into the manager spawn path, either way."""
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
    assert "--remote-control" not in extra
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
    assert captured == {}  # spawn_worker_tab never called (recorder stays empty)
    # gate returns before _write_pending_assignment → no pending leaked (dir is isolated)
    assert not list(paths.ASSIGNMENTS_PENDING.glob("*.json"))


def test_spawn_worker_impl_force_bypasses_pause(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _enable_pool(monkeypatch, tmp_path, letter="a")
    captured = _patch_spawn_worker_tab(monkeypatch)
    _write_usage_mcp(tmp_path, "a", 96.0)
    _write_usage_mcp(tmp_path, "b", 97.0)
    result = _asyncio.run(spawn_worker_impl("hi", name="forced-one", force=True))
    assert result.get("status") != "paused"
    assert captured.get("force") is True  # force forwarded to spawn_worker_tab


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
    # spawn_worker_impl lazy-imports spawn_worker_tab from .spawner, so patch the
    # source module's attribute (what the lazy import resolves to).
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

def test_spawn_worker_default_cwd_falls_back_to_getcwd_when_home_absent(fresh_orchestrator_dir, monkeypatch, tmp_path):
    captured = _patch_spawn_worker_tab(monkeypatch)
    absent = tmp_path / "does-not-exist"
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_HOME", str(absent))
    monkeypatch.chdir(tmp_path)
    _asyncio.run(spawn_worker_impl(initial_prompt="poke", name="wh-absent"))
    assert captured["cwd"] == str(tmp_path)  # os.getcwd() fallback

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
    # Managers are Claude-only — neither tool exposes a runtime selector.
    import inspect
    from dockwright.mcp_server import become_manager, spawn_replacement_manager

    assert "runtime" not in inspect.signature(become_manager).parameters
    assert "runtime" not in inspect.signature(spawn_replacement_manager).parameters


# --- wait_for_worker ---

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
    """Resolving a MANAGER record would pin the wait on a sid that never writes a
    done event — fail fast. And not with the generic "no worker named": the name
    IS taken, just by the wrong kind of session — say so."""
    register_self_impl(claude_sid="mgr-1", agent="manager", name="happy-yak", cwd="/x", iterm_sid="i1", pid=os.getpid())
    with pytest.raises(ValueError, match="held by an active manager"):
        _asyncio.run(wait_for_worker_impl("happy-yak", timeout_sec=1, _poll_interval=0.01))


def test_wait_for_worker_done_event_beats_manager_holder_error(fresh_orchestrator_dir):
    """A done event for the name must keep winning over the holder fail-fast —
    the precedence is statement order, so pin it against reorders."""
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
    """A closed worker shadowed by a same-named active manager still resolves to
    the worker's record — 'exited', not the holder error."""
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
    """Worker already in closed/ with a done event → return done immediately."""
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
    """Worker only in closed/ with no done event → exited immediately, no waiting."""
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
    # Audit repro (finding 2): worker finished task 1 30 min ago, manager
    # re-tasked it, wait must NOT return task 1's summary instantly.
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
    # Flow B: worker finished AFTER the last tasking; wait returns immediately.
    register_self_impl(claude_sid="w1", agent="worker", name="foo", cwd="/x", iterm_sid="i1", pid=os.getpid())
    _rewrite_active("w1", state="idle", tasked_at=time.time() - 60)
    _write_done_event(sid="w1", worker_name="foo", summary="fresh done")
    result = _asyncio.run(wait_for_worker_impl("foo", timeout_sec=60, _poll_interval=0.05))
    assert result["found"] == "done" and result["summary"] == "fresh done"


def test_wait_for_worker_legacy_record_without_stamps_keeps_old_behavior(fresh_orchestrator_dir):
    # Records written before the upgrade have neither tasked_at nor
    # processing_since: bound is 0 -> old (stale-returning) behavior preserved.
    register_self_impl(claude_sid="w1", agent="worker", name="foo", cwd="/x", iterm_sid="i1", pid=os.getpid())
    _write_done_event(sid="w1", worker_name="foo", summary="old done", completed_at=time.time() - 1800)
    result = _asyncio.run(wait_for_worker_impl("foo", timeout_sec=60, _poll_interval=0.05))
    assert result["found"] == "done" and result["summary"] == "old done"


def test_wait_for_worker_grace_window_admits_done_crossing_the_retask(fresh_orchestrator_dir):
    # A legit done landing within the 2s grace before the re-task must be
    # returned (otherwise a done/nudge crossing hangs the wait until timeout).
    register_self_impl(claude_sid="w1", agent="worker", name="foo", cwd="/x", iterm_sid="i1", pid=os.getpid())
    now = time.time()
    _write_done_event(sid="w1", worker_name="foo", summary="crossed done", completed_at=now - 1.0)
    _rewrite_active("w1", state="processing", tasked_at=now)
    result = _asyncio.run(wait_for_worker_impl("foo", timeout_sec=60, _poll_interval=0.05))
    assert result["summary"] == "crossed done"


def test_wait_for_worker_processing_since_gates_without_tasked_at(fresh_orchestrator_dir):
    # Human-typed re-task path: no manager send, but user_prompt_submit stamped
    # processing_since; a done event older than the running episode is stale.
    register_self_impl(claude_sid="w1", agent="worker", name="foo", cwd="/x", iterm_sid="i1", pid=os.getpid())
    _write_done_event(sid="w1", worker_name="foo", summary="TASK 1 done", completed_at=time.time() - 1800)
    _rewrite_active("w1", state="processing", processing_since=time.time() - 60)
    with pytest.raises(TimeoutError):
        _asyncio.run(wait_for_worker_impl("foo", timeout_sec=1, _poll_interval=0.05))


def test_wait_for_worker_processing_since_ignored_when_idle(fresh_orchestrator_dir):
    # processing_since outlives the episode (stop_hook flips state to idle but
    # keeps the field); it must only gate while state == processing.
    register_self_impl(claude_sid="w1", agent="worker", name="foo", cwd="/x", iterm_sid="i1", pid=os.getpid())
    _write_done_event(sid="w1", worker_name="foo", summary="done before idle", completed_at=time.time() - 1800)
    _rewrite_active("w1", state="idle", processing_since=time.time() - 60)
    result = _asyncio.run(wait_for_worker_impl("foo", timeout_sec=60, _poll_interval=0.05))
    assert result["summary"] == "done before idle"


def test_wait_for_worker_unrelated_record_stamps_never_gate_closed_worker(fresh_orchestrator_dir):
    # Guard for the record loop-variable leak: sid resolves via closed/, and a
    # DIFFERENT worker's active record (with fresh stamps) is the last one the
    # resolution loop iterated. Its tasked_at must not gate foo's done event.
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
    monkeypatch.setattr("dockwright.mcp_server._send_text", lambda wid, text: None)
    before = time.time()
    send_manager_to_worker_impl(worker="alpha", text="new task")
    record = state.read_json(paths.ACTIVE / "w1.json")
    assert record.get("tasked_at") is not None
    assert record["tasked_at"] >= before


# --- prepare_handoff manager-memory distill ---


class _FakeCompleted:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_prepare_handoff_writes_distill_file(fresh_orchestrator_dir, tmp_path, monkeypatch):
    # Manager session needs a transcript so find_session_log resolves to something real.
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
    # Multi-manager: memory lives in manager-memory/<domain>/<date>-<sid>.md
    assert memory_file.parent == paths.MANAGER_MEMORY / "general"
    assert memory_file.name.endswith("-mgr-old.md")
    assert "shipped X" in memory_file.read_text()
    # Distill is pure transcript summarization — pinned to sonnet to cut cost rather
    # than inheriting the user's (opus) default.
    argv = captured["args"][0]
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4-6"
    # Effort pinned to high (deterministic) instead of inheriting the CLI's undocumented default.
    assert argv[argv.index("--effort") + 1] == "high"
    # Slimmed bytes piped to claude -p (raw transcripts can be MBs and overflow the prompt).
    assert captured["input"] == b"USER: do X\n\nASSISTANT: ok"
    # Timeout cap applied so a hung subprocess can't block the handoff indefinitely.
    assert captured["timeout"] == 180
    assert log.exists()  # transcript stays on disk through the test


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
    """Distill prompt asks for Decisions + Direction + Shipped + Open threads.
    Three of four are time-uniform across the session, so dropping the head
    loses the user's original direction and early decisions. Keep both ends.
    """
    from dockwright.mcp_server import _slim_transcript
    raw_lines = [
        json.dumps({"type": "user", "message": {"content": f"msg-{i:03d}"}})
        for i in range(200)
    ]
    raw = ("\n".join(raw_lines)).encode("utf-8")
    slim = _slim_transcript(raw, max_bytes=500)
    decoded = slim.decode("utf-8")
    assert "[transcript middle truncated]" in decoded
    # Head: earliest messages survive.
    assert "msg-000" in decoded
    # Tail: latest messages survive.
    assert "msg-199" in decoded
    # Middle dropped.
    assert "msg-100" not in decoded


def test_slim_transcript_keeps_inner_text_of_list_tool_result():
    """Worker_done summaries arrive as `[{type:'text', text:'shipped abc'}]`
    inside tool_result.content. Don't elide them — they're tiny and feed the
    distill prompt's `Shipped` section.
    """
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
    """Bulk string-shaped tool_results (the common case) stay elided."""
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
    """`claude -p` writes 'Prompt is too long' to STDOUT (not stderr) — log it."""
    _write_fake_transcript(tmp_path, monkeypatch, "mgr-old", [
        {"type": "user", "message": {"content": "hi"}},
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
    # Handoff record is the source of truth — it MUST still exist.
    assert (paths.HANDOFFS / f"{result['handoff_id']}.json").exists()
    assert result["distill_path"] is None
    # No file should have been written into manager-memory.
    assert list(paths.MANAGER_MEMORY.iterdir()) == []


def test_prepare_handoff_distill_missing_transcript_skips(fresh_orchestrator_dir, tmp_path, monkeypatch):
    # HOME points at tmp_path but we do NOT write a transcript — find_session_log returns None.
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i0")

    called = []

    def fake_run(*args, **kwargs):
        called.append(args)
        return _FakeCompleted(stdout=b"should not be invoked")

    monkeypatch.setattr("dockwright.mcp_server.subprocess.run", fake_run)

    result = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    # Handoff still written even though distill was skipped.
    assert (paths.HANDOFFS / f"{result['handoff_id']}.json").exists()
    assert result["distill_path"] is None
    # claude -p must NOT have been spawned with no transcript to feed it.
    assert called == []
    assert list(paths.MANAGER_MEMORY.iterdir()) == []


# --- Multi-manager: routing filters ---

from dockwright.mcp_server import (
    _matches_manager, list_workers_impl as _lw, list_pending_questions_impl as _lpq,
    _write_question, worker_done_impl as _wd, list_closed_workers_impl as _lcw,
)


def test_routing_filter_isolates_workers_by_parent_manager_name(fresh_orchestrator_dir):
    """Manager A's list_workers must not see manager B's workers."""
    # Two managers, two workers — each scoped.
    register_self_impl(claude_sid="w-a", agent="worker", name="worker-a", cwd="/x",
                       iterm_sid="i1", pid=os.getpid(), parent_manager_name="manager-a")
    register_self_impl(claude_sid="w-b", agent="worker", name="worker-b", cwd="/y",
                       iterm_sid="i2", pid=os.getpid(), parent_manager_name="manager-b")
    workers_a = _lw(manager_name="manager-a")
    workers_b = _lw(manager_name="manager-b")
    assert [w["name"] for w in workers_a] == ["worker-a"]
    assert [w["name"] for w in workers_b] == ["worker-b"]


def test_routing_filter_excludes_null_parent_under_strict_scope(fresh_orchestrator_dir):
    """A legacy record (no parent_manager_name) is INVISIBLE to per-manager calls.

    Strict semantics — see ~/.claude/rules (orchestrator-routing-cleanup spec).
    Recovery path: _backfill_legacy_workers on a single-manager become_manager.
    """
    register_self_impl(claude_sid="legacy", agent="worker", name="oldie", cwd="/x",
                       iterm_sid="i1", pid=os.getpid())  # no parent_manager_name
    register_self_impl(claude_sid="w-a", agent="worker", name="worker-a", cwd="/x",
                       iterm_sid="i2", pid=os.getpid(), parent_manager_name="manager-a")
    a_view = _lw(manager_name="manager-a")
    b_view = _lw(manager_name="manager-b")
    assert "oldie" not in [w["name"] for w in a_view]
    assert "worker-a" in [w["name"] for w in a_view]
    assert "oldie" not in [w["name"] for w in b_view]
    assert "worker-a" not in [w["name"] for w in b_view]
    # Wildcard (None) still sees both — back-compat for legacy callers.
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
    # wait_for_worker by name resolved through manager scoping should find only the right one.
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
    """A null-parent (legacy) worker still writes to _unscoped (event-side
    contract preserved), but per-manager wait_for_worker calls do NOT resolve
    it — they raise ValueError. Recovery: backfill or wildcard call."""
    register_self_impl(claude_sid="w-legacy", agent="worker", name="legacy", cwd="/x",
                       iterm_sid="i1", pid=os.getpid())  # no parent_manager_name
    _wd(claude_sid="w-legacy", summary="legacy done")
    unscoped = list((paths.DONE / paths.UNSCOPED_BUCKET).glob("w-legacy-*.json"))
    assert len(unscoped) == 1  # write-side contract unchanged
    for mgr in ("manager-a", "manager-b"):
        with pytest.raises(ValueError, match="no worker named 'legacy'"):
            _asyncio.run(wait_for_worker_impl("legacy", timeout_sec=60,
                                              _poll_interval=0.05, manager_name=mgr))
    # Wildcard still resolves (back-compat).
    result = _asyncio.run(wait_for_worker_impl("legacy", timeout_sec=60,
                                                _poll_interval=0.05, manager_name=None))
    assert result["found"] == "done"
    assert result["summary"] == "legacy done"


def test_backfill_adopts_orphans_on_single_manager_boot(fresh_orchestrator_dir, monkeypatch):
    """When exactly one manager boots, _backfill_legacy_workers attributes any
    null-parent worker records to it — restoring per-manager visibility."""
    # Two null-parent workers (legacy / pre-multi-manager).
    register_self_impl(claude_sid="w1", agent="worker", name="orphan-1", cwd="/x",
                       iterm_sid="i1", pid=os.getpid())  # null parent
    register_self_impl(claude_sid="w2", agent="worker", name="orphan-2", cwd="/y",
                       iterm_sid="i2", pid=os.getpid())  # null parent
    # No manager yet — both are invisible under strict scope.
    assert _lw(manager_name="solo") == []
    # Manager registers; backfill should fire inside become_manager_impl.
    from dockwright.mcp_server import become_manager_impl
    monkeypatch.setattr("dockwright.mcp_server.names.roll_manager_name",
                        lambda is_taken=None: "solo")
    become_manager_impl(claude_sid="mgr-1", domain="general")
    # Now both orphans should belong to "solo" — per-manager lookup finds them.
    visible = sorted(w["name"] for w in _lw(manager_name="solo"))
    assert visible == ["orphan-1", "orphan-2"]


def test_become_manager_roll_taken_set_includes_worker_funny_names(fresh_orchestrator_dir, monkeypatch):
    """The auto-roll must not reuse a live worker's funny_name (legacy records
    may hold old-pool names that overlap the manager pool)."""
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
    """0 managers → backfill skips with a warning; orphans stay null-parent."""
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
    """2+ managers → backfill skips with a warning; orphans stay null-parent.

    Writes manager records directly (not via become_manager_impl) because the
    test process can host only one same-pid manager — the second register call
    prunes the first via `_prune_same_pid_ghosts`.
    """
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
    """Documented regression: a question written by a null-parent worker is
    NOT visible to any per-manager list_pending_questions call. The scoped
    questions monitor also ignores legacy flat files; recovery is backfill or
    list_pending_questions(manager_name=None)."""
    _write_question(worker_sid="w-orphan", worker_name="orphan",
                    question="anybody?", parent_manager_name=None)
    assert _lpq(manager_name="manager-a") == []
    assert _lpq(manager_name="manager-b") == []
    # Wildcard sees it (back-compat).
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


# --- Multi-manager: /manager-close ---

from dockwright.mcp_server import close_manager_self_impl


def test_close_manager_self_runs_distill_and_clears_active(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _write_fake_transcript(tmp_path, monkeypatch, "mgr-old", [
        {"type": "user", "message": {"content": "hello"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
    ])
    become_manager_impl(claude_sid="mgr-old", iterm_sid="i9", domain="general")

    def fake_run(*args, **kwargs):
        # Differentiate distill (subprocess.run with `input=`) from tmux calls (no input kw).
        if kwargs.get("input") is not None:
            return _FakeCompleted(stdout=b"## Decisions\nshipped\n")
        return _FakeCompleted(stdout=b"")

    monkeypatch.setattr("dockwright.mcp_server.subprocess.run", fake_run)
    result = close_manager_self_impl("mgr-old")
    assert result["ok"] is True
    assert result["distill_path"] is not None
    assert "general" in result["distill_path"]
    # Active record removed (manager closed).
    assert not (paths.ACTIVE / "mgr-old.json").exists()
    # Memory file written.
    assert Path(result["distill_path"]).exists()


def test_close_manager_self_swallows_distill_failure(fresh_orchestrator_dir, monkeypatch):
    """Distill failure must not prevent the active-record cleanup or tab close."""
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


# --- Multi-manager: memory layout migration ---

from dockwright.mcp_server import _migrate_flat_manager_memory


def test_migrate_flat_manager_memory_moves_legacy_files(fresh_orchestrator_dir):
    paths.MANAGER_MEMORY.mkdir(parents=True, exist_ok=True)
    flat_a = paths.MANAGER_MEMORY / "2026-05-01-mgr-old.md"
    flat_a.write_text("# old session A")
    flat_b = paths.MANAGER_MEMORY / "2026-05-02-mgr-older.md"
    flat_b.write_text("# old session B")
    moved = _migrate_flat_manager_memory()
    assert moved == 2
    # Moved into general/ subdir.
    general = paths.MANAGER_MEMORY / "general"
    assert (general / "2026-05-01-mgr-old.md").exists()
    assert (general / "2026-05-02-mgr-older.md").exists()
    # Originals gone.
    assert not flat_a.exists()
    assert not flat_b.exists()


def test_migrate_flat_manager_memory_is_idempotent(fresh_orchestrator_dir):
    """Calling twice produces 0 moves on the second pass."""
    paths.MANAGER_MEMORY.mkdir(parents=True, exist_ok=True)
    (paths.MANAGER_MEMORY / "2026-05-01-x.md").write_text("x")
    assert _migrate_flat_manager_memory() == 1
    assert _migrate_flat_manager_memory() == 0
    # Subdir structure preserved.
    assert (paths.MANAGER_MEMORY / "general" / "2026-05-01-x.md").exists()


def test_migrate_flat_manager_memory_ignores_existing_subdirs(fresh_orchestrator_dir):
    """A pre-existing subdir (e.g. general/, dlq/) shouldn't be touched."""
    (paths.MANAGER_MEMORY / "general").mkdir(parents=True)
    (paths.MANAGER_MEMORY / "general" / "x.md").write_text("kept")
    (paths.MANAGER_MEMORY / "dlq").mkdir()
    (paths.MANAGER_MEMORY / "dlq" / "y.md").write_text("kept too")
    moved = _migrate_flat_manager_memory()
    assert moved == 0
    assert (paths.MANAGER_MEMORY / "general" / "x.md").read_text() == "kept"
    assert (paths.MANAGER_MEMORY / "dlq" / "y.md").read_text() == "kept too"


# --- Multi-manager: SessionEnd distill fallback ---

from dockwright.hooks import session_end as _session_end


def test_session_end_distill_skips_when_memory_already_exists(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """If /manager-close already wrote today's memory file, SessionEnd must not redo distill."""
    _write_fake_transcript(tmp_path, monkeypatch, "mgr-x", [
        {"type": "user", "message": {"content": "go"}},
    ])
    state.write_json_atomic(paths.ACTIVE / "mgr-x.json", {
        "claude_sid": "mgr-x", "agent": "manager", "name": "grumpy-yak",
        "cwd": "/x", "iterm_sid": "i1", "pid": 1, "started_at": 0,
        "domain": "general",
    })
    # Pre-create today's memory file.
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
    # claude -p must NOT have been invoked since the file existed.
    assert not any(args and "claude" in args[0][0] for _label, args, _kw in calls if isinstance(args, tuple) and len(args) > 0 and isinstance(args[0], list))
    # Pre-existing content untouched.
    assert existing.read_text() == "pre-existing from /manager-close"


def test_session_end_distill_runs_when_no_memory_exists(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """cmd+w with no /manager-close must spawn a DETACHED fallback distill —
    never run it in-process, since the SessionEnd hook's 5s budget is far
    shorter than the distill's typical 10-30s `claude -p` round-trip."""
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
    # Stdio contract: DEVNULL keeps the detached child off the dead hook's tty;
    # the log redirection is the only diagnosability the unobserved child has.
    assert kw["stdin"] is subprocess.DEVNULL
    assert kw["stdout"].name.endswith("distill-fallback.log")
    assert kw["stderr"] is kw["stdout"]


# --- Distill child env sanitization (infinite-fan-out regression) ---


def test_distill_subprocess_env_strips_orchestrator_keys(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """The headless `claude -p` distill child must NOT inherit the orchestrator
    session env: an inherited CLAUDE_AGENT=manager makes the child's SessionStart
    hook register a phantom manager record, and its SessionEnd hook re-distill —
    spawning another `claude -p` with the same env, fanning out indefinitely.
    """
    from dockwright.mcp_server import _distill_manager_session
    _write_fake_transcript(tmp_path, monkeypatch, "mgr-x", [
        {"type": "user", "message": {"content": "go"}},
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
    # Sentinel marks the child so the hooks skip it even if a future spawn
    # path forgets to strip the env.
    assert env[paths.DISTILL_ENV_SENTINEL] == "1"
    # Spend-class tag opts the child into SessionEnd headless spend capture.
    assert env["CLAUDE_SPEND_CLASS"] == "distill"
    # Non-orchestrator env is still inherited (claude needs HOME, PATH, etc).
    assert env.get("HOME") == str(tmp_path)


def test_session_end_distill_child_never_redistills(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """SessionEnd for a distill child (sentinel set) must be a no-op — no
    `claude -p` spawned, no memory file written — even when CLAUDE_AGENT=manager
    leaked through and an active record exists for the sid."""
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


# --- spawn_worker stamps CLAUDE_PARENT_MANAGER ---


def test_spawn_worker_stamps_parent_manager_env(fresh_orchestrator_dir, monkeypatch):
    """spawn_worker_impl with manager_sid must inject CLAUDE_PARENT_MANAGER into the worker env."""
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
    """Without manager_sid (back-compat), CLAUDE_PARENT_MANAGER must not be added."""
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="do X",
        name="worker-x",
        cwd="/tmp/x",
    ))
    env = captured.get("env") or {}
    assert "CLAUDE_PARENT_MANAGER" not in env


# --- Full-cycle: parent_manager_name preserved across session_end → resume_worker ---

from dockwright.hooks import session_end as _session_end_h
from dockwright.mcp_server import resume_worker_impl as _resume_worker_mcp


def test_parent_manager_preserved_across_close_and_resume(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """Spawning a worker with a parent, then closing + resuming it, must re-stamp
    CLAUDE_PARENT_MANAGER on the resumed worker's env. Otherwise auto-closed
    (stale_monitor) or cmd+w-closed workers come back wildcard-visible to every
    manager, defeating multi-manager routing."""
    # Step 1: spawn worker with a parent manager via spawn_worker_impl. Use the
    # full path so the CLAUDE_PARENT_MANAGER env is computed from manager_sid.
    become_manager_impl(claude_sid="mgr-a", iterm_sid="i0", domain="general")
    mgr_name = state.read_json(paths.ACTIVE / "mgr-a.json")["name"]
    captured_spawn = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="task",
        name="worker-fc",
        cwd="/tmp/fc",
        manager_sid="mgr-a",
    ))
    assert (captured_spawn.get("env") or {}).get("CLAUDE_PARENT_MANAGER") == mgr_name

    # Step 2: simulate the worker's SessionStart hook writing its active record.
    # (In production the hook reads CLAUDE_PARENT_MANAGER and stamps the record;
    # we write the record directly to keep the test scoped to the
    # session_end → resume hop.)
    state.write_json_atomic(paths.ACTIVE / "wfc-sid.json", {
        "claude_sid": "wfc-sid", "agent": "worker", "name": "worker-fc",
        "cwd": "/tmp/fc", "iterm_sid": "iw", "pid": os.getpid(), "started_at": 0,
        "parent_manager_name": mgr_name,
    })

    # Step 3: trigger session_end on the worker — it must archive the parent into closed/.
    # A live transcript is required so resume_worker's transcript check (Bug 3) accepts it.
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

    # Step 4: resume_worker — assert spawn_worker_tab gets env={CLAUDE_PARENT_MANAGER: mgr_name}.
    captured_resume = _patch_spawn_registers_active(monkeypatch)
    _asyncio.run(_resume_worker_mcp(name="worker-fc", _registration_timeout_sec=2.0, _poll_interval=0.01))
    resumed_env = captured_resume.get("env") or {}
    assert resumed_env.get("CLAUDE_PARENT_MANAGER") == mgr_name, (
        f"resume_worker MUST pass CLAUDE_PARENT_MANAGER={mgr_name!r} from the closed "
        f"record; got env={resumed_env!r}"
    )


# --- Backfill: legacy parent-null workers attributed to the lone manager on boot ---


def test_become_manager_backfills_legacy_workers_when_sole_manager(fresh_orchestrator_dir, capsys):
    """3 parent-null workers + 1 manager booting → all 3 get attributed to that manager."""
    for i in range(3):
        state.write_json_atomic(paths.ACTIVE / f"legacy-{i}.json", {
            "claude_sid": f"legacy-{i}", "agent": "worker", "name": f"old-{i}",
            "cwd": "/x", "iterm_sid": f"i{i}", "pid": os.getpid(), "started_at": 0,
            # parent_manager_name intentionally missing (pre-multi-manager)
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
    """Ambiguous: 3 null-parent workers + 2 managers → leave null + warn on stderr."""
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
    """After first manager boot stamps the workers, a SECOND manager booting must
    not re-backfill (no null parents remain) and the existing stamps must stick."""
    state.write_json_atomic(paths.ACTIVE / "legacy-1.json", {
        "claude_sid": "legacy-1", "agent": "worker", "name": "old-1",
        "cwd": "/x", "iterm_sid": "i1", "pid": os.getpid(), "started_at": 0,
    })
    first = become_manager_impl(claude_sid="mgr-1st", iterm_sid="i9", domain="general")
    stamped_name = state.read_json(paths.ACTIVE / "legacy-1.json")["parent_manager_name"]
    assert stamped_name == first["name"]
    become_manager_impl(claude_sid="mgr-2nd", iterm_sid="i10", domain="dlq")
    # Stamped name must still point to the FIRST manager — second manager's boot
    # is a no-op because the workers no longer have null parents.
    assert state.read_json(paths.ACTIVE / "legacy-1.json")["parent_manager_name"] == first["name"]


def test_resume_reclaims_autoclosed_spend_to_ledger(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """An autoclosed record (closed_reason like 'idle>7200s') has spend only in closed/.
    Resume must ledger it before deleting, so the period is not lost."""
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
    result = _asyncio.run(_resume_worker_mcp(name="idle-worker", _registration_timeout_sec=2.0, _poll_interval=0.01))
    assert result["ok"] is True
    # closed record was deleted
    assert not (paths.CLOSED / "idle-sid.json").exists()
    # ledger has exactly one entry for this sid
    ledger_path = fresh_orchestrator_dir / "spend-ledger.jsonl"
    assert ledger_path.exists(), "ledger file must be created"
    entries = [_json.loads(line) for line in ledger_path.read_text().splitlines() if line.strip()]
    assert len(entries) == 1, f"expected 1 ledger entry, got {len(entries)}: {entries}"
    assert entries[0]["sid"] == "idle-sid"
    assert entries[0]["source"] == "resume_reclaim"


def test_resume_does_not_reledger_session_end_closures(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """session_end-reason closed records were already ledgered at close (Task 3).
    Resume must NOT append them again — doing so would double-count the period."""
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
    result = _asyncio.run(_resume_worker_mcp(name="ended-worker", _registration_timeout_sec=2.0, _poll_interval=0.01))
    assert result["ok"] is True
    # closed record was deleted
    assert not (paths.CLOSED / "ended-sid.json").exists()
    # ledger must remain empty — no double-count
    ledger_path = fresh_orchestrator_dir / "spend-ledger.jsonl"
    if ledger_path.exists():
        entries = [line for line in ledger_path.read_text().splitlines() if line.strip()]
        assert entries == [], f"ledger must be empty for session_end closure; got {entries}"


def test_resume_worker_with_null_parent_omits_env(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """A legacy closed record with no parent must resume without injecting an env."""
    _make_transcript(tmp_path, monkeypatch, "legacy-sid")
    state.write_json_atomic(paths.CLOSED / "legacy-sid.json", {
        "claude_sid": "legacy-sid", "name": "legacy-worker", "cwd": "/tmp/l",
        "iterm_sid": "il", "closed_at": 1.0,
        # parent_manager_name intentionally omitted (legacy record)
    })
    captured = _patch_spawn_registers_active(monkeypatch)
    _asyncio.run(_resume_worker_mcp(name="legacy-worker", _registration_timeout_sec=2.0, _poll_interval=0.01))
    env = captured.get("env")
    # Either env is None (back-compat) or the key is absent — both are acceptable.
    assert env is None or "CLAUDE_PARENT_MANAGER" not in env


# --- resume_worker bug fixes: newest-record selection, verify-before-delete, transcript ---

def _make_transcript(tmp_path, monkeypatch, sid, nonempty=True):
    """Create a ~/.claude/projects/*/<sid>.jsonl under a tmp HOME (so find_session_log
    locates it). Pass nonempty=False to create an empty file (resume would fail)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    projects = tmp_path / ".claude" / "projects" / "-Users-x"
    projects.mkdir(parents=True, exist_ok=True)
    log = projects / f"{sid}.jsonl"
    log.write_text(json.dumps({"type": "assistant", "message": {"content": []}}) if nonempty else "")
    return log


def _make_codex_transcript(tmp_path, monkeypatch, sid, nonempty=True):
    """Create a ~/.codex/sessions/**/rollout-*-<sid>.jsonl for Codex resume."""
    monkeypatch.setenv("HOME", str(tmp_path))
    sessions = tmp_path / ".codex" / "sessions" / "2026" / "05" / "26"
    sessions.mkdir(parents=True, exist_ok=True)
    log = sessions / f"rollout-2026-05-26T10-55-35-{sid}.jsonl"
    log.write_text(json.dumps({"type": "session_meta"}) if nonempty else "")
    return log


def _patch_spawn_registers_active(monkeypatch):
    """spawn_worker_tab mock that ALSO simulates the SessionStart hook registering
    the resumed worker into active/ — so resume_worker's poll-before-delete confirms it.
    Registers under the resume_sid it was given: `--resume <sid>` reuses the session
    id, so that's the sid the real hook writes."""
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
    """Bug 1: duplicate closed records under one name → return the newest (max closed_at),
    not the filesystem-arbitrary first iterdir match."""
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
    """Bugs 1+3 (the tkt-8773 scenario): the NEWER record's transcript is gone (junk
    session); the older record has a live transcript (the healthy session). Must pick
    the healthy one despite it being older."""
    _make_transcript(tmp_path, monkeypatch, "healthy-sid")  # older, live
    # junk-sid: newer closed_at but NO transcript file written
    state.write_json_atomic(paths.CLOSED / "junk-sid.json", {
        "claude_sid": "junk-sid", "name": "tkt-8773", "cwd": "/x", "closed_at": 999.0})
    state.write_json_atomic(paths.CLOSED / "healthy-sid.json", {
        "claude_sid": "healthy-sid", "name": "tkt-8773", "cwd": "/x", "closed_at": 100.0})
    _path, record = _find_closed("tkt-8773")
    assert record["claude_sid"] == "healthy-sid"


def test_find_closed_record_raises_when_no_live_transcript(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """Bug 3: every name-match points at a vanished transcript → raise a clear error
    naming the sids tried, rather than handing back a record that resume can't restore."""
    monkeypatch.setenv("HOME", str(tmp_path))  # empty projects tree → no transcripts
    state.write_json_atomic(paths.CLOSED / "dead-a.json", {
        "claude_sid": "dead-a", "name": "gone", "cwd": "/x", "closed_at": 1.0})
    state.write_json_atomic(paths.CLOSED / "dead-b.json", {
        "claude_sid": "dead-b", "name": "gone", "cwd": "/x", "closed_at": 2.0})
    with pytest.raises(ValueError) as exc:
        _find_closed("gone")
    msg = str(exc.value)
    assert "dead-a" in msg and "dead-b" in msg


def test_resume_worker_keeps_closed_record_when_registration_times_out(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """Bug 2: spawn returns a window id but the resumed session never registers into
    active/ → the closed record MUST survive (recoverable) and resume returns ok:False."""
    _make_transcript(tmp_path, monkeypatch, "stuck-sid")
    state.write_json_atomic(paths.CLOSED / "stuck-sid.json", {
        "claude_sid": "stuck-sid", "name": "stuck", "cwd": "/tmp/s", "closed_at": 1.0})
    _patch_spawn_worker_tab(monkeypatch)  # records kwargs but does NOT register active/
    result = _asyncio.run(_resume_worker_mcp(
        name="stuck", _registration_timeout_sec=0.05, _poll_interval=0.01))
    assert result["ok"] is False
    assert "did not register" in result["reason"]
    assert (paths.CLOSED / "stuck-sid.json").exists(), "closed record must be left intact for retry"


def test_resume_worker_unlinks_closed_record_after_registration_confirmed(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """Bug 2 happy path: once the resumed worker appears in active/, the closed record
    is deleted and resume returns ok:True."""
    _make_transcript(tmp_path, monkeypatch, "good-sid")
    state.write_json_atomic(paths.CLOSED / "good-sid.json", {
        "claude_sid": "good-sid", "name": "good", "cwd": "/tmp/g", "closed_at": 1.0})
    _patch_spawn_registers_active(monkeypatch)
    result = _asyncio.run(_resume_worker_mcp(
        name="good", _registration_timeout_sec=2.0, _poll_interval=0.01))
    assert result["ok"] is True
    assert result["sid"] == "good-sid"
    assert not (paths.CLOSED / "good-sid.json").exists(), "closed record must be deleted on success"


def test_resume_worker_claude_applies_remote_off_settings(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """A resumed claude worker must carry the SAME RC-off --settings as a fresh spawn."""
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    _make_transcript(tmp_path, monkeypatch, "rc-sid")
    state.write_json_atomic(paths.CLOSED / "rc-sid.json", {
        "claude_sid": "rc-sid", "name": "rc-worker", "cwd": "/tmp/rc", "closed_at": 1.0})
    captured = _patch_spawn_registers_active(monkeypatch)
    result = _asyncio.run(_resume_worker_mcp(
        name="rc-worker", _registration_timeout_sec=2.0, _poll_interval=0.01))
    assert result["ok"] is True
    assert (captured.get("extra_args") or [])[:2] == REMOTE_OFF_FLAGS
    assert "--remote-control" not in (captured.get("extra_args") or [])


def test_resume_worker_claude_honors_remote_control_opt_in(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """CLAUDE_ORCH_WORKER_RC=1 → resumed worker gets --remote-control, not the RC-off settings."""
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_RC", "1")
    _make_transcript(tmp_path, monkeypatch, "rc-on-sid")
    state.write_json_atomic(paths.CLOSED / "rc-on-sid.json", {
        "claude_sid": "rc-on-sid", "name": "rc-on-worker", "cwd": "/tmp/rc", "closed_at": 1.0})
    captured = _patch_spawn_registers_active(monkeypatch)
    result = _asyncio.run(_resume_worker_mcp(
        name="rc-on-worker", _registration_timeout_sec=2.0, _poll_interval=0.01))
    assert result["ok"] is True
    assert captured.get("extra_args") == RC_ON_FLAGS
    assert "--remote-control" in (captured.get("extra_args") or [])


def test_resume_worker_codex_skips_claude_remote_flags(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """Codex resume must NOT receive --settings (codex rejects it); extra_args stays falsy."""
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
    """A live session already holds the closed worker's name (e.g. a fresh worker was
    spawned under the same task name after the old one closed) → resume must refuse
    BEFORE spawning. Without the guard, the name-keyed registration poll matches the
    OTHER live session instantly: the closed record is deleted and the result claims
    name=X, while the resumed session actually re-registers as X-2 — so follow-up
    send_manager_to_worker(X) routes to the wrong worker."""
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
    """Managers roll names from the same funny-name pool, so a live manager can hold a
    closed worker's name. The refusal must say a MANAGER holds it — not suggest
    send_manager_to_worker/kill_worker, which are worker-only and would just raise
    "no worker named" for this name."""
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
    """TOCTOU inside the registration window: the pre-flight guard passed (name free),
    but a FOREIGN session registers the name before the resumed session does (e.g. a
    concurrent spawn_worker under the same task name). The poll must NOT confirm on
    the foreign record: confirming would delete the closed record (the only resume
    pointer) and hand back a name that routes to the foreign session."""
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
    result = _asyncio.run(_resume_worker_mcp(
        name="tkt-1234", _registration_timeout_sec=0.1, _poll_interval=0.01))
    assert result["ok"] is False, (
        "a foreign session claiming the name mid-window must not confirm the resume"
    )
    assert (paths.CLOSED / "old-sid.json").exists(), (
        "closed record must survive a foreign name claim"
    )


def test_resume_worker_confirms_via_resumed_sid_and_returns_registered_name(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """`claude --resume <sid>` reuses the session id, so the resumed session
    re-registers as active/<sid>.json. Confirmation is keyed on that sid; if the name
    was stolen mid-window and the hook suffixed it, the result must surface the
    ACTUAL registered handle so follow-up send_manager_to_worker routes correctly."""
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
    result = _asyncio.run(_resume_worker_mcp(
        name="tkt-1234", _registration_timeout_sec=2.0, _poll_interval=0.01))
    assert result["ok"] is True
    assert result["sid"] == "old-sid"
    assert result["name"] == "tkt-1234-2", (
        "must return the registered handle, not the requested name"
    )
    assert not (paths.CLOSED / "old-sid.json").exists()


def test_resume_worker_codex_accepts_new_sid_registration_under_name(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """Codex-lane fallback: if a codex build rolls a fresh thread id on resume, the
    old sid never re-registers. Accept a record that claimed the name and did NOT
    exist pre-spawn, and return the NEW sid (the old one points at nothing live)."""
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
    """The codex-lane name fallback must only accept WORKER registrations: a manager
    appearing mid-window under the claimed name (shared funny-name pool) is not the
    resumed session — confirming on it deletes the closed record and points the
    result at a manager."""
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
    """Two concurrent resume_worker(name) calls would both pass the pre-flight guard
    (nothing registered yet) and spawn TWO tabs resuming the same sid — transcript
    corruption. The second call must refuse while the first is in flight."""
    _make_transcript(tmp_path, monkeypatch, "dup-sid")
    state.write_json_atomic(paths.CLOSED / "dup-sid.json", {
        "claude_sid": "dup-sid", "name": "dup-task", "cwd": "/x", "closed_at": 1.0})
    _patch_spawn_worker_tab(monkeypatch)  # records kwargs; never registers active/

    async def scenario():
        first = _asyncio.create_task(_resume_worker_mcp(
            name="dup-task", _registration_timeout_sec=0.5, _poll_interval=0.01))
        await _asyncio.sleep(0.05)  # first call is now inside its registration poll
        with pytest.raises(ValueError, match="already in progress"):
            await _resume_worker_mcp(
                name="dup-task", _registration_timeout_sec=0.5, _poll_interval=0.01)
        return await first

    result = _asyncio.run(scenario())
    assert result["ok"] is False
    assert (paths.CLOSED / "dup-sid.json").exists()


def test_resume_worker_refuses_when_resume_sid_already_active(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """A live active record under the closed record's OWN sid (but a different name,
    so the name guard passes) means the session is already running — spawning
    `--resume <sid>` again would attach a second process to the same transcript, and
    a sid-keyed poll would instantly false-confirm on the pre-existing record."""
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


# --- Worker-slot semaphore -------------------------------------------------

from dockwright.mcp_server import (
    acquire_worker_slot_impl,
    release_worker_slot_impl,
)


def _register_worker(sid: str, name: str = "w", pid: int | None = None) -> None:
    """Helper: register an active worker so acquire's liveness check passes."""
    register_self_impl(
        claude_sid=sid,
        agent="worker",
        name=name,
        cwd="/tmp",
        iterm_sid="i",
        pid=pid if pid is not None else os.getpid(),
    )


def test_acquire_worker_slot_succeeds_under_cap(fresh_orchestrator_dir):
    _register_worker("sid-A", name="A")
    _register_worker("sid-B", name="B")
    r1 = acquire_worker_slot_impl(claude_sid="sid-A", category="mvn", max_concurrent=3)
    r2 = acquire_worker_slot_impl(claude_sid="sid-B", category="mvn", max_concurrent=3)
    assert "slot_id" in r1 and "slot_id" in r2
    assert r1["slot_id"] != r2["slot_id"]


def test_acquire_worker_slot_blocks_at_cap(fresh_orchestrator_dir):
    for n in ("A", "B", "C"):
        _register_worker(f"sid-{n}", name=n)
        acquire_worker_slot_impl(claude_sid=f"sid-{n}", category="mvn", max_concurrent=3)
    _register_worker("sid-D", name="D")
    with pytest.raises(TimeoutError):
        acquire_worker_slot_impl(
            claude_sid="sid-D", category="mvn", max_concurrent=3, timeout_sec=1
        )


def test_release_worker_slot_frees_one(fresh_orchestrator_dir):
    slot_ids = []
    for n in ("A", "B", "C"):
        _register_worker(f"sid-{n}", name=n)
        slot_ids.append(
            acquire_worker_slot_impl(
                claude_sid=f"sid-{n}", category="mvn", max_concurrent=3
            )["slot_id"]
        )
    release_worker_slot_impl(slot_id=slot_ids[1])
    _register_worker("sid-D", name="D")
    result = acquire_worker_slot_impl(
        claude_sid="sid-D", category="mvn", max_concurrent=3, timeout_sec=2
    )
    assert "slot_id" in result


def test_release_worker_slot_idempotent(fresh_orchestrator_dir):
    _register_worker("sid-A", name="A")
    slot = acquire_worker_slot_impl(claude_sid="sid-A", category="mvn", max_concurrent=3)
    r1 = release_worker_slot_impl(slot_id=slot["slot_id"])
    r2 = release_worker_slot_impl(slot_id=slot["slot_id"])
    assert r1["released"] is True
    assert r2["released"] is True


def test_acquire_evicts_stale_holders(fresh_orchestrator_dir):
    import json
    # Pre-seed a slot file with a holder whose claude_sid has no active record
    # AND whose pid is dead. acquire should evict it and grant.
    (paths.SLOTS).mkdir(parents=True, exist_ok=True)
    (paths.SLOTS / "mvn.json").write_text(json.dumps({
        "max_concurrent": 1,
        "holders": [{
            "slot_id": "stale-1",
            "claude_sid": "ghost-sid",
            "acquired_at": 0.0,
            "pid": 999999,  # almost certainly dead
        }],
    }))
    _register_worker("sid-A", name="A")
    result = acquire_worker_slot_impl(
        claude_sid="sid-A", category="mvn", max_concurrent=1, timeout_sec=2
    )
    assert "slot_id" in result and result["slot_id"] != "stale-1"


def test_env_var_overrides_default_count(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_SLOTS_MVN", "5")
    # Acquire 5 with max_concurrent omitted; the env var should set the cap.
    for n in range(5):
        _register_worker(f"sid-{n}", name=f"W{n}")
        acquire_worker_slot_impl(claude_sid=f"sid-{n}", category="mvn")
    _register_worker("sid-X", name="X")
    with pytest.raises(TimeoutError):
        acquire_worker_slot_impl(claude_sid="sid-X", category="mvn", timeout_sec=1)


def test_concurrent_acquires_serialize_safely(fresh_orchestrator_dir):
    import threading
    _register_worker("sid-A", name="A")
    _register_worker("sid-B", name="B")
    results: list = []
    errors: list = []

    def grab(sid):
        try:
            results.append(
                acquire_worker_slot_impl(
                    claude_sid=sid, category="mvn", max_concurrent=2, timeout_sec=5
                )
            )
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=grab, args=("sid-A",))
    t2 = threading.Thread(target=grab, args=("sid-B",))
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert not errors
    assert len(results) == 2
    ids = {r["slot_id"] for r in results}
    assert len(ids) == 2


# --- kill_worker graceful-close path --------------------------------------
# kill_worker_impl closes the worker's tmux window so Claude Code's SessionEnd
# hook fires (which in turn fires selffix-trigger.sh + orchestrator session-
# end's closed/<sid>.json archive). No SIGTERM, no manual selffix trigger.

def test_kill_worker_closes_window_instead_of_sigterm(fresh_orchestrator_dir, monkeypatch):
    """kill_worker_impl must close the worker's tmux window/pane — that hands the
    SIGHUP→grace→SIGKILL sequence to Claude Code, giving its SessionEnd hooks
    time to run. No `os.kill(SIGTERM)`, no `_trigger_selffix_for_outgoing_session`.
    """
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
    """If the worker process is already gone, there's nothing to close — return
    `already_dead=True` and skip the close call entirely.
    """
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
    """A terminal close-window subprocess failure must NOT propagate — the helper
    `_close_window` swallows internally, and `kill_worker_impl` must use
    it (not re-raise).
    """
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=12345)

    def boom(*a, **k):
        raise OSError("tmux server gone")
    monkeypatch.setattr("dockwright.mcp_server.subprocess.run", boom)
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)

    # No exception should escape.
    result = kill_worker_impl(worker="alpha", dry_run=False)
    assert "killed_pid" in result


# === Part A: manager records the pane id via env-inherit (with param override) ===

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


# === Part B: _input_is_idle parser ===

from dockwright.mcp_server import _input_is_idle


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
    # Claude Code empty-box ghost-text: faint (\x1b[2m) placeholder after the caret.
    screen = ("output above\n"
              "\x1b[39m❯ \x1b[2mSpawn a worker to investigate the codebase\x1b[0m\n"
              "  ? for shortcuts")
    assert _input_is_idle(screen) is True


def test_input_is_idle_ansi_typed_input_is_busy():
    # Real typed input is normal-intensity (no faint span) after the caret.
    assert _input_is_idle("\x1b[39m❯ \x1b[39mdo the migration first\x1b[0m") is False


def test_input_is_idle_ansi_empty_box_is_idle():
    # ANSI-captured but genuinely empty box (no placeholder, no typed text).
    assert _input_is_idle("\x1b[39m❯ \x1b[0m") is True


def test_input_is_idle_bare_reset_terminates_dim_span():
    # Faint span closed by the bare reset \x1b[m (no params) still reads as empty.
    assert _input_is_idle("\x1b[39m❯ \x1b[2msuggestion text\x1b[m") is True


def test_input_is_idle_bare_reset_ends_faint_so_later_text_is_busy():
    # The bare reset \x1b[m must END the faint span: normal-intensity text typed
    # AFTER it survives the dim-strip → busy. (Strict check of the empty-param branch.)
    assert _input_is_idle("\x1b[39m❯ \x1b[2mghost\x1b[mREAL") is False


# === Part C: _capture_text uses ANSI capture ===

def test_capture_text_uses_ansi_capture(monkeypatch):
    import dockwright.mcp_server as srv

    class _FakeDriver:
        def capture_screen(self, wid):
            raise AssertionError("must use ANSI capture, not plain capture_screen")

        def capture_screen_ansi(self, wid):
            return f"ansi:{wid}"

    monkeypatch.setattr(srv, "get_driver", lambda: _FakeDriver())
    assert srv._capture_text("%9") == "ansi:%9"


# === Part D: send_manager_to_manager (manager <-> manager) ===
#
# DIRECT + idle guard, loud on failure. When the peer's input box is idle, type the
# message CONTENT directly (delivered_live). When a human is mid-typing, do NOT type
# and return peer_busy (no clobber, no inbox). No live window → RAISE.

from dockwright.mcp_server import send_manager_to_manager_impl


def test_send_manager_to_manager_idle_delivers(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv
    state.write_json_atomic(paths.ACTIVE / "m2.json", {
        "claude_sid": "m2", "agent": "manager", "name": "peer", "window_id": "9"})
    monkeypatch.setattr(srv, "_capture_text", lambda wid: "❯ ")
    monkeypatch.setattr(srv, "_send_text", lambda wid, txt: None)
    assert srv.send_manager_to_manager_impl("peer", "hi")["status"] == "delivered_live"


def test_send_manager_to_manager_busy_returns_peer_busy_no_inbox(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv
    state.write_json_atomic(paths.ACTIVE / "m2.json", {
        "claude_sid": "m2", "agent": "manager", "name": "peer", "window_id": "9"})
    monkeypatch.setattr(srv, "_capture_text", lambda wid: "❯ writing a reply...")
    typed = []
    monkeypatch.setattr(srv, "_send_text", lambda wid, txt: typed.append(txt))
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
    monkeypatch.setattr(srv, "_capture_text", lambda wid: None)  # unreadable
    with pytest.raises(ValueError, match="unreadable"):
        srv.send_manager_to_manager_impl("peer", "hi")


def test_send_manager_to_manager_resolves_and_stamps_back(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv
    state.write_json_atomic(paths.ACTIVE / "m2.json", {
        "claude_sid": "m2", "agent": "manager", "name": "peer", "window_id": ""})
    monkeypatch.setattr(srv, "_resolve_manager_window", lambda *a, **k: "77")
    monkeypatch.setattr(srv, "_capture_text", lambda wid: "❯ ")
    sent = {}
    monkeypatch.setattr(srv, "_send_text", lambda wid, txt: sent.update(wid=wid))
    r = srv.send_manager_to_manager_impl("peer", "hi")
    assert r["status"] == "delivered_live" and sent["wid"] == "77"
    assert state.read_json(paths.ACTIVE / "m2.json")["window_id"] == "77"  # stamped back


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
    # Empty box showing a faint rotating placeholder — must NOT read as busy.
    monkeypatch.setattr(srv, "_capture_text",
                        lambda wid: "\x1b[39m❯ \x1b[2mSpawn a worker to investigate\x1b[0m")
    typed = []
    monkeypatch.setattr(srv, "_send_text", lambda wid, txt: typed.append(txt))
    r = srv.send_manager_to_manager_impl("peer", "hi")
    assert r["status"] == "delivered_live" and typed == ["hi"]


def test_send_manager_to_manager_ansi_typed_input_is_busy(fresh_orchestrator_dir, monkeypatch):
    import dockwright.mcp_server as srv
    state.write_json_atomic(paths.ACTIVE / "m2.json", {
        "claude_sid": "m2", "agent": "manager", "name": "peer", "window_id": "9"})
    monkeypatch.setattr(srv, "_capture_text",
                        lambda wid: "\x1b[39m❯ \x1b[39mhalf a thought\x1b[0m")
    typed = []
    monkeypatch.setattr(srv, "_send_text", lambda wid, txt: typed.append(txt))
    r = srv.send_manager_to_manager_impl("peer", "hi")
    assert r["status"] == "peer_busy" and r["delivered"] is False and typed == []


def test_kill_worker_does_not_match_manager(fresh_orchestrator_dir):
    """kill_worker targets workers only. Managers share the funny-name pool with
    worker display names, so an unfiltered name match would close a peer manager's
    (or the caller's own) tmux window. The refusal must NAME the manager holder —
    a bare "no worker named X" reads as a typo when X visibly exists in
    list_managers."""
    register_self_impl(claude_sid="m1", agent="manager", name="happy-yak", cwd="/x", iterm_sid="i1", pid=12345)
    with pytest.raises(ValueError, match="active manager") as exc:
        kill_worker_impl(worker="happy-yak", dry_run=True)
    assert "no worker named" not in str(exc.value)


def test_send_manager_to_worker_does_not_match_manager(fresh_orchestrator_dir, monkeypatch):
    """send_manager_to_worker must not resolve a manager: it types directly with NO
    idle guard, while manager panes are guarded (a human may be mid-typing there) —
    that's what send_manager_to_manager exists for. The error must point there."""
    register_self_impl(claude_sid="m1", agent="manager", name="happy-yak", cwd="/x", iterm_sid="42")
    typed = []
    monkeypatch.setattr(
        "dockwright.mcp_server._send_text",
        lambda wid, text: typed.append((wid, text)),
    )
    with pytest.raises(ValueError, match="active manager") as exc:
        send_manager_to_worker_impl(worker="happy-yak", text="hi")
    assert "send_manager_to_manager" in str(exc.value)
    assert typed == []


def test_worker_finder_names_manager_holder_by_sid_too(fresh_orchestrator_dir):
    """The finder matches by name OR sid; targeting a manager's SID must get the
    same holder-naming refusal, and a genuinely unknown id keeps the plain
    "no worker named" message."""
    register_self_impl(claude_sid="mgr-sid-1", agent="manager", name="happy-yak", cwd="/x", iterm_sid="i1", pid=12345)
    with pytest.raises(ValueError, match="active manager"):
        kill_worker_impl(worker="mgr-sid-1", dry_run=True)
    with pytest.raises(ValueError, match="no worker named 'ghost'"):
        kill_worker_impl(worker="ghost", dry_run=True)


# === Part D2: _send_text direct-typing helper (bracketed paste) ===

from dockwright.mcp_server import _send_text


def test_send_text_uses_bracketed_paste_then_single_enter(fresh_orchestrator_dir, monkeypatch):
    """Content is loaded into a buffer and pasted bracketed (-p), then exactly ONE Enter."""
    terminal._DRIVER = None
    calls = []
    monkeypatch.setattr(
        "dockwright.mcp_server.subprocess.run",
        lambda args, **kw: calls.append((list(args), kw)),
    )
    _send_text("42", "do the migration first")
    # set-option pin + load-buffer + paste-buffer + send-keys Enter
    assert len(calls) == 4
    load = next(a for a, _ in calls if "load-buffer" in a)
    paste = next(a for a, _ in calls if "paste-buffer" in a)
    enters = [a for a, _ in calls if "send-keys" in a and a[-1] == "Enter"]
    # Content goes through stdin as a single buffer payload — never a positional arg.
    load_kw = next(kw for a, kw in calls if "load-buffer" in a)
    assert load_kw["input"] == b"do the migration first"
    assert "do the migration first" not in load
    # Bracketed paste (-p) scoped to the target pane.
    assert "-p" in paste and "-t" in paste and paste[paste.index("-t") + 1] == "42"
    # Exactly one Enter, scoped to the pane.
    assert len(enters) == 1
    assert enters[0][enters[0].index("-t") + 1] == "42"


def test_send_text_multiline_arrives_whole(fresh_orchestrator_dir, monkeypatch):
    """A multi-line message must reach the window as ONE bracketed-paste payload with
    every newline preserved, and trigger exactly ONE Enter — so the embedded newlines
    insert as text instead of submitting and fragmenting the message."""
    terminal._DRIVER = None
    calls = []
    monkeypatch.setattr(
        "dockwright.mcp_server.subprocess.run",
        lambda args, **kw: calls.append((list(args), kw)),
    )
    multiline = "line one\nline two\n\nline four with trailing"
    _send_text("7", multiline)
    # The entire multi-line message is delivered as a single buffer payload, whole,
    # pasted bracketed (-p) so newlines arrive as text, never as submit.
    load_kw = next(kw for a, kw in calls if "load-buffer" in a)
    assert load_kw["input"] == multiline.encode("utf-8")
    paste = next(a for a, _ in calls if "paste-buffer" in a)
    assert "-p" in paste
    # Exactly one Enter overall, and no extra shell-outs — no per-line submit.
    enter_calls = [a for a, _ in calls if "send-keys" in a and a[-1] == "Enter"]
    assert len(enter_calls) == 1
    assert len(calls) == 4  # set-option pin + load-buffer + paste-buffer + send-keys


def test_send_text_swallows_failure(fresh_orchestrator_dir, monkeypatch):
    def boom(args, **kw):
        raise FileNotFoundError("tmux not installed")

    monkeypatch.setattr("dockwright.mcp_server.subprocess.run", boom)
    _send_text("42", "hi")  # must not raise


# === Part E: close-on-takeover (present id, ls-fallback, no-resolution) ===

def test_become_manager_with_takeover_skips_close_when_no_window_resolves(fresh_orchestrator_dir, monkeypatch):
    """Legacy predecessor (empty iterm_sid) AND the terminal ls resolves nothing →
    skip the close silently (no close-window subprocess call)."""
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
    """Legacy predecessor (empty iterm_sid): resolve its window via the terminal
    ls by the manager name in the title and close THAT window (not the incoming
    manager's)."""
    terminal._DRIVER = None
    monkeypatch.delenv("TMUX_PANE", raising=False)
    old = become_manager_impl(claude_sid="mgr-old", iterm_sid="")
    old_name = old["name"]
    handoff = prepare_handoff_impl(claude_sid="mgr-old", narrative_summary="state", trigger_reason="manual")
    monkeypatch.setattr("dockwright.mcp_server._pid_alive", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)
    # tmux list-panes tree: the predecessor's window (title carries its name) + the incoming
    # manager's own window (must be excluded).
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
    assert closed == ["7"]  # predecessor's window resolved by name, incoming (9) excluded


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
    """Pass-2 title match runs whether or not exclude_id is present.  The
    no-exclude_id caller is send_manager_to_manager (a SEND, not a close) so
    matching the peer's own titled window is the correct intent; active manager
    names are unique enough that a cross-manager prefix collision is not a risk.
    Both exclude_id='' and exclude_id=other-window should resolve."""
    from dockwright.mcp_server import _resolve_manager_window
    tree = [
        {"tabs": [
            {"title": "grumpy-yak · general", "windows": [
                {"id": 5, "title": "grumpy-yak · general", "env": {}},
            ]},
        ]},
    ]
    monkeypatch.setattr("dockwright.mcp_server._terminal_ls", lambda: tree)
    # No exclude_id → name pass still fires → resolves.
    assert _resolve_manager_window("some-sid", "grumpy-yak", exclude_id="") == "5"
    # With an exclude_id that isn't this window → name pass runs → resolves.
    assert _resolve_manager_window("some-sid", "grumpy-yak", exclude_id="9") == "5"


def test_kill_worker_resolves_window_id_records(fresh_orchestrator_dir, monkeypatch):
    """A worker record written by the NEW code (only window_id) resolves
    via state.window_id_of — proves the helper is on the read path."""
    state.write_json_atomic(paths.ACTIVE / "new-sid.json", {
        "claude_sid": "new-sid",
        "agent": "worker",
        "name": "new-worker",
        "cwd": "/x",
        "window_id": "new-win-1",  # new key only
        "pid": os.getpid(),
        "started_at": 0,
        "state": "idle",
        "parent_manager_name": None,
    })
    result = kill_worker_impl(worker="new-worker", dry_run=True)
    assert result["iterm_sid"] == "new-win-1"  # external return key stays iterm_sid; helper resolved it


def test_kill_worker_resolves_legacy_iterm_sid_records(fresh_orchestrator_dir, monkeypatch):
    """A pre-rename worker record (only legacy iterm_sid key) still resolves
    via the helper's fallback. Regression guard — locks in dual-key reads."""
    state.write_json_atomic(paths.ACTIVE / "legacy-sid.json", {
        "claude_sid": "legacy-sid",
        "agent": "worker",
        "name": "legacy-worker",
        "cwd": "/x",
        "iterm_sid": "leg-win-1",  # legacy key only
        "pid": os.getpid(),
        "started_at": 0,
        "state": "idle",
        "parent_manager_name": None,
    })
    result = kill_worker_impl(worker="legacy-worker", dry_run=True)
    assert result["iterm_sid"] == "leg-win-1"


def test_list_managers_returns_iterm_sid_from_window_id_records(fresh_orchestrator_dir):
    """become_manager writes the persistent JSON with the new `window_id` key, but
    list_managers' external return shape MUST keep `iterm_sid` (caller stability per
    spec). Regression guard: list_managers reads via state.window_id_of so the
    returned `iterm_sid` value carries the manager's actual window id, not None."""
    from dockwright.mcp_server import list_managers
    become_manager_impl(claude_sid="mgr-1", iterm_sid="win-9", domain="general")
    out = list_managers()
    assert len(out) == 1
    assert out[0]["claude_sid"] == "mgr-1"
    assert out[0]["iterm_sid"] == "win-9"


# --- spawn_worker manager_sid resolution: unresolvable sid → UNSCOPED warning ---

def test_spawn_worker_warns_on_unresolvable_manager_sid(fresh_orchestrator_dir, monkeypatch):
    """A non-empty manager_sid with no ACTIVE record (e.g. the funny NAME passed
    instead of the session UUID) must still spawn, but UNSCOPED and with a warning."""
    captured = _patch_spawn_worker_tab(monkeypatch)
    result = _asyncio.run(spawn_worker_impl(
        initial_prompt="hi",
        name="worker-unscoped",
        manager_sid="snug-ibex",  # funny name, not a registered sid
    ))
    assert result["parent_manager_name"] is None
    assert "warning" in result
    assert "snug-ibex" in result["warning"]
    assert "UNSCOPED" in result["warning"]
    assert "CLAUDE_PARENT_MANAGER" not in (captured.get("env") or {})


def test_spawn_worker_no_warning_on_none_manager_sid(fresh_orchestrator_dir, monkeypatch):
    """manager_sid=None is the intentional legacy single-manager wildcard — no warning."""
    _patch_spawn_worker_tab(monkeypatch)
    result = _asyncio.run(spawn_worker_impl(
        initial_prompt="hi",
        name="worker-legacy",
        manager_sid=None,
    ))
    assert result["parent_manager_name"] is None
    assert "warning" not in result


def test_spawn_worker_no_warning_on_resolvable_manager_sid(fresh_orchestrator_dir, monkeypatch):
    """A resolvable manager_sid keeps the existing behavior: scoped, no warning."""
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
    # falsy manager_sid → intentional legacy wildcard, no warning
    assert _resolve_parent_manager(None) == (None, None)
    assert _resolve_parent_manager("") == (None, None)
    # resolvable sid → name, no warning (resolvable-path behavior unchanged)
    resolved_name, resolved_warning = _resolve_parent_manager("mgr-a")
    assert resolved_name == mgr_name
    assert resolved_warning is None
    # truthy but unresolvable sid → None + warning
    unscoped_name, unscoped_warning = _resolve_parent_manager("snug-ibex")
    assert unscoped_name is None
    assert unscoped_warning is not None
    assert "UNSCOPED" in unscoped_warning


def test_resolve_manager_name_for_filter_warns_to_stderr_on_unresolvable_sid(fresh_orchestrator_dir, capsys):
    """The READ/filter helper degrades to wildcard when a manager_sid can't resolve;
    it must WARN to stderr so the silent "returns every manager's records" degradation
    is visible. Resolvable + falsy sids stay silent."""
    from dockwright.mcp_server import _resolve_manager_name_for_filter
    become_manager_impl(claude_sid="mgr-a", iterm_sid="i0", domain="general")
    mgr_name = state.read_json(paths.ACTIVE / "mgr-a.json")["name"]

    # Resolvable sid → returns name, no warning.
    assert _resolve_manager_name_for_filter("mgr-a", "list_workers") == mgr_name
    assert capsys.readouterr().err == ""

    # Falsy sid → intentional wildcard, no warning.
    assert _resolve_manager_name_for_filter(None, "list_workers") is None
    assert capsys.readouterr().err == ""

    # Truthy but unresolvable (funny name passed instead of UUID) → None + stderr warning
    # that names the tool, the bad sid, and the wildcard degradation.
    assert _resolve_manager_name_for_filter("snug-ibex", "list_workers") is None
    err = capsys.readouterr().err
    assert "list_workers" in err
    assert "snug-ibex" in err
    assert "wildcard" in err


def test_takeover_inherits_funny_name_and_preserves_worker_routing(fresh_orchestrator_dir, monkeypatch):
    """Recreate/takeover keeps the predecessor's funny name so a worker parented to
    that name still resolves to the recreated manager (parent_manager_name stays valid)."""
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
    # Make the old manager look dead so the takeover's _pid_alive guard short-circuits.
    old = state.read_json(paths.ACTIVE / "mgr-old.json")
    old["pid"] = 2
    state.write_json_atomic(paths.ACTIVE / "mgr-old.json", old)
    become_manager_with_takeover_impl(
        claude_sid="mgr-new", takeover_from="mgr-old",
        handoff_id=handoff["handoff_id"], iterm_sid="i-new",
    )
    # The recreated manager keeps the inherited funny name (never "manager").
    assert state.read_json(paths.ACTIVE / "mgr-new.json")["name"] == "happy-otter"
    # The worker parented to the old name still resolves to the recreated manager.
    workers = list_workers_impl(manager_name="happy-otter")
    assert [w["name"] for w in workers] == ["task-1"]


# --- Artifact store: document plane (docs/orchestrator-artifact-store-spec-v2.md Part I) ---

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


# --- F1: task_key canonical param + ticket deprecated alias on the 6 tools ---

from dockwright.mcp_server import (
    artifact_put as _tool_artifact_put, artifact_get as _tool_artifact_get,
    artifact_list as _tool_artifact_list, artifact_view as _tool_artifact_view,
    pipeline_status as _tool_pipeline_status, pipeline_event as _tool_pipeline_event,
)


def test_artifact_put_accepts_task_key_and_ticket_alias(fresh_orchestrator_dir):
    # task_key (canonical) writes under the key
    _tool_artifact_put(task_key="K-1", phase="spec", name="srs", content="a",
                       status="complete", writer_sid="s1")
    assert artifact_get_impl("K-1", "spec", "srs")["content"] == "a"
    # ticket (deprecated alias) still works for one release
    _tool_artifact_put(ticket="K-2", phase="spec", name="srs", content="b",
                       status="complete", writer_sid="s1")
    assert artifact_get_impl("K-2", "spec", "srs")["content"] == "b"
    # both given -> task_key wins
    _tool_artifact_put(task_key="K-3", ticket="K-2", phase="spec", name="srs",
                       content="c", status="complete", writer_sid="s1")
    assert artifact_get_impl("K-3", "spec", "srs")["content"] == "c"
    # neither given -> fail fast (mirrors the old required-param behavior)
    with pytest.raises(ValueError):
        _tool_artifact_put(phase="spec", name="srs", content="d",
                           status="complete", writer_sid="s1")


def test_artifact_read_tools_accept_task_key_and_ticket_alias(fresh_orchestrator_dir):
    artifact_put_impl("K-1", "spec", "srs", "body", "complete", "s1")
    assert _tool_artifact_get(task_key="K-1", phase="spec", name="srs")["content"] == "body"
    assert _tool_artifact_get(ticket="K-1", phase="spec", name="srs")["content"] == "body"
    assert _tool_artifact_list(task_key="K-1")[0]["name"] == "srs"
    assert _tool_artifact_list(ticket="K-1")[0]["name"] == "srs"
    assert "K-1" in _tool_artifact_view(task_key="K-1")
    assert "K-1" in _tool_pipeline_status(ticket="K-1")
    for tool in (_tool_artifact_get, _tool_artifact_list, _tool_artifact_view,
                 _tool_pipeline_status):
        with pytest.raises(ValueError):
            tool() if tool in (_tool_artifact_list, _tool_artifact_view,
                               _tool_pipeline_status) else tool(phase="spec", name="srs")


def test_pipeline_event_accepts_task_key_and_ticket_alias(fresh_orchestrator_dir):
    _tool_pipeline_event(task_key="K-1", type="note", reason="via task_key")
    _tool_pipeline_event(ticket="K-1", type="note", reason="via ticket")
    lines = [json.loads(l) for l in
             paths.artifact_events_path("K-1").read_text().splitlines()]
    assert [l["reason"] for l in lines if l["type"] == "note"] == ["via task_key", "via ticket"]
    with pytest.raises(ValueError):
        _tool_pipeline_event(type="note")


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


# --- Artifact store: folds (spec §6) ---

from dockwright.mcp_server import (
    _join_worker_liveness, pipeline_status_impl, artifact_view_impl, pipeline_event_impl,
)


def test_pipeline_status_joins_liveness(fresh_orchestrator_dir):
    # active writer
    register_self_impl(claude_sid="sid-live", agent="worker", name="w-live", cwd="/x", iterm_sid="i1")
    artifact_put_impl("TKT-SANDBOX-1", "spec", "a", "x", "complete", "sid-live")
    # done writer — event in a PER-MANAGER bucket (regression for v1's done_dir_for(None) bug)
    done_dir = paths.done_dir_for("mgr-name")
    done_dir.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(done_dir / "sid-done-ev1.json",
                            {"event_id": "ev1", "claude_sid": "sid-done", "summary": "ok"})
    artifact_put_impl("TKT-SANDBOX-1", "spec", "b", "x", "complete", "sid-done")
    out = pipeline_status_impl("TKT-SANDBOX-1")
    assert "(active)" in out and "(done)" in out


def test_join_liveness_runtime_from_closed_record(fresh_orchestrator_dir):
    # codex worker: done event AND closed record — runtime must come from closed/, not default
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
    # parse_artifact deliberately SKIPS corrupt frontmatter lines (state.py),
    # so a stamp can lose phase/name; the view's recovery branch rebuilds them
    # from the <phase>.<name>.md filename. Pre-fix that branch raised
    # NameError (Path never imported in mcp_server) and aborted the whole fold
    # (orch-audit finding 3).
    artifact_put_impl("TKT-SANDBOX-1", "spec", "good", "GOOD-body", "complete", "s1")
    artifact_put_impl("TKT-SANDBOX-1", "review", "bad", "BAD-body", "complete", "s2")
    bad_path = paths.artifact_path("TKT-SANDBOX-1", "review", "bad")
    text = bad_path.read_text()
    text = text.replace('phase: "review"', "phase: {corrupt")
    text = text.replace('name: "bad"', "name: {corrupt")
    bad_path.write_text(text)
    out = artifact_view_impl("TKT-SANDBOX-1")          # must not raise
    assert "spec.good" in out and "GOOD-body" in out
    assert "review.bad" in out and "BAD-body" in out


def test_events_reader_skips_malformed_trailing_line(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "spec", "a", "x", "complete", "s1")
    with open(paths.artifact_events_path("TKT-SANDBOX-1"), "a") as f:
        f.write('{"type":"note","trunc')          # simulated crash mid-append
    out = pipeline_status_impl("TKT-SANDBOX-1")            # must not raise
    assert "artifact_put" in out


def test_pipeline_event_appends(fresh_orchestrator_dir):
    pipeline_event_impl("TKT-SANDBOX-1", "dispatch", phase="implement", name="srs",
                        reason="fan-out", actor_sid="mgr-1")
    out = pipeline_status_impl("TKT-SANDBOX-1")
    assert "dispatch" in out and "fan-out" in out


# --- Artifact store: retention (spec §9) ---

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
    _age(stale_tmp, 1)                      # > 1h
    _prune_stale_artifacts()
    assert not stale_tmp.exists() and fresh_tmp.exists()


# --- Ownership plane: spawn-path pending assignment (spec §11, §12) ---

from dockwright.mcp_server import _derive_ticket, _current_branch


@pytest.fixture
def configured_key_regex(monkeypatch, tmp_path):
    """Point config discovery at a temp dockwright.toml carrying an operator's
    Jira-style key regex, so `_derive_ticket`/spawn derive keys exactly as an
    operator's deployment would — independent of the ambient ~/.claude config
    (DOCKWRIGHT_CONFIG is authoritative)."""
    p = tmp_path / "dockwright.toml"
    p.write_text("[task_keys]\nkey_regex = '[A-Za-z]{2,}-\\d+'\n")
    monkeypatch.setenv(_config.ENV_CONFIG_PATH, str(p))
    return p


@pytest.fixture
def no_orch_config(monkeypatch, tmp_path):
    """Authoritative 'no config': DOCKWRIGHT_CONFIG points at a nonexistent file,
    so config falls to generic defaults (no key derivation, no [spawn.env]) even
    on a machine whose ~/.claude/dockwright.toml is populated."""
    monkeypatch.setenv(_config.ENV_CONFIG_PATH, str(tmp_path / "nope.toml"))


def test_spawn_worker_writes_pending_assignment(fresh_orchestrator_dir, monkeypatch,
                                                configured_key_regex):
    captured = _patch_spawn_worker_tab(monkeypatch)
    register_self_impl(claude_sid="mgr-1", agent="manager", name="boss", cwd="/x", iterm_sid="i9")
    _asyncio.run(spawn_worker_impl(
        initial_prompt="/ticket-start TKT-8353 extra context",
        name="tkt-8353-dlq-fix", cwd="/tmp", manager_sid="mgr-1"))
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
    # Generic default: no [task_keys] key_regex -> a Jira-shaped reference in the
    # prompt is NOT auto-derived, and a keyless spawn gets no artifact footer.
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="/ticket-start TKT-8353 extra", name="w1", cwd="/tmp"))
    (pending,) = list(paths.ASSIGNMENTS_PENDING.glob("*.json"))
    assert state.read_json(pending)["ticket"] is None
    assert "[orchestrator] Artifact discipline" not in captured["initial_prompt"]


def test_spawn_env_injected_from_config(fresh_orchestrator_dir, monkeypatch, tmp_path):
    # [spawn.env] entries land in the spawned claude worker's env; a caller-supplied
    # value for the same key still wins.
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text('[spawn.env]\nFOO = "bar"\nSHARED = "from-config"\n')
    monkeypatch.setenv(_config.ENV_CONFIG_PATH, str(cfg))
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="task", name="w1", cwd="/tmp", env={"SHARED": "from-caller"}))
    assert captured["env"]["FOO"] == "bar"
    assert captured["env"]["SHARED"] == "from-caller"   # caller env wins over config


def test_spawn_env_absent_by_default(fresh_orchestrator_dir, monkeypatch, no_orch_config):
    # No [spawn.env] -> nothing extra is injected; in particular the former
    # hardcoded SUPERPOWERS_AUTONOMOUS is gone from the generic default.
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(initial_prompt="task", name="w1", cwd="/tmp"))
    assert "SUPERPOWERS_AUTONOMOUS" not in captured["env"]


def test_spawn_env_absent_by_default_codex_unaffected(fresh_orchestrator_dir, monkeypatch,
                                                      tmp_path):
    # [spawn.env] is claude-only; codex has its own protocol and is excluded.
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text('[spawn.env]\nFOO = "bar"\n')
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
    assert record["initial_prompt"] == "the ask"        # NOT the expanded prompt
    assert record["preset"] == "boiler"
    assert "BOILERPLATE" in captured["initial_prompt"]  # expansion still reaches the tab


def test_spawn_failure_unlinks_pending(fresh_orchestrator_dir, monkeypatch):
    async def boom(**kwargs):
        raise OSError("tmux down")
    monkeypatch.setattr(spawner, "spawn_worker_tab", boom)
    with pytest.raises(RuntimeError):
        _asyncio.run(spawn_worker_impl(initial_prompt="x", name="w1", cwd="/tmp"))
    assert list(paths.ASSIGNMENTS_PENDING.glob("*.json")) == []


def test_derive_ticket_from_name_or_prompt(configured_key_regex):
    assert _derive_ticket("tkt-8353-dlq-fix", "free text") == "TKT-8353"
    assert _derive_ticket("w1", "/ticket-start TKT-99") == "TKT-99"
    assert _derive_ticket("w1", "no key here") is None


def test_derive_ticket_none_without_config(no_orch_config):
    # Generic default: no configured key_regex -> nothing is derived, even from a
    # Jira-shaped reference. Explicit task_key is then the only keying path.
    assert _derive_ticket("x", "the ask TKT-8353") is None
    assert _derive_ticket("tkt-8353-dlq-fix", "free text") is None


def test_derive_ticket_with_configured_regex(configured_key_regex):
    assert _derive_ticket("w1", "the ask TKT-8353") == "TKT-8353"


def test_derive_ticket_invalid_regex_falls_to_none(monkeypatch, tmp_path):
    # A malformed operator regex must fail-open to no derivation, never crash.
    p = tmp_path / "dockwright.toml"
    p.write_text("[task_keys]\nkey_regex = '[A-Za-z'\n")
    monkeypatch.setenv(_config.ENV_CONFIG_PATH, str(p))
    assert _derive_ticket("w1", "the ask TKT-8353") is None


def test_current_branch_best_effort(tmp_path):
    assert _current_branch(str(tmp_path)) is None       # not a git repo
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "my-branch"], cwd=repo, check=True)
    assert _current_branch(str(repo)) == "my-branch"


# --- Auto-publish: artifact discipline footer on keyed spawns ---


def test_spawn_footer_injected_with_explicit_task_key(fresh_orchestrator_dir, monkeypatch):
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="build the scraper", name="yt-scraper", cwd="/tmp",
        task_key="yt-bot-public"))
    text = captured["initial_prompt"]
    assert "[orchestrator] Artifact discipline — task_key: `yt-bot-public`" in text
    assert 'artifact_put(task_key="yt-bot-public"' in text
    assert text.startswith("build the scraper")     # the ask still leads; footer trails


def test_spawn_footer_injected_with_derived_jira_key(fresh_orchestrator_dir, monkeypatch,
                                                     configured_key_regex):
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="/ticket-start TKT-8353 extra", name="tkt-8353-fix", cwd="/tmp"))
    assert "task_key: `TKT-8353`" in captured["initial_prompt"]


def test_spawn_footer_absent_when_no_key_resolves(fresh_orchestrator_dir, monkeypatch):
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(initial_prompt="just poke around", name="scout", cwd="/tmp"))
    assert "[orchestrator] Artifact discipline" not in captured["initial_prompt"]


def test_spawn_footer_absent_on_blank_prompt_even_with_key(fresh_orchestrator_dir, monkeypatch):
    # An empty prompt is a documented bare-runtime-session lane; injecting a footer
    # would fabricate a first turn out of nothing.
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(initial_prompt="", name="bare", cwd="/tmp", task_key="TKT-SANDBOX-1"))
    assert captured["initial_prompt"] == ""


def test_spawn_footer_lands_after_preset_boilerplate(fresh_orchestrator_dir, monkeypatch,
                                                     configured_key_regex):
    captured = _patch_spawn_worker_tab(monkeypatch)
    paths.PRESETS.mkdir(parents=True, exist_ok=True)
    (paths.PRESETS / "boiler.md").write_text("BOILERPLATE")
    _asyncio.run(spawn_worker_impl(
        initial_prompt="the ask TKT-9", name="w1", cwd="/tmp", preset="boiler"))
    text = captured["initial_prompt"]
    assert text.index("BOILERPLATE") < text.index("the ask TKT-9") \
        < text.index("[orchestrator] Artifact discipline")


def test_spawn_footer_not_in_assignment_record(fresh_orchestrator_dir, monkeypatch):
    _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(initial_prompt="do X", name="w1", cwd="/tmp", task_key="TKT-SANDBOX-2"))
    (pending,) = list(paths.ASSIGNMENTS_PENDING.glob("*.json"))
    record = state.read_json(pending)
    assert record["initial_prompt"] == "do X"       # raw pre-footer ask
    assert record["ticket"] == "TKT-SANDBOX-2"              # resolution-refactor regression pin


def test_spawn_footer_present_for_codex_runtime(fresh_orchestrator_dir, monkeypatch):
    # The footer is appended runtime-agnostically, before the spawner — pin that
    # the codex lane gets it too.
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="port the bot", name="cx-bot", cwd="/tmp",
        task_key="yt-bot-public", runtime="codex"))
    assert "[orchestrator] Artifact discipline" in captured["initial_prompt"]


# --- Repo freshness: sync-once footer on every non-blank spawn ---


def test_repo_sync_footer_injected_without_task_key(fresh_orchestrator_dir, monkeypatch):
    # Unkeyed scouts are the at-risk population — unlike the artifact footer,
    # this one must NOT be task_key-gated.
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="just poke around", name="scout", cwd="/tmp"))
    text = captured["initial_prompt"]
    assert "[orchestrator] Repo freshness" in text
    assert text.startswith("just poke around")      # the ask still leads


def test_repo_sync_footer_absent_on_blank_prompt(fresh_orchestrator_dir, monkeypatch):
    # Blank prompt = documented bare-runtime-session lane (same guard as the
    # artifact footer): no fabricated first turn.
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(initial_prompt="", name="bare", cwd="/tmp"))
    assert captured["initial_prompt"] == ""


def test_repo_sync_footer_present_for_codex_runtime(fresh_orchestrator_dir, monkeypatch):
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
        task_key="yt-bot-public"))
    text = captured["initial_prompt"]
    assert text.index("[orchestrator] Artifact discipline") \
        < text.index("[orchestrator] Repo freshness")


def test_repo_sync_footer_names_the_git_recipe():
    # Content pin: a paraphrase must not drop the sync recipe, the conflicted-
    # rebase escape hatch, or the stale-tree fallback.
    text = _repo_sync_footer()
    assert "fetch origin main" in text
    assert "merge --ff-only origin/main" in text
    assert "rebase origin/main" in text
    assert "git rebase --abort" in text
    assert "git show origin/main:<path>" in text


# --- Ownership plane: brief surfacing + pipeline_status join (spec §13) ---

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
    assert b["brief"] is None                              # no assignment → None


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
    assert r["brief"] is None                              # no crash on sid-less record


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
    assert "unrelated" not in out                  # other ticket filtered out


# --- Ownership plane: resume interplay (spec §12) ---

from dockwright.mcp_server import _spawn_and_confirm_resume


def test_codex_lane_confirm_migrates_assignment(fresh_orchestrator_dir):
    # closed codex worker with an assignment under the OLD sid
    state.write_json_atomic(paths.CLOSED / "old-sid.json", {
        "claude_sid": "old-sid", "name": "cx", "cwd": "/x", "runtime": "codex", "closed_at": 1.0})
    _seed_assignment("old-sid", "codex task")
    closed_path = paths.CLOSED / "old-sid.json"

    async def fake_spawn(**kwargs):
        # codex rolled a fresh thread id: register under a NEW sid claiming the name
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
        "w-res", "old-sid", "/x", 5.0, 0.05))
    assert "CLAUDE_ASSIGNMENT_ID" not in (captured.get("env") or {})


# --- Ownership plane: retention (spec §14) ---

from dockwright.mcp_server import _prune_stale_assignments


def test_prune_assignments_keeps_active_sid(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1")
    _seed_assignment("w1")
    _age(paths.ASSIGNMENTS / "w1.json", 31)
    _prune_stale_assignments()
    assert (paths.ASSIGNMENTS / "w1.json").exists()        # active = absolute keep


def test_prune_assignments_keeps_crash_orphan_within_retention(fresh_orchestrator_dir):
    _seed_assignment("w-crashed")                          # no active, no closed — the SIGHUP case
    _prune_stale_assignments()
    assert (paths.ASSIGNMENTS / "w-crashed.json").exists()  # fresh mtime → kept


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
    _age(stale, 2)                                         # > 24h
    _prune_stale_assignments()
    assert not stale.exists() and fresh_p.exists()


def test_prune_pending_sweeps_window_sidecar_orphans(fresh_orchestrator_dir):
    paths.ASSIGNMENTS_PENDING.mkdir(parents=True, exist_ok=True)
    stale = paths.pending_window_path("aid-old")
    fresh_p = paths.pending_window_path("aid-new")
    stale.write_text("777")
    fresh_p.write_text("888")
    _age(stale, 2)                                         # > 24h
    _prune_stale_assignments()
    assert not stale.exists() and fresh_p.exists()


# --- Review-fix regressions (code-review round 1) ---

def test_folds_tolerate_corrupted_stamp_lines(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "spec", "srs", "the body", "complete", "sid-1")
    p = paths.artifact_path("TKT-SANDBOX-1", "spec", "srs")
    corrupted = p.read_text().replace('status: "complete"', "status: {broken")
    p.write_text(corrupted)
    status_out = pipeline_status_impl("TKT-SANDBOX-1")     # must not raise (Important #2)
    assert "spec.srs" in status_out
    view_out = artifact_view_impl("TKT-SANDBOX-1")
    assert "the body" in view_out


def test_prune_artifacts_tolerates_vanishing_entries(fresh_orchestrator_dir):
    artifact_put_impl("TKT-SANDBOX-1", "spec", "srs", "x", "complete", "s1")
    # dangling symlink: rglob yields it, .stat() raises FileNotFoundError (Important #3)
    (paths.artifact_ticket_dir("TKT-SANDBOX-1") / "dangling").symlink_to(
        fresh_orchestrator_dir / "nope-does-not-exist")
    _prune_stale_artifacts()                        # must not raise
    assert paths.artifact_ticket_dir("TKT-SANDBOX-1").exists()


def test_spawn_value_error_unlinks_pending(fresh_orchestrator_dir, monkeypatch):
    async def raise_value_error(**kwargs):
        raise ValueError("disallowed extra args")
    monkeypatch.setattr(spawner, "spawn_worker_tab", raise_value_error)
    with pytest.raises(ValueError):
        _asyncio.run(spawn_worker_impl(initial_prompt="x", name="w1", cwd="/tmp"))
    assert list(paths.ASSIGNMENTS_PENDING.glob("*.json")) == []   # Minor #1


def test_derive_ticket_auto_name_never_shadows_prompt_key(fresh_orchestrator_dir,
                                                          configured_key_regex):
    # verifier round 2: spawn defaults name to "worker-<epoch>" BEFORE the pending
    # write; the name half of the regex matched it ahead of the real prompt key.
    assert _derive_ticket("worker-1749672000", "/ticket-start TKT-8353") == "TKT-8353"
    assert _derive_ticket("worker-1749672000", "no key here") is None
    # prompt key wins over a name-embedded key (canonical dispatch carries it in the prompt)
    assert _derive_ticket("tkt-8353-dlq-fix", "/ticket-start TKT-99") == "TKT-99"


# --- Personal task keys (no Jira ticket) ---

def test_spawn_worker_explicit_task_key_wins_over_derivation(fresh_orchestrator_dir, monkeypatch,
                                                             configured_key_regex):
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="build the bot; related cleanup tracked in TKT-999",
        name="yt-bot-scraper", cwd="/tmp", task_key="yt-bot-public"))
    (pending,) = list(paths.ASSIGNMENTS_PENDING.glob("*.json"))
    record = state.read_json(pending)
    assert record["ticket"] == "yt-bot-public"      # explicit ALWAYS wins over the derived key


def test_spawn_worker_without_task_key_keeps_derivation(fresh_orchestrator_dir, monkeypatch,
                                                        configured_key_regex):
    captured = _patch_spawn_worker_tab(monkeypatch)
    _asyncio.run(spawn_worker_impl(
        initial_prompt="/ticket-start TKT-8353", name="w1", cwd="/tmp"))
    (pending,) = list(paths.ASSIGNMENTS_PENDING.glob("*.json"))
    assert state.read_json(pending)["ticket"] == "TKT-8353"


def test_slug_key_round_trips_store_and_joins_assignments(fresh_orchestrator_dir):
    # arbitrary personal slug end-to-end: put -> list -> pipeline_status with assignments joined
    artifact_put_impl("yt-bot-public", "spec", "scraper", "# bot spec", "complete", "sid-bot")
    (entry,) = artifact_list_impl("yt-bot-public")
    assert entry["phase"] == "spec" and entry["name"] == "scraper"
    assert Path(entry["path"]).parent == paths.ARTIFACTS / "yt-bot-public"
    state.write_json_atomic(paths.ASSIGNMENTS / "sid-bot.json", {
        "claude_sid": "sid-bot", "name": "yt-bot-scraper", "ticket": "yt-bot-public",
        "initial_prompt": "build the scraper half",
    })
    out = pipeline_status_impl("yt-bot-public")
    assert "spec.scraper" in out
    assert "yt-bot-scraper" in out and "build the scraper half" in out


# --- task_key fail-fast validation (verifier hardenings on #54) ---

def test_spawn_worker_blank_task_key_rejected(fresh_orchestrator_dir, monkeypatch):
    _patch_spawn_worker_tab(monkeypatch)
    for blank in ("", "   "):
        with pytest.raises(ValueError, match="blank"):
            _asyncio.run(spawn_worker_impl(initial_prompt="x", name="w1", cwd="/tmp",
                                           task_key=blank))
    assert list(paths.ASSIGNMENTS_PENDING.glob("*.json")) == []   # fail-fast: nothing written


def test_spawn_worker_path_hostile_task_key_rejected(fresh_orchestrator_dir, monkeypatch):
    _patch_spawn_worker_tab(monkeypatch)
    with pytest.raises(ValueError, match="slug"):
        _asyncio.run(spawn_worker_impl(initial_prompt="x", name="w1", cwd="/tmp",
                                       task_key="yt bot"))
    assert list(paths.ASSIGNMENTS_PENDING.glob("*.json")) == []


# --- spend telemetry surfacing ----------------------------------------------

def test_list_workers_renders_compact_spend(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    record = state.read_json(paths.ACTIVE / "w1.json")
    record["spend"] = {"turns": 12, "out_tokens": 340_000, "in_tokens": 900,
                       "cache_read_tokens": 5_100_000, "last_turn_out": 200,
                       "last_msg_id": "msg_z"}
    state.write_json_atomic(paths.ACTIVE / "w1.json", record)
    workers = list_workers_impl()
    assert workers[0]["spend"] == "12 turns / 340k out"


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
    assert list_workers_impl()[0]["spend"] == "1 turn / 512 out"

    record["spend"] = {"turns": 200, "out_tokens": 2_400_000, "in_tokens": 0,
                       "cache_read_tokens": 0, "last_turn_out": 1, "last_msg_id": "m"}
    state.write_json_atomic(paths.ACTIVE / "w1.json", record)
    assert list_workers_impl()[0]["spend"] == "200 turns / 2.4M out"


def test_worker_done_stamps_spend_totals(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    record = state.read_json(paths.ACTIVE / "w1.json")
    record["spend"] = {"turns": 12, "out_tokens": 340_000, "in_tokens": 900,
                       "cache_read_tokens": 5_100_000, "last_turn_out": 200,
                       "last_msg_id": "msg_z"}
    state.write_json_atomic(paths.ACTIVE / "w1.json", record)
    worker_done_impl(claude_sid="w1", summary="done")
    done_files = list(paths.DONE.rglob("*.json"))
    event = state.read_json(done_files[0])
    # Totals only — the tail cursor and per-turn value are record internals.
    assert event["spend"] == {"turns": 12, "out_tokens": 340_000,
                              "in_tokens": 900, "cache_read_tokens": 5_100_000}


def test_worker_done_spend_none_when_never_metered(fresh_orchestrator_dir):
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="i1", pid=os.getpid())
    worker_done_impl(claude_sid="w1", summary="done")
    event = state.read_json(list(paths.DONE.rglob("*.json"))[0])
    assert event["spend"] is None

# ---- nested sub-session guards --------------------------------------------
# Nested records (claude -p children of a registered session, flagged
# nested:true by the SessionStart hook) stay visible for debugging but must
# never generate manager notifications or be managed like real workers.

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
    """Resume must run the same record/nested validation as a fresh ask."""
    from dockwright.mcp_server import _write_question
    qid = _write_question(worker_sid="nested-1", worker_name="nested", question="q?")
    _write_nested_record()
    with pytest.raises(ValueError, match="nested"):
        _asyncio.run(ask_manager_impl("nested-1", "q?", poll_interval=0.01, resume_question_id=qid))
    assert len(list(paths.QUESTIONS.rglob("*.json"))) == 1  # question untouched


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
    """A nested manager-agent ghost must not break single-manager attribution,
    and null-parent nested workers must not get stamped."""
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
        cwd="/tmp/x", initial_prompt="hi", name="alpha"))
    argv = list(captured["args"])
    # tmux passes the window name via -n; default worker title is <name> (no emoji).
    assert "-n" in argv and argv[argv.index("-n") + 1] == "alpha"
    assert not any("[w]" in str(a) for a in argv)


def test_resolve_manager_window_title_match_without_exclude_id(monkeypatch):
    """tmux ls omits env so Pass-1 (session-id) never fires; Pass-2 title match
    must run even when exclude_id is empty (send_manager_to_manager caller has no
    exclude_id — it's a send, not a close, so matching the peer's own titled window
    is the intent)."""
    import dockwright.mcp_server as m
    data = [{"wm_class": "mgr", "tabs": [{"title": "alpha · general",
             "windows": [{"id": "%3", "cwd": "/c", "title": "alpha · general", "pid": "1"}]}]}]
    monkeypatch.setattr("dockwright.mcp_server._terminal_ls", lambda: data)
    assert m._resolve_manager_window("no-such-sid", "alpha") == "%3"


def test_match_worker_by_cwd_uniqueness_on_tmux():
    """tmux ls populates {id,cwd,title,pid} only — no foreground_processes key.
    The function must fall back to cwd-uniqueness and return the single matching
    window id instead of silently returning ''."""
    import dockwright.mcp_server as m
    data = [{"wm_class": "claude-workers", "tabs": [{"title": "w",
             "windows": [{"id": "%6", "cwd": "/work/x", "title": "t", "pid": "2"}]}]}]
    rec = {"cwd": "/work/x", "runtime": "claude"}
    assert m._match_worker_window_by_cwd_runtime(data, rec) == "%6"


def test_mcp_send_and_close_emit_tmux_argv(fresh_orchestrator_dir, monkeypatch):
    """Regression guard: with tmux backend, _send_text and _close_window
    route through TmuxDriver and emit tmux argv (send-keys + kill-pane), not kitty argv.
    Uses the internal helpers directly so we avoid the full window-resolve scaffolding
    needed by send_manager_to_worker_impl while still covering the routing path."""
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

    # Exercise send path
    srv._send_text("%5", "hello worker")
    # Exercise close path
    srv._close_window("%5")

    # send_text should have emitted at least one "send-keys ... Enter"
    assert any(
        "send-keys" in c and c[-1] == "Enter" and "%5" in c
        for c in calls
    ), f"No tmux send-keys Enter found in: {calls}"

    # close should have emitted kill-pane targeting %5
    assert any(
        c[0] == "tmux" and "kill-pane" in c and "%5" in c
        for c in calls
    ), f"No tmux kill-pane %5 found in: {calls}"

    # Confirm it's tmux, not kitty
    assert not any("kitty" in c[0] for c in calls), f"kitty appeared in calls: {calls}"


def test_await_input_ready_returns_when_idle(fresh_orchestrator_dir, monkeypatch):
    """Claude lane: polls _input_is_idle and returns as soon as the box is ready."""
    import dockwright.mcp_server as srv
    calls = {"n": 0}

    def fake_idle(screen):
        calls["n"] += 1
        return calls["n"] >= 3          # ready on the 3rd poll

    monkeypatch.setattr(srv, "_capture_text", lambda wid: "screen")
    monkeypatch.setattr(srv, "_input_is_idle", fake_idle)
    monkeypatch.setattr(srv, "_INPUT_READY_POLL_SEC", 0.0)
    _asyncio.run(srv._await_input_ready("555", "claude"))
    assert calls["n"] == 3

def test_await_input_ready_times_out_without_raising(fresh_orchestrator_dir, monkeypatch):
    """Claude lane: never-idle pane (or _capture_text=None) → returns after the
    bounded timeout, no exception — typing into a booting pane is best-effort."""
    import dockwright.mcp_server as srv
    monkeypatch.setattr(srv, "_capture_text", lambda wid: None)   # unreadable forever
    monkeypatch.setattr(srv, "_INPUT_READY_POLL_SEC", 0.0)
    monkeypatch.setattr(srv, "_INPUT_READY_TIMEOUT_SEC", 0.05)
    _asyncio.run(srv._await_input_ready("555", "claude"))          # must not raise

def test_await_input_ready_codex_short_circuits(fresh_orchestrator_dir, monkeypatch):
    """Codex lane: _input_is_idle is Claude-caret-specific and can NEVER pass on a
    codex pane — the helper must take the fixed sleep and never call it."""
    import dockwright.mcp_server as srv

    def boom(screen):
        raise AssertionError("_input_is_idle must not be called for codex")

    monkeypatch.setattr(srv, "_input_is_idle", boom)
    monkeypatch.setattr(srv, "_INPUT_READY_CODEX_SLEEP_SEC", 0.0)
    _asyncio.run(srv._await_input_ready("555", "codex"))

def test_await_input_ready_no_window_id_returns_immediately(fresh_orchestrator_dir, monkeypatch):
    """No window id to poll → nothing to wait on (delivery's own resolve retries cover it)."""
    import dockwright.mcp_server as srv
    monkeypatch.setattr(srv, "_capture_text",
                        lambda wid: (_ for _ in ()).throw(AssertionError("no poll expected")))
    _asyncio.run(srv._await_input_ready("", "claude"))


# --- send_manager_to_worker auto_resume lane ---
from dockwright.mcp_server import send_manager_to_worker_auto_impl as _auto_send


def _write_closed(name, sid, cwd="/tmp/wt", runtime="claude", closed_at=1.0):
    state.write_json_atomic(paths.CLOSED / f"{sid}.json", {
        "claude_sid": sid, "name": name, "cwd": cwd,
        "runtime": runtime, "closed_at": closed_at,
    })


def test_auto_send_live_worker_delivers_without_resume(fresh_orchestrator_dir, monkeypatch):
    """Target alive → normal delivery; no resume, no `resumed` key in the result."""
    import dockwright.mcp_server as srv
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd="/x", iterm_sid="42")
    typed = []
    monkeypatch.setattr(srv, "_send_text", lambda wid, text: typed.append(text))

    async def no_resume(*a, **k):
        raise AssertionError("resume must not fire for a live worker")

    monkeypatch.setattr(srv, "resume_worker_impl", no_resume)
    result = _asyncio.run(_auto_send("alpha", "hi"))
    assert result["status"] == "delivered" and "resumed" not in result
    assert typed == ["[MANAGER] hi"]


def test_auto_send_resumes_closed_worker_and_delivers(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """Closed + resumable → resume (all real guards), readiness wait, deliver to the
    registered handle; marker appears EXACTLY once; tasked_at stamped; result carries
    resumed/sid."""
    import dockwright.mcp_server as srv
    _make_transcript(tmp_path, monkeypatch, "gone-sid")
    _write_closed("alpha", "gone-sid")
    _patch_spawn_registers_active(monkeypatch)   # fake spawn registers active/ + window "999"
    typed = []
    monkeypatch.setattr(srv, "_send_text", lambda wid, text: typed.append((wid, text)))
    monkeypatch.setattr(srv, "_terminal_ls", lambda: [
        {"tabs": [{"windows": [
            {"id": "999", "cwd": "/tmp/wt",
             "foreground_processes": [{"cmdline": ["claude", "--resume"]}]}]}]}])
    monkeypatch.setattr(srv, "_INPUT_READY_TIMEOUT_SEC", 0.0)   # skip the poll in tests
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
    """Never-existed worker → combined raise naming both the send failure and the
    probe failure. No silent inbox."""
    with pytest.raises(ValueError, match=r"no worker named 'ghost'.*auto_resume.*no closed worker"):
        _asyncio.run(_auto_send("ghost", "hi"))


def test_auto_send_closed_without_transcript_raises_combined(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """Closed record exists but its transcript is gone → probe error surfaced."""
    monkeypatch.setenv("HOME", str(tmp_path))   # empty projects tree → no transcripts
    _write_closed("alpha", "dead-sid")
    with pytest.raises(ValueError, match=r"auto_resume.*none have a live transcript"):
        _asyncio.run(_auto_send("alpha", "hi"))


def test_auto_send_registration_timeout_raises_and_keeps_record(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """resume returns ok:False → raise 'message NOT delivered'; closed record intact."""
    _make_transcript(tmp_path, monkeypatch, "stuck-sid")
    _write_closed("alpha", "stuck-sid")
    _patch_spawn_worker_tab(monkeypatch)   # spawns but never registers active/
    with pytest.raises(ValueError, match="message NOT delivered"):
        _asyncio.run(_auto_send("alpha", "hi",
                                _registration_timeout_sec=0.05, _poll_interval=0.01))
    assert (paths.CLOSED / "stuck-sid.json").exists()


def test_auto_send_manager_holder_refused(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """Name held by an active MANAGER + a same-name closed worker record → the
    resume holder-guard refusal propagates (no delivery, no resume)."""
    _make_transcript(tmp_path, monkeypatch, "old-worker-sid")
    state.write_json_atomic(paths.ACTIVE / "mgr1.json", {
        "claude_sid": "mgr1", "agent": "manager", "name": "happy-yak",
        "cwd": "/x", "iterm_sid": "i1", "pid": os.getpid(), "started_at": 0})
    _write_closed("happy-yak", "old-worker-sid")
    with pytest.raises(ValueError, match="already active"):
        _asyncio.run(_auto_send("happy-yak", "hi"))


def test_auto_send_nested_target_raises(fresh_orchestrator_dir):
    """Nested target: live path refuses; no closed record → combined raise carries
    the nested refusal."""
    state.write_json_atomic(paths.ACTIVE / "nested-abcd.json", {
        "claude_sid": "nested-abcd", "agent": "worker", "name": "nested-abcd1234",
        "cwd": "/x", "pid": os.getpid(), "started_at": 0,
        "nested": True, "nested_parent_name": "alpha"})
    with pytest.raises(ValueError, match="nested sub-session"):
        _asyncio.run(_auto_send("nested-abcd1234", "hi"))


def test_auto_send_codex_lane(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """Codex closed record → resume via the codex lane; readiness wait takes the
    fixed codex sleep (never polls _input_is_idle); delivery proceeds."""
    import dockwright.mcp_server as srv
    _make_codex_transcript(tmp_path, monkeypatch, "cx-sid")
    _write_closed("cx", "cx-sid", cwd="/tmp/cx", runtime="codex")
    _patch_spawn_registers_active(monkeypatch)   # registers under resume_sid, runtime codex
    typed = []
    monkeypatch.setattr(srv, "_send_text", lambda wid, text: typed.append(text))
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
    """A resume of the same name already in flight → the dedup guard raises; the
    loser retries later and lands on the live path once registration completes."""
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
    """Tab spawn fails (RuntimeError from resume) → propagates; closed record intact."""
    _make_transcript(tmp_path, monkeypatch, "sp-sid")
    _write_closed("alpha", "sp-sid")

    async def broken_spawn(**kwargs):
        raise ConnectionRefusedError("no tmux")

    monkeypatch.setattr(spawner, "spawn_worker_tab", broken_spawn)
    with pytest.raises(RuntimeError, match="Could not spawn tab"):
        _asyncio.run(_auto_send("alpha", "hi"))
    assert (paths.CLOSED / "sp-sid.json").exists()


def test_auto_send_post_resume_delivery_failure_names_resumed_sid(fresh_orchestrator_dir, tmp_path, monkeypatch):
    """Resume succeeds but the delivery send fails (no window resolvable) → the raise
    must carry the corrected guidance (worker WAS resumed; retry a plain send), not
    the live-path 'resume_worker or re-spawn' text."""
    import dockwright.mcp_server as srv
    _make_transcript(tmp_path, monkeypatch, "pr-sid")
    _write_closed("alpha", "pr-sid")

    async def fake_spawn(**kwargs):
        # Register active/ (confirms resume) but with NO window id and a cwd that
        # never matches the (empty) terminal listing → post-resume send raises.
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
    """The async tool wrapper with auto_resume omitted behaves exactly like today:
    closed worker → the live-path raise, resume never attempted."""
    import dockwright.mcp_server as srv
    _make_transcript(tmp_path, monkeypatch, "cl-sid")
    _write_closed("alpha", "cl-sid")

    async def no_resume(*a, **k):
        raise AssertionError("resume must not fire when auto_resume is off")

    monkeypatch.setattr(srv, "resume_worker_impl", no_resume)
    with pytest.raises(ValueError, match="no worker named 'alpha'"):
        _asyncio.run(srv.send_manager_to_worker(worker="alpha", text="hi"))
