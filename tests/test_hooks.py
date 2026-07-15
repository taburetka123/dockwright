import io
import json
import os
import shutil
import subprocess
import sys
import time
import pytest
from dockwright import paths, state
from dockwright.hooks import (
    session_start, user_prompt_submit, stop_hook, session_end,
    _set_tab_color, _set_tab_title, MANAGER_TAB_COLOR,
)

@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_WORKER_RUNTIME", raising=False)
    monkeypatch.delenv("CLAUDE_MANAGER_RUNTIME", raising=False)
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(paths, "QUESTIONS", tmp_path / "questions")
    monkeypatch.setattr(paths, "ANSWERS", tmp_path / "answers")
    monkeypatch.setattr(paths, "DONE", tmp_path / "done")
    monkeypatch.setattr(paths, "CLOSED", tmp_path / "closed")
    monkeypatch.setattr(paths, "TURN_ENDS", tmp_path / "turn-ends")
    monkeypatch.setattr(paths, "ARTIFACTS", tmp_path / "artifacts")
    monkeypatch.setattr(paths, "ASSIGNMENTS", tmp_path / "assignments")
    monkeypatch.setattr(paths, "ASSIGNMENTS_PENDING", tmp_path / "assignments" / ".pending")
    monkeypatch.setattr(paths, "SPEND_LEDGER", tmp_path / "spend-ledger.jsonl")
    monkeypatch.delenv("CLAUDE_ASSIGNMENT_ID", raising=False)
    paths.ensure_dirs()
    yield tmp_path

def test_session_start_skips_when_no_env(fresh, monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_AGENT", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    assert list((fresh / "active").iterdir()) == []

def test_session_start_skips_distill_child(fresh, monkeypatch):
    """A headless distill `claude -p` child carries the sentinel; even with a
    leaked CLAUDE_AGENT=manager it must NOT register as a manager (a registered
    distill child's own SessionEnd re-distills — infinite fan-out)."""
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setenv(paths.DISTILL_ENV_SENTINEL, "1")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "distill-1", "cwd": "/x"})))
    session_start()
    assert list((fresh / "active").iterdir()) == []

def test_session_start_registers_worker(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.setenv("CLAUDE_ITERM_SID", "i1")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "s1.json")
    assert record["name"] == "alpha"
    assert record["agent"] == "worker"
    assert record["window_id"] == "i1"
    assert record["runtime"] == "claude"

def test_session_start_registers_codex_worker_runtime(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.setenv("CLAUDE_WORKER_RUNTIME", "codex")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "s1.json")
    assert record["runtime"] == "codex"

def test_session_start_worker_gets_separate_funny_name(fresh, monkeypatch):
    """funny_name is a cosmetic field distinct from the routing `name` (task label)."""
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "fix-the-thing")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "s1.json")
    assert record["name"] == "fix-the-thing"  # routing key unchanged
    assert record["funny_name"]  # rolled, non-empty
    assert record["funny_name"] != record["name"]
    assert "-" in record["funny_name"]  # <adjective>-<noun>

def test_session_start_manager_has_no_funny_name(fresh, monkeypatch):
    """Managers carry their funny identity in `name`; funny_name stays null for them."""
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-1", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "mgr-1.json")
    assert record["funny_name"] is None
    assert record["runtime"] == "claude"

def test_session_start_pins_manager_runtime_to_claude(fresh, monkeypatch):
    # Managers are Claude-only: a stray CLAUDE_MANAGER_RUNTIME=codex is ignored.
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setenv("CLAUDE_MANAGER_RUNTIME", "codex")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-1", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "mgr-1.json")
    assert record["agent"] == "manager"
    assert record["runtime"] == "claude"

def test_session_start_worker_funny_name_avoids_collision(fresh, monkeypatch):
    """A fresh worker roll must not collide with a live worker's funny_name."""
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "task-b")
    # Existing live worker occupies every roll the rng would produce except one.
    state.write_json_atomic(fresh / "active" / "other.json", {
        "claude_sid": "other", "agent": "worker", "name": "task-a", "funny_name": "grumpy-yak",
        "cwd": "/x", "iterm_sid": "i0", "pid": os.getpid(), "started_at": 0,
    })
    import dockwright.names as names
    # Force the roller to first hand back the taken name, then a free one.
    seq = iter(["grumpy-yak", "snarky-otter"])
    monkeypatch.setattr(names, "_roll", lambda nouns, rng: next(seq))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "s1.json")
    assert record["funny_name"] == "snarky-otter"

def test_session_start_falls_back_to_pane_id(fresh, monkeypatch):
    """When CLAUDE_ITERM_SID is unset, persist the driver's native pane id (TMUX_PANE)."""
    _reset_driver(monkeypatch)
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.delenv("CLAUDE_ITERM_SID", raising=False)
    monkeypatch.setenv("TMUX_PANE", "42")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "s1.json")
    assert record["window_id"] == "42"

def test_session_start_iterm_sid_overrides_pane_id(fresh, monkeypatch):
    """Explicit CLAUDE_ITERM_SID wins over the driver pane id (backwards compat)."""
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.setenv("CLAUDE_ITERM_SID", "explicit-99")
    monkeypatch.setenv("TMUX_PANE", "42")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "s1.json")
    assert record["window_id"] == "explicit-99"

def test_session_start_refire_preserves_manager_name_and_state(fresh, monkeypatch):
    """SessionStart re-fires on every resume / context compaction. A registered
    manager must keep its routing `name` (tab title + worker→manager routing key)
    and live progress fields — a re-roll on each re-fire churns the tab title and
    silently breaks routing for live workers."""
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.delenv("CLAUDE_WORKER_NAME", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-1", "cwd": "/x"})))
    session_start()
    first = state.read_json(fresh / "active" / "mgr-1.json")
    # Simulate live progress accumulated between registration and the re-fire.
    first["state"] = "processing"
    first["last_turn_at"] = 123.0
    first["last_summary"] = "mid-task summary"
    state.write_json_atomic(fresh / "active" / "mgr-1.json", first)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-1", "cwd": "/y"})))
    session_start()
    second = state.read_json(fresh / "active" / "mgr-1.json")
    assert second["name"] == first["name"]
    assert second["started_at"] == first["started_at"]
    assert second["state"] == "processing"
    assert second["last_turn_at"] == 123.0
    assert second["last_summary"] == "mid-task summary"
    assert second["cwd"] == "/y"  # genuinely-volatile field does refresh

def test_session_start_refire_preserves_worker_funny_name_and_state(fresh, monkeypatch):
    """A worker's cosmetic funny_name and progress fields survive a re-fire."""
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "fix-the-thing")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    first = state.read_json(fresh / "active" / "s1.json")
    first["last_turn_at"] = 456.0
    state.write_json_atomic(fresh / "active" / "s1.json", first)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    second = state.read_json(fresh / "active" / "s1.json")
    assert second["funny_name"] == first["funny_name"]
    assert second["name"] == "fix-the-thing"
    assert second["started_at"] == first["started_at"]
    assert second["last_turn_at"] == 456.0

def test_session_start_agent_change_re_registers(fresh, monkeypatch):
    """A same-sid record under a DIFFERENT agent is not the same session resuming
    (e.g. a session relaunched under new env) — register fresh, don't preserve."""
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "manager", "name": "happy-yak",
        "cwd": "/x", "window_id": "", "pid": os.getpid(), "started_at": 0,
        "state": "idle", "last_turn_at": None, "last_summary": None,
        "domain": "general", "parent_manager_name": None, "runtime": "claude",
    })
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "task-x")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "s1.json")
    assert record["agent"] == "worker"
    assert record["name"] == "task-x"

def test_session_start_stamps_account_from_env(fresh, monkeypatch):
    """CLAUDE_ORCH_ACCOUNT=b in env → fresh registration → record["account"] == "b"."""
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "task-acct")
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT", "b")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s-acct1", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "s-acct1.json")
    assert record["account"] == "b"


def test_session_start_account_none_without_env(fresh, monkeypatch):
    """No CLAUDE_ORCH_ACCOUNT in env → fresh registration → record["account"] is None."""
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "task-no-acct")
    monkeypatch.delenv("CLAUDE_ORCH_ACCOUNT", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s-acct2", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "s-acct2.json")
    assert record["account"] is None


def test_session_start_account_invalid_env_stamps_none(fresh, monkeypatch):
    """CLAUDE_ORCH_ACCOUNT outside the a|b whitelist → fresh registration → record["account"] is None."""
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "task-bad-acct")
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT", "xyz")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s-acct4", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "s-acct4.json")
    assert record["account"] is None


def test_session_start_resume_refreshes_account_only_when_env_present(fresh, monkeypatch):
    """Re-fire with CLAUDE_ORCH_ACCOUNT=b updates record["account"];
    re-fire without the env var leaves the stamped value intact."""
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "task-resume-acct")
    # Pre-write an active record with account="a"
    state.write_json_atomic(fresh / "active" / "s-acct3.json", {
        "claude_sid": "s-acct3",
        "agent": "worker",
        "name": "task-resume-acct",
        "funny_name": None,
        "cwd": "/x",
        "window_id": "",
        "pid": os.getpid(),
        "started_at": 0.0,
        "state": "idle",
        "last_turn_at": None,
        "last_summary": None,
        "domain": None,
        "parent_manager_name": None,
        "runtime": "claude",
        "account": "a",
    })
    # Re-fire WITH env CLAUDE_ORCH_ACCOUNT=b → account must update to "b"
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT", "b")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s-acct3", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "s-acct3.json")
    assert record["account"] == "b"
    # Re-fire again WITHOUT the env var → account stays "b"
    monkeypatch.delenv("CLAUDE_ORCH_ACCOUNT", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s-acct3", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "s-acct3.json")
    assert record["account"] == "b"


def test_user_prompt_submit_marks_state_processing(fresh, monkeypatch):
    """Setting state=processing on each prompt lets the stale-monitor distinguish
    wedged mid-turn workers from idle ones, and bumps the file mtime as a heartbeat."""
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 0, "state": "idle",
    })
    monkeypatch.setattr(
        "dockwright.hooks.subprocess.run",
        lambda a, **kw: type("R", (), {"returncode": 0})(),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "prompt": "go"})))
    user_prompt_submit()
    record = state.read_json(fresh / "active" / "s1.json")
    assert record["state"] == "processing"

def test_user_prompt_submit_stamps_processing_since(fresh, monkeypatch):
    # wait_for_worker uses processing_since as the tasking-episode lower bound
    # for done events when the record is processing (covers human-typed
    # re-tasks that never pass through send_manager_to_worker).
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 0, "state": "idle",
    })
    monkeypatch.setattr(
        "dockwright.hooks.subprocess.run",
        lambda a, **kw: type("R", (), {"returncode": 0})(),
    )
    before = time.time()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "prompt": "go"})))
    user_prompt_submit()
    record = state.read_json(fresh / "active" / "s1.json")
    assert record["state"] == "processing"
    assert record.get("processing_since") is not None
    assert record["processing_since"] >= before

