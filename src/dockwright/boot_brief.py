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
