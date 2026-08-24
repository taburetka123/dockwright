import json
import re

import pytest

from dockwright.spend_session import build_report


def _asst(mid, *, ts=None, model="claude-opus-5", out=10, cin=0, ccr=0, ccc=0,
          cc=None, stop="end_turn", sidechain=None, skill=None, server=None,
          mcp_tool=None, tool_uses=(), usage_extra=None):
    usage = {"output_tokens": out, "input_tokens": cin,
             "cache_read_input_tokens": ccr, "cache_creation_input_tokens": ccc}
    if cc is not None:
        usage["cache_creation"] = cc
    if usage_extra:
        usage.update(usage_extra)
    content = [{"type": "tool_use", "id": tid, "name": tname}
               for tid, tname in tool_uses]
    rec = {"type": "assistant",
           "message": {"id": mid, "model": model, "usage": usage,
                       "stop_reason": stop, "content": content}}
    if ts is not None:
        rec["timestamp"] = ts
    if sidechain is not None:
        rec["isSidechain"] = sidechain
    if skill is not None:
        rec["attributionSkill"] = skill
    if server is not None:
        rec["attributionMcpServer"] = server
    if mcp_tool is not None:
        rec["attributionMcpTool"] = mcp_tool
    return rec


def _user(*, ts=None, text=None, tool_results=(), meta=False):
    blocks = [{"type": "tool_result", "tool_use_id": tid} for tid in tool_results]
    if text is not None:
        blocks.append({"type": "text", "text": text})
    rec = {"type": "user", "message": {"role": "user", "content": blocks}}
    if ts is not None:
        rec["timestamp"] = ts
    if meta:
        rec["isMeta"] = True
    return rec


