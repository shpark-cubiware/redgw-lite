#!/usr/bin/env bash
# =============================================================================
# RedGW curl 테스트 전체 실행
# 사용법: bash scripts/curl_tests.sh
# =============================================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEST_DIR="$PROJECT_DIR/tests"
export REDGW_BASE_URL="${REDGW_BASE_URL:-http://localhost:3080}"
BASE_URL="$REDGW_BASE_URL"

# .env는 각 자식 테스트(tests/test_0*.sh)가 직접 source한다 — 여기선 사용하지 않으므로
# 로드 안 함(rule 02: .env의 $ 특수문자가 현재 셸에서 source 시 변수 오염). 자식은 서브셸로 격리.

echo "============================================================"
echo " RedGW curl 테스트 전체 실행"
echo " 대상: $BASE_URL"
echo "============================================================"

# 헬스체크로 서비스 상태 확인
echo ""
echo "서비스 상태 확인 중..."
HEALTH=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/health")
if [ "$HEALTH" != "200" ]; then
  echo "ERROR: RedGW 서비스가 응답하지 않습니다 (HTTP $HEALTH)"
  echo "       docker compose up -d 로 서비스를 먼저 기동하세요."
  exit 1
fi
echo "서비스 정상 (HTTP 200)"

# 테스트 실행 전 Redis 데이터 초기화 (운영 DB 0)
echo ""
echo "Redis 데이터 초기화 (DB 0)..."
docker compose exec -T redis redis-cli -n 0 FLUSHDB > /dev/null 2>&1
echo "초기화 완료"

SCRIPTS=(
  "test_00_health.sh"
  "test_01_kv.sh"
  "test_02_map.sh"
  "test_03_queue.sh"
  "test_04_group.sh"
  "test_05_rank.sh"
  "test_06_event.sh"
  "test_07_pubsub.sh"
  "test_08_admin.sh"
  "test_09_auth.sh"
  "test_10_e2e_scenario.sh"
)

PASSED=0
FAILED=0
FAILED_SCRIPTS=()

set +e  # 개별 테스트 실패가 전체 스크립트를 중단하지 않도록
for script in "${SCRIPTS[@]}"; do
  echo ""
  echo "============================================================"
  echo " 실행: $script"
  echo "============================================================"
  if bash "$TEST_DIR/$script"; then
    PASSED=$((PASSED + 1))
  else
    FAILED=$((FAILED + 1))
    FAILED_SCRIPTS+=("$script")
  fi
  echo ""
done
set -e

echo ""
echo "============================================================"
echo " 전체 curl 테스트 완료"
echo " 결과: ${PASSED} 통과 / ${FAILED} 실패 (총 ${#SCRIPTS[@]})"
if [ ${FAILED} -gt 0 ]; then
  echo " 실패: ${FAILED_SCRIPTS[*]}"
fi
echo "============================================================"

[ "$FAILED" -eq 0 ] || exit 1
