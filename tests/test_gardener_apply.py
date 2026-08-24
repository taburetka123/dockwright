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
    # apply left x.md uncommitted-dirty; revert now dirty-checks (M2), so the
    # immediate undo needs --force-dirty (the documented flow commits first).
    assert wired.main(["revert", "--proposal", str(prop), "--force-dirty"]) == 0
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


def test_revert_dirty_target_refused_and_force_dirty(wired, git_root, tmp_path):
    """M2: revert dirty-checks symmetric with apply. An unrelated uncommitted
    edit refuses a plain revert; --force-dirty overrides. (The canon gate stays
    OFF for revert by design — reverting can legitimately restore a repo to a
    pre-existing red state.)"""
    prop = make_proposal(tmp_path, [str(git_root / "rules" / "x.md")], DIFF_MOD)
    assert wired.main(["apply", "--proposal", str(prop)]) == 0
    git(git_root, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-aqm", "applied")            # clean baseline for the revert
    (git_root / "rules" / "x.md").write_text("new line\nkeep\nunrelated\n")
    assert wired.main(["revert", "--proposal", str(prop)]) == 1
    assert wired.main(
        ["revert", "--proposal", str(prop), "--force-dirty"]) == 0


def test_apply_rollback_failure_surfaces_loud_message(
        wired, postrun_of, git_root, tmp_path, monkeypatch, capsys):
    """When a mid-apply failure triggers the uniform snapshot restore and
    that restore ITSELF fails, the raised ApplyError must say so loudly
    ("ROLLBACK OF <path> FAILED — inspect git status") instead of silently
    claiming the tree was restored. (Task-3 mechanism: the per-root
    `git apply -R` rollback became a byte-exact snapshot restore; the loud
    report keys on `_restore` returning a non-empty failed-path list.)"""
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
        if root == real_git_root:
            return FakeProc(0)                       # first root applies fine
        return FakeProc(1, "apply boom")              # second root fails

    monkeypatch.setattr(wired, "git_apply", fake_git_apply)
    # the uniform snapshot restore itself fails for every touched file
    monkeypatch.setattr(wired, "_restore", lambda snapshot: list(snapshot))
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


# ---- currency (review-time staleness probe) ------------------------------

def make_currency_proposal(tmp_path, targets, base_rev="abc1234",
                           kind="rule-edit", pid="cur-1", with_diff=True):
    p = tmp_path / (pid + ".md")
    tlist = ", ".join(targets)
    diff = ("## Diff\n```diff\n" + DIFF_MOD + "```\n" if with_diff
            else "## Diff\nprose brief, no fence\n")
    p.write_text(
        "---\n"
        f"id: {pid}\nrun_id: r1\ncluster: c\nlane: digest\n"
        f"evidence_kind: ops\nkind: {kind}\nalways_on_bytes: 0\n"
        f"base_rev: {base_rev}\ntargets: [{tlist}]\n"
        "expectation: e\ncheck_window_days: 7\nrevert: r\n"
        "---\n\n## Evidence\nE\n\n" + diff + "\n## Rationale\nR\n")
    return p


def commit_all(root, message):
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message)


