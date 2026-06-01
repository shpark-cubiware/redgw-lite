"""Audit ASGI 미들웨어.

Level=0이면 main.py에서 등록 자체 안 함 — 오버헤드 0.
Level>=1이면 모든 HTTP 요청에 대해:
1. X-Request-ID 검증·생성 → scope["redgw_request_id"]
2. exclude_paths 제외 (헬스체크 등)
3. 캡처 대상이면 body 수집 (쓰기 작업·Pub/Sub publish)
4. 응답 후 audit 라인 한 줄 JSON 직렬화·큐 put
5. X-Request-ID 응답 헤더 echo
"""

from __future__ import annotations

import hashlib
import json
import time

from app.audit.logger import (
    consume_pending_event,
    detect_admin_action,
    extract_ip,
    extract_key,
    extract_ns,
    format_iso_ts_now,
    get_audit_config,
    get_audit_logger,
    increment_event_count,
    validate_request_id,
)

# 캡처 시 수집하는 body 최대 크기 — 1 MB. 초과분은 잘림 (해시는 잘린 값 기준)
# 실제 라우터들은 max_value_size로 더 작게 강제하지만 안전망
_MAX_CAPTURE_BODY = 1024 * 1024


def _decide_capture(method: str, path: str, level: int) -> bool:
    """Level 별 캡처 결정.

    Level 0: 미캡처 (그러나 미들웨어 자체가 미등록이라 호출되지 않음)
    Level 1: 쓰기/삭제 (PUT/POST/DELETE) + admin. Pub/Sub publish는 Level 2.
    Level 2: 모든 요청 + Pub/Sub publish.
    """
    if level == 0:
        return False

    is_admin = path.startswith("/api/v1/admin/")
    is_pubsub_publish = "/publish/" in path
    is_write = method in ("PUT", "POST", "DELETE")

    if level == 1:
        if is_admin:
            return True
        if is_pubsub_publish:
            return False  # Level 2에서만
        return is_write

    # Level 2 — 모두 캡처
    return True


def _detect_value_type(body: bytes, content_type: str) -> str:
    """value_type 판정 규칙 (스키마 v1).

    - json: Content-Type=application/json
    - binary: Content-Type=application/octet-stream 또는 UTF-8 디코딩 실패
    - string: 그 외 (디폴트)
    """
    ct = content_type.split(";")[0].strip().lower()
    if ct == "application/json":
        return "json"
    if ct == "application/octet-stream":
        return "binary"
    try:
        body.decode("utf-8")
        return "string"
    except UnicodeDecodeError:
        return "binary"


