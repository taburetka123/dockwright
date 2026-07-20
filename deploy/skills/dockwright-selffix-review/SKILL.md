---
name: dockwright-selffix-review
description: Review unreviewed selffix retrospective findings in ~/.claude/dockwright/selffix/findings/ — Gardener proposals first, then raw findings digested by recommendation; asks which to apply, marks reviewed. Triggers on "review selffix", "review gardener proposals", or when unreviewed findings have accumulated.
user-invocable: true
disable-model-invocation: false
---

# Self-Fix Review

Read the queue of auto-generated retrospectives in `~/.claude/dockwright/selffix/findings/`, present a digest grouped by recommendation, and act on whichever fixes the user approves.

> Throughout, `<dockwright_repo>` means the dockwright checkout path, configured as `[paths] dockwright_repo` in dockwright.toml. Where a target is cp-deployed, its canon lives under `<dockwright_repo>/deploy/`.

## When to Use
- User asks to "review selffix", "check retros", "see what selffix found".
- Unreviewed findings have accumulated in `~/.claude/dockwright/selffix/findings/` (written by the SessionEnd `selffix-trigger.sh` pathway).
- After clearing a sprint of work and wanting to act on accumulated retros at once.

## Marked-reviewed contract

A findings file `<sessionId>.md` is considered **reviewed** when a sibling zero-byte file `<sessionId>.reviewed` exists next to it. Never delete or rename the original `.md` — leave the audit trail intact.

## Step 0 — Gardener cluster proposals FIRST

If `~/.claude/dockwright/gardener/proposals/pending/*.md` exist, present them BEFORE the raw-findings flow: each proposal is a pre-drafted, evidence-backed CLUSTER of findings (design PRD §7, maintained in the development repo) — one decision here can retire many raw findings at once, which is the whole point.

