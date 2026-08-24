"""Tests for deploy/scripts/gardener_eval_gate.py (T8 gate)."""
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
    """A scratch OVERLAY map that routes skill-shaped targets to the
    investigation suite — post-rung-3 the ONLY mechanism that still reaches
    it (docs/specs/eval-direction.md § Ladder execution record: DEFAULT_MAP
    claims no instruction surface; review guards the skill surface).

    Tests below that exercise the SUITE-RUN path (dry-run print, ledger event,
    proposal frontmatter, the missing-skill and unset-repo exit-2 guards) need
    a mapped target to get there at all, so they drive those same code paths
    through this overlay instead of a default entry that no longer exists.
    Returns the --map value; pass it in place of the no-overlay sentinel."""
    p = tmp_path / "overlay-map.json"
    p.write_text(json.dumps({"extends_default": True, "entries": [
        {"suite": "investigation", "patterns": ["*/skills/*investigat*"]}]}))
    return str(p)


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
    """The rung-3 DEFAULT_MAP contract (docs/specs/eval-direction.md § Ladder
    execution record, Decisions 8): the DEFAULT map gates the shipping surface
    (deploy/** -> deterministic pytest) and claims NOTHING on the instruction
    corpus — neither the rules pattern (dropped in A3) nor the skill pattern
    (dropped at rung 3: review guards that surface, and the eval provably
    cannot measure it — Injection B ran 6/6 GREEN with delivery
    content-verified 8/8, so a green here was unearned coverage).

    Delete-one-line RED proof: re-add ANY skills-shaped investigation entry
    (e.g. {"suite": "investigation", "patterns": ["*/skills/*investigat*"]})
    to DEFAULT_MAP in a scratch copy -> the two skill_miss assertions FAIL.
    Verified; output pasted in the task report."""
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.delenv("DOCKWRIGHT_INVESTIGATE_SKILL", raising=False)
    entries = mod.load_map(str(tmp_path / "no-overlay.json"))
    deploy_hit = mod.match_suites(
        [str(tmp_path / "repo/deploy/agents/worker.core.md")], entries)
    assert "pytest" in deploy_hit
    # rung 3: a skill-shaped target maps to NOTHING by default. Both shapes —
    # a FILE directly under skills/ and a SKILL.md inside an investigate-named
    # skill dir — since fnmatch `*` spans `/` and either would have hit the
    # removed "*/skills/*investigat*" pattern.
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
    """The investigate-skill binding names the file the runner copies into the
    SUT workdir (A2c), so it belongs to the investigation suite ONLY. Before
    rung 3 load_map appended it to `entries[0]` — positional, and entries[0]
    is now the PYTEST entry, so the same line would bind a skill path to the
    repo's pytest suite. It must key off `suite == "investigation"` instead.

    RED proof: restore `entries[0] = dict(entries[0], patterns=... +
    [investigate_skill()])` in a scratch copy -> the no-overlay half FAILS
    (the pytest entry's patterns grow the skill path). Verified; output pasted
    in the task report."""
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    binding = tmp_path / "bound" / "SKILL.md"
    monkeypatch.setenv("DOCKWRIGHT_INVESTIGATE_SKILL", str(binding))

    # No overlay: no entry claims the investigation suite, so the binding lands
    # NOWHERE — and the pytest entry keeps exactly its declared patterns.
    entries = mod.load_map(str(tmp_path / "no-overlay.json"))
    assert [e["suite"] for e in entries] == ["pytest"]
    assert entries[0]["patterns"] == ["*/deploy/*"]
    assert not any(str(binding) in (e.get("patterns") or []) for e in entries)

    # Overlay maps the investigation suite: the binding lands on THAT entry,
    # and only that one.
    ov = tmp_path / "map.json"
    ov.write_text(json.dumps({"extends_default": True, "entries": [
        {"suite": "investigation", "patterns": ["*/skills/*investigat*"]}]}))
    entries = mod.load_map(str(ov))
    by_suite = {e["suite"]: e for e in entries}
    assert str(binding) in by_suite["investigation"]["patterns"]
    assert by_suite["pytest"]["patterns"] == ["*/deploy/*"]
    # and the binding actually gates: the bound path itself now maps.
    assert "investigation" in mod.match_suites([str(binding)], entries)


