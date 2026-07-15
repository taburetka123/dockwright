from evals.investigation import gates


ANSWER = {
    "expected_category": "data_state_gap",
    "forbidden_categories": ["code_defect"],
    "required_keywords": ["vendor_to_markets"],
    "ruling_out_keywords": ["mapper is correct"],
    "required_reads": ["fixtures/schema-dump.txt"],
    "forbidden_phrases": ["v9.9.9"],
    "require_value_grounding": True,
    "max_turns": 50,
}

GOOD_FINDINGS = (
    "Verdict: rows never existed.\n"
    "ROOT_CAUSE: source table empty\n"
    "ROOT_CAUSE_CATEGORY: data_state_gap\n"
    "VALIDATED_CLAIMS: vendor_to_markets has 0 rows [fixtures/schema-dump.txt]\n"
    "the mapper is correct.\n"
)

GOOD_CALLS = [("Read", '{"file_path": "/tmp/work/fixtures/schema-dump.txt"}')]
CORPUS = "SELECT COUNT(*) FROM vendor_to_markets -> 0"


def _gate(**overrides):
    kwargs = dict(findings=GOOD_FINDINGS, tool_calls=GOOD_CALLS, num_turns=3,
                  answer=ANSWER, corpus=CORPUS)
    kwargs.update(overrides)
    return gates.score_deterministic(**kwargs)


def test_good_run_passes():
    result = _gate()
    assert result.passed, result.failures
    assert result.category == "data_state_gap"


def test_missing_block_fails():
    r = _gate(findings="prose only, no block")
    assert not r.passed
    assert any("no ROOT_CAUSE_CATEGORY" in f for f in r.failures)


def test_forbidden_category_fails():
    r = _gate(findings=GOOD_FINDINGS.replace("data_state_gap", "code_defect"))
    assert not r.passed


def test_missing_required_keyword_fails():
    r = _gate(findings=GOOD_FINDINGS.replace("vendor_to_markets", "sometable"))
    assert any("required keyword" in f for f in r.failures)


def test_missing_ruling_out_keyword_fails():
    r = _gate(findings=GOOD_FINDINGS.replace("the mapper is correct.", ""))
    assert any("ruling-out" in f for f in r.failures)


def test_required_read_satisfied_by_suffix_and_subagent_calls():
    r = _gate(tool_calls=[("Grep", '{"path": "fixtures/schema-dump.txt", "pattern": "x"}')])
    assert r.passed
    r = _gate(tool_calls=[("Read", '{"file_path": "other.txt"}')])
    assert any("required read" in f for f in r.failures)


def test_max_turns_backstop():
    r = _gate(num_turns=51)
    assert any("turns" in f for f in r.failures)


def test_forbidden_phrase_fails():
    r = _gate(findings=GOOD_FINDINGS + "\nrolled from v9.9.9")
    assert any("forbidden phrase" in f for f in r.failures)


def test_value_grounding_gate():
    r = _gate(findings=GOOD_FINDINGS + "\ndepth was 1,558", corpus=CORPUS)
    assert any("ungrounded" in f for f in r.failures)
    r = _gate(findings=GOOD_FINDINGS + "\ndepth was 1,558", corpus=CORPUS + "\nqueue: 1558")
    assert r.passed


def test_grounding_skipped_when_not_required():
    answer = dict(ANSWER, require_value_grounding=False)
    r = _gate(findings=GOOD_FINDINGS + "\nmystery 9,999", answer=answer)
    assert r.passed
