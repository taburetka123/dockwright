#!/usr/bin/env python3
"""Typed-proposal -> deterministic executor (Phase D T10).

A worker/engine writes a machine proposal FILE (flat items); a human or a
standing trust line authorizes acting on it; THIS script validates every
item fail-closed against an operator-authored verb-config and performs the
write by dispatching the operator's actuator argv - the model never types
action arguments into commands. Every outcome (executed / filtered /
refused / failed) is an append-only ledger event.

Gate on what runs, never on what is declared: predicates and syntax
patterns are evaluated on the fields of the proposal file being executed
(its sha256 is recorded in the `run` event), never on a summary.

Verb-config lives at <actions-dir>/verbs/<verb>.json, e.g.:

  {"verb": "queue-replay",
   "actuator": ["/path/to/actuator", "replay", "{queue}", "{message_id}"],
   "id_template": "{queue}/{message_id}",
   "patterns": {"queue": "^[a-z0-9][a-z0-9-]*$", "message_id": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
   "require": {"replay": "transient"},
   "forbid": {"source": "external-ci"},
   "allow": {"queue": ["a-dlq", "b-dlq"]},
   "ground_in": {"message_id": "/path/to/raw/{queue}.json"},
   "max_items": 50, "timeout_sec": 60, "max_age_sec": 3600}

`ground_in` files MUST be valid JSON; grounding is exact membership against
LEAF values (dict keys excluded) — a plain-text dump grounds nothing and
every item is REFUSED.

Every field substituted into argv, id_template, or a ground_in path MUST
have an entry in `patterns` (full-match) - an unpatterned templated field
is a config anomaly (exit 2), because that is exactly the channel where an
adversarial value (`--flag`, `../x`, whitespace) rides into a URL path or
gets parsed as a CLI flag by the actuator.

Exit codes:
  0  every item executed or filtered (policy mismatch / duplicate are
     EXPECTED outcomes; counters are printed explicitly - the everyday
     all-hold run is a loud no-op, not an error)
  1  >=1 item refused (structural per-item anomaly) or failed (actuator)
  2  structural call anomaly: unparseable proposal/config, format or verb
     mismatch, unpatterned templated field, over-cap, stale proposal,
     ledger lock busy. Never degrades to an empty success.

Sibling, not shared: gardener_apply.py is the separate governance-file patch
actuator (locates hunks, git-applies) with its own ledger and exit contract -
the "proposal"/"ledger.jsonl" nomenclature is shared but the state is disjoint.

Standalone, stdlib-only, py3.9-compatible (deployed-script convention).
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_HOME_ENV = os.environ.get("HOME")
HOME = Path(_HOME_ENV) if _HOME_ENV else Path("/nonexistent-no-home")
DEFAULT_ACTIONS_DIR = HOME / ".claude" / "dockwright" / "actions"

PROPOSAL_FORMAT = 1
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_]+)\}")
STDERR_TAIL = 200

CONFIG_REQUIRED = ("verb", "actuator", "id_template", "patterns", "max_items")


class LedgerAnomaly(Exception):
    """The ledger is damaged or holds an unresolved dispatch intent. Either
    means the dedup set can no longer be trusted, so the whole run fails
    closed (exit 2) rather than risk a silent double-fire."""


class Disposition:
    """One item's classification. argv is set only for kind=EXECUTE."""
    __slots__ = ("kind", "reason", "id_key", "argv")

    def __init__(self, kind, reason, id_key="", argv=None):
        self.kind = kind
        self.reason = reason
        self.id_key = id_key
        self.argv = argv


def parse_proposal(text: str) -> dict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"proposal is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise ValueError("proposal must be a JSON object")
    if data.get("proposal_format") != PROPOSAL_FORMAT:
        raise ValueError(
            f"proposal_format must be {PROPOSAL_FORMAT}, got {data.get('proposal_format')!r}")
    if not isinstance(data.get("verb"), str) or not data["verb"]:
        raise ValueError("proposal is missing a non-empty verb")
    items = data.get("items")
    if not isinstance(items, list):
        raise ValueError("proposal is missing an items list")
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"items[{i}] is not an object")
    return data


