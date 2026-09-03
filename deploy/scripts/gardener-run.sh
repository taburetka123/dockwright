#!/usr/bin/env bash

set -u

# shellcheck source=loop-label-prefix.sh
_GARDENER_SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$_GARDENER_SD/loop-label-prefix.sh" 2>/dev/null || true

HOMEDIR="${HOME:?}"
GARDENER_DIR="$HOMEDIR/.claude/dockwright/gardener"
DIGESTS_DIR="$GARDENER_DIR/digests"
RUNS_DIR="$GARDENER_DIR/runs"
LEDGER="$GARDENER_DIR/ledger.jsonl"
MARKER="$GARDENER_DIR/last-digest"
RUN_LOG="$GARDENER_DIR/run.log"
STOP_FILE="$HOMEDIR/.claude/dockwright/gardener-stop"
STOP_FILE_LEGACY="$HOMEDIR/.claude/gardener-stop"
FINDINGS_DIR="$HOMEDIR/.claude/dockwright/selffix/findings"
LOCK_DIR="$HOMEDIR/.claude/locks/analyst-run.lock"
ACTIVE_DIR="$HOMEDIR/.claude/dockwright/active"
OUTBOX_ROOT="$HOMEDIR/.claude/dockwright/notify-outbox"

TRIGGER="manual"
LANE="digest"
DRY_RUN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --trigger) TRIGGER="${2:-manual}"; shift; [ $# -gt 0 ] && shift ;;
    --lane)    LANE="${2:-digest}";    shift; [ $# -gt 0 ] && shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) shift ;;
  esac
done
case "$LANE" in digest|frontier) ;; *) LANE="digest" ;; esac

if command -v dockwright_module_enabled >/dev/null 2>&1 && ! dockwright_module_enabled gardener; then
  mkdir -p "$GARDENER_DIR"
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  skip  module-off ([modules] gardener=false)" >> "$RUN_LOG"
  exit 0
fi

MODE="visible"
if [ "${GARDENER_HEADLESS:-}" = "1" ]; then
  MODE="headless"
fi

if [ "$LANE" = "frontier" ]; then
  STOP_FILE="$HOMEDIR/.claude/dockwright/frontier-stop"
  STOP_FILE_LEGACY="$HOMEDIR/.claude/frontier-stop"
  MARKER="$GARDENER_DIR/last-frontier-run"
fi

TIMEOUT_SEC="${GARDENER_TIMEOUT_SEC:-1800}"
if [ "$LANE" = "frontier" ] && [ -z "${GARDENER_TIMEOUT_SEC:-}" ]; then
  TIMEOUT_SEC=2700
fi
GRACE_SEC="${GARDENER_GRACE_SEC:-900}"
POLL_SEC="${GARDENER_POLL_SEC:-20}"
if [ -z "${GARDENER_CWD:-}" ]; then
  GARDENER_CWD="$(command -v dockwright_repo_path >/dev/null 2>&1 && dockwright_repo_path 2>/dev/null || true)"
  [ -n "$GARDENER_CWD" ] || GARDENER_CWD="$HOMEDIR/.claude"
fi

TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
run_log() { echo "$(TS)  $1  ${RUN_ID:--}  ${2:-}" >> "$RUN_LOG"; }

notify() {
  if [ -n "${PYTEST_CURRENT_TEST:-}" ]; then return 0; fi
  /usr/bin/osascript -e "display notification \"${1//\"/}\" with title \"gardener\"" \
    >/dev/null 2>&1 || true
}

