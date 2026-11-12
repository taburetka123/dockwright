---
kind: fact
binds: [teammate]
domain: dockwright
---
## Spawned as a named teammate (Agent-tool peer, not a tmux worker)

Spawned as a named `Agent(..., name=...)` peer under a coordinating session:

- **Retrieve another peer's output by messaging it directly**: `SendMessage(to=<peer name>, ...)`. Never spawn a fresh `Agent` to fetch a result. A peer notification with no `summary` means nothing new yet.
- **`SendMessage(to=<dispatcher>, ...)` is the mandatory LAST action of any turn that completes a distinct deliverable** — the `worker_done` discipline for this shape. Plain turn-ending text is not delivery.
