#!/usr/bin/env python3
import json
import re
import sys


def is_real_assistant_event(event: dict) -> bool:
    if not isinstance(event, dict):
        return False
    if event.get("type") != "assistant" or event.get("isApiErrorMessage"):
        return False
    content = (event.get("message") or {}).get("content")
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            return True
        if block.get("type") == "text" and block.get("text", "").strip():
            return True
    return False


def has_real_assistant_turn(raw: bytes) -> bool:
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if is_real_assistant_event(event):
            return True
    return False


FIX_CMD_RE = re.compile(
    r"<command-name>\s*/(?:dockwright-fix|fix)\s*</command-name>", re.IGNORECASE)
_ANY_CMD_NAME_RE = re.compile(r"<command-name>\s*(/[^<\s]*)\s*</command-name>",
                              re.IGNORECASE)


def is_human_fix_invocation(content) -> bool:
    if not isinstance(content, str):
        return False
    head = content.lstrip()
    if not head.startswith("<command-message"):
        return False
    first = _ANY_CMD_NAME_RE.search(head)
    if first is None:
        return False
    return FIX_CMD_RE.fullmatch(first.group(0)) is not None


def has_human_fix_flag(raw: bytes) -> bool:
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "user":
            continue
        if is_human_fix_invocation((event.get("message") or {}).get("content")):
            return True
    return False


def is_worth_retrospecting(raw: bytes) -> bool:
    return has_real_assistant_turn(raw) or has_human_fix_flag(raw)


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] != "worth-retrospecting":
        print(f"usage: {argv[0]} worth-retrospecting <transcript>", file=sys.stderr)
        return 2
    try:
        with open(argv[2], "rb") as fh:
            raw = fh.read()
    except OSError as e:
        print(f"transcript_signal: cannot read {argv[2]}: {e}", file=sys.stderr)
        return 1
    return 0 if is_worth_retrospecting(raw) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
