#!/usr/bin/env python3
"""LLM-free corpus-watch trigger gate — eval-direction Part C (C1).

Watches ~/.claude git history for direct edits to gate-mapped instruction
surfaces. No hook invokes gardener_eval_gate.py (the wiring hole), so a
worker editing rules/skills directly bypasses every behavioral check; this
hourly tick catches every writer — worker, manager, human, script — because
it reads the repo history, not the edit mechanism.

Decision order (first hit wins). module-off is SILENT (no dirs created, no
log line — gardener_gate.main's clean-off-switch precedent; a gardener=false
machine reads as stale in loops_status, expected, same as the whole gardener
family). Every other decision appends one exact-string line to
~/.claude/dockwright/corpus-watch/check.log:
  stopped               ~/.claude/dockwright/corpus-watch-stop exists.
  no-repo               ~/.claude has no readable git HEAD.
  init                  no state file — record HEAD, examine nothing yet.
  bad-sha               state's last_sha is unreadable or no longer resolves
                        to a commit (history rewrite, mangled state). LOUD:
                        stderr + check.log + re-init + a drift finding naming
                        the unexamined range.
  no-new                HEAD == last_sha.
  quiet                 newest commit younger than QUIET_SEC — let a burst of
                        edits settle before examining the range.
  locked                shared analyst-run mutex held by a live pid — skip,
                        NO state write; the hourly tick is the retry.
  cooldown              newest corpus-watch-lane run_start younger than
                        COOLDOWN_SEC — skip, NO state write.
  no-instruction-churn  changes, but none gate-mapped and none under the
                        instruction dirs: advance last_sha, drift counters
                        untouched (distinct label so check.log doesn't read
                        as drift).
  drift                 unmapped instruction churn: advance last_sha and
                        accumulate the drift counters; at >= 3 files or
                        >= 2048 accumulated bytes write a corpus-drift
                        finding and reset the counters.
  spawn                 gate-mapped targets changed: spawn
                        corpus-watch-run.sh detached with the examined sha,
                        range, MAPPED-targets CSV and the gardener dir; state
                        (last_sha) is NOT advanced here — run.sh owns the
                        advance to the EXAMINED sha. A MIXED range (mapped +
                        unmapped instruction files) additionally runs the
                        drift accounting before spawning — counters update
                        and the threshold finding fires exactly as on the
                        drift branch; the state write records drift_sha=head
                        (last_sha untouched) so a re-examined range (run.sh
                        died, lock skip, cooldown retry) never re-accumulates
                        the same churn: accounting is at-most-once per head.
  spawn-blocked         gate-mapped targets changed but RUN_SCRIPT is not on
                        disk: LOUD (stderr + check.log naming the missing
                        script and the unexamined mapped targets), no spawn,
                        state NOT advanced — never log a success-shaped
                        "spawn" for a script that doesn't exist.

Standalone, stdlib-only, py3.9-compatible. Spawns corpus-watch-run.sh (which
takes the shared analyst run-lock and owns the post-run state advance to the
EXAMINED sha).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
import gardener_eval_gate  # noqa: E402  sibling deployed script
import gardener_gate  # noqa: E402  sibling deployed script

HOME = Path(os.environ.get("HOME", ""))
CLAUDE_REPO = HOME / ".claude"
WATCH_DIR = HOME / ".claude" / "dockwright" / "corpus-watch"
STATE_PATH = WATCH_DIR / "state.json"
CHECK_LOG = WATCH_DIR / "check.log"
STOP_PATH = HOME / ".claude" / "dockwright" / "corpus-watch-stop"
FINDINGS_DIR = HOME / ".claude" / "dockwright" / "selffix" / "findings"
RUN_SCRIPT = HOME / ".claude" / "scripts" / "corpus-watch-run.sh"
QUIET_SEC = 30 * 60
COOLDOWN_SEC = 6 * 3600
DRIFT_FILES_THRESHOLD = 3
DRIFT_BYTES_THRESHOLD = 2048
INSTRUCTION_DIRS = ("rules/", "skills/", "commands/", "agents/")


# ---- git plumbing --------------------------------------------------------

def _git(*args):
    return subprocess.run(
        ["git", "-C", str(CLAUDE_REPO), "-c", "core.quotePath=false", *args],
        capture_output=True, text=True, timeout=60)


def head_sha():
    """HEAD sha, or None when ~/.claude is not a readable git repo."""
    try:
        proc = _git("rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def is_commit(rev) -> bool:
    try:
        proc = _git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def changed_rel_paths(last, head):
    """Deduped, order-preserving repo-relative paths touched in last..head;
    None when the log call errors (a bad-sha symptom, not an empty range)."""
    try:
        proc = _git("log", "--name-only", "--format=", f"{last}..{head}")
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [p for p in dict.fromkeys(
        line.strip() for line in proc.stdout.splitlines()) if p]


def newest_commit_age(now):
    try:
        proc = _git("log", "-1", "--format=%ct")
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return now - int(proc.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def size_at(sha, rel) -> int:
    """Blob size of rel at sha; 0 when the object is missing (added/deleted
    side of the delta) — so a deleted file counts its full size as churn."""
    try:
        proc = _git("cat-file", "-s", f"{sha}:{rel}")
    except (OSError, subprocess.SubprocessError):
        return 0
    if proc.returncode != 0:
        return 0
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return 0


# ---- state ---------------------------------------------------------------

def _read_state():
    """(state, err): state is the parsed dict (err None), else err is
    "missing" (no file — init) or "garbage" (unreadable/mangled — the
    bad-sha LOUD path; a mangled state file loses the watch anchor exactly
    like a rewritten history does)."""
    try:
        raw = STATE_PATH.read_text()
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "garbage"
    try:
        data = json.loads(raw)
        last = data.get("last_sha")
        if not isinstance(last, str) or not last:
            return None, "garbage"
        state = {"last_sha": last,
                 "drift_files": int(data.get("drift_files", 0)),
                 "drift_bytes": int(data.get("drift_bytes", 0))}
        # drift_sha: optional double-count guard — the head whose churn
        # accounting already ran while last_sha stayed put (spawn branch).
        drift_sha = data.get("drift_sha")
        if isinstance(drift_sha, str) and drift_sha:
            state["drift_sha"] = drift_sha
        return state, None
    except (ValueError, TypeError):
        return None, "garbage"


def _fresh_state(head):
    return {"last_sha": head, "drift_files": 0, "drift_bytes": 0}


def _write_state(state) -> None:
    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    tmp = WATCH_DIR / (STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True))
    os.replace(tmp, STATE_PATH)


# ---- findings ------------------------------------------------------------

def _write_finding(text) -> Path:
    FINDINGS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = FINDINGS_DIR / f"corpus-drift-{stamp}.md"
    n = 1
    while path.exists():
        path = FINDINGS_DIR / f"corpus-drift-{stamp}-{n}.md"
        n += 1
    path.write_text(text)
    return path


def _drift_finding_text(last, head, deltas, files_acc, bytes_acc) -> str:
    lines = [
        "# Corpus drift — unmapped instruction churn",
        "",
        f"- range: `{last}..{head}` (this tick's examined range)",
        f"- accumulated since last reset: {files_acc} instruction files, "
        f"{bytes_acc} bytes of churn",
        "",
        "Per-file byte deltas (this range):",
        "",
    ]
    for rel, delta in deltas:
        lines.append(f"- `{rel}`: {delta} bytes")
    lines += [
        "",
        f"0 of {files_acc} behaviorally covered — direct-edit drift; no eval "
        "instrument for these surfaces yet (docs/specs/eval-direction.md §D).",
        "",
    ]
    return "\n".join(lines)


def _bad_sha_finding_text(last, head) -> str:
    return (
        "# Corpus drift — watch anchor lost (bad last_sha)\n"
        "\n"
        f"- recorded last_sha: `{last}`\n"
        f"- current HEAD: `{head}`\n"
        f"- unexamined range: `{last}..{head}`\n"
        "\n"
        "The corpus-watch state anchor no longer resolves to a commit (history\n"
        "rewrite, or a mangled state file). The gate re-initialized to HEAD;\n"
        "everything between the recorded anchor and HEAD went behaviorally\n"
        "unexamined — review that range by hand "
        "(docs/specs/eval-direction.md §D).\n"
    )


# ---- decision ------------------------------------------------------------

def _bad_sha(head, last, detail, effects):
    rng = f"{last}..{head}"
    detail["unexamined"] = rng
    effects["state"] = _fresh_state(head)
    effects["finding"] = _bad_sha_finding_text(last, head)
    effects["stderr"] = (
        f"corpus-watch-gate: bad last_sha {last!r} — re-initialized to "
        f"{head}; range {rng} was NOT examined")
    return "bad-sha", detail, effects


def decide(now):
    """(decision, detail, effects). effects may carry "state" (dict to write
    atomically), "finding" (markdown body), "spawn" (argv), "stderr" (loud
    line) — main() applies them; --dry-run discards them."""
    detail = {}
    effects = {}
    if STOP_PATH.exists():
        return "stopped", detail, effects
    head = head_sha()
    if head is None:
        detail["repo"] = str(CLAUDE_REPO)
        return "no-repo", detail, effects
    detail["head"] = head
    state, err = _read_state()
    if err == "missing":
        effects["state"] = _fresh_state(head)
        return "init", detail, effects
    last = state["last_sha"] if state else "unknown"
    if err == "garbage" or not is_commit(last):
        return _bad_sha(head, last, detail, effects)
    if head == last:
        return "no-new", detail, effects
    age = newest_commit_age(now)
    if age is not None and age < QUIET_SEC:
        detail["newest_commit_age_sec"] = int(age)
        return "quiet", detail, effects
    rels = changed_rel_paths(last, head)
    if rels is None:
        return _bad_sha(head, last, detail, effects)
    if gardener_gate.lock_held_by_live_pid():
        return "locked", detail, effects
    stamps = gardener_gate._run_start_timestamps("corpus-watch")
    if stamps and now - max(stamps) < COOLDOWN_SEC:
        detail["last_run_age_sec"] = int(now - max(stamps))
        return "cooldown", detail, effects
    detail["files"] = len(rels)
    abs_targets = [str(CLAUDE_REPO / rel) for rel in rels]
    entries = gardener_eval_gate.load_map(None)
    # _coverage (not match_suites) is the per-target classifier; its
    # docstring guarantees it mirrors match_suites' predicate exactly.
    rows = gardener_eval_gate._coverage(abs_targets, entries)
    mapped = [t for t, suites in rows if suites]
    # UNMAPPED instruction files only: a mapped skill edit is the spawn's
    # business, not drift churn (on the pure-drift path nothing is mapped,
    # so this equals the old "every instruction file" set there).
    instruction = [rel for rel, (_t, suites) in zip(rels, rows)
                   if not suites and rel.startswith(INSTRUCTION_DIRS)]

    # Unmapped-instruction-churn accounting runs for EVERY classified range —
    # a MIXED range (mapped + unmapped instruction files) must not drop its
    # drift accounting just because the spawn branch returns first (Tier-2
    # F4: run.sh advances last_sha past the whole range, so the churn would
    # never be counted, surfaced, or revisited). Guarded by drift_sha:
    # accounting for a given head runs at most once, so a re-examined range
    # (run.sh died, lock skip) never re-accumulates the same churn.
    counters = {"drift_files": state["drift_files"],
                "drift_bytes": state["drift_bytes"]}
    accounted = False
    if instruction and state.get("drift_sha") != head:
        deltas = [(rel, abs(size_at(head, rel) - size_at(last, rel)))
                  for rel in instruction]
        files_acc = state["drift_files"] + len(instruction)
        bytes_acc = state["drift_bytes"] + sum(d for _rel, d in deltas)
        detail["instruction_files"] = len(instruction)
        detail["acc_files"] = files_acc
        detail["acc_bytes"] = bytes_acc
        if (files_acc >= DRIFT_FILES_THRESHOLD
                or bytes_acc >= DRIFT_BYTES_THRESHOLD):
            detail["finding"] = True
            effects["finding"] = _drift_finding_text(
                last, head, deltas, files_acc, bytes_acc)
            counters = {"drift_files": 0, "drift_bytes": 0}
        else:
            detail["finding"] = False
            counters = {"drift_files": files_acc, "drift_bytes": bytes_acc}
        accounted = True

    if mapped:
        detail["mapped"] = len(mapped)
        detail["range"] = f"{last}..{head}"
        if accounted:
            # The spawn branch does NOT advance last_sha (run.sh owns it);
            # persist the counter update now, with drift_sha=head recording
            # that this head's churn is already counted.
            effects["state"] = {"last_sha": last, **counters,
                                "drift_sha": head}
        if not RUN_SCRIPT.is_file():
            detail["run_script_missing"] = str(RUN_SCRIPT)
            effects["stderr"] = (
                f"corpus-watch-gate: RUN_SCRIPT missing at {RUN_SCRIPT} — "
                f"not spawning; mapped targets unexamined: "
                f"{','.join(mapped)}")
            return "spawn-blocked", detail, effects
        effects["spawn"] = [
            "bash", str(RUN_SCRIPT), head, f"{last}..{head}",
            ",".join(mapped), str(gardener_gate.LEDGER_PATH.parent)]
        return "spawn", detail, effects
    if not instruction:
        effects["state"] = {"last_sha": head, **counters}
        return "no-instruction-churn", detail, effects
    effects["state"] = {"last_sha": head, **counters}
    return "drift", detail, effects


# ---- effects + logging ---------------------------------------------------

def _log_check(decision, detail) -> None:
    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with CHECK_LOG.open("a") as f:
        f.write(f"{stamp}  {decision}  {json.dumps(detail, sort_keys=True)}\n")


def _spawn_run(argv) -> None:
    """Launch corpus-watch-run.sh fully detached; the gate never waits on it."""
    subprocess.Popen(argv,
                     stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,
                     start_new_session=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Corpus-watch trigger gate (LLM-free).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the decision; write nothing, spawn nothing.")
    args = parser.parse_args(argv)

    if not gardener_gate.gardener_module_enabled():
        # gardener=false: no-op the whole gate (design-gate). No dirs created,
        # no check.log line — a clean off switch (gardener_gate precedent).
        print("corpus-watch-gate: module-off ([modules] gardener=false) — no-op")
        return 0

    now = time.time()
    if args.dry_run:
        decision, detail, _effects = decide(now)
        print(f"corpus-watch-gate: {decision} "
              f"{json.dumps(detail, sort_keys=True)} (dry-run)")
        return 0

    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    decision, detail, effects = decide(now)
    if "stderr" in effects:
        print(effects["stderr"], file=sys.stderr)
    if "finding" in effects:
        _write_finding(effects["finding"])
    if "state" in effects:
        _write_state(effects["state"])
    if "spawn" in effects:
        _spawn_run(effects["spawn"])
    _log_check(decision, detail)
    print(f"corpus-watch-gate: {decision} {json.dumps(detail, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
