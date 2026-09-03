import hashlib, json, os, subprocess, time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "scripts" / "canon-edit-guard.sh"

def _run(stdin, home, dockwright_repo="__default__", extra_env=None):
    env = dict(os.environ, HOME=str(home), **(extra_env or {}))
    if dockwright_repo == "__default__":
        dockwright_repo = str(home / "projects/personal/claude-orchestrator")
    if dockwright_repo is not None:
        cfg = home / "dockwright.toml"
        cfg.write_text(f'[paths]\ndockwright_repo = "{dockwright_repo}"\n')
        env["DOCKWRIGHT_CONFIG"] = str(cfg)
    else:
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
    (canon / "agents").mkdir(parents=True)
    (canon / "agents" / "manager.core.md").write_text("core text\n")
    (canon / "agents" / "worker.md").write_text("plain core\n")
    (canon / "agents" / "sub").mkdir(parents=True)
    (canon / "agents" / "sub" / "x.core.md").write_text("nested core\n")
    (home / ".claude" / "agents").mkdir(parents=True)
    (home / ".claude" / "agents" / "manager.md").write_text("composed text\n")
    (home / ".claude" / "agents" / "worker.md").write_text("plain composed\n")
    (home / ".claude" / "agents" / "foreign.md").write_text("not ours\n")
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
    assert "orchestrator/presets" not in hso["additionalContext"]
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


def test_renamed_presets_new_home_emits_warning(tmp_path):
    home = _make_home(tmp_path)
    fp = str(home / ".claude" / "dockwright" / "presets" / "verifier-settings.json")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home)
    assert r.returncode == 0
    hso = json.loads(r.stdout)["hookSpecificOutput"]
    assert "presets/verifier-settings.json" in hso["additionalContext"]
    assert "dockwright/presets" not in hso["additionalContext"]

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
    home = _make_home(tmp_path)
    (home / ".claude" / "dockwright" / "loops-registry.md").parent.mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "dockwright" / "loops-registry.md").write_text("x")
    fp = str(home / ".claude" / "dockwright" / "loops-registry.md")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home)
    assert r.returncode == 0
    hso = json.loads(r.stdout)["hookSpecificOutput"]
    assert "loops-registry.md" in hso["additionalContext"]
    assert "dockwright/loops-registry.md" not in hso["additionalContext"]


def test_dockwright_runtime_state_no_warning(tmp_path):
    home = _make_home(tmp_path)
    fp = str(home / ".claude" / "dockwright" / "active" / "sid.json")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home)
    assert r.returncode == 0 and r.stdout.strip() == ""

def test_renamed_branch_with_missing_canon_source_no_warning(tmp_path):
    home = _make_home(tmp_path)
    (home / "projects/personal/claude-orchestrator/deploy/tmux/status_row.py").unlink()
    fp = str(home / ".claude" / "orchestrator" / "status_row.py")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home)
    assert r.returncode == 0 and r.stdout.strip() == ""

def test_no_dockwright_repo_config_silent(tmp_path):
    home = _make_home(tmp_path)
    fp = str(home / ".claude" / "scripts" / "selffix-trigger.sh")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home, dockwright_repo=None)
    assert r.returncode == 0 and r.stdout.strip() == ""


def _ctx(r):
    return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def _write_stamp(home, mapping, core_sources=None):
    stamp = home / ".claude" / "agents" / ".compose-stamp.json"
    stamp.write_text(json.dumps({"core": {}, "outputs": mapping,
                                 "core_sources": core_sources or {}}))


def _install_validator(home):
    src = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "asset_validator.py"
    (home / ".claude" / "scripts" / "asset_validator.py").write_text(src.read_text())


def _install_stub_validator(home, body):
    (home / ".claude" / "scripts" / "asset_validator.py").write_text(body)


def test_composed_agent_names_core_and_overlay_not_cp(tmp_path):
    home = _make_home(tmp_path)
    fp = str(home / ".claude" / "agents" / "manager.md")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home)
    assert r.returncode == 0, r.stderr
    ctx = _ctx(r)
    assert "agents/manager.core.md" in ctx
    assert "dockwright-overlay/manager" in ctx
    assert "cp-deployed" not in ctx


