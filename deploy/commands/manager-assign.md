---
description: Promote this live session into a worker assigned to an active general manager (preserves history via claude --resume)
argument-hint: [worker-name]
---

# /manager-assign

Promote THIS Claude Code session into an orchestrator worker assigned to an active
**general-domain** manager. The conversation is preserved: the session relaunches via
`claude --resume <its own sid>` in the "claude-workers" tmux session, and the
SessionStart hook registers it as a worker (recognized only because the relaunch sets
`CLAUDE_AGENT=worker` / `CLAUDE_WORKER_NAME` / `CLAUDE_PARENT_MANAGER` — env that can't be
retrofitted into a live process).

Run exactly this — `$ARGUMENTS` (may be empty) is the optional worker routing name:

```bash
dockwright assign-to-manager --name "$ARGUMENTS"
```

What it does (deterministically, no MCP dependency):
- Reads `~/.claude/dockwright/active/*.json` directly, keeps `agent=="manager"` records whose
  `domain` is general (absent/null counts as general) and whose `pid` is alive.
- 0 managers → prints `No active general-domain manager. Start one with /manager.` and launches
  nothing.
- 1 → uses it. >1 → picks the newest by `started_at` and prints which, noting the others exist.
- Relaunches `claude --resume $CLAUDE_CODE_SESSION_ID` as a worker in the workers window, scoped
  to the chosen manager. Name defaults to `adopted-<first8ofsid>` when `$ARGUMENTS` is empty.

After it prints success, relay the chosen manager to the user and tell them to **close this tab
(cmd+w)** — the worker copy now in the workers window is the live continuation; this tab is just a
stale duplicate until closed (concurrent resume is safe, so there is no rush, but leaving it open
means two tabs share one session).

If it prints the "No active general-domain manager" message, stop and relay it — nothing launched.
