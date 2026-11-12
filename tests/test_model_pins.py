import shlex
import subprocess

import pytest

from dockwright import config, models, spawner
from dockwright.providers import get_provider
from dockwright.providers.base import REPORT_MODE, ChildRound

OPUS_MOVE = ('opus = "claude-opus-7-1"', 'opus = "claude-opus-9-9"')


def _cfg(tmp_path, monkeypatch, text):
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(text)
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))


def _model_in(cmd: str) -> str:
    tokens = shlex.split(cmd.split("<")[0])
    return tokens[tokens.index("--model") + 1]


def test_a_version_move_on_disk_reaches_resolve_model_without_a_reload(model_pins):
    assert models.resolve_model("@opus[1m]") == "claude-opus-7-1[1m]"
    model_pins(*OPUS_MOVE)
    assert models.resolve_model("@opus[1m]") == "claude-opus-9-9[1m]"
    assert models.resolve_model("@opus") == "claude-opus-9-9"


def test_a_version_move_reaches_the_spawn_defaults(model_pins):
    assert config.manager_model() == "claude-opus-7-1[1m]"
    model_pins(*OPUS_MOVE)
    assert config.manager_model() == "claude-opus-9-9[1m]"


def test_a_role_move_in_defaults_reaches_an_install_with_no_spawn_keys(model_pins):
    model_pins('manager_model = "@opus[1m]"', 'manager_model = "@sonnet"')
    model_pins('distill_model = "claude-sonnet-4-6"', 'distill_model = "claude-sonnet-7"')
    assert config.manager_model() == "claude-sonnet-7"
    assert config.distill_model() == "claude-sonnet-7"


def test_a_spawn_key_still_wins_and_survives_a_missing_pins_file(model_pins, tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch, '[spawn]\nmanager_model = "claude-opus-5[1m]"\n')
    model_pins.path.unlink()
    assert config.manager_model() == "claude-opus-5[1m]"


def test_a_worker_spawn_command_reads_the_move(model_pins):
    flags = ["--model", "@opus[1m]", "--effort", "xhigh"]
    assert _model_in(spawner._runtime_command("claude", "hi", flags, None)) == "claude-opus-7-1[1m]"
    model_pins(*OPUS_MOVE)
    assert _model_in(spawner._runtime_command("claude", "hi", flags, None)) == "claude-opus-9-9[1m]"
    token = spawner._runtime_command("claude", "hi", ["--model", "@opus", "--effort", "xhigh"], None)
    assert _model_in(token) == "claude-opus-9-9"


def test_a_claude_exec_child_reads_the_move(model_pins, tmp_path):
    exec_provider = get_provider("claude-exec")
    child = ChildRound(mode=REPORT_MODE, cwd="/tmp/x", brief_path=tmp_path / "r1.brief.md",
                       report_path=tmp_path / "r1.report.md", log_path=tmp_path / "r1.log",
                       session_id="11111111-2222-4333-8444-555555555555", model="@opus", effort="high")
    assert _model_in(exec_provider.spawn_command(child)) == "claude-opus-7-1"
    model_pins(*OPUS_MOVE)
    assert _model_in(exec_provider.spawn_command(child)) == "claude-opus-9-9"


def test_the_distill_argv_reads_a_moved_distill_default(model_pins, tmp_path, monkeypatch):
    from dockwright import distill
    log = tmp_path / "t.jsonl"
    log.write_text('{"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}\n')
    monkeypatch.setattr(distill, "find_session_log", lambda sid: log)
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv[argv.index("--model") + 1])
        return subprocess.CompletedProcess(argv, 0, stdout=b"distilled", stderr=b"")

    monkeypatch.setattr(distill.subprocess, "run", fake_run)
    distill._distill_manager_session("sid-pins")
    model_pins('distill_model = "claude-sonnet-4-6"', 'distill_model = "claude-sonnet-7"')
    distill._distill_manager_session("sid-pins")
    assert seen == ["claude-sonnet-4-6", "claude-sonnet-7"]


def test_model_manager_cli_prints_the_manager_launch_model(model_pins, capsys):
    assert models.model_main(["manager"]) == 0
    assert capsys.readouterr().out.strip() == "claude-opus-7-1[1m]"
    model_pins(*OPUS_MOVE)
    assert models.model_main(["manager"]) == 0
    assert capsys.readouterr().out.strip() == "claude-opus-9-9[1m]"


def test_model_manager_cli_prints_a_configured_manager_model(model_pins, tmp_path, monkeypatch, capsys):
    _cfg(tmp_path, monkeypatch, '[spawn]\nmanager_model = "@sonnet"\n')
    assert models.model_main(["manager"]) == 0
    assert capsys.readouterr().out.strip() == "claude-sonnet-7"


