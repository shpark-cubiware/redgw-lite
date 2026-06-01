#!/bin/bash
# =============================================================================
# RedGW Pub/Sub API 테스트
# =============================================================================
#
# Redis Pub/Sub + WebSocket 기반 실시간 메시징입니다.
#
# Pub/Sub 특성:
#   - Fire-and-forget: 메시지가 Redis에 저장되지 않음
#   - 구독자가 없으면 메시지 유실 (receivers=0)
#   - 실시간 알림, 브로드캐스트에 적합
#   - 영속성이 필요하면 Stream(Event) API 사용
#
# 엔드포인트:
#   POST      /ns/{ns}/publish/{channel}      — 채널에 메시지 발행
#   WEBSOCKET /ws/{ns}/subscribe/{channel}    — WebSocket 채널 구독
#
# 참고:
#   - WebSocket 구독은 curl로 테스트 불가 (websocat 또는 Python 필요)
#   - 아래 예시 코드 참조
# =============================================================================
BASE="${REDGW_BASE_URL:-http://localhost:3080}/api/v1"
CT="Content-Type: application/json"

# .env 로드 (API 키 등)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$SCRIPT_DIR/../.env" ] && source "$SCRIPT_DIR/../.env"

ERP_KEY="${REDGW_CLIENT_ERP_API_KEY:-redgw_ak_erp_xxxxxxxxxxxxxxxx}"
ERP="X-API-Key: $ERP_KEY"

echo "============================================"
echo " Pub/Sub API 테스트"
echo "============================================"

# ----- 1. 채널에 메시지 발행 -----
echo ""
echo "--- 1. POST /ns/ERP/publish/order-updates — 주문 업데이트 메시지 발행 ---"
curl -s -X POST "$BASE/ns/ERP/publish/order-updates" \
  -H "$ERP" -H "$CT" \
  -d '{"message": "order:2024-001 상태가 처리중에서 완료로 변경됨"}' | python -m json.tool

echo ""
echo "--- 1-1. 긴급 알림 발행 ---"
curl -s -X POST "$BASE/ns/shared/publish/emergency-alert" \
  -H "$ERP" -H "$CT" \
  -d '{"message": "긴급: 서울강남 대량주문 발생"}' | python -m json.tool

# ----- 2. WebSocket 구독 안내 -----
echo ""
echo "--- 2. WebSocket 구독 (별도 터미널에서 실행) ---"
echo ""
echo "  WebSocket 구독은 대화형이므로 별도 터미널에서 다음 명령을 실행하세요:"
echo ""
echo "  # 방법 1: websocat 사용 (설치 필요)"
echo "  websocat 'ws://localhost:3080/ws/ERP/subscribe/order-updates?api_key=$ERP_KEY'"
echo ""
echo "  # 방법 2: python 사용"
echo "  python -c \""
echo "import asyncio, websockets"
echo "async def listen():"
echo "    uri = 'ws://localhost:3080/ws/ERP/subscribe/order-updates?api_key=$ERP_KEY'"
echo "    async with websockets.connect(uri) as ws:"
echo "        print('구독 시작...')"
echo "        async for msg in ws:"
echo "            print('수신:', msg)"
echo "asyncio.run(listen())"
echo "\""
echo ""
echo "  구독 후 다른 터미널에서 발행하면 실시간 수신됩니다:"
echo "  curl -X POST http://localhost:3080/api/v1/ns/ERP/publish/order-updates \\"
echo "    -H 'X-API-Key: $ERP_KEY' \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"message\": \"실시간 테스트 메시지\"}'"

# ----- 3. 값 크기 검증 (VALUE_TOO_LARGE) -----
# message가 max_value_size를 초과하면 400 에러
echo ""
echo "--- 3. POST /ns/ERP/publish/big — 메시지 크기 초과 (→ 400) ---"
python -c "import json; print(json.dumps({'message': 'x'*1048577}))" > /tmp/redgw_big_pub.json
curl -s -X POST "$BASE/ns/ERP/publish/big" \
  -H "$ERP" -H "$CT" \
  -d @/tmp/redgw_big_pub.json | python -m json.tool
rm -f /tmp/redgw_big_pub.json

echo ""
echo "=== Pub/Sub 테스트 완료 ==="
