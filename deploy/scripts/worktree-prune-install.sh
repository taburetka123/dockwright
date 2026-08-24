#!/usr/bin/env bash
# Worktree-prune daily installer (see deploy/loops-registry.md, worktree-prune
# block — deployed to ~/.claude/dockwright/loops-registry.md; set [loops.status_overrides.worktree-prune]
# in dockwright.toml to live after this runs).
# Idempotent. Creates the worktree-prune state dir and hold list, and generates + loads
# the daily launchd tick. The tick is LLM-free Python (worktree_prune.py). It removes a
# worktree under ~/worktrees or ~/worktrees-personal only when ALL hold: not on the hold
# list, not `git worktree lock`ed, no interrupted git operation, commits contained in a
# durable ref (detached HEADs), the work is terminal (PR merged or closed, or already an
# ancestor of origin/main), the tree is clean, every gitignored entry is build output, and
# no live session owns the directory. Deleting the local BRANCH needs a stricter, separate
# proof: a PR merged at exactly that head SHA, or an ancestor of origin/main — never
# main/master. See deploy/loops-registry.md for the authoritative gate description.
#
# ⛔ FIRST DEPLOY OF A WIDENED CRITERION: touch the stop file BEFORE running
#    setup.sh. An already-installed plist points at ~/.claude/scripts/worktree_prune.py
#    with --apply, and setup.sh replaces that file — so setup.sh alone arms the new
#    criterion and the next scheduled tick applies it with no measurement taken.
#      mkdir -p ~/.claude/dockwright && touch ~/.claude/dockwright/worktree-prune-stop
#      test -f ~/.claude/dockwright/worktree-prune-stop   # verify before proceeding
#      ./setup.sh
#      WORKTREE_PRUNE_GH=<gh> WORKTREE_PRUNE_INSTALL_APPLY=0 ./worktree-prune-install.sh
#      rm ~/.claude/dockwright/worktree-prune-stop
#      launchctl kickstart -k "gui/$(id -u)/<label from the → Loaded: line below>"
#      # read last-scan.json — confirm `ts` is THIS run — then summary.gh_failed
#      # no file at all? a `stopped` row in check.log means a stop file survives;
#      # BOTH ~/.claude/dockwright/worktree-prune-stop and the legacy
#      # ~/.claude/worktree-prune-stop are honoured.
#      WORKTREE_PRUNE_GH=<gh> ./worktree-prune-install.sh     # then apply
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

# Baked launchd PATH. worktree_prune.py shells out to `git` and `gh`, so both
# must resolve under launchd's minimal env.
BAKED_PATH="/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:$HOMEDIR/.local/bin"
if [ -n "${EXTRA_PATH:-}" ]; then
  BAKED_PATH="$BAKED_PATH:$EXTRA_PATH"
fi

# The gh binary, named explicitly. If you use a gh wrapper (account selection,
# token injection), point WORKTREE_PRUNE_GH at it rather than fronting its
# directory on PATH: a PATH prefix would let that directory shadow EVERY binary
# this loop runs, and "is my wrapper early enough in PATH" is not a property any
# test can assert cheaply. argv[0] is.
#
# Getting this wrong is silent. A gh that resolves to the wrong account answers
# "Could not resolve to a Repository", the PR check falls through to the pure-git
# ancestor test, and every squash-merged branch then reads as unmerged — the loop
# reports a healthy scan and removes almost nothing. That exact misconfiguration
# ran for 62 ticks here. The `gh_failed` counter in the run summary is what makes
# it visible; check it after the first tick.
# A bare re-run is documented as idempotent, so it must NOT silently drop this
# binding: doing so returns the loop to the defect above with nothing reporting it.
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

# First install should NOT apply. Run one tick in dry-run, read last-scan.json
# and gh_failed, confirm the scan sees what you expect, THEN re-run with apply.
# There is no manual cleanup pass behind this loop; this is the only measurement
# taken before the first irreversible action.
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

# Capability probe: BEHAVIOURAL, never a grep. This plist widens what the loop
# deletes, and the hold list is the only way a human can protect a tree from it,
# so refuse to install over a script that predates it. A `grep keep.txt` would
# pass on a script whose keep-list CODE was deleted but whose docstring still
# mentions it.
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
