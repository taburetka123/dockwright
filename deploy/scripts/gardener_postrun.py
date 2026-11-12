#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path


_HOME_ENV = os.environ.get("HOME")
HOME = Path(_HOME_ENV) if _HOME_ENV else None
if HOME is None:
    HOME = Path("/nonexistent-no-home")


def _prefer_new(new: Path, legacy: Path) -> Path:
    if new.exists():
        return new
    if legacy.exists():
        return legacy
    return new


_GARDENER_DIR_ENV = os.environ.get("DOCKWRIGHT_GARDENER_DIR")
if _GARDENER_DIR_ENV:
    GARDENER_DIR = Path(_GARDENER_DIR_ENV)
else:
    GARDENER_DIR = _prefer_new(HOME / ".claude" / "dockwright" / "gardener", HOME / ".claude" / "gardener")
PENDING_DIR = GARDENER_DIR / "proposals" / "pending"
ACCEPTED_DIR = GARDENER_DIR / "proposals" / "accepted"
DECLINED_DIR = GARDENER_DIR / "proposals" / "declined"
REJECTED_DIR = GARDENER_DIR / "proposals" / "rejected"
CHECKS_DIR = GARDENER_DIR / "checks"
LEDGER_PATH = GARDENER_DIR / "ledger.jsonl"
FINDINGS_DIR = _prefer_new(HOME / ".claude" / "dockwright" / "selffix" / "findings", HOME / ".claude" / "selffix-findings")

def _scan_toml_str(text: str, section: str, key: str):
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


def config_path():
    env = os.environ.get("DOCKWRIGHT_CONFIG", "").strip()
    if env:
        p = Path(env).expanduser()
        return p if p.is_file() else None
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    candidates = [base / "dockwright" / "dockwright.toml",
                  Path.home() / ".claude" / "dockwright.toml"]
    return next((c for c in candidates if c.is_file()), None)


def config_toml_str(section: str, key: str) -> str:
    path = config_path()
    if path is None:
        return ""
    try:
        import tomllib
        with open(path, "rb") as fh:
            value = tomllib.load(fh).get(section, {}).get(key)
    except ModuleNotFoundError:
        try:
            value = _scan_toml_str(path.read_text(), section, key)
        except OSError:
            return ""
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def config_toml_int(section: str, key: str, default: int) -> int:
    path = config_path()
    if path is None:
        return default
    try:
        import tomllib
        with open(path, "rb") as fh:
            value = tomllib.load(fh).get(section, {}).get(key)
    except ModuleNotFoundError:
        try:
            value = _scan_toml_str(path.read_text(), section, key)
        except OSError:
            return default
    except Exception:
        return default
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except ValueError:
        return default


def _dockwright_repo() -> str:
    value = config_toml_str("paths", "dockwright_repo")
    return str(Path(value).expanduser()) if value else ""


_DOCKWRIGHT_REPO = _dockwright_repo()
ALLOWED_TARGET_ROOTS = [HOME / ".claude"] + (
    [Path(_DOCKWRIGHT_REPO)] if _DOCKWRIGHT_REPO else [])

PROPOSAL_REQUIRED_FIELDS = ("id", "run_id", "cluster", "targets", "lane",
                            "kind", "evidence_kind", "base_rev",
                            "expectation", "check_window_days", "revert")
LANES = ("digest", "frontier")
EVIDENCE_KINDS = ("findings", "ops", "external")
FLOW_COST_VERDICTS = ("none", "adds", "removes")
FINDING_MEMBER_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PROPOSAL_REQUIRED_PRESENT = ("always_on_bytes",)
PROPOSAL_REQUIRED_SECTIONS = ("## Evidence", "## Diff")
CHECK_REQUIRED_FIELDS = ("id", "run_id", "expectation", "check_window_days")


def ledger_append(event: str, **fields) -> None:
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
    return None, text


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


def _supersedes_ids(meta) -> list:
    return [str(sid).strip() for sid in _as_list(meta.get("supersedes")) if str(sid).strip()]


_DIFF_HEADER_RE = re.compile(r"^(?:\+\+\+|---)\s+(\S+)", re.MULTILINE)
_DIFF_FENCE_RE = re.compile(r"^```diff\s*$", re.MULTILINE)


def diff_paths(body: str) -> list[str]:
    paths = []
    for match in _DIFF_HEADER_RE.finditer(body):
        path = match.group(1)
        if path == "/dev/null":
            continue
        paths.append(path)
    return paths


