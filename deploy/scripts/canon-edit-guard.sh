#!/usr/bin/env bash
# canon-edit-guard.sh — PreToolUse hook (Edit|Write|MultiEdit).
# Warns when a session edits a ~/.claude file that is cp-deployed from the
# dockwright canon by setup.sh; the ~/.claude copy is reverted on the next
# setup.sh. Emits a permission-NEUTRAL additionalContext note pointing at the
# canon. Fail-open: any parse problem -> exit 0, no output, never blocks.
#
# The canon lives at the dockwright checkout's deploy/ dir, resolved from
# [paths] dockwright_repo in dockwright.toml. When that key is unset there is no
# canon to point at, so the guard exits silently (exit 0, no warning).
set -euo pipefail

CLAUDE_DIR="$HOME/.claude"

# One python3 pass: parse the hook's file_path from stdin AND resolve
# [paths] dockwright_repo (tomllib when the interpreter has it; a minimal
# scanner fallback for a py3.9 interpreter with no tomllib). Emits two lines:
# file_path, then the ~-expanded repo path (blank when unset).
guard_read="$(python3 <(cat <<'PYEOF'
import json, os, pathlib, sys

try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
fp = (data.get("tool_input") or {}).get("file_path")
fp = fp if isinstance(fp, str) else ""


def _expand(raw):
    return pathlib.Path(raw).expanduser()


def _config_file():
    env = os.environ.get("DOCKWRIGHT_CONFIG", "").strip()
    if env:
        p = _expand(env)
        return p if p.is_file() else None
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = _expand(xdg) if xdg else pathlib.Path.home() / ".config"
    for c in (base / "dockwright" / "dockwright.toml",
              pathlib.Path.home() / ".claude" / "dockwright.toml"):
        if c.is_file():
            return c
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
        if v[:1] in ("'", '"'):
            q = v[0]
            end = v.find(q, 1)
            return v[1:end] if end != -1 else v.strip(q)
        return v.split("#", 1)[0].strip() or None
    return None


repo = ""
path = _config_file()
if path is not None:
    value = None
    try:
        import tomllib
        with open(path, "rb") as fh:
            value = tomllib.load(fh).get("paths", {}).get("dockwright_repo")
    except ModuleNotFoundError:
        try:
            value = _scan(path.read_text(), "paths", "dockwright_repo")
        except OSError:
            value = None
    except Exception:
        value = None
    if isinstance(value, str) and value:
        repo = str(_expand(value))

sys.stdout.write(fp + "\n" + repo + "\n")
PYEOF
) 2>/dev/null || true)"

file_path="$(printf '%s' "$guard_read" | sed -n '1p')"
DOCKWRIGHT_REPO="$(printf '%s' "$guard_read" | sed -n '2p')"

[ -n "$file_path" ] || exit 0
# No configured dockwright repo -> no canon to point at; nothing to guard.
[ -n "$DOCKWRIGHT_REPO" ] || exit 0
CANON_DIR="$DOCKWRIGHT_REPO/deploy"

case "$file_path" in "$CLAUDE_DIR"/*) ;; *) exit 0 ;; esac

relpath="${file_path#"$CLAUDE_DIR"/}"

# Resolve the ~/.claude relpath to its canon SOURCE relpath. Most trees deploy at
# the SAME relpath (agents/ commands/ scripts/ skills/ statusline-command.sh). A
# few deploy RENAMED — setup.sh cp's them to a different ~/.claude path; mirror
# those lines here (setup.sh:356 loops-registry.md, :362 tmux conf, :364
# status_row.py) so renamed files are still guarded. Every branch is
# existence-gated below, so an ~/.claude path with no canon source (e.g.
# dockwright/ runtime state) never warns.
canon_rel=""
if [ -e "$CANON_DIR/$relpath" ]; then
    canon_rel="$relpath"
else
    case "$relpath" in
        dockwright/presets/*)               canon_rel="presets/${relpath#dockwright/presets/}" ;;
        dockwright/status_row.py)           canon_rel="tmux/status_row.py" ;;
        dockwright/dockwright.tmux.conf)    canon_rel="tmux/dockwright.conf" ;;
        dockwright/loops-registry.md)       canon_rel="loops-registry.md" ;;
        # deprecated, one release: edits through the compat symlink path still map
        orchestrator/presets/*)             canon_rel="presets/${relpath#orchestrator/presets/}" ;;
        orchestrator/status_row.py)         canon_rel="tmux/status_row.py" ;;
        orchestrator/dockwright.tmux.conf)  canon_rel="tmux/dockwright.conf" ;;
    esac
    if [ -n "$canon_rel" ] && [ ! -e "$CANON_DIR/$canon_rel" ]; then
        canon_rel=""
    fi
fi

[ -n "$canon_rel" ] || exit 0

python3 -c '
import json, sys
canon_dir = sys.argv[1]
relpath = sys.argv[2]
msg = ("⚠️ This file is cp-deployed from "
       f"`{canon_dir}/{relpath}` by setup.sh — edit the CANON there (+ commit the "
       "dockwright repo + run setup.sh), NOT ~/.claude, or your change is reverted "
       "on the next setup.sh.")
print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": msg}}))
' "$CANON_DIR" "$canon_rel"
