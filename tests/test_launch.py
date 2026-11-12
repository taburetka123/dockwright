import os
import stat
import subprocess
import sys

from dockwright import launch


def test_expand_replaces_the_file_pair_with_the_json(tmp_path):
    spec = tmp_path / "back.json"
    spec.write_text('{"general-purpose": {"description": "d", "prompt": "P"}}')
    assert launch.expand(["--model", "m", "--agents-file", str(spec), "-p", "hi"]) == [
        "--model", "m", "--agents", spec.read_text(), "-p", "hi"]
    assert launch.expand([f"--agents-file={spec}"]) == ["--agents", spec.read_text()]
    assert launch.expand(["--model", "m"]) == ["--model", "m"]


def test_main_execs_claude_with_the_expanded_argv(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    spec = tmp_path / "none.json"
    spec.write_text('{"x": {"description": "d", "prompt": "P"}}')
    env = {**os.environ, "DOCKWRIGHT_CLAUDE_BIN": str(fake)}
    out = subprocess.run([sys.executable, "-m", "dockwright", "launch", "--agents-file", str(spec), "--resume", "s1"],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == ["--agents", spec.read_text(), "--resume", "s1"]


def test_main_fails_loud_without_a_claude(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKWRIGHT_CLAUDE_BIN", "")
    monkeypatch.setenv("PATH", str(tmp_path))
    try:
        launch.main([])
    except SystemExit as err:
        assert "no `claude` on PATH" in str(err)
    else:
        raise AssertionError("launch.main returned")


def test_expand_inlines_an_agents_file_path_when_the_session_is_not_print_mode(tmp_path):
    spec = tmp_path / "back.json"
    spec.write_text('{"general-purpose": {"description": "d", "prompt": "P"}}')
    assert launch.expand(["--agents", str(spec), "--model", "m"]) == ["--agents", spec.read_text(), "--model", "m"]
    assert launch.expand([f"--agents={spec}"]) == ["--agents", spec.read_text()]
    assert launch.expand(["--agents", str(spec), "-p", "hi"]) == ["--agents", str(spec), "-p", "hi"]
    assert launch.expand(["--print", "--agents", str(spec)]) == ["--print", "--agents", str(spec)]
    assert launch.expand(["--agents", '{"x": {}}']) == ["--agents", '{"x": {}}']
