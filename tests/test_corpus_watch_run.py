"""Tests for deploy/scripts/corpus-watch-run.sh (eval-direction C2 eval
runner + finding writer).

Subprocess-exec's the REAL script with HOME=tmp_path. The gate binary is
stubbed via CORPUS_WATCH_GATE_BIN (env-overridable path), emitting a canned
coverage table + `sys.exit(int(os.environ["STUB_EXIT"]))`. `osascript` is
PATH-shimmed to observe notify calls; the script's own no-op-under-pytest
guard is overridden via CORPUS_WATCH_NOTIFY_FORCE=1 so notify counts are
observable (see corpus-watch-run.sh::_notify).
"""
import json
import os
import shutil
import signal
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "scripts" / "corpus-watch-run.sh"
RUNLOCK = SCRIPT.parent / "runlock.sh"


# ---- fixtures / helpers ---------------------------------------------------

def _seed_home(home):
    scripts = home / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(RUNLOCK, scripts / "runlock.sh")
    return home


def _write_stub_gate(bindir, failing_cases=None, stderr_line=None,
                      ran_investigation=False):
    """A tiny python file printing a canned coverage table (+ an optional
    'failing cases' line matching gardener_eval_gate.py's real wording) and
    exiting STUB_EXIT (env, defaulting to 0).

    stderr_line: an extra diagnostic printed to sys.stderr, mirroring
    gardener_eval_gate.py::run_pytest_suite's real behavior of putting the
    failing-test diagnostics on stderr, not stdout.

    ran_investigation: prints the gate's real per-suite
    "eval-gate: running investigation: ..." line, which corpus-watch-run.sh
    uses to decide whether the results/latest.json pointer is honest.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    stub = bindir / "stub_gate.py"
    lines = [
        "import os, sys",
        "print('eval-gate: coverage — 1 of 1 files mapped')",
        "print('  /x/y  covered-by: investigation')",
    ]
    if ran_investigation:
        lines.append(
            "print('eval-gate: running investigation: "
            "python -m evals.investigation.run_eval')")
    if failing_cases:
        lines.append(
            f"print('eval-gate: investigation failing cases: {failing_cases}')")
    if stderr_line:
        lines.append(f"print({stderr_line!r}, file=sys.stderr)")
    lines.append(
        "print('eval-gate: verdict (exit ' + os.environ.get(\"STUB_EXIT\", \"0\") + ')')")
    lines.append("sys.exit(int(os.environ.get('STUB_EXIT', '0')))")
    stub.write_text("\n".join(lines) + "\n")
    return stub


def _write_osascript_shim(bindir, capture_file):
    bindir.mkdir(parents=True, exist_ok=True)
    shim = bindir / "osascript"
    shim.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        echo "$@" >> {capture_file}
        exit 0
        """))
    shim.chmod(0o755)
    return shim


def _write_state(home, last_sha, files=3, bytes_=500):
    state_dir = home / ".claude" / "dockwright" / "corpus-watch"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(json.dumps(
        {"last_sha": last_sha, "drift_files": files, "drift_bytes": bytes_}))


def _state_path(home):
    return home / ".claude" / "dockwright" / "corpus-watch" / "state.json"


def _read_state(home):
    return json.loads(_state_path(home).read_text())


def _default_ledger_path(home):
    return home / ".claude" / "dockwright" / "gardener" / "ledger.jsonl"


def _read_ledger(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _findings(home):
    d = home / ".claude" / "dockwright" / "selffix" / "findings"
    return sorted(d.glob("corpus-watch-eval-*.md")) if d.is_dir() else []


def _env(home, bindir, gate_bin, stub_exit, notify_force=True, extra=None):
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "PATH": f"{bindir}{os.pathsep}{env['PATH']}",
        "CORPUS_WATCH_GATE_BIN": str(gate_bin),
        "STUB_EXIT": str(stub_exit),
    })
    if notify_force:
        env["CORPUS_WATCH_NOTIFY_FORCE"] = "1"
    else:
        env.pop("CORPUS_WATCH_NOTIFY_FORCE", None)
    if extra:
        env.update(extra)
    return env


