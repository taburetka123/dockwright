#!/usr/bin/env python3
"""Gardener Phase-1 artifact post-processor (PRD v2 §7.4–7.5, §12 Phase 1).

Three CLI modes:

  gardener_postrun.py postrun --run-id <id> [--known <file>]
      Called by gardener-run.sh after a digest run. Validates every artifact
      under proposals/pending/ and checks/ that the LEDGER does not already
      know (basenames from prior proposal / check_armed / proposal_rejected /
      decision events; an optional --known file supplements for tests).
      Ledger-derived knowledge means an artifact can never escape validation
      by appearing between runs — there is no snapshot window.
      Validation mechanically enforces the FR-8 scope guard on BOTH the
      declared `targets:` AND every path named in the ## Diff body, plus §7.4
      completeness (required fields incl. kind / always_on_bytes presence /
      check_window_days, and Evidence + Diff sections). Valid proposals stay
      pending and get a `proposal` ledger event; valid checks get
      `check_armed`; anything malformed, incomplete, or out-of-scope is
      QUARANTINED to proposals/rejected/ with a `proposal_rejected` event
      carrying the reasons.

  gardener_postrun.py decide --proposal <path> --kind accept|decline --reason <text>
      Called from the review sitting (dockwright-selffix-review's gardener phase)
      after the human decides a cluster. The proposal must still be under
      proposals/pending/ — deciding an already-moved file is refused, so a
      double-decide cannot write contradictory ledger events. Mechanically:
      moves the proposal to proposals/{accepted,declined}/ (collision-safe),
      appends a `decision` ledger event, and batch-marks every member finding
      reviewed. Members may be full finding basenames or unique prefixes
      (sid-prefix-8): exact match first, then a prefix glob — an ambiguous
      prefix is NOT marked and is reported, never guessed. Decline REQUIRES a
      reason: the recorded decline is what stops the digest from re-surfacing
      the cluster absent new members (PRD §7.5).
      NOTE: the applying auto-commit SHA is not captured here — the decision
      event records intent only. The `evaluate` mode below now records the
      outcome-check verdict; capturing the applying SHA itself remains the
      open Phase-2 item, if it still is.

  gardener_postrun.py evaluate [--verdicts <file>] [--dry-run] [--now <epoch>]
      Called from the review sitting (or a manager step) after an analyst has
      formed the kept/violated judgment on each matured armed check (PRD §7 step
      6 / §7.6). For every armed check whose window has matured (first
      check_armed ts + check_window_days), and that has no outcome yet, appends
      a `check_kept` / `check_violated` event keyed on check_id — copying the
      expectation VERBATIM from the first (immutable) check_armed event
      (append-only §7.6-1) and reading only the ledger + the analyst's verdicts
      file, never proposals/ (blind-to-generation-context §7.6-2). `violated`
      requires evidence (it feeds the next digest's revert/amend draft).
      Un-matured checks are skipped; already-recorded checks are idempotent
      no-ops; a bad verdicts file / structural anomaly exits 2. `--dry-run`
      prints the planned appends without writing. Recording the outcome does NOT
      draft the revert proposal — that is the next digest run's job.

Frontmatter format (deliberately a tiny YAML subset — stdlib-only parser, no
yaml dependency): `key: value` scalars and `key: [a, b, c]` inline lists,
inside a leading `---` ... `---` block; surrounding quotes on values are
stripped. The dockwright-gardener-digest skill emits exactly this shape.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path


_HOME_ENV = os.environ.get("HOME")
HOME = Path(_HOME_ENV) if _HOME_ENV else None
if HOME is None:
    # Module stays importable for tests; main() fails fast below.
    HOME = Path("/nonexistent-no-home")


def _prefer_new(new: Path, legacy: Path) -> Path:
    # deprecated, one release: legacy fallback while orchestrator-era state migrates
    if new.exists():
        return new
    if legacy.exists():
        return legacy
    return new


GARDENER_DIR = _prefer_new(HOME / ".claude" / "dockwright" / "gardener", HOME / ".claude" / "gardener")
PENDING_DIR = GARDENER_DIR / "proposals" / "pending"
ACCEPTED_DIR = GARDENER_DIR / "proposals" / "accepted"
DECLINED_DIR = GARDENER_DIR / "proposals" / "declined"
REJECTED_DIR = GARDENER_DIR / "proposals" / "rejected"
CHECKS_DIR = GARDENER_DIR / "checks"
LEDGER_PATH = GARDENER_DIR / "ledger.jsonl"
FINDINGS_DIR = _prefer_new(HOME / ".claude" / "dockwright" / "selffix" / "findings", HOME / ".claude" / "selffix-findings")

def _scan_toml_str(text: str, section: str, key: str):
    """Quoted `key = "value"` inside [section] — the tomllib-less fallback for
    the py3.9 /usr/bin/python3 gardener-run.sh invokes this postrun under."""
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


def _dockwright_repo() -> str:
    """[paths] dockwright_repo from dockwright.toml, ~-expanded ("" when unset).
    tomllib when available; the scanner fallback for py3.9. Deployed scripts
    must NOT import dockwright, so discovery is re-implemented."""
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
        return ""
    try:
        import tomllib
        with open(path, "rb") as fh:
            value = tomllib.load(fh).get("paths", {}).get("dockwright_repo")
    except ModuleNotFoundError:
        try:
            value = _scan_toml_str(path.read_text(), "paths", "dockwright_repo")
        except OSError:
            return ""
    except Exception:
        return ""
    return str(Path(value).expanduser()) if isinstance(value, str) and value else ""


# FR-8 scope guard: the Gardener's domain is the Claude meta-system only —
# ~/.claude plus the dockwright checkout ([paths] dockwright_repo, when set).
_DOCKWRIGHT_REPO = _dockwright_repo()
ALLOWED_TARGET_ROOTS = [HOME / ".claude"] + (
    [Path(_DOCKWRIGHT_REPO)] if _DOCKWRIGHT_REPO else [])

# always_on_bytes is presence-checked separately: 0 is a legitimate value.
# members is NOT unconditionally required: it is required (and UUID-shape
# enforced) only for evidence_kind=findings — ops/external evidence has no
# finding files to burn (arch review I3: the live `members: [ops-evidence]`
# sentinel slipped through non-emptiness-only validation).
PROPOSAL_REQUIRED_FIELDS = ("id", "run_id", "cluster", "targets", "lane",
                            "kind", "evidence_kind", "base_rev",
                            "expectation", "check_window_days", "revert")
LANES = ("digest", "frontier")
EVIDENCE_KINDS = ("findings", "ops", "external")
FINDING_MEMBER_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PROPOSAL_REQUIRED_PRESENT = ("always_on_bytes",)
PROPOSAL_REQUIRED_SECTIONS = ("## Evidence", "## Diff")
CHECK_REQUIRED_FIELDS = ("id", "run_id", "expectation", "check_window_days")


def ledger_append(event: str, **fields) -> None:
    """Append a typed ledger event. Envelope: `type` is the canonical key
    (matches the artifact-store events convention); `event` is emitted as a
    duplicate during the transition so pre-rename readers (incl. live
    notebook checks) keep working — drop it once those re-key. `v` versions
    the vocabulary (B2)."""
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {"type": event, "event": event, "v": 1, "ts": time.time(), **fields}
    with LEDGER_PATH.open("a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_frontmatter(text: str):
    """Parse the leading `---` frontmatter block. Returns (meta, body);
    meta is None when no well-formed block exists (caller quarantines)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    meta: dict = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            body = "\n".join(lines[i + 1:])
            return meta, body
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [_strip_quotes(item) for item in value[1:-1].split(",")
                         if _strip_quotes(item)]
        else:
            meta[key] = _strip_quotes(value)
    return None, text  # never saw the closing --- : malformed


