import os, shutil, subprocess, textwrap, time
import pytest
from pathlib import Path

from tests.test_model_pin_inventory import (
    HEADLESS_LIGHT_TOKEN,
    RESOLVER_PROBE_ID,
    resolver_stub,
)
SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "scripts" / "gardener-run.sh"

def test_gardener_script_syntax_ok():
    assert shutil.which("bash")
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


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
    presets = home / ".claude" / "dockwright" / "presets"
    presets.mkdir(parents=True)
    (presets / "gardener-analyst-settings.json").write_text("{}")
    skill = home / ".claude" / "skills" / "dockwright-gardener-digest"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# dockwright-gardener-digest\nstub body\n")
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


def _fake_tmux_logging_dir(tmp_path):
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
    env.pop("DOCKWRIGHT_CONFIG", None)
    env.pop("XDG_CONFIG_HOME", None)
    if sock is not None:
        env["DOCKWRIGHT_TMUX_SOCKET"] = sock
    else:
        env.pop("DOCKWRIGHT_TMUX_SOCKET", None)
        env.pop("CLAUDE_ORCH_TMUX_SOCKET", None)
    return env


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_gardener_dry_run_probes_without_spawning(tmp_path):
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
    home = tmp_path / "home"
    _seed_gardener_home(home)
    fakebin, log = _fake_tmux_logging_dir(tmp_path)
    r = subprocess.run(["bash", str(SCRIPT), "--trigger", "force"],
                       env=_gardener_probe_env(home, fakebin, sock=None),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 3, f"expected refusal exit 3, got {r.returncode}: {r.stderr}"
    assert "--dry-run" in r.stderr, r.stderr
    assert not log.exists(), f"refusal still reached tmux: {log.read_text()}"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_gardener_visible_hands_tmux_the_model_the_resolver_printed(tmp_path):
    home = tmp_path / "home"
    _seed_gardener_home(home)
    fakebin, log = _fake_tmux_logging_dir(tmp_path)
    resolver_stub(fakebin, RESOLVER_PROBE_ID, HEADLESS_LIGHT_TOKEN)
    subprocess.run(["bash", str(SCRIPT), "--trigger", "force"],
                   env=_gardener_probe_env(home, fakebin,
                                           sock=f"wt-iso-{os.getpid()}-model"),
                   capture_output=True, text=True, timeout=120)
    spawned = log.read_text()
    assert f"--model '{RESOLVER_PROBE_ID}' " in spawned, (
        "the visible lane did not hand the second shell the id the resolver printed, "
        f"single-quoted against zsh NOMATCH on a bracketed variant: {spawned}")


def _role_args_stub(fakebin, calls, role_args_body):
    stub = fakebin / "dockwright"
    stub.write_text(
        "#!/bin/bash\n"
        f'if [ "$1" = role-args ]; then echo "$@" >> {calls}\n{role_args_body}\nfi\n'
        f'[ "$1" = model ] && [ "$2" = resolve ] && echo {RESOLVER_PROBE_ID} && exit 0\n'
        "exit 2\n")
    stub.chmod(0o755)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_gardener_visible_launches_through_role_args_as_a_generic_worker_lane(tmp_path):
    home = tmp_path / "home"
    _seed_gardener_home(home)
    fakebin, log = _fake_tmux_logging_dir(tmp_path)
    calls = tmp_path / "role-args-calls.log"
    _role_args_stub(fakebin, calls,
                    "printf '%s\\n' --append-system-prompt-file '/roles/GenericWorker/role lane.md' "
                    "--settings /role-settings/abc.json; exit 0")
    subprocess.run(["bash", str(SCRIPT), "--trigger", "force"],
                   env=_gardener_probe_env(home, fakebin, sock=f"wt-iso-{os.getpid()}-role"),
                   capture_output=True, text=True, timeout=120)
    call = calls.read_text().split()
    assert call[:4] == ["role-args", "GenericWorker", "--launch", "lane"]
    assert call[4:6] == ["--cwd", str(home)]
    assert call[6] == "--base" and call[7].endswith("/settings.json")
    spawned = log.read_text()
    assert ("--append-system-prompt-file /roles/GenericWorker/role\\ lane.md "
            "--settings /role-settings/abc.json \"$(cat ") in spawned, spawned
    assert f"{fakebin / 'dockwright'} launch --model" in spawned, spawned
    assert call[7] not in spawned


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_gardener_visible_falls_back_to_its_own_settings_when_role_args_refuses(tmp_path):
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    fakebin, log = _fake_tmux_logging_dir(tmp_path)
    calls = tmp_path / "role-args-calls.log"
    _role_args_stub(fakebin, calls, "echo 'role-args: unknown role' >&2; exit 2")
    subprocess.run(["bash", str(SCRIPT), "--trigger", "force"],
                   env=_gardener_probe_env(home, fakebin, sock=f"wt-iso-{os.getpid()}-norole"),
                   capture_output=True, text=True, timeout=120)
    settings_file = calls.read_text().split()[7]
    spawned = log.read_text()
    assert f"--model '{RESOLVER_PROBE_ID}' --settings {settings_file} \"$(cat " in spawned, spawned
    assert "--append-system-prompt-file" not in spawned
    run_log = (gdir / "run.log").read_text()
    assert "role-args: unknown role" in run_log and "role-args GenericWorker --launch lane refused" in run_log


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_gardener_visible_refuses_to_spawn_when_the_resolver_fails(tmp_path):
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    fakebin, log = _fake_tmux_logging_dir(tmp_path)
    resolver_stub(fakebin, None, HEADLESS_LIGHT_TOKEN)
    r = subprocess.run(["bash", str(SCRIPT), "--trigger", "force"],
                       env=_gardener_probe_env(home, fakebin,
                                               sock=f"wt-iso-{os.getpid()}-norsv"),
                       capture_output=True, text=True, timeout=120)
    assert r.returncode != 0, f"a failed resolve exited 0: {r.stdout}"
    spawned = log.read_text() if log.exists() else ""
    assert "new-session" not in spawned and "new-window" not in spawned, (
        f"the visible lane spawned an analyst with no resolved model: {spawned}")
    run_log = (gdir / "run.log").read_text() if (gdir / "run.log").is_file() else ""
    assert "model-unresolved" in run_log, (
        f"the refusal left no line in run.log: {run_log!r}")


@pytest.mark.real_tmux
@pytest.mark.skipif(TMUX is None, reason="tmux not installed")
def test_gardener_visible_writes_sidecar_during_run(tmp_path, real_tmux):
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


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_gardener_visible_runs_the_three_repo_dedup_and_files_a_finding_without_aborting(tmp_path):
    home = tmp_path / "home"
    gdir = _seed_gardener_home(home)
    scripts = home / ".claude" / "scripts"
    (scripts / "dup_sentences.py").write_text(
        "import sys\nprint('[always] 9w 2 files abcdef12')\nprint(' '.join(sys.argv[1:]))\nsys.exit(1)\n")
    (scripts / "dup_sentences.baseline.json").write_text('{"min_words": 8, "records": []}')
    (home / ".claude" / "dockwright.toml").write_text('[paths]\nproject_units_dir = "~/units"\n')
    (home / "units").mkdir()
    (home / "deploy").mkdir()
    (home / "src").mkdir()
    fakebin, log = _fake_tmux_logging_dir(tmp_path)
    calls = tmp_path / "role-args-calls.log"
    _role_args_stub(fakebin, calls, "printf '%s\\n' --append-system-prompt-file /r.md --settings /s.json; exit 0")
    subprocess.run(["bash", str(SCRIPT), "--trigger", "force"],
                   env=_gardener_probe_env(home, fakebin, sock=f"wt-iso-{os.getpid()}-dedup"),
                   capture_output=True, text=True, timeout=120)
    findings = sorted((home / ".claude" / "dockwright" / "selffix" / "findings").glob("dedup-*.md"))
    assert len(findings) == 1, list((home / ".claude" / "dockwright").rglob("*"))
    body = findings[0].read_text()
    assert "[always] 9w 2 files abcdef12" in body
    assert f"--extra-root {home}/deploy --extra-root {home}/src --extra-root {home}/units" in body
    assert "--baseline" in body
    assert "launch --model" in log.read_text()
    assert "dedup" in (gdir / "run.log").read_text()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="no system python3")