@pytest.mark.parametrize("running,specced,same", [
    ("claude-opus-7-1", "claude-opus-7-1[1m]", True),
    ("claude-opus-7-1[1m]", "claude-opus-7-1[1m]", True),
    ("claude-opus-7-1[1m]", "claude-opus-7-1", True),
    (" CLAUDE-OPUS-7-1 ", "claude-opus-7-1[1m]", True),
    ("claude-fable-7-1", "claude-opus-7-1[1m]", False),
    ("claude-opus-7", "claude-opus-7-1[1m]", False),
    ("claude-sonnet-4-6", "claude-sonnet-4-6[1m]", False),
    ("claude-sonnet-4-6[1m]", "claude-sonnet-4-6[1m]", True),
    ("claude-sonnet-4-6-20260101", "claude-sonnet-4-6-20260101[1m]", False),
])
def test_same_model_ignores_1m_only_off_the_no_native_list(model_pins, running, specced, same):
    assert models.same_model(running, specced) is same


def test_model_manager_cli_compares_a_running_id(model_pins, capsys):
    assert models.model_main(["manager", "claude-opus-7-1"]) == 0
    assert capsys.readouterr().out.strip() == "same claude-opus-7-1[1m]"
    assert models.model_main(["manager", "claude-fable-7-1"]) == 0
    assert capsys.readouterr().out.strip() == "differs claude-opus-7-1[1m]"


def test_model_manager_cli_flags_a_200k_window_on_a_listed_model(model_pins, tmp_path, monkeypatch, capsys):
    _cfg(tmp_path, monkeypatch, '[spawn]\nmanager_model = "claude-sonnet-4-6[1m]"\n')
    assert models.model_main(["manager", "claude-sonnet-4-6"]) == 0
    assert capsys.readouterr().out.strip() == "differs claude-sonnet-4-6[1m]"
    assert models.model_main(["manager", "claude-sonnet-4-6[1m]"]) == 0
    assert capsys.readouterr().out.strip() == "same claude-sonnet-4-6[1m]"


def test_model_manager_cli_refuses_a_blank_running_id(model_pins, capsys):
    for blank in ("", "  "):
        assert models.model_main(["manager", blank]) == 2
        assert capsys.readouterr().out == ""


def test_model_manager_cli_reads_a_flag_as_usage(model_pins, capsys):
    for flag in ("--help", "-h"):
        assert models.model_main(["manager", flag]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "dockwright model manager" in captured.err


def test_a_malformed_no_native_list_fails_loudly(model_pins, capsys):
    model_pins('no_native_1m = ["claude-sonnet-4-6"]', 'no_native_1m = "claude-sonnet-4-6"')
    assert models.model_main(["manager", "claude-opus-7-1"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "models.toml" in captured.err


def test_a_pins_file_without_a_context_table_treats_every_1m_as_native(model_pins):
    model_pins('\n[context]\nno_native_1m = ["claude-sonnet-4-6"]\n', '\n')
    assert models.same_model("claude-sonnet-4-6", "claude-sonnet-4-6[1m]") is True


def test_a_missing_pins_file_fails_every_token_loudly(model_pins, capsys):
    model_pins.path.unlink()
    with pytest.raises(models.ModelPinsUnreadable, match="models.toml"):
        models.resolve_model("@opus")
    assert models.resolve_model("claude-opus-5-5") == "claude-opus-5-5"
    for argv in (["resolve", "@opus[1m]"], ["manager"], ["manager", "claude-opus-7-1"]):
        assert models.model_main(argv) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "models.toml" in captured.err


def test_a_malformed_pins_file_fails_loudly(model_pins):
    model_pins.path.write_text("[families\nopus = ")
    with pytest.raises(models.ModelPinsUnreadable, match="models.toml"):
        models.resolve_model("@opus")


def test_a_pins_file_missing_a_default_fails_loudly(model_pins):
    model_pins('manager_model = "@opus[1m]"\n', "")
    with pytest.raises(models.ModelPinsUnreadable, match="manager_model"):
        config.manager_model()


def test_a_defaults_entry_that_is_not_a_table_fails_loudly(model_pins):
    model_pins.path.write_text('defaults = "@opus[1m]"\n[families]\nopus = "claude-opus-7-1"\n')
    with pytest.raises(models.ModelPinsUnreadable) as err:
        config.manager_model()
    assert str(model_pins.path) in str(err.value) and "[defaults]" in str(err.value)


def test_a_family_pin_that_is_not_a_string_fails_loudly(model_pins):
    model_pins('opus = "claude-opus-7-1"', "opus = 7")
    with pytest.raises(models.ModelPinsUnreadable) as err:
        models.resolve_model("@opus[1m]")
    assert str(model_pins.path) in str(err.value) and "[families] opus" in str(err.value)


def test_the_shipped_pins_resolve_every_default():
    for key in ("manager_model", "distill_model", "scout_model"):
        resolved = models.resolve_model(models.default_model(key))
        assert resolved.startswith("claude-"), (key, resolved)
    assert sorted(models.families()) == ["fable", "haiku", "opus", "sonnet"]
