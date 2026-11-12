import os
import time
from datetime import datetime

import pytest

from dockwright import config, events
from dockwright.event_sources import github
from tests.test_mcp_tools import fresh_orchestrator_dir  # noqa: F401


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


NOW = _ts("2026-10-22T12:00:00Z")
FRIDAY = _ts("2026-10-23T12:00:00Z")
MONDAY = _ts("2026-10-26T12:00:00Z")
SHA = "a" * 40
HEAD = "b" * 40
OLD = "c" * 40
D1 = "d" * 40
D2 = "e" * 40
X = "f" * 40
MERGED_AT = "2026-10-21T08:00:00Z"
WAITING_AT = "2026-10-22T09:00:00Z"
URL = "https://github.com/o/r/pull/{}"
RUN_URL = "https://gh/o/r/actions/runs/{}"
SOURCE = ('[events]\nenabled = true\n[events.sources.github]\nowners = ["o"]\nreview_after_hours = 8\n'
          'approval_after_hours = 1\nrepeat_hours = 8\nwork_hours = "09:00-17:00"\ntz = "Etc/UTC"\n'
          'merged_window_days = 14\nenvs = ["prod"]\ndeployed_envs = ["prod", "prod-fallback"]\n'
          'ignore_reviewers = ["bot1"]\n')
SOURCE_FALLBACK = SOURCE + 'fallback_envs = ["prod-fallback"]\nfallback_skipped_job = "deploy-prod"\n'
PROD_JOB = "service / deploy-prod"
GH = '[gh]\n[gh.logins]\no = "me"\n'


def _rule(kind, enabled=True):
    return {"source": "github", "kind": kind, "action": "page", "schema": None, "domain": "general", "max_open": 2,
            "types": [], "skip_status": [], "enabled": enabled, "errors": []}


RULES = {"review-ping": _rule("review-wait"), "deploy-ping": _rule("approval-wait")}
REVIEW_ONLY = {"review-ping": _rule("review-wait"), "deploy-ping": _rule("approval-wait", enabled=False)}
APPROVAL_ONLY = {"review-ping": _rule("review-wait", enabled=False), "deploy-ping": _rule("approval-wait")}


@pytest.fixture
def github_cfg(tmp_path, monkeypatch):
    cfg = tmp_path / "events.toml"
    cfg.write_text(SOURCE + GH)
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))

    def write(text):
        cfg.write_text(text)

    return write


def _open(number, created, repo="o/r", title="t"):
    return {"repository": {"nameWithOwner": repo}, "number": number, "url": URL.format(number), "title": title,
            "createdAt": created}


def _entry(number, created, repo="o/r", title="t"):
    return {"url": URL.format(number), "title": title, "createdAt": created, "repo": repo, "number": number}


def _pr(ready=None, draft=False, decision=None, reviews=(), state="OPEN", author="me"):
    return {"state": state, "isDraft": draft, "reviewDecision": decision, "author": {"login": author},
            "timelineItems": {"nodes": [{"createdAt": ready}] if ready else []},
            "reviews": {"nodes": list(reviews)}}


def _review(login, typename="User", state="COMMENTED", at="2026-10-22T11:00:00Z"):
    return {"author": {"__typename": typename, "login": login}, "state": state, "submittedAt": at}


def _merged_row(number, closed, repo="o/r"):
    return {"repository": {"nameWithOwner": repo}, "closedAt": closed, "number": number}


def _view(sha=SHA, base="main"):
    return {"mergeCommit": {"oid": sha}, "baseRefName": base}


def _run(rid, head=HEAD, created="2026-10-22T08:30:00Z"):
    return {"databaseId": rid, "url": RUN_URL.format(rid), "createdAt": created, "headSha": head, "workflowName": "W"}


def _pending(env="prod", can=False, reviewers=("team-x",)):
    return {"environment": {"name": env}, "current_user_can_approve": can,
            "reviewers": [{"type": "Team", "reviewer": {"slug": r}} for r in reviewers]}


def _deployment(env, sha, created, status):
    return {"environment": env, "commitOid": sha, "createdAt": created,
            "latestStatus": {"state": status} if status else None}


def _errors(n, env="prod", sha=OLD):
    return [_deployment(env, sha, "2026-10-20T10:00:00Z", "ERROR") for _ in range(n)]


def _pages(*lists):
    def page(after):
        i = int(after[1:]) if after else 0
        more = i + 1 < len(lists)
        return {"pageInfo": {"hasNextPage": more, "endCursor": f"c{i + 1}" if more else None}, "nodes": lists[i]}

    return page


WAITING = _deployment("prod", HEAD, WAITING_AT, "WAITING")


class FakeGh:
    def __init__(self, open=None, merged=None, prs=None, runs=None, views=None, pending=None, deployments=None,
                 compare=None, clock=None, spend=0.0, default_branch="main", jobs=None):
        self.open, self.merged, self.prs, self.runs = open, merged, prs or {}, runs
        self.jobs = jobs or {}
        self.views, self.pending = views or {URL.format(9): _view()}, pending or {}
        self.deployments = deployments or _pages([WAITING])
        self.compare = {f"{SHA}...{HEAD}": "ahead", **(compare or {})}
        self.calls = []
        self.clock, self.spend, self.default_branch = clock, spend, default_branch

    def __call__(self, owner, argv, timeout=None):
        self.calls.append((owner, list(argv), timeout))
        if self.clock is not None:
            self.clock["t"] += self.spend
        if argv[:2] == ["search", "prs"]:
            return self.merged if "--merged" in argv else self.open
        if argv[:2] == ["api", "graphql"]:
            query = argv[3]
            if "deployments(" in query:
                after = next((a.split("=", 1)[1] for a in argv if a.startswith("after=")), None)
                page = self.deployments(after)
                return None if page is None else {"data": {"repository": {"deployments": page}}}
            if "pullRequest(" in query:
                number = int(next(a.split("=", 1)[1] for a in argv if a.startswith("number=")))
                pr = self.prs.get(number)
                return None if pr is None else {"data": {"repository": {"pullRequest": pr}}}
        if argv[:2] == ["run", "list"]:
            return self.runs
        if argv[:2] == ["pr", "view"]:
            return self.views.get(argv[2])
        if argv[:2] == ["run", "view"]:
            return self.jobs.get(int(argv[2]))
        if argv[0] == "api":
            if argv[1].endswith("/pending_deployments"):
                return self.pending.get(int(argv[1].split("/")[-2]))
            if "/compare/" in argv[1]:
                return {"status": self.compare.get(argv[1].rsplit("/", 1)[1], "behind")}
            if argv[1] == "repos/o/r":
                return {"default_branch": self.default_branch}
        raise AssertionError(argv)

    def named(self, *head):
        return [a for _, a, _ in self.calls if a[:len(head)] == list(head)]

    def deployment_calls(self):
        return [a for _, a, _ in self.calls if a[:2] == ["api", "graphql"] and "deployments(" in a[3]]