def test_rule_file_edit_is_unmapped_not_green(
        mod, postrun_of, tmp_path, monkeypatch, capsys):
    """eval-direction A3: the ambient-coverage mechanism died with the hermetic
    SUT context; a rule-only edit must surface as unmapped (gate exit 4 — a
    visible non-pass), never ride a coverage claim with no mechanism. Also
    binds this end-to-end through main(), not just match_suites — the gate's
    actual exit code is what the review flow acts on."""
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
    """I4: agent files no longer map to the INVESTIGATION suite. The runner
    provably never reads agent files (hardcoded skeleton + `claude -p` loads
    no agent mode), so the old `*/agents/worker*.md`->investigation mapping
    advertised coverage the harness cannot deliver — a vacuous mapped file.
    The DEPLOYED copy (~/.claude/agents/worker.md, no deploy/ dir) maps to
    NOTHING; a SOURCE agent file (deploy/agents/worker.core.md) is instead
    gated by the DETERMINISTIC pytest suite — correct, it is a shipping file.

    Post-rung-3 an operator OVERLAY is the only thing that can route anything
    to the investigation suite, so the I4 property is asserted here against a
    map that DOES claim that suite — otherwise "investigation not in ..."
    would pass vacuously, for the unrelated reason that nothing anywhere maps
    to it (drift-guard-tests.md: a check that cannot fail is not a check).

    RED-proof (delete-one-line guard): add '*/agents/worker.core.md' /
    '*/agents/worker.md' to the overlay's investigation entry in a scratch
    copy -> the `"investigation" not in ...` assertions FAIL. Verified; output
    pasted in the task report."""
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
    assert "pytest" in source            # gated by the repo suite instead
    # the skill surface reaches the suite ONLY through that overlay (the
    # binding selects the file the runner copies into the SUT workdir — A2c);
    # test_match_suites_default_map owns the default-map half.
    skill = mod.match_suites(
        [str(tmp_path / "h/.claude/skills/example-investigate/SKILL.md")], entries)
    assert "investigation" in skill


def test_worker_core_md_no_longer_advertised(mod):
    """I4: no DEFAULT_MAP pattern may advertise coverage for an agent file
    the investigation runner never reads.

    RED-proof (delete-one-line guard): re-add '*/agents/worker.core.md' to
    DEFAULT_MAP in a scratch copy -> this test FAILS. Verified; output pasted
    in the task report."""
    for entry in mod.DEFAULT_MAP:
        for pat in entry["patterns"]:
            assert "worker.core" not in pat and "agents/worker.md" not in pat


