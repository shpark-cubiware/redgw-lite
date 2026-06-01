#!/bin/bash
# =============================================================================
# RedGW Sorted Set (Rank) API 전체 기능 테스트
# =============================================================================
#
# Redis Sorted Set은 각 멤버에 실수(float) 스코어를 부여하여
# 스코어 기준으로 자동 정렬된 집합입니다.
#
# Sorted Set 특성:
#   - O(log N) 삽입/삭제
#   - 스코어 기준 자동 정렬 (오름차순/내림차순)
#   - 스코어 범위 조회 (ZRANGEBYSCORE)
#   - 최소/최대 스코어 추출 (ZPOPMIN/ZPOPMAX)
#
# Redis 키 형식: {ns}:rank:{key}
#   예: ERP:rank:priority:orders → 주문 우선순위 랭킹
#
# 엔드포인트:
#   POST   /ns/{ns}/rank/{key}               — 멤버+스코어 추가 (ZADD) + TTL 지원
#   GET    /ns/{ns}/rank/{key}               — 범위 조회 (ZRANGE/ZREVRANGE)
#   GET    /ns/{ns}/rank/{key}/score/{member} — 스코어 조회 (ZSCORE)
#   GET    /ns/{ns}/rank/{key}/between        — 스코어 범위 조회 (ZRANGEBYSCORE)
#   DELETE /ns/{ns}/rank/{key}/{member}       — 멤버 제거 (ZREM)
#   POST   /ns/{ns}/rank/{key}/incr          — 스코어 증감 (ZINCRBY)
#   PUT    /ns/{ns}/rank/{key}/touch         — TTL 갱신 (EXPIRE/PERSIST)
#   GET    /ns/{ns}/rank/{key}/pop           — 최소/최대 추출 (ZPOPMIN/ZPOPMAX)
#   POST   /ns/{ns}/rank/batch              — 배치 조회 (Pipeline ZRANGE)
#   POST   /ns/{ns}/rank/{key}/batch        — 배치 추가 (ZADD 다건)
#
# 활용 예:
#   - 주문 우선순위 랭킹
#   - 인력 배정 스코어 (80점 이상 필터)
#   - 고객 가치 점수
#   - 타임스탬프 기반 작업 스케줄러
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
echo " Sorted Set (Rank) API 테스트"
echo "============================================"

# ----- 1. 멤버+스코어 추가 (ZADD) -----
echo ""
echo "--- 1. POST /ns/ERP/rank/priority:orders — 주문 우선순위 등록 ---"
curl -s -X POST "$BASE/ns/ERP/rank/priority:orders" \
  -H "$ERP" -H "$CT" \
  -d '{"member": "order-2024-001", "score": 10}' | python -m json.tool

curl -s -X POST "$BASE/ns/ERP/rank/priority:orders" \
  -H "$ERP" -H "$CT" \
  -d '{"member": "order-2024-002", "score": 5}' > /dev/null

curl -s -X POST "$BASE/ns/ERP/rank/priority:orders" \
  -H "$ERP" -H "$CT" \
  -d '{"member": "order-2024-003", "score": 8}' > /dev/null

curl -s -X POST "$BASE/ns/ERP/rank/priority:orders" \
  -H "$ERP" -H "$CT" \
  -d '{"member": "order-2024-004", "score": 3}' > /dev/null
echo "4건 등록 완료"

# ----- 2. 범위 조회 — 오름차순 (ZRANGE) -----
echo ""
echo "--- 2. GET /ns/ERP/rank/priority:orders?start=0&stop=-1 — 스코어 오름차순 ---"
curl -s "$BASE/ns/ERP/rank/priority:orders?start=0&stop=-1" \
  -H "$ERP" | python -m json.tool

# ----- 3. 범위 조회 — 내림차순 (ZREVRANGE) -----
echo ""
echo "--- 3. GET ... ?reverse=true — 긴급도 높은 순 (내림차순) ---"
curl -s "$BASE/ns/ERP/rank/priority:orders?start=0&stop=9&reverse=true" \
  -H "$ERP" | python -m json.tool

# ----- 4. 멤버 스코어 조회 (ZSCORE) -----
echo ""
echo "--- 4. GET /ns/ERP/rank/priority:orders/score/order-2024-001 — 스코어 조회 ---"
curl -s "$BASE/ns/ERP/rank/priority:orders/score/order-2024-001" \
  -H "$ERP" | python -m json.tool

# ----- 5. 스코어 범위 조회 (ZRANGEBYSCORE) -----
echo ""
echo "--- 5. 인력 배정 스코어 데이터 준비 ---"
for pid_score in "EMP-001:95" "EMP-002:72" "EMP-003:88" "EMP-004:60" "EMP-005:91"; do
  IFS=':' read -r pid score <<< "$pid_score"
  curl -s -X POST "$BASE/ns/HRM/rank/assign-scores:req-001" \
    -H "$HRM" -H "$CT" \
    -d "{\"member\": \"$pid\", \"score\": $score}" > /dev/null
done
echo "5건 등록 완료"

echo ""
echo "--- 5-1. GET /ns/HRM/rank/.../between?min=80&max=100 — 80점 이상 배정 ---"
curl -s "$BASE/ns/HRM/rank/assign-scores:req-001/between?min=80&max=100" \
  -H "$HRM" | python -m json.tool

# ----- 6. 스코어 증감 (ZINCRBY) -----
echo ""
echo "--- 6. POST /ns/CRM/rank/customer-value/incr — customer 가치 점수 설정 ---"
curl -s -X POST "$BASE/ns/CRM/rank/customer-value" \
  -H "$CRM" -H "$CT" \
  -d '{"member": "kim-001", "score": 50}' > /dev/null