def _poll(fake, rules=RULES, cursor=None, now=NOW, until=None):
    return github.poll([], rules, cursor or {}, {}, now, call=fake, until=until)


def test_an_overdue_pr_pages_once_with_ping_1_and_the_exact_search_argv(github_cfg):
    ready = "2026-10-21T10:00:00Z"
    fake = FakeGh(open=[_open(1, ready)], prs={1: _pr(ready=ready)})
    evs, cursor = _poll(fake, REVIEW_ONLY)
    assert len(evs) == 1
    e = evs[0]
    assert e.id == f"github:o/r:1:review-wait:{int(_ts(ready))}:1" and e.source == "github" and e.kind == "review-wait"
    assert e.match == {"repo": "o/r", "pr": "1"} and e.ts == NOW and e.cursor_key == ""
    assert e.summary == "o/r#1 waits for review 10 business hours (ping 1) https://github.com/o/r/pull/1"
    assert e.payload == {"url": URL.format(1), "title": "t", "ready_at": ready, "last_human_review": None,
                         "hours": 10.0, "ping": 1}
    assert cursor["paged"] == {"o/r#1": {"episode": int(_ts(ready)), "n": 1, "at": NOW}}
    assert cursor["open"] == [_entry(1, ready)] and cursor["budget_spent"] == [] and cursor["failed"] == []
    assert fake.calls[0][0] == "o" and fake.calls[0][1] == [
        "search", "prs", "--author=@me", "--state=open", "--draft=false", "--owner", "o", "--limit", "100",
        "--json", "repository,number,url,title,createdAt"]
    assert fake.calls[1][0] == "o" and fake.calls[1][1][:2] == ["api", "graphql"]
    assert ["owner=o", "name=r", "number=1"] == [a for a in fake.calls[1][1] if a.split("=")[0] in ("owner", "name", "number")]
    evs, cursor = _poll(FakeGh(open=[_open(1, ready)], prs={1: _pr(ready=ready)}), REVIEW_ONLY, cursor, NOW + 60)
    assert evs == [] and cursor["paged"]["o/r#1"]["at"] == NOW


def test_a_pr_under_review_after_hours_is_silent(github_cfg):
    created = "2026-10-22T10:00:00Z"
    evs, cursor = _poll(FakeGh(open=[_open(1, created)], prs={1: _pr()}), REVIEW_ONLY)
    assert evs == [] and cursor["paged"] == {}


def test_approved_changes_requested_and_draft_prs_are_silent(github_cfg):
    ready = "2026-10-21T10:00:00Z"
    fake = FakeGh(open=[_open(n, ready) for n in (1, 2, 3)],
                  prs={1: _pr(decision="APPROVED"), 2: _pr(decision="CHANGES_REQUESTED"), 3: _pr(draft=True)})
    evs, cursor = _poll(fake, REVIEW_ONLY)
    assert evs == [] and cursor["paged"] == {} and len(fake.named("api", "graphql")) == 3


def test_a_bot_review_by_typename_or_ignore_list_does_not_count(github_cfg):
    ready = "2026-10-21T10:00:00Z"
    reviews = [_review("bot1", at="2026-10-22T11:00:00Z"), _review("roboto", typename="Bot", at="2026-10-22T11:30:00Z"),
               _review("me", at="2026-10-22T11:40:00Z")]
    evs, _ = _poll(FakeGh(open=[_open(1, ready)], prs={1: _pr(reviews=reviews)}), REVIEW_ONLY)
    assert [e.summary for e in evs] == ["o/r#1 waits for review 10 business hours (ping 1) https://github.com/o/r/pull/1"]
    assert evs[0].payload["last_human_review"] is None


def test_a_human_comment_review_restarts_the_count_with_a_new_episode(github_cfg):
    ready = "2026-10-19T10:00:00Z"
    wednesday = _ts("2026-10-21T12:00:00Z")
    evs, cursor = _poll(FakeGh(open=[_open(1, ready)], prs={1: _pr(ready=ready)}), REVIEW_ONLY, now=wednesday)
    assert [e.id for e in evs] == [f"github:o/r:1:review-wait:{int(_ts(ready))}:1"]
    reviewed = "2026-10-22T11:00:00Z"
    pr = _pr(ready=ready, reviews=[_review("human1", at=reviewed)])
    evs, cursor = _poll(FakeGh(open=[_open(1, ready)], prs={1: pr}), REVIEW_ONLY, cursor, NOW)
    assert evs == []
    friday = _ts("2026-10-23T16:00:00Z")
    evs, cursor = _poll(FakeGh(open=[_open(1, ready)], prs={1: pr}), REVIEW_ONLY, cursor, friday)
    assert [e.id for e in evs] == [f"github:o/r:1:review-wait:{int(_ts(reviewed))}:1"]
    assert evs[0].summary.startswith("o/r#1 waits for review 13 business hours (ping 1) ")
    assert evs[0].payload["last_human_review"] == reviewed
    assert cursor["paged"]["o/r#1"] == {"episode": int(_ts(reviewed)), "n": 1, "at": friday}