def test_deploy_target_maps_to_pytest_suite(mod, tmp_path, monkeypatch):
    """C1 delete-one-line guard: a shipping-surface change under deploy/ MUST
    route to the repo's own deterministic pytest suite — the mapping that
    ALONE would have caught the batch that broke main (all the token leaks +
    the byte ceiling live under deploy/, and their red tests already ship).

    RED-proof: remove the {"suite":"pytest","patterns":["*/deploy/*"]}
    DEFAULT_MAP entry in a scratch copy -> this test FAILS. Verified; output
    pasted in the task report."""
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
    """An overlay entry whose pattern OVERLAPS a DEFAULT_MAP pattern (the
    module docstring's own example shape) must win — the overlay is the
    operator's explicit intent for that surface, not a passive addition.
    Before the fix, overlay entries land AFTER the defaults in the returned
    list, and match_suites is first-match-per-suite, so the default entry's
    (empty) args silently shadow the overlay's args.

    RETARGETED at rung 3 onto the pytest/`*/deploy/*` entry: this test used a
    skill-shaped overlay overlapping the default investigation entry, which
    no longer exists — it kept PASSING, but for the wrong reason (no overlap
    left to resolve), i.e. exactly the decorative guard drift-guard-tests.md
    forbids. `*/deploy/*` is the only surviving default pattern, so it is the
    only surface on which precedence is still a real question.

    RED-proof: swap load_map's merge back to `entries + extra` (defaults
    first) in a scratch copy -> the default entry's empty args win and this
    FAILS. Verified; output pasted in the task report."""
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    ov = tmp_path / "map.json"
    ov.write_text(json.dumps({"extends_default": True, "entries": [
        {"suite": "pytest", "patterns": ["*/deploy/agents/*"],
         "args": ["--case", "x"]}]}))
    entries = mod.load_map(str(ov))
    # matches BOTH the overlay's "*/deploy/agents/*" AND the default's
    # "*/deploy/*" entry — the overlay's args must be the ones that survive.
    target = str(tmp_path / "repo/deploy/agents/worker.core.md")
    assert any(fnmatch.fnmatch(target, p)
               for e in mod.DEFAULT_MAP for p in e["patterns"])  # overlap is real
    hit = mod.match_suites([target], entries)
    assert hit["pytest"]["args"] == ["--case", "x"]


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
    """I4: a zero-mapped run is NOT a silent pass. Old contract: rc 0,
    "skipped", NO ledger event (that invisibility IS the I4 shape). New
    contract: rc 4, "NOT a pass", and a `skipped-unmapped` ledger event so
    the skip is auditable."""
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
    # still auditable: the explicit skip writes the same skipped-unmapped event
    evs = [json.loads(l) for l in postrun_of.LEDGER_PATH.read_text().splitlines()]
    assert [e for e in evs
            if e["type"] == "eval_gate"][-1]["verdict"] == "skipped-unmapped"


