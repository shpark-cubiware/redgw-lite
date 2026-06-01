#!/bin/bash
# =============================================================================
# RedGW Admin API 전체 기능 테스트
# =============================================================================
#
# RedGW 관리 API — admin 또는 와일드카드(*) 권한 필요.
#
# 엔드포인트 (※ /api/v1 접두어 포함):
#   GET    /api/v1/admin/keys                          — 키 목록 조회 (ns, type, pattern 필터)
#   GET    /api/v1/admin/info/{ns}/{type}/{key:path}   — 키 상세 정보 (타입, TTL, 크기)
#   PUT    /api/v1/admin/ttl/{ns}/{type}/{key:path}    — TTL 설정/변경 (admin 전용)
#   DELETE /api/v1/admin/keys/{ns}/{type}/{key:path}   — 키 삭제 (admin 전용)
#   GET    /api/v1/admin/stats                         — 네임스페이스별 통계
#   GET    /api/v1/admin/clients                       — 클라이언트 목록 (admin 전용)
#   DELETE /api/v1/admin/keys/bulk                     — 패턴 기반 벌크 삭제 (admin 전용)
#   GET    /api/v1/admin/metrics                       — 서비스 메트릭 (admin 전용)
#
# 권한:
#   - 키 목록, 상세 정보, 통계: 인증된 모든 사용자
#   - TTL 설정, 키 삭제, 벌크 삭제, 클라이언트 목록, 메트릭: admin 권한 필요
# =============================================================================
BASE="${REDGW_BASE_URL:-http://localhost:3080}"
CT="Content-Type: application/json"

# .env 로드 (API 키 등)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$SCRIPT_DIR/../.env" ] && source "$SCRIPT_DIR/../.env"

ADMIN="X-API-Key: ${REDGW_ADMIN_API_KEY:-redgw_admin_xxxxxxxxxxxxxxxx}"
ERP="X-API-Key: ${REDGW_CLIENT_ERP_API_KEY:-redgw_ak_erp_xxxxxxxxxxxxxxxx}"
HRM="X-API-Key: ${REDGW_CLIENT_HRM_API_KEY:-redgw_ak_hrm_xxxxxxxxxxxxxxxx}"

echo "============================================"
echo " Admin API 테스트"
echo "============================================"

# ----- 0. 테스트 데이터 준비 -----
echo ""
echo "--- 0. 테스트 데이터 준비 ---"
curl -s -X PUT "$BASE/api/v1/ns/ERP/kv/order-status" \
  -H "$ERP" -H "$CT" \
  -d '{"value": "active"}' > /dev/null
curl -s -X PUT "$BASE/api/v1/ns/ERP/map/order:2024-001" \
  -H "$ERP" -H "$CT" \
  -d '{"fields": {"type": "일반주문", "status": "처리중"}}' > /dev/null
curl -s -X PUT "$BASE/api/v1/ns/ERP/map/order:2024-002" \
  -H "$ERP" -H "$CT" \
  -d '{"fields": {"type": "긴급주문", "status": "완료"}}' > /dev/null
curl -s -X PUT "$BASE/api/v1/ns/HRM/kv/status" \
  -H "$HRM" -H "$CT" \
  -d '{"value": "running"}' > /dev/null
curl -s -X POST "$BASE/api/v1/ns/HRM/group/processed:batch" \
  -H "$HRM" -H "$CT" \
  -d '{"members": ["EMP-001", "EMP-002"]}' > /dev/null
echo "데이터 준비 완료"

# ----- 1. 키 목록 조회 -----
echo ""
echo "--- 1. GET /admin/keys — 전체 키 목록 ---"
curl -s "$BASE/api/v1/admin/keys" \
  -H "$ADMIN" | python -m json.tool

echo ""
echo "--- 1-1. GET /admin/keys?ns=ERP — ERP 네임스페이스만 ---"
curl -s "$BASE/api/v1/admin/keys?ns=ERP" \
  -H "$ADMIN" | python -m json.tool

echo ""
echo "--- 1-2. GET /admin/keys?ns=ERP&type=hash — ERP의 Hash 키만 ---"
curl -s "$BASE/api/v1/admin/keys?ns=ERP&type=hash" \
  -H "$ADMIN" | python -m json.tool

echo ""
echo "--- 1-3. GET /admin/keys?ns=ERP&pattern=order:* — ERP의 order:* 패턴 ---"
curl -s "$BASE/api/v1/admin/keys?ns=ERP&pattern=order:*" \
  -H "$ADMIN" | python -m json.tool

