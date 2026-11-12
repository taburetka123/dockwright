import json
import os
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "statusline-command.sh"


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def _run_payload(home: Path, payload: dict, extra_env: dict | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_AGENT"}
    env["HOME"] = str(home)
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
    assert "boss" in lines[0]
    assert "Opus 4.8 (1M context)" in lines[1]
    assert "high" in lines[1]


def test_statusline_manager_two_rows_layout(tmp_path):
    active = tmp_path / ".claude" / "orchestrator" / "active"
    _write(active / "mgr-1.json", {
        "claude_sid": "mgr-1", "agent": "manager", "name": "boss",
        "domain": "general", "pid": os.getpid(),
    })
    _write(active / "w-idle.json", {
        "claude_sid": "w-idle", "agent": "worker", "name": "rest",
        "parent_manager_name": "boss", "pid": os.getpid(), "state": "idle",
    })
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
    assert len(lines) == 2, f"expected exactly 2 rows, got: {out!r}"
    assert "🎯" in lines[0]
    assert "boss" in lines[0]
    assert "general" in lines[0]
    assert lines[0].index("general") < lines[0].index("1 proposals") < lines[0].index("2 todos")
    assert "🤖" not in out
    assert "i / " not in out
    assert "5h 42%" in lines[1]
    assert "cfg:default" in lines[1]
    assert "Opus 4.8 (1M context)" in lines[1]
    assert "high" in lines[1]


def test_statusline_manager_two_rows_without_proposals_or_todos(tmp_path):
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
    assert len(lines) == 2, f"expected exactly 2 rows, got: {out!r}"
    assert "boss" in lines[0]
    assert "proposals" not in out and "todos" not in out
    assert "🤖" not in out
    assert "Opus 4.8 (1M context)" in lines[1]


def test_statusline_worker_four_rows_layout(tmp_path):
    active = tmp_path / ".claude" / "orchestrator" / "active"
    _write(active / "w-1.json", {
        "claude_sid": "w-1", "agent": "worker", "name": "task-init",
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
    assert lines[0] == os.path.basename(str(tmp_path)), f"row1 must be dir+branch only: {lines[0]!r}"
    assert "zippy-otter" in lines[1]
    assert "task-init" in lines[1]
    assert "mighty-demon" in lines[1]
    assert "Opus 4.8 (1M context)" in lines[1]
    assert "high" in lines[1]
    assert "5h 42%" in lines[2]
    assert "7d 88%" in lines[2]
    assert "cfg:default" in lines[2]
    assert "1 proposals" in lines[3]
    assert "2 todos" in lines[3]


def test_statusline_worker_four_rows_empty_row4_when_no_proposals_todos(tmp_path):
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
    assert "·" not in last


def test_statusline_falls_back_to_model_id_when_no_display_name(tmp_path):
    out = _run_payload(tmp_path, {
        "cwd": str(tmp_path), "session_id": "plain-3",
        "model": {"id": "claude-opus-4-8[1m]"}, "effort": {"level": "high"},
    })
    assert "claude-opus-4-8[1m]" in out.split("\n")[-1]


def test_statusline_no_model_key_keeps_single_line(tmp_path):
    out = _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "plain-4"})
    assert "◆" not in out
    assert out.rstrip("\n").count("\n") == 0


def test_statusline_exits_zero_on_model_absent_regular_session(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_AGENT"}
    env["HOME"] = str(tmp_path)
    result = subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        input=json.dumps({"cwd": str(tmp_path), "session_id": "plain-x"}),
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode == 0, f"exited {result.returncode}: {result.stderr}"
    assert result.stdout.strip() != ""


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
    assert "boss" in lines[0], "manager identity rides row1"


def test_cfg_segment_on_worker_row3(tmp_path):
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
    _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "s", "rate_limits": RL})
    assert _usage_file(tmp_path, "a").exists()


def test_usage_not_written_without_rate_limits(tmp_path):
    _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "s"},
                 extra_env={"CLAUDE_ORCH_ACCOUNT": "b"})
    assert not _usage_file(tmp_path, "b").exists()


