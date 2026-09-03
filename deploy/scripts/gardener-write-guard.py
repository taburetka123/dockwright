#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys


def _allowed_root() -> str:
    new = os.path.expanduser("~/.claude/dockwright/gardener")
    if os.path.isdir(new):
        return os.path.realpath(new)
    return os.path.realpath(os.path.expanduser("~/.claude/gardener"))


ALLOWED_ROOT = _allowed_root()

PATH_KEYS = ("file_path", "notebook_path", "path")


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def allow() -> None:
    sys.exit(0)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        deny("gardener write-guard: unparseable hook payload (fail-closed)")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        deny("gardener write-guard: no tool_input (fail-closed)")
    target = next((tool_input[k] for k in PATH_KEYS
                   if isinstance(tool_input.get(k), str) and tool_input[k]), None)
    if target is None:
        deny("gardener write-guard: no path argument found (fail-closed)")
    resolved = os.path.realpath(os.path.expanduser(target))
    root = os.path.realpath(ALLOWED_ROOT)
    if resolved == root or resolved.startswith(root + os.sep):
        allow()
    deny(f"gardener is write-scoped to the gardener state dir ({root}) — refusing {resolved}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        deny("gardener write-guard: internal error (fail-closed)")
