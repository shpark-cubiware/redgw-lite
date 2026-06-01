"""Pub/Sub + WebSocket API"""

from __future__ import annotations

import asyncio
import logging

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect

from app.auth.api_key import resolve_client
from app.auth.namespace_guard import NamespaceGuard, require_write
from app.config import get_settings
from app.dependencies import get_client, get_redis
from app.schemas.common import ClientInfo
from app.schemas.pubsub import PublishRequest
from app.utils.key_builder import validate_key, validate_ns
from app.utils.response import ok
from app.utils.validation import check_value_size

logger = logging.getLogger("redgw.pubsub")

# WebSocket 동시 연결 제한 — 각 연결이 별도 Redis 커넥션을 생성하므로
# Redis maxclients 초과 및 메모리 과다 사용 방지
_MAX_WS_CONNECTIONS = 100
_ws_connection_count = 0
_ws_lock = asyncio.Lock()

router = APIRouter()


@router.post("/ns/{ns}/publish/{channel}", summary="채널에 메시지 발행")
async def publish_message(
    ns: str,
    channel: str,
    body: PublishRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    validate_ns(ns)
    validate_key(channel)

    check_value_size(body.message, label="Message")

    full_channel = f"{ns}:{channel}"
    receivers = await r.publish(full_channel, body.message)
    return ok({"channel": full_channel, "receivers": receivers}, ns=ns, key=channel, type="pubsub")


@router.websocket("/ws/{ns}/subscribe/{channel}")
async def websocket_subscribe(
    ws: WebSocket,
    ns: str,
    channel: str,
    api_key: str | None = Query(None, alias="api_key"),
) -> None:
    """WebSocket 채널 구독"""
    # API Key 인증
    if not api_key:
        await ws.close(code=4001, reason="API key required")
        return

    client = resolve_client(api_key)
    if client is None:
        await ws.close(code=4001, reason="Invalid API key")
        return

    if not NamespaceGuard.check_access(client, ns, "read"):
        await ws.close(code=4003, reason=f"Access to namespace '{ns}' denied")
        return

    try:
        validate_ns(ns)
        validate_key(channel)
    except HTTPException:
        await ws.close(code=4002, reason="Invalid namespace or channel name")
        return

    # 동시 연결 수 제한 체크 — Lock으로 check-then-act 원자성 보장
    global _ws_connection_count
    async with _ws_lock:
        if _ws_connection_count >= _MAX_WS_CONNECTIONS:
            await ws.close(
                code=4029,
                reason=f"Too many WebSocket connections (max={_MAX_WS_CONNECTIONS})",
            )
            logger.warning(
                "WebSocket rejected: max connections reached (%d)",
                _MAX_WS_CONNECTIONS,
            )
            return
        _ws_connection_count += 1

    full_channel = f"{ns}:{channel}"
    settings = get_settings()
    # 구독용 별도 Redis 연결
    sub_client: aioredis.Redis | None = None
    pubsub = None

    try:
        await ws.accept()
        sub_client = aioredis.from_url(
            settings.redis.url,
            decode_responses=True,
            socket_timeout=settings.redis.socket_timeout,
            retry_on_timeout=settings.redis.retry_on_timeout,
        )
        pubsub = sub_client.pubsub()
        await pubsub.subscribe(full_channel)
        logger.info(
            "Client '%s' subscribed to '%s' (connections: %d/%d)",
            client.client_id, full_channel, _ws_connection_count, _MAX_WS_CONNECTIONS,
        )

        # 메시지 펌프와 클라이언트 끊김 감시를 동시 실행.
        # 단일 루프(get_message only)에서는 유휴 채널에서 클라이언트가 끊겨도
        # ws.send_json이 호출되지 않아 WebSocketDisconnect를 감지하지 못한다 →
        # 좀비 연결이 _ws_connection_count 슬롯과 Redis 연결을 영구 점유해
        # _MAX_WS_CONNECTIONS(100) 포화 시 신규 구독 4029 거부 유발.
        async def _pump() -> None:
            while True:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and msg["type"] == "message":
                    await ws.send_json({"channel": full_channel, "data": msg["data"]})

        async def _watch_disconnect() -> None:
            # 클라이언트가 보내는 프레임(혹은 끊김)을 읽어 WebSocketDisconnect를 표면화.
            # 저수준 ws.receive()는 끊김 시 예외 대신 {"type":"websocket.disconnect"} dict를
            # '반환'한다. 이를 직접 WebSocketDisconnect로 올려야 한다 — 안 그러면 다음
            # receive() 호출이 RuntimeError를 던져 except Exception(ERROR 로그)로 잘못 분류된다.
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    raise WebSocketDisconnect(msg.get("code", 1000))

        pump_task = asyncio.create_task(_pump())
        watch_task = asyncio.create_task(_watch_disconnect())
        # 강참조 유지 — create_task 결과를 즉시 버리면 실행 중 GC될 수 있다.
        try:
            await asyncio.wait({pump_task, watch_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (pump_task, watch_task):
                if not t.done():
                    t.cancel()
            await asyncio.gather(pump_task, watch_task, return_exceptions=True)
        # 먼저 종료된 task의 예외를 재발생시켜 기존 except 분기를 동작시킨다.
        for t in (pump_task, watch_task):
            if t.done() and not t.cancelled() and t.exception() is not None:
                raise t.exception()
    except WebSocketDisconnect:
        logger.info("Client '%s' disconnected from '%s'", client.client_id, full_channel)
    except Exception as e:
        logger.error("WebSocket error: %s", e)
    finally:
        async with _ws_lock:
            _ws_connection_count -= 1
        try:
            if pubsub:
                await pubsub.unsubscribe(full_channel)
                await pubsub.aclose()
        finally:
            if sub_client:
                await sub_client.aclose()
