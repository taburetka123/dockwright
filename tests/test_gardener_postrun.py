import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
POSTRUN_PATH = REPO_ROOT / "deploy" / "scripts" / "gardener_postrun.py"
SCRIPTS = REPO_ROOT / "deploy" / "scripts"
# The birth gate lazily `import gardener_apply` from the deployed scripts dir;
# put it on sys.path so the import resolves regardless of test order/isolation
# (the deployed runtime gets this for free via __main__'s sys.path[0]).
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load_postrun():
    spec = importlib.util.spec_from_file_location("gardener_postrun_under_test", POSTRUN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def postrun(tmp_path, monkeypatch):
    mod = _load_postrun()
    # config isolation: point at an absent file so config_path() -> None and
    # every config_toml_* read returns its default, regardless of the
    # developer's live ~/.claude/dockwright.toml
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "no-config.toml"))
    gardener_dir = tmp_path / "gardener"
    findings_dir = tmp_path / "selffix-findings"
    monkeypatch.setattr(mod, "GARDENER_DIR", gardener_dir)
    monkeypatch.setattr(mod, "PENDING_DIR", gardener_dir / "proposals" / "pending")
    monkeypatch.setattr(mod, "ACCEPTED_DIR", gardener_dir / "proposals" / "accepted")
    monkeypatch.setattr(mod, "DECLINED_DIR", gardener_dir / "proposals" / "declined")
    monkeypatch.setattr(mod, "REJECTED_DIR", gardener_dir / "proposals" / "rejected")
    monkeypatch.setattr(mod, "CHECKS_DIR", gardener_dir / "checks")
    monkeypatch.setattr(mod, "LEDGER_PATH", gardener_dir / "ledger.jsonl")
    monkeypatch.setattr(mod, "FINDINGS_DIR", findings_dir)
    monkeypatch.setattr(
        mod, "ALLOWED_TARGET_ROOTS",
        [tmp_path / "claude-home", tmp_path / "orchestrator-clone"],
    )
    for d in (mod.PENDING_DIR, mod.CHECKS_DIR, findings_dir,
              tmp_path / "claude-home", tmp_path / "orchestrator-clone"):
        d.mkdir(parents=True, exist_ok=True)
    return mod


PROPOSAL_TEMPLATE = """---
id: {pid}
run_id: r-test
cluster: {cluster}
lane: digest
members: [0eef8c47-2bc4-41d3-84df-c61e3ec2f9d1, a2d9ddea-0320-417e-b556-d2a8a44420f2]
targets: [{target}]
kind: rule-edit
evidence_kind: findings
base_rev: abc1234
always_on_bytes: 9
flow_cost: none
expectation: NUDGED lines always pair with RESUMED within one ladder step
check_window_days: 14
revert: git revert of the applying auto-commit
---

## Evidence
8 findings across 3 weeks.

## Diff
```diff
--- /dev/null
+++ b/rules/foo.md
@@ -0,0 +1 @@
+new line
```

## Rationale
Because.
"""


def _write_proposal(postrun, name="p1.md", target=None, drop_field=None, cluster="claimed-vs-actual"):
    target = target or str(postrun.ALLOWED_TARGET_ROOTS[0] / "rules" / "foo.md")
    text = PROPOSAL_TEMPLATE.format(pid=name.removesuffix(".md"), target=target, cluster=cluster)
    if drop_field:
        text = "\n".join(line for line in text.splitlines()
                         if not line.startswith(f"{drop_field}:")) + "\n"
    path = postrun.PENDING_DIR / name
    path.write_text(text)
    return path


def _ledger_events(postrun):
    if not postrun.LEDGER_PATH.is_file():
        return []
    return [json.loads(line) for line in postrun.LEDGER_PATH.read_text().splitlines() if line.strip()]


class TestFrontmatterParse:
    def test_round_trip(self, postrun):
        path = _write_proposal(postrun)
        meta, body = postrun.parse_frontmatter(path.read_text())
        assert meta["id"] == "p1"
        assert meta["members"] == ["0eef8c47-2bc4-41d3-84df-c61e3ec2f9d1", "a2d9ddea-0320-417e-b556-d2a8a44420f2"]
        assert meta["targets"] == [str(postrun.ALLOWED_TARGET_ROOTS[0] / "rules" / "foo.md")]
        assert meta["always_on_bytes"] == "9"
        assert "## Diff" in body

    def test_no_frontmatter_returns_none(self, postrun):
        meta, _ = postrun.parse_frontmatter("just a body\n")
        assert meta is None

    def test_unterminated_frontmatter_returns_none(self, postrun):
        meta, _ = postrun.parse_frontmatter("---\nid: x\nno terminator\n")
        assert meta is None


class TestValidation:
    def test_valid_proposal_passes(self, postrun):
        path = _write_proposal(postrun)
        meta, body = postrun.parse_frontmatter(path.read_text())
        assert postrun.validate_proposal(meta, body) == []

    def test_target_outside_roots_rejected(self, postrun, tmp_path):
        path = _write_proposal(postrun, target=str(tmp_path / "elsewhere" / "x.md"))
        meta, _ = postrun.parse_frontmatter(path.read_text())
        violations = postrun.validate_proposal(meta)
        assert any("outside allowed roots" in v for v in violations)

    def test_traversal_target_rejected(self, postrun, tmp_path):
        sneaky = str(postrun.ALLOWED_TARGET_ROOTS[0] / ".." / "elsewhere" / "x.md")
        path = _write_proposal(postrun, name="p2.md", target=sneaky)
        meta, _ = postrun.parse_frontmatter(path.read_text())
        violations = postrun.validate_proposal(meta)
        assert any("outside allowed roots" in v for v in violations)

    @pytest.mark.parametrize("field", ["id", "members", "targets", "expectation", "revert"])
    def test_missing_required_field_rejected(self, postrun, field):
        path = _write_proposal(postrun, drop_field=field)
        meta, _ = postrun.parse_frontmatter(path.read_text())
        violations = postrun.validate_proposal(meta)
        assert violations, f"expected violation for missing {field}"


class TestPostrunProcess:
    def test_valid_proposal_logged_and_kept_pending(self, postrun):
        path = _write_proposal(postrun)
        summary = postrun.process_run_artifacts("r-test", known=set())
        assert path.exists()
        events = _ledger_events(postrun)
        assert any(e["event"] == "proposal" and e["proposal_id"] == "p1" for e in events)
        assert summary["proposals"] == 1 and summary["rejected"] == 0

    def test_out_of_scope_proposal_quarantined(self, postrun, tmp_path):
        path = _write_proposal(postrun, name="evil.md", target=str(tmp_path / "outside.md"))
        summary = postrun.process_run_artifacts("r-test", known=set())
        assert not path.exists()
        assert (postrun.REJECTED_DIR / "evil.md").exists()
        events = _ledger_events(postrun)
        assert any(e["event"] == "proposal_rejected" for e in events)
        assert summary["rejected"] == 1

    def test_unparseable_proposal_quarantined(self, postrun):
        (postrun.PENDING_DIR / "garbage.md").write_text("no frontmatter at all\n")
        summary = postrun.process_run_artifacts("r-test", known=set())
        assert (postrun.REJECTED_DIR / "garbage.md").exists()
        assert summary["rejected"] == 1

    def test_known_files_skipped(self, postrun):
        path = _write_proposal(postrun)
        postrun.process_run_artifacts("r-test", known=set())
        summary2 = postrun.process_run_artifacts("r-test-2", known={path.name})
        assert summary2["proposals"] == 0

    def test_check_artifact_armed(self, postrun):
        (postrun.CHECKS_DIR / "c1.md").write_text(
            "---\nid: c1\nrun_id: r-test\ncluster: stale-signal\n"
            "expectation: false-nudge rate stays 0\ncheck_window_days: 14\n"
            "fixed_by: PR #52\n---\n\nbody\n")
        summary = postrun.process_run_artifacts("r-test", known=set())
        events = _ledger_events(postrun)
        assert any(e["event"] == "check_armed" and e["check_id"] == "c1" for e in events)
        assert summary["checks"] == 1

    def test_invalid_check_quarantined(self, postrun):
        (postrun.CHECKS_DIR / "bad.md").write_text("---\nid: bad\n---\nno expectation\n")
        summary = postrun.process_run_artifacts("r-test", known=set())
        assert (postrun.REJECTED_DIR / "bad.md").exists()
        assert summary["rejected"] == 1


class TestDecide:
    def _accepted_setup(self, postrun):
        path = _write_proposal(postrun)
        for sid in ("0eef8c47-2bc4-41d3-84df-c61e3ec2f9d1", "a2d9ddea-0320-417e-b556-d2a8a44420f2"):
            (postrun.FINDINGS_DIR / f"{sid}.md").write_text("finding\n")
        return path

    def test_accept_moves_logs_and_marks_members(self, postrun):
        path = self._accepted_setup(postrun)
        rc = postrun.decide(str(path), "accept", reason="good catch")
        assert rc == 0
        assert (postrun.ACCEPTED_DIR / "p1.md").exists()
        assert not path.exists()
        for sid in ("0eef8c47-2bc4-41d3-84df-c61e3ec2f9d1", "a2d9ddea-0320-417e-b556-d2a8a44420f2"):
            assert (postrun.FINDINGS_DIR / f"{sid}.reviewed").exists()
        events = _ledger_events(postrun)
        decision = next(e for e in events if e["event"] == "decision")
        assert decision["kind"] == "accept"
        assert decision["proposal_id"] == "p1"
        assert set(decision["members"].split(",")) == {"0eef8c47-2bc4-41d3-84df-c61e3ec2f9d1", "a2d9ddea-0320-417e-b556-d2a8a44420f2"}

    def test_decline_requires_reason(self, postrun):
        path = self._accepted_setup(postrun)
        rc = postrun.decide(str(path), "decline", reason="")
        assert rc != 0
        assert path.exists()

    def test_decline_moves_and_marks(self, postrun):
        path = self._accepted_setup(postrun)
        rc = postrun.decide(str(path), "decline", reason="not worth always-on cost")
        assert rc == 0
        assert (postrun.DECLINED_DIR / "p1.md").exists()
        for sid in ("0eef8c47-2bc4-41d3-84df-c61e3ec2f9d1", "a2d9ddea-0320-417e-b556-d2a8a44420f2"):
            assert (postrun.FINDINGS_DIR / f"{sid}.reviewed").exists()
        decision = next(e for e in _ledger_events(postrun) if e["event"] == "decision")
        assert decision["kind"] == "decline"
        assert decision["reason"] == "not worth always-on cost"

    def _corpus_retire_setup(self, postrun):
        path = self._accepted_setup(postrun)
        text = path.read_text().replace("kind: rule-edit", "kind: corpus-retire")
        path.write_text(text)
        return path

    def test_decline_corpus_retire_keeps_members_unmarked(self, postrun):
        path = self._corpus_retire_setup(postrun)
        rc = postrun.decide(str(path), "decline", "keep them")
        assert rc == 0
        assert (postrun.DECLINED_DIR / "p1.md").exists()
        assert not path.exists()
        for sid in ("0eef8c47-2bc4-41d3-84df-c61e3ec2f9d1", "a2d9ddea-0320-417e-b556-d2a8a44420f2"):
            assert not (postrun.FINDINGS_DIR / f"{sid}.reviewed").exists()
        decision = next(e for e in _ledger_events(postrun) if e["event"] == "decision")
        assert decision["kind"] == "decline"
        assert decision["proposal_id"] == "p1"

    def test_accept_corpus_retire_marks_members(self, postrun):
        path = self._corpus_retire_setup(postrun)
        rc = postrun.decide(str(path), "accept", "adopt retirement")
        assert rc == 0
        assert (postrun.ACCEPTED_DIR / "p1.md").exists()
        for sid in ("0eef8c47-2bc4-41d3-84df-c61e3ec2f9d1", "a2d9ddea-0320-417e-b556-d2a8a44420f2"):
            assert (postrun.FINDINGS_DIR / f"{sid}.reviewed").exists()

    def test_decide_on_missing_member_finding_still_succeeds(self, postrun):
        path = _write_proposal(postrun)  # member findings never created
        rc = postrun.decide(str(path), "accept", reason="ok")
        assert rc == 0
        assert (postrun.ACCEPTED_DIR / "p1.md").exists()

    def test_decide_rejects_unknown_kind(self, postrun):
        path = self._accepted_setup(postrun)
        rc = postrun.decide(str(path), "maybe", reason="x")
        assert rc != 0


class TestDecideUuidRegression:
    """CRITICAL regression (verifier on #59): production findings are FULL
    UUIDs while the skill emitted sid-prefix-8 members — exact-name lookup
    silently marked 0 findings."""

    UUIDS = ("0eef8c47-2bc4-41d3-84df-c61e3ec2f9d1",
             "a2d9ddea-0320-417e-b556-d2a8a44420f2")

    def _setup(self, postrun, members):
        for uuid in self.UUIDS:
            (postrun.FINDINGS_DIR / f"{uuid}.md").write_text("finding\n")
        target = str(postrun.ALLOWED_TARGET_ROOTS[0] / "rules" / "foo.md")
        text = PROPOSAL_TEMPLATE.format(pid="p1", target=target, cluster="c")
        text = text.replace("members: [0eef8c47-2bc4-41d3-84df-c61e3ec2f9d1, a2d9ddea-0320-417e-b556-d2a8a44420f2]", f"members: [{members}]")
        path = postrun.PENDING_DIR / "p1.md"
        path.write_text(text)
        return path

    def test_prefix_members_mark_uuid_named_findings(self, postrun):
        path = self._setup(postrun, "0eef8c47, a2d9ddea")
        rc = postrun.decide(str(path), "accept", reason="ok")
        assert rc == 0
        for uuid in self.UUIDS:
            assert (postrun.FINDINGS_DIR / f"{uuid}.reviewed").exists(), uuid
        decision = next(e for e in _ledger_events(postrun) if e["event"] == "decision")
        assert decision["members_marked"] == "0eef8c47,a2d9ddea"
        assert decision["members_ambiguous"] == ""

    def test_full_basename_members_still_exact_match(self, postrun):
        path = self._setup(postrun, ", ".join(self.UUIDS))
        rc = postrun.decide(str(path), "accept", reason="ok")
        assert rc == 0
        for uuid in self.UUIDS:
            assert (postrun.FINDINGS_DIR / f"{uuid}.reviewed").exists()

    def test_ambiguous_prefix_left_unmarked(self, postrun):
        (postrun.FINDINGS_DIR / "0eef8c47-2bc4-41d3-84df-c61e3ec2f9d1.md").write_text("a\n")
        (postrun.FINDINGS_DIR / "0eef8c47-9999-0000-1111-222233334444.md").write_text("b\n")
        target = str(postrun.ALLOWED_TARGET_ROOTS[0] / "rules" / "foo.md")
        text = PROPOSAL_TEMPLATE.format(pid="p1", target=target, cluster="c")
        text = text.replace("members: [0eef8c47-2bc4-41d3-84df-c61e3ec2f9d1, a2d9ddea-0320-417e-b556-d2a8a44420f2]", "members: [0eef8c47]")
        path = postrun.PENDING_DIR / "p1.md"
        path.write_text(text)
        rc = postrun.decide(str(path), "accept", reason="ok")
        assert rc == 0
        assert not list(postrun.FINDINGS_DIR.glob("*.reviewed"))
        decision = next(e for e in _ledger_events(postrun) if e["event"] == "decision")
        assert decision["members_ambiguous"] == "0eef8c47"
        assert decision["members_marked"] == ""


