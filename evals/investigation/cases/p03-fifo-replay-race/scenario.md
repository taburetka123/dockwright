# Intermittent NOT_FOUND in chat-creation handler

**System:** acme-chat service, SQS FIFO consumer. **Window:** last 30 days.

A ticket reports two occurrences of `ChatNotFoundException` in
`ChatEventHandler` and proposes adding a null check before the lookup.
Decide whether that fix is right; find the actual root cause.

Evidence files in `fixtures/`: `ticket-excerpt.txt`, `handler.kt`,
`logs-30d.txt` (aggregated 30-day analysis), `queue-config.txt`.
