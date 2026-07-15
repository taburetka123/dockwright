#!/usr/bin/env python3
"""Token spend for a gardener analyst run, printed as ledger key=value pairs.

Usage: gardener_spend.py <session_cwd> <run_id>

Resolves the run's transcript under ~/.claude/projects/<munged cwd>/ by the
RUN_ID embedded in the session's first prompt — the cwd is shared with other
sessions (workers, managers), so only a transcript whose HEAD carries the run
id is the analyst session. Prints one line of space-separated key=value pairs
for gardener-run.sh's ledger_append, or nothing when unresolvable. Always
exits 0: spend is observability and must never fail a run.

Standalone by design (deployed to ~/.claude/scripts/ next to gardener-run.sh);
mirrors the claude-shape usage parsing in dockwright/transcript.py —
split assistant events repeat the same message.id and usage, so each id
counts once.

Runs under macOS system python3 (3.9) — gardener-run.sh invokes
/usr/bin/python3, so annotations stay behind `from __future__ import`.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HEAD_BYTES = 262144


def project_dir_name(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def find_run_transcript(projects_root: Path, cwd: str, run_id: str,
                        head_bytes: int = HEAD_BYTES) -> Path | None:
    project_dir = projects_root / project_dir_name(cwd)
    if not project_dir.is_dir():
        return None
    candidates = sorted(project_dir.glob("*.jsonl"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    for candidate in candidates:
        try:
            with open(candidate, "rb") as f:
                head = f.read(head_bytes)
        except OSError:
            continue
        if run_id.encode() in head:
            return candidate
    return None


def _usage_int(usage: dict, key: str) -> int:
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def sum_usage(log_path: Path) -> dict:
    """Whole-run totals, deduped by message id; malformed lines skipped."""
    # Key names match src/dockwright/transcript.py (the canonical
    # spend vocabulary — B2 alignment): out_tokens / in_tokens / cache_read_tokens.
    totals = {"out_tokens": 0, "in_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0}
    seen_ids: set[str] = set()
    try:
        with open(log_path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict) or event.get("type") != "assistant":
                    continue
                message = event.get("message")
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                usage = message.get("usage")
                if (not isinstance(message_id, str) or not message_id
                        or not isinstance(usage, dict) or message_id in seen_ids):
                    continue
                seen_ids.add(message_id)
                totals["out_tokens"] += _usage_int(usage, "output_tokens")
                totals["in_tokens"] += _usage_int(usage, "input_tokens")
                totals["cache_read_tokens"] += _usage_int(usage, "cache_read_input_tokens")
                totals["cache_creation_tokens"] += _usage_int(usage, "cache_creation_input_tokens")
    except OSError:
        pass
    return totals


def main(argv: list) -> int:
    try:
        if len(argv) < 2:
            return 0
        cwd, run_id = argv[0], argv[1]
        projects_root = Path(os.environ.get("HOME", "")) / ".claude" / "projects"
        log = find_run_transcript(projects_root, cwd, run_id)
        if log is None:
            return 0
        totals = sum_usage(log)
        print(" ".join(f"{key}={value}" for key, value in totals.items()))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
