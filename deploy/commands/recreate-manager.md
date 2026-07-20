---
description: Recreate this manager session in a fresh tab, handing off in-flight state
---

# /recreate-manager

User-initiated escape hatch when the manager session feels heavy or needs to pick up new MCP tools. Managers are Claude-only; the replacement always launches the Claude CLI. Do the following in order:

> **Bootstrap fallback for OLD sessions.** If `prepare_handoff` / `spawn_replacement_manager` are not in your MCP tool list, this manager's MCP server booted before recreation shipped. Fall back to: `bash ~/.claude/scripts/bootstrap-recreate.sh --narrative '<your narrative>' --from-sid <your sid> --reason 'mcp-refresh'`. The script writes the handoff JSON directly and the terminal driver opens a new tab whose MCP server picks up the fresh code. `setup.sh` propagates the script to `~/.claude/scripts/`.

1. Confirm I am currently a manager session by calling the `dockwright` MCP tool `list_workers`. If it errors with "not the manager" or similar, tell the user "ERROR: /recreate-manager only works from a manager session" and stop.
2. Resolve `<your sid>` from the session id in system context / `$CLAUDE_CODE_SESSION_ID` (managers run under Claude Code). If a prior broken bootstrap registered this manager under a different sid, use the active manager sid from `list_managers()` for this session. Do not invent or synthesize a sid.
3. Generate a `narrative_summary` (~8–12 sentences) of the current session state: what we have worked on this session, open threads with the user, in-flight worker state that isn't obvious from `list_workers`, and decisions made. The new manager reads this verbatim.
4. Surface a 2-line preview to the user: "Recreating manager. Narrative: <one-line summary>. Spawning replacement..."
5. Call `prepare_handoff(claude_sid=<your sid>, narrative_summary=<the narrative from step 3>, trigger_reason="manual")`. Capture the returned `handoff_id`.
6. Call `spawn_replacement_manager(handoff_id=<id>)` (or the bootstrap fallback above if the tool isn't available). The new tab opens with `/manager-resume <handoff_id>` under the Claude CLI and will take over the lock + SIGTERM this session.
7. Tell the user: "Replacement manager spawning in a new tab. This session will be terminated shortly." Then stop — do not make further tool calls; the new manager will SIGTERM this process when it takes over.

> **REQUIRED handoff invariant — closing THIS predecessor tab.** Closing this session's tab is a mandatory part of the takeover, performed by the successor: `become_manager_with_takeover` closes it automatically (via this session's recorded `iterm_sid`, or resolved through the terminal driver), and `/manager-resume` step 4b then confirms it via the takeover's `predecessor_pane_closed` return, kill-paning it manually only if that reports the pane still open. You (this predecessor) do nothing extra beyond stopping — but the design guarantees this tab is closed so there are never two live managers for one handoff. If this tab is somehow still alive after the successor reports it took over, the user should close it manually.
