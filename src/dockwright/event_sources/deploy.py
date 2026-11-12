from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

from .. import config, events, gh_cli
from . import BUDGET_SPENT, FAILED, Budget, SelectorFailed, failure, ordered
from . import gh_common
from .gh_common import CONTAINS, call_json as _call

RUN_FIELDS = "databaseId,headSha,status,conclusion,url,createdAt"
VIEW_FIELDS = "status,conclusion,url,jobs"
WINDOW_BEFORE = timedelta(hours=1)
LIST_LIMIT = "10"
PROD_SCAN_PAGES = 3
BAD = {"failure": "failure", "timed_out": "failure"}


def default_call(owner: str | None, argv: list[str], timeout: float | None = None):
    return gh_cli.run_json(owner, argv, timeout=max(1, math.ceil(timeout)) if timeout else 30)


def _word(conclusion: str) -> str:
    return "success" if conclusion == "success" else BAD.get(conclusion, "cancelled")


def _window(date: str) -> str:
    start = datetime.fromisoformat(date.replace("Z", "+00:00")).astimezone(timezone.utc) - WINDOW_BEFORE
    return start.strftime("%Y-%m-%dT%H:%M:%SZ")


def _pick_job(jobs: list[dict], env: str, source: dict) -> dict | None:
    match, skip = source["job_match"], source["job_skip"]
    for job in jobs:
        name = str(job.get("name") or "")
        if match and not re.search(match, name):
            continue
        if skip and re.search(skip, name):
            continue
        if f"({env}" in name:
            return job
    return None


def _decide(view: dict, run: dict, env: str, source: dict) -> tuple[str, str, str, str] | None:
    job = _pick_job(view.get("jobs") or [], env, source)
    if job is not None:
        if job.get("status") != "completed":
            return None
        return (_word(str(job.get("conclusion") or "")), str(job.get("completedAt") or ""),
                str(job.get("url") or ""), str(job.get("name") or ""))
    if (view.get("status") or run.get("status")) != "completed":
        return None
    conclusion = str(view.get("conclusion") or run.get("conclusion") or "")
    url = str(view.get("url") or run.get("url") or "")
    when = str(run.get("createdAt") or "")
    return ("nojob" if conclusion == "success" else _word(conclusion), when, url, "")


def _workflows(call, owner, repo, cursor, until, source) -> list[str]:
    known = cursor.setdefault("workflows", {})
    if repo not in known:
        listed = _call(call, owner, ["workflow", "list", "--repo", repo, "--json", "name"], until, f"workflows of {repo}")
        if not isinstance(listed, list):
            raise SelectorFailed(f"workflows of {repo} could not be read")
        names = {str(w.get("name") or "") for w in listed if isinstance(w, dict)}
        known[repo] = [w for w in source["workflows"] if w in names]
    if not known[repo]:
        raise SelectorFailed(f"no deploy workflow among {', '.join(source['workflows'])} exists in {repo}")
    return known[repo]


def _containing_runs(call, owner, repo, sha, cursor, until, source) -> list[dict]:
    dates = cursor.setdefault("commit_date", {})
    if sha not in dates:
        commit = _call(call, owner, ["api", f"repos/{repo}/commits/{sha}"], until, f"commit {sha}")
        date = ((commit or {}).get("commit") or {}).get("committer", {}).get("date") if isinstance(commit, dict) else None
        if not date:
            raise SelectorFailed(f"commit {sha} could not be read")
        dates[sha] = date
    window = _window(dates[sha])
    runs: list[dict] = []
    for workflow in _workflows(call, owner, repo, cursor, until, source):
        listed = _call(call, owner, ["run", "list", "--repo", repo, "--workflow", workflow, "--created", f">={window}",
                                     "--limit", LIST_LIMIT, "--json", RUN_FIELDS], until, f"runs of {workflow}")
        if not isinstance(listed, list):
            raise SelectorFailed(f"runs of {workflow} could not be read")
        runs.extend(r for r in listed if isinstance(r, dict))
    runs.sort(key=lambda r: str(r.get("createdAt") or ""), reverse=True)
    cursor.setdefault("contains", {})
    return [run for run in runs
            if gh_common.contains(call, owner, repo, sha, str(run.get("headSha") or ""), cursor, until)]


def _view(call, owner, repo, rid, until) -> dict:
    view = _call(call, owner, ["run", "view", str(rid), "--repo", repo, "--json", VIEW_FIELDS], until, f"run {rid}")
    if not isinstance(view, dict):
        raise SelectorFailed(f"run {rid} could not be read")
    return view


def _outcome_for_sha(call, owner, repo, sha, env, cursor, until, source):
    first_bad = None
    for run in _containing_runs(call, owner, repo, sha, cursor, until, source):
        rid = run.get("databaseId")
        decided = _decide(_view(call, owner, repo, rid, until), run, env, source)
        if decided is None:
            if first_bad is None:
                first_bad = "blocked"
            continue
        if decided[0] == "success":
            return rid, decided
        if first_bad is None:
            first_bad = (rid, decided)
    if first_bad is not None and first_bad != "blocked":
        return first_bad
    return None


