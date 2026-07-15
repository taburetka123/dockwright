import json, os, subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "scripts" / "canon-edit-guard.sh"

def _run(stdin, home, dockwright_repo="__default__"):
    """Run the guard. CANON_DIR now derives from [paths] dockwright_repo, so a
    config is written pointing at the canon `_make_home` builds. Pass
    dockwright_repo=None to simulate an unset key (guard must exit silently)."""
    env = dict(os.environ, HOME=str(home))
    if dockwright_repo == "__default__":
        dockwright_repo = str(home / "projects/personal/claude-orchestrator")
    if dockwright_repo is not None:
        cfg = home / "dockwright.toml"
        cfg.write_text(f'[paths]\ndockwright_repo = "{dockwright_repo}"\n')
        env["DOCKWRIGHT_CONFIG"] = str(cfg)
    else:
        # Point DOCKWRIGHT_CONFIG at a nonexistent file = authoritative "no config".
        env["DOCKWRIGHT_CONFIG"] = str(home / "absent.toml")
    return subprocess.run(["bash", str(SCRIPT)], input=stdin, capture_output=True,
                          text=True, env=env)

def _make_home(tmp_path):
    home = tmp_path / "home"
    canon = home / "projects/personal/claude-orchestrator/deploy"
    (canon / "scripts").mkdir(parents=True)
    (canon / "scripts" / "selffix-trigger.sh").write_text("x")
    (canon / "presets").mkdir(parents=True)
    (canon / "presets" / "verifier-settings.json").write_text("x")
    (canon / "tmux").mkdir(parents=True)
    (canon / "tmux" / "dockwright.conf").write_text("x")
    (canon / "tmux" / "status_row.py").write_text("x")
    (canon / "loops-registry.md").write_text("x")
    (home / ".claude" / "scripts").mkdir(parents=True)
    (home / ".claude" / "rules").mkdir(parents=True)
    (home / ".claude" / "scripts" / "selffix-trigger.sh").write_text("x")
    (home / ".claude" / "rules" / "style.md").write_text("x")
    (home / ".claude" / "orchestrator" / "presets").mkdir(parents=True)
    (home / ".claude" / "orchestrator" / "presets" / "verifier-settings.json").write_text("x")
    (home / ".claude" / "orchestrator" / "dockwright.tmux.conf").write_text("x")
    (home / ".claude" / "orchestrator" / "status_row.py").write_text("x")
    (home / ".claude" / "orchestrator" / "notebook").mkdir(parents=True)
    (home / ".claude" / "orchestrator" / "notebook" / "general.md").write_text("x")
    return home

def test_canon_sourced_file_emits_neutral_warning(tmp_path):
    home = _make_home(tmp_path)
    fp = str(home / ".claude" / "scripts" / "selffix-trigger.sh")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home)
    assert r.returncode == 0
    hso = json.loads(r.stdout)["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "scripts/selffix-trigger.sh" in hso["additionalContext"]
    assert "permissionDecision" not in hso

def test_native_claude_file_no_output(tmp_path):
    home = _make_home(tmp_path)
    fp = str(home / ".claude" / "rules" / "style.md")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home)
    assert r.returncode == 0 and r.stdout.strip() == ""

def test_non_claude_path_no_output(tmp_path):
    home = _make_home(tmp_path)
    fp = str(home / "projects" / "work" / "foo.kt")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home)
    assert r.returncode == 0 and r.stdout.strip() == ""

def test_malformed_stdin_fails_open(tmp_path):
    r = _run("not json", _make_home(tmp_path))
    assert r.returncode == 0 and r.stdout.strip() == ""

def test_missing_file_path_no_output(tmp_path):
    r = _run(json.dumps({"tool_input": {}}), _make_home(tmp_path))
    assert r.returncode == 0 and r.stdout.strip() == ""

def test_renamed_presets_file_emits_warning(tmp_path):
    home = _make_home(tmp_path)
    fp = str(home / ".claude" / "orchestrator" / "presets" / "verifier-settings.json")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home)
    assert r.returncode == 0
    hso = json.loads(r.stdout)["hookSpecificOutput"]
    assert "presets/verifier-settings.json" in hso["additionalContext"]
    assert "orchestrator/presets" not in hso["additionalContext"]  # names canon, not deployed path
    assert "permissionDecision" not in hso

