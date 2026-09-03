#!/usr/bin/env python3
import datetime
import json
import os
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

def _prefer_new(new: Path, legacy: Path) -> Path:
    if new.exists():
        return new
    if legacy.exists():
        return legacy
    return new


MANAGER_COLOR = ("#aa0066", "#ffffff")
UNREAD_COLOR = ("#aa3300", "#ffffff")
UNREAD_MARKER = "\u2709\ufe0e"
IDLE_COLOR = ("#444444", "#ffffff")
BUSY_COLOR = ("#aa8800", "#ffffff")
QUESTION_COLOR = ("#aa3300", "#ffffff")
SELECTED_COLOR = ("#0099cc", "#ffffff")
SELECTED_MARKER = "▸"

MENU_MAX_ROWS = 20
MENU_ROW_CELLS = 76
MENU_HEIGHT_OVERHEAD = 8
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
    if not payload:
        return chip(text, color, selected=selected)
    style, body = _styled(text, color, selected)
    return f"#[range=user|{tmux_escape(payload)}]#[{style}] {tmux_escape(body)} #[default]#[norange]"


def _switch_chip(text, color, record, selected_pane=""):
    wid = record.get("window_id")
    payload = f"switch:{wid}" if wid else None
    selected = bool(wid) and wid == selected_pane
    return clickable_chip(text, color, payload, selected=selected)


def _label(record):
    return record.get("name") or record.get("funny_name") or "worker"


INF = float("inf")
ACTIVITY_FIELDS = ("last_turn_at", "processing_since", "tasked_at", "started_at")


def _as_epoch(value):
    try:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            value = float(value)
            return value if value == value and value not in (INF, -INF) else None
        if not isinstance(value, str):
            return None
        parsed = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.timestamp()
    except Exception:
        return None


def _activity_at(record):
    best = None
    for field in ACTIVITY_FIELDS:
        ts = _as_epoch(record.get(field))
        if ts is not None and (best is None or ts > best):
            best = ts
    return best


def _freshest_first(record):
    ts = _activity_at(record)
    if ts is None:
        return (1, 0.0, _label(record))
    return (0, -ts, _label(record))


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


def _is_live(record, now=None):
    try:
        path = record.get("transcript_path")
        if not isinstance(path, str) or not path:
            return False
        mtime = Path(path).stat().st_mtime
        if now is None:
            now = time.time()
        return now - mtime < _turn_end_grace_sec()
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def classify_worker(record, question_sids, now=None):
    if record.get("claude_sid") in question_sids:
        return "question"
    if record.get("state") == "processing":
        return "processing"
    if _is_live(record, now):
        return "processing"
    if _is_delegating(record, now):
        return "processing"
    return "idle"


def _signature(record):
    ts = record.get("last_turn_at")
    summary = record.get("last_summary")
    if not ts and not summary:
        return ""
    return f"{str(ts).strip()}\x00{str(summary).strip()}"


def _mark_path(orch, record):
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
    try:
        path.with_name(path.name + ".err").unlink()
    except Exception:
        pass


def _log_mark_failure(path, exc):
    try:
        path.with_name(path.name + ".err").write_text(
            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
            f"{type(exc).__name__}: {exc}\n")
    except Exception:
        pass


def _live_pane_ids():
    try:
        out = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
                             capture_output=True, text=True, timeout=2, check=False)
    except Exception:
        return None
    if out.returncode != 0:
        return set() if "no server" in (out.stderr or "").lower() else None
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def _unread(record, selected_pane, orch, resolve_live_panes=None):
    path = _mark_path(orch, record)
    signature = _signature(record)
    if path is None or not signature:
        return False
    wid = record.get("window_id")
    if not wid:
        return False
    if wid == selected_pane:
        if _read_mark(path) != signature:
            _write_mark(path, signature)
        return False
    if _read_mark(path) == signature:
        return False
    panes = (resolve_live_panes or _live_pane_ids)()
    return panes is None or wid in panes


def render_managers(records, selected_pane="", orch=None):
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
            for r in sorted(idle, key=_freshest_first):
                parts.append(_switch_chip(f"💤 {_label(r)}", IDLE_COLOR, r, selected_pane))
        else:
            selected_in_idle = any(
                r.get("window_id") and r.get("window_id") == selected_pane for r in idle
            )
            parts.append(clickable_chip(f"💤{n}", IDLE_COLOR, "toggle:idle", selected=selected_in_idle))
    return " ".join(parts)


def _pid_alive(pid):
    if not pid:
        return True
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True
    return True


def _is_visible(record):
    if record.get("nested"):
        return False
    return _pid_alive(record.get("pid"))


def _idle_expanded(orch):
    return (orch / "statusline-idle-expanded").exists()


def _tmux(*args):
    try:
        subprocess.run(["tmux", *args], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _selected_pane():
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
    room = MENU_ROW_CELLS - _cells(head) - 3
    if summary and room > 8:
        head = f"{head} — {_truncate_cells(summary, room)}"
    return _truncate_cells(head, MENU_ROW_CELLS)


def _resolve_scope(records, pane):
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
    return (sorted(buckets["question"], key=_label)
            + sorted(buckets["processing"], key=_label)
            + sorted(buckets["idle"], key=_freshest_first))


def _switch_cmd(script, wid):
    return f'run-shell \'python3 "{script}" click "switch:{wid}"\''


def build_fleet_menu(records, question_sids, scope, selected_pane="", max_rows=MENU_MAX_ROWS, script=None):
    script = script or os.path.abspath(__file__)
    workers = [r for r in records if r.get("agent") == "worker"]
    if scope:
        workers = [w for w in workers if w.get("parent_manager_name") in (scope, None)]
    title = tmux_escape(f" {scope or 'all managers'} · {len(workers)} workers ")
    if not workers:
        return title, ["-no workers", "", ""]

    rows = []
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
            args.append("")
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
    records, qsids = collect(orch / "active", orch / "questions")
    scope = _resolve_scope(records, pane)
    max_rows = MENU_MAX_ROWS
    if str(height).isdigit():
        max_rows = max(3, min(MENU_MAX_ROWS, int(height) - MENU_HEIGHT_OVERHEAD))
    title, items = build_fleet_menu(records, qsids, scope, pane, max_rows)
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
        selected = argv[2] if len(argv) > 2 and argv[2] else _selected_pane()
        if which == "managers":
            sys.stdout.write(render_managers(records, selected, orch))
        else:
            sys.stdout.write(render_workers(records, qsids, _idle_expanded(orch), selected))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv, Path.home()))
