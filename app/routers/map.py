"""Hash 타입 API — /ns/{ns}/map/{key}"""

from __future__ import annotations

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends

from app.auth.namespace_guard import require_read, require_write
from app.dependencies import get_client, get_redis
from app.schemas.common import ClientInfo
from app.schemas.admin import TtlSetRequest
from app.schemas.map import MapBatchGetRequest, MapBatchSetRequest, MapFieldSetRequest, MapSetRequest
from app.utils.key_builder import MAP_PREFIX, build_key
from app.utils.response import error, not_found, ok
from app.utils.ttl import resolve_ttl, touch_key
from app.utils.validation import check_dict_values_size, check_value_size

router = APIRouter()


# ── Batch 엔드포인트 (반드시 {key} catch-all 경로보다 위에 정의) ──


@router.post("/ns/{ns}/map/batch", summary="여러 해시 일괄 조회")
async def batch_get_map(
    ns: str,
    body: MapBatchGetRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_read(client, ns)
    redis_keys = [build_key(ns, MAP_PREFIX, k) for k in body.keys]
    async with r.pipeline(transaction=False) as pipe:
        for rk in redis_keys:
            pipe.hgetall(rk)
        results = await pipe.execute()
    values = {k: v for k, v in zip(body.keys, results) if v}
    return ok({"values": values, "count": len(values)}, ns=ns, key="batch", type="hash")


@router.put("/ns/{ns}/map/batch", summary="여러 해시 일괄 저장")
async def batch_set_map(
    ns: str,
    body: MapBatchSetRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    for key_name, fields in body.items.items():
        check_dict_values_size(fields)

    ttl = resolve_ttl(body.ttl)
    async with r.pipeline(transaction=True) as pipe:
        for key_name, fields in body.items.items():
            redis_key = build_key(ns, MAP_PREFIX, key_name)
            pipe.hset(redis_key, mapping=fields)
            if ttl > 0:
                pipe.expire(redis_key, ttl)
            else:
                # ttl=0 = "TTL 없음" — HSET은 기존 TTL을 보존하므로 PERSIST로 제거.
                # 단건 set_map과 동일 계약(배치 경로 누락분 보완).
                pipe.persist(redis_key)
        await pipe.execute()

    return ok({"stored": len(body.items)}, ns=ns, key="batch", type="hash")


# ── 단건 엔드포인트 ──


@router.get("/ns/{ns}/map/{key}", summary="전체 필드 조회 (HGETALL)")
async def get_map(
    ns: str,
    key: str,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_read(client, ns)
    redis_key = build_key(ns, MAP_PREFIX, key)
    # HGETALL+TTL을 단일 파이프라인으로 1 왕복 (miss면 ttl 결과는 버린다)
    async with r.pipeline(transaction=False) as pipe:
        pipe.hgetall(redis_key)
        pipe.ttl(redis_key)
        fields, ttl = await pipe.execute()
    if not fields:
        raise not_found(key, ns)
    # 경합(HGETALL 직후 DEL/만료)으로 ttl=-2가 될 수 있어 -1로 정규화(쓰기 경로와 동일 계약).
    return ok({"fields": fields}, ns=ns, key=key, type="hash", ttl=ttl if ttl != -2 else -1)


@router.get("/ns/{ns}/map/{key}/{field}", summary="특정 필드 조회 (HGET)")
async def get_map_field(
    ns: str,
    key: str,
    field: str,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_read(client, ns)
    redis_key = build_key(ns, MAP_PREFIX, key)
    # HGET+TTL을 단일 파이프라인으로 1 왕복 (miss면 ttl 결과는 버린다)
    async with r.pipeline(transaction=False) as pipe:
        pipe.hget(redis_key, field)
        pipe.ttl(redis_key)
        value, ttl = await pipe.execute()
    if value is None:
        raise error("FIELD_NOT_FOUND", f"Field '{field}' not found in key '{key}'", status=404)
    # 경합(HGET 직후 DEL/만료)으로 ttl=-2가 될 수 있어 -1로 정규화(쓰기 경로와 동일 계약).
    return ok({"field": field, "value": value}, ns=ns, key=key, type="hash", ttl=ttl if ttl != -2 else -1)


@router.put("/ns/{ns}/map/{key}", summary="다수 필드 저장 (HMSET)")
async def set_map(
    ns: str,
    key: str,
    body: MapSetRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    check_dict_values_size(body.fields)

    redis_key = build_key(ns, MAP_PREFIX, key)

    ttl = resolve_ttl(body.ttl)
    # 쓰기·expire·ttl을 한 트랜잭션(MULTI/EXEC)에 묶어 1 왕복 + 원자 스냅샷 TTL.
    # ttl은 항상 마지막에 큐잉되므로 results[-1] (expire 조건부라 인덱스 가변)
    async with r.pipeline(transaction=True) as pipe:
        pipe.hset(redis_key, mapping=body.fields)
        if ttl > 0:
            pipe.expire(redis_key, ttl)
        else:
            # ttl=0은 "TTL 없음" 계약(kv write·touch와 동일). HSET은 기존 TTL을 보존하므로
            # 명시적 PERSIST로 제거해 kv↔map 동작을 일치시킨다. MULTI/EXEC라 원자적.
            pipe.persist(redis_key)
        pipe.ttl(redis_key)
        results = await pipe.execute()

    return ok({"fields": body.fields}, ns=ns, key=key, type="hash", ttl=results[-1])


@router.put("/ns/{ns}/map/{key}/touch", summary="TTL 갱신 (EXPIRE/PERSIST)")
async def touch_map(
    ns: str,
    key: str,
    body: TtlSetRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    redis_key = build_key(ns, MAP_PREFIX, key)
    return await touch_key(r, redis_key, body.ttl, ns, key, "hash")


@router.put("/ns/{ns}/map/{key}/{field}", summary="단일 필드 저장 (HSET)")
async def set_map_field(
    ns: str,
    key: str,
    field: str,
    body: MapFieldSetRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    check_value_size(body.value)

    redis_key = build_key(ns, MAP_PREFIX, key)
    ttl = resolve_ttl(body.ttl)

    if ttl > 0:
        async with r.pipeline(transaction=True) as pipe:
            pipe.hset(redis_key, field, body.value)
            pipe.expire(redis_key, ttl)
            pipe.ttl(redis_key)
            results = await pipe.execute()
    else:
        # ttl=0은 "TTL 없음" 계약 — HSET은 기존 TTL을 보존하므로 PERSIST로 제거해
        # kv write·touch와 동작을 일치시킨다(미적용 시 map만 잔존 TTL을 보고).
        async with r.pipeline(transaction=False) as pipe:
            pipe.hset(redis_key, field, body.value)
            pipe.persist(redis_key)
            pipe.ttl(redis_key)
            results = await pipe.execute()
        # 비트랜잭션 파이프라인이라 hset 직후 다른 클라가 DEL/만료시키면 ttl=-2(미존재) 가능 →
        # 쓰기는 성공이므로 meta.ttl만 -1로 정규화(queue/group/rank와 동일 계약).
        _ttl = results[-1]
        return ok({"field": field, "value": body.value}, ns=ns, key=key, type="hash", ttl=_ttl if _ttl != -2 else -1)
    return ok({"field": field, "value": body.value}, ns=ns, key=key, type="hash", ttl=results[-1])


@router.delete("/ns/{ns}/map/{key}", summary="전체 삭제")
async def delete_map(
    ns: str,
    key: str,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    redis_key = build_key(ns, MAP_PREFIX, key)
    deleted = await r.delete(redis_key)
    if not deleted:
        raise not_found(key, ns)
    return ok({"deleted": True}, ns=ns, key=key, type="hash")


@router.delete("/ns/{ns}/map/{key}/{field}", summary="특정 필드 삭제 (HDEL)")
async def delete_map_field(
    ns: str,
    key: str,
    field: str,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    redis_key = build_key(ns, MAP_PREFIX, key)
    deleted = await r.hdel(redis_key, field)
    if not deleted:
        raise error("FIELD_NOT_FOUND", f"Field '{field}' not found in key '{key}'", status=404)
    return ok({"deleted": True, "field": field}, ns=ns, key=key, type="hash")
