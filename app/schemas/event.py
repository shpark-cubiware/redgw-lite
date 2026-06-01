"""Stream 타입 요청/응답 모델"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class EventPublishRequest(BaseModel):
    data: dict[str, str] = Field(..., min_length=1)


class EventGroupCreateRequest(BaseModel):
    group: str = Field(..., min_length=1)


class EventBatchPublishRequest(BaseModel):
    events: list[dict[str, str]] = Field(..., min_length=1, max_length=100)

    @field_validator("events")
    @classmethod
    def _check_events(cls, v: list[dict[str, str]]) -> list[dict[str, str]]:
        for i, evt in enumerate(v):
            # 빈 이벤트 dict는 XADD {} → Redis 500 유발. 사전 거부.
            if len(evt) < 1:
                raise ValueError(f"events[{i}]: event must not be empty")
        return v


class EventAckRequest(BaseModel):
    ids: list[str] = Field(..., min_length=1)
