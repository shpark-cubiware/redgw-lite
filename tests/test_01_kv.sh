#!/bin/bash
# =============================================================================
# RedGW String (KV) API 전체 기능 테스트
# =============================================================================
#
# Redis String 타입은 가장 기본적인 key-value 저장소입니다.
# RedGW에서는 시스템 상태, 설정값, 진행률, 카운터, 분산 락 등에 활용합니다.
#
# Redis 키 형식: {ns}:kv:{key}
#   예: HRM:kv:status → HRM 네임스페이스의 "status" 키
#
# 엔드포인트:
#   PUT    /ns/{ns}/kv/{key}        — 값 저장 (SET/SETEX)
#   GET    /ns/{ns}/kv/{key}        — 값 조회 (GET)
#   DELETE /ns/{ns}/kv/{key}        — 값 삭제 (DEL)
#   POST   /ns/{ns}/kv/{key}/incr   — 원자적 증가 (INCRBY)
#   PUT    /ns/{ns}/kv/{key}/nx     — 값 없을 때만 저장 (SETNX, 분산 락)
#   PUT    /ns/{ns}/kv/{key}/touch  — TTL 갱신 (EXPIRE/PERSIST)
#   GET    /ns/{ns}/kv/{key}/exists  — 키 존재 확인 (EXISTS)
#   POST   /ns/{ns}/kv/batch        — 배치 조회 (MGET)
#   PUT    /ns/{ns}/kv/batch         — 배치 저장 (MSET, pipeline)
#
# 테스트 시나리오:
#   1. 값 저장 (TTL 포함)
#   2. 값 조회
#   3. 교차 네임스페이스 읽기 (ERP → HRM)
#   4. 공유 설정값 저장 (shared 네임스페이스)
#   5. 카운터/시퀀스 채번 (INCRBY)
#   6. 분산 락 (SETNX) — 획득 성공/충돌
#   7. 값 삭제 (DEL)
#   8. 404 Not Found 처리
#   9. 키 존재 확인 (EXISTS)
#  10. 배치 저장 (MSET)
#  11. 배치 조회 (MGET)
#  12. 값 크기 검증 (VALUE_TOO_LARGE)
#  13. TTL 갱신 (touch)
# =============================================================================
BASE="${REDGW_BASE_URL:-http://localhost:3080}/api/v1"
CT="Content-Type: application/json"

# .env 로드 (API 키 등)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$SCRIPT_DIR/../.env" ] && source "$SCRIPT_DIR/../.env"

HRM="X-API-Key: ${REDGW_CLIENT_HRM_API_KEY:-redgw_ak_hrm_xxxxxxxxxxxxxxxx}"
ERP="X-API-Key: ${REDGW_CLIENT_ERP_API_KEY:-redgw_ak_erp_xxxxxxxxxxxxxxxx}"

echo "============================================"
echo " String (KV) API 테스트"
echo "============================================"

# ----- 1. 값 저장 (SET/SETEX) -----
# Redis SET 명령: 키에 문자열 값을 저장
# TTL 지정 시 SETEX(SET + EXPIRE)로 자동 만료
echo ""
echo "--- 1. PUT /ns/HRM/kv/status — HRM 시스템 상태 저장 (TTL 60초) ---"
curl -s -X PUT "$BASE/ns/HRM/kv/status" \
  -H "$HRM" -H "$CT" \
  -d '{"value": "running", "ttl": 60}' | python -m json.tool

# ----- 2. 값 조회 (GET) -----
# Redis GET 명령: 키의 값을 반환
# TTL이 만료되면 자동으로 사라집니다
echo ""
echo "--- 2. GET /ns/HRM/kv/status — HRM 상태 조회 ---"
curl -s "$BASE/ns/HRM/kv/status" \
  -H "$HRM" | python -m json.tool

# ----- 3. 교차 네임스페이스 읽기 -----
# 권한 매트릭스: ERP → HRM = read 허용
# ERP는 HRM의 데이터를 읽을 수 있지만 쓸 수는 없음
echo ""
echo "--- 3. GET /ns/HRM/kv/status — ERP가 HRM 상태 읽기 (교차 read 허용) ---"
curl -s "$BASE/ns/HRM/kv/status" \
  -H "$ERP" | python -m json.tool

