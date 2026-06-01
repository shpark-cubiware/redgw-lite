"""관리 API — /admin/*"""

from __future__ import annotations

import asyncio
import logging
import time

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse

from app.auth.api_key import verify_api_key
from app.config import get_settings
from app.dependencies import get_redis
from app.schemas.admin import TtlSetRequest
from app.schemas.common import ClientInfo
from app.utils.key_builder import (
    STORAGE_PREFIXES,
    build_key,
    build_scan_pattern,
    parse_key,
    resolve_type_prefix,
    validate_ns,
)
from app.auth.namespace_guard import require_admin as _require_admin
from app.utils.response import error, ok_simple

logger = logging.getLogger("redgw.admin")

router = APIRouter()


@router.get("/systems", summary="등록된 시스템 목록")
async def list_systems() -> dict:
    """인증 불필요. 튜토리얼 UI용 시스템 클라이언트 목록 반환."""
    settings = get_settings()
    systems = sorted(cid for cid in settings.clients if cid != "MONITOR")
    return ok_simple({"systems": systems, "fixed": ["MONITOR", "admin"]})


# WHY: NS별 키 수 30초 TTL 캐시 — /metrics에서 매 scrape마다 SCAN하면 부하 과다
_ns_counts_cache: dict[str, int] = {}
_ns_counts_ts: float = 0.0
_NS_COUNTS_TTL: float = 30.0

# WHY: Prometheus /metrics 응답 5초 TTL 캐시 — scrape 간격 내 재생성 방지
_metrics_text_cache: str = ""
_metrics_text_ts: float = 0.0
_METRICS_TEXT_TTL: float = 5.0


async def _get_ns_counts_cached(r: aioredis.Redis) -> dict[str, int]:
    """NS별 키 수를 30초간 캐싱하여 반환. SCAN 부하를 줄인다."""
    global _ns_counts_cache, _ns_counts_ts
    now = time.time()
    if now - _ns_counts_ts < _NS_COUNTS_TTL:
        return _ns_counts_cache

    ns_counts: dict[str, int] = {}
    scanned = 0
    async for key in r.scan_iter(match="*", count=500):
        try:
            ns, prefix, _ = parse_key(key)
        except ValueError:
            scanned += 1
            if scanned >= 5000:
                break
            continue
        # 내부 제어/메트릭 키(__redgw:*, redgw:metrics:*)는 저장 접두어가 아니므로 제외
        if prefix in STORAGE_PREFIXES:
            ns_counts[ns] = ns_counts.get(ns, 0) + 1
        scanned += 1
        if scanned >= 5000:
            break

    _ns_counts_cache = ns_counts
    _ns_counts_ts = now
    return ns_counts


def _is_storage_key(redis_key: str) -> bool:
    """사용자 저장 키(3세그먼트 + 저장 접두어)면 True.

    내부 제어/메트릭 키(`__redgw:*`, `redgw:metrics:*`)는 콜론 3개 이상이라 parse_key는
    통과하지만 접두어가 STORAGE_PREFIXES에 없으므로 False로 걸러진다.
    """
    try:
        _, prefix, _ = parse_key(redis_key)
    except ValueError:
        return False
    return prefix in STORAGE_PREFIXES


