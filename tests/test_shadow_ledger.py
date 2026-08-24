"""Tests for deploy/scripts/shadow_ledger.py (Phase D T12).

Guards proven RED first (drift-guard-tests.md). The lessons under
test: criteria immutable via the FIRST ledger stamp (a hand-edited
criteria.json is caught by verbatim compare), and no criterion can pass
silently without observations."""
import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "shadow_ledger.py"


def _load():
    spec = importlib.util.spec_from_file_location("shadow_ledger", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sl = _load()

CRITERIA = {"min_n": 4, "min_used_rate": 0.75, "min_window_days": 14, "min_abstained": 0}


def arm(tmp_path, criteria=None):
    return sl.main(["arm", "--lane", "demo", "--criteria",
                    json.dumps(criteria or CRITERIA), "--shadow-dir", str(tmp_path)])


def append(tmp_path, draft_id, disposition, at=None):
    argv = ["append", "--lane", "demo", "--id", draft_id,
            "--disposition", disposition, "--shadow-dir", str(tmp_path)]
    if at is not None:
        argv += ["--now", str(at)]
    return sl.main(argv)


def report(tmp_path, *extra):
    return sl.main(["report", "--lane", "demo", "--shadow-dir", str(tmp_path)] + list(extra))


class TestArm:
    def test_arm_writes_criteria_and_stamp(self, tmp_path):
        assert arm(tmp_path) == 0
        assert json.loads((tmp_path / "demo.criteria.json").read_text()) == CRITERIA
        events = [json.loads(l) for l in (tmp_path / "demo.jsonl").read_text().splitlines()]
        assert events[0]["type"] == "criteria_armed"

    def test_rearm_same_is_idempotent(self, tmp_path):
        assert arm(tmp_path) == 0
        assert arm(tmp_path) == 0

    def test_rearm_restores_deleted_criteria_file(self, tmp_path):
        # M-c: an idempotent re-arm whose stamp matches must rewrite a missing
        # criteria file (not just print ok) so report works again.
        assert arm(tmp_path) == 0
        (tmp_path / "demo.criteria.json").unlink()
        assert arm(tmp_path) == 0
        assert (tmp_path / "demo.criteria.json").exists()
        assert json.loads((tmp_path / "demo.criteria.json").read_text()) == CRITERIA
        assert report(tmp_path) == 0  # criteria matches the stamp again

    def test_rearm_different_exit2(self, tmp_path):
        assert arm(tmp_path) == 0
        weaker = dict(CRITERIA, min_used_rate=0.1)
        assert arm(tmp_path, weaker) == 2

    def test_unknown_criteria_key_exit2(self, tmp_path):
        assert arm(tmp_path, dict(CRITERIA, hallucinated=1)) == 2

    def test_missing_criteria_key_exit2(self, tmp_path):
        partial = {k: v for k, v in CRITERIA.items() if k != "min_abstained"}
        assert arm(tmp_path, partial) == 2


class TestAppend:
    def test_append_before_arm_exit2(self, tmp_path):
        assert append(tmp_path, "d1", "used") == 2

    def test_append_valid_dispositions(self, tmp_path):
        arm(tmp_path)
        for i, d in enumerate(["used", "edited", "discarded", "abstained"]):
            assert append(tmp_path, f"d{i}", d) == 0

    def test_append_unknown_disposition_exit2(self, tmp_path):
        arm(tmp_path)
        assert append(tmp_path, "d1", "shipped") == 2


class TestReport:
    def test_empty_lane_not_yet_exit0_but_require_graduate_exit2(self, tmp_path, capsys):
        arm(tmp_path)
        assert report(tmp_path) == 0
        assert "NOT-YET" in capsys.readouterr().out
        assert report(tmp_path, "--require-graduate") == 2

    def test_graduates_when_all_criteria_met(self, tmp_path, capsys):
        arm(tmp_path)
        t0 = 1_000_000.0
        for i, d in enumerate(["used", "used", "used", "edited"]):
            append(tmp_path, f"d{i}", d, at=t0)
        # 3/4 used = 0.75, window 15d, min_abstained 0 satisfied.
        assert report(tmp_path, "--now", str(t0 + 15 * 86400)) == 0
        assert "GRADUATE" in capsys.readouterr().out
        assert report(tmp_path, "--require-graduate",
                      "--now", str(t0 + 15 * 86400)) == 0

    def test_edited_counts_against_used_rate(self, tmp_path, capsys):
        arm(tmp_path)
        t0 = 1_000_000.0
        for i, d in enumerate(["used", "used", "edited", "edited"]):
            append(tmp_path, f"d{i}", d, at=t0)
        assert report(tmp_path, "--require-graduate",
                      "--now", str(t0 + 15 * 86400)) == 1  # 0.5 < 0.75: NOT-YET

    def test_window_not_matured_not_yet(self, tmp_path):
        arm(tmp_path)
        t0 = 1_000_000.0
        for i in range(4):
            append(tmp_path, f"d{i}", "used", at=t0)
        assert report(tmp_path, "--require-graduate",
                      "--now", str(t0 + 5 * 86400)) == 1

    def test_abstained_outside_denominator_but_counted_for_min_abstained(self, tmp_path, capsys):
        arm(tmp_path, dict(CRITERIA, min_abstained=1))
        t0 = 1_000_000.0
        for i, d in enumerate(["used", "used", "used", "edited", "abstained"]):
            append(tmp_path, f"d{i}", d, at=t0)
        assert report(tmp_path, "--require-graduate",
                      "--now", str(t0 + 15 * 86400)) == 0
        out = capsys.readouterr().out
        assert "min_n: 4/4" in out  # abstained NOT in the denominator

    def test_tampered_criteria_file_exit2(self, tmp_path):
        arm(tmp_path)
        weakened = dict(CRITERIA, min_used_rate=0.01)
        (tmp_path / "demo.criteria.json").write_text(json.dumps(weakened))
        assert report(tmp_path) == 2  # verbatim stamp compare catches the edit

    def test_missing_criteria_file_exit2(self, tmp_path):
        arm(tmp_path)
        (tmp_path / "demo.criteria.json").unlink()
        assert report(tmp_path) == 2

    def test_unknown_disposition_line_exit2(self, tmp_path):
        arm(tmp_path)
        append(tmp_path, "d1", "used")
        with (tmp_path / "demo.jsonl").open("a") as fh:
            fh.write(json.dumps({"type": "disposition", "v": 1, "ts": 1.0,
                                 "lane": "demo", "id": "dX",
                                 "disposition": "shipped"}) + "\n")
        assert report(tmp_path) == 2

    def test_corrupt_ledger_line_report_exit2(self, tmp_path, capsys):
        # M-d: a non-blank undecodable ledger line must fail closed (exit 2),
        # not be silently skipped and a verdict computed over a damaged ledger.
        arm(tmp_path)
        append(tmp_path, "d1", "used")
        with (tmp_path / "demo.jsonl").open("a") as fh:
            fh.write('{"type": "disposi')  # truncated, non-blank
        capsys.readouterr()
        assert report(tmp_path) == 2
        assert "line" in capsys.readouterr().err

    def test_corrupt_ledger_line_report_blank_ok(self, tmp_path):
        # M-d boundary: blank lines are not anomalies.
        arm(tmp_path)
        append(tmp_path, "d1", "used")
        with (tmp_path / "demo.jsonl").open("a") as fh:
            fh.write("\n  \n")
        assert report(tmp_path) == 0

    def test_per_criterion_counts_printed(self, tmp_path, capsys):
        arm(tmp_path)
        append(tmp_path, "d1", "used", at=1_000_000.0)
        report(tmp_path, "--now", str(1_000_000.0 + 86400))
        out = capsys.readouterr().out
        assert "min_n: 1/4" in out and "min_used_rate" in out and "min_window_days" in out

    def test_all_abstained_lane_require_graduate_exit2_with_accurate_message(self, tmp_path, capsys):
        arm(tmp_path)
        t0 = 1_000_000.0
        append(tmp_path, "d1", "abstained", at=t0)
        append(tmp_path, "d2", "abstained", at=t0)
        assert report(tmp_path, "--require-graduate", "--now", str(t0 + 86400)) == 2
        err = capsys.readouterr().err
        assert "all abstained" in err and "never ran" not in err