def _run(home, bindir, gate_bin, stub_exit, sha, rng, targets="/x/y",
          gardener_dir=None, notify_force=True, extra=None, timeout=60):
    env = _env(home, bindir, gate_bin, stub_exit, notify_force=notify_force, extra=extra)
    argv = ["bash", str(SCRIPT), sha, rng, targets]
    if gardener_dir is not None:
        argv.append(str(gardener_dir))
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout)


# ---- syntax -----------------------------------------------------------

def test_script_syntax_ok():
    assert shutil.which("bash")
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)


# ---- exit 0: passed --------------------------------------------------------

def test_exit0_passed_ledger_bracket_no_finding_state_advance(tmp_path):
    home = tmp_path / "home"
    _seed_home(home)
    _write_state(home, "OLD", files=3, bytes_=500)
    gate = _write_stub_gate(tmp_path / "gatebin")
    bindir = tmp_path / "bin"
    gdir = tmp_path / "custom-gardener"

    r = _run(home, bindir, gate, 0, "NEWSHA", "OLD..NEWSHA", gardener_dir=gdir)
    assert r.returncode == 0, r.stderr

    events = _read_ledger(gdir / "ledger.jsonl")
    assert [e["type"] for e in events] == ["run_start", "run_end"]
    assert all(e.get("lane") == "corpus-watch" for e in events)
    assert events[0]["run_id"] == events[1]["run_id"]
    assert events[1]["status"] == "passed"
    assert events[1]["gate_exit"] == "0"

    assert not _findings(home)

    state = _read_state(home)
    assert state["last_sha"] == "NEWSHA"
    assert state["drift_files"] == 3
    assert state["drift_bytes"] == 500


def test_default_gardener_dir_used_when_4th_arg_omitted(tmp_path):
    home = tmp_path / "home"
    _seed_home(home)
    _write_state(home, "OLD")
    gate = _write_stub_gate(tmp_path / "gatebin")
    bindir = tmp_path / "bin"

    r = _run(home, bindir, gate, 0, "NEW", "OLD..NEW")  # no gardener_dir -> $4 omitted
    assert r.returncode == 0, r.stderr

    ledger = _default_ledger_path(home)
    assert ledger.exists()
    events = _read_ledger(ledger)
    assert [e["type"] for e in events] == ["run_start", "run_end"]


# ---- exit 1: behavioral RED ------------------------------------------------

def test_exit1_failed_writes_finding_and_notifies(tmp_path):
    home = tmp_path / "home"
    _seed_home(home)
    _write_state(home, "OLD")
    gate = _write_stub_gate(tmp_path / "gatebin", failing_cases="case_x")
    bindir = tmp_path / "bin"
    capture = tmp_path / "osascript-calls.log"
    _write_osascript_shim(bindir, capture)

    r = _run(home, bindir, gate, 1, "NEW1", "OLD..NEW1")
    assert r.returncode == 0, r.stderr

    findings = _findings(home)
    assert len(findings) == 1
    text = findings[0].read_text()
    assert "OLD..NEW1" in text
    assert "case_x" in text

    assert capture.exists()
    assert len(capture.read_text().splitlines()) == 1

    ledger = _read_ledger(_default_ledger_path(home))
    assert ledger[-1]["status"] == "failed"
    assert ledger[-1]["gate_exit"] == "1"
    assert _read_state(home)["last_sha"] == "NEW1"


