"""statusline-command.sh manager i/p counts — delegating workers read as processing.

Runs the real script under /bin/sh with HOME pointed at a synthetic tree, so it
exercises the actual jq/find pipeline (no CI here; jq+find exist on the dev Mac).
"""
import json
import os
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "statusline-command.sh"


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def _run_payload(home: Path, payload: dict, extra_env: dict | None = None) -> str:
    # Strip CLAUDE_AGENT so a record-less session is deterministically "regular"
    # regardless of the ambient session running pytest (a worker session would
    # otherwise leak CLAUDE_AGENT=worker and mask the non-agent code path).
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_AGENT"}
    env["HOME"] = str(home)
    # Pop CLAUDE_CONFIG_DIR + CLAUDE_ORCH_ACCOUNT so cfg:default and the usage-tap
    # account derivation are deterministic even if the pytest process itself runs
    # under a `b` worker (which sets both). Tests that need them pass via extra_env,
    # which is applied AFTER this pop.
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.pop("CLAUDE_ORCH_ACCOUNT", None)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _run_statusline(home: Path, session_id: str) -> str:
    return _run_payload(home, {"cwd": str(home), "session_id": session_id})


def test_statusline_counts_delegating_idle_worker_as_processing(tmp_path):
    active = tmp_path / ".claude" / "orchestrator" / "active"
    _write(active / "mgr-1.json", {
        "claude_sid": "mgr-1", "agent": "manager", "name": "boss",
        "domain": "general", "pid": os.getpid(),
    })
    # true idle worker
    _write(active / "w-idle.json", {
        "claude_sid": "w-idle", "agent": "worker", "name": "rest",
        "parent_manager_name": "boss", "pid": os.getpid(), "state": "idle",
    })
    # delegating worker: idle record + stamped transcript_path + subagent file
    # written AFTER the record (find -newer) and fresh (find -mmin -2)
    project_dir = tmp_path / ".claude" / "projects" / "-Users-test"
    log = project_dir / "w-del.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text("")
    _write(active / "w-del.json", {
        "claude_sid": "w-del", "agent": "worker", "name": "deleg",
        "parent_manager_name": "boss", "pid": os.getpid(), "state": "idle",
        "transcript_path": str(log),
    })
    time.sleep(0.05)  # find -newer compares sub-second mtimes; keep a margin
    agent = project_dir / "w-del" / "subagents" / "agent-aaa.jsonl"
    agent.parent.mkdir(parents=True)
    agent.write_text("{}")

    out = _run_statusline(tmp_path, "mgr-1")
    assert "1i / 1p" in out


def test_statusline_counts_unstamped_idle_worker_as_idle(tmp_path):
    active = tmp_path / ".claude" / "orchestrator" / "active"
    _write(active / "mgr-1.json", {
        "claude_sid": "mgr-1", "agent": "manager", "name": "boss",
        "domain": "general", "pid": os.getpid(),
    })
    _write(active / "w-idle.json", {
        "claude_sid": "w-idle", "agent": "worker", "name": "rest",
        "parent_manager_name": "boss", "pid": os.getpid(), "state": "idle",
    })
    out = _run_statusline(tmp_path, "mgr-1")
    assert "1i / 0p" in out


# --- model + effort badge (second line, every session) -----------------------

MODEL = {"id": "claude-opus-4-8[1m]", "display_name": "Opus 4.8 (1M context)"}


def test_statusline_regular_session_shows_model_and_effort(tmp_path):
    out = _run_payload(tmp_path, {
        "cwd": str(tmp_path), "session_id": "plain-1",
        "model": MODEL, "effort": {"level": "xhigh"},
    })
    lines = out.split("\n")
    assert len(lines) >= 2, f"expected a second line, got: {out!r}"
    assert "Opus 4.8 (1M context)" in lines[-1]
    assert "xhigh" in lines[-1]


def test_statusline_manager_model_effort_on_row2(tmp_path):
    # New 3-row manager layout: the worker counter rides row1 (line[0]) and the
    # model/effort badge rides row2 (line[1], with limits + cfg).
    active = tmp_path / ".claude" / "orchestrator" / "active"
    _write(active / "mgr-1.json", {
        "claude_sid": "mgr-1", "agent": "manager", "name": "boss",
        "domain": "general", "pid": os.getpid(),
    })
    out = _run_payload(tmp_path, {
        "cwd": str(tmp_path), "session_id": "mgr-1",
        "model": MODEL, "effort": {"level": "high"},
    })
    lines = out.split("\n")
    assert "0i / 0p" in lines[0]
    assert "Opus 4.8 (1M context)" in lines[1]
    assert "high" in lines[1]


