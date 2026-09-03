import json

from evals.run_eval import (
    _dry_run_fn,
    aggregate,
    evaluate,
    headline,
    load_cases,
    records_from_trace,
)


def _rec(case_id, label, flagged, defect_class="off-by-one", parsed_ok=True,
         error=None, cost=0.1, dur=1000):
    return {
        "case_id": case_id, "label": label, "defect_class": defect_class,
        "language": "python", "flagged": flagged, "parsed_ok": parsed_ok,
        "method": "json", "cost_usd": cost, "duration_ms": dur, "error": error,
    }


def test_aggregate_majority_and_metrics():
    cases_by_id = {
        "d1": {"id": "d1", "label": "defect", "defect_class": "off-by-one",
               "language": "python"},
        "d2": {"id": "d2", "label": "defect", "defect_class": "wrong-boundary",
               "language": "python"},
        "c1": {"id": "c1", "label": "clean", "defect_class": "clean",
               "language": "python"},
    }
    records = [
        _rec("d1", "defect", True), _rec("d1", "defect", True), _rec("d1", "defect", True),
        _rec("d2", "defect", True, "wrong-boundary"),
        _rec("d2", "defect", False, "wrong-boundary"),
        _rec("d2", "defect", True, "wrong-boundary"),
        _rec("c1", "clean", False, "clean"),
        _rec("c1", "clean", False, "clean"),
        _rec("c1", "clean", False, "clean"),
    ]
    agg = aggregate(records, cases_by_id)
    assert agg["n_cases"] == 3
    assert agg["case_metrics"]["recall"] == 1.0
    assert agg["case_metrics"]["false_positive_rate"] == 0.0
    assert agg["case_metrics"]["precision"] == 1.0
    assert agg["per_defect_class_recall"] == {"off-by-one": 1.0, "wrong-boundary": 1.0}
    assert agg["n_cases_with_disagreement"] == 1
    assert agg["n_runs_scored"] == 9
    assert round(agg["total_cost_usd"], 2) == 0.90


def test_aggregate_counts_errors_and_fallbacks():
    cases_by_id = {"d1": {"id": "d1", "label": "defect",
                          "defect_class": "off-by-one", "language": "python"}}
    records = [
        _rec("d1", "defect", True),
        _rec("d1", "defect", True, parsed_ok=False),
        _rec("d1", "defect", None, error="timeout"),
    ]
    agg = aggregate(records, cases_by_id)
    assert agg["n_runs_errored"] == 1
    assert agg["n_heuristic_fallbacks"] == 1
    assert agg["n_runs_scored"] == 2


def test_headline_string():
    agg = {
        "case_metrics": {"recall": 0.92, "false_positive_rate": 0.08,
                         "precision": 0.9, "accuracy": 0.92, "tp": 11, "fn": 1,
                         "tn": 11, "fp": 1, "n": 24},
        "n_cases": 24,
    }
    h = headline(agg, "sonnet", 3)
    assert "92%" in h and "8%" in h and "24 cases" in h and "sonnet" in h


def test_dry_run_pipeline_end_to_end():
    cases = load_cases()
    records = evaluate(cases, _dry_run_fn, repeats=1, concurrency=2)
    cases_by_id = {c["id"]: c for c in cases}
    agg = aggregate(records, cases_by_id)
    assert agg["n_cases"] == len(cases)
    assert agg["case_metrics"]["recall"] == 1.0
    assert agg["case_metrics"]["false_positive_rate"] == 0.0
    assert agg["n_runs_errored"] == 0


def test_records_from_trace_replays_against_current_dataset(tmp_path):
    trace = tmp_path / "t.jsonl"
    trace.write_text("\n".join([
        json.dumps({"case_id": "d1", "flagged": True, "parsed_ok": True,
                    "cost_usd": 0.2, "duration_ms": 1000, "model": "sonnet"}),
        json.dumps({"case_id": "d1", "flagged": True, "parsed_ok": True,
                    "cost_usd": 0.2, "duration_ms": 1000, "model": "sonnet"}),
        json.dumps({"case_id": "gone", "flagged": False, "model": "sonnet"}),
        "",
    ]) + "\n")
    cases_by_id = {"d1": {"id": "d1", "label": "defect",
                          "defect_class": "resource-leak", "language": "python"}}
    records, model, repeats = records_from_trace(trace, cases_by_id)
    assert model == "sonnet"
    assert repeats == 2
    assert len(records) == 2
    assert records[0]["defect_class"] == "resource-leak"
    assert records[0]["label"] == "defect"
    assert records[0]["flagged"] is True


def test_reaggregate_main_writes_results(tmp_path, monkeypatch):
    import evals.run_eval as R

    monkeypatch.setattr(R, "RESULTS_DIR", tmp_path / "results")
    trace = tmp_path / "tr.jsonl"
    trace.write_text("\n".join(
        json.dumps({"case_id": cid, "flagged": flagged, "parsed_ok": True,
                    "cost_usd": 0.1, "duration_ms": 100, "model": "sonnet"})
        for cid, flagged in [("d09_resourceleak_py", True),
                             ("c01_refactor_extract_py", False)]
    ) + "\n")

    rc = R.main(["--reaggregate", str(trace), "--run-id", "t"])
    assert rc == 0
    assert (tmp_path / "results" / "latest.json").exists()
    out = json.loads((tmp_path / "results" / "t.json").read_text())
    assert "resource-leak" in out["per_defect_class_recall"]
    assert out["trace_file"]
