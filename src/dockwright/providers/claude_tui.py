from __future__ import annotations

from .base import ChildRound


class ClaudeTui:
    name = "claude-tui"
    family = "claude"
    runtime = "claude"
    headless = False

    def spawn_command(self, child_round: ChildRound) -> str:
        from ..spawner import _runtime_command
        return _runtime_command("claude", child_round.brief_path.read_text(), list(child_round.extra_args), None)

    def resume_command(self, child_round: ChildRound) -> str:
        from ..spawner import _runtime_command
        return _runtime_command("claude", "", list(child_round.extra_args), child_round.resume_sid)

    def bootstrap(self, mode: str) -> str:
        return ""

    def env(self, mode: str) -> dict[str, str]:
        return {}
