---
name: dockwright-orchestrator-guide
description: Reference guide for dockwright — the claude-orchestrator manager/worker tmux orchestration tool. Use when asked how /manager works, what the ~/.claude/dockwright/ state files are for, the ask_manager / spawn_worker / answer_question flow, when troubleshooting it, or when the user references the orchestrator in conversation.
---

# dockwright (claude-orchestrator)

A Python tool that lets Claude Code (or Codex) sessions act as **managers** that spawn other Claude Code / Codex sessions (**workers**) into separate tmux windows, relay questions from workers to the human, and report status across all of them. Multiple managers can run at once, each scoped to its own workers.

The terminal layer is tmux: managers and workers each live in their own tmux window, spawned detached (`new-window -d`) so opening a worker never steals focus from the manager. Some legacy names survive from an earlier terminal driver: the env var `CLAUDE_ITERM_SID` and the `iterm_sid` key in some tool returns now hold a tmux **pane id**; records on disk write `window_id` (read back-compat via `state.window_id_of`).

Repo: your clone of the claude-orchestrator repository (`~/projects/.../claude-orchestrator/` by convention).

The **architect** (multi-repo ticket pipeline) is an **optional separate component, not included in this distribution** — a standalone MCP server exposing `architect_detect` / `architect_design`. Its per-ticket workdir lives under the state root at `architect/<ticket>/`. See the architect-pipeline skill; it's not covered here.

## Keep this skill in sync with the tool

**If you modify claude-orchestrator, you must also update this skill in the same change.**

Triggers for an update pass: MCP tools added/removed/changed signatures; hooks behavior changed; file-protocol changes (new dirs under the state root, renamed record fields); new `CLAUDE_*` env contract; items moving on/off the "What it can't do" list; breaking UX changes in the slash commands; new failure mode + workaround discovered. Stale skills are worse than no skill — they mislead.

## Mental model

```
       you                        you
        ↕                          ↕
  manager A (domain: general)   manager B (domain: reviews)   ← peer managers, each its own tmux window
   ↕     ↕                        ↕
  w1    w2                       w3        ← workers in a separate "claude-workers" tmux session
```

- **Regular** claude session: no agent overlay, no orchestrator. Most sessions.
- **Manager**: opt-in via `/manager [domain]`. Gets an auto-rolled funny `<adjective>-<animal>` name (e.g. `happy-yak`) — that name is the routing key for its workers' events. Multiple managers can coexist; each sees only its own workers (strict per-manager routing via `parent_manager_name`). Talks to the user, orchestrates workers, never edits code itself. Managers run the `claude` runtime.
- **Worker**: spawned by a manager (runtime `claude` or `codex`). Lives in its own window inside a dedicated **`claude-workers` tmux session**. Has `ask_manager`; must never call `AskUserQuestion` (headless — it would wedge). Gets a routing `name` (task label) plus a cosmetic `funny_name`; its tab title is color-coded by state (gray idle / yellow processing / red awaiting-human). Claude workers inherit all global rules / skills / MCPs; Codex workers get an injected worker-protocol bootstrap prompt.

**Key constraint**: no mid-turn interrupts. `send_manager_to_worker` types the message into the worker's pane via the terminal driver's send-text; if the worker is mid-turn, tmux buffers it and it submits on the worker's next idle. `ask_manager` blocks the worker's turn until the manager answers — bounded at 1500s per call; on timeout the worker gets a `NO_ANSWER_YET:` sentinel and re-calls with `resume_question_id`, so the wait continues without duplicating the question, and the worker's MCP server keeps servicing other tools meanwhile.

## Architecture

