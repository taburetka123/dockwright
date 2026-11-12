import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
POSTRUN_PATH = REPO_ROOT / "deploy" / "scripts" / "gardener_postrun.py"
SCRIPTS = REPO_ROOT / "deploy" / "scripts"
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


MEMBERS = ["0eef8c47-2bc4-41d3-84df-c61e3ec2f9d1", "a2d9ddea-0320-417e-b556-d2a8a44420f2"]
EXTRA_MEMBER = "5f1c2a33-9b8e-4c1d-8e7f-0a1b2c3d4e5f"


def _write_variant(postrun, pid, *, members=MEMBERS, lane="digest", evidence_kind="findings",
                   kind="rule-edit", supersedes=None, section=True, prose_diff=False):
    text = PROPOSAL_TEMPLATE.format(
        pid=pid, cluster="c",
        target=str(postrun.ALLOWED_TARGET_ROOTS[0] / "rules" / "foo.md"))
    text = text.replace(f"members: [{', '.join(MEMBERS)}]",
                        f"members: [{', '.join(members)}]" if members else "")
    text = text.replace("lane: digest", f"lane: {lane}")
    text = text.replace("evidence_kind: findings", f"evidence_kind: {evidence_kind}")
    text = text.replace("kind: rule-edit", f"kind: {kind}")
    if supersedes:
        text = text.replace("revert: ", f"supersedes: [{', '.join(supersedes)}]\nrevert: ", 1)
        if section:
            text += "\n## Supersedes\n" + "".join(f"- {sid}: redraft\n" for sid in supersedes)
    if prose_diff:
        text = re.sub(r"```diff\n.*?```\n",
                      "no artifact change: accepting closes the superseded proposals\n",
                      text, flags=re.S)
    path = postrun.PENDING_DIR / f"{pid}.md"
    path.write_text(text)
    return path


def _log_old(postrun, pid="old-1", **kw):
    path = _write_variant(postrun, pid, **kw)
    postrun.process_run_artifacts("r-old", known=postrun.known_from_ledger())
    return path


def _findings(postrun, *sids):
    for sid in sids:
        (postrun.FINDINGS_DIR / f"{sid}.md").write_text("finding\n")


def _events(postrun, kind):
    return [e for e in _ledger_events(postrun) if e["event"] == kind]


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
        path = _write_proposal(postrun)
        rc = postrun.decide(str(path), "accept", reason="ok")
        assert rc == 0
        assert (postrun.ACCEPTED_DIR / "p1.md").exists()

    def test_decide_rejects_unknown_kind(self, postrun):
        path = self._accepted_setup(postrun)
        rc = postrun.decide(str(path), "maybe", reason="x")
        assert rc != 0


class TestDecideUuidRegression:

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
NOW = 1_800_000_000.0


def _arm(postrun, check_id, ts, window="14", expectation="exp",
         run_id="r", cluster="c", lane="digest"):
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
    def test_matured_recorded_unmatured_skipped(self, postrun, tmp_path):
        _arm(postrun, "c-old", NOW - 20 * DAY)
        _arm(postrun, "c-new", NOW - 5 * DAY)
        vf = _verdicts_file(tmp_path, {"c-old": "kept", "c-new": "kept"})
        rc = postrun.evaluate(vf, dry_run=False, now=NOW)
        assert rc == 0
        outs = _outcomes(postrun)
        assert [o["check_id"] for o in outs] == ["c-old"]
        assert outs[0]["type"] == "check_kept"

    def test_event_shape(self, postrun, tmp_path):
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

    def test_idempotent_rerun(self, postrun, tmp_path):
        _arm(postrun, "c1", NOW - 20 * DAY)
        vf = _verdicts_file(tmp_path, {"c1": "kept"})
        assert postrun.evaluate(vf, dry_run=False, now=NOW) == 0
        assert postrun.evaluate(vf, dry_run=False, now=NOW) == 0
        assert len(_outcomes(postrun)) == 1

    def test_dry_run_writes_nothing(self, postrun, tmp_path):
        _arm(postrun, "c1", NOW - 20 * DAY)
        vf = _verdicts_file(tmp_path, {"c1": "kept"})
        before = postrun.LEDGER_PATH.stat().st_size
        rc = postrun.evaluate(vf, dry_run=True, now=NOW)
        assert rc == 0
        assert postrun.LEDGER_PATH.stat().st_size == before
        assert _outcomes(postrun) == []

    def test_violated_requires_evidence(self, postrun, tmp_path):
        _arm(postrun, "c1", NOW - 20 * DAY)
        vf = _verdicts_file(tmp_path, {"c1": "violated"})
        rc = postrun.evaluate(vf, dry_run=False, now=NOW)
        assert rc != 0
        assert _outcomes(postrun) == []

    def test_violated_with_evidence_records(self, postrun, tmp_path):
        _arm(postrun, "c1", NOW - 20 * DAY)
        vf = _verdicts_file(tmp_path, {"c1": {"verdict": "violated", "evidence": "recurred 3x"}})
        assert postrun.evaluate(vf, dry_run=False, now=NOW) == 0
        o = _outcomes(postrun)[0]
        assert o["type"] == "check_violated" and o["verdict"] == "violated"
        assert o["evidence"] == "recurred 3x"

    def test_unknown_check_id_refused_others_processed(self, postrun, tmp_path):
        _arm(postrun, "c-good", NOW - 20 * DAY)
        vf = _verdicts_file(tmp_path, {"c-good": "kept", "c-bogus": "kept"})
        rc = postrun.evaluate(vf, dry_run=False, now=NOW)
        assert rc != 0
        assert [o["check_id"] for o in _outcomes(postrun)] == ["c-good"]

    def test_bare_evaluate_no_verdicts_awaiting(self, postrun, capsys):
        _arm(postrun, "c1", NOW - 20 * DAY)
        rc = postrun.evaluate(None, dry_run=False, now=NOW)
        assert rc == 0
        assert _outcomes(postrun) == []
        assert "AWAITING-VERDICT" in capsys.readouterr().out

    def test_append_only_expectation_preserved(self, postrun, tmp_path):
        exp = 'quote " and unicode ✓ expectation'
        _arm(postrun, "c1", NOW - 20 * DAY, expectation=exp)
        vf = _verdicts_file(tmp_path, {"c1": "kept"})
        assert postrun.evaluate(vf, dry_run=False, now=NOW) == 0
        assert _outcomes(postrun)[0]["expectation"] == exp

    def test_kept_without_evidence_allowed(self, postrun, tmp_path):
        _arm(postrun, "c1", NOW - 20 * DAY)
        vf = _verdicts_file(tmp_path, {"c1": "kept"})
        assert postrun.evaluate(vf, dry_run=False, now=NOW) == 0
        assert _outcomes(postrun)[0]["evidence"] == ""

    def test_malformed_verdicts_file_hard_error(self, postrun, tmp_path):
        _arm(postrun, "c1", NOW - 20 * DAY)
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json")
        assert postrun.evaluate(str(bad), dry_run=False, now=NOW) == 2
        arr = tmp_path / "arr.json"
        arr.write_text("[1, 2, 3]")
        assert postrun.evaluate(str(arr), dry_run=False, now=NOW) == 2
        assert _outcomes(postrun) == []

    def test_unknown_verdict_string_refused(self, postrun, tmp_path):
        _arm(postrun, "c1", NOW - 20 * DAY)
        vf = _verdicts_file(tmp_path, {"c1": "maybe"})
        assert postrun.evaluate(vf, dry_run=False, now=NOW) != 0
        assert _outcomes(postrun) == []

    def test_window_parse_error(self, postrun, tmp_path):
        _arm(postrun, "c-bad", NOW - 20 * DAY, window="not-a-number")
        _arm(postrun, "c-good", NOW - 20 * DAY, window="14")
        vf = _verdicts_file(tmp_path, {"c-bad": "kept", "c-good": "kept"})
        rc = postrun.evaluate(vf, dry_run=False, now=NOW)
        assert rc != 0
        assert [o["check_id"] for o in _outcomes(postrun)] == ["c-good"]

    def test_duplicate_arm_records_against_first(self, postrun, tmp_path, capsys):
        _arm(postrun, "c1", NOW - 20 * DAY, expectation="E1")
        _arm(postrun, "c1", NOW - 10 * DAY, expectation="E2-weaker")
        vf = _verdicts_file(tmp_path, {"c1": "kept"})
        rc = postrun.evaluate(vf, dry_run=False, now=NOW)
        assert rc != 0
        outs = _outcomes(postrun)
        assert len(outs) == 1
        assert outs[0]["expectation"] == "E1"
        assert outs[0]["armed_ts"] == NOW - 20 * DAY
        assert "DUPLICATE-ARM" in capsys.readouterr().out

    def test_duplicate_arm_identical_expectation_benign(self, postrun, tmp_path):
        _arm(postrun, "c1", NOW - 20 * DAY, expectation="E")
        _arm(postrun, "c1", NOW - 10 * DAY, expectation="E")
        vf = _verdicts_file(tmp_path, {"c1": "kept"})
        assert postrun.evaluate(vf, dry_run=False, now=NOW) == 0
        assert len(_outcomes(postrun)) == 1

    def test_duplicate_arm_rerun_idempotent(self, postrun, tmp_path):
        _arm(postrun, "c1", NOW - 20 * DAY, expectation="E1")
        _arm(postrun, "c1", NOW - 10 * DAY, expectation="E2-weaker")
        vf = _verdicts_file(tmp_path, {"c1": "kept"})
        assert postrun.evaluate(vf, dry_run=False, now=NOW) != 0
        assert postrun.evaluate(vf, dry_run=False, now=NOW) == 0
        assert len(_outcomes(postrun)) == 1

    @pytest.mark.parametrize("delta,matured", [(-1, False), (0, True), (1, True)])
    def test_maturity_boundary_inclusive(self, postrun, delta, matured):
        info = {"armed_ts": NOW - 14 * DAY - delta, "check_window_days": "14"}
        assert postrun.is_matured(info, NOW) is matured

    def test_main_cli_dry_run(self, postrun, tmp_path):
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
    postrun.ledger_append("proposal", proposal_id="p1", path="/x")
    postrun.ledger_append("decision", proposal_id="p1", kind="accept")
    before = postrun.LEDGER_PATH.read_text()
    rc = postrun.annotate("p1", "superseded by cluster merge")
    assert rc == 0
    after = postrun.LEDGER_PATH.read_text()
    assert after.startswith(before)
    events = _ledger_events(postrun)
    assert len(events) == 3
    last = events[-1]
    assert last["type"] == "annotate"
    assert last["ref"] == "p1"
    assert last["note"] == "superseded by cluster merge"
    assert "path" not in last


