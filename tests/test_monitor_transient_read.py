"""A read that failed is not an event that was delivered.

The done and questions scans used to append the event's path to the cursor
BEFORE parsing it, so any read failure — transient or permanent — consumed the
event without ever emitting it. Both writers use write_json_atomic today, which
makes a torn read unreachable from them, so this was a fail-open rather than a
live bug; it is closed here because the property is the same one the lane-death
incident violated, and a latent version of it survives exactly until someone
adds a writer that is not atomic.

The split matters and is the whole design:
  * a PERMANENTLY malformed payload is consumed, loudly — retrying forever
    would wedge the lane on one bad file it can never emit anyway;
  * a TRANSIENT OSError is never consumed — it retries.
"""
import json

import pytest

from dockwright import lane_io, monitor, paths, state


@pytest.fixture
def scan_state(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "DONE", tmp_path / "done")
    monkeypatch.setattr(paths, "QUESTIONS", tmp_path / "questions")
    monkeypatch.setattr(paths, "LANE_HEALTH", tmp_path / "lane-health")
    monkeypatch.setattr(lane_io, "reader_is_dead", lambda fd=1: False)
    return tmp_path


MGR = {"name": "mgr", "sid": "mgr-sid"}


def _write(bucket_root, name, payload):
    bucket_root.mkdir(parents=True, exist_ok=True)
    path = bucket_root / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return path


def _cursor(tmp_path, lane):
    path = tmp_path / f".seen-{lane}-mgr"
    return path.read_text() if path.exists() else ""


def _explode_on(monkeypatch, target_name, error):
    """Make Path.read_text raise for one specific file only.

    Returns a switch so a test can heal the fault WITHOUT monkeypatch.undo(),
    which would also revert the paths fixture and silently point the scan at
    the real state root.
    """
    from pathlib import Path
    original = Path.read_text
    failing = {"on": True}

    def patched(self, *args, **kwargs):
        if failing["on"] and self.name == target_name:
            raise error
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", patched)
    return failing


def test_a_transient_read_error_never_consumes_a_question(scan_state, monkeypatch):
    tmp_path = scan_state
    path = _write(paths.QUESTIONS / "mgr", "q1.json",
                  {"worker_name": "w", "question": "must survive"})
    _explode_on(monkeypatch, "q1.json", OSError(24, "Too many open files"))

    monitor.run_questions_scan(dict(MGR))

    assert str(path) not in _cursor(tmp_path, "questions"), (
        "a transient read error consumed the question — it can never be "
        "emitted again")


def test_a_transient_read_error_never_consumes_a_done_event(scan_state, monkeypatch):
    tmp_path = scan_state
    path = _write(paths.DONE / "mgr", "d1.json",
                  {"worker_name": "w", "summary": "must survive"})
    _explode_on(monkeypatch, "d1.json", OSError(24, "Too many open files"))

    monitor.run_done_scan(dict(MGR))

    assert str(path) not in _cursor(tmp_path, "done")


def test_the_event_is_emitted_once_the_read_succeeds(scan_state, monkeypatch, capsys):
    """The retry must actually deliver, not just decline to consume."""
    tmp_path = scan_state
    _write(paths.DONE / "mgr", "d1.json",
           {"worker_name": "w", "summary": "must survive"})
    fault = _explode_on(monkeypatch, "d1.json", OSError(24, "Too many open files"))
    monitor.run_done_scan(dict(MGR))
    assert "must survive" not in capsys.readouterr().out

    fault["on"] = False
    monitor.run_done_scan(dict(MGR))
    assert "must survive" in capsys.readouterr().out


@pytest.mark.parametrize("lane,bucket,scan", [
    ("done", "done", monitor.run_done_scan),
    ("questions", "questions", monitor.run_questions_scan),
])
def test_a_permanently_malformed_payload_is_consumed_loudly(
        scan_state, capsys, lane, bucket, scan):
    """A poison pill must not wedge the lane — but it must not be silent."""
    tmp_path = scan_state
    path = _write(getattr(paths, bucket.upper()) / "mgr", "bad.json", "{not json")

    scan(dict(MGR))

    assert str(path) in _cursor(tmp_path, lane), (
        "an unparseable payload was left to retry forever")
    assert "unparseable" in capsys.readouterr().err, (
        "the drop was silent — a consumed-and-never-emitted event must say so")


def test_an_undecodable_payload_is_consumed_not_retried_forever(scan_state, capsys):
    """`read_text()` raises UnicodeDecodeError on invalid bytes, and that is a
    ValueError but NOT a JSONDecodeError — so it escaped the malformed-payload
    split, fell through to the retry ladder, and a permanently-undecodable file
    wedged the lane after five scans. Permanently broken is permanently broken,
    whichever exception says so."""
    tmp_path = scan_state
    bucket = paths.DONE / "mgr"
    bucket.mkdir(parents=True, exist_ok=True)
    path = bucket / "bad-bytes.json"
    path.write_bytes(b"\xff\xfe\x00 not utf-8 at all")

    monitor.run_done_scan(dict(MGR))

    assert str(path) in _cursor(tmp_path, "done"), (
        "an undecodable payload was left to retry until the lane wedged")
    assert "unparseable" in capsys.readouterr().err