| Component | Where | Purpose |
|---|---|---|
| Python package | `src/dockwright/` | FastMCP server + spawner + hooks + monitor CLI |
| Agent files | `~/.claude/agents/{manager,worker}.md` — **composed** by `setup.sh` from `deploy/agents/{manager,worker}.core.md` + overlay drop-ins + `{{vars}}` | System-prompt overlays for the two roles |
| Slash commands | `~/.claude/commands/` (source: `deploy/commands/`) | `/manager`, `/manager-resume`, `/manager-close`, `/recreate-manager`, `/manager-assign`, `/tab` |
| Hooks | `~/.claude/settings.json` (+ `~/.codex/hooks.json`) | `SessionStart`, `UserPromptSubmit`, `Stop`, `SessionEnd` shell out to `dockwright <sub>` |
| State broker | state root (`[paths].state_root`, default `~/.claude/dockwright/`) | File-based — atomic JSON writes, dir per concept |
| Manager memory | `~/.claude/dockwright/manager-memory/<domain>/` | Distilled per-session journals (outlives state cleanup) |
| tmux spawner | `spawner.py` + `terminal.py` | `tmux new-window -d`, env-var contract, claude/codex command building |
| Helper scripts | `~/.claude/scripts/` (source: `deploy/scripts/`) | `stale_monitor.py`, `preflight_cleanup.py`, `bootstrap-recreate.sh` |

The MCP server is one stdio FastMCP process per session. Every session (manager OR worker, claude or codex) connects to its own instance; all read/write the shared filesystem state. **Deployment model**: `src/` runs via `pip install -e` (hooks live on the next fire; `mcp_server.py` needs a session restart — that's what `/recreate-manager` is for), while the agent files are **composed** and the other copies (`commands/`, `scripts/`, `presets/`, `skills/`) are `cp`/`rsync`-deployed by `./setup.sh` — full when-does-it-go-live decision table in § Deployment surfaces below.

**The compose seam.** The deployed agent files are not stored verbatim — they are RENDERED at install time from three inputs:

1. **Core** — `deploy/agents/{manager,worker}.core.md`: the generic, operator-free prompt bodies, carrying `<!-- overlay: <name> -->` insertion markers and `{{var}}` placeholders.
2. **Overlay drop-ins** — `<overlay_dir>/<agent-stem>/*.md` (overlay_dir = `[paths].overlay_dir`, default `~/.claude/dockwright-overlay/`). Each drop-in optionally names an `insert_at: <marker>` in its frontmatter; matched drop-ins replace that marker line (sorted by filename), unbound ones append at end-of-file. The overlay lives in place at `config.overlay_dir()`, tracked and maintained by the operator (nothing to "install"); edits there — or in `dockwright.toml [agent_vars]` — take effect on the next `setup.sh` / `dockwright compose`.
3. **Vars** — `{{name}}` substitutions from `deploy/agents/vars.defaults.toml` `[agent_vars]` (the generic defaults), overridden per-key by the operator's `dockwright.toml [agent_vars]`.

`dockwright compose` (invoked by `setup.sh`) writes the merged text to `~/.claude/agents/` and records provenance in a SIDECAR `.compose-stamp.json` in the out dir — never an in-file header, so the deployed agent files carry only prompt content. Composing a core file with no markers and no vars returns it byte-for-byte (the identity guarantee the byte-equivalence gate rests on).

## Deployment surfaces — when to run setup.sh after merging an orchestrator change

Four independent surfaces receive an orchestrator change. What you must do depends on which file changed:

1. **Editable Python package — `src/dockwright/`.** `dockwright` is a `pip install -e` console-script that imports from `src/` directly, so on-disk changes are live the moment the local clone is on merged main. No `setup.sh`, no recreate. But it splits by process lifetime:
   - **Hooks** (`dockwright session-start|stop|user-prompt-submit|session-end`) run as a fresh subprocess each fire → they pick up `src/` changes immediately, for **newly-started sessions / the next hook fire** only.
   - **MCP server** (`dockwright mcp-server`) is long-running, started at manager/worker boot, and caches the package in `sys.modules`. A change to MCP tool code (`mcp_server.py`) is **not live in already-running sessions** until that session restarts.
2. **Composed agent files — need `setup.sh` (or `dockwright compose`).** The agent files are no longer copied; they are RENDERED from `deploy/agents/*.core.md` + overlay drop-ins + vars (see § The compose seam). A change to any core file, drop-in, or var is **not live until a recompose runs**. `dockwright compose --check` (also run by `dockwright doctor`) recomposes in memory and flags a stale deployed set.
3. **Other `cp`/`rsync`-deployed copies — need `setup.sh`.** `setup.sh` copies these into `~/.claude/`: `commands/*.md`, `scripts/*.py|*.sh`, `statusline-command.sh`, `presets/`, `skills/`. A change to any of these is **not live until `setup.sh` runs**.
4. **Settings / MCP registration.** `setup.sh` also merges the hooks snippet into `~/.claude/settings.json` and `~/.codex/hooks.json`, then registers the MCP via `claude mcp add` and `codex mcp add`. Only relevant on first install or when hook wiring changes.

Decision rule after merging a change:
- Changed a core agent file, an overlay drop-in, or an `[agent_vars]` value? → **run `setup.sh`** (or `dockwright compose`) to recompose.
- Changed another `cp`-deployed file (command, statusline, script, preset, skill)? → **run `setup.sh`**.
- Changed hook logic (`hooks.py`)? → live on the next session's hook fire via the editable install; no `setup.sh`. Already-running MCP servers are unaffected (hooks aren't MCP-resident).
- Changed MCP tool code (`mcp_server.py`)? → **recreate the manager** (and respawn workers) to restart the cached server; `setup.sh` alone won't do it.

