#!/bin/bash
# =============================================================================
# RedGW End-to-End 시나리오 테스트
# =============================================================================
#
# 실제 업무 흐름을 시뮬레이션하는 통합 테스트입니다.
# 모든 Redis 데이터 타입과 교차 네임스페이스 접근을 종합 검증합니다.
#
# 시나리오: 신규 수주 등록 → 담당 인력 배정 → 고객 가치 분석
#
#   1. [ERP] 주문 등록 (Map) + 이벤트 발행 (Stream)
#   2. [HRM] 이벤트 수신 → 담당 인력 배정 시작
#   3. [HRM] 배정 결과 저장 (Map + Rank)
#   4. [HRM] 처리 완료 이벤트 발행 → ERP 수신
#   5. [CRM] 분석 결과 저장 (Map + Set + Rank)
#   6. [공유] 알림 큐 (Queue), 공통 설정 (KV)
#
# 사용하는 Redis 타입: String, Hash, List, Set, Sorted Set, Stream
# 사용하는 API: KV, Map, Queue, Group, Rank, Event
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
echo " End-to-End 시나리오"
echo " ERP 주문 등록 → HRM 인력 배정 → CRM 고객 분석"
echo "============================================"

# ===== STEP 1: ERP — 주문 등록 =====
echo ""
echo "===== STEP 1: ERP — 주문 정보 저장 (Hash) ====="
curl -s -X PUT "$BASE/ns/ERP/map/order:2024-001" \
  -H "$ERP" -H "$CT" \
  -d '{
    "fields": {
      "type": "정기주문",
      "status": "처리중",
      "region": "서울강남",
      "manager": "kim",
      "customer": "cust-001",
      "product": "P-2024-001"
    },
    "ttl": 86400
  }' | python -m json.tool

# ===== STEP 2: ERP — 주문 이벤트 발행 =====
echo ""
echo "===== STEP 2: ERP — 주문 등록 이벤트 발행 (Stream) ====="
curl -s -X POST "$BASE/ns/ERP/event/order-registered" \
  -H "$ERP" -H "$CT" \
  -d '{"data": {"order_id": "2024-001", "type": "정기주문", "region": "서울강남"}}' | python -m json.tool

# ===== STEP 3: ERP — 주문 우선순위 등록 =====
echo ""
echo "===== STEP 3: ERP — 주문 우선순위 등록 (Sorted Set) ====="
curl -s -X POST "$BASE/ns/ERP/rank/priority:orders" \
  -H "$ERP" -H "$CT" \
  -d '{"member": "order-2024-001", "score": 10}' | python -m json.tool

# ===== STEP 4: HRM — 인력 배정 요청 큐 등록 =====
echo ""
echo "===== STEP 4: HRM — 인력 배정 요청 큐에 추가 (List) ====="
curl -s -X POST "$BASE/ns/HRM/queue/hrm-requests" \
  -H "$HRM" -H "$CT" \
  -d '{"value": "{\"req_id\":\"REQ-2024-001\",\"order_id\":\"2024-001\",\"role\":\"PM\"}", "direction": "right"}' | python -m json.tool

# ===== STEP 5: HRM — 큐에서 요청 꺼내기 =====
echo ""
echo "===== STEP 5: HRM — 배정 요청 꺼내기 (FIFO pop) ====="
curl -s "$BASE/ns/HRM/queue/hrm-requests/pop?direction=left" \
  -H "$HRM" | python -m json.tool

# ===== STEP 6: HRM — 인력 배정 결과 저장 =====
echo ""
echo "===== STEP 6: HRM — 배정 결과 저장 (Hash) ====="
curl -s -X PUT "$BASE/ns/HRM/map/assign:req-001" \
  -H "$HRM" -H "$CT" \
  -d '{"fields": {"score": "95", "staff_id": "emp-001", "req_id": "REQ-2024-001", "status": "assigned"}}' | python -m json.tool

# ===== STEP 7: HRM — 배정 적합도 랭킹에 등록 =====
echo ""
echo "===== STEP 7: HRM — 배정 적합도 랭킹 등록 (Sorted Set) ====="
curl -s -X POST "$BASE/ns/HRM/rank/assign-scores:req-001" \
  -H "$HRM" -H "$CT" \
  -d '{"member": "emp-001", "score": 95}' | python -m json.tool

# ===== STEP 8: HRM — 처리 완료 기록 (중복 방지) =====
echo ""
echo "===== STEP 8: HRM — 처리된 req_id 등록 (Set, 멱등성) ====="
curl -s -X POST "$BASE/ns/HRM/group/processed:batch-001" \
  -H "$HRM" -H "$CT" \
  -d '{"members": ["REQ-2024-001"]}' | python -m json.tool

# ===== STEP 9: HRM — 배정 완료 이벤트 발행 =====
echo ""
echo "===== STEP 9: HRM — 배정 완료 이벤트 발행 (Stream) ====="
curl -s -X POST "$BASE/ns/HRM/event/assign-complete" \
  -H "$HRM" -H "$CT" \
  -d '{"data": {"req_id": "REQ-2024-001", "score": "95", "staff_id": "emp-001", "order_id": "2024-001"}}' | python -m json.tool

# ===== STEP 10: ERP — HRM 배정 결과 조회 =====
echo ""
echo "===== STEP 10: ERP — HRM 배정 결과 조회 (교차 네임스페이스 read) ====="
curl -s "$BASE/ns/HRM/map/assign:req-001" \
  -H "$ERP" | python -m json.tool

# ===== STEP 11: ERP — 주문 상태 업데이트 =====
echo ""
echo "===== STEP 11: ERP — 주문 상태를 '배정완료'로 변경 ====="
curl -s -X PUT "$BASE/ns/ERP/map/order:2024-001/status" \
  -H "$ERP" -H "$CT" \
  -d '{"value": "배정완료"}' | python -m json.tool

# ===== STEP 12: CRM — Consumer Group으로 주문 이벤트 구독 =====
echo ""
echo "===== STEP 12: CRM — Consumer Group 생성 ====="
curl -s -X POST "$BASE/ns/ERP/event/order-registered/group" \
  -H "$CRM" -H "$CT" \
  -d '{"group": "crm-group"}' | python -m json.tool

echo ""
echo "===== STEP 12-1: CRM — 주문 이벤트 소비 ====="
curl -s "$BASE/ns/ERP/event/order-registered/group/crm-group/read?consumer=crm-worker-1&count=10" \
  -H "$CRM" | python -m json.tool

# ===== STEP 13: CRM — 고객 가치 분석 =====
echo ""
echo "===== STEP 13: CRM — 고객 가치 등급 등록 (Sorted Set) ====="
curl -s -X POST "$BASE/ns/CRM/rank/customer-value" \
  -H "$CRM" -H "$CT" \
  -d '{"member": "cust-001", "score": 85}' | python -m json.tool

# ===== STEP 14: 최종 확인 =====
echo ""
echo "===== STEP 14: 최종 확인 — 주문 정보 전체 조회 ====="
curl -s "$BASE/ns/ERP/map/order:2024-001" \
  -H "$ERP" | python -m json.tool

echo ""
echo "===== 배정 요청 중복 처리 여부 확인 ====="
curl -s "$BASE/ns/HRM/group/processed:batch-001/contains/REQ-2024-001" \
  -H "$HRM" | python -m json.tool

echo ""
echo "===== 주문 우선순위 현황 ====="
curl -s "$BASE/ns/ERP/rank/priority:orders?start=0&stop=9&reverse=true" \
  -H "$ERP" | python -m json.tool

echo ""
echo "============================================"
echo " E2E 시나리오 완료"
echo "============================================"
