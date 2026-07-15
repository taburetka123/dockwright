# dockwright-fix-S (Simple)

You are working on the dockwright repo at `{{dockwright_repo_path}}/` (Python MCP server + tmux spawner + tests + setup.sh). Use this preset only for trivial changes (1-2 files, ≤30 lines, no edge cases).

{{keyed_work_safety_net}}

## Flow

1. **Rebase**: `git fetch origin main && git rebase origin/main`.
2. **Implement** the task per the spec below the `---` divider. Touch only what's needed — no speculative refactors, no backwards-compat shims.
3. **Run tests**: `cd {{dockwright_repo_path}} && python -m pytest -x`. All must pass before commit.
4. **Commit**: single-line message (e.g. `Add X to Y`), NO ticket prefix (this repo doesn't use them — see `git log --oneline -10` to confirm). Push to origin/main.
5. **Deploy**: if anything under `deploy/scripts/`, `agents/`, `commands/`, or `presets/` changed, run `bash setup.sh` to deploy into `~/.claude/`. Verify with a concrete `ls` of the affected target dir.
6. **Done signal**: call `worker_done(claude_sid, summary)` with the commit SHA + a 1-line summary.

Boundaries: do not modify unrelated MCP tools, hooks, or agent files. Do not skip the pytest gate.