def test_user_prompt_submit_noop_for_non_orchestrator(fresh, monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_AGENT", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "prompt": "go"})))
    user_prompt_submit()
    assert capsys.readouterr().out == ""

def test_session_end_removes_active(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 0,
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    session_end()
    assert not (fresh / "active" / "s1.json").exists()

def test_session_end_removes_manager_active_record(fresh, monkeypatch):
    """Manager session_end cleans up its active record (multi-manager: no lock to release)."""
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    state.write_json_atomic(fresh / "active" / "mgr-1.json", {
        "claude_sid": "mgr-1", "agent": "manager", "name": "grumpy-yak", "cwd": "/x",
        "iterm_sid": "i9", "pid": 1, "started_at": 0, "domain": "general",
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-1"})))
    session_end()
    assert not (fresh / "active" / "mgr-1.json").exists()

def test_session_end_archives_worker_to_closed(fresh, monkeypatch):
    """User-initiated close (Cmd+W, /exit) must archive worker records so resume_worker can find them."""
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 12345.0,
        "last_summary": "shipped foo", "last_turn_at": "2026-05-19T00:00:00Z",
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    session_end()
    closed = state.read_json(fresh / "closed" / "s1.json")
    assert closed is not None
    assert closed["claude_sid"] == "s1"
    assert closed["name"] == "alpha"
    assert closed["cwd"] == "/x"
    assert closed["last_summary"] == "shipped foo"
    assert closed["closed_reason"] == "session_end"
    assert closed["runtime"] == "claude"
    assert isinstance(closed["closed_at"], (int, float))
    # Active is gone
    assert not (fresh / "active" / "s1.json").exists()


def test_session_end_copies_spend_into_closed_record(fresh, monkeypatch):
    """B5: the closed/ record is spend's only durable home — done/turn-end
    events expire on a ~24h TTL and active/ is unlinked right here, while the
    Gardener digest reads weekly."""
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    spend = {"turns": 3, "out_tokens": 1200, "in_tokens": 4500,
             "cache_read_tokens": 9000, "last_turn_out": 400, "last_msg_id": "msg_3"}
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 12345.0, "spend": spend,
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    session_end()
    closed = state.read_json(fresh / "closed" / "s1.json")
    assert closed["spend"] == spend


def test_session_end_copies_account_into_closed_record(fresh, monkeypatch):
    # D8: per-account spend attribution — the close whitelist must not drop the
    # account the worker was spawned on.
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 12345.0, "account": "b",
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    session_end()
    closed = state.read_json(fresh / "closed" / "s1.json")
    assert closed["account"] == "b"


def test_session_end_account_null_when_absent(fresh, monkeypatch):
    # Pre-fix / accountless records close with an explicit null — uniform schema.
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 12345.0,
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    session_end()
    closed = state.read_json(fresh / "closed" / "s1.json")
    assert closed["account"] is None


def test_session_end_closed_record_spend_null_when_never_accumulated(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 0,
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    session_end()
    closed = state.read_json(fresh / "closed" / "s1.json")
    assert closed is not None
    assert closed["spend"] is None


def test_session_end_does_not_archive_manager_to_closed(fresh, monkeypatch):
    """Managers don't get resumed via resume_worker — no closed/ record for them."""
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    state.write_json_atomic(fresh / "active" / "mgr-1.json", {
        "claude_sid": "mgr-1", "agent": "manager", "name": "grumpy-yak", "cwd": "/x",
        "iterm_sid": "i9", "pid": 1, "started_at": 0, "domain": "general",
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-1"})))
    session_end()
    assert not (fresh / "closed" / "mgr-1.json").exists()
    assert not (fresh / "active" / "mgr-1.json").exists()


def test_session_end_drops_worker_questions(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 0,
    })
    state.write_json_atomic(fresh / "questions" / "q1.json", {
        "question_id": "q1", "worker_sid": "s1", "worker_name": "alpha", "question": "...", "asked_at": 0,
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    session_end()
    assert not (fresh / "active" / "s1.json").exists()
    assert not (fresh / "questions" / "q1.json").exists()

def test_session_start_styles_tab_for_manager(fresh, monkeypatch):
    _reset_driver(monkeypatch)
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    monkeypatch.setenv("TMUX_PANE", "42")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr("dockwright.hooks.subprocess.run", fake_run)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-1", "cwd": "/x"})))
    session_start()
    assert any("rename-window" in a for a in calls)
    assert any("set-window-option" in a for a in calls)
    # Every tmux call goes to the orchestrator socket.
    assert all(a[:3] == ["tmux", "-L", "S"] for a in calls)
    # Every paint call must be scoped to this session's own pane, not the
    # currently-focused one.
    paint_calls = [a for a in calls if "rename-window" in a or "set-window-option" in a]
    assert all("-t" in a and a[a.index("-t") + 1] == "42" for a in paint_calls)


def test_style_manager_tab_has_no_emoji_keeps_name_domain_and_pink(monkeypatch):
    from dockwright.hooks import _style_manager_tab, MANAGER_TAB_COLOR
    titles, colors = [], []
    monkeypatch.setattr("dockwright.hooks._set_tab_title", lambda t: titles.append(t))
    monkeypatch.setattr("dockwright.hooks._set_tab_color", lambda c: colors.append(c))
    _style_manager_tab(name="boss", domain="payments")
    assert titles == ["boss · payments"]
    assert "🎯" not in titles[0]
    assert colors == [MANAGER_TAB_COLOR]


def test_style_manager_tab_sentinel_domain_omits_suffix_no_emoji(monkeypatch):
    from dockwright.hooks import _style_manager_tab
    titles = []
    monkeypatch.setattr("dockwright.hooks._set_tab_title", lambda t: titles.append(t))
    monkeypatch.setattr("dockwright.hooks._set_tab_color", lambda c: None)
    _style_manager_tab(name="boss", domain="manager")
    assert titles == ["boss"]
    assert "🎯" not in titles[0]


def _color_args(calls):
    """The two set-window-option calls a single set_tab_color emits, keyed by
    the style option they target (active = current-style, inactive = style)."""
    opts = {}
    for a in calls:
        if "set-window-option" in a:
            opt = a[a.index("set-window-option") + 3]  # ... -t <pane> <opt> <value>
            opts[opt] = a
    return opts


def test_session_start_worker_sets_gray_tab_color(fresh, monkeypatch):
    _reset_driver(monkeypatch)
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.setenv("TMUX_PANE", "42")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr("dockwright.hooks.subprocess.run", fake_run)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "w1", "cwd": "/x"})))
    session_start()
    color_calls = [a for a in calls if "set-window-option" in a]
    assert len(color_calls) == 2  # current-style (active) + style (inactive)
    opts = _color_args(calls)
    assert "bg=#444444,fg=#ffffff" in opts["window-status-current-style"]
    assert "bg=#222222,fg=#ffffff" in opts["window-status-style"]
    assert all(a[a.index("-t") + 1] == "42" for a in color_calls)
    # Workers get a cosmetic title: <funny_name> · <task_name> (no emoji; the
    # status-row chip carries the 🔧).
    title_calls = [a for a in calls if "rename-window" in a]
    assert len(title_calls) == 1
    assert any("alpha" in arg for arg in title_calls[0])
    assert not any("🔧" in arg for arg in title_calls[0])
    assert title_calls[0][title_calls[0].index("-t") + 1] == "42"


def test_session_start_worker_with_pending_question_paints_question_color(fresh, monkeypatch):
    """When a resumed worker has a pending question, SessionStart paints red, not gray."""
    _reset_driver(monkeypatch)
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.setenv("TMUX_PANE", "42")
    # Pre-existing question for sid s1
    state.write_json_atomic(fresh / "questions" / "q1.json", {
        "question_id": "q1", "worker_sid": "s1", "worker_name": "alpha",
        "question": "what now?", "asked_at": 0,
    })
    calls = []
    monkeypatch.setattr(
        "dockwright.hooks.subprocess.run",
        lambda a, **kw: calls.append(a) or type("R", (), {"returncode": 0})(),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    color_calls = [a for a in calls if "set-window-option" in a]
    assert len(color_calls) == 2
    opts = _color_args(calls)
    assert "bg=#aa3300,fg=#ffffff" in opts["window-status-current-style"]
    assert "bg=#441100,fg=#ffffff" in opts["window-status-style"]


def test_session_start_worker_skips_paint_when_no_pane_id(fresh, monkeypatch):
    """Without TMUX_PANE the paint can't be scoped to this session's tab.
    An unscoped command would hit whatever tab is focused, repainting the wrong
    session's tab — so the setters must no-op entirely instead of emitting it."""
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.delenv("TMUX_PANE", raising=False)
    calls = []

    monkeypatch.setattr(
        "dockwright.hooks.subprocess.run",
        lambda a, **kw: calls.append(a) or type("R", (), {"returncode": 0})(),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "w1", "cwd": "/x"})))
    session_start()
    assert [a for a in calls if "rename-window" in a or "set-window-option" in a] == []


def test_user_prompt_submit_sets_yellow_tab_color(fresh, monkeypatch, capsys):
    _reset_driver(monkeypatch)
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("TMUX_PANE", "42")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 0,
    })
    calls = []
    monkeypatch.setattr(
        "dockwright.hooks.subprocess.run",
        lambda a, **kw: calls.append(a) or type("R", (), {"returncode": 0})(),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "prompt": "go"})))
    user_prompt_submit()
    color_calls = [a for a in calls if "set-window-option" in a]
    assert len(color_calls) == 2
    opts = _color_args(calls)
    assert "bg=#aa8800,fg=#ffffff" in opts["window-status-current-style"]
    assert "bg=#443300,fg=#ffffff" in opts["window-status-style"]
    assert all(a[a.index("-t") + 1] == "42" for a in color_calls)


def test_user_prompt_submit_skips_color_for_manager(fresh, monkeypatch):
    """The mid-task tint is worker-only; manager keeps its pink even mid-turn."""
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    state.write_json_atomic(fresh / "active" / "mgr-1.json", {
        "claude_sid": "mgr-1", "agent": "manager", "name": "manager", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 0,
    })
    calls = []
    monkeypatch.setattr(
        "dockwright.hooks.subprocess.run",
        lambda a, **kw: calls.append(a) or type("R", (), {"returncode": 0})(),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-1", "prompt": "go"})))
    user_prompt_submit()
    assert calls == []


def test_stop_hook_sets_gray_when_no_pending_question(fresh, monkeypatch):
    _reset_driver(monkeypatch)
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("TMUX_PANE", "42")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 0,
    })
    calls = []
    monkeypatch.setattr(
        "dockwright.hooks.subprocess.run",
        lambda a, **kw: calls.append(a) or type("R", (), {"returncode": 0})(),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    stop_hook()
    color_calls = [a for a in calls if "set-window-option" in a]
    assert len(color_calls) == 2
    opts = _color_args(calls)
    assert "bg=#444444,fg=#ffffff" in opts["window-status-current-style"]
    assert "bg=#222222,fg=#ffffff" in opts["window-status-style"]
    assert all(a[a.index("-t") + 1] == "42" for a in color_calls)


def test_stop_hook_sets_red_when_pending_question_exists(fresh, monkeypatch):
    _reset_driver(monkeypatch)
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("TMUX_PANE", "42")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 0,
    })
    state.write_json_atomic(fresh / "questions" / "q1.json", {
        "question_id": "q1", "worker_sid": "s1", "worker_name": "alpha",
        "question": "what now?", "asked_at": 0,
    })
    calls = []
    monkeypatch.setattr(
        "dockwright.hooks.subprocess.run",
        lambda a, **kw: calls.append(a) or type("R", (), {"returncode": 0})(),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    stop_hook()
    color_calls = [a for a in calls if "set-window-option" in a]
    assert len(color_calls) == 2
    opts = _color_args(calls)
    assert "bg=#aa3300,fg=#ffffff" in opts["window-status-current-style"]
    assert "bg=#441100,fg=#ffffff" in opts["window-status-style"]
    assert all(a[a.index("-t") + 1] == "42" for a in color_calls)


def test_stop_hook_skips_color_for_manager(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    state.write_json_atomic(fresh / "active" / "mgr-1.json", {
        "claude_sid": "mgr-1", "agent": "manager", "name": "manager", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 0,
    })
    calls = []
    monkeypatch.setattr(
        "dockwright.hooks.subprocess.run",
        lambda a, **kw: calls.append(a) or type("R", (), {"returncode": 0})(),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-1"})))
    stop_hook()
    assert calls == []


def test_tmux_failure_does_not_crash_worker_hook(fresh, monkeypatch):
    """A tmux subprocess failure must not crash session_start — the active record still lands."""
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")

    def boom(args, **kwargs):
        raise FileNotFoundError("tmux not installed")

    monkeypatch.setattr("dockwright.hooks.subprocess.run", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "w1", "cwd": "/x"})))
    session_start()
    assert (fresh / "active" / "w1.json").exists()

