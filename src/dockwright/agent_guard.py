from __future__ import annotations

import argparse
import json
import sys

AGENT_TOOLS = ("Agent", "Task")
WORKFLOW_TOOL = "Workflow"
MATCHER = "|".join((*AGENT_TOOLS, WORKFLOW_TOOL))
FAILED = "agent guard failed - denying"


def deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def _listed(allowed: set[str]) -> str:
    return ", ".join(sorted(allowed)) or "no subagent"


def decide(payload: dict, allowed: set[str]) -> dict | None:
    if payload.get("tool_name") == WORKFLOW_TOOL:
        return deny(f"agent guard: a Workflow starts subagents no schema can declare; this session may start "
                    f"{_listed(allowed)}")
    if payload.get("tool_name") not in AGENT_TOOLS:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return deny("agent guard: the Agent call carries no input")
    kind = tool_input.get("subagent_type") or "general-purpose"
    if not isinstance(kind, str):
        return deny(f"agent guard: subagent_type is not a string; this session may start {_listed(allowed)}")
    model = tool_input.get("model") or "inherit"
    effort = tool_input.get("effort")
    effort = effort if isinstance(effort, str) and effort else None
    pair = f"{kind}:{model}"
    call = f"{pair}:{effort}" if effort else pair
    if pair in allowed:
        return None
    if effort is not None and call in allowed:
        if tool_input.get("name"):
            return deny(f"agent guard: {call} with `name` starts an in-process teammate, which runs at this "
                        f"session's effort instead of {effort}; re-issue the call without `name`")
        return None
    named = call if effort else f"{pair} with no effort"
    return deny(f"agent guard: {named} is not declared in this node's schema; it may start {_listed(allowed)}. "
                "An entry reads type:model:effort. When an entry has the call's subagent_type, re-issue the call with "
                "that entry's model and effort; a type no entry declares is fixed in the schema, not worked around")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dockwright agent-guard")
    parser.add_argument("--allow", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        print(json.dumps(deny("agent guard could not read the hook input - denying")))
        return 0
    decision = decide(payload, set(args.allow))
    if decision is not None:
        print(json.dumps(decision))
    return 0
