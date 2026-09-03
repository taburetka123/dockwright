from evals.investigation import gates


ANSWER = {
    "expected_category": "data_state_gap",
    "forbidden_categories": ["code_defect"],
    "required_keywords": ["vendor_market_map"],
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
    "VALIDATED_CLAIMS: vendor_market_map has 0 rows [fixtures/schema-dump.txt]\n"
    "the mapper is correct.\n"
)

GOOD_CALLS = [("Read", '{"file_path": "/tmp/work/fixtures/schema-dump.txt"}')]
CORPUS = "SELECT COUNT(*) FROM vendor_market_map -> 0"


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
    r = _gate(findings=GOOD_FINDINGS.replace("vendor_market_map", "sometable"))
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


GLOB_CALLS = [("Bash", '{"command": "for f in scenario.md fixtures/*; do cat \\"$f\\"; done"}')]
FIXTURE_TEXT = (
    "error_rate queried 13:15 UTC\n"
    "12:00 0.052 | 13:10 0.0007\n"
    "no errors from sessions started after 12:20\n"
)
GLOB_CORPUS = "=============== fixtures/schema-dump.txt\n" + FIXTURE_TEXT + CORPUS


def test_required_read_satisfied_by_content_evidence():
    r = _gate(tool_calls=GLOB_CALLS, corpus=GLOB_CORPUS,
              fixture_texts={"fixtures/schema-dump.txt": FIXTURE_TEXT})
    assert not any("required read" in f for f in r.failures), r.failures


def test_required_read_content_needs_most_lines():
    partial = "=============== something\n12:00 0.052 | 13:10 0.0007\n" + CORPUS
    r = _gate(tool_calls=GLOB_CALLS, corpus=partial,
              fixture_texts={"fixtures/schema-dump.txt": FIXTURE_TEXT})
    assert any("required read" in f for f in r.failures)


def test_required_read_content_no_distinctive_lines_never_passes():
    r = _gate(tool_calls=GLOB_CALLS, corpus="short\ntiny!\n" + CORPUS,
              fixture_texts={"fixtures/schema-dump.txt": "short\ntiny!\n"})
    assert any("required read" in f for f in r.failures)


def test_required_read_still_fails_when_unread():
    r = _gate(tool_calls=GLOB_CALLS, corpus=CORPUS,
              fixture_texts={"fixtures/schema-dump.txt": FIXTURE_TEXT})
    assert any("required read" in f for f in r.failures)


def test_required_read_input_match_still_works_without_fixture_texts():
    r = _gate()
    assert not r.failures


def test_content_satisfied_pins_fraction_boundary():
    fixture_text = (
        "alpha_line_one\n"
        "beta_line_two\n"
        "gamma_line_three\n"
        "delta_line_four\n"
        "epsilon_line_five\n"
    )
    assert len(gates._distinctive_lines(fixture_text)) == 5
    corpus_four_of_five = "alpha_line_one beta_line_two gamma_line_three delta_line_four"
    assert gates._content_satisfied(fixture_text, corpus_four_of_five) is True
    corpus_three_of_five = "alpha_line_one beta_line_two gamma_line_three"
    assert gates._content_satisfied(fixture_text, corpus_three_of_five) is False


def test_content_satisfied_dedups_repeated_lines():
    repeated = "repeated_distinctive_line"
    unique = "only_once_distinctive_line"
    fixture_text = (repeated + "\n") * 10 + unique + "\n"
    assert len(gates._distinctive_lines(fixture_text)) == 2
    assert gates._content_satisfied(fixture_text, repeated) is False


def test_content_satisfied_single_line_never_passes():
    fixture_text = "single_distinctive_line\n"
    assert gates._content_satisfied(fixture_text, fixture_text) is False


def test_content_satisfied_two_lines_both_present_passes():
    fixture_text = "alpha_line_one\nbeta_line_two\n"
    assert len(gates._distinctive_lines(fixture_text)) == 2
    assert gates._content_satisfied(fixture_text, fixture_text) is True
