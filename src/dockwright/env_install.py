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

_BIN_NAMES = r"(?:dockwright|orchestrator)"
_SUBCMD_RE = re.compile(
    r"(?:^|[\s/])" + _BIN_NAMES + r"\s+(" + "|".join(re.escape(s) for s in ORCH_SUBCOMMANDS) + r")\b"
)


def orch_subcommand(command: str) -> str | None:
    m = _SUBCMD_RE.search(command or "")
    return m.group(1) if m else None


_ORCH_BIN_RE = re.compile(
    r"(\S*" + _BIN_NAMES + r")\s+(?:" + "|".join(re.escape(s) for s in ORCH_SUBCOMMANDS) + r")\b"
)


def rendered_orch_bin(rendered: dict) -> str | None:
    for blocks in rendered.get("hooks", {}).values():
        for block in blocks:
            for hook in block.get("hooks", []):
                m = _ORCH_BIN_RE.search(hook.get("command", ""))
                if m:
                    return m.group(1)
    return None


def orch_owned_subcommand(command: str, orch_bin: str) -> str | None:
    pat = re.compile(r"(?:^|[\s'\"])" + re.escape(orch_bin) + r"\s+([a-z][a-z0-9-]*)")
    m = pat.search(command or "")
    return m.group(1) if m else None


def render_snippet(snippet: dict, orch_bin: str) -> dict:
    rendered = copy.deepcopy(snippet)
    for blocks in rendered.get("hooks", {}).values():
        for block in blocks:
            for hook in block.get("hooks", []):
                if "command" in hook:
                    hook["command"] = hook["command"].replace(PLACEHOLDER, orch_bin)
    return rendered


def merge_hooks(existing: dict, rendered: dict) -> dict:
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
                    new_cmd = new_hook.get("command", "")
                    for b in existing_blocks:
                        for h in b.get("hooks", []):
                            if h.get("command", "") == new_cmd:
                                replaced = True
                if not replaced:
                    existing_blocks.append({**copy.deepcopy(block_meta), "hooks": [copy.deepcopy(new_hook)]})
    return prune_orphan_hooks(merged, rendered)


def prune_orphan_hooks(merged: dict, rendered: dict) -> dict:
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
    prefix = target.name + ".bak."
    stamped = []
    for sibling in target.parent.glob(prefix + "*"):
        suffix = sibling.name[len(prefix):]
        if suffix.isdigit():
            stamped.append((int(suffix), sibling))
    for _, stale in sorted(stamped)[:-keep]:
        stale.unlink()


def merge_settings_file(target, snippet_path, orch_bin: str, mode: str) -> None:
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
