# evals — offline eval harness for the verifier

Measures how well dockwright's code-review **verifier** catches bad
worker output, as one quotable number. The methodology is described below, and
each run writes its headline metric and full results to `results/latest.json`.

## Quick start

```bash
# plumbing check — fake verifier, no API calls, no cost
python -m evals.run_eval --dry-run

# real run (default: model=sonnet, 3 runs/case over 24 cases = 72 calls)
python -m evals.run_eval

# production-faithful tier (the live verifier uses opus)
python -m evals.run_eval --model opus

# fast smoke against real verifier
python -m evals.run_eval --repeats 1 --limit 4

# unit tests for the pure scoring/parsing logic (no network)
python -m pytest evals/tests -q
```

## Layout

| Path | What |
|---|---|
| `dataset/*.json` | 24 labeled cases — 12 injected defects (off-by-one, dropped null-check, wrong boundary, broken error handling, wrong operator, resource leak) + 12 clean changes (incl. "looks-buggy-but-correct" false-positive traps). Each carries an `intent` + unified `diff`. |
| `verifier.py` | Drives the verifier: a headless `claude -p` reproduction of the production Tier-2 verifier binding, run read-only via `deploy/presets/verifier-settings.json`, diff supplied inline. |
| `scoring.py` | Pure verdict-parsing + confusion-matrix / metric math. Unit-tested. |
| `run_eval.py` | Orchestrates the run, scores it, prints the headline, writes results + traces. |
| `observability.py` | Local JSONL traces (always on) + optional Langfuse spans. |
| `results/latest.json` | Last real run's full metrics. |
| `traces/<run-id>.jsonl` | Per-call trace (prompt, raw verdict, cost, latency, tokens). |

## What "flagged" means

The verifier flags a change as defective iff it raises a **Critical or Important**
finding (`has_blocking_issue: true`) — the same bar the orchestrator's own rules
use to block a merge. A Minor style nit on correct code is **not** a flag.
