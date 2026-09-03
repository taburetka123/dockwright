#!/usr/bin/env bash

set -u

LOG="$HOME/.claude/dockwright/selffix/trigger.log"
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
DEBUG=0
if [ -f "$HOME/.claude/dockwright/selffix/debug" ] || [ -f "$HOME/.claude/selffix-debug" ] || [ "${SELFFIX_DEBUG:-}" = "1" ]; then
  DEBUG=1
fi
TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log_line() {
  echo "$(TS)  $1  ${2:--}  ${3:-}" >> "$LOG" || true
}
log_debug() {
  [ "$DEBUG" = "1" ] || return 0
  log_line "$@"
}

_SELFFIX_SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=loop-label-prefix.sh
. "$_SELFFIX_SD/loop-label-prefix.sh" 2>/dev/null || true
if command -v dockwright_module_enabled >/dev/null 2>&1 && ! dockwright_module_enabled gardener; then
  log_line "module-off" "-" "[modules] gardener=false"
  exit 0
fi

PAYLOAD=$(cat 2>/dev/null || echo "")
if [ -z "$PAYLOAD" ]; then
  log_line "skip:no-payload" "-" "stdin empty"
  exit 0
fi

SELFFIX_HIGH_SKILLS=""
if command -v dockwright_high_skills >/dev/null 2>&1; then
  SELFFIX_HIGH_SKILLS="$(dockwright_high_skills 2>/dev/null || true)"
fi

DETECT=$(SELFFIX_PAYLOAD="$PAYLOAD" SELFFIX_HIGH_SKILLS="$SELFFIX_HIGH_SKILLS" \
  SELFFIX_SIGNAL_DIR="$_SELFFIX_SD" /usr/bin/python3 - <<'PY' 2>/dev/null
import hashlib, json, os, re, sys

def bail(level, detail):
    print(level)
    print("-")
    print("-")
    print(detail)
    sys.exit(0)

raw = os.environ.get("SELFFIX_PAYLOAD", "")
try:
    payload = json.loads(raw)
except Exception as e:
    bail("skip:bad-json", f"payload not valid JSON ({type(e).__name__})")

transcript = payload.get("transcript_path") or ""
session_id = payload.get("session_id") or payload.get("sessionId") or ""
if not transcript:
    bail("skip:no-transcript-field", "payload missing transcript_path")
if not os.path.isfile(transcript):
    bail("skip:transcript-missing", f"transcript file does not exist: {transcript}")
if not session_id:
    session_id = os.path.basename(transcript).rsplit(".jsonl", 1)[0]

cwd = payload.get("cwd") or ""

HIGH_SKILLS = {s for s in os.environ.get("SELFFIX_HIGH_SKILLS", "").splitlines() if s.strip()}
EDIT_WRITE_HIGH_THRESHOLD = 5
PR_CREATE_RE = re.compile(r"\bgh\s+pr\s+create\b")
PUSHBACK_RE = re.compile(
    r"you'?re wrong|no,?\s+don'?t|stop doing|why u stopped|why did you stop|"
    r"i told you|that'?s wrong|not what i asked|"
    r"почему\s+(?:ты\s+)?останов|я\s+(?:же|тебе)\s+(?:говорил|сказал)|\bне\s+то\b|"
    r"ты\s+не\s*прав|перестань|\bхватит\b|\bне\s+надо\b|\bстоп\b|неправильн|"
    r"не\s+работает|я\s+(?:же\s+)?просил",
    re.IGNORECASE,
)
HARSH_RE = re.compile(
    r"\bfuck|\bwtf\b|\bbullshit\b|\bshit|\bdamn\b|"
    r"\bбля|\bху[йяеё]|\bпизд|[её]ба[лнт]|\bохуе|похуй|\bнаху|\bнахер|\bсук[аи]\b",
    re.IGNORECASE,
)
_signal_dir = os.environ.get("SELFFIX_SIGNAL_DIR", "")
if _signal_dir:
    sys.path.insert(0, _signal_dir)
try:
    from transcript_signal import is_human_fix_invocation  # noqa: E402
    fix_predicate_available = True
except Exception:
    def is_human_fix_invocation(content):
        return False
    fix_predicate_available = False

high_reasons = []
degradations = []
pushback_count = 0
harsh_count = 0
already_ran_selffix = False
user_msgs = 0
assistant_tool_uses = 0
edit_write_count = 0
first_user_msg = None
fix_command_flagged = False

with open(transcript, "r", errors="ignore") as f:
    for line in f:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        t = rec.get("type")
        msg = rec.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None

        if t == "user":
            if isinstance(content, str):
                user_msgs += 1
                stripped = content.lstrip()
                if first_user_msg is None and stripped:
                    first_user_msg = stripped
                if stripped.startswith("/dockwright-selffix"):
                    already_ran_selffix = True
                if PUSHBACK_RE.search(content):
                    pushback_count += 1
                if HARSH_RE.search(content):
                    harsh_count += 1
                if is_human_fix_invocation(content):
                    fix_command_flagged = True

        elif t == "assistant":
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") != "tool_use":
                    continue
                assistant_tool_uses += 1
                name = c.get("name")
                tinput = c.get("input") or {}
                if name == "Skill":
                    skill = tinput.get("skill") if isinstance(tinput, dict) else None
                    if skill == "dockwright-selffix":
                        already_ran_selffix = True
                    if skill in HIGH_SKILLS:
                        high_reasons.append(f"skill:{skill}")
                elif name == "Bash":
                    cmd = tinput.get("command", "") if isinstance(tinput, dict) else ""
                    if PR_CREATE_RE.search(cmd):
                        high_reasons.append("pr-created")
                elif name in ("Edit", "Write"):
                    edit_write_count += 1

if edit_write_count >= EDIT_WRITE_HIGH_THRESHOLD:
    high_reasons.append(f"edits:{edit_write_count}")

if pushback_count >= 1 and user_msgs >= 2:
    high_reasons.append(f"pushback:{pushback_count}")
if harsh_count >= 1 and user_msgs >= 2:
    high_reasons.append(f"harsh:{harsh_count}")

if not fix_predicate_available:
    degradations.append("fix-predicate-unavailable")
if fix_command_flagged:
    high_reasons.append("fix-command")

agent_val = ""
home_dir = os.environ.get("HOME", "")
if home_dir and session_id:
    active_path = os.path.join(home_dir, ".claude", "dockwright", "active", f"{session_id}.json")
    try:
        if os.path.isfile(active_path):
            with open(active_path) as af:
                rec = json.load(af)
            if isinstance(rec, dict):
                agent_val = rec.get("agent") or ""
                if not cwd:
                    cwd = rec.get("cwd") or ""
                if agent_val == "manager":
                    high_reasons.append("agent:manager")
    except Exception:
        pass

dedup_seed = f"{agent_val}|{cwd}|{(first_user_msg or '')[:500]}"
dedup_key = hashlib.sha256(dedup_seed.encode("utf-8", "ignore")).hexdigest()

if already_ran_selffix:
    level = "skip:already-ran"
elif high_reasons:
    level = "high"
else:
    level = "none"

print(level)
print(session_id)
print(transcript)
reasons = ("; ".join(sorted(set(high_reasons))) if high_reasons
           else f"users={user_msgs} tools={assistant_tool_uses} pushback={pushback_count} harsh={harsh_count}")
if degradations:
    reasons = f"{reasons} [{' '.join(sorted(set(degradations)))}]"
print(reasons)
print(dedup_key)
PY
)

