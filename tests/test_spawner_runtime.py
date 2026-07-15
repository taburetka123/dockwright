import shlex

from dockwright.spawner import _runtime_command


def _tokens(cmd):
    return shlex.split(cmd)


def test_claude_no_model_injects_opus_1m():
    tokens = _tokens(_runtime_command("claude", "do the task", extra_args=[]))
    assert tokens.count("--model") == 1
    assert tokens[tokens.index("--model") + 1] == "opus[1m]"


def test_claude_explicit_model_space_form_is_respected():
    tokens = _tokens(
        _runtime_command("claude", "do the task", extra_args=["--model", "sonnet"])
    )
    assert tokens.count("--model") == 1
    assert tokens[tokens.index("--model") + 1] == "sonnet"
    assert "opus[1m]" not in tokens


def test_claude_explicit_model_equals_form_is_respected():
    tokens = _tokens(
        _runtime_command("claude", "do the task", extra_args=["--model=claude-fable-5"])
    )
    assert "--model=claude-fable-5" in tokens
    assert "--model" not in tokens
    assert "opus[1m]" not in tokens


def test_claude_fallback_model_does_not_suppress_default_opus():
    tokens = _tokens(
        _runtime_command("claude", "do the task", extra_args=["--fallback-model", "sonnet"])
    )
    assert tokens.count("--model") == 1
    assert tokens[tokens.index("--model") + 1] == "opus[1m]"
    assert "--fallback-model" in tokens
    assert tokens[tokens.index("--fallback-model") + 1] == "sonnet"


def test_injected_model_precedes_resume_flag():
    tokens = _tokens(_runtime_command("claude", "", extra_args=[], resume_sid="sid-123"))
    assert "--model" in tokens and "--resume" in tokens
    assert tokens.index("--model") < tokens.index("--resume")


def test_injected_model_precedes_prompt():
    tokens = _tokens(_runtime_command("claude", "do the task", extra_args=[]))
    assert tokens.index("--model") < tokens.index("do the task")


def test_codex_spawn_unaffected_by_model_default():
    tokens = _tokens(_runtime_command("codex", "do the task", extra_args=[]))
    assert tokens[0] == "codex"
    assert "opus[1m]" not in tokens


def test_explicit_opus_1m_is_not_double_injected():
    tokens = _tokens(
        _runtime_command("claude", "do the task", extra_args=["--model", "opus[1m]"])
    )
    assert tokens.count("--model") == 1
    assert tokens[tokens.index("--model") + 1] == "opus[1m]"


def test_model_flag_detected_mid_extra_args():
    tokens = _tokens(
        _runtime_command(
            "claude", "do the task",
            extra_args=["--verbose", "--model", "sonnet", "--debug"],
        )
    )
    assert tokens.count("--model") == 1
    assert tokens[tokens.index("--model") + 1] == "sonnet"
    assert "opus[1m]" not in tokens