def test_usage_write_failure_keeps_exit_zero(tmp_path):
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


def _reset_str(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.283332+00:00")


def _snapshot(fetched_at_ms: int, rows: list, account_uuid: str = "acct-1",
              snapshot_uuid: str | None = None) -> dict:
    limits = [{"kind": "session", "percent": 12, "resets_at": None},
              {"kind": "weekly_all", "percent": 58, "resets_at": None}] + rows
    return {"oauthAccount": {"accountUuid": account_uuid},
            "cachedUsageUtilization": {"fetchedAtMs": fetched_at_ms,
                                       "accountUuid": snapshot_uuid or account_uuid,
                                       "utilization": {"limits": limits}}}


def _fable_row(pct: float, resets_at) -> dict:
    return {"kind": "weekly_scoped", "group": "weekly", "percent": pct, "severity": "normal",
            "resets_at": resets_at,
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
            "is_active": True}


def _write_cfg(cfg_dir: Path, data) -> None:
    cfg_dir.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data)
    (cfg_dir / ".claude.json").write_text(text)


def _rl_payload(home: Path) -> dict:
    return {"cwd": str(home), "session_id": "s", "rate_limits": RL}


def test_model_scoped_badge_and_record_from_the_default_config_snapshot(tmp_path):
    now = time.time()
    resets = now + 6 * 86400
    fetched_ms = int(now * 1000)
    _write_cfg(tmp_path, _snapshot(fetched_ms, [_fable_row(64.4, _reset_str(resets))]))
    out = _run_payload(tmp_path, _rl_payload(tmp_path))
    assert "Fable 64%" in out
    assert "(" not in out.split("Fable 64%")[1].split("\033")[0]
    rec = json.loads(_usage_file(tmp_path, "a").read_text())
    assert rec["model_scoped"] == [{"name": "Fable", "pct": 64, "resets_at": int(resets)}]
    assert rec["model_scoped_at"] == fetched_ms // 1000
    assert rec["five_hour_pct"] == 23.5
    assert rec["seven_day_pct"] == 11.0
    assert rec["five_hour_resets_at"] == "2026-06-15T12:00:00Z"
    assert rec["seven_day_resets_at"] == 1781600000
    assert isinstance(rec["ts"], (int, float))


def test_model_scoped_read_from_claude_config_dir(tmp_path):
    now = time.time()
    _write_cfg(tmp_path / ".claude-b",
               _snapshot(int(now * 1000), [_fable_row(64, _reset_str(now + 6 * 86400))]))
    assert not (tmp_path / ".claude.json").exists()
    out = _run_payload(tmp_path, _rl_payload(tmp_path),
                       extra_env={"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude-b")})
    assert "Fable 64%" in out
    rec = json.loads(_usage_file(tmp_path, "b").read_text())
    assert [r["name"] for r in rec["model_scoped"]] == ["Fable"]
    assert not _usage_file(tmp_path, "a").exists()


def test_model_scoped_empty_when_the_config_file_is_missing(tmp_path):
    out = _run_payload(tmp_path, _rl_payload(tmp_path))
    assert "Fable" not in out
    assert "5h " in out and "7d " in out
    rec = json.loads(_usage_file(tmp_path, "a").read_text())
    assert rec["model_scoped"] == []
    assert rec["model_scoped_at"] is None
    assert rec["five_hour_pct"] == 23.5
    assert rec["seven_day_pct"] == 11.0


def test_model_scoped_empty_when_the_config_is_not_json(tmp_path):
    _write_cfg(tmp_path, "{ this is not json")
    out = _run_payload(tmp_path, _rl_payload(tmp_path))
    assert "Fable" not in out
    rec = json.loads(_usage_file(tmp_path, "a").read_text())
    assert rec["model_scoped"] == []
    assert rec["model_scoped_at"] is None
    assert rec["five_hour_pct"] == 23.5


