import fnmatch
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
def investigation_map(tmp_path):
    p = tmp_path / "overlay-map.json"
    p.write_text(json.dumps({"extends_default": True, "entries": [
        {"suite": "investigation", "patterns": ["*/skills/*investigat*"]}]}))
    return str(p)


@pytest.fixture()
def investigate_skill_stub(tmp_path, monkeypatch):
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
    monkeypatch.delenv("DOCKWRIGHT_INVESTIGATE_SKILL", raising=False)
    entries = mod.load_map(str(tmp_path / "no-overlay.json"))
    deploy_hit = mod.match_suites(
        [str(tmp_path / "repo/deploy/agents/worker.core.md")], entries)
    assert "pytest" in deploy_hit
    skill_miss = mod.match_suites(
        [str(tmp_path / "x/.claude/skills/example-investigate.md")], entries)
    assert skill_miss == {}
    skill_dir_miss = mod.match_suites(
        [str(tmp_path / "x/.claude/skills/example-investigate/SKILL.md")], entries)
    assert skill_dir_miss == {}
    rules_miss = mod.match_suites(
        [str(tmp_path / "x/.claude/rules/investigation-evidence.md")], entries)
    assert rules_miss == {}
    miss = mod.match_suites([str(tmp_path / "x/.claude/rules/style.md")], entries)
    assert miss == {}


