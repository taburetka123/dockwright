---
name: dockwright-gardener-frontier
description: Frontier loop analyst run — re-run the frontier research sweep against the local baseline; emits a digest + proposals into the Gardener pipeline. Invoked by gardener-run.sh --lane frontier or /dockwright-gardener-frontier. Args: run_id=<id> digest=<path> trigger=<reason>.
user-invocable: true
disable-model-invocation: false
---

# Gardener frontier sweep — diff the field against the baseline, propose adoptions

You are the FRONTIER loop's analyst run — a separate registered loop from the findings digest, sharing only the Gardener's trust substrate (proposals queue, FR-8 quarantine, review sitting, decision ledger, run mutex). You research and propose; a HUMAN promotes. Nothing you write changes live behavior.

**Spend expectation (state of this loop, for the watching human):** one visible session, ~30–60 min wall, tens of web fetches/searches, ≤4 read-only research subagents — deliberately web-heavy, which is why this loop runs ~weekly with a 48h failure-retry gap, never hourly. Actual tokens/turns are stamped into the run_end ledger event automatically (spend telemetry); no self-reporting needed.

## Hard rules (visible mode)

- Write ONLY under `~/.claude/dockwright/gardener/` — the digest file (path given in args) and proposal files in `proposals/pending/`. A PreToolUse guard mechanically denies everything else; do not fight it.
- No Bash that mutates state. Prefer Read/Glob/Grep for local files; WebSearch/WebFetch for the field.
- Do not apply, draft-in-place, or edit any adoption anywhere. Proposals carry diffs or build-briefs as TEXT for a human to act on.
- When your artifacts are written, stop. No follow-up actions, no questions.
- Budget: ≤8 proposals per run; ≤4 research subagents; prefer direct fetches of known-good sources over broad searches.

## Args

Parse from $ARGUMENTS: `run_id=`, `digest=` (absolute output path), `trigger=`. Missing: derive run_id from UTC timestamp, digest as `~/.claude/dockwright/gardener/digests/<run_id>.md`, trigger=manual.

## Step 1 — Load the corpus (NEVER re-derive)

Read, in this order (`<dockwright_repo>` is the dockwright checkout path, `[paths] dockwright_repo` in dockwright.toml):
1. The frontier **baseline** research document (its A1 adoption queue, A2 disciplines, A3 watch-triggers, A4 skips, A5 re-checks, and Part B verdict) is maintained in the development repo and is not part of this distribution. If a local baseline digest exists (a prior frontier run's output under `~/.claude/dockwright/gardener/`), your whole job is the DELTA against it — those are prior state, not open questions. If none exists, treat THIS sweep as run #0: it establishes the local baseline (write its digest as the baseline for future deltas).
2. Any other research/review documents that ship with the distribution or that you have locally (loops, context-management, planned-work, architecture/soundness) — treat as settled evidence; do not re-litigate their verdicts, diff against them.
3. `docs/loops-registry.md` (repo or `deploy/`) if present — the current loop census and conventions.
4. The repo's recent state: `git -C <dockwright_repo> log --oneline -30` — so "already shipped" answers are current.
5. `~/.claude/dockwright/gardener/ledger.jsonl` — prior frontier proposals/decisions (dedup: never re-propose an accepted or declined adoption absent material new evidence; cite the decline reason when mentioning it).

## Step 2 — Evaluate the baseline's own forward-pointers FIRST

Before any new sweeping, settle what the baseline already queued:
- **A1 queue status:** for each baseline adopt-item, is it shipped (cite commit/PR), in-flight, or stale? Shipped items need no proposal — record status in the digest.
- **A3 watch-triggers:** evaluate each named re-visit trigger against today's facts. A fired trigger is a first-class proposal candidate.
- **A4 skips / A5 re-checks:** spot-check that the named reasons still hold; a skip whose reason has collapsed is a proposal candidate.

## Step 3 — Sweep the field (the delta pass)

Web-research what changed since the baseline's date in: the Claude Code/Agent-SDK harness surface (changelog, new primitives), the major agent frameworks and production setups the baseline tracked, and any genuinely new entrants the searches surface. For each candidate capability, apply the standing bar verbatim: **Pareto-only, zero-downside first, event-driven over tick-driven, human owns irreversible actions** — and check it against what the system ALREADY runs before calling it a gap (the baseline's ground-truth section shows how).

## Step 4 — Pre-draft adoption proposals

Each surviving candidate (≤8, ranked by bar-fit × external evidence strength × effort) becomes `~/.claude/dockwright/gardener/proposals/pending/<run_id>-<n>.md` in the SHARED artifact contract — same frontmatter the digest skill uses, with the frontier values:

```markdown
---
id: <run_id>-<n>
run_id: <run_id>
cluster: <kebab-case adoption name>
lane: frontier
evidence_kind: external
targets: [<absolute path of each file the change touches — or the docs/ path of the build-brief for larger adoptions>]
kind: <rule-edit|skill-edit|agent-edit|code-change|new-asset|build-brief>
always_on_bytes: <bytes added to always-loaded context; 0 otherwise>
base_rev: <short git rev of the primary target's repo — `git -C <repo> rev-parse --short HEAD`>
expectation: <one falsifiable sentence — what observable changes if adopted (used-by date, incident class stops, tokens saved)>
check_window_days: <14 or 28>
revert: <git revert of the applying commit | "close the build-brief unbuilt">
---

## Evidence
<the external sources (URLs, verbatim quotes), what the field converged on, and the
LOCAL fact that makes it applicable here — never "everyone does X" alone>

## Diff
<kind ≠ build-brief: READY-TO-APPLY unified diff against the CURRENT live target (read
it first). kind: build-brief: scope, acceptance criteria, estimated size (S/M/L), and
the named worker/pipeline shape that would build it — a commissioning document, since
frontier ACCEPTs often mean "build this", not "apply this patch">

## Rationale
<bar-fit walk: which north-star axis improves, what could regress and why it doesn't,
why this home, cost accounting, and what the baseline said about it (cite section)>
```

NO `members` — external evidence has no finding files; the validator enforces `evidence_kind: external` semantics. Updating the baseline document itself is a legitimate proposal (kind: rule-edit-style diff against the baseline maintained in the development repo) — that is how the baseline evolves under human control.

## Step 5 — The frontier digest

Write to the `digest=` path:

```markdown
# Gardener frontier digest — <run_id>
baseline: <local baseline digest> (<its date>) · sweep window: <baseline date>..<today> · trigger: <trigger>

## Baseline queue status
<A1 item → shipped (commit/PR) | in-flight | stale, one line each>

## Watch-triggers evaluated
<A3 trigger → fired/not-fired + the fact checked, one line each>

## Adoption proposals (ranked)
### 1. <name> → proposals/pending/<file>
<2-3 sentences: what the field shipped, why it clears the bar here, the expectation>

## Skips confirmed / collapsed
<A4 deltas only>

## Notes
<source-quality caveats, anything anomalous, or "none">
spend: <fetches≈N searches≈M subagents=K — coarse; exact tokens land in run_end automatically>
Status: ok
```

The literal last line MUST be `Status: ok` (or `Status: error <reason>`). The wrapper joins on it.

## Headless mode (deferred)

Same contract as the digest skill: artifacts as `=== ARTIFACT: <relative path> ===` fenced blocks on stdout, Status line last. Deferred with the headless flip (PRD §12, §16 Q5).
