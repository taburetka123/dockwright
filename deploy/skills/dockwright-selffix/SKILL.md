---
name: dockwright-selffix
description: Use when the user asks to retrospect the just-executed process — "run dockwright-selffix", "self-reflect", "review what just happened", "retro this session", or any explicit retrospective on the current workflow. Distinct from dockwright-selffix-review (which digests already-written retros from disk).
user-invocable: true
---

# Self-Fix: Process Review and Improvement

Review the conversation history and identify what was suboptimal about the process that just ran. This is a general-purpose retrospective — it applies to any workflow, skill, or multi-step task.

> **Headless contract** (see `~/.claude/scripts/selffix-trigger.sh` and `selffix-run.sh`): when invoked via `claude -p "/dockwright-selffix --transcript <path>"` the worker captures this skill's stdout into the findings file. The skill must emit findings to stdout only — no `Write`/`Edit` calls — otherwise the findings diverge from the file the worker wrote and the trigger's findings-exist gate breaks. The worker enforces this with `--disallowedTools "Write,Edit,NotebookEdit"`, so such calls are hard-denied in headless mode. Editing one of these three files? Update the other two.

## Input mode

- **Interactive (default)**: invoked by the user with no arguments. The "conversation history" is the current session — you already see it.
- **Headless** (`/dockwright-selffix --transcript <path>`): invoked by the SessionEnd-hook trigger (`~/.claude/scripts/selffix-trigger.sh`) in a separate `claude -p` process. There is **no** prior conversation in this process. Before Step 1, load the `.jsonl` transcript at `<path>` as "the conversation history" for the rest of the steps. **Always** project the transcript with `jq -r` while `jq` is available and permitted — never `Read` the raw `.jsonl` by preference, regardless of size. If the `jq` projection command is DENIED by the permission system, fall back to the `Read` tool with offset/limit sampling (head + tail of the file) instead of giving up. The raw file is ~85% non-signal (per-record envelope, thinking blocks, duplicate `toolUseResult` output); the projection keeps user text + assistant text + `tool_use` truncated to ~300 chars — a ~19-20× reduction with no quality loss. Recipe:
      ```bash
      jq -r 'select(.type=="user") | .message.content | if type=="string" then . else (.[] | select(.type=="text") | .text) end' <path> | grep -vE '<task-notification>|Monitor event|STALE_(PROCESSING|QUESTION)|AUTOCLOSED|task-id>' | head -c 200000
      jq -r 'select(.type=="assistant") | .message.content[]? | if .type=="text" then .text elif .type=="tool_use" then "TOOL_USE \(.name): \(.input | tostring | .[0:300])" else empty end' <path> | head -c 400000
      ```
      Adjust `head -c` budgets to fit context (run `wc -c <path>` first to gauge size). Read the resulting prose; the .jsonl envelope and tool_result blobs are dropped, which is what makes the projection fit. The user-stream `grep -vE` strips orchestrator noise: manager/worker transcripts are notification-heavy (task-notifications, Monitor events, STALE/AUTOCLOSED markers), and for those sessions the **tail of the conversation + the continuation summary are the highest-signal parts** — don't let the notification volume crowd them out of the budget.

      If the transcript at `<path>` cannot be read at all (missing file, every read tool denied), emit exactly `Status: error (transcript-unreadable)` as your ENTIRE output — never emit apology prose as findings. The worker treats that status as a failed run and enqueues a retry.

   **Output rule**: emit the full structured findings (Step 5 format, all issues, with recommendations) directly to your final response — your stdout IS the findings file; the worker captures it. Do NOT call `Write`, `Edit`, or any other tool to create a separate findings file; do NOT mention paths. Skip Step 6 (no interactive user). Do NOT apply fixes in headless mode.

## Steps

1. **Identify the process**: Look at the conversation history and determine what workflow or multi-step task was just executed. Summarize it in one sentence.

1b. **Human-flagged marker (`/dockwright-fix` command)** — scan the conversation's USER messages for a genuine `/dockwright-fix` command invocation (the deprecated alias `/fix` is recognized for one more release): a user message **whose content STARTS with** the command wrapper `<command-message…>` and carries `<command-name>/dockwright-fix</command-name>` (or `<command-name>/fix</command-name>`). **Position is load-bearing.** A distillation/handoff/journal session embeds a PRIOR session's transcript as its lone user-message string; that transcript can *contain* the tag mid-string (the prior session discussed or invoked `/dockwright-fix`) — that is NOT a flag for THIS session. Real invocations always begin with the wrapper; an embedded payload begins with prose ("Distill this …"). If the tag is present only mid-string, treat the run as an ordinary retro with NO `[MANUAL]` lead (do not fabricate a human flag — it hijacks the lead slot and cannot be rated `skip`). The note the user typed rides in the adjacent `<command-args>…</command-args>` of a genuine invocation. (In headless mode the `jq` projection surfaces both the tag and the args — verified.) If a genuine invocation is present, the user **deliberately flagged this session for the Gardener** — the note in `<command-args>` is the **top-priority** thing to retrospect.
   - Make the flagged point **Issue 1** (the lead). Quote the flagged text (the `<command-args>` note). Rate its impact high; **never rate a human-flagged issue `skip`** — the human explicitly asked for attention; use `discuss` unless the fix is an obvious `apply`.
   - **Tag the issue human-flagged** so the review/digest surfaces recognize it — render the title with the badge and add a `**Source**` line:
     ```
     ### Issue 1: 🚩 [MANUAL] <short title> — <impact>/10
     <what the human flagged and why it matters>
     **Source**: manual — human-flagged in-session via the `/dockwright-fix` command.
     **Fix**: <concrete fix addressing the flagged text>
     **Fix rating**: Impact <N>/10 · Risk <N>/10 · Effort <N>/10 · Confidence <N>/10
     **Recommendation**: discuss — explicit human flag; reviewer decides the action.
     ```

