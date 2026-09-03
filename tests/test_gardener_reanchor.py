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


def test_lenient_parse_bare_at_at(mod):
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
    with pytest.raises(mod.ApplyError) as exc:
        mod.lenient_parse("@@ -1,1 +1,1 @@\n-x\n+y\n")
    assert exc.value.code == 2


def test_lenient_parse_noise_between_header_and_hunk_ok(mod):
    fds = mod.lenient_parse(
        "diff --git a/f.md b/f.md\nindex abc..def 100644\n"
        "--- a/f.md\n+++ b/f.md\n@@\n ctx\n+x\n")
    assert len(fds) == 1


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
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n"
        "@@ -3,9 +3,9 @@\n alpha\n-beta\n+BETA\n gamma\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass == "reanchorable"


def test_classify_reanchorable_no_trailing_context_midfile(mod, scoped, tmp_path):
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
    (scoped / "rules" / "other.md").write_text("alpha\n")
    git(scoped, "add", "-A")
    git(scoped, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-qm", "other")
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/other.md\n+++ b/rules/other.md\n@@\n alpha\n+x\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass == "out-of-scope"


def test_classify_out_of_scope_absolute_undeclared_path(mod, scoped, tmp_path):
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
    rp = plan[os.path.realpath(str(scoped))]
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
    assert cls.klass == "reanchorable"
    root2 = tmp_path / "gone-root"
    monkeypatch.setattr(postrun_of, "ALLOWED_TARGET_ROOTS", [root2])
    (tmp_path / "d2").mkdir(exist_ok=True)
    p2 = make_proposal(tmp_path / "d2", [str(root2 / "rules" / "x.md")],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n alpha\n+x\n")
    cls2 = mod.classify_proposal(str(p2), env_lenient=True)
    assert cls2.klass in ("skipped-env", "missing-file")


def test_phantom_file_header_from_dash_content_fails_loud(mod, scoped, tmp_path):
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n"
        "@@ -1,99 +1,99 @@\n alpha\n--- not-a-header\n+++ also-content\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass in ("out-of-scope", "malformed", "drifted")
    assert cls.klass != "reanchorable"


def test_candidate_reject_fence_interior_and_old_block_exception(mod):
    fenced = [(0, 4)]
    assert mod._candidate_reject_reason(2, ["x", "y"], None, fenced) == \
        "is inside a fenced code block"
    assert mod._candidate_reject_reason(2, ["```", "x"], None, fenced) is None


def test_anchor_discards_fence_interior_decoy(mod):
    file_lines = ["intro", "```", "TARGET", "```", "tail"]
    with pytest.raises(mod.ApplyError) as exc:
        mod.anchor_hunks(file_lines, [[" TARGET", "+X"]])
    assert exc.value.klass == "drifted"
    assert "inside a fenced code block" in str(exc.value)


def test_anchor_keeps_fence_interior_when_declared_start_inside(mod):
    file_lines = ["a", "b", "```", "TARGET", "```", "tail"]
    anchored = mod.anchor_hunks(file_lines, [[" TARGET", "+X"]], starts=[4])
    assert anchored[0][0] == 3


def test_anchor_numeric_band_discards_far_decoy(mod):
    file_lines = ["x"] * 200 + ["TARGET"]
    with pytest.raises(mod.ApplyError) as exc:
        mod.anchor_hunks(file_lines, [[" TARGET", "+Y"]], starts=[5])
    assert exc.value.klass == "drifted"
    assert "declared start 5" in str(exc.value)
    assert "band 100" in str(exc.value)


def test_anchor_numeric_band_tolerates_near(mod):
    file_lines = ["x"] * 20 + ["TARGET"]
    anchored = mod.anchor_hunks(file_lines, [[" TARGET", "+Y"]], starts=[5])
    assert anchored[0][0] == 20


def test_anchor_bare_at_at_no_band(mod):
    file_lines = ["x"] * 200 + ["TARGET"]
    anchored = mod.anchor_hunks(file_lines, [[" TARGET", "+Y"]], starts=[None])
    assert anchored[0][0] == 200


def _commit(scoped, msg):
    git(scoped, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-aqm", msg)


def test_classify_A1_out_of_order_decoy_refused(mod, scoped, tmp_path):
    lines = ["# Title", "", "ANCHOR-EARLY"]
    lines += [f"filler-{i}" for i in range(126)]
    lines += ["ANCHOR-LATE", "ANCHOR-EARLY", "tail"]
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
    lines = ["# Title", "", "para", "", "normal", "more", "", "another",
             "yet", "final",
             "```", "DECOY-INSIDE-FENCE", "```", "tail"]
    (scoped / "rules" / "x.md").write_text("\n".join(lines) + "\n")
    _commit(scoped, "a2")
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n"
        "@@ -5,9 +5,9 @@\n DECOY-INSIDE-FENCE\n+EDIT\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass == "drifted"
    assert "inside a fenced code block" in cls.detail


def test_classify_legit_inside_fence_applies(mod, scoped, tmp_path):
    lines = ["# Title", "", "para", "", "normal", "more", "", "another",
             "yet", "final",
             "```", "DECOY-INSIDE-FENCE", "```", "tail"]
    (scoped / "rules" / "x.md").write_text("\n".join(lines) + "\n")
    _commit(scoped, "legit")
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n"
        "@@ -12,9 +12,9 @@\n DECOY-INSIDE-FENCE\n+EDIT\n")
    cls = mod.classify_proposal(str(p))
    assert cls.klass == "reanchorable"


_M7_NESTED = [
    "# Title", "", "TRIGGER: fixture.", "", "intro",
    "````",
    "inner example",
    "```",
    "DECOY-BLOCK",
    "````",
    "tail",
]


def test_fenced_ranges_length_aware(mod):
    assert mod._fenced_ranges(_M7_NESTED) == [(5, 9)]
    assert mod._fenced_ranges(["a", "```", "x", "```", "b"]) == [(1, 3)]
    assert mod._backtick_run_len("````") == 4
    assert mod._backtick_run_len("```diff") == 3
    assert mod._backtick_run_len("``") == 0
    assert mod._backtick_run_len("plain text") == 0


def test_classify_M7_nested_fence_decoy_refused(mod, scoped, tmp_path):
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
    assert "target drifted since drafting" not in err


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
    "--- /dev/null\n+++ b/rules/new.md\n@@\n"
    "+# New rule\n+\n+TRIGGER: when testing this fixture.\n+one\n+two\n")
CREATED_RULE_TEXT = "# New rule\n\nTRIGGER: when testing this fixture.\none\ntwo\n"


def test_apply_creation_and_revert_roundtrip(mod, scoped, tmp_path, ledgered):
    p = make_proposal(tmp_path, [str(scoped / "rules" / "new.md")],
                      CREATED_RULE_DIFF)
    assert mod.main(["apply", "--proposal", str(p)]) == 0
    assert (scoped / "rules" / "new.md").read_text() == CREATED_RULE_TEXT
    assert mod.main(["revert", "--proposal", str(p), "--force-dirty"]) == 0
    assert not (scoped / "rules" / "new.md").exists()


def test_revert_creation_refuses_when_content_diverged(mod, scoped,
                                                       tmp_path, ledgered):
    p = make_proposal(tmp_path, [str(scoped / "rules" / "new.md")],
                      CREATED_RULE_DIFF)
    assert mod.main(["apply", "--proposal", str(p)]) == 0
    (scoped / "rules" / "new.md").write_text(CREATED_RULE_TEXT + "EDITED\n")
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
    assert (scoped / "rules" / "x.md").read_text() == before


def test_act_verify_accepts_blank_adjacent_insertion(mod, scoped, tmp_path,
                                                     ledgered):
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
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n+# PREPENDED\n # Title\n")
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 0
    assert (scoped / "rules" / "x.md").read_text() == \
        "# PREPENDED\n# Title\n\nalpha\nbeta\ngamma\ndelta\n"


def test_apply_mid_file_insert_before(mod, scoped, tmp_path, ledgered):
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n+INSERT\n gamma\n")
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 0
    assert (scoped / "rules" / "x.md").read_text() == \
        "# Title\n\nalpha\nbeta\nINSERT\ngamma\ndelta\n"


def _make_test_bearing(root, passing=True):
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
    _make_test_bearing(scoped, passing=False)
    git(scoped, "add", "-A")
    git(scoped, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-qm", "suite")
    before = (scoped / "rules" / "x.md").read_text()
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n alpha\n+FORBIDDEN token\n")
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 1
    assert (scoped / "rules" / "x.md").read_text() == before


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
    monkeypatch.setenv("ASSET_VALIDATOR_TEST_SLEEP", "3")
    monkeypatch.setattr(mod, "_VALIDATOR_TIMEOUT_SEC", 1)
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n alpha\n+benign\n")
    before = (scoped / "rules" / "x.md").read_text()
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 1
    assert (scoped / "rules" / "x.md").read_text() == before


def test_canon_gate_real_validator_runs(mod, scoped, tmp_path, ledgered):
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n alpha\n+benign\n")
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 0
    events = [__import__("json").loads(l)
              for l in ledgered.read_text().splitlines()]
    assert events[-1]["canon_gate"] == "asset-validator:no-new-findings:checked=1"


def test_canon_gate_real_validator_new_finding_refuses(mod, scoped,
                                                       tmp_path, ledgered):
    p = make_proposal(tmp_path, [target(scoped)],
        "--- a/rules/x.md\n+++ b/rules/x.md\n@@\n-# Title\n+plain not-a-heading\n")
    before = (scoped / "rules" / "x.md").read_text()
    rc = mod.main(["apply", "--proposal", str(p)])
    assert rc == 1
    assert (scoped / "rules" / "x.md").read_text() == before


def test_canon_gate_launch_failure_message_carries_stderr(mod, scoped,
                                                          tmp_path, ledgered,
                                                          capsys):
    _make_test_bearing(scoped, passing=True)
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
    assert (scoped / "rules" / "x.md").read_text() == before
    err = capsys.readouterr().err
    assert "boom: No module named pytest" in err
    assert "(canon-gate)" in err
