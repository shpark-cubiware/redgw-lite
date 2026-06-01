"""리더 선출 (Redis SETNX 기반) 동시성 시나리오 테스트.

gunicorn 4 workers 중 1개만 STATUS를 기록하는 핵심 보장. 실제 동시 프로세스는
띄우지 않고, 동일 프로세스 내에서 여러 client가 SETNX를 경합하는
시나리오로 단일 점유를 검증한다.

검증 항목:
- SETNX(nx=True, ex=TTL)은 동시에 1명만 성공한다
- TTL 만료 후 다음 SETNX가 다시 성공한다
- leader가 EXPIRE로 TTL을 갱신할 수 있다
"""

from __future__ import annotations

import asyncio

import pytest


_LEADER_KEY = "__redgw:test_leader:lock"


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_setnx_only_one_wins():
    """동시에 N개 client가 SETNX 시도해도 1명만 성공."""
    from app.redis_client import get_redis_manager

    r = get_redis_manager().get_client()
    await r.delete(_LEADER_KEY)

    async def try_acquire(worker_id: str) -> bool:
        return bool(await r.set(_LEADER_KEY, worker_id, nx=True, ex=30))

    results = await asyncio.gather(
        try_acquire("w1"), try_acquire("w2"),
        try_acquire("w3"), try_acquire("w4"),
    )

    # 정확히 1명만 True
    assert sum(results) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_setnx_fails_when_already_held():
    """이미 leader가 있으면 새 시도는 실패."""
    from app.redis_client import get_redis_manager

    r = get_redis_manager().get_client()
    await r.delete(_LEADER_KEY)

    first = await r.set(_LEADER_KEY, "owner", nx=True, ex=30)
    assert bool(first) is True

    second = await r.set(_LEADER_KEY, "intruder", nx=True, ex=30)
    assert bool(second) is False

    # 점유자가 그대로
    held = await r.get(_LEADER_KEY)
    assert held == "owner"


@pytest.mark.asyncio(loop_scope="session")
async def test_leader_can_refresh_ttl_with_expire():
    """leader는 EXPIRE로 TTL을 갱신할 수 있다 (실제 갱신 루프 패턴)."""
    from app.redis_client import get_redis_manager

    r = get_redis_manager().get_client()
    await r.delete(_LEADER_KEY)

    await r.set(_LEADER_KEY, "leader_pid_42", nx=True, ex=5)
    initial_ttl = await r.ttl(_LEADER_KEY)
    assert 1 <= initial_ttl <= 5

    refreshed = await r.expire(_LEADER_KEY, 60)
    assert refreshed in (1, True)
    new_ttl = await r.ttl(_LEADER_KEY)
    assert 50 <= new_ttl <= 60


@pytest.mark.asyncio(loop_scope="session")
async def test_ttl_expiry_releases_lock():
    """짧은 TTL이 만료되면 다음 SETNX가 다시 성공."""
    from app.redis_client import get_redis_manager

    r = get_redis_manager().get_client()
    await r.delete(_LEADER_KEY)

    # 1초 TTL로 점유
    await r.set(_LEADER_KEY, "shortlived", nx=True, ex=1)
    # TTL 만료 대기
    await asyncio.sleep(1.2)

    # 새 SETNX 성공해야
    new = await r.set(_LEADER_KEY, "successor", nx=True, ex=30)
    assert bool(new) is True

    held = await r.get(_LEADER_KEY)
    assert held == "successor"


@pytest.mark.asyncio(loop_scope="session")
async def test_setnx_with_same_value_still_fails():
    """동일 값으로 SETNX 시도해도 nx 제약이 우선 — 실패."""
    from app.redis_client import get_redis_manager

    r = get_redis_manager().get_client()
    await r.delete(_LEADER_KEY)

    assert bool(await r.set(_LEADER_KEY, "v", nx=True, ex=30)) is True
    assert bool(await r.set(_LEADER_KEY, "v", nx=True, ex=30)) is False



