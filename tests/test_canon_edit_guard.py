import hashlib, json, os, subprocess, time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "scripts" / "canon-edit-guard.sh"

def _run(stdin, home, dockwright_repo="__default__", extra_env=None):
    """Run the guard. CANON_DIR now derives from [paths] dockwright_repo, so a
    config is written pointing at the canon `_make_home` builds. Pass
    dockwright_repo=None to simulate an unset key (guard must exit silently),
    extra_env to perturb the environment the hook runs in (a broken python3 on
    PATH, say)."""
    env = dict(os.environ, HOME=str(home), **(extra_env or {}))
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
    # Narrowed claim: with [paths] dockwright_repo unset there is no canon path
    # to NAME, so the cp-deployed branch — and only it — goes quiet, even for a
    # file that is genuinely cp-deployed. The other two sections do not depend on
    # that key and stay live with it unset: see
    # test_composed_and_drift_survive_an_unset_dockwright_repo (composed + DRIFT,
    # off the stamp) and test_asset_warnings_survive_an_unset_dockwright_repo.
    home = _make_home(tmp_path)
    fp = str(home / ".claude" / "scripts" / "selffix-trigger.sh")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home, dockwright_repo=None)
    assert r.returncode == 0 and r.stdout.strip() == ""


# --- composed agents, stamp drift, and asset warnings -------------------------

def _ctx(r):
    return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def _write_stamp(home, mapping, core_sources=None):
    stamp = home / ".claude" / "agents" / ".compose-stamp.json"
    stamp.write_text(json.dumps({"core": {}, "outputs": mapping,
                                 "core_sources": core_sources or {}}))


def _install_validator(home):
    """Installs the REAL deploy/scripts/asset_validator.py, so the tests below
    that assert on literal W-* codes are integration tests against Task 2 rather
    than against a mock (a mock would prove nothing about the hook reaching the
    live validator). The coupling is deliberate and has a cost: renaming a
    warning code in asset_validator.py surfaces here, in a file whose name gives
    no hint that the validator is the thing that moved."""
    src = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "asset_validator.py"
    (home / ".claude" / "scripts" / "asset_validator.py").write_text(src.read_text())


def _install_stub_validator(home, body):
    """A stand-in validator, for the properties the real one cannot exercise:
    misbehaviour (non-zero exit, hanging) and unconditional output."""
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
    # The silence tests above run in a world with no validator installed — which is
    # the INVERSE of production. This one proves the canon property with the warn
    # path live, on a file the validator has nothing to say about.
    home = _make_home(tmp_path)
    _install_validator(home)
    fp = home / ".claude" / "agents" / "foreign.md"
    fp.write_text("---\nname: foreign\ndescription: Not ours.\n---\n\nBody\n")
    r = _run(json.dumps({"tool_input": {"file_path": str(fp)}}), home)
    assert r.returncode == 0 and r.stdout.strip() == ""


def test_nested_agents_subpath_is_silent(tmp_path):
    # With no validator installed this file is silent for the wrong reason (the
    # whole warn path is off), which is the INVERSE of production — so install it
    # and pin the claim the name makes: no COMPOSED note for a nested path.
    # compose globs the core dir non-recursively, so it never emits one.
    home = _make_home(tmp_path)
    _install_validator(home)
    (home / ".claude" / "agents" / "sub").mkdir()
    (home / ".claude" / "agents" / "sub" / "x.md").write_text("x")
    fp = str(home / ".claude" / "agents" / "sub" / "x.md")
    r = _run(json.dumps({"tool_input": {"file_path": fp}}), home)
    assert r.returncode == 0, r.stderr
    assert "COMPOSED" not in _ctx(r)


def test_non_md_agents_file_is_never_called_cp_deployed(tmp_path):
    # setup.sh cp:s nothing into agents/ — it composes. A non-.md file there
    # (deploy/agents/vars.defaults.toml is a live one) misses the composed arm,
    # and must not fall through to cp wording naming a canon nobody copies from.
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
    assert "DRIFT" not in _ctx(_run(fp, home))                       # no stamp at all
    stamp = home / ".claude" / "agents" / ".compose-stamp.json"
    stamp.write_text("{not json")
    assert "DRIFT" not in _ctx(_run(fp, home))                       # malformed
    stamp.write_text(json.dumps({"core": {"manager.md": "abc"}}))    # pre-change deploy
    assert "DRIFT" not in _ctx(_run(fp, home))                       # no `outputs` key


