#!/usr/bin/env bash
set -euo pipefail

CLAUDE_DIR="$HOME/.claude"
if [ -d "$CLAUDE_DIR" ]; then
    CLAUDE_DIR="$(cd "$CLAUDE_DIR" && pwd -P)"
fi

guard_read="$(python3 <(cat <<'PYEOF'
import json, os, pathlib, sys

CLAUDE_HOME = os.path.realpath(str(pathlib.Path.home() / ".claude"))


def _inside(path):
    return path == CLAUDE_HOME or path.startswith(CLAUDE_HOME + os.sep)


def _normalize(raw):
    """Normalize before anything compares or splits the path.

    `..`, `//`, `./` and a trailing slash otherwise all miss the ~/.claude
    prefix test, and a `rules/../agents/x.md` shape reaches the validator as a
    rules/ relpath, earning rule-class warnings on an agent file. A symlinked
    ~/.claude (a dotfiles checkout) has to resolve INTO the config home too.

    But a full realpath() also follows the LEAF, so a per-file symlink out of
    the tree (agents/manager.md -> dotfiles/manager.md) lands outside and the
    prefix test drops it — silencing a warning that is still true, because the
    edit reaches the deployed agent and the next compose still deletes it. So
    resolve the PARENTS and keep the leaf name, and fall back to a full
    realpath only when that form does not land in the config home — which is
    what carries a symlink pointing INTO ~/.claude from outside."""
    if not raw:
        return ""
    absolute = os.path.abspath(raw)
    parents = os.path.join(os.path.realpath(os.path.dirname(absolute)),
                           os.path.basename(absolute))
    return parents if _inside(parents) else os.path.realpath(absolute)


try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
fp = (data.get("tool_input") or {}).get("file_path")
fp = _normalize(fp if isinstance(fp, str) else "")


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


def _overlay(value):
    """Mirrors config.overlay_dir(): an explicit value wins verbatim; otherwise
    prefer the new default when it exists, fall back to the legacy default when
    only it exists (un-migrated install), else the new default."""
    if isinstance(value, str) and value:
        return str(_expand(value))
    new = pathlib.Path.home() / ".claude" / "dockwright-overlay"
    legacy = pathlib.Path.home() / ".claude" / "orchestrator-overlay"
    if new.exists():
        return str(new)
    if legacy.exists():
        return str(legacy)
    return str(new)


def _stamp_entry(path):
    """(composed, core_source) for a file sitting DIRECTLY in ~/.claude/agents.

    Read from the compose stamp rather than from the canon: compose writes the
    stamp next to its outputs on every run and needs no [paths] dockwright_repo,
    so this is the only composed-detection that works on a stock install.
    Fail-open to ("", "").

    The `.md` and dirname tests are a second line of defence for a future
    caller, NOT the operative mechanism: the bash `case "$relpath" in
    agents/*.md)` arm already excludes every non-agents, nested and non-.md
    path before either return value is read, so no input reaches this function
    that those two tests would have to reject."""
    try:
        if not path.endswith(".md"):
            return "", ""
        agents = os.path.realpath(str(pathlib.Path.home() / ".claude" / "agents"))
        if os.path.dirname(path) != agents:
            return "", ""
        with open(os.path.join(agents, ".compose-stamp.json")) as fh:
            data = json.load(fh)
        outputs = data.get("outputs")
        name = os.path.basename(path)
        if not isinstance(outputs, dict) or name not in outputs:
            return "", ""
        sources = data.get("core_sources")
        core = sources.get(name) if isinstance(sources, dict) else None
        core = core if isinstance(core, str) else ""
        return "1", core.replace("\n", " ").replace("\r", " ")
    except Exception:
        return "", ""


repo = ""
overlay_raw = None
path = _config_file()
if path is not None:
    section = {}
    try:
        import tomllib
        with open(path, "rb") as fh:
            section = tomllib.load(fh).get("paths", {}) or {}
    except ModuleNotFoundError:
        try:
            text = path.read_text()
            section = {"dockwright_repo": _scan(text, "paths", "dockwright_repo"),
                       "overlay_dir": _scan(text, "paths", "overlay_dir")}
        except OSError:
            section = {}
    except Exception:
        section = {}
    value = section.get("dockwright_repo")
    if isinstance(value, str) and value:
        repo = str(_expand(value))
    overlay_raw = section.get("overlay_dir")

stamp_composed, stamp_core = _stamp_entry(fp)
sys.stdout.write("\n".join(
    [fp, repo, _overlay(overlay_raw), stamp_composed, stamp_core]) + "\n")
PYEOF
) 2>/dev/null || true)"

file_path="$(printf '%s' "$guard_read" | sed -n '1p')"
DOCKWRIGHT_REPO="$(printf '%s' "$guard_read" | sed -n '2p')"
OVERLAY_DIR="$(printf '%s' "$guard_read" | sed -n '3p')"
STAMP_COMPOSED="$(printf '%s' "$guard_read" | sed -n '4p')"
STAMP_CORE="$(printf '%s' "$guard_read" | sed -n '5p')"

[ -n "$file_path" ] || exit 0
CANON_DIR=""
[ -z "$DOCKWRIGHT_REPO" ] || CANON_DIR="$DOCKWRIGHT_REPO/deploy"

