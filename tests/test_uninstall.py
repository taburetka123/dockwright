import json
from pathlib import Path

import pytest

from dockwright import uninstall as un

ABS = "/Users/testop/projects/personal/claude-orchestrator/.venv/bin/orchestrator"
GUARD = "bash -c '\"$HOME/.claude/scripts/canon-edit-guard.sh\"'"

def _hook(cmd, timeout=5):
    return {"type": "command", "command": cmd, "timeout": timeout}

def _snippet_dict():
    return {"hooks": {
        "PreToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [_hook(GUARD)]}],
        "SessionStart": [{"hooks": [_hook("bash -c 'CLAUDE_PARENT_PID=$PPID @@DOCKWRIGHT_BIN@@ session-start'")]}],
        "Stop": [{"hooks": [_hook("bash -c 'CLAUDE_PARENT_PID=$PPID @@DOCKWRIGHT_BIN@@ stop'")]}],
    }}

def test_strip_removes_canonical_hooks_bare_and_abs():
    settings = {"hooks": {
        "SessionStart": [{"hooks": [_hook("bash -c '$PPID orchestrator session-start'")]}],
        "Stop": [{"hooks": [_hook(f"bash -c '$PPID {ABS} stop'")]}],
    }}
    out = un.strip_orchestrator_hooks(settings, _snippet_dict(), [ABS])
    assert "hooks" not in out

def test_bin_re_matches_both_generations():
    m = un._BIN_RE.search("bash -c 'CLAUDE_PARENT_PID=$PPID /r/.venv/bin/dockwright session-end'")
    assert m and m.group(1) == "/r/.venv/bin/dockwright"
    m = un._BIN_RE.search("bash -c '$PPID /r/claude-orchestrator/.venv/bin/orchestrator stop'")
    assert m and m.group(1) == "/r/claude-orchestrator/.venv/bin/orchestrator"
    assert un._BIN_RE.search("bash -c 'other-tool session-end'") is None

def test_strip_removes_dockwright_generation_hooks():
    dock = ABS.replace("bin/orchestrator", "bin/dockwright")
    settings = {"hooks": {
        "SessionStart": [{"hooks": [_hook("bash -c '$PPID dockwright session-start'")]}],
        "Stop": [{"hooks": [_hook(f"bash -c '$PPID {dock} stop'")]}],
    }}
    out = un.strip_orchestrator_hooks(settings, _snippet_dict(), [dock])
    assert "hooks" not in out

def test_strip_removes_stale_owned_subcommand_via_extra_bins():
    settings = {"hooks": {
        "PreCompact": [{"hooks": [_hook(f"bash -c '$PPID {ABS} manager-tts'")]}],
    }}
    out = un.strip_orchestrator_hooks(settings, None, [ABS])
    assert "hooks" not in out

def test_strip_removes_canon_edit_guard_with_and_without_snippet():
    settings = {"hooks": {"PreToolUse": [
        {"matcher": "Edit|Write|MultiEdit", "hooks": [_hook(GUARD)]}]}}
    assert "hooks" not in un.strip_orchestrator_hooks(settings, _snippet_dict(), [])
    assert "hooks" not in un.strip_orchestrator_hooks(settings, None, [])

def test_strip_preserves_foreign_hooks_matcher_and_top_level_keys():
    foreign_block = {"matcher": "Read", "hooks": [_hook("bash /other/native-hook.sh")]}
    settings = {
        "model": "opus",
        "statusLine": {"command": "x"},
        "hooks": {
            "PreToolUse": [foreign_block],
            "Stop": [{"hooks": [
                _hook(f"bash -c '$PPID {ABS} stop'"),
                _hook("bash /U/.claude/scripts/native-hook.sh", timeout=10),
            ]}],
        },
    }
    out = un.strip_orchestrator_hooks(settings, _snippet_dict(), [ABS])
    assert out["model"] == "opus"
    assert out["statusLine"] == {"command": "x"}
    assert out["hooks"]["PreToolUse"] == [foreign_block]
    stop_cmds = [h["command"] for b in out["hooks"]["Stop"] for h in b["hooks"]]
    assert stop_cmds == ["bash /U/.claude/scripts/native-hook.sh"]