echo ""
echo "--- 6-1. 가치 +5 증가 ---"
curl -s -X POST "$BASE/ns/CRM/rank/customer-value/incr" \
  -H "$CRM" -H "$CT" \
  -d '{"member": "kim-001", "delta": 5}' | python -m json.tool

echo ""
echo "--- 6-2. 가치 -3 감소 ---"
curl -s -X POST "$BASE/ns/CRM/rank/customer-value/incr" \
  -H "$CRM" -H "$CT" \
  -d '{"member": "kim-001", "delta": -3}' | python -m json.tool

# ----- 7. 최소/최대 스코어 추출 (ZPOPMIN/ZPOPMAX) -----
echo ""
echo "--- 7. 스케줄링 작업 등록 ---"
curl -s -X POST "$BASE/ns/shared/rank/schedule:jobs" \
  -H "$ERP" -H "$CT" \
  -d '{"member": "job-A", "score": 1707300000}' > /dev/null
curl -s -X POST "$BASE/ns/shared/rank/schedule:jobs" \
  -H "$ERP" -H "$CT" \
  -d '{"member": "job-B", "score": 1707200000}' > /dev/null
curl -s -X POST "$BASE/ns/shared/rank/schedule:jobs" \
  -H "$ERP" -H "$CT" \
  -d '{"member": "job-C", "score": 1707400000}' > /dev/null
echo "3건 등록 완료"

echo ""
echo "--- 7-1. GET .../pop?direction=min — 가장 빠른 실행 시간 추출 ---"
curl -s "$BASE/ns/shared/rank/schedule:jobs/pop?direction=min" \
  -H "$ERP" | python -m json.tool

echo ""
echo "--- 7-2. GET .../pop?direction=max — 가장 늦은 실행 시간 추출 ---"
curl -s "$BASE/ns/shared/rank/schedule:jobs/pop?direction=max" \
  -H "$ERP" | python -m json.tool

# ----- 8. 멤버 제거 (ZREM) -----
echo ""
echo "--- 8. DELETE /ns/ERP/rank/priority:orders/order-2024-004 — 주문 제거 ---"
curl -s -X DELETE "$BASE/ns/ERP/rank/priority:orders/order-2024-004" \
  -H "$ERP" | python -m json.tool

# ----- 9. TTL 지원 (ZADD + EXPIRE) -----
# 멤버+스코어 추가 시 TTL을 지정하면 Sorted Set 전체에 만료 시간이 설정됨
echo ""
echo "--- 9. POST /ns/ERP/rank/temp:score — TTL 120초와 함께 ZADD ---"
curl -s -X POST "$BASE/ns/ERP/rank/temp:score" \
  -H "$ERP" -H "$CT" \
  -d '{"member": "item-1", "score": 100, "ttl": 120}' | python -m json.tool

# ----- 10. 배치 추가 (ZADD 다건) -----
# 하나의 Sorted Set에 여러 멤버+스코어를 한 번에 추가
echo ""
echo "--- 10. POST /ns/ERP/rank/priority:alerts/batch — 배치 3건 추가 ---"
curl -s -X POST "$BASE/ns/ERP/rank/priority:alerts/batch" \
  -H "$ERP" -H "$CT" \
  -d '{
    "members": [
      {"member": "alert-001", "score": 90},
      {"member": "alert-002", "score": 50},
      {"member": "alert-003", "score": 70}
    ],
    "ttl": 3600
  }' | python -m json.tool

echo ""
echo "--- 10-1. 배치 추가 결과 확인 (내림차순) ---"
curl -s "$BASE/ns/ERP/rank/priority:alerts?start=0&stop=-1&reverse=true" \
  -H "$ERP" | python -m json.tool

# ----- 11. 배치 조회 (Pipeline ZRANGE) -----
# 여러 Sorted Set의 순위를 한 번에 조회
echo ""
echo "--- 11. POST /ns/ERP/rank/batch — 배치 조회 (2개 키, 내림차순) ---"
curl -s -X POST "$BASE/ns/ERP/rank/batch" \
  -H "$ERP" -H "$CT" \
  -d '{"keys": ["priority:orders", "priority:alerts"], "start": 0, "stop": -1, "reverse": true}' | python -m json.tool

# ----- 12. TTL 갱신 — touch -----
# PUT /ns/{ns}/rank/{key}/touch: Sorted Set의 TTL만 갱신
echo ""
echo "--- 12. PUT /ns/ERP/rank/priority:orders/touch — TTL을 3600초로 갱신 ---"
curl -s -X PUT "$BASE/ns/ERP/rank/priority:orders/touch" \
  -H "$ERP" -H "$CT" \
  -d '{"ttl": 3600}' | python -m json.tool

echo ""
echo "--- 12-1. PUT /ns/ERP/rank/priority:orders/touch — TTL 해제 (영구 보관) ---"
curl -s -X PUT "$BASE/ns/ERP/rank/priority:orders/touch" \
  -H "$ERP" -H "$CT" \
  -d '{"ttl": 0}' | python -m json.tool

echo ""
echo "--- 12-2. 존재하지 않는 Sorted Set touch (404) ---"
curl -s -X PUT "$BASE/ns/ERP/rank/nonexistent-rank/touch" \
  -H "$ERP" -H "$CT" \
  -d '{"ttl": 300}' | python -m json.tool

echo ""
echo "=== Rank 테스트 완료 ==="
