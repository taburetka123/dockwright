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


from pathlib import Path

from dockwright import spawner


def _which_factory(available: dict):
    return lambda cmd: available.get(cmd)


def test_interactive_shell_no_zsh_falls_back_to_bash(monkeypatch):
    # Stock Ubuntu: SHELL=/bin/bash, no zsh anywhere (L-1 repro).
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
    # fish can't run the POSIX `K=v cmd` inner command — must not be honored.
    monkeypatch.setenv("SHELL", "/usr/bin/fish")
    monkeypatch.setattr(spawner.shutil, "which",
                        _which_factory({"/usr/bin/fish": "/usr/bin/fish",
                                        "zsh": "/bin/zsh", "bash": "/bin/bash"}))
    assert spawner._interactive_shell() == "/bin/zsh"


def test_interactive_shell_last_resort_sh(monkeypatch):
    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.setattr(spawner.shutil, "which", _which_factory({}))
    assert spawner._interactive_shell() == "sh"


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_no_hardcoded_zsh_spawn_argv_left():
    # L-1 regression net: no python spawn site may hardcode zsh, and the two
    # shipped shell scripts must use the $SPAWN_SHELL shim.
    for py in (REPO_ROOT / "src" / "dockwright").glob("*.py"):
        assert '"zsh", "-ic"' not in py.read_text(), f"hardcoded zsh argv in {py.name}"
    for sh in (REPO_ROOT / "deploy" / "scripts").glob("*.sh"):
        assert "zsh -ic" not in sh.read_text(), f"hardcoded `zsh -ic` in {sh.name}"
