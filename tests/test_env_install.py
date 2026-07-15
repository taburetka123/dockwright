import json
from pathlib import Path

from dockwright import env_install as ei

ABS = "/Users/testop/projects/personal/claude-orchestrator/.venv/bin/orchestrator"

def _hook(cmd, timeout=5):
    return {"type": "command", "command": cmd, "timeout": timeout}

def test_orch_subcommand_matches_bare_and_abs_and_none():
    assert ei.orch_subcommand("bash -c 'CLAUDE_PARENT_PID=$PPID orchestrator session-start'") == "session-start"
    assert ei.orch_subcommand(f"bash -c '$PPID {ABS} stop'") == "stop"
    assert ei.orch_subcommand("bash -c 'echo orchestrating nothing'") is None
    assert ei.orch_subcommand("some other hook") is None

def test_subcommand_matches_both_generations():
    assert ei.orch_subcommand("/abs/.venv/bin/dockwright session-end") == "session-end"
    assert ei.orch_subcommand("/abs/.venv/bin/orchestrator session-end") == "session-end"
    assert ei.orch_subcommand("/abs/.venv/bin/other-tool session-end") is None

def test_rendered_bin_extraction_matches_both_generations():
    m = ei._ORCH_BIN_RE.search("/r/.venv/bin/dockwright session-start")
    assert m and m.group(1) == "/r/.venv/bin/dockwright"
    m = ei._ORCH_BIN_RE.search("/r/claude-orchestrator/.venv/bin/orchestrator stop")
    assert m and m.group(1) == "/r/claude-orchestrator/.venv/bin/orchestrator"

def test_render_substitutes_placeholder():
    snippet = {"hooks": {"Stop": [{"hooks": [_hook("x @@DOCKWRIGHT_BIN@@ stop")]}]}}
    out = ei.render_snippet(snippet, ABS)
    assert out["hooks"]["Stop"][0]["hooks"][0]["command"] == f"x {ABS} stop"
    # original untouched (deep copy)
    assert "@@DOCKWRIGHT_BIN@@" in snippet["hooks"]["Stop"][0]["hooks"][0]["command"]

def test_merge_converts_bare_to_abs_in_place():
    existing = {"hooks": {"SessionStart": [{"hooks": [_hook("bash -c '$PPID orchestrator session-start'")]}]}}
    rendered = {"hooks": {"SessionStart": [{"hooks": [_hook(f"bash -c '$PPID {ABS} session-start'")]}]}}
    merged = ei.merge_hooks(existing, rendered)
    cmds = [h["command"] for b in merged["hooks"]["SessionStart"] for h in b["hooks"]]
    assert cmds == [f"bash -c '$PPID {ABS} session-start'"]  # replaced, not duplicated

def test_merge_is_idempotent_on_abs():
    rendered = {"hooks": {"Stop": [{"hooks": [_hook(f"$PPID {ABS} stop")]}]}}
    once = ei.merge_hooks({}, rendered)
    twice = ei.merge_hooks(once, rendered)
    assert once == twice

def test_merge_preserves_other_keys_and_foreign_hooks():
    existing = {
        "model": "opus",
        "hooks": {"Stop": [{"hooks": [_hook("some-foreign-tool run")]}]},
    }
    rendered = {"hooks": {"Stop": [{"hooks": [_hook(f"$PPID {ABS} stop")]}]}}
    merged = ei.merge_hooks(existing, rendered)
    assert merged["model"] == "opus"
    cmds = [h["command"] for b in merged["hooks"]["Stop"] for h in b["hooks"]]
    assert "some-foreign-tool run" in cmds
    assert f"$PPID {ABS} stop" in cmds

def test_merge_settings_file_claude_creates_and_never_writes_mcpservers(tmp_path):
    snippet = tmp_path / "snippet.json"
    snippet.write_text(json.dumps({
        "_note": "x", "mcpServers": {"foo": {}},
        "hooks": {"SessionStart": [{"hooks": [_hook("bash -c '$PPID @@DOCKWRIGHT_BIN@@ session-start'")]}]},
    }))
    target = tmp_path / "settings.json"
    ei.merge_settings_file(target, snippet, ABS, "claude")
    out = json.loads(target.read_text())
    assert "mcpServers" not in out
    assert ABS in out["hooks"]["SessionStart"][0]["hooks"][0]["command"]

