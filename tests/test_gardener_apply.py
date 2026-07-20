"""Tests for deploy/scripts/gardener_apply.py (T11 actuator)."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "deploy" / "scripts"


@pytest.fixture()
def mod(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "gardener_apply_under_test", SCRIPTS / "gardener_apply.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def postrun_of(mod):
    # the instance gardener_apply actually bound (spec-review I3)
    return sys.modules["gardener_postrun"]


def make_proposal(tmp_path, targets, diff_body, meta_extra=""):
    p = tmp_path / "prop.md"
    tlist = ", ".join(targets)
    p.write_text(
        "---\n"
        "id: r1-1\nrun_id: r1\ncluster: c\nlane: digest\n"
        "evidence_kind: ops\nkind: rule-edit\nalways_on_bytes: 0\n"
        f"base_rev: abc1234\ntargets: [{tlist}]\n"
        "expectation: e\ncheck_window_days: 7\nrevert: r\n"
        f"{meta_extra}"
        "---\n\n## Evidence\nE\n\n## Diff\n```diff\n" + diff_body + "```\n\n## Rationale\nR\n")
    return p


DIFF_MOD = (
    "--- a/rules/x.md\n"
    "+++ b/rules/x.md\n"
    "@@ -1,2 +1,2 @@\n"
    "-old line\n"
    "+new line\n"
    " keep\n")


def test_extract_diff_text_missing_fence_is_code2(mod, tmp_path):
    with pytest.raises(mod.ApplyError) as exc:
        mod.extract_diff_text("## Diff\nplain prose, no fence\n")
    assert exc.value.code == 2


def test_split_file_diffs_counts_hunk_lines(mod):
    """A removed line "--- a/foo" immediately followed by an added line
    "+++ b/foo" (i.e. hunk CONTENT that is itself byte-for-byte a
    diff file-header pair) must NOT be misread as opening a second file
    diff — the splitter tracks @@ hunk-line counts, not header lookalikes.
    A naive pair-scanner (next "--- " line followed by a "+++ " line, with
    no @@-count tracking) would split this single-file diff into two.

    RED-proof (manually verified, not re-asserted here): with the inner
    hunk-count `while` loop's condition hardcoded to `False` (so
    `@@ -1,1 +1,1 @@` consumes zero hunk lines), this exact input splits
    into 2 FileDiffs instead of 1 — the "--- a/foo"/"+++ b/foo" pair gets
    read as a second file's header. Restored after confirming the failure.
    """
    text = "--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n--- a/foo\n+++ b/foo\n"
    diffs = mod.split_file_diffs(text)
    assert len(diffs) == 1
    assert diffs[0].old_raw == "a/f"
    assert diffs[0].new_raw == "b/f"
    assert diffs[0].hunks == ["@@ -1,1 +1,1 @@", "--- a/foo", "+++ b/foo"]


def test_split_file_diffs_keeps_no_newline_markers_verbatim(mod):
    # marker mid-hunk (after a removed line) and trailing (after the last
    # added line) — both must survive verbatim and neither is counted
    # toward the @@ old/new line totals.
    text = (
        "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n"
        "-old1\n\\ No newline at end of file\n-old2\n"
        "+new1\n+new2\n\\ No newline at end of file\n"
    )
    diffs = mod.split_file_diffs(text)
    assert len(diffs) == 1
    assert diffs[0].hunks == [
        "@@ -1,2 +1,2 @@",
        "-old1",
        "\\ No newline at end of file",
        "-old2",
        "+new1",
        "+new2",
        "\\ No newline at end of file",
    ]


def test_split_file_diffs_rejects_dropped_hunk(mod):
    """C1: a stray line (here a blank line) between two hunks of the SAME
    file must not silently truncate the patch to just the first hunk.

    RED-PROOF (manually verified, not re-asserted here): before the fix,
    this exact input — two context hunks against a 12-line file, separated
    by a blank line — split to a single FileDiff carrying only hunk 1;
    `apply` would report success having silently dropped hunk 2 (`git apply
    --check` validates only the already-truncated patch, so it is NOT a
    net for this). After the fix, `split_file_diffs` raises ApplyError
    code=2 naming the header/parsed-hunk-count mismatch (2 vs 1)."""
    text = (
        "--- a/f\n+++ b/f\n"
        "@@ -1,3 +1,3 @@\n"
        " line1\n-line2\n+line2mod\n line3\n"
        "\n"
        "@@ -10,3 +10,3 @@\n"
        " line10\n-line11\n+line11mod\n line12\n"
    )
    with pytest.raises(mod.ApplyError) as exc:
        mod.split_file_diffs(text)
    assert exc.value.code == 2
    assert "2 hunk header" in str(exc.value)
    assert "only 1 were parsed" in str(exc.value)


def test_split_file_diffs_clean_two_hunks_same_file_not_tripped(mod):
    """C1 regression guard: a LEGITIMATE two-hunk single-file diff (no stray
    line between hunks) must parse to ONE FileDiff carrying BOTH @@ headers
    and must NOT trip the fail-closed header-count check. Pins that the C1
    guard fires on truncation only, never on valid multi-hunk diffs."""
    text = (
        "--- a/f\n+++ b/f\n"
        "@@ -1,3 +1,3 @@\n"
        " line1\n-line2\n+line2mod\n line3\n"
        "@@ -10,3 +10,3 @@\n"
        " line10\n-line11\n+line11mod\n line12\n"
    )
    diffs = mod.split_file_diffs(text)
    assert len(diffs) == 1
    assert sum(1 for h in diffs[0].hunks if h.startswith("@@")) == 2


def test_split_file_diffs_empty_header_path_guarded(mod):
    text = "--- \n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n"
    with pytest.raises(mod.ApplyError) as exc:
        mod.split_file_diffs(text)
    assert exc.value.code == 2
    assert "malformed diff header" in str(exc.value)


def test_split_file_diffs_drops_git_noise_between_files(mod):
    text = (
        "diff --git a/f1 b/f1\nindex 111..222 100644\n"
        "--- a/f1\n+++ b/f1\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/f2 b/f2\nnew file mode 100644\nindex 000..333\n"
        "--- /dev/null\n+++ b/f2\n@@ -0,0 +1 @@\n+c\n")
    diffs = mod.split_file_diffs(text)
    assert [d.new_raw for d in diffs] == ["b/f1", "b/f2"]
    assert diffs[0].old_raw == "a/f1"
    assert diffs[1].old_raw == "/dev/null"
    joined = "\n".join(diffs[0].hunks + diffs[1].hunks)
    assert "index" not in joined and "diff --git" not in joined


def test_build_patches_rewrites_relative_to_root(mod, postrun_of, tmp_path, monkeypatch):
    root = tmp_path / "claude"
    (root / "rules").mkdir(parents=True)
    (root / "rules" / "x.md").write_text("old line\nkeep\n")
    monkeypatch.setattr(postrun_of, "ALLOWED_TARGET_ROOTS", [root])
    target = str(root / "rules" / "x.md")
    diffs = mod.split_file_diffs(DIFF_MOD)
    patches, files = mod.build_patches(diffs, [target])
    real_root = os.path.realpath(str(root))
    assert list(patches) == [real_root]
    assert files[real_root] == [os.path.join("rules", "x.md")]
    assert "--- a/rules/x.md" in patches[real_root]
    assert "+++ b/rules/x.md" in patches[real_root]


def test_build_patches_new_file_dev_null(mod, postrun_of, tmp_path, monkeypatch):
    root = tmp_path / "claude"
    root.mkdir()
    monkeypatch.setattr(postrun_of, "ALLOWED_TARGET_ROOTS", [root])
    dest = str(root / "rules" / "new.md")
    text = f"--- /dev/null\n+++ {dest}\n@@ -0,0 +1 @@\n+hello\n"
    patches, _files = mod.build_patches(mod.split_file_diffs(text), [dest])
    patch = list(patches.values())[0]
    assert "--- /dev/null" in patch
    assert "+++ b/rules/new.md" in patch


def test_build_patches_outside_roots_refused(mod, postrun_of, tmp_path, monkeypatch):
    monkeypatch.setattr(postrun_of, "ALLOWED_TARGET_ROOTS", [tmp_path / "claude"])
    evil = str(tmp_path / "elsewhere" / "f.md")
    text = f"--- {evil}\n+++ {evil}\n@@ -1 +1 @@\n-a\n+b\n"
    with pytest.raises(mod.ApplyError) as exc:
        mod.build_patches(mod.split_file_diffs(text), [evil])
    assert exc.value.code == 2
    assert "FR-8" in str(exc.value)


def test_build_patches_rename_refused(mod, tmp_path):
    old_target = str(tmp_path / "claude" / "rules" / "old.md")
    new_target = str(tmp_path / "claude" / "rules" / "new.md")
    text = "--- a/rules/old.md\n+++ b/rules/new.md\n@@ -1 +1 @@\n-a\n+b\n"
    diffs = mod.split_file_diffs(text)
    with pytest.raises(mod.ApplyError) as exc:
        mod.build_patches(diffs, [old_target, new_target])
    assert exc.value.code == 2
    assert "rename diffs are not supported" in str(exc.value)


def test_build_patches_dev_null_both_sides_refused(mod):
    text = "--- /dev/null\n+++ /dev/null\n@@ -1 +1 @@\n-a\n+a\n"
    diffs = mod.split_file_diffs(text)
    with pytest.raises(mod.ApplyError) as exc:
        mod.build_patches(diffs, [])
    assert exc.value.code == 2
    assert "/dev/null on both sides" in str(exc.value)


def test_build_patches_deleted_file(mod, postrun_of, tmp_path, monkeypatch):
    root = tmp_path / "claude"
    (root / "rules").mkdir(parents=True)
    monkeypatch.setattr(postrun_of, "ALLOWED_TARGET_ROOTS", [root])
    target = str(root / "rules" / "gone.md")
    text = f"--- {target}\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"
    diffs = mod.split_file_diffs(text)
    patches, files = mod.build_patches(diffs, [target])
    real_root = os.path.realpath(str(root))
    patch = patches[real_root]
    assert "--- a/rules/gone.md" in patch
    assert "+++ /dev/null" in patch
    assert files[real_root] == [os.path.join("rules", "gone.md")]


def test_resolve_reads_targets_and_meta(mod, postrun_of, tmp_path, monkeypatch):
    root = tmp_path / "claude"
    (root / "rules").mkdir(parents=True)
    monkeypatch.setattr(postrun_of, "ALLOWED_TARGET_ROOTS", [root])
    prop = make_proposal(tmp_path, [str(root / "rules" / "x.md")], DIFF_MOD)
    meta, patches, files = mod.resolve(str(prop))
    assert meta["id"] == "r1-1"
    assert len(patches) == 1


def git(root, *args, **kw):
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, **kw)


@pytest.fixture()
def git_root(tmp_path):
    root = tmp_path / "claude"
    (root / "rules").mkdir(parents=True)
    (root / "rules" / "x.md").write_text("old line\nkeep\n")
    git(root, "init", "-q")
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return root


@pytest.fixture()
def wired(mod, postrun_of, git_root, tmp_path, monkeypatch):
    monkeypatch.setattr(postrun_of, "ALLOWED_TARGET_ROOTS", [git_root])
    monkeypatch.setattr(postrun_of, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    return mod


def events(postrun_of):
    p = postrun_of.LEDGER_PATH
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def test_apply_clean_and_revert_roundtrip(wired, postrun_of, git_root, tmp_path):
    prop = make_proposal(tmp_path, [str(git_root / "rules" / "x.md")], DIFF_MOD)
    assert wired.main(["check", "--proposal", str(prop)]) == 0
    assert wired.main(["apply", "--proposal", str(prop)]) == 0
    assert (git_root / "rules" / "x.md").read_text() == "new line\nkeep\n"
    evs = events(postrun_of)
    applied = [e for e in evs if e["type"] == "proposal_applied"]
    assert applied and applied[-1]["proposal_id"] == "r1-1"
    assert "path" not in applied[-1]          # I1: never a top-level path key
    assert wired.main(["revert", "--proposal", str(prop)]) == 0
    assert (git_root / "rules" / "x.md").read_text() == "old line\nkeep\n"
    assert git(git_root, "status", "--porcelain").stdout.strip() == ""
    reverted = [e for e in events(postrun_of) if e["type"] == "proposal_reverted"]
    assert reverted and "path" not in reverted[-1]


def test_no_path_key_proven_red(wired, postrun_of, git_root, tmp_path):
    """Drift-guard discipline: prove the no-path assertion actually bites by
    emitting a doctored event through the same ledger and asserting the
    checker notices. (The guarded property lives in executed code — the
    ledger_append call sites — not in prose.)"""
    postrun_of.ledger_append("proposal_applied", proposal_id="x", path="/tmp/leak")
    evs = events(postrun_of)
    assert any("path" in e for e in evs if e["type"] == "proposal_applied")


def test_apply_context_mismatch_blocks_and_leaves_tree_untouched(
        wired, postrun_of, git_root, tmp_path):
    (git_root / "rules" / "x.md").write_text("drifted content\n")
    git(git_root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-aqm", "drift")
    prop = make_proposal(tmp_path, [str(git_root / "rules" / "x.md")], DIFF_MOD)
    assert wired.main(["apply", "--proposal", str(prop)]) == 1
    assert (git_root / "rules" / "x.md").read_text() == "drifted content\n"
    failed = [e for e in events(postrun_of) if e["type"] == "proposal_apply_failed"]
    assert failed and "path" not in failed[-1]


def test_ensure_clean_git_status_failure_is_fail_closed(mod, tmp_path):
    """M2: a nonzero `git status` returncode must fail closed, not pass
    silently just because stdout happened to be empty."""
    root = tmp_path / "not_a_repo"
    root.mkdir()
    with pytest.raises(mod.ApplyError) as exc:
        mod.ensure_clean(str(root), ["x.md"], False)
    assert exc.value.code == 1
    assert "git status failed" in str(exc.value)


def test_apply_dirty_target_refused(wired, git_root, tmp_path):
    (git_root / "rules" / "x.md").write_text("old line\nkeep\nuncommitted\n")
    prop = make_proposal(tmp_path, [str(git_root / "rules" / "x.md")], DIFF_MOD)
    assert wired.main(["apply", "--proposal", str(prop)]) == 1


def test_apply_dirty_target_force_dirty_succeeds(wired, git_root, tmp_path):
    (git_root / "rules" / "x.md").write_text("old line\nkeep\nuncommitted\n")
    prop = make_proposal(tmp_path, [str(git_root / "rules" / "x.md")], DIFF_MOD)
    assert wired.main(["apply", "--proposal", str(prop), "--force-dirty"]) == 0
    assert (git_root / "rules" / "x.md").read_text() == "new line\nkeep\nuncommitted\n"


def test_apply_rollback_failure_surfaces_loud_message(
        wired, postrun_of, git_root, tmp_path, monkeypatch, capsys):
    """When the mid-apply failure path's own reverse-apply also fails, the
    raised ApplyError message must say so loudly instead of silently
    claiming the earlier root(s) were reverted."""
    root2 = tmp_path / "claude2"
    (root2 / "rules").mkdir(parents=True)
    (root2 / "rules" / "y.md").write_text("old line\nkeep\n")
    git(root2, "init", "-q")
    git(root2, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    git(root2, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    monkeypatch.setattr(postrun_of, "ALLOWED_TARGET_ROOTS", [git_root, root2])

    diff2 = (
        "--- a/rules/y.md\n+++ b/rules/y.md\n@@ -1,2 +1,2 @@\n"
        "-old line\n+new line\n keep\n")
    prop = make_proposal(
        tmp_path,
        [str(git_root / "rules" / "x.md"), str(root2 / "rules" / "y.md")],
        DIFF_MOD + diff2)

    real_git_root = os.path.realpath(str(git_root))

    class FakeProc:
        def __init__(self, returncode, stderr=""):
            self.returncode = returncode
            self.stderr = stderr

    def fake_git_apply(root, patch, check=False, reverse=False):
        if check:
            return FakeProc(0)                      # both context-checks pass
        if reverse:
            return FakeProc(1, "cannot revert")      # rollback itself fails
        if root == real_git_root:
            return FakeProc(0)                       # first root applies fine
        return FakeProc(1, "apply boom")              # second root fails

    monkeypatch.setattr(wired, "git_apply", fake_git_apply)
    rc = wired.main(["apply", "--proposal", str(prop)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ROLLBACK OF" in err
    assert "FAILED" in err and "inspect git status" in err


def test_apply_non_git_root_refused(mod, postrun_of, tmp_path, monkeypatch):
    root = tmp_path / "plain"
    (root / "rules").mkdir(parents=True)
    (root / "rules" / "x.md").write_text("old line\nkeep\n")
    monkeypatch.setattr(postrun_of, "ALLOWED_TARGET_ROOTS", [root])
    monkeypatch.setattr(postrun_of, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    prop = make_proposal(tmp_path, [str(root / "rules" / "x.md")], DIFF_MOD)
    assert mod.main(["apply", "--proposal", str(prop)]) == 1


def test_apply_new_asset_creates_file_and_parent_dirs(wired, git_root, tmp_path):
    dest = str(git_root / "flows" / "new.md")   # flows/ does NOT exist yet —
    diff = f"--- /dev/null\n+++ {dest}\n@@ -0,0 +1,2 @@\n+hello\n+world\n"
    prop = make_proposal(tmp_path, [dest], diff)  # git apply creates leading dirs
    assert wired.main(["apply", "--proposal", str(prop)]) == 0
    assert (git_root / "flows" / "new.md").read_text() == "hello\nworld\n"


def test_base_rev_mismatch_warns_but_applies(wired, git_root, tmp_path, capsys):
    prop = make_proposal(tmp_path, [str(git_root / "rules" / "x.md")], DIFF_MOD)
    assert wired.main(["apply", "--proposal", str(prop)]) == 0
    assert "WARNING" in capsys.readouterr().out  # base_rev abc1234 != real HEAD


def test_prose_new_asset_distinct_error(wired, git_root, tmp_path, capsys):
    p = tmp_path / "prose.md"
    p.write_text("---\nid: r1-2\ntargets: [" + str(git_root / "r.md") + "]\n---\n"
                 "## Diff\nfull file content as prose\n")
    assert wired.main(["apply", "--proposal", str(p)]) == 2
    assert "pre-T11" in capsys.readouterr().err
