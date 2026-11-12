---
kind: fact
binds: [deliver]
domain: machine
---
## Background work: dispatch deliberately, drain before done

- **If a result gates your next action, run it foreground.**
- **Never background fast bounded commands** (`grep`/`find`/`ls`/`wc`).
- **Before `worker_done`** (or any terminal action), drain or `TaskStop` outstanding background tasks.
- On long background work, end the turn and let the harness notify — no sleeps, no ScheduleWakeup polls. One exception: a bounded poll (total sleep ≤300s per pass) when your terminal report depends on the result{{a_wait_mechanics_ref}}.
- **Before any `run_in_background:false` review/verification `Agent` call likely to run >~1-2 min**: if your task commits, WIP-commit your finished edits locally; then `artifact_put(phase="implement", status="partial")`.
