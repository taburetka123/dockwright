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
  says why not.
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
gate: embedded Python signal detect (HIGH only: configured [gardener] high_skills, gh pr create, >=5 edits, agent=manager, pushback>=1 EN+RU, harsh-language EN+RU), 60-min dedup hash, findings-exist skip, 14d prune; limit-brick check enqueues to ~/.claude/dockwright/selffix/retry/ instead of spawning; [modules] gardener=false no-ops the trigger (module-off)
run_contract: nohup selffix-run.sh → claude -p "/dockwright-selffix …", env-stripped, 25m TERM→KILL watchdog; takes the shared run-lock; failed runs (non-zero exit / <200B stub / lock-timeout) enqueue ONE durable retry consumed by the gardener-gate tick
permissions_mode: user-default; skill contract is stdout-only (never Write/Edit)
ledger_path: ~/.claude/dockwright/selffix/trigger.log
kill_switch: `dockwright selffix disable` (removes the settings.json hook line); queued retries in ~/.claude/dockwright/selffix/retry/ still drain via the gardener tick — also touch ~/.claude/dockwright/gardener-stop or empty the queue for a full stop
runtime_program_path: ~/.claude/scripts/selffix-trigger.sh
source_path: deploy/scripts/selffix-trigger.sh
deploy_mechanism: setup.sh cp (deploy/scripts/*.sh → ~/.claude/scripts/)
log_paths: ~/.claude/dockwright/selffix/trigger.log, ~/.claude/dockwright/selffix/findings/
event_paths: ~/.claude/dockwright/selffix/trigger.log
max_silence_hours: none
last_verified: 2026-06-13
```

### gardener-gate

```loop
name: gardener-gate
label: {prefix}.gardener-gate
status: pending-install
status_why: ships pending-install; opt in with `dockwright gardener enable` (installs the launchd gate; needs selffix enabled), then flip live via [loops.status_overrides] in dockwright.toml
trigger: launchd StartInterval 3600 (no wake catch-up)
gate: gardener_gate.py — [modules] gardener=false no-ops the gate (module-off) → stop file → shared run-lock pre-check → selffix retry pre-step (one queued retro per tick, retry-once, deferred while limit-bricked; gardener-stop pauses retry consumption too; a retry-spawning tick defers a due digest to the next tick) → 6h cooldown → 3/week cap → K=8 unreviewed-findings accumulation → 7d floor
run_contract: visible tmux window via gardener-run.sh; digest-only writes; 30m join + 15m grace, never kills the tab; takes the shared run-lock atomically
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
gate: per-worktree 3-gate — branch merged into origin/main (gh PR MERGED at the worktree HEAD, squash-aware, OR git ancestor) AND clean tree AND no live owner (orchestrator active record / lsof cwd); LLM-free
run_contract: python3 worktree_prune.py --apply, LLM-free, destructive (git worktree remove --force + local branch -D); DRY-RUN BY DEFAULT (--apply required to mutate); stop-file honored; rate cap 25/run; no model spawn so the shared run-lock does not apply
permissions_mode: n/a (LLM-free python)
ledger_path: ~/.claude/dockwright/worktree-prune/ledger.jsonl
kill_switch: ~/.claude/dockwright/worktree-prune-stop
runtime_program_path: ~/.claude/scripts/worktree_prune.py
source_path: deploy/scripts/worktree_prune.py
deploy_mechanism: setup.sh cp + worktree-prune-install.sh (plist)
log_paths: ~/.claude/dockwright/worktree-prune/launchd-out.log, ~/.claude/dockwright/worktree-prune/launchd-err.log
event_paths: ~/.claude/dockwright/worktree-prune/ledger.jsonl
max_silence_hours: none
last_verified: 2026-06-26
```

### bootlite-watchdog

```loop
name: bootlite-watchdog
label: {prefix}.bootlite-watchdog
status: pending-install
status_why: ships pending-install; the operator flips it live via [loops.status_overrides] in dockwright.toml once bootlite-install.sh has run
trigger: launchd StartInterval 3600 (tick half) + hooks.py session_end orphan flag (event half)
gate: bootlite_watchdog.py — stop file → active-record scan; orphan = live worker pid whose parent manager has no live session (per-parent_manager_name predicate; legacy null-parent workers count only when no manager is alive at all)
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

Loops deliberately NOT here: the manager's four Monitors (event-driven, in-session, die with the
manager by design — the bootlite watchdog is their failure-detection backstop); and any
human-invoked skill (the human is the gate).

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
