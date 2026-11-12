import time
import urllib.error
import urllib.parse

import pytest

from dockwright import events
from dockwright.event_sources import SelectorFailed, tracker

NOW = 1792700000.0
ENV = {"TRACKER_URL": "https://j", "TRACKER_EMAIL": "me@x", "TRACKER_API_TOKEN": "tok"}
RULE = {"source": "tracker", "kind": "sprint-assigned", "action": "start", "schema": "ticket", "domain": "general",
        "max_open": 2, "types": ["Story", "Bug"], "skip_status": ["Blocked"], "enabled": True, "errors": []}


def _adf(text):
    return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def _comment(cid, text="hi", author="Name", account="acc-1", created="2026-10-22T10:00:00.000+0000"):
    return {"id": str(cid), "body": _adf(text), "created": created,
            "author": {"displayName": author, "accountId": account}}


def _sub(issue="ABC-1", after="10", kinds=None, skip_own=None, sub_id="s1"):
    return events.Sub(sub_id, "K", "design", "tracker", {"issue": issue}, kinds, after, None, None, skip_own, NOW, None)


class FakeTracker:
    def __init__(self, comments=None, status=None, search=None, fail=None, clock=None, spend=0.0, children=None,
                 children_fail=None, missing=None):
        self.comments = comments or {}
        self.missing = set(missing or ())
        self.status = status or {}
        self.search = search or []
        self.fail = fail
        self.calls = []
        self.clock = clock
        self.spend = spend
        self.children = children or []
        self.children_fail = children_fail

    def __call__(self, method, url, env, timeout=None):
        self.calls.append((url, timeout))
        if self.clock is not None:
            self.clock["t"] += self.spend
        if self.fail is not None:
            raise self.fail
        parsed = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        parts = parsed.path.split("/")
        if parts[-1] == "comment":
            allc = self.comments.get(parts[-2], [])
            start = int(q.get("startAt", 0))
            return {"comments": allc[start:start + 100], "total": len(allc), "startAt": start}
        if parts[-1] == "jql":
            pages = self.search
            if q["jql"].startswith("parent in ("):
                if self.children_fail is not None:
                    raise self.children_fail
                pages = self.children
            page = int(q.get("nextPageToken", "p0")[1:])
            body = dict(pages[page])
            return body
        key = parts[-1]
        if key in self.missing:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        name, cat = self.status[key]
        return {"fields": {"status": {"name": name, "statusCategory": {"key": cat}}}}


def test_comments_after_the_lowest_after_of_the_issues_subs_become_comment_events():
    fake = FakeTracker(comments={"ABC-1": [_comment(9), _comment(11), _comment(12, "a" * 300), _comment(13)]},
                    status={"ABC-1": ["To Do", "new"]})
    evs, cursor = tracker.poll([_sub(after="10"), _sub(after="12", sub_id="s2", kinds=["comment"])], {}, {}, ENV, NOW,
                            call=fake)
    assert [e.id for e in evs] == ["tracker:ABC-1:comment:11", "tracker:ABC-1:comment:12", "tracker:ABC-1:comment:13",
                                   "tracker:ABC-1:status:new:1792700000"]
    e = evs[1]
    assert e.source == "tracker" and e.kind == "comment" and e.match == {"issue": "ABC-1"} and e.cursor_key == "12"
    assert e.summary == "ABC-1 #12 by Name: " + "a" * 200
    assert e.payload == {"author_id": "acc-1", "author": "Name", "created": "2026-10-22T10:00:00.000+0000",
                         "url": "https://j/browse/ABC-1?focusedCommentId=12"}
    assert e.ts == 1792663200.0
    assert cursor["comments"] == {"ABC-1": "13"} and cursor["status"] == {"ABC-1": ["To Do", "new"]}
    assert cursor["status_seen"] == {"ABC-1": NOW} and cursor["budget_spent"] == [] and cursor["failed"] == []
    assert len([u for u, _ in fake.calls if u.endswith("/comment?startAt=0&maxResults=100&orderBy=created")]) == 1


def test_a_comment_by_skip_own_is_still_emitted_and_dropped_at_matching():
    fake = FakeTracker(comments={"ABC-1": [_comment(11, account="me")]}, status={"ABC-1": ["To Do", "new"]})
    sub = _sub(after="10", skip_own="me", kinds=["comment"])
    evs, _ = tracker.poll([sub], {}, {}, ENV, NOW, call=fake)
    assert [e.payload["author_id"] for e in evs] == ["me"]
    assert events.matches(sub, evs[0]) is False
    assert events.matches(_sub(after="10", sub_id="s2"), evs[0]) is True


