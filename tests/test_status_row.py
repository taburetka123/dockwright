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
    sr._tmux("switch-client", "-t", "%dead")   # must not raise


def test_main_click_dispatches(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(sr, "handle_click", lambda payload, orch: seen.append((payload, orch)))
    sr.main(["status_row.py", "click", "switch:%5"], tmp_path)
    assert seen and seen[0][0] == "switch:%5"
    # Neither home exists under this tmp root, so _prefer_new returns the new default.
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
    out = sr.render_workers(recs, {"d"})  # delta has a pending question
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
    sr.main(["status_row.py", "managers"], home)   # no pane arg -> self-query
    assert "▸🎯 boss" in capsys.readouterr().out


def test_main_falls_back_to_selected_pane_when_arg_empty(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sr, "_selected_pane", lambda: "%5")
    home = tmp_path
    orch = home / ".claude" / "orchestrator"
    _write(orch / "active" / "m.json", {"agent": "manager", "name": "boss", "pid": os.getpid(), "window_id": "%5"})
    sr.main(["status_row.py", "managers", ""], home)   # empty pane arg -> self-query
    assert "▸🎯 boss" in capsys.readouterr().out


def test_main_click_still_reads_argv2_as_payload(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(sr, "handle_click", lambda payload, orch: seen.append(payload))
    sr.main(["status_row.py", "click", "switch:%5"], tmp_path)
    assert seen == ["switch:%5"]   # click path is disjoint from the selected-pane arg


def test_main_prefers_dockwright_home_over_legacy(tmp_path, capsys):
    home = tmp_path
    _write(home / ".claude" / "dockwright" / "active" / "m.json",
           {"agent": "manager", "name": "newboss", "pid": os.getpid(), "window_id": "%5"})
    _write(home / ".claude" / "orchestrator" / "active" / "m.json",
           {"agent": "manager", "name": "oldboss", "pid": os.getpid(), "window_id": "%5"})
    sr.main(["status_row.py", "managers", "%5"], home)
    out = capsys.readouterr().out
    assert "newboss" in out and "oldboss" not in out


def test_conf_passes_pane_id_to_both_status_rows():
    # Guards the shipped fix: both status-format rows must pass #{pane_id} to
    # status_row.py. Dropping it re-introduces the chip-click highlight lag and
    # the multi-client mis-highlight (the script then falls back to _selected_pane).
    conf = (Path(__file__).resolve().parents[1] / "deploy" / "tmux" / "dockwright.conf").read_text()
    assert "status_row.py managers #{pane_id}" in conf
    assert "status_row.py workers #{pane_id}" in conf


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
    sock = real_tmux  # throwaway per-pid socket from the fixture; never -L claude-orch
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
    ROWS, COLS = 30, 120  # bottom screen row == window height == status-format[1]'s row
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
        f"bind -n MouseDown1Status run-shell 'printf %s \"#{{mouse_status_range}}\" >> {payload_file}; "
        "python3 $HOME/.claude/dockwright/status_row.py click \"#{mouse_status_range}\"'\n"
    )
    # Birth the server WITH the conf so the #() status jobs activate; they do not
    # fire when status-format is set on an already-running server via source-file.
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
        # Keep reading the attached client's output so its terminal buffer never
        # blocks; a stalled buffer freezes the status redraw and the async #()
        # chip never paints (so no clickable range exists at click time).
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

        click(3, ROWS)   # col 3 of the bottom (workers) row = inside the first worker chip
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
    assert sr.SELECTED_COLOR[0] in out            # "#0099cc"
    assert sr.BUSY_COLOR[0] not in out            # state color overridden


def test_unselected_chip_keeps_state_color_and_no_selected_color():
    out = sr.chip("hi", sr.BUSY_COLOR)
    assert sr.BUSY_COLOR[0] in out                # "#aa8800"
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
    # status_row uses the ACTIVE (first) element of each hooks tuple as the chip bg.
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
    """Attach a PERSISTENT pty client to `session`; return (pid, fd). The caller
    drains fd (read-and-discard) so the client's terminal buffer never blocks the
    status redraw, and os.kill(pid, 9) at teardown. (_capture's fresh attach per
    call re-runs every #() job unconditionally, so it can't observe client-scoped
    or on-switch behavior — these tests need a client that stays put.)"""
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
    """A status-format #() wrapper that records the pane arg each render received,
    then execs the REAL status_row.py with it — so the test exercises the shipped
    script while observing which pane each render resolved to."""
    w = tmp_path / "wrap.sh"
    w.write_text(
        "#!/bin/sh\n"
        f'printf "%s %s\\n" "$1" "$2" >> "{log}"\n'      # "$1"=row (managers|workers), "$2"=pane
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
    """The fix, on the user's exact path: single client, click a worker chip ->
    the click routes through status_row.py's click resolver -> a run-shell
    switch-client. With #{pane_id} in the #() command the highlight job re-runs
    with the newly-viewed pane within a fraction of a second — far under
    status-interval (30s). A regression to interval-only refresh would leave the
    clicked pane unresolved for ~30s (the chip-click lag this fixes)."""
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
        "set -g status-interval 30\n"   # high: a prompt re-run can only be the click-driven redraw, not the timer
        f"set -g 'status-format[0]' \"#(sh {wrap} managers #{{pane_id}})\"\n"
        f"set -g 'status-format[1]' \"#(sh {wrap} workers #{{pane_id}})\"\n"
        f"bind -n MouseDown1Status run-shell 'python3 {script} click \"#{{mouse_status_range}}\"'\n"
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
        _drain(fd, 3.0)  # warm-up + settle the post-attach transient re-runs
        assert subprocess.run(["tmux", "-L", sock, "list-clients", "-F", "#{session_name}"],
                              capture_output=True, text=True).stdout.strip() == "alpha"
        n_before = len(log.read_text().splitlines()) if log.exists() else 0
        t0 = time.time()
        os.write(fd, ("\x1b[<0;3;%dM" % ROWS).encode())   # press: col 3 of the bottom (workers) row = inside the worker chip
        os.write(fd, ("\x1b[<0;3;%dm" % ROWS).encode())   # release
        reran = False
        while time.time() - t0 < 4:                        # << status-interval (30s)
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
    """Two clients viewing different windows must each resolve THEIR OWN pane.
    #{pane_id} expands per-client, so each client's render carries a distinct #()
    command -> a distinct job -> the script sees that client's own pane (both
    panes appear in the log). The no-arg fallback (_selected_pane, `tmux
    display-message` with no -c) can't tell clients apart and would resolve a
    single pane for both bars."""
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