def test_strip_is_idempotent_and_does_not_mutate_input():
    settings = {"hooks": {"Stop": [{"hooks": [_hook(f"bash -c '$PPID {ABS} stop'")]},
                                    {"hooks": [_hook("bash /other.sh")]}]}}
    snapshot = json.dumps(settings)
    once = un.strip_orchestrator_hooks(settings, None, [ABS])
    twice = un.strip_orchestrator_hooks(once, None, [ABS])
    assert once == twice
    assert json.dumps(settings) == snapshot

def test_strip_resolves_bin_from_settings_itself():
    # No extra_bins passed: the canonical Stop hook names the bin; the stale
    # manager-tts hook using the SAME bin must still strip.
    settings = {"hooks": {
        "Stop": [{"hooks": [_hook(f"bash -c '$PPID {ABS} stop'")]}],
        "PreCompact": [{"hooks": [_hook(f"bash -c '$PPID {ABS} manager-tts'")]}],
    }}
    out = un.strip_orchestrator_hooks(settings, None, [])
    assert "hooks" not in out


def test_provenance_stamp_recognizes_both_generations(tmp_path):
    new = tmp_path / "new.py"
    new.write_text("#!/usr/bin/env python3\n# deployed-from: dockwright@abc123 — do not edit\n")
    old = tmp_path / "old.py"
    old.write_text("#!/usr/bin/env python3\n# deployed-from: claude-orchestrator@abc123 — do not edit\n")
    foreign = tmp_path / "foreign.py"
    foreign.write_text("#!/usr/bin/env python3\n# deployed-from: some-other-tool@abc123\n")
    assert un._has_provenance_stamp(new)
    assert un._has_provenance_stamp(old)
    assert not un._has_provenance_stamp(foreign)


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        class R: returncode = 0
        return R()


