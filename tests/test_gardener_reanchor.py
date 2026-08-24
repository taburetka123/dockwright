"""Tests for the content-anchored re-apply primitives in gardener_apply.py.

Fixture-shape provenance: each failure class mirrors a real specimen shape
from the 2026-07-22 pending-corpus investigation (35 proposals: 3 clean /
6 malformed / 26 strict-fail against the reconstructed T0 world). The live
corpus is private and not committed; these fixtures are synthetic.
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "deploy" / "scripts"


@pytest.fixture()
def mod():
    spec = importlib.util.spec_from_file_location(
        "gardener_apply_under_test", SCRIPTS / "gardener_apply.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ---- lenient_parse ----

def test_lenient_parse_bare_at_at(mod):
    """Frontier lane emits bare `@@` with no line numbers — must parse."""
    fds = mod.lenient_parse(
        "--- a/f.md\n+++ b/f.md\n@@\n ctx\n+added\n")
    assert len(fds) == 1
    assert fds[0].old_raw == "a/f.md"
    assert fds[0].hunks == [[" ctx", "+added"]]


def test_lenient_parse_numbered_headers_and_multi_hunk(mod):
    fds = mod.lenient_parse(
        "--- a/f.md\n+++ b/f.md\n"
        "@@ -1,2 +1,2 @@\n ctx1\n-old\n+new\n"
        "@@ -9,1 +9,2 @@\n ctx2\n+add\n")
    assert len(fds) == 1
    assert len(fds[0].hunks) == 2
    assert fds[0].hunks[1] == [" ctx2", "+add"]


def test_lenient_parse_wrong_counts_do_not_swallow_next_hunk(mod):
    """The strict parser's count-overrun disease must not exist here: a
    grossly over-stated count must not absorb the next @@ header."""
    fds = mod.lenient_parse(
        "--- a/f.md\n+++ b/f.md\n"
        "@@ -1,99 +1,99 @@\n ctx1\n-old\n+new\n"
        "@@ -9,1 +9,2 @@\n ctx2\n+add\n")
    assert len(fds[0].hunks) == 2


def test_lenient_parse_multi_file(mod):
    fds = mod.lenient_parse(
        "--- a/f.md\n+++ b/f.md\n@@\n ctx\n+x\n"
        "--- a/g.md\n+++ b/g.md\n@@\n ctx2\n+y\n")
    assert [fd.new_raw for fd in fds] == ["b/f.md", "b/g.md"]


def test_lenient_parse_blank_line_between_hunks_absorbed(mod):
    """A blank separator between hunks lands in the previous hunk's body
    (as blank context) — hunk_blocks' trailing trim removes it."""
    fds = mod.lenient_parse(
        "--- a/f.md\n+++ b/f.md\n"
        "@@\n ctx1\n-old\n+new\n"
        "\n"
        "@@\n ctx2\n+add\n")
    assert len(fds[0].hunks) == 2
    assert fds[0].hunks[0] == [" ctx1", "-old", "+new", ""]


def test_lenient_parse_junk_in_body_is_malformed(mod):
    with pytest.raises(mod.ApplyError) as exc:
        mod.lenient_parse(
            "--- a/f.md\n+++ b/f.md\n@@\n ctx\n+x\nRationale prose here\n")
    assert exc.value.code == 2


def test_lenient_parse_hunk_before_file_header_is_malformed(mod):
    """RED-proof obligation (spec Testing §2): the lenient parity guard.
    Manually verify RED by deleting the `cur_fd is None` raise in a scratch
    copy — this test must then fail (the orphan hunk would be dropped
    silently); restore and confirm GREEN. Document the observed RED output
    here when executing.

    RED-proof: executed 2026-07-22 against a scratch copy with the raise
    deleted; observed (pytest -q):
        >               cur_fd.hunks.append(cur_hunk)
        E               AttributeError: 'NoneType' object has no attribute 'hunks'
        FAILED test_red_proof.py::test_lenient_parse_hunk_before_file_header_is_malformed
        1 failed in 0.02s
    (the orphan hunk escapes classification — an unclassified crash, not the
    ApplyError code=2 this test demands). Restored copy: GREEN."""
    with pytest.raises(mod.ApplyError) as exc:
        mod.lenient_parse("@@ -1,1 +1,1 @@\n-x\n+y\n")
    assert exc.value.code == 2


def test_lenient_parse_noise_between_header_and_hunk_ok(mod):
    fds = mod.lenient_parse(
        "diff --git a/f.md b/f.md\nindex abc..def 100644\n"
        "--- a/f.md\n+++ b/f.md\n@@\n ctx\n+x\n")
    assert len(fds) == 1


# ---- hunk_blocks / hunk_change_lines ----

def test_hunk_blocks_basic(mod):
    old, new = mod.hunk_blocks([" ctx", "-old", "+new", " tail"])
    assert old == ["ctx", "old", "tail"]
    assert new == ["ctx", "new", "tail"]


def test_hunk_blocks_blank_is_context(mod):
    old, new = mod.hunk_blocks([" a", "", "+x", " b"])
    assert old == ["a", "", "b"]
    assert new == ["a", "", "x", "b"]


def test_hunk_blocks_trailing_blank_trim_equal_counts(mod):
    old, new = mod.hunk_blocks([" a", "-o", "+n", ""])
    assert old == ["a", "o"]
    assert new == ["a", "n"]


def test_hunk_blocks_trailing_blank_kept_when_counts_differ(mod):
    """`+` adding a trailing blank is a REAL change — never trimmed."""
    old, new = mod.hunk_blocks([" a", "+"])
    assert old == ["a"]
    assert new == ["a", ""]


def test_hunk_blocks_ignores_no_newline_marker(mod):
    old, new = mod.hunk_blocks(["-o", "\\ No newline at end of file", "+n"])
    assert old == ["o"]
    assert new == ["n"]


def test_hunk_change_lines(mod):
    rem, add = mod.hunk_change_lines([" c", "-o1", "+n1", " c2", "-o2"])
    assert rem == ["o1", "o2"]
    assert add == ["n1"]


# ---- anchor_hunks ----

FILE = ["alpha", "beta", "gamma", "delta", "beta", "epsilon"]


def test_anchor_unique_match(mod):
    anchored = mod.anchor_hunks(FILE, [[" gamma", "-delta", "+DELTA"]])
    assert anchored == [(2, ["gamma", "delta"], ["gamma", "DELTA"])]


def test_anchor_ambiguous_refuses(mod):
    with pytest.raises(mod.ApplyError) as exc:
        mod.anchor_hunks(FILE, [[" beta", "+X"]])
    assert exc.value.klass == "ambiguous"
    assert exc.value.code == 1


def test_anchor_drifted_reports_longest_prefix(mod):
    with pytest.raises(mod.ApplyError) as exc:
        mod.anchor_hunks(FILE, [[" gamma", " NOPE", "+X"]])
    assert exc.value.klass == "drifted"
    assert "1/2" in str(exc.value)


def test_anchor_region_scoped_ordering(mod):
    """Second hunk anchors AFTER the first hunk's end — the second 'beta'."""
    anchored = mod.anchor_hunks(
        FILE, [[" alpha", "-beta", "+B1"], [" beta", "+B2"]])
    assert anchored[0][0] == 0
    assert anchored[1][0] == 4


def test_anchor_out_of_order_is_drifted(mod):
    with pytest.raises(mod.ApplyError) as exc:
        mod.anchor_hunks(
            FILE, [[" delta", "+X"], [" alpha", "+Y"]])
    assert exc.value.klass == "drifted"
    assert "out of order" in str(exc.value)


def test_anchor_pure_insertion_no_context_is_malformed(mod):
    with pytest.raises(mod.ApplyError) as exc:
        mod.anchor_hunks(FILE, [["+only-added"]])
    assert exc.value.code == 2


# ---- splice_lines / newline handling ----

def test_splice_multi_hunk(mod):
    anchored = mod.anchor_hunks(
        FILE, [[" alpha", "-beta", "+B1"], [" beta", "+B2", " epsilon"]])
    out = mod.splice_lines(FILE, anchored)
    assert out == ["alpha", "B1", "gamma", "delta", "beta", "B2", "epsilon"]


def test_splice_does_not_mutate_input(mod):
    src = list(FILE)
    anchored = mod.anchor_hunks(src, [[" alpha", "+X"]])
    mod.splice_lines(src, anchored)
    assert src == FILE


def test_read_join_preserves_missing_trailing_newline(mod, tmp_path):
    p = tmp_path / "f.md"
    p.write_bytes(b"a\nb")
    lines, had_nl = mod.read_file_lines(str(p))
    assert lines == ["a", "b"] and had_nl is False
    assert mod.join_file_lines(lines, had_nl) == "a\nb"


def test_read_join_preserves_trailing_newline(mod, tmp_path):
    p = tmp_path / "f.md"
    p.write_bytes(b"a\nb\n")
    lines, had_nl = mod.read_file_lines(str(p))
    assert lines == ["a", "b"] and had_nl is True
    assert mod.join_file_lines(lines, had_nl) == "a\nb\n"


# ---- classification / plan resolution / check CLI ----

def git(root, *args, inp=None):
    return subprocess.run(["git", "-C", str(root)] + list(args),
                          capture_output=True, text=True, input=inp)


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "root"
    (root / "rules").mkdir(parents=True)
    (root / "rules" / "x.md").write_text(
        "# Title\n\nalpha\nbeta\ngamma\ndelta\n")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-qm", "base")
    return root


@pytest.fixture()
def postrun_of(mod):
    import sys as _sys
    return _sys.modules["gardener_postrun"]


@pytest.fixture()
def scoped(mod, postrun_of, repo, monkeypatch):
    # monkeypatch, never direct assignment: gardener_postrun is the
    # session-shared sys.modules instance — a leaked ALLOWED_TARGET_ROOTS or
    # LEDGER_PATH would let a later test touch the real ~/.claude.
    monkeypatch.setattr(postrun_of, "ALLOWED_TARGET_ROOTS", [repo])
    return repo


def make_proposal(tmp_path, targets, diff_body, meta_extra=""):
    p = tmp_path / "prop.md"
    tlist = ", ".join(str(t) for t in targets)
    p.write_text(
        "---\n"
        "id: r1-1\nrun_id: r1\ncluster: c\nlane: digest\n"
        "evidence_kind: ops\nkind: rule-edit\nalways_on_bytes: 0\n"
        f"base_rev: abc1234\ntargets: [{tlist}]\n"
        "expectation: e\ncheck_window_days: 7\nrevert: r\n"
        f"{meta_extra}"
        "---\n\n## Evidence\nE\n\n## Diff\n```diff\n" + diff_body
        + "```\n\n## Rationale\nR\n")
    return p


def target(repo):
    return str(repo / "rules" / "x.md")


def test_classify_clean(mod, scoped, tmp_path):
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n"
        "@@ -3,3 +3,3 @@\n alpha\n-beta\n+BETA\n gamma\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass == "clean"


def test_classify_reanchorable_wrong_counts(mod, scoped, tmp_path):
    """Header claims 9 lines; body has 3. Strict git apply refuses;
    content anchoring rescues."""
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n"
        "@@ -3,9 +3,9 @@\n alpha\n-beta\n+BETA\n gamma\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass == "reanchorable"


def test_classify_reanchorable_no_trailing_context_midfile(mod, scoped, tmp_path):
    """The MID-FAILS shape from the spec's minimal repro: correct counts,
    leading context only, insertion mid-file. git apply anchors such a hunk
    at EOF and refuses; content anchoring applies it."""
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n"
        "@@ -3,1 +3,2 @@\n alpha\n+inserted\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass == "reanchorable"


def test_classify_reanchorable_bare_at_at(mod, scoped, tmp_path):
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n gamma\n+tail\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass == "reanchorable"


def test_classify_drifted(mod, scoped, tmp_path):
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n NOT-IN-FILE\n+x\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass == "drifted"
    assert "not found" in cls.detail