# ----- 2. 키 상세 정보 -----
echo ""
echo "--- 2. GET /admin/info/ERP/map/order:2024-001 — Hash 키 상세 정보 ---"
curl -s "$BASE/api/v1/admin/info/ERP/map/order:2024-001" \
  -H "$ADMIN" | python -m json.tool

echo ""
echo "--- 2-1. GET /admin/info/HRM/kv/status — String 키 상세 정보 ---"
curl -s "$BASE/api/v1/admin/info/HRM/kv/status" \
  -H "$ADMIN" | python -m json.tool

echo ""
echo "--- 2-2. GET /admin/info/HRM/group/processed:batch — Set 키 상세 정보 ---"
curl -s "$BASE/api/v1/admin/info/HRM/group/processed:batch" \
  -H "$ADMIN" | python -m json.tool

# ----- 3. TTL 설정/변경 -----
echo ""
echo "--- 3. PUT /admin/ttl/ERP/map/order:2024-001 — TTL 3600초로 설정 ---"
curl -s -X PUT "$BASE/api/v1/admin/ttl/ERP/map/order:2024-001" \
  -H "$ADMIN" -H "$CT" \
  -d '{"ttl": 3600}' | python -m json.tool

echo ""
echo "--- 3-1. TTL 해제 (영구 보관) ---"
curl -s -X PUT "$BASE/api/v1/admin/ttl/ERP/map/order:2024-001" \
  -H "$ADMIN" -H "$CT" \
  -d '{"ttl": 0}' | python -m json.tool

# ----- 4. 네임스페이스별 통계 -----
echo ""
echo "--- 4. GET /admin/stats — 네임스페이스별 통계 ---"
curl -s "$BASE/api/v1/admin/stats" \
  -H "$ADMIN" | python -m json.tool

# ----- 5. 키 삭제 -----
echo ""
echo "--- 5. DELETE /admin/keys/ERP/map/order:2024-002 — 관리자 키 삭제 ---"
curl -s -X DELETE "$BASE/api/v1/admin/keys/ERP/map/order:2024-002" \
  -H "$ADMIN" | python -m json.tool

echo ""
echo "--- 5-1. 삭제 확인 (404) ---"
curl -s "$BASE/api/v1/admin/info/ERP/map/order:2024-002" \
  -H "$ADMIN" | python -m json.tool

# ----- 6. 클라이언트 목록 (admin 전용) -----
echo ""
echo "--- 6. GET /admin/clients — 등록된 클라이언트 목록 ---"
curl -s "$BASE/api/v1/admin/clients" \
  -H "$ADMIN" | python -m json.tool

# ----- 7. 벌크 삭제 (admin 전용) -----
# 테스트 데이터 준비 — temp: 접두어 KV 3건
echo ""
echo "--- 7. 벌크 삭제 테스트 데이터 준비 ---"
curl -s -X PUT "$BASE/api/v1/ns/ERP/kv/temp:a" \
  -H "$ERP" -H "$CT" -d '{"value": "a"}' > /dev/null
curl -s -X PUT "$BASE/api/v1/ns/ERP/kv/temp:b" \
  -H "$ERP" -H "$CT" -d '{"value": "b"}' > /dev/null
curl -s -X PUT "$BASE/api/v1/ns/ERP/kv/temp:c" \
  -H "$ERP" -H "$CT" -d '{"value": "c"}' > /dev/null
echo "temp:* 3건 생성 완료"

echo ""
echo "--- 7-1. DELETE /admin/keys/bulk?dry_run=true — 미리보기 (삭제 안 함) ---"
curl -s -X DELETE "$BASE/api/v1/admin/keys/bulk?ns=ERP&type=kv&pattern=temp:*&dry_run=true" \
  -H "$ADMIN" | python -m json.tool

echo ""
echo "--- 7-2. DELETE /admin/keys/bulk — 실제 삭제 ---"
curl -s -X DELETE "$BASE/api/v1/admin/keys/bulk?ns=ERP&type=kv&pattern=temp:*" \
  -H "$ADMIN" | python -m json.tool

echo ""
echo "--- 7-3. 삭제 확인 (keys=0) ---"
curl -s "$BASE/api/v1/admin/keys?ns=ERP&pattern=temp:*" \
  -H "$ADMIN" | python -m json.tool

# ----- 8. 서비스 메트릭 (admin 전용) -----
echo ""
echo "--- 8. GET /admin/metrics — 서비스 메트릭 (요청 수, 상태코드, 업타임) ---"
curl -s "$BASE/api/v1/admin/metrics" \
  -H "$ADMIN" | python -m json.tool

echo ""
echo "=== Admin 테스트 완료 ==="