def _diff_path_violations(body: str, declared_targets: list[str]) -> list[str]:
    violations = []
    resolved_targets = [str(Path(os.path.realpath(os.path.expanduser(t))))
                        for t in declared_targets]
    for raw in diff_paths(body):
        if raw.startswith(("/", "~")):
            if not _target_in_scope(raw):
                violations.append(f"diff patches path outside allowed roots (FR-8): {raw}")
            elif str(Path(os.path.realpath(os.path.expanduser(raw)))) not in resolved_targets:
                violations.append(
                    f"diff patches a path not declared in targets: {raw}")
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
    violations.extend(_always_on_bytes_violations(meta, body))
    violations.extend(_flow_cost_violations(meta))
    return violations


def _overlay_dir() -> str:
    value = config_toml_str("paths", "overlay_dir") or "~/.claude/dockwright-overlay"
    return os.path.realpath(os.path.expanduser(value))


_OVERLAY_DIR = _overlay_dir()
_LEGACY_OVERLAY_DIR = os.path.realpath(os.path.expanduser("~/.claude/orchestrator-overlay"))
_ALWAYS_ON_PARTS = frozenset({"rules", "agents", "roles"})


def _is_always_on(resolved_path: str) -> bool:
    if not _target_in_scope(resolved_path):
        return False
    if _ALWAYS_ON_PARTS.intersection(Path(resolved_path).parts):
        return True
    return any(resolved_path == root or resolved_path.startswith(root + os.sep)
               for root in (_OVERLAY_DIR, _LEGACY_OVERLAY_DIR))


def _hunk_body_bytes(body_lines) -> int:
    delta = 0
    prev_sign = None
    for ln in body_lines:
        if ln.startswith("\\"):
            if prev_sign == "+":
                delta -= 1
            elif prev_sign == "-":
                delta += 1
            prev_sign = None
        elif ln.startswith("+"):
            delta += len(ln[1:].encode("utf-8")) + 1
            prev_sign = "+"
        elif ln.startswith("-"):
            delta -= len(ln[1:].encode("utf-8")) + 1
            prev_sign = "-"
        else:
            prev_sign = None
    return delta


def _strict_hunk_bodies(flat_hunks) -> list:
    bodies, cur = [], None
    for ln in flat_hunks:
        if ln.startswith("@@"):
            cur = []
            bodies.append(cur)
        elif cur is not None:
            cur.append(ln)
    return bodies


def compute_always_on_delta(body: str, declared_targets: list):
    try:
        import gardener_apply
        diff_text = gardener_apply.extract_diff_text(body)
    except Exception:  # noqa: BLE001 — no ```diff fence ⇒ unknowable
        return None
    try:
        strict = [(fd.old_raw, fd.new_raw, _strict_hunk_bodies(fd.hunks))
                  for fd in gardener_apply.split_file_diffs(diff_text)]
    except Exception:  # noqa: BLE001 — strict refuses (bare-@@ &c.)
        strict = None
    try:
        lenient = [(fd.old_raw, fd.new_raw, list(fd.hunks))
                   for fd in gardener_apply.lenient_parse(diff_text)]
    except Exception:  # noqa: BLE001 — lenient refuses
        lenient = None
    if strict is None:
        if lenient is None:
            return None
        per_file = lenient
    elif lenient is None:
        return None
    elif strict == lenient:
        per_file = strict
    else:
        per_file = lenient
    declared_abs = [os.path.realpath(os.path.expanduser(t))
                    for t in declared_targets]
    delta = 0
    for old_raw, new_raw, bodies in per_file:
        try:
            old_abs = gardener_apply._resolve_one(old_raw, declared_abs)
            new_abs = gardener_apply._resolve_one(new_raw, declared_abs)
        except Exception:  # noqa: BLE001 — unresolvable ⇒ unknowable
            return None
        path = new_abs if new_abs is not None else old_abs
        if path is None or not _is_always_on(path):
            continue
        for hunk_body in bodies:
            delta += _hunk_body_bytes(hunk_body)
    return delta


_BYTES_TOLERANCE = 16


def _bytes_tolerance() -> int:
    return config_toml_int("gardener", "bytes_tolerance", _BYTES_TOLERANCE)


def _always_on_bytes_violations(meta, body: str) -> list:
    if "always_on_bytes" not in meta:
        return []
    violations = []
    declared = None
    declared_raw = str(meta.get("always_on_bytes", "")).strip()
    try:
        declared = int(declared_raw)
    except (TypeError, ValueError):
        violations.append(f"always_on_bytes is not an integer: {declared_raw!r}")
    if not _DIFF_FENCE_RE.search(body):
        return violations
    computed = compute_always_on_delta(body, _as_list(meta.get("targets")))
    if computed is None:
        return violations
    tolerance = _bytes_tolerance()
    if declared is not None and abs(declared - computed) > tolerance:
        violations.append(f"always_on_bytes mismatch: declared {declared}, "
                          f"diff computes {computed:+d} always-on bytes")
    if computed > tolerance and not "".join(
            _as_list(meta.get("cost_justification"))).strip():
        violations.append(
            f"positive always-on delta ({computed:+d} B) without "
            "cost_justification — a proposal that grows always-loaded "
            "context must declare the value claim + the cheaper home it "
            "rejected (censor, PRD A5)")
    return violations


