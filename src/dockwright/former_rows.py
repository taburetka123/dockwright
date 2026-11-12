from __future__ import annotations

import tomllib
from pathlib import Path

from . import config
from .pipeline import Lane

LEGACY_FILENAME = "dockwright-roster.toml"
HINT = "rework the work to re-snapshot it on lanes"


def legacy_path() -> Path:
    return config.claude_config_home() / LEGACY_FILENAME


def _claude_table(row: str) -> dict | None:
    path = legacy_path()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    table = ((data.get("roles") or {}).get(row) or {}).get("claude")
    return table if isinstance(table, dict) else None


def _clean(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def lane_for(row: str) -> Lane:
    table = _claude_table(row) or {}
    model, effort = _clean(table.get("model")), _clean(table.get("effort"))
    for key, value in (("model", model), ("effort", effort)):
        if value is None:
            raise ValueError(f"row {row}: the legacy roster at {legacy_path()} has no claude {key} for it; {HINT}")
    from . import roles
    return Lane(name=row, role=_clean(table.get("context")) or roles.DEFAULT_ROLE,
                mode=_clean(table.get("mode")) or "report", model=model, effort=effort, codex=None)


def node_args(row: str) -> list[str]:
    lane = lane_for(row)
    return ["--model", lane.model, "--effort", lane.effort]
