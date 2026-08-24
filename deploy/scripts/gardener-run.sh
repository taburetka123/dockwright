#!/usr/bin/env bash
# Gardener analyst-run wrapper — Phase 0. Spawned detached by gardener_gate.py; clones the
# selffix-run.sh patterns (shared mutex, watchdog, env hygiene, Status line).
#
# Default mode is VISIBLE (A1): launches a regular, observable Claude session
# in a tmux window (in the "claude-workers" session when one exists) with NO
# orchestrator identity — CLAUDE_AGENT et al are stripped, so the hooks' gate
# ignores it and it never registers as a worker. The session writes exactly
# one file (the digest); spawn settings allow Write/Edit only under
# ~/.claude/dockwright/gardener/ and deny the live substrate outright. The wrapper joins
# on the digest's "Status:" line; on timeout it notifies + releases the mutex
# but NEVER kills the tab (a human may be mid-interjection — PRD §9.3).
#
# GARDENER_HEADLESS=1 switches to the selffix-style headless path
# (claude -p, stdout capture, --disallowedTools, hard process-group watchdog).
# DEFERRED-SPIKE: per PRD §12 the headless stdout contract is verified when
# this mode is first enabled, not in Phase 0. Flip bar lives in PRD §16 Q5.
#
# Usage: gardener-run.sh --trigger <accum|floor|force|frontier> [--lane digest|frontier] [--dry-run]
#
# Contract with gardener_gate.py: the gate pre-checks stop file / cap / lock
# cheaply and spawns this script; this script re-checks stop + lock
# atomically (the gate's check is advisory), writes run_start BEFORE spawning
# the session (write-ahead), and always appends a terminal run_end.
#
# ⚠️  The VISIBLE spawn tail launches a REAL `claude` session onto the LIVE tmux
#     socket by default (TMUX_SOCK defaults to `dockwright`; -L namespaces by
#     uid, not HOME, so a sandboxed HOME does NOT isolate it). Any probe or test
#     that runs this script MUST pass --dry-run, which prints the resolved spawn
#     plan and exits BEFORE touching tmux. A run under a sandboxed HOME against
#     the live/default socket is REFUSED (exit 3) — the same 2026-07-17 vector
#     that put two rogue managers on the operator's fleet via bootstrap-recreate.

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
# deprecated, one release: operator stop-file honored at either home
STOP_FILE_LEGACY="$HOMEDIR/.claude/gardener-stop"
FINDINGS_DIR="$HOMEDIR/.claude/dockwright/selffix/findings"
LOCK_DIR="$HOMEDIR/.claude/locks/analyst-run.lock"

TRIGGER="manual"
LANE="digest"
DRY_RUN=""
while [ $# -gt 0 ]; do
  case "$1" in
    # shift-guarded: a trailing flag with no value must not spin — `shift 2`
    # at $#=1 is a non-shifting no-op under plain `set -u` (verifier I3 on #63,
    # reproduced as a CPU-pinned hang).
    --trigger) TRIGGER="${2:-manual}"; shift; [ $# -gt 0 ] && shift ;;
    --lane)    LANE="${2:-digest}";    shift; [ $# -gt 0 ] && shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) shift ;;
  esac
done
case "$LANE" in digest|frontier) ;; *) LANE="digest" ;; esac

# [modules] gardener toggle: gardener=false must no-op every analyst run
# (design-gate; this is the backstop even if a stale gate ever spawns us). Bail
# after arg parse, before the mutex / tmux spawn. Best-effort source → fail-open
# = enabled.
if command -v dockwright_module_enabled >/dev/null 2>&1 && ! dockwright_module_enabled gardener; then
  mkdir -p "$GARDENER_DIR"
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  skip  module-off ([modules] gardener=false)" >> "$RUN_LOG"
  exit 0
fi

MODE="visible"
if [ "${GARDENER_HEADLESS:-}" = "1" ]; then
  MODE="headless"
fi

