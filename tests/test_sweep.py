import os
import subprocess
import sys
import time

import pytest

from dockwright import paths, state, sweep
from dockwright import terminal


def _reset_driver(monkeypatch):
    # Reset the process-wide cache so each _terminal_ls test gets a fresh
    # TmuxDriver (the only backend).
    terminal._DRIVER = None


@pytest.fixture
def fresh_orchestrator_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(paths, "DONE", tmp_path / "done")
    monkeypatch.setattr(paths, "TURN_ENDS", tmp_path / "turn-ends")
    monkeypatch.setattr(paths, "QUESTIONS", tmp_path / "questions")
    monkeypatch.setattr(paths, "ANSWERS", tmp_path / "answers")
    monkeypatch.setattr(paths, "CLOSED", tmp_path / "closed")
    monkeypatch.setattr(paths, "HANDOFFS", tmp_path / "handoffs")
    monkeypatch.setattr(paths, "PRESETS", tmp_path / "presets")
    monkeypatch.setattr(paths, "SLOTS", tmp_path / "slots")
    monkeypatch.setattr(paths, "MANAGER_MEMORY", tmp_path / "manager-memory")
    monkeypatch.setattr(paths, "ARCHITECT", tmp_path / "architect")
    monkeypatch.delenv("CLAUDE_SWEEP_MCP_IMAGES", raising=False)
    paths.ensure_dirs()
    yield tmp_path


def _dead_pid() -> int:
    proc = subprocess.Popen(["/bin/sleep", "0"])
    proc.wait()
    return proc.pid


def _write_active(sid, *, pid, name="task-x", agent="worker", window_id="42",
                  started_at=None, last_turn_at=None):
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {
        "claude_sid": sid, "agent": agent, "name": name, "window_id": window_id,
        "pid": pid, "started_at": started_at or time.time(),
        "last_turn_at": last_turn_at, "state": "idle",
    })


def _write_question(qid, worker_sid, manager=None):
    target = paths.QUESTIONS / manager if manager else paths.QUESTIONS
    target.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(target / f"{qid}.json", {
        "question_id": qid, "worker_sid": worker_sid, "question": "help?",
        "asked_at": time.time(),
    })


def test_dead_record_flagged_with_evidence(fresh_orchestrator_dir):
    started = time.time() - 3600
    _write_active("sid-dead", pid=_dead_pid(), name="fix-tests",
                  started_at=started, last_turn_at=started + 60)
    findings = sweep.scan_dead_active_records(set())
    assert len(findings) == 1
    f = findings[0]
    assert f["claude_sid"] == "sid-dead"
    assert f["name"] == "fix-tests"
    assert f["agent"] == "worker"
    assert f["pid"] > 0
    assert f["started_at"] == started
    assert f["last_turn_at"] == started + 60
    assert f["path"].endswith("active/sid-dead.json")


def test_live_pid_never_flagged(fresh_orchestrator_dir):
    _write_active("sid-live", pid=os.getpid())
    assert sweep.scan_dead_active_records(set()) == []


def test_dead_record_with_pending_question_not_flagged_scoped(fresh_orchestrator_dir):
    _write_active("sid-q", pid=_dead_pid())
    _write_question("q1", "sid-q", manager="some-mgr")
    pending = sweep._pending_question_sids()
    assert sweep.scan_dead_active_records(pending) == []


def test_dead_record_with_pending_question_not_flagged_legacy_flat(fresh_orchestrator_dir):
    _write_active("sid-q2", pid=_dead_pid())
    _write_question("q2", "sid-q2", manager=None)
    pending = sweep._pending_question_sids()
    assert sweep.scan_dead_active_records(pending) == []


def test_non_int_pid_and_unparseable_records_skipped(fresh_orchestrator_dir):
    _write_active("sid-no-pid", pid="not-an-int")
    (paths.ACTIVE / "garbage.json").write_text("{not json")
    assert sweep.scan_dead_active_records(set()) == []


