import os, shutil, subprocess, textwrap, time
import pytest
from pathlib import Path
SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "scripts" / "gardener-run.sh"

def test_gardener_has_tmux_visible_branch():
    src = SCRIPT.read_text()
    assert 'has-session -t claude-workers' in src
    assert 'new-session -d -s claude-workers' in src
    assert 'new-window -d -t claude-workers' in src

def test_gardener_kitty_branch_is_gone():
    src = SCRIPT.read_text()
    assert 'resolve_kitty_socket' not in src
    assert 'no-kitty-socket' not in src
    assert 'TERMINAL_BACKEND' not in src
    assert 'kitty @' not in src

def test_gardener_script_syntax_ok():
    assert shutil.which("bash")
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_gardener_notify_no_ops_under_pytest():
    """notify() runs real /usr/bin/osascript; tests exec this script for real
    (test_module_toggle), so the guard must live in the script itself — the
    2026-07-03 gardener-gate desktop-notification leak class."""
    src = SCRIPT.read_text()
    assert 'if [ -n "${PYTEST_CURRENT_TEST:-}" ]; then return 0; fi' in src


def test_gardener_tmux_conf_legacy_fallback():
    src = SCRIPT.read_text()
    # New home first, then TWO legacy fallbacks (dockwright-rename, one release).
    assert 'TMUX_CONF_FILE="$HOMEDIR/.claude/dockwright/dockwright.tmux.conf"' in src
    assert 'TMUX_CONF_LEGACY="$HOMEDIR/.claude/orchestrator/dockwright.tmux.conf"' in src
    assert 'TMUX_CONF_LEGACY2="$HOMEDIR/.claude/orchestrator/claude-orch.tmux.conf"' in src
    assert 'elif [ -f "$TMUX_CONF_LEGACY" ]; then FFLAG=(-f "$TMUX_CONF_LEGACY")' in src
    assert 'elif [ -f "$TMUX_CONF_LEGACY2" ]; then FFLAG=(-f "$TMUX_CONF_LEGACY2")' in src


# ---- behavioral: visible-path window lifecycle on a scratch tmux socket ----
# The kill on clean finish is this plan's only destructive op — string guards
# are not acceptable for it (a commented-out kill still matches substrings).
# Stub `claude` parses the digest path from its prompt arg, writes Status: ok,
# and stays alive like the real interactive REPL the wrapper leaves behind.
#
# These are the plan's first tmux-behavioral tests, so they ride the repo's
# sanctioned real-tmux harness: @pytest.mark.real_tmux + the `real_tmux`
# fixture (conftest.py). That marker is LOAD-BEARING — the autouse
# `no_live_tmux` guard otherwise ABSORBS every `tmux` subprocess.run in the
# pytest process (returning empty), which would make `_panes()` always read []
# and silently pass the kill assertion without a real kill (the exact
# coincidence-detector failure the destructive-op note warns about). The
# fixture also hands us a throwaway `wt-iso-<pid>` socket and kill+unlink
# teardown (leak-net covered), so the wrapper's real server never survives the
# test — never the live `dockwright` socket. The wrapper's OWN tmux calls run
# in a child bash process, untouched by the guard.

TMUX = shutil.which("tmux")


