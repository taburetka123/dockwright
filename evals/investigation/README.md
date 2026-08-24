# evals/investigation — regression evals for the investigation behavior stack

Runs a HERMETIC investigation session (no operator `~/.claude` rules — the
SUT gets `--setting-sources project`; the bound skill is DELIVERED by copying
it into the session workdir, see § Hermetic harness below) with the
investigation skill named by `DOCKWRIGHT_INVESTIGATE_SKILL` (operators can pin
it durably as `[evals] investigate_skill` in `dockwright.toml` — a direct
suite run resolves env > toml > this default itself, via
`runner.investigate_skill_path()`, and the gardener eval-gate wraps the same
resolver), default `~/.claude/skills/investigate/SKILL.md`, against
committed file-fixture cases, scored by deterministic gates
and a pinned `claude-opus-5` judge. Status caveat (2026-07-28, eval-direction
rung 3): a skill edit does NOT reliably turn this suite red — the
production-tier SUT investigates correctly even under an adversarially
inverted skill (see § Status below) — so the suite is a manual/overlay-mapped
instrument, not the default gate for skill edits.

**The default path does not exist by default** — pin the real binding in
`dockwright.toml` before relying on the suite: a non-dry-run invocation of
the suite itself refuses to run (exit 2) when the resolved path is missing,
rather than passing vacuously, and so does the gardener eval-gate that
wraps it:

```toml
[evals]
investigate_skill = "~/.claude/skills/<your-investigate-skill>/SKILL.md"
```

## Quick start

    # plumbing check - no API calls, $0
    python -m evals.investigation.run_eval --dry-run

    # one case, real run
    python -m evals.investigation.run_eval --case n01-noise-recovered

    # full suite (panels may convene: expect 20-30+ opus dispatches, 30-60 min)
    python -m evals.investigation.run_eval

    # --judge-model sets the LLM judge independently of --model (both default opus)
    python -m evals.investigation.run_eval --model sonnet --judge-model opus

    # unit tests (no network)
    python -m pytest evals/tests -q

## Case anatomy

    cases/<case-id>/
      scenario.md   the brief the agent sees
      case.json     case_id, tags, adversarial_signals (declared red herrings),
                    difficulty, provenance (which documented incident this encodes)
      fixtures/     evidence files the agent Reads/Greps (its ONLY evidence surface)
      answer.json   HIDDEN gates + judge rubric - never copied to the agent workdir

Gate fields (all optional except rubric/expected_category/max_turns):
`forbidden_categories`, `required_keywords`, `ruling_out_keywords`,
`required_reads` (satisfied when the path appears in a transcript tool-call
input, main or subagent, OR when >=80% of the fixture's unique stripped lines
of >=8 chars appear in the captured evidence corpus — user text + tool
outputs, per `deploy/scripts/value_grounding.py`'s `parse_transcripts`, not
literally "tool output" alone — a bash glob like `cat fixtures/*` reads
everything while naming nothing; for fixtures with <=4 distinctive lines 80%
means ALL lines, and <2 distinctive lines never passes the content arm at
all — the per-fixture meta-guard in `test_investigation_cases.py` is what
proves prompt echo cannot satisfy any committed case's required read — so
keep required fixtures as multi-line plain text, not JSON one-liners),
`forbidden_phrases`, `require_value_grounding` (report values must appear in
captured tool outputs), `samples`/`min_pass`. `expected_category` is
documentation + rubric context, never a gate.

## Authoring rules

- Grow the suite only from real, documented failures (same bar as the rules
  corpus). Declare the incident in `case.json.provenance`.
- Every planted red herring goes in `adversarial_signals`.
- Keep >=2 abstention cases (noise_no_incident/recovered) in the suite - a
  gate with only positive cases trains an agent that always finds something.
- Never put a `forbidden_phrase` inside a fixture (the sanity test enforces).
- Anchor required keywords to findings-block field content (category values,
  fixture-verbatim tokens), not prose phrasing - two live runs showed prose
  keywords are a phrasing lottery; let forbidden_categories + the rubric carry
  semantic discrimination.
- Required-read fixtures must be plain text with >=2 distinctive lines (>=8
  chars each). The content-evidence check needs stable line-for-line quoting:
  JSON one-liners can arrive json.dumps-escaped in the corpus and miss.
