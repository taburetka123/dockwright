# Vendor markets disappeared from the back-office

**System:** acme-vendor service + back-office UI. **Window:** since 2026-07-10 release.

Ops report: every vendor's "Markets" panel is empty. The `/vendors/{id}/markets`
endpoint returns `[]` for all vendors. The release included TKT-8501 ("vendor
market mapping"), and the on-call suspects the new mapper commit. The mapper
matches the TKT-8488 design doc line-for-line; unit tests are green.

Evidence files in `fixtures/`: `git-log.txt` (release commits), `mapper.kt`
(the suspect mapper), `db-counts.txt` (staging DB checks run by on-call),
`api-response.json` (a sample response). Find the root cause and recommend a fix.
