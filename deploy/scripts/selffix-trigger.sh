#!/usr/bin/env bash
# Canonical source: deploy/scripts/selffix-trigger.sh @ taburetka123/claude-orchestrator
# — deployed to ~/.claude/scripts/ by setup.sh. Edit the repo copy, not the deployed one.
#
# SessionEnd hook: decide whether the just-ended session deserves a
# /dockwright-selffix retrospective.
#   - HIGH signal  -> spawn selffix-run.sh in the background (writes findings).
#   - LOW / none   -> log a none line, no action.
#   - SKIP reasons -> log a skip line with reason.
# Always exits 0 fast; never blocks the session close.
#
# Historical note: this hook used to fire on Stop (every assistant turn) and
# spawn on the first HIGH-signal Stop. That captured pre-PR work only — any
# post-PR discussion (code-review handling, user pushback peeling back
# over-engineered fixes, etc.) was always missed because subsequent Stops hit
# the findings-exist gate. Moved to SessionEnd 2026-05-13: trigger fires
# exactly once at session close with the full transcript in hand.
# Trade-off: SIGKILL / power loss / hardware crash bypass SessionEnd and
# leave no retro for that session. Accepted; rare and recoverable via manual
# /dockwright-selffix on the saved transcript if needed.
#
# All outcomes land in ~/.claude/dockwright/selffix/trigger.log so the trigger
# is traceable post-hoc — every SessionEnd fires the script, and every fire
# writes exactly one line.
#
# Contract (see also ~/.claude/scripts/selffix-run.sh and
# ~/.claude/skills/dockwright-selffix/SKILL.md):
#   trigger  -> nohup spawn worker with (transcript-path, session-id)
#   worker   -> claude -p "/dockwright-selffix --transcript <path>" > $OUT
#               (spawned with --disallowedTools "Write,Edit,NotebookEdit")
#   skill    -> emits findings to stdout ONLY; never calls Write/Edit
# If you change one file, update the other two.

set -u

# Debug logging is OFF by default. Turn on by either:
#   touch ~/.claude/dockwright/selffix/debug
#   or export SELFFIX_DEBUG=1 in your shell rc
LOG="$HOME/.claude/dockwright/selffix/trigger.log"
DEBUG=0
# deprecated, one release: legacy debug flag honored while docs/habits migrate
if [ -f "$HOME/.claude/dockwright/selffix/debug" ] || [ -f "$HOME/.claude/selffix-debug" ] || [ "${SELFFIX_DEBUG:-}" = "1" ]; then
  DEBUG=1
  mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
fi
TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log_line() {
  # log_line <outcome> <session> <reasons-or-detail>
  [ "$DEBUG" = "1" ] || return 0
  echo "$(TS)  $1  ${2:--}  ${3:-}" >> "$LOG"
}

# [modules] gardener toggle: this SessionEnd retro is the head of the Gardener
# pipeline, so `[modules] gardener=false` must no-op it (design-gate: the toggle
# cleanly disables the WHOLE chain, proven by tests). Bail before reading the
# payload or running the detect. Sourcing the shared helper is best-effort — a
# missing lib (e.g. a test that copied only this script) means fail-open =
# module enabled, so the historic detect path is unaffected.
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

# Resolve the HIGH-complexity skill set from [gardener] high_skills (empty
# default → skill-based HIGH detection is OFF generically). Read here via the
# sourced helper (bare python3, tomllib-or-scanner) and passed into the detect
# heredoc as an env var — the heredoc runs under /usr/bin/python3, which on this
# machine is 3.9 with NO tomllib, so it must NOT parse config itself.
SELFFIX_HIGH_SKILLS=""
if command -v dockwright_high_skills >/dev/null 2>&1; then
  SELFFIX_HIGH_SKILLS="$(dockwright_high_skills 2>/dev/null || true)"
fi