## File protocol

The state root is `[paths].state_root` (default `~/.claude/dockwright`):

```
<state_root>/
├── active/<sid>.json                  per-session record: name, funny_name, agent, cwd, window_id,
│                                      pid, state (idle|processing), domain (managers),
│                                      parent_manager_name (workers), runtime (claude|codex),
│                                      last_summary, last_turn_at
├── questions/<manager>/<qid>.json     pending worker question (legacy unscoped: flat questions/<qid>.json)
├── answers/<qid>.json                 manager reply → worker (worker polls + unlinks after read)
├── done/<manager>/<sid>-<eid>.json    explicit worker_done events (legacy/unscoped: done/_unscoped/)
├── turn-ends/<manager>/<sid>-<ms>.json  worker turn-end markers written by the Stop hook
├── closed/<sid>.json                  archived worker records (SessionEnd or idle auto-close) — resume_worker reads these
├── handoffs/<handoff_id>.json         manager→manager recreation snapshots
├── manager-triggers.jsonl             append-only log of handoff takeovers
├── presets/<name>.md                  spawn-prompt boilerplate (spawn_worker preset= prepends it)
├── slots/<category>.json              worker-slot semaphore state (+ .lock)
├── architect/<ticket>/                architect per-ticket workdir (blackboard.db etc.)
├── .seen-{questions,done,turn-ends}-<manager>   monitor cursors (one-shot scans persist SEEN here)
├── .stale-emitted.json                stale_monitor edge-trigger dedup state
└── manager.lock                       LEGACY — no longer read or written by any code; ignore
```

Manager memory is **outside** that tree: `~/.claude/dockwright/manager-memory/<domain>/<YYYY-MM-DD>-<sid>.md`.

All JSON writes are atomic (`tmp` + `os.replace`); `read_json` treats missing and corrupt as None. Routing rule: a record/event with `parent_manager_name == X` is visible only to manager X; `parent_manager_name == null` (legacy) events go to the `_unscoped` bucket, invisible to scoped calls — recovery is `_backfill_legacy_workers`, which runs on `become_manager` when exactly one manager is active and adopts the orphans.

## MCP tools (the `dockwright` MCP)

Most manager-side read tools take `manager_sid` — the caller's **own session UUID** (NOT the funny name; passing the name degrades the filter to wildcard with a stderr warning). `None` = legacy wildcard.

**Manager-side:**