def test_merge_settings_file_codex_keeps_only_hooks_and_backs_up(tmp_path):
    snippet = tmp_path / "snippet.json"
    snippet.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [_hook("$PPID @@DOCKWRIGHT_BIN@@ stop")]}]}}))
    target = tmp_path / "hooks.json"
    target.write_text(json.dumps({"hooks": {}, "junk": 1}))
    ei.merge_settings_file(target, snippet, ABS, "codex")
    out = json.loads(target.read_text())
    assert set(out.keys()) == {"hooks"}
    assert any(p.name.startswith("hooks.json.bak.") for p in tmp_path.iterdir())

def test_rendered_orch_bin_extracts_path_and_handles_empty():
    rendered = {"hooks": {"Stop": [{"hooks": [_hook(f"bash -c '$PPID {ABS} stop'")]}]}}
    assert ei.rendered_orch_bin(rendered) == ABS
    assert ei.rendered_orch_bin({"hooks": {}}) is None
    assert ei.rendered_orch_bin({}) is None

def test_orch_owned_subcommand_is_precise():
    # orchestrator-owned (carry the real bin path in executable position)
    assert ei.orch_owned_subcommand(f"bash -c '$PPID {ABS} manager-tts'", ABS) == "manager-tts"
    assert ei.orch_owned_subcommand(f"bash -c '$PPID {ABS} stop'", ABS) == "stop"
    # foreign bash-script hooks -> not owned
    assert ei.orch_owned_subcommand("bash /U/.claude/scripts/auto-commit-on-edit.sh", ABS) is None
    assert ei.orch_owned_subcommand("bash /U/.claude/scripts/selffix-trigger.sh", ABS) is None
    # contrived false-positive cases (orchestrator as arg / dir / different binary) -> not owned
    assert ei.orch_owned_subcommand("echo orchestrator status", ABS) is None
    assert ei.orch_owned_subcommand("git -C /repos/orchestrator status", ABS) is None
    assert ei.orch_owned_subcommand("bash /opt/orchestrator deploy.sh", ABS) is None

def test_merge_prunes_orphan_keeps_canonical():
    existing = {"hooks": {"Stop": [
        {"hooks": [_hook(f"bash -c '$PPID {ABS} stop'")]},
        {"hooks": [_hook(f"bash -c '$PPID {ABS} manager-tts'")]},
    ]}}
    rendered = {"hooks": {"Stop": [{"hooks": [_hook(f"bash -c '$PPID {ABS} stop'")]}]}}
    merged = ei.merge_hooks(existing, rendered)
    cmds = [h["command"] for b in merged["hooks"]["Stop"] for h in b["hooks"]]
    assert not any("manager-tts" in c for c in cmds)
    assert any(c == f"bash -c '$PPID {ABS} stop'" for c in cmds)

def test_merge_preserves_foreign_hook_sharing_block_during_prune():
    # Mirrors the deployed SessionEnd shape: one block mixes canonical session-end + foreign
    # selffix; the orphan manager-tts sits in its own block.
    existing = {"hooks": {"SessionEnd": [
        {"hooks": [
            _hook(f"bash -c '$PPID {ABS} session-end'"),
            _hook("bash /U/.claude/scripts/selffix-trigger.sh", timeout=10),
        ]},
        {"hooks": [_hook(f"bash -c '$PPID {ABS} manager-tts'")]},
    ]}}
    rendered = {"hooks": {"SessionEnd": [{"hooks": [_hook(f"bash -c '$PPID {ABS} session-end'")]}]}}
    merged = ei.merge_hooks(existing, rendered)
    cmds = [h["command"] for b in merged["hooks"]["SessionEnd"] for h in b["hooks"]]
    assert any("selffix-trigger.sh" in c for c in cmds)
    assert any("session-end" in c for c in cmds)
    assert not any("manager-tts" in c for c in cmds)

