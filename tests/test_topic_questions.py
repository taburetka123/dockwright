import os
import time
from pathlib import Path

import pytest

from dockwright import board, lane_io, lanes, monitor, paths, registry, state
from dockwright import mcp_server as ms
from tests.test_mcp_tools import fresh_orchestrator_dir  # noqa: F401
from tests.test_status_row import sr


@pytest.fixture
def lane_root(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setattr(paths, "LANE_HEALTH", fresh_orchestrator_dir / "lane-health")
    monkeypatch.setattr(lane_io, "reader_is_dead", lambda fd=1: False)
    return fresh_orchestrator_dir


def _worker(sid, name, *, ticket=None, parent="mgr", kind="worker"):
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {"claude_sid": sid, "name": name, "agent": "worker",
                                                          "kind": kind, "parent_name": parent})
    if ticket:
        state.write_json_atomic(paths.assignment_path(sid), {"claude_sid": sid, "name": name, "ticket": ticket,
                                                             "assignment_id": f"a-{sid}"})


def test_a_keyed_question_is_written_once_under_by_task_and_still_listed_everywhere(fresh_orchestrator_dir):
    _worker("s1", "w1", ticket="K-1")
    qid = ms._write_question("s1", "w1", "which one?", "mgr", task_key="K-1")
    assert (paths.question_task_dir("K-1") / f"{qid}.json").is_file()
    assert not (paths.question_dir_for("mgr") / f"{qid}.json").exists()
    assert [q["question_id"] for q in ms.list_pending_questions_impl("mgr")] == [qid]
    assert [q["question_id"] for q in ms._topic_questions("K-1")] == [qid]
    assert [q["question_id"] for q in ms._pending_questions("K-1")] == [qid]
    assert len(board._questions()) == 1
    assert [e.stem for e, bucket in monitor.LaneScope({"name": "mgr", "sid": "m"}).question_entries()] == [qid]


def test_an_unkeyed_question_still_lands_in_the_managers_folder(fresh_orchestrator_dir):
    _worker("s2", "w2")
    qid = ms._write_question("s2", "w2", "hm?", "mgr")
    assert (paths.question_dir_for("mgr") / f"{qid}.json").is_file()


def test_writing_a_question_or_a_done_touches_the_daemon_wake_file(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setattr(paths, "DAEMON", fresh_orchestrator_dir / "daemon")
    monkeypatch.setattr(paths, "DAEMON_WAKE", fresh_orchestrator_dir / "daemon" / "wake")
    _worker("s3", "w3")
    ms._write_question("s3", "w3", "q", "mgr")
    first = paths.DAEMON_WAKE.stat().st_mtime
    import time
    time.sleep(0.01)
    ms.worker_done_impl("s3", "done")
    assert paths.DAEMON_WAKE.stat().st_mtime >= first


def test_the_answer_records_first_and_removes_every_copy(fresh_orchestrator_dir, monkeypatch):
    _worker("s1", "w1", ticket="K-1")
    qid = ms._write_question("s1", "w1", "which one?", "mgr", task_key="K-1")
    order = []
    real_put, real_write = ms._put_decision, state.write_json_atomic
    monkeypatch.setattr(ms, "_put_decision", lambda *a, **k: (order.append("record"), real_put(*a, **k))[1])

    def spy(path, payload):
        if path.parent == paths.ANSWERS:
            order.append("answer")
        return real_write(path, payload)

    monkeypatch.setattr(state, "write_json_atomic", spy)
    ms.answer_question_impl(qid, "the first", decided_by="engineer")
    assert order == ["record", "answer"]
    assert ms._find_question_paths(qid) == []


def test_an_answer_the_previous_release_left_a_copy_of_is_cleared_when_the_worker_reads_it(fresh_orchestrator_dir):
    _worker("s1", "w1", ticket="K-1")
    qid = ms._write_question("s1", "w1", "which one?", "mgr", task_key="K-1")
    state.write_json_atomic(paths.ANSWERS / f"{qid}.json", {"answer": "the first", "worker_sid": "s1"})
    assert ms._try_consume_answer(qid, "s1") == "the first"
    assert ms._find_question_paths(qid) == []
    assert ms._topic_questions("K-1") == []


def test_a_strict_answer_writes_nothing_when_the_record_fails(fresh_orchestrator_dir, monkeypatch):
    _worker("s1", "w1", ticket="K-1")
    qid = ms._write_question("s1", "w1", "which one?", "mgr", task_key="K-1")

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ms, "_put_decision", broken)
    with pytest.raises(OSError):
        ms.answer_question_impl(qid, "x", decided_by="engineer", strict=True)
    assert not (paths.ANSWERS / f"{qid}.json").exists()
    assert len(ms._find_question_paths(qid)) == 1
    ms.answer_question_impl(qid, "x")
    assert (paths.ANSWERS / f"{qid}.json").exists()


