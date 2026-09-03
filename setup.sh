#!/usr/bin/env bash

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "${DOCKWRIGHT_SETUP_ALLOW_WORKTREE:-}" != "1" ] && [ -f "$REPO_DIR/.git" ]; then
    COMMON_GIT_DIR="$(git -C "$REPO_DIR" rev-parse --git-common-dir 2>/dev/null || true)"
    if [ -z "$COMMON_GIT_DIR" ]; then
        echo "ERROR: Running from a linked worktree but 'git rev-parse --git-common-dir' failed (git not installed or not a git repo?). Run setup.sh directly from the main clone." >&2
        exit 1
    fi
    MAIN_CLONE="$(dirname "$COMMON_GIT_DIR")"
    if [ ! -d "$MAIN_CLONE" ] || [ ! -f "$MAIN_CLONE/setup.sh" ]; then
        echo "ERROR: Running from a linked worktree but could not locate the main clone (resolved '$MAIN_CLONE'). Run setup.sh directly from the main clone." >&2
        exit 1
    fi
    echo "→ Running from linked worktree; self-anchoring install to main clone: $MAIN_CLONE"
    REPO_DIR="$MAIN_CLONE"
fi

if [ "${DOCKWRIGHT_SETUP_ALLOW_WORKTREE:-}" != "1" ]; then
    case "$REPO_DIR" in
        "$HOME"/worktrees*)
            echo "ERROR: refusing to install from a worktree path ($REPO_DIR). Run setup.sh from the main clone." >&2
            exit 1
            ;;
    esac
fi

CLAUDE_DIR="${CLAUDE_DIR:-$HOME/.claude}"
CODEX_DIR="${CODEX_DIR:-$HOME/.codex}"

if [ "${DOCKWRIGHT_SETUP_FORCE:-}" != "1" ]; then
    ACTIVE_DIR="$CLAUDE_DIR/dockwright/active"
    if [ ! -d "$ACTIVE_DIR" ] && [ -d "$CLAUDE_DIR/orchestrator/active" ]; then
        ACTIVE_DIR="$CLAUDE_DIR/orchestrator/active"
    fi
    if [ -d "$ACTIVE_DIR" ]; then
        LIVE_COUNT=$(find -L "$ACTIVE_DIR" -maxdepth 1 -type f -name '*.json' | wc -l | tr -d ' ') || {
            echo "ERROR: cannot enumerate $ACTIVE_DIR — refusing to deploy over an unreadable session registry. Fix its permissions, or re-run with DOCKWRIGHT_SETUP_FORCE=1 for a deliberate live deploy." >&2
            exit 4
        }
        if [ "${LIVE_COUNT:-0}" -gt 0 ]; then
            echo "ERROR: $LIVE_COUNT active worker/manager session(s) registered under $ACTIVE_DIR — setup.sh mutates the deployed tree in place and a live session mid-turn would boot against a half-updated tree." >&2
            echo "        Wait for the sessions to finish (or close them), or re-run with DOCKWRIGHT_SETUP_FORCE=1 for a deliberate live deploy." >&2
            exit 4
        fi
    fi
fi

OVERLAY_DIR="${DOCKWRIGHT_OVERLAY_DIR:-$HOME/.claude/dockwright-overlay}"
[ -d "$OVERLAY_DIR" ] || { [ -d "$HOME/.claude/orchestrator-overlay" ] && OVERLAY_DIR="$HOME/.claude/orchestrator-overlay"; }

CODEX_PRESENT=0
if command -v codex >/dev/null 2>&1; then
    CODEX_PRESENT=1
else
    echo "→ codex not on PATH — skipping the ~/.codex deploy (agents, commands, skills, hooks)"
fi

RENDER_BIN="${DOCKWRIGHT_ORCH_BIN:-}"

echo "→ Installing dockwright from $REPO_DIR"