def _terminal_ls_payload():
    return [
        {"wm_class": "claude-workers", "tabs": [
            {"title": "[w] fix-tests", "windows": [
                {"id": 42, "cwd": "/tmp/wt-a"}]},
            {"title": "[w] stale-task", "windows": [
                {"id": 87, "cwd": "/tmp/wt-b"}]},
            {"title": "[w] crashed-with-q", "windows": [
                {"id": 99, "cwd": "/tmp/wt-c"}]},
        ]},
        {"wm_class": "mgr", "tabs": [
            {"title": "manager", "windows": [{"id": 7, "cwd": "/tmp/mgr"}]},
        ]},
    ]


def test_orphan_window_flagged_with_evidence(fresh_orchestrator_dir):
    orphans = sweep.scan_orphan_terminal_windows(_terminal_ls_payload(), protected=set())
    assert {o["window_id"] for o in orphans} == {"42", "87", "99"}
    o87 = next(o for o in orphans if o["window_id"] == "87")
    assert o87["tab_title"] == "[w] stale-task"
    assert o87["cwd"] == "/tmp/wt-b"


def test_window_backed_by_active_record_not_flagged(fresh_orchestrator_dir):
    _write_active("sid-a", pid=os.getpid(), window_id="42")
    protected = sweep._protected_window_ids(set())
    orphans = sweep.scan_orphan_terminal_windows(_terminal_ls_payload(), protected)
    assert "42" not in {o["window_id"] for o in orphans}


def test_window_backed_by_legacy_iterm_sid_field_not_flagged(fresh_orchestrator_dir):
    state.write_json_atomic(paths.ACTIVE / "sid-legacy.json", {
        "claude_sid": "sid-legacy", "agent": "worker", "name": "old",
        "iterm_sid": "87", "pid": os.getpid(),
    })
    protected = sweep._protected_window_ids(set())
    assert "87" in protected


def test_closed_record_with_pending_question_protects_window(fresh_orchestrator_dir):
    state.write_json_atomic(paths.CLOSED / "sid-c.json", {
        "claude_sid": "sid-c", "name": "crashed-with-q", "window_id": "99",
        "closed_at": time.time(), "closed_reason": "idle>7200s",
    })
    _write_question("q9", "sid-c", manager="some-mgr")
    protected = sweep._protected_window_ids(sweep._pending_question_sids())
    orphans = sweep.scan_orphan_terminal_windows(_terminal_ls_payload(), protected)
    assert "99" not in {o["window_id"] for o in orphans}


def test_closed_record_without_question_does_not_protect(fresh_orchestrator_dir):
    state.write_json_atomic(paths.CLOSED / "sid-d.json", {
        "claude_sid": "sid-d", "window_id": "87", "closed_at": time.time(),
    })
    protected = sweep._protected_window_ids(sweep._pending_question_sids())
    assert "87" not in protected


def test_non_workers_os_window_ignored(fresh_orchestrator_dir):
    orphans = sweep.scan_orphan_terminal_windows(_terminal_ls_payload(), protected=set())
    assert "7" not in {o["window_id"] for o in orphans}


def test_orphan_scan_tolerates_non_dict_elements(fresh_orchestrator_dir):
    # _terminal_ls only validates the top level; a valid list with non-dict
    # elements (or non-list tabs/windows) must degrade per-element, not crash
    # the whole scan. The driver never emits this — guard, don't report.
    payload = [
        "not-a-dict",
        42,
        None,
        {"wm_class": "claude-workers", "tabs": "not-a-list"},
        {"wm_class": "claude-workers", "tabs": [
            "not-a-dict-tab",
            {"title": "[w] bad-windows", "windows": "not-a-list"},
            {"title": "[w] mixed", "windows": [
                "not-a-dict-window",
                {"id": 5, "cwd": "/tmp/ok"},
            ]},
        ]},
    ]
    orphans = sweep.scan_orphan_terminal_windows(payload, protected=set())
    assert [o["window_id"] for o in orphans] == ["5"]


def _panes_stdout(*rows):
    # rows: (session, window_id, window_name, pane_id, cwd, pane_title, pid)
    return "\n".join(terminal._LS_FS.join(r) for r in rows) + "\n"


def _fake_tmux_run(stdout=None, rc=0, stderr="", raise_exc=None):
    def run(args, *pargs, **kwargs):
        assert args[0] == "tmux" and "list-panes" in args
        if raise_exc is not None:
            raise raise_exc
        return subprocess.CompletedProcess(args, returncode=rc,
                                           stdout=stdout or "", stderr=stderr)
    return run