_FLOW_COST_RE = re.compile(r"([A-Za-z]+)(.*)", re.DOTALL)
_FLOW_COST_PLACEHOLDER = re.compile(r"<(?:one )?clause>")
_FLOW_COST_NOISE = " \t\n`*'\".,:;-\u2014\u2013"


def _flow_cost_violations(meta) -> list:
    raw = "".join(_as_list(meta.get("flow_cost"))).strip(_FLOW_COST_NOISE)
    raw = "" if _FLOW_COST_PLACEHOLDER.search(raw) else raw
    if not raw:
        return ["missing required field: flow_cost — answer what an ordinary "
                "run that does NOT hit this problem pays "
                f"({'|'.join(FLOW_COST_VERDICTS)})"]
    match = _FLOW_COST_RE.match(raw)
    verdict = match.group(1).lower() if match else raw
    note = match.group(2) if match else ""
    if verdict not in FLOW_COST_VERDICTS:
        return [f"flow_cost verdict must be one of {FLOW_COST_VERDICTS}: "
                f"{verdict!r}"]
    if verdict != "none" and not note.strip(_FLOW_COST_NOISE):
        return [f"flow_cost: {verdict} requires a one-line note naming the "
                "recurring surface and the per-what (per review round / per "
                "PR / per test run / per session)"]
    return []


_APPLY_CHECK_FAIL = ("drifted", "ambiguous", "missing-file", "malformed",
                     "out-of-scope")

BACKPRESSURE_EVERY_DEFAULT = 2
BACKPRESSURE_MIN_BYTES_DEFAULT = 128
_APPLY_CHECK_QUALIFY = ("clean", "reanchorable")


def _apply_check(path: str, body: str):
    if not _DIFF_FENCE_RE.search(body):
        return "no-diff", ""
    try:
        import gardener_apply
        bound = gardener_apply.gardener_postrun
        saved = bound.ALLOWED_TARGET_ROOTS
        bound.ALLOWED_TARGET_ROOTS = ALLOWED_TARGET_ROOTS
        try:
            cls = gardener_apply.classify_proposal(path, env_lenient=True)
        finally:
            bound.ALLOWED_TARGET_ROOTS = saved
        return cls.klass, cls.detail
    except Exception as exc:  # noqa: BLE001 — env problem, fail open+loud
        return "skipped-env", f"{type(exc).__name__}: {exc}"


def validate_check(meta) -> list[str]:
    if not isinstance(meta, dict):
        return ["no parseable frontmatter"]
    return [f"missing required field: {field}"
            for field in CHECK_REQUIRED_FIELDS if not meta.get(field)]


class _ValidateError(Exception):
    pass


def _always_on_info(meta, body: str) -> str:
    declared = meta.get("always_on_bytes", "<missing>") if isinstance(meta, dict) else "<missing>"
    if not _DIFF_FENCE_RE.search(body):
        return f"always_on_bytes: declared {declared}, n/a (no diff fence)"
    computed = compute_always_on_delta(body, _as_list(meta.get("targets"))
                                       if isinstance(meta, dict) else [])
    if computed is None:
        return f"always_on_bytes: declared {declared}, computed unknown (diff unresolvable)"
    return f"always_on_bytes: declared {declared}, computed {computed:+d}"


def validate_artifact(path_str: str, is_check: bool) -> tuple:
    path = Path(path_str)
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        raise _ValidateError(f"cannot read {path_str}: {exc}")
    meta, body = parse_frontmatter(text)
    if is_check:
        return validate_check(meta), [], ""
    violations = validate_proposal(meta, body)
    notes = []
    if isinstance(meta, dict):
        violations.extend(supersede_violations(path, meta, body, known_from_ledger()))
        verdict, detail = _apply_check(str(path), body)
        if verdict in _APPLY_CHECK_FAIL:
            violations.append(f"apply-check failed ({verdict}): {detail}")
        elif verdict == "skipped-env":
            notes.append(f"apply-check skipped (environment): {detail}")
    return violations, notes, _always_on_info(meta, body)


def _unique_dest(dest_dir: Path, name: str) -> Path:
    dest = dest_dir / name
    if not dest.exists():
        return dest
    stem, suffix = os.path.splitext(name)
    n = 2
    while (dest_dir / f"{stem}-{n}{suffix}").exists():
        n += 1
    return dest_dir / f"{stem}-{n}{suffix}"