notify_manager() {
  /usr/bin/python3 - "$ACTIVE_DIR" "$OUTBOX_ROOT" "$1" <<'PY' 2>/dev/null || {
import errno, json, os, sys, tempfile, time

active_dir, outbox_root, line = sys.argv[1:4]
DEFAULT_DOMAIN = "general"
MAX_OS_PID = 0x7FFFFFFF


def is_live(record):
    pid = record.get("pid")
    if pid is True or pid is False or not isinstance(pid, int):
        return False
    if pid <= 0 or pid > MAX_OS_PID:
        return False
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM
    return True


candidates = []
for name in os.listdir(active_dir):
    if not name.endswith(".json"):
        continue
    try:
        with open(os.path.join(active_dir, name)) as f:
            record = json.load(f)
    except Exception:
        continue
    if not isinstance(record, dict):
        continue
    if record.get("agent") != "manager" or record.get("nested"):
        continue
    if (record.get("domain") or DEFAULT_DOMAIN) != DEFAULT_DOMAIN:
        continue
    manager_name = record.get("name")
    if not isinstance(manager_name, str) or not manager_name:
        continue
    if not is_live(record):
        continue
    started = record.get("started_at")
    started = started if isinstance(started, (int, float)) else 0.0
    candidates.append((started, manager_name))

if not candidates:
    sys.exit(1)

best = max(candidates)

bucket = best[1].replace("/", "_").replace("\\", "_")
if bucket in (".", ".."):
    bucket = "_" + bucket

now = time.time()
outbox = os.path.join(outbox_root, bucket)
os.makedirs(outbox, exist_ok=True)
target = os.path.join(outbox, "%d-%d-0.json" % (int(now * 1000), os.getpid()))
payload = {"line": line, "kind": "gardener", "buffered_at": now}
fd, tmp = tempfile.mkstemp(dir=outbox, suffix=".tmp")
try:
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(payload, sort_keys=True))
    os.replace(tmp, target)
except Exception:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
PY
    run_log "notify" "no live addressee, or delivery failed — fell back to a desktop notification"
    notify "$1"
  }
}

ledger_append() {
  /usr/bin/python3 - "$LEDGER" "$@" <<'PY' 2>/dev/null || true
import json, sys, time
path, event, *pairs = sys.argv[1:]
record = {"type": event, "event": event, "v": 1, "ts": time.time()}
for pair in pairs:
    key, _, value = pair.partition("=")
    record[key] = value
with open(path, "a") as f:
    f.write(json.dumps(record, sort_keys=True) + "\n")
PY
}

mkdir -p "$DIGESTS_DIR" "$RUNS_DIR"

if [ -f "$STOP_FILE" ] || [ -f "$STOP_FILE_LEGACY" ]; then
  run_log "skip" "stopped"
  exit 0
fi

. "$HOMEDIR/.claude/scripts/runlock.sh"
LIVE_WINDOW_SIDECAR=""
_gardener_cleanup() {
  [ -n "$LIVE_WINDOW_SIDECAR" ] && rm -f "$LIVE_WINDOW_SIDECAR" 2>/dev/null
  runlock_release
}
trap _gardener_cleanup EXIT INT TERM

if ! runlock_acquire "$LOCK_DIR" try; then
  run_log "skip" "locked holder=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo unknown)"
  exit 0
fi

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
RUN_DIR="$RUNS_DIR/$RUN_ID"
mkdir -p "$RUN_DIR"
DIGEST="$DIGESTS_DIR/$RUN_ID.md"
PROMPT_FILE="$RUN_DIR/prompt.md"
SETTINGS_FILE="$RUN_DIR/settings.json"
PRE_STATUS="$RUN_DIR/claude-repo-status.pre"
POST_STATUS="$RUN_DIR/claude-repo-status.post"
RUN_START_ISO="$(TS)"

run_log "started" "trigger=$TRIGGER lane=$LANE mode=$MODE pid=$$"
ledger_append run_start run_id="$RUN_ID" trigger="$TRIGGER" lane="$LANE" mode="$MODE" digest="$DIGEST"

INGEST_MODE="incremental"
if ! grep -Eq '"(event|type)": *"proposal"' "$LEDGER" 2>/dev/null; then
  INGEST_MODE="full"
fi

if [ "$LANE" = "frontier" ]; then
  cat > "$PROMPT_FILE" <<EOF