@pytest.fixture()
def remote_root(tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "claude2"
    (work / "rules").mkdir(parents=True)
    (work / "rules" / "x.md").write_text("old line\nkeep\n")
    git(work, "init", "-q")
    commit_all(work, "init")
    subprocess.run(["git", "init", "-q", "--bare", str(origin)],
                   capture_output=True, text=True)
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    git(work, "fetch", "-q", "origin")
    git(work, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return work


def write_reflog(root, ref, entries):
    """Lay down a ref's reflog explicitly: [(old_sha, new_sha, epoch), ...],
    oldest first. Fixtures must not depend on WHICH operations git chose to log
    — that varies with git version and config, and a fixture whose reflog turned
    out to hold one entry instead of two reads as a product defect."""
    common = git(root, "rev-parse", "--path-format=absolute",
                 "--git-common-dir").stdout.strip()
    path = os.path.join(common, "logs", ref)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        for old, new, when in entries:
            fh.write("%s %s t <t@t> %d +0000\tfixture\n" % (old, new, int(when)))


def reflog_epoch(root, ref):
    """When the ref last MOVED locally, in epoch seconds."""
    line = git(root, "reflog", "show", "--date=unix", ref).stdout.splitlines()[0]
    return int(line.split("@{")[1].split("}")[0])


def fetch_after(mod, root, *proposals):
    """Model the sitting's own fetch: it runs at review time, after every
    proposal was drafted. The fixtures build the repo first, so without this
    the ref reads as fetched-before-drafting."""
    common = git(root, "rev-parse", "--path-format=absolute",
                 "--git-common-dir").stdout.strip()
    latest = max(os.path.getmtime(p) for p in proposals)
    os.utime(os.path.join(common, "FETCH_HEAD"), (latest + 60, latest + 60))


def run_currency(mod, capsys, *proposals):
    rc = mod.main(["currency"] + [a for p in proposals
                                  for a in ("--proposal", str(p))])
    return rc, capsys.readouterr().out


def verdict_of(out, pid):
    """The verdict token on the proposal's own row — never the totals line,
    which names every class unconditionally."""
    for line in out.splitlines():
        if line.startswith(pid + "  "):
            return line[len(pid):].strip().split()[0]
    raise AssertionError(f"no row for {pid} in:\n{out}")


def rows_of(out, pid):
    lines = out.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(pid + "  "))
    body = []
    for line in lines[start + 1:]:
        if not line.startswith("  "):
            break
        body.append(line)
    return "\n".join(body)


def test_currency_unchanged_target_is_fresh(wired, git_root, tmp_path, capsys):
    base = git(git_root, "rev-parse", "--short", "HEAD").stdout.strip()
    prop = make_currency_proposal(tmp_path, [str(git_root / "rules" / "x.md")],
                                  base_rev=base)
    rc, out = run_currency(wired, capsys, prop)
    assert rc == 0
    assert verdict_of(out, "cur-1") == "fresh"


def test_currency_changed_target_is_stale_and_prints_rederive(wired, git_root,
                                                              tmp_path, capsys):
    base = git(git_root, "rev-parse", "--short", "HEAD").stdout.strip()
    prop = make_currency_proposal(tmp_path, [str(git_root / "rules" / "x.md")],
                                  base_rev=base)
    (git_root / "rules" / "x.md").write_text("changed\nkeep\n")
    commit_all(git_root, "move it")
    rc, out = run_currency(wired, capsys, prop)
    assert rc == 0
    assert verdict_of(out, "cur-1") == "STALE"
    body = rows_of(out, "cur-1")
    assert "rules/x.md  1  (key=base " + base in body
    assert f"re-derive: git -C {git_root} log -p {base}..HEAD -- rules/x.md" in body


def test_currency_prefers_remote_branch_over_local_head(mod, postrun_of, remote_root,
                                                        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(postrun_of, "ALLOWED_TARGET_ROOTS", [remote_root])
    base = git(remote_root, "rev-parse", "--short", "HEAD").stdout.strip()
    git(remote_root, "checkout", "-q", "-b", "side")
    (remote_root / "rules" / "x.md").write_text("remote change\nkeep\n")
    commit_all(remote_root, "remote move")
    git(remote_root, "push", "-q", "origin", "side:main")
    git(remote_root, "fetch", "-q", "origin")
    git(remote_root, "checkout", "-q", base)
    prop = make_currency_proposal(tmp_path, [str(remote_root / "rules" / "x.md")],
                                  base_rev=base)
    fetch_after(mod, remote_root, prop)
    assert git(remote_root, "rev-list", "--count",
               f"{base}..HEAD", "--", "rules/x.md").stdout.strip() == "0"
    rc, out = run_currency(mod, capsys, prop)
    assert rc == 0
    assert verdict_of(out, "cur-1") == "STALE"
    assert "ref=origin/main" in rows_of(out, "cur-1")


def test_currency_no_remote_at_all_uses_head_and_still_verdicts(wired, git_root,
                                                                tmp_path, capsys):
    base = git(git_root, "rev-parse", "--short", "HEAD").stdout.strip()
    prop = make_currency_proposal(tmp_path, [str(git_root / "rules" / "x.md")],
                                  base_rev=base)
    rc, out = run_currency(wired, capsys, prop)
    assert rc == 0
    assert verdict_of(out, "cur-1") == "fresh"
    assert "ref=HEAD" in rows_of(out, "cur-1")


def test_currency_remote_without_branch_is_unknown(mod, postrun_of, remote_root,
                                                   tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(postrun_of, "ALLOWED_TARGET_ROOTS", [remote_root])
    base = git(remote_root, "rev-parse", "--short", "HEAD").stdout.strip()
    git(remote_root, "update-ref", "-d", "refs/remotes/origin/HEAD")
    git(remote_root, "update-ref", "-d", "refs/remotes/origin/main")
    prop = make_currency_proposal(tmp_path, [str(remote_root / "rules" / "x.md")],
                                  base_rev=base)
    rc, out = run_currency(mod, capsys, prop)
    assert rc == 0
    assert verdict_of(out, "cur-1") == "unknown"
    assert "never fetched" in rows_of(out, "cur-1")


def test_currency_base_rev_on_a_side_branch_counts_what_the_drafter_lacked(
        mod, postrun_of, remote_root, tmp_path, monkeypatch, capsys):
    """`base_rev` comes from the drafter's LOCAL HEAD, which routinely sits on a
    side branch, so it is often not an ancestor of the comparison ref. The span
    must still be the set difference `base_rev..ref` — commits reachable from the
    ref that the drafter did not have. Keying this case on base_rev's commit DATE
    instead misses every such commit that predates the side commit."""
    monkeypatch.setattr(postrun_of, "ALLOWED_TARGET_ROOTS", [remote_root])
    fork = git(remote_root, "rev-parse", "--short", "HEAD").stdout.strip()
    (remote_root / "rules" / "x.md").write_text("main moved once\nkeep\n")
    commit_all(remote_root, "main t1")
    (remote_root / "rules" / "x.md").write_text("main moved twice\nkeep\n")
    commit_all(remote_root, "main t2")
    git(remote_root, "push", "-q", "origin", "HEAD:main")
    git(remote_root, "fetch", "-q", "origin")
    git(remote_root, "checkout", "-q", "-b", "side", fork)
    (remote_root / "unrelated.md").write_text("side work\n")
    commit_all(remote_root, "side, committed after t1 and t2")
    side = git(remote_root, "rev-parse", "--short", "HEAD").stdout.strip()
    assert git(remote_root, "merge-base", "--is-ancestor", side,
               "refs/remotes/origin/main").returncode != 0
    prop = make_currency_proposal(tmp_path, [str(remote_root / "rules" / "x.md")],
                                  base_rev=side)
    fetch_after(mod, remote_root, prop)
    rc, out = run_currency(mod, capsys, prop)
    assert rc == 0
    assert verdict_of(out, "cur-1") == "STALE"
    assert "rules/x.md  2  (key=base " + side in rows_of(out, "cur-1")


def test_currency_non_commit_base_rev_falls_back_to_mtime(wired, git_root, tmp_path,
                                                          capsys):
    """A `base_rev` that resolves to a blob or tree is not a revision. It must not
    reach the commit-span path, where a failed date lookup would become an empty
    `--since` — which git reads as "now" and reports as zero commits."""
    blob = git(git_root, "rev-parse", "HEAD:rules/x.md").stdout.strip()[:7]
    assert git(git_root, "cat-file", "-t", blob).stdout.strip() == "blob"
    prop = make_currency_proposal(tmp_path, [str(git_root / "rules" / "x.md")],
                                  base_rev=blob)
    rc, out = run_currency(wired, capsys, prop)
    assert rc == 0
    assert "key=base " not in rows_of(out, "cur-1")


def test_currency_base_rev_absent_from_root_uses_mtime(wired, git_root, tmp_path,
                                                       capsys):
    prop = make_currency_proposal(tmp_path, [str(git_root / "rules" / "x.md")],
                                  base_rev="deadbee")
    rc, out = run_currency(wired, capsys, prop)
    assert rc == 0
    assert "key=ref-at " in rows_of(out, "cur-1")


def test_currency_missing_target_splits_by_kind(wired, git_root, tmp_path, capsys):
    absent = str(git_root / "rules" / "not-yet.md")
    creating = make_currency_proposal(tmp_path, [absent], kind="new-asset",
                                      pid="cur-new")
    editing = make_currency_proposal(tmp_path, [absent], kind="rule-edit",
                                     pid="cur-edit")
    rc, out = run_currency(wired, capsys, creating, editing)
    assert rc == 0
    assert verdict_of(out, "cur-new") == "n/a"
    assert "target not yet created" in rows_of(out, "cur-new")
    assert verdict_of(out, "cur-edit") == "unknown"
    assert "target absent from" in rows_of(out, "cur-edit")


def test_currency_target_in_local_index_but_absent_from_ref_is_not_fresh(
        mod, postrun_of, remote_root, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(postrun_of, "ALLOWED_TARGET_ROOTS", [remote_root])
    base = git(remote_root, "rev-parse", "--short", "HEAD").stdout.strip()
    (remote_root / "rules" / "local-only.md").write_text("local\n")
    commit_all(remote_root, "local only, never pushed")
    prop = make_currency_proposal(
        tmp_path, [str(remote_root / "rules" / "local-only.md")], base_rev=base)
    assert git(remote_root, "ls-files", "--error-unmatch",
               "rules/local-only.md").returncode == 0
    fetch_after(mod, remote_root, prop)
    rc, out = run_currency(mod, capsys, prop)
    assert rc == 0
    assert verdict_of(out, "cur-1") == "unknown"
    assert "target absent from origin/main" in rows_of(out, "cur-1")


def test_currency_ref_fetched_before_drafting_is_unknown(mod, postrun_of, remote_root,
                                                         tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(postrun_of, "ALLOWED_TARGET_ROOTS", [remote_root])
    base = git(remote_root, "rev-parse", "--short", "HEAD").stdout.strip()
    prop = make_currency_proposal(tmp_path, [str(remote_root / "rules" / "x.md")],
                                  base_rev=base)
    fetched = mod.last_fetch_epoch(str(remote_root))
    assert fetched is not None
    os.utime(prop, (fetched + 3600, fetched + 3600))
    rc, out = run_currency(mod, capsys, prop)
    assert rc == 0
    assert verdict_of(out, "cur-1") == "unknown"
    assert "ref-stale" in rows_of(out, "cur-1")


def test_currency_quiet_repo_is_fresh_not_ref_stale(mod, postrun_of, remote_root,
                                                    tmp_path, monkeypatch, capsys):
    """A ref whose newest COMMIT predates drafting is not stale — only a ref
    last FETCHED before drafting is. Keying the gate on the tip commit date
    flagged every quiet repository."""
    monkeypatch.setattr(postrun_of, "ALLOWED_TARGET_ROOTS", [remote_root])
    base = git(remote_root, "rev-parse", "--short", "HEAD").stdout.strip()
    prop = make_currency_proposal(tmp_path, [str(remote_root / "rules" / "x.md")],
                                  base_rev=base)
    tip = int(git(remote_root, "log", "-1", "--format=%ct",
                  "refs/remotes/origin/main").stdout.strip())
    os.utime(prop, (tip + 7200, tip + 7200))
    fetched = mod.last_fetch_epoch(str(remote_root))
    os.utime(os.path.join(git(remote_root, "rev-parse", "--path-format=absolute",
                              "--git-common-dir").stdout.strip(), "FETCH_HEAD"),
             (tip + 10800, tip + 10800))
    assert fetched is not None
    rc, out = run_currency(mod, capsys, prop)
    assert rc == 0
    assert verdict_of(out, "cur-1") == "fresh"


def test_currency_multi_root_mixed_keys_takes_worst_verdict(mod, postrun_of, git_root,
                                                            remote_root, tmp_path,
                                                            monkeypatch, capsys):
    monkeypatch.setattr(postrun_of, "ALLOWED_TARGET_ROOTS", [git_root, remote_root])
    (git_root / "rules" / "only-here.md").write_text("distinct history\n")
    commit_all(git_root, "diverge this root")
    base = git(git_root, "rev-parse", "--short", "HEAD").stdout.strip()
    assert git(remote_root, "cat-file", "-t", base).returncode != 0
    before = git(remote_root, "rev-parse", "HEAD").stdout.strip()
    (remote_root / "rules" / "x.md").write_text("moved on the other root\nkeep\n")
    commit_all(remote_root, "other root moves")
    after = git(remote_root, "rev-parse", "HEAD").stdout.strip()
    git(remote_root, "push", "-q", "origin", "HEAD:main")
    git(remote_root, "fetch", "-q", "origin")
    prop = make_currency_proposal(
        tmp_path,
        [str(git_root / "rules" / "x.md"), str(remote_root / "rules" / "x.md")],
        base_rev=base)
    moved_at = int(git(remote_root, "log", "-1", "--format=%ct",
                       "refs/remotes/origin/main").stdout.strip())
    drafted = moved_at - 86400
    write_reflog(remote_root, "refs/remotes/origin/main",
                 [("0" * 40, before, moved_at - 172800), (before, after, moved_at)])
    os.utime(prop, (drafted, drafted))
    fetch_after(mod, remote_root, prop)
    rc, out = run_currency(mod, capsys, prop)
    assert rc == 0
    body = rows_of(out, "cur-1")
    assert "key=base " + base in body
    assert "key=ref-at " in body
    assert verdict_of(out, "cur-1") == "STALE"


def test_currency_prose_diff_proposal_still_gets_a_verdict(wired, git_root, tmp_path,
                                                           capsys):
    base = git(git_root, "rev-parse", "--short", "HEAD").stdout.strip()
    prop = make_currency_proposal(tmp_path, [str(git_root / "rules" / "x.md")],
                                  base_rev=base, kind="build-brief",
                                  with_diff=False)
    assert wired.main(["check", "--proposal", str(prop)]) == 2
    capsys.readouterr()
    rc, out = run_currency(wired, capsys, prop)
    assert rc == 0
    assert verdict_of(out, "cur-1") == "fresh"


def test_currency_unreadable_proposal_does_not_blind_the_batch(wired, git_root,
                                                              tmp_path, capsys):
    base = git(git_root, "rev-parse", "--short", "HEAD").stdout.strip()
    good = make_currency_proposal(tmp_path, [str(git_root / "rules" / "x.md")],
                                  base_rev=base, pid="cur-good")
    bad = tmp_path / "cur-bad.md"
    bad.write_text("no frontmatter here\n")
    rc, out = run_currency(wired, capsys, good, bad)
    assert rc == 0
    assert verdict_of(out, "cur-good") == "fresh"
    assert verdict_of(out, "cur-bad") == "unknown"


def test_currency_backdated_commit_arriving_after_drafting_is_stale(
        mod, postrun_of, remote_root, tmp_path, monkeypatch, capsys):
    """A date filter asks "was this commit AUTHORED after drafting"; the question
    is "did it ARRIVE on the ref after drafting". A long-lived branch merged in
    later carries an older committer date, so `--since <drafting>` excludes it and
    the target reads fresh while its content on the ref has changed. The span is
    keyed on where the ref POINTED at drafting instead.

    Drafting is pinned a day back, between the reflog horizon and the fetch that
    brought the merge, so no wall-clock gap decides the outcome. An earlier
    version also asserted the premise directly (`--since <drafting>` counts 0):
    that assertion depended on git's date-traversal semantics rather than on this
    code, was green 30x locally, and went red on CI."""
    monkeypatch.setattr(postrun_of, "ALLOWED_TARGET_ROOTS", [remote_root])
    env = {"GIT_COMMITTER_DATE": "2026-08-05T10:00:00Z",
           "GIT_AUTHOR_DATE": "2026-08-05T10:00:00Z"}
    git(remote_root, "checkout", "-q", "-b", "longlived")
    (remote_root / "rules" / "x.md").write_text("moved on a backdated branch\nkeep\n")
    git(remote_root, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    git(remote_root, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-qm", "backdated", env={**os.environ, **env})
    git(remote_root, "checkout", "-q", "main")
    before = git(remote_root, "rev-parse", "HEAD").stdout.strip()
    prop = make_currency_proposal(tmp_path, [str(remote_root / "rules" / "x.md")],
                                  base_rev="deadbee")
    git(remote_root, "-c", "user.email=t@t", "-c", "user.name=t",
        "merge", "-q", "--no-ff", "longlived", "-m", "merge it")
    after = git(remote_root, "rev-parse", "HEAD").stdout.strip()
    assert after != before, "merge did not land — a commit-creating git call " \
                            "needs -c user.email/-c user.name; a runner has no " \
                            "global identity and git only warns"
    git(remote_root, "push", "-q", "origin", "HEAD:main")
    git(remote_root, "fetch", "-q", "origin")
    now = int(git(remote_root, "log", "-1", "--format=%ct", "HEAD").stdout.strip())
    drafted = now - 86400
    write_reflog(remote_root, "refs/remotes/origin/main",
                 [("0" * 40, before, now - 172800), (before, after, now)])
    os.utime(prop, (drafted, drafted))
    fetch_after(mod, remote_root, prop)
    assert mod._ref_at(str(remote_root), "refs/remotes/origin/main",
                       mod._mtime_iso(drafted)) == before
    rc, out = run_currency(mod, capsys, prop)
    assert rc == 0
    assert verdict_of(out, "cur-1") == "STALE"


def test_currency_truncated_reflog_degrades_to_the_declared_weak_key(
        mod, postrun_of, remote_root, tmp_path, monkeypatch, capsys):
    """`<ref>@{<stamp>}` does NOT fail when the stamp predates the reflog's
    horizon: git exits 0, returns the OLDEST value it still remembers, and warns
    on stderr. Suppressing that warning made the probe treat a value NEWER than
    the drafter's as the drafter's, losing every commit in between and reporting
    fresh. The reconstruction must refuse instead, falling through to the key
    whose own label declares it is weak."""
    monkeypatch.setattr(postrun_of, "ALLOWED_TARGET_ROOTS", [remote_root])
    (remote_root / "rules" / "x.md").write_text("moved after drafting\nkeep\n")
    commit_all(remote_root, "the change the drafter never saw")
    git(remote_root, "push", "-q", "origin", "HEAD:main")
    git(remote_root, "fetch", "-q", "origin")
    prop = make_currency_proposal(tmp_path, [str(remote_root / "rules" / "x.md")],
                                  base_rev="deadbee")
    horizon = reflog_epoch(remote_root, "refs/remotes/origin/main")
    os.utime(prop, (horizon - 86400, horizon - 86400))
    fetch_after(mod, remote_root, prop)
    assert mod._ref_at(str(remote_root), "refs/remotes/origin/main",
                       mod._mtime_iso(horizon - 86400)) == ""
    rc, out = run_currency(mod, capsys, prop)
    assert rc == 0
    body = rows_of(out, "cur-1")
    assert "key=since " in body
    assert "blind to a backdated commit" in body
    assert verdict_of(out, "cur-1") == "STALE"