def test_exit1_second_run_within_throttle_does_not_renotify(tmp_path):
    home = tmp_path / "home"
    _seed_home(home)
    _write_state(home, "OLD")
    gate = _write_stub_gate(tmp_path / "gatebin", failing_cases="case_x")
    bindir = tmp_path / "bin"
    capture = tmp_path / "osascript-calls.log"
    _write_osascript_shim(bindir, capture)

    r1 = _run(home, bindir, gate, 1, "NEW1", "OLD..NEW1")
    assert r1.returncode == 0, r1.stderr
    assert len(capture.read_text().splitlines()) == 1

    r2 = _run(home, bindir, gate, 1, "NEW2", "NEW1..NEW2")
    assert r2.returncode == 0, r2.stderr
    # Still within the 6h throttle window: no second notify call.
    assert len(capture.read_text().splitlines()) == 1
    assert _read_state(home)["last_sha"] == "NEW2"


# ---- exit 2: infra-suspect --------------------------------------------------

def test_exit2_infra_suspect_finding_and_notify_use_infra_marker(tmp_path):
    home = tmp_path / "home"
    _seed_home(home)
    _write_state(home, "OLD")
    gate = _write_stub_gate(tmp_path / "gatebin")
    bindir = tmp_path / "bin"
    capture = tmp_path / "osascript-calls.log"
    _write_osascript_shim(bindir, capture)

    r = _run(home, bindir, gate, 2, "NEW1", "OLD..NEW1")
    assert r.returncode == 0, r.stderr

    findings = _findings(home)
    assert len(findings) == 1
    assert "infra-suspect" in findings[0].read_text()
    assert len(capture.read_text().splitlines()) == 1

    watch_dir = home / ".claude" / "dockwright" / "corpus-watch"
    assert (watch_dir / ".notify-marker-infra").exists()
    assert not (watch_dir / ".notify-marker-red").exists()

    ledger = _read_ledger(_default_ledger_path(home))
    assert ledger[-1]["status"] == "infra-suspect"
    assert ledger[-1]["gate_exit"] == "2"


def test_fresh_red_after_infra_notify_still_notifies_separate_marker(tmp_path):
    """The two verdict kinds use SEPARATE throttle markers (plan-review M12):
    an infra notification firing must never suppress a genuine, immediately
    following behavioral RED."""
    home = tmp_path / "home"
    _seed_home(home)
    _write_state(home, "OLD")
    gate = _write_stub_gate(tmp_path / "gatebin", failing_cases="case_y")
    bindir = tmp_path / "bin"
    capture = tmp_path / "osascript-calls.log"
    _write_osascript_shim(bindir, capture)

    r1 = _run(home, bindir, gate, 2, "NEW1", "OLD..NEW1")
    assert r1.returncode == 0, r1.stderr
    assert len(capture.read_text().splitlines()) == 1

    r2 = _run(home, bindir, gate, 1, "NEW2", "NEW1..NEW2")
    assert r2.returncode == 0, r2.stderr
    assert len(capture.read_text().splitlines()) == 2

    findings = _findings(home)
    assert len(findings) == 2


# ---- exit 4: nothing mapped (harmless tick/run race) -----------------------

def test_exit4_anomaly_no_finding_no_notify_state_advanced(tmp_path):
    home = tmp_path / "home"
    _seed_home(home)
    _write_state(home, "OLD")
    gate = _write_stub_gate(tmp_path / "gatebin")
    bindir = tmp_path / "bin"
    capture = tmp_path / "osascript-calls.log"
    _write_osascript_shim(bindir, capture)

    r = _run(home, bindir, gate, 4, "NEW", "OLD..NEW")
    assert r.returncode == 0, r.stderr

    assert not _findings(home)
    assert not capture.exists()

    run_log = home / ".claude" / "dockwright" / "corpus-watch" / "run.log"
    assert run_log.exists()
    assert "anomaly" in run_log.read_text()

    ledger = _read_ledger(_default_ledger_path(home))
    assert ledger[-1]["status"] == "anomaly-unmapped"
    assert ledger[-1]["gate_exit"] == "4"
    assert _read_state(home)["last_sha"] == "NEW"


