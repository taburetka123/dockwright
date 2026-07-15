"""Tests for the selffix-trigger.sh 14d findings reaper.

Contract (arch-soundness review A1 / state-stores IMPORTANT-3): unreviewed
findings are never age-pruned — they are the Gardener's input corpus and
pending proposals reference them by basename. A finding is deleted only after
review (its `.reviewed` sibling exists) and only once the review itself is
older than 14 days. Dedup markers keep the plain 14d prune.

Same harness as test_selffix_detect.py: the repo copy at
deploy/scripts/selffix-trigger.sh is the source of truth; tests exec
it directly under a tmp $HOME, no install required.
"""
import os
import time
from pathlib import Path

from tests.test_selffix_detect import (  # noqa: F401  (selffix is a fixture)
    _invoke,
    _user_text,
    _write_transcript,
    selffix,
)

OLD = time.time() - 20 * 86400
FRESH = time.time() - 60


def _findings_dir(selffix) -> Path:
    d = selffix["home"] / ".claude" / "dockwright" / "selffix" / "findings"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_finding(selffix, name: str, md_age: float, reviewed_age: float | None = None) -> Path:
    d = _findings_dir(selffix)
    md = d / f"{name}.md"
    md.write_text(f"## Self-Fix Findings\n{name}\n")
    os.utime(md, (md_age, md_age))
    if reviewed_age is not None:
        marker = d / f"{name}.reviewed"
        marker.touch()
        os.utime(marker, (reviewed_age, reviewed_age))
    return md


def _fire(selffix) -> None:
    transcript = _write_transcript(selffix["home"], "sid-prune", [_user_text("hello")])
    _invoke(selffix, "sid-prune", transcript)


def test_prune_keeps_old_unreviewed_findings(selffix):
    """An unreviewed finding is never deleted, no matter how old."""
    md = _write_finding(selffix, "old-unreviewed", md_age=OLD)
    _fire(selffix)
    assert md.is_file(), "unreviewed finding was pruned — Gardener input corpus destroyed"


def test_prune_deletes_pairs_reviewed_over_14d_ago(selffix):
    md = _write_finding(selffix, "old-reviewed", md_age=OLD, reviewed_age=OLD)
    _fire(selffix)
    assert not md.exists()
    assert not md.with_suffix(".reviewed").exists()


def test_prune_keeps_recently_reviewed_pairs(selffix):
    """Old finding, fresh review: retention clock starts at review time."""
    md = _write_finding(selffix, "fresh-review", md_age=OLD, reviewed_age=FRESH)
    _fire(selffix)
    assert md.is_file()
    assert md.with_suffix(".reviewed").is_file()


def test_prune_deletes_orphan_reviewed_markers(selffix):
    d = _findings_dir(selffix)
    orphan = d / "orphan.reviewed"
    orphan.touch()
    os.utime(orphan, (OLD, OLD))
    _fire(selffix)
    assert not orphan.exists()


def test_prune_keeps_fresh_findings(selffix):
    md = _write_finding(selffix, "fresh", md_age=FRESH)
    _fire(selffix)
    assert md.is_file()


def test_prune_dedup_markers_still_age_out(selffix):
    dedup = _findings_dir(selffix) / ".dedup"
    dedup.mkdir(parents=True, exist_ok=True)
    old_marker = dedup / "abc123"
    old_marker.touch()
    os.utime(old_marker, (OLD, OLD))
    fresh_marker = dedup / "def456"
    fresh_marker.touch()
    _fire(selffix)
    assert not old_marker.exists()
    assert fresh_marker.exists()
