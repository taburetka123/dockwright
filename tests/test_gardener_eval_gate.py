"""Tests for deploy/scripts/gardener_eval_gate.py (T8 gate)."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "deploy" / "scripts"


@pytest.fixture()
def mod():
    spec = importlib.util.spec_from_file_location(
        "gardener_eval_gate_under_test", SCRIPTS / "gardener_eval_gate.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def postrun_of(mod):
    return sys.modules["gardener_postrun"]


@pytest.fixture()
def investigate_skill_stub(tmp_path, monkeypatch):
    """Points the investigate-skill binding at a real (stub) file — I3's
    missing-binding guard blocks exit 2 by default on hosts (and CI) where
    the harness default `~/.claude/skills/investigate/SKILL.md` does not
    exist; tests exercising a real gate_targets() run need a binding that
    resolves."""
    skill = tmp_path / "stub-investigate" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("stub\n")
    monkeypatch.setenv("DOCKWRIGHT_INVESTIGATE_SKILL", str(skill))
    return skill


def test_investigate_skill_precedence(mod, postrun_of, tmp_path, monkeypatch):
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text("[evals]\ninvestigate_skill = '~/from-toml.md'\n")
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    monkeypatch.delenv("DOCKWRIGHT_INVESTIGATE_SKILL", raising=False)
    assert mod.investigate_skill().endswith("from-toml.md")
    monkeypatch.setenv("DOCKWRIGHT_INVESTIGATE_SKILL", "~/from-env.md")
    assert mod.investigate_skill().endswith("from-env.md")
    monkeypatch.delenv("DOCKWRIGHT_INVESTIGATE_SKILL")
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    assert mod.investigate_skill().endswith("skills/investigate/SKILL.md")


def test_match_suites_default_map(mod, tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    entries = mod.load_map(str(tmp_path / "no-overlay.json"))
    hit = mod.match_suites(
        [str(tmp_path / "x/.claude/rules/investigation-evidence.md")], entries)
    assert "investigation" in hit
    miss = mod.match_suites([str(tmp_path / "x/.claude/rules/style.md")], entries)
    assert miss == {}


def test_match_suites_worker_core_and_skill_glob(mod, tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    entries = mod.load_map(str(tmp_path / "no-overlay.json"))
    assert mod.match_suites(
        [str(tmp_path / "repo/deploy/agents/worker.core.md")], entries)
    # defense-in-depth: the DEPLOYED copy (~/.claude/agents/worker.md) must
    # also gate — a mis-drafted proposal targeting it should still be caught.
    assert mod.match_suites(
        [str(tmp_path / "h/.claude/agents/worker.md")], entries)
    assert mod.match_suites(
        [str(tmp_path / "h/.claude/skills/example-investigate/SKILL.md")], entries)


def test_overlay_extends_and_replaces(mod, tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    ov = tmp_path / "map.json"
    ov.write_text(json.dumps({"extends_default": True, "entries": [
        {"suite": "investigation", "patterns": ["*/custom/thing*"],
         "args": ["--case", "n01-noise-recovered"]}]}))
    entries = mod.load_map(str(ov))
    hit = mod.match_suites([str(tmp_path / "custom/thing.md")], entries)
    assert hit["investigation"]["args"] == ["--case", "n01-noise-recovered"]
    ov.write_text(json.dumps({"extends_default": False, "entries": []}))
    assert mod.match_suites(
        [str(tmp_path / "x/.claude/rules/investigation-evidence.md")],
        mod.load_map(str(ov))) == {}


def test_overlay_precedence_over_overlapping_default(mod, tmp_path, monkeypatch):
    """An overlay entry whose pattern OVERLAPS a DEFAULT_MAP pattern (the
    module docstring's own example shape) must win — the overlay is the
    operator's explicit intent for that surface, not a passive addition.
    Before the fix, overlay entries land AFTER the defaults in the returned
    list, and match_suites is first-match-per-suite, so the default entry's
    (empty) args silently shadow the overlay's args."""
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    ov = tmp_path / "map.json"
    ov.write_text(json.dumps({"extends_default": True, "entries": [
        {"suite": "investigation", "patterns": ["*/rules/*evidence*"],
         "args": ["--case", "x"]}]}))
    entries = mod.load_map(str(ov))
    # matches BOTH the overlay's "*/rules/*evidence*" AND the default's
    # exact "*/rules/investigation-evidence.md" entry.
    target = str(tmp_path / "h/.claude/rules/investigation-evidence.md")
    hit = mod.match_suites([target], entries)
    assert hit["investigation"]["args"] == ["--case", "x"]


def _results(cases):
    return {"cases": cases, "totals": {"cost_usd": 1.23}}


def _case(cid, passed, samples):
    return {"case_id": cid, "passed": passed, "samples": samples}


GATE_FAIL = {"error": None, "gate_failures": ["missing keyword"], "judge": None}
# SUT-behavioral errors (I2): a hanging or output-contract-breaking skill
# edit is the MOST LIKELY cause of these, not harness infra.
ERRORED = {"error": "timeout after 1800s", "gate_failures": None, "judge": None}
UNPARSEABLE_ERRORED = {
    "error": "unparseable claude -p output", "gate_failures": None, "judge": None}
# genuine harness-infra error (runner.py's own subprocess-exit string).
INFRA_ERRORED = {
    "error": "claude -p exited 1: some stderr", "gate_failures": None, "judge": None}
PASSED = {"error": None, "gate_failures": [], "judge": 85}


def test_classify_passed(mod):
    verdict, _s, code = mod.classify(0, _results([_case("a", True, [PASSED])]))
    assert (verdict, code) == ("passed", 0)


def test_classify_behavioral_fail(mod):
    verdict, _s, code = mod.classify(
        1, _results([_case("a", False, [GATE_FAIL]), _case("b", True, [PASSED])]))
    assert (verdict, code) == ("failed", 1)


def test_classify_timeout_only_is_behavioral_not_infra(mod):
    """I2: a timeout is a SUT-behavioral symptom (the skill under test
    hung), not harness infra — before the fix this misclassified as
    infra-suspect/2, letting a hanging skill edit slide as "infra noise"."""
    verdict, _s, code = mod.classify(1, _results([_case("a", False, [ERRORED])]))
    assert (verdict, code) == ("failed", 1)


def test_classify_unparseable_only_is_behavioral_not_infra(mod):
    """I2: unparseable claude -p output is the other SUT-behavioral
    symptom (a broken output contract) — must classify as failed/1."""
    verdict, _s, code = mod.classify(
        1, _results([_case("a", False, [UNPARSEABLE_ERRORED])]))
    assert (verdict, code) == ("failed", 1)


def test_classify_genuine_infra_exit_is_infra_suspect(mod):
    """I2: only the runner's own `claude -p exited N: ...` string is
    genuine harness infra."""
    verdict, _s, code = mod.classify(1, _results([_case("a", False, [INFRA_ERRORED])]))
    assert (verdict, code) == ("infra-suspect", 2)


def test_classify_mixed_infra_and_behavioral_error_is_failed(mod):
    """A case with one genuine-infra sample and one behavioral-error sample
    must NOT be waved off as infra — any non-infra failing sample makes the
    verdict behavioral."""
    verdict, _s, code = mod.classify(
        1, _results([_case("a", False, [INFRA_ERRORED, UNPARSEABLE_ERRORED])]))
    assert (verdict, code) == ("failed", 1)


def test_classify_mixed_error_and_gate_fail_is_behavioral(mod):
    verdict, _s, code = mod.classify(
        1, _results([_case("a", False, [ERRORED, GATE_FAIL])]))
    assert (verdict, code) == ("failed", 1)


def test_classify_missing_results_is_error(mod):
    verdict, _s, code = mod.classify(1, None)
    assert (verdict, code) == ("error", 2)


def test_main_skipped_no_mapped_targets(mod, postrun_of, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    postrun_of.LEDGER_PATH = tmp_path / "ledger.jsonl"
    rc = mod.main(["--targets", str(tmp_path / "x/.claude/rules/style.md"),
                   "--map", str(tmp_path / "no-overlay.json")])
    assert rc == 0
    assert "skipped" in capsys.readouterr().out
    assert not postrun_of.LEDGER_PATH.exists()   # skipped writes NO event


def test_main_dry_run_prints_commands(
        mod, tmp_path, monkeypatch, capsys, investigate_skill_stub):
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(f'[paths]\ndockwright_repo = "{tmp_path}/repo"\n')
    (tmp_path / "repo").mkdir()
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    rc = mod.main(["--targets", str(tmp_path / "h/.claude/rules/investigation-evidence.md"),
                   "--map", str(tmp_path / "no-overlay.json"), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "evals.investigation.run_eval" in out and "would run" in out


def test_main_unset_repo_with_mapped_target_is_exit_2(mod, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    rc = mod.main(["--targets", str(tmp_path / "h/.claude/rules/investigation-evidence.md"),
                   "--map", str(tmp_path / "no-overlay.json")])
    assert rc == 2


def test_main_run_writes_ledger_event_without_path_key(
        mod, postrun_of, tmp_path, monkeypatch, investigate_skill_stub):
    cfg = tmp_path / "dockwright.toml"
    repo = tmp_path / "repo"
    (repo / "evals" / "investigation" / "results").mkdir(parents=True)
    cfg.write_text(f'[paths]\ndockwright_repo = "{repo}"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    postrun_of.LEDGER_PATH = tmp_path / "ledger.jsonl"

    def fake_run(cmd, **kw):
        (repo / "evals" / "investigation" / "results" / "latest.json").write_text(
            json.dumps(_results([_case("a", True, [PASSED])])))

        class P:
            returncode = 0
        return P()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    rc = mod.main(["--targets",
                   str(tmp_path / "h/.claude/rules/investigation-evidence.md"),
                   "--map", str(tmp_path / "no-overlay.json")])
    assert rc == 0
    evs = [json.loads(l) for l in postrun_of.LEDGER_PATH.read_text().splitlines()]
    gate = [e for e in evs if e["type"] == "eval_gate"][-1]
    assert gate["verdict"] == "passed"
    assert "path" not in gate


def test_main_proposal_cli_uses_frontmatter_targets_and_id(
        mod, postrun_of, tmp_path, monkeypatch, investigate_skill_stub):
    cfg = tmp_path / "dockwright.toml"
    repo = tmp_path / "repo"
    (repo / "evals" / "investigation" / "results").mkdir(parents=True)
    cfg.write_text(f'[paths]\ndockwright_repo = "{repo}"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    postrun_of.LEDGER_PATH = tmp_path / "ledger.jsonl"

    target = tmp_path / "h" / ".claude" / "rules" / "investigation-evidence.md"
    prop = tmp_path / "prop.md"
    prop.write_text(
        "---\n"
        "id: r1-1\n"
        f"targets: [{target}]\n"
        "lane: digest\n"
        "---\n\n## Evidence\nE\n"
    )

    def fake_run(cmd, **kw):
        (repo / "evals" / "investigation" / "results" / "latest.json").write_text(
            json.dumps(_results([_case("a", True, [PASSED])])))

        class P:
            returncode = 0
        return P()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    rc = mod.main(["--proposal", str(prop), "--map", str(tmp_path / "no-overlay.json")])
    assert rc == 0
    evs = [json.loads(l) for l in postrun_of.LEDGER_PATH.read_text().splitlines()]
    gate = [e for e in evs if e["type"] == "eval_gate"][-1]
    assert gate["verdict"] == "passed"
    assert gate["proposal_id"] == "r1-1"
    assert gate["lane"] == "digest"
    assert "path" not in gate


def test_main_proposal_gates_on_diff_paths_not_just_targets(
        mod, tmp_path, monkeypatch, capsys, investigate_skill_stub):
    """I1: the actuator applies whatever the diff names, not just what
    `targets:` declares. A proposal declaring an unrelated rule but whose
    diff patches an investigate skill by ABSOLUTE path must still gate on
    the investigation suite — before the fix, gating only read `targets:`
    and this exact shape silently "skipped (no mapped surfaces)"."""
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(f'[paths]\ndockwright_repo = "{tmp_path}/repo"\n')
    (tmp_path / "repo").mkdir()
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))

    declared = tmp_path / "h" / ".claude" / "rules" / "foo.md"
    skill = tmp_path / "h" / ".claude" / "skills" / "example-investigate" / "SKILL.md"
    prop = tmp_path / "prop.md"
    prop.write_text(
        "---\n"
        "id: r1-1\n"
        f"targets: [{declared}]\n"
        "lane: digest\n"
        "---\n\n## Diff\n```diff\n"
        f"--- {skill}\n+++ {skill}\n@@ -1 +1 @@\n-a\n+b\n"
        "```\n"
    )
    rc = mod.main(["--proposal", str(prop), "--map", str(tmp_path / "no-overlay.json"),
                   "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "would run investigation" in out
    assert "skipped" not in out


def test_gate_missing_investigate_skill_is_exit_2(mod, tmp_path, monkeypatch, capsys):
    """I3: a resolved investigate-skill binding that doesn't exist on disk
    is a vacuous pass (the suite would run with nothing for the agent to
    read) — must block with exit 2, loudly naming the resolved path."""
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    missing = tmp_path / "nonexistent-skill" / "SKILL.md"
    monkeypatch.setenv("DOCKWRIGHT_INVESTIGATE_SKILL", str(missing))
    rc = mod.main(["--targets", str(tmp_path / "h/.claude/rules/investigation-evidence.md"),
                   "--map", str(tmp_path / "no-overlay.json")])
    assert rc == 2
    err = capsys.readouterr().err
    assert str(missing) in err
