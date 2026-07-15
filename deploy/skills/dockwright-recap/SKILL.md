---
name: dockwright-recap
description: Use when the user asks for a recap, a catch-up, or "what happened" — including a colloquial "recap" typed without the slash command. Scopes strictly to async activity SINCE the user's last typed message; never a full-session re-summary.
---

# Recap — async activity since the user's last message

Catch the user up on everything that happened since their previous **typed** message — for when async events (worker `done`/monitor pings, background tasks) drove autonomous action while they were away. NOT a full-session summary, NOT state they already know.

## Steps

1. Find the user's previous **typed** message. Ignore `<task-notification>`, `<system-reminder>`, and other harness-injected blocks — those are events, not user turns. Recap only what happened AFTER it.
2. Recap what happened since then, scannable, in this order (lead each line with its category emoji when in manager mode — ✅ done / 🔍 finding / ⚠️ flag / ❓ decision / 🚀 dispatch / 📋 status):
   - **Events received** — worker `done`, `FINISHED_SILENTLY`, `STALE_*`, questions, account flips: what fired + what it said.
   - **Actions taken** — workers spawned/killed/resumed/merged, PRs opened/merged, commits pushed, configs deployed, decisions made: each with its concrete artifact (SHA / PR# / file path).
   - **Current state** — in-flight workers + state, open PRs + mergeability, pending questions.
3. Ground it in live state, don't recap from memory: in manager mode call `list_workers(manager_sid=<your sid>)` + `list_pending_questions(...)`, and `gh pr view <n>` for any PR touched; verify claimed artifacts before relaying (per the manager agent file § Verify before relay).
4. End with **open threads / decisions still waiting on the user** — a short actionable list. That's the point of the recap.

If nothing happened since the user's last message, say so in one line. Keep it tight: events → actions → state → what's-waiting-on-you. Skip routine/benign pings.
