#!/usr/bin/env bats
# Tests for dlq-cookie-refresh.sh — the LLM-free tick. The Playwright grab is stubbed
# via DLQ_GRAB_CMD so all decision logic is exercised without a browser/VPN.

setup() {
  SCRIPT="$BATS_TEST_DIRNAME/../deploy/scripts/dlq-cookie-refresh.sh"
  TMP="$BATS_TEST_TMPDIR"
  export DLQ_STATE_DIR="$TMP/state"
  export PSP_DLQ_COOKIE_FILE="$TMP/dlq-cookie"
  export DLQ_META_FILE="$TMP/dlq-cookie.meta.json"
  export DLQ_STOP_FILE="$TMP/stop"
  export DLQ_NOTIFY_CMD="printf '%s\n' NOTIFY >> $TMP/notify.log"   # stub osascript
  export DLQ_PROBE_CMD=""        # default: no probe (overridden per-test)
  export DLQ_GRAB_CMD=""         # default: no grab (overridden per-test)
  export DLQ_REFRESH_THRESHOLD_SECS=172800
  export DLQ_REFRESH_COOLDOWN_SECS=0
  export DLQ_REFRESH_FAIL_NOTIFY_AFTER=3
  mkdir -p "$DLQ_STATE_DIR"
  NOW=$(date +%s)
}

seed_cookie() { printf '%s' 'AWSELBAuthSessionCookie-0=OLDVALUE' > "$PSP_DLQ_COOKIE_FILE"; }
seed_meta()   { printf '{"expires_at":%s,"grabbed_at":%s,"value_sha256":"x"}' "$1" "$NOW" > "$DLQ_META_FILE"; }
# A grab stub that prints a JSON result on stdout (mimics dlq-cookie-grab.cjs).
grab_ok()   { export DLQ_GRAB_CMD="printf '%s' '{\"ok\":true,\"reason\":\"\",\"cookie\":\"AWSELBAuthSessionCookie-0=NEWVALUE\",\"expires_at\":$((NOW+604800)),\"shards\":1}'"; }
grab_fail() { export DLQ_GRAB_CMD="printf '%s' '{\"ok\":false,\"reason\":\"okta_login_required\",\"cookie\":\"\",\"expires_at\":0,\"shards\":0}'"; }
grab_empty(){ export DLQ_GRAB_CMD="printf '%s' '{\"ok\":true,\"reason\":\"\",\"cookie\":\"\",\"expires_at\":0,\"shards\":0}'"; }

@test "stop file short-circuits" {
  touch "$DLQ_STOP_FILE"; seed_cookie
  run bash "$SCRIPT" --scheduled
  [ "$status" -eq 0 ]
  [[ "$output" == *"stopped"* ]]
}

@test "believed-fresh with inconclusive probe is a no-op (cookie untouched)" {
  seed_cookie; seed_meta $((NOW+600000))    # ~7d out
  export DLQ_PROBE_CMD="exit 2"             # hermetic: never hit the real ALB
  run bash "$SCRIPT" --scheduled
  [ "$status" -eq 0 ]
  [[ "$output" == *"noop"* ]]
  [ "$(cat "$PSP_DLQ_COOKIE_FILE")" = "AWSELBAuthSessionCookie-0=OLDVALUE" ]
}

@test "missing meta forces a refresh" {
  seed_cookie; grab_ok
  export DLQ_VERIFY_CMD="true"   # stub verify-GET success
  run bash "$SCRIPT" --scheduled
  [ "$status" -eq 0 ]
  [ "$(cat "$PSP_DLQ_COOKIE_FILE")" = "AWSELBAuthSessionCookie-0=NEWVALUE" ]
}

@test "near-expiry forces a refresh and writes new cookie atomically (mode 600, no trailing NL)" {
  seed_cookie; seed_meta $((NOW+3600)); grab_ok
  export DLQ_VERIFY_CMD="true"
  run bash "$SCRIPT" --scheduled
  [ "$status" -eq 0 ]
  [ "$(cat "$PSP_DLQ_COOKIE_FILE")" = "AWSELBAuthSessionCookie-0=NEWVALUE" ]
  [ "$(stat -f '%Lp' "$PSP_DLQ_COOKIE_FILE")" = "600" ]
  [ "$(wc -l < "$PSP_DLQ_COOKIE_FILE")" -eq 0 ]   # no trailing newline
}

@test "failed grab NEVER overwrites the cookie (I1)" {
  seed_cookie; seed_meta $((NOW+3600)); grab_fail
  run bash "$SCRIPT" --scheduled
  [ "$status" -ne 0 ]
  [ "$(cat "$PSP_DLQ_COOKIE_FILE")" = "AWSELBAuthSessionCookie-0=OLDVALUE" ]
}

