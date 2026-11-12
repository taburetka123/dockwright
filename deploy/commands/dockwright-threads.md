---
description: List open threads waiting on the user — unanswered manager asks, plus the board's inbox and open notes printed verbatim
---

# Threads

List open threads waiting on the user — questions / decisions / asks the manager has posed that the user has not yet answered or implicitly resolved.

When invoked, scan recent assistant messages for asks ("want me to X?", "ship it?", "should I Y?", "ready to verify?"). For each, judge whether the user has responded (explicit answer OR implicit resolution by talking around it / shipping the thing). Skip resolved ones.

Also print the board for your domain — yours if in manager mode, `general` otherwise. Run `dockwright board inbox --domain <domain>` and `dockwright board topics --domain <domain> --kind note --status open`, and print each command's output **exactly as it printed**, under the frame lines below. If a command exits non-zero, print its error output there instead. Do not re-word, re-order, select, group, rank or summarize, and add nothing of your own to its lines. A note line is a claim its author wrote in the past and it may be stale; the frame says so, never a line. **The moment you do anything but print — select among entries, summarize one, answer "is this still open?", propose acting on one — you are the one speaking, and the manager agent file's § Manager notebook gate binds you: run that entry's `check:` first.**

Output format:

```
▶ Open threads waiting on you:

1. <ask 1, 1-line>
2. <ask 2, 1-line>
3. <ask 3, 1-line>

▶ Board inbox — dockwright board inbox --domain <domain>:

<its output verbatim>

▶ Open notes — dockwright board topics --domain <domain> --kind note --status open, not re-verified:

<its output verbatim>
```

With no unanswered asks, drop the first heading rather than printing it empty:

```
▶ No unanswered asks.

▶ Board inbox — dockwright board inbox --domain <domain>:

<its output verbatim>

▶ Open notes — dockwright board topics --domain <domain> --kind note --status open, not re-verified:

<its output verbatim>
```

If there are no unanswered asks, the inbox prints only `Nothing is waiting on you.` and the notes print only `no topics`: `▶ No open threads. We're caught up.`
