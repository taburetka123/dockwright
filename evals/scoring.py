from __future__ import annotations

import json
import re
from dataclasses import dataclass

_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_READY_RE = re.compile(r"ready to merge\??\**:?\**\s*(yes|no|with[ _-]?fixes)", re.IGNORECASE)
_VALID_SEVERITY = {"critical", "important", "minor", "none"}


@dataclass
class Verdict:
    has_blocking_issue: bool
    highest_severity: str
    ready_to_merge: str
    parsed_ok: bool
    method: str
    raw: str = ""


def _normalize_ready(value: str) -> str:
    v = value.strip().lower().replace("-", "_").replace(" ", "_")
    return "with_fixes" if v in {"withfixes", "with_fixes"} else v


def parse_verdict(result_text: str) -> Verdict:
    text = result_text or ""

    blocks = _JSON_BLOCK_RE.findall(text)
    for block in reversed(blocks):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if "has_blocking_issue" not in data:
            continue
        severity = str(data.get("highest_severity", "")).lower()
        return Verdict(
            has_blocking_issue=bool(data["has_blocking_issue"]),
            highest_severity=severity if severity in _VALID_SEVERITY else "none",
            ready_to_merge=_normalize_ready(str(data.get("ready_to_merge", ""))),
            parsed_ok=True,
            method="json",
            raw=text,
        )

    ready_match = _READY_RE.search(text)
    ready = _normalize_ready(ready_match.group(1)) if ready_match else ""
    lower = text.lower()
    has_critical = "#### critical" in lower or "critical (must fix)" in lower
    has_important = "#### important" in lower or "important (should fix)" in lower
    blocking = ready in {"no", "with_fixes"} or has_critical or has_important
    severity = "critical" if has_critical else "important" if has_important else "none"
    return Verdict(
        has_blocking_issue=blocking,
        highest_severity=severity,
        ready_to_merge=ready,
        parsed_ok=False,
        method="heuristic",
        raw=text,
    )


def flagged_defective(verdict: Verdict) -> bool:
    return verdict.has_blocking_issue


@dataclass
class Confusion:
    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0

    def total(self) -> int:
        return self.tp + self.fp + self.tn + self.fn


def classify(label: str, flagged: bool) -> str:
    is_defect = label == "defect"
    if is_defect:
        return "tp" if flagged else "fn"
    return "fp" if flagged else "tn"


def confusion_from(pairs) -> Confusion:
    c = Confusion()
    for label, flagged in pairs:
        key = classify(label, flagged)
        setattr(c, key, getattr(c, key) + 1)
    return c


def _safe(num: int, denom: int):
    return None if denom == 0 else round(num / denom, 4)


def metrics(c: Confusion) -> dict:
    return {
        "recall": _safe(c.tp, c.tp + c.fn),
        "false_positive_rate": _safe(c.fp, c.fp + c.tn),
        "precision": _safe(c.tp, c.tp + c.fp),
        "accuracy": _safe(c.tp + c.tn, c.total()),
        "tp": c.tp,
        "fp": c.fp,
        "tn": c.tn,
        "fn": c.fn,
        "n": c.total(),
    }


def majority(flags):
    flags = list(flags)
    n = len(flags)
    trues = sum(1 for f in flags if f)
    falses = n - trues
    verdict = trues >= falses
    agreed = trues if verdict else falses
    return verdict, round(agreed / n, 4)
