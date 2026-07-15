---
description: Close this manager session cleanly, distilling its memory first
---

# /manager-close

User-initiated graceful shutdown for a manager tab. Distills the outgoing transcript into `~/.claude/dockwright/manager-memory/<domain>/<date>-<sid>.md` so future managers (peers or successors) inherit the context, then closes the tab.

Unlike `/recreate-manager`, no replacement is spawned — this is the "I'm done with this domain for now" path.

Do the following in order:

1. Confirm I am a manager session by calling `list_workers(manager_sid=<your sid>)`. If it errors, tell the user "ERROR: /manager-close only works from a manager session" and stop.
2. **Notebook sweep before distilling.** Scan this session for deferred fleet-scoped or conditional intents not yet recorded ("I'll do X after Y", "next manager should…", a planned dispatch you never fired). Write each as an entry in `~/.claude/dockwright/notebook/<domain>.md` per `~/.claude/agents/manager.md` § Manager notebook, and archive any entries this session resolved. The memory distill is a lossy narrative — the notebook is the lossless agenda; an intent that lives only in the distill ages out of the 7-day load window. Skip silently if nothing was deferred.
3. Tell the user: "Distilling outgoing memory; this can take up to 3 minutes. After that the tab closes."
4. Call `close_manager_self(claude_sid=<your session_id>)`. This synchronously:
   - Distills your transcript via `claude -p` (180s budget) into `dockwright/manager-memory/<domain>/<date>-<sid>.md`.
   - Removes your active record and drops pending questions.
   - Closes this tab via the terminal driver (`tmux kill-pane`).
5. The tool returns `{ok, distill_path, name, domain}` if it gets that far. Surface "Closed **<name>** (domain: <domain>). Memory: <distill_path or 'distill failed — see stderr'>." then run `/exit` to ensure the session ends if the tab close hasn't yet propagated.

If distillation fails (claude -p missing / timeout / error), the close still proceeds — the manager-memory file just doesn't get written for this session. The user can still close cmd+w as a fallback; SessionEnd hook will attempt a second distill if needed.
