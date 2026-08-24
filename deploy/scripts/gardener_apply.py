#!/usr/bin/env python3
"""Gardener proposal actuator (T11) — apply an accepted proposal's ## Diff
via `git apply` with a context check, replacing model-driven edits.

  gardener_apply.py check  --proposal <path>            read-only --check
  gardener_apply.py apply  --proposal <path> [--force-dirty]
  gardener_apply.py revert --proposal <path>            git apply -R
  gardener_apply.py currency [--proposal <path> ...]    read-only staleness table

Diff-header path forms (same family the postrun validator accepts):
absolute, ~-prefixed, and a/<rel> b/<rel> (suffix-matched against declared
`targets:`). Headers are rewritten repo-relative per affected root;
`diff --git`/`index`/mode lines are dropped — a foreign blob-SHA makes a
patch internally inconsistent. new-asset proposals arrive as new-file diffs
(`--- /dev/null`); prose-format legacy proposals fail with a distinct
message and stay on the manual path.

Failure policy: strict `git apply --check` is tried first; a root it refuses
falls back to content-anchored re-anchor — exact-unique, ordered, never fuzzy,
never guessing. Anything still unresolved refuses under a classified label
(drifted / ambiguous / missing-file / malformed / out-of-scope); never a fuzzy
apply, never a silent model edit.

Ledger events (`proposal_applied` / `proposal_apply_failed` /
`proposal_reverted`) reference the proposal by `proposal_id` ONLY — never a
top-level `path` key: known_from_ledger() derives the postrun known-set from
ANY event carrying `path`, and an apply-time event would hide a
not-yet-validated pending proposal from validation.

Standalone, stdlib-only, py3.9-compatible.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
from collections import Counter

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
import gardener_postrun  # sibling deployed script: parser, ledger, roots


class ApplyError(Exception):
    """Refusal with a process exit code: 1 = blocked (context/dirty/state),
    2 = malformed proposal or usage error. `klass` carries the failure-class
    token (clean|reanchorable|drifted|ambiguous|missing-file|malformed|
    out-of-scope) when known."""

    def __init__(self, message: str, code: int = 1, klass=None):
        super().__init__(message)
        self.code = code
        self.klass = klass


# ---- proposal parsing -------------------------------------------------

_FENCE_OPEN = re.compile(r"^```diff\s*$")
_FENCE_CLOSE = re.compile(r"^```\s*$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def extract_diff_text(body: str) -> str:
    """Concatenated content of every ```diff fence. Distinct error for the
    pre-T11 prose new-asset format (no fence at all)."""
    blocks, inside, cur = [], False, []
    for line in body.splitlines():
        if not inside and _FENCE_OPEN.match(line):
            inside, cur = True, []
            continue
        if inside and _FENCE_CLOSE.match(line):
            blocks.append("\n".join(cur))
            inside = False
            continue
        if inside:
            cur.append(line)
    if not blocks:
        raise ApplyError(
            "no ```diff block in proposal (pre-T11 prose format?) — "
            "apply manually or re-draft", code=2)
    return "\n".join(blocks)


class FileDiff:
    """One file's `---`/`+++` header pair + verbatim hunk lines."""
    __slots__ = ("old_raw", "new_raw", "hunks")

    def __init__(self, old_raw, new_raw, hunks):
        self.old_raw = old_raw
        self.new_raw = new_raw
        self.hunks = hunks


def split_file_diffs(diff_text: str):
    """Split unified-diff text into FileDiffs, tracking @@ line counts so a
    removed line whose content starts with '-- ' is never misread as a file
    header. `diff --git`/`index`/mode noise between files is dropped."""
    lines = diff_text.splitlines()
    diffs, i = [], 0
    while i < len(lines):
        if lines[i].startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            # first token only: a `--- a/f\t<timestamp>` header must not carry
            # the timestamp into the path (paths with spaces are unsupported,
            # same as the postrun validator's \S+)
            old_tokens = lines[i][4:].strip().split()
            new_tokens = lines[i + 1][4:].strip().split()
            if not old_tokens or not new_tokens:
                raise ApplyError(f"malformed diff header at line {i + 1}", code=2)
            old_raw = old_tokens[0]
            new_raw = new_tokens[0]
            i += 2
            hunks = []
            while i < len(lines):
                m = _HUNK_RE.match(lines[i])
                if not m:
                    # a non-`@@` line between hunks of the same file ends
                    # this file's hunk collection — the post-parse hunk-count
                    # assertion below (raw `@@` headers vs headers actually
                    # collected into FileDiffs) is what catches later hunks
                    # silently dropped by a stray line; `git apply --check`
                    # only validates the already-truncated patch, so it is
                    # NOT a net for this failure mode.
                    break
                old_n = int(m.group(1) if m.group(1) is not None else "1")
                new_n = int(m.group(2) if m.group(2) is not None else "1")
                hunks.append(lines[i])
                i += 1
                seen_old = seen_new = 0
                while i < len(lines) and (seen_old < old_n or seen_new < new_n):
                    ln = lines[i]
                    if ln.startswith("\\"):
                        pass  # "\ No newline at end of file" — not counted
                    elif ln.startswith("-"):
                        seen_old += 1
                    elif ln.startswith("+"):
                        seen_new += 1
                    else:
                        seen_old += 1
                        seen_new += 1
                    hunks.append(ln)
                    i += 1
                if i < len(lines) and lines[i].startswith("\\"):
                    hunks.append(lines[i])
                    i += 1
            if not any(h.startswith("@@") for h in hunks):
                raise ApplyError(
                    f"diff for {new_raw or old_raw} has no @@ hunks "
                    "(not a unified diff?)", code=2)
            diffs.append(FileDiff(old_raw, new_raw, hunks))
            continue
        i += 1
    if not diffs:
        raise ApplyError(
            "```diff block contains no '--- '/'+++ ' file diffs "
            "(not a unified diff — pre-T11 prose new-asset?)", code=2)
    total_headers = sum(1 for ln in lines if _HUNK_RE.match(ln))
    parsed_headers = sum(1 for fd in diffs for h in fd.hunks if h.startswith("@@"))
    if total_headers != parsed_headers:
        raise ApplyError(
            f"diff has {total_headers} hunk header(s) but only {parsed_headers} "
            "were parsed into file diffs — a stray line between hunks truncates "
            "the patch; re-draft with clean unified-diff structure", code=2)
    return diffs


# ---- content-anchored re-apply (lenient path) --------------------------

_LENIENT_HUNK_RE = re.compile(r"^@@")
_LENIENT_HUNK_START_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _parse_declared_starts(header):
    """(old_start, new_start) 1-based from a numbered `@@ -N[,c] +M[,d] @@`
    header, or (None, None) for a bare `@@` — bare headers carry no arithmetic
    to sanity-check a candidate against, so the proximity band is inapplicable."""
    m = _LENIENT_HUNK_START_RE.match(header)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


class LenientFileDiff:
    """One file's headers + hunk bodies, parsed by structure not counts.
    `hunk_starts` runs parallel to `hunks`: each is the (old_start, new_start)
    declared in the hunk header (or (None, None) for a bare @@), carried for
    the anchor proximity band without changing the plain-list `hunks` shape
    existing callers depend on."""
    __slots__ = ("old_raw", "new_raw", "hunks", "hunk_starts")

    def __init__(self, old_raw, new_raw):
        self.old_raw = old_raw
        self.new_raw = new_raw
        self.hunks = []
        self.hunk_starts = []


