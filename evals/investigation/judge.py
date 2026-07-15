"""LLM judge — grade an investigation's textual findings against a hidden rubric.

A no-tool grading call: a single headless ``claude -p`` run with the read-only
verifier preset and an empty MCP config, from a neutral throwaway cwd. It returns
an integer 0-100 (the LAST integer the model emits, clamped). A broken judge
fails CLOSED: any error — nonzero exit, timeout, unparseable output, no integer —
returns 0, so a scoring failure never masquerades as a passing sample.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile

from evals import verifier

JUDGE_THRESHOLD = 70


def judge_score(
    findings: str,
    rubric: str,
    *,
    model: str = "opus",
    timeout: int = 300,
    runner=subprocess.run,
) -> int:
    prompt = (
        "You are grading an incident-investigation answer against a hidden "
        "rubric. Return ONLY an integer 0-100.\n\n"
        f"RUBRIC:\n{rubric}\n\nFINDINGS:\n{findings}\n\nSCORE:"
    )
    cwd = tempfile.mkdtemp(prefix="inv-judge-cwd-")
    try:
        cmd = [
            "claude", "-p", prompt, "--model", model,
            "--settings", verifier.settings_path(), "--output-format", "json",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        ]
        proc = runner(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        if proc.returncode != 0:
            return 0
        payload = json.loads(proc.stdout)
        ints = re.findall(r"\d+", payload.get("result", ""))
        if not ints:
            return 0
        return max(0, min(100, int(ints[-1])))
    except Exception:
        return 0
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