1. **Read every pending proposal** (parallel Reads). Each carries YAML frontmatter (`id`, `cluster`, `lane`, `members`, `targets`, `kind`, `always_on_bytes`, `expectation`, `check_window_days`, `revert`) + Evidence / Diff / Rationale sections. The **`lane` field is the SOURCE** and BOTH lanes share this one queue, so it must be made explicit at review: `frontier` = the external frontier-research loop; `digest` = the internal selffix/ops-evidence Gardener (our retros). A proposal missing `lane` is legacy → treat as `digest` (Selffix). Additionally run `python3 ~/.claude/scripts/gardener_apply.py check --proposal <path>` per proposal (cheap, read-only) and carry an `applies cleanly: yes/no` line into each presented subsection — a `no` usually means the target drifted and the cluster needs a re-draft, which changes the decision calculus.
2. **Present each as a `###` subsection with handle G1, G2, …**, the header PREFIXED with the source badge so the lane is unmistakable at a glance — `### 🔭 G1. <cluster>` for `lane: frontier`, `### 🔧 G2. <cluster>` for `lane: digest` (selffix). (same visual rules as the findings digest: bold-labeled paragraphs, `---` separators): **Source.** `🔭 Frontier` (external research) or `🔧 Selffix` (internal retro/ops evidence) · **Cluster.** name + member count · **Evidence.** 1–2 sentence summary · **Change.** target file(s) + one-line diff summary + `always_on_bytes` cost · **Expectation.** the falsifiable sentence. When ≥1 of each lane is present, keep frontier and selffix proposals in clearly separate visual groups (e.g. a `🔭 Frontier proposals` / `🔧 Selffix proposals` divider) so the user is never guessing which is which. Include proposals in the plannotator digest file when that path is used.
3. **Per decision, the bookkeeping is ONE command** (never hand-roll the moves/markers):
   - ACCEPT — the diff is applied by CODE, not by hand (T11); never re-make the edit with Edit/Write:
     1. For `kind: new-asset`, sanity-check the destination home first (rule vs skill vs agent-file vs memory — cheapest correct home, matching neighbouring assets). **If the target is cp-deployed** — `setup.sh` copies it into `~/.claude` from the canon (usually same relpath, sometimes RENAMED, e.g. `~/.claude/dockwright/presets/X` ← `deploy/presets/X`) — the proposal's `targets:`/diff must point at the CANON SOURCE under `<dockwright_repo>/deploy/`; if it points at the `~/.claude` copy, defer for re-draft (or hand-redirect the diff header with explicit user sign-off).
     2. `python3 ~/.claude/scripts/gardener_apply.py apply --proposal <path>` — parses the `## Diff`, context-checks with `git apply --check`, applies all-or-nothing. On failure (target drifted): surface the error; the human picks defer (next digest re-drafts) / decline / explicitly-sanctioned manual edit. Never silently fall back to a model edit.
     3. **Canon target only:** run `./setup.sh` from the main dockwright clone now — the gate evaluates the LIVE `~/.claude`, and an undeployed canon diff would gate the pre-change stack (false pass). Precondition: the rest of the main-clone tree is clean (`git status`) — setup.sh deploys the WHOLE tree, and the apply script dirty-checks only the targets; unrelated uncommitted state would ride along into `~/.claude`.
     4. `python3 ~/.claude/scripts/gardener_eval_gate.py --proposal <path>` with `run_in_background: true`. `skipped` → continue. Otherwise WAIT for the verdict before deciding this proposal — and while any gate runs, do NOT apply other proposals (any live-stack edit mid-suite corrupts the verdict's attribution; declines/defers/presentation may continue). PASS → next step. FAIL (exit 1) → `python3 ~/.claude/scripts/gardener_apply.py revert --proposal <path>` (canon: re-run `./setup.sh` after the revert), surface the failing cases, human decides decline/defer. Exit 2 = infra-suspect (outage/errored samples, NOT behavioral) — surface it; don't treat as a failed proposal. Exit 2 still blocks the decide: keep the diff applied and re-run the gate after the outage, or revert + defer — never proceed to commit+decide on an infra-suspect verdict.
     5. Commit the target repo(s) yourself (`git add <targets> && git commit -m "gardener: apply <proposal-id>"`) — don't wait for the `~/.claude` auto-commit Stop hook: it only fires at end-of-turn with a generic message, and the next step needs the SHA now, attributed to this proposal. For the dockwright repo: commit + push per its conventions.
     6. `python3 ~/.claude/scripts/gardener_postrun.py decide --proposal <path> --kind accept --reason "<optional note>" --applied-rev <root>=<sha>` (repeat `--applied-rev` per committed root).
   - DECLINE: ask the user for a one-line reason (required — it's what stops the cluster from re-surfacing absent new members), then:
     `python3 ~/.claude/scripts/gardener_postrun.py decide --proposal <path> --kind decline --reason "<their reason>"`
   The command moves the proposal to `accepted/`/`declined/`, appends the ledger `decision` event, and **batch-marks every member finding `.reviewed`** — those findings then drop out of the raw-findings flow below automatically.
4. **Don't re-litigate declined clusters.** Anything under `proposals/declined/` stays decided; the digest pipeline re-surfaces a cluster only when strictly-new members appear.
5. Deferring a proposal (no decision) is fine — it just stays in `pending/` for the next sitting.

Then continue with the raw-findings steps below for whatever unreviewed findings remain.

## Steps

1. **Enumerate unreviewed** — list every `*.md` in `~/.claude/dockwright/selffix/findings/` that does NOT have a `<basename>.reviewed` sibling. Use the Bash tool:
   ```bash
   find ~/.claude/dockwright/selffix/findings -maxdepth 1 -name '*.md' | while read f; do
     [ -f "${f%.md}.reviewed" ] || echo "$f"
   done
   ```
   If the list is empty, print "no unreviewed retros" and stop.

2. **Read each unreviewed file** in one batch (parallel `Read` calls). For each issue extract:
   - **Title** from `### Issue N: <title> — <P>/10`
   - **Problem severity** (`<P>/10` above)
   - **Problem description** — the prose between the title line and the `**Fix**:` line. This is the WHAT-and-WHY: what happened, what it cost, why it matters. Compress to 1–2 sentences for the digest, but never drop it — a title alone (`"trigger phrasing"`) is not enough context to decide on the fix.
   - **Fix summary** — first sentence of the `**Fix**:` line.
   - **Fix rating** — the full `**Fix rating**: Impact <I>/10 · Risk <R>/10 · Effort <E>/10 · Confidence <C>/10` line.
   - **Recommendation** — the `**Recommendation**:` line (`apply` | `discuss` | `skip`).

3. **Derive a readable label for each session.** Raw UUIDs (`656ccd8f`) tell the user nothing. For each `<sessionId>.md`:
   1. Locate the transcript: `find ~/.claude/projects -name '<sessionId>.jsonl' -print -quit`
   2. Extract the **task key** from the parent directory name using the configured `[task_keys] key_regex` in dockwright.toml (e.g. a ticket-key-style regex `[A-Za-z]{2,}-\d+` matches a dir like `…-worktrees-PROJ-1234-foo` → `PROJ-1234`). If no `key_regex` is configured, or none matches, fall back to the last meaningful segment of the worktree directory name (the repo/branch slug).
   3. Extract a **2–5 word topic** from the first user prompt in the transcript:
      ```bash
      python3 -c "
      import json, sys
      with open('<jsonl-path>') as f:
          for line in f:
              try: m = json.loads(line)
              except: continue
              if m.get('type') == 'user':
                  msg = m.get('message', {}).get('content')
                  if isinstance(msg, str): print(msg[:300]); break
                  if isinstance(msg, list):
                      for c in msg:
                          if c.get('type') == 'text': print(c['text'][:300]); break
                      break
      "
      ```
      A ticket-start opener (a slash command whose args are just the task key) tells you only the key — pull the topic from the retro's strongest issue title or its opening paragraph instead. If the retro mentions e.g. "PDFBox stream lifetime", the topic is `pdfbox-stream-lifetime`.
   4. The final label is `<KEY> · <topic>` — e.g. `PROJ-1234 · sr-chat-missing-investigation`.

4. **Present a single digest** grouped by recommendation, in this order:
   - **Apply** (high-impact, low-risk, ready to ship)
   - **Discuss** (ambiguous; needs user judgment)
   - **Skip** (acknowledged, no action)

   **Human-flagged findings sort FIRST.** A finding (or an issue within one) carrying `🚩` / `[MANUAL]` / `**Source**: manual` was deliberately flagged by the user via the `/dockwright-fix` command. Present these in a dedicated `## 🚩 Human-labeled (flagged + corrections)` group **above** the Apply group, rendered with the 🚩 badge and the flagged text quoted. **Never auto-skip a human-flagged finding** — even a `skip` recommendation is surfaced for the user (the human explicitly asked for attention). Same `###` subsection format; continue the global letter handles. Issues tagged `⚖️ [CORRECTION]` / `**Source**: engineer-correction` sort into this same top group, rendered with the ⚖️ badge and the verbatim quote — a correction is a deliberate human label; never auto-skip it.

   **Assign a single-letter handle to each finding (A, B, C, …) globally across the digest, in presentation order (Apply first, then Discuss, then Skip).** Letters are the digest-local identifier the user references when picking fixes ("apply A, B; skip C"). They don't collide with the source `Issue N` numbering, so cross-reference back to the .md stays unambiguous. For >26 findings, continue with AA, BB, CC, … (rare). Letters reset per `/dockwright-selffix-review` invocation — they're not stable across runs.

   Visual structure — each finding is a `###` subsection with bold-labeled paragraphs separated by **blank lines**, and findings separated by `---` horizontal rules. Bullet lists collapse adjacent items in CommonMark; subsections + horizontal rules don't. Do not use bullet lists for the findings themselves.

   ```
   ## Apply (<n>)

   ### <X>. <session-label> — <issue title slug> (issue <N>)

   **Rating.** Impact <I>/10 · Risk <R>/10 · Effort <E>/10 · Confidence <C>/10

   **Problem.** <1–2 sentence WHAT-and-WHY — what happened, what it cost, why it matters>

   **Fix.** <one-line fix summary, including the target file path>

   ---

   ### <Y>. <next session-label> — <issue title slug> (issue <N>)

   ...

   ## Discuss (<n>)

   ### <Z>. <session-label> — <issue title slug> (issue <N>)

   **Rating.** ...

   **Problem.** ...

   **Fix.** ...

   **Why discuss.** <one-line reason — usually a low confidence or risk concern>

   ---

   ## Skip (<n>)

   ### <W>. <session-label> — <issue title slug> (issue <N>)

   **Problem.** <still required — the user needs to know what they're dismissing>

   **Why skip.** <one-line reason>

   ---
   ```

   The `(issue <N>)` parenthetical refers to the source `### Issue N` numbering in the original retro .md, so any digest finding can be traced back via the session label + issue number. Do NOT show the problem-severity score in the header — Apply/Discuss/Skip grouping + the Fix Rating already convey priority, and the severity number was visual noise.

   **Paragraph order is Rating → Problem → Fix** for Apply and Discuss findings — the rating is the decision-relevant info, so it goes first so the user can skim and accept/reject without reading the prose. For Skip findings the Rating/Fix lines collapse to a single `**Why skip.**` line (the finding has already been judged not worth acting on); the Problem line stays so the user knows what they're dismissing.

   Never collapse the rating to a single score. Never drop the Problem line. Never present a finding without its letter handle or its `(issue N)` source reference.

   **Also write the rendered digest to a temp markdown file** — the same content you presented in chat. Use a path OUTSIDE the findings dir so it doesn't get counted as a retro (`find ~/.claude/dockwright/selffix/findings -name '*.md'` feeds the status-line badge). Recipe: `DIGEST="$(mktemp -d)/digest.md"`, then Write the digest markdown to `$DIGEST`. Keep the path — Step 5 annotates this exact file.

5. **Open the digest file in Plannotator via the CLI** so the user annotates it inline. With the Bash tool, run `plannotator annotate "$DIGEST"` and set `run_in_background: true`. It opens the digest's exact content in the browser annotation UI, blocks until the user submits, then prints the structured annotations to stdout (delivered to you when the background task completes). The user marks each finding ✓ / ✗ / ? and leaves freeform notes — much cheaper than an AskUserQuestion form for 5–15 findings.

   **Why the CLI + an explicit file, NOT the `plannotator-last` Skill:** `plannotator-last` runs `plannotator annotate-last`, which self-selects "the last rendered assistant message" by reading the transcript — and that heuristic is unreliable here. Observed: even with the digest committed as its own standalone message immediately before the call, it opened a much earlier "let me derive labels" progress line instead of the digest. Targeting an explicit file with `plannotator annotate <file>` is deterministic and immune to that. (Its `/plannotator-annotate` *slash command* is `disable-model-invocation`, so it can't be reached via the Skill tool — but calling the underlying binary directly with Bash is fine. `run_in_background: true` is what avoids the Bash 600 s timeout while the user reviews at their own pace.)

   When plannotator returns, treat the annotations as authoritative input for Step 7 (Apply). If the annotations are unambiguous (clear ✓/✗ per finding, no open questions), skip Step 6. If any annotation is ambiguous, the finding has no annotation at all, or the user asked a follow-up in the notes, fall through to Step 6 to resolve.

6. **Fallback: `AskUserQuestion`** only for findings plannotator did not resolve. Skip this step entirely when Step 5's annotations were complete. When needed, ask in a single `AskUserQuestion` call:
   - Which apply-fixes to actually apply (multi-select)
   - Which discuss-items to act on now vs defer
   - Whether to mark the remaining sessions reviewed even if no fix applied

7. **Verify each approved fix against the session transcript before editing.** For every fix the user picked, locate the transcript:
   ```bash
   find ~/.claude/projects -name '<sessionId>.jsonl'
   ```
   Then grep / Read it for the specific claim the finding cites — the tool call, error string, missing skill invocation, timing claim, etc. The findings file is the model's interpretation; the transcript is ground truth. If the transcript contradicts the finding, surface the contradiction in chat, downgrade that fix to `discuss`, and skip it for this run. Use targeted Read with `offset`/`limit` or `Bash grep` over the .jsonl — do NOT bulk-load the whole transcript (a multi-hour session is 1–2 MB ≈ 300–500K tokens; loading it preemptively eats half the context budget). Skip this step only for fixes that don't reference session-specific behavior (e.g. "broaden a skill description's phrasing" is verifiable from the skill file alone).

   **Per-fix evidence cite is required, not a batched check.** For each approved fix, write one line BEFORE the corresponding Edit/Write call in Step 8 that names the cite source:
   - `<letter>: transcript-verified — <one-line quote or grep hit>`, OR
   - `<letter>: file-only — verifiable from <path> alone`.

   Batching all verifications into a single bash check at the top of Step 7 hides which fixes were actually grounded and which were taken on faith. The per-fix cite forces the discipline and makes a missed verification visible in the chat log.

8. **Apply approved fixes** — for each verified fix, read the source file referenced in the finding, make the edit. If the fix is behavioral (no code change), skip the file edit but still mark reviewed. Emit the Step 7 cite line for each fix in the wrap-up; "Applied: A, B, C" without cites is a discipline lapse — anyone reading the transcript should be able to confirm each fix was verified.

9. **Mark reviewed** — for every session the user decided on (applied or explicitly dismissed), `touch ~/.claude/dockwright/selffix/findings/<sessionId>.reviewed`. Do NOT auto-mark sessions the user deferred.

## Output Format

Final wrap-up after applying fixes:
```
Applied: <list of file:line changes>
Marked reviewed: <list of session IDs>
Deferred: <list of session IDs and their open questions>
```

## Common Pitfalls

- **Don't rename or delete `.md` files.** The `.reviewed` sibling is the only marker; the originals stay so the user can re-read later.
- **Don't apply fixes the file flagged as `Recommendation: skip`** unless the user explicitly asks. Skip-rated findings are already a judgment.
- **Don't auto-skip human-flagged (`🚩` / `source: manual`) findings.** They were explicitly flagged by the user; surface them above Apply regardless of their recommendation line.
- **Don't mark `Discuss` sessions reviewed without user confirmation.** Discuss means "user must decide" — silently marking it reviewed loses the open question.
- **Don't read findings files in a loop sequentially.** Batch the `Read` calls in parallel.
- **Don't dispatch a subagent for the editing step** — the parent has the conversation state to confirm edits before saving; a subagent loses that thread.
