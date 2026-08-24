"""Behavioral tests for the finish_run() gates (gardener proposals 53614-1, 22586-7).

Harness: exec the REAL script via the headless path (GARDENER_HEADLESS=1) with a
scratch HOME — reaches finish_run() with no tmux. A stub `claude` on PATH writes
the digest's Status line; the apply-check tests seed the REAL gardener_postrun.py
+ gardener_apply.py (_seed_real_postrun), so the birth gate quarantines for real.
run.log is the observable; quiet cases are anchored by "gardener-postrun:" in the
log plus the surviving-proposal count in pending/ — quiet must be provably
"postrun ran and rejected nothing", never "postrun absent" (drift-guard-tests).

GARDENER_SCRIPT_UNDER_TEST exists for the boundary-mutation red proof: point it
at a scratch copy whose `-gt` thresholds are flipped to `-ge` and the silent
boundary cases MUST fail — proving they bind to the exact boundary, not to
generic silence. Default always binds to the repo script.
"""
import os
import re
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
        # Bounds the watchdog subshell's orphaned `sleep` (it inherits the
        # script's stdio; a big value would stall pipe-reading callers).
        "GARDENER_TIMEOUT_SEC": "3",
    })
    # loop-label-prefix.sh is sourced from the script's own repo dir, so
    # dockwright_module_enabled IS defined here and resolves config via these
    # vars BEFORE the scratch HOME — an ambient [modules] gardener=false would
    # short-circuit every case at "module-off".
    env.pop("DOCKWRIGHT_CONFIG", None)
    env.pop("XDG_CONFIG_HOME", None)
    # conftest's autouse isolation net sets DOCKWRIGHT_GARDENER_DIR for every
    # test; gardener_postrun.py honors it while gardener-run.sh derives its
    # dirs from HOME — pop it so every child process sees one scratch world.
    env.pop("DOCKWRIGHT_GARDENER_DIR", None)
    return env


def _write_stub_claude(bindir, *, proposal_src=None, exit_code=0):
    """Headless invocation is `claude -p --model … < <prompt-on-stdin>`.

    The prompt moved to STDIN when the lane went default-deny: it now carries the
    skill BODY (user-level skill discovery is gone with the settings sources), and
    a body opening with YAML frontmatter cannot be an argument — `---…` parses as
    an option. So scan argv AND stdin for run_id=. Optionally copies a prepared
    proposal into pending/$RUN_ID-1.md so the postrun birth gate has this run's
    proposal."""
    bindir.mkdir(parents=True, exist_ok=True)
    stub = bindir / "claude"
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
        {write_proposal}
        # The headless join now requires real digest CONTENT, not just a
        # self-reported Status line: a child that could read nothing still printed
        # `Status: ok`, and accepting that touched the cadence marker and told the
        # operator a digest was ready. Two independent checks guard it — a `## `
        # section AND a byte floor — because the prompt itself instructs those
        # headings, so a hollow child could echo them over empty bodies. A stub
        # standing in for a SUCCESSFUL run must therefore look like one.
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
    """Real post-processor + actuator in the scratch home: gates downstream of
    postrun must be tested in a world where postrun exists — its birth gate
    quarantines failing proposals before finish_run ever sees them."""
    scripts = home / ".claude" / "scripts"
    shutil.copy(REPO_SCRIPTS / "gardener_postrun.py", scripts / "gardener_postrun.py")
    shutil.copy(REPO_SCRIPTS / "gardener_apply.py", scripts / "gardener_apply.py")


def _git_init_claude_target(home):
    """classify_proposal needs a git-versioned target root; a non-git root is
    an env-lenient pass, so the quarantine tests git-init the scratch ~/.claude
    and track one target file."""
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
    """Schema-valid proposal whose diff either applies to TARGET_LINES (clean)
    or anchors on context that does not exist (drifted -> birth-gate
    quarantine). Target sits outside rules//agents/ so always_on_bytes is 0."""
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
    """stdout/stderr go to FILES, not pipes: the headless watchdog's orphaned
    `sleep` inherits the script's stdio and would hold a pipe open past exit."""
    out, err = tmp_path / "script-out.log", tmp_path / "script-err.log"
    with open(out, "w") as o, open(err, "w") as e:
        r = subprocess.run(["bash", str(SCRIPT), "--trigger", "force"],
                           env=_headless_env(home, bindir),
                           stdout=o, stderr=e, timeout=120)
    return r.returncode, out.read_text(), err.read_text()


