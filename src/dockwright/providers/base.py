from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

REPORT_MODE = "report"
INTERACTIVE_MODE = "interactive"
REPLY_MODE = "reply"
MODES = (REPORT_MODE, INTERACTIVE_MODE, REPLY_MODE)
LEG_MODES = (REPORT_MODE, INTERACTIVE_MODE)


@dataclass(frozen=True)
class ChildRound:
    mode: str
    cwd: str
    brief_path: Path
    report_path: Path
    log_path: Path
    model: str | None = None
    effort: str | None = None
    session_id: str | None = None
    resume_sid: str | None = None
    extra_args: tuple[str, ...] = ()
    context_role: str | None = None


@runtime_checkable
class ChildProvider(Protocol):
    name: str
    family: str
    runtime: str
    headless: bool

    def spawn_command(self, child_round: ChildRound) -> str: ...
    def resume_command(self, child_round: ChildRound) -> str: ...
    def bootstrap(self, mode: str) -> str: ...
    def env(self, mode: str) -> dict[str, str]: ...


def shell_join(args) -> str:
    return " ".join(shlex.quote(str(a)) for a in args)


def validate_mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {list(MODES)}, got {mode!r}")
    return mode
