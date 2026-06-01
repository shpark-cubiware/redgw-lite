"""요청 메트릭 수집 (Redis 기반, 멀티워커 안전)

Gunicorn은 fork()로 worker를 생성하므로 인메모리 dict는 worker별로 독립된다.
Redis INCR을 사용하여 모든 worker의 메트릭을 단일 소스로 집계한다.
메트릭 키는 redgw:metrics: 접두어를 사용하며, 운영 데이터와 충돌하지 않는다.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("redgw.metrics")

_started_at: float = time.time()

# Redis 키 접두어 (운영 데이터와 분리)
_PREFIX = "redgw:metrics"
_TOTAL_KEY = f"{_PREFIX}:total_requests"

# Latency 버킷 (밀리초 기준, Prometheus histogram 호환)
_LATENCY_BUCKETS = [5, 10, 25, 50, 100, 250, 500, 1000, 5000]


def _status_key(status_code: int) -> str:
    return f"{_PREFIX}:status:{status_code}"


def _ns_req_key(ns: str) -> str:
    return f"{_PREFIX}:ns:{ns}"


def _client_req_key(client_id: str) -> str:
    return f"{_PREFIX}:client:{client_id}"


def _latency_bucket_key(bucket: int | str) -> str:
    return f"{_PREFIX}:latency:{bucket}"


def _auth_fail_key(reason: str) -> str:
    return f"{_PREFIX}:auth_failures:{reason}"


def _audit_dropped_key(cause: str) -> str:
    return f"{_PREFIX}:audit_dropped:{cause}"


async def record_auth_failure_async(reason: str) -> None:
    """인증·권한 실패 카운터 (reason 라벨).

    reason: auth_fail | namespace_denied | admin_required
    (rate limit은 Nginx 10r/s가 앱 도달 전 429로 차단 — 앱은 이 카운터를 올리지 않음)
    """
    try:
        from app.redis_client import get_redis_manager
        r = get_redis_manager().get_client()
        await r.incr(_auth_fail_key(reason))
    except Exception:
        logger.debug("Auth failure metric record failed", exc_info=True)


async def record_audit_dropped_async(cause: str) -> None:
    """Audit 라인 드롭 카운터 (cause 라벨).

    cause: disk_full | queue_full | level_zero
    """
    try:
        from app.redis_client import get_redis_manager
        r = get_redis_manager().get_client()
        await r.incr(_audit_dropped_key(cause))
    except Exception:
        logger.debug("Audit dropped metric record failed", exc_info=True)


async def record_request_async(
    status_code: int,
    duration_ms: float = 0.0,
    namespace: str = "",
    client_id: str = "",
) -> None:
    """요청 완료 시 비동기 호출 — Redis INCR로 전 worker 공유 집계."""
    try:
        from app.redis_client import get_redis_manager
        r = get_redis_manager().get_client()
        pipe = r.pipeline(transaction=False)
        pipe.incr(_TOTAL_KEY)
        pipe.incr(_status_key(status_code))

        # Per-namespace 요청 수
        if namespace:
            pipe.incr(_ns_req_key(namespace))

        # Per-client 요청 수
        if client_id:
            pipe.incr(_client_req_key(client_id))

        # Prometheus cumulative histogram: 각 le 버킷은 해당 값 이하인 모든 관측치의 누적 카운트
        if duration_ms > 0:
            pipe.incrbyfloat(f"{_PREFIX}:latency_sum", duration_ms)
            pipe.incr(f"{_PREFIX}:latency_count")
            for bucket in _LATENCY_BUCKETS:
                if duration_ms <= bucket:
                    pipe.incr(_latency_bucket_key(bucket))
            # +Inf 버킷 (항상 증가)
            pipe.incr(_latency_bucket_key("inf"))

        await pipe.execute()
    except Exception:
        logger.debug("Metrics record failed", exc_info=True)


async def get_metrics_async() -> dict:
    """현재 메트릭 스냅샷 반환 (전 worker 합산)."""
    try:
        from app.redis_client import get_redis_manager
        r = get_redis_manager().get_client()

        total = await r.get(_TOTAL_KEY)

        # status:* 키 조회
        status_keys: list[str] = []
        async for key in r.scan_iter(match=f"{_PREFIX}:status:*", count=500):
            status_keys.append(key)

        status_codes: dict[str, int] = {}
        if status_keys:
            values = await r.mget(*status_keys)
            for k, v in zip(status_keys, values):
                code = k.rsplit(":", 1)[-1]
                status_codes[code] = int(v or 0)

        # Per-namespace 요청 수
        ns_keys: list[str] = []
        async for key in r.scan_iter(match=f"{_PREFIX}:ns:*", count=500):
            ns_keys.append(key)
        ns_requests: dict[str, int] = {}
        if ns_keys:
            values = await r.mget(*ns_keys)
            for k, v in zip(ns_keys, values):
                ns = k.rsplit(":", 1)[-1]
                ns_requests[ns] = int(v or 0)

        # Per-client 요청 수
        client_keys: list[str] = []
        async for key in r.scan_iter(match=f"{_PREFIX}:client:*", count=500):
            client_keys.append(key)
        client_requests: dict[str, int] = {}
        if client_keys:
            values = await r.mget(*client_keys)
            for k, v in zip(client_keys, values):
                cid = k.rsplit(":", 1)[-1]
                client_requests[cid] = int(v or 0)

        # Latency histogram
        latency_sum = await r.get(f"{_PREFIX}:latency_sum")
        latency_count = await r.get(f"{_PREFIX}:latency_count")
        latency_buckets: dict[str, int] = {}
        bucket_keys = [_latency_bucket_key(b) for b in _LATENCY_BUCKETS] + [
            _latency_bucket_key("inf")
        ]
        values = await r.mget(*bucket_keys)
        labels = [str(b) for b in _LATENCY_BUCKETS] + ["+Inf"]
        for label, v in zip(labels, values):
            latency_buckets[label] = int(v or 0)

        # 인증·권한 실패 카운터 (reason 라벨)
        auth_fail_keys: list[str] = []
        async for key in r.scan_iter(match=f"{_PREFIX}:auth_failures:*", count=200):
            auth_fail_keys.append(key)
        auth_failures: dict[str, int] = {}
        if auth_fail_keys:
            af_values = await r.mget(*auth_fail_keys)
            for k, v in zip(auth_fail_keys, af_values):
                reason = k.rsplit(":", 1)[-1]
                auth_failures[reason] = int(v or 0)

        # Audit 드롭 카운터 (cause 라벨)
        audit_drop_keys: list[str] = []
        async for key in r.scan_iter(match=f"{_PREFIX}:audit_dropped:*", count=200):
            audit_drop_keys.append(key)
        audit_dropped: dict[str, int] = {}
        if audit_drop_keys:
            ad_values = await r.mget(*audit_drop_keys)
            for k, v in zip(audit_drop_keys, ad_values):
                cause = k.rsplit(":", 1)[-1]
                audit_dropped[cause] = int(v or 0)

        return {
            "total_requests": int(total or 0),
            "status_codes": status_codes,
            "uptime_seconds": round(time.time() - _started_at, 1),
            "ns_requests": ns_requests,
            "client_requests": client_requests,
            "latency": {
                "sum": float(latency_sum or 0),
                "count": int(latency_count or 0),
                "buckets": latency_buckets,
            },
            "auth_failures": auth_failures,
            "audit_dropped": audit_dropped,
        }
    except Exception as e:
        logger.debug("Failed to read metrics: %s", e)
        return {
            "total_requests": 0,
            "status_codes": {},
            "uptime_seconds": round(time.time() - _started_at, 1),
            "ns_requests": {},
            "client_requests": {},
            "latency": {"sum": 0, "count": 0, "buckets": {}},
            "auth_failures": {},
            "audit_dropped": {},
        }
