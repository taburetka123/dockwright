---
kind: guard
binds: [worktree]
domain: git
---
## Edits under `~/.claude` stay in YOUR session, never an implementer subagent

A plan or task that edits `~/.claude` rules, agents, skills or settings assigns that edit to your top-level session. Expect a harness self-modification dialog that no settings preset clears: it needs your manager at the pane, so flag it and keep working.

## Stay in your born repo — cross-repo work goes UP to the manager

An investigation or change that belongs to a different repo, even the real root cause, is not yours: `ask_manager(claude_sid, "<what you found + which repo it lives in + recommendation>")`. Never `cd`/clone/branch into another repo.

## Scoped to a worktree? Guard your edits and your staging

- **Isolation.** If your task branches, commits or pushes in a repo that may host concurrent workers, do NOT branch in the shared working directory: create an isolated worktree off the target base first, and {{worktree_isolation_ref}}. When in doubt, isolate.
- **Absolute-path edits.** Scoped to a worktree and about to Edit/Write a path under {{canonical_repo_roots}}: stop and substitute the worktree path.
- **Staging.** `git add <directory>` captures sibling workers' untracked files. Stage the explicit files you created or edited, and confirm with `git diff --cached --name-only` before committing.
- **Follow-up work after your worktree is gone:** recreate a throwaway worktree with `{{worktree_helper_cmd}}`, never a bare `git worktree add`, and never `git stash` + `git checkout <branch>` inside the shared clone.
- **Python.** Bootstrap a worktree-local venv (`python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`, then `.venv/bin/python -m pytest`); no `PYTHONPATH=` prefix, no borrowing a sibling's `.venv`.
