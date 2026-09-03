import importlib.util
import json
import os
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "tmux" / "status_row.py"


def _load():
    spec = importlib.util.spec_from_file_location("status_row", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sr = _load()


def test_tmux_escape_doubles_hash():
    assert sr.tmux_escape("a#b") == "a##b"


def test_chip_wraps_with_style_and_reset():
    assert sr.chip("hi", ("#aa8800", "#ffffff")) == "#[bg=#aa8800,fg=#ffffff] hi #[default]"


def test_clickable_chip_wraps_range_and_color():
    out = sr.clickable_chip("hi", ("#aa8800", "#ffffff"), "switch:%91")
    assert out == "#[range=user|switch:%91]#[bg=#aa8800,fg=#ffffff] hi #[default]#[norange]"


def test_clickable_chip_none_payload_falls_back_to_plain_chip():
    assert sr.clickable_chip("hi", ("#aa8800", "#ffffff"), None) == sr.chip("hi", ("#aa8800", "#ffffff"))


def test_switch_chip_builds_raw_single_percent_payload():
    rec = {"name": "w", "window_id": "%91"}
    out = sr._switch_chip("🔧 w", sr.BUSY_COLOR, rec)
    assert "#[range=user|switch:%91]" in out
    assert "%%91" not in out
    assert "🔧 w" in out


def test_switch_chip_missing_window_id_is_non_clickable():
    out = sr._switch_chip("🔧 w", sr.BUSY_COLOR, {"name": "w"})
    assert "range=user" not in out
    assert out == sr.chip("🔧 w", sr.BUSY_COLOR)


def test_render_managers_chips_are_clickable_when_window_id_present():
    out = sr.render_managers([{"agent": "manager", "name": "boss", "window_id": "%5"}])
    assert "#[range=user|switch:%5]" in out and "🎯 boss" in out


def test_render_workers_busy_chip_clickable():
    recs = [{"agent": "worker", "name": "busy", "state": "processing", "claude_sid": "b", "window_id": "%7"}]
    out = sr.render_workers(recs, set())
    assert "#[range=user|switch:%7]" in out and "🔧 busy" in out


def test_idle_collapsed_chip_carries_toggle_payload():
    recs = [{"agent": "worker", "name": "z", "state": "idle", "claude_sid": "z", "window_id": "%9"}]
    out = sr.render_workers(recs, set(), idle_expanded=False)
    assert "#[range=user|toggle:idle]" in out and "💤1" in out
    assert "switch:%9" not in out


def test_idle_expanded_shows_header_and_clickable_members():
    recs = [{"agent": "worker", "name": "z", "state": "idle", "claude_sid": "z", "window_id": "%9"}]
    out = sr.render_workers(recs, set(), idle_expanded=True)
    assert "#[range=user|toggle:idle]" in out and "💤1▾" in out
    assert "#[range=user|switch:%9]" in out and "💤 z" in out


def test_idle_expanded_default_is_false_signature_compatible():
    recs = [{"agent": "worker", "name": "z", "state": "idle", "claude_sid": "z"}]
    assert "💤1" in sr.render_workers(recs, set())


def test_handle_click_switch_calls_switch_client(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sr, "_tmux", lambda *a: calls.append(a))
    sr.handle_click("switch:%91", tmp_path)
    assert calls == [("switch-client", "-t", "%91")]


def test_handle_click_toggle_creates_then_removes_flag(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sr, "_tmux", lambda *a: calls.append(a))
    flag = tmp_path / "statusline-idle-expanded"
    sr.handle_click("toggle:idle", tmp_path)
    assert flag.exists()
    sr.handle_click("toggle:idle", tmp_path)
    assert not flag.exists()
    assert calls == [("refresh-client", "-S"), ("refresh-client", "-S")]


def test_handle_click_unknown_or_empty_is_noop(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sr, "_tmux", lambda *a: calls.append(a))
    sr.handle_click("", tmp_path)
    sr.handle_click("switch:", tmp_path)
    sr.handle_click("bogus", tmp_path)
    assert calls == []


def test_tmux_swallows_nonzero_and_errors(monkeypatch):
    def boom(*a, **k):
        raise OSError("no tmux")
    monkeypatch.setattr(sr.subprocess, "run", boom)
    sr._tmux("switch-client", "-t", "%dead")


def test_main_click_dispatches(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(sr, "handle_click", lambda payload, orch, *a: seen.append((payload, orch)))
    sr.main(["status_row.py", "click", "switch:%5"], tmp_path)
    assert seen and seen[0][0] == "switch:%5"
    assert seen[0][1] == tmp_path / ".claude" / "dockwright"


def test_classify_question_beats_state():
    rec = {"claude_sid": "s1", "state": "processing"}
    assert sr.classify_worker(rec, {"s1"}) == "question"


def test_classify_processing():
    assert sr.classify_worker({"claude_sid": "s2", "state": "processing"}, set()) == "processing"


def test_classify_idle_and_unknown_default_to_idle():
    assert sr.classify_worker({"claude_sid": "s3", "state": "idle"}, set()) == "idle"
    assert sr.classify_worker({"claude_sid": "s4", "state": "weird"}, set()) == "idle"
    assert sr.classify_worker({"claude_sid": "s5"}, set()) == "idle"


def test_render_workers_groups_idle_expands_busy_and_question():
    recs = [
        {"agent": "worker", "name": "alpha",  "state": "processing", "claude_sid": "a"},
        {"agent": "worker", "name": "bravo",  "state": "idle",       "claude_sid": "b"},
        {"agent": "worker", "name": "charlie","state": "idle",       "claude_sid": "c"},
        {"agent": "worker", "name": "delta",  "state": "processing", "claude_sid": "d"},
    ]
    out = sr.render_workers(recs, {"d"})
    assert out.index("#aa3300") < out.index("#aa8800") < out.index("#444444")
    assert "🔧 delta" in out and "🔧 alpha" in out
    assert "💤2" in out
    assert "🔧 bravo" not in out and "🔧 charlie" not in out


def test_render_workers_empty_is_empty_string():
    assert sr.render_workers([], set()) == ""


def test_render_managers_lists_each_pink():
    recs = [
        {"agent": "manager", "name": "boss"},
        {"agent": "worker",  "name": "w1", "state": "idle", "claude_sid": "x"},
    ]
    out = sr.render_managers(recs)
    assert "🎯 boss" in out and "#aa0066" in out
    assert "w1" not in out


def test_render_managers_shows_domain_after_name():
    recs = [{"agent": "manager", "name": "mighty-demon", "domain": "general"}]
    out = sr.render_managers(recs)
    assert "🎯 mighty-demon · general" in out


def test_render_managers_omits_separator_when_domain_absent():
    recs = [{"agent": "manager", "name": "mighty-demon"}]
    out = sr.render_managers(recs)
    assert "🎯 mighty-demon" in out
    assert " · " not in out


def test_render_managers_selected_with_domain_still_marked_and_bold():
    recs = [{"agent": "manager", "name": "mighty-demon", "domain": "general", "window_id": "%5"}]
    out = sr.render_managers(recs, selected_pane="%5")
    assert "▸🎯 mighty-demon · general" in out and ",bold]" in out


def test_label_prefers_name_then_funny():
    assert sr._label({"name": "task-x", "funny_name": "calm-koala"}) == "task-x"
    assert sr._label({"funny_name": "calm-koala"}) == "calm-koala"
    assert sr._label({}) == "worker"


def _write(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj))


def test_collect_filters_nested_and_dead_pid(tmp_path):
    active = tmp_path / "active"
    _write(active / "live.json",   {"agent": "worker", "name": "live",  "state": "idle", "claude_sid": "live", "pid": os.getpid()})
    _write(active / "dead.json",   {"agent": "worker", "name": "dead",  "state": "idle", "claude_sid": "dead", "pid": 2 ** 30})
    _write(active / "nested.json", {"agent": "worker", "name": "nest",  "state": "idle", "claude_sid": "nest", "pid": os.getpid(), "nested": True})
    _write(active / "nopid.json",  {"agent": "worker", "name": "nopid", "state": "idle", "claude_sid": "nopid"})
    records, qsids = sr.collect(active, tmp_path / "questions")
    names = {r["name"] for r in records}
    assert names == {"live", "nopid"}
    assert qsids == set()


def test_collect_reads_question_sids_recursively(tmp_path):
    active = tmp_path / "active"
    _write(active / "w.json", {"agent": "worker", "name": "w", "state": "idle", "claude_sid": "wsid", "pid": os.getpid()})
    q = tmp_path / "questions"
    _write(q / "boss" / "q1.json", {"worker_sid": "wsid", "question": "?"})
    records, qsids = sr.collect(active, q)
    assert "wsid" in qsids


def test_collect_skips_malformed_json(tmp_path):
    active = tmp_path / "active"
    active.mkdir(parents=True)
    (active / "bad.json").write_text("{not json")
    _write(active / "ok.json", {"agent": "worker", "name": "ok", "state": "idle", "claude_sid": "ok", "pid": os.getpid()})
    records, _ = sr.collect(active, tmp_path / "questions")
    assert {r["name"] for r in records} == {"ok"}


def test_main_workers_writes_grouped_row(tmp_path, capsys):
    home = tmp_path
    orch = home / ".claude" / "orchestrator"
    _write(orch / "active" / "b.json", {"agent": "worker", "name": "busy", "state": "processing", "claude_sid": "b", "pid": os.getpid()})
    _write(orch / "active" / "i.json", {"agent": "worker", "name": "rest", "state": "idle",       "claude_sid": "i", "pid": os.getpid()})
    sr.main(["status_row.py", "workers"], home)
    out = capsys.readouterr().out
    assert "🔧 busy" in out and "💤1" in out


def test_main_unknown_arg_defaults_to_workers(tmp_path, capsys):
    home = tmp_path
    (home / ".claude" / "orchestrator" / "active").mkdir(parents=True)
    sr.main(["status_row.py"], home)
    assert capsys.readouterr().out == ""


def _boom_selected_pane():
    raise AssertionError("_selected_pane() must not be called when the pane is passed as argv")


def test_main_workers_uses_argv_selected_pane(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sr, "_selected_pane", _boom_selected_pane)
    home = tmp_path
    orch = home / ".claude" / "orchestrator"
    _write(orch / "active" / "s.json", {"agent": "worker", "name": "sel", "state": "processing", "claude_sid": "s", "pid": os.getpid(), "window_id": "%7"})
    _write(orch / "active" / "o.json", {"agent": "worker", "name": "oth", "state": "processing", "claude_sid": "o", "pid": os.getpid(), "window_id": "%8"})
    sr.main(["status_row.py", "workers", "%7"], home)
    out = capsys.readouterr().out
    assert "▸🔧 sel" in out and "▸🔧 oth" not in out


def test_main_managers_uses_argv_selected_pane(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sr, "_selected_pane", _boom_selected_pane)
    home = tmp_path
    orch = home / ".claude" / "orchestrator"
    _write(orch / "active" / "m.json", {"agent": "manager", "name": "boss", "pid": os.getpid(), "window_id": "%5"})
    sr.main(["status_row.py", "managers", "%5"], home)
    assert "▸🎯 boss" in capsys.readouterr().out


def test_main_falls_back_to_selected_pane_when_arg_absent(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sr, "_selected_pane", lambda: "%5")
    home = tmp_path
    orch = home / ".claude" / "orchestrator"
    _write(orch / "active" / "m.json", {"agent": "manager", "name": "boss", "pid": os.getpid(), "window_id": "%5"})
    sr.main(["status_row.py", "managers"], home)
    assert "▸🎯 boss" in capsys.readouterr().out


def test_main_falls_back_to_selected_pane_when_arg_empty(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sr, "_selected_pane", lambda: "%5")
    home = tmp_path
    orch = home / ".claude" / "orchestrator"
    _write(orch / "active" / "m.json", {"agent": "manager", "name": "boss", "pid": os.getpid(), "window_id": "%5"})
    sr.main(["status_row.py", "managers", ""], home)
    assert "▸🎯 boss" in capsys.readouterr().out


def test_main_click_still_reads_argv2_as_payload(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(sr, "handle_click", lambda payload, orch, *a: seen.append(payload))
    sr.main(["status_row.py", "click", "switch:%5"], tmp_path)
    assert seen == ["switch:%5"]


def test_main_prefers_dockwright_home_over_legacy(tmp_path, capsys):
    home = tmp_path
    _write(home / ".claude" / "dockwright" / "active" / "m.json",
           {"agent": "manager", "name": "newboss", "pid": os.getpid(), "window_id": "%5"})
    _write(home / ".claude" / "orchestrator" / "active" / "m.json",
           {"agent": "manager", "name": "oldboss", "pid": os.getpid(), "window_id": "%5"})
    sr.main(["status_row.py", "managers", "%5"], home)
    out = capsys.readouterr().out
    assert "newboss" in out and "oldboss" not in out


import fcntl
import pty
import re
import select
import shutil
import struct
import subprocess
import termios
import time

import pytest


def _tmux_version():
    exe = shutil.which("tmux")
    if exe is None:
        return None
    try:
        out = subprocess.run([exe, "-V"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


_TMUX_VER = _tmux_version()

requires_menu_tmux = pytest.mark.skipif(
    _TMUX_VER is not None and _TMUX_VER < (3, 5),
    reason=f"fleet menu needs tmux >= 3.5 for `display-menu -M` "
           f"(found {'.'.join(map(str, _TMUX_VER)) if _TMUX_VER else 'none'})")


def _capture(sock, session, secs=8):
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm-256color"
        os.execvp("tmux", ["tmux", "-L", sock, "attach", "-t", session])
    buf = b""
    deadline = time.time() + secs
    os.set_blocking(fd, False)
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.3)
        if r:
            try:
                c = os.read(fd, 65536)
            except OSError:
                break
            if not c:
                break
            buf += c
    os.kill(pid, 9)
    return re.sub(r"\x1b\[[0-9;]*m", "", buf.decode("utf-8", "replace"))


@pytest.mark.real_tmux
def test_live_render_two_rows(tmp_path, monkeypatch, real_tmux):
    home = tmp_path
    orch = home / ".claude" / "dockwright"
    (orch / "active").mkdir(parents=True)
    (orch / "questions").mkdir(parents=True)
    shutil.copy(_SCRIPT, orch / "status_row.py")
    _write(orch / "active" / "m.json", {"agent": "manager", "name": "boss", "pid": os.getpid()})
    _write(orch / "active" / "b.json", {"agent": "worker", "name": "busyone", "state": "processing", "claude_sid": "b", "pid": os.getpid()})
    _write(orch / "active" / "i.json", {"agent": "worker", "name": "rest", "state": "idle", "claude_sid": "i", "pid": os.getpid()})
    conf = tmp_path / "test.conf"
    conf.write_text(
        'set -g status 2\n'
        'set -g status-interval 1\n'
        'set -g \'status-format[0]\' "MGR #(python3 $HOME/.claude/dockwright/status_row.py managers)"\n'
        'set -g \'status-format[1]\' "WRK #(python3 $HOME/.claude/dockwright/status_row.py workers)"\n'
    )
    monkeypatch.setenv("HOME", str(home))
    sock = real_tmux
    subprocess.run(["tmux", "-L", sock, "-f", str(conf), "new-session", "-d", "-s", "wrk", "-x", "200", "-y", "50"], check=True)
    try:
        text = _capture(sock, "wrk")
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)
    assert "boss" in text
    assert "busyone" in text
    assert "💤1" in text


@pytest.mark.real_tmux
def test_click_switches_cross_session(tmp_path, monkeypatch, real_tmux):
    ROWS, COLS = 30, 120
    home = tmp_path
    orch = home / ".claude" / "dockwright"
    (orch / "active").mkdir(parents=True)
    (orch / "questions").mkdir(parents=True)
    shutil.copy(_SCRIPT, orch / "status_row.py")
    monkeypatch.setenv("HOME", str(home))
    sock = real_tmux
    payload_file = tmp_path / "payload.out"

    conf = tmp_path / "t.conf"
    conf.write_text(
        "set -g mouse on\n"
        "set -g status 2\n"
        "set -g status-interval 1\n"
        "set -g 'status-format[0]' \"managers\"\n"
        "set -g 'status-format[1]' \"#(python3 $HOME/.claude/dockwright/status_row.py workers)\"\n"
        "unbind -n MouseDown1Status\n"
        f"bind -n MouseUp1Status run-shell 'printf %s \"#{{mouse_status_range}}\" >> {payload_file}; "
        "python3 $HOME/.claude/dockwright/status_row.py click \"#{mouse_status_range}\"'\n"
    )
    subprocess.run(["tmux", "-L", sock, "-f", str(conf), "new-session", "-d", "-s", "alpha", "-x", str(COLS), "-y", str(ROWS)], check=True)
    subprocess.run(["tmux", "-L", sock, "new-session", "-d", "-s", "beta", "-x", str(COLS), "-y", str(ROWS)], check=True)
    beta_pane = subprocess.run(
        ["tmux", "-L", sock, "display-message", "-p", "-t", "beta:0", "#{pane_id}"],
        capture_output=True, text=True).stdout.strip()
    _write(orch / "active" / "w.json",
           {"agent": "worker", "name": "wkr", "state": "processing", "claude_sid": "w",
            "pid": os.getpid(), "window_id": beta_pane})

    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
        os.environ["TERM"] = "xterm-256color"
        os.environ.pop("TMUX", None); os.environ.pop("TMUX_PANE", None)
        os.execvp("tmux", ["tmux", "-L", sock, "attach", "-t", "alpha"])
        os._exit(127)
    os.set_blocking(fd, False)

    def drain(secs):
        end = time.time() + secs
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.2)
            if r:
                try:
                    if not os.read(fd, 65536):
                        return
                except OSError:
                    return

    try:
        drain(2.5)
        assert subprocess.run(["tmux", "-L", sock, "list-clients", "-F", "#{session_name}"],
                              capture_output=True, text=True).stdout.strip() == "alpha"

        def click(c, r):
            os.write(fd, ("\x1b[<0;%d;%dM" % (c, r)).encode()); drain(0.3)
            os.write(fd, ("\x1b[<0;%d;%dm" % (c, r)).encode()); drain(0.6)

        click(7, ROWS)
        drain(0.6)

        captured = payload_file.read_text().strip() if payload_file.exists() else ""
        switched = subprocess.run(["tmux", "-L", sock, "list-clients", "-F", "#{session_name}"],
                                  capture_output=True, text=True).stdout.strip()
        assert captured == f"switch:{beta_pane}", f"payload was {captured!r}"
        assert switched == "beta", f"client session was {switched!r}"
    finally:
        os.kill(pid, 9)


def test_chip_selected_adds_marker_and_bold():
    out = sr.chip("hi", ("#aa8800", "#ffffff"), selected=True)
    assert out == "#[bg=#0099cc,fg=#ffffff,bold] ▸hi #[default]"


def test_chip_unselected_unchanged():
    assert sr.chip("hi", ("#aa8800", "#ffffff")) == "#[bg=#aa8800,fg=#ffffff] hi #[default]"


def test_clickable_chip_selected_marks_inside_range():
    out = sr.clickable_chip("hi", ("#aa8800", "#ffffff"), "switch:%91", selected=True)
    assert out == "#[range=user|switch:%91]#[bg=#0099cc,fg=#ffffff,bold] ▸hi #[default]#[norange]"


def test_clickable_chip_selected_none_payload_falls_back_to_selected_plain_chip():
    assert sr.clickable_chip("hi", sr.BUSY_COLOR, None, selected=True) == sr.chip("hi", sr.BUSY_COLOR, selected=True)


def test_switch_chip_selected_when_window_id_matches_selected_pane():
    rec = {"name": "w", "window_id": "%91"}
    out = sr._switch_chip("🔧 w", sr.BUSY_COLOR, rec, "%91")
    assert "▸🔧 w" in out and ",bold]" in out
    assert "#[range=user|switch:%91]" in out


def test_switch_chip_not_selected_when_pane_differs():
    out = sr._switch_chip("🔧 w", sr.BUSY_COLOR, {"name": "w", "window_id": "%91"}, "%2")
    assert "▸" not in out and ",bold]" not in out


def test_switch_chip_empty_selected_pane_never_highlights():
    out = sr._switch_chip("🔧 w", sr.BUSY_COLOR, {"name": "w", "window_id": "%91"}, "")
    assert "▸" not in out


def test_switch_chip_empty_window_id_never_highlights_even_if_pane_empty():
    out = sr._switch_chip("🔧 w", sr.BUSY_COLOR, {"name": "w"}, "")
    assert "▸" not in out


def test_render_managers_highlights_only_matching_manager():
    recs = [
        {"agent": "manager", "name": "boss", "window_id": "%5"},
        {"agent": "manager", "name": "other", "window_id": "%6"},
    ]
    out = sr.render_managers(recs, selected_pane="%5")
    assert "▸🎯 boss" in out
    assert "▸🎯 other" not in out and "🎯 other" in out


def test_render_workers_highlights_matching_busy_chip():
    recs = [
        {"agent": "worker", "name": "sel", "state": "processing", "claude_sid": "s", "window_id": "%7"},
        {"agent": "worker", "name": "oth", "state": "processing", "claude_sid": "o", "window_id": "%8"},
    ]
    out = sr.render_workers(recs, set(), selected_pane="%7")
    assert "▸🔧 sel" in out
    assert "▸🔧 oth" not in out and "🔧 oth" in out


def test_render_workers_highlights_expanded_idle_member():
    recs = [{"agent": "worker", "name": "z", "state": "idle", "claude_sid": "z", "window_id": "%9"}]
    out = sr.render_workers(recs, set(), idle_expanded=True, selected_pane="%9")
    assert "▸💤 z" in out


def test_render_workers_collapsed_idle_pill_highlighted_when_selected_in_idle():
    recs = [
        {"agent": "worker", "name": "a", "state": "idle", "claude_sid": "a", "window_id": "%9"},
        {"agent": "worker", "name": "b", "state": "idle", "claude_sid": "b", "window_id": "%10"},
    ]
    out = sr.render_workers(recs, set(), idle_expanded=False, selected_pane="%10")
    assert "▸💤2" in out and "toggle:idle" in out


def test_render_workers_collapsed_idle_pill_plain_when_selected_not_in_idle():
    recs = [{"agent": "worker", "name": "a", "state": "idle", "claude_sid": "a", "window_id": "%9"}]
    out = sr.render_workers(recs, set(), idle_expanded=False, selected_pane="%999")
    assert "▸💤1" not in out and "💤1" in out


def test_selected_chip_uses_selected_color_not_state_color():
    out = sr.chip("hi", sr.BUSY_COLOR, selected=True)
    assert sr.SELECTED_COLOR[0] in out
    assert sr.BUSY_COLOR[0] not in out


def test_unselected_chip_keeps_state_color_and_no_selected_color():
    out = sr.chip("hi", sr.BUSY_COLOR)
    assert sr.BUSY_COLOR[0] in out
    assert sr.SELECTED_COLOR[0] not in out


def test_render_workers_selected_chip_is_recolored():
    recs = [
        {"agent": "worker", "name": "sel", "state": "processing", "claude_sid": "s", "window_id": "%7"},
        {"agent": "worker", "name": "oth", "state": "processing", "claude_sid": "o", "window_id": "%8"},
    ]
    out = sr.render_workers(recs, set(), selected_pane="%7")
    assert sr.SELECTED_COLOR[0] in out
    assert sr.BUSY_COLOR[0] in out


def test_render_workers_collapsed_idle_pill_recolored_when_selected_in_idle():
    recs = [
        {"agent": "worker", "name": "a", "state": "idle", "claude_sid": "a", "window_id": "%9"},
        {"agent": "worker", "name": "b", "state": "idle", "claude_sid": "b", "window_id": "%10"},
    ]
    out = sr.render_workers(recs, set(), idle_expanded=False, selected_pane="%10")
    assert "▸💤2" in out and sr.SELECTED_COLOR[0] in out


def test_selected_pane_returns_pane_on_success(monkeypatch):
    monkeypatch.setattr(sr.subprocess, "run",
                        lambda *a, **k: sr.subprocess.CompletedProcess(a, 0, stdout="%42\n", stderr=""))
    assert sr._selected_pane() == "%42"


def test_selected_pane_empty_on_nonzero(monkeypatch):
    monkeypatch.setattr(sr.subprocess, "run",
                        lambda *a, **k: sr.subprocess.CompletedProcess(a, 1, stdout="", stderr="x"))
    assert sr._selected_pane() == ""


def test_selected_pane_empty_on_exception(monkeypatch):
    def boom(*a, **k):
        raise OSError("no tmux")
    monkeypatch.setattr(sr.subprocess, "run", boom)
    assert sr._selected_pane() == ""


def test_bg_colors_match_hooks_constants():
    from dockwright.hooks import (
        MANAGER_TAB_COLOR, WORKER_TAB_COLOR_IDLE,
        WORKER_TAB_COLOR_BUSY, WORKER_TAB_COLOR_QUESTION,
    )
    assert sr.MANAGER_COLOR[0] == MANAGER_TAB_COLOR[0]
    assert sr.IDLE_COLOR[0] == WORKER_TAB_COLOR_IDLE[0]
    assert sr.BUSY_COLOR[0] == WORKER_TAB_COLOR_BUSY[0]
    assert sr.QUESTION_COLOR[0] == WORKER_TAB_COLOR_QUESTION[0]


@pytest.mark.real_tmux
def test_live_render_highlights_selected_window(tmp_path, monkeypatch, real_tmux):
    home = tmp_path
    orch = home / ".claude" / "dockwright"
    (orch / "active").mkdir(parents=True)
    (orch / "questions").mkdir(parents=True)
    shutil.copy(_SCRIPT, orch / "status_row.py")
    conf = tmp_path / "test.conf"
    conf.write_text(
        'set -g status 2\n'
        'set -g status-interval 1\n'
        'set -g \'status-format[0]\' "MGR #(python3 $HOME/.claude/dockwright/status_row.py managers)"\n'
        'set -g \'status-format[1]\' "WRK #(python3 $HOME/.claude/dockwright/status_row.py workers)"\n'
    )
    monkeypatch.setenv("HOME", str(home))
    sock = real_tmux
    subprocess.run(["tmux", "-L", sock, "-f", str(conf), "new-session", "-d", "-s", "wrk", "-x", "200", "-y", "50"], check=True)
    pane = subprocess.run(["tmux", "-L", sock, "display-message", "-p", "-t", "wrk:0", "#{pane_id}"],
                          capture_output=True, text=True).stdout.strip()
    _write(orch / "active" / "m.json", {"agent": "manager", "name": "bossmgr", "pid": os.getpid(), "window_id": pane})
    _write(orch / "active" / "s.json", {"agent": "worker", "name": "selwkr", "state": "processing", "claude_sid": "s", "pid": os.getpid(), "window_id": pane})
    _write(orch / "active" / "o.json", {"agent": "worker", "name": "othwkr", "state": "processing", "claude_sid": "o", "pid": os.getpid(), "window_id": "%999"})
    try:
        text = _capture(sock, "wrk")
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)
    assert "▸🎯 bossmgr" in text
    assert "▸🔧 selwkr" in text
    assert "▸🔧 othwkr" not in text
    assert "🔧 othwkr" in text


