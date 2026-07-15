"""Pure scoring + verdict-parsing logic for the verifier eval.

No I/O, no network — everything here is deterministic so it can be unit-tested
in isolation. The verifier itself (the LLM call) lives in ``verifier.py``.

The operational signal we score on is ``has_blocking_issue`` — whether the
verifier raised a Critical/Important finding. That mirrors the orchestrator's
own rule that "Critical or Important findings are merge-blockers" (workflow.md
§ Review discipline); Minor/nit findings are NOT treated as a flag.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

# Matches a fenced json block. `\{.*?\}` assumes the verdict schema stays FLAT
# (three scalar keys) — a nested object value would need matching to the closing
# fence instead. The prompt in verifier.py pins that flat schema.
_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_READY_RE = re.compile(r"ready to merge\??\**:?\**\s*(yes|no|with[ _-]?fixes)", re.IGNORECASE)
_VALID_SEVERITY = {"critical", "important", "minor", "none"}


@dataclass
class Verdict:
    has_blocking_issue: bool
    highest_severity: str
    ready_to_merge: str
    parsed_ok: bool  # True when the machine-readable json tail parsed cleanly
    method: str  # "json" | "heuristic"
    raw: str = ""


def _normalize_ready(value: str) -> str:
    v = value.strip().lower().replace("-", "_").replace(" ", "_")
    return "with_fixes" if v in {"withfixes", "with_fixes"} else v


def parse_verdict(result_text: str) -> Verdict:
    """Extract the verifier's binary judgement.

    Primary path: the last fenced ```json block (the structured tail the prompt
    asks for). Fallback: a keyword heuristic over the prose, so a run that
    forgot the tail is still scored rather than dropped — but flagged
    parsed_ok=False so the harness can report how often the fallback fired.
    """
    text = result_text or ""

    blocks = _JSON_BLOCK_RE.findall(text)
    for block in reversed(blocks):  # last well-formed block wins
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

    # ---- heuristic fallback ----
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
    """The harness's binary classifier output: did the verifier raise a blocker?"""
    return verdict.has_blocking_issue


# --------------------------------------------------------------------- scoring
@dataclass
class Confusion:
    tp: int = 0  # defect, flagged   (caught)
    fp: int = 0  # clean,  flagged   (false alarm)
    tn: int = 0  # clean,  passed    (correctly cleared)
    fn: int = 0  # defect, passed    (escaped)

    def total(self) -> int:
        return self.tp + self.fp + self.tn + self.fn


def classify(label: str, flagged: bool) -> str:
    is_defect = label == "defect"
    if is_defect:
        return "tp" if flagged else "fn"
    return "fp" if flagged else "tn"


def confusion_from(pairs) -> Confusion:
    """pairs: iterable of (label, flagged_bool)."""
    c = Confusion()
    for label, flagged in pairs:
        key = classify(label, flagged)
        setattr(c, key, getattr(c, key) + 1)
    return c


def _safe(num: int, denom: int):
    return None if denom == 0 else round(num / denom, 4)


def metrics(c: Confusion) -> dict:
    return {
        # catch-rate / recall on defects: of all injected defects, how many caught
        "recall": _safe(c.tp, c.tp + c.fn),
        # false-positive rate: of all clean changes, how many wrongly flagged
        "false_positive_rate": _safe(c.fp, c.fp + c.tn),
        # precision: of all flags raised, how many were real defects
        "precision": _safe(c.tp, c.tp + c.fp),
        "accuracy": _safe(c.tp + c.tn, c.total()),
        "tp": c.tp,
        "fp": c.fp,
        "tn": c.tn,
        "fn": c.fn,
        "n": c.total(),
    }


def majority(flags):
    """Return (majority_bool, agreement_fraction) over repeated runs of one case.

    Ties (even number of runs split evenly) break toward True — a verifier that
    flags a real defect even half the time is still surfacing it. agreement is
    the fraction of runs that agreed with the majority verdict.
    """
    flags = list(flags)
    n = len(flags)
    trues = sum(1 for f in flags if f)
    falses = n - trues
    verdict = trues >= falses  # tie -> True
    agreed = trues if verdict else falses
    return verdict, round(agreed / n, 4)
