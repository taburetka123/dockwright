# Loops registry — the declarative loop-master

Every standing background loop on this machine, declared as a structured block that
`tests/test_loops_registry.py` reconciles against the machine (plist census, launchctl state,
program paths, settings hooks) and `~/.claude/scripts/loops_status.py` reads for the fleet health
report. This registry + launchd as the runtime supervisor + the convention below IS the
loop-master: a **structured + machine-checked** registry rather than a prose table — the
structure is what lets the pytest reconcile declared intent against the machine.

Deployed to `~/.claude/dockwright/loops-registry.md` by setup.sh; the source of truth is
`deploy/loops-registry.md` in this repo. Each new loop adds a block here **in the same PR that ships
it** — the registry pytest fails on unregistered plists. Authoring router: the § Discipline
section below applies in any session that touches a launchd plist / recurring job, whatever repo
it is in.

## Discipline (any session touching a loop)

Applies when creating, modifying, debugging, or removing a launchd plist, login agent, recurring
background job, scheduled `claude -p` automation, or a hook-triggered loop on this machine — in
ANY repo ("why didn't loop X run?" questions included). Every standing loop is declared in this
registry; the registry pytest (`tests/test_loops_registry.py`) fails on unregistered plists (the
`<label_prefix>.*.plist` census) and on intent-vs-machine drift — so:

