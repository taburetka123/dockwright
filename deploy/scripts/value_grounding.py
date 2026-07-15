#!/usr/bin/env python3
"""Value-grounding checker: numbers/versions/ids asserted in a report must
appear in a tool output captured on disk (the session transcript JSONL).

Checks run against the tool-call record, which the model does not author —
provenance-presence checks in prose are defeated by provenance fabrication
(ported from dexter tools/eval_score.py; see the Phase B design spec).

Standalone + stdlib-only: deployed verbatim to ~/.claude/scripts/ by setup.sh.
Consumed three ways: CLI (the integrator agent), importlib (evals gates + tests).
"""
from __future__ import annotations

import argparse
import glob as globmod
import json
import os
import re
import sys
from dataclasses import dataclass

_VERSION_RE = re.compile(r"\bv?\d+\.\d+\.\d+(?:\.\d+)?\b")
_COMMA_COUNT_RE = re.compile(r"\b\d{1,3}(?:,\d{3})+\b")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_LONG_DIGIT_RE = re.compile(r"(?<!\d)\d{6,}(?!\d)")
_TICKET_KEY_RE = re.compile(r"\b[A-Z]{2,}-\d+\b")

ALL_CLASSES = ("version", "comma_count", "uuid", "long_digit_run", "ticket_key")

_CLASS_RES = {
    "version": _VERSION_RE,
    "comma_count": _COMMA_COUNT_RE,
    "uuid": _UUID_RE,
    "long_digit_run": _LONG_DIGIT_RE,
    "ticket_key": _TICKET_KEY_RE,
}


@dataclass(frozen=True)
class Token:
    text: str
    token_class: str


def extract_tokens(text: str, classes: tuple[str, ...] = ALL_CLASSES) -> list[Token]:
    tokens: list[Token] = []
    seen: set[tuple[str, str]] = set()
    for cls in classes:
        for match in _CLASS_RES[cls].finditer(text or ""):
            tok = match.group(0)
            if cls == "version":
                # A year-leading dotted token (2026.06.29) is a date, not a version;
                # service versions start well below 2000 (dexter year-guard).
                if int(tok.lstrip("vV").split(".", 1)[0]) >= 2000:
                    continue
            key = (tok, cls)
            if key not in seen:
                seen.add(key)
                tokens.append(Token(tok, cls))
    return tokens


def _digit_boundary_search(needle: str, corpus: str) -> bool:
    return re.search(rf"(?<!\d){re.escape(needle)}(?!\d)", corpus) is not None


def is_grounded(token: Token, corpus: str) -> bool:
    corpus = corpus or ""
    text = token.text
    if token.token_class == "version":
        bare = text.lstrip("vV")
        return _digit_boundary_search(bare, corpus) or _digit_boundary_search(text, corpus)
    if token.token_class == "comma_count":
        return text in corpus or _digit_boundary_search(text.replace(",", ""), corpus)
    if token.token_class == "uuid":
        return text.lower() in corpus.lower()
    if token.token_class == "long_digit_run":
        return _digit_boundary_search(text, corpus)
    return re.search(rf"{re.escape(text)}(?!\d)", corpus) is not None  # ticket_key


def ungrounded(report: str, corpus: str, classes: tuple[str, ...] = ALL_CLASSES) -> list[Token]:
    return [t for t in extract_tokens(report, classes) if not is_grounded(t, corpus)]


_EXCLUDED_RESULT_TOOLS = {"Agent", "Task"}