def _target_in_scope(target: str) -> bool:
    resolved = Path(os.path.realpath(os.path.expanduser(target)))
    for root in ALLOWED_TARGET_ROOTS:
        root_resolved = Path(os.path.realpath(str(root)))
        if resolved == root_resolved or str(resolved).startswith(str(root_resolved) + os.sep):
            return True
    return False


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


_DIFF_HEADER_RE = re.compile(r"^(?:\+\+\+|---)\s+(\S+)", re.MULTILINE)


def diff_paths(body: str) -> list[str]:
    """Paths named in unified-diff headers inside the ## Diff section(s).
    /dev/null (new/deleted-file markers) is ignored."""
    paths = []
    for match in _DIFF_HEADER_RE.finditer(body):
        path = match.group(1)
        if path == "/dev/null":
            continue
        paths.append(path)
    return paths


def _diff_path_violations(body: str, declared_targets: list[str]) -> list[str]:
    """FR-8 for the diff body: absolute (or ~) diff paths must resolve inside
    the allowed roots; relative paths (the `a/...`/`b/...` unified-diff form)
    must suffix-match a declared target — a diff that patches something the
    frontmatter didn't declare is a scope-guard bypass, not a formatting
    nicety (verifier finding on #59: declare ~/.claude/x, patch ~/.ssh/config)."""
    violations = []
    resolved_targets = [str(Path(os.path.realpath(os.path.expanduser(t))))
                        for t in declared_targets]
    for raw in diff_paths(body):
        if raw.startswith(("/", "~")):
            if not _target_in_scope(raw):
                violations.append(f"diff patches path outside allowed roots (FR-8): {raw}")
            continue
        rel = re.sub(r"^[ab]/", "", raw)
        if not any(t == rel or t.endswith(os.sep + rel) for t in resolved_targets):
            violations.append(
                f"diff path does not match any declared target: {raw}")
    return violations