# ----- 4. 공유 설정값 (shared 네임스페이스) -----
# shared 네임스페이스는 모든 시스템이 read/write 가능
# 시스템 공통 설정, 환경변수 공유에 활용
echo ""
echo "--- 4. PUT /ns/shared/kv/config:max-retry — 공유 설정값 저장 ---"
curl -s -X PUT "$BASE/ns/shared/kv/config:max-retry" \
  -H "$HRM" -H "$CT" \
  -d '{"value": "3"}' | python -m json.tool

# ----- 5. INCR — 카운터/시퀀스 -----
# Redis INCRBY: 원자적(atomic) 정수 증가
# - 키가 없으면 0에서 시작
# - 동시 호출해도 값이 유실되지 않음 (race condition 없음)
# 활용: 주문번호 채번, API 호출 횟수, 통계 카운터
echo ""
echo "--- 5. POST /ns/shared/kv/seq:order-number/incr — 시퀀스 채번 1 (→ 1) ---"
curl -s -X POST "$BASE/ns/shared/kv/seq:order-number/incr" \
  -H "$ERP" -H "$CT" \
  -d '{"delta": 1}' | python -m json.tool

echo ""
echo "--- 5-1. 한번 더 INCR (→ 2) ---"
curl -s -X POST "$BASE/ns/shared/kv/seq:order-number/incr" \
  -H "$ERP" -H "$CT" \
  -d '{"delta": 1}' | python -m json.tool

echo ""
echo "--- 5-2. delta=10으로 10씩 증가 (→ 12) ---"
curl -s -X POST "$BASE/ns/shared/kv/seq:order-number/incr" \
  -H "$ERP" -H "$CT" \
  -d '{"delta": 10}' | python -m json.tool

# ----- 6. SETNX — 분산 락 -----
# Redis SETNX(SET if Not eXists): 키가 없을 때만 저장
# 분산 환경에서 락(lock)을 구현하는 기본 패턴
# - 성공: 200 + acquired=true (락 획득)
# - 실패: 409 + KEY_EXISTS (이미 다른 시스템이 보유 중)
# TTL은 데드락 방지를 위한 자동 해제 타이머
echo ""
echo "--- 6. PUT /ns/shared/kv/lock:report-gen/nx — 분산 락 획득 (HRM) ---"
curl -s -X PUT "$BASE/ns/shared/kv/lock:report-gen/nx" \
  -H "$HRM" -H "$CT" \
  -d '{"value": "HRM", "ttl": 30}' | python -m json.tool

echo ""
echo "--- 6-1. PUT /ns/shared/kv/lock:report-gen/nx — ERP 락 시도 (→ 409 충돌) ---"
curl -s -X PUT "$BASE/ns/shared/kv/lock:report-gen/nx" \
  -H "$ERP" -H "$CT" \
  -d '{"value": "ERP", "ttl": 30}' | python -m json.tool

# ----- 7. 값 삭제 (DEL) -----
# Redis DEL: 키를 즉시 삭제
# 락 해제에도 사용: DELETE 후 다른 시스템이 SETNX 가능
echo ""
echo "--- 7. DELETE /ns/HRM/kv/status — 값 삭제 ---"
curl -s -X DELETE "$BASE/ns/HRM/kv/status" \
  -H "$HRM" | python -m json.tool

# ----- 8. 404 Not Found -----
# 존재하지 않는 키 조회 시 404 + KEY_NOT_FOUND 에러
echo ""
echo "--- 8. GET /ns/HRM/kv/nonexistent — 없는 키 조회 (→ 404) ---"
curl -s "$BASE/ns/HRM/kv/nonexistent" \
  -H "$HRM" | python -m json.tool

