from __future__ import annotations

import json
import os
import re
import sys

from . import paths

KEY_ENV = "DOCKWRIGHT_TOPIC_KEY"
READ_TOOLS = frozenset({"Read", "Glob", "Grep", "LS", "ToolSearch"})
STORE_READS = frozenset({"mcp__dockwright__artifact_get", "mcp__dockwright__artifact_list"})
PUT_TOOL = "mcp__dockwright__artifact_put"
WRITE_TOOL = "Write"
OWN_SID_FIELDS = {"mcp__dockwright__worker_done": "claude_sid", "mcp__dockwright__child_done": "child_sid",
                  PUT_TOOL: "writer_sid"}
OWN_SID_COMMAND = ["printenv", "CLAUDE_CODE_SESSION_ID"]
BOARD_FLAGS = frozenset({"--domain", "--kind", "--status", "--json"})
BOARD_WORD = re.compile(r"[A-Za-z0-9_./:=@-]+")


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def _refuse(tool) -> dict:
    return _deny(f"{tool}: the topic agent changes nothing - it reads, writes its answer file in its own topic "
                 "folder, and answers in a reply.* artifact of its own topic")


def _own_session(payload: dict, tool_input: dict, field: str) -> bool:
    session = payload.get("session_id")
    return isinstance(session, str) and bool(session) and tool_input.get(field) == session


def _in_own_folder(path, task_key) -> bool:
    if not task_key or not isinstance(path, str) or not os.path.isabs(path):
        return False
    root = os.path.realpath(paths.topic_export_dir(task_key))
    return os.path.realpath(path).startswith(root + os.sep)


def _own_answer(tool_input: dict, task_key: str | None) -> bool:
    phase, name = tool_input.get("phase"), tool_input.get("name")
    return (bool(task_key) and (tool_input.get("task_key") or tool_input.get("ticket")) == task_key
            and (phase == "reply" or (phase == "summary" and name == "thread"))
            and _in_own_folder(tool_input.get("content_path"), task_key))


def _allowed_bash(command: str) -> bool:
    words = command.strip().split(" ")
    if words == OWN_SID_COMMAND:
        return True
    return words[:2] == ["dockwright", "board"] and all(
        BOARD_WORD.fullmatch(word) and (not word.startswith("-") or word in BOARD_FLAGS) for word in words[2:])


def decide(payload: dict, task_key: str | None) -> dict | None:
    tool = payload.get("tool_name") if isinstance(payload, dict) else None
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    if not isinstance(tool, str) or not isinstance(tool_input, dict):
        return _refuse(tool)
    if tool in READ_TOOLS or tool in STORE_READS:
        return None
    if tool == WRITE_TOOL:
        return None if _in_own_folder(tool_input.get("file_path"), task_key) else _refuse(tool)
    field = OWN_SID_FIELDS.get(tool)
    if (field is not None and _own_session(payload, tool_input, field)
            and (tool != PUT_TOOL or _own_answer(tool_input, task_key))):
        return None
    command = tool_input.get("command")
    if tool == "Bash" and isinstance(command, str) and _allowed_bash(command):
        return None
    return _refuse(tool)


def main(argv=None) -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError:
        print(json.dumps(_deny("the topic agent guard could not read the hook input - denying")))
        return 0
    decision = decide(payload, os.environ.get(KEY_ENV))
    if decision is not None:
        print(json.dumps(decision))
    return 0
