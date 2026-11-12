from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from . import config, paths, state, transcript
from .boot_epoch import BootEpochUnavailable, resolve as resolve_boot_epoch
from .mcp_server import _input_is_idle
from .boot_epoch import plausible as boot_epoch_plausible
from .registry import _drop_questions_for_worker
from .session_pid import _looks_like_session, pid_command
from .stale_monitor import LIVE_TURN_RE, _closed_record
from .terminal import MANAGER_SESSION, TmuxDriver, get_driver

DIGEST_DIR_NAME = "revive"
UNATTRIBUTED_STEM = "_unattributed"
DIGEST_MAX_BYTES_ENV = "DOCKWRIGHT_REVIVE_DIGEST_MAX_BYTES"
DIGEST_MAX_BYTES_DEFAULT = 600_000
READY_TIMEOUT_ENV = "DOCKWRIGHT_REVIVE_READY_TIMEOUT_SEC"
READY_TIMEOUT_DEFAULT = 300.0
READY_POLL_SEC = 2.0
SPAWN_TIMEOUT_SEC = 15

VERDICT_REVIVE = "revive"
VERDICT_POSTBOOT = "skip-postboot"
VERDICT_COVERED = "skip-covered"
VERDICT_ALIVE = "skip-alive"
VERDICT_UNUSABLE = "skip-unusable"

VERDICTS_COVERING = (VERDICT_POSTBOOT, VERDICT_ALIVE)

ENG = "ENG"
MGR = "MGR"

_ALLOWED_EVENT_TYPES = frozenset({"user", "assistant"})
_LEADING_TAG = re.compile(r"^<[A-Za-z][A-Za-z0-9_-]*\b[^>]*>")
_SUMMARY_MAX_CHARS = 400
_CLAUDE_COMMAND_RE = re.compile(r"^\d+(\.\d+)+$")

INSTRUCTION = (
    "Your predecessor on domain {domain}, manager {name}, died in the host reboot. "
    "Read {path} before anything else — it holds the reduced transcript of that "
    "session plus a report on its dead workers — then pick that work back up."
)

RESUME_INSTRUCTION = (
    "You are manager {name} on domain {domain}, resumed on your own transcript after "
    "a host reboot at {stamp}. First invoke Skill(dockwright:arm-lanes) and, after "
    "`sleep 10`, confirm with `dockwright lanes` — your four lane monitors died with "
    "the host and nothing has re-armed them. {workers} Then continue your own work."
)
RESUME_WORKERS_SENTENCE = (
    "Your workers died in that reboot too: {names}. Resume each with "
    "resume_worker(<name>) — list_closed_workers shows them with closed_reason "
    "{reason} — and tell each one it was resumed after a host reboot and should pick "
    "its task back up."
)
RESUME_NO_WORKERS_SENTENCE = "No workers were registered to you, so there is nothing to resume."
RESUME_CLOSED_REASON = "host-reboot"
PAUSE_CLOSED_REASON = "fleet-pause"


@dataclass(frozen=True)
class Candidate:
    record: dict
    sid: str
    name: str
    domain: str
    verdict: str
    reason: str


@dataclass(frozen=True)
class Block:
    ts: str | None
    who: str
    text: str


def _is_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def registered_domain(record) -> str | None:
    domain = (record or {}).get("domain")
    if not isinstance(domain, str) or not domain.strip():
        return None
    return domain.strip()


def normalize_domain(record) -> str:
    return registered_domain(record) or paths.DEFAULT_DOMAIN


def digest_dir() -> Path:
    return paths.ROOT / DIGEST_DIR_NAME


def digest_path(sid: str) -> Path:
    return digest_dir() / f"{paths._safe_segment(sid or 'unknown')}.md"


def unattributed_digest_path() -> Path:
    return digest_dir() / f"{UNATTRIBUTED_STEM}.md"


def load_active_records() -> list[dict]:
    return [record for record in state.list_json_in(paths.ACTIVE)
            if isinstance(record, dict)]


def _home_projects_root(record) -> Path:
    home = Path(os.environ.get("HOME", ""))
    if (record.get("runtime") or "claude") == "codex":
        return home / ".codex" / "sessions"
    return home / ".claude" / "projects"


def _account_projects_root(record) -> Path | None:
    account = record.get("account")
    if not isinstance(account, str) or not account.strip():
        return None
    letter = account.strip()
    if letter == config.default_account():
        return None
    return paths.account_config_dir(letter) / "projects"


