import pytest

from dockwright import pipeline, waiting_on
from dockwright.waiting_on import PositionFacts

START = {"type": "pipeline_started", "token_id": "t1", "node": "design", "index": 1, "holder": "a-design",
         "cwd": "/w", "manager": "mgr"}


def _fork(*rows):
    return {"type": "token_forked", "token_id": "t1", "node": "task",
            "rows": [{"row": r, "stage": 1, "token_id": f"t1/{r}"} for r in rows]}


def _row_request(row, rid):
    return {"type": "advance_requested", "token_id": f"t1/{row}", "request_id": rid,
            "edge": {"from": "design", "to": "task"}, "index_to": 1, "kind": "fork", "row": row}


def _row_commit(row, rid):
    return {"type": "advance_committed", "token_id": f"t1/{row}", "request_id": rid, "node": "task", "index": 1,
            "holder": f"a-row-{row}"}


def _request(rid, to, token_id="t1", frm="design"):
    return {"type": "advance_requested", "token_id": token_id, "request_id": rid, "edge": {"from": frm, "to": to},
            "index_to": 1, "kind": "advance"}


def _pending(rid, reason, token_id="t1"):
    return {"type": "advance_pending", "token_id": token_id, "request_id": rid, "reason": reason,
            "assignment_id": None}


def _value_of(token):
    return waiting_on.worker(token.node) if token.holder else waiting_on.stalled(token.node)


def _evaluate(*events, any_question=False):
    return waiting_on.evaluate(pipeline.fold(list(events)), _value_of, any_question)


@pytest.mark.parametrize("facts, expected", [
    (PositionFacts("design", question=True), "human:question"),
    (PositionFacts("design", question=True, alive=True, approval_dialog=True), "human:question"),
    (PositionFacts("design", alive=True, approval_dialog=True, gate=True), "human:approval-dialog"),
    (PositionFacts("design", approval_dialog=True, gate=True), "stalled:design"),
    (PositionFacts("design", alive=True, gate=True, limited=True), "human:gate"),
    (PositionFacts("design", alive=True, limited=True), "external:time"),
    (PositionFacts("design", alive=True, limited=True, reported=True), "stalled:design"),
    (PositionFacts("design", alive=True), "worker:design"),
    (PositionFacts("pr", alive=True, merge_wait=True), "worker:pr"),
    (PositionFacts("pr", alive=True, reported=True, merge_wait=True, tracker_watch=True), "external:merge"),
    (PositionFacts("pr", tracker_watch=True, finished=True), waiting_on.EXTERNAL_TRACKER),
    (PositionFacts("post-merge", alive=True, reported=True, finished=True), "none"),
    (PositionFacts("design"), "stalled:design"),
])
def test_the_first_true_row_gives_the_position_value(facts, expected):
    assert waiting_on.token_value(facts) == expected


@pytest.mark.parametrize("reason, expected", [
    ("paused", "external:time"),
    ("manager-limited", "external:time"),
    ("usage-cap", "external:time"),
    ("held", "external:time"),
    ("spawn-error", "stalled:task"),
    ("no_register", "stalled:task"),
])
def test_an_open_request_reads_time_only_on_a_usage_pause(reason, expected):
    work, tokens, requests = _evaluate(START, _request("r1", "task"), _pending("r1", reason))
    assert (work, tokens, requests) == (expected, {"t1": None}, {"r1": expected})


def test_a_request_with_no_pending_event_yet_reads_stalled_at_its_target():
    assert _evaluate(START, _request("r1", "task")) == ("stalled:task", {"t1": None}, {"r1": "stalled:task"})


def test_a_token_without_an_open_request_is_a_position():
    assert _evaluate(START) == ("worker:design", {"t1": "worker:design"}, {})


def test_forked_rows_waiting_on_a_usage_pause_read_time():
    work, tokens, requests = _evaluate(START, _fork("a", "b"), _row_request("a", "ra"),
                                       _pending("ra", "paused", "t1/a"), _row_request("b", "rb"),
                                       _pending("rb", "paused", "t1/b"))
    assert work == "external:time" and tokens == {"t1": None}
    assert requests == {"ra": "external:time", "rb": "external:time"}


def test_a_join_whose_spawn_fails_reads_stalled_at_the_join_target():
    work, tokens, requests = _evaluate(START, _fork("a"), _row_request("a", "ra"), _row_commit("a", "ra"),
                                       {"type": "token_retired", "token_id": "t1/a"},
                                       _request("rj", "pr", frm="task"), _pending("rj", "spawn-error"))
    assert (work, tokens, requests) == ("stalled:pr", {"t1": None}, {"rj": "stalled:pr"})


def test_a_row_whose_spawn_fails_outranks_a_working_row():
    work, tokens, requests = _evaluate(START, _fork("a", "b"), _row_request("a", "ra"), _row_commit("a", "ra"),
                                       _row_request("b", "rb"), _pending("rb", "spawn-error", "t1/b"))
    assert work == "stalled:task"
    assert tokens == {"t1": None, "t1/a": "worker:task"} and requests == {"rb": "stalled:task"}


