import json
import os
import time
from pathlib import Path
from dockwright.transcript import (
    delegation_fresh_sec,
    find_session_log,
    is_delegating,
    last_assistant_summary,
    latest_subagent_mtime,
)

def test_find_session_log_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert find_session_log("nonexistent-sid") is None

def test_find_session_log_finds_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    projects = tmp_path / ".claude" / "projects" / "-Users-x"
    projects.mkdir(parents=True)
    log = projects / "the-sid.jsonl"
    log.write_text("")
    assert find_session_log("the-sid") == log

def test_find_session_log_finds_codex_rollout(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    sessions = tmp_path / ".codex" / "sessions" / "2026" / "05" / "26"
    sessions.mkdir(parents=True)
    log = sessions / "rollout-2026-05-26T10-55-35-the-sid.jsonl"
    log.write_text("{}")
    assert find_session_log("the-sid", runtime="codex") == log

def test_last_assistant_summary_extracts_codex_output_text(tmp_path):
    log = tmp_path / "codex.jsonl"
    log.write_text("\n".join([
        json.dumps({
            "timestamp": "2026-05-26T04:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "older"}],
            },
        }),
        json.dumps({
            "timestamp": "2026-05-26T04:01:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "newer summary"}],
            },
        }),
    ]))
    assert last_assistant_summary(log) == ("newer summary", "2026-05-26T04:01:00Z")

def test_last_assistant_summary_extracts_text(tmp_path):
    log = tmp_path / "x.jsonl"
    lines = [
        {"type": "user", "message": {"content": "hi"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello there"}]}, "timestamp": "2026-05-15T10:00:00Z"},
        {"type": "user", "message": {"content": "more"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "final answer"}]}, "timestamp": "2026-05-15T10:05:00Z"},
    ]
    log.write_text("\n".join(json.dumps(l) for l in lines))
    summary, ts = last_assistant_summary(log)
    assert "final answer" in summary
    assert ts == "2026-05-15T10:05:00Z"

def test_last_assistant_summary_empty_log(tmp_path):
    log = tmp_path / "x.jsonl"
    log.write_text("")
    assert last_assistant_summary(log) == (None, None)

def test_last_assistant_summary_truncates(tmp_path):
    log = tmp_path / "x.jsonl"
    long_text = "x" * 500
    log.write_text(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": long_text}]},
        "timestamp": "2026-05-15T10:00:00Z",
    }))
    summary, _ = last_assistant_summary(log, max_chars=120)
    assert len(summary) <= 120


def _make_session_tree(tmp_path, sid="del-sid", slug="-Users-x"):
    """Synthetic ~/.claude/projects/<slug>/ tree: main log + subagents dir."""
    project_dir = tmp_path / ".claude" / "projects" / slug
    project_dir.mkdir(parents=True)
    log = project_dir / f"{sid}.jsonl"
    log.write_text("")
    subagents = project_dir / sid / "subagents"
    subagents.mkdir(parents=True)
    return log, subagents


def test_latest_subagent_mtime_zero_when_dir_absent(tmp_path):
    project_dir = tmp_path / ".claude" / "projects" / "-Users-x"
    project_dir.mkdir(parents=True)
    log = project_dir / "s.jsonl"
    log.write_text("")
    assert latest_subagent_mtime(log, "s") == 0.0


def test_latest_subagent_mtime_zero_when_dir_empty(tmp_path):
    log, _subagents = _make_session_tree(tmp_path)
    assert latest_subagent_mtime(log, "del-sid") == 0.0


def test_latest_subagent_mtime_picks_newest_and_ignores_meta(tmp_path):
    log, subagents = _make_session_tree(tmp_path)
    old = subagents / "agent-aaa.jsonl"
    old.write_text("{}")
    os.utime(old, (1000.0, 1000.0))
    new = subagents / "agent-bbb.jsonl"
    new.write_text("{}")
    os.utime(new, (2000.0, 2000.0))
    meta = subagents / "agent-ccc.meta.json"
    meta.write_text("{}")
    os.utime(meta, (3000.0, 3000.0))
    assert latest_subagent_mtime(log, "del-sid") == 2000.0


