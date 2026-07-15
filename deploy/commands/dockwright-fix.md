---
description: Flag this session for an end-of-session dockwright-selffix retrospective, recording an optional note
argument-hint: [note]
---

# /dockwright-fix

This session is now flagged for a `dockwright-selffix` retrospective that runs **at the end of the session** — the SessionEnd hook fires it automatically, keying on the transcript record of THIS command invocation as the flag.

The note you typed after the command (`$ARGUMENTS`, if any) is already recorded in that invocation. There is nothing for you to capture, write, or persist now.

**Continue your current work. Do NOT invoke `dockwright-selffix` or run any retrospective now** — it runs automatically when the session ends. This command does nothing but set the flag.

> If the Gardener module is disabled (`[modules] gardener=false` in dockwright.toml), the SessionEnd retro pathway is off and this flag is a no-op.
