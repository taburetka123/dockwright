#!/usr/bin/env bash
# canon-edit-guard.sh — PreToolUse hook (Edit|Write|MultiEdit).
# Warns when a session edits a ~/.claude file that is cp-deployed from the
# dockwright canon by setup.sh, or COMPOSED into ~/.claude/agents by
# `dockwright compose` from a core + overlay drop-ins; either way the direct
# edit is destroyed on the next setup.sh / compose. Also flags a composed
# agent that has already DRIFTED from the last compose stamp, and folds in
# asset-validator warnings for the touched file. Emits a permission-NEUTRAL
# additionalContext note. Fail-open: any parse problem -> exit 0, no output,
# never blocks.
#
# The canon lives at the dockwright checkout's deploy/ dir, resolved from
# [paths] dockwright_repo in dockwright.toml. That key defaults UNSET and
# `dockwright compose` itself needs no config, so only the cp-deployed branch —
# which has no canon path to name without it — may depend on it. Composed/DRIFT
# detection falls back to the compose stamp (self-sufficient: compose writes it
# next to the outputs), and the asset warnings never consult the canon at all.
set -euo pipefail

CLAUDE_DIR="$HOME/.claude"
# The pre-pass below hands back a path whose directories are resolved, so
# resolve this side too or the prefix test compares a physical path against a
# symlinked one — a symlinked ~/.claude (a dotfiles checkout) or a HOME with a
# trailing slash would silence the hook for the whole install.
if [ -d "$CLAUDE_DIR" ]; then
    CLAUDE_DIR="$(cd "$CLAUDE_DIR" && pwd -P)"
fi

# One python3 pass: parse the hook's file_path from stdin, resolve
# [paths] dockwright_repo + overlay_dir (tomllib when the interpreter has it;
# a minimal scanner fallback for a py3.9 interpreter with no tomllib), and look
# the file up in the compose stamp. Emits five lines: file_path, the ~-expanded
# repo path (blank when unset), the resolved overlay dir, whether the stamp
# says this file is composed, and the core source the stamp names for it.
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
# No configured dockwright repo -> no canon path to name in the cp-deployed
# wording. The composed and asset-warning sections below do not need one.
CANON_DIR=""
[ -z "$DOCKWRIGHT_REPO" ] || CANON_DIR="$DOCKWRIGHT_REPO/deploy"

case "$file_path" in "$CLAUDE_DIR"/*) ;; *) exit 0 ;; esac

relpath="${file_path#"$CLAUDE_DIR"/}"

# Resolve the ~/.claude relpath to its canon SOURCE relpath. Most trees deploy at
# the SAME relpath (commands/ scripts/ skills/ statusline-command.sh). A few
# deploy RENAMED — setup.sh cp's them to a different ~/.claude path; mirror those
# lines here (setup.sh:356 loops-registry.md, :362 tmux conf, :364 status_row.py)
# so renamed files are still guarded. Every branch is existence-gated below, so an
# ~/.claude path with no canon source (e.g. dockwright/ runtime state) never warns.
#
# agents/ is NOT in that world at all: setup.sh cp's nothing there, `dockwright
# compose` renders it. So every agents/ path skips the cp lookup — the composed
# arm handles agents/*.md, and the other two arms stay silent rather than tell an
# operator to edit a canon file setup.sh never copies.
canon_rel=""
composed=""
case "$relpath" in
    agents/*/*) ;;            # `*` crosses `/` in case patterns; compose emits no nested outputs
    agents/*.md)
        agent_stem="${relpath#agents/}"; agent_stem="${agent_stem%.md}"
        # A composed file is NOT cp'd: compose renders ~/.claude/agents/X.md from
        # deploy/agents/X.core.md (or a plain X.md core) + overlay drop-ins, so a
        # direct edit here is DESTROYED at the next compose. Prefer the canon when
        # one is configured (it can name the exact source file); fall back to the
        # stamp, which is what a stock install has.
        if [ -n "$CANON_DIR" ] && [ -e "$CANON_DIR/agents/$agent_stem.core.md" ]; then
            canon_rel="agents/$agent_stem.core.md"; composed=1
        elif [ -n "$CANON_DIR" ] && [ -e "$CANON_DIR/agents/$agent_stem.md" ]; then
            canon_rel="agents/$agent_stem.md"; composed=1
        elif [ -n "$STAMP_COMPOSED" ]; then
            composed=1
        fi
        ;;
    agents/*) ;;              # non-.md under agents/ (vars.defaults.toml): not cp'd either
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
                    # deprecated, one release: edits through the compat symlink path still map
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

# Asset warnings for the file being touched. Today they are computed at commit
# time and written to a log nobody opens; here they arrive in-session, attached
# to the file, at the moment of authorship. Fail-soft in every direction.
#
# The whole hook budget is 5s (settings.snippet.json) and the callee-side
# --max-seconds cap is only advisory — a stale or partially-deployed validator
# need not honour the flag, and its SIGALRM cannot interrupt a regex that stays
# in C — so bound the wall clock on THIS side too. `timeout(1)` is absent on
# stock macOS; the interpreter this hook already requires does the job. The
# wrapper also absorbs the callee exit status: a command substitution propagates
# it under `set -e`, and a PreToolUse hook exiting non-zero BLOCKS the tool call.
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

# warn_text becomes a single argv element to the final `python3 -c` below. A
# corrupt or hostile validator that prints more than the exec arg limit would make
# that exec fail E2BIG (status 126 — a non-blocking error, so the fail-open
# contract survives, but the composed/DRIFT/canon note would die with it). Cap it
# well under any ARG_MAX / MAX_ARG_STRLEN; a real per-file warning set is far
# below the cap, so mark a truncation visibly rather than silently.
if [ "${#warn_text}" -gt 8192 ]; then
    warn_text="${warn_text:0:8192}
... (asset-validator output truncated)"
fi

[ -n "$canon_rel" ] || [ -n "$composed" ] || [ -n "$warn_text" ] || exit 0

# NOTE: single-quoted python program — no apostrophe may appear anywhere in this
# body (including inside the message strings) or bash ends the quote early and the
# hook degrades to silence.
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