def superseded_dir() -> Path:
    return PENDING_DIR.parent / "superseded"


def _move_lock_path() -> Path:
    return LEDGER_PATH.parent / "proposals" / ".move.lock"


@contextlib.contextmanager
def move_lock():
    path = _move_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def open_apply_ids() -> set:
    last_started: dict = {}
    last_closed: dict = {}
    for idx, rec in enumerate(_iter_ledger_events()):
        pid = rec.get("proposal_id")
        if not pid:
            continue
        kind = _event_type(rec)
        if kind == "proposal_apply_started":
            last_started[pid] = idx
        elif kind == "proposal_reverted":
            last_closed[pid] = idx
        elif (kind == "proposal_apply_failed" and rec.get("after_start") is True
              and rec.get("rollback_failed") is not True):
            last_closed[pid] = idx
    return {pid for pid, idx in last_started.items() if idx > last_closed.get(pid, -1)}


def supersession_touched_ids() -> set:
    return {str(rec.get("proposal_id")) for rec in _iter_ledger_events()
            if _event_type(rec) in ("proposal_superseded", "proposal_restored")
            and rec.get("proposal_id")}


def pending_by_id(exclude=None) -> dict:
    found: dict = {}
    for path in sorted(PENDING_DIR.glob("*.md")):
        if exclude is not None and path.name == Path(exclude).name:
            continue
        try:
            meta, _body = parse_frontmatter(path.read_text())
        except (OSError, UnicodeDecodeError):
            continue
        if isinstance(meta, dict) and meta.get("id"):
            found[str(meta["id"])] = (path, meta)
    return found


def _member_covered(member: str, carriers: list) -> bool:
    return any(c == member or c.startswith(member) or member.startswith(c)
               for c in carriers)


def _pending_members() -> list:
    carried: list = []
    for _path, other in pending_by_id().values():
        carried.extend(_as_list(other.get("members")))
    return carried


def _supersede_id_violations(sid: str, meta: dict, hit, known: set, open_ids: set) -> list:
    if sid == str(meta.get("id")):
        return [f"supersedes names the proposal itself: {sid}"]
    if hit is None:
        return [f"supersedes id is not a pending proposal: {sid}"]
    old_path, old_meta = hit
    violations = []
    if old_path.name not in known:
        violations.append(
            f"supersedes id was born in this postrun and is not yet in the ledger: {sid}")
    if str(old_meta.get("lane") or "digest") != str(meta.get("lane") or "digest"):
        violations.append(f"supersedes id is in another lane: {sid}")
    if sid in open_ids:
        violations.append(f"supersedes id has an open apply: {sid}")
    old_members = _as_list(old_meta.get("members"))
    new_is_findings = str(meta.get("evidence_kind") or "findings") == "findings"
    if old_members and not new_is_findings:
        violations.append(
            f"supersedes {sid}, which carries members, but evidence_kind is not findings")
    elif old_members:
        new_members = _as_list(meta.get("members"))
        dropped = [m for m in old_members if not _member_covered(m, new_members)]
        if dropped:
            violations.append(
                f"supersedes {sid} but drops its members: {','.join(dropped)}")
    return violations


def supersede_violations(path: Path, meta: dict, body: str, known: set, ids=None) -> list:
    if ids is None:
        ids = _supersedes_ids(meta)
    if not ids:
        return []
    violations = []
    if "## Supersedes" not in body:
        violations.append("missing required section: ## Supersedes (supersedes is set)")
    pending = pending_by_id(exclude=path)
    open_ids = open_apply_ids()
    for sid in ids:
        violations.extend(_supersede_id_violations(sid, meta, pending.get(sid), known, open_ids))
    return violations


def _edit_frontmatter(path: Path, add=None, remove=()) -> None:
    st = path.stat()
    lines = path.read_text().splitlines(keepends=True)
    close = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    head = [ln for ln in lines[1:close] if ln.partition(":")[0].strip() not in remove]
    head += [f"{key}: {value}\n" for key, value in (add or {}).items()]
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("".join([lines[0], *head, *lines[close:]]))
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def _supersede_one(old_path: Path, old_meta: dict, successor_id: str, run_id: str) -> Path:
    dest_dir = superseded_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(dest_dir, old_path.name)
    ledger_append("proposal_superseded", proposal_id=str(old_meta.get("id")),
                  superseded_by=successor_id, run_id=run_id, path=str(dest),
                  from_path=str(old_path), lane=str(old_meta.get("lane") or "digest"),
                  members=",".join(_as_list(old_meta.get("members"))))
    shutil.move(str(old_path), str(dest))
    _edit_frontmatter(dest, add={"superseded_by": successor_id, "superseded_run": run_id},
                      remove=("superseded_by", "superseded_run"))
    return dest


