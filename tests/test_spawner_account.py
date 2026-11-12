import asyncio
import json
import subprocess
import time
from pathlib import Path

import pytest

from dockwright import config, models, paths, spawner
from tests.conftest import allow_codex


@pytest.fixture
def sp(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ACCOUNT_ACTIVE", tmp_path / "account-active")
    monkeypatch.setattr(paths, "ACCOUNT_STATE", tmp_path / "account-state.json")
    monkeypatch.setattr(paths, "SPAWN_COUNTER", tmp_path / "spawn-counter.json")
    monkeypatch.setattr(paths, "ACCOUNT_USAGE", tmp_path / "usage")
    cfg = tmp_path / "two-pool.toml"
    cfg.write_text(
        '[accounts]\ndefault = "a"\n'
        '[[accounts.pool]]\nname = "a"\n[[accounts.pool]]\nname = "b"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    monkeypatch.delenv("CLAUDE_ORCH_ACCOUNT_WEIGHT_A", raising=False)
    monkeypatch.delenv("CLAUDE_ORCH_ACCOUNT_WEIGHT_B", raising=False)

    def _fail_if_called(*a, **k):
        raise AssertionError("picker must not call subprocess/security under the login model")

    monkeypatch.setattr(subprocess, "run", _fail_if_called)
    yield tmp_path


@pytest.fixture
def sp3(sp, monkeypatch):
    cfg = sp / "three-pool.toml"
    cfg.write_text('[accounts]\ndefault = "a"\n' + "".join(f'[[accounts.pool]]\nname = "{n}"\n' for n in "abc"))
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    monkeypatch.delenv("CLAUDE_ORCH_ACCOUNT_WEIGHT_C", raising=False)
    yield sp


def _write_active(tmp_path: Path, letter: str) -> None:
    (tmp_path / "account-active").write_text(letter + "\n")


def _write_state(tmp_path: Path, accounts: dict) -> None:
    (tmp_path / "account-state.json").write_text(
        json.dumps({"accounts": accounts})
    )


def _write_marks(tmp_path: Path, marks: dict) -> None:
    (tmp_path / "account-state.json").write_text(
        json.dumps({"accounts": {}, "model_spent": marks}))


def _bricked_entry(reset_ts: float | None = None) -> dict:
    now = int(time.time())
    entry: dict = {"bricked_at": now - 10, "last_seen": now - 10}
    if reset_ts is not None:
        entry["reset_ts"] = reset_ts
    else:
        entry["reset_ts"] = now + 3600
    return entry


def _write_usage(tmp_path, letter, *, pct5=None, pct7=None, ts_offset=0.0,
                 r5=None, r7=None, ts="auto", fable=None, fable_reset=None, fable_at="auto",
                 fable_name="Fable"):
    udir = tmp_path / "usage"
    udir.mkdir(parents=True, exist_ok=True)
    rec = {"five_hour_pct": pct5, "seven_day_pct": pct7,
           "five_hour_resets_at": r5, "seven_day_resets_at": r7}
    rec["ts"] = (time.time() - ts_offset) if ts == "auto" else ts
    if fable is not None:
        rec["model_scoped"] = [{"name": fable_name, "pct": fable, "resets_at": fable_reset}]
        rec["model_scoped_at"] = time.time() if fable_at == "auto" else fable_at
    (udir / f"{letter}.json").write_text(json.dumps(rec))


def test_pick_returns_none_when_no_account_active_file(sp, monkeypatch):
    result = spawner._pick_account()
    assert result is None


def test_pick_returns_none_when_account_active_invalid_letter(sp, monkeypatch):
    _write_active(sp, "x")
    result = spawner._pick_account()
    assert result is None


def test_pick_returns_none_when_account_active_whitespace_padded(sp, monkeypatch):
    (sp / "account-active").write_text(" a \n")
    result = spawner._pick_account()
    assert result is None


def test_pick_default_weights_distribute_1_to_1(sp, monkeypatch):
    _write_active(sp, "a")
    selections = [spawner._pick_account() for _ in range(10)]
    a_count = selections.count("a")
    b_count = selections.count("b")
    assert a_count == 5, f"expected 5 'a', got {a_count}: {selections}"
    assert b_count == 5, f"expected 5 'b', got {b_count}: {selections}"


def test_pick_counter_0_selects_a(sp, monkeypatch):
    _write_active(sp, "a")
    result = spawner._pick_account()
    assert result == "a"


def test_pick_counter_3_selects_b(sp, monkeypatch):
    _write_active(sp, "a")
    for _ in range(3):
        spawner._pick_account()
    result = spawner._pick_account()
    assert result == "b"


def test_pick_skips_bricked_a_falls_to_b(sp, monkeypatch):
    _write_active(sp, "a")
    _write_state(sp, {"a": _bricked_entry()})
    for _ in range(3):
        pass
    (sp / "spawn-counter.json").write_text(json.dumps({"counter": 0}))
    result = spawner._pick_account()
    assert result == "b"


def test_pick_skips_bricked_b_stays_on_a(sp, monkeypatch):
    _write_active(sp, "a")
    _write_state(sp, {"b": _bricked_entry()})
    (sp / "spawn-counter.json").write_text(json.dumps({"counter": 3}))
    result = spawner._pick_account()
    assert result == "a"


def test_pick_returns_none_when_both_accounts_bricked(sp, monkeypatch):
    _write_active(sp, "a")
    _write_state(sp, {
        "a": _bricked_entry(),
        "b": _bricked_entry(),
    })
    result = spawner._pick_account()
    assert result is None


def test_pick_not_bricked_when_reset_ts_in_past(sp, monkeypatch):
    _write_active(sp, "a")
    past_reset = int(time.time()) - 60
    _write_state(sp, {"a": {"bricked_at": past_reset - 3600, "reset_ts": past_reset}})
    (sp / "spawn-counter.json").write_text(json.dumps({"counter": 0}))
    result = spawner._pick_account()
    assert result == "a"


def test_pick_makes_no_keychain_calls(sp, monkeypatch):
    _write_active(sp, "a")

    def _fail_if_called(args, *a, **kw):
        raise AssertionError(f"_pick_account must not call subprocess.run: {args}")

    monkeypatch.setattr(subprocess, "run", _fail_if_called)
    result = spawner._pick_account()
    assert result == "a"


def test_pick_creates_counter_file_on_first_call(sp, monkeypatch):
    _write_active(sp, "a")
    assert not (sp / "spawn-counter.json").exists()
    spawner._pick_account()
    assert (sp / "spawn-counter.json").exists()
    data = json.loads((sp / "spawn-counter.json").read_text())
    assert data["counter"] == 1


def test_pick_increments_counter_on_each_call(sp, monkeypatch):
    _write_active(sp, "a")
    for i in range(5):
        spawner._pick_account()
    data = json.loads((sp / "spawn-counter.json").read_text())
    assert data["counter"] == 5


def test_pick_tolerates_corrupt_counter_file(sp, monkeypatch):
    _write_active(sp, "a")
    (sp / "spawn-counter.json").write_text("not-json{{{")
    result = spawner._pick_account()
    assert result == "a"


def test_pick_tolerates_missing_counter_key(sp, monkeypatch):
    _write_active(sp, "a")
    (sp / "spawn-counter.json").write_text(json.dumps({"other": 99}))
    result = spawner._pick_account()
    assert result == "a"


def test_pick_env_weight_2_1(sp, monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT_WEIGHT_A", "2")
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT_WEIGHT_B", "1")
    _write_active(sp, "a")
    selections = [spawner._pick_account() for _ in range(6)]
    assert selections.count("a") == 4
    assert selections.count("b") == 2


def test_pick_env_weight_1_1(sp, monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT_WEIGHT_A", "1")
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT_WEIGHT_B", "1")
    _write_active(sp, "a")
    selections = [spawner._pick_account() for _ in range(4)]
    assert selections.count("a") == 2
    assert selections.count("b") == 2


def test_pick_env_weight_zero_b_clamped_to_1(sp, monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT_WEIGHT_A", "3")
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT_WEIGHT_B", "0")
    _write_active(sp, "a")
    result = spawner._pick_account()
    assert result in ("a", "b")


def test_active_account_reads_pointer(sp, monkeypatch):
    _write_active(sp, "a")
    assert spawner._active_account() == "a"
    _write_active(sp, "b")
    assert spawner._active_account() == "b"
    _write_active(sp, "z")
    assert spawner._active_account() is None
    (sp / "account-active").unlink()
    assert spawner._active_account() is None


def test_account_is_bricked_missing_state_file_returns_false(sp, monkeypatch):
    assert not (sp / "account-state.json").exists()
    result = spawner._account_is_bricked("a")
    assert result is False


def test_account_is_bricked_corrupt_state_file_returns_false(sp, monkeypatch):
    (sp / "account-state.json").write_text("garbage{{")
    result = spawner._account_is_bricked("a")
    assert result is False


def test_account_is_bricked_true_when_reset_in_future(sp, monkeypatch):
    future_reset = int(time.time()) + 3600
    _write_state(sp, {"a": {"reset_ts": future_reset}})
    assert spawner._account_is_bricked("a") is True


def test_account_is_bricked_false_when_reset_in_past(sp, monkeypatch):
    past_reset = int(time.time()) - 60
    _write_state(sp, {"a": {"reset_ts": past_reset}})
    assert spawner._account_is_bricked("a") is False


def test_account_is_bricked_true_within_brick_window_no_reset_ts(sp, monkeypatch):
    recent_brick = int(time.time()) - 10
    _write_state(sp, {"a": {"bricked_at": recent_brick}})
    assert spawner._account_is_bricked("a") is True


def test_account_is_bricked_false_when_unknown_account(sp, monkeypatch):
    _write_state(sp, {"a": _bricked_entry()})
    assert spawner._account_is_bricked("b") is False


import math

def test_to_epoch_variants():
    assert spawner._to_epoch(1781500000) == 1781500000.0
    assert spawner._to_epoch(1781500000.5) == 1781500000.5
    assert spawner._to_epoch("1781500000") == 1781500000.0
    iso_z = spawner._to_epoch("2026-06-15T12:00:00Z")
    iso_off = spawner._to_epoch("2026-06-15T12:00:00+00:00")
    assert iso_z is not None and math.isclose(iso_z, iso_off)
    assert spawner._to_epoch("garbage") is None
    assert spawner._to_epoch(None) is None
    assert spawner._to_epoch(True) is None


def test_usage_is_fresh_boundary(sp, monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_USAGE_FRESH_TTL_SEC", "600")
    now = time.time()
    assert spawner._usage_is_fresh({"ts": now - 599}, now) is True
    assert spawner._usage_is_fresh({"ts": now - 600}, now) is False
    assert spawner._usage_is_fresh({"ts": "x"}, now) is False
    assert spawner._usage_is_fresh(None, now) is False


def test_read_usage_missing_and_corrupt(sp):
    assert spawner._read_usage("a") is None
    (sp / "usage").mkdir(parents=True, exist_ok=True)
    (sp / "usage" / "a.json").write_text("not-json{{")
    assert spawner._read_usage("a") is None


def test_near_limit_fresh_5h(sp):
    _write_usage(sp, "a", pct5=90.0, pct7=10.0)
    assert spawner._near_limit("a", time.time()) is True

def test_near_limit_fresh_7d(sp):
    _write_usage(sp, "a", pct5=10.0, pct7=92.0)
    assert spawner._near_limit("a", time.time()) is True

def test_near_limit_fresh_under_threshold(sp):
    _write_usage(sp, "a", pct5=80.0, pct7=80.0)
    assert spawner._near_limit("a", time.time()) is False

def test_near_limit_missing_record_false(sp):
    assert spawner._near_limit("a", time.time()) is False

def test_near_limit_stale_carry_forward_5h(sp):
    now = time.time()
    _write_usage(sp, "a", pct5=95.0, pct7=10.0, ts_offset=10_000, r5=now + 3600)
    assert spawner._near_limit("a", now) is True

def test_near_limit_stale_after_reset_false(sp):
    now = time.time()
    _write_usage(sp, "a", pct5=95.0, pct7=10.0, ts_offset=10_000, r5=now - 60)
    assert spawner._near_limit("a", now) is False

def test_near_limit_stale_carry_forward_7d_only(sp):
    now = time.time()
    _write_usage(sp, "a", pct5=10.0, pct7=95.0, ts_offset=10_000, r7=now + 3600)
    assert spawner._near_limit("a", now) is True

def test_near_limit_stale_unparseable_reset_false(sp):
    now = time.time()
    _write_usage(sp, "a", pct5=95.0, pct7=10.0, ts_offset=10_000, r5="garbage")
    assert spawner._near_limit("a", now) is False


def test_base_weights_default_is_1_1(sp):
    assert spawner._base_weights() == (1, 1)

def test_counter_weights_degrade_no_usage(sp):
    assert spawner._counter_weights(time.time()) == (1, 1)

def test_counter_weights_one_stale_degrades(sp):
    _write_usage(sp, "a", pct5=0.0, pct7=0.0)
    assert spawner._counter_weights(time.time()) == (1, 1)

def test_counter_weights_both_fresh_equal_zero(sp):
    _write_usage(sp, "a", pct5=0.0, pct7=0.0)
    _write_usage(sp, "b", pct5=0.0, pct7=0.0)
    assert spawner._counter_weights(time.time()) == (10000, 10000)

def test_counter_weights_both_fresh_a_hot_b_cool(sp):
    _write_usage(sp, "a", pct5=80.0, pct7=0.0)
    _write_usage(sp, "b", pct5=30.0, pct7=0.0)
    assert spawner._counter_weights(time.time()) == (400, 4900)

def test_counter_weights_saturated_a_zero(sp):
    _write_usage(sp, "a", pct5=100.0, pct7=0.0)
    _write_usage(sp, "b", pct5=0.0, pct7=0.0)
    assert spawner._counter_weights(time.time()) == (0, 10000)

def test_counter_weights_both_rounded_to_zero_degrade_to_base(sp):
    _write_active(sp, "a")
    _write_usage(sp, "a", pct5=99.5, pct7=0.0)
    _write_usage(sp, "b", pct5=99.5, pct7=0.0)
    assert spawner._counter_weights(time.time()) == (1, 1)
    assert spawner._pick_account(force=True) in ("a", "b")

def test_counter_weights_idle_b_post_reset_full_headroom(sp):
    now = time.time()
    _write_usage(sp, "a", pct5=44.0, pct7=0.0)
    _write_usage(sp, "b", pct5=63.0, pct7=0.0, ts_offset=30_000, r5=now - 3600)
    assert spawner._counter_weights(now) == (3136, 10000)

def test_base_weights_env_override(sp, monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT_WEIGHT_A", "2")
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT_WEIGHT_B", "1")
    assert spawner._base_weights() == (2, 1)


def test_pick_breaker_excludes_near_limit_both_parities(sp):
    _write_active(sp, "a")
    _write_usage(sp, "a", pct5=90.0, pct7=0.0)
    _write_usage(sp, "b", pct5=30.0, pct7=0.0)
    picks = [spawner._pick_account() for _ in range(12)]
    assert set(picks) == {"b"}, picks

def test_pick_both_near_limit_still_returns_letter(sp):
    _write_active(sp, "a")
    _write_usage(sp, "a", pct5=95.0, pct7=0.0)
    _write_usage(sp, "b", pct5=95.0, pct7=0.0)
    assert spawner._pick_account() in ("a", "b")

def test_pick_force_ignores_breaker(sp):
    _write_active(sp, "a")
    _write_usage(sp, "a", pct5=99.0, pct7=0.0)
    _write_usage(sp, "b", pct5=99.0, pct7=0.0)
    assert spawner._pick_account(force=True) in ("a", "b")

def test_pick_force_still_skips_bricked(sp):
    _write_active(sp, "a")
    _write_usage(sp, "a", pct5=99.0, pct7=0.0)
    _write_usage(sp, "b", pct5=99.0, pct7=0.0)
    _write_state(sp, {"a": _bricked_entry()})
    (sp / "spawn-counter.json").write_text(json.dumps({"counter": 0}))
    assert spawner._pick_account(force=True) == "b"

def test_pick_near_limit_a_bricked_b_falls_back_to_a(sp):
    _write_active(sp, "a")
    _write_usage(sp, "a", pct5=95.0, pct7=0.0)
    _write_state(sp, {"b": _bricked_entry()})
    assert spawner._pick_account() == "a"

def test_pick_saturated_a_never_picked_under_force(sp):
    _write_active(sp, "a")
    _write_usage(sp, "a", pct5=100.0, pct7=0.0)
    _write_usage(sp, "b", pct5=0.0, pct7=0.0)
    picks = [spawner._pick_account(force=True) for _ in range(12)]
    assert set(picks) == {"b"}, picks


def _counts(picks):
    return tuple(picks.count(n) for n in "abc")


def test_pick_renormalizes_weights_over_accounts_not_near_limit(sp3, monkeypatch):
    for n, w in (("A", "2"), ("B", "2"), ("C", "1")):
        monkeypatch.setenv(f"CLAUDE_ORCH_ACCOUNT_WEIGHT_{n}", w)
    _write_active(sp3, "a")
    _write_usage(sp3, "b", pct5=95.0, pct7=0.0)
    assert _counts([spawner._pick_account() for _ in range(30)]) == (20, 0, 10)


def test_pick_renormalizes_weights_over_accounts_not_bricked(sp3, monkeypatch):
    for n, w in (("A", "1"), ("B", "1"), ("C", "2")):
        monkeypatch.setenv(f"CLAUDE_ORCH_ACCOUNT_WEIGHT_{n}", w)
    _write_active(sp3, "a")
    _write_state(sp3, {"a": _bricked_entry()})
    assert _counts([spawner._pick_account() for _ in range(30)]) == (0, 10, 20)


def test_pick_falls_back_to_unblocked_accounts_by_weight_when_all_are_near_limit(sp3):
    _write_active(sp3, "a")
    _write_usage(sp3, "a", pct5=95.0, pct7=0.0)
    _write_usage(sp3, "b", pct5=95.0, pct7=0.0)
    _write_usage(sp3, "c", pct5=0.0, pct7=0.0)
    _write_state(sp3, {"c": _bricked_entry()})
    assert _counts([spawner._pick_account() for _ in range(10)]) == (5, 5, 0)


def test_pick_uses_base_weights_when_the_candidates_weights_sum_to_zero(sp3):
    _write_active(sp3, "a")
    _write_usage(sp3, "a", pct5=100.0, pct7=0.0)
    _write_usage(sp3, "b", pct5=100.0, pct7=0.0)
    _write_usage(sp3, "c", pct5=0.0, pct7=0.0)
    _write_state(sp3, {"c": _bricked_entry()})
    assert _counts([spawner._pick_account(force=True) for _ in range(10)]) == (5, 5, 0)


@pytest.mark.parametrize("pct, reset_in, at_age, spent", [
    (88, 3600, 0, True),
    (87, 3600, 0, False),
    (99, 3600, 7200, True),
    (100, -60, 7200, False),
    (100, None, 7200, False),
    (100, None, 0, True),
])
def test_fable_spent_by_snapshot_follows_pause_freshness_and_reset(sp, pct, reset_in, at_age, spent):
    now = time.time()
    _write_usage(sp, "a", pct5=0.0, pct7=0.0, fable=pct,
                 fable_reset=None if reset_in is None else now + reset_in,
                 fable_at=now - at_age)
    assert spawner._fable_spent("a", now) is spent


def test_fable_spent_without_a_row_a_mark_or_a_record_is_false(sp):
    now = time.time()
    _write_usage(sp, "a", pct5=0.0, pct7=0.0)
    assert spawner._fable_spent("a", now) is False
    assert spawner._fable_spent("b", now) is False


@pytest.mark.parametrize("row, reset_after_mark, snapshot_after_mark, now_after_mark, spent", [
    (False, None, None, 600, True),
    (True, 4200, -1800, 600, True),
    (True, 540, -1800, 600, False),
    (True, 4200, 60, 600, False),
    (True, -60, -7200, 600, True),
])
def test_fable_spent_by_mark_holds_until_a_newer_snapshot_or_a_later_reset(
        sp, row, reset_after_mark, snapshot_after_mark, now_after_mark, spent):
    marked = 1_800_000_000.0
    now = marked + now_after_mark
    if row:
        _write_usage(sp, "a", pct5=0.0, pct7=0.0, fable=38,
                     fable_reset=marked + reset_after_mark,
                     fable_at=marked + snapshot_after_mark)
    else:
        _write_usage(sp, "a", pct5=0.0, pct7=0.0)
    _write_marks(sp, {"a": {"fable": marked, "claude opus": marked + 100}})
    assert spawner._fable_spent("a", now) is spent


WEEK = 7 * 86400


@pytest.mark.parametrize("age, r7_after_mark, spent", [
    (600, None, True),
    (WEEK - 1, None, True),
    (WEEK, None, False),
    (600, 300, False),
    (600, -60, True),
    (600, 3600, True),
    (600, WEEK, True),
    (600, WEEK + 60, False),
    (60, WEEK + 3000, True),
    (3000, WEEK + 3000, False),
    (600, "not-a-date", True),
    (WEEK, "not-a-date", False),
])
def test_a_mark_with_no_row_ends_at_the_seven_day_reset_or_the_cap(sp, age, r7_after_mark, spent):
    marked = 1_800_000_000.0
    r7 = r7_after_mark if not isinstance(r7_after_mark, (int, float)) else marked + r7_after_mark
    _write_usage(sp, "a", pct5=0.0, pct7=0.0, r7=r7)
    _write_marks(sp, {"a": {"fable": marked}})
    assert spawner._fable_spent("a", marked + age) is spent


@pytest.mark.parametrize("row_reset_after_mark, r7_after_mark, age, spent", [
    (4200, WEEK + 60, 600, True),
    (None, WEEK + 60, 600, False),
    (-60, WEEK + 60, 600, False),
    (None, None, 600, True),
    (None, None, WEEK, False),
    (-60, None, WEEK, False),
])
def test_a_row_reset_later_than_the_mark_wins_and_any_other_row_falls_to_the_rule(
        sp, row_reset_after_mark, r7_after_mark, age, spent):
    marked = 1_800_000_000.0
    _write_usage(sp, "a", pct5=0.0, pct7=0.0, fable=38, fable_at=marked - 1800,
                 fable_reset=None if row_reset_after_mark is None else marked + row_reset_after_mark,
                 r7=None if r7_after_mark is None else marked + r7_after_mark)
    _write_marks(sp, {"a": {"fable": marked}})
    assert spawner._fable_spent("a", marked + age) is spent


def test_a_mark_with_no_usage_record_holds_until_the_cap(sp):
    marked = 1_800_000_000.0
    _write_marks(sp, {"a": {"fable": marked}})
    assert spawner._fable_spent("a", marked + 600) is True
    assert spawner._fable_spent("a", marked + WEEK) is False


def test_the_reported_stuck_mark_ends(sp):
    (sp / "usage").mkdir(parents=True, exist_ok=True)
    (sp / "usage" / "a.json").write_text(json.dumps({
        "five_hour_pct": 5, "seven_day_pct": 1, "five_hour_resets_at": 1793822400,
        "seven_day_resets_at": 1794409200, "ts": 1793812938,
        "model_scoped": [], "model_scoped_at": None}))
    _write_marks(sp, {"a": {"fable": 1793111915.013}})
    assert spawner._fable_spent("a", 1793812894.0) is False


def test_a_fable_pick_leaves_a_refused_account_for_one_with_an_ended_mark(sp):
    now = time.time()
    _write_active(sp, "a")
    _write_usage(sp, "a", pct5=0.0, pct7=0.0, fable=84, fable_reset=now + 86400, fable_at=now - 36000)
    _write_usage(sp, "b", pct5=0.0, pct7=0.0, r7=now + 6 * 86400)
    _write_marks(sp, {"a": {"fable": now - 600}, "b": {"fable": now - 8 * 86400}})
    assert {spawner._pick_account(fable=True) for _ in range(12)} == {"b"}


def test_model_spent_at_matches_the_family_word_in_the_banner_name(sp):
    _write_marks(sp, {"a": {"claude fable 5.1": 10.0, "fable": 5.0, "claude opus": 99.0},
                      "b": {"fable-5.2": 7.0, "fabled": 50.0}})
    assert spawner._model_spent_at("a", "fable") == 10.0
    assert spawner._model_spent_at("b", "fable") == 7.0
    assert spawner._model_spent_at("c", "fable") is None


@pytest.mark.parametrize("name, spent", [
    ("Fable", True),
    ("Fable 5.1", True),
    ("Claude Fable", True),
    ("fable-5.1", True),
    ("Opus", False),
    ("Fabled", False),
])
def test_the_snapshot_rule_finds_the_fable_row_by_its_family_word(sp, name, spent):
    now = time.time()
    _write_usage(sp, "a", pct5=0.0, pct7=0.0, fable=100, fable_reset=now + 3600, fable_name=name)
    assert spawner._fable_spent("a", now) is spent


def test_fable_left_needs_an_unbricked_cool_account_with_fable(sp):
    now = time.time()
    _write_usage(sp, "a", pct5=0.0, pct7=0.0, fable=100, fable_reset=now + 3600)
    _write_usage(sp, "b", pct5=0.0, pct7=0.0, fable=30, fable_reset=now + 3600)
    assert spawner._fable_left(now) is True
    _write_usage(sp, "b", pct5=95.0, pct7=0.0, fable=30, fable_reset=now + 3600)
    assert spawner._fable_left(now) is False
    assert spawner._fable_left(now, force=True) is True
    _write_state(sp, {"b": _bricked_entry()})
    assert spawner._fable_left(now, force=True) is False


def test_fable_pick_skips_spent_account_and_other_picks_do_not(sp):
    now = time.time()
    _write_active(sp, "a")
    _write_usage(sp, "a", pct5=0.0, pct7=0.0, fable=100, fable_reset=now + 3600)
    _write_usage(sp, "b", pct5=0.0, pct7=0.0, fable=30, fable_reset=now + 3600)
    assert {spawner._pick_account(fable=True) for _ in range(12)} == {"b"}
    assert {spawner._pick_account() for _ in range(12)} == {"a", "b"}


def test_fable_pick_skips_a_marked_account(sp):
    now = time.time()
    _write_active(sp, "a")
    _write_usage(sp, "a", pct5=0.0, pct7=0.0, fable=38, fable_reset=now + 3600, fable_at=now - 7200)
    _write_usage(sp, "b", pct5=0.0, pct7=0.0, fable=30, fable_reset=now + 3600)
    _write_marks(sp, {"a": {"fable": now - 60}})
    assert {spawner._pick_account(fable=True) for _ in range(12)} == {"b"}


def test_fable_pick_force_still_skips_spent_account(sp):
    now = time.time()
    _write_active(sp, "a")
    _write_usage(sp, "a", pct5=0.0, pct7=0.0, fable=100, fable_reset=now + 3600)
    _write_usage(sp, "b", pct5=99.0, pct7=0.0, fable=30, fable_reset=now + 3600)
    assert {spawner._pick_account(force=True, fable=True) for _ in range(6)} == {"b"}


@pytest.mark.parametrize("value, expected", [
    ("@fable", "@opus"),
    ("@fable[1m]", "@opus[1m]"),
    ("@opus", "@opus"),
    ("@sonnet", "@sonnet"),
    ("sonnet", "sonnet"),
])
def test_fable_as_opus_moves_only_fable(value, expected):
    assert spawner._fable_as_opus(value) == expected


def test_fable_as_opus_moves_a_concrete_fable_id():
    concrete = models.families()["fable"]
    assert spawner._fable_as_opus(concrete) == "@opus"
    assert spawner._fable_as_opus(f"{concrete}[1m]") == "@opus[1m]"


def test_fable_to_opus_args_raises_the_effort_to_xhigh_and_keeps_max():
    assert spawner._fable_to_opus_args(["--model", "@fable[1m]", "--effort", "medium"]) == [
        "--model", "@opus[1m]", "--effort", "xhigh"]
    assert spawner._fable_to_opus_args(["--model=@fable", "--effort=medium", "-x"]) == [
        "--model=@opus", "--effort=xhigh", "-x"]
    assert spawner._fable_to_opus_args(["--model", "@fable", "--effort", "max"]) == [
        "--model", "@opus", "--effort", "max"]


def test_gate_pool_off_ok(sp):
    assert spawner.usage_spawn_gate()["status"] == "ok"

def test_gate_both_cool_ok(sp):
    _write_active(sp, "a")
    _write_usage(sp, "a", pct5=10.0, pct7=10.0)
    _write_usage(sp, "b", pct5=10.0, pct7=10.0)
    assert spawner.usage_spawn_gate()["status"] == "ok"

def test_gate_both_hot_paused(sp):
    _write_active(sp, "a")
    now = time.time()
    _write_usage(sp, "a", pct5=96.0, pct7=10.0, r5=now + 1000)
    _write_usage(sp, "b", pct5=97.0, pct7=10.0, r5=now + 2000)
    g = spawner.usage_spawn_gate()
    assert g["status"] == "paused"
    assert g["a_pct"] == 96.0 and g["b_pct"] == 97.0
    assert g["earliest_reset_ts"] == now + 1000
    assert g["retry_after_s"] is not None and g["retry_after_s"] > 0
    assert "reason" in g

def test_gate_both_hot_unparseable_reset_null(sp):
    _write_active(sp, "a")
    _write_usage(sp, "a", pct5=96.0, pct7=10.0, r5="garbage")
    _write_usage(sp, "b", pct5=97.0, pct7=10.0, r5=None)
    g = spawner.usage_spawn_gate()
    assert g["status"] == "paused"
    assert g["earliest_reset_ts"] is None
    assert g["retry_after_s"] is None

def test_gate_one_hot_one_cool_ok(sp):
    _write_active(sp, "a")
    _write_usage(sp, "a", pct5=96.0, pct7=10.0)
    _write_usage(sp, "b", pct5=10.0, pct7=10.0)
    assert spawner.usage_spawn_gate()["status"] == "ok"

def test_gate_one_hot_one_bricked_paused(sp):
    _write_active(sp, "a")
    _write_usage(sp, "a", pct5=96.0, pct7=10.0, r5=time.time() + 500)
    _write_state(sp, {"b": _bricked_entry()})
    assert spawner.usage_spawn_gate()["status"] == "paused"

def test_gate_both_bricked_ok(sp):
    _write_active(sp, "a")
    _write_state(sp, {"a": _bricked_entry(), "b": _bricked_entry()})
    assert spawner.usage_spawn_gate()["status"] == "ok"

def test_gate_force_bypasses_pause(sp):
    _write_active(sp, "a")
    _write_usage(sp, "a", pct5=99.0, pct7=10.0)
    _write_usage(sp, "b", pct5=99.0, pct7=10.0)
    g = spawner.usage_spawn_gate(force=True)
    assert g["status"] == "ok" and g.get("forced") is True


def test_usable_5h_pct_unknown_returns_none(sp):
    now = time.time()
    assert spawner._usable_5h_pct({"five_hour_pct": None, "ts": now}, now) is None
    assert spawner._usable_5h_pct({"ts": now}, now) is None
    assert spawner._usable_5h_pct({"five_hour_pct": True, "ts": now}, now) is None
    assert spawner._usable_5h_pct(None, now) is None
    assert spawner._usable_5h_pct({"five_hour_pct": 40.0, "ts": now}, now) == 40.0


def test_usable_5h_pct_stale_pre_reset_returns_pct(sp):
    now = time.time()
    rec = {"five_hour_pct": 80.0, "ts": now - 10_000, "five_hour_resets_at": now + 3600}
    assert spawner._usable_5h_pct(rec, now) == 80.0

def test_usable_5h_pct_stale_post_reset_returns_zero(sp):
    now = time.time()
    rec = {"five_hour_pct": 80.0, "ts": now - 10_000, "five_hour_resets_at": now - 60}
    assert spawner._usable_5h_pct(rec, now) == 0.0

def test_usable_5h_pct_stale_unparseable_reset_returns_none(sp):
    now = time.time()
    rec = {"five_hour_pct": 80.0, "ts": now - 10_000, "five_hour_resets_at": "garbage"}
    assert spawner._usable_5h_pct(rec, now) is None

def test_usable_5h_pct_stale_missing_reset_returns_none(sp):
    now = time.time()
    rec = {"five_hour_pct": 80.0, "ts": now - 10_000, "five_hour_resets_at": None}
    assert spawner._usable_5h_pct(rec, now) is None


def test_counter_weights_carry_forward_a_stale_b_fresh(sp):
    now = time.time()
    _write_usage(sp, "a", pct5=80.0, pct7=0.0, ts_offset=10_000, r5=now + 3600)
    _write_usage(sp, "b", pct5=30.0, pct7=0.0)
    assert spawner._counter_weights(now) == (400, 4900)

def test_counter_weights_carry_forward_both_stale_idle_b_case(sp):
    now = time.time()
    _write_usage(sp, "a", pct5=80.0, pct7=0.0, ts_offset=10_000, r5=now + 3600)
    _write_usage(sp, "b", pct5=30.0, pct7=0.0, ts_offset=10_000, r5=now + 3600)
    assert spawner._counter_weights(now) == (400, 4900)

def test_counter_weights_stale_post_reset_reads_zero(sp):
    now = time.time()
    _write_usage(sp, "a", pct5=80.0, pct7=0.0, ts_offset=10_000, r5=now - 60)
    _write_usage(sp, "b", pct5=30.0, pct7=0.0)
    assert spawner._counter_weights(now) == (10000, 4900)

def test_counter_weights_stale_unparseable_reset_degrades(sp):
    now = time.time()
    _write_usage(sp, "a", pct5=80.0, pct7=0.0, ts_offset=10_000, r5="garbage")
    _write_usage(sp, "b", pct5=30.0, pct7=0.0)
    assert spawner._counter_weights(now) == (1, 1)

def test_counter_weights_5h_future_7d_past_normalizes(sp):
    now = time.time()
    _write_usage(sp, "a", pct5=80.0, pct7=99.0, ts_offset=10_000, r5=now + 3600, r7=now - 1000)
    _write_usage(sp, "b", pct5=30.0, pct7=0.0)
    assert spawner._counter_weights(now) == (400, 4900)


def test_counter_weights_fresh_null_5h_both_degrades(sp):
    _write_usage(sp, "a", pct5=None, pct7=10.0)
    _write_usage(sp, "b", pct5=None, pct7=20.0)
    assert spawner._counter_weights(time.time()) == (1, 1)


def test_counter_weights_fresh_null_5h_partial_degrades(sp):
    _write_usage(sp, "a", pct5=None, pct7=0.0)
    _write_usage(sp, "b", pct5=50.0, pct7=0.0)
    assert spawner._counter_weights(time.time()) == (1, 1)


def test_pick_null_5h_does_not_favor_unknown_account(sp):
    _write_active(sp, "a")
    _write_usage(sp, "a", pct5=None, pct7=0.0)
    _write_usage(sp, "b", pct5=50.0, pct7=0.0)
    selections = [spawner._pick_account() for _ in range(10)]
    assert selections.count("a") == 5 and selections.count("b") == 5


def test_gate_stale_carry_forward_pause_populates_pct(sp):
    _write_active(sp, "a")
    now = time.time()
    _write_usage(sp, "a", pct5=95.0, pct7=10.0, ts_offset=10_000, r5=now + 3600)
    _write_usage(sp, "b", pct5=96.0, pct7=10.0, ts_offset=10_000, r5=now + 7200)
    g = spawner.usage_spawn_gate()
    assert g["status"] == "paused"
    assert g["a_pct"] == 95.0 and g["b_pct"] == 96.0
    assert g["earliest_reset_ts"] == now + 3600


def test_pick_default_smooth_interleave_1_to_1(sp, monkeypatch):
    _write_active(sp, "a")
    seq = [spawner._pick_account() for _ in range(5)]
    assert seq == ["a", "b", "a", "b", "a"], seq

def test_pick_no_a_clump_first_three(sp, monkeypatch):
    _write_active(sp, "a")
    seq = [spawner._pick_account() for _ in range(3)]
    assert seq != ["a", "a", "a"], seq
    assert seq == ["a", "b", "a"], seq

def test_pick_smooth_interleave_2_to_1(sp, monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT_WEIGHT_A", "2")
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT_WEIGHT_B", "1")
    _write_active(sp, "a")
    seq = [spawner._pick_account() for _ in range(3)]
    assert seq == ["a", "b", "a"], seq

def test_pick_smooth_interleave_1_to_1(sp, monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT_WEIGHT_A", "1")
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT_WEIGHT_B", "1")
    _write_active(sp, "a")
    seq = [spawner._pick_account() for _ in range(2)]
    assert seq == ["a", "b"], seq

def test_pick_smooth_interleave_6_to_4_non_coprime(sp, monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT_WEIGHT_A", "6")
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT_WEIGHT_B", "4")
    _write_active(sp, "a")
    seq = [spawner._pick_account() for _ in range(10)]
    assert seq == ["a", "b", "a", "b", "a", "a", "b", "a", "b", "a"], seq
    assert seq.count("a") == 6 and seq.count("b") == 4


def test_pick_combined_carry_forward_weights_drive_smooth_selection(sp):
    now = time.time()
    _write_active(sp, "a")
    _write_usage(sp, "a", pct5=80.0, pct7=0.0, ts_offset=10_000, r5=now + 3600)
    _write_usage(sp, "b", pct5=30.0, pct7=0.0)
    assert spawner._counter_weights(now) == (400, 4900)
    seq = [spawner._pick_account() for _ in range(53)]
    assert seq[0] == "a"
    assert seq.count("a") == 4 and seq.count("b") == 49, seq


def _held_state(tmp_path: Path, *names: str) -> None:
    (tmp_path / "account-state.json").write_text(json.dumps(
        {"accounts": {}, "held": {n: {"at": 1, "reason": "operator", "source": "cli"} for n in names}}))


def test_pick_skips_a_held_account(sp):
    _write_active(sp, "a")
    _held_state(sp, "a")
    (sp / "spawn-counter.json").write_text(json.dumps({"counter": 0}))
    assert spawner._pick_account() == "b"


def test_gate_reports_held(sp):
    _write_active(sp, "a")
    _held_state(sp, "a", "b")
    gate = spawner.usage_spawn_gate()
    assert gate["status"] == "held" and gate["account"] == "a"
    assert "account-hold --release a" in gate["hint"]


def test_gate_with_the_default_free_and_the_other_held_is_ok(sp):
    _write_active(sp, "a")
    _held_state(sp, "b")
    assert spawner.usage_spawn_gate()["status"] == "ok"


class _Proc:
    returncode = 0

    async def communicate(self):
        return (b"42\n", b"")


def _patch_launch(monkeypatch):
    launches = []

    async def fake_exec(*args, **kwargs):
        launches.append(args)
        return _Proc()

    monkeypatch.setattr(spawner.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(spawner, "_fable_left", lambda now, force=False: False)
    return launches


def test_a_held_default_account_refuses_the_spawn_instead_of_the_default_login(sp, monkeypatch):
    _write_active(sp, "a")
    _held_state(sp, "a", "b")
    launches = _patch_launch(monkeypatch)
    with pytest.raises(ValueError, match="on hold"):
        asyncio.run(spawner.spawn_worker_tab(cwd="/x", initial_prompt="hi", name="x",
                                             extra_args=["--model", "claude-opus-5-5"]))
    assert launches == []


def test_a_codex_spawn_ignores_a_held_default_account(sp, monkeypatch):
    allow_codex(monkeypatch, sp, "/x", extra_toml=(sp / "two-pool.toml").read_text())
    _write_active(sp, "a")
    _held_state(sp, "a", "b")
    launches = _patch_launch(monkeypatch)
    asyncio.run(spawner.spawn_worker_tab(cwd="/x", initial_prompt="hi", name="x", runtime="codex"))
    assert len(launches) == 1