# --- manager 3-row layout ----------------------------------------------------

def test_statusline_manager_three_rows_layout(tmp_path):
    # row1 = role glyph + name · domain identity + worker counter
    # row2 = rate limits + cfg account + model + effort
    # row3 = proposals + todos
    active = tmp_path / ".claude" / "orchestrator" / "active"
    _write(active / "mgr-1.json", {
        "claude_sid": "mgr-1", "agent": "manager", "name": "boss",
        "domain": "general", "pid": os.getpid(),
    })
    # proposals + todos fixtures (script reads $HOME/.claude/gardener/proposals/pending/*.md
    # and $HOME/.claude/todos/*.md).
    proposals_dir = tmp_path / ".claude" / "gardener" / "proposals" / "pending"
    proposals_dir.mkdir(parents=True)
    (proposals_dir / "p1.md").write_text("x")
    todos_dir = tmp_path / ".claude" / "todos"
    todos_dir.mkdir(parents=True)
    (todos_dir / "t1.md").write_text("x")
    (todos_dir / "t2.md").write_text("x")

    out = _run_payload(tmp_path, {
        "cwd": str(tmp_path), "session_id": "mgr-1",
        "model": MODEL, "effort": {"level": "high"},
        "rate_limits": {"five_hour": {"used_percentage": 42}},
    })
    lines = out.split("\n")
    assert len(lines) == 3, f"expected exactly 3 rows, got: {out!r}"
    # row1: role glyph + identity (name · domain) + worker counter
    assert "🎯" in lines[0]
    assert "boss" in lines[0]
    assert "general" in lines[0]
    assert "0i / 0p" in lines[0]
    # row2: rate limits + cfg + model + effort
    assert "5h 42%" in lines[1]
    assert "cfg:default" in lines[1]
    assert "Opus 4.8 (1M context)" in lines[1]
    assert "high" in lines[1]
    # row3: proposals + todos
    assert "1 proposals" in lines[2]
    assert "2 todos" in lines[2]


def test_statusline_manager_three_rows_empty_row3_when_no_proposals_todos(tmp_path):
    # With no proposals/todos, the manager still emits exactly 3 lines (row3 empty).
    active = tmp_path / ".claude" / "orchestrator" / "active"
    _write(active / "mgr-1.json", {
        "claude_sid": "mgr-1", "agent": "manager", "name": "boss",
        "domain": "general", "pid": os.getpid(),
    })
    out = _run_payload(tmp_path, {
        "cwd": str(tmp_path), "session_id": "mgr-1",
        "model": MODEL, "effort": {"level": "high"},
    })
    lines = out.split("\n")
    assert len(lines) == 3, f"expected exactly 3 rows, got: {out!r}"
    assert lines[2] == ""


# --- worker 4-row layout -----------------------------------------------------

