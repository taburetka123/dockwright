"""Report-only maintenance scan: `dockwright sweep [--dry-run]`.

Prints three classes of debris with per-item evidence, modifying NOTHING:

  - Dead active/ records: active/<sid>.json whose pid is no longer alive — the
    session died without its SessionEnd hook firing (crash, force-quit, SIGHUP).
  - Orphan terminal windows: worker windows in the "claude-workers" group
    with no backing active/ record.
  - Orphan MCP docker clients/containers: sessions spawn stdio MCP servers as
    `docker run -i --rm <image>` host processes; a dead session reparents the
    client to PPID 1 and leaks the container.

Deliberately stricter than the destructive pruners (preflight_cleanup.py,
registry._prune_stale_active_records): anything whose session still has a
pending question under questions/ is never flagged — the manager can still
answer_question + resume_worker it. A future --apply must inherit these
invariants; today this module performs no destructive operation at all.

Worktree pruning is out of scope and deliberately unowned: the daily
`ticket-cleanup` launchd loop was retired 2026-06-11 (its plist had pointed at
a deleted binary since 2026-04-17, failing silently every day — arch-soundness
review A4). Manual fallback: the command named by dockwright.toml
`[hints].worktree_cleanup` (default: unset — no hint line is printed unless an
operator configures their own cleanup command). Reviving the loop is a
deliberate re-add: plist + stop-file + loops-registry row, not a path fix.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime

from . import config, paths, state
from .spawner import WORKERS_OS_WINDOW_CLASS
from .terminal import get_driver
from .state import _pid_alive, window_id_of

USAGE = "Usage: dockwright sweep [--dry-run]"
DEFAULT_MCP_IMAGES = ("crystaldba/postgres-mcp",)
MCP_IMAGES_ENV = "CLAUDE_SWEEP_MCP_IMAGES"


def _pending_question_sids() -> set[str]:
    sids: set[str] = set()
    if not paths.QUESTIONS.is_dir():
        return sids
    for q_path in paths.QUESTIONS.rglob("*.json"):
        record = state.read_json(q_path)
        if record and record.get("worker_sid"):
            sids.add(record["worker_sid"])
    return sids


def scan_dead_active_records(pending_sids: set[str]) -> list[dict]:
    findings: list[dict] = []
    if not paths.ACTIVE.is_dir():
        return findings
    for record_path in sorted(paths.ACTIVE.iterdir()):
        if record_path.suffix != ".json":
            continue
        record = state.read_json(record_path)
        if record is None:
            continue
        pid = record.get("pid")
        if not isinstance(pid, int) or _pid_alive(pid):
            continue
        if record.get("claude_sid") in pending_sids:
            continue
        findings.append({
            "path": str(record_path),
            "claude_sid": record.get("claude_sid"),
            "name": record.get("name"),
            "agent": record.get("agent"),
            "pid": pid,
            "started_at": record.get("started_at"),
            "last_turn_at": record.get("last_turn_at"),
        })
    return findings


def _terminal_ls() -> tuple[list | None, str | None]:
    return get_driver().ls_with_error()


def _protected_window_ids(pending_sids: set[str]) -> set[str]:
    protected: set[str] = set()
    for record in state.list_json_in(paths.ACTIVE):
        wid = window_id_of(record)
        if wid:
            protected.add(str(wid))
    for record in state.list_json_in(paths.CLOSED):
        if record.get("claude_sid") in pending_sids:
            wid = window_id_of(record)
            if wid:
                protected.add(str(wid))
    return protected


def scan_orphan_terminal_windows(os_windows: list, protected: set[str]) -> list[dict]:
    # _terminal_ls only validates the top level; per-element isinstance guards keep
    # a valid list with malformed elements degrading per-element, not crashing.
    orphans: list[dict] = []
    for osw in os_windows:
        if not isinstance(osw, dict) or osw.get("wm_class") != WORKERS_OS_WINDOW_CLASS:
            continue
        tabs = osw.get("tabs", [])
        if not isinstance(tabs, list):
            continue
        for tab in tabs:
            if not isinstance(tab, dict):
                continue
            windows = tab.get("windows", [])
            if not isinstance(windows, list):
                continue
            for win in windows:
                if not isinstance(win, dict):
                    continue
                wid = win.get("id")
                if wid is None or str(wid) in protected:
                    continue
                orphans.append({
                    "window_id": str(wid),
                    "tab_title": tab.get("title") or "?",
                    "cwd": win.get("cwd") or "?",
                })
    return orphans


def _mcp_images() -> tuple[str, ...]:
    raw = os.environ.get(MCP_IMAGES_ENV)
    if not raw:
        return DEFAULT_MCP_IMAGES
    images = tuple(part.strip() for part in raw.split(",") if part.strip())
    return images or DEFAULT_MCP_IMAGES


def _ps_snapshot() -> tuple[list[dict] | None, str | None]:
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,etime=,command="],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"ps failed: {e}"
    if proc.returncode != 0:
        return None, f"ps exited {proc.returncode}"
    rows: list[dict] = []
    for line in proc.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) != 4:
            continue
        pid_s, ppid_s, etime, command = parts
        if not (pid_s.isdigit() and ppid_s.isdigit()):
            continue
        rows.append({"pid": int(pid_s), "ppid": int(ppid_s),
                     "etime": etime, "command": command})
    if not rows:
        # rc=0 with nothing parseable must degrade, not read as "zero clients" —
        # the container scan would otherwise flag every container of a known
        # image off an empty snapshot. Real ps always lists at least itself.
        return None, "ps returned no parseable rows"
    return rows, None


def _docker_containers(images: tuple[str, ...]) -> tuple[list[dict] | None, str | None]:
    try:
        proc = subprocess.run(
            ["docker", "ps", "--no-trunc",
             "--format", "{{.ID}}\t{{.Image}}\t{{.RunningFor}}"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"docker ps failed: {e}"
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        return None, f"docker ps exited {proc.returncode}: {err or 'no stderr'}"
    containers: list[dict] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        container_id, image, age = parts
        if _image_matches(image, images):
            containers.append({"container_id": container_id, "image": image, "age": age})
    return containers, None


def _image_matches(image: str, images: tuple[str, ...]) -> bool:
    # Bare-name comparison: a registry-qualified report (docker.io/crystaldba/
    # postgres-mcp) does NOT match the bare default — put the qualified name in
    # CLAUDE_SWEEP_MCP_IMAGES if your daemon reports images that way.
    return any(
        image == known
        or image.startswith(f"{known}:")
        or image.startswith(f"{known}@")
        for known in images
    )


def _is_mcp_client(command: str, images: tuple[str, ...]) -> bool:
    """Deliberately looser than the literal `docker run -i --rm` (no flag
    matching) — the image list is the real constraint, and report-only false
    positives only add report lines."""
    tokens = command.split()
    if not tokens or os.path.basename(tokens[0]) != "docker":
        return False
    if "run" not in tokens:
        return False
    return any(_image_matches(tok, images) for tok in tokens)


def _looks_like_session(command: str) -> bool:
    """argv[0]'s basename only. A claude/codex-shaped token elsewhere in the
    command line (a path arg ending in /claude, a container --name claude)
    must not read as a session — it would hide a genuine orphan, and the
    preflight mirror would trust a recycled pid. Every real session's argv[0]
    is literally `claude`/`codex` or an absolute path to it (the `zsh -ic
    claude ...` spawn wrapper never matters: the claude child it forks is
    always the process actually checked or walked through)."""
    tokens = command.split()
    return bool(tokens) and os.path.basename(tokens[0]) in ("claude", "codex")


def _chain_reaches_live_session(pid: int, proc_by_pid: dict[int, dict]) -> bool:
    seen: set[int] = set()
    current = pid
    while current in proc_by_pid and current not in seen:
        seen.add(current)
        if _looks_like_session(proc_by_pid[current]["command"]):
            return True
        current = proc_by_pid[current]["ppid"]
    return False


def scan_orphan_mcp_clients(ps_rows: list[dict], images: tuple[str, ...]) -> list[dict]:
    proc_by_pid = {row["pid"]: row for row in ps_rows}
    findings: list[dict] = []
    for row in ps_rows:
        if row["ppid"] != 1 or not _is_mcp_client(row["command"], images):
            continue
        if _chain_reaches_live_session(row["pid"], proc_by_pid):
            continue
        image = next(
            (tok for tok in row["command"].split() if _image_matches(tok, images)), "?")
        findings.append({
            "pid": row["pid"],
            "ppid": row["ppid"],
            "image": image,
            "age": row["etime"],
            "command": row["command"],
        })
    return findings


def scan_leaked_mcp_containers(
    containers: list[dict],
    ps_rows: list[dict],
    images: tuple[str, ...],
) -> tuple[list[dict], list[str]]:
    """Per image: zero live docker-run clients means every running container of
    it is client-less, i.e. leaked — flag each. When clients exist, the precise
    client<->container mapping is unknowable from the host side (the docker-run
    cmdline carries no container id), so a count mismatch only earns an
    image-level ambiguity note, never individual flags."""
    clients_per_image: dict[str, int] = {known: 0 for known in images}
    for row in ps_rows:
        if not _is_mcp_client(row["command"], images):
            continue
        for known in images:
            if any(_image_matches(tok, (known,)) for tok in row["command"].split()):
                clients_per_image[known] += 1
    leaked: list[dict] = []
    notes: list[str] = []
    for known in images:
        own = [c for c in containers if _image_matches(c["image"], (known,))]
        if not own:
            continue
        # docker ps runs after the ps snapshot — a container whose client
        # started in between has no client in the snapshot and would
        # false-flag. Sub-minute RunningFor strings are all seconds-scale;
        # skip those from flagging and the ambiguity math, but say so.
        mature = [c for c in own if "second" not in c["age"].lower()]
        if len(mature) < len(own):
            notes.append(
                f"{known}: {len(own) - len(mature)} container(s) younger than "
                f"a minute ignored (may postdate the process snapshot)")
        if not mature:
            continue
        clients = clients_per_image[known]
        if clients == 0:
            leaked.extend(mature)
        elif len(mature) > clients:
            notes.append(
                f"{known}: {len(mature)} containers vs {clients} clients — "
                f"{len(mature) - clients} likely leaked, not individually flagged "
                f"(client-to-container mapping is ambiguous)")
    return leaked, notes


def _ticket_cleanup_hint() -> "str | None":
    """The worktree-pruning hint line, or None when the operator configured
    an empty command (hint suppressed)."""
    cmd = config.worktree_cleanup_hint()
    if not cmd.strip():
        return None
    return (
        "hint: worktree pruning is out of sweep's scope — run "
        f"`{cmd}` manually for that "
        "(the daily launchd loop was retired 2026-06-11)."
    )


def _fmt_ts(epoch) -> str:
    if not isinstance(epoch, (int, float)):
        return "-"
    return datetime.fromtimestamp(epoch).isoformat(sep=" ", timespec="seconds")


def format_report(
    dead: list[dict],
    orphans: list[dict] | None,
    terminal_error: str | None,
    mcp_clients: list[dict] | None,
    mcp_containers: list[dict] | None,
    mcp_notes: list[str],
    ps_error: str | None,
    docker_error: str | None,
) -> str:
    lines = ["dockwright sweep (report-only — nothing modified)", ""]
    lines.append(f"Dead active records ({len(dead)}):")
    if dead:
        for f in dead:
            lines.append(
                f"  - {f['path']}  name={f['name']}  agent={f['agent']}  "
                f"pid={f['pid']} (dead)  started_at={_fmt_ts(f['started_at'])}  "
                f"last_turn_at={_fmt_ts(f['last_turn_at'])}"
            )
    else:
        lines.append("  (none — clean)")
    lines.append("")
    if terminal_error is not None:
        lines.append(f"Orphan terminal windows: scan skipped — {terminal_error}")
        lines.append("  (partial report: the other scans are unaffected)")
    else:
        lines.append(
            f"Orphan terminal windows in '{WORKERS_OS_WINDOW_CLASS}' ({len(orphans)}):")
        if orphans:
            for w in orphans:
                lines.append(
                    f"  - window {w['window_id']}  tab={w['tab_title']!r}  "
                    f"cwd={w['cwd']}")
        else:
            lines.append("  (none — clean)")
    lines.append("")
    if ps_error is not None:
        lines.append(f"MCP docker scan skipped — {ps_error}")
        lines.append("  (partial report: client liveness is unknowable without ps)")
    else:
        lines.append(f"Orphan MCP docker clients ({len(mcp_clients)}):")
        if mcp_clients:
            for c in mcp_clients:
                lines.append(
                    f"  - pid={c['pid']}  ppid={c['ppid']}  image={c['image']}  "
                    f"age={c['age']}  cmd={c['command']}")
        else:
            lines.append("  (none — clean)")
        lines.append("")
        if docker_error is not None:
            lines.append(
                f"Leaked MCP containers: container scan skipped — {docker_error}")
        else:
            lines.append(f"Leaked MCP containers ({len(mcp_containers)}):")
            if mcp_containers:
                for c in mcp_containers:
                    lines.append(
                        f"  - container {c['container_id'][:12]}  "
                        f"image={c['image']}  age={c['age']}")
            else:
                lines.append("  (none — clean)")
            for note in mcp_notes:
                lines.append(f"  note: {note}")
    hint = _ticket_cleanup_hint()
    if hint is not None:
        lines.append("")
        lines.append(hint)
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if argv not in ([], ["--dry-run"]):
        print(USAGE, file=sys.stderr)
        return 2
    pending_sids = _pending_question_sids()
    dead = scan_dead_active_records(pending_sids)
    os_windows, terminal_error = _terminal_ls()
    orphans = None
    if terminal_error is None:
        orphans = scan_orphan_terminal_windows(
            os_windows, _protected_window_ids(pending_sids))
    images = _mcp_images()
    ps_rows, ps_error = _ps_snapshot()
    mcp_clients = None
    mcp_containers = None
    mcp_notes: list[str] = []
    docker_error = None
    if ps_error is None:
        mcp_clients = scan_orphan_mcp_clients(ps_rows, images)
        containers, docker_error = _docker_containers(images)
        if docker_error is None:
            mcp_containers, mcp_notes = scan_leaked_mcp_containers(
                containers, ps_rows, images)
    print(format_report(dead, orphans, terminal_error, mcp_clients,
                        mcp_containers, mcp_notes, ps_error, docker_error))
    return 0