def test_an_override_with_no_request_leaves_a_holderless_position():
    work, tokens, _ = _evaluate(START, {"type": "token_override", "token_id": "t1", "to": "design", "index": 1})
    assert (work, tokens) == ("stalled:design", {"t1": "stalled:design"})


def test_a_work_with_tokens_but_no_position_reads_stalled_at_its_first_token():
    assert _evaluate(START, _fork("a")) == ("stalled:task", {"t1": None}, {})


def test_any_open_question_of_the_work_lifts_it_to_human_question():
    assert _evaluate(START, any_question=True)[0] == "human:question"


def test_the_rank_orders_who_acts_first():
    values = ["none", "external:merge", waiting_on.EXTERNAL_TRACKER, "external:time", "worker:pr", "stalled:task",
              "human:gate", "human:approval-dialog", "human:question"]
    assert sorted(values, key=waiting_on.rank) == list(reversed(values))


@pytest.mark.parametrize("values, any_question, expected", [
    ([], False, "none"),
    ([], True, "human:question"),
    (["worker:task"], True, "human:question"),
    (["external:merge", "worker:task", "stalled:pr"], False, "stalled:pr"),
    (["none", waiting_on.EXTERNAL_TRACKER], False, waiting_on.EXTERNAL_TRACKER),
    (["worker:a", "worker:b"], False, "worker:a"),
])
def test_the_work_value_is_the_most_urgent_position(values, any_question, expected):
    assert waiting_on.work_value(values, any_question) == expected


@pytest.mark.parametrize("command, expected", [
    ("bash /Users/x/.claude/scripts/plannotator-gate /r/ --gate --json", True),
    ("/bin/bash /Users/x/.claude/scripts/plannotator-gate /r/ --gate --json", True),
    ("/Users/x/.claude/scripts/plannotator-gate /r/", True),
    ("FOO=1 BAR=2 zsh /Users/x/.claude/scripts/plannotator-gate /r/", True),
    ("grep plannotator-gate x", False),
    ("claude --settings w.json run plannotator-gate /r/ now", False),
    ("/bin/zsh -c source /Users/x/.claude/shell-snapshots/s.sh && eval 'bash /x/plannotator-gate /r/'", False),
    ("bash /Users/x/.claude/scripts/plannotator-gate sessions", False),
    ("bash /Users/x/.claude/scripts/plannotator-gate --help", False),
    ("bash /Users/x/.claude/scripts/plannotator-gate -h", False),
    ("", False),
])
def test_a_gate_launch_is_the_gate_script_itself_not_a_mention(command, expected):
    assert waiting_on.is_gate_launch(command) is expected


_WATCH = f"/Users/x/.claude/scripts/{waiting_on.TRACKER_WATCH_SCRIPT}"


@pytest.mark.parametrize("command, expected", [
    ("/opt/homebrew/Cellar/python@3.13/3.13.1/Frameworks/Python.framework/Versions/3.13/Resources/Python.app/"
     f"Contents/MacOS/Python {_WATCH} KEY-1 --poll 300", "KEY-1"),
    (f"python3 {_WATCH} --once KEY-2", "KEY-2"),
    (f"python3 {_WATCH} --poll 300 KEY-5", "KEY-5"),
    (f"python3 {_WATCH} --poll=300 --once KEY-6", "KEY-6"),
    (f"{_WATCH} KEY-3", "KEY-3"),
    (f"cat {_WATCH}", None),
    ("python3 /Users/x/.claude/scripts/other.py KEY-4", None),
    (f"python3 {_WATCH}", None),
    ("", None),
])
def test_a_tracker_watcher_is_named_by_its_script_and_its_key(command, expected):
    assert waiting_on.tracker_watch_key(command) == expected


_AUTHOR_WAIT = f"/Users/x/.claude/scripts/{waiting_on.AUTHOR_WAIT_SCRIPT}"


@pytest.mark.parametrize("command, expected", [
    (f"/opt/py/Python.app/Contents/MacOS/Python {_AUTHOR_WAIT} --task-key K-1 --node design --issue ISS-1 "
     "--after 9", ("K-1", "design")),
    (f"python3 {_AUTHOR_WAIT} --issue ISS-2 --node design --task-key K-2 --questions /q.md", ("K-2", "design")),
    (f"{_AUTHOR_WAIT} --task-key=K-3 --node=design --after 4", ("K-3", "design")),
    (f"python3 {_AUTHOR_WAIT} --task-key K-0 --node design --issue ISS-7 --node plan --task-key=K-7 --after 4",
     ("K-7", "plan")),
    (f"python3 {_AUTHOR_WAIT} --task-key K-4 --issue ISS-4 --after 4", None),
    (f"cat {_AUTHOR_WAIT} --task-key K-5 --node design", None),
    (f"python3 /Users/x/.claude/scripts/{waiting_on.TRACKER_WATCH_SCRIPT} K-6 --poll 300", None),
    ("", None),
])
def test_an_author_wait_is_named_by_its_work_and_its_node(command, expected):
    assert waiting_on.author_wait_key(command) == expected


