#!/usr/bin/env python3
"""CLI orchestrator for the investigation eval harness.

Discovers self-contained investigation cases under ``cases/``, drives each
through a headless ``claude -p`` worker (``runner.run_case``) for one or more
samples, scores every sample with the deterministic gate (``gates``) and — only
when the gate passes — an LLM judge (``judge``), then rolls the samples up to a
per-case PASS/FAIL by a ``min_pass``-of-``samples`` majority.

The model under test (``--model``, default opus) and the LLM judge
(``--judge-model``, default opus) are independent CLI-overridable knobs — the
judge is never silently downgraded when ``--model`` picks a cheaper SUT tier.

Usage:
    python -m evals.investigation.run_eval                 # full run, model=opus, judge=opus
    python -m evals.investigation.run_eval --dry-run       # plumbing check, no API calls
    python -m evals.investigation.run_eval --case n01-foo  # one case (repeatable)
    python -m evals.investigation.run_eval --tags abstention --repeats 1
    python -m evals.investigation.run_eval --model sonnet --judge-model opus

Results (real runs only) land in ``results/latest.json``; per-sample traces in
``traces/<run-id>.jsonl``.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import time

from evals.investigation import gates, judge, runner

_HERE = os.path.dirname(os.path.abspath(__file__))
CASES_DIR = os.path.join(_HERE, "cases")
RESULTS_DIR = os.path.join(_HERE, "results")
TRACES_DIR = os.path.join(_HERE, "traces")

# Keys of a scored sample that surface in results/latest.json (the compact form).
_RESULT_SAMPLE_KEYS = (
    "gate_failures", "judge", "error", "cost_usd", "duration_ms", "transcript_missing")


def dry_findings(answer: dict) -> str:
    lines = ["Verdict: dry-run fabricated findings."]
    lines.append(f"ROOT_CAUSE_CATEGORY: {answer.get('expected_category', 'insufficient_evidence')}")
    lines += answer.get("required_keywords") or []
    lines += answer.get("ruling_out_keywords") or []
    return "\n".join(lines)


def dry_run_case(case: dict, **_kwargs) -> runner.RunRecord:
    """Fabricate a RunRecord that passes the deterministic gate without any API
    call — the ``--dry-run`` stand-in for ``runner.run_case``."""
    answer = case["answer"]
    findings = dry_findings(answer)
    tool_calls = [
        ("Read", json.dumps({"file_path": r}))
        for r in (answer.get("required_reads") or [])
    ]
    return runner.RunRecord(
        case_id=case["case_id"], findings=findings, tool_calls=tool_calls,
        corpus=findings, num_turns=1,
    )


def discover_cases(cases_dir, *, limit, only_ids, tags) -> list[dict]:
    """Load every case under ``cases_dir`` (a subdir with case.json), applying
    the --case / --tags / --limit filters. Missing/empty dir -> []."""
    if not os.path.isdir(cases_dir):
        return []
    cases: list[dict] = []
    for name in sorted(os.listdir(cases_dir)):
        case_dir = os.path.join(cases_dir, name)
        if not os.path.isdir(case_dir):
            continue
        if not os.path.exists(os.path.join(case_dir, "case.json")):
            continue
        case = runner.load_case(case_dir)
        if only_ids is not None and case["case_id"] not in only_ids:
            continue
        if tags is not None:
            case_tags = case["meta"].get("tags") or []
            if not any(t in case_tags for t in tags):
                continue
        cases.append(case)
    return cases[:limit] if limit is not None else cases


def _score_sample(rec: runner.RunRecord, answer, rubric, *, skip_judge, judge_fn,
                  judge_model) -> dict:
    sample = {
        "gate_failures": None, "judge": None, "error": rec.error,
        "cost_usd": rec.cost_usd, "duration_ms": rec.duration_ms,
        "transcript_missing": rec.transcript_missing,
        "findings": rec.findings, "session_id": rec.session_id, "passed": False,
    }
    if rec.error:  # errored run is a failed sample — no gating attempted
        return sample
    gate = gates.score_deterministic(
        findings=rec.findings, tool_calls=rec.tool_calls, num_turns=rec.num_turns,
        answer=answer, corpus=rec.corpus,
    )
    sample["gate_failures"] = gate.failures
    if not gate.passed:
        return sample
    if skip_judge:  # covers --skip-judge and --dry-run (judge skipped == pass)
        sample["passed"] = True
        return sample
    score = judge_fn(rec.findings, rubric, model=judge_model)
    sample["judge"] = score
    sample["passed"] = score >= judge.JUDGE_THRESHOLD
    return sample


def evaluate_case(case, *, model, timeout, repeats, skip_judge, run_case_fn,
                  judge_fn, judge_model="opus") -> dict:
    """Run a case for its resolved sample count and roll up to PASS/FAIL.

    samples = --repeats override, else answer["samples"] (default 1).
    min_pass = answer["min_pass"] or ceil(samples/2) [1 when samples==1],
    clamped to samples so --repeats 1 vs a pinned min_pass 2 stays winnable.

    ``model`` drives the SUT worker (``run_case_fn``); ``judge_fn`` is scored
    with the independent ``judge_model`` (default opus) so grading tier never
    silently downgrades with ``--model``.
    """
    answer = case["answer"]
    samples = repeats if repeats is not None else answer.get("samples", 1)
    min_pass = answer.get("min_pass")
    if min_pass is None:
        min_pass = math.ceil(samples / 2) if samples > 1 else 1
    min_pass = min(min_pass, samples)
    rubric = answer.get("rubric", "")

    sample_results = [
        _score_sample(
            run_case_fn(case, model=model, timeout=timeout), answer, rubric,
            skip_judge=skip_judge, judge_fn=judge_fn, judge_model=judge_model,
        )
        for _ in range(samples)
    ]
    passed_samples = sum(s["passed"] for s in sample_results)
    return {
        "case_id": case["case_id"],
        "samples": sample_results,
        "passed": passed_samples >= min_pass,
    }


def _write_results(results: dict) -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, "latest.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(results, indent=2) + "\n")
    return path


def _write_trace(run_id: str, case_results: list[dict]) -> str:
    os.makedirs(TRACES_DIR, exist_ok=True)
    path = os.path.join(TRACES_DIR, f"{run_id}.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for cr in case_results:
            for s in cr["samples"]:
                fh.write(json.dumps({
                    "case_id": cr["case_id"], "findings": s["findings"],
                    "gate_failures": s["gate_failures"], "judge": s["judge"],
                    "session_id": s["session_id"], "error": s["error"],
                    "transcript_missing": s["transcript_missing"],
                }) + "\n")
    return path


def _build_results(run_id, model, repeats, case_results) -> dict:
    cases = [
        {
            "case_id": cr["case_id"],
            "samples": [{k: s[k] for k in _RESULT_SAMPLE_KEYS} for s in cr["samples"]],
            "passed": cr["passed"],
        }
        for cr in case_results
    ]
    passed = sum(cr["passed"] for cr in case_results)
    cost = sum(
        s["cost_usd"] or 0.0 for cr in case_results for s in cr["samples"]
    )
    return {
        "run_id": run_id,
        "model": model,
        "repeats": repeats,
        "suite_passed": passed == len(case_results),
        "cases": cases,
        "totals": {
            "cases": len(case_results),
            "passed": passed,
            "failed": len(case_results) - passed,
            "cost_usd": round(cost, 4),
        },
    }


def _print_report(case_results: list[dict], model: str) -> None:
    for cr in case_results:
        mark = "PASS" if cr["passed"] else "FAIL"
        passed_n = sum(s["passed"] for s in cr["samples"])
        print(f"  [{mark}] {cr['case_id']}  ({passed_n}/{len(cr['samples'])} samples)")
    passed = sum(cr["passed"] for cr in case_results)
    print(f"investigation suite: {passed}/{len(case_results)} cases passed (model={model})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Investigation eval harness")
    ap.add_argument("--dry-run", action="store_true",
                    help="fabricate passing records (no API calls) — plumbing check")
    ap.add_argument("--limit", type=int, default=None, help="only first N cases")
    ap.add_argument("--case", action="append", dest="cases", default=None,
                    metavar="ID", help="run only this case id (repeatable)")
    ap.add_argument("--tags", default=None, help="comma-separated tag filter")
    ap.add_argument("--model", default="opus", help="worker (SUT) model (default opus)")
    ap.add_argument("--judge-model", default="opus",
                    help="LLM judge model, independent of --model (default opus)")
    ap.add_argument("--repeats", type=int, default=None,
                    help="override per-case sample count")
    ap.add_argument("--skip-judge", action="store_true",
                    help="deterministic gate only, no LLM judge")
    ap.add_argument("--timeout", type=int, default=1800, help="per-sample timeout (s)")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="parallel cases (>1 -> ThreadPoolExecutor)")
    args = ap.parse_args(argv)

    tags = args.tags.split(",") if args.tags else None
    cases = discover_cases(CASES_DIR, limit=args.limit, only_ids=args.cases, tags=tags)
    if not cases:
        print(f"investigation suite: no cases found in {CASES_DIR}")
        return 0

    skip_judge = args.skip_judge or args.dry_run
    run_case_fn = dry_run_case if args.dry_run else runner.run_case
    judge_fn = None if skip_judge else judge.judge_score
    run_id = f"{'dry' if args.dry_run else args.model}-{time.strftime('%Y%m%d-%H%M%S')}"

    def _eval(case):
        return evaluate_case(
            case, model=args.model, timeout=args.timeout, repeats=args.repeats,
            skip_judge=skip_judge, run_case_fn=run_case_fn, judge_fn=judge_fn,
            judge_model=args.judge_model,
        )

    if args.concurrency > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            case_results = list(ex.map(_eval, cases))
    else:
        case_results = [_eval(c) for c in cases]

    _print_report(case_results, args.model)
    _write_trace(run_id, case_results)
    if args.dry_run:  # plumbing check — no results write, always exit 0
        return 0
    _write_results(_build_results(run_id, args.model, args.repeats, case_results))
    return 0 if all(cr["passed"] for cr in case_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