def test_statusline_worker_four_rows_layout(tmp_path):
    # New 4-row worker layout, mirroring the manager's row discipline:
    #   row1 = dir + branch only (nothing else crammed on -> long branches never truncate)
    #   row2 = funny_name · task ⟵ parent identity + model + effort
    #   row3 = rate limits + cfg account
    #   row4 = proposals + todos
    active = tmp_path / ".claude" / "orchestrator" / "active"
    _write(active / "w-1.json", {
        "claude_sid": "w-1", "agent": "worker", "name": "moveout-init",
        "funny_name": "zippy-otter", "parent_manager_name": "mighty-demon",
        "pid": os.getpid(), "state": "idle",
    })
    proposals_dir = tmp_path / ".claude" / "gardener" / "proposals" / "pending"
    proposals_dir.mkdir(parents=True)
    (proposals_dir / "p1.md").write_text("x")
    todos_dir = tmp_path / ".claude" / "todos"
    todos_dir.mkdir(parents=True)
    (todos_dir / "t1.md").write_text("x")
    (todos_dir / "t2.md").write_text("x")

    out = _run_payload(tmp_path, {
        "cwd": str(tmp_path), "session_id": "w-1",
        "model": MODEL, "effort": {"level": "high"},
        "rate_limits": {"five_hour": {"used_percentage": 42},
                        "seven_day": {"used_percentage": 88}},
    })
    lines = out.split("\n")
    assert len(lines) == 4, f"expected exactly 4 rows, got: {out!r}"
    # row1: dir only (branch empty — tmp_path is not a git repo); no badges crammed on.
    assert lines[0] == os.path.basename(str(tmp_path)), f"row1 must be dir+branch only: {lines[0]!r}"
    # row2: identity (funny_name · task ⟵ parent) + model + effort
    assert "zippy-otter" in lines[1]
    assert "moveout-init" in lines[1]
    assert "mighty-demon" in lines[1]
    assert "Opus 4.8 (1M context)" in lines[1]
    assert "high" in lines[1]
    # row3: rate limits + cfg account
    assert "5h 42%" in lines[2]
    assert "7d 88%" in lines[2]
    assert "cfg:default" in lines[2]
    # row4: proposals + todos
    assert "1 proposals" in lines[3]
    assert "2 todos" in lines[3]


def test_statusline_worker_four_rows_empty_row4_when_no_proposals_todos(tmp_path):
    # With no proposals/todos, the worker still emits exactly 4 lines (row4 empty),
    # matching the manager's empty-row3 behavior.
    active = tmp_path / ".claude" / "orchestrator" / "active"
    _write(active / "w-1.json", {
        "claude_sid": "w-1", "agent": "worker", "name": "task-x",
        "funny_name": "zippy", "parent_manager_name": "boss",
        "pid": os.getpid(), "state": "idle",
    })
    out = _run_payload(tmp_path, {
        "cwd": str(tmp_path), "session_id": "w-1",
        "model": MODEL, "effort": {"level": "high"},
    })
    lines = out.split("\n")
    assert len(lines) == 4, f"expected exactly 4 rows, got: {out!r}"
    assert lines[3] == ""


def test_statusline_worker_shows_model_effort(tmp_path):
    active = tmp_path / ".claude" / "orchestrator" / "active"
    _write(active / "w-1.json", {
        "claude_sid": "w-1", "agent": "worker", "name": "task-x",
        "funny_name": "zippy", "parent_manager_name": "boss",
        "pid": os.getpid(), "state": "idle",
    })
    out = _run_payload(tmp_path, {
        "cwd": str(tmp_path), "session_id": "w-1",
        "model": MODEL, "effort": {"level": "medium"},
    })
    lines = out.split("\n")
    # New 4-row worker layout: model + effort ride the identity row (row2), not
    # the last line (row4 = proposals + todos, empty here).
    assert len(lines) == 4, f"worker emits 4 rows, got: {out!r}"
    assert "Opus 4.8 (1M context)" in lines[1]
    assert "medium" in lines[1]
    assert "zippy" in lines[1]


def test_statusline_omits_effort_when_absent(tmp_path):
    out = _run_payload(tmp_path, {
        "cwd": str(tmp_path), "session_id": "plain-2",
        "model": MODEL,
    })
    last = out.split("\n")[-1]
    assert "Opus 4.8 (1M context)" in last
    assert "·" not in last  # no dangling separator when effort is absent


def test_statusline_falls_back_to_model_id_when_no_display_name(tmp_path):
    out = _run_payload(tmp_path, {
        "cwd": str(tmp_path), "session_id": "plain-3",
        "model": {"id": "claude-opus-4-8[1m]"}, "effort": {"level": "high"},
    })
    assert "claude-opus-4-8[1m]" in out.split("\n")[-1]


def test_statusline_no_model_key_keeps_single_line(tmp_path):
    # Legacy / model-absent payloads must not gain a model badge or extra content
    # line (the script's pre-existing trailing newline is unchanged and benign).
    out = _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "plain-4"})
    assert "◆" not in out
    assert out.rstrip("\n").count("\n") == 0


