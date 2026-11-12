# dockwright — dev guide

Manager/worker orchestration for Claude Code and Codex over tmux: an MCP server, session hooks, and a deployable `~/.claude` payload. This file is for working ON dockwright; the README covers using it.

## Layout

- `src/dockwright/` — the Python package: `mcp_server.py` (MCP tool surface), `hooks.py` (SessionStart/Stop/SessionEnd + nested detection), `spawner.py`, `terminal.py` (tmux driver), `stale_monitor.py`, CLI dispatcher `__main__.py`.
- `deploy/` — everything `setup.sh` copies into `~/.claude` (and `~/.codex`): `agents/*.core.md`, `commands/`, `skills/`, `scripts/`, `presets/`, `tmux/`, `loops-registry.md`.
- `deploy/roles/` — generic role cores (`<Role>.core.md`) and canonical fragments (`fragments/`). `dockwright compose` renders them with the operator's manifests in `~/.claude/roles/<Role>/role.toml` into `role.<launch>.md`, `role.codex.md` and `role.settings.json` beside each manifest (`src/dockwright/roles.py`).
- `publish/` — public-repo assets sourced at export time by the dockwright-publish skill: `publish/README.md` is the source of truth for the PUBLIC repo's README (edit it here, ships on every export). This repo's root `README.md` is the development repo's own and never ships.
- `setup.sh` — installer/deployer. Refuses to run from a linked worktree (self-anchors to the main clone). **Refuses to run while `$CLAUDE_DIR/dockwright/active/*.json` shows registered live worker/manager sessions (exit 4) — it mutates the deployed tree in place, and a session booting mid-run sees a half-updated `~/.claude` silently; `DOCKWRIGHT_SETUP_FORCE=1` overrides for a deliberate live deploy.** A `[paths] state_root`-pinned registry is outside the gate's sight — on such an install check `list_workers`/`list_managers` yourself before deploying.
- `tests/` + `evals/tests/` — pytest suites. `docs/` — design docs and specs (dev repo only; not part of the public export).

## Tests

```bash
.venv/bin/python -m pytest
```

