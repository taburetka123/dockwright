import pytest
from dockwright import boot_epoch


SYSCTL_REAL = "{ sec = 1786730000, usec = 50000 } Fri Aug 14 17:53:20 2026\n"
NOW = 1_786_800_000.0


def test_parse_is_anchored_on_sec_not_usec():
    assert boot_epoch.parse_sysctl_boottime(SYSCTL_REAL) == 1786730000


def test_parse_returns_none_on_unparseable():
    assert boot_epoch.parse_sysctl_boottime("no numbers here") is None


def test_parse_proc_stat_btime():
    text = "cpu  1 2 3\nbtime 1786730000\nprocesses 99\n"
    assert boot_epoch.parse_proc_stat_btime(text) == 1786730000


def test_future_epoch_is_implausible():
    assert boot_epoch.plausible(int(NOW) + 60, now=NOW, awake=10.0) is False


def test_epoch_newer_than_uptime_allows_is_implausible():
    assert boot_epoch.plausible(int(NOW) - 10, now=NOW, awake=5000.0) is False


def test_epoch_consistent_with_uptime_is_plausible():
    assert boot_epoch.plausible(int(NOW) - 5000, now=NOW, awake=5000.0) is True


def test_a_1970_epoch_is_implausible_whatever_the_uptime_says():
    assert boot_epoch.plausible(50000, now=NOW, awake=5000.0) is False
    long_awake = NOW - boot_epoch.EPOCH_FLOOR
    assert boot_epoch.plausible(boot_epoch.EPOCH_FLOOR - 1, now=NOW,
                                awake=long_awake) is False
    assert boot_epoch.plausible(boot_epoch.EPOCH_FLOOR, now=NOW,
                                awake=long_awake) is True


def test_resolve_prefers_env_override_and_skips_plausibility():
    epoch = boot_epoch.resolve(env={"DOCKWRIGHT_BOOT_EPOCH": "1786730000"},
                               now=1786728000.0, awake=10.0)
    assert epoch == 1786730000


@pytest.mark.parametrize("override", ["inf", "-inf", "nan", "not a number"])
def test_resolve_raises_when_override_is_not_a_usable_number(override):
    with pytest.raises(boot_epoch.BootEpochUnavailable):
        boot_epoch.resolve(env={"DOCKWRIGHT_BOOT_EPOCH": override})


def test_resolve_raises_when_nothing_resolves():
    with pytest.raises(boot_epoch.BootEpochUnavailable):
        boot_epoch.resolve(env={}, sysctl_reader=lambda: None,
                           proc_stat_reader=lambda: None)


def test_resolve_raises_when_derived_value_is_implausible():
    with pytest.raises(boot_epoch.BootEpochUnavailable):
        boot_epoch.resolve(env={}, now=NOW, awake=5000.0,
                           sysctl_reader=lambda: f"{{ sec = {int(NOW) - 10}, usec = 1 }}",
                           proc_stat_reader=lambda: None)


def test_resolve_refuses_a_1970_boot_time_instead_of_running_over_it():
    with pytest.raises(boot_epoch.BootEpochUnavailable):
        boot_epoch.resolve(env={}, now=NOW, awake=5000.0,
                           sysctl_reader=lambda: "{ sec = 50000, usec = 0 }",
                           proc_stat_reader=lambda: None)


def test_the_env_override_still_bypasses_the_sanity_floor():
    assert boot_epoch.resolve(env={"DOCKWRIGHT_BOOT_EPOCH": "200"},
                              now=NOW, awake=5000.0) == 200
