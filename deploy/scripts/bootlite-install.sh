#!/usr/bin/env bash

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
