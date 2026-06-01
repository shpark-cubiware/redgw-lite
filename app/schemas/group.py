"""Set 타입 요청/응답 모델"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class GroupAddRequest(BaseModel):
    members: list[str] = Field(..., min_length=1)
    ttl: int | None = Field(None, ge=0)


class GroupBatchGetRequest(BaseModel):
    keys: list[str] = Field(..., min_length=1, max_length=100)


class GroupBatchAddRequest(BaseModel):
    items: dict[str, list[str]] = Field(..., min_length=1, max_length=100)
    ttl: int | None = Field(None, ge=0)

    @field_validator("items")
    @classmethod
    def _check_members(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for key, members in v.items():
            # 빈 멤버 리스트는 SADD 인자 0개 → Redis ResponseError(500) 유발. 사전 거부.
            if len(members) < 1:
                raise ValueError(f"items[{key}]: members must not be empty")
            for i, m in enumerate(members):
                if len(m) < 1:
                    raise ValueError(f"items[{key}][{i}]: member must not be empty")
        return v


class GroupOpsRequest(BaseModel):
    # 다른 배치 스키마와 동일하게 100건 상한 — read 권한만으로 대량 키 집합연산이
    # 단일 스레드 Redis를 장시간 점유(타 NS 영향)하는 것을 방어한다.
    keys: list[str] = Field(..., min_length=2, max_length=100)
