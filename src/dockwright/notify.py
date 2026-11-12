from __future__ import annotations

import os
import subprocess


def macos(title: str, text: str) -> int | None:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    script = f'display notification "{_quote(text)}" with title "{_quote(title)}"'
    try:
        return subprocess.run(["osascript", "-e", script],
                              capture_output=True, timeout=2, check=False).returncode
    except Exception:
        return -1


def _quote(value: str) -> str:
    return value.replace("\\", "").replace('"', "")
