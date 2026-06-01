"""List 타입 요청/응답 모델"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class QueuePushRequest(BaseModel):
    value: str = Field(..., min_length=1)
    direction: Literal["left", "right"] = "right"
    ttl: int | None = Field(None, ge=0)


class QueueBatchPushRequest(BaseModel):
    values: list[str] = Field(..., min_length=1, max_length=100)  # each value validated below
    direction: Literal["left", "right"] = "right"
    ttl: int | None = Field(None, ge=0)

    @field_validator("values")
    @classmethod
    def _check_values(cls, v: list[str]) -> list[str]:
        for i, val in enumerate(v):
            if len(val) < 1:
                raise ValueError(f"values[{i}]: value must not be empty")
        return v


class QueueTrimRequest(BaseModel):
    keep: int = Field(..., ge=1)
