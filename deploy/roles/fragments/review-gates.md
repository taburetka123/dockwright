---
kind: guard
binds: [review-dispatch]
domain: review
---
## `<SUBAGENT-STOP>` waives skill *discovery*, not engineering discipline

`<SUBAGENT-STOP>`{{subagent_stop_ref}} waives the skill-discovery intro and nothing else. The always-on engineering rules ({{evidence_rule_ref}}, close-the-loop) still bind, and so does any discipline skill whose trigger fires.

"Spawned worker / headless / small change" never drops a reviewer pass, collapses a two-stage review into one agent, or ships a design never adversarially reviewed. Right-size the planning ceremony, never the gates.

**Classify before you dispatch**; the reviewer is always yours, the manager owns no review lane. ⛔ A behavioral surface (`deploy/**`, `src/dockwright/**`, `tests/**`, `evals/**`, `scripts/**`, `publish/**`, `.github/**`, `setup.sh`, `pyproject.toml`, `uv.lock`, a migration, a spawn/kill/hook script), other code or config, or >100 LOC is **Tier 2**; unsure ⇒ Tier 2. Prose ≤100 LOC is Tier 1: read it and post a PR comment. The loop, its recommended round count and the verdict record: {{tier2_flow_ref}}.

- Tell the reviewer it is READ-ONLY in the brief. YOU are the guard: baseline the tree, have the reviewer mutate only by absolute path OUTSIDE your checkout, and confirm `git status --porcelain` is clean after every round.
- Publish each verdict in the reviewer's own words: no record ⇒ the gate did not run.
- Escalate any Critical or Important you mean to downgrade, defer or accept. A Minor outside the full-depth blast radius ({{drift_guard_rule_ref}} § Blast radius) is yours to close, disclosed in your report.
- Never merge a PR you opened; merge another's PR only by your node's merge step, after its review loops closed.

{{subagent_authorization}}
