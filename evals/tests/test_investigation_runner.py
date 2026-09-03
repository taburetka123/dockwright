import json
import os
import sys
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


def _bind_skill(monkeypatch, tmp_path, text="# Bound scratch skill\nBe rigorous.\n"):
    skill = tmp_path / "bound" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(text)
    monkeypatch.setenv("DOCKWRIGHT_INVESTIGATE_SKILL", str(skill))
    return skill


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


def test_build_prompt_contains_contract(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKWRIGHT_INVESTIGATE_SKILL", raising=False)
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    prompt = runner.build_prompt("SCENARIO-BODY")
    assert "SCENARIO-BODY" in prompt
    assert "investigate-skill.md" in prompt
    assert "ROOT_CAUSE_CATEGORY" in prompt
    assert "English" in prompt
    assert "background knowledge" in prompt


def test_run_case_copies_bound_skill_into_workdir(monkeypatch, tmp_path):
    bound = _bind_skill(monkeypatch, tmp_path,
                        "# Bound scratch skill\nDISTINCTIVE-BINDING-MARKER-9d3f\n")
    case = runner.load_case(_mk_case(tmp_path))
    seen = {}

    class Proc:
        returncode = 0
        stdout = json.dumps({"result": "x", "session_id": "no-such-sid",
                             "num_turns": 1})
        stderr = ""

    def fake(cmd, **kwargs):
        copied = Path(kwargs["cwd"]) / runner.WORKDIR_SKILL_NAME
        seen["exists"] = copied.exists()
        seen["text"] = copied.read_text() if copied.exists() else None
        seen["cmd"] = cmd
        return Proc()

    rec = runner.run_case(case, model="claude-opus-5", timeout=10, runner=fake)
    assert rec.error is None
    assert seen["exists"] is True
    assert seen["text"] == bound.read_text()
    assert runner.WORKDIR_SKILL_NAME in seen["cmd"][2]
    assert str(bound) not in " ".join(seen["cmd"])


def test_run_case_missing_binding_is_errored_sample(monkeypatch, tmp_path):
    ghost = tmp_path / "gone" / "SKILL.md"
    monkeypatch.setenv("DOCKWRIGHT_INVESTIGATE_SKILL", str(ghost))
    case = runner.load_case(_mk_case(tmp_path))
    fake = _fake_claude({"result": "x", "session_id": "no-such-sid"})
    rec = runner.run_case(case, model="claude-opus-5", timeout=10, runner=fake)
    assert rec.error is not None
    assert "skill binding unreadable" in rec.error
    assert str(ghost) in rec.error
    assert not hasattr(fake, "last_cmd")


def test_investigate_skill_toml_fallback(monkeypatch, tmp_path):
    toml = tmp_path / "dockwright.toml"
    toml.write_text('[evals]\ninvestigate_skill = "~/own/skills/pin/SKILL.md"\n')
    monkeypatch.delenv("DOCKWRIGHT_INVESTIGATE_SKILL", raising=False)
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(toml))
    assert runner.investigate_skill_path().endswith("own/skills/pin/SKILL.md")


def test_investigate_skill_env_beats_toml(monkeypatch, tmp_path):
    toml = tmp_path / "dockwright.toml"
    toml.write_text('[evals]\ninvestigate_skill = "~/own/skills/pin/SKILL.md"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(toml))
    monkeypatch.setenv("DOCKWRIGHT_INVESTIGATE_SKILL", "~/env/wins/SKILL.md")
    assert runner.investigate_skill_path().endswith("env/wins/SKILL.md")


def test_investigate_skill_default_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKWRIGHT_INVESTIGATE_SKILL", raising=False)
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    assert runner.investigate_skill_path().endswith("skills/investigate/SKILL.md")


def test_investigate_skill_toml_fallback_when_dockwright_unimportable(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "dockwright", None)
    toml = tmp_path / "dockwright.toml"
    toml.write_text('[evals]\ninvestigate_skill = "~/fallback/pin/SKILL.md"\n')
    monkeypatch.delenv("DOCKWRIGHT_INVESTIGATE_SKILL", raising=False)
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(toml))
    assert runner.investigate_skill_path().endswith("fallback/pin/SKILL.md")


def test_investigate_skill_toml_fallback_corrupt_file_returns_default(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "dockwright", None)
    toml = tmp_path / "dockwright.toml"
    toml.write_text("not = [valid")
    monkeypatch.delenv("DOCKWRIGHT_INVESTIGATE_SKILL", raising=False)
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(toml))
    assert runner.investigate_skill_path().endswith("skills/investigate/SKILL.md")


def test_run_case_success_and_error(monkeypatch, tmp_path):
    _bind_skill(monkeypatch, tmp_path)
    case = runner.load_case(_mk_case(tmp_path))
    payload = {"result": "ROOT_CAUSE_CATEGORY: recovered", "session_id": "no-such-sid",
               "total_cost_usd": 0.1, "duration_ms": 5, "num_turns": 2, "is_error": False}
    fake = _fake_claude(payload)
    rec = runner.run_case(case, model="opus", timeout=10, runner=fake)
    assert rec.error is None and rec.findings.startswith("ROOT_CAUSE")
    assert rec.transcript_missing is True
    assert "--settings" in fake.last_cmd

    rec = runner.run_case(case, model="opus", timeout=10,
                          runner=_fake_claude({}, returncode=1))
    assert rec.error is not None


def test_judge_score_parses_last_int_and_fails_closed():
    fake = _fake_claude({"result": "Reasoning... SCORE: 85"})
    assert judge.judge_score("f", "r", runner=fake) == 85
    fake_err = _fake_claude({}, returncode=1)
    assert judge.judge_score("f", "r", runner=fake_err) == 0


def test_preamble_does_not_leak_expected_discipline(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKWRIGHT_INVESTIGATE_SKILL", raising=False)
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    prompt = runner.build_prompt("SCENARIO-BODY")
    for leaked in ("hypotheses + falsifiers", "evidence fidelity", "stop block"):
        assert leaked not in prompt
    assert "follow its discipline" in prompt


def test_run_case_is_hermetic(monkeypatch, tmp_path):
    _bind_skill(monkeypatch, tmp_path)
    case = runner.load_case(_mk_case(tmp_path))
    payload = {"result": "x", "session_id": "no-such-sid", "num_turns": 1,
               "is_error": False}
    fake = _fake_claude(payload)
    runner.run_case(case, model="claude-opus-5", timeout=10, runner=fake)
    cmd = fake.last_cmd
    assert "--setting-sources" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == "project"


def test_judge_cmd_is_hermetic_and_pinned():
    fake = _fake_claude({"result": "80"})
    from evals.investigation import judge
    score = judge.judge_score("findings", "rubric", runner=fake)
    assert score == 80
    cmd = fake.last_cmd
    assert "--setting-sources" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == "project"
    assert "claude-opus-5" in cmd
    assert "opus" not in cmd


def test_run_eval_default_models_are_pinned():
    from evals.investigation import run_eval as re_mod
    parser = re_mod.build_parser()
    assert parser.get_default("model") == "claude-opus-5"
    assert parser.get_default("judge_model") == "claude-opus-5"