# ---- exit 5: partial coverage (defensive — gate contract has it) -----------

def test_exit5_partial_coverage_finding_red_marker_state_advance(tmp_path):
    """Tier-2 F1/F4: the gate contract now has exit 5 = mapped suites passed
    but >=1 target unmapped. Should be unreachable from corpus-watch (run.sh
    is handed only the mapped subset), but the defensive branch must treat it
    like the finding-writing path — finding marked `partial-coverage`, notify
    via the RED marker (a coverage gap is a real signal, not infra noise),
    state advances.

    RED-proof: against the pre-fix code exit 5 fell into the `*)` branch —
    status `infra-suspect`, infra marker. Verified; output pasted in the task
    report."""
    home = tmp_path / "home"
    _seed_home(home)
    _write_state(home, "OLD")
    gate = _write_stub_gate(tmp_path / "gatebin")
    bindir = tmp_path / "bin"
    capture = tmp_path / "osascript-calls.log"
    _write_osascript_shim(bindir, capture)

    r = _run(home, bindir, gate, 5, "NEW", "OLD..NEW")
    assert r.returncode == 0, r.stderr

    findings = _findings(home)
    assert len(findings) == 1
    assert "partial-coverage" in findings[0].read_text()
    assert len(capture.read_text().splitlines()) == 1

    watch_dir = home / ".claude" / "dockwright" / "corpus-watch"
    assert (watch_dir / ".notify-marker-red").exists()
    assert not (watch_dir / ".notify-marker-infra").exists()

    ledger = _read_ledger(_default_ledger_path(home))
    assert ledger[-1]["status"] == "partial-coverage"
    assert ledger[-1]["gate_exit"] == "5"
    assert _read_state(home)["last_sha"] == "NEW"


# ---- Critical-1: stderr must be captured into the finding ------------------

def test_exit1_finding_includes_stderr_diagnostics(tmp_path):
    """Post-rung-3, a failed pytest suite's failing-test diagnostics print to
    STDERR (gardener_eval_gate.py::run_pytest_suite). A stdout-only capture
    leaves the exit-1 finding's entire diagnostic payload empty."""
    home = tmp_path / "home"
    _seed_home(home)
    _write_state(home, "OLD")
    gate = _write_stub_gate(tmp_path / "gatebin", failing_cases="case_x",
                             stderr_line="eval-gate: pytest suite FAILED — "
                                          "DIAG: assertion failed at foo.py:42")
    bindir = tmp_path / "bin"
    _write_osascript_shim(bindir, tmp_path / "osascript-calls.log")

    r = _run(home, bindir, gate, 1, "NEW1", "OLD..NEW1")
    assert r.returncode == 0, r.stderr

    findings = _findings(home)
    assert len(findings) == 1
    text = findings[0].read_text()
    assert "DIAG: assertion failed at foo.py:42" in text


# ---- Critical-2: results pointer is suite-aware, not unconditional ---------

def test_exit1_finding_omits_results_pointer_when_investigation_did_not_run(tmp_path):
    """The pytest suite (the rung-3 default-map route) writes no
    results/latest.json — an unconditional pointer to it is a stale-artifact
    claim for every pytest-suite verdict."""
    home = tmp_path / "home"
    _seed_home(home)
    _write_state(home, "OLD")
    gate = _write_stub_gate(tmp_path / "gatebin", failing_cases="case_x")
    bindir = tmp_path / "bin"
    _write_osascript_shim(bindir, tmp_path / "osascript-calls.log")

    r = _run(home, bindir, gate, 1, "NEW1", "OLD..NEW1")
    assert r.returncode == 0, r.stderr

    findings = _findings(home)
    assert len(findings) == 1
    text = findings[0].read_text()
    assert "evals/investigation/results/latest.json" not in text