def test_pages_are_read_to_the_total():
    fake = FakeTracker(comments={"ABC-1": [_comment(i) for i in range(100, 250)]}, status={"ABC-1": ["To Do", "new"]})
    evs, cursor = tracker.poll([_sub(after="99", kinds=["comment"])], {}, {}, ENV, NOW, call=fake)
    assert len(evs) == 150 and cursor["comments"]["ABC-1"] == "249"
    assert [u.split("startAt=")[1].split("&")[0] for u, _ in fake.calls] == ["0", "100"]


def test_the_first_sight_of_an_issue_emits_its_status_once():
    fake = FakeTracker(status={"ABC-1": ["Done", "done"]})
    evs, cursor = tracker.poll([_sub()], {}, {}, ENV, NOW, call=fake)
    assert [(e.kind, e.payload) for e in evs] == [("status", {"name": "Done", "category": "done"})]
    assert cursor["status"] == {"ABC-1": ["Done", "done"]} and cursor["status_seen"] == {"ABC-1": NOW}
    evs, cursor = tracker.poll([_sub()], {}, cursor, ENV, NOW + 300, call=fake)
    assert evs == []
    fake2 = FakeTracker(status={"ABC-1": ["In Progress", "indeterminate"]})
    tracker.poll([_sub(kinds=["comment"])], {}, {}, ENV, NOW, call=fake2)
    assert all("/comment" in u for u, _ in fake2.calls)


def test_a_subscription_newer_than_the_last_status_event_gets_the_current_status_on_its_first_poll():
    fake = FakeTracker(status={"ABC-1": ["Done", "done"]})
    cursor = {"status": {"ABC-1": ["Done", "done"]}, "status_seen": {"ABC-1": NOW - 20 * 86400}}
    old = events.Sub("s1", "K", "design", "tracker", {"issue": "ABC-1"}, None, "10", None, None, None, NOW - 25 * 86400,
                     None)
    evs, cursor = tracker.poll([old], {}, cursor, ENV, NOW, call=fake)
    assert evs == []
    fresh = events.Sub("s9", "K", "post-merge", "tracker", {"issue": "ABC-1"}, ["comment", "status"], "10", None,
                       ["author-wait", "close"], None, NOW + 100, None)
    evs, cursor = tracker.poll([old, fresh], {}, cursor, ENV, NOW + 300, call=fake)
    assert [(e.kind, e.payload["category"], e.ts) for e in evs] == [("status", "done", NOW + 300)]
    assert cursor["status_seen"] == {"ABC-1": NOW + 300}
    evs, cursor = tracker.poll([old, fresh], {}, cursor, ENV, NOW + 600, call=fake)
    assert evs == []


def test_a_status_change_becomes_a_status_event_once():
    cursor = {"status": {"ABC-1": ["In Progress", "indeterminate"]}}
    fake = FakeTracker(status={"ABC-1": ["Done", "done"]})
    evs, cursor = tracker.poll([_sub()], {}, cursor, ENV, NOW, call=fake)
    assert [(e.kind, e.id, e.payload, e.summary) for e in evs] == [
        ("status", "tracker:ABC-1:status:done:1792700000", {"name": "Done", "category": "done"}, "ABC-1 status Done (done)")]
    assert evs[0].match == {"issue": "ABC-1"} and evs[0].cursor_key == "" and evs[0].ts == NOW
    assert cursor["status"]["ABC-1"] == ["Done", "done"]
    evs, cursor = tracker.poll([_sub()], {}, cursor, ENV, NOW + 1, call=FakeTracker(status={"ABC-1": ["Done", "done"]}))
    assert evs == []


def _search_page(keys, token=None):
    page = {"issues": [{"key": k, "fields": {"summary": f"sum {k}", "issuetype": {"name": "Story"},
                                             "status": {"name": "To Do"}}} for k in keys]}
    if token:
        page["nextPageToken"] = token
    return page


def _page(issues, token=None):
    page = {"issues": issues}
    if token:
        page["nextPageToken"] = token
    return page


def _issue(key, itype="Story", status="To Do", parent=None, ptype="Story", pstatus="To Do", subtasks=(), account="me"):
    fields = {"summary": f"sum {key}", "issuetype": {"name": itype, "subtask": itype == "Sub-task"},
              "status": {"name": status}, "subtasks": [{"key": s} for s in subtasks],
              "assignee": {"accountId": account}}
    if parent:
        fields["parent"] = {"key": parent, "fields": {"summary": f"sum {parent}", "issuetype": {"name": ptype},
                                                       "status": {"name": pstatus}}}
    return {"key": key, "fields": fields}


