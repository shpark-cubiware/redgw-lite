"""FastAPI 앱 초기화, 라우터 등록"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings, validate_config
from app.redis_client import get_redis_manager, init_redis_manager
from app.routers import (
    kv, map as map_router, queue, group, rank, event, pubsub, admin,
)


logger = logging.getLogger("redgw")

# 앱 버전 — 단일 소스는 resource/pyproject.toml. 빌드 시 Dockerfile ARG/ENV(REDGW_VERSION)로
# 주입된다(build.sh가 pyproject에서 추출). 앱은 pip 설치 대상이 아니라 importlib.metadata로는
# 못 읽으므로 환경변수 경로를 쓴다. 미설정(로컬 dev) 시 "dev".
APP_VERSION = os.getenv("REDGW_VERSION", "dev")

_STATUS_INTERVAL = 600  # 10분
# STATUS 단일 로거 선출 락 — gunicorn 멀티워커 중 매 주기 1개만 기록한다.
# TTL은 인터벌보다 짧게(다음 주기 재경쟁) → 락 보유 워커 사망 시 자동 인수.
_STATUS_LEADER_KEY = "__redgw:status_monitor:tick"
_STATUS_LEADER_TTL = int(_STATUS_INTERVAL * 0.8)
_monitor_shutdown: asyncio.Event | None = None


def _ngettext(n: int, singular: str, plural: str) -> str:
    """단수/복수 선택 헬퍼."""
    return singular if n == 1 else plural


async def _emit_status_line(r) -> None:
    """STATUS 한 줄(Redis 메모리/keys 등 서비스 상태)을 구성·기록한다.

    호출부(_status_monitor_loop)에서 단일 로거 락을 선출한 뒤에만 호출한다.
    """
    from app.utils.file_logger import write_log_async

    # Redis 메모리
    info = await r.info("memory")
    used_mb = round(info.get("used_memory", 0) / 1024 / 1024, 1)
    maxmem = info.get("maxmemory", 0)
    limit_mb = round(maxmem / 1024 / 1024, 1) if maxmem else 0
    usage_pct = round(used_mb / limit_mb * 100, 1) if limit_mb else 0

    # 키 수
    keys = await r.dbsize()



    level = "W" if usage_pct >= 80 else "I"
    line = (
        f"Redis {used_mb}/{limit_mb} MB ({usage_pct}%)"
        f" | keys: {keys}"

    )
    await write_log_async("STATUS", line, level=level)


async def _status_monitor_loop() -> None:
    """10분 간격 Redis 메모리 + 프로세스 상태 로깅 (멀티워커 중 1개만 기록).

    gunicorn 멀티워커 환경에서 모든 워커가 이 루프를 돌지만, 매 주기 Redis NX 락
    (_STATUS_LEADER_KEY, TTL<interval)을 잡은 단일 워커만 STATUS를 기록한다 — 워커
    수만큼 중복되던 로그를 1줄로 줄인다. 락 보유 워커가 죽으면 TTL 만료 후 다음
    주기에 다른 워커가 자동 인수한다. (이전 startup flock 게이트는 컨테이너 환경에서
    워커 간 배타에 실패해 STATUS가 워커 수만큼 중복됐다.)
    """
    global _monitor_shutdown
    if _monitor_shutdown is None:
        _monitor_shutdown = asyncio.Event()

    while not _monitor_shutdown.is_set():
        try:
            r = get_redis_manager().get_client()
            # 단일 로거 선출 — NX 락을 잡은 워커만 이번 주기 STATUS를 기록한다.
            if await r.set(_STATUS_LEADER_KEY, str(os.getpid()), nx=True, ex=_STATUS_LEADER_TTL):
                await _emit_status_line(r)
        except Exception:
            pass  # Redis 장애 시 무시 — 헬스체크 로그로 충분

        # 인터럽트 가능 대기 (shutdown_event 설정 시 즉시 종료)
        try:
            await asyncio.wait_for(_monitor_shutdown.wait(), timeout=_STATUS_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """앱 시작/종료 시 Redis 연결 관리 + 설정 검증 + 통합 로깅"""
    settings = get_settings()

    # ★ (1) 통합 파일 로거 초기화 — 모든 워커에서 실행
    from app.utils.file_logger import (
        init_file_logger, write_log,
        acquire_startup_lock, release_startup_lock, is_startup_logger,
    )
    init_file_logger()

    # ★ (1.1) Audit 로거 초기화 — 모든 워커에서 실행. Level=0이면 No-op.
    # 디렉토리 생성 실패 등 OSError가 lifespan 전체를 막지 않도록 보호.
    # 실패 시 audit 미들웨어는 logger=None 감지로 passthrough.
    try:
        from app.audit import init_audit_logger
        init_audit_logger(settings.audit)
    except Exception as _audit_init_err:
        logger.error("Audit logger init failed: %s — continuing without audit", _audit_init_err)

    # ★ (1.5) 시작 로그 중복 방지 — 첫 번째 워커만 시작 시퀀스 기록
    # gunicorn 4 workers가 동시에 lifespan을 실행하므로,
    # fcntl.flock 기반 비차단 락으로 1개 워커만 MAIN/CONFIG 로그를 기록한다.
    acquire_startup_lock()

    if is_startup_logger():
        _banner = [
            "██████╗ ███████╗██████╗  ██████╗ ██╗    ██╗",
            "██╔══██╗██╔════╝██╔══██╗██╔════╝ ██║    ██║",
            "██████╔╝█████╗  ██║  ██║██║  ███╗██║ █╗ ██║",
            "██╔══██╗██╔══╝  ██║  ██║██║   ██║██║███╗██║",
            "██║  ██║███████╗██████╔╝╚██████╔╝╚███╔███╔╝",
            "╚═╝  ╚═╝╚══════╝╚═════╝  ╚═════╝  ╚══╝╚══╝",
            f" v{APP_VERSION}".ljust(28) + "Cubiware Co., Ltd.",
            "",
        ]
        for line in _banner:
            write_log("MAIN", line)

        # Rust/Python 구현 표시
        from app.utils.key_builder import _USE_RUST as _KB_RUST
        from app.utils.validation import _USE_RUST as _VAL_RUST
        _impl = "Rust (redgw_core)" if (_KB_RUST and _VAL_RUST) else "Python (fallback)"
        write_log("MAIN", f"Core engine: {_impl}")

        # ★ Audit 시작 배너 — 함수로 추출 (app/audit/banner.py)해 단위 테스트 가능
        from app.audit.banner import format_audit_banner
        _audit_cfg = settings.audit
        for _bline in format_audit_banner(_audit_cfg):
            _bl_level = "W" if _bline.startswith("***") or "WARNING" in _bline or "LOW DISK" in _bline else "I"
            write_log("MAIN", _bline, level=_bl_level)

    # ★ (2) 기존 설정 검증 (유지 — 모든 워커에서 실행)
    warnings = validate_config(settings)
    for w in warnings:
        logger.warning("CONFIG: %s", w)

    # ★ (3) 통합 설정 검증 — 모든 워커에서 검증, 첫 워커만 redgw.log 기록
    from app.config_validator import validate_all_configs, log_validation_report
    report = validate_all_configs()



    if is_startup_logger():
        log_validation_report(report)
    if report.has_fatal:
        if is_startup_logger():
            write_log("MAIN", "FATAL configuration errors — process terminated", level="F")
        raise SystemExit("FATAL configuration errors — see log/redgw.log")

    # ★ (4) Redis 연결 (기존 + write_log 추가)
    manager = init_redis_manager(settings)
    connected = await manager.connect(max_retries=3, retry_delay=2.0)
    if connected:
        redis_addr = settings.redis.url.rsplit("@", 1)[-1]
        logger.info("Redis connected: %s", redis_addr)
        if is_startup_logger():
            write_log("MAIN", f"Redis connected: {redis_addr}")
    else:
        logger.warning("Redis unavailable — starting in degraded mode")
        if is_startup_logger():
            write_log("MAIN", "Redis unavailable — starting in degraded mode", level="W")
    app.state.initial_redis_connected = connected



    # ★ (5.5) 상태 모니터 시작 — 10분 간격 Redis 메모리 + 프로세스 상태 로깅.
    # 모든 워커가 루프를 시작하되, 루프 안의 Redis NX 락으로 매 주기 1개 워커만
    # 기록한다. (startup flock 게이트는 컨테이너 환경에서 워커 간 배타에 실패해
    # STATUS가 워커 수만큼 중복됐다 → Redis 선출로 이관.)
    global _monitor_shutdown
    _monitor_task = None
    if connected:
        _monitor_shutdown = asyncio.Event()
        _monitor_task = asyncio.create_task(_status_monitor_loop())

    yield

    # ★ (6) 종료
    # 상태 모니터 종료
    if _monitor_task and _monitor_shutdown:
        _monitor_shutdown.set()
        try:
            await asyncio.wait_for(_monitor_task, timeout=3)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _monitor_task.cancel()
            try:
                await _monitor_task
            except (asyncio.CancelledError, Exception):
                pass

    # 시작 로그 락 해제 (첫 워커만 보유 → 여기서 해제, 나머지 워커는 noop)
    release_startup_lock()
    write_log("MAIN", "RedGW shutting down...")

    # Audit 로거 종료 — 큐 drain 후 종료 라인 (PCI-DSS 10.2.6). Level=0이면 No-op.
    try:
        from app.audit import shutdown_audit_logger, is_active as _audit_active
        if _audit_active():
            _count = shutdown_audit_logger()
            write_log("MAIN", f"Audit shutdown — flushed {_count} events since start")
        else:
            shutdown_audit_logger()
    except Exception:
        pass



    await manager.disconnect()
    logger.info("Redis disconnected")
    write_log("MAIN", "RedGW stopped")


def create_app() -> FastAPI:
    """앱 팩토리"""
    settings = get_settings()

    # 운영 로그는 plain 고정 — 실시간 사람 인지 우선. 구조화 분석은 audit 로그(/app/audit/) 별도
    log_level = getattr(logging, settings.logging.level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = FastAPI(
        title="RedGW — Redis Gateway",
        description="Redis 기반 REST 미들웨어 — 이기종 시스템 간 데이터 공유 게이트웨이",
        version=APP_VERSION,
        lifespan=lifespan,
        docs_url="/docs" if settings.server.docs else None,
        redoc_url="/redoc" if settings.server.docs else None,
    )
    # 응답 직렬화: FastAPI 0.131+는 라우터의 `-> dict` 반환 타입을 Pydantic(Rust) 코어로
    # 직접 JSON 바이트 직렬화한다(stdlib json 대비 2배+). 커스텀 응답클래스(ORJSONResponse)는
    # deprecated이며 불필요 — 내장 경로에 의존한다.

    # CORS 미들웨어 — 허용 도메인을 명시적으로 지정
    cors_origins = settings.server.cors_origins
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "PUT", "POST", "DELETE"],
            allow_headers=["X-API-Key", "Content-Type"],
        )

    # 순수 ASGI 미들웨어 등록 (BaseHTTPMiddleware 미사용 — 메모리 할당 최소화)
    # BaseHTTPMiddleware는 요청마다 asyncio Task + MemoryStream 쌍을 생성하여
    # 장기 실행 시 glibc malloc 아레나 단편화를 유발한다.
    # Starlette는 add_middleware 역순으로 실행:
    #   요청 → SecurityHeaders → Metrics → AccessLog(조건부) → Audit(level>=1 조건부) → 라우터
    #   (Audit은 가장 먼저 add되어 innermost — 라우터 직전. 아래 L366 주석 참조)
    from app.middleware import (
        SecurityHeadersMiddleware,
        MetricsMiddleware,
        AccessLogMiddleware,
    )

    # Audit 미들웨어 — Level>=1에서만 등록. 가장 먼저 add → innermost가 되어
    # request_id를 scope에 박은 후 AccessLog가 응답 시점에 읽을 수 있도록 한다.
    if settings.audit.level >= 1:
        from app.audit.middleware import AuditMiddleware
        app.add_middleware(AuditMiddleware)

    if settings.logging.access_log:
        app.add_middleware(AccessLogMiddleware)
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    # 전역 예외 핸들러
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled exception: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Internal server error",
                },
            },
        )

    # 라우터 등록
    app.include_router(kv.router, prefix="/api/v1", tags=["KV (String)"])
    app.include_router(map_router.router, prefix="/api/v1", tags=["Map (Hash)"])
    app.include_router(queue.router, prefix="/api/v1", tags=["Queue (List)"])
    app.include_router(group.router, prefix="/api/v1", tags=["Group (Set)"])
    app.include_router(rank.router, prefix="/api/v1", tags=["Rank (Sorted Set)"])
    app.include_router(event.router, prefix="/api/v1", tags=["Event (Stream)"])
    app.include_router(pubsub.router, prefix="/api/v1", tags=["Pub/Sub"])
    app.include_router(admin.router, prefix="/api/v1", tags=["Admin"])


    # 루트 엔드포인트 — 서비스 정보
    @app.get("/", tags=["Health"])
    async def root():
        return {
            "service": "RedGW",
            "version": APP_VERSION,
            "description": "Redis Gateway for inter-system data sharing",
            "organization": "Cubiware Co., Ltd.",
            "health": "/health",
            "api": "/api/v1",
        }

    # 헬스체크 — Redis 지연·메모리 포함, 상태 변경 시에만 로그
    _last_redis_status: dict[str, bool | None] = {"connected": None}

    @app.get("/health", tags=["Health"])
    async def health_check():
        manager = get_redis_manager()

        # Redis ping + 지연 측정
        t0 = time.monotonic()
        redis_ok = await manager.ping()
        latency_ms = round((time.monotonic() - t0) * 1000, 2)

        # Redis 메모리 정보 (연결 실패 시 스킵)
        memory_info: dict = {}
        if redis_ok:
            try:
                r = manager.get_client()
                info = await r.info("memory")
                used_mb = round(info.get("used_memory", 0) / 1024 / 1024, 1)
                maxmem = info.get("maxmemory", 0)
                memory_info = {
                    "used_mb": used_mb,
                    "limit_mb": round(maxmem / 1024 / 1024, 1) if maxmem else None,
                    "usage_pct": round(used_mb / (maxmem / 1024 / 1024) * 100, 1) if maxmem else None,
                }
            except Exception:
                logger.debug("Memory info unavailable", exc_info=True)

        # 상태 판정 (메모리 90% 초과 시 degraded)
        usage_pct = memory_info.get("usage_pct")
        status = "healthy"
        if not redis_ok:
            status = "degraded"
        elif usage_pct is not None and usage_pct >= 90:
            status = "degraded"

        # 상태 변경 시에만 로그 출력
        if _last_redis_status["connected"] is None:
            _last_redis_status["connected"] = getattr(app.state, "initial_redis_connected", True)
        if redis_ok != _last_redis_status["connected"]:
            if redis_ok:
                logger.info("Redis reconnected")
            else:
                logger.warning("Redis connection lost")
            _last_redis_status["connected"] = redis_ok



        return {
            "status": status,
            "redis": {
                "connected": redis_ok,
                "latency_ms": latency_ms if redis_ok else None,
                **memory_info,
            },

            "audit_level": settings.audit.level,
        }

    return app


app = create_app()