| Tool | Effect |
|---|---|
| `become_manager(claude_sid, iterm_sid?, domain?, name?)` | Registers this session as a manager. Rolls a funny `<adjective>-<animal>` name (unique across active records; pass `name` to preserve an existing identity — the `/manager-reboot` lane), defaults `domain="general"`. Runs legacy-worker backfill + same-pid ghost pruning. Returns `{ok, name, domain, runtime}`. No lock — multiple managers coexist. |
| `spawn_worker(initial_prompt, name?, cwd?, extra_args?, env?, preset?, manager_sid?, runtime?, task_key?, force?)` | Opens a window in the `claude-workers` tmux session (creates it if missing). `name` auto-suffixes `-2`, `-3` on collision. `preset` prepends `<state_root>/presets/<name>.md`. `manager_sid` stamps `parent_manager_name` (via `CLAUDE_PARENT_MANAGER` env) so events route back; unresolvable sid → UNSCOPED worker + warning. `runtime="codex"` launches Codex with `--ask-for-approval never --sandbox danger-full-access --dangerously-bypass-hook-trust` + a bootstrap prompt; Claude workers get remote-control-off `--settings`. `env` can add vars but can't override orchestrator-controlled keys. |
| `list_workers(manager_sid?)` | Worker records + last assistant turn from transcript + `alive` (PID check). |
| `list_pending_questions(manager_sid?)` | Pending questions, oldest first. |
| `answer_question(question_id, text)` | Writes `answers/<qid>.json`, unlinks the question. |
| `send_manager_to_worker(worker, text, auto_resume=false)` | Types the content **directly into the worker's pane**, prefixed `[MANAGER] ` (bracketed paste + single Enter — multi-line safe). tmux buffers if the worker is mid-turn. No inbox file EVER — RAISES when the worker has no live window. With `auto_resume=true`, a closed worker with a resumable transcript is resumed and the message delivered in one call (result carries `resumed: true`); still RAISES when nothing is resumable. Status: `delivered`. |
| `send_manager_to_manager(name, text)` | Message a **peer manager** by funny name. Idle-guarded: peer's input box empty → types directly (`delivered_live`); human mid-typing → does NOT type (`peer_busy`, delivered=False) — no inbox; RAISES when the peer has no live/readable window. |
| `list_managers()` | All active managers: name, domain, sid, window id, runtime. |
| `kill_worker(worker)` | Drops pending questions, then **gracefully closes the worker's tmux pane** (SIGHUP → grace → SIGKILL) so the worker's SessionEnd hook fires (selffix retro + closed/ archive). Not SIGTERM. |
| `get_worker_summary(worker)` | Full un-truncated last assistant message. |
| `get_worker_tail(worker, lines=50)` | Last N transcript entries (role + 200-char preview). |
| `attach_existing(manager_sid?)` | Called on `/manager` startup: surviving workers + orphan questions. |
| `list_closed_workers(manager_sid?, limit?)` | Closed/auto-closed worker records, newest first. |
| `resume_worker(name)` | Reopens a closed worker in its original cwd via `claude --resume <sid>` / `codex resume <sid>` — full history restored, `parent_manager_name` preserved. Deletes the closed record only after the resumed session re-registers (else `{ok: false}` and the record stays for retry). |
| `prepare_handoff(claude_sid, narrative_summary, trigger_reason)` | Snapshots workers + questions into `handoffs/<id>.json` AND distills the transcript into manager-memory. |
| `spawn_replacement_manager(handoff_id)` | Opens a new window (in the old manager's session) with initial prompt `/manager-resume <handoff_id>`. |
| `become_manager_with_takeover(claude_sid, takeover_from, handoff_id, iterm_sid?)` | Atomic takeover: closes the old manager's window, inherits its name + domain (so workers' `parent_manager_name` stays valid), marks the handoff consumed, logs to `manager-triggers.jsonl`. |
| `close_manager_self(claude_sid)` | `/manager-close`: synchronously distills memory, removes the active record, closes own window. |

**Worker-side:**