def test_load_map_appends_binding_only_to_investigation_entries(
        mod, tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    binding = tmp_path / "bound" / "SKILL.md"
    monkeypatch.setenv("DOCKWRIGHT_INVESTIGATE_SKILL", str(binding))

    entries = mod.load_map(str(tmp_path / "no-overlay.json"))
    assert [e["suite"] for e in entries] == ["pytest"]
    assert entries[0]["patterns"] == ["*/deploy/*"]
    assert not any(str(binding) in (e.get("patterns") or []) for e in entries)

    ov = tmp_path / "map.json"
    ov.write_text(json.dumps({"extends_default": True, "entries": [
        {"suite": "investigation", "patterns": ["*/skills/*investigat*"]}]}))
    entries = mod.load_map(str(ov))
    by_suite = {e["suite"]: e for e in entries}
    assert str(binding) in by_suite["investigation"]["patterns"]
    assert by_suite["pytest"]["patterns"] == ["*/deploy/*"]
    assert "investigation" in mod.match_suites([str(binding)], entries)


def test_rule_file_edit_is_unmapped_not_green(
        mod, postrun_of, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DOCKWRIGHT_INVESTIGATE_SKILL", raising=False)
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setattr(postrun_of, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    entries = mod.load_map(str(tmp_path / "no-overlay.json"))
    target = str(tmp_path / "x/.claude/rules/investigation-evidence.md")
    assert mod.match_suites([target], entries) == {}
    rc = mod.main(["--targets", target, "--map", str(tmp_path / "no-overlay.json")])
    assert rc == 4


def test_match_suites_worker_core_and_skill_glob(
        mod, tmp_path, monkeypatch, investigation_map):
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    entries = mod.load_map(investigation_map)
    assert any(e.get("suite") == "investigation" for e in entries)
    deployed = mod.match_suites(
        [str(tmp_path / "h/.claude/agents/worker.md")], entries)
    assert "investigation" not in deployed
    assert deployed == {}
    source = mod.match_suites(
        [str(tmp_path / "repo/deploy/agents/worker.core.md")], entries)
    assert "investigation" not in source
    assert "pytest" in source
    skill = mod.match_suites(
        [str(tmp_path / "h/.claude/skills/example-investigate/SKILL.md")], entries)
    assert "investigation" in skill


def test_worker_core_md_no_longer_advertised(mod):
    for entry in mod.DEFAULT_MAP:
        for pat in entry["patterns"]:
            assert "worker.core" not in pat and "agents/worker.md" not in pat


def test_deploy_target_maps_to_pytest_suite(mod, tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    entries = mod.load_map(str(tmp_path / "no-overlay.json"))
    hit = mod.match_suites(
        [str(tmp_path / "repo/deploy/agents/worker.core.md")], entries)
    assert "pytest" in hit


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
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    ov = tmp_path / "map.json"
    ov.write_text(json.dumps({"extends_default": True, "entries": [
        {"suite": "pytest", "patterns": ["*/deploy/agents/*"],
         "args": ["--case", "x"]}]}))
    entries = mod.load_map(str(ov))
    target = str(tmp_path / "repo/deploy/agents/worker.core.md")
    assert any(fnmatch.fnmatch(target, p)
               for e in mod.DEFAULT_MAP for p in e["patterns"])
    hit = mod.match_suites([target], entries)
    assert hit["pytest"]["args"] == ["--case", "x"]


def _results(cases):
    return {"cases": cases, "totals": {"cost_usd": 1.23}}


def _case(cid, passed, samples):
    return {"case_id": cid, "passed": passed, "samples": samples}


GATE_FAIL = {"error": None, "gate_failures": ["missing keyword"], "judge": None}
ERRORED = {"error": "timeout after 1800s", "gate_failures": None, "judge": None}
UNPARSEABLE_ERRORED = {
    "error": "unparseable claude -p output", "gate_failures": None, "judge": None}
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
    verdict, _s, code = mod.classify(1, _results([_case("a", False, [ERRORED])]))
    assert (verdict, code) == ("failed", 1)


def test_classify_unparseable_only_is_behavioral_not_infra(mod):
    verdict, _s, code = mod.classify(
        1, _results([_case("a", False, [UNPARSEABLE_ERRORED])]))
    assert (verdict, code) == ("failed", 1)


def test_classify_genuine_infra_exit_is_infra_suspect(mod):
    verdict, _s, code = mod.classify(1, _results([_case("a", False, [INFRA_ERRORED])]))
    assert (verdict, code) == ("infra-suspect", 2)


def test_classify_mixed_infra_and_behavioral_error_is_failed(mod):
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
    monkeypatch.setattr(postrun_of, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    rc = mod.main(["--targets", str(tmp_path / "x/.claude/rules/style.md"),
                   "--map", str(tmp_path / "no-overlay.json")])
    assert rc == 4
    out = capsys.readouterr().out
    assert "NOT a pass" in out
    assert "0 of 1" in out
    evs = [json.loads(l) for l in postrun_of.LEDGER_PATH.read_text().splitlines()]
    gate = [e for e in evs if e["type"] == "eval_gate"][-1]
    assert gate["verdict"] == "skipped-unmapped"
    assert "path" not in gate


def test_zero_mapped_exits_4_and_ledgers(mod, postrun_of, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setattr(postrun_of, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    rc = mod.main(["--targets", ",".join([
                       str(tmp_path / "x/agents/manager.core.md"),
                       str(tmp_path / "x/CLAUDE.md")]),
                   "--map", str(tmp_path / "no-overlay.json")])
    assert rc == 4
    out = capsys.readouterr().out
    assert "0 of 2" in out and "NOT a pass" in out
    evs = [json.loads(l) for l in postrun_of.LEDGER_PATH.read_text().splitlines()]
    gate = [e for e in evs if e["type"] == "eval_gate"][-1]
    assert gate["verdict"] == "skipped-unmapped"
    assert gate["targets_total"] == "2"
    assert "path" not in gate


def test_allow_unmapped_exits_0_still_loud(
        mod, postrun_of, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setattr(postrun_of, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    rc = mod.main(["--targets", str(tmp_path / "x/CLAUDE.md"),
                   "--map", str(tmp_path / "no-overlay.json"),
                   "--allow-unmapped"])
    assert rc == 0
    assert "NOT a pass" in capsys.readouterr().out
    evs = [json.loads(l) for l in postrun_of.LEDGER_PATH.read_text().splitlines()]
    assert [e for e in evs
            if e["type"] == "eval_gate"][-1]["verdict"] == "skipped-unmapped"


def test_partial_coverage_named_in_verdict(
        mod, postrun_of, tmp_path, monkeypatch, capsys, investigate_skill_stub,
        investigation_map):
    cfg = tmp_path / "dockwright.toml"
    repo = tmp_path / "repo"
    (repo / "evals" / "investigation" / "results").mkdir(parents=True)
    cfg.write_text(f'[paths]\ndockwright_repo = "{repo}"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    monkeypatch.setattr(postrun_of, "LEDGER_PATH", tmp_path / "ledger.jsonl")

    def fake_run(cmd, **kw):
        (repo / "evals" / "investigation" / "results" / "latest.json").write_text(
            json.dumps(_results([_case("a", True, [PASSED])])))

        class P:
            returncode = 0
        return P()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    mapped = tmp_path / "h/.claude/skills/example-investigate/SKILL.md"
    unmapped = tmp_path / "h/.claude/rules/foo.md"
    rc = mod.main(["--targets", ",".join([str(mapped), str(unmapped)]),
                   "--map", investigation_map])
    assert rc == 5
    out = capsys.readouterr().out
    assert "passed-partial" in out
    assert "1 of 2 touched files routed to a mapped suite" in out
    assert "unmapped:" in out
    assert str(unmapped) in out


def test_partial_coverage_mixed_deploy_and_skill_exits_5(
        mod, postrun_of, tmp_path, monkeypatch, capsys):
    repo = _pytest_repo_env(tmp_path, monkeypatch, postrun_of, passing=True)
    mapped = repo / "deploy" / "scripts" / "harmless.sh"
    unmapped = tmp_path / "x/.claude/skills/investigate/SKILL.md"
    rc = mod.main(["--targets", ",".join([str(mapped), str(unmapped)]),
                   "--map", str(tmp_path / "no-overlay.json")])
    assert rc == 5
    out = capsys.readouterr().out
    assert "passed-partial" in out
    assert "1 of 2 touched files routed to a mapped suite" in out
    assert f"unmapped: {unmapped}" in out
    evs = [json.loads(l) for l in postrun_of.LEDGER_PATH.read_text().splitlines()]
    gate = [e for e in evs if e["type"] == "eval_gate"][-1]
    assert gate["verdict"] == "passed-partial"
    assert gate["targets_total"] == "2"
    assert gate["targets_mapped"] == "1"
    assert "path" not in gate


def test_partial_coverage_allow_unmapped_downgrades_5_to_0(
        mod, postrun_of, tmp_path, monkeypatch, capsys):
    repo = _pytest_repo_env(tmp_path, monkeypatch, postrun_of, passing=True)
    mapped = repo / "deploy" / "scripts" / "harmless.sh"
    unmapped = tmp_path / "x/.claude/skills/investigate/SKILL.md"
    rc = mod.main(["--targets", ",".join([str(mapped), str(unmapped)]),
                   "--map", str(tmp_path / "no-overlay.json"),
                   "--allow-unmapped"])
    assert rc == 0
    assert "passed-partial" in capsys.readouterr().out
    evs = [json.loads(l) for l in postrun_of.LEDGER_PATH.read_text().splitlines()]
    gate = [e for e in evs if e["type"] == "eval_gate"][-1]
    assert gate["verdict"] == "passed-partial"
    assert gate["targets_total"] == "2"
    assert gate["targets_mapped"] == "1"


def test_partial_coverage_suite_failure_wins_exit_1(
        mod, postrun_of, tmp_path, monkeypatch, capsys):
    repo = _pytest_repo_env(tmp_path, monkeypatch, postrun_of, passing=False)
    mapped = repo / "deploy" / "scripts" / "harmless.sh"
    unmapped = tmp_path / "x/.claude/skills/investigate/SKILL.md"
    rc = mod.main(["--targets", ",".join([str(mapped), str(unmapped)]),
                   "--map", str(tmp_path / "no-overlay.json")])
    assert rc == 1
    evs = [json.loads(l) for l in postrun_of.LEDGER_PATH.read_text().splitlines()]
    gate = [e for e in evs if e["type"] == "eval_gate"][-1]
    assert gate["verdict"] == "failed"


def test_main_dry_run_prints_commands(
        mod, tmp_path, monkeypatch, capsys, investigate_skill_stub,
        investigation_map):
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(f'[paths]\ndockwright_repo = "{tmp_path}/repo"\n')
    (tmp_path / "repo").mkdir()
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    rc = mod.main(["--targets",
                   str(tmp_path / "h/.claude/skills/example-investigate/SKILL.md"),
                   "--map", investigation_map, "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "evals.investigation.run_eval" in out and "would run" in out


def test_main_unset_repo_with_mapped_target_is_exit_2(
        mod, tmp_path, monkeypatch, capsys, investigate_skill_stub,
        investigation_map):
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    rc = mod.main(["--targets",
                   str(tmp_path / "h/.claude/skills/example-investigate/SKILL.md"),
                   "--map", investigation_map])
    assert rc == 2
    assert "dockwright_repo unset" in capsys.readouterr().err


def test_main_run_writes_ledger_event_without_path_key(
        mod, postrun_of, tmp_path, monkeypatch, investigate_skill_stub,
        investigation_map):
    cfg = tmp_path / "dockwright.toml"
    repo = tmp_path / "repo"
    (repo / "evals" / "investigation" / "results").mkdir(parents=True)
    cfg.write_text(f'[paths]\ndockwright_repo = "{repo}"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    monkeypatch.setattr(postrun_of, "LEDGER_PATH", tmp_path / "ledger.jsonl")

    def fake_run(cmd, **kw):
        (repo / "evals" / "investigation" / "results" / "latest.json").write_text(
            json.dumps(_results([_case("a", True, [PASSED])])))

        class P:
            returncode = 0
        return P()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    rc = mod.main(["--targets",
                   str(tmp_path / "h/.claude/skills/example-investigate/SKILL.md"),
                   "--map", investigation_map])
    assert rc == 0
    evs = [json.loads(l) for l in postrun_of.LEDGER_PATH.read_text().splitlines()]
    gate = [e for e in evs if e["type"] == "eval_gate"][-1]
    assert gate["verdict"] == "passed"
    assert "path" not in gate
    assert gate["targets_total"] == "1"
    assert gate["targets_mapped"] == "1"


def test_main_proposal_cli_uses_frontmatter_targets_and_id(
        mod, postrun_of, tmp_path, monkeypatch, investigate_skill_stub,
        investigation_map):
    cfg = tmp_path / "dockwright.toml"
    repo = tmp_path / "repo"
    (repo / "evals" / "investigation" / "results").mkdir(parents=True)
    cfg.write_text(f'[paths]\ndockwright_repo = "{repo}"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    monkeypatch.setattr(postrun_of, "LEDGER_PATH", tmp_path / "ledger.jsonl")

    target = tmp_path / "h" / ".claude" / "skills" / "example-investigate" / "SKILL.md"
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
    rc = mod.main(["--proposal", str(prop), "--map", investigation_map])
    assert rc == 0
    evs = [json.loads(l) for l in postrun_of.LEDGER_PATH.read_text().splitlines()]
    gate = [e for e in evs if e["type"] == "eval_gate"][-1]
    assert gate["verdict"] == "passed"
    assert gate["proposal_id"] == "r1-1"
    assert gate["lane"] == "digest"
    assert "path" not in gate


def test_main_proposal_gates_on_diff_paths_not_just_targets(
        mod, tmp_path, monkeypatch, capsys, investigate_skill_stub,
        investigation_map):
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
    rc = mod.main(["--proposal", str(prop), "--map", investigation_map,
                   "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "would run investigation" in out
    assert "skipped" not in out


def test_gate_missing_investigate_skill_is_exit_2(
        mod, tmp_path, monkeypatch, capsys, investigation_map):
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    missing = tmp_path / "nonexistent-skill" / "SKILL.md"
    monkeypatch.setenv("DOCKWRIGHT_INVESTIGATE_SKILL", str(missing))
    rc = mod.main(["--targets",
                   str(tmp_path / "h/.claude/skills/example-investigate/SKILL.md"),
                   "--map", investigation_map])
    assert rc == 2
    err = capsys.readouterr().err
    assert str(missing) in err
    assert "VACUOUS PASS" in err


def _make_test_bearing(root, passing=True):
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n")
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert %s\n" % ("True" if passing else "False"))
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    py = venv_bin / "python"
    py.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    py.chmod(0o755)


def _pytest_repo_env(tmp_path, monkeypatch, postrun_of, passing):
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_test_bearing(repo, passing=passing)
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(f'[paths]\ndockwright_repo = "{repo}"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    monkeypatch.setattr(postrun_of, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    return repo


def test_pytest_suite_failing_repo_exits_1(
        mod, postrun_of, tmp_path, monkeypatch, capsys):
    repo = _pytest_repo_env(tmp_path, monkeypatch, postrun_of, passing=False)
    rc = mod.main(["--targets", str(repo / "deploy/agents/worker.core.md"),
                   "--map", str(tmp_path / "no-overlay.json")])
    assert rc == 1


def test_pytest_suite_passing_repo_exits_0_with_coverage(
        mod, postrun_of, tmp_path, monkeypatch, capsys):
    repo = _pytest_repo_env(tmp_path, monkeypatch, postrun_of, passing=True)
    rc = mod.main(["--targets", str(repo / "deploy/agents/worker.core.md"),
                   "--map", str(tmp_path / "no-overlay.json")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "passed (exit 0)" in out
    assert "passed-partial" not in out
    assert "1 of 1 touched files routed to a mapped suite" in out


def _make_launch_failing_python(root):
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n")
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    py = venv_bin / "python"
    py.write_text('#!/bin/sh\necho "boom: No module named pytest" >&2\nexit 1\n')
    py.chmod(0o755)


def test_pytest_suite_launch_failure_is_infra_exit_2(
        mod, postrun_of, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_launch_failing_python(repo)
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(f'[paths]\ndockwright_repo = "{repo}"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    monkeypatch.setattr(postrun_of, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    rc = mod.main(["--targets", str(repo / "deploy/agents/worker.core.md"),
                   "--map", str(tmp_path / "no-overlay.json")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "boom: No module named pytest" in err
    assert "could not run" in err


def test_pytest_suite_missing_venv_exits_2(
        mod, postrun_of, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(f'[paths]\ndockwright_repo = "{repo}"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    monkeypatch.setattr(postrun_of, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    rc = mod.main(["--targets", str(repo / "deploy/agents/worker.core.md"),
                   "--map", str(tmp_path / "no-overlay.json")])
    assert rc == 2
    assert "no .venv" in capsys.readouterr().err