# Per-loop wiring (the wrapper is shared MECHANISM — spawn/watchdog/guard/
# audit/postrun — parameterized by lane; each loop keeps its OWN gate, stop
# file, marker, and budget: arch-soundness C1/M7).
if [ "$LANE" = "frontier" ]; then
  STOP_FILE="$HOMEDIR/.claude/dockwright/frontier-stop"
  STOP_FILE_LEGACY="$HOMEDIR/.claude/frontier-stop"
  MARKER="$GARDENER_DIR/last-frontier-run"
fi

# Visible-mode join budget: 30 min to overdue, then a grace window before the
# run is ledger-marked timed-out and the mutex freed. The tab itself is never
# killed. Headless mode uses the selffix-style hard watchdog instead.
#
# Known tradeoff (verifier round, accepted): the mutex frees when THIS wrapper
# exits (≤ TIMEOUT+GRACE ≈ 45min) while a slow human-driven visible session
# can outlive it — so a later selffix retro, or a --force, can briefly overlap
# with a still-open analyst tab. Chosen deliberately: holding the lock for the
# tab's lifetime would starve selffix retros for as long as a human idles the
# tab (hours), which is the worse failure. Overlap from the gate side is also
# bounded by the min-run-gap cooldown in gardener_gate.py.
TIMEOUT_SEC="${GARDENER_TIMEOUT_SEC:-1800}"
if [ "$LANE" = "frontier" ] && [ -z "${GARDENER_TIMEOUT_SEC:-}" ]; then
  # Web-heavy sweep gets a longer default join budget than the local digest.
  TIMEOUT_SEC=2700
fi
GRACE_SEC="${GARDENER_GRACE_SEC:-900}"
POLL_SEC="${GARDENER_POLL_SEC:-20}"
# cwd for the spawned session: must be a dir Claude Code already trusts, or
# the first prompt blocks on the trust dialog (manager.md "Worker cwd" rule).
# Resolution: GARDENER_CWD env > [paths] dockwright_repo > ~/.claude (always a
# trusted dir). The dockwright repo is the ideal cwd (gardener_spend.py reads
# its transcripts, git reads target its history), but it's operator-configured.
if [ -z "${GARDENER_CWD:-}" ]; then
  GARDENER_CWD="$(command -v dockwright_repo_path >/dev/null 2>&1 && dockwright_repo_path 2>/dev/null || true)"
  [ -n "$GARDENER_CWD" ] || GARDENER_CWD="$HOMEDIR/.claude"
fi

TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
run_log() { echo "$(TS)  $1  ${RUN_ID:--}  ${2:-}" >> "$RUN_LOG"; }

notify() {
  # Best-effort local notification; never blocks or fails the run.
  # No-op under pytest (PYTEST_CURRENT_TEST, inherited by child processes):
  # tests exec this script for real, and a test must never fire a desktop
  # notification (the 2026-07-03 gardener-gate leak class).
  if [ -n "${PYTEST_CURRENT_TEST:-}" ]; then return 0; fi
  /usr/bin/osascript -e "display notification \"${1//\"/}\" with title \"gardener\"" \
    >/dev/null 2>&1 || true
}

