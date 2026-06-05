"""Sorted Set 타입 API — /ns/{ns}/rank/{key}"""

from __future__ import annotations

import logging
import math
from typing import Literal

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Query

from app.auth.namespace_guard import require_read, require_write
from app.dependencies import get_client, get_redis
from app.schemas.common import ClientInfo
from app.schemas.admin import TtlSetRequest
from app.schemas.rank import RankAddRequest, RankBatchAddRequest, RankBatchGetRequest, RankIncrRequest
from app.utils.key_builder import RANK_PREFIX, build_key
from app.utils.response import error, not_found, ok
from app.utils.ttl import resolve_ttl, touch_key
from app.utils.validation import check_value_size

router = APIRouter()

logger = logging.getLogger("redgw.rank")


# ── Batch 엔드포인트 (반드시 {key} catch-all 경로보다 위에 정의) ──


@router.post("/ns/{ns}/rank/batch", summary="여러 Sorted Set 일괄 조회")
async def batch_get_rank(
    ns: str,
    body: RankBatchGetRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_read(client, ns)
    redis_keys = [build_key(ns, RANK_PREFIX, k) for k in body.keys]
    async with r.pipeline(transaction=False) as pipe:
        for rk in redis_keys:
            if body.reverse:
                pipe.zrevrange(rk, body.start, body.stop, withscores=True)
            else:
                pipe.zrange(rk, body.start, body.stop, withscores=True)
        results = await pipe.execute()
    values = {}
    for k, result in zip(body.keys, results):
        if result:
            values[k] = [{"member": m, "score": s} for m, s in result]
    return ok({"values": values, "count": len(values)}, ns=ns, key="batch", type="zset")


# ── 단건 엔드포인트 ──


@router.post("/ns/{ns}/rank/{key}/batch", summary="단일 키에 여러 멤버+스코어 일괄 추가 (ZADD)")
async def batch_add_rank(
    ns: str,
    key: str,
    body: RankBatchAddRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    for item in body.members:
        check_value_size(item.member, label="Member")
        # nan/inf는 Redis 접촉 전에 거부 — 사후(ZADD ResponseError) 거부는 같은 파이프라인의
        # EXPIRE/PERSIST가 이미 실행돼 키 TTL을 부수효과로 바꾼다(거부된 쓰기인데 TTL 변경).
        # inf는 ZADD가 받아 저장하나 JSON 응답에서 null로 직렬화돼 API로 회수 불가 → 입력단 거부.
        if not math.isfinite(item.score):
            raise error("INVALID_VALUE", "Score must be a finite number (nan/inf rejected)", status=400)
    redis_key = build_key(ns, RANK_PREFIX, key)
    mapping = {item.member: item.score for item in body.members}
    # ZADD는 기존 TTL 보존 → zadd·(조건)expire·ttl을 단일 파이프라인으로 3→1 왕복
    ttl = resolve_ttl(body.ttl)
    async with r.pipeline(transaction=False) as pipe:
        pipe.zadd(redis_key, mapping)
        if ttl > 0:
            pipe.expire(redis_key, ttl)
        else:
            pipe.persist(redis_key)  # ttl=0=무TTL — ZADD는 기존 TTL 보존하므로 제거
        pipe.ttl(redis_key)
        try:
            results = await pipe.execute()
        except aioredis.ResponseError as e:
            # score가 nan이면 Redis ZADD가 거부 — incr_rank와 동일하게 400으로 변환
            # (미변환 시 unhandled → 500 INTERNAL_ERROR + 로그 오염).
            logger.warning("ZADD rejected (ns=%s key=%s): %s", ns, key, e)
            raise error("INVALID_VALUE", "Score is not a valid number (nan)", status=400)
    _ttl = results[-1]  # 경합 시 -2 → -1 정규화 (쓰기 성공이므로, queue/group과 동일 계약)
    return ok({"added": results[0], "total": len(body.members)}, ns=ns, key=key, type="zset", ttl=_ttl if _ttl != -2 else -1)


@router.post("/ns/{ns}/rank/{key}", summary="멤버+스코어 추가 (ZADD)")
async def add_rank(
    ns: str,
    key: str,
    body: RankAddRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    check_value_size(body.member, label="Member")
    # nan/inf는 Redis 접촉 전에 거부 — 사후 거부 시 같은 파이프라인의 EXPIRE/PERSIST가 이미
    # 실행돼 키 TTL을 부수효과로 바꾼다(거부된 쓰기인데 TTL 변경).
    # inf는 ZADD가 받아 저장하나 JSON 응답에서 null로 직렬화돼 API로 회수 불가 → 입력단 거부.
    if not math.isfinite(body.score):
        raise error("INVALID_VALUE", "Score must be a finite number (nan/inf rejected)", status=400)
    redis_key = build_key(ns, RANK_PREFIX, key)
    # ZADD는 기존 TTL 보존 → zadd·(조건)expire·ttl을 단일 파이프라인으로 3→1 왕복
    ttl = resolve_ttl(body.ttl)
    async with r.pipeline(transaction=False) as pipe:
        pipe.zadd(redis_key, {body.member: body.score})
        if ttl > 0:
            pipe.expire(redis_key, ttl)
        else:
            pipe.persist(redis_key)  # ttl=0=무TTL — ZADD는 기존 TTL 보존하므로 제거
        pipe.ttl(redis_key)
        try:
            results = await pipe.execute()
        except aioredis.ResponseError as e:
            # score가 nan이면 Redis ZADD가 거부 — incr_rank와 동일하게 400으로 변환
            # (미변환 시 unhandled → 500 INTERNAL_ERROR + 로그 오염).
            logger.warning("ZADD rejected (ns=%s key=%s): %s", ns, key, e)
            raise error("INVALID_VALUE", "Score is not a valid number (nan)", status=400)
    _ttl = results[-1]  # 경합 시 -2 → -1 정규화 (쓰기 성공이므로, queue/group과 동일 계약)
    return ok({"added": results[0], "member": body.member, "score": body.score}, ns=ns, key=key, type="zset", ttl=_ttl if _ttl != -2 else -1)


@router.get("/ns/{ns}/rank/{key}", summary="범위 조회 (ZRANGE/ZREVRANGE)")
async def range_rank(
    ns: str,
    key: str,
    start: int = Query(0),
    stop: int = Query(-1),
    reverse: bool = Query(False),
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_read(client, ns)
    redis_key = build_key(ns, RANK_PREFIX, key)

    if reverse:
        results = await r.zrevrange(redis_key, start, stop, withscores=True)
    else:
        results = await r.zrange(redis_key, start, stop, withscores=True)

    members = [{"member": m, "score": s} for m, s in results]
    return ok({"members": members, "count": len(members)}, ns=ns, key=key, type="zset")


@router.get("/ns/{ns}/rank/{key}/score/{member}", summary="멤버의 스코어 조회 (ZSCORE)")
async def score_rank(
    ns: str,
    key: str,
    member: str,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_read(client, ns)
    redis_key = build_key(ns, RANK_PREFIX, key)
    score = await r.zscore(redis_key, member)
    if score is None:
        raise error("MEMBER_NOT_FOUND", f"Member '{member}' not found in rank '{key}'", status=404)
    return ok({"member": member, "score": score}, ns=ns, key=key, type="zset")


@router.get("/ns/{ns}/rank/{key}/between", summary="스코어 범위 조회 (ZRANGEBYSCORE)")
async def between_rank(
    ns: str,
    key: str,
    min: float = Query(..., description="최소 스코어"),
    max: float = Query(..., description="최대 스코어"),
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_read(client, ns)
    redis_key = build_key(ns, RANK_PREFIX, key)
    try:
        results = await r.zrangebyscore(redis_key, min, max, withscores=True)
    except aioredis.ResponseError as e:
        # ?min=nan 등 nan 경계값은 Redis가 거부 — 400으로 변환(미변환 시 500).
        logger.warning("ZRANGEBYSCORE rejected (ns=%s key=%s): %s", ns, key, e)
        raise error("INVALID_VALUE", "Score range bound is not a valid number (nan)", status=400)
    members = [{"member": m, "score": s} for m, s in results]
    return ok({"members": members, "count": len(members)}, ns=ns, key=key, type="zset")


@router.delete("/ns/{ns}/rank/{key}", summary="전체 삭제 (DEL)")
async def delete_rank(
    ns: str,
    key: str,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    redis_key = build_key(ns, RANK_PREFIX, key)
    deleted = await r.delete(redis_key)
    if not deleted:
        raise not_found(key, ns)
    return ok({"deleted": True}, ns=ns, key=key, type="zset")


@router.delete("/ns/{ns}/rank/{key}/{member}", summary="멤버 제거 (ZREM)")
async def remove_rank(
    ns: str,
    key: str,
    member: str,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    redis_key = build_key(ns, RANK_PREFIX, key)
    removed = await r.zrem(redis_key, member)
    if not removed:
        raise error("MEMBER_NOT_FOUND", f"Member '{member}' not found in rank '{key}'", status=404)
    return ok({"removed": True, "member": member}, ns=ns, key=key, type="zset")


@router.post("/ns/{ns}/rank/{key}/incr", summary="스코어 증감 (ZINCRBY)")
async def incr_rank(
    ns: str,
    key: str,
    body: RankIncrRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    check_value_size(body.member, label="Member")
    # nan/inf delta 거부 — inf는 ZINCRBY가 받아 inf로 저장하나 JSON 응답에서 null로 직렬화돼
    # 회수 불가, +inf에 -inf delta는 NaN ResponseError(500) 유발. 입력단에서 차단.
    if not math.isfinite(body.delta):
        raise error("INVALID_VALUE", "Delta must be a finite number (nan/inf rejected)", status=400)
    redis_key = build_key(ns, RANK_PREFIX, key)
    # ZINCRBY는 기존 TTL을 보존(값을 모름) → zincrby+ttl 단일 파이프라인으로 보존 TTL을
    # meta에 보고(incr_kv와 동일 패턴 — 형제 엔드포인트 간 meta.ttl 비대칭 제거).
    async with r.pipeline(transaction=False) as pipe:
        pipe.zincrby(redis_key, body.delta, body.member)
        pipe.ttl(redis_key)
        try:
            new_score, ttl = await pipe.execute()
        except aioredis.ResponseError as e:
            # 증감 결과가 nan(예: +inf 후 -inf) 또는 범위를 벗어나면 Redis가 거부.
            logger.warning("ZINCRBY rejected (ns=%s key=%s): %s", ns, key, e)
            raise error(
                "INVALID_VALUE",
                "Resulting score is not a number (nan) or out of range",
                status=400,
            )
    # 비트랜잭션 파이프라인이라 zincrby 직후 다른 클라가 DEL/만료시키면 ttl=-2(미존재) 가능 →
    # 쓰기는 성공이므로 meta.ttl만 -1로 정규화(add_rank/batch_add_rank와 동일 계약).
    return ok({"member": body.member, "score": new_score}, ns=ns, key=key, type="zset", ttl=ttl if ttl != -2 else -1)


@router.put("/ns/{ns}/rank/{key}/touch", summary="TTL 갱신 (EXPIRE/PERSIST)")
async def touch_rank(
    ns: str,
    key: str,
    body: TtlSetRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    redis_key = build_key(ns, RANK_PREFIX, key)
    return await touch_key(r, redis_key, body.ttl, ns, key, "zset")


@router.get("/ns/{ns}/rank/{key}/pop", summary="최소/최대 스코어 추출 (ZPOPMIN/ZPOPMAX)")
async def pop_rank(
    ns: str,
    key: str,
    direction: Literal["min", "max"] = Query("min", description="min 또는 max"),
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    redis_key = build_key(ns, RANK_PREFIX, key)

    if direction == "max":
        result = await r.zpopmax(redis_key, count=1)
    else:
        result = await r.zpopmin(redis_key, count=1)

    if not result:
        raise error("RANK_EMPTY", f"Rank '{key}' is empty", status=404)

    member, score = result[0]
    return ok({"member": member, "score": score}, ns=ns, key=key, type="zset")
