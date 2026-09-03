from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "deploy" / "scripts"


def test_distill_does_not_fall_back_to_the_denylist(tmp_path, monkeypatch):
    from dockwright import distill

    log = tmp_path / "transcript.jsonl"
    log.write_text(
        '{"type": "user", "message": {"content": "go"}}\n'
        '{"type": "assistant", "message": {"content": '
        '[{"type": "text", "text": "ok"}]}}\n'
    )
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

    assert "--disallowedTools" not in captured["cmd"]
