import io
import json
import os
import shutil
import subprocess
import sys
import time
import pytest
from dockwright import config, hooks, paths, state
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
    monkeypatch.setattr(paths, "HANDOFFS", tmp_path / "handoffs")
    monkeypatch.setattr(paths, "TURN_ENDS", tmp_path / "turn-ends")
    monkeypatch.setattr(paths, "ARTIFACTS", tmp_path / "artifacts")
    monkeypatch.setattr(paths, "ASSIGNMENTS", tmp_path / "assignments")
    monkeypatch.setattr(paths, "ASSIGNMENTS_PENDING", tmp_path / "assignments" / ".pending")
    monkeypatch.setattr(paths, "SPEND_LEDGER", tmp_path / "spend-ledger.jsonl")
    monkeypatch.delenv("CLAUDE_ASSIGNMENT_ID", raising=False)
    paths.ensure_dirs()
    yield tmp_path


def _install_two_pool(monkeypatch, tmp_path):
    cfg = tmp_path / "two-pool.toml"
    cfg.write_text('[accounts]\ndefault = "a"\n'
                   '[[accounts.pool]]\nname = "a"\n[[accounts.pool]]\nname = "b"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))

@pytest.fixture(autouse=True)
def _no_process_tree_probes(monkeypatch):
    from dockwright import identity
    monkeypatch.setattr(identity, "_ppid_of", lambda pid: None)
    monkeypatch.setattr(hooks, "_pid_looks_like_session", lambda pid: False)

def test_session_start_skips_when_no_env(fresh, monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_AGENT", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    assert list((fresh / "active").iterdir()) == []

def test_session_start_skips_distill_child(fresh, monkeypatch):
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
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "fix-the-thing")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "s1.json")
    assert record["name"] == "fix-the-thing"
    assert record["funny_name"]
    assert record["funny_name"] != record["name"]
    assert "-" in record["funny_name"]

def test_session_start_manager_has_no_funny_name(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-1", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "mgr-1.json")
    assert record["funny_name"] is None
    assert record["runtime"] == "claude"

def test_session_start_pins_manager_runtime_to_claude(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setenv("CLAUDE_MANAGER_RUNTIME", "codex")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-1", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "mgr-1.json")
    assert record["agent"] == "manager"
    assert record["runtime"] == "claude"

def test_session_start_worker_funny_name_avoids_collision(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "task-b")
    state.write_json_atomic(fresh / "active" / "other.json", {
        "claude_sid": "other", "agent": "worker", "name": "task-a", "funny_name": "grumpy-yak",
        "cwd": "/x", "iterm_sid": "i0", "pid": os.getpid(), "started_at": 0,
    })
    import dockwright.names as names
    seq = iter(["grumpy-yak", "snarky-otter"])
    monkeypatch.setattr(names, "_roll", lambda nouns, rng: next(seq))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "s1.json")
    assert record["funny_name"] == "snarky-otter"

def test_session_start_falls_back_to_pane_id(fresh, monkeypatch):
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
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.setenv("CLAUDE_ITERM_SID", "explicit-99")
    monkeypatch.setenv("TMUX_PANE", "42")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "s1.json")
    assert record["window_id"] == "explicit-99"

def test_session_start_refire_preserves_manager_name_and_state(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.delenv("CLAUDE_WORKER_NAME", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-1", "cwd": "/x"})))
    session_start()
    first = state.read_json(fresh / "active" / "mgr-1.json")
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
    assert second["cwd"] == "/y"

def test_session_start_refire_preserves_worker_funny_name_and_state(fresh, monkeypatch):
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
    _install_two_pool(monkeypatch, fresh)
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "task-acct")
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT", "b")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s-acct1", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "s-acct1.json")
    assert record["account"] == "b"


def test_session_start_account_none_without_env(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "task-no-acct")
    monkeypatch.delenv("CLAUDE_ORCH_ACCOUNT", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s-acct2", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "s-acct2.json")
    assert record["account"] is None


def test_session_start_account_invalid_env_stamps_none(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "task-bad-acct")
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT", "xyz")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s-acct4", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "s-acct4.json")
    assert record["account"] is None


