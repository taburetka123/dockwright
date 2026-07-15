#!/usr/bin/env bash
# Gardener Phase-0 installer.
# Idempotent. Creates the gardener state dirs, enables the selffix-debug
# trigger.log denominator (PRD §6), generates + loads the hourly launchd gate.
#
# The gate itself is conservative by construction: hourly launchd ticks are
# LLM-free file arithmetic (gardener_gate.py); a run spawns only on the
# accumulation gate (K=8 new unreviewed findings), the weekly floor, or a
# manual --force — capped at 3 runs/week (PRD §5, §10).
#
# DISABLE (one line each — per-loop stop files, B3 convention; stopping one
# loop does NOT stop the other):
#   touch ~/.claude/dockwright/gardener-stop            # digest loop: gate refuses to spawn (incl. --force)
#   touch ~/.claude/dockwright/frontier-stop            # frontier loop: same contract
# UNINSTALL the schedulers (one line each — labels below are this operator's
# default, com.dockwright; the actual labels are dockwright.toml
# [loops].label_prefix + ".gardener-gate"/".gardener-frontier", see
# loop-label-prefix.sh):
#   launchctl bootout "gui/$(id -u)/com.dockwright.gardener-gate" && rm ~/Library/LaunchAgents/com.dockwright.gardener-gate.plist
#   launchctl bootout "gui/$(id -u)/com.dockwright.gardener-frontier" && rm ~/Library/LaunchAgents/com.dockwright.gardener-frontier.plist

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=loop-label-prefix.sh
source "$SCRIPT_DIR/loop-label-prefix.sh"

HOMEDIR="${HOME:?}"

# [modules] gardener toggle: refuse to install the loops when the Gardener is
# disabled (design-gate: gardener=false no-ops the whole subsystem, install
# included). Idempotent no-op — nothing is created, nothing loaded.
if ! dockwright_module_enabled gardener; then
  echo "→ Gardener module disabled ([modules] gardener=false in dockwright.toml) — skipping install." >&2
  echo "  Enable it: set [modules] gardener=true (or remove the key) and re-run." >&2
  exit 0
fi

GARDENER_DIR="$HOMEDIR/.claude/dockwright/gardener"
SCRIPTS_DIR="$HOMEDIR/.claude/scripts"
LOOP_LABEL_PREFIX="$(dockwright_loop_label_prefix)"
PLIST_LABEL="${LOOP_LABEL_PREFIX}.gardener-gate"
PLIST_PATH="$HOMEDIR/Library/LaunchAgents/$PLIST_LABEL.plist"
GATE_PATH="$SCRIPTS_DIR/gardener_gate.py"

echo "→ Creating $GARDENER_DIR/{digests,proposals,runs}"
mkdir -p "$GARDENER_DIR/digests" "$GARDENER_DIR/proposals" "$GARDENER_DIR/runs"

if [ ! -x "$GATE_PATH" ] && [ ! -f "$GATE_PATH" ]; then
  echo "ERROR: $GATE_PATH not deployed — run setup.sh first (it cp-deploys deploy/scripts/)." >&2
  exit 1
fi

echo "→ Enabling selffix debug logging (trigger.log denominator, PRD §6)"
mkdir -p "$HOMEDIR/.claude/dockwright/selffix"
touch "$HOMEDIR/.claude/dockwright/selffix/debug"

