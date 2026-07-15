#!/usr/bin/env python3
"""Render one row of the orchestrator's 2-row tmux status line.

Standalone + stdlib-only by design: invoked from dockwright.conf as
  #(python3 $HOME/.claude/dockwright/status_row.py {managers|workers})
so it must NOT depend on the dockwright package being importable from
tmux's /bin/sh #() environment. Deployed beside the conf by setup.sh.

Reads ~/.claude/dockwright/active/*.json + questions/**/*.json and prints a
single line of tmux-format text (with #[bg=..,fg=..] escapes) to stdout.

Per-state colors mirror src/dockwright/hooks.py
(MANAGER_TAB_COLOR / WORKER_TAB_COLOR_*) — keep in sync if those change. The
active (first) element of each (active,inactive) tuple is used as the chip bg.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

def _prefer_new(new: Path, legacy: Path) -> Path:
    # deprecated, one release: legacy fallback while orchestrator-era state migrates
    if new.exists():
        return new
    if legacy.exists():
        return legacy
    return new


MANAGER_COLOR = ("#aa0066", "#ffffff")
IDLE_COLOR = ("#444444", "#ffffff")
BUSY_COLOR = ("#aa8800", "#ffffff")
QUESTION_COLOR = ("#aa3300", "#ffffff")
SELECTED_COLOR = ("#0099cc", "#ffffff")  # (bg, fg) — currently-viewed window's chip; cool accent distinct from every (warm) state color
SELECTED_MARKER = "▸"


def tmux_escape(text):
    return str(text).replace("#", "##")


def _styled(text, color, selected):
    bg, fg = SELECTED_COLOR if selected else color
    style = f"bg={bg},fg={fg}" + (",bold" if selected else "")
    body = f"{SELECTED_MARKER}{text}" if selected else text
    return style, body


def chip(text, color, selected=False):
    style, body = _styled(text, color, selected)
    return f"#[{style}] {tmux_escape(body)} #[default]"


def clickable_chip(text, color, payload, selected=False):
    """A chip wrapped in a clickable tmux status range. payload None -> plain
    (non-clickable) chip, so records without a window_id degrade gracefully.
    selected -> render with the ▸ marker + bold (the currently-viewed window)."""
    if not payload:
        return chip(text, color, selected=selected)
    style, body = _styled(text, color, selected)
    return f"#[range=user|{tmux_escape(payload)}]#[{style}] {tmux_escape(body)} #[default]#[norange]"


def _switch_chip(text, color, record, selected_pane=""):
    """Clickable chip that switches the client to the record's tmux pane on click.
    window_id is a tmux pane id (%N); emitted raw (single %) because #() output is
    NOT strftime-expanded. No window_id -> non-clickable plain chip. When window_id
    equals the attached client's current pane (selected_pane), render it selected."""
    wid = record.get("window_id")
    payload = f"switch:{wid}" if wid else None
    selected = bool(wid) and wid == selected_pane
    return clickable_chip(text, color, payload, selected=selected)


def _label(record):
    return record.get("name") or record.get("funny_name") or "worker"


def _manager_label(record):
    domain = record.get("domain")
    return f"{_label(record)} · {domain}" if domain else _label(record)


def classify_worker(record, question_sids):
    if record.get("claude_sid") in question_sids:
        return "question"
    # default: anything not "processing" (incl. idle / missing / unknown) ->
    # idle, so it collapses into the 💤N count rather than expanding.
    return "processing" if record.get("state") == "processing" else "idle"


def render_managers(records, selected_pane=""):
    mgrs = [r for r in records if r.get("agent") == "manager"]
    return " ".join(_switch_chip(f"🎯 {_manager_label(r)}", MANAGER_COLOR, r, selected_pane) for r in mgrs)


