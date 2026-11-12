from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import config

NONE = "none"


def repo_name(cwd: Path) -> str:
    try:
        top = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True)
    except OSError:
        return cwd.name
    if top.returncode == 0 and top.stdout.strip():
        return Path(top.stdout.strip()).name
    return cwd.name


def origin_owner(cwd: Path) -> str:
    try:
        url = subprocess.run(["git", "-C", str(cwd), "remote", "get-url", "origin"], capture_output=True, text=True)
    except OSError:
        return ""
    if url.returncode != 0:
        return ""
    tail = url.stdout.strip().replace(":", "/").rstrip("/")
    parts = tail.split("/")
    return parts[-2] if len(parts) >= 2 else ""


def profile_for(cwd: str | Path | None, settings: dict | None = None) -> str:
    settings = config.profiles() if settings is None else settings
    sets = settings.get("sets", {})
    if not sets:
        return NONE
    path = Path(cwd or os.getcwd()).expanduser().resolve()
    named = settings.get("repos", {}).get(repo_name(path))
    if named in sets:
        return named
    if origin_owner(path) in settings.get("personal_owners", []):
        return NONE
    for root in settings.get("work_roots", []):
        root_path = Path(root).expanduser().resolve()
        if path == root_path or root_path in path.parents:
            default = settings.get("work_default", NONE)
            return default if default in sets else NONE
    return NONE


def profile_units(units: dict, profile: str, sets: dict[str, list[str]]) -> list:
    domains = list(sets.get(profile, []))
    picked = [u for u in units.values() if u.home.startswith("profile:") and u.domain in domains]
    return sorted(picked, key=lambda u: (domains.index(u.domain), u.name))


def profile_text(profile: str, units: list) -> str:
    from .roles import profile_mark
    parts = [u.body.strip("\n") for u in units if u.body.strip()]
    return profile_mark(profile) + "\n\n" + "\n\n".join(parts) + "\n"
