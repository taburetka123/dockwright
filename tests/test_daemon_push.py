from dockwright import board, daemon_push, paths, state

START = 1_000.0


def _q(topic, asked_at):
    return {"kind": "question", "topic": topic, "anchor": asked_at}


def _gate(topic):
    return {"kind": "gate", "topic": topic, "anchor": None}


class Sender:
    def __init__(self, rc=0):
        self.sent = []
        self.rc = rc

    def __call__(self, title, text):
        self.sent.append((title, text))
        return self.rc


def _run(now, items, send):
    return daemon_push.run(now, START, items, send=send)


def test_one_notification_per_wait_after_the_threshold():
    send = Sender()
    assert _run(2_000.0, [_q("WORK-1", 1_821.0)], send) == []
    records = _run(2_001.0, [_q("WORK-1", 1_821.0)], send)
    assert records == [{"type": "push", "topic": "WORK-1", "kinds": ["question"], "ok": True, "rc": 0}]
    assert send.sent == [("dockwright", "WORK-1: question")]
    assert _run(2_500.0, [_q("WORK-1", 1_821.0), _q("WORK-1", 2_400.0)], send) == []
    assert len(send.sent) == 1


def test_a_topic_that_frees_and_waits_again_gets_one_more():
    send = Sender()
    _run(2_001.0, [_q("WORK-1", 1_821.0)], send)
    assert _run(2_100.0, [], send) == []
    assert _run(2_200.0, [_q("WORK-1", 2_150.0)], send) == []
    assert [r["type"] for r in _run(2_330.0, [_q("WORK-1", 2_150.0)], send)] == ["push"]
    assert len(send.sent) == 2


def test_a_gate_waits_from_the_first_pass_that_sees_it():
    send = Sender()
    assert _run(2_000.0, [_gate("WORK-2")], send) == []
    assert _run(2_179.0, [_gate("WORK-2")], send) == []
    assert _run(2_180.0, [_gate("WORK-2"), {"kind": "dialog", "topic": "WORK-2", "anchor": 5.0}], send)[0]["kinds"] == ["dialog", "gate"]
    assert send.sent == [("dockwright", "WORK-2: permission dialog, gate")]


def test_the_hourly_cap_counts_the_rest_into_the_next_notification():
    send = Sender()
    items = [_q(f"WORK-{n}", 1_500.0) for n in range(7)]
    records = _run(2_000.0, items, send)
    assert [r["type"] for r in records].count("push") == 6
    assert [r["type"] for r in records].count("push_capped") == 1
    later = _run(5_700.0, items + [_q("WORK-9", 5_400.0)], send)
    assert [r["topic"] for r in later] == ["WORK-9"]
    assert send.sent[-1] == ("dockwright", "WORK-9: question (+1 more waited while capped)")


def test_a_wait_that_began_before_start_is_never_pushed():
    send = Sender()
    assert _run(5_000.0, [_q("WORK-1", 900.0)], send) == []
    assert send.sent == []


def test_other_inbox_kinds_are_ignored():
    send = Sender()
    items = [{"kind": "undelivered", "topic": "WORK-1", "anchor": 1_100.0},
             {"kind": "review-by 2026-10-01", "topic": "WORK-2", "anchor": 1_100.0}]
    assert _run(5_000.0, items, send) == []


def test_a_failed_send_is_recorded_and_not_retried():
    send = Sender(rc=1)
    records = _run(2_001.0, [_q("WORK-1", 1_821.0)], send)
    assert records == [{"type": "push", "topic": "WORK-1", "kinds": ["question"], "ok": False, "rc": 1}]
    assert _run(2_100.0, [_q("WORK-1", 1_821.0)], send) == []


