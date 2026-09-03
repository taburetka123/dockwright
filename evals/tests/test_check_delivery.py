import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

SKILL_LINE = "Investigate by falsifying the hypothesis you most want to be true."
assert len(SKILL_LINE) >= 40
SKILL_TEXT = f"# Scratch investigate skill\n\n- {SKILL_LINE}\n"


def _mk_transcript(config_dir: Path, sid: str, tool_input, result_text):
    proj = config_dir / "projects" / "slug"
    proj.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "t1",
                                     "name": "Read", "input": tool_input}]},
        }),
        json.dumps({
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                     "content": result_text}]},
        }),
    ]
    (proj / f"{sid}.jsonl").write_text("\n".join(lines) + "\n")


def _mk_trace(path: Path, sids):
    with path.open("w") as fh:
        for i, sid in enumerate(sids):
            fh.write(json.dumps({"case_id": f"c{i}", "session_id": sid,
                                 "findings": "", "gate_failures": None,
                                 "judge": None, "error": None,
                                 "transcript_missing": False}) + "\n")


def _mk_skill(tmp_path: Path) -> Path:
    skill = tmp_path / "SKILL.md"
    skill.write_text(SKILL_TEXT)
    return skill


def _run(trace, skill, expect, config_dir):
    return subprocess.run(
        [sys.executable, "-m", "evals.investigation.check_delivery",
         "--trace", str(trace), "--skill", str(skill),
         "--expect", expect, "--config-dir", str(config_dir)],
        capture_output=True, text=True, cwd=REPO)


def test_all_delivered_exit_0(tmp_path):
    skill = _mk_skill(tmp_path)
    _mk_transcript(tmp_path / "cfg", "s1",
                   {"file_path": "investigate-skill.md"},
                   f"     1\t# Scratch investigate skill\n     3\t- {SKILL_LINE}\n")
    trace = tmp_path / "run.jsonl"
    _mk_trace(trace, ["s1"])
    p = _run(trace, skill, "read", tmp_path / "cfg")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "1/1" in p.stdout


def test_denied_read_of_bound_path_is_not_delivery(tmp_path):
    skill = _mk_skill(tmp_path)
    _mk_transcript(
        tmp_path / "cfg", "s1",
        {"file_path": str(skill)},
        f"Claude requested permissions to read {skill}, but you haven't "
        "granted it yet.")
    trace = tmp_path / "run.jsonl"
    _mk_trace(trace, ["s1"])
    p = _run(trace, skill, "read", tmp_path / "cfg")
    assert p.returncode == 1, p.stdout + p.stderr
    assert "NOT-READ" in p.stdout
    assert _run(trace, skill, "not-read", tmp_path / "cfg").returncode == 0


def test_not_read_detected_exit_1(tmp_path):
    skill = _mk_skill(tmp_path)
    _mk_transcript(tmp_path / "cfg", "s1", {"file_path": "/some/other/file"},
                   "unrelated fixture content, nothing from the bound skill")
    trace = tmp_path / "run.jsonl"
    _mk_trace(trace, ["s1"])
    assert _run(trace, skill, "read", tmp_path / "cfg").returncode == 1
    assert _run(trace, skill, "not-read", tmp_path / "cfg").returncode == 0


def test_missing_transcript_is_indeterminate_exit_2(tmp_path):
    skill = _mk_skill(tmp_path)
    trace = tmp_path / "run.jsonl"
    _mk_trace(trace, ["ghost-sid"])
    p = _run(trace, skill, "read", tmp_path / "cfg")
    assert p.returncode == 2


def test_unreadable_skill_exits_2(tmp_path):
    trace = tmp_path / "run.jsonl"
    _mk_trace(trace, ["s1"])
    _mk_transcript(tmp_path / "cfg", "s1", {"file_path": "x"}, "y")
    p = _run(trace, tmp_path / "no-such-skill.md", "read", tmp_path / "cfg")
    assert p.returncode == 2, p.stdout + p.stderr
    assert "no-such-skill.md" in p.stderr


def test_probeless_skill_exits_2(tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text("# tiny\nshort line\n")
    trace = tmp_path / "run.jsonl"
    _mk_trace(trace, ["s1"])
    _mk_transcript(tmp_path / "cfg", "s1", {"file_path": "x"}, "y")
    p = _run(trace, skill, "read", tmp_path / "cfg")
    assert p.returncode == 2, p.stdout + p.stderr
