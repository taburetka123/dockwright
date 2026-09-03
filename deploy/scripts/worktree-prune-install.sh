#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=loop-label-prefix.sh
source "$SCRIPT_DIR/loop-label-prefix.sh"

HOMEDIR="${HOME:?}"
WT_DIR="$HOMEDIR/.claude/dockwright/worktree-prune"
SCRIPTS_DIR="$HOMEDIR/.claude/scripts"
PLIST_LABEL="$(dockwright_loop_label_prefix).worktree-prune"
PLIST_PATH="$HOMEDIR/Library/LaunchAgents/$PLIST_LABEL.plist"
SCRIPT="$SCRIPTS_DIR/worktree_prune.py"

BAKED_PATH="/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:$HOMEDIR/.local/bin"
if [ -n "${EXTRA_PATH:-}" ]; then
  BAKED_PATH="$BAKED_PATH:$EXTRA_PATH"
fi

if [ -z "${WORKTREE_PRUNE_GH:-}" ] && [ -f "$PLIST_PATH" ]; then
  PRESERVED="$(sed -n '/<key>WORKTREE_PRUNE_GH<\/key>/{n;s:.*<string>\(.*\)</string>.*:\1:p;}' "$PLIST_PATH" | head -1)"
  if [ -n "$PRESERVED" ]; then
    WORKTREE_PRUNE_GH="$PRESERVED"
    echo "→ Preserving WORKTREE_PRUNE_GH from the existing plist: $PRESERVED"
  fi
fi
GH_BIN_LINE=""
if [ -n "${WORKTREE_PRUNE_GH:-}" ]; then
  GH_BIN_LINE="        <key>WORKTREE_PRUNE_GH</key>
        <string>$WORKTREE_PRUNE_GH</string>"
else
  echo "WARN: WORKTREE_PRUNE_GH is unset — the tick will resolve \`gh\` by PATH order," >&2
  echo "      which under the baked PATH is: $(PATH="$BAKED_PATH" command -v gh 2>/dev/null || echo '<not found>')" >&2
  echo "      If that is not the gh you want, re-run with WORKTREE_PRUNE_GH=<path>." >&2
fi

APPLY_ARG="        <string>--apply</string>"
if [ "${WORKTREE_PRUNE_INSTALL_APPLY:-1}" = "0" ]; then
  APPLY_ARG=""
  echo "→ Installing in DRY-RUN mode (--apply omitted)"
fi

echo "→ Creating $WT_DIR"
mkdir -p "$WT_DIR"

if [ ! -f "$SCRIPT" ]; then
  echo "ERROR: $SCRIPT not deployed — run setup.sh first (it cp-deploys deploy/scripts/)." >&2
  exit 1
fi

if ! /usr/bin/python3 "$SCRIPT" --capabilities 2>/dev/null | grep -qx "keeplist"; then
  echo "ERROR: deployed $SCRIPT does not report the 'keeplist' capability." >&2
  echo "  Installing this plist over it would widen deletion with no way to hold a tree." >&2
  echo "  Run ./setup.sh from the main clone first, then re-run this installer." >&2
  exit 1
fi

echo "→ Ensuring hold list $WT_DIR/keep.txt"
if [ ! -f "$WT_DIR/keep.txt" ]; then
  cat > "$WT_DIR/keep.txt" <<'KEEPEOF'
# worktree-prune hold list — one path or glob per line; '#' comments.
#
# A tree matching any line is never removed. Matching is case-insensitive and a
# directory holds everything beneath it. Both ~ and $VARS are expanded.
#
# This file must EXIST: if it is missing the loop stops instead of running with
# nothing held.
#
# ⛔ EVERY LINE MUST MATCH SOMETHING THAT EXISTS RIGHT NOW, or the loop stops.
#    A literal path that is not on disk stops it. A glob that matches NO existing
#    path stops it — an existing parent directory is not enough. Silently
#    protecting nothing is the failure this file exists to prevent, so a hold you
#    cannot see the effect of is treated as a mistake rather than ignored.
#
#    Consequence worth knowing: you cannot pre-register a hold for a tree that
#    does not exist yet. Add the line when the tree appears, and delete lines for
#    trees you have removed by hand.
#
# Examples (adjust to paths that exist on your machine):
#   /Users/you/worktrees/KEEP-ME
#   /Users/you/worktrees/TKT-1234
KEEPEOF
else
  echo "  (exists, left as-is)"
fi

echo "→ Writing $PLIST_PATH (daily 10:00 tick)"
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
$APPLY_ARG
    </array>
    <!-- Daily tick at 10:00. LLM-free Python; destructive (git worktree remove -f
         + local branch -D). Script is dry-run by default; the apply flag is required to mutate.
         Disable: touch ~/.claude/dockwright/worktree-prune-stop
         Uninstall: launchctl bootout gui/\$(id -u)/$PLIST_LABEL && rm $PLIST_PATH -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>10</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>$WT_DIR/launchd-out.log</string>
    <key>StandardErrorPath</key>
    <string>$WT_DIR/launchd-err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <!-- git and gh must both resolve under launchd's minimal env. Prefer
             WORKTREE_PRUNE_GH over PATH order for a gh wrapper. -->
        <key>PATH</key>
        <string>$BAKED_PATH</string>
$GH_BIN_LINE
    </dict>
</dict>
</plist>
EOF

echo "→ (Re)loading launchd job $PLIST_LABEL"
launchctl bootout "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
if launchctl list "$PLIST_LABEL" >/dev/null 2>&1; then
  echo "→ Loaded: $PLIST_LABEL (daily 10:00)"
else
  echo "WARN: $PLIST_LABEL not visible in launchctl list after bootstrap — check $PLIST_PATH" >&2
fi

cat <<EOF

Worktree-prune installed.
  Tick (daily 10:00, LLM-free): /usr/bin/python3 $SCRIPT --apply
  Logs: $WT_DIR/launchd-out.log, $WT_DIR/launchd-err.log
  Ledger: $WT_DIR/ledger.jsonl
  Dry-run preview: python3 $SCRIPT
  JSON preview:    python3 $SCRIPT --json
  STOP (soft):     touch ~/.claude/dockwright/worktree-prune-stop
  Uninstall:       launchctl bootout "gui/\$(id -u)/$PLIST_LABEL" && rm $PLIST_PATH

NEXT STEP: set status = "live" under [loops.status_overrides.worktree-prune]
  in dockwright.toml.
EOF
