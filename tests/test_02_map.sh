#!/bin/bash
# =============================================================================
# RedGW Hash (Map) API 전체 기능 테스트
# =============================================================================
#
# Redis Hash 타입은 하나의 키에 여러 필드-값 쌍을 저장합니다.
# RDBMS의 한 행(row)에 해당하며, 필드 단위로 읽기/쓰기가 가능합니다.
#
# Redis 키 형식: {ns}:map:{key}
#   예: ERP:map:order:2024-001 → ERP의 주문 정보
#
# 엔드포인트:
#   PUT    /ns/{ns}/map/{key}          — 다수 필드 저장 (HMSET)
#   GET    /ns/{ns}/map/{key}          — 전체 필드 조회 (HGETALL)
#   GET    /ns/{ns}/map/{key}/{field}  — 특정 필드 조회 (HGET)
#   PUT    /ns/{ns}/map/{key}/{field}  — 단일 필드 저장 (HSET)
#   PUT    /ns/{ns}/map/{key}/touch    — TTL 갱신 (EXPIRE/PERSIST)
#   DELETE /ns/{ns}/map/{key}          — 전체 삭제 (DEL)
#   DELETE /ns/{ns}/map/{key}/{field}  — 특정 필드 삭제 (HDEL)
#   POST   /ns/{ns}/map/batch         — 배치 조회 (Pipeline HGETALL)
#   PUT    /ns/{ns}/map/batch          — 배치 저장 (Pipeline HMSET)
#
# 활용 예:
#   - 주문 정보 (type, status, region, manager)
#   - 고객/거래처 프로필
#   - 장비 상태 캐시
#   - HRM 배정 결과
# =============================================================================
BASE="${REDGW_BASE_URL:-http://localhost:3080}/api/v1"
CT="Content-Type: application/json"

# .env 로드 (API 키 등)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$SCRIPT_DIR/../.env" ] && source "$SCRIPT_DIR/../.env"

HRM="X-API-Key: ${REDGW_CLIENT_HRM_API_KEY:-redgw_ak_hrm_xxxxxxxxxxxxxxxx}"
ERP="X-API-Key: ${REDGW_CLIENT_ERP_API_KEY:-redgw_ak_erp_xxxxxxxxxxxxxxxx}"
CRM="X-API-Key: ${REDGW_CLIENT_CRM_API_KEY:-redgw_ak_crm_xxxxxxxxxxxxxxxx}"

echo "============================================"
echo " Hash (Map) API 테스트"
echo "============================================"

# ----- 1. 다수 필드 저장 (HMSET) -----
echo ""
echo "--- 1. PUT /ns/ERP/map/order:2024-001 — 주문 정보 전체 저장 ---"
curl -s -X PUT "$BASE/ns/ERP/map/order:2024-001" \
  -H "$ERP" -H "$CT" \
  -d '{
    "fields": {
      "type": "일반주문",
      "status": "처리중",
      "region": "서울강남",
      "manager": "kim"
    },
    "ttl": 86400
  }' | python -m json.tool

# ----- 2. 전체 필드 조회 (HGETALL) -----
echo ""
echo "--- 2. GET /ns/ERP/map/order:2024-001 — 주문 전체 조회 ---"
curl -s "$BASE/ns/ERP/map/order:2024-001" \
  -H "$ERP" | python -m json.tool

# ----- 3. 특정 필드 조회 (HGET) -----
echo ""
echo "--- 3. GET /ns/ERP/map/order:2024-001/status — status 필드만 조회 ---"
curl -s "$BASE/ns/ERP/map/order:2024-001/status" \
  -H "$ERP" | python -m json.tool

# ----- 4. 단일 필드 저장 (HSET) -----
echo ""
echo "--- 4. PUT /ns/ERP/map/order:2024-001/status — status 필드만 변경 ---"
curl -s -X PUT "$BASE/ns/ERP/map/order:2024-001/status" \
  -H "$ERP" -H "$CT" \
  -d '{"value": "완료"}' | python -m json.tool

echo ""
echo "--- 4-1. 변경 확인 ---"
curl -s "$BASE/ns/ERP/map/order:2024-001/status" \
  -H "$ERP" | python -m json.tool

# ----- 5. 교차 네임스페이스 읽기 -----
echo ""
echo "--- 5. GET /ns/ERP/map/order:2024-001 — HRM가 ERP 주문 읽기 (read 허용) ---"
curl -s "$BASE/ns/ERP/map/order:2024-001" \
  -H "$HRM" | python -m json.tool

echo ""
echo "--- 5-1. GET /ns/ERP/map/order:2024-001 — CRM가 ERP 주문 읽기 (read 허용) ---"
curl -s "$BASE/ns/ERP/map/order:2024-001" \
  -H "$CRM" | python -m json.tool