def validate_proposal(meta, body: str = "") -> list[str]:
    violations: list[str] = []
    if not isinstance(meta, dict):
        return ["no parseable frontmatter"]
    for field in PROPOSAL_REQUIRED_FIELDS:
        if not meta.get(field):
            violations.append(f"missing required field: {field}")
    for field in PROPOSAL_REQUIRED_PRESENT:
        if field not in meta:
            violations.append(f"missing required field: {field}")
    for section in PROPOSAL_REQUIRED_SECTIONS:
        if section not in body:
            violations.append(f"missing required section: {section}")
    lane = meta.get("lane")
    if lane and lane not in LANES:
        violations.append(f"lane must be one of {LANES}: {lane}")
    evidence_kind = meta.get("evidence_kind")
    if evidence_kind and evidence_kind not in EVIDENCE_KINDS:
        violations.append(f"evidence_kind must be one of {EVIDENCE_KINDS}: {evidence_kind}")
    if evidence_kind == "findings" or not evidence_kind:
        members = _as_list(meta.get("members"))
        if not members:
            violations.append("missing required field: members (evidence_kind=findings)")
        for member in members:
            if not FINDING_MEMBER_RE.match(member):
                violations.append(
                    f"member is not a full finding UUID basename: {member}")
    targets = _as_list(meta.get("targets"))
    for target in targets:
        if not _target_in_scope(target):
            violations.append(f"target outside allowed roots (FR-8): {target}")
    violations.extend(_diff_path_violations(body, targets))
    return violations


def validate_check(meta) -> list[str]:
    if not isinstance(meta, dict):
        return ["no parseable frontmatter"]
    return [f"missing required field: {field}"
            for field in CHECK_REQUIRED_FIELDS if not meta.get(field)]


def _unique_dest(dest_dir: Path, name: str) -> Path:
    """Collision-safe destination: never silently overwrite an artifact that
    already landed in accepted/declined/rejected under the same basename."""
    dest = dest_dir / name
    if not dest.exists():
        return dest
    stem, suffix = os.path.splitext(name)
    n = 2
    while (dest_dir / f"{stem}-{n}{suffix}").exists():
        n += 1
    return dest_dir / f"{stem}-{n}{suffix}"


def _quarantine(path: Path, reasons: list[str], run_id: str, lane: str = "digest") -> None:
    REJECTED_DIR.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(REJECTED_DIR, path.name)
    shutil.move(str(path), str(dest))
    ledger_append("proposal_rejected", run_id=run_id, path=str(dest),
                  reasons="; ".join(reasons), lane=lane)


