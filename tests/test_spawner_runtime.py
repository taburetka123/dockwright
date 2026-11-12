import shlex

import pytest

from dockwright import models, paths, spawner
from dockwright.spawner import _runtime_command

DEFAULT_MODEL = models.families()["opus"] + "[1m]"
FLAGS = ["--model", "@opus", "--effort", "xhigh"]


def _tokens(cmd):
    return shlex.split(cmd)


def test_claude_explicit_model_space_form_is_respected():
    tokens = _tokens(
        _runtime_command("claude", "do the task", extra_args=["--model", "sonnet", "--effort", "xhigh"])
    )
    assert tokens.count("--model") == 1
    assert tokens[tokens.index("--model") + 1] == "sonnet"
    assert DEFAULT_MODEL not in tokens


def test_claude_explicit_model_equals_form_is_respected():
    tokens = _tokens(
        _runtime_command("claude", "do the task", extra_args=["--model=claude-sonnet-5", "--effort=xhigh"])
    )
    assert "--model=claude-sonnet-5" in tokens
    assert "--model" not in tokens
    assert DEFAULT_MODEL not in tokens


@pytest.mark.parametrize("extra,missing", [
    (["--effort", "xhigh"], "--model"),
    (["--model", "@opus"], "--effort"),
    ([], "--model / --effort"),
])
def test_a_claude_worker_launch_without_model_or_effort_is_refused(extra, missing):
    with pytest.raises(ValueError, match="claude worker launch without --model / --effort: pass both in extra_args"):
        _runtime_command("claude", "do the task", extra_args=extra)


def test_short_m_and_equals_forms_count_as_the_model():
    for extra in (["-m", "@opus", "--effort", "xhigh"], ["--model=@opus", "--effort=xhigh"]):
        tokens = _tokens(_runtime_command("claude", "do", extra_args=extra))
        assert tokens.count("--model") + tokens.count("-m") + sum(t.startswith("--model=") for t in tokens) == 1


def test_a_manager_launch_passes_without_the_flags():
    tokens = _tokens(_runtime_command("claude", "", extra_args=[], agent="manager"))
    assert "--model" not in tokens


def test_a_resume_without_the_flags_is_refused_too():
    with pytest.raises(ValueError, match="without --model / --effort"):
        _runtime_command("claude", "", extra_args=[], resume_sid="sid-1")


def test_codex_spawn_unaffected_by_model_default():
    tokens = _tokens(_runtime_command("codex", "do the task", extra_args=[]))
    assert tokens[0] == "codex"
    assert DEFAULT_MODEL not in tokens


def test_explicit_default_model_is_not_double_injected():
    tokens = _tokens(
        _runtime_command("claude", "do the task", extra_args=["--model", DEFAULT_MODEL, "--effort", "xhigh"])
    )
    assert tokens.count("--model") == 1
    assert tokens[tokens.index("--model") + 1] == DEFAULT_MODEL


def test_model_flag_detected_mid_extra_args():
    tokens = _tokens(
        _runtime_command(
            "claude", "do the task",
            extra_args=["--verbose", "--model", "sonnet", "--debug", "--effort", "xhigh"],
        )
    )
    assert tokens.count("--model") == 1
    assert tokens[tokens.index("--model") + 1] == "sonnet"
    assert DEFAULT_MODEL not in tokens


from dockwright import spawner


def _which_factory(available: dict):
    return lambda cmd: available.get(cmd)


def test_interactive_shell_no_zsh_falls_back_to_bash(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(spawner.shutil, "which",
                        _which_factory({"/bin/bash": "/bin/bash", "bash": "/bin/bash"}))
    assert spawner._interactive_shell() == "/bin/bash"


def test_interactive_shell_prefers_zsh_when_no_shell_env(monkeypatch):
    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.setattr(spawner.shutil, "which",
                        _which_factory({"zsh": "/bin/zsh", "bash": "/bin/bash"}))
    assert spawner._interactive_shell() == "/bin/zsh"


def test_interactive_shell_ignores_non_posix_shell_env(monkeypatch):
    monkeypatch.setenv("SHELL", "/usr/bin/fish")
    monkeypatch.setattr(spawner.shutil, "which",
                        _which_factory({"/usr/bin/fish": "/usr/bin/fish",
                                        "zsh": "/bin/zsh", "bash": "/bin/bash"}))
    assert spawner._interactive_shell() == "/bin/zsh"


def test_interactive_shell_last_resort_sh(monkeypatch):
    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.setattr(spawner.shutil, "which", _which_factory({}))
    assert spawner._interactive_shell() == "sh"


from dockwright import paths


def _install_worker_agent(tmp_path, monkeypatch, text):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "worker.md").write_text(text)
    monkeypatch.setattr(paths, "AGENTS_DIR", agents)
    return agents / "worker.md"


