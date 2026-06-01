"""Admin 요청/응답 모델"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TtlSetRequest(BaseModel):
    ttl: int = Field(..., ge=0)