def _child(key, parent, account="me", category="new"):
    return {"key": key, "fields": {"parent": {"key": parent},
                                   "status": {"name": "S", "statusCategory": {"key": category}},
                                   "assignee": {"accountId": account} if account else None}}


def _subtask(key, parent="ABC-1", **kw):
    return _issue(key, "Sub-task", parent=parent, **kw)


def _poll(search, children=(), keys=(), **kw):
    fake = FakeTracker(search=[_page(search)], children=[_page(list(children))], **kw)
    evs, cursor = tracker.poll([], {"sprint": RULE}, {"sprint": {"keys": list(keys)}}, ENV, NOW, call=fake)
    return fake, evs, cursor


def test_the_sprint_rule_seeds_its_cursor_on_the_first_pass_and_emits_new_keys_after():
    fake = FakeTracker(search=[_search_page(["ABC-1", "ABC-2"])])
    evs, cursor = tracker.poll([], {"sprint": RULE}, {}, ENV, NOW, call=fake)
    assert evs == [] and cursor["sprint"]["keys"] == ["ABC-1", "ABC-2"]
    fake = FakeTracker(search=[_search_page(["ABC-1", "ABC-2", "ABC-3"])])
    evs, cursor = tracker.poll([], {"sprint": RULE}, cursor, ENV, NOW + 1200, call=fake)
    assert [(e.id, e.kind, e.match, e.summary, e.payload) for e in evs] == [
        ("tracker:ABC-3:sprint-assigned", "sprint-assigned", {"issue": "ABC-3"}, "ABC-3 sum ABC-3 (Story, To Do)",
         {"url": "https://j/browse/ABC-3", "type": "Story", "status": "To Do"})]
    assert cursor["sprint"]["keys"] == ["ABC-1", "ABC-2", "ABC-3"] and evs[0].ts == NOW + 1200


def _searches(fake):
    return [url for url, _ in fake.calls if "/search/jql?" in url]


def test_the_sprint_search_runs_every_sprint_every_sec_and_the_issues_every_poll():
    def fake():
        return FakeTracker(comments={"ABC-9": [_comment(1)]}, status={"ABC-9": ["To Do", "new"]},
                           search=[_search_page(["ABC-1"])])

    first = fake()
    _, cursor = tracker.poll([_sub("ABC-9", after="0")], {"sprint": RULE}, {}, ENV, NOW, call=first)
    assert len(_searches(first)) == 1 and cursor["sprint_last"] == NOW
    sprint = cursor["sprint"]
    second = fake()
    _, cursor = tracker.poll([_sub("ABC-9", after="0")], {"sprint": RULE}, cursor, ENV, NOW + 300, call=second)
    assert _searches(second) == []
    assert any(url.split("?")[0].endswith("/ABC-9/comment") for url, _ in second.calls)
    assert cursor["sprint"] == sprint and cursor["sprint_last"] == NOW
    third = fake()
    _, cursor = tracker.poll([_sub("ABC-9", after="0")], {"sprint": RULE}, cursor, ENV, NOW + 1200, call=third)
    assert len(_searches(third)) == 1 and cursor["sprint_last"] == NOW + 1200


def test_a_spent_or_failed_sprint_search_runs_on_the_next_poll():
    spent = FakeTracker(search=[_search_page(["ABC-1"])])
    cursor = {"sprint": {"keys": []}, "sprint_last": NOW, "budget_spent": ["rule:sprint"]}
    tracker.poll([], {"sprint": RULE}, cursor, ENV, NOW + 300, call=spent)
    assert len(_searches(spent)) == 1
    down = FakeTracker(fail=urllib.error.HTTPError("https://j", 500, "Server Error", {}, None))
    _, cursor = tracker.poll([], {"sprint": RULE}, {}, ENV, NOW, call=down)
    assert "sprint_last" not in cursor and cursor["failed"][0][0] == "rule:sprint"
    again = FakeTracker(search=[_search_page(["ABC-1"])])
    _, cursor = tracker.poll([], {"sprint": RULE}, cursor, ENV, NOW + 300, call=again)
    assert len(_searches(again)) == 1 and cursor["sprint_last"] == NOW + 300


def test_the_sprint_search_follows_next_page_token_and_stops_without_one():
    fake = FakeTracker(search=[_search_page(["ABC-1"], "p1"), _search_page(["ABC-2"])])
    evs, cursor = tracker.poll([], {"sprint": RULE}, {}, ENV, NOW, call=fake)
    assert cursor["sprint"]["keys"] == ["ABC-1", "ABC-2"] and len(fake.calls) == 2
    assert "nextPageToken=p1" in fake.calls[1][0] and "nextPageToken" not in fake.calls[0][0]


