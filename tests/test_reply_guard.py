import io
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from dockwright import paths, reply_guard

KEY = "note-meta-x"
REPO = Path(__file__).resolve().parents[1]
PRESET = REPO / "deploy" / "presets" / "reply-settings.json"
OWN = "@@OWN@@"


def _resolved(payload):
    own = str(paths.topic_export_dir(KEY))
    tool_input = {k: v.replace(OWN, own) if isinstance(v, str) else v for k, v in payload["tool_input"].items()}
    return {**payload, "tool_input": tool_input}


def _call(tool, **tool_input):
    return {"session_id": "s", "tool_name": tool, "tool_input": tool_input}


def _bash(command):
    return _call("Bash", command=command)


def _put(**tool_input):
    return _call("mcp__dockwright__artifact_put",
                 **{"content_path": f"{OWN}/answer.md", "status": "complete", "writer_sid": "s", **tool_input})


DENIED = [
    _call("mcp__chat__send_message", channel_id="C1", message="the cert is renewed"),
    _call("mcp__dockwright__pipeline_start", task_key=KEY, schema="ticket"),
    _call("mcp__dockwright__spawn_worker", initial_prompt="start the fix"),
    _call("SomeFutureTool"),
    _call("Write", file_path="/tmp/x", content="x"),
    _call("Write", file_path="relative.md", content="x"),
    _call("Agent", prompt="start the fix"),
    _bash("gh pr comment 1 -b x"),
    _bash(f"dockwright board topic {KEY}; rm -rf /tmp/y"),
    _bash(f"dockwright board topic {KEY} > /tmp/y"),
    _bash("printenv $(rm -rf /tmp/y)"),
    _bash("printenv"),
    _bash("printenv GH_TOKEN"),
    _bash("printenv CLAUDE_CODE_SESSION_ID GH_TOKEN"),
    _bash("dockwright board --serve --port 8741"),
    _bash("dockwright board --ser"),
    _bash(f"dockwright board topic {KEY} --serve"),
    _bash("dockwright board --port=8741 --serve"),
    _put(task_key=KEY, phase="decision", name="msg-1", decided_by="engineer"),
    _put(task_key="other-topic", phase="reply", name="20260930T101010"),
    _put(task_key=KEY, phase="summary", name="other"),
    _put(ticket="other-topic", phase="reply", name="20260930T101010"),
    _call("mcp__dockwright__worker_done", claude_sid="another-session", summary="done"),
    _call("mcp__dockwright__child_done", child_sid="another-session", summary="done"),
    _call("mcp__dockwright__artifact_put", content_path=f"{OWN}/answer.md", status="complete",
          writer_sid="another-session", task_key=KEY, phase="reply", name="20260930T101010"),
    _put(task_key=KEY, phase="reply", name="20260930T101010", content_path="/tmp/x"),
    _put(task_key=KEY, phase="reply", name="20260930T101010", content_path=f"{OWN}/../other-key/a.md"),
]

ALLOWED = [
    _call("Read", file_path="/tmp/x"),
    _call("Grep", pattern="cert"),
    _call("mcp__dockwright__artifact_get", task_key=KEY, phase="summary", name="thread", reader_sid="s"),
    _call("mcp__dockwright__artifact_list", task_key=KEY),
    _call("mcp__dockwright__worker_done", claude_sid="s", summary="topic agent reply"),
    _call("mcp__dockwright__child_done", child_sid="s", summary="topic agent reply"),
    _bash(f"dockwright board topic {KEY}"),
    _bash("printenv CLAUDE_CODE_SESSION_ID"),
    _bash("dockwright board"),
    _bash("dockwright board inbox --domain meta"),
    _bash("dockwright board topics --json --domain meta --kind note --status open"),
    _put(task_key=KEY, phase="reply", name="20260930T101010"),
    _put(task_key=KEY, phase="summary", name="thread", overwrite=True),
    _put(ticket=KEY, phase="reply", name="20260930T101010"),
    _call("Write", file_path=f"{OWN}/answer.md", content="x"),
]


def _denial(decision):
    assert decision is not None
    out = decision["hookSpecificOutput"]
    assert (out["hookEventName"], out["permissionDecision"]) == ("PreToolUse", "deny")
    return out["permissionDecisionReason"]


@pytest.mark.parametrize("payload", DENIED, ids=lambda p: p["tool_name"])
def test_the_guard_denies_every_call_it_does_not_name(payload):
    reason = _denial(reply_guard.decide(_resolved(payload), KEY))
    assert payload["tool_name"] in reason and "changes nothing" in reason and "reply." in reason


@pytest.mark.parametrize("payload", ALLOWED, ids=lambda p: p["tool_name"])
def test_the_guard_lets_reads_and_its_own_answer_through(payload):
    assert reply_guard.decide(_resolved(payload), KEY) is None


