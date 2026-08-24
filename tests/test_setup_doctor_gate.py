"""setup.sh doctor gate: an accounts:login-only failure warns and continues;
any other FAIL — or a doctor CRASH with no [FAIL] lines — still aborts.

Login state is a user prerequisite (README prereqs: tmux/Python/claude CLI —
not "already logged in"), not installer wiring: on a fresh box `claude mcp add`
creates a marker-less ~/.claude.json BEFORE doctor runs, so the old hard gate
aborted the documented quickstart. Both pre-existing setup.sh test files run
under DOCKWRIGHT_SETUP_FILES_ONLY=1, which skips the doctor block entirely —
this path had NO test, which is why the abort shipped unseen. The
DOCKWRIGHT_SETUP_RUN_DOCTOR=1 knob exists solely so these tests can drive the
REAL gate logic with a stub doctor binary; no shipped path sets it.
"""
import os
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

_STUB = """#!/usr/bin/env bash
# Stub dockwright: canned doctor output/rc from env; every other verb no-ops.
if [ "${1:-}" = "doctor" ]; then
    cat "$STUB_DOCTOR_OUT"
    exit "$STUB_DOCTOR_RC"
fi
exit 0
"""

_ONLY_LOGIN_FAIL = (
    "  [PASS] venv-import: ok\n"
    "  [FAIL] accounts:login: a (no /nowhere/.claude.json — never logged in? — "
    "fix: claude, then /login)\n"
    "doctor: 1 check(s) FAILED\n")

_LOGIN_PLUS_WIRING_FAIL = (
    "  [FAIL] accounts:login: a (no /nowhere/.claude.json — never logged in? — "
    "fix: claude, then /login)\n"
    "  [FAIL] hooks:claude: non-abs dockwright hooks: ['x']\n"
    "doctor: 2 check(s) FAILED\n")

_CRASH = (
    "Traceback (most recent call last):\n"
    "  File \"doctor.py\", line 27, in mcp_command_claude\n"
    "AttributeError: 'list' object has no attribute 'get'\n")

_ALL_PASS = (
    "  [PASS] venv-import: ok\n"
    "  [PASS] accounts:login: all 1 pool account(s) carry an oauthAccount marker\n"
    "doctor: all checks passed\n")


def _run_setup_with_doctor(tmp_path, doctor_output, doctor_rc):
    claude_dir = tmp_path / "claude"
    home = tmp_path / "home"
    home.mkdir()
    stub = tmp_path / "stub-dockwright"
    stub.write_text(_STUB)
    stub.chmod(0o755)
    out_file = tmp_path / "doctor-out.txt"
    out_file.write_text(doctor_output)
    env = {**os.environ,
           "DOCKWRIGHT_SETUP_ALLOW_WORKTREE": "1",
           "DOCKWRIGHT_SETUP_FILES_ONLY": "1",
           "DOCKWRIGHT_SETUP_RUN_DOCTOR": "1",
           "DOCKWRIGHT_ORCH_BIN": str(stub),
           "STUB_DOCTOR_OUT": str(out_file),
           "STUB_DOCTOR_RC": str(doctor_rc),
           "HOME": str(home),
           "PATH": "/usr/bin:/bin",
           "CLAUDE_DIR": str(claude_dir),
           "CODEX_DIR": str(tmp_path / "codex")}
    env.pop("DOCKWRIGHT_SETUP_FORCE", None)
    return subprocess.run(["bash", str(_REPO / "setup.sh")], env=env,
                          capture_output=True, text=True, cwd=str(_REPO))


def test_accounts_login_only_failure_warns_and_continues(tmp_path):
    r = _run_setup_with_doctor(tmp_path, _ONLY_LOGIN_FAIL, 1)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Install complete" in r.stdout
    assert "WARNING: accounts:login failed" in r.stderr
    assert "fix: claude, then /login" in r.stderr      # exact remediation reaches the human
    assert "Environment wiring verified." not in r.stdout


def test_other_wiring_failure_still_aborts(tmp_path):
    r = _run_setup_with_doctor(tmp_path, _LOGIN_PLUS_WIRING_FAIL, 1)
    assert r.returncode == 1
    assert "Install complete" not in r.stdout


def test_doctor_crash_without_fail_lines_still_aborts(tmp_path):
    """rc!=0 with no [FAIL] lines is a doctor CRASH, not a login diagnosis —
    it must never be swallowed as ignorable (fail-loud, drift-guard-tests.md)."""
    r = _run_setup_with_doctor(tmp_path, _CRASH, 1)
    assert r.returncode == 1
    assert "Install complete" not in r.stdout
    assert "WARNING: accounts:login failed" not in r.stderr


def test_all_pass_verifies_and_completes(tmp_path):
    r = _run_setup_with_doctor(tmp_path, _ALL_PASS, 0)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Environment wiring verified." in r.stdout
    assert "Install complete" in r.stdout
    # setup.sh prints unrelated provenance warnings (e.g. non-main branch) —
    # assert only the GATE's warning is absent
    assert "WARNING: accounts:login failed" not in r.stderr


def test_real_doctor_fail_line_matches_setup_gate_anchor(tmp_path, monkeypatch, capsys):
    """The gate above is proven with a STUB doctor — it proves the parser, not
    that real doctor emits what the parser expects. This test is the other
    half of the bridge: run the REAL doctor against a fixture whose only
    plausible accounts failure is accounts:login, extract the ignore-anchor
    FROM setup.sh's own executed text (no duplicated constant to drift), and
    assert the real emitted FAIL line matches it — and still carries the exact
    remediation the warn path forwards to the human. A reformat of doctor's
    output line (indent, tag, check name) now fails HERE instead of silently
    flipping the installer between warn-and-continue and abort."""
    import re
    from dockwright import config, doctor

    code = [ln for ln in (_REPO / "setup.sh").read_text().splitlines()
            if not ln.lstrip().startswith("#")]
    anchors = [m.group(1) for ln in code
               for m in [re.search(r"grep -v '([^']+)'", ln)]
               if m and "accounts:login" in m.group(1)]
    assert len(anchors) == 1, f"expected exactly 1 accounts:login ignore-anchor in setup.sh, got {anchors}"
    anchor = anchors[0]

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text('[accounts]\ndefault = "a"\n[[accounts.pool]]\nname = "a"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    rc = doctor.main(["--host-claude-json", str(home / ".claude.json")])
    out = capsys.readouterr().out
    assert rc == 1
    login_fails = [ln for ln in out.splitlines()
                   if "accounts:login" in ln and "[FAIL]" in ln]
    assert len(login_fails) == 1, out
    line = login_fails[0]
    assert re.search(anchor, line), (
        f"real doctor FAIL line {line!r} no longer matches the setup.sh gate "
        f"anchor {anchor!r} — the installer's warn-vs-abort decision just "
        f"silently changed")
    assert "fix: claude, then /login" in line  # keeps the stub scenarios representative


def test_subname_check_is_not_swallowed_by_prefix_match(tmp_path):
    """Minor-3 (delta probe E10): the ignore-anchor must match the exact check
    name `accounts:login`, not any name it prefixes — a hypothetical
    `accounts:login:sub` FAIL is a DIFFERENT check and must abort, not ride
    the warn-and-continue path."""
    subname = (
        "  [FAIL] accounts:login:sub: something else broke\n"
        "doctor: 1 check(s) FAILED\n")
    r = _run_setup_with_doctor(tmp_path, subname, 1)
    assert r.returncode == 1
    assert "Install complete" not in r.stdout
    assert "WARNING: accounts:login failed" not in r.stderr