def test_the_sprint_search_stops_at_twenty_pages():
    fake = FakeTracker(search=[_search_page([f"ABC-{i}"], f"p{i + 1}") for i in range(30)])
    evs, cursor = tracker.poll([], {"sprint": RULE}, {}, ENV, NOW, call=fake)
    assert evs == [] and len(fake.calls) == 20 and "sprint" not in cursor
    assert cursor["failed"] == [["rule:sprint", "tracker rule:sprint: sprint search did not end after 20 pages"]]


def test_a_disabled_rule_sends_no_search():
    fake = FakeTracker(search=[_search_page(["ABC-1"])])
    evs, cursor = tracker.poll([], {"sprint": {**RULE, "enabled": False}}, {}, ENV, NOW, call=fake)
    assert evs == [] and fake.calls == [] and "sprint" not in cursor
    evs, cursor = tracker.poll([], {"x": {**RULE, "errors": ["kind is required"]}}, {}, ENV, NOW, call=fake)
    assert fake.calls == []


def test_the_jql_names_the_types_and_the_skipped_statuses():
    fake = FakeTracker(search=[_search_page([])])
    tracker.poll([], {"sprint": RULE}, {}, ENV, NOW, call=fake)
    url = fake.calls[0][0]
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert q["jql"] == ('sprint in openSprints() AND assignee = currentUser() AND (issuetype in ("Story", "Bug") '
                        'OR issuetype in subTaskIssueTypes()) AND status not in ("Blocked")')
    assert q["fields"] == "summary,issuetype,status,parent,subtasks,assignee" and q["maxResults"] == "100"
    assert url.startswith("https://j/rest/api/3/search/jql?")
    fake = FakeTracker(search=[_search_page([])])
    tracker.poll([], {"sprint": {**RULE, "types": [], "skip_status": []}}, {}, ENV, NOW, call=fake)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(fake.calls[0][0]).query))
    assert q["jql"] == "sprint in openSprints() AND assignee = currentUser()"


def test_a_sub_task_of_mine_starts_alone_while_a_sibling_is_someone_elses():
    fake, evs, cursor = _poll(
        [_subtask("ABC-2"), _subtask("ABC-3"), _issue("ABC-1", subtasks=["ABC-2", "ABC-3", "ABC-4"])],
        [_child("ABC-2", "ABC-1"), _child("ABC-3", "ABC-1"), _child("ABC-4", "ABC-1", account="other")])
    assert [(e.id, e.summary, e.payload["parent"]) for e in evs] == [
        ("tracker:ABC-2:sprint-assigned", "ABC-2 sum ABC-2 (Sub-task, To Do, sub-task of ABC-1)", "ABC-1"),
        ("tracker:ABC-3:sprint-assigned", "ABC-3 sum ABC-3 (Sub-task, To Do, sub-task of ABC-1)", "ABC-1")]
    assert cursor["sprint"]["keys"] == ["ABC-2", "ABC-3"]
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(fake.calls[1][0]).query))
    assert q["jql"] == "parent in (ABC-1) AND issuetype in subTaskIssueTypes()"
    assert q["fields"] == "parent,assignee,status"


def test_a_parent_whose_sub_tasks_are_all_mine_starts_once():
    _, evs, cursor = _poll([_subtask("ABC-2"), _subtask("ABC-3"), _issue("ABC-1", subtasks=["ABC-2", "ABC-3"])],
                           [_child("ABC-2", "ABC-1"), _child("ABC-3", "ABC-1")])
    assert [(e.id, e.match, e.summary, e.payload) for e in evs] == [
        ("tracker:ABC-1:sprint-assigned", {"issue": "ABC-1"}, "ABC-1 sum ABC-1 (Story, To Do, sub-tasks ABC-2, ABC-3)",
         {"url": "https://j/browse/ABC-1", "type": "Story", "status": "To Do", "subtasks": ["ABC-2", "ABC-3"]})]
    assert cursor["sprint"]["keys"] == ["ABC-1", "ABC-2", "ABC-3"]


