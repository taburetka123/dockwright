import json

from dockwright.transcript import recount_spend, sum_usage, sum_usage_by_model


def _usage(output=0, input_tokens=0, cache_read=0, cache_creation=0):
    return {
        "input_tokens": input_tokens,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "output_tokens": output,
        "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
        "service_tier": "standard",
        "cache_creation": {
            "ephemeral_1h_input_tokens": cache_creation,
            "ephemeral_5m_input_tokens": 0,
        },
        "inference_geo": "not_available",
        "speed": "standard",
    }


def _assistant_line(msg_id, usage, text="ok"):
    return json.dumps({
        "type": "assistant",
        "timestamp": "2026-06-11T00:00:00Z",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": "claude-fable-5",
            "content": [{"type": "text", "text": text}],
            "usage": usage,
        },
    })


def _user_line(text="do the thing"):
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}})


def test_sum_usage_totals_whole_file_deduped(tmp_path):
    log = tmp_path / "sid.jsonl"
    log.write_text("\n".join([
        _user_line(),
        _assistant_line("msg_a", _usage(output=100, input_tokens=5, cache_read=1000, cache_creation=20)),
        _assistant_line("msg_a", _usage(output=100, input_tokens=5, cache_read=1000, cache_creation=20)),
        _assistant_line("msg_b", _usage(output=50, input_tokens=2, cache_read=500, cache_creation=10)),
        "not json at all",
    ]) + "\n")
    assert sum_usage(log) == {
        "out_tokens": 150,
        "in_tokens": 7,
        "cache_read_tokens": 1500,
        "cache_creation_tokens": 30,
    }


def test_sum_usage_missing_file_returns_zeros(tmp_path):
    assert sum_usage(tmp_path / "absent.jsonl") == {
        "out_tokens": 0, "in_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
    }


def _model_line(msg_id, model, output=0, input_tokens=0, cache_read=0,
                cache_5m=0, cache_1h=0, structured=True):
    usage = {
        "input_tokens": input_tokens,
        "cache_creation_input_tokens": cache_5m + cache_1h,
        "cache_read_input_tokens": cache_read,
        "output_tokens": output,
    }
    if structured:
        usage["cache_creation"] = {
            "ephemeral_5m_input_tokens": cache_5m,
            "ephemeral_1h_input_tokens": cache_1h,
        }
    return json.dumps({
        "type": "assistant",
        "message": {"id": msg_id, "role": "assistant", "model": model,
                    "content": [{"type": "text", "text": "ok"}], "usage": usage},
    })


def test_sum_usage_by_model_groups_and_splits_ttl(tmp_path):
    log = tmp_path / "sid.jsonl"
    log.write_text("\n".join([
        _user_line(),
        _model_line("m1", "claude-fable-5", output=100, input_tokens=5,
                    cache_read=1000, cache_1h=200),
        _model_line("m2", "claude-fable-5", output=50, cache_5m=10),
        _model_line("m3", "claude-sonnet-4-6", output=7, cache_read=20),
    ]) + "\n")
    by_model = sum_usage_by_model(log)
    assert set(by_model) == {"claude-fable-5", "claude-sonnet-4-6"}
    fable = by_model["claude-fable-5"]
    assert fable["calls"] == 2
    assert fable["out_tokens"] == 150
    assert fable["in_tokens"] == 5
    assert fable["cache_read_tokens"] == 1000
    assert fable["cache_creation_1h_tokens"] == 200
    assert fable["cache_creation_5m_tokens"] == 10
    assert by_model["claude-sonnet-4-6"]["cache_read_tokens"] == 20


def test_sum_usage_by_model_dedupes_split_events(tmp_path):
    log = tmp_path / "sid.jsonl"
    log.write_text("\n".join([
        _model_line("dup", "claude-fable-5", output=3594, cache_1h=112),
        _model_line("dup", "claude-fable-5", output=3594, cache_1h=112),
        _model_line("dup", "claude-fable-5", output=3594, cache_1h=112),
    ]) + "\n")
    fable = sum_usage_by_model(log)["claude-fable-5"]
    assert fable["calls"] == 1
    assert fable["out_tokens"] == 3594
    assert fable["cache_creation_1h_tokens"] == 112


def test_sum_usage_by_model_reads_whole_file_not_tail(tmp_path):
    log = tmp_path / "sid.jsonl"
    lines = [_model_line(f"m{i}", "claude-fable-5", output=1000, cache_1h=1000,
                         input_tokens=0) for i in range(400)]
    log.write_text("\n".join(lines) + "\n")
    assert log.stat().st_size > 65536
    fable = sum_usage_by_model(log)["claude-fable-5"]
    assert fable["calls"] == 400
    assert fable["out_tokens"] == 400_000
    assert fable["cache_creation_1h_tokens"] == 400_000