DEPLOY_STAMP="$CLAUDE_DIR/dockwright/.deploy-stamp"
DEPLOY_SHA="$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
DEPLOY_SHA_SHORT="$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
DEPLOY_BRANCH="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
DEPLOY_DIRTY=0
[ -n "$(git -C "$REPO_DIR" status --porcelain 2>/dev/null)" ] && DEPLOY_DIRTY=1
DEPLOY_TAG=""
if [ "$DEPLOY_BRANCH" = "HEAD" ]; then
    DEPLOY_TAG="$(git -C "$REPO_DIR" describe --tags --exact-match HEAD 2>/dev/null || true)"
fi
if [ -n "$DEPLOY_TAG" ] && [ "$DEPLOY_DIRTY" = "0" ]; then
    echo "→ Deploying from release tag '$DEPLOY_TAG' ($DEPLOY_SHA_SHORT)"
elif [ "$DEPLOY_BRANCH" = "HEAD" ]; then
    echo "⚠️  WARNING: deploying from detached HEAD ($DEPLOY_SHA_SHORT, dirty=$DEPLOY_DIRTY) — the live surface will diverge from main; re-run setup.sh from clean main to converge." >&2
elif [ "$DEPLOY_BRANCH" != "main" ] || [ "$DEPLOY_DIRTY" = "1" ]; then
    echo "⚠️  WARNING: deploying from branch '$DEPLOY_BRANCH' (dirty=$DEPLOY_DIRTY) — the live surface will diverge from main; re-run setup.sh from clean main to converge." >&2
fi
if [ -f "$DEPLOY_STAMP" ]; then
    PREV_SHA="$(sed -n 's/^sha=//p' "$DEPLOY_STAMP" | head -1)"
    if [ -n "$PREV_SHA" ] && [ "$PREV_SHA" != "$DEPLOY_SHA" ] && [ "$PREV_SHA" != "unknown" ]; then
        if ! git -C "$REPO_DIR" cat-file -e "$PREV_SHA" 2>/dev/null; then
            echo "⚠️  WARNING: previously deployed sha $PREV_SHA is unknown in this checkout (deployed from another worktree/branch?) — cannot verify ancestry." >&2
        elif git -C "$REPO_DIR" merge-base --is-ancestor "$DEPLOY_SHA" "$PREV_SHA" 2>/dev/null; then
            echo "⚠️  WARNING: ancestry REGRESSION — HEAD $DEPLOY_SHA is an ancestor of previously deployed $PREV_SHA; this deploy rolls the live surface backwards." >&2
        fi
    fi
fi

if [ "${DOCKWRIGHT_SETUP_FILES_ONLY:-}" != "1" ]; then
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found on PATH — dockwright needs Python to install." >&2
    echo "  macOS:  brew install python@3.13   (then open a new shell so \$(brew --prefix)/bin is on PATH)" >&2
    echo "  Linux:  install python3.13 (e.g. apt/dnf package, or pyenv) and ensure 'python3' is on PATH." >&2
    exit 1