def test_terminal_ls_success(fresh_orchestrator_dir, monkeypatch):
    _reset_driver(monkeypatch)
    out = _panes_stdout(
        ("claude-workers", "@1", "w-a", "%4", "/tmp/wt-a", "[w] fix-tests", "111"),
    )
    monkeypatch.setattr(sweep.subprocess, "run", _fake_tmux_run(stdout=out))
    os_windows, err = sweep._terminal_ls()
    assert err is None
    assert os_windows[0]["wm_class"] == "claude-workers"


def test_terminal_ls_nonzero_rc_degrades(fresh_orchestrator_dir, monkeypatch):
    _reset_driver(monkeypatch)
    monkeypatch.setattr(sweep.subprocess, "run",
                        _fake_tmux_run(rc=1, stderr="boom"))
    os_windows, err = sweep._terminal_ls()
    assert os_windows is None and "exited 1" in err


def test_terminal_ls_oserror_degrades(fresh_orchestrator_dir, monkeypatch):
    _reset_driver(monkeypatch)
    monkeypatch.setattr(sweep.subprocess, "run",
                        _fake_tmux_run(raise_exc=FileNotFoundError("no tmux")))
    os_windows, err = sweep._terminal_ls()
    assert os_windows is None and "no tmux" in err


def test_terminal_ls_no_server_degrades_to_empty(fresh_orchestrator_dir, monkeypatch):
    # tmux's "no server running" is a benign empty fleet, not an error.
    _reset_driver(monkeypatch)
    monkeypatch.setattr(sweep.subprocess, "run",
                        _fake_tmux_run(rc=1, stderr="no server running on /tmp/x"))
    os_windows, err = sweep._terminal_ls()
    assert os_windows == [] and err is None


IMAGES = ("crystaldba/postgres-mcp",)


def _ps_rows():
    return [
        {"pid": 100, "ppid": 1, "etime": "13-04:00:11",
         "command": "/usr/local/bin/docker run -i --rm crystaldba/postgres-mcp"},
        {"pid": 200, "ppid": 150, "etime": "01:00:00",
         "command": "docker run -i --rm crystaldba/postgres-mcp"},
        {"pid": 150, "ppid": 120, "etime": "02:00:00", "command": "zsh -ic claude"},
        {"pid": 120, "ppid": 1, "etime": "02:00:00",
         "command": "/Users/testop/.local/bin/claude --resume abc"},
        {"pid": 300, "ppid": 1, "etime": "10:00",
         "command": "docker run -i --rm some-other/image"},
        {"pid": 400, "ppid": 1, "etime": "05:00", "command": "ssh-agent -l"},
    ]


def test_orphan_mcp_client_flagged_with_evidence(fresh_orchestrator_dir):
    findings = sweep.scan_orphan_mcp_clients(_ps_rows(), IMAGES)
    assert [f["pid"] for f in findings] == [100]
    f = findings[0]
    assert f["image"] == "crystaldba/postgres-mcp"
    assert f["age"] == "13-04:00:11"
    assert "docker run -i --rm" in f["command"]


def test_client_with_live_claude_ancestor_never_flagged(fresh_orchestrator_dir):
    # pid 200 -> 150 (zsh) -> 120 (claude): excluded by the ppid != 1 gate
    # (a client still parented to its session is by definition not orphaned).
    findings = sweep.scan_orphan_mcp_clients(_ps_rows(), IMAGES)
    assert 200 not in [f["pid"] for f in findings]


def test_chain_walk_reaches_claude_across_hops(fresh_orchestrator_dir):
    rows = [
        {"pid": 700, "ppid": 701, "etime": "01:00",
         "command": "docker run -i --rm crystaldba/postgres-mcp"},
        {"pid": 701, "ppid": 702, "etime": "01:00", "command": "zsh"},
        {"pid": 702, "ppid": 1, "etime": "01:00",
         "command": "/Users/testop/.local/bin/claude --resume xyz"},
    ]
    proc_by_pid = {r["pid"]: r for r in rows}
    assert sweep._chain_reaches_live_session(700, proc_by_pid) is True