def test_session_start_tmux_failure_does_not_crash(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "manager")

    def boom(args, **kwargs):
        raise FileNotFoundError("tmux not installed")

    monkeypatch.setattr("dockwright.hooks.subprocess.run", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-1", "cwd": "/x"})))
    # Must not raise — the active record must still be written
    session_start()
    assert (fresh / "active" / "mgr-1.json").exists()

def test_session_start_dedupes_name_with_suffix(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    # pre-populate an existing record with name=alpha and different sid.
    # Use a live pid so the stale-prune step doesn't drop it before the collision check.
    state.write_json_atomic(fresh / "active" / "other.json", {
        "claude_sid": "other", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i0", "pid": os.getpid(), "started_at": 0,
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "s1.json")
    assert record["name"] == "alpha-2"

def test_stop_hook_writes_turn_end_marker(fresh, monkeypatch):
    """Stop hook writes a one-shot turn-ends/<sid>-<ms>.json that the manager monitor watches."""
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 0,
        "last_summary": "did stuff", "last_turn_at": "2026-05-19T00:00:00Z",
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    monkeypatch.setattr(
        "dockwright.hooks.subprocess.run",
        lambda a, **kw: type("R", (), {"returncode": 0})(),
    )
    stop_hook()
    turn_ends = list((fresh / "turn-ends").rglob("*.json"))
    assert len(turn_ends) == 1
    marker = state.read_json(turn_ends[0])
    assert marker["sid"] == "s1"
    assert marker["agent"] == "worker"
    assert marker["name"] == "alpha"
    assert marker["last_summary"] == "did stuff"
    # Filename pattern is <sid>-<ms-ts>.json
    assert turn_ends[0].name.startswith("s1-")
    assert turn_ends[0].name.endswith(".json")
    # null-parent worker → written to the shared _unscoped bucket
    assert turn_ends[0].parent.name == paths.UNSCOPED_BUCKET


def test_stop_hook_scopes_turn_end_to_parent_manager(fresh, monkeypatch):
    """A worker with a parent manager writes its turn-end into turn-ends/<manager>/."""
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 0,
        "last_summary": "did stuff", "last_turn_at": "2026-05-19T00:00:00Z",
        "parent_manager_name": "manager-a",
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    monkeypatch.setattr(
        "dockwright.hooks.subprocess.run",
        lambda a, **kw: type("R", (), {"returncode": 0})(),
    )
    stop_hook()
    scoped = list((fresh / "turn-ends" / "manager-a").glob("*.json"))
    assert len(scoped) == 1
    assert state.read_json(scoped[0])["sid"] == "s1"
    # nothing leaked into the unscoped bucket
    assert list((fresh / "turn-ends" / paths.UNSCOPED_BUCKET).glob("*.json")) == []


def test_stop_hook_turn_end_marker_includes_runtime(fresh, monkeypatch):
    """The marker carries runtime so the silent-finish re-read resolves the
    right transcript; absent on the record it defaults to 'claude'."""
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    monkeypatch.setattr(
        "dockwright.hooks.subprocess.run",
        lambda a, **kw: type("R", (), {"returncode": 0})(),
    )
    # record carrying an explicit runtime -> passed through
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "pid": 1, "started_at": 0, "runtime": "codex",
        "last_summary": "did stuff", "last_turn_at": "2026-05-19T00:00:00Z",
    })
    stop_hook()
    marker = state.read_json(list((fresh / "turn-ends").rglob("*.json"))[0])
    assert marker["runtime"] == "codex"

    # record with no runtime -> defaults to "claude"
    shutil.rmtree(fresh / "turn-ends")
    state.write_json_atomic(fresh / "active" / "s2.json", {
        "claude_sid": "s2", "agent": "worker", "name": "beta", "cwd": "/x",
        "pid": 1, "started_at": 0,
        "last_summary": "did stuff", "last_turn_at": "2026-05-19T00:00:00Z",
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s2"})))
    stop_hook()
    marker = state.read_json(list((fresh / "turn-ends").rglob("*.json"))[0])
    assert marker["runtime"] == "claude"


def test_stop_hook_scopes_manager_turn_end_to_own_name(fresh, monkeypatch):
    """A manager has no parent, so its own turn-end is keyed on its own name —
    landing in turn-ends/<manager>/ (NOT _unscoped, which every peer manager watches).
    The monitor's self-sid grep then suppresses the manager's own ping."""
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    state.write_json_atomic(fresh / "active" / "mgr-1.json", {
        "claude_sid": "mgr-1", "agent": "manager", "name": "weary-badger", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 0,
        "last_summary": "managed", "last_turn_at": "2026-05-19T00:00:00Z",
        "parent_manager_name": None,
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-1"})))
    monkeypatch.setattr(
        "dockwright.hooks.subprocess.run",
        lambda a, **kw: type("R", (), {"returncode": 0})(),
    )
    stop_hook()
    scoped = list((fresh / "turn-ends" / "weary-badger").glob("*.json"))
    assert len(scoped) == 1
    marker = state.read_json(scoped[0])
    assert marker["sid"] == "mgr-1"
    assert marker["agent"] == "manager"
    # a manager's own turn-end must NOT leak into the shared bucket
    assert list((fresh / "turn-ends" / paths.UNSCOPED_BUCKET).glob("*.json")) == []


def test_stop_hook_preserves_old_summary_on_empty_transcript(fresh, monkeypatch):
    """If the transcript can't be summarized, don't clobber the existing summary."""
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 0,
        "last_summary": "previously seen", "last_turn_at": "2026-01-01T00:00:00Z",
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    stop_hook()
    record = state.read_json(fresh / "active" / "s1.json")
    # find_session_log returns None for non-existent sid → branch not entered → preserved
    assert record["last_summary"] == "previously seen"


def test_stop_hook_reads_codex_runtime_transcript(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("HOME", str(fresh))
    sessions = fresh / ".codex" / "sessions" / "2026" / "05" / "26"
    sessions.mkdir(parents=True)
    log = sessions / "rollout-2026-05-26T10-55-35-s1.jsonl"
    log.write_text(json.dumps({
        "timestamp": "2026-05-26T04:01:00Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "codex finished"}],
        },
    }))
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 0, "runtime": "codex",
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    monkeypatch.setattr(
        "dockwright.hooks.subprocess.run",
        lambda a, **kw: type("R", (), {"returncode": 0})(),
    )
    stop_hook()
    record = state.read_json(fresh / "active" / "s1.json")
    assert record["last_summary"] == "codex finished"
    assert record["last_turn_at"] == "2026-05-26T04:01:00Z"


def test_stop_hook_records_uptime_for_sleep_correct_idle(fresh, monkeypatch):
    """Stop hook must record last_turn_at_uptime so stale_monitor's idle-elapsed math
    doesn't burn the 2h grace during laptop sleep (wall ticks through sleep, uptime doesn't)."""
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 0,
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    monkeypatch.setattr(
        "dockwright.hooks.subprocess.run",
        lambda a, **kw: type("R", (), {"returncode": 0})(),
    )
    stop_hook()
    record = state.read_json(fresh / "active" / "s1.json")
    assert isinstance(record["last_turn_at_uptime"], float)
    assert record["last_turn_at_uptime"] > 0


def _reset_driver(monkeypatch):
    """Reset the process-wide cache so each paint test gets a fresh TmuxDriver."""
    import dockwright.terminal as terminal
    terminal._DRIVER = None


def _capture_tab_calls(monkeypatch):
    _reset_driver(monkeypatch)
    calls = []
    monkeypatch.setattr(
        "dockwright.hooks.subprocess.run",
        lambda a, **kw: calls.append(a) or type("R", (), {"returncode": 0})(),
    )
    return calls


def test_set_tab_color_skips_when_pane_unset(monkeypatch):
    """Without TMUX_PANE the paint can't be scoped to this session's tab,
    so it must no-op rather than emit an unscoped command that hits the focused tab."""
    monkeypatch.delenv("TMUX_PANE", raising=False)
    calls = _capture_tab_calls(monkeypatch)
    _set_tab_color(MANAGER_TAB_COLOR)
    assert calls == []


def test_set_tab_title_skips_when_pane_unset(monkeypatch):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    calls = _capture_tab_calls(monkeypatch)
    _set_tab_title("🎯 manager")
    assert calls == []


def test_set_tab_color_scopes_to_pane(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "42")
    calls = _capture_tab_calls(monkeypatch)
    _set_tab_color(MANAGER_TAB_COLOR)
    assert len(calls) == 2  # current-style (active) + style (inactive)
    assert all("set-window-option" in c for c in calls)
    assert all(c[c.index("-t") + 1] == "42" for c in calls)


def test_set_tab_title_scopes_to_pane(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "42")
    calls = _capture_tab_calls(monkeypatch)
    _set_tab_title("🎯 manager")
    assert len(calls) == 1
    assert "rename-window" in calls[0]
    assert calls[0][calls[0].index("-t") + 1] == "42"
    assert "🎯 manager" in calls[0]


@pytest.mark.parametrize("setter", [
    lambda: _set_tab_color(MANAGER_TAB_COLOR),
    lambda: _set_tab_title("🔧 some-worker"),
])
def test_tab_setters_scope_to_pane(monkeypatch, setter):
    """Regression: tab paints MUST scope by this session's own pane, never an
    unscoped command. An unscoped rename/set-window-option lands on whatever
    window is current, repainting a neighbor's (or a manager's) tab. The driver
    must always pass `-t <TMUX_PANE>`.
    """
    monkeypatch.setenv("TMUX_PANE", "42")
    calls = _capture_tab_calls(monkeypatch)
    setter()
    assert calls  # at least one tmux call emitted
    assert all("-t" in c and c[c.index("-t") + 1] == "42" for c in calls)


# --- managers get funny <adjective>-<creature> names, never the literal "manager" ---

def test_session_start_manager_gets_funny_name_not_literal(fresh, monkeypatch):
    from dockwright import names
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.delenv("CLAUDE_WORKER_NAME", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-1", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "mgr-1.json")
    name = record["name"]
    assert record["agent"] == "manager"
    assert name != "manager"
    assert not name.startswith("manager-")
    adj, noun = name.split("-", 1)
    assert adj in names.ADJECTIVES
    assert noun in names.MANAGER_NOUNS


def test_session_start_worker_funny_name_draws_from_worker_pool(fresh, monkeypatch):
    from dockwright import names
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "task-label")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "w-1", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "w-1.json")
    adj, noun = record["funny_name"].split("-", 1)
    assert adj in names.ADJECTIVES
    assert noun in names.WORKER_NOUNS


def test_session_start_manager_honors_explicit_name(fresh, monkeypatch):
    # Takeover threads the inherited funny name through CLAUDE_WORKER_NAME; the hook
    # must keep honoring an explicit name instead of rolling a fresh one.
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "happy-otter")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-1", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "mgr-1.json")
    assert record["name"] == "happy-otter"


def test_session_start_two_managers_get_distinct_non_literal_names(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.delenv("CLAUDE_WORKER_NAME", raising=False)
    # Distinct CLI pids — two live sessions are two OS processes; sharing one
    # pid would (correctly) read as a /clear sid rotation, not a peer.
    monkeypatch.setenv("CLAUDE_PARENT_PID", str(os.getpid()))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-1", "cwd": "/x"})))
    session_start()
    monkeypatch.setenv("CLAUDE_PARENT_PID", str(os.getppid()))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-2", "cwd": "/x"})))
    session_start()
    rec1 = state.read_json(fresh / "active" / "mgr-1.json")
    rec2 = state.read_json(fresh / "active" / "mgr-2.json")
    for rec in (rec1, rec2):
        assert rec["name"] != "manager"
        assert not rec["name"].startswith("manager-")
    assert rec1["name"] != rec2["name"]


def test_manager_roll_taken_set_spans_routing_and_funny_names(fresh, monkeypatch):
    """Pools are role-disjoint for NEW rolls, but active legacy records may
    carry old-pool names — a fresh manager roll must treat BOTH peer routing
    names and peer worker funny_names as taken."""
    state.write_json_atomic(fresh / "active" / "w-legacy.json", {
        "claude_sid": "w-legacy", "agent": "worker", "name": "worker-1",
        "funny_name": "happy-dragon", "pid": 1,
    })
    captured = {}

    def fake_roll(is_taken):
        captured["is_taken"] = is_taken
        return "calm-ghost"

    monkeypatch.setattr("dockwright.names.roll_manager_name", fake_roll)
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.delenv("CLAUDE_WORKER_NAME", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-1", "cwd": "/x"})))
    session_start()
    assert captured["is_taken"]("happy-dragon") is True
    assert captured["is_taken"]("worker-1") is True
    assert captured["is_taken"]("free-name") is False


def test_worker_roll_taken_set_spans_routing_and_funny_names(fresh, monkeypatch):
    """A legacy manager's routing name came from the old combined pool; a fresh
    worker funny-name roll must not collide with it."""
    state.write_json_atomic(fresh / "active" / "mgr-legacy.json", {
        "claude_sid": "mgr-legacy", "agent": "manager", "name": "happy-otter",
        "funny_name": None, "pid": 1,
    })
    state.write_json_atomic(fresh / "active" / "w-peer.json", {
        "claude_sid": "w-peer", "agent": "worker", "name": "worker-2",
        "funny_name": "calm-panda", "pid": 1,
    })
    captured = {}

    def fake_roll(is_taken):
        captured["is_taken"] = is_taken
        return "quick-fox"

    monkeypatch.setattr("dockwright.names.roll_worker_name", fake_roll)
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "task-label")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "w-1", "cwd": "/x"})))
    session_start()
    assert captured["is_taken"]("happy-otter") is True
    assert captured["is_taken"]("calm-panda") is True
    assert captured["is_taken"]("free-name") is False