def _attach_pty_client(sock, session, rows=30, cols=120):
    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        os.environ["TERM"] = "xterm-256color"
        os.environ.pop("TMUX", None)
        os.environ.pop("TMUX_PANE", None)
        os.execvp("tmux", ["tmux", "-L", sock, "attach", "-t", session])
        os._exit(127)
    os.set_blocking(fd, False)
    return pid, fd


def _logging_wrapper(tmp_path, orch, log):
    w = tmp_path / "wrap.sh"
    w.write_text(
        "#!/bin/sh\n"
        f'printf "%s %s\\n" "$1" "$2" >> "{log}"\n'
        f'exec python3 "{orch}/status_row.py" "$1" "$2"\n'
    )
    w.chmod(0o755)
    return w


def _drain(fd, secs=0.0):
    end = time.time() + secs
    while True:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try:
                if not os.read(fd, 65536):
                    return
            except OSError:
                return
        if time.time() >= end:
            return


@pytest.mark.real_tmux
def test_live_chip_click_moves_highlight_without_interval_wait(tmp_path, monkeypatch, real_tmux):
    ROWS, COLS = 30, 120
    home = tmp_path
    orch = home / ".claude" / "orchestrator"
    (orch / "active").mkdir(parents=True)
    (orch / "questions").mkdir(parents=True)
    shutil.copy(_SCRIPT, orch / "status_row.py")
    log = tmp_path / "runs.log"
    wrap = _logging_wrapper(tmp_path, orch, log)
    script = orch / "status_row.py"
    conf = tmp_path / "t.conf"
    conf.write_text(
        "set -g mouse on\n"
        "set -g status 2\n"
        "set -g status-interval 30\n"
        f"set -g 'status-format[0]' \"#(sh {wrap} managers #{{pane_id}})\"\n"
        f"set -g 'status-format[1]' \"#(sh {wrap} workers #{{pane_id}})\"\n"
        "unbind -n MouseDown1Status\n"
        f"bind -n MouseUp1Status run-shell 'python3 {script} click \"#{{mouse_status_range}}\"'\n"
    )
    monkeypatch.setenv("HOME", str(home))
    sock = real_tmux
    subprocess.run(["tmux", "-L", sock, "-f", str(conf), "new-session", "-d", "-s", "alpha", "-x", str(COLS), "-y", str(ROWS)], check=True)
    subprocess.run(["tmux", "-L", sock, "new-session", "-d", "-s", "beta", "-x", str(COLS), "-y", str(ROWS)], check=True)
    beta_pane = subprocess.run(["tmux", "-L", sock, "display-message", "-p", "-t", "beta:0", "#{pane_id}"], capture_output=True, text=True).stdout.strip()
    _write(orch / "active" / "w.json",
           {"agent": "worker", "name": "wkr", "state": "processing", "claude_sid": "w", "pid": os.getpid(), "window_id": beta_pane})

    pid, fd = _attach_pty_client(sock, "alpha", ROWS, COLS)
    try:
        _drain(fd, 3.0)
        assert subprocess.run(["tmux", "-L", sock, "list-clients", "-F", "#{session_name}"],
                              capture_output=True, text=True).stdout.strip() == "alpha"
        n_before = len(log.read_text().splitlines()) if log.exists() else 0
        t0 = time.time()
        os.write(fd, ("\x1b[<0;7;%dM" % ROWS).encode())
        os.write(fd, ("\x1b[<0;7;%dm" % ROWS).encode())
        reran = False
        while time.time() - t0 < 4:
            _drain(fd, 0.1)
            new = (log.read_text().splitlines() if log.exists() else [])[n_before:]
            if any(beta_pane in l for l in new):
                reran = True
                break
        switched = subprocess.run(["tmux", "-L", sock, "list-clients", "-F", "#{session_name}"],
                                  capture_output=True, text=True).stdout.strip()
    finally:
        os.kill(pid, 9)
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)

    assert switched == "beta", f"chip click did not switch the client; session={switched!r}"
    assert reran, (
        f"highlight #() did not re-run with the clicked pane {beta_pane} within 4s of the chip click "
        f"(status-interval is 30s) — the chip-click highlight lag regressed. log={log.read_text()!r}"
    )


