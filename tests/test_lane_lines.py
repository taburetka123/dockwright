import pytest

from dockwright import lane_lines

N = "n" * 48
K = "k" * 40
NODE = "p" * 16
CMD = "c" * 100

WAKING = {
    "ADVANCE_STUCK": [dict(task_key=K, to=NODE, reason=r) for r in ("paused", "spawn-error", "no_register")],
    "ADVANCE_UNREGISTERED": [dict(task_key=K, name=N, window="%123")],
    "NODE_PARKED": [dict(task_key=K, node=NODE, name=N),
                    dict(task_key=K, node=NODE, name=N, gate=True, reason="idle"),
                    dict(task_key=K, node=NODE, name=N, gate=True, gate_lost=True, token_id="t1/2", reason="")],
    "NODE_UNADVANCED": [dict(task_key=K, node=NODE, index=12, name=N),
                        dict(task_key=K, node=NODE, index=3, name=N, row="2")],
    "NODE_HELD": [dict(task_key=K, node=NODE, name=N, reason=""),
                  dict(task_key=K, node=NODE, name=N, token_id="t1/2", reason="")],
    "TRIGGER_POLL_FAILED": [dict(task_key=K, url="https://example.invalid/" + "u" * 96, trigger="merge"),
                            dict(task_key=K, url="https://example.invalid/u", trigger="approve"),
                            dict(task_key=K, url="https://example.invalid/u", node=NODE),
                            dict(task_key=K, url="ABC-1", trigger="status", status="Verified, category:done",
                                 to="release")],
    "RECONCILE_ERROR": [dict(task_key="*", error=""), dict(task_key=K, error="")],
    "DONE": [dict(worker=N, summary="")],
    "QUESTION": [dict(worker=N, question="")],
    "FINISHED_SILENTLY": [dict(name=N, exited=True, summary="")],
    "STALE_PROCESSING": [dict(name=N, minutes=120)],
    "NUDGED": [dict(name=N, why="120min rate-limited", repeat=True)],
    "LIMIT_CLEARED": [dict(clock="23:59", summary="")],
    "CONTEXT": [dict(name=N, used=966, window="1M", pct=97, threshold=t, own=o)
                for t, o in ((70, True), (80, True), (90, True), (80, False), (90, False))],
    "APPROVAL_PROMPT": [dict(name=N, excerpt=""), dict(name=N, excerpt="", source="hook")],
    "SAFEGUARD_PAUSE": [dict(name=N, variant=v, pane="%123", key="2", window=30)
                        for v in ("continued", "not-typed", "again", "unsettled", "unsent")]
                       + [dict(name=N, variant="continued", pane="%123", key="2", window=30, parent=N)],
    "ORPHAN_WINDOW": [dict(pane="%123", what="tab='" + N + "'", minutes=30)],
    "SWITCHED": [dict(account="a", new="b", subject="worker " + N + " auth-401 credential suspect")],
    "AUTH_401": [dict(name=N, account="b")],
    "AUTH_401_ESCALATED": [dict(account="b", subject="worker " + N, fix=CMD)],
    "IDLE_UNTASKED": [dict(name=N, minutes=30, variant=v) for v in ("no-input", "unsent-text", "nothing-typed")],
    "MODEL_CREDIT_EXHAUSTED": [dict(name=N, variant=v, spent="claude-opus-5-5", model="claude-opus-5-5[1m]",
                                    remedy="r" * 80, resume="s" * 60, switch="w" * 60, named=" " + "m" * 40,
                                    cascade=" " + "c" * 40, confirm_sec=600)
                               for v in ("unresolved", "same-family", "still-out", "not-typed",
                                         "unsent-text", "partial-switch")],
    "USAGE_CAP": [dict(breaches="b 5h 71% ≥ 70%; b 7d 81% ≥ 80%", until="2026-10-02 03:00", path="/" + "p" * 80)],
    "USAGE_CAP_CLEARED": [dict()],
    "USAGE_CAP_FILE": [dict(path="/" + "p" * 80, error="e" * 300)],
    "LANE_SILENT": [dict(lane="turn-ends", minutes=40, interval=5)],
    "ACCOUNT_HOLD": [dict(account="b", since="2026-10-16T10:00Z", reason=r) for r in ("banner", "appeal pending")],
    "FABLE_LIMIT": [dict(name=N, variant=v, reason="no tab", attempts=3, pid=4242, pane="%9",
                         switch="switch it to claude-opus-5-5 at effort xhigh", resume="tell it to resume")
                    for v in ("resume-refused", "close-refused", "human", "not-typed", "unsent-text")],
    "EVENT_STARTED": [dict(task_key=K, rule="sprint", event_id="tracker:K:sprint-assigned")],
    "EVENT_STALLED": [dict(rule="sprint", domain="general", since="2026-10-23T10:00:00Z")],
    "EVENT_RULE_FAILED": [dict(rule="sprint", error="RuntimeError: x")],
    "EVENT_PAGE": [dict(rule="review-ping", line="o/r#1 waits for review 9 business hours (ping 1) https://gh/o/r/pull/1",
                        event_id="github:o/r:1:review-wait:1:1")],
    "EVENT_POLL_FAILED": [dict(source="tracker", error=""), dict(source="chat", error="", skipped=True)],
    "CHAT_ARCHIVE_FAILED": [dict(failure_kind="auth", failure="AuthError")],
    "EVENT_ENV_INSECURE": [dict(path="/p", reason=r) for r in ("missing", "mode")],
    "DAEMON_STUCK": [dict(step="reconcile:silly-dragon", minutes=10)],
}

