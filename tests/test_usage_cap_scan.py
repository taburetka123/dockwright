import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

from dockwright import config, mcp_server, paths

REPO_ROOT = Path(__file__).resolve().parents[1]
STALE_MONITOR_PATH = REPO_ROOT / "src" / "dockwright" / "stale_monitor.py"
KEY = "usage-cap:fleet"
FILE_KEY = "usage-cap:file"


def _load_stale_monitor():
    spec = importlib.util.spec_from_file_location("stale_monitor_usage_cap", STALE_MONITOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def sm(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DOCKWRIGHT_STATE_DIR", raising=False)
    mod = _load_stale_monitor()
    for name in ("active", "closed", "questions", "usage", "assignments"):
        (tmp_path / name).mkdir()
    for name, value in (("ROOT", tmp_path), ("ACTIVE", tmp_path / "active"), ("CLOSED", tmp_path / "closed"),
                        ("QUESTIONS", tmp_path / "questions"), ("CLAUDE_PROJECTS", tmp_path / "projects"),
                        ("CODEX_SESSIONS", tmp_path / "codex-sessions"), ("ASSIGNMENTS", tmp_path / "assignments")):
        monkeypatch.setattr(mod, name, value)
    monkeypatch.setattr(paths, "USAGE_CAPS", tmp_path / "usage-caps.toml")
    monkeypatch.setattr(paths, "ACCOUNT_USAGE", tmp_path / "usage")
    monkeypatch.setattr(config, "account_names", lambda: ("a", "b"))
    return mod


@pytest.fixture
def windows(sm, monkeypatch):
    closed = []
    monkeypatch.setattr(sm, "_close_window", closed.append)
    return closed


def _breach(root, text="[b]\nfive_hour = 70\n", reset_scale=1):
    now = time.time()
    (root / "usage-caps.toml").write_text(text)
    (root / "usage" / "b.json").write_text(json.dumps(
        {"five_hour_pct": 71, "five_hour_resets_at": (now + 3600) * reset_scale, "ts": now}))


def _record(root, sid, **fields):
    record = {"claude_sid": sid, "name": f"w-{sid}", "agent": "worker", "window_id": f"%{sid}",
              "transcript_path": f"/nonexistent/{sid}.jsonl", "pid": os.getpid(), **fields}
    (root / "active" / f"{sid}.json").write_text(json.dumps(record))


def _assignment(root, sid, **fields):
    (root / "assignments" / f"{sid}.json").write_text(json.dumps({"claude_sid": sid, **fields}))


def _share_the_state_root(root, monkeypatch):
    for name, value in (("CLOSED", root / "closed"), ("ASSIGNMENTS", root / "assignments"),
                        ("ASSIGNMENTS_PENDING", root / "assignments" / ".pending")):
        monkeypatch.setattr(paths, name, value)


def _scan(sm, emitted, now=None, manager_name=None):
    events, next_emitted = [], {}
    sm._scan_usage_cap(now or int(time.time()), emitted, next_emitted,
                       lambda kind, name, lane_kind, fields, key=None, sid=None:
                       events.append((lane_kind, sm._render_line(lane_kind, fields), key)), manager_name)
    return events, next_emitted


def _active(root):
    return sorted(p.stem for p in (root / "active").iterdir())


def _closed_reason(root, sid):
    return json.loads((root / "closed" / f"{sid}.json").read_text())["closed_reason"]


def test_a_breach_closes_every_worker_in_the_fleet_and_pages_once(sm, windows, tmp_path):
    _breach(tmp_path)
    _record(tmp_path, "1")
    _record(tmp_path, "2", parent_manager_name="gone-manager")
    _record(tmp_path, "3", human_attached_at=time.time())
    _record(tmp_path, "4", runtime="codex", provider="codex-exec", kind="subworker")
    _record(tmp_path, "m", agent="manager", name="mgr-A")
    _record(tmp_path, "n", nested=True)
    events, state = _scan(sm, {})
    assert _active(tmp_path) == ["m", "n"]
    assert [_closed_reason(tmp_path, sid) for sid in ("1", "2", "3", "4")] == ["usage-cap"] * 4
    assert sorted(windows) == ["%1", "%2", "%3", "%4"]
    assert [(kind, key) for kind, _line, key in events] == [("USAGE_CAP", KEY)]
    assert events[0][1].startswith("USAGE_CAP b 5h 71% ≥ 70% → tell the engineer the fleet is paused until ")
    assert state[KEY]["state"] == "paused" and state[KEY]["paged"]


def test_a_later_tick_of_the_same_pause_closes_newcomers_and_stays_silent(sm, windows, tmp_path):
    _breach(tmp_path)
    _, state = _scan(sm, {})
    _record(tmp_path, "4")
    events, next_state = _scan(sm, state)
    assert events == [] and _active(tmp_path) == []
    assert next_state[KEY] == state[KEY]


def test_a_millisecond_reset_closes_the_fleet_and_pages_until_its_window_resets(sm, windows, tmp_path):
    _breach(tmp_path, reset_scale=1000)
    _record(tmp_path, "1")
    events, state = _scan(sm, {})
    assert _active(tmp_path) == [] and _closed_reason(tmp_path, "1") == "usage-cap"
    assert [(kind, key) for kind, _line, key in events] == [("USAGE_CAP", KEY)]
    assert "the fleet is paused until its window resets;" in events[0][1]
    assert state[KEY]["state"] == "paused" and state[KEY]["paged"]


def test_a_page_that_fails_to_render_closes_the_fleet_and_records_no_page(sm, windows, tmp_path, monkeypatch):
    render, calls = sm._usage_cap.page_fields, []

    def fail_once(verdict, now):
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("render failed")
        return render(verdict, now)

    monkeypatch.setattr(sm._usage_cap, "page_fields", fail_once)
    _breach(tmp_path)
    _record(tmp_path, "1")
    next_emitted = {}
    with pytest.raises(RuntimeError, match="render failed"):
        sm._scan_usage_cap(int(time.time()), {}, next_emitted, lambda *args, **kwargs: None)
    assert _active(tmp_path) == [] and _closed_reason(tmp_path, "1") == "usage-cap"
    assert KEY not in next_emitted
    events, state = _scan(sm, next_emitted)
    assert [(kind, key) for kind, _line, key in events] == [("USAGE_CAP", KEY)]
    assert state[KEY]["state"] == "paused" and state[KEY]["paged"]


def test_a_record_another_pass_already_closed_is_left_alone(sm, windows, tmp_path):
    _record(tmp_path, "1")
    record_path = tmp_path / "active" / "1.json"
    record = json.loads(record_path.read_text())
    record_path.unlink()
    assert sm._archive_child(record_path, record, "usage-cap", claim=True) is False
    assert not (tmp_path / "closed" / "1.json").exists() and windows == []


def test_a_paused_pipeline_node_stays_out_of_the_resume_list(sm, windows, tmp_path, monkeypatch):
    _share_the_state_root(tmp_path, monkeypatch)
    _breach(tmp_path)
    _record(tmp_path, "1")
    _record(tmp_path, "2")
    _assignment(tmp_path, "2", ticket="work-x", pipeline_request_id="r1")
    _scan(sm, {})
    rows = mcp_server.list_closed_workers_impl()
    assert [row["name"] for row in rows if row["closed_reason"] == "usage-cap"] == ["w-1"]
    assert _closed_reason(tmp_path, "2") == "usage-cap-node" and _active(tmp_path) == []


def test_the_pause_lifting_pages_cleared_once(sm, windows, tmp_path):
    now = int(time.time())
    events, state = _scan(sm, {KEY: {"state": "paused", "at": now - 600, "paged": now - 600}}, now)
    assert [kind for kind, _line, _key in events] == ["USAGE_CAP_CLEARED"]
    assert events[0][1].startswith("USAGE_CAP_CLEARED fleet → tell the engineer the pause is lifted; ")
    assert "closed with reason usage-cap: send_manager_to_worker(" in events[0][1]
    assert state[KEY] == {"state": "cleared", "at": now, "paged": now}
    again, _ = _scan(sm, state, now + 60)
    assert again == []


def test_a_key_the_dead_lane_rolls_back_repeats_the_transition(sm, windows, tmp_path):
    now = int(time.time())
    prior = {KEY: {"state": "paused", "at": now - 600, "paged": now - 600}}
    first, next_emitted = _scan(sm, prior, now)
    state_path = tmp_path / "emitted.json"
    sm._commit_actions_only(state_path, prior, next_emitted)
    second, _ = _scan(sm, json.loads(state_path.read_text()), now + 60)
    assert [kind for kind, _l, _k in first + second] == ["USAGE_CAP_CLEARED", "USAGE_CAP_CLEARED"]


def test_cleared_pages_again_after_the_limit_recovery_zeroes_paged(sm, windows, tmp_path):
    now = int(time.time())
    events, state = _scan(sm, {KEY: {"state": "cleared", "at": now - 60, "paged": 0}}, now)
    assert [kind for kind, _l, _k in events] == ["USAGE_CAP_CLEARED"]
    assert state[KEY] == {"state": "cleared", "at": now - 60, "paged": now}


def test_a_paused_key_with_paged_zero_pages_again_and_keeps_the_pause_start(sm, windows, tmp_path):
    _breach(tmp_path)
    now = int(time.time())
    events, state = _scan(sm, {KEY: {"state": "paused", "at": now - 900, "paged": 0}}, now)
    assert [kind for kind, _l, _k in events] == ["USAGE_CAP"]
    assert state[KEY] == {"state": "paused", "at": now - 900, "paged": now}


def test_a_cleared_key_is_carried_for_a_day_then_dropped(sm, windows, tmp_path):
    now = int(time.time())
    young = {"state": "cleared", "at": now - 60, "paged": now - 60}
    events, state = _scan(sm, {KEY: young}, now)
    assert events == [] and state[KEY] == young
    events, state = _scan(sm, {KEY: {**young, "at": now - 86400}}, now)
    assert events == [] and KEY not in state


def test_a_broken_file_pages_once_and_closes_nobody(sm, windows, tmp_path):
    _breach(tmp_path, "[b]\nfive_hour = 170\n")
    _record(tmp_path, "1")
    paused = {"state": "paused", "at": 1, "paged": 1}
    first, state = _scan(sm, {KEY: paused})
    second, _ = _scan(sm, state)
    assert [kind for kind, _l, _k in first] == ["USAGE_CAP_FILE"] and second == []
    assert "five_hour must be a number from 1 to 100" in first[0][1]
    assert state[KEY] == paused and state[FILE_KEY]
    assert _active(tmp_path) == ["1"] and windows == []


def test_a_broken_table_beside_a_breach_still_closes_the_fleet_and_pages_both(sm, windows, tmp_path):
    _breach(tmp_path, "[b]\nfive_hour = 70\n[a]\nseven_day = 0.8\n")
    _record(tmp_path, "1")
    events, state = _scan(sm, {})
    assert [kind for kind, _l, _k in events] == ["USAGE_CAP_FILE", "USAGE_CAP"]
    assert "seven_day must be a number from 1 to 100" in events[0][1]
    assert state[KEY]["state"] == "paused" and state[FILE_KEY]
    assert _active(tmp_path) == [] and _closed_reason(tmp_path, "1") == "usage-cap"


def test_a_manager_that_never_saw_the_pause_is_told_to_resume_its_closed_workers(sm, windows, tmp_path):
    _breach(tmp_path)
    _record(tmp_path, "1", parent_manager_name="mgr-M")
    _record(tmp_path, "2", parent_manager_name="mgr-A")
    seen, _ = _scan(sm, {}, manager_name="mgr-A")
    assert [kind for kind, _l, _k in seen] == ["USAGE_CAP"] and _active(tmp_path) == []
    (tmp_path / "usage" / "b.json").write_text(json.dumps({"five_hour_pct": 10, "ts": time.time()}))
    now = int(time.time()) + 1
    events, state = _scan(sm, {}, now, manager_name="mgr-M")
    assert [kind for kind, _l, _k in events] == ["USAGE_CAP_CLEARED"]
    assert state[KEY] == {"state": "cleared", "at": now, "paged": now}
    again, _ = _scan(sm, state, now + 60, manager_name="mgr-M")
    assert again == []
    other, other_state = _scan(sm, {}, now, manager_name="mgr-Z")
    assert other == [] and other_state == {}


def test_without_the_usage_cap_module_the_step_is_silent(sm, windows, tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_usage_cap", None)
    _breach(tmp_path)
    _record(tmp_path, "1")
    events, state = _scan(sm, {})
    assert events == [] and state == {} and _active(tmp_path) == ["1"]


def test_main_closes_the_fleet_before_the_tick_reads_the_active_records(sm, windows, tmp_path, capsys,
                                                                          monkeypatch):
    calls = []
    scan, load = sm._scan_usage_cap, sm._load_active_records

    def spy_scan(*args):
        calls.append("usage-cap")
        return scan(*args)

    def spy_load():
        calls.append("load")
        return load()

    monkeypatch.setattr(sm, "_scan_usage_cap", spy_scan)
    monkeypatch.setattr(sm, "_load_active_records", spy_load)
    _breach(tmp_path)
    _record(tmp_path, "mgr-1", agent="manager", name="mgr-A", domain="general")
    _record(tmp_path, "w-1", name="zippy-otter", parent_manager_name="mgr-A")
    assert sm.main(manager_name="mgr-A") == 0
    out = capsys.readouterr().out
    assert calls[0] == "usage-cap", calls
    assert "USAGE_CAP b 5h 71% ≥ 70% → " in out, out
    assert _active(tmp_path) == ["mgr-1"]
    assert _closed_reason(tmp_path, "w-1") == "usage-cap"
