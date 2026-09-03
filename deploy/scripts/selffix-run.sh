#!/usr/bin/env bash

set -u

TRANSCRIPT="${1:?transcript path required}"
SESSION_ID="${2:?session id required}"

LOG="$HOME/.claude/dockwright/selffix/trigger.log"
DEBUG=0
if [ -f "$HOME/.claude/dockwright/selffix/debug" ] || [ -f "$HOME/.claude/selffix-debug" ] || [ "${SELFFIX_DEBUG:-}" = "1" ]; then
  DEBUG=1
  mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
fi
TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
worker_log() {
  [ "$DEBUG" = "1" ] || return 0
  echo "$(TS)  worker:$1  ${SESSION_ID}  ${2:-}" >> "$LOG"
}

retry_log() {
  [ "$DEBUG" = "1" ] || return 0
  echo "$(TS)  $1  ${SESSION_ID}  ${2:-}" >> "$LOG"
}

_SELFFIX_RUN_SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=loop-label-prefix.sh
. "$_SELFFIX_RUN_SD/loop-label-prefix.sh" 2>/dev/null || true
if command -v dockwright_module_enabled >/dev/null 2>&1 && ! dockwright_module_enabled gardener; then
  worker_log "module-off" "[modules] gardener=false — retro skipped"
  exit 0
fi

RETRY_LIB="$HOME/.claude/scripts/selffix-retry-lib.sh"
[ -f "$RETRY_LIB" ] && . "$RETRY_LIB"
RETRY_ATTEMPT="${SELFFIX_RETRY_ATTEMPT:-0}"
case "$RETRY_ATTEMPT" in (''|*[!0-9]*) RETRY_ATTEMPT=0 ;; esac

enqueue_retry() {
  if [ "$RETRY_ATTEMPT" -ge 1 ]; then
    retry_log "retry:exhausted" "reason=$1"
    return 0
  fi
  if command -v selffix_enqueue_retry >/dev/null 2>&1 \
     && selffix_enqueue_retry "$SESSION_ID" "$TRANSCRIPT" "$1"; then
    retry_log "retry:enqueued" "reason=$1 attempts=0"
  else
    retry_log "retry:enqueue-failed" "reason=$1"
  fi
}

OUT_DIR="$HOME/.claude/dockwright/selffix/findings"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/${SESSION_ID}.md"

worker_log "started" "transcript=$TRANSCRIPT pid=$$"

if [ ! -f "$TRANSCRIPT" ]; then
  worker_log "error" "transcript-missing"
  exit 0
fi

SIGNAL_LIB="$_SELFFIX_RUN_SD/transcript_signal.py"
[ -f "$SIGNAL_LIB" ] || SIGNAL_LIB="$HOME/.claude/scripts/transcript_signal.py"
if [ -f "$SIGNAL_LIB" ]; then
  if ! /usr/bin/python3 "$SIGNAL_LIB" worth-retrospecting "$TRANSCRIPT" 2>/dev/null; then
    worker_log "skip" "no-model-turn — instruction-only transcript, not retrospected"
    exit 0
  fi
else
  worker_log "warn" "transcript-signal-missing $SIGNAL_LIB — run setup.sh; retro proceeding ungated"
fi

LOCK_DIR="$HOME/.claude/locks/analyst-run.lock"
RUNLOCK_LIB="$HOME/.claude/scripts/runlock.sh"
if [ ! -f "$RUNLOCK_LIB" ]; then
  worker_log "error" "runlock-lib-missing $RUNLOCK_LIB — deploy runlock.sh (setup.sh)"
  exit 0
fi
. "$RUNLOCK_LIB"
trap runlock_release EXIT INT TERM

LOCK_WAIT_MAX="${SELFFIX_LOCK_WAIT_MAX:-7200}"
if ! runlock_acquire "$LOCK_DIR" wait "$LOCK_WAIT_MAX"; then
  worker_log "error" "lock-timeout waited=${LOCK_WAIT_MAX}s — retro dropped, live holder kept its lock"
  enqueue_retry "lock-timeout"
  exit 0