def known_from_ledger() -> set[str]:
    """Basenames of every artifact the ledger has already processed (any
    event carrying a path). Authoritative known-set: an artifact written
    between runs is still unknown and gets validated on the next postrun."""
    known: set[str] = set()
    if not LEDGER_PATH.is_file():
        return known
    try:
        lines = LEDGER_PATH.read_text().splitlines()
    except OSError:
        return known
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and isinstance(event.get("path"), str):
            known.add(os.path.basename(event["path"]))
    return known


def process_run_artifacts(run_id: str, known: set[str], lane: str = "") -> dict:
    """Validate every artifact the ledger doesn't know yet. `known` is the
    ledger-derived set (plus any --known supplement)."""
    summary = {"proposals": 0, "checks": 0, "rejected": 0}
    for d in (PENDING_DIR, CHECKS_DIR, REJECTED_DIR):
        d.mkdir(parents=True, exist_ok=True)
    for path in sorted(PENDING_DIR.glob("*.md")):
        if path.name in known:
            continue
        meta, body = parse_frontmatter(path.read_text())
        violations = validate_proposal(meta, body)
        if violations:
            _quarantine(path, violations, run_id,
                        lane=str((meta or {}).get("lane") or lane or "digest"))
            summary["rejected"] += 1
            continue
        members = _as_list(meta.get("members"))
        targets = _as_list(meta.get("targets"))
        ledger_append("proposal", run_id=run_id, proposal_id=str(meta.get("id")),
                      path=str(path), cluster=str(meta.get("cluster", "")),
                      members=",".join(members), targets=",".join(targets),
                      lane=str(meta.get("lane") or lane or "digest"),
                      evidence_kind=str(meta.get("evidence_kind") or "findings"),
                      **{"class": str(meta.get("kind", ""))})
        summary["proposals"] += 1
    for path in sorted(CHECKS_DIR.glob("*.md")):
        if path.name in known:
            continue
        meta, _body = parse_frontmatter(path.read_text())
        violations = validate_check(meta)
        if violations:
            _quarantine(path, violations, run_id,
                        lane=str((meta or {}).get("lane") or lane or "digest"))
            summary["rejected"] += 1
            continue
        ledger_append("check_armed", run_id=run_id, check_id=str(meta.get("id")),
                      path=str(path), cluster=str(meta.get("cluster", "")),
                      expectation=str(meta.get("expectation")),
                      check_window_days=str(meta.get("check_window_days")),
                      lane=str(meta.get("lane") or lane or "digest"))
        summary["checks"] += 1
    return summary


def _resolve_member(sid: str):
    """Finding file for a member id: exact basename first, then unique-prefix
    glob (the skill historically emitted sid-prefix-8). Returns
    (path|None, "exact"|"prefix"|"missing"|"ambiguous")."""
    exact = FINDINGS_DIR / f"{sid}.md"
    if exact.is_file():
        return exact, "exact"
    hits = sorted(FINDINGS_DIR.glob(f"{sid}*.md"))
    if len(hits) == 1:
        return hits[0], "prefix"
    if len(hits) > 1:
        return None, "ambiguous"
    return None, "missing"


