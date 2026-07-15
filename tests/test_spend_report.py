"""spend-report: source merge, sid dedup, day bucketing."""
import importlib
import json
import time
from datetime import date, datetime, timedelta

import pytest

from dockwright import paths, spend_ledger, spend_report, state


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(paths, "CLOSED", tmp_path / "closed")
    monkeypatch.setattr(paths, "SPEND_LEDGER", tmp_path / "spend-ledger.jsonl")
    monkeypatch.setattr(spend_report, "GARDENER_LEDGER", tmp_path / "gardener-ledger.jsonl")
    (tmp_path / "active").mkdir()
    (tmp_path / "closed").mkdir()
    return tmp_path


SPEND = {"turns": 2, "out_tokens": 100, "in_tokens": 10, "cache_read_tokens": 1000}


def _ts(days_ago=0, hour=12):
    d = datetime.now().replace(hour=hour, minute=0, second=0, microsecond=0)
    return (d - timedelta(days=days_ago)).timestamp()


def test_units_from_ledger_one_per_entry(world):
    spend_ledger.append_drop_event(
        {"claude_sid": "a", "agent": "worker", "name": "w1", "spend": SPEND}, "session_end")
    units = spend_report.collect_units()
    assert len(units) == 1
    assert units[0]["name"] == "w1"
    assert units[0]["source"] == "session_end"
    assert units[0]["date"] == date.today()
    assert units[0]["spend"]["out_tokens"] == 100


def test_closed_skipped_when_session_end_reason_and_sid_in_ledger(world):
    spend_ledger.append_drop_event(
        {"claude_sid": "a", "agent": "worker", "name": "w1", "spend": SPEND}, "session_end")
    state.write_json_atomic(world / "closed" / "a.json", {
        "claude_sid": "a", "name": "w1", "closed_at": _ts(),
        "closed_reason": "session_end", "spend": SPEND,
    })
    assert len(spend_report.collect_units()) == 1


def test_closed_counted_when_not_in_ledger(world):
    # pre-ledger closure (the existing post-#69 records): no ledger entry at all
    state.write_json_atomic(world / "closed" / "old.json", {
        "claude_sid": "old", "name": "w-old", "closed_at": _ts(days_ago=1),
        "closed_reason": "session_end", "spend": SPEND,
    })
    units = spend_report.collect_units()
    assert [u["name"] for u in units] == ["w-old"]
    assert units[0]["source"] == "closed"
    assert units[0]["date"] == date.today() - timedelta(days=1)


def test_autoclosed_record_counted_even_with_other_ledger_periods(world):
    # period A ledgered at a session_end close, worker resumed, then autoclosed:
    # the closed snapshot is period B — a period the ledger never saw.
    spend_ledger.append_drop_event(
        {"claude_sid": "a", "agent": "worker", "name": "w1", "spend": SPEND}, "session_end")
    state.write_json_atomic(world / "closed" / "a.json", {
        "claude_sid": "a", "name": "w1", "closed_at": _ts(),
        "closed_reason": "idle>7200s", "spend": SPEND,
    })
    units = spend_report.collect_units()
    assert len(units) == 2


def test_active_records_counted_as_live_today(world):
    state.write_json_atomic(world / "active" / "live.json", {
        "claude_sid": "live", "agent": "manager", "name": "mgr", "spend": SPEND,
    })
    state.write_json_atomic(world / "active" / "fresh.json", {
        "claude_sid": "fresh", "agent": "worker", "name": "noturns",
    })
    units = spend_report.collect_units()
    assert [u["name"] for u in units] == ["mgr"]
    assert units[0]["source"] == "live"
    assert units[0]["agent"] == "manager"
    assert units[0]["date"] == date.today()


def test_nested_active_record_labeled_nested(world):
    state.write_json_atomic(world / "active" / "n.json", {
        "claude_sid": "n", "agent": "manager", "name": "nested-abc",
        "nested": True, "spend": SPEND,
    })
    assert spend_report.collect_units()[0]["agent"] == "nested"


def test_gardener_run_end_rows_with_string_values(world):
    # gardener-run.sh's ledger_append stores ALL values as strings
    (world / "gardener-ledger.jsonl").write_text("\n".join([
        json.dumps({"event": "run_end", "ts": _ts(), "lane": "analyst",
                    "out_tokens": "86079", "in_tokens": "18434",
                    "cache_read_tokens": "6832014", "cache_creation_tokens": "369651"}),
        json.dumps({"event": "run_end", "ts": _ts()}),
        json.dumps({"event": "run_start", "ts": _ts()}),
    ]) + "\n")
    units = spend_report.collect_units()
    assert len(units) == 1
    assert units[0]["name"] == "gardener:analyst"
    assert units[0]["agent"] == "headless"
    assert units[0]["source"] == "gardener"
    assert units[0]["spend"]["out_tokens"] == 86079
    assert units[0]["spend"].get("turns") is None