def test_start_blocker_reads_each_key_live():
    fake = FakeTracker(status={"ABC-1": ("To Do", "new"), "ABC-2": ("In Development", "indeterminate"),
                               "ABC-3": ("Blocked", "indeterminate"), "ABC-4": ("Blocked", "indeterminate"),
                               "ABC-5": ("X", "")}, missing={"ABC-9"})

    def blocker(issue, subtasks=()):
        return tracker.start_blocker(RULE, issue, list(subtasks), ENV, call=fake)

    assert blocker("ABC-1") == "" and fake.calls[0][0] == "https://j/rest/api/3/issue/ABC-1?fields=status"
    assert blocker("ABC-1", ["ABC-3"]) == "" and blocker("ABC-1", ["ABC-9"]) == ""
    assert blocker("ABC-2") == "ABC-2 is past To Do" and blocker("ABC-5") == "ABC-5 is past To Do"
    assert blocker("ABC-1", ["ABC-3", "ABC-2"]) == "ABC-2 is past To Do"
    assert blocker("ABC-4") == "ABC-4 is in Blocked" and blocker("ABC-9") == "ABC-9 was not found"
    with pytest.raises(SelectorFailed):
        tracker.start_blocker(RULE, "ABC-1", [], ENV, call=FakeTracker(fail=urllib.error.URLError("down")))
    assert tracker.sprint_keys(None) == []


def test_the_sprint_cursor_shows_every_found_issue_a_parent_with_someone_elses_sub_task_included():
    fake = FakeTracker(search=[_page([_issue("ABC-1", subtasks=["ABC-2", "ABC-3"]), _subtask("ABC-2")])],
                       children=[_page([_child("ABC-2", "ABC-1"), _child("ABC-3", "ABC-1", account="other")])])
    _, seeded = tracker.poll([], {"sprint": RULE}, {}, ENV, NOW, call=fake)
    assert seeded["sprint"]["keys"] == ["ABC-2"] and tracker.sprint_keys(seeded) == ["ABC-1", "ABC-2"]
    _, cursor = tracker.poll([], {"sprint": RULE}, seeded, ENV, NOW + 1200, call=fake)
    assert tracker.sprint_keys(cursor) == ["ABC-1", "ABC-2"]


def test_a_parent_of_someone_elses_starts_my_sub_tasks_alone():
    _, evs, cursor = _poll([_subtask("ABC-2"), _subtask("ABC-3")], [_child("ABC-2", "ABC-1"), _child("ABC-3", "ABC-1")])
    assert [(e.id, e.payload["parent"]) for e in evs] == [("tracker:ABC-2:sprint-assigned", "ABC-1"),
                                                          ("tracker:ABC-3:sprint-assigned", "ABC-1")]
    assert cursor["sprint"]["keys"] == ["ABC-2", "ABC-3"]


def test_a_blocked_sub_task_of_mine_counts_as_mine():
    _, evs, cursor = _poll([_subtask("ABC-2"), _issue("ABC-1", subtasks=["ABC-2", "ABC-3"])],
                           [_child("ABC-2", "ABC-1"), _child("ABC-3", "ABC-1")])
    assert [(e.id, e.payload["subtasks"]) for e in evs] == [("tracker:ABC-1:sprint-assigned", ["ABC-2", "ABC-3"])]
    assert cursor["sprint"]["keys"] == ["ABC-1", "ABC-2", "ABC-3"]


def test_an_unassigned_sub_task_blocks_the_parent_only_while_open():
    search = [_subtask("ABC-2"), _issue("ABC-1", subtasks=["ABC-2", "ABC-3"])]
    _, evs, _ = _poll(search, [_child("ABC-2", "ABC-1"), _child("ABC-3", "ABC-1", account=None, category="done")])
    assert [(e.id, e.payload["subtasks"]) for e in evs] == [("tracker:ABC-1:sprint-assigned", ["ABC-2"])]
    _, evs, _ = _poll(search, [_child("ABC-2", "ABC-1"), _child("ABC-3", "ABC-1", account=None, category="new")])
    assert [(e.id, e.payload["parent"]) for e in evs] == [("tracker:ABC-2:sprint-assigned", "ABC-1")]


def test_a_parent_of_mine_with_no_sub_task_of_mine_starts_nothing():
    _, evs, cursor = _poll([_issue("ABC-1", subtasks=["ABC-2"])], [_child("ABC-2", "ABC-1", account="other")])
    assert evs == [] and cursor["sprint"]["keys"] == []


def test_the_rule_types_and_skip_status_apply_to_a_sub_tasks_parent():
    fake, evs, _ = _poll([_subtask("ABC-2", ptype="Enhancement")], [_child("ABC-2", "ABC-1")])
    assert evs == [] and len(fake.calls) == 1
    _, evs, cursor = _poll([_subtask("ABC-2", pstatus="Blocked")], [_child("ABC-2", "ABC-1")])
    assert evs == [] and cursor["sprint"]["keys"] == []


