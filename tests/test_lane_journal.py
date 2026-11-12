import json

from dockwright import lane_journal, paths


def _lane_events(key):
    path = paths.artifact_events_path(key)
    return [e for e in (json.loads(line) for line in path.read_text().splitlines())
            if e.get("type") == "lane_event"]


def test_no_journal_means_no_write_and_no_topic_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ARTIFACTS", tmp_path / "artifacts")
    assert lane_journal.journal_once("WORK-1", "DONE", {"worker": "w", "event_id": "e1", "advanced": True}) is False
    assert not (tmp_path / "artifacts").exists()


def test_one_line_per_distinct_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ARTIFACTS", tmp_path / "artifacts")
    journal = paths.artifact_events_path("WORK-1")
    journal.parent.mkdir(parents=True)
    journal.write_text("")
    fields = {"worker": "w", "event_id": "e1", "advanced": True}
    assert lane_journal.journal_once("WORK-1", "DONE", fields) is True
    assert lane_journal.journal_once("WORK-1", "DONE", dict(fields)) is True
    assert lane_journal.journal_once("WORK-1", "DONE", {**fields, "event_id": "e2"}) is True
    assert [e["fields"]["event_id"] for e in _lane_events("WORK-1")] == ["e1", "e2"]


def test_journal_first_writes_once_per_identity_and_says_whether_it_wrote(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ARTIFACTS", tmp_path / "artifacts")
    assert lane_journal.journal_first("WORK-1", "NODE_HELD", {"hold": "h1"}, ("hold",)) is False
    journal = paths.artifact_events_path("WORK-1")
    journal.parent.mkdir(parents=True)
    journal.write_text("")
    fields = {"task_key": "WORK-1", "reason": "wait", "hold": "h1"}
    assert lane_journal.journal_first("WORK-1", "NODE_HELD", fields, ("hold",)) is True
    assert lane_journal.journal_first("WORK-1", "NODE_HELD", {**fields, "reason": "changed"}, ("hold",)) is False
    assert lane_journal.journal_first("WORK-1", "NODE_HELD", {**fields, "hold": "h2"}, ("hold",)) is True
    assert lane_journal.journal_first("WORK-1", "NODE_PARKED", fields, ("hold",)) is True
    assert [(e["kind"], e["fields"]["hold"]) for e in _lane_events("WORK-1")] == [
        ("NODE_HELD", "h1"), ("NODE_HELD", "h2"), ("NODE_PARKED", "h1")]