def test_looks_like_session_matches_executable_token_only(fresh_orchestrator_dir):
    # Only argv[0]'s basename counts: a claude/codex token anywhere else in the
    # command line (mount args, container names, shell -c payloads) must not
    # make a process read as a session. Empirically every real session's
    # argv[0] is literally `claude`/`codex` (or an absolute path to it).
    assert sweep._looks_like_session("claude --settings {}")
    assert sweep._looks_like_session("/Users/testop/.local/bin/claude --resume abc")
    assert sweep._looks_like_session("codex resume xyz")
    assert not sweep._looks_like_session("docker run -v /mnt/claude:/data img")
    assert not sweep._looks_like_session("zsh -ic claude")
    assert not sweep._looks_like_session("")


def test_claude_token_in_client_args_no_longer_hides_orphan(fresh_orchestrator_dir):
    # A PPID==1 docker client whose own command carries a claude-shaped token
    # (here a container name) used to read as a session at the walk's first
    # step and hide the orphan. With argv[0]-only matching it is flagged.
    rows = [{"pid": 500, "ppid": 1, "etime": "01:00",
             "command": "docker run -i --rm --name claude crystaldba/postgres-mcp"}]
    assert [f["pid"] for f in sweep.scan_orphan_mcp_clients(rows, IMAGES)] == [500]


def test_path_arg_ending_in_claude_does_not_hide_orphan(fresh_orchestrator_dir):
    # A bare path token ending in /claude basename-matches the loose matcher
    # and used to hide this genuine orphan.
    rows = [{"pid": 800, "ppid": 1, "etime": "01:00",
             "command": "docker run -i --rm --mount-from /mnt/claude crystaldba/postgres-mcp"}]
    assert [f["pid"] for f in sweep.scan_orphan_mcp_clients(rows, IMAGES)] == [800]


def test_unlisted_image_and_non_docker_ignored(fresh_orchestrator_dir):
    findings = sweep.scan_orphan_mcp_clients(_ps_rows(), IMAGES)
    pids = [f["pid"] for f in findings]
    assert 300 not in pids and 400 not in pids


def test_ancestor_walk_survives_ppid_cycles(fresh_orchestrator_dir):
    rows = [
        {"pid": 600, "ppid": 601, "etime": "01:00",
         "command": "docker run -i --rm crystaldba/postgres-mcp"},
        {"pid": 601, "ppid": 600, "etime": "01:00", "command": "zsh"},
    ]
    # Not PPID==1, so not a candidate — but the walk must not loop forever
    # when invoked on a cyclic snapshot.
    assert sweep._chain_reaches_live_session(600, {r["pid"]: r for r in rows}) is False


def test_leaked_containers_flagged_when_zero_clients(fresh_orchestrator_dir):
    containers = [
        {"container_id": "abc123", "image": "crystaldba/postgres-mcp", "age": "2 weeks ago"},
        {"container_id": "def456", "image": "crystaldba/postgres-mcp", "age": "3 days ago"},
        {"container_id": "zzz999", "image": "unrelated/img", "age": "1 hour ago"},
    ]
    ps_rows = [r for r in _ps_rows() if r["pid"] not in (100, 200)]  # no clients
    leaked, notes = sweep.scan_leaked_mcp_containers(containers, ps_rows, IMAGES)
    assert [c["container_id"] for c in leaked] == ["abc123", "def456"]
    assert notes == []


def test_containers_ambiguous_when_clients_exist(fresh_orchestrator_dir):
    containers = [
        {"container_id": "abc123", "image": "crystaldba/postgres-mcp", "age": "2 weeks ago"},
        {"container_id": "def456", "image": "crystaldba/postgres-mcp", "age": "3 days ago"},
        {"container_id": "ghi789", "image": "crystaldba/postgres-mcp", "age": "1 day ago"},
    ]
    ps_rows = _ps_rows()  # pids 100 + 200 are clients of this image
    leaked, notes = sweep.scan_leaked_mcp_containers(containers, ps_rows, IMAGES)
    assert leaked == []
    assert len(notes) == 1
    assert "3 containers vs 2 clients" in notes[0]
    assert "1 likely leaked" in notes[0]


