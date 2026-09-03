# Changelog

User-facing release notes for dockwright. Entries describe what an adopter
gets, not internal development history.

## v1.5.0 — 2026-09-03

- **Four self-improvement gates are gone: the corpus-watch trigger gate and the headless spawn audit, plus two development-only CI gates.** Both named gates were v1.4.0 headline features. Nothing replaces them in reduced form and no check moved elsewhere. **If you ran `corpus-watch-install.sh`, clean it up by hand — `setup.sh` will not.** It copies files in and never deletes a script that left the release, and it never touches launchd, so both the plist and its gate script survive an upgrade and the loop keeps firing and keeps spending. Remove the scripts, which is what stops the spend:

  ```
  rm -f ~/.claude/scripts/{corpus_watch_gate.py,corpus-watch-run.sh,corpus-watch-install.sh,headless_scan.py}
  ```

  Then, if the installer had already bootstrapped the agent, unload the plist too (substitute your own `[loops] label_prefix` for `com.dockwright`):

  ```
  launchctl bootout gui/$(id -u)/com.dockwright.corpus-watch
  rm -f ~/Library/LaunchAgents/com.dockwright.corpus-watch.plist
  ```

  Only an operator who deliberately ran that installer is affected: the loop shipped `status: pending-install` and never armed itself on a plain clone-and-install. `dockwright uninstall` finds the plist by label prefix and removes it, so a full uninstall needs no hand step.
- **`dockwright.__version__` is gone. Use `importlib.metadata.version("dockwright")`.** The attribute had read `"0.3.0"` since 2026-07-07 and never tracked the real version, so it shipped saying `0.3.0` at v1.0.0 through v1.4.0. Anything you built on that value was working from a wrong number. `importlib.metadata` has always returned the right answer and now it is the only answer. Removed rather than corrected because nothing consumed it and a hand-maintained parallel copy of the version is what caused the problem.
- **A worker reports to its manager through `worker_done`, not into its own pane.** A worker that took a turn after calling `worker_done` used to leave that output where only a human watching the pane would see it. Measured before the change: none of 70 completion records carried a report, while 5 of 13 live worker panes did.
- **A peer manager's message names its sender.** `send_manager_to_manager` types straight into the recipient's pane, so a peer's message arrived indistinguishable from the operator typing. It now carries a sender header, matching the `[MANAGER]` prefix `send_manager_to_worker` has always used.
- **The fleet reads a worker's own transcript to decide whether it is working.** A worker whose transcript was written in the last 120 seconds counts as working — for the fleet icon and for the idle auto-close sweep, which previously judged both from a state field that lags. An unmounted volume now counts as reapable rather than blocking the sweep.
- **Idle fleet workers sort by last activity, freshest first**, instead of alphabetically. Active workers keep their stable order so they do not jump around while you read the list.
- **A re-tasked worker's next finish reaches its manager.** A worker given new work after `worker_done` lost its following silent turn-end permanently, because a completion less than 10 minutes old suppressed it and suppression consumed the signal.
- **`loops_status.py` reports a running loop correctly when `[loops] label_prefix` does not start with `com.`** It read `launchctl list` through a `com.`-only filter, so it called such a loop `not loaded` and flagged drift against it. The shipped default prefix is `com.dockwright`, so this only affected an install that changed the prefix.
- **The manager notebook now ships with the skill that maintains it.** New `dockwright-notebook-hygiene` skill. A manager may no longer state a notebook entry as current fact without re-deriving it in the same session, and the notebook is verified and pruned before every handoff and every manager close. The mandate already shipped; the skill carrying it did not.
- **Gardener run notifications address one manager instead of the whole machine.** A routine gardener run fired up to four desktop notifications, and a desktop popup has no addressee.
- **A recap item that waits on you carries its own context** — what the thing is, what your answer unblocks, the options, and what happens if you do nothing — rather than a bare ticket key or PR number.
- **The shipped code carries no comments or docstrings.** Roughly 10,000 lines of comment and docstring text left `src/` and `deploy/`. Tests that asserted over other tests, or over source text instead of behavior, went with them. This is a house style. Where a comment was carrying meaning the code did not carry itself, that meaning is now a gap.

## v1.4.0 — 2026-08-25

- **Per-session spend attribution.** `dockwright spend-report <worker|session-id>` breaks one session's cost down from its own transcripts, with cache-read called out as the dominant component rather than buried — so an expensive worker is identifiable instead of merely suspected.
- **`dockwright lanes` — tell a quiet fleet from a dead one.** Silence is both the healthy state and the failure mode of a monitor lane. This reports whether a manager can still hear its fleet, which previously could not be distinguished from everything being fine.
- **An adversarial reviewer agent.** `dockwright-reviewer` reviews a PR whose author is the dispatching session: reads the diff, runs the tests, returns a verdict. ⚠️ Its `tools:` withholds write access, and the agent file states plainly that this was measured NOT to bind — read-only is a convention you state in the brief, not a guarantee the runtime enforces.
- **Headless spawn audit.** `headless_scan.py` finds every `claude`/`codex` spawn in a tree and reports which ones nothing vouches for. It detects convention violations and enforces nothing; the file says so in its own first lines.
- **Typed proposals with a deterministic executor.** A machine-written proposal file is validated item by item before anything runs, so the authorization step is separated from the acting step.
- **Corpus-watch trigger gate**, LLM-free: watches for direct edits to gate-mapped instruction surfaces, closing a wiring hole where nothing invoked the eval gate.
- **Shadow graduation ledger.** A capability running in shadow — drafting what a human disposes of — is armed with its graduation criteria *before* data collection, so the bar cannot be moved after seeing the results.

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
