# tests/test_value_grounding.py
"""Unit tests for deploy/scripts/value_grounding.py (imported via importlib —
the script is standalone, not a package module; see test_gardener_postrun.py)."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

VG_PATH = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "value_grounding.py"


@pytest.fixture(scope="module")
def vg():
    spec = importlib.util.spec_from_file_location("value_grounding_under_test", VG_PATH)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so the @dataclass decorator can resolve its module
    # (see test_worktree_prune.py — same gotcha with frozen dataclass + `from
    # __future__ import annotations`).
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _texts(tokens):
    return [t.text for t in tokens]


class TestExtractTokens:
    def test_versions_with_and_without_v(self, vg):
        toks = vg.extract_tokens("deployed v1.900.440 then 16.2.9", classes=("version",))
        assert _texts(toks) == ["v1.900.440", "16.2.9"]

    def test_version_year_guard_skips_dotted_dates(self, vg):
        assert vg.extract_tokens("on 2026.06.29 we saw it", classes=("version",)) == []

    def test_comma_counts(self, vg):
        toks = vg.extract_tokens("queue depth 1,558+ and 1,234,567 rows", classes=("comma_count",))
        assert _texts(toks) == ["1,558", "1,234,567"]

    def test_uuid(self, vg):
        u = "db77cc0d-027c-489f-9148-9d4dcc11dd35"
        assert _texts(vg.extract_tokens(f"session {u}", classes=("uuid",))) == [u]

    def test_long_digit_run_and_boundaries(self, vg):
        toks = vg.extract_tokens("epoch 1720958400 port 8080", classes=("long_digit_run",))
        assert _texts(toks) == ["1720958400"]  # 8080 is <6 digits

    def test_ticket_key(self, vg):
        assert _texts(vg.extract_tokens("see TKT-8517.", classes=("ticket_key",))) == ["TKT-8517"]

    def test_all_classes_default(self, vg):
        toks = vg.extract_tokens("v1.2.3 and 1,558 and TKT-1")
        classes = {t.token_class for t in toks}
        assert classes == {"version", "comma_count", "ticket_key"}


class TestGrounding:
    def test_version_grounded_with_or_without_v(self, vg):
        t = vg.Token("v1.900.537", "version")
        assert vg.is_grounded(t, "image tag 1.900.537 running")
        assert not vg.is_grounded(t, "image tag 1.900.440 running")

    def test_comma_count_grounded_by_bare_form_on_digit_boundary(self, vg):
        t = vg.Token("1,558", "comma_count")
        assert vg.is_grounded(t, "ApproximateNumberOfMessages: 1558")
        # substring of a longer number does NOT ground it
        assert not vg.is_grounded(t, "id 91558723")

    def test_uuid_case_insensitive(self, vg):
        t = vg.Token("DB77CC0D-027C-489F-9148-9D4DCC11DD35", "uuid")
        assert vg.is_grounded(t, "sid db77cc0d-027c-489f-9148-9d4dcc11dd35")

    def test_long_digit_run_digit_boundary(self, vg):
        t = vg.Token("1720958400", "long_digit_run")
        assert vg.is_grounded(t, "ts=1720958400Z")
        assert not vg.is_grounded(t, "ts=91720958400123")

    def test_ticket_key_literal(self, vg):
        t = vg.Token("TKT-8517", "ticket_key")
        assert vg.is_grounded(t, "per TKT-8517 comment")
        assert not vg.is_grounded(t, "per TKT-851 comment")

    def test_version_not_grounded_by_longer_number_prefix(self, vg):
        t = vg.Token("1.2.3", "version")
        assert not vg.is_grounded(t, "release 21.2.3")
        assert vg.is_grounded(t, "v1.2.3 deployed")

    def test_ticket_key_not_grounded_by_longer_number_suffix(self, vg):
        t = vg.Token("TKT-8517", "ticket_key")
        assert not vg.is_grounded(t, "TKT-85170")
        assert vg.is_grounded(t, "see TKT-8517,")


class TestUngrounded:
    def test_reports_only_missing_tokens_deduped(self, vg):
        report = "rate for v1.2.3 was 1,558 (v1.2.3 again)"
        corpus = "v1.2.3 deployed"
        out = vg.ungrounded(report, corpus)
        assert [(t.text, t.token_class) for t in out] == [("1,558", "comma_count")]

    def test_empty_report_or_classes(self, vg):
        assert vg.ungrounded("", "anything") == []
        assert vg.ungrounded("v9.9.9", "x", classes=()) == []


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


@pytest.fixture()
def transcript_tree(tmp_path):
    """Main transcript + subagents sidecar mimicking <cfg>/projects/<slug>/."""
    proj = tmp_path / "projects" / "-tmp-work"
    sid = "aaaaaaaa-1111-2222-3333-bbbbbbbbcccc"
    main = proj / f"{sid}.jsonl"
    _write_jsonl(main, [
        {"type": "user", "message": {"content": [{"type": "text", "text": "brief mentions TKT-9999"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "fixtures/es-errors.log"}},
            {"type": "tool_use", "id": "t2", "name": "Agent", "input": {"prompt": "advocate"}},
            {"type": "text", "text": "assistant prose v7.7.7 never corpus"},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "error at v1.900.537"}]},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "advocate says v6.6.6"}]},
        ]}, "toolUseResult": {"report": "agent-report v6.6.6"}},
    ])
    sub = proj / sid / "subagents" / "agent-abc.jsonl"
    _write_jsonl(sub, [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "s1", "name": "Grep", "input": {"pattern": "depth", "path": "fixtures/queue.json"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "s1", "content": [{"type": "text", "text": "ApproximateNumberOfMessages: 1558"}]},
        ]}},
    ])
    return {"root": tmp_path, "sid": sid, "main": main, "sub": sub}


class TestParseTranscripts:
    def test_merges_tool_calls_across_main_and_subagents(self, vg, transcript_tree):
        calls, _ = vg.parse_transcripts([str(transcript_tree["main"]), str(transcript_tree["sub"])])
        names = [c[0] for c in calls]
        assert "Read" in names and "Grep" in names and "Agent" in names
        read_input = next(c[1] for c in calls if c[0] == "Read")
        assert "fixtures/es-errors.log" in read_input

    def test_corpus_includes_user_text_and_real_tool_outputs(self, vg, transcript_tree):
        _, corpus = vg.parse_transcripts([str(transcript_tree["main"]), str(transcript_tree["sub"])])
        assert "TKT-9999" in corpus            # user brief
        assert "v1.900.537" in corpus          # Read output
        assert "1558" in corpus                # subagent Grep output

    def test_corpus_excludes_agent_results_and_assistant_text(self, vg, transcript_tree):
        _, corpus = vg.parse_transcripts([str(transcript_tree["main"]), str(transcript_tree["sub"])])
        assert "v6.6.6" not in corpus          # Agent tool_result + toolUseResult both excluded
        assert "v7.7.7" not in corpus          # assistant prose excluded

    def test_malformed_lines_skipped(self, vg, tmp_path):
        p = tmp_path / "bad.jsonl"
        p.write_text('{"type":"user","message":{"content":"plain string brief"}}\nnot-json\n')
        calls, corpus = vg.parse_transcripts([str(p)])
        assert calls == [] and "plain string brief" in corpus


class TestFindSessionTranscripts:
    def test_globs_main_and_subagents(self, vg, transcript_tree):
        found = vg.find_session_transcripts(
            transcript_tree["sid"], config_dirs=[str(transcript_tree["root"])]
        )
        assert str(transcript_tree["main"]) in found
        assert str(transcript_tree["sub"]) in found


class TestCli:
    def test_exit_codes_and_json(self, vg, transcript_tree, tmp_path, capsys):
        report = tmp_path / "report.md"
        report.write_text("we saw v1.900.537 and depth 1,558")
        rc = vg.main([
            "--report", str(report),
            "--session", transcript_tree["sid"],
            "--config-dir", str(transcript_tree["root"]),
        ])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0 and out["ungrounded"] == []

        report.write_text("fabricated v9.9.9")
        rc = vg.main([
            "--report", str(report),
            "--session", transcript_tree["sid"],
            "--config-dir", str(transcript_tree["root"]),
        ])
        out = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert out["ungrounded"] == [{"token": "v9.9.9", "class": "version"}]

    def test_usage_error_no_transcripts(self, vg, tmp_path, capsys):
        report = tmp_path / "r.md"
        report.write_text("x")
        rc = vg.main(["--report", str(report), "--session", "nope", "--config-dir", str(tmp_path)])
        assert rc == 2