def test_containers_match_clients_no_findings(fresh_orchestrator_dir):
    containers = [
        {"container_id": "abc123", "image": "crystaldba/postgres-mcp", "age": "1 day ago"},
        {"container_id": "def456", "image": "crystaldba/postgres-mcp", "age": "1 day ago"},
    ]
    leaked, notes = sweep.scan_leaked_mcp_containers(containers, _ps_rows(), IMAGES)
    assert leaked == [] and notes == []


def test_seconds_old_containers_not_flagged_as_leaked(fresh_orchestrator_dir):
    # docker ps runs AFTER the ps snapshot: a container whose client started in
    # between has no client in the snapshot and would false-flag as leaked.
    # Sub-minute docker RunningFor strings are all seconds-scale — skip those,
    # but say so (no silent caps).
    containers = [
        {"container_id": "old123", "image": "crystaldba/postgres-mcp", "age": "2 weeks ago"},
        {"container_id": "new456", "image": "crystaldba/postgres-mcp", "age": "5 seconds ago"},
        {"container_id": "new789", "image": "crystaldba/postgres-mcp",
         "age": "Less than a second ago"},
    ]
    ps_rows = [r for r in _ps_rows() if r["pid"] not in (100, 200)]  # no clients
    leaked, notes = sweep.scan_leaked_mcp_containers(containers, ps_rows, IMAGES)
    assert [c["container_id"] for c in leaked] == ["old123"]
    assert any("2 container(s) younger than a minute" in n for n in notes)


def test_all_young_containers_only_note_no_flags(fresh_orchestrator_dir):
    containers = [
        {"container_id": "new456", "image": "crystaldba/postgres-mcp", "age": "9 seconds ago"},
    ]
    ps_rows = [r for r in _ps_rows() if r["pid"] not in (100, 200)]  # no clients
    leaked, notes = sweep.scan_leaked_mcp_containers(containers, ps_rows, IMAGES)
    assert leaked == []
    assert any("younger than a minute" in n for n in notes)


def test_young_containers_excluded_from_ambiguity_math(fresh_orchestrator_dir):
    # 2 mature containers vs 2 clients balances out; the young third must not
    # tip the image into the "3 vs 2, likely leaked" ambiguity note.
    containers = [
        {"container_id": "abc123", "image": "crystaldba/postgres-mcp", "age": "2 weeks ago"},
        {"container_id": "def456", "image": "crystaldba/postgres-mcp", "age": "3 days ago"},
        {"container_id": "new789", "image": "crystaldba/postgres-mcp", "age": "4 seconds ago"},
    ]
    leaked, notes = sweep.scan_leaked_mcp_containers(containers, _ps_rows(), IMAGES)
    assert leaked == []
    assert not any("likely leaked" in n for n in notes)
    assert any("younger than a minute" in n for n in notes)


def test_mcp_images_env_override(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setenv("CLAUDE_SWEEP_MCP_IMAGES", "a/b, c/d ,,")
    assert sweep._mcp_images() == ("a/b", "c/d")
    monkeypatch.delenv("CLAUDE_SWEEP_MCP_IMAGES")
    assert sweep._mcp_images() == ("crystaldba/postgres-mcp",)


def test_ps_snapshot_parses_real_format(fresh_orchestrator_dir, monkeypatch):
    raw = ("  100     1 13-04:00:11 /usr/local/bin/docker run -i --rm crystaldba/postgres-mcp\n"
           "  200   150    01:00:00 zsh -ic claude\n"
           "bogus line that should be skipped\n")
    monkeypatch.setattr(sweep.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], returncode=0, stdout=raw, stderr=""))
    rows, err = sweep._ps_snapshot()
    assert err is None
    assert rows[0] == {"pid": 100, "ppid": 1, "etime": "13-04:00:11",
                       "command": "/usr/local/bin/docker run -i --rm crystaldba/postgres-mcp"}
    assert len(rows) == 2


def test_ps_snapshot_degrades(fresh_orchestrator_dir, monkeypatch):
    def boom(*a, **k):
        raise OSError("no ps")
    monkeypatch.setattr(sweep.subprocess, "run", boom)
    rows, err = sweep._ps_snapshot()
    assert rows is None and "no ps" in err


