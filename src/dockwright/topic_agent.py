import re
from pathlib import Path

from . import mcp_server as ms
from . import artifact_export, config, paths, state, topic
from .providers.base import REPLY_MODE
from .reply_guard import KEY_ENV

AGENT_SUFFIX = "-agent"
PROVIDER = "claude-exec"
LAUNCHED = ("registered", "no_register")

BRIEF = """You are the topic agent of `{task_key}`. Someone wrote to this topic: `{record}`. Answer it, then exit.

1. Read with `artifact_get(task_key="{task_key}", phase=<phase>, name=<name>, reader_sid=<your sid>)`, then Read the file its `content_path` names: `{record}` first, then `summary.thread` and `context.ask` when they exist, then every `decision.*` and `reply.*` record that `artifact_list(task_key="{task_key}")` names. `dockwright board topic {task_key}` prints the topic's state.
2. Write your answer to a new file in `{folder}`, its first line `re: {record}`, then publish it in one `artifact_put(task_key="{task_key}", phase="reply", name=<UTC YYYYMMDDTHHMMSS>, content_path=<that file>, status="complete", scope="output", writer_sid=<your sid>)`.
3. Start no work and change nothing else. If the message asks for an action, name the action in your answer and point to the "new work" button on the topic's board page.
4. Fold the exchange into `summary.thread`: read it when it exists, write at most 15 lines to a new file in `{folder}`, then `artifact_put(task_key="{task_key}", phase="summary", name="thread", content_path=<that file>, status="complete", scope="output", overwrite=True, writer_sid=<your sid>)`.
5. Last: `worker_done(claude_sid=<your sid>, summary="topic agent reply for {task_key}: reply.<your stamp> — on the board, no relay needed")`.

Your sid is in the `dockwright: your claude_sid is …` line of your context; else run `printenv CLAUDE_CODE_SESSION_ID`.
"""


def agent_name(task_key: str) -> str:
    return f"{task_key}{AGENT_SUFFIX}"


def _live_records() -> list[dict]:
    return [r for r in state.list_json_in(paths.ACTIVE) if isinstance(r, dict) and ms._pid_reads_alive(r.get("pid"))]


def _live_managers() -> list[dict]:
    return [r for r in _live_records() if r.get("agent") == "manager" and not r.get("nested")]


def _domain_of(record) -> str:
    return record.get("domain") or paths.DEFAULT_DOMAIN


def live_domain_manager(domain: str) -> str | None:
    return next((r.get("claude_sid") for r in _live_managers() if _domain_of(r) == domain), None)


def live_domains() -> list[str]:
    return sorted({d for d in map(_domain_of, _live_managers()) if topic.is_key(d)})


def domain_manager_sid(task_key: str) -> str | None:
    domain = (topic.load(task_key) or {}).get("domain")
    return (live_domain_manager(domain) if domain else None) or ms._manager_sid_for(task_key, None)


def _agent_running(task_key: str) -> bool:
    name = re.compile(rf"{re.escape(agent_name(task_key))}(-\d+)?")
    return any(name.fullmatch(record.get("name") or "") for record in _live_records())


def _cwd(task_key: str) -> str | None:
    worktree = ((topic.load(task_key) or {}).get("links") or {}).get("worktree")
    return worktree if worktree and Path(worktree).is_dir() else None


async def spawn(task_key: str, record: str) -> dict:
    manager_sid = domain_manager_sid(task_key)
    if manager_sid is None:
        return {"route": "agent", "ok": False, "reason": "no manager"}
    if _agent_running(task_key):
        return {"route": "agent", "ok": False, "reason": "agent still answering"}
    model, effort = config.provider_model(PROVIDER), config.provider_effort(PROVIDER)
    if not model or not effort:
        return {"route": "agent", "ok": False, "reason": "[subworkers.providers.claude-exec] model/effort is unset"}
    try:
        folder = artifact_export.make_dir(paths.topic_export_dir(task_key))
        result = await ms.spawn_worker_impl(BRIEF.format(task_key=task_key, record=record, folder=folder),
                                            name=agent_name(task_key), cwd=_cwd(task_key),
                                            env={KEY_ENV: task_key}, manager_sid=manager_sid, task_key=task_key,
                                            provider=PROVIDER, mode=REPLY_MODE, model=model, effort=effort,
                                            _tool="topic")
    except ValueError as err:
        return {"route": "agent", "ok": False, "reason": f"spawn refused: {err}"}
    status = result.get("status")
    if status not in LAUNCHED:
        return {"route": "agent", "ok": False, "reason": status or "no status"}
    return {"route": "agent", "ok": True, "reason": None}
