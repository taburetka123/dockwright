---
description: Focus a worker's tmux window, inferring the target from conversation context
argument-hint: [worker-name | funny-name | sid]
---

# /tab

Focus the tmux window of an active worker. With no argument, infer which worker from this conversation.

Do the following in order:

1. Parse `$ARGUMENTS`. It may be empty, a worker `name` (task label), a `funny_name`, a partial of either, or a `claude_sid`.
2. Read every record in `~/.claude/dockwright/active/*.json`. Keep only records where `agent == "worker"`. Each has `claude_sid`, `name`, `funny_name` (cosmetic, may be absent on older records), and `iterm_sid` (now a tmux pane id, not a kitty window id).
3. Resolve the target worker:
   - **`$ARGUMENTS` non-empty:** match against `name`, `funny_name`, and `claude_sid` — exact match first, then case-insensitive substring. Exactly one match → that's the target. Multiple → list them as `name · funny_name` and ask the user which. None → say so and list the active workers.
   - **`$ARGUMENTS` empty:** INFER the target from this conversation — the worker most recently discussed, dispatched, or referenced by the user this session. State which worker you picked and why in one line before focusing, so a wrong guess is visible. If context gives no single clear worker, list the active workers and ask.
4. Focus the chosen worker's window:

```bash
SOCK="${DOCKWRIGHT_TMUX_SOCKET:-${CLAUDE_ORCH_TMUX_SOCKET:-dockwright}}"
tmux -L "$SOCK" select-window -t <iterm_sid>            # focus the worker's window in the attached session
# if you're attached to a different session: tmux -L "$SOCK" switch-client -t claude-workers:<iterm_sid>
```

CAPABILITY note: tmux can't raise a separate OS window or focus an unattached client — `/tab` selects the worker's window within the attached session (workers live in the `claude-workers` session); use `switch-client` if you're attached elsewhere.
5. Confirm: `Focused <name> (<funny_name>) — window <iterm_sid>`. If the tmux command errors (e.g. window gone), surface the error and note the worker may have closed.
