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
_DEPLOYED_SETTINGS = os.path.expanduser(
    "~/.claude/dockwright/presets/investigation-eval-settings.json"
)


def settings_path() -> str:
    return _DEPLOYED_SETTINGS if os.path.exists(_DEPLOYED_SETTINGS) else DEFAULT_SETTINGS


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
- First read investigate-skill.md in the current working directory and
  follow its discipline.
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


_DEFAULT_INVESTIGATE_SKILL = "~/.claude/skills/investigate/SKILL.md"

WORKDIR_SKILL_NAME = "investigate-skill.md"


def _toml_config() -> dict:
    try:
        from dockwright import config as dw_config
        return dw_config.load()
    except ImportError:
        pass
    try:
        import tomllib
    except ModuleNotFoundError:
        return {}
    home = os.path.expanduser("~")
    env = os.environ.get("DOCKWRIGHT_CONFIG", "").strip()
    if env:
        candidates = [os.path.expanduser(env)]
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
        xdg_base = os.path.expanduser(xdg) if xdg else os.path.join(home, ".config")
        candidates = [os.path.join(xdg_base, "dockwright", "dockwright.toml"),
                      os.path.join(home, ".claude", "dockwright.toml")]
    candidate = next((c for c in candidates if os.path.isfile(c)), None)
    if candidate is None:
        return {}
    try:
        with open(candidate, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def investigate_skill_path() -> str:
    env = os.environ.get("DOCKWRIGHT_INVESTIGATE_SKILL", "").strip()
    if env:
        return os.path.expanduser(env)
    evals_section = _toml_config().get("evals")
    if isinstance(evals_section, dict):
        pinned = evals_section.get("investigate_skill")
        if isinstance(pinned, str) and pinned.strip():
            return os.path.expanduser(pinned.strip())
    return os.path.expanduser(_DEFAULT_INVESTIGATE_SKILL)


def build_prompt(scenario: str) -> str:
    preamble = CONTRACT_PREAMBLE.format(
        FINDINGS_BLOCK_SKELETON=FINDINGS_BLOCK_SKELETON,
    )
    return preamble + "\n\n---\n\n" + scenario


def prepare_workdir(case_dir: str) -> str:
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
        skill_src = investigate_skill_path()
        try:
            shutil.copy2(skill_src, os.path.join(workdir, WORKDIR_SKILL_NAME))
        except OSError:
            return RunRecord(
                case_id=case["case_id"],
                error=f"skill binding unreadable: {skill_src}",
            )
        cmd = [
            "claude", "-p", build_prompt(case["scenario"]), "--model", model,
            "--settings", settings or settings_path(), "--output-format", "json",
            "--setting-sources", "project",
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