def test_unmocked_hook_paint_cannot_reach_a_real_tmux_binary(fresh, monkeypatch, tmp_path, no_live_tmux):
    """Regression for the smug-kestrel incident: pytest inherits TMUX_PANE from
    the tmux session hosting it (a developer terminal or an orchestrator worker
    tab). A test that exercises session_start WITHOUT patching subprocess.run
    must still never execute a real `tmux` binary — the original incident rolled
    the manager name "smug-kestrel" (state written to tmp_path, so no record
    anywhere) and repainted the hosting worker's live tab with manager styling,
    scoped to the inherited pane id. The conftest no_live_tmux guard must absorb
    the call before it reaches PATH."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    invocation_log = tmp_path / "tmux-invocations.log"
    fake_tmux = bin_dir / "tmux"
    fake_tmux.write_text(f"#!/bin/sh\necho \"$@\" >> '{invocation_log}'\n")
    fake_tmux.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    # Simulate the leaked host-session env the incident ran under.
    monkeypatch.setenv("TMUX_PANE", "132")
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-leak", "cwd": "/x"})))
    session_start()
    assert not invocation_log.exists(), (
        f"a live tmux invocation escaped the test sandbox:\n{invocation_log.read_text() if invocation_log.exists() else ''}"
    )


# --- Ownership plane: SessionStart claim (spec §12) ---

def _seed_pending(assignment_id="aid1", requested="alpha"):
    paths.ASSIGNMENTS_PENDING.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.ASSIGNMENTS_PENDING / f"{assignment_id}.json", {
        "assignment_id": assignment_id, "requested_name": requested, "name": requested,
        "initial_prompt": "do the thing", "preset": None, "cwd": "/x", "branch": None,
        "manager_sid": "mgr-1", "parent_manager_name": "boss", "runtime": "claude",
        "ticket": None, "spawned_at": 1.0,
    })


def test_session_start_claims_pending_assignment(fresh, monkeypatch):
    _seed_pending()
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.setenv("CLAUDE_ASSIGNMENT_ID", "aid1")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    assert not (paths.ASSIGNMENTS_PENDING / "aid1.json").exists()
    record = state.read_json(paths.ASSIGNMENTS / "s1.json")
    assert record["claude_sid"] == "s1"
    assert record["name"] == "alpha"
    assert record["initial_prompt"] == "do the thing"
    assert record["claimed_at"] > 0


def test_claim_records_suffixed_registered_name(fresh, monkeypatch):
    _seed_pending(requested="alpha")
    state.write_json_atomic(paths.ACTIVE / "other.json", {     # name thief
        "claude_sid": "other", "agent": "worker", "name": "alpha",
        "cwd": "/x", "pid": os.getpid(), "started_at": 0,
    })
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.setenv("CLAUDE_ASSIGNMENT_ID", "aid1")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    record = state.read_json(paths.ASSIGNMENTS / "s1.json")
    assert record["name"] == "alpha-2"
    assert record["requested_name"] == "alpha"


def test_claim_no_env_is_noop(fresh, monkeypatch):
    _seed_pending()
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    assert (paths.ASSIGNMENTS_PENDING / "aid1.json").exists()   # untouched
    assert not (paths.ASSIGNMENTS / "s1.json").exists()
    assert state.read_json(paths.ACTIVE / "s1.json")            # registration unaffected


def test_claim_skips_when_assignment_exists(fresh, monkeypatch):
    _seed_pending()
    paths.ASSIGNMENTS.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.ASSIGNMENTS / "s1.json", {"claude_sid": "s1", "initial_prompt": "original"})
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.setenv("CLAUDE_ASSIGNMENT_ID", "aid1")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    assert state.read_json(paths.ASSIGNMENTS / "s1.json")["initial_prompt"] == "original"


def test_session_end_preserves_assignment(fresh, monkeypatch):
    paths.ASSIGNMENTS.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.ASSIGNMENTS / "s1.json", {"claude_sid": "s1"})
    state.write_json_atomic(paths.ACTIVE / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "pid": os.getpid(), "started_at": 0,
    })
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    session_end()
    assert (paths.ASSIGNMENTS / "s1.json").exists()             # structural-survival pin
    assert (paths.CLOSED / "s1.json").exists()


def test_claim_never_raises_on_malformed_assignment_id(fresh, monkeypatch):
    # whitespace-only id would hit _safe_segment's ValueError; the claim must
    # degrade silently — hooks run at every orchestrator session start (Important #4)
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.setenv("CLAUDE_ASSIGNMENT_ID", "   ")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()                                  # must not raise
    assert state.read_json(paths.ACTIVE / "s1.json")  # registration survived


def test_session_start_overrides_window_id_from_spawn_sidecar(fresh, monkeypatch):
    """A2: even if the worker's shell built the wrong driver (window_id derives
    to ""), the spawn-captured sidecar overrides the record's window_id."""
    import dockwright.hooks as hooks
    monkeypatch.setattr(hooks, "get_driver", lambda: type("D", (), {
        "current_pane_id": lambda self: None,
        "set_tab_title": lambda self, *a: None,
        "set_tab_color": lambda self, *a: None,
    })())
    assignment_id = "asg-123"
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_ASSIGNMENT_ID", assignment_id)
    monkeypatch.delenv("CLAUDE_ITERM_SID", raising=False)
    paths.ASSIGNMENTS_PENDING.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.pending_assignment_path(assignment_id),
                            {"assignment_id": assignment_id, "name": "w1"})
    paths.pending_window_path(assignment_id).write_text("777")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "sid-w1", "cwd": "/tmp/wt"})))
    session_start()
    rec = state.read_json(paths.ACTIVE / "sid-w1.json")
    assert rec["window_id"] == "777"
    assert not paths.pending_window_path(assignment_id).exists()


# --- stop_hook spend telemetry (observability only) -------------------------

def _spend_usage(output=0, input_tokens=0, cache_read=0):
    return {
        "input_tokens": input_tokens,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": cache_read,
        "output_tokens": output,
        "service_tier": "standard",
    }


def _spend_assistant_line(msg_id, usage):
    return json.dumps({
        "type": "assistant",
        "timestamp": "2026-06-11T00:00:00Z",
        "message": {
            "id": msg_id, "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "ok"}], "usage": usage,
        },
    })


def _write_worker_transcript(home, sid, lines):
    project_dir = home / ".claude" / "projects" / "-Users-x"
    project_dir.mkdir(parents=True, exist_ok=True)
    log = project_dir / f"{sid}.jsonl"
    log.write_text("\n".join(lines) + "\n")
    return log


def _stop(monkeypatch, sid="s1"):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": sid})))
    stop_hook()


def test_stop_hook_accumulates_spend_across_turns(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("HOME", str(fresh))
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "pid": 1, "started_at": 0, "runtime": "claude",
    })
    usage = _spend_usage(output=100, input_tokens=3, cache_read=1000)
    log = _write_worker_transcript(fresh, "s1", [
        _spend_assistant_line("msg_a", usage),
        _spend_assistant_line("msg_a", usage),     # split event, same API call
        _spend_assistant_line("msg_b", _spend_usage(output=50, input_tokens=1, cache_read=500)),
    ])
    _stop(monkeypatch)
    record = state.read_json(fresh / "active" / "s1.json")
    assert record["spend"]["turns"] == 1
    assert record["spend"]["out_tokens"] == 150
    assert record["spend"]["in_tokens"] == 4
    assert record["spend"]["cache_read_tokens"] == 1500
    assert record["spend"]["last_turn_out"] == 150
    assert record["spend"]["last_msg_id"] == "msg_b"

    with log.open("a") as f:
        f.write(_spend_assistant_line("msg_c", _spend_usage(output=7, input_tokens=2)) + "\n")
    _stop(monkeypatch)
    record = state.read_json(fresh / "active" / "s1.json")
    assert record["spend"]["turns"] == 2
    assert record["spend"]["out_tokens"] == 157
    assert record["spend"]["last_turn_out"] == 7
    assert record["spend"]["last_msg_id"] == "msg_c"


def test_stop_hook_spend_skips_silently_on_malformed_transcript(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("HOME", str(fresh))
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "pid": 1, "started_at": 0, "runtime": "claude",
    })
    _write_worker_transcript(fresh, "s1", ["{{{garbage", json.dumps({"type": "user"})])
    _stop(monkeypatch)
    record = state.read_json(fresh / "active" / "s1.json")
    assert "spend" not in record
    assert record["state"] == "idle"                  # hook completed normally