def _write_stub_claude(bindir, *, write_status: bool):
    bindir.mkdir(parents=True, exist_ok=True)
    stub = bindir / "claude"
    status_line = 'echo "Status: ok" >> "$DIGEST"' if write_status else "true"
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        # last arg is the prompt: "/dockwright-gardener-digest run_id=.. digest=<path> .."
        PROMPT="${{@: -1}}"
        DIGEST=$(printf '%s' "$PROMPT" | sed -n 's/.*digest=\\([^ ]*\\).*/\\1/p')
        {status_line}
        sleep 300
        """))
    stub.chmod(0o755)
    return stub


def _gardener_env(home, bindir, sock):
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "PATH": f"{bindir}:{env['PATH']}",
        "DOCKWRIGHT_TMUX_SOCKET": sock,
        "GARDENER_CWD": str(home),
        "GARDENER_TIMEOUT_SEC": "15",
        "GARDENER_GRACE_SEC": "3",
        "GARDENER_POLL_SEC": "1",
    })
    return env


def _seed_gardener_home(home):
    scripts = home / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(SCRIPT.parent / "runlock.sh", scripts / "runlock.sh")
    # postrun/spend are best-effort ('|| true') — absent is fine.
    presets = home / ".claude" / "dockwright" / "presets"
    presets.mkdir(parents=True)
    (presets / "gardener-analyst-settings.json").write_text("{}")
    return home / ".claude" / "dockwright" / "gardener"


def _panes(sock):
    r = subprocess.run(["tmux", "-L", sock, "list-panes", "-a",
                        "-F", "#{pane_id}"], capture_output=True, text=True)
    return r.stdout.split()


@pytest.mark.real_tmux
@pytest.mark.skipif(TMUX is None, reason="tmux not installed")
def test_gardener_visible_kills_pane_on_status_ok(tmp_path, real_tmux):
    sock = real_tmux
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    _write_stub_claude(tmp_path / "bin", write_status=True)
    try:
        r = subprocess.run(["bash", str(SCRIPT), "--trigger", "force"],
                           env=_gardener_env(home, tmp_path / "bin", sock),
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        assert _panes(sock) == [], "pane must be killed after Status: ok"
        assert list((gdir / "live-windows").glob("*.window")) == [], \
            "sidecar must be removed by the EXIT trap"
        ledger = (gdir / "ledger.jsonl").read_text()
        assert "window_killed" in ledger
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"],
                       capture_output=True)


@pytest.mark.real_tmux
@pytest.mark.skipif(TMUX is None, reason="tmux not installed")
def test_gardener_visible_leaves_pane_on_timeout(tmp_path, real_tmux):
    """No Status line → overdue path: the tab is the human's to close (PRD
    §9.3) — never killed; the sidecar still cleans up on wrapper exit."""
    sock = real_tmux
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    _write_stub_claude(tmp_path / "bin", write_status=False)
    try:
        r = subprocess.run(["bash", str(SCRIPT), "--trigger", "force"],
                           env=_gardener_env(home, tmp_path / "bin", sock),
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        assert len(_panes(sock)) == 1, "timeout must leave the pane open"
        assert list((gdir / "live-windows").glob("*.window")) == []
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"],
                       capture_output=True)


# ---- --dry-run probe boundary + sandbox-HOME refusal (2026-07-17 incident) ----
# gardener-run.sh's visible spawn tail has the SAME shape as the incident script:
# live-socket default (TMUX_SOCK=dockwright) + a real `claude` spawn onto it. These
# two run the REAL script with a test-local fake-tmux dir fronting PATH (the
# bootstrap-recreate-guard idiom), so the spawn tail can only ever hit a logging
# stub — never real tmux, even if the conftest autouse shim were removed. They
# reach the visible spawn path via --trigger force + a seeded runlock/preset home.


def _fake_tmux_logging_dir(tmp_path):
    """A fake `tmux` that LOGS every invocation and fakes has-session (miss) +
    new-session/new-window (returns a pane id). Its log file existing proves the
    spawn tail ran; its absence proves the script exited before any tmux call."""
    d = tmp_path / "fakebin"
    d.mkdir()
    log = tmp_path / "tmux-invocations.log"
    (d / "tmux").write_text(
        "#!/bin/bash\n"
        f"echo \"$@\" >> {log}\n"
        "case \"$*\" in *has-session*) exit 1 ;; *new-session*|*new-window*) echo '@1'; exit 0 ;; esac\n"
        "exit 0\n")
    (d / "tmux").chmod(0o755)
    return d, log


def _gardener_probe_env(home, fakebin, sock=None):
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "PATH": f"{fakebin}{os.pathsep}{env['PATH']}",
        "GARDENER_CWD": str(home),
        "GARDENER_TIMEOUT_SEC": "1",
        "GARDENER_GRACE_SEC": "1",
        "GARDENER_POLL_SEC": "1",
    })
    if sock is not None:
        env["DOCKWRIGHT_TMUX_SOCKET"] = sock
    else:
        # Leave the socket at its live/default (`dockwright`) — the exact incident
        # shape. Pop BOTH overrides so an ambient operator socket can't steer it.
        env.pop("DOCKWRIGHT_TMUX_SOCKET", None)
        env.pop("CLAUDE_ORCH_TMUX_SOCKET", None)
    return env


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_gardener_dry_run_probes_without_spawning(tmp_path):
    """--dry-run reaches the visible spawn gate, prints the plan, and exits 0
    BEFORE any tmux call. RED against the unfixed script: the arg loop's default
    arm silently `shift`s unknown flags, so --dry-run was ignored and the spawn
    tail ran → the fake-tmux log EXISTED."""
    home = tmp_path / "home"
    _seed_gardener_home(home)
    fakebin, log = _fake_tmux_logging_dir(tmp_path)
    r = subprocess.run(["bash", str(SCRIPT), "--trigger", "force", "--dry-run"],
                       env=_gardener_probe_env(home, fakebin),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "DRY_RUN: no spawn." in r.stdout, r.stdout
    assert not log.exists(), f"--dry-run still reached tmux: {log.read_text()}"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_gardener_sandboxed_home_live_socket_is_refused(tmp_path):
    """The caged incident-shape reproduction for gardener: a sandboxed HOME does
    NOT isolate tmux (-L namespaces by uid, not HOME), so a probe run under a
    sandboxed HOME against the live/default socket would spawn onto the LIVE
    fleet. The guard refuses it (exit 3), naming --dry-run, before any tmux call.
    RED against the unfixed script: no guard → spawn tail ran (fake-tmux log
    existed) and the run exited 0 via the timeout path, not 3."""
    home = tmp_path / "home"
    _seed_gardener_home(home)
    fakebin, log = _fake_tmux_logging_dir(tmp_path)
    r = subprocess.run(["bash", str(SCRIPT), "--trigger", "force"],
                       env=_gardener_probe_env(home, fakebin, sock=None),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 3, f"expected refusal exit 3, got {r.returncode}: {r.stderr}"
    assert "--dry-run" in r.stderr, r.stderr
    assert not log.exists(), f"refusal still reached tmux: {log.read_text()}"


@pytest.mark.real_tmux
@pytest.mark.skipif(TMUX is None, reason="tmux not installed")
def test_gardener_visible_writes_sidecar_during_run(tmp_path, real_tmux):
    """While the run is live the sidecar must exist and carry the pane id —
    it is what shields the pane from the M-2 orphan alarm."""
    sock = real_tmux
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    _write_stub_claude(tmp_path / "bin", write_status=False)
    env = _gardener_env(home, tmp_path / "bin", sock)
    env["GARDENER_TIMEOUT_SEC"] = "8"
    env["GARDENER_GRACE_SEC"] = "1"
    try:
        proc = subprocess.Popen(["bash", str(SCRIPT), "--trigger", "force"],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        sidecar = None
        for _ in range(60):
            time.sleep(0.5)
            found = list((gdir / "live-windows").glob("*.window"))
            if found:
                sidecar = found[0]
                break
        assert sidecar is not None, "sidecar must appear at spawn"
        pane = sidecar.read_text().strip()
        assert pane and pane in _panes(sock)
        proc.wait(timeout=120)
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"],
                       capture_output=True)
