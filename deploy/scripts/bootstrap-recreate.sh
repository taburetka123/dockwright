#!/usr/bin/env bash

set -euo pipefail

NARRATIVE=""
FROM_SID=""
MANAGER_NAME=""
DOMAIN=""
REASON="bootstrap"
DRY_RUN=""

while [ $# -gt 0 ]; do
    case "$1" in
        --narrative|--from-sid|--reason|--manager-name|--domain)
            if [ $# -lt 2 ] || [ "${2#--}" != "$2" ]; then
                echo "ERROR: $1 requires a value (got '${2:-}')" >&2
                echo "Usage: $0 --narrative <prose> --from-sid <sid> [--manager-name <name>] [--domain <domain>] [--reason <string>] [--dry-run]" >&2
                exit 2
            fi
            case "$1" in
                --narrative) NARRATIVE="$2" ;;
                --from-sid) FROM_SID="$2" ;;
                --reason) REASON="$2" ;;
                --manager-name) MANAGER_NAME="$2" ;;
                --domain) DOMAIN="$2" ;;
                *) echo "internal: unhandled value flag $1" >&2; exit 2 ;;
            esac
            shift 2 ;;
        --dry-run)
            DRY_RUN=1; shift ;;
        *)
            echo "ERROR: unknown arg '$1'" >&2
            echo "Usage: $0 --narrative <prose> --from-sid <sid> [--manager-name <name>] [--domain <domain>] [--reason <string>] [--dry-run]" >&2
            exit 2 ;;
    esac
done

if [ -z "$NARRATIVE" ] || [ -z "$FROM_SID" ]; then
    echo "ERROR: --narrative and --from-sid are required" >&2
    echo "Usage: $0 --narrative <prose> --from-sid <sid> [--manager-name <name>] [--domain <domain>] [--reason <string>] [--dry-run]" >&2
    exit 2
fi

ORCH_DIR="$HOME/.claude/dockwright"
HANDOFFS_DIR="$ORCH_DIR/handoffs"
ACTIVE_DIR="$ORCH_DIR/active"
QUESTIONS_DIR="$ORCH_DIR/questions"

FROM_RECORD="$ACTIVE_DIR/$FROM_SID.json"
RECORD_AGENT=""
if [ -f "$FROM_RECORD" ]; then
    RECORD_AGENT=$(jq -r '.agent // empty' "$FROM_RECORD" 2>/dev/null || true)
fi
if [ "$RECORD_AGENT" = "manager" ]; then
    if [ -z "$MANAGER_NAME" ]; then
        MANAGER_NAME=$(jq -r '.name // empty' "$FROM_RECORD" 2>/dev/null || true)
    fi
    if [ -z "$DOMAIN" ]; then
        DOMAIN=$(jq -r '.domain // empty' "$FROM_RECORD" 2>/dev/null || true)
    fi
fi
MISSING=""
if [ -z "$MANAGER_NAME" ]; then MISSING="manager_name"; fi
if [ -z "$DOMAIN" ]; then MISSING="${MISSING:+$MISSING and }domain"; fi
if [ -n "$MISSING" ]; then
    echo "ERROR: cannot resolve $MISSING for predecessor $FROM_SID (probed $FROM_RECORD)." >&2
    echo "A handoff without them silently re-rolls the successor's identity/domain and strands its workers." >&2
    echo "Pass --manager-name <name> / --domain <domain> explicitly (recover the name from workers'" >&2
    echo "parent_manager_name in ~/.claude/dockwright/active/*.json, done/<name>/ bucket names, the" >&2
    echo "domain notebook, or spend-ledger drop events)." >&2
    exit 4
fi

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

HANDOFF_JSON=$(jq -cn \
    --arg handoff_id "$HANDOFF_ID" \
    --arg from_sid "$FROM_SID" \
    --argjson prepared_at "$NOW" \
    --arg trigger_reason "$REASON" \
    --arg narrative "$NARRATIVE" \
    --arg manager_name "$MANAGER_NAME" \
    --arg domain "$DOMAIN" \
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
        manager_name: $manager_name,
        domain: $domain,
        workers_snapshot: $workers,
        questions_snapshot: $questions
    }')

CWD=$(pwd)
MANAGER_SETTINGS="$ORCH_DIR/presets/manager-settings.json"
RC_ARG=""
if [ "${DOCKWRIGHT_MANAGER_RC:-1}" != "0" ]; then
    RC_ARG="--remote-control "
fi
SKIP_ARG=""
if [ "${DOCKWRIGHT_MANAGER_SKIP_PERMS:-}" = "1" ]; then
    SKIP_ARG="--dangerously-skip-permissions "
fi
unset DOCKWRIGHT_MANAGER_SKIP_PERMS
if [ -f "$MANAGER_SETTINGS" ]; then
    RUNTIME_CMD="claude ${RC_ARG}${SKIP_ARG}--model 'claude-opus-5[1m]' --settings '$MANAGER_SETTINGS' '/manager-resume $HANDOFF_ID'"
else
    RUNTIME_CMD="claude ${RC_ARG}${SKIP_ARG}--model 'claude-opus-5[1m]' '/manager-resume $HANDOFF_ID'"
fi

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
            CONFIG_PREFIX="CLAUDE_ORCH_ACCOUNT=$DEFAULT_ACCOUNT "
        fi
    fi
fi

TMUX_SOCK="${DOCKWRIGHT_TMUX_SOCKET:-${CLAUDE_ORCH_TMUX_SOCKET:-dockwright}}"
TMUX_CONF_FILE="$HOME/.claude/dockwright/dockwright.tmux.conf"
TMUX_CONF_LEGACY="$HOME/.claude/orchestrator/dockwright.tmux.conf"
TMUX_CONF_LEGACY2="$HOME/.claude/orchestrator/claude-orch.tmux.conf"
FFLAG=()
if [ -f "$TMUX_CONF_FILE" ]; then FFLAG=(-f "$TMUX_CONF_FILE")
elif [ -f "$TMUX_CONF_LEGACY" ]; then FFLAG=(-f "$TMUX_CONF_LEGACY")
elif [ -f "$TMUX_CONF_LEGACY2" ]; then FFLAG=(-f "$TMUX_CONF_LEGACY2"); fi
if [ -n "$DRY_RUN" ]; then
    echo "DRY_RUN: no spawn. socket=$TMUX_SOCK config_prefix=[$CONFIG_PREFIX] cmd=[$RUNTIME_CMD]"
    echo "handoff_id: $HANDOFF_ID"
    echo "handoff_path: (dry-run, not written) $HANDOFF_PATH"
    echo "handoff_payload: $HANDOFF_JSON"
    exit 0
fi
if [ "$HOME" != "$(eval echo ~"$(id -un)")" ]; then
    case "$TMUX_SOCK" in
        dockwright|claude-orch)
            echo "ERROR: \$HOME ($HOME) is not the uid's real home — refusing to spawn onto live socket '$TMUX_SOCK'. Use --dry-run to probe, or set DOCKWRIGHT_TMUX_SOCKET to a scratch socket." >&2
            exit 3 ;;
    esac
fi
mkdir -p "$HANDOFFS_DIR"
printf '%s\n' "$HANDOFF_JSON" > "$HANDOFF_PATH.tmp"
mv "$HANDOFF_PATH.tmp" "$HANDOFF_PATH"
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