def test_plain_core_agent_is_still_reported_as_composed(tmp_path):
    home = _make_home(tmp_path)
    fp = str(home / ".claude" / "agents" / "worker.md")
    ctx = _ctx(_run(json.dumps({"tool_input": {"file_path": fp}}), home))
    assert "COMPOSED" in ctx and "cp-deployed" not in ctx


def test_foreign_agent_file_with_no_canon_is_silent(tmp_path):
    home = _make_home(tmp_path)
    fp = str(home / ".claude" / "agents" / "foreign.md")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home)
    assert r.returncode == 0 and r.stdout.strip() == ""


def test_foreign_agent_with_validator_present_emits_no_canon_note(tmp_path):
    home = _make_home(tmp_path)
    _install_validator(home)
    fp = home / ".claude" / "agents" / "foreign.md"
    fp.write_text("---\nname: foreign\ndescription: Not ours.\n---\n\nBody\n")
    r = _run(json.dumps({"tool_input": {"file_path": str(fp)}}), home)
    assert r.returncode == 0 and r.stdout.strip() == ""


def test_nested_agents_subpath_is_silent(tmp_path):
    home = _make_home(tmp_path)
    _install_validator(home)
    (home / ".claude" / "agents" / "sub").mkdir()
    (home / ".claude" / "agents" / "sub" / "x.md").write_text("x")
    fp = str(home / ".claude" / "agents" / "sub" / "x.md")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home)
    assert r.returncode == 0, r.stderr
    assert "COMPOSED" not in _ctx(r)


def test_non_md_agents_file_is_never_called_cp_deployed(tmp_path):
    home = _make_home(tmp_path)
    canon = home / "projects/personal/claude-orchestrator/deploy"
    (canon / "agents" / "vars.defaults.toml").write_text("x")
    (home / ".claude" / "agents" / "vars.defaults.toml").write_text("x")
    fp = str(home / ".claude" / "agents" / "vars.defaults.toml")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home)
    assert r.returncode == 0 and r.stdout.strip() == ""


def test_custom_overlay_dir_is_named(tmp_path):
    home = _make_home(tmp_path)
    cfg = home / "dockwright.toml"
    cfg.write_text(f'[paths]\ndockwright_repo = "{home}/projects/personal/claude-orchestrator"\n'
                   f'overlay_dir = "{home}/custom-overlay"\n')
    r = subprocess.run(["bash", str(SCRIPT)],
                       input=json.dumps({"tool_input": {"file_path": str(
                           home / ".claude" / "agents" / "manager.md")}}),
                       capture_output=True, text=True,
                       env=dict(os.environ, HOME=str(home), DOCKWRIGHT_CONFIG=str(cfg)))
    assert "custom-overlay/manager" in _ctx(r)


def test_legacy_overlay_dir_named_when_only_it_exists(tmp_path):
    home = _make_home(tmp_path)
    (home / ".claude" / "orchestrator-overlay").mkdir(parents=True)
    ctx = _ctx(_run(json.dumps({"tool_input": {"file_path": str(
        home / ".claude" / "agents" / "manager.md")}}), home))
    assert "orchestrator-overlay/manager" in ctx


def test_drift_against_the_compose_stamp_is_loud(tmp_path):
    home = _make_home(tmp_path)
    _write_stamp(home, {"manager.md": hashlib.sha256(b"something else\n").hexdigest()})
    ctx = _ctx(_run(json.dumps({"tool_input": {"file_path": str(
        home / ".claude" / "agents" / "manager.md")}}), home))
    assert "DRIFT" in ctx


def test_no_drift_paragraph_when_the_deployed_file_matches_the_stamp(tmp_path):
    home = _make_home(tmp_path)
    deployed = home / ".claude" / "agents" / "manager.md"
    _write_stamp(home, {"manager.md": hashlib.sha256(deployed.read_bytes()).hexdigest()})
    ctx = _ctx(_run(json.dumps({"tool_input": {"file_path": str(deployed)}}), home))
    assert "COMPOSED" in ctx and "DRIFT" not in ctx


def test_absent_corrupt_or_pre_outputs_stamp_claims_no_drift(tmp_path):
    home = _make_home(tmp_path)
    fp = json.dumps({"tool_input": {"file_path": str(
        home / ".claude" / "agents" / "manager.md")}})
    assert "DRIFT" not in _ctx(_run(fp, home))
    stamp = home / ".claude" / "agents" / ".compose-stamp.json"
    stamp.write_text("{not json")
    assert "DRIFT" not in _ctx(_run(fp, home))
    stamp.write_text(json.dumps({"core": {"manager.md": "abc"}}))
    assert "DRIFT" not in _ctx(_run(fp, home))


