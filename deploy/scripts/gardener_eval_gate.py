#!/usr/bin/env python3
"""Gardener eval-gate (T8) — behavioral gate between apply and decide.

A proposal whose targets touch an investigation-facing surface must keep the
investigation eval suite green (run against the LIVE deployed stack, so run
this AFTER gardener_apply.py apply — and after setup.sh for canon targets)
before `gardener_postrun.py decide --kind accept` finalizes it.

  gardener_eval_gate.py --proposal <path> [--map <json>] [--dry-run]
  gardener_eval_gate.py --targets <p1,p2> [--map <json>] [--dry-run]

Exit: 0 = passed or skipped (no mapped surfaces); 1 = behavioral failure
(revert the diff; human decides decline/defer); 2 = infra (suite could not
run, results missing/stale, or every failing case failed only on genuine
harness-infra errors (`claude -p exited N`) — infra-suspect: do NOT read as
a behavioral verdict. A timeout or an unparseable-output sample is itself
SUT-behavioral, not infra, and counts toward exit 1).

Mapping: DEFAULT_MAP (generic) + operator overlay
~/.claude/dockwright/gardener/eval-gate-map.json:
  {"extends_default": true, "entries": [
    {"suite": "investigation", "patterns": ["*/skills/my-investigate-skill/*"],
     "args": ["--tags", "evidence-fidelity"]}]}

v1 LIMITATION: only the investigation suite can gate. The verifier harness
(evals/run_eval.py) is measurement-only — unconditional exit 0, no pass bar —
so review surfaces are NOT gated (backing store: steal-tasklist T8b).

The `eval_gate` ledger event references proposal_id only — never a top-level
`path` key (known_from_ledger() would absorb it into the postrun known-set).
Skipped gates write no event.

Standalone, stdlib-only, py3.9-compatible.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
import gardener_postrun  # sibling deployed script: config, ledger, parser

DEFAULT_INVESTIGATE_SKILL = "~/.claude/skills/investigate/SKILL.md"
JUDGE_THRESHOLD = 70  # mirrors evals/investigation/judge.py::JUDGE_THRESHOLD

SUITES = {
    "investigation": {
        "module": "evals.investigation.run_eval",
        "results": os.path.join("evals", "investigation", "results", "latest.json"),
        "base_args": ["--concurrency", "3"],
    },
}

DEFAULT_MAP = [
    {"suite": "investigation",
     "patterns": [
         "*/skills/*investigat*",
         "*/skills/*investigat*/*",
         "*/rules/investigation-evidence.md",
         "*/agents/worker.core.md",
         "*/agents/worker.md",
     ]},
]


def investigate_skill() -> str:
    """env DOCKWRIGHT_INVESTIGATE_SKILL > dockwright.toml [evals]
    investigate_skill > harness default."""
    env = os.environ.get("DOCKWRIGHT_INVESTIGATE_SKILL", "").strip()
    if env:
        return os.path.expanduser(env)
    toml = gardener_postrun.config_toml_str("evals", "investigate_skill")
    if toml:
        return os.path.expanduser(toml)
    return os.path.expanduser(DEFAULT_INVESTIGATE_SKILL)


def overlay_path() -> str:
    return os.path.join(str(gardener_postrun.GARDENER_DIR), "eval-gate-map.json")


def load_map(map_path=None):
    entries = [dict(e) for e in DEFAULT_MAP]
    entries[0] = dict(entries[0],
                      patterns=list(entries[0]["patterns"]) + [investigate_skill()])
    path = map_path or overlay_path()
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                overlay = json.load(fh)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"eval-gate: bad map file {path}: {exc}", file=sys.stderr)
            raise SystemExit(2)
        extra = overlay.get("entries") or []
        entries = extra + (entries if overlay.get("extends_default", True) else [])
    return entries


def match_suites(targets, entries):
    """{suite: {suite, args}} for every entry any target matches (first
    matching entry per suite wins)."""
    matched = {}
    for t in targets:
        norm = os.path.realpath(os.path.expanduser(t))
        for e in entries:
            suite = e.get("suite")
            pats = e.get("patterns") or []
            if suite and suite not in matched and any(
                    fnmatch.fnmatch(norm, os.path.expanduser(p)) for p in pats):
                matched[suite] = {"suite": suite, "args": list(e.get("args") or [])}
    return matched


# ---- results classification --------------------------------------------

def sample_failed(smp) -> bool:
    if smp.get("error"):
        return True
    if smp.get("gate_failures"):
        return True
    judge = smp.get("judge")
    return judge is not None and judge < JUDGE_THRESHOLD


_INFRA_ERROR_PREFIX = "claude -p exited"


def _is_infra_error(err) -> bool:
    """True only for genuine harness-infra errors (runner.py's `claude -p
    exited N: ...` string). `RunRecord.error` also covers "timeout after Ns"
    and "unparseable claude -p output" — both are SUT-behavioral (the most
    likely symptoms of a bad skill edit: it hangs, or it breaks the output
    contract), so neither counts as infra here."""
    return isinstance(err, str) and err.startswith(_INFRA_ERROR_PREFIX)


def summarize(results):
    if not results:
        return {"cases_passed": 0, "cases_failed": 0, "errored_samples": 0,
                "failed_cases": [], "all_failures_errored": False, "cost_usd": None}
    cases = results.get("cases") or []
    failed = [c for c in cases if not c.get("passed")]
    errored = sum(1 for c in cases for s in (c.get("samples") or []) if s.get("error"))
    all_err = bool(failed) and all(
        all(_is_infra_error(s.get("error"))
            for s in (c.get("samples") or []) if sample_failed(s))
        for c in failed)
    return {
        "cases_passed": len(cases) - len(failed),
        "cases_failed": len(failed),
        "errored_samples": errored,
        "failed_cases": [c.get("case_id") for c in failed],
        "all_failures_errored": all_err,
        "cost_usd": (results.get("totals") or {}).get("cost_usd"),
    }


def classify(returncode, results):
    """(verdict, summary, exit_code). Behavioral fail only when a failing
    case failed on a real gate/judge miss; errored-only failures are
    infra-suspect (spec-review I4)."""
    summary = summarize(results)
    if returncode not in (0, 1) or results is None:
        return ("error", summary, 2)
    if returncode == 0:
        return ("passed", summary, 0)
    if summary["all_failures_errored"]:
        return ("infra-suspect", summary, 2)
    return ("failed", summary, 1)


# ---- suite execution -----------------------------------------------------

def python_for(repo: str) -> str:
    venv = os.path.join(repo, ".venv", "bin", "python")
    return venv if os.path.exists(venv) else "python3"


def read_results(path: str, pre_mtime):
    """Parsed latest.json, or None when missing/stale (not rewritten by this
    run) / unparseable."""
    if not os.path.exists(path):
        return None
    if pre_mtime is not None and os.path.getmtime(path) == pre_mtime:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def run_suite(entry, repo: str):
    spec = SUITES.get(entry["suite"])
    if spec is None:
        print(f"eval-gate: unknown suite {entry['suite']!r} in map", file=sys.stderr)
        return ("error", summarize(None), 2)
    results_path = os.path.join(repo, spec["results"])
    pre_mtime = os.path.getmtime(results_path) if os.path.exists(results_path) else None
    cmd = [python_for(repo), "-m", spec["module"]] + \
        list(spec["base_args"]) + list(entry["args"])
    env = dict(os.environ)
    env["DOCKWRIGHT_INVESTIGATE_SKILL"] = investigate_skill()
    print(f"eval-gate: running {entry['suite']}: {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=repo, env=env)
    return classify(proc.returncode, read_results(results_path, pre_mtime))


def gate_targets(targets, proposal_id, lane, map_path, dry_run) -> int:
    matched = match_suites(targets, load_map(map_path))
    if not matched:
        print("eval-gate: skipped (no mapped surfaces)")
        return 0
    if "investigation" in matched:
        skill_path = investigate_skill()
        if not os.path.exists(skill_path):
            print(
                "eval-gate: investigate skill NOT FOUND at resolved path "
                f"{skill_path} — a missing binding is a VACUOUS PASS (the "
                "suite would run with nothing to read), blocking (exit 2). "
                "Resolution order: env DOCKWRIGHT_INVESTIGATE_SKILL > "
                "[evals] investigate_skill in dockwright.toml > default "
                f"({DEFAULT_INVESTIGATE_SKILL})", file=sys.stderr)
            return 2
    repo = gardener_postrun._dockwright_repo()
    if not repo or not os.path.isdir(repo):
        print("eval-gate: [paths] dockwright_repo unset or missing but a target "
              "is gate-mapped — cannot run the suite (exit 2)", file=sys.stderr)
        return 2
    if dry_run:
        for entry in matched.values():
            spec = SUITES.get(entry["suite"])
            cmd = ([python_for(repo), "-m", spec["module"]] + list(spec["base_args"]) +
                   list(entry["args"])) if spec else ["<unknown suite>"]
            print(f"eval-gate: would run {entry['suite']}: {' '.join(cmd)} "
                  f"(cwd={repo}, DOCKWRIGHT_INVESTIGATE_SKILL={investigate_skill()})")
        return 0
    started = time.time()
    worst = ("passed", summarize(None), 0)
    agg = {"cases_passed": 0, "cases_failed": 0, "errored_samples": 0, "cost": 0.0}
    for entry in matched.values():
        verdict, summary, code = run_suite(entry, repo)
        agg["cases_passed"] += summary["cases_passed"]
        agg["cases_failed"] += summary["cases_failed"]
        agg["errored_samples"] += summary["errored_samples"]
        agg["cost"] += summary["cost_usd"] or 0.0
        if summary["failed_cases"]:
            print(f"eval-gate: {entry['suite']} failing cases: "
                  f"{', '.join(summary['failed_cases'])}")
        rank = {"passed": 0, "infra-suspect": 1, "error": 1, "failed": 2}
        if rank[verdict] > rank[worst[0]]:
            worst = (verdict, summary, code)
    verdict, _summary, code = worst
    gardener_postrun.ledger_append(
        "eval_gate", proposal_id=proposal_id, lane=lane,
        suites=",".join(sorted(matched)), verdict=verdict,
        cases_passed=str(agg["cases_passed"]),
        cases_failed=str(agg["cases_failed"]),
        errored_samples=str(agg["errored_samples"]),
        cost_usd=str(round(agg["cost"], 4)),
        duration_s=str(int(time.time() - started)))
    print(f"eval-gate: {verdict} (exit {code})")
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the mapped eval suite for a gardener proposal's targets.")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--proposal", help="proposal file; targets from frontmatter")
    src.add_argument("--targets", help="comma-separated target paths (ad-hoc)")
    parser.add_argument("--map", dest="map_path", default=None,
                        help="override the overlay map path (tests/E2E)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print resolved suites/commands, run nothing")
    args = parser.parse_args(argv)

    if args.proposal:
        try:
            with open(args.proposal, encoding="utf-8") as fh:
                meta, body = gardener_postrun.parse_frontmatter(fh.read())
        except OSError as exc:
            print(f"eval-gate: cannot read proposal: {exc}", file=sys.stderr)
            return 2
        if not isinstance(meta, dict):
            print("eval-gate: no parseable frontmatter", file=sys.stderr)
            return 2
        # gate on the UNION of declared targets and the diff's actual paths —
        # the actuator applies whatever the diff names, not just what
        # `targets:` declares, so an absolute-path diff hunk that patches an
        # undeclared gate-mapped surface must still be caught here.
        declared = gardener_postrun._as_list(meta.get("targets"))
        targets = list(dict.fromkeys(declared + gardener_postrun.diff_paths(body)))
        proposal_id = str(meta.get("id"))
        lane = str(meta.get("lane") or "digest")
    else:
        targets = [t for t in args.targets.split(",") if t.strip()]
        proposal_id, lane = "adhoc", ""
    if not targets:
        print("eval-gate: no targets", file=sys.stderr)
        return 2
    return gate_targets(targets, proposal_id, lane, args.map_path, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