def test_stop_hook_survives_spend_parser_raising(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("HOME", str(fresh))
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "pid": 1, "started_at": 0, "runtime": "claude",
    })
    _write_worker_transcript(fresh, "s1", [_spend_assistant_line("msg_a", _spend_usage(output=1))])
    monkeypatch.setattr(
        "dockwright.transcript.tail_usage_entries",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    _stop(monkeypatch)                                # must not raise
    record = state.read_json(fresh / "active" / "s1.json")
    assert record["state"] == "idle"
    assert "spend" not in record
    assert len(list((fresh / "turn-ends").rglob("*.json"))) == 1


def test_stop_hook_skips_spend_for_codex_runtime(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("HOME", str(fresh))
    state.write_json_atomic(fresh / "active" / "sx.json", {
        "claude_sid": "sx", "agent": "worker", "name": "beta", "cwd": "/x",
        "pid": 1, "started_at": 0, "runtime": "codex",
    })
    sessions = fresh / ".codex" / "sessions" / "2026" / "06" / "11"
    sessions.mkdir(parents=True)
    (sessions / "rollout-2026-06-11T00-00-00-sx.jsonl").write_text(
        _spend_assistant_line("msg_a", _spend_usage(output=5)) + "\n")
    _stop(monkeypatch, sid="sx")
    record = state.read_json(fresh / "active" / "sx.json")
    assert "spend" not in record


# ---- nested sub-session detection + muting --------------------------------
# A `claude -p` (or interactive claude) launched from WITHIN a registered
# session's Bash inherits the orchestrator env (CLAUDE_AGENT, CLAUDE_WORKER_NAME,
# CLAUDE_PARENT_MANAGER) and would self-register as a ghost worker whose Stop
# hook pings the manager on every turn. Detection is process-ancestry (another
# active record's pid is an ancestor of this CLI) with a same-pane-window +
# live-pid fallback; env markers (CLAUDECODE, CLAUDE_CODE_CHILD_SESSION,
# CLAUDE_CODE_SESSION_ID) are normalized by the CLI for hook children and
# cannot discriminate.

from dockwright.hooks import _ancestor_pids, _detect_nested_parent


def _write_parent_record(sid="parent-sid", name="parent-worker", pid=4242,
                         window_id="175", agent="worker", **overrides):
    record = {
        "claude_sid": sid, "agent": agent, "name": name, "cwd": "/x",
        "window_id": window_id, "pid": pid, "started_at": 0,
        "state": "processing", "parent_manager_name": "mgr",
    }
    record.update(overrides)
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", record)
    return record


def test_ancestor_pids_walks_ppid_chain(monkeypatch):
    from dockwright import identity
    table = {100: 50, 50: 10, 10: 1}
    monkeypatch.setattr(identity, "_ppid_of", lambda pid: table.get(pid))
    assert _ancestor_pids(100) == {50, 10}


def test_ancestor_pids_stops_on_lookup_failure(monkeypatch):
    from dockwright import identity
    monkeypatch.setattr(identity, "_ppid_of", lambda pid: None)
    assert _ancestor_pids(100) == set()


def test_detect_nested_parent_via_ancestry(fresh, monkeypatch):
    _write_parent_record(pid=4242)
    monkeypatch.setattr("dockwright.hooks._ancestor_pids",
                        lambda pid: {4242, 59070})
    monkeypatch.setattr("dockwright.hooks._pid_looks_like_session", lambda pid: True)
    parent = _detect_nested_parent("child-sid", cli_pid=9999)
    assert parent == {"sid": "parent-sid", "name": "parent-worker"}


def test_detect_nested_parent_never_raises(fresh, monkeypatch):
    def boom(pid):
        raise RuntimeError("ps exploded")
    monkeypatch.setattr("dockwright.hooks._ancestor_pids", boom)
    assert _detect_nested_parent("child-sid", cli_pid=9999) is None


def test_session_start_registers_nested_when_parent_cli_is_ancestor(fresh, monkeypatch):
    _write_parent_record(pid=4242)
    monkeypatch.setattr("dockwright.hooks._ancestor_pids",
                        lambda pid: {4242})
    monkeypatch.setattr("dockwright.hooks._pid_looks_like_session", lambda pid: True)
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "parent-worker")   # inherited env
    monkeypatch.setenv("CLAUDE_PARENT_MANAGER", "mgr")
    monkeypatch.setenv("CLAUDE_PARENT_PID", "9999")
    monkeypatch.setenv("CLAUDE_WORKER_RUNTIME", "codex")        # inherited noise
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "abcd1234-rest-of-sid", "cwd": "/x"})))
    session_start()
    record = state.read_json(paths.ACTIVE / "abcd1234-rest-of-sid.json")
    assert record["nested"] is True
    assert record["nested_parent_sid"] == "parent-sid"
    assert record["nested_parent_name"] == "parent-worker"
    assert record["name"] == "nested-abcd1234"      # NOT parent-worker-2
    assert record["funny_name"] is None
    assert record["window_id"] == ""                # parent owns the window
    assert record["parent_manager_name"] == "mgr"   # kept for visibility scoping
    assert record["runtime"] == "claude"            # not the inherited codex marker


def test_session_start_registers_nested_via_same_window_fallback(fresh, monkeypatch):
    """Detached children (nohup / orphaned background) lose the ancestry chain;
    the inherited TMUX_PANE matching a LIVE other record still flags them."""
    _reset_driver(monkeypatch)
    _write_parent_record(pid=4242, window_id="175")
    monkeypatch.setattr("dockwright.hooks._ancestor_pids", lambda pid: set())
    monkeypatch.setattr("dockwright.hooks._pid_alive", lambda pid: pid == 4242)
    monkeypatch.setattr("dockwright.hooks._pid_looks_like_session", lambda pid: True)
    monkeypatch.setenv("TMUX_PANE", "175")
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "parent-worker")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "ffff0000-rest", "cwd": "/x"})))
    session_start()
    record = state.read_json(paths.ACTIVE / "ffff0000-rest.json")
    assert record["nested"] is True
    assert record["nested_parent_name"] == "parent-worker"


def test_session_start_same_window_dead_pid_not_nested(fresh, monkeypatch):
    """A crash-leftover record sharing the window (tab reuse after a dead
    session) must NOT mark the fresh legit session nested."""
    _write_parent_record(pid=4242, window_id="175")
    monkeypatch.setattr("dockwright.hooks._ancestor_pids", lambda pid: set())
    monkeypatch.setattr("dockwright.hooks._pid_alive", lambda pid: False)
    monkeypatch.setenv("TMUX_PANE", "175")
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "fresh-worker")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "s2", "cwd": "/x"})))
    session_start()
    record = state.read_json(paths.ACTIVE / "s2.json")
    assert not record.get("nested")
    assert record["name"] == "fresh-worker"


def test_session_start_not_nested_without_signals(fresh, monkeypatch):
    """Unrelated live peer (different window, not an ancestor) — no false positive."""
    _write_parent_record(pid=4242, window_id="42")
    monkeypatch.setattr("dockwright.hooks._ancestor_pids", lambda pid: set())
    monkeypatch.setattr("dockwright.hooks._pid_alive", lambda pid: True)
    monkeypatch.setenv("TMUX_PANE", "175")
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "fresh-worker")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "s2", "cwd": "/x"})))
    session_start()
    record = state.read_json(paths.ACTIVE / "s2.json")
    assert not record.get("nested")
    assert record["name"] == "fresh-worker"


def test_nested_session_start_skips_assignment_claim(fresh, monkeypatch):
    """An inherited CLAUDE_ASSIGNMENT_ID must not let a nested child steal the
    pending assignment file."""
    _write_parent_record(pid=4242)
    paths.ASSIGNMENTS_PENDING.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.pending_assignment_path("aid1"),
                            {"assignment_id": "aid1"})
    monkeypatch.setattr("dockwright.hooks._ancestor_pids", lambda pid: {4242})
    monkeypatch.setattr("dockwright.hooks._pid_looks_like_session", lambda pid: True)
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "parent-worker")
    monkeypatch.setenv("CLAUDE_ASSIGNMENT_ID", "aid1")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "child-1", "cwd": "/x"})))
    session_start()
    assert state.read_json(paths.ACTIVE / "child-1.json")["nested"] is True
    assert paths.pending_assignment_path("aid1").exists()
    assert not (paths.ASSIGNMENTS / "child-1.json").exists()


def test_nested_session_start_skips_tab_paint(fresh, monkeypatch):
    """The inherited TMUX_PANE is the PARENT's window — painting would
    retitle the parent worker's tab."""
    _write_parent_record(pid=4242)
    calls = _capture_tab_calls(monkeypatch)
    monkeypatch.setattr("dockwright.hooks._ancestor_pids", lambda pid: {4242})
    monkeypatch.setattr("dockwright.hooks._pid_looks_like_session", lambda pid: True)
    monkeypatch.setenv("TMUX_PANE", "175")
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "parent-worker")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "child-1", "cwd": "/x"})))
    session_start()
    assert calls == []


def test_user_prompt_submit_nested_skips_busy_paint(fresh, monkeypatch):
    state.write_json_atomic(paths.ACTIVE / "child-1.json", {
        "claude_sid": "child-1", "agent": "worker", "name": "nested-child001",
        "nested": True, "window_id": "", "pid": 1, "state": "idle",
    })
    calls = _capture_tab_calls(monkeypatch)
    monkeypatch.setenv("TMUX_PANE", "175")
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "child-1"})))
    user_prompt_submit()
    assert state.read_json(paths.ACTIVE / "child-1.json")["state"] == "processing"
    assert calls == []


def test_stop_hook_nested_writes_no_turn_end(fresh, monkeypatch):
    """THE noise fix: a nested session's Stop must never write a turn-end
    marker, while the record itself stays fresh for list_workers debugging."""
    state.write_json_atomic(paths.ACTIVE / "child-1.json", {
        "claude_sid": "child-1", "agent": "worker", "name": "nested-child001",
        "nested": True, "window_id": "", "pid": 1, "state": "processing",
        "parent_manager_name": "mgr", "runtime": "claude",
    })
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "child-1"})))
    stop_hook()
    record = state.read_json(paths.ACTIVE / "child-1.json")
    assert record["state"] == "idle"
    assert list(paths.TURN_ENDS.rglob("*.json")) == []


def test_session_end_nested_not_archived(fresh, monkeypatch):
    state.write_json_atomic(paths.ACTIVE / "child-1.json", {
        "claude_sid": "child-1", "agent": "worker", "name": "nested-child001",
        "nested": True, "window_id": "", "pid": 1, "state": "idle",
    })
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "child-1"})))
    session_end()
    assert not (paths.ACTIVE / "child-1.json").exists()
    assert list(paths.CLOSED.glob("*.json")) == []


def test_session_end_nested_manager_never_distills(fresh, monkeypatch):
    """Fan-out hardening: a nested child of a MANAGER inherits
    CLAUDE_AGENT=manager; its SessionEnd must NOT run the distill (which spawns
    another `claude -p`, which would register nested, whose SessionEnd...)."""
    state.write_json_atomic(paths.ACTIVE / "child-1.json", {
        "claude_sid": "child-1", "agent": "manager", "name": "nested-child001",
        "nested": True, "window_id": "", "pid": 1, "state": "idle",
        "domain": "general",
    })
    distills = []
    from dockwright import distill
    monkeypatch.setattr(distill, "distill_and_write_memory",
                        lambda sid, domain=None: distills.append(sid))
    popens = []
    monkeypatch.setattr(
        "dockwright.hooks.subprocess.Popen",
        lambda *a, **kw: popens.append((a, kw)),
    )
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "child-1"})))
    session_end()
    assert distills == []
    assert popens == []