def lenient_parse(diff_text):
    """Structure-driven unified-diff parse: hunk boundaries come from `@@`
    lines (numbered or bare) and `--- `/`+++ ` header pairs; the counts in
    hunk headers are ignored entirely (the generator writes them by hand
    and gets the arithmetic wrong). Body lines must look like diff lines
    (' ', '-', '+', '\\', or empty = blank context); anything else inside a
    hunk is malformed — loud, never silently absorbed or truncated."""
    lines = diff_text.splitlines()
    fds, i = [], 0
    cur_fd, cur_hunk = None, None
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("--- ") and i + 1 < len(lines) \
                and lines[i + 1].startswith("+++ "):
            old_tokens = ln[4:].strip().split()
            new_tokens = lines[i + 1][4:].strip().split()
            if not old_tokens or not new_tokens:
                raise ApplyError(
                    f"malformed diff header at line {i + 1}", code=2,
                    klass="malformed")
            cur_fd = LenientFileDiff(old_tokens[0], new_tokens[0])
            fds.append(cur_fd)
            cur_hunk = None
            i += 2
            continue
        if _LENIENT_HUNK_RE.match(ln):
            if cur_fd is None:
                raise ApplyError(
                    "hunk header before any '--- '/'+++ ' file header",
                    code=2, klass="malformed")
            cur_hunk = []
            cur_fd.hunks.append(cur_hunk)
            cur_fd.hunk_starts.append(_parse_declared_starts(ln))
            i += 1
            continue
        if cur_hunk is not None:
            if ln == "" or ln.startswith((" ", "+", "-", "\\")):
                cur_hunk.append(ln)
                i += 1
                continue
            raise ApplyError(
                f"non-diff line inside a hunk body: {ln[:80]!r}", code=2,
                klass="malformed")
        i += 1  # `diff --git`/`index`/mode noise outside hunk bodies
    if not fds:
        raise ApplyError(
            "```diff block contains no '--- '/'+++ ' file diffs", code=2,
            klass="malformed")
    for fd in fds:
        if not fd.hunks:
            raise ApplyError(
                f"diff for {fd.new_raw or fd.old_raw} has no @@ hunks",
                code=2, klass="malformed")
    return fds


def _trailing_blank_count(block):
    n = 0
    for ln in reversed(block):
        if ln != "":
            break
        n += 1
    return n


def hunk_blocks(body):
    """Hunk body -> (old_block, new_block): context+deletions and
    context+additions. A completely empty body line is blank context.
    Trailing blank context is trimmed only when old and new end in the
    SAME number of blanks (a between-hunk separator artifact); differing
    counts are a real change and are preserved."""
    old, new = [], []
    for ln in body:
        if ln.startswith("\\"):
            continue  # "\ No newline at end of file"
        if ln == "":
            old.append("")
            new.append("")
        elif ln.startswith(" "):
            old.append(ln[1:])
            new.append(ln[1:])
        elif ln.startswith("-"):
            old.append(ln[1:])
        else:  # "+"
            new.append(ln[1:])
    t_old, t_new = _trailing_blank_count(old), _trailing_blank_count(new)
    if t_old and t_old == t_new:
        old = old[:len(old) - t_old]
        new = new[:len(new) - t_new]
    return old, new


def hunk_change_lines(body):
    """Only the actual change: (-removed, +added) line contents, in order.
    Task-3 act-verification compares these against a git-generated -U0
    diff of what really changed on disk — independent of the splicer."""
    removed = [ln[1:] for ln in body
               if ln.startswith("-") and not ln.startswith("\\")]
    added = [ln[1:] for ln in body if ln.startswith("+")]
    return removed, added


def _find_block(file_lines, block, start):
    hits, m = [], len(block)
    for i in range(start, len(file_lines) - m + 1):
        if file_lines[i:i + m] == block:
            hits.append(i)
    return hits


_ANCHOR_PROXIMITY_BAND = 100


def _backtick_run_len(line):
    """Length of the leading backtick run (after lstrip) when it is a fence
    delimiter (>=3 backticks), else 0. CommonMark counts the run length: a
    fence's closer must be at least as long as its opener."""
    stripped = line.lstrip()
    n = 0
    for ch in stripped:
        if ch == "`":
            n += 1
        else:
            break
    return n if n >= 3 else 0


def _fenced_ranges(file_lines):
    """0-based (open_idx, close_idx) inclusive index pairs for each ``` fenced
    block, CommonMark fence-LENGTH-aware: a fence opened by a run of N
    backticks closes ONLY on a line whose leading backtick run is >= N — a
    shorter run inside (a ``` line within a ````-opened fence) is CONTENT, not
    a close. A length-blind toggle closed the outer fence early, so the fenced
    decoy sat "outside" a phantom-shortened fence and spliced rc=0 into the
    outer fence — the exact channel this guard exists to shut (M7). An unclosed
    trailing fence yields no bounded interior and is ignored."""
    ranges, open_idx, open_len = [], None, 0
    for i, ln in enumerate(file_lines):
        run = _backtick_run_len(ln)
        if run == 0:
            continue
        if open_idx is None:
            open_idx, open_len = i, run
        elif run >= open_len:
            ranges.append((open_idx, i))
            open_idx, open_len = None, 0
    return ranges


def _strictly_inside_fence(pos, length, fenced):
    """True when the candidate span [pos, pos+length-1] lies wholly BETWEEN a
    fence's delimiters (neither delimiter line is part of the span)."""
    end = pos + length - 1
    return any(fs < pos and end < fe for fs, fe in fenced)


def _line_in_fence(idx0, fenced):
    """True when 0-based line idx0 lies within a fenced block (delimiters incl.)."""
    return any(fs <= idx0 <= fe for fs, fe in fenced)


def _candidate_reject_reason(pos, old, declared_start, fenced):
    """Why an exact-match candidate at 0-based `pos` must be DISCARDED as a
    decoy, or None to keep it. `declared_start` is the hunk header's 1-based
    side line number, or None for a bare @@."""
    # (a) fence ineligibility: a match wholly inside a ``` block is a
    # self-quote decoy (these rule files quote themselves constantly) — UNLESS
    # the hunk visibly edits fence structure (its old-block carries a fence
    # delimiter line) or the declared start says the edit is meant to land
    # inside that same fence.
    if _strictly_inside_fence(pos, len(old), fenced):
        edits_fence = any(ln.lstrip().startswith("```") for ln in old)
        declared_inside = (declared_start is not None
                           and _line_in_fence(declared_start - 1, fenced))
        if not edits_fence and not declared_inside:
            return "is inside a fenced code block"
    # (b) numeric proximity band: hand-written arithmetic is off by lines, not
    # sections — a numbered hunk whose only match is hundreds of lines from the
    # declared start is anchoring on a decoy, not tolerating sloppy counts.
    if declared_start is not None:
        delta = abs((pos + 1) - declared_start)
        if delta > _ANCHOR_PROXIMITY_BAND:
            return (f"is {delta} lines from declared start {declared_start} "
                    f"(band {_ANCHOR_PROXIMITY_BAND})")
    return None


