"""String 타입 API — /ns/{ns}/kv/{key}"""

from __future__ import annotations

import logging

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends

from app.auth.namespace_guard import require_read, require_write
from app.dependencies import get_client, get_redis
from app.schemas.common import ClientInfo
from app.schemas.admin import TtlSetRequest
from app.schemas.kv import KvBatchGetRequest, KvBatchSetRequest, KvIncrRequest, KvSetRequest
from app.utils.key_builder import KV_PREFIX, build_key
from app.utils.response import error, not_found, ok
from app.utils.ttl import resolve_ttl, touch_key
from app.utils.validation import check_value_size

router = APIRouter()

logger = logging.getLogger("redgw.kv")


# ── Batch 엔드포인트 (반드시 {key} catch-all 경로보다 위에 정의) ──


@router.post("/ns/{ns}/kv/batch", summary="여러 키 일괄 조회 (MGET)")
async def batch_get_kv(
    ns: str,
    body: KvBatchGetRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_read(client, ns)
    redis_keys = [build_key(ns, KV_PREFIX, k) for k in body.keys]
    values = await r.mget(*redis_keys)
    result = {k: v for k, v in zip(body.keys, values) if v is not None}
    return ok({"values": result, "count": len(result)}, ns=ns, key="batch", type="string")


@router.put("/ns/{ns}/kv/batch", summary="여러 키 일괄 저장 (MSET)")
async def batch_set_kv(
    ns: str,
    body: KvBatchSetRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    for key_name, value in body.items.items():
        check_value_size(value, label=f"Key '{key_name}'")

    ttl = resolve_ttl(body.ttl)
    async with r.pipeline(transaction=True) as pipe:
        for key_name, value in body.items.items():
            redis_key = build_key(ns, KV_PREFIX, key_name)
            if ttl > 0:
                pipe.setex(redis_key, ttl, value)
            else:
                pipe.set(redis_key, value)
        await pipe.execute()

    return ok({"stored": len(body.items)}, ns=ns, key="batch", type="string")


# ── 단건 엔드포인트 ──


@router.get("/ns/{ns}/kv/{key}", summary="값 조회")
async def get_kv(
    ns: str,
    key: str,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_read(client, ns)
    redis_key = build_key(ns, KV_PREFIX, key)
    # GET+TTL을 단일 파이프라인으로 묶어 1 왕복 (miss면 ttl 결과는 버린다)
    async with r.pipeline(transaction=False) as pipe:
        pipe.get(redis_key)
        pipe.ttl(redis_key)
        value, ttl = await pipe.execute()
    if value is None:
        raise not_found(key, ns)
    # 비트랜잭션 파이프라인이라 GET 직후 다른 클라가 DEL/만료시키면 ttl=-2(미존재) 가능 →
    # 값은 GET 시점 실재했으므로 200 유지하되 meta.ttl만 -1로 정규화(쓰기 경로와 동일 계약).
    return ok({"value": value}, ns=ns, key=key, type="string", ttl=ttl if ttl != -2 else -1)


@router.put("/ns/{ns}/kv/{key}", summary="값 저장")
async def set_kv(
    ns: str,
    key: str,
    body: KvSetRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    check_value_size(body.value)

    redis_key = build_key(ns, KV_PREFIX, key)
    ttl = resolve_ttl(body.ttl)

    if ttl > 0:
        await r.setex(redis_key, ttl, body.value)
    else:
        await r.set(redis_key, body.value)

    # 방금 TTL을 설정/소거했으므로 재조회 없이 응답.
    # SET은 KEEPTTL 미사용 → 기존 TTL 소거(=-1), SETEX는 ttl 설정. (1 왕복 절감)
    return ok({"value": body.value}, ns=ns, key=key, type="string", ttl=ttl if ttl > 0 else -1)


@router.delete("/ns/{ns}/kv/{key}", summary="값 삭제")
async def delete_kv(
    ns: str,
    key: str,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    redis_key = build_key(ns, KV_PREFIX, key)
    deleted = await r.delete(redis_key)
    if not deleted:
        raise not_found(key, ns)
    return ok({"deleted": True}, ns=ns, key=key, type="string")


@router.get("/ns/{ns}/kv/{key}/exists", summary="키 존재 여부 확인 (EXISTS)")
async def exists_kv(
    ns: str,
    key: str,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_read(client, ns)
    redis_key = build_key(ns, KV_PREFIX, key)
    exists = await r.exists(redis_key)
    return ok({"exists": bool(exists)}, ns=ns, key=key, type="string")


@router.post("/ns/{ns}/kv/{key}/incr", summary="값 증가 (INCRBY)")
async def incr_kv(
    ns: str,
    key: str,
    body: KvIncrRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    redis_key = build_key(ns, KV_PREFIX, key)
    # INCRBY는 기존 TTL을 보존(값을 모름) → incrby+ttl을 단일 파이프라인으로 1 왕복
    async with r.pipeline(transaction=False) as pipe:
        pipe.incrby(redis_key, body.delta)
        pipe.ttl(redis_key)
        try:
            new_value, ttl = await pipe.execute()
        except aioredis.ResponseError as e:
            # 기존값이 정수가 아니거나, 기존값+delta 합이 int64 범위를 넘으면 Redis가 거부.
            # (delta 값 자체의 범위는 KvIncrRequest에서 422로 선차단)
            logger.warning("INCRBY rejected (ns=%s key=%s): %s", ns, key, e)
            raise error(
                "INVALID_VALUE",
                "Existing value is not an integer or the increment is out of int64 range",
                status=400,
            )
    # 비트랜잭션 파이프라인이라 incrby 직후 다른 클라가 DEL/만료시키면 ttl=-2(미존재) 가능 →
    # 쓰기는 성공이므로 meta.ttl만 -1로 정규화(queue/group/rank와 동일 계약).
    return ok({"value": str(new_value)}, ns=ns, key=key, type="string", ttl=ttl if ttl != -2 else -1)


@router.put("/ns/{ns}/kv/{key}/touch", summary="TTL 갱신 (EXPIRE/PERSIST)")
async def touch_kv(
    ns: str,
    key: str,
    body: TtlSetRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    redis_key = build_key(ns, KV_PREFIX, key)
    return await touch_key(r, redis_key, body.ttl, ns, key, "string")


@router.put("/ns/{ns}/kv/{key}/nx", summary="값이 없을 때만 저장 (SETNX, 분산 락)")
async def set_kv_nx(
    ns: str,
    key: str,
    body: KvSetRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    redis_key = build_key(ns, KV_PREFIX, key)
    ttl = resolve_ttl(body.ttl)

    if ttl > 0:
        acquired = await r.set(redis_key, body.value, nx=True, ex=ttl)
    else:
        acquired = await r.set(redis_key, body.value, nx=True)

    if not acquired:
        raise error("KEY_EXISTS", f"Key '{key}' already exists in namespace '{ns}'", status=409)

    # 획득 성공 = 신규 생성이므로 방금 설정한 TTL을 안다 (재조회 불필요, 1 왕복 절감)
    return ok({"acquired": True, "value": body.value}, ns=ns, key=key, type="string", ttl=ttl if ttl > 0 else -1)
