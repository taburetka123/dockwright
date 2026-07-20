#!/usr/bin/env bash
# bootstrap-recreate.sh — manual manager handoff for OLD sessions whose MCP
# server booted BEFORE prepare_handoff / spawn_replacement_manager shipped.
#
# Chicken-and-egg: those old sessions cannot call the new tools to recreate
# themselves. This script does the same work via plain shell:
#   1. Synthesize a handoff JSON matching prepare_handoff_impl's shape.
#   2. tmux: spawn a new window running the Claude CLI with
#      `/manager-resume <handoff_id>`.
#   3. The new manager's MCP server has fresh code, so it can call
#      become_manager_with_takeover normally — that SIGTERMs this old session.
#
# Managers are Claude-only; the replacement always launches `claude`.
#
# Usage:
#   bootstrap-recreate.sh --narrative "<prose>" --from-sid <sid> [--reason <string>] [--dry-run]
#
# Defaults:
#   --reason   bootstrap
#
# ⚠️  Executing this script SPAWNS A REAL MANAGER onto the LIVE tmux socket by
#     default (TMUX_SOCK defaults to `dockwright`; -L namespaces by uid, not HOME,
#     so a sandboxed HOME does NOT isolate it). Any probe or test that runs this
#     script MUST pass --dry-run, which prints the resolved spawn plan and exits
#     BEFORE touching tmux. (2026-07-17 incident: a sandbox-HOME probe without
#     --dry-run put two rogue `claude /manager-resume` managers on the operator's
#     live fleet.)

set -euo pipefail

NARRATIVE=""
FROM_SID=""
REASON="bootstrap"
DRY_RUN=""

while [ $# -gt 0 ]; do
    case "$1" in
        --narrative)
            NARRATIVE="$2"; shift 2 ;;
        --from-sid)
            FROM_SID="$2"; shift 2 ;;
        --reason)
            REASON="$2"; shift 2 ;;
        --dry-run)
            DRY_RUN=1; shift ;;
        *)
            echo "ERROR: unknown arg '$1'" >&2
            echo "Usage: $0 --narrative <prose> --from-sid <sid> [--reason <string>] [--dry-run]" >&2
            exit 2 ;;
    esac
done

if [ -z "$NARRATIVE" ] || [ -z "$FROM_SID" ]; then
    echo "ERROR: --narrative and --from-sid are required" >&2
    echo "Usage: $0 --narrative <prose> --from-sid <sid> [--reason <string>] [--dry-run]" >&2
    exit 2
fi

ORCH_DIR="$HOME/.claude/dockwright"
HANDOFFS_DIR="$ORCH_DIR/handoffs"
ACTIVE_DIR="$ORCH_DIR/active"
QUESTIONS_DIR="$ORCH_DIR/questions"

mkdir -p "$HANDOFFS_DIR"

HANDOFF_ID=$(uuidgen | tr -d - | tr '[:upper:]' '[:lower:]')
NOW=$(python3 -c 'import time; print(time.time())')

