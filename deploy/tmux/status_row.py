#!/usr/bin/env python3
"""Render one row of dockwright's 2-row tmux status line.

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
import unicodedata
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

MENU_MAX_ROWS = 20      # a menu taller/wider than the client is silently NOT displayed (tmux 3.7b) — cap + explicit overflow row
MENU_ROW_CELLS = 76     # row width budget in DISPLAY CELLS (wide chars count 2); fits any realistic client
MENU_HEIGHT_OVERHEAD = 8  # 2 status rows + menu borders/title + separator + overflow row
MENU_STATE_ICON = {"question": "❓", "processing": "🔧", "idle": "💤"}


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


def _cells(text):
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _truncate_cells(text, budget):
    if _cells(text) <= budget:
        return text
    out, used = [], 0
    for c in text:
        w = 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
        if used + w > budget - 1:
            break
        out.append(c)
        used += w
    return "".join(out) + "…"


def _first_line(text):
    for line in str(text or "").splitlines():
        line = " ".join(line.split())
        if line:
            return line
    return ""


def _menu_label(record, question_sids, selected_pane):
    icon = MENU_STATE_ICON[classify_worker(record, question_sids)]
    funny = record.get("funny_name") or ""
    task = record.get("name") or ""
    who = f"{funny} · {task}" if funny and task else (funny or task or "worker")
    marker = "▸" if record.get("window_id") and record.get("window_id") == selected_pane else ""
    head = f"{marker}{icon} {who}"
    summary = _first_line(record.get("last_summary"))
    room = MENU_ROW_CELLS - _cells(head) - 3          # 3 = " — "
    if summary and room > 8:
        head = f"{head} — {_truncate_cells(summary, room)}"
    return _truncate_cells(head, MENU_ROW_CELLS)


def _resolve_scope(records, pane):
    """Manager name whose fleet the menu shows. The clicking client's viewed pane
    binds the scope: a manager's own window -> that manager; a worker's window ->
    its parent. No match -> the sole manager if there is exactly one, else None
    (= unscoped: show everything, grouped per manager)."""
    if pane:
        for r in records:
            if r.get("window_id") and r.get("window_id") == pane:
                if r.get("agent") == "manager":
                    return r.get("name")
                return r.get("parent_manager_name")
    managers = [r for r in records if r.get("agent") == "manager"]
    if len(managers) == 1:
        return managers[0].get("name")
    return None


def _bucketed(workers, question_sids):
    buckets = {"question": [], "processing": [], "idle": []}
    for r in workers:
        buckets[classify_worker(r, question_sids)].append(r)
    return [r for b in ("question", "processing", "idle") for r in sorted(buckets[b], key=_label)]


def _switch_cmd(script, wid):
    # Re-enter this script's silent click path: a menu item command runs server-side,
    # where a bare switch-client on a dead pane flashes a cmdq error at the engineer.
    # Deliberately NOT tmux_escape'd, and assumes the deploy path holds no '/#/$/" —
    # true for ~/.claude/dockwright/status_row.py and the tests' tmp copies.
    return f'run-shell \'python3 "{script}" click "switch:{wid}"\''


def build_fleet_menu(records, question_sids, scope, selected_pane="", max_rows=MENU_MAX_ROWS, script=None):
    """(title, args) for `tmux display-menu`: args is the flat item list — triples
    for items, a single '' for a separator (tmux's separator syntax)."""
    script = script or os.path.abspath(__file__)
    workers = [r for r in records if r.get("agent") == "worker"]
    if scope:
        # null parent = legacy record, visible to every manager (statusline-command.sh parity)
        workers = [w for w in workers if w.get("parent_manager_name") in (scope, None)]
    title = tmux_escape(f" {scope or 'all managers'} · {len(workers)} workers ")
    if not workers:
        return title, ["-no workers", "", ""]

    rows = []   # ("header", name) | ("worker", record)
    by_mgr = {}
    for w in workers:
        by_mgr.setdefault(w.get("parent_manager_name") or "?", []).append(w)
    if scope is None and len(by_mgr) > 1:
        for mgr in sorted(by_mgr):
            rows.append(("header", mgr))
            rows.extend(("worker", w) for w in _bucketed(by_mgr[mgr], question_sids))
    else:
        rows = [("worker", w) for w in _bucketed(workers, question_sids)]

    args, n_rows, key_n = [], 0, 0
    for i, (kind, item) in enumerate(rows):
        if n_rows >= max_rows:
            remaining = sum(1 for k, _ in rows[i:] if k == "worker")
            args.append("")   # separator: a single '' arg
            args += [f"+{remaining} more — full window tree", "w", "choose-tree -Zw"]
            break
        n_rows += 1
        if kind == "header":
            args += [f"-#[bold]{tmux_escape(str(item))}", "", ""]
            continue
        label = tmux_escape(_menu_label(item, question_sids, selected_pane))
        wid = item.get("window_id")
        if wid:
            key_n += 1
            args += [label, str(key_n) if key_n <= 9 else "", _switch_cmd(script, wid)]
        else:
            args += [f"-{label}", "", ""]
    return title, args


def show_fleet_menu(orch, client, mouse_x, pane, height):
    """Pop the fleet menu on the clicking client. Detached Popen: the CLI
    display-menu call blocks until the menu closes and must outlive this
    script (menu survives issuer exit — spike-verified)."""
    records, qsids = collect(orch / "active", orch / "questions")
    scope = _resolve_scope(records, pane)
    max_rows = MENU_MAX_ROWS
    if str(height).isdigit():
        # taller-than-client menus silently don't display; leave room for
        # status rows + borders/title + the separator/overflow rows
        max_rows = max(3, min(MENU_MAX_ROWS, int(height) - MENU_HEIGHT_OVERHEAD))
    title, items = build_fleet_menu(records, qsids, scope, pane, max_rows)
    # -M: script-issued menus are not mouse-selectable without it.
    # -O (STAYOPEN): REQUIRED — a no-button pointer-motion event (SGR code 35)
    # satisfies tmux's MOUSE_RELEASE() macro (35 & MOUSE_MASK_BUTTONS == 3), so
    # without -O the first motion event outside the box closes the menu: it
    # vanished as the engineer moved the pointer toward it (tmux 3.7b
    # menu.c:335-337). With -O, motion outside is survived, motion inside
    # hovers a row (menu.c sets md->choice), and a press chooses the hovered
    # row. Press outside the box / q / Esc still dismiss.
    cmd = ["tmux", "display-menu", "-M", "-O"]
    if client:
        cmd += ["-c", client]
    cmd += ["-x", mouse_x if str(mouse_x).isdigit() else "M", "-y", "S", "-T", title]
    cmd += items
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def handle_click(payload, orch, client="", mouse_x="", pane="", height=""):
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
    elif payload == "menu:fleet":
        show_fleet_menu(orch, client, mouse_x, pane, height)


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
            handle_click(
                argv[2] if len(argv) > 2 else "",
                orch,
                argv[3] if len(argv) > 3 else "",
                argv[4] if len(argv) > 4 else "",
                argv[5] if len(argv) > 5 else "",
                argv[6] if len(argv) > 6 else "",
            )
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