@pytest.mark.parametrize("rel, body, code", [
    ("rules/no-trigger.md", "# Legacy\n\nJust prose, no trigger line.\n", "W-RULE-TRIGGER"),
    ("commands/plain.md", "---\nname: plain\ndescription: x\n---\n\nBody\n", "W-NAMING"),
    ("agents/stranger.md", "no frontmatter here\n", "W-FRONTMATTER"),
    ("skills/plain/SKILL.md", "no frontmatter here\n", "W-FRONTMATTER"),
    ("flows/dangling.md", "See ~/.claude/rules/does-not-exist.md for the rest.\n",
     "W-REF-MISSING"),
])
def test_asset_warnings_for_the_touched_file_reach_the_session(tmp_path, rel, body, code):
    # One case per asset class the filter admits. Pinning only rules/ and agents/
    # left commands/, flows/ and skills/ droppable from the filter forever with a
    # fully green suite.
    home = _make_home(tmp_path)
    _install_validator(home)
    bad = home / ".claude" / rel
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(body)
    ctx = _ctx(_run(json.dumps({"tool_input": {"file_path": str(bad)}}), home))
    assert code in ctx


def test_paths_outside_the_asset_classes_are_never_validated(tmp_path):
    # The filter is pinned from BELOW by the parametrised test above; this pins
    # its UPPER bound. It needs a validator that warns unconditionally: the real
    # one classifies by relpath itself and returns nothing for a non-asset path,
    # so widening the filter to `*)` is invisible to it. The first assertion
    # keeps the stub honest — if it stopped emitting, the negatives below would
    # pass vacuously.
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
    home = _make_home(tmp_path)          # no asset_validator.py in this home
    ctx = _ctx(_run(json.dumps({"tool_input": {"file_path": str(
        home / ".claude" / "scripts" / "selffix-trigger.sh")}}), home))
    assert "cp-deployed" in ctx


def test_a_failing_validator_never_blocks_the_edit(tmp_path):
    # A PreToolUse hook exiting non-zero is the BLOCK signal, and a command
    # substitution propagates the callee status under `set -e`. So a validator
    # that exits 2 — truncated mid-deploy, operator-modified, a renamed flag, a
    # python3 that cannot open the file — must not reach the hook exit status.
    home = _make_home(tmp_path)
    _install_stub_validator(
        home, "import sys\nsys.stderr.write(\"boom\\n\")\nsys.exit(2)\n")
    r = _run(json.dumps({"tool_input": {"file_path": str(
        home / ".claude" / "agents" / "manager.md")}}), home)
    assert r.returncode == 0, r.stderr
    assert "COMPOSED" in _ctx(r)          # and the note survives the failure


def test_a_hanging_validator_cannot_blow_the_hook_budget(tmp_path):
    # --max-seconds is enforced INSIDE the callee, so a validator that ignores it
    # (stale, partially deployed) or cannot service its SIGALRM (a regex that
    # stays in C) would hold the whole hook. The 5s budget belongs to the caller.
    home = _make_home(tmp_path)
    _install_stub_validator(home, "import time\ntime.sleep(30)\n")
    started = time.monotonic()
    r = _run(json.dumps({"tool_input": {"file_path": str(
        home / ".claude" / "agents" / "manager.md")}}), home)
    elapsed = time.monotonic() - started
    assert r.returncode == 0, r.stderr
    assert elapsed < 4, f"hook took {elapsed:.2f}s against a 5s budget"
    assert "COMPOSED" in _ctx(r)          # the hang costs the warnings, not the note


