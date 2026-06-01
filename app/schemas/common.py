"""공통 응답 모델"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class KeyMeta(BaseModel):
    ns: str
    key: str
    type: str
    ttl: int = -1


class ApiResponse(BaseModel, Generic[T]):
    ok: bool = True
    data: T | None = None
    meta: KeyMeta | None = None


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    ok: bool = False
    error: ErrorDetail


class ClientInfo(BaseModel):
    """인증된 클라이언트 정보"""
    client_id: str
    description: str
    namespaces: dict[str, list[str]]