def transcript_search_paths(record) -> list[Path]:
    found: list[Path] = []
    recorded = record.get("transcript_path")
    if isinstance(recorded, str) and recorded.strip():
        found.append(Path(recorded.strip()))
    found.append(_home_projects_root(record))
    account_root = _account_projects_root(record)
    if account_root is not None:
        found.append(account_root)
    return found


def resolve_transcript(record) -> tuple[Path | None, list[Path]]:
    searched = transcript_search_paths(record)
    recorded = record.get("transcript_path")
    if isinstance(recorded, str) and recorded.strip():
        candidate = Path(recorded.strip())
        try:
            if candidate.is_file():
                return candidate, searched
        except OSError:
            pass
    sid = record.get("claude_sid")
    if isinstance(sid, str) and sid:
        try:
            found = transcript.find_session_log(sid, record.get("runtime") or "claude")
        except OSError:
            found = None
        if found is not None and found.is_file():
            return found, searched
        account_root = _account_projects_root(record)
        if account_root is not None:
            hit = transcript.find_session_log(sid, "claude", account_root)
            if hit is not None:
                return hit, searched
    return None, searched


def transcript_mtime_of(record) -> float | None:
    source_path, _searched = resolve_transcript(record)
    if source_path is None:
        return None
    try:
        return source_path.stat().st_mtime
    except OSError:
        return None


def _live_session_command(record) -> str:
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return ""
    if not state._pid_alive(pid):
        return ""
    command = pid_command(pid)
    return command if _looks_like_session(command) else ""


def record_pid_is_any_session(record) -> bool:
    return bool(_live_session_command(record))


def record_pid_is_session(record) -> bool:
    sid = record.get("claude_sid")
    if not isinstance(sid, str) or not sid:
        return False
    return sid in _live_session_command(record)


def classify_records(records, boot_epoch, transcript_mtime=transcript_mtime_of,
                     pid_is_session=None) -> list[Candidate]:
    pid_is_session = record_pid_is_session if pid_is_session is None else pid_is_session
    plan: list[Candidate] = []
    for record in records or []:
        if (not isinstance(record, dict) or record.get("agent") != "manager"
                or record.get("nested")):
            continue
        started = record.get("started_at")
        if not _is_number(started):
            verdict = VERDICT_UNUSABLE
            reason = "started_at is missing or not a number"
        elif started >= boot_epoch:
            verdict = VERDICT_POSTBOOT
            reason = f"started_at {started} postdates boot {boot_epoch}"
        else:
            mtime = transcript_mtime(record)
            if _is_number(mtime) and mtime >= boot_epoch:
                verdict = VERDICT_ALIVE
                reason = f"transcript activity at {mtime} postdates boot {boot_epoch}"
            elif pid_is_session(record):
                verdict = VERDICT_ALIVE
                reason = f"pid {record.get('pid')} is a live session (already resumed?)"
            else:
                verdict = VERDICT_REVIVE
                reason = f"started_at {started} predates boot {boot_epoch}"
        plan.append(Candidate(record=record,
                              sid=record.get("claude_sid") or "",
                              name=record.get("name") or "",
                              domain=normalize_domain(record),
                              verdict=verdict,
                              reason=reason))
    return plan


def _holder_of(candidate: Candidate) -> str:
    return candidate.name or candidate.sid or "?"


def _claim_key(candidate: Candidate) -> tuple[float, str]:
    started = candidate.record.get("started_at")
    return (-started if _is_number(started) else 0.0, candidate.sid)


def _covered_domains(classified) -> dict[str, str]:
    covered: dict[str, str] = {}
    for candidate in classified:
        if (candidate.verdict in VERDICTS_COVERING
                and registered_domain(candidate.record) is not None):
            covered.setdefault(candidate.domain, _holder_of(candidate))
    return covered


def _record_pid_alive(record) -> bool:
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False
    return state._pid_alive(pid)


def booting_managers(plan) -> list[Candidate]:
    return [c for c in plan
            if c.verdict in VERDICTS_COVERING
            and registered_domain(c.record) is None
            and _record_pid_alive(c.record)]


def _domain_winners(classified, covered) -> dict[str, Candidate]:
    winners: dict[str, Candidate] = {}
    for candidate in sorted(revive_candidates(classified), key=_claim_key):
        if candidate.domain not in covered and candidate.domain not in winners:
            winners[candidate.domain] = candidate
    return winners


