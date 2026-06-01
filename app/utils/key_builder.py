"""네임스페이스:타입접두어:키 조립 유틸리티

Rust(redgw_core) 모듈이 사용 가능하면 Rust 구현을 사용하고,
빌드 실패/로드 실패 시 기존 Python 구현으로 자동 전환한다.
"""

import os
import re

from app.utils.response import error

# ── Rust 모듈 사용 가능 여부 ──────────────────────────────────
# REDGW_DISABLE_RUST=true 로 설정하면 Rust 모듈이 있어도 Python fallback 사용
_USE_RUST = False

if os.environ.get("REDGW_DISABLE_RUST", "").lower() not in ("true", "1", "yes"):
    try:
        from redgw_core import (
            validate_ns as _rust_validate_ns,
            validate_key as _rust_validate_key,
            build_key as _rust_build_key,
            build_keys_batch as _rust_build_keys_batch,
            parse_key as _rust_parse_key,
        )
        _USE_RUST = True
    except ImportError:
        pass

# ── 타입 접두어 상수 (변경 없음) ──────────────────────────────
KV_PREFIX = "kv"        # String  → /ns/{ns}/kv/{key}
MAP_PREFIX = "map"      # Hash    → /ns/{ns}/map/{key}
QUEUE_PREFIX = "q"      # List    → /ns/{ns}/queue/{key}
GROUP_PREFIX = "grp"    # Set     → /ns/{ns}/group/{key}
RANK_PREFIX = "rank"    # Sorted Set → /ns/{ns}/rank/{key}
EVENT_PREFIX = "evt"    # Stream  → /ns/{ns}/event/{key}

# 사용자 저장 키의 타입 접두어 집합. 내부 제어/메트릭 키(__redgw:*, redgw:metrics:*)는
# 접두어가 여기 속하지 않으므로, admin SCAN 집계가 이 집합으로 사용자 키만 선별한다.
STORAGE_PREFIXES = frozenset(
    {KV_PREFIX, MAP_PREFIX, QUEUE_PREFIX, GROUP_PREFIX, RANK_PREFIX, EVENT_PREFIX}
)

# ── Python fallback (기존 코드 보존) ──────────────────────────
_NS_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
_KEY_RE = re.compile(r'^[A-Za-z0-9_.:\-/]{1,256}$')


def _py_validate_ns(ns: str) -> str:
    """네임스페이스 형식 검증 — 영숫자, _, - 만 허용 (1~64자)"""
    if not _NS_RE.fullmatch(ns):
        raise error(
            "INVALID_NAMESPACE",
            f"Namespace '{ns}' contains invalid characters. "
            "Allowed: A-Z, a-z, 0-9, _, - (max 64 chars)",
            status=400,
        )
    return ns


def _py_validate_key(key: str) -> str:
    """키 형식 검증 — 영숫자, _, ., :, -, / 만 허용 (1~256자)"""
    if not _KEY_RE.fullmatch(key):
        raise error(
            "INVALID_KEY",
            f"Key '{key}' contains invalid characters. "
            "Allowed: A-Z, a-z, 0-9, _, ., :, -, / (max 256 chars)",
            status=400,
        )
    return key


def _py_build_key(ns: str, type_prefix: str, key: str) -> str:
    _py_validate_ns(ns)
    _py_validate_key(key)
    return f"{ns}:{type_prefix}:{key}"


def _py_parse_key(redis_key: str) -> tuple[str, str, str]:
    parts = redis_key.split(":", 2)
    if len(parts) != 3:
        raise ValueError(f"Invalid redis key format: {redis_key}")
    return parts[0], parts[1], parts[2]


def _py_build_keys_batch(ns: str, prefix: str, keys: list[str]) -> list[str]:
    _py_validate_ns(ns)
    return [f"{ns}:{prefix}:{_py_validate_key(k)}" for k in keys]