case "$file_path" in "$CLAUDE_DIR"/*) ;; *) exit 0 ;; esac

relpath="${file_path#"$CLAUDE_DIR"/}"

canon_rel=""
composed=""
case "$relpath" in
    agents/*/*) ;;
    agents/*.md)
        agent_stem="${relpath#agents/}"; agent_stem="${agent_stem%.md}"
        if [ -n "$CANON_DIR" ] && [ -e "$CANON_DIR/agents/$agent_stem.core.md" ]; then
            canon_rel="agents/$agent_stem.core.md"; composed=1
        elif [ -n "$CANON_DIR" ] && [ -e "$CANON_DIR/agents/$agent_stem.md" ]; then
            canon_rel="agents/$agent_stem.md"; composed=1
        elif [ -n "$STAMP_COMPOSED" ]; then
            composed=1
        fi
        ;;
    agents/*) ;;
    *)
        if [ -n "$CANON_DIR" ]; then
            if [ -e "$CANON_DIR/$relpath" ]; then
                canon_rel="$relpath"
            else
                case "$relpath" in
                    dockwright/presets/*)               canon_rel="presets/${relpath#dockwright/presets/}" ;;
                    dockwright/status_row.py)           canon_rel="tmux/status_row.py" ;;
                    dockwright/dockwright.tmux.conf)    canon_rel="tmux/dockwright.conf" ;;
                    dockwright/loops-registry.md)       canon_rel="loops-registry.md" ;;
                    orchestrator/presets/*)             canon_rel="presets/${relpath#orchestrator/presets/}" ;;
                    orchestrator/status_row.py)         canon_rel="tmux/status_row.py" ;;
                    orchestrator/dockwright.tmux.conf)  canon_rel="tmux/dockwright.conf" ;;
                esac
                if [ -n "$canon_rel" ] && [ ! -e "$CANON_DIR/$canon_rel" ]; then
                    canon_rel=""
                fi
            fi
        fi
        ;;
esac

warn_text=""
case "$relpath" in
    rules/*|commands/*|agents/*|flows/*|skills/*)
        validator="$CLAUDE_DIR/scripts/asset_validator.py"
        if [ -f "$validator" ]; then
            warn_text="$(python3 -c 'import subprocess, sys
try:
    sys.stdout.write(subprocess.run([sys.executable] + sys.argv[1:], capture_output=True,
                                    text=True, timeout=2).stdout)
except Exception:
    pass' "$validator" --repo "$CLAUDE_DIR" --files "$relpath" --max-seconds 2 2>/dev/null || true)"
        fi
        ;;
esac

if [ "${#warn_text}" -gt 8192 ]; then
    warn_text="${warn_text:0:8192}
... (asset-validator output truncated)"
fi

[ -n "$canon_rel" ] || [ -n "$composed" ] || [ -n "$warn_text" ] || exit 0

python3 -c '
import hashlib, json, os, sys
canon_dir, canon_rel, composed, stamp_core, overlay_dir, file_path, claude_dir, warn_text = sys.argv[1:9]


def drifted():
    """True when the deployed file no longer matches what compose last wrote —
    i.e. it holds bytes with no home in core or overlay. Fail-open to False."""
    try:
        with open(os.path.join(claude_dir, "agents", ".compose-stamp.json")) as fh:
            outputs = json.load(fh).get("outputs")
        prev = outputs.get(os.path.basename(file_path)) if isinstance(outputs, dict) else None
        if not isinstance(prev, str) or not prev:
            return False
        with open(file_path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest() != prev
    except Exception:
        return False


parts = []
if composed:
    stem = os.path.basename(file_path)[:-3]
    if canon_rel:
        source = f"`{canon_dir}/{canon_rel}`"
    elif stamp_core:
        source = f"the core `agents/{stamp_core}` in the dockwright checkout"
    else:
        source = "its core in the dockwright checkout"
    parts.append(
        f"⚠️ `~/.claude/agents/{stem}.md` is COMPOSED, not copied: `dockwright compose` renders it "
        f"from {source} plus the overlay drop-ins in `{overlay_dir}/{stem}/`. "
        f"A direct edit HERE is DROPPED at the next compose (every setup.sh recomposes) — put "
        f"generic text in the core, operator-specific text in a drop-in.")
    if drifted():
        parts.append(
            "🚨 DRIFT: this deployed file ALREADY differs from what the last compose wrote. The "
            "next compose OVERWRITES it from the core plus the overlay drop-ins; any bytes here "
            "with no home in the core or the overlay are lost. Recover them into the core or a "
            "drop-in first (`dockwright compose --check`).")
elif canon_rel:
    parts.append(
        f"⚠️ This file is cp-deployed from `{canon_dir}/{canon_rel}` by setup.sh — edit the CANON "
        f"there (+ commit the dockwright repo + run setup.sh), NOT ~/.claude, or your change is "
        f"reverted on the next setup.sh.")
if warn_text:
    parts.append("⚠️ asset-validator on this file:\n" + warn_text)
if parts:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "additionalContext": "\n\n".join(parts)}}))
' "$CANON_DIR" "$canon_rel" "$composed" "$STAMP_CORE" "$OVERLAY_DIR" "$file_path" "$CLAUDE_DIR" "$warn_text"