def test_ps_snapshot_no_parseable_rows_degrades(fresh_orchestrator_dir, monkeypatch):
    # rc=0 with nothing parseable must degrade, NOT read as "zero clients" —
    # a future --apply flagging every container off an empty snapshot would be
    # exactly the disaster invariant 3 forbids. Real ps always lists itself.
    monkeypatch.setattr(sweep.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], returncode=0, stdout="garbage\nmore garbage here too\n", stderr=""))
    rows, err = sweep._ps_snapshot()
    assert rows is None and "no parseable rows" in err


def test_image_matches_digest_pinned_form(fresh_orchestrator_dir):
    assert sweep._image_matches(
        "crystaldba/postgres-mcp@sha256:deadbeef", IMAGES) is True
    assert sweep._image_matches(
        "crystaldba/postgres-mcp-other@sha256:deadbeef", IMAGES) is False


def test_docker_containers_parses_and_filters(fresh_orchestrator_dir, monkeypatch):
    raw = ("abc123\tcrystaldba/postgres-mcp\t2 weeks ago\n"
           "def456\tcrystaldba/postgres-mcp:latest\t3 days ago\n"
           "zzz999\tunrelated/img\t1 hour ago\n")
    monkeypatch.setattr(sweep.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], returncode=0, stdout=raw, stderr=""))
    containers, err = sweep._docker_containers(IMAGES)
    assert err is None
    assert [c["container_id"] for c in containers] == ["abc123", "def456"]
    assert containers[1]["image"] == "crystaldba/postgres-mcp:latest"


def test_docker_containers_degrades_on_daemon_down(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setattr(sweep.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], returncode=1, stdout="", stderr="Cannot connect to the Docker daemon"))
    containers, err = sweep._docker_containers(IMAGES)
    assert containers is None and "Docker daemon" in err


def _snapshot(root):
    files = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            files[str(p.relative_to(root))] = p.read_bytes()
    return files


def _patch_snapshots(monkeypatch, *, terminal_ls=([], None), ps=([], None), docker=([], None)):
    monkeypatch.setattr(sweep, "_terminal_ls", lambda: terminal_ls)
    monkeypatch.setattr(sweep, "_ps_snapshot", lambda: ps)
    monkeypatch.setattr(sweep, "_docker_containers", lambda images: docker)


def test_main_reports_all_three_scan_classes(fresh_orchestrator_dir, monkeypatch, capsys):
    started = time.time() - 3600
    _write_active("sid-dead", pid=_dead_pid(), name="fix-tests",
                  started_at=started, last_turn_at=None)
    _write_active("sid-live", pid=os.getpid(), window_id="42", name="alive")
    # Two images: crystaldba has 1 orphan client (pid 100) + 2 containers ->
    # ambiguity note; other/mcp-img has 0 clients + 1 container -> flagged.
    monkeypatch.setenv("CLAUDE_SWEEP_MCP_IMAGES",
                       "crystaldba/postgres-mcp,other/mcp-img")
    orphan_client_rows = [r for r in _ps_rows() if r["pid"] != 200]
    containers = [
        {"container_id": "abc123", "image": "crystaldba/postgres-mcp", "age": "2 weeks ago"},
        {"container_id": "def456", "image": "crystaldba/postgres-mcp", "age": "3 days ago"},
        {"container_id": "xyz000abcdef", "image": "other/mcp-img", "age": "9 days ago"},
    ]
    _patch_snapshots(monkeypatch, terminal_ls=(_terminal_ls_payload(), None),
                     ps=(orphan_client_rows, None), docker=(containers, None))
    rc = sweep.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "report-only" in out
    assert "Dead active records (1):" in out
    assert "sid-dead.json" in out and "fix-tests" in out
    assert "last_turn_at=-" in out
    assert "Orphan terminal windows in 'claude-workers' (2):" in out
    assert "window 87" in out and "[w] stale-task" in out and "/tmp/wt-b" in out
    assert "window 42" not in out
    assert "Orphan MCP docker clients (1):" in out
    assert "pid=100" in out and "crystaldba/postgres-mcp" in out and "13-04:00:11" in out
    assert "Leaked MCP containers (1):" in out
    assert "xyz000abcdef"[:12] in out and "9 days ago" in out
    assert "2 containers vs 1 clients" in out and "1 likely leaked" in out
    assert "abc123" not in out  # ambiguous, never individually flagged
    assert "worktree pruning" not in out  # no operator config -> hint suppressed (default "")