def decide(proposal_path: str, kind: str, reason: str) -> int:
    """Review-sitting bookkeeping for one human decision. Returns exit code."""
    if kind not in ("accept", "decline"):
        print(f"gardener-decide: unknown kind {kind!r} (accept|decline)", file=sys.stderr)
        return 2
    if kind == "decline" and not reason.strip():
        print("gardener-decide: decline requires --reason — the recorded reason is "
              "what stops the cluster from re-surfacing (PRD §7.5)", file=sys.stderr)
        return 2
    path = Path(proposal_path)
    if not path.is_file():
        print(f"gardener-decide: no such proposal: {path}", file=sys.stderr)
        return 2
    resolved = Path(os.path.realpath(str(path)))
    pending_resolved = Path(os.path.realpath(str(PENDING_DIR)))
    if pending_resolved not in resolved.parents:
        print(f"gardener-decide: {path} is not under proposals/pending/ — "
              "already-decided proposals are final; a second decide would write "
              "contradictory ledger events", file=sys.stderr)
        return 2
    meta, _body = parse_frontmatter(path.read_text())
    if not isinstance(meta, dict):
        print(f"gardener-decide: unparseable frontmatter in {path}", file=sys.stderr)
        return 2
    members = _as_list(meta.get("members"))
    evidence_kind = str(meta.get("evidence_kind") or "findings")
    dest_dir = ACCEPTED_DIR if kind == "accept" else DECLINED_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(dest_dir, path.name)
    shutil.move(str(path), str(dest))
    marked, ambiguous, missing = [], [], []
    if evidence_kind == "findings":
        for sid in members:
            finding, how = _resolve_member(sid)
            if how == "ambiguous":
                ambiguous.append(sid)
                continue
            if finding is None:
                missing.append(sid)
                continue
            marker = finding.with_suffix(".reviewed")
            if not marker.exists():
                marker.touch()
            marked.append(sid)
    # ops/external evidence has no finding files: skipping the burn is BY
    # DECLARATION, not a missing-file accident (arch review I3).
    ledger_append("decision", kind=kind, proposal_id=str(meta.get("id")),
                  path=str(dest), cluster=str(meta.get("cluster", "")),
                  members=",".join(members), reason=reason,
                  members_marked=",".join(marked),
                  members_ambiguous=",".join(ambiguous),
                  lane=str(meta.get("lane") or "digest"),
                  evidence_kind=evidence_kind,
                  **{"class": str(meta.get("kind", ""))})
    print(f"gardener-decide: {kind} {meta.get('id')} → {dest}; "
          f"marked reviewed: {len(marked)}/{len(members)} members"
          + (f"; AMBIGUOUS prefixes left unmarked: {','.join(ambiguous)}" if ambiguous else "")
          + (f"; no finding file for: {','.join(missing)}" if missing else ""))
    return 0


# ---- evaluate mode: outcome-check recording sink (PRD §7 step 6 / §7.6) ----
#
# The keep/revert loop's recording half. `decide` records the human's
# accept/decline; `evaluate` records the analyst's kept/violated verdict on a
# *matured* armed check. Symmetric to decide by construction: the tool does not
# form the verdict (that is a §7.6 judgment against post-acceptance data — a
# self-grepping tool would verify a fantasy and would break the "blind to
# generation context" constraint); it records the supplied verdict under
# maturity + idempotency + append-only guards, writing ONLY the ledger.

OUTCOME_EVENTS = ("check_kept", "check_violated")
VERDICT_EVENT = {"kept": "check_kept", "violated": "check_violated"}
# Dispositions that must fail the process loudly (bad input / structural anomaly
# / append-only breach) even when other checks record cleanly.
ANOMALY_KINDS = frozenset({"REFUSED", "UNKNOWN-CHECK", "WINDOW-PARSE-ERROR", "DUPLICATE-ARM"})


def _event_type(rec: dict) -> str:
    """Canonical event type, tolerating the pre-rename envelope (ledger_append
    emits both `type` and `event`; older ledger lines carry only `event`)."""
    return rec.get("type") or rec.get("event") or ""


def _iter_ledger_events():
    """Yield each decodable ledger record; skip blank/undecodable lines and a
    missing file (same tolerance as known_from_ledger)."""
    if not LEDGER_PATH.is_file():
        return
    try:
        lines = LEDGER_PATH.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            yield rec


def armed_checks_from_ledger() -> dict:
    """{check_id: info} anchored to the FIRST check_armed per id — the immutable
    append-only stamp (expectation + armed_ts). A later same-id arming with a
    DIFFERENT expectation sets info['duplicate_arm']=True (surfaced as an
    anomaly, never merged): taking the first, not the last, is what stops a
    re-emitted `id: cX` file's weakened expectation from migrating into an
    outcome event (known_from_ledger keys on basename, so a second arm is
    mechanically possible)."""
    armed: dict = {}
    for rec in _iter_ledger_events():
        if _event_type(rec) != "check_armed":
            continue
        cid = rec.get("check_id")
        if not cid:
            continue
        if cid not in armed:
            armed[cid] = {
                "armed_ts": rec.get("ts"),
                "check_window_days": rec.get("check_window_days"),
                "expectation": rec.get("expectation"),
                "run_id": rec.get("run_id", ""),
                "cluster": rec.get("cluster", ""),
                "lane": rec.get("lane", ""),
                "duplicate_arm": False,
            }
        elif rec.get("expectation") != armed[cid]["expectation"]:
            armed[cid]["duplicate_arm"] = True
    return armed


