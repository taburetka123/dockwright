#!/usr/bin/env bash
# Boot-lite watchdog installer (see deploy/loops-registry.md, bootlite-watchdog
# block — deployed to ~/.claude/dockwright/loops-registry.md; set [loops.status_overrides.bootlite-watchdog]
# in dockwright.toml to live after this runs).
# Idempotent. Creates the bootlite state dir and generates + loads the hourly
# launchd tick. The tick is LLM-free file/pid arithmetic (bootlite_watchdog.py);
# it notifies when live worker records have no live parent manager and, ONLY
# under CLAUDE_ORCH_AUTONUDGE=1, types a checkpoint-and-finish message into the
# orphaned workers' panes.
#
# Knobs baked into the plist FROM THE INSTALLER'S ENVIRONMENT (launchd inherits
# no shell env — an exported variable that isn't baked here is dead at tick time):
#   CLAUDE_ORCH_AUTONUDGE=1     enable the worker nudge (default: notify only)
#   BOOTLITE_RENOTIFY_SEC=N     renotify cadence per stretch (default 14400 = 4h)
#   BOOTLITE_MAX_NOTIFY=N       notification cap per stretch (default 6)
# Re-run this installer after changing any of them.
#
# DISABLE (one line):
#   touch ~/.claude/dockwright/bootlite-stop            # soft stop: tick exits before scanning
# UNINSTALL the scheduler (one line — label below is this operator's default,
# com.dockwright; the actual label is dockwright.toml [loops].label_prefix +
# ".bootlite-watchdog", see loop-label-prefix.sh):
#   launchctl bootout "gui/$(id -u)/com.dockwright.bootlite-watchdog" && rm ~/Library/LaunchAgents/com.dockwright.bootlite-watchdog.plist

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=loop-label-prefix.sh
source "$SCRIPT_DIR/loop-label-prefix.sh"

HOMEDIR="${HOME:?}"
BOOTLITE_DIR="$HOMEDIR/.claude/dockwright/bootlite"
SCRIPTS_DIR="$HOMEDIR/.claude/scripts"
PLIST_LABEL="$(dockwright_loop_label_prefix).bootlite-watchdog"
PLIST_PATH="$HOMEDIR/Library/LaunchAgents/$PLIST_LABEL.plist"
WATCHDOG_PATH="$SCRIPTS_DIR/bootlite_watchdog.py"

echo "→ Creating $BOOTLITE_DIR"
mkdir -p "$BOOTLITE_DIR"

if [ ! -f "$WATCHDOG_PATH" ]; then
  echo "ERROR: $WATCHDOG_PATH not deployed — run setup.sh first (it cp-deploys deploy/scripts/)." >&2
  exit 1
fi

ENV_EXTRA=""
if [ "${CLAUDE_ORCH_AUTONUDGE:-}" = "1" ]; then
  ENV_EXTRA="$ENV_EXTRA
        <key>CLAUDE_ORCH_AUTONUDGE</key>
        <string>1</string>"
  echo "→ Baking CLAUDE_ORCH_AUTONUDGE=1 into the plist (nudges enabled)"
fi
if [ -n "${BOOTLITE_RENOTIFY_SEC:-}" ]; then
  ENV_EXTRA="$ENV_EXTRA
        <key>BOOTLITE_RENOTIFY_SEC</key>
        <string>$BOOTLITE_RENOTIFY_SEC</string>"
  echo "→ Baking BOOTLITE_RENOTIFY_SEC=$BOOTLITE_RENOTIFY_SEC into the plist"
fi
if [ -n "${BOOTLITE_MAX_NOTIFY:-}" ]; then
  ENV_EXTRA="$ENV_EXTRA
        <key>BOOTLITE_MAX_NOTIFY</key>
        <string>$BOOTLITE_MAX_NOTIFY</string>"
  echo "→ Baking BOOTLITE_MAX_NOTIFY=$BOOTLITE_MAX_NOTIFY into the plist"
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
        <string>$WATCHDOG_PATH</string>
    </array>
    <!-- Hourly tick. LLM-free file/pid arithmetic; notifications are
         deduped per orphan stretch (renotify cadence + cap in the script).
         Disable: touch ~/.claude/dockwright/bootlite-stop
         Uninstall: launchctl bootout gui/\$(id -u)/$PLIST_LABEL && rm $PLIST_PATH -->
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>$BOOTLITE_DIR/launchd-out.log</string>
    <key>StandardErrorPath</key>
    <string>$BOOTLITE_DIR/launchd-err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <!-- tmux must be on PATH so the nudge path can drive the live
             instance under launchd (the script types into the worker pane via
             tmux send-keys). -->
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:$HOMEDIR/.local/bin</string>$ENV_EXTRA
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

Boot-lite watchdog installed.
  Tick (hourly, LLM-free): /usr/bin/python3 $WATCHDOG_PATH   → log: $BOOTLITE_DIR/check.log
  Manual check:  python3 $WATCHDOG_PATH --dry-run
  STOP (soft):   touch ~/.claude/dockwright/bootlite-stop
  Uninstall:     launchctl bootout "gui/\$(id -u)/$PLIST_LABEL" && rm $PLIST_PATH
EOF