def render_workers(records, question_sids, idle_expanded=False, selected_pane=""):
    workers = [r for r in records if r.get("agent") == "worker"]
    buckets = {"question": [], "processing": [], "idle": []}
    for r in workers:
        buckets[classify_worker(r, question_sids)].append(r)
    parts = []
    for r in sorted(buckets["question"], key=_label):
        parts.append(_switch_chip(f"🔧 {_label(r)}", QUESTION_COLOR, r, selected_pane))
    for r in sorted(buckets["processing"], key=_label):
        parts.append(_switch_chip(f"🔧 {_label(r)}", BUSY_COLOR, r, selected_pane))
    idle = buckets["idle"]
    if idle:
        n = len(idle)
        if idle_expanded:
            parts.append(clickable_chip(f"💤{n}▾", IDLE_COLOR, "toggle:idle"))
            for r in sorted(idle, key=_label):
                parts.append(_switch_chip(f"💤 {_label(r)}", IDLE_COLOR, r, selected_pane))
        else:
            # collapsed: the per-worker chips aren't shown, so surface "your current
            # window is one of these" by highlighting the count pill itself.
            selected_in_idle = any(
                r.get("window_id") and r.get("window_id") == selected_pane for r in idle
            )
            parts.append(clickable_chip(f"💤{n}", IDLE_COLOR, "toggle:idle", selected=selected_in_idle))
    return " ".join(parts)


def _pid_alive(pid):
    if not pid:
        return True  # no pid -> can't disprove liveness; keep the record
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True  # exists, owned by another user
    return True


def _is_visible(record):
    if record.get("nested"):
        return False
    return _pid_alive(record.get("pid"))


def _idle_expanded(orch):
    return (orch / "statusline-idle-expanded").exists()


def _tmux(*args):
    """Run a bare `tmux` command (the run-shell child's $TMUX targets the
    dockwright socket). Never raises — a dead pane id or missing binary is a
    silent no-op, mirroring the render path's never-crash contract."""
    try:
        subprocess.run(["tmux", *args], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _selected_pane():
    """Pane id (%N) the attached tmux client is currently viewing, or "" when it
    can't be determined. Runs inside tmux's #() job, where $TMUX targets the
    orchestrator socket; a bare `tmux display-message` (no -c/-t) resolves the
    most-recently-active client's current pane — the human's view in the
    single-client orchestrator. Never raises: a tmux hiccup degrades to "" =
    highlight nothing, preserving the never-crash render contract."""
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "#{pane_id}"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except Exception:
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def handle_click(payload, orch):
    if payload.startswith("switch:"):
        target = payload[len("switch:"):]
        if target:
            _tmux("switch-client", "-t", target)
    elif payload == "toggle:idle":
        flag = orch / "statusline-idle-expanded"
        try:
            if flag.exists():
                flag.unlink()
            else:
                orch.mkdir(parents=True, exist_ok=True)
                flag.touch()
        finally:
            _tmux("refresh-client", "-S")


def collect(active_dir, questions_dir):
    records = []
    if active_dir.is_dir():
        for p in sorted(active_dir.glob("*.json")):
            try:
                rec = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and _is_visible(rec):
                records.append(rec)
    question_sids = set()
    if questions_dir.is_dir():
        for p in questions_dir.rglob("*.json"):
            try:
                q = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(q, dict) and q.get("worker_sid"):
                question_sids.add(q["worker_sid"])
    return records, question_sids


def main(argv, home):
    which = argv[1] if len(argv) > 1 else "workers"
    orch = _prefer_new(home / ".claude" / "dockwright", home / ".claude" / "orchestrator")
    if which == "click":
        try:
            handle_click(argv[2] if len(argv) > 2 else "", orch)
        except Exception:
            pass
        return 0
    try:
        records, qsids = collect(orch / "active", orch / "questions")
        # Selected pane: tmux expands the status-format's #{pane_id} per-client
        # and passes it here, so this is THIS client's currently-viewed pane —
        # authoritative and client-scoped. Baking it into the #() command also
        # fixes a chip-click lag: a click switches the client via a run-shell
        # switch-client, which does not promptly re-run this job; but switching
        # to a different pane changes the command string, so tmux re-runs it
        # immediately and the highlight moves at once. Fall back to querying the
        # pane ourselves when the arg is absent — note _selected_pane() can't
        # tell clients apart, so it mis-highlights when >1 client is attached.
        selected = argv[2] if len(argv) > 2 and argv[2] else _selected_pane()
        if which == "managers":
            sys.stdout.write(render_managers(records, selected))
        else:
            sys.stdout.write(render_workers(records, qsids, _idle_expanded(orch), selected))
    except Exception:
        pass  # a status redraw must never be crashed by this script
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv, Path.home()))
