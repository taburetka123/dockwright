# acme-tasks latency spike + errors since 14:00 UTC

**System:** acme-tasks service. **Window:** 2026-07-12 13:50–14:30 UTC.

Error rate jumped ~40× at around 14:00 UTC. A teammate looked at one trace,
saw a 67 ms `HikariPool.getConnection` span, and suspects DB connection-pool
exhaustion. Confirm or refute, find the root cause, recommend a fix.

Evidence files in `fixtures/`: `traces.txt`, `pool-metrics.txt`,
`deploy-log.txt`, `error-log.txt`.
