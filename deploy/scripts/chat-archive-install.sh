#!/usr/bin/env bash

set -euo pipefail

USAGE="usage: chat-archive-install.sh <dockwright-bin> <chat-dir> <label> <interval-sec> <triggers-dir> <env-json-file>"
DOCKWRIGHT_BIN="${1:?$USAGE}"
CHAT_DIR="${2:?$USAGE}"
PLIST_LABEL="${3:?$USAGE}"
INTERVAL="${4:?$USAGE}"
TRIGGERS_DIR="${5:?$USAGE}"
ENV_FILE="${6:?$USAGE}"
HOMEDIR="${HOME:?}"
PLIST_PATH="$HOMEDIR/Library/LaunchAgents/$PLIST_LABEL.plist"

if [ ! -x "$DOCKWRIGHT_BIN" ]; then
  echo "ERROR: $DOCKWRIGHT_BIN is not executable — run setup.sh from the dockwright main clone first." >&2
  exit 1
fi

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

mkdir -p "$CHAT_DIR" "$TRIGGERS_DIR" "$HOMEDIR/Library/LaunchAgents"
chmod 700 "$CHAT_DIR"
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
        <string>chat-archive</string>
        <string>pass</string>
    </array>
    <key>StartInterval</key>
    <integer>$INTERVAL</integer>
    <key>WatchPaths</key>
    <array>
        <string>$(xml_escape "$TRIGGERS_DIR")</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$CHAT_DIR/launchd-out.log</string>
    <key>StandardErrorPath</key>
    <string>$CHAT_DIR/launchd-err.log</string>
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
echo "→ Loaded $PLIST_LABEL (every ${INTERVAL}s and on $TRIGGERS_DIR): $DOCKWRIGHT_BIN chat-archive pass"