def test_gardener_dir_env_redirects_subprocess_ledger(tmp_path):
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
    mod = _load_postrun()
    assert str(tmp_path) in str(mod.LEDGER_PATH)
    assert str(mod.LEDGER_PATH).endswith(
        os.path.join("gardener-state", "ledger.jsonl"))


def test_birth_gate_skips_ledger_known_proposals(postrun_env):
    mod, root, pending = postrun_env
    text = _proposal_text(
        [str(root / "rules" / "x.md")],
        "```diff\n--- a/rules/x.md\n+++ b/rules/x.md\n@@\n GONE\n+x\n```")
    (pending / "known.md").write_text(text)
    summary = mod.process_run_artifacts("r9", {"known.md"})
    assert summary["rejected"] == 0
    assert (pending / "known.md").exists()


def _delta_body(diff: str) -> str:
    return "## Evidence\nE\n\n## Diff\n```diff\n" + diff + "\n```\n\n## Rationale\nR\n"


class TestComputeAlwaysOnDelta:
    def _rules_target(self, postrun):
        return [str(postrun.ALLOWED_TARGET_ROOTS[0] / "rules" / "foo.md")]

    def test_ab_relative_headers_resolve_against_declared_targets(self, postrun):
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

    def test_a_role_fragment_counts_toward_the_always_on_delta(self, postrun):
        root = postrun.ALLOWED_TARGET_ROOTS[0]
        targets = [str(root / "roles" / "Coder" / "fragments" / "foo.md")]
        diff = ("--- a/roles/Coder/fragments/foo.md\n"
                "+++ b/roles/Coder/fragments/foo.md\n"
                "@@ -1,0 +1,1 @@\n"
                "+added\n")
        assert postrun.compute_always_on_delta(_delta_body(diff), targets) == len("added") + 1

    def test_multibyte_counts_utf8_bytes_not_chars(self, postrun):
        diff = ("--- a/rules/foo.md\n"
                "+++ b/rules/foo.md\n"
                "@@ -1,1 +0,0 @@\n"
                "-αβγδε\n")
        assert postrun.compute_always_on_delta(_delta_body(diff), self._rules_target(postrun)) == -11

    def test_content_lines_starting_with_dashes_not_misread_as_headers(self, postrun):
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
        diff = ("--- a/rules/foo.md\n"
                "+++ b/rules/foo.md\n"
                "@@\n"
                " beta\n"
                "+gamma\n")
        delta = postrun.compute_always_on_delta(_delta_body(diff), self._rules_target(postrun))
        assert delta == len("gamma") + 1

    def test_unresolvable_header_returns_none(self, postrun):
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
        root = postrun.ALLOWED_TARGET_ROOTS[0]
        targets = [str(root / "rules" / "bar.md"), str(root / "rules" / "baz.md")]
        rem = "R" * 50
        add = "A" * 49
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
        expected = -3 * (len(rem) + 1) + 4 * (len(add) + 1)
        assert expected == 47
        assert postrun.compute_always_on_delta(_delta_body(diff), targets) == expected

    def test_undercounted_hunk_truncation_recomputes_from_lenient(self, postrun):
        targets = self._rules_target(postrun)
        rem = "R" * 200
        small = "s"
        big = "A" * 125
        diff = ("--- a/rules/foo.md\n"
                "+++ b/rules/foo.md\n"
                "@@ -1,2 +1,2 @@\n"
                " keep\n"
                f"-{rem}\n"
                f"+{small}\n"
                f"+{big}\n"
                f"+{big}\n")
        expected = -(len(rem) + 1) + (len(small) + 1) + 2 * (len(big) + 1)
        assert expected == 53
        assert postrun.compute_always_on_delta(_delta_body(diff), targets) == expected


class TestAlwaysOnBytesConsistency:

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

    _BARE_AT_ADDITIVE = ("--- a/rules/foo.md\n"
                         "+++ b/rules/foo.md\n"
                         "@@\n"
                         " beta\n"
                         "+padding padding padding padding\n")

    def test_bare_at_at_lying_zero_quarantined(self, postrun):
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
        meta = {"always_on_bytes": "-500", "targets": ["x"]}
        assert postrun._always_on_bytes_violations(meta, _delta_body("garbage")) == []

    def test_tolerance_boundary_16_passes(self, postrun):
        assert postrun._BYTES_TOLERANCE == 16
        computed = 3 * (len("padding padding padding padding") + 1)
        self._proposal(postrun, declared=computed - 16, diff=self._ADDITIVE)
        summary = postrun.process_run_artifacts("r-c", set())
        assert summary["rejected"] == 0 and summary["proposals"] == 1

    def test_tolerance_boundary_17_quarantined(self, postrun):
        computed = 3 * (len("padding padding padding padding") + 1)
        self._proposal(postrun, declared=computed - 17, diff=self._ADDITIVE)
        summary = postrun.process_run_artifacts("r-c", set())
        assert summary["rejected"] == 1
        assert any("always_on_bytes mismatch" in e.get("reasons", "")
                   for e in _ledger_events(postrun)
                   if e.get("type") == "proposal_rejected")

    def test_bytes_tolerance_config_widens_mismatch_tolerance(
            self, postrun, tmp_path, monkeypatch):
        cfg = tmp_path / "dockwright.toml"
        cfg.write_text("[gardener]\nbytes_tolerance = 32\n")
        monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
        computed = 3 * (len("padding padding padding padding") + 1)
        self._proposal(postrun, declared=computed - 17, diff=self._ADDITIVE)
        summary = postrun.process_run_artifacts("r-c", set())
        assert summary["rejected"] == 0 and summary["proposals"] == 1

    def test_scan_fallback_reads_unquoted_bytes_tolerance(self, postrun):
        assert postrun._scan_toml_str("[gardener]\nbytes_tolerance = 32\n",
                                      "gardener", "bytes_tolerance") == "32"


