import time

import pytest

from dockwright import config, events
from dockwright.event_sources import deploy

NOW = 1792700000.0
SHA = "a" * 40
HEAD = "b" * 40
OLD = "c" * 40
PROD_HEAD = "d" * 40
FOREIGN = "e" * 40
DATE = "2026-10-22T10:00:00Z"


@pytest.fixture
def deploy_cfg(tmp_path, monkeypatch):
    cfg = tmp_path / "events.toml"
    cfg.write_text('[events]\nenabled = true\n[events.sources.deploy]\nworkflows = ["W1", "W2"]\n'
                   'job_match = "Deploy:|static-deploy"\njob_skip = "prod|Fallback"\n'
                   '[events.sources.github]\ndeployed_envs = ["prod", "prod-fallback"]\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    return cfg


def _deployment(env, sha, created, status):
    return {"environment": env, "commitOid": sha, "createdAt": created,
            "latestStatus": {"state": status} if status else None}


def _pages(*lists):
    def page(after):
        i = int(after[1:]) if after else 0
        more = i + 1 < len(lists)
        return {"pageInfo": {"hasNextPage": more, "endCursor": f"c{i + 1}" if more else None}, "nodes": lists[i]}

    return page


def _sub(sha=SHA, env="staging", run_id=None, sub_id="d1", repo="o/r"):
    match = {"repo": repo, "env": env}
    match.update({"run_id": run_id} if run_id else {"sha": sha})
    return events.Sub(sub_id, "K", "post-merge", "deploy", match, None, None, None, None, None, NOW, None)


def _run(rid, head=HEAD, status="completed", conclusion="success", created="2026-10-22T11:00:00Z"):
    return {"databaseId": rid, "headSha": head, "status": status, "conclusion": conclusion,
            "url": f"https://gh/o/r/actions/runs/{rid}", "createdAt": created}


def _job(name, status="completed", conclusion="success", done="2026-10-22T11:10:00Z", rid=1):
    return {"name": name, "status": status, "conclusion": conclusion, "completedAt": done,
            "url": f"https://gh/o/r/actions/runs/{rid}/job/{name.replace(' ', '')}"}


class FakeGh:
    def __init__(self, runs=None, views=None, compare=None, commit_date=DATE, clock=None, spend=0.0, workflows=None,
                 deployments=None):
        self.runs = runs if runs is not None else {}
        self.views = views or {}
        self.compare = compare or {}
        self.commit_date = commit_date
        self.calls = []
        self.clock = clock
        self.spend = spend
        self.workflows = workflows
        self.deployments = deployments

    def __call__(self, owner, argv, timeout=None):
        self.calls.append((owner, list(argv), timeout))
        if self.clock is not None:
            self.clock["t"] += self.spend
        if argv[:2] == ["api", "graphql"] and "deployments(" in argv[3]:
            after = next((a.split("=", 1)[1] for a in argv if a.startswith("after=")), None)
            page = self.deployments(after) if self.deployments else None
            return None if page is None else {"data": {"repository": {"deployments": page}}}
        if argv[:2] == ["workflow", "list"]:
            names = self.workflows if self.workflows is not None else [n for n, v in self.runs.items() if v is not None]
            return None if names is None else [{"name": n} for n in names]
        if argv[:2] == ["run", "list"]:
            return self.runs.get(argv[argv.index("--workflow") + 1])
        if argv[:2] == ["run", "view"]:
            return self.views.get(int(argv[2]))
        if argv[0] == "api" and "/commits/" in argv[1]:
            return {"commit": {"committer": {"date": self.commit_date}}}
        if argv[0] == "api" and "/compare/" in argv[1]:
            pair = argv[1].rsplit("/", 1)[1]
            return {"status": self.compare.get(pair, "ahead")}
        raise AssertionError(argv)

    def named(self, *head):
        return [a for _, a, _ in self.calls if a[:len(head)] == list(head)]

    def deployment_calls(self):
        return [a for _, a, _ in self.calls if a[:2] == ["api", "graphql"] and "deployments(" in a[3]]

    def compare_calls(self):
        return [a for _, a, _ in self.calls if a[0] == "api" and "/compare/" in a[1]]


def _view(jobs, status="completed", conclusion="success", rid=1):
    return {"status": status, "conclusion": conclusion, "url": f"https://gh/o/r/actions/runs/{rid}", "jobs": jobs}


def test_the_newest_successful_run_containing_the_sha_wins(deploy_cfg):
    fake = FakeGh(runs={"W1": [_run(1, created="2026-10-22T11:00:00Z"), _run(2, conclusion="failure",
                                                                                created="2026-10-22T12:00:00Z")],
                        "W2": None},
                  views={1: _view([_job("Deploy: api (staging)", rid=1)], rid=1),
                         2: _view([_job("Deploy: api (staging)", conclusion="failure", rid=2)], conclusion="failure",
                                  rid=2)})
    evs, cursor = deploy.poll([_sub()], {}, {}, {}, NOW, call=fake)
    assert len(evs) == 1
    e = evs[0]
    assert e.id == f"deploy:o/r:{SHA}:staging:1:d1" and e.kind == "job" and e.source == "deploy" and e.cursor_key == "1"
    assert e.match == {"repo": "o/r", "env": "staging", "sha": SHA, "sub_id": "d1"}
    assert e.summary == "o/r staging success 2026-10-22T11:10:00Z https://gh/o/r/actions/runs/1/job/Deploy:api(staging)"
    assert e.payload == {"outcome": "success", "run_id": 1, "job": "Deploy: api (staging)",
                         "url": "https://gh/o/r/actions/runs/1/job/Deploy:api(staging)"}
    assert e.ts == NOW and cursor["decided"] == {"d1": 1}
    assert cursor["commit_date"] == {SHA: DATE} and cursor["contains"] == {f"{SHA}:{HEAD}": True}
    listed = [a for _, a, _ in fake.calls if a[:2] == ["run", "list"]]
    assert listed[0][2:] == ["--repo", "o/r", "--workflow", "W1", "--created", ">=2026-10-22T09:00:00Z", "--limit",
                             "10", "--json", "databaseId,headSha,status,conclusion,url,createdAt"]
    assert fake.calls[0][0] == "o"


def test_the_newest_success_ends_the_views_and_an_older_run_is_never_read(deploy_cfg):
    fake = FakeGh(runs={"W1": [_run(1, created="2026-10-22T11:00:00Z"), _run(2, created="2026-10-22T12:00:00Z"),
                               _run(3, conclusion="failure", created="2026-10-22T13:00:00Z")], "W2": []},
                  views={3: _view([_job("Deploy: api (staging)", conclusion="failure", rid=3)], conclusion="failure", rid=3),
                         2: _view([_job("Deploy: api (staging)", rid=2)], rid=2),
                         1: _view([_job("Deploy: api (staging)", rid=1)], rid=1)})
    evs, cursor = deploy.poll([_sub()], {}, {}, {}, NOW, call=fake)
    assert [e.payload["run_id"] for e in evs] == [2]
    assert [a[2] for _, a, _ in fake.calls if a[:2] == ["run", "view"]] == ["3", "2"]


def test_a_run_whose_head_is_behind_the_sha_is_ignored(deploy_cfg):
    fake = FakeGh(runs={"W1": [_run(1, head=OLD)], "W2": []}, compare={f"{SHA}...{OLD}": "behind"},
                  views={1: _view([_job("Deploy: api (staging)")])})
    evs, cursor = deploy.poll([_sub()], {}, {}, {}, NOW, call=fake)
    assert evs == [] and cursor["contains"] == {f"{SHA}:{OLD}": False}
    assert not [a for _, a, _ in fake.calls if a[:2] == ["run", "view"]]
    fake = FakeGh(runs={"W1": [_run(1, head=OLD)], "W2": []}, compare={f"{SHA}...{OLD}": "diverged"})
    evs, _ = deploy.poll([_sub()], {}, {}, {}, NOW, call=fake)
    assert evs == []


def test_a_completed_run_with_no_env_job_is_nojob(deploy_cfg):
    fake = FakeGh(runs={"W1": [_run(1)], "W2": []}, views={1: _view([_job("Deploy: api (prod)")])})
    evs, _ = deploy.poll([_sub()], {}, {}, {}, NOW, call=fake)
    assert [e.payload["outcome"] for e in evs] == ["nojob"]
    assert evs[0].summary == "o/r staging nojob 2026-10-22T11:00:00Z https://gh/o/r/actions/runs/1"
    assert evs[0].payload == {"outcome": "nojob", "run_id": 1, "job": "", "url": "https://gh/o/r/actions/runs/1"}
    fake = FakeGh(runs={"W1": [_run(1, conclusion="failure")], "W2": []}, views={1: _view([], conclusion="failure")})
    assert [e.payload["outcome"] for e in deploy.poll([_sub()], {}, {}, {}, NOW, call=fake)[0]] == ["failure"]
    fake = FakeGh(runs={"W1": [_run(1, conclusion="cancelled")], "W2": []},
                  views={1: _view([], conclusion="cancelled")})
    assert [e.payload["outcome"] for e in deploy.poll([_sub()], {}, {}, {}, NOW, call=fake)[0]] == ["cancelled"]


def test_prod_success_in_prod_fallback_containing_the_sha_is_success(deploy_cfg):
    done = _deployment("prod-fallback", PROD_HEAD, "2026-10-22T21:48:17Z", "SUCCESS")
    fake = FakeGh(deployments=_pages([done]), compare={f"{SHA}...{PROD_HEAD}": "ahead"})
    evs, cursor = deploy.poll([_sub(env="prod")], {}, {}, {}, NOW, call=fake)
    assert [e.payload["outcome"] for e in evs] == ["success"]
    assert evs[0].summary == f"o/r prod success 2026-10-22T21:48:17Z https://github.com/o/r/commit/{PROD_HEAD}"
    assert evs[0].payload == {"outcome": "success", "run_id": None, "job": "prod-fallback",
                              "url": f"https://github.com/o/r/commit/{PROD_HEAD}"}
    assert evs[0].id == f"deploy:o/r:{SHA}:prod:{PROD_HEAD[:12]}:d1"
    assert cursor["decided"] == {"d1": PROD_HEAD[:12]} and "deployments" not in cursor
    assert len(fake.deployment_calls()) == 1 and len(fake.compare_calls()) == 1
    assert not fake.named("run", "list") and not fake.named("workflow", "list")


def test_prod_success_of_a_foreign_commit_emits_nothing(deploy_cfg):
    fake = FakeGh(deployments=_pages([_deployment("prod", FOREIGN, "2026-10-22T10:00:00Z", "SUCCESS")]),
                  compare={f"{SHA}...{FOREIGN}": "behind"})
    evs, cursor = deploy.poll([_sub(env="prod")], {}, {}, {}, NOW, call=fake)
    assert evs == [] and cursor["decided"] == {} and cursor["failed"] == []
    assert len(fake.deployment_calls()) == 1


def test_prod_waiting_emits_nothing(deploy_cfg):
    fake = FakeGh(deployments=_pages([_deployment("prod", PROD_HEAD, "2026-10-22T10:00:00Z", "WAITING")]))
    evs, cursor = deploy.poll([_sub(env="prod")], {}, {}, {}, NOW, call=fake)
    assert evs == [] and cursor["failed"] == [] and len(fake.deployment_calls()) == 1


def test_prod_error_of_our_sha_is_not_a_failure(deploy_cfg):
    fake = FakeGh(deployments=_pages([_deployment("prod", PROD_HEAD, "2026-10-22T10:00:00Z", "ERROR")]),
                  compare={f"{SHA}...{PROD_HEAD}": "ahead"})
    evs, cursor = deploy.poll([_sub(env="prod")], {}, {}, {}, NOW, call=fake)
    assert evs == [] and cursor["failed"] == [] and fake.compare_calls() == []
    assert len(fake.deployment_calls()) == 1


def test_an_older_prod_success_under_a_newer_error_is_success(deploy_cfg):
    newer = _deployment("prod", FOREIGN, "2026-10-23T10:00:00Z", "ERROR")
    older = _deployment("prod", PROD_HEAD, "2026-10-22T10:00:00Z", "SUCCESS")
    fake = FakeGh(deployments=_pages([newer, older]), compare={f"{SHA}...{PROD_HEAD}": "identical"})
    evs, _ = deploy.poll([_sub(env="prod")], {}, {}, {}, NOW, call=fake)
    assert [e.payload["outcome"] for e in evs] == ["success"]


def test_prod_reads_the_next_page_when_the_first_has_no_success(deploy_cfg):
    pages = _pages([_deployment("prod", FOREIGN, "2026-10-23T10:00:00Z", "ERROR")],
                   [_deployment("prod", PROD_HEAD, "2026-10-22T10:00:00Z", "SUCCESS")])
    fake = FakeGh(deployments=pages, compare={f"{SHA}...{PROD_HEAD}": "ahead"})
    evs, _ = deploy.poll([_sub(env="prod")], {}, {}, {}, NOW, call=fake)
    assert [e.payload["outcome"] for e in evs] == ["success"] and len(fake.deployment_calls()) == 2


def test_two_prod_subs_on_one_repo_share_one_deployments_call(deploy_cfg):
    done = _deployment("prod", PROD_HEAD, "2026-10-22T10:00:00Z", "SUCCESS")
    fake = FakeGh(deployments=_pages([done]), compare={f"{SHA}...{PROD_HEAD}": "ahead", f"{OLD}...{PROD_HEAD}": "behind"})
    evs, _ = deploy.poll([_sub(env="prod"), _sub(sha=OLD, env="prod", sub_id="d2")], {}, {}, {}, NOW, call=fake)
    assert [e.match["sub_id"] for e in evs] == ["d1"] and len(fake.deployment_calls()) == 1


def test_prod_picks_the_success_with_the_newest_status_time_not_the_newest_deployment(deploy_cfg):
    redeployed = _deployment("prod", PROD_HEAD, "2026-10-22T10:00:00Z", "SUCCESS")
    redeployed["latestStatus"]["createdAt"] = "2026-10-24T09:00:00Z"
    newer = _deployment("prod", FOREIGN, "2026-10-23T10:00:00Z", "SUCCESS")
    newer["latestStatus"]["createdAt"] = "2026-10-23T10:05:00Z"
    fake = FakeGh(deployments=_pages([newer, redeployed]),
                  compare={f"{SHA}...{PROD_HEAD}": "ahead", f"{SHA}...{FOREIGN}": "behind"})
    evs, _ = deploy.poll([_sub(env="prod")], {}, {}, {}, NOW, call=fake)
    assert [e.payload["outcome"] for e in evs] == ["success"] and evs[0].payload["job"] == "prod"
    assert evs[0].id == f"deploy:o/r:{SHA}:prod:{PROD_HEAD[:12]}:d1"
    fake = FakeGh(deployments=_pages([redeployed, newer]), compare={f"{SHA}...{FOREIGN}": "ahead", f"{SHA}...{PROD_HEAD}": "behind"})
    evs, _ = deploy.poll([_sub(env="prod")], {}, {}, {}, NOW, call=fake)
    assert evs == [] and len(fake.compare_calls()) == 1


def test_prod_uses_the_status_log_url_when_present(deploy_cfg):
    node = _deployment("prod", PROD_HEAD, "2026-10-22T10:00:00Z", "SUCCESS")
    node["latestStatus"].update({"createdAt": "2026-10-22T10:05:00Z", "logUrl": "https://gh/o/r/deployments/1"})
    fake = FakeGh(deployments=_pages([node]), compare={f"{SHA}...{PROD_HEAD}": "ahead"})
    evs, _ = deploy.poll([_sub(env="prod")], {}, {}, {}, NOW, call=fake)
    assert evs[0].summary == "o/r prod success 2026-10-22T10:05:00Z https://gh/o/r/deployments/1"


def test_prod_polls_without_deploy_workflows(deploy_cfg):
    deploy_cfg.write_text('[events]\nenabled = true\n[events.sources.deploy]\nworkflows = []\n'
                          '[events.sources.github]\ndeployed_envs = ["prod"]\n')
    done = _deployment("prod", PROD_HEAD, "2026-10-22T10:00:00Z", "SUCCESS")
    fake = FakeGh(deployments=_pages([done]), compare={f"{SHA}...{PROD_HEAD}": "ahead"})
    evs, _ = deploy.poll([_sub(env="prod")], {}, {}, {}, NOW, call=fake)
    assert [e.payload["outcome"] for e in evs] == ["success"]
    with pytest.raises(events.PollError, match="workflows is empty"):
        deploy.poll([_sub()], {}, {}, {}, NOW, call=fake)


def test_the_job_is_picked_by_the_config_patterns_and_the_env_in_parentheses(deploy_cfg):
    jobs = [_job("Deploy: api (prod)", conclusion="failure"), _job("Fallback deploy (staging)", conclusion="failure"),
            _job("Build"), _job("Deploy: api (staging)", conclusion="timed_out")]
    fake = FakeGh(runs={"W1": [_run(1, conclusion="failure")], "W2": []}, views={1: _view(jobs, conclusion="failure")})
    evs, _ = deploy.poll([_sub()], {}, {}, {}, NOW, call=fake)
    assert [(e.payload["outcome"], e.payload["job"]) for e in evs] == [("failure", "Deploy: api (staging)")]
    fake = FakeGh(runs={"W1": [_run(1)], "W2": []}, views={1: _view([_job("static-deploy (dev)")])})
    evs, _ = deploy.poll([_sub(env="dev")], {}, {}, {}, NOW, call=fake)
    assert [(e.payload["outcome"], e.payload["job"]) for e in evs] == [("success", "static-deploy (dev)")]


def test_a_run_id_subscription_reads_that_run_alone(deploy_cfg):
    fake = FakeGh(views={77: _view([_job("Deploy: api (staging)", rid=77)], rid=77)})
    evs, cursor = deploy.poll([_sub(run_id="77")], {}, {}, {}, NOW, call=fake)
    assert [(a[:3]) for _, a, _ in fake.calls] == [["run", "view", "77"]]
    assert evs[0].id == "deploy:o/r:77:staging:77:d1"
    assert evs[0].match == {"repo": "o/r", "env": "staging", "run_id": "77", "sub_id": "d1"}
    assert cursor["decided"] == {"d1": 77}


def test_a_pending_run_emits_nothing(deploy_cfg):
    fake = FakeGh(runs={"W1": [_run(1, status="in_progress", conclusion="")], "W2": []},
                  views={1: _view([_job("Deploy: api (staging)", status="in_progress", conclusion="")],
                                  status="in_progress", conclusion="")})
    evs, cursor = deploy.poll([_sub()], {}, {}, {}, NOW, call=fake)
    assert evs == [] and cursor["decided"] == {}
    fake = FakeGh(runs={"W1": [_run(2, status="in_progress", conclusion="", created="2026-10-22T12:00:00Z"),
                               _run(1, conclusion="failure")], "W2": []},
                  views={1: _view([_job("Deploy: api (staging)", conclusion="failure")], conclusion="failure"),
                         2: _view([], status="in_progress", conclusion="")})
    evs, _ = deploy.poll([_sub()], {}, {}, {}, NOW, call=fake)
    assert evs == []


def test_a_missing_workflow_is_skipped_and_all_missing_fails_the_subscription(deploy_cfg):
    fake = FakeGh(runs={"W1": None, "W2": [_run(1)]}, views={1: _view([_job("Deploy: api (staging)")])})
    evs, _ = deploy.poll([_sub()], {}, {}, {}, NOW, call=fake)
    assert len(evs) == 1
    fake = FakeGh(runs={"W1": None, "W2": None})
    evs, cursor = deploy.poll([_sub()], {}, {}, {}, NOW, call=fake)
    assert evs == [] and [f[0] for f in cursor["failed"]] == ["d1"] and "no deploy workflow" in cursor["failed"][0][1]
    assert cursor["workflows"] == {"o/r": []}


def test_a_failed_run_list_of_an_existing_workflow_fails_the_subscription_instead_of_deciding_on_the_rest(deploy_cfg):
    runs = {"W1": None, "W2": [_run(2, conclusion="failure", created="2026-10-22T12:00:00Z")]}
    views = {2: _view([_job("Deploy: api (staging)", conclusion="failure", rid=2)], conclusion="failure", rid=2)}
    fake = FakeGh(runs=runs, views=views, workflows=["W1", "W2"])
    evs, cursor = deploy.poll([_sub()], {}, {}, {}, NOW, call=fake)
    assert evs == [] and cursor["decided"] == {} and [f[0] for f in cursor["failed"]] == ["d1"]
    assert "W1" in cursor["failed"][0][1] and cursor["workflows"] == {"o/r": ["W1", "W2"]}
    healthy = FakeGh(runs={"W1": [_run(1)], "W2": runs["W2"]}, views={**views, 1: _view([_job("Deploy: api (staging)")])},
                     workflows=["W1", "W2"])
    evs, cursor = deploy.poll([_sub()], {}, cursor, {}, NOW + 60, call=healthy)
    assert [e.payload["outcome"] for e in evs] == ["success"]
    assert not any(a[:2] == ["workflow", "list"] for _, a, _ in healthy.calls)
    class Dead(FakeGh):
        def __call__(self, owner, argv, timeout=None):
            self.calls.append((owner, list(argv), timeout))
            return None

    evs, cursor = deploy.poll([_sub(sub_id="d2")], {}, {}, {}, NOW, call=Dead())
    assert evs == [] and [f[0] for f in cursor["failed"]] == ["d2"] and "could not be read" in cursor["failed"][0][1]


def test_no_workflows_configured_raises_a_poll_error_naming_the_key(tmp_path, monkeypatch):
    cfg = tmp_path / "events.toml"
    cfg.write_text("[events]\nenabled = true\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    with pytest.raises(events.PollError, match=r"\[events.sources.deploy\] workflows is empty"):
        deploy.poll([_sub()], {}, {}, {}, NOW, call=FakeGh())


def test_a_second_pass_does_not_re_emit_a_decided_run(deploy_cfg):
    fake = FakeGh(runs={"W1": [_run(1)], "W2": []}, views={1: _view([_job("Deploy: api (staging)")])})
    evs, cursor = deploy.poll([_sub()], {}, {}, {}, NOW, call=fake)
    assert len(evs) == 1
    fake2 = FakeGh(runs={"W1": [_run(1)], "W2": []}, views={1: _view([_job("Deploy: api (staging)")])})
    evs, cursor = deploy.poll([_sub()], {}, cursor, {}, NOW + 60, call=fake2)
    assert evs == [] and fake2.calls == []


def test_a_failed_commit_read_fails_that_subscription_alone(deploy_cfg):
    class Dead(FakeGh):
        def __call__(self, owner, argv, timeout=None):
            self.calls.append((owner, list(argv), timeout))
            return None if "/commits/" in argv[1] else super().__call__(owner, argv, timeout)

    dead = Dead(runs={"W1": [_run(1)], "W2": []}, views={77: _view([_job("Deploy: api (staging)", rid=77)], rid=77)})
    evs, cursor = deploy.poll([_sub(), _sub(run_id="77", sub_id="d2")], {}, {}, {}, NOW, call=dead)
    assert [e.match["sub_id"] for e in evs] == ["d2"]
    assert [f[0] for f in cursor["failed"]] == ["d1"] and "commit" in cursor["failed"][0][1]
    assert cursor["commit_date"] == {} and cursor["decided"] == {"d2": 77}


def test_a_failed_compare_or_view_is_not_cached_and_the_next_pass_reads_it_again(deploy_cfg):
    runs = {"W1": [_run(1)], "W2": []}
    views = {1: _view([_job("Deploy: api (staging)", rid=1)], rid=1)}

    class Flaky(FakeGh):
        def __init__(self, drop, **kw):
            super().__init__(**kw)
            self.drop = drop

        def __call__(self, owner, argv, timeout=None):
            if self.drop in " ".join(argv):
                self.calls.append((owner, list(argv), timeout))
                return None
            return super().__call__(owner, argv, timeout)

    evs, cursor = deploy.poll([_sub()], {}, {}, {}, NOW, call=Flaky("/compare/", runs=runs, views=views))
    assert evs == [] and cursor["contains"] == {} and [f[0] for f in cursor["failed"]] == ["d1"]
    healthy = FakeGh(runs=runs, views=views)
    evs, cursor = deploy.poll([_sub()], {}, cursor, {}, NOW + 60, call=healthy)
    assert [e.payload["outcome"] for e in evs] == ["success"] and cursor["failed"] == []
    assert any("/compare/" in a[1] for _, a, _ in healthy.calls)
    evs, cursor = deploy.poll([_sub(sub_id="d3")], {}, {}, {}, NOW, call=Flaky("run view", runs=runs, views=views))
    assert evs == [] and [f[0] for f in cursor["failed"]] == ["d3"] and cursor["decided"] == {}
    evs, cursor = deploy.poll([_sub(run_id="77")], {}, {}, {}, NOW, call=FakeGh(views={}))
    assert evs == [] and [f[0] for f in cursor["failed"]] == ["d1"]


def test_a_new_subscription_on_a_decided_sha_polls_again_and_sees_the_later_run(deploy_cfg):
    runs = {"W1": [_run(1, conclusion="failure")], "W2": []}
    views = {1: _view([_job("Deploy: api (staging)", conclusion="failure", rid=1)], conclusion="failure", rid=1)}
    evs, cursor = deploy.poll([_sub()], {}, {}, {}, NOW, call=FakeGh(runs=runs, views=views))
    assert [e.payload["outcome"] for e in evs] == ["failure"] and cursor["decided"] == {"d1": 1}
    runs2 = {"W1": [_run(2, created="2026-10-22T12:00:00Z"), _run(1, conclusion="failure")], "W2": []}
    views2 = {**views, 2: _view([_job("Deploy: api (staging)", rid=2)], rid=2)}
    evs, cursor = deploy.poll([_sub(sub_id="d2")], {}, cursor, {}, NOW + 600, call=FakeGh(runs=runs2, views=views2))
    assert [(e.payload["outcome"], e.payload["run_id"], e.match["sub_id"]) for e in evs] == [("success", 2, "d2")]
    assert cursor["decided"] == {"d2": 2}


def test_cache_entries_of_subscriptions_that_are_gone_are_pruned(deploy_cfg):
    runs = {"W1": [_run(1)], "W2": []}
    views = {1: _view([_job("Deploy: api (staging)")])}
    evs, cursor = deploy.poll([_sub(), _sub(sha=OLD, sub_id="d2")], {}, {}, {}, NOW, call=FakeGh(runs=runs, views=views))
    assert set(cursor["commit_date"]) == {SHA, OLD} and set(cursor["decided"]) == {"d1", "d2"}
    evs, cursor = deploy.poll([_sub(sha=OLD, sub_id="d2")], {}, cursor, {}, NOW + 60, call=FakeGh(runs=runs, views=views))
    assert set(cursor["commit_date"]) == {OLD} and set(cursor["decided"]) == {"d2"}
    assert all(k.startswith(OLD) for k in cursor["contains"]) and set(cursor["workflows"]) == {"o/r"}
    evs, cursor = deploy.poll([_sub(sha=OLD, sub_id="d3", repo="o/other")], {}, cursor, {}, NOW + 120,
                              call=FakeGh(runs=runs, views=views))
    assert set(cursor["workflows"]) == {"o/other"}


def test_the_default_call_rounds_the_time_left_up_to_a_whole_second(monkeypatch, deploy_cfg):
    seen = []
    monkeypatch.setattr(deploy.gh_cli, "run_json", lambda owner, argv, timeout=30: seen.append(timeout))
    deploy.default_call("o", ["run", "view"], timeout=0.4)
    deploy.default_call("o", ["run", "view"], timeout=4.2)
    assert seen == [1, 5]


def test_the_budget_keeps_the_finished_subs_and_queues_the_rest(deploy_cfg, monkeypatch):
    clock = {"t": NOW}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    views = {1: _view([_job("Deploy: api (staging)")]), 2: _view([_job("Deploy: api (staging)", rid=2)], rid=2)}
    fake = FakeGh(runs={"W1": [_run(1)], "W2": [_run(2)]}, views=views, clock=clock, spend=10.0)
    subs = [_sub(sha=SHA), _sub(sha=OLD, sub_id="d2")]
    evs, cursor = deploy.poll(subs, {}, {}, {}, NOW, call=fake, until=NOW + 65)
    assert [e.match["sha"] for e in evs] == [SHA] and cursor["budget_spent"] == ["d2"]
    assert cursor["decided"] == {"d1": 1} and cursor["commit_date"][OLD] == DATE
    assert len(fake.calls) == 7
    fake2 = FakeGh(runs={"W1": [_run(1)], "W2": [_run(2)]}, views=views, clock=clock, spend=5.0)
    evs, cursor = deploy.poll(subs, {}, cursor, {}, clock["t"], call=fake2, until=clock["t"] + 30)
    assert [e.match["sha"] for e in evs] == [OLD]
    assert [a[:2] for _, a, _ in fake2.calls] == [["run", "list"], ["run", "list"], ["api", f"repos/o/r/compare/{OLD}...{HEAD}"][:2],
                                                  ["run", "view"]]
    assert cursor["budget_spent"] == [] and len(fake2.calls) == 4 and "runs" not in cursor


def test_two_environments_of_one_run_each_get_their_own_job_outcome(deploy_cfg):
    jobs = [_job("Deploy: api (staging)", rid=7), _job("Deploy: api (dev)", conclusion="failure", rid=7)]
    fake = FakeGh(runs={"W1": [_run(7)], "W2": []}, views={7: _view(jobs, rid=7)})
    evs, cursor = deploy.poll([_sub(env="dev", sub_id="d2"), _sub(env="staging")], {}, {}, {}, NOW, call=fake)
    assert sorted((e.match["env"], e.payload["outcome"], e.payload["job"]) for e in evs) == [
        ("dev", "failure", "Deploy: api (dev)"), ("staging", "success", "Deploy: api (staging)")]
    evs, cursor = deploy.poll([_sub(env="staging", sub_id="d3"), _sub(env="dev", sub_id="d4")], {}, {}, {}, NOW,
                              call=FakeGh(runs={"W1": [_run(7)], "W2": []}, views={7: _view(jobs, rid=7)}))
    assert sorted((e.match["env"], e.payload["outcome"]) for e in evs) == [("dev", "failure"), ("staging", "success")]


def test_a_changed_workflow_list_re_reads_a_cached_repo(deploy_cfg):
    fake = FakeGh(runs={"W1": [_run(1)], "W2": []}, views={1: _view([_job("Deploy: api (staging)")])})
    evs, cursor = deploy.poll([_sub()], {}, {"workflows": {"o/r": []}, "workflows_of": ["W0"]}, {}, NOW, call=fake)
    assert [e.payload["outcome"] for e in evs] == ["success"]
    assert len(fake.named("workflow", "list")) == 1 and cursor["workflows_of"] == ["W1", "W2"]
    fake = FakeGh(runs={"W1": [_run(1)], "W2": []}, views={1: _view([_job("Deploy: api (staging)")])})
    evs, _ = deploy.poll([_sub(sub_id="d2")], {}, {"workflows": {"o/r": []}}, {}, NOW, call=fake)
    assert [e.payload["outcome"] for e in evs] == ["success"]


def test_a_selector_that_never_fits_the_budget_still_finishes_over_passes(deploy_cfg, monkeypatch):
    clock = {"t": NOW}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    runs = [_run(i, created=f"2026-10-22T1{i}:00:00Z") for i in range(1, 7)]
    views = {i: _view([_job("Deploy: api (staging)", rid=i)], rid=i) for i in range(1, 7)}
    cursor = {}
    produced = []
    for _ in range(6):
        fake = FakeGh(runs={"W1": runs, "W2": []}, views=views, clock=clock, spend=4.0)
        evs, cursor = deploy.poll([_sub()], {}, cursor, {}, clock["t"], call=fake, until=clock["t"] + 30)
        produced.extend(evs)
        if produced:
            break
    assert [e.payload["run_id"] for e in produced] == [6]


def test_each_gh_call_gets_the_time_left_as_its_timeout(deploy_cfg, monkeypatch):
    clock = {"t": NOW}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    fake = FakeGh(runs={"W1": [_run(1)], "W2": []}, views={1: _view([_job("Deploy: api (staging)")])}, clock=clock,
                  spend=2.0)
    deploy.poll([_sub()], {}, {}, {}, NOW, call=fake, until=NOW + 20)
    timeouts = [t for _, _, t in fake.calls]
    assert timeouts == sorted(timeouts, reverse=True) and timeouts[0] == 20.0 and timeouts[1] == 18.0
    fake = FakeGh(runs={"W1": [_run(1)], "W2": []}, views={1: _view([_job("Deploy: api (staging)")])})
    deploy.poll([_sub()], {}, {}, {}, NOW, call=fake)
    assert {t for _, _, t in fake.calls} == {30}


def test_the_default_call_is_gh_cli_run_json(monkeypatch, deploy_cfg):
    seen = []
    monkeypatch.setattr(deploy.gh_cli, "run_json", lambda owner, argv, timeout=30: seen.append((owner, argv, timeout)))
    deploy.default_call("o", ["run", "view"], timeout=4)
    assert seen == [("o", ["run", "view"], 4)]
