import json
import subprocess

import pytest

from dockwright import compaction, distill


def _transcript(tmp_path, monkeypatch):
    log = tmp_path / "transcript.jsonl"
    log.write_text(
        json.dumps({"type": "user", "message": {"content": "go"}}) + "\n"
        + json.dumps({"type": "assistant", "message": {
            "content": [{"type": "text", "text": "done"}]}}) + "\n"
    )
    monkeypatch.setattr(distill, "find_session_log", lambda sid: log)


class _Completed:
    def __init__(self, stdout: bytes):
        self.returncode = 0
        self.stdout = stdout
        self.stderr = b""


def _words(n: int, tag: str = "w") -> str:
    return " ".join(f"{tag}{i}" for i in range(n))


def _install_runs(monkeypatch, outputs):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": argv, "input": kwargs.get("input")})
        return _Completed(outputs[len(calls) - 1].encode("utf-8"))

    monkeypatch.setattr(distill.subprocess, "run", fake_run)
    return calls


def test_budget_is_400_words():
    assert distill._DISTILL_WORD_BUDGET == 400


def test_prompt_tells_the_writer_the_word_budget():
    assert f"{distill._DISTILL_WORD_BUDGET} words" in distill._DISTILL_PROMPT


def test_the_distill_child_is_asked_for_the_six_compaction_items_and_the_two_voices_rule(tmp_path, monkeypatch):
    _transcript(tmp_path, monkeypatch)
    calls = _install_runs(monkeypatch, [_words(10)])
    distill._distill_manager_session("sid-six")
    prompt = calls[0]["argv"][calls[0]["argv"].index("-p") + 1]
    assert f"Be sure to preserve: {compaction.preserve_clause()}." in prompt
    assert compaction.VOICES in prompt
    assert "Sections:" not in prompt
    assert f"{distill._DISTILL_WORD_BUDGET} words" in prompt


def test_word_count_matches_wc_w_where_wc_has_no_0xa0_byte():
    sample = "## Résumé\n- the naïve café menu said — ok\n| a | b |\nfile:line `x`\n"
    wc = subprocess.run(
        ["wc", "-w"], input=sample.encode("utf-8"), capture_output=True,
        env={"LC_ALL": "en_US.UTF-8", "PATH": "/usr/bin:/bin"}, check=True,
    )
    assert distill._word_count(sample) == int(wc.stdout.split()[0])


def test_within_budget_calls_claude_once_and_writes_as_is(tmp_path, monkeypatch):
    _transcript(tmp_path, monkeypatch)
    draft = "## Decisions\n" + _words(300)
    calls = _install_runs(monkeypatch, [draft])
    assert distill._distill_manager_session("sid-ok") == draft
    assert len(calls) == 1


def test_overrun_re_asks_once_with_the_count_in_the_prompt(tmp_path, monkeypatch):
    _transcript(tmp_path, monkeypatch)
    first = _words(650, "a")
    second = _words(380, "b")
    calls = _install_runs(monkeypatch, [first, second])
    assert distill._distill_manager_session("sid-over") == second
    assert len(calls) == 2
    retry_prompt = calls[1]["argv"][calls[1]["argv"].index("-p") + 1]
    assert "650 words" in retry_prompt
    assert f"{distill._DISTILL_WORD_BUDGET}" in retry_prompt
    assert calls[1]["input"] == calls[0]["input"]
    assert calls[1]["argv"][1:] == ["-p", retry_prompt] + calls[0]["argv"][3:]


def test_second_overrun_truncates_and_says_so_on_the_first_line(tmp_path, monkeypatch):
    _transcript(tmp_path, monkeypatch)
    first = _words(650, "a")
    second = _words(500, "b")
    calls = _install_runs(monkeypatch, [first, second])
    out = distill._distill_manager_session("sid-over2")
    assert len(calls) == 2
    first_line = out.splitlines()[0]
    assert "truncated" in first_line
    assert "500 words" in first_line
    assert f"{distill._DISTILL_WORD_BUDGET}" in first_line
    assert distill._word_count(out) <= distill._DISTILL_WORD_BUDGET
    assert "b0 " in out and "b499" not in out
    assert "a0" not in out


def test_failed_re_ask_falls_back_to_truncating_the_first_draft(tmp_path, monkeypatch):
    _transcript(tmp_path, monkeypatch)
    first = _words(650, "a")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            return _Completed(first.encode("utf-8"))
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(distill.subprocess, "run", fake_run)
    out = distill._distill_manager_session("sid-retry-dead")
    assert len(calls) == 2
    assert out.splitlines()[0].startswith("[truncated")
    assert "650 words" in out.splitlines()[0]
    assert distill._word_count(out) <= distill._DISTILL_WORD_BUDGET


def test_truncation_keeps_the_head_within_budget():
    body = "## Decisions\n" + _words(60, "d") + "\n## Rulings\n" + _words(60, "r") + "\n## Open threads\n" + _words(60, "o")
    out = distill._fit_to_word_budget(body, 100)
    assert distill._word_count(out) == 100
    assert out.splitlines()[0].startswith("[truncated")
    assert "## Decisions" in out and "## Rulings" in out
    assert "## Open threads" not in out
    assert distill._fit_to_word_budget(_words(50), 100) == _words(50)


def test_truncation_holds_the_budget_when_the_notice_alone_exceeds_it():
    out = distill._fit_to_word_budget(_words(100), 5)
    assert distill._word_count(out) == 5
    assert out.startswith("[truncated")
