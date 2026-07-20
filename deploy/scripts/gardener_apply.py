#!/usr/bin/env python3
"""Gardener proposal actuator (T11) — apply an accepted proposal's ## Diff
via `git apply` with a context check, replacing model-driven edits.

  gardener_apply.py check  --proposal <path>            read-only --check
  gardener_apply.py apply  --proposal <path> [--force-dirty]
  gardener_apply.py revert --proposal <path>            git apply -R

Diff-header path forms (same family the postrun validator accepts):
absolute, ~-prefixed, and a/<rel> b/<rel> (suffix-matched against declared
`targets:`). Headers are rewritten repo-relative per affected root;
`diff --git`/`index`/mode lines are dropped — a foreign blob-SHA makes a
patch internally inconsistent. new-asset proposals arrive as new-file diffs
(`--- /dev/null`); prose-format legacy proposals fail with a distinct
message and stay on the manual path.

Failure policy: `git apply --check` failure means the target drifted since
drafting — STOP and surface; never fuzzy-apply (no --3way), never fall back
to a model edit silently.

Ledger events (`proposal_applied` / `proposal_apply_failed` /
`proposal_reverted`) reference the proposal by `proposal_id` ONLY — never a
top-level `path` key: known_from_ledger() derives the postrun known-set from
ANY event carrying `path`, and an apply-time event would hide a
not-yet-validated pending proposal from validation.

Standalone, stdlib-only, py3.9-compatible.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
import gardener_postrun  # sibling deployed script: parser, ledger, roots


class ApplyError(Exception):
    """Refusal with a process exit code: 1 = blocked (context/dirty/state),
    2 = malformed proposal or usage error."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


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


# ---- path resolution / patch building ----------------------------------

def _resolve_one(raw: str, declared_abs):
    """Header path -> absolute realpath, or None for /dev/null."""
    if raw == "/dev/null":
        return None
    if raw.startswith(("/", "~")):
        return os.path.realpath(os.path.expanduser(raw))
    rel = re.sub(r"^[ab]/", "", raw)
    for t in declared_abs:
        if t == rel or t.endswith(os.sep + rel):
            return t
    raise ApplyError(f"diff path {raw!r} matches no declared target", code=2)


def _root_of(path: str) -> str:
    for root in gardener_postrun.ALLOWED_TARGET_ROOTS:
        root_r = os.path.realpath(str(root))
        if path == root_r or path.startswith(root_r + os.sep):
            return root_r
    raise ApplyError(f"path outside allowed roots (FR-8): {path}", code=2)


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

def _check_all(patches, reverse=False):
    for root, patch in patches.items():
        proc = git_apply(root, patch, check=True, reverse=reverse)
        if proc.returncode != 0:
            raise ApplyError(
                f"context check failed in {root}:\n{proc.stderr.strip()}\n"
                "target drifted since drafting — defer for re-draft, decline, "
                "or hand-apply with explicit human sign-off", code=1)


def cmd_check(args) -> int:
    _meta, patches, _files = resolve(args.proposal)
    for root in patches:
        ensure_git_root(root)
    _check_all(patches)
    print(f"gardener-apply: check OK — applies cleanly to "
          f"{len(patches)} root(s): {', '.join(sorted(patches))}")
    return 0


def cmd_apply(args) -> int:
    meta, patches, files = resolve(args.proposal)
    for root in patches:
        ensure_git_root(root)
    for root in patches:
        ensure_clean(root, files[root], args.force_dirty)
    base_rev = str(meta.get("base_rev", ""))
    head_revs = {root: head_rev(root) for root in patches}
    if base_rev and not any(
            h and (h.startswith(base_rev) or base_rev.startswith(h))
            for h in head_revs.values()):
        print(f"WARNING: base_rev {base_rev} matches no current HEAD "
              f"({', '.join(f'{r}={s}' for r, s in sorted(head_revs.items()))}) — "
              "tree moved since drafting; git apply --check is the authoritative gate")
    _check_all(patches)
    applied = []
    for root, patch in patches.items():
        proc = git_apply(root, patch)
        if proc.returncode != 0:
            rollback_failures = []
            for r in applied:
                rproc = git_apply(r, patches[r], reverse=True)
                if rproc.returncode != 0:
                    rollback_failures.append(r)
            message = (
                f"apply failed in {root} after passing --check (reverted "
                f"{len(applied)} earlier root(s)):\n{proc.stderr.strip()}")
            if rollback_failures:
                message += "\n" + "\n".join(
                    f"ROLLBACK OF {r} FAILED — tree left modified, inspect git status"
                    for r in rollback_failures)
            raise ApplyError(message, code=1)
        applied.append(root)
    gardener_postrun.ledger_append(
        "proposal_applied", proposal_id=str(meta.get("id")),
        base_rev=base_rev,
        head_revs=";".join(f"{r}={s}" for r, s in sorted(head_revs.items())),
        targets=",".join(gardener_postrun._as_list(meta.get("targets"))),
        lane=str(meta.get("lane") or "digest"))
    print(f"gardener-apply: applied {meta.get('id')} to {len(applied)} root(s); "
          "commit the target repo(s), run the eval gate if mapped, then "
          "gardener_postrun.py decide --kind accept --applied-rev <root>=<sha>")
    return 0


def cmd_revert(args) -> int:
    meta, patches, _files = resolve(args.proposal)
    for root in patches:
        ensure_git_root(root)
    _check_all(patches, reverse=True)
    for root, patch in patches.items():
        proc = git_apply(root, patch, reverse=True)
        if proc.returncode != 0:
            raise ApplyError(
                f"revert failed in {root}:\n{proc.stderr.strip()}", code=1)
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
        if name == "apply":
            p.add_argument("--force-dirty", action="store_true")
    args = parser.parse_args(argv)
    handler = {"check": cmd_check, "apply": cmd_apply, "revert": cmd_revert}[args.cmd]
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
