import asyncio
import subprocess
import pytest
from dockwright import terminal, paths
from dockwright.terminal import TmuxDriver, TerminalDriver, get_driver, WORKERS_OS_WINDOW_CLASS


def _capture_run(monkeypatch):
    calls = []
    def fake(args, *a, **kw):
        calls.append({"args": list(args), "kwargs": kw})
        text = kw.get("text")
        return subprocess.CompletedProcess(args, returncode=0,
                                           stdout=("" if text else b""),
                                           stderr=("" if text else b""))
    monkeypatch.setattr(subprocess, "run", fake)
    return calls


def test_get_driver_singleton_and_tmux_default(monkeypatch):
    monkeypatch.setattr(terminal, "_DRIVER", None)
    d = get_driver()
    assert isinstance(d, TmuxDriver)
    assert get_driver() is d  # singleton


def test_tmux_socket_default_and_env(monkeypatch):
    monkeypatch.delenv("DOCKWRIGHT_TMUX_SOCKET", raising=False)
    monkeypatch.delenv("CLAUDE_ORCH_TMUX_SOCKET", raising=False)
    assert TmuxDriver().socket() == "dockwright"
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "p1smoke")
    assert TmuxDriver().socket() == "p1smoke"


def test_socket_env_precedence(monkeypatch):
    d = TmuxDriver()
    monkeypatch.delenv("DOCKWRIGHT_TMUX_SOCKET", raising=False)
    monkeypatch.delenv("CLAUDE_ORCH_TMUX_SOCKET", raising=False)
    assert d.socket() == "dockwright"
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "legacy-sock")
    assert d.socket() == "legacy-sock"
    monkeypatch.setenv("DOCKWRIGHT_TMUX_SOCKET", "new-sock")
    assert d.socket() == "new-sock"


def test_tmux_current_pane_id(monkeypatch):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert TmuxDriver().current_pane_id() is None
    monkeypatch.setenv("TMUX_PANE", "%5")
    assert TmuxDriver().current_pane_id() == "%5"


def test_get_driver_always_tmux(monkeypatch):
    # get_driver() returns TmuxDriver unconditionally, regardless of any env.
    from dockwright import terminal as term
    for val in [None, "", "tmux", "weird", "kitty"]:
        monkeypatch.setattr(term, "_DRIVER", None)
        if val is None:
            monkeypatch.delenv("CLAUDE_ORCH_TERMINAL", raising=False)
        else:
            monkeypatch.setenv("CLAUDE_ORCH_TERMINAL", val)
        assert isinstance(term.get_driver(), TmuxDriver), val
    # singleton: same instance on repeat
    monkeypatch.setattr(term, "_DRIVER", None)
    d = term.get_driver()
    assert term.get_driver() is d


def _fake_exec_capture(monkeypatch, stdout=b"%7\n", rc=0):
    captured = {}
    class FakeProc:
        returncode = rc
        async def communicate(self): return (stdout, b"" if rc == 0 else b"err")
    async def fake_exec(*args, **kw):
        captured["args"] = list(args); return FakeProc()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return captured


def _fake_exec_capture_all(monkeypatch, stdout=b"%7\n", rc=0):
    calls = []
    class FakeProc:
        returncode = rc
        async def communicate(self): return (stdout, b"" if rc == 0 else b"err")
    async def fake_exec(*args, **kw):
        calls.append(list(args)); return FakeProc()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return calls


