"""Set 타입 API — /ns/{ns}/group/{key}"""

from __future__ import annotations

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends

from app.auth.namespace_guard import require_read, require_write
from app.dependencies import get_client, get_redis
from app.schemas.common import ClientInfo
from app.schemas.admin import TtlSetRequest
from app.schemas.group import GroupAddRequest, GroupBatchAddRequest, GroupBatchGetRequest, GroupOpsRequest
from app.utils.key_builder import GROUP_PREFIX, build_key
from app.utils.response import error, ok
from app.utils.ttl import resolve_ttl, touch_key
from app.utils.validation import check_value_size

router = APIRouter()


# ── Batch 엔드포인트 (반드시 {key} catch-all 경로보다 위에 정의) ──


@router.post("/ns/{ns}/group/batch", summary="여러 Set 일괄 조회")
async def batch_get_group(
    ns: str,
    body: GroupBatchGetRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_read(client, ns)
    redis_keys = [build_key(ns, GROUP_PREFIX, k) for k in body.keys]
    async with r.pipeline(transaction=False) as pipe:
        for rk in redis_keys:
            pipe.smembers(rk)
        results = await pipe.execute()
    values = {k: sorted(v) for k, v in zip(body.keys, results) if v}
    return ok({"values": values, "count": len(values)}, ns=ns, key="batch", type="set")


@router.put("/ns/{ns}/group/batch", summary="여러 Set 일괄 멤버 추가")
async def batch_add_group(
    ns: str,
    body: GroupBatchAddRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    for members in body.items.values():
        for m in members:
            check_value_size(m, label="Member")
    ttl = resolve_ttl(body.ttl)
    async with r.pipeline(transaction=True) as pipe:
        for key_name, members in body.items.items():
            redis_key = build_key(ns, GROUP_PREFIX, key_name)
            pipe.sadd(redis_key, *members)
            if ttl > 0:
                pipe.expire(redis_key, ttl)
            else:
                pipe.persist(redis_key)  # ttl=0=무TTL — SADD는 기존 TTL 보존하므로 제거
        await pipe.execute()
    return ok({"stored": len(body.items)}, ns=ns, key="batch", type="set")


# ── 단건 엔드포인트 ──


@router.post("/ns/{ns}/group/{key}", summary="멤버 추가 (SADD)")
async def add_group(
    ns: str,
    key: str,
    body: GroupAddRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    for m in body.members:
        check_value_size(m, label="Member")
    redis_key = build_key(ns, GROUP_PREFIX, key)
    # SADD는 기존 TTL 보존 → sadd·(조건)expire·ttl을 단일 파이프라인으로 3→1 왕복.
    # results[0]=추가된 멤버 수, results[-1]=실제 ttl (expire 조건부라 인덱스 가변)
    ttl = resolve_ttl(body.ttl)
    async with r.pipeline(transaction=False) as pipe:
        pipe.sadd(redis_key, *body.members)
        if ttl > 0:
            pipe.expire(redis_key, ttl)
        else:
            pipe.persist(redis_key)  # ttl=0=무TTL — SADD는 기존 TTL 보존하므로 제거
        pipe.ttl(redis_key)
        results = await pipe.execute()
    # 비트랜잭션 파이프라인이라 sadd 직후 다른 클라가 DEL시키면 ttl=-2(미존재) 가능 →
    # 쓰기는 성공이므로 meta.ttl만 -1로 정규화(queue push와 동일 계약, added>0+ttl=-2 모순 방지).
    _ttl = results[-1]
    return ok({"added": results[0]}, ns=ns, key=key, type="set", ttl=_ttl if _ttl != -2 else -1)


@router.get("/ns/{ns}/group/{key}", summary="전체 멤버 조회 (SMEMBERS)")
async def members_group(
    ns: str,
    key: str,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_read(client, ns)
    redis_key = build_key(ns, GROUP_PREFIX, key)
    members = await r.smembers(redis_key)
    return ok({"members": sorted(members)}, ns=ns, key=key, type="set")


@router.get("/ns/{ns}/group/{key}/contains/{member}", summary="멤버 존재 확인 (SISMEMBER)")
async def contains_group(
    ns: str,
    key: str,
    member: str,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_read(client, ns)
    redis_key = build_key(ns, GROUP_PREFIX, key)
    exists = await r.sismember(redis_key, member)
    return ok({"member": member, "exists": bool(exists)}, ns=ns, key=key, type="set")


@router.delete("/ns/{ns}/group/{key}/{member}", summary="멤버 제거 (SREM)")
async def remove_group(
    ns: str,
    key: str,
    member: str,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    redis_key = build_key(ns, GROUP_PREFIX, key)
    removed = await r.srem(redis_key, member)
    if not removed:
        raise error("MEMBER_NOT_FOUND", f"Member '{member}' not found in group '{key}'", status=404)
    return ok({"removed": True, "member": member}, ns=ns, key=key, type="set")


@router.get("/ns/{ns}/group/{key}/count", summary="멤버 수 (SCARD)")
async def count_group(
    ns: str,
    key: str,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_read(client, ns)
    redis_key = build_key(ns, GROUP_PREFIX, key)
    count = await r.scard(redis_key)
    return ok({"count": count}, ns=ns, key=key, type="set")


@router.put("/ns/{ns}/group/{key}/touch", summary="TTL 갱신 (EXPIRE/PERSIST)")
async def touch_group(
    ns: str,
    key: str,
    body: TtlSetRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    redis_key = build_key(ns, GROUP_PREFIX, key)
    return await touch_key(r, redis_key, body.ttl, ns, key, "set")


async def _set_op(op: str, r: aioredis.Redis, ns: str, keys: list[str], client: ClientInfo) -> dict:
    """Set 연산 공통 로직 (inter/union/diff)."""
    require_read(client, ns)
    redis_keys = [build_key(ns, GROUP_PREFIX, k) for k in keys]
    fn = {"inter": r.sinter, "union": r.sunion, "diff": r.sdiff}[op]
    result = await fn(*redis_keys)
    return ok({"members": sorted(result)}, ns=ns, key=f"ops:{op}", type="set")


@router.post("/ns/{ns}/group/ops/inter", summary="교집합 (SINTER)")
async def inter_groups(
    ns: str, body: GroupOpsRequest, client: ClientInfo = Depends(get_client), r: aioredis.Redis = Depends(get_redis),
) -> dict:
    return await _set_op("inter", r, ns, body.keys, client)


@router.post("/ns/{ns}/group/ops/union", summary="합집합 (SUNION)")
async def union_groups(
    ns: str, body: GroupOpsRequest, client: ClientInfo = Depends(get_client), r: aioredis.Redis = Depends(get_redis),
) -> dict:
    return await _set_op("union", r, ns, body.keys, client)


@router.post("/ns/{ns}/group/ops/diff", summary="차집합 (SDIFF)")
async def diff_groups(
    ns: str, body: GroupOpsRequest, client: ClientInfo = Depends(get_client), r: aioredis.Redis = Depends(get_redis),
) -> dict:
    return await _set_op("diff", r, ns, body.keys, client)
