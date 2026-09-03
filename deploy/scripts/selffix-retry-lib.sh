#!/usr/bin/env bash

selffix_enqueue_retry() {
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
