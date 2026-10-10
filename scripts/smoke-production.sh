# Production connectivity smoke — SupportDesk.
# Read-only by default: frontend, backend health, CORS preflight, auth gate.
# Optional business flow only when SD_SMOKE_EMAIL + SD_SMOKE_PASSWORD are
# exported (dedicated demo account; creates SMOKE- prefixed tickets).
# Usage: ./scripts/smoke-production.sh [pages_url] [api_url]
set -u
PAGES_URL="${1:-https://supportdesk-cta.pages.dev}"
API_URL="${2:-https://supportdesk-api-kh02.onrender.com}"
ORIGIN="${PAGES_URL%/}"
PASS=0; FAIL=0

chk() { # name, ok(0=pass), detail
  if [ "$2" -eq 0 ]; then echo "PASS | $1 ${3:-}"; PASS=$((PASS+1))
  else echo "FAIL | $1 ${3:-}"; FAIL=$((FAIL+1)); fi
}

echo "Pages: $ORIGIN"
echo "API  : $API_URL"
echo

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$ORIGIN/" || echo 000)
[ "$code" = "200" ]; chk "frontend reachable (Pages)" $? "($code)"
title=$(curl -s --max-time 30 "$ORIGIN/" | grep -o '<title>[^<]*' | head -1 || true)
case "$title" in
  *SupportDesk*) chk "frontend identity" 0 "($title)" ;;
  *) chk "frontend identity" 1 "(${title:-missing title})" ;;
esac
if [ -n "${SD_SMOKE_SOURCE_SHA:-}" ]; then
  # Cloudflare's edge can serve a stale (or not-yet-visible) release.json
  # right after a deploy — CD run 38031263651 failed the revision check
  # while the file was already live. Retry with cache revalidation.
  release_sha=""
  for attempt in 1 2 3 4 5 6; do
    release_sha=$(curl -s --max-time 30 -H 'Cache-Control: no-cache' -H 'Pragma: no-cache' "$ORIGIN/release.json" \
      | python3 -c 'import json,sys; print(json.load(sys.stdin).get("source_sha", ""))' 2>/dev/null || true)
    [ "$release_sha" = "$SD_SMOKE_SOURCE_SHA" ] && break
    [ "$attempt" -lt 6 ] && sleep 10
  done
  [ "$release_sha" = "$SD_SMOKE_SOURCE_SHA" ]; chk "frontend revision" $? "(${release_sha:-missing})"
fi

body=""
for attempt in 1 2 3; do
  body=$(curl -s --max-time 60 "$API_URL/api/health" || true)
  if echo "$body" | grep -q '"status"'; then break; fi
  if [ "$attempt" -lt 3 ]; then sleep 5; fi
done
echo "$body" | grep -q '"status"'; chk "backend /api/health" $? "(${body:-no body})"

acao=$(curl -s -D - -o /dev/null --max-time 30 -X OPTIONS "$API_URL/api/auth/login" \
  -H "Origin: $ORIGIN" -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: content-type" \
  | tr -d '\r' | grep -i '^access-control-allow-origin:' | head -1 || true)
case "$acao" in
  *"$ORIGIN"*) chk "CORS preflight allows Pages origin" 0 "($acao)" ;;
  *) chk "CORS preflight allows Pages origin" 1 "(${acao:-missing ACAO — set CORS_ORIGINS on the kh02 Render service})" ;;
esac

legacy_acao=$(curl -s -D - -o /dev/null --max-time 30 -X OPTIONS "$API_URL/api/auth/login" \
  -H "Origin: https://supportdesk-aht.pages.dev" -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: content-type" \
  | tr -d '\r' | grep -i '^access-control-allow-origin:' | head -1 || true)
[ -z "$legacy_acao" ]; chk "CORS rejects legacy Pages origin" $? "(${legacy_acao:-no ACAO})"

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$API_URL/openapi.json" || echo 000)
[ "$code" = "200" ]; chk "openapi.json" $? "($code)"

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$API_URL/api/tickets" || echo 000)
case "$code" in
  401|403) chk "auth gate (GET /api/tickets -> 401/403)" 0 "($code)" ;;
  *) chk "auth gate (GET /api/tickets -> 401/403)" 1 "($code)" ;;
esac

if [ -n "${SD_SMOKE_EMAIL:-}" ] && [ -n "${SD_SMOKE_PASSWORD:-}" ]; then
  tok=$(curl -s --max-time 60 -X POST "$API_URL/api/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"$SD_SMOKE_EMAIL\",\"password\":\"$SD_SMOKE_PASSWORD\"}" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true)
  [ -n "$tok" ]; chk "smoke login (JWT)" $?
  if [ -n "$tok" ]; then
    ref="SMOKE-$(date +%Y%m%d%H%M%S)"
    created=$(curl -s --max-time 60 -X POST "$API_URL/api/tickets" \
      -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
      -d "{\"subject\":\"Smoke connectivity $ref\",\"description\":\"Automated smoke ($ref) — safe to close.\"}" || true)
    echo "$created" | grep -q "$ref"; chk "create ticket ($ref)" $?
    list=$(curl -s --max-time 60 -H "Authorization: Bearer $tok" "$API_URL/api/tickets" || true)
    echo "$list" | grep -q '"id"'; chk "list tickets (auth'd)" $?
  fi
else
  if [ "${SD_REQUIRE_BUSINESS:-0}" = "1" ]; then
    chk "business flow credentials" 1 "(set SD_SMOKE_EMAIL and SD_SMOKE_PASSWORD for the dedicated customer account)"
  else
    echo "SKIP | business flow (export SD_SMOKE_EMAIL + SD_SMOKE_PASSWORD to enable)"
  fi
fi

echo
echo "Result: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