@pytest.mark.real_tmux
def test_live_highlight_is_client_scoped(tmp_path, monkeypatch, real_tmux):
    home = tmp_path
    orch = home / ".claude" / "orchestrator"
    (orch / "active").mkdir(parents=True)
    (orch / "questions").mkdir(parents=True)
    shutil.copy(_SCRIPT, orch / "status_row.py")
    log = tmp_path / "runs.log"
    wrap = _logging_wrapper(tmp_path, orch, log)
    conf = tmp_path / "t.conf"
    conf.write_text(
        "set -g status 2\n"
        "set -g status-interval 1\n"
        "set -g 'status-format[0]' \"MGR\"\n"
        f"set -g 'status-format[1]' \"#(sh {wrap} workers #{{pane_id}})\"\n"
    )
    monkeypatch.setenv("HOME", str(home))
    sock = real_tmux
    subprocess.run(["tmux", "-L", sock, "-f", str(conf), "new-session", "-d", "-s", "A", "-x", "200", "-y", "50"], check=True)
    subprocess.run(["tmux", "-L", sock, "new-session", "-d", "-s", "B", "-x", "200", "-y", "50"], check=True)
    pa = subprocess.run(["tmux", "-L", sock, "display-message", "-p", "-t", "A:0", "#{pane_id}"], capture_output=True, text=True).stdout.strip()
    pb = subprocess.run(["tmux", "-L", sock, "display-message", "-p", "-t", "B:0", "#{pane_id}"], capture_output=True, text=True).stdout.strip()

    pidA, fA = _attach_pty_client(sock, "A")
    pidB, fB = _attach_pty_client(sock, "B")
    panes = set()
    try:
        end = time.time() + 6
        while time.time() < end:
            _drain(fA, 0.05)
            _drain(fB, 0.05)
            panes = {l.split()[-1] for l in log.read_text().splitlines() if l.strip()} if log.exists() else set()
            if pa in panes and pb in panes:
                break
    finally:
        os.kill(pidA, 9)
        os.kill(pidB, 9)
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)

    assert pa in panes and pb in panes, (
        f"each client's bar must resolve its own pane; saw {sorted(panes)}, expected both A={pa} and B={pb}. "
        f"Without #{{pane_id}} per-client, the bars cannot resolve distinct panes."
    )


def test_cells_counts_narrow_and_wide():
    assert sr._cells("αβγδεζ") == 6
    assert sr._cells("🔧") == 2
    assert sr._cells("ab") == 2


def test_truncate_cells_exact_budget_unchanged():
    text = "abcdefgh"
    assert sr._truncate_cells(text, sr._cells(text)) == text


def test_truncate_cells_over_budget_ends_with_ellipsis_within_budget():
    text = "a" * 20
    out = sr._truncate_cells(text, 10)
    assert out.endswith("…")
    assert sr._cells(out) <= 10


def test_first_line_picks_first_nonempty_and_collapses_whitespace():
    assert sr._first_line("  \n  hello   world  \nsecond line") == "hello world"


def test_first_line_none_or_empty_is_empty_string():
    assert sr._first_line(None) == ""
    assert sr._first_line("") == ""


def test_menu_label_icon_question_beats_state():
    rec = {"funny_name": "calm-koala", "name": "task-x", "state": "processing", "claude_sid": "s1"}
    label = sr._menu_label(rec, {"s1"}, "")
    assert label.startswith("❓")


def test_menu_label_icon_processing_and_idle():
    proc = {"name": "w", "state": "processing", "claude_sid": "p1"}
    idle = {"name": "w", "state": "idle", "claude_sid": "i1"}
    assert sr._menu_label(proc, set(), "").startswith("🔧")
    assert sr._menu_label(idle, set(), "").startswith("💤")


def test_menu_label_funny_and_task_joined():
    rec = {"funny_name": "calm-koala", "name": "task-x", "state": "idle", "claude_sid": "s2"}
    label = sr._menu_label(rec, set(), "")
    assert "calm-koala · task-x" in label


def test_menu_label_funny_only_degrades():
    rec = {"funny_name": "calm-koala", "state": "idle", "claude_sid": "s3"}
    label = sr._menu_label(rec, set(), "")
    assert "calm-koala" in label and "·" not in label


def test_menu_label_task_only_degrades():
    rec = {"name": "task-x", "state": "idle", "claude_sid": "s4"}
    label = sr._menu_label(rec, set(), "")
    assert "task-x" in label and "·" not in label


def test_menu_label_marker_only_when_selected():
    rec = {"name": "w", "window_id": "%7", "state": "idle", "claude_sid": "s5"}
    assert sr._menu_label(rec, set(), "%7").startswith("▸")
    assert not sr._menu_label(rec, set(), "%8").startswith("▸")


def test_menu_label_summary_appended_after_dash():
    rec = {"name": "w", "state": "idle", "claude_sid": "s6", "last_summary": "doing a thing"}
    label = sr._menu_label(rec, set(), "")
    assert "— doing a thing" in label


def test_menu_label_no_summary_no_dash():
    rec = {"name": "w", "state": "idle", "claude_sid": "s7"}
    label = sr._menu_label(rec, set(), "")
    assert "—" not in label


