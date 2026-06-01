"""Hash 타입 요청/응답 모델"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class MapSetRequest(BaseModel):
    fields: dict[str, str] = Field(..., min_length=1)
    ttl: int | None = Field(None, ge=0)

    @field_validator("fields")
    @classmethod
    def _check_values(cls, v: dict[str, str]) -> dict[str, str]:
        # 단건 MapFieldSetRequest.value(min_length=1)와 동일 정책 — 빈 필드값 거부.
        for f, val in v.items():
            if len(val) < 1:
                raise ValueError(f"fields[{f}]: value must not be empty")
        return v


class MapFieldSetRequest(BaseModel):
    value: str = Field(..., min_length=1)
    ttl: int | None = Field(None, ge=0)


class MapBatchGetRequest(BaseModel):
    keys: list[str] = Field(..., min_length=1, max_length=100)


class MapBatchSetRequest(BaseModel):
    items: dict[str, dict[str, str]] = Field(..., min_length=1, max_length=100)
    ttl: int | None = Field(None, ge=0)

    @field_validator("items")
    @classmethod
    def _check_fields(cls, v: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
        for key, fields in v.items():
            # 빈 fields dict는 HSET mapping={} → redis-py DataError(500) 유발. 사전 거부.
            if len(fields) < 1:
                raise ValueError(f"items[{key}]: fields must not be empty")
            # 개별 필드값도 단건 경로(min_length=1)와 동일하게 빈 값 거부(계약 일치).
            for f, val in fields.items():
                if len(val) < 1:
                    raise ValueError(f"items[{key}].fields[{f}]: value must not be empty")
        return v