def test_session_start_resume_refreshes_account_only_when_env_present(fresh, monkeypatch):
    _install_two_pool(monkeypatch, fresh)
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "task-resume-acct")
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
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT", "b")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s-acct3", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "s-acct3.json")
    assert record["account"] == "b"
    monkeypatch.delenv("CLAUDE_ORCH_ACCOUNT", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s-acct3", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "s-acct3.json")
    assert record["account"] == "b"


def test_user_prompt_submit_marks_state_processing(fresh, monkeypatch):
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
    monkeypatch.setenv("HOME", str(fresh))
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 0,
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    session_end()
    assert not (fresh / "active" / "s1.json").exists()

def test_session_end_removes_manager_active_record(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    state.write_json_atomic(fresh / "active" / "mgr-1.json", {
        "claude_sid": "mgr-1", "agent": "manager", "name": "grumpy-yak", "cwd": "/x",
        "iterm_sid": "i9", "pid": 1, "started_at": 0, "domain": "general",
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-1"})))
    session_end()
    assert not (fresh / "active" / "mgr-1.json").exists()

def test_session_end_archives_worker_to_closed(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("HOME", str(fresh))
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
    assert not (fresh / "active" / "s1.json").exists()


def test_session_end_copies_spend_into_closed_record(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("HOME", str(fresh))
    spend = {"turns": 3, "out_tokens": 1200, "in_tokens": 4500,
             "cache_read_tokens": 9000, "last_turn_out": 400, "last_msg_id": "msg_3"}
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 12345.0, "spend": spend,
        "transcript_path": "/tmp/somewhere/s1.jsonl",
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    session_end()
    closed = state.read_json(fresh / "closed" / "s1.json")
    assert closed["spend"] == spend
    assert closed["transcript_path"] == "/tmp/somewhere/s1.jsonl"


def test_session_end_resolves_transcript_path_when_never_cached(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("HOME", str(fresh))
    project_dir = fresh / ".claude" / "projects" / "-Users-x"
    project_dir.mkdir(parents=True)
    log = project_dir / "s1.jsonl"
    log.write_text("")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "pid": 1, "started_at": 0, "runtime": "claude",
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    session_end()
    closed = state.read_json(fresh / "closed" / "s1.json")
    assert closed["transcript_path"] == str(log)


def test_session_end_copies_account_into_closed_record(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("HOME", str(fresh))
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 12345.0, "account": "b",
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    session_end()
    closed = state.read_json(fresh / "closed" / "s1.json")
    assert closed["account"] == "b"


def test_session_end_account_null_when_absent(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("HOME", str(fresh))
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
    monkeypatch.setenv("HOME", str(fresh))
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
    monkeypatch.setenv("HOME", str(fresh))
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
    assert all(a[:3] == ["tmux", "-L", "S"] for a in calls)
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
    opts = {}
    for a in calls:
        if "set-window-option" in a:
            opt = a[a.index("set-window-option") + 3]
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
    assert len(color_calls) == 2
    opts = _color_args(calls)
    assert "bg=#444444,fg=#ffffff" in opts["window-status-current-style"]
    assert "bg=#222222,fg=#ffffff" in opts["window-status-style"]
    assert all(a[a.index("-t") + 1] == "42" for a in color_calls)
    title_calls = [a for a in calls if "rename-window" in a]
    assert len(title_calls) == 1
    assert any("alpha" in arg for arg in title_calls[0])
    assert not any("🔧" in arg for arg in title_calls[0])
    assert title_calls[0][title_calls[0].index("-t") + 1] == "42"


def test_session_start_worker_with_pending_question_paints_question_color(fresh, monkeypatch):
    _reset_driver(monkeypatch)
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.setenv("TMUX_PANE", "42")
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
    session_start()
    assert (fresh / "active" / "mgr-1.json").exists()

def test_session_start_dedupes_name_with_suffix(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    state.write_json_atomic(fresh / "active" / "other.json", {
        "claude_sid": "other", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i0", "pid": os.getpid(), "started_at": 0,
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "s1.json")
    assert record["name"] == "alpha-2"

def test_stop_hook_writes_turn_end_marker(fresh, monkeypatch):
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
    assert turn_ends[0].name.startswith("s1-")
    assert turn_ends[0].name.endswith(".json")
    assert turn_ends[0].parent.name == paths.UNSCOPED_BUCKET


def test_stop_hook_scopes_turn_end_to_parent_manager(fresh, monkeypatch):
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
    assert list((fresh / "turn-ends" / paths.UNSCOPED_BUCKET).glob("*.json")) == []


def test_stop_hook_turn_end_marker_includes_runtime(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    monkeypatch.setattr(
        "dockwright.hooks.subprocess.run",
        lambda a, **kw: type("R", (), {"returncode": 0})(),
    )
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "pid": 1, "started_at": 0, "runtime": "codex",
        "last_summary": "did stuff", "last_turn_at": "2026-05-19T00:00:00Z",
    })
    stop_hook()
    marker = state.read_json(list((fresh / "turn-ends").rglob("*.json"))[0])
    assert marker["runtime"] == "codex"

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
    assert list((fresh / "turn-ends" / paths.UNSCOPED_BUCKET).glob("*.json")) == []


def test_stop_hook_preserves_old_summary_on_empty_transcript(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 0,
        "last_summary": "previously seen", "last_turn_at": "2026-01-01T00:00:00Z",
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    stop_hook()
    record = state.read_json(fresh / "active" / "s1.json")
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
    assert len(calls) == 2
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
    monkeypatch.setenv("TMUX_PANE", "42")
    calls = _capture_tab_calls(monkeypatch)
    setter()
    assert calls
    assert all("-t" in c and c[c.index("-t") + 1] == "42" for c in calls)


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
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "happy-otter")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-1", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "mgr-1.json")
    assert record["name"] == "happy-otter"


def test_session_start_two_managers_get_distinct_non_literal_names(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.delenv("CLAUDE_WORKER_NAME", raising=False)
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
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    invocation_log = tmp_path / "tmux-invocations.log"
    fake_tmux = bin_dir / "tmux"
    fake_tmux.write_text(f"#!/bin/sh\necho \"$@\" >> '{invocation_log}'\n")
    fake_tmux.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("TMUX_PANE", "132")
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-leak", "cwd": "/x"})))
    session_start()
    assert not invocation_log.exists(), (
        f"a live tmux invocation escaped the test sandbox:\n{invocation_log.read_text() if invocation_log.exists() else ''}"
    )


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
    state.write_json_atomic(paths.ACTIVE / "other.json", {
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
    assert (paths.ASSIGNMENTS_PENDING / "aid1.json").exists()
    assert not (paths.ASSIGNMENTS / "s1.json").exists()
    assert state.read_json(paths.ACTIVE / "s1.json")


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
    monkeypatch.setenv("HOME", str(fresh))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    session_end()
    assert (paths.ASSIGNMENTS / "s1.json").exists()
    assert (paths.CLOSED / "s1.json").exists()


def test_claim_never_raises_on_malformed_assignment_id(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.setenv("CLAUDE_ASSIGNMENT_ID", "   ")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    assert state.read_json(paths.ACTIVE / "s1.json")


def test_session_start_overrides_window_id_from_spawn_sidecar(fresh, monkeypatch):
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


def _spend_assistant_line_ts(msg_id, usage, timestamp):
    line = json.loads(_spend_assistant_line(msg_id, usage))
    line["timestamp"] = timestamp
    return json.dumps(line)


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
        _spend_assistant_line("msg_a", usage),
        _spend_assistant_line("msg_b", _spend_usage(output=50, input_tokens=1, cache_read=500)),
    ])
    _stop(monkeypatch)
    record = state.read_json(fresh / "active" / "s1.json")
    assert record["spend"]["turns"] == 1
    assert record["spend"]["out_tokens"] == 150
    assert record["spend"]["in_tokens"] == 4
    assert record["spend"]["cache_read_tokens"] == 1500
    assert record["spend"]["cache_creation_tokens"] == 0
    assert record["spend"]["last_turn_out"] == 150
    assert "last_msg_id" not in record["spend"]

    with log.open("a") as f:
        f.write(_spend_assistant_line("msg_c", _spend_usage(output=7, input_tokens=2)) + "\n")
    _stop(monkeypatch)
    record = state.read_json(fresh / "active" / "s1.json")
    assert record["spend"]["turns"] == 2
    assert record["spend"]["out_tokens"] == 157
    assert record["spend"]["last_turn_out"] == 7
    assert "last_msg_id" not in record["spend"]


def test_stop_hook_spend_reconciles_with_full_transcript_recount(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("HOME", str(fresh))
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "pid": 1, "started_at": 0, "runtime": "claude",
    })
    filler = json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": "x" * 4096}]}})

    def usage(out, inp, cr, cc):
        return {"input_tokens": inp, "cache_creation_input_tokens": cc,
                "cache_read_input_tokens": cr, "output_tokens": out,
                "service_tier": "standard"}

    turn_specs = [
        [("msg_t1a", usage(1000, 10, 100, 5)), ("msg_t1a", usage(1000, 10, 100, 5)),
         ("msg_t1b", usage(2000, 20, 200, 10))],
        [("msg_t2a", usage(400, 4, 40, 2))],
        [("msg_t3a", usage(30, 3, 300, 1))],
    ]
    expected_turn_out = [3000, 400, 30]
    lines = []
    for turn_index, spec in enumerate(turn_specs):
        for msg_id, u in spec:
            lines.extend([filler] * 25)
            lines.append(_spend_assistant_line(msg_id, u))
        _write_worker_transcript(fresh, "s1", lines)
        _stop(monkeypatch)
        record = state.read_json(fresh / "active" / "s1.json")
        assert record["spend"]["turns"] == turn_index + 1
        assert record["spend"]["last_turn_out"] == expected_turn_out[turn_index]
    assert state.read_json(fresh / "active" / "s1.json")["spend"] == {
        "turns": 3, "out_tokens": 3430, "in_tokens": 37,
        "cache_read_tokens": 640, "cache_creation_tokens": 18,
        "last_turn_out": 30,
        "by_model": {},
    }


def test_stop_hook_passes_started_at_to_birth_filter(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("HOME", str(fresh))
    born = 1785000000.0
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "pid": 1, "started_at": born, "runtime": "claude",
    })
    _write_worker_transcript(fresh, "s1", [
        _spend_assistant_line_ts("msg_replayed", _spend_usage(output=5000), "2026-07-20T00:00:00Z"),
        _spend_assistant_line_ts("msg_own", _spend_usage(output=70), "2026-07-26T00:00:00Z"),
    ])
    _stop(monkeypatch)
    record = state.read_json(fresh / "active" / "s1.json")
    assert record["spend"]["out_tokens"] == 70


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
    assert record["state"] == "idle"


def test_stop_hook_survives_spend_parser_raising(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("HOME", str(fresh))
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "pid": 1, "started_at": 0, "runtime": "claude",
    })
    _write_worker_transcript(fresh, "s1", [_spend_assistant_line("msg_a", _spend_usage(output=1))])
    monkeypatch.setattr(
        "dockwright.transcript.recount_spend",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    _stop(monkeypatch)
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
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "parent-worker")
    monkeypatch.setenv("CLAUDE_PARENT_MANAGER", "mgr")
    monkeypatch.setenv("CLAUDE_PARENT_PID", "9999")
    monkeypatch.setenv("CLAUDE_WORKER_RUNTIME", "codex")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "abcd1234-rest-of-sid", "cwd": "/x"})))
    session_start()
    record = state.read_json(paths.ACTIVE / "abcd1234-rest-of-sid.json")
    assert record["nested"] is True
    assert record["nested_parent_sid"] == "parent-sid"
    assert record["nested_parent_name"] == "parent-worker"
    assert record["name"] == "nested-abcd1234"
    assert record["funny_name"] is None
    assert record["window_id"] == ""
    assert record["parent_manager_name"] == "mgr"
    assert record["runtime"] == "claude"


def test_session_start_registers_nested_via_same_window_fallback(fresh, monkeypatch):
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
    _write_parent_record(pid=4242)
    calls = _capture_tab_calls(monkeypatch)
    monkeypatch.setattr("dockwright.hooks._ancestor_pids", lambda pid: {4242})
    monkeypatch.setattr("dockwright.hooks._pid_looks_like_session", lambda pid: True)
    monkeypatch.setattr("dockwright.registry._pid_alive", lambda pid: True)
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
    assert record["window_id"] == ""
    assert calls == []


def test_session_start_sid_rotation_supersedes_old_record_not_nested(fresh, monkeypatch):
    _reset_driver(monkeypatch)
    own_pid = os.getpid()
    _write_parent_record(sid="old-sid", name="alpha", pid=own_pid, window_id="175",
                         funny_name="grumpy-camel",
                         transcript_path="/x/.claude/projects/-x/old-sid.jsonl")
    monkeypatch.setattr("dockwright.hooks._ancestor_pids", lambda pid: set())
    monkeypatch.setattr("dockwright.hooks._pid_alive", lambda pid: True)
    monkeypatch.setenv("TMUX_PANE", "175")
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.setenv("CLAUDE_PARENT_MANAGER", "mgr")
    monkeypatch.setenv("CLAUDE_PARENT_PID", str(own_pid))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "new-sid", "cwd": "/x"})))
    session_start()
    record = state.read_json(paths.ACTIVE / "new-sid.json")
    assert not record.get("nested")
    assert record["name"] == "alpha"
    assert record["funny_name"] == "grumpy-camel"
    assert record["window_id"] == "175"
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
    assert record["name"] == "happy-otter"
    assert record["domain"] == "tickets"
    assert not (paths.ACTIVE / "mgr-old.json").exists()
    from dockwright import identity
    assert identity._list_manager_records()[0]["claude_sid"] == "mgr-new"


def test_session_start_rotation_drops_old_sid_questions(fresh, monkeypatch):
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
    _write_parent_record(sid="old-sid", pid=9999, window_id="175")
    monkeypatch.setattr("dockwright.hooks._ancestor_pids", lambda pid: set())
    monkeypatch.setattr("dockwright.hooks._pid_alive", lambda pid: True)
    monkeypatch.setenv("TMUX_PANE", "175")
    assert _detect_nested_parent("new-sid", cli_pid=9999) is None


def test_detect_nested_parent_rejects_recycled_pid_ancestor(fresh, monkeypatch):
    _write_parent_record(pid=4242)
    monkeypatch.setattr("dockwright.hooks._ancestor_pids", lambda pid: {4242})
    monkeypatch.setattr("dockwright.hooks._pid_looks_like_session",
                        lambda pid: False)
    assert _detect_nested_parent("child-sid", cli_pid=9999) is None


LIVE_PID = 111
DEAD_PID = 222


@pytest.fixture
def orphan_env(fresh, monkeypatch):
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
    from dockwright import hooks
    notifications = []
    monkeypatch.setattr(hooks, "_notify_macos", notifications.append)
    _write_manager()
    _write_worker("w1", "grumpy-yak")
    _write_worker("w2", "other-manager")
    _write_worker("w3", "grumpy-yak", pid=DEAD_PID)
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
    _write_worker("w0", "grumpy-yak", pid=None)
    _write_worker("w1", "grumpy-yak")
    _end_session(monkeypatch)
    flag = state.read_json(paths.ORPHANS / "grumpy-yak.json")
    assert [w["claude_sid"] for w in flag["workers"]] == ["w1"]


def test_worker_session_end_writes_no_orphan_flag(orphan_env, monkeypatch, fresh):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("HOME", str(fresh))
    _write_worker("w1", "grumpy-yak")
    _write_worker("w2", "grumpy-yak")
    _end_session(monkeypatch, sid="w1")
    assert not paths.ORPHANS.exists() or list(paths.ORPHANS.iterdir()) == []


def test_session_end_no_record_writes_no_orphan_flag(orphan_env, monkeypatch):
    _write_worker("w1", "grumpy-yak")
    _end_session(monkeypatch, sid="mgr-gone")
    assert not paths.ORPHANS.exists() or list(paths.ORPHANS.iterdir()) == []


def test_orphan_flag_lands_even_if_distill_raises(orphan_env, monkeypatch):
    import dockwright.hooks as hooks_mod
    def boom(sid, record):
        raise RuntimeError("distill exploded")
    monkeypatch.setattr(hooks_mod, "_maybe_distill_on_session_end", boom)
    _write_manager()
    _write_worker("w1", "grumpy-yak")
    _end_session(monkeypatch)
    assert (paths.ORPHANS / "grumpy-yak.json").exists()


def test_nested_worker_not_counted_in_orphan_flag(orphan_env, monkeypatch):
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


def _write_handoff(handoffs_dir, from_sid="mgr-1", prepared_age_sec=60.0, body=None):
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    path = handoffs_dir / "h1.json"
    if body is None:
        body = {"handoff_id": "h1", "from_sid": from_sid,
                 "prepared_at": now - prepared_age_sec, "consumed_at": None,
                 "trigger_reason": "manual"}
    path.write_text(json.dumps(body) if not isinstance(body, str) else body)
    os.utime(path, (now - prepared_age_sec, now - prepared_age_sec))
    return path


def test_session_end_fresh_handoff_suppresses_flag_and_notification(orphan_env, monkeypatch, fresh):
    from dockwright import hooks
    notifications = []
    monkeypatch.setattr(hooks, "_notify_macos", notifications.append)
    monkeypatch.setattr(paths, "HANDOFFS", fresh / "handoffs")
    _write_handoff(fresh / "handoffs", from_sid="mgr-1", prepared_age_sec=60)
    _write_manager()
    _write_worker("w1", "grumpy-yak")
    _end_session(monkeypatch, reason="clear")
    assert state.read_json(paths.ORPHANS / "grumpy-yak.json") is None
    assert notifications == []


def test_session_end_stale_handoff_does_not_suppress(orphan_env, monkeypatch, fresh):
    from dockwright import hooks
    notifications = []
    monkeypatch.setattr(hooks, "_notify_macos", notifications.append)
    monkeypatch.setattr(paths, "HANDOFFS", fresh / "handoffs")
    _write_handoff(fresh / "handoffs", from_sid="mgr-1",
                   prepared_age_sec=hooks.HANDOFF_SUPPRESS_SEC + 60)
    _write_manager()
    _write_worker("w1", "grumpy-yak")
    _end_session(monkeypatch)
    assert state.read_json(paths.ORPHANS / "grumpy-yak.json") is not None
    assert len(notifications) == 1


def test_session_end_stale_prepared_at_with_fresh_mtime_does_not_suppress(orphan_env, monkeypatch, fresh):
    from dockwright import hooks
    notifications = []
    monkeypatch.setattr(hooks, "_notify_macos", notifications.append)
    monkeypatch.setattr(paths, "HANDOFFS", fresh / "handoffs")
    stale_prepared = time.time() - hooks.HANDOFF_SUPPRESS_SEC - 60
    _write_handoff(fresh / "handoffs", from_sid="mgr-1", prepared_age_sec=0,
                   body={"handoff_id": "h1", "from_sid": "mgr-1",
                         "prepared_at": stale_prepared,
                         "consumed_at": time.time(), "trigger_reason": "manual"})
    _write_manager()
    _write_worker("w1", "grumpy-yak")
    _end_session(monkeypatch)
    assert state.read_json(paths.ORPHANS / "grumpy-yak.json") is not None
    assert len(notifications) == 1


def test_session_end_other_sids_handoff_does_not_suppress(orphan_env, monkeypatch, fresh):
    from dockwright import hooks
    notifications = []
    monkeypatch.setattr(hooks, "_notify_macos", notifications.append)
    monkeypatch.setattr(paths, "HANDOFFS", fresh / "handoffs")
    _write_handoff(fresh / "handoffs", from_sid="mgr-OTHER", prepared_age_sec=60)
    _write_manager()
    _write_worker("w1", "grumpy-yak")
    _end_session(monkeypatch)
    assert state.read_json(paths.ORPHANS / "grumpy-yak.json") is not None
    assert len(notifications) == 1


def test_session_end_malformed_handoff_fails_toward_alarming(orphan_env, monkeypatch, fresh):
    from dockwright import hooks
    notifications = []
    monkeypatch.setattr(hooks, "_notify_macos", notifications.append)
    monkeypatch.setattr(paths, "HANDOFFS", fresh / "handoffs")
    _write_handoff(fresh / "handoffs", body="{not json")
    _write_manager()
    _write_worker("w1", "grumpy-yak")
    _end_session(monkeypatch)
    assert state.read_json(paths.ORPHANS / "grumpy-yak.json") is not None
    assert len(notifications) == 1


def test_session_end_missing_handoffs_dir_does_not_suppress(orphan_env, monkeypatch, fresh):
    from dockwright import hooks
    notifications = []
    monkeypatch.setattr(hooks, "_notify_macos", notifications.append)
    monkeypatch.setattr(paths, "HANDOFFS", fresh / "handoffs-nonexistent")
    _write_manager()
    _write_worker("w1", "grumpy-yak")
    _end_session(monkeypatch)
    assert state.read_json(paths.ORPHANS / "grumpy-yak.json") is not None
    assert len(notifications) == 1


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
    monkeypatch.setenv("HOME", str(fresh))
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
    assert not (fresh / "closed" / "m1.json").exists()
    assert not (fresh / "closed" / "n1.json").exists()


def test_session_end_no_spend_no_ledger_line(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("HOME", str(fresh))
    state.write_json_atomic(fresh / "active" / "s1.json", {
        "claude_sid": "s1", "agent": "worker", "name": "alpha",
        "cwd": "/x", "pid": os.getpid(), "started_at": 1.0,
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    session_end()
    assert _ledger_entries(fresh) == []


def test_clear_rotation_ledgers_old_records_spend(fresh, monkeypatch):
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
    new = state.read_json(fresh / "active" / "new-sid.json")
    assert new["name"] == "alpha"
    assert "spend" not in new


def test_session_end_captures_tagged_headless_spend(fresh, monkeypatch, tmp_path):
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
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_SPEND_CLASS", "distill")
    monkeypatch.setenv("HOME", str(fresh))
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
    assert record["state"] == "idle"


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
    assert "bg=#aa8800,fg=#ffffff" in opts["window-status-current-style"]


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
    assert "bg=#aa3300,fg=#ffffff" in opts["window-status-current-style"]


def test_session_end_manager_spawns_detached_fallback_distill(fresh, monkeypatch):
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
    assert kw["stdin"] is subprocess.DEVNULL
    assert kw["stdout"].name.endswith("distill-fallback.log")
    assert kw["stderr"] is kw["stdout"]


def test_session_end_manager_skips_distill_when_memory_file_exists(fresh, monkeypatch):
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


import sys as _sys

from dockwright.hooks import _proc_argv


def test_proc_argv_reads_own_process_real_syscall():
    argv = _proc_argv(os.getpid())
    assert isinstance(argv, list) and len(argv) >= 1
    joined = " ".join(argv)
    assert "python" in joined or "pytest" in joined


def test_proc_argv_dead_pid_returns_none():
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
    monkeypatch.setattr("dockwright.hooks._proc_argv", lambda pid: None)
    got = _detect_agent_team_parent({"agent_type": "Explore"}, cli_pid=777)
    assert got == {"sid": None, "name": None, "agent_id": None}


def test_detect_teammate_parent_record_missing(fresh, monkeypatch):
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
    assert record["name"] == "nested-eeee1111"
    assert record["agent_id"] == "implementer@session-abcd1234"
    assert record["nested_parent_sid"] == "parent-sid"
    assert record["nested_parent_name"] == "parent-worker"
    assert record["funny_name"] is None
    assert record["window_id"] == ""
    assert record.get("agent") != "manager" or record.get("nested")


def test_session_start_teammate_payload_only_still_nested(fresh, monkeypatch):
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

def test_resolve_session_pid_captured_is_session(monkeypatch):
    monkeypatch.setenv("CLAUDE_PARENT_PID", "500")
    monkeypatch.setattr(hooks, "_pid_looks_like_session", lambda pid: pid == 500)
    assert hooks._resolve_session_pid() == 500


def test_resolve_session_pid_walks_past_live_intermediate(monkeypatch):
    from dockwright import identity
    monkeypatch.setenv("CLAUDE_PARENT_PID", "600")
    monkeypatch.setattr(identity, "_ppid_of", {600: 590, 590: 1}.get)
    monkeypatch.setattr(hooks, "_pid_looks_like_session", lambda pid: pid == 590)
    assert hooks._resolve_session_pid() == 590


def test_resolve_session_pid_dead_intermediate_falls_back_to_own_chain(monkeypatch):
    from dockwright import identity
    monkeypatch.setenv("CLAUDE_PARENT_PID", "600")
    self_parent = os.getppid()
    monkeypatch.setattr(identity, "_ppid_of", {self_parent: 700, 700: 1}.get)
    monkeypatch.setattr(hooks, "_pid_looks_like_session", lambda pid: pid == 700)
    assert hooks._resolve_session_pid() == 700


def test_resolve_session_pid_nearest_session_wins(monkeypatch):
    from dockwright import identity
    monkeypatch.setenv("CLAUDE_PARENT_PID", "600")
    monkeypatch.setattr(identity, "_ppid_of",
                        {600: 590, 590: 500, 500: 400, 400: 1}.get)
    monkeypatch.setattr(hooks, "_pid_looks_like_session",
                        lambda pid: pid in (590, 400))
    assert hooks._resolve_session_pid() == 590


def test_resolve_session_pid_skips_ancestor_walk_when_start_matches(monkeypatch):
    from dockwright import identity

    def _boom(pid):
        raise AssertionError("must not walk ancestors when start already matches")

    monkeypatch.setenv("CLAUDE_PARENT_PID", "500")
    monkeypatch.setattr(identity, "_ppid_of", _boom)
    monkeypatch.setattr(hooks, "_pid_looks_like_session", lambda pid: pid == 500)
    assert hooks._resolve_session_pid() == 500


def test_resolve_session_pid_no_session_returns_captured(monkeypatch):
    from dockwright import identity
    monkeypatch.setenv("CLAUDE_PARENT_PID", "600")
    monkeypatch.setattr(identity, "_ppid_of", lambda pid: None)
    monkeypatch.setattr(hooks, "_pid_looks_like_session", lambda pid: False)
    assert hooks._resolve_session_pid() == 600


def test_ancestor_chain_is_strict_and_ordered(monkeypatch):
    from dockwright import identity
    monkeypatch.setattr(identity, "_ppid_of", {10: 9, 9: 8, 8: 9}.get)
    assert hooks._ancestor_chain(10) == [9, 8]


def test_session_start_records_resolved_session_pid(fresh, monkeypatch):
    from dockwright import identity
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.setenv("CLAUDE_PARENT_PID", "600")
    monkeypatch.setattr(identity, "_ppid_of", {600: 590, 590: 1}.get)
    monkeypatch.setattr(hooks, "_pid_looks_like_session", lambda pid: pid == 590)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    assert state.read_json(fresh / "active" / "s1.json")["pid"] == 590


def test_awake_seconds_works_without_clock_uptime_raw(monkeypatch):
    monkeypatch.delattr(time, "CLOCK_UPTIME_RAW", raising=False)
    v = hooks._awake_seconds()
    assert isinstance(v, float) and v > 0.0


def test_stop_hook_stamps_uptime_without_clock_uptime_raw(fresh, monkeypatch):
    monkeypatch.delattr(time, "CLOCK_UPTIME_RAW", raising=False)
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    stop_hook()
    rec = json.loads((paths.ACTIVE / "s1.json").read_text())
    assert isinstance(rec["last_turn_at_uptime"], float)
    assert rec["state"] == "idle"


def test_session_start_emits_worker_sid_context(fresh, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    payload = json.loads(capsys.readouterr().out)
    ctx = payload["hookSpecificOutput"]
    assert ctx["hookEventName"] == "SessionStart"
    assert "your claude_sid is s1" in ctx["additionalContext"]
    assert "worker_done" in ctx["additionalContext"]


def test_session_start_emits_manager_sid_wording(fresh, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "m1", "cwd": "/x"})))
    session_start()
    payload = json.loads(capsys.readouterr().out)
    ctx = payload["hookSpecificOutput"]
    assert "your session id is m1" in ctx["additionalContext"]
    assert "manager_sid" in ctx["additionalContext"]
    assert "worker_done" not in ctx["additionalContext"]


def test_session_start_codex_worker_emits_no_context(fresh, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.setenv("CLAUDE_WORKER_RUNTIME", "codex")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    session_start()
    assert capsys.readouterr().out == ""


def test_session_end_envless_manager_record_runs_manager_leg(fresh, monkeypatch):
    monkeypatch.delenv("CLAUDE_AGENT", raising=False)
    distills = []
    monkeypatch.setattr(hooks, "_maybe_distill_on_session_end",
                        lambda sid, rec: distills.append(sid))
    orphan_flags = []
    monkeypatch.setattr(hooks, "_flag_orphaned_workers",
                        lambda sid, rec, reason: orphan_flags.append(sid))
    state.write_json_atomic(fresh / "active" / "m1.json", {
        "claude_sid": "m1", "agent": "manager", "name": "lucky-werewolf",
        "cwd": "/x", "iterm_sid": "i1", "pid": 1, "started_at": 0,
        "domain": "general",
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "m1"})))
    session_end()
    assert not (fresh / "active" / "m1.json").exists(), "record must be cleaned up"
    assert distills == ["m1"], "fallback distill must fire for the env-less manager"
    assert orphan_flags == ["m1"], "orphan flagging must run for the env-less manager"


def test_session_end_envless_worker_record_archives(fresh, monkeypatch):
    monkeypatch.delenv("CLAUDE_AGENT", raising=False)
    monkeypatch.setenv("HOME", str(fresh))
    state.write_json_atomic(fresh / "active" / "w1.json", {
        "claude_sid": "w1", "agent": "worker", "name": "alpha", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 12345.0,
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "w1"})))
    session_end()
    assert not (fresh / "active" / "w1.json").exists()
    closed = json.loads((fresh / "closed" / "w1.json").read_text())
    assert closed["closed_reason"] == "session_end"