def test_a_repeat_before_repeat_hours_is_silent_and_after_it_is_ping_2_with_a_new_id(github_cfg):
    ready = "2026-10-21T10:00:00Z"

    def fake():
        return FakeGh(open=[_open(1, ready)], prs={1: _pr(ready=ready)})

    evs, cursor = _poll(fake(), REVIEW_ONLY)
    first = evs[0].id
    evs, cursor = _poll(fake(), REVIEW_ONLY, cursor, NOW + 3600)
    assert evs == []
    evs, cursor = _poll(fake(), REVIEW_ONLY, cursor, FRIDAY)
    assert [e.id for e in evs] == [f"github:o/r:1:review-wait:{int(_ts(ready))}:2"] and evs[0].id != first
    assert evs[0].summary.startswith("o/r#1 waits for review 18 business hours (ping 2) ")
    assert cursor["paged"]["o/r#1"] == {"episode": int(_ts(ready)), "n": 2, "at": FRIDAY}


def test_a_pr_gone_from_search_and_back_with_the_same_start_continues_the_count(fresh_orchestrator_dir, github_cfg):
    ready = "2026-10-21T10:00:00Z"
    evs, cursor = _poll(FakeGh(open=[_open(1, ready)], prs={1: _pr(ready=ready)}), REVIEW_ONLY)
    assert events.append(evs[0]) is True
    evs, cursor = _poll(FakeGh(open=[]), REVIEW_ONLY, cursor, FRIDAY)
    assert evs == [] and cursor["open"] == [] and cursor["paged"]["o/r#1"]["n"] == 1
    evs, cursor = _poll(FakeGh(open=[_open(1, ready)], prs={1: _pr(ready=ready)}), REVIEW_ONLY, cursor, MONDAY)
    assert [e.id for e in evs] == [f"github:o/r:1:review-wait:{int(_ts(ready))}:2"]
    assert events.append(evs[0]) is True


def test_paged_entries_older_than_30_days_are_dropped(github_cfg):
    cursor = {"paged": {"o/r#7": {"episode": 1, "n": 1, "at": NOW - 31 * 86400},
                        "o/r#8": {"episode": 1, "n": 1, "at": NOW - 86400}}}
    evs, cursor = _poll(FakeGh(open=[]), REVIEW_ONLY, cursor)
    assert evs == [] and list(cursor["paged"]) == ["o/r#8"]


def test_a_failed_search_keeps_last_pass_list_and_a_closed_pr_is_skipped(github_cfg):
    ready = "2026-10-21T10:00:00Z"
    cursor = {"open": [_entry(1, ready), _entry(2, ready)]}
    fake = FakeGh(open=None, prs={1: _pr(ready=ready, state="CLOSED"), 2: _pr(ready=ready)})
    evs, cursor = _poll(fake, REVIEW_ONLY, cursor)
    assert [e.match["pr"] for e in evs] == ["2"]
    assert cursor["open"] == [_entry(1, ready), _entry(2, ready)]
    assert [f[0] for f in cursor["failed"]] == ["open:me"] and "could not be read" in cursor["failed"][0][1]
    assert len(fake.named("api", "graphql")) == 2
    evs, cursor = _poll(FakeGh(open=[_open(2, ready)], prs={2: _pr(ready=ready)}), REVIEW_ONLY, cursor, NOW + 600)
    assert cursor["open"] == [_entry(2, ready)] and cursor["failed"] == []


def test_two_logins_make_two_searches(github_cfg):
    github_cfg(SOURCE.replace('owners = ["o"]', 'owners = ["o", "p"]') + '[gh]\n[gh.logins]\no = "me"\np = "you"\n')
    fake = FakeGh(open=[])
    _poll(fake, REVIEW_ONLY)
    searches = [(owner, argv) for owner, argv, _ in fake.calls if argv[:2] == ["search", "prs"]]
    assert sorted(owner for owner, _ in searches) == ["o", "p"]
    assert [argv[argv.index("--owner") + 1] for owner, argv in sorted(searches)] == ["o", "p"]
    assert all(argv.count("--owner") == 1 for _, argv in searches)


def test_an_owner_removed_from_the_config_loses_its_cursor_rows_and_its_pages(github_cfg):
    ready = "2026-10-21T10:00:00Z"
    cursor = {"open": [_entry(1, ready), _entry(2, ready, repo="p/x")],
              "merged": {"o/r": {"prs": [[9, MERGED_AT]]}, "p/x": {"prs": [[3, MERGED_AT]]}},
              "deploy_scan": {"p/x": {"run": 1}}}
    fake = FakeGh(open=None, merged=None, prs={1: _pr(ready=ready), 2: _pr(ready=ready)})
    evs, cursor = _poll(fake, RULES, cursor)
    assert [e.match["repo"] for e in evs] == ["o/r"]
    assert cursor["open"] == [_entry(1, ready)] and list(cursor["merged"]) == ["o/r"] and cursor["deploy_scan"] == {}
    assert not any("p/x" in a for _, argv, _ in fake.calls for a in argv)


def test_a_skipped_non_default_branch_pr_is_not_read_again(github_cfg):
    views = {URL.format(10): _view(X, base="release"), URL.format(9): _view()}
    merged = [_merged_row(10, "2026-10-21T12:00:00Z"), _merged_row(9, MERGED_AT)]
    evs, cursor = _poll(_approval_fake(merged=merged, views=views), APPROVAL_ONLY)
    fake = _approval_fake(merged=merged, views=views)
    evs, cursor = _poll(fake, APPROVAL_ONLY, cursor, NOW + 600)
    assert fake.named("pr", "view") == [] and cursor["merged"]["o/r"]["number"] == 9
    assert cursor["merged"]["o/r"]["skipped"] == [10]


