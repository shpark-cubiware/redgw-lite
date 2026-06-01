"""Pub/Sub 요청/응답 모델"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PublishRequest(BaseModel):
    message: str = Field(..., min_length=1)
