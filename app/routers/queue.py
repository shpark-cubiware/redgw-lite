"""List 타입 API — /ns/{ns}/queue/{key}"""

from __future__ import annotations

from typing import Literal

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Query

from app.auth.namespace_guard import require_read, require_write
from app.config import get_settings
from app.dependencies import get_client, get_redis
from app.schemas.common import ClientInfo
from app.schemas.admin import TtlSetRequest
from app.schemas.queue import QueueBatchPushRequest, QueuePushRequest, QueueTrimRequest
from app.utils.key_builder import QUEUE_PREFIX, build_key
from app.utils.response import error, not_found, ok
from app.utils.ttl import resolve_ttl, touch_key
from app.utils.validation import check_value_size

router = APIRouter()

# 원자적 check-and-push Lua 스크립트 (register_script → EVALSHA로 캐싱)
def _single_push_lua(cmd: str) -> str:
    return (
        f"if redis.call('llen',KEYS[1])>=tonumber(ARGV[2]) then return -1 end\n"
        f"return redis.call('{cmd}',KEYS[1],ARGV[1])"
    )


def _batch_push_lua(cmd: str) -> str:
    return (
        f"local cur=redis.call('llen',KEYS[1])\n"
        f"local max=tonumber(ARGV[1])\n"
        f"local cnt=tonumber(ARGV[2])\n"
        f"if cur+cnt>max then return -1 end\n"
        f"for i=3,#ARGV do redis.call('{cmd}',KEYS[1],ARGV[i]) end\n"
        f"return cur+cnt"
    )


_LUA_SOURCES: dict[tuple[str, str], str] = {
    ("single", "left"): _single_push_lua("lpush"),
    ("single", "right"): _single_push_lua("rpush"),
    ("batch", "left"): _batch_push_lua("lpush"),
    ("batch", "right"): _batch_push_lua("rpush"),
}
# WHY: Lua 스크립트 캐시 — register_script()로 EVALSHA 사용, 매 요청마다 재등록 방지
_script_cache: dict[tuple[str, str], object] = {}


def _get_script(r: aioredis.Redis, mode: str, direction: str):
    key = (mode, direction)
    if key not in _script_cache:
        _script_cache[key] = r.register_script(_LUA_SOURCES[key])
    return _script_cache[key]