def test_menu_label_long_summary_truncated_within_budget():
    rec = {"name": "w", "state": "idle", "claude_sid": "s8", "last_summary": "x" * 200}
    label = sr._menu_label(rec, set(), "")
    assert sr._cells(label) <= sr.MENU_ROW_CELLS
    assert "…" in label


def test_resolve_scope_manager_pane_returns_its_name():
    recs = [{"agent": "manager", "name": "boss", "window_id": "%5"}]
    assert sr._resolve_scope(recs, "%5") == "boss"


def test_resolve_scope_worker_pane_returns_parent():
    recs = [{"agent": "worker", "name": "w", "window_id": "%7", "parent_manager_name": "boss"}]
    assert sr._resolve_scope(recs, "%7") == "boss"


def test_resolve_scope_worker_pane_null_parent_is_none():
    recs = [{"agent": "worker", "name": "w", "window_id": "%7", "parent_manager_name": None}]
    assert sr._resolve_scope(recs, "%7") is None


def test_resolve_scope_unknown_pane_one_manager_falls_back_to_it():
    recs = [{"agent": "manager", "name": "boss", "window_id": "%5"}]
    assert sr._resolve_scope(recs, "%999") == "boss"


def test_resolve_scope_unknown_pane_two_managers_is_none():
    recs = [
        {"agent": "manager", "name": "boss", "window_id": "%5"},
        {"agent": "manager", "name": "other", "window_id": "%6"},
    ]
    assert sr._resolve_scope(recs, "%999") is None


def test_resolve_scope_empty_pane_one_manager_falls_back_to_it():
    recs = [{"agent": "manager", "name": "boss", "window_id": "%5"}]
    assert sr._resolve_scope(recs, "") == "boss"


def test_build_fleet_menu_scoped_keeps_own_and_null_parent_drops_peers():
    recs = [
        {"agent": "worker", "name": "mine", "state": "idle", "claude_sid": "m", "window_id": "%1", "parent_manager_name": "boss"},
        {"agent": "worker", "name": "legacy", "state": "idle", "claude_sid": "l", "window_id": "%2", "parent_manager_name": None},
        {"agent": "worker", "name": "theirs", "state": "idle", "claude_sid": "t", "window_id": "%3", "parent_manager_name": "other"},
    ]
    _, args = sr.build_fleet_menu(recs, set(), "boss")
    joined = " ".join(args)
    assert "mine" in joined
    assert "legacy" in joined
    assert "theirs" not in joined


def test_build_fleet_menu_title_carries_scope_and_count():
    recs = [{"agent": "worker", "name": "w", "state": "idle", "claude_sid": "w", "window_id": "%1", "parent_manager_name": "boss"}]
    title, _ = sr.build_fleet_menu(recs, set(), "boss")
    assert title == " boss · 1 workers "


def test_build_fleet_menu_unscoped_title_says_all_managers():
    recs = [{"agent": "worker", "name": "w", "state": "idle", "claude_sid": "w", "window_id": "%1"}]
    title, _ = sr.build_fleet_menu(recs, set(), None)
    assert title == " all managers · 1 workers "


def test_build_fleet_menu_empty_returns_disabled_row():
    title, args = sr.build_fleet_menu([], set(), "boss")
    assert args == ["-no workers", "", ""]


def test_build_fleet_menu_orders_question_then_processing_then_idle_alpha_within():
    recs = [
        {"agent": "worker", "name": "zulu",   "state": "idle",       "claude_sid": "z", "window_id": "%1"},
        {"agent": "worker", "name": "mike",   "state": "idle",       "claude_sid": "m", "window_id": "%2"},
        {"agent": "worker", "name": "delta",  "state": "processing", "claude_sid": "d", "window_id": "%3"},
        {"agent": "worker", "name": "alpha",  "state": "processing", "claude_sid": "a", "window_id": "%4"},
        {"agent": "worker", "name": "quebec", "state": "idle",       "claude_sid": "q", "window_id": "%5"},
    ]
    qsids = {"q"}
    _, args = sr.build_fleet_menu(recs, qsids, None)
    labels = args[0::3]
    positions = {name: next(i for i, l in enumerate(labels) if name in l)
                 for name in ("quebec", "alpha", "delta", "mike", "zulu")}
    assert positions["quebec"] < positions["alpha"] < positions["delta"] < positions["mike"] < positions["zulu"]


def test_build_fleet_menu_digit_keys_skip_disabled_rows():
    recs = [{"agent": "worker", "name": "nowin", "state": "idle", "claude_sid": "n"}]
    recs += [{"agent": "worker", "name": f"w{i}", "state": "idle", "claude_sid": f"w{i}", "window_id": f"%{i}"} for i in range(3)]
    _, args = sr.build_fleet_menu(recs, set(), None)
    keys = args[1::3]
    assert sorted(k for k in keys if k) == ["1", "2", "3"]


def test_build_fleet_menu_no_window_id_is_disabled_row_empty_command():
    recs = [{"agent": "worker", "name": "nowin", "state": "idle", "claude_sid": "n"}]
    _, args = sr.build_fleet_menu(recs, set(), None)
    label, key, cmd = args[0:3]
    assert label.startswith("-")
    assert key == ""
    assert cmd == ""


def test_build_fleet_menu_escapes_hash_in_labels_and_title():
    recs = [{"agent": "worker", "name": "task#1", "funny_name": "fun#name", "state": "idle",
             "claude_sid": "e", "window_id": "%1", "last_summary": "do #{thing}"}]
    title, args = sr.build_fleet_menu(recs, set(), "sc#pe")
    assert "sc##pe" in title
    label = args[0]
    assert "task##1" in label
    assert "fun##name" in label
    assert "##{thing}" in label


def test_build_fleet_menu_item_command_embeds_script_path():
    recs = [{"agent": "worker", "name": "w", "state": "idle", "claude_sid": "w", "window_id": "%42"}]
    _, args = sr.build_fleet_menu(recs, set(), None, script="/opt/status_row.py")
    cmd = args[2]
    assert cmd == 'run-shell \'python3 "/opt/status_row.py" click "switch:%42"\''


def test_build_fleet_menu_empty_keys_past_ninth_selectable():
    recs = [{"agent": "worker", "name": f"w{i:02d}", "state": "idle", "claude_sid": f"s{i}", "window_id": f"%{i}"} for i in range(12)]
    _, args = sr.build_fleet_menu(recs, set(), None)
    keys = args[1::3]
    assert keys[:9] == [str(n) for n in range(1, 10)]
    assert keys[9:12] == ["", "", ""]


def test_build_fleet_menu_overflow_caps_and_adds_more_row():
    recs = [{"agent": "worker", "name": f"w{i:02d}", "state": "idle", "claude_sid": f"s{i}", "window_id": f"%{i}"} for i in range(25)]
    _, args = sr.build_fleet_menu(recs, set(), None, max_rows=20)
    assert len(args) == 20 * 3 + 1 + 3
    assert args[60] == ""
    assert args[61] == "+5 more — full window tree"
    assert args[62] == "w"
    assert args[63] == "choose-tree -Zw"


def test_build_fleet_menu_respects_custom_max_rows():
    recs = [{"agent": "worker", "name": f"w{i}", "state": "idle", "claude_sid": f"s{i}", "window_id": f"%{i}"} for i in range(10)]
    _, args = sr.build_fleet_menu(recs, set(), None, max_rows=5)
    assert len(args) == 5 * 3 + 1 + 3


def test_build_fleet_menu_unscoped_multi_manager_groups_under_bold_headers():
    recs = [
        {"agent": "worker", "name": "a1", "state": "idle", "claude_sid": "a1", "window_id": "%1", "parent_manager_name": "alpha"},
        {"agent": "worker", "name": "b1", "state": "idle", "claude_sid": "b1", "window_id": "%2", "parent_manager_name": "beta"},
    ]
    _, args = sr.build_fleet_menu(recs, set(), None)
    labels = args[0::3]
    assert any("-#[bold]alpha" in l for l in labels)
    assert any("-#[bold]beta" in l for l in labels)
    alpha_idx = next(i for i, l in enumerate(labels) if "-#[bold]alpha" in l)
    a1_idx = next(i for i, l in enumerate(labels) if "a1" in l)
    beta_idx = next(i for i, l in enumerate(labels) if "-#[bold]beta" in l)
    b1_idx = next(i for i, l in enumerate(labels) if "b1" in l)
    assert alpha_idx < a1_idx < beta_idx < b1_idx


def test_build_fleet_menu_headers_count_toward_row_cap():
    recs = [
        {"agent": "worker", "name": "a1", "state": "idle", "claude_sid": "a1", "window_id": "%1", "parent_manager_name": "alpha"},
        {"agent": "worker", "name": "b1", "state": "idle", "claude_sid": "b1", "window_id": "%2", "parent_manager_name": "beta"},
    ]
    _, args = sr.build_fleet_menu(recs, set(), None, max_rows=2)
    assert len(args) == 2 * 3 + 1 + 3
    assert args[7] == "+1 more — full window tree"


