# dockwright-fix-M (Medium)

You are working on the dockwright repo at `{{dockwright_repo_path}}/` (Python MCP server + tmux spawner + tests + setup.sh). Use this preset for single-feature changes (3-5 files) that warrant a focused code-review pass.

{{keyed_work_safety_net}}

## Flow

1. **Rebase**: `git fetch origin main && git rebase origin/main`.
2. **Implement** the task per the spec below the `---` divider. Add or extend tests in `tests/test_*.py` for every new code path. Mock the filesystem with `tmp_path` + `monkeypatch.setattr(paths, ...)`; never touch real `~/.claude/dockwright/`.
3. **Run tests**: `cd {{dockwright_repo_path}} && python -m pytest -x`. All must pass before review.
4. **Code review**: {{fix_preset_review_M}}
5. **Commit**: single-line message (e.g. `Add X to Y`), NO ticket prefix (this repo doesn't use them — see `git log --oneline -10` to confirm). Push to origin/main.
6. **Deploy**: if anything under `deploy/scripts/`, `agents/`, `commands/`, or `presets/` changed, run `bash setup.sh`. Verify with a concrete `ls` of the affected target dir. Note: setup.sh refuses (exit 4) while `~/.claude/dockwright/active/` has registered sessions — your own record included. `ls` it first: if every live record is this task's own session (compare `claude_sid`) or its manager, deploy with `DOCKWRIGHT_SETUP_FORCE=1` and say so in `worker_done`; if unrelated sessions are live, defer the deploy and report it instead — never blind-force.
7. **Done signal**: call `worker_done(claude_sid, summary)` with the commit SHA + a 1-line summary per fix.

Boundaries: do not modify unrelated MCP tools, hooks, or agent files. Do not skip the pytest or review gates.
