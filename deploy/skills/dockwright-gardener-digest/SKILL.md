---
name: dockwright-gardener-digest
description: Gardener analyst run — cluster the selffix/ops evidence backlog into ranked, pre-drafted improvement proposals (design PRD §6–§7, maintained in the development repo). Invoked by gardener-run.sh in a spawned visible session, or manually as /dockwright-gardener-digest before a review sitting. Args: run_id=<id> digest=<path> trigger=<reason> mode=<full|incremental>.
user-invocable: true
disable-model-invocation: false
---

# Gardener digest — observe → cluster → rank → pre-draft

You are the Gardener's analyst run. Design PRD (maintained in the development repo): §6 sources, §7 loop, §9 safety. `<dockwright_repo>` is the dockwright checkout path, configured as `[paths] dockwright_repo` in dockwright.toml. You observe and propose; a HUMAN promotes. Nothing you write changes live behavior.

**Independence firewall: do NOT read PRD §8** ("Seeded failure classes") — it contains evaluation fixtures; deriving failure classes independently from the evidence is the point, and reading the answers contaminates the derivation.

## Hard rules (visible mode)

- Write ONLY under `~/.claude/dockwright/gardener/` — the digest file (path given in args), proposal files in `proposals/pending/`, check files in `checks/`. A PreToolUse guard mechanically denies everything else; do not fight it.
- No Bash that mutates state (no git commit, no touch/mkdir/redirects). Prefer Read/Glob/Grep tools.
- Do not apply, draft-in-place, or edit any fix anywhere. Proposals carry diffs as TEXT for a human to apply.
- When your artifacts are written, stop. No follow-up actions, no questions.
- Budget: ≤10 proposals per run; ≤2 transcript deep-dives; if input volume threatens the run budget, narrow the window and SAY SO in the digest's Notes.

## Args