def test_statusline_exits_zero_on_model_absent_regular_session(tmp_path):
    # A statusline must NEVER exit non-zero, or Claude Code blanks the whole line.
    # The non-agent branches must guarantee exit 0 even when model_effort is empty
    # (a trailing `[ -n "$model_effort" ]` test would otherwise exit 1).
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_AGENT"}
    env["HOME"] = str(tmp_path)
    result = subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        input=json.dumps({"cwd": str(tmp_path), "session_id": "plain-x"}),
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode == 0, f"exited {result.returncode}: {result.stderr}"
    assert result.stdout.strip() != ""  # the line still renders


# --- cfg:<account> segment (every session, row1) -----------------------------

def test_cfg_segment_account_b(tmp_path):
    out = _run_payload(
        tmp_path, {"cwd": str(tmp_path), "session_id": "s"},
        extra_env={"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude-b")},
    )
    assert "cfg:b" in out


def test_cfg_segment_default_when_unset(tmp_path):
    out = _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "s"})
    assert "cfg:default" in out


def test_cfg_segment_default_when_canonical(tmp_path):
    out = _run_payload(
        tmp_path, {"cwd": str(tmp_path), "session_id": "s"},
        extra_env={"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude")},
    )
    assert "cfg:default" in out


def test_cfg_segment_on_manager_row2(tmp_path):
    """New 3-row manager layout: the cfg badge rides row2 (limits + account +
    model + effort), and the worker counter rides row1."""
    active = tmp_path / ".claude" / "orchestrator" / "active"
    _write(active / "mgr-1.json", {
        "claude_sid": "mgr-1", "agent": "manager", "name": "boss",
        "domain": "general", "pid": os.getpid(),
    })
    out = _run_payload(
        tmp_path, {"cwd": str(tmp_path), "session_id": "mgr-1"},
        extra_env={"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude-b")},
    )
    lines = out.split("\n")
    assert "cfg:b" in lines[1], "cfg badge rides the manager row2"
    assert "0i / 0p" in lines[0], "manager i/p worker counter rides row1"


def test_cfg_segment_on_worker_row3(tmp_path):
    """New 4-row worker layout: the cfg badge rides row3 (rate limits + account),
    and the identity row (row2, funny_name · task ⟵ parent + model) is unaffected."""
    active = tmp_path / ".claude" / "orchestrator" / "active"
    _write(active / "w-1.json", {
        "claude_sid": "w-1", "agent": "worker", "name": "task-x",
        "funny_name": "zippy", "parent_manager_name": "boss",
        "pid": os.getpid(), "state": "idle",
    })
    out = _run_payload(
        tmp_path, {"cwd": str(tmp_path), "session_id": "w-1"},
        extra_env={"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude-b")},
    )
    lines = out.split("\n")
    assert "cfg:b" in lines[2], "cfg badge rides the worker row3 (limits + account)"
    assert "zippy" in lines[1], "worker identity row unaffected by the cfg badge"


RL = {"five_hour": {"used_percentage": 23.5, "resets_at": "2026-06-15T12:00:00Z"},
      "seven_day": {"used_percentage": 11.0, "resets_at": 1781600000}}


def _usage_file(home, letter):
    return home / ".claude" / "dockwright" / "usage" / f"{letter}.json"


def test_usage_written_for_orch_account_b(tmp_path):
    _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "s", "rate_limits": RL},
                 extra_env={"CLAUDE_ORCH_ACCOUNT": "b"})
    f = _usage_file(tmp_path, "b")
    assert f.exists()
    rec = json.loads(f.read_text())
    assert rec["five_hour_pct"] == 23.5
    assert rec["seven_day_pct"] == 11.0
    assert rec["five_hour_resets_at"] == "2026-06-15T12:00:00Z"
    assert rec["seven_day_resets_at"] == 1781600000
    assert isinstance(rec["ts"], (int, float))


def test_usage_account_derived_from_config_dir(tmp_path):
    _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "s", "rate_limits": RL},
                 extra_env={"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude-b")})
    assert _usage_file(tmp_path, "b").exists()
    assert not _usage_file(tmp_path, "a").exists()


def test_usage_host_defaults_to_a(tmp_path):
    # neither CLAUDE_ORCH_ACCOUNT nor CLAUDE_CONFIG_DIR (the _run_payload harness pops it)
    _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "s", "rate_limits": RL})
    assert _usage_file(tmp_path, "a").exists()


def test_usage_not_written_without_rate_limits(tmp_path):
    _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "s"},
                 extra_env={"CLAUDE_ORCH_ACCOUNT": "b"})
    assert not _usage_file(tmp_path, "b").exists()


