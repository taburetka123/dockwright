#!/usr/bin/env bash
# corpus-watch-run.sh <examined_sha> <range> <targets-csv> [<gardener-dir>]
# Eval runner for corpus-watch (eval-direction C2). Takes the shared analyst
# run-lock (try); busy => exit 0 with state UN-advanced so the next tick
# retries. Runs the behavioral eval gate on the mapped direct-edit targets,
# writes the verdict as a sitting finding on red/infra, advances state to the
# EXAMINED sha (never completion-time HEAD) before releasing the lock.
#
# Verdict branches (gate exit -> finding / notify / state):
#   0  passed          -> no finding, no notify,  advance to $1
#   1  failed          -> finding (behavioral RED), notify (6h throttle, red marker), advance to $1
#   2  error/infra     -> finding (infra-suspect),  notify (24h throttle, infra marker), advance to $1
#   4  nothing-mapped  -> anomaly line in run.log only (harmless tick/run map race), advance to $1
#   5  partial-coverage-> finding (partial-coverage), notify (6h throttle, red marker), advance to $1
#      (defensive: should be unreachable here — the tick hands this script
#      only MAPPED targets — but the gate contract has exit 5, and a coverage
#      gap is a real signal, not infra noise)
# Any other exit is treated like 2 (fail loud, never silent).
set -euo pipefail

SHA="${1:?examined sha required}"
RANGE="${2:?commit range required}"
TARGETS="${3:?targets csv required}"
HOMEDIR="${HOME:?}"
GARDENER_DIR="${4:-$HOMEDIR/.claude/dockwright/gardener}"
GATE_BIN="${CORPUS_WATCH_GATE_BIN:-$HOMEDIR/.claude/scripts/gardener_eval_gate.py}"
WATCH_DIR="$HOMEDIR/.claude/dockwright/corpus-watch"
STATE="$WATCH_DIR/state.json"
FINDINGS_DIR="$HOMEDIR/.claude/dockwright/selffix/findings"
LEDGER="$GARDENER_DIR/ledger.jsonl"
LOCK_DIR="$HOMEDIR/.claude/locks/analyst-run.lock"
RUN_LOG="$WATCH_DIR/run.log"
NOTIFY_RED_MARKER="$WATCH_DIR/.notify-marker-red"
NOTIFY_INFRA_MARKER="$WATCH_DIR/.notify-marker-infra"
RUN_ID="cw-$(date -u +%Y%m%d-%H%M%S)-$$"
# SEPARATE throttles/markers per verdict kind (plan-review M12) — an infra
# notification must never suppress a genuine behavioral RED, and vice versa.
NOTIFY_RED_THROTTLE_SEC="${CORPUS_WATCH_RED_THROTTLE_SEC:-21600}"     # 6h
NOTIFY_INFRA_THROTTLE_SEC="${CORPUS_WATCH_INFRA_THROTTLE_SEC:-86400}" # 24h
mkdir -p "$WATCH_DIR" "$FINDINGS_DIR" "$GARDENER_DIR"

# shellcheck source=runlock.sh
. "$HOMEDIR/.claude/scripts/runlock.sh"
# try-mode: the hourly tick is the retry, so a busy lock just means this run
# loses — exit 0 with NO ledger events and state left UN-advanced.
runlock_acquire "$LOCK_DIR" try || exit 0

GATE_TMP="$WATCH_DIR/.gate-output.$$"
RUN_STARTED=""
RUN_ENDED=""

_error_run_end() {
  # Guaranteed terminal run_end (Important-1): any failure between run_start
  # and the normal completion path (findings-dir write failure, an
  # unresolvable python3 at the state-advance heredoc, anything else `set -e`
  # catches) must still close the ledger bracket and log it, and must NOT
  # advance state — the advance either never ran or its atomic tmp+replace
  # never completed, so state is untouched either way (fail-closed re-
  # examination on the next tick is correct).
  local rc="${RC:-unknown}"
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  error  run_id=$RUN_ID range=$RANGE gate_exit=$rc — run aborted before completion" >> "$RUN_LOG" 2>/dev/null || true
  ledger_append run_end run_id="$RUN_ID" lane=corpus-watch status="error" gate_exit="$rc"
  RUN_ENDED=1
}

