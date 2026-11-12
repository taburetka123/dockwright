# Changelog

User-facing release notes for dockwright. Entries describe what an adopter
gets, not internal development history.

## v1.6.0 — 2026-11-12

### Before you upgrade

- **A Claude worker spawn must name its model and effort.** `spawn_worker` and `spawn_child` refuse a Claude worker launch whose arguments lack `--model` or `--effort`, and `[spawn] worker_model` is no longer read. `dockwright assign-to-manager` needs both flags too, and `resume_worker` refuses a record that carries neither unless you pass `model` and `effort`. The shipped manager core passes both. A script or agent of your own that spawned workers without them now gets an error that names the two flags.
- **`artifact_put` takes a file, not text.** Pass `content_path=<absolute path of a file>`: the store copies that file's UTF-8 text, up to 1 MiB, as the body. A call that passes only `content` is refused with a hint that names `content_path`. A call that passes both reads the file. `artifact_get` and `artifact_view` return `content_path`, a copy of the body outside the store, instead of the body. Other text arguments of the MCP tools stop at 8 KiB, except the texts the engine stores verbatim: an ask, a question, an answer, a topic message.
- **The worker protocol tools have new names. The old names still work in this release.** The new names are `spawn_child`, `ask_parent`, `child_done`, `wait_for_child`, `answer_child`, `list_children`, `send_to_child`, `kill_child` and `get_child_tail`. `spawn_worker`, `ask_manager`, `worker_done`, `wait_for_worker`, `answer_question`, `list_workers`, `send_manager_to_worker`, `kill_worker` and `get_worker_tail` are deprecated aliases of them. Move your own agents and skills to the new names.
- **New default models, named through family tokens.** `@opus`, `@sonnet`, `@fable` and `@haiku` resolve through the packaged `models.toml` to Opus 5.5, Sonnet 5.5, Fable 5.2 and Haiku 5.5. A fresh `dockwright init` starts managers on `@fable[1m]` at effort `medium`. An existing `dockwright.toml` that pins a concrete model id keeps it.
- **Managers launch in a manager home.** Every manager launch starts in `[paths] manager_home` (default `~/projects/work/manager`, overridden by `DOCKWRIGHT_MANAGER_HOME`), and `setup.sh` creates the directory.
- **The dockwright tmux server drops inherited GitHub tokens.** It unsets `GH_TOKEN`, `GITHUB_TOKEN` and direnv's state variables, so no pane inherits the token of the shell that started the server. Authenticate `gh` in its own config instead. `dockwright doctor` and `dockwright lanes` report a leaked token.
- **Removed with no alias.** `spawn_worker` refuses `tier`: pass `--model` and `--effort` instead. The lane pages `STALE_QUESTION` and `AUTOCLOSED` are gone, and several lane lines are reworded, so a script that parses them needs a look. `FINISHED_SILENTLY` reads `CLAUDE_ORCH_SILENT_FINISH_GRACE_SEC` (default 1800 s), and `CLAUDE_ORCH_TURN_END_GRACE_SEC` no longer governs it. The gardener digest no longer reads `GARDENER_K`. 37 keys left `deploy/agents/vars.defaults.toml`, and a value you set for one of them in `[agent_vars]` no longer reaches an agent core. A Codex spawn is refused until `[codex] allowed_roots` names its directory, and Codex `extra_args` may not carry `-C`, `--cd`, `--add-dir` or `--`. `/dockwright-threads` prints `dockwright board`. `bootstrap-recreate.sh` exits 1 when it finds no `dockwright` binary. `dockwright install-codex-skills` still runs as a deprecated alias of `dockwright translate-codex`, which `setup.sh` now runs.
- **Sessions that dockwright launches start without six built-in tools:** `Artifact`, `SendFeedback`, `Workflow`, `AskUserQuestion`, `ReportFindings` and `ScheduleWakeup`. A manager keeps `ScheduleWakeup`.

### New