class TestCostJustificationCensor:

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

    _ADD96 = TestAlwaysOnBytesConsistency._ADDITIVE
    _ADD17 = ("--- a/rules/foo.md\n"
              "+++ b/rules/foo.md\n"
              "@@ -1,0 +1,1 @@\n"
              "+" + "x" * 16 + "\n")
    _ADD16 = ("--- a/rules/foo.md\n"
              "+++ b/rules/foo.md\n"
              "@@ -1,0 +1,1 @@\n"
              "+" + "x" * 15 + "\n")
    _REMOVE = ("--- a/rules/foo.md\n"
               "+++ b/rules/foo.md\n"
               "@@ -1,2 +1,1 @@\n"
               "-" + "y" * 40 + "\n"
               " keep\n")

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
        self._proposal(postrun, declared='"lots"', diff=self._ADD96)
        summary = postrun.process_run_artifacts("r-z", set())
        assert summary["rejected"] == 1
        reasons = [e.get("reasons", "") for e in _ledger_events(postrun)
                   if e.get("type") == "proposal_rejected"]
        assert any("not an integer" in r and "cost_justification" in r
                   for r in reasons)

    def test_non_int_declared_unparseable_diff_still_flagged(self, postrun):
        meta = {"always_on_bytes": "lots", "targets": ["x"]}
        assert any("not an integer" in v
                   for v in postrun._always_on_bytes_violations(
                       meta, _delta_body("garbage")))

    def test_frontier_lane_positive_undeclared_quarantined(self, postrun):
        self._proposal(postrun, declared=96, diff=self._ADD96, lane="frontier")
        summary = postrun.process_run_artifacts("r-z", set(), lane="frontier")
        assert summary["rejected"] == 1

    def test_censor_config_floor(self, postrun, tmp_path, monkeypatch):
        cfg = tmp_path / "dockwright.toml"
        cfg.write_text("[gardener]\nbytes_tolerance = 32\n")
        monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
        self._proposal(postrun, declared=17, diff=self._ADD17)
        summary = postrun.process_run_artifacts("r-z", set())
        assert summary["rejected"] == 0 and summary["proposals"] == 1

    def test_list_valued_whitespace_justification_quarantined(self, postrun):
        self._proposal(postrun, declared=96, diff=self._ADD96,
                       justification_line='cost_justification: ["  "]\n')
        summary = postrun.process_run_artifacts("r-z", set())
        assert summary["rejected"] == 1

    def test_zero_delta_without_field_passes(self, postrun):
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


def _negative_proposal_text(postrun, pid, target_rel="rules/big.md"):
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
        text = _additive_proposal_text(postrun, "sham").replace(
            "always_on_bytes: 40", "always_on_bytes: -400")
        (postrun.PENDING_DIR / "sham.md").write_text(text)
        self._postrun_cli(postrun, "r1")
        events = _bp_events(postrun)
        assert events[0]["proposals"] == 0 and events[0]["negative"] == 0

    def test_config_override_unquoted_int(self, postrun, tmp_path, monkeypatch):
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
        monkeypatch.setattr(postrun, "_apply_check",
                            lambda path, body: ("skipped-env", "boom"))
        (postrun.PENDING_DIR / "n1.md").write_text(_negative_proposal_text(postrun, "n1"))
        self._postrun_cli(postrun, "r1")
        events = _bp_events(postrun)
        assert events[0]["proposals"] == 1 and events[0]["negative"] == 0

    def test_computed_not_declared_governs_qualification(self, postrun):
        text = _negative_delta_proposal_text(postrun, "straddle", n_lines=3, declared=-130)
        (postrun.PENDING_DIR / "straddle.md").write_text(text)
        self._postrun_cli(postrun, "r1")
        events = _bp_events(postrun)
        assert events[0]["proposals"] == 1
        assert events[0]["negative"] == 0

    def test_swallow_declared_strict_value_earns_no_credit(self, postrun):
        root = postrun.ALLOWED_TARGET_ROOTS[0]
        (root / "rules").mkdir(parents=True, exist_ok=True)
        rem = "R" * 50
        add = "A" * 49
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
        assert events[0]["proposals"] == 0
        assert events[0]["negative"] == 0
        assert any("always_on_bytes mismatch" in e.get("reasons", "")
                   for e in _ledger_events(postrun)
                   if e.get("type") == "proposal_rejected")

    def test_undercount_declared_strict_value_earns_no_credit(self, postrun):
        root = postrun.ALLOWED_TARGET_ROOTS[0]
        (root / "rules").mkdir(parents=True, exist_ok=True)
        (postrun.PENDING_DIR / "a1.md").write_text(_additive_proposal_text(postrun, "a1"))
        self._postrun_cli(postrun, "r1")
        rem = "R" * 200
        small = "s"
        big = "A" * 125
        foo = root / "rules" / "foo.md"
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
        assert [e["streak"] for e in events] == [1, 1]
        assert events[-1]["proposals"] == 0 and events[-1]["negative"] == 0
        assert any("always_on_bytes mismatch" in e.get("reasons", "")
                   for e in _ledger_events(postrun)
                   if e.get("type") == "proposal_rejected")

    def test_below_min_bytes_threshold_does_not_qualify(self, postrun):
        text = _negative_delta_proposal_text(postrun, "small", n_lines=1, declared=-40)
        (postrun.PENDING_DIR / "small.md").write_text(text)
        self._postrun_cli(postrun, "r1")
        events = _bp_events(postrun)
        assert events[0]["proposals"] == 1
        assert events[0]["negative"] == 0


class TestFlowCost:

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
        for value in ("`none`", "**none**", "none.", "'adds' - one per round"):
            assert postrun._flow_cost_violations(self._meta(value)) == [], value

    def test_bracketed_value_does_not_raise(self, postrun):
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
        meta = {"id": "p1", "run_id": "r1", "cluster": "c", "lane": "digest",
                "targets": [str(postrun.HOME / ".claude" / "rules" / "x.md")],
                "kind": "rule-edit", "evidence_kind": "ops",
                "always_on_bytes": "0", "base_rev": "abc1234",
                "expectation": "e", "check_window_days": "7", "revert": "r"}
        violations = postrun.validate_proposal(meta, "## Evidence\n## Diff\nprose\n")
        assert any("flow_cost" in v for v in violations)

    def test_frontier_lane_is_checked_too(self, postrun):
        meta = {"id": "f1", "run_id": "r1", "cluster": "c", "lane": "frontier",
                "targets": [str(postrun.HOME / ".claude" / "rules" / "x.md")],
                "kind": "rule-edit", "evidence_kind": "external",
                "always_on_bytes": "0", "base_rev": "abc1234",
                "expectation": "e", "check_window_days": "14", "revert": "r"}
        violations = postrun.validate_proposal(meta, "## Evidence\n## Diff\nprose\n")
        assert any("flow_cost" in v for v in violations)


