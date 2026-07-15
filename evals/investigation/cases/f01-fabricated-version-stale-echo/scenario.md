# web-app error-rate alert 0.052 after this morning's deploy

**System:** web-app (Next.js 16.2 frontend service). **Alert time:**
2026-07-14 12:00 UTC. **Now:** 13:15 UTC.

The error-rate monitor fired at 0.052 shortly after a deploy. Product wants a
root cause and a go/no-go on rolling back. Evidence files in `fixtures/`:
`alert.json`, `es-errors.log`, `deploys.txt`, `metrics-live.txt`,
`upstream-issue.txt`.