@pytest.fixture
def fake_install(tmp_path, monkeypatch):
    """A complete fake installed footprint + foreign sentinels, fully under tmp."""
    home = tmp_path / "home"
    claude = home / ".claude"; codex = home / ".codex"
    lagents = home / "LaunchAgents"; lbin = home / ".local" / "bin"
    xdg = home / ".config" / "dockwright"
    state = claude / "orchestrator"; memory = claude / "manager-memory"
    repo = tmp_path / "repo"
    monkeypatch.setenv("HOME", str(home))

    cfg = tmp_path / "dockwright.toml"
    cfg.write_text('[loops]\nlabel_prefix = "com.dw-test"\n'
                   f'[paths]\noverlay_dir = "{tmp_path / "no-overlay"}"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))

    # Deterministic tool presence: never depend on the host having claude /
    # codex / launchctl on PATH (and never risk resolving the real ones).
    monkeypatch.setattr(
        un.shutil, "which",
        lambda name: f"/fake/bin/{name}" if name in ("claude", "codex", "launchctl") else None)

    # fake repo: deploy mirror + venv
    (repo / "deploy" / "agents").mkdir(parents=True)
    (repo / "deploy" / "agents" / "manager.core.md").write_text("core")
    (repo / "deploy" / "agents" / "worker.core.md").write_text("core")
    (repo / "deploy" / "commands").mkdir()
    (repo / "deploy" / "commands" / "manager.md").write_text("cmd")
    (repo / "deploy" / "commands" / "tab.md").write_text("cmd")
    (repo / "deploy" / "skills").mkdir()
    (repo / "deploy" / "skills" / "dockwright-recap").mkdir()
    (repo / "deploy" / "skills" / "dockwright-recap" / "SKILL.md").write_text("s")
    (repo / "deploy" / "scripts").mkdir()
    (repo / "deploy" / "scripts" / "gardener_gate.py").write_text("#!/usr/bin/env python3\n")
    (repo / "deploy" / "scripts" / "helper.cjs").write_text("module.exports = 1\n")
    (repo / "deploy" / "settings.snippet.json").write_text(json.dumps(_snippet_dict()))
    venv_bin = repo / ".venv" / "bin"; venv_bin.mkdir(parents=True)
    orch_bin = venv_bin / "orchestrator"; orch_bin.write_text("#!/bin/sh\n")

    # deployed claude surface
    (claude / "agents").mkdir(parents=True)
    (claude / "agents" / "manager.md").write_text("deployed")
    (claude / "agents" / "worker.md").write_text("deployed")
    (claude / "agents" / ".compose-stamp.json").write_text(
        json.dumps({"core": {"manager.md": "x", "worker.md": "x"}}))
    (claude / "agents" / "foreign-agent.md").write_text("FOREIGN")
    (claude / "commands").mkdir()
    (claude / "commands" / "manager.md").write_text("cmd")
    (claude / "commands" / "tab.md").write_text("cmd")
    (claude / "commands" / "foreign-cmd.md").write_text("FOREIGN")
    (claude / "skills" / "dockwright-recap").mkdir(parents=True)
    (claude / "skills" / "dockwright-recap" / "SKILL.md").write_text("s")
    (claude / "skills" / "foreign-skill").mkdir()
    (claude / "skills" / "foreign-skill" / "SKILL.md").write_text("FOREIGN")
    (claude / "scripts").mkdir()
    (claude / "scripts" / "gardener_gate.py").write_text(
        "#!/usr/bin/env python3\n# deployed-from: claude-orchestrator@abc123 — do not edit\n")
    (claude / "scripts" / "stale_monitor.py").write_text(
        "#!/usr/bin/env python3\n# deployed-from: claude-orchestrator@abc123 — do not edit\n")
    (claude / "scripts" / "helper.cjs").write_text("module.exports = 1\n")
    (claude / "scripts" / "foreign-hook.sh").write_text("#!/bin/bash\nFOREIGN\n")
    (claude / "statusline-command.sh").write_text("#!/bin/bash\n")
    (claude / "loops-registry.md").write_text("registry")
    (state / "active").mkdir(parents=True)
    memory.mkdir()
    for d in ("gardener", "bootlite", "worktree-prune", "selffix-findings", "selffix-retry"):
        (claude / d).mkdir()
    (claude / "selffix-trigger.log").write_text("log")
    for flag in ("gardener-stop", "frontier-stop", "bootlite-stop",
                 "worktree-prune-stop", "selffix-debug"):
        (claude / flag).write_text("")
    (claude / ".orchestrator-deploy").write_text("sha=abc\n")
    for i in range(3):
        (claude / f"settings.json.bak.{1000 + i}").write_text("{}")
    (claude / "settings.json.bak.keep-me").write_text("HAND-NAMED")

    abs_bin = str(orch_bin)
    (claude / "settings.json").write_text(json.dumps({
        "model": "opus",
        "hooks": {
            "SessionStart": [{"hooks": [_hook(f"bash -c 'CLAUDE_PARENT_PID=$PPID {abs_bin} session-start'")]}],
            "Stop": [{"hooks": [
                _hook(f"bash -c 'CLAUDE_PARENT_PID=$PPID {abs_bin} stop'"),
                _hook("bash /foreign/keeper.sh"),
            ]}],
            "PreToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [_hook(GUARD)]}],
        },
    }))

    # deployed codex surface (+ foreign sentinels)
    (codex / "agents").mkdir(parents=True)
    (codex / "agents" / "manager.toml").write_text("t")
    (codex / "agents" / "worker.toml").write_text("t")
    (codex / "commands").mkdir()
    (codex / "commands" / "manager.md").write_text("cmd")
    (codex / "commands" / "tab.md").write_text("cmd")
    (codex / "commands" / "foreign-cmd.md").write_text("FOREIGN")
    (codex / "skills" / "manager").mkdir(parents=True)
    (codex / "skills" / "manager" / "SKILL.md").write_text("w")
    (codex / "skills" / "tab").mkdir()
    (codex / "skills" / "tab" / "SKILL.md").write_text("w")
    (codex / "skills" / "foreign-tool").mkdir()
    (codex / "skills" / "foreign-tool" / "SKILL.md").write_text("FOREIGN")
    (codex / "hooks.json").write_text(json.dumps({
        "hooks": {"Stop": [{"hooks": [_hook(f"bash -c 'CLAUDE_PARENT_PID=$PPID {abs_bin} stop'")]}]}}))
    (codex / "hooks.json.bak.555").write_text("{}")
    (codex / "config.toml").write_text("USER CODEX CONFIG")  # must survive

    lagents.mkdir(parents=True)
    (lagents / "com.dw-test.gardener-gate.plist").write_text("<plist/>")
    (lagents / "com.dw-test.gardener-frontier.plist").write_text("<plist/>")
    (lagents / "com.other-tool.something.plist").write_text("FOREIGN")
    lbin.mkdir(parents=True)
    (lbin / "orchestrator").symlink_to(orch_bin)
    xdg.mkdir(parents=True)
    (xdg / "dockwright.toml").write_text("[paths]\n")

    argv = ["--yes",
            "--claude-dir", str(claude), "--codex-dir", str(codex),
            "--launch-agents-dir", str(lagents), "--local-bin-dir", str(lbin),
            "--repo-dir", str(repo), "--state-root", str(state),
            "--manager-memory-root", str(memory), "--xdg-config-dir", str(xdg)]
    return {"claude": claude, "codex": codex, "lagents": lagents, "lbin": lbin,
            "xdg": xdg, "state": state, "memory": memory, "repo": repo,
            "argv": argv, "tmp": tmp_path}


def _roots_from(fx):
    return un.Roots(claude_dir=fx["claude"], codex_dir=fx["codex"],
                    launch_agents_dir=fx["lagents"], local_bin_dir=fx["lbin"],
                    repo_dir=fx["repo"], state_root=fx["state"],
                    manager_memory_root=fx["memory"], xdg_config_dir=fx["xdg"])


def test_plan_paths_all_under_tmp_guard(fake_install):
    plan = un.build_plan(_roots_from(fake_install))
    tmp = fake_install["tmp"]
    for path in (plan.remove + [e.target for e in plan.hook_edits]
                 + [plist for _, plist in plan.launchd] + plan.prune_if_empty):
        assert str(path).startswith(str(tmp)), f"plan path escapes tmp: {path}"


def test_plan_notes_state_root_removal_loudly(fake_install):
    plan = un.build_plan(_roots_from(fake_install))
    state = fake_install["state"]
    matches = [n for n in plan.notes if "RUNTIME STATE" in n]
    assert len(matches) == 1
    assert str(state) in matches[0]


def test_full_footprint_removed_foreign_preserved(fake_install):
    fx = fake_install
    runner = FakeRunner()
    assert un.main(fx["argv"], run=runner) == 0
    claude, codex = fx["claude"], fx["codex"]

    for gone in (claude / "agents" / "manager.md", claude / "agents" / "worker.md",
                 claude / "agents" / ".compose-stamp.json",
                 claude / "commands" / "manager.md", claude / "commands" / "tab.md",
                 claude / "skills" / "dockwright-recap",
                 claude / "scripts" / "gardener_gate.py",
                 claude / "scripts" / "stale_monitor.py",
                 claude / "scripts" / "helper.cjs",
                 claude / "statusline-command.sh", claude / "loops-registry.md",
                 fx["state"], fx["memory"],
                 claude / "gardener", claude / "bootlite", claude / "worktree-prune",
                 claude / "selffix-findings", claude / "selffix-retry",
                 claude / "selffix-trigger.log",
                 claude / "gardener-stop", claude / "frontier-stop",
                 claude / "bootlite-stop", claude / "worktree-prune-stop",
                 claude / "selffix-debug", claude / ".orchestrator-deploy",
                 claude / "settings.json.bak.1000", claude / "settings.json.bak.1001",
                 claude / "settings.json.bak.1002",
                 codex / "agents" / "manager.toml", codex / "commands" / "manager.md",
                 codex / "skills" / "manager", codex / "skills" / "tab",
                 codex / "hooks.json", codex / "hooks.json.bak.555",
                 fx["xdg"], fx["lbin"] / "orchestrator",
                 fx["lagents"] / "com.dw-test.gardener-gate.plist",
                 fx["lagents"] / "com.dw-test.gardener-frontier.plist",
                 fx["repo"] / ".venv"):
        assert not gone.exists() and not gone.is_symlink(), f"should be gone: {gone}"

    # foreign survivors, byte-identical
    assert (claude / "agents" / "foreign-agent.md").read_text() == "FOREIGN"
    assert (claude / "commands" / "foreign-cmd.md").read_text() == "FOREIGN"
    assert (claude / "skills" / "foreign-skill" / "SKILL.md").read_text() == "FOREIGN"
    assert (claude / "scripts" / "foreign-hook.sh").read_text() == "#!/bin/bash\nFOREIGN\n"
    assert (claude / "settings.json.bak.keep-me").read_text() == "HAND-NAMED"
    assert (codex / "commands" / "foreign-cmd.md").read_text() == "FOREIGN"
    assert (codex / "skills" / "foreign-tool" / "SKILL.md").read_text() == "FOREIGN"
    assert (codex / "config.toml").read_text() == "USER CODEX CONFIG"
    assert (fx["lagents"] / "com.other-tool.something.plist").read_text() == "FOREIGN"
    assert codex.exists()  # config.toml keeps it alive; agents/commands stay (foreign files)

    # settings.json stripped, not deleted; foreign hook + top-level key survive
    out = json.loads((claude / "settings.json").read_text())
    assert out["model"] == "opus"
    cmds = [h["command"] for blocks in out.get("hooks", {}).values()
            for b in blocks for h in b["hooks"]]
    assert cmds == ["bash /foreign/keeper.sh"]
    baks = list(claude.glob("settings.json.uninstall-bak.*"))
    assert len(baks) == 1 and "orchestrator" in baks[0].read_text()

    # subprocess effects went through the runner only
    bootouts = [c for c in runner.calls if c[:2] == ["launchctl", "bootout"]]
    assert {c[2].rsplit("/", 1)[1] for c in bootouts} == {
        "com.dw-test.gardener-gate", "com.dw-test.gardener-frontier"}
    assert ["claude", "mcp", "remove", "--scope", "user", "claude-orchestrator"] in runner.calls
    assert ["codex", "mcp", "remove", "claude-orchestrator"] in runner.calls


def test_mcp_deregisters_both_generations(fake_install):
    fx = fake_install
    runner = FakeRunner()
    assert un.main(fx["argv"], run=runner) == 0
    assert ["claude", "mcp", "remove", "--scope", "user", "dockwright"] in runner.calls
    assert ["claude", "mcp", "remove", "--scope", "user", "claude-orchestrator"] in runner.calls
    assert ["codex", "mcp", "remove", "dockwright"] in runner.calls
    assert ["codex", "mcp", "remove", "claude-orchestrator"] in runner.calls


def test_symlink_plan_removes_dockwright_target(fake_install):
    fx = fake_install
    link = fx["lbin"] / "orchestrator"
    dock_bin = fx["repo"] / ".venv" / "bin" / "dockwright"
    dock_bin.write_text("#!/bin/sh\n")
    link.unlink()
    link.symlink_to(dock_bin)
    plan = un.build_plan(_roots_from(fx))
    assert link in plan.remove


def test_symlink_plan_removes_orchestrator_target(fake_install):
    fx = fake_install  # fixture default: link -> repo/.venv/bin/orchestrator
    link = fx["lbin"] / "orchestrator"
    plan = un.build_plan(_roots_from(fx))
    assert link in plan.remove


def test_symlink_plan_keeps_foreign_target_with_note(fake_install):
    fx = fake_install
    link = fx["lbin"] / "orchestrator"
    foreign = fx["tmp"] / "foreign-bin"
    foreign.write_text("#!/bin/sh\n")
    link.unlink()
    link.symlink_to(foreign)
    plan = un.build_plan(_roots_from(fx))
    assert link not in plan.remove
    assert any("kept" in n and str(link) in n for n in plan.notes)


def test_symlink_plan_removes_both_link_names(fake_install):
    """setup.sh creates a `dockwright` link while the pre-rename `orchestrator`
    link may still exist — uninstall inspects BOTH and removes each whose target
    is a recognized venv binary."""
    fx = fake_install
    orch_link = fx["lbin"] / "orchestrator"  # fixture default → orchestrator bin
    dock_bin = fx["repo"] / ".venv" / "bin" / "dockwright"
    dock_bin.write_text("#!/bin/sh\n")
    dock_link = fx["lbin"] / "dockwright"
    dock_link.symlink_to(dock_bin)
    plan = un.build_plan(_roots_from(fx))
    assert orch_link in plan.remove
    assert dock_link in plan.remove


def test_symlink_plan_keeps_foreign_dockwright_link_with_note(fake_install):
    """The keep-with-note discipline applies to the new `dockwright` link name
    too: a dockwright link pointing at a non-venv target is kept, not removed."""
    fx = fake_install
    foreign = fx["tmp"] / "foreign-bin"
    foreign.write_text("#!/bin/sh\n")
    dock_link = fx["lbin"] / "dockwright"
    dock_link.symlink_to(foreign)
    plan = un.build_plan(_roots_from(fx))
    assert dock_link not in plan.remove
    assert any("kept" in n and str(dock_link) in n for n in plan.notes)


def test_codex_dir_pruned_when_fully_emptied(fake_install):
    fx = fake_install
    for foreign in (fx["codex"] / "commands" / "foreign-cmd.md",
                    fx["codex"] / "config.toml"):
        foreign.unlink()
    import shutil as _sh
    _sh.rmtree(fx["codex"] / "skills" / "foreign-tool")
    assert un.main(fx["argv"], run=FakeRunner()) == 0
    assert not fx["codex"].exists()


def test_dry_run_removes_nothing(fake_install):
    fx = fake_install
    runner = FakeRunner()
    assert un.main(fx["argv"][1:] + ["--dry-run"], run=runner) == 0  # drop --yes
    assert (fx["claude"] / "agents" / "manager.md").exists()
    assert (fx["repo"] / ".venv").exists()
    assert runner.calls == []


def test_non_tty_without_yes_exits_2(fake_install, monkeypatch):
    fx = fake_install
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": staticmethod(lambda: False)})())
    runner = FakeRunner()
    assert un.main(fx["argv"][1:], run=runner) == 2  # argv without --yes
    assert (fx["claude"] / "agents" / "manager.md").exists()
    assert runner.calls == []


def test_rerun_after_uninstall_is_idempotent(fake_install):
    fx = fake_install
    assert un.main(fx["argv"], run=FakeRunner()) == 0
    assert un.main(fx["argv"], run=FakeRunner()) == 0


def test_corrupt_config_fails_closed(fake_install, monkeypatch, tmp_path):
    fx = fake_install
    bad = tmp_path / "bad.toml"
    bad.write_text("this is [not toml")
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(bad))
    runner = FakeRunner()
    assert un.main(fx["argv"], run=runner) == 1
    assert (fx["claude"] / "agents" / "manager.md").exists()
    assert runner.calls == []