def test_handle_click_menu_fleet_builds_display_menu_command(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sr.subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
    orch = tmp_path
    (orch / "active").mkdir(parents=True)
    _write(orch / "active" / "m.json", {"agent": "manager", "name": "boss", "pid": os.getpid(), "window_id": "%1"})
    sr.handle_click("menu:fleet", orch)
    assert calls
    assert calls[0][:4] == ["tmux", "display-menu", "-M", "-O"]


def test_handle_click_menu_fleet_client_flag_present_only_when_nonempty(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sr.subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
    orch = tmp_path
    (orch / "active").mkdir(parents=True)
    sr.handle_click("menu:fleet", orch, client="/dev/ttys001")
    sr.handle_click("menu:fleet", orch, client="")
    assert "-c" in calls[0] and "/dev/ttys001" in calls[0]
    assert "-c" not in calls[1]


def test_handle_click_menu_fleet_mouse_x_numeric_vs_fallback(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sr.subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
    orch = tmp_path
    (orch / "active").mkdir(parents=True)
    sr.handle_click("menu:fleet", orch, mouse_x="42")
    sr.handle_click("menu:fleet", orch, mouse_x="")
    sr.handle_click("menu:fleet", orch, mouse_x="abc")
    for cmd in calls:
        assert cmd[cmd.index("-y") + 1] == "S"
    assert calls[0][calls[0].index("-x") + 1] == "42"
    assert calls[1][calls[1].index("-x") + 1] == "M"
    assert calls[2][calls[2].index("-x") + 1] == "M"


def test_handle_click_menu_fleet_title_escaped_present(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sr.subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
    orch = tmp_path
    (orch / "active").mkdir(parents=True)
    sr.handle_click("menu:fleet", orch)
    cmd = calls[0]
    assert "-T" in cmd
    title = cmd[cmd.index("-T") + 1]
    assert "all managers" in title


def test_handle_click_menu_fleet_height_caps_rows(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sr.subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
    orch = tmp_path
    (orch / "active").mkdir(parents=True)
    for i in range(10):
        _write(orch / "active" / f"w{i}.json",
               {"agent": "worker", "name": f"w{i}", "state": "idle", "claude_sid": f"s{i}", "pid": os.getpid(), "window_id": f"%{i}"})

    def n_item_rows(cmd):
        items = cmd[cmd.index("-T") + 2:]
        n, i = 0, 0
        while i < len(items) and items[i] != "":
            n += 1
            i += 3
        return n

    sr.handle_click("menu:fleet", orch, height="12")
    sr.handle_click("menu:fleet", orch, height="")
    sr.handle_click("menu:fleet", orch, height="abc")
    assert n_item_rows(calls[0]) == 4
    assert n_item_rows(calls[1]) == 10
    assert n_item_rows(calls[2]) == 10


def test_handle_click_menu_fleet_popen_raises_is_swallowed(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise OSError("no tmux")
    monkeypatch.setattr(sr.subprocess, "Popen", boom)
    orch = tmp_path
    (orch / "active").mkdir(parents=True)
    sr.handle_click("menu:fleet", orch)


def test_render_workers_no_leading_fleet_chip():
    recs = [{"agent": "worker", "name": "w", "state": "idle", "claude_sid": "w"}]
    out = sr.render_workers(recs, set())
    assert "🤖" not in out
    assert out.startswith("#[range=user|toggle:idle]")


def test_show_fleet_menu_excludes_nested_records(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sr.subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
    orch = tmp_path
    _write(orch / "active" / "nested.json",
           {"agent": "worker", "name": "hidden", "state": "idle", "claude_sid": "h", "pid": os.getpid(), "nested": True, "window_id": "%1"})
    _write(orch / "active" / "visible.json",
           {"agent": "worker", "name": "shown", "state": "idle", "claude_sid": "v", "pid": os.getpid(), "window_id": "%2"})
    sr.handle_click("menu:fleet", orch)
    joined = " ".join(calls[0])
    assert "shown" in joined
    assert "hidden" not in joined


def test_main_click_passes_extra_argv_through(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(sr, "handle_click",
                        lambda payload, orch, client, mouse_x, pane, height: seen.append((payload, client, mouse_x, pane, height)))
    sr.main(["status_row.py", "click", "menu:fleet", "/dev/ttys001", "42", "%7", "30"], tmp_path)
    assert seen == [("menu:fleet", "/dev/ttys001", "42", "%7", "30")]


def _shipped_fleet_conf(orch):
    conf_src = (Path(__file__).resolve().parents[1] / "deploy" / "tmux" / "dockwright.conf").read_text()
    sf1 = next((l for l in conf_src.splitlines() if "'status-format[1]'" in l), None)
    bind = next((l for l in conf_src.splitlines() if l.startswith("bind -n MouseUp1Status")), None)
    unbind = next((l for l in conf_src.splitlines() if l.startswith("unbind -n MouseDown1Status")), None)
    if sf1 is None or bind is None or unbind is None:
        pytest.fail("could not extract shipped status-format[1] / unbind / MouseUp1Status lines from dockwright.conf")
    deployed = "$HOME/.claude/dockwright/status_row.py"
    if deployed not in sf1 or deployed not in bind:
        pytest.fail(f"shipped conf no longer references {deployed!r} — the path rewrite would be a silent no-op")
    script = str(orch / "status_row.py")
    sf1 = sf1.replace(deployed, script)
    bind = bind.replace(deployed, script)
    return (
        "set -g mouse on\n"
        "set -g status 2\n"
        "set -g status-interval 1\n"
        'set -g \'status-format[0]\' "MGR"\n'
        f"{sf1}\n{unbind}\n{bind}\n"
    )


def _accumulate(fd, secs, needle=None):
    buf = b""
    end = time.time() + secs
    needle_b = needle.encode() if needle else None
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                c = os.read(fd, 65536)
            except OSError:
                break
            if not c:
                break
            buf += c
        if needle_b is not None and needle_b in buf:
            break
    return re.sub(r"\x1b\[[0-9;]*m", "", buf.decode("utf-8", "replace"))


def _client_session(sock):
    return subprocess.run(["tmux", "-L", sock, "list-clients", "-F", "#{session_name}"],
                          capture_output=True, text=True).stdout.strip()


def _sgr_motion(fd, cells, pace=0.03):
    for c, r in cells:
        os.write(fd, ("\x1b[<35;%d;%dM" % (c, r)).encode())
        _drain(fd, pace)


def _birth_manager_and_two_workers(sock, conf, orch, rows, cols):
    subprocess.run(["tmux", "-L", sock, "-f", str(conf), "new-session", "-d", "-s", "alpha", "-x", str(cols), "-y", str(rows)], check=True)
    alpha_pane = subprocess.run(["tmux", "-L", sock, "display-message", "-p", "-t", "alpha:0", "#{pane_id}"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["tmux", "-L", sock, "new-session", "-d", "-s", "wk", "-x", str(cols), "-y", str(rows)], check=True)
    subprocess.run(["tmux", "-L", sock, "new-window", "-t", "wk"], check=True)
    wk0 = subprocess.run(["tmux", "-L", sock, "display-message", "-p", "-t", "wk:0", "#{pane_id}"], capture_output=True, text=True).stdout.strip()
    wk1 = subprocess.run(["tmux", "-L", sock, "display-message", "-p", "-t", "wk:1", "#{pane_id}"], capture_output=True, text=True).stdout.strip()
    _write(orch / "active" / "m.json",
           {"agent": "manager", "name": "boss-mgr", "pid": os.getpid(), "window_id": alpha_pane})
    _write(orch / "active" / "wa.json",
           {"agent": "worker", "name": "wa-task", "funny_name": "wa-funny", "state": "processing",
            "claude_sid": "wa", "pid": os.getpid(), "parent_manager_name": "boss-mgr", "window_id": wk0})
    _write(orch / "active" / "wb.json",
           {"agent": "worker", "name": "wb-task", "funny_name": "wb-funny", "state": "idle",
            "claude_sid": "wb", "pid": os.getpid(), "parent_manager_name": "boss-mgr", "window_id": wk1})
    return alpha_pane


@pytest.mark.real_tmux
@requires_menu_tmux
def test_live_fleet_menu_pops_on_label_click(tmp_path, monkeypatch, real_tmux):
    ROWS, COLS = 30, 120
    home = tmp_path
    orch = home / ".claude" / "dockwright"
    (orch / "active").mkdir(parents=True)
    (orch / "questions").mkdir(parents=True)
    shutil.copy(_SCRIPT, orch / "status_row.py")
    monkeypatch.setenv("HOME", str(home))
    sock = real_tmux
    conf = tmp_path / "t.conf"
    conf.write_text(_shipped_fleet_conf(orch))
    _birth_manager_and_two_workers(sock, conf, orch, ROWS, COLS)

    pid, fd = _attach_pty_client(sock, "alpha", ROWS, COLS)
    try:
        _drain(fd, 2.0)
        assert _client_session(sock) == "alpha"
        os.write(fd, ("\x1b[<0;3;%dM" % ROWS).encode())
        os.write(fd, ("\x1b[<0;3;%dm" % ROWS).encode())
        overlay = _accumulate(fd, 3.0, needle="wa-funny")
        os.write(fd, b"q")
        _drain(fd, 0.3)
    finally:
        os.kill(pid, 9)
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)

    assert "wa-funny" in overlay, f"fleet menu did not pop (no worker funny_name in overlay): {overlay!r}"
    assert "workers" in overlay, f"menu title (' boss-mgr · N workers ') missing from overlay: {overlay!r}"


@pytest.mark.real_tmux
@requires_menu_tmux
def test_live_fleet_menu_survives_pointer_motion(tmp_path, monkeypatch, real_tmux):
    ROWS, COLS = 30, 120
    home = tmp_path
    orch = home / ".claude" / "dockwright"
    (orch / "active").mkdir(parents=True)
    (orch / "questions").mkdir(parents=True)
    shutil.copy(_SCRIPT, orch / "status_row.py")
    monkeypatch.setenv("HOME", str(home))
    sock = real_tmux
    conf = tmp_path / "t.conf"
    conf.write_text(_shipped_fleet_conf(orch))
    _birth_manager_and_two_workers(sock, conf, orch, ROWS, COLS)

    pid, fd = _attach_pty_client(sock, "alpha", ROWS, COLS)
    switched = "alpha"
    try:
        _drain(fd, 2.0)
        assert _client_session(sock) == "alpha"
        os.write(fd, ("\x1b[<0;3;%dM" % ROWS).encode())
        os.write(fd, ("\x1b[<0;3;%dm" % ROWS).encode())
        overlay = _accumulate(fd, 3.0, needle="wa-funny")
        assert "wa-funny" in overlay, f"fleet menu did not open: {overlay!r}"
        _sgr_motion(fd, [(1, r) for r in range(ROWS - 1, ROWS - 9, -1)])
        os.write(fd, b"\x1b[B")
        _drain(fd, 0.2)
        os.write(fd, b"\r")
        poll_end = time.time() + 3.0
        while time.time() < poll_end:
            _drain(fd, 0.1)
            switched = _client_session(sock)
            if switched and switched != "alpha":
                break
    finally:
        os.kill(pid, 9)
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)

    assert switched == "wk", (
        f"menu died while the pointer travelled toward it (session={switched!r}) — "
        "motion events outside the box closed the non-STAYOPEN menu")


@pytest.mark.real_tmux
@requires_menu_tmux
def test_live_fleet_menu_row_click_jumps(tmp_path, monkeypatch, real_tmux):
    ROWS, COLS = 30, 120
    home = tmp_path
    orch = home / ".claude" / "dockwright"
    (orch / "active").mkdir(parents=True)
    (orch / "questions").mkdir(parents=True)
    shutil.copy(_SCRIPT, orch / "status_row.py")
    monkeypatch.setenv("HOME", str(home))
    sock = real_tmux
    conf = tmp_path / "t.conf"
    conf.write_text(_shipped_fleet_conf(orch))
    _birth_manager_and_two_workers(sock, conf, orch, ROWS, COLS)

    pid, fd = _attach_pty_client(sock, "alpha", ROWS, COLS)
    switched = "alpha"
    try:
        _drain(fd, 2.0)
        assert _client_session(sock) == "alpha"
        for rr in range(ROWS - 3, ROWS - 13, -1):
            os.write(fd, ("\x1b[<0;3;%dM" % ROWS).encode())
            os.write(fd, ("\x1b[<0;3;%dm" % ROWS).encode())
            _drain(fd, 0.8)
            _sgr_motion(fd, [(3, ROWS - 1), (4, ROWS - 2), (6, rr + 1), (8, rr)])
            os.write(fd, ("\x1b[<0;8;%dM" % rr).encode())
            _drain(fd, 0.15)
            os.write(fd, ("\x1b[<0;8;%dm" % rr).encode())
            poll_end = time.time() + 1.5
            while time.time() < poll_end:
                _drain(fd, 0.1)
                switched = _client_session(sock)
                if switched and switched != "alpha":
                    break
            if switched and switched != "alpha":
                break
    finally:
        os.kill(pid, 9)
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)

    assert switched == "wk", f"menu row click did not jump the client to the worker session; session={switched!r}"


@pytest.mark.real_tmux
@requires_menu_tmux
def test_live_fleet_menu_survives_human_timed_click(tmp_path, monkeypatch, real_tmux):
    ROWS, COLS = 30, 120
    home = tmp_path
    orch = home / ".claude" / "dockwright"
    (orch / "active").mkdir(parents=True)
    (orch / "questions").mkdir(parents=True)
    shutil.copy(_SCRIPT, orch / "status_row.py")
    monkeypatch.setenv("HOME", str(home))
    sock = real_tmux
    conf = tmp_path / "t.conf"
    conf.write_text(_shipped_fleet_conf(orch))
    _birth_manager_and_two_workers(sock, conf, orch, ROWS, COLS)

    pid, fd = _attach_pty_client(sock, "alpha", ROWS, COLS)
    switched = "alpha"
    try:
        _drain(fd, 2.0)
        assert _client_session(sock) == "alpha"
        os.write(fd, ("\x1b[<0;3;%dM" % ROWS).encode())
        mid_hold = _accumulate(fd, 0.8, needle="wa-funny")
        assert "wa-funny" not in mid_hold, "menu opened on PRESS — MouseDown routing is back"
        os.write(fd, ("\x1b[<0;3;%dm" % ROWS).encode())
        overlay = _accumulate(fd, 3.0, needle="wa-funny")
        assert "wa-funny" in overlay, f"menu did not open on release: {overlay!r}"
        for rr in range(ROWS - 3, ROWS - 13, -1):
            os.write(fd, ("\x1b[<0;3;%dM" % ROWS).encode())
            _drain(fd, 0.3)
            os.write(fd, ("\x1b[<0;3;%dm" % ROWS).encode())
            _drain(fd, 0.8)
            _sgr_motion(fd, [(3, ROWS - 1), (4, ROWS - 2), (6, rr + 1), (8, rr)])
            os.write(fd, ("\x1b[<0;8;%dM" % rr).encode())
            _drain(fd, 0.15)
            os.write(fd, ("\x1b[<0;8;%dm" % rr).encode())
            poll_end = time.time() + 1.5
            while time.time() < poll_end:
                _drain(fd, 0.1)
                switched = _client_session(sock)
                if switched and switched != "alpha":
                    break
            if switched and switched != "alpha":
                break
    finally:
        os.kill(pid, 9)
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)

    assert switched == "wk", f"menu did not survive the human-timed click; session={switched!r}"


@pytest.mark.real_tmux
@requires_menu_tmux
def test_live_fleet_menu_scoped_to_clicking_manager(tmp_path, monkeypatch, real_tmux):
    ROWS, COLS = 30, 120
    home = tmp_path
    orch = home / ".claude" / "dockwright"
    (orch / "active").mkdir(parents=True)
    (orch / "questions").mkdir(parents=True)
    shutil.copy(_SCRIPT, orch / "status_row.py")
    monkeypatch.setenv("HOME", str(home))
    sock = real_tmux
    conf = tmp_path / "t.conf"
    conf.write_text(_shipped_fleet_conf(orch))

    subprocess.run(["tmux", "-L", sock, "-f", str(conf), "new-session", "-d", "-s", "alpha", "-x", str(COLS), "-y", str(ROWS)], check=True)
    alpha_pane = subprocess.run(["tmux", "-L", sock, "display-message", "-p", "-t", "alpha:0", "#{pane_id}"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["tmux", "-L", sock, "new-session", "-d", "-s", "other", "-x", str(COLS), "-y", str(ROWS)], check=True)
    subprocess.run(["tmux", "-L", sock, "new-window", "-t", "other"], check=True)
    subprocess.run(["tmux", "-L", sock, "new-window", "-t", "other"], check=True)
    p_b = subprocess.run(["tmux", "-L", sock, "display-message", "-p", "-t", "other:0", "#{pane_id}"], capture_output=True, text=True).stdout.strip()
    p_wa = subprocess.run(["tmux", "-L", sock, "display-message", "-p", "-t", "other:1", "#{pane_id}"], capture_output=True, text=True).stdout.strip()
    p_wb = subprocess.run(["tmux", "-L", sock, "display-message", "-p", "-t", "other:2", "#{pane_id}"], capture_output=True, text=True).stdout.strip()
    _write(orch / "active" / "ma.json",
           {"agent": "manager", "name": "alpha-mgr", "pid": os.getpid(), "window_id": alpha_pane})
    _write(orch / "active" / "mb.json",
           {"agent": "manager", "name": "beta-mgr", "pid": os.getpid(), "window_id": p_b})
    _write(orch / "active" / "wa.json",
           {"agent": "worker", "name": "wa-task", "funny_name": "wa-funny", "state": "processing",
            "claude_sid": "wa", "pid": os.getpid(), "parent_manager_name": "alpha-mgr", "window_id": p_wa})
    _write(orch / "active" / "wb.json",
           {"agent": "worker", "name": "wb-task", "funny_name": "wb-funny", "state": "processing",
            "claude_sid": "wb", "pid": os.getpid(), "parent_manager_name": "beta-mgr", "window_id": p_wb})

    pid, fd = _attach_pty_client(sock, "alpha", ROWS, COLS)
    try:
        _drain(fd, 2.0)
        assert _client_session(sock) == "alpha"
        os.write(fd, ("\x1b[<0;3;%dM" % ROWS).encode())
        os.write(fd, ("\x1b[<0;3;%dm" % ROWS).encode())
        overlay = _accumulate(fd, 3.0, needle="wa-funny")
        os.write(fd, b"q")
        _drain(fd, 0.3)
    finally:
        os.kill(pid, 9)
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)

    assert "wa-funny" in overlay, f"clicking manager A's own worker missing from the menu: {overlay!r}"
    assert "wb-funny" not in overlay, f"peer manager B's worker leaked into A's menu: {overlay!r}"
    assert "alpha-mgr" in overlay, f"menu title missing scoping manager A's name: {overlay!r}"
    assert "1 workers" in overlay, f"menu title not scoped to A's single worker: {overlay!r}"


_TS = "2026-08-14T01:55:33.039Z"


@pytest.fixture(autouse=True)
def _tmux_cannot_answer(monkeypatch):
    monkeypatch.setattr(sr, "_live_pane_ids", lambda: None)


def _mgr(**kw):
    rec = {"agent": "manager", "name": "boss", "claude_sid": "sid1", "window_id": "%5",
           "last_turn_at": _TS, "last_summary": "έτοιμο"}
    rec.update(kw)
    return {k: v for k, v in rec.items() if v is not _ABSENT}


class _Absent:
    pass


_ABSENT = _Absent()


def _lit(out):
    return sr.UNREAD_MARKER in out


def test_unread_lights_when_no_mark_exists(tmp_path):
    out = sr.render_managers([_mgr()], selected_pane="%9", orch=tmp_path)
    assert _lit(out)
    assert f"bg={sr.UNREAD_COLOR[0]}" in out


def test_read_mark_equal_to_signature_is_not_lit(tmp_path):
    sr._write_mark(sr._mark_path(tmp_path, _mgr()), sr._signature(_mgr()))
    out = sr.render_managers([_mgr()], selected_pane="%9", orch=tmp_path)
    assert not _lit(out)
    assert f"bg={sr.MANAGER_COLOR[0]}" in out


def test_stamp_then_leave_clears_the_chip(tmp_path):
    lit_before = sr.render_managers([_mgr()], selected_pane="%9", orch=tmp_path)
    viewing = sr.render_managers([_mgr()], selected_pane="%5", orch=tmp_path)
    after = sr.render_managers([_mgr()], selected_pane="%9", orch=tmp_path)
    assert _lit(lit_before)
    assert not _lit(viewing)
    assert not _lit(after), "opening the tab did not clear the mark"


def test_no_stamp_when_the_manager_pane_is_not_selected(tmp_path):
    sr.render_managers([_mgr()], selected_pane="%9", orch=tmp_path)
    assert list(tmp_path.glob(".read-*")) == []
    assert _lit(sr.render_managers([_mgr()], selected_pane="%9", orch=tmp_path))


def test_mark_round_trip_through_the_production_writer(tmp_path):
    rec = _mgr(last_summary="ουρά με νέα γραμμή\n")
    path = sr._mark_path(tmp_path, rec)
    sr._write_mark(path, sr._signature(rec))
    assert sr._read_mark(path) == sr._signature(rec)
    assert not _lit(sr.render_managers([rec], selected_pane="%9", orch=tmp_path))


def test_mark_file_with_a_trailing_newline_still_reads_as_read(tmp_path):
    rec = _mgr()
    sr._mark_path(tmp_path, rec).write_text(sr._signature(rec) + "\n")
    assert not _lit(sr.render_managers([rec], selected_pane="%9", orch=tmp_path))


def test_signature_moves_on_summary_alone(tmp_path):
    first = _mgr(last_turn_at=None)
    sr.render_managers([first], selected_pane="%5", orch=tmp_path)
    second = _mgr(last_turn_at=None, last_summary="νέα περίληψη")
    assert _lit(sr.render_managers([second], selected_pane="%9", orch=tmp_path))


def test_no_signal_at_all_is_never_lit(tmp_path):
    out = sr.render_managers([_mgr(last_turn_at=None, last_summary=None)],
                             selected_pane="%9", orch=tmp_path)
    assert not _lit(out)
    assert f"bg={sr.MANAGER_COLOR[0]}" in out
    assert list(tmp_path.glob(".read-*")) == []


def test_no_key_is_never_lit_and_writes_nothing(tmp_path):
    rec = _mgr(claude_sid=_ABSENT, name=_ABSENT)
    out = sr.render_managers([rec], selected_pane="%5", orch=tmp_path)
    assert not _lit(out)
    assert list(tmp_path.glob(".read-*")) == []


def test_future_timestamp_marked_read_stays_dark(tmp_path):
    rec = _mgr(last_turn_at="2030-01-01T00:00:00.000Z")
    sr._write_mark(sr._mark_path(tmp_path, rec), sr._signature(rec))
    assert not _lit(sr.render_managers([rec], selected_pane="%9", orch=tmp_path))
    other = _mgr(last_turn_at="2030-01-02T00:00:00.000Z")
    assert _lit(sr.render_managers([other], selected_pane="%9", orch=tmp_path))


def test_past_timestamp_with_older_mark_mtime_stays_dark(tmp_path):
    rec = _mgr(last_turn_at="2020-01-01T00:00:00.000Z")
    path = sr._mark_path(tmp_path, rec)
    sr._write_mark(path, sr._signature(rec))
    old = time.time() - 10 * 365 * 24 * 3600
    os.utime(path, (old, old))
    assert not _lit(sr.render_managers([rec], selected_pane="%9", orch=tmp_path))


def test_epoch_shaped_mark_never_reads_as_read(tmp_path):
    rec = _mgr()
    sr._mark_path(tmp_path, rec).write_text("1786377498")
    assert _lit(sr.render_managers([rec], selected_pane="%9", orch=tmp_path))


def test_unwritable_mark_dir_still_renders_the_chips(tmp_path):
    wall = tmp_path / "nodir"
    wall.write_text("not a directory")
    out = sr.render_managers([_mgr()], selected_pane="%5", orch=wall)
    assert "🎯 boss" in out


def test_orch_default_none_leaves_managers_row_unchanged(tmp_path):
    assert sr.render_managers([_mgr()]) == (
        "#[range=user|switch:%5]#[bg=#aa0066,fg=#ffffff] 🎯 boss #[default]#[norange]")
    assert sr.render_managers([_mgr(domain="general", window_id=None)]) == (
        "#[bg=#aa0066,fg=#ffffff] 🎯 boss · general #[default]")


def test_mark_write_failure_leaves_a_reason(tmp_path):
    rec = _mgr(last_summary="\udcff")
    assert "🎯 boss" in sr.render_managers([rec], selected_pane="%5", orch=tmp_path)
    assert "UnicodeEncodeError" in (tmp_path / ".read-sid1.err").read_text()
    assert list(tmp_path.glob("*.tmp")) == []
    sr._write_mark(sr._mark_path(tmp_path, _mgr()), "recovered")
    assert not (tmp_path / ".read-sid1.err").exists(), "stale reason survived a good write"


def test_each_manager_gets_its_own_failure_reason(tmp_path):
    a = _mgr(claude_sid="sidA", window_id="%1", last_summary="\udcff")
    b = _mgr(claude_sid="sidB", window_id="%2", last_summary="\udcfe")
    sr.render_managers([a], selected_pane="%1", orch=tmp_path)
    sr.render_managers([b], selected_pane="%2", orch=tmp_path)
    assert "\\udcff" in repr((tmp_path / ".read-sidA.err").read_text())
    assert "\\udcfe" in repr((tmp_path / ".read-sidB.err").read_text())


def test_sanitised_key_never_contains_a_dot(tmp_path):
    checked = 0
    for key in ("a.b", "mark-errors.log", "x.err", "y.123.tmp", "..", "./../z", ".",
                "sid.1", "a..b", "…", "sid\u3002err", "9.9"):
        path = sr._mark_path(tmp_path, {"claude_sid": key})
        if path is None:
            continue
        checked += 1
        assert "." not in path.name[len(".read-"):], key
    assert checked >= 8, "gutting _mark_path to None would pass this vacuously"


def test_failure_reason_carries_a_utc_timestamp(tmp_path):
    with pytest.MonkeyPatch.context() as tz:
        tz.setenv("TZ", "Etc/GMT-9")
        time.tzset()
        try:
            rec = _mgr(last_summary="\udcff")
            sr.render_managers([rec], selected_pane="%5", orch=tmp_path)
            stamp = (tmp_path / ".read-sid1.err").read_text().split()[0]
            assert stamp.endswith("Z")
            assert abs(time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
                       - time.mktime(time.gmtime())) < 120
        finally:
            pass
    time.tzset()


def test_manager_without_window_id_is_never_lit(tmp_path):
    rec = _mgr(window_id="")
    for pane in ("", "%5", "%999"):
        assert not _lit(sr.render_managers([rec], selected_pane=pane, orch=tmp_path)), pane
    assert list(tmp_path.glob(".read-*")) == []


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_live_pane_ids_parses_tmux_output(monkeypatch):
    monkeypatch.undo()
    seen = []
    monkeypatch.setattr(sr.subprocess, "run",
                        lambda *a, **k: seen.append((a, k)) or _Proc(stdout="%1\n%660\n\n  %663  \n"))
    assert sr._live_pane_ids() == {"%1", "%660", "%663"}
    argv, kwargs = seen[0]
    assert argv[0] == ["tmux", "list-panes", "-a", "-F", "#{pane_id}"], argv
    assert kwargs.get("capture_output") and kwargs.get("text")


def test_live_pane_ids_returns_none_when_tmux_raises(monkeypatch):
    monkeypatch.undo()
    def boom(*a, **k):
        raise OSError("tmux is gone")
    monkeypatch.setattr(sr.subprocess, "run", boom)
    assert sr._live_pane_ids() is None


def test_live_pane_ids_returns_none_on_an_unexplained_failure(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(sr.subprocess, "run",
                        lambda *a, **k: _Proc(returncode=1, stderr="protocol version mismatch"))
    assert sr._live_pane_ids() is None


def test_live_pane_ids_returns_empty_only_when_there_is_no_server(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(sr.subprocess, "run",
                        lambda *a, **k: _Proc(returncode=1, stderr="no server running on /tmp/x"))
    assert sr._live_pane_ids() == set()


def test_live_pane_ids_bounds_its_wait(monkeypatch):
    monkeypatch.undo()
    seen = {}
    monkeypatch.setattr(sr.subprocess, "run",
                        lambda *a, **k: seen.update(k) or _Proc(stdout="%1\n"))
    sr._live_pane_ids()
    assert 0 < seen.get("timeout", 0) <= 5


def test_unread_resolves_live_panes_itself_when_the_caller_omits_them(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "_live_pane_ids", lambda: {"%660"})
    assert sr._unread(_mgr(window_id="%1"), "%660", tmp_path) is False
    assert sr._unread(_mgr(window_id="%660", claude_sid="s2"), "%999", tmp_path) is True


def test_dead_pane_manager_is_never_lit(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "_live_pane_ids", lambda: {"%660", "%663"})
    ghost = _mgr(window_id="%1")
    for pane in ("", "%660", "%663", "%999"):
        assert not _lit(sr.render_managers([ghost], selected_pane=pane, orch=tmp_path)), pane
    assert list(tmp_path.glob(".read-*")) == []


def test_live_pane_manager_still_lights(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "_live_pane_ids", lambda: {"%5", "%660"})
    assert _lit(sr.render_managers([_mgr()], selected_pane="%660", orch=tmp_path))


def test_unanswerable_tmux_keeps_lighting(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "_live_pane_ids", lambda: None)
    assert _lit(sr.render_managers([_mgr()], selected_pane="%999", orch=tmp_path))


def test_live_pane_lookup_is_skipped_while_nothing_would_light(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sr, "_live_pane_ids", lambda: calls.append(1) or {"%5"})
    rec = _mgr()
    sr._write_mark(sr._mark_path(tmp_path, rec), sr._signature(rec))
    sr.render_managers([rec], selected_pane="%999", orch=tmp_path)
    assert calls == [], "queried tmux with nothing to light"
    two = [_mgr(claude_sid="a"), _mgr(claude_sid="b")]
    sr.render_managers(two, selected_pane="%999", orch=tmp_path)
    assert calls == [1], "queried tmux once per manager instead of once per pass"


def test_manager_with_window_id_still_lights(tmp_path):
    assert _lit(sr.render_managers([_mgr()], selected_pane="%999", orch=tmp_path))


def test_key_with_a_path_separator_cannot_escape_the_orch_dir(tmp_path):
    rec = _mgr(claude_sid="../../escaped")
    sr.render_managers([rec], selected_pane="%5", orch=tmp_path)
    assert list(tmp_path.parent.glob(".read-*")) == []
    assert not (tmp_path / ".." / ".." / ".read-").exists()
    assert all(p.is_file() for p in tmp_path.glob(".read-*"))


def test_two_processes_never_share_one_tmp_mark_file(tmp_path, monkeypatch):
    path = sr._mark_path(tmp_path, _mgr())
    seen = []
    real_replace = sr.os.replace

    def spy(src, dst):
        seen.append(str(src))
        real_replace(src, dst)

    monkeypatch.setattr(sr.os, "replace", spy)
    monkeypatch.setattr(sr.os, "getpid", lambda: 111)
    sr._write_mark(path, "A" * 200)
    monkeypatch.setattr(sr.os, "getpid", lambda: 222)
    sr._write_mark(path, "B" * 200)
    assert seen[0] != seen[1], f"both processes wrote through one tmp file: {seen[0]}"
    assert list(tmp_path.glob("*.tmp")) == []
    assert sr._read_mark(path) == "B" * 200


def test_surrogate_in_summary_does_not_blank_the_managers_row(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sr, "_selected_pane", _boom_selected_pane)
    orch = tmp_path / ".claude" / "dockwright"
    _write(orch / "active" / "m.json",
           {"agent": "manager", "name": "boss", "claude_sid": "sid1", "pid": os.getpid(),
            "window_id": "%5", "last_turn_at": _TS, "last_summary": "\udcff"})
    sr.main(["status_row.py", "managers", "%5"], tmp_path)
    assert "🎯 boss" in capsys.readouterr().out


def test_selected_chip_keeps_marker_and_lit_chip_keeps_click_range(tmp_path):
    selected = sr.render_managers([_mgr()], selected_pane="%5", orch=tmp_path)
    assert f"{sr.SELECTED_MARKER}" in selected
    lit = sr.render_managers([_mgr(claude_sid="sid2")], selected_pane="%9", orch=tmp_path)
    assert "#[range=user|switch:%5]" in lit and _lit(lit)


def test_main_managers_passes_orch_so_the_chip_can_light(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sr, "_selected_pane", _boom_selected_pane)
    home = tmp_path
    orch = home / ".claude" / "dockwright"
    _write(orch / "active" / "m.json",
           {"agent": "manager", "name": "boss", "claude_sid": "sid1", "pid": os.getpid(),
            "window_id": "%5", "last_turn_at": _TS, "last_summary": "έτοιμο"})
    sr.main(["status_row.py", "managers", "%9"], home)
    assert sr.UNREAD_MARKER in capsys.readouterr().out
    sr.main(["status_row.py", "managers", "%5"], home)
    assert (orch / ".read-sid1").read_text() == _TS + "\x00έτοιμο"
    sr.main(["status_row.py", "managers", "%9"], home)
    assert sr.UNREAD_MARKER not in capsys.readouterr().out


@pytest.mark.real_tmux
def test_unread_marker_is_one_cell_as_tmux_counts_it(real_tmux):
    sock = real_tmux
    subprocess.run(["tmux", "-L", sock, "new-session", "-d", "-s", "widthcheck"], check=True)

    def cells(text):
        subprocess.run(["tmux", "-L", sock, "set", "-g", "@probe", text], check=True)
        out = subprocess.run(["tmux", "-L", sock, "display-message", "-p", "#{p6:@probe}|"],
                             capture_output=True, text=True, check=True).stdout
        return 6 - out.split("|")[0].count(" ")

    try:
        assert (cells("A"), cells("漢"), cells("🎯")) == (1, 2, 2), "the width oracle itself is broken"
        assert cells(sr.UNREAD_MARKER) == 1
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)
    assert len(sr.UNREAD_MARKER) == 2 and sr.UNREAD_MARKER[1] == "\ufe0e"


@pytest.mark.real_tmux
def test_live_manager_chip_lights_then_clears_on_visit(tmp_path, monkeypatch, real_tmux):
    home = tmp_path
    orch = home / ".claude" / "dockwright"
    (orch / "active").mkdir(parents=True)
    (orch / "questions").mkdir(parents=True)
    shutil.copy(_SCRIPT, orch / "status_row.py")
    conf = tmp_path / "unread.conf"
    conf.write_text(
        'set -g status 2\n'
        'set -g status-interval 1\n'
        'set -g \'status-format[0]\' "MGR '
        '#(python3 $HOME/.claude/dockwright/status_row.py managers #{pane_id})"\n'
        'set -g \'status-format[1]\' "WRK"\n'
    )
    monkeypatch.setenv("HOME", str(home))
    sock = real_tmux
    subprocess.run(["tmux", "-L", sock, "-f", str(conf), "new-session", "-d", "-s", "unreadsess",
                    "-x", "200", "-y", "50"], check=True)
    try:
        subprocess.run(["tmux", "-L", sock, "new-window", "-t", "unreadsess"], check=True)
        mgr_pane = subprocess.run(["tmux", "-L", sock, "display-message", "-p", "-t", "unreadsess:0",
                                   "#{pane_id}"], capture_output=True, text=True).stdout.strip()
        _write(orch / "active" / "m.json",
               {"agent": "manager", "name": "boss", "claude_sid": "sid1", "pid": os.getpid(),
                "window_id": mgr_pane, "last_turn_at": _TS, "last_summary": "έτοιμο"})
        _write(orch / "active" / "g.json",
               {"agent": "manager", "name": "ghost", "claude_sid": "sid2", "pid": os.getpid(),
                "window_id": "%9999", "last_turn_at": _TS, "last_summary": "έτοιμο"})

        subprocess.run(["tmux", "-L", sock, "select-window", "-t", "unreadsess:1"], check=True)
        away = _capture(sock, "unreadsess", secs=5)
        subprocess.run(["tmux", "-L", sock, "select-window", "-t", "unreadsess:0"], check=True)
        visiting = _capture(sock, "unreadsess", secs=5)
        subprocess.run(["tmux", "-L", sock, "select-window", "-t", "unreadsess:1"], check=True)
        back = _capture(sock, "unreadsess", secs=5)
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)

    assert "boss" in away, f"manager chip missing entirely: {away!r}"
    assert "ghost" in away, f"dead-pane manager chip missing entirely: {away!r}"
    assert sr.UNREAD_MARKER in away, f"unread chip did not light: {away!r}"
    lines = [l for l in away.splitlines() if "boss" in l and "ghost" in l]
    assert lines, f"never captured a frame holding both chips: {away!r}"
    assert all(l.count(sr.UNREAD_MARKER) == 1 for l in lines), (
        f"expected exactly one lit chip per frame — the dead-pane manager must "
        f"not light: {lines!r}")
    assert sr.UNREAD_MARKER not in visiting, f"chip lit while he was looking at it: {visiting!r}"
    assert sr.UNREAD_MARKER not in back, f"visiting the tab did not clear the chip: {back!r}"
    assert (orch / ".read-sid1").read_text() == _TS + "\x00έτοιμο"


def _delegating_tree(tmp_path, sid="w-sid", log_age=400.0, sub_age=5.0):
    import time as _t
    project = tmp_path / ".claude" / "projects" / "-Users-x"
    project.mkdir(parents=True, exist_ok=True)
    log = project / f"{sid}.jsonl"
    log.write_text("")
    subagents = project / sid / "subagents"
    subagents.mkdir(parents=True, exist_ok=True)
    agent = subagents / "agent-aaa.jsonl"
    agent.write_text("{}")
    now = _t.time()
    os.utime(log, (now - log_age, now - log_age))
    os.utime(agent, (now - sub_age, now - sub_age))
    return log


def _idle_record(log, sid="w-sid", **extra):
    rec = {"claude_sid": sid, "state": "idle", "runtime": "claude",
           "transcript_path": str(log)}
    rec.update(extra)
    return rec


def test_classify_idle_with_live_subagent_reads_as_processing(tmp_path):
    log = _delegating_tree(tmp_path)
    assert sr.classify_worker(_idle_record(log), set()) == "processing"


def test_classify_idle_when_subagent_write_predates_the_main_log(tmp_path):
    log = _delegating_tree(tmp_path, log_age=300.0, sub_age=400.0)
    assert sr.classify_worker(_idle_record(log), set()) == "idle"


def test_classify_idle_when_subagent_quiet_past_the_liveness_window(tmp_path):
    log = _delegating_tree(tmp_path, log_age=4000.0, sub_age=2000.0)
    assert sr.classify_worker(_idle_record(log), set()) == "idle"


def test_classify_liveness_window_moves_with_the_episode_grace_env(tmp_path, monkeypatch):
    log = _delegating_tree(tmp_path, log_age=800.0, sub_age=600.0)
    rec = _idle_record(log)
    monkeypatch.setenv("CLAUDE_ORCH_EPISODE_GRACE_SEC", "1200")
    assert sr.classify_worker(rec, set()) == "processing"
    monkeypatch.setenv("CLAUDE_ORCH_EPISODE_GRACE_SEC", "300")
    assert sr.classify_worker(rec, set()) == "idle"


def test_classify_idle_without_a_transcript_path(tmp_path):
    _delegating_tree(tmp_path)
    assert sr.classify_worker({"claude_sid": "w-sid", "state": "idle"}, set()) == "idle"


def test_classify_idle_for_codex_runtime(tmp_path):
    log = _delegating_tree(tmp_path)
    assert sr.classify_worker(_idle_record(log, runtime="codex"), set()) == "idle"


def test_classify_survives_an_unreadable_transcript_path(tmp_path):
    assert sr.classify_worker(
        {"claude_sid": "w-sid", "state": "idle",
         "transcript_path": str(tmp_path / "nope" / "gone.jsonl")}, set()) == "idle"
    assert sr.classify_worker(
        {"claude_sid": "w-sid", "state": "idle", "transcript_path": 17}, set()) == "idle"


def test_classify_question_still_beats_a_live_subagent(tmp_path):
    log = _delegating_tree(tmp_path)
    assert sr.classify_worker(_idle_record(log), {"w-sid"}) == "question"


def test_render_workers_lifts_a_delegating_worker_out_of_the_idle_group(tmp_path):
    log = _delegating_tree(tmp_path)
    recs = [
        _idle_record(log, name="alpha", agent="worker"),
        {"agent": "worker", "name": "bravo", "state": "idle", "claude_sid": "b"},
    ]
    out = sr.render_workers(recs, set())
    assert "🔧 alpha" in out
    assert "#aa8800" in out
    assert "💤1" in out
    assert "💤 alpha" not in out


def test_fleet_menu_row_marks_a_delegating_worker_as_working(tmp_path):
    log = _delegating_tree(tmp_path)
    label = sr._menu_label(_idle_record(log, name="alpha"), set(), "")
    assert "🔧" in label and "💤" not in label


def test_dead_delegating_worker_is_still_filtered_out(tmp_path):
    log = _delegating_tree(tmp_path)
    dead = _idle_record(log, name="alpha", agent="worker", pid=2 ** 22)
    assert sr._is_visible(dead) is False
    assert sr.render_workers([r for r in [dead] if sr._is_visible(r)], set()) == ""


def test_as_epoch_reads_iso_utc_and_epoch_float_on_one_scale():
    assert abs(sr._as_epoch("2026-08-26T07:47:41.136Z") - 1787730461.136) < 1e-6
    assert sr._as_epoch(1787730461.136) == 1787730461.136


def test_as_epoch_returns_none_for_absent_or_malformed():
    for bad in (None, "", "not-a-date", "2026-13-45T99:99:99Z", [], {}, True, float("nan"),
                int("9" * 400)):
        assert sr._as_epoch(bad) is None


def test_activity_at_takes_the_newest_candidate_across_scales():
    rec = {"last_turn_at": "2026-08-25T09:48:10.313Z",
           "processing_since": 1787649075.695,
           "tasked_at": 1787650150.619,
           "started_at": 1787634651.285}
    assert abs(sr._activity_at(rec) - 1787651290.313) < 1e-6


def test_activity_at_falls_back_when_last_turn_at_is_null():
    rec = {"last_turn_at": None, "processing_since": 1787727890.748, "started_at": 1787727890.606}
    assert sr._activity_at(rec) == 1787727890.748


def test_activity_at_is_none_when_the_record_carries_no_usable_stamp():
    assert sr._activity_at({"name": "w"}) is None
    assert sr._activity_at({"name": "w", "last_turn_at": "garbage", "started_at": None}) is None


def test_freshest_first_orders_newest_first_undated_last_name_breaks_ties():
    old = {"name": "aaa", "started_at": 100.0}
    new = {"name": "zzz", "started_at": 200.0}
    undated_b = {"name": "bbb"}
    undated_a = {"name": "aab"}
    assert sorted([old, undated_b, new, undated_a], key=sr._freshest_first) == [
        new, old, undated_a, undated_b
    ]


def test_render_workers_orders_idle_freshest_first_not_alphabetically():
    recs = [
        {"agent": "worker", "name": "aaa", "claude_sid": "a", "window_id": "%1", "last_turn_at": "2026-08-25T09:00:00.000Z"},
        {"agent": "worker", "name": "zzz", "claude_sid": "z", "window_id": "%2", "last_turn_at": "2026-08-26T09:00:00.000Z"},
    ]
    out = sr.render_workers(recs, set(), idle_expanded=True)
    assert out.index("💤 zzz") < out.index("💤 aaa")


def test_render_workers_keeps_processing_alphabetical_not_by_freshness():
    recs = [
        {"agent": "worker", "name": "aaa", "state": "processing", "claude_sid": "a", "window_id": "%1", "processing_since": 100.0},
        {"agent": "worker", "name": "zzz", "state": "processing", "claude_sid": "z", "window_id": "%2", "processing_since": 200.0},
    ]
    out = sr.render_workers(recs, set())
    assert out.index("🔧 aaa") < out.index("🔧 zzz")


def test_render_workers_keeps_processing_above_idle_regardless_of_stamps():
    recs = [
        {"agent": "worker", "name": "idle-newer", "claude_sid": "i", "window_id": "%1", "last_turn_at": "2026-08-26T12:00:00.000Z"},
        {"agent": "worker", "name": "busy-older", "state": "processing", "claude_sid": "b", "window_id": "%2", "processing_since": 1.0},
    ]
    out = sr.render_workers(recs, set(), idle_expanded=True)
    assert out.index("🔧 busy-older") < out.index("💤 idle-newer")


def test_render_workers_undated_worker_sorts_last_and_does_not_crash():
    recs = [
        {"agent": "worker", "name": "undated", "claude_sid": "u", "window_id": "%1"},
        {"agent": "worker", "name": "zdated", "claude_sid": "d", "window_id": "%2", "started_at": 5.0},
    ]
    out = sr.render_workers(recs, set(), idle_expanded=True)
    assert out.index("💤 zdated") < out.index("💤 undated")


def test_bucketed_sorts_only_the_idle_bucket_by_freshness():
    recs = [
        {"agent": "worker", "name": "idle-aaa", "claude_sid": "io", "started_at": 10.0},
        {"agent": "worker", "name": "idle-zzz", "claude_sid": "in", "started_at": 20.0},
        {"agent": "worker", "name": "busy-aaa", "claude_sid": "bo", "state": "processing", "processing_since": 10.0},
        {"agent": "worker", "name": "busy-zzz", "claude_sid": "bn", "state": "processing", "processing_since": 20.0},
        {"agent": "worker", "name": "q-aaa", "claude_sid": "qa", "started_at": 10.0},
        {"agent": "worker", "name": "q-zzz", "claude_sid": "qz", "started_at": 20.0},
    ]
    names = [r["name"] for r in sr._bucketed(recs, {"qa", "qz"})]
    assert names == ["q-aaa", "q-zzz", "busy-aaa", "busy-zzz", "idle-zzz", "idle-aaa"]


def test_build_fleet_menu_keeps_per_manager_grouping_and_sorts_within_it():
    recs = [
        {"agent": "worker", "name": "b-aaa", "claude_sid": "1", "parent_manager_name": "bee", "started_at": 10.0},
        {"agent": "worker", "name": "b-zzz", "claude_sid": "2", "parent_manager_name": "bee", "started_at": 20.0},
        {"agent": "worker", "name": "a-aaa", "claude_sid": "3", "parent_manager_name": "ant", "started_at": 30.0},
        {"agent": "worker", "name": "a-zzz", "claude_sid": "4", "parent_manager_name": "ant", "started_at": 40.0},
    ]
    _, args = sr.build_fleet_menu(recs, set(), None)
    flat = " ".join(args)
    for earlier, later in (("ant", "a-zzz"), ("a-zzz", "a-aaa"), ("a-aaa", "bee"),
                           ("bee", "b-zzz"), ("b-zzz", "b-aaa")):
        assert flat.index(earlier) < flat.index(later)

def _live_tree(tmp_path, sid="w-sid", log_age=3.0):
    import time as _t
    project = tmp_path / ".claude" / "projects" / "-Users-x"
    project.mkdir(parents=True, exist_ok=True)
    log = project / f"{sid}.jsonl"
    log.write_text("")
    now = _t.time()
    os.utime(log, (now - log_age, now - log_age))
    return log


def test_classify_a_just_written_transcript_as_processing_though_state_is_idle(tmp_path):
    log = _live_tree(tmp_path, log_age=3.0)
    assert sr.classify_worker(_idle_record(log), set()) == "processing"


def test_classify_idle_once_the_transcript_predates_the_turn_end_grace(tmp_path):
    log = _live_tree(tmp_path, log_age=400.0)
    assert sr.classify_worker(_idle_record(log), set()) == "idle"


def test_classify_transcript_liveness_window_moves_with_the_turn_end_grace_env(tmp_path, monkeypatch):
    log = _live_tree(tmp_path, log_age=200.0)
    rec = _idle_record(log)
    monkeypatch.setenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", "600")
    assert sr.classify_worker(rec, set()) == "processing"
    monkeypatch.setenv("CLAUDE_ORCH_TURN_END_GRACE_SEC", "60")
    assert sr.classify_worker(rec, set()) == "idle"


def test_classify_idle_when_the_transcript_path_is_absent_or_unusable(tmp_path):
    assert sr.classify_worker({"claude_sid": "w-sid", "state": "idle"}, set()) == "idle"
    assert sr.classify_worker(
        {"claude_sid": "w-sid", "state": "idle", "transcript_path": ""}, set()) == "idle"
    assert sr.classify_worker(
        {"claude_sid": "w-sid", "state": "idle", "transcript_path": 17}, set()) == "idle"
    assert sr.classify_worker(
        {"claude_sid": "w-sid", "state": "idle",
         "transcript_path": str(tmp_path / "gone" / "missing.jsonl")}, set()) == "idle"


def test_classify_processing_state_still_wins_with_no_transcript_at_all():
    assert sr.classify_worker({"claude_sid": "w-sid", "state": "processing"}, set()) == "processing"


def test_classify_question_still_beats_a_live_transcript(tmp_path):
    log = _live_tree(tmp_path, log_age=3.0)
    assert sr.classify_worker(_idle_record(log), {"w-sid"}) == "question"


def test_is_live_honours_a_now_passed_by_the_caller(tmp_path):
    import time as _t
    log = _live_tree(tmp_path, log_age=3.0)
    rec = _idle_record(log)
    now = _t.time()
    assert sr._is_live(rec, now) is True
    assert sr._is_live(rec, now + 10_000) is False
    assert sr.classify_worker(rec, set(), now + 10_000) == "idle"


def test_is_live_returns_false_rather_than_raising_on_a_hostile_record(tmp_path):
    for rec in ({}, {"transcript_path": None}, {"transcript_path": 17},
                {"transcript_path": []}, {"transcript_path": ""},
                {"transcript_path": "\0bad"},
                {"transcript_path": str(tmp_path / "gone" / "missing.jsonl")}):
        assert sr._is_live(rec) is False


def test_is_live_returns_a_bool_for_a_stat_able_path_that_is_not_a_transcript(tmp_path):
    assert isinstance(sr._is_live({"transcript_path": str(tmp_path)}), bool)


def test_render_workers_lifts_a_live_worker_out_of_the_idle_group(tmp_path):
    log = _live_tree(tmp_path, log_age=3.0)
    recs = [
        _idle_record(log, name="alpha", agent="worker"),
        {"agent": "worker", "name": "bravo", "state": "idle", "claude_sid": "b"},
    ]
    out = sr.render_workers(recs, set())
    assert "🔧 alpha" in out
    assert "#aa8800" in out
    assert "💤1" in out
    assert "💤 alpha" not in out


def test_fleet_menu_row_marks_a_live_worker_as_working(tmp_path):
    log = _live_tree(tmp_path, log_age=3.0)
    label = sr._menu_label(_idle_record(log, name="alpha"), set(), "")
    assert "🔧" in label and "💤" not in label