SILENT = {
    "ADVANCE_RECONCILED": (dict(task_key=K, to=NODE, name=N), False),
    "NODE_RESTARTED": (dict(task_key=K, to=NODE, name=N), False),
    "ADVANCE_SETTLED": (dict(task_key=K, to=NODE, name=N), False),
    "TRIGGER_FIRED": (dict(task_key=K, to=NODE, state="committed"), False),
    "TRIGGER_WAKE": (dict(task_key=K, node=NODE), True),
    "LEASE_WAKE": (dict(task_key=K, node=NODE, category="env:example/svc-a", record="decision.msg-1"), True),
    "EVENT_WAKE": (dict(task_key=K, node=NODE, record="decision.msg-1", events=2), True),
    "HANDED_OFF_CLOSED": (dict(task_key=K, name=N, reason="done"), True),
    "NODE_NO_SUMMARY": (dict(task_key=K, node=NODE, name=N), True),
    "RESUMED": (dict(name=N), True),
    "IDLE_NUDGED": (dict(name=N, minutes=30), True),
    "MODEL_FALLBACK": (dict(name=N, model=CMD, effort="xhigh", target="its pane", told=""), True),
    "ORPHANED_CHILD": (dict(name=N, parent=N), True),
    "FABLE_LIMIT": (dict(name=N, variant="moved", status="delivered"), True),
}

FIXED_TAIL = ("ORPHAN_WINDOW", "AUTH_401", "AUTH_401_ESCALATED", "IDLE_UNTASKED", "LANE_SILENT")


@pytest.mark.parametrize("kind", sorted(WAKING))
def test_every_waking_kind_renders_head_arrow_step_within_the_budget(kind):
    for fields in WAKING[kind]:
        line = lane_lines.render(kind, **fields)
        assert line is not None and line.startswith(kind + " "), fields
        assert lane_lines.ARROW in line
        assert "\n" not in line
        budget_line = line if kind in FIXED_TAIL else line.split(lane_lines.DASH)[0]
        assert len(budget_line) <= lane_lines.LINE_BUDGET, (kind, len(budget_line))


@pytest.mark.parametrize("kind", sorted(SILENT))
def test_every_silent_kind_renders_nothing_and_says_whether_it_journals(kind):
    fields, journals = SILENT[kind]
    assert lane_lines.render(kind, **fields) is None
    assert lane_lines.journals(kind) is journals


