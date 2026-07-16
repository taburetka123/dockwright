"""`dockwright uninstall` — provenance-driven removal of the installed footprint.

Reverses setup.sh and the optional loop installers: boots out the launchd
loops, deregisters the MCP server, strips exactly the orchestrator-owned hooks
out of settings.json / hooks.json (foreign hooks and every other settings key
survive), then removes the deployed files. Removal is DERIVED, never a
hardcoded glob: `# deployed-from:` provenance stamps for scripts, the
.compose-stamp.json sidecar for agents, the repo's deploy/ listing for
commands/skills, config-resolved paths for state. A file this tool cannot
positively identify as its own is left alone.

Deliberately kept (printed as notes): the clone itself, settings.json
(stripped, not deleted) plus a fresh <name>.uninstall-bak.<ns> safety copy —
a suffix chosen OUTSIDE the .bak.<digits> pattern so the bak-pile sweep can
never delete it — a hand-authored ~/.claude/dockwright.toml, and the operator
overlay dir.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .env_install import ORCH_SUBCOMMANDS, orch_owned_subcommand, orch_subcommand

MCP_SERVER_NAME = "dockwright"
LEGACY_MCP_SERVER_NAME = "claude-orchestrator"   # one-release: uninstall both
PROVENANCE_MARKER = "# deployed-from: dockwright@"
LEGACY_PROVENANCE_MARKER = "# deployed-from: claude-orchestrator@"
CANON_GUARD_MARKER = "canon-edit-guard.sh"
SELFFIX_TRIGGER_MARKER = "selffix-trigger.sh"

_BIN_RE = re.compile(
    r"(\S*(?:dockwright|orchestrator))\s+(?:"
    + "|".join(re.escape(s) for s in ORCH_SUBCOMMANDS) + r")\b"
)


@dataclass
class Roots:
    claude_dir: Path
    codex_dir: Path
    launch_agents_dir: Path
    local_bin_dir: Path
    repo_dir: Path
    state_root: Path
    manager_memory_root: Path
    xdg_config_dir: Path


@dataclass
class HookEdit:
    target: Path
    new_text: str | None  # None => delete the file (codex hooks.json emptied out)
    removed: int


@dataclass
class Plan:
    launchd: list[tuple[str, Path]] = field(default_factory=list)
    mcp: list[list[str]] = field(default_factory=list)
    hook_edits: list[HookEdit] = field(default_factory=list)
    remove: list[Path] = field(default_factory=list)
    prune_if_empty: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.launchd or self.mcp or self.hook_edits or self.remove)


def _orch_bins(settings: dict, extra: list[str]) -> set[str]:
    """Every orchestrator binary path the settings' own canonical hooks name,
    plus caller-supplied candidates (repo venv bin, symlink target)."""
    bins = {b for b in extra if b}
    for blocks in settings.get("hooks", {}).values():
        for block in blocks:
            for hook in block.get("hooks", []):
                m = _BIN_RE.search(hook.get("command", ""))
                if m:
                    bins.add(m.group(1))
    return bins


def strip_orchestrator_hooks(settings: dict, snippet: dict | None,
                             extra_bins: list[str]) -> dict:
    """Inverse of env_install.merge_hooks: drop exactly the hooks this tool
    installed — canonical subcommands (bare or by path), stale subcommands
    owned by any known orchestrator binary, and the snippet's foreign-shaped
    hooks (canon-edit-guard; its script is deleted by the uninstall, so the
    hook would fire a dead command on every Edit). Everything else survives
    byte-identical."""
    our_foreign = set()
    if snippet:
        for blocks in snippet.get("hooks", {}).values():
            for block in blocks:
                for hook in block.get("hooks", []):
                    cmd = hook.get("command", "")
                    if cmd and orch_subcommand(cmd) is None:
                        our_foreign.add(cmd)
    bins = _orch_bins(settings, extra_bins)

    def ours(cmd: str) -> bool:
        if orch_subcommand(cmd) is not None:
            return True
        if any(orch_owned_subcommand(cmd, b) is not None for b in bins):
            return True
        if cmd in our_foreign or CANON_GUARD_MARKER in cmd or SELFFIX_TRIGGER_MARKER in cmd:
            return True
        return False

    out = copy.deepcopy(settings)
    hooks = out.get("hooks", {})
    for event in list(hooks.keys()):
        new_blocks = []
        for block in hooks[event]:
            kept = [h for h in block.get("hooks", [])
                    if not ours(h.get("command", ""))]
            if kept:
                new_blocks.append({**block, "hooks": kept})
        if new_blocks:
            hooks[event] = new_blocks
        else:
            del hooks[event]
    if "hooks" in out and not out["hooks"]:
        del out["hooks"]
    return out


def _count_hooks(settings: dict) -> int:
    return sum(len(block.get("hooks", []))
               for blocks in settings.get("hooks", {}).values() for block in blocks)


def _load_snippet(repo_dir: Path) -> dict | None:
    path = repo_dir / "deploy" / "settings.snippet.json"
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _has_provenance_stamp(path: Path) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = (fh.readline(), fh.readline())
    except OSError:
        return False
    return any(line.startswith((PROVENANCE_MARKER, LEGACY_PROVENANCE_MARKER)) for line in head)


def _numeric_baks(dirpath: Path, base: str) -> list[Path]:
    prefix = base + ".bak."
    return sorted(p for p in dirpath.glob(prefix + "*")
                  if p.name[len(prefix):].isdigit())


def _add(plan: Plan, path: Path) -> None:
    if path.is_symlink() or path.exists():
        plan.remove.append(path)


def build_plan(roots: Roots) -> Plan:
    """Read-only footprint derivation. Every path appended here is positively
    identified as dockwright's own; foreign files are never matched."""
    plan = Plan()

    prefix = config.loop_label_prefix()
    for plist in sorted(roots.launch_agents_dir.glob(prefix + ".*.plist")):
        plan.launchd.append((plist.name[: -len(".plist")], plist))

    for server_name in (MCP_SERVER_NAME, LEGACY_MCP_SERVER_NAME):
        if shutil.which("claude"):
            plan.mcp.append(["claude", "mcp", "remove", "--scope", "user", server_name])
        if shutil.which("codex"):
            plan.mcp.append(["codex", "mcp", "remove", server_name])

    snippet = _load_snippet(roots.repo_dir)
    extra_bins = [str(roots.repo_dir / ".venv" / "bin" / "dockwright"),
                  str(roots.repo_dir / ".venv" / "bin" / "orchestrator")]
    # Both console-script link names may exist in ~/.local/bin: setup.sh creates
    # `dockwright`; the pre-rename `orchestrator` link persists until removed.
    # Inspect BOTH — feed each target into extra_bins for hook stripping, and
    # decide removal per link below.
    bin_links: list[tuple[Path, str]] = []
    for link_name in ("dockwright", "orchestrator"):
        link = roots.local_bin_dir / link_name
        link_target = ""
        if link.is_symlink():
            try:
                link_target = os.readlink(link)
            except OSError:
                link_target = ""
            if link_target:
                extra_bins.append(link_target)
        bin_links.append((link, link_target))

    for target, mode in ((roots.claude_dir / "settings.json", "claude"),
                         (roots.codex_dir / "hooks.json", "codex")):
        if not target.exists():
            continue
        data = json.loads(target.read_text())
        stripped = strip_orchestrator_hooks(data, snippet, extra_bins)
        removed = _count_hooks(data) - _count_hooks(stripped)
        if mode == "codex" and not stripped:
            # Empty after stripping => every byte of content was orchestrator-owned;
            # a single foreign key (or hook) keeps the file alive on the elif path.
            plan.hook_edits.append(HookEdit(target, None, removed))
        elif stripped != data:
            plan.hook_edits.append(
                HookEdit(target, json.dumps(stripped, indent=2) + "\n", removed))

    agents_dir = roots.claude_dir / "agents"
    stamp = agents_dir / ".compose-stamp.json"
    core_names: list[str] = []
    if stamp.exists():
        try:
            core_names = sorted(json.loads(stamp.read_text()).get("core", {}))
        except (OSError, json.JSONDecodeError):
            core_names = []
    if not core_names:
        core_names = sorted(p.name.replace(".core.md", ".md")
                            for p in (roots.repo_dir / "deploy" / "agents").glob("*.core.md"))
    for name in core_names:
        _add(plan, agents_dir / name)
        _add(plan, roots.codex_dir / "agents" / (Path(name).stem + ".toml"))
    _add(plan, stamp)

    command_names = sorted(p.name for p in (roots.repo_dir / "deploy" / "commands").glob("*.md"))
    overlay_commands = config.overlay_dir() / "commands"
    if overlay_commands.is_dir():
        command_names += sorted(p.name for p in overlay_commands.glob("*.md"))
    for name in command_names:
        _add(plan, roots.claude_dir / "commands" / name)
        _add(plan, roots.codex_dir / "commands" / name)
        _add(plan, roots.codex_dir / "skills" / Path(name).stem)

    for skill in sorted((roots.repo_dir / "deploy" / "skills").glob("*")):
        _add(plan, roots.claude_dir / "skills" / skill.name)

    scripts_dir = roots.claude_dir / "scripts"
    deploy_script_names = {p.name for p in (roots.repo_dir / "deploy" / "scripts").glob("*")
                           if p.is_file()}
    deploy_script_names.add("stale_monitor.py")
    if scripts_dir.is_dir():
        for f in sorted(scripts_dir.iterdir()):
            if f.is_file() and (f.name in deploy_script_names or _has_provenance_stamp(f)):
                plan.remove.append(f)

    # State artifacts live at TWO homes during the one-release migration window:
    # the new ~/.claude/dockwright/ tree (state_root removes it wholesale) and the
    # legacy top-level ~/.claude/<name> paths — real files on an un-migrated
    # install, compat symlinks after `migrate-state` ran. List both so uninstall
    # is correct pre- AND post-migration. _add is existence-gated; _remove_path
    # unlinks a (possibly dangling) symlink without following it, so removing the
    # new tree then the legacy `orchestrator -> dockwright` link is order-safe.
    dockwright_home = roots.claude_dir / "dockwright"
    for p in (
        roots.claude_dir / "statusline-command.sh",   # top-level, never migrated
        roots.state_root,
        roots.manager_memory_root,
        roots.xdg_config_dir,
        # New dockwright/ home. Redundant with state_root removal unless state_root
        # is pinned elsewhere (migrate.py always targets ~/.claude/dockwright/*).
        dockwright_home / "loops-registry.md",
        dockwright_home / "manager-memory",
        dockwright_home / "gardener",
        dockwright_home / "bootlite",
        dockwright_home / "worktree-prune",
        dockwright_home / "selffix",
        dockwright_home / "gardener-stop",
        dockwright_home / "frontier-stop",
        dockwright_home / "bootlite-stop",
        dockwright_home / "worktree-prune-stop",
        dockwright_home / ".deploy-stamp",
        # Legacy top-level home: real dirs/files on an un-migrated install, compat
        # symlinks after migrate-state (removed here so none dangle). Includes the
        # `orchestrator -> dockwright` state-root link itself.
        roots.claude_dir / "orchestrator",
        roots.claude_dir / "loops-registry.md",
        roots.claude_dir / "manager-memory",
        roots.claude_dir / "gardener",
        roots.claude_dir / "bootlite",
        roots.claude_dir / "worktree-prune",
        roots.claude_dir / "selffix-findings",
        roots.claude_dir / "selffix-retry",
        roots.claude_dir / "selffix-trigger.log",
        roots.claude_dir / "selffix-nudges.log",
        roots.claude_dir / "selffix-nudges.log.tmp",
        roots.claude_dir / "selffix-debug",
        roots.claude_dir / "gardener-stop",
        roots.claude_dir / "frontier-stop",
        roots.claude_dir / "bootlite-stop",
        roots.claude_dir / "worktree-prune-stop",
        roots.claude_dir / ".orchestrator-deploy",
    ):
        _add(plan, p)
    if roots.state_root.is_symlink() or roots.state_root.exists():
        plan.notes.append(
            f"{roots.state_root} is the orchestrator RUNTIME STATE tree (active workers, "
            "artifacts, handoffs, presets) — removing it is irreversible")
    plan.remove += _numeric_baks(roots.claude_dir, "settings.json")
    plan.remove += _numeric_baks(roots.codex_dir, "hooks.json")

    for link, link_target in bin_links:
        if link.is_symlink():
            if link_target.endswith((".venv/bin/dockwright", ".venv/bin/orchestrator")):
                plan.remove.append(link)
            else:
                plan.notes.append(
                    f"kept {link} — symlink target {link_target!r} is not a dockwright venv binary")

    _add(plan, roots.repo_dir / ".venv")  # LAST: the running binary lives here

    plan.prune_if_empty = [roots.codex_dir / "agents", roots.codex_dir / "commands",
                           roots.codex_dir / "skills", roots.codex_dir]
    plan.notes.append(f"kept the clone at {roots.repo_dir} — delete it yourself when done")
    plan.notes.append(f"kept {roots.claude_dir / 'settings.json'} (orchestrator hooks stripped; "
                      "an .uninstall-bak.<ts> safety copy sits beside it)")
    if (roots.claude_dir / "dockwright.toml").exists():
        plan.notes.append(f"kept hand-authored {roots.claude_dir / 'dockwright.toml'}")
    if config.overlay_dir().exists():
        plan.notes.append(f"kept operator overlay {config.overlay_dir()}")
    return plan


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def execute_plan(plan: Plan, run=subprocess.run) -> None:
    """Apply the plan. EVERY subprocess effect (launchctl, claude/codex mcp)
    goes through `run` so tests inject a recorder and never touch the machine."""
    for label, plist in plan.launchd:
        if shutil.which("launchctl"):
            run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
                check=False, capture_output=True)
        plist.unlink(missing_ok=True)
    for argv in plan.mcp:
        run(argv, check=False, capture_output=True)
    for edit in plan.hook_edits:
        if edit.new_text is None:
            # Whole-file delete: everything inside was positively orchestrator-owned
            # (reproducible from the repo snippet), so no backup — a leftover
            # .uninstall-bak would keep the emptied ~/.codex dir from pruning.
            edit.target.unlink()
            continue
        current = edit.target.read_text()
        edit.target.with_name(
            edit.target.name + f".uninstall-bak.{time.time_ns()}").write_text(current)
        edit.target.write_text(edit.new_text)
    for path in plan.remove:
        _remove_path(path)
    for d in plan.prune_if_empty:
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()


