"""Durable spend ledger: append-at-drop + headless capture + tolerant read."""
import json

import pytest

from dockwright import paths, spend_ledger


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "SPEND_LEDGER", tmp_path / "spend-ledger.jsonl")
    return tmp_path / "spend-ledger.jsonl"


def _record(**overrides):
    record = {
        "claude_sid": "sid-1", "agent": "worker", "name": "alpha",
        "parent_manager_name": "mgr", "runtime": "claude", "started_at": 100.0,
        "spend": {"turns": 3, "out_tokens": 500, "in_tokens": 20,
                  "cache_read_tokens": 9000, "last_turn_out": 100,
                  "last_msg_id": "msg_x"},
    }
    record.update(overrides)
    return record


def test_append_drop_event_writes_one_line_stripping_cursor_fields(ledger):
    spend_ledger.append_drop_event(_record(), "session_end")
    lines = ledger.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["sid"] == "sid-1"
    assert entry["name"] == "alpha"
    assert entry["agent"] == "worker"
    assert entry["source"] == "session_end"
    assert entry["spend"] == {"turns": 3, "out_tokens": 500, "in_tokens": 20,
                              "cache_read_tokens": 9000}
    assert entry["ts"] > 0


def test_append_drop_event_labels_nested(ledger):
    spend_ledger.append_drop_event(_record(nested=True, agent="manager"), "prune")
    entry = json.loads(ledger.read_text())
    assert entry["agent"] == "nested"


def test_append_drop_event_defaults_agentless_closed_record_to_worker(ledger):
    # closed/ records carry no agent key — only workers are archived there.
    record = _record()
    record.pop("agent")
    spend_ledger.append_drop_event(record, "resume_reclaim")
    assert json.loads(ledger.read_text())["agent"] == "worker"


def test_append_drop_event_skips_records_without_spend(ledger):
    spend_ledger.append_drop_event(_record(spend=None), "session_end")
    spend_ledger.append_drop_event(_record(spend={"turns": "junk"}), "session_end")
    spend_ledger.append_drop_event(None, "session_end")
    assert not ledger.exists()


def test_append_drop_event_appends(ledger):
    spend_ledger.append_drop_event(_record(), "session_end")
    spend_ledger.append_drop_event(_record(claude_sid="sid-2"), "prune")
    assert len(ledger.read_text().splitlines()) == 2


def test_append_drop_event_carries_account(ledger):
    spend_ledger.append_drop_event(_record(account="b"), "session_end")
    assert json.loads(ledger.read_text())["account"] == "b"


def test_append_drop_event_account_null_when_absent(ledger):
    # Nested teammates and pre-fix records carry no account -> honest null.
    spend_ledger.append_drop_event(_record(), "session_end")
    assert json.loads(ledger.read_text())["account"] is None


def test_append_headless_event_sums_transcript(ledger, tmp_path):
    transcript = tmp_path / "h.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"id": "m1", "usage": {
            "output_tokens": 70, "input_tokens": 4,
            "cache_read_input_tokens": 100, "cache_creation_input_tokens": 9}},
    }) + "\n")
    spend_ledger.append_headless_event("distill", "h-sid", str(transcript))
    entry = json.loads(ledger.read_text())
    assert entry["agent"] == "headless"
    assert entry["name"] == "distill"
    assert entry["sid"] == "h-sid"
    assert entry["source"] == "headless"
    assert entry["spend"] == {"out_tokens": 70, "in_tokens": 4,
                              "cache_read_tokens": 100, "cache_creation_tokens": 9}


def test_append_headless_event_skips_empty_or_missing_transcript(ledger, tmp_path):
    spend_ledger.append_headless_event("distill", "h-sid", str(tmp_path / "absent.jsonl"))
    spend_ledger.append_headless_event("distill", "h-sid", None)
    spend_ledger.append_headless_event("", "h-sid", str(tmp_path / "absent.jsonl"))
    assert not ledger.exists()


def test_read_events_tolerates_malformed_lines(ledger):
    spend_ledger.append_drop_event(_record(), "session_end")
    with open(ledger, "a") as f:
        f.write("torn line\n")
        f.write(json.dumps({"no_spend": True}) + "\n")
    spend_ledger.append_drop_event(_record(claude_sid="sid-2"), "prune")
    events = spend_ledger.read_events()
    assert [e["sid"] for e in events] == ["sid-1", "sid-2"]


def test_read_events_missing_file_returns_empty(ledger):
    assert spend_ledger.read_events() == []
