#!/usr/bin/env python3
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
