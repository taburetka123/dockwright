import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from dockwright import lane_lines

REPO_ROOT = Path(__file__).resolve().parent.parent
STALE_MONITOR_PATH = REPO_ROOT / "src" / "dockwright" / "stale_monitor.py"
STATUSLINE = REPO_ROOT / "deploy" / "statusline-command.sh"


def _load_stale_monitor():
    spec = importlib.util.spec_from_file_location("stale_monitor_ctx", STALE_MONITOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_statusline(home: Path, payload: dict) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_AGENT"}
    env["HOME"] = str(home)
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.pop("CLAUDE_ORCH_ACCOUNT", None)
    return subprocess.run(["/bin/sh", str(STATUSLINE)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=30)


def _ctx_payload(home, sid="sess-1", tokens=370000, window=1000000, pct=37, **extra):
    payload = {"cwd": str(home), "session_id": sid,
               "context_window": {"total_input_tokens": tokens,
                                  "context_window_size": window,
                                  "used_percentage": pct}}
    payload.update(extra)
    return payload


def _ctx_file(home, sid):
    return home / ".claude" / "dockwright" / "usage" / "context" / f"{sid}.json"


# ---------- the tap ----------

def test_tap_writes_the_context_file_mapping_total_input_tokens(tmp_path):
    r = _run_statusline(tmp_path, _ctx_payload(tmp_path))
    assert r.returncode == 0, r.stderr
    rec = json.loads(_ctx_file(tmp_path, "sess-1").read_text())
    assert rec["used_tokens"] == 370000, "used_tokens maps from total_input_tokens"
    assert rec["context_window_size"] == 1000000
    assert rec["used_percentage"] == 37
    assert isinstance(rec["ts"], (int, float))


def test_tap_writes_even_when_the_payload_carries_no_rate_limits(tmp_path):
    _run_statusline(tmp_path, _ctx_payload(tmp_path))
    assert _ctx_file(tmp_path, "sess-1").exists(), (
        "context_window is emitted unconditionally; the tap must not sit behind the "
        "rate_limits gate")


def test_tap_writes_nothing_when_used_percentage_is_null(tmp_path):
    payload = {"cwd": str(tmp_path), "session_id": "sess-1",
               "context_window": {"total_input_tokens": 0,
                                  "context_window_size": 1000000,
                                  "used_percentage": None}}
    r = _run_statusline(tmp_path, payload)
    assert r.returncode == 0
    assert not _ctx_file(tmp_path, "sess-1").exists()


def test_tap_removes_a_stale_file_when_used_percentage_goes_null(tmp_path):
    _run_statusline(tmp_path, _ctx_payload(tmp_path, tokens=966000, pct=97))
    assert _ctx_file(tmp_path, "sess-1").exists()
    payload = {"cwd": str(tmp_path), "session_id": "sess-1",
               "context_window": {"total_input_tokens": 0,
                                  "context_window_size": 1000000,
                                  "used_percentage": None}}
    _run_statusline(tmp_path, payload)
    assert not _ctx_file(tmp_path, "sess-1").exists(), (
        "a compacted session must not keep reporting its pre-compact size")


@pytest.mark.parametrize("bad", ["../evil", ".hidden", "x/y"])
def test_tap_refuses_an_unsafe_session_id(tmp_path, bad):
    r = _run_statusline(tmp_path, _ctx_payload(tmp_path, sid=bad))
    assert r.returncode == 0, r.stderr
    root = tmp_path / ".claude" / "dockwright" / "usage"
    found = list(root.rglob("*.json")) if root.exists() else []
    assert found == [], found
    assert not (tmp_path / ".claude" / "dockwright" / "evil.json").exists()


def test_tap_leaves_the_account_usage_json_alone(tmp_path):
    payload = _ctx_payload(tmp_path)
    payload["rate_limits"] = {"five_hour": {"used_percentage": 42},
                              "seven_day": {"used_percentage": 7}}
    _run_statusline(tmp_path, payload)
    acct = json.loads(
        (tmp_path / ".claude" / "dockwright" / "usage" / "a.json").read_text())
    assert acct["five_hour_pct"] == 42
    assert "used_tokens" not in acct


# ---------- the pager ----------

@pytest.fixture
def sm(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DOCKWRIGHT_STATE_DIR", raising=False)
    return _load_stale_monitor()


@pytest.fixture
def ctx_dir(tmp_path):
    d = tmp_path / "ctxdir"
    d.mkdir()
    return d


def _write_ctx(ctx_dir, sid, tokens, window=1000000, pct=None, ts=None):
    pct = pct if pct is not None else min(100, round(tokens / window * 100))
    (ctx_dir / f"{sid}.json").write_text(json.dumps(
        {"used_tokens": tokens, "context_window_size": window,
         "used_percentage": pct, "ts": ts if ts is not None else int(time.time())}))


def _worker(sid="w-1", name="zippy-otter", parent="boss"):
    return {"claude_sid": sid, "agent": "worker", "name": name,
            "parent_name": parent, "pid": os.getpid(), "state": "idle"}


def _scan(sm, ctx_dir, records, emitted, now=None, manager_name="boss"):
    events = []
    next_emitted = {}
    sm._scan_context(now or int(time.time()), emitted, next_emitted,
                     lambda kind, name, lane_kind, fields, key=None, sid=None: events.append(
                         (kind, name, fields, key)),
                     [(Path(f"/nonexistent/{r['claude_sid']}.json"), r) for r in records],
                     manager_name, context_dir=ctx_dir)
    return events, next_emitted


def _steps(events):
    return [fields["threshold"] for _kind, _name, fields, _key in events]


def _lines(events):
    return [lane_lines.render("CONTEXT", **fields) for _kind, _name, fields, _key in events]


def _tick(sm, ctx_dir, tokens, state, window=1000000, record=None):
    """One scan at `tokens`; returns (steps paged, new state)."""
    rec = record or _worker()
    _write_ctx(ctx_dir, rec["claude_sid"], tokens, window=window)
    events, state = _scan(sm, ctx_dir, [rec], state)
    return _steps(events), state


# --- one page per step, one step per crossing ---

def test_crossing_70_pages_once_and_not_again_at_72_or_75(sm, ctx_dir):
    paged, state = _tick(sm, ctx_dir, 700000, {})
    assert paged == [70]
    assert state["context:w-1"] == {"step": 70}
    paged, state = _tick(sm, ctx_dir, 720000, state)
    assert paged == []
    paged, state = _tick(sm, ctx_dir, 750000, state)
    assert paged == []
    assert state["context:w-1"] == {"step": 70}


def test_crossing_80_pages_once(sm, ctx_dir):
    _paged, state = _tick(sm, ctx_dir, 750000, {})
    paged, state = _tick(sm, ctx_dir, 800000, state)
    assert paged == [80]
    assert state["context:w-1"] == {"step": 80}
    paged, _state = _tick(sm, ctx_dir, 850000, state)
    assert paged == []


def test_crossing_90_pages_once(sm, ctx_dir):
    _paged, state = _tick(sm, ctx_dir, 850000, {})
    paged, state = _tick(sm, ctx_dir, 900000, state)
    assert paged == [90]
    assert state["context:w-1"] == {"step": 90}
    paged, _state = _tick(sm, ctx_dir, 970000, state)
    assert paged == [], "97% crosses no step 90 has not already crossed"


def test_the_line_keeps_the_shape_and_names_the_step(sm, ctx_dir):
    _write_ctx(ctx_dir, "w-1", 966000)
    events, _ = _scan(sm, ctx_dir, [_worker()], {})
    assert [e[2] for e in events] == [
        {"name": "zippy-otter", "used": 966, "window": "1M", "pct": 97, "threshold": t, "own": False}
        for t in (70, 80, 90)], "every line reports the reading that fired them; only the step differs"
    assert _lines(events) == [
        None,
        'CONTEXT zippy-otter 966k of 1M (97%), 80% step → have it finish and report, then kill_worker("zippy-otter") '
        "and spawn_worker a fresh one from its artifacts; a token-holding pipeline node: kill_worker only, never one "
        "parked at a gate",
        "CONTEXT zippy-otter 966k of 1M (97%), 90% step → tell the engineer",
    ], "a worker's 70% step names no manager action, so it does not print"


# --- a drop re-arms the step it fell under ---

def test_a_drop_below_every_step_rearms_70(sm, ctx_dir):
    _paged, state = _tick(sm, ctx_dir, 850000, {})
    paged, state = _tick(sm, ctx_dir, 400000, state)
    assert paged == []
    assert "context:w-1" not in state, "below the first step is the reset"
    paged, _state = _tick(sm, ctx_dir, 710000, state)
    assert paged == [70]


def test_a_drop_below_80_rearms_80_and_not_70(sm, ctx_dir):
    _paged, state = _tick(sm, ctx_dir, 850000, {})
    paged, state = _tick(sm, ctx_dir, 710000, state)
    assert paged == [], "falling back under 80 is not itself news"
    assert state["context:w-1"] == {"step": 70}
    paged, _state = _tick(sm, ctx_dir, 810000, state)
    assert paged == [80], "80 re-arms; 70 was never dropped, so it does not re-page"


# --- a tick that crosses more than one step ---

def test_one_tick_across_all_three_steps_emits_three_ascending_lines(sm, ctx_dir):
    paged, state = _tick(sm, ctx_dir, 910000, {})
    assert paged == [70, 80, 90], (
        "each crossed step carries a different manager action; the 80 line is the only "
        "one that winds a worker down")
    assert state["context:w-1"] == {"step": 90}


def test_a_two_step_jump_above_a_stored_step_emits_only_the_new_ones(sm, ctx_dir):
    _paged, state = _tick(sm, ctx_dir, 729740, {})
    assert _paged == [70]
    paged, state = _tick(sm, ctx_dir, 906691, state)
    assert paged == [80, 90]
    assert state["context:w-1"] == {"step": 90}


# --- the step is a percentage of the record's own window ---

def test_the_step_is_a_percentage_of_the_records_own_window(sm, ctx_dir):
    paged, state = _tick(sm, ctx_dir, 139000, {}, window=200000)
    assert paged == [], "139k is 69.5% of a 200k window"
    paged, state = _tick(sm, ctx_dir, 140000, state, window=200000)
    assert paged == [70]
    paged, state = _tick(sm, ctx_dir, 160000, state, window=200000)
    assert paged == [80]
    paged, _state = _tick(sm, ctx_dir, 180000, state, window=200000)
    assert paged == [90]


def test_a_200k_window_page_names_its_own_size(sm, ctx_dir):
    _write_ctx(ctx_dir, "w-1", 140000, window=200000)
    events, _ = _scan(sm, ctx_dir, [_worker()], {})
    assert lane_lines.head("CONTEXT", **events[0][2]) == "zippy-otter 140k of 200k (70%), 70% step"


# --- scope ---

def test_pager_silent_below_the_first_step(sm, ctx_dir):
    _write_ctx(ctx_dir, "w-1", 400000)
    events, next_emitted = _scan(sm, ctx_dir, [_worker()], {})
    assert events == []
    assert "context:w-1" not in next_emitted


def test_pager_covers_the_managers_own_record(sm, ctx_dir):
    _write_ctx(ctx_dir, "mgr-1", 800000)
    mgr = {"claude_sid": "mgr-1", "agent": "manager", "name": "boss",
           "domain": "general", "pid": os.getpid()}
    events, _ = _scan(sm, ctx_dir, [mgr], {})
    assert [line.split(" → ")[0] for line in _lines(events)] == [
        "CONTEXT boss 800k of 1M (80%), 70% step",
        "CONTEXT boss 800k of 1M (80%), 80% step",
    ], "_in_scope never matches a manager's own record; it must be added by name"
    assert all("propose /manager-recycle" in line for line in _lines(events))


def test_the_managers_own_pane_steps_like_a_workers(sm, ctx_dir):
    mgr = {"claude_sid": "mgr-1", "agent": "manager", "name": "boss",
           "domain": "general", "pid": os.getpid()}
    paged, state = _tick(sm, ctx_dir, 700000, {}, record=mgr)
    assert paged == [70]
    paged, state = _tick(sm, ctx_dir, 750000, state, record=mgr)
    assert paged == []
    paged, _state = _tick(sm, ctx_dir, 800000, state, record=mgr)
    assert paged == [80]


def test_an_unreadable_tick_between_two_fresh_ones_does_not_repage(sm, ctx_dir):
    _paged, state = _tick(sm, ctx_dir, 966000, {})
    (ctx_dir / "w-1.json").write_text("{not json")
    _e2, state2 = _scan(sm, ctx_dir, [_worker()], state)
    assert state2.get("context:w-1") == state["context:w-1"], "the step must be carried"
    paged, _ = _tick(sm, ctx_dir, 966000, state2)
    assert paged == [], "carrying the step is what stops a repeat page"


def test_a_missing_tick_between_two_fresh_ones_does_not_repage(sm, ctx_dir):
    _paged, state = _tick(sm, ctx_dir, 966000, {})
    (ctx_dir / "w-1.json").unlink()
    _e2, state2 = _scan(sm, ctx_dir, [_worker()], state)
    assert state2.get("context:w-1") == state["context:w-1"]
    paged, _ = _tick(sm, ctx_dir, 966000, state2)
    assert paged == []


@pytest.mark.parametrize("body", ["", "[]", "null", "{not json"])
def test_a_malformed_context_file_pages_nothing_and_raises_nothing(sm, ctx_dir, body):
    (ctx_dir / "w-1.json").write_text(body)
    events, _ = _scan(sm, ctx_dir, [_worker()], {})
    assert events == []


def test_the_writer_directory_is_the_readers_default(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude" / "dockwright").mkdir(parents=True)
    r = _run_statusline(home, _ctx_payload(home, sid="w-1", tokens=966000, pct=97))
    assert r.returncode == 0, r.stderr
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DOCKWRIGHT_STATE_DIR", raising=False)
    mod = _load_stale_monitor()
    events = []
    mod._scan_context(int(time.time()), {}, {},
                      lambda kind, name, lane_kind, fields, key=None, sid=None: events.append(fields),
                      [(Path("/nonexistent/w-1.json"), _worker())], "boss")
    assert len(events) == 3, (
        "the statusline's write path and _scan_context's default read path must be the "
        "same directory; every other pager test passes context_dir explicitly")
    assert (events[-1]["used"], events[-1]["threshold"]) == (966, 90)


def test_a_raising_context_scan_does_not_silence_the_rest_of_the_lane(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DOCKWRIGHT_STATE_DIR", raising=False)
    mod = _load_stale_monitor()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(mod, "QUESTIONS", tmp_path / "questions")
    monkeypatch.setattr(mod, "CLOSED", tmp_path / "closed")
    monkeypatch.setattr(mod, "CLAUDE_PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(mod, "CODEX_SESSIONS", tmp_path / "codex-sessions")

    def boom(*a, **k):
        raise RuntimeError("context scan exploded")

    monkeypatch.setattr(mod, "_scan_context", boom)

    state_path = mod._emitted_state_path("mgr-A")
    (tmp_path / "active").mkdir(parents=True, exist_ok=True)
    rc = mod.main(manager_name="mgr-A")

    assert rc == 0
    assert state_path.exists(), (
        "an unwrapped raise would skip the state write and silence every other detector")
    assert "context scan exploded" in capsys.readouterr().err


def test_main_emits_a_context_page_for_a_scoped_worker(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DOCKWRIGHT_STATE_DIR", raising=False)
    mod = _load_stale_monitor()
    active = tmp_path / "active"
    active.mkdir(parents=True)
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    for name, value in (("ROOT", tmp_path), ("ACTIVE", active),
                        ("QUESTIONS", tmp_path / "questions"),
                        ("CLOSED", tmp_path / "closed"),
                        ("CLAUDE_PROJECTS", tmp_path / "projects"),
                        ("CODEX_SESSIONS", tmp_path / "codex-sessions"),
                        ("USAGE_CONTEXT", ctx)):
        monkeypatch.setattr(mod, name, value)

    (active / "w-1.json").write_text(json.dumps(_worker(sid="w-1", parent="mgr-A")))
    (active / "mgr.json").write_text(json.dumps(
        {"claude_sid": "mgr-1", "agent": "manager", "name": "mgr-A",
         "domain": "general", "pid": os.getpid()}))
    _write_ctx(ctx, "w-1", 966000)
    _write_ctx(ctx, "mgr-1", 400000)

    rc = mod.main(manager_name="mgr-A")
    out = capsys.readouterr().out

    assert rc == 0
    assert "CONTEXT zippy-otter 966k of 1M (97%), 90% step → tell the engineer" in out, out
    assert "70% step" not in out, "a worker's 70% step is silent"
    assert "CONTEXT mgr-A" not in out, "the manager is under the first step here"


def test_main_skips_a_worker_belonging_to_another_manager(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DOCKWRIGHT_STATE_DIR", raising=False)
    mod = _load_stale_monitor()
    active = tmp_path / "active"
    active.mkdir(parents=True)
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    for name, value in (("ROOT", tmp_path), ("ACTIVE", active),
                        ("QUESTIONS", tmp_path / "questions"),
                        ("CLOSED", tmp_path / "closed"),
                        ("CLAUDE_PROJECTS", tmp_path / "projects"),
                        ("CODEX_SESSIONS", tmp_path / "codex-sessions"),
                        ("USAGE_CONTEXT", ctx)):
        monkeypatch.setattr(mod, name, value)

    (active / "peer.json").write_text(json.dumps(
        _worker(sid="peer", name="other-tab", parent="mgr-B")))
    _write_ctx(ctx, "peer", 966000)

    mod.main(manager_name="mgr-A")
    assert "CONTEXT other-tab" not in capsys.readouterr().out


def _dead_pid():
    p = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
    p.wait()
    return p.pid


def test_a_dead_session_is_never_paged(sm, ctx_dir):
    dead = _dead_pid()
    assert not sm._pid_alive(dead), "fixture failed to produce a reaped pid"
    _write_ctx(ctx_dir, "w-1", 966000, ts=int(time.time()) - 86400)
    record = _worker()
    record["pid"] = dead
    events, next_emitted = _scan(sm, ctx_dir, [record], {})
    assert events == [], (
        "a killed worker keeps its active record and its context file forever; paging "
        "about it is actionable-wrong — there is nothing left to compact")
    assert "context:w-1" not in next_emitted


def test_a_dead_session_stops_paging_at_the_next_step(sm, ctx_dir):
    paged, state = _tick(sm, ctx_dir, 700000, {})
    assert paged == [70]
    record = _worker()
    record["pid"] = _dead_pid()
    _write_ctx(ctx_dir, "w-1", 966000)
    later, _ = _scan(sm, ctx_dir, [record], state)
    assert later == [], "80 and 90 must not page about a session that is gone"


def test_a_record_with_no_pid_still_pages(sm, ctx_dir):
    _write_ctx(ctx_dir, "w-1", 966000)
    record = _worker()
    record.pop("pid")
    events, _ = _scan(sm, ctx_dir, [record], {})
    assert len(events) == 3, "_pid_alive fails open; an absent pid is not evidence of death"