def test_sum_usage_by_model_flat_cache_creation_falls_back_to_5m(tmp_path):
    log = tmp_path / "sid.jsonl"
    log.write_text(
        _model_line("m1", "claude-fable-5", output=10, cache_5m=300, structured=False) + "\n"
    )
    fable = sum_usage_by_model(log)["claude-fable-5"]
    assert fable["cache_creation_5m_tokens"] == 300
    assert fable["cache_creation_1h_tokens"] == 0


def test_sum_usage_by_model_missing_file_returns_empty(tmp_path):
    assert sum_usage_by_model(tmp_path / "absent.jsonl") == {}


def test_sum_usage_by_model_skips_events_without_model(tmp_path):
    log = tmp_path / "sid.jsonl"
    log.write_text("\n".join([
        json.dumps({"type": "assistant", "message": {"id": "nomodel",
                    "usage": {"output_tokens": 9}}}),
        _model_line("ok", "claude-fable-5", output=1),
    ]) + "\n")
    by_model = sum_usage_by_model(log)
    assert set(by_model) == {"claude-fable-5"}


def _assistant_line_ts(msg_id, usage, timestamp):
    line = json.loads(_assistant_line(msg_id, usage))
    line["timestamp"] = timestamp
    return json.dumps(line)


def test_recount_fresh_counts_whole_file_deduped(tmp_path):
    log = tmp_path / "sid.jsonl"
    log.write_text("\n".join([
        _user_line(),
        _assistant_line("msg_a", _usage(output=100, input_tokens=5, cache_read=1000, cache_creation=20)),
        _assistant_line("msg_a", _usage(output=100, input_tokens=5, cache_read=1000, cache_creation=20)),
        _assistant_line("msg_b", _usage(output=50, input_tokens=2, cache_read=500, cache_creation=10)),
        "not json at all",
    ]) + "\n")
    spend = recount_spend(log, None)
    assert spend == {
        "turns": 1,
        "out_tokens": 150,
        "in_tokens": 7,
        "cache_read_tokens": 1500,
        "cache_creation_tokens": 30,
        "last_turn_out": 150,
        "by_model": {"claude-fable-5": {
            "out_tokens": 150, "in_tokens": 7, "cache_read_tokens": 1500,
            "cache_creation_5m_tokens": 0, "cache_creation_1h_tokens": 30}},
    }


def test_recount_unchanged_file_returns_prior_no_turn_drift(tmp_path):
    log = tmp_path / "sid.jsonl"
    log.write_text(_assistant_line("msg_a", _usage(output=9)) + "\n")
    first = recount_spend(log, None)
    assert first["turns"] == 1
    again = recount_spend(log, first)
    assert again is first


def test_recount_growth_bumps_turn_and_delta(tmp_path):
    log = tmp_path / "sid.jsonl"
    log.write_text(_assistant_line("msg_a", _usage(output=100, input_tokens=1)) + "\n")
    first = recount_spend(log, None)
    with log.open("a") as f:
        f.write(_assistant_line("msg_b", _usage(output=40, input_tokens=2, cache_read=7)) + "\n")
    second = recount_spend(log, first)
    assert second == {
        "turns": 2,
        "out_tokens": 140,
        "in_tokens": 3,
        "cache_read_tokens": 7,
        "cache_creation_tokens": 0,
        "last_turn_out": 40,
        "by_model": {"claude-fable-5": {
            "out_tokens": 140, "in_tokens": 3, "cache_read_tokens": 7,
            "cache_creation_5m_tokens": 0, "cache_creation_1h_tokens": 0}},
    }


def test_recount_counts_beyond_any_window(tmp_path):
    log = tmp_path / "sid.jsonl"
    filler = json.dumps({"type": "user", "message": {"role": "user", "content": "x" * 4096}})
    lines = [_assistant_line("msg_early", _usage(output=1000))]
    lines += [filler] * 50
    lines += [_assistant_line("msg_late", _usage(output=1))]
    log.write_text("\n".join(lines) + "\n")
    spend = recount_spend(log, None)
    assert spend["out_tokens"] == 1001


