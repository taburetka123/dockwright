"""`dockwright boot-brief` — expansion-free manager boot loader.

Replaces the boot docs' inline memory-loader bash (E2E F-2): that one-liner
carried $-expansions, which the permission system's expansion guard can never
allowlist, so every manager boot ate an approval prompt on it. A plain
`dockwright boot-brief --domain <d>` is prefix-allowlistable. Prints pointers
only (the model Reads each path — Read paginates, nothing is dropped):

    AGENT_LINES <n>
    MEMORY <path>          (≤5, newest-first, mtime within 7 days)
    NOTEBOOK <path> (<n> bytes)
    NOTEBOOK_WARN [...]    (only when the notebook exceeds 4 KB)

Exit 0 on every DATA condition — missing/empty stores, unreadable files — so a
boot loader never fails the boot over absent state. (argparse still SystemExits(2)
on unrecognized args, standard CLI behavior; that's a caller bug, not a data one.)
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from . import paths

MEMORY_WINDOW_DAYS = 7
MEMORY_CAP = 5
NOTEBOOK_WARN_BYTES = 4096


def _agent_file() -> Path:
    return Path.home() / ".claude" / "agents" / "manager.md"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="dockwright boot-brief",
        description="Print manager boot pointers (agent-file line count, "
                    "recent memory paths, notebook pointer).")
    parser.add_argument("--domain", default="general")
    args = parser.parse_args(argv)

    agent = _agent_file()
    if agent.is_file():
        try:
            with agent.open() as f:
                print(f"AGENT_LINES {sum(1 for _ in f)}")
        except OSError:
            pass

    now = time.time()
    # Resolve the memory root through the config-honoring helper the WRITER uses
    # (distill._write_memory_file_atomic → paths.manager_memory_domain_dir), so an
    # operator with a custom `[paths] manager_memory` still gets MEMORY pointers
    # instead of an empty read off the state root.
    mem_dir = paths.manager_memory_domain_dir(args.domain)
    entries: list[tuple[float, Path]] = []
    if mem_dir.is_dir():
        for f in mem_dir.glob("*.md"):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if now - mtime <= MEMORY_WINDOW_DAYS * 86400:
                entries.append((mtime, f))
    for _, f in sorted(entries, key=lambda e: e[0], reverse=True)[:MEMORY_CAP]:
        print(f"MEMORY {f}")

    nb = paths.ROOT / "notebook" / f"{args.domain}.md"
    if nb.is_file():
        try:
            size = nb.stat().st_size
        except OSError:
            size = 0
        if size > 0:
            print(f"NOTEBOOK {nb} ({size} bytes)")
            if size > NOTEBOOK_WARN_BYTES:
                print(f"NOTEBOOK_WARN [notebook >4KB ({size} bytes) — archive "
                      f"resolved entries to notebook/archive/]")
    return 0