- **Pipeline engine.** A work can run on a schema you write: a TOML file of nodes and edges under `[pipeline] schemas_dir`. `pipeline_start` opens a work, the engine spawns one session per node, and `pipeline_advance` moves the token. `pipeline_status` replays the work and says what it waits on. `pipeline_rework` opens a new round and `pipeline_close` closes the work. An edge can fork one row per repo and join the rows, return work to an earlier node, or fire on a PR merge, an approval or an issue-tracker status without a model turn. A node names its own model, effort, profile and reviewer children. `dockwright schema validate` and `dockwright schema show` check a schema. No schema ships, so the engine does nothing until you write one.
- **Event layer.** A session subscribes to an event and sleeps instead of polling: `event_subscribe`, or `dockwright event subscribe`. The sources are GitHub pull requests and Actions runs, deployments, an issue tracker, the clock, a chat app's macOS notifications and operator commands (`exec`). Rules under `[events.rules]` queue, page, reply to or start work on what the sources see. `dockwright event gate` says whether a rule acts now.
- **Fleet daemon, off by default.** With `[daemon] enabled = true`, `setup.sh` installs a launchd job that runs `dockwright daemon tick` once a minute. The daemon reads every manager's queues and polls the event sources. With `[lanes] in_manager = false` it also takes the lane monitors over from the manager tabs and writes each page into that manager's outbox. With `enabled = false`, `setup.sh` removes the job.
- **A board of what waits on you.** `dockwright board` prints the pending questions, the sessions held at a gate or a permission dialog, and the works that wait on a person. `dockwright board --serve` serves the same screens, topic pages and answer forms on localhost, in a light and a dark theme that meet WCAG 2.2 AA. `topic_message` and `dockwright topic message` write to a work from outside the fleet. `ask_engineer` records a manager's question on the board and returns at once.
- **Chat archive, off by default.** With `[chat_archive] enabled = true`, dockwright keeps your direct messages, group messages and mention threads on disk for 90 days. `chat_search`, `chat_read` and `chat_conversations` read the archive.
- **A worker runs child sessions of its own.** `spawn_child` starts a child as an interactive Claude session (`claude-tui`), a headless `claude -p` (`claude-exec`) or a headless `codex exec` (`codex-exec`, only under `[codex] allowed_roots`). The call names the child's role, its mode (`report` or `interactive`), its model and its effort. Children show on the tmux bar, in the stale monitor and in the spend record.
- **Role renders.** A session loads the core rules plus the rules of its role. `deploy/roles/` holds the generic role cores and fragments, `dockwright compose` renders one file per role and profile, and `compose --check` measures every render against its `[ceilings]` entry.
- **Pause the whole fleet.** `dockwright pause` stops every manager and worker and writes a manifest. `dockwright resume` brings them back on the same transcripts. `dockwright revive --resume` brings back managers that a reboot killed, on their own transcripts.
- **Usage caps per account.** `<state root>/usage-caps.toml` caps a pool account per five-hour or seven-day window. While a cached reading of any capped account sits at or over its cap, the fleet refuses new spawns until that window resets or you edit the file. `force` does not lift a cap. The file is read on every check, so a change needs no redeploy.
- **Fleet spend.** `dockwright spend-fleet --days N` prices every API call in a date window across every pool account. `dockwright spend-report` now reads Codex sessions too.
- **Permission dialogs and safeguard pauses get seen.** A `PermissionRequest` hook marks every permission dialog, the stale lane pages `APPROVAL_PROMPT` once per dialog, and the manager reads and answers it with `get_worker_pane` and `answer_permission_prompt`. The lane clears Claude Code's "Session paused" safeguard dialog on a worker and pages `SAFEGUARD_PAUSE`.
- **Your own pane stays yours.** No dockwright automation types into a pane whose input box holds text. A line you type into a worker's pane marks the worker attached for 40 minutes, and `kill_child` refuses to close it in that time. `set_human_attached` drops the mark.
- **Model limits.** A worker whose turn ends on a per-model credit banner switches to a fallback model and resumes. A Fable spawn goes to a pool account with Fable left, and launches on Opus when no account has any. `dockwright account-hold` holds an account by hand.
- **Smaller additions.** `dockwright tech-debt` lists a tech-debt store, filters it by tag and runs each entry's check. `dockwright notebook mark` and `dockwright notebook check` make a manager handoff wait for a notebook pass. `get_worker_summary` returns the worker's `worker_done` report. A worker that stops with nobody owning its next step gets one nudge, at most three workers per scan. A closed worker's SessionEnd hook stops the browsers its session launched. `dockwright doctor` names a pool account that holds no MCP OAuth token for a server you require.

### Changed

- **tmux bar.** The status bar grows up to five lines as managers and workers need room, and shrinks back. A chip folds every worker row into one line. The expanded idle list folds back after 10 minutes. The manager statusline shows proposals and todos where the worker counter stood, and the per-model weekly usage window.
- **Fewer repeated pages.** A chat-archive or event-poll failure pages only after 3 failed attempts in a row over 10 minutes of awake time. `STALE_QUESTION` is gone, and a new question still pages once. `FINISHED_SILENTLY` waits 30 minutes and holds while the worker still runs a background shell. An idle auto-close prints no line. A park that only repeats the previous one goes to the work's journal.
- **Worktree prune removes less.** The prune loop removes a worktree only when its head is merged into `main` and the tree is older than the age window.
- **Manager memory.** Each manager memory file is capped at 400 words, and a manager boot reads three of them.
- **The gardener digest runs weekly**, and a proposal can supersede pending proposals.
- **`setup.sh`** finds a Python 3.11 or newer interpreter when the `python3` on `PATH` is older, and derives worker presets from the presets it deployed.
- **MCP tool descriptions fit in 2048 characters**, and each parameter carries its own rule in its own description.
- **The shipped agent cores are shorter.** Procedures moved into the `dockwright-orchestrator-guide` skill, and every prohibition stays in its always-loaded file.

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
