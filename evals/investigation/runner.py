"""Drive one investigation case through a headless ``claude -p`` worker, offline.

A case is a self-contained directory (``scenario.md`` + ``fixtures/`` +
``case.json`` + ``answer.json``). The worker is spawned with a read-only preset
and an empty MCP config into a throwaway workdir that holds ONLY the scenario
and fixtures — never the answer key — so the model's evidence surface is exactly
the fixture excerpts. After the run we recover the session transcripts via
``value_grounding`` to reconstruct the tool-call trace (for the required-reads /
value-grounding gates) alongside the model's textual findings.

An errored run (nonzero exit, timeout, unparseable output) is a failed sample,
not a crashed suite: ``run_case`` never raises — it returns a ``RunRecord`` with
``error`` set and the remaining fields defaulted.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

from evals.investigation import gates

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
DEFAULT_SETTINGS = os.path.join(
    REPO_ROOT, "deploy", "presets", "investigation-eval-settings.json"
)
# Deployed location the live dockwright install actually points at; preferred if present.
_DEPLOYED_SETTINGS = os.path.expanduser(
    "~/.claude/dockwright/presets/investigation-eval-settings.json"
)


def settings_path() -> str:
    return _DEPLOYED_SETTINGS if os.path.exists(_DEPLOYED_SETTINGS) else DEFAULT_SETTINGS


# Verbatim from deploy/agents/worker.core.md (the investigation findings block).
# A test asserts this matches that source; do not edit one without the other.
FINDINGS_BLOCK_SKELETON = (
    'ROOT_CAUSE: <one line, or "none">\n'
    "ROOT_CAUSE_CATEGORY: code_defect | data_state_gap | race_or_replay | "
    "upstream_invariant_broken | deployment_regression | external_dependency | "
    "resource_exhaustion | database_contention | configuration_error | "
    "security_abuse | noise_no_incident | recovered | insufficient_evidence\n"
    "VALIDATED_CLAIMS: <fact [tool/source]> — one per line, only facts backed "
    "by a tool output read this session\n"
    "NON_VALIDATED_CLAIMS: <hypothesis + why unproven> — one per line\n"
    'CAUSAL_CHAIN: <A → B → C, or "n/a">\n'
    'RECOMMENDED_ACTIONS: <ranked, or "none">'
)

CONTRACT_PREAMBLE = """You are an investigation-class worker being evaluated offline.

Rules of engagement:
- Investigate using ONLY the fixture files in the current working directory
  (they are excerpts of real tool outputs: logs, query results, schema dumps,
  git history). No network access, no MCP tools. Read/Grep/Bash over these
  files is your entire evidence surface.
- Cite only evidence from the fixture files in this directory; do not reference
  incidents, tickets, or values from your background knowledge or ambient rules
  — they are not evidence here.
- First read {SKILL_PATH} and follow
  its discipline (hypotheses + falsifiers, evidence fidelity, stop block).
- Answer in English.
- End your reply with the structured findings block, verdict line first:

{FINDINGS_BLOCK_SKELETON}
"""


@dataclass
class RunRecord:
    case_id: str
    findings: str = ""
    tool_calls: list = field(default_factory=list)
    corpus: str = ""
    num_turns: int = 0
    session_id: str = ""
    cost_usd: float | None = None
    duration_ms: int | None = None
    transcript_missing: bool = False
    error: str | None = None


def load_case(case_dir: str) -> dict:
    with open(os.path.join(case_dir, "scenario.md"), encoding="utf-8") as fh:
        scenario = fh.read()
    with open(os.path.join(case_dir, "case.json"), encoding="utf-8") as fh:
        meta = json.load(fh)
    with open(os.path.join(case_dir, "answer.json"), encoding="utf-8") as fh:
        answer = json.load(fh)
    return {
        "case_id": meta.get("case_id") or os.path.basename(case_dir.rstrip("/")),
        "scenario": scenario,
        "answer": answer,
        "meta": meta,
        "case_dir": case_dir,
    }


# The investigation-discipline skill the evaluated worker is told to read.
# Operator installs point this at their own skill via the env override.
_DEFAULT_INVESTIGATE_SKILL = "~/.claude/skills/investigate/SKILL.md"


def investigate_skill_path() -> str:
    return os.path.expanduser(
        os.environ.get("DOCKWRIGHT_INVESTIGATE_SKILL", _DEFAULT_INVESTIGATE_SKILL)
    )


def build_prompt(scenario: str) -> str:
    preamble = CONTRACT_PREAMBLE.format(
        SKILL_PATH=investigate_skill_path(),
        FINDINGS_BLOCK_SKELETON=FINDINGS_BLOCK_SKELETON,
    )
    return preamble + "\n\n---\n\n" + scenario


def prepare_workdir(case_dir: str) -> str:
    """Copy scenario.md + fixtures/ (NOT case.json/answer.json) into a temp dir."""
    workdir = tempfile.mkdtemp(prefix="inv-eval-")
    shutil.copy2(
        os.path.join(case_dir, "scenario.md"),
        os.path.join(workdir, "scenario.md"),
    )
    shutil.copytree(
        os.path.join(case_dir, "fixtures"),
        os.path.join(workdir, "fixtures"),
    )
    return workdir


def run_case(
    case: dict,
    *,
    model: str,
    timeout: int,
    settings: str | None = None,
    runner=subprocess.run,
) -> RunRecord:
    workdir = prepare_workdir(case["case_dir"])
    try:
        cmd = [
            "claude", "-p", build_prompt(case["scenario"]), "--model", model,
            "--settings", settings or settings_path(), "--output-format", "json",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        ]
        try:
            proc = runner(
                cmd, capture_output=True, text=True, timeout=timeout, cwd=workdir
            )
        except subprocess.TimeoutExpired:
            return RunRecord(case_id=case["case_id"], error=f"timeout after {timeout}s")
        if proc.returncode != 0:
            return RunRecord(
                case_id=case["case_id"],
                error=f"claude -p exited {proc.returncode}: {proc.stderr[:300]}",
            )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return RunRecord(case_id=case["case_id"], error="unparseable claude -p output")
        vg = gates.load_value_grounding()
        sid = payload.get("session_id", "")
        transcripts = vg.find_session_transcripts(sid) if sid else []
        tool_calls, corpus = vg.parse_transcripts(transcripts) if transcripts else ([], "")
        return RunRecord(
            case_id=case["case_id"], findings=payload.get("result", ""),
            tool_calls=tool_calls, corpus=corpus, num_turns=payload.get("num_turns", 0),
            session_id=sid, cost_usd=payload.get("total_cost_usd"),
            duration_ms=payload.get("duration_ms"),
            transcript_missing=not transcripts, error=None,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
