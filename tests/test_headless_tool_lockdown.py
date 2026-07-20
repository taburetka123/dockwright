"""Headless claude spawns must disallow file-writing tools.

The selffix retro, gardener headless digest, and manager-memory distill lanes
all feed session-transcript content (untrusted: Jira text, PR comments,
fetched pages) into `claude -p`. Their only legitimate output is markdown on
stdout, captured by the caller (selffix-run.sh > $OUT, gardener-run.sh >
$DIGEST, distill.py subprocess capture) — so Write/Edit/NotebookEdit are
reachable-but-unused surface and must stay hard-disallowed. Repo copies under
deploy/scripts/ are the source of truth; setup.sh deploys them.
"""
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "deploy" / "scripts"
LOCKDOWN_FLAG = '--disallowedTools "Write,Edit,NotebookEdit"'


def _code_lines(text: str) -> list[str]:
    # The run scripts also document the flag in comments (selffix-run.sh's
    # header contract), so a raw substring match over the whole text stays
    # green even when the actual invocation drops the flag. Match only
    # non-comment lines so the guard binds to executed code, not prose.
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def test_selffix_retro_disallows_file_writing_tools():
    src = (SCRIPTS / "selffix-run.sh").read_text()
    assert any(LOCKDOWN_FLAG in line for line in _code_lines(src))


def test_gardener_headless_path_disallows_file_writing_tools():
    # Anchored to the headless block: the visible tmux lane intentionally
    # scopes tools via the gardener-analyst settings preset instead.
    src = (SCRIPTS / "gardener-run.sh").read_text()
    headless_block = src.split('if [ "$MODE" = "headless" ]')[1].split("\nfi\n")[0]
    assert any(LOCKDOWN_FLAG in line for line in _code_lines(headless_block))


def test_distill_cmd_disallows_file_writing_tools(tmp_path, monkeypatch):
    from dockwright import distill

    log = tmp_path / "transcript.jsonl"
    log.write_text('{"type": "user", "message": {"content": "go"}}\n')
    monkeypatch.setattr("dockwright.distill.find_session_log", lambda sid: log)

    captured = {}

    class _FakeCompleted:
        returncode = 0
        stdout = b"## Decisions\nok\n"
        stderr = b""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompleted()

    monkeypatch.setattr("dockwright.distill.subprocess.run", fake_run)
    assert distill._distill_manager_session("sid-lockdown") is not None

    cmd = captured["cmd"]
    flag_at = cmd.index("--disallowedTools")
    assert cmd[flag_at + 1] == "Write,Edit,NotebookEdit"
