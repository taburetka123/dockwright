import json

import pytest

from dockwright.spend_codex import (
    build_codex_report, no_usage_report, parse_rollout, render_codex_text,
    report_from_spend)
from dockwright.transcript import (
    codex_subagent_rollouts, codex_usage_of, codex_usage_totals)

TS = "2026-09-24T10:17:{:02d}.000Z"


def _usage(inp=0, cached=0, write=0, out=0, reasoning=0):
    return {"input_tokens": inp, "cached_input_tokens": cached,
            "cache_write_input_tokens": write, "output_tokens": out,
            "reasoning_output_tokens": reasoning, "total_tokens": inp + out}


def _meta(thread_id, *, parent=None, spawn_parent=None, forked_from=None, second=0):
    payload = {"id": thread_id, "session_id": parent or thread_id,
               "cli_version": "0.153.2", "originator": "codex_exec"}
    if parent is not None:
        payload["parent_thread_id"] = parent
        payload["source"] = {"subagent": {"other": "guardian"}}
    if spawn_parent is not None:
        payload["source"] = {"subagent": {"thread_spawn": {
            "parent_thread_id": spawn_parent, "depth": 1}}}
    if forked_from is not None:
        payload["forked_from_id"] = forked_from
    return {"timestamp": TS.format(second), "type": "session_meta", "payload": payload}


def _ctx(model, second=0):
    return {"timestamp": TS.format(second), "type": "turn_context",
            "payload": {"model": model}}


def _tc(total, last, *, second=0, limit_id="codex"):
    return {"timestamp": TS.format(second), "type": "event_msg",
            "payload": {"type": "token_count",
                        "info": {"total_token_usage": total,
                                 "last_token_usage": last,
                                 "model_context_window": 258400},
                        "rate_limits": {"limit_id": limit_id}}}


def _tc_null(second=0):
    return {"timestamp": TS.format(second), "type": "event_msg",
            "payload": {"type": "token_count", "info": None,
                        "rate_limits": {"limit_id": "codex"}}}


def _running(lasts, *, start=0):
    events, total = [], _usage()
    for i, last in enumerate(lasts):
        total = {key: total[key] + last[key] for key in last}
        events.append(_tc(total, last, second=start + i))
    return events


def _write(path, records, *, junk=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in records] + list(junk)
    path.write_text("".join(line + "\n" for line in lines))
    return path


def _rollout(root, thread_id, records, *, day="2026/09/24",
             stamp="2026-09-24T17-16-59"):
    return _write(root / day / f"rollout-{stamp}-{thread_id}.jsonl", records)


def test_parse_rollout_sums_each_advancing_event(tmp_path):
    a, b = _usage(100, 40, 0, 7, 3), _usage(200, 150, 0, 9, 1)
    path = _write(tmp_path / "r.jsonl",
                  [_meta("t1"), _ctx("gpt-5.5", 1), *_running([a, b], start=2)])
    parsed = parse_rollout(path)
    assert parsed["events"] == 2
    assert [c["in_tokens"] for c in parsed["calls"]] == [60, 50]
    assert [c["cache_read_tokens"] for c in parsed["calls"]] == [40, 150]
    assert [c["out_tokens"] for c in parsed["calls"]] == [7, 9]
    assert [c["reasoning_tokens"] for c in parsed["calls"]] == [3, 1]
    assert {c["model"] for c in parsed["calls"]} == {"gpt-5.5"}


@pytest.mark.parametrize("limit_id", ["premium", "codex"])
def test_parse_rollout_skips_an_event_that_repeats_the_previous_total(tmp_path, limit_id):
    first, second = _running([_usage(100, 0, 0, 5), _usage(300, 0, 0, 6)])
    info = second["payload"]["info"]
    repeat = _tc(info["total_token_usage"], info["last_token_usage"],
                 limit_id=limit_id)
    parsed = parse_rollout(_write(tmp_path / "r.jsonl", [first, second, repeat]))
    assert parsed["events"] == 3
    assert len(parsed["calls"]) == 2
    assert sum(c["in_tokens"] for c in parsed["calls"]) == 400


