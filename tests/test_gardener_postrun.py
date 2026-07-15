import importlib.util
import json
import os
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
POSTRUN_PATH = REPO_ROOT / "deploy" / "scripts" / "gardener_postrun.py"


def _load_postrun():
    spec = importlib.util.spec_from_file_location("gardener_postrun_under_test", POSTRUN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def postrun(tmp_path, monkeypatch):
    mod = _load_postrun()
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
always_on_bytes: 120
expectation: NUDGED lines always pair with RESUMED within one ladder step
check_window_days: 14
revert: git revert of the applying auto-commit
---

## Evidence
8 findings across 3 weeks.

## Diff
```diff
--- a/rules/foo.md
+++ b/rules/foo.md
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
        assert meta["always_on_bytes"] == "120"
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
        text = text.replace("--- a/rules/foo.md\n+++ b/rules/foo.md\n+new line", diff_block)
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
        text = path.read_text().replace("always_on_bytes: 120", "always_on_bytes: 0")
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
        text = text.replace("--- a/rules/foo.md\n+++ b/rules/foo.md",
                            "--- a/docs/adoption.md\n+++ b/docs/adoption.md")
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


class TestHomeFallback:
    def test_prefers_dockwright_homes(self, tmp_path, monkeypatch):
        claude = tmp_path / ".claude"
        for rel in ("dockwright/gardener", "gardener",
                    "dockwright/selffix/findings", "selffix-findings"):
            (claude / rel).mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        mod = _load_postrun()
        assert mod.GARDENER_DIR == claude / "dockwright" / "gardener"
        assert mod.FINDINGS_DIR == claude / "dockwright" / "selffix" / "findings"

    def test_falls_back_to_legacy_homes(self, tmp_path, monkeypatch):
        claude = tmp_path / ".claude"
        for rel in ("gardener", "selffix-findings"):
            (claude / rel).mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        mod = _load_postrun()
        assert mod.GARDENER_DIR == claude / "gardener"
        assert mod.FINDINGS_DIR == claude / "selffix-findings"
