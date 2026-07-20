"""Official project pre-trust: mark a directory trusted in a Claude Code
config JSON (the ~/.claude.json shape) so a spawn there never blocks on the
interactive "Do you trust this folder?" dialog.

Why a file write: interactive trust accepts don't reliably persist across
concurrent sessions, while directly-written flags do (VM E2E 2026-07-16 B2,
both runs — finding L-11). This replaces the manager's hand-written flags
with a product mechanism. The read-modify-replace does race Claude's own
config writes; exposure is one small atomic write on the FIRST spawn into a
cwd (already-trusted is a pure read), the same pattern the E2E validated
against 5 concurrent sessions.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import state

TRUST_KEY = "hasTrustDialogAccepted"


def _default_config_json() -> Path:
    """Seam for tests (conftest redirects it suite-wide); resolved at call
    time, never import time, so a monkeypatched HOME takes effect.
    Path.home() (not os.environ["HOME"] directly) so an unset HOME can't
    silently resolve to a relative ./.claude.json that pretrust_dir would
    then CREATE in the cwd; it still honors a monkeypatched HOME on POSIX."""
    return Path.home() / ".claude.json"


def pretrust_dir(cwd, config_json: Path | None = None) -> bool:
    """Best-effort: ensure projects[<abs cwd>].hasTrustDialogAccepted is true.

    True = the entry is present after the call (written now, or already
    there). Never raises. A corrupt or non-dict-shaped file is left
    untouched — never clobber Claude's own state to plant a flag.
    """
    target = config_json or _default_config_json()
    key = str(Path(cwd).expanduser().resolve())
    try:
        try:
            data = json.loads(target.read_text())
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            return False
        entry = projects.get(key)
        if isinstance(entry, dict) and entry.get(TRUST_KEY) is True:
            return True
        if isinstance(entry, dict):
            entry[TRUST_KEY] = True
        else:
            projects[key] = {TRUST_KEY: True}
        state.write_json_atomic(target, data)
        return True
    except Exception:
        return False