_cleanup() {
  if [ -n "$RUN_STARTED" ] && [ -z "$RUN_ENDED" ]; then
    _error_run_end
  fi
  rm -f "$GATE_TMP"
  runlock_release
}
trap _cleanup EXIT
# INT/TERM must TERMINATE, never resume: a bare `trap _cleanup EXIT INT TERM`
# runs the handler (releasing the lock + writing an error run_end) but then
# falls back to whatever the script was doing (waiting on the still-running,
# unsignaled gate child) — the script proceeds past that point, advances
# state, and appends a second contradictory run_end for the same run_id.
trap '_cleanup; exit 143' INT TERM

ledger_append() {
  # ledger_append <event> [key=value]... — JSONL via python3 so quoting in
  # free-text values can't corrupt the ledger (gardener-run.sh's helper,
  # copied verbatim).
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

_notify() {
  # Best-effort local notification; never blocks or fails the run.
  #
  # Skips under PYTEST_CURRENT_TEST (gardener_gate._notify precedent — the
  # 2026-07-03 desktop-notification leak class), UNLESS
  # CORPUS_WATCH_NOTIFY_FORCE is set: tests PATH-shim `osascript` to observe
  # call counts/args, so they set the override to force this through; a real
  # invocation of this script never sets it and keeps the no-op.
  if [ -z "${CORPUS_WATCH_NOTIFY_FORCE:-}" ] && [ -n "${PYTEST_CURRENT_TEST:-}" ]; then
    return 0
  fi
  osascript -e "display notification \"${1//\"/}\" with title \"corpus-watch\"" \
    >/dev/null 2>&1 || true
}

_marker_stale() {
  # _marker_stale <marker-file> <throttle-sec> — true when enough time has
  # passed since the marker's mtime to notify again. A missing marker reads
  # as "never notified" (age = infinite), always stale.
  local marker="$1" throttle="$2" now last
  now="$(date +%s)"
  if [ -f "$marker" ]; then
    last="$(date -r "$marker" +%s 2>/dev/null || echo 0)"
  else
    last=0
  fi
  [ "$((now - last))" -gt "$throttle" ]
}

_write_finding() {
  # _write_finding <status> — corpus-watch-eval-<utc-ts>.md: verdict, commit
  # range, the gate's failing-cases line (if any) + last 30 lines of the
  # gate's combined stdout+stderr, and (only when the captured output shows
  # the investigation suite actually ran) a pointer to its results/traces for
  # deeper digging. Post-rung-3 the default map routes REDs to the repo's own
  # pytest suite, whose failing-test diagnostics print to STDERR — capturing
  # stdout only would leave the exit-1 finding's entire payload empty.
  local status="$1" stamp path failing_line tail30 n
  stamp="$(date -u +%Y%m%d-%H%M%S)"
  path="$FINDINGS_DIR/corpus-watch-eval-$stamp.md"
  n=1
  while [ -e "$path" ]; do
    path="$FINDINGS_DIR/corpus-watch-eval-$stamp-$n.md"
    n=$((n + 1))
  done
  failing_line="$(printf '%s\n' "$GATE_OUTPUT" | grep 'failing cases:' || true)"
  tail30="$(printf '%s\n' "$GATE_OUTPUT" | tail -n 30)"
  {
    echo "# Corpus-watch eval verdict: $status"
    echo
    echo "- examined sha: \`$SHA\`"
    echo "- commit range: \`$RANGE\`"
    echo "- gate exit: $RC"
    if [ "$status" = "infra-suspect" ]; then
      echo "- **infra-suspect** — do not read this as a behavioral verdict"
    fi
    if [ -n "$failing_line" ]; then
      echo
      echo "$failing_line"
    fi
    echo
    echo "## Gate output (last 30 lines, stdout+stderr)"
    echo
    echo '```'
    printf '%s\n' "$tail30"
    echo '```'
    # The results/traces pointer is only honest for a suite that actually
    # writes results/latest.json (the investigation eval). The pytest suite
    # (the rung-3 default-map route for shipping-surface REDs) writes no
    # results file, so an unconditional pointer here pointed at a stale
    # artifact for every pytest-suite verdict (Critical-2). Gate the line on
    # the gate's own per-suite "running investigation:" print.
    if printf '%s\n' "$GATE_OUTPUT" | grep -q 'running investigation:'; then
      echo
      echo "Full results: evals/investigation/results/latest.json (traces alongside it)."
    fi
  } > "$path"
}

ledger_append run_start run_id="$RUN_ID" lane=corpus-watch trigger=corpus-watch range="$RANGE"
RUN_STARTED=1

set +e
# PYTHONUNBUFFERED=1: the gate child's stdout is block-buffered by default
# when redirected to a file while its stderr is not — in the merged
# stdout+stderr capture below, a buffered stdout batch can flush AFTER an
# already-written stderr diagnostic despite printing before it in program
# order, burying the diagnostic well outside _write_finding's tail -30 window.
PYTHONUNBUFFERED=1 /usr/bin/python3 "$GATE_BIN" --targets "$TARGETS" > "$GATE_TMP" 2>&1
RC=$?
set -e
GATE_OUTPUT="$(cat "$GATE_TMP" 2>/dev/null || true)"

case "$RC" in
  0)
    STATUS="passed"
    ;;
  1)
    STATUS="failed"
    _write_finding "$STATUS"
    if _marker_stale "$NOTIFY_RED_MARKER" "$NOTIFY_RED_THROTTLE_SEC"; then
      _notify "corpus-watch: behavioral RED on $RANGE — see selffix findings"
      touch "$NOTIFY_RED_MARKER"
    fi
    ;;
  2)
    STATUS="infra-suspect"
    _write_finding "$STATUS"
    if _marker_stale "$NOTIFY_INFRA_MARKER" "$NOTIFY_INFRA_THROTTLE_SEC"; then
      _notify "corpus-watch: infra-suspect on $RANGE — eval harness could not run"
      touch "$NOTIFY_INFRA_MARKER"
    fi
    ;;
  4)
    STATUS="anomaly-unmapped"
    # Harmless race: the tick's own mapping check ran against the pre-run
    # map; by the time this script re-ran the gate, a concurrent map edit
    # left nothing mapped. Not a finding (nothing to report) — just a loud
    # log line so the race is visible if it ever recurs unexpectedly often.
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  anomaly  run_id=$RUN_ID range=$RANGE gate exited 4 (nothing mapped) — map changed between tick and run, harmless race" >> "$RUN_LOG"
    ;;
  5)
    # Defensive: the gate's partial-coverage exit (mapped suites green but
    # >=1 target unmapped). Unreachable from corpus-watch in the normal flow
    # — the tick hands this script only mapped targets — but a concurrent
    # map edit between tick and run can unmap a subset. A coverage gap over
    # a live direct edit is a RED-class signal (something changed and was
    # NOT checked), never infra noise.
    STATUS="partial-coverage"
    _write_finding "$STATUS"
    if _marker_stale "$NOTIFY_RED_MARKER" "$NOTIFY_RED_THROTTLE_SEC"; then
      _notify "corpus-watch: partial coverage on $RANGE — unmapped targets went unchecked, see selffix findings"
      touch "$NOTIFY_RED_MARKER"
    fi
    ;;
  *)
    # Undocumented exit from the gate binary — never silently pass; treat
    # like infra-suspect (fail loud, never claim a behavioral verdict).
    STATUS="infra-suspect"
    _write_finding "$STATUS"
    if _marker_stale "$NOTIFY_INFRA_MARKER" "$NOTIFY_INFRA_THROTTLE_SEC"; then
      _notify "corpus-watch: unexpected gate exit $RC on $RANGE — treated as infra-suspect"
      touch "$NOTIFY_INFRA_MARKER"
    fi
    ;;
esac

# State advance: last_sha <- the EXAMINED sha ($1), never completion-time
# HEAD (commits landing during the run must be seen by the next tick).
# Atomic tmp+replace; every other key (the drift counters) is preserved.
# Runs BEFORE the EXIT trap releases the run-lock (after release, a
# concurrent tick's drift-branch write could race it).
/usr/bin/python3 - "$STATE" "$SHA" <<'PY'
import json, os, sys
path, sha = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        data = {}
except (OSError, ValueError):
    data = {}
data["last_sha"] = sha
data.setdefault("drift_files", 0)
data.setdefault("drift_bytes", 0)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, sort_keys=True)
os.replace(tmp, path)
PY

ledger_append run_end run_id="$RUN_ID" lane=corpus-watch status="$STATUS" gate_exit="$RC"
RUN_ENDED=1

exit 0
