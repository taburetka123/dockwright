#!/usr/bin/env bash
# setup.sh — wire dockwright into Claude and Codex

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

# Guard: if running from a linked git worktree, self-anchor to the main clone.
# In a linked worktree, .git is a FILE (not a directory). Installing from a
# worktree puts .venv inside the worktree; the ~/.local/bin/dockwright symlink
# then dangles when the worktree is removed. Redirect REPO_DIR to the main clone
# so the venv and symlink are always durable.
#
# DOCKWRIGHT_SETUP_ALLOW_WORKTREE=1 bypasses BOTH this self-anchor redirect and
# the worktree-path refusal below — a sandboxed test run (spec S6) must deploy
# the invoking worktree's own bytes, not redirect to the main clone.
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
# Overlay payload root (operator drop-ins: commands/, scripts/, presets/,
# setup.d/). DOCKWRIGHT_OVERLAY_DIR overrides for sandboxed tests; the default
# matches how the package resolves the overlay (config.DEFAULT_OVERLAY_DIR).
OVERLAY_DIR="${DOCKWRIGHT_OVERLAY_DIR:-$HOME/.claude/dockwright-overlay}"
# deprecated, one release: fall back to the legacy overlay home when the new one
# doesn't exist yet (mirrors config.DEFAULT_OVERLAY_DIR's on-disk fallback).
[ -d "$OVERLAY_DIR" ] || { [ -d "$HOME/.claude/orchestrator-overlay" ] && OVERLAY_DIR="$HOME/.claude/orchestrator-overlay"; }

# ~/.codex belongs to a SECOND tool — never create or populate it for a user
# who doesn't have Codex installed. This single gate covers every $CODEX_DIR
# write below (agent mirror, command deploy, skill wrappers, hooks.json); the
# codex MCP registration in step 5 is already gated the same way.
CODEX_PRESENT=0
if command -v codex >/dev/null 2>&1; then
    CODEX_PRESENT=1
else
    echo "→ codex not on PATH — skipping the ~/.codex deploy (agents, commands, skills, hooks)"
fi

# Binary used for the deploy-time file TRANSFORMS (agent compose, command/preset
# render, codex agent mirror + skill wrappers). A normal install builds one in
# .venv and re-points RENDER_BIN at it below. In FILES_ONLY mode there is no
# build, so the transforms are skipped UNLESS DOCKWRIGHT_ORCH_BIN names a
# prebuilt binary (e.g. a worktree's .venv/bin/dockwright) — that override is
# what lets the byte-equivalence gate render fully from a worktree.
RENDER_BIN="${DOCKWRIGHT_ORCH_BIN:-}"

echo "→ Installing dockwright from $REPO_DIR"

# Deploy provenance (arch-soundness review 2026-06-11 A3): the deployed
# ~/.claude surface flapped between PR-states live with no record of what was
# running. Stamp every deploy; warn loudly when the deploying tree is not
# clean main or when it would roll the live surface backwards in ancestry.
DEPLOY_STAMP="$CLAUDE_DIR/dockwright/.deploy-stamp"
DEPLOY_SHA="$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
DEPLOY_SHA_SHORT="$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
DEPLOY_BRANCH="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
DEPLOY_DIRTY=0
[ -n "$(git -C "$REPO_DIR" status --porcelain 2>/dev/null)" ] && DEPLOY_DIRTY=1
if [ "$DEPLOY_BRANCH" != "main" ] || [ "$DEPLOY_DIRTY" = "1" ]; then
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