def build_plan(records, boot_epoch, transcript_mtime=transcript_mtime_of,
               pid_is_session=None) -> list[Candidate]:
    classified = classify_records(records, boot_epoch, transcript_mtime, pid_is_session)
    covered = _covered_domains(classified)
    winners = _domain_winners(classified, covered)
    plan: list[Candidate] = []
    for candidate in classified:
        if (candidate.verdict != VERDICT_REVIVE
                or winners.get(candidate.domain) is candidate):
            plan.append(candidate)
            continue
        holder = covered.get(candidate.domain) or _holder_of(winners[candidate.domain])
        plan.append(replace(candidate, verdict=VERDICT_COVERED,
                            reason=f"already covered by {holder}"))
    return plan


def render_noop_line(plan, boot_epoch) -> str | None:
    if any(c.verdict in (VERDICT_REVIVE, VERDICT_COVERED) for c in plan):
        return None
    postboot = sum(1 for c in plan if c.verdict == VERDICT_POSTBOOT)
    alive = sum(1 for c in plan if c.verdict == VERDICT_ALIVE)
    unusable = sum(1 for c in plan if c.verdict == VERDICT_UNUSABLE)
    clauses = []
    if postboot:
        clauses.append(f"{postboot} record(s) postdate boot, liveness NOT verified")
    if alive:
        clauses.append(f"{alive} record(s) showed transcript activity after boot")
    if unusable:
        clauses.append(f"{unusable} record(s) with an unreadable started_at, "
                       "liveness NOT verified")
    if not clauses:
        clauses.append("no manager records")
    return f"revive: no pre-reboot ghosts (boot {boot_epoch}); " + "; ".join(clauses)


def dead_worker_records(records, boot_epoch, transcript_mtime=transcript_mtime_of,
                        pid_is_any_session=None) -> list[dict]:
    dead, _live = split_worker_records(records, boot_epoch, transcript_mtime, pid_is_any_session)
    return dead


def live_pid_worker_records(records, boot_epoch, transcript_mtime=transcript_mtime_of,
                            pid_is_any_session=None) -> list[dict]:
    _dead, live = split_worker_records(records, boot_epoch, transcript_mtime, pid_is_any_session)
    return live


def split_worker_records(records, boot_epoch, transcript_mtime=transcript_mtime_of,
                         pid_is_any_session=None) -> tuple[list[dict], list[dict]]:
    pid_is_any_session = (record_pid_is_any_session if pid_is_any_session is None
                          else pid_is_any_session)
    dead: list[dict] = []
    live: list[dict] = []
    for record in records or []:
        if (not isinstance(record, dict) or record.get("agent") != "worker"
                or record.get("nested")):
            continue
        sid = record.get("claude_sid")
        if not isinstance(sid, str) or not sid:
            continue
        started = record.get("started_at")
        if not _is_number(started) or started >= boot_epoch:
            continue
        mtime = transcript_mtime(record)
        if _is_number(mtime) and mtime >= boot_epoch:
            continue
        if pid_is_any_session(record):
            live.append(record)
            continue
        dead.append(record)
    return dead, live


def archive_dead_workers(records, boot_epoch, transcript_mtime=transcript_mtime_of) -> list[dict]:
    archived: list[dict] = []
    for record in dead_worker_records(records, boot_epoch, transcript_mtime):
        sid = record["claude_sid"]
        source_path, _searched = resolve_transcript(record)
        closed = _closed_record(record, RESUME_CLOSED_REASON, state.window_id_of(record),
                                str(source_path) if source_path is not None else None)
        paths.CLOSED.mkdir(parents=True, exist_ok=True)
        state.write_json_atomic(paths.CLOSED / f"{sid}.json", closed)
        (paths.ACTIVE / f"{sid}.json").unlink(missing_ok=True)
        _drop_questions_for_worker(sid)
        archived.append(record)
    return archived


def split_dead_workers(records, boot_epoch, plan) -> tuple[dict[str, list[dict]], list[dict]]:
    digested = {c.name for c in plan
                if c.verdict in (VERDICT_REVIVE, VERDICT_COVERED) and c.name}
    by_manager: dict[str, list[dict]] = {name: [] for name in digested}
    unattributed: list[dict] = []
    for record in records or []:
        if (not isinstance(record, dict) or record.get("agent") != "worker"
                or record.get("nested")):
            continue
        started = record.get("started_at")
        if _is_number(started) and started >= boot_epoch:
            continue
        parent = record.get("parent_manager_name")
        if isinstance(parent, str) and parent in by_manager:
            by_manager[parent].append(record)
        else:
            unattributed.append(record)
    return by_manager, unattributed