def recorded_outcomes_from_ledger() -> set:
    """check_ids that already carry any check_kept/check_violated event."""
    recorded = set()
    for rec in _iter_ledger_events():
        if _event_type(rec) in OUTCOME_EVENTS and rec.get("check_id"):
            recorded.add(rec["check_id"])
    return recorded


def _window_seconds(check_window_days):
    """days → seconds, or None when unparseable (armed with a bad window)."""
    try:
        return int(str(check_window_days)) * 86400
    except (TypeError, ValueError):
        return None


def is_matured(armed_info: dict, now: float) -> bool:
    """now >= armed_ts + window (inclusive). False for an unparseable window or
    a non-numeric armed_ts (those are handled as WINDOW-PARSE-ERROR upstream)."""
    win = _window_seconds(armed_info.get("check_window_days"))
    ts = armed_info.get("armed_ts")
    if win is None or not isinstance(ts, (int, float)):
        return False
    return now >= ts + win


def _load_verdicts(path: str) -> dict:
    """{check_id: {'verdict': str, 'evidence': str}}. The file value is either a
    bare verdict string or a {verdict, evidence} object. Raises ValueError on a
    structurally malformed file (not JSON-object) — a bad verdicts file must
    fail loudly, never silently record nothing."""
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError("verdicts file must be a JSON object (check_id -> verdict)")
    out: dict = {}
    for cid, val in raw.items():
        if isinstance(val, str):
            out[cid] = {"verdict": val.strip(), "evidence": ""}
        elif isinstance(val, dict):
            out[cid] = {"verdict": str(val.get("verdict", "")).strip(),
                        "evidence": str(val.get("evidence", ""))}
        else:
            raise ValueError(f"verdict for {cid} must be a string or object")
    return out


class Disposition:
    """One armed check's evaluation decision. `event` is the dict of fields to
    ledger_append (None = nothing to record)."""
    __slots__ = ("check_id", "kind", "message", "event")

    def __init__(self, check_id, kind, message, event=None):
        self.check_id = check_id
        self.kind = kind
        self.message = message
        self.event = event


def plan_evaluations(armed: dict, recorded: set, verdicts: dict, now: float) -> list:
    """Pure: one Disposition per armed check (+ UNKNOWN-CHECK per verdict naming
    an un-armed id). No I/O, no clock — `now` is injected."""
    dispositions = []
    for cid in sorted(armed):
        info = armed[cid]
        win = _window_seconds(info.get("check_window_days"))
        if win is None or not isinstance(info.get("armed_ts"), (int, float)):
            dispositions.append(Disposition(
                cid, "WINDOW-PARSE-ERROR",
                f"armed with unparseable check_window_days="
                f"{info.get('check_window_days')!r}"))
            continue
        if cid in recorded:
            dispositions.append(Disposition(cid, "ALREADY-RECORDED",
                                            "outcome already in ledger"))
            continue
        if not is_matured(info, now):
            days_left = (info["armed_ts"] + win - now) / 86400
            dispositions.append(Disposition(cid, "NOT-DUE",
                                            f"matures in {days_left:.1f}d"))
            continue
        entry = verdicts.get(cid)
        if entry is None:
            dispositions.append(Disposition(cid, "AWAITING-VERDICT",
                                            "matured; no verdict supplied"))
            continue
        verdict, evidence = entry["verdict"], entry["evidence"]
        if verdict not in VERDICT_EVENT:
            dispositions.append(Disposition(
                cid, "REFUSED", f"unknown verdict {verdict!r} (kept|violated)"))
            continue
        if verdict == "violated" and not evidence.strip():
            dispositions.append(Disposition(
                cid, "REFUSED",
                "violated requires evidence (it feeds the next digest's revert draft)"))
            continue
        event = {
            "check_id": cid,
            "run_id": info.get("run_id", ""),
            "cluster": info.get("cluster", ""),
            "expectation": info.get("expectation"),
            "check_window_days": str(info.get("check_window_days")),
            "armed_ts": info["armed_ts"],
            "lane": info.get("lane", ""),
            "verdict": verdict,
            "evidence": evidence,
        }
        if info.get("duplicate_arm"):
            dispositions.append(Disposition(
                cid, "DUPLICATE-ARM",
                "re-armed later with a differing expectation; recording against "
                "the FIRST (immutable) stamp", event=event))
        else:
            dispositions.append(Disposition(cid, "RECORD", f"record {verdict}", event=event))
    for cid in sorted(verdicts):
        if cid not in armed:
            dispositions.append(Disposition(
                cid, "UNKNOWN-CHECK",
                "verdict names a check_id with no check_armed event"))
    return dispositions