def anchor_hunks(file_lines, bodies, starts=None):
    """Locate each hunk's old_block in the file by exact contiguous match,
    region-scoped: hunk k is searched only after hunk k-1's end. Exactly
    one SURVIVING candidate anchors; zero or many refuse with a classified
    error — anchoring is deterministic, never fuzzy.

    Decoy-resistant: a candidate strictly inside a fenced code block is
    discarded (unless the hunk edits fence structure or its declared start
    points inside that fence), and — when the hunk header carries explicit
    numbers (`starts[k]` not None) — a candidate hundreds of lines from the
    declared start is discarded. 0 candidates after filtering => `drifted`
    naming the discard + why; >=2 => `ambiguous`. `starts` (parallel to
    `bodies`, the relevant-side 1-based declared start or None per hunk) is
    optional — omitted => no proximity band (bare-@@ behavior)."""
    fenced = _fenced_ranges(file_lines)
    anchored, search_from = [], 0
    for hi, body in enumerate(bodies):
        old, new = hunk_blocks(body)
        if not old:
            raise ApplyError(
                f"hunk {hi + 1}: no anchorable old lines "
                "(pure insertion without context)", code=2, klass="malformed")
        declared = None if starts is None else starts[hi]
        raw_hits = _find_block(file_lines, old, search_from)
        kept, discards = [], []
        for pos in raw_hits:
            why = _candidate_reject_reason(pos, old, declared, fenced)
            if why:
                discards.append((pos, why))
            else:
                kept.append(pos)
        if not kept:
            if discards:
                detail = "; ".join(
                    f"only match at line {pos + 1} {why}"
                    for pos, why in discards)
                raise ApplyError(f"hunk {hi + 1}: {detail}", code=1,
                                 klass="drifted")
            if _find_block(file_lines, old, 0):
                raise ApplyError(
                    f"hunk {hi + 1}: matches only before the previous hunk "
                    "(out of order)", code=1, klass="drifted")
            best = 0
            for k in range(len(old), 0, -1):
                if _find_block(file_lines, old[:k], 0):
                    best = k
                    break
            raise ApplyError(
                f"hunk {hi + 1}: old text not found in target "
                f"(longest matching prefix {best}/{len(old)} lines)",
                code=1, klass="drifted")
        if len(kept) > 1:
            raise ApplyError(
                f"hunk {hi + 1}: ambiguous — old text matches at "
                f"{len(kept)} positions", code=1, klass="ambiguous")
        anchored.append((kept[0], old, new))
        search_from = kept[0] + len(old)
    return anchored


def splice_lines(file_lines, anchored):
    """Build the post-apply content: replace each anchored old_block with
    its new_block, bottom-up so earlier positions stay valid."""
    out = list(file_lines)
    for pos, old, new in reversed(anchored):
        out[pos:pos + len(old)] = new
    return out


def read_file_lines(path):
    """(lines, had_trailing_newline). Splits on '\\n' ONLY — matching git's
    line semantics; str.splitlines() would also split on U+2028 etc. and
    silently diverge from git's numbering."""
    with open(path, encoding="utf-8") as fh:
        data = fh.read()
    had_nl = data.endswith("\n")
    lines = data.split("\n")
    if had_nl:
        lines.pop()
    return lines, had_nl


def join_file_lines(lines, had_trailing_nl):
    return "\n".join(lines) + ("\n" if had_trailing_nl else "")


# ---- path resolution / patch building ----------------------------------

def _resolve_one(raw: str, declared_abs):
    """Header path -> absolute realpath, or None for /dev/null. An absolute/~
    header must ALSO name a declared target — symmetric with the a/<rel> rule
    below (#208: a proposal that declares one path and patches another by
    absolute path must be refused, never silently applied)."""
    if raw == "/dev/null":
        return None
    if raw.startswith(("/", "~")):
        resolved = os.path.realpath(os.path.expanduser(raw))
        if resolved not in declared_abs:
            raise ApplyError(
                f"diff path {raw!r} is not among declared targets",
                code=2, klass="out-of-scope")
        return resolved
    rel = re.sub(r"^[ab]/", "", raw)
    for t in declared_abs:
        if t == rel or t.endswith(os.sep + rel):
            return t
    raise ApplyError(f"diff path {raw!r} matches no declared target",
                     code=2, klass="out-of-scope")


def _root_of(path: str) -> str:
    for root in gardener_postrun.ALLOWED_TARGET_ROOTS:
        root_r = os.path.realpath(str(root))
        if path == root_r or path.startswith(root_r + os.sep):
            return root_r
    raise ApplyError(f"path outside allowed roots (FR-8): {path}", code=2,
                     klass="out-of-scope")


def build_patches(diffs, declared_targets):
    """(patches, files): {root: patch_text} with headers rewritten
    repo-relative, and {root: [rel_path, ...]} for dirty-checks."""
    declared_abs = [os.path.realpath(os.path.expanduser(t))
                    for t in declared_targets]
    per_root, per_root_files = {}, {}
    for fd in diffs:
        old_abs = _resolve_one(fd.old_raw, declared_abs)
        new_abs = _resolve_one(fd.new_raw, declared_abs)
        if old_abs is not None and new_abs is not None and old_abs != new_abs:
            raise ApplyError("rename diffs are not supported", code=2)
        path = new_abs if new_abs is not None else old_abs
        if path is None:
            raise ApplyError("file diff with /dev/null on both sides", code=2)
        root = _root_of(path)
        rel = os.path.relpath(path, root)
        old_h = "/dev/null" if old_abs is None else "a/" + rel
        new_h = "/dev/null" if new_abs is None else "b/" + rel
        chunk = ["--- " + old_h, "+++ " + new_h] + fd.hunks
        per_root.setdefault(root, []).append("\n".join(chunk))
        per_root_files.setdefault(root, []).append(rel)
    patches = {root: "\n".join(chunks) + "\n" for root, chunks in per_root.items()}
    return patches, per_root_files