def load_verb_config(verbs_dir: Path, verb: str) -> dict:
    path = verbs_dir / f"{verb}.json"
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise ValueError(f"no verb-config: {path}")
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unreadable verb-config {path}: {exc}")
    if not isinstance(data, dict):
        raise ValueError(f"verb-config {path} must be a JSON object")
    return data


def _fields_in(template: str) -> set:
    return set(PLACEHOLDER_RE.findall(template))


def templated_fields(config: dict) -> set:
    fields = set()
    for element in config.get("actuator") or []:
        if isinstance(element, str):
            fields |= _fields_in(element)
    id_template = config.get("id_template")
    if isinstance(id_template, str):
        fields |= _fields_in(id_template)
    for path_template in (config.get("ground_in") or {}).values():
        if isinstance(path_template, str):
            fields |= _fields_in(path_template)
    return fields


def validate_config(config: dict) -> list:
    problems = []
    for key in CONFIG_REQUIRED:
        if key not in config:
            problems.append(f"missing required key: {key}")
    actuator = config.get("actuator")
    if not (isinstance(actuator, list) and actuator
            and all(isinstance(e, str) for e in actuator)):
        problems.append("actuator must be a non-empty list of strings")
    patterns = config.get("patterns")
    if not isinstance(patterns, dict):
        problems.append("patterns must be an object")
        patterns = {}
    for field, pattern in patterns.items():
        try:
            re.compile(pattern)
        except re.error as exc:
            problems.append(f"invalid pattern for {field}: {exc}")
    # The C1 guard: every templated field must carry a syntax pattern.
    for field in sorted(templated_fields(config)):
        if field not in patterns:
            problems.append(f"templated field has no syntax pattern: {field}")
    for key in ("require", "forbid"):
        value = config.get(key)
        if value is not None and not isinstance(value, dict):
            problems.append(f"{key} must be an object")
    allow = config.get("allow")
    if allow is not None:
        if not isinstance(allow, dict) or not all(
                isinstance(v, list) for v in allow.values()):
            problems.append("allow must be an object of lists")
    for key in ("max_items", "timeout_sec", "max_age_sec"):
        value = config.get(key)
        if value is not None and not (isinstance(value, int)
                                      and not isinstance(value, bool) and value > 0):
            problems.append(f"{key} must be a positive integer")
    ground_in = config.get("ground_in")
    if ground_in is not None and not (isinstance(ground_in, dict) and all(
            isinstance(v, str) for v in ground_in.values())):
        problems.append("ground_in must be an object of path templates")
    return problems


def compile_patterns(config: dict) -> dict:
    return {field: re.compile(pattern)
            for field, pattern in (config.get("patterns") or {}).items()}


def substitute(template: str, item: dict) -> str:
    return PLACEHOLDER_RE.sub(lambda m: str(item[m.group(1)]), template)


def _referenced_fields(config: dict) -> set:
    return (templated_fields(config)
            | set((config.get("require") or {}))
            | set((config.get("forbid") or {}))
            | set((config.get("allow") or {}))
            | set((config.get("ground_in") or {})))


def _leaf_values(node, acc: set) -> set:
    """Collect the LEAF VALUES of a parsed-JSON tree as strings. Dict values
    are recursed but keys are NOT collected (a JSON key must never ground an
    id); lists are recursed; str/int/float/bool leaves are added as str(leaf).
    Membership against this set is exact - never a substring."""
    if isinstance(node, dict):
        for value in node.values():
            _leaf_values(value, acc)
    elif isinstance(node, list):
        for value in node:
            _leaf_values(value, acc)
    elif isinstance(node, (str, int, float)):  # bool is a subclass of int
        acc.add(str(node))
    return acc