def apply_supersession(meta: dict, run_id: str) -> tuple:
    ids = _supersedes_ids(meta)
    if not ids:
        return 0, ""
    successor = str(meta.get("id"))
    with move_lock():
        blocked = sorted(set(ids) & open_apply_ids())
        if blocked:
            return 0, f"supersedes id has an open apply: {','.join(blocked)}"
        pending = pending_by_id()
        missing = [sid for sid in ids if sid not in pending]
        if missing:
            return 0, f"supersedes id is not a pending proposal: {','.join(missing)}"
        for sid in ids:
            old_path, old_meta = pending[sid]
            _supersede_one(old_path, old_meta, successor, run_id)
    return len(ids), ""


def _completion_plan(meta: dict) -> list:
    successor = str(meta.get("id"))
    open_ids = open_apply_ids()
    touched = supersession_touched_ids()
    pending = pending_by_id()
    return [sid for sid in _supersedes_ids(meta)
            if sid != successor and sid in pending and sid not in open_ids
            and sid not in touched]


def _complete_supersession_locked(meta: dict, run_id: str, ids: list) -> int:
    successor = str(meta.get("id"))
    pending = pending_by_id()
    moved = 0
    for sid in ids:
        hit = pending.get(sid)
        if hit is None:
            continue
        old_path, old_meta = hit
        _supersede_one(old_path, old_meta, successor, run_id)
        moved += 1
    return moved


def _in_superseded_dir(src: Path) -> bool:
    return os.path.realpath(str(src.parent)) == os.path.realpath(str(superseded_dir()))


def _restorable_source(sid: str, successor_id: str, rec: dict):
    src = Path(str(rec.get("path", "")))
    if not src.is_file() or not _in_superseded_dir(src):
        return None
    try:
        src_meta, _body = parse_frontmatter(src.read_text())
    except (OSError, UnicodeDecodeError):
        return None
    if not isinstance(src_meta, dict) or str(src_meta.get("id")) != sid:
        return None
    stamp = src_meta.get("superseded_by")
    if stamp and str(stamp) != successor_id:
        return None
    return src


def restore_candidates(successor_id: str, ids: list) -> tuple:
    moved: dict = {}
    restored_at: dict = {}
    for idx, rec in enumerate(_iter_ledger_events()):
        sid = rec.get("proposal_id")
        kind = _event_type(rec)
        if kind == "proposal_superseded" and rec.get("superseded_by") == successor_id:
            moved[sid] = (idx, rec)
        elif kind == "proposal_restored":
            restored_at[sid] = idx
    pending = pending_by_id()
    plan, unrestorable = [], {}
    for sid in ids:
        if sid not in moved or restored_at.get(sid, -1) > moved[sid][0]:
            continue
        if sid in pending:
            continue
        rec = moved[sid][1]
        src = _restorable_source(sid, successor_id, rec)
        if src is None:
            unrestorable[sid] = [m for m in str(rec.get("members") or "").split(",") if m]
            continue
        plan.append((sid, src, Path(str(rec.get("from_path") or src.name)).name))
    return plan, unrestorable


def restore_one(sid: str, src: Path, name: str, successor_id: str) -> Path:
    dest = PENDING_DIR / name
    shutil.move(str(src), str(dest))
    _edit_frontmatter(dest, remove=("superseded_by", "superseded_run"))
    ledger_append("proposal_restored", proposal_id=sid, restored_by=successor_id,
                  path=str(dest))
    return dest


def _quarantine(path: Path, reasons: list[str], run_id: str, lane: str = "digest") -> None:
    REJECTED_DIR.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(REJECTED_DIR, path.name)
    shutil.move(str(path), str(dest))
    ledger_append("proposal_rejected", run_id=run_id, path=str(dest),
                  reasons="; ".join(reasons), lane=lane)


def known_from_ledger() -> set[str]:
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


def _backpressure_stats():
    streak, seen = 0, set()
    for rec in _iter_ledger_events():
        if _event_type(rec) != "backpressure" or rec.get("lane") != "digest":
            continue
        seen.add(rec.get("run_id"))
        try:
            proposals = int(rec.get("proposals") or 0)
            negative = int(rec.get("negative") or 0)
        except (TypeError, ValueError):
            continue
        if negative > 0:
            streak = 0
        elif proposals > 0:
            streak += 1
    return streak, seen


