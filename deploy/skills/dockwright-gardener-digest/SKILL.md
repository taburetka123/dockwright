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

- Write ONLY under `~/.claude/dockwright/gardener/` — the digest file (path given in args), proposal files in `proposals/pending/`, check files in `checks/`. A PreToolUse guard mechanically denies any other Write/Edit/NotebookEdit/MultiEdit target; do not fight it.
- **The mechanical guard covers file-writing tools ONLY — Bash is explicitly NOT vetoed.** `deploy/scripts/gardener-write-guard.py`'s own docstring: "Bash is NOT vetoed here (command strings are not reliably parseable for write-ness) — it stays on the runtime's own vetting plus the watching human" (PRD §9.1/§16 Q5) — a deliberate design choice, not a gap. The skill's earlier "verified 2026-07-06 ... Bash mkdir/cp ALSO denied" claim was WRONG: reproduced live twice since (2026-07-30, 2026-08-03), a Bash `>` redirect and a bare `mkdir` into `/tmp` both succeeded undenied. Avoiding a Bash write outside the gardener dir is SELF-DISCIPLINE (next bullet), never a mechanically enforced backstop — stage any intermediate data in-memory (a `python3 -c` one-liner, or just hold it across the turn), never attempt a scratch-directory write via Bash.
- No Bash that mutates state (no git commit, no touch/mkdir/redirects). Prefer Read/Glob/Grep tools.
- Do not apply, draft-in-place, or edit any fix anywhere. Proposals carry diffs as TEXT for a human to apply.
- When your artifacts are written, stop. No follow-up actions, no questions.
- Budget: ≤10 proposals per run; ≤2 transcript deep-dives; if input volume threatens the run budget, narrow the window and SAY SO in the digest's Notes.

## Args

Parse from $ARGUMENTS: `run_id=`, `digest=` (absolute digest output path), `trigger=`, `mode=` (`full` = the whole unreviewed backlog; `incremental` = unreviewed findings newer than `~/.claude/dockwright/gardener/last-digest`'s mtime PLUS the parked remainder — unreviewed findings of any age that are not members of any proposal in `proposals/pending/`, member sets read from those files' `members:` frontmatter. The review sitting is proposals-only: this fold-in is what keeps below-bar singletons cycling through digest runs instead of orphaning). Missing args: derive run_id from UTC timestamp, digest path as `~/.claude/dockwright/gardener/digests/<run_id>.md`, trigger=manual, mode=incremental.

## Step 1 — Read prior Gardener memory (dedup substrate)

