import os

import pytest

from dockwright import session_pid


def _table(monkeypatch, parents: dict, sessions: set):
    monkeypatch.setattr(session_pid, "_ppid_of", lambda pid: parents.get(pid))
    monkeypatch.setattr(session_pid, "_pid_looks_like_session", lambda pid: pid in sessions)


def test_explicit_start_that_is_a_session_returns_itself(monkeypatch):
    _table(monkeypatch, {100: 1}, {100})
    assert session_pid.resolve_cli_pid(100) == 100


def test_explicit_start_walks_ancestors_to_the_first_session(monkeypatch):
    _table(monkeypatch, {300: 200, 200: 100, 100: 1}, {100})
    assert session_pid.resolve_cli_pid(300) == 100


def test_explicit_start_ignores_claude_parent_pid_seed(monkeypatch):
    monkeypatch.setenv("CLAUDE_PARENT_PID", "900")
    _table(monkeypatch, {300: 200, 200: 100, 100: 1, 900: 1}, {100, 900})
    assert session_pid.resolve_cli_pid(300) == 100


def test_explicit_start_returns_none_when_no_session_in_chain(monkeypatch):
    _table(monkeypatch, {300: 200, 200: 1}, set())
    assert session_pid.resolve_cli_pid(300) is None


def test_default_mode_seeds_from_claude_parent_pid(monkeypatch):
    monkeypatch.setenv("CLAUDE_PARENT_PID", "900")
    monkeypatch.setattr(os, "getppid", lambda: 300)
    _table(monkeypatch, {300: 200, 200: 100, 100: 1, 900: 1}, {100, 900})
    assert session_pid.resolve_cli_pid() == 900


def test_default_mode_tries_getppid_when_seed_leads_nowhere(monkeypatch):
    monkeypatch.setenv("CLAUDE_PARENT_PID", "900")
    monkeypatch.setattr(os, "getppid", lambda: 300)
    _table(monkeypatch, {300: 200, 200: 100, 100: 1, 900: 1}, {100})
    assert session_pid.resolve_cli_pid() == 100


def test_default_mode_returns_none_when_nothing_matches(monkeypatch):
    monkeypatch.delenv("CLAUDE_PARENT_PID", raising=False)
    monkeypatch.setattr(os, "getppid", lambda: 300)
    _table(monkeypatch, {300: 1}, set())
    assert session_pid.resolve_cli_pid() is None


def test_hook_resolver_keeps_captured_tail(monkeypatch):
    from dockwright import hooks
    monkeypatch.setenv("CLAUDE_PARENT_PID", "900")
    monkeypatch.setattr(os, "getppid", lambda: 300)
    _table(monkeypatch, {300: 1, 900: 1}, set())
    assert hooks._resolve_session_pid() == 900


def test_hook_resolver_matches_shared_function_when_a_session_exists(monkeypatch):
    from dockwright import hooks
    monkeypatch.setenv("CLAUDE_PARENT_PID", "900")
    monkeypatch.setattr(os, "getppid", lambda: 300)
    _table(monkeypatch, {300: 200, 200: 100, 100: 1, 900: 1}, {100})
    assert hooks._resolve_session_pid() == session_pid.resolve_cli_pid() == 100


def test_join_pair_mcp_server_and_monitor_loop_agree(monkeypatch):
    parents = {5000: 4000, 4100: 4000, 4200: 4100, 4000: 1}
    _table(monkeypatch, parents, {4000})
    from_mcp_server = session_pid.resolve_cli_pid(4000) or 4000
    from_monitor_loop = session_pid.resolve_cli_pid(4100)
    assert from_mcp_server == from_monitor_loop == 4000


def test_resolve_cli_pid_none_falls_back_to_the_start_pid_itself(monkeypatch):
    from dockwright import mcp_server
    _table(monkeypatch, {1001: 1}, set())
    monkeypatch.setattr(mcp_server.os, "getppid", iter([1001]).__next__)
    monkeypatch.setattr(mcp_server, "resolve_cli_pid", session_pid.resolve_cli_pid)
    parent_pid = mcp_server.os.getppid()
    assert (session_pid.resolve_cli_pid(parent_pid) or parent_pid) == 1001


def test_looks_like_session_accepts_claude_and_codex_only():
    assert session_pid._looks_like_session("/usr/local/bin/claude --model x")
    assert session_pid._looks_like_session("codex exec")
    assert not session_pid._looks_like_session("python -m dockwright monitor questions")
    assert not session_pid._looks_like_session("")


def test_iter_ancestors_stops_on_cycle_or_root(monkeypatch):
    monkeypatch.setattr(session_pid, "_ppid_of", {100: 50, 50: 10, 10: 1}.get)
    assert list(session_pid._iter_ancestors(100)) == [50, 10]
    monkeypatch.setattr(session_pid, "_ppid_of", {100: 50, 50: 60, 60: 50}.get)
    assert list(session_pid._iter_ancestors(100)) == [50, 60]