def test_without_its_topic_key_the_guard_denies_every_put():
    _denial(reply_guard.decide(_put(task_key=KEY, phase="reply", name="20260930T101010"), None))


@pytest.mark.parametrize("payload", [{}, [], {"tool_name": "Read"}, {"tool_name": "Read", "tool_input": "x"},
                                     {"tool_name": None, "tool_input": {}}])
def test_a_hook_input_the_guard_cannot_read_is_denied(payload):
    _denial(reply_guard.decide(payload, KEY))


def _main(monkeypatch, stdin):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    return reply_guard.main([])


def test_main_answers_the_hook_on_stdout(monkeypatch, capsys):
    monkeypatch.setenv(reply_guard.KEY_ENV, KEY)
    assert _main(monkeypatch, json.dumps(_resolved(_put(task_key=KEY, phase="reply", name="20260930T101010")))) == 0
    assert capsys.readouterr().out == ""
    assert _main(monkeypatch, json.dumps(_call("mcp__dockwright__pipeline_start", task_key=KEY))) == 0
    _denial(json.loads(capsys.readouterr().out))
    assert _main(monkeypatch, "not json") == 0
    _denial(json.loads(capsys.readouterr().out))


def test_main_denies_a_put_when_the_session_carries_no_topic_key(monkeypatch, capsys):
    monkeypatch.delenv(reply_guard.KEY_ENV, raising=False)
    assert _main(monkeypatch, json.dumps(_put(task_key=KEY, phase="reply", name="20260930T101010"))) == 0
    _denial(json.loads(capsys.readouterr().out))


def _preset_hook_command():
    entries = json.loads(PRESET.read_text())["hooks"]["PreToolUse"]
    return next(entry["hooks"][0]["command"] for entry in entries if entry["matcher"] == "*")


def _run_hook(command, payload, tmpdir=None):
    env = {**os.environ, "PYTHONPATH": str(REPO / "src"), reply_guard.KEY_ENV: KEY}
    if tmpdir is not None:
        env["TMPDIR"] = str(tmpdir)
    return subprocess.run(["sh", "-c", command], input=json.dumps(payload), capture_output=True, text=True,
                          timeout=30, env=env)


def test_the_preset_hook_denies_while_its_binary_is_unrendered():
    result = _run_hook(_preset_hook_command(), _call("mcp__chat__send_message", message="x"))
    assert result.returncode == 0
    _denial(json.loads(result.stdout))
    result = _run_hook(_preset_hook_command(), _call("mcp__dockwright__artifact_list", task_key=KEY))
    _denial(json.loads(result.stdout))


def test_the_rendered_preset_hook_runs_the_guard(tmp_path):
    command = _preset_hook_command().replace("@@DOCKWRIGHT_BIN@@", f"{shlex.quote(sys.executable)} -m dockwright")
    foreign = _run_hook(command, _call("mcp__chat__send_message", message="x"))
    assert foreign.returncode == 0
    assert "mcp__chat__send_message" in _denial(json.loads(foreign.stdout))
    listed = _run_hook(command, _call("mcp__dockwright__artifact_list", task_key=KEY))
    assert (listed.returncode, listed.stdout) == (0, "")
    own = Path(os.path.realpath(tmp_path)) / "dockwright" / "topic" / KEY
    answered = _run_hook(command, _put(task_key=KEY, phase="reply", name="20260930T101010",
                                       content_path=str(own / "answer.md")), tmp_path)
    assert (answered.returncode, answered.stdout) == (0, "")
    strayed = _run_hook(command, _put(task_key=KEY, phase="reply", name="20260930T101010",
                                      content_path="/tmp/x"), tmp_path)
    _denial(json.loads(strayed.stdout))


def test_without_a_session_id_the_guard_denies_the_done_signal_and_the_put():
    for payload in (_call("mcp__dockwright__worker_done", claude_sid="s", summary="done"),
                    _put(task_key=KEY, phase="reply", name="20260930T101010")):
        payload.pop("session_id")
        _denial(reply_guard.decide(payload, KEY))


@pytest.mark.parametrize("target", [
    lambda: paths.topic_export_dir("other-key") / "a.md",
    lambda: paths.topic_export_dir(KEY) / ".." / "other-key" / "a.md",
    lambda: paths.topic_export_dir(KEY),
])
def test_a_write_outside_its_own_topic_folder_is_denied(target):
    payload = _call("Write", file_path=str(target()), content="x")
    assert _denial(reply_guard.decide(payload, KEY))


def test_a_write_through_a_symlinked_parent_resolves_before_the_check(tmp_path):
    paths.topic_export_dir(KEY).mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(paths.EXPORTS)
    payload = _call("Write", file_path=str(link / "topic" / KEY / "a.md"), content="x")
    assert reply_guard.decide(payload, KEY) is None


def test_without_its_topic_key_the_guard_denies_every_write():
    assert _denial(reply_guard.decide(_resolved(_call("Write", file_path=f"{OWN}/a.md", content="x")), None))