def test_renamed_tmux_conf_emits_warning(tmp_path):
    home = _make_home(tmp_path)
    fp = str(home / ".claude" / "orchestrator" / "dockwright.tmux.conf")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home)
    assert r.returncode == 0
    assert "tmux/dockwright.conf" in json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]

def test_renamed_status_row_emits_warning(tmp_path):
    home = _make_home(tmp_path)
    fp = str(home / ".claude" / "orchestrator" / "status_row.py")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home)
    assert r.returncode == 0
    assert "tmux/status_row.py" in json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]

def test_orchestrator_runtime_state_no_warning(tmp_path):
    home = _make_home(tmp_path)
    fp = str(home / ".claude" / "orchestrator" / "notebook" / "general.md")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home)
    assert r.returncode == 0 and r.stdout.strip() == ""


# --- new dockwright/ deploy home (renamed-deploy mappings) --------------------

def test_renamed_presets_new_home_emits_warning(tmp_path):
    home = _make_home(tmp_path)
    fp = str(home / ".claude" / "dockwright" / "presets" / "verifier-settings.json")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home)
    assert r.returncode == 0
    hso = json.loads(r.stdout)["hookSpecificOutput"]
    assert "presets/verifier-settings.json" in hso["additionalContext"]
    assert "dockwright/presets" not in hso["additionalContext"]  # names canon, not deployed path

def test_renamed_tmux_conf_new_home_emits_warning(tmp_path):
    home = _make_home(tmp_path)
    fp = str(home / ".claude" / "dockwright" / "dockwright.tmux.conf")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home)
    assert r.returncode == 0
    assert "tmux/dockwright.conf" in json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]

def test_renamed_status_row_new_home_emits_warning(tmp_path):
    home = _make_home(tmp_path)
    fp = str(home / ".claude" / "dockwright" / "status_row.py")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home)
    assert r.returncode == 0
    assert "tmux/status_row.py" in json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]

def test_renamed_loops_registry_new_home_emits_warning(tmp_path):
    # loops-registry.md now deploys to dockwright/loops-registry.md (renamed);
    # the guard maps it back to the top-level canon source.
    home = _make_home(tmp_path)
    (home / ".claude" / "dockwright" / "loops-registry.md").parent.mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "dockwright" / "loops-registry.md").write_text("x")
    fp = str(home / ".claude" / "dockwright" / "loops-registry.md")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home)
    assert r.returncode == 0
    hso = json.loads(r.stdout)["hookSpecificOutput"]
    assert "loops-registry.md" in hso["additionalContext"]
    assert "dockwright/loops-registry.md" not in hso["additionalContext"]  # names canon, not deployed path


def test_dockwright_runtime_state_no_warning(tmp_path):
    home = _make_home(tmp_path)
    fp = str(home / ".claude" / "dockwright" / "active" / "sid.json")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home)
    assert r.returncode == 0 and r.stdout.strip() == ""

def test_renamed_branch_with_missing_canon_source_no_warning(tmp_path):
    # A renamed-deploy path whose case branch MATCHES but whose canon source was
    # removed (stale deployed file; the cp'd tmux files aren't --delete-pruned)
    # must fail open to no warning via the existence gate.
    home = _make_home(tmp_path)
    (home / "projects/personal/claude-orchestrator/deploy/tmux/status_row.py").unlink()
    fp = str(home / ".claude" / "orchestrator" / "status_row.py")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home)
    assert r.returncode == 0 and r.stdout.strip() == ""

def test_no_dockwright_repo_config_silent(tmp_path):
    # With [paths] dockwright_repo unset there is no canon to point at — the
    # guard must exit 0 silently (no warning), even for a would-be canon file.
    home = _make_home(tmp_path)
    fp = str(home / ".claude" / "scripts" / "selffix-trigger.sh")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home, dockwright_repo=None)
    assert r.returncode == 0 and r.stdout.strip() == ""