def test_tmux_conf_legacy_fallback_order(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    new = tmp_path / "dockwright.tmux.conf"
    legacy = tmp_path / "claude-orch.tmux.conf"
    monkeypatch.setattr(paths, "TMUX_CONF", new)
    monkeypatch.setattr(paths, "TMUX_CONF_LEGACY", legacy)
    d = TmuxDriver()
    assert d._tmux_base() == ["tmux", "-L", "S"]                      # both absent
    legacy.write_text("# legacy\n")
    assert d._tmux_base() == ["tmux", "-L", "S", "-f", str(legacy)]   # legacy only
    new.write_text("# new\n")
    assert d._tmux_base() == ["tmux", "-L", "S", "-f", str(new)]      # new wins


def test_tmux_spawn_new_session_sources_conf_workers(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    conf = tmp_path / "dockwright.tmux.conf"
    conf.write_text("# conf\n")
    monkeypatch.setattr(paths, "TMUX_CONF", conf)
    calls = _fake_exec_capture_all(monkeypatch, b"%7\n")
    async def none(self): return None
    monkeypatch.setattr(TmuxDriver, "find_group_pane", none)
    pane = asyncio.run(TmuxDriver().spawn(cwd="/tmp/x", title="t", argv=["zsh"],
                                          route_to_workers_window=True))
    assert pane == "%7"
    assert calls[0][:5] == ["tmux", "-L", "S", "-f", str(conf)]   # -f at birth
    assert "new-session" in calls[0]
    assert calls[1] == ["tmux", "-L", "S", "source-file", str(conf)]  # heal AFTER spawn


def test_tmux_spawn_new_session_sources_conf_mgr(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    conf = tmp_path / "dockwright.tmux.conf"
    conf.write_text("# conf\n")
    monkeypatch.setattr(paths, "TMUX_CONF", conf)
    calls = _fake_exec_capture_all(monkeypatch, b"%9\n")
    async def absent(self, s): return False
    monkeypatch.setattr(TmuxDriver, "_has_session", absent)
    asyncio.run(TmuxDriver().spawn(cwd="/c", title="t", argv=["zsh"],
                                   route_to_manager_session=True))
    assert "new-session" in calls[0]
    assert calls[1] == ["tmux", "-L", "S", "source-file", str(conf)]


def test_tmux_spawn_new_session_no_conf_no_source(monkeypatch):
    # conftest pins both conf names absent by default
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    calls = _fake_exec_capture_all(monkeypatch, b"%7\n")
    async def none(self): return None
    monkeypatch.setattr(TmuxDriver, "find_group_pane", none)
    asyncio.run(TmuxDriver().spawn(cwd="/x", title="t", argv=["zsh"],
                                   route_to_workers_window=True))
    assert len(calls) == 1
    assert "-f" not in calls[0]
    assert not any("source-file" in c for c in calls)


def test_tmux_spawn_new_window_never_sources_conf(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    conf = tmp_path / "dockwright.tmux.conf"
    conf.write_text("# conf\n")
    monkeypatch.setattr(paths, "TMUX_CONF", conf)
    calls = _fake_exec_capture_all(monkeypatch, b"%9\n")
    async def some(self): return "%1"
    monkeypatch.setattr(TmuxDriver, "find_group_pane", some)
    asyncio.run(TmuxDriver().spawn(cwd="/c", title="t", argv=["zsh"],
                                   route_to_workers_window=True))
    assert len(calls) == 1
    assert not any("source-file" in c for c in calls)


def test_tmux_spawn_failure_no_source(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    conf = tmp_path / "dockwright.tmux.conf"
    conf.write_text("# conf\n")
    monkeypatch.setattr(paths, "TMUX_CONF", conf)
    calls = _fake_exec_capture_all(monkeypatch, b"", rc=1)
    async def none(self): return None
    monkeypatch.setattr(TmuxDriver, "find_group_pane", none)
    with pytest.raises(RuntimeError):
        asyncio.run(TmuxDriver().spawn(cwd="/x", title="t", argv=["zsh"],
                                       route_to_workers_window=True))
    assert not any("source-file" in c for c in calls)


def test_tmux_spawn_new_session_sources_legacy_conf(monkeypatch, tmp_path):
    # 2026-07-07 incident replay: rename shipped, setup.sh not re-run yet —
    # only the legacy-named conf is on disk. Birth must still carry it.
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    legacy = tmp_path / "claude-orch.tmux.conf"
    legacy.write_text("# legacy\n")
    monkeypatch.setattr(paths, "TMUX_CONF", tmp_path / "dockwright.tmux.conf")
    monkeypatch.setattr(paths, "TMUX_CONF_LEGACY", legacy)
    calls = _fake_exec_capture_all(monkeypatch, b"%7\n")
    async def absent(self, s): return False
    monkeypatch.setattr(TmuxDriver, "_has_session", absent)
    asyncio.run(TmuxDriver().spawn(cwd="/c", title="t", argv=["zsh"],
                                   route_to_manager_session=True))
    assert calls[0][:5] == ["tmux", "-L", "S", "-f", str(legacy)]
    assert calls[1] == ["tmux", "-L", "S", "source-file", str(legacy)]


def test_tmux_spawn_workers_new_session(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    cap = _fake_exec_capture(monkeypatch, b"%7\n")
    async def none(self): return None
    monkeypatch.setattr(TmuxDriver, "find_group_pane", none)
    pane = asyncio.run(TmuxDriver().spawn(cwd="/tmp/x", title="[w] a",
                                          argv=["zsh","-ic","echo hi"],
                                          route_to_workers_window=True))
    assert pane == "%7"
    assert cap["args"] == ["tmux","-L","S","new-session","-d","-s","claude-workers",
                           "-n","[w] a","-c","/tmp/x","-P","-F","#{pane_id}",
                           "--","zsh","-ic","echo hi"]


def test_tmux_spawn_workers_existing_session(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    cap = _fake_exec_capture(monkeypatch, b"%9\n")
    async def some(self): return "%1"
    monkeypatch.setattr(TmuxDriver, "find_group_pane", some)
    pane = asyncio.run(TmuxDriver().spawn(cwd="/c", title="t", argv=["zsh"],
                                          route_to_workers_window=True))
    assert pane == "%9"
    assert cap["args"] == ["tmux","-L","S","new-window","-d","-t","claude-workers",
                           "-n","t","-c","/c","-P","-F","#{pane_id}","--","zsh"]


def test_tmux_spawn_target_match(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    cap = _fake_exec_capture(monkeypatch, b"%3\n")
    asyncio.run(TmuxDriver().spawn(cwd="/c", title="t", argv=["zsh"],
                                   target_window_match="claude-managers"))
    assert cap["args"] == ["tmux","-L","S","new-window","-d","-t",
                           "claude-managers","-n","t","-c","/c","-P","-F",
                           "#{pane_id}","--","zsh"]


def test_tmux_spawn_default_mode(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    cap = _fake_exec_capture(monkeypatch, b"%2\n")
    asyncio.run(TmuxDriver().spawn(cwd="/c", title="t", argv=["zsh"]))
    assert cap["args"] == ["tmux","-L","S","new-window","-d","-n","t","-c","/c",
                           "-P","-F","#{pane_id}","--","zsh"]
    assert "-t" not in cap["args"]


def test_tmux_spawn_raises_on_failure(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    _fake_exec_capture(monkeypatch, b"", rc=1)
    async def none(self): return None
    monkeypatch.setattr(TmuxDriver, "find_group_pane", none)
    with pytest.raises(RuntimeError, match="tmux .* failed"):
        asyncio.run(TmuxDriver().spawn(cwd="/c", title="t", argv=["zsh"],
                                       route_to_workers_window=True))


def test_tmux_find_group_pane(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    cap = _fake_exec_capture(monkeypatch, b"%4\n%5\n")
    assert asyncio.run(TmuxDriver().find_group_pane()) == "%4"
    assert cap["args"] == ["tmux","-L","S","list-panes","-t","claude-workers",
                           "-F","#{pane_id}"]
    _fake_exec_capture(monkeypatch, b"", rc=1)  # no session
    assert asyncio.run(TmuxDriver().find_group_pane()) is None


def test_tmux_pane_exists(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    cap = _fake_exec_capture(monkeypatch, b"%4\n%5\n%6\n")
    assert asyncio.run(TmuxDriver().pane_exists("%5")) is True
    assert cap["args"] == ["tmux","-L","S","list-panes","-a","-F","#{pane_id}"]
    _fake_exec_capture(monkeypatch, b"%4\n%5\n")
    assert asyncio.run(TmuxDriver().pane_exists("%9")) is False
    _fake_exec_capture(monkeypatch, b"", rc=1)
    assert asyncio.run(TmuxDriver().pane_exists("%5")) is False


def test_tmux_send_text_sequence_and_strip(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    calls = _capture_run(monkeypatch)
    TmuxDriver().send_text("%5", "line1\nline2\n\n")  # trailing newlines stripped
    # call[0] = ensure pin (fire-and-forget)
    assert calls[0]["args"] == ["tmux","-L","S","set-option","-s",
                                "extended-keys-format","xterm"]
    # call[1] = load-buffer with stripped bytes via stdin
    assert calls[1]["args"] == ["tmux","-L","S","load-buffer","-b","orch_5","-"]
    assert calls[1]["kwargs"]["input"] == b"line1\nline2"
    # call[2] = paste-buffer bracketed + delete
    assert calls[2]["args"] == ["tmux","-L","S","paste-buffer","-p","-d",
                                "-b","orch_5","-t","%5"]
    # call[3] = single Enter
    assert calls[3]["args"] == ["tmux","-L","S","send-keys","-t","%5","Enter"]


def test_tmux_send_text_swallows_and_default_submits_enter(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    def boom(args, *a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(subprocess, "run", boom)
    # _ensure_inject_safe's own swallow eats the first raise (the pin) before the
    # inject try; the inject try then swallows the rest. exception swallowed, None.
    assert TmuxDriver().send_text("%5", "x") is None


def test_tmux_send_text_checked_short_circuits(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    # load-buffer fails -> False, no paste, no enter (ensure pin runs first, ignored)
    seq = []
    def fake(args, *a, **kw):
        seq.append(list(args))
        rc = 1 if "load-buffer" in args else 0
        return subprocess.CompletedProcess(args, rc, "", "")
    monkeypatch.setattr(subprocess, "run", fake)
    assert TmuxDriver().send_text_checked("%5", "x") is False
    assert ["tmux","-L","S","paste-buffer","-p","-d","-b","orch_5","-t","%5"] not in seq
    assert not any("send-keys" in s for s in seq)


def test_tmux_send_text_checked_paste_fail(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    def fake(args, *a, **kw):
        rc = 1 if "paste-buffer" in args else 0
        return subprocess.CompletedProcess(args, rc, "", "")
    monkeypatch.setattr(subprocess, "run", fake)
    assert TmuxDriver().send_text_checked("%5", "x") is False  # no enter


def test_tmux_send_text_checked_success_and_text_mode(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    seen = {}
    def fake(args, *a, **kw):
        if "load-buffer" in args:
            seen["input"] = kw.get("input"); seen["text"] = kw.get("text")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(subprocess, "run", fake)
    assert TmuxDriver().send_text_checked("%5", "hi") is True
    assert seen["input"] == "hi" and seen["text"] is True  # str, text=True


def test_tmux_send_text_checked_ignores_failing_pin(monkeypatch):
    # Important #1: a failing set-option pin must NOT change the result
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    def fake(args, *a, **kw):
        if "set-option" in args:
            return subprocess.CompletedProcess(args, 1, "", "boom")  # pin fails
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(subprocess, "run", fake)
    assert TmuxDriver().send_text_checked("%5", "hi") is True  # still drives off inject


def test_tmux_capture_screen_argv(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    calls = _capture_run(monkeypatch)
    TmuxDriver().capture_screen("%5")
    assert calls[0]["args"] == ["tmux","-L","S","capture-pane","-p","-t","%5"]
    calls.clear()
    TmuxDriver().capture_screen_ansi("%5")
    assert calls[0]["args"] == ["tmux","-L","S","capture-pane","-p","-e","-t","%5"]
    assert calls[0]["kwargs"]["text"] is True


def _panes_stdout(*rows):
    # rows: (session, window_id, window_name, pane_id, cwd, pane_title, pid)
    return "\n".join(terminal._LS_FS.join(r) for r in rows) + "\n"


def test_tmux_ls_builds_pane_tree(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    out = _panes_stdout(
        ("claude-workers","@1","w-a","%4","/tmp/a","[w] a","111"),
        ("claude-workers","@2","w-b","%5","/tmp/b","[w] b","222"),
        ("other","@9","z","%9","/tmp/z","z","333"),
    )
    seen = {}
    def fake(args, *a, **kw):
        seen["args"] = list(args)
        payload = out if kw.get("text") else out.encode()
        return subprocess.CompletedProcess(args, 0, payload, payload)
    monkeypatch.setattr(subprocess, "run", fake)
    tree = TmuxDriver().ls()
    assert seen["args"] == ["tmux","-L","S","list-panes","-a","-F", terminal._LS_FORMAT]
    workers = [o for o in tree if o["wm_class"] == "claude-workers"]
    assert len(workers) == 1
    wins = [w for t in workers[0]["tabs"] for w in t["windows"]]
    assert {w["id"] for w in wins} == {"%4","%5"}
    w4 = next(w for w in wins if w["id"] == "%4")
    assert w4["cwd"] == "/tmp/a" and w4["title"] == "[w] a" and w4["pid"] == "111"
    assert any(o["wm_class"] == "other" for o in tree)


def test_tmux_ls_with_error_no_server(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    def fake(args, *a, **kw):
        msg = "no server running on /tmp/x"
        s = msg if kw.get("text") else msg.encode()
        return subprocess.CompletedProcess(args, 1, s, s)
    monkeypatch.setattr(subprocess, "run", fake)
    assert TmuxDriver().ls_with_error() == ([], None)  # benign empty fleet
    assert TmuxDriver().ls() == []


def test_tmux_ls_with_error_real_error(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    def fake(args, *a, **kw):
        msg = "boom"
        s = msg if kw.get("text") else msg.encode()
        return subprocess.CompletedProcess(args, 1, s, s)
    monkeypatch.setattr(subprocess, "run", fake)
    data, err = TmuxDriver().ls_with_error()
    assert data is None and "boom" in err
    assert TmuxDriver().ls() is None


def test_tmux_ls_timeouts(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    t = {}
    def cap(args, *a, **kw):
        t["v"] = kw.get("timeout")
        payload = b"" if not kw.get("text") else ""
        return subprocess.CompletedProcess(args, 0, payload, payload)
    monkeypatch.setattr(subprocess, "run", cap)
    TmuxDriver().ls(); assert t["v"] == 2
    TmuxDriver().ls_with_error(); assert t["v"] == 10


def test_tmux_close(monkeypatch):
    calls = _capture_run(monkeypatch)
    TmuxDriver().close("")
    assert calls == []  # no-op on empty
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    calls2 = _capture_run(monkeypatch)
    TmuxDriver().close("%5")
    assert calls2[0]["args"] == ["tmux","-L","S","kill-pane","-t","%5"]


def test_tmux_set_tab_title(monkeypatch):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    calls = _capture_run(monkeypatch)
    TmuxDriver().set_tab_title("hi")
    assert calls == []  # no-op without current pane
    monkeypatch.setenv("TMUX_PANE", "%5")
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    calls2 = _capture_run(monkeypatch)
    TmuxDriver().set_tab_title("hi")
    assert calls2[0]["args"] == ["tmux","-L","S","rename-window","-t","%5","hi"]


def test_tmux_base_includes_conf_only_when_present(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    monkeypatch.setattr(paths, "TMUX_CONF", tmp_path / "absent.conf")
    assert TmuxDriver()._tmux_base() == ["tmux", "-L", "S"]
    conf = tmp_path / "dockwright.tmux.conf"
    conf.write_text("# conf\n")
    monkeypatch.setattr(paths, "TMUX_CONF", conf)
    assert TmuxDriver()._tmux_base() == ["tmux", "-L", "S", "-f", str(conf)]

def test_tmux_set_tab_color_paints_window_status(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%5")
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    calls = _capture_run(monkeypatch)
    TmuxDriver().set_tab_color("#aa8800", "#443300")
    assert calls[0]["args"] == ["tmux","-L","S","set-window-option","-t","%5",
                                "window-status-current-style","bg=#aa8800,fg=#ffffff"]
    assert calls[1]["args"] == ["tmux","-L","S","set-window-option","-t","%5",
                                "window-status-style","bg=#443300,fg=#ffffff"]

def test_tmux_set_tab_color_noop_without_pane(monkeypatch):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    calls = _capture_run(monkeypatch)
    TmuxDriver().set_tab_color("#a", "#b")
    assert calls == []


def test_send_text_submit_false_omits_enter_tmux(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    terminal._DRIVER = None
    calls = _capture_run(monkeypatch)
    TmuxDriver().send_text("%5", "hi", submit=False)
    assert not any("send-keys" in c["args"] and c["args"][-1] == "Enter" for c in calls)
    assert any("paste-buffer" in c["args"] for c in calls)
    calls.clear()
    TmuxDriver().send_text("%5", "hi")  # default sends Enter
    assert any("send-keys" in c["args"] and c["args"][-1] == "Enter" for c in calls)


def test_tmux_spawn_manager_session_new(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    cap = _fake_exec_capture(monkeypatch, b"%9\n")
    async def absent(self, s): return False
    monkeypatch.setattr(TmuxDriver, "_has_session", absent)
    pane = asyncio.run(TmuxDriver().spawn(cwd="/c", title="t", argv=["zsh"],
                                          route_to_manager_session=True))
    assert pane == "%9"
    assert cap["args"] == ["tmux","-L","S","new-session","-d","-s","mgr",
                           "-n","t","-c","/c","-P","-F","#{pane_id}","--","zsh"]


def test_tmux_spawn_manager_session_existing(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    cap = _fake_exec_capture(monkeypatch, b"%10\n")
    async def present(self, s): return True
    monkeypatch.setattr(TmuxDriver, "_has_session", present)
    asyncio.run(TmuxDriver().spawn(cwd="/c", title="t", argv=["zsh"],
                                   route_to_manager_session=True))
    assert cap["args"][:7] == ["tmux","-L","S","new-window","-d","-t","mgr"]


def test_tmux_spawn_manager_session_overrides_target_match(monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_TMUX_SOCKET", "S")
    cap = _fake_exec_capture(monkeypatch, b"%11\n")
    async def present(self, s): return True
    monkeypatch.setattr(TmuxDriver, "_has_session", present)
    asyncio.run(TmuxDriver().spawn(cwd="/c", title="t", argv=["zsh"],
                                   route_to_manager_session=True,
                                   target_window_match="window_id:%14"))
    assert "mgr" in cap["args"] and "window_id:%14" not in cap["args"]


def test_terminal_module_is_fastmcp_free():
    import sys, subprocess as sp, textwrap
    code = textwrap.dedent('''
        import importlib, sys
        importlib.import_module("dockwright.terminal")
        bad = [m for m in sys.modules if m == "dockwright.mcp_server"
               or m == "mcp" or m.startswith("mcp.")]
        assert not bad, bad
        print("OK")
    ''')
    import os
    from pathlib import Path
    src = str(Path(__file__).resolve().parents[1] / "src")
    r = sp.run([sys.executable, "-c", code], capture_output=True, text=True,
               env={**os.environ, "PYTHONPATH": src})
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr
