from __future__ import annotations

from . import Budget, SelectorFailed, call_timeout, spent

CONTAINS = ("ahead", "identical")
DEPLOYMENTS_PAGE = 100
DEPLOYMENTS_QUERY = (
    "query($owner: String!, $name: String!, $envs: [String!]!, $after: String) { repository(owner: $owner, name: $name) { "
    f"deployments(environments: $envs, first: {DEPLOYMENTS_PAGE}, after: $after, "
    "orderBy: {field: CREATED_AT, direction: DESC}) { pageInfo { hasNextPage endCursor } "
    "nodes { environment commitOid createdAt latestStatus { state createdAt logUrl } } } } }")


def call_json(call, owner, argv, until, what: str | None = None):
    if spent(until):
        raise Budget()
    result = call(owner, argv, timeout=call_timeout(until))
    if result is None:
        if spent(until):
            raise Budget()
        if what:
            raise SelectorFailed(f"{what} could not be read")
    return result


def contains(call, owner, repo, base: str, head: str, cursor: dict, until) -> bool:
    cache = cursor.setdefault("contains", {})
    key = f"{base}:{head}"
    if key not in cache:
        cmp = call_json(call, owner, ["api", f"repos/{repo}/compare/{base}...{head}"], until, f"compare {base}...{head}")
        if not isinstance(cmp, dict) or not cmp.get("status"):
            raise SelectorFailed(f"compare {base}...{head} could not be read")
        cache[key] = cmp["status"] in CONTAINS
    return cache[key]


def deployments_page(call, owner, repo, envs, after, until) -> tuple[list[dict], bool, str | None]:
    name = repo.split("/", 1)[1]
    argv = ["api", "graphql", "-f", f"query={DEPLOYMENTS_QUERY}", "-F", f"owner={owner}", "-F", f"name={name}",
            *(a for e in envs for a in ("-F", f"envs[]={e}")), *(("-F", f"after={after}") if after else ())]
    data = call_json(call, owner, argv, until, f"deployments of {repo}")
    page = ((data.get("data") or {}).get("repository") or {}).get("deployments") if isinstance(data, dict) else None
    if not isinstance(page, dict) or not isinstance(page.get("nodes"), list):
        raise SelectorFailed(f"deployments of {repo} could not be read")
    info = page.get("pageInfo") or {}
    nodes = [n for n in page["nodes"] if isinstance(n, dict)]
    return nodes, bool(info.get("hasNextPage")), (str(info["endCursor"]) if info.get("endCursor") else None)
