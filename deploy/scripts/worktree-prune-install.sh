#!/usr/bin/env bash
# Worktree-prune daily installer (see deploy/loops-registry.md, worktree-prune
# block — deployed to ~/.claude/dockwright/loops-registry.md; set [loops.status_overrides.worktree-prune]
# in dockwright.toml to live after this runs).
# Idempotent. Creates the worktree-prune state dir and generates + loads the daily
# launchd tick. The tick is LLM-free Python (worktree_prune.py); it removes worktrees under
# ~/worktrees and ~/worktrees-personal whose branch is merged into origin/main, the working
# tree is clean, and no live orchestrator session owns the directory.
#
# DISABLE (one line):
#   touch ~/.claude/dockwright/worktree-prune-stop          # soft stop: tick exits before scanning
# UNINSTALL the scheduler (one line — label below is this operator's default,
# com.dockwright; the actual label is dockwright.toml [loops].label_prefix +
# ".worktree-prune", see loop-label-prefix.sh):
#   launchctl bootout "gui/$(id -u)/com.dockwright.worktree-prune" && rm ~/Library/LaunchAgents/com.dockwright.worktree-prune.plist
# DRY-RUN preview (no mutations):
#   python3 ~/.claude/scripts/worktree_prune.py          # default is dry-run
#   python3 ~/.claude/scripts/worktree_prune.py --json   # machine-readable dry-run
#
# gh keychain caveat: under a non-GUI launchd context, `gh` may fail to unlock its
# keychain token. If so, Gate A (PR-MERGED check via gh) degrades to the pure-git
# ancestor fallback. For squash-merged branches the squash commit is not a git ancestor
# of the worktree HEAD, so those worktrees are SKIPPED (under-prune — safe, never
# over-prune). Run the installer from a GUI session to ensure keychain access is
# available to gh at tick time.
#
# After running this installer, set `status = "live"` under
# [loops.status_overrides.worktree-prune] in dockwright.toml.

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

# Baked launchd PATH. worktree_prune.py shells out to `git` and `gh` (Gate A's
# `gh pr view` PR-state check), so both must resolve under launchd's minimal env.
# The generic dirs below carry a Homebrew/system gh; set EXTRA_PATH before
# running this installer to append your own (e.g. a personal gh wrapper's dir).
BAKED_PATH="/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:$HOMEDIR/.local/bin"
if [ -n "${EXTRA_PATH:-}" ]; then
  BAKED_PATH="$BAKED_PATH:$EXTRA_PATH"
fi

echo "→ Creating $WT_DIR"
mkdir -p "$WT_DIR"

if [ ! -f "$SCRIPT" ]; then
  echo "ERROR: $SCRIPT not deployed — run setup.sh first (it cp-deploys deploy/scripts/)." >&2
  exit 1
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
        <string>--apply</string>
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
        <!-- git and gh must both be on PATH so Gate A's PR-state check
             (gh pr view) resolves under launchd's minimal env. Append your own
             dir via EXTRA_PATH when running the installer. -->
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
