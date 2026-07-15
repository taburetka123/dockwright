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
#   bootstrap-recreate.sh --narrative "<prose>" --from-sid <sid> [--reason <string>]
#
# Defaults:
#   --reason   bootstrap

set -euo pipefail

NARRATIVE=""
FROM_SID=""
REASON="bootstrap"

while [ $# -gt 0 ]; do
    case "$1" in
        --narrative)
            NARRATIVE="$2"; shift 2 ;;
        --from-sid)
            FROM_SID="$2"; shift 2 ;;
        --reason)
            REASON="$2"; shift 2 ;;
        *)
            echo "ERROR: unknown arg '$1'" >&2
            echo "Usage: $0 --narrative <prose> --from-sid <sid> [--reason <string>]" >&2
            exit 2 ;;
    esac
done

if [ -z "$NARRATIVE" ] || [ -z "$FROM_SID" ]; then
    echo "ERROR: --narrative and --from-sid are required" >&2
    echo "Usage: $0 --narrative <prose> --from-sid <sid> [--reason <string>]" >&2
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
RUNTIME_CMD="claude --model 'opus[1m]' '/manager-resume $HANDOFF_ID'"

# Login model: the recreated manager rides the active pointer. a -> default
# ~/.claude (no CLAUDE_CONFIG_DIR); b -> ~/.claude-b iff its .claude.json carries
# the orchestrator MCP (built/maintained by worker spawns), else fall back to the
# default login. No token is injected — the per-config-dir keychain login authenticates.
CONFIG_PREFIX=""
ACCOUNT_ACTIVE_FILE="$HOME/.claude/dockwright/account-active"
if [ -s "$ACCOUNT_ACTIVE_FILE" ]; then
    ACTIVE_LETTER=$(tr -d '\n' < "$ACCOUNT_ACTIVE_FILE" || true)
    if [ "$ACTIVE_LETTER" = "a" ]; then
        CONFIG_PREFIX='CLAUDE_ORCH_ACCOUNT=a '
    elif [ "$ACTIVE_LETTER" = "b" ]; then
        FARM="$HOME/.claude-b"
        if [ -f "$FARM/.claude.json" ] && jq -e '.mcpServers["dockwright"] // .mcpServers["claude-orchestrator"]' "$FARM/.claude.json" >/dev/null 2>&1; then
            CONFIG_PREFIX="CLAUDE_CONFIG_DIR=$FARM CLAUDE_ORCH_ACCOUNT=b "
        else
            CONFIG_PREFIX='CLAUDE_ORCH_ACCOUNT=a '   # b farm not ready -> default login
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