def test_the_machine_zone_follows_the_local_clock():
    before = os.environ.get("TZ")
    os.environ["TZ"] = "Etc/GMT-2"
    time.tzset()
    try:
        start, now = _ts("2026-10-16T12:00:00Z"), _ts("2026-10-16T12:30:00Z")
        assert github._business_hours(start, now, "10:00-16:00", "") == 0.5
        assert github._business_hours(start, now, "10:00-16:00", "Etc/GMT-2") == 0.5
        assert github._business_hours(start, now, "10:00-16:00", "Etc/UTC") == 0.5
        assert github._business_hours(start, now, "14:00-16:00", "") == 0.5
        assert github._business_hours(start, now, "14:00-16:00", "Etc/UTC") == 0.0
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


def test_business_hours_skip_nights_and_weekends(github_cfg):
    ready = "2026-10-16T16:00:00Z"

    def fake():
        return FakeGh(open=[_open(1, ready)], prs={1: _pr(ready=ready)})

    assert _poll(fake(), REVIEW_ONLY, now=_ts("2026-10-17T12:00:00Z"))[0] == []
    assert _poll(fake(), REVIEW_ONLY, now=_ts("2026-10-19T15:30:00Z"))[0] == []
    evs, _ = _poll(fake(), REVIEW_ONLY, now=_ts("2026-10-19T16:00:00Z"))
    assert [e.summary for e in evs] == ["o/r#1 waits for review 8 business hours (ping 1) https://github.com/o/r/pull/1"]
    assert github._business_hours(_ts("2026-10-16T18:00:00Z"), _ts("2026-10-19T08:00:00Z"), "09:00-17:00", "Etc/UTC") == 0
    assert github._business_hours(_ts("2026-10-16T12:00:00Z"), _ts("2026-10-16T12:30:00Z"), "10:00-16:00", "Etc/GMT-2") == 0.5


def test_an_empty_owners_list_raises_poll_error(github_cfg):
    github_cfg(SOURCE.replace('owners = ["o"]', "owners = []") + GH)
    with pytest.raises(events.PollError, match="owners is empty"):
        _poll(FakeGh(open=[]), REVIEW_ONLY)


def test_a_none_from_gh_fails_the_selector_and_keeps_its_cursor(github_cfg):
    ready = "2026-10-21T10:00:00Z"
    cursor = {"paged": {"o/r#1": {"episode": int(_ts(ready)), "n": 1, "at": NOW - 86400}}}
    evs, cursor = _poll(FakeGh(open=[_open(1, ready)], prs={}), REVIEW_ONLY, cursor)
    assert evs == [] and cursor["paged"]["o/r#1"]["n"] == 1
    assert [f[0] for f in cursor["failed"]] == [f"pr:{URL.format(1)}"] and "could not be read" in cursor["failed"][0][1]


def test_a_spent_budget_leaves_the_rest_in_budget_spent(github_cfg, monkeypatch):
    clock = {"t": NOW}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    ready = "2026-10-21T10:00:00Z"
    fake = FakeGh(open=[_open(1, ready), _open(2, ready)], prs={1: _pr(ready=ready), 2: _pr(ready=ready)}, clock=clock,
                  spend=10.0)
    evs, cursor = _poll(fake, REVIEW_ONLY, until=NOW + 15)
    assert [e.match["pr"] for e in evs] == ["1"] and cursor["budget_spent"] == [f"pr:{URL.format(2)}"]
    assert [t for _, _, t in fake.calls] == [15.0, 5.0]
    fake2 = FakeGh(open=[_open(1, ready), _open(2, ready)], prs={1: _pr(ready=ready), 2: _pr(ready=ready)}, clock=clock,
                   spend=1.0)
    evs, cursor = _poll(fake2, REVIEW_ONLY, cursor, now=clock["t"], until=clock["t"] + 30)
    assert [e.match["pr"] for e in evs] == ["2"] and cursor["budget_spent"] == []
    assert fake2.calls[0][1][:2] == ["search", "prs"]
    assert "number=2" in fake2.calls[1][1] and "number=1" in fake2.calls[2][1]


def test_the_first_pass_does_not_seed(github_cfg):
    ready = "2026-10-21T10:00:00Z"
    evs, cursor = _poll(FakeGh(open=[_open(1, ready)], prs={1: _pr(ready=ready)}), REVIEW_ONLY, {})
    assert len(evs) == 1 and cursor["paged"]["o/r#1"]["n"] == 1


def test_a_disabled_review_rule_runs_no_search(github_cfg):
    fake = FakeGh(open=[_open(1, "2026-10-21T10:00:00Z")])
    disabled = {"review-ping": _rule("review-wait", enabled=False), "deploy-ping": _rule("approval-wait", enabled=False)}
    evs, cursor = _poll(fake, disabled)
    assert evs == [] and fake.calls == [] and cursor["budget_spent"] == []


def _approval_fake(**kw):
    kw.setdefault("merged", [_merged_row(9, MERGED_AT)])
    kw.setdefault("runs", [_run(456)])
    kw.setdefault("pending", {456: [_pending()]})
    return FakeGh(**kw)


def _job(name, conclusion, status="completed"):
    return {"name": name, "status": status, "conclusion": conclusion}


def _fallback_fake(jobs=None, **kw):
    kw.setdefault("pending", {456: [_pending("prod-fallback")]})
    kw.setdefault("deployments", _pages([_deployment("prod-fallback", HEAD, WAITING_AT, "WAITING")]))
    return _approval_fake(jobs=jobs if jobs is not None else {456: {"jobs": [_job(PROD_JOB, "skipped")]}}, **kw)


