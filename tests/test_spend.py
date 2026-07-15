"""Spend telemetry: transcript tail usage parsing + accumulation (observability only)."""
import json

from dockwright.transcript import accumulate_spend, tail_usage_entries, sum_usage
from dockwright.transcript import sum_usage_by_model


def _usage(output=0, input_tokens=0, cache_read=0, cache_creation=0):
    """Real per-turn usage shape as written by Claude Code transcripts (2026-06)."""
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


# --- tail_usage_entries -----------------------------------------------------

def test_tail_usage_extracts_entries_in_order(tmp_path):
    log = tmp_path / "sid.jsonl"
    log.write_text("\n".join([
        _user_line(),
        _assistant_line("msg_a", _usage(output=100, input_tokens=5, cache_read=1000, cache_creation=20)),
        _assistant_line("msg_b", _usage(output=200, input_tokens=2, cache_read=2000)),
    ]) + "\n")
    entries = tail_usage_entries(log)
    assert [e["message_id"] for e in entries] == ["msg_a", "msg_b"]
    assert entries[0]["output_tokens"] == 100
    assert entries[0]["input_tokens"] == 5
    assert entries[0]["cache_read_tokens"] == 1000
    assert entries[0]["cache_creation_tokens"] == 20
    assert entries[1]["output_tokens"] == 200


def test_tail_usage_dedupes_split_events_sharing_message_id(tmp_path):
    # One API response with text + tool_use blocks lands as MULTIPLE assistant
    # events that share message.id and repeat the SAME usage — count once.
    log = tmp_path / "sid.jsonl"
    usage = _usage(output=3594, input_tokens=2, cache_read=515941, cache_creation=112)
    log.write_text("\n".join([
        _assistant_line("msg_dup", usage, text="part one"),
        _assistant_line("msg_dup", usage, text="part two"),
        _assistant_line("msg_dup", usage, text="part three"),
        _assistant_line("msg_other", _usage(output=48)),
    ]) + "\n")
    entries = tail_usage_entries(log)
    assert [e["message_id"] for e in entries] == ["msg_dup", "msg_other"]
    assert entries[0]["output_tokens"] == 3594


def test_tail_usage_skips_malformed_lines(tmp_path):
    log = tmp_path / "sid.jsonl"
    log.write_text("\n".join([
        "not json at all {{{",
        json.dumps(["a", "list", "event"]),
        json.dumps("just a string"),
        json.dumps(None),
        json.dumps({"type": "assistant", "message": None}),
        json.dumps({"type": "assistant", "message": "not-a-dict"}),
        json.dumps({"type": "assistant", "message": {"id": "msg_no_usage", "content": []}}),
        json.dumps({"type": "assistant", "message": {"id": "msg_bad_usage", "usage": "nope"}}),
        _assistant_line("msg_good", _usage(output=7)),
        "",
    ]) + "\n")
    entries = tail_usage_entries(log)
    assert [e["message_id"] for e in entries] == ["msg_good"]
    assert entries[0]["output_tokens"] == 7


def test_tail_usage_skips_assistant_events_without_message_id(tmp_path):
    # No string id → no dedupe / cursor anchor; claude-shape transcripts always
    # carry one, so a missing id is treated as malformed and skipped.
    log = tmp_path / "sid.jsonl"
    event = json.loads(_assistant_line("placeholder", _usage(output=9)))
    del event["message"]["id"]
    log.write_text(json.dumps(event) + "\n" + _assistant_line("msg_ok", _usage(output=1)) + "\n")
    entries = tail_usage_entries(log)
    assert [e["message_id"] for e in entries] == ["msg_ok"]


def test_tail_usage_coerces_missing_usage_fields_to_zero(tmp_path):
    log = tmp_path / "sid.jsonl"
    log.write_text(json.dumps({
        "type": "assistant",
        "message": {"id": "msg_sparse", "usage": {"output_tokens": 12}},
    }) + "\n")
    entries = tail_usage_entries(log)
    assert entries == [{
        "message_id": "msg_sparse",
        "output_tokens": 12,
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }]


def test_tail_usage_coerces_non_numeric_usage_values_to_zero(tmp_path):
    log = tmp_path / "sid.jsonl"
    log.write_text(json.dumps({
        "type": "assistant",
        "message": {"id": "msg_weird", "usage": {
            "output_tokens": "lots", "input_tokens": None, "cache_read_input_tokens": 5,
        }},
    }) + "\n")
    entries = tail_usage_entries(log)
    assert entries[0]["output_tokens"] == 0
    assert entries[0]["input_tokens"] == 0
    assert entries[0]["cache_read_tokens"] == 5


def test_tail_usage_seeks_tail_and_drops_partial_first_line(tmp_path):
    # File bigger than the window: only the tail is read and the first line of
    # the window is dropped as possibly partial (stale_monitor precedent).
    log = tmp_path / "sid.jsonl"
    filler = _assistant_line("msg_old", _usage(output=999_999), text="x" * 2000)
    lines = [filler] * 50 + [_assistant_line("msg_new", _usage(output=42))]
    log.write_text("\n".join(lines) + "\n")
    entries = tail_usage_entries(log, max_bytes=4096)
    assert log.stat().st_size > 4096
    ids = [e["message_id"] for e in entries]
    assert ids[-1] == "msg_new"
    assert "msg_old" not in ids or len(ids) < 50  # never the whole file


