#!/usr/bin/env python3
"""Render the latest eval result into the RESULTS block of docs/evals.md.

Keeps the committed headline in sync with evals/results/latest.json so the doc
is reproducible: `python -m evals.run_eval && python -m evals.render_results`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVALS_DIR.parent
LATEST = EVALS_DIR / "results" / "latest.json"
DOC = REPO_ROOT / "docs" / "evals.md"

_BLOCK = re.compile(
    r"(<!-- RESULTS:BEGIN.*?-->\n).*?(\n<!-- RESULTS:END -->)", re.DOTALL
)


def _pct(x):
    return "n/a" if x is None else f"{x * 100:.0f}%"


def render(result: dict) -> str:
    cm = result["case_metrics"]
    rm = result["run_metrics"]
    lines = [
        f"> **{result['headline']}**",
        "",
        f"_Run `{result['run_id']}` · model `{result['model']}` · "
        f"{result['repeats']} runs/case · {result['n_cases']} cases · "
        f"${result['total_cost_usd']} · majority vote._",
        "",
        "| Metric | Case-level (majority) | Run-level (all runs) |",
        "|---|---|---|",
        f"| catch-rate (recall) | **{_pct(cm['recall'])}** | {_pct(rm['recall'])} |",
        f"| false-positive rate | **{_pct(cm['false_positive_rate'])}** | {_pct(rm['false_positive_rate'])} |",
        f"| precision | **{_pct(cm['precision'])}** | {_pct(rm['precision'])} |",
        f"| accuracy | {_pct(cm['accuracy'])} | {_pct(rm['accuracy'])} |",
        "",
        f"confusion (case-level): TP={cm['tp']} · FN={cm['fn']} · "
        f"TN={cm['tn']} · FP={cm['fp']} (n={cm['n']})",
        "",
        f"per-defect-class catch-rate: "
        + ", ".join(f"`{k}` {_pct(v)}" for k, v in result["per_defect_class_recall"].items()),
        "",
        f"determinism: mean per-case agreement "
        f"{_pct(result['mean_case_agreement'])}, "
        f"{result['n_cases_with_disagreement']}/{result['n_cases']} cases "
        f"disagreed across the {result['repeats']} runs · "
        f"heuristic-fallback parses: {result['n_heuristic_fallbacks']}/"
        f"{result['n_runs_scored']} · errored runs: {result['n_runs_errored']}",
    ]
    return "\n".join(lines)


def main() -> int:
    if not LATEST.exists():
        raise SystemExit(f"no results at {LATEST}; run `python -m evals.run_eval` first")
    result = json.loads(LATEST.read_text())
    block = render(result)
    text = DOC.read_text()
    new, n = _BLOCK.subn(rf"\g<1>{block}\g<2>", text)
    if n != 1:
        raise SystemExit("RESULTS markers not found exactly once in docs/evals.md")
    DOC.write_text(new)
    print(f"updated {DOC} from {LATEST.name}")
    print(result["headline"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
