---
description: Entry point for non-ticket spec-driven work (investigations, manager-authored specs, research tasks)
argument-hint: [task description | /abs/path/to/spec.md | implement /abs/path/to/spec.md — <amendments>]
---

# /dockwright-general-work

Entry point for non-ticket spec-driven work (investigations, manager-authored specs, research tasks).

`$ARGUMENTS` is one of:
- A task description (for fresh work with no spec yet)
- An absolute path to a spec/investigation doc (for implement phase)
- Both: `implement /path/to/spec.md — <any amendments from manager>`

## Classify into one mode, then run it

- **Mode A (investigation):** research question / "why is X happening" / data lookup — gather evidence, no code changes. Write a structured findings doc (question verbatim, evidence with sample sizes, findings, a 3–5-line verdict, open questions) to `~/.claude/scratch/<task-slug>-<YYYYMMDD>.md`, then call `worker_done` with the doc path + the verdict only.
- **Mode B (implementation):** a spec/doc path was passed — Read it IN FULL before any other action; manager amendments after `—` override the spec. Work in the provided worktree (if none was provided, create one off a freshly fetched `origin/main`; never remove it — teardown is the manager's). {{dev_chain_mode_b}}
- **Mode C (combined):** fresh task description with no spec yet — run Mode A in full and stop there; the manager reviews the findings doc and spawns a separate Phase-2 worker with `implement <doc path>`. Default to Mode C when classification is unclear.

Do NOT:
- Dump full investigation findings in `worker_done` — doc path + 5-line verdict is the contract; the manager reads the doc for depth.
- Skip or silently collapse the {{dev_chain_name}} chain for "small" tasks — no size carve-out; if you drop a step anyway, enumerate each dropped step + one-line reason in `worker_done` AND the PR body.
- Remove your own worktree — a session that deletes its own cwd breaks every subsequent hook.

When done, call `worker_done(claude_sid, summary)` — run `echo ${CLAUDE_CODE_SESSION_ID:-$CODEX_THREAD_ID}` to get your claude_sid.
