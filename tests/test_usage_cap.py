import asyncio
import json
import sys
import time
from datetime import datetime, timezone
import pytest

from dockwright import config, paths, spawner, usage_cap
from dockwright.__main__ import main as dockwright_main
from dockwright.usage_cap import Breach, Entry, Verdict

NOW = 1_900_000_000.0


@pytest.fixture
def caps(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "USAGE_CAPS", tmp_path / "usage-caps.toml")
    monkeypatch.setattr(paths, "ACCOUNT_USAGE", tmp_path / "usage")
    monkeypatch.setattr(config, "account_names", lambda: ("a", "b"))
    (tmp_path / "usage").mkdir()
    return tmp_path


def _caps(root, text):
    (root / "usage-caps.toml").write_text(text)


def _usage(root, account, **rec):
    (root / "usage" / f"{account}.json").write_text(json.dumps(rec))


def _toml_utc(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_valid_file_parses_each_account_its_caps_and_its_expiry(caps):
    _caps(caps, "[b]\nfive_hour = 70\nseven_day = 80\nuntil = 2026-10-02T03:00:00+03:00\n")
    assert usage_cap.load_caps().entries == [
        Entry("b", {"five_hour": 70.0, "seven_day": 80.0},
              datetime.fromisoformat("2026-10-02T03:00:00+03:00").timestamp())]


@pytest.mark.parametrize("literal, expected", [
    ("2026-10-02T03:00:00+03:00", datetime.fromisoformat("2026-10-02T03:00:00+03:00").timestamp()),
    ("2026-10-02T03:00:00", datetime(2026, 10, 2, 3, 0).timestamp()),
    ("2026-10-02", datetime(2026, 10, 2).timestamp()),
])
def test_until_takes_an_offset_datetime_a_local_datetime_or_a_date(caps, literal, expected):
    _caps(caps, f"[b]\nfive_hour = 70\nuntil = {literal}\n")
    assert usage_cap.load_caps().entries[0].until == expected


def test_no_file_means_no_caps(caps):
    assert usage_cap.load_caps() == ([], [])
    assert usage_cap.evaluate(NOW) == Verdict((), None)


@pytest.mark.parametrize("text, fragment", [
    ("[b\n", ""),
    ("b = 70\n", "must be a table"),
    ("[c]\nfive_hour = 70\n", "unknown account 'c'"),
    ("[b]\nfive_hour = 70\nweekly = 80\n", "unknown keys ['weekly']"),
    ("[b]\nfive_hour = 0\n", "five_hour must be a number from 1 to 100"),
    ("[b]\nseven_day = 101\n", "seven_day must be a number from 1 to 100"),
    ("[b]\nfive_hour = true\n", "five_hour must be a number from 1 to 100"),
    ("[b]\nuntil = 2026-10-02\n", "needs five_hour or seven_day"),
    ('[b]\nfive_hour = 70\nuntil = "friday"\n', "until must be a TOML date or date-time"),
    ("[b]\nfive_hour = 70\nuntil = 0001-01-01\n", "until 0001-01-01 is out of range"),
])
def test_a_broken_file_is_an_error_and_never_a_breach(caps, text, fragment):
    _caps(caps, text)
    _usage(caps, "b", five_hour_pct=99, five_hour_resets_at=NOW + 3600, ts=NOW)
    verdict = usage_cap.evaluate(NOW)
    assert verdict.breaches == ()
    assert verdict.error and fragment in verdict.error


def test_a_broken_table_leaves_the_breach_in_a_valid_table(caps):
    _caps(caps, "[b]\nfive_hour = 70\n[a]\nseven_day = 0.8\n")
    _usage(caps, "b", five_hour_pct=71, five_hour_resets_at=NOW + 3600, ts=NOW)
    verdict = usage_cap.evaluate(NOW)
    assert verdict.breaches == (Breach("b", "5h", 71.0, 70.0, NOW + 3600),)
    assert "[a] seven_day must be a number from 1 to 100" in verdict.error
    payload = usage_cap.refusal(NOW)
    assert payload["reason"].startswith("usage cap: b 5h 71% ≥ 70%; usage cap file ")
    assert "is broken" in payload["reason"] and f"fix {paths.USAGE_CAPS}" in payload["hint"]
    assert (payload["earliest_reset_ts"], payload["retry_after_s"]) == (None, None)


def test_an_unknown_account_names_both_places_to_fix(caps):
    _caps(caps, "[c]\nfive_hour = 70\n")
    error = usage_cap.evaluate(NOW).error
    assert str(paths.USAGE_CAPS) in error and "[accounts] pool" in error and "dockwright.toml" in error
    assert "the pool resolves to a, b" in error


def test_a_file_that_is_not_utf8_is_an_error_and_never_a_breach(caps):
    (caps / "usage-caps.toml").write_bytes(b"# \xe4\n[b]\nfive_hour = 70\n")
    _usage(caps, "b", five_hour_pct=99, five_hour_resets_at=NOW + 3600, ts=NOW)
    verdict = usage_cap.evaluate(NOW)
    assert verdict.breaches == () and "utf-8" in verdict.error


def test_a_fresh_reading_at_or_over_the_cap_before_its_reset_is_a_breach(caps):
    _caps(caps, "[b]\nfive_hour = 70\n")
    _usage(caps, "b", five_hour_pct=70, five_hour_resets_at=NOW + 3600, ts=NOW)
    assert usage_cap.evaluate(NOW).breaches == (Breach("b", "5h", 70.0, 70.0, NOW + 3600),)


def test_a_stale_reading_over_the_cap_still_holds_until_its_reset(caps):
    _caps(caps, "[b]\nfive_hour = 70\n")
    _usage(caps, "b", five_hour_pct=71, five_hour_resets_at=NOW + 600, ts=NOW - 7200)
    assert usage_cap.evaluate(NOW).breaches == (Breach("b", "5h", 71.0, 70.0, NOW + 600),)


def test_a_reading_over_the_cap_after_its_reset_is_not_a_breach(caps):
    _caps(caps, "[b]\nfive_hour = 70\n")
    _usage(caps, "b", five_hour_pct=95, five_hour_resets_at=NOW - 1, ts=NOW)
    assert usage_cap.evaluate(NOW).breaches == ()


def test_a_reading_under_the_cap_is_not_a_breach(caps):
    _caps(caps, "[b]\nfive_hour = 70\n")
    _usage(caps, "b", five_hour_pct=69.9, five_hour_resets_at=NOW + 3600, ts=NOW)
    assert usage_cap.evaluate(NOW).breaches == ()


def test_an_iso_reset_time_is_read(caps):
    _caps(caps, "[b]\nfive_hour = 70\n")
    reset = datetime.fromtimestamp(NOW + 3600, tz=timezone.utc).isoformat()
    _usage(caps, "b", five_hour_pct=80, five_hour_resets_at=reset, ts=NOW)
    assert usage_cap.evaluate(NOW).breaches == (Breach("b", "5h", 80.0, 70.0, NOW + 3600),)


def test_without_a_reset_time_only_a_fresh_reading_counts(caps):
    _caps(caps, "[b]\nfive_hour = 70\n")
    _usage(caps, "b", five_hour_pct=80, ts=NOW - 60)
    assert usage_cap.evaluate(NOW).breaches == (Breach("b", "5h", 80.0, 70.0, None),)
    _usage(caps, "b", five_hour_pct=80, ts=NOW - 700)
    assert usage_cap.evaluate(NOW).breaches == ()


def test_the_freshness_ttl_override_binds_the_cap_rule_too(caps, monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_USAGE_FRESH_TTL_SEC", "1200")
    _caps(caps, "[b]\nfive_hour = 70\n")
    _usage(caps, "b", five_hour_pct=80, ts=NOW - 700)
    assert usage_cap.evaluate(NOW).breaches == (Breach("b", "5h", 80.0, 70.0, None),)


def test_no_reading_is_not_a_breach(caps):
    _caps(caps, "[b]\nfive_hour = 70\nseven_day = 80\n")
    assert usage_cap.evaluate(NOW).breaches == ()


def test_an_expired_entry_has_no_effect(caps):
    _caps(caps, "[b]\nfive_hour = 70\nuntil = 2020-01-01\n")
    _usage(caps, "b", five_hour_pct=99, five_hour_resets_at=NOW + 3600, ts=NOW)
    assert usage_cap.evaluate(NOW).breaches == ()


@pytest.mark.parametrize("until", ["2020-01-01", _toml_utc(NOW)])
def test_an_expired_table_for_an_account_outside_the_pool_refuses_nothing(caps, monkeypatch, until):
    monkeypatch.setattr(config, "account_names", lambda: ("a",))
    _caps(caps, f"[b]\nfive_hour = 70\nuntil = {until}\n")
    _usage(caps, "b", five_hour_pct=99, five_hour_resets_at=NOW + 3600, ts=NOW)
    assert usage_cap.evaluate(NOW) == Verdict((), None)


@pytest.mark.parametrize("until", ["", f"until = {_toml_utc(NOW + 1)}\n"])
def test_a_live_table_for_an_account_outside_the_pool_is_still_an_error(caps, monkeypatch, until):
    monkeypatch.setattr(config, "account_names", lambda: ("a",))
    _caps(caps, f"[b]\nfive_hour = 70\n{until}")
    verdict = usage_cap.evaluate(NOW)
    assert verdict.breaches == () and "unknown account 'b'" in verdict.error


def test_the_seven_day_window_and_an_earlier_expiry_set_the_lift(caps):
    _caps(caps, f"[b]\nseven_day = 80\nuntil = {_toml_utc(NOW + 600)}\n")
    _usage(caps, "b", seven_day_pct=81, seven_day_resets_at=NOW + 86400, ts=NOW)
    assert usage_cap.evaluate(NOW).breaches == (Breach("b", "7d", 81.0, 80.0, NOW + 600),)


def test_the_pause_lifts_at_the_latest_breach_and_is_unknown_when_one_is():
    first = Breach("b", "5h", 71.0, 70.0, NOW + 60)
    second = Breach("b", "7d", 81.0, 80.0, NOW + 600)
    assert usage_cap.lift_at(Verdict((first, second), None)) == NOW + 600
    assert usage_cap.lift_at(Verdict((first, second._replace(lift_at=None)), None)) is None
    assert usage_cap.lift_at(Verdict((), None)) is None


def test_refusal_is_none_without_a_breach(caps):
    _caps(caps, "[b]\nfive_hour = 70\n")
    _usage(caps, "b", five_hour_pct=10, five_hour_resets_at=NOW + 3600, ts=NOW)
    assert usage_cap.refusal(NOW) is None


def test_refusal_names_the_breach_and_when_the_pause_lifts(caps):
    _caps(caps, "[b]\nfive_hour = 70\n")
    _usage(caps, "b", five_hour_pct=71, five_hour_resets_at=NOW + 3600, ts=NOW)
    payload = usage_cap.refusal(NOW)
    assert payload["status"] == "paused" and payload["pend_reason"] == "usage-cap"
    assert payload["reason"] == "usage cap: b 5h 71% ≥ 70%"
    assert "force does not lift" in payload["hint"] and str(paths.USAGE_CAPS) in payload["hint"]
    assert (payload["a_pct"], payload["b_pct"]) == (None, 71.0)
    assert (payload["earliest_reset_ts"], payload["retry_after_s"]) == (NOW + 3600, 3600.0)


def test_a_broken_file_refuses_with_its_error_and_no_lift_time(caps):
    _caps(caps, "[b]\nfive_hour = 170\n")
    payload = usage_cap.refusal(NOW)
    assert payload["status"] == "paused" and payload["pend_reason"] == "usage-cap"
    assert "is broken" in payload["reason"] and "five_hour must be a number" in payload["reason"]
    assert (payload["earliest_reset_ts"], payload["retry_after_s"]) == (None, None)
    assert "or wait" not in payload["hint"] and f"fix {paths.USAGE_CAPS}" in payload["hint"]


def _breach_b(root):
    now = time.time()
    _caps(root, "[b]\nfive_hour = 70\n")
    _usage(root, "b", five_hour_pct=71, five_hour_resets_at=now + 3600, ts=now)


@pytest.mark.parametrize("force", [False, True])
def test_the_gate_refuses_a_breach_before_force_and_before_the_pool_check(caps, force):
    _breach_b(caps)
    payload = spawner.usage_spawn_gate(force=force)
    assert payload["status"] == "paused" and payload["pend_reason"] == "usage-cap"
    assert payload["reason"] == "usage cap: b 5h 71% ≥ 70%"


def test_the_gate_is_unchanged_without_a_cap_file(caps):
    assert spawner.usage_spawn_gate() == {"status": "ok"}


def test_the_gate_ignores_an_expired_table_for_an_account_outside_the_pool(caps, monkeypatch):
    monkeypatch.setattr(config, "account_names", lambda: ("a",))
    _caps(caps, "[b]\nfive_hour = 70\nuntil = 2020-01-01\n")
    assert spawner.usage_spawn_gate() == {"status": "ok"}


class _Driver:
    def __init__(self):
        self.spawned = 0

    async def spawn(self, **kw):
        self.spawned += 1
        return "%1"

    def set_window_option(self, window_id, option, value):
        pass


@pytest.fixture
def driver(monkeypatch):
    drv = _Driver()
    monkeypatch.setattr(spawner, "get_driver", lambda: drv)
    monkeypatch.setattr(spawner.trust, "pretrust_dir", lambda *a, **k: None)
    monkeypatch.setattr(spawner, "_pick_account", lambda force=False, fable=False: None)
    return drv


@pytest.mark.parametrize("env", [None, {"CLAUDE_CODE_OAUTH_TOKEN": "t"}])
def test_the_tab_refuses_a_worker_before_it_spawns(caps, driver, tmp_path, env):
    _breach_b(caps)
    with pytest.raises(spawner.UsageCapPaused, match="usage cap: b 5h 71% ≥ 70%"):
        asyncio.run(spawner.spawn_worker_tab(cwd=str(tmp_path), initial_prompt="x", name="w", env=env))
    assert driver.spawned == 0


def test_the_tab_lets_a_manager_through(caps, driver, tmp_path):
    _breach_b(caps)
    asyncio.run(spawner.spawn_worker_tab(cwd=str(tmp_path), initial_prompt="/manager", name="m",
                                         agent="manager", runtime="claude"))
    assert driver.spawned == 1


def test_the_command_prints_each_capped_window_and_its_state(caps, capsys):
    now = time.time()
    _caps(caps, "[b]\nfive_hour = 70\nseven_day = 80\n")
    _usage(caps, "b", five_hour_pct=71, five_hour_resets_at=now + 3600, seven_day_pct=50,
           seven_day_resets_at=now + 86400, ts=now)
    assert usage_cap.main([]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"b 5h used 71% cap 70% until no expiry: breached until {usage_cap.clock(now + 3600, now)}",
        "b 7d used 50% cap 80% until no expiry: ok",
    ]


def test_clock_on_a_millisecond_epoch_says_its_window_resets():
    assert usage_cap.clock((NOW + 3600) * 1000, NOW) == "its window resets"


def test_the_command_prints_a_millisecond_reset_as_its_window_resets(caps, capsys):
    now = time.time()
    _caps(caps, "[b]\nfive_hour = 70\n")
    _usage(caps, "b", five_hour_pct=71, five_hour_resets_at=(now + 3600) * 1000, ts=now)
    assert usage_cap.main([]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "b 5h used 71% cap 70% until no expiry: breached until its window resets"]


@pytest.mark.parametrize("pool", [("a", "b"), ("a",)])
def test_the_command_marks_an_expired_cap(caps, monkeypatch, capsys, pool):
    monkeypatch.setattr(config, "account_names", lambda: pool)
    _caps(caps, "[b]\nfive_hour = 70\nuntil = 2020-01-01\n")
    assert usage_cap.main([]) == 0
    assert capsys.readouterr().out.strip() == "b 5h used ? cap 70% until 2020-01-01 00:00: expired"


def test_the_command_exits_1_on_a_broken_file(caps, capsys):
    _caps(caps, "[b]\nfive_hour = 170\n")
    assert usage_cap.main([]) == 1
    assert "five_hour must be a number from 1 to 100" in capsys.readouterr().err


def test_the_command_prints_the_valid_tables_and_exits_1_beside_a_broken_one(caps, capsys):
    _caps(caps, "[b]\nfive_hour = 70\n[a]\nseven_day = 0.8\n")
    assert usage_cap.main([]) == 1
    out = capsys.readouterr()
    assert out.out.splitlines() == ["b 5h used ? cap 70% until no expiry: ok"]
    assert "[a] seven_day must be a number from 1 to 100" in out.err


def test_paused_exits_0_naming_the_breach(caps, capsys):
    _breach_b(caps)
    assert usage_cap.main(["--paused"]) == 0
    assert capsys.readouterr().out.strip() == "usage cap: b 5h 71% ≥ 70%"


def test_paused_exits_0_on_a_broken_file(caps, capsys):
    _caps(caps, "[b]\nfive_hour = 170\n")
    assert usage_cap.main(["--paused"]) == 0
    assert "is broken" in capsys.readouterr().out


@pytest.mark.parametrize("text", [None, "[b]\nfive_hour = 70\n"])
def test_paused_exits_1_without_a_breach(caps, capsys, text):
    if text is not None:
        _caps(caps, text)
        _usage(caps, "b", five_hour_pct=10, five_hour_resets_at=time.time() + 3600, ts=time.time())
    assert usage_cap.main(["--paused"]) == 1
    assert capsys.readouterr().out == ""


def test_the_command_says_when_there_is_no_cap(caps, capsys):
    assert usage_cap.main([]) == 0
    assert capsys.readouterr().out.startswith("no usage caps: ")


def test_dockwright_dispatches_usage_cap(caps, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["dockwright", "usage-cap"])
    with pytest.raises(SystemExit) as exit_:
        dockwright_main()
    assert exit_.value.code == 0
    assert capsys.readouterr().out.startswith("no usage caps: ")


def test_page_fields_name_the_breaches_the_lift_and_the_file():
    breach = Breach("b", "5h", 71.0, 70.0, NOW + 3600)
    assert usage_cap.page_fields(Verdict((breach,), None), NOW) == {
        "breaches": "b 5h 71% ≥ 70%", "until": usage_cap.clock(NOW + 3600, NOW),
        "path": str(paths.USAGE_CAPS), "error": ""}
    assert usage_cap.page_fields(Verdict((), "bad"), NOW)["error"] == "bad"
    assert usage_cap.page_fields(Verdict((breach,), "bad"), NOW)["until"] == (
        f"{usage_cap.clock(NOW + 3600, NOW)} and the cap file is fixed")