def test_a_waiting_run_containing_our_merge_pages_after_an_hour_with_waiting_since_from_the_deployment(github_cfg):
    fake = _approval_fake(deployments=_pages([WAITING, _deployment("prod", OLD, "2026-10-20T10:00:00Z", "SUCCESS")]))
    evs, cursor = _poll(fake, APPROVAL_ONLY)
    assert len(evs) == 1
    e = evs[0]
    assert e.id == f"github:o/r:run:456:approval-wait:{int(_ts(WAITING_AT))}:1" and e.kind == "approval-wait"
    assert e.match == {"repo": "o/r", "run_id": "456"} and e.ts == NOW
    assert e.summary == "o/r run 456 waits for prod approval 3 business hours (ping 1) https://gh/o/r/actions/runs/456"
    assert e.payload == {"url": RUN_URL.format(456), "envs": ["prod"], "can_approve": False, "reviewers": ["team-x"],
                         "waiting_since": WAITING_AT, "hours": 3.0, "ping": 1}
    assert cursor["paged"] == {"o/r:run:456": {"episode": int(_ts(WAITING_AT)), "n": 1, "at": NOW}}
    assert cursor["merged"]["o/r"]["sha"] == SHA and cursor["merged"]["o/r"]["number"] == 9
    assert cursor["merged"]["o/r"]["closedAt"] == MERGED_AT and cursor["deploy_scan"] == {}
    assert cursor["default_branch"] == {"o/r": "main"}
    assert fake.calls[0][0] == "o" and fake.calls[0][1] == [
        "search", "prs", "--author=@me", "--merged", "--merged-at", ">=2026-10-08", "--owner", "o", "--limit", "100",
        "--json", "repository,closedAt,number"]
    assert fake.named("run", "list")[0] == ["run", "list", "--repo", "o/r", "--status", "waiting", "--limit", "5",
                                            "--json", "databaseId,url,createdAt,headSha,workflowName"]
    assert fake.named("pr", "view")[0] == ["pr", "view", URL.format(9), "--json", "mergeCommit,baseRefName"]
    assert fake.named("api", "repos/o/r/actions/runs/456/pending_deployments")
    dep = fake.deployment_calls()[0]
    assert ["owner=o", "name=r", "envs[]=prod", "envs[]=prod-fallback"] == [a for a in dep if "=" in a and not a.startswith("query=")]
    assert cursor["contains"] == {f"{SHA}:{HEAD}": True, f"{SHA}:{OLD}": False}


def test_a_run_older_than_our_merge_is_skipped(github_cfg):
    fake = _approval_fake(runs=[_run(456, created="2026-10-21T07:00:00Z")])
    evs, cursor = _poll(fake, APPROVAL_ONLY)
    assert evs == [] and not fake.named("api", "repos/o/r/actions/runs/456/pending_deployments")


def test_a_run_without_our_merge_by_compare_is_skipped(github_cfg):
    fake = _approval_fake(compare={f"{SHA}...{HEAD}": "diverged"})
    evs, cursor = _poll(fake, APPROVAL_ONLY)
    assert evs == [] and fake.deployment_calls() == [] and cursor["contains"] == {f"{SHA}:{HEAD}": False}


def test_only_the_newest_of_two_waiting_runs_is_considered(github_cfg):
    newer = _run(457, created="2026-10-22T09:30:00Z")
    fake = _approval_fake(runs=[_run(456), newer], pending={457: [_pending()]},
                          deployments=_pages([_deployment("prod", HEAD, "2026-10-22T10:00:00Z", "WAITING")]))
    evs, _ = _poll(fake, APPROVAL_ONLY)
    assert [e.match["run_id"] for e in evs] == ["457"]
    assert not fake.named("api", "repos/o/r/actions/runs/456/pending_deployments")


def test_a_newer_waiting_run_without_our_merge_does_not_hide_ours(github_cfg):
    newer = _run(457, head=X, created="2026-10-22T09:30:00Z")
    fake = _approval_fake(runs=[_run(456), newer], compare={f"{SHA}...{X}": "diverged"})
    evs, cursor = _poll(fake, APPROVAL_ONLY)
    assert [e.match["run_id"] for e in evs] == ["456"]
    assert not fake.named("api", "repos/o/r/actions/runs/457/pending_deployments")
    assert cursor["contains"][f"{SHA}:{X}"] is False
    older = _run(455, created="2026-10-21T07:00:00Z")
    fake = _approval_fake(runs=[older, newer], compare={f"{SHA}...{X}": "diverged"})
    evs, _ = _poll(fake, APPROVAL_ONLY)
    assert evs == [] and fake.named("api", "repos/o/r/compare/" + f"{SHA}...{HEAD}") == []


def test_a_run_waiting_only_on_an_environment_outside_envs_is_skipped(github_cfg):
    fake = _approval_fake(pending={456: [_pending("prod-fallback")]})
    evs, cursor = _poll(fake, APPROVAL_ONLY)
    assert evs == [] and fake.deployment_calls() == [] and cursor["paged"] == {}


def test_a_run_waiting_only_on_a_fallback_environment_pages_when_its_prod_job_was_skipped(github_cfg):
    github_cfg(SOURCE_FALLBACK + GH)
    fake = _fallback_fake()
    evs, cursor = _poll(fake, APPROVAL_ONLY)
    assert len(evs) == 1
    e = evs[0]
    assert e.summary == "o/r run 456 waits for prod-fallback approval 3 business hours (ping 1) https://gh/o/r/actions/runs/456"
    assert e.payload["envs"] == ["prod-fallback"] and e.payload["waiting_since"] == WAITING_AT
    assert fake.named("run", "view") == [["run", "view", "456", "--repo", "o/r", "--json", "jobs"]]
    assert cursor["paged"] == {"o/r:run:456": {"episode": int(_ts(WAITING_AT)), "n": 1, "at": NOW}}


