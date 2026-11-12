from __future__ import annotations

import shlex

from .base import INTERACTIVE_MODE, REPLY_MODE, REPORT_MODE, ChildRound, shell_join, validate_mode

TOOL_TIMEOUT_MARGIN_SEC = 60
REPORT_CONFIG = ('approval_policy="never"', 'sandbox_mode="read-only"',
                 "mcp_servers.dockwright.enabled=false")
INTERACTIVE_CONFIG = (
    'approval_policy="on-request"',
    'approvals_reviewer="auto_review"',
    'sandbox_mode="workspace-write"',
)
SERVICE_TIER_CONFIG = ('service_tier="default"',)
INTERACTIVE_BOOTSTRAP = (
    "You are a dockwright child session running headless in a tmux window. Your `child_sid` is the "
    "value of the environment variable CODEX_THREAD_ID in your shell (run `printenv CODEX_THREAD_ID`). "
    "Ask your parent with the MCP tool `ask_parent(child_sid, question)`; if it returns a "
    "`NO_ANSWER_YET:` sentinel, call it again with the same question plus the `resume_question_id` "
    "it names — never proceed without the answer. When the task is complete, call "
    "`child_done(child_sid, summary)` as your final action."
)
REPORT_BOOTSTRAP = (
    "You are a dockwright child session running headless and read-only. Your final message is your "
    "report: it is written to a file your parent reads. You have no tool to ask questions — record "
    "every assumption in the report and finish."
)


def _refuse_reply(mode: str) -> None:
    validate_mode(mode)
    if mode == REPLY_MODE:
        raise ValueError("mode 'reply' runs only on the claude-exec provider")


class CodexExec:
    name = "codex-exec"
    family = "codex"
    runtime = "codex"
    headless = True

    def _mode_config(self, mode: str) -> tuple[str, ...]:
        _refuse_reply(mode)
        if mode == REPORT_MODE:
            return REPORT_CONFIG
        from .. import config
        wait = config.ask_parent_wait_sec(self.name) or 0
        return (*INTERACTIVE_CONFIG,
                f"mcp_servers.dockwright.tool_timeout_sec={wait + TOOL_TIMEOUT_MARGIN_SEC}")

    def _options(self, child_round: ChildRound) -> list[str]:
        from ..models import resolve_model, resolve_model_args
        from ..spawner import _validate_codex_extra_args
        _validate_codex_extra_args(list(child_round.extra_args))
        args = ["--json", "--skip-git-repo-check", "--dangerously-bypass-hook-trust"]
        args.extend(resolve_model_args(child_round.extra_args))
        for kv in (*self._mode_config(child_round.mode), *SERVICE_TIER_CONFIG):
            args.extend(["-c", kv])
        if child_round.model:
            args.extend(["-m", resolve_model(child_round.model)])
        args.extend(["-o", f"{child_round.report_path}.tmp"])
        return args

    def _pipeline(self, argv: list[str], child_round: ChildRound) -> str:
        tmp = shlex.quote(f"{child_round.report_path}.tmp")
        return (f"{shell_join(argv)} - < {shlex.quote(str(child_round.brief_path))} "
                f"2>&1 | tee {shlex.quote(str(child_round.log_path))}; "
                f"[ -f {tmp} ] && mv {tmp} {shlex.quote(str(child_round.report_path))}")

    def spawn_command(self, child_round: ChildRound) -> str:
        argv = ["codex", "exec", "-C", child_round.cwd, *self._options(child_round)]
        return self._pipeline(argv, child_round)

    def resume_command(self, child_round: ChildRound) -> str:
        if not child_round.resume_sid:
            raise ValueError("codex-exec resume_command needs resume_sid")
        argv = ["codex", "exec", "resume", child_round.resume_sid, *self._options(child_round)]
        return self._pipeline(argv, child_round)

    def bootstrap(self, mode: str) -> str:
        _refuse_reply(mode)
        return INTERACTIVE_BOOTSTRAP if mode == INTERACTIVE_MODE else REPORT_BOOTSTRAP

    def env(self, mode: str) -> dict[str, str]:
        return {}
