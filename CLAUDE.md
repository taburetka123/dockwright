# dockwright — dev guide

Manager/worker orchestration for Claude Code and Codex over tmux: an MCP server, session hooks, and a deployable `~/.claude` payload. This file is for working ON dockwright; the README covers using it.

## Layout

- `src/dockwright/` — the Python package: `mcp_server.py` (MCP tool surface), `hooks.py` (SessionStart/Stop/SessionEnd + nested detection), `spawner.py`, `terminal.py` (tmux driver), `stale_monitor.py`, CLI dispatcher `__main__.py`.
- `deploy/` — everything `setup.sh` copies into `~/.claude` (and `~/.codex`): `agents/*.core.md`, `commands/`, `skills/`, `scripts/`, `presets/`, `tmux/`, `loops-registry.md`.
- `publish/` — public-repo assets sourced at export time by the dockwright-publish skill: `publish/README.md` is the source of truth for the PUBLIC repo's README (edit it here, ships on every export). This repo's root `README.md` is dockwright-dev's own and never ships.
- `setup.sh` — installer/deployer. Refuses to run from a linked worktree (self-anchors to the main clone).
- `tests/` + `evals/tests/` — pytest suites. `docs/` — design docs and specs (dev repo only; not part of the public export).

## Tests

```bash
.venv/bin/python -m pytest
```

- `pyproject.toml` sets `pythonpath = ["src"]` and `testpaths`, so bare pytest from the repo root resolves imports to THIS tree.
- Use this worktree's own `.venv` — never a sibling checkout's venv and never a `PYTHONPATH=` prefix: both silently import stale source and go green on the wrong code.
- Fresh worktree: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`.

## Making a change live (surface → activation)

| You changed | It goes live when |
|---|---|
| `src/` code run by hooks or the CLI (`hooks.py`, `spawner.py`, …) | next hook fire / CLI run — editable install, nothing to redeploy |
| `src/dockwright/mcp_server.py` | manager recreate (`/recreate-manager`) — the running MCP server process caches the module |
| `deploy/**` (agents, commands, skills, scripts, presets, tmux conf) | `./setup.sh` from the main clone |
| hook wiring (`deploy/settings.snippet.json`) or MCP registration (`claude mcp add`, done by setup) | `./setup.sh` |
| `evals/investigation/` cases or harness | nothing to deploy — run on demand: `python -m evals.investigation.run_eval` |

`stale_monitor.py` is dual-homed: it lives in `src/` but also ships as a standalone copy to `~/.claude/scripts/` — changing it needs `setup.sh`, and it must stay stdlib-only.

## Conventions

- Runtime state lives under `~/.claude/dockwright/`. Always resolve it through `config.state_root()` / `paths.py` — never hardcode the path.
- Deployed scripts (`deploy/scripts/`, `stale_monitor.py`) are standalone and stdlib-only.
- Agent cores (`deploy/agents/*.core.md`) stay generic. Operator-specific text belongs in the overlay dir (`[paths] overlay_dir` in `dockwright.toml`), never in the repo — the repo is a public-publish candidate: no personal identities, machine-specific paths, or private conventions.
- Renames and removals keep a one-release deprecation alias (see CHANGELOG for the pattern).
- Commit style: `topic-slug: Imperative summary` (see `git log`).

## PRs & review

Personal repo — no bot reviewers configured. Before opening a PR: run the full pytest suite, and for changes touching deployed surfaces run `dockwright doctor` after a test deploy. PRs merge to `main` after a code-review pass; the deployed machine converges by re-running `setup.sh` from clean main.
