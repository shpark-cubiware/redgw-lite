"""FastAPI 의존성 주입 모듈"""

from __future__ import annotations

import redis.asyncio as aioredis
from fastapi import Depends

from app.auth.api_key import verify_api_key
from app.redis_client import get_redis_manager
from app.schemas.common import ClientInfo


async def get_redis() -> aioredis.Redis:
    """Redis 클라이언트 의존성"""
    return get_redis_manager().get_client()


async def get_client(
    client: ClientInfo = Depends(verify_api_key),
) -> ClientInfo:
    """인증된 클라이언트 의존성.
    verify_api_key를 직접 래핑 — 테스트 시 app.dependency_overrides로 mock 주입 용이."""
    return client
