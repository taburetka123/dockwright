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
import time
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
UNREAD_COLOR = ("#aa3300", "#ffffff")  # manager wrote something unseen; same vocabulary as a worker's pending question
# Carried as well as the color: _styled discards the color on a SELECTED chip.
# VS15 (text presentation) pins the glyph to ONE cell, which is what tmux counts
# it as — without it an emoji-presentation terminal draws two, and every
# #[range=user|…] click boundary to the right of a lit chip drifts a column.
UNREAD_MARKER = "\u2709\ufe0e"
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


EPISODE_GRACE_SEC_DEFAULT = 900
EPISODE_GRACE_ENV = "CLAUDE_ORCH_EPISODE_GRACE_SEC"
TURN_END_GRACE_SEC_DEFAULT = 120
TURN_END_GRACE_ENV = "CLAUDE_ORCH_TURN_END_GRACE_SEC"


def _turn_end_grace_sec():
    try:
        value = int(os.environ.get(TURN_END_GRACE_ENV, str(TURN_END_GRACE_SEC_DEFAULT)))
    except ValueError:
        return TURN_END_GRACE_SEC_DEFAULT
    return value if value >= 0 else TURN_END_GRACE_SEC_DEFAULT


def _episode_grace_sec():
    try:
        value = int(os.environ.get(EPISODE_GRACE_ENV, ""))
    except ValueError:
        value = EPISODE_GRACE_SEC_DEFAULT
    if value <= 0:
        value = EPISODE_GRACE_SEC_DEFAULT
    return max(value, _turn_end_grace_sec())


def _is_delegating(record, now=None):
    try:
        if (record.get("runtime") or "claude") != "claude":
            return False
        sid = record.get("claude_sid")
        transcript_path = record.get("transcript_path")
        if not sid or not transcript_path or not isinstance(transcript_path, str):
            return False
        log = Path(transcript_path)
        newest = 0.0
        for entry in (log.parent / sid / "subagents").glob("agent-*.jsonl"):
            try:
                newest = max(newest, entry.stat().st_mtime)
            except OSError:
                continue
        if newest <= 0:
            return False
        if now is None:
            now = time.time()
        return newest > log.stat().st_mtime and now - newest < _episode_grace_sec()
    except (OSError, TypeError, ValueError):
        return False


def classify_worker(record, question_sids, now=None):
    if record.get("claude_sid") in question_sids:
        return "question"
    if record.get("state") == "processing":
        return "processing"
    if _is_delegating(record, now):
        return "processing"
    return "idle"


def _signature(record):
    """Opaque identity of the manager's last message to the engineer.

    ⛔ Compared with != ONLY. Never parsed, never compared to a clock:
    last_turn_at is an ISO-UTC string lifted verbatim out of the transcript
    (transcript.py:118) while any clock-derived mark would be a local epoch, and
    comparing the two scales yields a chip that is always-on or never-on — off
    by hours and invisible to the eye. != also sidesteps ordering: ISO strings
    of differing sub-second precision do not sort ("…33.039Z" < "…33Z").

    Both fields, because hooks.py:689-692 writes them under SEPARATE conditions
    — a transcript event carrying no timestamp moves last_summary alone. Either
    one moving means he has something unseen. Each side is str()'d and stripped
    so the file round-trips exactly."""
    ts = record.get("last_turn_at")
    summary = record.get("last_summary")
    if not ts and not summary:
        return ""
    return f"{str(ts).strip()}\x00{str(summary).strip()}"


def _mark_path(orch, record):
    """Mark file for this manager, or None when there is nowhere to put one.
    The key is sanitised: a '/' in it would escape `orch`, and the mkdir below
    would then create a `.read-..` DIRECTORY that preflight's file-only GC can
    never sweep."""
    key = record.get("claude_sid") or record.get("name")
    if orch is None or not key:
        return None
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(key))
    return orch / f".read-{safe}" if safe.strip("_") else None


def _read_mark(path):
    try:
        return path.read_text().strip()
    except Exception:
        return ""