def test_exit1_finding_includes_results_pointer_when_investigation_ran(tmp_path):
    home = tmp_path / "home"
    _seed_home(home)
    _write_state(home, "OLD")
    gate = _write_stub_gate(tmp_path / "gatebin", failing_cases="case_x",
                             ran_investigation=True)
    bindir = tmp_path / "bin"
    _write_osascript_shim(bindir, tmp_path / "osascript-calls.log")

    r = _run(home, bindir, gate, 1, "NEW1", "OLD..NEW1")
    assert r.returncode == 0, r.stderr

    findings = _findings(home)
    assert len(findings) == 1
    text = findings[0].read_text()
    assert "evals/investigation/results/latest.json" in text


# ---- Important-1: guaranteed terminal run_end ------------------------------

def test_findings_dir_unwritable_still_writes_error_run_end_and_skips_advance(tmp_path):
    """A write failure inside _write_finding (e.g. an unwritable findings
    dir) must not silently drop the ledger bracket or silently advance
    state — the trap must close it out as status=error and leave state
    untouched for the next tick's fail-closed re-examination."""
    home = tmp_path / "home"
    _seed_home(home)
    _write_state(home, "OLD")
    gate = _write_stub_gate(tmp_path / "gatebin", failing_cases="case_x")
    bindir = tmp_path / "bin"
    capture = tmp_path / "osascript-calls.log"
    _write_osascript_shim(bindir, capture)

    findings_dir = home / ".claude" / "dockwright" / "selffix" / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)
    findings_dir.chmod(0o000)
    try:
        r = _run(home, bindir, gate, 1, "NEW1", "OLD..NEW1")
    finally:
        findings_dir.chmod(0o755)

    assert r.returncode != 0

    ledger = _read_ledger(_default_ledger_path(home))
    assert [e["type"] for e in ledger] == ["run_start", "run_end"]
    assert ledger[-1]["status"] == "error"
    assert ledger[-1]["gate_exit"] == "1"

    assert _read_state(home)["last_sha"] == "OLD"

    run_log = home / ".claude" / "dockwright" / "corpus-watch" / "run.log"
    assert run_log.exists()
    assert "error" in run_log.read_text()

    # The failure happens inside _write_finding, before the notify branch —
    # no stray desktop notification either.
    assert not capture.exists()


# ---- Minor-1: notify-leak guard (PYTEST_CURRENT_TEST, no FORCE) ------------

def test_exit1_notify_skipped_without_force_under_pytest_current_test(tmp_path):
    """Without CORPUS_WATCH_NOTIFY_FORCE, and with PYTEST_CURRENT_TEST
    present (pytest sets this in its own env for every running test, and
    _env() copies os.environ), the script's no-op notify guard must fire:
    the finding is still written, but osascript must never be invoked."""
    assert "PYTEST_CURRENT_TEST" in os.environ
    home = tmp_path / "home"
    _seed_home(home)
    _write_state(home, "OLD")
    gate = _write_stub_gate(tmp_path / "gatebin", failing_cases="case_x")
    bindir = tmp_path / "bin"
    capture = tmp_path / "osascript-calls.log"
    _write_osascript_shim(bindir, capture)

    r = _run(home, bindir, gate, 1, "NEW1", "OLD..NEW1", notify_force=False)
    assert r.returncode == 0, r.stderr

    findings = _findings(home)
    assert len(findings) == 1

    assert not capture.exists()


# ---- lock busy --------------------------------------------------------

def test_lock_busy_exits_zero_no_ledger_no_state_advance(tmp_path):
    home = tmp_path / "home"
    _seed_home(home)
    _write_state(home, "OLD")
    lock = home / ".claude" / "locks" / "analyst-run.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(os.getpid()))
    gate = _write_stub_gate(tmp_path / "gatebin")
    bindir = tmp_path / "bin"

    before = _state_path(home).read_text()
    r = _run(home, bindir, gate, 0, "NEW", "OLD..NEW")
    assert r.returncode == 0, r.stderr

    assert not _default_ledger_path(home).exists()
    assert _state_path(home).read_text() == before
    assert not _findings(home)


