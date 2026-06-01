"""
=============================================================================
Pub/Sub API 테스트 (test_pubsub.py)
=============================================================================

Redis Pub/Sub + WebSocket 기반 실시간 메시징 API를 테스트합니다.

엔드포인트:
  POST      /ns/{ns}/publish/{channel}      — 채널에 메시지 발행
  WEBSOCKET /ws/{ns}/subscribe/{channel}    — WebSocket 채널 구독

Pub/Sub 특성:
  - Fire-and-forget: 메시지가 Redis에 저장되지 않음
  - 구독자가 없으면 메시지가 유실됨
  - 실시간 알림, 채팅, 이벤트 브로드캐스트에 적합
  - 영속성이 필요하면 Stream(Event) API를 사용

참고:
  - WebSocket 구독은 uvicorn 실서버(ws_server 픽스처) + websockets 클라이언트로 자동 검증
  - publish API는 인프로세스 httpx 클라이언트로 검증

테스트 시나리오:
  - 메시지 발행 (receivers 수 반환)
  - 구독자 없는 경우 receivers = 0
  - 교차 네임스페이스 write 거부
=============================================================================
"""

import asyncio

import pytest
import pytest_asyncio
from httpx import AsyncClient

from tests.conftest import ADMIN_KEY, HRM_KEY, INVALID_KEY, ERP_KEY, CRM_KEY


