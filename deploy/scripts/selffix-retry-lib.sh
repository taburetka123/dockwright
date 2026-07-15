#!/usr/bin/env bash
# Shared enqueue helper for the selffix durable-retry queue
# (~/.claude/dockwright/selffix/retry/, one JSON file per session, filename = sid so
# re-enqueue overwrites). Sourced by selffix-trigger.sh (limit-brick path)
# and selffix-run.sh (failure paths); consumed once per gardener-gate tick
# by gardener_gate.py:process_retry_queue with SELFFIX_RETRY_ATTEMPT=1.
# Best-effort by contract: callers treat a failed enqueue as a lost retro,
# never as an error that may block the SessionEnd path.
#
# Canonical source: deploy/scripts/selffix-retry-lib.sh @
# taburetka123/claude-orchestrator — deployed to ~/.claude/scripts/ by
# setup.sh. Edit the repo copy, not the deployed one.

selffix_enqueue_retry() {
  # selffix_enqueue_retry <sid> <transcript-path> <reason>
  local sid="$1" transcript="$2" reason="$3"
  local retry_dir="$HOME/.claude/dockwright/selffix/retry"
  mkdir -p "$retry_dir" 2>/dev/null || return 1
  SELFFIX_RETRY_SID="$sid" SELFFIX_RETRY_TRANSCRIPT="$transcript" \
  SELFFIX_RETRY_REASON="$reason" SELFFIX_RETRY_DIR="$retry_dir" \
  /usr/bin/python3 - <<'PY' 2>/dev/null
import json, os, tempfile, time
d = os.environ["SELFFIX_RETRY_DIR"]
entry = {
    "sid": os.environ["SELFFIX_RETRY_SID"],
    "transcript_path": os.environ["SELFFIX_RETRY_TRANSCRIPT"],
    "attempts": 0,
    "enqueued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "reason": os.environ["SELFFIX_RETRY_REASON"],
}
fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
with os.fdopen(fd, "w") as f:
    json.dump(entry, f)
os.replace(tmp, os.path.join(d, entry["sid"] + ".json"))
PY
}
