"""gardener_spend.py — resolve a gardener run's transcript and sum its token usage."""
import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SPEND_PATH = REPO_ROOT / "deploy" / "scripts" / "gardener_spend.py"


def _load_spend():
    spec = importlib.util.spec_from_file_location("gardener_spend_under_test", SPEND_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def spend_mod():
    return _load_spend()


def _usage_line(msg_id, output=0, input_tokens=0, cache_read=0):
    return json.dumps({
        "type": "assistant",
        "message": {
            "id": msg_id, "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": cache_read,
                "output_tokens": output,
            },
        },
    })


def test_sum_usage_dedupes_split_events_and_skips_malformed(spend_mod, tmp_path):
    log = tmp_path / "sid.jsonl"
    log.write_text("\n".join([
        "garbage {{{",
        json.dumps({"type": "user", "message": {"content": "hi"}}),
        _usage_line("msg_a", output=100, input_tokens=3, cache_read=1000),
        _usage_line("msg_a", output=100, input_tokens=3, cache_read=1000),
        _usage_line("msg_b", output=50, input_tokens=1, cache_read=500),
        json.dumps({"type": "assistant", "message": {"id": "msg_no_usage"}}),
    ]) + "\n")
    totals = spend_mod.sum_usage(log)
    assert totals == {"out_tokens": 150, "in_tokens": 4, "cache_read_tokens": 1500, "cache_creation_tokens": 0}


def test_project_dir_munges_every_non_alphanumeric_to_dash(spend_mod):
    assert spend_mod.project_dir_name("/Users/testop/projects/personal/claude-orchestrator") \
        == "-Users-testop-projects-personal-claude-orchestrator"
    assert spend_mod.project_dir_name("/tmp/llm_audit.v2") == "-tmp-llm-audit-v2"


def test_find_run_transcript_matches_run_id_in_head(spend_mod, tmp_path):
    project = tmp_path / "-Users-x-repo"
    project.mkdir(parents=True)
    (project / "other.jsonl").write_text(
        json.dumps({"type": "user", "message": {"content": "unrelated session"}}) + "\n")
    target = project / "gardener-sid.jsonl"
    target.write_text(
        json.dumps({"type": "user", "message": {
            "content": "You are the Gardener (run id 20260611T010203Z-77, trigger: accum)"}}) + "\n")
    found = spend_mod.find_run_transcript(tmp_path, "/Users/x/repo", "20260611T010203Z-77")
    assert found == target


def test_find_run_transcript_none_when_no_match(spend_mod, tmp_path):
    project = tmp_path / "-Users-x-repo"
    project.mkdir(parents=True)
    (project / "a.jsonl").write_text(json.dumps({"type": "user"}) + "\n")
    assert spend_mod.find_run_transcript(tmp_path, "/Users/x/repo", "RUN-NOPE") is None
    assert spend_mod.find_run_transcript(tmp_path, "/Users/never-seen", "RUN-NOPE") is None


def test_find_run_transcript_ignores_run_id_beyond_head_window(spend_mod, tmp_path):
    # The run id is only a discriminator when it's in the session's FIRST
    # prompt; a session that merely mentions the id later must not match.
    project = tmp_path / "-Users-x-repo"
    project.mkdir(parents=True)
    log = project / "chatty.jsonl"
    filler = json.dumps({"type": "user", "message": {"content": "x" * 1000}})
    log.write_text("\n".join([filler] * 5 + [
        json.dumps({"type": "user", "message": {"content": "mentions RUN-DEEP late"}}),
    ]) + "\n")
    assert spend_mod.find_run_transcript(
        tmp_path, "/Users/x/repo", "RUN-DEEP", head_bytes=2048) is None


def test_main_prints_ledger_pairs(spend_mod, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / ".claude" / "projects" / "-Users-x-repo"
    project.mkdir(parents=True)
    (project / "sid.jsonl").write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": "Gardener run id RUN-42"}}),
        _usage_line("msg_a", output=1234, input_tokens=5, cache_read=99),
    ]) + "\n")
    assert spend_mod.main(["/Users/x/repo", "RUN-42"]) == 0
    assert capsys.readouterr().out.strip() \
        == "out_tokens=1234 in_tokens=5 cache_read_tokens=99 cache_creation_tokens=0"


def test_main_prints_nothing_when_unresolvable(spend_mod, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert spend_mod.main(["/Users/x/repo", "RUN-42"]) == 0
    assert capsys.readouterr().out == ""


def test_main_never_raises_on_bad_args(spend_mod, capsys):
    assert spend_mod.main([]) == 0
    assert capsys.readouterr().out == ""


def test_script_runs_under_system_python(tmp_path):
    # gardener-run.sh invokes the deployed copy with /usr/bin/python3 (macOS
    # ships 3.9): modern-only syntax (e.g. `X | None` signature annotations)
    # would crash at import and silently cost the ledger its spend keys.
    import subprocess
    system_python = "/usr/bin/python3"
    if not Path(system_python).exists():
        pytest.skip("no system python3")
    result = subprocess.run(
        [system_python, str(SPEND_PATH), "/nonexistent/cwd", "RUN-X"],
        capture_output=True, text=True, env={"HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