def load_proposal(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise ApplyError(f"cannot read proposal: {exc}", code=2)
    meta, body = gardener_postrun.parse_frontmatter(text)
    if not isinstance(meta, dict):
        raise ApplyError("no parseable frontmatter", code=2)
    return meta, body


def resolve(proposal_path: str):
    """proposal file -> (meta, patches, files) — everything the git layer needs."""
    meta, body = load_proposal(proposal_path)
    targets = gardener_postrun._as_list(meta.get("targets"))
    if not targets:
        raise ApplyError("proposal declares no targets", code=2)
    diffs = split_file_diffs(extract_diff_text(body))
    return meta, *build_patches(diffs, targets)


# ---- classification / apply-plan resolution -----------------------------

class Classification:
    __slots__ = ("klass", "detail")

    def __init__(self, klass, detail=""):
        self.klass = klass
        self.detail = detail


class FilePlan:
    __slots__ = ("rel", "action", "anchored", "new_lines", "bodies",
                 "starts", "had_nl")

    def __init__(self, rel, action, anchored=None, new_lines=None,
                 bodies=None, starts=None, had_nl=True):
        self.rel = rel
        self.action = action  # "splice" | "create" | "delete"
        self.anchored = anchored or []
        self.new_lines = new_lines or []
        self.bodies = bodies or []
        # declared side-starts parallel to bodies (proximity band at re-anchor)
        self.starts = starts or []
        self.had_nl = had_nl


class RootPlan:
    __slots__ = ("mode", "patch", "files", "rels")

    def __init__(self, mode, patch=None, files=None, rels=None):
        self.mode = mode  # "patch" | "patch-reverse" | "splice"
        self.patch = patch
        self.files = files or []
        self.rels = rels or []


def _lenient_root_plans(diff_text, declared_targets, only_roots=None):
    """Lenient parse + per-file anchoring -> {root: RootPlan(mode=splice)}.
    Raises classified ApplyErrors (drifted/ambiguous/missing-file/
    malformed/out-of-scope). `only_roots` (a set of root paths) restricts
    anchoring to the roots whose strict patch failed — a root git can
    already apply must never be spuriously refused by an ambiguous content
    anchor (plan-review M2)."""
    declared_abs = [os.path.realpath(os.path.expanduser(t))
                    for t in declared_targets]
    per_root = {}
    for fd in lenient_parse(diff_text):
        old_abs = _resolve_one(fd.old_raw, declared_abs)
        new_abs = _resolve_one(fd.new_raw, declared_abs)
        if old_abs is not None and new_abs is not None and old_abs != new_abs:
            raise ApplyError("rename diffs are not supported", code=2,
                             klass="malformed")
        path = new_abs if new_abs is not None else old_abs
        if path is None:
            raise ApplyError("file diff with /dev/null on both sides",
                             code=2, klass="malformed")
        root = _root_of(path)
        if only_roots is not None and root not in only_roots:
            continue
        rel = os.path.relpath(path, root)
        if new_abs is None:
            raise ApplyError(
                f"{rel}: file-deletion diffs are not supported in "
                "re-anchor mode", code=2, klass="malformed")
        if old_abs is None:  # creation
            if os.path.exists(path):
                raise ApplyError(
                    f"{rel}: new-file target already exists", code=1,
                    klass="drifted")
            if len(fd.hunks) != 1:
                raise ApplyError(
                    f"{rel}: new-file diff must carry exactly one hunk",
                    code=2, klass="malformed")
            _old, new = hunk_blocks(fd.hunks[0])
            if _old:
                raise ApplyError(
                    f"{rel}: new-file hunk carries context/deletion lines",
                    code=2, klass="malformed")
            fp = FilePlan(rel, "create", new_lines=new, bodies=fd.hunks)
        else:
            if not os.path.isfile(path):
                raise ApplyError(f"{rel}: target file does not exist",
                                 code=1, klass="missing-file")
            file_lines, had_nl = read_file_lines(path)
            old_starts = [s[0] for s in fd.hunk_starts]
            try:
                anchored = anchor_hunks(file_lines, fd.hunks, old_starts)
            except ApplyError as exc:
                raise ApplyError(f"{rel}: {exc}", code=exc.code,
                                 klass=exc.klass)
            fp = FilePlan(rel, "splice", anchored=anchored,
                          bodies=fd.hunks, starts=old_starts, had_nl=had_nl)
        plan = per_root.setdefault(root, RootPlan("splice"))
        plan.files.append(fp)
        plan.rels.append(rel)
    return per_root


def resolve_plan(proposal_path):
    """proposal -> (meta, {root: RootPlan}). Strict per root first —
    byte-identical behavior for proposals git can already apply; roots
    whose strict patch fails (or whose diff the strict parser refuses)
    fall back to the content-anchored splice plan."""
    meta, body = load_proposal(proposal_path)
    targets = gardener_postrun._as_list(meta.get("targets"))
    if not targets:
        raise ApplyError("proposal declares no targets", code=2,
                         klass="malformed")
    diff_text = extract_diff_text(body)
    strict_ok = {}
    try:
        patches, files = build_patches(split_file_diffs(diff_text), targets)
        for root, patch in patches.items():
            proc = git_apply(root, patch, check=True)
            strict_ok[root] = (proc.returncode == 0, patch, files[root])
    except ApplyError:
        strict_ok = {}  # strict parse refused — lenient owns everything
    if strict_ok and all(ok for ok, _p, _f in strict_ok.values()):
        return meta, {
            root: RootPlan("patch", patch=patch, rels=rels)
            for root, (_ok, patch, rels) in strict_ok.items()}
    failing = None
    if strict_ok:
        failing = {root for root, (ok, _p, _f) in strict_ok.items() if not ok}
    lenient = _lenient_root_plans(diff_text, targets, only_roots=failing)
    plan = {}
    for root, (ok, patch, rels) in strict_ok.items():
        if ok:
            plan[root] = RootPlan("patch", patch=patch, rels=rels)
    plan.update(lenient)
    return meta, plan


def classify_proposal(proposal_path, env_lenient=False):
    """Classify without mutating anything. Returns Classification; only an
    unreadable proposal file raises (OSError surfaces as ApplyError from
    load_proposal)."""
    try:
        meta, plan = resolve_plan(proposal_path)
    except ApplyError as exc:
        if exc.klass in ("drifted", "ambiguous", "missing-file",
                         "malformed", "out-of-scope"):
            return Classification(exc.klass, str(exc))
        if env_lenient:
            return Classification("skipped-env", str(exc))
        raise
    if not env_lenient:
        # CLI check keeps today's environment contract: versioned roots
        # are required. The birth gate (env_lenient=True) skips this —
        # the CONTENT verdict stands; a non-git root is a machine
        # condition, not a proposal defect (spec: "skip the strict probe
        # and classify by content").
        for root in plan:
            ensure_git_root(root)
    if all(rp.mode == "patch" for rp in plan.values()):
        return Classification("clean")
    detail = "; ".join(
        f"{fp.rel}: {len(fp.anchored) or 1} hunk(s) re-anchored"
        for rp in plan.values() if rp.mode == "splice" for fp in rp.files)
    return Classification("reanchorable", detail)


# ---- git layer ----------------------------------------------------------

def _git(root, *args, patch_input=None):
    return subprocess.run(["git", "-C", str(root)] + list(args),
                          capture_output=True, text=True, input=patch_input)


def ensure_git_root(root: str) -> None:
    proc = _git(root, "rev-parse", "--show-toplevel")
    if proc.returncode != 0:
        raise ApplyError(
            f"{root} is not a git repository — T11 requires versioned roots", code=1)
    top = os.path.realpath(proc.stdout.strip())
    if top != os.path.realpath(root):
        raise ApplyError(
            f"{root} resolves inside repo {top}, not its own repo — refusing", code=1)


def ensure_clean(root: str, rels, force: bool) -> None:
    proc = _git(root, "status", "--porcelain", "--", *rels)
    if proc.returncode != 0:
        raise ApplyError(
            f"git status failed in {root} (fail-closed): {proc.stderr.strip()}", code=1)
    if proc.stdout.strip() and not force:
        raise ApplyError(
            f"target files dirty in {root}:\n{proc.stdout}"
            "revert-safety needs a clean start (--force-dirty to override)", code=1)


def head_rev(root: str) -> str:
    return _git(root, "rev-parse", "--short", "HEAD").stdout.strip()


def git_apply(root: str, patch: str, check: bool = False, reverse: bool = False):
    args = ["apply", "--whitespace=nowarn"]
    if check:
        args.append("--check")
    if reverse:
        args.append("-R")
    args.append("-")
    return _git(root, *args, patch_input=patch)


# ---- commands -----------------------------------------------------------

def cmd_check(args) -> int:
    cls = classify_proposal(args.proposal)
    if cls.klass == "clean":
        _meta, plan = resolve_plan(args.proposal)
        print(f"gardener-apply: check OK (clean) — applies cleanly to "
              f"{len(plan)} root(s): {', '.join(sorted(plan))}")
        return 0
    if cls.klass == "reanchorable":
        print("gardener-apply: check OK (reanchorable) — strict git apply "
              "refuses the hand-written hunk headers, but every hunk's "
              "context is intact and unique; apply will re-anchor by "
              f"content. {cls.detail}")
        return 0
    code = 2 if cls.klass in ("malformed", "out-of-scope") else 1
    raise ApplyError(f"check BLOCKED ({cls.klass}): {cls.detail}",
                     code=code, klass=cls.klass)


class TargetCurrency:
    __slots__ = ("rel", "klass", "count", "key", "detail", "rederive")

    def __init__(self, rel, klass, count=None, key="", detail="", rederive=""):
        self.rel = rel
        self.klass = klass
        self.count = count
        self.key = key
        self.detail = detail
        self.rederive = rederive


class ProposalCurrency:
    __slots__ = ("pid", "verdict", "targets", "note")

    def __init__(self, pid, verdict, targets=None, note=""):
        self.pid = pid
        self.verdict = verdict
        self.targets = targets or []
        self.note = note


_CREATING_KINDS = ("new-asset", "build-brief")
_VERDICT_ORDER = ("STALE", "unknown", "fresh", "n/a")


def comparison_ref(root):
    try:
        ensure_git_root(root)
    except ApplyError as exc:
        return None, str(exc)
    sym = _git(root, "symbolic-ref", "refs/remotes/origin/HEAD")
    if sym.returncode == 0:
        ref = sym.stdout.strip()
        if ref and _git(root, "rev-parse", "--verify", "--quiet",
                        ref).returncode == 0:
            return ref, ""
    if _git(root, "rev-parse", "--verify", "--quiet",
            "refs/remotes/origin/main").returncode == 0:
        return "refs/remotes/origin/main", ""
    if _git(root, "remote").stdout.strip():
        return None, "remote exists but no remote default branch — never fetched"
    return "HEAD", ""


def _short_ref(ref):
    return ref[len("refs/remotes/"):] if ref.startswith("refs/remotes/") else ref


def _ref_at(root, ref, stamp):
    env = dict(os.environ, LC_ALL="C", LANG="C")
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify",
         "%s@{%s}" % (ref, stamp)],
        capture_output=True, text=True, env=env)
    if proc.returncode != 0 or proc.stderr.strip():
        return ""
    return proc.stdout.strip()