echo "→ Writing $PLIST_PATH (hourly gate tick)"
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
        <string>$GATE_PATH</string>
    </array>
    <!-- Hourly tick. The tick is LLM-free file arithmetic; the gate's own
         K-threshold / weekly-floor / 3-per-week cap make the actual run
         cadence conservative (PRD §5, §10).
         Disable: touch ~/.claude/dockwright/gardener-stop
         Uninstall: launchctl bootout gui/\$(id -u)/$PLIST_LABEL && rm $PLIST_PATH -->
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>$GARDENER_DIR/launchd-out.log</string>
    <key>StandardErrorPath</key>
    <string>$GARDENER_DIR/launchd-err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <!-- tmux must be on PATH so the run wrapper can drive the live
             instance; ~/.local/bin for claude. (MCP OAuth's \$USER caveat from
             the pr-review-poller plist doesn't apply — the gate is LLM-free
             and the visible session inherits the user's own GUI session.) -->
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:$HOMEDIR/.local/bin</string>
    </dict>
</dict>
</plist>
EOF

echo "→ (Re)loading launchd job $PLIST_LABEL"
launchctl bootout "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true
# || true: under set -e a bootstrap failure would abort before the explicit
# warn branch below ever runs (verifier finding on #58).
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" || true
sleep 1  # launchctl list can lag a just-bootstrapped job (observed false WARN)
if launchctl list "$PLIST_LABEL" >/dev/null 2>&1; then
  echo "→ Loaded: $PLIST_LABEL (hourly)"
else
  echo "WARN: $PLIST_LABEL not visible in launchctl list after bootstrap — check $PLIST_PATH" >&2
fi

# --- Frontier loop (separate registered loop — own gate, stop file, marker,
# budget; shares the artifact contract + review sitting + run mutex) ---------
FRONTIER_LABEL="${LOOP_LABEL_PREFIX}.gardener-frontier"
FRONTIER_PLIST="$HOMEDIR/Library/LaunchAgents/$FRONTIER_LABEL.plist"
FRONTIER_GATE="$SCRIPTS_DIR/frontier_gate.py"
FRONTIER_MARKER="$GARDENER_DIR/last-frontier-run"

if [ ! -f "$FRONTIER_GATE" ]; then
  echo "ERROR: $FRONTIER_GATE not deployed — run setup.sh first." >&2
  exit 1
fi

if [ ! -f "$FRONTIER_MARKER" ]; then
  # Arm the interval clock: run #0 is the manual v1 research
  # (2026-06-11). An absent marker
  # means NOT-armed in frontier_gate.py, so a fresh deploy can never fire a
  # surprise token-heavy web sweep — arming is this explicit install step.
  echo "→ Arming frontier marker (first automated sweep ~7d from now)"
  touch "$FRONTIER_MARKER"
fi

echo "→ Writing $FRONTIER_PLIST (daily gate tick; the 7d interval lives in the gate)"
cat > "$FRONTIER_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$FRONTIER_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$FRONTIER_GATE</string>
    </array>
    <!-- Daily LLM-free tick; the gate's marker-interval (7d default),
         48h failure-retry gap, frontier-stop file, and shared run mutex
         decide whether anything actually runs.
         Disable: touch ~/.claude/dockwright/frontier-stop
         Uninstall: launchctl bootout gui/\$(id -u)/$FRONTIER_LABEL && rm $FRONTIER_PLIST -->
    <key>StartInterval</key>
    <integer>86400</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>$GARDENER_DIR/frontier-launchd-out.log</string>
    <key>StandardErrorPath</key>
    <string>$GARDENER_DIR/frontier-launchd-err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:$HOMEDIR/.local/bin</string>
    </dict>
</dict>
</plist>
EOF

echo "→ (Re)loading launchd job $FRONTIER_LABEL"
launchctl bootout "gui/$(id -u)/$FRONTIER_LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$FRONTIER_PLIST" || true
sleep 1
if launchctl list "$FRONTIER_LABEL" >/dev/null 2>&1; then
  echo "→ Loaded: $FRONTIER_LABEL (daily tick)"
else
  echo "WARN: $FRONTIER_LABEL not visible in launchctl list after bootstrap — check $FRONTIER_PLIST" >&2
fi

cat <<EOF

Gardener loops installed.
  Digest gate (hourly, LLM-free):  /usr/bin/python3 $GATE_PATH   → log: $GARDENER_DIR/gate.log
    Manual: python3 $GATE_PATH --force · Dry-run: python3 $GATE_PATH --dry-run
    STOP:   touch ~/.claude/dockwright/gardener-stop
    Uninstall: launchctl bootout "gui/\$(id -u)/$PLIST_LABEL" && rm $PLIST_PATH
  Frontier gate (daily tick, 7d interval): /usr/bin/python3 $FRONTIER_GATE   → log: $GARDENER_DIR/frontier-gate.log
    Manual: python3 $FRONTIER_GATE --force · Dry-run: python3 $FRONTIER_GATE --dry-run
    STOP:   touch ~/.claude/dockwright/frontier-stop
    Uninstall: launchctl bootout "gui/\$(id -u)/$FRONTIER_LABEL" && rm $FRONTIER_PLIST
EOF