def evaluate(verdicts_path, dry_run: bool, now: float) -> int:
    """Record outcome events for matured armed checks. Returns exit code."""
    verdicts = {}
    if verdicts_path:
        try:
            verdicts = _load_verdicts(verdicts_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"gardener-evaluate: cannot read verdicts file {verdicts_path}: {exc}",
                  file=sys.stderr)
            return 2
    armed = armed_checks_from_ledger()
    recorded = recorded_outcomes_from_ledger()
    dispositions = plan_evaluations(armed, recorded, verdicts, now)
    prefix = "would append" if dry_run else "appended"
    written, anomaly = 0, False
    for disp in dispositions:
        if disp.kind in ANOMALY_KINDS:
            anomaly = True
        if disp.event is not None:
            event_type = VERDICT_EVENT[disp.event["verdict"]]
            if not dry_run:
                ledger_append(event_type, **disp.event)
            written += 1
            print(f"  [{disp.kind}] {disp.check_id}: {prefix} {event_type} — {disp.message}")
        else:
            print(f"  [{disp.kind}] {disp.check_id}: {disp.message}")
    mode = "DRY-RUN (nothing written)" if dry_run else "recorded"
    print(f"gardener-evaluate: {mode}; {written} outcome event(s) across "
          f"{len(dispositions)} disposition(s)"
          + ("; ANOMALIES present → exit 2" if anomaly else ""))
    return 2 if anomaly else 0


def main(argv: list[str] | None = None) -> int:
    if not _HOME_ENV:
        print("gardener-postrun: HOME is not set — refusing to guess paths", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="Gardener artifact post-processor.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_post = sub.add_parser("postrun")
    p_post.add_argument("--run-id", required=True)
    p_post.add_argument("--lane", default="")
    p_post.add_argument("--known", default=None,
                        help="Optional file of extra known basenames (tests).")
    p_dec = sub.add_parser("decide")
    p_dec.add_argument("--proposal", required=True)
    p_dec.add_argument("--kind", required=True, choices=["accept", "decline"])
    p_dec.add_argument("--reason", default="")
    p_eval = sub.add_parser("evaluate")
    p_eval.add_argument("--verdicts", default=None,
                        help="JSON file mapping check_id -> 'kept'|'violated' or "
                             "{verdict, evidence}. Omit for a 'what's due' report.")
    p_eval.add_argument("--dry-run", action="store_true",
                        help="Print planned appends without writing the ledger.")
    p_eval.add_argument("--now", type=float, default=None,
                        help="Override the maturity clock (epoch seconds; tests/replay).")
    args = parser.parse_args(argv)

    if args.cmd == "postrun":
        known = known_from_ledger()
        if args.known and Path(args.known).is_file():
            known |= {line.strip() for line in Path(args.known).read_text().splitlines()
                      if line.strip()}
        summary = process_run_artifacts(args.run_id, known, lane=args.lane)
        print(f"gardener-postrun: proposals={summary['proposals']} "
              f"checks={summary['checks']} rejected={summary['rejected']}")
        return 0
    if args.cmd == "evaluate":
        now = args.now if args.now is not None else time.time()
        return evaluate(args.verdicts, args.dry_run, now)
    return decide(args.proposal, args.kind, args.reason)


if __name__ == "__main__":
    sys.exit(main())