2. **Find issues**: Analyze the execution for problems in these categories:
   - **Correctness**: Did anything produce wrong results, miss edge cases, or fail silently?
   - **Robustness**: What happens if a step fails mid-way? Is the process resumable? Is state lost?
   - **Efficiency**: Was context wasted on large data? Were things done sequentially that could be parallel? Were there unnecessary re-reads or redundant steps?
   - **Clarity**: Were instructions to the AI ambiguous? Could a step be misinterpreted?
   - **Durability**: Does the process survive session restarts? Is intermediate state persisted?

2b. **Mine engineer in-thread corrections (labeled failures — these are gold).** Scan the conversation's USER messages for places where the engineer CORRECTED the assistant: stated that its output, claim, assumption, or action was wrong (factually or procedurally), or reversed its decision. Tells: direct refutation ("это не так", "GRPC — это не штатный механизм", "you're checking the wrong table"), a reversal instruction after the assistant committed to a path, an in-session rule/skill edit prompted by the pushback. An ordinary instruction or answer is NOT a correction — the marker is the engineer contradicting something the assistant already asserted or did. Emit each as its own issue:

   ```
   ### Issue N: ⚖️ [CORRECTION] <short title> — <impact>/10
   <what the assistant asserted/did, and how the engineer corrected it>
   **Source**: engineer-correction — in-thread.
   **Quote**: «<verbatim engineer words, ≤2 lines>»
   **Resolution**: <the corrected truth, one line>
   **Durable fix**: <path of the rule/skill/code fix landed in-session, or "none">
   **Fix**: <concrete follow-through — verify/strengthen the landed fix, or the missing durable fix>
   **Fix rating**: Impact <N>/10 · Risk <N>/10 · Effort <N>/10 · Confidence <N>/10
   **Recommendation**: apply | discuss — never `skip`: a correction is human-labeled ground truth.
   ```

   Corrections are first-class evidence downstream (the Gardener digest weighs them like human-flagged findings), so extract them even when a durable fix already landed — the pipeline still needs to know the failure happened and verify the fix holds.

3. **Rate each issue 0-10 by impact** (0 = cosmetic, 10 = causes failure or data loss). Only report issues rated 3 or higher.

4. **For each issue, draft a concrete fix and rate it on four dimensions** (0-10 each):
   - **Impact** — how much the fix helps if applied. 10 = prevents recurrence across sessions; 1 = cosmetic.
   - **Side-effect risk** — could the fix conflict with existing rules/skills/code, auto-load and compete with something else, or create second-order bugs? 10 = high risk; 1 = additive and isolated. **Always check what already exists** — rules auto-load and may compete with skills with overlapping triggers; new files may shadow existing TRIGGERs. Predict the conflict, don't discover it after deploy.
   - **Effort** — cost to apply. 10 = multiple files + new tests + complex changes; 1 = single-line edit.
   - **Confidence** — sure the fix will actually work. 10 = pattern proven elsewhere in your setup; 1 = speculative.

   A good fix is high Impact, low Risk, low Effort, high Confidence.

5. **Present findings** in this format:

   ```
   ### Issue N: <short title> — <impact>/10
   <What's wrong and why it matters>
   **Fix**: <concrete fix description>
   **Fix rating**: Impact <N>/10 · Risk <N>/10 · Effort <N>/10 · Confidence <N>/10
   **Recommendation**: apply | skip | discuss — <one-line reason>
   ```

   Derive the recommendation from the rating: high Impact + low Risk + high Confidence → `apply`; low Impact or high Risk or low Confidence → `skip`; ambiguous or behavior-change with weak evidence → `discuss`. The one-line reason cites the rating that drove the call.

6. **Ask user which fixes to apply.** Do not apply fixes automatically — present the list and let the user choose. Order issues by recommendation (apply first, then discuss, then skip) so the user reads the high-priority ones first.

7. **Apply approved fixes** to the relevant skill files, rules, or code.

## Guidelines

- Focus on the PROCESS (skill definitions, workflow steps, instructions), not on the specific data from this run.
- Suggest improvements to skill files, CLAUDE.md rules, or memory files — the things that persist across sessions.
- If the process involves skills, read the actual SKILL.md files before suggesting changes.
- Be specific — "improve error handling" is too vague, "add a check for empty API response in step 3 of Phase 1" is good.
- Don't suggest improvements that add complexity without clear benefit.
- **Side-effect check is non-negotiable**: before proposing a fix that adds a new rule or skill, identify any existing rules/skills with overlapping triggers. Two paths to the same goal will compete and one will win — name which one in the Risk score's justification.