def _content_blocks(message) -> list:
    content = (message or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


def _block_text(block) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def parse_transcripts(paths: list[str]) -> tuple[list[tuple[str, str]], str]:
    """Merged (tool_calls, evidence_corpus) across transcript JSONL files.

    Corpus = user text + tool outputs whose tool is NOT Agent/Task (a subagent's
    final report is model-authored prose — counting it would launder fabricated
    values into "grounded"). Assistant text never enters the corpus.
    """
    tool_calls: list[tuple[str, str]] = []
    corpus_parts: list[str] = []
    for path in paths:
        id_to_name: dict[str, str] = {}
        records: list[dict] = []
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict):
                        records.append(rec)
        except OSError:
            continue
        for rec in records:  # pass 1: map tool_use ids to names, collect calls
            if rec.get("type") != "assistant":
                continue
            for block in _content_blocks(rec.get("message")):
                if block.get("type") == "tool_use":
                    name = str(block.get("name", ""))
                    id_to_name[str(block.get("id", ""))] = name
                    try:
                        input_str = json.dumps(block.get("input", {}), ensure_ascii=False)
                    except (TypeError, ValueError):
                        input_str = str(block.get("input", ""))
                    tool_calls.append((name, input_str))
        for rec in records:  # pass 2: corpus from user records
            if rec.get("type") != "user":
                continue
            rec_has_excluded_result = False
            for block in _content_blocks(rec.get("message")):
                if block.get("type") == "text":
                    corpus_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    name = id_to_name.get(str(block.get("tool_use_id", "")), "")
                    if name in _EXCLUDED_RESULT_TOOLS:
                        rec_has_excluded_result = True
                        continue
                    corpus_parts.append(_block_text(block))
            if "toolUseResult" in rec and not rec_has_excluded_result:
                raw = rec["toolUseResult"]
                corpus_parts.append(raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False))
    return tool_calls, "\n".join(p for p in corpus_parts if p)


def corpus_from_transcripts(paths: list[str]) -> str:
    return parse_transcripts(paths)[1]


def _default_config_dirs() -> list[str]:
    dirs = []
    for env in ("CLAUDE_CODE_CONFIG_DIR", "CLAUDE_CONFIG_DIR"):
        if os.environ.get(env):
            dirs.append(os.environ[env])
    dirs += [os.path.expanduser("~/.claude"), os.path.expanduser("~/.claude-b")]
    return dirs


def find_session_transcripts(session_id: str, config_dirs: list[str] | None = None) -> list[str]:
    found: list[str] = []
    for root in config_dirs or _default_config_dirs():
        found += globmod.glob(os.path.join(root, "projects", "*", f"{session_id}.jsonl"))
        found += globmod.glob(os.path.join(root, "projects", "*", session_id, "subagents", "*.jsonl"))
    return sorted(set(found))


def _sibling_subagent_transcripts(transcript_path: str) -> list[str]:
    stem = os.path.splitext(transcript_path)[0]
    return sorted(globmod.glob(os.path.join(stem, "subagents", "*.jsonl")))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Ground report values in transcript tool outputs")
    parser.add_argument("--report", required=True, help="report file path, or - for stdin")
    parser.add_argument("--transcript", action="append", default=[], help="transcript JSONL (repeatable)")
    parser.add_argument("--session", help="session id to locate transcripts for")
    parser.add_argument("--config-dir", action="append", default=[], help="config dir(s) to search")
    parser.add_argument("--classes", default=",".join(ALL_CLASSES))
    args = parser.parse_args(argv)

    classes = tuple(c for c in args.classes.split(",") if c)
    unknown = set(classes) - set(ALL_CLASSES)
    if unknown:
        print(json.dumps({"error": f"unknown classes: {sorted(unknown)}"}))
        return 2

    report = sys.stdin.read() if args.report == "-" else None
    if report is None:
        try:
            with open(args.report, encoding="utf-8") as fh:
                report = fh.read()
        except OSError as exc:
            print(json.dumps({"error": f"cannot read report: {exc}"}))
            return 2

    transcripts = list(args.transcript)
    for t in list(transcripts):
        transcripts += _sibling_subagent_transcripts(t)
    if args.session:
        transcripts += find_session_transcripts(args.session, args.config_dir or None)
    transcripts = sorted(set(transcripts))
    if not transcripts:
        print(json.dumps({"error": "no transcripts found"}))
        return 2

    _, corpus = parse_transcripts(transcripts)
    missing = ungrounded(report, corpus, classes)
    print(json.dumps({
        "ungrounded": [{"token": t.text, "class": t.token_class} for t in missing],
        "checked_tokens": len(extract_tokens(report, classes)),
        "corpus_bytes": len(corpus),
        "transcripts": transcripts,
    }, ensure_ascii=False))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
