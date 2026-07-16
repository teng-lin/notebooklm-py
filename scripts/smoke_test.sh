#!/usr/bin/env bash
# Baoku clone — integration smoke test
# Prerequisites: backend running on port 8000, frontend running on port 5173
set -euo pipefail

BASE_URL="${API_BASE:-http://localhost:8000}"
FRONTEND_URL="${FRONTEND_BASE:-http://localhost:5173}"
PASS=0
FAIL=0

green() { printf "\033[32m%s\033[0m\n" "$1"; }
red()   { printf "\033[31m%s\033[0m\n" "$1"; }
bold()  { printf "\033[1m%s\033[0m\n" "$1"; }

check() {
    local name="$1"
    local code="$2"
    shift 2
    local actual
    actual=$(curl -s -o /dev/null -w "%{http_code}" "$@" 2>/dev/null || true)
    if [ "$actual" = "$code" ]; then
        green "  ✓ $name"
        PASS=$((PASS + 1))
    else
        red "  ✗ $name (expected $code, got $actual)"
        FAIL=$((FAIL + 1))
    fi
}

check_json() {
    local name="$1"
    local field="$2"
    local expected="$3"
    shift 3
    local actual
    actual=$(curl -s "$@" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('$field', ''))" 2>/dev/null || true)
    if [ "$actual" = "$expected" ]; then
        green "  ✓ $name"
        PASS=$((PASS + 1))
    else
        red "  ✗ $name (expected $field=$expected, got $actual)"
        FAIL=$((FAIL + 1))
    fi
}

bold "=== Baoku Clone — Integration Smoke Test ==="
bold "Backend: $BASE_URL"
bold ""

# 1. Health check
bold "1. Basic connectivity"
check "Health endpoint returns 200" 200 "$BASE_URL/api/health"

# 2. Auth flow
bold "2. Auth flow"
check "Register returns 200" 200 -X POST "$BASE_URL/api/auth/register" \
    -H "Content-Type: application/json" \
    -d '{"username":"smoketest","password":"SmokePass1","display_name":"Smoke Test"}'

TOKEN=$(curl -s -X POST "$BASE_URL/api/auth/login" \
    -H "Content-Type: application/json" \
    -d '{"username":"smoketest","password":"SmokePass1"}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null || true)

if [ -n "$TOKEN" ]; then
    green "  ✓ Login returns JWT token"
    PASS=$((PASS + 1))
else
    red "  ✗ Login failed to return token"
    FAIL=$((FAIL + 1))
fi

AUTH="Authorization: Bearer $TOKEN"

check_json "Me endpoint returns correct username" "username" "smoketest" \
    "$BASE_URL/api/auth/me" -H "$AUTH"

# 3. Notebook CRUD
bold "3. Notebook CRUD"
NOTEBOOK_ID=$(curl -s -X POST "$BASE_URL/api/notebooks" \
    -H "Content-Type: application/json" \
    -H "$AUTH" \
    -d '{"title":"Smoke Test Notebook"}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || true)

if [ -n "$NOTEBOOK_ID" ]; then
    green "  ✓ Create notebook returns ID"
    PASS=$((PASS + 1))
else
    red "  ✗ Create notebook failed"
    FAIL=$((FAIL + 1))
fi

check "List notebooks returns 200" 200 "$BASE_URL/api/notebooks" -H "$AUTH"
check "Get notebook returns 200" 200 "$BASE_URL/api/notebooks/$NOTEBOOK_ID" -H "$AUTH"

# 4. Sources
bold "4. Source upload"
check "Upload source returns 200" 200 -X POST "$BASE_URL/api/sources/upload" \
    -H "$AUTH" \
    -F "notebook_id=$NOTEBOOK_ID" \
    -F "file=@pyproject.toml"

# 5. Chat
bold "5. Chat"
SESSION_ID=$(curl -s -X POST "$BASE_URL/api/chat/sessions" \
    -H "Content-Type: application/json" \
    -H "$AUTH" \
    -d "{\"notebook_id\":$NOTEBOOK_ID,\"title\":\"Smoke Chat\"}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || true)

if [ -n "$SESSION_ID" ]; then
    green "  ✓ Create chat session returns ID"
    PASS=$((PASS + 1))
else
    red "  ✗ Create chat session failed"
    FAIL=$((FAIL + 1))
fi

check "Send message returns 200" 200 -X POST "$BASE_URL/api/chat/sessions/$SESSION_ID/messages" \
    -H "Content-Type: application/json" \
    -H "$AUTH" \
    -d '{"content":"Hello, this is a smoke test message"}'

# 6. Generation
bold "6. Generation"
check "Generate document returns 200" 200 -X POST "$BASE_URL/api/generation/generate" \
    -H "Content-Type: application/json" \
    -H "$AUTH" \
    -d "{\"notebook_id\":$NOTEBOOK_ID,\"content_type\":\"document\",\"prompt\":\"测试摘要\",\"template\":\"summary\"}"

# 7. External KB
bold "7. External KB"
check "Create external KB connection returns 200" 200 -X POST "$BASE_URL/api/external-kb/connections" \
    -H "Content-Type: application/json" \
    -H "$AUTH" \
    -d '{"name":"Smoke Test KB","provider_type":"openapi","api_base_url":"https://example.com/api"}'

# 8. Auth middleware
bold "8. Auth middleware"
check "No token returns 401" 401 "$BASE_URL/api/notebooks"
check "Bad token returns 401" 401 "$BASE_URL/api/notebooks" -H "Authorization: Bearer invalid"

# 9. CORS (dev mode)
if [ "${BAOKU_DEV:-0}" = "1" ]; then
    bold "9. CORS headers"
    CORS_CHECK=$(curl -s -o /dev/null -w "%{http_code}" -X OPTIONS "$BASE_URL/api/health" \
        -H "Origin: http://localhost:5173" \
        -H "Access-Control-Request-Method: GET")
    if [ "$CORS_CHECK" = "200" ]; then
        green "  ✓ CORS preflight returns 200"
        PASS=$((PASS + 1))
    else
        red "  ✗ CORS preflight failed (is BAOKU_DEV=1?)"
        FAIL=$((FAIL + 1))
    fi
fi

# Summary
bold ""
bold "=== Results ==="
bold "Passed: $PASS"
bold "Failed: $FAIL"
if [ "$FAIL" -eq 0 ]; then
    green "All smoke tests passed!"
else
    red "Some smoke tests failed."
    exit 1
fi