Parse from $ARGUMENTS: `run_id=`, `digest=` (absolute digest output path), `trigger=`, `mode=` (`full` = whole unreviewed backlog; `incremental` = only unreviewed findings newer than `~/.claude/dockwright/gardener/last-digest`'s mtime). Missing args: derive run_id from UTC timestamp, digest path as `~/.claude/dockwright/gardener/digests/<run_id>.md`, trigger=manual, mode=incremental.

## Step 1 — Read prior Gardener memory (dedup substrate)

Read `~/.claude/dockwright/gardener/ledger.jsonl` (it is small). Collect:
- **Declined clusters**: `decision` events with `kind=decline` — their `members` sets and reasons. NEVER re-propose a cluster whose member set adds nothing new over a declined one; mention it in Notes only if strictly-new members arrived.
- **Accepted proposals** and **armed checks** (`decision` kind=accept, `check_armed`): don't re-propose what's already accepted/armed; outcome follow-up is Phase 2's job, not yours.
- Prior `proposal` events still pending (files in `proposals/pending/`): do not duplicate them.

## Step 2 — Observe (PRD §6 sources, cost discipline)

1. **Findings (primary):** unreviewed = `~/.claude/dockwright/selffix/findings/*.md` with no `.reviewed` sibling. `mode=full` → all of them; `mode=incremental` → only those newer than the last-digest marker. Read them (batch parallel Reads).
2. **Ops state (windowed, cheap):** `~/.claude/dockwright/gardener/gate.log` tail; `~/.claude/dockwright/closed/*.json` `closed_reason` distribution; `.stale-emitted*.json`; `~/.claude/dockwright/selffix/trigger.log` tail if present.
3. **Manager memory:** newest ≤5 files in `~/.claude/dockwright/manager-memory/*/`.
4. **Substrate metrics (arithmetic, not reading):** total bytes + file count of `~/.claude/rules/`, `~/.claude/agents/`, skills count — the manageability-surface trend.
5. **Transcripts:** ON-DEMAND only, max 2, tail-windowed — only to confirm/refute a specific cluster hypothesis.

## Step 3 — Cluster and rank (PRD §7.2–7.3)

Group issue-level evidence by recurring FAILURE CLASS (same root pattern across sessions), never by session/ticket. A finding file can contribute to multiple clusters.

- **Proposability bar:** a cluster is proposable only with recurrence across **≥3 sessions OR ≥2 distinct weeks** (regression-to-the-mean guard). Below the bar → report in the digest, no proposal.
- **Rank** = recurrence count × cost-per-occurrence (as described in the findings) × fix-cheapness. Descending.
- Singletons: one-line "unclustered" list in the digest.
- **Human-flagged findings bypass the bar AND get an actionable proposal — treat them as IMPORTANT.** A finding carrying `🚩` / `[MANUAL]` / `**Source**: manual` (a user `/dockwright-fix` flag — see `dockwright-selffix` SKILL.md) is a deliberate human ask; it does NOT need ≥3-session recurrence. Never cluster it away or drop it under "below the bar / unclustered" — surface each verbatim in the dedicated `## 🚩 Human-flagged (manual)` digest section (Step 7), **and pre-draft an actionable proposal for it by default** (human-flagged proposals count *beyond* the ≤10 above-bar cap). Do NOT downgrade a human-flagged ask to "surface only / no proposal": if the clean fix needs a spike, draft the proposal capturing the ready zero-downside part (e.g. a discoverability cross-reference to an existing recipe) and flag the spike as a gated step inside it. Omit a proposal ONLY when there is genuinely no actionable artifact at all — and then say why explicitly in the human-flagged section. (2026-06-22: this run downgraded the human-flagged vendor-auth ask to surface-only; user: "Vendor auth is important. Basically I want you to treat human marked issues as important.")

## Step 4 — Already-fixed detection (backtest discipline)

Before drafting any proposal whose fix targets code, scripts, or config: **READ the live target first.** If the defect the cluster describes is already fixed (the code now does what the proposal would have asked):
- Do NOT emit a duplicate proposal.
- Emit an **outcome-check artifact** instead (`~/.claude/dockwright/gardener/checks/<run_id>-<n>.md`, format below): a pre-registered falsifiable expectation that the shipped fix actually holds, with a check window. Name what shipped (commit/PR if discoverable from `git -C <repo> log` reads).
- Record the cluster in the digest under "## Already fixed (outcome checks armed)".

The same applies to prose: if a rule/skill already covers the cluster's lesson, check whether the findings POST-DATE the rule — if yes, that's an adherence gap (propose strengthening/trigger-fix); if no, it's already fixed.

## Step 5 — Pre-draft proposals (PRD §7.4)

Top clusters above the bar, ≤10 total, each as `~/.claude/dockwright/gardener/proposals/pending/<run_id>-<n>.md`:

```markdown
---
id: <run_id>-<n>
run_id: <run_id>
cluster: <kebab-case failure-class name>
lane: digest
evidence_kind: <findings|ops — findings when the cluster's evidence is selffix finding files; ops when it is operational state (logs, ledgers, git history) with NO finding files behind it>
members: [<FULL finding-file basename without .md — the complete UUID, never a prefix>, ...]
targets: [<absolute path of each file the diff touches>]
kind: <rule-edit|skill-edit|agent-edit|code-change|new-asset>
always_on_bytes: <signed integer — bytes this diff adds to ALWAYS-LOADED context (rules/agent files); 0 for skills/code>
base_rev: <short git rev of the primary target's repo at drafting time — `git -C <repo> rev-parse --short HEAD`>
expectation: <one falsifiable sentence — what observable stops/starts happening if this works>
check_window_days: <7 or 14>
revert: git revert of the applying auto-commit (diff below is its own inverse)
---

## Evidence
<recurrence count, sessions, date span; 2–4 quoted instances with sid attributions>

## Diff
```diff
<READY-TO-APPLY unified diff against the CURRENT live target (you read it in Step 4);
 for kind: new-asset, full file content + exact destination path instead>
```

## Rationale
<why this fix, why this home (home-selection: rule vs skill vs agent-file vs memory
— cheapest correct home; name the alternative you rejected),
cost accounting (the always_on_bytes number justified), Pareto check (which
north-star axis improves; which could regress and why it doesn't)>
```

Frontmatter format is load-bearing: scalars and `[a, b]` inline lists only — `gardener_postrun.py` parses it mechanically and QUARANTINES anything malformed or targeting outside `~/.claude` + the dockwright repo (`[paths] dockwright_repo`, when set) (FR-8).

**Canon-targeting (cp-deployed files).** A `~/.claude` file is cp-deployed by `setup.sh` — and reverted on the next run — when setup.sh copies it from the canon. MOST trees deploy at the SAME relative path (`agents/`, `commands/`, `scripts/`, `skills/`, `statusline-command.sh`, `loops-registry.md`); a FEW deploy RENAMED (`~/.claude/dockwright/presets/X` ← `deploy/presets/X`; `~/.claude/dockwright/status_row.py` ← `deploy/tmux/status_row.py`; `~/.claude/dockwright/dockwright.tmux.conf` ← `deploy/tmux/dockwright.conf`). For any cp-deployed target, `targets:` MUST be the actual canon SOURCE path under `<dockwright_repo>/deploy/` (the dockwright checkout, `[paths] dockwright_repo`), never `~/.claude/...` — a diff applied to the `~/.claude` copy is wiped on the next `setup.sh`. Determine the source from the setup.sh deploy mapping; do NOT assume same-relpath. Native `~/.claude` files with NO canon source (`rules/`, `flows/`, `~/.claude/dockwright/` runtime state such as `notebook/`, skills absent from the canon) keep their `~/.claude` target. The validator already whitelists the dockwright repo as an allowed target root, so a canon path passes quarantine.

**Ops-evidence proposals are legitimate** (the first real run proved it: the severed-hook discovery had no finding files behind it — its evidence was git history and a silent log). Declare them honestly: `evidence_kind: ops` with `members` OMITTED — never invent sentinel member strings. `members` is required, full-UUID-shaped, and review-burned only for `evidence_kind: findings`; the validator enforces this.

Tradeoff-laden proposals (anything adding friction, common-path behavior, or standing tokens) must say so in Rationale — never bundle them with clean ones (PRD §3.2).

### Proposal-shaping priors

Learned from the human's edits 2026-06-24; shapes how the gardener drafts FUTURE proposals:

1. **Structural/root-cause fix > heuristic patch.** Kill ambiguity at the source, don't patch the symptom. (G1/G2: `/dockwright-fix` command vs size-ceiling+strip.)
2. **One general well-homed skill > new always-on rule or scattered/duplicated edits.** Use always-on skill DESCRIPTIONS for discoverability; keep always-on RULE bytes minimal (cost-averse to standing cost). (G3 general review skill vs 712-byte rule; G7 explicit token homes in descriptions.)
3. **Discipline in the worker/flow (self-driving) > manager-memory-dependent clauses** — EXCEPT where the lever is structural and deterministically manager-controlled (cwd/dispatch). (G4 → flow; G5 → manager.md because cwd is set hard at spawn.)
4. **Consolidate logic into its logical home; MOVE > cross-reference; no duplicates.** (G7: move a capability into its owning skill rather than cross-referencing it from several.)
5. **Hunt the downstream/deeper failure mode a change may create or miss.** (G5: read-only default alone breeds base-clone writes on the investigation→fix pivot.)

## Step 6 — Check artifacts

```markdown
---
id: <run_id>-c<n>
run_id: <run_id>
cluster: <failure-class>
expectation: <falsifiable sentence over observable data>
check_window_days: <7|14>
fixed_by: <commit/PR ref or "unknown">
---

## How to check
<the exact query/log/file inspection a Phase-2 run (or human) performs at the window>
```

## Step 7 — The digest file

Write to the `digest=` path:

```markdown
# Gardener digest — <run_id>
data window: <span> · <N> findings read (<mode>) · trigger: <trigger>

## Proposals (ranked)
### 1. <cluster> — <k> findings → proposals/pending/<file>
<3-sentence evidence summary + the one-line expectation>
...

## Already fixed (outcome checks armed)
### <cluster> — checks/<file>
<what the defect was, what shipped, the armed expectation>

## 🚩 Human-flagged (manual)
<one entry per source:manual finding — the flagged text quoted + its sid. These bypass the proposability bar and are NEVER buried under "below the bar". If none, omit this section.>

## Below the bar / unclustered
<one line each>

## Substrate metrics
rules: <bytes>/<files> · agents: <bytes>/<files> · skills: <count> · trend vs last digest if known

## Notes
<data anomalies, sample-bias caveats, budget narrowing if any, or "none">
Status: ok
```

The literal last line MUST be `Status: ok` (or `Status: error <reason>`). The wrapper joins on it.

## Headless mode (GARDENER_HEADLESS=1 — deferred)

When invoked under `claude -p` (no Write tools): emit the SAME artifacts as fenced blocks on stdout, each preceded by `=== ARTIFACT: <relative path> ===`, ending with the Status line. The wrapper writes the files. (Deferred-spike contract — PRD §12.)

## Testing the SessionEnd/selffix pathway

Reference for any test that empirically checks a SessionEnd hook or selffix-pathway behavior. Do NOT use "a findings file exists" as the only success signal — none-signal sessions (the common case) deliberately write no findings file, so "no file" is ambiguous between "pathway broken" and "pathway ran and correctly produced nothing". Instead:

- Enable debug: `touch ~/.claude/dockwright/selffix/debug` (or `export SELFFIX_DEBUG=1`) and tail `~/.claude/dockwright/selffix/trigger.log` — every SessionEnd fires the trigger and writes exactly one line (`spawn` / `none` / `skip:*` / `retry:enqueued`). Failed/stub/brick-deferred runs also log `retry:*` lifecycle verbs (`retry:enqueued`, `retry:dropped …`, `retry:exhausted`) — a queued retry is consumed by the gardener-gate loop's pre-digest step, so a missing findings file right after session end may simply mean the retro is queued in `~/.claude/dockwright/selffix/retry/`. That line is the real signal.
- Or read the orchestrator `closed/<sid>.json` `closed_reason` to confirm how the session ended.
- For process-death observation (did the spawned worker actually start / survive?), use a **60s window, not 5s** — claude headless startup + the first turn is slow.