def _count_commits(root, rel, span_args):
    proc = _git(root, "rev-list", "--count", *span_args, "--", rel)
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip() or 0)
    except ValueError:
        return None


def last_fetch_epoch(root):
    stamps = []
    for flag in ("--git-dir", "--git-common-dir"):
        proc = _git(root, "rev-parse", "--path-format=absolute", flag)
        gitdir = proc.stdout.strip()
        if proc.returncode != 0 or not gitdir:
            continue
        try:
            stamps.append(os.path.getmtime(os.path.join(gitdir, "FETCH_HEAD")))
        except OSError:
            continue
    return max(stamps) if stamps else None


def _rederive_cmd(root, span_display, rel):
    return "git -C %s log -p %s -- %s" % (
        shlex.quote(root), " ".join(span_display), shlex.quote(rel))


def target_currency(root, ref, rel, base_rev, mtime_iso, mtime_epoch, kind):
    short = _short_ref(ref)
    if ref != "HEAD":
        fetched = last_fetch_epoch(root)
        if fetched is None:
            return TargetCurrency(
                rel, "unknown",
                detail="ref-stale: %s in %s was never fetched — fetch and re-run"
                       % (short, root))
        if int(fetched) < int(mtime_epoch):
            return TargetCurrency(
                rel, "unknown",
                detail="ref-stale: %s in %s last fetched %s, before drafting "
                       "%s — fetch and re-run"
                       % (short, root, _mtime_iso(fetched), mtime_iso))
    if _git(root, "cat-file", "-e", "%s:%s" % (ref, rel)).returncode != 0:
        if kind in _CREATING_KINDS:
            return TargetCurrency(rel, "n/a",
                                  detail="target not yet created (kind=%s) in %s" % (kind, root))
        return TargetCurrency(rel, "unknown",
                              detail="target absent from %s in %s" % (short, root))
    kind_proc = _git(root, "cat-file", "-t", base_rev) if base_rev else None
    is_commit = (kind_proc is not None and kind_proc.returncode == 0
                 and kind_proc.stdout.strip() == "commit")
    was = _ref_at(root, ref, mtime_iso)
    if is_commit:
        key = "key=base %s" % base_rev
        start = base_rev
    elif was:
        key = "key=ref-at %s" % mtime_iso
        start = was
    else:
        key = "key=since %s (date filter — blind to a backdated commit)" % mtime_iso
        span = ["--since", mtime_iso, ref]
        span_display = ["--since", shlex.quote(mtime_iso), short]
        start = None
    if start is not None:
        span = ["%s..%s" % (start, ref)]
        span_display = ["%s..%s" % (start, short)]
    n = _count_commits(root, rel, span)
    if n is None:
        return TargetCurrency(rel, "unknown", key=key,
                              detail="git rev-list failed on %s in %s" % (short, root))
    rederive = _rederive_cmd(root, span_display, rel) if n else ""
    return TargetCurrency(rel, "STALE" if n else "fresh", count=n, key=key,
                          detail="ref=%s in %s" % (short, root), rederive=rederive)


def _mtime_iso(epoch):
    stamp = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def proposal_currency(path):
    base = os.path.basename(path)
    pid = base[:-3] if base.endswith(".md") else base
    try:
        meta, _body = load_proposal(path)
    except ApplyError as exc:
        return ProposalCurrency(pid, "unknown", note=str(exc))
    pid = str(meta.get("id") or pid)
    kind = str(meta.get("kind") or "")
    base_rev = str(meta.get("base_rev") or "")
    mtime_epoch = os.path.getmtime(path)
    mtime_iso = _mtime_iso(mtime_epoch)
    rows = []
    for raw in gardener_postrun._as_list(meta.get("targets")):
        target = os.path.realpath(os.path.expanduser(raw))
        try:
            root = _root_of(target)
        except ApplyError as exc:
            rows.append(TargetCurrency(raw, "unknown", detail=str(exc)))
            continue
        ref, reason = comparison_ref(root)
        rel = os.path.relpath(target, root)
        if ref is None:
            rows.append(TargetCurrency(rel, "unknown", detail=reason))
            continue
        rows.append(target_currency(root, ref, rel, base_rev, mtime_iso,
                                    mtime_epoch, kind))
    if not rows:
        return ProposalCurrency(pid, "unknown", rows,
                                note="proposal declares no targets")
    classes = [row.klass for row in rows]
    for klass in _VERDICT_ORDER:
        if klass in classes:
            return ProposalCurrency(pid, klass, rows)
    return ProposalCurrency(pid, "unknown", rows)


def cmd_currency(args) -> int:
    roots = [os.path.realpath(str(r))
             for r in gardener_postrun.ALLOWED_TARGET_ROOTS]
    print("currency: fetch every root below before trusting this table")
    for root in roots:
        fetched = last_fetch_epoch(root)
        when = _mtime_iso(fetched) if fetched else "never"
        print("currency: root %s  (last fetched: %s)" % (root, when))
    paths = list(args.proposal or [])
    if not paths:
        pending = str(gardener_postrun.PENDING_DIR)
        try:
            names = os.listdir(pending)
        except OSError as exc:
            print("currency: cannot list %s: %s" % (pending, exc),
                  file=sys.stderr)
            return 2
        paths = sorted(os.path.join(pending, name) for name in names
                       if name.endswith(".md"))
    tally = Counter()
    for path in paths:
        result = proposal_currency(path)
        tally[result.verdict] += 1
        print("")
        note = "  (%s)" % result.note if result.note else ""
        print("%s  %s%s" % (result.pid, result.verdict, note))
        for row in result.targets:
            shown = "—" if row.count is None else str(row.count)
            bits = [bit for bit in (row.key, row.detail) if bit]
            print("  %s  %s  (%s)" % (row.rel, shown, ", ".join(bits)))
            if row.rederive:
                print("    re-derive: %s" % row.rederive)
    print("")
    print("currency: %d STALE, %d unknown, %d fresh, %d n/a of %d"
          % (tally["STALE"], tally["unknown"], tally["fresh"], tally["n/a"],
             len(paths)))
    return 0