def test_channel_none_sends_nothing_and_keeps_no_state(tmp_path, monkeypatch):
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text('[daemon]\npush = "none"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    send = Sender()
    assert _run(9_000.0, [_q("WORK-1", 1_100.0)], send) == []
    assert send.sent == []
    assert not daemon_push.state_path().exists()


def test_state_lives_under_the_daemon_dir():
    _run(2_001.0, [_q("WORK-1", 1_821.0)], Sender())
    saved = state.read_json(paths.DAEMON / "push-state.json")
    assert saved["topics"]["WORK-1"] == {"since": 1_821.0, "notified": True}


def test_a_hand_edited_state_of_the_wrong_shape_is_ignored():
    state.write_json_atomic(daemon_push.state_path(), {"topics": {}, "sent": 5, "skipped": True})
    send = Sender()
    assert [r["type"] for r in _run(2_001.0, [_q("WORK-1", 1_821.0)], send)] == ["push"]
    assert send.sent == [("dockwright", "WORK-1: question")]


def test_a_question_topic_missing_for_one_pass_waits_again_before_a_repeat():
    send = Sender()
    _run(2_001.0, [_q("WORK-1", 1_821.0)], send)
    assert _run(2_060.0, [], send) == []
    assert _run(2_120.0, [_q("WORK-1", 1_821.0)], send) == []
    assert _run(2_239.0, [_q("WORK-1", 1_821.0)], send) == []
    assert [r["type"] for r in _run(2_240.0, [_q("WORK-1", 1_821.0)], send)] == ["push"]


def test_a_question_asked_after_the_last_pass_is_a_new_wait():
    send = Sender()
    _run(2_001.0, [_q("WORK-1", 1_821.0)], send)
    assert _run(2_050.0, [_q("WORK-1", 2_030.0)], send) == []
    assert [r["type"] for r in _run(2_210.0, [_q("WORK-1", 2_030.0)], send)] == ["push"]


def test_a_wait_that_restarts_while_the_channel_is_off_is_pushed(tmp_path, monkeypatch):
    send = Sender()
    _run(2_001.0, [_q("WORK-1", 1_821.0)], send)
    cfg = tmp_path / "dockwright.toml"
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    cfg.write_text('[daemon]\npush = "none"\n')
    _run(3_000.0, [], send)
    _run(4_000.0, [_q("WORK-1", 3_500.0)], send)
    cfg.write_text('[daemon]\npush = "macos"\n')
    assert [r["type"] for r in _run(4_100.0, [_q("WORK-1", 3_500.0)], send)] == ["push"]
    assert len(send.sent) == 2


def test_a_failed_send_keeps_the_capped_count_for_the_next_one(tmp_path, monkeypatch):
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text("[daemon]\npush_cap_per_hour = 1\n")
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    _run(2_000.0, [_q("WORK-0", 1_500.0), _q("WORK-1", 1_500.0)], Sender())
    _run(5_700.0, [_q("WORK-2", 5_400.0)], Sender(rc=1))
    send = Sender()
    _run(9_400.0, [_q("WORK-3", 9_100.0)], send)
    assert send.sent == [("dockwright", "WORK-3: question (+1 more waited while capped)")]


def test_the_cap_keeps_the_longest_waits():
    items = [_q(f"A{n}", 1_800.0) for n in range(6)] + [_q("Z-oldest", 1_000.5)]
    records = _run(2_000.0, items, Sender())
    assert [r["topic"] for r in records if r["type"] == "push_capped"] == ["A5"]
    assert records[0]["topic"] == "Z-oldest"


def test_every_human_wait_kind_the_board_lists_is_pushed(monkeypatch):
    monkeypatch.setitem(board.HUMAN_KINDS, "human:review", "review")
    send = Sender()
    item = {"kind": "review", "topic": "WORK-3", "anchor": None}
    assert _run(2_000.0, [item], send) == []
    assert [r["type"] for r in _run(2_180.0, [item], send)] == ["push"]
    assert send.sent == [("dockwright", "WORK-3: review")]


def test_a_new_question_joins_a_wait_that_was_not_pushed_yet():
    send = Sender()
    _run(2_000.0, [_gate("WORK-2")], send)
    assert _run(2_120.0, [_gate("WORK-2"), _q("WORK-2", 2_100.0)], send) == []
    assert [r["kinds"] for r in _run(2_180.0, [_gate("WORK-2"), _q("WORK-2", 2_100.0)], send)] == [["gate", "question"]]


def test_a_failed_send_is_recorded_once_and_never_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DAEMON", tmp_path / "daemon")
    paths.DAEMON.mkdir()
    send = Sender(rc=1)
    records = _run(2_001.0, [_q("WORK-1", 1_821.0)], send)
    assert records == [{"type": "push", "topic": "WORK-1", "kinds": ["question"], "ok": False, "rc": 1}]
    assert _run(2_100.0, [_q("WORK-1", 1_821.0)], send) == []
    assert len(send.sent) == 1


def test_the_state_is_written_before_the_send_so_a_crash_mid_send_does_not_repeat_it(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DAEMON", tmp_path / "daemon")
    paths.DAEMON.mkdir()
    order = []

    def send(title, text):
        order.append(("send", state.read_json(daemon_push.state_path())["topics"]["WORK-1"]["notified"]))
        return 0

    _run(2_001.0, [_q("WORK-1", 1_821.0)], send)
    assert order == [("send", True)]