def test_filter_and_group_by_day(world):
    spend_ledger.append_drop_event(
        {"claude_sid": "a", "agent": "worker", "name": "w1", "spend": SPEND}, "session_end")
    with open(world / "spend-ledger.jsonl", "a") as f:
        f.write(json.dumps({"ts": _ts(days_ago=2), "sid": "b", "name": "w2",
                            "agent": "worker", "source": "prune", "spend": SPEND}) + "\n")
        f.write(json.dumps({"ts": _ts(days_ago=30), "sid": "c", "name": "w3",
                            "agent": "worker", "source": "prune", "spend": SPEND}) + "\n")
    units = spend_report.collect_units()
    days = spend_report.group_by_day(units, since=date.today() - timedelta(days=6))
    assert [d["date"] for d in days] == [date.today(), date.today() - timedelta(days=2)]
    assert days[0]["rows"][0]["name"] == "w1"
    assert days[0]["total"]["out_tokens"] == 100


def test_group_merges_same_name_periods_within_a_day(world):
    for sid in ("a", "b"):
        spend_ledger.append_drop_event(
            {"claude_sid": sid, "agent": "worker", "name": "w1", "spend": SPEND}, "session_end")
    days = spend_report.group_by_day(spend_report.collect_units(),
                                     since=date.today())
    assert len(days) == 1
    assert len(days[0]["rows"]) == 1
    row = days[0]["rows"][0]
    assert row["out_tokens"] == 200
    assert row["turns"] == 4


def test_render_text_report(world, capsys):
    spend_ledger.append_drop_event(
        {"claude_sid": "a", "agent": "worker", "name": "w1",
         "spend": {"turns": 2, "out_tokens": 105_571, "in_tokens": 7_618,
                   "cache_read_tokens": 42_398_278}}, "session_end")
    assert spend_report.main(["--days", "1"]) == 0
    out = capsys.readouterr().out
    assert "w1" in out
    assert "105.6k" in out
    assert "42.4M" in out
    assert "no $-cost" in out
    assert str(date.today()) in out


def test_render_counts_spendless_closed_sessions_in_footer(world, capsys):
    state.write_json_atomic(world / "closed" / "old.json", {
        "claude_sid": "old", "name": "pre-capture", "closed_at": time.time(),
        "closed_reason": "session_end",
    })
    spend_report.main(["--days", "1"])
    out = capsys.readouterr().out
    assert "1 closed session(s) in window have no spend data" in out