def _write_text(path, content):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _snapshot(plan):
    """{abs_path: original_bytes_or_None} for every file the plan touches
    (None = file did not exist — a creation)."""
    snap = {}
    for root, rp in plan.items():
        for rel in rp.rels:
            path = os.path.join(root, rel)
            if os.path.isfile(path):
                with open(path, "rb") as fh:
                    snap[path] = fh.read()
            else:
                snap[path] = None
    return snap


def _restore(snapshot):
    """Best-effort byte-exact restore; returns paths whose restore failed."""
    failures = []
    for path, data in snapshot.items():
        try:
            if data is None:
                if os.path.exists(path):
                    os.remove(path)
            else:
                with open(path, "wb") as fh:
                    fh.write(data)
        except OSError:
            failures.append(path)
    return failures


def _parse_u0_ranges(diff_out):
    """`git diff --no-index -U0` output -> (ranges, removed, added):
    old-file 1-based (start, count) per hunk, plus the -/+ line contents.
    File headers appear only BEFORE the first @@ — skipping them
    structurally (not by `---` prefix) keeps a removed content line that
    itself starts with '--' (rendered '---x') correctly counted
    (plan-review M5)."""
    ranges, removed, added = [], [], []
    seen_hunk = False
    for ln in diff_out.splitlines():
        m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,(\d+))? @@", ln)
        if m:
            seen_hunk = True
            ranges.append((int(m.group(1)),
                           int(m.group(2) if m.group(2) is not None else "1")))
            continue
        if not seen_hunk or ln.startswith("\\"):
            continue
        if ln.startswith("-"):
            removed.append(ln[1:])
        elif ln.startswith("+"):
            added.append(ln[1:])
    return ranges, removed, added


def _act_verify(root, fp, original_bytes):
    """Gate on what was DONE: compare the pre-apply snapshot against the
    written file with a git-GENERATED -U0 diff (independent of the
    splicer's arithmetic) and assert (1) every changed old-file range lies
    inside an anchored old-block region, (2) the removed/added line
    sequences equal exactly the hunks' -/+ lines. Returns the full-context
    git diff text as the printed record."""
    path = os.path.join(root, fp.rel)
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix="gardener-preimage-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(original_bytes if original_bytes is not None else b"")
        proc = subprocess.run(
            ["git", "diff", "--no-index", "-U0", "--", tmp, path],
            capture_output=True, text=True)
        ranges, removed, added = _parse_u0_ranges(proc.stdout)
        want_removed, want_added = [], []
        for body in fp.bodies:
            r, a = hunk_change_lines(body)
            want_removed.extend(r)
            want_added.extend(a)
        # MULTISET, not exact sequence: git renders a MINIMAL -U0 diff, so a
        # blank-adjacent insertion (["", "NEW-PARA"] after context followed by
        # an existing blank) is emitted rotated (["NEW-PARA", ""]) — same set,
        # reordered. A skewed anchor still changes WHICH lines are -/+, so the
        # multiset differs and the corruption is still caught (independence
        # proof: test_act_verify_catches_corrupted_splice).
        if Counter(removed) != Counter(want_removed) \
                or Counter(added) != Counter(want_added):
            raise ApplyError(
                f"act-verify FAILED for {fp.rel}: the change actually "
                "written to disk does not equal the proposal's -/+ lines "
                f"(git saw {len(removed)}-/{len(added)}+, proposal has "
                f"{len(want_removed)}-/{len(want_added)}+)", code=1)
        if fp.action == "splice":
            regions = [(pos + 1, len(old)) for pos, old, _new in fp.anchored]
            for start, count in ranges:
                if count == 0:
                    # git attributes a count-0 insertion to old_line =
                    # insertion_point - 1 (a top-of-file prepend is
                    # `@@ -0,0 +1 @@`; insert-before-line-N is `@@ -(N-1),0 @@`),
                    # so a leading-edge insert — which the re-anchor path emits
                    # with trailing context only — lands one below the region
                    # start. The MINIMAL-diff rotation is symmetric at the
                    # trailing edge: when an inserted line equals the existing
                    # line just past the region, git pairs them and attributes
                    # the insertion one line PAST the region end (rs+rc_), not
                    # rs+rc_-1 (the blank-adjacent I1 shape). Accept the whole
                    # [rs-1, rs+rc_] span — both rotation directions.
                    inside = any(
                        rs - 1 <= start <= rs + rc_
                        for rs, rc_ in regions)
                else:
                    inside = any(
                        rs <= start and start + count <= rs + rc_
                        for rs, rc_ in regions)
                if not inside:
                    raise ApplyError(
                        f"act-verify FAILED for {fp.rel}: change at old "
                        f"lines {start},{count} falls outside every "
                        "anchored region", code=1)
        record = subprocess.run(
            ["git", "diff", "--no-index", "--", tmp, path],
            capture_output=True, text=True)
        return record.stdout
    finally:
        os.remove(tmp)


def _execute_plan(plan, snapshot):
    """Write everything; on any failure restore ALL snapshots. Returns the
    concatenated act-verify records for stdout."""
    records = []
    try:
        for root, rp in plan.items():
            if rp.mode in ("patch", "patch-reverse"):
                proc = git_apply(root, rp.patch,
                                 reverse=(rp.mode == "patch-reverse"))
                if proc.returncode != 0:
                    raise ApplyError(
                        f"apply failed in {root} after passing --check:\n"
                        f"{proc.stderr.strip()}", code=1)
                continue
            for fp in rp.files:
                path = os.path.join(root, fp.rel)
                if fp.action == "create":
                    parent = os.path.dirname(path)
                    if parent and not os.path.isdir(parent):
                        os.makedirs(parent)
                    content = join_file_lines(fp.new_lines, True)
                    _write_text(path, content)
                elif fp.action == "delete":
                    # reverse of a creation: delete iff byte-equal to what
                    # the creation wrote — never delete diverged content
                    if not os.path.isfile(path):
                        raise ApplyError(
                            f"revert: {fp.rel} does not exist", code=1,
                            klass="drifted")
                    with open(path, encoding="utf-8") as fh:
                        current = fh.read()
                    if current != join_file_lines(fp.new_lines, True):
                        raise ApplyError(
                            f"revert: {fp.rel} content diverged from the "
                            "created content — refusing to delete", code=1,
                            klass="drifted")
                    os.remove(path)
                    continue  # byte-equality precondition IS the verification
                else:
                    file_lines, had_nl = read_file_lines(path)
                    anchored = anchor_hunks(file_lines, fp.bodies,
                                            fp.starts or None)
                    content = join_file_lines(
                        splice_lines(file_lines, anchored), had_nl)
                    _write_text(path, content)
                    fp.anchored = anchored
                records.append(_act_verify(root, fp, snapshot[path]))
    except (ApplyError, OSError) as exc:
        failures = _restore(snapshot)
        message = f"apply failed, all touched files restored: {exc}"
        for path in failures:
            message += (f"\nROLLBACK OF {path} FAILED — tree left "
                        "modified, inspect git status")
        code = exc.code if isinstance(exc, ApplyError) else 1
        raise ApplyError(message, code=code,
                         klass=getattr(exc, "klass", None))
    return records


# ---- canon-write gate (spec Fix 4) --------------------------------------