@pytest.mark.parametrize("rel, body, code", [
    ("rules/no-trigger.md", "# Legacy\n\nJust prose, no trigger line.\n", "W-RULE-TRIGGER"),
    ("commands/plain.md", "---\nname: plain\ndescription: x\n---\n\nBody\n", "W-NAMING"),
    ("agents/stranger.md", "no frontmatter here\n", "W-FRONTMATTER"),
    ("skills/plain/SKILL.md", "no frontmatter here\n", "W-FRONTMATTER"),
    ("flows/dangling.md", "See ~/.claude/rules/does-not-exist.md for the rest.\n",
     "W-REF-MISSING"),
])
def test_asset_warnings_for_the_touched_file_reach_the_session(tmp_path, rel, body, code):
    home = _make_home(tmp_path)
    _install_validator(home)
    bad = home / ".claude" / rel
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(body)
    ctx = _ctx(_run(json.dumps({"tool_input": {"file_path": str(bad)}}), home))
    assert code in ctx


def test_paths_outside_the_asset_classes_are_never_validated(tmp_path):
    home = _make_home(tmp_path)
    _install_stub_validator(home, "print(\"W-STUB touched\")\n")
    asset = home / ".claude" / "rules" / "anything.md"
    asset.write_text("x")
    assert "W-STUB" in _ctx(_run(json.dumps(
        {"tool_input": {"file_path": str(asset)}}), home))
    for rel in ("dockwright/active/sid.json", "scripts/selffix-trigger.sh"):
        r = _run(json.dumps({"tool_input": {"file_path": str(
            home / ".claude" / rel)}}), home)
        assert r.returncode == 0, r.stderr
        assert "W-STUB" not in r.stdout, rel


def test_asset_warnings_survive_an_unset_dockwright_repo(tmp_path):
    home = _make_home(tmp_path)
    _install_validator(home)
    bad = home / ".claude" / "rules" / "no-trigger.md"
    bad.write_text("# Legacy\n\nJust prose, no trigger line.\n")
    r = _run(json.dumps({"tool_input": {"file_path": str(bad)}}), home, dockwright_repo=None)
    assert "W-RULE-TRIGGER" in _ctx(r)


def test_clean_asset_file_stays_silent(tmp_path):
    home = _make_home(tmp_path)
    _install_validator(home)
    good = home / ".claude" / "rules" / "clean.md"
    good.write_text("# Clean\n\nTRIGGER: Load when testing.\n")
    r = _run(json.dumps({"tool_input": {"file_path": str(good)}}), home)
    assert r.returncode == 0 and r.stdout.strip() == ""


def test_absent_validator_does_not_break_the_canon_note(tmp_path):
    home = _make_home(tmp_path)
    ctx = _ctx(_run(json.dumps({"tool_input": {"file_path": str(
        home / ".claude" / "scripts" / "selffix-trigger.sh")}}), home))
    assert "cp-deployed" in ctx


def test_a_failing_validator_never_blocks_the_edit(tmp_path):
    home = _make_home(tmp_path)
    _install_stub_validator(
        home, "import sys\nsys.stderr.write(\"boom\\n\")\nsys.exit(2)\n")
    r = _run(json.dumps({"tool_input": {"file_path": str(
        home / ".claude" / "agents" / "manager.md")}}), home)
    assert r.returncode == 0, r.stderr
    assert "COMPOSED" in _ctx(r)


def test_a_hanging_validator_cannot_blow_the_hook_budget(tmp_path):
    home = _make_home(tmp_path)
    _install_stub_validator(home, "import time\ntime.sleep(30)\n")
    started = time.monotonic()
    r = _run(json.dumps({"tool_input": {"file_path": str(
        home / ".claude" / "agents" / "manager.md")}}), home)
    elapsed = time.monotonic() - started
    assert r.returncode == 0, r.stderr
    assert elapsed < 4, f"hook took {elapsed:.2f}s against a 5s budget"
    assert "COMPOSED" in _ctx(r)