def test_a_strict_answer_records_under_the_topic_when_the_assignment_is_gone(fresh_orchestrator_dir):
    _worker("s1", "w1", ticket="K-1")
    qid = ms._write_question("s1", "w1", "which one?", "mgr", task_key="K-1")
    paths.assignment_path("s1").unlink()
    ms.answer_question_impl(qid, "the first", decided_by="engineer", strict=True)
    assert ms.artifact_get_impl("K-1", "decision", f"q-{qid[:8]}")["decided_by"] == "engineer"


def test_a_strict_answer_with_no_topic_writes_nothing(fresh_orchestrator_dir):
    _worker("s4", "w4")
    qid = ms._write_question("s4", "w4", "?", "mgr")
    with pytest.raises(ValueError, match="no topic"):
        ms.answer_question_impl(qid, "x", decided_by="engineer", strict=True)
    assert not (paths.ANSWERS / f"{qid}.json").exists()


def test_a_subworker_question_gets_no_topic_copy(fresh_orchestrator_dir):
    _worker("s2", "w1-child", ticket="K-1", kind="subworker")
    qid = ms._write_question("s2", "w1-child", "?", "mgr", task_key="K-1")
    assert not paths.question_task_dir("K-1").exists()
    assert state.read_json(paths.question_dir_for("mgr") / f"{qid}.json")["task_key"] is None
    assert ms._topic_questions("K-1") == []


def test_a_question_without_a_key_keeps_the_old_path(fresh_orchestrator_dir):
    _worker("s3", "w3", ticket="K-9")
    qid = ms._write_question("s3", "w3", "legacy?", "mgr")
    assert not (paths.QUESTIONS / paths.QUESTION_BY_TASK).exists()
    assert [q["question_id"] for q in ms._topic_questions("K-9")] == [qid]
    assert [q["question_id"] for q in ms.list_pending_questions_impl("mgr")] == [qid]


def test_a_session_end_counts_each_question_once(fresh_orchestrator_dir):
    _worker("s1", "w1", ticket="K-1")
    qid = ms._write_question("s1", "w1", "which one?", "mgr", task_key="K-1")
    assert registry._drop_questions_for_worker("s1") == 1
    assert ms._find_question_paths(qid) == []


def test_a_keyed_question_is_found_under_its_task(fresh_orchestrator_dir):
    _worker("s1", "w1", ticket="K-1")
    qid = ms._write_question("s1", "w1", "which one?", "mgr", task_key="K-1")
    assert ms._find_question_path(qid) == paths.question_task_dir("K-1") / f"{qid}.json"


def test_a_manager_named_by_task_gets_its_own_folder(fresh_orchestrator_dir):
    assert paths.question_dir_for("by-task") == paths.QUESTIONS / "_by-task"


def test_the_backlog_counts_a_keyed_question_once(fresh_orchestrator_dir):
    _worker("s1", "w1", ticket="K-1")
    ms._write_question("s1", "w1", "which one?", "mgr", task_key="K-1")
    assert lanes._backlog("mgr", "questions", window=-1, now=10**12) == 1


