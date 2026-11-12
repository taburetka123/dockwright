from dockwright import event_queue, paths
from dockwright.events import Event
from tests.test_mcp_tools import fresh_orchestrator_dir  # noqa: F401


def E(id, ts):
    return Event(id, ts, "exec", "incident", {"incident": id}, "", f"INCIDENT {id}", {})


def test_push_keeps_order_and_drops_a_repeated_id(fresh_orchestrator_dir):
    assert event_queue.push("r", [E("a", 1), E("b", 2)]) == 2
    assert event_queue.push("r", [E("b", 2), E("c", 3)]) == 1
    assert [e.id for e in event_queue.read("r")] == ["a", "b", "c"]
    assert event_queue.read("r")[0] == E("a", 1)


def R(id, ts):
    return Event(id, ts, "chat", "dm", {"channel": "D1"}, "", "s", {"reply": {"channel": "D1", "delivered_at": None}})


def test_update_patches_the_reply_payload_and_reports_a_missing_id(fresh_orchestrator_dir):
    event_queue.push("r", [R("a", 1)])
    assert event_queue.update("r", "a", {"delivered_at": 5.0, "delivered_sid": "w"}) is True
    assert event_queue.read("r")[0].payload["reply"] == {"channel": "D1", "delivered_at": 5.0, "delivered_sid": "w"}
    assert event_queue.update("r", "none", {"delivered_at": 1}) is False


def test_rekey_changes_the_id_in_place_and_the_old_id_acks_nothing(fresh_orchestrator_dir):
    event_queue.push("r", [R("a", 1), R("b", 2)])
    assert event_queue.rekey("r", "a", "a:esc", {"escalated_at": 9.0}) is True
    assert [e.id for e in event_queue.read("r")] == ["a:esc", "b"]
    assert event_queue.read("r")[0].payload["reply"]["escalated_at"] == 9.0
    assert event_queue.ack("r", "a") == (0, 2) and event_queue.ack("r", "a:esc") == (1, 1)
    assert event_queue.rekey("r", "none", "x", {}) is False


def test_ack_removes_one_or_all(fresh_orchestrator_dir):
    event_queue.push("r", [E("a", 1), E("b", 2), E("c", 3)])
    assert event_queue.ack("r", "b") == (1, 2)
    assert [e.id for e in event_queue.read("r")] == ["a", "c"]
    assert event_queue.ack("r", "*") == (2, 0)
    assert event_queue.read("r") == []
    assert event_queue.ack("r", "zzz") == (0, 0)


def test_touch_nudged_keeps_a_concurrent_ack(fresh_orchestrator_dir):
    event_queue.push("r", [E("a", 1), E("b", 2)])
    event_queue.touch_nudged("r", 5.0)
    assert event_queue.nudged("r") == 5.0
    event_queue.ack("r", "a")
    event_queue.touch_nudged("r", 6.0)
    assert [e.id for e in event_queue.read("r")] == ["b"]
    assert event_queue.nudged("r") == 6.0


def test_an_unknown_rule_reads_empty(fresh_orchestrator_dir):
    assert event_queue.read("none") == []
    assert event_queue.nudged("none") is None


def test_the_file_is_written_atomically(fresh_orchestrator_dir):
    event_queue.push("r", [E("a", 1)])
    names = [p.name for p in paths.EVENTS_QUEUES.iterdir()]
    assert names == ["r.json"]