fi
worker_log "lock-acquired" ""

: > "$OUT"

TIMEOUT_SEC="${SELFFIX_TIMEOUT_SEC:-1500}"
GRACE_SEC="${SELFFIX_GRACE_SEC:-30}"

set -m
SKILL_FILE="$HOME/.claude/skills/dockwright-selffix/SKILL.md"
if [ ! -f "$SKILL_FILE" ]; then
  worker_log "error" "skill-missing $SKILL_FILE — run setup.sh"
  echo "Status: error (skill-missing $SKILL_FILE)" >> "$OUT"
  enqueue_retry "skill-missing"
  exit 0
fi
PROMPT_FILE="$(mktemp "${TMPDIR:-/tmp}/selffix-prompt.XXXXXX")" || {
  worker_log "error" "prompt-file-mktemp-failed"
  echo "Status: error (prompt-file-mktemp-failed)" >> "$OUT"
  enqueue_retry "prompt-file"
  exit 0
}
_selffix_cleanup() { rm -f "$PROMPT_FILE" 2>/dev/null; runlock_release; }
trap _selffix_cleanup EXIT INT TERM
{
  cat "$SKILL_FILE"
  printf '\n\n---\nExecute the skill above now, in headless mode, with --transcript %s\n' \
    "$TRANSCRIPT"
} > "$PROMPT_FILE"
( exec env -u CLAUDE_AGENT -u CLAUDE_WORKER_NAME -u CLAUDE_PARENT_MANAGER -u CLAUDE_DOMAIN \
    claude -p \
    --model claude-sonnet-5 \
    --add-dir "$(dirname "$TRANSCRIPT")" \
    --allowedTools 'Bash(jq:*) Bash(wc:*) Bash(head:*) Bash(tail:*) Bash(grep:*)' \
    --tools "Bash,Read,Grep,Glob" \
    --strict-mcp-config \
    --mcp-config '{"mcpServers":{}}' \
    --setting-sources "" \
    --no-session-persistence \
    --disallowedTools "Write,Edit,NotebookEdit" \
    < "$PROMPT_FILE" > "$OUT" 2>&1 ) &
CHILD_PID=$!
PGID=$CHILD_PID

(
  sleep "$TIMEOUT_SEC"
  kill -TERM "-$PGID" 2>/dev/null
  sleep "$GRACE_SEC"
  kill -KILL "-$PGID" 2>/dev/null
) &
WATCHDOG_PID=$!

wait "$CHILD_PID"
EC=$?

kill "$WATCHDOG_PID" 2>/dev/null
wait "$WATCHDOG_PID" 2>/dev/null
set +m

if ! grep -q '^Status:' "$OUT"; then
  if [ "$EC" -eq 0 ]; then
    echo "" >> "$OUT"
    echo "Status: ok (exit=$EC)" >> "$OUT"
  else
    echo "" >> "$OUT"
    echo "Status: error (exit=$EC, watchdog=$TIMEOUT_SEC s)" >> "$OUT"
  fi
fi

OUT_BYTES=$(wc -c < "$OUT" | awk '{print $1}')
if [ "$EC" -ne 0 ]; then
  worker_log "finished-error" "exit=$EC bytes=$OUT_BYTES out=$OUT"
  enqueue_retry "finished-error"
elif tail -n 3 "$OUT" | grep -q '^Status: error'; then
  worker_log "finished-error" "exit=$EC status-error bytes=$OUT_BYTES out=$OUT"
  enqueue_retry "status-error"
elif [ "$OUT_BYTES" -lt 200 ]; then
  worker_log "finished" "exit=$EC bytes=$OUT_BYTES out=$OUT"
  enqueue_retry "stub"
else
  worker_log "finished" "exit=$EC bytes=$OUT_BYTES out=$OUT"
fi

exit 0