# ----- 9. 키 존재 확인 (EXISTS) -----
# Redis EXISTS 명령: 키의 존재 여부만 빠르게 확인
# 값을 가져오지 않아 GET보다 가벼움
echo ""
echo "--- 9. PUT 후 EXISTS 확인 (true) ---"
curl -s -X PUT "$BASE/ns/HRM/kv/exists-test" \
  -H "$HRM" -H "$CT" \
  -d '{"value": "temp", "ttl": 60}' > /dev/null

curl -s "$BASE/ns/HRM/kv/exists-test/exists" \
  -H "$HRM" | python -m json.tool

echo ""
echo "--- 9-1. 없는 키 EXISTS (false) ---"
curl -s "$BASE/ns/HRM/kv/no-such-key/exists" \
  -H "$HRM" | python -m json.tool

# ----- 10. 배치 저장 (MSET) -----
# 파이프라인으로 여러 키-값 쌍을 한 번에 저장
# TTL 지정 시 모든 키에 동일하게 적용
echo ""
echo "--- 10. PUT /ns/HRM/kv/batch — 배치 저장 (3개) ---"
curl -s -X PUT "$BASE/ns/HRM/kv/batch" \
  -H "$HRM" -H "$CT" \
  -d '{"items": {"sys:version": "1.0.0", "sys:uptime": "3600", "sys:mode": "production"}, "ttl": 300}' | python -m json.tool

# ----- 11. 배치 조회 (MGET) -----
# 여러 키를 한 번에 조회, 없는 키는 결과에서 제외
echo ""
echo "--- 11. POST /ns/HRM/kv/batch — 배치 조회 (존재 3 + 미존재 1) ---"
curl -s -X POST "$BASE/ns/HRM/kv/batch" \
  -H "$HRM" -H "$CT" \
  -d '{"keys": ["sys:version", "sys:uptime", "sys:mode", "sys:nonexistent"]}' | python -m json.tool

# ----- 12. 값 크기 검증 (VALUE_TOO_LARGE) -----
# max_value_size(기본 1MB) 초과 시 400 에러
# 큰 값은 임시 파일로 전달 (OS 인수 길이 제한 회피)
echo ""
echo "--- 12. PUT /ns/HRM/kv/big — 값 크기 초과 테스트 (→ 400) ---"
python -c "import json; print(json.dumps({'value': 'x'*1048577}))" > /tmp/redgw_big_kv.json
curl -s -X PUT "$BASE/ns/HRM/kv/big" \
  -H "$HRM" -H "$CT" \
  -d @/tmp/redgw_big_kv.json | python -m json.tool
rm -f /tmp/redgw_big_kv.json

# ----- 13. TTL 갱신 — touch -----
# PUT /ns/{ns}/kv/{key}/touch: 관리자 권한 없이 TTL만 갱신
# ttl > 0: EXPIRE로 TTL 재설정, ttl = 0: PERSIST로 영구 전환
echo ""
echo "--- 13. PUT /ns/HRM/kv/touch-test — touch용 데이터 준비 (TTL 60초) ---"
curl -s -X PUT "$BASE/ns/HRM/kv/touch-test" \
  -H "$HRM" -H "$CT" \
  -d '{"value": "session-data", "ttl": 60}' | python -m json.tool

echo ""
echo "--- 13-1. PUT /ns/HRM/kv/touch-test/touch — TTL을 300초로 갱신 ---"
curl -s -X PUT "$BASE/ns/HRM/kv/touch-test/touch" \
  -H "$HRM" -H "$CT" \
  -d '{"ttl": 300}' | python -m json.tool

echo ""
echo "--- 13-2. PUT /ns/HRM/kv/touch-test/touch — TTL 해제 (영구 보관) ---"
curl -s -X PUT "$BASE/ns/HRM/kv/touch-test/touch" \
  -H "$HRM" -H "$CT" \
  -d '{"ttl": 0}' | python -m json.tool

echo ""
echo "--- 13-3. 존재하지 않는 키 touch (404) ---"
curl -s -X PUT "$BASE/ns/HRM/kv/nonexistent-touch/touch" \
  -H "$HRM" -H "$CT" \
  -d '{"ttl": 300}' | python -m json.tool

echo ""
echo "=== KV 테스트 완료 ==="
