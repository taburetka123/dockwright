#!/usr/bin/env python3
"""Worktree prune — periodic, dry-run-by-default cleanup of merged worktrees.

Git worktrees under ~/worktrees and ~/worktrees-personal accumulate after a
branch merges. This loop removes one ONLY when all three gates pass:

  A. merged   — the worktree's branch is merged into origin/main (gh PR state
                MERGED with a matching head SHA, OR HEAD is an ancestor of
                origin/main). A headRefOid mismatch (post-merge local commits)
                fails the gate — we must not drop unmerged local work.
  B. clean    — `git status --porcelain` is empty except for the install-time
                injected untracked files (.claude/, CLAUDE.md, .mcp.json).
  C. unowned  — no live session/process owns the worktree (no orchestrator
                active-record cwd inside it with a live pid, no lsof cwd inside
                it, and the pruner is not running from inside it).

The single invariant is "only ever under-prune": every error, parse failure,
missing tool, unreadable signal, or ambiguous edge resolves to SKIP. Dry-run is
the default; `--apply` is required to mutate, and even under --apply every
candidate's B+C gates are re-verified immediately before removal (TOCTOU guard)
and at most MAX_REMOVALS worktrees are removed per run.

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


# One decided candidate: (candidate, action, reason). reason is None for an
# eligible WOULD-REMOVE / REMOVED row.
ScanRow = Tuple[Candidate, str, Optional[str]]
# run_prune's return: (decision, {"results": [...], "summary": {...}}).
PruneResult = Tuple[str, dict]


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


def _default_run(args: List[str], cwd: Optional[str] = None) -> RunResult:
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                              timeout=30)
        return RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")
    except Exception as e:  # FileNotFoundError, TimeoutExpired, ...
        return RunResult(1, "", str(e))


def _empty_summary() -> dict:
    return {"scanned": 0, "would_remove": 0, "removed": 0, "skipped": 0,
            "capped": 0, "by_reason": {}}


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
            ))
    return candidates


def decide(cand: Candidate, gates: dict) -> Tuple[str, Optional[str]]:
    """Map the three gate results to an action. WOULD-REMOVE only when all three
    pass; otherwise SKIP with the FIRST failing reason (merged > clean > owned)."""
    if not gates.get("merged"):
        return "SKIP", "unmerged"
    if not gates.get("clean"):
        return "SKIP", "dirty"
    if not gates.get("unowned"):
        return "SKIP", "owned"
    return "WOULD-REMOVE", None


def _results_list(scanned: List[ScanRow]) -> List[dict]:
    return [{"path": cand.path, "action": action, "reason": reason,
             "branch": cand.branch, "detached": cand.detached, "clone": cand.clone}
            for cand, action, reason in scanned]


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
            "by_reason": by_reason}


def _scan(run: RunFn, clone_parents: List[str], roots: List[str],
          active_dir: Path, self_path: Optional[str], now: float) -> List[ScanRow]:
    """Enumerate, fetch-per-clone, and gate every candidate. Returns a list of
    (Candidate, action, reason). A clone whose `git fetch origin main` fails has
    ALL its candidates SKIP/fetch_failed without gate evaluation — stale
    origin/main would make the merged gate unreliable."""
    candidates = enumerate_candidates(run, clone_parents, roots)
    active_records = _load_active_records(active_dir)
    lsof_cwds = _collect_lsof_cwds(run)

    by_clone: dict = {}
    for cand in candidates:
        by_clone.setdefault(cand.clone, []).append(cand)

    scanned: List[ScanRow] = []
    for clone, cands in by_clone.items():
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
                gates = {
                    "merged": gate_merged(run, cand),
                    "clean": gate_clean(run, cand),
                    "unowned": gate_unowned(cand, active_records, lsof_cwds, self_path),
                }
                action, reason = decide(cand, gates)
            except Exception:
                action, reason = "SKIP", "error"
            scanned.append((cand, action, reason))
    return scanned


def _finish_dry_run(scanned: List[ScanRow], now: float) -> PruneResult:
    for cand, action, _reason in scanned:
        if action == "WOULD-REMOVE":
            _ledger_append("would_remove", ts=now, path=cand.path, branch=cand.branch,
                           head=cand.head, clone=cand.clone)
    summary = _summarize(scanned)
    _log_check("dry-run", {"mode": "dry-run", **summary}, ts=now)
    return "dry-run", {"results": _results_list(scanned), "summary": summary}


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
               self_path: Optional[str], max_removals: int, now: float) -> PruneResult:
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

    for cand in to_process:
        fresh_active = _load_active_records(active_dir)
        fresh_lsof = _collect_lsof_cwds(run)
        try:
            clean = gate_clean(run, cand)
            unowned = gate_unowned(cand, fresh_active, fresh_lsof, self_path)
        except Exception:
            clean, unowned = False, False
        if not clean:
            _ledger_append("skip_toctou", ts=now, path=cand.path, reason="toctou_dirty")
            outcomes.append((cand, "SKIP", "toctou_dirty"))
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
        try:
            rm = run(["git", "-C", cand.clone, "worktree", "remove", "--force",
                      cand.path], None)
        except Exception:
            rm = None
        if rm is None or rm.returncode != 0:
            _ledger_append("remove_failed", ts=now, path=cand.path, clone=cand.clone,
                           rc=(rm.returncode if rm is not None else None))
            outcomes.append((cand, "REMOVE-FAILED", "remove_failed"))
            continue
        removed += 1
        _ledger_append("removed", ts=now, path=cand.path, clone=cand.clone,
                       branch=cand.branch)
        if cand.branch and not cand.detached:
            try:
                bd = run(["git", "-C", cand.clone, "branch", "-D", cand.branch], None)
                bd_ok = bd is not None and bd.returncode == 0
            except Exception:
                bd_ok = False
            _ledger_append("branch_deleted", ts=now, branch=cand.branch,
                           clone=cand.clone, ok=bd_ok)
        outcomes.append((cand, "REMOVED", None))

    for cand in capped:
        outcomes.append((cand, "SKIP", "capped"))

    summary = _summarize(outcomes, removed=removed, capped=len(capped))
    _log_check("apply", {"mode": "apply", **summary}, ts=now)
    return "applied", {"results": _results_list(outcomes), "summary": summary}


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

    scanned = _scan(run, clone_parents, roots, active_dir, self_path, now)
    if apply:
        return _apply_arm(run, scanned, active_dir, self_path, max_removals, now)
    return _finish_dry_run(scanned, now)


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
            res = run(["gh", "pr", "view", cand.branch, "--json", "state,headRefOid"],
                      cand.path)
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


def gate_clean(run: RunFn, cand: Candidate) -> bool:
    """Gate B. True iff `git status --porcelain` is empty save for injected
    untracked entries. Any subprocess error resolves to dirty (SKIP)."""
    try:
        res = run(["git", "-C", cand.path, "status", "--porcelain"], None)
    except Exception:
        return False
    if res is None or res.returncode != 0:
        return False
    for line in (res.stdout or "").splitlines():
        if not line.strip():
            continue
        if _is_ignorable_status_line(line):
            continue
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
    args = parser.parse_args(argv)
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
        f"capped={summary.get('capped', 0)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
