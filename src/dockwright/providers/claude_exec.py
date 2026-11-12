from __future__ import annotations

import json
import shlex

from ..denied_tools import disallowed_tools_arg
from .base import INTERACTIVE_MODE, REPLY_MODE, REPORT_MODE, ChildRound, shell_join, validate_mode
from .codex_exec import REPORT_BOOTSTRAP

REPORT_PRESET = "verifier-settings.json"
INTERACTIVE_PRESET = "worker-headless-settings.json"
REPLY_PRESET = "reply-settings.json"
BIN_PLACEHOLDER = "@@DOCKWRIGHT_BIN@@"
GUARD_COMMAND = " reply-guard"
MATCH_ALL = "*"
REFUSED_FAMILY = "fable"


def _model_problem(model: str) -> str | None:
    from ..models import concrete_family
    family = concrete_family(model)
    if family is None:
        return ("is not a concrete claude-<family>-<version> id. An alias resolves to whatever "
                f"the release calls that tier's latest, and {REFUSED_FAMILY!r} is one of the "
                "aliases, so an alias cannot keep this lane off it")
    if family == REFUSED_FAMILY:
        return f"names the {REFUSED_FAMILY} family"
    return None


def _list_at(mapping, key) -> list:
    value = mapping.get(key) if isinstance(mapping, dict) else None
    return value if isinstance(value, list) else []


def _guard_commands(settings) -> list[str]:
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    commands = [hook.get("command") for entry in _list_at(hooks, "PreToolUse")
                if isinstance(entry, dict) and entry.get("matcher", MATCH_ALL) in (MATCH_ALL, "")
                for hook in _list_at(entry, "hooks") if isinstance(hook, dict)]
    return [command for command in commands if isinstance(command, str) and GUARD_COMMAND in command]


def _reply_guard_problem(preset) -> str | None:
    try:
        settings = json.loads(preset.read_text())
    except OSError:
        return "is missing"
    except ValueError:
        return "is not valid JSON"
    commands = _guard_commands(settings)
    if not commands:
        return f"runs no{GUARD_COMMAND} hook on every tool"
    if any(BIN_PLACEHOLDER in command for command in commands):
        return f"still carries {BIN_PLACEHOLDER}, so its tool guard cannot run"
    return None


class ClaudeExec:
    name = "claude-exec"
    family = "claude"
    runtime = "claude"
    headless = True

    def _argv(self, child_round: ChildRound, resume: bool) -> list[str]:
        from .. import paths
        validate_mode(child_round.mode)
        args = ["claude", "-p"]
        if resume:
            if not child_round.resume_sid:
                raise ValueError("claude-exec resume_command needs resume_sid")
            args.extend(["--resume", child_round.resume_sid])
        elif child_round.session_id:
            args.extend(["--session-id", child_round.session_id])
        from ..models import resolve_model, resolve_model_args, UnknownFamily
        if not child_round.model:
            raise ValueError("claude-exec needs a model: pass model= on the spawn")
        try:
            model = resolve_model(child_round.model)
        except UnknownFamily as err:
            raise ValueError(f"claude-exec refuses --model {child_round.model!r}: {err}")
        problem = _model_problem(model)
        if problem is not None:
            raise ValueError(
                f"claude-exec refuses --model {model!r}: it {problem}. This provider is an "
                "unattended `claude -p`, so a Fable quota that runs out bricks it with nobody "
                "to retype /model. The value came from spawn_child(model=) or "
                "[subworkers.providers.claude-exec] model. Name a concrete non-Fable id there.")
        args.extend(["--output-format", "text", "--model", model])
        if child_round.effort:
            args.extend(["--effort", child_round.effort])
        if "--" in child_round.extra_args:
            raise ValueError("extra_args for claude-exec cannot end the options with '--': "
                             "the preset and --strict-mcp-config would become prompt words")
        args.extend(resolve_model_args(child_round.extra_args))
        preset = paths.PRESETS / {REPORT_MODE: REPORT_PRESET, REPLY_MODE: REPLY_PRESET}.get(
            child_round.mode, INTERACTIVE_PRESET)
        if child_round.mode == REPLY_MODE:
            problem = _reply_guard_problem(preset)
            if problem is not None:
                raise ValueError(f"claude-exec refuses mode 'reply': {preset} {problem}; run setup.sh")
        from .. import roles
        from ..profiles import profile_for
        profile = profile_for(child_round.cwd)
        role_files = roles.launch_files(child_round.context_role, "child", profile=profile)
        settings = preset
        if role_files is not None:
            render = role_files[0]
            if child_round.mode == REPORT_MODE:
                render = roles.codex_file(child_round.context_role, profile=profile) or render
            args.extend(["--append-system-prompt-file", str(render)])
            merged = roles.apply_layer(json.loads(preset.read_text()), roles.read_role_layer(role_files[1]))
            settings = roles.write_spawn_settings(merged)
        args.extend(["--settings", str(settings)])
        args.extend(roles.agents_arg(profile, print_mode=True))
        if child_round.mode == REPORT_MODE:
            args.append("--strict-mcp-config")
        args.append(disallowed_tools_arg())
        return args

    def _pipeline(self, argv: list[str], child_round: ChildRound) -> str:
        report = shlex.quote(str(child_round.report_path))
        tmp = shlex.quote(str(child_round.report_path) + ".tmp")
        return (f"{shell_join(argv)} < {shlex.quote(str(child_round.brief_path))} > {tmp} "
                f"2> {shlex.quote(str(child_round.log_path))} && mv {tmp} {report}")

    def spawn_command(self, child_round: ChildRound) -> str:
        return self._pipeline(self._argv(child_round, resume=False), child_round)

    def resume_command(self, child_round: ChildRound) -> str:
        return self._pipeline(self._argv(child_round, resume=True), child_round)

    def bootstrap(self, mode: str) -> str:
        validate_mode(mode)
        return REPORT_BOOTSTRAP if mode == REPORT_MODE else ""

    def env(self, mode: str) -> dict[str, str]:
        return {}
