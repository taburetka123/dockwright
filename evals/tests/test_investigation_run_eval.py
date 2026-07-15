import json

from evals.investigation import run_eval, runner


ANSWER = {
    "expected_category": "recovered",
    "required_keywords": ["returned to baseline"],
    "ruling_out_keywords": ["no ongoing impact"],
    "required_reads": ["fixtures/metrics.txt"],
    "samples": 3,
    "min_pass": 2,
    "rubric": "must abstain",
}


def _case(tmp_path, answer=ANSWER):
    d = tmp_path / "cases" / "n99-demo"
    (d / "fixtures").mkdir(parents=True)
    (d / "scenario.md").write_text("s")
    (d / "fixtures" / "metrics.txt").write_text("m")
    (d / "case.json").write_text(json.dumps({"case_id": "n99-demo", "tags": ["abstention"]}))
    (d / "answer.json").write_text(json.dumps(answer))
    return runner.load_case(str(d))


def test_dry_findings_pass_the_gate(tmp_path):
    case = _case(tmp_path)
    result = run_eval.evaluate_case(
        case, model="opus", timeout=5, repeats=None, skip_judge=True,
        run_case_fn=run_eval.dry_run_case, judge_fn=None)
    assert result["passed"], result
    # transcript_missing surfaces per sample so a transcript-recovery failure is
    # diagnosable in both results and traces, not disguised as gate noise.
    assert "transcript_missing" in result["samples"][0]
    built = run_eval._build_results("run", "opus", None, [result])
    assert "transcript_missing" in built["cases"][0]["samples"][0]


def test_min_pass_semantics(tmp_path):
    case = _case(tmp_path)
    calls = {"n": 0}
    def flaky_run(case, **kw):
        calls["n"] += 1
        good = runner.RunRecord(case_id=case["case_id"],
                                findings=run_eval.dry_findings(case["answer"]),
                                tool_calls=[("Read", '{"file_path": "fixtures/metrics.txt"}')],
                                corpus="x", num_turns=1)
        bad = runner.RunRecord(case_id=case["case_id"], error="boom")
        return good if calls["n"] % 3 else bad  # every 3rd sample errors
    result = run_eval.evaluate_case(case, model="opus", timeout=5, repeats=None,
                                    skip_judge=True, run_case_fn=flaky_run, judge_fn=None)
    assert calls["n"] == 3          # samples from answer.json
    assert result["passed"]         # 2 of 3 >= min_pass 2


def test_repeats_overrides_samples(tmp_path):
    case = _case(tmp_path)
    seen = {"n": 0}
    def counting(case, **kw):
        seen["n"] += 1
        return runner.RunRecord(case_id=case["case_id"], error="always")
    run_eval.evaluate_case(case, model="opus", timeout=5, repeats=1,
                           skip_judge=True, run_case_fn=counting, judge_fn=None)
    assert seen["n"] == 1


def test_judge_only_after_gate_pass(tmp_path):
    case = _case(tmp_path, dict(ANSWER, samples=1, min_pass=1))
    judged = {"n": 0}
    def fake_judge(findings, rubric, **kw):
        judged["n"] += 1
        return 90
    def bad_run(case, **kw):
        return runner.RunRecord(case_id=case["case_id"], findings="no block",
                                tool_calls=[], corpus="", num_turns=1)
    r = run_eval.evaluate_case(case, model="opus", timeout=5, repeats=None,
                               skip_judge=False, run_case_fn=bad_run, judge_fn=fake_judge)
    assert judged["n"] == 0 and not r["passed"]


def test_judge_uses_independent_judge_model(tmp_path):
    case = _case(tmp_path, dict(ANSWER, samples=1, min_pass=1))
    seen = {}
    def fake_judge(findings, rubric, **kw):
        seen["model"] = kw.get("model")
        return 90
    def good_run(case, **kw):
        return runner.RunRecord(
            case_id=case["case_id"], findings=run_eval.dry_findings(case["answer"]),
            tool_calls=[("Read", '{"file_path": "fixtures/metrics.txt"}')],
            corpus="x", num_turns=1)
    r = run_eval.evaluate_case(case, model="sonnet", timeout=5, repeats=None,
                               skip_judge=False, run_case_fn=good_run, judge_fn=fake_judge,
                               judge_model="opus")
    assert r["passed"], r
    assert seen["model"] == "opus"


def test_discover_cases_filters(tmp_path):
    _case(tmp_path)
    cases = run_eval.discover_cases(str(tmp_path / "cases"), limit=None,
                                    only_ids=None, tags=["abstention"])
    assert [c["case_id"] for c in cases] == ["n99-demo"]
    assert run_eval.discover_cases(str(tmp_path / "cases"), limit=None,
                                   only_ids=None, tags=["nope"]) == []


def test_main_dry_run_exit_zero_and_no_latest(tmp_path, monkeypatch, capsys):
    _case(tmp_path)
    monkeypatch.setattr(run_eval, "CASES_DIR", str(tmp_path / "cases"))
    monkeypatch.setattr(run_eval, "RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setattr(run_eval, "TRACES_DIR", str(tmp_path / "traces"))
    rc = run_eval.main(["--dry-run"])
    assert rc == 0
    assert "n99-demo" in capsys.readouterr().out
    assert not (tmp_path / "results" / "latest.json").exists()
