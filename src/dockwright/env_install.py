"""Template + idempotently merge dockwright hooks into a settings/hooks JSON file.

The committed settings.snippet.json carries an @@DOCKWRIGHT_BIN@@ placeholder in every
hook command; setup.sh substitutes the absolute venv-binary path at install time so hooks
invoke the orchestrator by explicit path, never via bare-PATH resolution. The merge
converges: an existing hook invoking the same orchestrator subcommand (bare or absolute)
is rewritten to the absolute-path command; a missing one is appended.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import time
from pathlib import Path

PLACEHOLDER = "@@DOCKWRIGHT_BIN@@"
ORCH_SUBCOMMANDS = ("session-start", "user-prompt-submit", "stop", "session-end")
BACKUP_KEEP = 5

_BIN_NAMES = r"(?:dockwright|orchestrator)"  # orchestrator: one-release legacy recognition
_SUBCMD_RE = re.compile(
    r"(?:^|[\s/])" + _BIN_NAMES + r"\s+(" + "|".join(re.escape(s) for s in ORCH_SUBCOMMANDS) + r")\b"
)


def orch_subcommand(command: str) -> str | None:
    """Return the orchestrator subcommand a hook command invokes, else None.

    Matches both bare `orchestrator <sub>` and any `/abs/path/orchestrator <sub>`.
    """
    m = _SUBCMD_RE.search(command or "")
    return m.group(1) if m else None


# Pull the orchestrator binary token out of a rendered canonical command, e.g.
# "... /abs/.venv/bin/dockwright session-start" -> "/abs/.venv/bin/dockwright".
# Greedy \S* backtracks to the LAST '/...dockwright|orchestrator' before a canonical
# subcommand, so a path that itself contains "claude-orchestrator" resolves to the
# trailing binary.
_ORCH_BIN_RE = re.compile(
    r"(\S*" + _BIN_NAMES + r")\s+(?:" + "|".join(re.escape(s) for s in ORCH_SUBCOMMANDS) + r")\b"
)


def rendered_orch_bin(rendered: dict) -> str | None:
    """The orchestrator binary path embedded in the rendered snippet's hook commands, else None.

    The rendered snippet is ground truth for the binary path: render_snippet substitutes a single
    orch_bin into every command. Used to anchor the prune discriminator precisely (vs. matching a
    bare 'orchestrator' token, which would misclassify foreign hooks mentioning the word).
    """
    for blocks in rendered.get("hooks", {}).values():
        for block in blocks:
            for hook in block.get("hooks", []):
                m = _ORCH_BIN_RE.search(hook.get("command", ""))
                if m:
                    return m.group(1)
    return None


def orch_owned_subcommand(command: str, orch_bin: str) -> str | None:
    """Subcommand a hook invokes via the GIVEN orchestrator binary path (any subcommand,
    canonical or stale), else None.

    Matches orch_bin only in command (executable) position — preceded by start, whitespace, or a
    quote — so a foreign hook that merely mentions 'orchestrator' as a path component or argument
    (e.g. `git -C /repos/orchestrator status`) is NOT misclassified as orchestrator-owned. The
    captured subcommand token is unconstrained, so a stale subcommand dropped from the canonical
    set (e.g. manager-tts) is still recognized as orchestrator-owned and thus prunable.
    """
    pat = re.compile(r"(?:^|[\s'\"])" + re.escape(orch_bin) + r"\s+([a-z][a-z0-9-]*)")
    m = pat.search(command or "")
    return m.group(1) if m else None


def render_snippet(snippet: dict, orch_bin: str) -> dict:
    """Deep-copy the snippet with @@DOCKWRIGHT_BIN@@ replaced by orch_bin in every command."""
    rendered = copy.deepcopy(snippet)
    for blocks in rendered.get("hooks", {}).values():
        for block in blocks:
            for hook in block.get("hooks", []):
                if "command" in hook:
                    hook["command"] = hook["command"].replace(PLACEHOLDER, orch_bin)
    return rendered


def merge_hooks(existing: dict, rendered: dict) -> dict:
    """Merge rendered orchestrator hooks into existing settings (converging, idempotent).

    Replaces an existing same-event hook invoking the same orchestrator subcommand with the
    rendered (absolute-path) command; appends if absent. Leaves foreign hooks and all
    non-hooks keys untouched. Finally prunes orchestrator-owned hooks whose subcommand has
    left the canonical set (see prune_orphan_hooks) — that removal step is what makes the
    merge truly converge to the snippet rather than only grow.
    """
    merged = copy.deepcopy(existing)
    merged_hooks = merged.setdefault("hooks", {})
    for event, blocks in rendered.get("hooks", {}).items():
        existing_blocks = merged_hooks.setdefault(event, [])
        for new_block in blocks:
            block_meta = {k: v for k, v in new_block.items() if k != "hooks"}
            for new_hook in new_block.get("hooks", []):
                sub = orch_subcommand(new_hook.get("command", ""))
                replaced = False
                if sub is not None:
                    for b in existing_blocks:
                        for h in b.get("hooks", []):
                            if orch_subcommand(h.get("command", "")) == sub:
                                h["command"] = new_hook["command"]
                                for k in ("type", "timeout"):
                                    if k in new_hook:
                                        h[k] = new_hook[k]
                                replaced = True
                else:
                    # Foreign hook (not an orchestrator subcommand): idempotent by
                    # byte-identical command. Matches command only — a future timeout/type/matcher
                    # change to an already-deployed foreign hook would NOT propagate (fine
                    # for the stable canon-edit-guard.sh command).
                    new_cmd = new_hook.get("command", "")
                    for b in existing_blocks:
                        for h in b.get("hooks", []):
                            if h.get("command", "") == new_cmd:
                                replaced = True
                if not replaced:
                    existing_blocks.append({**copy.deepcopy(block_meta), "hooks": [copy.deepcopy(new_hook)]})
    return prune_orphan_hooks(merged, rendered)


def prune_orphan_hooks(merged: dict, rendered: dict) -> dict:
    """Remove orchestrator-owned hooks whose subcommand is not in the canonical (rendered) set
    for their event. Leaves foreign hooks and canonical orchestrator hooks untouched.

    Per-event: an event's allowed orchestrator subcommands are exactly those the rendered snippet
    defines for it (empty if the event is absent from rendered). Foreign hooks (not invoking the
    orchestrator binary) are always kept; a block keeps its non-hook keys (e.g. matcher). Blocks
    whose hook list empties out and event keys with no blocks left are dropped. No-op when the
    rendered snippet exposes no orchestrator binary path — never over-prunes a degenerate snippet.
    """
    orch_bin = rendered_orch_bin(rendered)
    if orch_bin is None:
        return merged

    allowed: dict[str, set[str]] = {}
    for event, blocks in rendered.get("hooks", {}).items():
        subs: set[str] = set()
        for block in blocks:
            for hook in block.get("hooks", []):
                sub = orch_owned_subcommand(hook.get("command", ""), orch_bin)
                if sub is not None:
                    subs.add(sub)
        allowed[event] = subs

    pruned = copy.deepcopy(merged)
    hooks = pruned.get("hooks", {})
    for event in list(hooks.keys()):
        allowed_subs = allowed.get(event, set())
        new_blocks = []
        for block in hooks[event]:
            kept = [
                h
                for h in block.get("hooks", [])
                if (sub := orch_owned_subcommand(h.get("command", ""), orch_bin)) is None
                or sub in allowed_subs
            ]
            if kept:
                new_blocks.append({**block, "hooks": kept})
        if new_blocks:
            hooks[event] = new_blocks
        else:
            del hooks[event]
    return pruned


def prune_backups(target: Path, keep: int = BACKUP_KEEP) -> None:
    """Delete all but the newest `keep` timestamped backups of `target`.

    Matches only <target-name>.bak.<digits> siblings (this module's own backup
    convention); hand-named backups and the account-farm's .bak-<label>
    variants never match.
    """
    prefix = target.name + ".bak."
    stamped = []
    for sibling in target.parent.glob(prefix + "*"):
        suffix = sibling.name[len(prefix):]
        if suffix.isdigit():
            stamped.append((int(suffix), sibling))
    for _, stale in sorted(stamped)[:-keep]:
        stale.unlink()


def merge_settings_file(target, snippet_path, orch_bin: str, mode: str) -> None:
    """Render + merge the hooks snippet into `target`.

    mode='claude': merge hooks into an existing settings.json, preserving all other keys
      (creates a hooks-only file if absent); never writes mcpServers.
    mode='codex': target holds only {"hooks": {...}}.
    Backs up an existing target to <target>.bak.<epoch_ns> before any mutation; a
    byte-identical no-op merge writes neither file nor backup; only the newest
    BACKUP_KEEP timestamped backups are kept (older ones pruned every run).
    """
    snippet = json.loads(Path(snippet_path).read_text())
    snippet.pop("mcpServers", None)
    snippet.pop("_note", None)
    rendered = render_snippet(snippet, orch_bin)

    target = Path(target)
    current: str | None = None
    if target.exists():
        current = target.read_text()
        existing = json.loads(current)
    else:
        existing = {}

    if mode == "codex":
        base = {"hooks": existing.get("hooks", {})}
        out = {"hooks": merge_hooks(base, rendered).get("hooks", {})}
    else:
        out = merge_hooks(existing, rendered)

    new_content = json.dumps(out, indent=2) + "\n"
    if current != new_content:
        if current is not None:
            target.with_name(target.name + f".bak.{time.time_ns()}").write_text(current)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_content)
    prune_backups(target)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Render + merge dockwright hooks into a settings file.")
    p.add_argument("--target", type=Path, required=True)
    p.add_argument("--snippet", type=Path, required=True)
    p.add_argument("--orch-bin", required=True)
    p.add_argument("--mode", choices=("claude", "codex"), required=True)
    args = p.parse_args(argv)
    merge_settings_file(args.target, args.snippet, args.orch_bin, args.mode)
    print(f"Merged dockwright hooks into {args.target} (mode={args.mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