ledger_append() {
  # ledger_append <event> [key=value]... — JSONL via python3 so quoting in
  # free-text values can't corrupt the ledger.
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

# --- Stop re-check (the gate's check races a just-touched stop file) -------
if [ -f "$STOP_FILE" ] || [ -f "$STOP_FILE_LEGACY" ]; then
  run_log "skip" "stopped"
  exit 0
fi

# --- Shared single-runner mutex (same lock selffix-run.sh uses) ------------
# Protocol lives in runlock.sh (steal only dead/over-aged holders,
# owner-checked release). try-mode: the gate fires hourly, so a busy lock
# just means "this tick loses"; the next tick retries.
. "$HOMEDIR/.claude/scripts/runlock.sh"
LIVE_WINDOW_SIDECAR=""
_gardener_cleanup() {
  # Sidecar removal must ride the same trap as the mutex: once the wrapper is
  # gone nothing else supervises the pane, so its M-2 protection must drop
  # (fail toward alarming — a crashed wrapper's sidecar also ages out via the
  # reader-side mtime TTL).
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
# Write-ahead by design: the gate's 3/week cap counts run_start events, i.e.
# spawn ATTEMPTS, not completed digests — an abandoned visible tab is real
# token/attention cost and should consume budget (PRD §10).
ledger_append run_start run_id="$RUN_ID" trigger="$TRIGGER" lane="$LANE" mode="$MODE" digest="$DIGEST"

# --- Phase-1 digest prompt: the dockwright-gardener-digest skill owns the method
# (observe → cluster → rank → pre-draft, PRD §7); the prompt is just the
# invocation + run parameters. mode=full until the ledger holds its first
# `proposal` event (PRD §12 Phase 1: full-backlog first run), incremental
# afterwards (marker-windowed). -----------------------------------------------
# Whitespace-tolerant match — not coupled to json.dumps' exact spacing
# (verifier minor on #59).
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

# --- Spawn-time write scoping (visible mode) --------------------------------
# THE mechanical barrier is the PreToolUse write-guard hook
# (gardener-write-guard.py): it denies any Write/Edit/NotebookEdit whose
# resolved target is outside ~/.claude/dockwright/gardener/, fail-closed, decisively.
# It must be a hook, not permission rules: permission arrays MERGE across
# settings sources and this user's settings.json carries Write(*)/Edit(*) in
# allow with defaultMode "auto" — so there is NO ask-tier for writes on this
# machine, and deny>allow>ask cannot express "everything except X". Verified
# empirically (verifier round, 2026-06-11): an outside-scope Write succeeded
# under a replace-allow payload (arrays union, Write(*) survived) and was
# mechanically denied once the guard hook was added.
#
# The permission deny-rules below stay as substrate belt-and-braces (they
# produce a clearer denial message and hold even if the hook file went
# missing — hook-missing reads as command-failure, not as allow, but cheap
# redundancy on the highest-stakes paths is worth 20 lines). ~/.claude paths
# additionally trip the runtime's own "sensitive file" check (observed: it
# asks in interactive sessions, hard-denies in -p). Rule-path syntax: `~/` is
# the home-relative form; a single-leading-slash path would be read as
# settings-FILE-relative.
#
# Honest layer accounting (PRD §9.1): mechanical = guard hook + deny-rules +
# post-run audit; instructional = the prompt's HARD RULES; Bash is NOT
# path-scopable by any of these (command strings aren't reliably parseable
# for write-ness) and falls to the runtime's own vetting plus the watching
# human. Before any headless flip, Bash needs its own treatment (PRD §16 Q5).
# The settings payload lives in a deployed PRESET (single source — a third
# inline copy was looming, arch review B5): deploy/presets/ →
# setup.sh §4c rsync → ~/.claude/dockwright/presets/. Copied per-run into
# RUN_DIR as an immutable audit snapshot; a missing preset fails the run
# loudly rather than running unguarded.
SETTINGS_PRESET="$HOMEDIR/.claude/dockwright/presets/gardener-analyst-settings.json"
if ! cp "$SETTINGS_PRESET" "$SETTINGS_FILE" 2>/dev/null; then
  run_log "error" "settings preset missing: $SETTINGS_PRESET — run setup.sh"
  ledger_append run_end run_id="$RUN_ID" status="error" audit="skipped" lane="$LANE" detail="settings-preset-missing"
  notify "gardener $RUN_ID: settings preset missing — run setup.sh"
  exit 0
fi

git -C "$HOMEDIR/.claude" status --porcelain 2>/dev/null | sort > "$PRE_STATUS" || true

finish_run() {
  # finish_run <status> [detail] — audit, ledger run_end, marker, notify.
  #
  # The audit is ADVISORY, not attribution: it flags every ~/.claude write in
  # the run window regardless of author. The analyst session, the human
  # hand-editing a rule mid-run, and another live session's auto-commit are
  # indistinguishable at the repo level (same git author), so a hit means
  # "review whether this was yours", never "the gardener breached" — exactly
  # the visible-mode posture: the watching human adjudicates.
  local status="$1" detail="${2:-}"
  git -C "$HOMEDIR/.claude" status --porcelain 2>/dev/null | sort > "$POST_STATUS" || true
  # Three sweep halves, because no single one covers ~/.claude: git status
  # diff + commits-in-window see the TRACKED zone only — ~/.claude/.gitignore
  # is a `*`-whitelist, so new files in dockwright/ (orchestrator state,
  # manager-memory), projects/ etc are git-invisible. The mtime find covers the
  # gitignored zones, minus known constant-churn HARNESS MACHINERY that any
  # concurrent session churns and that bears no behavior (orchestrator state
  # rewrites every few seconds; transcripts under projects/ append per live session —
  # excluded as *.jsonl while projects/*/memory/*.md stays IN scope, since
  # auto-memory is exactly a silently-writable surface worth sweeping;
  # backups/ sessions/ tasks/ are runtime state — attribution-confirmed noise
  # on the first re-spike run). Excluded dirs remain reachable by analyst
  # Bash in principle — the guard hook + watching human cover that vector,
  # not this sweep (PRD §9.1).
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
    notify "gardener $RUN_ID: writes outside gardener/ in the run window (may include concurrent-session edits) — review $RUN_DIR/audit-stray-paths.txt"
  fi
  # Token spend (observability only): resolvable only when the session left a
  # transcript whose head carries this RUN_ID (visible mode; headless runs use
  # --no-session-persistence and yield nothing). Best-effort — an empty result
  # adds no keys and never fails the run.
  local spend
  spend=$(/usr/bin/python3 "$HOMEDIR/.claude/scripts/gardener_spend.py" "$GARDENER_CWD" "$RUN_ID" 2>/dev/null || true)
  # Phase-1 artifact validation: scope-guard (FR-8, declared targets AND diff
  # bodies) + ledger entries for every artifact the ledger doesn't know yet.
  # The known-set is ledger-derived inside the post-processor — no pre-run
  # snapshot, so an artifact appearing between runs can never escape
  # validation (verifier finding on #59). Runs on every terminal status —
  # a timed-out session may still have written artifacts worth validating.
  postrun_summary=$(/usr/bin/python3 "$HOMEDIR/.claude/scripts/gardener_postrun.py" postrun \
      --run-id "$RUN_ID" --lane "$LANE" 2>&1) || true
  run_log "postrun" "$postrun_summary"
  # Surface the birth gate: gardener_postrun.py quarantines apply-check
  # failures to proposals/rejected/ SILENTLY, and the `|| true` above swallows
  # a crashed postrun. Fires only on real rejections or an unparseable
  # summary — postrun's pass classes (env-lenient skips, prose-diff briefs)
  # stay quiet.
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
  # $spend is deliberately unquoted: it word-splits into key=value args for
  # ledger_append (digit values only, no quoting hazard).
  ledger_append run_end run_id="$RUN_ID" status="$status" audit="$audit" detail="$detail" lane="$LANE" postrun="$postrun_summary" $spend
  if [ "$status" = "ok" ]; then
    touch "$MARKER"
    run_log "finished" "digest=$DIGEST audit=$audit"
    notify "gardener digest ready: $RUN_ID ($audit)"
    # Backlog-escalation notify: six consecutive digests (07-09..07-20) each wrote
    # a "backlog growing" Notes line nobody acted on, because the routine notify()
    # above fires identically regardless of size — this is the size/age-gated
    # second signal those Notes lines were asking for.
    PENDING_DIR="$GARDENER_DIR/proposals/pending"
    PENDING_COUNT=$(ls "$PENDING_DIR"/*.md 2>/dev/null | wc -l | tr -d ' ')
    OLDEST_FILE=$(ls -t "$PENDING_DIR"/*.md 2>/dev/null | tail -1)
    OLDEST_AGE_DAYS=0
    if [ -n "${OLDEST_FILE:-}" ]; then
      OLDEST_MTIME=$(date -r "$OLDEST_FILE" +%s 2>/dev/null || date +%s)
      OLDEST_AGE_DAYS=$(( ($(date +%s) - OLDEST_MTIME) / 86400 ))
    fi
    if [ "${PENDING_COUNT:-0}" -gt 20 ] || [ "$OLDEST_AGE_DAYS" -gt 14 ]; then
      notify "gardener backlog: $PENDING_COUNT pending proposals, oldest ~${OLDEST_AGE_DAYS}d — run /dockwright-selffix-review"
    fi
    # Six digests' "backlog growing" Notes lines compounded to nothing — the
    # size/age-gated second signal those Notes were asking for. date -r, not
    # stat: the platform-split stat-probe class is banned under deploy/
    # (tests/test_mtime_probe.py, the rc.2 N-1 Linux-boot bug class).
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
      notify "gardener backlog: $pending_count pending proposals, oldest ~${oldest_age_days}d — run /dockwright-selffix-review"
      ledger_append backlog run_id="$RUN_ID" pending="$pending_count" oldest_days="$oldest_age_days" lane="$LANE"
    fi
  else
    run_log "finished-$status" "audit=$audit $detail"
  fi
}

if [ "$MODE" = "headless" ]; then
  # --- DEFERRED-SPIKE path (PRD §12): not exercised in Phase 0. -------------
  # Tool-surface lockdown, VERBATIM the quadruple in selffix-run.sh (see the long
  # rationale there: untrusted-input child, the 2026-07-29 manager-kill incident,
  # and why the denylist below is no longer the primary defence). Duplicated
  # inline rather than sourced because a helper would be fail-open;
  # tests/test_headless_lane_lockdown.py runs both scripts for real and fails if
  # they drift.
  #
  # --add-dir / --allowedTools are as load-bearing here as in selffix-run.sh, and
  # for the same reason: --setting-sources "" means the operator's blanket allow
  # rules no longer paper over missing grants. Without them this lane was
  # measurably HOLLOW — on the exact prior argv, `ls` and `Read` of the findings
  # corpus were both DENIED and the model printed `Status: ok` anyway, which the
  # old join condition accepted: run recorded ok, cadence MARKER touched (which
  # SUPPRESSES the next digest), operator notified that a digest was ready. That
  # is the "gate passes because it had nothing to check" shape from
  # ~/.claude/rules/drift-guard-tests.md, with a suppression and a false success
  # notice on top. Hence BOTH the grants below and the content check at the join:
  # a model's self-reported Status: line is never evidence the lane worked.
  # Scope is deliberately the two dockwright state dirs, NOT ~/.claude — the
  # child reads untrusted evidence and must not reach settings.json/.claude.json.
  # That boundary is real ONLY because --allowedTools carries NO BARE TOOL NAMES:
  # a bare `Read`/`Grep`/`Glob` pre-approves that tool for ANY path and overrides
  # --add-dir entirely. Measured with the bare tail present, the child read
  # ~/.claude/settings.json, ~/.claude.json (oauthAccount, the project trust list,
  # MCP approvals) and ~/.ssh/config; with it removed those are DENIED and every
  # in-scope read still works, because --add-dir already grants Read/Grep/Glob
  # within scope. Grant by path (`Read(<dir>/**)`) or not at all.
  #
  # STILL DEFERRED, and NOT fixed here (both pre-date this change): the digest
  # skill's secondary sweeps (closed/, manager-memory/, rules+agents byte counts)
  # fall outside those two dirs, and `--tools` has no Write, so proposals can only
  # arrive via the skill's `=== ARTIFACT: <path> ===` stdout fallback — which
  # nothing here parses (stdout goes straight into $DIGEST). Whoever flips the
  # spike (PRD §12 / §16 Q5) owns those two together with a per-lane surface.
  #
  # --lane frontier stays REFUSED: its skill is deliberately web-heavy
  # (WebSearch/WebFetch, research subagents) and cannot work under this surface.
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
  # STDIN, not `-p <arg>`: the skill body opens with YAML frontmatter, and an
  # argument starting with `---` is parsed as an option (`error: unknown option`).
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
  # A `Status:` line alone is the child's self-report and proves nothing: a run
  # that could read nothing still printed it. Require real digest CONTENT before
  # recording ok — otherwise the marker would suppress the next digest on the
  # strength of an empty file. Two independent checks because `^## ` alone is a
  # SHAPE check and the prompt itself instructs the model to emit those headings:
  # a child that read nothing could echo the template's section titles over empty
  # bodies. The byte floor is the same idea as selffix-run.sh's 200-byte stub
  # guard — real digests run to several KB.
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

# --- VISIBLE path (Phase 0–1 default, Amendment A1) --------------------------
TMUX_SOCK="${DOCKWRIGHT_TMUX_SOCKET:-${CLAUDE_ORCH_TMUX_SOCKET:-dockwright}}"
TMUX_CONF_FILE="$HOMEDIR/.claude/dockwright/dockwright.tmux.conf"
# Pre-rename conf homes — retire with CLAUDE_ORCH_TMUX_SOCKET (one release).
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
# Refuse the incident shape: a sandboxed HOME does NOT isolate tmux (-L
# namespaces by uid, not HOME) — a probe run under HOME=/tmp/... would spawn
# onto the LIVE socket (2026-07-17: two rogue managers via bootstrap-recreate).
# Real runs always have HOME == the uid's passwd home; probes must use --dry-run.
# Gated on the socket: a sandboxed-HOME run against an EXPLICIT scratch socket is
# a legitimate test shape (the real-tmux behavioral tests below do exactly that),
# so the guard fires only on sandbox-HOME + live/default socket.
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

# Shield the live pane from the M-2 orphan-window alarm: the gardener session
# deliberately never registers an active record, so without this sidecar the
# stale monitor would page ORPHAN_WINDOW ~2min into every visible run.
mkdir -p "$GARDENER_DIR/live-windows"
LIVE_WINDOW_SIDECAR="$GARDENER_DIR/live-windows/$RUN_ID.window"
printf '%s' "$WINDOW_ID" > "$LIVE_WINDOW_SIDECAR"

# Join on the digest's Status line. Overdue → notify, wait one grace window,
# then mark timed-out and exit (EXIT trap frees the mutex). The tab is the
# human's to close — never killed (PRD §9.3 / A1).
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
  # Clean finish: the digest is durable and postrun ran inside finish_run —
  # the interactive pane's only remaining value is a leak (observed windows
  # dated 07-09/10/13 lingering a week). Error/timeout tabs stay open for
  # diagnosis/interjection (PRD §9.3) and the M-2 alarm nags them honestly.
  if tmux -L "$TMUX_SOCK" kill-pane -t "$WINDOW_ID" 2>/dev/null; then
    run_log "window_killed" "window_id=$WINDOW_ID"
    ledger_append window_killed run_id="$RUN_ID" window_id="$WINDOW_ID"
  else
    run_log "window_kill_failed" "window_id=$WINDOW_ID"
  fi
elif grep -q '^Status:' "$DIGEST" 2>/dev/null; then
  finish_run error "$(grep '^Status:' "$DIGEST" | tail -1)"
else
  # Known Phase-0 behavior: a human who finishes driving the session AFTER
  # this deadline leaves a valid digest whose marker was never advanced, so a
  # later tick may re-digest the same backlog. Bounded by the 3/week cap;
  # revisit if it ever happens in practice.
  finish_run timeout "no Status line within $((TIMEOUT_SEC + GRACE_SEC))s; tab left open"
fi
exit 0