def test_gardener_dedup_reads_the_units_root_under_the_system_python3(tmp_path):
    home = tmp_path / "home"
    _seed_gardener_home(home)
    scripts = home / ".claude" / "scripts"
    (scripts / "dup_sentences.py").write_text(
        "import sys\nprint('[always] 9w 2 files abcdef12')\nprint(' '.join(sys.argv[1:]))\nsys.exit(1)\n")
    (home / ".claude" / "dockwright.toml").write_text('[paths]\nproject_units_dir = "~/units"\n')
    (home / "units").mkdir()
    fakebin, log = _fake_tmux_logging_dir(tmp_path)
    (fakebin / "python3").symlink_to("/usr/bin/python3")
    calls = tmp_path / "role-args-calls.log"
    _role_args_stub(fakebin, calls, "printf '%s\\n' --append-system-prompt-file /r.md --settings /s.json; exit 0")
    subprocess.run(["bash", str(SCRIPT), "--trigger", "force"],
                   env=_gardener_probe_env(home, fakebin, sock=f"wt-iso-{os.getpid()}-dedup39"),
                   capture_output=True, text=True, timeout=120)
    findings = sorted((home / ".claude" / "dockwright" / "selffix" / "findings").glob("dedup-*.md"))
    assert len(findings) == 1, list((home / ".claude" / "dockwright").rglob("*"))
    assert f"--extra-root {home}/units" in findings[0].read_text()