class TestSupersedeAtBirth:
    def test_supersede_moves_old_keeps_mtime_and_records_both_sides(self, postrun):
        old = _log_old(postrun)
        stamp = old.stat().st_mtime_ns - 3600 * 10**9
        os.utime(old, ns=(stamp, stamp))
        new = _write_variant(postrun, "new-1", supersedes=["old-1"])
        summary = postrun.process_run_artifacts("r-new", known=postrun.known_from_ledger())
        moved = postrun.superseded_dir() / "old-1.md"
        assert new.exists() and not old.exists() and moved.exists()
        meta, body = postrun.parse_frontmatter(moved.read_text())
        assert meta["superseded_by"] == "new-1"
        assert meta["superseded_run"] == "r-new"
        assert "## Evidence" in body
        assert moved.stat().st_mtime_ns == stamp
        superseded = _events(postrun, "proposal_superseded")
        assert [(e["proposal_id"], e["superseded_by"]) for e in superseded] == [("old-1", "new-1")]
        born = [e for e in _events(postrun, "proposal") if e["proposal_id"] == "new-1"]
        assert born[0]["supersedes"] == "old-1"
        assert summary["superseded"] == 1 and summary["rejected"] == 0

    def test_non_findings_proposal_cannot_supersede_one_with_members(self, postrun):
        old = _log_old(postrun)
        new = _write_variant(postrun, "new-1", members=None, evidence_kind="ops",
                             supersedes=["old-1"])
        summary = postrun.process_run_artifacts("r-new", known=postrun.known_from_ledger())
        assert old.exists() and not new.exists()
        assert summary["rejected"] == 1

    def test_dropping_a_member_is_quarantined(self, postrun):
        old = _log_old(postrun)
        _write_variant(postrun, "new-1", members=MEMBERS[:1], supersedes=["old-1"])
        summary = postrun.process_run_artifacts("r-new", known=postrun.known_from_ledger())
        assert old.exists() and summary["rejected"] == 1

    def test_legacy_prefix_member_is_covered_by_full_uuid(self, postrun):
        old = _write_variant(postrun, "old-1")
        old.write_text(old.read_text().replace(MEMBERS[0], MEMBERS[0][:8]))
        postrun.ledger_append("proposal", proposal_id="old-1", path=str(old))
        _write_variant(postrun, "new-1", supersedes=["old-1"])
        summary = postrun.process_run_artifacts("r-new", known=postrun.known_from_ledger())
        assert summary["superseded"] == 1 and summary["rejected"] == 0

    def test_missing_supersedes_section_is_quarantined(self, postrun):
        old = _log_old(postrun)
        _write_variant(postrun, "new-1", supersedes=["old-1"], section=False)
        summary = postrun.process_run_artifacts("r-new", known=postrun.known_from_ledger())
        assert old.exists() and summary["rejected"] == 1

    def test_unknown_id_is_quarantined(self, postrun):
        _write_variant(postrun, "new-1", supersedes=["nope-1"])
        summary = postrun.process_run_artifacts("r-new", known=set())
        assert summary["rejected"] == 1

    def test_other_lane_id_is_quarantined(self, postrun):
        old = _log_old(postrun, lane="frontier", evidence_kind="external", members=None)
        _write_variant(postrun, "new-1", supersedes=["old-1"])
        summary = postrun.process_run_artifacts("r-new", known=postrun.known_from_ledger())
        assert old.exists() and summary["rejected"] == 1

    def test_id_born_in_the_same_postrun_is_quarantined(self, postrun):
        old = _write_variant(postrun, "old-1")
        _write_variant(postrun, "new-1", supersedes=["old-1"])
        summary = postrun.process_run_artifacts("r-both", known=set())
        assert old.exists()
        assert summary["rejected"] == 1 and summary["superseded"] == 0

    def test_open_apply_blocks_until_reverted(self, postrun):
        old = _log_old(postrun)
        postrun.ledger_append("proposal_apply_started", proposal_id="old-1")
        _write_variant(postrun, "new-1", supersedes=["old-1"])
        first = postrun.process_run_artifacts("r-1", known=postrun.known_from_ledger())
        assert old.exists() and first["rejected"] == 1
        postrun.ledger_append("proposal_reverted", proposal_id="old-1")
        _write_variant(postrun, "new-2", supersedes=["old-1"])
        second = postrun.process_run_artifacts("r-2", known=postrun.known_from_ledger())
        assert not old.exists() and second["superseded"] == 1

    def test_failure_before_start_does_not_close_an_open_apply(self, postrun):
        postrun.ledger_append("proposal_apply_started", proposal_id="old-1")
        postrun.ledger_append("proposal_apply_failed", proposal_id="old-1", after_start=False)
        assert postrun.open_apply_ids() == {"old-1"}
        postrun.ledger_append("proposal_apply_failed", proposal_id="old-1", after_start=True)
        assert postrun.open_apply_ids() == set()

    def test_apply_opened_between_check_and_move_blocks_the_move(self, postrun, monkeypatch):
        old = _log_old(postrun)
        answers = iter([set(), {"old-1"}])
        monkeypatch.setattr(postrun, "open_apply_ids", lambda: next(answers))
        new = _write_variant(postrun, "new-1", supersedes=["old-1"])
        summary = postrun.process_run_artifacts("r-new", known=postrun.known_from_ledger())
        assert old.exists() and not new.exists()
        assert summary["rejected"] == 1 and summary["superseded"] == 0

    def test_two_new_proposals_on_one_old_quarantine_the_second(self, postrun):
        old = _log_old(postrun)
        _write_variant(postrun, "new-a", supersedes=["old-1"])
        _write_variant(postrun, "new-b", supersedes=["old-1"])
        summary = postrun.process_run_artifacts("r-new", known=postrun.known_from_ledger())
        assert not old.exists()
        assert summary["superseded"] == 1 and summary["rejected"] == 1
        assert (postrun.PENDING_DIR / "new-a.md").exists()

    def test_proposal_retire_does_not_count_toward_backpressure(self, postrun):
        _log_old(postrun)
        _write_variant(postrun, "retire-1", kind="proposal-retire", supersedes=["old-1"],
                       prose_diff=True)
        summary = postrun.process_run_artifacts("r-new", known=postrun.known_from_ledger())
        assert summary["proposals"] == 1 and summary["digest_proposals"] == 0

    def test_redraft_with_supersedes_counts_toward_backpressure(self, postrun):
        _log_old(postrun)
        _write_variant(postrun, "new-1", supersedes=["old-1"])
        summary = postrun.process_run_artifacts("r-new", known=postrun.known_from_ledger())
        assert summary["proposals"] == 1 and summary["superseded"] == 1
        assert summary["digest_proposals"] == 1 and summary["digest_negative"] == 0

    def test_negative_redraft_with_supersedes_earns_backpressure_credit(self, postrun):
        _log_old(postrun, members=None, evidence_kind="ops")
        text = _negative_proposal_text(postrun, "new-1")
        text = text.replace("revert: r\n", "supersedes: [old-1]\nrevert: r\n", 1)
        text += "\n## Supersedes\n- old-1: redraft\n"
        (postrun.PENDING_DIR / "new-1.md").write_text(text)
        summary = postrun.process_run_artifacts("r-new", known=postrun.known_from_ledger())
        assert summary["rejected"] == 0 and summary["superseded"] == 1
        assert summary["digest_proposals"] == 1 and summary["digest_negative"] == 1

    def test_proposal_retire_earns_no_backpressure_credit_either_way(self, postrun):
        _log_old(postrun)
        _write_variant(postrun, "retire-1", kind="proposal-retire", supersedes=["old-1"],
                       prose_diff=True)
        summary = postrun.process_run_artifacts("r-new", known=postrun.known_from_ledger())
        assert summary["superseded"] == 1
        assert summary["digest_proposals"] == 0 and summary["digest_negative"] == 0

    def test_blank_supersedes_behaves_like_absent(self, postrun):
        path = _write_variant(postrun, "new-1")
        path.write_text(path.read_text().replace("revert: ", "supersedes:\nrevert: ", 1))
        summary = postrun.process_run_artifacts("r-new", known=set())
        assert path.exists() and summary["rejected"] == 0
        assert summary["proposals"] == 1 and summary["digest_proposals"] == 1
        assert _events(postrun, "proposal")[-1]["supersedes"] == ""
        assert postrun.decide(str(path), "accept", "ok") == 0
        assert _events(postrun, "decision")[-1]["supersedes"] == ""

    def test_frontier_supersession_moves_the_old_file(self, postrun):
        old = _log_old(postrun, lane="frontier", evidence_kind="external", members=None)
        _write_variant(postrun, "new-1", lane="frontier", evidence_kind="external",
                       members=None, supersedes=["old-1"])
        summary = postrun.process_run_artifacts("r-new", known=postrun.known_from_ledger(),
                                                lane="frontier")
        assert not old.exists() and (postrun.superseded_dir() / "old-1.md").exists()
        assert summary["superseded"] == 1 and summary["rejected"] == 0
        assert _events(postrun, "proposal_superseded")[-1]["lane"] == "frontier"

    def test_open_apply_on_one_of_two_ids_moves_neither(self, postrun):
        old_1 = _log_old(postrun, "old-1")
        old_2 = _log_old(postrun, "old-2")
        postrun.ledger_append("proposal_apply_started", proposal_id="old-2")
        new = _write_variant(postrun, "new-1", supersedes=["old-1", "old-2"])
        summary = postrun.process_run_artifacts("r-new", known=postrun.known_from_ledger())
        assert old_1.exists() and old_2.exists() and not new.exists()
        assert summary["rejected"] == 1 and summary["superseded"] == 0
        assert _events(postrun, "proposal_superseded") == []

    def test_refused_successor_keeps_its_birth_event_before_the_rejection(self, postrun,
                                                                          monkeypatch):
        _log_old(postrun)
        answers = iter([set(), {"old-1"}])
        monkeypatch.setattr(postrun, "open_apply_ids", lambda: next(answers))
        _write_variant(postrun, "new-1", supersedes=["old-1"])
        postrun.process_run_artifacts("r-new", known=postrun.known_from_ledger())
        trail = [(e["event"], e.get("proposal_id") or Path(e["path"]).name)
                 for e in _ledger_events(postrun)
                 if e["event"] in ("proposal", "proposal_rejected")]
        assert trail[-2:] == [("proposal", "new-1"), ("proposal_rejected", "new-1.md")]

    def test_failure_after_start_with_failed_rollback_keeps_the_apply_open(self, postrun):
        postrun.ledger_append("proposal_apply_started", proposal_id="old-1")
        postrun.ledger_append("proposal_apply_failed", proposal_id="old-1",
                              after_start=True, rollback_failed=True)
        assert postrun.open_apply_ids() == {"old-1"}
        postrun.ledger_append("proposal_apply_failed", proposal_id="old-1",
                              after_start=True, rollback_failed=False)
        assert postrun.open_apply_ids() == set()

    def test_edit_frontmatter_leaves_one_file_and_keeps_mtime(self, postrun):
        path = _write_variant(postrun, "p-1")
        stamp = path.stat().st_mtime_ns - 3600 * 10**9
        os.utime(path, ns=(stamp, stamp))
        postrun._edit_frontmatter(path, add={"superseded_by": "x"}, remove=("cluster",))
        meta, body = postrun.parse_frontmatter(path.read_text())
        assert meta["superseded_by"] == "x" and "cluster" not in meta and "## Evidence" in body
        assert path.stat().st_mtime_ns == stamp
        assert sorted(p.name for p in postrun.PENDING_DIR.iterdir()) == ["p-1.md"]


