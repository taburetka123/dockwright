from datetime import datetime

import pytest

from dockwright import cron


def _dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


@pytest.mark.parametrize("expr,yes,no", [
    ("*/15 * * * *", ["2026-10-13 09:00", "2026-10-13 09:45"], ["2026-10-13 09:10"]),
    ("0 9 * * 1-5", ["2026-10-13 09:00"], ["2026-10-13 09:01", "2026-10-10 09:00", "2026-10-11 09:00"]),
    ("30 2 1 * *", ["2026-11-01 02:30"], ["2026-11-02 02:30"]),
    ("0 0 * * 7", ["2026-10-11 00:00"], ["2026-10-12 00:00"]),
    ("0 0 * * 0", ["2026-10-11 00:00"], []),
    ("5,35 8-10/2 * * *", ["2026-10-13 08:05", "2026-10-13 10:35"], ["2026-10-13 09:05"]),
    ("0 12 15 * 3", ["2026-10-15 12:00", "2026-10-14 12:00"], ["2026-10-13 12:00"]),
])
def test_matches(expr, yes, no):
    spec = cron.parse(expr)
    assert all(cron.matches(spec, _dt(s)) for s in yes)
    assert not any(cron.matches(spec, _dt(s)) for s in no)


@pytest.mark.parametrize("expr", ["* * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *", "* * * * 8",
                                  "mon * * * *", "*/0 * * * *", "5-1 * * * *", "@daily"])
def test_parse_refuses(expr):
    with pytest.raises(ValueError):
        cron.parse(expr)


def test_latest_slot_returns_the_last_matching_minute_in_the_window():
    spec = cron.parse("0 9 * * *")
    after = _dt("2026-10-13 08:00").timestamp()
    until = _dt("2026-10-13 18:00").timestamp()
    assert cron.latest_slot(spec, after, until) == _dt("2026-10-13 09:00").timestamp()
    assert cron.latest_slot(spec, _dt("2026-10-13 09:00").timestamp(), until) is None
    assert cron.latest_slot(spec, _dt("2026-10-13 08:59").timestamp(), _dt("2026-10-13 09:00").timestamp()) == \
        _dt("2026-10-13 09:00").timestamp()


def test_latest_slot_after_a_long_sleep_names_one_slot_and_stops_at_the_search_depth():
    spec = cron.parse("*/15 * * * *")
    after = _dt("2026-10-10 00:00").timestamp()
    until = _dt("2026-10-13 18:07").timestamp()
    assert cron.latest_slot(spec, after, until) == _dt("2026-10-13 18:00").timestamp()
    rare = cron.parse("0 0 29 2 *")
    assert cron.latest_slot(rare, after, until) is None