def test_the_lane_emits_a_keyed_question_once(lane_root, capsys):
    _worker("s1", "w1", ticket="K-1")
    ms._write_question("s1", "w1", "which one?", "mgr", task_key="K-1")
    monitor.run_questions_scan({"name": "mgr"})
    assert capsys.readouterr().out.count("QUESTION w1 → ") == 1
    monitor.run_questions_scan({"name": "mgr"})
    assert "QUESTION" not in capsys.readouterr().out


def test_an_old_path_cursor_is_honoured(lane_root, capsys):
    _worker("s1", "w1", ticket="K-1")
    qid = ms._write_question("s1", "w1", "which one?", "mgr", task_key="K-1")
    seen = monitor._seen_file("questions", "mgr")
    seen.write_text(str(paths.question_dir_for("mgr") / f"{qid}.json") + "\n")
    monitor.run_questions_scan({"name": "mgr"})
    assert "QUESTION" not in capsys.readouterr().out


def test_the_fleet_menu_names_the_topic(fresh_orchestrator_dir):
    _worker("s1", "w1", ticket="K-1")
    ms._write_question("s1", "w1", "which one?", "mgr", task_key="K-1")
    records, question_sids = sr.collect(paths.ACTIVE, paths.QUESTIONS)
    assert question_sids == {"s1": {"task_key": "K-1", "text": "which one?"}}
    record = next(r for r in records if r.get("claude_sid") == "s1")
    assert sr._menu_label(record, question_sids, "").startswith(f"{sr.MENU_STATE_ICON['question']} K-1 — which one?")


def test_a_keyed_question_the_previous_release_left_in_the_managers_folder_is_seen_by_every_reader(
        fresh_orchestrator_dir):
    _worker("s1", "w1", ticket="K-1")
    only_topic = ms._write_question("s1", "w1", "topic copy only?", "mgr", task_key="K-1")
    only_manager = ms._write_question("s1", "w1", "manager copy only?", "mgr", task_key="K-1")
    legacy = paths.question_task_dir("K-1") / f"{only_manager}.json"
    paths.question_dir_for("mgr").mkdir(parents=True, exist_ok=True)
    legacy.rename(paths.question_dir_for("mgr") / legacy.name)
    both = {only_topic, only_manager}
    assert {q["question_id"] for q in ms._topic_questions("K-1")} == both
    assert {q["question_id"] for q in ms.list_pending_questions_impl("mgr")} == both
    assert ms._oldest_pending_question("s1")["question_id"] == only_topic


def test_a_strict_answer_is_retried_after_the_answer_write_failed(fresh_orchestrator_dir, monkeypatch):
    _worker("s1", "w1", ticket="K-1")
    qid = ms._write_question("s1", "w1", "which one?", "mgr", task_key="K-1")
    real_write = state.write_json_atomic
    failing = {"on": True}

    def flaky(path, payload):
        if failing["on"] and path.parent == paths.ANSWERS:
            raise OSError("disk full")
        return real_write(path, payload)

    monkeypatch.setattr(state, "write_json_atomic", flaky)
    with pytest.raises(OSError):
        ms.answer_question_impl(qid, "the first", decided_by="engineer", strict=True)
    failing["on"] = False
    ms.answer_question_impl(qid, "the second", decided_by="engineer", strict=True)
    assert state.read_json(paths.ANSWERS / f"{qid}.json")["answer"] == "the second"
    assert "the second" in ms.artifact_get_impl("K-1", "decision", f"q-{qid[:8]}")["content"]


def test_an_answer_records_under_the_topic_when_the_assignment_is_gone(fresh_orchestrator_dir):
    _worker("s1", "w1", ticket="K-1")
    qid = ms._write_question("s1", "w1", "which one?", "mgr", task_key="K-1")
    paths.assignment_path("s1").unlink()
    ms.answer_question_impl(qid, "the first")
    assert ms.artifact_get_impl("K-1", "decision", f"q-{qid[:8]}")["decided_by"] == "manager"


