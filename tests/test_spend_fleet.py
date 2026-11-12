import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone

import pytest

from dockwright import config, paths, pricing, spend_fleet, state


@pytest.fixture(autouse=True)
def tz():
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("TZ", "Etc/GMT-9")
        time.tzset()
        yield
    time.tzset()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(paths, "CLOSED", tmp_path / "closed")
    monkeypatch.setattr(paths, "ASSIGNMENTS", tmp_path / "assignments")
    monkeypatch.setattr(paths, "ARTIFACTS", tmp_path / "artifacts")
    monkeypatch.setattr(paths, "CONFIG_HOME", tmp_path / ".claude")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(config, "account_names", lambda: ())
    for name in ("active", "closed", "assignments", "artifacts"):
        (tmp_path / name).mkdir()
    return tmp_path


def _z(local_dt: datetime) -> str:
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _window():
    today = datetime.now().astimezone().date()
    since = today - timedelta(days=1)
    until = today + timedelta(days=1)
    midnight = datetime(today.year, today.month, today.day).astimezone()
    in_ts = _z(midnight + timedelta(hours=10))
    out_ts = _z(midnight - timedelta(days=3))
    return since, until, in_ts, out_ts


def _shift(ts: str, seconds: int) -> str:
    parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=timezone.utc)
    return (parsed + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _asst(mid, *, ts=None, model="claude-opus-5-5", out=10, cin=0, ccr=0, ccc=0, cc=None,
          tool_uses=(), effort=None, cwd=None, agent=None, text=None):
    usage = {"output_tokens": out, "input_tokens": cin,
             "cache_read_input_tokens": ccr, "cache_creation_input_tokens": ccc}
    if cc is not None:
        usage["cache_creation"] = cc
    content = [{"type": "tool_use", "id": tid, "name": tname, **({"input": rest[0]} if rest else {})}
               for tid, tname, *rest in tool_uses]
    if text is not None:
        content.insert(0, {"type": "text", "text": text})
    rec = {"type": "assistant",
           "message": {"id": mid, "model": model, "usage": usage, "content": content}}
    if ts is not None:
        rec["timestamp"] = ts
    if effort is not None:
        rec["effort"] = effort
    if cwd is not None:
        rec["cwd"] = cwd
    if agent is not None:
        rec["agentId"] = agent
    return rec


def _user(*, ts=None, text=None, tool_results=()):
    blocks = [{"type": "tool_result", "tool_use_id": item[0], "content": item[1]}
              for item in tool_results]
    if text is not None:
        blocks.append({"type": "text", "text": text})
    rec = {"type": "user", "message": {"role": "user", "content": blocks}}
    if ts is not None:
        rec["timestamp"] = ts
    return rec


def _write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def _main_path(world, sid):
    return world / ".claude" / "projects" / "-proj" / f"{sid}.jsonl"


def _sub_path(world, sid, agent):
    return world / ".claude" / "projects" / "-proj" / sid / "subagents" / f"agent-{agent}.jsonl"


def _bounds():
    since, until, _, _ = _window()
    return spend_fleet.window_bounds(since, until)


def test_dedup_by_message_id_and_synthetic_dropped(world):
    _, _, in_ts, _ = _window()
    p = _write(_main_path(world, "s1"), [
        _asst("m1", ts=in_ts, out=5),
        _asst("m1", ts=in_ts, out=5, tool_uses=(("t1", "Bash", {"command": "ls"}),)),
        _asst("syn", ts=in_ts, model="<synthetic>", out=99),
    ])
    since_ts, until_ts = _bounds()
    r = spend_fleet.scan_transcript(str(p), since_ts, until_ts)
    assert len(r["rows"]) == 1 and r["meta"]["calls_all"] == 1
    assert r["rows"][0]["out"] == 5 and r["rows"][0]["tools"] == ["Bash:ls"]


def test_window_by_call_timestamp_not_mtime(world):
    since, until, in_ts, out_ts = _window()
    today = datetime.now().astimezone().date()
    p = _write(_main_path(world, "s1"), [
        _asst("m1", ts=out_ts, out=1),
        _asst("m2", ts=in_ts, out=2),
    ])
    since_ts, until_ts = _bounds()
    r = spend_fleet.scan_transcript(str(p), since_ts, until_ts)
    assert [row["day"] for row in r["rows"]] == [str(today)]
    assert r["meta"]["calls_all"] == 2
    assert r["meta"]["tokens_all"]["out"] == 3


def test_cache_split_and_pricing_match_cost_breakdown(world):
    _, _, in_ts, _ = _window()
    p = _write(_main_path(world, "s1"), [
        _asst("a", ts=in_ts, out=10, ccc=1000),
        _asst("b", ts=_shift(in_ts, 5), out=10,
              cc={"ephemeral_5m_input_tokens": 300, "ephemeral_1h_input_tokens": 700}),
    ])
    since_ts, until_ts = _bounds()
    r = spend_fleet.scan_transcript(str(p), since_ts, until_ts)
    row_a, row_b = r["rows"]
    assert (row_a["cache_5m"], row_a["cache_1h"]) == (1000, 0)
    assert (row_b["cache_5m"], row_b["cache_1h"]) == (300, 700)
    assert row_b["ctx"] == 1000
    assert row_b["usd"]["total"] == pytest.approx(pricing.cost_breakdown(
        "claude-opus-5-5", rates=pricing.get_rates(), output_tokens=10,
        cache_creation_5m_tokens=300, cache_creation_1h_tokens=700)["total"])
    assert row_b["priced"] is True


def test_sidecar_joined_even_when_its_mtime_is_before_window(world):
    since, until, in_ts, _ = _window()
    _write(_main_path(world, "s1"), [_asst("m1", ts=in_ts, out=10)])
    sub = _write(_sub_path(world, "s1", "x"), [_asst("k1", ts=in_ts, out=20, agent="x")])
    old = time.time() - 30 * 86400
    os.utime(sub, (old, old))
    out = spend_fleet.build(since, until, jobs=1)
    assert len(out["sessions"]) == 1
    s = out["sessions"][0]
    assert s["calls"] == 1 and s["calls_sub"] == 1 and len(s["subagents"]) == 1
    assert s["subagents"][0]["agent"] == "x" and s["usd_sub"] > 0
    assert {row["agent"] for row in out["rows"]} == {None, "x"}
    assert out["summary"]["files"] == 1


def test_labels_node_child_worker_manager_headless(world):
    since, until, in_ts, _ = _window()
    for sid in ("n1", "c1", "w1", "m1"):
        _write(_main_path(world, sid), [_asst("m-" + sid, ts=in_ts, cwd="/w/x")])
    _write(_main_path(world, "h1"), [_asst("m-h1", ts=in_ts, cwd=tempfile.gettempdir() + "/spike")])
    events = world / "artifacts" / "w1" / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text("".join(json.dumps(e) + "\n" for e in [
        {"type": "advance_committed", "sid": "n1", "node": "repo", "index": 1},
        {"type": "child_round", "sids": ["c1"], "node": "repo", "slot": "reviewer",
         "lane": "document", "round": 1, "legs": ["r"], "providers": ["claude-tui"]},
    ]))
    state.write_json_atomic(world / "assignments" / "c1.json",
                            {"claude_sid": "c1", "context_role": "DocReviewer"})
    state.write_json_atomic(world / "assignments" / "w1.json",
                            {"claude_sid": "w1", "ticket": "T-1", "name": "wrk",
                             "spawn_extra_args": ["--model", "claude-opus-5-5", "--effort", "xhigh"]})
    state.write_json_atomic(world / "closed" / "m1.json",
                            {"claude_sid": "m1", "kind": "manager", "account": "b"})
    out = spend_fleet.build(since, until, jobs=1)
    by_sid = {s["sid"]: s for s in out["sessions"]}
    kinds = {sid: s["kind"] for sid, s in by_sid.items()}
    assert kinds == {"n1": "node", "c1": "child", "w1": "worker", "m1": "manager", "h1": "headless"}
    n1 = by_sid["n1"]
    assert (n1["work"], n1["node"], n1["index"]) == ("w1", "repo", 1)
    c1 = by_sid["c1"]
    assert (c1["child_slot"], c1["child_lane"], c1["child_rounds"], c1["child_role"]) == (
        "reviewer", "document", 1, "DocReviewer")
    w1 = by_sid["w1"]
    assert (w1["work"], w1["spawn_model"], w1["spawn_effort"], w1["name"]) == (
        "T-1", "claude-opus-5-5", "xhigh", "wrk")
    assert by_sid["m1"]["account"] == "b"
    assert out["summary"]["by_kind"]["node"]["sessions"] == 1
    assert out["summary"]["by_node"]["repo"]["sessions"] == 2


def test_sdk_cli_session_in_manager_home_is_not_a_manager(world, monkeypatch):
    since, until, in_ts, _ = _window()
    home = world / "manager-home"
    monkeypatch.setattr(config, "manager_home_default", lambda: home)
    cli = _asst("m-cli", ts=in_ts, cwd=str(home))
    cli["entrypoint"] = "cli"
    sdk = _asst("m-sdk", ts=in_ts, cwd=str(home))
    sdk["entrypoint"] = "sdk-cli"
    _write(_main_path(world, "cli"), [cli])
    _write(_main_path(world, "sdk"), [sdk])
    _write(_main_path(world, "old"), [_asst("m-old", ts=in_ts, cwd=str(home))])
    out = spend_fleet.build(since, until, jobs=1)
    by_sid = {s["sid"]: s for s in out["sessions"]}
    assert (by_sid["cli"]["kind"], by_sid["old"]["kind"]) == ("manager", "manager")
    assert by_sid["sdk"]["kind"] != "manager"
    assert (by_sid["cli"]["entrypoint"], by_sid["sdk"]["entrypoint"], by_sid["old"]["entrypoint"]) == (
        "cli", "sdk-cli", None)


def _record(sid, *, out, cache_read, cache_creation, model_requests, in_tokens=0):
    return {"claude_sid": sid, "spend": {
        "turns": 1, "out_tokens": out, "in_tokens": in_tokens, "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation, "model_requests": model_requests}}


def test_cross_check_exact_lags_short(world):
    since, until, in_ts, _ = _window()
    for sid in ("exact", "lags", "short"):
        _write(_main_path(world, sid), [_asst("m-" + sid, ts=in_ts, out=10, ccr=100, ccc=50)])
        _write(_sub_path(world, sid, "a"), [_asst("k-" + sid, ts=in_ts, out=5, ccr=10, agent="a")])
    state.write_json_atomic(world / "closed" / "exact.json",
                            _record("exact", out=15, cache_read=110, cache_creation=50, model_requests=1))
    state.write_json_atomic(world / "closed" / "lags.json",
                            _record("lags", out=14, cache_read=110, cache_creation=50, model_requests=1))
    state.write_json_atomic(world / "closed" / "short.json",
                            _record("short", out=15, cache_read=111, cache_creation=50, model_requests=1))
    out = spend_fleet.build(since, until, jobs=1)
    cc = out["summary"]["cross_check"]
    assert (cc["compared"], cc["exact"], cc["record_lags"], cc["scan_short"]) == (3, 1, 1, 1)
    assert cc["worst"]["sid"] == "short" and cc["worst"]["diff"]["cache_read_tokens"] == -1


def test_tools_and_result_chars_across_split_records(world):
    _, _, in_ts, _ = _window()
    p = _write(_main_path(world, "s1"), [
        _asst("m1", ts=in_ts, text="hi"),
        _asst("m1", ts=in_ts, tool_uses=(("t1", "Bash", {"command": "git status\nls"}),)),
        _user(ts=_shift(in_ts, 1), tool_results=(("t1", "x" * 42),)),
        _asst("m2", ts=_shift(in_ts, 2),
              tool_uses=(("t2", "Read", {"file_path": "/w/docs/specs/x.md"}),
                         ("t3", "mcp__dockwright__artifact_get", {}),
                         ("t4", "Agent", {"subagent_type": "Explore", "model": "haiku"}),
                         ("t5", "Read", {}))),
    ])
    since_ts, until_ts = _bounds()
    r = spend_fleet.scan_transcript(str(p), since_ts, until_ts)
    row = r["rows"][0]
    assert row["tools"] == ["Bash:git status"] and row["result_chars"] == 42
    assert r["rows"][1]["tools"] == ["Read:/w/docs/specs/x.md", "mcp:dockwright:artifact_get",
                                     "Agent:Explore:haiku", "Read"]
    assert r["meta"]["tool_calls"]["Read:/w/docs/specs/x.md"] == 1
    assert r["meta"]["result_chars"]["Bash:git status"] == 42


def test_cli_writes_three_files_and_json(world, tmp_path, capsys):
    since, until, in_ts, _ = _window()
    _write(_main_path(world, "s1"), [_asst("m1", ts=in_ts, out=10)])
    out_dir = tmp_path / "out"
    rc = spend_fleet.main(["--since", str(since), "--until", str(until),
                           "--out", str(out_dir), "--jobs", "1"])
    assert rc == 0 and {p.name for p in out_dir.iterdir()} == {"calls.jsonl", "sessions.jsonl", "summary.json"}
    text = capsys.readouterr().out
    assert "scan_short" in text
    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["calls"] == 1
    assert len((out_dir / "calls.jsonl").read_text().splitlines()) == 1
    assert spend_fleet.main(["--since", str(since), "--until", str(until), "--jobs", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["window"]["since"] == str(since)
    assert spend_fleet.main(["--days", "2", "--since", str(since)]) == 2
    capsys.readouterr()
    assert spend_fleet.main(["--since", str(until), "--until", str(since), "--jobs", "1"]) == 2
    capsys.readouterr()
    assert spend_fleet.main(["--days", "0", "--jobs", "1"]) == 2
    assert "--days" in capsys.readouterr().err
    assert spend_fleet.main(["--days", "-2", "--jobs", "1"]) == 2
    assert "--since" not in capsys.readouterr().err


def test_roots_dedup_symlinked_account_dir(world, monkeypatch):
    since, until, in_ts, _ = _window()
    _write(_main_path(world, "s1"), [_asst("m1", ts=in_ts, out=10)])
    (world / ".claude-b").symlink_to(world / ".claude")
    monkeypatch.setattr(config, "account_names", lambda: ("b",))
    assert len(spend_fleet.transcript_roots()) == 1
    out = spend_fleet.build(since, until, jobs=1)
    assert out["summary"]["files"] == 1


def test_sidecar_without_main_is_not_scanned(world):
    since, until, in_ts, _ = _window()
    _write(_sub_path(world, "s1", "x"), [_asst("k1", ts=in_ts, out=20, agent="x")])
    out = spend_fleet.build(since, until, jobs=1)
    assert out["summary"]["files"] == 0 and out["sessions"] == []


def test_unparsable_timestamp_row_is_dropped_but_counted_in_calls_all(world):
    _, _, in_ts, _ = _window()
    p = _write(_main_path(world, "s1"), [
        _asst("m1", ts=in_ts, out=1),
        _asst("m2", out=2),
        _asst("m3", ts=_shift(in_ts, 30), out=3),
    ])
    since_ts, until_ts = _bounds()
    r = spend_fleet.scan_transcript(str(p), since_ts, until_ts)
    assert [row["out"] for row in r["rows"]] == [1, 3]
    assert r["meta"]["calls_all"] == 3 and r["meta"]["tokens_all"]["out"] == 6
    assert r["rows"][1]["gap_s"] == 30


def test_cross_check_skips_record_without_model_requests(world):
    since, until, in_ts, _ = _window()
    _write(_main_path(world, "s1"), [_asst("m1", ts=in_ts, out=10)])
    state.write_json_atomic(world / "closed" / "s1.json", {
        "claude_sid": "s1", "spend": {"turns": 1, "out_tokens": 10, "in_tokens": 0,
                                      "cache_read_tokens": 0, "cache_creation_tokens": 0}})
    out = spend_fleet.build(since, until, jobs=1)
    cc = out["summary"]["cross_check"]
    assert cc["compared"] == 0 and cc["worst"] is None


def test_seen_is_per_file(world):
    since, until, in_ts, _ = _window()
    _write(_main_path(world, "s1"), [_asst("same", ts=in_ts, out=10)])
    _write(_sub_path(world, "s1", "x"), [_asst("same", ts=in_ts, out=20, agent="x")])
    out = spend_fleet.build(since, until, jobs=1)
    s = out["sessions"][0]
    assert s["calls"] == 1 and s["calls_sub"] == 1
    assert len(out["rows"]) == 2


def test_out_dir_created_and_file_path_refused(world, tmp_path, capsys):
    since, until, in_ts, _ = _window()
    _write(_main_path(world, "s1"), [_asst("m1", ts=in_ts, out=10)])
    nested = tmp_path / "a" / "b" / "c"
    assert spend_fleet.main(["--since", str(since), "--until", str(until),
                             "--out", str(nested), "--jobs", "1"]) == 0
    assert (nested / "summary.json").is_file()
    blocker = tmp_path / "file"
    blocker.write_text("x")
    capsys.readouterr()
    assert spend_fleet.main(["--since", str(since), "--until", str(until),
                             "--out", str(blocker), "--jobs", "1"]) == 2
    assert "Traceback" not in capsys.readouterr().err