LEVEL=$(printf '%s\n' "$DETECT" | sed -n '1p')
SESSION_ID=$(printf '%s\n' "$DETECT" | sed -n '2p')
TRANSCRIPT=$(printf '%s\n' "$DETECT" | sed -n '3p')
REASONS=$(printf '%s\n' "$DETECT" | sed -n '4p')
DEDUP_KEY=$(printf '%s\n' "$DETECT" | sed -n '5p')

if [ -z "${LEVEL:-}" ]; then
  log_line "skip:parse-error" "-" "python detect failed"
  exit 0
fi

FINDINGS_DIR="$HOME/.claude/dockwright/selffix/findings"
DEDUP_DIR="$FINDINGS_DIR/.dedup"
mkdir -p "$DEDUP_DIR"

PRUNED_FINDINGS=0
while IFS= read -r marker; do
  [ -n "$marker" ] || continue
  rm -f "${marker%.reviewed}.md" "$marker" 2>/dev/null || true
  PRUNED_FINDINGS=$((PRUNED_FINDINGS + 1))
done < <(find "$FINDINGS_DIR" -maxdepth 1 -type f -name '*.reviewed' -mtime +14 2>/dev/null)
PRUNED_DEDUP=$(find "$DEDUP_DIR" -maxdepth 1 -type f -mtime +14 -print 2>/dev/null | wc -l | tr -d ' ')
find "$DEDUP_DIR" -maxdepth 1 -type f -mtime +14 -delete 2>/dev/null || true
log_debug "prune" "-" "findings=$PRUNED_FINDINGS dedup=$PRUNED_DEDUP"

if [ -f "$FINDINGS_DIR/${SESSION_ID}.md" ] && [ "$LEVEL" = "high" ]; then
  log_line "skip:findings-exist" "$SESSION_ID" "$REASONS"
  exit 0
fi

if [ "$LEVEL" = "high" ] && [ -n "$DEDUP_KEY" ] && \
   [ -n "$(find "$DEDUP_DIR" -maxdepth 1 -name "$DEDUP_KEY" -mmin -60 2>/dev/null)" ]; then
  log_line "skip:dedup" "$SESSION_ID" "$REASONS key=$DEDUP_KEY"
  exit 0
fi

case "$LEVEL" in
  high)
    if [ -n "$DEDUP_KEY" ]; then : > "$DEDUP_DIR/$DEDUP_KEY" 2>/dev/null || true; fi
    if [ -n "$(find "$HOME/.claude/dockwright" -maxdepth 1 -name '.manager-limited-*' -mmin -5 2>/dev/null | head -1)" ] \
       && . "$HOME/.claude/scripts/selffix-retry-lib.sh" 2>/dev/null \
       && selffix_enqueue_retry "$SESSION_ID" "$TRANSCRIPT" "brick"; then
      log_line "retry:enqueued" "$SESSION_ID" "reason=brick $REASONS"
      exit 0
    fi
    nohup bash "$HOME/.claude/scripts/selffix-run.sh" \
      "$TRANSCRIPT" "$SESSION_ID" \
      >/dev/null 2>&1 </dev/null &
    SPAWN_PID=$!
    disown >/dev/null 2>&1 || true
    log_line "spawn" "$SESSION_ID" "$REASONS pid=$SPAWN_PID"
    ;;
  none)
    log_line "none" "$SESSION_ID" "$REASONS"
    ;;
  skip:*)
    log_line "$LEVEL" "$SESSION_ID" "$REASONS"
    ;;
  *)
    log_line "skip:unknown-level" "$SESSION_ID" "level=$LEVEL"
    ;;
esac

exit 0
