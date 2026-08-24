"""Deterministic gate — adapted from an earlier deterministic scorer,
adapted: required_queries -> required_reads (fixture-path suffix match against
Read/Grep/Glob/Bash tool-call inputs, falling back to content evidence — >=80%
of the fixture's unique distinctive lines present in the captured corpus —
when the tool-call input names no file), loops -> num_turns, plus the
value-grounding gate backed by deploy/scripts/value_grounding.py."""
from __future__ import annotations

import importlib.util
import os
import re
import sys
from dataclasses import dataclass, field

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_VG_PATH = os.path.join(REPO_ROOT, "deploy", "scripts", "value_grounding.py")

_CATEGORY_RE = re.compile(r"ROOT_CAUSE_CATEGORY:\s*([a-z_]+)", re.IGNORECASE)
_READ_TOOLS = {"Read", "Grep", "Glob", "Bash"}

_vg_module = None


def load_value_grounding():
    global _vg_module
    if _vg_module is None:
        spec = importlib.util.spec_from_file_location("value_grounding", _VG_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _vg_module = module
    return _vg_module


def parse_category(findings: str) -> str | None:
    match = _CATEGORY_RE.search(findings or "")
    return match.group(1).lower() if match else None


def _read_satisfied(required: str, tool_calls: list[tuple[str, str]]) -> bool:
    basename = os.path.basename(required)
    for name, input_str in tool_calls:
        if name not in _READ_TOOLS:
            continue
        if required in input_str or basename in input_str:
            return True
    return False


READ_CONTENT_LINE_FRACTION = 0.8
_MIN_DISTINCTIVE_LINE_LEN = 8


def _distinctive_lines(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if len(stripped) >= _MIN_DISTINCTIVE_LINE_LEN and stripped not in seen:
            seen.add(stripped)
            out.append(stripped)
    return out


def _content_satisfied(fixture_text: str, corpus: str) -> bool:
    """A read demonstrably happened when >=80% of the fixture's unique
    distinctive lines appear in the captured evidence corpus — whatever shell
    form fetched them (a bash glob names no file in its input). Empty fixture
    or corpus never passes, and neither does a single-line fixture — there's
    no fractional signal to threshold against a lone line."""
    lines = _distinctive_lines(fixture_text)
    if len(lines) < 2 or not corpus:
        return False
    present = sum(1 for line in lines if line in corpus)
    return present / len(lines) >= READ_CONTENT_LINE_FRACTION


@dataclass
class GateResult:
    passed: bool
    failures: list[str] = field(default_factory=list)
    category: str | None = None


def score_deterministic(
    *, findings: str, tool_calls: list[tuple[str, str]], num_turns: int,
    answer: dict, corpus: str = "", fixture_texts: dict[str, str] | None = None,
) -> GateResult:
    failures: list[str] = []
    category = parse_category(findings)
    text = (findings or "").lower()

    forbidden = {c.lower() for c in answer.get("forbidden_categories") or []}
    if category is None:
        failures.append("findings have no ROOT_CAUSE_CATEGORY")
    elif category in forbidden:
        failures.append(f"category '{category}' is in forbidden_categories")

    for keyword in answer.get("required_keywords") or []:
        if keyword.lower() not in text:
            failures.append(f"missing required keyword: {keyword}")
    for keyword in answer.get("ruling_out_keywords") or []:
        if keyword.lower() not in text:
            failures.append(f"missing ruling-out keyword: {keyword}")

    for required in answer.get("required_reads") or []:
        if _read_satisfied(required, tool_calls or []):
            continue
        if _content_satisfied((fixture_texts or {}).get(required, ""), corpus):
            continue
        failures.append(f"missing required read in transcript: {required}")

    max_turns = answer.get("max_turns")
    if isinstance(max_turns, int) and num_turns > max_turns:
        failures.append(f"num_turns {num_turns} exceed max {max_turns}")

    for phrase in answer.get("forbidden_phrases") or []:
        if phrase.lower() in text:
            failures.append(f"forbidden phrase present: {phrase!r}")

    if answer.get("require_value_grounding"):
        vg = load_value_grounding()
        for token in vg.ungrounded(findings, corpus):
            failures.append(f"ungrounded value (in no captured evidence): {token.text}")

    return GateResult(passed=not failures, failures=failures, category=category)
