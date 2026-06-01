#!/bin/bash
# =============================================================================
# RedGW List (Queue) API 전체 기능 테스트
# =============================================================================
#
# Redis List 타입은 양방향 연결 리스트로 큐(Queue) 패턴에 적합합니다.
#
# 큐 패턴:
#   FIFO: RPUSH(right) → LPOP(left)  — 먼저 넣은 것이 먼저 나옴 (일반 처리)
#   LIFO: LPUSH(left)  → LPOP(left)  — 나중에 넣은 것이 먼저 나옴 (긴급 처리)
#
# Redis 키 형식: {ns}:q:{key}
#   예: HRM:q:hrm-requests → HRM 인력 배정 요청 큐
#
# 엔드포인트:
#   POST /ns/{ns}/queue/{key}        — 큐에 추가 (RPUSH/LPUSH) + TTL 지원
#   GET  /ns/{ns}/queue/{key}/pop    — 큐에서 추출 (LPOP/RPOP)
#   GET  /ns/{ns}/queue/{key}        — 범위 조회 (LRANGE, 꺼내지 않고 peek)
#   GET  /ns/{ns}/queue/{key}/len    — 큐 길이 (LLEN)
#   POST /ns/{ns}/queue/{key}/trim   — 최근 N건만 유지 (LTRIM)
#   PUT  /ns/{ns}/queue/{key}/touch  — TTL 갱신 (EXPIRE/PERSIST)
#   POST /ns/{ns}/queue/{key}/batch  — 배치 추가 (다수 값 일괄 PUSH)
#
# 활용 예:
#   - 인력 배정 요청 큐 (FIFO)
#   - 긴급 요청 우선 처리 (LIFO)
#   - API 호출 이력 로그 (push + trim)
#   - 거래처 지원 요청 큐
# =============================================================================
BASE="${REDGW_BASE_URL:-http://localhost:3080}/api/v1"
CT="Content-Type: application/json"

# .env 로드 (API 키 등)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$SCRIPT_DIR/../.env" ] && source "$SCRIPT_DIR/../.env"

HRM="X-API-Key: ${REDGW_CLIENT_HRM_API_KEY:-redgw_ak_hrm_xxxxxxxxxxxxxxxx}"
ERP="X-API-Key: ${REDGW_CLIENT_ERP_API_KEY:-redgw_ak_erp_xxxxxxxxxxxxxxxx}"

echo "============================================"
echo " List (Queue) API 테스트"
echo "============================================"

# ----- 1. 큐에 추가 — RPUSH (기본 방향) -----
echo ""
echo "--- 1. POST /ns/HRM/queue/hrm-requests — 인력 배정 요청 1 (RPUSH) ---"
curl -s -X POST "$BASE/ns/HRM/queue/hrm-requests" \
  -H "$HRM" -H "$CT" \
  -d '{"value": "{\"staff_id\":\"EMP-2024-001\",\"order_id\":\"2024-001\"}", "direction": "right"}' | python -m json.tool

echo ""
echo "--- 1-1. 인력 배정 요청 2 추가 ---"
curl -s -X POST "$BASE/ns/HRM/queue/hrm-requests" \
  -H "$HRM" -H "$CT" \
  -d '{"value": "{\"staff_id\":\"EMP-2024-002\",\"order_id\":\"2024-002\"}", "direction": "right"}' | python -m json.tool

echo ""
echo "--- 1-2. 인력 배정 요청 3 추가 ---"
curl -s -X POST "$BASE/ns/HRM/queue/hrm-requests" \
  -H "$HRM" -H "$CT" \
  -d '{"value": "{\"staff_id\":\"EMP-2024-003\",\"order_id\":\"2024-003\"}", "direction": "right"}' | python -m json.tool

# ----- 2. 큐에 추가 — LPUSH (왼쪽) -----
echo ""
echo "--- 2. POST (direction=left) — 긴급 요청을 앞에 추가 (LPUSH) ---"
curl -s -X POST "$BASE/ns/HRM/queue/hrm-requests" \
  -H "$HRM" -H "$CT" \
  -d '{"value": "{\"staff_id\":\"EMP-URGENT\",\"order_id\":\"URGENT-001\"}", "direction": "left"}' | python -m json.tool

# ----- 3. 큐 길이 조회 (LLEN) -----
echo ""
echo "--- 3. GET /ns/HRM/queue/hrm-requests/len — 대기 중인 요청 수 ---"
curl -s "$BASE/ns/HRM/queue/hrm-requests/len" \
  -H "$HRM" | python -m json.tool

# ----- 4. 범위 조회 — peek (LRANGE) -----
echo ""
echo "--- 4. GET /ns/HRM/queue/hrm-requests?start=0&stop=-1 — 전체 큐 내용 확인 ---"
curl -s "$BASE/ns/HRM/queue/hrm-requests?start=0&stop=-1" \
  -H "$HRM" | python -m json.tool

echo ""
echo "--- 4-1. 처음 2건만 조회 ---"
curl -s "$BASE/ns/HRM/queue/hrm-requests?start=0&stop=1" \
  -H "$HRM" | python -m json.tool

# ----- 5. 큐에서 추출 — LPOP (FIFO) -----
echo ""
echo "--- 5. GET /ns/HRM/queue/hrm-requests/pop?direction=left — FIFO 추출 ---"
curl -s "$BASE/ns/HRM/queue/hrm-requests/pop?direction=left" \
  -H "$HRM" | python -m json.tool

