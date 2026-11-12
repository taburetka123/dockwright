import os
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

_STUB = """#!/usr/bin/env bash
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

_ONLY_SIZE_FAIL = (
    "  [PASS] venv-import: ok\n"
    "  [FAIL] agents:size: manager.md: 141883 bytes > 140000 — compress a core "
    "section, an overlay drop-in or an [agent_vars] value, or move the number "
    "in [ceilings] deliberately\n"
    "doctor: 1 check(s) FAILED\n")

_ONLY_PLUGIN_FAIL = (
    "  [PASS] venv-import: ok\n"
    "  [FAIL] plugins:dockwright: dockwright@dockwright not enabled in "
    "settings — run setup.sh to install and enable the dockwright plugin\n"
    "doctor: 1 check(s) FAILED\n")

_ONLY_LANES_ENV_FAIL = (
    "  [PASS] venv-import: ok\n"
    "  [FAIL] lanes:env: tmux global environment carries CLAUDE_ORCH_AUTONUDGE=1 but [lanes.env] does not match "
    "— the daemon will run without it; fix: copy the pair into [lanes.env] in dockwright.toml and run setup.sh\n"
    "doctor: 1 check(s) FAILED\n")

_ONLY_TMUX_ENV_FAIL = (
    "  [PASS] venv-import: ok\n"
    "  [FAIL] tmux:env: the global environment carries GH_TOKEN, which every new pane inherits "
    "— fix: tmux -L dockwright set-environment -gu GH_TOKEN\n"
    "doctor: 1 check(s) FAILED\n")

_ONLY_MCP_OAUTH_FAIL = (
    "  [PASS] venv-import: ok\n"
    "  [FAIL] accounts:mcp-oauth: a: svc (no usable token in svc|k — fix: claude in a directory whose "
    ".mcp.json declares svc, then /mcp → svc → Authenticate)\n"
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
    assert "WARNING: doctor failed only on checks that do not gate the install" in r.stderr
    assert "fix: claude, then /login" in r.stderr
    assert "Environment wiring verified." not in r.stdout


def test_agents_size_only_failure_warns_and_continues(tmp_path):
    r = _run_setup_with_doctor(tmp_path, _ONLY_SIZE_FAIL, 1)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Install complete" in r.stdout
    assert "WARNING: doctor failed only on checks that do not gate the install" in r.stderr
    assert "141883 bytes > 140000" in r.stderr


def test_plugins_dockwright_only_failure_warns_and_continues(tmp_path):
    r = _run_setup_with_doctor(tmp_path, _ONLY_PLUGIN_FAIL, 1)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Install complete" in r.stdout
    assert "WARNING: doctor failed only on checks that do not gate the install" in r.stderr
    assert "dockwright@dockwright not enabled in settings" in r.stderr


def test_daemon_config_only_failure_warns_and_continues(tmp_path):
    r = _run_setup_with_doctor(tmp_path, _ONLY_LANES_ENV_FAIL, 1)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Install complete" in r.stdout
    assert "WARNING: doctor failed only on checks that do not gate the install" in r.stderr
    assert "CLAUDE_ORCH_AUTONUDGE=1" in r.stderr


def test_tmux_env_only_failure_warns_and_continues(tmp_path):
    r = _run_setup_with_doctor(tmp_path, _ONLY_TMUX_ENV_FAIL, 1)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Install complete" in r.stdout
    assert "WARNING: doctor failed only on checks that do not gate the install" in r.stderr
    assert "set-environment -gu GH_TOKEN" in r.stderr


def test_accounts_mcp_oauth_only_failure_warns_and_continues(tmp_path):
    r = _run_setup_with_doctor(tmp_path, _ONLY_MCP_OAUTH_FAIL, 1)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Install complete" in r.stdout
    assert "WARNING: doctor failed only on checks that do not gate the install" in r.stderr
    assert "/mcp → svc → Authenticate" in r.stderr


def test_other_wiring_failure_still_aborts(tmp_path):
    r = _run_setup_with_doctor(tmp_path, _LOGIN_PLUS_WIRING_FAIL, 1)
    assert r.returncode == 1
    assert "Install complete" not in r.stdout


def test_doctor_crash_without_fail_lines_still_aborts(tmp_path):
    r = _run_setup_with_doctor(tmp_path, _CRASH, 1)
    assert r.returncode == 1
    assert "Install complete" not in r.stdout
    assert "WARNING: doctor failed only on checks that do not gate the install" not in r.stderr


def test_all_pass_verifies_and_completes(tmp_path):
    r = _run_setup_with_doctor(tmp_path, _ALL_PASS, 0)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Environment wiring verified." in r.stdout
    assert "Install complete" in r.stdout
    assert "WARNING: doctor failed only on checks that do not gate the install" not in r.stderr


def test_real_doctor_fail_lines_are_carved_out_by_the_real_installer(
        tmp_path, monkeypatch, capsys):
    from dockwright import config, doctor, paths

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    core = tmp_path / "core"
    core.mkdir()
    (core / "manager.core.md").write_text("manager core\n")
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text('[accounts]\ndefault = "a"\n[[accounts.pool]]\nname = "a"\n'
                   '[ceilings]\n"manager.md" = { bytes = 5 }\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    plist = paths.LAUNCH_AGENTS / "com.dockwright.daemon.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("x")
    os.utime(plist, (0.0, 0.0))

    rc = doctor.main(["--host-claude-json", str(home / ".claude.json"),
                      "--compose-core-dir", str(core)])
    assert rc == 1
    out = capsys.readouterr().out
    carved = [ln for ln in out.splitlines() if "[FAIL]" in ln
              and ("accounts:login: " in ln or "agents:size: " in ln or "daemon:alive: " in ln)]
    assert len(carved) == 3, out

    gate = tmp_path / "gate"
    gate.mkdir()
    r = _run_setup_with_doctor(
        gate, "\n".join(carved) + "\ndoctor: 3 check(s) FAILED\n", 1)
    assert r.returncode == 0, (
        f"the real doctor FAIL lines no longer match the installer's "
        f"warn-vs-abort anchor — setup.sh aborted on:\n" + "\n".join(carved)
        + "\n" + r.stdout + r.stderr)
    assert "Install complete" in r.stdout
    assert "WARNING: doctor failed only on checks that do not gate the install" in r.stderr


def test_subname_check_is_not_swallowed_by_prefix_match(tmp_path):
    subname = (
        "  [FAIL] accounts:login:sub: something else broke\n"
        "doctor: 1 check(s) FAILED\n")
    r = _run_setup_with_doctor(tmp_path, subname, 1)
    assert r.returncode == 1
    assert "Install complete" not in r.stdout
    assert "WARNING: doctor failed only on checks that do not gate the install" not in r.stderr


_ONLY_DAEMON_FAIL = (
    "  [PASS] venv-import: ok\n"
    "  [FAIL] daemon:alive: last pass 900s ago, 0 scan error(s) in it — fix: read /x/launchd-err.log\n"
    "doctor: 1 check(s) FAILED\n")


def test_daemon_alive_only_failure_warns_and_continues(tmp_path):
    r = _run_setup_with_doctor(tmp_path, _ONLY_DAEMON_FAIL, 1)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Install complete" in r.stdout
    assert "WARNING: doctor failed only on checks that do not gate the install" in r.stderr
    assert "daemon:alive" in r.stderr
