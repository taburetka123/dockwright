# evals/investigation — regression evals for the investigation behavior stack

Runs the LIVE deployed investigation stack (global `~/.claude` rules + the
investigation skill named by `DOCKWRIGHT_INVESTIGATE_SKILL` (operators can pin
it durably as `[evals] investigate_skill` in `dockwright.toml` — the gardener
eval-gate resolves env > toml > this default), default
`~/.claude/skills/investigate/SKILL.md`) against committed file-fixture cases and scores the
result with deterministic gates (dexter-ported) and an opus judge. The point:
a rule/skill edit that silently breaks investigation behavior becomes a red
suite instead of a latent incident.

**The default path does not exist by default** — pin the real binding in
`dockwright.toml` before relying on the suite (the gardener eval-gate blocks
with exit 2 if the resolved path is missing rather than passing vacuously):

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
`required_reads` (paths that must appear in transcript tool calls, main or
subagent), `forbidden_phrases`, `require_value_grounding` (report values must
appear in captured tool outputs), `samples`/`min_pass`. `expected_category` is
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
