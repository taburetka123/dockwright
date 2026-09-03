import json
import os
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

from tests.test_gardener_run_tmux import _seed_gardener_home

SCRIPT = Path(os.environ.get(
    "GARDENER_SCRIPT_UNDER_TEST",
    Path(__file__).resolve().parent.parent / "deploy" / "scripts" / "gardener-run.sh"))


def _headless_env(home, bindir):
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "PATH": f"{bindir}{os.pathsep}{env['PATH']}",
        "GARDENER_HEADLESS": "1",
        "GARDENER_CWD": str(home),
        "GARDENER_TIMEOUT_SEC": "3",
    })
    env.pop("DOCKWRIGHT_CONFIG", None)
    env.pop("XDG_CONFIG_HOME", None)
    env.pop("DOCKWRIGHT_GARDENER_DIR", None)
    return env


def _write_stub_claude(bindir, *, proposal_src=None, exit_code=0, stray_write=False):
    bindir.mkdir(parents=True, exist_ok=True)
    stub = bindir / "claude"
    stray = ('mkdir -p "$HOME/.claude/rules"\n'
             '        echo stray > "$HOME/.claude/rules/stray-fixture.md"\n'
             if stray_write else "")
    write_proposal = ""
    if proposal_src is not None:
        write_proposal = textwrap.dedent(f"""\
            PENDING="$HOME/.claude/dockwright/gardener/proposals/pending"
            mkdir -p "$PENDING"
            cp {str(proposal_src)!r} "$PENDING/$RUN_ID-1.md"
            """)
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        RUN_ID=""
        for a in "$@"; do
          case "$a" in
            *run_id=*) RUN_ID=$(printf '%s' "$a" | sed -n 's/.*run_id=\\([^ ]*\\).*/\\1/p') ;;
          esac
        done
        if [ -z "$RUN_ID" ] && [ ! -t 0 ]; then
          RUN_ID=$(sed -n 's/.*run_id=\\([^ ]*\\).*/\\1/p' | head -1)
        fi
        {write_proposal}{stray}
        echo "## Clusters"
        for i in $(seq 1 12); do
          echo "### $i. cluster-$i — 3 sessions"
          echo "Evidence: findings a$i, b$i, c$i. Recurrence across three runs;"
          echo "the adherence mention count for this file is now above the bar."
        done
        echo "## Proposals (ranked)"
        echo "1. tighten the projection budget — always_on_bytes: 0"
        echo "## Notes"
        echo "window: full; inputs: 431 findings"
        echo "Status: ok"
        exit {exit_code}
        """))
    stub.chmod(0o755)
    return stub


REPO_SCRIPTS = Path(__file__).resolve().parent.parent / "deploy" / "scripts"
TARGET_LINES = "alpha\nbeta\ngamma\n"


def _seed_real_postrun(home):
    scripts = home / ".claude" / "scripts"
    shutil.copy(REPO_SCRIPTS / "gardener_postrun.py", scripts / "gardener_postrun.py")
    shutil.copy(REPO_SCRIPTS / "gardener_apply.py", scripts / "gardener_apply.py")


def _git_init_claude_target(home):
    claude = home / ".claude"
    target = claude / "gardener-target.md"
    target.write_text(TARGET_LINES)
    for cmd in (["git", "init", "-q"],
                ["git", "add", "gardener-target.md"],
                ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-qm", "seed"]):
        subprocess.run(cmd, cwd=claude, check=True, capture_output=True)
    return target


def _proposal_text(target, *, drifted):
    ctx = ["WRONG-ONE", "WRONG-TWO", "WRONG-THREE"] if drifted \
        else ["alpha", "beta", "gamma"]
    return textwrap.dedent(f"""\
        ---
        id: finish-gates-fixture-1
        run_id: finish-gates-fixture
        cluster: finish-gates-harness
        lane: digest
        evidence_kind: findings
        members: [0f0e0d0c-0b0a-4990-8877-665544332211]
        targets: [{target}]
        kind: code-change
        always_on_bytes: 0
        flow_cost: none
        base_rev: deadbee
        expectation: harness fixture
        check_window_days: 14
        revert: n/a
        ---

        ## Evidence

        Harness fixture.

        ## Diff

        ```diff
        --- a/gardener-target.md
        +++ b/gardener-target.md
        @@ -1,3 +1,3 @@
         {ctx[0]}
        -{ctx[1]}
        +CHANGED
         {ctx[2]}
        ```
        """)


def _run_script(home, bindir, tmp_path):
    out, err = tmp_path / "script-out.log", tmp_path / "script-err.log"
    with open(out, "w") as o, open(err, "w") as e:
        r = subprocess.run(["bash", str(SCRIPT), "--trigger", "force"],
                           env=_headless_env(home, bindir),
                           stdout=o, stderr=e, timeout=120)
    return r.returncode, out.read_text(), err.read_text()


def _run_log(gdir):
    p = gdir / "run.log"
    return p.read_text() if p.exists() else ""


def test_applycheck_surfaces_quarantine(tmp_path):
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    _seed_real_postrun(home)
    target = _git_init_claude_target(home)
    src = tmp_path / "fixture-proposal.md"
    src.write_text(_proposal_text(target, drifted=True))
    _write_stub_claude(tmp_path / "bin", proposal_src=src)
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    log = _run_log(gdir)
    assert "REJECTED:1" in log, log
    assert list((gdir / "proposals" / "pending").glob("*.md")) == [], log
    assert len(list((gdir / "proposals" / "rejected").glob("*.md"))) == 1, log


def test_applycheck_quiet_on_clean_run(tmp_path):
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    _seed_real_postrun(home)
    target = _git_init_claude_target(home)
    src = tmp_path / "fixture-proposal.md"
    src.write_text(_proposal_text(target, drifted=False))
    _write_stub_claude(tmp_path / "bin", proposal_src=src)
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    log = _run_log(gdir)
    assert "  applycheck  " not in log, log
    assert "gardener-postrun:" in log, log
    assert "  finished  " in log, log
    assert len(list((gdir / "proposals" / "pending").glob("*.md"))) == 1, log


def test_applycheck_surfaces_postrun_crash(tmp_path):
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    _write_stub_claude(tmp_path / "bin")
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    log = _run_log(gdir)
    assert "postrun-unparseable" in log, log
    assert "  finished  " in log, log


def _prose_brief_text(target):
    return textwrap.dedent(f"""\
        ---
        id: finish-gates-fixture-2
        run_id: finish-gates-fixture
        cluster: finish-gates-harness
        lane: digest
        evidence_kind: findings
        members: [0f0e0d0c-0b0a-4990-8877-665544332211]
        targets: [{target}]
        kind: build-brief
        always_on_bytes: 0
        flow_cost: none
        base_rev: deadbee
        expectation: harness fixture
        check_window_days: 14
        revert: n/a
        ---

        ## Evidence

        Harness fixture.

        ## Diff

        Prose description of the intended change — no diff fence by design.
        """)


def test_applycheck_quiet_on_prose_diff_brief(tmp_path):
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    _seed_real_postrun(home)
    target = home / ".claude" / "gardener-target.md"
    target.write_text(TARGET_LINES)
    src = tmp_path / "fixture-proposal.md"
    src.write_text(_prose_brief_text(target))
    _write_stub_claude(tmp_path / "bin", proposal_src=src)
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    log = _run_log(gdir)
    assert "  applycheck  " not in log, log
    assert "gardener-postrun:" in log, log
    assert "  finished  " in log, log
    assert len(list((gdir / "proposals" / "pending").glob("*.md"))) == 1, log


def test_applycheck_notifications_stay_loud(tmp_path):
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    _seed_manager(home, "loud-quarantine", "general")
    _seed_real_postrun(home)
    target = _git_init_claude_target(home)
    src = tmp_path / "fixture-proposal.md"
    src.write_text(_proposal_text(target, drifted=True))
    _write_stub_claude(tmp_path / "bin", proposal_src=src)
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    log = _run_log(gdir)
    assert "REJECTED:1" in log, log
    assert not _routed_lines(home, "loud-quarantine", "quarantined"), \
        _outbox_lines(home, "loud-quarantine")


def test_postrun_crash_notification_stays_loud(tmp_path):
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    _seed_manager(home, "loud-unparseable", "general")
    _write_stub_claude(tmp_path / "bin")
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    log = _run_log(gdir)
    assert "postrun-unparseable" in log, log
    assert not _routed_lines(home, "loud-unparseable", "postrun failed"), \
        _outbox_lines(home, "loud-unparseable")


def _seed_pending_backlog(gdir, count, oldest_age_days=None):
    pending = gdir / "proposals" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (pending / f"backlog-{i:03d}.md").write_text("seeded\n")
    if oldest_age_days is not None and count:
        ts = time.time() - oldest_age_days * 86400 - 3600
        os.utime(pending / "backlog-000.md", (ts, ts))


def test_backlog_fires_above_count_threshold(tmp_path):
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    _seed_pending_backlog(gdir, 21)
    _write_stub_claude(tmp_path / "bin")
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    log = _run_log(gdir)
    assert "  backlog  " in log, log
    assert "pending=21" in log, log
    ledger = (gdir / "ledger.jsonl").read_text()
    assert '"event": "backlog"' in ledger, ledger
    assert '"pending": "21"' in ledger, ledger


def test_backlog_silent_at_count_threshold(tmp_path):
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    _seed_pending_backlog(gdir, 20)
    _write_stub_claude(tmp_path / "bin")
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    log = _run_log(gdir)
    assert "  finished  " in log, log
    assert "  backlog  " not in log, log
    ledger = (gdir / "ledger.jsonl").read_text()
    assert '"event": "backlog"' not in ledger, ledger


def test_backlog_fires_above_age_threshold(tmp_path):
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    _seed_pending_backlog(gdir, 1, oldest_age_days=15)
    _write_stub_claude(tmp_path / "bin")
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    log = _run_log(gdir)
    assert "  backlog  " in log, log
    assert "pending=1" in log, log
    assert "oldest_days=15" in log, log


def test_backlog_silent_at_age_threshold(tmp_path):
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    _seed_pending_backlog(gdir, 1, oldest_age_days=14)
    _write_stub_claude(tmp_path / "bin")
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    log = _run_log(gdir)
    assert "  finished  " in log, log
    assert "  backlog  " not in log, log


DEAD_PID = 0x7FFFFFFE


def _seed_manager(home, name, domain, *, started_at=1000.0, pid=None, **extra):
    active = home / ".claude" / "dockwright" / "active"
    active.mkdir(parents=True, exist_ok=True)
    record = {"claude_sid": name, "agent": "manager", "name": name,
              "parent_manager_name": None, "started_at": started_at,
              "pid": os.getpid() if pid is None else pid}
    if domain is not None:
        record["domain"] = domain
    record.update(extra)
    (active / f"{name}.json").write_text(json.dumps(record))


def _outbox_lines(home, manager_name):
    d = home / ".claude" / "dockwright" / "notify-outbox" / manager_name
    if not d.is_dir():
        return []
    return [json.loads(p.read_text())["line"] for p in sorted(d.glob("*.json"))]


def _routed_lines(home, manager_name, needle):
    return [l for l in _outbox_lines(home, manager_name) if needle in l]


def test_digest_ready_addresses_the_general_manager(tmp_path):
    home = tmp_path / "home"
    _seed_gardener_home(home)
    _seed_manager(home, "green-otter", "general")
    _write_stub_claude(tmp_path / "bin")
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    assert len(_routed_lines(home, "green-otter", "digest ready")) == 1, \
        _outbox_lines(home, "green-otter")


def test_peer_domain_managers_are_not_addressed(tmp_path):
    home = tmp_path / "home"
    _seed_gardener_home(home)
    _seed_manager(home, "green-otter", "general")
    _seed_manager(home, "blue-heron", "product")
    _seed_manager(home, "grey-marten", "job-search")
    _write_stub_claude(tmp_path / "bin")
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    assert len(_routed_lines(home, "green-otter", "digest ready")) == 1
    assert _outbox_lines(home, "blue-heron") == []
    assert _outbox_lines(home, "grey-marten") == []


def test_manager_without_a_domain_key_counts_as_general(tmp_path):
    home = tmp_path / "home"
    _seed_gardener_home(home)
    _seed_manager(home, "old-record", None)
    _write_stub_claude(tmp_path / "bin")
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    assert len(_routed_lines(home, "old-record", "digest ready")) == 1, \
        _outbox_lines(home, "old-record")


def test_newest_general_manager_wins_a_recreate_overlap(tmp_path):
    home = tmp_path / "home"
    _seed_gardener_home(home)
    _seed_manager(home, "old-mgr", "general", started_at=1000.0)
    _seed_manager(home, "new-mgr", "general", started_at=2000.0)
    _write_stub_claude(tmp_path / "bin")
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    assert len(_routed_lines(home, "new-mgr", "digest ready")) == 1
    assert _outbox_lines(home, "old-mgr") == []


def test_nested_manager_record_is_never_the_addressee(tmp_path):
    home = tmp_path / "home"
    _seed_gardener_home(home)
    _seed_manager(home, "ghost-mgr", "general", started_at=9000.0, nested=True)
    _seed_manager(home, "real-mgr", "general", started_at=1000.0)
    _write_stub_claude(tmp_path / "bin")
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    assert len(_routed_lines(home, "real-mgr", "digest ready")) == 1
    assert _outbox_lines(home, "ghost-mgr") == []


def test_a_dead_managers_record_is_not_an_addressee(tmp_path):
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    _seed_manager(home, "dead-mgr", "general", pid=DEAD_PID)
    _write_stub_claude(tmp_path / "bin")
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    assert _outbox_lines(home, "dead-mgr") == []
    assert list((home / ".claude" / "dockwright" / "notify-outbox"
                 ).rglob("*.json")) == []
    assert "fell back to a desktop notification" in _run_log(gdir)


def test_a_dead_newer_manager_does_not_shadow_a_live_older_one(tmp_path):
    home = tmp_path / "home"
    _seed_gardener_home(home)
    _seed_manager(home, "dead-successor", "general", started_at=2000.0, pid=DEAD_PID)
    _seed_manager(home, "live-predecessor", "general", started_at=1000.0)
    _write_stub_claude(tmp_path / "bin")
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    assert len(_routed_lines(home, "live-predecessor", "digest ready")) == 1
    assert _outbox_lines(home, "dead-successor") == []


def test_a_pid_we_may_not_signal_still_counts_as_alive(tmp_path):
    home = tmp_path / "home"
    _seed_gardener_home(home)
    _seed_manager(home, "root-owned-mgr", "general", pid=1)
    _write_stub_claude(tmp_path / "bin")
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    assert len(_routed_lines(home, "root-owned-mgr", "digest ready")) == 1, \
        _outbox_lines(home, "root-owned-mgr")


def test_a_boolean_pid_is_not_read_as_pid_one(tmp_path):
    home = tmp_path / "home"
    _seed_gardener_home(home)
    _seed_manager(home, "bool-pid-mgr", "general", pid=True)
    _write_stub_claude(tmp_path / "bin")
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    assert list((home / ".claude" / "dockwright" / "notify-outbox"
                 ).rglob("*.json")) == []


def test_no_general_manager_writes_no_outbox_entry(tmp_path):
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    _seed_manager(home, "blue-heron", "product")
    _write_stub_claude(tmp_path / "bin")
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    assert "  finished  " in _run_log(gdir)
    assert _outbox_lines(home, "blue-heron") == []
    outbox_root = home / ".claude" / "dockwright" / "notify-outbox"
    assert list(outbox_root.rglob("*.json")) == []


def test_backlog_escalation_addresses_the_general_manager_once(tmp_path):
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    _seed_pending_backlog(gdir, 21)
    _seed_manager(home, "green-otter", "general")
    _write_stub_claude(tmp_path / "bin")
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    assert "  backlog  " in _run_log(gdir)
    assert len(_routed_lines(home, "green-otter", "gardener backlog")) == 1, \
        _outbox_lines(home, "green-otter")


def test_stray_path_audit_addresses_the_general_manager(tmp_path):
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    _seed_manager(home, "green-otter", "general")
    _write_stub_claude(tmp_path / "bin", stray_write=True)
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    log = _run_log(gdir)
    assert "unattributed writes outside gardener/" in log, log
    assert len(_routed_lines(home, "green-otter", "writes outside gardener/")) == 1, \
        _outbox_lines(home, "green-otter")
    audit_files = list((gdir / "runs").rglob("audit-stray-paths.txt"))
    assert len(audit_files) == 1 and "rules/stray-fixture.md" in audit_files[0].read_text()


def test_clean_run_writes_no_audit_line(tmp_path):
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    _seed_manager(home, "green-otter", "general")
    _write_stub_claude(tmp_path / "bin")
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    assert "unattributed writes outside gardener/" not in _run_log(gdir)
    assert _routed_lines(home, "green-otter", "writes outside gardener/") == []


def test_outbox_entry_matches_the_drain_contract(tmp_path):
    home = tmp_path / "home"
    _seed_gardener_home(home)
    _seed_manager(home, "green-otter", "general")
    _write_stub_claude(tmp_path / "bin")
    before = time.time()
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    entries = sorted((home / ".claude" / "dockwright" / "notify-outbox"
                      / "green-otter").glob("*.json"))
    assert entries, "no outbox entry written"
    payload = json.loads(entries[0].read_text())
    assert isinstance(payload["line"], str) and payload["line"]
    assert payload["kind"] == "gardener"
    assert before <= payload["buffered_at"] <= time.time()
    assert not list((home / ".claude" / "dockwright" / "notify-outbox"
                     / "green-otter").glob("*.tmp")), "atomic write leaked a temp file"


def test_the_real_consumer_delivers_what_the_real_producer_wrote(tmp_path,
                                                                 monkeypatch,
                                                                 capsys):
    from dockwright import monitor, paths

    home = tmp_path / "home"
    _seed_gardener_home(home)
    _seed_manager(home, "green-otter", "general")
    _write_stub_claude(tmp_path / "bin")
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0

    monkeypatch.setattr(paths, "ROOT", home / ".claude" / "dockwright")
    capsys.readouterr()
    monitor._drain_notify_outbox("green-otter")
    out = capsys.readouterr()

    assert "digest ready" in out.out, (out.out, out.err)
    assert "outbox drain failed" not in out.err
    assert list(paths.notify_outbox_dir_for("green-otter").glob("*.json")) == [], \
        "a delivered entry must be unlinked, or it replays every drain"
