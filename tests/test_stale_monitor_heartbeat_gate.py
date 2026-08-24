"""A scan that could not persist its dedup state must not claim it delivered.

The heartbeat means "this lane is delivering". A `stale` scan whose
emitted-state write failed is about to re-page everything it just paged and
re-fire the acts it just performed — and `dockwright lanes` reading OK through
that is the report claiming health while broken, which is the one failure this
whole change exists to remove.
"""
import json

import pytest

from dockwright import stale_monitor


@pytest.fixture
def sm_root(tmp_path, monkeypatch):
    monkeypatch.setattr(stale_monitor, "ROOT", tmp_path)
    return tmp_path


def test_no_heartbeat_when_the_cursor_write_failed(sm_root, monkeypatch):
    real_write = stale_monitor._write_json_atomic

    def _fail_cursor_only(path, data):
        if ".stale-emitted-" in path.name:
            raise OSError(30, "Read-only file system")
        return real_write(path, data)

    monkeypatch.setattr(stale_monitor, "_write_json_atomic", _fail_cursor_only)
    assert stale_monitor.main(manager_name="mgr") == 0
    assert not stale_monitor._lane_heartbeat_path("mgr", "stale").exists(), (
        "the lane reported itself alive while unable to persist its own dedup "
        "state, so `dockwright lanes` would read OK through a state-root "
        "failure that is actively duplicating pages")


def test_heartbeat_is_written_on_a_clean_scan(sm_root):
    """The other direction — otherwise 'never heartbeat' would pass."""
    assert stale_monitor.main(manager_name="mgr") == 0
    record = json.loads(
        stale_monitor._lane_heartbeat_path("mgr", "stale").read_text())
    assert record["lane"] == "stale"
    assert record["consecutive_errors"] == 0