class TestDecideGuards:
    def test_decide_refuses_already_decided_path(self, postrun):
        path = postrun.PENDING_DIR / "p1.md"
        target = str(postrun.ALLOWED_TARGET_ROOTS[0] / "rules" / "foo.md")
        path.write_text(PROPOSAL_TEMPLATE.format(pid="p1", target=target, cluster="c"))
        assert postrun.decide(str(path), "accept", reason="ok") == 0
        moved = postrun.ACCEPTED_DIR / "p1.md"
        rc = postrun.decide(str(moved), "decline", reason="changed my mind")
        assert rc != 0
        decisions = [e for e in _ledger_events(postrun) if e["event"] == "decision"]
        assert len(decisions) == 1

    def test_basename_collision_uniquified_not_overwritten(self, postrun):
        target = str(postrun.ALLOWED_TARGET_ROOTS[0] / "rules" / "foo.md")
        postrun.ACCEPTED_DIR.mkdir(parents=True, exist_ok=True)
        (postrun.ACCEPTED_DIR / "p1.md").write_text("EARLIER DECISION\n")
        path = postrun.PENDING_DIR / "p1.md"
        path.write_text(PROPOSAL_TEMPLATE.format(pid="p1", target=target, cluster="c"))
        assert postrun.decide(str(path), "accept", reason="ok") == 0
        assert (postrun.ACCEPTED_DIR / "p1.md").read_text() == "EARLIER DECISION\n"
        assert (postrun.ACCEPTED_DIR / "p1-2.md").exists()

    def test_scalar_members_string_handled(self, postrun):
        (postrun.FINDINGS_DIR / "aaaa1111.md").write_text("finding\n")
        target = str(postrun.ALLOWED_TARGET_ROOTS[0] / "rules" / "foo.md")
        text = PROPOSAL_TEMPLATE.format(pid="p1", target=target, cluster="c")
        text = text.replace("members: [0eef8c47-2bc4-41d3-84df-c61e3ec2f9d1, a2d9ddea-0320-417e-b556-d2a8a44420f2]", "members: aaaa1111")
        path = postrun.PENDING_DIR / "p1.md"
        path.write_text(text)
        assert postrun.decide(str(path), "accept", reason="ok") == 0
        assert (postrun.FINDINGS_DIR / "aaaa1111.reviewed").exists()


class TestDiffBodyScopeGuard:
    def _meta_body(self, postrun, diff_block, target=None):
        target = target or str(postrun.ALLOWED_TARGET_ROOTS[0] / "rules" / "foo.md")
        text = PROPOSAL_TEMPLATE.format(pid="p1", target=target, cluster="c")
        text = text.replace("--- /dev/null\n+++ b/rules/foo.md\n@@ -0,0 +1 @@\n+new line", diff_block)
        return postrun.parse_frontmatter(text)

    def test_absolute_diff_path_outside_roots_rejected(self, postrun, tmp_path):
        sneaky = tmp_path / "sneaky" / "ssh_config"
        meta, body = self._meta_body(postrun, f"--- {sneaky}\n+++ {sneaky}\n+evil")
        violations = postrun.validate_proposal(meta, body)
        assert any("diff patches path outside allowed roots" in v for v in violations)

    def test_relative_diff_not_matching_declared_target_rejected(self, postrun):
        meta, body = self._meta_body(postrun, "--- a/other/file.md\n+++ b/other/file.md\n+x")
        violations = postrun.validate_proposal(meta, body)
        assert any("does not match any declared target" in v for v in violations)

    def test_dev_null_new_file_diff_ok(self, postrun):
        meta, body = self._meta_body(postrun, "--- /dev/null\n+++ b/rules/foo.md\n+new")
        assert postrun.validate_proposal(meta, body) == []

    def test_absolute_in_scope_diff_path_ok(self, postrun):
        target = str(postrun.ALLOWED_TARGET_ROOTS[0] / "rules" / "foo.md")
        meta, body = self._meta_body(postrun, f"--- {target}\n+++ {target}\n+x")
        assert postrun.validate_proposal(meta, body) == []

    def test_symlink_target_escaping_root_rejected(self, postrun, tmp_path):
        outside = tmp_path / "outside-root"
        outside.mkdir()
        link = postrun.ALLOWED_TARGET_ROOTS[0] / "rules" / "link.md"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside / "real.md")
        meta, body = self._meta_body(postrun, "--- a/rules/foo.md\n+++ b/rules/foo.md\n+x",
                                     target=str(link))
        violations = postrun.validate_proposal(meta, body)
        assert any("outside allowed roots" in v for v in violations)


class TestSevenFourCompleteness:
    @pytest.mark.parametrize("field", ["kind", "check_window_days"])
    def test_missing_new_required_field_rejected(self, postrun, field):
        path = _write_proposal(postrun, drop_field=field)
        meta, body = postrun.parse_frontmatter(path.read_text())
        assert any(field in v for v in postrun.validate_proposal(meta, body))

    def test_always_on_bytes_zero_is_valid(self, postrun):
        path = _write_proposal(postrun)
        text = path.read_text().replace("always_on_bytes: 9", "always_on_bytes: 0")
        meta, body = postrun.parse_frontmatter(text)
        assert postrun.validate_proposal(meta, body) == []

    def test_always_on_bytes_absent_rejected(self, postrun):
        path = _write_proposal(postrun, drop_field="always_on_bytes")
        meta, body = postrun.parse_frontmatter(path.read_text())
        assert any("always_on_bytes" in v for v in postrun.validate_proposal(meta, body))

    def test_missing_diff_section_rejected(self, postrun):
        path = _write_proposal(postrun)
        text = path.read_text().replace("## Diff", "## NotADiff")
        meta, body = postrun.parse_frontmatter(text)
        assert any("## Diff" in v for v in postrun.validate_proposal(meta, body))

    def test_quoted_values_stripped(self, postrun):
        path = _write_proposal(postrun)
        text = path.read_text().replace("cluster: claimed-vs-actual",
                                        'cluster: "claimed-vs-actual"')
        text = text.replace("members: [0eef8c47-2bc4-41d3-84df-c61e3ec2f9d1, a2d9ddea-0320-417e-b556-d2a8a44420f2]",
                            'members: ["0eef8c47-2bc4-41d3-84df-c61e3ec2f9d1", "a2d9ddea-0320-417e-b556-d2a8a44420f2"]')
        meta, _ = postrun.parse_frontmatter(text)
        assert meta["cluster"] == "claimed-vs-actual"
        assert meta["members"] == ["0eef8c47-2bc4-41d3-84df-c61e3ec2f9d1", "a2d9ddea-0320-417e-b556-d2a8a44420f2"]


class TestLedgerDerivedKnown:
    def test_second_postrun_does_not_redo_ledger_known_artifacts(self, postrun):
        _write_proposal(postrun)
        postrun.process_run_artifacts("r1", known=postrun.known_from_ledger())
        postrun.process_run_artifacts("r2", known=postrun.known_from_ledger())
        proposals = [e for e in _ledger_events(postrun) if e["event"] == "proposal"]
        assert len(proposals) == 1

    def test_artifact_written_between_runs_still_validated(self, postrun, tmp_path):
        _write_proposal(postrun, name="p1.md")
        postrun.process_run_artifacts("r1", known=postrun.known_from_ledger())
        # late artifact appears AFTER r1's postrun — a pre-run snapshot would
        # have hidden it forever; ledger-derived known catches it on r2
        _write_proposal(postrun, name="late-evil.md", target=str(tmp_path / "outside.md"))
        summary = postrun.process_run_artifacts("r2", known=postrun.known_from_ledger())
        assert summary["rejected"] == 1
        assert (postrun.REJECTED_DIR / "late-evil.md").exists()


class TestB2Vocabulary:
    def test_events_carry_dual_envelope_and_version(self, postrun):
        _write_proposal(postrun)
        postrun.process_run_artifacts("r1", known=set())
        event = _ledger_events(postrun)[0]
        assert event["type"] == "proposal" and event["event"] == "proposal"
        assert event["v"] == 1

    def test_proposal_event_carries_lane_class_evidence_kind(self, postrun):
        _write_proposal(postrun)
        postrun.process_run_artifacts("r1", known=set())
        event = next(e for e in _ledger_events(postrun) if e["type"] == "proposal")
        assert event["lane"] == "digest"
        assert event["class"] == "rule-edit"
        assert event["evidence_kind"] == "findings"

    def test_frontier_lane_frontmatter_wins(self, postrun):
        path = _write_proposal(postrun)
        text = path.read_text().replace("lane: digest", "lane: frontier")
        text = text.replace("evidence_kind: findings", "evidence_kind: external")
        text = "\n".join(line for line in text.splitlines()
                         if not line.startswith("members:")) + "\n"
        path.write_text(text)
        postrun.process_run_artifacts("r1", known=set())
        event = next(e for e in _ledger_events(postrun) if e["type"] == "proposal")
        assert event["lane"] == "frontier"
        assert event["evidence_kind"] == "external"

    def test_decision_event_carries_lane_class(self, postrun):
        path = _write_proposal(postrun)
        postrun.decide(str(path), "accept", reason="ok")
        decision = next(e for e in _ledger_events(postrun) if e["type"] == "decision")
        assert decision["lane"] == "digest"
        assert decision["class"] == "rule-edit"
        assert decision["evidence_kind"] == "findings"


class TestEvidenceKind:
    def _external_proposal(self, postrun, name="ext.md"):
        target = str(postrun.ALLOWED_TARGET_ROOTS[1] / "docs" / "adoption.md")
        text = PROPOSAL_TEMPLATE.format(pid=name.removesuffix(".md"),
                                        target=target, cluster="frontier-adoption")
        text = text.replace("evidence_kind: findings", "evidence_kind: external")
        text = "\n".join(line for line in text.splitlines()
                         if not line.startswith("members:")) + "\n"
        text = text.replace("--- /dev/null\n+++ b/rules/foo.md",
                            "--- /dev/null\n+++ b/docs/adoption.md")
        path = postrun.PENDING_DIR / name
        path.write_text(text)
        return path

    def test_external_without_members_is_valid(self, postrun):
        path = self._external_proposal(postrun)
        meta, body = postrun.parse_frontmatter(path.read_text())
        assert postrun.validate_proposal(meta, body) == []

    def test_findings_with_prefix_members_rejected_by_validation(self, postrun):
        path = _write_proposal(postrun)
        text = path.read_text().replace(
            "members: [0eef8c47-2bc4-41d3-84df-c61e3ec2f9d1, a2d9ddea-0320-417e-b556-d2a8a44420f2]",
            "members: [0eef8c47, ops-evidence]")
        meta, body = postrun.parse_frontmatter(text)
        violations = postrun.validate_proposal(meta, body)
        assert sum("not a full finding UUID" in v for v in violations) == 2

    def test_invalid_evidence_kind_rejected(self, postrun):
        path = _write_proposal(postrun)
        text = path.read_text().replace("evidence_kind: findings", "evidence_kind: vibes")
        meta, body = postrun.parse_frontmatter(text)
        assert any("evidence_kind must be one of" in v
                   for v in postrun.validate_proposal(meta, body))

    def test_missing_base_rev_rejected(self, postrun):
        path = _write_proposal(postrun, drop_field="base_rev")
        meta, body = postrun.parse_frontmatter(path.read_text())
        assert any("base_rev" in v for v in postrun.validate_proposal(meta, body))

    def test_decide_skips_burn_for_external_by_declaration(self, postrun):
        (postrun.FINDINGS_DIR / "0eef8c47-2bc4-41d3-84df-c61e3ec2f9d1.md").write_text("f\n")
        path = self._external_proposal(postrun)
        rc = postrun.decide(str(path), "accept", reason="adopt it")
        assert rc == 0
        assert not list(postrun.FINDINGS_DIR.glob("*.reviewed"))
        decision = next(e for e in _ledger_events(postrun) if e["type"] == "decision")
        assert decision["evidence_kind"] == "external"
        assert decision["members_marked"] == ""

    def test_missing_lane_rejected_by_validation(self, postrun):
        path = _write_proposal(postrun, drop_field="lane")
        meta, body = postrun.parse_frontmatter(path.read_text())
        assert any("lane" in v for v in postrun.validate_proposal(meta, body))

    def test_invalid_lane_value_rejected(self, postrun):
        path = _write_proposal(postrun)
        text = path.read_text().replace("lane: digest", "lane: sideways")
        meta, body = postrun.parse_frontmatter(text)
        assert any("lane must be one of" in v for v in postrun.validate_proposal(meta, body))

    def test_decision_event_lane_frontier_consistent(self, postrun):
        path = self._external_proposal(postrun)
        text = path.read_text().replace("lane: digest", "lane: frontier")
        path.write_text(text)
        rc = postrun.decide(str(path), "decline", reason="not worth it yet")
        assert rc == 0
        decision = next(e for e in _ledger_events(postrun) if e["type"] == "decision")
        assert decision["lane"] == "frontier"

    def test_quarantine_event_carries_lane(self, postrun, tmp_path):
        path = _write_proposal(postrun, name="evil.md", target=str(tmp_path / "outside.md"))
        text = path.read_text().replace("lane: digest", "lane: frontier")
        path.write_text(text)
        postrun.process_run_artifacts("r1", known=set(), lane="frontier")
        rejected = next(e for e in _ledger_events(postrun) if e["type"] == "proposal_rejected")
        assert rejected["lane"] == "frontier"

    def test_decide_burns_when_evidence_kind_absent_legacy(self, postrun):
        sid = "0eef8c47-2bc4-41d3-84df-c61e3ec2f9d1"
        (postrun.FINDINGS_DIR / f"{sid}.md").write_text("f\n")
        path = postrun.PENDING_DIR / "legacy.md"
        target = str(postrun.ALLOWED_TARGET_ROOTS[0] / "rules" / "foo.md")
        text = PROPOSAL_TEMPLATE.format(pid="legacy", target=target, cluster="c")
        text = "\n".join(line for line in text.splitlines()
                         if not line.startswith("evidence_kind:")) + "\n"
        text = text.replace(
            "members: [0eef8c47-2bc4-41d3-84df-c61e3ec2f9d1, a2d9ddea-0320-417e-b556-d2a8a44420f2]",
            f"members: [{sid}]")
        path.write_text(text)
        rc = postrun.decide(str(path), "accept", reason="ok")
        assert rc == 0
        assert (postrun.FINDINGS_DIR / f"{sid}.reviewed").exists()


