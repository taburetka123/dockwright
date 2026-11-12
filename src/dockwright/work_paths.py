import json

from . import mcp_server, paths, pipeline, pipeline_reconcile, state, topic


def _json_object(path, what):
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ValueError(f"{what} {path} is not JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{what} {path} is not a JSON object")
    return data


def _assignment_cwds() -> dict[str, set[str]]:
    if not paths.ASSIGNMENTS.is_dir():
        raise FileNotFoundError(f"no assignments directory at {paths.ASSIGNMENTS}")
    by_key: dict[str, set[str]] = {}
    for directory in (paths.ASSIGNMENTS_PENDING, paths.ASSIGNMENTS):
        if not directory.is_dir():
            continue
        for p in sorted(directory.glob("*.json")):
            record = _json_object(p, "assignment")
            if record is None:
                continue
            key, cwd = record.get("ticket"), record.get("cwd")
            if isinstance(key, str) and isinstance(cwd, str) and cwd:
                by_key.setdefault(key, set()).add(cwd)
    return by_key


def _event_cwds(task_key) -> set[str]:
    found = set()
    for ev in mcp_server._read_events(paths.artifact_events_path(task_key)):
        cwd = ev.get("cwd")
        if isinstance(cwd, str) and cwd:
            found.add(cwd)
    return found


def _plan_cwds(task_key) -> set[str]:
    root_plan = paths.artifact_path(task_key, "plan", "tasks")
    plans = [root_plan] + sorted((root_plan.parent / "rounds").glob(f"*/{root_plan.name}"))
    found = set()
    for p in plans:
        try:
            text = p.read_text()
        except FileNotFoundError:
            continue
        try:
            _, body = state.parse_artifact(text)
            data = json.loads(body)
        except ValueError as e:
            raise ValueError(f"plan.tasks {p} cannot be read: {e}") from e
        tasks = data.get("tasks") if isinstance(data, dict) else None
        for task in tasks if isinstance(tasks, list) else []:
            cwd = task.get("cwd") if isinstance(task, dict) else None
            if isinstance(cwd, str) and cwd:
                found.add(cwd)
    return found


def _started(task_key) -> bool:
    return any(ev.get("type") == "pipeline_started"
               for ev in mcp_server._read_events(paths.artifact_events_path(task_key)))


def _unfinished(task_key) -> bool:
    if mcp_server._load_snapshot_for(task_key) is None:
        return True
    if pipeline_reconcile.reconcilable(task_key):
        return True
    fold_ = mcp_server._fold_for(task_key)
    return fold_.closed is None and pipeline.DEFAULT_TOKEN_ID not in fold_.tokens


def _kept_worktree(task_key):
    card = _json_object(paths.topic_path(task_key), "topic")
    if card is None or not topic.keeps(task_key):
        return None
    links = card.get("links")
    worktree = links.get("worktree") if isinstance(links, dict) else None
    return worktree if isinstance(worktree, str) and worktree else None


def open_work_paths() -> list[dict]:
    if not paths.ARTIFACTS.is_dir():
        raise FileNotFoundError(f"no artifacts directory at {paths.ARTIFACTS}")
    assigned = _assignment_cwds()
    works = []
    for d in sorted(paths.ARTIFACTS.iterdir()):
        if not d.is_dir():
            continue
        key = d.name
        found: set[str] = set()
        listed = False
        worktree = _kept_worktree(key)
        if worktree is not None:
            found.add(worktree)
            listed = True
        if ((d / "schema.toml").exists() or _started(key)) and _unfinished(key):
            found |= _event_cwds(key) | _plan_cwds(key) | assigned.get(key, set())
            listed = True
        if listed:
            works.append({"task_key": key, "paths": sorted(found)})
    return works
