#!/usr/bin/env bash

RUNLOCK_DIR=""
RUNLOCK_HELD=""

_runlock_holder_pid() { cat "$RUNLOCK_DIR/pid" 2>/dev/null; }

_runlock_dir_age() {
  local mtime
  mtime=$(date -r "$RUNLOCK_DIR" +%s 2>/dev/null)
  if [ -z "$mtime" ]; then echo 0; return; fi
  echo $(( $(date +%s) - mtime ))
}

_runlock_try_steal() {
  local max_hold="${RUNLOCK_MAX_HOLD_SEC:-7200}"
  local holder steal=""
  holder=$(_runlock_holder_pid)
  if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then
    steal="dead"
  elif [ "$(_runlock_dir_age)" -gt "$max_hold" ]; then
    steal="over-aged"
  fi
  [ -n "$steal" ] || return 1
  local grave="$RUNLOCK_DIR.stale.$$.$RANDOM"
  mv "$RUNLOCK_DIR" "$grave" 2>/dev/null || return 1
  rm -rf "$grave" 2>/dev/null
  return 0
}

runlock_acquire() {
  RUNLOCK_DIR="${1:?runlock_acquire: lock dir required}"
  local mode="${2:?runlock_acquire: mode required (wait|try)}"
  local max_wait="${3:-7200}"
  local poll="${RUNLOCK_POLL_SEC:-15}"
  mkdir -p "$(dirname "$RUNLOCK_DIR")" 2>/dev/null
  local waited=0
  while ! mkdir "$RUNLOCK_DIR" 2>/dev/null; do
    if _runlock_try_steal; then
      continue
    fi
    if [ "$mode" = "try" ]; then
      return 1
    fi
    if [ "$waited" -ge "$max_wait" ]; then
      return 1
    fi
    sleep "$poll"
    waited=$((waited + poll))
  done
  echo "$$" > "$RUNLOCK_DIR/pid"
  RUNLOCK_HELD=1
  return 0
}

runlock_release() {
  [ -n "${RUNLOCK_HELD:-}" ] || return 0
  if [ "$(_runlock_holder_pid)" = "$$" ]; then
    rm -rf "$RUNLOCK_DIR" 2>/dev/null
  fi
  RUNLOCK_HELD=""
  return 0
}
