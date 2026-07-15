#!/usr/bin/env python3
"""PreToolUse write-guard for Gardener analyst sessions (PRD v2 §9.1).

Injected via the spawned session's --settings payload (gardener-run.sh) as a
PreToolUse hook on Write|Edit|NotebookEdit. Denies any file-writing tool call
whose target resolves outside the gardener state dir (see `_allowed_root()`).

Why a hook and not permission rules: permission arrays MERGE across settings
sources and deny>allow>ask has no "everything except X" shape — this user's
settings.json carries Write(*)/Edit(*) in allow, so anything not explicitly
denied is silently auto-approved (verified empirically: an outside-scope
Write succeeded under a replace-allow --settings payload). A PreToolUse deny
is decisive regardless of allow rules, expresses the complement directly, and
fails closed.

Fail-closed contract: no parseable payload, no recognizable path key, or any
internal error → deny. The only allowed outcome is a resolved target under
the gardener state dir (see `_allowed_root()`).

Scope: file-writing tools only. Bash is NOT vetoed here (command strings are
not reliably parseable for write-ness) — it stays on the runtime's own
vetting plus the watching human, per PRD §9.1 / §16 Q5.
"""
from __future__ import annotations

import json
import os
import sys


def _allowed_root() -> str:
    # deprecated, one release: prefer the dockwright gardener home, fall back to legacy
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
    # No output = no opinion: fall through to normal permission evaluation
    # (the payload's allow rule / the user's settings take it from here).
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
