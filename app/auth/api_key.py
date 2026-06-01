"""API Key 검증 모듈"""

from __future__ import annotations

import logging

from fastapi import Header, Request

from app.config import get_settings
from app.schemas.common import ClientInfo
from app.utils.response import error

logger = logging.getLogger("redgw.auth")

# WHY: O(1) 조회 캐시 — 설정 로드 시 1회 구축, verify_api_key()에서 매 요청 참조
_key_map: dict[str, ClientInfo] | None = None


def _build_key_map() -> dict[str, ClientInfo]:
    """설정에서 api_key → ClientInfo 매핑을 구축한다."""
    settings = get_settings()
    mapping: dict[str, ClientInfo] = {}

    # admin
    if settings.admin.api_key:
        mapping[settings.admin.api_key] = ClientInfo(
            client_id="admin",
            description="Administrator",
            namespaces={"*": ["read", "write"]},
        )

    # clients
    for client_id, cfg in settings.clients.items():
        mapping[cfg.api_key] = ClientInfo(
            client_id=client_id,
            description=cfg.description,
            namespaces=cfg.namespaces,
        )

    return mapping


def resolve_client(api_key: str) -> ClientInfo | None:
    """API 키로 클라이언트를 찾는다. 없으면 None 반환. O(1) 조회."""
    global _key_map
    if _key_map is None:
        _key_map = _build_key_map()
    return _key_map.get(api_key)


def reset_key_map() -> None:
    """테스트용 역매핑 캐시 리셋"""
    global _key_map
    _key_map = None


async def verify_api_key(
    request: Request,
    x_api_key: str = Header(..., alias="X-API-Key"),
) -> ClientInfo:
    """X-API-Key 헤더에서 API 키 추출 → 클라이언트 매칭"""
    client = resolve_client(x_api_key)
    if client is None:
        # nginx가 무조건 덮어쓰는 X-Real-IP($remote_addr) 우선 — request.client.host는
        # 항상 nginx 컨테이너 IP라 실제 공격 출처 식별에 무용하다(audit extract_ip와 동일 정책).
        xri = request.headers.get("x-real-ip")
        client_ip = (
            (xri.strip() if xri else None)            # audit extract_ip와 동일하게 공백 제거
            or (request.client.host if request.client else None)
            or "unknown"
        )
        logger.warning(
            "AUTH_FAIL ip=%s method=%s path=%s",
            client_ip,
            request.method,
            request.url.path,
        )
        # Audit: contextvar로 pending event 설정 — 미들웨어 finally가 event/error_code
        # 덮어씀. Level=0/미초기화면 미들웨어 자체가 미등록이라 무시됨.
        try:
            from app.audit import set_pending_event
            set_pending_event(event="auth_fail", error_code="UNAUTHORIZED")
        except Exception:
            pass
        # 메트릭 카운터 (Redis INCR, 비동기) — 실패는 조용히 무시 (메인 동작 영향 없음)
        try:
            from app.utils.metrics import record_auth_failure_async
            await record_auth_failure_async("auth_fail")
        except Exception:
            pass
        raise error("UNAUTHORIZED", "Invalid API key", status=401)
    # 라우터/미들웨어가 client_id를 audit·access 로그에서 읽을 수 있도록 scope에 박음
    request.scope["redgw_client_id"] = client.client_id
    return client
