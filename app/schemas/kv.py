"""String 타입 요청/응답 모델"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class KvSetRequest(BaseModel):
    value: str = Field(..., min_length=1)
    # le: Redis EXPIRE/SETEX 한계 초과 ttl이 ResponseError→500으로 새는 것을 입력단에서 차단
    # (KvIncrRequest.delta 바운드와 동일 취지). ~316년이면 운영상 충분.
    ttl: int | None = Field(None, ge=0, le=9_999_999_999)


class KvIncrRequest(BaseModel):
    # Redis INCRBY는 64비트 부호있는 정수만 허용 → delta 값이 int64 범위를 벗어나면 422로 차단.
    # (미차단 시 범위초과 delta가 incr_kv에서 ResponseError→500으로 노출됨)
    # 주의: 이 바운드는 delta 값만 검증한다. 합산 결과 오버플로(기존값+delta) 또는 비정수
    # 기존값으로 인한 런타임 ResponseError는 별도 경로 — incr_kv에서 잡아 400(INVALID_VALUE)으로 변환.
    delta: int = Field(1, ge=-9223372036854775808, le=9223372036854775807)


class KvBatchGetRequest(BaseModel):
    keys: list[str] = Field(..., min_length=1, max_length=100)


class KvBatchSetRequest(BaseModel):
    items: dict[str, str] = Field(..., min_length=1, max_length=100)
    # le: Redis EXPIRE/SETEX 한계 초과 ttl이 ResponseError→500으로 새는 것을 입력단에서 차단
    # (KvIncrRequest.delta 바운드와 동일 취지). ~316년이면 운영상 충분.
    ttl: int | None = Field(None, ge=0, le=9_999_999_999)

    @field_validator("items")
    @classmethod
    def _check_values(cls, v: dict[str, str]) -> dict[str, str]:
        # 단건 KvSetRequest.value(min_length=1)와 동일 정책 — 빈 값 거부(계약 일치).
        for k, val in v.items():
            if len(val) < 1:
                raise ValueError(f"items[{k}]: value must not be empty")
        return v
