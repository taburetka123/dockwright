import json
from pathlib import Path
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
    # No leftover .tmp file
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
            "written_at": 1781000000.5, "read_set": []}
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


def test_parse_artifact_missing_frontmatter_raises():
    with pytest.raises(ValueError):
        parse_artifact("just a body, no frontmatter")


def test_parse_artifact_skips_malformed_line():
    text = serialize_artifact(_stamp(), "body")
    lines = text.splitlines()
    # Corrupt the phase line (line 1, right after the opening delimiter)
    assert lines[1].startswith("phase:")
    lines[1] = "phase: {not json"
    stamp, body = parse_artifact("\n".join(lines))
    assert "phase" not in stamp          # bad line skipped
    assert stamp["name"] == "srs"        # good lines survive
    assert body == "body"


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
    # "---" inside a frontmatter VALUE must not sever the block (review Important #1)
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
    append_event(p, {"type": "note", "name": "n" * 10_000})    # oversize NON-reason field
    (line,) = p.read_text().splitlines()
    assert len(line.encode()) <= 3500
    json.loads(line)                                            # still valid JSON


def test_write_json_atomic_unique_tmp_per_invocation(tmp_path, monkeypatch):
    # Two writers of the SAME target must never share a tmp path: with a
    # target-derived tmp, concurrent write_text+os.replace interleave across
    # processes -> truncated JSON at the final path, or FileNotFoundError from
    # the second os.replace (orch-audit finding 1; manager MCP process vs
    # worker Stop hook both rewrite active/<sid>.json).
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
    # Thread hammer standing in for the cross-process interleave: no exception,
    # final file always parses, no tmp litter.
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
