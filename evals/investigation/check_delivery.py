"""Per-sample delivery check for injection runs (eval-direction Part B).

A green/red injection verdict is meaningless unless delivery is proven — an
unread scratch skill tests nothing. Delivery is CONTENT-based: for each sample
in a trace file, resolve its session transcript and check whether the sample's
captured evidence corpus (tool RESULTS + user text, per ``value_grounding.
parse_transcripts``) contains distinctive verbatim content of the bound skill.

Why content, not path (A2c): the predecessor matched the skill path against
tool-call INPUTS, so a Read that the sandbox DENIED still scored DELIVERED —
which is exactly what happened once the SUT session went hermetic and the
out-of-cwd read started failing. A mere path mention in a tool call never
counts as delivery; only the skill's own text appearing in what came BACK
does. Because the runner now copies the bound skill into the SUT workdir, the
bound path is not even stable — the content is.

    python -m evals.investigation.check_delivery \
        --trace evals/investigation/traces/<run>.jsonl \
        --skill <bound-skill-path> [--expect read|not-read] [--config-dir DIR]

Exit 0: every sample matches --expect (default read). Exit 1: any mismatch.
Exit 2: any sample's transcript is missing (indeterminate — a missing
transcript never counts as a match in either direction), or --skill is
unreadable / carries no probe-worthy line (an instrument that cannot detect
delivery must say so, not verdict every sample NOT-READ).
"""
from __future__ import annotations

import argparse
import json
import sys

from evals.investigation import gates

PROBE_MIN_LEN = 40
PROBE_COUNT = 5


def content_probes(skill_text: str) -> list[str]:
    seen, lines = set(), []
    for raw in skill_text.splitlines():
        line = raw.strip()
        if len(line) >= PROBE_MIN_LEN and line not in seen:
            seen.add(line)
            lines.append(line)
    return sorted(lines, key=len, reverse=True)[:PROBE_COUNT]


def sample_ingested_skill(corpus: str, skill_text: str) -> bool:
    return any(probe in (corpus or "") for probe in content_probes(skill_text))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trace", required=True)
    ap.add_argument("--skill", required=True)
    ap.add_argument("--expect", choices=("read", "not-read"), default="read")
    ap.add_argument("--config-dir", action="append", default=None,
                    help="transcript root(s) (default: value_grounding's)")
    args = ap.parse_args(argv)

    try:
        with open(args.skill, encoding="utf-8") as fh:
            skill_text = fh.read()
    except OSError as exc:
        print(f"check_delivery: cannot read --skill {args.skill}: {exc}",
              file=sys.stderr)
        return 2
    if not content_probes(skill_text):
        print(f"check_delivery: --skill {args.skill} has no line of "
              f">={PROBE_MIN_LEN} chars — no content probe to prove delivery "
              "with; delivery is indeterminate for this binding",
              file=sys.stderr)
        return 2

    vg = gates.load_value_grounding()
    rows, missing, matches = [], 0, 0
    with open(args.trace, encoding="utf-8") as fh:
        samples = [json.loads(l) for l in fh if l.strip()]
    for smp in samples:
        sid = smp.get("session_id") or ""
        transcripts = vg.find_session_transcripts(sid, args.config_dir) if sid else []
        if not transcripts:
            rows.append((smp.get("case_id"), sid, "MISSING-TRANSCRIPT"))
            missing += 1
            continue
        _tool_calls, corpus = vg.parse_transcripts(transcripts)
        ingested = sample_ingested_skill(corpus, skill_text)
        status = "DELIVERED" if ingested else "NOT-READ"
        ok = (status == "DELIVERED") == (args.expect == "read")
        matches += ok
        rows.append((smp.get("case_id"), sid, status))
    for case_id, sid, status in rows:
        print(f"  {case_id}  {sid}  {status}")
    checkable = len(samples) - missing
    print(f"check_delivery: {matches}/{checkable} samples match "
          f"--expect {args.expect}; {missing} missing transcript(s)")
    if missing:
        return 2
    return 0 if matches == checkable else 1


if __name__ == "__main__":
    raise SystemExit(main())