- The suite refuses to run (exit 2) when the resolved investigate skill is
  missing; direct runs resolve env DOCKWRIGHT_INVESTIGATE_SKILL > dockwright
  toml [evals] investigate_skill > the default, same as the gardener gate.
  The suite prints `investigation suite: skill binding = <path>` at start
  unconditionally — including on `--dry-run` — so the resolved binding is
  always visible before any case runs; `--dry-run` is exempt only from the
  exit-2 refusal, since it fabricates findings and never touches the bound
  skill.

## Hermetic SUT/judge sessions

Both the SUT worker and the LLM judge run with `--setting-sources project` —
no user-level settings, no operator `~/.claude/rules/*`, no user hooks. This
keeps the suite's baseline from depending on this machine's private rules
corpus (the repo is a public-publish candidate; a generic install must pass
the same suite) and keeps a corpus regression from hiding behind an ambient
rule that duplicates the skill's own discipline.

**Skill delivery is structural, not a permission incidental.** Under the
hermetic flag the SUT session cannot `Read` an absolute path outside its
temp workdir (that Read comes back permission-DENIED). So `prepare_workdir`
copies the bound skill INTO the workdir as `investigate-skill.md`, alongside
`scenario.md`, and the preamble tells the SUT to read that relative, always-
readable, in-cwd copy — never an absolute path. The binding itself is
unaffected: env `DOCKWRIGHT_INVESTIGATE_SKILL` > `dockwright.toml`
`[evals] investigate_skill` > the package default still decide WHICH file
gets copied; only the delivery mechanism changed.

## Pinned default models

`--model` and `--judge-model` both default to the concrete `claude-opus-5`
(not a family alias) — a family-alias move must never read as a corpus
regression. Pass `--model claude-sonnet-5` (etc.) to drop the SUT tier; the
judge model is independent and never silently downgrades with `--model`.

## Adversarial injection fixtures (`injections/`)

`injections/ablated/SKILL.md` and `injections/inverted/SKILL.md` are
deliberately degraded investigation skills used only by the eval-direction
proof protocol (`docs/specs/eval-direction.md` Part B) to prove the suite's
own sensitivity to skill-surface regressions — bound ONLY via
`DOCKWRIGHT_INVESTIGATE_SKILL` scratch runs. **Never bind either file
outside that protocol** — never deploy, symlink, or point a real
`dockwright.toml`/env binding at them.

The fixture files and their own `injections/README.md` live in the development
repository ONLY — they are not part of this distribution, because the proof
protocol that gives them meaning is not either.

## Delivery verification (`check_delivery.py`)

A green or red injection verdict is meaningless unless delivery is proven.
`check_delivery.py` is CONTENT-based: for each sample in a trace file it
checks whether the sample's captured evidence corpus (tool results + user
text) contains distinctive verbatim lines of the bound skill — a mere path
mention in a tool-call input is never enough, so a denied or failed Read can
never register as delivery.

    python -m evals.investigation.check_delivery \
        --trace evals/investigation/traces/<run>.jsonl \
        --skill <bound-skill-path> [--expect read|not-read]

Exit 0: every sample matches `--expect` (default `read`). Exit 1: any
mismatch. Exit 2: a sample's transcript is missing, or `--skill` is
unreadable / carries no probe-worthy line — an instrument that cannot
detect delivery says so rather than verdicting every sample NOT-READ.

## Status: manual/overlay instrument, not gate-mapped by default

As of the eval-direction rung-3 re-scope
(`docs/specs/eval-direction.md` § Ladder execution record), the gardener
eval-gate's `DEFAULT_MAP` no longer routes anything to this suite:
delivery-verified runs with the leak and ambient confounds removed (the
production findings-block skeleton retained deliberately — "priors alone"
is not established) showed the production SUT tier routes around a fully
inverted bound skill, so a behavioral eval here cannot detect skill-surface
regressions at that tier in the production-faithful configuration. Review (Tier-2 / spec-reviewer) is
the guard of record for skill-file edits now. `SUITES["investigation"]`
still exists — this suite remains runnable directly
(`python -m evals.investigation.run_eval`) and an operator overlay
(`~/.claude/dockwright/gardener/eval-gate-map.json`) can still map it back
in deliberately.