def test_partial_coverage_named_in_verdict(
        mod, postrun_of, tmp_path, monkeypatch, capsys, investigate_skill_stub,
        investigation_map):
    """Partial mapping: run the mapped suite, but a green suite over a
    partially-mapped target set is NOT a full pass at the exit-code layer
    (Tier-2 F1) — the consumers (sitting skill, corpus-watch-run.sh) act on
    the exit code, not the printed table. One mapped (investigate skill, via
    the overlay that now owns that mapping) + one unmapped rule -> exit 5,
    verdict `passed-partial`, and the verdict line still NAMES coverage:
    "1 of 2 touched files routed to a mapped suite; unmapped: <file>"."""
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
    """Tier-2 F1, the reviewer's exact bypass shape: an inverted-skill-shaped
    UNMAPPED path bundled with ONE mapped deploy/ path used to ride through
    on the mapped file's green suite as exit 0 = PASS. It must exit 5
    (`passed-partial`) and the ledger event must carry the coverage counts
    (F2) so "1 of 2" is distinguishable from "2 of 2" after the fact.

    RED-proof: against the pre-fix code this ran rc 0 with verdict `passed`
    and a coverage-blind ledger event. Verified; output pasted in the task
    report."""
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
    """--allow-unmapped downgrades 5 -> 0 exactly as it does 4 -> 0: an
    explicit, auditable skip. The ledger event still records the truth
    (`passed-partial` + the coverage counts), never a clean `passed`."""
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
    """Failure precedence: exit 5 must NEVER replace a failure code — a mixed
    target set whose mapped suite goes RED stays exit 1 (behavioral), with
    the ledger verdict `failed`, not `passed-partial`."""
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
    """Must reach and fail on the repo-guard branch specifically — not the
    earlier missing-investigate-skill guard (I3), which would ALSO exit 2 and
    make this test vacuously green on any host lacking the default skill.
    investigate_skill_stub makes the skill binding resolve, so the only path
    to exit 2 left is the repo-unset guard; assert its exact stderr string so
    the test can only pass via that branch.

    RED-proof: stub the repo-guard branch to `if False:` in a scratch copy ->
    this test FAILS (raises, since run_suite's subprocess.run(cwd="") errors
    out instead of a controlled exit 2). Verified; output pasted in the task
    report."""
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
    # F2: the durable record carries coverage — "1 of 1" must be
    # distinguishable from a partial run in the ledger alone.
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
    rc = mod.main(["--proposal", str(prop), "--map", investigation_map,
                   "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "would run investigation" in out
    assert "skipped" not in out


def test_gate_missing_investigate_skill_is_exit_2(
        mod, tmp_path, monkeypatch, capsys, investigation_map):
    """I3: a resolved investigate-skill binding that doesn't exist on disk
    is a vacuous pass (the suite would run with nothing for the agent to
    read) — must block with exit 2, loudly naming the resolved path.

    The guard fires on any map that routes a target to the investigation
    suite, which post-rung-3 means an operator overlay — so the test drives it
    through one. It must NOT be "repaired" by leaving the target unmapped and
    asserting rc == 4: that would exit at the zero-mapped branch and SILENTLY
    RETIRE the vacuous-pass guard (the exit code would be right for the wrong
    reason). The stderr assertion below pins the branch."""
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


# ---- pytest suite runner (deploy/** shipping surfaces, deterministic) ----

def _make_test_bearing(root, passing=True):
    """Tiny real pytest suite + a .venv/bin/python WRAPPER that execs the test
    interpreter, so the gate's `<repo>/.venv/bin/python -m pytest` runs the
    real pytest installed in the dev venv (proven shape,
    tests/test_gardener_reanchor.py::_make_test_bearing). Self-contained —
    the guard test reads no repo files.

    Wrapper, not a bare symlink: a `.venv/bin/python -> <interp>` symlink loses
    the venv's site-packages on Linux (CI run 29931892118 — `No module named
    pytest`, empty stdout); the exec wrapper resolves site-packages on every
    platform."""
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
    """Runner half of the C1 fix: a deploy-shaped target routes to the pytest
    suite; a RED repo suite -> gate exit 1 (behavioral). The incident's exact
    shape, now caught deterministically — the repo has NO CI."""
    repo = _pytest_repo_env(tmp_path, monkeypatch, postrun_of, passing=False)
    rc = mod.main(["--targets", str(repo / "deploy/agents/worker.core.md"),
                   "--map", str(tmp_path / "no-overlay.json")])
    assert rc == 1


def test_pytest_suite_passing_repo_exits_0_with_coverage(
        mod, postrun_of, tmp_path, monkeypatch, capsys):
    """All-mapped green stays a clean exit 0 — exit 5 must NEVER fire when
    every target mapped (F1's no-false-alarm half). Verdict wording is the
    honest F5 form: "routed to a mapped suite", not "behaviorally covered"
    (routing is not proof the touched file is exercised)."""
    repo = _pytest_repo_env(tmp_path, monkeypatch, postrun_of, passing=True)
    rc = mod.main(["--targets", str(repo / "deploy/agents/worker.core.md"),
                   "--map", str(tmp_path / "no-overlay.json")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "passed (exit 0)" in out
    assert "passed-partial" not in out
    assert "1 of 1 touched files routed to a mapped suite" in out


def _make_launch_failing_python(root):
    """A repo with a test suite but a .venv/bin/python that fails to LAUNCH
    pytest — prints to STDERR and exits 1 with EMPTY stdout. Reproduces the CI
    runner shape exactly (`No module named pytest`), hermetically, on any
    platform."""
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
    """A python-level launch failure (pytest not importable) exits rc=1 with
    EMPTY stdout — could-not-run, not a failing test. Must classify infra-
    suspect (exit 2, the documented could-not-run meaning) with the STDERR
    diagnostic visible, NEVER behavioral exit 1 with an empty tail (CI run
    29931892118).

    RED-proof: revert run_pytest_suite's empty-stdout branch (classify any
    rc!=0 as behavioral exit 1) in a scratch copy -> this test sees rc==1 and
    an empty tail -> RED. Verified 2026-07-22; pasted in the report."""
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
    """Missing harness must fail LOUD as infra-suspect (exit 2), never a
    silent pass (I4 lesson) — the review flow treats exit 2 as blocking."""
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
