# Changelog

User-facing release notes for dockwright. Entries describe what an adopter
gets, not internal development history.

## v1.3.0 — 2026-07-21

- **Zero-touch headless workers.** Worker spawns now default to a scoped headless permission preset (auto permission mode + the protocol MCP allowlist + config-derived writable directories), so a headless/no-human fleet runs without stalling on permission dialogs. The stale monitor detects and pages a pane sitting on an approval prompt, and headless `claude -p` lanes (retrospective/distill) are locked down to disallow `Write`/`Edit`/`NotebookEdit`.
- **Clickable fleet menu.** The tmux status row carries a one-click fleet menu that opens STAYOPEN, so pointer motion can no longer dismiss it; the decorative worker-count chip was removed in favor of a single clean click target.
- **Correct single-account operation.** The account layer is now correct for a single-`/login` user with no phantom pool behavior; multi-account pools additionally get an MCP-config refresh and a deploy-time `dockwright accounts-sync` reconcile.
- **Opt-in manager skip-permissions.** A manager launch can be brought up ungated via `DOCKWRIGHT_MANAGER_SKIP_PERMS=1` (env-gated, off by default) for sanctioned host-driver / classifier-outage windows; every manager launch now carries remote control.
- **Gardener actuation** (self-improvement module, still off by default). An approved proposal can now be applied as a git patch, gated by an eval-gate that validates what the diff actually touches — not what it declares — with labeled-failure mining feeding the digest.
- **Faster manager boot.** A new `dockwright boot-brief` emits memory + notebook pointers at manager startup instead of inlining their full contents, keeping large memory/notebook state out of the boot context window.
- **Notification hygiene.** Quieted three false-positive notification paths (manager-end handoff, silently-finished holds, and gardener windows), and stopped state migration from manufacturing legacy compatibility symlinks.

## v1.2.0 — 2026-07-16

- **Linux is now a first-class install target.** Fresh-Linux installs work end-to-end: worker spawn picks a portable interactive shell instead of hardcoded `zsh`; the macOS-only awake-clock call is guarded with a portable fallback; the session id reaches workers via SessionStart context injection instead of a shell echo that tripped expansion guards; and the GNU-incompatible `stat` mtime probe was replaced with the portable `date -r` form.
- **Deterministic Linux ghost-worker reap fixed** (5-part). Session registration resolves the real claude/codex session pid past Linux's short-lived hook intermediate; stale-record prunes only delete a dead-pid record when its tmux pane is gone too; `worker_done` self-heals from the claimed assignment when the active record was reaped, so a finished worker's completion signal still lands; pruned records always leave a forensic spend-ledger line; and the stale monitor pages `ORPHAN_WINDOW` for worker panes with no backing record.
- **Fresh-install hardening.** `setup.sh` creates the worker home directory (new `dockwright ensure-worker-home`), so a bare `spawn_worker` no longer falls back to the manager's cwd; setup fails fast on a missing or too-old `python3`; a stale or broken `.venv` is recreated instead of silently reused.
- **Headless worker permission preset.** Ships a scoped settings preset for headless/no-human worker spawns — protocol MCP tools allowed, config-derived `additionalDirectories` injected by the new `finalize-presets` setup step — instead of blanket permission-skipping.
- **Self-improvement pipeline opt-in CLI.** New `dockwright selffix enable|disable` (wires/removes the SessionEnd retrospective hook) and `dockwright gardener enable|disable [--lane digest|frontier|all]`, replacing hand-editing settings.json. The gardener digest lane refuses to enable without selffix; enable is gated on `launchctl` availability and exits non-zero when the launchd bootstrap fails; uninstall strips the selffix hook.
- **Manager guidance hardening.** Evidence before any worker kill (capture the pane first — a live pane with no record is a registration failure to root-cause, not a session to kill); headless spawns must use scoped permission presets; never pre-downgrade a model dispatch to dodge a safeguard flag — the runtime auto-fallback is the correct outcome.
- **CLI polish.** Bare `dockwright doctor` works (arguments defaulted); expired pending assignments are swept on the spawn path.

## v1.1.0 — 2026-07-15

- **Offline investigation evals harness** (`evals/investigation/`) — regression evals for the investigation behavior stack: 6 committed file-fixture incident cases (fabricated-evidence, stale-metric echo, red-herring, data-shape traps, plus abstention cases), scored by deterministic gates and an LLM judge. Run with `python -m evals.investigation.run_eval` (`--dry-run` costs $0); point it at your own investigation skill via `DOCKWRIGHT_INVESTIGATE_SKILL`.
- **Value-grounding checker** (`value_grounding.py`, deployed to `~/.claude/scripts/`) — mechanically verifies that numbers, versions, and ids asserted in a report actually appear in the session's captured tool outputs, catching fabricated or stale-echoed evidence. Consumed by the evals gates and available as a CLI.
- **Asset validator** (`asset_validator.py`, deployed to `~/.claude/scripts/`) — warn-only structural validation for `~/.claude` assets (rules/skills/commands/agents/flows): missing TRIGGER lines, name/frontmatter mismatches, dead cross-references, dead deprecation-alias targets. Runs from the auto-commit hook on staged files; `--all` for a full audit.
- Desktop notification titles now say "dockwright" (previously "orchestrator").
- Internal naming cleanup: remaining cosmetic "orchestrator" references renamed to dockwright.
- The README is now maintained in the development repo and refreshed on every release export.

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
