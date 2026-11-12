import json
import os
import time
from pathlib import Path
from dockwright.transcript import (
    delegation_fresh_sec,
    episode_grace_sec,
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

def test_find_codex_rollout_honours_codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    codex_home = tmp_path / "elsewhere" / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    sessions = codex_home / "sessions" / "2026" / "09" / "07"
    sessions.mkdir(parents=True)
    log = sessions / "rollout-2026-09-07T10-00-00-the-sid.jsonl"
    log.write_text("{}")
    assert find_session_log("the-sid", runtime="codex") == log
    monkeypatch.delenv("CODEX_HOME")
    assert find_session_log("the-sid", runtime="codex") is None


def test_find_session_log_searches_the_root_it_is_given_on_both_runtimes(
        tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    projects = tmp_path / "pool" / "projects" / "-Users-x"
    projects.mkdir(parents=True)
    claude_log = projects / "the-sid.jsonl"
    claude_log.write_text("")
    sessions = tmp_path / "pool" / "sessions" / "2026" / "09"
    sessions.mkdir(parents=True)
    codex_log = sessions / "rollout-2026-09-08T10-00-00-the-sid.jsonl"
    codex_log.write_text("{}")
    assert find_session_log("the-sid") is None
    assert find_session_log("the-sid", "claude", tmp_path / "pool" / "projects") == claude_log
    assert find_session_log("the-sid", "codex", tmp_path / "pool" / "sessions") == codex_log

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
    os.utime(log, (now - 60, now - 60))
    agent = subagents / "agent-aaa.jsonl"
    agent.write_text("{}")
    os.utime(agent, (now - 5, now - 5))
    record = {"claude_sid": "del-sid", "runtime": "claude"}
    assert is_delegating(record, now) is True


def test_is_delegating_false_for_consumed_foreground_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    log, subagents = _make_session_tree(tmp_path)
    now = time.time()
    agent = subagents / "agent-aaa.jsonl"
    agent.write_text("{}")
    os.utime(agent, (now - 30, now - 30))
    os.utime(log, (now - 5, now - 5))
    record = {"claude_sid": "del-sid", "runtime": "claude"}
    assert is_delegating(record, now) is False


def test_is_delegating_false_when_subagent_quiet_past_grace(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_ORCH_EPISODE_GRACE_SEC", raising=False)
    log, subagents = _make_session_tree(tmp_path)
    now = time.time()
    os.utime(log, (now - 2000, now - 2000))
    agent = subagents / "agent-aaa.jsonl"
    agent.write_text("{}")
    os.utime(agent, (now - 1000, now - 1000))
    record = {"claude_sid": "del-sid", "runtime": "claude"}
    assert is_delegating(record, now) is False


def test_is_delegating_false_for_codex_runtime_and_missing_log(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert is_delegating({"claude_sid": "x", "runtime": "codex"}, 0.0) is False
    assert is_delegating({"claude_sid": "ghost", "runtime": "claude"}, 0.0) is False
    assert is_delegating({}, 0.0) is False


def test_is_delegating_accepts_preresolved_log(tmp_path, monkeypatch):
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


def test_turn_end_grace_env_no_longer_tightens_the_display_window(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_ORCH_EPISODE_GRACE_SEC", raising=False)
    log, subagents = _make_session_tree(tmp_path)
    now = time.time()
    os.utime(log, (now - 400, now - 400))
    agent = subagents / "agent-aaa.jsonl"
    agent.write_text("{}")
    os.utime(agent, (now - 200, now - 200))
    record = {"claude_sid": "del-sid", "runtime": "claude"}
    monkeypatch.setenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", "60")
    assert is_delegating(record, now) is True


def test_episode_grace_default_and_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_EPISODE_GRACE_SEC", raising=False)
    monkeypatch.delenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", raising=False)
    assert episode_grace_sec() == 900
    monkeypatch.setenv("CLAUDE_ORCH_EPISODE_GRACE_SEC", "1800")
    assert episode_grace_sec() == 1800
    monkeypatch.setenv("CLAUDE_ORCH_EPISODE_GRACE_SEC", "abc")
    assert episode_grace_sec() == 900
    monkeypatch.setenv("CLAUDE_ORCH_EPISODE_GRACE_SEC", "0")
    assert episode_grace_sec() == 900


def test_episode_grace_never_falls_below_turn_end_grace(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_EPISODE_GRACE_SEC", "60")
    monkeypatch.setenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", "1200")
    assert episode_grace_sec() == 1200


def test_is_delegating_survives_a_subagent_pause_past_the_turn_end_grace(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_ORCH_EPISODE_GRACE_SEC", raising=False)
    monkeypatch.delenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", raising=False)
    log, subagents = _make_session_tree(tmp_path)
    now = time.time()
    os.utime(log, (now - 400, now - 400))
    agent = subagents / "agent-aaa.jsonl"
    agent.write_text("{}")
    os.utime(agent, (now - 300, now - 300))
    record = {"claude_sid": "del-sid", "runtime": "claude"}
    assert delegation_fresh_sec() == 120
    assert is_delegating(record, now) is True



def test_is_delegating_freshness_moves_with_the_episode_grace_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    log, subagents = _make_session_tree(tmp_path)
    now = time.time()
    os.utime(log, (now - 800, now - 800))
    agent = subagents / "agent-aaa.jsonl"
    agent.write_text("{}")
    os.utime(agent, (now - 600, now - 600))
    record = {"claude_sid": "del-sid", "runtime": "claude"}
    monkeypatch.setenv("CLAUDE_ORCH_EPISODE_GRACE_SEC", "1200")
    assert is_delegating(record, now) is True
    monkeypatch.setenv("CLAUDE_ORCH_EPISODE_GRACE_SEC", "300")
    assert is_delegating(record, now) is False


def test_is_delegating_true_for_a_worker_with_a_live_child(tmp_path, monkeypatch):
    from dockwright import paths, state
    monkeypatch.setattr(paths, "ACTIVE", tmp_path / "active")
    (tmp_path / "active").mkdir()
    state.write_json_atomic(paths.ACTIVE / "c1.json", {
        "claude_sid": "c1", "agent": "worker", "kind": "subworker", "name": "alpha-review",
        "parent_name": "alpha", "pid": os.getpid()})
    assert is_delegating({"claude_sid": "w1", "name": "alpha", "runtime": "codex"}, time.time()) is True
    assert is_delegating({"claude_sid": "w2", "name": "bravo", "runtime": "claude"}, time.time()) is False


TURN_A = ('{"type":"turn.completed","usage":{"input_tokens":212735,"cached_input_tokens":153088,'
          '"cache_write_input_tokens":0,"output_tokens":255,"reasoning_output_tokens":101}}')
TURN_B = ('{"type":"turn.completed","usage":{"input_tokens":70690,"cached_input_tokens":0,'
          '"cache_write_input_tokens":12,"output_tokens":9,"reasoning_output_tokens":0}}')


def test_codex_usage_of_maps_turn_completed_onto_the_spend_scheme():
    from dockwright.transcript import codex_usage_of
    assert codex_usage_of(json.loads(TURN_A)) == {
        "out_tokens": 255, "in_tokens": 59647, "cache_read_tokens": 153088,
        "cache_creation_tokens": 0, "reasoning_tokens": 101}
    assert codex_usage_of({"type": "turn.started"}) is None
    assert codex_usage_of({"type": "turn.completed", "usage": "x"}) is None
    assert codex_usage_of({"type": "turn.completed",
                           "usage": {"input_tokens": 5, "cached_input_tokens": 9}})["in_tokens"] == 0


def test_recount_codex_spend_sums_one_round_log_and_is_idempotent(tmp_path):
    from dockwright.transcript import recount_codex_spend
    log = tmp_path / "r1.log"
    log.write_text("hook: SessionStart\n" + TURN_A + "\nReading additional input from stdin…\n" + TURN_B + "\n")
    first = recount_codex_spend(log, None)
    assert first == {"turns": 2, "out_tokens": 264, "in_tokens": 130337, "cache_read_tokens": 153088,
                     "cache_creation_tokens": 12, "reasoning_tokens": 101, "last_turn_out": 9,
                     "by_model": {}}
    assert recount_codex_spend(log, first) == first


def test_recount_codex_spend_keeps_prior_without_events_and_ignores_sibling_rounds(tmp_path):
    from dockwright.transcript import recount_codex_spend
    (tmp_path / "r1.log").write_text(TURN_A + "\n")
    r2 = tmp_path / "r2.log"
    r2.write_text('{"type":"thread.started","thread_id":"t"}\n')
    prior = {"turns": 1, "out_tokens": 1}
    assert recount_codex_spend(r2, prior) == prior
    assert recount_codex_spend(tmp_path / "missing.log", prior) == prior


def test_is_delegating_reads_live_children_from_the_records_it_is_given(tmp_path, monkeypatch):
    from dockwright import paths
    monkeypatch.setattr(paths, "ACTIVE", tmp_path / "active")
    (tmp_path / "active").mkdir()
    parent = {"claude_sid": "p1", "name": "alpha", "runtime": "codex"}
    child = {"claude_sid": "c1", "kind": "subworker", "parent_name": "alpha",
             "pid": os.getpid()}
    assert is_delegating(parent, time.time(), records=[child]) is True
    assert is_delegating(parent, time.time(), records=[]) is False
