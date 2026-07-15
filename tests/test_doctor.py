import json
from pathlib import Path
from dockwright import doctor

ABS = "/Users/testop/projects/personal/claude-orchestrator/.venv/bin/orchestrator"

def test_mcp_command_extractors():
    assert doctor.mcp_command_claude(
        {"mcpServers": {"claude-orchestrator": {"command": ABS}}}, "claude-orchestrator") == ABS
    assert doctor.mcp_command_codex(
        {"mcp_servers": {"claude-orchestrator": {"command": "orchestrator"}}}, "claude-orchestrator") == "orchestrator"
    assert doctor.mcp_command_claude({}, "claude-orchestrator") is None

def test_check_mcp_pass_fail():
    assert doctor.check_mcp("claude", ABS, ABS).ok
    assert not doctor.check_mcp("codex", "orchestrator", ABS).ok

def test_check_hooks_abspath_flags_bare():
    bare = {"hooks": {"Stop": [{"hooks": [{"command": "bash -c '$PPID orchestrator stop'"}]}]}}
    abss = {"hooks": {"Stop": [{"hooks": [{"command": f"bash -c '$PPID {ABS} stop'"}]}]}}
    assert not doctor.check_hooks_abspath(bare, ABS, "claude").ok
    assert doctor.check_hooks_abspath(abss, ABS, "claude").ok

def test_cli_returns_1_on_failure(tmp_path):
    cj = tmp_path / "claude.json"; cj.write_text(json.dumps({"mcpServers": {"claude-orchestrator": {"command": "orchestrator"}}}))
    rc = doctor.main(["--orch-bin", ABS, "--claude-json", str(cj), "--brew-prefix", str(tmp_path)])
    assert rc == 1   # bare reg fails (venv-import also fails since ABS python absent — both FAIL)

def test_cli_fails_on_unparseable_existing_config(tmp_path):
    # An existing-but-malformed settings.json must FAIL the fail-loud gate, not skip vacuously.
    bad = tmp_path / "settings.json"; bad.write_text("{ not json")
    rc = doctor.main(["--orch-bin", ABS, "--settings", str(bad), "--brew-prefix", str(tmp_path)])
    assert rc == 1

def test_cli_skips_missing_files(tmp_path):
    # only no-brew-editable + venv-import run; missing claude/codex/settings paths are skipped
    rc = doctor.main(["--orch-bin", ABS, "--claude-json", str(tmp_path/'absent.json'),
                      "--brew-prefix", str(tmp_path)])
    # venv-import fails (ABS not real here) but missing claude-json must not raise
    assert rc in (0, 1)
