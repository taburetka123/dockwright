#!/usr/bin/env bash
# scripts/integration-test.sh — manual e2e smoke test.
#
# Run this from a fresh iTerm2 window AFTER `./setup.sh`.
# It walks you through verifying spawn → ask → answer → status → kill → re-attach.

set -e

cat <<'EOF'
==== dockwright integration test ====

This is a manual checklist. Each step opens a new tab or types into an
existing one. Verify the expected behavior at each step.

Prereqs:
- ./setup.sh has been run
- iTerm2 Python API is enabled
- A fresh ~/.claude/dockwright (stale records are pruned automatically)

------------------------------------------------------------
Step 1: Open a fresh Claude Code session in this iTerm2 window.
        Type: /manager
Expected:
  - "Manager active. Re-attached to 0 workers. 0 questions pending."

Press Enter when you've done this and seen the expected output:
EOF
read -r

cat <<'EOF'
------------------------------------------------------------
Step 2: Tell the manager:
        "Spawn a worker named 'test-1' in /tmp with initial prompt 'echo hi and then ask me what to do next.'"
Expected:
  - A new iTerm2 tab opens, titled "test-1".
  - claude starts in /tmp with that prompt.
  - The worker echoes "hi" and then calls ask_manager.
  - Manager (your current tab) gets a notification: "test-1 asks: <something>"

Press Enter when you see the manager relay the question:
EOF
read -r

cat <<'EOF'
------------------------------------------------------------
Step 3: Answer the manager's relayed question with: "exit cleanly".
Expected:
  - Worker tab: ask_manager returns "exit cleanly".
  - Worker continues, ends turn idle.

Press Enter when the worker has resumed:
EOF
read -r

cat <<'EOF'
------------------------------------------------------------
Step 4: Tell the manager: "status".
Expected:
  - Table showing test-1 with state, last_turn_at, last_summary.

Press Enter when you see the table:
EOF
read -r

cat <<'EOF'
------------------------------------------------------------
Step 5: Tell the manager: "kill test-1".
Expected:
  - Worker tab's claude process exits.
  - active/<sid>.json removed (SessionEnd hook).

Press Enter when test-1 is gone:
EOF
read -r

cat <<'EOF'
------------------------------------------------------------
Step 6: Close the manager tab (Cmd+W). Confirm if asked.
        Open a NEW iTerm2 tab, run: claude
        Type: /manager
Expected:
  - "Manager active. Re-attached to 0 workers. 0 questions pending."
  - the active record for this manager points at the new session.

Press Enter when re-attach works:
EOF
read -r

echo ""
echo "✓ Integration test complete. Spawn → ask → answer → status → kill → re-attach all work."