def test_the_fleet_menu_names_the_oldest_question(fresh_orchestrator_dir):
    _worker("s1", "w1")
    folder = paths.question_dir_for("mgr")
    state.write_json_atomic(folder / "b.json", {"question_id": "b", "worker_sid": "s1", "question": "older",
                                                "asked_at": 1.0, "task_key": "K-1"})
    state.write_json_atomic(folder / "a.json", {"question_id": "a", "worker_sid": "s1", "question": "newer",
                                                "asked_at": 2.0, "task_key": "K-1"})
    _, question_sids = sr.collect(paths.ACTIVE, paths.QUESTIONS)
    assert question_sids["s1"]["text"] == "older"


def _manager(sid, name, domain="general"):
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {"claude_sid": sid, "name": name, "agent": "manager",
                                                          "domain": domain, "pid": os.getpid()})


def test_ask_engineer_writes_a_keyed_record_marked_as_the_managers(fresh_orchestrator_dir):
    _manager("m1", "mgr")
    result = ms.ask_engineer_impl("m1", "ship it?", task_key="K-1")
    qid = result["question_id"]
    record = state.read_json(paths.question_task_dir("K-1") / f"{qid}.json")
    assert record["asked_by"] == "manager"
    assert record["domain"] == "general"
    assert record["parent_manager_name"] == "mgr"
    assert record["worker_sid"] == "m1" and record["worker_name"] == "mgr"
    assert result == {"ok": True, "question_id": qid, "task_key": "K-1"}


def test_ask_engineer_without_a_key_lands_on_the_domain_topic(fresh_orchestrator_dir):
    _manager("m2", "mgr2", domain="product")
    assert ms.ask_engineer_impl("m2", "q?")["task_key"] == "fleet-product"


def test_ask_engineer_without_a_domain_lands_on_fleet_general(fresh_orchestrator_dir):
    state.write_json_atomic(paths.ACTIVE / "m3.json", {"claude_sid": "m3", "name": "mgr3", "agent": "manager",
                                                       "pid": os.getpid()})
    result = ms.ask_engineer_impl("m3", "q?")
    assert result["task_key"] == "fleet-general"
    assert state.read_json(paths.question_task_dir("fleet-general") / f"{result['question_id']}.json")["domain"] == "general"


def test_ask_engineer_refuses_a_worker_and_a_nested_session(fresh_orchestrator_dir):
    _worker("s1", "w1")
    with pytest.raises(ValueError, match="not a live manager"):
        ms.ask_engineer_impl("s1", "q?")
    state.write_json_atomic(paths.ACTIVE / "m4.json", {"claude_sid": "m4", "name": "mgr4", "agent": "manager",
                                                       "nested": True, "pid": os.getpid()})
    with pytest.raises(ValueError, match="not a live manager"):
        ms.ask_engineer_impl("m4", "q?")
    with pytest.raises(ValueError, match="not a live manager"):
        ms.ask_engineer_impl("nobody", "q?")


def test_ask_engineer_refuses_a_key_that_is_not_a_topic(fresh_orchestrator_dir):
    _manager("m5", "mgr5")
    with pytest.raises(ValueError):
        ms.ask_engineer_impl("m5", "q?", task_key="not a key!")


def test_a_worker_question_carries_no_asked_by_field(fresh_orchestrator_dir):
    _worker("s1", "w1", ticket="K-1")
    qid = ms._write_question("s1", "w1", "q", "mgr", task_key="K-1")
    record = state.read_json(paths.question_task_dir("K-1") / f"{qid}.json")
    assert "asked_by" not in record and "domain" not in record