class TestOpenAppliesCommand:
    def test_prints_nothing_when_no_apply_is_open(self, postrun, capsys):
        assert postrun.main(["open-applies"]) == 0
        assert capsys.readouterr().out == ""

    def test_prints_each_open_id_sorted(self, postrun, capsys):
        postrun.ledger_append("proposal_apply_started", proposal_id="b-2")
        postrun.ledger_append("proposal_apply_started", proposal_id="a-1")
        postrun.ledger_append("proposal_apply_started", proposal_id="c-3")
        postrun.ledger_append("proposal_reverted", proposal_id="c-3")
        assert postrun.main(["open-applies"]) == 0
        assert capsys.readouterr().out == "a-1\nb-2\n"


class TestDecideSupersession:
    def _supersede(self, postrun, new_pid="new-1", **kw):
        old = _log_old(postrun)
        stamp = old.stat().st_mtime_ns - 3600 * 10**9
        os.utime(old, ns=(stamp, stamp))
        new = _write_variant(postrun, new_pid, supersedes=["old-1"], **kw)
        postrun.process_run_artifacts("r-new", known=postrun.known_from_ledger())
        return old, new, stamp

    def test_decline_proposal_retire_restores_and_marks_nothing(self, postrun):
        _findings(postrun, *MEMBERS)
        old, retire, stamp = self._supersede(postrun, "retire-1", kind="proposal-retire",
                                             prose_diff=True)
        assert postrun.decide(str(retire), "decline", "not stale") == 0
        assert old.exists()
        meta, _ = postrun.parse_frontmatter(old.read_text())
        assert "superseded_by" not in meta and "superseded_run" not in meta
        assert old.stat().st_mtime_ns == stamp
        assert not any(postrun.FINDINGS_DIR.glob("*.reviewed"))
        assert [e["proposal_id"] for e in _events(postrun, "proposal_restored")] == ["old-1"]
        assert _events(postrun, "decision")[-1]["restored"] == "old-1"

    def test_decline_with_restore_flag_marks_only_uncarried_members(self, postrun):
        _findings(postrun, *MEMBERS, EXTRA_MEMBER)
        old, new, _ = self._supersede(postrun, members=MEMBERS + [EXTRA_MEMBER])
        assert postrun.decide(str(new), "decline", "wrong merge", restore_superseded=True) == 0
        assert old.exists()
        assert (postrun.FINDINGS_DIR / f"{EXTRA_MEMBER}.reviewed").exists()
        assert not any((postrun.FINDINGS_DIR / f"{sid}.reviewed").exists() for sid in MEMBERS)

    def test_decline_without_flag_keeps_superseded_and_marks_all(self, postrun):
        _findings(postrun, *MEMBERS, EXTRA_MEMBER)
        old, new, _ = self._supersede(postrun, members=MEMBERS + [EXTRA_MEMBER])
        assert postrun.decide(str(new), "decline", "not worth it") == 0
        assert not old.exists() and (postrun.superseded_dir() / "old-1.md").exists()
        assert all((postrun.FINDINGS_DIR / f"{sid}.reviewed").exists()
                   for sid in MEMBERS + [EXTRA_MEMBER])
        assert _events(postrun, "decision")[-1]["supersedes"] == "old-1"

    def test_accept_with_restore_flag_is_refused(self, postrun):
        _old, new, _ = self._supersede(postrun)
        assert postrun.decide(str(new), "accept", "ok", restore_superseded=True) == 2
        assert new.exists()

    def test_restore_name_collision_refuses_and_moves_nothing(self, postrun):
        old, retire, _ = self._supersede(postrun, "retire-1", kind="proposal-retire",
                                         prose_diff=True)
        old.write_text("placeholder\n")
        assert postrun.decide(str(retire), "decline", "not stale") == 2
        assert retire.exists()
        assert (postrun.superseded_dir() / "old-1.md").exists()

    def test_decide_completes_an_unfinished_supersession(self, postrun):
        old = _log_old(postrun)
        new = _write_variant(postrun, "new-1", supersedes=["old-1"])
        postrun.ledger_append("proposal", proposal_id="new-1", path=str(new), supersedes="old-1")
        assert postrun.decide(str(new), "accept", "ok") == 0
        moved = postrun.superseded_dir() / "old-1.md"
        assert not old.exists() and moved.exists()
        assert postrun.parse_frontmatter(moved.read_text())[0]["superseded_by"] == "new-1"

    def test_completion_skips_an_id_with_supersession_history(self, postrun):
        old = _log_old(postrun)
        postrun.ledger_append("proposal_superseded", proposal_id="old-1", superseded_by="t-1")
        postrun.ledger_append("proposal_restored", proposal_id="old-1", restored_by="t-1")
        new = _write_variant(postrun, "new-1", supersedes=["old-1"])
        postrun.ledger_append("proposal", proposal_id="new-1", path=str(new), supersedes="old-1")
        assert postrun.decide(str(new), "accept", "ok") == 0
        assert old.exists()

    def test_restoring_decline_leaves_a_never_moved_id_in_place(self, postrun):
        _findings(postrun, *MEMBERS)
        old = _log_old(postrun)
        new = _write_variant(postrun, "new-1", supersedes=["old-1"])
        postrun.ledger_append("proposal", proposal_id="new-1", path=str(new), supersedes="old-1")
        assert postrun.decide(str(new), "decline", "wrong", restore_superseded=True) == 0
        assert old.exists()
        assert _events(postrun, "decision")[-1]["restored"] == ""
        assert not any(postrun.FINDINGS_DIR.glob("*.reviewed"))

    def test_cli_restore_flag_reaches_decide(self, postrun):
        old, new, _ = self._supersede(postrun)
        rc = postrun.main(["decide", "--proposal", str(new), "--kind", "decline",
                           "--reason", "x", "--restore-superseded"])
        assert rc == 0 and old.exists()

    def test_completion_refuses_a_successor_that_drops_a_member(self, postrun, capsys):
        _findings(postrun, *MEMBERS)
        old = _log_old(postrun)
        new = _write_variant(postrun, "new-1", members=MEMBERS[:1], supersedes=["old-1"])
        assert postrun.decide(str(new), "accept", "ok") == 2
        assert old.exists() and new.exists()
        assert not (postrun.superseded_dir() / "old-1.md").exists()
        assert not any(postrun.FINDINGS_DIR.glob("*.reviewed"))
        assert _events(postrun, "proposal_superseded") == []
        assert _events(postrun, "decision") == []
        assert "drops its members" in capsys.readouterr().err

    def test_completion_refuses_a_cross_lane_successor(self, postrun, capsys):
        old = _log_old(postrun, lane="frontier", evidence_kind="external", members=None)
        new = _write_variant(postrun, "new-1", supersedes=["old-1"])
        assert postrun.decide(str(new), "accept", "ok") == 2
        assert old.exists() and new.exists()
        assert not (postrun.superseded_dir() / "old-1.md").exists()
        assert _events(postrun, "decision") == []
        assert "another lane" in capsys.readouterr().err

    def test_accept_marks_no_member_carried_by_an_id_completion_skipped(self, postrun):
        _findings(postrun, *MEMBERS)
        old = _log_old(postrun)
        new = _write_variant(postrun, "new-1", supersedes=["old-1"])
        postrun.ledger_append("proposal", proposal_id="new-1", path=str(new), supersedes="old-1")
        postrun.ledger_append("proposal_apply_started", proposal_id="old-1")
        assert postrun.decide(str(new), "accept", "ok") == 0
        assert old.exists() and not new.exists()
        assert not any(postrun.FINDINGS_DIR.glob("*.reviewed"))
        assert _events(postrun, "decision")[-1]["members_marked"] == ""

    def test_accept_proposal_retire_marks_members(self, postrun):
        _findings(postrun, *MEMBERS)
        old, retire, _ = self._supersede(postrun, "retire-1", kind="proposal-retire",
                                         prose_diff=True)
        assert postrun.decide(str(retire), "accept", "stale") == 0
        assert all((postrun.FINDINGS_DIR / f"{sid}.reviewed").exists() for sid in MEMBERS)
        assert not old.exists() and (postrun.superseded_dir() / "old-1.md").exists()
        assert (postrun.ACCEPTED_DIR / "retire-1.md").exists()

    def test_undecodable_pending_file_does_not_break_decide(self, postrun):
        _findings(postrun, *MEMBERS)
        old = _log_old(postrun)
        new = _write_variant(postrun, "new-1", supersedes=["old-1"])
        postrun.ledger_append("proposal", proposal_id="new-1", path=str(new), supersedes="old-1")
        (postrun.PENDING_DIR / "garbage.md").write_bytes(b"\xff\xfe\x00garbage")
        assert postrun.decide(str(new), "accept", "ok") == 0
        assert not old.exists() and (postrun.superseded_dir() / "old-1.md").exists()

    def _stranded_supersession(self, postrun, src: Path, sid="old-1"):
        old = _log_old(postrun, sid)
        retire = _write_variant(postrun, "retire-1", kind="proposal-retire", prose_diff=True,
                                supersedes=[sid])
        postrun.ledger_append("proposal", proposal_id="retire-1", path=str(retire),
                              supersedes=sid)
        src.parent.mkdir(parents=True, exist_ok=True)
        os.replace(old, src)
        postrun._edit_frontmatter(src, add={"superseded_by": "retire-1",
                                            "superseded_run": "r-x"})
        postrun.ledger_append("proposal_superseded", proposal_id=sid, superseded_by="retire-1",
                              run_id="r-x", path=str(src), from_path=str(old))
        return old, retire

    def test_restore_skips_a_ledger_path_outside_superseded(self, postrun, tmp_path, capsys):
        stray = tmp_path / "elsewhere" / "old-1.md"
        old, retire = self._stranded_supersession(postrun, stray)
        assert postrun.decide(str(retire), "decline", "not stale") == 0
        assert stray.exists() and not old.exists() and not retire.exists()
        assert (postrun.DECLINED_DIR / "retire-1.md").exists()
        assert _events(postrun, "proposal_restored") == []
        assert _events(postrun, "decision")[-1]["not_restored"] == "old-1"
        err = capsys.readouterr().err
        assert "old-1" in err and "not restored" in err

    def test_restore_skips_a_superseded_file_whose_id_differs(self, postrun, capsys):
        src = postrun.superseded_dir() / "old-1.md"
        old, retire = self._stranded_supersession(postrun, src)
        src.write_text(src.read_text().replace("id: old-1", "id: other-1", 1))
        assert postrun.decide(str(retire), "decline", "not stale") == 0
        assert src.exists() and not old.exists() and not retire.exists()
        assert "id: other-1" in src.read_text()
        assert _events(postrun, "proposal_restored") == []
        assert _events(postrun, "decision")[-1]["not_restored"] == "old-1"
        err = capsys.readouterr().err
        assert "old-1" in err and "not restored" in err

    def test_restore_after_a_crash_before_the_stamp(self, postrun, monkeypatch):
        _findings(postrun, *MEMBERS)
        old = _log_old(postrun)
        new = _write_variant(postrun, "new-1", supersedes=["old-1"])
        real_edit = postrun._edit_frontmatter
        interrupted = []

        def interrupt_once(path, add=None, remove=()):
            if not interrupted:
                interrupted.append(path)
                raise KeyboardInterrupt
            return real_edit(path, add=add, remove=remove)

        monkeypatch.setattr(postrun, "_edit_frontmatter", interrupt_once)
        with pytest.raises(KeyboardInterrupt):
            postrun.process_run_artifacts("r-new", known=postrun.known_from_ledger())
        moved = postrun.superseded_dir() / "old-1.md"
        assert moved.exists() and not old.exists()
        assert "superseded_by" not in postrun.parse_frontmatter(moved.read_text())[0]
        assert postrun.decide(str(new), "decline", "wrong merge", restore_superseded=True) == 0
        assert old.exists() and not moved.exists()
        assert "superseded_by" not in postrun.parse_frontmatter(old.read_text())[0]
        assert not any(postrun.FINDINGS_DIR.glob("*.reviewed"))
        assert [e["proposal_id"] for e in _events(postrun, "proposal_restored")] == ["old-1"]
        assert _events(postrun, "decision")[-1]["restored"] == "old-1"

    def test_restoring_decline_warns_and_marks_none_of_an_id_whose_file_is_missing(
            self, postrun, capsys):
        _findings(postrun, *MEMBERS, EXTRA_MEMBER)
        old = _log_old(postrun)
        new = _write_variant(postrun, "new-1", members=MEMBERS + [EXTRA_MEMBER],
                             supersedes=["old-1"])
        postrun.ledger_append("proposal", proposal_id="new-1", path=str(new), supersedes="old-1")
        postrun.ledger_append("proposal_superseded", proposal_id="old-1", superseded_by="new-1",
                              run_id="r-x", path=str(postrun.superseded_dir() / "old-1.md"),
                              from_path=str(old), members=",".join(MEMBERS))
        old.unlink()
        assert postrun.decide(str(new), "decline", "wrong merge", restore_superseded=True) == 0
        assert not new.exists() and (postrun.DECLINED_DIR / "new-1.md").exists()
        assert {p.stem for p in postrun.FINDINGS_DIR.glob("*.reviewed")} == {EXTRA_MEMBER}
        assert _events(postrun, "proposal_restored") == []
        decision = _events(postrun, "decision")[-1]
        assert decision["not_restored"] == "old-1" and decision["restored"] == ""
        assert decision["members_kept"] == ",".join(MEMBERS)
        assert decision["members_marked"] == EXTRA_MEMBER
        captured = capsys.readouterr()
        assert "old-1" in captured.err and "not restored" in captured.err
        assert ("; kept unmarked (still carried by a pending proposal or by an id not "
                "restored): 2") in captured.out

    def test_accept_after_a_crash_before_the_move_marks_no_member_of_the_stranded_id(
            self, postrun, monkeypatch):
        _findings(postrun, *MEMBERS)
        old = _log_old(postrun)
        new = _write_variant(postrun, "new-1", supersedes=["old-1"])
        real_move = postrun.shutil.move
        crashed = []

        def interrupt_once(src, dst):
            if not crashed:
                crashed.append(src)
                raise KeyboardInterrupt
            return real_move(src, dst)

        monkeypatch.setattr(postrun.shutil, "move", interrupt_once)
        with pytest.raises(KeyboardInterrupt):
            postrun.process_run_artifacts("r-new", known=postrun.known_from_ledger())
        assert old.exists()
        assert [e["proposal_id"] for e in _events(postrun, "proposal_superseded")] == ["old-1"]
        assert postrun.decide(str(new), "accept", "ok") == 0
        assert old.exists() and not new.exists()
        assert not any(postrun.FINDINGS_DIR.glob("*.reviewed"))
        assert _events(postrun, "decision")[-1]["members_marked"] == ""

    def test_decline_refusal_names_the_restore_flag(self, postrun, capsys):
        _findings(postrun, *MEMBERS)
        old = _log_old(postrun)
        new = _write_variant(postrun, "new-1", members=MEMBERS[:1], supersedes=["old-1"])
        assert postrun.decide(str(new), "decline", "wrong") == 2
        assert old.exists() and new.exists()
        assert not any(postrun.FINDINGS_DIR.glob("*.reviewed"))
        assert "--restore-superseded" in capsys.readouterr().err

    def _retire_stranded_before_the_move(self, postrun, monkeypatch):
        _findings(postrun, *MEMBERS)
        old = _log_old(postrun)
        retire = _write_variant(postrun, "retire-1", kind="proposal-retire", prose_diff=True,
                                supersedes=["old-1"])
        real_move = postrun.shutil.move
        failed = []

        def fail_once(src, dst):
            if not failed:
                failed.append(src)
                raise OSError("rename failed")
            return real_move(src, dst)

        monkeypatch.setattr(postrun.shutil, "move", fail_once)
        with pytest.raises(OSError):
            postrun.process_run_artifacts("r-retire", known=postrun.known_from_ledger())
        assert old.exists() and retire.exists()
        assert [e["superseded_by"] for e in _events(postrun, "proposal_superseded")] == ["retire-1"]
        return old, retire

    def test_restoring_decline_warns_when_the_superseded_id_was_decided_since(
            self, postrun, monkeypatch, capsys):
        old, retire = self._retire_stranded_before_the_move(postrun, monkeypatch)
        assert postrun.decide(str(old), "accept", "ok") == 0
        assert (postrun.ACCEPTED_DIR / "old-1.md").exists() and not old.exists()
        assert postrun.decide(str(retire), "decline", "not stale") == 0
        assert (postrun.DECLINED_DIR / "retire-1.md").exists() and not retire.exists()
        assert not old.exists() and not (postrun.superseded_dir() / "old-1.md").exists()
        assert _events(postrun, "proposal_restored") == []
        decision = _events(postrun, "decision")[-1]
        assert decision["proposal_id"] == "retire-1"
        assert decision["not_restored"] == "old-1" and decision["restored"] == ""
        err = capsys.readouterr().err
        assert "old-1" in err and "not restored" in err

    def test_restoring_decline_warns_when_a_later_redraft_superseded_the_id(
            self, postrun, monkeypatch, capsys):
        old, retire = self._retire_stranded_before_the_move(postrun, monkeypatch)
        _write_variant(postrun, "new-2", supersedes=["old-1"])
        postrun.process_run_artifacts("r-redraft", known=postrun.known_from_ledger())
        moved = postrun.superseded_dir() / "old-1.md"
        assert moved.exists() and not old.exists()
        assert postrun.parse_frontmatter(moved.read_text())[0]["superseded_by"] == "new-2"
        assert postrun.decide(str(retire), "decline", "not stale") == 0
        assert moved.exists() and not old.exists()
        assert postrun.parse_frontmatter(moved.read_text())[0]["superseded_by"] == "new-2"
        assert (postrun.DECLINED_DIR / "retire-1.md").exists() and not retire.exists()
        assert not any(postrun.FINDINGS_DIR.glob("*.reviewed"))
        assert _events(postrun, "proposal_restored") == []
        assert _events(postrun, "decision")[-1]["not_restored"] == "old-1"
        err = capsys.readouterr().err
        assert "old-1" in err and "not restored" in err


