from __future__ import annotations

import json
from pathlib import Path

from . import state

TRUST_KEY = "hasTrustDialogAccepted"


def _default_config_json() -> Path:
    return Path.home() / ".claude.json"


def pretrust_dir(cwd, config_json: Path | None = None) -> bool:
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