def _parse_event(line):
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    if not isinstance(line, str) or not line.strip():
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def _accepted_eng_text(text) -> str:
    if not isinstance(text, str):
        return ""
    cleaned = text.strip()
    if not cleaned or _LEADING_TAG.match(cleaned):
        return ""
    return cleaned


def _text_pieces(message) -> list[str]:
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    pieces = []
    for part in content:
        if (isinstance(part, dict) and part.get("type") == "text"
                and isinstance(part.get("text"), str)):
            pieces.append(part["text"])
    return pieces


def _eng_text(event) -> str:
    if event.get("isMeta"):
        return ""
    kept = [cleaned for cleaned in (_accepted_eng_text(piece)
                                    for piece in _text_pieces(event.get("message")))
            if cleaned]
    return "\n".join(kept)


def _mgr_text(event) -> str:
    kept = [piece.strip() for piece in _text_pieces(event.get("message")) if piece.strip()]
    return " ".join(kept)


def reduce_transcript(lines) -> list[Block]:
    blocks: list[Block] = []
    for line in lines or []:
        event = _parse_event(line)
        if event is None:
            continue
        kind = event.get("type")
        if kind not in _ALLOWED_EVENT_TYPES:
            continue
        who = ENG if kind == "user" else MGR
        text = _eng_text(event) if kind == "user" else _mgr_text(event)
        if not text:
            continue
        ts = event.get("timestamp")
        blocks.append(Block(ts=ts if isinstance(ts, str) else None, who=who, text=text))
    return blocks