def test_usage_write_failure_keeps_exit_zero(tmp_path):
    # Make the (new, preferred) usage dir's parent a FILE so mkdir -p fails →
    # swallowed, still exit 0.
    dockwright = tmp_path / ".claude" / "dockwright"
    dockwright.parent.mkdir(parents=True, exist_ok=True)
    dockwright.write_text("i am a file, not a dir")
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_AGENT"}
    env["HOME"] = str(tmp_path); env["CLAUDE_ORCH_ACCOUNT"] = "a"
    env.pop("CLAUDE_CONFIG_DIR", None)
    result = subprocess.run(["/bin/sh", str(SCRIPT)],
                            input=json.dumps({"cwd": str(tmp_path), "session_id": "s",
                                              "rate_limits": RL}),
                            capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 0
    assert result.stdout.strip() != ""


# --- dockwright home preference (dockwright-rename, one release) --------------
# Statusline renders under BOTH old and new deployments: each state dir prefers
# the dockwright home, falling back to the legacy orchestrator/ path.

def test_statusline_prefers_dockwright_active_over_legacy(tmp_path):
    # Same session id present in both homes; the dockwright record must win.
    _write(tmp_path / ".claude" / "dockwright" / "active" / "mgr-1.json", {
        "claude_sid": "mgr-1", "agent": "manager", "name": "newboss",
        "domain": "general", "pid": os.getpid(),
    })
    _write(tmp_path / ".claude" / "orchestrator" / "active" / "mgr-1.json", {
        "claude_sid": "mgr-1", "agent": "manager", "name": "oldboss",
        "domain": "general", "pid": os.getpid(),
    })
    out = _run_statusline(tmp_path, "mgr-1")
    assert "newboss" in out and "oldboss" not in out


def test_statusline_prefers_dockwright_proposals_over_legacy(tmp_path):
    active = tmp_path / ".claude" / "dockwright" / "active"
    _write(active / "mgr-1.json", {
        "claude_sid": "mgr-1", "agent": "manager", "name": "boss",
        "domain": "general", "pid": os.getpid(),
    })
    new_pending = tmp_path / ".claude" / "dockwright" / "gardener" / "proposals" / "pending"
    new_pending.mkdir(parents=True)
    (new_pending / "p1.md").write_text("x")
    (new_pending / "p2.md").write_text("x")
    # A legacy proposal that must be ignored once the dockwright home exists.
    legacy_pending = tmp_path / ".claude" / "gardener" / "proposals" / "pending"
    legacy_pending.mkdir(parents=True)
    (legacy_pending / "stale.md").write_text("x")
    out = _run_payload(tmp_path, {
        "cwd": str(tmp_path), "session_id": "mgr-1",
        "model": MODEL, "effort": {"level": "high"},
    })
    assert "2 proposals" in out.split("\n")[2]


def _dockwright_usage_file(home, letter):
    return home / ".claude" / "dockwright" / "usage" / f"{letter}.json"


def test_usage_written_to_dockwright_home_when_present(tmp_path):
    # With the dockwright usage home already present, the cache writes there.
    (tmp_path / ".claude" / "dockwright" / "usage").mkdir(parents=True)
    _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "s", "rate_limits": RL},
                 extra_env={"CLAUDE_ORCH_ACCOUNT": "b"})
    f = _dockwright_usage_file(tmp_path, "b")
    assert f.exists()
    assert json.loads(f.read_text())["five_hour_pct"] == 23.5


def test_usage_written_to_legacy_home_when_unmigrated(tmp_path):
    # An un-migrated install: the legacy usage dir already exists and the
    # dockwright home does not — the write must land in the legacy dir, NOT
    # create a fresh (unread) dockwright/usage home.
    (tmp_path / ".claude" / "orchestrator" / "usage").mkdir(parents=True)
    _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "s", "rate_limits": RL},
                 extra_env={"CLAUDE_ORCH_ACCOUNT": "b"})
    legacy_f = tmp_path / ".claude" / "orchestrator" / "usage" / "b.json"
    assert legacy_f.exists()
    assert json.loads(legacy_f.read_text())["five_hour_pct"] == 23.5
    assert not _dockwright_usage_file(tmp_path, "b").exists()
