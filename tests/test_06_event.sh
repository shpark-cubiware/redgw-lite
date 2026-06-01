#!/bin/bash
# =============================================================================
# RedGW Stream (Event) API 전체 기능 테스트
# =============================================================================
#
# Redis Stream은 메시지 영속성을 보장하는 이벤트 로그 시스템입니다.
# Pub/Sub와 달리 메시지가 저장되며, Consumer Group으로 분산 소비가 가능합니다.
#
# Stream vs Pub/Sub:
#   Stream: 메시지 저장됨, 재읽기 가능, Consumer Group + ACK
#   Pub/Sub: Fire-and-forget, 구독자 없으면 유실
#
# Redis 키 형식: {ns}:evt:{key}
#   예: ERP:evt:order-registered → 주문 등록 이벤트 스트림
#
# 엔드포인트:
#   POST /ns/{ns}/event/{key}                      — 이벤트 발행 (XADD)
#   POST /ns/{ns}/event/{key}/batch                — 이벤트 일괄 발행 (Pipeline XADD)
#   GET  /ns/{ns}/event/{key}                      — 이벤트 읽기 (XRANGE)
#   POST /ns/{ns}/event/{key}/group                — Consumer Group 생성
#   GET  /ns/{ns}/event/{key}/group/{g}/read       — 그룹 소비 (XREADGROUP)
#   POST /ns/{ns}/event/{key}/group/{g}/ack        — 처리 확인 (XACK)
#   PUT  /ns/{ns}/event/{key}/touch                — TTL 갱신 (EXPIRE/PERSIST)
#   GET  /ns/{ns}/event/{key}/info                 — 스트림 정보 (XINFO)
#
# 핵심 규칙:
#   - Consumer Group 생성/ACK = read 권한 (구독 행위)
#   - 이벤트 발행(XADD) = write 권한
#
# Consumer Group 흐름:
#   1. Group 생성 → 2. 이벤트 발행 → 3. Worker가 읽기 → 4. ACK 처리
#   ACK하지 않은 메시지는 pending → 다른 Worker에게 재배정 가능
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
echo " Stream (Event) API 테스트"
echo "============================================"

# ----- 1. 이벤트 발행 (XADD) -----
echo ""
echo "--- 1. POST /ns/ERP/event/order-registered — 주문 등록 이벤트 발행 ---"
curl -s -X POST "$BASE/ns/ERP/event/order-registered" \
  -H "$ERP" -H "$CT" \
  -d '{"data": {"order_id": "2024-001", "type": "일반주문", "region": "서울강남"}}' | python -m json.tool

echo ""
echo "--- 1-1. 주문 이벤트 추가 발행 ---"
curl -s -X POST "$BASE/ns/ERP/event/order-registered" \
  -H "$ERP" -H "$CT" \
  -d '{"data": {"order_id": "2024-002", "type": "긴급주문", "region": "부산해운대"}}' | python -m json.tool

echo ""
echo "--- 1-2. 주문 이벤트 추가 발행 ---"
curl -s -X POST "$BASE/ns/ERP/event/order-registered" \
  -H "$ERP" -H "$CT" \
  -d '{"data": {"order_id": "2024-003", "type": "대량주문", "region": "대전유성"}}' | python -m json.tool

# ----- 2. 이벤트 읽기 (XRANGE) -----
echo ""
echo "--- 2. GET /ns/ERP/event/order-registered?last_id=0&count=10 — 처음부터 읽기 ---"
curl -s "$BASE/ns/ERP/event/order-registered?last_id=0&count=10" \
  -H "$ERP" | python -m json.tool

echo ""
echo "--- 2-1. count=1 — 1건만 읽기 ---"
curl -s "$BASE/ns/ERP/event/order-registered?last_id=0&count=1" \
  -H "$ERP" | python -m json.tool

# ----- 3. Consumer Group 생성 (XGROUP CREATE) -----
echo ""
echo "--- 3. POST .../group — CRM Consumer Group 생성 ---"
curl -s -X POST "$BASE/ns/ERP/event/order-registered/group" \
  -H "$CRM" -H "$CT" \
  -d '{"group": "crm-analysis-group"}' | python -m json.tool

echo ""
echo "--- 3-1. 중복 그룹 생성 시도 (409) ---"
curl -s -X POST "$BASE/ns/ERP/event/order-registered/group" \
  -H "$CRM" -H "$CT" \
  -d '{"group": "crm-analysis-group"}' | python -m json.tool

# ----- 4. Consumer Group 읽기 (XREADGROUP) -----
echo ""
echo "--- 4. GET .../group/crm-analysis-group/read — worker-1이 2건 소비 ---"
curl -s "$BASE/ns/ERP/event/order-registered/group/crm-analysis-group/read?consumer=worker-1&count=2" \
  -H "$CRM" | python -m json.tool

