---
description: List open threads waiting on the user — unanswered manager asks + open manager-notebook entries
---

# Threads

List open threads waiting on the user — questions / decisions / asks the manager has posed that the user has not yet answered or implicitly resolved.

When invoked, scan recent assistant messages for asks ("want me to X?", "ship it?", "should I Y?", "ready to verify?"). For each, judge whether the user has responded (explicit answer OR implicit resolution by talking around it / shipping the thing). Skip resolved ones.

Also read the manager notebook (planned/conditional fleet work — see the manager agent file (`~/.claude/agents/manager.md`) § Manager notebook): `cat <state_root>/notebook/<domain>.md` (your domain if in manager mode, `general.md` otherwise; `<state_root>` defaults to `~/.claude/dockwright`). List its open `## [ ]` entries as a second block — title + `when:` one-liners. Skip the block silently if the file is absent or empty.

Output format:

```
▶ Open threads waiting on you:

1. <ask 1, 1-line>
2. <ask 2, 1-line>
3. <ask 3, 1-line>

▶ Planned (notebook):

1. <entry title> — when: <condition, 1-line>
```

If zero open threads and no notebook entries: `▶ No open threads. We're caught up.`
