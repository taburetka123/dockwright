#!/usr/bin/env python3
"""Worktree prune — periodic, dry-run-by-default cleanup of dead worktrees.

Git worktrees under ~/worktrees and ~/worktrees-personal accumulate after a
branch merges. Removal and branch deletion are SEPARATE authorities with
separate proofs, because they lose different things:

  `git worktree remove` loses only what is not in git. Its gates:
    kept        — the operator's hold list (WT_DIR/keep.txt). Checked first, and
                  its absence stops the entire run: with no manual cleanup pass
                  this file is the only human control, so "holds unreadable"
                  must never resolve to "nothing is held".
    locked      — `git worktree lock` is git's own hold, and it already blocks
                  removal; skip such trees instead of failing on them daily.
    in progress — a paused rebase/bisect/cherry-pick. Invisible to `status
                  --porcelain`, so it needs its own marker check.
    contained   — detached HEADs only: the commits must be reachable from a tag
                  or a confirmed remote ref. NOT from a local branch, which this
                  loop can itself delete.
    terminal    — the work is finished: a PR that is MERGED or CLOSED (and none
                  OPEN), or HEAD already an ancestor of origin/main.
    clean       — `git status --porcelain` empty but for installer-injected
                  untracked entries.
    ignored ok  — every `.gitignore`d entry is build output. Without this the
                  loop silently deletes gitignored design docs and SDD state,
                  which `status --porcelain` cannot see at all.
    unowned     — no live session/process owns the directory.

  `git branch -D` loses commits, so it keeps the ORIGINAL, stricter proof:
    gh PR MERGED at exactly this head SHA, or HEAD an ancestor of origin/main —
    and never for main/master, nor for a detached worktree.

The single invariant is "only ever under-prune": every error, parse failure,
missing tool, unreadable signal, or ambiguous edge resolves to SKIP. Dry-run is
the default; `--apply` is required to mutate, every gate that can change between
scan and removal is re-verified immediately before it (TOCTOU guard), and at
most MAX_REMOVALS worktrees are removed per run.

Structural conventions (ledger, check.log, _pid_alive) mirror
deploy/scripts/bootlite_watchdog.py. Zero tokens — every check is git /
pid / file arithmetic. Kill switch: touch ~/.claude/dockwright/worktree-prune-stop.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import subprocess
import sys
import time
from collections import namedtuple
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple


def _env_positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _scan_toml_str(text: str, section: str, key: str):
    """Quoted `key = "value"` inside [section] — the tomllib-less fallback for
    the py3.9 /usr/bin/python3 this loop's launchd plist runs it under."""
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1].strip()
            continue
        if cur != section or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() != key:
            continue
        v = v.strip()
        if v[:1] in ("'", '"'):
            q = v[0]
            end = v.find(q, 1)
            return v[1:end] if end != -1 else v.strip(q)
        return v.split("#", 1)[0].strip() or None
    return None


def _config_paths_str(key: str):
    """[paths].<key> (a comma-separated string) from dockwright.toml, or None.
    tomllib when available; the scanner fallback for py3.9. Deployed script:
    must NOT import dockwright."""
    env = os.environ.get("DOCKWRIGHT_CONFIG", "").strip()
    if env:
        p = Path(env).expanduser()
        candidates = [p] if p.is_file() else []
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
        base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
        candidates = [base / "dockwright" / "dockwright.toml",
                      Path.home() / ".claude" / "dockwright.toml"]
    path = next((c for c in candidates if c.is_file()), None)
    if path is None:
        return None
    try:
        import tomllib
        with open(path, "rb") as fh:
            value = tomllib.load(fh).get("paths", {}).get(key)
    except ModuleNotFoundError:
        try:
            value = _scan_toml_str(path.read_text(), "paths", key)
        except OSError:
            return None
    except Exception:
        return None
    return value if isinstance(value, str) and value else None


def _resolve_paths(env_name: str, config_key: str, default: str) -> List[str]:
    """Comma-list resolution: env var > [paths].<config_key> > built-in default."""
    raw = os.environ.get(env_name)
    if raw is None:
        raw = _config_paths_str(config_key)
    if raw is None:
        raw = default
    return [os.path.expanduser(p.strip()) for p in raw.split(",") if p.strip()]


HOME = Path(os.environ.get("HOME", ""))


def _prefer_new(new: Path, legacy: Path) -> Path:
    # deprecated, one release: legacy fallback while orchestrator-era state migrates
    if new.exists():
        return new
    if legacy.exists():
        return legacy
    return new


ORCH_ACTIVE = _prefer_new(HOME / ".claude" / "dockwright", HOME / ".claude" / "orchestrator") / "active"
WT_DIR = _prefer_new(HOME / ".claude" / "dockwright" / "worktree-prune", HOME / ".claude" / "worktree-prune")
# deprecated, one release: operator stop-file honored at either home
STOP_PATHS = (HOME / ".claude" / "dockwright" / "worktree-prune-stop", HOME / ".claude" / "worktree-prune-stop")
LEDGER_PATH = WT_DIR / "ledger.jsonl"
CHECK_LOG_PATH = WT_DIR / "check.log"

# Roots: env override > dockwright.toml [paths].worktree_roots/repo_roots >
# built-in defaults (the same keys config.py exposes as worktree_roots/repo_roots).
ROOTS = _resolve_paths("WORKTREE_PRUNE_ROOTS", "worktree_roots",
                       "~/worktrees,~/worktrees-personal")
CLONE_PARENTS = _resolve_paths("WORKTREE_PRUNE_CLONE_PARENTS", "repo_roots",
                               "~/projects/work,~/projects/personal")
MAX_REMOVALS = _env_positive_int("WORKTREE_PRUNE_MAX_REMOVALS", 25)

# Untracked entries injected by the project-config installer; their presence
# does NOT make a worktree dirty for prune purposes.
INJECTED_UNTRACKED = {".claude", "CLAUDE.md", ".mcp.json"}

# The gh binary. Resolved explicitly rather than by PATH order: fronting a
# wrapper directory on the daemon's PATH would grant it the right to shadow
# every other binary this loop runs, and a "which gh wins" assertion is a
# substring test. argv[0] is the thing that matters, so name it.
GH_BIN = os.environ.get("WORKTREE_PRUNE_GH", "gh")

# Operator hold list. Its ABSENCE stops the whole run (see run_prune): with no
# manual cleanup pass this file is the only way a human can protect anything,
# so "the holds are unreadable" must never resolve to "nothing is held".
KEEPLIST_PATH = WT_DIR / "keep.txt"