def test_a_sub_task_a_first_rule_rejects_starts_under_a_later_rule_that_takes_its_parent():
    fake = FakeTracker(search=[_page([_subtask("ABC-2")])], children=[_page([_child("ABC-2", "ABC-1")])])
    rules = {"b": {**RULE, "types": ["Bug"]}, "a": RULE}
    evs, cursor = tracker.poll([], rules, {"sprint": {"keys": []}}, ENV, NOW, call=fake)
    assert [(e.id, e.payload["parent"]) for e in evs] == [("tracker:ABC-2:sprint-assigned", "ABC-1")]
    assert cursor["sprint"]["keys"] == ["ABC-2"]


def test_a_story_under_an_epic_starts_as_today_and_sends_no_child_search():
    fake, evs, _ = _poll([_issue("ABC-1", parent="EP-1", ptype="Epic")])
    assert [(e.summary, e.payload) for e in evs] == [
        ("ABC-1 sum ABC-1 (Story, To Do)", {"url": "https://j/browse/ABC-1", "type": "Story", "status": "To Do"})]
    assert len(fake.calls) == 1


def test_a_failed_child_search_emits_nothing_and_keeps_the_sprint_cursor():
    _, evs, new = _poll([_subtask("ABC-2")], keys=["ABC-9"], children_fail=urllib.error.URLError("down"))
    assert evs == [] and new["sprint"] == {"keys": ["ABC-9"]}
    assert new["failed"] == [["rule:sprint", "tracker rule:sprint: <urlopen error down>"]]


def _carry(cursor, search, children):
    fake = FakeTracker(search=[_page(search)], children=[_page(children)])
    last = cursor.get("sprint_last")
    return tracker.poll([], {"sprint": RULE}, cursor, ENV, NOW if last is None else last + 1200, call=fake)


def test_started_units_never_start_twice_as_ownership_moves():
    evs, cursor = _carry({"sprint": {"keys": []}},
                         [_subtask("ABC-2"), _subtask("ABC-3"), _issue("ABC-1", subtasks=["ABC-2", "ABC-3"])],
                         [_child("ABC-2", "ABC-1"), _child("ABC-3", "ABC-1")])
    assert [e.id for e in evs] == ["tracker:ABC-1:sprint-assigned"]
    evs, cursor = _carry(cursor, [_subtask("ABC-2"), _issue("ABC-1", subtasks=["ABC-2", "ABC-3"])],
                         [_child("ABC-2", "ABC-1"), _child("ABC-3", "ABC-1", account="other")])
    assert evs == [] and cursor["sprint"]["keys"] == ["ABC-1", "ABC-2", "ABC-3"]
    evs, cursor = _carry(cursor, [_subtask("ABC-2"), _subtask("ABC-3"), _subtask("ABC-4"),
                                  _issue("ABC-1", subtasks=["ABC-2", "ABC-3", "ABC-4"])],
                         [_child("ABC-2", "ABC-1"), _child("ABC-3", "ABC-1"), _child("ABC-4", "ABC-1")])
    assert evs == [] and cursor["sprint"]["keys"] == ["ABC-1", "ABC-2", "ABC-3"]


def test_a_parent_that_turns_all_mine_after_a_sub_task_started_starts_only_the_new_one():
    evs, cursor = _carry({"sprint": {"keys": []}}, [_subtask("ABC-2"), _issue("ABC-1", subtasks=["ABC-2", "ABC-3"])],
                         [_child("ABC-2", "ABC-1"), _child("ABC-3", "ABC-1", account="other")])
    assert [e.id for e in evs] == ["tracker:ABC-2:sprint-assigned"]
    evs, cursor = _carry(cursor, [_subtask("ABC-2"), _subtask("ABC-3"), _issue("ABC-1", subtasks=["ABC-2", "ABC-3"])],
                         [_child("ABC-2", "ABC-1"), _child("ABC-3", "ABC-1")])
    assert [(e.id, e.summary) for e in evs] == [
        ("tracker:ABC-3:sprint-assigned", "ABC-3 sum ABC-3 (Sub-task, To Do, sub-task of ABC-1)")]
    assert cursor["sprint"]["keys"] == ["ABC-2", "ABC-3"]
    evs, cursor = _carry(cursor, [_subtask("ABC-2"), _subtask("ABC-3"), _subtask("ABC-4"),
                                  _issue("ABC-1", subtasks=["ABC-2", "ABC-3", "ABC-4"])],
                         [_child("ABC-2", "ABC-1"), _child("ABC-3", "ABC-1"), _child("ABC-4", "ABC-1")])
    assert [(e.id, e.payload["parent"]) for e in evs] == [("tracker:ABC-4:sprint-assigned", "ABC-1")]


