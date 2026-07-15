"""Every committed case must be loadable and internally consistent."""
import json
from pathlib import Path

import pytest

CASES_DIR = Path(__file__).resolve().parents[1] / "investigation" / "cases"
VALID_CATEGORIES = {
    "code_defect", "data_state_gap", "race_or_replay", "upstream_invariant_broken",
    "deployment_regression", "external_dependency", "resource_exhaustion",
    "database_contention", "configuration_error", "security_abuse",
    "noise_no_incident", "recovered", "insufficient_evidence",
}

ALL_CASES = sorted(p for p in CASES_DIR.iterdir() if p.is_dir())


def test_cases_exist():
    assert len(ALL_CASES) >= 3


@pytest.mark.parametrize("case_dir", ALL_CASES, ids=lambda p: p.name)
def test_case_shape(case_dir):
    assert (case_dir / "scenario.md").is_file()
    assert (case_dir / "fixtures").is_dir() and any((case_dir / "fixtures").iterdir())
    meta = json.loads((case_dir / "case.json").read_text())
    assert meta["case_id"] == case_dir.name
    assert isinstance(meta.get("tags"), list) and meta["tags"]
    assert meta.get("provenance"), "each case must declare its incident provenance"
    assert isinstance(meta.get("adversarial_signals"), list)
    answer = json.loads((case_dir / "answer.json").read_text())
    assert answer["expected_category"] in VALID_CATEGORIES
    for cat in answer.get("forbidden_categories", []):
        assert cat in VALID_CATEGORIES
    assert answer.get("rubric", "").strip()
    assert isinstance(answer.get("max_turns"), int)
    for rel in answer.get("required_reads", []):
        assert (case_dir / rel).is_file(), f"required_read {rel} missing from case"


@pytest.mark.parametrize("case_dir", ALL_CASES, ids=lambda p: p.name)
def test_answer_values_grounded_in_fixtures(case_dir):
    """A forbidden phrase must never appear in a case's own fixtures — else the
    gate could fail an agent for quoting legitimate evidence."""
    answer = json.loads((case_dir / "answer.json").read_text())
    corpus = "\n".join(
        p.read_text(errors="ignore")
        for p in (case_dir / "fixtures").rglob("*") if p.is_file()
    )
    for phrase in answer.get("forbidden_phrases", []):
        assert phrase not in corpus, (
            f"forbidden phrase {phrase!r} appears in fixtures — the gate could "
            "fail an agent for quoting legitimate evidence")