# FILES_ONLY (S6 sandbox) skips every step that mutates the machine or needs the
# freshly installed binary: venv/pip install, the ~/.local/bin symlink, and
# homebrew cleanup. The file TRANSFORMS below (compose, render, codex mirror)
# are gated on RENDER_BIN instead, so DOCKWRIGHT_ORCH_BIN can drive them here.
if [ "${DOCKWRIGHT_SETUP_FILES_ONLY:-}" != "1" ]; then
# 0. Fail fast on a python that can't build the venv. A fresh macOS ships CLT
# python 3.9 and dies at `pip install -e` with a raw PEP-660 error and no hint
# (macOS E2E finding I-1). Presence first: a python-less box must error cleanly
# before any pyproject parsing. The floor comes from pyproject's requires-python
# so this check can never drift from the packaging contract. FILES_ONLY skips
# venv/pip entirely, so the whole check is gated with it (the S6 sandbox pins
# PATH to the CLT python deliberately).
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found on PATH — dockwright needs Python to install." >&2
    echo "  macOS:  brew install python@3.13   (then open a new shell so \$(brew --prefix)/bin is on PATH)" >&2
    echo "  Linux:  install python3.13 (e.g. apt/dnf package, or pyenv) and ensure 'python3' is on PATH." >&2
    exit 1