def test_model_scoped_badge_hidden_after_reset_but_row_kept(tmp_path):
    now = time.time()
    resets = now - 60
    _write_cfg(tmp_path, _snapshot(int(now * 1000), [_fable_row(64, _reset_str(resets))]))
    out = _run_payload(tmp_path, _rl_payload(tmp_path))
    assert "Fable" not in out
    rec = json.loads(_usage_file(tmp_path, "a").read_text())
    assert rec["model_scoped"] == [{"name": "Fable", "pct": 64, "resets_at": int(resets)}]


@pytest.mark.parametrize("age_s,suffix", [(3 * 3600, "(3h)"), (2 * 86400, "(2d)")])
def test_model_scoped_badge_shows_the_snapshot_age(tmp_path, age_s, suffix):
    now = time.time()
    _write_cfg(tmp_path, _snapshot(int((now - age_s) * 1000),
                                   [_fable_row(64, _reset_str(now + 6 * 86400))]))
    out = _run_payload(tmp_path, _rl_payload(tmp_path))
    assert f"Fable 64% {suffix}" in out


def test_model_scoped_badge_keeps_the_manager_and_worker_row_counts(tmp_path):
    now = time.time()
    _write_cfg(tmp_path, _snapshot(int(now * 1000), [_fable_row(64, _reset_str(now + 6 * 86400))]))
    active = tmp_path / ".claude" / "orchestrator" / "active"
    _write(active / "mgr-1.json", {
        "claude_sid": "mgr-1", "agent": "manager", "name": "boss",
        "domain": "general", "pid": os.getpid(),
    })
    out = _run_payload(tmp_path, {
        "cwd": str(tmp_path), "session_id": "mgr-1",
        "model": MODEL, "effort": {"level": "high"}, "rate_limits": RL,
    })
    lines = out.split("\n")
    assert len(lines) == 2, f"expected exactly 2 rows, got: {out!r}"
    assert "7d 11%" in lines[1] and "Fable 64%" in lines[1]
    assert lines[1].index("7d 11%") < lines[1].index("Fable 64%") < lines[1].index("cfg:default")

    _write(active / "w-1.json", {
        "claude_sid": "w-1", "agent": "worker", "name": "task-init",
        "funny_name": "zippy-otter", "parent_manager_name": "mighty-demon",
        "pid": os.getpid(), "state": "idle",
    })
    out = _run_payload(tmp_path, {
        "cwd": str(tmp_path), "session_id": "w-1",
        "model": MODEL, "effort": {"level": "high"}, "rate_limits": RL,
    })
    lines = out.split("\n")
    assert len(lines) == 4, f"expected exactly 4 rows, got: {out!r}"
    assert "Fable 64%" in lines[2]


def test_model_scoped_empty_when_the_snapshot_belongs_to_another_account(tmp_path):
    now = time.time()
    rows = [_fable_row(64, _reset_str(now + 6 * 86400))]
    _write_cfg(tmp_path, _snapshot(int(now * 1000), rows, account_uuid="acct-b", snapshot_uuid="acct-a"))
    out = _run_payload(tmp_path, _rl_payload(tmp_path))
    assert "Fable" not in out
    rec = json.loads(_usage_file(tmp_path, "a").read_text())
    assert rec["model_scoped"] == []
    assert rec["model_scoped_at"] is None

    data = _snapshot(int(now * 1000), rows)
    del data["oauthAccount"]
    _write_cfg(tmp_path, data)
    out = _run_payload(tmp_path, _rl_payload(tmp_path))
    assert "Fable" not in out
    rec = json.loads(_usage_file(tmp_path, "a").read_text())
    assert rec["model_scoped"] == []


@pytest.mark.parametrize("text", ["{}\n{}", json.dumps({
    "oauthAccount": {"accountUuid": "acct-1"},
    "cachedUsageUtilization": {"fetchedAtMs": 1791472939405, "accountUuid": "acct-1",
                               "utilization": {"limits": {"kind": "weekly_scoped"}}}})])
