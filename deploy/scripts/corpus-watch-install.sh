#!/usr/bin/env bash
# Corpus-watch installer (see deploy/loops-registry.md, corpus-watch block —
# deployed to ~/.claude/dockwright/loops-registry.md; set
# [loops.status_overrides.corpus-watch] in dockwright.toml to live after this
# runs).
# Idempotent. Creates the corpus-watch state dir and generates + loads the
# hourly launchd tick. The tick is LLM-free file/git arithmetic
# (corpus_watch_gate.py); it watches ~/.claude for direct edits to gate-mapped
# instruction surfaces and spawns corpus-watch-run.sh (which takes the shared
# analyst run-lock and runs gardener_eval_gate.py) only when gate-mapped
# targets actually changed, past a 30-min quiet period and a 6h per-lane
# cooldown.
#
# [modules] gardener=false: the tick itself no-ops SILENTLY (gardener_gate's
# clean-off-switch precedent) — this installer does NOT refuse to install on
# a module-off machine, unlike gardener-install.sh's whole-subsystem gate;
# the plist can be armed ahead of enabling the module, and loops-status
# reading it as stale on such a machine is expected (registry `gate:` field).
#
# DISABLE (one line):
#   touch ~/.claude/dockwright/corpus-watch-stop     # soft stop: tick exits before scanning
# UNINSTALL the scheduler (one line — label below is this operator's default,
# com.dockwright; the actual label is dockwright.toml [loops].label_prefix +
# ".corpus-watch", see loop-label-prefix.sh):
#   launchctl bootout "gui/$(id -u)/com.dockwright.corpus-watch" && rm ~/Library/LaunchAgents/com.dockwright.corpus-watch.plist
# DRY-RUN preview (no mutations):
#   python3 ~/.claude/scripts/corpus_watch_gate.py --dry-run
#
# After running this installer, set `status = "live"` under
# [loops.status_overrides.corpus-watch] in dockwright.toml.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=loop-label-prefix.sh
source "$SCRIPT_DIR/loop-label-prefix.sh"

HOMEDIR="${HOME:?}"
WATCH_DIR="$HOMEDIR/.claude/dockwright/corpus-watch"
SCRIPTS_DIR="$HOMEDIR/.claude/scripts"
PLIST_LABEL="$(dockwright_loop_label_prefix).corpus-watch"
PLIST_PATH="$HOMEDIR/Library/LaunchAgents/$PLIST_LABEL.plist"
SCRIPT="$SCRIPTS_DIR/corpus_watch_gate.py"
RUN_SCRIPT="$SCRIPTS_DIR/corpus-watch-run.sh"

# Baked launchd PATH. corpus_watch_gate.py shells out to `git` (reading
# ~/.claude history); corpus-watch-run.sh (its detached spawn target) sources
# runlock.sh and runs gardener_eval_gate.py, whose SUT/judge subprocesses
# invoke `claude -p` by bare name — both must resolve under launchd's minimal
# env. The generic dirs below carry a Homebrew/system git and the standard
# claude CLI install location; set EXTRA_PATH before running this installer
# to append your own.
BAKED_PATH="/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:$HOMEDIR/.local/bin"
if [ -n "${EXTRA_PATH:-}" ]; then
  BAKED_PATH="$BAKED_PATH:$EXTRA_PATH"
fi

echo "→ Creating $WATCH_DIR"
mkdir -p "$WATCH_DIR"

if [ ! -f "$SCRIPT" ]; then
  echo "ERROR: $SCRIPT not deployed — run setup.sh first (it cp-deploys deploy/scripts/)." >&2
  exit 1
fi

if [ ! -f "$RUN_SCRIPT" ]; then
  echo "WARN: $RUN_SCRIPT not deployed yet — the tick will hit spawn-blocked (loud, state un-advanced) until setup.sh deploys it." >&2
fi

echo "→ Writing $PLIST_PATH (hourly tick)"
mkdir -p "$HOMEDIR/Library/LaunchAgents"
cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$SCRIPT</string>
    </array>
    <!-- Hourly tick. LLM-free git/file arithmetic; the gate's own quiet
         period, run-lock pre-check and 6h per-lane cooldown make the actual
         eval-spawn cadence conservative (worst case 4 runs/day).
         Disable: touch ~/.claude/dockwright/corpus-watch-stop
         Uninstall: launchctl bootout gui/\$(id -u)/$PLIST_LABEL && rm $PLIST_PATH -->
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>$WATCH_DIR/launchd-out.log</string>
    <key>StandardErrorPath</key>
    <string>$WATCH_DIR/launchd-err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <!-- git and the claude CLI must both be on PATH: the gate reads
             ~/.claude's git history directly, and its detached spawn target
             (corpus-watch-run.sh -> gardener_eval_gate.py) invokes `claude -p`
             by bare name for the SUT/judge subprocesses. Append your own dir
             via EXTRA_PATH when running the installer. -->
        <key>PATH</key>
        <string>$BAKED_PATH</string>
    </dict>
</dict>
</plist>
EOF

echo "→ (Re)loading launchd job $PLIST_LABEL"
launchctl bootout "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
# `launchctl list | grep -q` SIGPIPEs launchctl when grep exits early — under
# pipefail the pipeline then "fails" and prints a spurious WARN. Query the
# label directly instead.
if launchctl list "$PLIST_LABEL" >/dev/null 2>&1; then
  echo "→ Loaded: $PLIST_LABEL (hourly)"
else
  echo "WARN: $PLIST_LABEL not visible in launchctl list after bootstrap — check $PLIST_PATH" >&2
fi

cat <<EOF

Corpus-watch installed.
  Tick (hourly, LLM-free):  /usr/bin/python3 $SCRIPT   → log: $WATCH_DIR/check.log
  Dry-run preview:          python3 $SCRIPT --dry-run
  Logs: $WATCH_DIR/launchd-out.log, $WATCH_DIR/launchd-err.log
  STOP (soft):     touch ~/.claude/dockwright/corpus-watch-stop
  Uninstall:       launchctl bootout "gui/\$(id -u)/$PLIST_LABEL" && rm $PLIST_PATH

NEXT STEP: set status = "live" under [loops.status_overrides.corpus-watch]
  in dockwright.toml.
EOF