def test_parse_rollout_skips_the_post_compaction_marker(tmp_path):
    (first,) = _running([_usage(1000, 900, 0, 10)])
    total = first["payload"]["info"]["total_token_usage"]
    compacted = {"timestamp": TS.format(5), "type": "compacted",
                 "payload": {"message": ""}}
    marker = _tc(total, {**_usage(), "total_tokens": 17741}, second=6)
    b = _usage(500, 0, 0, 4)
    after = _tc({key: total[key] + b[key] for key in b}, b, second=7)
    parsed = parse_rollout(_write(tmp_path / "r.jsonl",
                                  [first, compacted, marker, after]))
    assert parsed["events"] == 3
    assert [c["in_tokens"] for c in parsed["calls"]] == [100, 500]


def test_parse_rollout_keeps_a_resumed_task_whose_total_restarts(tmp_path):
    round1 = _running([_usage(700, 0, 0, 5), _usage(800, 0, 0, 6)])
    settings = {"timestamp": TS.format(29), "type": "event_msg",
                "payload": {"type": "thread_settings_applied"}}
    round2 = _running([_usage(750, 0, 0, 7)], start=30)
    parsed = parse_rollout(_write(tmp_path / "r.jsonl",
                                  [*round1, settings, *round2]))
    assert len(parsed["calls"]) == 3
    assert sum(c["in_tokens"] for c in parsed["calls"]) == 2250


def test_parse_rollout_ignores_null_info_and_malformed_lines(tmp_path):
    (event,) = _running([_usage(10, 0, 0, 1)], start=1)
    info = event["payload"]["info"]
    repeat = _tc(info["total_token_usage"], info["last_token_usage"], second=3)
    path = _write(tmp_path / "r.jsonl", [_tc_null(0), event, _tc_null(2), repeat],
                  junk=["{not json", "[1, 2]", ""])
    parsed = parse_rollout(path)
    assert parsed["events"] == 2
    assert len(parsed["calls"]) == 1


def test_parse_rollout_attributes_each_call_to_the_latest_turn_context_model(tmp_path):
    e1, e2, e3 = _running([_usage(1, 0, 0, 1), _usage(2, 0, 0, 1),
                           _usage(3, 0, 0, 1)])
    path = _write(tmp_path / "r.jsonl",
                  [e1, _ctx("gpt-5.5"), e2, _ctx(""), _ctx("gpt-6-astra"), e3])
    assert [c["model"] for c in parse_rollout(path)["calls"]] == [
        None, "gpt-5.5", "gpt-6-astra"]


def test_parse_rollout_wall_clock_spans_every_timestamped_record(tmp_path):
    done = {"timestamp": TS.format(9), "type": "event_msg",
            "payload": {"type": "task_complete"}}
    path = _write(tmp_path / "r.jsonl",
                  [_meta("t1"), *_running([_usage(1)], start=3), done])
    parsed = parse_rollout(path)
    assert parsed["last_ts"] - parsed["first_ts"] == pytest.approx(9.0)


def test_parse_rollout_of_a_missing_file_is_empty(tmp_path):
    assert parse_rollout(tmp_path / "absent.jsonl") == {
        "calls": [], "events": 0, "first_ts": None, "last_ts": None}


def test_codex_usage_totals_maps_codex_usage_to_spend_keys():
    assert codex_usage_totals(_usage(1000, 600, 5, 80, 30)) == {
        "out_tokens": 80, "in_tokens": 400, "cache_read_tokens": 600,
        "cache_creation_tokens": 5, "reasoning_tokens": 30}
    assert codex_usage_totals({"input_tokens": 10,
                               "cached_input_tokens": 50})["in_tokens"] == 0


def test_codex_usage_of_uses_the_same_mapping():
    usage = _usage(1000, 600, 5, 80, 30)
    assert codex_usage_of({"type": "turn.completed", "usage": usage}) == (
        codex_usage_totals(usage))
    assert codex_usage_of({"type": "turn.started", "usage": usage}) is None