# ---- Important-2 (fix wave 2): split traps, SIGTERM must terminate --------

def test_sigterm_mid_run_single_run_end_state_not_advanced_lock_released(tmp_path):
    """bash defers a trapped signal until the current foreground command
    completes (documented bash behavior, confirmed empirically here): a
    SIGTERM sent while the gate child is still running is NOT delivered
    until that child exits on its own. A bare `trap _cleanup EXIT INT TERM`
    then runs the cleanup handler (releasing the lock + writing an error
    run_end) and returns WITHOUT exiting — so bash just continues the
    script from right after the gate invocation: it computes RC, advances
    state, and appends a SECOND, contradictory run_end for the same
    run_id. The fix splits the trap so INT/TERM explicitly terminate
    (`_cleanup; exit 143`) instead of falling back into the script.

    Reproduced against a stub gate that sleeps a few seconds — long enough
    to still be running when the SIGTERM is sent, so delivery is deferred
    until it exits."""
    home = tmp_path / "home"
    _seed_home(home)
    _write_state(home, "OLD")
    bindir = tmp_path / "bin"

    gatebin = tmp_path / "gatebin"
    gatebin.mkdir(parents=True, exist_ok=True)
    stub = gatebin / "stub_gate.py"
    stub.write_text(textwrap.dedent("""\
        import time, sys, os
        time.sleep(4)
        sys.exit(int(os.environ.get("STUB_EXIT", "0")))
        """))

    env = _env(home, bindir, stub, 0)
    proc = subprocess.Popen(
        ["bash", str(SCRIPT), "NEW1", "OLD..NEW1", "/x/y"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        ledger_path = _default_ledger_path(home)
        deadline = time.time() + 10
        while time.time() < deadline:
            if ledger_path.exists() and _read_ledger(ledger_path):
                break
            time.sleep(0.1)
        else:
            pytest.fail("run_start never appeared in the ledger")

        lock_dir = home / ".claude" / "locks" / "analyst-run.lock"
        assert lock_dir.is_dir()

        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail("script did not terminate promptly after SIGTERM")

        assert proc.returncode == 143

        events = _read_ledger(ledger_path)
        run_ends = [e for e in events if e["type"] == "run_end"]
        assert len(run_ends) == 1, run_ends
        assert run_ends[0]["status"] == "error"
        assert run_ends[0]["run_id"] == events[0]["run_id"]

        assert _read_state(home)["last_sha"] == "OLD"
        assert not lock_dir.exists()
    finally:
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=5)
        else:
            # The child gate (a detached grandchild) may still be sleeping —
            # sweep the whole group so no orphaned `sleep`-alike lingers.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


# ---- Important-3 (fix wave 2): PYTHONUNBUFFERED defeats stdout burial -----

def test_gate_invocation_line_sets_pythonunbuffered():
    """Anchored to the executed invocation line, not a whole-file substring
    match a comment could satisfy (drift-guard-tests): PYTHONUNBUFFERED=1
    must be part of the actual `python3 "$GATE_BIN" ...` command so the
    gate child's stdout doesn't block-buffer in the merged capture."""
    text = SCRIPT.read_text()
    invocation_lines = [
        line for line in text.splitlines()
        if "GATE_BIN" in line and "--targets" in line
        and not line.strip().startswith("#")
    ]
    assert len(invocation_lines) == 1, invocation_lines
    assert "PYTHONUNBUFFERED=1" in invocation_lines[0]


def test_exit1_pythonunbuffered_prevents_stderr_diagnostic_burial(tmp_path):
    """Behavioral reproduction of the ordering defect: the gate prints a
    batch of stdout lines (kept in Python's default block-buffer — a
    regular file, not a tty, so nothing auto-flushes at this small volume)
    and only THEN writes its diagnostic straight to stderr via a raw
    os.write (always immediate, unaffected by PYTHONUNBUFFERED). Without
    PYTHONUNBUFFERED=1, none of the 45 stdout lines reach the merged
    capture file until interpreter-shutdown flush — which happens AFTER
    the already-written stderr line — so the file ends with the stdout
    batch and the diagnostic sits buried at the front, outside
    `tail -n 30`. With PYTHONUNBUFFERED=1 every print() writes through
    immediately in program order, so the diagnostic (written last) lands
    last, inside the tail window.

    Note: a naive "stderr marker FIRST, then N stdout lines" stub does NOT
    discriminate — the marker's os.write is always the first OS-level
    write regardless of buffering, so its file position never moves.
    The stdout-batch-then-marker shape above is the one that actually
    depends on PYTHONUNBUFFERED, and it reproduces deterministically
    (no reliance on the platform's exact stdio block-buffer-size
    threshold — 45 short lines stay safely under it either way)."""
    home = tmp_path / "home"
    _seed_home(home)
    _write_state(home, "OLD")
    bindir = tmp_path / "bin"
    _write_osascript_shim(bindir, tmp_path / "osascript-calls.log")

    gatebin = tmp_path / "gatebin"
    gatebin.mkdir(parents=True, exist_ok=True)
    stub = gatebin / "stub_gate.py"
    stub.write_text(textwrap.dedent("""\
        import os, sys
        for i in range(45):
            print(f"eval-gate: mapped target {i} covered-by: investigation")
        os.write(2, b"eval-gate: DIAG-MARKER-XYZ pytest suite FAILED at foo.py:42\\n")
        sys.exit(1)
        """))

    r = _run(home, bindir, stub, 1, "NEW1", "OLD..NEW1")
    assert r.returncode == 0, r.stderr

    findings = _findings(home)
    assert len(findings) == 1
    text = findings[0].read_text()
    assert "DIAG-MARKER-XYZ" in text


# ---- Minor-5 (fix wave 2): all failing-cases lines, not just the first ----

def test_exit1_finding_includes_all_failing_cases_lines_not_just_first(tmp_path):
    """`grep -m1 'failing cases:'` took only the FIRST matching suite's
    line; when multiple suites fail, every one must surface in the
    finding, not just the first encountered."""
    home = tmp_path / "home"
    _seed_home(home)
    _write_state(home, "OLD")
    bindir = tmp_path / "bin"
    _write_osascript_shim(bindir, tmp_path / "osascript-calls.log")

    gatebin = tmp_path / "gatebin"
    gatebin.mkdir(parents=True, exist_ok=True)
    stub = gatebin / "stub_gate.py"
    stub.write_text(textwrap.dedent("""\
        import sys
        for i in range(35):
            print(f"eval-gate: mapped target {i} covered-by: investigation")
        print('eval-gate: investigation failing cases: case_alpha')
        print('eval-gate: pytest failing cases: case_beta')
        for i in range(35, 70):
            print(f"eval-gate: mapped target {i} covered-by: investigation")
        sys.exit(1)
        """))

    r = _run(home, bindir, stub, 1, "NEW1", "OLD..NEW1")
    assert r.returncode == 0, r.stderr

    findings = _findings(home)
    assert len(findings) == 1
    text = findings[0].read_text()
    # Both failing-cases lines are pushed out of the trailing tail -n 30
    # block by the 35 lines printed after them — so both only surface via
    # the dedicated failing-cases grep line above the gate-output block,
    # not via the tail. That isolates this assertion from the tail-30
    # section and pins it on the grep-all-lines fix specifically.
    assert "case_alpha" in text
    assert "case_beta" in text
    gate_output_marker = "## Gate output"
    tail_section = text[text.index(gate_output_marker):]
    assert "case_alpha" not in tail_section
    assert "case_beta" not in tail_section