echo ""
echo "--- 5-1. 한 건 더 FIFO 추출 ---"
curl -s "$BASE/ns/HRM/queue/hrm-requests/pop?direction=left" \
  -H "$HRM" | python -m json.tool

# ----- 6. 큐에서 추출 — RPOP (LIFO) -----
echo ""
echo "--- 6. GET /ns/HRM/queue/hrm-requests/pop?direction=right — LIFO 추출 ---"
curl -s "$BASE/ns/HRM/queue/hrm-requests/pop?direction=right" \
  -H "$HRM" | python -m json.tool

# ----- 7. 이력 로그 + TRIM -----
echo ""
echo "--- 7. 이력 로그 10건 추가 ---"
for i in $(seq 1 10); do
  curl -s -X POST "$BASE/ns/ERP/queue/log:api-calls" \
    -H "$ERP" -H "$CT" \
    -d "{\"value\": \"call-$i\"}" > /dev/null
done
echo "10건 추가 완료"

echo ""
echo "--- 7-1. GET /ns/ERP/queue/log:api-calls/len — 현재 길이 ---"
curl -s "$BASE/ns/ERP/queue/log:api-calls/len" \
  -H "$ERP" | python -m json.tool

echo ""
echo "--- 7-2. POST /ns/ERP/queue/log:api-calls/trim — 최근 5건만 유지 ---"
curl -s -X POST "$BASE/ns/ERP/queue/log:api-calls/trim" \
  -H "$ERP" -H "$CT" \
  -d '{"keep": 5}' | python -m json.tool

echo ""
echo "--- 7-3. TRIM 후 내용 확인 ---"
curl -s "$BASE/ns/ERP/queue/log:api-calls?start=0&stop=-1" \
  -H "$ERP" | python -m json.tool

# ----- 8. 빈 큐에서 POP (404) -----
echo ""
echo "--- 8. GET /ns/HRM/queue/empty-queue/pop — 빈 큐 POP (404) ---"
curl -s "$BASE/ns/HRM/queue/empty-queue/pop?direction=left" \
  -H "$HRM" | python -m json.tool

# ----- 9. TTL 지원 (PUSH + EXPIRE) -----
# 큐에 값 추가 시 TTL을 지정하면 키 전체에 만료 시간이 설정됨
echo ""
echo "--- 9. POST /ns/ERP/queue/temp-tasks — TTL 120초와 함께 PUSH ---"
curl -s -X POST "$BASE/ns/ERP/queue/temp-tasks" \
  -H "$ERP" -H "$CT" \
  -d '{"value": "task-1", "ttl": 120}' | python -m json.tool

# ----- 10. 배치 추가 (Batch Push) -----
# 여러 값을 한 번에 큐에 추가 (Lua 스크립트 기반 원자적 처리)
echo ""
echo "--- 10. POST /ns/HRM/queue/batch-test/batch — 배치 3건 추가 ---"
curl -s -X POST "$BASE/ns/HRM/queue/batch-test/batch" \
  -H "$HRM" -H "$CT" \
  -d '{"values": ["item-1", "item-2", "item-3"], "direction": "right", "ttl": 300}' | python -m json.tool

echo ""
echo "--- 10-1. 배치 추가 후 큐 내용 확인 ---"
curl -s "$BASE/ns/HRM/queue/batch-test?start=0&stop=-1" \
  -H "$HRM" | python -m json.tool

# ----- 11. 값 크기 검증 (VALUE_TOO_LARGE) -----
echo ""
echo "--- 11. POST /ns/HRM/queue/big — 값 크기 초과 (→ 400) ---"
python -c "import json; print(json.dumps({'value': 'x'*1048577}))" > /tmp/redgw_big_queue.json
curl -s -X POST "$BASE/ns/HRM/queue/big" \
  -H "$HRM" -H "$CT" \
  -d @/tmp/redgw_big_queue.json | python -m json.tool
rm -f /tmp/redgw_big_queue.json

# ----- 12. TTL 갱신 — touch -----
# PUT /ns/{ns}/queue/{key}/touch: 큐의 TTL만 갱신
echo ""
echo "--- 12. POST /ns/ERP/queue/touch-test — touch용 큐 데이터 준비 ---"
curl -s -X POST "$BASE/ns/ERP/queue/touch-test" \
  -H "$ERP" -H "$CT" \
  -d '{"value": "item-1", "ttl": 60}' > /dev/null
echo "데이터 준비 완료"

echo ""
echo "--- 12-1. PUT /ns/ERP/queue/touch-test/touch — TTL을 600초로 갱신 ---"
curl -s -X PUT "$BASE/ns/ERP/queue/touch-test/touch" \
  -H "$ERP" -H "$CT" \
  -d '{"ttl": 600}' | python -m json.tool

echo ""
echo "--- 12-2. PUT /ns/ERP/queue/touch-test/touch — TTL 해제 (영구 보관) ---"
curl -s -X PUT "$BASE/ns/ERP/queue/touch-test/touch" \
  -H "$ERP" -H "$CT" \
  -d '{"ttl": 0}' | python -m json.tool

echo ""
echo "--- 12-3. 존재하지 않는 큐 touch (404) ---"
curl -s -X PUT "$BASE/ns/ERP/queue/nonexistent-queue/touch" \
  -H "$ERP" -H "$CT" \
  -d '{"ttl": 300}' | python -m json.tool

echo ""
echo "=== Queue 테스트 완료 ==="
