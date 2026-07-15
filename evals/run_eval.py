#!/usr/bin/env python3
"""Offline eval harness for dockwright's code-review verifier.

Feeds each labeled diff in evals/dataset/ to the verifier (a headless
`claude -p` reproduction of the production Tier-2 verifier binding, run
read-only via verifier-settings.json), records the verdict, and scores
catch-rate / false-positive-rate / precision into ONE headline metric.

Usage:
    python -m evals.run_eval                      # full run, model=sonnet, 3 runs/case
    python -m evals.run_eval --model opus         # production-faithful tier
    python -m evals.run_eval --repeats 1 --limit 4  # quick smoke
    python -m evals.run_eval --dry-run            # plumbing check, no API calls

The headline number, full metrics, per-case verdicts and per-run traces are
written under evals/results/ and evals/traces/ and printed to stdout.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

from evals.observability import LangfuseTracer, LocalTraceWriter
from evals.scoring import (
    confusion_from,
    flagged_defective,
    majority,
    metrics,
    parse_verdict,
)
from evals.verifier import build_prompt, run_verifier, settings_path

EVALS_DIR = Path(__file__).resolve().parent
DATASET_DIR = EVALS_DIR / "dataset"
RESULTS_DIR = EVALS_DIR / "results"
TRACES_DIR = EVALS_DIR / "traces"


def load_cases(limit: int | None = None) -> list[dict]:
    cases = [json.loads(p.read_text()) for p in sorted(DATASET_DIR.glob("*.json"))]
    if not cases:
        raise SystemExit(f"no cases found in {DATASET_DIR}")
    return cases[:limit] if limit else cases


# ---------------------------------------------------------------- pure scoring
def aggregate(records: list[dict], cases_by_id: dict[str, dict]) -> dict:
    """Turn per-run records into the full metric bundle. Pure: no I/O.

    A record is {case_id, label, flagged(bool|None on error), parsed_ok,
    method, cost_usd, duration_ms, error}. Runs with error=True are excluded
    from scoring but counted.
    """
    by_case: dict[str, list[bool]] = defaultdict(list)
    errors = 0
    fallback = 0
    cost = 0.0
    durations: list[int] = []
    run_pairs: list[tuple[str, bool]] = []

    for r in records:
        if r.get("error"):
            errors += 1
            continue
        if not r.get("parsed_ok", True):
            fallback += 1
        if r.get("cost_usd"):
            cost += r["cost_usd"]
        if r.get("duration_ms"):
            durations.append(r["duration_ms"])
        by_case[r["case_id"]].append(r["flagged"])
        run_pairs.append((r["label"], r["flagged"]))

    # case-level (majority vote across repeats)
    case_rows = []
    agreements = []
    flips = 0
    case_pairs = []
    class_hits: dict[str, list[bool]] = defaultdict(list)
    for case_id, flags in by_case.items():
        case = cases_by_id[case_id]
        verdict, agreement = majority(flags)
        agreements.append(agreement)
        if agreement < 1.0:
            flips += 1
        case_pairs.append((case["label"], verdict))
        if case["label"] == "defect":
            class_hits[case["defect_class"]].append(verdict)
        case_rows.append(
            {
                "case_id": case_id,
                "label": case["label"],
                "defect_class": case["defect_class"],
                "language": case["language"],
                "runs": flags,
                "majority_flagged": verdict,
                "agreement": agreement,
                "correct": (verdict == (case["label"] == "defect")),
            }
        )

    case_metrics = metrics(confusion_from(case_pairs))
    run_metrics = metrics(confusion_from(run_pairs))

    per_class_recall = {
        cls: round(sum(v) / len(v), 4) for cls, v in sorted(class_hits.items())
    }

    return {
        "case_metrics": case_metrics,
        "run_metrics": run_metrics,
        "per_defect_class_recall": per_class_recall,
        "cases": sorted(case_rows, key=lambda r: r["case_id"]),
        "n_cases": len(case_rows),
        "n_runs_scored": len(run_pairs),
        "n_runs_errored": errors,
        "n_heuristic_fallbacks": fallback,
        "mean_case_agreement": round(sum(agreements) / len(agreements), 4)
        if agreements
        else None,
        "n_cases_with_disagreement": flips,
        "total_cost_usd": round(cost, 4),
        "mean_duration_ms": round(sum(durations) / len(durations)) if durations else None,
    }


def headline(agg: dict, model: str, repeats: int) -> str:
    m = agg["case_metrics"]
    recall = "n/a" if m["recall"] is None else f"{m['recall'] * 100:.0f}%"
    fpr = "n/a" if m["false_positive_rate"] is None else f"{m['false_positive_rate'] * 100:.0f}%"
    prec = "n/a" if m["precision"] is None else f"{m['precision'] * 100:.0f}%"
    return (
        f"Verifier catches {recall} of injected defects at {fpr} false-positive "
        f"rate over {agg['n_cases']} cases (model={model}, {repeats} runs/case, "
        f"majority vote); precision {prec}."
    )


def records_from_trace(trace_path, cases_by_id) -> tuple[list[dict], str, int]:
    """Rebuild scoreable records from a committed trace + the CURRENT dataset.

    Lets you re-score an existing run after a dataset fix WITHOUT spending API
    calls — relabeling a case's defect_class (or fixing a label) does not change
    the verifier's verdict, so the verdict (flagged/parsed_ok) and cost/latency
    are replayed from the trace while label/defect_class are read fresh from the
    dataset. Errored runs are not traced, so n_runs_errored reflects only what
    the trace captured. Returns (records, model, repeats).
    """
    records: list[dict] = []
    model = None
    per_case: dict[str, int] = defaultdict(int)
    for line in Path(trace_path).read_text().splitlines():
        if not line.strip():
            continue
        tr = json.loads(line)
        case = cases_by_id.get(tr["case_id"])
        if case is None:  # case removed from dataset since the trace was taken
            continue
        model = model or tr.get("model")
        per_case[tr["case_id"]] += 1
        records.append({
            "case_id": tr["case_id"],
            "label": case["label"],
            "defect_class": case["defect_class"],
            "language": case["language"],
            "flagged": tr.get("flagged"),
            "parsed_ok": tr.get("parsed_ok", True),
            "cost_usd": tr.get("cost_usd"),
            "duration_ms": tr.get("duration_ms"),
            "error": None,
        })
    repeats = max(per_case.values()) if per_case else 0
    return records, model or "unknown", repeats


def _rel(path) -> str:
    """Repo-relative string for a trace path, robust to relative inputs and to
    traces living outside the repo (falls back to the absolute path)."""
    p = Path(path).resolve()
    try:
        return str(p.relative_to(EVALS_DIR.parent))
    except ValueError:
        return str(p)


def _write_results(agg, head, *, run_id, model, repeats, trace_rel, write_latest):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = {
        "run_id": run_id,
        "model": model,
        "repeats": repeats,
        "headline": head,
        **agg,
        "trace_file": trace_rel,
    }
    (RESULTS_DIR / f"{run_id}.json").write_text(json.dumps(out, indent=2) + "\n")
    if write_latest:
        (RESULTS_DIR / "latest.json").write_text(json.dumps(out, indent=2) + "\n")
    print(f"Wrote {RESULTS_DIR / (run_id + '.json')}")


# --------------------------------------------------------------------- running
def _dry_run_fn(case: dict):
    """Deterministic fake verifier for plumbing checks — verdict == ground truth.
    Produces an obvious 100%/0% result so a non-trivial number means real calls."""
    is_defect = case["label"] == "defect"
    text = (
        f"[DRY RUN] echoing label.\n```json\n"
        f'{{"has_blocking_issue": {str(is_defect).lower()}, '
        f'"highest_severity": "{"critical" if is_defect else "none"}", '
        f'"ready_to_merge": "{"no" if is_defect else "yes"}"}}\n```'
    )
    return parse_verdict(text), {
        "result_text": text,
        "cost_usd": 0.0,
        "duration_ms": 0,
        "usage": None,
        "session_id": "dry-run",
        "model": "dry-run",
    }


def evaluate(cases, run_one, *, repeats, concurrency, trace_writer=None,
             langfuse=None, on_progress=None) -> list[dict]:
    """Run each (case, repeat) through run_one concurrently; return per-run records.

    run_one(case) -> (Verdict, meta_dict). Exceptions become error records so a
    single flaky call never aborts the whole eval.
    """
    tasks = [(c, rep) for c in cases for rep in range(repeats)]
    records: list[dict] = []

    def _do(task):
        case, rep = task
        try:
            verdict, meta = run_one(case)
            return case, rep, verdict, meta, None
        except Exception as exc:  # verifier process failure / timeout
            return case, rep, None, {}, str(exc)

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        for case, rep, verdict, meta, err in ex.map(_do, tasks):
            done += 1
            rec = {
                "case_id": case["id"],
                "label": case["label"],
                "defect_class": case["defect_class"],
                "language": case["language"],
                "repeat": rep,
                "model": meta.get("model"),
                "cost_usd": meta.get("cost_usd"),
                "duration_ms": meta.get("duration_ms"),
            }
            if err:
                rec.update(error=err, flagged=None, parsed_ok=False, method=None)
            else:
                rec.update(
                    error=None,
                    flagged=flagged_defective(verdict),
                    parsed_ok=verdict.parsed_ok,
                    method=verdict.method,
                    ready_to_merge=verdict.ready_to_merge,
                    highest_severity=verdict.highest_severity,
                )
                if trace_writer is not None:
                    trace_writer.write(
                        {**rec, "result_text": meta.get("result_text", ""),
                         "usage": meta.get("usage"), "session_id": meta.get("session_id")}
                    )
                if langfuse is not None:
                    langfuse.record(
                        case_id=case["id"], model=meta.get("model", "?"),
                        prompt=build_prompt(case), output=meta.get("result_text", ""),
                        metadata={"label": case["label"], "repeat": rep,
                                  "flagged": rec["flagged"]},
                        usage=meta.get("usage"),
                    )
            records.append(rec)
            if on_progress:
                on_progress(done, len(tasks), rec)
    return records


# ------------------------------------------------------------------------ main
def _print_report(agg: dict, head: str) -> None:
    print("\n" + "=" * 78)
    print("HEADLINE:", head)
    print("=" * 78)
    cm, rm = agg["case_metrics"], agg["run_metrics"]
    print(
        f"\nCase-level (majority of repeats)  recall={cm['recall']}  "
        f"FPR={cm['false_positive_rate']}  precision={cm['precision']}  "
        f"accuracy={cm['accuracy']}"
    )
    print(
        f"  confusion: TP={cm['tp']} FN={cm['fn']} TN={cm['tn']} FP={cm['fp']} (n={cm['n']})"
    )
    print(
        f"Run-level (every individual run)  recall={rm['recall']}  "
        f"FPR={rm['false_positive_rate']}  precision={rm['precision']}  (n={rm['n']})"
    )
    print(f"\nPer-defect-class catch-rate: {agg['per_defect_class_recall']}")
    print(
        f"Determinism: mean per-case agreement={agg['mean_case_agreement']}, "
        f"{agg['n_cases_with_disagreement']}/{agg['n_cases']} cases disagreed across runs"
    )
    print(
        f"Runs: scored={agg['n_runs_scored']} errored={agg['n_runs_errored']} "
        f"heuristic-fallbacks={agg['n_heuristic_fallbacks']}"
    )
    print(
        f"Cost: ${agg['total_cost_usd']}  mean latency: {agg['mean_duration_ms']} ms"
    )
    misses = [c for c in agg["cases"] if not c["correct"]]
    if misses:
        print("\nMisclassified cases:")
        for c in misses:
            kind = "MISSED defect" if c["label"] == "defect" else "FALSE ALARM"
            print(f"  [{kind}] {c['case_id']} ({c['defect_class']}) runs={c['runs']}")
    print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="sonnet",
                    help="verifier model (default sonnet; opus = production tier)")
    ap.add_argument("--repeats", type=int, default=3,
                    help="runs per case for variance (default 3)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--limit", type=int, default=None, help="only first N cases")
    ap.add_argument("--dry-run", action="store_true",
                    help="fake verifier (no API calls) — plumbing check")
    ap.add_argument("--reaggregate", default=None, metavar="TRACE.jsonl",
                    help="re-score an existing trace against the current dataset "
                         "(no API calls); regenerates results/latest.json")
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args(argv)

    cases = load_cases(limit=args.limit)
    cases_by_id = {c["id"]: c for c in cases}

    if args.reaggregate:
        trace_path = Path(args.reaggregate)
        if not trace_path.exists():
            raise SystemExit(f"no trace at {trace_path}")
        records, model, repeats = records_from_trace(trace_path, cases_by_id)
        run_id = args.run_id or trace_path.stem
        print(f"Re-aggregating {len(records)} runs from {trace_path} "
              f"(model={model}, repeats={repeats}) — no API calls")
        agg = aggregate(records, cases_by_id)
        head = headline(agg, model, repeats)
        _print_report(agg, head)
        _write_results(agg, head, run_id=run_id, model=model, repeats=repeats,
                       trace_rel=_rel(trace_path), write_latest=True)
        return 0

    run_id = args.run_id or f"{'dry' if args.dry_run else args.model}-r{args.repeats}-{int(time.time())}"

    print(f"Eval run {run_id}: {len(cases)} cases x {args.repeats} repeats "
          f"= {len(cases) * args.repeats} verifier calls")
    if not args.dry_run:
        print(f"Verifier: claude -p --model {args.model} "
              f"--settings {settings_path()} (read-only preset)")

    TRACES_DIR.mkdir(parents=True, exist_ok=True)
    trace_path = TRACES_DIR / f"{run_id}.jsonl"
    langfuse = LangfuseTracer(run_id)
    print(f"Observability: local JSONL -> {trace_path} | Langfuse: {langfuse.status}")

    if args.dry_run:
        run_one = _dry_run_fn
    else:
        def run_one(case):
            meta = run_verifier(build_prompt(case), model=args.model,
                                timeout=args.timeout)
            return parse_verdict(meta["result_text"]), meta

    def progress(done, total, rec):
        mark = "ERR" if rec.get("error") else ("FLAG" if rec["flagged"] else "pass")
        print(f"  [{done}/{total}] {rec['case_id']} r{rec['repeat']} -> {mark}",
              file=sys.stderr)

    with LocalTraceWriter(trace_path) as tw:
        records = evaluate(cases, run_one, repeats=args.repeats,
                           concurrency=args.concurrency, trace_writer=tw,
                           langfuse=langfuse, on_progress=progress)
    langfuse.flush()

    model_label = "dry-run" if args.dry_run else args.model
    agg = aggregate(records, cases_by_id)
    head = headline(agg, model_label, args.repeats)
    _print_report(agg, head)
    _write_results(agg, head, run_id=run_id, model=model_label, repeats=args.repeats,
                   trace_rel=_rel(trace_path), write_latest=not args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
