from __future__ import annotations

import base64
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from .. import config, events
from . import BUDGET_SPENT, FAILED, Budget, SelectorFailed, call_timeout, failure, ordered, spent

PAGE = 100
SPRINT_PAGE_CAP = 20
SUMMARY_CHARS = 200
SPRINT_KIND = "sprint-assigned"
SPRINT_CURSOR = "sprint"
SPRINT_SELECTOR = "rule:sprint"
SPRINT_LAST = "sprint_last"
SPRINT_FIELDS = "summary,issuetype,status,parent,subtasks,assignee"
CHILD_FIELDS = "parent,assignee,status"
_FAILS = (urllib.error.URLError, OSError, ValueError, http.client.HTTPException, KeyError, TypeError)
_BODY = (KeyError, TypeError, ValueError, AttributeError)


def default_call(method: str, url: str, env: dict[str, str], timeout: float | None = None):
    cred = base64.b64encode(f"{env['TRACKER_EMAIL']}:{env['TRACKER_API_TOKEN']}".encode()).decode()
    req = urllib.request.Request(url, method=method,
                                 headers={"Authorization": "Basic " + cred, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def text_of(body) -> str:
    if isinstance(body, str):
        return body
    out: list[str] = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "text" and isinstance(n.get("text"), str):
                out.append(n["text"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(body)
    return " ".join(out)


def _created_ts(created: str, now: float) -> float:
    try:
        return datetime.strptime(created, "%Y-%m-%dT%H:%M:%S.%f%z").timestamp()
    except (ValueError, TypeError):
        try:
            return datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError, AttributeError):
            return now


def _key_num(value: str) -> int:
    return int(value) if value.isdigit() else -1


def _comment_events(base: str, key: str, floor: str, comments: list[dict], now: float) -> list[events.Event]:
    out = []
    for c in comments:
        cid = str(c.get("id") or "")
        if not cid.isdigit() or _key_num(cid) <= _key_num(floor):
            continue
        author = c.get("author") or {}
        text = text_of(c.get("body"))
        out.append(events.Event(
            f"tracker:{key}:comment:{cid}", _created_ts(str(c.get("created") or ""), now), "tracker", "comment",
            {"issue": key}, cid, f"{key} #{cid} by {author.get('displayName', '?')}: {text[:SUMMARY_CHARS]}",
            {"author_id": str(author.get("accountId") or ""), "author": str(author.get("displayName") or ""),
             "created": str(c.get("created") or ""), "url": f"{base}/browse/{key}?focusedCommentId={cid}"}))
    return out


def _wants_status(subs: list[events.Sub]) -> bool:
    return any(s.kinds is None or "status" in s.kinds for s in subs)


def _wants_comments(subs: list[events.Sub]) -> bool:
    return any(s.kinds is None or "comment" in s.kinds for s in subs)


def _get(call, url, env, until):
    if spent(until):
        raise Budget()
    try:
        return call("GET", url, env, timeout=call_timeout(until))
    except _FAILS as err:
        if spent(until):
            raise Budget() from err
        raise SelectorFailed(err) from err


def _status_due(subs: list[events.Sub], previous, current, seen) -> bool:
    if previous is None or list(previous) != current:
        return True
    return any(s.since > float(seen) for s in subs if s.kinds is None or "status" in s.kinds) if seen is not None else True


def _issue(call, base, env, key, subs, cursor, now, until) -> tuple[list[events.Event], dict]:
    out: list[events.Event] = []
    patch: dict = {}
    if _wants_comments(subs):
        floor = min((s.after for s in subs if s.after), key=_key_num, default="0")
        start, total, comments = 0, None, []
        while total is None or start < total:
            page = _get(call, f"{base}/rest/api/3/issue/{key}/comment?startAt={start}&maxResults={PAGE}"
                        "&orderBy=created", env, until)
            batch = page.get("comments") or []
            comments.extend(batch)
            total = int(page.get("total") or 0)
            start += PAGE
            if not batch:
                break
        out.extend(_comment_events(base, key, floor, comments, now))
        ids = [str(c.get("id")) for c in comments if str(c.get("id") or "").isdigit()]
        last = max(ids + [cursor.get("comments", {}).get(key, "0")], key=_key_num)
        patch["comments"] = last
    if _wants_status(subs):
        issue = _get(call, f"{base}/rest/api/3/issue/{key}?fields=status", env, until)
        status = (issue.get("fields") or {}).get("status") or {}
        current = [str(status.get("name") or ""), str((status.get("statusCategory") or {}).get("key") or "")]
        previous = cursor.get("status", {}).get(key)
        seen = (cursor.get("status_seen") or {}).get(key)
        if _status_due(subs, previous, current, seen):
            out.append(events.Event(f"tracker:{key}:status:{current[1]}:{now:.0f}", now, "tracker", "status",
                                    {"issue": key}, "", f"{key} status {current[0]} ({current[1]})",
                                    {"name": current[0], "category": current[1]}))
            patch["status_seen"] = now
        patch["status"] = current
    return out, patch


def _search(call, base: str, env: dict[str, str], jql: str, fields: str, until: float | None) -> list[dict]:
    issues: list[dict] = []
    token = None
    for _ in range(SPRINT_PAGE_CAP):
        query = {"jql": jql, "fields": fields, "maxResults": str(PAGE)}
        if token:
            query["nextPageToken"] = token
        page = _get(call, f"{base}/rest/api/3/search/jql?{urllib.parse.urlencode(query)}", env, until)
        issues.extend(page.get("issues") or [])
        token = page.get("nextPageToken")
        if not token:
            return issues
    raise SelectorFailed(f"sprint search did not end after {SPRINT_PAGE_CAP} pages")


def _jql(rule: dict) -> str:
    clauses = ["sprint in openSprints()", "assignee = currentUser()"]
    if rule.get("types"):
        clauses.append("(issuetype in (" + ", ".join(f'"{t}"' for t in rule["types"])
                       + ") OR issuetype in subTaskIssueTypes())")
    if rule.get("skip_status"):
        clauses.append("status not in (" + ", ".join(f'"{s}"' for s in rule["skip_status"]) + ")")
    return " AND ".join(clauses)


def _key(issue: dict) -> str:
    return str(issue.get("key") or "")


def _name(fields: dict, field: str) -> str:
    return str((fields.get(field) or {}).get("name") or "")


def _account(fields: dict) -> str:
    return str((fields.get("assignee") or {}).get("accountId") or "")


def _parent_of(fields: dict) -> str:
    if not (fields.get("issuetype") or {}).get("subtask"):
        return ""
    return str((fields.get("parent") or {}).get("key") or "")


def _parent_name(fields: dict, field: str) -> str:
    return _name((fields.get("parent") or {}).get("fields") or {}, field)


def _parent_typed(rule: dict, fields: dict) -> bool:
    return not rule.get("types") or _parent_name(fields, "issuetype") in rule["types"]


def _parent_skipped(rule: dict, fields: dict) -> bool:
    return _parent_name(fields, "status") in (rule.get("skip_status") or [])


def _live_status(call, base: str, env: dict[str, str], key: str) -> tuple[str, str] | None:
    try:
        body = call("GET", f"{base}/rest/api/3/issue/{key}?fields=status", env, timeout=call_timeout(None))
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise SelectorFailed(err) from err
    except _FAILS as err:
        raise SelectorFailed(err) from err
    try:
        status = (body.get("fields") or {}).get("status") or {}
        return str(status.get("name") or ""), str((status.get("statusCategory") or {}).get("key") or "")
    except _BODY as err:
        raise SelectorFailed(err) from err


def start_blocker(rule: dict, issue: str, subtasks: list[str], env: dict[str, str], call=None) -> str:
    call = call or default_call
    base = str(env.get("TRACKER_URL") or "").rstrip("/")
    skipped = rule.get("skip_status") or []
    found = _live_status(call, base, env, issue)
    if found is None:
        return f"{issue} was not found"
    if found[0] in skipped:
        return f"{issue} is in {found[0]}"
    if found[1] != "new":
        return f"{issue} is past To Do"
    for key in subtasks:
        found = _live_status(call, base, env, key)
        if found is not None and found[1] != "new" and found[0] not in skipped:
            return f"{key} is past To Do"
    return ""


def sprint_keys(cursor: dict | None) -> list[str]:
    value = cursor.get(SPRINT_CURSOR) if isinstance(cursor, dict) else None
    shown = value.get("shown") if isinstance(value, dict) else None
    return [str(k) for k in shown] if isinstance(shown, list) else []


def _owners(children: list[dict], me: str) -> tuple[dict[str, list[str]], set[str]]:
    mine: dict[str, list[str]] = {}
    foreign: set[str] = set()
    for child in children:
        fields = child.get("fields") or {}
        parent = str((fields.get("parent") or {}).get("key") or "")
        account = _account(fields)
        if account and account == me:
            keys = mine.setdefault(parent, [])
            if _key(child) not in keys:
                keys.append(_key(child))
        elif account or ((fields.get("status") or {}).get("statusCategory") or {}).get("key") != "done":
            foreign.add(parent)
    return mine, foreign


def _units(found: list[tuple[dict, dict]], parents: set[str], mine: dict[str, list[str]],
           foreign: set[str]) -> list[dict]:
    keys = {_key(issue) for _, issue in found}
    fallback: dict[str, list[dict]] = {}
    units: list[dict] = []
    done: set[str] = set()
    for rule, issue in found:
        key = _key(issue)
        if not key or key in done:
            continue
        fields = issue.get("fields") or {}
        parent = _parent_of(fields)
        if parent and (not _parent_typed(rule, fields) or _parent_skipped(rule, fields)):
            continue
        done.add(key)
        if parent:
            unit = {"key": key, "fields": fields, "parent": parent, "cover": [key]}
            if parent in foreign or parent not in keys:
                units.append(unit)
            else:
                fallback.setdefault(parent, []).append(unit)
        elif key in parents:
            if key not in foreign:
                subtasks = mine.get(key, [])
                units.append({"key": key, "fields": fields, "parent": "", "subtasks": subtasks,
                              "cover": [key] + subtasks, "fallback": fallback.setdefault(key, [])})
        else:
            units.append({"key": key, "fields": fields, "parent": "", "cover": [key]})
    return units


def _sprint_event(base: str, unit: dict, now: float) -> events.Event:
    key, fields = unit["key"], unit["fields"]
    itype, status = _name(fields, "issuetype"), _name(fields, "status")
    payload = {"url": f"{base}/browse/{key}", "type": itype, "status": status}
    note = ""
    if unit["parent"]:
        note = f", sub-task of {unit['parent']}"
        payload["parent"] = unit["parent"]
    elif unit.get("subtasks"):
        note = ", sub-tasks " + ", ".join(unit["subtasks"])
        payload["subtasks"] = list(unit["subtasks"])
    return events.Event(f"tracker:{key}:{SPRINT_KIND}", now, "tracker", SPRINT_KIND, {"issue": key}, "",
                        f"{key} {fields.get('summary', '')} ({itype}, {status}{note})", payload)


def _sprint(call, base, env, rules, cursor, now, until) -> tuple[list[events.Event], dict]:
    found: list[tuple[dict, dict]] = []
    for rule in rules:
        found.extend((rule, issue) for issue in _search(call, base, env, _jql(rule), SPRINT_FIELDS, until))
    me = next((a for a in (_account(issue.get("fields") or {}) for _, issue in found) if a), "")
    parents: set[str] = set()
    for rule, issue in found:
        fields = issue.get("fields") or {}
        parent = _parent_of(fields)
        if parent and _parent_typed(rule, fields):
            parents.add(parent)
        elif not parent and fields.get("subtasks"):
            parents.add(_key(issue))
    parents.discard("")
    children: list[dict] = []
    if parents:
        children = _search(call, base, env, "parent in (" + ", ".join(sorted(parents))
                           + ") AND issuetype in subTaskIssueTypes()", CHILD_FIELDS, until)
    mine, foreign = _owners(children, me)
    units = _units(found, parents, mine, foreign)
    shown = _distinct(_key(issue) for _, issue in found)
    previous = cursor.get(SPRINT_CURSOR)
    if previous is None:
        return [], {"keys": _distinct(k for unit in units for k in unit["cover"]), "shown": shown}
    prev = [str(k) for k in previous.get("keys") or []]
    started = set(prev)
    emit: list[dict] = []
    for unit in units:
        if unit["parent"] and unit["parent"] in started:
            continue
        if not started.intersection(unit["cover"]):
            emit.append(unit)
        elif "fallback" in unit and unit["key"] not in started:
            emit.extend(u for u in unit["fallback"] if u["key"] not in started)
    visible = {_key(issue) for _, issue in found} | {_key(child) for child in children} | parents
    keys = _distinct([k for k in prev if k in visible] + [k for unit in emit for k in unit["cover"]])
    return [_sprint_event(base, unit, now) for unit in emit], {"keys": keys, "shown": shown}


def _distinct(keys) -> list[str]:
    out: list[str] = []
    for key in keys:
        if key and key not in out:
            out.append(key)
    return out


def _sprint_due(cursor: dict, now: float) -> bool:
    if SPRINT_SELECTOR in (cursor.get(BUDGET_SPENT) or []):
        return True
    last = cursor.get(SPRINT_LAST)
    return (not isinstance(last, (int, float)) or isinstance(last, bool)
            or now - last >= config.events_source("tracker")["sprint_every_sec"])


def poll(subs: list[events.Sub], rules: dict[str, dict], cursor: dict, env: dict[str, str], now: float, call=None,
         until: float | None = None) -> tuple[list[events.Event], dict]:
    call = call or default_call
    base = str(env.get("TRACKER_URL") or "").rstrip("/")
    by_issue: dict[str, list[events.Sub]] = {}
    for s in subs:
        if s.source == "tracker" and s.match.get("issue"):
            by_issue.setdefault(s.match["issue"], []).append(s)
    sprint_rules = [r for r in rules.values()
                    if r.get("source") == "tracker" and r.get("kind") == SPRINT_KIND and r.get("enabled")
                    and not r.get("errors")]
    selectors = ordered(sorted(by_issue) + ([SPRINT_SELECTOR] if sprint_rules and _sprint_due(cursor, now) else []),
                        cursor)
    new_cursor = {k: v for k, v in cursor.items() if k not in (BUDGET_SPENT, FAILED)}
    new_cursor.setdefault("comments", dict(cursor.get("comments") or {}))
    new_cursor.setdefault("status", dict(cursor.get("status") or {}))
    new_cursor.setdefault("status_seen", dict(cursor.get("status_seen") or {}))
    out: list[events.Event] = []
    left: list[str] = []
    failed: list[list] = []
    for i, selector in enumerate(selectors):
        try:
            if selector == SPRINT_SELECTOR:
                evs, patch = _sprint(call, base, env, sprint_rules, cursor, now, until)
                new_cursor[SPRINT_CURSOR] = patch
                new_cursor[SPRINT_LAST] = now
            else:
                evs, patch = _issue(call, base, env, selector, by_issue[selector], cursor, now, until)
                if "comments" in patch:
                    new_cursor["comments"][selector] = patch["comments"]
                if "status" in patch:
                    new_cursor["status"][selector] = patch["status"]
                if "status_seen" in patch:
                    new_cursor["status_seen"][selector] = patch["status_seen"]
        except Budget:
            left = selectors[i:]
            break
        except SelectorFailed as err:
            failed.append(failure(selector, f"tracker {selector}: {err}"))
            continue
        except _BODY as err:
            failed.append(failure(selector, f"tracker {selector}: malformed response ({type(err).__name__}: {err})"))
            continue
        out.extend(evs)
    new_cursor[BUDGET_SPENT] = left
    new_cursor[FAILED] = failed
    return out, new_cursor