def _outcome_for_run(call, owner, repo, run_id, env, until, source):
    view = _view(call, owner, repo, run_id, until)
    decided = _decide(view, {"status": view.get("status"), "conclusion": view.get("conclusion"),
                             "url": view.get("url")}, env, source)
    return None if decided is None else (int(run_id) if str(run_id).isdigit() else run_id, decided)


def _prod_pages(call, owner, repo, envs, cursor, until):
    cache = cursor["deployments"].setdefault(repo, {"pages": [], "after": None, "more": True})
    yield from list(cache["pages"])
    while cache["more"] and len(cache["pages"]) < PROD_SCAN_PAGES:
        nodes, more, after = gh_common.deployments_page(call, owner, repo, envs, cache["after"], until)
        cache["pages"].append(nodes)
        cache["after"], cache["more"] = after, more
        yield nodes


def _success_time(node: dict) -> str:
    status = node.get("latestStatus") or {}
    return str(status.get("createdAt") or node.get("createdAt") or "")


def _outcome_for_prod(call, owner, repo, sha, cursor, until, prod_envs):
    newest: dict[str, dict] = {}
    for nodes in _prod_pages(call, owner, repo, list(prod_envs), cursor, until):
        for node in nodes:
            env = str(node.get("environment") or "")
            if env not in prod_envs or str((node.get("latestStatus") or {}).get("state") or "") != "SUCCESS":
                continue
            if env not in newest or _success_time(node) > _success_time(newest[env]):
                newest[env] = node
        if all(e in newest for e in prod_envs):
            break
    for env, node in newest.items():
        head = str(node.get("commitOid") or "")
        if gh_common.contains(call, owner, repo, sha, head, cursor, until):
            url = str((node.get("latestStatus") or {}).get("logUrl") or f"https://github.com/{repo}/commit/{head}")
            return head[:12], ("success", _success_time(node), url, env)
    return None


def _prune(cursor: dict, subs: dict[str, events.Sub]) -> None:
    shas = {s.match.get("sha") for s in subs.values() if s.match.get("sha")}
    repos = {str(s.match.get("repo")) for s in subs.values()}
    cursor["decided"] = {k: v for k, v in cursor["decided"].items() if k in subs}
    cursor["commit_date"] = {k: v for k, v in cursor["commit_date"].items() if k in shas}
    cursor["contains"] = {k: v for k, v in cursor["contains"].items() if k.split(":")[0] in shas}
    cursor["workflows"] = {k: v for k, v in cursor["workflows"].items() if k in repos}


def poll(subs: list[events.Sub], rules: dict[str, dict], cursor: dict, env: dict[str, str], now: float, call=None,
         until: float | None = None) -> tuple[list[events.Event], dict]:
    call = call or default_call
    source = config.events_source("deploy")
    by_id: dict[str, events.Sub] = {s.sub_id: s for s in subs if s.source == "deploy"}
    if not source["workflows"] and any(s.match.get("env") != "prod" for s in by_id.values()):
        raise events.PollError("[events.sources.deploy] workflows is empty: name the deploy workflows there")
    prod_envs = config.events_source("github")["deployed_envs"]
    new_cursor = {k: v for k, v in cursor.items() if k not in (BUDGET_SPENT, FAILED)}
    for key in ("decided", "commit_date", "contains", "workflows"):
        new_cursor[key] = dict(cursor.get(key) or {})
    if cursor.get("workflows_of") != source["workflows"]:
        new_cursor["workflows"] = {}
    new_cursor["workflows_of"] = list(source["workflows"])
    _prune(new_cursor, by_id)
    new_cursor["deployments"] = {}
    selectors = ordered([k for k in sorted(by_id) if k not in new_cursor["decided"]], cursor)
    out: list[events.Event] = []
    left: list[str] = []
    failed: list[list] = []
    for i, key in enumerate(selectors):
        sub = by_id[key]
        repo, envname = str(sub.match.get("repo")), str(sub.match.get("env"))
        owner = repo.split("/")[0]
        try:
            if envname == "prod":
                found = _outcome_for_prod(call, owner, repo, sub.match["sha"], new_cursor, until, prod_envs)
            elif sub.match.get("run_id"):
                found = _outcome_for_run(call, owner, repo, sub.match["run_id"], envname, until, source)
            else:
                found = _outcome_for_sha(call, owner, repo, sub.match["sha"], envname, new_cursor, until, source)
        except Budget:
            left = selectors[i:]
            break
        except SelectorFailed as err:
            failed.append(failure(key, f"deploy {repo}: {err}"))
            continue
        if found is None:
            continue
        rid, (outcome, when, url, job) = found
        target = sub.match.get("sha") or sub.match.get("run_id")
        out.append(events.Event(f"deploy:{repo}:{target}:{envname}:{rid}:{sub.sub_id}", now, "deploy", "job",
                                {**sub.match, "sub_id": sub.sub_id}, str(rid), f"{repo} {envname} {outcome} {when} {url}",
                                {"outcome": outcome, "run_id": None if envname == "prod" else rid, "job": job, "url": url}))
        new_cursor["decided"][key] = rid
    del new_cursor["deployments"]
    new_cursor[BUDGET_SPENT] = left
    new_cursor[FAILED] = failed
    return out, new_cursor
