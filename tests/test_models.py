import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from dockwright import models

REPO = Path(__file__).resolve().parent.parent


def test_the_roster_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("dockwright.roster")


@pytest.mark.parametrize("value,problem", [
    ("@fable", None), ("@opus[1m]", None), ("claude-opus-5-5", None), ("claude-fable-5-1[1m]", None),
    ("CLAUDE-OPUS-5", None), ("", "is empty"), ("   ", "is empty"), ("opus", "a bare alias"),
    ("best", "a bare alias"), ("fable-5-1", "a bare alias"), ("@nosuch", "does not resolve"),
    ("claude-fabel-5-1", "names family 'fabel', which is not in the map"),
    ("claude-x-1", "names family 'x', which is not in the map"),
])
def test_model_value_problem_admits_a_token_or_a_concrete_id(value, problem):
    got = models.model_value_problem(value)
    assert got is None if problem is None else problem in got


def test_concrete_family_reads_the_family_of_a_concrete_id_only():
    assert models.concrete_family("claude-fable-5-1[1m]") == "fable"
    assert models.concrete_family("  claude-fabled-9-1 ") == "fabled"
    assert models.concrete_family("claude-haiku-4-5-20251001") == "haiku"
    assert models.concrete_family("fable") is None and models.concrete_family("@opus") is None


def test_resolve_model_expands_family_and_variant():
    assert models.resolve_model("@opus[1m]") == models.families()["opus"] + "[1m]"
    assert models.resolve_model("@opus") == models.families()["opus"]
    assert models.resolve_model("@sonnet") == models.families()["sonnet"]


def test_resolve_model_sees_a_token_through_surrounding_whitespace():
    opus = models.families()["opus"]
    for padded in (" @opus[1m]", "@opus[1m] ", "\t@opus[1m]\n"):
        assert models.resolve_model(padded) == opus + "[1m]", repr(padded)
    with pytest.raises(models.UnknownFamily):
        models.resolve_model(" @nosuch")


def test_resolve_model_passes_concrete_ids_through():
    assert models.resolve_model("claude-opus-5[1m]") == "claude-opus-5[1m]"
    assert models.resolve_model("gpt-5.5") == "gpt-5.5"


def test_resolve_model_refuses_unknown_family():
    for bad in ("@nosuch", "@[1m]", "@", "@OPUS"):
        with pytest.raises(models.UnknownFamily):
            models.resolve_model(bad)


def test_resolve_model_args_expands_tokens_after_and_inside_the_model_flags():
    opus = models.families()["opus"]
    assert models.resolve_model_args(["--model", "@opus", "-m", "@opus[1m]", "--model=@opus", "--effort", "xhigh"]) == [
        "--model", opus, "-m", opus + "[1m]", f"--model={opus}", "--effort", "xhigh"]


def test_map_option_args_applies_fn_only_to_the_named_options_values():
    args = ["--model", "a", "-m", "b", "--model=c", "--effort", "x", "--effort=y", "a"]
    assert models.map_option_args(args, models.MODEL_OPTIONS, str.upper) == [
        "--model", "A", "-m", "B", "--model=C", "--effort", "x", "--effort=y", "a"]
    assert models.map_option_args(args, models.EFFORT_OPTIONS, str.upper) == [
        "--model", "a", "-m", "b", "--model=c", "--effort", "X", "--effort=Y", "a"]


def test_every_default_model_is_a_concrete_versioned_id():
    for family, model in models.families().items():
        assert models.CONCRETE_MODEL.match(model), f"{family} -> {model}"
        assert not model.endswith("-latest"), family


def test_default_model_reads_the_defaults_table(model_pins):
    assert models.default_model("manager_model") == "@opus[1m]"
    with pytest.raises(models.ModelPinsUnreadable):
        models.default_model("nosuch")


def test_model_resolve_cli_prints_the_concrete_id(capsys):
    assert models.model_main(["resolve", "@opus[1m]"]) == 0
    assert capsys.readouterr().out.strip() == models.families()["opus"] + "[1m]"


def test_model_resolve_cli_fails_closed_with_empty_stdout(capsys):
    assert models.model_main(["resolve", "@nosuch"]) == 1
    captured = capsys.readouterr()
    assert captured.out.strip() == ""
    assert "nosuch" in captured.err and str(models.MODELS_FILE) in captured.err


def test_model_resolve_cli_usage_is_non_zero(capsys):
    assert models.model_main(["resolve"]) == 2
    assert models.model_main([]) == 2


def test_model_resolve_cli_exits_non_zero_end_to_end():
    out = subprocess.run([sys.executable, "-m", "dockwright", "model", "resolve", "@nosuch"],
                         capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode != 0, "a bare `return` would exit 0 here"
    assert out.stdout.strip() == ""


def test_the_roster_cli_is_gone():
    out = subprocess.run([sys.executable, "-m", "dockwright", "roster", "resolve", "REVIEW_TIER2"],
                         capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode != 0 and "roster" not in out.stdout