# Ref namespaces accepted as proof that a detached HEAD's commits survive
# removal. Deny-by-default, and refs/heads/* is deliberately NOT here: this loop
# runs `branch -D`, so a local branch is a ref it can delete — today, tomorrow,
# or once someone registers a worktree on it. Tags and remote-tracking refs are
# the namespaces `_apply_arm` provably never writes.
PROOF_MAIN_REF = "refs/remotes/origin/main"
PROOF_TAG_NAMESPACE = "refs/tags/"
PROOF_REMOTE_NAMESPACE = "refs/remotes/"

# Markers of an interrupted git operation. A paused `rebase -i` leaves HEAD
# detached and `status --porcelain` EMPTY, so gate_clean cannot see it.
IN_PROGRESS_MARKERS = ("rebase-merge", "rebase-apply", "BISECT_START",
                       "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
                       "sequencer")

# Ignored paths that carry no information. Matched per path SEGMENT at any
# depth, because build output nests (common/target/, service/target/). Anything
# not listed blocks removal: `git status --porcelain` cannot see ignored content
# at all, and this workflow keeps design docs in gitignored dirs.
IGNORED_ARTIFACT_NAMES = frozenset({
    "target", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
    "build", "dist", ".gradle", "out", "coverage", ".angular", ".nx",
    ".playwright-mcp", ".ruff_cache", ".mypy_cache", ".terraform", "htmlcov",
    ".tox", ".claude", "CLAUDE.md", ".mcp.json", ".codex", ".DS_Store",
    ".flattened-pom.xml",
})

# Never `branch -D` these, whatever the gates say.
PROTECTED_BRANCHES = frozenset({"main", "master"})

# Feature token -> the function that implements it. `--capabilities` prints a
# token only when its function actually exists in this module, so deleting the
# feature deletes the token. A probe that greps the file for a string would
# still pass on a script whose code was removed but whose docstring was not.
CAPABILITY_IMPLS = (
    ("keeplist", "load_keeplist_text"),
    ("ignored-content-gate", "ignored_ok_from_porcelain"),
    ("containment-gate", "gate_contained"),
    ("in-progress-gate", "gate_in_progress"),
)


def capabilities() -> List[str]:
    return [token for token, impl in CAPABILITY_IMPLS
            if callable(globals().get(impl))]

# (returncode, stdout, stderr) — attribute access AND tuple unpacking both work.
RunResult = namedtuple("RunResult", ["returncode", "stdout", "stderr"])

# run(args, cwd) -> RunResult. cwd is the second positional (None = inherit).
RunFn = Callable[[List[str], Optional[str]], RunResult]


@dataclass(frozen=True)
class Candidate:
    path: str
    head: str
    branch: Optional[str]
    detached: bool
    clone: str
    locked: bool = False


# One decided candidate: (candidate, action, reason). reason is None for an
# eligible WOULD-REMOVE / REMOVED row.
ScanRow = Tuple[Candidate, str, Optional[str]]
# run_prune's return: (decision, {"results": [...], "summary": {...}}).
PruneResult = Tuple[str, dict]


def _expand(entry: str) -> str:
    """~ and $VAR expansion. Without it the most natural hold anyone writes,
    `~/worktrees/*`, matches no absolute candidate path and silently holds
    nothing."""
    return os.path.expandvars(os.path.expanduser(entry))


def _is_glob(entry: str) -> bool:
    return any(ch in entry for ch in "*?[")


def _glob_prefix(entry: str) -> str:
    """Longest leading directory of a glob that contains no metacharacter."""
    parts = entry.split("/")
    keep: List[str] = []
    for part in parts:
        if _is_glob(part):
            break
        keep.append(part)
    return "/".join(keep)


def load_keeplist(path: Path) -> Tuple[Optional[List[str]], Optional[str]]:
    """(entries, fatal_reason) for the hold file. Absent/unreadable -> the holds
    are unknown, which must never resolve to "nothing is held"."""
    try:
        raw = path.read_text()
    except OSError:
        return None, "keeplist_missing"
    return load_keeplist_text(raw)


def load_keeplist_text(raw: str) -> Tuple[Optional[List[str]], Optional[str]]:
    """(entries, fatal_reason). A literal entry pointing nowhere, or a glob whose
    fixed prefix does not exist, is a typo or a stale hold: the operator believes
    a tree is protected and it is not. That must stop the run, because a warning
    into check.log is not a signal anybody reads."""
    entries: List[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entry = _expand(line)
        if _is_glob(entry):
            # Matching nothing is the dangerous direction: the operator believes
            # a tree is held and it is not. An existing PREFIX is not enough —
            # `~/worktrees/TKT-9855-*` (one hyphen too many) has a real prefix
            # and holds nothing.
            import glob as _glob
            if not _glob.glob(entry):
                return None, "keeplist_entry_missing"
        elif not os.path.exists(entry):
            return None, "keeplist_entry_missing"
        entries.append(entry)
    return entries, None


def keeplist_matches(entries: List[str], cand_path: str) -> bool:
    """Case-insensitive: APFS is case-insensitive but realpath does not
    normalise case, so `tkt-9855` must still hold `TKT-9855`. Over-matching
    under-prunes, which is the safe direction."""
    raw = cand_path
    try:
        real = os.path.realpath(cand_path)
    except (ValueError, OSError):
        real = cand_path
    for entry in entries:
        for target in {raw.casefold(), real.casefold()}:
            pattern = entry.casefold()
            if _is_glob(entry):
                import fnmatch
                if fnmatch.fnmatch(target, pattern):
                    return True
            else:
                if target == pattern or _is_under_ci(target, pattern):
                    return True
    return False


def _is_under_ci(path: str, parent: str) -> bool:
    """_is_under, case-insensitively. APFS is case-insensitive but realpath does
    not normalise case, so a hold written `tkt-9855` must still cover
    `TKT-9855/repo` — the docstring and the installer's own template both
    promise that case-insensitivity composes with directory containment."""
    if not path or not parent:
        return False
    try:
        rp = os.path.realpath(path).casefold()
        rparent = os.path.realpath(parent).casefold()
        if rp == rparent:
            return True
        return os.path.commonpath([rp, rparent]) == rparent
    except (ValueError, OSError):
        return False


def _pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno == errno.EPERM


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _ledger_append(event: str, ts: Optional[float] = None, **fields) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {"event": event, "ts": ts if ts is not None else time.time()}
    record.update(fields)
    with LEDGER_PATH.open("a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def _log_check(decision: str, detail: dict, ts: Optional[float] = None) -> None:
    CHECK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    when = (datetime.fromtimestamp(ts, timezone.utc) if ts is not None
            else datetime.now(timezone.utc))
    stamp = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    with CHECK_LOG_PATH.open("a") as f:
        f.write(f"{stamp}  {decision}  {json.dumps(detail, sort_keys=True)}\n")


def _worktree_reflog_shas(run: RunFn, cand: Candidate) -> List[str]:
    """Distinct SHAs in this worktree's own HEAD reflog, oldest first.

    Read BEFORE removal: `git worktree remove` deletes
    .git/worktrees/<n>/logs/HEAD, and a commit the engineer `reset --hard`'d
    away is reachable from nothing else. Logging the SHAs does not prevent the
    loss; it makes it recoverable while the objects survive."""
    # ⛔ Do NOT derive .git/worktrees/<basename>. Git de-duplicates that id with
    # a numeric suffix, and this layout is <TICKET>/<repo>, so basenames collide
    # across tickets: 98 of 128 real candidates resolved to another worktree's
    # admin dir, producing a plausible, complete-looking SHA list belonging to a
    # different tree. Ask git which gitdir this worktree actually uses.
    try:
        res = run(["git", "-C", cand.path, "rev-parse", "--absolute-git-dir"], None)
    except Exception:
        return []
    if res is None or res.returncode != 0:
        return []
    gitdir = (res.stdout or "").strip()
    if not gitdir:
        return []
    log = os.path.join(gitdir, "logs", "HEAD")
    try:
        if not os.path.isfile(log):
            return []
        seen: List[str] = []
        with open(log) as fh:
            for line in fh:
                for sha in line.split()[:2]:
                    if (len(sha) == 40 and sha not in seen
                            and sha != "0" * 40
                            and set(sha) <= set("0123456789abcdef")):
                        seen.append(sha)
        return seen
    except OSError:
        return []


def _write_last_scan(results: List[dict], summary: dict, now: float) -> None:
    """Overwrite the per-run snapshot. Answers 'why was tree X passed over?' —
    which the ledger could not, because it recorded only removals. Overwritten
    rather than appended: the question is always about the last run."""
    try:
        path = WT_DIR / "last-scan.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": now, "summary": summary, "candidates": results}
        path.write_text(json.dumps(payload, indent=1, sort_keys=True))
    except OSError:
        pass


def _default_run(args: List[str], cwd: Optional[str] = None) -> RunResult:
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                              timeout=30)
        return RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")
    except Exception as e:  # FileNotFoundError, TimeoutExpired, ...
        return RunResult(1, "", str(e))