class TestDecideProtectsSharedMembers:
    @pytest.mark.parametrize("kind", ["accept", "decline"])
    def test_plain_decide_leaves_a_member_another_pending_proposal_carries(self, postrun, kind):
        _findings(postrun, *MEMBERS, EXTRA_MEMBER)
        first = _write_variant(postrun, "a-1", members=[MEMBERS[0], MEMBERS[1]])
        second = _write_variant(postrun, "b-1", members=[MEMBERS[0], EXTRA_MEMBER])
        postrun.process_run_artifacts("r-1", known=set())
        assert postrun.decide(str(first), kind, "reason") == 0
        assert {p.stem for p in postrun.FINDINGS_DIR.glob("*.reviewed")} == {MEMBERS[1]}
        assert _events(postrun, "decision")[-1]["members_marked"] == MEMBERS[1]
        assert postrun.decide(str(second), "accept", "ok") == 0
        assert {p.stem for p in postrun.FINDINGS_DIR.glob("*.reviewed")} == {
            MEMBERS[0], MEMBERS[1], EXTRA_MEMBER}

    def test_decide_reports_the_shared_member_as_kept(self, postrun, capsys):
        _findings(postrun, *MEMBERS, EXTRA_MEMBER)
        first = _write_variant(postrun, "a-1", members=[MEMBERS[0], MEMBERS[1]])
        _write_variant(postrun, "b-1", members=[MEMBERS[0], EXTRA_MEMBER])
        postrun.process_run_artifacts("r-1", known=set())
        assert postrun.decide(str(first), "accept", "ok") == 0
        assert _events(postrun, "decision")[-1]["members_kept"] == MEMBERS[0]
        assert "; kept unmarked (still carried by a pending proposal): 1" in capsys.readouterr().out


