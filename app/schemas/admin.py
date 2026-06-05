"""Admin 요청/응답 모델"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TtlSetRequest(BaseModel):
    # 모든 touch 엔드포인트 + admin set_ttl 공용. le: Redis EXPIRE 한계 초과 ttl의 500 누수 차단.
    ttl: int = Field(..., ge=0, le=9_999_999_999)