fi
MIN_PY="$(sed -n 's/^requires-python *= *">= *\([0-9][0-9.]*\) *[",].*/\1/p' "$REPO_DIR/pyproject.toml" 2>/dev/null | head -1 || true)"
MIN_PY="${MIN_PY:-3.11}"
python_meets_min() {
    "$1" -c "import sys; sys.exit(0 if sys.version_info >= tuple(int(x) for x in '$MIN_PY'.split('.')) else 1)" 2>/dev/null
}
if ! python_meets_min python3; then
    echo "ERROR: dockwright requires Python >= $MIN_PY; found: $(python3 --version 2>&1) at $(command -v python3)." >&2
    echo "  macOS:  brew install python@3.13   (then open a new shell so \$(brew --prefix)/bin is on PATH)" >&2
    echo "  Linux:  install python3.13 (e.g. apt/dnf package, or pyenv) and ensure 'python3' on PATH resolves to it." >&2
    exit 1
fi
VENV_STALE=0
if [ -d "$REPO_DIR/.venv" ] && ! python_meets_min "$REPO_DIR/.venv/bin/python"; then
    VENV_STALE=1
fi
if [ ! -d "$REPO_DIR/.venv" ] || [ "$VENV_STALE" = "1" ] || [ ! -x "$REPO_DIR/.venv/bin/pip" ]; then
    if ! python3 -c "import venv, ensurepip" >/dev/null 2>&1; then
        PY_MM="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 3)"
        echo "ERROR: python3 at $(command -v python3) cannot create virtualenvs — the venv/ensurepip module is missing." >&2
        echo "  Debian/Ubuntu: sudo apt install python${PY_MM}-venv   (or: python3-venv)" >&2
        echo "  Other:         install your distro's Python venv/ensurepip package, then re-run ./setup.sh" >&2
        exit 1
    fi
fi
if [ "$VENV_STALE" = "1" ]; then
    echo "→ Existing .venv is stale or broken (python missing or < $MIN_PY) — recreating"
    rm -rf "$REPO_DIR/.venv"
fi
if [ ! -d "$REPO_DIR/.venv" ]; then
    echo "→ Creating .venv"
    python3 -m venv "$REPO_DIR/.venv"
fi
if [ ! -x "$REPO_DIR/.venv/bin/pip" ]; then
    echo "→ Bootstrapping pip in .venv"
    "$REPO_DIR/.venv/bin/python" -m ensurepip --upgrade >/dev/null
fi
"$REPO_DIR/.venv/bin/python" -m pip uninstall -y claude-orchestrator >/dev/null 2>&1 || true
"$REPO_DIR/.venv/bin/python" -m pip install -e "$REPO_DIR" >/dev/null

DOCKWRIGHT_BIN="$REPO_DIR/.venv/bin/dockwright"
if [ ! -x "$DOCKWRIGHT_BIN" ]; then
    echo "ERROR: $DOCKWRIGHT_BIN not found after install" >&2
    exit 1
fi

LOCAL_BIN="$HOME/.local/bin"
mkdir -p "$LOCAL_BIN"
ln -sf "$DOCKWRIGHT_BIN" "$LOCAL_BIN/dockwright"
echo "→ Linked $LOCAL_BIN/dockwright → $DOCKWRIGHT_BIN"

"$DOCKWRIGHT_BIN" clean-homebrew --dist-name dockwright --console-script dockwright
"$DOCKWRIGHT_BIN" clean-homebrew --dist-name claude_orchestrator --console-script orchestrator

RENDER_BIN="$DOCKWRIGHT_BIN"
fi

if [ -n "$RENDER_BIN" ]; then
    "$RENDER_BIN" migrate-state --claude-dir "$CLAUDE_DIR"
fi

backup_then_cp() {
    if [ -f "$2" ] && ! cmp -s "$1" "$2"; then cp "$2" "$2.bak"; fi
    cp "$1" "$2"
}

if [ -n "$RENDER_BIN" ]; then
mkdir -p "$CLAUDE_DIR/agents"
"$RENDER_BIN" compose --core-dir "$REPO_DIR/deploy/agents" --out-dir "$CLAUDE_DIR/agents"
echo "→ Composed agent definitions to $CLAUDE_DIR/agents/"

if [ "$CODEX_PRESENT" = "1" ]; then
mkdir -p "$CODEX_DIR/agents"
python3 -c "
import json
from pathlib import Path

src_dir = Path('$CLAUDE_DIR') / 'agents'
out_dir = Path('$CODEX_DIR') / 'agents'
out_dir.mkdir(parents=True, exist_ok=True)

def parse_agent(path):
    text = path.read_text()
    meta = {}
    body = text
    if text.startswith('---'):
        parts = text.split('---', 2)
        if len(parts) == 3:
            _, raw_meta, body = parts
            for line in raw_meta.splitlines():
                if ':' in line:
                    key, value = line.split(':', 1)
                    meta[key.strip()] = value.strip()
            body = body.lstrip()
    name = meta.get('name') or path.stem
    description = meta.get('description') or ''
    return name, description, body

stamp = json.loads((src_dir / '.compose-stamp.json').read_text())
for name in sorted(stamp['core']):
    path = src_dir / name
    name, description, body = parse_agent(path)
    target = out_dir / f'{path.stem}.toml'
    target.write_text(
        'name = ' + json.dumps(name, ensure_ascii=False) + '\n'
        'description = ' + json.dumps(description, ensure_ascii=False) + '\n'
        'developer_instructions = ' + json.dumps(body, ensure_ascii=False) + '\n',
        encoding='utf-8',
    )
"
echo "→ Installed Codex agent definitions to $CODEX_DIR/agents/"
fi
fi

mkdir -p "$CLAUDE_DIR/commands"
if [ "$CODEX_PRESENT" = "1" ]; then
    mkdir -p "$CODEX_DIR/commands"
fi
if [ -n "$RENDER_BIN" ]; then
    "$RENDER_BIN" render --src "$REPO_DIR/deploy/commands" --out "$CLAUDE_DIR/commands" --glob '*.md'
    if [ "$CODEX_PRESENT" = "1" ]; then
        "$RENDER_BIN" render --src "$REPO_DIR/deploy/commands" --out "$CODEX_DIR/commands" --glob '*.md'
    fi
    echo "→ Rendered slash commands to $CLAUDE_DIR/commands/"
else
    for f in "$REPO_DIR/deploy/commands/"*.md; do
        [ -e "$f" ] || continue
        backup_then_cp "$f" "$CLAUDE_DIR/commands/$(basename "$f")"
        if [ "$CODEX_PRESENT" = "1" ]; then
            backup_then_cp "$f" "$CODEX_DIR/commands/$(basename "$f")"
        fi
    done
    echo "→ Installed slash commands (verbatim; no render binary) to $CLAUDE_DIR/commands/"
fi

if [ -d "$OVERLAY_DIR/commands" ]; then
    for f in "$OVERLAY_DIR/commands/"*.md; do
        [ -e "$f" ] || continue
        backup_then_cp "$f" "$CLAUDE_DIR/commands/$(basename "$f")"
        if [ "$CODEX_PRESENT" = "1" ]; then
            backup_then_cp "$f" "$CODEX_DIR/commands/$(basename "$f")"
        fi
    done
    echo "→ Installed overlay commands to $CLAUDE_DIR/commands/"
fi

if [ -n "$RENDER_BIN" ] && [ "$CODEX_PRESENT" = "1" ]; then
mkdir -p "$CODEX_DIR/skills"
CODEX_SKILL_SRC="$(mktemp -d)"
"$RENDER_BIN" render --src "$REPO_DIR/deploy/commands" --out "$CODEX_SKILL_SRC" --glob '*.md' >/dev/null
if [ -d "$OVERLAY_DIR/commands" ]; then
    cp "$OVERLAY_DIR/commands/"*.md "$CODEX_SKILL_SRC/"
fi
"$RENDER_BIN" install-codex-skills "$CODEX_SKILL_SRC" "$CODEX_DIR/skills" >/dev/null
rm -rf "$CODEX_SKILL_SRC"
echo "→ Installed Codex skill wrappers to $CODEX_DIR/skills/"
fi

if [ -d "$REPO_DIR/deploy/skills" ]; then
    mkdir -p "$CLAUDE_DIR/skills"
    rsync -a "$REPO_DIR/deploy/skills/" "$CLAUDE_DIR/skills/"
    echo "→ Installed Claude skills to $CLAUDE_DIR/skills/"
fi

mkdir -p "$CLAUDE_DIR/scripts"
cp "$REPO_DIR/deploy/scripts/"*.py "$CLAUDE_DIR/scripts/"
cp "$REPO_DIR/deploy/scripts/"*.sh "$CLAUDE_DIR/scripts/"
cp "$REPO_DIR/deploy/scripts/"*.cjs "$CLAUDE_DIR/scripts/" 2>/dev/null || true
cp "$REPO_DIR/src/dockwright/stale_monitor.py" "$CLAUDE_DIR/scripts/stale_monitor.py"
chmod +x "$CLAUDE_DIR/scripts/"*.py "$CLAUDE_DIR/scripts/"*.sh
echo "→ Installed dockwright helper scripts to $CLAUDE_DIR/scripts/"

stamp_provenance() {
    python3 -c '
import sys

path, source_rel, sha = sys.argv[1], sys.argv[2], sys.argv[3]
header = "# deployed-from: dockwright@" + sha + " — do not edit here; edit " + source_rel + " in the repo\n"

with open(path) as f:
    lines = f.readlines()

insert_at = 1 if lines and lines[0].startswith("#!") else 0
if insert_at < len(lines) and lines[insert_at].startswith("# deployed-from:"):
    lines[insert_at] = header
else:
    lines.insert(insert_at, header)

with open(path, "w") as f:
    f.writelines(lines)
' "$1" "$2" "$DEPLOY_SHA_SHORT"
}
for f in "$REPO_DIR/deploy/scripts/"*.py "$REPO_DIR/deploy/scripts/"*.sh; do
    name="$(basename "$f")"
    stamp_provenance "$CLAUDE_DIR/scripts/$name" "deploy/scripts/$name"
done
stamp_provenance "$CLAUDE_DIR/scripts/stale_monitor.py" "src/dockwright/stale_monitor.py"
echo "→ Stamped provenance headers on deployed scripts"

if [ -d "$OVERLAY_DIR/scripts" ]; then
    cp "$OVERLAY_DIR/scripts/"* "$CLAUDE_DIR/scripts/"
    for f in "$OVERLAY_DIR/scripts/"*.py "$OVERLAY_DIR/scripts/"*.sh; do
        [ -e "$f" ] || continue
        name="$(basename "$f")"
        chmod +x "$CLAUDE_DIR/scripts/$name"
        stamp_provenance "$CLAUDE_DIR/scripts/$name" "$(basename "$OVERLAY_DIR")/scripts/$name"
    done
    echo "→ Installed overlay scripts to $CLAUDE_DIR/scripts/"
fi

backup_then_cp "$REPO_DIR/deploy/statusline-command.sh" "$CLAUDE_DIR/statusline-command.sh"
chmod +x "$CLAUDE_DIR/statusline-command.sh"
echo "→ Installed statusline-command.sh to $CLAUDE_DIR/"

mkdir -p "$CLAUDE_DIR/dockwright"
cp "$REPO_DIR/deploy/loops-registry.md" "$CLAUDE_DIR/dockwright/loops-registry.md"
echo "→ Installed loops-registry.md to $CLAUDE_DIR/dockwright/"

mkdir -p "$CLAUDE_DIR/dockwright"
cp "$REPO_DIR/deploy/tmux/dockwright.conf" "$CLAUDE_DIR/dockwright/dockwright.tmux.conf"
echo "→ Installed tmux config to $CLAUDE_DIR/dockwright/dockwright.tmux.conf"
cp "$REPO_DIR/deploy/tmux/status_row.py" "$CLAUDE_DIR/dockwright/status_row.py"
chmod +x "$CLAUDE_DIR/dockwright/status_row.py"
echo "→ Installed status_row.py to $CLAUDE_DIR/dockwright/status_row.py"

mkdir -p "$CLAUDE_DIR/dockwright/presets"
rsync -a --delete "$REPO_DIR/deploy/presets/" "$CLAUDE_DIR/dockwright/presets/"
if [ -n "$RENDER_BIN" ]; then
    "$RENDER_BIN" render --src "$REPO_DIR/deploy/presets" --out "$CLAUDE_DIR/dockwright/presets" --glob '*.md'
fi
echo "→ Installed worker-spawn presets to $CLAUDE_DIR/dockwright/presets/"

if [ -d "$OVERLAY_DIR/presets" ]; then
    cp "$OVERLAY_DIR/presets/"* "$CLAUDE_DIR/dockwright/presets/"
    echo "→ Installed overlay presets to $CLAUDE_DIR/dockwright/presets/"
fi

if [ -n "$RENDER_BIN" ]; then
    "$RENDER_BIN" finalize-presets --file "$CLAUDE_DIR/dockwright/presets/worker-headless-settings.json"
fi

mkdir -p "$CLAUDE_DIR/dockwright/notebook/archive"

if [ "${DOCKWRIGHT_SETUP_FILES_ONLY:-}" != "1" ]; then
if command -v claude >/dev/null 2>&1; then
    claude mcp remove --scope user claude-orchestrator >/dev/null 2>&1 || true
    claude mcp remove --scope user dockwright >/dev/null 2>&1 || true
    claude mcp add --scope user dockwright "$DOCKWRIGHT_BIN" mcp-server >/dev/null
    echo "→ Registered dockwright MCP (Claude) → $DOCKWRIGHT_BIN"
else
    echo "WARNING: 'claude' CLI not on PATH. Manually run:" >&2
    echo "  claude mcp add --scope user dockwright \"$DOCKWRIGHT_BIN\" mcp-server" >&2
fi
if command -v codex >/dev/null 2>&1; then
    codex mcp remove claude-orchestrator >/dev/null 2>&1 || true
    codex mcp remove dockwright >/dev/null 2>&1 || true
    codex mcp add dockwright -- "$DOCKWRIGHT_BIN" mcp-server >/dev/null
    echo "→ Registered dockwright MCP (Codex) → $DOCKWRIGHT_BIN"
else
    echo "→ codex not on PATH — skipping Codex MCP registration"
fi

SETTINGS="$CLAUDE_DIR/settings.json"
SNIPPET="$REPO_DIR/deploy/settings.snippet.json"
"$DOCKWRIGHT_BIN" install-hooks --target "$SETTINGS" --snippet "$SNIPPET" --orch-bin "$DOCKWRIGHT_BIN" --mode claude
echo "→ Wired dockwright hooks into $SETTINGS (explicit path)"

if [ "$CODEX_PRESENT" = "1" ]; then
    mkdir -p "$CODEX_DIR"
    "$DOCKWRIGHT_BIN" install-hooks --target "$CODEX_DIR/hooks.json" --snippet "$SNIPPET" --orch-bin "$DOCKWRIGHT_BIN" --mode codex
    echo "→ Wired dockwright hooks into $CODEX_DIR/hooks.json (explicit path)"
fi
fi

mkdir -p "$CLAUDE_DIR/dockwright/active" "$CLAUDE_DIR/dockwright/questions" "$CLAUDE_DIR/dockwright/answers" "$CLAUDE_DIR/dockwright/done" "$CLAUDE_DIR/dockwright/handoffs"
echo "→ Created $CLAUDE_DIR/dockwright/ state directories"

mkdir -p "$CLAUDE_DIR/dockwright"
{
    echo "sha=$DEPLOY_SHA"
    echo "branch=$DEPLOY_BRANCH"
    echo "dirty=$DEPLOY_DIRTY"
    echo "date=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "repo_dir=$REPO_DIR"
} > "$DEPLOY_STAMP"
echo "→ Stamped deploy provenance to $DEPLOY_STAMP (sha=$DEPLOY_SHA branch=$DEPLOY_BRANCH dirty=$DEPLOY_DIRTY)"

if [ "${DOCKWRIGHT_SETUP_FILES_ONLY:-}" != "1" ]; then
echo "→ Reconciling pool-account config-dir farms (accounts-sync)…"
"$DOCKWRIGHT_BIN" accounts-sync

"$DOCKWRIGHT_BIN" write-registry-snapshot || true

WORKER_HOME="$("$DOCKWRIGHT_BIN" ensure-worker-home || true)"
[ -n "$WORKER_HOME" ] && echo "→ Ensured worker home exists: $WORKER_HOME"
fi

RUN_DOCTOR_GATE=0
[ "${DOCKWRIGHT_SETUP_FILES_ONLY:-}" != "1" ] && RUN_DOCTOR_GATE=1
[ "${DOCKWRIGHT_SETUP_RUN_DOCTOR:-}" = "1" ] && RUN_DOCTOR_GATE=1
if [ "$RUN_DOCTOR_GATE" = "1" ]; then
DOCTOR_BIN="${DOCKWRIGHT_BIN:-${RENDER_BIN:-}}"
if [ -z "$DOCTOR_BIN" ]; then
    echo "ERROR: doctor gate needs a dockwright binary (set DOCKWRIGHT_ORCH_BIN when forcing the gate under FILES_ONLY)" >&2
    exit 1
fi
echo "→ Verifying environment wiring (dockwright doctor)…"
DOCTOR_ARGS=(--orch-bin "$DOCTOR_BIN" --claude-json "$HOME/.claude.json"
    --host-claude-json "$HOME/.claude.json" --settings "${SETTINGS:-$CLAUDE_DIR/settings.json}"
    --brew-prefix "$(brew --prefix 2>/dev/null || echo /opt/homebrew)"
    --dist-name dockwright --server-name dockwright --strict)
DOCTOR_ARGS+=(--compose-core-dir "$REPO_DIR/deploy/agents" --compose-out-dir "$CLAUDE_DIR/agents")
[ -f "$CODEX_DIR/hooks.json" ] && DOCTOR_ARGS+=(--codex-hooks "$CODEX_DIR/hooks.json")
[ -f "$CODEX_DIR/config.toml" ] && DOCTOR_ARGS+=(--codex-config "$CODEX_DIR/config.toml")
DOCTOR_OUT="$("$DOCTOR_BIN" doctor "${DOCTOR_ARGS[@]}" 2>&1)" && DOCTOR_RC=0 || DOCTOR_RC=$?
printf '%s\n' "$DOCTOR_OUT"
if [ "$DOCTOR_RC" -eq 0 ]; then
    echo "→ Environment wiring verified."
else
    DOCTOR_FAILS="$(printf '%s\n' "$DOCTOR_OUT" | grep '\[FAIL\]' || true)"
    DOCTOR_OTHER="$(printf '%s\n' "$DOCTOR_FAILS" | grep -v '^  \[FAIL\] accounts:login: ' || true)"
    if [ -z "$DOCTOR_FAILS" ] || [ -n "$DOCTOR_OTHER" ]; then
        exit "$DOCTOR_RC"
    fi
    echo "WARNING: accounts:login failed — login state is a user prerequisite, not installer wiring; install continues." >&2
    printf '%s\n' "$DOCTOR_FAILS" >&2
    echo "  Run the fix printed above, then re-verify: $DOCTOR_BIN doctor" >&2
fi
fi

if [ "${DOCKWRIGHT_SETUP_FILES_ONLY:-}" != "1" ]; then
if [ -d "$OVERLAY_DIR/setup.d" ]; then
    for f in "$OVERLAY_DIR/setup.d/"*.sh; do
        [ -e "$f" ] || continue
        echo "→ Running overlay setup.d step: $(basename "$f")"
        bash "$f"
    done
fi
fi

echo ""
echo "✓ Install complete."
echo "  Prereq: tmux installed and on PATH (brew install tmux)."
echo "  Start a session:"
echo "    dockwright manager"
echo "  (or manually: tmux -L dockwright -f ~/.claude/dockwright/dockwright.tmux.conf new-session,"
echo "  then launch claude (or codex) inside it and run /manager)."
echo ""
echo "  Optional self-improvement (off by default, extra token cost):"
echo "    dockwright selffix enable    # session-end retrospectives (findings)"
echo "    dockwright gardener enable   # background digest of findings into ranked proposals (needs selffix)"
echo "                                 #   --lane all also arms the weekly web-research sweep"