/dockwright-gardener-frontier run_id=$RUN_ID digest=$DIGEST trigger=$TRIGGER
EOF
else
  cat > "$PROMPT_FILE" <<EOF
/dockwright-gardener-digest run_id=$RUN_ID digest=$DIGEST trigger=$TRIGGER mode=$INGEST_MODE
EOF
fi

SETTINGS_PRESET="$HOMEDIR/.claude/dockwright/presets/gardener-analyst-settings.json"
if ! cp "$SETTINGS_PRESET" "$SETTINGS_FILE" 2>/dev/null; then
  run_log "error" "settings preset missing: $SETTINGS_PRESET — run setup.sh"
  ledger_append run_end run_id="$RUN_ID" status="error" audit="skipped" lane="$LANE" detail="settings-preset-missing"
  notify "gardener $RUN_ID: settings preset missing — run setup.sh"
  exit 0
fi

git -C "$HOMEDIR/.claude" status --porcelain 2>/dev/null | sort > "$PRE_STATUS" || true

finish_run() {
  local status="$1" detail="${2:-}"
  git -C "$HOMEDIR/.claude" status --porcelain 2>/dev/null | sort > "$POST_STATUS" || true
  local stray
  stray=$(
    {
      comm -13 "$PRE_STATUS" "$POST_STATUS" 2>/dev/null | sed 's/^...//'
      git -C "$HOMEDIR/.claude" log --since="$RUN_START_ISO" --name-only --pretty=format: 2>/dev/null
      find "$HOMEDIR/.claude" -type f -newer "$PRE_STATUS" \
        ! -path "$HOMEDIR/.claude/dockwright/gardener/*" \
        ! -path "$HOMEDIR/.claude/.git/*" \
        ! -path "$HOMEDIR/.claude/dockwright/*" \
        ! -path "$HOMEDIR/.claude/orchestrator/*" \
        ! -path "$HOMEDIR/.claude/statsig/*" \
        ! -path "$HOMEDIR/.claude/shell-snapshots/*" \
        ! -path "$HOMEDIR/.claude/todos/*" \
        ! -path "$HOMEDIR/.claude/debug/*" \
        ! -path "$HOMEDIR/.claude/file-history/*" \
        ! -path "$HOMEDIR/.claude/plugins/*" \
        ! -path "$HOMEDIR/.claude/projects/*.jsonl" \
        ! -path "$HOMEDIR/.claude/projects/*/subagents/*" \
        ! -path "$HOMEDIR/.claude/projects/*/tool-results/*" \
        ! -path "$HOMEDIR/.claude/backups/*" \
        ! -path "$HOMEDIR/.claude/sessions/*" \
        ! -path "$HOMEDIR/.claude/tasks/*" \
        ! -name "history.jsonl" \
        2>/dev/null | sed "s|^$HOMEDIR/.claude/||"
    } | grep -v '^$' | grep -v '^gardener/' | sort -u | head -40
  )
  local audit="clean"
  if [ -n "$stray" ]; then
    audit="unattributed-writes"
    {
      echo "# Writes outside gardener/ during run window $RUN_START_ISO..$(TS)."
      echo "# Advisory: includes ANY concurrent session's edits (same git author) —"
      echo "# review whether these were yours before reading this as a gardener breach."
      printf '%s\n' "$stray"
    } > "$RUN_DIR/audit-stray-paths.txt"
    run_log "audit" "unattributed writes outside gardener/ — see $RUN_DIR/audit-stray-paths.txt"
    notify_manager "gardener $RUN_ID: writes outside gardener/ in the run window (may include concurrent-session edits) — review $RUN_DIR/audit-stray-paths.txt"
  fi
  local spend
  spend=$(/usr/bin/python3 "$HOMEDIR/.claude/scripts/gardener_spend.py" "$GARDENER_CWD" "$RUN_ID" 2>/dev/null || true)
  postrun_summary=$(/usr/bin/python3 "$HOMEDIR/.claude/scripts/gardener_postrun.py" postrun \
      --run-id "$RUN_ID" --lane "$LANE" 2>&1) || true
  run_log "postrun" "$postrun_summary"
  local postrun_rejected=""
  case "$postrun_summary" in
    *"gardener-postrun:"*rejected=*)
      postrun_rejected="${postrun_summary##*rejected=}"
      postrun_rejected="${postrun_rejected%%[^0-9]*}"
      ;;
  esac
  if [ -z "$postrun_rejected" ]; then
    local postrun_head="${postrun_summary%%$'\n'*}"
    run_log "applycheck" "postrun-unparseable: ${postrun_head:0:120}"
    notify "gardener $RUN_ID: postrun failed/unparseable — apply-check did not run"
  elif [ "$postrun_rejected" -gt 0 ]; then
    run_log "applycheck" "REJECTED:$postrun_rejected"
    notify "gardener $RUN_ID: $postrun_rejected proposal(s) quarantined by the birth gate — see proposals/rejected/"
  fi
  ledger_append run_end run_id="$RUN_ID" status="$status" audit="$audit" detail="$detail" lane="$LANE" postrun="$postrun_summary" $spend
  if [ "$status" = "ok" ]; then
    touch "$MARKER"
    run_log "finished" "digest=$DIGEST audit=$audit"
    notify_manager "gardener digest ready: $RUN_ID ($audit)"
    local pending_dir="$GARDENER_DIR/proposals/pending"
    local pending_list pending_count=0 oldest_file oldest_mtime oldest_age_days=0
    pending_list=$(ls -t "$pending_dir"/*.md 2>/dev/null)
    if [ -n "$pending_list" ]; then
      pending_count=$(printf '%s\n' "$pending_list" | wc -l | tr -d ' ')
    fi
    oldest_file="${pending_list##*$'\n'}"
    if [ -n "${oldest_file:-}" ]; then
      oldest_mtime=$(date -r "$oldest_file" +%s 2>/dev/null || date +%s)
      oldest_age_days=$(( ($(date +%s) - oldest_mtime) / 86400 ))
    fi
    if [ "${pending_count:-0}" -gt 20 ] || [ "$oldest_age_days" -gt 14 ]; then
      run_log "backlog" "pending=$pending_count oldest_days=$oldest_age_days"
      notify_manager "gardener backlog: $pending_count pending proposals, oldest ~${oldest_age_days}d — run /dockwright-selffix-review"
      ledger_append backlog run_id="$RUN_ID" pending="$pending_count" oldest_days="$oldest_age_days" lane="$LANE"
    fi
  else
    run_log "finished-$status" "audit=$audit $detail"
  fi
}

if [ "$MODE" = "headless" ]; then
  if [ "$LANE" != "digest" ]; then
    run_log "error" "headless-unsupported-lane lane=$LANE — only digest is supported headless"
    echo "Status: error (headless lane=$LANE unsupported)" >> "$DIGEST"
    finish_run error "headless-unsupported-lane lane=$LANE"
    exit 0
  fi
  GARDENER_SKILL_FILE="$HOMEDIR/.claude/skills/dockwright-gardener-digest/SKILL.md"
  if [ ! -f "$GARDENER_SKILL_FILE" ]; then
    run_log "error" "skill-missing $GARDENER_SKILL_FILE — run setup.sh"
    echo "Status: error (skill-missing)" >> "$DIGEST"
    finish_run error "skill-missing"
    exit 0
  fi
  HEADLESS_PROMPT_FILE="$RUN_DIR/headless-prompt.md"
  {
    cat "$GARDENER_SKILL_FILE"
    printf '\n\n---\nExecute the skill above now, in headless mode, with: '
    cat "$PROMPT_FILE"
  } > "$HEADLESS_PROMPT_FILE"
  mkdir -p "$FINDINGS_DIR" "$GARDENER_DIR" 2>/dev/null || true
  set -m
  ( exec env -u CLAUDE_AGENT -u CLAUDE_WORKER_NAME -u CLAUDE_PARENT_MANAGER -u CLAUDE_DOMAIN \
      claude -p \
      --model claude-sonnet-5 \
      --add-dir "$FINDINGS_DIR" \
      --add-dir "$GARDENER_DIR" \
      --allowedTools 'Bash(cat:*) Bash(ls:*) Bash(wc:*) Bash(head:*) Bash(tail:*) Bash(grep:*) Bash(jq:*)' \
      --tools "Bash,Read,Grep,Glob" \
      --strict-mcp-config \
      --mcp-config '{"mcpServers":{}}' \
      --setting-sources "" \
      --no-session-persistence \
      --disallowedTools "Write,Edit,NotebookEdit" \
      < "$HEADLESS_PROMPT_FILE" > "$DIGEST" 2>&1 ) &
  CHILD_PID=$!
  PGID=$CHILD_PID
  ( sleep "$TIMEOUT_SEC"; kill -TERM "-$PGID" 2>/dev/null
    sleep 30; kill -KILL "-$PGID" 2>/dev/null ) &
  WATCHDOG_PID=$!
  wait "$CHILD_PID"; EC=$?
  kill "$WATCHDOG_PID" 2>/dev/null; wait "$WATCHDOG_PID" 2>/dev/null
  set +m
  DIGEST_BYTES=$(wc -c < "$DIGEST" 2>/dev/null | awk '{print $1}')
  DIGEST_MIN_BYTES="${GARDENER_DIGEST_MIN_BYTES:-800}"
  if grep -q '^Status:' "$DIGEST" 2>/dev/null && grep -q '^## ' "$DIGEST" 2>/dev/null \
     && [ "${DIGEST_BYTES:-0}" -ge "$DIGEST_MIN_BYTES" ] && [ "$EC" -eq 0 ]; then
    finish_run ok "exit=$EC"
  else
    echo "" >> "$DIGEST"
    if [ "$EC" -eq 0 ] && { ! grep -q '^## ' "$DIGEST" 2>/dev/null \
         || [ "${DIGEST_BYTES:-0}" -lt "$DIGEST_MIN_BYTES" ]; }; then
      echo "Status: error (empty digest: no '## ' sections, or under ${DIGEST_MIN_BYTES}B — the child produced no real content)" >> "$DIGEST"
      run_log "error" "empty-digest exit=$EC bytes=${DIGEST_BYTES:-0} — refusing to mark the run ok or touch the cadence marker"
      finish_run error "empty-digest exit=$EC"
    else
      echo "Status: error (exit=$EC)" >> "$DIGEST"
      finish_run error "exit=$EC"
    fi
  fi
  exit 0
fi

TMUX_SOCK="${DOCKWRIGHT_TMUX_SOCKET:-${CLAUDE_ORCH_TMUX_SOCKET:-dockwright}}"
TMUX_CONF_FILE="$HOMEDIR/.claude/dockwright/dockwright.tmux.conf"
TMUX_CONF_LEGACY="$HOMEDIR/.claude/orchestrator/dockwright.tmux.conf"
TMUX_CONF_LEGACY2="$HOMEDIR/.claude/orchestrator/claude-orch.tmux.conf"
FFLAG=()
if [ -f "$TMUX_CONF_FILE" ]; then FFLAG=(-f "$TMUX_CONF_FILE")
elif [ -f "$TMUX_CONF_LEGACY" ]; then FFLAG=(-f "$TMUX_CONF_LEGACY")
elif [ -f "$TMUX_CONF_LEGACY2" ]; then FFLAG=(-f "$TMUX_CONF_LEGACY2"); fi
if [ -n "$DRY_RUN" ]; then
    echo "DRY_RUN: no spawn. socket=$TMUX_SOCK cwd=$GARDENER_CWD"
    exit 0
fi
if [ "$HOME" != "$(eval echo ~"$(id -un)")" ]; then
    case "$TMUX_SOCK" in
        dockwright|claude-orch)
            echo "ERROR: \$HOME ($HOME) is not the uid's real home — refusing to spawn onto live socket '$TMUX_SOCK'. Use --dry-run to probe, or set DOCKWRIGHT_TMUX_SOCKET to a scratch socket." >&2
            exit 3 ;;
    esac
fi
if tmux -L "$TMUX_SOCK" has-session -t claude-workers 2>/dev/null; then
  TMUX_HEAD=(new-window -d -t claude-workers)
else
  TMUX_HEAD=(new-session -d -s claude-workers)
fi
INNER_CMD="cd $(printf '%q' "$GARDENER_CWD") && env -u CLAUDE_AGENT -u CLAUDE_WORKER_NAME -u CLAUDE_PARENT_MANAGER -u CLAUDE_DOMAIN claude --model claude-sonnet-5 --settings $(printf '%q' "$SETTINGS_FILE") \"\$(cat $(printf '%q' "$PROMPT_FILE"))\""
SPAWN_SHELL="$(command -v zsh || command -v bash || echo sh)"
WINDOW_ID=$(tmux -L "$TMUX_SOCK" ${FFLAG[@]+"${FFLAG[@]}"} "${TMUX_HEAD[@]}" \
  -n "🌱 gardener $RUN_ID" -c "$GARDENER_CWD" -P -F '#{pane_id}' -- \
  "$SPAWN_SHELL" -ic "$INNER_CMD" 2>>"$RUN_LOG")
if [ -z "$WINDOW_ID" ]; then
  run_log "error" "tmux launch failed"
  ledger_append run_end run_id="$RUN_ID" status="error" audit="skipped" lane="$LANE" detail="tmux-launch-failed"
  notify "gardener $RUN_ID: tmux launch failed"
  exit 0
fi
run_log "spawned" "window_id=$WINDOW_ID backend=tmux"
ledger_append session_spawned run_id="$RUN_ID" window_id="$WINDOW_ID" lane="$LANE" mode=visible

mkdir -p "$GARDENER_DIR/live-windows"
LIVE_WINDOW_SIDECAR="$GARDENER_DIR/live-windows/$RUN_ID.window"
printf '%s' "$WINDOW_ID" > "$LIVE_WINDOW_SIDECAR"

DEADLINE=$((SECONDS + TIMEOUT_SEC))
while (( SECONDS < DEADLINE )); do
  grep -q '^Status:' "$DIGEST" 2>/dev/null && break
  sleep "$POLL_SEC"
done

if ! grep -q '^Status:' "$DIGEST" 2>/dev/null; then
  run_log "overdue" "no Status after ${TIMEOUT_SEC}s — grace ${GRACE_SEC}s"
  notify "gardener $RUN_ID overdue (${TIMEOUT_SEC}s) — tab left open, mutex frees in ${GRACE_SEC}s"
  GRACE_DEADLINE=$((SECONDS + GRACE_SEC))
  while (( SECONDS < GRACE_DEADLINE )); do
    grep -q '^Status:' "$DIGEST" 2>/dev/null && break
    sleep "$POLL_SEC"
  done
fi

if grep -q '^Status: ok' "$DIGEST" 2>/dev/null; then
  finish_run ok
  if tmux -L "$TMUX_SOCK" kill-pane -t "$WINDOW_ID" 2>/dev/null; then
    run_log "window_killed" "window_id=$WINDOW_ID"
    ledger_append window_killed run_id="$RUN_ID" window_id="$WINDOW_ID"
  else
    run_log "window_kill_failed" "window_id=$WINDOW_ID"
  fi
elif grep -q '^Status:' "$DIGEST" 2>/dev/null; then
  finish_run error "$(grep '^Status:' "$DIGEST" | tail -1)"
else
  finish_run timeout "no Status line within $((TIMEOUT_SEC + GRACE_SEC))s; tab left open"
fi
exit 0
