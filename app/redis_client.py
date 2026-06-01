"""Redis 연결 풀 관리 모듈"""

from __future__ import annotations

import asyncio
import logging

import redis.asyncio as aioredis

from app.config import Settings

logger = logging.getLogger("redgw.redis")


class RedisManager:
    """redis.asyncio 기반 연결 풀 관리"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: aioredis.Redis | None = None

    async def connect(self, max_retries: int = 3, retry_delay: float = 2.0) -> bool:
        """연결 풀 초기화. 실패 시 재시도. 성공 여부 반환."""
        cfg = self._settings.redis
        for attempt in range(1, max_retries + 1):
            client = None
            try:
                client = aioredis.from_url(
                    cfg.url,
                    max_connections=cfg.max_connections,
                    decode_responses=cfg.decode_responses,
                    socket_timeout=cfg.socket_timeout,
                    retry_on_timeout=cfg.retry_on_timeout,
                )
                if await client.ping():
                    self._client = client
                    return True
                await client.aclose()
            except Exception as e:
                logger.warning(
                    "Redis connect attempt %d/%d failed: %s", attempt, max_retries, e
                )
                # ping 중 소켓이 열린 뒤 예외 시 클라이언트 자원 누수 방지
                if client is not None:
                    try:
                        await client.aclose()
                    except Exception:
                        pass
                if attempt < max_retries:
                    await asyncio.sleep(retry_delay)
        return False

    async def disconnect(self) -> None:
        """연결 풀 종료"""
        if self._client:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    async def ping(self) -> bool:
        """헬스체크"""
        if not self._client:
            return False
        try:
            return await self._client.ping()
        except Exception:
            return False

    def get_client(self) -> aioredis.Redis:
        """Redis 클라이언트 반환"""
        if not self._client:
            raise RuntimeError("Redis is not connected. Call connect() first.")
        return self._client


# WHY: 연결 풀 싱글턴 — lifespan에서 init_redis_manager()로 초기화, 전 라우터에서 공유
_manager: RedisManager | None = None


def get_redis_manager() -> RedisManager:
    if _manager is None:
        raise RuntimeError("RedisManager not initialized")
    return _manager


def init_redis_manager(settings: Settings) -> RedisManager:
    global _manager
    _manager = RedisManager(settings)
    return _manager
