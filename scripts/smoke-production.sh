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
  echo "SKIP | business flow (export SD_SMOKE_EMAIL + SD_SMOKE_PASSWORD to enable)"
fi

echo
echo "Result: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
