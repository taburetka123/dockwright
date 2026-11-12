import pytest

from dockwright import config, former_rows


ROSTER = '''
[roles.REVIEW_TIER2]
providers = ["claude", "codex"]
[roles.REVIEW_TIER2.claude]
model = "@opus"
effort = "xhigh"
context = "CodeReviewer"
mode = "report"
execution = "subworker"
[roles.REVIEW_TIER2.codex]
model = "gpt-5.6-sol"
mode = "report"
[roles.BARE]
providers = ["claude"]
[roles.BARE.claude]
model = "@sonnet"
effort = "xhigh"
[roles.NO_MODEL]
providers = ["claude"]
[roles.NO_EFFORT]
providers = ["claude"]
[roles.NO_EFFORT.claude]
model = "@sonnet"
'''


@pytest.fixture
def legacy_file(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    home.mkdir()
    (home / "dockwright-roster.toml").write_text(ROSTER)
    monkeypatch.setattr(config, "claude_config_home", lambda: home)
    return home / "dockwright-roster.toml"


def test_a_row_gives_a_lane_from_its_claude_table_and_never_a_codex_model(legacy_file):
    lane = former_rows.lane_for("REVIEW_TIER2")
    assert (lane.role, lane.mode, lane.model, lane.effort, lane.codex) == ("CodeReviewer", "report", "@opus", "xhigh", None)


def test_a_bare_row_defaults_mode_to_report_and_context_to_generic_worker(legacy_file):
    lane = former_rows.lane_for("BARE")
    assert (lane.role, lane.mode, lane.model, lane.effort) == ("GenericWorker", "report", "@sonnet", "xhigh")


def test_node_args_are_the_model_and_effort_flags(legacy_file):
    assert former_rows.node_args("REVIEW_TIER2") == ["--model", "@opus", "--effort", "xhigh"]


@pytest.mark.parametrize("row,missing", [("NO_MODEL", "model"), ("UNKNOWN", "model"), ("NO_EFFORT", "effort")])
def test_a_row_without_a_claude_model_or_effort_is_refused_with_the_rework_hint(legacy_file, row, missing):
    with pytest.raises(ValueError, match=f"row {row}: the legacy roster at .* has no claude {missing} for it; rework the work"):
        former_rows.lane_for(row)


def test_a_missing_or_unparseable_file_is_refused(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    home.mkdir()
    monkeypatch.setattr(config, "claude_config_home", lambda: home)
    with pytest.raises(ValueError, match="has no claude model for it"):
        former_rows.lane_for("REVIEW_TIER2")
    (home / "dockwright-roster.toml").write_text("not = toml = at all")
    with pytest.raises(ValueError, match="has no claude model for it"):
        former_rows.lane_for("REVIEW_TIER2")