# ── 공개 API (Rust 사용 가능 시 Rust, 아니면 Python) ──────────
if _USE_RUST:
    def validate_ns(ns: str) -> str:
        try:
            _rust_validate_ns(ns)
            return ns
        except ValueError as e:
            raise error("INVALID_NAMESPACE", str(e), status=400)

    def validate_key(key: str) -> str:
        try:
            _rust_validate_key(key)
            return key
        except ValueError as e:
            raise error("INVALID_KEY", str(e), status=400)

    def build_key(ns: str, type_prefix: str, key: str) -> str:
        try:
            return _rust_build_key(ns, type_prefix, key)
        except ValueError as e:
            msg = str(e)
            if "Namespace" in msg:
                raise error("INVALID_NAMESPACE", msg, status=400)
            raise error("INVALID_KEY", msg, status=400)

    def parse_key(redis_key: str) -> tuple[str, str, str]:
        # _rust_parse_key는 형식 오류 시 ValueError를 던지며, 그대로 전파한다.
        return _rust_parse_key(redis_key)

    def build_keys_batch(ns: str, prefix: str, keys: list[str]) -> list[str]:
        try:
            return _rust_build_keys_batch(ns, prefix, keys)
        except ValueError as e:
            msg = str(e)
            if "Namespace" in msg:
                raise error("INVALID_NAMESPACE", msg, status=400)
            raise error("INVALID_KEY", msg, status=400)
else:
    validate_ns = _py_validate_ns
    validate_key = _py_validate_key
    build_key = _py_build_key
    parse_key = _py_parse_key
    build_keys_batch = _py_build_keys_batch


# ── URL 타입 → Redis 접두어 매핑 (변경 없음) ──────────────────
TYPE_PREFIX_MAP: dict[str, str] = {
    "string": KV_PREFIX, "kv": KV_PREFIX,
    "hash": MAP_PREFIX, "map": MAP_PREFIX,
    "list": QUEUE_PREFIX, "queue": QUEUE_PREFIX,
    "set": GROUP_PREFIX, "group": GROUP_PREFIX,
    "zset": RANK_PREFIX, "rank": RANK_PREFIX,
    "stream": EVENT_PREFIX, "event": EVENT_PREFIX,
    # 저장 접두어 자신도 타입 토큰으로 허용 — `keys` 출력에 보이는 prefix(q·grp·evt)를
    # 그대로 del/info 등에 입력할 수 있게(보이는 것=입력 가능). kv·map·rank는 위에서 이미 충족.
    QUEUE_PREFIX: QUEUE_PREFIX, GROUP_PREFIX: GROUP_PREFIX, EVENT_PREFIX: EVENT_PREFIX,
}


def resolve_type_prefix(type_name: str) -> str:
    """URL 타입 이름을 Redis 접두어로 변환. 미등록 타입이면 400 에러."""
    prefix = TYPE_PREFIX_MAP.get(type_name)
    if prefix is None:
        valid = ", ".join(sorted(TYPE_PREFIX_MAP.keys()))
        raise error("INVALID_TYPE", f"Unknown type '{type_name}'. Valid types: {valid}", status=400)
    return prefix


def build_scan_pattern(ns: str | None, type_name: str | None, pattern: str = "*") -> str:
    """SCAN 검색 패턴 구성 — ns, type, pattern 조합.

    항상 3세그먼트 `{ns}:{type}:{key}` 형식만 매칭하도록 통일한다(평문 `*` 미반환).
    type_name이 주어졌으나 미등록이면 resolve_type_prefix가 INVALID_TYPE(400)을 던져
    조용한 전체타입 확장(예: 오타 type → ns 전체 삭제)을 막는다.

    주의: glob `*:*:{pattern}`은 콜론 3개 이상인 내부키(`__redgw:status_monitor:tick` 등)도
    매칭하므로, 내부키 배제는 호출부의 결과 필터(STORAGE_PREFIXES)로 보강해야 한다."""
    prefix = resolve_type_prefix(type_name) if type_name else "*"
    ns_part = ns if ns else "*"
    return f"{ns_part}:{prefix}:{pattern}"
