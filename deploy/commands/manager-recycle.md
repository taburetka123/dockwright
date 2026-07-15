---
description: Recycle this manager in place — /clear + reboot in the same tab (context-bloat only)
---

# /manager-recycle

In-place manager recreate: clears this session's conversation (`/clear`) and reboots manager state in the SAME tab via `/manager-reboot`. Armed Monitors, background shells, the MCP server process, and the tmux pane all survive `/clear` — only the conversation context resets.

**Serves context-bloat only.** `/clear` does not restart the MCP server process, so this flow cannot pick up `mcp_server.py` changes, MCP registration, or settings changes — need new MCP tool code → use `/recreate-manager`.

Do the following in order:

1. Confirm I am currently a manager session: call the `dockwright` MCP tool `list_workers`. If it errors with "not the manager" or similar, tell the user "ERROR: /manager-recycle only works from a manager session" and stop.
2. Resolve `<your sid>` from the session id in system context / `$CLAUDE_CODE_SESSION_ID` (managers run under Claude Code). If a prior broken bootstrap registered this manager under a different sid, use the active manager sid from `list_managers()`. Do not invent or synthesize a sid.
3. Generate a `narrative_summary` (~8–12 sentences): what we worked on this session, open threads with the user, in-flight worker state that isn't obvious from `list_workers`, decisions made. The rebooted manager reads this verbatim.
4. Call `prepare_handoff(claude_sid=<your sid>, narrative_summary=<the narrative>, trigger_reason="in-place")`. Capture the returned `handoff_id`. This also runs the synchronous memory distill, so the lossy-loss contract equals the windowed `/recreate-manager` flow's.
5. Arm the detached clear-and-reboot sender as ONE Bash call with `run_in_background: true` (background shells survive `/clear` — that is the bridge this flow rides). Substitute `<handoff_id>` literally; everything else is verbatim:

```bash
PANE="$TMUX_PANE"
SOCK="${DOCKWRIGHT_TMUX_SOCKET:-${CLAUDE_ORCH_TMUX_SOCKET:-dockwright}}"
[ -n "$PANE" ] || exit 0
# Locate OUR transcript by sid, not cwd-slug (Bash pwd can drift).
CUR=$(ls "$HOME/.claude/projects"/*/"$CLAUDE_CODE_SESSION_ID".jsonl 2>/dev/null | head -1)
[ -n "$CUR" ] || exit 0
PROJ=$(dirname "$CUR")
BASE=$(ls -t "$PROJ"/*.jsonl 2>/dev/null | head -1)
sleep 3
# Flush the input box first: Ctrl-U, NEVER Esc (Esc cancels the running turn).
tmux -L "$SOCK" send-keys -t "$PANE" C-u
printf '/clear' | tmux -L "$SOCK" load-buffer -b orch_recycle -
tmux -L "$SOCK" paste-buffer -p -d -b orch_recycle -t "$PANE"
tmux -L "$SOCK" send-keys -t "$PANE" Enter
# Typed mid-turn the /clear queues and executes as a builtin at turn end.
# Poll for sid rotation — a NEW transcript jsonl — the ground-truth signal.
NEW="$BASE"
for _ in $(seq 1 60); do
  sleep 1
  NEW=$(ls -t "$PROJ"/*.jsonl 2>/dev/null | head -1)
  [ "$NEW" != "$BASE" ] && break
done
if [ "$NEW" = "$BASE" ]; then
  exit 0  # /clear never landed — nothing destructive happened; user just retries
fi
sleep 2
tmux -L "$SOCK" send-keys -t "$PANE" C-u
printf '/manager-reboot <handoff_id>' | tmux -L "$SOCK" load-buffer -b orch_recycle -
tmux -L "$SOCK" paste-buffer -p -d -b orch_recycle -t "$PANE"
tmux -L "$SOCK" send-keys -t "$PANE" Enter
```

6. End your turn with exactly one line — no further tool calls after arming the sender: "Recycling in place (handoff `<first 8 chars of handoff_id>`): context clears at turn end; reboot follows automatically." The queued `/clear` executes when this turn ends; the sender then types `/manager-reboot <handoff_id>` into the fresh conversation.

**Failure modes:** sender's `/clear` never lands (concurrent user typing, input dialog open) → the poll times out and exits silently; the session stays fat and unchanged — re-run `/manager-recycle` (a fresh handoff supersedes; the old one is harmless, just unconsumed). The old conversation stays reachable via `/resume <old-sid>` in this tab — a recovery net the windowed flow lacks. Requires a tmux-launched manager: with `$TMUX_PANE` unset the sender's tmux calls all no-op and the session is left unchanged. The timeout branch is not a guarantee: a `/clear` queued mid-turn can still fire after the poll gave up (a long final turn), leaving a cleared manager with no reboot typed — recover by running `/manager-reboot <handoff_id>` manually (newest id via `ls -t ~/.claude/dockwright/handoffs/`), or `/resume <old-sid>` to return to the fat conversation. Also note the sender's `Ctrl-U` flush wipes any in-progress draft in the input box, and the trailing Enter could confirm an open dialog — invoke at a quiet moment.
