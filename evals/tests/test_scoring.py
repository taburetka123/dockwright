"""TDD coverage for the pure scoring/parsing logic — no network, no claude calls."""
from evals.scoring import (
    Confusion,
    classify,
    confusion_from,
    flagged_defective,
    majority,
    metrics,
    parse_verdict,
)


# ---------------------------------------------------------------- parse_verdict
def test_parse_verdict_reads_json_tail():
    text = (
        "The change drops index n.\n\n"
        '```json\n{"has_blocking_issue": true, "highest_severity": "critical", '
        '"ready_to_merge": "no"}\n```'
    )
    v = parse_verdict(text)
    assert v.parsed_ok is True
    assert v.method == "json"
    assert v.has_blocking_issue is True
    assert v.highest_severity == "critical"
    assert v.ready_to_merge == "no"
    assert flagged_defective(v) is True


def test_parse_verdict_clean_json_tail():
    text = '```json\n{"has_blocking_issue": false, "highest_severity": "none", "ready_to_merge": "yes"}\n```'
    v = parse_verdict(text)
    assert v.parsed_ok is True
    assert flagged_defective(v) is False


def test_parse_verdict_uses_last_json_block_when_several():
    text = (
        '```json\n{"has_blocking_issue": true, "highest_severity": "minor", "ready_to_merge": "with_fixes"}\n```\n'
        "on reflection:\n"
        '```json\n{"has_blocking_issue": false, "highest_severity": "none", "ready_to_merge": "yes"}\n```'
    )
    v = parse_verdict(text)
    assert v.has_blocking_issue is False


def test_parse_verdict_heuristic_fallback_blocking():
    # No JSON tail; the model wrote prose with a clear blocking verdict.
    text = (
        "#### Critical (Must Fix)\n1. Off-by-one in the loop bound...\n\n"
        "### Assessment\n**Ready to merge?** No"
    )
    v = parse_verdict(text)
    assert v.parsed_ok is False
    assert v.method == "heuristic"
    assert flagged_defective(v) is True


def test_parse_verdict_heuristic_fallback_clean():
    text = "### Assessment\n**Ready to merge?** Yes\n\nNo blocking issues found."
    v = parse_verdict(text)
    assert v.parsed_ok is False
    assert v.method == "heuristic"
    assert flagged_defective(v) is False


def test_parse_verdict_minor_only_is_not_blocking():
    # A clean change that earns only a style nit must NOT count as flagged.
    text = '```json\n{"has_blocking_issue": false, "highest_severity": "minor", "ready_to_merge": "with_fixes"}\n```'
    v = parse_verdict(text)
    assert flagged_defective(v) is False


# -------------------------------------------------------------------- classify
def test_classify_quadrants():
    assert classify("defect", True) == "tp"
    assert classify("defect", False) == "fn"
    assert classify("clean", True) == "fp"
    assert classify("clean", False) == "tn"


# ------------------------------------------------------------------- confusion
def test_confusion_from_pairs():
    pairs = [
        ("defect", True),   # tp
        ("defect", True),   # tp
        ("defect", False),  # fn
        ("clean", False),   # tn
        ("clean", True),    # fp
    ]
    c = confusion_from(pairs)
    assert (c.tp, c.fn, c.tn, c.fp) == (2, 1, 1, 1)
    assert c.total() == 5


# --------------------------------------------------------------------- metrics
def test_metrics_basic():
    c = Confusion(tp=9, fn=1, tn=11, fp=1)
    m = metrics(c)
    assert m["recall"] == 0.9
    assert m["false_positive_rate"] == round(1 / 12, 4)
    assert m["precision"] == 0.9
    assert m["accuracy"] == round(20 / 22, 4)


def test_metrics_safe_division_no_defects():
    c = Confusion(tp=0, fn=0, tn=5, fp=0)
    m = metrics(c)
    assert m["recall"] is None          # undefined, not a crash
    assert m["false_positive_rate"] == 0.0
    assert m["precision"] is None       # tp+fp == 0


# --------------------------------------------------------------------- majority
def test_majority_vote_and_agreement():
    assert majority([True, True, False]) == (True, round(2 / 3, 4))
    assert majority([False, False, False]) == (False, 1.0)
    assert majority([True]) == (True, 1.0)


def test_majority_tie_breaks_toward_flagged():
    # Even runs with a tie: err on the side of "flagged" (a verifier that
    # ever flags a real defect is doing its job); document the choice.
    assert majority([True, False])[0] is True
