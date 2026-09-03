---
description: List open threads waiting on the user — unanswered manager asks, plus the manager notebook's own entry headers quoted verbatim
---

# Threads

List open threads waiting on the user — questions / decisions / asks the manager has posed that the user has not yet answered or implicitly resolved.

When invoked, scan recent assistant messages for asks ("want me to X?", "ship it?", "should I Y?", "ready to verify?"). For each, judge whether the user has responded (explicit answer OR implicit resolution by talking around it / shipping the thing). Skip resolved ones.

Also QUOTE the manager notebook (planned/conditional fleet work — see the manager agent file (`~/.claude/agents/manager.md`) § Manager notebook). For `<state_root>/notebook/<domain>.md` (your domain if in manager mode, `general.md` otherwise; `<state_root>` defaults to `~/.claude/dockwright`), print every header line that is not marked resolved, **exactly as written in the file** and nothing else on the line. Skip the block silently if the file is absent, empty, or holds no unresolved entry.

⛔ **Derive the lines; do not transcribe the file from memory.** One call emits exactly the lines to print, byte-exact and in file order:

```
grep '^## ' <path> | grep -v '^## \[x\]'
```

Hand-copying headers drifts silently — they carry emoji, em-dashes, backticked identifiers and non-ASCII text, and a mistranscribed header is indistinguishable from the file's own text, which is the one thing this block promises the reader it is not. **The selector is `^## ` minus `^## [x]`, never `## [ ]`:** some entries are rulings that carry no checkbox, and they are the `⛔ do not act on this` ones. Selecting on `## [ ]` silently drops exactly those.

⛔ **Verbatim means verbatim, and the completeness is what makes it honest.** Print the header byte-for-byte, including its emoji and any status word it carries — that word is usually the only thing telling the reader the entry is not live work. Do not shorten, re-word, re-order, translate, group or rank. Print every unresolved entry, not a selection you think is relevant. Add nothing of your own: no category label, no "Planned", no "waiting on you" over this block, no `when:`, no summary, no count of what you left out. The user is reading their own file; you are the pipe, not the author.

⛔ **A header is a claim its author wrote in the past and it may be stale — that is the notebook's honesty level, and quoting it does not raise or lower it.** The frame carries that in the words given below; say it nowhere else and never per line. **The moment you do anything but quote — select among entries, summarize one, answer "is this still open?", propose acting on one — you are the one speaking, and the agent file's § Manager notebook gate binds you: run that entry's `check:` first.**

Output format:

```
▶ Open threads waiting on you:

1. <ask 1, 1-line>
2. <ask 2, 1-line>
3. <ask 3, 1-line>

▶ Notebook — headers as written in <path to the notebook file>, not re-verified:

<header line verbatim>
<header line verbatim>
```

With no unanswered asks but a non-empty notebook, drop the first heading rather than printing it empty:

```
▶ No unanswered asks.

▶ Notebook — headers as written in <path to the notebook file>, not re-verified:

<header line verbatim>
```

If zero open threads and no unresolved notebook entries: `▶ No open threads. We're caught up.`
