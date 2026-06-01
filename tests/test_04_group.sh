#!/bin/bash
# =============================================================================
# RedGW Set (Group) API 전체 기능 테스트
# =============================================================================
#
# Redis Set 타입은 중복 없는 문자열 집합을 저장합니다.
#
# Set 특성:
#   - 중복 자동 제거 (SADD 멱등성)
#   - O(1) 멤버 존재 확인 (SISMEMBER)
#   - 집합 연산 지원 (교집합, 합집합, 차집합)
#
# Redis 키 형식: {ns}:grp:{key}
#   예: ERP:grp:tags:order-2024-001 → 주문 태그 집합
#
# 엔드포인트:
#   POST   /ns/{ns}/group/{key}                   — 멤버 추가 (SADD) + TTL 지원
#   GET    /ns/{ns}/group/{key}                   — 전체 멤버 조회 (SMEMBERS)
#   GET    /ns/{ns}/group/{key}/contains/{member}  — 멤버 존재 확인 (SISMEMBER)
#   DELETE /ns/{ns}/group/{key}/{member}           — 멤버 제거 (SREM)
#   GET    /ns/{ns}/group/{key}/count              — 멤버 수 (SCARD)
#   PUT    /ns/{ns}/group/{key}/touch              — TTL 갱신 (EXPIRE/PERSIST)
#   POST   /ns/{ns}/group/ops/inter               — 교집합 (SINTER)
#   POST   /ns/{ns}/group/ops/union               — 합집합 (SUNION)
#   POST   /ns/{ns}/group/ops/diff                — 차집합 (SDIFF)
#   POST   /ns/{ns}/group/batch                   — 배치 조회 (Pipeline SMEMBERS)
#   PUT    /ns/{ns}/group/batch                    — 배치 추가 (Pipeline SADD)
#
# 활용 예:
#   - 온라인 사용자 추적
#   - 주문 태그 관리
#   - 인력 중복 처리 추적 (이미 처리된 건 확인)
#   - 공통 고객 분석 (교집합)
#   - 전체 대상 목록 통합 (합집합)
#   - 미처리 건 추출 (차집합)
# =============================================================================
BASE="${REDGW_BASE_URL:-http://localhost:3080}/api/v1"
CT="Content-Type: application/json"

# .env 로드 (API 키 등)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$SCRIPT_DIR/../.env" ] && source "$SCRIPT_DIR/../.env"

HRM="X-API-Key: ${REDGW_CLIENT_HRM_API_KEY:-redgw_ak_hrm_xxxxxxxxxxxxxxxx}"
ERP="X-API-Key: ${REDGW_CLIENT_ERP_API_KEY:-redgw_ak_erp_xxxxxxxxxxxxxxxx}"

echo "============================================"
echo " Set (Group) API 테스트"
echo "============================================"

# ----- 1. 멤버 추가 (SADD) -----
echo ""
echo "--- 1. POST /ns/ERP/group/online:users — 온라인 사용자 등록 ---"
curl -s -X POST "$BASE/ns/ERP/group/online:users" \
  -H "$ERP" -H "$CT" \
  -d '{"members": ["kim", "lee", "park"]}' | python -m json.tool

# ----- 2. 전체 멤버 조회 (SMEMBERS) -----
echo ""
echo "--- 2. GET /ns/ERP/group/online:users — 전체 멤버 목록 ---"
curl -s "$BASE/ns/ERP/group/online:users" \
  -H "$ERP" | python -m json.tool

# ----- 3. 멤버 존재 확인 (SISMEMBER) -----
echo ""
echo "--- 3. GET /ns/ERP/group/online:users/contains/kim — kim 존재 확인 (true) ---"
curl -s "$BASE/ns/ERP/group/online:users/contains/kim" \
  -H "$ERP" | python -m json.tool

echo ""
echo "--- 3-1. GET /ns/ERP/group/online:users/contains/choi — choi 존재 확인 (false) ---"
curl -s "$BASE/ns/ERP/group/online:users/contains/choi" \
  -H "$ERP" | python -m json.tool

# ----- 4. 멤버 수 (SCARD) -----
echo ""
echo "--- 4. GET /ns/ERP/group/online:users/count — 멤버 수 ---"
curl -s "$BASE/ns/ERP/group/online:users/count" \
  -H "$ERP" | python -m json.tool

# ----- 5. 멤버 제거 (SREM) -----
echo ""
echo "--- 5. DELETE /ns/ERP/group/online:users/kim — kim 로그아웃 ---"
curl -s -X DELETE "$BASE/ns/ERP/group/online:users/kim" \
  -H "$ERP" | python -m json.tool

echo ""
echo "--- 5-1. 제거 후 멤버 수 확인 ---"
curl -s "$BASE/ns/ERP/group/online:users/count" \
  -H "$ERP" | python -m json.tool

# ----- 6. 주문 태그 관리 -----
echo ""
echo "--- 6. POST /ns/ERP/group/tags:order-2024-001 — 주문 태그 추가 ---"
curl -s -X POST "$BASE/ns/ERP/group/tags:order-2024-001" \
  -H "$ERP" -H "$CT" \
  -d '{"members": ["긴급", "대량주문", "서울"]}' | python -m json.tool