def test_a_started_parent_keeps_its_key_while_its_status_is_skipped():
    evs, cursor = _carry({"sprint": {"keys": []}}, [_subtask("ABC-2"), _issue("ABC-1", subtasks=["ABC-2"])],
                         [_child("ABC-2", "ABC-1")])
    assert [e.id for e in evs] == ["tracker:ABC-1:sprint-assigned"]
    evs, cursor = _carry(cursor, [_subtask("ABC-2", pstatus="Blocked")], [_child("ABC-2", "ABC-1")])
    assert evs == [] and cursor["sprint"]["keys"] == ["ABC-1", "ABC-2"]
    evs, cursor = _carry(cursor, [_subtask("ABC-2"), _subtask("ABC-3"), _issue("ABC-1", subtasks=["ABC-2", "ABC-3"])],
                         [_child("ABC-2", "ABC-1"), _child("ABC-3", "ABC-1")])
    assert evs == []


def test_a_parent_started_without_sub_tasks_covers_sub_tasks_added_later():
    evs, cursor = _carry({"sprint": {"keys": []}}, [_issue("ABC-1")], [])
    assert [e.id for e in evs] == ["tracker:ABC-1:sprint-assigned"]
    evs, cursor = _carry(cursor, [_issue("ABC-1", subtasks=["ABC-2", "ABC-3"]), _subtask("ABC-2")],
                         [_child("ABC-2", "ABC-1"), _child("ABC-3", "ABC-1", account="other")])
    assert evs == []


def test_a_failed_call_records_the_selector_and_leaves_its_cursor():
    cursor = {"comments": {"ABC-1": "5"}}
    fake = FakeTracker(fail=urllib.error.URLError("down"))
    evs, new = tracker.poll([_sub()], {}, cursor, ENV, NOW, call=fake)
    assert evs == [] and new["comments"] == {"ABC-1": "5"} and new["status"] == {}
    assert new["failed"] == [["ABC-1", "tracker ABC-1: <urlopen error down>"]]
    assert cursor == {"comments": {"ABC-1": "5"}}
    _, new = tracker.poll([_sub()], {}, cursor, ENV, NOW, call=FakeTracker(fail=ValueError("bad json")))
    assert new["failed"][0][0] == "ABC-1" and "bad json" in new["failed"][0][1]


def test_a_malformed_body_for_one_issue_fails_that_selector_alone():
    def call(method, url, env, timeout=None):
        if "/ABC-0" in url:
            return {"fields": "not an object"} if url.endswith("fields=status") else []
        return {"comments": [_comment(77)], "total": 1}

    subs = [_sub("ABC-0", after="0", kinds=["comment"]), _sub("ABC-1", after="0", kinds=["comment"], sub_id="s2")]
    evs, cursor = tracker.poll(subs, {}, {}, ENV, NOW, call=call)
    assert [e.id for e in evs] == ["tracker:ABC-1:comment:77"] and [f[0] for f in cursor["failed"]] == ["ABC-0"]
    evs, cursor = tracker.poll([_sub("ABC-0", after="0", kinds=["status"])], {}, {}, ENV, NOW, call=call)
    assert evs == [] and [f[0] for f in cursor["failed"]] == ["ABC-0"]


def test_one_unreadable_issue_fails_alone_and_polls_last_next_pass():
    def call(method, url, env, timeout=None):
        if "/ABC-0/" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return {"comments": [_comment(77)], "total": 1}

    subs = [_sub("ABC-0", after="0", kinds=["comment"]), _sub("ABC-1", after="0", kinds=["comment"], sub_id="s2")]
    evs, cursor = tracker.poll(subs, {}, {}, ENV, NOW, call=call)
    assert [e.id for e in evs] == ["tracker:ABC-1:comment:77"] and cursor["comments"] == {"ABC-1": "77"}
    assert [f[0] for f in cursor["failed"]] == ["ABC-0"] and "404" in cursor["failed"][0][1]
    seen = []

    def call2(method, url, env, timeout=None):
        seen.append(url.split("/issue/")[1].split("/")[0])
        return {"comments": [], "total": 0}

    evs, cursor = tracker.poll(subs, {}, cursor, ENV, NOW + 300, call=call2)
    assert seen == ["ABC-1", "ABC-0"] and cursor["failed"] == []


