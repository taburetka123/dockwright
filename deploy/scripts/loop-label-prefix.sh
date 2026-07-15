#!/usr/bin/env bash
# loop-label-prefix.sh — resolve this operator's launchd label-prefix for
# background loops ([loops].label_prefix in dockwright.toml; default
# "com.dockwright" — src/dockwright/config.py DEFAULT_LOOP_LABEL_PREFIX).
#
# Sourced by deploy/scripts/*-install.sh. setup.sh cp's this file into
# ~/.claude/scripts/ alongside the install scripts (same flat directory as
# the repo checkout's deploy/scripts/), so the relative lookup below resolves
# whether an install script runs from the repo checkout or from its deployed
# copy.
#
# Two tiers:
#   1. Import the real config.py (repo checkout + its venv, found two levels
#      up from this file) — the authoritative resolution, including every
#      discovery-order / tilde-expansion edge case config.py itself handles.
#   2. Self-contained fallback: re-read the SAME operator config file
#      directly (env DOCKWRIGHT_CONFIG -> XDG_CONFIG_HOME/dockwright ->
#      ~/.claude/dockwright.toml), needing no package import at all. This is
#      what makes a deployed-copy (re)install — no adjacent repo checkout —
#      still honour an operator override (e.g. this operator's
#      ~/.claude/dockwright.toml [loops].label_prefix = "com.dockwright").
# Both tiers default to "com.dockwright" on any failure (fail-open, the same
# contract config.py itself guarantees).
#
# CAVEAT: changing label_prefix between installs of the SAME loop makes the
# NEXT (re)install create a plist under the NEW label — the old one is not
# removed automatically. Bootout + rm the old plist by hand (see each
# install script's own "Uninstall" comment) when switching prefixes.

dockwright_loop_label_prefix() {
  local script_dir repo_root prefix py
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  repo_root="$(cd "$script_dir/../.." 2>/dev/null && pwd || true)"

  if [ -n "$repo_root" ] && [ -f "$repo_root/src/dockwright/config.py" ]; then
    py="$repo_root/.venv/bin/python"
    [ -x "$py" ] || py="python3"
    prefix="$("$py" -c "
import sys
sys.path.insert(0, '$repo_root/src')
from dockwright import config
print(config.loop_label_prefix())
" 2>/dev/null)" || prefix=""
    if [ -n "$prefix" ]; then
      echo "$prefix"
      return
    fi
  fi

  prefix="$(python3 - <<'PYEOF' 2>/dev/null
import os
import pathlib


def _expand(raw):
    return pathlib.Path(raw).expanduser()


env = os.environ.get("DOCKWRIGHT_CONFIG", "").strip()
if env:
    candidates = [_expand(env)]
else:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    xdg_base = _expand(xdg) if xdg else pathlib.Path.home() / ".config"
    candidates = [xdg_base / "dockwright" / "dockwright.toml",
                  pathlib.Path.home() / ".claude" / "dockwright.toml"]

for candidate in candidates:
    if candidate.is_file():
        try:
            import tomllib
            with open(candidate, "rb") as fh:
                data = tomllib.load(fh)
            value = data.get("loops", {}).get("label_prefix")
            if isinstance(value, str) and value:
                print(value)
        except Exception:
            pass
        break
PYEOF
)" || prefix=""
  echo "${prefix:-com.dockwright}"
}

# _dockwright_toml_get <section> <key> <kind> — echo the dockwright.toml value
# at [section] key, resolved through the SAME discovery order config.py uses
# (env DOCKWRIGHT_CONFIG -> XDG_CONFIG_HOME/dockwright -> ~/.claude/...).
# <kind> shapes the output: bool -> "true"/"false"; path -> the ~-expanded
# string; list -> one item per line; str (default) -> the raw string. Prints
# NOTHING when the key is unset/absent so every caller applies its own default.
#
# tomllib is used when the interpreter has it (py3.11+); a minimal scanner is
# the fallback for the py3.9 /usr/bin/python3 some launchd / SessionEnd-hook
# contexts resolve `python3` to (no tomllib there). The scanner covers only the
# simple scalar / inline-list shapes dockwright.toml uses — it is NOT a general
# TOML parser. Fail-open: any error prints nothing.
_dockwright_toml_get() {
  DOCKWRIGHT_Q_SECTION="$1" DOCKWRIGHT_Q_KEY="$2" DOCKWRIGHT_Q_KIND="${3:-str}" \
  python3 - <<'PYEOF' 2>/dev/null
import os
import pathlib


def _expand(raw):
    return pathlib.Path(raw).expanduser()


def _config_file():
    env = os.environ.get("DOCKWRIGHT_CONFIG", "").strip()
    if env:
        p = _expand(env)
        return p if p.is_file() else None
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = _expand(xdg) if xdg else pathlib.Path.home() / ".config"
    for candidate in (base / "dockwright" / "dockwright.toml",
                      pathlib.Path.home() / ".claude" / "dockwright.toml"):
        if candidate.is_file():
            return candidate
    return None


def _scan(text, section, key):
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1].strip()
            continue
        if cur != section or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() != key:
            continue
        v = v.strip()
        if v[:1] == "[":
            inner = v[1:v.rfind("]")] if "]" in v else v[1:]
            out = []
            for part in inner.split(","):
                p = part.strip()
                if len(p) >= 2 and p[0] == p[-1] and p[0] in ("'", '"'):
                    out.append(p[1:-1])
                elif p:
                    out.append(p)
            return out
        if v[:1] in ("'", '"'):
            q = v[0]
            end = v.find(q, 1)
            return v[1:end] if end != -1 else v.strip(q)
        v = v.split("#", 1)[0].strip()
        if v == "true":
            return True
        if v == "false":
            return False
        return v or None
    return None


section = os.environ.get("DOCKWRIGHT_Q_SECTION", "")
key = os.environ.get("DOCKWRIGHT_Q_KEY", "")
kind = os.environ.get("DOCKWRIGHT_Q_KIND", "str")
path = _config_file()
value = None
if path is not None:
    try:
        import tomllib
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        value = data.get(section, {}).get(key)
    except ModuleNotFoundError:
        try:
            value = _scan(path.read_text(), section, key)
        except OSError:
            value = None
    except Exception:
        value = None

if kind == "bool":
    if isinstance(value, bool):
        print("true" if value else "false")
elif kind == "list":
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                print(item)
elif kind == "path":
    if isinstance(value, str) and value:
        print(str(_expand(value)))
else:
    if isinstance(value, str):
        print(value)
PYEOF
}

# dockwright_module_enabled <module> — exit 0 (enabled) / 1 (disabled). Reads
# [modules].<module>; ONLY an explicit `<module> = false` disables. Default +
# fail-open (missing key / no config / parse error): ENABLED.
dockwright_module_enabled() {
  local name="${1:?module name required}" value
  value="$(_dockwright_toml_get modules "$name" bool)" || value=""
  [ "$value" = "false" ] && return 1
  return 0
}

# dockwright_repo_path — echo the ~-expanded [paths].dockwright_repo, or
# nothing when unset (self-referential tooling home; e.g. the Gardener cwd).
dockwright_repo_path() {
  _dockwright_toml_get paths dockwright_repo path
}

# dockwright_high_skills — echo [gardener].high_skills, one skill name per
# line (empty when unset). The selffix trigger's task-triage HIGH gate.
dockwright_high_skills() {
  _dockwright_toml_get gardener high_skills list
}
