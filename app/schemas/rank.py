"""Sorted Set 타입 요청/응답 모델"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RankAddRequest(BaseModel):
    member: str = Field(..., min_length=1)
    score: float
    ttl: int | None = Field(None, ge=0)


class RankMemberScore(BaseModel):
    member: str = Field(..., min_length=1)
    score: float


class RankBatchAddRequest(BaseModel):
    members: list[RankMemberScore] = Field(..., min_length=1, max_length=100)
    ttl: int | None = Field(None, ge=0)


class RankBatchGetRequest(BaseModel):
    keys: list[str] = Field(..., min_length=1, max_length=100)
    start: int = 0
    stop: int = -1
    reverse: bool = False


class RankIncrRequest(BaseModel):
    member: str = Field(..., min_length=1)
    delta: float