def test_an_unknown_kind_raises():
    with pytest.raises(KeyError):
        lane_lines.render("NOT_A_KIND", name="x")


def test_the_live_check_line_names_resume_worker():
    assert lane_lines.render("NODE_PARKED", task_key="b2-fake", node="design", name="b2-fake-node") == (
        'NODE_PARKED b2-fake design b2-fake-node → resume_worker("b2-fake-node"); if it waits on someone the '
        'engine does not watch: pipeline_advance(claude_sid=<your sid>, task_key="b2-fake", to="?", '
        'reason=<what it waits on>) holds it and ends this page')


def test_the_parked_step_names_the_rows_token_in_the_hold():
    line = lane_lines.render("NODE_PARKED", task_key="K", node="task", name="K-task-1", token_id="t1/a")
    assert 'to="?", token_id="t1/a", reason=<what it waits on>)' in line


def test_a_tail_follows_the_step_on_one_line():
    line = lane_lines.render("DONE", worker="w1", summary="shipped\nPR open")
    assert line.startswith('DONE w1 → ') and line.endswith(" — shipped PR open")


def test_nudged_wakes_only_as_an_unanswered_repeat():
    assert lane_lines.render("NUDGED", name="w1", why="30min", repeat=False) is None
    assert lane_lines.render("NUDGED", name="w1", why="60min", repeat=True).startswith(
        'NUDGED w1 (60min) → get_worker_tail("w1")')
    assert lane_lines.journals("NUDGED")


def test_a_dialog_on_a_pane_points_at_the_guide_skill_section():
    pointer = "per dockwright-orchestrator-guide § APPROVAL_PROMPT pages"
    assert pointer in lane_lines.render("NUDGED", name="w1", why="60min", repeat=True)
    assert lane_lines.render("APPROVAL_PROMPT", name="w1", excerpt="rm -rf x") == (
        'APPROVAL_PROMPT w1 → get_worker_pane("w1") and read the whole command; '
        'answer_permission_prompt("w1", approve|refuse, prompt_at=<its prompt_at>) ' + pointer
        + ", or escalate with it verbatim — rm -rf x")


def test_a_cleared_safeguard_pause_names_the_option_the_lane_chose():
    assert lane_lines.render("SAFEGUARD_PAUSE", name="w1", variant="continued", pane="%7", key="2",
                             window=30) == (
        'SAFEGUARD_PAUSE w1 → get_worker_tail("w1") in a few minutes to confirm it resumed'
        ' — a safeguard flag paused it; the lane chose "2. Edit prompt and retry" and typed continue')


def test_a_cleared_safeguard_pause_on_a_child_names_the_parent_and_get_child_tail():
    assert lane_lines.render("SAFEGUARD_PAUSE", name="c1", variant="continued", pane="%7", key="2",
                             window=30, parent="alpha") == (
        'SAFEGUARD_PAUSE c1 (child of alpha) → get_child_tail("c1") in a few minutes to confirm it resumed'
        ' — a safeguard flag paused it; the lane chose "2. Edit prompt and retry" and typed continue')


@pytest.mark.parametrize("variant", ["not-typed", "again", "unsettled", "unsent"])
def test_a_safeguard_pause_the_lane_left_sends_the_engineer_to_the_pane_and_forbids_a_relay(variant):
    line = lane_lines.render("SAFEGUARD_PAUSE", name="w1", variant=variant, pane="%7", key="2", window=30)
    step = line.split(lane_lines.DASH)[0]
    assert "tell the engineer" in step and "pane %7" in step
    assert "never relay into that pane" in step


def test_an_unsettled_safeguard_pause_names_the_key_for_a_dialog_that_still_shows():
    line = lane_lines.render("SAFEGUARD_PAUSE", name="w1", variant="unsettled", pane="%7", key="2", window=30)
    assert "if the dialog still shows, press 2 (Edit prompt and retry), never Enter" in line.split(lane_lines.DASH)[0]


