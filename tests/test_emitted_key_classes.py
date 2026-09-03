import json
from pathlib import Path

import pytest

from dockwright import stale_monitor

SOURCE = Path(stale_monitor.__file__).read_text(encoding="utf-8")


def test_the_two_classes_do_not_overlap():
    overlap = set(stale_monitor.ACTION_KEY_PREFIXES) & set(
        stale_monitor.PAGE_KEY_PREFIXES)
    assert overlap == set(), overlap


@pytest.mark.parametrize("key,is_action", [
    ("nudge_sent:abc", True),
    ("nudged:abc:123", True),
    ("scheduled:abc", True),
    ("recovery:abc", True),
    ("auth-recovery:abc", True),
    ("last_autoclose_run", True),
    ("codex_log_cache", True),
    ("processing:abc:1", False),
    ("question:abc", False),
    ("orphan:%1", False),
    ("approval:a:b", False),
    ("auth-emit:abc", False),
    ("lane_silent:done", False),
])
def test_the_classifier_puts_each_known_key_on_the_right_side(key, is_action):
    assert stale_monitor._is_action_key(key) is is_action


def test_a_failed_delivery_keeps_the_acts_and_drops_the_pages(tmp_path):
    state = tmp_path / ".stale-emitted-mgr.json"
    emitted = {"question:old": 4}
    next_emitted = {
        "nudge_sent:w1": 1234.0,
        "last_autoclose_run": 99.0,
        "processing:w1:9": 30,
        "lane_silent:done": {"at": 1.0, "level": 1},
    }
    stale_monitor._commit_actions_only(state, emitted, next_emitted)

    written = json.loads(state.read_text())
    assert written["nudge_sent:w1"] == 1234.0, "a performed nudge was un-recorded"
    assert written["last_autoclose_run"] == 99.0
    assert "processing:w1:9" not in written, (
        "a page that never reached the manager was recorded as shown")
    assert "lane_silent:done" not in written
    assert written["question:old"] == 4, "prior state was discarded"


def test_an_unclassified_key_takes_the_page_path(tmp_path):
    state = tmp_path / ".stale-emitted-mgr.json"
    stale_monitor._commit_actions_only(state, {}, {"brand-new-key": 1})
    assert "brand-new-key" not in json.loads(state.read_text())


def _killed_mid_scan(tmp_path, mode):
    import subprocess
    import sys as _sys

    state = tmp_path / f".stale-emitted-{mode}.json"
    src = Path(stale_monitor.__file__).resolve().parents[1]
    script = (
        "import os, signal, sys\n"
        f"sys.path.insert(0, {str(src)!r})\n"
        "from pathlib import Path\n"
        "from dockwright import stale_monitor as sm\n"
        f"sm.ROOT = Path({str(tmp_path)!r})\n"
        f"state = Path({str(state)!r})\n"
        "emitted, next_emitted = {}, {}\n"
        + ("sm._record_action_ahead(state, emitted, next_emitted,"
           " 'nudge_sent:w1', 1234.0)\n"
           if mode == "ahead" else
           "next_emitted['nudge_sent:w1'] = 1234.0\n")
        + "os.kill(os.getpid(), signal.SIGKILL)\n")
    result = subprocess.run([_sys.executable, "-c", script], capture_output=True)
    assert result.returncode == -9, "the child was not hard-killed"
    if not state.exists():
        return None
    return json.loads(state.read_text()).get("nudge_sent:w1")


def test_recording_after_the_act_loses_it_to_a_hard_kill(tmp_path):
    assert _killed_mid_scan(tmp_path, "after") is None


def test_write_ahead_survives_a_hard_kill(tmp_path):
    assert _killed_mid_scan(tmp_path, "ahead") == 1234.0