DAY = 86400
NOW = 1_800_000_000.0  # fixed maturity clock for deterministic tests


def _arm(postrun, check_id, ts, window="14", expectation="exp",
         run_id="r", cluster="c", lane="digest"):
    """Append a raw check_armed ledger line with a controllable ts."""
    rec = {"type": "check_armed", "event": "check_armed", "v": 1, "ts": ts,
           "check_id": check_id, "check_window_days": window,
           "expectation": expectation, "run_id": run_id, "cluster": cluster,
           "path": f"/x/{check_id}.md", "lane": lane}
    postrun.LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with postrun.LEDGER_PATH.open("a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def _outcomes(postrun):
    return [e for e in _ledger_events(postrun)
            if (e.get("type") or e.get("event")) in ("check_kept", "check_violated")]


def _verdicts_file(tmp_path, mapping):
    p = tmp_path / "verdicts.json"
    p.write_text(json.dumps(mapping))
    return str(p)


class TestEvaluate:
    def test_matured_recorded_unmatured_skipped(self, postrun, tmp_path):  # case 1
        _arm(postrun, "c-old", NOW - 20 * DAY)   # window 14 → matured
        _arm(postrun, "c-new", NOW - 5 * DAY)    # window 14 → not due
        vf = _verdicts_file(tmp_path, {"c-old": "kept", "c-new": "kept"})
        rc = postrun.evaluate(vf, dry_run=False, now=NOW)
        assert rc == 0
        outs = _outcomes(postrun)
        assert [o["check_id"] for o in outs] == ["c-old"]
        assert outs[0]["type"] == "check_kept"

    def test_event_shape(self, postrun, tmp_path):  # case 2
        _arm(postrun, "c1", NOW - 20 * DAY, window="14",
             expectation="no recurrence", run_id="r7", cluster="clstr", lane="digest")
        vf = _verdicts_file(tmp_path, {"c1": {"verdict": "kept", "evidence": "0 hits"}})
        assert postrun.evaluate(vf, dry_run=False, now=NOW) == 0
        o = _outcomes(postrun)[0]
        assert o["type"] == "check_kept" and o["event"] == "check_kept" and o["v"] == 1
        assert o["check_id"] == "c1" and o["run_id"] == "r7" and o["cluster"] == "clstr"
        assert o["expectation"] == "no recurrence"
        assert o["check_window_days"] == "14" and o["armed_ts"] == NOW - 20 * DAY
        assert o["lane"] == "digest" and o["verdict"] == "kept" and o["evidence"] == "0 hits"

    def test_idempotent_rerun(self, postrun, tmp_path):  # case 3
        _arm(postrun, "c1", NOW - 20 * DAY)
        vf = _verdicts_file(tmp_path, {"c1": "kept"})
        assert postrun.evaluate(vf, dry_run=False, now=NOW) == 0
        assert postrun.evaluate(vf, dry_run=False, now=NOW) == 0   # rerun: no false alarm
        assert len(_outcomes(postrun)) == 1

    def test_dry_run_writes_nothing(self, postrun, tmp_path):  # case 4
        _arm(postrun, "c1", NOW - 20 * DAY)
        vf = _verdicts_file(tmp_path, {"c1": "kept"})
        before = postrun.LEDGER_PATH.stat().st_size
        rc = postrun.evaluate(vf, dry_run=True, now=NOW)
        assert rc == 0
        assert postrun.LEDGER_PATH.stat().st_size == before
        assert _outcomes(postrun) == []

    def test_violated_requires_evidence(self, postrun, tmp_path):  # case 5
        _arm(postrun, "c1", NOW - 20 * DAY)
        vf = _verdicts_file(tmp_path, {"c1": "violated"})
        rc = postrun.evaluate(vf, dry_run=False, now=NOW)
        assert rc != 0
        assert _outcomes(postrun) == []

    def test_violated_with_evidence_records(self, postrun, tmp_path):  # case 6
        _arm(postrun, "c1", NOW - 20 * DAY)
        vf = _verdicts_file(tmp_path, {"c1": {"verdict": "violated", "evidence": "recurred 3x"}})
        assert postrun.evaluate(vf, dry_run=False, now=NOW) == 0
        o = _outcomes(postrun)[0]
        assert o["type"] == "check_violated" and o["verdict"] == "violated"
        assert o["evidence"] == "recurred 3x"

    def test_unknown_check_id_refused_others_processed(self, postrun, tmp_path):  # case 7
        _arm(postrun, "c-good", NOW - 20 * DAY)
        vf = _verdicts_file(tmp_path, {"c-good": "kept", "c-bogus": "kept"})
        rc = postrun.evaluate(vf, dry_run=False, now=NOW)
        assert rc != 0
        assert [o["check_id"] for o in _outcomes(postrun)] == ["c-good"]

    def test_bare_evaluate_no_verdicts_awaiting(self, postrun, capsys):  # case 8
        _arm(postrun, "c1", NOW - 20 * DAY)
        rc = postrun.evaluate(None, dry_run=False, now=NOW)
        assert rc == 0
        assert _outcomes(postrun) == []
        assert "AWAITING-VERDICT" in capsys.readouterr().out

    def test_append_only_expectation_preserved(self, postrun, tmp_path):  # case 9
        exp = 'quote " and unicode ✓ expectation'
        _arm(postrun, "c1", NOW - 20 * DAY, expectation=exp)
        vf = _verdicts_file(tmp_path, {"c1": "kept"})
        assert postrun.evaluate(vf, dry_run=False, now=NOW) == 0
        assert _outcomes(postrun)[0]["expectation"] == exp

    def test_kept_without_evidence_allowed(self, postrun, tmp_path):  # case 10
        _arm(postrun, "c1", NOW - 20 * DAY)
        vf = _verdicts_file(tmp_path, {"c1": "kept"})
        assert postrun.evaluate(vf, dry_run=False, now=NOW) == 0
        assert _outcomes(postrun)[0]["evidence"] == ""

    def test_malformed_verdicts_file_hard_error(self, postrun, tmp_path):  # case 11
        _arm(postrun, "c1", NOW - 20 * DAY)
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json")
        assert postrun.evaluate(str(bad), dry_run=False, now=NOW) == 2
        arr = tmp_path / "arr.json"
        arr.write_text("[1, 2, 3]")
        assert postrun.evaluate(str(arr), dry_run=False, now=NOW) == 2
        assert _outcomes(postrun) == []

    def test_unknown_verdict_string_refused(self, postrun, tmp_path):  # case 12
        _arm(postrun, "c1", NOW - 20 * DAY)
        vf = _verdicts_file(tmp_path, {"c1": "maybe"})
        assert postrun.evaluate(vf, dry_run=False, now=NOW) != 0
        assert _outcomes(postrun) == []

    def test_window_parse_error(self, postrun, tmp_path):  # case 13
        _arm(postrun, "c-bad", NOW - 20 * DAY, window="not-a-number")
        _arm(postrun, "c-good", NOW - 20 * DAY, window="14")
        vf = _verdicts_file(tmp_path, {"c-bad": "kept", "c-good": "kept"})
        rc = postrun.evaluate(vf, dry_run=False, now=NOW)
        assert rc != 0
        assert [o["check_id"] for o in _outcomes(postrun)] == ["c-good"]

    def test_duplicate_arm_records_against_first(self, postrun, tmp_path, capsys):  # case 14
        _arm(postrun, "c1", NOW - 20 * DAY, expectation="E1")
        _arm(postrun, "c1", NOW - 10 * DAY, expectation="E2-weaker")
        vf = _verdicts_file(tmp_path, {"c1": "kept"})
        rc = postrun.evaluate(vf, dry_run=False, now=NOW)
        assert rc != 0                                   # DUPLICATE-ARM is an anomaly
        outs = _outcomes(postrun)
        assert len(outs) == 1
        assert outs[0]["expectation"] == "E1"            # first, immutable
        assert outs[0]["armed_ts"] == NOW - 20 * DAY     # first stamp's ts
        assert "DUPLICATE-ARM" in capsys.readouterr().out

    def test_duplicate_arm_identical_expectation_benign(self, postrun, tmp_path):  # case 14b
        _arm(postrun, "c1", NOW - 20 * DAY, expectation="E")
        _arm(postrun, "c1", NOW - 10 * DAY, expectation="E")
        vf = _verdicts_file(tmp_path, {"c1": "kept"})
        assert postrun.evaluate(vf, dry_run=False, now=NOW) == 0   # no anomaly
        assert len(_outcomes(postrun)) == 1

    def test_duplicate_arm_rerun_idempotent(self, postrun, tmp_path):  # pins guard ordering
        # A recorded DUPLICATE-ARM check reruns to exit 0 (ALREADY-RECORDED
        # precedes the duplicate-arm branch), not a re-raised anomaly.
        _arm(postrun, "c1", NOW - 20 * DAY, expectation="E1")
        _arm(postrun, "c1", NOW - 10 * DAY, expectation="E2-weaker")
        vf = _verdicts_file(tmp_path, {"c1": "kept"})
        assert postrun.evaluate(vf, dry_run=False, now=NOW) != 0   # first run: DUPLICATE-ARM anomaly
        assert postrun.evaluate(vf, dry_run=False, now=NOW) == 0   # rerun: ALREADY-RECORDED
        assert len(_outcomes(postrun)) == 1                        # still exactly one event

    # delta = seconds PAST the maturity boundary (armed earlier → more elapsed →
    # matured). is_matured is the correct inclusive `>=`; do NOT alter it to fit
    # this test — the arithmetic below is what pins delta=0 as matured.
    @pytest.mark.parametrize("delta,matured", [(-1, False), (0, True), (1, True)])
    def test_maturity_boundary_inclusive(self, postrun, delta, matured):  # case 15
        info = {"armed_ts": NOW - 14 * DAY - delta, "check_window_days": "14"}
        assert postrun.is_matured(info, NOW) is matured

    def test_main_cli_dry_run(self, postrun, tmp_path):  # CLI wiring
        _arm(postrun, "c1", NOW - 20 * DAY)
        vf = _verdicts_file(tmp_path, {"c1": "kept"})
        rc = postrun.main(["evaluate", "--verdicts", vf, "--dry-run", "--now", str(NOW)])
        assert rc == 0
        assert _outcomes(postrun) == []


class TestConfigTomlStr:
    def test_config_toml_str_reads_key(self, postrun, tmp_path, monkeypatch):
        cfg = tmp_path / "dockwright.toml"
        cfg.write_text("[evals]\ninvestigate_skill = '~/x/SKILL.md'\n")
        monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
        assert postrun.config_toml_str("evals", "investigate_skill") == "~/x/SKILL.md"
        assert postrun.config_toml_str("evals", "missing") == ""
        assert postrun.config_toml_str("nope", "x") == ""

    def test_config_toml_str_no_config(self, postrun, monkeypatch, tmp_path):
        monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
        assert postrun.config_toml_str("evals", "investigate_skill") == ""

    def test_dockwright_repo_still_resolves_via_config_toml_str(self, postrun, tmp_path, monkeypatch):
        cfg = tmp_path / "dockwright.toml"
        cfg.write_text('[paths]\ndockwright_repo = "~/repo"\n')
        monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
        assert postrun._dockwright_repo() == str(Path("~/repo").expanduser())


class TestDecideAppliedRev:
    def test_decide_records_applied_rev(self, postrun):
        path = _write_proposal(postrun)
        rc = postrun.decide(str(path), "accept", "ok", applied_rev=["/r=abc123"])
        assert rc == 0
        events = _ledger_events(postrun)
        dec = [e for e in events if e["type"] == "decision"][-1]
        assert dec["applied_rev"] == "/r=abc123"

    def test_decide_applied_rev_defaults_empty(self, postrun):
        path = _write_proposal(postrun)
        postrun.decide(str(path), "accept", "ok")
        events = _ledger_events(postrun)
        dec = [e for e in events if e["type"] == "decision"][-1]
        assert dec["applied_rev"] == ""


class TestHomeFallback:
    def test_prefers_dockwright_homes(self, tmp_path, monkeypatch):
        # opt out of the autouse DOCKWRIGHT_GARDENER_DIR override: this test
        # probes the HOME-based _prefer_new branch (env-unset path).
        monkeypatch.delenv("DOCKWRIGHT_GARDENER_DIR", raising=False)
        claude = tmp_path / ".claude"
        for rel in ("dockwright/gardener", "gardener",
                    "dockwright/selffix/findings", "selffix-findings"):
            (claude / rel).mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        mod = _load_postrun()
        assert mod.GARDENER_DIR == claude / "dockwright" / "gardener"
        assert mod.FINDINGS_DIR == claude / "dockwright" / "selffix" / "findings"

    def test_falls_back_to_legacy_homes(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DOCKWRIGHT_GARDENER_DIR", raising=False)
        claude = tmp_path / ".claude"
        for rel in ("gardener", "selffix-findings"):
            (claude / rel).mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        mod = _load_postrun()
        assert mod.GARDENER_DIR == claude / "gardener"
        assert mod.FINDINGS_DIR == claude / "selffix-findings"


# ---- birth gate (Task 4): apply-check a new pending proposal at postrun ----


def _proposal_text(targets, diff_section, kind="rule-edit"):
    tlist = ", ".join(targets)
    return (
        "---\n"
        "id: r9-1\nrun_id: r9\ncluster: c\nlane: digest\n"
        "evidence_kind: ops\n"
        f"kind: {kind}\nalways_on_bytes: 0\nflow_cost: none\nbase_rev: abc1234\n"
        f"targets: [{tlist}]\n"
        "expectation: e\ncheck_window_days: 7\nrevert: r\n"
        "---\n\n## Evidence\nE\n\n## Diff\n" + diff_section
        + "\n\n## Rationale\nR\n")


@pytest.fixture()
def postrun_env(tmp_path, monkeypatch):
    mod = _load_postrun()
    # config isolation: point at an absent file so config_path() -> None and
    # every config_toml_* read returns its default, regardless of the
    # developer's live ~/.claude/dockwright.toml
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "no-config.toml"))
    root = tmp_path / "root"
    (root / "rules").mkdir(parents=True)
    pending = tmp_path / "pending"
    pending.mkdir()
    monkeypatch.setattr(mod, "PENDING_DIR", pending)
    monkeypatch.setattr(mod, "CHECKS_DIR", tmp_path / "checks")
    monkeypatch.setattr(mod, "REJECTED_DIR", tmp_path / "rejected")
    monkeypatch.setattr(mod, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(mod, "ALLOWED_TARGET_ROOTS", [root])
    return mod, root, pending


def test_birth_gate_quarantines_dead_patch(postrun_env):
    """A drifted-at-birth diff must be quarantined, not enqueued.

    RED-proof (spec Testing §2, drift-guard discipline): with the
    `verdict, detail = _apply_check(...)` gate block removed from
    process_run_artifacts in a scratch copy of gardener_postrun.py, this
    proposal lands in the ledger as a valid `proposal` event and the test
    goes RED — verified 2026-07-22, captured output:

        >       assert summary["rejected"] == 1
        E       assert 0 == 1
        tests/test_gardener_postrun.py:... test_birth_gate_quarantines_dead_patch
        1 failed

    Restored after; GREEN with the gate present."""
    mod, root, pending = postrun_env
    (root / "rules" / "x.md").write_text("alpha\nbeta\n")
    text = _proposal_text(
        [str(root / "rules" / "x.md")],
        "```diff\n--- a/rules/x.md\n+++ b/rules/x.md\n@@\n GONE\n+x\n```")
    (pending / "r9-1.md").write_text(text)
    summary = mod.process_run_artifacts("r9", set())
    assert summary["rejected"] == 1
    rejected = list(mod.REJECTED_DIR.glob("*.md"))
    assert len(rejected) == 1
    events = [json.loads(l) for l in mod.LEDGER_PATH.read_text().splitlines()]
    assert events[-1]["event"] == "proposal_rejected"
    assert "apply-check failed at birth (drifted)" in events[-1]["reasons"]


def test_birth_gate_quarantines_undeclared_absolute_diff_path(postrun_env):
    """I4: declare-one-patch-another by ABSOLUTE path. A proposal declaring
    rules/x.md whose diff patches rules/other.md (also in allowed roots) by
    absolute path is a scope-guard bypass — quarantined at birth, the reason
    naming the undeclared path (validate_proposal's _diff_path_violations now
    checks the absolute form against declared targets, symmetric with a/<rel>).

    RED-proof (executed 2026-07-22): revert the absolute-branch declared-target
    check in _diff_path_violations in a scratch copy -> validate_proposal returns
    no violation, the proposal is enqueued as a valid `proposal` event, and
    summary["rejected"] == 0. Restored: GREEN (rejected == 1)."""
    mod, root, pending = postrun_env
    (root / "rules" / "x.md").write_text("alpha\n")
    other = root / "rules" / "other.md"
    other.write_text("alpha\n")
    text = _proposal_text(
        [str(root / "rules" / "x.md")],
        f"```diff\n--- {other}\n+++ {other}\n@@ -1 +1 @@\n-alpha\n+ALPHA\n```")
    (pending / "r9-1.md").write_text(text)
    summary = mod.process_run_artifacts("r9", set())
    assert summary["rejected"] == 1
    events = [json.loads(l) for l in mod.LEDGER_PATH.read_text().splitlines()]
    assert events[-1]["event"] == "proposal_rejected"
    assert "not declared in targets" in events[-1]["reasons"]


def test_birth_gate_passes_and_records_reanchorable(postrun_env):
    mod, root, pending = postrun_env
    (root / "rules" / "x.md").write_text("alpha\nbeta\ngamma\n")
    text = _proposal_text(
        [str(root / "rules" / "x.md")],
        "```diff\n--- a/rules/x.md\n+++ b/rules/x.md\n"
        "@@ -1,9 +1,9 @@\n alpha\n-beta\n+BETA\n gamma\n```")
    (pending / "r9-1.md").write_text(text)
    summary = mod.process_run_artifacts("r9", set())
    assert summary["proposals"] == 1 and summary["rejected"] == 0
    events = [json.loads(l) for l in mod.LEDGER_PATH.read_text().splitlines()]
    assert events[-1]["event"] == "proposal"
    assert events[-1]["apply_check"] == "reanchorable"


def test_birth_gate_fenceless_build_brief_passes_no_diff(postrun_env):
    """kind: build-brief ships a prose ## Diff — must get the honest
    apply_check=no-diff label, decided by fence PRESENCE, not the kind
    declaration.

    RED-proof (spec Testing §2): with the fence-presence exemption removed
    in a scratch copy (the `if not _DIFF_FENCE_RE.search(body): return
    "no-diff", ""` early-return deleted so the gate runs classify
    unconditionally), a fenceless body makes classify_proposal's
    extract_diff_text raise (code=2, klass=None); under env_lenient=True
    that yields apply_check=skipped-env instead of the honest no-diff, so
    the label assertion goes RED — verified 2026-07-22, captured output:

        >       assert events[-1]["apply_check"] == "no-diff"
        E       AssertionError: assert 'skipped-env' == 'no-diff'
        E         - no-diff
        E         + skipped-env
        tests/test_gardener_postrun.py:887: AssertionError
        1 failed

    (The exemption is load-bearing for the LABEL, not for pass/fail:
    env_lenient keeps a fenceless proposal out of quarantine either way,
    but only the exemption records the correct no-diff class.) Restored
    after; GREEN with the fence exemption present."""
    mod, root, pending = postrun_env
    text = _proposal_text(
        [str(root / "rules" / "x.md")],
        "Build brief: prose description, no diff fence.",
        kind="build-brief")
    (pending / "r9-1.md").write_text(text)
    summary = mod.process_run_artifacts("r9", set())
    assert summary["proposals"] == 1 and summary["rejected"] == 0
    events = [json.loads(l) for l in mod.LEDGER_PATH.read_text().splitlines()]
    assert events[-1]["apply_check"] == "no-diff"


def test_birth_gate_env_failure_never_quarantines(postrun_env, monkeypatch):
    mod, root, pending = postrun_env
    (root / "rules" / "x.md").write_text("alpha\n")
    text = _proposal_text(
        [str(root / "rules" / "x.md")],
        "```diff\n--- a/rules/x.md\n+++ b/rules/x.md\n@@\n alpha\n+x\n```")
    (pending / "r9-1.md").write_text(text)

    def boom(path, env_lenient=False):
        raise RuntimeError("git not installed")

    import gardener_apply
    monkeypatch.setattr(gardener_apply, "classify_proposal", boom)
    summary = mod.process_run_artifacts("r9", set())
    assert summary["proposals"] == 1 and summary["rejected"] == 0
    events = [json.loads(l) for l in mod.LEDGER_PATH.read_text().splitlines()]
    assert events[-1]["apply_check"] == "skipped-env"


def test_birth_gate_env_failure_warns_on_stderr(postrun_env, monkeypatch, capsys):
    """skipped-env must not vacuate the birth gate silently: a broken
    environment has to be visible on stderr, not just dropped."""
    mod, root, pending = postrun_env
    (root / "rules" / "x.md").write_text("alpha\n")
    text = _proposal_text(
        [str(root / "rules" / "x.md")],
        "```diff\n--- a/rules/x.md\n+++ b/rules/x.md\n@@\n alpha\n+x\n```")
    (pending / "r9-1.md").write_text(text)

    def boom(path, env_lenient=False):
        raise RuntimeError("git not installed")

    import gardener_apply
    monkeypatch.setattr(gardener_apply, "classify_proposal", boom)
    summary = mod.process_run_artifacts("r9", set())
    captured = capsys.readouterr()
    assert "apply-check skipped (environment)" in captured.err
    assert summary["proposals"] == 1 and summary["rejected"] == 0
    events = [json.loads(l) for l in mod.LEDGER_PATH.read_text().splitlines()]
    assert events[-1]["apply_check"] == "skipped-env"


def test_birth_gate_skipped_env_counted_in_summary(postrun_env, monkeypatch):
    """M5: a skipped-env verdict is counted in the postrun summary dict so a
    machine-wide vacuous birth gate (every proposal skipped-env) is visible in
    the one-line summary, not only scattered stderr WARNINGs."""
    mod, root, pending = postrun_env
    (root / "rules" / "x.md").write_text("alpha\n")
    text = _proposal_text(
        [str(root / "rules" / "x.md")],
        "```diff\n--- a/rules/x.md\n+++ b/rules/x.md\n@@\n alpha\n+x\n```")
    (pending / "r9-1.md").write_text(text)

    def boom(path, env_lenient=False):
        raise RuntimeError("git not installed")

    import gardener_apply
    monkeypatch.setattr(gardener_apply, "classify_proposal", boom)
    summary = mod.process_run_artifacts("r9", set())
    assert summary["skipped_env"] == 1
    assert summary["proposals"] == 1 and summary["rejected"] == 0


def test_annotate_appends_correction_only(postrun):
    """M1: annotate appends a typed correction event referencing an earlier
    entry. The ledger is APPEND-ONLY — the prior bytes must be byte-identical
    and nothing else added/removed; the correct response to a stray event is an
    appended annotation, never an in-place edit. No top-level path key."""
    postrun.ledger_append("proposal", proposal_id="p1", path="/x")
    postrun.ledger_append("decision", proposal_id="p1", kind="accept")
    before = postrun.LEDGER_PATH.read_text()
    rc = postrun.annotate("p1", "superseded by cluster merge")
    assert rc == 0
    after = postrun.LEDGER_PATH.read_text()
    assert after.startswith(before)      # append-only: prior bytes untouched
    events = _ledger_events(postrun)
    assert len(events) == 3
    last = events[-1]
    assert last["type"] == "annotate"
    assert last["ref"] == "p1"
    assert last["note"] == "superseded by cluster merge"
    assert "path" not in last


def test_gardener_dir_env_redirects_subprocess_ledger(tmp_path):
    """I3 by-construction (subprocess): a CLI run that sets
    DOCKWRIGHT_GARDENER_DIR writes THAT ledger, never the live one — no HOME
    faking required. An ad-hoc reviewer/test run redirects the audit record
    structurally, closing the incident's still-open write path."""
    sub = tmp_path / "sub-gardener"
    env = {**os.environ, "DOCKWRIGHT_GARDENER_DIR": str(sub)}
    proc = subprocess.run(
        [sys.executable, str(POSTRUN_PATH), "annotate",
         "--ref", "e-123", "--note", "correction"],
        env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    ledger = sub / "ledger.jsonl"
    assert ledger.is_file()
    events = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
    assert events[-1]["type"] == "annotate"
    assert events[-1]["ref"] == "e-123"


def test_gardener_dir_env_redirects_fresh_import(tmp_path):
    """I3 by-construction (fresh import): under the autouse
    DOCKWRIGHT_GARDENER_DIR env, a fresh module load derives its LEDGER_PATH
    from the env — a leaked import can never point at the live ledger."""
    mod = _load_postrun()
    assert str(tmp_path) in str(mod.LEDGER_PATH)
    assert str(mod.LEDGER_PATH).endswith(
        os.path.join("gardener-state", "ledger.jsonl"))


def test_birth_gate_skips_ledger_known_proposals(postrun_env):
    """Rollout safety: the live pending proposals are ledger-known and must
    never be retro-quarantined."""
    mod, root, pending = postrun_env
    text = _proposal_text(
        [str(root / "rules" / "x.md")],
        "```diff\n--- a/rules/x.md\n+++ b/rules/x.md\n@@\n GONE\n+x\n```")
    (pending / "known.md").write_text(text)
    summary = mod.process_run_artifacts("r9", {"known.md"})
    assert summary["rejected"] == 0
    assert (pending / "known.md").exists()


# ---- back-pressure: computed always-on delta (spec 2a) ---------------------


def _delta_body(diff: str) -> str:
    return "## Evidence\nE\n\n## Diff\n```diff\n" + diff + "\n```\n\n## Rationale\nR\n"


class TestComputeAlwaysOnDelta:
    def _rules_target(self, postrun):
        return [str(postrun.ALLOWED_TARGET_ROOTS[0] / "rules" / "foo.md")]

    def test_ab_relative_headers_resolve_against_declared_targets(self, postrun):
        """C1: git-conventional a/…​/b/… headers must resolve by suffix-match
        into the declared targets (the actuator's rule), never via realpath
        from CWD — otherwise every honest a/b eviction diff computes 0."""
        diff = ("--- a/rules/foo.md\n"
                "+++ b/rules/foo.md\n"
                "@@ -1,3 +1,1 @@\n"
                "-line one\n"
                "-line two\n"
                "-line three\n"
                "+one\n")
        delta = postrun.compute_always_on_delta(_delta_body(diff), self._rules_target(postrun))
        assert delta == (len("one") + 1) - (len("line one") + 1 + len("line two") + 1 + len("line three") + 1)

    def test_new_file_dev_null_counts_all_added_bytes(self, postrun):
        diff = ("--- /dev/null\n"
                "+++ b/rules/foo.md\n"
                "@@ -0,0 +1 @@\n"
                "+new line\n")
        assert postrun.compute_always_on_delta(_delta_body(diff), self._rules_target(postrun)) == 9

    def test_deleted_file_is_negative(self, postrun):
        diff = ("--- a/rules/foo.md\n"
                "+++ /dev/null\n"
                "@@ -1,2 +0,0 @@\n"
                "-alpha\n"
                "-beta\n")
        assert postrun.compute_always_on_delta(_delta_body(diff), self._rules_target(postrun)) == -11

    def test_mixed_multi_file_counts_only_always_on(self, postrun):
        """rules file shrinks, skill file grows — only the rules part counts."""
        root = postrun.ALLOWED_TARGET_ROOTS[0]
        targets = [str(root / "rules" / "foo.md"), str(root / "skills" / "bar" / "SKILL.md")]
        diff = ("--- a/rules/foo.md\n"
                "+++ b/rules/foo.md\n"
                "@@ -1,1 +0,0 @@\n"
                "-gone\n"
                "--- a/skills/bar/SKILL.md\n"
                "+++ b/skills/bar/SKILL.md\n"
                "@@ -1,0 +1,1 @@\n"
                "+very long added skill line that would dominate if counted\n")
        assert postrun.compute_always_on_delta(_delta_body(diff), targets) == -5

    def test_cyrillic_counts_utf8_bytes_not_chars(self, postrun):
        """I1: 'абвгд' is 5 chars but 10 UTF-8 bytes."""
        diff = ("--- a/rules/foo.md\n"
                "+++ b/rules/foo.md\n"
                "@@ -1,1 +0,0 @@\n"
                "-абвгд\n")
        assert postrun.compute_always_on_delta(_delta_body(diff), self._rules_target(postrun)) == -11

    def test_content_lines_starting_with_dashes_not_misread_as_headers(self, postrun):
        """C1 sibling: a removed content line '-- old' renders as '--- old';
        the hunk-count-tracking parser must keep it inside the hunk."""
        diff = ("--- a/rules/foo.md\n"
                "+++ b/rules/foo.md\n"
                "@@ -1,2 +1,1 @@\n"
                "--- old heading\n"
                "-second\n"
                "+-- new heading\n")
        expected = (len("-- new heading") + 1) - (len("-- old heading") + 1 + len("second") + 1)
        assert postrun.compute_always_on_delta(_delta_body(diff), self._rules_target(postrun)) == expected

    def test_prose_diff_lines_outside_fence_ignored(self, postrun):
        body = ("## Evidence\nE\n\n## Diff\n"
                "The plan was:\n--- a/rules/foo.md\n+++ b/rules/foo.md\n\n"
                "```diff\n"
                "--- a/rules/foo.md\n"
                "+++ b/rules/foo.md\n"
                "@@ -1,1 +0,0 @@\n"
                "-x\n"
                "```\n\n## Rationale\nR\n")
        assert postrun.compute_always_on_delta(body, self._rules_target(postrun)) == -2

    def test_no_newline_marker_undoes_the_newline_byte(self, postrun):
        diff = ("--- /dev/null\n"
                "+++ b/rules/foo.md\n"
                "@@ -0,0 +1 @@\n"
                "+tail\n"
                "\\ No newline at end of file\n")
        assert postrun.compute_always_on_delta(_delta_body(diff), self._rules_target(postrun)) == 4

    def test_bare_at_at_edit_falls_back_to_lenient(self, postrun):
        """A bare (unnumbered) `@@` hunk header — the form the generator
        historically emits — fails the strict split_file_diffs; the actuator's
        lenient parser reads the same extract_diff_text output, and the byte
        walk runs over its per-file hunk bodies with identical arithmetic. This
        must return the real delta, not None (strict-only returned None here)."""
        diff = ("--- a/rules/foo.md\n"
                "+++ b/rules/foo.md\n"
                "@@\n"
                " beta\n"
                "+gamma\n")
        delta = postrun.compute_always_on_delta(_delta_body(diff), self._rules_target(postrun))
        assert delta == len("gamma") + 1

    def test_unresolvable_header_returns_none(self, postrun):
        """Fail-closed: one unmatched file poisons the whole computation."""
        diff = ("--- a/rules/other.md\n"
                "+++ b/rules/other.md\n"
                "@@ -1,1 +0,0 @@\n"
                "-x\n")
        assert postrun.compute_always_on_delta(_delta_body(diff), self._rules_target(postrun)) is None

    def test_unparseable_diff_returns_none(self, postrun):
        assert postrun.compute_always_on_delta(_delta_body("not a diff at all"), self._rules_target(postrun)) is None

    def test_no_fence_returns_none(self, postrun):
        assert postrun.compute_always_on_delta("## Diff\nprose only\n", self._rules_target(postrun)) is None

    def test_non_always_on_target_counts_zero(self, postrun):
        root = postrun.ALLOWED_TARGET_ROOTS[0]
        targets = [str(root / "skills" / "bar" / "SKILL.md")]
        diff = ("--- a/skills/bar/SKILL.md\n"
                "+++ b/skills/bar/SKILL.md\n"
                "@@ -1,0 +1,1 @@\n"
                "+added\n")
        assert postrun.compute_always_on_delta(_delta_body(diff), targets) == 0

    def test_overcounted_first_hunk_swallow_recomputes_from_lenient(self, postrun):
        """IMPORTANT (final-review): a hand-written FIRST hunk header that
        OVERCOUNTS makes strict split_file_diffs' count-consumption loop swallow
        the NEXT file's `--- `/`+++ ` header and `@@` line into the first file's
        hunk body — strict parse SUCCEEDS (the swallowed `@@` still balances the
        total_headers==parsed_headers assertion) but the second file's `+` lines
        vanish from attribution.

        Reviewer's repro shape: rules/bar.md removes three 50-byte lines
        (net -153) under an OVERCOUNTED `@@ -1,7 +1,4 @@` (real body 2 ctx + 3
        removed = -1,5 +1,2); the counts drive the loop across rules/baz.md's
        `--- `/`+++ `/`@@` header (the two path lines, equal length, cancel in
        the strict body walk, leaving a clean strict -153) and stop right before
        baz's `+` lines, which are then dropped by the outer file loop. baz ADDS
        four 49-byte lines (+200). The strict reading computes -153; the
        actuator's lenient re-anchor (what actually applies, since git refuses
        the bad counts) computes the TRUE +47. This gate must return +47.

        RED-proof (drift-guard-tests.md), executed 2026-07-23 against the
        strict-only compute_always_on_delta (no lenient cross-check):

            >       assert postrun.compute_always_on_delta(_delta_body(diff), targets) == expected
            E       assert -153 == 47
            tests/test_gardener_postrun.py: test_overcounted_first_hunk_swallow_recomputes_from_lenient
            1 failed

        The strict swallow mis-attributed -153 to bar and dropped baz's +200
        entirely. GREEN once the strict per-file (old_raw, new_raw) list is
        cross-checked against lenient_parse and the divergent (swallow) shape
        recomputes from the lenient parse."""
        root = postrun.ALLOWED_TARGET_ROOTS[0]
        targets = [str(root / "rules" / "bar.md"), str(root / "rules" / "baz.md")]
        rem = "R" * 50   # each removed line -> -(50 + 1); three -> -153
        add = "A" * 49   # each added line   -> +(49 + 1); four  -> +200
        diff = ("--- a/rules/bar.md\n"
                "+++ b/rules/bar.md\n"
                "@@ -1,7 +1,4 @@\n"
                " ctx1\n"
                " ctx2\n"
                f"-{rem}\n"
                f"-{rem}\n"
                f"-{rem}\n"
                "--- a/rules/baz.md\n"
                "+++ b/rules/baz.md\n"
                "@@ -1,1 +1,5 @@\n"
                " keep\n"
                f"+{add}\n"
                f"+{add}\n"
                f"+{add}\n"
                f"+{add}\n")
        expected = -3 * (len(rem) + 1) + 4 * (len(add) + 1)  # -153 + 200 = +47
        assert expected == 47
        assert postrun.compute_always_on_delta(_delta_body(diff), targets) == expected

    def test_undercounted_hunk_truncation_recomputes_from_lenient(self, postrun):
        """IMPORTANT-2 (final-review sibling of the boundary swallow): a
        hand-written hunk header that UNDERCOUNTS the real body makes strict
        split_file_diffs count-SATISFY early and silently drop the trailing
        body lines WITHIN one file — no file boundary is crossed, so the strict
        and lenient (old_raw, new_raw) pair lists are IDENTICAL and a pair-only
        cross-check keeps the truncated strict walk. Only comparing the
        hunk-body line lists too catches it.

        Repro shape: rules/foo.md hunk body is `ctx / -200B / +small / +125B /
        +125B` under an UNDERCOUNTED `@@ -1,2 +1,2 @@` (old 2 = ctx + removal,
        new 2 = ctx + the small addition). Strict count-satisfies right after
        `+small` and drops both +125B lines → computes -199; the actuator's
        lenient re-anchor applies the whole body → TRUE +53.

        RED-proof (drift-guard-tests.md), executed 2026-07-23 against the
        pair-list-only cross-check:

            >       assert postrun.compute_always_on_delta(_delta_body(diff), targets) == expected
            E       AssertionError: assert -199 == 53
            tests/test_gardener_postrun.py: test_undercounted_hunk_truncation_recomputes_from_lenient
            1 failed

        The pair lists matched (one file, unchanged), so the pair-only check
        kept the truncated strict -199 and dropped +252. GREEN once the
        cross-check compares FULL per-file structure (pairs AND body line
        lists) and recomputes from lenient on ANY divergence."""
        targets = self._rules_target(postrun)
        rem = "R" * 200   # removal -> -(200 + 1)
        small = "s"       # small addition -> +(1 + 1)
        big = "A" * 125   # each dropped addition -> +(125 + 1); two of them
        diff = ("--- a/rules/foo.md\n"
                "+++ b/rules/foo.md\n"
                "@@ -1,2 +1,2 @@\n"
                " keep\n"
                f"-{rem}\n"
                f"+{small}\n"
                f"+{big}\n"
                f"+{big}\n")
        expected = -(len(rem) + 1) + (len(small) + 1) + 2 * (len(big) + 1)  # +53
        assert expected == 53
        assert postrun.compute_always_on_delta(_delta_body(diff), targets) == expected


class TestAlwaysOnBytesConsistency:
    """Spec 2b: the declared always_on_bytes must match the diff-computed act.

    RED-proof (drift-guard-tests.md): before the _always_on_bytes_violations
    wiring lands in validate_proposal, the sham proposal below validates clean
    and reaches the ledger — these tests are the red run; capture the output
    and keep it in this docstring at implementation time.

    Verified 2026-07-23, captured output (5 failed, 1 passed — the passing
    one is the honest-declaration positive case, which needs no gate to
    already pass):

        tests/test_gardener_postrun.py::TestAlwaysOnBytesConsistency::test_sham_negative_declaration_quarantined FAILED
        tests/test_gardener_postrun.py::TestAlwaysOnBytesConsistency::test_dishonest_zero_on_ab_relative_additive_diff_quarantined FAILED
        tests/test_gardener_postrun.py::TestAlwaysOnBytesConsistency::test_no_fence_skips_the_check FAILED
        tests/test_gardener_postrun.py::TestAlwaysOnBytesConsistency::test_non_integer_declared_with_fence_flagged FAILED
        tests/test_gardener_postrun.py::TestAlwaysOnBytesConsistency::test_unparseable_diff_skips_consistency FAILED

        >       assert summary["rejected"] == 1
        E       assert 0 == 1
        tests/test_gardener_postrun.py:1205: test_sham_negative_declaration_quarantined

        >       assert summary["rejected"] == 1
        E       assert 0 == 1
        tests/test_gardener_postrun.py:1221: test_dishonest_zero_on_ab_relative_additive_diff_quarantined

        >       assert postrun._always_on_bytes_violations(meta, "## Diff\\nprose\\n") == []
        E       AttributeError: module 'gardener_postrun_under_test' has no attribute '_always_on_bytes_violations'
        tests/test_gardener_postrun.py:1225: test_no_fence_skips_the_check

        (test_non_integer_declared_with_fence_flagged and
        test_unparseable_diff_skips_consistency failed the same way: no
        `_always_on_bytes_violations` attribute yet.)
        5 failed, 1 passed in 2.33s

    Restored after; GREEN with the gate present (see Step 5's full-file run)."""

    def _proposal(self, postrun, declared, diff, name="c1.md"):
        target = str(postrun.ALLOWED_TARGET_ROOTS[0] / "rules" / "foo.md")
        text = ("---\n"
                "id: c1\nrun_id: r-c\ncluster: c\nlane: digest\n"
                "evidence_kind: ops\nkind: rule-edit\n"
                f"always_on_bytes: {declared}\nflow_cost: none\nbase_rev: abc1234\n"
                "cost_justification: fixture — honest additive test case\n"
                f"targets: [{target}]\n"
                "expectation: e\ncheck_window_days: 7\nrevert: r\n"
                "---\n\n" + _delta_body(diff))
        path = postrun.PENDING_DIR / name
        path.write_text(text)
        return path

    _ADDITIVE = ("--- a/rules/foo.md\n"
                 "+++ b/rules/foo.md\n"
                 "@@ -1,0 +1,3 @@\n"
                 "+padding padding padding padding\n"
                 "+padding padding padding padding\n"
                 "+padding padding padding padding\n")

    # Same additive edit, but with a BARE (unnumbered) `@@` header — the form
    # the generator historically emits, and the one strict split_file_diffs
    # refuses. An EDIT (not a /dev/null creation) so the birth gate's lenient
    # fallback re-anchors it to `reanchorable` (a pass class) against the file
    # created in the test.
    _BARE_AT_ADDITIVE = ("--- a/rules/foo.md\n"
                         "+++ b/rules/foo.md\n"
                         "@@\n"
                         " beta\n"
                         "+padding padding padding padding\n")

    def test_bare_at_at_lying_zero_quarantined(self, postrun):
        """Reviewer's repro (Important): a bare-`@@` additive EDIT with a
        LYING always_on_bytes: 0. Before this fix, compute_always_on_delta
        parsed only with the strict split_file_diffs, which raises on the bare
        `@@`, so it returned None and _always_on_bytes_violations skipped its
        `computed is None` branch — while the birth gate's LENIENT fallback
        re-anchored the same diff cleanly and returned `reanchorable` (a pass
        class). So the sham proposal reached the ledger unchecked. The lenient
        fallback in compute_always_on_delta now computes the positive byte
        delta the actuator actually applies, so the mismatch is caught at
        validation.

        RED-proof (drift-guard-tests.md), executed 2026-07-23 against the
        unmodified strict-only gardener_postrun.py:

            >       assert summary["rejected"] == 1
            E       assert 0 == 1
            tests/test_gardener_postrun.py:1281: test_bare_at_at_lying_zero_quarantined
            1 failed

        The proposal had reached the ledger as a valid `proposal` event
        (apply_check=reanchorable). GREEN with the lenient fallback present."""
        (postrun.ALLOWED_TARGET_ROOTS[0] / "rules").mkdir(parents=True, exist_ok=True)
        (postrun.ALLOWED_TARGET_ROOTS[0] / "rules" / "foo.md").write_text(
            "alpha\nbeta\ngamma\n")
        self._proposal(postrun, declared=0, diff=self._BARE_AT_ADDITIVE)
        summary = postrun.process_run_artifacts("r-c", set())
        assert summary["rejected"] == 1
        assert any("always_on_bytes mismatch" in e.get("reasons", "")
                   for e in _ledger_events(postrun)
                   if e.get("type") == "proposal_rejected")

    def test_sham_negative_declaration_quarantined(self, postrun):
        """The declaration lies negative while the diff ADDS rule bytes —
        the exact 'gate passes while broken' input the gate must refuse."""
        self._proposal(postrun, declared=-500, diff=self._ADDITIVE)
        summary = postrun.process_run_artifacts("r-c", set())
        assert summary["rejected"] == 1
        assert any("always_on_bytes mismatch" in e.get("reasons", "")
                   for e in _ledger_events(postrun)
                   if e.get("type") == "proposal_rejected")

    def test_honest_declaration_within_tolerance_passes(self, postrun):
        computed = 3 * (len("padding padding padding padding") + 1)
        self._proposal(postrun, declared=computed, diff=self._ADDITIVE)
        summary = postrun.process_run_artifacts("r-c", set())
        assert summary["rejected"] == 0
        assert summary["proposals"] == 1

    def test_dishonest_zero_on_ab_relative_additive_diff_quarantined(self, postrun):
        """C1 direction (b): a/b headers with declared 0 must not sail through."""
        self._proposal(postrun, declared=0, diff=self._ADDITIVE)
        summary = postrun.process_run_artifacts("r-c", set())
        assert summary["rejected"] == 1

    def test_no_fence_skips_the_check(self, postrun):
        meta = {"always_on_bytes": "-500", "targets": ["x"]}
        assert postrun._always_on_bytes_violations(meta, "## Diff\nprose\n") == []

    def test_non_integer_declared_with_fence_flagged(self, postrun):
        meta = {"always_on_bytes": "lots", "targets": ["x"]}
        body = _delta_body("--- /dev/null\n+++ b/rules/foo.md\n@@ -0,0 +1 @@\n+x\n")
        assert any("not an integer" in v
                   for v in postrun._always_on_bytes_violations(meta, body))

    def test_unparseable_diff_skips_consistency(self, postrun):
        """None from the computer ⇒ the apply-check gate owns the failure."""
        meta = {"always_on_bytes": "-500", "targets": ["x"]}
        assert postrun._always_on_bytes_violations(meta, _delta_body("garbage")) == []

    def test_tolerance_boundary_16_passes(self, postrun):
        """M-3: |declared - computed| == exactly _BYTES_TOLERANCE (16) is the
        pass edge (the check is `> tolerance`, not `>=`). The _ADDITIVE diff
        computes +96 (three 31-char lines, each +(31 + 1)); declaring 80 leaves
        |80 - 96| == 16 → validates clean and reaches the ledger."""
        assert postrun._BYTES_TOLERANCE == 16
        computed = 3 * (len("padding padding padding padding") + 1)  # +96
        self._proposal(postrun, declared=computed - 16, diff=self._ADDITIVE)  # 80
        summary = postrun.process_run_artifacts("r-c", set())
        assert summary["rejected"] == 0 and summary["proposals"] == 1

    def test_tolerance_boundary_17_quarantined(self, postrun):
        """M-3: |declared - computed| == 17 crosses the tolerance and is
        quarantined — the same _ADDITIVE diff (+96) with declared 79 leaves
        |79 - 96| == 17 > 16."""
        computed = 3 * (len("padding padding padding padding") + 1)  # +96
        self._proposal(postrun, declared=computed - 17, diff=self._ADDITIVE)  # 79
        summary = postrun.process_run_artifacts("r-c", set())
        assert summary["rejected"] == 1
        assert any("always_on_bytes mismatch" in e.get("reasons", "")
                   for e in _ledger_events(postrun)
                   if e.get("type") == "proposal_rejected")

    def test_bytes_tolerance_config_widens_mismatch_tolerance(
            self, postrun, tmp_path, monkeypatch):
        """Spec I1: [gardener] bytes_tolerance (UNQUOTED int — a quoted value
        would pass even with the broken str-only reader) governs the 2b
        mismatch tolerance. |declared - computed| = 17 quarantines at the
        default floor (test_tolerance_boundary_17_quarantined) but passes at
        bytes_tolerance = 32."""
        cfg = tmp_path / "dockwright.toml"
        cfg.write_text("[gardener]\nbytes_tolerance = 32\n")
        monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
        computed = 3 * (len("padding padding padding padding") + 1)  # +96
        self._proposal(postrun, declared=computed - 17, diff=self._ADDITIVE)
        summary = postrun.process_run_artifacts("r-c", set())
        assert summary["rejected"] == 0 and summary["proposals"] == 1

    def test_scan_fallback_reads_unquoted_bytes_tolerance(self, postrun):
        assert postrun._scan_toml_str("[gardener]\nbytes_tolerance = 32\n",
                                      "gardener", "bytes_tolerance") == "32"


class TestCostJustificationCensor:
    """Censor (spec: docs/specs/gardener-token-censor.md, PRD A5): a diff
    computing positive always-on bytes beyond bytes_tolerance must declare a
    non-empty cost_justification, or the proposal is quarantined — same
    validation pass and quarantine machinery as the 2b consistency check.

    RED-proof (drift-guard-tests.md): captured 2026-07-28 against the
    pre-implementation _always_on_bytes_violations (no censor, single-reason
    early-return shape) — real run, `.venv/bin/python -m pytest
    "tests/test_gardener_postrun.py::TestCostJustificationCensor" -v`:

        tests/test_gardener_postrun.py::TestCostJustificationCensor::test_positive_undeclared_quarantined FAILED
        tests/test_gardener_postrun.py::TestCostJustificationCensor::test_positive_declared_passes PASSED
        tests/test_gardener_postrun.py::TestCostJustificationCensor::test_whitespace_justification_quarantined FAILED
        tests/test_gardener_postrun.py::TestCostJustificationCensor::test_negative_without_field_passes PASSED
        tests/test_gardener_postrun.py::TestCostJustificationCensor::test_negative_with_field_passes PASSED
        tests/test_gardener_postrun.py::TestCostJustificationCensor::test_noise_floor_16_passes_without_field PASSED
        tests/test_gardener_postrun.py::TestCostJustificationCensor::test_boundary_17_quarantined_without_field FAILED
        tests/test_gardener_postrun.py::TestCostJustificationCensor::test_no_fence_skips_censor PASSED
        tests/test_gardener_postrun.py::TestCostJustificationCensor::test_both_violations_reported_together FAILED
        tests/test_gardener_postrun.py::TestCostJustificationCensor::test_non_int_declared_unparseable_diff_still_flagged PASSED
        tests/test_gardener_postrun.py::TestCostJustificationCensor::test_frontier_lane_positive_undeclared_quarantined FAILED
        tests/test_gardener_postrun.py::TestCostJustificationCensor::test_censor_config_floor PASSED

        >       assert summary["rejected"] == 1
        E       assert 0 == 1
        test_positive_undeclared_quarantined

        >       assert summary["rejected"] == 1
        E       assert 0 == 1
        test_whitespace_justification_quarantined

        >       assert summary["rejected"] == 1
        E       assert 0 == 1
        test_boundary_17_quarantined_without_field

        >       assert any("not an integer" in r and "cost_justification" in r
                            for r in reasons)
        E       assert False
        test_both_violations_reported_together

        >       assert summary["rejected"] == 1
        E       assert 0 == 1
        test_frontier_lane_positive_undeclared_quarantined

        5 failed, 7 passed in 0.35s

    test_non_int_declared_unparseable_diff_still_flagged already PASSES
    pre-change (old code's early-return still reports the non-integer
    violation) — that is its regression-pin role, pinning that the
    restructure preserves the old semantics rather than breaking it.
    test_censor_config_floor also already PASSES pre-change: raising
    bytes_tolerance to 32 makes the *existing* 2b mismatch check pass too
    (declared 17 == computed 17), so there is nothing for the not-yet-built
    censor to quarantine in this input regardless — it is a genuine
    assertion of the post-change behavior, just not a RED case on its own.

    Restored after; GREEN with the censor present (see Step 5's full-file
    run)."""

    _JUSTIFIED = "cost_justification: correction-labeled; no cheaper home\n"

    def _proposal(self, postrun, declared, diff, justification_line="",
                  lane="digest", name="z1.md"):
        target = str(postrun.ALLOWED_TARGET_ROOTS[0] / "rules" / "foo.md")
        text = ("---\n"
                f"id: z1\nrun_id: r-z\ncluster: c\nlane: {lane}\n"
                "evidence_kind: ops\nkind: rule-edit\n"
                f"always_on_bytes: {declared}\nflow_cost: none\nbase_rev: abc1234\n"
                + justification_line +
                f"targets: [{target}]\n"
                "expectation: e\ncheck_window_days: 7\nrevert: r\n"
                "---\n\n" + _delta_body(diff))
        (postrun.PENDING_DIR / name).write_text(text)

    _ADD96 = TestAlwaysOnBytesConsistency._ADDITIVE          # computes +96
    _ADD17 = ("--- a/rules/foo.md\n"
              "+++ b/rules/foo.md\n"
              "@@ -1,0 +1,1 @@\n"
              "+" + "x" * 16 + "\n")                         # computes +17
    _ADD16 = ("--- a/rules/foo.md\n"
              "+++ b/rules/foo.md\n"
              "@@ -1,0 +1,1 @@\n"
              "+" + "x" * 15 + "\n")                         # computes +16
    _REMOVE = ("--- a/rules/foo.md\n"
               "+++ b/rules/foo.md\n"
               "@@ -1,2 +1,1 @@\n"
               "-" + "y" * 40 + "\n"
               " keep\n")                                    # computes -41

    def test_positive_undeclared_quarantined(self, postrun):
        self._proposal(postrun, declared=96, diff=self._ADD96)
        summary = postrun.process_run_artifacts("r-z", set())
        assert summary["rejected"] == 1
        assert any("cost_justification" in e.get("reasons", "")
                   for e in _ledger_events(postrun)
                   if e.get("type") == "proposal_rejected")

    def test_positive_declared_passes(self, postrun):
        self._proposal(postrun, declared=96, diff=self._ADD96,
                       justification_line=self._JUSTIFIED)
        summary = postrun.process_run_artifacts("r-z", set())
        assert summary["rejected"] == 0 and summary["proposals"] == 1

    def test_whitespace_justification_quarantined(self, postrun):
        self._proposal(postrun, declared=96, diff=self._ADD96,
                       justification_line='cost_justification: "  "\n')
        summary = postrun.process_run_artifacts("r-z", set())
        assert summary["rejected"] == 1

    def test_negative_without_field_passes(self, postrun):
        (postrun.ALLOWED_TARGET_ROOTS[0] / "rules").mkdir(
            parents=True, exist_ok=True)
        (postrun.ALLOWED_TARGET_ROOTS[0] / "rules" / "foo.md").write_text(
            "y" * 40 + "\nkeep\n")
        self._proposal(postrun, declared=-41, diff=self._REMOVE)
        summary = postrun.process_run_artifacts("r-z", set())
        assert summary["rejected"] == 0 and summary["proposals"] == 1

    def test_negative_with_field_passes(self, postrun):
        """The field is allowed on any proposal, required only on positive."""
        (postrun.ALLOWED_TARGET_ROOTS[0] / "rules").mkdir(
            parents=True, exist_ok=True)
        (postrun.ALLOWED_TARGET_ROOTS[0] / "rules" / "foo.md").write_text(
            "y" * 40 + "\nkeep\n")
        self._proposal(postrun, declared=-41, diff=self._REMOVE,
                       justification_line=self._JUSTIFIED)
        summary = postrun.process_run_artifacts("r-z", set())
        assert summary["rejected"] == 0 and summary["proposals"] == 1

    def test_noise_floor_16_passes_without_field(self, postrun):
        self._proposal(postrun, declared=16, diff=self._ADD16)
        summary = postrun.process_run_artifacts("r-z", set())
        assert summary["rejected"] == 0 and summary["proposals"] == 1

    def test_boundary_17_quarantined_without_field(self, postrun):
        self._proposal(postrun, declared=17, diff=self._ADD17)
        summary = postrun.process_run_artifacts("r-z", set())
        assert summary["rejected"] == 1

    def test_no_fence_skips_censor(self, postrun):
        meta = {"always_on_bytes": "500", "targets": ["x"]}
        assert postrun._always_on_bytes_violations(meta, "## Diff\nprose\n") == []

    def test_both_violations_reported_together(self, postrun):
        """M1 restructure RED-proof: non-int declared AND an undeclared
        positive delta must land as TWO reasons on ONE quarantine event
        (the old early-return shape could only ever report one)."""
        self._proposal(postrun, declared='"lots"', diff=self._ADD96)
        summary = postrun.process_run_artifacts("r-z", set())
        assert summary["rejected"] == 1
        reasons = [e.get("reasons", "") for e in _ledger_events(postrun)
                   if e.get("type") == "proposal_rejected"]
        assert any("not an integer" in r and "cost_justification" in r
                   for r in reasons)

    def test_non_int_declared_unparseable_diff_still_flagged(self, postrun):
        """Restructure must preserve the old semantics: non-int declared with
        an uncomputable diff still reports the non-integer violation."""
        meta = {"always_on_bytes": "lots", "targets": ["x"]}
        assert any("not an integer" in v
                   for v in postrun._always_on_bytes_violations(
                       meta, _delta_body("garbage")))

    def test_frontier_lane_positive_undeclared_quarantined(self, postrun):
        """Lane-uniform: the censor is contract mechanics, not digest policy."""
        self._proposal(postrun, declared=96, diff=self._ADD96, lane="frontier")
        summary = postrun.process_run_artifacts("r-z", set(), lane="frontier")
        assert summary["rejected"] == 1

    def test_censor_config_floor(self, postrun, tmp_path, monkeypatch):
        """bytes_tolerance=32 also lifts the censor floor: +17 B undeclared
        passes (shared key, spec I1)."""
        cfg = tmp_path / "dockwright.toml"
        cfg.write_text("[gardener]\nbytes_tolerance = 32\n")
        monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
        self._proposal(postrun, declared=17, diff=self._ADD17)
        summary = postrun.process_run_artifacts("r-z", set())
        assert summary["rejected"] == 0 and summary["proposals"] == 1

    def test_list_valued_whitespace_justification_quarantined(self, postrun):
        """A bracket-wrapped whitespace value must not evade the emptiness
        filter (parse_frontmatter coerces [..] to a list)."""
        self._proposal(postrun, declared=96, diff=self._ADD96,
                       justification_line='cost_justification: ["  "]\n')
        summary = postrun.process_run_artifacts("r-z", set())
        assert summary["rejected"] == 1

    def test_zero_delta_without_field_passes(self, postrun):
        """Computed exactly 0 (equal add/remove) never requires the field."""
        (postrun.ALLOWED_TARGET_ROOTS[0] / "rules").mkdir(
            parents=True, exist_ok=True)
        (postrun.ALLOWED_TARGET_ROOTS[0] / "rules" / "foo.md").write_text(
            "y" * 10 + "\nkeep\n")
        diff = ("--- a/rules/foo.md\n"
                "+++ b/rules/foo.md\n"
                "@@ -1,2 +1,2 @@\n"
                "-" + "y" * 10 + "\n"
                "+" + "z" * 10 + "\n"
                " keep\n")
        self._proposal(postrun, declared=0, diff=diff)
        summary = postrun.process_run_artifacts("r-z", set())
        assert summary["rejected"] == 0 and summary["proposals"] == 1


# ---- back-pressure gate (spec 2c/2d) ---------------------------------------


def _negative_proposal_text(postrun, pid, target_rel="rules/big.md"):
    """A genuinely-applyable net-negative proposal: the target file exists
    with removable content and the declared bytes match the computed act."""
    root = postrun.ALLOWED_TARGET_ROOTS[0]
    target = root / target_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = ["x" * 60 for _ in range(4)]
    target.write_text("\n".join(lines) + "\nkeep\n")
    removed = sum(len(l) + 1 for l in lines)
    diff_lines = "\n".join("-" + l for l in lines)
    diff = (f"--- a/{target_rel}\n+++ b/{target_rel}\n"
            f"@@ -1,5 +1,1 @@\n{diff_lines}\n keep\n")
    return ("---\n"
            f"id: {pid}\nrun_id: r-x\ncluster: evict\nlane: digest\n"
            "evidence_kind: ops\nkind: rule-edit\n"
            f"always_on_bytes: -{removed}\nflow_cost: none\nbase_rev: abc1234\n"
            f"targets: [{target}]\n"
            "expectation: e\ncheck_window_days: 7\nrevert: r\n"
            "---\n\n" + _delta_body(diff))


def _additive_proposal_text(postrun, pid):
    root = postrun.ALLOWED_TARGET_ROOTS[0]
    target = root / "rules" / f"{pid}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("alpha\n")
    line = "padding padding padding padding padding"
    diff = (f"--- a/rules/{pid}.md\n+++ b/rules/{pid}.md\n"
            "@@ -1,1 +1,2 @@\n alpha\n"
            f"+{line}\n")
    return ("---\n"
            f"id: {pid}\nrun_id: r-x\ncluster: add\nlane: digest\n"
            "evidence_kind: ops\nkind: rule-edit\n"
            f"always_on_bytes: {len(line) + 1}\nflow_cost: none\nbase_rev: abc1234\n"
            "cost_justification: fixture — additive backpressure case\n"
            f"targets: [{target}]\n"
            "expectation: e\ncheck_window_days: 7\nrevert: r\n"
            "---\n\n" + _delta_body(diff))


def _negative_delta_proposal_text(postrun, pid, n_lines, declared, target_rel=None):
    """A genuinely-applyable negative proposal removing n_lines of 39-char
    content (computed delta = -40 * n_lines: each removed line contributes
    -(39 + 1) bytes). `declared` is set INDEPENDENTLY of the computed value
    so callers can straddle a threshold or pin computed-vs-declared
    precisely, while staying inside the Task-2 consistency tolerance
    (|declared - computed| <= 16) so the proposal reaches the ledger rather
    than being quarantined before qualification is ever evaluated."""
    target_rel = target_rel or f"rules/{pid}.md"
    root = postrun.ALLOWED_TARGET_ROOTS[0]
    target = root / target_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = ["x" * 39 for _ in range(n_lines)]
    target.write_text("\n".join(lines) + "\nkeep\n")
    diff_lines = "\n".join("-" + l for l in lines)
    diff = (f"--- a/{target_rel}\n+++ b/{target_rel}\n"
            f"@@ -1,{n_lines + 1} +1,1 @@\n{diff_lines}\n keep\n")
    return ("---\n"
            f"id: {pid}\nrun_id: r-x\ncluster: evict\nlane: digest\n"
            "evidence_kind: ops\nkind: rule-edit\n"
            f"always_on_bytes: {declared}\nflow_cost: none\nbase_rev: abc1234\n"
            f"targets: [{target}]\n"
            "expectation: e\ncheck_window_days: 7\nrevert: r\n"
            "---\n\n" + _delta_body(diff))


def _bp_events(postrun):
    return [e for e in _ledger_events(postrun) if e.get("type") == "backpressure"]


class TestBackpressureGate:
    """Spec 2c: consecutive-miss streak over proposal-bearing digest runs.

    RED-proof (drift-guard-tests.md), executed 2026-07-23 against
    gardener_postrun.py before config_toml_int / _backpressure_stats /
    record_backpressure / the summary-line wiring landed (8 failed, 2
    passed — the 2 passing cases, frontier-lane-appends-no-event and the
    unquoted-TOML-scan fallback, need no gate to already pass):

        FAILED tests/test_gardener_postrun.py::TestBackpressureGate::test_heartbeat_even_with_zero_artifacts
        FAILED tests/test_gardener_postrun.py::TestBackpressureGate::test_two_consecutive_additive_runs_flag_violation
        FAILED tests/test_gardener_postrun.py::TestBackpressureGate::test_negative_proposal_resets_streak
        FAILED tests/test_gardener_postrun.py::TestBackpressureGate::test_zero_proposal_run_is_neutral
        FAILED tests/test_gardener_postrun.py::TestBackpressureGate::test_interleaved_rerun_is_idempotent
        FAILED tests/test_gardener_postrun.py::TestBackpressureGate::test_sham_negative_declaration_earns_no_credit
        FAILED tests/test_gardener_postrun.py::TestBackpressureGate::test_config_override_unquoted_int
        FAILED tests/test_gardener_postrun.py::TestBackpressureGate::test_skipped_env_earns_no_credit

        >       events = _bp_events(postrun)
        >       assert len(events) == 1
        E       assert 0 == 1
        tests/test_gardener_postrun.py: test_heartbeat_even_with_zero_artifacts
        (no `backpressure` events at all — the postrun CLI never called
        record_backpressure)

        >       assert postrun.config_toml_int("gardener", "backpressure_every", 2) == 3
        E       AttributeError: module 'gardener_postrun_under_test' has no
        attribute 'config_toml_int'. Did you mean: 'config_toml_str'?
        tests/test_gardener_postrun.py: test_config_override_unquoted_int

        8 failed, 2 passed in 0.68s

    Restored after; GREEN with config_toml_int, _backpressure_stats,
    record_backpressure, and the postrun summary-line wiring present (see
    Step 4's full-file run).

    Delete-one-line mutation proofs (Task 6 drift-guard sweep,
    drift-guard-tests.md), each executed 2026-07-23 on a scratch copy of
    gardener_postrun.py and restored via `git checkout --` after each (md5
    match against the pre-mutation copy; `git status --porcelain` then shows
    only this test file changed):

    (a) Violation branch disabled — `violation = streak >= every` in
        record_backpressure replaced by `violation = False`. ONLY
        test_two_consecutive_additive_runs_flag_violation caught it (1 failed,
        11 passed): the streak still counted [1, 2], but the flag never fired:

            >       assert [e["violation"] for e in events] == [False, True]
            E       assert [False, False] == [False, True]
            E         At index 1 diff: False != True
            tests/test_gardener_postrun.py:1453: AssertionError
            FAILED tests/test_gardener_postrun.py::TestBackpressureGate::test_two_consecutive_additive_runs_flag_violation
            1 failed, 11 passed

    (b) Heartbeat unwired — the `record_backpressure` call block in main()
        (the `if args.lane in ("", "digest"):` postrun branch) commented out,
        so NO `backpressure` event is ever appended. This flipped
        test_heartbeat_even_with_zero_artifacts and every other test that reads
        a backpressure event red — 10 failed, 2 passed
        (test_frontier_lane_appends_no_event and
        test_scan_fallback_reads_unquoted_int need no event to pass). The
        heartbeat test run in isolation:

            >       assert len(events) == 1
            E       assert 0 == 1
            E        +  where 0 = len([])
            tests/test_gardener_postrun.py:1442: AssertionError
            FAILED tests/test_gardener_postrun.py::TestBackpressureGate::test_heartbeat_even_with_zero_artifacts

        GREEN with the whole gate restored (see Step 4's full-file run)."""

    def _postrun_cli(self, postrun, run_id, lane=""):
        args = ["postrun", "--run-id", run_id]
        if lane:
            args += ["--lane", lane]
        return postrun.main(args)

    def test_heartbeat_even_with_zero_artifacts(self, postrun):
        self._postrun_cli(postrun, "r0")
        events = _bp_events(postrun)
        assert len(events) == 1
        assert events[0]["proposals"] == 0 and events[0]["negative"] == 0
        assert events[0]["violation"] is False

    def test_two_consecutive_additive_runs_flag_violation(self, postrun, capsys):
        (postrun.PENDING_DIR / "a1.md").write_text(_additive_proposal_text(postrun, "a1"))
        self._postrun_cli(postrun, "r1")
        (postrun.PENDING_DIR / "a2.md").write_text(_additive_proposal_text(postrun, "a2"))
        self._postrun_cli(postrun, "r2")
        events = _bp_events(postrun)
        assert [e["streak"] for e in events] == [1, 2]
        assert [e["violation"] for e in events] == [False, True]
        captured = capsys.readouterr()
        assert "back-pressure violation" in captured.err
        assert "VIOLATION" in captured.out

    def test_negative_proposal_resets_streak(self, postrun):
        (postrun.PENDING_DIR / "a1.md").write_text(_additive_proposal_text(postrun, "a1"))
        self._postrun_cli(postrun, "r1")
        (postrun.PENDING_DIR / "n1.md").write_text(_negative_proposal_text(postrun, "n1"))
        self._postrun_cli(postrun, "r2")
        events = _bp_events(postrun)
        assert events[-1]["negative"] == 1
        assert events[-1]["streak"] == 0 and events[-1]["violation"] is False

    def test_zero_proposal_run_is_neutral(self, postrun):
        (postrun.PENDING_DIR / "a1.md").write_text(_additive_proposal_text(postrun, "a1"))
        self._postrun_cli(postrun, "r1")
        self._postrun_cli(postrun, "r-empty")
        (postrun.PENDING_DIR / "a2.md").write_text(_additive_proposal_text(postrun, "a2"))
        self._postrun_cli(postrun, "r2")
        assert [e["streak"] for e in _bp_events(postrun)] == [1, 1, 2]

    def test_interleaved_rerun_is_idempotent(self, postrun):
        """I3: A → B → A again; the second A must not append (whole-ledger
        run_id dedup, not last-event)."""
        (postrun.PENDING_DIR / "a1.md").write_text(_additive_proposal_text(postrun, "a1"))
        self._postrun_cli(postrun, "rA")
        self._postrun_cli(postrun, "rB")
        self._postrun_cli(postrun, "rA")
        events = _bp_events(postrun)
        assert [e["run_id"] for e in events] == ["rA", "rB"]

    def test_frontier_lane_appends_no_event(self, postrun):
        self._postrun_cli(postrun, "rf", lane="frontier")
        assert _bp_events(postrun) == []

    def test_sham_negative_declaration_earns_no_credit(self, postrun):
        """Validation-ORDERING guard, not a computed-vs-declared guard: this
        lying-negative additive proposal (declares -400 while the diff only
        ADDS bytes, Δ=440) is caught by the Task-2 consistency check
        (|Δ| > 16) and quarantined BEFORE it ever reaches the
        qualify-for-backpressure-credit branch — it counts as ZERO proposals
        AND zero negative, and the streak is untouched. Because the Task-2
        gate fires first, this test cannot distinguish 'qualification reads
        the computed delta' from 'qualification reads the declaration' — a
        declared-value swap in the qualify branch would ALSO pass this test
        (the proposal never reaches that branch either way). That
        computed-vs-declared guard is
        test_computed_not_declared_governs_qualification below, whose
        declared/computed straddle the -128 min-bytes threshold while
        staying INSIDE the Task-2 tolerance so the proposal actually reaches
        qualification."""
        text = _additive_proposal_text(postrun, "sham").replace(
            "always_on_bytes: 40", "always_on_bytes: -400")
        (postrun.PENDING_DIR / "sham.md").write_text(text)
        self._postrun_cli(postrun, "r1")
        events = _bp_events(postrun)
        assert events[0]["proposals"] == 0 and events[0]["negative"] == 0

    def test_config_override_unquoted_int(self, postrun, tmp_path, monkeypatch):
        """I2: the natural UNQUOTED TOML int must be honored (a quoted value
        would pass even with the broken str-only reader)."""
        cfg = tmp_path / "dockwright.toml"
        cfg.write_text("[gardener]\nbackpressure_every = 3\n")
        monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
        assert postrun.config_toml_int("gardener", "backpressure_every", 2) == 3
        for i, pid in enumerate(["a1", "a2"]):
            (postrun.PENDING_DIR / f"{pid}.md").write_text(_additive_proposal_text(postrun, pid))
            self._postrun_cli(postrun, f"r{i}")
        assert [e["violation"] for e in _bp_events(postrun)] == [False, False]

    def test_scan_fallback_reads_unquoted_int(self, postrun):
        assert postrun._scan_toml_str("[gardener]\nbackpressure_every = 3\n",
                                      "gardener", "backpressure_every") == "3"

    def test_skipped_env_earns_no_credit(self, postrun, monkeypatch):
        """I5: an env-skipped apply-check must not qualify a negative diff."""
        monkeypatch.setattr(postrun, "_apply_check",
                            lambda path, body: ("skipped-env", "boom"))
        (postrun.PENDING_DIR / "n1.md").write_text(_negative_proposal_text(postrun, "n1"))
        self._postrun_cli(postrun, "r1")
        events = _bp_events(postrun)
        assert events[0]["proposals"] == 1 and events[0]["negative"] == 0

    def test_computed_not_declared_governs_qualification(self, postrun):
        """Straddle test: the computed delta (three 39-char lines removed =
        -40*3 = -120) and the declared value (-130) straddle the -128
        min-bytes threshold, while |declared - computed| = 10 stays inside
        the Task-2 consistency tolerance (16) — the proposal reaches the
        ledger as a valid `proposal` event, not a Task-2 quarantine (unlike
        test_sham_negative_declaration_earns_no_credit above). Computed -120
        is LESS negative than -128, so it must NOT qualify as a
        negative-byte proposal — qualification must read the
        ACTUATOR-COMPUTED delta, never the declaration.

        Mutation-red proof (drift-guard-tests.md): with
        `delta = compute_always_on_delta(body, targets)` in
        process_run_artifacts replaced by the DECLARED value (-130), this
        test goes red, because declared -130 <= -128 DOES qualify.

        RED-proof, executed 2026-07-23 against a scratch copy of
        gardener_postrun.py with that one line mutated (`delta =
        int(str(meta.get("always_on_bytes", "0")).strip())` in place of the
        `compute_always_on_delta` call):

            gardener-postrun: proposals=1 checks=0 rejected=0 skipped_env=0 backpressure=1/1 streak=0
            >       assert events[0]["negative"] == 0
            E       assert 1 == 0
            tests/test_gardener_postrun.py:1571: AssertionError
            FAILED tests/test_gardener_postrun.py::TestBackpressureGate::test_computed_not_declared_governs_qualification
            1 failed, 11 passed in 0.34s

        (all 11 OTHER TestBackpressureGate tests, incl.
        test_below_min_bytes_threshold_does_not_qualify, stayed green under
        this mutation — only the straddle test caught it.) Restored after
        (verified via `git diff --stat` showing only this test file
        changed, and an md5 match against the pre-mutation copy of
        gardener_postrun.py); GREEN with the real computed delta governing
        qualification (see Step 4's full-file run)."""
        text = _negative_delta_proposal_text(postrun, "straddle", n_lines=3, declared=-130)
        (postrun.PENDING_DIR / "straddle.md").write_text(text)
        self._postrun_cli(postrun, "r1")
        events = _bp_events(postrun)
        assert events[0]["proposals"] == 1
        assert events[0]["negative"] == 0

    def test_swallow_declared_strict_value_earns_no_credit(self, postrun):
        """End-to-end (final-review IMPORTANT): the overcounted-first-hunk
        swallow from TestComputeAlwaysOnDelta, run through the full postrun CLI.
        A proposal declares always_on_bytes: -153 — the value the strict swallow
        MIS-computes — over a diff that TRULY grows the always-on corpus by +47
        (bar removes -153, baz adds +200). Before the cross-check, strict
        computed -153, matched the -153 declaration (Δ=0, inside tolerance), the
        birth gate re-anchored it `reanchorable` (a qualify class), and -153 <=
        -128 earned NEGATIVE credit → the streak reset on a corpus-GROWING diff.
        The cross-check now recomputes +47, so the declaration MISMATCHES
        (|-153 - 47| = 200 > 16) → quarantined at validation, never enqueued,
        never credited.

        RED-proof (drift-guard-tests.md), executed 2026-07-23 against the
        strict-only compute_always_on_delta:

            >       assert events[0]["proposals"] == 0
            E       assert 1 == 0
            tests/test_gardener_postrun.py: test_swallow_declared_strict_value_earns_no_credit
            1 failed

        (the pre-fix run enqueued it as proposals=1/negative=1 — birth gate
        reanchorable, strict -153 <= -128 credited). GREEN with the lenient
        cross-check present."""
        root = postrun.ALLOWED_TARGET_ROOTS[0]
        (root / "rules").mkdir(parents=True, exist_ok=True)
        rem = "R" * 50
        add = "A" * 49
        # target files so the birth gate would re-anchor cleanly PRE-fix (the
        # RED must earn credit, not be quarantined for a missing file)
        (root / "rules" / "bar.md").write_text("ctx1\nctx2\n" + f"{rem}\n{rem}\n{rem}\n")
        (root / "rules" / "baz.md").write_text("keep\n")
        bar = str(root / "rules" / "bar.md")
        baz = str(root / "rules" / "baz.md")
        diff = ("--- a/rules/bar.md\n"
                "+++ b/rules/bar.md\n"
                "@@ -1,7 +1,4 @@\n"
                " ctx1\n"
                " ctx2\n"
                f"-{rem}\n"
                f"-{rem}\n"
                f"-{rem}\n"
                "--- a/rules/baz.md\n"
                "+++ b/rules/baz.md\n"
                "@@ -1,1 +1,5 @@\n"
                " keep\n"
                f"+{add}\n"
                f"+{add}\n"
                f"+{add}\n"
                f"+{add}\n")
        text = ("---\n"
                "id: sw1\nrun_id: r-sw\ncluster: swallow\nlane: digest\n"
                "evidence_kind: ops\nkind: rule-edit\n"
                "always_on_bytes: -153\nflow_cost: none\nbase_rev: abc1234\n"
                f"targets: [{bar}, {baz}]\n"
                "expectation: e\ncheck_window_days: 7\nrevert: r\n"
                "---\n\n" + _delta_body(diff))
        (postrun.PENDING_DIR / "sw1.md").write_text(text)
        self._postrun_cli(postrun, "r1")
        events = _bp_events(postrun)
        assert events[0]["proposals"] == 0   # quarantined at validation, not enqueued
        assert events[0]["negative"] == 0    # never earns negative credit
        assert any("always_on_bytes mismatch" in e.get("reasons", "")
                   for e in _ledger_events(postrun)
                   if e.get("type") == "proposal_rejected")

    def test_undercount_declared_strict_value_earns_no_credit(self, postrun):
        """End-to-end sibling (IMPORTANT-2): an intra-file UNDERCOUNT
        truncation, run through the full postrun CLI with a prior run holding a
        streak so 'streak untouched' is observable. Run 1 is an honest additive
        proposal → streak 1. Run 2's proposal declares always_on_bytes: -199 —
        the truncated strict value — over a diff that TRULY grows the corpus by
        +53. Pre-fix the pair-only cross-check kept the truncated strict -199,
        it matched the declaration (Δ=0), the birth gate found it CLEAN (the
        truncated patch is internally consistent and applies), and -199 <= -128
        earned NEGATIVE credit → the streak RESET on a corpus-growing diff. The
        full-structure cross-check recomputes +53, so the declaration MISMATCHES
        (|-199 - 53| = 252 > 16) → quarantined at validation, never enqueued,
        never credited, streak untouched at 1.

        RED-proof (drift-guard-tests.md), executed 2026-07-23 against the
        pair-list-only cross-check:

            >       assert [e["streak"] for e in events] == [1, 1]
            E       assert [1, 0] == [1, 1]
            E         At index 1 diff: 0 != 1
            tests/test_gardener_postrun.py: test_undercount_declared_strict_value_earns_no_credit
            1 failed

        (run 2 earned negative credit pre-fix — negative=1 — resetting the
        streak to 0.) GREEN with the full per-file structure comparison."""
        root = postrun.ALLOWED_TARGET_ROOTS[0]
        (root / "rules").mkdir(parents=True, exist_ok=True)
        # run 1: honest additive proposal → streak 1
        (postrun.PENDING_DIR / "a1.md").write_text(_additive_proposal_text(postrun, "a1"))
        self._postrun_cli(postrun, "r1")
        # run 2: the undercount-truncation proposal declaring the mis-parsed strict value
        rem = "R" * 200
        small = "s"
        big = "A" * 125
        foo = root / "rules" / "foo.md"
        # file holds the old block [keep, REM] so the birth gate would anchor PRE-fix
        foo.write_text("keep\n" + rem + "\n")
        diff = ("--- a/rules/foo.md\n"
                "+++ b/rules/foo.md\n"
                "@@ -1,2 +1,2 @@\n"
                " keep\n"
                f"-{rem}\n"
                f"+{small}\n"
                f"+{big}\n"
                f"+{big}\n")
        text = ("---\n"
                "id: uc1\nrun_id: r-uc\ncluster: undercount\nlane: digest\n"
                "evidence_kind: ops\nkind: rule-edit\n"
                "always_on_bytes: -199\nflow_cost: none\nbase_rev: abc1234\n"
                f"targets: [{foo}]\n"
                "expectation: e\ncheck_window_days: 7\nrevert: r\n"
                "---\n\n" + _delta_body(diff))
        (postrun.PENDING_DIR / "uc1.md").write_text(text)
        self._postrun_cli(postrun, "r2")
        events = _bp_events(postrun)
        assert [e["streak"] for e in events] == [1, 1]   # run 2 quarantined → streak untouched
        assert events[-1]["proposals"] == 0 and events[-1]["negative"] == 0
        assert any("always_on_bytes mismatch" in e.get("reasons", "")
                   for e in _ledger_events(postrun)
                   if e.get("type") == "proposal_rejected")

    def test_below_min_bytes_threshold_does_not_qualify(self, postrun):
        """Threshold-band test: pins the -128 BACKPRESSURE_MIN_BYTES_DEFAULT
        floor itself, not merely 'negative is negative'. An HONEST proposal
        (declared == computed == -40, one 39-char line removed) is a genuine
        negative-byte edit, but -40 does not clear the -128 min-bytes floor
        — it must not qualify.

        Mutation-red proof: relaxing the qualify condition from
        `delta <= -min_bytes` to `delta < 0` in process_run_artifacts flips
        this red, because -40 < 0.

        RED-proof, executed 2026-07-23 against a scratch copy of
        gardener_postrun.py with that condition weakened to
        `if delta is not None and delta < 0:`:

            gardener-postrun: proposals=1 checks=0 rejected=0 skipped_env=0 backpressure=1/1 streak=0
            >       assert events[0]["negative"] == 0
            E       assert 1 == 0
            tests/test_gardener_postrun.py:1601: AssertionError
            FAILED tests/test_gardener_postrun.py::TestBackpressureGate::test_computed_not_declared_governs_qualification
            FAILED tests/test_gardener_postrun.py::TestBackpressureGate::test_below_min_bytes_threshold_does_not_qualify
            2 failed, 10 passed in 0.30s

        (this mutation ALSO flips test_computed_not_declared_governs_qualification
        red, since its computed -120 < 0 too — expected: it is still a
        negative delta, just one that must stay unqualified below the -128
        floor. The two tests are non-redundant: mutation 1
        (declared-value swap) flips ONLY the straddle test; mutation 2
        (threshold relaxation) flips BOTH.) Restored after (verified via
        `git diff --stat` showing only this test file changed, and an md5
        match against the pre-mutation copy of gardener_postrun.py); GREEN
        with the -128 floor enforced (see Step 4's full-file run)."""
        text = _negative_delta_proposal_text(postrun, "small", n_lines=1, declared=-40)
        (postrun.PENDING_DIR / "small.md").write_text(text)
        self._postrun_cli(postrun, "r1")
        events = _bp_events(postrun)
        assert events[0]["proposals"] == 1
        assert events[0]["negative"] == 0


class TestFlowCost:
    """Spec: docs/specs/gardener-flow-cost.md."""

    def _meta(self, value=None):
        meta = {"flow_cost": value} if value is not None else {}
        return meta

    def test_none_passes(self, postrun):
        assert postrun._flow_cost_violations(self._meta("none")) == []

    def test_adds_with_note_passes(self, postrun):
        assert postrun._flow_cost_violations(
            self._meta("adds — one re-derivation per review round")) == []

    def test_removes_with_note_passes(self, postrun):
        assert postrun._flow_cost_violations(
            self._meta("removes — drops the per-round re-derivation")) == []

    def test_plain_hyphen_separator_passes(self, postrun):
        assert postrun._flow_cost_violations(
            self._meta("adds - one extra assertion per test run")) == []

    def test_colon_separator_passes(self, postrun):
        assert postrun._flow_cost_violations(
            self._meta("adds: one disclosure line per PR")) == []

    def test_comma_and_en_dash_separators_pass(self, postrun):
        assert postrun._flow_cost_violations(
            self._meta("adds, one clause per PR")) == []
        assert postrun._flow_cost_violations(
            self._meta("adds\u2013one clause per PR")) == []

    def test_markup_and_trailing_punctuation_are_stripped(self, postrun):
        """The skill renders these values backticked, so a drafter copying
        the rendered form must not lose the proposal to a quarantine."""
        for value in ("`none`", "**none**", "none.", "'adds' - one per round"):
            assert postrun._flow_cost_violations(self._meta(value)) == [], value

    def test_bracketed_value_does_not_raise(self, postrun):
        """parse_frontmatter turns `[none]` into a LIST; an uncoerced list
        would raise here and abort validation for the whole run."""
        assert postrun._flow_cost_violations(self._meta(["none"])) == []

    def test_absent_field_is_a_violation(self, postrun):
        violations = postrun._flow_cost_violations(self._meta())
        assert any("flow_cost" in v for v in violations)

    def test_whitespace_only_is_a_violation(self, postrun):
        violations = postrun._flow_cost_violations(self._meta("   "))
        assert any("flow_cost" in v for v in violations)

    def test_unknown_verdict_is_a_violation(self, postrun):
        violations = postrun._flow_cost_violations(self._meta("maybe — who knows"))
        assert any("adds" in v and "removes" in v for v in violations)

    def test_adds_without_note_is_a_violation(self, postrun):
        violations = postrun._flow_cost_violations(self._meta("adds"))
        assert any("one-line note" in v for v in violations)

    def test_removes_without_note_is_a_violation(self, postrun):
        violations = postrun._flow_cost_violations(self._meta("removes —"))
        assert any("one-line note" in v for v in violations)

    def test_validate_proposal_reports_it(self, postrun):
        """The check runs inside the normal validation pass, so a proposal
        missing the field is quarantined by the existing machinery."""
        meta = {"id": "p1", "run_id": "r1", "cluster": "c", "lane": "digest",
                "targets": [str(postrun.HOME / ".claude" / "rules" / "x.md")],
                "kind": "rule-edit", "evidence_kind": "ops",
                "always_on_bytes": "0", "base_rev": "abc1234",
                "expectation": "e", "check_window_days": "7", "revert": "r"}
        violations = postrun.validate_proposal(meta, "## Evidence\n## Diff\nprose\n")
        assert any("flow_cost" in v for v in violations)

    def test_frontier_lane_is_checked_too(self, postrun):
        """Lane-uniform, like the always_on_bytes consistency check."""
        meta = {"id": "f1", "run_id": "r1", "cluster": "c", "lane": "frontier",
                "targets": [str(postrun.HOME / ".claude" / "rules" / "x.md")],
                "kind": "rule-edit", "evidence_kind": "external",
                "always_on_bytes": "0", "base_rev": "abc1234",
                "expectation": "e", "check_window_days": "14", "revert": "r"}
        violations = postrun.validate_proposal(meta, "## Evidence\n## Diff\nprose\n")
        assert any("flow_cost" in v for v in violations)
