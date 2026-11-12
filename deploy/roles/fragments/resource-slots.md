---
kind: fact
binds: [build, run-tests]
domain: dockwright
---
<!-- overlay: auto-sync-collisions -->
## Resource slots (concurrency-heavy commands)

**MANDATORY before any `mvn test`, `gradle test`, big `npm install`, or `docker build`** (cap default 5 for mvn), whether you run the command directly OR dispatch a subagent (`Agent(...)`) whose task includes it:

  slot = acquire_worker_slot(claude_sid=YOUR_SID, category="mvn")  # or "gradle", "npm", "docker-build"
  # ... run mvn test (or dispatch the subagent that will run it) ...
  release_worker_slot(slot_id=slot["slot_id"])

- **Always release**, even if mvn fails or the subagent exits non-zero — a try/finally pair.
- **Subagents cannot hold slots.** Acquire BEFORE the `Agent(...)` call, release AFTER it returns.
- **Parallel subagent batch:** ONE slot before the batch, release once after the last completes.
- Light commands (file edits, single-file reads, `git status`, design work) need no slot.
- Acquire timeout (default 1800s) → the fleet is wedged: `ask_manager` and stop.
