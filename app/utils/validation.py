"""값 크기 검증 공통 함수

Rust(redgw_core) 모듈이 사용 가능하면 bytes 복사 없이 UTF-8 길이를 확인하고,
빌드 실패/로드 실패 시 기존 Python 구현으로 자동 전환한다.
"""

from __future__ import annotations

import os

from app.config import get_settings
from app.utils.response import error

# ── Rust 모듈 사용 가능 여부 ──────────────────────────────────
# REDGW_DISABLE_RUST=true 로 설정하면 Rust 모듈이 있어도 Python fallback 사용
_USE_RUST = False

if os.environ.get("REDGW_DISABLE_RUST", "").lower() not in ("true", "1", "yes"):
    try:
        from redgw_core import check_utf8_byte_len as _rust_check_utf8_byte_len
        _USE_RUST = True
    except ImportError:
        pass


def _exceeds_size(value: str, max_size: int) -> bool:
    """값의 UTF-8 바이트 크기가 max_size를 초과하는지 확인."""
    if _USE_RUST:
        return not _rust_check_utf8_byte_len(value, max_size)
    return len(value.encode()) > max_size


def check_value_size(value: str, *, label: str = "Value") -> None:
    """단일 값 크기 검증. 초과 시 VALUE_TOO_LARGE 예외 발생."""
    max_size = get_settings().defaults.max_value_size
    if _exceeds_size(value, max_size):
        raise error("VALUE_TOO_LARGE", f"{label} exceeds max size ({max_size} bytes)", status=400)


def check_dict_values_size(data: dict[str, str]) -> None:
    """dict의 모든 값 크기 검증. 초과 시 VALUE_TOO_LARGE 예외 발생."""
    max_size = get_settings().defaults.max_value_size
    for name, value in data.items():
        if _exceeds_size(value, max_size):
            raise error("VALUE_TOO_LARGE", f"Field '{name}' exceeds max size ({max_size} bytes)", status=400)