def record_backpressure(run_id: str, proposals: int, negative: int):
    streak, seen = _backpressure_stats()
    if run_id in seen:
        return None
    if negative > 0:
        streak = 0
    elif proposals > 0:
        streak += 1
    every = config_toml_int("gardener", "backpressure_every",
                            BACKPRESSURE_EVERY_DEFAULT)
    violation = streak >= every
    if violation:
        print(f"WARNING: back-pressure violation — {streak} consecutive "
              "proposal-bearing digest runs carried zero negative-byte "
              "proposals (spec: PRD v2 Amendment A4)", file=sys.stderr)
    ledger_append("backpressure", run_id=run_id, lane="digest",
                  proposals=proposals, negative=negative,
                  streak=streak, violation=violation)
    return {"streak": streak, "violation": violation}


def process_run_artifacts(run_id: str, known: set[str], lane: str = "") -> dict:
    summary = {"proposals": 0, "checks": 0, "rejected": 0, "skipped_env": 0,
               "digest_proposals": 0, "digest_negative": 0, "superseded": 0}
    for d in (PENDING_DIR, CHECKS_DIR, REJECTED_DIR):
        d.mkdir(parents=True, exist_ok=True)
    for path in sorted(PENDING_DIR.glob("*.md")):
        if path.name in known:
            continue
        meta, body = parse_frontmatter(path.read_text())
        violations = validate_proposal(meta, body)
        if isinstance(meta, dict):
            violations.extend(supersede_violations(path, meta, body, known))
        if violations:
            _quarantine(path, violations, run_id,
                        lane=str((meta or {}).get("lane") or lane or "digest"))
            summary["rejected"] += 1
            continue
        verdict, detail = _apply_check(str(path), body)
        if verdict == "skipped-env":
            summary["skipped_env"] += 1
            print(f"WARNING: apply-check skipped (environment) for "
                  f"{path.name}: {detail}", file=sys.stderr)
        if verdict in _APPLY_CHECK_FAIL:
            _quarantine(path,
                        [f"apply-check failed at birth ({verdict}): {detail}"],
                        run_id,
                        lane=str((meta or {}).get("lane") or lane or "digest"))
            summary["rejected"] += 1
            continue
        members = _as_list(meta.get("members"))
        targets = _as_list(meta.get("targets"))
        supersedes = _supersedes_ids(meta)
        ledger_append("proposal", run_id=run_id, proposal_id=str(meta.get("id")),
                      path=str(path), cluster=str(meta.get("cluster", "")),
                      members=",".join(members), targets=",".join(targets),
                      lane=str(meta.get("lane") or lane or "digest"),
                      evidence_kind=str(meta.get("evidence_kind") or "findings"),
                      apply_check=verdict,
                      supersedes=",".join(supersedes),
                      **{"class": str(meta.get("kind", ""))})
        moved, refusal = apply_supersession(meta, run_id)
        if refusal:
            _quarantine(path, [refusal], run_id,
                        lane=str(meta.get("lane") or lane or "digest"))
            summary["rejected"] += 1
            continue
        summary["superseded"] += moved
        summary["proposals"] += 1
        eff_lane = str(meta.get("lane") or lane or "digest")
        if eff_lane == "digest" and str(meta.get("kind", "")) != "proposal-retire":
            summary["digest_proposals"] += 1
            if verdict in _APPLY_CHECK_QUALIFY:
                min_bytes = config_toml_int("gardener", "backpressure_min_bytes",
                                            BACKPRESSURE_MIN_BYTES_DEFAULT)
                delta = compute_always_on_delta(body, targets)
                if delta is not None and delta <= -min_bytes:
                    summary["digest_negative"] += 1
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
    exact = FINDINGS_DIR / f"{sid}.md"
    if exact.is_file():
        return exact, "exact"
    hits = sorted(FINDINGS_DIR.glob(f"{sid}*.md"))
    if len(hits) == 1:
        return hits[0], "prefix"
    if len(hits) > 1:
        return None, "ambiguous"
    return None, "missing"


