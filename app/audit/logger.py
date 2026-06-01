"""Audit 로그 핵심 — JSON 한 줄 + QueueHandler 백그라운드 thread.

기존 운영 로그(`app/utils/file_logger.py`)의 `DailySizeRotatingHandler`를
재사용하되 prefix·디렉토리·보존 정책을 별도로 가져간다. QueueHandler를
앞단에 두어 요청 thread는 큐 put만 하고 실제 파일 I/O는 listener thread가
처리한다 (P5).

Level=0이면 init이 호출되어도 디렉토리·핸들러를 만들지 않고 No-op.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path
from queue import Full, Queue
from typing import Any

from app.config import AuditConfig
from app.utils.file_logger import DailySizeRotatingHandler

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 모듈 상태 (싱글턴)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_audit_logger: logging.Logger | None = None
_audit_queue: Queue | None = None
_audit_listener: QueueListener | None = None
_audit_config: AuditConfig | None = None
_event_count: int = 0

# X-Request-ID 검증 정규식 (G4 합의안)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")



# NS·key 추출 — write/read 라우터 경로
# /api/v1/ns/{ns}/(kv|map|queue|group|rank|event)/{key}(/...)?
_NS_KEY_RE = re.compile(
    r"^/api/v1/ns/([^/]+)/(?:kv|map|queue|group|rank|event)/([^?]+?)(?:\?.*)?$"
)
_NS_ONLY_RE = re.compile(r"^/api/v1/ns/([^/]+)/")

# 큐 적체 한도 — 초과 시 put_nowait 실패 → 드롭 카운터
_QUEUE_MAXSIZE = 10000

# 워커별 드롭 카운터 — 모듈 글로벌 (QueueHandler는 listener thread에서도 호출됨)
# Redis 집계는 미들웨어 finally에서 fire-and-forget으로 flush
_dropped_queue_full: int = 0
_dropped_disk_full: int = 0

# 인증·권한 훅이 미들웨어 finally 블록에 전달하는 메타데이터.
# contextvar는 같은 asyncio task 내에서 부모/자식 코드 사이 안전한 전파 채널.
_pending_event: contextvars.ContextVar = contextvars.ContextVar(
    "audit_pending_event", default=None
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 초기화·종료
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def init_audit_logger(config: AuditConfig) -> None:
    """Audit 로거 초기화. Level=0이면 No-op."""
    global _audit_logger, _audit_queue, _audit_listener, _audit_config, _event_count

    _audit_config = config
    _event_count = 0

    if config.level == 0:
        return

    audit_dir = Path(config.directory)
    audit_dir.mkdir(parents=True, exist_ok=True)

    prefix = f"audit.w{os.getpid()}" if config.per_worker_files else "audit"
    file_handler = DailySizeRotatingHandler(
        log_dir=str(audit_dir),
        prefix=prefix,
        max_bytes=config.max_file_size_mb * 1024 * 1024,
        retention_days=config.retention_days,
        max_total_size_mb=config.max_total_size_mb,
    )
    # passthrough — record_event/middleware가 이미 JSON 한 줄을 message로 넘김
    file_handler.setFormatter(logging.Formatter("%(message)s"))

    # P5: QueueHandler + QueueListener — 요청 thread는 큐에 put만 하고 즉시 반환,
    # listener thread가 백그라운드에서 파일 I/O를 처리한다.
    _audit_queue = Queue(maxsize=_QUEUE_MAXSIZE)
    _audit_listener = QueueListener(
        _audit_queue, file_handler, respect_handler_level=False
    )
    _audit_listener.start()

    logger = logging.getLogger("redgw.audit")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    # 표준 QueueHandler 대신 드롭 카운터 추가한 서브클래스 사용
    logger.addHandler(_DropCountingQueueHandler(_audit_queue))

    _audit_logger = logger


class _DropCountingQueueHandler(QueueHandler):
    """큐 가득 시 _dropped_queue_full 증가. 표준 QueueHandler.enqueue 오버라이드."""

    def enqueue(self, record):
        global _dropped_queue_full
        try:
            self.queue.put_nowait(record)
        except Full:
            # 큐 가득 참만 드롭으로 카운트. 그 외 예외는 QueueHandler.emit의
            # 상위 except로 전파되어 handleError가 처리 (드롭 오분류 방지).
            _dropped_queue_full += 1


def get_dropped_counts() -> dict[str, int]:
    """드롭 카운터 스냅샷 (모니터링용). disk_full은 file_logger 핸들러 통해 누적."""
    return {"queue_full": _dropped_queue_full, "disk_full": _dropped_disk_full}


def reset_dropped_counts() -> None:
    """테스트용."""
    global _dropped_queue_full, _dropped_disk_full
    _dropped_queue_full = 0
    _dropped_disk_full = 0


def increment_dropped_disk_full(n: int = 1) -> None:
    """audit 핸들러의 _cleanup_by_total_size에서 호출 — sync 컨텍스트 안전."""
    global _dropped_disk_full
    _dropped_disk_full += n


def shutdown_audit_logger() -> int:
    """큐 drain 후 listener stop. lifespan shutdown에서 호출.

    Returns:
        총 캡처 이벤트 수 (종료 라인 출력용).
    """
    global _audit_listener, _audit_logger, _audit_queue
    if _audit_listener:
        try:
            _audit_listener.stop()
        except Exception:
            pass
        _audit_listener = None
    count = _event_count
    _audit_logger = None
    _audit_queue = None
    return count


def reset_audit_logger() -> None:
    """테스트용 싱글턴 리셋. 큐·listener·logger·카운터 전부 초기화."""
    global _audit_logger, _audit_queue, _audit_listener, _audit_config, _event_count
    if _audit_listener:
        try:
            _audit_listener.stop()
        except Exception:
            pass
    if _audit_logger:
        for h in list(_audit_logger.handlers):
            try:
                h.close()
            except Exception:
                pass
            _audit_logger.removeHandler(h)
    _audit_logger = None
    _audit_queue = None
    _audit_listener = None
    _audit_config = None
    _event_count = 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 조회 헬퍼
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_audit_logger() -> logging.Logger | None:
    return _audit_logger


def get_audit_config() -> AuditConfig | None:
    return _audit_config


def get_event_count() -> int:
    return _event_count


def increment_event_count() -> None:
    """미들웨어·record_event가 새 라인 기록 후 호출."""
    global _event_count
    _event_count += 1


def get_queue_size() -> int:
    """현재 큐 적체 (모니터링용). 미초기화 시 0."""
    if _audit_queue:
        return _audit_queue.qsize()
    return 0


def is_active() -> bool:
    """Level>=1 + 초기화 완료 여부."""
    return _audit_logger is not None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 공용 유틸 — 미들웨어/훅 양쪽에서 사용
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def format_iso_ts_now() -> str:
    """ISO8601 UTC with ms — '2026-05-29T03:14:25.123Z'."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def validate_request_id(value: str | None) -> str:
    """X-Request-ID 헤더 수용 정책 (G4).

    영숫자·`-`·`_`, 1~128자만 허용. 위반 시 새 UUID4.
    """
    if value and _REQUEST_ID_RE.match(value):
        return value
    return str(uuid.uuid4())