def test_a_call_that_runs_past_the_budget_is_not_a_failure_but_the_next_pass_starts_there(monkeypatch):
    clock = {"t": NOW}
    monkeypatch.setattr(time, "time", lambda: clock["t"])

    def call(method, url, env, timeout=None):
        spend = 20.0
        if timeout is not None and timeout < spend:
            clock["t"] += timeout
            raise TimeoutError("timed out")
        clock["t"] += spend
        key = url.split("/issue/")[1].split("/")[0]
        return {"comments": [_comment(int(key.split("-")[1]) * 10 + 1)], "total": 1}

    subs = [_sub(f"ABC-{i}", after="0", kinds=["comment"], sub_id=f"s{i}") for i in (1, 2, 3)]
    evs, cursor = tracker.poll(subs, {}, {}, ENV, clock["t"], call=call, until=clock["t"] + 30)
    assert [e.id for e in evs] == ["tracker:ABC-1:comment:11"]
    assert cursor["budget_spent"] == ["ABC-2", "ABC-3"] and cursor["failed"] == []
    evs, cursor = tracker.poll(subs, {}, cursor, ENV, clock["t"], call=call, until=clock["t"] + 30)
    assert [e.id for e in evs] == ["tracker:ABC-2:comment:21"] and cursor["budget_spent"] == ["ABC-3", "ABC-1"]


def test_the_budget_keeps_the_finished_issues_and_queues_the_rest(monkeypatch):
    clock = {"t": NOW}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    fake = FakeTracker(comments={"ABC-1": [_comment(11)], "ABC-2": [_comment(21)]},
                    status={"ABC-1": ["To Do", "new"], "ABC-2": ["To Do", "new"]}, clock=clock, spend=40.0)
    subs = [_sub("ABC-1", after="10", kinds=["comment"]), _sub("ABC-2", after="20", kinds=["comment"], sub_id="s2")]
    evs, cursor = tracker.poll(subs, {}, {}, ENV, NOW, call=fake, until=NOW + 30)
    assert [e.id for e in evs] == ["tracker:ABC-1:comment:11"]
    assert cursor["comments"] == {"ABC-1": "11"} and cursor["budget_spent"] == ["ABC-2"]
    fake2 = FakeTracker(comments={"ABC-1": [_comment(11)], "ABC-2": [_comment(21)]}, clock=clock, spend=40.0)
    evs, cursor = tracker.poll(subs, {}, cursor, ENV, clock["t"], call=fake2, until=clock["t"] + 30)
    assert [e.id for e in evs] == ["tracker:ABC-2:comment:21"] and cursor["budget_spent"] == ["ABC-1"]
    assert "/ABC-2/" in fake2.calls[0][0]
    fake3 = FakeTracker(comments={"ABC-1": [_comment(11)], "ABC-2": [_comment(21)]}, clock=clock, spend=40.0)
    evs, cursor = tracker.poll(subs, {}, cursor, ENV, clock["t"], call=fake3, until=None)
    assert len(evs) == 2 and cursor["budget_spent"] == []


def test_each_call_gets_the_time_left_as_its_timeout(monkeypatch):
    clock = {"t": NOW}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    fake = FakeTracker(comments={"ABC-1": [_comment(11)]}, status={"ABC-1": ["To Do", "new"]}, clock=clock, spend=2.0)
    tracker.poll([_sub()], {}, {}, ENV, NOW, call=fake, until=NOW + 5)
    assert [t for _, t in fake.calls] == [5.0, 3.0]
    fake = FakeTracker(comments={"ABC-1": [_comment(11)]}, status={"ABC-1": ["To Do", "new"]})
    tracker.poll([_sub()], {}, {}, ENV, NOW, call=fake, until=None)
    assert [t for _, t in fake.calls] == [30, 30]


def test_the_default_call_does_basic_auth_against_tracker_url_with_the_timeout(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"comments": [], "total": 0, "startAt": 0}'

    def fake_urlopen(req, timeout=None):
        seen["url"], seen["timeout"] = req.full_url, timeout
        seen["auth"] = req.get_header("Authorization")
        return Resp()

    monkeypatch.setattr(tracker.urllib.request, "urlopen", fake_urlopen)
    evs, cursor = tracker.poll([_sub(kinds=["comment"])], {}, {}, ENV, NOW, until=time.time() + 7)
    assert evs == [] and seen["url"].startswith("https://j/rest/api/3/issue/ABC-1/comment?")
    assert seen["auth"] == "Basic bWVAeDp0b2s=" and 6.0 < seen["timeout"] <= 7.0