| Tool | Effect |
|---|---|
| `ask_manager(claude_sid, question, resume_question_id=None)` | Writes `questions/<manager>/<qid>.json`, polls `answers/<qid>.json` every 500ms (async — other tool calls on the same worker stay serviceable), returns the answer. Server-side timeout of 1500s returns a `NO_ANSWER_YET:` re-ask sentinel; re-call with `resume_question_id` to keep waiting on the SAME pending question. Self-heals on corrupt answer files. |
| `worker_done(claude_sid, summary)` | One-shot done event in `done/<manager>/`. Worker's REQUIRED last action on task completion. |
| `wait_for_worker(name, timeout_sec=3600, manager_sid?)` | Block until that worker writes a done event (`{found: "done", summary}`) or its session exits (`{found: "exited"}`). Lets workers (or the manager) chain on a sibling's completion. |
| `acquire_worker_slot(claude_sid, category, max_concurrent?, timeout_sec=1800)` | Per-category semaphore so N workers don't OOM the host on `mvn test` etc. Default cap: `mvn=3`; override per call or via env `CLAUDE_ORCH_SLOTS_<CATEGORY>`. Stale holders auto-evicted. Pair with… |
| `release_worker_slot(slot_id)` | …release. Idempotent. |

There is no `register_self` MCP tool and no `send_instruction` — registration happens in the SessionStart hook; instruction delivery is `send_manager_to_worker`.

## Hooks (in `~/.claude/settings.json`, Codex mirror in `~/.codex/hooks.json`)

All four are wrapped: `bash -c 'CLAUDE_PARENT_PID=$PPID dockwright <sub>'` — `$PPID` of the hook shell is claude's real PID, flowing into the active record so `kill_worker` targets the right process. Each hook gates on `CLAUDE_AGENT ∈ {manager, worker}` (plain sessions are no-ops) and short-circuits on the `CLAUDE_ORCHESTRATOR_DISTILL` sentinel (so headless distill children never register — that once caused infinite `claude -p` fan-out).

