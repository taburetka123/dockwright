import importlib.util
import json
import time
from pathlib import Path

import pytest

from dockwright import state


REPO_ROOT = Path(__file__).resolve().parent.parent
PREFLIGHT_PATH = REPO_ROOT / "deploy" / "scripts" / "preflight_cleanup.py"


def _load_preflight():
    spec = importlib.util.spec_from_file_location("preflight_under_test", PREFLIGHT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def preflight(tmp_path, monkeypatch):
    mod = _load_preflight()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(mod, "HANDOFFS", tmp_path / "handoffs")
    monkeypatch.setattr(mod, "DONE", tmp_path / "done")
    monkeypatch.setattr(mod, "CLOSED", tmp_path / "closed")
    monkeypatch.setattr(mod, "TURN_ENDS", tmp_path / "turn-ends")
    monkeypatch.setattr(mod, "QUESTIONS", tmp_path / "questions")
    monkeypatch.setattr(mod, "MANAGER_LOCK", tmp_path / "manager.lock")
    monkeypatch.setattr(mod, "NOTIFY_OUTBOX", tmp_path / "notify-outbox")
    (tmp_path / "active").mkdir()
    (tmp_path / "done").mkdir()
    (tmp_path / "turn-ends").mkdir()
    (tmp_path / "questions").mkdir()
    return mod


def _write_old(path: Path, age_sec: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(path, {"x": 1})
    old = time.time() - age_sec
    import os
    os.utime(path, (old, old))


def test_prune_turn_ends_recurses_per_manager_subdirs(preflight):
    """Stale turn-ends are pruned across per-manager subdirs and _unscoped, not just flat."""
    old_age = preflight.STALE_TURN_END_SEC + 60
    _write_old(preflight.TURN_ENDS / "manager-a" / "s1-1.json", old_age)
    _write_old(preflight.TURN_ENDS / "_unscoped" / "s2-2.json", old_age)
    _write_old(preflight.TURN_ENDS / "legacy-flat.json", old_age)  # pre-scoping layout
    _write_old(preflight.TURN_ENDS / "manager-a" / "fresh.json", 0)  # recent → keep

    pruned = preflight._prune_turn_ends(time.time())

    assert pruned == 3
    remaining = list(preflight.TURN_ENDS.rglob("*.json"))
    assert [p.name for p in remaining] == ["fresh.json"]


def test_prune_done_recurses_per_manager_subdirs(preflight):
    old_age = preflight.STALE_DONE_SEC + 60
    _write_old(preflight.DONE / "manager-b" / "s1-1.json", old_age)
    _write_old(preflight.DONE / "_unscoped" / "s2-2.json", old_age)
    _write_old(preflight.DONE / "manager-b" / "fresh.json", 0)

    pruned = preflight._prune_done(time.time())

    assert pruned == 2
    remaining = list(preflight.DONE.rglob("*.json"))
    assert [p.name for p in remaining] == ["fresh.json"]


def test_prune_active_preserves_live_manager_records(preflight, monkeypatch):
    state.write_json_atomic(preflight.ACTIVE / "mgr-live.json", {
        "claude_sid": "c174f986-95bd-4c8f-8991-bc4a90912df4",
        "agent": "manager",
        "name": "spry-walrus",
        "pid": 8629,
    })
    monkeypatch.setattr(preflight, "_pid_alive", lambda pid: pid == 8629)
    monkeypatch.setattr(preflight, "_process_command", lambda pid: "claude --resume abc")

    pruned, kept_odd = preflight._prune_active()

    assert pruned == []
    assert kept_odd == []
    assert (preflight.ACTIVE / "mgr-live.json").exists()


def test_prune_active_keeps_and_reports_alive_record_with_non_session_command(preflight, monkeypatch):
    """A live pid whose process is NOT a claude/codex session (pid recycling, or a
    record we can't explain) is odd-looking: never deleted, surfaced for a human."""
    state.write_json_atomic(preflight.ACTIVE / "mgr-recycled.json", {
        "claude_sid": "sid-recycled",
        "agent": "manager",
        "name": "brave-griffin",
        "pid": 4242,
    })
    monkeypatch.setattr(preflight, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(preflight, "_process_command", lambda pid: "/usr/sbin/distnoted agent")

    pruned, kept_odd = preflight._prune_active()

    assert pruned == []
    assert (preflight.ACTIVE / "mgr-recycled.json").exists()
    assert len(kept_odd) == 1
    assert "brave-griffin" in kept_odd[0]


def test_looks_like_session_matches_executable_token_only(preflight):
    """Mirrors sweep._looks_like_session: only argv[0]'s basename counts, so a
    claude/codex token in args (mount paths, container names) can't make a
    recycled pid read as a session."""
    assert preflight._looks_like_session("claude --settings {}")
    assert preflight._looks_like_session("/Users/testop/.local/bin/codex resume x")
    assert not preflight._looks_like_session("docker run -v /mnt/claude:/data img")
    assert not preflight._looks_like_session("zsh -ic claude")
    assert not preflight._looks_like_session("")


def test_looks_like_session_mirror_agrees_with_sweep(preflight):
    """The duplication is intentional (stdlib-only script); this pins the two
    implementations against drifting apart."""
    from dockwright import sweep
    commands = [
        "claude --settings {}",
        "/Users/testop/.local/bin/claude --resume abc",
        "codex resume xyz",
        "docker run -i --rm --name claude crystaldba/postgres-mcp",
        "docker run -i --rm --mount-from /mnt/claude crystaldba/postgres-mcp",
        "zsh -ic claude",
        "/usr/sbin/distnoted agent",
        "",
    ]
    for command in commands:
        assert preflight._looks_like_session(command) == sweep._looks_like_session(command), command


def test_prune_active_reports_alive_wrapper_shell_as_odd(preflight, monkeypatch):
    """A record whose alive pid is a wrapper shell (claude only as an argument)
    is odd-looking under argv[0]-only matching: kept, surfaced for a human."""
    state.write_json_atomic(preflight.ACTIVE / "mgr-wrapper.json", {
        "claude_sid": "sid-wrapper",
        "agent": "manager",
        "name": "shy-kraken",
        "pid": 5151,
    })
    monkeypatch.setattr(preflight, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(preflight, "_process_command", lambda pid: "zsh -ic claude")

    pruned, kept_odd = preflight._prune_active()

    assert pruned == []
    assert (preflight.ACTIVE / "mgr-wrapper.json").exists()
    assert len(kept_odd) == 1
    assert "shy-kraken" in kept_odd[0]


def test_prune_active_keeps_and_reports_records_without_usable_pid(preflight, monkeypatch):
    """Missing or non-positive pid can't prove the session dead — keep and report,
    don't silently skip (missing pid) or delete (pid 0)."""
    state.write_json_atomic(preflight.ACTIVE / "mgr-no-pid.json", {
        "claude_sid": "sid-no-pid",
        "agent": "manager",
        "name": "odd-sphinx",
    })
    state.write_json_atomic(preflight.ACTIVE / "mgr-zero-pid.json", {
        "claude_sid": "sid-zero-pid",
        "agent": "manager",
        "name": "odd-hydra",
        "pid": 0,
    })
    monkeypatch.setattr(preflight, "_pid_alive", lambda pid: False)

    pruned, kept_odd = preflight._prune_active()

    assert pruned == []
    assert (preflight.ACTIVE / "mgr-no-pid.json").exists()
    assert (preflight.ACTIVE / "mgr-zero-pid.json").exists()
    assert len(kept_odd) == 2
    assert any("odd-sphinx" in entry for entry in kept_odd)
    assert any("odd-hydra" in entry for entry in kept_odd)


def test_prune_active_keeps_and_reports_record_with_pid_beyond_os_range(preflight):
    """os.kill raises OverflowError (not OSError) for pids above the C int range —
    a poisoned record must be classified no-usable-pid (kept + reported), not
    traceback the whole preflight at every /manager boot. No _pid_alive mock:
    the test proves the guard fires before os.kill ever sees the huge pid."""
    state.write_json_atomic(preflight.ACTIVE / "mgr-huge-pid.json", {
        "claude_sid": "sid-huge-pid",
        "agent": "manager",
        "name": "huge-golem",
        "pid": 2**31,
    })

    pruned, kept_odd = preflight._prune_active()

    assert pruned == []
    assert (preflight.ACTIVE / "mgr-huge-pid.json").exists()
    assert len(kept_odd) == 1
    assert "huge-golem" in kept_odd[0]


def test_prune_active_real_ps_path_keeps_own_pid_record(preflight):
    """No mocks: the pytest process's own pid is alive and its real ps command
    line is python, not claude/codex — the real _process_command/_looks_like_session
    path must classify it odd-looking and keep it."""
    import os
    state.write_json_atomic(preflight.ACTIVE / "mgr-real-pid.json", {
        "claude_sid": "sid-real-pid",
        "agent": "manager",
        "name": "real-pid-record",
        "pid": os.getpid(),
    })

    pruned, kept_odd = preflight._prune_active()

    assert pruned == []
    assert (preflight.ACTIVE / "mgr-real-pid.json").exists()
    assert any("real-pid-record" in entry for entry in kept_odd)


def test_prune_active_prunes_dead_pid_record_and_names_it(preflight, monkeypatch):
    state.write_json_atomic(preflight.ACTIVE / "mgr-dead.json", {
        "claude_sid": "sid-dead",
        "agent": "manager",
        "name": "late-yak",
        "pid": 999999,
    })
    state.write_json_atomic(preflight.QUESTIONS / "q1.json", {"worker_sid": "sid-dead"})
    monkeypatch.setattr(preflight, "_pid_alive", lambda pid: False)

    pruned, kept_odd = preflight._prune_active()

    assert pruned == ["late-yak"]
    assert kept_odd == []
    assert not (preflight.ACTIVE / "mgr-dead.json").exists()
    assert not (preflight.QUESTIONS / "q1.json").exists()


def test_main_combined_summary_disambiguates_pruned_names_and_odd_reasons(preflight, monkeypatch, capsys):
    """Pruned names ride in parentheses (the parts list is comma-joined, so a bare
    ': name1, name2' would blur into the next part); odd reasons stay comma-free."""
    state.write_json_atomic(preflight.ACTIVE / "mgr-dead.json", {
        "claude_sid": "sid-dead",
        "agent": "manager",
        "name": "late-yak",
        "pid": 999999,
    })
    state.write_json_atomic(preflight.ACTIVE / "mgr-recycled.json", {
        "claude_sid": "sid-recycled",
        "agent": "manager",
        "name": "brave-griffin",
        "pid": 4242,
    })
    monkeypatch.setattr(preflight, "_pid_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(preflight, "_process_command", lambda pid: "/usr/sbin/distnoted agent")

    preflight.main()

    out = capsys.readouterr().out
    assert "1 stale active record(s) (late-yak)" in out
    assert "; kept 1 odd-looking active record(s), not pruned: brave-griffin (alive but non-session command)" in out


def test_main_reports_kept_odd_records_even_when_nothing_pruned(preflight, monkeypatch, capsys):
    state.write_json_atomic(preflight.ACTIVE / "mgr-recycled.json", {
        "claude_sid": "sid-recycled",
        "agent": "manager",
        "name": "brave-griffin",
        "pid": 4242,
    })
    monkeypatch.setattr(preflight, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(preflight, "_process_command", lambda pid: "/usr/sbin/distnoted agent")

    preflight.main()

    out = capsys.readouterr().out
    assert "odd-looking" in out
    assert "brave-griffin" in out
    assert (preflight.ACTIVE / "mgr-recycled.json").exists()


def _write_old_raw(path: Path, age_sec: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    old = time.time() - age_sec
    import os
    os.utime(path, (old, old))


def test_gc_stale_cursors_pruned(preflight):
    old_age = preflight.STALE_CURSOR_SEC + 60
    _write_old_raw(preflight.ROOT / ".seen-done-feral-yak", old_age)
    _write_old_raw(preflight.ROOT / ".batch-turn-ends-surly-dingo", old_age)
    _write_old_raw(preflight.ROOT / ".last-seen-active", old_age)
    _write_old_raw(preflight.ROOT / ".seen-done-live-manager", 0)

    preflight._gc_husks(time.time())

    assert not (preflight.ROOT / ".seen-done-feral-yak").exists()
    assert not (preflight.ROOT / ".batch-turn-ends-surly-dingo").exists()
    assert not (preflight.ROOT / ".last-seen-active").exists()
    assert (preflight.ROOT / ".seen-done-live-manager").exists()


def test_gc_empty_bucket_dirs_removed(preflight):
    """Per-manager bucket dirs are mkdir'd on demand and never rmdir'd —
    empty ones are debris; non-empty (and the top-level dirs) stay."""
    (preflight.DONE / "dead-manager").mkdir(parents=True)
    (preflight.TURN_ENDS / "dead-manager").mkdir(parents=True)
    (preflight.QUESTIONS / "dead-manager").mkdir(parents=True)
    _write_old(preflight.DONE / "live-manager" / "evt.json", 0)

    preflight._gc_husks(time.time())

    assert not (preflight.DONE / "dead-manager").exists()
    assert not (preflight.TURN_ENDS / "dead-manager").exists()
    assert not (preflight.QUESTIONS / "dead-manager").exists()
    assert (preflight.DONE / "live-manager" / "evt.json").exists()
    assert preflight.DONE.is_dir() and preflight.TURN_ENDS.is_dir() and preflight.QUESTIONS.is_dir()


def test_gc_dead_manager_lock_removed(preflight):
    preflight.MANAGER_LOCK.write_text('{"claude_sid": "$CLAUDE_CODE_SESSION_ID"}')

    preflight._gc_husks(time.time())

    assert not preflight.MANAGER_LOCK.exists()


def test_gc_clean_world_is_noop(preflight):
    assert preflight._gc_husks(time.time()) == 0


def test_gc_prunes_old_fs_ladder_state(preflight):
    old_age = preflight.STALE_CURSOR_SEC + 60
    _write_old_raw(preflight.ROOT / ".fs-emitted-surly-dingo.json", old_age)
    _write_old_raw(preflight.ROOT / ".fs-emitted-fresh-mgr.json", 0)

    preflight._gc_husks(time.time())

    assert not (preflight.ROOT / ".fs-emitted-surly-dingo.json").exists()
    assert (preflight.ROOT / ".fs-emitted-fresh-mgr.json").exists()


def test_gc_prunes_stale_outbox_entries_and_empty_buckets(preflight):
    """Outbox entries are drained by live managers within minutes; anything
    stale belongs to a dead manager and its now-empty bucket dir is debris."""
    old_age = preflight.STALE_CURSOR_SEC + 60
    outbox = preflight.NOTIFY_OUTBOX / "dead-mgr"
    _write_old_raw(outbox / "100-1-0.json", old_age)
    fresh_outbox = preflight.NOTIFY_OUTBOX / "live-mgr"
    _write_old_raw(fresh_outbox / "200-1-0.json", 0)

    preflight._gc_husks(time.time())

    assert not (outbox / "100-1-0.json").exists()
    assert not outbox.exists()            # emptied bucket dir removed
    assert (fresh_outbox / "200-1-0.json").exists()


def test_prune_active_ledgers_spend(preflight, tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "SPEND_LEDGER", tmp_path / "spend-ledger.jsonl")
    state.write_json_atomic(tmp_path / "active" / "dead.json", {
        "claude_sid": "dead", "agent": "worker", "name": "gone", "pid": 1,
        "spend": {"turns": 2, "out_tokens": 10, "in_tokens": 1, "cache_read_tokens": 3},
    })
    monkeypatch.setattr(preflight, "_pid_alive", lambda pid: False)
    pruned, kept = preflight._prune_active()
    assert pruned == ["gone"]
    entry = json.loads((tmp_path / "spend-ledger.jsonl").read_text())
    assert entry["sid"] == "dead" and entry["source"] == "preflight_prune"


def test_prune_closed_ledgers_only_autoclosed_spend(preflight, tmp_path, monkeypatch):
    """session_end-reason closures were ledgered at close; re-appending at the
    7d prune would double-count. Autoclose-reason records were never ledgered."""
    monkeypatch.setattr(preflight, "SPEND_LEDGER", tmp_path / "spend-ledger.jsonl")
    (tmp_path / "closed").mkdir()
    old = time.time() - 8 * 24 * 3600
    state.write_json_atomic(tmp_path / "closed" / "auto.json", {
        "claude_sid": "auto", "name": "idleworker", "closed_at": old,
        "closed_reason": "idle>7200s",
        "spend": {"turns": 4, "out_tokens": 44, "in_tokens": 4, "cache_read_tokens": 4},
    })
    state.write_json_atomic(tmp_path / "closed" / "clean.json", {
        "claude_sid": "clean", "name": "cleanworker", "closed_at": old,
        "closed_reason": "session_end",
        "spend": {"turns": 5, "out_tokens": 55, "in_tokens": 5, "cache_read_tokens": 5},
    })
    assert preflight._prune_closed(time.time()) == 2
    entries = [json.loads(l) for l in (tmp_path / "spend-ledger.jsonl").read_text().splitlines()]
    assert [e["sid"] for e in entries] == ["auto"]
    assert entries[0]["source"] == "closed_prune"


def test_root_prefers_dockwright_home(tmp_path, monkeypatch):
    (tmp_path / ".claude" / "dockwright").mkdir(parents=True)
    (tmp_path / ".claude" / "orchestrator").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    mod = _load_preflight()
    assert mod.ROOT == tmp_path / ".claude" / "dockwright"
    assert mod.ACTIVE == tmp_path / ".claude" / "dockwright" / "active"


def test_root_falls_back_to_legacy_home(tmp_path, monkeypatch):
    (tmp_path / ".claude" / "orchestrator").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    mod = _load_preflight()
    assert mod.ROOT == tmp_path / ".claude" / "orchestrator"