def test_tail_usage_missing_file_returns_empty(tmp_path):
    assert tail_usage_entries(tmp_path / "nope.jsonl") == []


def test_tail_usage_ignores_codex_rollout_shape(tmp_path):
    # Claude transcript shape only — codex rollouts have no message.usage.
    log = tmp_path / "rollout.jsonl"
    log.write_text(json.dumps({
        "type": "response_item",
        "payload": {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "hi"}]},
    }) + "\n")
    assert tail_usage_entries(log) == []


# --- accumulate_spend -------------------------------------------------------

def _entry(msg_id, output=0, input_tokens=0, cache_read=0, cache_creation=0):
    return {
        "message_id": msg_id,
        "output_tokens": output,
        "input_tokens": input_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
    }


def test_accumulate_fresh_spend_counts_all_entries():
    entries = [
        _entry("msg_a", output=100, input_tokens=3, cache_read=1000),
        _entry("msg_b", output=200, input_tokens=4, cache_read=2000),
    ]
    spend = accumulate_spend(None, entries)
    assert spend == {
        "turns": 1,
        "out_tokens": 300,
        "in_tokens": 7,
        "cache_read_tokens": 3000,
        "last_turn_out": 300,
        "last_msg_id": "msg_b",
    }


def test_accumulate_counts_only_entries_after_cursor():
    prior = {"turns": 5, "out_tokens": 1000, "in_tokens": 10,
             "cache_read_tokens": 9000, "last_turn_out": 50, "last_msg_id": "msg_b"}
    entries = [
        _entry("msg_a", output=111),
        _entry("msg_b", output=222),
        _entry("msg_c", output=30, input_tokens=1, cache_read=100),
        _entry("msg_d", output=40, input_tokens=2, cache_read=200),
    ]
    spend = accumulate_spend(prior, entries)
    assert spend["turns"] == 6
    assert spend["out_tokens"] == 1070
    assert spend["in_tokens"] == 13
    assert spend["cache_read_tokens"] == 9300
    assert spend["last_turn_out"] == 70
    assert spend["last_msg_id"] == "msg_d"


def test_accumulate_cursor_not_in_tail_counts_whole_tail():
    # Long turn rolled the cursor out of the 64KB window — count what we can
    # see (the part beyond the window is accepted undercount).
    prior = {"turns": 2, "out_tokens": 500, "in_tokens": 5,
             "cache_read_tokens": 100, "last_turn_out": 10, "last_msg_id": "msg_gone"}
    entries = [_entry("msg_x", output=60), _entry("msg_y", output=40)]
    spend = accumulate_spend(prior, entries)
    assert spend["turns"] == 3
    assert spend["out_tokens"] == 600
    assert spend["last_turn_out"] == 100
    assert spend["last_msg_id"] == "msg_y"


def test_accumulate_no_entries_returns_prior_unchanged():
    prior = {"turns": 2, "out_tokens": 500, "in_tokens": 5,
             "cache_read_tokens": 100, "last_turn_out": 10, "last_msg_id": "msg_b"}
    assert accumulate_spend(prior, []) == prior
    assert accumulate_spend(None, []) is None


def test_accumulate_cursor_at_last_entry_is_a_no_op():
    # Stop re-fired with nothing new after the cursor → no turn bump, no drift.
    prior = {"turns": 4, "out_tokens": 800, "in_tokens": 8,
             "cache_read_tokens": 400, "last_turn_out": 20, "last_msg_id": "msg_b"}
    entries = [_entry("msg_a", output=1), _entry("msg_b", output=2)]
    assert accumulate_spend(prior, entries) == prior


def test_accumulate_tolerates_corrupt_prior_spend():
    # A hand-edited / partial record must not crash the fold; missing numeric
    # keys count from zero.
    prior = {"last_msg_id": "msg_a"}
    entries = [_entry("msg_a", output=1), _entry("msg_b", output=9, input_tokens=2, cache_read=3)]
    spend = accumulate_spend(prior, entries)
    assert spend["turns"] == 1
    assert spend["out_tokens"] == 9
    assert spend["in_tokens"] == 2
    assert spend["cache_read_tokens"] == 3
    assert spend["last_msg_id"] == "msg_b"


# --- sum_usage --------------------------------------------------------------

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


# --- sum_usage_by_model -------------------------------------------------

def _model_line(msg_id, model, output=0, input_tokens=0, cache_read=0,
                cache_5m=0, cache_1h=0, structured=True):
    """Assistant line with explicit model + TTL-split cache_creation."""
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
    # > 64KB file: a tail read (SPEND_TAIL_MAX_BYTES) would miss the early turns.
    # The full read must count EVERY turn (bug 1: tail truncation).
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
    # Older transcript: no structured cache_creation object, only the flat field.
    # Attribute the flat total to the 5m bucket (API default TTL; conservative).
    log = tmp_path / "sid.jsonl"
    log.write_text(
        _model_line("m1", "claude-fable-5", output=10, cache_5m=300, structured=False) + "\n"
    )
    # cache_5m=300 with structured=False -> flat cache_creation_input_tokens=300, no object
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
