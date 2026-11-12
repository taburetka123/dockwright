from __future__ import annotations

import json
import os
import re
import subprocess
import time

from . import config

_OWNER = re.compile(r"^https?://github\.com/([^/\s]+)/[^/\s]+/")
_TOKENS: dict[str, tuple[str, float]] = {}
_TOKEN_ENV = ("GH_TOKEN", "GITHUB_TOKEN")


def owner_of(url: str) -> str | None:
    match = _OWNER.match(str(url))
    return match.group(1) if match else None


def _token(login: str) -> str:
    cached = _TOKENS.get(login)
    now = time.time()
    if cached is not None and now - cached[1] < config.gh_token_ttl_sec():
        return cached[0]
    try:
        out = subprocess.run([config.gh_binary(), "auth", "token", "-h", "github.com", "-u", login],
                             capture_output=True, text=True, timeout=10,
                             env={k: v for k, v in os.environ.items() if k not in _TOKEN_ENV})
    except (OSError, subprocess.TimeoutExpired) as err:
        raise ValueError(f"gh could not run: {err}") from err
    token = (out.stdout or "").strip()
    if out.returncode != 0 or not token:
        raise ValueError(f"gh has no token for login {login}")
    _TOKENS[login] = (token, now)
    return token


def env_for_owner(owner: str | None) -> dict | None:
    if not config.gh_configured():
        return None
    login = config.gh_logins().get(owner) if owner else None
    if not login:
        raise ValueError(f"no gh login configured for owner {owner}")
    return {**{k: v for k, v in os.environ.items() if k not in _TOKEN_ENV}, "GH_TOKEN": _token(login)}


def env_for(url: str) -> dict | None:
    return env_for_owner(owner_of(url))


def run(url: str, argv: list[str], cwd: str | None = None, timeout: int = 30) -> subprocess.CompletedProcess:
    env = env_for(url)
    if config.gh_configured():
        cwd = None
    elif not (cwd and os.path.isdir(cwd)):
        cwd = None
    return subprocess.run([config.gh_binary(), *argv], capture_output=True, text=True, timeout=timeout, cwd=cwd,
                          env=env)


def run_text(argv: list[str], cwd: str | None = None) -> str | None:
    if len(argv) < 3 or argv[:2] != ["pr", "view"]:
        raise ValueError("gh_cli.run_text needs ['pr', 'view', <url>, ...]")
    try:
        out = run(argv[2], argv, cwd)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return out.stdout if out.returncode == 0 else None


def run_json(owner: str | None, argv: list[str], timeout: int = 30):
    try:
        env = env_for_owner(owner)
        out = subprocess.run([config.gh_binary(), *argv], capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def pr_json(url: str, fields: str, cwd: str | None = None) -> dict:
    try:
        out = run(url, ["pr", "view", url, "--json", fields], cwd)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise ValueError(f"gh could not run: {err}") from err
    if out.returncode != 0:
        raise ValueError(f"gh exited {out.returncode}: {(out.stderr or '').strip()[:200]}")
    try:
        data = json.loads(out.stdout)
    except ValueError as err:
        raise ValueError(f"gh printed no JSON: {err}") from err
    return data if isinstance(data, dict) else {}
