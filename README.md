# dockwright

![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)

**Run a fleet of AI coding agents from one chat window.**

dockwright turns a Claude Code session into a *manager* that spawns, supervises, and coordinates *worker* sessions — Claude Code or Codex CLIs, each in its own tmux window. Workers are full processes with their own context, tools, and transcript, not in-process subagents: they run in parallel, survive your chat, and stay inspectable in plain tmux. You talk to the manager; the manager runs the fleet.

## Quickstart

Prerequisites: macOS or Linux with `tmux`, Python 3.11+, and the `claude` CLI (`codex` optional). The optional background loops (Gardener) are installed via launchd and are macOS-only; everything else is platform-neutral.

```bash
git clone https://github.com/taburetka123/dockwright.git
cd dockwright
./setup.sh   # venv + editable install, deploys to ~/.claude, registers the MCP server, wires hooks, runs doctor
```

Start a manager:

```bash
dockwright manager
```

That one command brings up dockwright's dedicated tmux server, opens a Claude Code session in it, and promotes that session to manager. You land inside it. Run it again while a manager is already up and it reattaches to that session instead of starting a second one. Then just ask:

> Spawn a worker to rebase the stale feature branch onto main.

The worker opens in its own tmux window and starts working. When it hits a decision it can't make alone, the manager relays its question to you and routes your answer back. When it finishes, the manager surfaces its summary. Ask `status` anytime for a table of every worker.

> **Command namespace.** `setup.sh` installs slash commands into `~/.claude/commands` (and the Codex equivalents), claiming the names `/manager`, `/manager-*`, `/tab`, `/recreate-manager`, and `/dockwright-*`. If you already have user commands with those names, they get overwritten.

## Architecture

```
 you
  │ chat
  ▼
 manager ─────────── Claude Code session (tmux session "mgr")
  │
  │ spawn_worker · answer_question · kill_worker · …   (MCP tools)
  ▼
 dockwright MCP server + session hooks
  │
  │ all state = plain JSON files under ~/.claude/dockwright/
  ▼
 workers ──────────── claude / codex CLIs (tmux session "claude-workers")
     ask_manager ──► question file ──► manager monitor ──► you
     worker_done ──► done file ─────► manager monitor ──► you
```

There is no daemon. Sessions register themselves through SessionStart/Stop hooks; workers and managers communicate by writing JSON files that background monitors inside the manager session poll. A standalone stale monitor watches the fleet from outside: mid-turn stalls, silently finished workers, idle tabs (auto-closed, resumable), and rate-limited accounts.

## Key concepts

- **Manager / workers.** A manager talks to you and owns its worker pool; multiple managers can coexist, each with an isolated pool. Workers are spawned CLI processes — `runtime="claude"` (default) or `"codex"` — with color-coded tab titles (idle / processing / awaiting human).
- **`ask_manager` / `worker_done`.** The worker-side protocol: block on a question until the human answers through the manager; signal explicit completion with a summary. Both are files on disk, so nothing is lost if a session dies mid-flight.
- **Manager recreation.** `/recreate-manager` snapshots in-flight state, opens a fresh manager tab, and transfers the manager identity — workers and pending questions carry across. Manager memory is distilled to disk across recreations and session ends.
- **Artifacts & pipeline.** Workers persist specs/plans/results as durable per-task documents (`artifact_put`); `pipeline_status(task_key)` replays the whole board — artifacts × assignments × events — after any crash or recreate.
- **Account pooling & auto-switch.** Optional: spawns round-robin across multiple `/login` accounts weighted by rate-limit headroom; when the active account hits its limit, a pointer flips and new spawns plus manager recovery ride the healthy one.
- **The compose seam.** Deployed agent files are generated from generic cores (`deploy/agents/*.core.md`) + your private overlay drop-ins + `dockwright.toml` vars — the shipped product stays generic, your conventions live outside the repo.
- **Optional modules.** A self-improvement loop (session retrospectives digested into ranked improvement proposals) ships inert, off by default. If you use [Superpowers](https://github.com/obra/superpowers), bind its skills via overlay vars; otherwise a framework-neutral engineering chain ships as-is.

## Commands

CLI:

```bash
dockwright manager        # bring up the tmux server, open Claude, promote it to manager, attach
dockwright doctor         # verify wiring (MCP registration, hooks, venv, compose freshness)
dockwright init           # write a dockwright.toml with every default spelled out
dockwright compose        # recompose agent files (cores + overlay + vars) standalone
dockwright spend-report   # token spend per session / account
dockwright migrate-state  # move a legacy state tree into ~/.claude/dockwright/ (setup.sh runs this for you)
dockwright uninstall      # provenance-driven removal; --dry-run to preview
```

Slash commands (installed for Claude and Codex): `/manager`, `/recreate-manager`, `/manager-close`, `/tab <worker>`, `/dockwright-threads`, and the `/dockwright-*` utility family.

MCP tools: `spawn_worker`, `list_workers`, `answer_question`, `kill_worker`, `resume_worker`, `wait_for_worker`, `send_manager_to_worker`, the artifact/pipeline store, and more — see the `dockwright-orchestrator-guide` skill installed with the package.

## Configuration

`dockwright init` writes `~/.config/dockwright/dockwright.toml` with every key at its default — paths, spawn models, account pool, overlay dir, module toggles. Every key is optional; the file changes nothing until you edit it. Environment knobs (stale thresholds, idle TTL, concurrency slots) are documented in the orchestrator-guide skill.

## Evals

The code-review verifier that gates worker output ships with an offline eval harness (`evals/`) over 24 labeled cases (12 injected defects, 12 clean changes including "looks-buggy-but-correct" traps).

```bash
python -m evals.run_eval --dry-run     # plumbing check — fake verifier, no API calls, $0
python -m evals.run_eval               # real run (default model tier: sonnet, 3 runs/case)
python -m evals.run_eval --model opus  # production-faithful tier — the live verifier runs on opus
python -m pytest evals/tests -q        # pure scoring/parsing logic, no network
```

Real runs call the Claude API and cost money; `--dry-run` doesn't. The default eval tier is `sonnet` (cheap); pass `--model opus` to measure the tier the production verifier actually uses. Methodology and honesty notes: [`evals/README.md`](evals/README.md).

## Uninstall

```bash
dockwright uninstall --dry-run   # preview everything that would be removed
dockwright uninstall             # do it
```

Removal is provenance-driven, never a hardcoded glob: it boots out the launchd loops, deregisters the MCP server, strips exactly the dockwright-owned hooks out of `~/.claude/settings.json` / `~/.codex/hooks.json` (foreign hooks survive), and removes only files it can positively identify as its own. Deliberately kept: the clone itself, your `dockwright.toml`, and the operator overlay dir.

## Limitations

- Terminal-backed: tmux is the sole backend (runs in any terminal that can host `tmux attach`).
- No web/GUI status view — `status` lives in the manager chat.
- The optional background loops (Gardener/selffix) are launchd-based, macOS-only.

## Docs

- [`deploy/loops-registry.md`](deploy/loops-registry.md) — registry of the optional background loops.
- `dockwright-orchestrator-guide` — the full product manual, installed as a skill so your manager can read it too.

## License

[Apache-2.0](LICENSE). See also [`NOTICE`](NOTICE).