def test_merge_prune_is_idempotent_with_orphan():
    existing = {"hooks": {"Stop": [
        {"hooks": [_hook(f"bash -c '$PPID {ABS} stop'")]},
        {"hooks": [_hook(f"bash -c '$PPID {ABS} manager-tts'")]},
    ]}}
    rendered = {"hooks": {"Stop": [{"hooks": [_hook(f"bash -c '$PPID {ABS} stop'")]}]}}
    once = ei.merge_hooks(existing, rendered)
    twice = ei.merge_hooks(once, rendered)
    assert once == twice
    assert not any("manager-tts" in h["command"]
                   for b in once["hooks"]["Stop"] for h in b["hooks"])

def test_prune_drops_event_with_only_orphan_orchestrator_hooks():
    existing = {"hooks": {
        "Stop": [{"hooks": [_hook(f"bash -c '$PPID {ABS} stop'")]}],
        "PreCompact": [{"hooks": [_hook(f"bash -c '$PPID {ABS} manager-tts'")]}],
    }}
    rendered = {"hooks": {"Stop": [{"hooks": [_hook(f"bash -c '$PPID {ABS} stop'")]}]}}
    merged = ei.merge_hooks(existing, rendered)
    assert "PreCompact" not in merged["hooks"]
    assert "Stop" in merged["hooks"]

def test_prune_keeps_foreign_only_event():
    existing = {"hooks": {"PreToolUse": [{"hooks": [_hook("bash /U/foreign.sh")]}]}}
    rendered = {"hooks": {"Stop": [{"hooks": [_hook(f"bash -c '$PPID {ABS} stop'")]}]}}
    merged = ei.merge_hooks(existing, rendered)
    assert "PreToolUse" in merged["hooks"]
    assert any("foreign.sh" in h["command"]
               for b in merged["hooks"]["PreToolUse"] for h in b["hooks"])

def test_prune_preserves_block_matcher_key():
    existing = {"hooks": {"Stop": [
        {"matcher": "*", "hooks": [
            _hook(f"bash -c '$PPID {ABS} stop'"),
            _hook(f"bash -c '$PPID {ABS} manager-tts'"),
        ]},
    ]}}
    rendered = {"hooks": {"Stop": [{"hooks": [_hook(f"bash -c '$PPID {ABS} stop'")]}]}}
    merged = ei.merge_hooks(existing, rendered)
    block = merged["hooks"]["Stop"][0]
    assert block.get("matcher") == "*"
    cmds = [h["command"] for h in block["hooks"]]
    assert not any("manager-tts" in c for c in cmds)
    assert any("stop" in c for c in cmds)

def test_merge_settings_file_claude_prunes_orphan_and_preserves_everything_else(tmp_path):
    # The manager-specified regression: settings.json with an orphan orchestrator hook + a
    # canonical snippet WITHOUT it -> orphan gone, non-orchestrator hooks preserved, canonical
    # orchestrator hooks present, foreign top-level keys preserved.
    snippet = tmp_path / "snippet.json"
    snippet.write_text(json.dumps({"hooks": {
        "Stop": [{"hooks": [_hook("bash -c '$PPID @@DOCKWRIGHT_BIN@@ stop'")]}],
        "SessionEnd": [{"hooks": [_hook("bash -c '$PPID @@DOCKWRIGHT_BIN@@ session-end'")]}],
    }}))
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({
        "model": "opus",
        "hooks": {
            "Stop": [
                {"hooks": [_hook("bash /U/.claude/scripts/auto-commit-on-edit.sh", timeout=15)]},
                {"hooks": [_hook(f"bash -c '$PPID {ABS} stop'")]},
                {"hooks": [_hook(f"bash -c '$PPID {ABS} manager-tts'")]},
            ],
            "SessionEnd": [
                {"hooks": [
                    _hook(f"bash -c '$PPID {ABS} session-end'"),
                    _hook("bash /U/.claude/scripts/selffix-trigger.sh", timeout=10),
                ]},
            ],
        },
    }))
    ei.merge_settings_file(target, snippet, ABS, "claude")
    out = json.loads(target.read_text())
    cmds = [h["command"] for blocks in out["hooks"].values() for b in blocks for h in b["hooks"]]
    assert not any("manager-tts" in c for c in cmds)                       # orphan pruned
    assert any(c == f"bash -c '$PPID {ABS} stop'" for c in cmds)           # canonical present
    assert any("session-end" in c and ABS in c for c in cmds)             # canonical present
    assert any("auto-commit-on-edit.sh" in c for c in cmds)               # sibling-block foreign
    assert any("selffix-trigger.sh" in c for c in cmds)                   # mixed-block foreign
    assert out["model"] == "opus"                                          # foreign top key

