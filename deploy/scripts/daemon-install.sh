#!/usr/bin/env bash

set -euo pipefail

USAGE="usage: daemon-install.sh <dockwright-bin> <daemon-dir> <label> <tick|observe> <interval-sec|wake-path> <env-json-file>"
DOCKWRIGHT_BIN="${1:?$USAGE}"
DAEMON_DIR="${2:?$USAGE}"
PLIST_LABEL="${3:?$USAGE}"
MODE="${4:?$USAGE}"
MODE_ARG="${5:?$USAGE}"
ENV_FILE="${6:?$USAGE}"
HOMEDIR="${HOME:?}"
PLIST_PATH="$HOMEDIR/Library/LaunchAgents/$PLIST_LABEL.plist"

if [ ! -x "$DOCKWRIGHT_BIN" ]; then
  echo "ERROR: $DOCKWRIGHT_BIN is not executable — run setup.sh from the dockwright main clone first." >&2
  exit 1
fi
case "$MODE" in tick|observe) ;; *) echo "$USAGE" >&2; exit 2 ;; esac

xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

ENV_XML=""
while IFS=$'\t' read -r key value; do
  [ -n "$key" ] || continue
  ENV_XML="$ENV_XML
        <key>$(xml_escape "$key")</key>
        <string>$(xml_escape "$value")</string>"
done < <(python3 -c 'import json,sys
for k,v in sorted(json.load(open(sys.argv[1])).items()):
    print(f"{k}\t{v}")' "$ENV_FILE")

if [ "$MODE" = "tick" ]; then
  TRIGGER_XML="    <key>StartInterval</key>
    <integer>$MODE_ARG</integer>
    <key>RunAtLoad</key>
    <true/>"
  OUT_LOG="$DAEMON_DIR/launchd-out.log"; ERR_LOG="$DAEMON_DIR/launchd-err.log"
else
  TRIGGER_XML="    <key>WatchPaths</key>
    <array>
        <string>$(xml_escape "$MODE_ARG")</string>
    </array>
    <key>ThrottleInterval</key>
    <integer>1</integer>
    <key>RunAtLoad</key>
    <false/>"
  OUT_LOG="$DAEMON_DIR/launchd-wake-out.log"; ERR_LOG="$DAEMON_DIR/launchd-wake-err.log"
fi

mkdir -p "$DAEMON_DIR" "$HOMEDIR/Library/LaunchAgents"
cat > "$PLIST_PATH" <<EOF2
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$DOCKWRIGHT_BIN</string>
        <string>daemon</string>
        <string>$MODE</string>
    </array>
$TRIGGER_XML
    <key>StandardOutPath</key>
    <string>$OUT_LOG</string>
    <key>StandardErrorPath</key>
    <string>$ERR_LOG</string>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:$HOMEDIR/.local/bin</string>$ENV_XML
    </dict>
</dict>
</plist>
EOF2

launchctl bootout "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
echo "→ Loaded $PLIST_LABEL ($MODE $MODE_ARG): $DOCKWRIGHT_BIN daemon $MODE"