def test_session_start_refire_preserves_nested_and_skips_paint(fresh, monkeypatch):
    """SessionStart re-fires on resume/compaction; the nested identity and the
    no-paint rule must survive the re-fire."""
    state.write_json_atomic(paths.ACTIVE / "child-1.json", {
        "claude_sid": "child-1", "agent": "worker", "name": "nested-child001",
        "nested": True, "nested_parent_sid": "parent-sid",
        "nested_parent_name": "parent-worker", "funny_name": None,
        "window_id": "", "pid": 1, "state": "idle", "cwd": "/x",
    })
    calls = _capture_tab_calls(monkeypatch)
    monkeypatch.setenv("TMUX_PANE", "175")
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "parent-worker")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "child-1", "cwd": "/x"})))
    session_start()
    record = state.read_json(paths.ACTIVE / "child-1.json")
    assert record["nested"] is True
    assert record["name"] == "nested-child001"
    assert record["window_id"] == ""    # re-fire must not adopt the parent's window
    assert calls == []


# ---- /clear sid rotation (verifier blocker on #62) -------------------------
# /clear gives the SAME CLI process a NEW session id. The old active record
# (same pid, same window, pid alive) must read as a rotation — supersede it
# and re-register under the existing identity — never as a nested session.

def test_session_start_sid_rotation_supersedes_old_record_not_nested(fresh, monkeypatch):
    _reset_driver(monkeypatch)
    own_pid = os.getpid()                               # a LIVE pid, immune to dead-pid pruning
    _write_parent_record(sid="old-sid", name="alpha", pid=own_pid, window_id="175",
                         funny_name="grumpy-camel",
                         transcript_path="/x/.claude/projects/-x/old-sid.jsonl")
    monkeypatch.setattr("dockwright.hooks._ancestor_pids", lambda pid: set())
    monkeypatch.setattr("dockwright.hooks._pid_alive", lambda pid: True)
    monkeypatch.setenv("TMUX_PANE", "175")
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.setenv("CLAUDE_PARENT_MANAGER", "mgr")
    monkeypatch.setenv("CLAUDE_PARENT_PID", str(own_pid))  # SAME pid as old record
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "new-sid", "cwd": "/x"})))
    session_start()
    record = state.read_json(paths.ACTIVE / "new-sid.json")
    assert not record.get("nested")
    assert record["name"] == "alpha"                    # no -2 suffix
    assert record["funny_name"] == "grumpy-camel"       # identity carried over
    assert record["window_id"] == "175"
    # New sid = new transcript; the old sid's path must not ride along until
    # the first Stop re-stamps.
    assert "transcript_path" not in record
    assert not (paths.ACTIVE / "old-sid.json").exists()


def test_session_start_manager_sid_rotation_keeps_identity(fresh, monkeypatch):
    own_pid = os.getpid()
    state.write_json_atomic(paths.ACTIVE / "mgr-old.json", {
        "claude_sid": "mgr-old", "agent": "manager", "name": "happy-otter",
        "cwd": "/x", "window_id": "175", "pid": own_pid, "started_at": 0,
        "state": "idle", "domain": "tickets", "parent_manager_name": None,
        "funny_name": None, "runtime": "claude",
    })
    monkeypatch.setattr("dockwright.hooks._ancestor_pids", lambda pid: set())
    monkeypatch.setattr("dockwright.hooks._pid_alive", lambda pid: True)
    monkeypatch.setenv("TMUX_PANE", "175")
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setenv("CLAUDE_PARENT_PID", str(own_pid))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "mgr-new", "cwd": "/x"})))
    session_start()
    record = state.read_json(paths.ACTIVE / "mgr-new.json")
    assert not record.get("nested")
    assert record["agent"] == "manager"
    assert record["name"] == "happy-otter"              # routing key survives /clear
    assert record["domain"] == "tickets"
    assert not (paths.ACTIVE / "mgr-old.json").exists()
    from dockwright import identity
    assert identity._list_manager_records()[0]["claude_sid"] == "mgr-new"


def test_session_start_rotation_drops_old_sid_questions(fresh, monkeypatch):
    """The old conversation is gone after /clear — its pending questions can
    never be answered into it."""
    own_pid = os.getpid()
    _write_parent_record(sid="old-sid", name="alpha", pid=own_pid, window_id="175")
    paths.QUESTIONS.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(paths.QUESTIONS / "q1.json",
                            {"question_id": "q1", "worker_sid": "old-sid"})
    monkeypatch.setattr("dockwright.hooks._ancestor_pids", lambda pid: set())
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.setenv("CLAUDE_PARENT_PID", str(own_pid))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "new-sid", "cwd": "/x"})))
    session_start()
    assert not (paths.QUESTIONS / "q1.json").exists()


def test_detect_nested_parent_ignores_same_process_record(fresh, monkeypatch):
    """Defense in depth: even if the rotation pre-step is bypassed, the window
    fallback must never match a record owned by THIS process."""
    _write_parent_record(sid="old-sid", pid=9999, window_id="175")
    monkeypatch.setattr("dockwright.hooks._ancestor_pids", lambda pid: set())
    monkeypatch.setattr("dockwright.hooks._pid_alive", lambda pid: True)
    monkeypatch.setenv("TMUX_PANE", "175")
    assert _detect_nested_parent("new-sid", cli_pid=9999) is None


def test_detect_nested_parent_rejects_recycled_pid_ancestor(fresh, monkeypatch):
    """A stale record whose dead session's pid was recycled by a NON-session
    process (e.g. tmux itself) must not nest-flag every new worker."""
    _write_parent_record(pid=4242)
    monkeypatch.setattr("dockwright.hooks._ancestor_pids", lambda pid: {4242})
    monkeypatch.setattr("dockwright.hooks._pid_looks_like_session",
                        lambda pid: False)
    assert _detect_nested_parent("child-sid", cli_pid=9999) is None


# --- Boot-lite event half: orphan flag on manager session_end -----------------

LIVE_PID = 111
DEAD_PID = 222


@pytest.fixture
def orphan_env(fresh, monkeypatch):
    """Manager-agent session_end environment with deterministic pid liveness
    and a subprocess recorder (conftest's terminal guard is overridden here so
    osascript calls are captured instead of executed)."""
    from dockwright import state as state_mod
    monkeypatch.setattr(paths, "ORPHANS", fresh / "orphans")
    monkeypatch.setattr(state_mod, "_pid_alive", lambda pid: pid == LIVE_PID)
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    calls = []

    def fake_run(args, *pargs, **kwargs):
        calls.append(args)
        output = "" if kwargs.get("text") else b""
        import subprocess as sp
        return sp.CompletedProcess(args, returncode=0, stdout=output, stderr=output)

    import subprocess as sp
    monkeypatch.setattr(sp, "run", fake_run)
    return calls


def _write_manager(name="grumpy-yak", sid="mgr-1"):
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {
        "claude_sid": sid, "agent": "manager", "name": name, "cwd": "/x",
        "iterm_sid": "i9", "pid": 1, "started_at": 0, "domain": "general",
    })


def _write_worker(sid, parent, pid=LIVE_PID, **extra):
    record = {
        "claude_sid": sid, "agent": "worker", "name": f"task-{sid}",
        "funny_name": f"funny-{sid}", "cwd": "/x", "window_id": f"w-{sid}",
        "pid": pid, "started_at": 0, "state": "processing",
        "parent_manager_name": parent,
    }
    record.update(extra)
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", record)


def _end_session(monkeypatch, sid="mgr-1", reason="other"):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": sid, "reason": reason})))
    session_end()


def test_session_end_manager_with_live_workers_writes_orphan_flag(orphan_env, monkeypatch):
    # Assert the notification at the _notify_macos seam: the helper itself
    # no-ops under pytest (PYTEST_CURRENT_TEST guard), so the osascript argv
    # never reaches subprocess.run in tests.
    from dockwright import hooks
    notifications = []
    monkeypatch.setattr(hooks, "_notify_macos", notifications.append)
    _write_manager()
    _write_worker("w1", "grumpy-yak")
    _write_worker("w2", "other-manager")            # other parent — not ours
    _write_worker("w3", "grumpy-yak", pid=DEAD_PID)  # dead pid — not orphaned
    _end_session(monkeypatch)
    flag = state.read_json(paths.ORPHANS / "grumpy-yak.json")
    assert flag is not None
    assert flag["manager_name"] == "grumpy-yak"
    assert flag["manager_sid"] == "mgr-1"
    assert flag["source"] == "session_end"
    assert flag["reason"] == "other"
    assert isinstance(flag["orphaned_at"], float)
    assert [w["claude_sid"] for w in flag["workers"]] == ["w1"]
    worker = flag["workers"][0]
    assert worker["name"] == "task-w1"
    assert worker["funny_name"] == "funny-w1"
    assert worker["pid"] == LIVE_PID
    assert worker["window_id"] == "w-w1"
    assert worker["state"] == "processing"
    assert len(notifications) == 1
    assert "grumpy-yak" in notifications[0]


def test_orphan_flag_window_id_supports_legacy_iterm_sid(orphan_env, monkeypatch):
    _write_manager()
    record = {
        "claude_sid": "w1", "agent": "worker", "name": "task-w1", "cwd": "/x",
        "iterm_sid": "legacy-7", "pid": LIVE_PID, "started_at": 0,
        "parent_manager_name": "grumpy-yak",
    }
    state.write_json_atomic(paths.ACTIVE / "w1.json", record)
    _end_session(monkeypatch)
    flag = state.read_json(paths.ORPHANS / "grumpy-yak.json")
    assert flag["workers"][0]["window_id"] == "legacy-7"


def test_session_end_manager_without_live_workers_writes_no_flag(orphan_env, monkeypatch):
    _write_manager()
    _write_worker("w2", "other-manager")
    _write_worker("w3", "grumpy-yak", pid=DEAD_PID)
    _end_session(monkeypatch)
    assert not (paths.ORPHANS / "grumpy-yak.json").exists()
    assert [c for c in orphan_env if c and c[0] == "osascript"] == []


def test_session_end_malformed_pid_record_does_not_abort_scan(orphan_env, monkeypatch):
    _write_manager()
    _write_worker("w0", "grumpy-yak", pid=None)      # malformed — must be skipped
    _write_worker("w1", "grumpy-yak")
    _end_session(monkeypatch)
    flag = state.read_json(paths.ORPHANS / "grumpy-yak.json")
    assert [w["claude_sid"] for w in flag["workers"]] == ["w1"]