def decide(proposal_path: str, kind: str, reason: str, applied_rev=None,
           restore_superseded: bool = False) -> int:
    if kind not in ("accept", "decline"):
        print(f"gardener-decide: unknown kind {kind!r} (accept|decline)", file=sys.stderr)
        return 2
    if kind == "decline" and not reason.strip():
        print("gardener-decide: decline requires --reason — the recorded reason is "
              "what stops the cluster from re-surfacing (PRD §7.5)", file=sys.stderr)
        return 2
    if kind == "accept" and restore_superseded:
        print("gardener-decide: --restore-superseded applies to a decline only",
              file=sys.stderr)
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
    meta, body = parse_frontmatter(path.read_text())
    if not isinstance(meta, dict):
        print(f"gardener-decide: unparseable frontmatter in {path}", file=sys.stderr)
        return 2
    proposal_id = str(meta.get("id"))
    proposal_kind = str(meta.get("kind", ""))
    supersedes = _supersedes_ids(meta)
    restoring = kind == "decline" and bool(supersedes) and (
        proposal_kind == "proposal-retire" or restore_superseded)
    members = _as_list(meta.get("members"))
    evidence_kind = str(meta.get("evidence_kind") or "findings")
    keep_evidence = kind == "decline" and proposal_kind in ("corpus-retire", "proposal-retire")
    dest_dir = ACCEPTED_DIR if kind == "accept" else DECLINED_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    restored = []
    withheld = []
    with move_lock():
        restore_plan, unrestorable = (restore_candidates(proposal_id, supersedes)
                                      if restoring else ([], {}))
        collisions = [name for _sid, _src, name in restore_plan
                      if (PENDING_DIR / name).exists()]
        if collisions:
            print(f"gardener-decide: cannot restore — proposals/pending/ already holds "
                  f"{', '.join(collisions)}; nothing was moved", file=sys.stderr)
            return 2
        if not path.is_file():
            print(f"gardener-decide: no such proposal: {path} — a run superseded it "
                  "while this decide was starting; look in proposals/superseded/",
                  file=sys.stderr)
            return 2
        completion = _completion_plan(meta) if supersedes and not restoring else []
        if completion:
            violations = supersede_violations(path, meta, body, known_from_ledger(),
                                              ids=completion)
            if violations:
                hint = ("\n  to decline without moving the proposals it supersedes, re-run "
                        "with --restore-superseded" if kind == "decline" else "")
                print(f"gardener-decide: cannot complete the supersession declared by "
                      f"{proposal_id} — nothing was moved:\n  "
                      + "\n  ".join(violations) + hint, file=sys.stderr)
                return 2
            _complete_supersession_locked(meta, f"decide-{proposal_id}", completion)
        for sid in unrestorable:
            print(f"gardener-decide: {sid} not restored — the ledger says {proposal_id} "
                  "superseded it, but proposals/superseded/ holds no file with that id under "
                  "that stamp: the file is missing, the proposal was decided since, or a "
                  "later run superseded it again", file=sys.stderr)
        for sid, src, name in restore_plan:
            restore_one(sid, src, name, proposal_id)
            restored.append(sid)
        dest = _unique_dest(dest_dir, path.name)
        shutil.move(str(path), str(dest))
        if evidence_kind == "findings" and not keep_evidence:
            withheld = _pending_members() + [m for ms in unrestorable.values() for m in ms]
    marked, ambiguous, missing, kept = [], [], [], []
    if evidence_kind == "findings" and not keep_evidence:
        for sid in members:
            if _member_covered(sid, withheld):
                kept.append(sid)
                continue
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
    ledger_append("decision", kind=kind, proposal_id=proposal_id,
                  path=str(dest), cluster=str(meta.get("cluster", "")),
                  members=",".join(members), reason=reason,
                  members_marked=",".join(marked),
                  members_ambiguous=",".join(ambiguous),
                  members_kept=",".join(kept),
                  lane=str(meta.get("lane") or "digest"),
                  evidence_kind=evidence_kind,
                  applied_rev=";".join(applied_rev or []),
                  supersedes=",".join(supersedes),
                  restored=",".join(restored),
                  not_restored=",".join(unrestorable),
                  **{"class": proposal_kind})
    kept_why = ("still carried by a pending proposal"
                + (" or by an id not restored" if unrestorable else ""))
    print(f"gardener-decide: {kind} {proposal_id} → {dest}; "
          f"marked reviewed: {len(marked)}/{len(members)} members"
          + (f"; kept unmarked ({kept_why}): {len(kept)}" if kept else "")
          + (f"; AMBIGUOUS prefixes left unmarked: {','.join(ambiguous)}" if ambiguous else "")
          + (f"; no finding file for: {','.join(missing)}" if missing else "")
          + (f"; members kept as evidence ({proposal_kind} decline)" if keep_evidence else "")
          + (f"; restored to pending: {','.join(restored)}" if restored else ""))
    return 0


OUTCOME_EVENTS = ("check_kept", "check_violated")
VERDICT_EVENT = {"kept": "check_kept", "violated": "check_violated"}
ANOMALY_KINDS = frozenset({"REFUSED", "UNKNOWN-CHECK", "WINDOW-PARSE-ERROR", "DUPLICATE-ARM"})


def _event_type(rec: dict) -> str:
    return rec.get("type") or rec.get("event") or ""