def test_model_scoped_empty_on_two_json_values_or_a_limits_object(tmp_path, text):
    _write_cfg(tmp_path, text)
    out = _run_payload(tmp_path, _rl_payload(tmp_path))
    assert "Fable" not in out
    rec = json.loads(_usage_file(tmp_path, "a").read_text())
    assert rec["model_scoped"] == []
    assert rec["model_scoped_at"] is None
    assert rec["five_hour_pct"] == 23.5
    assert rec["seven_day_pct"] == 11.0


def test_statusline_prefers_dockwright_active_over_legacy(tmp_path):
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
    legacy_pending = tmp_path / ".claude" / "gardener" / "proposals" / "pending"
    legacy_pending.mkdir(parents=True)
    (legacy_pending / "stale.md").write_text("x")
    out = _run_payload(tmp_path, {
        "cwd": str(tmp_path), "session_id": "mgr-1",
        "model": MODEL, "effort": {"level": "high"},
    })
    assert "2 proposals" in out.split("\n")[0]


def _dockwright_usage_file(home, letter):
    return home / ".claude" / "dockwright" / "usage" / f"{letter}.json"


def test_usage_written_to_dockwright_home_when_present(tmp_path):
    (tmp_path / ".claude" / "dockwright" / "usage").mkdir(parents=True)
    _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "s", "rate_limits": RL},
                 extra_env={"CLAUDE_ORCH_ACCOUNT": "b"})
    f = _dockwright_usage_file(tmp_path, "b")
    assert f.exists()
    assert json.loads(f.read_text())["five_hour_pct"] == 23.5


def test_usage_written_to_legacy_home_when_unmigrated(tmp_path):
    (tmp_path / ".claude" / "orchestrator" / "usage").mkdir(parents=True)
    _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "s", "rate_limits": RL},
                 extra_env={"CLAUDE_ORCH_ACCOUNT": "b"})
    legacy_f = tmp_path / ".claude" / "orchestrator" / "usage" / "b.json"
    assert legacy_f.exists()
    assert json.loads(legacy_f.read_text())["five_hour_pct"] == 23.5
    assert not _dockwright_usage_file(tmp_path, "b").exists()


def test_statusline_worker_label_reads_parent_name_only(tmp_path):
    active = tmp_path / ".claude" / "orchestrator" / "active"
    _write(active / "w-1.json", {
        "claude_sid": "w-1", "agent": "worker", "name": "alpha", "funny_name": "grumpy-yak",
        "parent_name": "boss", "pid": os.getpid(), "state": "idle",
    })
    out = _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "w-1"}, {"CLAUDE_AGENT": "worker"})
    assert "boss" in out


GREEN, YELLOW, RED = "\x1b[38;5;82m", "\x1b[38;5;220m", "\x1b[38;5;196m"


def _ctx(tokens, window, pct):
    return {"total_input_tokens": tokens, "context_window_size": window,
            "used_percentage": pct, "remaining_percentage": 100 - pct,
            "current_usage": {"input_tokens": tokens, "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0, "output_tokens": 10}}


def test_ctx_badge_renders_a_1m_window(tmp_path):
    out = _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "s",
                                  "context_window": _ctx(370000, 1000000, 37)})
    assert "ctx 37% 370k/1M" in out


def test_ctx_badge_rounds_the_token_count_rather_than_truncating(tmp_path):
    out = _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "s",
                                  "context_window": _ctx(312500, 1000000, 31)})
    assert "ctx 31% 313k/1M" in out, "312500 must round to 313k, not truncate to 312k"


def test_ctx_badge_derives_the_200k_suffix(tmp_path):
    out = _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "s",
                                  "context_window": _ctx(74000, 200000, 37)})
    assert "ctx 37% 74k/200k" in out
    assert "1M" not in out


def test_ctx_badge_prints_a_window_that_is_neither_a_round_m_nor_k_verbatim(tmp_path):
    out = _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "s",
                                  "context_window": _ctx(65536, 131072, 50)})
    assert "ctx 50% 66k/131072" in out