def test_worker_session_end_writes_no_orphan_flag(orphan_env, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    _write_worker("w1", "grumpy-yak")
    _write_worker("w2", "grumpy-yak")
    _end_session(monkeypatch, sid="w1")
    assert not paths.ORPHANS.exists() or list(paths.ORPHANS.iterdir()) == []


def test_session_end_no_record_writes_no_orphan_flag(orphan_env, monkeypatch):
    _write_worker("w1", "grumpy-yak")
    _end_session(monkeypatch, sid="mgr-gone")        # no active record for this sid
    assert not paths.ORPHANS.exists() or list(paths.ORPHANS.iterdir()) == []


def test_orphan_flag_lands_even_if_distill_raises(orphan_env, monkeypatch):
    import dockwright.hooks as hooks_mod
    def boom(sid, record):
        raise RuntimeError("distill exploded")
    monkeypatch.setattr(hooks_mod, "_maybe_distill_on_session_end", boom)
    _write_manager()
    _write_worker("w1", "grumpy-yak")
    _end_session(monkeypatch)                        # must not raise
    assert (paths.ORPHANS / "grumpy-yak.json").exists()


def test_nested_worker_not_counted_in_orphan_flag(orphan_env, monkeypatch):
    """Nested sub-sessions inherit CLAUDE_PARENT_MANAGER — they must not read
    as the dying manager's workers (they die with their parent process)."""
    _write_manager()
    _write_worker("ghost", "grumpy-yak", nested=True)
    _end_session(monkeypatch)
    assert not (paths.ORPHANS / "grumpy-yak.json").exists()


def test_nested_manager_ghost_session_end_does_not_flag(orphan_env, monkeypatch):
    state.write_json_atomic(paths.ACTIVE / "mgr-1.json", {
        "claude_sid": "mgr-1", "agent": "manager", "name": "nested-mgr1", "cwd": "/x",
        "pid": 1, "started_at": 0, "nested": True,
    })
    _write_worker("w1", "nested-mgr1")
    _end_session(monkeypatch)
    assert not paths.ORPHANS.exists() or list(paths.ORPHANS.iterdir()) == []


def _spend_dict(out=500):
    return {"turns": 2, "out_tokens": out, "in_tokens": 10,
            "cache_read_tokens": 100, "last_turn_out": out, "last_msg_id": "m"}


def _ledger_entries(fresh):
    path = fresh / "spend-ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_session_end_ledgers_worker_spend_and_archives(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha",
        "cwd": "/x", "pid": os.getpid(), "started_at": 1.0,
        "spend": _spend_dict(),
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    session_end()
    entries = _ledger_entries(fresh)
    assert len(entries) == 1
    assert entries[0]["sid"] == "s1"
    assert entries[0]["source"] == "session_end"
    # closed/ archive still carries spend too (report dedups by sid).
    closed = state.read_json(fresh / "closed" / "s1.json")
    assert closed["spend"]["out_tokens"] == 500


def test_session_end_ledgers_manager_and_nested_spend(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    state.write_json_atomic(fresh / "active" / "m1.json", {
        "claude_sid": "m1", "agent": "manager", "name": "mgr",
        "cwd": "/x", "pid": os.getpid(), "started_at": 1.0,
        "spend": _spend_dict(out=900),
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "m1"})))
    session_end()
    state.write_json_atomic(fresh / "active" / "n1.json", {
        "claude_sid": "n1", "agent": "manager", "name": "nested-n1",
        "nested": True, "cwd": "/x", "pid": os.getpid(), "started_at": 1.0,
        "spend": _spend_dict(out=70),
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "n1"})))
    session_end()
    by_sid = {e["sid"]: e for e in _ledger_entries(fresh)}
    assert by_sid["m1"]["agent"] == "manager"
    assert by_sid["n1"]["agent"] == "nested"
    # neither got a closed/ archive (pre-existing behavior, unchanged)
    assert not (fresh / "closed" / "m1.json").exists()
    assert not (fresh / "closed" / "n1.json").exists()


def test_session_end_no_spend_no_ledger_line(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha",
        "cwd": "/x", "pid": os.getpid(), "started_at": 1.0,
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    session_end()
    assert _ledger_entries(fresh) == []


def test_clear_rotation_ledgers_old_records_spend(fresh, monkeypatch):
    """/clear pops the inherited copy's spend (hooks re-register under a new
    sid) — the popped period must land in the ledger, not vanish."""
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_PARENT_PID", str(os.getpid()))
    monkeypatch.delenv("CLAUDE_WORKER_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_PARENT_MANAGER", raising=False)
    state.write_json_atomic(fresh / "active" / "old-sid.json", {
        "claude_sid": "old-sid", "agent": "worker", "name": "alpha",
        "cwd": "/x", "pid": os.getpid(), "started_at": 1.0,
        "spend": _spend_dict(out=333),
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "new-sid", "cwd": "/x"})))
    session_start()
    entries = _ledger_entries(fresh)
    assert len(entries) == 1
    assert entries[0]["sid"] == "old-sid"
    assert entries[0]["source"] == "rotation"
    assert entries[0]["spend"]["out_tokens"] == 333
    # rotation behavior itself unchanged: new record exists, spend reset
    new = state.read_json(fresh / "active" / "new-sid.json")
    assert new["name"] == "alpha"
    assert "spend" not in new


def test_session_end_captures_tagged_headless_spend(fresh, monkeypatch, tmp_path):
    """CLAUDE_SPEND_CLASS contract: env-stripped headless runs (distill, later
    selffix/jira-update) tag themselves; SessionEnd lands whole-transcript
    spend in the ledger — their only capture."""
    monkeypatch.delenv("CLAUDE_AGENT", raising=False)
    monkeypatch.setenv("CLAUDE_SPEND_CLASS", "distill")
    transcript = tmp_path / "headless.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"id": "m1", "usage": {
            "output_tokens": 42, "input_tokens": 3,
            "cache_read_input_tokens": 7, "cache_creation_input_tokens": 1}},
    }) + "\n")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "session_id": "h1", "transcript_path": str(transcript)})))
    session_end()
    entries = _ledger_entries(fresh)
    assert len(entries) == 1
    assert entries[0]["sid"] == "h1"
    assert entries[0]["name"] == "distill"
    assert entries[0]["agent"] == "headless"
    assert entries[0]["source"] == "headless"
    assert entries[0]["spend"]["out_tokens"] == 42
    assert entries[0]["spend"]["cache_creation_tokens"] == 1


def test_session_end_untagged_non_orchestrator_session_is_untouched(fresh, monkeypatch):
    monkeypatch.delenv("CLAUDE_AGENT", raising=False)
    monkeypatch.delenv("CLAUDE_SPEND_CLASS", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "u1"})))
    session_end()
    assert _ledger_entries(fresh) == []


def test_session_end_orchestrator_session_ignores_leaked_spend_class(fresh, monkeypatch):
    """A leaked CLAUDE_SPEND_CLASS on a real worker must not double-capture —
    the worker's spend already flows through the drop path."""
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_SPEND_CLASS", "distill")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha",
        "cwd": "/x", "pid": os.getpid(), "started_at": 1.0,
        "spend": _spend_dict(),
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    session_end()
    entries = _ledger_entries(fresh)
    assert [e["source"] for e in entries] == ["session_end"]


def test_distill_session_with_sentinel_and_spend_class_captures_headless(fresh, monkeypatch, tmp_path):
    """The real distill child shape: sentinel set (hooks inert) + leaked
    CLAUDE_AGENT=manager + CLAUDE_SPEND_CLASS=distill → headless capture, no
    registration, no distill fan-out."""
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setenv(paths.DISTILL_ENV_SENTINEL, "1")
    monkeypatch.setenv("CLAUDE_SPEND_CLASS", "distill")
    transcript = tmp_path / "d.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"id": "m1", "usage": {"output_tokens": 5, "input_tokens": 1,
                    "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
    }) + "\n")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "session_id": "d1", "transcript_path": str(transcript)})))
    session_end()
    entries = _ledger_entries(fresh)
    assert len(entries) == 1
    assert entries[0]["name"] == "distill"


def test_distill_child_env_is_tagged_for_spend_capture():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1]
              / "src" / "dockwright" / "distill.py").read_text()
    assert 'distill_env["CLAUDE_SPEND_CLASS"] = "distill"' in source


def _make_delegating_tree(home, sid, *, agent_age_sec=5):
    project_dir = home / ".claude" / "projects" / "-Users-test"
    project_dir.mkdir(parents=True, exist_ok=True)
    log = project_dir / f"{sid}.jsonl"
    log.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "dispatched the verifier"}]},
        "timestamp": "2026-06-13T00:00:00Z"}) + "\n")
    now = time.time()
    os.utime(log, (now - 60, now - 60))
    subagents = project_dir / sid / "subagents"
    subagents.mkdir(parents=True, exist_ok=True)
    agent = subagents / "agent-aaa.jsonl"
    agent.write_text("{}")
    os.utime(agent, (now - agent_age_sec, now - agent_age_sec))
    return log


def test_stop_hook_stamps_transcript_path(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("HOME", str(fresh))
    log = _make_delegating_tree(fresh, "s1")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "window_id": "42", "pid": 1, "started_at": 0, "state": "processing",
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    stop_hook()
    record = state.read_json(fresh / "active" / "s1.json")
    assert record["transcript_path"] == str(log)
    assert record["state"] == "idle"          # state stays turn-truth


def test_stop_hook_paints_busy_while_delegating(fresh, monkeypatch):
    _reset_driver(monkeypatch)
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("TMUX_PANE", "42")
    monkeypatch.setenv("HOME", str(fresh))
    _make_delegating_tree(fresh, "s1")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "window_id": "42", "pid": 1, "started_at": 0, "state": "processing",
    })
    calls = []
    monkeypatch.setattr(
        "dockwright.hooks.subprocess.run",
        lambda a, **kw: calls.append(a) or type("R", (), {"returncode": 0})(),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    stop_hook()
    color_calls = [a for a in calls if "set-window-option" in a]
    assert len(color_calls) == 2
    opts = _color_args(calls)
    assert "bg=#aa8800,fg=#ffffff" in opts["window-status-current-style"]  # BUSY, not idle grey


def test_stop_hook_question_red_beats_delegating_busy(fresh, monkeypatch):
    _reset_driver(monkeypatch)
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("TMUX_PANE", "42")
    monkeypatch.setenv("HOME", str(fresh))
    _make_delegating_tree(fresh, "s1")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "window_id": "42", "pid": 1, "started_at": 0, "state": "processing",
    })
    state.write_json_atomic(fresh / "questions" / "q1.json", {
        "question_id": "q1", "worker_sid": "s1", "worker_name": "alpha",
        "question": "?", "asked_at": 0,
    })
    calls = []
    monkeypatch.setattr(
        "dockwright.hooks.subprocess.run",
        lambda a, **kw: calls.append(a) or type("R", (), {"returncode": 0})(),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    stop_hook()
    color_calls = [a for a in calls if "set-window-option" in a]
    assert len(color_calls) == 2
    opts = _color_args(calls)
    assert "bg=#aa3300,fg=#ffffff" in opts["window-status-current-style"]  # question red wins


def test_session_end_manager_spawns_detached_fallback_distill(fresh, monkeypatch):
    # The SessionEnd hook budget is 5s (settings.snippet.json) but the distill's
    # `claude -p` takes 10-30s: it must be spawned DETACHED
    # (start_new_session=True), never run in-process (orch-audit finding 4 —
    # the cmd+w close path lost the manager memory file).
    state.write_json_atomic(paths.ACTIVE / "m1.json", {
        "claude_sid": "m1", "agent": "manager", "name": "mgr", "cwd": "/x",
        "window_id": "", "pid": 1, "state": "idle", "domain": "general",
    })
    monkeypatch.setattr(paths, "MANAGER_MEMORY", fresh / "manager-memory")
    popens = []
    monkeypatch.setattr(
        "dockwright.hooks.subprocess.Popen",
        lambda *a, **kw: popens.append((a, kw)) or type("P", (), {"pid": 4242})(),
    )
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "m1"})))
    session_end()
    assert len(popens) == 1, "session_end must spawn exactly one fallback distill"
    (cmd,), kw = popens[0]
    assert cmd[:3] == [sys.executable, "-m", "dockwright"]
    assert cmd[3:] == ["distill", "m1", "--domain", "general"]
    assert kw["start_new_session"] is True
    # Stdio contract: DEVNULL keeps the detached child off the dead hook's tty;
    # the log redirection is the only diagnosability the unobserved child has.
    assert kw["stdin"] is subprocess.DEVNULL
    assert kw["stdout"].name.endswith("distill-fallback.log")
    assert kw["stderr"] is kw["stdout"]


