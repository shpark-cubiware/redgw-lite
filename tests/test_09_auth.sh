#!/bin/bash
# =============================================================================
# RedGW 인증/권한 테스트
# =============================================================================
#
# RedGW 보안 체계:
#   1단계: 인증(Authentication) — X-API-Key 헤더로 클라이언트 식별
#     - 헤더 누락: 422 Unprocessable Entity
#     - 잘못된 키: 401 Unauthorized
#
#   2단계: 권한(Authorization) — 네임스페이스별 read/write 권한 검사
#     - 권한 없음: 403 Forbidden
#
# 권한 매트릭스 (config.yaml):
#   ┌────────────┬──────┬──────┬──────┬──────┬────────┐
#   │ 클라이언트   │ HRM │ ERP │ CRM │shared│   *    │
#   ├────────────┼──────┼──────┼──────┼──────┼────────┤
#   │ HRM       │ rw   │  r   │  -   │  rw  │   -    │
#   │ ERP       │  r   │ rw   │  r   │  rw  │   -    │
#   │ CRM       │  -   │  r   │ rw   │  rw  │   -    │
#   │ MONITOR │  -   │  -   │  -   │  -   │   r    │
#   │ admin      │ rw   │ rw   │ rw   │  rw  │  rw    │
#   └────────────┴──────┴──────┴──────┴──────┴────────┘
# =============================================================================
BASE="${REDGW_BASE_URL:-http://localhost:3080}/api/v1"
CT="Content-Type: application/json"

# .env 로드 (API 키 등)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$SCRIPT_DIR/../.env" ] && source "$SCRIPT_DIR/../.env"

HRM="X-API-Key: ${REDGW_CLIENT_HRM_API_KEY:-redgw_ak_hrm_xxxxxxxxxxxxxxxx}"
ERP="X-API-Key: ${REDGW_CLIENT_ERP_API_KEY:-redgw_ak_erp_xxxxxxxxxxxxxxxx}"
CRM="X-API-Key: ${REDGW_CLIENT_CRM_API_KEY:-redgw_ak_crm_xxxxxxxxxxxxxxxx}"
MONITOR="X-API-Key: ${REDGW_CLIENT_MONITOR_API_KEY:-redgw_ak_monitor_xxxxxxxxxxxxxxxx}"
ADMIN="X-API-Key: ${REDGW_ADMIN_API_KEY:-redgw_admin_xxxxxxxxxxxxxxxx}"

echo "============================================"
echo " 인증/권한 테스트"
echo "============================================"

# ----- 0. 테스트 데이터 준비 -----
curl -s -X PUT "$BASE/ns/HRM/kv/status" \
  -H "$HRM" -H "$CT" \
  -d '{"value": "running"}' > /dev/null
curl -s -X PUT "$BASE/ns/ERP/kv/status" \
  -H "$ERP" -H "$CT" \
  -d '{"value": "active"}' > /dev/null
curl -s -X PUT "$BASE/ns/CRM/kv/status" \
  -H "$CRM" -H "$CT" \
  -d '{"value": "standby"}' > /dev/null
echo "테스트 데이터 준비 완료"

# ----- 1. API 키 없이 요청 (422) -----
echo ""
echo "--- 1. API 키 없이 요청 (422 — 헤더 필수) ---"
curl -s "$BASE/ns/HRM/kv/status" | python -m json.tool

# ----- 2. 잘못된 API 키 (401) -----
echo ""
echo "--- 2. 잘못된 API 키 (401) ---"
curl -s "$BASE/ns/HRM/kv/status" \
  -H "X-API-Key: invalid_key_xxxxx" | python -m json.tool

# ----- 3. 자기 네임스페이스 read/write (허용) -----
echo ""
echo "--- 3. HRM → HRM read (허용) ---"
curl -s "$BASE/ns/HRM/kv/status" \
  -H "$HRM" | python -m json.tool

# ----- 4. 교차 네임스페이스 read (허용) -----
echo ""
echo "--- 4. ERP → HRM read (허용 — ERP는 HRM에 read 권한) ---"
curl -s "$BASE/ns/HRM/kv/status" \
  -H "$ERP" | python -m json.tool

# ----- 5. 교차 네임스페이스 write (거부) -----
echo ""
echo "--- 5. HRM → ERP write (거부 403 — HRM는 ERP에 read만) ---"
curl -s -X PUT "$BASE/ns/ERP/kv/hacked" \
  -H "$HRM" -H "$CT" \
  -d '{"value": "hacked"}' | python -m json.tool

# ----- 6. 권한 없는 네임스페이스 접근 (거부) -----
echo ""
echo "--- 6. CRM → HRM read (거부 403 — CRM는 HRM 권한 없음) ---"
curl -s "$BASE/ns/HRM/kv/status" \
  -H "$CRM" | python -m json.tool

# ----- 7. MONITOR 와일드카드 read -----
echo ""
echo "--- 7. MONITOR → HRM read (허용 — '*' 와일드카드 read) ---"
curl -s "$BASE/ns/HRM/kv/status" \
  -H "$MONITOR" | python -m json.tool

echo ""
echo "--- 7-1. MONITOR → ERP read (허용) ---"
curl -s "$BASE/ns/ERP/kv/status" \
  -H "$MONITOR" | python -m json.tool

echo ""
echo "--- 7-2. MONITOR → CRM read (허용) ---"
curl -s "$BASE/ns/CRM/kv/status" \
  -H "$MONITOR" | python -m json.tool

echo ""
echo "--- 7-3. MONITOR → ERP write (거부 403 — read만 허용) ---"
curl -s -X PUT "$BASE/ns/ERP/kv/test" \
  -H "$MONITOR" -H "$CT" \
  -d '{"value": "test"}' | python -m json.tool

# ----- 8. shared 네임스페이스 (공유 영역) -----
echo ""
echo "--- 8. HRM → shared write (허용) ---"
curl -s -X PUT "$BASE/ns/shared/kv/shared-test" \
  -H "$HRM" -H "$CT" \
  -d '{"value": "from-hrm"}' | python -m json.tool

echo ""
echo "--- 8-1. CRM → shared read (허용) ---"
curl -s "$BASE/ns/shared/kv/shared-test" \
  -H "$CRM" | python -m json.tool

# ----- 9. Admin 전체 접근 -----
echo ""
echo "--- 9. Admin → 모든 네임스페이스 read/write (허용) ---"
curl -s "$BASE/ns/HRM/kv/status" \
  -H "$ADMIN" | python -m json.tool

echo ""
echo "=== 인증/권한 테스트 완료 ==="