def detect_admin_action(path: str) -> str | None:
    """admin 잡 조작 경로에서 action 식별자를 추출한다(해당 없으면 None)."""

    return None


def extract_ns(path: str) -> str | None:
    m = _NS_ONLY_RE.match(path)
    return m.group(1) if m else None


def extract_key(path: str) -> str | None:
    m = _NS_KEY_RE.match(path)
    return m.group(2) if m else None


def set_pending_event(event: str, error_code: str | None = None) -> None:
    """인증·권한 훅에서 호출. 미들웨어 finally가 record의 event/error_code를 덮음.

    contextvar는 같은 asyncio task 안에서만 보이므로 요청별 격리 자동.
    """
    _pending_event.set((event, error_code))


def consume_pending_event() -> tuple[str, str | None] | None:
    """미들웨어가 finally에서 호출. 다음 요청 오염 회피용 reset 포함."""
    val = _pending_event.get()
    if val is not None:
        _pending_event.set(None)
    return val


def extract_ip(scope: dict) -> str | None:
    """클라이언트 IP. nginx가 모든 location에서 무조건 덮어쓰는 X-Real-IP 우선.

    X-Forwarded-For는 nginx가 `$proxy_add_x_forwarded_for`로 클라이언트가 보낸 값
    '뒤에' 실제 peer를 덧붙이므로 첫 번째 값은 클라이언트가 위조할 수 있다 → 감사
    로그 IP 귀속이 스푸핑 가능. X-Real-IP는 `$remote_addr`로 강제되어 위조 불가이므로
    이를 신뢰하고, 없으면 scope client(직접 peer)로 폴백한다.
    """
    headers = scope.get("headers") or []
    for name, val in headers:
        if name == b"x-real-ip":
            try:
                ip = val.decode("ascii", "ignore").strip()
                if ip:
                    return ip
            except Exception:
                pass
            break
    client = scope.get("client")
    if client and len(client) >= 1:
        return client[0]
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# record_event — 보조 캡처 API (테스트/외부 통합용)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def record_event(
    *,
    event: str,
    scope: dict,
    client_id: str | None = None,
    error_code: str | None = None,
    status: int | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """단건 감사 이벤트를 직접 기록하는 보조 API.

    NOTE: 현재 운영 경로는 이 함수를 호출하지 않는다. 인증 실패·NS 권한 거부는
    인증/권한 훅이 set_pending_event로 contextvar만 남기고, 미들웨어 finally(또는
    _pass_with_header)가 직접 logger.info로 기록한다. 이 함수는 테스트/외부 통합에서
    미들웨어를 거치지 않고 이벤트를 남길 때 쓰는 보조 경로다.

    Level 0이거나 초기화 미완이면 즉시 반환.
    """
    if _audit_logger is None or _audit_config is None or _audit_config.level == 0:
        return

    path = scope.get("path", "?")
    if path in _audit_config.exclude_paths:
        return

    request_id = scope.get("redgw_request_id") or str(uuid.uuid4())

    record: dict[str, Any] = {
        "ts": format_iso_ts_now(),
        "request_id": request_id,
        "event": event,
        "client_id": client_id,
        "ip": extract_ip(scope),
        "method": scope.get("method", "?"),
        "path": path,
        "ns": extract_ns(path),
        "key": extract_key(path),
        "status": status,
        "error_code": error_code,
        "latency_ms": None,
        "value_hash": None,
        "value_size": None,
        "value_type": None,
        "admin_action": detect_admin_action(path),
        "schema_version": 1,
    }
    if extra:
        record.update(extra)

    try:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        _audit_logger.info(line)
        increment_event_count()
    except Exception:
        pass
