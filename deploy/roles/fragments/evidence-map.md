---
kind: fact
binds: [deliver]
domain: dockwright
---
## `worker_done` must carry a per-capability evidence map

Before calling `worker_done`, map **every capability you were dispatched to deliver** (each distinct ask, not "the task") to the evidence that proves it, labelled `fixed` / `tested-live` / `unit-tested (named test)` / `eyeballed-judgment` / `not-done`{{a_evidence_map_rule_ref}}.

- **`eyeballed-judgment` ≠ done** — never folded into "all addressed". A capability with no named evidence is `not-done`, green build notwithstanding.
- **Runtime-critical changes** (hooks, monitor, IPC, terminal automation): fixture/monkeypatch unit tests do NOT prove the live path. Name the one concrete live E2E to run, or state plainly that the live path is unverified.
- **An installer, MCP server, hook, slash command or terminal automation** is done only after you ran the installer and its primary flow once end to end. The report lists what ran live and what is only unit-covered.
