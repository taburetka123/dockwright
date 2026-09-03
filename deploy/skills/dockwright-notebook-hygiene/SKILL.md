---
name: dockwright-notebook-hygiene
description: Actualize, prune and compress the manager notebook — verify every entry against a live source, delete what died, shrink what survived. Run it before ANY prepare_handoff or close_manager_self, including the mcp-refresh self-trigger that reaches prepare_handoff with no command, so the successor inherits a true agenda instead of a large one. Also use when the boot brief prints NOTEBOOK_WARN, or when the user asks to clean up the notebook.
---

# Notebook hygiene

The manager notebook at `<state_root>/notebook/<domain>.md` is working state. A cold successor inherits it verbatim and acts on it, so a large notebook full of yesterday's truth is worse than a small one: the successor acts on the dead lines too.

⛔ **The manager runs this pass itself and never spawns a worker for it.** The agent file's § Manager notebook owns that rule and states its reason; the pass reconciles the notebook against what THIS session learned.

Entry format, the `check:` contract, and archive-on-resolve mechanics belong to the `dockwright-orchestrator-guide` skill § Manager notebook. This skill owns the pass only.

## When

- **Before ANY `prepare_handoff` or `close_manager_self`.** The agent file's § Manager notebook binds the pass to the tool call rather than to a list of commands — read the gate there.
- When the boot brief prints `NOTEBOOK_WARN` — the notebook has grown past the boot warning threshold.
- When the user asks.

## The pass

### 1. Verify by fact, never by memory

For every unresolved entry, ask what a live source says NOW, not what was true when the entry was written. Run the entry's `check:`. Where the `check:` is not runnable, go to whatever system owns the fact — the code host, the tracker, the chat thread, the database.

An entry that says "awaiting X" when X answered two days ago is a landmine: the next manager acts on it.

### 2. The per-line test

*If this line vanished, would someone re-derive it in one command, or would they never know it?*

- **One command** → delete the line and keep the command as the entry's `check:`. State — which PR is where, what is deployed, what is pending — is re-derivable and rots fastest. It is the bulk of what makes a notebook large.
- **Never know** → it is a decision, a measurement, an obligation to a named person, or a refutation. Those survive.

⛔ **Do NOT relocate survivors into skills, rules, code comments or the tracker.** A decision worth keeping stays in the notebook. Relocation buys a second copy nobody re-reads.

### 3. What dies, in practice

- **Whole entries whose work shipped.** Archive the entry in full and re-file anything still open as a FRESH short entry. The guide's § Manager notebook owns that mechanic and the anti-pattern it replaces.
- **A copy of something already durable.** An entry restating a rule file, a doc or a README is duplication; keep one line pointing at the source.
- **Refutations whose target is gone.** A line that exists only to stop someone acting on a wrong claim dies when the claim's entry dies.
- **Obligations that already fired or expired.** Check every one; do not assume the category is safe. An obligation reads as permanent and is often already discharged — a promise kept, a window whose date has passed, a question the user has since answered.

### 4. What survives

Decisions with their reasons; measurements that cost real work to obtain; open questions to a named person; parked work with the verbatim instruction that parked it; and traps that would cost someone a round.

**Every surviving entry carries a `check:`** — one cheap command or inspection that settles whether it is still live. An entry whose liveness cannot be checked in one call is why the file rotted. Give it one wherever a runnable check exists, and `check: none — <who or what settles it>` where none does.

⛔ **An entry is never deleted BECAUSE it cannot be checked.** Unverifiable is not resolved, and § 3's deletion grounds are unaffected. An obligation, a ruling or an open question whose only source is the user has nothing runnable to check by construction — that is what it IS. How such an entry is relayed belongs to the agent file's § Manager notebook.

### 5. Report

Bytes before and after, entries before and after, how many died and the two or three reasons covering most of them, and anything you kept that you expected to kill.

## Anti-patterns

- *"I will note it is stale rather than remove it."* → The next reader still reads it. Remove it.
- *"The entry is huge but it has one live line."* → Then the entry is one live line. Rewrite it as that line.
- *"Splitting the notebook by topic will fix the size."* → It will not. You read the same bytes at boot either way; the problem is staleness, not grouping.
- *"An obligation to a person is always worth keeping."* → Check each one. An obligation you cannot check is not thereby dead; see § 4.
