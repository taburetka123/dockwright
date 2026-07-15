# Changelog

User-facing release notes for dockwright. Entries describe what an adopter
gets, not internal development history.

## v1.0.0 — 2026-07-15

Initial public release.

- **Manager/worker orchestration over tmux** — a Claude Code session becomes the manager; it spawns and supervises Claude Code / Codex worker sessions, each in its own tmux window (`spawn_worker`, `ask_manager` / `answer_question`, `worker_done`).
- **MCP server + session hooks, no daemon** — all fleet state is plain JSON under `~/.claude/dockwright/`; sessions self-register via SessionStart/Stop hooks.
- **Stale monitor** — an external watchdog for mid-turn stalls, silently finished workers, idle tabs (auto-close + resume), and rate-limited accounts.
- **Account pooling & auto-switch (optional)** — headroom-weighted spawn placement across multiple `/login` accounts, automatic pointer flip on rate-limit bricks, and manager takeover-recovery when the manager itself is limited.
- **Artifact & pipeline store** — durable per-task specs/plans/results (`artifact_put`), with `pipeline_status` replaying the whole board after any crash or manager recreation.
- **The compose seam** — generic agent cores + your private overlay drop-ins + `dockwright.toml` vars; the shipped product stays generic.
- **Offline evals harness** for the code-review verifier: 24 labeled cases, `sonnet` default tier, `--model opus` for the production-faithful tier, `--dry-run` at $0.
- **Installer & lifecycle CLI** — `setup.sh`, `dockwright doctor`, `dockwright init`, `dockwright compose`, `dockwright spend-report`, `dockwright migrate-state`, and provenance-driven `dockwright uninstall`.
- **Optional self-improvement module** (Gardener + selffix) — session retrospectives digested into ranked improvement proposals; ships inert, off by default.

Licensed under Apache-2.0 (see `LICENSE` and `NOTICE`).
