"""Manager-session memory distill: slim the transcript, run `claude -p`, persist.

Shared by the MCP server (prepare_handoff / close_manager_self) and the
SessionEnd hook fallback (hooks._maybe_distill_on_session_end). Must stay free
of FastMCP — it sits on the every-session hook path.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from typing import Any

from . import config, paths, state
from .transcript import find_session_log


_DISTILL_PROMPT = (
    "Distill this Claude Code manager session transcript into a journal entry "
    "for a successor manager. Format: markdown. Sections: Decisions (what we "
    "settled on and why), User direction changes (where the user redirected "
    "mid-task), Shipped (commits with SHAs from worker_done events I saw), "
    "Open threads (unfinished discussions or pending dispatches). Be concrete; "
    "skip pleasantries and tool-call mechanics; preserve the user's verbatim "
    "phrasings on contentious points. Aim for ≤80 lines. "
    "Output ONLY the journal markdown to stdout — no preamble, no sign-off, no "
    "surrounding code fence. Do NOT call Write, Edit, or any tool, and do NOT "
    "create a memory file or update MEMORY.md: the caller captures your stdout "
    "verbatim and persists it. Cite a commit SHA or file:line only if it "
    "appears verbatim in the transcript — never infer, complete, or reconstruct "
    "one; omit it or write `[SHA not in transcript]` instead."
)

# Raw transcripts can be MBs of tool_use inputs + tool_result outputs, which
# overflow `claude -p`'s prompt limit. We strip those and keep only the
# semantic content: user text, assistant text, and tool_use markers.
# 500KB is well below `claude -p`'s prompt cap with headroom for the
# distill prompt itself; 180s is generous given typical distill latency is
# 10-30s, but a slow API round-trip on a near-cap input shouldn't fail.
_DISTILL_MAX_INPUT_BYTES = 500_000
_DISTILL_TIMEOUT_SECONDS = 180


def _extract_tool_result_text(tr_content: Any) -> str:
    """Pull plain text out of a tool_result.content (str or list of blocks).

    Worker_done summaries and other small text payloads sometimes arrive as
    list-shaped tool_result content with `[{type: 'text', text: '...'}]`.
    Returns "" if no plain-text content found.
    """
    if isinstance(tr_content, str):
        return ""
    if not isinstance(tr_content, list):
        return ""
    parts = [
        b.get("text", "")
        for b in tr_content
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    return "\n".join(p for p in parts if p)


def _slim_transcript(raw: bytes, max_bytes: int = _DISTILL_MAX_INPUT_BYTES) -> bytes:
    """Reduce a JSONL transcript to user/assistant text + tool_use names.

    Drops tool_use inputs and the bulk of tool_result content (the size).
    Preserves: user text, assistant text, tool_use names, and any plain-text
    inside list-shaped tool_results (where worker_done summaries arrive).
    Drops: `thinking` blocks — their conclusion lives in the following text
    block which we keep; loss is "why we decided X" inner reasoning, which
    is acceptable for a successor-manager journal.

    If still over max_bytes after slimming, keeps the FIRST 30% (early
    decisions + original user direction — the distill prompt asks for
    those) plus the LAST 70% (recent activity + open threads), with a
    `[transcript middle truncated]` marker between them.
    """
    slim_lines: list[str] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        message = event.get("message") or {}
        content = message.get("content")
        if etype == "user":
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts: list[str] = []
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "text":
                        parts.append(c.get("text", ""))
                    elif c.get("type") == "tool_result":
                        inner = _extract_tool_result_text(c.get("content"))
                        parts.append(inner if inner else "[tool_result elided]")
                text = "\n".join(p for p in parts if p)
            if text.strip():
                slim_lines.append(f"USER: {text}")
        elif etype == "assistant":
            if not isinstance(content, list):
                continue
            parts = []
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text":
                    parts.append(c.get("text", ""))
                elif c.get("type") == "tool_use":
                    parts.append(f"[tool_use: {c.get('name', '?')}]")
            text = "\n".join(p for p in parts if p)
            if text.strip():
                slim_lines.append(f"ASSISTANT: {text}")

    slim = "\n\n".join(slim_lines).encode("utf-8")
    if len(slim) > max_bytes:
        marker = b"\n\n[transcript middle truncated]\n\n"
        budget = max_bytes - len(marker)
        head_budget = budget * 3 // 10
        tail_budget = budget - head_budget
        slim = slim[:head_budget] + marker + slim[-tail_budget:]
    return slim


def _distill_manager_session(claude_sid: str) -> str | None:
    """Run `claude -p` over a slimmed manager transcript; return distilled markdown.

    Best-effort: any failure (missing transcript, subprocess error, timeout, empty
    stdout) returns None. Caller logs to stderr but never raises — the handoff
    record write already succeeded by the time this is invoked.
    """
    log_path = find_session_log(claude_sid)
    if log_path is None:
        print(f"manager-memory: no transcript found for {claude_sid}; skipping distill", file=sys.stderr)
        return None
    try:
        transcript_bytes = log_path.read_bytes()
    except OSError as e:
        print(f"manager-memory: could not read transcript {log_path}: {e}", file=sys.stderr)
        return None
    slimmed = _slim_transcript(transcript_bytes)
    claude_bin = shutil.which("claude") or "claude"
    # Strip the orchestrator's own session env so the headless child's
    # SessionStart/SessionEnd hooks don't treat it as a manager (which would
    # register a phantom manager record and re-distill on exit — infinite
    # `claude -p` fan-out). The sentinel makes the hooks skip it outright.
    distill_env = {k: v for k, v in os.environ.items() if k not in paths.ORCHESTRATOR_ENV_KEYS}
    distill_env[paths.DISTILL_ENV_SENTINEL] = "1"
    distill_env["CLAUDE_SPEND_CLASS"] = "distill"
    started_at = time.monotonic()
    try:
        result = subprocess.run(
            [
                claude_bin, "-p", _DISTILL_PROMPT,
                "--model", config.distill_model(),
                "--effort", "high",
                "--output-format", "text",
                "--disallowedTools", "Write,Edit,NotebookEdit",
            ],
            input=slimmed,
            capture_output=True,
            timeout=_DISTILL_TIMEOUT_SECONDS,
            check=False,
            env=distill_env,
        )
    except FileNotFoundError:
        print(f"manager-memory: `claude` CLI not found (tried {claude_bin}); skipping distill", file=sys.stderr)
        return None
    except subprocess.TimeoutExpired:
        print(
            f"manager-memory: claude -p timed out after {_DISTILL_TIMEOUT_SECONDS}s "
            f"for {claude_sid} (input {len(slimmed)} bytes)",
            file=sys.stderr,
        )
        return None
    except Exception as e:
        print(f"manager-memory: claude -p failed for {claude_sid}: {e}", file=sys.stderr)
        return None
    if result.returncode != 0:
        # `claude -p` writes some failure messages (e.g. "Prompt is too long") to
        # stdout, not stderr — log both so future incidents are diagnosable.
        stdout_excerpt = (result.stdout or b"")[:300].decode("utf-8", errors="replace")
        stderr_excerpt = (result.stderr or b"")[:300].decode("utf-8", errors="replace")
        print(
            f"manager-memory: claude -p exit {result.returncode} for {claude_sid} "
            f"(input {len(slimmed)} bytes); stdout={stdout_excerpt!r} stderr={stderr_excerpt!r}",
            file=sys.stderr,
        )
        return None
    out = (result.stdout or b"").decode("utf-8", errors="replace").strip()
    if not out:
        print(f"manager-memory: claude -p produced empty output for {claude_sid}", file=sys.stderr)
        return None
    elapsed = time.monotonic() - started_at
    print(
        f"manager-memory: distilled {claude_sid} in {elapsed:.1f}s "
        f"(input {len(slimmed)} bytes, output {len(out)} bytes) via {claude_bin}",
        file=sys.stderr,
    )
    return out


def _write_memory_file_atomic(domain: str, claude_sid: str, distilled: str) -> str | None:
    """Persist a distilled session to manager-memory/<domain>/<date>-<sid>.md.

    Writes to `<file>.tmp` first then atomically renames, so a SIGKILL mid-write
    can't leave a half-written final path. Returns the final path on success,
    None on OSError.
    """
    domain = domain or paths.DEFAULT_DOMAIN
    date_str = datetime.now().strftime("%Y-%m-%d")
    domain_dir = paths.manager_memory_domain_dir(domain)
    memory_file = domain_dir / f"{date_str}-{claude_sid}.md"
    tmp_file = memory_file.with_suffix(".md.tmp")
    try:
        domain_dir.mkdir(parents=True, exist_ok=True)
        tmp_file.write_text(distilled)
        os.replace(tmp_file, memory_file)
        print(
            f"manager-memory: wrote {len(distilled)} bytes to {memory_file}",
            file=sys.stderr,
        )
        return str(memory_file)
    except OSError as e:
        print(f"manager-memory: could not write {memory_file}: {e}", file=sys.stderr)
        return None


def distill_and_write_memory(claude_sid: str, domain: str | None = None) -> str | None:
    """Distill the manager's transcript and persist to the per-domain memory dir.

    Used by both `prepare_handoff_impl` (recreation) and `close_manager_self_impl`
    (manual /manager-close) and the SessionEnd fallback hook. Returns the written
    path or None on any failure (no distill, write error, etc.).

    `domain` defaults to the live active record's domain, then DEFAULT_DOMAIN.
    """
    if domain is None:
        record = state.read_json(paths.ACTIVE / f"{claude_sid}.json")
        domain = (record or {}).get("domain") or paths.DEFAULT_DOMAIN
    distilled = _distill_manager_session(claude_sid)
    if distilled is None:
        return None
    return _write_memory_file_atomic(domain, claude_sid, distilled)


def main(argv: list[str]) -> int:
    """CLI: `dockwright distill <sid> [--domain <domain>]`.

    Lets a SUCCESSOR session distill a bricked predecessor's transcript: the
    `claude -p` child inherits the caller's env, so run from the recovery
    manager it bills the healthy account (the predecessor's own SessionEnd
    distill died on the bricked one — the 2026-06-11 lost-memory bug).
    """
    import argparse
    parser = argparse.ArgumentParser(prog="dockwright distill",
                                     description="Distill a manager session transcript to manager-memory.")
    parser.add_argument("sid", help="session id whose transcript to distill")
    parser.add_argument("--domain", default=None,
                        help="manager-memory domain (default: the session's active-record domain)")
    args = parser.parse_args(argv)
    written = distill_and_write_memory(args.sid, domain=args.domain)
    if written is None:
        return 1
    print(written)
    return 0
