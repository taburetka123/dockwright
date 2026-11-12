import pytest

from dockwright import failure_streak

T0 = 1792700000.0


@pytest.fixture
def clocks(monkeypatch):
    now = {"wall": T0, "awake": 0.0}
    monkeypatch.setattr(failure_streak, "wall_seconds", lambda: now["wall"])
    monkeypatch.setattr(failure_streak, "awake_seconds", lambda: now["awake"])
    return now


def test_fail_counts_in_a_row_and_clear_resets(clocks):
    streaks = {}
    for i in range(3):
        entry = failure_streak.fail(streaks, "k", T0 + i)
    assert entry["n"] == 3
    assert entry["since"] == T0
    failure_streak.clear(streaks, "k")
    assert "k" not in streaks
    assert failure_streak.fail(streaks, "k", T0 + 10)["n"] == 1


def test_due_needs_three_failures_and_ten_awake_minutes(clocks):
    streaks = {}
    failure_streak.fail(streaks, "k", T0)
    failure_streak.fail(streaks, "k", T0)
    clocks["awake"] = 900.0
    assert failure_streak.due(streaks["k"]) is False
    failure_streak.fail(streaks, "k", T0)
    clocks["awake"] = 599.0
    assert failure_streak.due(streaks["k"]) is False
    clocks["awake"] = 600.0
    assert failure_streak.due(streaks["k"]) is True


def test_a_reboot_counts_the_span_from_boot(clocks):
    entry = {"n": 3, "since": T0, "awake": 5000.0}
    clocks["awake"] = 700.0
    assert failure_streak.due(entry) is True
    clocks["awake"] = 100.0
    assert failure_streak.due(entry) is False


def test_slept_compares_wall_time_with_awake_time(clocks):
    begun = failure_streak.mark()
    clocks["wall"] += 30
    clocks["awake"] += 2
    assert failure_streak.slept(begun) is True
    begun = failure_streak.mark()
    clocks["wall"] += 4
    assert failure_streak.slept(begun) is False
    begun = failure_streak.mark()
    clocks["awake"] += 600
    assert failure_streak.slept(begun) is False


@pytest.mark.parametrize("text", [None, "not json", "[]", '{"k": "x"}', '{"k": {"n": 2}}',
                                  '{"k": {"n": 2, "since": 1.0, "awake": "x"}}'])
def test_load_drops_a_missing_corrupt_or_malformed_file_or_entry(tmp_path, text):
    path = tmp_path / "failures.json"
    if text is not None:
        path.write_text(text)
    assert failure_streak.load(path) == {}


@pytest.mark.parametrize("bad", [{"n": 0, "since": 1.0, "awake": 2.0}, {"n": True, "since": 1.0, "awake": 2.0},
                                 {"n": 1, "since": True, "awake": 2.0}, {"n": 1.5, "since": 1.0, "awake": 2.0}])
def test_load_drops_an_entry_with_a_bad_count_or_stamp(tmp_path, bad):
    path = tmp_path / "failures.json"
    failure_streak.save(path, {"bad": bad})
    assert failure_streak.load(path) == {}


def test_a_good_entry_beside_a_bad_one_survives_a_save_and_a_load(tmp_path):
    path = tmp_path / "failures.json"
    good = {"n": 2, "since": T0, "awake": 12}
    failure_streak.save(path, {"good": good, "bad": {"n": 2}})
    loaded = failure_streak.load(path)
    assert loaded == {"good": good}
    failure_streak.save(path, loaded)
    assert failure_streak.load(path) == {"good": good}