def test_recount_excludes_replayed_history_before_started_at(tmp_path):
    from datetime import datetime
    born = datetime.fromisoformat("2026-07-28T12:00:00+00:00").timestamp()
    log = tmp_path / "sid.jsonl"
    log.write_text("\n".join([
        _assistant_line_ts("msg_replayed", _usage(output=5000), "2026-07-28T03:00:00Z"),
        _assistant_line_ts("msg_own", _usage(output=70), "2026-07-28T12:00:05Z"),
    ]) + "\n")
    spend = recount_spend(log, None, started_at=born)
    assert spend["out_tokens"] == 70
    assert spend["turns"] == 1


def test_recount_missing_timestamp_counts(tmp_path):
    log = tmp_path / "sid.jsonl"
    event = json.loads(_assistant_line("msg_nots", _usage(output=11)))
    del event["timestamp"]
    bad_ts = _assistant_line_ts("msg_badts", _usage(output=3), "not-a-date")
    log.write_text(json.dumps(event) + "\n" + bad_ts + "\n")
    spend = recount_spend(log, None, started_at=1785000000.0)
    assert spend["out_tokens"] == 14


def test_recount_missing_file_keeps_prior(tmp_path):
    prior = {"turns": 3, "out_tokens": 500, "in_tokens": 5,
             "cache_read_tokens": 100, "cache_creation_tokens": 0, "last_turn_out": 10}
    assert recount_spend(tmp_path / "nope.jsonl", prior) is prior
    assert recount_spend(tmp_path / "nope.jsonl", None) is None


def test_recount_zero_entries_nonzero_prior_keeps_prior(tmp_path):
    log = tmp_path / "sid.jsonl"
    log.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
    prior = {"turns": 3, "out_tokens": 500, "in_tokens": 5,
             "cache_read_tokens": 100, "cache_creation_tokens": 0, "last_turn_out": 10}
    assert recount_spend(log, prior) is prior
    assert recount_spend(log, None) is None


def test_recount_adopts_lower_totals_from_fully_read_file(tmp_path):
    log = tmp_path / "sid.jsonl"
    log.write_text(_assistant_line("msg_only", _usage(output=100)) + "\n")
    prior = {"turns": 9, "out_tokens": 900, "in_tokens": 0,
             "cache_read_tokens": 0, "cache_creation_tokens": 0, "last_turn_out": 5}
    spend = recount_spend(log, prior)
    assert spend["turns"] == 10
    assert spend["out_tokens"] == 100
    assert spend["last_turn_out"] == 0


def test_recount_upgrades_legacy_prior_and_drops_cursor(tmp_path):
    log = tmp_path / "sid.jsonl"
    log.write_text(_assistant_line("msg_a", _usage(output=100)) + "\n")
    prior = {"turns": 42, "out_tokens": 60, "in_tokens": 0, "cache_read_tokens": 0,
             "last_turn_out": 2, "last_msg_id": "msg_gone"}
    spend = recount_spend(log, prior)
    assert spend["turns"] == 43
    assert spend["out_tokens"] == 100
    assert spend["last_turn_out"] == 40
    assert "last_msg_id" not in spend


def test_recount_tolerates_corrupt_prior(tmp_path):
    log = tmp_path / "sid.jsonl"
    log.write_text(_assistant_line("msg_a", _usage(output=8, input_tokens=2)) + "\n")
    spend = recount_spend(log, {"last_msg_id": "msg_x"})
    assert spend["turns"] == 1
    assert spend["out_tokens"] == 8
    assert spend["in_tokens"] == 2