def test_main_clean_state(fresh_orchestrator_dir, monkeypatch, capsys):
    _write_active("sid-live", pid=os.getpid(), window_id="42")
    _patch_snapshots(monkeypatch)
    rc = sweep.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("(none — clean)") == 4


def test_main_degrades_when_terminal_unavailable(fresh_orchestrator_dir, monkeypatch, capsys):
    _write_active("sid-dead", pid=_dead_pid())
    _patch_snapshots(monkeypatch, terminal_ls=(None, "tmux list-panes failed: no tmux"))
    rc = sweep.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Dead active records (1):" in out
    assert "scan skipped" in out and "no tmux" in out
    assert "partial" in out


def test_main_degrades_when_ps_unavailable(fresh_orchestrator_dir, monkeypatch, capsys):
    _patch_snapshots(monkeypatch, ps=(None, "ps failed: boom"))
    rc = sweep.main([])
    out = capsys.readouterr().out
    assert rc == 0
    # No ps data -> client liveness unknowable -> BOTH mcp scans skipped.
    assert "MCP docker scan skipped" in out and "boom" in out
    assert "Leaked MCP containers" not in out


def test_main_degrades_when_docker_unavailable(fresh_orchestrator_dir, monkeypatch, capsys):
    _patch_snapshots(monkeypatch, ps=(_ps_rows(), None),
                     docker=(None, "docker ps failed: daemon down"))
    rc = sweep.main([])
    out = capsys.readouterr().out
    assert rc == 0
    # Client scan still runs off ps; only the container side is skipped.
    assert "Orphan MCP docker clients (1):" in out
    assert "container scan skipped" in out and "daemon down" in out


def test_main_modifies_nothing(fresh_orchestrator_dir, monkeypatch, capsys):
    _write_active("sid-dead", pid=_dead_pid())
    _write_active("sid-live", pid=os.getpid())
    _write_question("q1", "sid-dead", manager="m")
    _patch_snapshots(monkeypatch, terminal_ls=(_terminal_ls_payload(), None),
                     ps=(_ps_rows(), None))
    before = _snapshot(fresh_orchestrator_dir)
    assert sweep.main([]) == 0
    assert _snapshot(fresh_orchestrator_dir) == before


def test_main_dry_run_alias_identical(fresh_orchestrator_dir, monkeypatch, capsys):
    _write_active("sid-dead", pid=_dead_pid())
    _patch_snapshots(monkeypatch)
    assert sweep.main([]) == 0
    plain = capsys.readouterr().out
    assert sweep.main(["--dry-run"]) == 0
    assert capsys.readouterr().out == plain


def test_main_unknown_arg_usage_error(fresh_orchestrator_dir, capsys):
    rc = sweep.main(["--apply"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "Usage" in captured.err
    assert captured.out == ""


def test_main_unpatched_terminal_ls_hits_conftest_absorb_and_degrades(
        fresh_orchestrator_dir, monkeypatch, capsys, no_live_tmux):
    # conftest's no_live_tmux guard absorbs tmux subprocess calls with rc=0 +
    # empty stdout, so an unpatched _terminal_ls returns an empty fleet (no
    # panes) — proving the suite can never touch live tmux through sweep. ps and
    # docker helpers are stubbed so no real host commands run.
    _reset_driver(monkeypatch)
    monkeypatch.setattr(sweep, "_ps_snapshot", lambda: ([], None))
    monkeypatch.setattr(sweep, "_docker_containers", lambda images: ([], None))
    rc = sweep.main([])
    out = capsys.readouterr().out
    assert rc == 0
    # Empty absorbed stdout -> no orphan windows; the scan runs cleanly.
    assert "Orphan terminal windows in 'claude-workers' (0):" in out


def test_cli_dispatch_wired():
    from dockwright import __main__ as cli
    import dockwright.sweep as sweep_mod
    called = {}
    orig = sweep_mod.main

    def fake_main(argv):
        called["argv"] = argv
        return 0

    try:
        sweep_mod.main = fake_main
        sys_argv = sys.argv
        sys.argv = ["orchestrator", "sweep", "--dry-run"]
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        assert called["argv"] == ["--dry-run"]
    finally:
        sweep_mod.main = orig
        sys.argv = sys_argv
