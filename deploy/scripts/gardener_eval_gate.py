#!/usr/bin/env python3
"""Gardener eval-gate (T8) — behavioral gate between apply and decide.

A proposal whose targets touch a gate-mapped surface must keep the mapped
suite green (run against the LIVE deployed stack, so run this AFTER
gardener_apply.py apply — and after setup.sh for canon targets) before
`gardener_postrun.py decide --kind accept` finalizes it.

  gardener_eval_gate.py --proposal <path> [--map <json>] [--dry-run]
  gardener_eval_gate.py --targets <p1,p2> [--map <json>] [--dry-run]

Exit: 0 = passed with every target mapped, or an explicit --allow-unmapped
skip of a zero-mapped (4) or partially-mapped (5) run;
1 = behavioral failure (revert the diff; human decides decline/defer);
2 = infra (suite could not run, results missing/stale, or every failing case
failed only on genuine harness-infra errors (`claude -p exited N`) —
infra-suspect: do NOT read as a behavioral verdict. A timeout or an
unparseable-output sample is itself SUT-behavioral, not infra, and counts
toward exit 1); 4 = nothing mapped, nothing checked — a zero-mapped run is
NOT a silent pass (the coverage table is printed and a `skipped-unmapped`
ledger event written; pass --allow-unmapped to downgrade the exit to 0 after
confirming the canon gate already covered the touched files);
5 = mapped suites PASSED but >=1 target UNMAPPED (`passed-partial`) —
partial coverage is not a pass at the exit-code layer: the consumers act on
the code, not the printed table, so an unchecked file must not ride through
on a bundled mapped file's green. Never fires when all targets mapped, and
never replaces a failure (1/2 win); --allow-unmapped downgrades 5 -> 0
exactly like 4 -> 0 (the ledger event still records `passed-partial`).

Every run prints a per-file coverage table (each target -> matched suite(s)
or UNMAPPED) and the verdict line names coverage ("N of M touched files
routed to a mapped suite; unmapped: ..."), never a bare PASS.

Mapping: DEFAULT_MAP (generic) + operator overlay
~/.claude/dockwright/gardener/eval-gate-map.json:
  {"extends_default": true, "entries": [
    {"suite": "investigation", "patterns": ["*/skills/my-investigate-skill/*"],
     "args": ["--tags", "evidence-fidelity"]}]}

WHAT THE DEFAULT MAP GATES (rung-3 scope, docs/specs/eval-direction.md
§ Ladder execution record): shipping surfaces (deploy/**) — and only those —
are routed to the repo's own pytest suite (the `pytest` entry in SUITES +
the `*/deploy/*` DEFAULT_MAP entry). Routing, not proof the touched file is
exercised: a green here says the mapped suite passed, not that a test
references the touched file. No LLM; this is the mapping that alone would
have caught the batch that broke main.

The LLM investigation suite is NOT in DEFAULT_MAP: it is a MANUAL instrument
(`python -m evals.investigation.run_eval`) plus an opt-in overlay mapping,
because a behavioral eval of this SUT tier provably cannot detect
skill-surface regressions (a fully inverted skill ran 6/6 green with delivery
content-verified). Review guards that surface instead, and an instruction-only
edit exits 4 — a visible non-pass — rather than a false green. `load_map`
appends the resolved investigate-skill binding to every entry whose
`suite == "investigation"` (normally only the operator's), and the
missing-investigate-skill exit-2 vacuous-pass guard still fires whenever a map
routes a target to that suite. The verifier harness (evals/run_eval.py) stays
measurement-only (unconditional exit 0, no pass bar), so review surfaces are
NOT eval-gated either (backing store: steal-tasklist T8b).

The `eval_gate` ledger event references proposal_id only — never a top-level
`path` key (known_from_ledger() would absorb it into the postrun known-set).
A zero-mapped run writes a `skipped-unmapped` event (with or without
--allow-unmapped); any run that reaches the suite loop writes its
`passed`/`failed`/`error` verdict (a suite that could not run — missing
.venv, timeout — is an in-loop `error` result and still writes one). Only
dry-run and the two pre-run exit-2 guards (missing investigate-skill binding,
unset/missing dockwright_repo) write no event.

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
        "kind": "eval",  # claude -p LLM eval; verdict from results/latest.json
        "module": "evals.investigation.run_eval",
        "results": os.path.join("evals", "investigation", "results", "latest.json"),
        "base_args": ["--concurrency", "3"],
    },
    # DETERMINISTIC, no-LLM: runs the dockwright repo's OWN pytest suite. The
    # `*/deploy/*` DEFAULT_MAP entry routes every shipping-surface change here
    # — the mapping that ALONE would have caught the batch that broke main
    # (all the token leaks + the byte ceiling live under deploy/, and their
    # red tests already ship in this repo). No results file: the process rc IS
    # the verdict (run_pytest_suite).
    "pytest": {
        "kind": "pytest",
    },
}

DEFAULT_MAP = [
    # NO investigation entry, by design — the rung-3 re-scope
    # (docs/specs/eval-direction.md § Ladder execution record; Decisions 8).
    # The skill surface is guarded by REVIEW (Tier-2 / spec-reviewer), not by
    # this eval. With delivery PROVEN (workdir copy + content probes, 8/8),
    # the preamble de-leaked, and the ambient corpus excluded (hermetic
    # `--setting-sources project`) — the production findings-block skeleton
    # deliberately retained, so "priors alone" is NOT the claim — the
    # production-tier SUT still routes around adversarially degraded skill
    # text: Injection B — a fully inverted investigate skill — ran 6/6 GREEN
    # through this gate. A green here was therefore
    # UNEARNED coverage, so the entry is gone and a skill-only proposal now
    # surfaces as exit 4 ("NOTHING was checked … NOT a pass") at the sitting
    # instead of a false GREEN.
    #
    # SUITES["investigation"] deliberately STAYS: the suite remains a valid
    # MANUAL instrument (`python -m evals.investigation.run_eval`), and an
    # operator overlay (~/.claude/dockwright/gardener/eval-gate-map.json) can
    # still map it deliberately — load_map binds the investigate-skill path to
    # any entry whose suite == "investigation", whichever map it came from.
    #
    # Instruction-corpus coverage is therefore NONE by default: rules went in
    # eval-direction A3 (the "ambient-coverage only" mechanism died with the
    # hermetic SUT context, and re-binding a live rule alongside the skill
    # would mask skill degradations — eval-trust Part 2), skills went here.
    # Both return via per-case instruction binding
    # (docs/specs/eval-direction.md §D) — until then an instruction-only edit
    # exits 4 (a visible non-pass), never a false green.
    #
    # shipping surfaces -> the repo's own deterministic pytest suite. `*` in
    # fnmatch spans `/`, so `*/deploy/*` matches both the a/-relative and the
    # realpath-absolute forms match_suites normalizes each target to.
    {"suite": "pytest",
     "patterns": ["*/deploy/*"]},
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
    # The resolved investigate-skill binding names the file the runner copies
    # into the SUT workdir (A2c), so it belongs to the investigation suite and
    # to nothing else. Keyed off the suite, never off a position: post-rung-3
    # DEFAULT_MAP carries no investigation entry, so the old
    # `entries[0] = ...` would bind the skill path onto the PYTEST entry.
    # Zero investigation entries is the normal case and appends nowhere;
    # overlay entries (the only ones that claim the suite now) each get it.
    # Overlay-first ordering — match_suites is first-match-per-suite — is
    # preserved: this rewrites entries in place, it does not reorder them.
    return [dict(e, patterns=list(e.get("patterns") or []) + [investigate_skill()])
            if e.get("suite") == "investigation" else e
            for e in entries]


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


def _coverage(targets, entries):
    """[(target, [suite names matching])] — per-file, for the printed table.
    MIRRORS match_suites' matching predicate EXACTLY (realpath-normalized
    target vs expanduser'd pattern); any divergence would misreport coverage
    vs what actually runs. match_suites only aggregates across targets, so the
    per-file view needs its own loop."""
    rows = []
    for t in targets:
        norm = os.path.realpath(os.path.expanduser(t))
        suites = sorted({
            e["suite"] for e in entries
            if e.get("suite") and any(
                fnmatch.fnmatch(norm, os.path.expanduser(p))
                for p in (e.get("patterns") or []))})
        rows.append((t, suites))
    return rows


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


def _venv_python(repo: str) -> str:
    return os.path.join(repo, ".venv", "bin", "python")


def _suite_cmd(entry, repo: str):
    """The command run_suite would exec for `entry` — for --dry-run display."""
    spec = SUITES.get(entry["suite"])
    if spec is None:
        return ["<unknown suite>"]
    if spec.get("kind") == "pytest":
        return [_venv_python(repo), "-m", "pytest", "-q", "--tb=no",
                "-p", "no:cacheprovider"]
    return ([python_for(repo), "-m", spec["module"]] +
            list(spec["base_args"]) + list(entry["args"]))


def run_pytest_suite(repo: str):
    """Deterministic gate: the dockwright repo's OWN `pytest -q`. rc 0 ->
    passed; rc nonzero -> behavioral FAIL (exit 1) with the last ~15 output
    lines; missing .venv / timeout / cannot-launch -> infra-suspect (exit 2,
    which the review flow already treats as blocking the decide). No
    results.json — the process rc is the verdict."""
    py = _venv_python(repo)
    if not os.path.isfile(py):
        print(f"eval-gate: pytest suite cannot run in {repo}: no .venv — "
              "provision it (python3 -m venv .venv && .venv/bin/pip install "
              "-e '.[dev]'); infra-suspect, blocking (exit 2)", file=sys.stderr)
        return ("error", summarize(None), 2)
    cmd = [py, "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider"]
    print(f"eval-gate: running pytest: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True,
                              timeout=1800)
    except subprocess.TimeoutExpired:
        print(f"eval-gate: pytest suite timed out in {repo} — not a pass; "
              "infra-suspect (exit 2)", file=sys.stderr)
        return ("error", summarize(None), 2)
    except OSError as exc:
        print(f"eval-gate: pytest suite could not launch in {repo}: {exc} — "
              "infra-suspect (exit 2)", file=sys.stderr)
        return ("error", summarize(None), 2)
    if proc.returncode == 0:
        return ("passed", summarize(None), 0)
    stdout = (proc.stdout or "").strip()
    err_tail = "\n".join((proc.stderr or "").strip().splitlines()[-15:])
    if not stdout:
        # rc!=0 with EMPTY stdout is not a failing test — pytest always prints
        # a report. It is a python-level launch failure (e.g. `No module named
        # pytest`) whose only diagnostic is on STDERR. could-not-run is
        # infra-suspect (exit 2), NEVER behavioral (exit 1): classifying it
        # behavioral would blame the change under test for a broken harness,
        # and printing only the empty stdout tail leaves the operator blind
        # (CI run 29931892118 — the venv-symlink launch failure).
        print(f"eval-gate: pytest suite produced no report in {repo} "
              f"(exit {proc.returncode}) — could not run; infra-suspect "
              f"(exit 2):\n{err_tail}", file=sys.stderr)
        return ("error", summarize(None), 2)
    out_tail = "\n".join(stdout.splitlines()[-15:])
    # behavioral failure: append the stderr tail too — a real test failure can
    # still carry diagnostic warnings/errors on stderr.
    print(f"eval-gate: pytest suite FAILED in {repo} (exit {proc.returncode}) "
          f"— behavioral (exit 1):\n{out_tail}\n{err_tail}", file=sys.stderr)
    return ("failed", summarize(None), 1)


def run_suite(entry, repo: str):
    spec = SUITES.get(entry["suite"])
    if spec is None:
        print(f"eval-gate: unknown suite {entry['suite']!r} in map", file=sys.stderr)
        return ("error", summarize(None), 2)
    if spec.get("kind") == "pytest":
        return run_pytest_suite(repo)
    results_path = os.path.join(repo, spec["results"])
    pre_mtime = os.path.getmtime(results_path) if os.path.exists(results_path) else None
    cmd = [python_for(repo), "-m", spec["module"]] + \
        list(spec["base_args"]) + list(entry["args"])
    env = dict(os.environ)
    env["DOCKWRIGHT_INVESTIGATE_SKILL"] = investigate_skill()
    print(f"eval-gate: running {entry['suite']}: {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=repo, env=env)
    return classify(proc.returncode, read_results(results_path, pre_mtime))


def gate_targets(targets, proposal_id, lane, map_path, dry_run,
                 allow_unmapped=False) -> int:
    entries = load_map(map_path)
    rows = _coverage(targets, entries)
    mapped_count = sum(1 for _t, suites in rows if suites)
    unmapped = [t for t, suites in rows if not suites]

    # ALWAYS print the per-file coverage table — no run is ever a silent
    # nothing-was-checked pass (I4).
    print(f"eval-gate: coverage — {mapped_count} of {len(targets)} files mapped")
    for t, suites in rows:
        print(f"  {t}  covered-by: {', '.join(suites)}" if suites
              else f"  {t}  UNMAPPED")

    matched = match_suites(targets, entries)
    if not matched:
        print(f"eval-gate: NOTHING was checked — 0 of {len(targets)} files "
              "mapped; this is NOT a pass")
        gardener_postrun.ledger_append(
            "eval_gate", proposal_id=proposal_id, lane=lane,
            verdict="skipped-unmapped", targets_total=str(len(targets)))
        return 0 if allow_unmapped else 4
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
            print(f"eval-gate: would run {entry['suite']}: "
                  f"{' '.join(_suite_cmd(entry, repo))} "
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
    if code == 0 and unmapped:
        # F1: mapped suites green but >=1 target unmapped — partial coverage
        # must not read as PASS at the exit-code layer (the sitting skill and
        # corpus-watch-run.sh act on the code, not the printed table). Never
        # fires when all targets mapped; never replaces a failure — a 1/2
        # from the loop already won above. --allow-unmapped downgrades 5 -> 0
        # exactly like 4 -> 0; the ledger records `passed-partial` either way.
        verdict = "passed-partial"
        code = 0 if allow_unmapped else 5
    gardener_postrun.ledger_append(
        "eval_gate", proposal_id=proposal_id, lane=lane,
        suites=",".join(sorted(matched)), verdict=verdict,
        cases_passed=str(agg["cases_passed"]),
        cases_failed=str(agg["cases_failed"]),
        errored_samples=str(agg["errored_samples"]),
        cost_usd=str(round(agg["cost"], 4)),
        duration_s=str(int(time.time() - started)),
        targets_total=str(len(targets)),
        targets_mapped=str(mapped_count))
    covered = f"; {mapped_count} of {len(targets)} touched files routed to a mapped suite"
    if unmapped:
        covered += f"; unmapped: {', '.join(unmapped)}"
    print(f"eval-gate: {verdict} (exit {code}){covered}")
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
    parser.add_argument("--allow-unmapped", action="store_true",
                        help="downgrade a zero-mapped run's exit 4 (or a "
                             "partial-coverage run's exit 5) to 0 — the "
                             "explicit, auditable skip; use only after "
                             "confirming the canon gate covered the unmapped "
                             "targets")
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
    return gate_targets(targets, proposal_id, lane, args.map_path, args.dry_run,
                        allow_unmapped=args.allow_unmapped)


if __name__ == "__main__":
    sys.exit(main())