def _empty_summary() -> dict:
    return {"scanned": 0, "would_remove": 0, "removed": 0, "skipped": 0,
            "capped": 0, "gh_failed": 0, "by_reason": {}}


def _is_under(path: str, parent: str) -> bool:
    """True iff realpath(path) is parent or a descendant of realpath(parent).
    Symlinked roots match (realpath both sides); different drives / bad input
    resolve to False (not contained) so they're never treated as owned/in-root."""
    if not path or not parent:
        return False
    try:
        rp = os.path.realpath(path)
        rparent = os.path.realpath(parent)
        if rp == rparent:
            return True
        return os.path.commonpath([rp, rparent]) == rparent
    except (ValueError, OSError):
        return False


def _discover_clones(clone_parents: List[str]) -> List[str]:
    """Git repos directly under each clone-parent — a child dir containing .git."""
    clones: List[str] = []
    for parent in clone_parents:
        try:
            entries = sorted(os.scandir(parent), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir() and os.path.exists(os.path.join(entry.path, ".git")):
                    clones.append(entry.path)
            except OSError:
                continue
    return clones


def _parse_worktree_porcelain(text: str) -> List[dict]:
    records: List[dict] = []
    cur: dict = {}
    for line in text.splitlines():
        if not line.strip():
            if cur:
                records.append(cur)
                cur = {}
            continue
        if line.startswith("worktree "):
            if cur:
                records.append(cur)
            cur = {"path": line[len("worktree "):]}
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            ref = line[len("branch "):]
            cur["branch"] = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        elif line.strip() == "detached":
            cur["detached"] = True
        # Both spellings: `git worktree lock --reason "..."` emits `locked <why>`.
        elif line.strip() == "locked" or line.startswith("locked "):
            cur["locked"] = True
    if cur:
        records.append(cur)
    return records


def enumerate_candidates(run: RunFn, clone_parents: Optional[List[str]] = None,
                         roots: Optional[List[str]] = None) -> List[Candidate]:
    """Worktrees of every clone-parent repo whose path lives under a configured
    root. The main worktree (under the clone-parent, not a root) and any off-root
    linked worktree are excluded by the containment test."""
    if clone_parents is None:
        clone_parents = CLONE_PARENTS
    if roots is None:
        roots = ROOTS
    candidates: List[Candidate] = []
    for clone in _discover_clones(clone_parents):
        try:
            res = run(["git", "-C", clone, "worktree", "list", "--porcelain"], None)
        except Exception:
            continue
        if res is None or res.returncode != 0:
            continue
        for rec in _parse_worktree_porcelain(res.stdout or ""):
            path = rec.get("path")
            if not path:
                continue
            if not any(_is_under(path, root) for root in roots):
                continue
            candidates.append(Candidate(
                path=path,
                head=rec.get("head", ""),
                branch=rec.get("branch"),
                detached=bool(rec.get("detached")),
                clone=clone,
                locked=bool(rec.get("locked")),
            ))
    return candidates


# First-failing-reason order. Written down because `by_reason` counts are read
# as if they were totals: whichever reason is checked first is the only one a
# multi-failing candidate ever reports. `kept` leads so the hold count is true.
DECIDE_ORDER = (
    ("kept", "kept"),
    ("not_locked", "locked"),
    ("in_progress_clear", "in_progress"),
    ("contained", "uncontained"),   # may carry its own (uncontained / remote_unconfirmed)
    ("terminal", "not_terminal"),
    ("clean", "dirty"),
    ("ignored_ok", "ignored_content"),
    ("unowned", "owned"),
)


def decide(cand: Candidate, gates: dict) -> Tuple[str, Optional[str]]:
    """Map gate results to an action. WOULD-REMOVE only when every gate passes;
    otherwise SKIP with the FIRST failing reason in DECIDE_ORDER."""
    if gates.get("kept"):
        return "SKIP", "kept"
    for key, reason in DECIDE_ORDER[1:]:
        value = gates.get(key)
        if isinstance(value, tuple):
            # A gate that can fail for more than one distinguishable cause
            # reports its own reason; a bare bool uses the DECIDE_ORDER default.
            ok, why = value
            if not ok:
                return "SKIP", why or reason or "error"
            continue
        if not value:
            return "SKIP", reason
    return "WOULD-REMOVE", None


def _results_list(run: RunFn, scanned: List[ScanRow],
                  failed: Optional[dict] = None,
                  reflogs: Optional[dict] = None) -> List[dict]:
    """One row per candidate. `failed_gates` carries EVERY gate the candidate
    missed, not just `reason` — `decide` reports only the first, so a tree safe
    on four counts and a tree held by one accident look identical otherwise, and
    last-scan.json is the sole measurement behind the dry-run deploy step."""
    failed = failed or {}
    reflogs = reflogs or {}
    rows = []
    for cand, action, reason in scanned:
        # A REMOVED candidate's reflog MUST come from the pre-removal capture:
        # its directory is gone, so recomputing here yields [] for precisely the
        # trees whose SHAs the snapshot exists to preserve.
        reflog = (reflogs[cand.path] if cand.path in reflogs
                  else _worktree_reflog_shas(run, cand))
        rows.append({"path": cand.path, "action": action, "reason": reason,
                     "failed_gates": failed.get(cand.path, []),
                     "branch": cand.branch, "detached": cand.detached,
                     "clone": cand.clone, "head": cand.head,
                     "locked": cand.locked, "reflog": reflog})
    return rows


def _summarize(outcomes: List[ScanRow], removed: int = 0, capped: int = 0) -> dict:
    by_reason: dict = {}
    would_remove = 0
    skipped = 0
    for _cand, action, reason in outcomes:
        if action == "WOULD-REMOVE":
            would_remove += 1
        elif action == "REMOVED":
            continue
        else:  # SKIP / REMOVE-FAILED (capped rows are SKIP/"capped" too)
            skipped += 1
            if reason:
                by_reason[reason] = by_reason.get(reason, 0) + 1
    # NOTE: `capped` is a sub-slice of `skipped` (capped candidates are SKIP rows
    # with reason="capped"), and also appears in by_reason["capped"]. It is NOT a
    # separate bucket to subtract from skipped — skipped already includes it.
    return {"scanned": len(outcomes), "would_remove": would_remove,
            "removed": removed, "skipped": skipped, "capped": capped,
            "gh_failed": 0, "by_reason": by_reason}


def _scan(run: RunFn, clone_parents: List[str], roots: List[str],
          active_dir: Path, self_path: Optional[str], now: float,
          keeplist: Optional[List[str]] = None,
          stats: Optional[dict] = None,
          failed_gates: Optional[dict] = None) -> List[ScanRow]:
    """Enumerate, fetch-per-clone, and gate every candidate. Returns a list of
    (Candidate, action, reason). A clone whose `git fetch origin main` fails has
    ALL its candidates SKIP/fetch_failed without gate evaluation — stale
    origin/main would make the merged gate unreliable."""
    candidates = enumerate_candidates(run, clone_parents, roots)
    active_records = _load_active_records(active_dir)
    lsof_cwds = _collect_lsof_cwds(run)

    # Holds are evaluated BEFORE the fetch loop: they need no git, and a kept
    # tree in a clone whose fetch failed would otherwise report `fetch_failed`,
    # making by_reason["kept"] a lie about how many trees are actually held.
    held = {c.path for c in candidates if keeplist_matches(keeplist or [], c.path)}

    by_clone: dict = {}
    for cand in candidates:
        by_clone.setdefault(cand.clone, []).append(cand)

    scanned: List[ScanRow] = []
    for clone, cands in by_clone.items():
        kept_here = [c for c in cands if c.path in held]
        for cand in kept_here:
            _ledger_append("kept", ts=now, path=cand.path, clone=cand.clone)
            scanned.append((cand, "SKIP", "kept"))
        cands = [c for c in cands if c.path not in held]
        if not cands:
            continue
        try:
            fetch = run(["git", "-C", clone, "fetch", "origin", "main"], None)
            fetch_ok = fetch is not None and fetch.returncode == 0
        except Exception:
            fetch_ok = False
        if not fetch_ok:
            _ledger_append("fetch_failed", ts=now, clone=clone)
            for cand in cands:
                scanned.append((cand, "SKIP", "fetch_failed"))
            continue
        for cand in cands:
            try:
                text = _porcelain(run, cand)
                gates = {
                    "kept": False,
                    "not_locked": not cand.locked,
                    "in_progress_clear": gate_in_progress(run, cand),
                    "contained": (gate_contained(run, cand) if cand.detached
                                  else (True, None)),
                    "terminal": gate_terminal(run, cand, stats),
                    "clean": text is not None and clean_from_porcelain(text),
                    "ignored_ok": check_ignored(run, cand, text),
                    "unowned": gate_unowned(cand, active_records, lsof_cwds, self_path),
                }
                action, reason = decide(cand, gates)
                if failed_gates is not None and action != "WOULD-REMOVE":
                    # Enumerate, never classify by name: `decide` already treats
                    # ANY tuple-valued gate as carrying its own reason, and a
                    # non-empty tuple is truthy, so a name-based `not value` test
                    # records a FAILING tuple gate as a pass — leaving `reason`
                    # and `failed_gates` contradicting each other in the same row.
                    misses = []
                    for key, _r in DECIDE_ORDER:
                        value = gates.get(key)
                        if key == "kept":
                            if value:
                                misses.append("kept")
                            continue
                        ok = value[0] if isinstance(value, tuple) else bool(value)
                        if not ok:
                            misses.append(key)
                    failed_gates[cand.path] = misses
            except Exception:
                action, reason = "SKIP", "error"
            scanned.append((cand, action, reason))
    return scanned


def _finish_dry_run(run: RunFn, scanned: List[ScanRow], now: float,
                    stats: Optional[dict] = None,
                    failed_gates: Optional[dict] = None) -> PruneResult:
    for cand, action, _reason in scanned:
        if action == "WOULD-REMOVE":
            _ledger_append("would_remove", ts=now, path=cand.path, branch=cand.branch,
                           head=cand.head, clone=cand.clone)
    summary = _summarize(scanned)
    # Set BEFORE _log_check: patching it in afterwards left check.log — a durable
    # operator surface — reporting 0 in exactly the scenario the counter exists
    # to expose. A hardcoded 0 is worse than an empty log.
    summary["gh_failed"] = (stats or {}).get("gh_failed", 0)
    _log_check("dry-run", {"mode": "dry-run", **summary}, ts=now)
    return "dry-run", {"results": _results_list(run, scanned, failed_gates),
                       "summary": summary}


def _head_unchanged(run: RunFn, cand: Candidate) -> bool:
    """True iff the worktree HEAD still equals the EXACT SHA captured at scan
    (`cand.head`). Compared to that scanned SHA, NOT to ancestry of origin/main —
    a squash-merged branch's HEAD is never an ancestor yet must stay eligible, so
    the test is SHA-equality, not `--is-ancestor`. Any rev-parse failure -> False
    (treat as moved -> SKIP)."""
    try:
        res = run(["git", "-C", cand.path, "rev-parse", "HEAD"], None)
    except Exception:
        return False
    if res is None or res.returncode != 0:
        return False
    return (res.stdout or "").strip() == cand.head


def _apply_arm(run: RunFn, scanned: List[ScanRow], active_dir: Path,
               self_path: Optional[str], max_removals: int, now: float,
               stats: Optional[dict] = None,
               failed_gates: Optional[dict] = None) -> PruneResult:
    """Mutating arm. For each scan-eligible candidate (capped at max_removals)
    RE-VERIFY Gate B (clean), Gate C (unowned), AND head-stability against freshly
    collected signals — the scan-to-now window is where a worktree can become
    dirty, re-owned, or advance past the merged SHA (a commit landing on the
    branch stays clean+unowned but must NOT be pruned). Gate A (merged) is not
    re-checked: a merged branch cannot un-merge. Only on a successful `worktree
    remove` is `branch -D` attempted, and never for a detached HEAD."""
    eligible = [cand for (cand, action, _r) in scanned if action == "WOULD-REMOVE"]
    outcomes: List[ScanRow] = [(cand, action, reason)
                               for (cand, action, reason) in scanned
                               if action != "WOULD-REMOVE"]
    to_process = eligible[:max_removals]
    capped = eligible[max_removals:]
    removed = 0
    removed_reflogs: dict = {}

    for cand in to_process:
        fresh_active = _load_active_records(active_dir)
        fresh_lsof = _collect_lsof_cwds(run)
        # Re-read holds per candidate: the realistic operator action is exactly
        # the racing one — "the run is going, save this one".
        fresh_keep, keep_fatal = load_keeplist(KEEPLIST_PATH)
        try:
            text = _porcelain(run, cand)
            clean = text is not None and clean_from_porcelain(text)
            ignored_ok, ignored_why = check_ignored(run, cand, text)
            unowned = gate_unowned(cand, fresh_active, fresh_lsof, self_path)
            in_progress_clear = gate_in_progress(run, cand)
            contained_ok = (gate_contained(run, cand)[0] if cand.detached else True)
        except Exception:
            clean = ignored_ok = unowned = in_progress_clear = contained_ok = False
            ignored_why = "ignored_content"
        if keep_fatal is not None or keeplist_matches(fresh_keep or [], cand.path):
            _ledger_append("skip_toctou", ts=now, path=cand.path, reason="toctou_kept")
            outcomes.append((cand, "SKIP", "toctou_kept"))
            continue
        if not clean:
            _ledger_append("skip_toctou", ts=now, path=cand.path, reason="toctou_dirty")
            outcomes.append((cand, "SKIP", "toctou_dirty"))
            continue
        if not ignored_ok:
            why = "toctou_" + (ignored_why or "ignored_content")
            _ledger_append("skip_toctou", ts=now, path=cand.path, reason=why)
            outcomes.append((cand, "SKIP", why))
            continue
        if not in_progress_clear:
            _ledger_append("skip_toctou", ts=now, path=cand.path,
                           reason="toctou_in_progress")
            outcomes.append((cand, "SKIP", "toctou_in_progress"))
            continue
        if not contained_ok:
            _ledger_append("skip_toctou", ts=now, path=cand.path,
                           reason="toctou_uncontained")
            outcomes.append((cand, "SKIP", "toctou_uncontained"))
            continue
        if not unowned:
            _ledger_append("skip_toctou", ts=now, path=cand.path, reason="toctou_owned")
            outcomes.append((cand, "SKIP", "toctou_owned"))
            continue
        if not _head_unchanged(run, cand):
            _ledger_append("skip_toctou", ts=now, path=cand.path,
                           reason="toctou_head_moved")
            outcomes.append((cand, "SKIP", "toctou_head_moved"))
            continue
        reflog = _worktree_reflog_shas(run, cand)
        # Both of these must be computed BEFORE the removal: `gate_merged` shells
        # out with cwd = the worktree, and `_worktree_reflog_shas` reads that
        # worktree's gitdir. After `remove --force` both fail rc!=0 on the missing
        # directory — silently, in the safe direction, which is how a loop ends up
        # refusing every squash-merged branch and recording an empty reflog for
        # exactly the trees it destroyed.
        may_delete_branch = (bool(cand.branch) and not cand.detached
                             and cand.branch not in PROTECTED_BRANCHES
                             and gate_merged(run, cand))
        try:
            # ONE --force. git's own hint for a locked worktree is "use
            # 'remove -f -f' to override" — that single character is the guard.
            rm = run(["git", "-C", cand.clone, "worktree", "remove", "--force",
                      cand.path], None)
        except Exception:
            rm = None
        if rm is None or rm.returncode != 0:
            # The directory may be gone even on a non-zero exit, so the SHAs are
            # already in hand and must be recorded here too.
            _ledger_append("remove_failed", ts=now, path=cand.path, clone=cand.clone,
                           rc=(rm.returncode if rm is not None else None),
                           head=cand.head, reflog=reflog)
            outcomes.append((cand, "REMOVE-FAILED", "remove_failed"))
            continue
        removed += 1
        # The SHAs are the whole recovery story: after `worktree remove` the
        # objects survive unreachable for gc.pruneExpire (~2 weeks), so a logged
        # SHA turns an irreversible delete into a recoverable one. 223 removals
        # before this change recorded none.
        _ledger_append("removed", ts=now, path=cand.path, clone=cand.clone,
                       branch=cand.branch, head=cand.head, reflog=reflog)
        if cand.branch in PROTECTED_BRANCHES:
            _ledger_append("branch_delete_refused", ts=now, branch=cand.branch,
                           clone=cand.clone, reason="protected")
        elif cand.branch and not cand.detached:
            # Removal and deletion are separate authorities with separate
            # proofs. Removal only needs the work to be terminal, because the
            # branch ref survives it. Deleting that ref destroys commits, so it
            # keeps the ORIGINAL, stricter proof: a PR merged at exactly this
            # head SHA, or HEAD already an ancestor of origin/main. A CLOSED
            # (abandoned, never merged) PR is terminal but proves nothing.
            if not may_delete_branch:
                _ledger_append("branch_delete_refused", ts=now, branch=cand.branch,
                               clone=cand.clone, reason="unmerged")
            else:
                try:
                    bd = run(["git", "-C", cand.clone, "branch", "-D", cand.branch],
                             None)
                    bd_ok = bd is not None and bd.returncode == 0
                except Exception:
                    bd_ok = False
                _ledger_append("branch_deleted", ts=now, branch=cand.branch,
                               clone=cand.clone, ok=bd_ok)
        removed_reflogs[cand.path] = reflog
        outcomes.append((cand, "REMOVED", None))

    for cand in capped:
        outcomes.append((cand, "SKIP", "capped"))

    summary = _summarize(outcomes, removed=removed, capped=len(capped))
    summary["gh_failed"] = (stats or {}).get("gh_failed", 0)
    _log_check("apply", {"mode": "apply", **summary}, ts=now)
    return "applied", {"results": _results_list(run, outcomes, failed_gates,
                                                removed_reflogs),
                       "summary": summary}


def run_prune(now: float, apply: bool = False, run: Optional[RunFn] = None,
              clone_parents: Optional[List[str]] = None,
              roots: Optional[List[str]] = None,
              max_removals: Optional[int] = None,
              active_dir: Optional[Path] = None,
              self_path: Optional[str] = None) -> PruneResult:
    """Scan, gate, and (under --apply) prune. Stop-file is honored FIRST: when
    it exists nothing is scanned, no `run` call fires, and no ledger is written —
    even with apply=True."""
    if any(p.exists() for p in STOP_PATHS):
        _log_check("stopped", {}, ts=now)
        return "stopped", {"results": [], "summary": _empty_summary()}

    # The hold list is the only human control over an unattended delete loop.
    # Unknown holds stop the run: nothing is scanned, no `run` call fires.
    keeplist, fatal = load_keeplist(KEEPLIST_PATH)
    if fatal is not None:
        _log_check("stopped", {"reason": fatal}, ts=now)
        return "stopped", {"results": [], "summary": _empty_summary()}

    if run is None:
        run = _default_run
    if clone_parents is None:
        clone_parents = CLONE_PARENTS
    if roots is None:
        roots = ROOTS
    if max_removals is None:
        max_removals = MAX_REMOVALS
    if active_dir is None:
        active_dir = ORCH_ACTIVE
    if self_path is None:
        self_path = os.getcwd()

    stats: dict = {}
    failed_gates: dict = {}
    scanned = _scan(run, clone_parents, roots, active_dir, self_path, now,
                    keeplist, stats, failed_gates)
    if apply:
        result = _apply_arm(run, scanned, active_dir, self_path, max_removals, now,
                            stats, failed_gates)
    else:
        result = _finish_dry_run(run, scanned, now, stats, failed_gates)
    _write_last_scan(result[1].get("results", []), result[1].get("summary", {}), now)
    return result


def gate_merged(run: RunFn, cand: Candidate) -> bool:
    """Gate A. True iff the branch is merged into origin/main.

    PR path (only when the worktree is on a branch): the PR must be MERGED AND
    its headRefOid must equal the worktree HEAD — a mismatch means local commits
    landed after the merge, so the branch is NOT fully merged and must be kept.
    Any gh failure / parse error falls through to the ancestor check:
    merge-base --is-ancestor HEAD origin/main. Either path proving merged is
    enough; neither proving it (the safe default) keeps the worktree."""
    if cand.branch and not cand.detached:
        try:
            res = run([GH_BIN, "pr", "view", cand.branch, "--json",
                       "state,headRefOid"], cand.path)
            if res is not None and res.returncode == 0:
                data = json.loads(res.stdout or "")
                if (data.get("state") == "MERGED"
                        and data.get("headRefOid") == cand.head):
                    return True
        except Exception:
            pass  # fall through to the ancestor check
    try:
        anc = run(["git", "-C", cand.clone, "merge-base", "--is-ancestor",
                   cand.head, "origin/main"], None)
        return bool(anc is not None and anc.returncode == 0)
    except Exception:
        return False


def _first_ref(run: RunFn, cand: Candidate, *patterns: str) -> Optional[str]:
    """First ref matching any pattern that contains cand.head, or None. Raises
    nothing; a failure is indistinguishable from "no match" here, so callers
    that need to tell them apart use _contains_failed."""
    args = ["git", "-C", cand.clone, "for-each-ref", "--contains", cand.head,
            "--format=%(refname)"] + list(patterns)
    try:
        res = run(args, None)
    except Exception:
        return None
    if res is None or res.returncode != 0:
        return None
    for line in (res.stdout or "").splitlines():
        ref = line.strip()
        # */HEAD sorts before every branch name and is never a usable proof:
        # `ls-remote --heads origin HEAD` returns rc=0 with no rows.
        if ref and not ref.endswith("/HEAD"):
            return ref
    return None


def _remote_and_branch(ref: str) -> Optional[Tuple[str, str]]:
    """refs/remotes/<remote>/<branch> -> (remote, branch). Splits on the FIRST
    slash after the namespace: branch names commonly carry slashes."""
    if not ref.startswith(PROOF_REMOTE_NAMESPACE):
        return None
    rest = ref[len(PROOF_REMOTE_NAMESPACE):]
    remote, sep, branch = rest.partition("/")
    if not sep or not remote or not branch:
        return None
    return remote, branch


def _remote_confirms(run: RunFn, cand: Candidate, ref: str) -> bool:
    """True iff the server's SHA for the branch behind `ref` equals the local
    remote-tracking SHA. Name-existence is NOT enough: a force-push leaves the
    name in place over a history that no longer holds cand.head."""
    parts = _remote_and_branch(ref)
    if parts is None:
        return False
    remote, branch = parts
    try:
        local = run(["git", "-C", cand.clone, "rev-parse", ref], None)
        server = run(["git", "-C", cand.clone, "ls-remote", "--heads", remote,
                      branch], None)
    except Exception:
        return False
    if local is None or local.returncode != 0:
        return False
    if server is None or server.returncode != 0:
        return False
    local_sha = (local.stdout or "").strip()
    rows = [r for r in (server.stdout or "").splitlines() if r.strip()]
    if not local_sha or not rows:
        return False  # rc=0 with no rows = branch gone server-side
    server_sha = rows[0].split()[0].strip()
    return bool(server_sha) and server_sha == local_sha


def gate_contained(run: RunFn, cand: Candidate) -> Tuple[bool, Optional[str]]:
    """Gate E, detached worktrees only. Returns (proven, reason_if_not).

    A detached worktree's HEAD is its own GC root; removing it orphans every
    commit no other ref holds. Three ladder steps, in refname-sort-proof order:
    origin/main, then tags, then other remote-tracking refs with a live SHA
    confirmation. A single query would return refs/remotes/origin/HEAD first."""
    if _first_ref(run, cand, PROOF_MAIN_REF) is not None:
        return True, None
    if _first_ref(run, cand, PROOF_TAG_NAMESPACE) is not None:
        return True, None
    ref = _first_ref(run, cand, PROOF_REMOTE_NAMESPACE)
    if ref is None:
        return False, "uncontained"
    if not _remote_confirms(run, cand, ref):
        return False, "remote_unconfirmed"
    return True, None


def gate_terminal(run: RunFn, cand: Candidate,
                  stats: Optional[dict] = None) -> bool:
    """Gate A-remove. Is the work finished, so the CHECKOUT is disposable?

    Distinct from the branch-delete proof (gate_merged), which is unchanged.
    A branch worktree's commits survive removal via its own branch ref, so this
    gate is about intent, not data loss. Ancestry is a PEER signal, not a
    gh-failure fallback: "no PR, HEAD already on main" is removable today and
    must stay removable."""
    if cand.detached:
        return True
    prs = _gh_prs(run, cand, stats)
    if prs is not None:
        if any((pr or {}).get("state") == "OPEN" for pr in prs):
            return False
        if any((pr or {}).get("state") in ("MERGED", "CLOSED") for pr in prs):
            return True
    try:
        anc = run(["git", "-C", cand.clone, "merge-base", "--is-ancestor",
                   cand.head, "origin/main"], None)
        return bool(anc is not None and anc.returncode == 0)
    except Exception:
        return False


def _gh_prs(run: RunFn, cand: Candidate,
            stats: Optional[dict] = None) -> Optional[List[dict]]:
    """Every PR on this head branch, or None when gh could not answer. `pr list`
    (not `pr view`) so a head carrying several PRs cannot hide an OPEN one.

    A gh failure is NOT a skip reason — it falls through to the ancestor check —
    so without the counter it leaves no trace anywhere, and a broken gh is
    indistinguishable from a repo full of unmerged branches. That is exactly how
    the last one went unnoticed for 62 runs."""
    def _failed():
        if stats is not None:
            stats["gh_failed"] = stats.get("gh_failed", 0) + 1
        return None

    if not cand.branch:
        return None
    try:
        res = run([GH_BIN, "pr", "list", "--head", cand.branch, "--state", "all",
                   "--json", "number,state,headRefOid"], cand.path)
    except Exception:
        return _failed()
    if res is None or res.returncode != 0:
        return _failed()
    try:
        data = json.loads(res.stdout or "")
    except Exception:
        return _failed()
    return data if isinstance(data, list) else _failed()


def _is_ignorable_status_line(line: str) -> bool:
    """A porcelain line is ignorable iff it is UNTRACKED (`??`) and names an
    install-time injected entry. A tracked modification (` M CLAUDE.md`) is NOT
    ignorable — the status code, not a substring match, decides."""
    if line[:2] != "??":
        return False
    path = line[3:].rstrip("/")
    if not path:
        return False
    first_segment = path.split("/", 1)[0]
    return path in INJECTED_UNTRACKED or first_segment in INJECTED_UNTRACKED


def _porcelain(run: RunFn, cand: Candidate) -> Optional[str]:
    """`git status --porcelain --ignored` output, or None on any failure.

    ONE call serves both Gate B and Gate B2: `--ignored` only ADDS `!!` lines,
    it never changes the others, so both gates read the same text."""
    try:
        res = run(["git", "-C", cand.path, "status", "--porcelain", "--ignored"],
                  None)
    except Exception:
        return None
    if res is None or res.returncode != 0:
        return None
    return res.stdout or ""


def clean_from_porcelain(text: str) -> bool:
    """Gate B over already-collected porcelain text. `!!` (ignored) lines belong
    to Gate B2 and are not dirt here."""
    for line in text.splitlines():
        if not line.strip() or line[:2] == "!!":
            continue
        if _is_ignorable_status_line(line):
            continue
        return False
    return True


def _artifact_name(segment: str) -> bool:
    return segment in IGNORED_ARTIFACT_NAMES or segment.endswith(".egg-info")


def _is_artifact_path(entry: str) -> bool:
    """True iff an ignored entry is build output or installer-injected.

    Only the FIRST or LAST segment may carry the artifact name — never a middle
    one. `git status --ignored` collapses a wholly-ignored directory, so the
    real shapes are `common/target/` (name last) and `.claude/settings.json`
    (name first). Accepting ANY segment would launder everything around one
    artifact-named directory: `docs/target/notes.md`, `notes/out/plan.md` and
    `src/dist/README-IMPORTANT.md` would all read as build output, which is the
    exact blind spot this gate exists to close. Anything unrecognised is NOT an
    artifact — deny by default."""
    segments = [s for s in entry.rstrip("/").split("/") if s]
    if not segments:
        return False
    return _artifact_name(segments[0]) or _artifact_name(segments[-1])


def ignored_ok_from_porcelain(text: str) -> bool:
    """Gate B2 over already-collected porcelain text. True iff every ignored
    entry is a known artifact. `git status --porcelain` alone is blind to
    ignored content and `worktree remove --force` deletes it, so without this
    gate the loop silently destroys gitignored design docs and SDD state."""
    for line in text.splitlines():
        if line[:2] != "!!":
            continue
        entry = line[3:].strip()
        if entry and not _is_artifact_path(entry):
            return False
    return True


def gate_clean(run: RunFn, cand: Candidate) -> bool:
    """Gate B. Any subprocess error resolves to dirty (SKIP)."""
    text = _porcelain(run, cand)
    return False if text is None else clean_from_porcelain(text)


def _submodule_porcelain(run: RunFn, cand: Candidate) -> Optional[str]:
    """Ignored content inside populated submodules, or None on any failure.

    A submodule is a separate repository and `--ignored` does not descend into
    it, so the superproject's porcelain is empty for the one shape that matters:
    an ignored file inside a submodule. Every other submodule state (untracked,
    modified, unpushed) surfaces as ` M sub` and is already caught."""
    try:
        res = run(["git", "-C", cand.path, "submodule", "foreach", "--recursive",
                   "--quiet", "git status --porcelain --ignored"], None)
    except Exception:
        return None
    if res is None or res.returncode != 0:
        return None
    return res.stdout or ""


def check_ignored(run: RunFn, cand: Candidate,
                  text: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """Gate B2, the single choke point. Returns (ok, reason_if_not).

    Every caller routes through here — the scan, the pre-removal re-check, and
    gate_ignored — so a fourth source of ignored content is added in ONE place.
    Three separate copies of this composition is how a guard ends up present in
    the module and absent from the path that matters.

    The reason is tri-state on purpose: `ignored_content` means the sweep ran and
    found something, `submodule_sweep_failed` means it could not run. Both block,
    but reporting the second as the first tells the operator a tree is correctly
    protected when in truth the gate is broken — and the widening would go
    silently dead while last-scan.json looked healthy."""
    text = _porcelain(run, cand) if text is None else text
    if text is None:
        return False, "ignored_content"
    if not ignored_ok_from_porcelain(text):
        return False, "ignored_content"
    sub = _submodule_porcelain(run, cand)
    if sub is None:
        return False, "submodule_sweep_failed"
    if not ignored_ok_from_porcelain(sub):
        return False, "ignored_content"
    return True, None


def gate_ignored(run: RunFn, cand: Candidate) -> bool:
    """Gate B2 as a plain boolean, for callers that do not need the reason."""
    return check_ignored(run, cand)[0]


def gate_in_progress(run: RunFn, cand: Candidate) -> bool:
    """Gate D. True iff NO interrupted git operation is in flight.

    `--git-path` takes exactly ONE argument, so the flag is repeated per marker;
    `rev-parse --git-path a b c` is an error (rc=128), not a batch, and treating
    that rc as "in progress" would skip every candidate and silently no-op the
    whole loop. git failing at all -> False (unknown state is never terminal)."""
    args = ["git", "-C", cand.path, "rev-parse"]
    for marker in IN_PROGRESS_MARKERS:
        args += ["--git-path", marker]
    try:
        res = run(args, None)
    except Exception:
        return False
    if res is None or res.returncode != 0:
        return False
    paths = [p.strip() for p in (res.stdout or "").splitlines() if p.strip()]
    if len(paths) != len(IN_PROGRESS_MARKERS):
        return False
    for p in paths:
        resolved = p if os.path.isabs(p) else os.path.join(cand.path, p)
        if os.path.exists(resolved):
            return False
    return True


def _load_active_records(active_dir: Path) -> Optional[List[dict]]:
    """Orchestrator active session records. Returns None when the directory is
    missing or unreadable (the "no signal" sentinel that Gate C treats as
    ownership-unknown), else a list (possibly empty) of the dict records. A
    single corrupt record is skipped, not promoted to None — a readable dir
    with one bad file is still a real "these are the live sessions" signal."""
    try:
        if not active_dir.is_dir():
            return None
        # os.scandir raises PermissionError on an unreadable dir, which Path.glob
        # silently swallows (yielding []) on macOS — using it keeps the
        # unreadable->None distinction the safety contract depends on.
        paths = [entry.path for entry in os.scandir(active_dir)
                 if entry.name.endswith(".json")]
    except OSError:
        return None
    records: List[dict] = []
    for path in paths:
        rec = _read_json(Path(path))
        if isinstance(rec, dict):
            records.append(rec)
    return records


def _collect_lsof_cwds(run: RunFn) -> Optional[List[str]]:
    """Process cwds via `lsof -d cwd -F pn` (one field per line: `p<pid>` /
    `n<path>`). Returns None when lsof is unavailable / errored with no usable
    output (the "no signal" sentinel); else the list of `n` paths. lsof commonly
    exits non-zero on permission errors while still listing accessible cwds, so
    parsed paths are kept even on a non-zero exit — discarding them would risk
    over-pruning a worktree a live process still sits in."""
    try:
        res = run(["lsof", "-d", "cwd", "-F", "pn"], None)
    except Exception:
        return None
    if res is None:
        return None
    cwds = [line[1:] for line in (res.stdout or "").splitlines()
            if line.startswith("n")]
    if res.returncode != 0 and not cwds:
        return None
    return cwds


def gate_unowned(cand: Candidate, active_records: Optional[List[dict]],
                 lsof_cwds: Optional[List[str]], self_path: Optional[str],
                 pid_alive: Callable[[int], bool] = _pid_alive) -> bool:
    """Gate C. True iff NO live session/process owns the worktree.

    Availability contract: active_records is None when the active dir is
    unreadable; lsof_cwds is None when lsof is unavailable. If BOTH are None the
    ownership is unknown -> return False (owned -> SKIP). Otherwise the worktree
    is owned iff any available signal points inside it (an active-record cwd with
    a live pid, an lsof cwd, or the pruner's own cwd); else it is eligible."""
    # Self-guard first, regardless of signal availability.
    if self_path and _is_under(self_path, cand.path):
        return False
    if active_records is None and lsof_cwds is None:
        return False
    if active_records:
        for rec in active_records:
            if not isinstance(rec, dict):
                continue
            cwd = rec.get("cwd")
            pid = rec.get("pid")
            if not cwd or not (isinstance(pid, int) and pid_alive(pid)):
                continue
            if _is_under(cwd, cand.path):
                return False
    if lsof_cwds:
        for cwd in lsof_cwds:
            if _is_under(cwd, cand.path):
                return False
    return True


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Worktree prune: remove merged+clean+unowned worktrees (dry-run by default).")
    parser.add_argument("--apply", action="store_true",
                        help="Actually remove eligible worktrees (default: dry-run).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Force dry-run; OVERRIDES --apply if both are passed (safe direction).")
    parser.add_argument("--json", action="store_true",
                        help="Emit the decision + per-candidate results as JSON.")
    parser.add_argument("--capabilities", action="store_true",
                        help="List feature tokens (one per line) and exit; the "
                             "installer gates on these.")
    args = parser.parse_args(argv)
    if args.capabilities:
        # Printed from the same module that implements them, so deleting the
        # feature deletes the token. A grep over the file would not.
        for token in capabilities():
            print(token)
        return 0
    # --dry-run wins over --apply: the conflicting-flags resolution is the safe
    # (non-mutating) direction.
    apply = args.apply and not args.dry_run
    decision, info = run_prune(time.time(), apply=apply)
    results = info.get("results", [])
    summary = info.get("summary", {})

    if args.json:
        print(json.dumps({"decision": decision, "results": results,
                          "summary": summary}, sort_keys=True))
        return 0

    for r in results:
        print(f"{r['path']}  {r['action']}  {r.get('reason') or ''}".rstrip())
    print(
        f"worktree-prune: {decision} "
        f"scanned={summary.get('scanned', 0)} "
        f"would_remove={summary.get('would_remove', 0)} "
        f"removed={summary.get('removed', 0)} "
        f"skipped={summary.get('skipped', 0)} "
        f"capped={summary.get('capped', 0)} "
        f"gh_failed={summary.get('gh_failed', 0)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