def test_merge_settings_file_claude_prune_is_idempotent(tmp_path):
    snippet = tmp_path / "snippet.json"
    snippet.write_text(json.dumps({"hooks": {
        "Stop": [{"hooks": [_hook("bash -c '$PPID @@DOCKWRIGHT_BIN@@ stop'")]}],
    }}))
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [_hook(f"bash -c '$PPID {ABS} stop'")]},
        {"hooks": [_hook(f"bash -c '$PPID {ABS} manager-tts'")]},
    ]}}))
    ei.merge_settings_file(target, snippet, ABS, "claude")
    out1 = json.loads(target.read_text())
    ei.merge_settings_file(target, snippet, ABS, "claude")
    out2 = json.loads(target.read_text())
    assert out1 == out2
    cmds = [h["command"] for blocks in out1["hooks"].values() for b in blocks for h in b["hooks"]]
    assert not any("manager-tts" in c for c in cmds)

def test_merge_settings_file_codex_prunes_orphan(tmp_path):
    snippet = tmp_path / "snippet.json"
    snippet.write_text(json.dumps({"hooks": {
        "Stop": [{"hooks": [_hook("bash -c '$PPID @@DOCKWRIGHT_BIN@@ stop'")]}],
    }}))
    target = tmp_path / "hooks.json"
    target.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [_hook(f"bash -c '$PPID {ABS} stop'")]},
        {"hooks": [_hook(f"bash -c '$PPID {ABS} manager-tts'")]},
    ]}}))
    ei.merge_settings_file(target, snippet, ABS, "codex")
    out = json.loads(target.read_text())
    assert set(out.keys()) == {"hooks"}
    cmds = [h["command"] for b in out["hooks"]["Stop"] for h in b["hooks"]]
    assert not any("manager-tts" in c for c in cmds)
    assert any("stop" in c for c in cmds)


FOREIGN = "bash -c '\"$HOME/.claude/scripts/canon-edit-guard.sh\"'"

def test_merge_appends_foreign_hook_with_matcher():
    rendered = {"hooks": {"PreToolUse": [
        {"matcher": "Edit|Write|MultiEdit", "hooks": [_hook(FOREIGN)]}]}}
    merged = ei.merge_hooks({}, rendered)
    block = merged["hooks"]["PreToolUse"][0]
    assert block.get("matcher") == "Edit|Write|MultiEdit"
    assert block["hooks"][0]["command"] == FOREIGN

def test_merge_foreign_hook_is_idempotent():
    rendered = {"hooks": {"PreToolUse": [
        {"matcher": "Edit|Write|MultiEdit", "hooks": [_hook(FOREIGN)]}]}}
    once = ei.merge_hooks({}, rendered)
    twice = ei.merge_hooks(once, rendered)
    assert once == twice
    cmds = [h["command"] for b in twice["hooks"]["PreToolUse"] for h in b["hooks"]]
    assert cmds.count(FOREIGN) == 1