echo ""
echo "--- 4-1. worker-2가 나머지 소비 ---"
curl -s "$BASE/ns/ERP/event/order-registered/group/crm-analysis-group/read?consumer=worker-2&count=5" \
  -H "$CRM" | python -m json.tool

# ----- 5. 처리 완료 확인 (XACK) -----
echo ""
echo "--- 5. ACK 처리를 위해 먼저 이벤트 ID 추출 ---"
# 이벤트 읽기로 ID 확인
EVENT_IDS=$(curl -s "$BASE/ns/ERP/event/order-registered?last_id=0&count=10" \
  -H "$ERP" | python -c "
import sys, json
data = json.load(sys.stdin)
ids = [e['id'] for e in data['data']['events']]
print(json.dumps(ids))
")
echo "이벤트 IDs: $EVENT_IDS"

echo ""
echo "--- 5-1. POST .../group/crm-analysis-group/ack — ACK 처리 ---"
curl -s -X POST "$BASE/ns/ERP/event/order-registered/group/crm-analysis-group/ack" \
  -H "$CRM" -H "$CT" \
  -d "{\"ids\": $EVENT_IDS}" | python -m json.tool

# ----- 6. 스트림 정보 조회 (XINFO STREAM) -----
echo ""
echo "--- 6. GET /ns/ERP/event/order-registered/info — 스트림 상세 정보 ---"
curl -s "$BASE/ns/ERP/event/order-registered/info" \
  -H "$ERP" | python -m json.tool

# ----- 7. HRM 배정 완료 이벤트 -----
echo ""
echo "--- 7. POST /ns/HRM/event/assign-complete — HRM 배정 완료 이벤트 ---"
curl -s -X POST "$BASE/ns/HRM/event/assign-complete" \
  -H "$HRM" -H "$CT" \
  -d '{"data": {"req_id": "req-001", "score": "95", "staff_id": "kim-001"}}' | python -m json.tool

echo ""
echo "--- 7-1. ERP가 HRM 배정 이벤트 읽기 (교차 네임스페이스 read) ---"
curl -s "$BASE/ns/HRM/event/assign-complete?last_id=0&count=10" \
  -H "$ERP" | python -m json.tool

# ----- 8. 값 크기 검증 (VALUE_TOO_LARGE) -----
# data 필드의 각 value가 max_value_size를 초과하면 400 에러
echo ""
echo "--- 8. POST /ns/ERP/event/big — data 값 크기 초과 (→ 400) ---"
python -c "import json; print(json.dumps({'data': {'payload': 'x'*1048577}}))" > /tmp/redgw_big_evt.json
curl -s -X POST "$BASE/ns/ERP/event/big" \
  -H "$ERP" -H "$CT" \
  -d @/tmp/redgw_big_evt.json | python -m json.tool
rm -f /tmp/redgw_big_evt.json

# ----- 9. 이벤트 일괄 발행 (Batch XADD) -----
# POST /ns/{ns}/event/{key}/batch: 여러 이벤트를 파이프라인으로 한 번에 발행
echo ""
echo "--- 9. POST /ns/ERP/event/batch-test/batch — 이벤트 3건 일괄 발행 ---"
curl -s -X POST "$BASE/ns/ERP/event/batch-test/batch" \
  -H "$ERP" -H "$CT" \
  -d '{
    "events": [
      {"action": "created", "order_id": "2024-010", "type": "일반주문"},
      {"action": "updated", "order_id": "2024-011", "type": "긴급주문"},
      {"action": "closed", "order_id": "2024-012", "type": "대량주문"}
    ]
  }' | python -m json.tool

echo ""
echo "--- 9-1. 일괄 발행 결과 확인 (XRANGE) ---"
curl -s "$BASE/ns/ERP/event/batch-test?last_id=0&count=10" \
  -H "$ERP" | python -m json.tool

# ----- 10. TTL 갱신 — touch -----
# PUT /ns/{ns}/event/{key}/touch: Stream의 TTL만 갱신
echo ""
echo "--- 10. PUT /ns/ERP/event/order-registered/touch — TTL을 7200초로 갱신 ---"
curl -s -X PUT "$BASE/ns/ERP/event/order-registered/touch" \
  -H "$ERP" -H "$CT" \
  -d '{"ttl": 7200}' | python -m json.tool

echo ""
echo "--- 10-1. PUT /ns/ERP/event/order-registered/touch — TTL 해제 (영구 보관) ---"
curl -s -X PUT "$BASE/ns/ERP/event/order-registered/touch" \
  -H "$ERP" -H "$CT" \
  -d '{"ttl": 0}' | python -m json.tool

echo ""
echo "--- 10-2. 존재하지 않는 Stream touch (404) ---"
curl -s -X PUT "$BASE/ns/ERP/event/nonexistent-stream/touch" \
  -H "$ERP" -H "$CT" \
  -d '{"ttl": 300}' | python -m json.tool

echo ""
echo "=== Event 테스트 완료 ==="