def _grounding_values(path: str, ground_cache: dict):
    """Cache and return (status, values) for a grounding file. status is one of
    'ok' (values is the leaf-value set), 'unreadable', or 'invalid'."""
    if path not in ground_cache:
        try:
            raw = Path(path).read_text()
        except OSError:
            ground_cache[path] = ("unreadable", None)
        else:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                ground_cache[path] = ("invalid", None)
            else:
                ground_cache[path] = ("ok", _leaf_values(parsed, set()))
    return ground_cache[path]


def classify_item(item, config, compiled_patterns, executed_keys, ground_cache):
    """Fail-closed pipeline: presence -> syntax -> policy -> grounding ->
    idempotency. Returns a Disposition; never raises on item content."""
    for field in sorted(_referenced_fields(config)):
        if field not in item:
            return Disposition("REFUSED", f"missing field: {field}")
    for field in sorted(templated_fields(config)):
        value = item[field]
        if not isinstance(value, str) or not compiled_patterns[field].fullmatch(value):
            return Disposition("REFUSED", f"field {field} fails syntax pattern: {value!r}")
    id_key = substitute(config["id_template"], item)
    for field, expected in (config.get("require") or {}).items():
        if item[field] != expected:
            return Disposition(
                "FILTERED", f"require {field}={expected!r}, got {item[field]!r}", id_key)
    for field, banned in (config.get("forbid") or {}).items():
        if item[field] == banned:
            return Disposition("FILTERED", f"forbid {field}={banned!r}", id_key)
    for field, allowed in (config.get("allow") or {}).items():
        if item[field] not in allowed:
            return Disposition("FILTERED", f"{field} not in allowlist: {item[field]!r}", id_key)
    for field, path_template in (config.get("ground_in") or {}).items():
        path = substitute(path_template, item)
        status, values = _grounding_values(path, ground_cache)
        if status == "unreadable":
            return Disposition("REFUSED", f"grounding file unreadable: {path}", id_key)
        if status == "invalid":
            return Disposition("REFUSED", f"grounding file is not valid JSON: {path}", id_key)
        if str(item[field]) not in values:
            return Disposition("REFUSED", f"value not grounded in {path}: {item[field]!r}", id_key)
    if id_key in executed_keys:
        return Disposition("FILTERED", "already executed (idempotency ledger)", id_key)
    argv = [substitute(element, item) for element in config["actuator"]]
    return Disposition("EXECUTE", "validated", id_key, argv)


def ledger_path(actions_dir: Path) -> Path:
    return actions_dir / "ledger.jsonl"


