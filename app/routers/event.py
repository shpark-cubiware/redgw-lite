"""Stream 타입 API — /ns/{ns}/event/{key}"""

from __future__ import annotations

import logging
import re

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Query

from app.auth.namespace_guard import require_read, require_write
from app.config import get_settings
from app.dependencies import get_client, get_redis
from app.schemas.common import ClientInfo
from app.schemas.admin import TtlSetRequest
from app.schemas.event import EventAckRequest, EventBatchPublishRequest, EventGroupCreateRequest, EventPublishRequest
from app.utils.key_builder import EVENT_PREFIX, build_key
from app.utils.response import error, ok
from app.utils.ttl import touch_key
from app.utils.validation import check_dict_values_size

logger = logging.getLogger("redgw.event")

router = APIRouter()

_STREAM_ID_RE = re.compile(r"^(0|\d+-\d+)$")


@router.post("/ns/{ns}/event/{key}/batch", summary="여러 이벤트 일괄 발행 (Pipeline XADD)")
async def batch_publish_event(
    ns: str,
    key: str,
    body: EventBatchPublishRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    for evt in body.events:
        check_dict_values_size(evt)

    settings = get_settings()
    redis_key = build_key(ns, EVENT_PREFIX, key)
    maxlen = settings.defaults.max_stream_length

    async with r.pipeline(transaction=False) as pipe:
        for evt in body.events:
            pipe.xadd(redis_key, evt, maxlen=maxlen)
        ids = await pipe.execute()

    return ok({"ids": ids, "count": len(ids)}, ns=ns, key=key, type="stream")


@router.post("/ns/{ns}/event/{key}", summary="이벤트 발행 (XADD)")
async def publish_event(
    ns: str,
    key: str,
    body: EventPublishRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    check_dict_values_size(body.data)

    settings = get_settings()
    redis_key = build_key(ns, EVENT_PREFIX, key)

    entry_id = await r.xadd(
        redis_key,
        body.data,
        maxlen=settings.defaults.max_stream_length,
    )
    return ok({"id": entry_id}, ns=ns, key=key, type="stream")


@router.get("/ns/{ns}/event/{key}", summary="이벤트 읽기 (XRANGE)")
async def read_events(
    ns: str,
    key: str,
    last_id: str = Query("0", description="시작 ID (0 = 처음부터)"),
    count: int = Query(10, description="읽을 이벤트 수", ge=1, le=1000),
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_read(client, ns)
    redis_key = build_key(ns, EVENT_PREFIX, key)

    # last_id 포맷 검증
    if not _STREAM_ID_RE.match(last_id):
        raise error("INVALID_STREAM_ID", f"Invalid stream ID format: '{last_id}'", status=400)

    # last_id가 "0"이면 처음부터, 아니면 해당 ID 이후부터
    min_id = "-" if last_id == "0" else f"({last_id}"
    entries = await r.xrange(redis_key, min=min_id, max="+", count=count)

    events = [{"id": eid, "data": data} for eid, data in entries]
    return ok({"events": events, "count": len(events)}, ns=ns, key=key, type="stream")


@router.post("/ns/{ns}/event/{key}/group", summary="Consumer Group 생성 (XGROUP CREATE)")
async def create_group(
    ns: str,
    key: str,
    body: EventGroupCreateRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    # Consumer Group 생성은 구독(소비) 행위이므로 read 권한으로 허용
    require_read(client, ns)
    redis_key = build_key(ns, EVENT_PREFIX, key)

    try:
        await r.xgroup_create(redis_key, body.group, id="0", mkstream=True)
    except aioredis.ResponseError as e:
        # Redis: "BUSYGROUP Consumer Group name already exists" — 그룹 중복 생성 시
        if "BUSYGROUP" in str(e):
            raise error("GROUP_EXISTS", f"Consumer group '{body.group}' already exists", status=409)
        logger.error("XGROUP CREATE failed: %s", e)
        raise error("INTERNAL_ERROR", "Stream operation failed", status=500)

    return ok({"group": body.group, "created": True}, ns=ns, key=key, type="stream")


@router.get(
    "/ns/{ns}/event/{key}/group/{group}/read",
    summary="Consumer Group 읽기 (XREADGROUP)",
)
async def read_group(
    ns: str,
    key: str,
    group: str,
    consumer: str = Query(..., description="Consumer 이름"),
    count: int = Query(5, description="읽을 이벤트 수", ge=1, le=1000),
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_read(client, ns)
    redis_key = build_key(ns, EVENT_PREFIX, key)

    try:
        results = await r.xreadgroup(
            group, consumer, {redis_key: ">"}, count=count
        )
    except aioredis.ResponseError as e:
        # Redis: "NOGROUP No such key ... or consumer group ..." — 존재하지 않는 그룹
        if "NOGROUP" in str(e):
            raise error("GROUP_NOT_FOUND", f"Consumer group '{group}' not found", status=404)
        logger.error("XREADGROUP failed: %s", e)
        raise error("INTERNAL_ERROR", "Stream operation failed", status=500)

    events = []
    if results:
        for _stream, entries in results:
            for eid, data in entries:
                events.append({"id": eid, "data": data})

    return ok({"events": events, "count": len(events), "consumer": consumer}, ns=ns, key=key, type="stream")


@router.post(
    "/ns/{ns}/event/{key}/group/{group}/ack",
    summary="처리 완료 확인 (XACK)",
)
async def ack_events(
    ns: str,
    key: str,
    group: str,
    body: EventAckRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    # ACK은 소비 완료 확인이므로 read 권한으로 허용
    require_read(client, ns)
    # ids 포맷 검증 — 검증 없이 XACK에 넘기면 잘못된 형식이 Redis ResponseError를 거쳐
    # 500 INTERNAL_ERROR로 잘못 매핑된다(read_events 등 다른 엔드포인트와 동일하게 400 처리).
    for sid in body.ids:
        if not _STREAM_ID_RE.match(sid):
            raise error("INVALID_STREAM_ID", f"Invalid stream ID format: '{sid}'", status=400)
    redis_key = build_key(ns, EVENT_PREFIX, key)
    acked = await r.xack(redis_key, group, *body.ids)
    return ok({"acked": acked}, ns=ns, key=key, type="stream")


@router.put("/ns/{ns}/event/{key}/touch", summary="TTL 갱신 (EXPIRE/PERSIST)")
async def touch_event(
    ns: str,
    key: str,
    body: TtlSetRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    redis_key = build_key(ns, EVENT_PREFIX, key)
    return await touch_key(r, redis_key, body.ttl, ns, key, "stream")


@router.get("/ns/{ns}/event/{key}/info", summary="스트림 정보 (XINFO STREAM)")
async def stream_info(
    ns: str,
    key: str,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_read(client, ns)
    redis_key = build_key(ns, EVENT_PREFIX, key)

    try:
        info = await r.xinfo_stream(redis_key)
    except aioredis.ResponseError:
        raise error("KEY_NOT_FOUND", f"Stream '{key}' not found in namespace '{ns}'", status=404)

    # 그룹 정보도 조회
    try:
        groups_info = await r.xinfo_groups(redis_key)
        groups = [{"name": g["name"], "consumers": g["consumers"], "pending": g["pending"]} for g in groups_info]
    except aioredis.ResponseError:
        groups = []

    return ok(
        {
            "length": info["length"],
            "first_entry": info.get("first-entry"),
            "last_entry": info.get("last-entry"),
            "groups": groups,
        },
        ns=ns,
        key=key,
        type="stream",
    )
