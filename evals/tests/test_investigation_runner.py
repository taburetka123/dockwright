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
    """run_case now COPIES the bound skill into the workdir, so every run_case
    test needs a real binding — the machine default may or may not exist."""
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


def test_findings_block_skeleton_matches_worker_core():
    core = (REPO_ROOT / "deploy" / "agents" / "worker.core.md").read_text()
    assert runner.FINDINGS_BLOCK_SKELETON.strip() in core


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
    """eval-direction A2c: the binding no longer reaches the SUT as a path in
    the prompt — it selects WHICH file gets copied to <workdir>/
    investigate-skill.md. Asserted from INSIDE the fake runner: run_case
    deletes the workdir in its finally, so the copy exists only while
    `claude -p` would be running.

    (Replaces test_build_prompt_skill_path_env_override — the prompt carries
    no bound path to assert on any more.)"""
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
    assert runner.WORKDIR_SKILL_NAME in seen["cmd"][2]  # the prompt
    assert str(bound) not in " ".join(seen["cmd"])  # no absolute path leaks


def test_run_case_missing_binding_is_errored_sample(monkeypatch, tmp_path):
    """A binding that vanished mid-run is an errored sample, never a raise and
    never a skill-less run that looks healthy (run_eval's exit-2 guard covers
    the start-of-run case; this is the belt)."""
    ghost = tmp_path / "gone" / "SKILL.md"
    monkeypatch.setenv("DOCKWRIGHT_INVESTIGATE_SKILL", str(ghost))
    case = runner.load_case(_mk_case(tmp_path))
    fake = _fake_claude({"result": "x", "session_id": "no-such-sid"})
    rec = runner.run_case(case, model="claude-opus-5", timeout=10, runner=fake)
    assert rec.error is not None
    assert "skill binding unreadable" in rec.error
    assert str(ghost) in rec.error
    assert not hasattr(fake, "last_cmd")  # claude -p never ran


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
    """Drives runner._toml_config's own tomllib fallback branch (not
    dockwright.config.load) by blocking `from dockwright import config` —
    the path a direct `python -m` run takes on a host without the editable
    install."""
    monkeypatch.setitem(sys.modules, "dockwright", None)
    toml = tmp_path / "dockwright.toml"
    toml.write_text('[evals]\ninvestigate_skill = "~/fallback/pin/SKILL.md"\n')
    monkeypatch.delenv("DOCKWRIGHT_INVESTIGATE_SKILL", raising=False)
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(toml))
    assert runner.investigate_skill_path().endswith("fallback/pin/SKILL.md")


def test_investigate_skill_toml_fallback_corrupt_file_returns_default(monkeypatch, tmp_path):
    """Same import-blocked fallback path, but DOCKWRIGHT_CONFIG names a file
    that exists and fails to parse — must return {} (harness default), never
    fall through to another candidate."""
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


def test_preamble_does_not_leak_expected_discipline(monkeypatch, tmp_path):
    """eval-direction A1: the harness must never describe what the bound skill
    should contain — the leaked description anchored the SUT's refusal of an
    inverted skill (eval-trust findings, Injection B)."""
    monkeypatch.delenv("DOCKWRIGHT_INVESTIGATE_SKILL", raising=False)
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    prompt = runner.build_prompt("SCENARIO-BODY")
    for leaked in ("hypotheses + falsifiers", "evidence fidelity", "stop block"):
        assert leaked not in prompt
    # the read instruction itself survives the de-leak (the current text line-
    # wraps as "follow\n  its discipline", so this asserts red pre-change too)
    assert "follow its discipline" in prompt


def test_run_case_is_hermetic(monkeypatch, tmp_path):
    """eval-direction A2: SUT sessions must not load the operator's ambient
    corpus (spiked: --setting-sources project suppresses user rules, keeps the
    --settings preset deny-list and transcript writing)."""
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
    """eval-direction A2/A4 for the judge: same hermetic flag; pinned default
    model. _fake_claude JSON-encodes the payload — judge_score json.loads its
    stdout and reads payload["result"] (a raw string would be swallowed by the
    fail-closed except and green for the wrong reason — plan-review M1)."""
    fake = _fake_claude({"result": "80"})
    from evals.investigation import judge
    score = judge.judge_score("findings", "rubric", runner=fake)
    assert score == 80
    cmd = fake.last_cmd
    assert "--setting-sources" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == "project"
    assert "claude-opus-5" in cmd  # default model pinned (judge.py:26)
    assert "opus" not in cmd  # exact-token check: no bare family alias arg


def test_run_eval_default_models_are_pinned():
    """eval-direction A4: concrete IDs, never a family alias. Anchored to the
    parser object, not source text (drift-guard: no substring coincidence)."""
    from evals.investigation import run_eval as re_mod
    parser = re_mod.build_parser()
    assert parser.get_default("model") == "claude-opus-5"
    assert parser.get_default("judge_model") == "claude-opus-5"