def test_codex_subagent_rollouts_finds_every_descendant_once(tmp_path):
    root = tmp_path / "sessions"
    _rollout(root, "parent", [_meta("parent")])
    guardian = _rollout(root, "guard", [_meta("guard", parent="parent")])
    spawned = _rollout(root, "spawn", [_meta("spawn", spawn_parent="parent")],
                       day="2026/09/25", stamp="2026-09-25T01-00-00")
    grandchild = _rollout(root, "grand", [_meta("grand", parent="spawn")])
    forked = _rollout(root, "fork", [_meta("fork", spawn_parent="parent",
                                           forked_from="parent")])
    _rollout(root, "other", [_meta("other", parent="someone-else")])
    _rollout(root, "review", [{"type": "session_meta", "payload": {
        "id": "review", "source": {"subagent": "review"}}}])
    _write(root / "2026/09/24" / "rollout-2026-09-24T17-16-59-bad.jsonl", [],
           junk=["{not json"])
    found = codex_subagent_rollouts("parent", root=root)
    assert sorted(found) == sorted([(guardian, False), (spawned, False),
                                    (grandchild, False), (forked, True)])
    assert codex_subagent_rollouts("nobody", root=root) == []


def test_codex_subagent_rollouts_of_a_missing_root_is_empty(tmp_path):
    assert codex_subagent_rollouts("parent", root=tmp_path / "absent") == []


def test_codex_subagent_rollouts_defaults_to_the_codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    guardian = _rollout(tmp_path / "codex" / "sessions", "guard",
                        [_meta("guard", parent="parent")])
    assert codex_subagent_rollouts("parent") == [(guardian, False)]


def _family(tmp_path):
    root = tmp_path / "sessions"
    done = {"timestamp": TS.format(40), "type": "event_msg",
            "payload": {"type": "task_complete"}}
    parent = _rollout(root, "parent", [
        _meta("parent"), _ctx("gpt-5.5", 1),
        *_running([_usage(1000, 400, 0, 20, 5), _usage(1200, 900, 0, 30, 6)],
                  start=2),
        done])
    _rollout(root, "guard", [
        _meta("guard", parent="parent", second=10), _ctx("codex-auto-review", 11),
        *_running([_usage(500, 100, 0, 4, 2)], start=12)])
    _rollout(root, "fork", [
        _meta("fork", spawn_parent="parent", forked_from="parent"),
        *_running([_usage(99999, 0, 0, 1)])])
    return root, parent


def test_build_codex_report_splits_main_and_subagents_and_groups_models(tmp_path):
    root, parent = _family(tmp_path)
    report = build_codex_report(parent, name="pe-node", sid="parent",
                                sessions_root=root)
    assert report["runtime"] == "codex" and report["source"] == "rollout"
    assert report["session"] == {"name": "pe-node", "sid": "parent"}
    assert report["tokens"]["main"] == {
        "in_tokens": 900, "cache_read_tokens": 1300, "cache_creation_tokens": 0,
        "out_tokens": 50, "reasoning_tokens": 11, "api_calls": 2}
    assert report["tokens"]["subagents"] == {
        "in_tokens": 400, "cache_read_tokens": 100, "cache_creation_tokens": 0,
        "out_tokens": 4, "reasoning_tokens": 2, "api_calls": 1}
    assert report["transcript"] == {"path": str(parent), "subagent_files": 1,
                                    "forked_subagents_not_counted": 1}
    assert report["api_calls"] == {"unique": 3, "records": 3}
    assert [(row["model"], row["api_calls"]) for row in report["by_model"]] == [
        ("gpt-5.5", 2), ("codex-auto-review", 1)]
    assert report["wall_clock"]["seconds"] == pytest.approx(40.0)
    assert report["money"] is None
    assert "tokens only" in report["note"]