class TestPublish:
    """Pub/Sub 메시지 발행 테스트"""

    async def test_publish_no_subscribers(self, client: AsyncClient):
        """
        구독자 없는 상태에서 메시지 발행.

        Redis PUBLISH는 메시지를 수신한 구독자 수를 반환합니다.
        구독자가 없으면 receivers=0이지만 에러는 아닙니다.
        """
        resp = await client.post(
            "/api/v1/ns/ERP/publish/order-alerts",
            headers={"X-API-Key": ERP_KEY},
            json={"message": '{"type":"new_order","id":"2024-001"}'},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["data"]["channel"] == "ERP:order-alerts"
        assert data["data"]["receivers"] == 0  # 구독자 없음
        assert data["meta"]["type"] == "pubsub"

    async def test_publish_response_structure(self, client: AsyncClient):
        """응답 구조 검증 — channel, receivers, meta"""
        resp = await client.post(
            "/api/v1/ns/HRM/publish/assign-notifications",
            headers={"X-API-Key": HRM_KEY},
            json={"message": "assign completed"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["channel"] == "HRM:assign-notifications"
        assert "receivers" in body["data"]
        assert body["meta"]["ns"] == "HRM"
        assert body["meta"]["key"] == "assign-notifications"

    async def test_publish_cross_ns_write_denied(self, client: AsyncClient):
        """
        교차 네임스페이스 write 거부.

        HRM → ERP publish는 write 권한이 필요하지만,
        HRM는 ERP에 read만 허용이므로 403.
        """
        resp = await client.post(
            "/api/v1/ns/ERP/publish/alerts",
            headers={"X-API-Key": HRM_KEY},
            json={"message": "should be denied"},
        )
        assert resp.status_code == 403

    async def test_publish_own_namespace(self, client: AsyncClient):
        """자기 네임스페이스에 publish → 허용"""
        resp = await client.post(
            "/api/v1/ns/ERP/publish/internal-alerts",
            headers={"X-API-Key": ERP_KEY},
            json={"message": "internal notification"},
        )
        assert resp.status_code == 200

    async def test_publish_shared_namespace(self, client: AsyncClient):
        """shared 네임스페이스에 publish — 모든 시스템 허용"""
        resp = await client.post(
            "/api/v1/ns/shared/publish/broadcast",
            headers={"X-API-Key": CRM_KEY},
            json={"message": '{"event":"system_maintenance","time":"03:00"}'},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["channel"] == "shared:broadcast"

    async def test_admin_publish_any_namespace(self, client: AsyncClient):
        """admin은 모든 네임스페이스에 publish 가능"""
        resp = await client.post(
            "/api/v1/ns/HRM/publish/admin-broadcast",
            headers={"X-API-Key": ADMIN_KEY},
            json={"message": "admin message"},
        )
        assert resp.status_code == 200

    async def test_publish_message_too_large(self, client: AsyncClient):
        """메시지 크기가 max_value_size 초과 시 400 오류"""
        large_msg = "x" * (1048576 + 1)  # 1MB + 1byte
        resp = await client.post(
            "/api/v1/ns/ERP/publish/size-test",
            headers={"X-API-Key": ERP_KEY},
            json={"message": large_msg},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "VALUE_TOO_LARGE"


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def ws_server(app):
    """WebSocket 테스트용 실서버 (세션 1회 기동).

    `websockets` 클라이언트는 실제 TCP 소켓이 필요하므로 uvicorn을 백그라운드
    스레드에서 띄운다. `lifespan="off"`로 앱 lifespan(전역 Redis 매니저 재초기화 등)을
    막아 conftest가 세션 루프에서 만든 Redis 매니저를 보존한다.
    WS 핸들러는 자체 Redis 연결(aioredis.from_url)을 server 루프에서 생성하므로
    전역 매니저에 의존하지 않아 루프 충돌이 없다.
    """
    import threading

    import uvicorn

    # ws="websockets-sansio": uvicorn 0.44+의 모던 WS 구현. 기본('websockets')은
    # deprecated된 websockets.legacy를 import해 DeprecationWarning을 낸다.
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, lifespan="off",
        ws="websockets-sansio", log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # 기동 + 포트 바인딩 대기 (최대 ~5초)
    for _ in range(100):
        if server.started and server.servers:
            break
        await asyncio.sleep(0.05)
    else:
        server.should_exit = True
        raise RuntimeError("WS 테스트 서버 기동 실패")
    port = server.servers[0].sockets[0].getsockname()[1]

    yield f"ws://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=5)


class TestWebSocketSubscribe:
    """WebSocket 구독 테스트.

    uvicorn 실서버(ws_server 픽스처) + `websockets` 클라이언트로 실제 핸드셰이크를
    검증한다. 구독 핸들러는 자체 Redis 연결을 쓰고, publish는 conftest Redis
    클라이언트(pytest 루프)로 보내 루프 충돌을 회피한다.
    """

    async def test_subscribe_and_receive(self, ws_server):
        """WebSocket 구독 후 메시지 수신."""
        import json

        from websockets.asyncio.client import connect

        from app.redis_client import get_redis_manager

        uri = f"{ws_server}/api/v1/ws/ERP/subscribe/test-ch?api_key={ERP_KEY}"
        async with connect(uri) as ws:
            # 핸들러가 Redis SUBSCRIBE를 마칠 때까지 대기 후 publish
            await asyncio.sleep(0.5)
            r = get_redis_manager().get_client()
            receivers = await r.publish("ERP:test-ch", "hello-ws")
            assert receivers >= 1  # WebSocket 구독자가 있어야 함

            raw = await asyncio.wait_for(ws.recv(), timeout=5)

        data = json.loads(raw)
        assert data["channel"] == "ERP:test-ch"
        assert data["data"] == "hello-ws"

    async def test_subscribe_no_api_key(self, ws_server):
        """API 키 없이 WebSocket 연결 → 4001 거부 (핸드셰이크 실패)"""
        import websockets
        from websockets.asyncio.client import connect

        uri = f"{ws_server}/api/v1/ws/ERP/subscribe/test-ch"
        with pytest.raises(websockets.exceptions.WebSocketException):
            async with connect(uri) as ws:
                await ws.recv()  # 거부 시 connect 또는 recv에서 예외

    async def test_subscribe_invalid_api_key(self, ws_server):
        """잘못된 API 키로 WebSocket 연결 → 4001 거부"""
        import websockets
        from websockets.asyncio.client import connect

        uri = f"{ws_server}/api/v1/ws/ERP/subscribe/test-ch?api_key={INVALID_KEY}"
        with pytest.raises(websockets.exceptions.WebSocketException):
            async with connect(uri) as ws:
                await ws.recv()

    async def test_subscribe_namespace_denied(self, ws_server):
        """권한 없는 네임스페이스 구독 → 4003 거부 (CRM는 HRM 권한 없음)"""
        import websockets
        from websockets.asyncio.client import connect

        uri = f"{ws_server}/api/v1/ws/HRM/subscribe/test-ch?api_key={CRM_KEY}"
        with pytest.raises(websockets.exceptions.WebSocketException):
            async with connect(uri) as ws:
                await ws.recv()

    async def test_subscribe_disconnect_releases_slot(self, ws_server):
        """유휴 채널에서 클라이언트가 끊긴 뒤 재구독이 정상적으로 성공한다.

        수정 전: while True: get_message()만 호출해 유휴 채널에서 끊김을 감지 못해
        _MAX_WS_CONNECTIONS 슬롯이 반환되지 않음 → 100개 연결 후 4029 거부.
        수정 후: _watch_disconnect task가 ws.receive()로 끊김을 즉시 감지,
        finally에서 슬롯을 반환해 재구독이 가능해야 한다.

        _ws_connection_count는 서버 스레드(별도 이벤트 루프)의 변수라 직접 임포트로
        검증이 불가하므, 연결 종료 후 동일 채널 재구독이 성공하는지로 간접 검증한다.
        """
        from websockets.asyncio.client import connect

        uri = f"{ws_server}/api/v1/ws/ERP/subscribe/idle-ch?api_key={ERP_KEY}"

        # 1차 구독 → 채널에 메시지 없이 연결 유지 → close
        async with connect(uri):
            await asyncio.sleep(0.3)

        # 슬롯이 반환될 때까지 대기 (서버가 watch_disconnect를 처리하는 시간 허용)
        await asyncio.sleep(0.5)

        # 2차 구독이 정상 성공하면 슬롯이 반환된 것을 간접 확인
        async with connect(uri) as ws2:
            await asyncio.sleep(0.1)
            # 연결이 살아있어야 함 (서버가 4029로 거부하지 않았다)
            assert ws2.state.name != "CLOSED"