def test_a_megabyte_emitting_validator_cannot_kill_the_note(tmp_path):
    # warn_text is a single argv element to the final `python3 -c`. A rogue/corrupt
    # validator printing more than the exec arg limit makes that exec fail E2BIG
    # (status 126 — non-blocking, so fail-open holds — but WITHOUT the bash-side cap
    # the composed note dies with the exec: the script exits 126 under `set -e` and
    # emits nothing). The cap keeps the note alive and marks the cut visibly.
    home = _make_home(tmp_path)
    _install_stub_validator(
        home, "import sys\nsys.stdout.write(\"W-STUB \" + \"x\" * 3_000_000 + \"\\n\")\n")
    r = _run(json.dumps({"tool_input": {"file_path": str(
        home / ".claude" / "agents" / "manager.md")}}), home)
    assert r.returncode == 0, r.stderr          # never 126 out of the E2BIG exec
    ctx = _ctx(r)
    assert "COMPOSED" in ctx                     # the note survives the flood
    assert "truncated" in ctx                    # and the cut is announced, not silent
    assert len(ctx) < 20_000                     # not the full 3MB


def _code_lines():
    """Script lines minus comment-only ones: a guard must bind to the line that
    RUNS, never to prose that merely mentions it (drift-guard-tests.md)."""
    return [ln for ln in SCRIPT.read_text().splitlines()
            if not ln.lstrip().startswith("#")]


def test_the_validator_invocation_is_capped_and_fail_open():
    """Line-anchored, NOT a whole-file substring search — the comment block above
    the invocation names --max-seconds in prose, so a file-wide `in` check would
    stay green with the flag stripped from the command that runs.

    `2>/dev/null` and `|| true` are pinned structurally on purpose. Now that the
    call goes through an in-process wrapper that returns 0 whatever the validator
    does, no input can make either of them matter (the behaviour they used to be
    the only guard for is covered by test_a_failing_validator_never_blocks_the_edit);
    they remain as the fail-open belt for a failure of the WRAPPER itself.
    """
    invocations = [ln for ln in _code_lines() if '--repo "$CLAUDE_DIR"' in ln]
    assert len(invocations) == 1, invocations
    line = invocations[0]
    assert "--max-seconds 2" in line, line
    assert "2>/dev/null" in line, line
    assert "|| true" in line, line
    assert any(ln.strip().startswith('if [ -f "$validator" ]') for ln in _code_lines())


def test_composed_and_drift_survive_an_unset_dockwright_repo(tmp_path):
    # `dockwright compose` needs no [paths] dockwright_repo (its core dir is
    # package-relative), and that key defaults UNSET — so gating the composed and
    # DRIFT warnings on it made the headline warning inert on every install but
    # the author's, on exactly the file whose bytes the next compose deletes. The
    # stamp is self-sufficient; use it.
    home = _make_home(tmp_path)
    _write_stamp(home, {"manager.md": hashlib.sha256(b"something else\n").hexdigest()},
                 core_sources={"manager.md": "manager.core.md"})
    fp = json.dumps({"tool_input": {"file_path": str(
        home / ".claude" / "agents" / "manager.md")}})
    ctx = _ctx(_run(fp, home, dockwright_repo=None))
    assert "COMPOSED" in ctx and "DRIFT" in ctx
    assert "manager.core.md" in ctx           # named from the stamp, not a canon path
    assert "cp-deployed" not in ctx

    # A pre-`core_sources` stamp still proves the file is composed; only the
    # core's name degrades.
    _write_stamp(home, {"manager.md": hashlib.sha256(b"something else\n").hexdigest()})
    ctx = _ctx(_run(fp, home, dockwright_repo=None))
    assert "COMPOSED" in ctx and "DRIFT" in ctx


def test_an_unstamped_agent_stays_silent_without_a_canon(tmp_path):
    # The upper bound of the ungating above: with no canon AND no stamp entry
    # there is no evidence the file is generated, so the guard says nothing
    # rather than warn about every hand-written agent on the machine.
    home = _make_home(tmp_path)
    _write_stamp(home, {"manager.md": "abc"})
    r = _run(json.dumps({"tool_input": {"file_path": str(
        home / ".claude" / "agents" / "foreign.md")}}), home, dockwright_repo=None)
    assert r.returncode == 0 and r.stdout.strip() == ""