def test_codex_hooks_foreign_top_level_key_rewritten_not_deleted(fake_install):
    fx = fake_install
    codex = fx["codex"]
    abs_bin = str(fx["repo"] / ".venv" / "bin" / "orchestrator")
    (codex / "hooks.json").write_text(json.dumps({
        "hooks": {"Stop": [{"hooks": [_hook(f"bash -c 'CLAUDE_PARENT_PID=$PPID {abs_bin} stop'")]}]},
        "foreignKey": 1,
    }))
    assert un.main(fx["argv"], run=FakeRunner()) == 0
    assert (codex / "hooks.json").exists()
    assert json.loads((codex / "hooks.json").read_text()) == {"foreignKey": 1}
    baks = list(codex.glob("hooks.json.uninstall-bak.*"))
    assert len(baks) == 1
    assert "orchestrator" in baks[0].read_text()


def test_corrupt_settings_json_fails_closed(fake_install):
    fx = fake_install
    (fx["claude"] / "settings.json").write_text("not json{")
    runner = FakeRunner()
    assert un.main(fx["argv"], run=runner) == 1
    assert (fx["claude"] / "agents" / "manager.md").exists()
    assert runner.calls == []


def test_uninstall_removes_both_homes_and_compat_symlinks(tmp_path, monkeypatch):
    """Post-migration (State B) layout: real state lives under ~/.claude/dockwright/,
    with compat symlinks at the legacy top-level paths. Uninstall must remove the
    new dockwright/ tree AND every now-dangling legacy compat symlink (including the
    `orchestrator -> dockwright` state-root link) — while keeping the operator
    overlay."""
    home = tmp_path / "home"
    claude = home / ".claude"
    dock = claude / "dockwright"
    (dock / "active").mkdir(parents=True)
    (dock / "active" / "w.json").write_text("{}")
    (dock / "manager-memory").mkdir()
    for d in ("gardener", "bootlite", "worktree-prune"):
        (dock / d).mkdir()
    (dock / "selffix" / "findings").mkdir(parents=True)
    (dock / "loops-registry.md").write_text("reg")
    (dock / ".deploy-stamp").write_text("sha=abc\n")
    stops = ("gardener-stop", "frontier-stop", "bootlite-stop", "worktree-prune-stop")
    for stop in stops:
        (dock / stop).write_text("")

    # compat symlinks at the legacy top-level names (what migrate-state leaves).
    links = {
        "orchestrator": "dockwright",
        "manager-memory": "dockwright/manager-memory",
        "gardener": "dockwright/gardener",
        "bootlite": "dockwright/bootlite",
        "worktree-prune": "dockwright/worktree-prune",
        "selffix-findings": "dockwright/selffix/findings",
        "loops-registry.md": "dockwright/loops-registry.md",
        ".orchestrator-deploy": "dockwright/.deploy-stamp",
        **{s: f"dockwright/{s}" for s in stops},
    }
    for name, target_rel in links.items():
        (claude / name).symlink_to(target_rel)

    # operator overlay (new home) + its own compat symlink — both KEPT.
    overlay = claude / "dockwright-overlay"
    (overlay / "commands").mkdir(parents=True)
    (claude / "orchestrator-overlay").symlink_to("dockwright-overlay")
    (claude / "statusline-command.sh").write_text("#!/bin/bash\n")

    codex = home / ".codex"
    lagents = home / "LaunchAgents"; lagents.mkdir(parents=True)
    lbin = home / ".local" / "bin"; lbin.mkdir(parents=True)
    xdg = home / ".config" / "dockwright"
    repo = tmp_path / "repo"
    for sub in ("agents", "commands", "skills", "scripts"):
        (repo / "deploy" / sub).mkdir(parents=True)
    (repo / ".venv" / "bin").mkdir(parents=True)

    monkeypatch.setenv("HOME", str(home))
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(f'[paths]\nstate_root = "{dock}"\noverlay_dir = "{overlay}"\n'
                   f'manager_memory = "{dock / "manager-memory"}"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    monkeypatch.setattr(un.shutil, "which", lambda name: None)

    roots = un.Roots(claude_dir=claude, codex_dir=codex, launch_agents_dir=lagents,
                     local_bin_dir=lbin, repo_dir=repo, state_root=dock,
                     manager_memory_root=dock / "manager-memory", xdg_config_dir=xdg)
    plan = un.build_plan(roots)
    un.execute_plan(plan, run=FakeRunner())

    # new dockwright/ home fully removed
    assert not dock.exists() and not dock.is_symlink()
    # every legacy compat symlink removed — none left dangling
    for name in links:
        p = claude / name
        assert not p.exists() and not p.is_symlink(), f"compat symlink survived: {name}"
    # operator overlay content kept (the compat symlink points at kept content)
    assert (overlay / "commands").exists()
    assert not (claude / "statusline-command.sh").exists()  # deployed, removed


def test_strip_removes_selffix_sessionend_hook():
    foreign = _hook("bash /other/native-hook.sh")          # genuinely foreign → survives
    settings = {"hooks": {"SessionEnd": [
        {"hooks": [foreign]},
        {"hooks": [_hook("bash /home/u/.claude/scripts/selffix-trigger.sh", timeout=30)]},
    ]}}
    out = un.strip_orchestrator_hooks(settings, None, [])
    cmds = [h["command"] for b in out.get("hooks", {}).get("SessionEnd", []) for h in b["hooks"]]
    assert cmds == ["bash /other/native-hook.sh"]           # foreign survives, selffix stripped
    assert not any("selffix-trigger.sh" in c for c in cmds)