def test_session_end_distill_sentinel_beats_record_gate(fresh, monkeypatch):
    monkeypatch.delenv("CLAUDE_AGENT", raising=False)
    monkeypatch.setenv(paths.DISTILL_ENV_SENTINEL, "1")
    state.write_json_atomic(fresh / "active" / "d1.json", {
        "claude_sid": "d1", "agent": "manager", "name": "ghost", "cwd": "/x",
        "iterm_sid": "i1", "pid": 1, "started_at": 0, "domain": "general",
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "d1"})))
    session_end()
    assert (fresh / "active" / "d1.json").exists(), "distill child must not touch records"


def test_session_end_headless_spend_uses_passed_payload(fresh, monkeypatch):
    monkeypatch.delenv("CLAUDE_AGENT", raising=False)
    monkeypatch.setenv("CLAUDE_SPEND_CLASS", "distill")
    events = []
    from dockwright import spend_ledger
    monkeypatch.setattr(spend_ledger, "append_headless_event",
                        lambda cls, sid, path: events.append((cls, sid, path)))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "h1", "transcript_path": "/t/h1.jsonl"})))
    session_end()
    assert events == [("distill", "h1", "/t/h1.jsonl")]


def test_session_start_pending_takeover_manager_writes_no_record(fresh, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "noisy-wizard")
    monkeypatch.setenv("CLAUDE_ITERM_SID", "i1")
    monkeypatch.setenv("DOCKWRIGHT_PENDING_TAKEOVER", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "rec-1", "cwd": "/x"})))
    session_start()
    assert list((fresh / "active").iterdir()) == []
    out = capsys.readouterr().out
    assert "rec-1" in out and "session id" in out