def ledger_append(actions_dir: Path, event: str, **fields) -> None:
    path = ledger_path(actions_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"type": event, "v": 1, "ts": time.time()}
    record.update(fields)
    with path.open("a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def executed_keys_from_ledger(actions_dir: Path, verb: str) -> set:
    """Dedup set for `verb` = id_keys with a recorded action_executed. Fails
    closed: a non-blank undecodable line, or a dispatch intent (M-a) with no
    recorded outcome, raises LedgerAnomaly - never a silently shrunk set."""
    keys = set()
    dispatched = set()
    resolved = set()
    path = ledger_path(actions_dir)
    if not path.is_file():
        return keys
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            raise LedgerAnomaly(
                f"ledger {path} line {lineno} is not valid JSON - refusing to "
                "run against a damaged ledger (its dedup set would silently "
                "shrink and re-fire an already-executed action)")
        if not (isinstance(record, dict) and record.get("verb") == verb
                and record.get("id_key")):
            continue
        rtype = record.get("type")
        if rtype == "action_executed":
            keys.add(record["id_key"])
            resolved.add(record["id_key"])
        elif rtype == "action_failed":
            resolved.add(record["id_key"])
        elif rtype == "action_dispatch":
            dispatched.add(record["id_key"])
    unresolved = dispatched - resolved
    if unresolved:
        raise LedgerAnomaly(
            f"unresolved dispatch in ledger for id_key(s) {sorted(unresolved)} "
            "- the actuator may have run; verify and repair before re-running")
    return keys


def _acquire_lock(actions_dir: Path):
    """Non-blocking exclusive lock for the whole run; busy -> None (exit 2).
    A parallel invocation must not interleave between the executed-keys read
    and the appends - that window is how a duplicate write would slip in."""
    actions_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(actions_dir / "ledger.lock", os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _dispatch(disposition, timeout_sec: int):
    """Run one actuator; returns (ok, exit_code_or_reason, stderr_tail)."""
    try:
        proc = subprocess.run(disposition.argv, capture_output=True,
                              timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        return False, "timeout", ""
    except OSError as exc:
        return False, "spawn-error", str(exc)[:STDERR_TAIL]
    tail = proc.stderr.decode("utf-8", errors="replace")[-STDERR_TAIL:]
    return proc.returncode == 0, proc.returncode, tail


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic executor for typed proposal files.")
    parser.add_argument("--verb", required=True)
    parser.add_argument("--proposal", required=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the validated plan; no dispatch, no ledger writes.")
    parser.add_argument("--approved-by", default="",
                        help="Recorded in the run event when a human gate applied.")
    parser.add_argument("--actions-dir", default=str(DEFAULT_ACTIONS_DIR))
    parser.add_argument("--now", type=float, default=None,
                        help="Override the staleness clock (epoch; tests).")
    args = parser.parse_args(argv)
    actions_dir = Path(args.actions_dir).expanduser()
    now = args.now if args.now is not None else time.time()

    proposal_path = Path(args.proposal).expanduser()
    try:
        raw = proposal_path.read_bytes()
    except OSError as exc:
        print(f"action-executor: cannot read proposal: {exc}", file=sys.stderr)
        return 2
    try:
        proposal = parse_proposal(raw.decode("utf-8", errors="strict"))
    except (ValueError, UnicodeDecodeError) as exc:
        print(f"action-executor: bad proposal: {exc}", file=sys.stderr)
        return 2
    if proposal["verb"] != args.verb:
        print(f"action-executor: --verb {args.verb} != proposal verb "
              f"{proposal['verb']!r}", file=sys.stderr)
        return 2
    if not proposal["items"]:
        print("action-executor: proposal has zero items - nothing to do and "
              "success must not share a signal; a broken adapter emitting [] "
              "must be loud (re-generate the proposal)", file=sys.stderr)
        return 2
    try:
        config = load_verb_config(actions_dir / "verbs", args.verb)
    except ValueError as exc:
        print(f"action-executor: {exc}", file=sys.stderr)
        return 2
    problems = validate_config(config)
    if problems:
        for problem in problems:
            print(f"action-executor: verb-config: {problem}", file=sys.stderr)
        return 2
    max_age = config.get("max_age_sec")
    if max_age is not None and now - proposal_path.stat().st_mtime > max_age:
        print(f"action-executor: proposal older than max_age_sec={max_age} - "
              "re-generate it (stale approvals must not fire late)", file=sys.stderr)
        return 2
    if len(proposal["items"]) > config["max_items"]:
        print(f"action-executor: {len(proposal['items'])} items exceed "
              f"max_items={config['max_items']} - whole call refused, nothing "
              "executed (no truncation)", file=sys.stderr)
        return 2

    lock_fd = None
    if not args.dry_run:
        lock_fd = _acquire_lock(actions_dir)
        if lock_fd is None:
            print("action-executor: ledger lock busy (another run in flight)",
                  file=sys.stderr)
            return 2
    try:
        compiled = compile_patterns(config)
        try:
            executed_keys = executed_keys_from_ledger(actions_dir, args.verb)
        except LedgerAnomaly as exc:
            print(f"action-executor: {exc}", file=sys.stderr)
            return 2
        ground_cache = {}
        dispositions = []
        batch_keys = set()
        for item in proposal["items"]:
            disposition = classify_item(item, config, compiled, executed_keys,
                                        ground_cache)
            if disposition.kind == "EXECUTE":
                # Intra-run dedup: a second item resolving to the same id_key
                # within ONE proposal must not dispatch twice. Kept SEPARATE
                # from executed_keys (the cross-run ledger set) so the reason is
                # honest - the first instance may still fail, so reusing the
                # ledger-idempotency reason here would mislabel it.
                if disposition.id_key in batch_keys:
                    disposition = Disposition(
                        "FILTERED", "duplicate id within proposal",
                        disposition.id_key)
                else:
                    batch_keys.add(disposition.id_key)
            dispositions.append(disposition)
        if any(d.kind != "REFUSED" and not d.id_key for d in dispositions):
            # An empty id_key is invisible to the dedup set AND the
            # unresolved-intent check (both filter on truthiness) — a record
            # that participates in nothing while everything reports success.
            print("action-executor: empty id_key from id_template — such a "
                  "record is invisible to idempotency; refusing the whole "
                  "call, fix patterns/id_template", file=sys.stderr)
            return 2
        counts = {"execute": 0, "filtered": 0, "refused": 0, "failed": 0}
        for disposition in dispositions:
            counts[disposition.kind.lower() if disposition.kind != "EXECUTE"
                   else "execute"] += 1

        if args.dry_run:
            for disposition in dispositions:
                line = f"  [{disposition.kind}] {disposition.id_key or '?'}: {disposition.reason}"
                if disposition.argv:
                    line += f" -> {disposition.argv}"
                print(line)
            print(f"action-executor: DRY-RUN execute={counts['execute']} "
                  f"filtered={counts['filtered']} refused={counts['refused']}")
            return 1 if counts["refused"] else 0

        ledger_append(actions_dir, "run", verb=args.verb,
                      proposal_path=str(proposal_path),
                      proposal_sha256=hashlib.sha256(raw).hexdigest(),
                      run_id=str(proposal.get("run_id", "")),
                      source_artifact=str(proposal.get("source_artifact", "")),
                      approved_by=args.approved_by,
                      n_items=len(dispositions), n_execute=counts["execute"],
                      n_filtered=counts["filtered"], n_refused=counts["refused"])
        timeout_sec = config.get("timeout_sec", 60)
        for disposition in dispositions:
            if disposition.kind == "FILTERED":
                ledger_append(actions_dir, "action_filtered", verb=args.verb,
                              id_key=disposition.id_key, reason=disposition.reason)
                continue
            if disposition.kind == "REFUSED":
                ledger_append(actions_dir, "action_refused", verb=args.verb,
                              id_key=disposition.id_key, reason=disposition.reason)
                continue
            # WAL intent: recorded BEFORE the actuator runs, so a crash in the
            # window before the outcome append is caught as UNRESOLVED on rerun
            # (executed_keys_from_ledger) instead of silently re-firing.
            ledger_append(actions_dir, "action_dispatch", verb=args.verb,
                          id_key=disposition.id_key, argv=disposition.argv)
            ok, code, tail = _dispatch(disposition, timeout_sec)
            if ok:
                ledger_append(actions_dir, "action_executed", verb=args.verb,
                              id_key=disposition.id_key, argv=disposition.argv)
            else:
                counts["failed"] += 1
                counts["execute"] -= 1
                ledger_append(actions_dir, "action_failed", verb=args.verb,
                              id_key=disposition.id_key, argv=disposition.argv,
                              exit_code=code, stderr_tail=tail)
        print(f"action-executor: executed={counts['execute']} "
              f"filtered={counts['filtered']} refused={counts['refused']} "
              f"failed={counts['failed']}")
        return 1 if counts["refused"] or counts["failed"] else 0
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