Read `~/.claude/dockwright/gardener/ledger.jsonl` (it is small). Collect:
- **Declined clusters**: `decision` events with `kind=decline` — their `members` sets and reasons. NEVER re-propose a cluster whose member set adds nothing new over a declined one; mention it in Notes only if strictly-new members arrived. Scope: a declined `corpus-retire` proposal dedups only against future corpus-retire proposals — declining retirement keeps the members as clustering evidence and must never block a real failure-class proposal over the same findings.
- **Accepted proposals** and **armed checks** (`decision` kind=accept, `check_armed`): don't re-propose what's already accepted/armed; outcome follow-up is Phase 2's job, not yours.
- Prior `proposal` events still pending (files in `proposals/pending/`): do not duplicate them.
- **Declined/rejected proposals are EVIDENCE, not just dedup substrate:** decline reasons (`decision` kind=decline) and quarantine reasons (`proposal_rejected`) are labeled failures of the Gardener's own drafting. When ≥2 declines share a reason-class (wrong home, too much always-on cost, heuristic patch over root cause, …), report the pattern in the digest's `## Proposal-shaping learnings` section; if it contradicts a shaping prior above, the fix to this skill's own priors is itself proposable (normal bar).
- **Back-pressure state:** find the latest `backpressure` event (`lane: digest`). If it carries `violation: true`, THIS run MUST rank a negative-`always_on_bytes` proposal first (applyable diff — see Step 5's eviction duty) — or state explicitly in Notes why none is possible; the human sees that line at the sitting. Also: any `proposal_rejected` event naming a proposal that a prior digest's `## Eviction lane` row points at (`proposed-<id>`) flips that row back to `watching` — a birth-quarantined eviction proposal must not keep absorbing the repeat-offender pressure.

## Step 2 — Observe (PRD §6 sources, cost discipline)

1. **Findings (primary):** unreviewed = `~/.claude/dockwright/selffix/findings/*.md` with no `.reviewed` sibling. `mode=full` → all of them; `mode=incremental` → those newer than the last-digest marker, plus the parked remainder (unreviewed, any age, not a member of any `proposals/pending/*.md` — read the member sets from their `members:` frontmatter). Read them — parallel `Read` calls are fine for a small `mode=incremental` window (roughly ≤30 files); for `mode=full`, prefer a single batched `Bash` cat sweep (`for f in *.md; do echo "=== $f ==="; cat "$f"; done`-style), capped at ~15-20 files per invocation to stay under the inline-display truncation threshold.
1b. **Engineer corrections (labeled failures, first-class):** issues inside findings files tagged `**Source**: engineer-correction` / `⚖️ [CORRECTION]` (extracted by dockwright-selffix Step 2b). Human-labeled ground truth about a real assistant failure — weigh like 🚩 human-flagged evidence.
2. **Ops state + manager memory (windowed, cheap, ONE combined sweep):** `~/.claude/dockwright/gardener/gate.log` tail; `~/.claude/dockwright/closed/*.json` `closed_reason` distribution; `.stale-emitted*.json`; `~/.claude/dockwright/selffix/trigger.log` tail if present; `ls -t ~/.claude/dockwright/manager-memory/*/*.md 2>/dev/null | head -5` (then read whatever it returns). Fold manager memory into this SAME command block — as a standalone bullet it has been silently skipped whole with no disclosure, even with real readable data sitting there.
3. **Substrate metrics (arithmetic, not reading):** total bytes + file count of `~/.claude/rules/`, `~/.claude/agents/`, skills count — the manageability-surface trend.
4. **Transcripts:** ON-DEMAND only, max 2, tail-windowed — only to confirm/refute a specific cluster hypothesis.

## Step 3 — Cluster and rank (PRD §7.2–7.3)

Group issue-level evidence by recurring FAILURE CLASS (same root pattern across sessions), never by session/ticket. A finding file can contribute to multiple clusters.

- **Proposability bar:** a cluster is proposable only with recurrence across **≥3 sessions OR ≥2 distinct weeks** (regression-to-the-mean guard). Below the bar → report in the digest, no proposal.
- **Rank** = recurrence count × cost-per-occurrence (as described in the findings) × fix-cheapness. Descending.
- Singletons: one-line "unclustered" list in the digest.
- **Human-flagged findings bypass the bar AND get an actionable proposal — treat them as IMPORTANT.** A finding carrying `🚩` / `[MANUAL]` / `**Source**: manual` (a user `/dockwright-fix` flag — see `dockwright-selffix` SKILL.md) is a deliberate human ask; it does NOT need ≥3-session recurrence. Never cluster it away or drop it under "below the bar / unclustered" — surface each verbatim in the dedicated `## 🚩 Human-flagged (manual)` digest section (Step 7), **and pre-draft an actionable proposal for it by default** (human-flagged proposals count *beyond* the ≤10 above-bar cap). Do NOT downgrade a human-flagged ask to "surface only / no proposal": if the clean fix needs a spike, draft the proposal capturing the ready zero-downside part (e.g. a discoverability cross-reference to an existing recipe) and flag the spike as a gated step inside it. Omit a proposal ONLY when there is genuinely no actionable artifact at all — and then say why explicitly in the human-flagged section. (2026-06-22: this run downgraded the human-flagged vendor-auth ask to surface-only; user: "Vendor auth is important. Basically I want you to treat human marked issues as important.")
- **Detection recipe for the marker above:** grep on `**Source**: manual` (or the literal `🚩 [MANUAL]` heading prefix) only — never a bare command-name substring (`fix` / `dockwright-fix` or any deprecated alias). Ordinary retros routinely record the marker's *absence* in prose (e.g. "No genuine `/dockwright-fix` invocation appears..."), and a bare-substring grep false-positives on exactly those lines.
- **Engineer corrections bypass the bar** the same way human-flagged findings do (a correction is a human label, not a model guess) — but Step 4 already-fixed detection still applies: a correction whose durable fix already landed (the common case — the engineer fixed it in-session) yields an **outcome check** (does the landed rule/skill actually hold? does its TRIGGER catch the case?), not a duplicate proposal. Like human-flagged proposals, correction-sourced proposals count beyond the ≤10 above-bar cap.
- **Newer resolution wins.** When two pieces of evidence contradict on the same behavior (an older correction/rule vs a newer correction), draft from the NEWEST resolution; keep the older as history; FLAG the conflict explicitly (in the cluster's Evidence and in Notes) for the reviewer. Never average contradicting guidance, never silently merge. "Newer" is keyed on finding-file recency (the session order Step 2 already reads), not a per-correction timestamp field — when two corrections share a single findings file, the explicit conflict FLAG is the backstop instead.

### Step 3a — Adherence tally (eviction lane input)

While clustering, tally ADHERENCE failures per guidance file: an issue whose own text says the guidance already existed and was loaded but was violated or read narrowly ("rule already mandates this", "compliance miss", "was loaded but ignored") increments the counter of each guidance file it names (rules/, agents/, skills). The tally is CROSS-RUN and the digest itself is its state — no extra state file:

- `mode=incremental`: start from the `## Eviction lane` table of the newest prior digest that CONTAINS that section (scan `digests/*.md` newest-first — frontier digests share the directory and error/timeout runs leave partial files; a table-less newest file must NOT silently reset the counts), then add this run's mentions.
- `mode=full`: rebuild from scratch (full re-read = full recount; carrying forward would double-count). Derive `proposed-<id>` statuses from `proposals/pending/` + the ledger, and zero the counter of any file with a live pending eviction proposal — a full recount must not resurrect counts already converted into a pending proposal.
- Row shape: `file · cumulative adherence mentions · status (watching | proposed-<id>)`. When this run emits an eviction proposal for a file, set its status to `proposed-<id>` and zero the counter; re-accumulation starts from later mentions only. Declined-cluster dedup stays Step 1's member-set machinery, unchanged.

Count at most one adherence mention per finding file toward the threshold (one file ≈ one session), so a ≥3-mention repeat offender satisfies Step 3's proposability bar by construction — no separate exemption needed. In `mode=incremental`, tally increments come only from findings newer than the last-digest marker (their first read): a parked-remainder finding re-read via the incremental fold-in was already tallied when first read — re-counting would double-count. (Known, accepted undercount: a finding FIRST read via the fold-in — it arrived mid-digest-run, so it is marker-older ever after — is never tallied; before the fold-in it was never read at all, and the tally is a calibrating heuristic.)

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
kind: <rule-edit|skill-edit|agent-edit|code-change|new-asset|build-brief|corpus-retire>
always_on_bytes: <signed integer — UTF-8 bytes this diff adds to ALWAYS-LOADED context (rules/agent files); 0 for skills/code>
cost_justification: <REQUIRED when the diff nets positive always-on bytes — any plus (the postrun quarantine enforces only beyond [gardener] bytes_tolerance, default 16): one line — the value claim + the cheaper home you rejected; omit for ≤0>
flow_cost: <none | adds — <one clause> | removes — <one clause>> — REQUIRED; see "Flow-cost question" below
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
 for kind: new-asset, a NEW-FILE unified diff: `--- /dev/null` header +
 `+++ <absolute destination path, byte-equal to the targets: entry>` with the
 full content as +lines — gardener_apply.py (T11) applies it mechanically>
```

## Rationale
<why this fix, why this home (home-selection: rule vs skill vs agent-file vs memory
— cheapest correct home; name the alternative you rejected),
cost accounting (the always_on_bytes number justified), Pareto check (which
north-star axis improves; which could regress and why it doesn't)>
```

Frontmatter format is load-bearing: scalars and `[a, b]` inline lists only — `gardener_postrun.py` parses it mechanically and QUARANTINES anything malformed or targeting outside `~/.claude` + the dockwright repo (`[paths] dockwright_repo`, when set) (FR-8).

**Diff format is load-bearing too — a proposal whose diff cannot apply is dead on arrival.** Requirements for every ```diff fence:
- Hunk headers MUST be numbered — `@@ -<start>,<count> +<start>,<count> @@`, computed from the target you read in Step 4; never a bare `@@`.
- Every change needs ≥1 unchanged context line on BOTH sides. git treats a hunk with no trailing context as anchored at end-of-file — a leading-context-only "append after this block" hunk can NEVER apply mid-file, however correct its numbers.
- Context lines start with a single space; blank context lines are a single space on their own line; nothing but diff lines between `@@` and the fence close.
- **Self-check each proposal after writing it**: `python3 ~/.claude/scripts/gardener_apply.py check --proposal <path>` (read-only — allowed under the write-guard). Exit 0 = the diff applies; any non-zero exit means fix the diff and re-check before finishing the run. Never leave a proposal failing this check: it is burned as drafting-failure evidence, and once the birth gate ships it is quarantined outright. Exception — `kind: build-brief` and `kind: corpus-retire` (a prose `## Diff`, no ```diff fence): the CLI check exits 2 on fence absence by design, so SKIP the self-check for them; the postrun birth gate classifies fence absence as a passing `no-diff` verdict instead.
- **Generate the diff programmatically, don't hand-type it.** Capture the exact old/new line lists from the Step-4 Read, then run stdlib `difflib.unified_diff()` (e.g. `python3 -c "import difflib; old=open('<target>').readlines(); new=[...]; print(''.join(difflib.unified_diff(old,new,fromfile='a/<rel>',tofile='b/<rel>',lineterm='')))"`) to produce the hunk headers and context — this can't miscount a hunk, drop leading context, or truncate a multi-hunk diff by construction, the three failure modes that cost a manual repair-and-recheck cycle in nearly every run (7/8 proposals failed apply-check in run `20260723T140325Z-22586`; this run needed repair cycles on two proposals before this paragraph existed). Keep the self-check above as the safety net, not the primary correctness mechanism.

**Compute `always_on_bytes` precisely before writing it, in UTF-8 BYTES, not characters** — e.g. `python3 -c "print(len(new_text.encode())-len(old_text.encode()))"` against the exact before/after text of each always-loaded file touched — don't hand-estimate and patch the frontmatter afterward in a separate verification pass. Many non-ASCII characters take two or more bytes each, so a char-counted declaration on a file carrying them diverges by ~the size of the edit, and the postrun consistency gate quarantines any >16-byte mismatch against the diff-computed value.

**Canon-targeting (cp-deployed files).** A `~/.claude` file is cp-deployed by `setup.sh` — and reverted on the next run — when setup.sh copies it from the canon. MOST trees deploy at the SAME relative path (`agents/`, `commands/`, `scripts/`, `skills/`, `statusline-command.sh`, `loops-registry.md`); a FEW deploy RENAMED (`~/.claude/dockwright/presets/X` ← `deploy/presets/X`; `~/.claude/dockwright/status_row.py` ← `deploy/tmux/status_row.py`; `~/.claude/dockwright/dockwright.tmux.conf` ← `deploy/tmux/dockwright.conf`). For any cp-deployed target, `targets:` MUST be the actual canon SOURCE path under `<dockwright_repo>/deploy/` (the dockwright checkout, `[paths] dockwright_repo`), never `~/.claude/...` — a diff applied to the `~/.claude` copy is wiped on the next `setup.sh`. Determine the source from the setup.sh deploy mapping; do NOT assume same-relpath. Native `~/.claude` files with NO canon source (`rules/`, `flows/`, `~/.claude/dockwright/` runtime state such as `notebook/`, skills absent from the canon) keep their `~/.claude` target. The validator already whitelists the dockwright repo as an allowed target root, so a canon path passes quarantine.

`targets:` MUST be a full absolute path — e.g. `<dockwright_repo>/deploy/loops-registry.md` expanded to its real absolute form — never a bare relative fragment (`deploy/loops-registry.md`, `loops-registry.md`), which may appear above for illustration only; the postrun validator resolves each target against the process CWD and quarantines anything that lands outside the allowed roots.

**Ops-evidence proposals are legitimate** (the first real run proved it: the severed-hook discovery had no finding files behind it — its evidence was git history and a silent log). Declare them honestly: `evidence_kind: ops` with `members` OMITTED — never invent sentinel member strings. `members` is required, full-UUID-shaped, and review-burned only for `evidence_kind: findings`; the validator enforces this.

### Eviction lane — repeat offenders and the negative-byte duty

A file at **≥3 cumulative adherence mentions** (Step 3a; threshold under calibration — investigation prior, not derived from data) is a repeat offender: prose about it demonstrably does not change behavior (40% of all failures are already-covered-but-violated; 0 negative-byte proposals in the first 72). The DEFAULT proposal shape for its cluster is one of:

- **CONVERT-to-enforcement** — the rule's decidable core becomes a hook / test / settings check (the guards-that-dont-guard pattern, PR #229); the prose shrinks to a pointer. Emit as `kind: build-brief` with a PROSE `## Diff` naming (1) the exact enforcement mechanism and (2) which prose gets trimmed — only AFTER the enforcement is live; the sequencing is part of the brief. ⛔ **The brief orders ONE behavioural check that proves the mechanism fires, and no more.** Deeper test mass is earned only where the guarded action is itself irreversible or outward-facing — a destructive or production operation, an action under the user's name, the user's own data. An enforcement target is internal tooling unless it guards one of those. The radius sizes MASS only: red-proofs, mutation sweeps, meta-tests and source/AST-parsing tests stay refused on EVERY surface, full-depth ones included (`~/.claude/rules/drift-guard-tests.md` § Blast radius).
- **SPLIT** — a multi-topic file into single-concept files with precise TRIGGERs. Honest note: rules load unconditionally, so a split alone is byte-neutral — it treats narrow-reading failures; pair it with compression to qualify as negative.
- **COMPRESS** — anchors to one line each, dedup, demote reference-class content to an on-demand skill.

Additive PROSE targeting a repeat offender requires an explicit Rationale justification for why prose will work this time; without one, use the shapes above.

**Negative-byte duty:** every run aims to include ≥1 proposal with a negative `always_on_bytes` and an APPLYABLE diff (COMPRESS / dedup / demotion / net-shrinking SPLIT). A `kind: build-brief` CONVERT does NOT satisfy the duty — its trim is not yet applyable, and the postrun gate counts only deltas computed from applyable diffs. If there is genuinely no candidate, write the explicit Notes line `no negative-byte proposal this run: <why>`; the postrun back-pressure gate tracks the miss streak and flags the sitting on the second consecutive miss.

Tradeoff-laden proposals (anything adding friction, common-path behavior, or standing tokens) must say so in Rationale — never bundle them with clean ones (PRD §3.2).

### Token censor — compress, then decide (before writing any proposal)

Run BOTH passes on every drafted proposal before its file is written:

**COMPRESS.** The diff's `+` lines carry only what a future session needs at load time — the corrected value, the new instruction. WHY-prose, evidence, incident anchors ("(2026-07-2x: ...)") belong in `## Rationale` (proposal-file bytes, not always-on) — never in the diff. One proposal = one coherent change: a sentence the mechanical fix works without is cut, or split into its own proposal that clears the bar (and this censor) on its own evidence — "the file is being touched anyway" is not a reason for a ride-along note. Deletions obey the same rule: bundling unrelated removals to buy a negative sign is the mirror image of the bundled-prose defect. Sign-check: an EDIT of existing guidance (reword, wrong value, stale reference, tightened phrasing — an edit-shape, independent of the ⚖️ evidence kind) carries a strong prior of ≤ 0 bytes — replace, don't append; an edit computing positive usually means bundled padding (anchor: a JQL fix that mechanically removed characters shipped as +305 B of bundled rule-reword prose). The one escape: an edit that still legitimately needs more bytes after honest compression goes through DECIDE like any positive delta.

**DECIDE — cost is an emission input, not a disclosure.** After COMPRESS, take the computed `always_on_bytes`:
- ≤ 0 → emit.
- \> 0 → the proposal must EARN the bytes. Legitimate only when (a) the evidence is human-flagged (`🚩`/`[MANUAL]`) or ⚖️ engineer-correction, or (b) the cluster is above-bar AND no cheaper home (skill body, flow, enforcement conversion — priors #1/#2/#6) can hold the lesson, with the rejected alternative named. Then declare the REQUIRED frontmatter field `cost_justification:` (one line: the value claim + the cheaper home rejected; for an Eviction-lane repeat offender it must also answer why prose will work this time). The postrun QUARANTINES any proposal whose diff computes more than `bytes_tolerance` positive always-on bytes without a non-empty `cost_justification`.
- Otherwise DROP at draft time — the proposal is not written and never reaches the sitting. Record each drop as one line in the digest's `## Below the bar / unclustered`: `censored (+N B): <cluster> — <why the value didn't clear the cost>`.

### Flow-cost question — what does an ordinary run pay?

Answer ONE question per proposal, in the required `flow_cost:` frontmatter field: **what does this cost on every ordinary run that does NOT hit the problem it solves?**

- `none` — it fires only on the symptom; a run that never hits the problem is unchanged.
- `adds — <one clause>` — it puts mandatory work on a recurring surface. Name the surface AND the per-what: per review round, per PR, per test run, per session.
- `removes — <one clause>` — it takes mandatory work off a recurring surface. Say which.

Unsure ⇒ answer `adds` and name what you could not rule out. `none` is a claim that needs confidence; `adds` blocks nothing, so the cautious answer is free.

Two things the answer is NOT. It is not "is this a good idea" — the ADD-ONE clause added to `drift-guard-tests.md` on 2026-07-29 was CORRECT about the failure it described, and was demanded in most review rounds over the three weeks that followed — then was cut from that file on 2026-08-20 for the test mass it built (counts in its § Why, not here). And it is not a veto: an `adds` proposal is emitted, ranked and presented like any other, and the human picks. Answering is the whole duty — no second pass, no search for a cheaper shape, no justification.

**Mandatory work added to a review round, a test suite, or an artifact every session must carry is the shape with the worst measured record.** Look for it in your own draft first.

### Corpus-retire lane — the exit for never-clustering findings

The review sitting is proposals-only: a parked below-bar finding leaves the unreviewed corpus only through a proposal decision (or an explicit user dismissal at the sitting). When the parked remainder (unreviewed, not a member of any pending proposal) contains findings older than 30 days (initial calibration value) not already covered by a still-pending `corpus-retire` proposal, draft ONE retire proposal for them: `kind: corpus-retire`, `evidence_kind: findings`, `members` = the full UUIDs, `targets:` = the absolute findings dir (expand `~/.claude/dockwright/selffix/findings` — the path the decide actuates on; NEVER `targets: []`, which the postrun required-field check rejects as falsy and quarantines), `always_on_bytes: 0`, `flow_cost: none`, `base_rev` from `git -C ~/.claude rev-parse --short HEAD`, `revert: delete the members' .reviewed markers (restores them to the unreviewed corpus)`, a PROSE `## Diff` — "no artifact change: accepting retires the members as spent evidence" — and a falsifiable expectation (the parked remainder shrinks by <n>; the retired findings prune within ~14 days of the decision). Like build-briefs, the fence-less diff passes the birth gate as `no-diff` — SKIP the `gardener_apply.py check` self-check for it (the CLI exits 2 on fence absence). The `## Evidence` section is one line per member (sid + strongest issue title + age) so the human can skim what they are retiring; retire proposals count beyond the ≤10 above-bar cap. ACCEPT at the sitting batch-marks the members reviewed via the normal `postrun decide` path (no apply, no eval gate); DECLINE means keep-as-evidence (see Step 1's dedup scope). Never include a 🚩 human-flagged or ⚖️ correction-sourced finding in a retire draft — those keep their own decision surface (the sitting's remainder grep and explicit user dismissal). Known, pre-existing quirk shared with build-briefs: a fence-less proposal makes the run proposal-bearing for the back-pressure streak without contributing a qualifying negative delta — the negative-byte duty stays independent of this lane. Both censor passes apply to it like any proposal — it computes `always_on_bytes: 0`, so DECIDE emits it.

### Proposal-shaping priors

Learned from the human's edits 2026-06-24; shapes how the gardener drafts FUTURE proposals:

1. **Structural/root-cause fix > heuristic patch.** Kill ambiguity at the source, don't patch the symptom. (G1/G2: `/dockwright-fix` command vs size-ceiling+strip.)
2. **One general well-homed skill > new always-on rule or scattered/duplicated edits.** Use always-on skill DESCRIPTIONS for discoverability; keep always-on RULE bytes minimal (cost-averse to standing cost). (G3 general review skill vs 712-byte rule; G7 explicit token homes in descriptions.)
3. **Discipline in the worker/flow (self-driving) > manager-memory-dependent clauses** — EXCEPT where the lever is structural and deterministically manager-controlled (cwd/dispatch). (G4 → flow; G5 → manager.md because cwd is set hard at spawn.)
4. **Consolidate logic into its logical home; MOVE > cross-reference; no duplicates.** (G7: move a capability into its owning skill rather than cross-referencing it from several.)
5. **Hunt the downstream/deeper failure mode a change may create or miss.** (G5: read-only default alone breeds base-clone writes on the investigation→fix pivot.)
6. **Repeat adherence offender → default enforcement/split/compress; additive prose only with an explicit Rationale justification.** (Corpus-diet 2026-07: 40% of failures were already-covered-but-violated; a third restatement of an ignored rule has weak predicted effect.)

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

## Engineer corrections (labeled failures)
<one entry per ⚖️ correction-sourced issue — the verbatim quote + sid + whether the durable fix landed (→ outcome check) or is still missing (→ proposal). Omit the section when none.>

## Proposal-shaping learnings
<recurring decline/quarantine reason-classes (≥2) from Step 1 — what the human keeps rejecting and why. Omit the section when none. When a pattern here is newly reconfirmed THIS run — not just a repeat mention of an already-declined/already-fixed one — draft an actual proposal for it under the normal ≤10 cap rather than only describing it.>

## Below the bar / unclustered
<one line each; censor drops as `censored (+N B): <cluster> — <why>`>

## Substrate metrics
rules: <bytes>/<files> · agents: <bytes>/<files> · skills: <count> · trend vs last digest if known

## Eviction lane
| file | adherence mentions (cum.) | status |
|---|---|---|
<tally rows — carry per Step 3a>
negative-byte proposals this run: <n>

## Notes
<data anomalies, sample-bias caveats, budget narrowing if any, or "none">
Status: ok
```

The literal last line MUST be `Status: ok` (or `Status: error <reason>`). The wrapper joins on it.

## Headless mode (GARDENER_HEADLESS=1 — deferred)

When invoked under `claude -p` (no Write tools): emit the SAME artifacts as fenced blocks on stdout, each preceded by `=== ARTIFACT: <relative path> ===`, ending with the Status line. The wrapper writes the files. (Deferred-spike contract — PRD §12.)

## Testing the SessionEnd/selffix pathway

Reference for any test that empirically checks a SessionEnd hook or selffix-pathway behavior. Do NOT use "a findings file exists" as the only success signal — none-signal sessions (the common case) deliberately write no findings file, so "no file" is ambiguous between "pathway broken" and "pathway ran and correctly produced nothing". Instead:

- Tail `~/.claude/dockwright/selffix/trigger.log` — every SessionEnd fires the trigger and writes exactly one outcome line (`spawn` / `none` / `skip:*` / `retry:enqueued`), with no debug flag needed; that line is the loop's ledger and its freshness event. `touch ~/.claude/dockwright/selffix/debug` (or `export SELFFIX_DEBUG=1`) additionally logs the verbose extras — the trigger's `prune` counters and the run/gate lifecycle. Failed/stub/brick-deferred runs also log `retry:*` lifecycle verbs (`retry:enqueued`, `retry:dropped …`, `retry:exhausted`) — a queued retry is consumed by the gardener-gate loop's pre-digest step, so a missing findings file right after session end may simply mean the retro is queued in `~/.claude/dockwright/selffix/retry/`. That line is the real signal.
- Or read the orchestrator `closed/<sid>.json` `closed_reason` to confirm how the session ended.
- For process-death observation (did the spawned worker actually start / survive?), use a **60s window, not 5s** — claude headless startup + the first turn is slow.