class TestValidateSubcommand:

    def _fence_less(self, postrun, name="cr.md", declared="0", supersedes=None,
                    with_section=True):
        target = postrun.ALLOWED_TARGET_ROOTS[0] / "findings"
        target.mkdir(parents=True, exist_ok=True)
        sup = f"supersedes: [{supersedes}]\n" if supersedes else ""
        section = "\n## Supersedes\nolder: retire — spent\n" if with_section else ""
        text = (
            "---\n"
            "id: cr-1\nrun_id: r-test\ncluster: c\nlane: digest\n"
            "kind: corpus-retire\nevidence_kind: findings\n"
            "members: [0eef8c47-2bc4-41d3-84df-c61e3ec2f9d1, "
            "a2d9ddea-0320-417e-b556-d2a8a44420f2]\n"
            f"targets: [{target}]\n"
            f"always_on_bytes: {declared}\nflow_cost: none\nbase_rev: abc1234\n"
            + sup +
            "expectation: the parked remainder shrinks by 1\n"
            "check_window_days: 14\nrevert: delete the .reviewed markers\n"
            "---\n\n## Evidence\none line per member.\n\n"
            "## Diff\nNo artifact change: accepting retires the members.\n"
            + section
        )
        path = postrun.PENDING_DIR / name
        path.write_text(text)
        return path

    def test_missing_supersedes_section_is_reported(self, postrun):
        path = self._fence_less(postrun, supersedes="older", with_section=False)
        violations, _notes, _info = postrun.validate_artifact(str(path), False)
        assert any("## Supersedes" in v for v in violations)

    def test_declared_bytes_mismatch_reports_the_computed_delta(self, postrun):
        path = _write_proposal(postrun, name="m.md",
                               target=postrun.ALLOWED_TARGET_ROOTS[0] / "flows" / "f.md")
        text = path.read_text()
        text = text.replace("always_on_bytes: 9", "always_on_bytes: 593")
        text = text.replace("+++ b/rules/foo.md", "+++ b/flows/f.md")
        path.write_text(text)
        violations, _notes, info = postrun.validate_artifact(str(path), False)
        assert any("always_on_bytes mismatch" in v for v in violations)
        assert "computed +0" in info
        assert "declared 593" in info

    def test_computed_delta_is_the_diff_not_the_declaration(self, postrun):
        path = _write_proposal(postrun, name="c.md")
        _violations, _notes, info = postrun.validate_artifact(str(path), False)
        assert "declared 9" in info
        assert "computed +9" in info

    def test_valid_proposal_passes_and_prints_delta(self, postrun):
        path = _write_proposal(postrun, name="ok.md")
        violations, _notes, info = postrun.validate_artifact(str(path), False)
        assert violations == []
        assert "always_on_bytes" in info

    def test_fence_less_proposal_passes_with_na(self, postrun):
        path = self._fence_less(postrun, name="ok-cr.md")
        violations, _notes, info = postrun.validate_artifact(str(path), False)
        assert violations == []
        assert "n/a (no diff fence)" in info

    def test_check_artifact_validates(self, postrun):
        good = postrun.CHECKS_DIR / "c1.md"
        good.write_text("---\nid: c1\nrun_id: r\nexpectation: x\n"
                        "check_window_days: 30\n---\n\nbody\n")
        assert postrun.validate_artifact(str(good), True)[0] == []
        bad = postrun.CHECKS_DIR / "c2.md"
        bad.write_text("---\nid: c2\nrun_id: r\n---\n\nbody\n")
        assert any("expectation" in v
                   for v in postrun.validate_artifact(str(bad), True)[0])

    def test_validate_agrees_with_the_gate_on_independent_proposals(self, postrun):
        good = _write_proposal(postrun, name="a.md")
        bad = _write_proposal(postrun, name="b.md", drop_field="revert")
        pre = {p.name: postrun.validate_artifact(str(p), False)[0] == []
               for p in (good, bad)}
        postrun.process_run_artifacts("r-gate", set())
        post = {"a.md": (postrun.PENDING_DIR / "a.md").is_file(),
                "b.md": (postrun.PENDING_DIR / "b.md").is_file()}
        assert pre == post

    def test_known_divergence_two_proposals_one_supersedes_id(self, postrun):
        older = _write_proposal(postrun, name="older.md")
        postrun.ledger_append("proposal", proposal_id="older",
                              path=str(older), lane="digest")
        first = self._fence_less(postrun, name="s1.md", supersedes="older")
        second = self._fence_less(postrun, name="s2.md", supersedes="older")
        assert postrun.validate_artifact(str(first), False)[0] == []
        assert postrun.validate_artifact(str(second), False)[0] == []
        postrun.process_run_artifacts("r-div", {"older.md"})
        survivors = sorted(p.name for p in postrun.PENDING_DIR.glob("s*.md"))
        assert survivors == ["s1.md"]

    def test_validate_mutates_nothing(self, postrun, tmp_path):
        path = _write_proposal(postrun, name="ro.md")
        postrun.LEDGER_PATH.write_text("")

        def snapshot():
            return sorted(
                (str(p.relative_to(tmp_path)), p.stat().st_size, p.stat().st_mtime_ns)
                for p in tmp_path.rglob("*") if p.is_file()
            )

        before = snapshot()
        postrun.validate_artifact(str(path), False)
        assert snapshot() == before

    def test_non_integer_bytes_rejected_without_a_diff_fence(self, postrun):
        path = self._fence_less(postrun, name="bad-bytes.md", declared="unknown")
        violations, _notes, _info = postrun.validate_artifact(str(path), False)
        assert any("not an integer" in v for v in violations)

    def test_gate_quarantines_non_integer_bytes_without_a_fence(self, postrun):
        self._fence_less(postrun, name="bad-bytes-gate.md", declared="unknown")
        postrun.process_run_artifacts("r-int", set())
        assert not (postrun.PENDING_DIR / "bad-bytes-gate.md").is_file()
        assert (postrun.REJECTED_DIR / "bad-bytes-gate.md").is_file()

    def test_backpressure_warning_names_the_streak_and_zero_negative(self, postrun, capsys):
        for i in range(3):
            postrun.record_backpressure(f"r{i}", 2, 0)
        err = capsys.readouterr().err
        assert "back-pressure violation" in err
        assert "3 consecutive" in err
        assert "zero negative-byte proposals" in err
        assert "only grew" not in err

    def test_non_utf8_artifact_exits_2_instead_of_raising(self, postrun):
        path = postrun.PENDING_DIR / "binary.md"
        path.write_bytes(b"\xff\xfe\x00binary")
        assert postrun.main(["validate", "--proposal", str(path)]) == 2

    def test_empty_proposal_target_reports_instead_of_raising(self, postrun):
        assert postrun.main(["validate", "--proposal", ""]) == 2

    def test_main_returns_0_on_a_clean_proposal(self, postrun):
        path = _write_proposal(postrun, name="clean.md")
        assert postrun.main(["validate", "--proposal", str(path)]) == 0

    def test_main_returns_1_and_prints_violations_on_stdout(self, postrun, capsys):
        path = _write_proposal(postrun, name="dirty.md", drop_field="revert")
        assert postrun.main(["validate", "--proposal", str(path)]) == 1
        out = capsys.readouterr().out
        assert "missing required field: revert" in out

    def test_main_check_flag_dispatches_to_the_check_path(self, postrun, capsys):
        bad = postrun.CHECKS_DIR / "c3.md"
        bad.write_text("---\nid: c3\nrun_id: r\n---\n\nbody\n")
        assert postrun.main(["validate", "--check", str(bad)]) == 1
        out = capsys.readouterr().out
        assert "missing required field: expectation" in out

    def test_skipped_env_apply_check_still_exits_0_with_a_note(self, postrun, monkeypatch, capsys):
        path = _write_proposal(postrun, name="env.md")
        monkeypatch.setattr(postrun, "_apply_check",
                            lambda path_str, body: ("skipped-env", "boom"))
        assert postrun.main(["validate", "--proposal", str(path)]) == 0
        out = capsys.readouterr().out
        assert "NOTE apply-check skipped (environment): boom" in out