@router.get("/admin/keys", summary="키 목록 조회")
async def list_keys(
    ns: str | None = Query(None, description="네임스페이스 필터"),
    pattern: str = Query("*", description="키 패턴"),
    type: str | None = Query(None, description="타입 필터 (string, hash, list, set, zset, stream)"),
    cursor: int = Query(0, description="페이지네이션 커서 (0=첫 페이지)"),
    count: int = Query(100, description="페이지 크기", ge=1, le=1000),
    client: ClientInfo = Depends(verify_api_key),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    # 입력 검증
    if ns:
        validate_ns(ns)

    search_pattern = build_scan_pattern(ns, type, pattern)
    next_cursor, keys = await r.scan(cursor=cursor, match=search_pattern, count=count)

    # 내부 제어/메트릭 키(__redgw:*, redgw:metrics:*)는 사용자 키가 아니므로 제외.
    # glob `*:*:*`가 3세그먼트 내부키도 매칭하기 때문에 결과 측에서 한 번 더 거른다.
    user_keys = [k for k in keys if _is_storage_key(k)]

    return ok_simple({
        "keys": sorted(user_keys),
        "count": len(user_keys),
        "cursor": next_cursor,
        "has_more": next_cursor != 0,
    })


@router.get("/admin/info/{ns}/{type}/{key:path}", summary="키 상세 정보")
async def key_info(
    ns: str,
    type: str,
    key: str,
    client: ClientInfo = Depends(verify_api_key),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    prefix = resolve_type_prefix(type)
    redis_key = build_key(ns, prefix, key)

    key_type = await r.type(redis_key)
    if key_type == "none":
        raise error("KEY_NOT_FOUND", f"Key not found: {redis_key}", status=404)

    ttl = await r.ttl(redis_key)
    encoding = await r.object("encoding", redis_key)

    # 타입별 크기/길이 정보
    size_info: dict = {}
    if key_type == "string":
        size_info["length"] = await r.strlen(redis_key)
    elif key_type == "hash":
        size_info["field_count"] = await r.hlen(redis_key)
    elif key_type == "list":
        size_info["length"] = await r.llen(redis_key)
    elif key_type == "set":
        size_info["member_count"] = await r.scard(redis_key)
    elif key_type == "zset":
        size_info["member_count"] = await r.zcard(redis_key)
    elif key_type == "stream":
        size_info["length"] = await r.xlen(redis_key)

    return ok_simple({
        "redis_key": redis_key,
        "type": key_type,
        "ttl": ttl,
        "encoding": encoding,
        **size_info,
    })


@router.put("/admin/ttl/{ns}/{type}/{key:path}", summary="TTL 설정/변경")
async def set_ttl(
    ns: str,
    type: str,
    key: str,
    body: TtlSetRequest,
    client: ClientInfo = Depends(verify_api_key),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    _require_admin(client)
    prefix = resolve_type_prefix(type)
    redis_key = build_key(ns, prefix, key)

    exists = await r.exists(redis_key)
    if not exists:
        raise error("KEY_NOT_FOUND", f"Key not found: {redis_key}", status=404)

    if body.ttl > 0:
        await r.expire(redis_key, body.ttl)
    else:
        await r.persist(redis_key)

    actual_ttl = await r.ttl(redis_key)
    return ok_simple({"redis_key": redis_key, "ttl": actual_ttl})


@router.delete("/admin/keys/bulk", summary="패턴 기반 일괄 삭제")
async def bulk_delete_keys(
    ns: str = Query(..., description="네임스페이스"),
    type: str | None = Query(None, description="타입 필터"),
    pattern: str = Query("*", description="키 패턴"),
    dry_run: bool = Query(False, description="True면 삭제 대상만 반환"),
    client: ClientInfo = Depends(verify_api_key),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    _require_admin(client)
    validate_ns(ns)

    search_pattern = build_scan_pattern(ns, type, pattern)

    keys_to_delete: list[str] = []
    truncated = False
    async for key in r.scan_iter(match=search_pattern, count=500):
        # 내부 제어/메트릭 키 보호 — list_keys와 동일 필터(ns=__redgw 등으로 내부키
        # 패턴에 도달해도 삭제 대상에서 제외).
        if not _is_storage_key(key):
            continue
        keys_to_delete.append(key)
        if len(keys_to_delete) >= 10000:
            truncated = True   # 1만 상한 도달 — 일부만 처리됨을 호출자에 알림(재실행 필요)
            break

    if dry_run:
        return ok_simple({"keys": sorted(keys_to_delete), "count": len(keys_to_delete), "truncated": truncated, "dry_run": True})

    deleted = 0
    for i in range(0, len(keys_to_delete), 100):
        batch = keys_to_delete[i:i + 100]
        deleted += await r.delete(*batch)

    return ok_simple({"deleted": deleted, "truncated": truncated, "dry_run": False})


@router.delete("/admin/keys/{ns}/{type}/{key:path}", summary="키 삭제")
async def delete_key(
    ns: str,
    type: str,
    key: str,
    client: ClientInfo = Depends(verify_api_key),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    _require_admin(client)
    prefix = resolve_type_prefix(type)
    redis_key = build_key(ns, prefix, key)

    deleted = await r.delete(redis_key)
    if not deleted:
        raise error("KEY_NOT_FOUND", f"Key not found: {redis_key}", status=404)
    return ok_simple({"redis_key": redis_key, "deleted": True})


@router.get("/admin/stats", summary="네임스페이스별 통계")
async def get_stats(
    limit: int = Query(10000, description="최대 스캔 키 수", ge=1, le=100000),
    client: ClientInfo = Depends(verify_api_key),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    # 전체 Redis info
    info = await r.info("memory")

    # 네임스페이스별 키 수 집계 (상한선 적용)
    ns_stats: dict[str, dict] = {}
    scanned = 0
    async for key in r.scan_iter(match="*", count=500):
        # scanned는 '반복한 전체 키 수' — limit이 반복 자체를 제한하도록 모든 키에서 증가시킨다.
        # (저장 키만 세면 내부키가 많을 때 cap이 반복을 못 막고 truncated도 부정확해진다.)
        scanned += 1
        try:
            ns, prefix, _ = parse_key(key)
        except ValueError:
            if scanned >= limit:
                break
            continue
        # 내부 제어/메트릭 키는 가짜 네임스페이스로 통계·Prometheus에 새지 않게 집계 제외
        if prefix in STORAGE_PREFIXES:
            if ns not in ns_stats:
                ns_stats[ns] = {"total_keys": 0, "by_type": {}}
            ns_stats[ns]["total_keys"] += 1
            ns_stats[ns]["by_type"][prefix] = ns_stats[ns]["by_type"].get(prefix, 0) + 1

        if scanned >= limit:
            break

    return ok_simple({
        "redis_memory": {
            "used_memory_human": info.get("used_memory_human", "N/A"),
            "used_memory_peak_human": info.get("used_memory_peak_human", "N/A"),
        },
        "namespaces": ns_stats,
        "scanned_keys": scanned,
        "truncated": scanned >= limit,
    })


@router.get("/admin/clients", summary="클라이언트 목록 및 API Key 조회")
async def list_clients(
    client: ClientInfo = Depends(verify_api_key),
) -> dict:
    """등록된 클라이언트 목록과 API Key를 반환한다. (admin 전용)"""
    _require_admin(client)
    settings = get_settings()

    clients = {}
    for cid, cfg in settings.clients.items():
        clients[cid] = {
            "api_key": _mask_key(cfg.api_key),
            "description": cfg.description,
            "namespaces": cfg.namespaces,
        }

    # admin 키도 포함
    if settings.admin.api_key:
        clients["admin"] = {
            "api_key": _mask_key(settings.admin.api_key),
            "description": "Administrator",
            "namespaces": {"*": ["read", "write"]},
        }

    return ok_simple({"clients": clients})


@router.get("/admin/metrics", summary="서비스 메트릭")
async def service_metrics(
    client: ClientInfo = Depends(verify_api_key),
) -> dict:
    _require_admin(client)
    from app.utils.metrics import get_metrics_async
    return ok_simple(await get_metrics_async())


@router.get("/metrics", summary="Prometheus 형식 메트릭", response_class=PlainTextResponse)
async def prometheus_metrics(
    client: ClientInfo = Depends(verify_api_key),
    r: aioredis.Redis = Depends(get_redis),
) -> str:
    _require_admin(client)

    # 5초 TTL 캐시 — 동일 scrape 간격 내 재생성 방지
    global _metrics_text_cache, _metrics_text_ts
    now = time.time()
    if _metrics_text_cache and now - _metrics_text_ts < _METRICS_TEXT_TTL:
        return _metrics_text_cache

    from app.utils.metrics import get_metrics_async
    m = await get_metrics_async()

    # Redis 정보 (memory + stats + clients)
    info = await r.info("memory")
    stats = await r.info("stats")
    clients_info = await r.info("clients")

    used_bytes = info.get("used_memory", 0)
    maxmem = info.get("maxmemory", 0)

    # NS별 키 수 (간이 통계, 최대 5000개 스캔, 30초 캐싱)
    ns_counts = await _get_ns_counts_cached(r)

    lines: list[str] = []

    # ── 요청 카운터 (HTTP status별) ─────────────────
    lines += [
        "# HELP redgw_requests_total Total requests by HTTP status",
        "# TYPE redgw_requests_total counter",
    ]
    for code, count in m["status_codes"].items():
        lines.append(f'redgw_requests_total{{status="{code}"}} {count}')

    # ── Latency histogram ─────────────────────────
    lat = m.get("latency", {})
    if lat.get("count", 0) > 0:
        lines += [
            "# HELP redgw_request_duration_ms Request latency histogram (ms)",
            "# TYPE redgw_request_duration_ms histogram",
        ]
        # 각 버킷은 이미 누적값 (record_request_async에서 le 이하 전부 증가)
        for label, cnt in lat.get("buckets", {}).items():
            lines.append(f'redgw_request_duration_ms_bucket{{le="{label}"}} {cnt}')
        lines.append(f"redgw_request_duration_ms_sum {lat.get('sum', 0)}")
        lines.append(f"redgw_request_duration_ms_count {lat.get('count', 0)}")

    # ── Per-namespace 요청 수 ─────────────────────
    ns_reqs = m.get("ns_requests", {})
    if ns_reqs:
        lines += [
            "# HELP redgw_ns_requests_total Requests per namespace",
            "# TYPE redgw_ns_requests_total counter",
        ]
        for ns, count in ns_reqs.items():
            lines.append(f'redgw_ns_requests_total{{ns="{ns}"}} {count}')

    # ── Per-client 요청 수 ────────────────────────
    client_reqs = m.get("client_requests", {})
    if client_reqs:
        lines += [
            "# HELP redgw_client_requests_total Requests per client",
            "# TYPE redgw_client_requests_total counter",
        ]
        for cid, count in client_reqs.items():
            lines.append(f'redgw_client_requests_total{{client="{cid}"}} {count}')

    # ── 인증·권한 실패 카운터 (보안 모니터링) ─────
    # 수집은 되나(get_metrics_async) 그동안 Prometheus 텍스트에는 누락돼 있었다.
    # reason: auth_fail | namespace_denied | admin_required
    auth_failures = m.get("auth_failures", {})
    if auth_failures:
        lines += [
            "# HELP redgw_auth_failures_total Authentication/authorization failures by reason",
            "# TYPE redgw_auth_failures_total counter",
        ]
        for reason, count in auth_failures.items():
            lines.append(f'redgw_auth_failures_total{{reason="{reason}"}} {count}')

    # ── 빌드 정보 (Rust/Python 구현 표시) ────────
    from app.utils.key_builder import _USE_RUST as _kb_rust
    from app.utils.validation import _USE_RUST as _val_rust
    _core_impl = "rust" if (_kb_rust and _val_rust) else "python"
    lines += [
        "# HELP redgw_build_info Build information",
        "# TYPE redgw_build_info gauge",
        f'redgw_build_info{{core_engine="{_core_impl}"}} 1',
    ]

    # ── 서비스 업타임 ─────────────────────────────
    lines += [
        "# HELP redgw_uptime_seconds Service uptime",
        "# TYPE redgw_uptime_seconds gauge",
        f"redgw_uptime_seconds {m['uptime_seconds']}",
    ]

    # ── Redis 메모리 ──────────────────────────────
    lines += [
        "# HELP redgw_redis_memory_bytes Redis used memory",
        "# TYPE redgw_redis_memory_bytes gauge",
        f"redgw_redis_memory_bytes {used_bytes}",
    ]
    if maxmem:
        lines += [
            "# HELP redgw_redis_memory_limit_bytes Redis maxmemory",
            "# TYPE redgw_redis_memory_limit_bytes gauge",
            f"redgw_redis_memory_limit_bytes {maxmem}",
        ]

    # ── Redis 운영 통계 ───────────────────────────
    ops_per_sec = stats.get("instantaneous_ops_per_sec", 0)
    keyspace_hits = stats.get("keyspace_hits", 0)
    keyspace_misses = stats.get("keyspace_misses", 0)
    evicted_keys = stats.get("evicted_keys", 0)
    connected_clients = clients_info.get("connected_clients", 0)

    lines += [
        "# HELP redgw_redis_ops_per_sec Redis instantaneous ops/sec",
        "# TYPE redgw_redis_ops_per_sec gauge",
        f"redgw_redis_ops_per_sec {ops_per_sec}",
        "# HELP redgw_redis_keyspace_hits_total Redis keyspace hits",
        "# TYPE redgw_redis_keyspace_hits_total counter",
        f"redgw_redis_keyspace_hits_total {keyspace_hits}",
        "# HELP redgw_redis_keyspace_misses_total Redis keyspace misses",
        "# TYPE redgw_redis_keyspace_misses_total counter",
        f"redgw_redis_keyspace_misses_total {keyspace_misses}",
        "# HELP redgw_redis_evicted_keys_total Redis evicted keys",
        "# TYPE redgw_redis_evicted_keys_total counter",
        f"redgw_redis_evicted_keys_total {evicted_keys}",
        "# HELP redgw_redis_connected_clients Redis connected clients",
        "# TYPE redgw_redis_connected_clients gauge",
        f"redgw_redis_connected_clients {connected_clients}",
    ]

    # ── NS별 키 수 ────────────────────────────────
    lines += [
        "# HELP redgw_ns_keys_total Keys per namespace (sampled)",
        "# TYPE redgw_ns_keys_total gauge",
    ]
    for ns, count in ns_counts.items():
        lines.append(f'redgw_ns_keys_total{{ns="{ns}"}} {count}')



    result = "\n".join(lines) + "\n"
    _metrics_text_cache = result
    _metrics_text_ts = now
    return result


def _mask_key(key: str, visible: int = 8) -> str:
    """API 키의 앞부분만 노출하고 나머지를 마스킹한다."""
    if len(key) <= 4:
        return "****"
    if len(key) <= visible:
        return key[:4] + "****"
    return key[:visible] + "***"


# ─────────────────────────────────────────────────────────────
# Audit 운영 상세 — Q1=(b) 결정에 따라 admin 권한 뒤에서만 노출
# ─────────────────────────────────────────────────────────────
# 디스크 스캔 30s TTL 캐시 — 모니터링 폴링(예: 10s)에서 디렉토리 스캔 부하 절감
_health_detail_cache: dict = {"ts": 0.0, "payload": None}
_HEALTH_DETAIL_TTL = 30.0


def _compute_audit_disk_stats(audit_cfg) -> dict:
    """감사 디렉토리 디스크 용량·파일 수 계산.

    shutil.disk_usage·iterdir·stat은 블로킹 I/O이므로 async 핸들러에서 직접 호출하면
    느린 스토리지(NFS 등)·파일 다수 시 이벤트 루프를 막는다. run_in_executor로 오프로드한다.
    """
    none_all = {
        "total_gb": None, "used_gb": None, "free_gb": None,
        "free_pct": None, "current_usage_mb": None, "file_count": None,
    }
    if audit_cfg.level <= 0:
        return none_all
    try:
        import shutil
        from pathlib import Path
        target = Path(audit_cfg.directory)
        if not target.exists():
            return {
                "total_gb": None, "used_gb": None, "free_gb": None,
                "free_pct": None, "current_usage_mb": 0, "file_count": 0,
            }
        total, used, free = shutil.disk_usage(str(target))
        file_count = 0
        bytes_total = 0
        for f in target.iterdir():
            if f.is_file() and f.name.startswith("audit."):
                try:
                    bytes_total += f.stat().st_size
                    file_count += 1
                except OSError:
                    continue
        return {
            "total_gb": round(total / (1024**3), 2),
            "used_gb": round(used / (1024**3), 2),
            "free_gb": round(free / (1024**3), 2),
            "free_pct": round(free / total * 100, 1) if total > 0 else None,
            "current_usage_mb": round(bytes_total / (1024**2), 2),
            "file_count": file_count,
        }
    except Exception:
        return none_all


@router.get("/admin/health-detail", summary="감사 로그 디스크·큐 상세 상태")
async def admin_health_detail(client: ClientInfo = Depends(verify_api_key)):
    """디스크 사용량·큐 적체·설정 등 audit 운영 상세.

    `/health`는 공개 상태(`audit_level` 1필드)만 노출하고, 상세 정보는
    여기서만 노출한다. admin 권한 필수.
    """
    _require_admin(client)

    # TTL 캐시 — queue_size만 매번 갱신, 나머지 디스크 스캔은 30s 캐시
    now = time.time()
    cached = _health_detail_cache.get("payload")
    if cached and (now - _health_detail_cache["ts"]) < _HEALTH_DETAIL_TTL:
        # 공유 캐시 객체를 in-place 변이하지 않고 얕은 복사 후 갱신(가변 전역 위생).
        result = dict(cached)
        try:
            from app.audit import get_queue_size
            result["queue_size"] = get_queue_size()
        except Exception:
            result["queue_size"] = None
        return result

    settings = get_settings()
    audit_cfg = settings.audit

    payload: dict = {
        "audit_level": audit_cfg.level,
        "audit_directory": audit_cfg.directory if audit_cfg.level > 0 else None,
        "max_total_size_mb": audit_cfg.max_total_size_mb,
        "retention_days": audit_cfg.retention_days,
        "per_worker_files": audit_cfg.per_worker_files,
        "payload_prefix_bytes": audit_cfg.payload_prefix_bytes,
        "exclude_paths": list(audit_cfg.exclude_paths),
    }

    # 큐 적체 + 워커별 드롭 카운터 (모니터링용)
    try:
        from app.audit import get_queue_size, get_dropped_counts
        payload["queue_size"] = get_queue_size()
        payload["dropped_local"] = get_dropped_counts()  # {queue_full, disk_full}
    except Exception:
        payload["queue_size"] = None
        payload["dropped_local"] = {"queue_full": 0, "disk_full": 0}

    # 디스크 capacity + 사용량 — 블로킹 I/O는 executor로 오프로드(이벤트 루프 보호)
    loop = asyncio.get_running_loop()
    payload.update(
        await loop.run_in_executor(None, _compute_audit_disk_stats, audit_cfg)
    )

    _health_detail_cache["ts"] = now
    _health_detail_cache["payload"] = payload
    return payload


@router.get(
    "/admin/audit/origin/{ns}/{type}/{key:path}",
    summary="Audit Correlation — 키의 origin 메타 조회 (Q3)",
)
async def admin_audit_origin(
    ns: str,
    type: str,
    key: str,
    r: aioredis.Redis = Depends(get_redis),
    client: ClientInfo = Depends(verify_api_key),
):
    """Audit Correlation이 활성(`audit.correlation: true`)일 때 origin 메타 조회.

    원본 API 요청의 `request_id`·`client_id`·`ts`를 반환. 폐쇄망 운영자가
    공유 키의 변경을 원본 API 호출로 추적할 때 사용.

    Level=0 또는 correlation 비활성 시에도 엔드포인트는 응답 (origin이 없어
    404). 항상 admin 권한 필요.
    """
    _require_admin(client)

    # 타입 → 저장 접두어는 단일 소스 resolve_type_prefix로 통일(중복 맵 제거).
    # 과거 로컬 맵은 list/set/stream을 queue/group/event로 만들어 저장 접두어(q/grp/evt)와
    # 어긋난 키를 조회했다 — 쓰기 경로(build_key)와 불일치하던 잠복버그를 함께 해소.
    type_prefix = resolve_type_prefix(type)
    redis_key = build_key(ns, type_prefix, key)
    origin_key = f"__redgw:audit:origin:{redis_key}"

    raw = await r.get(origin_key)
    if raw is None:
        raise error(
            "KEY_NOT_FOUND",
            f"No audit origin for {ns}:{type_prefix}:{key} (correlation disabled or expired)",
            status=404,
        )

    import json as _json
    try:
        payload = _json.loads(raw)
    except _json.JSONDecodeError:
        payload = {"raw": raw}

    ttl = await r.ttl(origin_key)
    payload["ttl_seconds"] = ttl if ttl >= 0 else None
    payload["redis_key"] = redis_key
    return payload
