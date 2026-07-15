# dockwright-fix-L (Large)

You are working on the claude-orchestrator repo at `{{dockwright_repo_path}}/` (Python MCP server + tmux spawner + tests + setup.sh). Use this preset for complex, cross-cutting, async, or persistence-layer changes.

{{keyed_work_safety_net}}

## Flow

1. **Rebase**: `git fetch origin main && git rebase origin/main`.
2. **Plan**: {{fix_preset_plan_L}}
3. **Execute**: {{fix_preset_execute_L}}
4. **Run tests**: `cd {{dockwright_repo_path}} && python -m pytest -x`. All must pass.
5. **Final code review**: {{fix_preset_review_L}}
6. **Commit**: single-line message, NO ticket prefix (this repo doesn't use them — see `git log --oneline -10` to confirm). No multi-line `-m "$(cat <<EOF...)"`. Push to origin/main.
7. **Deploy**: if anything under `deploy/scripts/`, `agents/`, `commands/`, or `presets/` changed, run `bash setup.sh`. Verify with a concrete `ls` of the affected target dir.
8. **Done signal**: call `worker_done(claude_sid, summary)` with the commit SHA + summary.

Boundaries: do not modify unrelated MCP tools, hooks, or agent files. Do not skip the pytest or review gates.
