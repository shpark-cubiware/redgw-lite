"""감사 로그 패키지.

운영 로그(MAIN/CONFIG/STATUS)와 독립된 채널로 사용자 행위·인증·NS 권한
거부·admin 작업을 캡처한다. JSON 한 줄 포맷, 별도 디렉토리(/app/audit/).

레벨:
  0 = OFF (운영 금지)
  1 = BASIC (디폴트) — 쓰기·삭제·admin·인증/권한 실패
  2 = FULL — Level 1 + read + Pub/Sub publish + payload prefix(옵트인)
"""

from app.audit.logger import (
    consume_pending_event,
    detect_admin_action,
    extract_ip,
    extract_key,
    extract_ns,
    format_iso_ts_now,
    get_audit_config,
    get_audit_logger,
    get_dropped_counts,
    get_event_count,
    get_queue_size,
    increment_dropped_disk_full,
    increment_event_count,
    init_audit_logger,
    is_active,
    record_event,
    reset_audit_logger,
    reset_dropped_counts,
    set_pending_event,
    shutdown_audit_logger,
    validate_request_id,
)

__all__ = [
    "consume_pending_event",
    "detect_admin_action",
    "extract_ip",
    "extract_key",
    "extract_ns",
    "format_iso_ts_now",
    "get_audit_config",
    "get_audit_logger",
    "get_dropped_counts",
    "get_event_count",
    "get_queue_size",
    "increment_dropped_disk_full",
    "increment_event_count",
    "init_audit_logger",
    "is_active",
    "record_event",
    "reset_audit_logger",
    "reset_dropped_counts",
    "set_pending_event",
    "shutdown_audit_logger",
    "validate_request_id",
]