def test_build_codex_report_is_none_when_the_rollout_has_no_token_counts(tmp_path):
    root = tmp_path / "sessions"
    parent = _rollout(root, "parent", [_meta("parent"), _tc_null(1)])
    assert build_codex_report(parent, name="x", sid="parent",
                              sessions_root=root) is None


def test_report_from_spend_labels_the_record_source():
    report = report_from_spend(
        {"turns": 1, "out_tokens": 2387, "in_tokens": 109099,
         "cache_read_tokens": 673280, "cache_creation_tokens": 0,
         "reasoning_tokens": 141, "last_turn_out": 2387, "by_model": {}},
        name="w", sid="s1")
    assert report["source"] == "record"
    assert report["tokens"]["main"] == {
        "in_tokens": 109099, "cache_read_tokens": 673280,
        "cache_creation_tokens": 0, "out_tokens": 2387, "reasoning_tokens": 141}
    assert report["tokens"]["subagents"] is None
    assert report["turns"] == 1
    assert report["money"] is None
    assert "latest round" in report["note"]
    assert "sub-agent threads not included" in report["note"]


@pytest.mark.parametrize("spend", [None, {}, {"turns": 1}, {"in_tokens": "12"},
                                   {"in_tokens": True}])
def test_report_from_spend_without_token_counts_is_none(spend):
    assert report_from_spend(spend, name="w", sid="s1") is None


def test_render_names_both_threads_models_and_the_forked_note(tmp_path):
    root, parent = _family(tmp_path)
    lines = render_codex_text(build_codex_report(
        parent, name="pe-node", sid="parent", sessions_root=root)).splitlines()
    assert lines[0].startswith("Session spend — pe-node (parent…) · ")
    assert lines[0].endswith(" · codex")
    assert lines[1] == f"rollout: {parent} (+1 sub-agent rollout(s))"
    assert ("TOKENS        main: in 900 / cache-rd 1.3k / cache-wr 0 / out 50 "
            "(reasoning 11) [2 calls]") in lines
    assert ("              sub-agents: in 400 / cache-rd 100 / cache-wr 0 / out 4 "
            "(reasoning 2) [1 calls]") in lines
    assert any(l.startswith("  gpt-5.5") and l.endswith("[2 calls]") for l in lines)
    assert any(l.startswith("  codex-auto-review") for l in lines)
    assert "MONEY         not priced — codex models carry no rates (tokens only)" in lines
    assert any(l.startswith("WALL CLOCK    0:00:40.000") and l.endswith("3 API calls")
               for l in lines)
    assert ("note: 1 forked sub-agent thread(s) not counted — a forked rollout "
            "replays its parent's calls") in lines


def test_render_omits_the_subagents_line_when_none_was_counted(tmp_path):
    root = tmp_path / "sessions"
    parent = _rollout(root, "solo", [_meta("solo"),
                                     *_running([_usage(10, 0, 0, 1)])])
    text = render_codex_text(build_codex_report(parent, name="s", sid="solo",
                                                sessions_root=root))
    assert "sub-agents:" not in text
    assert "sub-agent rollout(s)" not in text
    assert "forked" not in text


def test_render_fallback_shows_turns_and_reasoning_only_when_present():
    with_reasoning = render_codex_text(report_from_spend(
        {"turns": 1, "in_tokens": 10, "out_tokens": 2, "reasoning_tokens": 1},
        name="w", sid="s1"))
    assert "rollout:" not in with_reasoning
    assert "note: rollout not found or empty" in with_reasoning
    assert "(reasoning 1) [1 turn(s)]" in with_reasoning
    assert "WALL CLOCK" not in with_reasoning
    assert "sub-agents:" not in with_reasoning
    ledger = render_codex_text(report_from_spend(
        {"turns": 1, "in_tokens": 10, "out_tokens": 2}, name="w", sid="s1"))
    assert "reasoning" not in ledger


def test_render_no_usage_prints_the_note():
    assert render_codex_text(no_usage_report(name="w", sid="s1")) == (
        "codex session carries no usage data — no rollout token counts and no "
        "recorded spend\n")