def test_ctx_badge_reports_used_above_the_window_without_clamping(tmp_path):
    out = _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "s",
                                  "context_window": _ctx(966000, 200000, 100)})
    assert "ctx 100% 966k/200k" in out, (
        "a credit latch can shrink the window under a live session; the badge reports it")


@pytest.mark.parametrize("pct,colour", [(59, GREEN), (60, YELLOW), (85, YELLOW), (86, RED)])
def test_rate_badge_threshold_boundaries_survive_the_pct_color_extraction(tmp_path, pct, colour):
    out = _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "s",
                                  "rate_limits": {"five_hour": {"used_percentage": pct}}})
    assert f"{colour}· 5h {pct}%" in out


@pytest.mark.parametrize("pct,colour", [(37, GREEN), (80, YELLOW), (95, RED)])
def test_ctx_badge_takes_its_colour_from_the_same_scale(tmp_path, pct, colour):
    out = _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "s",
                                  "context_window": _ctx(pct * 10000, 1000000, pct)})
    assert f"{colour}· ctx {pct}%" in out


def test_ctx_badge_silent_when_used_percentage_is_null(tmp_path):
    payload = {"cwd": str(tmp_path), "session_id": "s",
               "context_window": {"total_input_tokens": 0, "context_window_size": 1000000,
                                  "used_percentage": None, "remaining_percentage": None}}
    out = _run_payload(tmp_path, payload)
    assert "· ctx " not in out
    assert out.strip() != ""


def test_ctx_badge_silent_when_the_payload_has_no_context_window(tmp_path):
    out = _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "s"})
    assert "· ctx " not in out
    assert out.strip() != ""


def test_ctx_badge_rides_manager_row2_without_changing_the_row_count(tmp_path):
    active = tmp_path / ".claude" / "dockwright" / "active"
    _write(active / "mgr-1.json", {
        "claude_sid": "mgr-1", "agent": "manager", "name": "boss",
        "domain": "general", "pid": os.getpid(),
    })
    out = _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "mgr-1",
                                  "context_window": _ctx(370000, 1000000, 37)})
    lines = out.split("\n")
    assert len(lines) == 2, f"expected exactly 2 rows, got: {out!r}"
    assert "ctx 37% 370k/1M" in lines[1]


def test_ctx_badge_rides_worker_row3_without_changing_the_row_count(tmp_path):
    active = tmp_path / ".claude" / "dockwright" / "active"
    _write(active / "w-1.json", {
        "claude_sid": "w-1", "agent": "worker", "name": "task-x", "funny_name": "zippy",
        "parent_name": "boss", "pid": os.getpid(), "state": "idle",
    })
    out = _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "w-1",
                                  "context_window": _ctx(370000, 1000000, 37)})
    lines = out.split("\n")
    assert len(lines) == 4, f"expected exactly 4 rows, got: {out!r}"
    assert "ctx 37% 370k/1M" in lines[2]


def test_ctx_badge_rides_row1_of_a_plain_session_that_has_a_git_branch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["git", "init", "-q", "-b", "trunk"],
                ["git", "config", "user.email", "t@t"], ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    (repo / "f").write_text("x")
    subprocess.run(["git", "add", "f"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=repo, check=True, capture_output=True)

    out = _run_payload(tmp_path, {"cwd": str(repo), "session_id": "s",
                                  "context_window": _ctx(370000, 1000000, 37)})
    assert "trunk" in out.split("\n")[0]
    assert "ctx 37% 370k/1M" in out.split("\n")[0]


def test_rate_badges_and_usage_tap_unaffected_by_a_payload_with_no_context_window(tmp_path):
    out = _run_payload(tmp_path, {"cwd": str(tmp_path), "session_id": "s",
                                  "rate_limits": {"five_hour": {"used_percentage": 42},
                                                  "seven_day": {"used_percentage": 88}}})
    assert "5h 42%" in out and "7d 88%" in out
    assert "· ctx " not in out
    rec = json.loads(_usage_file(tmp_path, "a").read_text())
    assert rec["five_hour_pct"] == 42 and rec["seven_day_pct"] == 88