def test_context_step_depends_on_the_pane_and_the_threshold():
    worker = dict(name="w1", used=140, window="200k", pct=70)
    assert lane_lines.render("CONTEXT", threshold=70, own=False, **worker) is None
    assert "kill_worker(\"w1\")" in lane_lines.render("CONTEXT", threshold=80, own=False, **worker)
    assert lane_lines.render("CONTEXT", threshold=90, own=False, **worker).endswith("→ tell the engineer")
    assert "/manager-recycle" in lane_lines.render("CONTEXT", threshold=70, own=True, **worker)


def test_reconcile_error_step_depends_on_the_key():
    assert lane_lines.render("RECONCILE_ERROR", task_key="*", error="OSError: x") == (
        "RECONCILE_ERROR * → dockwright lanes — OSError: x")
    assert lane_lines.render("RECONCILE_ERROR", task_key="K", error="E") == (
        'RECONCILE_ERROR K → pipeline_status("K") — E')


def test_poll_failed_step_says_whether_the_failed_items_are_retried():
    assert "retried last" in lane_lines.render("EVENT_POLL_FAILED", source="tracker", error="e")
    line = lane_lines.render("EVENT_POLL_FAILED", source="chat", error="e", skipped=True)
    assert "not retried" in line and "credential" not in line

def test_switched_head_is_what_the_limit_rollup_quotes():
    assert lane_lines.head("SWITCHED", account="a", new="b", subject="worker w1 limited") == (
        "account a→b (worker w1 limited)")


def test_a_missing_field_renders_a_question_mark_instead_of_raising():
    assert lane_lines.render("STALE_PROCESSING", name="w1") == (
        'STALE_PROCESSING w1 (?min) → get_worker_tail("w1"), surface it to the engineer and ask: '
        "kill_worker or follow up")


def test_the_done_of_a_node_whose_advance_committed_is_silent_and_journals():
    assert lane_lines.render("DONE", worker="w1", summary="s", advanced=True) is None
    assert lane_lines.render("DONE", worker="w1", summary="s", advanced=False).startswith("DONE w1 → ")
    assert lane_lines.journals("DONE")


def test_the_done_that_repeats_a_park_is_silent_and_journals():
    assert lane_lines.render("DONE", worker="w1", summary="s", repeat=True) is None
    assert lane_lines.render("DONE", worker="w1", summary="s", repeat=False).startswith("DONE w1 → ")


def test_a_journaled_kind_is_labelled_by_its_kind_and_head():
    assert lane_lines.label("DONE", {"worker": "K-design", "advanced": True}) == "DONE K-design"
    assert lane_lines.label("HANDED_OFF_CLOSED", {"task_key": "K", "name": "w", "reason": "grace"}) == "HANDED_OFF_CLOSED K w (grace)"
    assert lane_lines.label("NOT_A_KIND", {"x": 1}) == "NOT_A_KIND"
    assert lane_lines.label("DONE", "not a dict") == "DONE"
    assert lane_lines.label({"x": 1}, {"worker": "w"}) == "{'x': 1}"
    assert lane_lines.label(["DONE"], {"worker": "w"}) == "['DONE']"


def test_a_chat_archive_failure_names_the_kind_the_failure_and_the_status_command():
    assert lane_lines.render("CHAT_ARCHIVE_FAILED", failure_kind="auth", failure="AuthError") == (
        "CHAT_ARCHIVE_FAILED auth → AuthError; see dockwright chat-archive status")
    assert lane_lines.render("CHAT_ARCHIVE_FAILED", failure_kind="http", failure="HTTPError 503") == (
        "CHAT_ARCHIVE_FAILED http → HTTPError 503; see dockwright chat-archive status")


def test_a_status_poll_failure_names_the_issue_and_the_statuses():
    line = lane_lines.render("TRIGGER_POLL_FAILED", task_key="K", url="ABC-1", trigger="status",
                             status="Verified, category:done", to="release")
    assert line.startswith("TRIGGER_POLL_FAILED K ABC-1")
    assert "release" in line and "Verified, category:done" in line