@pytest.mark.parametrize("status,conclusion", [("in_progress", ""), ("completed", "success"), ("completed", "failure"),
                                               ("completed", "cancelled"), ("queued", "")])
def test_a_fallback_run_whose_prod_job_is_not_skipped_stays_silent(github_cfg, status, conclusion):
    github_cfg(SOURCE_FALLBACK + GH)
    fake = _fallback_fake(jobs={456: {"jobs": [_job(PROD_JOB, conclusion, status)]}})
    evs, cursor = _poll(fake, APPROVAL_ONLY)
    assert evs == [] and cursor["paged"] == {} and cursor["failed"] == []


def test_a_fallback_run_without_a_matching_job_stays_silent(github_cfg):
    github_cfg(SOURCE_FALLBACK + GH)
    fake = _fallback_fake(jobs={456: {"jobs": [_job("service / deploy-fallback-prod / Deploy", "", "waiting"), _job("e2e", "failure")]}})
    evs, cursor = _poll(fake, APPROVAL_ONLY)
    assert evs == [] and cursor["paged"] == {}


@pytest.mark.parametrize("jobs", [
    [_job(PROD_JOB + " / Deploy: prod", "", "in_progress"), _job(PROD_JOB + " / Notify on failure", "skipped")],
    [_job(PROD_JOB + " / Deploy: prod", "failure"), _job(PROD_JOB + " / Smoke", "skipped")],
    [_job(PROD_JOB + " (us) / Deploy: prod", "failure"), _job(PROD_JOB + " (eu)", "skipped")]])
def test_a_fallback_run_with_a_matching_job_that_is_not_skipped_stays_silent_beside_a_skipped_one(github_cfg, jobs):
    github_cfg(SOURCE_FALLBACK + GH)
    evs, cursor = _poll(_fallback_fake(jobs={456: {"jobs": jobs}}), APPROVAL_ONLY)
    assert evs == [] and cursor["paged"] == {} and cursor["failed"] == []


def test_a_waiting_deployment_of_a_second_primary_environment_still_dates_the_wait(github_cfg):
    github_cfg(SOURCE.replace('envs = ["prod"]', 'envs = ["prod", "canary"]') + GH)
    canary = _deployment("canary", HEAD, "2026-10-22T10:30:00Z", "WAITING")
    fake = _approval_fake(deployments=_pages([canary, WAITING]))
    evs, _ = _poll(fake, APPROVAL_ONLY)
    assert evs[0].payload["envs"] == ["prod"] and evs[0].payload["waiting_since"] == "2026-10-22T10:30:00Z"
    dep = fake.deployment_calls()[0]
    assert "envs[]=canary" in dep


def test_an_empty_fallback_skipped_job_never_pages_or_reads_jobs(github_cfg):
    github_cfg(SOURCE + 'fallback_envs = ["prod-fallback"]\n' + GH)
    fake = _fallback_fake()
    evs, cursor = _poll(fake, APPROVAL_ONLY)
    assert evs == [] and cursor["paged"] == {} and fake.named("run", "view") == []


def test_a_run_waiting_on_a_primary_and_a_fallback_environment_takes_the_primary_path(github_cfg):
    github_cfg(SOURCE_FALLBACK + GH)
    newer_fallback = _deployment("prod-fallback", HEAD, "2026-10-22T10:30:00Z", "WAITING")
    fake = _fallback_fake(pending={456: [_pending(), _pending("prod-fallback")]}, deployments=_pages([newer_fallback, WAITING]),
                          jobs={456: {"jobs": [_job(PROD_JOB, "", "in_progress")]}})
    evs, cursor = _poll(fake, APPROVAL_ONLY)
    assert evs[0].summary == "o/r run 456 waits for prod approval 3 business hours (ping 1) https://gh/o/r/actions/runs/456"
    assert evs[0].payload["envs"] == ["prod"] and evs[0].payload["waiting_since"] == WAITING_AT
    assert fake.named("run", "view") == [] and cursor["paged"]["o/r:run:456"]["episode"] == int(_ts(WAITING_AT))


def test_a_scan_cached_on_the_fallback_path_does_not_feed_the_primary_path(github_cfg):
    github_cfg(SOURCE_FALLBACK + GH)
    fallback_wait = _deployment("prod-fallback", HEAD, "2026-10-22T09:30:00Z", "WAITING")
    first = _fallback_fake(deployments=_pages([fallback_wait] + _errors(99), _errors(100), _errors(100), _errors(1)))
    evs, cursor = _poll(first, APPROVAL_ONLY)
    assert evs == [] and cursor["deploy_scan"]["o/r"]["waiting_since"] == "2026-10-22T09:30:00Z"
    assert cursor["deploy_scan"]["o/r"]["envs"] == ["prod-fallback"] and first.named("run", "view") == []
    prod_wait = _deployment("prod", HEAD, "2026-10-22T10:00:00Z", "WAITING")
    done = [_deployment("prod", OLD, "2026-10-20T10:00:00Z", "SUCCESS"), _deployment("prod-fallback", OLD, "2026-10-20T10:00:00Z", "SUCCESS")]
    second = _fallback_fake(pending={456: [_pending(), _pending("prod-fallback")]},
                            deployments=_pages([prod_wait] + done, _errors(100), _errors(100), _errors(1)))
    evs, cursor = _poll(second, APPROVAL_ONLY, cursor)
    assert evs[0].summary == "o/r run 456 waits for prod approval 2 business hours (ping 1) https://gh/o/r/actions/runs/456"
    assert evs[0].payload["waiting_since"] == "2026-10-22T10:00:00Z" and cursor["deploy_scan"] == {}