def test_recount_counts_structured_1h_cache_creation(tmp_path):
    log = tmp_path / "sid.jsonl"
    event = json.loads(_assistant_line("msg_1h", _usage(output=10)))
    event["message"]["usage"]["cache_creation_input_tokens"] = 0
    event["message"]["usage"]["cache_creation"] = {
        "ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 1892}
    log.write_text(json.dumps(event) + "\n")
    spend = recount_spend(log, None)
    assert spend["cache_creation_tokens"] == 1892


def _event(mid="m1", model="claude-opus-5", usage=None, **extra):
    e = {"type": "assistant",
         "message": {"id": mid, "model": model,
                     "usage": usage if usage is not None else {"output_tokens": 1}}}
    if model is None:
        del e["message"]["model"]
    e.update(extra)
    return e


def test_modelless_assistant_event_counts_in_recount(tmp_path):
    from dockwright.transcript import recount_spend
    log = tmp_path / "t.jsonl"
    log.write_text(json.dumps(_event(model=None, usage={"output_tokens": 7})) + "\n")
    spend = recount_spend(log, None)
    assert spend["out_tokens"] == 7


def test_sum_usage_keeps_flat_cache_creation_while_recount_uses_ttl_split(tmp_path):
    from dockwright.transcript import recount_spend, sum_usage
    usage = {"output_tokens": 1, "cache_creation_input_tokens": 0,
             "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                "ephemeral_1h_input_tokens": 1000}}
    log = tmp_path / "t.jsonl"
    log.write_text(json.dumps(_event(usage=usage)) + "\n")
    assert recount_spend(log, None)["cache_creation_tokens"] == 1000
    assert sum_usage(log)["cache_creation_tokens"] == 0


def test_usage_totals_of_flags_unknown_usage_keys_any_type():
    from dockwright.transcript import usage_totals_of
    entry = usage_totals_of(_event(usage={"output_tokens": 1,
                                          "banana_tokens": 5,
                                          "new_nested": {"a": 1}}))
    assert entry["unknown_usage_keys"] == ["banana_tokens", "new_nested"]
    known = usage_totals_of(_event(usage={
        "output_tokens": 1, "input_tokens": 2, "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 4, "cache_creation": {},
        "iterations": [], "service_tier": "standard", "speed": "standard",
        "inference_geo": "us", "server_tool_use": {}}))
    assert known["unknown_usage_keys"] == []


def test_usage_totals_of_rejects_malformed_shapes():
    from dockwright.transcript import usage_totals_of
    assert usage_totals_of({"type": "user"}) is None
    assert usage_totals_of({"type": "assistant"}) is None
    assert usage_totals_of({"type": "assistant", "message": {"id": "m",
                            "usage": "nope"}}) is None
    assert usage_totals_of("not a dict") is None
    assert usage_totals_of({"type": "assistant",
                            "message": "not a dict"}) is None
    assert usage_totals_of({"type": "assistant",
                            "message": {"id": "", "usage": {}}}) is None


def test_known_usage_keys_pinned():
    from dockwright.transcript import _KNOWN_USAGE_KEYS
    assert _KNOWN_USAGE_KEYS == frozenset({
        "input_tokens", "output_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "cache_creation", "iterations",
        "service_tier", "speed", "inference_geo", "server_tool_use",
    })


def test_nested_unknown_cache_creation_key_flagged():
    from dockwright.transcript import usage_totals_of
    entry = usage_totals_of(_event(usage={
        "output_tokens": 1,
        "cache_creation": {"ephemeral_5m_input_tokens": 100,
                           "ephemeral_1h_input_tokens": 0,
                           "ephemeral_2h_input_tokens": 50000}}))
    assert entry["unknown_usage_keys"] == ["cache_creation.ephemeral_2h_input_tokens"]
    known = usage_totals_of(_event(usage={
        "output_tokens": 1,
        "cache_creation": {"ephemeral_5m_input_tokens": 100,
                           "ephemeral_1h_input_tokens": 200}}))
    assert known["unknown_usage_keys"] == []


def test_nested_unknown_server_tool_use_key_flagged():
    from dockwright.transcript import usage_totals_of
    entry = usage_totals_of(_event(usage={
        "output_tokens": 1,
        "server_tool_use": {"web_search_requests": 0, "new_thing": 7}}))
    assert entry["unknown_usage_keys"] == ["server_tool_use.new_thing"]
    known = usage_totals_of(_event(usage={
        "output_tokens": 1,
        "server_tool_use": {"web_search_requests": 2, "web_fetch_requests": 0}}))
    assert known["unknown_usage_keys"] == []


def test_nested_known_key_sets_pinned():
    from dockwright.transcript import (_KNOWN_CACHE_CREATION_KEYS,
                                       _KNOWN_SERVER_TOOL_USE_KEYS)
    assert _KNOWN_CACHE_CREATION_KEYS == frozenset({
        "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"})
    assert _KNOWN_SERVER_TOOL_USE_KEYS == frozenset({
        "web_search_requests", "web_fetch_requests"})


def test_recount_by_model_split_and_modelless_totals_only(tmp_path):
    log = tmp_path / "sid.jsonl"
    opus = json.loads(_assistant_line("msg_o", _usage(output=10)))
    opus["message"]["model"] = "claude-opus-5"
    modelless = json.loads(_assistant_line("msg_x", _usage(output=7)))
    del modelless["message"]["model"]
    log.write_text("\n".join([
        _assistant_line("msg_f", _usage(output=100, cache_read=50)),
        json.dumps(opus),
        json.dumps(modelless),
    ]) + "\n")
    spend = recount_spend(log, None)
    assert spend["out_tokens"] == 117
    assert set(spend["by_model"]) == {"claude-fable-5", "claude-opus-5"}
    assert spend["by_model"]["claude-opus-5"]["out_tokens"] == 10
    assert sum(b["out_tokens"] for b in spend["by_model"].values()) == 110


def test_unknown_container_contents_flagged_without_nested_contract(monkeypatch):
    from dockwright import transcript
    monkeypatch.setattr(transcript, "_KNOWN_USAGE_KEYS",
                        transcript._KNOWN_USAGE_KEYS | {"new_container"})
    entry = transcript.usage_totals_of(_json_event(
        {"output_tokens": 1, "new_container": {"x": 1, "y": 2}}))
    assert entry["unknown_usage_keys"] == ["new_container.x", "new_container.y"]


def test_nested_contract_map_pinned_to_known_keys():
    from dockwright.transcript import _KNOWN_USAGE_KEYS, _NESTED_KNOWN_KEYS
    assert set(_NESTED_KNOWN_KEYS) <= _KNOWN_USAGE_KEYS
    assert set(_NESTED_KNOWN_KEYS) == {"cache_creation", "server_tool_use"}


def test_known_key_unexpected_shape_flagged():
    from dockwright.transcript import usage_totals_of
    entry = usage_totals_of(_json_event({
        "output_tokens": "9",
        "cache_creation": [{"ephemeral_5m_input_tokens": 5}],
        "iterations": {"output_tokens": 1},
        "server_tool_use": 3,
    }))
    assert entry["unknown_usage_keys"] == [
        "cache_creation(unexpected-shape)",
        "iterations(unexpected-shape)",
        "output_tokens(unexpected-shape)",
        "server_tool_use(unexpected-shape)",
    ]


def test_known_key_none_values_not_flagged():
    from dockwright.transcript import usage_totals_of
    entry = usage_totals_of(_json_event({
        "output_tokens": None, "cache_creation": None,
        "iterations": None, "server_tool_use": None}))
    assert entry["unknown_usage_keys"] == []


def _json_event(usage):
    return {"type": "assistant",
            "message": {"id": "m1", "model": "claude-opus-5", "usage": usage}}


def test_recount_auto_discovers_subagent_sidecars(tmp_path):
    log = tmp_path / "sid.jsonl"
    log.write_text(_assistant_line("msg_main", _usage(output=100)) + "\n")
    subdir = tmp_path / "sid" / "subagents"
    subdir.mkdir(parents=True)
    (subdir / "agent-a.jsonl").write_text(
        _assistant_line("msg_sub", _usage(output=40)) + "\n"
        + _assistant_line("msg_main", _usage(output=100)) + "\n")
    spend = recount_spend(log, None)
    assert spend["out_tokens"] == 140
    assert spend["by_model"]["claude-fable-5"]["out_tokens"] == 140


def test_recount_explicit_empty_subagent_list_disables_discovery(tmp_path):
    log = tmp_path / "sid.jsonl"
    log.write_text(_assistant_line("msg_main", _usage(output=100)) + "\n")
    subdir = tmp_path / "sid" / "subagents"
    subdir.mkdir(parents=True)
    (subdir / "agent-a.jsonl").write_text(
        _assistant_line("msg_sub", _usage(output=40)) + "\n")
    assert recount_spend(log, None, subagent_logs=[])["out_tokens"] == 100


def test_recount_missing_sidecar_skipped_main_kept(tmp_path):
    log = tmp_path / "sid.jsonl"
    log.write_text(_assistant_line("msg_main", _usage(output=100)) + "\n")
    spend = recount_spend(log, None,
                          subagent_logs=[tmp_path / "gone.jsonl"])
    assert spend["out_tokens"] == 100


def test_nested_known_value_shape_flagged():
    from dockwright.transcript import usage_totals_of
    entry = usage_totals_of(_json_event({
        "output_tokens": 1,
        "cache_creation": {"ephemeral_5m_input_tokens": {"oops": 1},
                           "ephemeral_1h_input_tokens": 5},
        "server_tool_use": {"web_search_requests": "2",
                            "web_fetch_requests": True}}))
    assert entry["unknown_usage_keys"] == [
        "cache_creation.ephemeral_5m_input_tokens(unexpected-shape)",
        "server_tool_use.web_fetch_requests(unexpected-shape)",
        "server_tool_use.web_search_requests(unexpected-shape)",
    ]


def test_nested_known_value_none_and_numeric_not_flagged():
    from dockwright.transcript import usage_totals_of
    entry = usage_totals_of(_json_event({
        "output_tokens": 1,
        "cache_creation": {"ephemeral_5m_input_tokens": None,
                           "ephemeral_1h_input_tokens": 5}}))
    assert entry["unknown_usage_keys"] == []