| Hook | What it does |
|---|---|
| `SessionStart` | Writes `active/<sid>.json` (resolves unique routing name; rolls a cosmetic `funny_name` for workers, a funny manager name if none pinned). Paints the tmux tab title: manager `🎯 <name> · <domain>`, worker `🔧 <funny> · <task>` (gray, or red if a pending question survived a resume). |
| `UserPromptSubmit` | Sets state=processing + stamps processing_since (tasking-episode bound for wait_for_worker's stale-done gating — covers re-tasks typed directly into the worker window); paints worker tab busy. |
| `Stop` | Updates `last_summary` / `last_turn_at` from the transcript; sets `state=idle`; writes a turn-end marker into `turn-ends/<owning-manager>/`; paints tab gray (or red if a question is pending). Also stamps `last_turn_at_uptime` (CLOCK_UPTIME_RAW) so idle math survives laptop sleep. |
| `SessionEnd` | Archives worker record to `closed/` (so `resume_worker` works), removes `active/`, drops the worker's pending questions. For managers: fallback distill to manager-memory if `/manager-close` didn't already write one. |

**Env contract** (stamped by the spawner / hook wrapper): `CLAUDE_AGENT`, `CLAUDE_WORKER_NAME`, `CLAUDE_PARENT_MANAGER`, `CLAUDE_WORKER_RUNTIME`, `CLAUDE_PARENT_PID`, `CLAUDE_DOMAIN`, `CLAUDE_ITERM_SID` (legacy name — now a tmux pane id). All are stripped (plus the `CLAUDE_ORCHESTRATOR_DISTILL` sentinel set) for distill children.

## Monitors

Managers arm **four Monitor tasks** at `/manager` boot, each a one-shot CLI in a loop (identity — *which manager am I* — resolves per scan via `TMUX_PANE` → PPID-walk, so no name substitution into the command):

| Monitor command | Cadence | Fires on |
|---|---|---|
| `while true; do dockwright monitor questions; sleep 2; done` | 2s | new `questions/<me>/` files → `<worker> asks: …` |
| `while true; do dockwright monitor turn-ends; sleep 5; done` | 5s | silent-finish detector → `FINISHED_SILENTLY <name>: <summary>`; repeats per lull ladder-rate-limited (15/30/60min…, cap 4h) |
| `while true; do dockwright monitor done; sleep 2; done` | 2s | new `done/<me>/` files → `<worker> done: <summary>` |
| `while true; do dockwright monitor stale; sleep 60; done` | 60s | wraps `stale_monitor.py --manager <me>` |

`stale_monitor.py` emits edge-triggered alarms — `STALE_PROCESSING <name>` (transcript silent >30min, re-pages at 60/120…), `STALE_QUESTION` (unanswered >2min, doubling), and `AUTOCLOSED <name>` — workers idle >2h with no pending question get auto-closed (archived to `closed/`, pane gracefully closed so SessionEnd fires; threshold via `CLAUDE_ORCH_IDLE_TTL_HOURS`). AUTOCLOSED lines are diverted to a per-manager notify-outbox and ride the next wake from any monitor lane (or flush within 30min) instead of paging on their own. `preflight_cleanup.py` runs at `/manager` boot: prunes aborted handoffs (>1h unconsumed), consumed handoffs + done/turn-end events (>24h), closed records (>7d), dead-pid active records.

## Slash commands

| Command | What it does |
|---|---|
| `/manager [domain]` | `become_manager` → paint tab → `attach_existing` → load agent overlay → `preflight_cleanup.py` → load last-7-days manager memory for the domain → arm Monitors. |
| `/manager-close` | Graceful shutdown, no replacement: `close_manager_self` distills memory (~up to 3 min) then closes the window. |
| `/recreate-manager` | Shed context / pick up new MCP code: writes a narrative, `prepare_handoff` → `spawn_replacement_manager` → new window runs `/manager-resume <id>`. Bootstrap fallback for pre-recreation MCP servers: `bash ~/.claude/scripts/bootstrap-recreate.sh --narrative … --from-sid … --reason …`. |
| `/manager-resume <handoff_id>` | Successor side: `become_manager_with_takeover`, verify the predecessor window actually closed, re-arm monitors, load memory, surface a takeover brief. |
| `/manager-assign [name]` | Promote a plain live session into a worker of the newest live general-domain manager: relaunches itself via `claude --resume <own sid>` under worker env in the workers session (env can't be retrofitted into a live process). Then the old window must be closed by the user. |
| `/tab [worker]` | Focus a worker's tmux window, inferring the target from conversation context when no argument. |

## Flows

### F1. Spawn
Manager calls `spawn_worker(name="rebase-bot", initial_prompt=…, manager_sid=<own sid>)` → spawner opens a window in the `claude-workers` session → worker's SessionStart hook registers it (`parent_manager_name=<manager funny name>` from env) → manager replies "spawned rebase-bot".

### F2. Worker question
Worker calls `ask_manager(sid, "conflict in X — ours or theirs?")` → question file in `questions/<manager>/` → the questions Monitor pings within ~2s → manager relays to user, then `answer_question(qid, "ours")` → worker's 500ms poll returns, worker resumes. The worker's tab turns red while waiting. If the manager stays silent past 1500s, `ask_manager` returns a `NO_ANSWER_YET:` sentinel and the worker re-calls with `resume_question_id` — the question file stays pending the whole time, so the manager sees one stable question.

### F3. Completion
Worker ends its task with `worker_done(sid, summary)` → done Monitor pings → manager surfaces the summary. Anyone can also block on it via `wait_for_worker(name)`.

### F4. Instruction
`send_manager_to_worker("rebase-bot", "also check migration order")` → typed straight into the worker's pane, prefixed `[MANAGER] `; submits immediately if idle, on next idle if mid-turn.

### F5. Manager recreation
`/recreate-manager` → `prepare_handoff` (snapshot + memory distill) → `spawn_replacement_manager` → new window `/manager-resume <id>` → `become_manager_with_takeover` closes the old window, inherits name + domain → workers and pending questions carry over untouched.

### F6. Crash recovery
Manager window dies: SessionEnd hook removes its record and runs the fallback memory distill. Workers keep running. A new `/manager` in the same situation (exactly one manager booting, orphaned workers) adopts legacy/orphan workers via the backfill.

## Manager memory

Every manager close path (handoff, `/manager-close`, SessionEnd fallback) distills the transcript via headless `claude -p` (model `[spawn].distill_model`, default `claude-sonnet-4-6`; transcript slimmed to ≤500KB, 180s timeout) into `~/.claude/dockwright/manager-memory/<domain>/<date>-<sid>.md` — sections: Decisions, User direction changes, Shipped, Open threads. `/manager` boot loads the domain's last-7-days files into context. Domains exist to scope these memory pools (`/manager reviews` ≠ `/manager general`).

## Manager notebook (entry format + mechanics)

The manager notebook is the durable agenda for planned/conditional fleet-scoped work — intents that would otherwise die in chat context at session end. It complements manager memory (memory is a lossy 7-day narrative; the notebook is a lossless agenda whose entries exit only by resolution or explicit triage). The *when-to-write* / *when-to-check* behavior is ambient in the manager agent file (`deploy/agents/manager.core.md`, composed to `~/.claude/agents/manager.md`) § Manager notebook; the format + mechanics live here.

**Location:** `<state_root>/notebook/<domain>.md` — one file per domain, auto-printed at boot by the `/manager` / `/manager-resume` memory-loader step (same skip-silently-if-absent contract as memory). Resolved/dropped entries move to `<state_root>/notebook/archive/<domain>.md`.

**Entry format:**

```markdown
## [ ] <imperative title>
- when: <the condition, in plain words>
- check: <named command or inspection that settles the condition in ONE cheap call — e.g. `gh pr view 54 --json merged`, a ledger grep whose exact line shape is verified against the writer, or "today ≥ review-by">
- context: <links/paths that let a cold manager act — ticket, PR, doc §>
- added: <YYYY-MM-DD>, <manager name> (sid <first 8 chars>)
- review-by: <YYYY-MM-DD>   # surfaces for explicit triage if still unripe — no silent expiry
```

**`check:` must be settleable in one cheap tool call or a date comparison — not judgment.** A `check:` that parses another store's machine format (a ledger grep, a JSON field) must cite the owning writer's code (file:line) for the exact line shape it matches, or call a stats command that store ships — never a guessed grep. The event is named by its writer, not by the CLI verb that produces it — a check keyed on the wrong string silently reports "unripe" forever, the exact failure the notebook exists to kill.

**Archive-on-resolve.** When an entry is acted on or explicitly dropped: flip `## [ ]` to `## [x]`, append one outcome line (`- resolved: <date> — <what happened>`), move the whole block to `notebook/archive/<domain>.md`, and delete it from the active file. The active file stays small — boot warns above ~4 KB.

**Review-by triage.** An entry past its `review-by` surfaces in the startup brief even if unripe. Triage explicitly with the user: re-date, drop (archive with a reason), or act now. Never silently re-date.

**Gardener stays separate.** The Gardener ledger (`~/.claude/dockwright/gardener/ledger.jsonl`) is machine-parsed JSONL with its own piggybacked check cadence — it does NOT move into the notebook. Notebook entries may reference ledger state in their `check:`, linking by reference.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `/manager` says MCP not connected / `become_manager` not found | MCP servers live in `~/.claude.json` (via `claude mcp add`), NOT `~/.claude/settings.json` | `claude mcp add --scope user dockwright dockwright mcp-server`, restart session |
| `list_workers` / monitors return other managers' stuff, or a stderr "did not resolve to an active manager" warning | You passed the manager's **funny name** as `manager_sid` — it takes the session UUID | Pass `$CLAUDE_CODE_SESSION_ID` (or Codex `$CODEX_THREAD_ID`) |
| Worker spawned but its events never reach the manager | Worker is UNSCOPED (`parent_manager_name=null`) — `manager_sid` was missing/unresolvable at spawn | Respawn with the correct `manager_sid`; or boot a single manager so `_backfill_legacy_workers` adopts it |
| `spawn_worker` raises a tmux-spawn error | tmux server unreachable on the dockwright socket | Confirm tmux is installed and can start a server on `-L dockwright` (socket override: `DOCKWRIGHT_TMUX_SOCKET`; legacy `CLAUDE_ORCH_TMUX_SOCKET` honored one release) |
| Worker blocked but manager sees no question | Monitor missed it or scoping issue | `list_pending_questions(manager_sid=<sid>)`; for legacy flat questions use `manager_sid=None` |
| Worker pane gone, no SessionEnd fired (kill -9, OOM) | Orphan active record | `_prune_stale_active_records` runs on most tool calls and reaps dead-pid records automatically; worst case `rm active/<sid>.json` |
| Worker disappeared after hours idle | 2h idle auto-close by stale_monitor (by design) | `list_closed_workers()` then `resume_worker(name)` — full history comes back |
| Old MCP server lacks `prepare_handoff` / `runtime` args | Server booted before the code shipped | `bash ~/.claude/scripts/bootstrap-recreate.sh …` writes the handoff + spawns the replacement directly |
| Monitor exits 2 "cannot resolve owning manager" | Identity resolution failed (no `TMUX_PANE` match, PPID-walk miss) | Check `active/` has a manager record with your `window_id`/pid; re-run `/manager` |
| Two sessions share one transcript after `/manager-assign` | Expected: the old window is a stale duplicate | Close the old window |
| Hook commands missing `CLAUDE_PARENT_PID` | Hand-edited settings.json | Re-run `./setup.sh` — idempotent merge |

## Verification fixtures + tmux panes

- For a test needing a pane in a specific process state, launch a raw command (`sleep`, `cat`) in a tmux window rather than a worker — claude won't reliably sit on a blocking syscall.
- The `window_id` a spawn returns is a tmux **pane** id (`%N`); pane ids and window ids are separate id-spaces. Address a worker's pane by its pane id (`kill-pane -t <pane_id>`, `send-keys -t <pane_id>`), never a bare window index.

## What it can't do (still deferred)

- Hierarchical sub-managers (peer managers exist; a manager can't own another manager)
- Worker-to-worker messaging (only `wait_for_worker` on a sibling's completion)
- Per-agent `--allowedTools` / permission scoping (workers only get remote-control-off settings)
- Persistent worker pools / pre-spawned roles (presets cover prompt boilerplate only)
- Web / GUI status view (`status` is terminal-only)
- Cross-machine orchestration
- **Real mid-turn interrupts.** Direct send-text lands immediately only if the worker is idle; mid-turn it buffers. "Stop now" = `kill_worker`.

## See also

- Repo: the claude-orchestrator repository — see the `README`.
- Manager operating principles: `deploy/agents/manager.core.md` (core source; composed to `~/.claude/agents/manager.md`)
- Worker protocol (incl. the AskUserQuestion ban): `deploy/agents/worker.core.md`
- Architect pipeline: the architect-pipeline skill + the `claude-architect` MCP server — an optional separate component, not included in this distribution.
- Background loops: `deploy/loops-registry.md` — structured registry + convention; fleet health via `python3 ~/.claude/scripts/loops_status.py`

## How this differs from in-process `Agent` subagents

The Claude Code `Agent` tool spawns subagents *within* one session — same process, results return into the parent's context, gone at session end. claude-orchestrator spawns **separate CLI processes** in **separate tmux windows**: own context windows, own transcripts (`~/.claude/projects/` / `~/.codex/sessions/`), survive the manager, human-readable scrollback, resumable after close. Trade-off: filesystem comms (~500ms answer poll, ~2–5s monitor latency) and no shared memory.

Use `Agent` for "search 5 things in parallel right now". Use the orchestrator for "hours of independent work running in tabs while I do other things".