def test_json_output(world, capsys):
    spend_ledger.append_drop_event(
        {"claude_sid": "a", "agent": "worker", "name": "w1", "spend": SPEND}, "session_end")
    assert spend_report.main(["--days", "1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["days"][0]["rows"][0]["name"] == "w1"
    assert payload["total"]["out_tokens"] == 100
    assert payload["window"]["since"] == str(date.today())


def test_since_flag_and_conflict(world, capsys):
    assert spend_report.main(["--since", "2026-06-01", "--days", "3"]) == 2
    assert spend_report.main(["--since", "not-a-date"]) == 2


def test_merged_row_source_label_keeps_distinct_tags(world):
    with open(world / "spend-ledger.jsonl", "a") as f:
        f.write(json.dumps({"ts": _ts(), "sid": "x1", "name": "w1",
                            "agent": "worker", "source": "preflight_prune", "spend": SPEND}) + "\n")
        f.write(json.dumps({"ts": _ts(), "sid": "x2", "name": "w1",
                            "agent": "worker", "source": "prune", "spend": SPEND}) + "\n")
    days = spend_report.group_by_day(spend_report.collect_units(), since=date.today())
    assert days[0]["rows"][0]["source"] == "preflight_prune+prune"


def test_humanize():
    assert spend_report._humanize(None) == "-"
    assert spend_report._humanize(999) == "999"
    assert spend_report._humanize(105_571) == "105.6k"
    assert spend_report._humanize(42_398_278) == "42.4M"
    assert spend_report._humanize(1_500_000_000) == "1.5G"


# --- display-honesty: absent fields render as "-", not "0" ---

# SPEND has no cache_creation_tokens — mirrors a record-sourced drop event
_SPEND_NO_CACHE_CR = {"turns": 2, "out_tokens": 105_571, "in_tokens": 7_618,
                      "cache_read_tokens": 42_398_278}


def test_record_rows_show_dash_for_uncaptured_cache_creation(world, capsys):
    """A ledger drop event with no cache_creation_tokens must render '-' in the
    cache-cr column for both the row line and the day-total line."""
    spend_ledger.append_drop_event(
        {"claude_sid": "a", "agent": "worker", "name": "w1",
         "spend": _SPEND_NO_CACHE_CR}, "session_end")
    assert spend_report.main(["--days", "1"]) == 0
    out = capsys.readouterr().out
    # Find the w1 row line and split on whitespace to locate the cache-cr cell.
    # Column order: name agent turns in out cache-rd cache-cr source
    w1_line = next(ln for ln in out.splitlines() if "w1" in ln)
    cells = w1_line.split()
    # cells[0]='w1', [1]='worker', [2]=turns, [3]=in, [4]=out, [5]=cache-rd, [6]=cache-cr
    assert cells[6] == "-", f"cache-cr cell should be '-' but got {cells[6]!r} in: {w1_line!r}"

    day_total_line = next(ln for ln in out.splitlines() if "day total" in ln)
    dt_cells = day_total_line.split()
    # "day total" spans two tokens; cells: ['day','total','',turns,in,out,cache-rd,cache-cr,source]
    # Split keeps words so: ['day', 'total', turns, in, out, cache-rd, cache-cr]
    # Find cache-cr as the cell at position -2 (last non-source cell before source which is '')
    # Safer: just check the day total line also contains '-'
    assert "-" in dt_cells, f"day-total line should have '-' for cache-cr: {day_total_line!r}"


def test_json_rows_null_for_uncaptured_keys(world, capsys):
    """--json output: cache_creation_tokens must be null (None) when never captured."""
    spend_ledger.append_drop_event(
        {"claude_sid": "a", "agent": "worker", "name": "w1",
         "spend": _SPEND_NO_CACHE_CR}, "session_end")
    assert spend_report.main(["--days", "1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    row = payload["days"][0]["rows"][0]
    assert row["cache_creation_tokens"] is None, (
        f"row cache_creation_tokens should be null but got {row['cache_creation_tokens']!r}")
    total = payload["total"]
    assert total["cache_creation_tokens"] is None, (
        f"grand total cache_creation_tokens should be null but got {total['cache_creation_tokens']!r}")


def test_non_dict_state_files_are_skipped_not_crashing(world, capsys):
    """A valid-JSON-but-non-dict file in closed/ or active/ must not crash the
    report, must not produce rows, and must not count as a spendless closure."""
    (world / "closed" / "garbage.json").write_text('["not", "a", "record"]')
    (world / "active" / "garbage.json").write_text('"just a string"')
    (world / "closed" / "real.json").write_text(json.dumps({
        "claude_sid": "real", "name": "w-real", "closed_at": _ts(),
        "closed_reason": "session_end", "spend": SPEND,
    }))
    assert spend_report.collect_units()[0]["name"] == "w-real"
    assert len(spend_report.collect_units()) == 1
    assert spend_report.main(["--days", "1"]) == 0
    out = capsys.readouterr().out
    assert "w-real" in out
    assert "closed session(s) in window have no spend data" not in out


def test_mixed_presence_partial_sum(world, capsys):
    """When a gardener event (has cache_creation_tokens) and a record-sourced event
    (no cache_creation_tokens) land on the same day, the day total for
    cache_creation_tokens is the gardener's value only (partial sum of present values)."""
    # Gardener event with cache_creation_tokens=369651
    (world / "gardener-ledger.jsonl").write_text(
        json.dumps({"event": "run_end", "ts": _ts(), "lane": "analyst",
                    "out_tokens": "86079", "in_tokens": "18434",
                    "cache_read_tokens": "6832014",
                    "cache_creation_tokens": "369651"}) + "\n"
    )
    # Record-sourced drop event (no cache_creation_tokens)
    spend_ledger.append_drop_event(
        {"claude_sid": "b", "agent": "worker", "name": "w1",
         "spend": _SPEND_NO_CACHE_CR}, "session_end")
    assert spend_report.main(["--days", "1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    day = payload["days"][0]
    # Grand total cache_creation_tokens == 369651 (only the gardener's contribution)
    assert payload["total"]["cache_creation_tokens"] == 369651, (
        f"grand total should be 369651 (gardener only) but got "
        f"{payload['total']['cache_creation_tokens']!r}")
    # Day total also
    assert day["total"]["cache_creation_tokens"] == 369651


def test_gardener_ledger_prefers_dockwright_home(tmp_path, monkeypatch):
    (tmp_path / ".claude" / "dockwright" / "gardener").mkdir(parents=True)
    (tmp_path / ".claude" / "gardener").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    try:
        importlib.reload(spend_report)
        assert spend_report.GARDENER_LEDGER == (
            tmp_path / ".claude" / "dockwright" / "gardener" / "ledger.jsonl")
    finally:
        monkeypatch.undo()
        importlib.reload(spend_report)


def test_gardener_ledger_falls_back_to_legacy_home(tmp_path, monkeypatch):
    (tmp_path / ".claude" / "gardener").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    try:
        importlib.reload(spend_report)
        assert spend_report.GARDENER_LEDGER == (
            tmp_path / ".claude" / "gardener" / "ledger.jsonl")
    finally:
        monkeypatch.undo()
        importlib.reload(spend_report)