def _print_plan(plan: Plan) -> None:
    if plan.launchd:
        print("launchd loops (bootout + remove plist):")
        for label, plist in plan.launchd:
            print(f"  {label}  [{plist}]")
    if plan.mcp:
        print("MCP deregistrations:")
        for argv in plan.mcp:
            print("  " + " ".join(argv))
    if plan.hook_edits:
        print("hook removals (foreign hooks + other settings keys untouched):")
        for edit in plan.hook_edits:
            action = "delete (only orchestrator hooks inside)" if edit.new_text is None \
                else f"strip {edit.removed} orchestrator hook(s)"
            print(f"  {edit.target}: {action}")
    if plan.remove:
        print("remove:")
        for path in plan.remove:
            print(f"  {path}")
    for note in plan.notes:
        print(f"note: {note}")


def main(argv=None, run=subprocess.run) -> int:
    p = argparse.ArgumentParser(
        description="Remove everything setup.sh (and the optional loop installers) deployed.")
    p.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--claude-dir", type=Path, default=None)
    p.add_argument("--codex-dir", type=Path, default=None)
    p.add_argument("--launch-agents-dir", type=Path, default=None)
    p.add_argument("--local-bin-dir", type=Path, default=None)
    p.add_argument("--repo-dir", type=Path, default=None)
    p.add_argument("--state-root", type=Path, default=None)
    p.add_argument("--manager-memory-root", type=Path, default=None)
    p.add_argument("--xdg-config-dir", type=Path, default=None)
    args = p.parse_args(argv)

    err = config.load_error()
    if err is not None:
        print(f"ERROR: refusing to uninstall with a corrupt dockwright.toml "
              f"({config.config_path()}): {err}", file=sys.stderr)
        print("Fix or delete the config file, then re-run.", file=sys.stderr)
        return 1

    home = Path(os.environ.get("HOME", str(Path.home())))
    xdg_base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    roots = Roots(
        claude_dir=args.claude_dir or config.claude_config_home(),
        codex_dir=args.codex_dir or home / ".codex",
        launch_agents_dir=args.launch_agents_dir or home / "Library" / "LaunchAgents",
        local_bin_dir=args.local_bin_dir or home / ".local" / "bin",
        repo_dir=args.repo_dir or Path(__file__).resolve().parents[2],
        state_root=args.state_root or config.state_root(),
        manager_memory_root=args.manager_memory_root or config.manager_memory_root(),
        xdg_config_dir=args.xdg_config_dir
        or (Path(xdg_base) if xdg_base else home / ".config") / "dockwright",
    )

    try:
        plan = build_plan(roots)
    except json.JSONDecodeError as e:
        print(f"ERROR: refusing to uninstall — unparseable JSON in a hooks/settings file: {e}",
              file=sys.stderr)
        print("Fix or remove the corrupt file, then re-run.", file=sys.stderr)
        return 1
    if plan.empty():
        print("Nothing to uninstall.")
        return 0
    _print_plan(plan)
    if args.dry_run:
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print("Refusing to remove anything without --yes (stdin is not a TTY).",
                  file=sys.stderr)
            return 2
        if input("Proceed with uninstall? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1
    execute_plan(plan, run=run)
    print("Uninstall complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