def _load_script(path, root):
    import importlib.util
    home = root / "home"
    (home / ".claude" / "dockwright").mkdir(parents=True, exist_ok=True)
    saved = {k: os.environ.get(k) for k in ("HOME", "DOCKWRIGHT_STATE_DIR")}
    os.environ["HOME"], os.environ["DOCKWRIGHT_STATE_DIR"] = str(home), str(root)
    try:
        spec = importlib.util.spec_from_file_location(path.stem + "_engineer_ask", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    if hasattr(mod, "ORCH_ROOT"):
        mod.ORCH_ROOT, mod.ACTIVE, mod.QUESTIONS = root, root / "active", root / "questions"
    return mod


REPO = Path(__file__).resolve().parent.parent


def _engineer_ask_beside_a_worker_question(root):
    _manager("m1", "mgr")
    _worker("s1", "w1", ticket="K-1")
    qid = ms.ask_engineer_impl("m1", "ship?", task_key="K-1")["question_id"]
    wq = ms._write_question("s1", "w1", "which?", "mgr", task_key="K-1")
    return qid, wq


def _stems(entries):
    return {Path(e).stem if not isinstance(e, tuple) else e[0].stem for e in entries}


SKIPPING_READERS = [
    ("registry.manager_question_paths", lambda root: _stems(registry.manager_question_paths("mgr"))),
    ("monitor.LaneScope.question_entries",
     lambda root: _stems(monitor.LaneScope({"name": "mgr", "sid": "m"}).question_entries())),
    ("daemon.DaemonScope.question_entries",
     lambda root: _stems(__import__("dockwright.daemon", fromlist=["x"]).DaemonScope(start=0.0, now=time.time())
                         .question_entries())),
    ("list_pending_questions_impl", lambda root: {q["question_id"] for q in ms.list_pending_questions_impl("mgr")}),
    ("_oldest_pending_question", lambda root: {(ms._oldest_pending_question(sid, "mgr") or {}).get("question_id")
                                               for sid in ("m1", "s1")}),
    ("_pending_question_ids", lambda root: set(ms._pending_question_ids("K-1"))),
    ("monitor._has_pending_question_for_sid",
     lambda root: {sid for sid in ("m1", "s1") if monitor._has_pending_question_for_sid(sid, 0.0)}),
    ("sweep._pending_question_sids", lambda root: __import__("dockwright.sweep", fromlist=["x"])._pending_question_sids()),
    ("stale_monitor._pending_question_sids",
     lambda root: _load_script(REPO / "src" / "dockwright" / "stale_monitor.py", root)._pending_question_sids()),
    ("bootlite_watchdog._pending_question_sids",
     lambda root: _load_script(REPO / "deploy" / "scripts" / "bootlite_watchdog.py", root)._pending_question_sids()),
]


@pytest.mark.parametrize("reader, read", SKIPPING_READERS, ids=[r[0] for r in SKIPPING_READERS])
def test_each_reader_skips_an_engineer_ask(fresh_orchestrator_dir, reader, read):
    qid, wq = _engineer_ask_beside_a_worker_question(fresh_orchestrator_dir)
    seen = read(fresh_orchestrator_dir)
    assert qid not in seen and "m1" not in seen
    assert wq in seen or "s1" in seen


DROPPERS = [
    ("registry._drop_questions_for_worker", lambda root: registry._drop_questions_for_worker("m1")),
    ("stale_monitor._drop_questions_of",
     lambda root: _load_script(REPO / "src" / "dockwright" / "stale_monitor.py", root)._drop_questions_of("m1")),
    ("preflight_cleanup._drop_questions_for_worker",
     lambda root: _load_script(REPO / "deploy" / "scripts" / "preflight_cleanup.py", root)
     ._drop_questions_for_worker("m1")),
]


@pytest.mark.parametrize("dropper, drop", DROPPERS, ids=[d[0] for d in DROPPERS])
def test_each_dropper_keeps_an_engineer_ask(fresh_orchestrator_dir, dropper, drop):
    qid, wq = _engineer_ask_beside_a_worker_question(fresh_orchestrator_dir)
    drop(fresh_orchestrator_dir)
    assert (paths.question_task_dir("K-1") / f"{qid}.json").is_file()
    registry._drop_questions_for_worker("s1")
    assert not (paths.question_task_dir("K-1") / f"{wq}.json").exists()


KEEPING_READERS = [
    ("list_pending_questions_impl(include_engineer_asks=True)",
     lambda: {q["question_id"] for q in ms.list_pending_questions_impl("mgr", include_engineer_asks=True)}),
    ("list_pending_questions tool include_engineer_asks=True",
     lambda: {q["question_id"] for q in ms.list_pending_questions(manager_sid=None, include_engineer_asks=True)}),
    ("board._questions", lambda: {q["question_id"] for q in board._questions()}),
    ("_topic_questions", lambda: {q["question_id"] for q in ms._topic_questions("K-1")}),
    ("_pending_questions", lambda: {q["question_id"] for q in ms._pending_questions("K-1")}),
]


@pytest.mark.parametrize("reader, read", KEEPING_READERS, ids=[r[0] for r in KEEPING_READERS])
def test_each_board_reader_sees_an_engineer_ask(fresh_orchestrator_dir, reader, read):
    qid, wq = _engineer_ask_beside_a_worker_question(fresh_orchestrator_dir)
    assert read() == {qid, wq}


def test_the_list_pending_questions_tool_skips_an_engineer_ask_without_the_flag(fresh_orchestrator_dir):
    qid, wq = _engineer_ask_beside_a_worker_question(fresh_orchestrator_dir)
    assert {q["question_id"] for q in ms.list_pending_questions(manager_sid=None)} == {wq}


def test_a_managers_session_end_and_preflight_prune_keep_its_asks(fresh_orchestrator_dir, monkeypatch):
    from dockwright import hooks
    qid, _ = _engineer_ask_beside_a_worker_question(fresh_orchestrator_dir)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "m1")
    monkeypatch.setattr(hooks, "_read_hook_input", lambda: {"session_id": "m1"}, raising=False)
    registry._drop_questions_for_worker("m1")
    assert (paths.question_task_dir("K-1") / f"{qid}.json").is_file()
    state.write_json_atomic(paths.ACTIVE / "m1.json", {"claude_sid": "m1", "name": "mgr", "agent": "manager",
                                                       "domain": "general", "pid": 2 ** 22 - 7})
    pre = _load_script(REPO / "deploy" / "scripts" / "preflight_cleanup.py", fresh_orchestrator_dir)
    monkeypatch.setattr(pre, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(pre, "_live_pane_ids", lambda: set())
    pruned, _ = pre._prune_active()
    assert "mgr" in pruned
    assert (paths.question_task_dir("K-1") / f"{qid}.json").is_file()


def test_handoff_recovery_and_attach_carry_the_managers_open_asks(fresh_orchestrator_dir, monkeypatch):
    qid, wq = _engineer_ask_beside_a_worker_question(fresh_orchestrator_dir)
    monkeypatch.setattr(ms, "_find_manager_record", lambda: state.read_json(paths.ACTIVE / "m1.json"))
    handoff = state.read_json(Path(ms.prepare_handoff_impl("m1", "summary", "test")["path"]))
    assert {q["question_id"] for q in handoff["questions_snapshot"]} == {qid, wq}
    monkeypatch.setattr(ms, "_recovery_target_liveness", lambda sid, record: None)
    recovery = state.read_json(Path(ms.prepare_recovery_handoff_impl("m1")["path"]))
    assert {q["question_id"] for q in recovery["questions_snapshot"]} == {qid, wq}
    assert {q["question_id"] for q in ms.attach_existing_impl("mgr")["orphan_questions"]} == {qid, wq}
    assert {q["question_id"] for q in ms.list_pending_questions_impl("mgr")} == {wq}


def _answer_line(key, qid, tail):
    return (f'ANSWER {key} {qid[:8]} → artifact_get("{key}", "decision", "q-{qid[:8]}") — the engineer answered '
            f"on the board or the CLI: read the full answer there, act on it, do not re-ask — {tail}")


def _outbox(name):
    return sorted(paths.notify_outbox_dir_for(name).glob("*.json")) if paths.notify_outbox_dir_for(name).is_dir() else []


def test_an_engineer_ask_answered_from_the_board_pages_the_live_manager(fresh_orchestrator_dir):
    _manager("m1", "mgr", "general")
    qid = ms.ask_engineer_impl("m1", "ship?\nmore", task_key="K-1")["question_id"]
    result = ms.answer_question_impl(qid, "yes, ship\nsecond line", decided_by="engineer", strict=True, via="board")
    assert result == {"ok": True, "question_id": qid, "task_key": "K-1"}
    d = ms.artifact_get_impl("K-1", "decision", f"q-{qid[:8]}")
    assert d["decided_by"] == "engineer" and "yes, ship" in d["content"]
    entries = _outbox("mgr")
    assert len(entries) == 1
    payload = state.read_json(entries[0])
    assert payload["source"] == "board"
    assert payload["line"] == _answer_line("K-1", qid, "yes, ship")
    assert not (paths.ANSWERS / f"{qid}.json").exists()
    assert ms._topic_questions("K-1") == []


def test_the_answer_tail_is_the_first_line_cut_at_300(fresh_orchestrator_dir):
    _manager("m1", "mgr", "general")
    qid = ms.ask_engineer_impl("m1", "ship?", task_key="K-1")["question_id"]
    ms.answer_question_impl(qid, "x" * 400 + "\nsecond", decided_by="engineer", via="board")
    assert state.read_json(_outbox("mgr")[0])["line"].endswith(" — " + "x" * 300)


def test_the_answer_goes_to_the_first_live_manager_of_the_domain_by_name(fresh_orchestrator_dir):
    _manager("m0", "zed-mgr", "general")
    qid = ms.ask_engineer_impl("m0", "ship?", task_key="K-1")["question_id"]
    (paths.ACTIVE / "m0.json").unlink()
    _manager("m2", "beta-mgr", "general")
    _manager("m3", "alpha-mgr", "general")
    _manager("m4", "other-mgr", "product")
    ms.answer_question_impl(qid, "yes", decided_by="engineer", via="board")
    assert len(_outbox("alpha-mgr")) == 1
    assert _outbox("beta-mgr") == [] and _outbox("zed-mgr") == [] and _outbox("other-mgr") == []


def test_the_answer_queues_under_the_recorded_name_when_no_manager_of_the_domain_lives(fresh_orchestrator_dir):
    _manager("m0", "mgr", "general")
    qid = ms.ask_engineer_impl("m0", "ship?", task_key="K-1")["question_id"]
    (paths.ACTIVE / "m0.json").unlink()
    _manager("m4", "other-mgr", "product")
    ms.answer_question_impl(qid, "yes", decided_by="engineer", via="board")
    assert len(_outbox("mgr")) == 1 and _outbox("other-mgr") == []


def test_an_engineer_ask_on_a_closed_work_is_answered_from_the_board(fresh_orchestrator_dir, tmp_path, monkeypatch):
    import asyncio
    from dockwright import topic_message
    from tests.test_mcp_tools import _seed_snapshot
    _seed_snapshot("K-9", tmp_path)
    state.append_event(paths.artifact_events_path("K-9"), {"type": "work_closed", "token_id": "t1", "node": "design",
                                                           "reason": "done", "actor_sid": "m1"})
    _manager("m1", "mgr", "general")
    assert board.row("K-9", board.snapshot())["waiting_on"] == "none"
    qid = ms.ask_engineer_impl("m1", "archive it?", task_key="K-9")["question_id"]
    assert [i["kind"] for i in board.inbox()["waiting"] if i["key"] == "K-9"] == ["question"]
    result = asyncio.run(topic_message.deliver("K-9", "ok", kind="answer", decided_by="engineer", via="board",
                                               question_id=qid))
    assert result["ok"] and result["record"] == f"decision.q-{qid[:8]}"
    assert len(_outbox("mgr")) == 1


def test_a_managers_own_close_writes_the_record_and_pages_nothing(fresh_orchestrator_dir):
    _manager("m1", "mgr", "general")
    qid = ms.ask_engineer_impl("m1", "ship?", task_key="K-1")["question_id"]
    result = ms.answer_question_impl(qid, "his words", decided_by="engineer")
    assert result == {"ok": True, "question_id": qid, "task_key": "K-1"}
    assert ms.artifact_get_impl("K-1", "decision", f"q-{qid[:8]}")["decided_by"] == "engineer"
    assert _outbox("mgr") == []
    assert ms._topic_questions("K-1") == []


@pytest.mark.parametrize("via, decided_by, pages", [
    ("board", "engineer", 1),
    ("cli", "engineer", 1),
    ("cli", "manager", 0),
    ("mcp", "manager", 0),
    ("mcp", "engineer", 0),
])
def test_only_the_engineers_own_channel_pages_the_answer(fresh_orchestrator_dir, tmp_path, via, decided_by, pages):
    import asyncio
    from dockwright import topic_message
    from tests.test_mcp_tools import _seed_snapshot
    _seed_snapshot("K-9", tmp_path)
    _manager("m1", "mgr", "general")
    qid = ms.ask_engineer_impl("m1", "archive it?", task_key="K-9")["question_id"]
    asyncio.run(topic_message.deliver("K-9", "ok", kind="answer", decided_by=decided_by, via=via, question_id=qid))
    assert len(_outbox("mgr")) == pages
    assert ms._topic_questions("K-9") == []


def test_a_worker_answer_still_writes_the_answer_file_and_no_outbox(fresh_orchestrator_dir):
    _manager("m1", "mgr", "general")
    _worker("s1", "w1", ticket="K-1")
    qid = ms._write_question("s1", "w1", "which?", "mgr", task_key="K-1")
    assert ms.answer_question_impl(qid, "the first", decided_by="engineer") == {"ok": True}
    assert (paths.ANSWERS / f"{qid}.json").is_file()
    assert _outbox("mgr") == []


def test_an_answer_queued_under_a_dead_managers_name_carries_its_domain(fresh_orchestrator_dir):
    _manager("m0", "walrus", "general")
    qid = ms.ask_engineer_impl("m0", "ship?", task_key="K-1")["question_id"]
    (paths.ACTIVE / "m0.json").unlink()
    ms.answer_question_impl(qid, "yes", decided_by="engineer", via="board")
    assert state.read_json(_outbox("walrus")[0])["domain"] == "general"


def test_a_fresh_manager_adopts_the_answers_queued_for_its_domains_dead_managers(fresh_orchestrator_dir):
    _manager("m0", "walrus", "general")
    qid = ms.ask_engineer_impl("m0", "ship?", task_key="K-1")["question_id"]
    (paths.ACTIVE / "m0.json").unlink()
    ms.answer_question_impl(qid, "yes", decided_by="engineer", via="board")
    _manager("m8", "seal", "general")
    _manager("m7", "crab", "product")
    (paths.ACTIVE / "m7.json").unlink()
    __import__("dockwright.daemon", fromlist=["x"]).OutboxEmit("crab", source="board").emit("ANSWER other")
    __import__("dockwright.daemon", fromlist=["x"]).OutboxEmit("seal", source="board").emit("ANSWER live")
    result = ms.become_manager_impl("m9", iterm_sid="%1", domain="general", name="otter")
    assert result["name"] == "otter"
    lines = [state.read_json(p)["line"] for p in _outbox("otter")]
    assert len(lines) == 1 and lines[0].startswith("ANSWER K-1")
    assert _outbox("walrus") == [] and len(_outbox("crab")) == 1 and len(_outbox("seal")) == 1
