#!/usr/bin/env bash
# runlock.sh — shared single-runner mutex for fleet `claude -p` analyst runs.
#
# Purpose: serialize headless/visible analyst sessions (selffix retros,
# gardener digests) so concurrent spawns never stampede the Anthropic rate
# limiter (PRD v2 §8.1/§9.3). One lock domain, two acquisition disciplines:
# selffix WAITS (a retro must eventually run), gardener TRIES (the hourly
# tick is the retry).
#
# Canonical lock home: ~/.claude/locks/analyst-run.lock — a NEUTRAL path.
# (The lock previously lived inside one consumer's data dir,
# selffix-findings/.run.lock; a wholesale prune of that dir would have
# deleted a held lock — arch-soundness review A5 / coupling F5.)
#
# Protocol: atomic `mkdir` on the lock dir; the winner writes its pid to
# <lock>/pid. A lock dir with no readable pid is mid-acquisition and treated
# as held. Steal is allowed ONLY from:
#   - a dead holder (pid no longer alive — SIGKILL/power-loss bypassed its
#     release trap), or
#   - an over-aged holder (lock-dir mtime older than RUNLOCK_MAX_HOLD_SEC —
#     a live-but-wedged holder whose own runtime watchdog failed; consumers
#     bound their runs at ≤45min, so 2h of holding is pathology).
# A waiter that exhausts its wait budget GIVES UP (returns 1). It never
# breaks a live, in-budget holder: the old selffix 2h valve rm -rf'd live
# holders' locks, the evicted holder's EXIT trap then deleted the thief's
# lock, and mutual exclusion collapsed under exactly the retro storm the
# lock exists to prevent (arch review A5 / counterfactual F1).
#
# Steal is mv-then-remove: rename the dir to a unique grave first, so two
# concurrent stealers can't both win, and a racing stealer holding a stale
# view can never delete a successor's fresh lock.
#
# Release deletes the lock only while <lock>/pid is still the caller's own
# pid — an evicted or superseded holder's release is a no-op.
#
# Usage (source this file, then):
#   trap runlock_release EXIT INT TERM
#   runlock_acquire <lock-dir> wait [max-wait-sec]   # poll until acquired or budget spent
#   runlock_acquire <lock-dir> try                   # single attempt, no waiting
#   runlock_release                                  # idempotent, owner-checked
#
# Tunables (env): RUNLOCK_POLL_SEC (default 15), RUNLOCK_MAX_HOLD_SEC
# (default 7200).
#
# Consumers: selffix-run.sh (~/.claude repo), gardener-run.sh (this repo).
# Tests: tests/test_runlock.py.

RUNLOCK_DIR=""
RUNLOCK_HELD=""

_runlock_holder_pid() { cat "$RUNLOCK_DIR/pid" 2>/dev/null; }

_runlock_dir_age() {
  local mtime
  mtime=$(stat -f %m "$RUNLOCK_DIR" 2>/dev/null || stat -c %Y "$RUNLOCK_DIR" 2>/dev/null)
  if [ -z "$mtime" ]; then echo 0; return; fi
  echo $(( $(date +%s) - mtime ))
}

_runlock_try_steal() {
  # Steal only a dead or over-aged holder. Empty pid = mid-acquisition =
  # held (the fresh dir mtime keeps it out of the over-age branch too).
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
  mv "$RUNLOCK_DIR" "$grave" 2>/dev/null || return 1  # lost the race — fine
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
      return 1  # bounded queue: drop, never break a live in-budget holder
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