@test "empty cookie from grab NEVER overwrites (I1)" {
  seed_cookie; seed_meta $((NOW+3600)); grab_empty
  run bash "$SCRIPT" --scheduled
  [ "$status" -ne 0 ]
  [ "$(cat "$PSP_DLQ_COOKIE_FILE")" = "AWSELBAuthSessionCookie-0=OLDVALUE" ]
}

@test "verify-GET failure rejects the new cookie (I1)" {
  seed_cookie; seed_meta $((NOW+3600)); grab_ok
  export DLQ_VERIFY_CMD="false"   # new cookie fails the authed GET
  run bash "$SCRIPT" --scheduled
  [ "$status" -ne 0 ]
  [ "$(cat "$PSP_DLQ_COOKIE_FILE")" = "AWSELBAuthSessionCookie-0=OLDVALUE" ]
}

@test "--once forces a refresh even when fresh" {
  seed_cookie; seed_meta $((NOW+600000)); grab_ok
  export DLQ_VERIFY_CMD="true"
  run bash "$SCRIPT" --once
  [ "$status" -eq 0 ]
  [ "$(cat "$PSP_DLQ_COOKIE_FILE")" = "AWSELBAuthSessionCookie-0=NEWVALUE" ]
}

@test "probe 302->okta on a fresh cookie triggers early-expiry refresh" {
  seed_cookie; seed_meta $((NOW+600000)); grab_ok
  export DLQ_VERIFY_CMD="true"
  export DLQ_PROBE_CMD="exit 7"   # convention: exit 7 = expired
  run bash "$SCRIPT" --scheduled
  [ "$status" -eq 0 ]
  [[ "$output" == *"early"* ]]
  [ "$(cat "$PSP_DLQ_COOKIE_FILE")" = "AWSELBAuthSessionCookie-0=NEWVALUE" ]
}

@test "probe conn-fail on a fresh cookie is a silent no-op (no notify)" {
  seed_cookie; seed_meta $((NOW+600000))
  export DLQ_PROBE_CMD="exit 2"   # convention: exit 2 = inconclusive
  run bash "$SCRIPT" --scheduled
  [ "$status" -eq 0 ]
  [[ "$output" == *"noop"* ]]
  [ ! -f "$TMP/notify.log" ]
  [ "$(cat "$PSP_DLQ_COOKIE_FILE")" = "AWSELBAuthSessionCookie-0=OLDVALUE" ]
}

@test "healthy->failing edge notifies once; second failure below N does not re-notify" {
  seed_cookie; seed_meta $((NOW+3600)); grab_fail
  run bash "$SCRIPT" --scheduled        # failure #1 -> edge notify
  run bash "$SCRIPT" --scheduled        # failure #2 -> below N(3), still failing, no new notify
  [ "$(grep -c NOTIFY "$TMP/notify.log")" -eq 1 ]
}

@test "recovery after failures notifies" {
  seed_cookie; seed_meta $((NOW+3600)); grab_fail
  run bash "$SCRIPT" --scheduled        # fail -> notify (edge)
  grab_ok; export DLQ_VERIFY_CMD="true"
  run bash "$SCRIPT" --scheduled        # success -> recovery notify
  [ "$(grep -c NOTIFY "$TMP/notify.log")" -eq 2 ]
  [ "$(cat "$PSP_DLQ_COOKIE_FILE")" = "AWSELBAuthSessionCookie-0=NEWVALUE" ]
}

@test "third consecutive failure re-notifies (failures>=N)" {
  seed_cookie; seed_meta $((NOW+3600)); grab_fail
  run bash "$SCRIPT" --scheduled   # #1 healthy->failing edge -> notify
  run bash "$SCRIPT" --scheduled   # #2 failures=2 < N(3) -> no notify
  run bash "$SCRIPT" --scheduled   # #3 failures=3 >= N(3) -> notify
  [ "$(grep -c NOTIFY "$TMP/notify.log")" -eq 2 ]
}

@test "grab with expires_at 0 (session cookie) is clamped to a future expiry, not 0" {
  seed_cookie; seed_meta $((NOW+3600))
  export DLQ_GRAB_CMD="printf '%s' '{\"ok\":true,\"reason\":\"\",\"cookie\":\"AWSELBAuthSessionCookie-0=NEWVALUE\",\"expires_at\":0,\"shards\":1}'"
  export DLQ_VERIFY_CMD="true"
  run bash "$SCRIPT" --scheduled
  [ "$status" -eq 0 ]
  local written; written="$(jq -r '.expires_at' "$DLQ_META_FILE")"
  [ "$written" -gt "$NOW" ]   # clamped to ~now+7d, NOT 0 (would force daily re-grab)
}

@test "status prints expiry and health" {
  seed_cookie; seed_meta $((NOW+600000))
  run bash "$SCRIPT" --status
  [ "$status" -eq 0 ]
  [[ "$output" == *"expires"* ]]
  [[ "$output" == *"health"* ]]
}
