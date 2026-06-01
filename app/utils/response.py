"""공통 응답 헬퍼

응답 본문은 dict 리터럴로 직접 구성한다. (Pydantic 모델 생성 + model_dump 경유를
제거해 직렬화 1패스로 축소 — 라우터의 `-> dict` 반환 타입을 FastAPI 내장
Pydantic 코어(Rust)가 직접 JSON 바이트로 직렬화한다.)

셰이프 계약은 app.schemas.common 의 ApiResponse/KeyMeta/ErrorResponse 와 동일하게
유지한다(스키마는 OpenAPI 문서·계약 참조용으로 존속). 셰이프 회귀는
tests/test_serialization.py 의 TestResponseShapeContract 가 고정한다.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def ok(data: Any, ns: str, key: str, type: str, ttl: int = -1) -> dict:
    """성공 응답 생성 (메타 포함)."""
    return {
        "ok": True,
        "data": data,
        "meta": {"ns": ns, "key": key, "type": type, "ttl": ttl},
    }


def ok_simple(data: Any) -> dict:
    """메타 없는 단순 성공 응답 (meta=None 유지 — 기존 셰이프와 동일)."""
    return {"ok": True, "data": data, "meta": None}


def error(code: str, message: str, status: int = 400) -> HTTPException:
    """에러 응답을 HTTPException으로 반환."""
    return HTTPException(
        status_code=status,
        detail={"ok": False, "error": {"code": code, "message": message}},
    )


def not_found(key: str, ns: str) -> HTTPException:
    """KEY_NOT_FOUND 에러 헬퍼."""
    return error("KEY_NOT_FOUND", f"Key '{key}' not found in namespace '{ns}'", status=404)
