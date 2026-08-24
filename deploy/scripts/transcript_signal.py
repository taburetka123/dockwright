#!/usr/bin/env python3
"""Is a transcript worth feeding to a headless `claude -p` child?

A session whose model never ran (dead login, 401 storm) leaves a transcript that
is ~100% embedded instructions and ~0% conversation: slash-command wrappers,
numbered procedures, text addressed to "you" — all of it written for a DIFFERENT
session. On 2026-07-29 exactly such a transcript was handed to the memory-distill
child, which executed the `/manager-takeover-recovery` procedure it had been asked
to summarise: it closed a live manager's tmux pane, then killed and resumed a
worker. Domain `general` had no manager for 2h09m.

Reducing the untrusted INPUT is independent of how far the child's AUTHORITY is
contained, and it closes the specific shape that actually fired. `distill.py` got
this gate under PR #245; this module is the shell lanes' equivalent, callable as:

    python3 transcript_signal.py worth-retrospecting <path>   # exit 0 / 1

Deliberately dependency-free and standalone so `selffix-run.sh` can call it
without importing the dockwright package (the retro runs from a dying session's
SessionEnd hook, with no venv guarantees).

Canonical source: deploy/scripts/transcript_signal.py @
taburetka123/claude-orchestrator — deployed by setup.sh. Edit the repo copy.
"""
import json
import re
import sys


def is_real_assistant_event(event: dict) -> bool:
    """Did the MODEL speak in this event?

    `isApiErrorMessage` entries are CLI-emitted banners ("Login expired · Please
    run /login"), not model output — a session whose login was dead has only
    these, and counting them as turns would make a transcript that is 100%
    embedded instructions look like a conversation worth retrospecting.

    Mirrors `distill.py::_is_real_assistant_event` (PR #245) deliberately: the two
    lanes fire on the SAME SessionEnd over the SAME transcript, so a shape one
    lane skips and the other ingests is a hole.
    """
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
    """True if the session's model ran at least once.

    Reads the RAW JSONL rather than any rendered/slimmed text on purpose: an
    `ASSISTANT:` prefix in a projection is just a line prefix, so transcript
    CONTENT could forge one. An `assistant` EVENT cannot be forged by what a user
    message or a tool result says.
    """
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


# CANONICAL — this module is the single source of the human-fix-flag predicate.
# `selffix-trigger.sh` imports `is_human_fix_invocation` from here rather than
# carrying its own matcher: the trigger decides whether to SPAWN the retro and
# this module decides whether the retro may RUN, so two hand-written matchers for
# one concept guarantee a silent divergence — a session the trigger flags and the
# gate then drops loses the highest-priority retro input with only a DEBUG-gated
# log line. (Tier-2 on PR #248 measured three such shapes: leading whitespace,
# different case, and a tag past a fixed character window.)
#
# Regex form, not a literal tag, so pasting THIS code does not self-match — the
# same reason `selffix-trigger.sh` used a regex. `\s*` + IGNORECASE match the
# trigger's long-standing tolerance.
FIX_CMD_RE = re.compile(
    r"<command-name>\s*/(?:dockwright-fix|fix)\s*</command-name>", re.IGNORECASE)
_ANY_CMD_NAME_RE = re.compile(r"<command-name>\s*(/[^<\s]*)\s*</command-name>",
                              re.IGNORECASE)


def is_human_fix_invocation(content) -> bool:
    """Is this user record a human typing `/dockwright-fix` in THIS session?

    Two invariants, both load-bearing:

    1. POSITION — a genuine invocation's content STARTS with the
       `<command-message…>` wrapper. A distillation/handoff session embeds a
       PRIOR transcript as its lone user string, and that payload can carry a
       real tag buried mid-string; that is not a flag for this session.
    2. FIRST COMMAND — the flag must be the command that was actually invoked,
       i.e. the FIRST `<command-name>` element. Position alone does not close
       forgery: a genuine `/dockwright-general-work` invocation whose
       `<command-args>` brief happens to quote the fix tag also starts with the
       wrapper. Keying on the first element makes the exemption structural
       (which element) rather than textual (does the string appear anywhere).

    Only str content qualifies: a list-content record is tool output, not
    something a human typed.

    HONEST LIMIT: this is keyed on the transcript's own text, so a session whose
    FIRST command really is `/dockwright-fix` is indistinguishable from one where
    a human typed it — which is the point — but nothing here proves a human typed
    it rather than a payload having been constructed to look that way. Invariant 2
    closes the realistic forgery (a tag quoted inside another command's args); a
    transcript forged wholesale at position zero still passes. That is acceptable
    only because this gate is the BELT: the child's authority is contained
    independently (default-deny tools, no MCP, no inherited settings), so the
    worst case is prose reaching a findings file a human later reads. Do not let
    this predicate grow into a trust decision that something else relies on.
    """
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
    """True if any user record in the transcript is a genuine fix invocation."""
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
    """Feed this transcript to a model, or not.

    A session whose model never ran has nothing to retrospect and is the worst
    possible input. The one exception is a session a human explicitly flagged with
    `/dockwright-fix`: the note they typed IS the signal, and dropping it would
    silently lose a deliberate human ask. That exception is safe because the input
    gate is the belt, not the brace — the child's authority is contained
    independently (default-deny tools, no MCP, no inherited settings).
    """
    return has_real_assistant_turn(raw) or has_human_fix_flag(raw)


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] != "worth-retrospecting":
        print(f"usage: {argv[0]} worth-retrospecting <transcript>", file=sys.stderr)
        return 2
    try:
        with open(argv[2], "rb") as fh:
            raw = fh.read()
    except OSError as e:
        # Fail CLOSED: an unreadable transcript is not a transcript we can vouch
        # for, and the caller's job is to decide not to feed it to a model.
        print(f"transcript_signal: cannot read {argv[2]}: {e}", file=sys.stderr)
        return 1
    return 0 if is_worth_retrospecting(raw) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
