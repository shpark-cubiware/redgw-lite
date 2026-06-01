"""TTL 해석 유틸리티"""

from __future__ import annotations

from app.config import get_settings
from app.utils.response import not_found, ok


def resolve_ttl(body_ttl: int | None) -> int:
    """요청 TTL이 None이면 기본값을 반환한다."""
    if body_ttl is not None:
        return body_ttl
    return get_settings().defaults.ttl


async def touch_key(r, redis_key: str, ttl: int, ns: str, key: str, type_name: str) -> dict:
    """Touch 엔드포인트 공통 로직 — exists 확인 → expire/persist → ok 응답.

    exists·expire/persist·ttl을 단일 파이프라인으로 묶어 3 왕복 → 1 왕복.
    not_found 판정은 반드시 EXISTS 결과로 한다 — PERSIST는 '키 없음'과
    '키는 있으나 TTL 없음'을 둘 다 0으로 반환해 반환값만으로 구분할 수 없기 때문이다.
    없는 키에 expire/persist가 실행돼도 no-op이라 부작용은 없다(EXISTS로 걸러 raise).
    """
    async with r.pipeline(transaction=False) as pipe:
        pipe.exists(redis_key)
        if ttl > 0:
            pipe.expire(redis_key, ttl)
        else:
            pipe.persist(redis_key)
        pipe.ttl(redis_key)
        results = await pipe.execute()
    # not_found 판정: EXISTS(results[0]) 우선. 추가로 비트랜잭션 파이프라인이라
    # EXISTS=1 직후 다른 클라가 DEL/만료시키는 경합에선 TTL(results[-1])이 -2(미존재)가
    # 되는데, 이때 results[0]만 보면 200+meta.ttl=-2가 새어나간다 → -2면 not_found.
    # (정상: expire 분기는 양수, persist 분기는 -1. -2는 경합에서만 발생.)
    if not results[0] or results[-1] == -2:
        raise not_found(key, ns)
    return ok({"touched": True}, ns=ns, key=key, type=type_name, ttl=results[-1])
