#!/usr/bin/env bash
# Background worker: run /dockwright-selffix against a captured transcript and write
# findings to ~/.claude/dockwright/selffix/findings/<sessionId>.md. Mirrors the
# pr-review-run watchdog pattern: guarantees a Status: line on every exit path
# so consumers (status line, daily-review skill) can tell success from hang.
#
# Lifecycle events ("started", "finished", error reasons) are also appended to
# ~/.claude/dockwright/selffix/trigger.log so a tail of that file shows the full
# trigger -> spawn -> finish chain.
#
# Contract (see also ~/.claude/scripts/selffix-trigger.sh and
# ~/.claude/skills/dockwright-selffix/SKILL.md):
#   This worker captures `claude -p` stdout into $OUT. The skill MUST emit
#   findings to stdout only — no Write/Edit calls — otherwise findings
#   diverge from the file the worker wrote and the trigger's findings-exist
#   gate breaks.
#
# Retry queue (selffix-retry-lib.sh): failed runs (non-zero exit, <200-byte
# degenerate stub, lock-timeout) enqueue ONE durable retry entry into
# ~/.claude/dockwright/selffix/retry/<sid>.json; gardener_gate.py later re-spawns this
# script with SELFFIX_RETRY_ATTEMPT=1, which marks the gate's retry — a
# retried run that fails again logs retry:exhausted and never re-enqueues.
# Enqueue is best-effort: a failed enqueue never blocks the SessionEnd path.
#
# Canonical source: deploy/scripts/selffix-run.sh @
# taburetka123/claude-orchestrator — deployed by setup.sh. Edit the repo
# copy, not the deployed one.
#
# Usage: selffix-run.sh <transcript-path> <session-id>

set -u

TRANSCRIPT="${1:?transcript path required}"
SESSION_ID="${2:?session id required}"

LOG="$HOME/.claude/dockwright/selffix/trigger.log"
DEBUG=0
# deprecated, one release: legacy debug flag honored while docs/habits migrate
if [ -f "$HOME/.claude/dockwright/selffix/debug" ] || [ -f "$HOME/.claude/selffix-debug" ] || [ "${SELFFIX_DEBUG:-}" = "1" ]; then
  DEBUG=1
  # The module-off log line below can fire before OUT_DIR is created, so ensure
  # the nested log home exists.
  mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
fi
TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
worker_log() {
  [ "$DEBUG" = "1" ] || return 0
  echo "$(TS)  worker:$1  ${SESSION_ID}  ${2:-}" >> "$LOG"
}

retry_log() {
  # Same one-line format as the trigger's log_line — retry lifecycle verbs
  # (retry:enqueued / retry:exhausted) are not worker: events.
  [ "$DEBUG" = "1" ] || return 0
  echo "$(TS)  $1  ${SESSION_ID}  ${2:-}" >> "$LOG"
}

# [modules] gardener toggle: the retro worker is part of the Gardener pipeline,
# so gardener=false must no-op it (design-gate). Bail after arg parse, before
# creating the findings file / taking the mutex / spawning claude. Best-effort
# source — a missing lib means fail-open = enabled.
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
  # enqueue_retry <reason> — one durable retry per session. The gate retries
  # with SELFFIX_RETRY_ATTEMPT=1; a retried run that fails again (ANY path,
  # including lock-timeout) logs retry:exhausted and never re-enqueues.
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

# Transcript may have rotated away while this worker waited in the run queue.
# Don't leave a counted stub .md for that case — just log and bail (no $OUT yet).
if [ ! -f "$TRANSCRIPT" ]; then
  worker_log "error" "transcript-missing"
  exit 0
fi

# --- Single-runner mutex --------------------------------------------------
# Only one selffix worker runs `claude -p` at a time. SessionEnd can fire many
# retros at once; running their `claude -p` calls concurrently stampedes the
# Anthropic rate limiter and every worker dies with a 131-byte "rate limited"
# stub. Serializing keeps each retro isolated. Protocol lives in runlock.sh
# (atomic mkdir; steal only dead or over-aged holders; owner-checked release).
# wait-mode: a retro queues up to SELFFIX_LOCK_WAIT_MAX, then DROPS — it never
# breaks a live, in-budget holder. (The old 2h valve rm -rf'd live holders'
# locks and mutual exclusion collapsed under retro storms — arch review A5.)
# The 25m runtime watchdog below only kills the claude child's process group,
# so this script always reaches its EXIT trap and frees the lock on the
# normal + timeout paths.
LOCK_DIR="$HOME/.claude/locks/analyst-run.lock"
RUNLOCK_LIB="$HOME/.claude/scripts/runlock.sh"
if [ ! -f "$RUNLOCK_LIB" ]; then
  # Fail honestly: without this guard a missing lib makes runlock_acquire a
  # command-not-found and the error path mislabels it "lock-timeout" (the
  # test_high_signal_writes_findings_file_to_disk flake).
  worker_log "error" "runlock-lib-missing $RUNLOCK_LIB — deploy runlock.sh (setup.sh)"
  exit 0
fi
. "$RUNLOCK_LIB"
trap runlock_release EXIT INT TERM

LOCK_WAIT_MAX="${SELFFIX_LOCK_WAIT_MAX:-7200}"   # 2h queue budget
if ! runlock_acquire "$LOCK_DIR" wait "$LOCK_WAIT_MAX"; then
  worker_log "error" "lock-timeout waited=${LOCK_WAIT_MAX}s — retro dropped, live holder kept its lock"
  enqueue_retry "lock-timeout"
  exit 0
fi
worker_log "lock-acquired" ""
# --------------------------------------------------------------------------

: > "$OUT"

TIMEOUT_SEC="${SELFFIX_TIMEOUT_SEC:-1500}"   # 25m
GRACE_SEC="${SELFFIX_GRACE_SEC:-30}"

set -m
# Strip orchestrator worker identity before spawning the retro. SessionEnd runs
# this script as a child of the dying worker, so CLAUDE_AGENT=worker (+ name /
# parent-manager / domain) would otherwise leak into `claude -p`. The orchestrator
# hooks gate on `CLAUDE_AGENT in ("manager","worker")`, so the retro would register
# itself in active/ as a phantom worker and write turn-ends/<manager>/ markers under
# the dead worker's parent — phantom turn-end noise. Unsetting CLAUDE_AGENT alone
# disables the gate; the rest are stripped for completeness. The retro has no
# legitimate need for worker identity.
( exec env -u CLAUDE_AGENT -u CLAUDE_WORKER_NAME -u CLAUDE_PARENT_MANAGER -u CLAUDE_DOMAIN \
    claude -p "/dockwright-selffix --transcript $TRANSCRIPT" \
    --model claude-sonnet-5 \
    --no-session-persistence > "$OUT" 2>&1 ) &
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
if [ "$EC" -eq 0 ]; then
  worker_log "finished" "exit=$EC bytes=$OUT_BYTES out=$OUT"
else
  worker_log "finished-error" "exit=$EC bytes=$OUT_BYTES out=$OUT"
fi

if [ "$EC" -ne 0 ]; then
  enqueue_retry "finished-error"
elif [ "$OUT_BYTES" -lt 200 ]; then
  # Zero-exit but degenerate output: real findings are >=2.7KB; ~105B means
  # the model never answered (rate-limit banner stub).
  enqueue_retry "stub"
fi

exit 0