def test_a_dotdot_path_resolves_before_it_is_classified(tmp_path):
    # An un-normalized path missed the composed branch AND reached the validator
    # as `rules/../agents/manager.md`, which the validator classifies by prefix —
    # earning rule-class warnings on an agent file.
    home = _make_home(tmp_path)
    _install_validator(home)
    fp = str(home / ".claude" / "rules" / ".." / "agents" / "manager.md")
    ctx = _ctx(_run(json.dumps({"tool_input": {"file_path": fp}}), home))
    assert "COMPOSED" in ctx
    assert "W-RULE" not in ctx


def test_a_symlinked_claude_home_is_still_guarded(tmp_path):
    # ~/.claude is frequently a symlink into a dotfiles checkout. The pre-pass
    # resolves file_path's directories, so CLAUDE_DIR must be resolved on the
    # bash side too — otherwise the prefix test compares a physical path against
    # a symlinked one and the hook goes silent for the WHOLE install, on every
    # file, with nothing to distinguish that from "this file is fine".
    home = _make_home(tmp_path)
    real = tmp_path / "real-claude"
    (home / ".claude").rename(real)
    (home / ".claude").symlink_to(real)
    ctx = _ctx(_run(json.dumps({"tool_input": {"file_path": str(
        home / ".claude" / "agents" / "manager.md")}}), home))
    assert "COMPOSED" in ctx


def test_a_file_symlinked_out_of_claude_is_still_guarded(tmp_path):
    # The other direction of the same normalization: a per-file symlink OUT of
    # the tree (~/.claude/agents/manager.md -> ~/dotfiles/manager.md). Resolving
    # the leaf as well as its parents lands the path outside ~/.claude and drops
    # it — but the edit still writes the deployed agent, and the next compose
    # still deletes it, so the guard must still speak. Parents resolved, leaf
    # kept.
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
    # Upper bound on keeping the leaf unresolved: a path OUTSIDE ~/.claude whose
    # leaf symlinks in still writes the deployed agent, so the full-realpath
    # fallback has to catch it.
    home = _make_home(tmp_path)
    link = home / "manager-link.md"
    link.symlink_to(home / ".claude" / "agents" / "manager.md")
    ctx = _ctx(_run(json.dumps({"tool_input": {"file_path": str(link)}}), home))
    assert "COMPOSED" in ctx


def test_a_broken_python3_on_path_never_blocks_the_edit(tmp_path):
    # The stdin pre-pass is the OTHER python3 call, and a command substitution
    # propagates the callee status under `set -e` — so an interpreter that
    # cannot run the program exits the hook non-zero, which is Claude Code's
    # BLOCK signal. Same hazard as test_a_failing_validator_never_blocks_the_edit,
    # on the call that runs before any file has even been classified.
    home = _make_home(tmp_path)
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "python3").write_text("#!/bin/sh\nexit 2\n")
    (shim / "python3").chmod(0o755)
    r = _run(json.dumps({"tool_input": {"file_path": str(
        home / ".claude" / "agents" / "manager.md")}}), home,
        extra_env={"PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr    # exit 2 == PreToolUse BLOCK
    assert r.stdout.strip() == ""         # and fail-open means silence, not a note


def test_a_multiline_core_source_cannot_corrupt_the_line_protocol(tmp_path):
    # The pre-pass hands bash five NEWLINE-separated fields and the core name is
    # the last of them, read with `sed -n 5p`. A newline anywhere in the stamp's
    # core_sources value would add a sixth line and silently TRUNCATE the name
    # bash reads, so the value is sanitized before it enters the protocol.
    home = _make_home(tmp_path)
    _write_stamp(home, {"manager.md": hashlib.sha256(b"something else\n").hexdigest()},
                 core_sources={"manager.md": "man\nager\r.core.md"})
    ctx = _ctx(_run(json.dumps({"tool_input": {"file_path": str(
        home / ".claude" / "agents" / "manager.md")}}), home, dockwright_repo=None))
    assert "agents/man ager .core.md" in ctx
