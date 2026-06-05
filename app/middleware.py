"""순수 ASGI 미들웨어 — BaseHTTPMiddleware 미사용으로 메모리 할당 최소화

BaseHTTPMiddleware는 요청마다 asyncio Task + MemoryStream 쌍을 생성하여
장기 실행 시 glibc malloc 아레나 단편화를 유발한다.
순수 ASGI 미들웨어는 send 래퍼만 사용하므로 추가 Task/Stream 할당이 없다.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from app.utils.metrics import record_request_async

logger = logging.getLogger("redgw")

_ns_re = re.compile(r"/api/v1/(?:ns|ws)/([^/]+)/")
# 메트릭 ns 라벨 카디널리티 가드 — namespace 검증 규칙과 동일(영숫자/_/-, 1~64자).
# ns 추출은 인증 전 path 기반이라, 미인증·오타 요청의 임의 ns를 그대로 집계하면
# TTL 없는 Redis 메트릭 키(redgw:metrics:ns:*)가 무제한 증식해 메모리를 잠식한다.
_valid_ns_re = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# fire-and-forget 메트릭 task 강참조 유지 — 미보관 시 실행 중 task가 GC돼 INCR 유실
# 가능(asyncio 공식 문서). 완료 시 done callback으로 자동 제거. (app/auth/namespace_guard.py 동일 패턴)
_metric_tasks: set = set()

# 보안 헤더 (바이트 튜플로 사전 인코딩 — 요청당 재인코딩 방지)
_SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"cache-control", b"no-store"),
    (b"x-xss-protection", b"1; mode=block"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
]
# 헤더 이름 집합 — 응답마다 재생성하지 않도록 모듈 로드 시 1회 계산.
_SECURITY_HEADER_NAMES = frozenset(n for n, _ in _SECURITY_HEADERS)


class SecurityHeadersMiddleware:
    """응답에 보안 헤더 5개를 주입한다."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                # 라우트가 동일 보안 헤더를 이미 설정했으면 중복 방지 — 우리 값으로 통일.
                headers = [(n, v) for (n, v) in message.get("headers", []) if n not in _SECURITY_HEADER_NAMES]
                headers.extend(_SECURITY_HEADERS)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


class MetricsMiddleware:
    """요청 완료 시 Redis 기반 메트릭을 비동기 기록한다."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        t0 = time.monotonic()
        status_code = 500  # 기본값 (예외 시)

        # namespace 추출 (path 기반 — 인증 전에도 결정됨)
        # 정상 형식(영숫자/_/-, 1~64자)만 메트릭 라벨로 집계해 카디널리티 폭주를 막는다.
        ns = ""
        m = _ns_re.search(scope.get("path", ""))
        if m and _valid_ns_re.match(m.group(1)):
            ns = m.group(1)

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 500)
            if message["type"] == "http.response.body":
                more_body = message.get("more_body", False)
                await send(message)
                if not more_body:
                    # client_id는 인증(verify_api_key)이 scope에 박은 값을 재사용한다 —
                    # 미들웨어가 x-api-key를 다시 resolve_client 하던 중복 조회를 제거.
                    # 이 시점엔 라우터/의존성이 이미 실행돼 scope가 채워져 있다(인증 라우트 한정).
                    client_id = scope.get("redgw_client_id", "")
                    duration_ms = (time.monotonic() - t0) * 1000
                    # fire-and-forget: 메트릭 Redis 왕복을 요청 ASGI 태스크에서 분리한다.
                    # 요청은 즉시 완료(클라/keepalive 연결 빨리 반납), 기록은 백그라운드 task로.
                    # record_request_async는 내부 try/except로 예외를 삼켜 미회수 예외 경고가 없다.
                    # (정확성 모델 불변 — 여전히 Redis 단일 소스 집계, await만 제거)
                    task = asyncio.create_task(
                        record_request_async(status_code, duration_ms, ns, client_id)
                    )
                    _metric_tasks.add(task)
                    task.add_done_callback(_metric_tasks.discard)
                return
            await send(message)

        await self.app(scope, receive, send_wrapper)


class AccessLogMiddleware:
    """HTTP 요청/응답을 로깅한다."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        t0 = time.monotonic()
        method = scope.get("method", "?")
        path = scope.get("path", "?")
        status_code = 0

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 0)
            if message["type"] == "http.response.body":
                more_body = message.get("more_body", False)
                await send(message)
                if not more_body:
                    duration_ms = round((time.monotonic() - t0) * 1000, 2)
                    # G4: audit 미들웨어가 박아준 request_id를 운영 로그에도 노출.
                    # Level=0 또는 audit 미설치 시 "-" 출력.
                    req_id = scope.get("redgw_request_id", "-")
                    logger.info(
                        "%s %s %s %.2fms req=%s",
                        method, path, status_code, duration_ms, req_id,
                    )
                return
            await send(message)

        await self.app(scope, receive, send_wrapper)