class AuditMiddleware:
    """순수 ASGI 미들웨어. Level=0이면 main.py가 등록 자체 안 함."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        config = get_audit_config()
        logger = get_audit_logger()
        # 안전망: 등록은 됐는데 init 안 됨/Level 변경 → passthrough
        if config is None or logger is None:
            await self.app(scope, receive, send)
            return

        # 1) request_id 처리 — 헤더 채택 또는 새 UUID4
        raw_id = ""
        for name, val in scope.get("headers") or ():
            if name == b"x-request-id":
                raw_id = val.decode("ascii", "ignore")
                break
        request_id = validate_request_id(raw_id)
        scope["redgw_request_id"] = request_id

        # 2) 경로 필터
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if path in config.exclude_paths:
            # 응답 헤더 echo만 수행. exclude_paths(/health·/metrics 등)는 감사 대상이
            # 아니므로 인증/권한 실패가 있어도 감사 라인을 남기지 않는다(고빈도 노이즈 차단).
            await self._pass_with_header(scope, receive, send, request_id, record_pending=False)
            return

        # 3) 캡처 대상이 아니면 헤더만 echo (단 인증/권한 실패는 pending event로 기록)
        if not _decide_capture(method, path, config.level):
            await self._pass_with_header(scope, receive, send, request_id)
            return

        # 4) body 수집 (Level 2 GET은 본문 없음, 쓰기 작업은 본문 있음)
        body_chunks: list[bytes] = []
        body_total = 0
        truncated = False

        async def receive_wrapper():
            nonlocal body_total, truncated
            msg = await receive()
            if msg["type"] == "http.request":
                chunk = msg.get("body", b"") or b""
                if chunk and not truncated:
                    remaining = _MAX_CAPTURE_BODY - body_total
                    if len(chunk) <= remaining:
                        body_chunks.append(chunk)
                        body_total += len(chunk)
                    else:
                        body_chunks.append(chunk[:remaining])
                        body_total += remaining
                        truncated = True
            return msg

        # 5) 응답 상태 캡처 + X-Request-ID echo
        status_code = 0

        async def send_wrapper(msg):
            nonlocal status_code
            if msg["type"] == "http.response.start":
                status_code = msg.get("status", 0)
                hdrs = list(msg.get("headers") or [])
                # 중복 회피 후 추가
                hdrs = [(n, v) for (n, v) in hdrs if n != b"x-request-id"]
                hdrs.append((b"x-request-id", request_id.encode("ascii", "ignore")))
                msg = {**msg, "headers": hdrs}
            await send(msg)

        # 6) Content-Type·client_id 헤더 캐싱
        content_type = ""
        for name, val in scope.get("headers") or ():
            if name == b"content-type":
                try:
                    content_type = val.decode("ascii", "ignore")
                except Exception:
                    pass
                break

        t0 = time.monotonic()
        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        finally:
            latency_ms = round((time.monotonic() - t0) * 1000, 2)
            try:
                body = b"".join(body_chunks) if body_chunks else b""

                value_hash = None
                value_size = None
                value_type = None
                if method in ("PUT", "POST", "DELETE") and body:
                    value_size = len(body)
                    value_hash = hashlib.sha256(body).hexdigest()[:16]
                    value_type = _detect_value_type(body, content_type)

                # 인증·권한 훅이 박은 메타데이터가 있으면 event/error_code 덮어쓰기
                pending = consume_pending_event()
                if pending is not None:
                    p_event, p_error_code = pending
                else:
                    p_event, p_error_code = "api_call", None

                record = {
                    "ts": format_iso_ts_now(),
                    "request_id": request_id,
                    "event": p_event,
                    "client_id": scope.get("redgw_client_id"),
                    "ip": extract_ip(scope),
                    "method": method,
                    "path": path,
                    "ns": extract_ns(path),
                    "key": extract_key(path),
                    "status": status_code,
                    "error_code": p_error_code,
                    "latency_ms": latency_ms,
                    "value_hash": value_hash,
                    "value_size": value_size,
                    "value_type": value_type,
                    "admin_action": detect_admin_action(path),
                    "schema_version": 1,
                }

                # Level 2 옵트인: 페이로드 prefix
                if (
                    config.level == 2
                    and config.payload_prefix_bytes > 0
                    and body
                ):
                    prefix_bytes = body[: config.payload_prefix_bytes]
                    record["value_prefix"] = prefix_bytes.decode(
                        "utf-8", errors="replace"
                    )

                line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                logger.info(line)
                increment_event_count()

                # Q3 Correlation ID — 쓰기 작업의 origin 메타를 Redis에 박음.
                # 운영자가 admin API로 origin_request_id를 조회해
                # 인과를 추적하는 채널.
                ns_val = record.get("ns")
                # correlation origin 키에는 데이터 키 라우트의 path 파라미터 key를 쓴다.
                # record["key"](extract_key 정규식)는 batch/ops/ack 등 다중 세그먼트 액션
                # 경로의 꼬리까지 key로 잡아(예: "ops/inter", "mystream/group/g1/ack") 존재하지
                # 않는 origin 키를 만든다. path_params["key"]는 /{type}/{key}[...] 데이터
                # 라우트에만 존재하므로 batch·ops처럼 key 파라미터가 없는 라우트는 자동 제외된다.
                # (단일 데이터 키 경로에서는 extract_key와 동일 값이라 정상 동작은 불변.)
                key_val = (scope.get("path_params") or {}).get("key")
                if (
                    config.correlation
                    and method in ("PUT", "POST", "DELETE")
                    and ns_val
                    and key_val
                    and status_code < 400
                ):
                    try:
                        from app.redis_client import get_redis_manager
                        from app.utils.key_builder import build_key, resolve_type_prefix
                        # path /api/v1/ns/{ns}/{type_token}/{key} 에서 URL 토큰 추출.
                        parts = path.split("/")
                        if len(parts) >= 6:
                            # URL 토큰(queue/group/event)을 저장 접두어(q/grp/evt)로 변환해야
                            # 실제 데이터 키 및 admin 읽기 경로(resolve_type_prefix)와 일치한다.
                            # 미변환 시 queue/group/event의 origin이 잘못된 키에 저장돼
                            # /admin/audit/origin 조회가 404가 된다.
                            type_prefix = resolve_type_prefix(parts[5])
                            redis_key = build_key(ns_val, type_prefix, key_val)
                            origin_key = f"__redgw:audit:origin:{redis_key}"
                            origin_value = json.dumps(
                                {
                                    "request_id": request_id,
                                    "client_id": scope.get("redgw_client_id"),
                                    "ts": record["ts"],
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            r = get_redis_manager().get_client()
                            await r.set(
                                origin_key,
                                origin_value,
                                ex=config.correlation_ttl_seconds,
                            )
                    except Exception:
                        pass
            except Exception:
                pass

    async def _pass_with_header(
        self, scope, receive, send, request_id: str, record_pending: bool = True,
    ) -> None:
        """캡처 안 함. X-Request-ID 응답 헤더만 echo.

        record_pending=True(기본)면 인증/권한 실패(pending event)를 메서드와 무관하게
        감사 라인으로 남긴다 — GET read 엔드포인트의 auth_fail/namespace_denied가 Level 1
        계약대로 기록되게 한다. 쓰기 요청은 _decide_capture가 True라 이 경로로 오지 않으므로
        중복 기록은 없다. exclude_paths는 record_pending=False로 호출돼 감사하지 않는다.
        """
        status_code = 0

        async def send_wrapper(msg):
            nonlocal status_code
            if msg["type"] == "http.response.start":
                status_code = msg.get("status", 0)
                hdrs = list(msg.get("headers") or [])
                hdrs = [(n, v) for (n, v) in hdrs if n != b"x-request-id"]
                hdrs.append((b"x-request-id", request_id.encode("ascii", "ignore")))
                msg = {**msg, "headers": hdrs}
            await send(msg)

        t0 = time.monotonic()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # pending은 항상 소비(컨텍스트 정리). 기록은 record_pending일 때만.
            pending = consume_pending_event()
            if record_pending and pending is not None:
                p_event, p_error_code = pending
                p_path = scope.get("path", "")
                record = {
                    "ts": format_iso_ts_now(),
                    "request_id": request_id,
                    "event": p_event,
                    "client_id": scope.get("redgw_client_id"),
                    "ip": extract_ip(scope),
                    "method": scope.get("method", "GET"),
                    "path": p_path,
                    "ns": extract_ns(p_path),
                    "key": extract_key(p_path),
                    "status": status_code,
                    "error_code": p_error_code,
                    "latency_ms": round((time.monotonic() - t0) * 1000, 2),
                    "value_hash": None,
                    "value_size": None,
                    "value_type": None,
                    "admin_action": detect_admin_action(p_path),
                    "schema_version": 1,
                }
                try:
                    # logger는 __call__ 지역변수라 이 메서드 스코프엔 없다 — 직접 가져온다.
                    get_audit_logger().info(
                        json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                    )
                    increment_event_count()
                except Exception:
                    pass
