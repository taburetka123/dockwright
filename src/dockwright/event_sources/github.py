from __future__ import annotations

import math
import re
from datetime import datetime, time as daytime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import config, events, gh_cli
from . import BUDGET_SPENT, FAILED, Budget, SelectorFailed, failure, ordered
from .gh_common import call_json, contains, deployments_page

DEPLOY_SCAN_PAGES = 3
PAGED_RETENTION_SEC = 30 * 86400
SEARCH_LIMIT = "100"
RUN_LIMIT = "5"
REVIEW_KIND = "review-wait"
APPROVAL_KIND = "approval-wait"
SILENT_DECISIONS = ("APPROVED", "CHANGES_REQUESTED")
OPEN_FIELDS = "repository,number,url,title,createdAt"
MERGED_FIELDS = "repository,closedAt,number"
RUN_FIELDS = "databaseId,url,createdAt,headSha,workflowName"
MERGE_FIELDS = "mergeCommit,baseRefName"
PR_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!) { repository(owner: $owner, name: $name) { "
    "pullRequest(number: $number) { state isDraft reviewDecision author { login } "
    "timelineItems(last: 1, itemTypes: [READY_FOR_REVIEW_EVENT]) { nodes { ... on ReadyForReviewEvent { createdAt } } } "
    "reviews(last: 50) { nodes { author { __typename login } state submittedAt } } } } }")


def default_call(owner: str | None, argv: list[str], timeout: float | None = None):
    return gh_cli.run_json(owner, argv, timeout=max(1, math.ceil(timeout)) if timeout else 30)


def _zone(name: str):
    return ZoneInfo(name) if name else None


def _window(window: str) -> tuple[daytime, daytime]:
    lo, hi = window.split("-")
    return daytime(int(lo[:2]), int(lo[3:])), daytime(int(hi[:2]), int(hi[3:]))


def _business_hours(start: float, now: float, window: str, tz: str) -> float:
    if now <= start:
        return 0.0
    zone = _zone(tz)
    opens, closes = _window(window)
    begin, end = datetime.fromtimestamp(start, zone), datetime.fromtimestamp(now, zone)
    total = 0.0
    day = begin.date()
    while day <= end.date():
        if day.weekday() < 5:
            lo = max(begin, datetime.combine(day, opens, zone))
            hi = min(end, datetime.combine(day, closes, zone))
            if hi > lo:
                total += (hi - lo).total_seconds() / 3600
        day += timedelta(days=1)
    return total


def _ts(iso: str) -> float:
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()


def _date(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _episode_ping(paged: dict, key: str, episode: int) -> int:
    entry = paged.get(key) or {}
    return int(entry.get("n") or 0) + 1 if entry.get("episode") == episode else 1


def _due(paged: dict, key: str, episode: int, now: float, source: dict) -> bool:
    entry = paged.get(key) or {}
    if entry.get("episode") != episode:
        return True
    return _business_hours(float(entry.get("at") or 0), now, source["work_hours"], source["tz"]) >= source["repeat_hours"]


def _logins(owners: list[str]) -> dict[str, list[str]]:
    configured = config.gh_logins() if config.gh_configured() else None
    groups: dict[str, list[str]] = {}
    for owner in owners:
        login = "all" if configured is None else (configured.get(owner) or owner)
        groups.setdefault(login, []).append(owner)
    return groups


def _repo_of(row: dict) -> str:
    return str((row.get("repository") or {}).get("nameWithOwner") or "")


def _search(call, owner, argv, until, what) -> list[dict]:
    listed = call_json(call, owner, argv, until, what)
    if not isinstance(listed, list):
        raise SelectorFailed(f"{what} could not be read")
    return [r for r in listed if isinstance(r, dict)]


def _open(call, login, owners, cursor, until) -> None:
    argv = ["search", "prs", "--author=@me", "--state=open", "--draft=false",
            *(a for o in owners for a in ("--owner", o)), "--limit", SEARCH_LIMIT, "--json", OPEN_FIELDS]
    rows = _search(call, None if login == "all" else owners[0], argv, until, f"open PRs of {login}")
    kept = [e for e in cursor["open"] if str(e.get("repo", "")).split("/")[0] not in owners]
    cursor["open"] = kept + [{"url": str(r.get("url") or ""), "title": str(r.get("title") or ""),
                              "createdAt": str(r.get("createdAt") or ""), "repo": _repo_of(r), "number": r.get("number")}
                             for r in rows if _repo_of(r) and r.get("url")]


def _merged(call, login, owners, cursor, previous: dict, now, source, until) -> None:
    since = _date(now - source["merged_window_days"] * 86400)
    argv = ["search", "prs", "--author=@me", "--merged", "--merged-at", f">={since}",
            *(a for o in owners for a in ("--owner", o)), "--limit", SEARCH_LIMIT, "--json", MERGED_FIELDS]
    rows = _search(call, None if login == "all" else owners[0], argv, until, f"merged PRs of {login}")
    by_repo: dict[str, list[list]] = {}
    for r in rows:
        repo = _repo_of(r)
        if repo and r.get("number") is not None and r.get("closedAt"):
            by_repo.setdefault(repo, []).append([int(r["number"]), str(r["closedAt"])])
    cursor["merged"] = {repo: entry for repo, entry in cursor["merged"].items() if repo.split("/")[0] not in owners}
    for repo, prs in by_repo.items():
        entry = dict(previous.get(repo) or {})
        entry["prs"] = sorted(prs, key=lambda p: p[1], reverse=True)
        listed = {p[0] for p in entry["prs"]}
        entry["skipped"] = [n for n in entry.get("skipped") or [] if n in listed]
        cursor["merged"][repo] = entry


def _default_branch(call, owner, repo, cursor, until) -> str:
    known = cursor["default_branch"]
    if repo not in known:
        info = call_json(call, owner, ["api", f"repos/{repo}"], until, f"repository {repo}")
        if not isinstance(info, dict) or not info.get("default_branch"):
            raise SelectorFailed(f"repository {repo} could not be read")
        known[repo] = str(info["default_branch"])
    return known[repo]


def _merge_of(call, owner, repo, entry: dict, cursor, until) -> tuple[str, str] | None:
    skipped = [n for n in entry.get("skipped") or [] if isinstance(n, int)]
    for number, closed in entry.get("prs") or []:
        if number in skipped:
            continue
        if entry.get("number") == number and entry.get("sha"):
            return str(entry["sha"]), str(entry.get("closedAt") or closed)
        url = f"https://github.com/{repo}/pull/{number}"
        view = call_json(call, owner, ["pr", "view", url, "--json", MERGE_FIELDS], until, f"merge of {url}")
        sha = ((view or {}).get("mergeCommit") or {}).get("oid") if isinstance(view, dict) else None
        if not sha:
            raise SelectorFailed(f"merge of {url} could not be read")
        if str(view.get("baseRefName") or "") != _default_branch(call, owner, repo, cursor, until):
            entry["skipped"] = skipped + [number]
            skipped = entry["skipped"]
            continue
        entry.update({"number": number, "closedAt": closed, "sha": str(sha)})
        return str(sha), closed
    for key in ("number", "closedAt", "sha"):
        entry.pop(key, None)
    return None


def _pr_node(call, owner, repo, number, until) -> dict | None:
    name = repo.split("/", 1)[1]
    argv = ["api", "graphql", "-f", f"query={PR_QUERY}", "-F", f"owner={owner}", "-F", f"name={name}", "-F",
            f"number={number}"]
    data = call_json(call, owner, argv, until, f"PR {repo}#{number}")
    repository = ((data.get("data") or {}).get("repository") or {}) if isinstance(data, dict) else None
    if not isinstance(repository, dict) or "pullRequest" not in repository:
        raise SelectorFailed(f"PR {repo}#{number} could not be read")
    node = repository["pullRequest"]
    return node if isinstance(node, dict) else None


def _last_human_review(node: dict, source: dict) -> str | None:
    author = str((node.get("author") or {}).get("login") or "")
    stamps = []
    for review in ((node.get("reviews") or {}).get("nodes") or []):
        who = review.get("author") or {}
        login = str(who.get("login") or "")
        if who.get("__typename") != "User" or not login or login == author or login in source["ignore_reviewers"]:
            continue
        if review.get("submittedAt"):
            stamps.append(str(review["submittedAt"]))
    return max(stamps, key=_ts) if stamps else None


def _pr(call, entry: dict, cursor, now, source, until) -> events.Event | None:
    repo, number, url = str(entry["repo"]), entry["number"], str(entry["url"])
    owner = repo.split("/")[0]
    node = _pr_node(call, owner, repo, number, until)
    if node is None or node.get("state") != "OPEN" or node.get("isDraft") or node.get("reviewDecision") in SILENT_DECISIONS:
        return None
    ready_nodes = ((node.get("timelineItems") or {}).get("nodes") or [])
    ready_at = str(ready_nodes[-1].get("createdAt")) if ready_nodes and ready_nodes[-1].get("createdAt") else str(entry["createdAt"])
    last_review = _last_human_review(node, source)
    start = max(_ts(ready_at), _ts(last_review)) if last_review else _ts(ready_at)
    hours = _business_hours(start, now, source["work_hours"], source["tz"])
    key, episode = f"{repo}#{number}", int(start)
    if hours < source["review_after_hours"] or not _due(cursor["paged"], key, episode, now, source):
        return None
    ping = _episode_ping(cursor["paged"], key, episode)
    cursor["paged"][key] = {"episode": episode, "n": ping, "at": now}
    return events.Event(f"github:{repo}:{number}:{REVIEW_KIND}:{episode}:{ping}", now, "github", REVIEW_KIND,
                        {"repo": repo, "pr": str(number)}, "",
                        f"{repo}#{number} waits for review {hours:.0f} business hours (ping {ping}) {url}",
                        {"url": url, "title": str(entry.get("title") or ""), "ready_at": ready_at,
                         "last_human_review": last_review, "hours": hours, "ping": ping})


def _newest_waiting(call, owner, repo, sha, closed, cursor, until) -> dict | None:
    argv = ["run", "list", "--repo", repo, "--status", "waiting", "--limit", RUN_LIMIT, "--json", RUN_FIELDS]
    listed = call_json(call, owner, argv, until, f"waiting runs of {repo}")
    if not isinstance(listed, list):
        raise SelectorFailed(f"waiting runs of {repo} could not be read")
    runs = [r for r in listed if isinstance(r, dict) and r.get("databaseId") is not None and r.get("headSha")
            and str(r.get("createdAt") or "") >= closed]
    runs.sort(key=lambda r: str(r.get("createdAt") or ""), reverse=True)
    return next((r for r in runs if contains(call, owner, repo, sha, str(r["headSha"]), cursor, until)), None)


def _pending(call, owner, repo, rid, source, until) -> tuple[list[str], bool, list[str]]:
    listed = call_json(call, owner, ["api", f"repos/{repo}/actions/runs/{rid}/pending_deployments"], until,
                       f"pending deployments of run {rid}")
    if not isinstance(listed, list):
        raise SelectorFailed(f"pending deployments of run {rid} could not be read")
    named = [(str((p.get("environment") or {}).get("name")), p) for p in listed if isinstance(p, dict)]
    order = source["envs"]
    waiting = [p for name, p in named if name in order]
    if not waiting:
        order = source["fallback_envs"]
        waiting = [p for name, p in named if name in order]
    envs = [e for e in order if any(name == e for name, _ in named)]
    reviewers = []
    for p in waiting:
        for r in p.get("reviewers") or []:
            who = (r.get("reviewer") or {}) if isinstance(r, dict) else {}
            name = str(who.get("slug") or who.get("login") or "")
            if name and name not in reviewers:
                reviewers.append(name)
    return envs, any(bool(p.get("current_user_can_approve")) for p in waiting), reviewers


def _fallback_only(envs: list[str], source: dict) -> bool:
    return bool(envs) and not any(e in source["envs"] for e in envs)


def _jobs(call, owner, repo, rid, until) -> list[dict]:
    what = f"jobs of run {rid}"
    view = call_json(call, owner, ["run", "view", str(rid), "--repo", repo, "--json", "jobs"], until, what)
    jobs = view.get("jobs") if isinstance(view, dict) else None
    if not isinstance(jobs, list):
        raise SelectorFailed(f"{what} could not be read")
    return [j for j in jobs if isinstance(j, dict)]


def _prod_job_skipped(call, owner, repo, rid, source, until) -> bool:
    pattern = source["fallback_skipped_job"]
    if not pattern:
        return False
    matched = [job for job in _jobs(call, owner, repo, rid, until) if re.search(pattern, str(job.get("name") or ""))]
    return bool(matched) and all(job.get("status") == "completed" and job.get("conclusion") == "skipped" for job in matched)


def _prod_has(call, owner, repo, base_sha, run_head, rid, envs, source, cursor, entry: dict | None, now, until):
    deployed_envs = source["deployed_envs"]
    all_envs = list(deployed_envs) + [e for e in envs if e not in deployed_envs]
    decided: dict[str, bool] = dict(entry.get("decided") or {}) if entry else {}
    after = entry.get("after") if entry else None
    waiting_since = entry.get("waiting_since") if entry else None
    scanned = int(entry.get("scanned") or 0) if entry else 0
    for _ in range(DEPLOY_SCAN_PAGES):
        if all(e in decided for e in deployed_envs):
            break
        nodes, more, after = deployments_page(call, owner, repo, all_envs, after, until)
        scanned += len(nodes)
        for node in nodes:
            env, sha = str(node.get("environment") or ""), str(node.get("commitOid") or "")
            state = str((node.get("latestStatus") or {}).get("state") or "")
            if waiting_since is None and env in envs and sha == run_head and state == "WAITING" and node.get("createdAt"):
                waiting_since = str(node["createdAt"])
            if env in deployed_envs and env not in decided and state == "SUCCESS":
                decided[env] = contains(call, owner, repo, base_sha, sha, cursor, until)
                if decided[env]:
                    return "deployed", None, waiting_since
        if not more:
            for env in deployed_envs:
                decided.setdefault(env, False)
            break
    if all(e in decided for e in deployed_envs):
        return "not-deployed", None, waiting_since
    return "undecided", {"run": rid, "after": after, "waiting_since": waiting_since, "decided": decided, "at": now,
                         "scanned": scanned, "envs": envs}, waiting_since


def _run(call, repo, cursor, now, source, until) -> events.Event | None:
    owner = repo.split("/")[0]
    entry = cursor["merged"][repo]
    merge = _merge_of(call, owner, repo, entry, cursor, until)
    if merge is None:
        cursor["deploy_scan"].pop(repo, None)
        return None
    sha, closed = merge
    run = _newest_waiting(call, owner, repo, sha, closed, cursor, until)
    if run is None:
        cursor["deploy_scan"].pop(repo, None)
        return None
    rid, head, url = run["databaseId"], str(run["headSha"]), str(run.get("url") or "")
    envs, can_approve, reviewers = _pending(call, owner, repo, rid, source, until)
    if not envs:
        cursor["deploy_scan"].pop(repo, None)
        return None
    scan = cursor["deploy_scan"].get(repo)
    wait_envs = envs if _fallback_only(envs, source) else source["envs"]
    scan = scan if isinstance(scan, dict) and scan.get("run") == rid and scan.get("envs") == wait_envs else None
    verdict, scan, waiting_since = _prod_has(call, owner, repo, sha, head, rid, wait_envs, source, cursor, scan, now, until)
    if scan is None:
        cursor["deploy_scan"].pop(repo, None)
    else:
        cursor["deploy_scan"][repo] = scan
    if verdict != "not-deployed":
        return None
    waiting_since = waiting_since or str(run.get("createdAt") or "")
    hours = _business_hours(_ts(waiting_since), now, source["work_hours"], source["tz"])
    key, episode = f"{repo}:run:{rid}", int(_ts(waiting_since))
    if hours < source["approval_after_hours"] or not _due(cursor["paged"], key, episode, now, source):
        return None
    if _fallback_only(envs, source) and not _prod_job_skipped(call, owner, repo, rid, source, until):
        return None
    ping = _episode_ping(cursor["paged"], key, episode)
    cursor["paged"][key] = {"episode": episode, "n": ping, "at": now}
    note = ", you can approve" if can_approve else ""
    return events.Event(f"github:{repo}:run:{rid}:{APPROVAL_KIND}:{episode}:{ping}", now, "github", APPROVAL_KIND,
                        {"repo": repo, "run_id": str(rid)}, "",
                        f"{repo} run {rid} waits for {'/'.join(envs)} approval {hours:.0f} business hours (ping {ping}{note}) {url}",
                        {"url": url, "envs": envs, "can_approve": can_approve, "reviewers": reviewers,
                         "waiting_since": waiting_since, "hours": hours, "ping": ping})


def _prune(cursor: dict, now: float, owners: list[str]) -> None:
    cursor["open"] = [e for e in cursor["open"] if str(e.get("repo", "")).split("/")[0] in owners]
    cursor["merged"] = {k: v for k, v in cursor["merged"].items() if k.split("/")[0] in owners}
    cursor["paged"] = {k: v for k, v in cursor["paged"].items()
                       if isinstance(v, dict) and now - float(v.get("at") or 0) < PAGED_RETENTION_SEC}
    bases = {str(e.get("sha")) for e in cursor["merged"].values() if isinstance(e, dict) and e.get("sha")}
    cursor["contains"] = {k: v for k, v in cursor["contains"].items() if k.split(":")[0] in bases}
    cursor["deploy_scan"] = {k: v for k, v in cursor["deploy_scan"].items() if k in cursor["merged"]}


def _rules(rules: dict[str, dict], kind: str) -> list[dict]:
    return [r for r in rules.values()
            if r.get("source") == "github" and r.get("kind") == kind and r.get("enabled") and not r.get("errors")]


def poll(subs: list[events.Sub], rules: dict[str, dict], cursor: dict, env: dict[str, str], now: float, call=None,
         until: float | None = None) -> tuple[list[events.Event], dict]:
    call = call or default_call
    source = config.events_source("github")
    if not source["owners"]:
        raise events.PollError("[events.sources.github] owners is empty: name the GitHub owners to watch")
    review, approval = bool(_rules(rules, REVIEW_KIND)), bool(_rules(rules, APPROVAL_KIND))
    new_cursor = {k: v for k, v in cursor.items() if k not in (BUDGET_SPENT, FAILED)}
    for key in ("merged", "paged", "deploy_scan", "contains", "default_branch"):
        new_cursor[key] = {k: v for k, v in (cursor.get(key) or {}).items()} if isinstance(cursor.get(key), dict) else {}
    new_cursor["merged"] = {k: dict(v) for k, v in new_cursor["merged"].items() if isinstance(v, dict)}
    new_cursor["open"] = [dict(e) for e in (cursor.get("open") or []) if isinstance(e, dict)]
    _prune(new_cursor, now, source["owners"])
    previous_merged = {k: dict(v) for k, v in new_cursor["merged"].items()}
    logins = _logins(source["owners"])
    lists = ([f"open:{login}" for login in logins] if review else []) + \
            ([f"merged:{login}" for login in logins] if approval else [])
    out: list[events.Event] = []
    left: list[str] = []
    failed: list[list] = []

    def run_phase(selectors: list[str]) -> bool:
        for i, selector in enumerate(selectors):
            kind, _, target = selector.partition(":")
            try:
                if kind == "open":
                    _open(call, target, logins[target], new_cursor, until)
                elif kind == "merged":
                    _merged(call, target, logins[target], new_cursor, previous_merged, now, source, until)
                elif kind == "pr":
                    entry = next(e for e in new_cursor["open"] if e.get("url") == target)
                    event = _pr(call, entry, new_cursor, now, source, until)
                    if event is not None:
                        out.append(event)
                else:
                    event = _run(call, target, new_cursor, now, source, until)
                    if event is not None:
                        out.append(event)
            except Budget:
                left.extend(selectors[i:])
                return False
            except SelectorFailed as err:
                failed.append(failure(selector, f"github {selector}: {err}"))
        return True

    fits = run_phase(ordered(lists, cursor))
    items = ([f"pr:{e['url']}" for e in new_cursor["open"] if e.get("url")] if review else []) + \
            ([f"run:{repo}" for repo in sorted(new_cursor["merged"])] if approval else [])
    items = ordered(items, cursor)
    if fits:
        run_phase(items)
    else:
        left.extend(items)
    new_cursor[BUDGET_SPENT] = left
    new_cursor[FAILED] = failed
    return out, new_cursor
