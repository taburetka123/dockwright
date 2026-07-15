"""spend-cost: window session collection + per-model dollar reconstruction."""
import json
from datetime import date, datetime, timedelta

import pytest

from dockwright import paths, spend_ledger, spend_cost, state, transcript


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(paths, "CLOSED", tmp_path / "closed")
    monkeypatch.setattr(paths, "SPEND_LEDGER", tmp_path / "spend-ledger.jsonl")
    (tmp_path / "active").mkdir()
    (tmp_path / "closed").mkdir()
    return tmp_path


def _ts(days_ago=0, hour=12):
    d = datetime.now().replace(hour=hour, minute=0, second=0, microsecond=0)
    return (d - timedelta(days=days_ago)).timestamp()


SPEND = {"turns": 2, "out_tokens": 100, "in_tokens": 10, "cache_read_tokens": 1000}


def test_collect_sessions_dedupes_by_sid_across_ledger_rows(world):
    # account-autoswitch shape: 3 ledger rows, 1 sid -> one session.
    for _ in range(3):
        spend_ledger.append_drop_event(
            {"claude_sid": "auto", "agent": "worker", "name": "autoswitch",
             "spend": SPEND}, "session_end")
    sessions = spend_cost.collect_sessions()
    sids = [s["sid"] for s in sessions]
    assert sids.count("auto") == 1


def test_collect_sessions_merges_ledger_closed_active(world):
    spend_ledger.append_drop_event(
        {"claude_sid": "led", "agent": "worker", "name": "w1", "spend": SPEND},
        "session_end")
    state.write_json_atomic(world / "closed" / "cl.json", {
        "claude_sid": "cl", "name": "w2", "closed_at": _ts(days_ago=1),
        "closed_reason": "idle>7200s", "spend": SPEND})
    state.write_json_atomic(world / "active" / "ac.json", {
        "claude_sid": "ac", "agent": "manager", "name": "mgr", "spend": SPEND})
    sids = {s["sid"] for s in spend_cost.collect_sessions()}
    assert sids == {"led", "cl", "ac"}


def _write_transcript(world, sid, lines):
    # find_session_log scans ~/.claude/projects/*/<sid>.jsonl; point HOME at world.
    proj = world / ".claude" / "projects" / "-proj"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{sid}.jsonl").write_text("\n".join(lines) + "\n")


def _aline(msg_id, model, output=0, cache_1h=0):
    usage = {"output_tokens": output, "input_tokens": 0,
             "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": cache_1h,
             "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                "ephemeral_1h_input_tokens": cache_1h}}
    return json.dumps({"type": "assistant",
                       "message": {"id": msg_id, "role": "assistant",
                                   "model": model, "usage": usage}})


def test_reconstruct_prices_per_model_from_transcripts(world, monkeypatch):
    monkeypatch.setenv("HOME", str(world))
    spend_ledger.append_drop_event(
        {"claude_sid": "f1", "agent": "worker", "name": "fable-job", "spend": SPEND},
        "session_end")
    spend_ledger.append_drop_event(
        {"claude_sid": "s1", "agent": "worker", "name": "sonnet-job", "spend": SPEND},
        "session_end")
    # Fable: 1M output ($50) + 1M 1h cache-write ($20) = $70
    _write_transcript(world, "f1", [_aline("a", "claude-fable-5",
                                           output=1_000_000, cache_1h=1_000_000)])
    # Sonnet: 1M output = $15
    _write_transcript(world, "s1", [_aline("b", "claude-sonnet-4-6", output=1_000_000)])
    report = spend_cost.build_report(since=date.today(), until=date.today())
    assert report["total"] == pytest.approx(85.0)
    by_model = {m["model"]: m for m in report["models"]}
    assert by_model["claude-fable-5"]["cost"] == pytest.approx(70.0)
    assert by_model["claude-sonnet-4-6"]["cost"] == pytest.approx(15.0)
    # cache share = cache_write+read / total = 20 / 85
    assert report["cache_cost"] == pytest.approx(20.0)


def test_reconstruct_flags_missing_transcript(world, monkeypatch):
    monkeypatch.setenv("HOME", str(world))
    spend_ledger.append_drop_event(
        {"claude_sid": "gone", "agent": "worker", "name": "pruned", "spend": SPEND},
        "session_end")
    report = spend_cost.build_report(since=date.today(), until=date.today())
    assert report["missing_transcripts"] == 1
    assert report["total"] == 0.0


def test_reconstruct_flags_unpriced_model(world, monkeypatch):
    monkeypatch.setenv("HOME", str(world))
    spend_ledger.append_drop_event(
        {"claude_sid": "syn", "agent": "worker", "name": "synth", "spend": SPEND},
        "session_end")
    _write_transcript(world, "syn", [_aline("a", "<synthetic>", output=1_000_000)])
    report = spend_cost.build_report(since=date.today(), until=date.today())
    assert report["total"] == 0.0
    assert report["unpriced_out_tokens"] == 1_000_000


def test_main_renders_dollar_total(world, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(world))
    spend_ledger.append_drop_event(
        {"claude_sid": "f1", "agent": "worker", "name": "fable-job", "spend": SPEND},
        "session_end")
    _write_transcript(world, "f1", [_aline("a", "claude-fable-5", output=1_000_000)])
    assert spend_cost.main(["--days", "1"]) == 0
    out = capsys.readouterr().out
    assert "$50.00" in out
    assert "claude-fable-5" in out


def test_main_json_output(world, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(world))
    spend_ledger.append_drop_event(
        {"claude_sid": "f1", "agent": "worker", "name": "fable-job", "spend": SPEND},
        "session_end")
    _write_transcript(world, "f1", [_aline("a", "claude-fable-5", output=1_000_000)])
    assert spend_cost.main(["--days", "1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == pytest.approx(50.0)