- `pyproject.toml` sets `pythonpath = ["src"]` and `testpaths`, so bare pytest from the repo root resolves imports to THIS tree.
- For a full-suite count, always use bare `.venv/bin/python -m pytest` from repo root — `pytest tests/` silently excludes `evals/tests/` (~56 tests) and under-reports the total.
- Use this worktree's own `.venv` — never a sibling checkout's venv and never a `PYTHONPATH=` prefix: both silently import stale source and go green on the wrong code.
- Fresh worktree: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`.
- A test that loads a dual-homed standalone script (`stale_monitor.py` and any future sibling) via `importlib.util.spec_from_file_location`/`exec_module` for isolation must set every env var that affects module-level path binding (e.g. `HOME`) *before* `exec_module` — patching a derived attribute afterward only covers what the module happens to read today; `stale_monitor.py` binds `HOME` and derives `ROOT`/`ACTIVE`/`CLOSED` at import time, so a post-hoc `mod.ROOT = ...` patch leaves the others live-rooted.

## Making a change live (surface → activation)

| You changed | It goes live when |
|---|---|
| `src/` code run by hooks or the CLI (`hooks.py`, `spawner.py`, …) | next hook fire / CLI run — editable install, nothing to redeploy |
| `src/dockwright/mcp_server.py` | manager recreate (`/recreate-manager`) — the running MCP server process caches the module |
| `src/dockwright/models.toml` (model pins) | the next resolution in any process, no redeploy — an editable install reads it from the main clone, and a running MCP server picks it up on its next launch |
| `deploy/**` (agents, commands, skills, scripts, presets, tmux conf) | `./setup.sh` from the main clone |
| `deploy/roles/**`, or a manifest, fragment or overlay under `~/.claude/roles/` | `dockwright compose` (setup.sh runs it) |
| a unit's frontmatter, `[profiles]`, `[subagents]`, `roles/actions.toml`, or the content of `[paths] project_units_dir` | `dockwright compose` (setup.sh runs it) |
| a NEW file under one of the seven allowlisted directories | it also needs its path in `shipped-files.txt` — see below |
| hook wiring (`deploy/settings.snippet.json`) or MCP registration (`claude mcp add`, done by setup) | `./setup.sh` |
| `evals/investigation/` cases or harness | nothing to deploy — run on demand: `python -m evals.investigation.run_eval` |

`stale_monitor.py` is dual-homed: it lives in `src/` but also ships as a standalone copy to `~/.claude/scripts/` — changing it needs `setup.sh`, and it must stay stdlib-only. It binds `HOME` and derives `ROOT`/`ACTIVE`/`CLOSED` at import time, so another package module that imports it (`monitor.py` borrows `_process_index` and `_has_live_background_shell`) may borrow only functions that take everything they read as arguments — a borrowed function that reads those module globals would resolve them against the real home on a `[paths] state_root`-pinned install and under any fixture that redirects `paths.ROOT`.
Agent sizes are gated in two places, on two different artifacts. `tests/test_agent_size_ceiling.py` bounds the SHIPPED render — the cores plus `deploy/agents/vars.defaults.toml`, no operator overlay, `HOME` pinned — so it measures the same bytes in CI and on every machine and never skips; run it before drafting text edits to a core file. `dockwright doctor`'s `agents:size` check bounds the DEPLOYED render — those cores plus YOUR overlay drop-ins and YOUR `[agent_vars]` — against `[ceilings]` in `dockwright.toml`, which is unset by default because no repo constant can budget your overlay; `setup.sh` prints that failure loudly and continues, the way it already does for `accounts:login`. Neither ceiling moves as a side effect of the change in flight: an author pays for new text by compressing something in the same PR, never by raising the constant to fit their own diff. A ceiling RISES only on an operator grant about the corpus as a whole, in its own PR carrying nothing but `CHANGELOG.md` and the constants. Growth is otherwise paid for out of the CORE files: trimming a `vars.defaults.toml` default buys shipped bytes and buys nothing on any install that overrides that key, which is most of them.

## Adding a file that ships

`publish-allowlist.txt` and `publish-excludes.txt` (repo root) are the export
policy: `export set = git ls-files(HEAD) ∩ allowlist − excludes`. They live here
rather than in the operator's publish skill so `tests/export_surface.py` resolves
them on a CI runner.

`shipped-files.txt` (repo root) lists every path that export ships, one per
line. A new file under an allowlisted directory does not reach the public repo
until its line is there: the ship-list gate fails on the mismatch, on your machine
and in CI, and `publish.sh verify` refuses the export besides. (`build_export`
itself does not read the list — the file is built into the export tree and the
gate is what stops it.)

Add the line in the same PR as the file: that is the point — `publish-allowlist.txt`'s
directory entries let new content ship with no edit anywhere, so without the
manifest line nothing puts a newly shipped file in front of a reviewer.

⚠️ **Only seven directories are allowlisted**: `src`, `deploy`, `tests`,
`scripts`, `evals/dataset`, `evals/tests`, `evals/investigation/cases`. The rest
of `evals/` is allowlisted file by file, so `evals/newmodule.py` does NOT ship —
adding a `shipped-files.txt` line for a path outside those seven reddens the
suite, because the list would be claiming something ships when it does not.

The line is an affirmation that the file may be PUBLIC, which is a separate
question from whether it belongs in this repo. Two gates enforce the surface and
both are publish-excluded operator machinery: `tests/test_export_ship_list.py`
and `tests/test_export_place_zones.py` (no place-naming IANA timezone may ship —
use `Etc/UTC`, or `Etc/GMT-N` where a fixture needs a non-UTC clock).

Employer and operator name tokens are refused separately, at publish time, by the
`dockwright-publish` skill's `sweep.py` over the built export tree. Its whitelist
cannot excuse one — not per file, not with a bare `*` — so a red gate is cleared
by scrubbing the source or publish-excluding the file, never by widening the
whitelist.

## Conventions

- Runtime state lives under `~/.claude/dockwright/`. Always resolve it through `config.state_root()` / `paths.py` — never hardcode the path.
- Deployed scripts (`deploy/scripts/`, `stale_monitor.py`) are standalone and stdlib-only.
- Agent cores (`deploy/agents/*.core.md`) stay generic. Operator-specific text belongs in the overlay dir (`[paths] overlay_dir` in `dockwright.toml`), never in the repo — the repo is a public-publish candidate: no personal identities, machine-specific paths, or private conventions.
- Renames and removals keep a one-release deprecation alias (see CHANGELOG for the pattern).
- Commit style: `topic-slug: Imperative summary` (see `git log`).
- Platform-level decisions (substrate, state model, review cadence, public boundary) are recorded as one-page ADRs in `docs/adr/` — see `docs/adr/0000-template.md`; docs precedence order lives in `docs/README.md`.
- Design specs from `superpowers:brainstorming` go to `docs/specs/`, not the skill's default `docs/superpowers/specs/` — that path is gitignored local planning scratch.

## PRs & review

Personal repo — no bot reviewers configured. Before opening a PR: run the full pytest suite, and for changes touching deployed surfaces run `dockwright doctor` after a test deploy. PRs merge to `main` after a code-review pass; the deployed machine converges by re-running `setup.sh` from clean main.