class _SuiteRed(Exception):
    def __init__(self, root, proc):
        super().__init__(root)
        self.root = root
        self.proc = proc


def _root_is_test_bearing(root):
    return (os.path.isfile(os.path.join(root, "pyproject.toml"))
            and os.path.isdir(os.path.join(root, "tests")))


def _run_repo_suite(root):
    py = os.path.join(root, ".venv", "bin", "python")
    if not os.path.isfile(py):
        raise ApplyError(
            f"canon gate cannot run in {root}: no .venv — provision it "
            "(python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'); "
            "refusing to mutate a test-bearing repo unchecked", code=1,
            klass="canon-gate")
    try:
        return subprocess.run(
            [py, "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider"],
            cwd=root, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        raise ApplyError(
            f"canon gate: suite timed out in {root} — not a pass", code=1,
            klass="canon-gate")


_VALIDATOR_TIMEOUT_SEC = 60  # monkeypatchable in tests


def _asset_validator_findings(root, rels):
    """(warning_set, files_checked) from the sibling asset_validator for
    the touched files (missing files filtered — a creation has no
    pre-image). EVERY failure shape refuses — nonzero exit, empty stdout,
    unparseable JSON: the validator's own SIGALRM fail-soft os._exit(0)s
    with EMPTY output on timeout (right for a commit hook, fatal here) —
    a gate that cannot check must never pass (drift-guard: missing input
    fails loud, never passes empty)."""
    script = os.path.join(_SCRIPT_DIR, "asset_validator.py")
    if not os.path.isfile(script):
        raise ApplyError(
            f"canon gate cannot run in {root}: asset_validator.py not "
            "found next to gardener_apply.py — refusing an ungated write",
            code=1, klass="canon-gate")
    # REPO-RELATIVE paths, load-bearing: asset_validator.validate_one
    # dispatches on rel.startswith("rules/") / "skills/" / ... — an
    # ABSOLUTE path matches no asset class and yields zero warnings for
    # every file, making the whole arm silently vacuous (plan-review r2
    # CRITICAL; the drift-guard "green gate that validated nothing").
    files = [rel for rel in rels
             if os.path.isfile(os.path.join(root, rel))]
    if not files:
        return set(), 0
    proc = subprocess.run(
        [sys.executable, script, "--repo", str(root), "--json",
         "--max-seconds", str(_VALIDATOR_TIMEOUT_SEC), "--files"] + files,
        capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise ApplyError(
            f"canon gate: asset_validator produced no verdict in {root} "
            f"(exit {proc.returncode}, empty or missing output — timeout "
            "or crash; fail-closed):\n"
            f"{proc.stdout[-300:]}{proc.stderr[-300:]}", code=1,
            klass="canon-gate")
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise ApplyError(
            "canon gate: asset_validator produced no parseable verdict "
            f"in {root} (fail-closed):\n{proc.stdout[-300:]}"
            f"{proc.stderr[-300:]}", code=1, klass="canon-gate")
    return (set(map(str, data.get("warnings", []))),
            int(data.get("files_checked", len(files))))


def _canon_gate(plan, pre_validator):
    """Post-write, per written-into root — UNCONDITIONAL, never map-driven
    (a map entry can be silently unmapped; a root-level gate cannot).
    Returns {root: summary}; raises on refusal (caller restores)."""
    summaries = {}
    for root, rp in plan.items():
        if _root_is_test_bearing(root):
            proc = _run_repo_suite(root)
            if proc.returncode != 0:
                raise _SuiteRed(root, proc)
            tail = (proc.stdout.strip().splitlines() or ["passed"])[-1]
            summaries[root] = "pytest:" + tail.strip()
        else:
            post, checked = _asset_validator_findings(root, rp.rels)
            pre, _pre_checked = pre_validator.get(root, (set(), 0))
            new = post - pre
            if new:
                raise ApplyError(
                    f"canon gate: apply added {len(new)} asset-validator "
                    f"finding(s) in {root}:\n" + "\n".join(sorted(new)),
                    code=1, klass="canon-gate")
            # files_checked rides along: a pass that validated 0 of the
            # touched files must be distinguishable from a checked pass
            summaries[root] = f"asset-validator:no-new-findings:checked={checked}"
    return summaries


def cmd_apply(args) -> int:
    try:
        meta, plan = resolve_plan(args.proposal)
    except ApplyError as exc:
        if exc.klass:
            raise ApplyError(f"({exc.klass}) {exc}", code=exc.code,
                             klass=exc.klass)
        raise
    for root in plan:
        ensure_git_root(root)
    for root, rp in plan.items():
        ensure_clean(root, rp.rels, args.force_dirty)
    base_rev = str(meta.get("base_rev", ""))
    head_revs = {root: head_rev(root) for root in plan}
    if base_rev and not any(
            h and (h.startswith(base_rev) or base_rev.startswith(h))
            for h in head_revs.values()):
        print(f"WARNING: base_rev {base_rev} matches no current HEAD "
              f"({', '.join(f'{r}={s}' for r, s in sorted(head_revs.items()))}) — "
              "tree moved since drafting; the apply-time classification is "
              "the authoritative gate")
    try:
        # pre-write validator baseline: a failure here (missing script /
        # timeout / crash) is a canon-gate refusal too — nothing is written
        # yet, so no snapshot to restore, but the reason must still carry
        # the grep-able token (spec Fix 4: consumers grep one token).
        pre_validator = {root: _asset_validator_findings(root, rp.rels)
                         for root, rp in plan.items()
                         if not _root_is_test_bearing(root)}
    except ApplyError as exc:
        raise ApplyError(f"(canon-gate) {exc}", code=exc.code,
                         klass="canon-gate")
    snapshot = _snapshot(plan)
    records = _execute_plan(plan, snapshot)
    try:
        gate_summaries = _canon_gate(plan, pre_validator)
    except _SuiteRed as red:
        failures = _restore(snapshot)
        try:
            rerun = _run_repo_suite(red.root)
        except ApplyError as exc2:
            raise ApplyError(f"(canon-gate) {exc2}", code=1, klass="canon-gate")
        # combine stdout AND stderr: a python-level launch failure (pytest not
        # importable) exits nonzero with EMPTY stdout and its diagnostic on
        # stderr, so a stdout-only tail would be an empty, undiagnosable message
        # (CI run 29931892118).
        tail = "\n".join(
            ((red.proc.stdout or "") + (red.proc.stderr or ""))
            .strip().splitlines()[-15:])
        if rerun.returncode != 0:
            message = (
                f"canon gate: pre-existing failures in {red.root} "
                "— the repo was red BEFORE this apply; fix the repo first "
                f"(all touched files restored):\n{tail}")
        else:
            message = (
                f"canon gate: the apply broke the suite in {red.root} "
                f"(all touched files restored):\n{tail}")
        for path in failures:
            message += (f"\nROLLBACK OF {path} FAILED — tree left "
                        "modified, inspect git status")
        raise ApplyError(f"(canon-gate) {message}", code=1,
                         klass="canon-gate")
    except ApplyError as exc:
        failures = _restore(snapshot)
        message = f"{exc} (all touched files restored)"
        for path in failures:
            message += (f"\nROLLBACK OF {path} FAILED — tree left "
                        "modified, inspect git status")
        raise ApplyError(f"(canon-gate) {message}", code=1,
                         klass="canon-gate")
    except BaseException:
        # Ctrl-C / crash mid-suite: the written state must not survive an
        # abnormal exit with no gate verdict (spec Fix 4 "Abnormal exit
        # restores")
        _restore(snapshot)
        raise
    for rec in records:
        if rec.strip():
            print(rec)
    mode = ("clean" if all(rp.mode == "patch" for rp in plan.values())
            else "reanchored")
    gardener_postrun.ledger_append(
        "proposal_applied", proposal_id=str(meta.get("id")),
        base_rev=base_rev,
        head_revs=";".join(f"{r}={s}" for r, s in sorted(head_revs.items())),
        targets=",".join(gardener_postrun._as_list(meta.get("targets"))),
        apply_mode=mode,
        canon_gate="; ".join(sorted(gate_summaries.values())),
        lane=str(meta.get("lane") or "digest"))
    print(f"gardener-apply: applied {meta.get('id')} ({mode}) to "
          f"{len(plan)} root(s); commit the target repo(s), run the eval "
          "gate if mapped, then gardener_postrun.py decide --kind accept "
          "--applied-rev <root>=<sha>")
    return 0


def _reverse_body(body):
    """Swap the roles of -/+ lines so anchor/splice runs in reverse."""
    out = []
    for ln in body:
        if ln.startswith("-"):
            out.append("+" + ln[1:])
        elif ln.startswith("+"):
            out.append("-" + ln[1:])
        else:
            out.append(ln)
    return out


def _lenient_revert_root_plans(diff_text, declared_targets, only_roots=None):
    """Reverse-aware lenient resolution (plan-review C1): the tree holds
    the APPLIED content, so forward anchoring is meaningless here. Edits
    get their -/+ roles swapped and are anchored against the CURRENT
    (applied) content; forward creations become guarded deletes."""
    declared_abs = [os.path.realpath(os.path.expanduser(t))
                    for t in declared_targets]
    per_root = {}
    for fd in lenient_parse(diff_text):
        old_abs = _resolve_one(fd.old_raw, declared_abs)
        new_abs = _resolve_one(fd.new_raw, declared_abs)
        if old_abs is not None and new_abs is not None and old_abs != new_abs:
            raise ApplyError("rename diffs are not supported", code=2,
                             klass="malformed")
        path = new_abs if new_abs is not None else old_abs
        if path is None:
            raise ApplyError("file diff with /dev/null on both sides",
                             code=2, klass="malformed")
        root = _root_of(path)
        if only_roots is not None and root not in only_roots:
            continue
        rel = os.path.relpath(path, root)
        if new_abs is None:
            raise ApplyError(
                f"{rel}: file-deletion diffs are not supported in "
                "re-anchor mode", code=2, klass="malformed")
        if old_abs is None:  # forward creation -> reverse = guarded delete
            if len(fd.hunks) != 1:
                raise ApplyError(
                    f"{rel}: new-file diff must carry exactly one hunk",
                    code=2, klass="malformed")
            _old, new = hunk_blocks(fd.hunks[0])
            if _old:
                raise ApplyError(
                    f"{rel}: new-file hunk carries context/deletion lines",
                    code=2, klass="malformed")
            fp = FilePlan(rel, "delete", new_lines=new, bodies=fd.hunks)
        else:
            if not os.path.isfile(path):
                raise ApplyError(f"{rel}: target file does not exist",
                                 code=1, klass="missing-file")
            reversed_bodies = [_reverse_body(b) for b in fd.hunks]
            # reversed hunks anchor against the APPLIED (new-side) content, so
            # the declared start is the +N side number when present.
            new_starts = [s[1] for s in fd.hunk_starts]
            file_lines, had_nl = read_file_lines(path)
            try:
                anchored = anchor_hunks(file_lines, reversed_bodies, new_starts)
            except ApplyError as exc:
                raise ApplyError(f"{rel} (revert): {exc}", code=exc.code,
                                 klass=exc.klass)
            fp = FilePlan(rel, "splice", anchored=anchored,
                          bodies=reversed_bodies, starts=new_starts,
                          had_nl=had_nl)
        plan = per_root.setdefault(root, RootPlan("splice"))
        plan.files.append(fp)
        plan.rels.append(rel)
    return per_root


def resolve_revert_plan(proposal_path):
    """Reverse twin of resolve_plan: strict `git apply -R --check` per root
    first; failing roots fall back to reverse content anchoring. NEVER
    routes through the forward resolve_plan — post-apply, the forward
    old-blocks are gone by construction (plan-review C1)."""
    meta, body = load_proposal(proposal_path)
    targets = gardener_postrun._as_list(meta.get("targets"))
    if not targets:
        raise ApplyError("proposal declares no targets", code=2,
                         klass="malformed")
    diff_text = extract_diff_text(body)
    strict_ok = {}
    try:
        patches, files = build_patches(split_file_diffs(diff_text), targets)
        for root, patch in patches.items():
            proc = git_apply(root, patch, check=True, reverse=True)
            strict_ok[root] = (proc.returncode == 0, patch, files[root])
    except ApplyError:
        strict_ok = {}
    if strict_ok and all(ok for ok, _p, _f in strict_ok.values()):
        return meta, {
            root: RootPlan("patch-reverse", patch=patch, rels=rels)
            for root, (_ok, patch, rels) in strict_ok.items()}
    failing = None
    if strict_ok:
        failing = {root for root, (ok, _p, _f) in strict_ok.items() if not ok}
    lenient = _lenient_revert_root_plans(diff_text, targets,
                                         only_roots=failing)
    plan = {}
    for root, (ok, patch, rels) in strict_ok.items():
        if ok:
            plan[root] = RootPlan("patch-reverse", patch=patch, rels=rels)
    plan.update(lenient)
    return meta, plan


def cmd_revert(args) -> int:
    meta, plan = resolve_revert_plan(args.proposal)
    for root in plan:
        ensure_git_root(root)
    # dirty-check symmetric with apply (M2): a revert reverse-anchors against
    # possibly-moved content, so it needs a clean start just as much. The canon
    # gate is deliberately NOT run on revert — a revert removes a change and can
    # legitimately return a root to a PRE-EXISTING red state that the original
    # apply was gated against; re-gating would wrongly refuse the undo.
    for root, rp in plan.items():
        ensure_clean(root, rp.rels, args.force_dirty)
    snapshot = _snapshot(plan)
    records = _execute_plan(plan, snapshot)
    for rec in records:
        if rec.strip():
            print(rec)
    gardener_postrun.ledger_append(
        "proposal_reverted", proposal_id=str(meta.get("id")),
        lane=str(meta.get("lane") or "digest"))
    print(f"gardener-apply: reverted {meta.get('id')}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply/revert a gardener proposal's diff via git apply.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("check", "apply", "revert"):
        p = sub.add_parser(name)
        p.add_argument("--proposal", required=True)
        if name in ("apply", "revert"):
            p.add_argument("--force-dirty", action="store_true")
    p_cur = sub.add_parser("currency")
    p_cur.add_argument("--proposal", action="append")
    args = parser.parse_args(argv)
    handler = {"check": cmd_check, "apply": cmd_apply, "revert": cmd_revert,
               "currency": cmd_currency}[args.cmd]
    try:
        return handler(args)
    except ApplyError as exc:
        print(f"gardener-apply: {exc}", file=sys.stderr)
        if args.cmd == "apply":
            try:
                meta, _body = load_proposal(args.proposal)
                pid = str(meta.get("id"))
                lane = str(meta.get("lane") or "digest")
            except ApplyError:
                pid, lane = "unknown", "digest"
            gardener_postrun.ledger_append(
                "proposal_apply_failed", proposal_id=pid, reasons=str(exc),
                lane=lane)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