def _iter_ledger_events():
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
    recorded = set()
    for rec in _iter_ledger_events():
        if _event_type(rec) in OUTCOME_EVENTS and rec.get("check_id"):
            recorded.add(rec["check_id"])
    return recorded


def _window_seconds(check_window_days):
    try:
        return int(str(check_window_days)) * 86400
    except (TypeError, ValueError):
        return None


def is_matured(armed_info: dict, now: float) -> bool:
    win = _window_seconds(armed_info.get("check_window_days"))
    ts = armed_info.get("armed_ts")
    if win is None or not isinstance(ts, (int, float)):
        return False
    return now >= ts + win


def _load_verdicts(path: str) -> dict:
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
    __slots__ = ("check_id", "kind", "message", "event")

    def __init__(self, check_id, kind, message, event=None):
        self.check_id = check_id
        self.kind = kind
        self.message = message
        self.event = event


def plan_evaluations(armed: dict, recorded: set, verdicts: dict, now: float) -> list:
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


def annotate(ref: str, note: str) -> int:
    ledger_append("annotate", ref=ref, note=note)
    print(f"gardener-annotate: appended annotate ref={ref!r}")
    return 0


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
    p_dec.add_argument("--applied-rev", action="append", default=None, dest="applied_rev",
                       help="root=sha that applied this proposal (repeatable; recorded "
                            "in the decision event — closes the Phase-2 'capture the "
                            "applying SHA' item)")
    p_dec.add_argument("--restore-superseded", action="store_true",
                       dest="restore_superseded",
                       help="On decline, return the proposals this one superseded to "
                            "proposals/pending/.")
    p_eval = sub.add_parser("evaluate")
    p_eval.add_argument("--verdicts", default=None,
                        help="JSON file mapping check_id -> 'kept'|'violated' or "
                             "{verdict, evidence}. Omit for a 'what's due' report.")
    p_eval.add_argument("--dry-run", action="store_true",
                        help="Print planned appends without writing the ledger.")
    p_eval.add_argument("--now", type=float, default=None,
                        help="Override the maturity clock (epoch seconds; tests/replay).")
    p_ann = sub.add_parser("annotate")
    p_ann.add_argument("--ref", required=True,
                       help="ts or id of the ledger event being annotated.")
    p_ann.add_argument("--note", required=True,
                       help="The correction/annotation text.")
    sub.add_parser("open-applies",
                   help="Print each proposal id with an open apply, one per line (read-only).")
    p_val = sub.add_parser("validate",
                           help="Read-only pre-flight: run the birth gate's own "
                                "rules against a drafted artifact.")
    g_val = p_val.add_mutually_exclusive_group(required=True)
    g_val.add_argument("--proposal")
    g_val.add_argument("--check", dest="check_path")
    args = parser.parse_args(argv)

    if args.cmd == "open-applies":
        for pid in sorted(open_apply_ids()):
            print(pid)
        return 0
    if args.cmd == "validate":
        is_check = args.check_path is not None
        target = args.check_path if is_check else args.proposal
        try:
            violations, notes, info = validate_artifact(target, is_check)
        except _ValidateError as exc:
            print(f"gardener-validate: {exc}", file=sys.stderr)
            return 2
        if info:
            print(info)
        for violation in violations:
            print(violation)
        for note in notes:
            print(f"NOTE {note}")
        return 1 if violations else 0
    if args.cmd == "postrun":
        known = known_from_ledger()
        if args.known and Path(args.known).is_file():
            known |= {line.strip() for line in Path(args.known).read_text().splitlines()
                      if line.strip()}
        summary = process_run_artifacts(args.run_id, known, lane=args.lane)
        line = (f"gardener-postrun: proposals={summary['proposals']} "
                f"checks={summary['checks']} rejected={summary['rejected']} "
                f"skipped_env={summary['skipped_env']} "
                f"superseded={summary['superseded']}")
        if args.lane in ("", "digest"):
            bp = record_backpressure(args.run_id,
                                     summary["digest_proposals"],
                                     summary["digest_negative"])
            if bp is None:
                line += " backpressure=already-recorded"
            else:
                line += (f" backpressure={summary['digest_negative']}"
                         f"/{summary['digest_proposals']} streak={bp['streak']}")
                if bp["violation"]:
                    line += " VIOLATION"
        print(line)
        return 0
    if args.cmd == "evaluate":
        now = args.now if args.now is not None else time.time()
        return evaluate(args.verdicts, args.dry_run, now)
    if args.cmd == "annotate":
        return annotate(args.ref, args.note)
    return decide(args.proposal, args.kind, args.reason, applied_rev=args.applied_rev,
                  restore_superseded=args.restore_superseded)


if __name__ == "__main__":
    sys.exit(main())