def test_a_megabyte_emitting_validator_cannot_kill_the_note(tmp_path):
    home = _make_home(tmp_path)
    _install_stub_validator(
        home, "import sys\nsys.stdout.write(\"W-STUB \" + \"x\" * 3_000_000 + \"\\n\")\n")
    r = _run(json.dumps({"tool_input": {"file_path": str(
        home / ".claude" / "agents" / "manager.md")}}), home)
    assert r.returncode == 0, r.stderr
    ctx = _ctx(r)
    assert "COMPOSED" in ctx
    assert "truncated" in ctx
    assert len(ctx) < 20_000


def test_composed_and_drift_survive_an_unset_dockwright_repo(tmp_path):
    home = _make_home(tmp_path)
    _write_stamp(home, {"manager.md": hashlib.sha256(b"something else\n").hexdigest()},
                 core_sources={"manager.md": "manager.core.md"})
    fp = json.dumps({"tool_input": {"file_path": str(
        home / ".claude" / "agents" / "manager.md")}})
    ctx = _ctx(_run(fp, home, dockwright_repo=None))
    assert "COMPOSED" in ctx and "DRIFT" in ctx
    assert "manager.core.md" in ctx
    assert "cp-deployed" not in ctx

    _write_stamp(home, {"manager.md": hashlib.sha256(b"something else\n").hexdigest()})
    ctx = _ctx(_run(fp, home, dockwright_repo=None))
    assert "COMPOSED" in ctx and "DRIFT" in ctx


def test_an_unstamped_agent_stays_silent_without_a_canon(tmp_path):
    home = _make_home(tmp_path)
    _write_stamp(home, {"manager.md": "abc"})
    r = _run(json.dumps({"tool_input": {"file_path": str(
        home / ".claude" / "agents" / "foreign.md")}}), home, dockwright_repo=None)
    assert r.returncode == 0 and r.stdout.strip() == ""


def test_a_dotdot_path_resolves_before_it_is_classified(tmp_path):
    home = _make_home(tmp_path)
    _install_validator(home)
    fp = str(home / ".claude" / "rules" / ".." / "agents" / "manager.md")
    ctx = _ctx(_run(json.dumps({"tool_input": {"file_path": fp}}), home))
    assert "COMPOSED" in ctx
    assert "W-RULE" not in ctx


def test_a_symlinked_claude_home_is_still_guarded(tmp_path):
    home = _make_home(tmp_path)
    real = tmp_path / "real-claude"
    (home / ".claude").rename(real)
    (home / ".claude").symlink_to(real)
    ctx = _ctx(_run(json.dumps({"tool_input": {"file_path": str(
        home / ".claude" / "agents" / "manager.md")}}), home))
    assert "COMPOSED" in ctx


def test_a_file_symlinked_out_of_claude_is_still_guarded(tmp_path):
    home = _make_home(tmp_path)
    outside = tmp_path / "dotfiles"
    outside.mkdir()
    deployed = home / ".claude" / "agents" / "manager.md"
    real = outside / "manager.md"
    real.write_text(deployed.read_text())
    deployed.unlink()
    deployed.symlink_to(real)
    ctx = _ctx(_run(json.dumps({"tool_input": {"file_path": str(deployed)}}), home))
    assert "COMPOSED" in ctx


def test_a_symlink_from_outside_pointing_into_claude_is_guarded(tmp_path):
    home = _make_home(tmp_path)
    link = home / "manager-link.md"
    link.symlink_to(home / ".claude" / "agents" / "manager.md")
    ctx = _ctx(_run(json.dumps({"tool_input": {"file_path": str(link)}}), home))
    assert "COMPOSED" in ctx


def test_a_broken_python3_on_path_never_blocks_the_edit(tmp_path):
    home = _make_home(tmp_path)
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "python3").write_text("#!/bin/sh\nexit 2\n")
    (shim / "python3").chmod(0o755)
    r = _run(json.dumps({"tool_input": {"file_path": str(
        home / ".claude" / "agents" / "manager.md")}}), home,
        extra_env={"PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ""


def test_a_multiline_core_source_cannot_corrupt_the_line_protocol(tmp_path):
    home = _make_home(tmp_path)
    _write_stamp(home, {"manager.md": hashlib.sha256(b"something else\n").hexdigest()},
                 core_sources={"manager.md": "man\nager\r.core.md"})
    ctx = _ctx(_run(json.dumps({"tool_input": {"file_path": str(
        home / ".claude" / "agents" / "manager.md")}}), home, dockwright_repo=None))
    assert "agents/man ager .core.md" in ctx