def _write(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


T = "2026-07-31T10:00:{:02d}.000Z"


def test_totals_split_main_vs_sidechain(tmp_path):
    main = _write(tmp_path / "s.jsonl", [
        _user(ts=T.format(0), text="go"),
        _asst("m1", ts=T.format(1), out=100, ccr=1000),
    ])
    subdir = tmp_path / "s" / "subagents"
    subdir.mkdir(parents=True)
    sub = _write(subdir / "agent-a.jsonl", [
        _asst("s1", ts=T.format(2), out=40, sidechain=True),
    ])
    r = build_report(main, subagent_logs=[sub])
    assert r["tokens"]["main"]["out_tokens"] == 100
    assert r["tokens"]["subagents"]["out_tokens"] == 40
    assert r["money"]["total"] == pytest.approx(
        r["money"]["main"] + r["money"]["subagents"])
    assert r["transcript"]["path"] == str(main)
    assert r["transcript"]["subagent_files"] == 1
    assert r["server_tool_use"] == {}


def test_iterations_never_summed(tmp_path):
    # iterations that would DOUBLE the totals if summed alongside top-level
    main = _write(tmp_path / "s.jsonl", [
        _asst("m1", ts=T.format(0), out=100,
              usage_extra={"iterations": [{"output_tokens": 100}]}),
    ])
    r = build_report(main)
    assert r["tokens"]["main"]["out_tokens"] == 100


def test_global_dedup_across_main_and_sidecar(tmp_path):
    main = _write(tmp_path / "s.jsonl", [_asst("dup", ts=T.format(0), out=100)])
    subdir = tmp_path / "s" / "subagents"
    subdir.mkdir(parents=True)
    sub = _write(subdir / "agent-a.jsonl", [_asst("dup", ts=T.format(1), out=100)])
    r = build_report(main, subagent_logs=[sub])
    assert r["tokens"]["main"]["out_tokens"] == 100
    assert r["tokens"]["subagents"]["out_tokens"] == 0  # main parsed first wins
    # split-response duplication: same message id twice -> 1 unique, 2 records
    assert r["api_calls"]["unique"] == 1
    assert r["api_calls"]["records"] == 2


def test_birth_filter_excludes_all_record_types_and_is_reported(tmp_path):
    import datetime
    old, new = T.format(0), T.format(30)
    new_epoch = datetime.datetime.fromisoformat(T.format(20)).timestamp()
    main = _write(tmp_path / "s.jsonl", [
        _user(ts=old, text="replayed prompt"),
        _asst("old", ts=old, out=500),
        _user(ts=new, text="fresh prompt"),
        _asst("new", ts=new, out=7),
    ])
    r = build_report(main, started_at=new_epoch)
    assert r["tokens"]["main"]["out_tokens"] == 7
    assert r["excluded_replayed"] == 2
    assert r["episode_count"] == 1          # replayed prompt opens no episode
    assert r["wall_clock"]["seconds"] == 0.0  # span starts at the fresh records
    raw = build_report(main, started_at=new_epoch, include_replayed=True)
    assert raw["tokens"]["main"]["out_tokens"] == 507


def test_episode_rule_midturn_injection_opens_no_episode(tmp_path):
    main = _write(tmp_path / "s.jsonl", [
        _user(ts=T.format(0), text="task"),
        _asst("m1", ts=T.format(1), stop="tool_use",
              tool_uses=[("t1", "Bash")]),
        # manager message lands MID-turn (previous stop_reason == tool_use):
        _user(ts=T.format(2), text="mid-turn nudge", tool_results=("t1",)),
        _asst("m2", ts=T.format(3), stop="end_turn"),
        # meta record after end_turn: still no new episode
        _user(ts=T.format(4), text="sys", meta=True),
        # real second prompt after end_turn:
        _user(ts=T.format(5), text="follow-up"),
        _asst("m3", ts=T.format(6), stop="end_turn"),
    ])
    r = build_report(main)
    assert r["episode_count"] == 2
    assert r["end_turn_count"] == 2


def test_tool_latency_pairing_and_unpaired(tmp_path):
    main = _write(tmp_path / "s.jsonl", [
        _user(ts=T.format(0), text="go"),
        _asst("m1", ts=T.format(1), stop="tool_use",
              tool_uses=[("t1", "Bash"), ("t2", "Bash")]),
        _user(ts=T.format(5), tool_results=("t1",)),   # 4s latency; t2 unpaired
        _asst("m2", ts=T.format(6), stop="end_turn"),
    ])
    r = build_report(main)
    bash = next(t for t in r["tools"] if t["tool"] == "Bash")
    assert bash["calls"] == 2 and bash["timed"] == 1
    assert bash["total_s"] == pytest.approx(4.0)


def test_missing_timestamps_counted_for_tokens_not_timing(tmp_path):
    main = _write(tmp_path / "s.jsonl", [
        _asst("m1", out=50),                      # no ts at all
        _asst("m2", ts=T.format(9), out=1),
    ])
    r = build_report(main)
    assert r["tokens"]["main"]["out_tokens"] == 51
    assert r["wall_clock"]["seconds"] == 0.0      # single timestamped instant


def test_unknown_usage_fields_and_unpriced_model_surface(tmp_path):
    main = _write(tmp_path / "s.jsonl", [
        _asst("m1", ts=T.format(0), model="claude-mystery-9", out=5,
              usage_extra={"banana_tokens": 3}),
    ])
    r = build_report(main)
    assert "banana_tokens" in r["unknown_usage_fields"]
    assert r["money"]["unpriced_out_tokens"] == 5
    assert r["money"]["total"] == 0.0


def test_attribution_rollups(tmp_path):
    main = _write(tmp_path / "s.jsonl", [
        _user(ts=T.format(0), text="go"),
        _asst("m1", ts=T.format(1), out=10, skill="example-investigate"),
        _asst("m2", ts=T.format(2), out=20, server="postgres",
              mcp_tool="execute_sql"),
        _asst("m3", ts=T.format(3), out=40),
    ])
    r = build_report(main)
    skills = {row["skill"]: row for row in r["attribution"]["by_skill"]}
    assert skills["example-investigate"]["out_tokens"] == 10
    assert skills["(none)"]["out_tokens"] == 60
    mcp = {(row["server"], row["tool"]): row for row in r["attribution"]["by_mcp"]}
    assert mcp[("postgres", "execute_sql")]["out_tokens"] == 20
    for row in r["attribution"]["by_skill"] + r["attribution"]["by_mcp"]:
        assert "cost" in row  # tokens never rendered without money beside them


def test_prices_table_included_with_source(tmp_path):
    main = _write(tmp_path / "s.jsonl", [_asst("m1", ts=T.format(0))])
    r = build_report(main)
    assert "claude-opus-5" in {m["model"] for m in r["money"]["by_model"]}
    assert r["prices"]["source"] in ("built-in defaults",
                                    "built-in + dockwright.toml [pricing.rates] overrides")
    assert r["prices"]["rates"]  # non-empty mapping of the rates actually used


M = 1_000_000


def test_money_wiring_exact_dollars(tmp_path):
    # opus (in $5/MTok, out $25/MTok):
    #   out 1M*25=25, in 2M*5=10, read 10M*5*0.1=5,
    #   write5m 4M*5*1.25=25, write1h 1M*5*2=10   -> main = 75.0
    # haiku (in $1/MTok, out $5/MTok):
    #   out 2M*5=10, in 1M*1=1, read 5M*1*0.1=0.5,
    #   write5m 2M*1*1.25=2.5, write1h 0.5M*1*2=1 -> subagents = 15.0
    main = _write(tmp_path / "s.jsonl", [
        _user(ts=T.format(0), text="go"),
        _asst("m1", ts=T.format(1), model="claude-opus-5",
              out=1 * M, cin=2 * M, ccr=10 * M,
              cc={"ephemeral_5m_input_tokens": 4 * M,
                  "ephemeral_1h_input_tokens": 1 * M},
              stop="tool_use", tool_uses=[("t1", "Bash")]),
        _user(ts=T.format(3), tool_results=("t1",)),   # 2s Bash latency
        _asst("m2", ts=T.format(4), out=0, stop="end_turn"),
    ])
    subdir = tmp_path / "s" / "subagents"
    subdir.mkdir(parents=True)
    sub = _write(subdir / "agent-a.jsonl", [
        _asst("s1", ts=T.format(2), model="claude-haiku-4",
              out=2 * M, cin=1 * M, ccr=5 * M,
              cc={"ephemeral_5m_input_tokens": 2 * M,
                  "ephemeral_1h_input_tokens": 500_000},
              sidechain=True),
    ])
    r = build_report(main, subagent_logs=[sub])
    assert r["money"]["main"] == pytest.approx(75.0)
    assert r["money"]["subagents"] == pytest.approx(15.0)
    assert r["money"]["total"] == pytest.approx(90.0)
    assert r["money"]["anatomy"]["input"] == pytest.approx(11.0)
    assert r["money"]["anatomy"]["output"] == pytest.approx(35.0)
    assert r["money"]["anatomy"]["cache_read"] == pytest.approx(5.5)
    assert r["money"]["anatomy"]["cache_write"] == pytest.approx(38.5)
    assert sum(r["money"]["anatomy"].values()) == pytest.approx(90.0)
    assert r["tokens"]["main"]["cost"] == pytest.approx(75.0)
    assert r["tokens"]["subagents"]["cost"] == pytest.approx(15.0)
    assert len(r["episodes"]) == 1
    assert r["episodes"][0]["cost"] == pytest.approx(75.0)  # main-chain only
    bash = next(t for t in r["tools"] if t["tool"] == "Bash")
    assert bash["avg_s"] == pytest.approx(2.0)


def test_inline_sidechain_records_excluded_from_main_chain_stats(tmp_path):
    sub_prompt = {"type": "user", "isSidechain": True,
                  "timestamp": T.format(12),
                  "message": {"role": "user", "content": [
                      {"type": "text", "text": "subagent prompt"}]}}
    main = _write(tmp_path / "s.jsonl", [
        _user(ts=T.format(10), text="go"),
        _asst("m1", ts=T.format(11), stop="end_turn"),
        # inline sidechain assistants: ts outside the main span, end_turn stops
        _asst("s1", ts=T.format(1), out=5, stop="end_turn", sidechain=True),
        _asst("s2", ts=T.format(50), out=5, stop="end_turn", sidechain=True),
        sub_prompt,  # prompt-shaped sidechain user record
    ])
    r = build_report(main)
    assert r["wall_clock"]["seconds"] == pytest.approx(1.0)  # 10s -> 11s only
    assert r["end_turn_count"] == 1
    assert r["episode_count"] == 1
    assert r["tokens"]["subagents"]["out_tokens"] == 10
    # sidechain records (incl. the ts=50 one) must not stretch the episode
    assert r["episodes"][0]["duration_s"] == pytest.approx(1.0)


def test_attribution_costs_priced_per_model(tmp_path):
    main = _write(tmp_path / "s.jsonl", [
        _user(ts=T.format(0), text="go"),
        _asst("m1", ts=T.format(1), model="claude-opus-5", out=1 * M,
              skill="mixed"),
        _asst("m2", ts=T.format(2), model="claude-haiku-4", out=1 * M,
              skill="mixed"),
    ])
    r = build_report(main)
    rows = [row for row in r["attribution"]["by_skill"]
            if row["skill"] == "mixed"]
    assert len(rows) == 1                       # merged by label
    assert rows[0]["out_tokens"] == 2 * M
    # opus out 1M*25=25 plus haiku out 1M*5=5 — NOT 2M priced under one model
    assert rows[0]["cost"] == pytest.approx(30.0)


def test_birth_boundary_exact_timestamp_kept(tmp_path):
    import datetime
    boundary = datetime.datetime.fromisoformat(T.format(20)).timestamp()
    main = _write(tmp_path / "s.jsonl", [
        _asst("m1", ts=T.format(20), out=9),
    ])
    r = build_report(main, started_at=boundary)
    assert r["tokens"]["main"]["out_tokens"] == 9   # == started_at is KEPT
    assert r["excluded_replayed"] == 0


def test_sidecar_file_records_are_subagent_even_without_flag(tmp_path):
    main = _write(tmp_path / "s.jsonl", [_asst("m1", ts=T.format(0), out=1)])
    subdir = tmp_path / "s" / "subagents"
    subdir.mkdir(parents=True)
    sub = _write(subdir / "agent-a.jsonl", [
        _asst("s-unique", ts=T.format(1), out=40),   # no isSidechain flag
    ])
    r = build_report(main, subagent_logs=[sub])
    assert r["tokens"]["main"]["out_tokens"] == 1
    assert r["tokens"]["subagents"]["out_tokens"] == 40


def test_replayed_tool_use_id_not_double_counted(tmp_path):
    rec = _asst("m1", ts=T.format(1), stop="tool_use",
                tool_uses=[("t1", "Bash")])
    main = _write(tmp_path / "s.jsonl", [
        _user(ts=T.format(0), text="go"),
        rec, rec,                                  # verbatim replay
        _user(ts=T.format(5), tool_results=("t1",)),
    ])
    r = build_report(main)
    bash = next(t for t in r["tools"] if t["tool"] == "Bash")
    assert bash["calls"] == 1
    assert bash["timed"] == 1
    assert bash["total_s"] == pytest.approx(4.0)


def test_episode_duration_includes_trailing_null_stop_record(tmp_path):
    main = _write(tmp_path / "s.jsonl", [
        _user(ts=T.format(0), text="go"),
        _asst("m1", ts=T.format(1), stop="end_turn"),
        _asst("m2", ts=T.format(7), stop=None),    # trailing null-stop record
        # post-stop idle bookkeeping (attachment/system/queue-op records) must
        # NOT extend the episode:
        {"type": "attachment", "timestamp": T.format(55)},
    ])
    r = build_report(main)
    assert r["episodes"][0]["duration_s"] == pytest.approx(7.0)


def test_server_tool_use_requests_surfaced(tmp_path):
    dup = _asst("m1", ts=T.format(0),
                usage_extra={"server_tool_use": {"web_search_requests": 2}})
    main = _write(tmp_path / "s.jsonl", [
        dup, dup,                                  # same message id: counts ONCE
        _asst("m2", ts=T.format(1),
              usage_extra={"server_tool_use": {"web_search_requests": 0,
                                               "web_fetch_requests": 0}}),
    ])
    r = build_report(main)
    assert r["server_tool_use"] == {"web_search_requests": 2}  # zeros dropped


# --- _render_text ------------------------------------------------------


def test_wall_clock_shows_record_count_when_split_response_present(tmp_path):
    from dockwright.spend_session import _render_text
    main = _write(tmp_path / "s.jsonl", [
        _asst("dup", ts=T.format(0), out=1),
        _asst("dup", ts=T.format(1), out=1),   # split response: same id twice
    ])
    r = build_report(main)
    assert r["api_calls"] == {"unique": 1, "records": 2}
    text = _render_text(r)
    assert "1 API calls (2 records)" in text


def test_wall_clock_omits_record_count_without_duplication(tmp_path):
    from dockwright.spend_session import _render_text
    main = _write(tmp_path / "s.jsonl", [_asst("m1", ts=T.format(0), out=1)])
    r = build_report(main)
    assert r["api_calls"] == {"unique": 1, "records": 1}
    text = _render_text(r)
    # "records)" is what an unconditional count actually renders (" (1 records)");
    # the old "(records" form was unproducible and could never fail.
    assert "records)" not in text


def test_tools_block_notes_unpaired_or_untimed_calls(tmp_path):
    from dockwright.spend_session import _render_text
    main = _write(tmp_path / "s.jsonl", [
        _user(ts=T.format(0), text="go"),
        _asst("m1", ts=T.format(1), stop="tool_use",
              tool_uses=[("t1", "Bash"), ("t2", "Bash")]),
        _user(ts=T.format(5), tool_results=("t1",)),   # t2 unpaired
        _asst("m2", ts=T.format(6), stop="end_turn"),
    ])
    r = build_report(main)
    text = _render_text(r)
    assert ("note: 1 tool call(s) unpaired or un-timestamped — counted in "
            "calls, excluded from total-s/avg-s") in text


def test_tools_block_omits_unpaired_note_when_fully_paired(tmp_path):
    from dockwright.spend_session import _render_text
    main = _write(tmp_path / "s.jsonl", [
        _user(ts=T.format(0), text="go"),
        _asst("m1", ts=T.format(1), stop="tool_use",
              tool_uses=[("t1", "Bash")]),
        _user(ts=T.format(2), tool_results=("t1",)),
        _asst("m2", ts=T.format(3), stop="end_turn"),
    ])
    r = build_report(main)
    text = _render_text(r)
    assert "unpaired or un-timestamped" not in text


def test_fmt_duration_rejects_negative_seconds():
    from dockwright.spend_session import _fmt_duration
    assert _fmt_duration(-1.5) == "-"


# --- resolution + CLI ------------------------------------------------------


@pytest.fixture
def world(tmp_path, monkeypatch):
    from dockwright import paths, spend_report
    root = tmp_path / "dockwright"
    monkeypatch.setattr(paths, "ROOT", root)
    monkeypatch.setattr(paths, "ACTIVE", root / "active")
    monkeypatch.setattr(paths, "CLOSED", root / "closed")
    monkeypatch.setattr(paths, "SPEND_LEDGER", root / "spend-ledger.jsonl")
    monkeypatch.setattr(spend_report, "GARDENER_LEDGER",
                        root / "gardener-ledger.jsonl")
    (root / "active").mkdir(parents=True)
    (root / "closed").mkdir(parents=True)
    projects = tmp_path / "home" / ".claude" / "projects" / "-proj"
    projects.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return {"root": root, "projects": projects}


def _closed(world, sid, name, *, closed_at=1785400000.0, started_at=None,
            transcript_path=None, runtime="claude"):
    record = {"claude_sid": sid, "name": name, "closed_at": closed_at,
              "closed_reason": "idle>7200s", "runtime": runtime,
              "started_at": started_at, "transcript_path": transcript_path}
    (world["root"] / "closed" / f"{sid}.json").write_text(json.dumps(record))
    return record


def _epoch(second):
    import datetime
    return datetime.datetime.fromisoformat(T.format(second)).timestamp()


def test_resolve_by_sid_and_by_name_latest_wins(world, capsys):
    from dockwright.spend_session import resolve_session
    _write(world["projects"] / "aaa1.jsonl", [_asst("m1", ts=T.format(0))])
    _write(world["projects"] / "bbb2.jsonl", [_asst("m2", ts=T.format(1))])
    _closed(world, "aaa1", "my-task", closed_at=100.0)
    _closed(world, "bbb2", "my-task", closed_at=200.0)
    assert resolve_session("aaa1")["sid"] == "aaa1"
    picked = resolve_session("my-task")
    assert picked["sid"] == "bbb2"          # latest wins
    assert "other session" in capsys.readouterr().err  # ambiguity noted


def test_resolve_prefers_recorded_transcript_path_else_find(world, tmp_path):
    from dockwright.spend_session import resolve_session
    log = _write(world["projects"] / "ccc3.jsonl", [_asst("m1", ts=T.format(0))])
    _closed(world, "ccc3", "old-worker", transcript_path=None)  # pre-#242 record
    resolved = resolve_session("ccc3")
    assert resolved["log"] == log            # report-time re-resolution
    # a recorded path is trusted over the by-sid search
    moved = _write(tmp_path / "moved.jsonl", [_asst("m2", ts=T.format(0))])
    _closed(world, "ccc3", "old-worker", transcript_path=str(moved))
    assert resolve_session("ccc3")["log"] == moved


def test_resolve_falls_back_to_active_records(world):
    from dockwright.spend_session import resolve_session
    _write(world["projects"] / "live1.jsonl", [_asst("m1", ts=T.format(0))])
    (world["root"] / "active" / "live1.json").write_text(json.dumps({
        "claude_sid": "live1", "name": "live-worker", "runtime": "claude"}))
    resolved = resolve_session("live-worker")
    assert resolved["sid"] == "live1"
    assert resolved["log"] == world["projects"] / "live1.jsonl"


def _live(world, sid, name, started_at):
    _write(world["projects"] / f"{sid}.jsonl", [_asst("m1", ts=T.format(0))])
    (world["root"] / "active" / f"{sid}.json").write_text(json.dumps({
        "claude_sid": sid, "name": name, "runtime": "claude",
        "started_at": started_at}))


def test_two_live_records_same_name_tiebreak_on_started_at(world):
    """Both live records share the synthetic 'newest' recency, so only the
    started_at tiebreak decides. Asserted BOTH ways round with the fixture
    files unchanged — without the tiebreak the winner is whatever order the
    directory happens to list, so one of the two assertions must fail."""
    from dockwright.spend_session import resolve_session
    _live(world, "liveA", "twin", 100.0)
    _live(world, "liveB", "twin", 900.0)
    assert resolve_session("twin")["sid"] == "liveB"
    _live(world, "liveA", "twin", 5000.0)      # same files; A now started later
    assert resolve_session("twin")["sid"] == "liveA"


def test_resolve_bare_sid_with_no_records_at_all(world, capsys):
    """Post-prune: the closed/ record is gone but the transcript survives, so
    the sid is still reportable straight off ~/.claude/projects."""
    from dockwright.spend_session import resolve_session, run
    log = _write(world["projects"] / "orph1.jsonl",
                 [_asst("m1", ts=T.format(0), out=5)])
    resolved = resolve_session("orph1")
    assert resolved["sid"] == "orph1" and resolved["log"] == log
    assert resolved["name"] is None
    assert run(["orph1"]) == 0
    assert "MONEY" in capsys.readouterr().out


def test_active_and_closed_records_merge_newest_field_wins(world, tmp_path):
    from dockwright.spend_session import resolve_session
    recorded = _write(tmp_path / "kept.jsonl", [_asst("m1", ts=T.format(0))])
    _closed(world, "mrg1", "old-name", closed_at=100.0, started_at=1.0,
            transcript_path=str(recorded))
    (world["root"] / "active" / "mrg1.json").write_text(json.dumps({
        "claude_sid": "mrg1", "name": "live-name", "runtime": "claude",
        "started_at": 500.0}))          # live record carries no transcript_path
    resolved = resolve_session("mrg1")
    assert resolved["name"] == "live-name"    # live wins where it has a value
    assert resolved["started_at"] == 500.0
    assert resolved["log"] == recorded        # closed fills the gap it left


def test_run_e2e_text_output_money_headline(world, capsys):
    from dockwright.spend_session import run
    _write(world["projects"] / "ddd4.jsonl", [
        _user(ts=T.format(0), text="go"),
        _asst("m1", ts=T.format(1), out=1_000_000, stop="end_turn"),
    ])
    subdir = world["projects"] / "ddd4" / "subagents"
    subdir.mkdir(parents=True)
    _write(subdir / "agent-a.jsonl", [_asst("s1", ts=T.format(2), out=10)])
    _closed(world, "ddd4", "worker-x", transcript_path=None)
    assert run(["worker-x"]) == 0
    out = capsys.readouterr().out
    assert "MONEY" in out and "$" in out
    assert out.index("MONEY") < out.index("TOKENS")   # money leads
    assert "prompt-episode" in out                     # never a bare "turn"
    assert "prices:" in out
    assert "1 subagent transcript(s)" in out           # sidecar discovered by run
    # tokens line carries $ beside it
    tokens_line = next(l for l in out.splitlines() if l.startswith("TOKENS"))
    assert "$" in tokens_line
    assert "$25.00" in out                             # opus out 1M * $25/MTok
    # D5 header: name, sid8, and the session's date
    assert re.match(r"Session spend — worker-x \(ddd4…\) · \d{4}-\d{2}-\d{2}$",
                    out.splitlines()[0])


def test_tools_column_fits_a_long_mcp_tool_name(world, tmp_path, capsys):
    from dockwright.spend_session import run
    long_name = "mcp__playwright__browser_take_screenshot"
    log = _write(tmp_path / "tools.jsonl", [
        _user(ts=T.format(0), text="go"),
        _asst("m1", ts=T.format(1), stop="tool_use", tool_uses=[("t1", long_name)]),
        _user(ts=T.format(2), tool_results=("t1",)),
    ])
    assert run(["--transcript", str(log)]) == 0
    out = capsys.readouterr().out.splitlines()
    header = next(l for l in out if l.startswith("TOOLS"))
    row = next(l for l in out if long_name in l)
    assert long_name in row                       # never truncated
    # ...and the name still fits its column, so the counts stay under the header
    calls_col = header.index("calls") + len("calls")
    assert row[:calls_col].endswith("1")


def test_render_handles_epoch_zero_timestamps(world, tmp_path, capsys):
    """Timestamp presence is `is not None`, not truthiness: epoch 0 is a real
    instant and must render as one, not as the missing-value dash."""
    from dockwright.spend_session import run
    epoch0 = "1970-01-01T00:00:00.000Z"
    log = _write(tmp_path / "zero.jsonl", [
        _user(ts=epoch0, text="go"),
        _asst("m1", ts=epoch0, out=5, stop="end_turn"),
    ])
    assert run(["--transcript", str(log)]) == 0
    out = capsys.readouterr().out.splitlines()
    wall = next(l for l in out if l.startswith("WALL CLOCK"))
    assert "(- →" not in wall and "→ -)" not in wall
    episode_row = out[out.index(next(l for l in out if l.startswith("EPISODES"))) + 1]
    assert "-" not in episode_row                  # start rendered, not dashed


def test_render_prices_line_lists_only_the_rates_actually_used(world, capsys):
    from dockwright.spend_session import run
    _write(world["projects"] / "prc1.jsonl", [
        _asst("m1", ts=T.format(0), model="claude-opus-5", out=10)])
    _closed(world, "prc1", "worker-p")
    assert run(["prc1"]) == 0
    prices = next(l for l in capsys.readouterr().out.splitlines()
                  if "prices:" in l)
    assert "opus in $5/M out $25/M" in prices
    assert "haiku" not in prices and "sonnet" not in prices


def test_render_footnotes_server_tool_use_as_unpriced(world, capsys):
    from dockwright.spend_session import run
    log = _write(world["projects"] / "stu1.jsonl", [
        _asst("m1", ts=T.format(0),
              usage_extra={"server_tool_use": {"web_search_requests": 2}})])
    assert run(["--transcript", str(log)]) == 0
    assert ("note: server tool use not priced: web_search_requests=2"
            in capsys.readouterr().out)


def test_run_json_output(world, capsys):
    from dockwright.spend_session import run
    _write(world["projects"] / "eee5.jsonl", [_asst("m1", ts=T.format(0))])
    _closed(world, "eee5", "worker-j")
    assert run(["eee5", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["money"]["total"] >= 0
    assert data["prices"]["source"]
    assert data["session"] == {"name": "worker-j", "sid": "eee5"}


def test_json_carries_both_episode_and_end_turn_counts(world, capsys):
    """Episodes and end_turns diverge BY DESIGN, so the JSON must carry both:
    two prompts open two episodes, while three end_turn stops land inside them
    (the trailing end_turn opens no episode — no prompt precedes it). Equal
    counts would let either be mistaken for the other downstream."""
    from dockwright.spend_session import run
    _write(world["projects"] / "epc1.jsonl", [
        _user(ts=T.format(0), text="go"),
        _asst("m1", ts=T.format(1), stop="tool_use", tool_uses=[("t1", "Bash")]),
        _user(ts=T.format(2), tool_results=("t1",)),
        _asst("m2", ts=T.format(3), stop="end_turn"),
        _user(ts=T.format(4), text="again"),
        _asst("m3", ts=T.format(5), stop="end_turn"),
        _asst("m4", ts=T.format(6), stop="end_turn"),   # consecutive, no prompt
    ])
    _closed(world, "epc1", "worker-e")
    assert run(["epc1", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["episode_count"] == 2
    assert data["end_turn_count"] == 3


def test_ambiguous_name_surfaces_the_alternatives_in_json(world, capsys):
    """The stderr note is invisible to a --json consumer piping stdout, so the
    picked sid and the set it was picked from ship in the payload too."""
    from dockwright.spend_session import run
    _write(world["projects"] / "aaa1.jsonl", [_asst("m1", ts=T.format(0))])
    _write(world["projects"] / "bbb2.jsonl", [_asst("m2", ts=T.format(1))])
    _closed(world, "aaa1", "my-task", closed_at=100.0)
    _closed(world, "bbb2", "my-task", closed_at=200.0)
    assert run(["my-task", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["resolution"]["picked_sid"] == "bbb2"
    assert data["resolution"]["ambiguous_sids"] == ["bbb2", "aaa1"]  # newest first


def test_unambiguous_name_carries_no_resolution_block(world, capsys):
    from dockwright.spend_session import run
    _write(world["projects"] / "solo1.jsonl", [_asst("m1", ts=T.format(0))])
    _closed(world, "solo1", "solo")
    assert run(["solo", "--json"]) == 0
    assert "resolution" not in json.loads(capsys.readouterr().out)


def test_run_include_replayed_flag_widens_the_birth_filter(world, capsys):
    from dockwright.spend_session import run
    _write(world["projects"] / "rep1.jsonl", [
        _asst("old", ts=T.format(0), out=500),
        _asst("new", ts=T.format(30), out=7),
    ])
    _closed(world, "rep1", "worker-r", started_at=_epoch(20))
    assert run(["rep1", "--json"]) == 0
    trimmed = json.loads(capsys.readouterr().out)
    assert trimmed["tokens"]["main"]["out_tokens"] == 7
    assert trimmed["excluded_replayed"] == 1
    assert run(["rep1", "--include-replayed", "--json"]) == 0
    full = json.loads(capsys.readouterr().out)
    assert full["tokens"]["main"]["out_tokens"] == 507


def test_run_unknown_session_exits_2(world, capsys):
    from dockwright.spend_session import run
    assert run(["nope"]) == 2
    assert "not" in capsys.readouterr().err.lower()


def test_run_with_no_arguments_exits_2(world, capsys):
    from dockwright.spend_session import run
    assert run([]) == 2
    assert "required" in capsys.readouterr().err


def test_run_known_session_with_pruned_transcript_exits_2(world, capsys):
    from dockwright.spend_session import run
    _closed(world, "gone7", "worker-g")          # record kept, transcript pruned
    assert run(["worker-g"]) == 2
    assert "transcript not found" in capsys.readouterr().err


def test_run_transcript_flag_reports_arbitrary_file(world, tmp_path, capsys):
    from dockwright.spend_session import run
    log = _write(tmp_path / "any.jsonl", [_asst("m1", ts=T.format(0), out=5)])
    assert run(["--transcript", str(log)]) == 0
    assert "MONEY" in capsys.readouterr().out


def test_run_missing_transcript_flag_target_exits_2(world, tmp_path, capsys):
    from dockwright.spend_session import run
    assert run(["--transcript", str(tmp_path / "absent.jsonl")]) == 2
    assert "transcript not found" in capsys.readouterr().err


def test_codex_session_says_no_usage(world, capsys):
    from dockwright.spend_session import run
    _closed(world, "fff6", "codex-w", runtime="codex")
    assert run(["codex-w"]) == 0                 # short-circuits before the log
    assert "no usage data" in capsys.readouterr().out


def test_codex_json_mode_emits_json_not_prose(world, capsys):
    """--json must never put prose on stdout: a consumer parses it blind."""
    from dockwright.spend_session import run
    _closed(world, "fff6", "codex-w", runtime="codex")
    assert run(["codex-w", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)   # prose here would raise
    assert data["runtime"] == "codex"
    assert data["session"] == {"name": "codex-w", "sid": "fff6"}
    assert "no usage data" in data["note"]


def test_fleet_mode_untouched_and_positional_dispatches(world, capsys):
    from dockwright import spend_report
    assert spend_report.main([]) == 0
    out = capsys.readouterr().out
    assert "Spend report" in out           # fleet mode unchanged
    _write(world["projects"] / "abc9.jsonl", [_asst("m1", ts=T.format(0))])
    _closed(world, "abc9", "worker-d")
    assert spend_report.main(["abc9"]) == 0
    assert "MONEY" in capsys.readouterr().out


def test_fleet_window_flags_with_a_session_error_exit_2(world, capsys):
    from dockwright import spend_report
    _write(world["projects"] / "abc9.jsonl", [_asst("m1", ts=T.format(0))])
    _closed(world, "abc9", "worker-d")
    assert spend_report.main(["abc9", "--days", "3"]) == 2
    assert "fleet-mode" in capsys.readouterr().err
    assert spend_report.main(["abc9", "--since", "2026-07-01"]) == 2
    assert "fleet-mode" in capsys.readouterr().err
    log = world["projects"] / "abc9.jsonl"
    assert spend_report.main(["--transcript", str(log), "--days", "3"]) == 2
    assert "fleet-mode" in capsys.readouterr().err


def test_render_survives_a_transcript_with_no_usage(world, tmp_path, capsys):
    from dockwright.spend_session import run
    log = tmp_path / "empty.jsonl"
    log.write_text("")
    assert run(["--transcript", str(log)]) == 0
    out = capsys.readouterr().out
    assert "TOTAL $0.00" in out
    assert "prices:" in out          # no priced rows → the full table, not a crash
    assert "·" not in out.splitlines()[0]   # no timestamps → no date element


def test_include_replayed_without_a_session_is_a_fleet_mode_error(world, capsys):
    from dockwright import spend_report
    assert spend_report.main(["--include-replayed"]) == 2
    assert "per-session" in capsys.readouterr().err


def test_spend_report_forwards_session_flags(world, capsys):
    from dockwright import spend_report
    log = _write(world["projects"] / "fwd1.jsonl", [_asst("m1", ts=T.format(0))])
    assert spend_report.main(["--transcript", str(log), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["transcript"]["path"] == str(log)


def test_spend_report_forwards_include_replayed_with_a_session(world, capsys):
    from dockwright import spend_report
    _write(world["projects"] / "fwd2.jsonl", [
        _asst("old", ts=T.format(0), out=500),
        _asst("new", ts=T.format(30), out=7),
    ])
    _closed(world, "fwd2", "worker-f", started_at=_epoch(20))
    assert spend_report.main(["fwd2", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["excluded_replayed"] == 1
    assert spend_report.main(["fwd2", "--include-replayed", "--json"]) == 0
    full = json.loads(capsys.readouterr().out)
    assert full["excluded_replayed"] == 0            # the flag reached run()
    assert full["tokens"]["main"]["out_tokens"] == 507


def test_leading_dash_session_name_survives_the_forwarding(world, capsys):
    """`--` must be forwarded AFTER the flags and BEFORE the name, or a
    dash-leading name reaches run()'s parser as an unknown option."""
    from dockwright import spend_report
    assert spend_report.main(["--", "some-name"]) == 2
    assert "session not found" in capsys.readouterr().err
    assert spend_report.main(["--", "-dashy"]) == 2
    assert "session not found" in capsys.readouterr().err   # not a usage error
    # flags must stay in front of the escape, or they become positionals
    assert spend_report.main(["--json", "--", "-dashy"]) == 2
    assert "session not found" in capsys.readouterr().err


def test_nested_unknown_usage_field_reaches_report_note(tmp_path):
    # F1 (PR #253 Tier-2): the nested-key loud-fail must flow end-to-end — a
    # new cache TTL injected inside cache_creation surfaces in the report.
    main = _write(tmp_path / "s.jsonl", [
        _asst("m1", ts=T.format(0), out=5,
              cc={"ephemeral_5m_input_tokens": 100,
                  "ephemeral_1h_input_tokens": 0,
                  "ephemeral_2h_input_tokens": 50000}),
    ])
    r = build_report(main)
    assert "cache_creation.ephemeral_2h_input_tokens" in r["unknown_usage_fields"]
    from dockwright.spend_session import _render_text
    assert "cache_creation.ephemeral_2h_input_tokens" in _render_text(r)


def test_recount_and_report_share_the_same_token_source(tmp_path):
    # R5 pin (PR #253 Tier-2 round 3): one quantity, two consumers — the
    # Stop-hook accountant and the session report must see the SAME events
    # (main + sidecars, global dedup, main first). Third drift of a duplicated
    # quantity today; this pin is the part that stops a fourth. If either
    # side's acquisition changes (a path stops globbing sidecars, dedup scope
    # shifts), these equalities go red.
    from dockwright.transcript import recount_spend, subagent_logs_for
    main = _write(tmp_path / "s.jsonl", [
        _user(ts=T.format(0), text="go"),
        _asst("m1", ts=T.format(1), out=100, cin=3, ccr=1000,
              cc={"ephemeral_5m_input_tokens": 20,
                  "ephemeral_1h_input_tokens": 30}),
        _asst("m1", ts=T.format(2), out=100, cin=3, ccr=1000,
              cc={"ephemeral_5m_input_tokens": 20,
                  "ephemeral_1h_input_tokens": 30}),      # split-response dup
    ])
    subdir = tmp_path / "s" / "subagents"
    subdir.mkdir(parents=True)
    _write(subdir / "agent-a.jsonl", [
        _asst("s1", ts=T.format(3), out=40, ccr=200, sidechain=True),
        _asst("m1", ts=T.format(4), out=100, sidechain=True),  # cross-file dup
    ])
    _write(subdir / "agent-b.jsonl", [
        _asst("s2", ts=T.format(5), out=7, model="claude-sonnet-5",
              sidechain=True),
    ])
    # BOTH sides run their production DEFAULT path (round-4 alignment: None
    # means auto-discover in build_report and recount_spend alike) — mutating
    # either side's discovery to a private/empty glob goes red. Non-vacuity
    # control: the shared helper genuinely resolves both files.
    assert len(subagent_logs_for(main)) == 2
    report = build_report(main)
    assert report["transcript"]["subagent_files"] == 2
    spend = recount_spend(main, None)          # auto-discovers the same files
    r_main, r_sub = report["tokens"]["main"], report["tokens"]["subagents"]
    assert spend["out_tokens"] == r_main["out_tokens"] + r_sub["out_tokens"] == 147
    assert spend["in_tokens"] == r_main["in_tokens"] + r_sub["in_tokens"]
    assert (spend["cache_read_tokens"]
            == r_main["cache_read_tokens"] + r_sub["cache_read_tokens"] == 1200)
    assert spend["cache_creation_tokens"] == sum(
        bucket["cache_creation_5m_tokens"] + bucket["cache_creation_1h_tokens"]
        for bucket in (r_main, r_sub)) == 50
    # per-model buckets agree with the report's model rows too
    report_by_model = {row["model"]: row for row in report["money"]["by_model"]}
    for model, bucket in spend["by_model"].items():
        assert bucket["out_tokens"] == report_by_model[model]["out_tokens"]


def test_build_report_auto_discovers_sidecars_like_recount(tmp_path):
    # Round-4 alignment: same param name, same None default, SAME meaning in
    # both money functions — build_report(log) must see exactly what
    # recount_spend(log, None) sees (the sibling's opposite default measured
    # a 27% under-count). Explicit [] scopes to main, identically.
    main = _write(tmp_path / "s.jsonl", [
        _asst("m1", ts=T.format(0), out=100),
    ])
    subdir = tmp_path / "s" / "subagents"
    subdir.mkdir(parents=True)
    _write(subdir / "agent-a.jsonl", [
        _asst("s1", ts=T.format(1), out=40, sidechain=True),
    ])
    r = build_report(main)                              # None -> discover
    assert r["tokens"]["subagents"]["out_tokens"] == 40
    assert r["transcript"]["subagent_files"] == 1
    scoped = build_report(main, subagent_logs=[])       # [] -> main only
    assert scoped["tokens"]["subagents"]["out_tokens"] == 0
    assert scoped["transcript"]["subagent_files"] == 0