# ----- 6. 고객 프로필 저장 및 필드 단위 조회 -----
echo ""
echo "--- 6. PUT /ns/ERP/map/customer:kim-001 — 고객 프로필 저장 ---"
curl -s -X PUT "$BASE/ns/ERP/map/customer:kim-001" \
  -H "$ERP" -H "$CT" \
  -d '{"fields": {"name": "김철수", "jumin_no": "900101-1xxxxxx", "staff_id": "EMP-2024-001", "status": "고객"}}' | python -m json.tool

echo ""
echo "--- 6-1. HRM가 고객 staff_id만 조회 ---"
curl -s "$BASE/ns/ERP/map/customer:kim-001/staff_id" \
  -H "$HRM" | python -m json.tool

# ----- 7. 특정 필드 삭제 (HDEL) -----
echo ""
echo "--- 7. DELETE /ns/ERP/map/order:2024-001/manager — manager 필드 삭제 ---"
curl -s -X DELETE "$BASE/ns/ERP/map/order:2024-001/manager" \
  -H "$ERP" | python -m json.tool

# ----- 8. 전체 삭제 (DEL) -----
echo ""
echo "--- 8. DELETE /ns/ERP/map/customer:kim-001 — 고객 전체 삭제 ---"
curl -s -X DELETE "$BASE/ns/ERP/map/customer:kim-001" \
  -H "$ERP" | python -m json.tool

# ----- 9. 배치 저장 (Pipeline HMSET) -----
# 여러 Hash 키에 필드를 한 번에 저장
echo ""
echo "--- 9. PUT /ns/ERP/map/batch — 배치 저장 (2개 해시) ---"
curl -s -X PUT "$BASE/ns/ERP/map/batch" \
  -H "$ERP" -H "$CT" \
  -d '{
    "items": {
      "device:cam-001": {"location": "서울강남", "status": "online", "model": "HD-4K"},
      "device:cam-002": {"location": "부산해운대", "status": "offline", "model": "FHD"}
    },
    "ttl": 3600
  }' | python -m json.tool

# ----- 10. 배치 조회 (Pipeline HGETALL) -----
echo ""
echo "--- 10. POST /ns/ERP/map/batch — 배치 조회 (존재 2 + 미존재 1) ---"
curl -s -X POST "$BASE/ns/ERP/map/batch" \
  -H "$ERP" -H "$CT" \
  -d '{"keys": ["device:cam-001", "device:cam-002", "device:cam-999"]}' | python -m json.tool

# ----- 11. 값 크기 검증 (HMSET) -----
echo ""
echo "--- 11. PUT /ns/ERP/map/big — 필드 값 크기 초과 (→ 400) ---"
python -c "import json; print(json.dumps({'fields': {'data': 'x'*1048577}}))" > /tmp/redgw_big_map.json
curl -s -X PUT "$BASE/ns/ERP/map/big" \
  -H "$ERP" -H "$CT" \
  -d @/tmp/redgw_big_map.json | python -m json.tool

# ----- 12. 값 크기 검증 (HSET 단일 필드) -----
echo ""
echo "--- 12. PUT /ns/ERP/map/order:2024-001/bigfield — 단일 필드 크기 초과 (→ 400) ---"
python -c "import json; print(json.dumps({'value': 'x'*1048577}))" > /tmp/redgw_big_field.json
curl -s -X PUT "$BASE/ns/ERP/map/order:2024-001/bigfield" \
  -H "$ERP" -H "$CT" \
  -d @/tmp/redgw_big_field.json | python -m json.tool
rm -f /tmp/redgw_big_map.json /tmp/redgw_big_field.json

# ----- 13. TTL 갱신 — touch -----
# PUT /ns/{ns}/map/{key}/touch: 관리자 권한 없이 Hash 키의 TTL만 갱신
echo ""
echo "--- 13. PUT /ns/ERP/map/order:2024-001/touch — TTL을 7200초로 갱신 ---"
curl -s -X PUT "$BASE/ns/ERP/map/order:2024-001/touch" \
  -H "$ERP" -H "$CT" \
  -d '{"ttl": 7200}' | python -m json.tool

echo ""
echo "--- 13-1. PUT /ns/ERP/map/order:2024-001/touch — TTL 해제 (영구 보관) ---"
curl -s -X PUT "$BASE/ns/ERP/map/order:2024-001/touch" \
  -H "$ERP" -H "$CT" \
  -d '{"ttl": 0}' | python -m json.tool

echo ""
echo "--- 13-2. 존재하지 않는 키 touch (404) ---"
curl -s -X PUT "$BASE/ns/ERP/map/nonexistent-map/touch" \
  -H "$ERP" -H "$CT" \
  -d '{"ttl": 300}' | python -m json.tool

echo ""
echo "=== Map 테스트 완료 ==="