def test_merge_foreign_and_orchestrator_coexist_idempotently():
    rendered = {"hooks": {
        "Stop": [{"hooks": [_hook(f"bash -c '$PPID {ABS} stop'")]}],
        "PreToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [_hook(FOREIGN)]}]}}
    once = ei.merge_hooks({}, rendered)
    twice = ei.merge_hooks(once, rendered)
    assert once == twice
    assert any("stop" in h["command"] for b in twice["hooks"]["Stop"] for h in b["hooks"])
    assert twice["hooks"]["PreToolUse"][0]["matcher"] == "Edit|Write|MultiEdit"

def test_merge_appends_foreign_hook_beside_existing_foreign_block():
    existing = {"hooks": {"PreToolUse": [
        {"matcher": "Read", "hooks": [_hook("bash /other/native-hook.sh")]}]}}
    rendered = {"hooks": {"PreToolUse": [
        {"matcher": "Edit|Write|MultiEdit", "hooks": [_hook(FOREIGN)]}]}}
    merged = ei.merge_hooks(existing, rendered)
    cmds = [h["command"] for b in merged["hooks"]["PreToolUse"] for h in b["hooks"]]
    assert "bash /other/native-hook.sh" in cmds
    assert FOREIGN in cmds
    assert any(b.get("matcher") == "Edit|Write|MultiEdit" for b in merged["hooks"]["PreToolUse"])


def _cap_snippet(tmp_path):
    snippet = tmp_path / "snippet.json"
    snippet.write_text(json.dumps({"hooks": {
        "Stop": [{"hooks": [_hook("bash -c '$PPID @@DOCKWRIGHT_BIN@@ stop'")]}],
    }}))
    return snippet

def _bak_names(tmp_path, base="settings.json"):
    return sorted(p.name for p in tmp_path.glob(base + ".bak.*"))

def test_noop_rerun_writes_no_backup_and_no_write(tmp_path):
    snippet = _cap_snippet(tmp_path)
    target = tmp_path / "settings.json"
    ei.merge_settings_file(target, snippet, ABS, "claude")
    assert _bak_names(tmp_path) == []  # fresh create: nothing to back up
    ei.merge_settings_file(target, snippet, ABS, "claude")
    assert _bak_names(tmp_path) == []  # byte-identical re-run: no backup minted
    mtime = target.stat().st_mtime_ns
    ei.merge_settings_file(target, snippet, ABS, "claude")
    assert target.stat().st_mtime_ns == mtime  # and no rewrite either

def test_mutating_run_still_backs_up_first(tmp_path):
    snippet = _cap_snippet(tmp_path)
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"model": "opus"}))
    ei.merge_settings_file(target, snippet, ABS, "claude")
    baks = _bak_names(tmp_path)
    assert len(baks) == 1
    assert json.loads((tmp_path / baks[0]).read_text()) == {"model": "opus"}

def test_backups_capped_at_keep(tmp_path):
    snippet = _cap_snippet(tmp_path)
    target = tmp_path / "settings.json"
    for i in range(ei.BACKUP_KEEP + 4):
        # alternate the orch bin so every run is a real mutation
        ei.merge_settings_file(target, snippet, f"{ABS}{i}", "claude")
    baks = _bak_names(tmp_path)
    assert len(baks) == ei.BACKUP_KEEP
    # newest survive: suffixes strictly increasing, so the kept set is the max-5
    suffixes = sorted(int(n.rsplit(".", 1)[1]) for n in baks)
    assert suffixes == sorted(suffixes)[-ei.BACKUP_KEEP:]

def test_preexisting_pile_pruned_in_one_run(tmp_path):
    snippet = _cap_snippet(tmp_path)
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"hooks": {}}))
    for i in range(20):
        (tmp_path / f"settings.json.bak.{1000 + i}").write_text("{}")
    ei.merge_settings_file(target, snippet, ABS, "claude")
    assert len(_bak_names(tmp_path)) == ei.BACKUP_KEEP

def test_non_digit_backups_never_pruned(tmp_path):
    snippet = _cap_snippet(tmp_path)
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"hooks": {}}))
    keep_mine = tmp_path / "settings.json.bak.mine"
    keep_mine.write_text("precious")
    for i in range(10):
        (tmp_path / f"settings.json.bak.{2000 + i}").write_text("{}")
    ei.merge_settings_file(target, snippet, ABS, "claude")
    assert keep_mine.read_text() == "precious"
    assert "settings.json.bak.mine" in _bak_names(tmp_path)

def test_codex_target_gets_same_cap(tmp_path):
    snippet = _cap_snippet(tmp_path)
    target = tmp_path / "hooks.json"
    for i in range(ei.BACKUP_KEEP + 3):
        ei.merge_settings_file(target, snippet, f"{ABS}{i}", "codex")
    assert len(_bak_names(tmp_path, "hooks.json")) == ei.BACKUP_KEEP