def _run_log(gdir):
    p = gdir / "run.log"
    return p.read_text() if p.exists() else ""


# ---- Task 1: postrun quarantine surfacing (proposal 53614-1) ---------------


def test_applycheck_surfaces_quarantine(tmp_path):
    """A REAL postrun quarantine (drifted diff, real classifier, real git
    root) must reach run.log as REJECTED:N — the file moves to rejected/
    silently otherwise."""
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
    """Quiet must mean postrun-ran-and-rejected-nothing: no applycheck line,
    but the parsed summary is provably present and the proposal survived
    the birth gate into pending."""
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
    """The one genuine residual the old subprocess gate could not serve:
    postrun absent/crashed is swallowed by `|| true` — the summary parse
    must then fail LOUD, never read as a clean run."""
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    _write_stub_claude(tmp_path / "bin")
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    log = _run_log(gdir)
    assert "postrun-unparseable" in log, log
    assert "  finished  " in log, log


def _prose_brief_text(target):
    """kind: build-brief legitimately ships a prose ## Diff (no ```diff
    fence) — a postrun PASS class the old subprocess gate false-positived
    on (strict CLI check exits 2 on no-diff)."""
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
    """Postrun's pass classes must NOT resurface as failures: a build-brief's
    prose ## Diff is a deliberate no-diff PASS — the old subprocess gate
    false-positived exactly here."""
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


def test_applycheck_notify_is_wired():
    """notify() is un-exercisable under pytest — pin both branches' notify
    calls to executed lines adjacent to their behaviorally-proven run_log
    lines."""
    lines = SCRIPT.read_text().splitlines()
    rej = [i for i, l in enumerate(lines)
           if re.match(r'\s*run_log "applycheck" "REJECTED:', l)]
    unp = [i for i, l in enumerate(lines)
           if re.match(r'\s*run_log "applycheck" "postrun-unparseable', l)]
    assert len(rej) == 1 and len(unp) == 1, (rej, unp)
    assert re.match(
        r'\s*notify "gardener \$RUN_ID: \$postrun_rejected proposal\(s\) quarantined',
        lines[rej[0] + 1]), lines[rej[0] + 1]
    assert re.match(
        r'\s*notify "gardener \$RUN_ID: postrun failed/unparseable',
        lines[unp[0] + 1]), lines[unp[0] + 1]


# ---- Task 2: size/age-gated backlog escalation (proposal 22586-7) ----------


def _seed_pending_backlog(gdir, count, oldest_age_days=None):
    """Seed pending/*.md (names incidental — nothing in the script keys on
    them; these tests run without postrun, so the seeded count stays stable).
    oldest_age_days sets ONE file's mtime that many whole days + 1h back."""
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
    """-gt 20: exactly 20 stays silent. The finished-line anchor proves the
    silence is finish_run-ok silence, not an early exit (stop file, lock,
    module-off, missing preset all exit 0 with no backlog line)."""
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
    """15 whole days (integer division of now−mtime by 86400) > 14 → fires,
    even with a tiny backlog (count 1)."""
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
    """-gt 14: exactly 14 whole days stays silent; same ran-anchor as the
    count-boundary case."""
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    _seed_pending_backlog(gdir, 1, oldest_age_days=14)
    _write_stub_claude(tmp_path / "bin")
    rc, _, _ = _run_script(home, tmp_path / "bin", tmp_path)
    assert rc == 0
    log = _run_log(gdir)
    assert "  finished  " in log, log
    assert "  backlog  " not in log, log


def test_backlog_notify_is_wired():
    """Same executed-line + adjacency pin as the applycheck twin: the notify
    half is unreachable behaviorally, so bind it to the run_log line whose
    placement the behavioral cases prove (same branch, adjacent line)."""
    lines = SCRIPT.read_text().splitlines()
    hits = [i for i, l in enumerate(lines)
            if re.match(r'\s*run_log "backlog" ', l)]
    assert len(hits) == 1, hits
    assert re.match(r'\s*notify "gardener backlog: ', lines[hits[0] + 1]), \
        lines[hits[0] + 1]