def test_session_start_pending_takeover_ignores_workers(fresh, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.setenv("CLAUDE_WORKER_NAME", "alpha")
    monkeypatch.setenv("CLAUDE_ITERM_SID", "i1")
    monkeypatch.setenv("DOCKWRIGHT_PENDING_TAKEOVER", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "w1", "cwd": "/x"})))
    session_start()
    assert state.read_json(fresh / "active" / "w1.json")["name"] == "alpha"


def test_session_start_pending_takeover_requires_exact_one(fresh, monkeypatch):
    for i, bad in enumerate(("0", "true", "")):
        monkeypatch.setenv("CLAUDE_AGENT", "manager")
        monkeypatch.setenv("CLAUDE_ITERM_SID", "i1")
        monkeypatch.setenv("DOCKWRIGHT_PENDING_TAKEOVER", bad)
        sid = f"m-{i}"
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": sid, "cwd": "/x"})))
        session_start()
        assert state.read_json(fresh / "active" / f"{sid}.json") is not None, bad
        (fresh / "active" / f"{sid}.json").unlink()


def test_session_start_pending_takeover_keeps_existing_record_branch(fresh, monkeypatch):
    state.write_json_atomic(fresh / "active" / "rec-1.json", {
        "claude_sid": "rec-1", "agent": "manager", "name": "noisy-wizard",
        "cwd": "/old", "window_id": "i0", "pid": 1, "started_at": 123.0,
        "state": "idle", "last_turn_at": None, "last_summary": None,
        "domain": "personal", "parent_manager_name": None, "runtime": "claude",
    })
    monkeypatch.setenv("CLAUDE_AGENT", "manager")
    monkeypatch.setenv("CLAUDE_ITERM_SID", "i2")
    monkeypatch.setenv("DOCKWRIGHT_PENDING_TAKEOVER", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "rec-1", "cwd": "/new"})))
    session_start()
    record = state.read_json(fresh / "active" / "rec-1.json")
    assert record["name"] == "noisy-wizard"
    assert record["domain"] == "personal"
    assert record["cwd"] == "/new"
    assert record["window_id"] == "i2"


def test_session_start_pending_takeover_keeps_rotation_branch(fresh, monkeypatch):
    own_pid = os.getpid()
    state.write_json_atomic(fresh / "active" / "mgr-old.json", {
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
    monkeypatch.setenv("DOCKWRIGHT_PENDING_TAKEOVER", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "mgr-new", "cwd": "/x"})))
    session_start()
    record = state.read_json(fresh / "active" / "mgr-new.json")
    assert record is not None and record["name"] == "happy-otter"
    assert record["domain"] == "tickets"
    assert not (fresh / "active" / "mgr-old.json").exists()