fi
MIN_PY="$(sed -n 's/^requires-python *= *">= *\([0-9][0-9.]*\) *[",].*/\1/p' "$REPO_DIR/pyproject.toml" 2>/dev/null | head -1 || true)"
MIN_PY="${MIN_PY:-3.11}"
python_meets_min() {  # $1 = python executable; true iff it exists, runs, and is >= MIN_PY
    "$1" -c "import sys; sys.exit(0 if sys.version_info >= tuple(int(x) for x in '$MIN_PY'.split('.')) else 1)" 2>/dev/null
}
if ! python_meets_min python3; then
    echo "ERROR: dockwright requires Python >= $MIN_PY; found: $(python3 --version 2>&1) at $(command -v python3)." >&2
    echo "  macOS:  brew install python@3.13   (then open a new shell so \$(brew --prefix)/bin is on PATH)" >&2
    echo "  Linux:  install python3.13 (e.g. apt/dnf package, or pyenv) and ensure 'python3' on PATH resolves to it." >&2
    exit 1
fi
# A .venv built by an older python — or whose interpreter broke (brew python
# upgrade, half-finished create) — makes every re-run fail identically with no
# hint; recovery used to require knowing `rm -rf .venv` (macOS E2E finding
# N-6). The venv is a build artifact this script itself creates: recreate it.
if [ -d "$REPO_DIR/.venv" ] && ! python_meets_min "$REPO_DIR/.venv/bin/python"; then
    echo "→ Existing .venv is stale or broken (python missing or < $MIN_PY) — recreating"
    rm -rf "$REPO_DIR/.venv"
fi
# 1. Install the Python package
if [ ! -d "$REPO_DIR/.venv" ]; then
    echo "→ Creating .venv"
    python3 -m venv "$REPO_DIR/.venv"
fi
if [ ! -x "$REPO_DIR/.venv/bin/pip" ]; then
    echo "→ Bootstrapping pip in .venv"
    "$REPO_DIR/.venv/bin/python" -m ensurepip --upgrade >/dev/null
fi
# One-release sweep: drop the pre-rename claude_orchestrator dist so the editable
# reinstall below doesn't leave a stale second distribution shadowing dockwright.
"$REPO_DIR/.venv/bin/python" -m pip uninstall -y claude-orchestrator >/dev/null 2>&1 || true
"$REPO_DIR/.venv/bin/python" -m pip install -e "$REPO_DIR" >/dev/null

# 2. Ensure `dockwright` is on PATH
DOCKWRIGHT_BIN="$REPO_DIR/.venv/bin/dockwright"
if [ ! -x "$DOCKWRIGHT_BIN" ]; then
    echo "ERROR: $DOCKWRIGHT_BIN not found after install" >&2
    exit 1
fi

# Symlink into ~/.local/bin if it exists and is on PATH. Only the dockwright link
# is (re)created here; the legacy `orchestrator` link is left as-is (the migration
# note covers retiring it) and uninstall removes either name.
LOCAL_BIN="$HOME/.local/bin"
mkdir -p "$LOCAL_BIN"
ln -sf "$DOCKWRIGHT_BIN" "$LOCAL_BIN/dockwright"
echo "→ Linked $LOCAL_BIN/dockwright → $DOCKWRIGHT_BIN"

# Remove any duplicate Homebrew system-python editable install (surgical — only this
# distribution's own artifacts; never unrelated brew packages). Idempotent. The
# second call sweeps the pre-rename claude_orchestrator/orchestrator artifacts.
"$DOCKWRIGHT_BIN" clean-homebrew --dist-name dockwright --console-script dockwright
"$DOCKWRIGHT_BIN" clean-homebrew --dist-name claude_orchestrator --console-script orchestrator

# The freshly installed binary drives the deploy-time file transforms below.
RENDER_BIN="$DOCKWRIGHT_BIN"
fi

# One-shot state migration (idempotent): orchestrator-era paths -> ~/.claude/dockwright
# with compat symlinks left at the old locations. Runs before every deploy copy so
# targets below always land in the new home. RENDER_BIN-gated like the transforms:
# DOCKWRIGHT_BIN in a real install, DOCKWRIGHT_ORCH_BIN in the FILES_ONLY sandbox.
if [ -n "$RENDER_BIN" ]; then
    "$RENDER_BIN" migrate-state --claude-dir "$CLAUDE_DIR"
fi

# Backup-before-overwrite for user-visible deployed files (mirrors the
# settings.json capped-backup pattern): keep ONE .bak per file per run,
# only when content actually changes. Scoped to user-customizable surfaces
# copied by a shell cp: statusline-command.sh, and the verbatim-fallback /
# overlay command copies (per-file loops below). State-root-internal files
# (presets, tmux conf, scripts) keep plain cp. Composed agents and the
# render-seam command writes go through the compose/render binary (write_text),
# not a shell cp, so this helper does not wrap those.
backup_then_cp() {  # src dst
    if [ -f "$2" ] && ! cmp -s "$1" "$2"; then cp "$2" "$2.bak"; fi
    cp "$1" "$2"
}

# 3. Compose agent definitions (core + overlay drop-ins + dockwright.toml vars).
# Fails loud on a bad overlay (unknown marker) BEFORE anything deploys. Needs a
# render binary — skipped in FILES_ONLY unless DOCKWRIGHT_ORCH_BIN provides one.
if [ -n "$RENDER_BIN" ]; then
mkdir -p "$CLAUDE_DIR/agents"
"$RENDER_BIN" compose --core-dir "$REPO_DIR/deploy/agents" --out-dir "$CLAUDE_DIR/agents"
echo "→ Composed agent definitions to $CLAUDE_DIR/agents/"

if [ "$CODEX_PRESENT" = "1" ]; then
mkdir -p "$CODEX_DIR/agents"
# Mirror is scoped to the composed CORE files named by the compose stamp — never
# the whole ~/.claude/agents/ dir, which may also hold foreign agent files
# deployed by other repos.
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

# 4. Deploy slash commands through the render seam (both Claude + Codex dests).
# var-free commands render byte-identically, so today's deploys stay byte-stable.
# Without a render binary (FILES_ONLY, no DOCKWRIGHT_ORCH_BIN) fall back to a
# verbatim cp — the render transform is skipped but the files still deploy.
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
    # Per-file (not glob-cp) so backup_then_cp can preserve any operator hand-edit
    # of a deployed command before overwriting.
    for f in "$REPO_DIR/deploy/commands/"*.md; do
        [ -e "$f" ] || continue
        backup_then_cp "$f" "$CLAUDE_DIR/commands/$(basename "$f")"
        if [ "$CODEX_PRESENT" = "1" ]; then
            backup_then_cp "$f" "$CODEX_DIR/commands/$(basename "$f")"
        fi
    done
    echo "→ Installed slash commands (verbatim; no render binary) to $CLAUDE_DIR/commands/"
fi

# Overlay operator commands (drop-ins deployed AFTER the core commands). Runs on
# EVERY overlay install, so back up any hand-edited copy before overwriting.
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

# Codex exposes these same entry points as user-invocable skills. Generate them
# from wrapper sources STAGED in a temp dir — the core RENDERED commands plus
# the overlay verbatim commands (overlay overwrites same-stem core, matching the
# deployed dir's precedence). Scoping to a staging dir instead of globbing the
# deployed $CODEX_DIR/commands is deliberate: on an operator machine that dir
# ALSO holds foreign commands from other deployers, and install-codex-skills
# unconditionally overwrites <stem>/SKILL.md — a deployed-dir glob would clobber
# their wrappers. Same hazard the compose-stamp-scoped codex agent mirror guards
# against. Needs a render binary; skipped without one.
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

# 4a'. Copy Claude skills shipped by this repo (e.g. dockwright-gardener-digest).
if [ -d "$REPO_DIR/deploy/skills" ]; then
    mkdir -p "$CLAUDE_DIR/skills"
    rsync -a "$REPO_DIR/deploy/skills/" "$CLAUDE_DIR/skills/"
    echo "→ Installed Claude skills to $CLAUDE_DIR/skills/"
fi

# 4b. Copy helper scripts (stale-monitor, preflight, bootstrap-recreate, etc.)
mkdir -p "$CLAUDE_DIR/scripts"
cp "$REPO_DIR/deploy/scripts/"*.py "$CLAUDE_DIR/scripts/"
cp "$REPO_DIR/deploy/scripts/"*.sh "$CLAUDE_DIR/scripts/"
# .cjs helpers are require-loaded Node modules, not executed — no chmod +x.
cp "$REPO_DIR/deploy/scripts/"*.cjs "$CLAUDE_DIR/scripts/" 2>/dev/null || true
# stale_monitor.py lives in the package since the OSS split (Step 1) but keeps
# shipping to ~/.claude/scripts/ as a standalone stdlib-only script.
cp "$REPO_DIR/src/dockwright/stale_monitor.py" "$CLAUDE_DIR/scripts/stale_monitor.py"
chmod +x "$CLAUDE_DIR/scripts/"*.py "$CLAUDE_DIR/scripts/"*.sh
echo "→ Installed dockwright helper scripts to $CLAUDE_DIR/scripts/"

# 4b*. Stamp deployed script copies (.py/.sh ONLY — .md is exempt, a header line
# would enter agent/command context and agents already carry the compose-stamp
# sidecar) with a provenance line naming the canon source + sha they came from.
# Inserted right after the shebang (line 1) so the copy stays runnable standalone.
# Idempotent: a prior deploy's `# deployed-from:` line is REPLACED, not duplicated.
stamp_provenance() {
    # $1 = deployed file path, $2 = canon source relpath shown in the message
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
# Iterate SOURCE basenames (repo deploy/scripts/), NEVER the target dir:
# ~/.claude/scripts/ also holds operator-personal scripts deployed by OTHER
# repos (e.g. claude-config's archive-dialog.py / auto-commit-on-edit.sh) —
# a target-dir glob would stamp those with false provenance pointing at
# deploy/scripts/ paths that don't exist. Only files THIS repo just cp'd
# get stamped; stale_monitor.py is source-anchored to its cp above.
for f in "$REPO_DIR/deploy/scripts/"*.py "$REPO_DIR/deploy/scripts/"*.sh; do
    name="$(basename "$f")"
    stamp_provenance "$CLAUDE_DIR/scripts/$name" "deploy/scripts/$name"
done
stamp_provenance "$CLAUDE_DIR/scripts/stale_monitor.py" "src/dockwright/stale_monitor.py"
echo "→ Stamped provenance headers on deployed scripts"

# Overlay operator scripts (deployed AFTER the core scripts; .py/.sh made
# executable and provenance-stamped, mirroring the core script deploy above).
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

# 4b'. Deploy statusline-command.sh (manager session renders worker-count badge).
# User-customizable surface → back up any hand-edited copy before overwriting.
backup_then_cp "$REPO_DIR/deploy/statusline-command.sh" "$CLAUDE_DIR/statusline-command.sh"
chmod +x "$CLAUDE_DIR/statusline-command.sh"
echo "→ Installed statusline-command.sh to $CLAUDE_DIR/"

# 4b''. Deploy the loops registry (read by ~/.claude/scripts/loops_status.py and the
# background-loops rule; source of truth is deploy/loops-registry.md here).
mkdir -p "$CLAUDE_DIR/dockwright"
cp "$REPO_DIR/deploy/loops-registry.md" "$CLAUDE_DIR/dockwright/loops-registry.md"
echo "→ Installed loops-registry.md to $CLAUDE_DIR/dockwright/"

# 4b'''. Deploy the dedicated tmux-server config (loaded via `tmux -L dockwright
# -f <this>` at server birth when the terminal-backend flag = tmux).
mkdir -p "$CLAUDE_DIR/dockwright"
cp "$REPO_DIR/deploy/tmux/dockwright.conf" "$CLAUDE_DIR/dockwright/dockwright.tmux.conf"
echo "→ Installed tmux config to $CLAUDE_DIR/dockwright/dockwright.tmux.conf"
cp "$REPO_DIR/deploy/tmux/status_row.py" "$CLAUDE_DIR/dockwright/status_row.py"
chmod +x "$CLAUDE_DIR/dockwright/status_row.py"
echo "→ Installed status_row.py to $CLAUDE_DIR/dockwright/status_row.py"

# 4c. Mirror worker-spawn presets (referenced by spawn_worker's preset= kwarg).
# Use rsync --delete so removed/renamed presets in the source are pruned from the dest;
# plain `cp` would leave stale files behind after a rename.
mkdir -p "$CLAUDE_DIR/dockwright/presets"
rsync -a --delete "$REPO_DIR/deploy/presets/" "$CLAUDE_DIR/dockwright/presets/"
# .md presets carry the deploy-time {{vars}} seam — render them over the verbatim
# rsync copies (var-free presets render byte-identically). Skipped without a
# render binary — the rsync copies are then the raw fallback.
if [ -n "$RENDER_BIN" ]; then
    "$RENDER_BIN" render --src "$REPO_DIR/deploy/presets" --out "$CLAUDE_DIR/dockwright/presets" --glob '*.md'
fi
echo "→ Installed worker-spawn presets to $CLAUDE_DIR/dockwright/presets/"

# Overlay operator presets (deployed AFTER the core rsync --delete above, so the
# prune step doesn't wipe them). Present only when the overlay ships presets/.
if [ -d "$OVERLAY_DIR/presets" ]; then
    cp "$OVERLAY_DIR/presets/"* "$CLAUDE_DIR/dockwright/presets/"
    echo "→ Installed overlay presets to $CLAUDE_DIR/dockwright/presets/"
fi

# Finalize the headless worker preset: inject operator-absolute
# permissions.additionalDirectories resolved from dockwright.toml [paths]
# (repo_roots + worktree_roots + worker_home). Tilde in settings values is
# undocumented, so the shipped fixture stays generic and the deployed copy
# gets absolute paths here. Runs AFTER the overlay copy: an operator preset
# that already pins the key (even []) is respected — inject-only-if-absent —
# while one overlaid merely for extra allow rules still gets the fix.
if [ -n "$RENDER_BIN" ]; then
    "$RENDER_BIN" finalize-presets --file "$CLAUDE_DIR/dockwright/presets/worker-headless-settings.json"
fi

# 4d. Ensure the manager notebook dirs exist (planned/conditional-work agenda,
# read at manager boot; manager notebook agenda). Contents are runtime
# state owned by manager sessions — never copied or pruned from the repo.
mkdir -p "$CLAUDE_DIR/dockwright/notebook/archive"

# FILES_ONLY (S6 sandbox) skips MCP registration + hook wiring — both mutate the
# tester's real Claude/Codex config (claude mcp add, ~/.claude/settings.json).
if [ "${DOCKWRIGHT_SETUP_FILES_ONLY:-}" != "1" ]; then
# 5. Register the MCP server with Claude and Codex.
#    Claude writes to ~/.claude.json under user scope.
#    Settings.json's mcpServers key is NOT read by Claude Code — only `claude mcp add` works.
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

# 6. Wire dockwright hooks (explicit path) into Claude + Codex
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

# 8. Create dockwright state dir
mkdir -p "$CLAUDE_DIR/dockwright/active" "$CLAUDE_DIR/dockwright/questions" "$CLAUDE_DIR/dockwright/answers" "$CLAUDE_DIR/dockwright/done" "$CLAUDE_DIR/dockwright/handoffs"
echo "→ Created $CLAUDE_DIR/dockwright/ state directories"

# 9. Stamp deploy provenance (computed at the top of this script)
mkdir -p "$CLAUDE_DIR/dockwright"
{
    echo "sha=$DEPLOY_SHA"
    echo "branch=$DEPLOY_BRANCH"
    echo "dirty=$DEPLOY_DIRTY"
    echo "date=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "repo_dir=$REPO_DIR"
} > "$DEPLOY_STAMP"
echo "→ Stamped deploy provenance to $DEPLOY_STAMP (sha=$DEPLOY_SHA branch=$DEPLOY_BRANCH dirty=$DEPLOY_DIRTY)"

# FILES_ONLY (S6 sandbox) skips doctor (needs $DOCKWRIGHT_BIN + the wired config it
# verifies) and the overlay setup.d runner below.
if [ "${DOCKWRIGHT_SETUP_FILES_ONLY:-}" != "1" ]; then
# Ensure the default/configured worker home exists so a bare spawn_worker never
# falls back to the manager's (untrusted) cwd on a fresh install (fix M-1). NOT
# RENDER_BIN-gated: the S6 sandbox sets DOCKWRIGHT_ORCH_BIN but neither HOME nor
# CLAUDE_ORCH_WORKER_HOME, so a RENDER_BIN gate would mkdir the operator's real
# worker home during FILES_ONLY tests. The FILES_ONLY guard is the machine-mutation seam.
WORKER_HOME="$("$DOCKWRIGHT_BIN" ensure-worker-home || true)"
[ -n "$WORKER_HOME" ] && echo "→ Ensured worker home exists: $WORKER_HOME"

echo "→ Verifying environment wiring (dockwright doctor)…"
DOCTOR_ARGS=(--orch-bin "$DOCKWRIGHT_BIN" --claude-json "$HOME/.claude.json" --settings "$SETTINGS"
    --brew-prefix "$(brew --prefix 2>/dev/null || echo /opt/homebrew)"
    --dist-name dockwright --server-name dockwright --strict)
DOCTOR_ARGS+=(--compose-core-dir "$REPO_DIR/deploy/agents" --compose-out-dir "$CLAUDE_DIR/agents")
[ -f "$CODEX_DIR/hooks.json" ] && DOCTOR_ARGS+=(--codex-hooks "$CODEX_DIR/hooks.json")
[ -f "$CODEX_DIR/config.toml" ] && DOCTOR_ARGS+=(--codex-config "$CODEX_DIR/config.toml")
"$DOCKWRIGHT_BIN" doctor "${DOCTOR_ARGS[@]}"
echo "→ Environment wiring verified."

# Overlay setup.d hooks — arbitrary operator install steps (the SSH url rewrite
# moves here in Task 2). Sorted (glob expands sorted), run last of everything.
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
