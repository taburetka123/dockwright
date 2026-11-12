import json
import os
import subprocess
import sys

from dockwright import paths, state
from dockwright.notebook_mark import main, read, refusal, write, write_unless_fresh, notebook_file
from tests.test_mcp_tools import fresh_orchestrator_dir  # noqa: F401

NOTEBOOK = "# N\n\n## open entry\ncheck: x\n"


def _notebook(domain="general", text=NOTEBOOK):
    path = notebook_file(domain)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _marks_dir():
    return paths.ROOT / "notebook" / "marks"


def test_missing_or_empty_notebook_passes(fresh_orchestrator_dir):
    assert refusal("s1", "general") is None
    _notebook(text="")
    assert refusal("s1", "general") is None


def test_notebook_without_mark_refuses(fresh_orchestrator_dir):
    _notebook()
    text = refusal("s1", "general")
    assert "no mark" in text
    assert "dockwright notebook mark --sid s1 --domain general" in text
    assert "--not-actualized" in text


def test_fresh_mark_of_this_session_passes(fresh_orchestrator_dir):
    _notebook()
    write("s1", "general", now=1000.0)
    assert refusal("s1", "general", now=1000.0 + 3600) is None


def test_not_actualized_mark_passes(fresh_orchestrator_dir):
    _notebook()
    write("s1", "general", reason="context exhausted", now=1000.0)
    assert refusal("s1", "general", now=1001.0) is None
    assert read("general") == {"sid": "s1", "at": 1000.0, "actualized": False, "reason": "context exhausted"}


def test_mark_of_another_session_refuses(fresh_orchestrator_dir):
    _notebook()
    write("s0", "general")
    assert "from session s0" in refusal("s1", "general")


def test_stale_mark_refuses(fresh_orchestrator_dir):
    _notebook()
    write("s1", "general", now=1000.0)
    assert "120 minutes old" in refusal("s1", "general", now=1000.0 + 7200)


def test_malformed_mark_reads_as_none_and_refuses(fresh_orchestrator_dir):
    _notebook()
    mark = _marks_dir() / "general.json"
    mark.parent.mkdir(parents=True)
    for text in ("not json", json.dumps({"sid": 3})):
        mark.write_text(text)
        assert read("general") is None
        assert "no mark" in refusal("s1", "general")


def test_cli_mark_takes_domain_from_active_manager_record(fresh_orchestrator_dir):
    state.write_json_atomic(paths.ACTIVE / "s1.json", {"agent": "manager", "domain": "meta"})
    assert main(["mark", "--sid", "s1"]) == 0
    assert read("meta")["sid"] == "s1"
    assert read("meta")["actualized"] is True


def test_cli_mark_without_record_or_domain_exits_2(fresh_orchestrator_dir):
    assert main(["mark", "--sid", "nobody"]) == 2
    assert not _marks_dir().exists() or not any(_marks_dir().iterdir())


def test_unsafe_domain_mark_stays_inside_marks(fresh_orchestrator_dir):
    assert main(["mark", "--sid", "s1", "--domain", "../x"]) == 0
    files = [p for p in paths.ROOT.rglob("*") if p.is_file()]
    assert files == [_marks_dir() / "___x.json"]


def test_non_slug_domain_can_be_marked_and_then_passes(fresh_orchestrator_dir):
    _notebook("alpha.beta")
    assert refusal("s1", "alpha.beta") is not None
    assert main(["mark", "--sid", "s1", "--domain", "alpha.beta"]) == 0
    assert refusal("s1", "alpha.beta") is None


def test_write_unless_fresh(fresh_orchestrator_dir):
    assert write_unless_fresh("s1", "general", "r") is False
    assert read("general") is None
    _notebook()
    write("peer", "general", now=1000.0)
    assert write_unless_fresh("s1", "general", "r", now=1500.0) is False
    assert read("general")["sid"] == "peer"
    assert write_unless_fresh("s1", "general", "r", now=1000.0 + 3601) is True
    assert read("general") == {"sid": "s1", "at": 4601.0, "actualized": False, "reason": "r"}


def test_write_unless_fresh_returns_false_on_oserror(fresh_orchestrator_dir):
    _notebook()
    _marks_dir().write_text("not a directory")
    assert write_unless_fresh("s1", "general", "r") is False


def test_cli_not_actualized_needs_a_reason(fresh_orchestrator_dir):
    assert main(["mark", "--sid", "s1", "--domain", "meta", "--not-actualized", "  "]) == 2


def test_cli_check_exit_codes(fresh_orchestrator_dir, capsys):
    _notebook("meta")
    assert main(["check", "--sid", "s1", "--domain", "meta"]) == 1
    assert "dockwright notebook mark --sid s1 --domain meta" in capsys.readouterr().err
    assert main(["mark", "--sid", "s1", "--domain", "meta", "--not-actualized", "server refused pipeline_start"]) == 0
    assert main(["check", "--sid", "s1", "--domain", "meta"]) == 0


def test_dockwright_notebook_subcommand_dispatches(tmp_path):
    result = subprocess.run([sys.executable, "-m", "dockwright", "notebook", "check", "--sid", "s1", "--domain", "meta"],
                            env={**os.environ, "HOME": str(tmp_path / "h")}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_dash_domain_hint_marks_and_then_passes(fresh_orchestrator_dir):
    _notebook("-x")
    text = refusal("s1", "-x")
    assert "dockwright notebook mark --sid s1 --domain=-x`" in text
    assert main(["mark", "--sid", "s1", "--domain=-x"]) == 0
    assert refusal("s1", "-x") is None


def test_unrepresentable_mark_time_reads_as_none(fresh_orchestrator_dir):
    _notebook()
    mark = _marks_dir() / "general.json"
    mark.parent.mkdir(parents=True)
    for at in ("NaN", "Infinity", "-Infinity", "1e300"):
        mark.write_text(f'{{"sid": "s1", "at": {at}, "actualized": false, "reason": "r"}}')
        assert read("general") is None
        assert "no mark" in refusal("s1", "general")


def test_future_mark_refuses_and_recovery_replaces_it(fresh_orchestrator_dir):
    _notebook()
    write("s1", "general", now=1000.0 + 7200)
    assert "dated in the future" in refusal("s1", "general", now=1000.0)
    assert write_unless_fresh("s2", "general", "r", now=1000.0) is True
    assert read("general")["sid"] == "s2"