- **New loop** → add a ```loop block to this registry in the same change, following the
  convention below: LLM-free gate, stop-file kill switch (`~/.claude/dockwright/<loop>-stop`), JSONL ledger,
  decision log, rate cap, visible-first, scoped `--settings` (NEVER
  `--dangerously-skip-permissions`), any `claude -p` spawn takes the shared run-lock or the block
  says why not, and any `claude -p` / `claude --print` carries an explicit concrete `--model` —
  a missing flag rides whatever the CLI's global default currently is, and a bare alias resolves
  to whatever that release calls the latest model of that tier. Neither is something the loop
  controls, and the tier aliases include the largest model, so an alias is not a cheap way to
  stay off it. Any `claude -p` lane that opens a session also carries the role render from
  `dockwright role-args <Role> --launch lane --cwd "$PWD"` (`--render-only` on a locked-down
  lane), beside `--model`; a visible lane that passes `--agents-file` starts `claude` through
  `dockwright launch`.
- **Pausing / retiring / re-enabling a loop** → flip the block's `status` (+ why) in the same
  change. That field is the fleet's only intended-state record.
- **Debugging "why didn't X run?"** → `python3 ~/.claude/scripts/loops_status.py` first
  (launchctl state, stop files, event freshness, drift flags).

## The convention (binding checklist for any NEW loop)

Cloned from the Gardener, the reference loop:

1. **LLM-free gate before any model spend** — the scheduled tick is pure file/pid arithmetic; a
   model session spawns only when the gate's domain condition arms.
2. **Stop-file kill switch** — `~/.claude/dockwright/<loop>-stop`; the tick exits before scanning. Stopped
   means stopped (even `--force` refuses where a force exists).
3. **JSONL ledger with typed events** — `~/.claude/<loop>/ledger.jsonl`, append-only.
4. **Gate/check decision log** — one line per tick (`gate.log` / `check.log`), so the loop is
   traceable post-hoc even when it does nothing.
5. **Rate cap and/or cooldown** — bound the worst-case burst a wedged state could produce.
6. **Visible-first before headless** — a model run lands in an observable tmux window until the
   headless contract is spiked and deliberately flipped.
7. **Scoped permissions, never `--dangerously-skip-permissions`** — scoped `--settings` (+
   PreToolUse guard where file writes are possible); gardener-run.sh shows how.
8. **Any `claude -p` spawn takes the shared run-lock (`~/.claude/locks/analyst-run.lock`) — or
   this loop's block says why not.** The fleet-spend invariant must be a stated default, not an
   accident of two loops (counterfactual review RESHAPE-3).
9. **Registry block in the same PR**, with `last_verified` set from machine checks, not memory.

### Is it a new loop, or part of the Gardener? (classification rule)

> 1. Own trigger condition or own evidence base? **No** → a new §6 observation source of the
>    existing digest. **Yes** → its own registered loop (registry block, full convention).
> 2. Output is meta-system proposals passing FR-8? → write the Gardener artifact contract into
>    `proposals/pending/` and ride the review sitting ("sitting-fed producer"). Output acts on the
>    world directly? → an "actuator loop"; it owns its actuators and never enters the sitting.

Under this rule "a Gardener mode" is not a category — the Gardener is one loop, not a framework.

### Flip triggers (when to build more than this registry)

Adopted from the counterfactual architecture review; T8 is
the research's original trigger — one of nine, not the only one.

| # | Class | Trigger (checkable condition) | Then build |
|---|---|---|---|
| T1 | spend | Rate-limit signature in ≥2 distinct loops' outputs within one hour | fleet spawn budget — global semaphore in a shared wrapper |
| T2 | spend | ≥5 concurrent fleet-loop `claude -p` processes observed once | same as T1 |
| T3 | consistency | A new loop ships after this registry exists, missing any five-tuple element | runner becomes the mandatory entry point (checklist-as-API) |
| T4 | testability | Second gate bug in a class another loop's gate already solved (poller TZ bug = #1) | extract shared gate helpers with tests |
| T5 | observability | Third dark-loop incident (>7d undetected; ticket-cleanup = #1, poller = #2 if unintended) OR loops-status unshipped 90 days after B6 acceptance | ledger envelope + status owned by one runtime |
| T6 | observability | One cross-loop debugging session burns >30 min correlating ≥3 of the log schemas | one `{ts, loop, event, detail}` envelope |
| T7 | scale | ≥8 launchd loops total, or ≥6 autonomous `claude -p` loops | full runtime |
| T8 | duplication | Third consumer needs the run-lock | superseded if runlock extraction lands at n=2 |
| T9 | platform | Anthropic per-account concurrency becomes billed or hard-capped | global chokepoint mandatory |

## Schema

Machine-checked fields (the pytest + loops-status parse these; keep values literal):
`name`, `label` (launchd label, rendered as `<label_prefix>.<loop-name>` at install time —
`dockwright.toml` `[loops].label_prefix`, default `com.dockwright`; this operator's
`~/.claude/dockwright.toml` sets `com.dockwright` — or `none` for hook-triggered),
`status` + `status_why`,
`runtime_program_path`, `hook_command` (hook loops only), `ledger_path`, `kill_switch` (`none`
allowed but explicit), `log_paths`, `event_paths` (files loops-status stats for freshness),
`max_silence_hours` (`none` = no freshness check), `last_verified`. Prose-but-required (five-tuple
completeness is asserted): `trigger`, `gate`, `run_contract`, `permissions_mode`, `source_path`,
`deploy_mechanism`.

`status` semantics (the reconciliation point — the checker fails when intent and machine disagree):

- `live` — plist present + label loaded (hook loops: hook line present in settings.json); program
  path exists.
- `paused` — deliberately unloaded/unwired; label NOT loaded (hook line absent); program path
  still exists. Re-enabling means flipping this field back in the same change — that's the teeth.
- `retiring` — removal in progress; no machine assertions (transitional, flip to `retired` when
  done).
- `retired` — plist absent, label not loaded.
- `pending-install` — shipped in a repo but the installer hasn't run on this machine; no machine
  assertions. Flip to `live` after installing.

Maintenance: the census is generated, never typed — the pytest globs
`~/Library/LaunchAgents/<label_prefix>.*.plist` (`config.loop_label_prefix()`, ALSO globbing the
legacy hardcoded prefix — `tests/test_loops_registry.py` `LEGACY_LABEL_PREFIX` — so plists installed
before `dockwright.toml` had a `[loops]` key stay covered) and fails on any plist without a block here. Run
`python3 ~/.claude/scripts/loops_status.py` for the live fleet view.

## The loops

### selffix

```loop
name: selffix
label: none
hook_command: selffix-trigger.sh
status: pending-install
status_why: ships pending-install; opt in on a fresh install with `dockwright selffix enable` (wires the SessionEnd hook), then flip live via [loops.status_overrides] in dockwright.toml
trigger: ~/.claude/settings.json SessionEnd hook
gate: embedded Python signal detect (HIGH only: configured [gardener] high_skills, gh pr create, >=5 edits, agent=manager, pushback>=1 EN+RU, harsh-language EN+RU), 60-min dedup hash, findings-exist skip, 14d prune; limit-brick check, and `dockwright usage-cap --paused` while an operator usage cap is breached, enqueue to ~/.claude/dockwright/selffix/retry/ instead of spawning, and the gardener tick holds that queue while either holds; [modules] gardener=false no-ops the trigger (module-off)
run_contract: nohup selffix-run.sh → claude -p "/dockwright-selffix …", env-stripped, 25m TERM→KILL watchdog; takes the shared run-lock; failed runs (non-zero exit / <200B stub / lock-timeout) enqueue ONE durable retry consumed by the gardener-gate tick
permissions_mode: user-default; skill contract is stdout-only (never Write/Edit)
ledger_path: ~/.claude/dockwright/selffix/trigger.log
kill_switch: `dockwright selffix disable` (removes the settings.json hook line); queued retries in ~/.claude/dockwright/selffix/retry/ still drain via the gardener tick — also touch ~/.claude/dockwright/gardener-stop or empty the queue for a full stop
runtime_program_path: ~/.claude/scripts/selffix-trigger.sh
source_path: deploy/scripts/selffix-trigger.sh
deploy_mechanism: setup.sh cp (deploy/scripts/*.sh → ~/.claude/scripts/)
log_paths: ~/.claude/dockwright/selffix/trigger.log, ~/.claude/dockwright/selffix/findings/
event_paths: ~/.claude/dockwright/selffix/trigger.log
max_silence_hours: 72
last_verified: 2026-07-22
```

`last_verified` above is backed by an actual `loops_status.py` run, not a memory stamp. What the run
SHOWED: `STALE (141h since last event)` while the hook was wired and findings were being written
normally — i.e. the gate was permanently-red-by-construction, because `trigger.log` was DEBUG-gated.
That gate is now removed at source (`selffix-trigger.sh` `log_line`), so the OUTCOME line is written
on every fire; that property is established by the code and by
`tests/test_selffix_ledger_gate.py::test_outcome_line_written_with_debug_off`, NOT by the run above.
A machine converges on its next `setup.sh` + first SessionEnd; until then `loops_status.py` on it
still reports STALE.

`72` is sized off the loop's OWN observed fire intervals — `trigger.log` timestamps across its
debug-ON windows, which is the only population that measures when this hook actually fires. Worst
real gap there is 32.5h (2026-06-20→06-22), then 31.2h and 26.9h, excluding the two known dark
windows (403.8h severed hook, 583.9h debug-off). 72h is ~2.2x the worst observed and also survives
a weekend-plus with the machine off. (48h was the applied value and would have been ~1.5x — thin
enough that an ordinary away-period trips a false STALE, which is the same "always red, so ignore
it" failure this entry exists to fix.) The DEBUG flag now only adds verbose extras (`prune`
counters, run/gate lifecycle) on top.

### gardener-gate

```loop
name: gardener-gate
label: {prefix}.gardener-gate
status: pending-install
status_why: ships pending-install; opt in with `dockwright gardener enable` (installs the launchd gate; needs selffix enabled), then flip live via [loops.status_overrides] in dockwright.toml
trigger: launchd StartInterval 3600 (no wake catch-up)
gate: gardener_gate.py — [modules] gardener=false no-ops the gate (module-off) → stop file → shared run-lock pre-check → selffix retry pre-step (one queued retro per tick, retry-once, deferred while limit-bricked; gardener-stop pauses retry consumption too; a retry-spawning tick defers a due digest to the next tick) → 6h cooldown → 3/week attempt cap (--force bypasses cooldown, cap and the 7d interval) → 7d interval since the last ok digest, with ≥1 unreviewed finding
run_contract: visible tmux window via gardener-run.sh; digest-only writes; 60m join (frontier 45m) + 15m grace, never kills the tab; takes the shared run-lock atomically; the selffix retry spawn additionally gets [gardener] retry_env from dockwright.toml (KEY=VALUE, retry spawn only), and selffix-run.sh runs ${DOCKWRIGHT_CLAUDE_BIN:-claude}
permissions_mode: scoped --settings (Write/Edit allowed only under ~/.claude/dockwright/gardener/, substrate denied) + PreToolUse write-guard hook
ledger_path: ~/.claude/dockwright/gardener/ledger.jsonl
kill_switch: ~/.claude/dockwright/gardener-stop
runtime_program_path: ~/.claude/scripts/gardener_gate.py
source_path: deploy/scripts/gardener_gate.py
deploy_mechanism: setup.sh cp + gardener-install.sh (plist)
log_paths: ~/.claude/dockwright/gardener/gate.log, ~/.claude/dockwright/gardener/launchd-out.log, ~/.claude/dockwright/gardener/launchd-err.log
event_paths: ~/.claude/dockwright/gardener/gate.log
max_silence_hours: 26
last_verified: 2026-06-13
```

### gardener-frontier

```loop
name: gardener-frontier
label: {prefix}.gardener-frontier
status: pending-install
status_why: ships pending-install; opt in with `dockwright gardener enable --lane frontier` (or --lane all), then flip live via [loops.status_overrides] in dockwright.toml
trigger: launchd StartInterval 86400 (daily tick)
gate: frontier_gate.py — [modules] gardener=false no-ops the gate (module-off) → stop file → shared run-lock → 48h retry cooldown → armed marker (first run is a human decision; installer arms run #0) → 7d interval due
run_contract: gardener-run.sh --lane frontier (shared run mechanism: tmux spawn, watchdog, write-guard, audit, postrun); web-heavy research sweep via /dockwright-gardener-frontier; takes the shared run-lock
permissions_mode: scoped --settings via gardener-run.sh (write-guard hook; writes only under ~/.claude/dockwright/gardener/)
ledger_path: ~/.claude/dockwright/gardener/ledger.jsonl
kill_switch: ~/.claude/dockwright/frontier-stop
runtime_program_path: ~/.claude/scripts/frontier_gate.py
source_path: deploy/scripts/frontier_gate.py
deploy_mechanism: setup.sh cp + gardener-install.sh (plist)
log_paths: ~/.claude/dockwright/gardener/frontier-gate.log
event_paths: ~/.claude/dockwright/gardener/frontier-gate.log
max_silence_hours: 72
last_verified: 2026-06-11
```

### worktree-prune

```loop
name: worktree-prune
label: {prefix}.worktree-prune
status: pending-install
status_why: ships pending-install; the operator flips it live via [loops.status_overrides] in dockwright.toml once worktree-prune-install.sh has run
trigger: launchd StartCalendarInterval daily 10:00
gate: two SEPARATE proofs, because removal and branch-deletion lose different things. REMOVAL: not on the operator hold list AND not `git worktree lock`ed AND no unfinished dockwright work claims it (a path the work recorded is the tree or inside it, or shares the tree's first directory under a scan root, or that directory carries the work's key; an open note, lane or fleet card's links.worktree claims the same way) AND older than the age window (default 14 days, WORKTREE_PRUNE_MIN_AGE_DAYS; created-at is the LATER of the worktree admin dir's first `logs/HEAD` reflog entry and that dir's `st_birthtime`, so a gc-emptied reflog and a reflog restored from backup both still date the tree, and an unreadable clock refuses) AND no interrupted git op (rebase/bisect/cherry-pick markers, which `status --porcelain` cannot see) AND — for a detached HEAD — commits contained in a tag or a SHA-confirmed remote ref (never a local branch, which this loop can itself delete) AND merged (no OPEN PR, and HEAD an ancestor of origin/main or a MERGED PR into main whose head is exactly HEAD; a PR closed unmerged or merged at an older commit keeps the tree) AND clean tree AND every gitignored entry is build output AND no live owner. BRANCH-DELETE keeps the original stricter proof: gh PR MERGED at exactly this head SHA or git ancestor, never main/master, never a detached HEAD. LLM-free
run_contract: python3 worktree_prune.py --apply, LLM-free, destructive (git worktree remove --force + local branch -D); DRY-RUN BY DEFAULT (--apply required to mutate); stop-file honored; rate cap 75/run; no model spawn so the shared run-lock does not apply. AGE GATE: a tree younger than WORKTREE_PRUNE_MIN_AGE_DAYS (default 14) is never removed. Both entrances to the window are fail-safe, but they are ONE resolution point rather than two independent barriers: the env var goes through _env_positive_int, and the run_prune parameter clamps a non-positive value back to the MIN_AGE_DAYS constant that same helper produced — so _env_positive_int is what makes both safe, and a module binding that stopped using it would disable the gate from either direction. worktree-prune-install.sh emits the knob into the plist EnvironmentVariables and preserves it across a bare re-run, so unlike WORKTREE_PRUNE_LSOF it is not inert under launchd. The clock is the worktree's git admin dir, never the worktree directory's mtime (measured to disagree with creation by up to 137 days, and the pruner's own run touches it) and never the branch's first commit (a fresh branch off main has none). An unresolvable age refuses with `age_unknown`, counted separately from `too_young` so a dead clock is visible in by_reason rather than reading as a quiet day; on a filesystem with no `st_birthtime` a tree whose reflog git has emptied is therefore never prunable, which is accepted deliberately in favour of the fail-closed direction. HOLD LIST at ~/.claude/dockwright/worktree-prune/keep.txt is fail-closed: the whole run STOPS rather than proceeding with nothing held if the file is missing/unreadable, if a literal entry points at a path that does not exist, or if a glob matches no existing path (an existing parent prefix is NOT enough). Holds therefore cannot be pre-registered for trees that do not exist yet. `gh` is resolved via WORKTREE_PRUNE_GH (env, plist) rather than PATH order; the run summary's gh_failed counter is how a misresolved gh becomes visible instead of silently reading every squash-merged branch as unmerged. `lsof` is pinned via WORKTREE_PRUNE_LSOF, defaulting to the absolute /usr/sbin/lsof because launchd's baked PATH omits /usr/sbin, and falling back to a PATH lookup when that path is absent. Unlike WORKTREE_PRUNE_GH this pin is a code default only: the installer neither emits it into the plist nor preserves it across a bare re-run, so an override must be set in the environment the job itself runs under. The OWNERSHIP gate is fail-closed on BOTH its proofs: if either the dockwright active-record scan or the lsof cwd scan is WHOLLY unavailable, the run refuses every candidate rather than proceeding on the surviving half. A scan is reported unavailable ONLY when lsof exits non-zero AND printed nothing: an exit-0 run listing no cwds, and a non-zero exit after partial output, are both trusted as complete, so a truncated or empty scan is NOT covered by that guarantee. ⛔ No field in the run summary discriminates a dead ownership scan from a healthy quiet day. `removed: 0` is produced by both, and `by_reason.owned` counts only candidates that cleared the nine earlier gates, so it stays in single digits and never approaches `scanned`. Diagnose a suspected dead scan by running the script by hand and reading the candidate list; an `owner_scan_failed` counter beside `gh_failed` is the durable fix and this loop does not have one yet. OPEN-WORK GATE: each run asks `dockwright pipeline active-paths --json` (binary: WORKTREE_PRUNE_DOCKWRIGHT, then PATH, then ~/.local/bin/dockwright, which launchd's baked PATH needs) for every unfinished pipeline work and every open note, lane or fleet card with the paths they claim, and asks again before each removal. Any failure of that answer — no binary, a non-zero exit, unparsable or wrong-version output, a malformed entry — refuses every candidate with `work_scan_failed` and writes the reason and the command's stderr to check.log. A failure at scan time sets the summary's `works_failed` to 1; a failure at re-verify leaves it at 0 and shows only as `toctou_work_scan_failed` in by_reason; the command itself exits 1 rather than print a partial list when a store record cannot be read
permissions_mode: n/a (LLM-free python)
ledger_path: ~/.claude/dockwright/worktree-prune/ledger.jsonl
kill_switch: ~/.claude/dockwright/worktree-prune-stop
runtime_program_path: ~/.claude/scripts/worktree_prune.py
source_path: deploy/scripts/worktree_prune.py
deploy_mechanism: setup.sh cp + worktree-prune-install.sh (plist)
log_paths: ~/.claude/dockwright/worktree-prune/launchd-out.log, ~/.claude/dockwright/worktree-prune/launchd-err.log, ~/.claude/dockwright/worktree-prune/last-scan.json
event_paths: ~/.claude/dockwright/worktree-prune/ledger.jsonl
max_silence_hours: none
last_verified: 2026-10-06
```

### bootlite-watchdog

```loop
name: bootlite-watchdog
label: {prefix}.bootlite-watchdog
status: pending-install
status_why: ships pending-install; the operator flips it live via [loops.status_overrides] in dockwright.toml once bootlite-install.sh has run
trigger: launchd StartInterval 3600 (tick half) + hooks.py session_end orphan flag (event half)
gate: bootlite_watchdog.py — stop file → active-record scan; orphan = live worker pid, subworkers and daemon-started nodes excluded, whose parent manager has no live session (per-parent_manager_name predicate; legacy null-parent workers count only when no manager is alive at all)
run_contract: notify (osascript) deduped per stretch (4h renotify, cap 6); optional one-shot checkpoint nudge per worker under CLAUDE_ORCH_AUTONUDGE=1; never spawns a model session, so the shared run-lock does not apply
permissions_mode: n/a (LLM-free python)
ledger_path: ~/.claude/dockwright/bootlite/ledger.jsonl
kill_switch: ~/.claude/dockwright/bootlite-stop
runtime_program_path: ~/.claude/scripts/bootlite_watchdog.py
source_path: deploy/scripts/bootlite_watchdog.py
deploy_mechanism: setup.sh cp + bootlite-install.sh (plist)
log_paths: ~/.claude/dockwright/bootlite/check.log, ~/.claude/dockwright/bootlite/launchd-out.log, ~/.claude/dockwright/bootlite/launchd-err.log
event_paths: ~/.claude/dockwright/bootlite/check.log
max_silence_hours: 26
last_verified: 2026-06-11
```

### daemon

```loop
name: daemon
label: {prefix}.daemon
status: pending-install
status_why: ships pending-install; setup.sh installs it while dockwright.toml [daemon] enabled = true (dockwright daemon sync), then the operator flips it live via [loops.status_overrides]
trigger: launchd StartInterval 60, RunAtLoad, ProcessType Interactive
gate: dockwright daemon tick — stop file → tick.lock (flock; busy → exit 0) → alive stamp, then a thread stamps daemon/alive.json every 30 s for the whole pass → the done, turn-ends and questions scans over every manager bucket under observe.lock → while [lanes] in_manager = false: stale_monitor.main per live manager, the events step ([events] enabled: poll each due source of tracker, deploy, time, github, chat and exec on its own thread under events/cursors/<name>/.poll.lock (busy: skipped this pass) and wait for all of them, store events, then run [events.rules] under events/.rules.lock, each rule on its own; the chat source reads chat/archive.db read-only on every chat poll for the rows captured since its cursor and writes chat/triggers/ while [chat_archive] enabled = true), one scan of artifacts/ (reconcile:scan), pipeline_reconcile.reconcile and note_check.run per live manager, reconcile of daemon-owned works, [schedules] starts, pages-monitor silence → notifications
run_contract: writes daemon/ledger.jsonl, daemon/ticks.jsonl, daemon/alive.json, topic journals (deduped lane_event kinds), events/log, events/subs, events/cursors under the state root; while [lanes] in_manager = false also writes the notify-outbox of every manager bucket, live or not (the pages monitor in the tab prints it; a closed tab's lines wait for the next manager of that name), the stale cursor .stale-emitted-<manager>.json, pipeline requests through the engine, and daemon/schedules.json; at most push_cap_per_hour macOS notifications an hour across topic waits and silent pages monitors; one start per schedule per pass; the stale checks wait, with a RECONCILE_ERROR page, while the job's environment lacks a [lanes.env] pair (the plists carry it after setup.sh); alive.json carries the running step and the awake time it started, so a fresh stamp means the tick process is alive and the daemon parent reads alive for the whole pass; the pages monitor prints DAEMON_STUCK when one step runs over 600 s of machine-awake time; each ticks.jsonl line of an interval pass carries steps, the seconds per step; the tick reads assignments/ and closed/ once per pass while their directory mtime does not change; spawns sessions only through the engine and runs no claude -p of its own; the chat source writes one trigger file per notification row under chat/triggers/ (ids and names, never text) and toggles chat/triggers/.wake while a row waits for its message in the archive, and reads chat/archive.db in mode=ro; deletes events/log day files older than [events] retention_days once an hour; types sent.reply lines into worker panes whose input line is idle or queued ([events.rules] action = "reply")
permissions_mode: n/a (LLM-free python)
ledger_path: ~/.claude/dockwright/daemon/ledger.jsonl
kill_switch: ~/.claude/dockwright/daemon-stop
runtime_program_path: none
source_path: src/dockwright/daemon.py
deploy_mechanism: setup.sh → dockwright daemon sync → daemon-install.sh (two plists, [lanes.env] in EnvironmentVariables) while [daemon] enabled = true; enabled = false + setup.sh boots both out
log_paths: ~/.claude/dockwright/daemon/ticks.jsonl, ~/.claude/dockwright/daemon/launchd-out.log, ~/.claude/dockwright/daemon/launchd-err.log
event_paths: ~/.claude/dockwright/daemon/ticks.jsonl
max_silence_hours: 1
last_verified: 2026-11-11
```

### daemon-wake

```loop
name: daemon-wake
label: {prefix}.daemon-wake
status: pending-install
status_why: ships pending-install; dockwright daemon sync installs it beside daemon while [daemon] enabled = true, then the operator flips it live via [loops.status_overrides]
trigger: launchd WatchPaths ~/.claude/dockwright/daemon/wake (touched by the engine on every question and done), ThrottleInterval 1, RunAtLoad false, ProcessType Interactive
gate: dockwright daemon observe — stop file → observe.lock (flock, non-blocking; busy → exit 0) → the three observing scans, repeated while the wake file was touched during the round, at most three rounds
run_contract: writes daemon/ledger.jsonl, daemon/ticks.jsonl (trigger wake), daemon/wake-heartbeat.json and, while [lanes] in_manager = false, the notify-outbox of every manager bucket; never acts, never spawns, never notifies; rate cap one start a second and three rounds a start
permissions_mode: n/a (LLM-free python)
ledger_path: ~/.claude/dockwright/daemon/ledger.jsonl
kill_switch: ~/.claude/dockwright/daemon-stop
runtime_program_path: none
source_path: src/dockwright/daemon.py
deploy_mechanism: setup.sh → dockwright daemon sync → daemon-install.sh (observe mode) while [daemon] enabled = true; enabled = false + setup.sh boots it out
log_paths: ~/.claude/dockwright/daemon/launchd-wake-out.log, ~/.claude/dockwright/daemon/launchd-wake-err.log
event_paths: ~/.claude/dockwright/daemon/ticks.jsonl
max_silence_hours: 168
last_verified: 2026-11-11
```

### chat-archive

```loop
name: chat-archive
label: {prefix}.chat-archive
status: pending-install
status_why: ships pending-install; dockwright chat-archive sync installs it while [chat_archive] enabled = true and runner = "launchd"; the operator flips it live via [loops.status_overrides]
trigger: launchd StartInterval 300 and WatchPaths ~/.claude/dockwright/chat/triggers, RunAtLoad true
gate: dockwright chat-archive pass — stop file → pass.lock (flock, non-blocking; busy → exit 0) → the trigger step, then the scheduled steps (DM search, mention searches, live threads, the hourly group sweep, the daily registry, prune) when the last full pass is older than every_sec
run_contract: reads the chat app's MCP with the freshest OAuth token Claude Code keeps in the Keychain for an account of [accounts] pool; writes chat/archive.db, chat/raw/, chat/ledger.jsonl (the decision log: one record per pass with every step's counts, pages, errors and held failures, never message text), chat/failures.json (the count of failed passes in a row per kind) and, on a failure, one CHAT_ARCHIVE_FAILED line an hour per kind (auth, http, parser, hand, crash) to every live manager's notify-outbox: http, hand and a keychain auth only after 3 failed passes in a row over 10 minutes of machine-awake time, a pass the machine slept through not counting, and any other auth, parser and crash on the pass they happen; never sends, never posts; rate cap 15 searches and 45 reads a minute; a 401 retries with the next freshest token, and only after every token got a 401 does one claude -p nudge run (--model from [chat_archive] model, --max-turns 2, one read tool allowed, every write tool disallowed); the claude hand (hand = "claude") runs headless and not visible-first because it only presses read tools and writes no text of its own; a step's independent reads go in batches of about 150 KB of expected responses (three thread reads or three conversation pages a run), and every later page of a conversation or thread is one run, so a full pass with N live threads and M active conversations runs at least (N + M) / 3 claude -p runs; neither claude -p run takes the shared run-lock because each is a bounded run of read-only tool calls (the nudge one call, the claude hand one batch) with no session, no text of its own and no write; a nudge runs at most once an hour (chat/nudged); both runs drop CLAUDE_CONFIG_DIR and use the default account; the manager runner (runner = "manager") is the same pass from the arm-lanes monitor
permissions_mode: n/a (LLM-free python); the nudge and the claude hand: --tools "" --strict-mcp-config --setting-sources "" --allowedTools <read tools> --disallowedTools <write tools> --no-session-persistence
ledger_path: ~/.claude/dockwright/chat/ledger.jsonl
kill_switch: ~/.claude/dockwright/chat-archive-stop
runtime_program_path: none
source_path: src/dockwright/chat_archive.py
deploy_mechanism: setup.sh → dockwright chat-archive sync → chat-archive-install.sh while [chat_archive] enabled = true and runner = "launchd"; enabled = false or runner = "manager" + setup.sh boots it out
log_paths: ~/.claude/dockwright/chat/launchd-out.log, ~/.claude/dockwright/chat/launchd-err.log
event_paths: ~/.claude/dockwright/chat/ledger.jsonl
max_silence_hours: 1
last_verified: 2026-10-27
```

Loops deliberately NOT here: the manager's `pages` plugin-monitor lane (event-driven, in-session, dies with the
manager by design; the daemon notifies when it falls silent, and `dockwright lanes` shows it); the four former
lanes `questions|done|turn-ends|stale`, which exit at once while `[lanes] in_manager = false` and otherwise run
in the tab as before; and any human-invoked skill (the human is the gate).

## Per-loop notes

### bootlite-watchdog — semantics

- **Two halves.** Event half: `session_end` for a manager that still parents live-pid workers
  writes `~/.claude/dockwright/orphans/<manager>.json` + notifies (clean closes are silent —
  `/manager-close` and takeover unlink the active record around the tab close). Tick half: hourly
  scan covering SIGKILL/power-loss where no hook ever fires.
- **Stretch dedup.** State entry per manager name: first notification immediately (or inherited
  from the event half's flag — no double-notify), renotify every `BOOTLITE_RENOTIFY_SEC` (4h) up
  to `BOOTLITE_MAX_NOTIFY` (6) per stretch. Healthy sweep clears state + flags once the manager
  name is live again, including flags whose stretch resolved before any tick saw it.
- **Nudge contract** (`CLAUDE_ORCH_AUTONUDGE=1` only): one typed message per worker per stretch —
  *checkpoint and finish durably* (commit/push, `worker_done`), explicitly NOT "resume your task";
  workers blocked in `ask_manager` are skipped.
- **Honest remedy.** Adoption of *named* orphans is manual today (`_backfill_legacy_workers`
  adopts null-parent records only; takeover needs a handoff a crash never wrote). Designed
  follow-up: surface `orphans/` flags at manager boot — the flag format already carries what a
  booting manager needs.

## Extraction trigger footnote

The run-lock extraction has LANDED (`deploy/scripts/runlock.sh` + `tests/test_runlock.py`,
deployed by setup.sh) — the counterfactual review's F1 (the selffix 2h valve evicted live holders)
forced extraction at n=2, superseding the original "third consumer" rule. New `claude -p` loops
source it per convention line 8. Everything else that repeats (~100 LOC/loop: watchdog, JSONL
append, plist boilerplate, stop-file check) stays per-loop until a flip trigger above fires.