def _write_mark(path, signature):
    """Write-then-replace, on a tmp name unique to THIS process: a target-derived
    tmp name lets two tmux clients' render jobs interleave on one file and
    publish a torn mark (src/dockwright/state.py:43-48 documents the same
    defect). The `.read-<key>.<pid>.tmp` form still matches preflight's
    `.read-*` sweep, which a dot-prefixed form would not.

    Swallows everything: a failed mark write must never blank the status row,
    and the failure mode it degrades to — the chip stays lit — is the safe one."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(signature)
        os.replace(tmp, path)
        _clear_mark_failure(path)
    except Exception as exc:
        _log_mark_failure(path, exc)
        try:
            tmp.unlink()
        except Exception:
            pass


def _clear_mark_failure(path):
    """A stale reason beside a working mark reads as a live fault."""
    try:
        path.with_name(path.name + ".err").unlink()
    except Exception:
        pass


def _log_mark_failure(path, exc):
    """A failed mark write degrades to "the chip stays lit". That is the safe
    direction, but it says nothing about WHY, and this render path has no
    stderr — tmux discards a #() job's. Leave the reason beside the mark it
    belongs to, one file per manager so two failures in one pass do not
    overwrite each other, and overwritten rather than appended so neither can
    grow.

    ⚠️ Covers the failures where the DIRECTORY is still writable — a bad value
    in the record (the observed case: a lone surrogate in last_summary), or the
    mark path being a directory. It cannot cover the filesystem class, because
    an unwritable or missing orch dir defeats this write too; there the cause is
    the directory itself and is visible by looking at it.

    `.read-<key>.err` cannot collide with any mark: the sanitiser strips "."
    from every key, so no mark filename ever contains one — pinned by
    test_sanitised_key_never_contains_a_dot. preflight's `.read-*` sweep
    collects it like the marks."""
    try:
        path.with_name(path.name + ".err").write_text(
            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
            f"{type(exc).__name__}: {exc}\n")
    except Exception:
        pass


def _live_pane_ids():
    """Pane ids alive on this tmux server; None when tmux cannot answer, set()
    when no server is running.

    ⛔ None and set() are NOT interchangeable. None means "I cannot tell" and
    _unread keeps lighting on it; returning set() there instead would make
    `wid in panes` False for every manager and darken every unread chip
    permanently — the failure this feature exists to remove. No consumer-side
    test can catch that, because they stub this function; the producer tests
    (test_live_pane_ids_*) are what pin it.

    Mirrors preflight_cleanup._live_pane_ids, itself a deliberate stdlib-only
    duplicate of registry._live_pane_ids — this script may not import the
    package. Two deliberate deltas from that sibling: no `-L <socket>`, because
    this runs inside tmux's #() job where $TMUX already targets the running
    server (and the socket name is not available here); and a 2s rather than 5s
    timeout, because a status row must redraw."""
    try:
        out = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
                             capture_output=True, text=True, timeout=2, check=False)
    except Exception:
        return None
    if out.returncode != 0:
        return set() if "no server" in (out.stderr or "").lower() else None
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def _unread(record, selected_pane, orch, resolve_live_panes=None):
    """True when this manager wrote something the engineer has not seen.

    STAMPS the read mark as a side effect when his client's current pane is this
    manager's pane — which is what makes opening the tab clear the chip, and
    watching the tab never light it (the stamp lands in the same render pass as
    the draw). A pane switch re-runs this job at once rather than at the next
    5s tick, because #{pane_id} is baked into the #() command string
    (dockwright.conf:89-102), so the chip clears immediately.

    Missing mark -> UNREAD, deliberately: a spurious light costs one glance, a
    missed message costs the whole feature."""
    path = _mark_path(orch, record)
    signature = _signature(record)
    if path is None or not signature:
        return False
    wid = record.get("window_id")
    # THE PROPERTY: never light a chip that nothing could ever clear. A chip
    # that stays lit forever is worse than a missed message — it trains him to
    # ignore the whole row. Two shapes break the clearing path, and guarding
    # only the first leaves the second:
    #   - no window_id at all. hooks.py:472 falls back to "" when neither
    #     CLAUDE_ITERM_SID nor the driver yields a pane id, and :620 stores that
    #     (:488/:526 overwrite only when truthy, so "" persists).
    #   - a window_id naming a pane that has since died. #{pane_id} only ever
    #     names a LIVE pane, so the equality below can never match it, and
    #     handle_click's switch-client fails silently. preflight_cleanup.py
    #     :269-285 keeps such a record on purpose when its pid was recycled.
    # The falsy check must come FIRST: selected_pane is "" whenever tmux cannot
    # resolve the client's pane, and "" == "" would otherwise stamp a paneless
    # record as read — silently eating the message. Same reason _switch_chip
    # guards its own comparison with bool(wid).
    if not wid:
        return False
    if wid == selected_pane:
        if _read_mark(path) != signature:
            _write_mark(path, signature)
        return False
    if _read_mark(path) == signature:
        return False
    # tmux unanswerable (None) -> keep lighting: absence of evidence is not a
    # dead pane, and the loud direction is the safe one.
    # Resolve it here when the caller did not: the guard must not be opt-in, or
    # a future caller that omits the argument lights dead-pane chips again.
    # render_managers passes a memoised resolver so the pass costs one lookup.
    panes = (resolve_live_panes or _live_pane_ids)()
    return panes is None or wid in panes


def render_managers(records, selected_pane="", orch=None):
    """orch=None -> no mark I/O at all (the row renders exactly as before).

    The live-pane lookup is lazy and memoised for the pass: it costs a
    subprocess only on a tick where a chip would otherwise light, and nothing at
    all once everything is read."""
    mgrs = [r for r in records if r.get("agent") == "manager"]
    cached = []

    def resolve_live_panes():
        if not cached:
            cached.append(_live_pane_ids())
        return cached[0]

    parts = []
    for r in mgrs:
        unread = _unread(r, selected_pane, orch, resolve_live_panes)
        label = f"{UNREAD_MARKER} 🎯 {_manager_label(r)}" if unread else f"🎯 {_manager_label(r)}"
        parts.append(_switch_chip(label, UNREAD_COLOR if unread else MANAGER_COLOR, r, selected_pane))
    return " ".join(parts)


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
            sys.stdout.write(render_managers(records, selected, orch))
        else:
            sys.stdout.write(render_workers(records, qsids, _idle_expanded(orch), selected))
    except Exception:
        pass  # a status redraw must never be crashed by this script
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv, Path.home()))