WORKERS_JSON='[]'
if [ -d "$ACTIVE_DIR" ] && compgen -G "$ACTIVE_DIR/*.json" >/dev/null; then
    WORKERS_JSON=$(cat "$ACTIVE_DIR"/*.json 2>/dev/null | jq -s '[.[] | select(.agent == "worker")]')
fi

QUESTIONS_JSON='[]'
if [ -d "$QUESTIONS_DIR" ]; then
    QUESTIONS_JSON=$(python3 - "$QUESTIONS_DIR" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
records = []
for path in root.rglob("*.json"):
    try:
        records.append(json.loads(path.read_text()))
    except Exception:
        pass
records.sort(key=lambda r: r.get("asked_at") or 0)
print(json.dumps(records))
PY
)
fi

HANDOFF_PATH="$HANDOFFS_DIR/$HANDOFF_ID.json"

jq -n \
    --arg handoff_id "$HANDOFF_ID" \
    --arg from_sid "$FROM_SID" \
    --argjson prepared_at "$NOW" \
    --arg trigger_reason "$REASON" \
    --arg narrative "$NARRATIVE" \
    --argjson workers "$WORKERS_JSON" \
    --argjson questions "$QUESTIONS_JSON" \
    '{
        handoff_id: $handoff_id,
        from_sid: $from_sid,
        to_sid: null,
        prepared_at: $prepared_at,
        consumed_at: null,
        trigger_reason: $trigger_reason,
        narrative_summary: $narrative,
        workers_snapshot: $workers,
        questions_snapshot: $questions
    }' > "$HANDOFF_PATH.tmp"
mv "$HANDOFF_PATH.tmp" "$HANDOFF_PATH"

CWD=$(pwd)
# Manager lane is pinned (orch-audit model-allocation): never inherit the
# user's interactive model default. Single-quoted so the -ic shell can't glob [1m].
# E2E F-2: ride the deployed manager allowlist when present so the resumed boot
# clears its approval prompts; absent (setup.sh not run) = old behavior.
MANAGER_SETTINGS="$ORCH_DIR/presets/manager-settings.json"
# Same argv tail as manager_launch.manager_claude_args() (inline copy — this
# script is standalone bash): remote control default-ON via the reliable
# --remote-control flag; DOCKWRIGHT_MANAGER_RC=0 opts out. Keep in sync.
# RC_ARG goes before --model: --remote-control [name] would bind the trailing
# /manager-resume prompt as the RC name otherwise (see manager_claude_args docstring).
RC_ARG=""
if [ "${DOCKWRIGHT_MANAGER_RC:-1}" != "0" ]; then
    RC_ARG="--remote-control "
fi
# OPT-IN, default OFF (inline copy of manager_claude_args(), keep in sync):
# DOCKWRIGHT_MANAGER_SKIP_PERMS=1 removes the Bash safety classifier for the
# recreated manager — sanctioned ONLY for manager.core.md's two named uses
# (classifier outage; sandbox-E2E/publish host driver). Bare flag: parse-safe.
SKIP_ARG=""
if [ "${DOCKWRIGHT_MANAGER_SKIP_PERMS:-}" = "1" ]; then
    SKIP_ARG="--dangerously-skip-permissions "
fi
# One-shot guarantee: SKIP_ARG above has captured the flag (RUNTIME_CMD below
# interpolates the shell var, not the env); unset so a tmux SERVER born by the
# spawn tail cannot inherit the var (server env outlives this shell) and make
# every future recreate skip-permissions.
unset DOCKWRIGHT_MANAGER_SKIP_PERMS
if [ -f "$MANAGER_SETTINGS" ]; then
    RUNTIME_CMD="claude ${RC_ARG}${SKIP_ARG}--model 'opus[1m]' --settings '$MANAGER_SETTINGS' '/manager-resume $HANDOFF_ID'"
else
    RUNTIME_CMD="claude ${RC_ARG}${SKIP_ARG}--model 'opus[1m]' '/manager-resume $HANDOFF_ID'"
fi

# Login model: the recreated manager rides the active pointer against the
# registry account list (account-registry.json — names in order, default,
# config_dir overrides; absent falls back to the historical a/b pair). The
# default registry account rides ~/.claude (no CLAUDE_CONFIG_DIR); every
# other registry account rides its own CLAUDE_CONFIG_DIR farm (registry
# config_dir override if set, else the ~/.claude-<name> convention) iff its
# .claude.json carries the orchestrator MCP (built/maintained by worker
# spawns), else fall back to the default login. No token is injected — the
# per-config-dir keychain login authenticates.
CONFIG_PREFIX=""
ACCOUNT_ACTIVE_FILE="$HOME/.claude/dockwright/account-active"
ACCOUNT_REGISTRY_FILE="$HOME/.claude/dockwright/account-registry.json"
if [ -s "$ACCOUNT_ACTIVE_FILE" ]; then
    ACTIVE_LETTER=$(tr -d '\n' < "$ACCOUNT_ACTIVE_FILE" || true)
    DEFAULT_ACCOUNT="a"
    FARM_OVERRIDE=""
    if [ -f "$ACCOUNT_REGISTRY_FILE" ]; then
        DEFAULT_ACCOUNT=$(jq -r '.default // "a"' "$ACCOUNT_REGISTRY_FILE" 2>/dev/null || echo a)
        FARM_OVERRIDE=$(jq -r --arg n "$ACTIVE_LETTER" \
            '.pool[]? | select(.name == $n) | .config_dir // empty' \
            "$ACCOUNT_REGISTRY_FILE" 2>/dev/null || true)
    fi
    if [ "$ACTIVE_LETTER" = "$DEFAULT_ACCOUNT" ]; then
        CONFIG_PREFIX="CLAUDE_ORCH_ACCOUNT=$ACTIVE_LETTER "
    else
        FARM="${FARM_OVERRIDE:-$HOME/.claude-$ACTIVE_LETTER}"
        if [ -f "$FARM/.claude.json" ] && jq -e '.mcpServers["dockwright"] // .mcpServers["claude-orchestrator"]' "$FARM/.claude.json" >/dev/null 2>&1; then
            CONFIG_PREFIX="CLAUDE_CONFIG_DIR=$FARM CLAUDE_ORCH_ACCOUNT=$ACTIVE_LETTER "
        else
            CONFIG_PREFIX="CLAUDE_ORCH_ACCOUNT=$DEFAULT_ACCOUNT "   # farm not ready -> default login
        fi
    fi
fi

TMUX_SOCK="${DOCKWRIGHT_TMUX_SOCKET:-${CLAUDE_ORCH_TMUX_SOCKET:-dockwright}}"
TMUX_CONF_FILE="$HOME/.claude/dockwright/dockwright.tmux.conf"
# Pre-rename conf homes — retire with CLAUDE_ORCH_TMUX_SOCKET (one release).
TMUX_CONF_LEGACY="$HOME/.claude/orchestrator/dockwright.tmux.conf"
TMUX_CONF_LEGACY2="$HOME/.claude/orchestrator/claude-orch.tmux.conf"
FFLAG=()
if [ -f "$TMUX_CONF_FILE" ]; then FFLAG=(-f "$TMUX_CONF_FILE")
elif [ -f "$TMUX_CONF_LEGACY" ]; then FFLAG=(-f "$TMUX_CONF_LEGACY")
elif [ -f "$TMUX_CONF_LEGACY2" ]; then FFLAG=(-f "$TMUX_CONF_LEGACY2"); fi
if [ -n "$DRY_RUN" ]; then
    echo "DRY_RUN: no spawn. socket=$TMUX_SOCK config_prefix=[$CONFIG_PREFIX] cmd=[$RUNTIME_CMD]"
    echo "handoff_id: $HANDOFF_ID"
    echo "handoff_path: $HANDOFF_PATH"
    exit 0
fi
# Refuse the incident shape: a sandboxed HOME does NOT isolate tmux (-L
# namespaces by uid, not HOME) — a probe run under HOME=/tmp/... would spawn
# onto the LIVE socket (2026-07-17: two rogue managers). Real runs always have
# HOME == the uid's passwd home; probes must use --dry-run. The socket gate is a
# deliberate refinement: a sandboxed-HOME run against an EXPLICIT scratch socket
# is a legitimate test shape, so the guard fires only on sandbox-HOME +
# live/default socket, the exact incident shape.
if [ "$HOME" != "$(eval echo ~"$(id -un)")" ]; then
    case "$TMUX_SOCK" in
        dockwright|claude-orch)
            echo "ERROR: \$HOME ($HOME) is not the uid's real home — refusing to spawn onto live socket '$TMUX_SOCK'. Use --dry-run to probe, or set DOCKWRIGHT_TMUX_SOCKET to a scratch socket." >&2
            exit 3 ;;
    esac
fi
if tmux -L "$TMUX_SOCK" has-session -t mgr 2>/dev/null; then
    TMUX_HEAD=(new-window -d -t mgr)
else
    TMUX_HEAD=(new-session -d -s mgr)
fi
SPAWN_SHELL="$(command -v zsh || command -v bash || echo sh)"
WINDOW_ID=$(tmux -L "$TMUX_SOCK" ${FFLAG[@]+"${FFLAG[@]}"} "${TMUX_HEAD[@]}" \
    -n "manager (incoming)" -c "$CWD" -P -F '#{pane_id}' -- \
    "$SPAWN_SHELL" -ic "${CONFIG_PREFIX}CLAUDE_AGENT=manager CLAUDE_WORKER_NAME=manager $RUNTIME_CMD")

echo "handoff_id: $HANDOFF_ID"
echo "handoff_path: $HANDOFF_PATH"
echo "new window_id: $WINDOW_ID"
echo ""
echo "The new manager will call become_manager_with_takeover and SIGTERM this session."