WORKER_AGENT_TEXT = (
    "---\nname: worker\ndescription: A worker.\n---\n\n"
    "# Worker Agent\n\nYou are running in **worker** mode. Don't ask; `ask_manager`.\n"
)


def test_claude_worker_appends_agent_file(tmp_path, monkeypatch):
    agent = _install_worker_agent(tmp_path, monkeypatch, WORKER_AGENT_TEXT)
    tokens = _tokens(_runtime_command("claude", "do the task", extra_args=FLAGS))
    assert tokens.count("--append-system-prompt-file") == 1
    assert tokens[tokens.index("--append-system-prompt-file") + 1] == str(agent)
    assert tokens[-1].startswith("$(cat ") and tokens.index("--append-system-prompt-file") < len(tokens) - 1
    assert "Worker Agent" not in _runtime_command("claude", "do the task", extra_args=FLAGS)
    assert [f.read_text() for f in paths.PROMPT_FILES.glob("*.txt")] == ["do the task"]


def test_claude_worker_resume_appends_agent_file(tmp_path, monkeypatch):
    agent = _install_worker_agent(tmp_path, monkeypatch, WORKER_AGENT_TEXT)
    tokens = _tokens(_runtime_command("claude", "", extra_args=FLAGS, resume_sid="sid-123"))
    assert tokens[tokens.index("--append-system-prompt-file") + 1] == str(agent)
    assert "--resume" in tokens


def test_agent_file_omitted_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "AGENTS_DIR", tmp_path / "no-agents")
    tokens = _tokens(_runtime_command("claude", "do the task", extra_args=FLAGS))
    assert "--append-system-prompt-file" not in tokens


def test_agent_file_omitted_when_frontmatter_only(tmp_path, monkeypatch):
    _install_worker_agent(tmp_path, monkeypatch, "---\nname: worker\n---\n\n   \n")
    tokens = _tokens(_runtime_command("claude", "do the task", extra_args=FLAGS))
    assert "--append-system-prompt-file" not in tokens


def test_caller_append_system_prompt_suppresses_agent_file(tmp_path, monkeypatch):
    _install_worker_agent(tmp_path, monkeypatch, WORKER_AGENT_TEXT)
    tokens = _tokens(_runtime_command(
        "claude", "do the task", extra_args=[*FLAGS, "--append-system-prompt", "mine"]))
    assert "--append-system-prompt-file" not in tokens
    assert tokens[tokens.index("--append-system-prompt") + 1] == "mine"


def test_caller_system_prompt_suppresses_agent_file(tmp_path, monkeypatch):
    _install_worker_agent(tmp_path, monkeypatch, WORKER_AGENT_TEXT)
    tokens = _tokens(_runtime_command(
        "claude", "do the task", extra_args=[*FLAGS, "--system-prompt=mine"]))
    assert "--append-system-prompt-file" not in tokens


def test_manager_launch_gets_no_worker_agent_file(tmp_path, monkeypatch):
    _install_worker_agent(tmp_path, monkeypatch, WORKER_AGENT_TEXT)
    tokens = _tokens(_runtime_command("claude", "boot", extra_args=[], agent="manager"))
    assert "--append-system-prompt-file" not in tokens


def test_codex_worker_gets_no_agent_file(tmp_path, monkeypatch):
    _install_worker_agent(tmp_path, monkeypatch, WORKER_AGENT_TEXT)
    tokens = _tokens(_runtime_command("codex", "do the task", extra_args=[]))
    assert "--append-system-prompt-file" not in tokens


@pytest.mark.parametrize("supplied,expected", [
    (["--model", "@opus[1m]"], models.families()["opus"] + "[1m]"),
    (["--model=@sonnet"], models.families()["sonnet"]),
])
def test_a_caller_supplied_token_is_resolved_before_the_command_is_built(supplied, expected):
    cmd = _runtime_command("claude", "hi", extra_args=[*supplied, "--effort", "xhigh"])
    assert "@" not in cmd, f"a family token reached the command line: {cmd}"
    tokens = _tokens(cmd)
    assert tokens.count("--model") + sum(t.startswith("--model=") for t in tokens) == 1
    assert any(t in (expected, f"--model={expected}") for t in tokens)


def test_codex_worker_prompt_puts_the_role_text_between_bootstrap_and_task():
    prompt = spawner._codex_worker_prompt("do X", "ROLE TEXT")
    assert prompt.index(spawner.CODEX_WORKER_BOOTSTRAP_PROMPT) < prompt.index("ROLE TEXT") < prompt.index("Task:\ndo X")
    assert spawner._codex_worker_prompt("do X") == f"{spawner.CODEX_WORKER_BOOTSTRAP_PROMPT}\n\nTask:\ndo X"
    assert spawner._codex_worker_prompt("", "  ") == spawner.CODEX_WORKER_BOOTSTRAP_PROMPT