def test_session_end_manager_skips_distill_when_memory_file_exists(fresh, monkeypatch):
    # Idempotence: /manager-close already distilled -> no spawn.
    state.write_json_atomic(paths.ACTIVE / "m1.json", {
        "claude_sid": "m1", "agent": "manager", "name": "mgr", "cwd": "/x",
        "window_id": "", "pid": 1, "state": "idle", "domain": "general",
    })
    monkeypatch.setattr(paths, "MANAGER_MEMORY", fresh / "manager-memory")
    from datetime import datetime
    memory_dir = paths.manager_memory_domain_dir("general")
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / f"{datetime.now().strftime('%Y-%m-%d')}-m1.md").write_text("already distilled")
    popens = []
    monkeypatch.setattr(
        "dockwright.hooks.subprocess.Popen",
        lambda *a, **kw: popens.append((a, kw)),
    )
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "m1"})))
    session_end()
    assert popens == []


# ---- agent-team (--agent-id) subagent detection ----------------------------
# Claude Code's agent-teams feature launches each subagent as its OWN tmux
# session (`claude --agent-id <name>@<team> --parent-session-id <sid> ...`),
# a child of the tmux SERVER — the pid-ancestry walk above never matches, so
# without dedicated detection these register as phantom managers off the
# polluted tmux global env (CLAUDE_AGENT=manager, CLAUDE_WORKER_NAME=manager).

import sys as _sys

from dockwright.hooks import _proc_argv


def test_proc_argv_reads_own_process_real_syscall():
    """REAL un-mocked read of the current process — proves the platform lane
    (KERN_PROCARGS2 on darwin) parses without faulting on a live argv."""
    argv = _proc_argv(os.getpid())
    assert isinstance(argv, list) and len(argv) >= 1
    joined = " ".join(argv)
    assert "python" in joined or "pytest" in joined


def test_proc_argv_dead_pid_returns_none():
    # 999999 exceeds macOS's pid range and no /proc entry exists on Linux —
    # both lanes must degrade to None, never raise.
    assert _proc_argv(999999) is None


@pytest.mark.skipif(_sys.platform != "darwin", reason="KERN_PROCARGS2 denies non-owner reads; /proc does not")
def test_proc_argv_foreign_pid_returns_none():
    assert _proc_argv(1) is None


from dockwright.hooks import _detect_agent_team_parent

TEAMMATE_ARGV = [
    "/Users/u/.local/share/claude/versions/2.1.207",
    "--agent-id", "implementer@session-abcd1234",
    "--agent-name", "implementer",
    "--team-name", "session-abcd1234",
    "--agent-color", "blue",
    "--parent-session-id", "parent-sid",
    "--agent-type", "general-purpose",
    "--permission-mode", "auto",
]


def test_detect_teammate_via_argv(fresh, monkeypatch):
    _write_parent_record(sid="parent-sid", name="parent-worker", pid=4242)
    monkeypatch.setattr("dockwright.hooks._proc_argv", lambda pid: list(TEAMMATE_ARGV))
    got = _detect_agent_team_parent({}, cli_pid=777)
    assert got == {"sid": "parent-sid", "name": "parent-worker",
                   "agent_id": "implementer@session-abcd1234"}


def test_detect_teammate_via_payload_when_argv_unreadable(fresh, monkeypatch):
    """Payload lane alone must still detect — muting the phantom matters
    more than attributing it."""
    monkeypatch.setattr("dockwright.hooks._proc_argv", lambda pid: None)
    got = _detect_agent_team_parent({"agent_type": "Explore"}, cli_pid=777)
    assert got == {"sid": None, "name": None, "agent_id": None}


def test_detect_teammate_parent_record_missing(fresh, monkeypatch):
    """Teammate of an unregistered parent: sid from argv, name unresolvable."""
    monkeypatch.setattr("dockwright.hooks._proc_argv", lambda pid: list(TEAMMATE_ARGV))
    got = _detect_agent_team_parent({}, cli_pid=777)
    assert got["sid"] == "parent-sid"
    assert got["name"] is None


def test_detect_teammate_flag_without_value(fresh, monkeypatch):
    monkeypatch.setattr("dockwright.hooks._proc_argv",
                        lambda pid: ["claude-bin", "--agent-id"])
    got = _detect_agent_team_parent({}, cli_pid=777)
    assert got == {"sid": None, "name": None, "agent_id": None}


def test_detect_teammate_prompt_text_is_not_a_flag(fresh, monkeypatch):
    """THE false-positive guard: a real worker whose PROMPT contains the
    literal text '--agent-id' / '--parent-session-id' (this fix's own task
    brief did) must NOT read as a teammate — that would mute a real worker."""
    prompt = ("fix bug: SDD --agent-id subagents misregister as managers; "
              "argv carries --parent-session-id 1391778a-cdd0 for attribution")
    monkeypatch.setattr("dockwright.hooks._proc_argv",
                        lambda pid: ["claude", "--model", "opus[1m]", prompt])
    assert _detect_agent_team_parent({}, cli_pid=777) is None


def test_detect_teammate_plain_sessions_return_none(fresh, monkeypatch):
    monkeypatch.setattr("dockwright.hooks._proc_argv",
                        lambda pid: ["claude", "--model", "opus[1m]", "/manager-resume abc"])
    assert _detect_agent_team_parent({}, cli_pid=777) is None


def test_detect_teammate_never_raises(fresh, monkeypatch):
    def boom(pid):
        raise RuntimeError("sysctl exploded")
    monkeypatch.setattr("dockwright.hooks._proc_argv", boom)
    assert _detect_agent_team_parent({}, cli_pid=777) is None


def test_detect_teammate_payload_lane_survives_argv_exception(fresh, monkeypatch):
    def boom(pid):
        raise RuntimeError("sysctl exploded")
    monkeypatch.setattr("dockwright.hooks._proc_argv", boom)
    got = _detect_agent_team_parent({"agent_type": "Explore"}, cli_pid=777)
    assert got == {"sid": None, "name": None, "agent_id": None}


def _teammate_polluted_env(monkeypatch):
    """The exact env an agent-team subagent inherits from the tmux server's
    global environment (captured live 2026-07-13) — the same values that
    made phantoms register as literal "manager"/"manager-2"."""
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "manager")
    monkeypatch.delenv("CLAUDE_PARENT_MANAGER", raising=False)
    monkeypatch.setenv("CLAUDE_PARENT_PID", "777")


def test_session_start_registers_teammate_as_nested(fresh, monkeypatch):
    _write_parent_record(sid="parent-sid", name="parent-worker", pid=4242)
    monkeypatch.setattr("dockwright.hooks._proc_argv", lambda pid: list(TEAMMATE_ARGV))

    def ancestry_must_not_run(sid, cli_pid):
        raise AssertionError("teammate detection must not consult ancestry")
    monkeypatch.setattr("dockwright.hooks._detect_nested_parent", ancestry_must_not_run)
    _teammate_polluted_env(monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "eeee1111-rest-of-sid", "cwd": "/x",
         "agent_type": "general-purpose"})))
    session_start()
    record = state.read_json(paths.ACTIVE / "eeee1111-rest-of-sid.json")
    assert record["nested"] is True
    assert record["name"] == "nested-eeee1111"          # NOT "manager"/"manager-2"
    assert record["agent_id"] == "implementer@session-abcd1234"
    assert record["nested_parent_sid"] == "parent-sid"
    assert record["nested_parent_name"] == "parent-worker"
    assert record["funny_name"] is None
    assert record["window_id"] == ""
    # excluded from the manager registry surface (list_managers filter shape)
    assert record.get("agent") != "manager" or record.get("nested")


def test_session_start_teammate_payload_only_still_nested(fresh, monkeypatch):
    """argv unreadable: the payload lane alone must still mute the session."""
    monkeypatch.setattr("dockwright.hooks._proc_argv", lambda pid: None)
    _teammate_polluted_env(monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "ffff2222-rest", "cwd": "/x", "agent_type": "Explore"})))
    session_start()
    record = state.read_json(paths.ACTIVE / "ffff2222-rest.json")
    assert record["nested"] is True
    assert record["agent_id"] is None
    assert record["nested_parent_sid"] is None
    assert record["nested_parent_name"] is None


def test_session_start_worker_prompt_with_agentid_text_stays_worker(fresh, monkeypatch):
    """4-case matrix, real worker + the killer false-positive guard: a
    spawned worker whose PROMPT argv element contains '--agent-id' text must
    register as a normal worker."""
    prompt = ("fix a recurring bug: SDD --agent-id subagents get misregistered; "
              "key on --parent-session-id for attribution")
    monkeypatch.setattr("dockwright.hooks._proc_argv",
                        lambda pid: ["claude", "--settings", "{}", "--model",
                                     "claude-fable-5[1m]", prompt])
    monkeypatch.setattr("dockwright.hooks._ancestor_pids", lambda pid: set())
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "fix-agentid-manager-reg")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "s9", "cwd": "/x"})))
    session_start()
    record = state.read_json(paths.ACTIVE / "s9.json")
    assert not record.get("nested")
    assert record["agent"] == "worker"
    assert record["name"] == "fix-agentid-manager-reg"


def test_session_start_manager_without_agentid_stays_manager(fresh, monkeypatch):
    """4-case matrix, real manager: no payload agent_type, no --agent-id argv."""
    monkeypatch.setattr("dockwright.hooks._proc_argv",
                        lambda pid: ["claude", "--model", "opus[1m]",
                                     "/manager-resume 1cfa898f"])
    monkeypatch.setattr("dockwright.hooks._ancestor_pids", lambda pid: set())
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.delenv("CLAUDE_WORKER_NAME", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "mgr-9", "cwd": "/x"})))
    session_start()
    record = state.read_json(paths.ACTIVE / "mgr-9.json")
    assert record["agent"] == "manager"
    assert not record.get("nested")


def test_session_start_claude_p_ancestry_lane_still_fires(fresh, monkeypatch):
    """4-case matrix, classic `claude -p` nested child: teammate detector
    returns None (no agent_type, no --agent-id), ancestry lane still nests it;
    its record carries agent_id None."""
    _write_parent_record(pid=4242)
    monkeypatch.setattr("dockwright.hooks._proc_argv",
                        lambda pid: ["claude", "-p", "distill this transcript"])
    monkeypatch.setattr("dockwright.hooks._ancestor_pids", lambda pid: {4242})
    monkeypatch.setattr("dockwright.hooks._pid_looks_like_session", lambda pid: True)
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "parent-worker")
    monkeypatch.setenv("CLAUDE_PARENT_PID", "9999")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "abab3333-rest", "cwd": "/x"})))
    session_start()
    record = state.read_json(paths.ACTIVE / "abab3333-rest.json")
    assert record["nested"] is True
    assert record["nested_parent_sid"] == "parent-sid"
    assert record["agent_id"] is None
