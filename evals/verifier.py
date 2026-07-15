"""Drive dockwright's code-review verifier on a single case, offline.

The verifier this harness reproduces is the production Tier-2 verifier binding
(model: opus), spawned read-only with ``presets/verifier-settings.json`` and
fed a git diff range. This module reproduces that verifier as a single headless
``claude -p`` call:

  * same reviewer prompt (the requesting-code-review template, verbatim core);
  * same read-only guardrail preset (Write/Edit/mutating-git denied);
  * the diff is supplied INLINE instead of via a git range, so a case is a
    self-contained data file rather than a live worktree — the only adaptation
    needed to make the harness re-runnable from committed data.

A small machine-readable JSON tail is appended to the prompt purely so the
verdict can be scored automatically; the review instructions above it are the
production ones.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SETTINGS = os.path.join(
    REPO_ROOT, "deploy", "presets", "verifier-settings.json"
)
# Deployed location the live dockwright install actually points at; preferred if present.
_DEPLOYED_SETTINGS_NEW = os.path.expanduser(
    "~/.claude/dockwright/presets/verifier-settings.json"
)
_DEPLOYED_SETTINGS_LEGACY = os.path.expanduser(
    "~/.claude/orchestrator/presets/verifier-settings.json"
)  # deprecated, one release
DEPLOYED_SETTINGS = (
    _DEPLOYED_SETTINGS_NEW if os.path.exists(_DEPLOYED_SETTINGS_NEW)
    else _DEPLOYED_SETTINGS_LEGACY if os.path.exists(_DEPLOYED_SETTINGS_LEGACY)
    else _DEPLOYED_SETTINGS_NEW
)


def settings_path() -> str:
    return DEPLOYED_SETTINGS if os.path.exists(DEPLOYED_SETTINGS) else DEFAULT_SETTINGS


# Core review instructions lifted from
# skills/requesting-code-review/code-reviewer.md (the production template).
_REVIEW_BODY = """You are a Senior Code Reviewer with expertise in software architecture,
design patterns, and best practices. Your job is to review a completed change
against its stated intent and identify defects before they cascade.

## Stated intent of the change

{intent}

## The change (unified diff)

```diff
{diff}
```

## What to Check

- Correctness: does the change actually do what the intent says? Off-by-one
  errors, wrong boundary/comparison operators, dropped null/None checks,
  broken or swallowed error handling, resource leaks, wrong logical operators.
- Edge cases: empty input, null/None, boundary values, error paths.
- Does it introduce a regression versus the code it replaces?

Be calibrated: do NOT invent problems in correct code. A change that is correct
and idiomatic should pass. Only raise Critical/Important for a real defect that
would produce wrong behavior, a crash, or a security/data-loss risk; reserve
Minor for pure style.

## Output Format

Briefly state the strongest issue you found (or "no blocking issues"), then end
your reply with EXACTLY one fenced json block and nothing after it:

```json
{{"has_blocking_issue": true_or_false, "highest_severity": "critical|important|minor|none", "ready_to_merge": "yes|no|with_fixes"}}
```

Rules for the json:
- has_blocking_issue = true iff there is at least one Critical or Important
  defect (a real bug). A correct change with at most a Minor style nit has
  has_blocking_issue = false.
- ready_to_merge = "yes" only when has_blocking_issue is false.
"""


def build_prompt(case: dict) -> str:
    diff = case["diff"]
    diff_text = "\n".join(diff) if isinstance(diff, list) else str(diff)
    return _REVIEW_BODY.format(intent=case["intent"], diff=diff_text)


class VerifierError(RuntimeError):
    pass


def run_verifier(
    prompt: str,
    *,
    model: str = "sonnet",
    settings: str | None = None,
    timeout: int = 240,
    cwd: str | None = None,
) -> dict:
    """Invoke ``claude -p`` headless and return parsed result metadata.

    Returns a dict with: result_text, cost_usd, duration_ms, usage,
    session_id, is_error, num_turns, permission_denials. Raises VerifierError
    on process failure / timeout / unparseable output so the runner can record
    the failure rather than silently scoring garbage.
    """
    settings = settings or settings_path()
    # Neutral empty cwd so the verifier loads only global ~/.claude context and
    # not whatever project CLAUDE.md happens to sit at the invocation dir —
    # keeps every case's system prompt identical and reproducible.
    own_cwd = cwd is None
    cwd = cwd or tempfile.mkdtemp(prefix="verifier-cwd-")
    cmd = [
        "claude",
        "-p",
        prompt,
        "--model",
        model,
        "--settings",
        settings,
        "--output-format",
        "json",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired as exc:
        raise VerifierError(f"claude -p timed out after {timeout}s") from exc
    finally:
        if own_cwd:
            # rmtree (not rmdir): claude -p may drop a stray file in cwd; rmdir
            # would leak the temp dir in that case.
            shutil.rmtree(cwd, ignore_errors=True)

    if proc.returncode != 0:
        raise VerifierError(
            f"claude -p exited {proc.returncode}: {proc.stderr.strip()[:500]}"
        )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise VerifierError(
            f"could not parse claude -p json output: {proc.stdout[:300]}"
        ) from exc

    return {
        "result_text": payload.get("result", ""),
        "cost_usd": payload.get("total_cost_usd"),
        "duration_ms": payload.get("duration_ms"),
        "usage": payload.get("usage"),
        "session_id": payload.get("session_id"),
        "is_error": payload.get("is_error", False),
        "num_turns": payload.get("num_turns"),
        "permission_denials": payload.get("permission_denials", []),
        "model": model,
    }