def test_a_fallback_environment_outside_deployed_envs_is_queried_for_its_waiting_deployment(github_cfg):
    github_cfg(SOURCE_FALLBACK.replace('deployed_envs = ["prod", "prod-fallback"]', 'deployed_envs = ["prod"]') + GH)
    fake = _fallback_fake(deployments=_pages([_deployment("prod-fallback", HEAD, "2026-10-22T10:00:00Z", "WAITING"),
                                              _deployment("prod", OLD, "2026-10-20T10:00:00Z", "SUCCESS")]))
    evs, _ = _poll(fake, APPROVAL_ONLY)
    dep = fake.deployment_calls()[0]
    assert ["owner=o", "name=r", "envs[]=prod", "envs[]=prod-fallback"] == [a for a in dep if "=" in a and not a.startswith("query=")]
    assert evs[0].summary.startswith("o/r run 456 waits for prod-fallback approval 2 business hours (ping 1)")
    assert evs[0].payload["waiting_since"] == "2026-10-22T10:00:00Z"


def test_a_fallback_run_under_the_threshold_reads_no_jobs(github_cfg):
    github_cfg(SOURCE_FALLBACK + GH)
    fake = _fallback_fake(deployments=_pages([_deployment("prod-fallback", HEAD, "2026-10-22T11:30:00Z", "WAITING")]))
    evs, cursor = _poll(fake, APPROVAL_ONLY)
    assert evs == [] and fake.named("run", "view") == [] and cursor["paged"] == {}


def test_an_unreadable_job_list_fails_the_selector_without_paging(github_cfg):
    github_cfg(SOURCE_FALLBACK + GH)
    fake = _fallback_fake(jobs={})
    evs, cursor = _poll(fake, APPROVAL_ONLY)
    assert evs == [] and cursor["paged"] == {}
    assert [f[0] for f in cursor["failed"]] == ["run:o/r"] and "jobs of run 456 could not be read" in cursor["failed"][0][1]


def test_waiting_since_on_the_fallback_path_comes_from_the_fallback_deployment(github_cfg):
    github_cfg(SOURCE_FALLBACK + GH)
    fake = _fallback_fake(deployments=_pages([_deployment("prod-fallback", HEAD, "2026-10-22T10:00:00Z", "WAITING")]))
    evs, _ = _poll(fake, APPROVAL_ONLY)
    assert evs[0].summary.startswith("o/r run 456 waits for prod-fallback approval 2 business hours (ping 1)")
    assert evs[0].payload["waiting_since"] == "2026-10-22T10:00:00Z"


def test_the_fallback_job_read_sees_a_job_past_the_rest_page_size(github_cfg):
    github_cfg(SOURCE_FALLBACK + GH)
    filler = [_job(f"service / job-{i}", "success") for i in range(33)]
    fake = _fallback_fake(jobs={456: {"jobs": filler + [_job(PROD_JOB, "skipped")]}})
    evs, _ = _poll(fake, APPROVAL_ONLY)
    assert len(evs) == 1 and evs[0].payload["envs"] == ["prod-fallback"]


def test_can_approve_puts_you_can_approve_in_the_summary(github_cfg):
    fake = _approval_fake(pending={456: [_pending(can=True, reviewers=("devs",)), _pending("prod-fallback")]})
    evs, _ = _poll(fake, APPROVAL_ONLY)
    assert evs[0].summary == ("o/r run 456 waits for prod approval 3 business hours (ping 1, you can approve) "
                              "https://gh/o/r/actions/runs/456")
    assert evs[0].payload["can_approve"] is True and evs[0].payload["reviewers"] == ["devs"]
    assert evs[0].payload["envs"] == ["prod"]


def test_the_newest_successful_deployment_containing_our_merge_silences(github_cfg):
    fake = _approval_fake(deployments=_pages([WAITING, _deployment("prod", D1, "2026-10-21T12:00:00Z", "SUCCESS")]),
                          compare={f"{SHA}...{D1}": "identical"})
    evs, cursor = _poll(fake, APPROVAL_ONLY)
    assert evs == [] and cursor["paged"] == {} and cursor["deploy_scan"] == {}


def test_an_inactive_deployment_is_skipped_and_the_next_success_decides(github_cfg):
    nodes = [WAITING, _deployment("prod", D1, "2026-10-21T12:00:00Z", "INACTIVE"),
             _deployment("prod", D2, "2026-10-20T12:00:00Z", "SUCCESS")]
    fake = _approval_fake(deployments=_pages(nodes), compare={f"{SHA}...{D1}": "ahead", f"{SHA}...{D2}": "behind"})
    evs, cursor = _poll(fake, APPROVAL_ONLY)
    assert len(evs) == 1 and f"{SHA}:{D1}" not in cursor["contains"] and cursor["contains"][f"{SHA}:{D2}"] is False


def test_a_newer_deployment_without_our_merge_does_not_silence(github_cfg):
    fake = _approval_fake(deployments=_pages([WAITING, _deployment("prod", D2, "2026-10-21T20:00:00Z", "SUCCESS")]),
                          compare={f"{SHA}...{D2}": "behind"})
    evs, _ = _poll(fake, APPROVAL_ONLY)
    assert [e.match["run_id"] for e in evs] == ["456"]


def test_a_hundred_undeployed_then_a_success_on_page_two_silences_with_two_graphql_calls(github_cfg):
    fake = _approval_fake(deployments=_pages([WAITING] + _errors(99), [_deployment("prod", D1, "2026-10-19T10:00:00Z", "SUCCESS")]),
                          compare={f"{SHA}...{D1}": "ahead"})
    evs, cursor = _poll(fake, APPROVAL_ONLY)
    assert evs == [] and len(fake.deployment_calls()) == 2 and cursor["deploy_scan"] == {}
    assert "after=c1" in fake.deployment_calls()[1] and not any(a.startswith("after=") for a in fake.deployment_calls()[0])