def test_is_delegating_true_when_subagent_outlived_main_log(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    log, subagents = _make_session_tree(tmp_path)
    now = time.time()
    os.utime(log, (now - 60, now - 60))                  # main log froze at Stop
    agent = subagents / "agent-aaa.jsonl"
    agent.write_text("{}")
    os.utime(agent, (now - 5, now - 5))                  # subagent still writing
    record = {"claude_sid": "del-sid", "runtime": "claude"}
    assert is_delegating(record, now) is True


def test_is_delegating_false_for_consumed_foreground_agent(tmp_path, monkeypatch):
    """Foreground agent result consumed in-turn: main log has LATER writes."""
    monkeypatch.setenv("HOME", str(tmp_path))
    log, subagents = _make_session_tree(tmp_path)
    now = time.time()
    agent = subagents / "agent-aaa.jsonl"
    agent.write_text("{}")
    os.utime(agent, (now - 30, now - 30))
    os.utime(log, (now - 5, now - 5))                    # worker wrote after the agent
    record = {"claude_sid": "del-sid", "runtime": "claude"}
    assert is_delegating(record, now) is False


def test_is_delegating_false_when_subagent_quiet_past_grace(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    log, subagents = _make_session_tree(tmp_path)
    now = time.time()
    os.utime(log, (now - 600, now - 600))
    agent = subagents / "agent-aaa.jsonl"
    agent.write_text("{}")
    os.utime(agent, (now - 300, now - 300))              # grew after log, but stale
    record = {"claude_sid": "del-sid", "runtime": "claude"}
    assert is_delegating(record, now) is False


def test_is_delegating_false_for_codex_runtime_and_missing_log(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert is_delegating({"claude_sid": "x", "runtime": "codex"}, 0.0) is False
    assert is_delegating({"claude_sid": "ghost", "runtime": "claude"}, 0.0) is False
    assert is_delegating({}, 0.0) is False


def test_is_delegating_accepts_preresolved_log(tmp_path, monkeypatch):
    """Callers that already resolved the session log pass it in — no second scan."""
    monkeypatch.setenv("HOME", str(tmp_path))
    log, subagents = _make_session_tree(tmp_path)
    now = time.time()
    os.utime(log, (now - 60, now - 60))
    agent = subagents / "agent-aaa.jsonl"
    agent.write_text("{}")
    os.utime(agent, (now - 5, now - 5))
    record = {"claude_sid": "del-sid", "runtime": "claude"}
    assert is_delegating(record, now, log=log) is True


def test_is_delegating_false_at_exact_mtime_tie(tmp_path, monkeypatch):
    """Subagent mtime == main log mtime: the growth predicate is strict (>),
    so an exact tie reads as not delegating (fail-safe grey)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    log, subagents = _make_session_tree(tmp_path)
    now = time.time()
    tie = now - 5
    agent = subagents / "agent-aaa.jsonl"
    agent.write_text("{}")
    os.utime(agent, (tie, tie))
    os.utime(log, (tie, tie))
    record = {"claude_sid": "del-sid", "runtime": "claude"}
    assert is_delegating(record, now) is False


def test_delegation_fresh_sec_reads_grace_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", "300")
    assert delegation_fresh_sec() == 300


def test_delegation_fresh_sec_default_and_invalid_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", raising=False)
    assert delegation_fresh_sec() == 120
    monkeypatch.setenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", "abc")
    assert delegation_fresh_sec() == 120
    monkeypatch.setenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", "-5")
    assert delegation_fresh_sec() == 120


def test_is_delegating_grace_env_moves_freshness_window(tmp_path, monkeypatch):
    """The monitor's grace env override moves the read-side freshness with it,
    so a non-default grace can't split monitor truth from list_workers/paint."""
    monkeypatch.setenv("HOME", str(tmp_path))
    log, subagents = _make_session_tree(tmp_path)
    now = time.time()
    os.utime(log, (now - 400, now - 400))
    agent = subagents / "agent-aaa.jsonl"
    agent.write_text("{}")
    os.utime(agent, (now - 200, now - 200))              # 200s old: stale at 120, fresh at 300
    record = {"claude_sid": "del-sid", "runtime": "claude"}
    monkeypatch.setenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", "300")
    assert is_delegating(record, now) is True
    monkeypatch.setenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", "60")
    assert is_delegating(record, now) is False
