from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

AGENTS_FILE = "--agents-file"
LINUX_ARG_STRLEN_MAX = 131072


AGENTS = "--agents"
PRINT_FLAGS = ("-p", "--print")


def _inline(value: str, print_mode: bool) -> str:
    if print_mode or not Path(value).is_file():
        return value
    return Path(value).read_text(encoding="utf-8")


def expand(argv: list[str]) -> list[str]:
    print_mode = any(a in PRINT_FLAGS for a in argv)
    out: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == AGENTS_FILE and i + 1 < len(argv):
            out += [AGENTS, Path(argv[i + 1]).read_text(encoding="utf-8")]
            i += 2
            continue
        if arg.startswith(AGENTS_FILE + "="):
            out += [AGENTS, Path(arg.split("=", 1)[1]).read_text(encoding="utf-8")]
            i += 1
            continue
        if arg == AGENTS and i + 1 < len(argv):
            out += [AGENTS, _inline(argv[i + 1], print_mode)]
            i += 2
            continue
        if arg.startswith(AGENTS + "="):
            out += [AGENTS, _inline(arg.split("=", 1)[1], print_mode)]
            i += 1
            continue
        out.append(arg)
        i += 1
    return out


def _check_arg_lengths(args: list[str]) -> None:
    if not sys.platform.startswith("linux"):
        return
    for arg in args:
        if len(arg.encode()) >= LINUX_ARG_STRLEN_MAX:
            raise SystemExit(f"dockwright launch: an argument is {len(arg.encode())} B; Linux passes at most "
                             f"{LINUX_ARG_STRLEN_MAX} B per argument, so the agent definitions cannot go inline here")


def claude_bin() -> str:
    pinned = os.environ.get("DOCKWRIGHT_CLAUDE_BIN", "")
    if pinned:
        return pinned
    found = shutil.which("claude")
    if not found:
        raise SystemExit("dockwright launch: no `claude` on PATH and DOCKWRIGHT_CLAUDE_BIN is unset")
    return found


def main(argv: list[str] | None = None) -> int:
    args = expand(sys.argv[2:] if argv is None else argv)
    _check_arg_lengths(args)
    os.execvp(claude_bin(), ["claude", *args])
    return 0