def test_three_pages_without_a_decision_record_deploy_scan_and_the_next_poll_continues_from_it(github_cfg):
    fallback_done = _deployment("prod-fallback", D2, "2026-10-21T09:00:00Z", "SUCCESS")
    fallback_ours = _deployment("prod-fallback", D1, "2026-10-18T09:00:00Z", "SUCCESS")
    prod_old = _deployment("prod", D2, "2026-10-18T10:00:00Z", "SUCCESS")
    pages = _pages([WAITING, fallback_done] + _errors(98), _errors(100), _errors(100), [fallback_ours, prod_old])
    compare = {f"{SHA}...{D1}": "ahead", f"{SHA}...{D2}": "behind"}
    fake = _approval_fake(deployments=pages, compare=compare)
    evs, cursor = _poll(fake, APPROVAL_ONLY)
    assert evs == [] and len(fake.deployment_calls()) == 3 and cursor["failed"] == []
    assert cursor["deploy_scan"]["o/r"] == {"run": 456, "after": "c3", "waiting_since": WAITING_AT,
                                            "decided": {"prod-fallback": False}, "at": NOW, "scanned": 300,
                                            "envs": ["prod"]}
    assert cursor["paged"] == {}
    fake2 = _approval_fake(deployments=pages, compare=compare)
    evs, cursor = _poll(fake2, APPROVAL_ONLY, cursor, NOW + 600)
    assert len(fake2.deployment_calls()) == 1 and "after=c3" in fake2.deployment_calls()[0]
    assert [e.id for e in evs] == [f"github:o/r:run:456:approval-wait:{int(_ts(WAITING_AT))}:1"]
    assert cursor["deploy_scan"] == {} and f"{SHA}:{D1}" not in cursor["contains"]


def test_an_exhausted_list_without_a_success_means_not_deployed(github_cfg):
    fake = _approval_fake(deployments=_pages([WAITING] + _errors(5)))
    evs, cursor = _poll(fake, APPROVAL_ONLY)
    assert len(evs) == 1 and len(fake.deployment_calls()) == 1 and cursor["deploy_scan"] == {}


def test_a_waiting_deployment_gives_waiting_since_else_the_run_created_at(github_cfg):
    created = "2026-10-22T10:00:00Z"
    fake = _approval_fake(runs=[_run(456, created=created)], deployments=_pages(_errors(2)))
    evs, _ = _poll(fake, APPROVAL_ONLY)
    assert [e.id for e in evs] == [f"github:o/r:run:456:approval-wait:{int(_ts(created))}:1"]
    assert evs[0].summary.startswith("o/r run 456 waits for prod approval 2 business hours (ping 1) ")
    assert evs[0].payload["waiting_since"] == created


def test_a_merged_pr_into_a_non_default_branch_is_not_our_merge(github_cfg):
    views = {URL.format(10): _view(X, base="release"), URL.format(9): _view()}
    fake = _approval_fake(merged=[_merged_row(10, "2026-10-21T12:00:00Z"), _merged_row(9, MERGED_AT)], views=views,
                          compare={f"{X}...{HEAD}": "behind"})
    evs, cursor = _poll(fake, APPROVAL_ONLY)
    assert [e.match["run_id"] for e in evs] == ["456"]
    assert cursor["merged"]["o/r"]["number"] == 9 and cursor["merged"]["o/r"]["sha"] == SHA
    assert [a[2] for a in fake.named("pr", "view")] == [URL.format(10), URL.format(9)]
    evs, cursor = _poll(_approval_fake(merged=[_merged_row(10, "2026-10-21T12:00:00Z"), _merged_row(9, MERGED_AT)],
                                       views=views), APPROVAL_ONLY, cursor, NOW + 600)
    assert cursor["merged"]["o/r"]["number"] == 9


def test_a_rerun_with_a_new_deployment_is_a_new_episode(fresh_orchestrator_dir, github_cfg):
    evs, cursor = _poll(_approval_fake(), APPROVAL_ONLY)
    assert events.append(evs[0]) is True
    rerun_at = "2026-10-23T10:00:00Z"
    fake = _approval_fake(deployments=_pages([_deployment("prod", HEAD, rerun_at, "WAITING")]))
    evs, cursor = _poll(fake, APPROVAL_ONLY, cursor, FRIDAY)
    assert [e.id for e in evs] == [f"github:o/r:run:456:approval-wait:{int(_ts(rerun_at))}:1"]
    assert events.append(evs[0]) is True
    assert cursor["paged"]["o/r:run:456"] == {"episode": int(_ts(rerun_at)), "n": 1, "at": FRIDAY}


def test_a_success_run_without_a_deployment_does_not_silence(github_cfg):
    evs, _ = _poll(_approval_fake(deployments=_pages([WAITING])), APPROVAL_ONLY)
    assert len(evs) == 1


def test_contains_cache_entries_for_a_stale_base_are_pruned(github_cfg):
    cursor = {"merged": {"o/r": {"closedAt": MERGED_AT, "number": 9, "sha": SHA}},
              "contains": {f"{OLD}:{HEAD}": True, f"{SHA}:{HEAD}": True},
              "deploy_scan": {"o/gone": {"run": 1}}}
    evs, cursor = _poll(_approval_fake(deployments=_pages([WAITING] + _errors(1))), APPROVAL_ONLY, cursor)
    assert len(evs) == 1 and cursor["contains"] == {f"{SHA}:{HEAD}": True}
    assert cursor["deploy_scan"] == {}


def test_a_disabled_approval_rule_runs_no_merged_search(github_cfg):
    fake = _approval_fake(open=[])
    _poll(fake, REVIEW_ONLY)
    assert all("--merged" not in argv for _, argv, _ in fake.calls) and fake.named("run", "list") == []
