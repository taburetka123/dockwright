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


def _required_read_params():
    params = []
    for case_dir in ALL_CASES:
        answer = json.loads((case_dir / "answer.json").read_text())
        for rel in answer.get("required_reads", []):
            params.append(pytest.param(case_dir, rel, id=f"{case_dir.name}:{rel}"))
    return params


REQUIRED_READ_PARAMS = _required_read_params()


def test_required_read_params_collected():
    # Recursive drift-guard: the sweep below is itself a guard — assert the
    # exact table size so a case silently losing its required_reads goes red.
    # Update the count when adding/removing cases or required reads.
    assert len(REQUIRED_READ_PARAMS) == 11


@pytest.mark.parametrize("case_dir,rel", REQUIRED_READ_PARAMS)
def test_prompt_and_sibling_fixtures_cannot_satisfy_required_read(
        case_dir, rel, monkeypatch):
    """The content-evidence fallback must be satisfiable ONLY by reading the
    required fixture itself — never by the prompt (scenario echo) plus reads
    of every OTHER fixture. Guards the gate's false-positive vector at
    authoring time, including future cross-fixture content duplication."""
    from evals.investigation import gates, runner

    monkeypatch.setenv("DOCKWRIGHT_INVESTIGATE_SKILL", "/tmp/hermetic/SKILL.md")
    prompt = runner.build_prompt((case_dir / "scenario.md").read_text())
    required = case_dir / rel
    siblings = "\n".join(
        p.read_text(errors="ignore")
        for p in (case_dir / "fixtures").rglob("*")
        if p.is_file() and p.resolve() != required.resolve())
    corpus = prompt + "\n" + siblings
    assert len(gates._distinctive_lines(required.read_text())) >= 2, (
        f"{rel} has <2 distinctive lines — the content arm is dead for it and "
        "this guard would be vacuous; give the fixture >=2 lines of >=8 chars")
    assert not gates._content_satisfied(required.read_text(), corpus), (
        f"{rel} is satisfiable without reading it — scenario or a sibling "
        "fixture echoes its content; rewrite the case so the required "
        "fixture's lines are unique to it")