@router.post("/ns/{ns}/queue/{key}/batch", summary="큐에 여러 값 일괄 추가")
async def batch_push_queue(
    ns: str,
    key: str,
    body: QueueBatchPushRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    for v in body.values:
        check_value_size(v)

    redis_key = build_key(ns, QUEUE_PREFIX, key)
    settings = get_settings()
    max_len = settings.defaults.max_list_length

    script = _get_script(r, "batch", body.direction)
    result = await script(keys=[redis_key], args=[str(max_len), str(len(body.values))] + body.values)
    if result == -1:
        raise error("QUEUE_FULL", f"Queue '{key}' would exceed max length ({max_len})", status=400)

    # QUEUE_FULL raise는 위에서 먼저 처리(순서 불변). (조건)expire·ttl을 단일 파이프라인으로 묶어 3→2 왕복.
    ttl = resolve_ttl(body.ttl)
    async with r.pipeline(transaction=False) as pipe:
        if ttl > 0:
            pipe.expire(redis_key, ttl)
        else:
            pipe.persist(redis_key)  # ttl=0=무TTL — RPUSH/LPUSH는 기존 TTL 보존하므로 제거
        pipe.ttl(redis_key)
        ttl_results = await pipe.execute()
    _ttl = ttl_results[-1]  # -2(경합 시 키 소멸) → -1 정규화 (push 성공이므로 404 아님)
    return ok({"length": result, "pushed": len(body.values)}, ns=ns, key=key, type="list", ttl=_ttl if _ttl != -2 else -1)


@router.post("/ns/{ns}/queue/{key}", summary="큐에 추가 (RPUSH/LPUSH)")
async def push_queue(
    ns: str,
    key: str,
    body: QueuePushRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    check_value_size(body.value)

    settings = get_settings()
    redis_key = build_key(ns, QUEUE_PREFIX, key)
    max_len = settings.defaults.max_list_length

    script = _get_script(r, "single", body.direction)
    result = await script(keys=[redis_key], args=[body.value, str(max_len)])
    if result == -1:
        raise error("QUEUE_FULL", f"Queue '{key}' has reached max length ({max_len})", status=400)

    # QUEUE_FULL raise는 위에서 먼저 처리(순서 불변). (조건)expire·ttl을 단일 파이프라인으로 묶어 3→2 왕복.
    ttl = resolve_ttl(body.ttl)
    async with r.pipeline(transaction=False) as pipe:
        if ttl > 0:
            pipe.expire(redis_key, ttl)
        else:
            pipe.persist(redis_key)  # ttl=0=무TTL — RPUSH/LPUSH는 기존 TTL 보존하므로 제거
        pipe.ttl(redis_key)
        ttl_results = await pipe.execute()
    # Lua(키 생성)와 별도 라운드트립이라 그 사이 다른 클라가 pop/del/만료시키면 ttl이 -2(미존재)가
    # 될 수 있다. push는 이미 성공했으므로 404가 아니라 meta.ttl만 -1로 정규화(length>0 + ttl=-2
    # 모순 방지). touch_key는 키 부재가 의미 있어 404를 내지만, 여기선 쓰기 성공이라 부적절.
    _ttl = ttl_results[-1]
    return ok({"length": result}, ns=ns, key=key, type="list", ttl=_ttl if _ttl != -2 else -1)


@router.get("/ns/{ns}/queue/{key}/pop", summary="큐에서 추출 (LPOP/RPOP)")
async def pop_queue(
    ns: str,
    key: str,
    direction: Literal["left", "right"] = Query("left", description="pop 방향: left(FIFO) 또는 right(LIFO)"),
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    redis_key = build_key(ns, QUEUE_PREFIX, key)

    if direction == "right":
        value = await r.rpop(redis_key)
    else:
        value = await r.lpop(redis_key)

    if value is None:
        raise error("QUEUE_EMPTY", f"Queue '{key}' is empty", status=404)

    return ok({"value": value}, ns=ns, key=key, type="list")


@router.get("/ns/{ns}/queue/{key}", summary="범위 조회 (LRANGE)")
async def range_queue(
    ns: str,
    key: str,
    start: int = Query(0),
    stop: int = Query(-1),
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_read(client, ns)
    redis_key = build_key(ns, QUEUE_PREFIX, key)
    values = await r.lrange(redis_key, start, stop)
    return ok({"values": values, "count": len(values)}, ns=ns, key=key, type="list")


@router.get("/ns/{ns}/queue/{key}/len", summary="큐 길이 (LLEN)")
async def len_queue(
    ns: str,
    key: str,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_read(client, ns)
    redis_key = build_key(ns, QUEUE_PREFIX, key)
    length = await r.llen(redis_key)
    return ok({"length": length}, ns=ns, key=key, type="list")


@router.delete("/ns/{ns}/queue/{key}", summary="전체 삭제 (DEL)")
async def delete_queue(
    ns: str,
    key: str,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    redis_key = build_key(ns, QUEUE_PREFIX, key)
    deleted = await r.delete(redis_key)
    if not deleted:
        raise not_found(key, ns)
    return ok({"deleted": True}, ns=ns, key=key, type="list")


@router.put("/ns/{ns}/queue/{key}/touch", summary="TTL 갱신 (EXPIRE/PERSIST)")
async def touch_queue(
    ns: str,
    key: str,
    body: TtlSetRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    redis_key = build_key(ns, QUEUE_PREFIX, key)
    return await touch_key(r, redis_key, body.ttl, ns, key, "list")


@router.post("/ns/{ns}/queue/{key}/trim", summary="최근 N건만 유지 (LTRIM)")
async def trim_queue(
    ns: str,
    key: str,
    body: QueueTrimRequest,
    client: ClientInfo = Depends(get_client),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    require_write(client, ns)
    redis_key = build_key(ns, QUEUE_PREFIX, key)
    # 최근 keep건만 유지 (꼬리쪽 keep개 — 기본 RPUSH/LPOP FIFO에서 꼬리가 최신)
    await r.ltrim(redis_key, -body.keep, -1)
    length = await r.llen(redis_key)
    return ok({"kept": length, "length": length}, ns=ns, key=key, type="list")