DETECT=$(SELFFIX_PAYLOAD="$PAYLOAD" SELFFIX_HIGH_SKILLS="$SELFFIX_HIGH_SKILLS" /usr/bin/python3 - <<'PY' 2>/dev/null
import hashlib, json, os, re, sys

def bail(level, detail):
    # Emit the same 4-line shape as a normal detect so the bash side can route
    # the outcome through the standard log path with a distinguishable reason.
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

# cwd + first-user-message feed the dedup key (see end of script). cwd may be in
# the payload; if not, it's filled from the active record below.
cwd = payload.get("cwd") or ""

# HIGH-complexity skills come from [gardener] high_skills (resolved by the bash
# side, passed in via SELFFIX_HIGH_SKILLS — newline-separated). Empty default →
# skill-based HIGH detection is OFF generically; an operator opts in via config.
HIGH_SKILLS = {s for s in os.environ.get("SELFFIX_HIGH_SKILLS", "").splitlines() if s.strip()}
# NOTE (2026-05-20): spawn_worker / worker_done were REMOVED from the HIGH gate.
# Every orchestrator worker calls worker_done on its terminal turn, so gating on
# it fired a retro for ~every worker session — 4x findings volume, and when a
# manager teardown ended ~21 workers at once they spawned ~21 retro workers
# simultaneously, stampeding the Anthropic rate limiter (each died with a
# 131-byte "rate limited" stub). HIGH now = configured high_skills, gh pr create,
# >=5 edits, or agent=manager. Manager sessions are rare enough not to stampede.
EDIT_WRITE_HIGH_THRESHOLD = 5
PR_CREATE_RE = re.compile(r"\bgh\s+pr\s+create\b")
# User-pushback + harsh-language signals (EN+RU). ANY single match in a USER
# message is a HIGH signal: a correction-heavy session is exactly the
# transcript a retro learns from, and a false-positive retro costs one cheap
# headless run (user decision 2026-06-13; the old >=3 MED tier fired 0 times
# in the log's lifetime). Only str-content user records are scanned, so
# assistant text and tool_results can never trigger. \b and IGNORECASE are
# Unicode-aware: "\bне\s+то\b" does not match "не только"; "\bбля" does not
# match "корабля".
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
# Structural human-flag: the /dockwright-fix slash command (deprecated alias
# /fix still recognized for one release). The harness records a
# command invocation as a type=user record whose message.content (a STRING)
# carries <command-name>/dockwright-fix</command-name> or
# <command-name>/fix</command-name>
# (verified across 845 real command records). Keying on the structural tag —
# not a textual sigil — defeats prose/backtick mentions (`/dockwright-fix`) and
# the old @fix/@gardener text. But a distillation/handoff session embeds a
# PRIOR session's transcript as THIS message's content, and that transcript
# can carry a real <command-name>/dockwright-fix</command-name> tag — so the
# structural tag ALONE still false-fires (observed on manager-distill retros).
# The position invariant closes it: a genuine invocation's content STARTS with
# <command-message…>; an embedded transcript has the tag buried mid-string.
# We require BOTH below. The note rides in <command-args>; the
# dockwright-selffix retro reads it. \s* tolerates incidental whitespace, and the
# regex form (not the literal tag) means pasting THIS code does not self-match.
FIX_CMD_RE = re.compile(r"<command-name>\s*/(?:dockwright-fix|fix)\s*</command-name>", re.IGNORECASE)

high_reasons = []
pushback_count = 0
harsh_count = 0
already_ran_selffix = False
user_msgs = 0
assistant_tool_uses = 0
edit_write_count = 0
first_user_msg = None
fix_command_flagged = False  # set when a /dockwright-fix (or deprecated /fix) slash-command invocation is seen

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
                if FIX_CMD_RE.search(content) and stripped.startswith("<command-message"):
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

# Pushback/harsh are *reactions* — they need >=1 prior assistant turn to react
# to. A single-user-message session (a `claude -p` app call whose lone "user
# message" is a document/transcript payload) cannot be genuine pushback; gating
# on user_msgs>=2 suppresses false-fires on embedded foreign-language filler
# ("неправильн"/"хватит"/"не надо" spoken inside a video transcript) without
# losing real multi-turn corrections (the 2026-06-13 >=1 decision assumed user
# messages are human chat turns; this encodes the turn-count that assumption
# implied).
if pushback_count >= 1 and user_msgs >= 2:
    high_reasons.append(f"pushback:{pushback_count}")
if harsh_count >= 1 and user_msgs >= 2:
    high_reasons.append(f"harsh:{harsh_count}")

# The /dockwright-fix command = a deliberate human request to retrospect this
# session. Unlike pushback/harsh (reactions, gated on user_msgs>=2), a single
# one-shot /dockwright-fix invocation must fire — NO turn-count gate.
if fix_command_flagged:
    high_reasons.append("fix-command")

# Manager sessions: ALWAYS retro — coordination work is itself worth reviewing,
# and manager turns rarely surface any of the other HIGH signals on their own.
# Lookup is best-effort: SessionEnd fires BEFORE dockwright session-end, so
# active/<sid>.json is still present at trigger time for both regular session-
# close and the manual kill_worker / autoclose / takeover paths.
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

# Dedup key: same agent + cwd + first-user-message => same logical work. The
# bash side uses it to skip a re-spawn within 60 min (retry-storm guard), while
# still spawning for a legit re-occurrence days later (stale marker).
dedup_seed = f"{agent_val}|{cwd}|{(first_user_msg or '')[:500]}"
dedup_key = hashlib.sha256(dedup_seed.encode("utf-8", "ignore")).hexdigest()

if already_ran_selffix:
    level = "skip:already-ran"
elif high_reasons:
    level = "high"
else:
    level = "none"

# Output: 5 lines for bash to read.
print(level)
print(session_id)
print(transcript)
print("; ".join(sorted(set(high_reasons))) if high_reasons
      else f"users={user_msgs} tools={assistant_tool_uses} pushback={pushback_count} harsh={harsh_count}")
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

# Prune: a finding is deleted ONLY after it has been reviewed (its .reviewed
# sibling exists) AND the review itself is >14 days old (marker mtime), so the
# retention clock starts at review time. Unreviewed findings are NEVER
# age-pruned: they are the Gardener's input corpus and pending proposals
# reference them by basename — silently destroying unreviewed evidence breaks
# the never-re-surface-without-decision contract (arch review 2026-06-11 A1).
# Dedup markers keep the plain 14d prune.
PRUNED_FINDINGS=0
while IFS= read -r marker; do
  [ -n "$marker" ] || continue
  rm -f "${marker%.reviewed}.md" "$marker" 2>/dev/null || true
  PRUNED_FINDINGS=$((PRUNED_FINDINGS + 1))
done < <(find "$FINDINGS_DIR" -maxdepth 1 -type f -name '*.reviewed' -mtime +14 2>/dev/null)
PRUNED_DEDUP=$(find "$DEDUP_DIR" -maxdepth 1 -type f -mtime +14 -print 2>/dev/null | wc -l | tr -d ' ')
find "$DEDUP_DIR" -maxdepth 1 -type f -mtime +14 -delete 2>/dev/null || true
log_line "prune" "-" "findings=$PRUNED_FINDINGS dedup=$PRUNED_DEDUP"

# If a findings file already exists (even empty = in-flight worker), never
# re-spawn for this session. -f covers both "worker running" and "worker done".
if [ -f "$FINDINGS_DIR/${SESSION_ID}.md" ] && [ "$LEVEL" = "high" ]; then
  log_line "skip:findings-exist" "$SESSION_ID" "$REASONS"
  exit 0
fi

# Dedup guard: skip a re-spawn for the same agent+cwd+first-user-message within
# the last 60 min. Catches retry storms (near-identical sessions firing in a
# burst); a legit re-occurrence days later has only a stale marker (>60 min), so
# it spawns normally.
if [ "$LEVEL" = "high" ] && [ -n "$DEDUP_KEY" ] && \
   [ -n "$(find "$DEDUP_DIR" -maxdepth 1 -name "$DEDUP_KEY" -mmin -60 2>/dev/null)" ]; then
  log_line "skip:dedup" "$SESSION_ID" "$REASONS key=$DEDUP_KEY"
  exit 0
fi

case "$LEVEL" in
  high)
    # Record the dedup marker so a retry storm within 60 min skips re-spawning.
    if [ -n "$DEDUP_KEY" ]; then : > "$DEDUP_DIR/$DEDUP_KEY" 2>/dev/null || true; fi
    # Limit-brick probe: while the account is rate-limit bricked,
    # stale_monitor refreshes ~/.claude/dockwright/.manager-limited-* every
    # scan (~60s) and removes them on clear; the 5-min freshness window is
    # ~5x that refresh cadence, deliberately tighter than the monitor's
    # ~10-min dead-loop threshold. A fresh flag means a spawn dies
    # in seconds leaving a banner stub — enqueue a durable retry instead; the
    # gardener gate retries once after the brick clears. Fail-open: no fresh
    # flag, probe error, or enqueue failure -> spawn as always (a doomed run
    # still self-enqueues via selffix-run.sh).
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