def test_a_tracker_watcher_under_an_author_wait_counts_only_through_the_author_wait():
    commands = {10: f"python3 {_AUTHOR_WAIT} --task-key K --node design --issue K", 11: f"python3 {_WATCH} K",
                12: "sh -c run", 13: f"python3 {_WATCH} K --poll 300"}
    parents = {10: 1, 11: 10, 12: 10, 13: 12}
    assert waiting_on.process_waits({"command_by_pid": commands, "parent_by_pid": parents}) == \
        (set(), {("K", "design")})
    commands[20], parents[20] = f"python3 {_WATCH} K", 1
    assert waiting_on.process_waits({"command_by_pid": commands, "parent_by_pid": parents}) == \
        ({"K"}, {("K", "design")})
    assert waiting_on.process_waits(None) == (set(), set())


def test_descends_from_walks_parents_and_stops_on_a_cycle_or_the_depth_limit():
    assert waiting_on.descends_from(3, 1, {3: 2, 2: 1}) is True
    assert waiting_on.descends_from(3, 9, {3: 2, 2: 3}) is False
    assert waiting_on.descends_from(3, 9, {}) is False
    assert waiting_on.descends_from(3, 3, {3: 2}) is False
    chain = {n: n - 1 for n in range(2, 30)}
    assert waiting_on.descends_from(17, 1, chain) is True
    assert waiting_on.descends_from(18, 1, chain) is False


def test_a_lease_wait_is_external_lease_and_outranks_a_merge_wait():
    assert waiting_on.token_value(PositionFacts(node="post-merge", lease_wait=True)) == "external:lease"
    assert waiting_on.token_value(PositionFacts(node="post-merge", lease_wait=True, merge_wait=True)) == "external:lease"
    assert waiting_on.token_value(PositionFacts(node="post-merge", merge_wait=True)) == "external:merge"
    assert waiting_on._ORDER.index("external:lease") < waiting_on._ORDER.index("external:merge")


def test_a_fresh_request_reads_worker_at_its_target_for_sixty_seconds():
    now = 1000.0
    assert waiting_on.request_value({"edge": {"to": "repo"}, "ts": now - 2}, now) == "worker:repo"
    assert waiting_on.request_value({"edge": {"to": "repo"}, "ts": now - 61}, now) == "stalled:repo"
    assert waiting_on.request_value({"edge": {"to": "repo"}, "ts": now - 2}) == "stalled:repo"
    assert waiting_on.request_value({"edge": {"to": "repo"}, "ts": now - 2, "pending": {"reason": "paused"}},
                                    now) == "external:time"


def test_a_fresh_token_without_a_holder_reads_worker_for_sixty_seconds():
    now = 1000.0
    assert waiting_on.token_value(PositionFacts("design", started_at=now - 10), now) == "worker:design"
    assert waiting_on.token_value(PositionFacts("design", started_at=now - 120), now) == "stalled:design"
    assert waiting_on.token_value(PositionFacts("design", started_at=now - 10)) == "stalled:design"
    assert waiting_on.token_value(PositionFacts("design", question=True, started_at=now - 10), now) == "human:question"


def test_the_fallback_branch_of_evaluate_gets_the_same_grace():
    now = 1000.0
    fold_ = pipeline.fold([START, _fork("a")])
    assert waiting_on.evaluate(fold_, _value_of, False, now=now, started_at_of=lambda t: now - 5)[0] == "worker:task"
    assert waiting_on.evaluate(fold_, _value_of, False, now=now, started_at_of=lambda t: now - 65)[0] == "stalled:task"
    assert waiting_on.evaluate(fold_, _value_of, False)[0] == "stalled:task"
    fresh = pipeline.fold([START, {**_request("r1", "task"), "ts": now - 3}])
    assert waiting_on.evaluate(fresh, _value_of, False, now=now)[0] == "worker:task"


def test_a_subscribed_idle_holder_reads_external_source_even_while_it_waits_on_a_merge():
    facts = waiting_on.PositionFacts(node="post-merge", alive=False, merge_wait=True, event_subs=("deploy",))
    assert waiting_on.token_value(facts, now=10.0) == "external:deploy"
    alive = waiting_on.PositionFacts(node="post-merge", alive=True, event_subs=("deploy",))
    assert waiting_on.token_value(alive, now=10.0) == "worker:post-merge"
    assert waiting_on.token_value(PositionFacts(node="design", lease_wait=True, event_subs=("tracker",))) == "external:tracker"


def test_an_event_wait_ranks_just_above_the_tracker_wait():
    assert waiting_on.rank("external:deploy") == waiting_on.rank(waiting_on.EXTERNAL_EVENT)
    assert waiting_on.rank(waiting_on.EXTERNAL_TRACKER) == waiting_on.rank("external:deploy") + 1
    assert waiting_on.rank("external:tracker") == waiting_on.rank("external:deploy")
    assert waiting_on.rank("external:time") < waiting_on.rank("external:deploy")
    assert waiting_on.rank("external:lease") > waiting_on.rank(waiting_on.EXTERNAL_TRACKER)