def test_classify_ambiguous(mod, scoped, tmp_path):
    (scoped / "rules" / "x.md").write_text("dup\nmid\ndup\n")
    git(scoped, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-aqm", "dup")
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n dup\n+x\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass == "ambiguous"


def test_classify_missing_file(mod, scoped, tmp_path):
    p = make_proposal(tmp_path, [str(scoped / "rules" / "gone.md")],
        "--- a/rules/gone.md\n+++ b/rules/gone.md\n@@\n x\n+y\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass == "missing-file"


def test_classify_malformed_junk_body(mod, scoped, tmp_path):
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n alpha\n+x\nprose junk\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass == "malformed"


def test_classify_out_of_scope_undeclared_path(mod, scoped, tmp_path):
    """FR-8 / #208 shape: declares rules/x.md, patches rules/other.md."""
    (scoped / "rules" / "other.md").write_text("alpha\n")
    git(scoped, "add", "-A")
    git(scoped, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-qm", "other")
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/other.md\n+++ b/rules/other.md\n@@\n alpha\n+x\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass == "out-of-scope"


def test_classify_out_of_scope_absolute_undeclared_path(mod, scoped, tmp_path):
    """I4 / #208 shape: declares rules/x.md but the diff patches ANOTHER
    existing allowed-roots file by ABSOLUTE path. The a/<rel> form was already
    scope-checked; the absolute form bypassed it (accepted if merely inside
    allowed roots) — now it must ALSO name a declared target or be refused
    out-of-scope.

    RED-proof (executed 2026-07-22): revert the absolute-branch declared-target
    check in _resolve_one (return the realpath unconditionally) in a scratch
    copy -> classify returns 'reanchorable' and apply would patch rules/other.md
    though the review sitting only shows rules/x.md as the target. Restored:
    GREEN (out-of-scope)."""
    (scoped / "rules" / "other.md").write_text("alpha\nbeta\n")
    git(scoped, "add", "-A")
    git(scoped, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-qm", "other")
    other_abs = str(scoped / "rules" / "other.md")
    p = make_proposal(tmp_path, [target(scoped)],
        f"--- {other_abs}\n+++ {other_abs}\n@@ -1,1 +1,1 @@\n alpha\n+ALPHA\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass == "out-of-scope"


def test_classify_new_file_creation_reanchorable(mod, scoped, tmp_path):
    p = make_proposal(tmp_path, [str(scoped / "rules" / "new.md")],
        "--- /dev/null\n+++ b/rules/new.md\n@@\n+line1\n+line2\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass == "reanchorable"


def test_classify_new_file_already_exists_is_drifted(mod, scoped, tmp_path):
    p = make_proposal(tmp_path, [target(scoped)],
        "--- /dev/null\n+++ b/rules/x.md\n@@\n+line1\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass == "drifted"


def test_classify_count_overrun_swallowing_next_file_header(mod, scoped, tmp_path):
    """#208 mangler shape: hunk-1's inflated count would swallow the second
    file's header in the strict parser (parity assertion trips there);
    lenient parse owns both files and both must land in the plan.

    RED-proof obligation (spec Testing §2 mangler catch): in a scratch
    copy, force resolve_plan to keep the STRICT parse result on parity
    failure instead of falling back to lenient — this test must then FAIL
    (file g.md's change would be silently dropped). Restore; paste the
    observed failure output here.

    RED-PROOF EXECUTED 2026-07-22 (scratch copy of gardener_apply.py).
    Empirical finding first: the strict parser does NOT raise on this
    specimen — the inflated `@@ -1,50` count absorbs g.md's `--- `/`+++ `
    header AND its `@@` line as hunk-body lines, so total-vs-parsed `@@`
    parity stays 2==2 (verified: split_file_diffs yields 1 FileDiff, no
    ApplyError). The `except ApplyError: strict_ok = {}` fallback is
    therefore NOT the trigger here; the real fallback is the failing
    `git apply --check` on the corrupt single-file patch. So the faithful
    "force the truncated strict view to win" edit is to keep the
    check-FAILING strict plan instead of falling back to lenient — in
    resolve_plan change
        if strict_ok and all(ok for ok, _p, _f in strict_ok.values()):
    to
        if strict_ok:            # SCRATCH: keep strict even on check-fail
    Observed (pytest -q) against that scratch copy:
        >       assert cls.klass == "reanchorable"
        E       AssertionError: assert 'clean' == 'reanchorable'
        E         - reanchorable
        E         + clean
        FAILED ...::test_classify_count_overrun_swallowing_next_file_header
    (the strict-truncated plan drops g.md entirely — one patch-mode root
    over rels=['rules/x.md'] only — and classifies 'clean', proving g.md's
    change was silently swallowed). Restored copy: GREEN."""
    (scoped / "rules" / "g.md").write_text("alpha\nomega\n")
    git(scoped, "add", "-A")
    git(scoped, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-qm", "g")
    p = make_proposal(
        tmp_path,
        [target(scoped), str(scoped / "rules" / "g.md")],
        "--- a/rules/x.md\n+++ b/rules/x.md\n"
        "@@ -1,50 +1,50 @@\n alpha\n-beta\n+BETA\n"
        "--- a/rules/g.md\n+++ b/rules/g.md\n"
        "@@ -1,2 +1,2 @@\n alpha\n-omega\n+OMEGA\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass == "reanchorable"
    _meta, plan = mod.resolve_plan(str(p))
    rp = plan[os.path.realpath(str(scoped))]  # plan keys are realpathed
    assert rp.mode == "splice"
    assert sorted(fp.rel for fp in rp.files) == ["rules/g.md", "rules/x.md"]


def test_classify_env_lenient_non_git_root(mod, postrun_of, tmp_path,
                                           monkeypatch):
    root = tmp_path / "notgit"
    (root / "rules").mkdir(parents=True)
    (root / "rules" / "x.md").write_text("alpha\n")
    monkeypatch.setattr(postrun_of, "ALLOWED_TARGET_ROOTS", [root])
    p = make_proposal(tmp_path, [str(root / "rules" / "x.md")],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n alpha\n+x\n")
    cls = mod.classify_proposal(str(p), env_lenient=True)
    assert cls.klass == "reanchorable"  # content classification still works
    root2 = tmp_path / "gone-root"
    monkeypatch.setattr(postrun_of, "ALLOWED_TARGET_ROOTS", [root2])
    (tmp_path / "d2").mkdir(exist_ok=True)
    p2 = make_proposal(tmp_path / "d2", [str(root2 / "rules" / "x.md")],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n alpha\n+x\n")
    cls2 = mod.classify_proposal(str(p2), env_lenient=True)
    assert cls2.klass in ("skipped-env", "missing-file")


def test_phantom_file_header_from_dash_content_fails_loud(mod, scoped, tmp_path):
    """Spec's named parser hazard: a deletion whose content starts with
    '-- ' renders as '--- x'; followed by a '+++'-lookalike addition it
    opens a phantom file section in the lenient parser. The phantom path
    must fail LOUDLY (out-of-scope via _resolve_one) — never apply."""
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n"
        "@@ -1,99 +1,99 @@\n alpha\n--- not-a-header\n+++ also-content\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass in ("out-of-scope", "malformed", "drifted")
    assert cls.klass != "reanchorable"


# ---- decoy-resistant anchoring (I2) ----
#
# Attacks A1/A2: when the intended anchor is absent/behind-cursor and exactly
# one VERBATIM copy of the old-block survives elsewhere, the pre-fix engine
# spliced there and reported success (act-verify + canon gate both pass — they
# verify the write matches the PLAN, not that the plan matches INTENT). Two
# guards close the corner: a candidate strictly inside a ``` fence is a
# self-quote decoy, and a numbered hunk whose only match is >100 lines from its
# declared start is anchoring on a decoy.


def test_candidate_reject_fence_interior_and_old_block_exception(mod):
    """Unit: a fence-interior candidate is discarded (a); an old-block that
    itself carries a ``` delimiter edits fence structure and is kept (exception
    i)."""
    fenced = [(0, 4)]  # candidate at pos 2 is strictly inside
    assert mod._candidate_reject_reason(2, ["x", "y"], None, fenced) == \
        "is inside a fenced code block"
    assert mod._candidate_reject_reason(2, ["```", "x"], None, fenced) is None


def test_anchor_discards_fence_interior_decoy(mod):
    """A bare-@@ hunk whose only match sits strictly inside a ``` fence is
    refused drifted — no numbers needed for the fence guard.

    RED (pre-fix): anchor_hunks had no fence awareness — it anchored on the
    interior match and the splice landed inside the code fence."""
    file_lines = ["intro", "```", "TARGET", "```", "tail"]
    with pytest.raises(mod.ApplyError) as exc:
        mod.anchor_hunks(file_lines, [[" TARGET", "+X"]])
    assert exc.value.klass == "drifted"
    assert "inside a fenced code block" in str(exc.value)


def test_anchor_keeps_fence_interior_when_declared_start_inside(mod):
    """Exception (ii): the numbered header's declared old-start points inside
    the same fence — declared intent to edit fence content, so keep it."""
    file_lines = ["a", "b", "```", "TARGET", "```", "tail"]  # fence idx 2..4
    anchored = mod.anchor_hunks(file_lines, [[" TARGET", "+X"]], starts=[4])
    assert anchored[0][0] == 3


def test_anchor_numeric_band_discards_far_decoy(mod):
    """A numbered hunk whose only match is >100 lines from the declared start
    is anchoring on a decoy, not tolerating a sloppy count.

    RED (pre-fix): the header numbers were never threaded to anchoring, so the
    far unique match anchored regardless of the declared start."""
    file_lines = ["x"] * 200 + ["TARGET"]  # match index 200 => line 201
    with pytest.raises(mod.ApplyError) as exc:
        mod.anchor_hunks(file_lines, [[" TARGET", "+Y"]], starts=[5])
    assert exc.value.klass == "drifted"
    assert "declared start 5" in str(exc.value)
    assert "band 100" in str(exc.value)


def test_anchor_numeric_band_tolerates_near(mod):
    """Off by <100 lines is sloppy arithmetic, not a decoy — still anchors."""
    file_lines = ["x"] * 20 + ["TARGET"]  # match line 21, declared 5 => |21-5|=16
    anchored = mod.anchor_hunks(file_lines, [[" TARGET", "+Y"]], starts=[5])
    assert anchored[0][0] == 20


def test_anchor_bare_at_at_no_band(mod):
    """Bare @@ carries no numbers, so the band is inapplicable — a far unique
    match still anchors (uniqueness is the only gate without numbers)."""
    file_lines = ["x"] * 200 + ["TARGET"]
    anchored = mod.anchor_hunks(file_lines, [[" TARGET", "+Y"]], starts=[None])
    assert anchored[0][0] == 200


def _commit(scoped, msg):
    git(scoped, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-aqm", msg)


def test_classify_A1_out_of_order_decoy_refused(mod, scoped, tmp_path):
    """A1 (reviewer): hunk 2's intended anchor is behind the region cursor and
    a VERBATIM decoy exists >100 lines later. Pre-fix the engine SPLICED AT THE
    DECOY, rc=0; the numeric proximity band now refuses it drifted.

    RED (pre-fix, no starts threaded): classify_proposal returned
    'reanchorable' and apply would splice EDIT-EARLY at line 131 (the decoy)
    instead of line 3."""
    lines = ["# Title", "", "ANCHOR-EARLY"]
    lines += [f"filler-{i}" for i in range(126)]          # lines 4..129
    lines += ["ANCHOR-LATE", "ANCHOR-EARLY", "tail"]      # lines 130,131,132
    (scoped / "rules" / "x.md").write_text("\n".join(lines) + "\n")
    _commit(scoped, "a1")
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n"
        "@@ -130,9 +130,9 @@\n ANCHOR-LATE\n+EDIT-LATE\n"
        "@@ -3,9 +3,9 @@\n ANCHOR-EARLY\n+EDIT-EARLY\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass == "drifted"
    assert "declared start 3" in cls.detail and "band 100" in cls.detail


def test_classify_A2_fence_decoy_refused(mod, scoped, tmp_path):
    """A2 (reviewer): the intended text is gone and its only verbatim copy is
    INSIDE a ``` fence; the numbered header points OUTSIDE the fence. Pre-fix
    the engine SPLICED INTO THE CODE FENCE, rc=0; the fence guard now refuses.

    RED (pre-fix, no fence awareness): classify returned 'reanchorable' and the
    splice landed on the fenced copy."""
    lines = ["# Title", "", "para", "", "normal", "more", "", "another",
             "yet", "final",
             "```", "DECOY-INSIDE-FENCE", "```", "tail"]   # fence lines 11..13
    (scoped / "rules" / "x.md").write_text("\n".join(lines) + "\n")
    _commit(scoped, "a2")
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n"
        "@@ -5,9 +5,9 @@\n DECOY-INSIDE-FENCE\n+EDIT\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass == "drifted"
    assert "inside a fenced code block" in cls.detail


def test_classify_legit_inside_fence_applies(mod, scoped, tmp_path):
    """Exception (ii): the SAME fenced file, but the numbered header declares a
    start INSIDE the fence — a legitimate edit of fenced content, which must
    still re-anchor (not be refused as a decoy)."""
    lines = ["# Title", "", "para", "", "normal", "more", "", "another",
             "yet", "final",
             "```", "DECOY-INSIDE-FENCE", "```", "tail"]   # fenced line 12
    (scoped / "rules" / "x.md").write_text("\n".join(lines) + "\n")
    _commit(scoped, "legit")
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n"
        "@@ -12,9 +12,9 @@\n DECOY-INSIDE-FENCE\n+EDIT\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass == "reanchorable"


_M7_NESTED = [
    "# Title", "", "TRIGGER: fixture.", "", "intro",
    "````",           # line 6: outer fence OPEN (4 backticks)
    "inner example",  # line 7
    "```",            # line 8: 3-backtick line — CONTENT inside the ````-fence
    "DECOY-BLOCK",    # line 9: the old-block, only copy, inside the outer fence
    "````",           # line 10: outer fence CLOSE (4 backticks)
    "tail",           # line 11
]


def test_fenced_ranges_length_aware(mod):
    """M7 unit: a ``` (3-backtick) line inside a ````-opened fence is CONTENT,
    not a close — the fence closes only on a run >= the opener's length.

    RED (length-blind toggle): _fenced_ranges(_M7_NESTED) == [(5, 7)] (closed
    early at the inner ```), so the decoy at line 9 read as OUTSIDE the fence.
    POST-FIX: [(5, 9)] (proper outer fence)."""
    assert mod._fenced_ranges(_M7_NESTED) == [(5, 9)]
    # a plain 3-backtick fence is still detected
    assert mod._fenced_ranges(["a", "```", "x", "```", "b"]) == [(1, 3)]
    assert mod._backtick_run_len("````") == 4
    assert mod._backtick_run_len("```diff") == 3
    assert mod._backtick_run_len("``") == 0        # <3 backticks is not a fence
    assert mod._backtick_run_len("plain text") == 0


def test_classify_M7_nested_fence_decoy_refused(mod, scoped, tmp_path):
    """M7 (verifier's shape): the intended anchor is gone and the only verbatim
    copy of the old-block sits inside a ````-opened fence that also contains a
    ``` content line; the numbered header points OUTSIDE the fence, within the
    proximity band. Pre-fix the length-BLIND _fenced_ranges closed the outer
    fence early at the inner ```, so the decoy read as outside-the-fence and
    the engine SPLICED INTO THE OUTER FENCE, rc=0 — the exact class I2 exists
    to prevent, defeated by fence length.

    RED-proof (executed 2026-07-23): with _fenced_ranges reverted to the
    length-blind toggle in a scratch copy, _fenced_ranges(_M7_NESTED) == [(5,7)]
    and classify returns 'reanchorable' (splices into the outer fence). Fixed
    (length-aware): [(5,9)], classify == 'drifted' (candidate discarded as
    in-fence)."""
    (scoped / "rules" / "x.md").write_text("\n".join(_M7_NESTED) + "\n")
    _commit(scoped, "m7-nested")
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n"
        "@@ -3,9 +3,9 @@\n DECOY-BLOCK\n+INJECTED\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass == "drifted"
    assert "inside a fenced code block" in cls.detail


def test_apply_M7_nested_fence_legit_inside_applies(mod, scoped, tmp_path,
                                                    ledgered):
    """M7 leg 2 (no over-correction): the SAME nested-fence file, but the
    numbered header declares a start INSIDE the (now properly-detected) outer
    fence — a legitimate edit of fenced content, which must still apply rc=0
    (exception ii), not be over-refused by the length fix."""
    (scoped / "rules" / "x.md").write_text("\n".join(_M7_NESTED) + "\n")
    _commit(scoped, "m7-legit")
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n"
        "@@ -9,9 +9,9 @@\n DECOY-BLOCK\n+ADDED-IN-FENCE\n")
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 0
    assert (scoped / "rules" / "x.md").read_text() == \
        "\n".join(_M7_NESTED[:9] + ["ADDED-IN-FENCE"] + _M7_NESTED[9:]) + "\n"


def test_classify_sloppy_but_near_numbers_still_reanchorable(mod, scoped,
                                                             tmp_path):
    """The corpus's bread-and-butter: hand-written line numbers off by a few
    (here declared 10 vs real 6) must keep working — the band tolerates
    sloppiness, refusing only section-scale mismatches."""
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@ -10,1 +10,1 @@\n gamma\n+X\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass == "reanchorable"


def test_check_cli_reanchorable_exit0_and_says_so(mod, scoped, tmp_path, capsys):
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n"
        "@@ -3,9 +3,9 @@\n alpha\n-beta\n+BETA\n gamma\n")
    rc = mod.main(["check", "--proposal", str(p)])
    assert rc == 0
    assert "re-anchor" in capsys.readouterr().out


def test_check_cli_drifted_exit1_class_labeled(mod, scoped, tmp_path, capsys):
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n NOT-IN-FILE\n+x\n")
    rc = mod.main(["check", "--proposal", str(p)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "drifted" in err
    assert "target drifted since drafting" not in err  # the old lying blanket


# ---- apply / revert execution (Task 3) ----

@pytest.fixture()
def ledgered(postrun_of, tmp_path, monkeypatch):
    monkeypatch.setattr(postrun_of, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    return postrun_of.LEDGER_PATH


def test_apply_reanchorable_writes_and_ledgers(mod, scoped, tmp_path, ledgered):
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n"
        "@@ -3,9 +3,9 @@\n alpha\n-beta\n+BETA\n gamma\n")
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 0
    assert (scoped / "rules" / "x.md").read_text() == \
        "# Title\n\nalpha\nBETA\ngamma\ndelta\n"
    events = [__import__("json").loads(l)
              for l in ledgered.read_text().splitlines()]
    assert events[-1]["event"] == "proposal_applied"
    assert "path" not in events[-1]


def test_apply_no_trailing_context_insertion_lands_midfile(mod, scoped,
                                                          tmp_path, ledgered):
    """The signature generator shape git itself can never apply mid-file."""
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n"
        "@@ -3,1 +3,2 @@\n alpha\n+inserted\n")
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 0
    assert (scoped / "rules" / "x.md").read_text() == \
        "# Title\n\nalpha\ninserted\nbeta\ngamma\ndelta\n"


def test_apply_preserves_missing_trailing_newline(mod, scoped, tmp_path,
                                                  ledgered):
    (scoped / "rules" / "x.md").write_bytes(b"# Title\n\nalpha\nbeta")
    git(scoped, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-aqm", "nonl")
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n alpha\n-beta\n+BETA\n")
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 0
    assert (scoped / "rules" / "x.md").read_bytes() == b"# Title\n\nalpha\nBETA"


CREATED_RULE_DIFF = (
    # valid-rule-shaped content: once Task 7 lands, the REAL asset
    # validator gates creations too — a bare "one\ntwo" new rules/ file
    # would carry NEW rule-title/TRIGGER warnings and be refused. The
    # implementer verifies these two lines satisfy the validator's rule
    # checks (read validate_one) before relying on them.
    "--- /dev/null\n+++ b/rules/new.md\n@@\n"
    "+# New rule\n+\n+TRIGGER: when testing this fixture.\n+one\n+two\n")
CREATED_RULE_TEXT = "# New rule\n\nTRIGGER: when testing this fixture.\none\ntwo\n"


def test_apply_creation_and_revert_roundtrip(mod, scoped, tmp_path, ledgered):
    p = make_proposal(tmp_path, [str(scoped / "rules" / "new.md")],
                      CREATED_RULE_DIFF)
    assert mod.main(["apply", "--proposal", str(p)]) == 0
    assert (scoped / "rules" / "new.md").read_text() == CREATED_RULE_TEXT
    # created new.md is untracked (dirty); the immediate undo uses --force-dirty
    assert mod.main(["revert", "--proposal", str(p), "--force-dirty"]) == 0
    assert not (scoped / "rules" / "new.md").exists()


def test_revert_creation_refuses_when_content_diverged(mod, scoped,
                                                       tmp_path, ledgered):
    p = make_proposal(tmp_path, [str(scoped / "rules" / "new.md")],
                      CREATED_RULE_DIFF)
    assert mod.main(["apply", "--proposal", str(p)]) == 0
    (scoped / "rules" / "new.md").write_text(CREATED_RULE_TEXT + "EDITED\n")
    # --force-dirty so the refusal comes from the content-divergence guard
    # (the thing under test), not the M2 dirty preflight.
    assert mod.main(["revert", "--proposal", str(p), "--force-dirty"]) == 1


def test_revert_reanchored_edit(mod, scoped, tmp_path, ledgered):
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n"
        "@@ -3,9 +3,9 @@\n alpha\n-beta\n+BETA\n gamma\n")
    before = (scoped / "rules" / "x.md").read_text()
    assert mod.main(["apply", "--proposal", str(p)]) == 0
    assert mod.main(["revert", "--proposal", str(p), "--force-dirty"]) == 0
    assert (scoped / "rules" / "x.md").read_text() == before


def test_apply_rollback_restores_all_on_late_failure(mod, postrun_of,
                                                     tmp_path, ledgered,
                                                     monkeypatch):
    """Two roots, both splice-mode; the second root's write explodes —
    the first root's already-written file must be restored byte-exact."""
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    for r in (r1, r2):
        (r / "rules").mkdir(parents=True)
        (r / "rules" / "x.md").write_text("alpha\nbeta\n")
        git(r, "init", "-q"); git(r, "add", "-A")
        git(r, "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-qm", "b")
    monkeypatch.setattr(postrun_of, "ALLOWED_TARGET_ROOTS", [r1, r2])
    p = make_proposal(
        tmp_path, [str(r1 / "rules" / "x.md"), str(r2 / "rules" / "x.md")],
        "--- " + str(r1 / "rules" / "x.md") + "\n"
        "+++ " + str(r1 / "rules" / "x.md") + "\n@@\n alpha\n+ONE\n"
        "--- " + str(r2 / "rules" / "x.md") + "\n"
        "+++ " + str(r2 / "rules" / "x.md") + "\n@@\n alpha\n+TWO\n")
    real_write = mod._write_text
    calls = {"n": 0}

    def exploding_write(path, content):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        real_write(path, content)

    monkeypatch.setattr(mod, "_write_text", exploding_write)
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 1
    assert (r1 / "rules" / "x.md").read_text() == "alpha\nbeta\n"
    assert (r2 / "rules" / "x.md").read_text() == "alpha\nbeta\n"


def test_act_verify_catches_corrupted_splice(mod, scoped, tmp_path,
                                             ledgered, monkeypatch):
    """Independence proof for act-verification (spec Testing §2): corrupt
    the anchor ARITHMETIC (shift every anchor one line down). Layer (a)
    'written == precomputed' cannot see it — the write faithfully writes
    the wrong content; the git-generated -U0 invariant must catch it and
    the apply must roll back.

    RED-proof: this test IS the red proof for the invariant — with
    _act_verify stubbed to `return ""` in a scratch copy, this test fails
    (the corrupted content survives, no rollback).

    RED-PROOF EXECUTED 2026-07-22 (scratch copy of gardener_apply.py with
    the whole _act_verify body replaced by `return ""`). Observed
    (pytest -q against the scratch copy):
        rc = mod.main(["apply", "--proposal", str(p)])
        >       assert rc == 1
        E       assert 0 == 1
        Captured stdout:
        gardener-apply: applied r1-1 (reanchored) to 1 root(s); ...
        FAILED ...::test_act_verify_catches_corrupted_splice
        1 failed
    (with the act-verify gate gone the skewed-anchor splice writes the
    wrong content, apply returns 0, and no rollback fires — the corrupted
    file survives). Restored copy: GREEN."""
    real_anchor = mod.anchor_hunks

    def skewed_anchor(file_lines, bodies, starts=None):
        return [(pos + 1, old, new)
                for pos, old, new in real_anchor(file_lines, bodies, starts)]

    monkeypatch.setattr(mod, "anchor_hunks", skewed_anchor)
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n"
        "@@ -3,9 +3,9 @@\n alpha\n-beta\n+BETA\n gamma\n")
    before = (scoped / "rules" / "x.md").read_text()
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 1
    assert (scoped / "rules" / "x.md").read_text() == before  # rolled back


def test_act_verify_accepts_blank_adjacent_insertion(mod, scoped, tmp_path,
                                                     ledgered):
    """I1: the generator's most common insertion shape — inserting
    [blank, NEW-PARA] after a context line that is FOLLOWED by an existing
    blank — must NOT be false-refused. git renders a MINIMAL -U0 diff:
    it pairs the inserted blank with the existing following blank and
    attributes added=[NEW-PARA, blank] one line lower, so the OLD exact
    global -/+ SEQUENCE equality saw added ["NEW-PARA", ""] != proposal's
    ["", "NEW-PARA"] and rolled back a byte-correct write (live specimen
    75003-4 — the error "does not equal the proposal's -/+ lines" reads like
    engine corruption).

    Byte-correct result of ` A\\n+\\n+NEW-PARA` against "A\\n\\nB\\n" is
    "A\\n\\nNEW-PARA\\n\\nB\\n" (TWO blanks: the newly-inserted one plus the
    pre-existing one). The dispatch brief's "A\\n\\nNEW-PARA\\nB\\n" collapsed
    the pre-existing blank by mistake; the two-blank form is what the hunk
    actually applies to.

    RED-proof (this test IS its own red proof, executed 2026-07-22 against
    the pre-fix gardener_apply.py): the apply FAILS with
        act-verify FAILED for rules/x.md: the change actually written to disk
        does not equal the proposal's -/+ lines (git saw 0-/2+, proposal has
        0-/2+)
    rc==1, file rolled back to "A\\n\\nB\\n". With the multiset comparison
    (+ the trailing-edge count-0 region band) the apply is rc==0.
    """
    (scoped / "rules" / "x.md").write_text("A\n\nB\n")
    git(scoped, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-aqm", "blank-adjacent")
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@ -1,1 +1,3 @@\n A\n+\n+NEW-PARA\n")
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 0
    assert (scoped / "rules" / "x.md").read_text() == "A\n\nNEW-PARA\n\nB\n"


def test_apply_failure_ledgers_class_token(mod, scoped, tmp_path, ledgered):
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n NOT-IN-FILE\n+x\n")
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 1
    events = [__import__("json").loads(l)
              for l in ledgered.read_text().splitlines()]
    last = events[-1]
    assert last["event"] == "proposal_apply_failed"
    assert "(drifted)" in last["reasons"]
    assert "path" not in last


def test_apply_top_of_file_prepend(mod, scoped, tmp_path, ledgered):
    """A leading-edge insert (trailing-context-only hunk — the only shape the
    re-anchor path can carry at BOF, since it cannot supply leading context).
    git diff -U0 renders a top-of-file prepend as `@@ -0,0 +1 @@`, attributing
    the count-0 insertion to old_line 0 = one below the anchored region start
    (rs = pos+1 = 1). apply must succeed and the file must start with the
    prepended heading.

    The prepended line is itself a '# ' heading so the post-edit file still
    satisfies the canon gate's real asset-validator W-RULE-TITLE check
    (Task 7 gates this non-test-bearing root): the BOF-arithmetic intent is
    unchanged — still a single-line trailing-context-only insert at line 0.

    RED before the region-arithmetic fix: act-verify FAILED (change at old
    lines 0,0 falls outside every anchored region), file rolled back — the
    `rs <= start` bound (1 <= 0) rejected a byte-correct write."""
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n+# PREPENDED\n # Title\n")
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 0
    assert (scoped / "rules" / "x.md").read_text() == \
        "# PREPENDED\n# Title\n\nalpha\nbeta\ngamma\ndelta\n"


def test_apply_mid_file_insert_before(mod, scoped, tmp_path, ledgered):
    """Insert-before a mid-file line: git diff -U0 renders it as
    `@@ -(N-1),0 +N @@`, again attributing the count-0 insertion one below the
    anchored region start. apply must succeed and INSERT must land immediately
    before gamma.

    RED before the region-arithmetic fix: act-verify FAILED (change at old
    lines 4,0 falls outside every anchored region), file rolled back — the
    `rs <= start` bound (5 <= 4) rejected a byte-correct write."""
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n+INSERT\n gamma\n")
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 0
    assert (scoped / "rules" / "x.md").read_text() == \
        "# Title\n\nalpha\nbeta\nINSERT\ngamma\ndelta\n"


# ---- canon-write gate (Task 7, spec Fix 4) ----

def _make_test_bearing(root, passing=True):
    """Give a repo fixture a real, tiny pytest suite + a .venv/bin/python that
    execs the test interpreter, so `-m pytest` runs the real pytest installed
    in the dev venv.

    Uses a WRAPPER script (`exec "<sys.executable>" "$@"`), not a bare symlink.
    The 2026-07-22 spike that blessed the symlink ran only on darwin, where a
    `<root>/.venv/bin/python -> <interp>` symlink with no pyvenv.cfg resolves
    site-packages fine. On Linux (CI) it does NOT — launching the interpreter
    through such a symlink loses the venv's site-packages, so pytest is not
    importable and the inner run dies `No module named pytest` with EMPTY
    stdout (CI run 29931892118, this repo's first CI red). The wrapper execs
    the interpreter directly, so site-packages resolve on every platform."""
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n")
    (root / "tests").mkdir(exist_ok=True)
    guard = ("def test_guard():\n"
             "    text = open('rules/x.md').read()\n"
             + ("    assert True\n" if passing else
                "    assert 'FORBIDDEN' not in text\n"))
    (root / "tests" / "test_guard.py").write_text(guard)
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    import sys as _sys
    py = venv_bin / "python"
    py.write_text(f'#!/bin/sh\nexec "{_sys.executable}" "$@"\n')
    py.chmod(0o755)


def test_canon_gate_refuses_apply_that_breaks_the_suite(mod, scoped,
                                                        tmp_path, ledgered):
    """The incident shape: an apply that would leave the repo's own suite
    red must be refused and rolled back — class canon-gate.

    RED-proof (spec Testing §2): with the _canon_gate call removed from
    cmd_apply in a scratch copy, this apply LANDS and the repo is left
    red — this test then fails.

    RED-PROOF EXECUTED 2026-07-22 (scratch copy of gardener_apply.py with
    the `gate_summaries = _canon_gate(plan, pre_validator)` call and its
    try/except block deleted from cmd_apply, `gate_summaries = {}` in its
    place). Observed (pytest -q against the scratch copy):
        rc = mod.main(["apply", "--proposal", str(p)])
        >       assert rc == 1
        E       assert 0 == 1
        Captured stdout:
        gardener-apply: applied r1-1 (reanchored) to 1 root(s); ...
        FAILED ...::test_canon_gate_refuses_apply_that_breaks_the_suite
    (with the gate gone the FORBIDDEN-token insert lands, apply returns 0,
    and the repo's own suite is left red — the incident's exact shape).
    Restored copy: GREEN."""
    _make_test_bearing(scoped, passing=False)
    git(scoped, "add", "-A")
    git(scoped, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-qm", "suite")
    before = (scoped / "rules" / "x.md").read_text()
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n alpha\n+FORBIDDEN token\n")
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 1
    assert (scoped / "rules" / "x.md").read_text() == before  # rolled back


def test_canon_gate_passes_and_ledgers_green_suite(mod, scoped, tmp_path,
                                                   ledgered):
    _make_test_bearing(scoped, passing=True)
    git(scoped, "add", "-A")
    git(scoped, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-qm", "suite")
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n alpha\n+benign\n")
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 0
    events = [__import__("json").loads(l)
              for l in ledgered.read_text().splitlines()]
    assert events[-1]["event"] == "proposal_applied"
    assert events[-1]["canon_gate"].startswith("pytest:")


def test_canon_gate_failure_ledgers_class_token(mod, scoped, tmp_path,
                                                ledgered):
    """A canon-gate refusal must carry the grep-able (canon-gate) token in
    the proposal_apply_failed ledger reasons (spec Fix 4: "consumers grep
    one token"; matches the repo's ({klass}) convention that
    test_apply_failure_ledgers_class_token asserts for drifted)."""
    _make_test_bearing(scoped, passing=False)
    git(scoped, "add", "-A")
    git(scoped, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-qm", "suite")
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n alpha\n+FORBIDDEN token\n")
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 1
    events = [__import__("json").loads(l)
              for l in ledgered.read_text().splitlines()]
    last = events[-1]
    assert last["event"] == "proposal_apply_failed"
    assert "(canon-gate)" in last["reasons"]
    assert "path" not in last


def test_canon_gate_attributes_preexisting_red(mod, scoped, tmp_path,
                                               ledgered, capsys):
    """Repo red BEFORE the apply: refuse, but never blame the proposal."""
    _make_test_bearing(scoped, passing=False)
    (scoped / "rules" / "x.md").write_text(
        "# Title\n\nalpha\nbeta\ngamma\ndelta\nFORBIDDEN already\n")
    git(scoped, "add", "-A")
    git(scoped, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-qm", "red")
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n alpha\n+benign\n")
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "pre-existing" in err


def test_canon_gate_refuses_when_venv_missing(mod, scoped, tmp_path,
                                              ledgered, capsys):
    """Missing harness must fail LOUD, never silently skip (I4 lesson)."""
    (scoped / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (scoped / "tests").mkdir()
    git(scoped, "add", "-A")
    git(scoped, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-qm", "tb")
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n alpha\n+benign\n")
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 1
    assert "no .venv" in capsys.readouterr().err


def test_canon_gate_validator_new_finding_refuses(mod, scoped, tmp_path,
                                                  ledgered, monkeypatch):
    """Non-test-bearing root: NEW asset-validator findings refuse; the
    pre-existing set does not (attribution by pre/post diff)."""
    calls = {"n": 0}

    def fake_findings(root, rels):
        calls["n"] += 1
        return (({"old-finding"}, 1) if calls["n"] == 1
                else ({"old-finding", "NEW: leaked token"}, 1))

    monkeypatch.setattr(mod, "_asset_validator_findings", fake_findings)
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n alpha\n+benign\n")
    before = (scoped / "rules" / "x.md").read_text()
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 1
    assert (scoped / "rules" / "x.md").read_text() == before


def test_canon_gate_validator_no_new_findings_passes(mod, scoped, tmp_path,
                                                     ledgered, monkeypatch):
    monkeypatch.setattr(mod, "_asset_validator_findings",
                        lambda root, rels: ({"old-finding"}, 1))
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n alpha\n+benign\n")
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 0
    events = [__import__("json").loads(l)
              for l in ledgered.read_text().splitlines()]
    assert events[-1]["canon_gate"].startswith(
        "asset-validator:no-new-findings:checked=")


def test_canon_gate_validator_timeout_refuses(mod, scoped, tmp_path,
                                              ledgered, monkeypatch):
    """The validator's SIGALRM fail-soft os._exit(0)s with EMPTY stdout on
    timeout — right for its commit hook, fatal here. Empty output must
    REFUSE, never parse as zero findings (spec Fix 4 failure-shape rule).

    RED-proof (spec Fix 4): weaken the empty-output check in a scratch
    copy (treat empty stdout as no warnings) — this test then FAILS: the
    timed-out gate blesses the write.

    RED-PROOF EXECUTED 2026-07-22 (scratch copy of gardener_apply.py with
    the `proc.returncode != 0 or not proc.stdout.strip()` guard in
    _asset_validator_findings weakened to `proc.returncode != 0` and an
    early `if not proc.stdout.strip(): return set(), 0` added — i.e. empty
    stdout treated as zero findings). Observed (pytest -q against the
    scratch copy):
        rc = mod.main(["apply", "--proposal", str(p)])
        >       assert rc == 1
        E       assert 0 == 1
        Captured stdout:
        gardener-apply: applied r1-1 (reanchored) to 1 root(s); ...
        FAILED ...::test_canon_gate_validator_timeout_refuses
    (the timed-out pre AND post runs both return an empty finding set, the
    diff is empty, the gate passes and the write is blessed). Restored
    copy: GREEN."""
    monkeypatch.setenv("ASSET_VALIDATOR_TEST_SLEEP", "3")
    monkeypatch.setattr(mod, "_VALIDATOR_TIMEOUT_SEC", 1)
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n alpha\n+benign\n")
    before = (scoped / "rules" / "x.md").read_text()
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 1
    assert (scoped / "rules" / "x.md").read_text() == before  # rolled back


def test_canon_gate_real_validator_runs(mod, scoped, tmp_path, ledgered):
    """Integration: the REAL sibling asset_validator.py runs for a
    non-test-bearing root (no monkeypatch) and the ledger records its
    verdict.

    `checked=N` is how many files were HANDED to the validator, NOT how
    many it classified: asset_validator emits files_checked = len(files it
    was given). The vacuous-arm (absolute-path) mutation leaves checked=1
    intact — the validator still counts the file it was handed, it just
    classifies nothing and returns zero warnings. So checked= is a
    provenance record, not the vacuity guard. The guard that the path form
    actually reaches an asset class is
    test_canon_gate_real_validator_new_finding_refuses, which the
    absolute-path mutation DOES flip (see its RED-proof)."""
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n alpha\n+benign\n")
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 0
    events = [__import__("json").loads(l)
              for l in ledgered.read_text().splitlines()]
    assert events[-1]["canon_gate"] == "asset-validator:no-new-findings:checked=1"


def test_canon_gate_real_validator_new_finding_refuses(mod, scoped,
                                                       tmp_path, ledgered):
    """The REAL validator must be able to REFUSE — no monkeypatch anywhere.
    The apply degrades rules/x.md in a way the validator warns about
    post-edit but not pre-edit (a NEW finding). Verified 2026-07-22:
    replacing the leading '# Title' heading with plain text fires
    W-RULE-TITLE post-edit (absent pre-edit — the heading was valid), and
    the check stays entirely inside the fixture root.

    RED-proof (plan-review r2 CRITICAL): with _asset_validator_findings
    passing ABSOLUTE paths (the vacuous form this fixture exists to kill),
    validate_one classifies nothing, zero warnings pre AND post, and this
    test FAILS (the apply lands).

    RED-PROOF EXECUTED 2026-07-22 (scratch copy of gardener_apply.py with
    the `files = [rel for rel in rels if ...]` line in
    _asset_validator_findings changed to pass `os.path.join(root, rel)`
    absolute paths to the validator). Observed (pytest -q against the
    scratch copy):
        rc = mod.main(["apply", "--proposal", str(p)])
        >       assert rc == 1
        E       assert 0 == 1
        Captured stdout:
        gardener-apply: applied r1-1 (reanchored) to 1 root(s); ...
        FAILED ...::test_canon_gate_real_validator_new_finding_refuses
    (absolute paths match no asset class in validate_one — 0 warnings pre
    AND post, empty diff, the W-RULE-TITLE regression sails through and the
    apply lands). Restored copy: GREEN."""
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n-# Title\n+plain not-a-heading\n")
    before = (scoped / "rules" / "x.md").read_text()
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 1
    assert (scoped / "rules" / "x.md").read_text() == before  # rolled back


def test_canon_gate_launch_failure_message_carries_stderr(mod, scoped,
                                                          tmp_path, ledgered,
                                                          capsys):
    """A canon-gate suite that fails to LAUNCH (empty stdout, error on stderr —
    the CI-runner `No module named pytest` shape) must still yield a
    diagnosable refusal: the message combines stdout AND stderr, so a
    python-level launch failure is never an empty tail. Refusal semantics
    unchanged (rc=1, rolled back, (canon-gate) token).

    RED-proof: revert the stdout+stderr tail combine in cmd_apply's _SuiteRed
    handler (stdout-only) in a scratch copy -> the refusal message loses the
    stderr text -> this test's `boom` assertion RED. Verified 2026-07-22;
    pasted in the report."""
    _make_test_bearing(scoped, passing=True)
    # replace the venv python with a launcher that fails like a broken venv:
    # empty stdout, diagnostic on stderr, exit 1.
    py = scoped / ".venv" / "bin" / "python"
    py.write_text('#!/bin/sh\necho "boom: No module named pytest" >&2\nexit 1\n')
    py.chmod(0o755)
    git(scoped, "add", "-A")
    git(scoped, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-qm", "suite")
    before = (scoped / "rules" / "x.md").read_text()
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n alpha\n+benign\n")
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 1
    assert (scoped / "rules" / "x.md").read_text() == before  # rolled back
    err = capsys.readouterr().err
    assert "boom: No module named pytest" in err
    assert "(canon-gate)" in err
