---
kind: fact
binds: [investigate]
domain: investigation
---
## Investigation / read-only tasks: no OUTWARD actions without an explicit instruction

On an investigation, scout, verification, or any read-only dispatch, READ freely but take NO **outward, hard-to-reverse action** — a {{tracker_eg}} transition/comment, a {{comms_examples}} send, a PR or PR-comment create, any non-GET external API call — unless the manager's instruction *names that action*. "Investigate X" / "find the root cause" / "verify the diff" authorizes reading and reporting only. DB writes are off-limits. If acting looks necessary, `ask_manager` with the proposed action.

## Investigation-class workers: end with a structured findings block

On an investigation, scout, verification, or triage dispatch, end the `worker_done` summary with the verdict line FIRST (principle 3), then this machine block:

```
ROOT_CAUSE: <one line, or "none">
ROOT_CAUSE_CATEGORY: code_defect | data_state_gap | race_or_replay | upstream_invariant_broken | deployment_regression | external_dependency | resource_exhaustion | database_contention | configuration_error | security_abuse | noise_no_incident | recovered | insufficient_evidence
VALIDATED_CLAIMS: <fact [tool/source]> — one per line, only facts backed by a tool output read this session
NON_VALIDATED_CLAIMS: <hypothesis + why unproven> — one per line
CAUSAL_CHAIN: <A → B → C, or "n/a">
RECOMMENDED_ACTIONS: <ranked, or "none">
```

- Exactly ONE category. The abstention values (`noise_no_incident`, `recovered`, `insufficient_evidence`) are first-class outcomes.
- The verdict line derives from VALIDATED_CLAIMS only; hypotheses stay in NON_VALIDATED_CLAIMS.
- Fail-soft: a missing or malformed block never fails the task or the relay.
- Optional: a typed actions file for a deterministic executor → one line `PROPOSED_ACTIONS_FILE: <path>`. Never inline the actions.