def digest_max_bytes(env=None) -> int:
    env = os.environ if env is None else env
    raw = (env.get(DIGEST_MAX_BYTES_ENV) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DIGEST_MAX_BYTES_DEFAULT
    return value if value > 0 else DIGEST_MAX_BYTES_DEFAULT


def _boot_stamp(boot_epoch) -> str:
    try:
        return datetime.fromtimestamp(boot_epoch).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "unrenderable local time"


def _digest_size(path: Path) -> str:
    try:
        return f"{path.stat().st_size / 1024:.1f} KB"
    except OSError:
        return "size unknown"


def _digest_note(path: Path, dropped: int) -> str:
    if dropped > 0:
        return f"({_digest_size(path)}, truncated, {dropped / 1024:.1f} KB dropped)"
    return f"({_digest_size(path)})"


def _hhmmss(ts) -> str:
    if not isinstance(ts, str) or not ts:
        return "--:--:--"
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%H:%M:%S")
    except ValueError:
        return "--:--:--"


def _render_block(block: Block) -> str:
    return f"[{_hhmmss(block.ts)} {block.who}] {block.text}"


def _one_line(value, limit: int = _SUMMARY_MAX_CHARS) -> str:
    text = " ".join(str(value).split()) if value is not None else ""
    if not text:
        return "-"
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _render_worker(worker) -> list[str]:
    source_path, _searched = resolve_transcript(worker)
    return [
        f"- name: {worker.get('name') or '?'}",
        f"  sid: {worker.get('claude_sid') or '?'}",
        f"  cwd: {worker.get('cwd') or '-'}",
        f"  last_turn_at: {worker.get('last_turn_at') if worker.get('last_turn_at') is not None else '-'}",
        f"  last_summary: {_one_line(worker.get('last_summary'))}",
        f"  transcript: {source_path if source_path is not None else 'NOT RESOLVED'}",
    ]


def _render_workers_section(record, dead_workers) -> str:
    lines = ["", f"## Dead workers of {record.get('name') or '?'}", ""]
    if not dead_workers:
        lines.append("(none)")
    else:
        for worker in dead_workers:
            lines.extend(_render_worker(worker))
    return "\n".join(lines) + "\n"


def render_unattributed(workers) -> str:
    lines = ["# revive — dead workers whose manager got no digest", ""]
    if not workers:
        lines.append("(none)")
    else:
        for worker in workers:
            lines.extend(_render_worker(worker))
            lines.append(f"  parent_manager_name: {worker.get('parent_manager_name') or '-'}")
    return "\n".join(lines) + "\n"


def _render_header(record, source_path, source_bytes, reduced_bytes,
                   dropped, kept_blocks) -> str:
    name = record.get("name") or "?"
    sid = record.get("claude_sid") or "?"
    lines = [
        f"# revive digest — {name} ({normalize_domain(record)})",
        "",
        f"- manager: {name}",
        f"- domain: {normalize_domain(record)}",
        f"- sid: {sid}",
        f"- transcript: {source_path if source_path is not None else 'NOT RESOLVED'}",
        f"- source bytes: {source_bytes}",
        f"- reduced bytes: {reduced_bytes}",
    ]
    if source_path is None:
        searched = ", ".join(str(p) for p in transcript_search_paths(record))
        lines.append(f"- NO TRANSCRIPT RESOLVED — sid {sid}, "
                     f"account {record.get('account') or '?'}, searched: {searched}")
    if dropped > 0:
        lines.append(f"- TRUNCATED: dropped {dropped} bytes from the start of the "
                     f"reduced stream, {kept_blocks} block(s) kept")
    lines.extend(["", "## Stream", ""])
    return "\n".join(lines) + "\n"


def render_digest(record, blocks, dead_workers, source_path, source_bytes,
                  max_bytes=None) -> tuple[str, int]:
    max_bytes = digest_max_bytes() if max_bytes is None else max_bytes
    parts = [_render_block(block) for block in (blocks or [])]
    sizes = [len(part.encode("utf-8")) + 1 for part in parts]
    reduced_bytes = sum(sizes)
    tail = _render_workers_section(record, dead_workers)
    tail_bytes = len(tail.encode("utf-8"))
    dropped = 0
    start = 0
    while start < len(parts):
        header = _render_header(record, source_path, source_bytes, reduced_bytes,
                                dropped, len(parts) - start)
        if (len(header.encode("utf-8")) + tail_bytes
                + reduced_bytes - dropped) <= max_bytes:
            break
        dropped += sizes[start]
        start += 1
    header = _render_header(record, source_path, source_bytes, reduced_bytes,
                            dropped, len(parts) - start)
    body = "\n".join(parts[start:])
    if body:
        body += "\n"
    return header + body + tail, dropped


@dataclass
class Window:
    candidate: Candidate
    digest: Path | None
    pane: str
    text: str
    dropped: int = 0
    reason: str = "the readiness window ran out"
    resume: bool = False
    confirmed: bool = False
    delivered: bool = False
    registered: bool = False


def resume_not_confirmed_reason(sid: str, pid_is_session=None) -> str | None:
    pid_is_session = record_pid_is_session if pid_is_session is None else pid_is_session
    record = state.read_json(paths.ACTIVE / f"{sid}.json") if sid else None
    if not isinstance(record, dict):
        return f"no active record for sid {sid or '?'} — the resumed session has not registered"
    if not pid_is_session(record):
        return (f"the registered pid {record.get('pid')} of sid {sid} is not a live session — "
                "claude --resume may not have attached")
    return None


def digest_instruction(candidate: Candidate, digest: Path) -> str:
    return INSTRUCTION.format(domain=candidate.domain, name=candidate.name or "?",
                              path=digest)


def resume_instruction(candidate: Candidate, boot_epoch, worker_names) -> str:
    names = [n for n in (worker_names or []) if n]
    if names:
        workers = RESUME_WORKERS_SENTENCE.format(names=", ".join(names),
                                                reason=RESUME_CLOSED_REASON)
    else:
        workers = RESUME_NO_WORKERS_SENTENCE
    return RESUME_INSTRUCTION.format(name=candidate.name or "?", domain=candidate.domain,
                                     stamp=_boot_stamp(boot_epoch), workers=workers)


def not_ready_reason(screen_text, pane_command) -> str | None:
    command = (pane_command or "").strip()
    if not _CLAUDE_COMMAND_RE.match(command):
        return f"not Claude Code in the pane (pane_current_command={command!r})"
    if LIVE_TURN_RE.search(screen_text or ""):
        return "a turn is running"
    if not _input_is_idle(screen_text):
        return "the input is busy"
    return None


def is_ready(screen_text: str | None, pane_command: str | None) -> bool:
    return not_ready_reason(screen_text, pane_command) is None


def ready_timeout_sec(env=None) -> float:
    env = os.environ if env is None else env
    raw = (env.get(READY_TIMEOUT_ENV) or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return READY_TIMEOUT_DEFAULT
    return value if math.isfinite(value) and value >= 0 else READY_TIMEOUT_DEFAULT


def _label(candidate: Candidate) -> str:
    return f"{candidate.name or candidate.sid or '?'} ({candidate.domain})"


def _write_file(path: Path, body: str) -> None:
    digest_dir().mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _reduce_source(source_path: Path):
    try:
        source_bytes = source_path.stat().st_size
        with open(source_path, "r", encoding="utf-8", errors="replace") as handle:
            return reduce_transcript(handle), source_bytes
    except OSError:
        return None, 0


def write_candidate_digest(candidate: Candidate, dead_workers) -> tuple[Path, int]:
    record = candidate.record
    source_path, searched = resolve_transcript(record)
    blocks: list[Block] = []
    source_bytes = 0
    if source_path is not None:
        blocks, source_bytes = _reduce_source(source_path)
        if blocks is None:
            blocks, source_path = [], None
    target = digest_path(candidate.sid)
    body, dropped = render_digest(record, blocks, dead_workers,
                                  source_path, source_bytes)
    _write_file(target, body)
    if source_path is None:
        print(f"revive: WARNING no transcript resolved for {_label(candidate)}, "
              f"sid {candidate.sid or '?'}, account {record.get('account') or '?'} — "
              f"searched {', '.join(str(p) for p in searched)}; the digest at "
              f"{target} carries only its dead workers", file=sys.stderr)
    return target, dropped


async def spawn_manager_window(candidate: Candidate, resume: bool = False) -> str:
    from .spawner import spawn_worker_tab
    async with asyncio.timeout(SPAWN_TIMEOUT_SEC):
        window_id, _name = await spawn_worker_tab(
            cwd=str(paths.ensure_manager_home()),
            initial_prompt="" if resume else f"/manager {candidate.domain}",
            name=candidate.name if resume else "",
            agent="manager",
            tab_title=(f"{candidate.name or '?'} · {candidate.domain}" if resume
                       else "manager (revived)"),
            resume_sid=candidate.sid if resume else None,
            env={"CLAUDE_DOMAIN": candidate.domain},
            runtime="claude",
            extra_args=["--model", config.manager_model()],
        )
    return window_id or ""


def _report(window: Window, reason: str | None, prefix: str = "revive") -> None:
    if window.resume:
        sid = window.candidate.sid or "?"
        if reason is None:
            print(f"{prefix}: {_label(window.candidate)} — resumed on sid {sid} "
                  "(registered, live pid), notice delivered")
        elif window.confirmed:
            print(f"{prefix}: {_label(window.candidate)} — resumed on sid {sid} "
                  f"(registered, live pid) but the notice was NOT delivered — {reason}; "
                  f"type it into its window yourself: {window.text}")
        else:
            print(f"{prefix}: {_label(window.candidate)} — resume of sid {sid} NOT confirmed — "
                  f"{reason}; check its window; the notice to type yourself: {window.text}")
        return
    note = _digest_note(window.digest, window.dropped)
    if reason is None:
        print(f"{prefix}: {_label(window.candidate)} — delivered → {window.digest} "
              f"{note}")
    else:
        print(f"{prefix}: {_label(window.candidate)} — NOT delivered — {reason}; "
              f"read it yourself: {window.digest} {note}")


def _window_not_ready_reason(driver, window: Window) -> str | None:
    if not window.pane:
        return "the spawn returned no pane id"
    if window.resume and not window.registered:
        unconfirmed = resume_not_confirmed_reason(window.candidate.sid)
        if unconfirmed is not None:
            return unconfirmed
        window.confirmed = True
    screen = driver.capture_screen_ansi(window.pane)
    return not_ready_reason(screen, driver.pane_current_command(window.pane))


async def deliver(windows, *, prefix: str = "revive") -> None:
    driver = get_driver()
    pending = list(windows)
    deadline = time.monotonic() + ready_timeout_sec()
    while True:
        still: list[Window] = []
        ready: list[Window] = []
        for window in pending:
            reason = _window_not_ready_reason(driver, window)
            if reason is not None:
                window.reason = reason
                still.append(window)
            else:
                ready.append(window)
        if ready:
            await asyncio.sleep(READY_POLL_SEC)
        for window in ready:
            reason = _window_not_ready_reason(driver, window)
            if reason is not None:
                window.reason = f"{reason} on the confirming poll"
                still.append(window)
                continue
            sent = driver.send_text_checked(window.pane, window.text)
            if sent:
                window.delivered = True
            _report(window, None if sent else "tmux refused the paste", prefix=prefix)
        pending = still
        if not pending or time.monotonic() >= deadline:
            break
        await asyncio.sleep(READY_POLL_SEC)
    for window in pending:
        _report(window, window.reason, prefix=prefix)


def revive_candidates(plan) -> list[Candidate]:
    return [c for c in plan if c.verdict == VERDICT_REVIVE]


def covered_candidates(plan) -> list[Candidate]:
    return [c for c in plan if c.verdict == VERDICT_COVERED]


def _worker_names(workers) -> list[str]:
    return [w.get("name") for w in workers if isinstance(w.get("name"), str) and w.get("name")]


def _report_live_pid_workers(live) -> None:
    for record in live:
        print(f"revive: {record.get('name') or record.get('claude_sid') or '?'} NOT archived — "
              f"pid {record.get('pid')} is a live session: {_live_session_command(record)}. "
              "If that is this worker, it is not dead. If it is a different session, this "
              "record is stale and the next prune removes it — do not kill that process",
              file=sys.stderr)


async def apply_plan(plan, records, boot_epoch, dry_run: bool = False,
                     resume: bool = False) -> int:
    targets = revive_candidates(plan)
    covered = covered_candidates(plan)
    if dry_run:
        for candidate in targets:
            if resume:
                print(f"revive: --dry-run — would resume {_label(candidate)} on sid "
                      f"{candidate.sid or '?'} with `claude --resume` and tell it "
                      "about the reboot")
            else:
                print(f"revive: --dry-run — would write {digest_path(candidate.sid)} and "
                      f"open a '/manager {candidate.domain}' window for {_label(candidate)}")
        for candidate in covered:
            print(f"revive: --dry-run — would write {digest_path(candidate.sid)} for "
                  f"{_label(candidate)} and open no window")
        if resume:
            dead, live = split_worker_records(records, boot_epoch)
            print(f"revive: --dry-run — would move {len(dead)} dead worker record(s) to "
                  f"closed/ with closed_reason {RESUME_CLOSED_REASON}"
                  + (f": {', '.join(_worker_names(dead))}" if dead else ""))
            _report_live_pid_workers(live)
        return 0
    archived_names: set[str] = set()
    if resume:
        archived = archive_dead_workers(records, boot_epoch)
        archived_names = set(_worker_names(archived))
        print(f"revive: {len(archived)} dead worker record(s) moved to closed/ with "
              f"closed_reason {RESUME_CLOSED_REASON} — resume_worker finds them there"
              + (f": {', '.join(sorted(archived_names))}" if archived_names else ""))
        _report_live_pid_workers(live_pid_worker_records(records, boot_epoch))
    if not targets and not covered:
        return 0
    by_manager, unattributed = split_dead_workers(records, boot_epoch, plan)
    digests_for = list(covered) if resume else [*targets, *covered]
    digests = {candidate.sid: write_candidate_digest(
        candidate, by_manager.get(candidate.name, []))
        for candidate in digests_for}
    for candidate in covered:
        path, dropped = digests[candidate.sid]
        print(f"revive: {_label(candidate)} — its transcript → {path} "
              f"{_digest_note(path, dropped)}")
    if digests:
        reported = sum(len(by_manager.get(candidate.name, [])) for candidate in digests_for)
        print(f"revive: {reported} dead worker(s) reported inside the digests")
    if unattributed:
        orphans = unattributed_digest_path()
        _write_file(orphans, render_unattributed(unattributed))
        print(f"revive: {len(unattributed)} dead worker(s) whose manager got no "
              f"digest → {orphans}")
    windows: list[Window] = []
    rc = 0
    for candidate in targets:
        if resume:
            path, dropped = None, 0
            names = [n for n in _worker_names(by_manager.get(candidate.name, []))
                     if n in archived_names]
            text = resume_instruction(candidate, boot_epoch, names)
        else:
            path, dropped = digests[candidate.sid]
            text = digest_instruction(candidate, path)
        try:
            pane = await (spawn_manager_window(candidate, resume=True) if resume
                          else spawn_manager_window(candidate))
        except Exception as e:
            rc = 1
            if resume:
                print(f"revive: {_label(candidate)} — NOT resumed — the window could "
                      f"not be opened: {type(e).__name__}: {e}; resume it yourself: "
                      f"claude --resume {candidate.sid or '?'}")
            else:
                print(f"revive: {_label(candidate)} — NOT delivered — the window could "
                      f"not be opened: {type(e).__name__}: {e}; read it yourself: "
                      f"{path} {_digest_note(path, dropped)}")
            continue
        windows.append(Window(candidate=candidate, digest=path, pane=pane, text=text,
                              dropped=dropped, resume=resume))
    await deliver(windows)
    return rc


def attach_to_manager_session() -> int:
    socket = TmuxDriver().socket()
    base = ["tmux", "-L", socket]
    try:
        has = subprocess.run([*base, "has-session", "-t", MANAGER_SESSION],
                             capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"revive: cannot reach tmux to attach: {e}", file=sys.stderr)
        return 1
    if has.returncode != 0:
        print(f"revive: no '{MANAGER_SESSION}' tmux session to attach to",
              file=sys.stderr)
        return 1
    if os.environ.get("TMUX"):
        detail = ""
        try:
            switched = subprocess.run([*base, "switch-client", "-t", MANAGER_SESSION],
                                      capture_output=True, text=True, timeout=5,
                                      check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            detail = str(e) or type(e).__name__
        else:
            if switched.returncode != 0:
                detail = switched.stderr.strip() or f"exit {switched.returncode}"
        if detail:
            print(f"revive: switch-client failed: {detail}; the revived windows are "
                  f"open — attach with: tmux -L {socket} attach -t {MANAGER_SESSION}",
                  file=sys.stderr)
        return 0
    try:
        os.execvp("tmux", [*base, "attach-session", "-t", MANAGER_SESSION])
    except OSError as e:
        print(f"revive: failed to exec tmux attach: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dockwright revive",
        description="Open one manager window per manager that died in the last "
                    "host reboot, and hand each a digest of its predecessor's "
                    "transcript. Ghost records are reported, never touched. With "
                    "--resume, reopen each dead manager on its own transcript instead "
                    "(claude --resume), archive the dead worker records so resume_worker "
                    "finds them, and tell the manager what to do; the CHANGELOG entry "
                    "resume-after-reboot carries the exact predicates.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan; write nothing, open nothing, send nothing")
    parser.add_argument("--resume", action="store_true",
                        help="resume each dead manager on its own transcript instead of "
                             "opening a fresh one with a digest, and archive the dead worker "
                             f"records to closed/ with closed_reason {RESUME_CLOSED_REASON}")
    parser.add_argument("--no-attach", action="store_true",
                        help="leave the terminal where it is instead of attaching "
                             f"to the '{MANAGER_SESSION}' session")
    args = parser.parse_args(argv)
    try:
        boot_epoch = resolve_boot_epoch()
    except BootEpochUnavailable as e:
        print(f"revive: {e}", file=sys.stderr)
        return 3
    records = load_active_records()
    plan = build_plan(records, boot_epoch)
    print(f"revive: boot epoch {boot_epoch} ({_boot_stamp(boot_epoch)})")
    if os.environ.get("DOCKWRIGHT_BOOT_EPOCH") and not boot_epoch_plausible(boot_epoch):
        print(f"revive: WARNING boot epoch {boot_epoch} comes from DOCKWRIGHT_BOOT_EPOCH and "
              "is not plausible against the clock and uptime — every record older than it "
              "counts as dead", file=sys.stderr)
    booting = booting_managers(plan)
    for candidate in booting:
        print(f"revive: {candidate.name or candidate.sid or '?'} has not registered a "
              f"domain (pane {state.window_id_of(candidate.record) or '?'}, cwd "
              f"{candidate.record.get('cwd') or '?'}) — it may claim any domain, so "
              "revive cannot tell which ghosts it covers", file=sys.stderr)
    if booting:
        print("revive: if its /manager is still loading, re-run in a few seconds; if "
              "that session is not going to run /manager, finish or close it. Nothing "
              "was spawned and nothing was written", file=sys.stderr)
        if not args.dry_run:
            return 4
        print("revive: --dry-run — the plan below is NOT trustworthy while a manager "
              "is registering its domain")
    for candidate in plan:
        print(f"revive: {candidate.verdict} {_label(candidate)} — {candidate.reason}")
    noop = render_noop_line(plan, boot_epoch)
    if noop is not None:
        print(noop)
    ghosts = (len(covered_candidates(plan)) if args.resume
              else len(revive_candidates(plan)) + len(covered_candidates(plan)))
    if ghosts:
        print(f"revive: {ghosts} ghost record(s) left untouched — they stay in "
              "list_managers")
    if booting:
        return 4
    rc = asyncio.run(apply_plan(plan, records, boot_epoch, dry_run=args.dry_run,
                                resume=args.resume))
    if (rc == 0 and revive_candidates(plan)
            and not args.dry_run and not args.no_attach):
        return attach_to_manager_session()
    return rc