def test_a_codex_prompt_with_role_text_rides_a_prompt_file_not_the_command_line():
    role_text = "R" * 40_000
    command = _runtime_command("codex", "do X", [], role_text=role_text)
    assert len(command) < 2_000 and '"$(cat ' in command
    prompt_file = next(paths.PROMPT_FILES.glob("*.txt"))
    assert str(prompt_file) in command
    assert prompt_file.read_text() == spawner._codex_worker_prompt("do X", role_text)
    assert shlex.split(command)[-1] == f"$(cat {prompt_file})"


def test_a_codex_prompt_with_role_text_and_no_task_still_carries_the_role():
    command = _runtime_command("codex", "", [], role_text="ROLE")
    prompt_file = next(paths.PROMPT_FILES.glob("*.txt"))
    assert str(prompt_file) in command
    assert prompt_file.read_text() == spawner._codex_worker_prompt("", "ROLE")


def test_a_codex_prompt_without_role_text_stays_on_the_command_line():
    command = _runtime_command("codex", "do X", [])
    assert "$(cat" not in command and "Task:" in command
    assert not paths.PROMPT_FILES.exists()


def test_the_prompt_file_expands_to_the_full_prompt_in_a_shell(tmp_path):
    import subprocess
    role_text = "line one\n'quoted' \"double\" $HOME `tick`\n" * 200
    command = _runtime_command("codex", "do X", [], role_text=role_text)
    prompt_arg = command[command.index('"$(cat '):]
    out = subprocess.run(["sh", "-c", f"printf '%s' {prompt_arg}"], capture_output=True, text=True, check=True)
    assert out.stdout == spawner._codex_worker_prompt("do X", role_text).rstrip("\n")


def test_a_codex_prompt_over_the_linux_argument_cap_is_refused_on_linux(monkeypatch):
    monkeypatch.setattr(spawner.sys, "platform", "linux")
    with pytest.raises(ValueError, match="Linux passes at most 131071 B in one argument"):
        _runtime_command("codex", "do X", [], role_text="R" * 140_000)
    assert not paths.PROMPT_FILES.exists()
    assert '"$(cat ' in _runtime_command("codex", "do X", [], role_text="R" * 100_000)


def test_a_large_codex_prompt_is_not_refused_off_linux(monkeypatch):
    monkeypatch.setattr(spawner.sys, "platform", "darwin")
    assert '"$(cat ' in _runtime_command("codex", "do X", [], role_text="R" * 300_000)


def test_a_claude_prompt_rides_a_prompt_file_not_the_command_line():
    prompt = "P" * 100_000
    command = _runtime_command("claude", prompt, extra_args=FLAGS)
    assert len(command) < 2_000
    prompt_file = next(paths.PROMPT_FILES.glob("*.txt"))
    assert prompt_file.read_text() == prompt
    assert shlex.split(command)[-1] == f"$(cat {prompt_file})"


@pytest.mark.real_tmux
def test_a_claude_prompt_far_past_the_tmux_limit_reaches_claude_whole(real_tmux, tmp_path):
    import asyncio
    import time

    from dockwright import terminal

    received = tmp_path / "received.txt"
    stub = tmp_path / "bin" / "claude"
    stub.parent.mkdir()
    stub.write_text("#!/bin/sh\n"
                    'for a; do last="$a"; done\n'
                    f'printf %s "$last" > {shlex.quote(str(received))}.tmp && '
                    f"mv {shlex.quote(str(received))}.tmp {shlex.quote(str(received))}\n")
    stub.chmod(0o755)
    prompt = ("brief line with 'single' \"double\" $HOME `tick` \\ back\n" * 2_000).rstrip("\n")
    command = _runtime_command("claude", prompt, extra_args=["--model", "m", "--effort", "xhigh"])
    argv = ["/bin/sh", "-c", f"PATH={shlex.quote(str(stub.parent))}:$PATH; {command}"]
    pane = asyncio.run(terminal.get_driver().spawn(cwd=str(tmp_path), title="big-prompt", argv=argv,
                                                   route_to_workers_window=True))
    assert pane
    deadline = time.time() + 15
    while not received.exists() and time.time() < deadline:
        time.sleep(0.1)
    assert received.read_text() == prompt


WORKER_DENY = "--disallowedTools=Artifact,SendFeedback,Workflow,AskUserQuestion,ReportFindings,ScheduleWakeup"


def test_claude_command_of_another_agent_kind_denies_the_six_tools_before_the_prompt():
    tokens = _tokens(_runtime_command("claude", "do the task", extra_args=[], agent="lane"))
    assert tokens.count(WORKER_DENY) == 1
    assert tokens.index(WORKER_DENY) < len(tokens) - 1
    assert tokens[-1].startswith("$(cat ")


def test_claude_manager_command_carries_no_worker_deny():
    assert "--disallowedTools" not in _runtime_command("claude", "boot", extra_args=[], agent="manager")


def test_codex_command_carries_no_deny():
    assert "disallowedTools" not in _runtime_command("codex", "do the task", extra_args=[])