# ----- 7. 중복 방지 (멱등성 확인) -----
echo ""
echo "--- 7. POST /ns/HRM/group/processed:batch-001 — 처리된 staff_id 등록 ---"
curl -s -X POST "$BASE/ns/HRM/group/processed:batch-001" \
  -H "$HRM" -H "$CT" \
  -d '{"members": ["EMP-2024-001", "EMP-2024-002"]}' | python -m json.tool

echo ""
echo "--- 7-1. 이미 처리된 EMP-2024-001 확인 (true) ---"
curl -s "$BASE/ns/HRM/group/processed:batch-001/contains/EMP-2024-001" \
  -H "$HRM" | python -m json.tool

echo ""
echo "--- 7-2. 미처리 EMP-9999-999 확인 (false) ---"
curl -s "$BASE/ns/HRM/group/processed:batch-001/contains/EMP-9999-999" \
  -H "$HRM" | python -m json.tool

# ----- 8. 집합 연산 — 교집합/합집합/차집합 -----
echo ""
echo "--- 8. 교집합 준비: HRM customer + ERP customer ---"
curl -s -X POST "$BASE/ns/shared/group/customer:HRM" \
  -H "$HRM" -H "$CT" \
  -d '{"members": ["kim-001", "lee-002", "park-003"]}' > /dev/null
curl -s -X POST "$BASE/ns/shared/group/customer:ERP" \
  -H "$ERP" -H "$CT" \
  -d '{"members": ["kim-001", "choi-004", "park-003"]}' > /dev/null
echo "데이터 준비 완료"

echo ""
echo "--- 8-1. POST /ns/shared/group/ops/inter — 교집합 (공통 고객) ---"
curl -s -X POST "$BASE/ns/shared/group/ops/inter" \
  -H "$ERP" -H "$CT" \
  -d '{"keys": ["customer:HRM", "customer:ERP"]}' | python -m json.tool

echo ""
echo "--- 8-2. POST /ns/shared/group/ops/union — 합집합 (전체 고객) ---"
curl -s -X POST "$BASE/ns/shared/group/ops/union" \
  -H "$ERP" -H "$CT" \
  -d '{"keys": ["customer:HRM", "customer:ERP"]}' | python -m json.tool

echo ""
echo "--- 8-3. POST /ns/shared/group/ops/diff — 차집합 (HRM에만 있는 고객) ---"
curl -s -X POST "$BASE/ns/shared/group/ops/diff" \
  -H "$ERP" -H "$CT" \
  -d '{"keys": ["customer:HRM", "customer:ERP"]}' | python -m json.tool

# ----- 9. TTL 지원 (SADD + EXPIRE) -----
# 멤버 추가 시 TTL을 지정하면 Set 전체에 만료 시간이 설정됨
echo ""
echo "--- 9. POST /ns/ERP/group/session:active — TTL 300초와 함께 SADD ---"
curl -s -X POST "$BASE/ns/ERP/group/session:active" \
  -H "$ERP" -H "$CT" \
  -d '{"members": ["sess-001", "sess-002"], "ttl": 300}' | python -m json.tool

# ----- 10. 배치 추가 (Pipeline SADD) -----
# 여러 Set에 멤버를 한 번에 추가
echo ""
echo "--- 10. PUT /ns/ERP/group/batch — 배치 추가 (2개 Set) ---"
curl -s -X PUT "$BASE/ns/ERP/group/batch" \
  -H "$ERP" -H "$CT" \
  -d '{
    "items": {
      "team:alpha": ["kim", "lee", "park"],
      "team:beta": ["choi", "jung"]
    },
    "ttl": 3600
  }' | python -m json.tool

# ----- 11. 배치 조회 (Pipeline SMEMBERS) -----
echo ""
echo "--- 11. POST /ns/ERP/group/batch — 배치 조회 (존재 2 + 미존재 1) ---"
curl -s -X POST "$BASE/ns/ERP/group/batch" \
  -H "$ERP" -H "$CT" \
  -d '{"keys": ["team:alpha", "team:beta", "team:gamma"]}' | python -m json.tool

# ----- 12. TTL 갱신 — touch -----
# PUT /ns/{ns}/group/{key}/touch: Set의 TTL만 갱신
echo ""
echo "--- 12. PUT /ns/ERP/group/online:users/touch — TTL을 1800초로 갱신 ---"
curl -s -X PUT "$BASE/ns/ERP/group/online:users/touch" \
  -H "$ERP" -H "$CT" \
  -d '{"ttl": 1800}' | python -m json.tool

echo ""
echo "--- 12-1. PUT /ns/ERP/group/online:users/touch — TTL 해제 (영구 보관) ---"
curl -s -X PUT "$BASE/ns/ERP/group/online:users/touch" \
  -H "$ERP" -H "$CT" \
  -d '{"ttl": 0}' | python -m json.tool

echo ""
echo "--- 12-2. 존재하지 않는 Set touch (404) ---"
curl -s -X PUT "$BASE/ns/ERP/group/nonexistent-group/touch" \
  -H "$ERP" -H "$CT" \
  -d '{"ttl": 300}' | python -m json.tool

echo ""
echo "=== Group 테스트 완료 ==="
