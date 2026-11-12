import json
import shutil
from pathlib import Path

import pytest

from dockwright import state
from dockwright.state import read_json, write_json_atomic, list_json_in

def test_write_then_read(tmp_path):
    f = tmp_path / "x.json"
    write_json_atomic(f, {"a": 1, "b": "two"})
    assert read_json(f) == {"a": 1, "b": "two"}

def test_read_missing_returns_none(tmp_path):
    assert read_json(tmp_path / "nope.json") is None

def test_write_atomic_no_partial(tmp_path):
    f = tmp_path / "x.json"
    write_json_atomic(f, {"a": 1})
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())

def test_list_json_in_returns_parsed_records(tmp_path):
    write_json_atomic(tmp_path / "a.json", {"id": "a"})
    write_json_atomic(tmp_path / "b.json", {"id": "b"})
    (tmp_path / "not-json.txt").write_text("ignore")
    records = sorted(list_json_in(tmp_path), key=lambda r: r["id"])
    assert records == [{"id": "a"}, {"id": "b"}]

def test_read_json_corrupt_returns_none(tmp_path):
    f = tmp_path / "x.json"
    f.write_text("{not valid json")
    assert read_json(f) is None


def test_read_json_non_utf8_returns_none(tmp_path):
    f = tmp_path / "x.json"
    f.write_bytes(b"\xff\xfe{}")
    assert read_json(f) is None


def test_window_id_of_prefers_new_key():
    assert state.window_id_of({"window_id": "w1", "iterm_sid": "w2"}) == "w1"


def test_window_id_of_falls_back_to_iterm_sid():
    assert state.window_id_of({"iterm_sid": "w2"}) == "w2"


def test_window_id_of_handles_neither_key():
    assert state.window_id_of({}) == ""


def test_window_id_of_handles_none_values():
    assert state.window_id_of({"window_id": None, "iterm_sid": "w2"}) == "w2"
    assert state.window_id_of({"window_id": None, "iterm_sid": None}) == ""


import os
import threading
import pytest
from dockwright.state import serialize_artifact, parse_artifact, append_event


def _stamp(**over):
    base = {"phase": "spec", "name": "srs", "status": "complete",
            "writer_sid": "sid-1", "contract_hash": None,
            "written_at": 1781000000.5, "read_set": [], "scope": "internal"}
    return {**base, **over}


def test_artifact_roundtrip_preserves_stamp_and_body():
    text = serialize_artifact(_stamp(), "# Body\n\nwith --- inside\n")
    stamp, body = parse_artifact(text)
    assert stamp == _stamp()
    assert body == "# Body\n\nwith --- inside\n"


def test_frontmatter_serializes_read_set():
    rs = [{"name": "design.spec", "written_at": 1780990000.0, "contract_hash": "sha256:0c2"}]
    stamp, _ = parse_artifact(serialize_artifact(_stamp(read_set=rs), "x"))
    assert stamp["read_set"] == rs


def test_scope_round_trips_and_is_absent_on_legacy_stamps():
    text = state.serialize_artifact({"phase": "p", "name": "n", "status": "complete", "writer_sid": "w",
                                     "contract_hash": None, "written_at": 1.0, "read_set": [], "scope": "output"}, "body")
    stamp, body = state.parse_artifact(text)
    assert stamp["scope"] == "output" and body == "body"
    legacy = "---\nphase: \"p\"\nname: \"n\"\nstatus: \"complete\"\nwriter_sid: \"w\"\n---\nx"
    stamp, _ = state.parse_artifact(legacy)
    assert stamp.get("scope") is None


def test_parse_artifact_missing_frontmatter_raises():
    with pytest.raises(ValueError):
        parse_artifact("just a body, no frontmatter")


def test_parse_artifact_skips_malformed_line():
    text = serialize_artifact(_stamp(), "body")
    lines = text.splitlines()
    assert lines[1].startswith("phase:")
    lines[1] = "phase: {not json"
    stamp, body = parse_artifact("\n".join(lines))
    assert "phase" not in stamp
    assert stamp["name"] == "srs"
    assert body == "body"


def test_decided_by_is_written_only_when_present_and_round_trips():
    text = serialize_artifact(_stamp(phase="decision", decided_by="engineer"), "b")
    stamp, body = parse_artifact(text)
    assert stamp == _stamp(phase="decision", decided_by="engineer") and body == "b"
    assert text.splitlines()[1].startswith("phase:")
    assert "decided_by" not in serialize_artifact(_stamp(), "b")
    assert "decided_by" not in serialize_artifact(_stamp(decided_by=None), "b")


