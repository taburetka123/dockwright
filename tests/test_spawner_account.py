import json
import subprocess
import time
from pathlib import Path

import pytest

from dockwright import config, paths, spawner


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


def _write_active(tmp_path: Path, letter: str) -> None:
    (tmp_path / "account-active").write_text(letter + "\n")


def _write_state(tmp_path: Path, accounts: dict) -> None:
    (tmp_path / "account-state.json").write_text(
        json.dumps({"accounts": accounts})
    )


def _bricked_entry(reset_ts: float | None = None) -> dict:
    now = int(time.time())
    entry: dict = {"bricked_at": now - 10, "last_seen": now - 10}
    if reset_ts is not None:
        entry["reset_ts"] = reset_ts
    else:
        entry["reset_ts"] = now + 3600
    return entry


def _write_usage(tmp_path, letter, *, pct5=None, pct7=None, ts_offset=0.0,
                 r5=None, r7=None, ts="auto"):
    udir = tmp_path / "usage"
    udir.mkdir(parents=True, exist_ok=True)
    rec = {"five_hour_pct": pct5, "seven_day_pct": pct7,
           "five_hour_resets_at": r5, "seven_day_resets_at": r7}
    rec["ts"] = (time.time() - ts_offset) if ts == "auto" else ts
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
