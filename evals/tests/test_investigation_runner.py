import json
import os
from pathlib import Path

from evals.investigation import judge, runner

REPO_ROOT = Path(__file__).resolve().parents[2]


def _fake_claude(payload, returncode=0):
    class Proc:
        def __init__(self):
            self.returncode = returncode
            self.stdout = json.dumps(payload)
            self.stderr = ""
    def fake(cmd, **kwargs):
        fake.last_cmd = cmd
        fake.last_kwargs = kwargs
        return Proc()
    return fake


def _mk_case(tmp_path):
    case = tmp_path / "cases" / "p99-demo"
    (case / "fixtures").mkdir(parents=True)
    (case / "scenario.md").write_text("# Scenario\nSymptom: demo.\n")
    (case / "fixtures" / "log.txt").write_text("err v1.2.3\n")
    (case / "case.json").write_text(json.dumps({"case_id": "p99-demo", "tags": ["demo"]}))
    (case / "answer.json").write_text(json.dumps({"expected_category": "recovered", "rubric": "r"}))
    return str(case)


def test_load_case(tmp_path):
    case = runner.load_case(_mk_case(tmp_path))
    assert case["case_id"] == "p99-demo"
    assert "Symptom" in case["scenario"]
    assert case["answer"]["rubric"] == "r"


def test_prepare_workdir_excludes_answer(tmp_path):
    workdir = runner.prepare_workdir(_mk_case(tmp_path))
    try:
        names = set(os.listdir(workdir))
        assert names == {"scenario.md", "fixtures"}
    finally:
        import shutil
        shutil.rmtree(workdir)


def test_findings_block_skeleton_matches_worker_core():
    core = (REPO_ROOT / "deploy" / "agents" / "worker.core.md").read_text()
    assert runner.FINDINGS_BLOCK_SKELETON.strip() in core


def test_build_prompt_contains_contract(monkeypatch):
    monkeypatch.delenv("DOCKWRIGHT_INVESTIGATE_SKILL", raising=False)
    prompt = runner.build_prompt("SCENARIO-BODY")
    assert "SCENARIO-BODY" in prompt
    assert "skills/investigate/SKILL.md" in prompt
    assert "ROOT_CAUSE_CATEGORY" in prompt
    assert "English" in prompt
    assert "background knowledge" in prompt


def test_build_prompt_skill_path_env_override(monkeypatch):
    monkeypatch.setenv("DOCKWRIGHT_INVESTIGATE_SKILL", "~/own/skills/deep-dig/SKILL.md")
    prompt = runner.build_prompt("SCENARIO-BODY")
    assert "own/skills/deep-dig/SKILL.md" in prompt
    assert "skills/investigate/SKILL.md" not in prompt


def test_run_case_success_and_error(tmp_path):
    case = runner.load_case(_mk_case(tmp_path))
    payload = {"result": "ROOT_CAUSE_CATEGORY: recovered", "session_id": "no-such-sid",
               "total_cost_usd": 0.1, "duration_ms": 5, "num_turns": 2, "is_error": False}
    fake = _fake_claude(payload)
    rec = runner.run_case(case, model="opus", timeout=10, runner=fake)
    assert rec.error is None and rec.findings.startswith("ROOT_CAUSE")
    assert rec.transcript_missing is True  # fake sid has no transcript on disk
    assert "--settings" in fake.last_cmd

    rec = runner.run_case(case, model="opus", timeout=10,
                          runner=_fake_claude({}, returncode=1))
    assert rec.error is not None


def test_judge_score_parses_last_int_and_fails_closed():
    fake = _fake_claude({"result": "Reasoning... SCORE: 85"})
    assert judge.judge_score("f", "r", runner=fake) == 85
    fake_err = _fake_claude({}, returncode=1)
    assert judge.judge_score("f", "r", runner=fake_err) == 0