def test_append_event_sets_defaults_and_appends(tmp_path):
    p = tmp_path / "events.jsonl"
    append_event(p, {"type": "note", "reason": "hello"})
    append_event(p, {"type": "dispatch", "phase": "impl"})
    lines = [json.loads(l) for l in p.read_text().splitlines()]
    assert [l["type"] for l in lines] == ["note", "dispatch"]
    assert all("ts" in l and "event_id" in l for l in lines)


def test_concurrent_event_appends_all_valid(tmp_path):
    p = tmp_path / "events.jsonl"
    n = 50
    threads = [threading.Thread(target=append_event, args=(p, {"type": "note", "reason": f"r{i}"}))
               for i in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    lines = p.read_text().splitlines()
    assert len(lines) == n
    assert all(json.loads(l)["type"] == "note" for l in lines)


def test_append_event_truncates_oversized_reason(tmp_path):
    p = tmp_path / "events.jsonl"
    append_event(p, {"type": "note", "reason": "x" * 10_000})
    (line,) = p.read_text().splitlines()
    assert len(line.encode()) <= 3500
    assert json.loads(line)["reason"].endswith("…[truncated]")


def test_frontmatter_value_containing_delimiter_roundtrips():
    stamp = _stamp(name="acme---web")
    parsed, body = parse_artifact(serialize_artifact(stamp, "body"))
    assert parsed == stamp
    assert body == "body"


def test_body_with_leading_blank_lines_roundtrips_exactly():
    text = serialize_artifact(_stamp(), "\n\nstarts blank")
    _, body = parse_artifact(text)
    assert body == "\n\nstarts blank"


def test_append_event_does_not_mutate_caller_dict(tmp_path):
    event = {"type": "note", "reason": "x" * 10_000}
    snapshot = dict(event)
    append_event(tmp_path / "events.jsonl", event)
    assert event == snapshot


def test_append_event_caps_oversized_non_reason_fields(tmp_path):
    p = tmp_path / "events.jsonl"
    append_event(p, {"type": "note", "name": "n" * 10_000})
    (line,) = p.read_text().splitlines()
    assert len(line.encode()) <= 3500
    json.loads(line)


def test_write_json_atomic_unique_tmp_per_invocation(tmp_path, monkeypatch):
    target = tmp_path / "sid.json"
    srcs = []
    real_replace = os.replace
    def recording_replace(src, dst):
        srcs.append(str(src))
        real_replace(src, dst)
    monkeypatch.setattr(state.os, "replace", recording_replace)
    state.write_json_atomic(target, {"a": 1})
    state.write_json_atomic(target, {"a": 2})
    assert len(srcs) == 2
    assert srcs[0] != srcs[1], "tmp path must be unique per invocation"


def test_write_json_atomic_concurrent_writers_same_target(tmp_path):
    target = tmp_path / "sid.json"
    errors = []
    def writer(n):
        try:
            for i in range(200):
                state.write_json_atomic(target, {"writer": n, "i": i})
        except Exception as e:   # noqa: BLE001 - the test asserts none occur
            errors.append(e)
    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert state.read_json(target) is not None
    assert [p for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


import multiprocessing
import time


def _hold(path, seconds, out):
    from dockwright import state
    with state.file_lock(path):
        out.put(("acquired", time.monotonic()))
        time.sleep(seconds)


def test_file_lock_serialises_two_processes(tmp_path):
    lock = tmp_path / ".lock"
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    a = ctx.Process(target=_hold, args=(lock, 0.6, q))
    b = ctx.Process(target=_hold, args=(lock, 0.0, q))
    a.start()
    time.sleep(0.15)
    b.start()
    a.join()
    b.join()
    t1 = q.get(timeout=5)[1]
    t2 = q.get(timeout=5)[1]
    assert abs(t2 - t1) >= 0.5


def test_file_lock_creates_the_lock_file_and_its_directory(tmp_path):
    lock = tmp_path / "deep" / "dir" / ".lock"
    with state.file_lock(lock):
        assert lock.is_file()
    with state.file_lock(lock):
        pass


def _assignments_in(tmp_path, monkeypatch):
    from dockwright import paths
    monkeypatch.setattr(paths, "ASSIGNMENTS", tmp_path / "assignments")
    record = {"assignment_id": "a1", "claude_sid": "old-sid", "ticket": "K"}
    write_json_atomic(paths.assignment_path("old-sid"), record)
    return paths, record


def test_migrate_assignment_moves_the_record_to_the_new_sid(tmp_path, monkeypatch):
    paths, record = _assignments_in(tmp_path, monkeypatch)
    state.migrate_assignment("old-sid", "new-sid")
    assert not paths.assignment_path("old-sid").exists()
    assert read_json(paths.assignment_path("new-sid")) == {**record, "claude_sid": "new-sid"}


def test_migrate_assignment_keeps_the_old_record_whole_when_the_new_write_fails(tmp_path, monkeypatch):
    paths, record = _assignments_in(tmp_path, monkeypatch)
    real_write = state.write_json_atomic

    def write(path, data):
        if path == paths.assignment_path("new-sid"):
            raise OSError("disk full")
        return real_write(path, data)

    monkeypatch.setattr(state, "write_json_atomic", write)
    with pytest.raises(OSError):
        state.migrate_assignment("old-sid", "new-sid")
    assert read_json(paths.assignment_path("old-sid")) == record
    assert not paths.assignment_path("new-sid").exists()


@pytest.fixture
def memo():
    yield state.memo_dirs
    state.memo_dirs(())


def _read(directory):
    return sorted(list_json_in(directory), key=lambda r: r["id"])


def test_without_the_memo_every_read_goes_to_disk(tmp_path):
    write_json_atomic(tmp_path / "a.json", {"id": "a", "v": 1})
    assert _read(tmp_path) == [{"id": "a", "v": 1}]
    (tmp_path / "a.json").write_text(json.dumps({"id": "a", "v": 2}))
    assert _read(tmp_path) == [{"id": "a", "v": 2}]


def test_the_memo_serves_an_unchanged_directory_without_opening_files(tmp_path, memo, monkeypatch):
    write_json_atomic(tmp_path / "a.json", {"id": "a"})
    write_json_atomic(tmp_path / "b.json", {"id": "b"})
    memo([tmp_path])
    first = _read(tmp_path)
    real_read_text = Path.read_text

    def no_file(path, *args, **kwargs):
        if tmp_path in Path(path).parents:
            raise AssertionError(f"opened {path}")
        return real_read_text(path, *args, **kwargs)

    def no_read_json(path):
        raise AssertionError(f"read {path}")

    monkeypatch.setattr(state, "read_json", no_read_json)
    monkeypatch.setattr(Path, "read_text", no_file)
    assert _read(tmp_path) == first == [{"id": "a"}, {"id": "b"}]


def test_the_memo_sees_a_new_a_replaced_and_a_removed_file(tmp_path, memo):
    write_json_atomic(tmp_path / "a.json", {"id": "a", "v": 1})
    memo([tmp_path])
    assert _read(tmp_path) == [{"id": "a", "v": 1}]
    write_json_atomic(tmp_path / "b.json", {"id": "b"})
    assert _read(tmp_path) == [{"id": "a", "v": 1}, {"id": "b"}]
    write_json_atomic(tmp_path / "a.json", {"id": "a", "v": 2})
    assert _read(tmp_path) == [{"id": "a", "v": 2}, {"id": "b"}]
    (tmp_path / "b.json").unlink()
    assert _read(tmp_path) == [{"id": "a", "v": 2}]


def test_a_caller_cannot_change_the_next_read(tmp_path, memo):
    write_json_atomic(tmp_path / "a.json", {"id": "a", "tags": ["x"]})
    memo([tmp_path])
    got = _read(tmp_path)
    got[0]["tags"].append("y")
    got[0]["id"] = "z"
    assert _read(tmp_path) == [{"id": "a", "tags": ["x"]}]


def test_a_directory_outside_the_memo_reads_the_disk(tmp_path, memo):
    inside, outside = tmp_path / "a", tmp_path / "b"
    write_json_atomic(inside / "r.json", {"id": "in"})
    write_json_atomic(outside / "r.json", {"id": "out", "v": 1})
    memo([inside])
    assert _read(outside) == [{"id": "out", "v": 1}]
    (outside / "r.json").write_text(json.dumps({"id": "out", "v": 2}))
    assert _read(outside) == [{"id": "out", "v": 2}]


def test_a_memoized_directory_that_is_absent_reads_empty(tmp_path, memo):
    target = tmp_path / "later"
    memo([target])
    assert _read(target) == []
    write_json_atomic(target / "a.json", {"id": "a"})
    assert _read(target) == [{"id": "a"}]
    shutil.rmtree(target)
    assert _read(target) == []


def test_a_corrupt_file_is_skipped_on_both_reads(tmp_path, memo):
    write_json_atomic(tmp_path / "a.json", {"id": "a"})
    (tmp_path / "bad.json").write_text("{")
    memo([tmp_path])
    assert _read(tmp_path) == [{"id": "a"}]
    assert _read(tmp_path) == [{"id": "a"}]


def test_memo_dirs_empty_turns_the_memo_off(tmp_path, memo):
    write_json_atomic(tmp_path / "a.json", {"id": "a", "v": 1})
    memo([tmp_path])
    assert _read(tmp_path) == [{"id": "a", "v": 1}]
    memo(())
    (tmp_path / "a.json").write_text(json.dumps({"id": "a", "v": 2}))
    assert _read(tmp_path) == [{"id": "a", "v": 2}]
