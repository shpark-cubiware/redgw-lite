"""Audit 미들웨어·로거·인증 훅 통합 테스트.

세션 앱은 config.yaml 디폴트(Level=1)로 기동되므로 Level=1 동작은 session
client로 검증한다. Level=0/2 동작과 logger 모듈 단위 동작은 별도 격리 환경
(reset_audit_logger + 임시 디렉토리)에서 직접 호출로 검증한다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

import pytest

from tests.conftest import (
    ADMIN_KEY,
    HRM_KEY,
    MONITOR_KEY,
    INVALID_KEY,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 공통 헬퍼
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _audit_dir() -> Path:
    return Path(os.environ.get("REDGW_AUDIT_DIRECTORY", "/tmp/audit-test"))


def _read_audit_lines(directory: Path | None = None) -> list[dict]:
    directory = directory or _audit_dir()
    if not directory.exists():
        return []
    lines: list[dict] = []
    for fpath in sorted(directory.glob("audit.*.log")):
        try:
            content = fpath.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in content.splitlines():
            if raw.strip():
                try:
                    lines.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
    return lines


async def _flush_audit(timeout: float = 0.5) -> None:
    """audit 큐 drain — 백그라운드 listener thread가 파일에 쓰도록 기다림."""
    from app.audit import logger as _mod
    if _mod._audit_queue is None:
        return
    deadline = time.time() + timeout
    while not _mod._audit_queue.empty() and time.time() < deadline:
        await asyncio.sleep(0.01)
    # 추가 여유 — listener가 emit 호출 후 stream flush까지
    await asyncio.sleep(0.05)


async def _lines_since(before: int) -> list[dict]:
    """before 인덱스 이후 추가된 라인만 반환."""
    await _flush_audit()
    return _read_audit_lines()[before:]


def _last_line_for(lines: list[dict], path: str, method: str | None = None) -> dict | None:
    for entry in reversed(lines):
        if entry.get("path") == path and (method is None or entry.get("method") == method):
            return entry
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 레벨 동작 (모듈 단위)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _restore_session_audit():
    """세션 audit logger를 conftest와 동일하게 재초기화."""
    from app.audit import init_audit_logger, reset_audit_logger
    from app.config import get_settings
    reset_audit_logger()
    init_audit_logger(get_settings().audit)


class TestAuditLevels:
    """init_audit_logger의 Level별 동작 — 모듈 단위 격리 검증."""

    def setup_method(self):
        from app.audit import reset_audit_logger
        reset_audit_logger()

    def teardown_method(self):
        _restore_session_audit()

    def test_level_0_no_directory(self, tmp_path):
        """Level=0 init 호출되어도 디렉토리 생성 안 함."""
        from app.audit import init_audit_logger, is_active
        from app.config import AuditConfig

        target = tmp_path / "shouldnt_exist"
        init_audit_logger(AuditConfig(level=0, directory=str(target)))

        assert not target.exists()
        assert is_active() is False

    def test_level_1_creates_directory(self, tmp_path):
        """Level=1 init 시 디렉토리 자동 생성."""
        from app.audit import init_audit_logger, is_active
        from app.config import AuditConfig

        target = tmp_path / "audit_l1"
        init_audit_logger(AuditConfig(level=1, directory=str(target)))

        assert target.exists()
        assert is_active() is True

    def test_level_2_creates_directory(self, tmp_path):
        """Level=2 init도 동일."""
        from app.audit import init_audit_logger, is_active
        from app.config import AuditConfig

        target = tmp_path / "audit_l2"
        init_audit_logger(AuditConfig(level=2, directory=str(target)))

        assert target.exists()
        assert is_active() is True

    def test_level_0_record_event_noop(self, tmp_path):
        """Level=0에서 record_event 호출은 무동작."""
        from app.audit import init_audit_logger, record_event, get_event_count
        from app.config import AuditConfig

        init_audit_logger(AuditConfig(level=0, directory=str(tmp_path / "off")))
        record_event(event="auth_fail", scope={"method": "GET", "path": "/x"})
        assert get_event_count() == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 헬퍼 함수 단위 테스트
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestAuditHelpers:
    """logger 모듈의 헬퍼 함수 단위 동작."""

    def test_validate_request_id_valid(self):
        from app.audit import validate_request_id
        rid = validate_request_id("req-abc_123")
        assert rid == "req-abc_123"

    def test_validate_request_id_too_long(self):
        from app.audit import validate_request_id
        # 129자 → 새 UUID4
        long_id = "a" * 129
        rid = validate_request_id(long_id)
        assert rid != long_id
        # UUID4 형식 확인
        uuid.UUID(rid)

    def test_validate_request_id_invalid_chars(self):
        from app.audit import validate_request_id
        # 공백 포함 → 새 UUID4
        rid = validate_request_id("abc def")
        assert rid != "abc def"
        uuid.UUID(rid)

    def test_validate_request_id_empty(self):
        from app.audit import validate_request_id
        rid = validate_request_id(None)
        uuid.UUID(rid)
        rid2 = validate_request_id("")
        uuid.UUID(rid2)



    def test_detect_admin_action_none_for_generic(self):
        from app.audit import detect_admin_action
        assert detect_admin_action("/api/v1/admin/keys") is None
        assert detect_admin_action("/api/v1/admin/stats") is None
        assert detect_admin_action("/api/v1/ns/HRM/kv/foo") is None

    def test_extract_ns_key(self):
        from app.audit import extract_ns, extract_key
        path = "/api/v1/ns/HRM/kv/order:001"
        assert extract_ns(path) == "HRM"
        assert extract_key(path) == "order:001"

    def test_extract_ns_none_for_non_api_path(self):
        from app.audit import extract_ns, extract_key
        assert extract_ns("/health") is None
        assert extract_key("/admin/keys") is None

    def test_format_iso_ts_now(self):
        from app.audit import format_iso_ts_now
        ts = format_iso_ts_now()
        # YYYY-MM-DDTHH:MM:SS.mmmZ 형태
        assert ts.endswith("Z")
        assert "T" in ts
        assert len(ts) == 24  # "2026-05-29T03:14:25.123Z"

    def test_extract_ip_prefers_x_real_ip(self):
        """X-Real-IP(nginx 강제)를 우선 — 위조 가능한 XFF 첫 값을 신뢰하지 않는다."""
        from app.audit import extract_ip
        scope = {
            "headers": [
                # 클라이언트가 위조한 XFF가 앞에 와도 무시되어야 함
                (b"x-forwarded-for", b"1.2.3.4, 10.0.0.9"),
                (b"x-real-ip", b"10.0.0.9"),
            ],
            "client": ("172.17.0.5", 5000),
        }
        assert extract_ip(scope) == "10.0.0.9"

    def test_extract_ip_falls_back_to_scope_client(self):
        """X-Real-IP가 없으면 직접 peer로 폴백."""
        from app.audit import extract_ip
        scope = {"headers": [(b"x-forwarded-for", b"1.2.3.4")], "client": ("172.17.0.5", 5000)}
        assert extract_ip(scope) == "172.17.0.5"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Level=1 미들웨어 캡처 — 세션 앱 사용
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestAuditMiddlewareLevel1:
    """세션 앱(Level=1 디폴트)으로 미들웨어 캡처 동작 확인."""

    @pytest.fixture(autouse=True)
    def _ensure_audit_active(self):
        from app.audit import is_active
        assert is_active(), "Session app must have audit active at Level>=1"

    async def _baseline(self) -> int:
        await _flush_audit()
        return len(_read_audit_lines())

    @pytest.mark.asyncio
    async def test_put_kv_captured(self, client):
        before = await self._baseline()
        path = "/api/v1/ns/HRM/kv/audit_put_kv"
        resp = await client.put(
            path,
            headers={"X-API-Key": HRM_KEY, "Content-Type": "application/json"},
            json={"value": "hello"},
        )
        assert resp.status_code == 200

        lines = await _lines_since(before)
        entry = _last_line_for(lines, path, "PUT")
        assert entry is not None
        assert entry["status"] == 200
        assert entry["ns"] == "HRM"
        assert entry["key"] == "audit_put_kv"
        assert entry["value_hash"] is not None
        assert entry["value_size"] > 0
        assert entry["value_type"] == "json"
        assert entry["schema_version"] == 1
        assert entry["event"] == "api_call"

    @pytest.mark.asyncio
    async def test_delete_kv_captured(self, client):
        # 먼저 키 생성
        await client.put(
            "/api/v1/ns/HRM/kv/audit_del",
            headers={"X-API-Key": HRM_KEY, "Content-Type": "application/json"},
            json={"value": "x"},
        )
        before = await self._baseline()
        resp = await client.delete(
            "/api/v1/ns/HRM/kv/audit_del",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code in (200, 204)

        lines = await _lines_since(before)
        entry = _last_line_for(lines, "/api/v1/ns/HRM/kv/audit_del", "DELETE")
        assert entry is not None

    @pytest.mark.asyncio
    async def test_get_not_captured_at_level_1(self, client):
        # 먼저 키 생성
        await client.put(
            "/api/v1/ns/HRM/kv/audit_get",
            headers={"X-API-Key": HRM_KEY, "Content-Type": "application/json"},
            json={"value": "y"},
        )
        before = await self._baseline()
        resp = await client.get(
            "/api/v1/ns/HRM/kv/audit_get",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 200

        lines = await _lines_since(before)
        # GET 경로가 캡처된 라인은 없어야 함
        get_lines = [
            ln for ln in lines
            if ln.get("method") == "GET" and ln.get("path") == "/api/v1/ns/HRM/kv/audit_get"
        ]
        assert get_lines == []

    @pytest.mark.asyncio
    async def test_value_hash_matches_body(self, client):
        body_bytes = b'{"value":"hash-test-value"}'
        before = await self._baseline()
        await client.put(
            "/api/v1/ns/HRM/kv/audit_hash",
            headers={"X-API-Key": HRM_KEY, "Content-Type": "application/json"},
            content=body_bytes,
        )
        lines = await _lines_since(before)
        entry = _last_line_for(lines, "/api/v1/ns/HRM/kv/audit_hash", "PUT")
        assert entry is not None
        expected = hashlib.sha256(body_bytes).hexdigest()[:16]
        assert entry["value_hash"] == expected
        assert entry["value_size"] == len(body_bytes)

    @pytest.mark.asyncio
    async def test_value_type_json(self, client):
        before = await self._baseline()
        await client.put(
            "/api/v1/ns/HRM/kv/audit_vt_json",
            headers={"X-API-Key": HRM_KEY, "Content-Type": "application/json"},
            json={"value": "1"},
        )
        lines = await _lines_since(before)
        entry = _last_line_for(lines, "/api/v1/ns/HRM/kv/audit_vt_json", "PUT")
        assert entry["value_type"] == "json"

    @pytest.mark.asyncio
    async def test_schema_version_in_every_record(self, client):
        before = await self._baseline()
        await client.put(
            "/api/v1/ns/HRM/kv/audit_sv",
            headers={"X-API-Key": HRM_KEY, "Content-Type": "application/json"},
            json={"value": "sv"},
        )
        lines = await _lines_since(before)
        assert lines
        for ln in lines:
            assert ln.get("schema_version") == 1

    @pytest.mark.asyncio
    async def test_client_id_populated_on_success(self, client):
        before = await self._baseline()
        await client.put(
            "/api/v1/ns/HRM/kv/audit_cid",
            headers={"X-API-Key": HRM_KEY, "Content-Type": "application/json"},
            json={"value": "v"},
        )
        lines = await _lines_since(before)
        entry = _last_line_for(lines, "/api/v1/ns/HRM/kv/audit_cid", "PUT")
        assert entry["client_id"] == "HRM"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 이벤트 캡처 — 인증·권한·레이트 (Level=1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestAuditEvents:

    async def _baseline(self) -> int:
        await _flush_audit()
        return len(_read_audit_lines())

    @pytest.mark.asyncio
    async def test_auth_fail_event(self, client):
        before = await self._baseline()
        resp = await client.put(
            "/api/v1/ns/HRM/kv/auth_fail_key",
            headers={"X-API-Key": INVALID_KEY, "Content-Type": "application/json"},
            json={"value": "x"},
        )
        assert resp.status_code == 401

        lines = await _lines_since(before)
        entry = _last_line_for(lines, "/api/v1/ns/HRM/kv/auth_fail_key", "PUT")
        assert entry is not None
        assert entry["event"] == "auth_fail"
        assert entry["error_code"] == "UNAUTHORIZED"
        assert entry["status"] == 401
        assert entry["client_id"] is None

    @pytest.mark.asyncio
    async def test_namespace_denied_event(self, client):
        # MONITORING은 모든 NS read만 가능 → write 시도 시 NAMESPACE_DENIED
        before = await self._baseline()
        resp = await client.put(
            "/api/v1/ns/HRM/kv/ns_denied_key",
            headers={"X-API-Key": MONITOR_KEY, "Content-Type": "application/json"},
            json={"value": "x"},
        )
        assert resp.status_code == 403

        lines = await _lines_since(before)
        entry = _last_line_for(lines, "/api/v1/ns/HRM/kv/ns_denied_key", "PUT")
        assert entry is not None
        assert entry["event"] == "namespace_denied"
        assert entry["error_code"] == "NAMESPACE_DENIED"
        assert entry["status"] == 403

    @pytest.mark.asyncio
    async def test_admin_required_event(self, client):
        # 일반 클라이언트가 admin 전용 작업 시도 → ADMIN_REQUIRED
        before = await self._baseline()
        resp = await client.delete(
            "/api/v1/admin/keys/HRM/kv/some_key",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 403

        lines = await _lines_since(before)
        entry = _last_line_for(lines, "/api/v1/admin/keys/HRM/kv/some_key", "DELETE")
        assert entry is not None
        assert entry["event"] == "admin_required"
        assert entry["error_code"] == "ADMIN_REQUIRED"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# X-Request-ID 동작 (Level=1, AccessLog 통합)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestAuditRequestId:

    async def _baseline(self) -> int:
        await _flush_audit()
        return len(_read_audit_lines())

    @pytest.mark.asyncio
    async def test_request_id_generated_when_absent(self, client):
        before = await self._baseline()
        resp = await client.put(
            "/api/v1/ns/HRM/kv/rid_gen",
            headers={"X-API-Key": HRM_KEY, "Content-Type": "application/json"},
            json={"value": "v"},
        )
        assert resp.status_code == 200
        # 응답 헤더 echo
        rid = resp.headers.get("x-request-id")
        assert rid
        uuid.UUID(rid)
        # audit 라인의 request_id와 일치
        lines = await _lines_since(before)
        entry = _last_line_for(lines, "/api/v1/ns/HRM/kv/rid_gen", "PUT")
        assert entry["request_id"] == rid

    @pytest.mark.asyncio
    async def test_request_id_accepted_when_valid(self, client):
        before = await self._baseline()
        my_id = "test-req-abc_123"
        resp = await client.put(
            "/api/v1/ns/HRM/kv/rid_accept",
            headers={
                "X-API-Key": HRM_KEY,
                "Content-Type": "application/json",
                "X-Request-ID": my_id,
            },
            json={"value": "v"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("x-request-id") == my_id

        lines = await _lines_since(before)
        entry = _last_line_for(lines, "/api/v1/ns/HRM/kv/rid_accept", "PUT")
        assert entry["request_id"] == my_id

    @pytest.mark.asyncio
    async def test_request_id_rejected_when_too_long(self, client):
        long_id = "a" * 129
        resp = await client.put(
            "/api/v1/ns/HRM/kv/rid_long",
            headers={
                "X-API-Key": HRM_KEY,
                "Content-Type": "application/json",
                "X-Request-ID": long_id,
            },
            json={"value": "v"},
        )
        assert resp.status_code == 200
        echoed = resp.headers.get("x-request-id")
        assert echoed != long_id
        uuid.UUID(echoed)

    @pytest.mark.asyncio
    async def test_request_id_rejected_when_invalid_chars(self, client):
        bad_id = "has space"
        resp = await client.put(
            "/api/v1/ns/HRM/kv/rid_bad",
            headers={
                "X-API-Key": HRM_KEY,
                "Content-Type": "application/json",
                "X-Request-ID": bad_id,
            },
            json={"value": "v"},
        )
        assert resp.status_code == 200
        echoed = resp.headers.get("x-request-id")
        assert echoed != bad_id
        uuid.UUID(echoed)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# exclude_paths 필터 (Level=1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestAuditExclude:

    @pytest.mark.asyncio
    async def test_health_not_captured(self, client):
        await _flush_audit()
        before = len(_read_audit_lines())
        resp = await client.get("/health")
        assert resp.status_code in (200, 503)
        await _flush_audit()
        lines = _read_audit_lines()[before:]
        # /health는 캡처되지 않음
        health_lines = [ln for ln in lines if ln.get("path") == "/health"]
        assert health_lines == []
        # 다만 X-Request-ID 헤더는 echo 되어야 함
        assert resp.headers.get("x-request-id")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 수명주기 (logger 모듈 단위)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestAuditLifecycle:

    def setup_method(self):
        from app.audit import reset_audit_logger
        reset_audit_logger()

    def teardown_method(self):
        _restore_session_audit()

    def test_shutdown_returns_event_count(self, tmp_path):
        from app.audit import (
            init_audit_logger,
            record_event,
            shutdown_audit_logger,
        )
        from app.config import AuditConfig

        init_audit_logger(AuditConfig(level=1, directory=str(tmp_path / "a")))

        # 이벤트 3개 기록
        for i in range(3):
            record_event(
                event="auth_fail",
                scope={"method": "PUT", "path": f"/api/v1/ns/HRM/kv/x{i}"},
                error_code="UNAUTHORIZED",
            )
        time.sleep(0.1)  # listener thread flush
        count = shutdown_audit_logger()
        assert count == 3

    def test_per_worker_files_naming(self, tmp_path):
        from app.audit import (
            init_audit_logger,
            record_event,
            shutdown_audit_logger,
        )
        from app.config import AuditConfig

        target = tmp_path / "per_worker"
        init_audit_logger(
            AuditConfig(level=1, directory=str(target), per_worker_files=True)
        )
        record_event(
            event="auth_fail",
            scope={"method": "GET", "path": "/api/v1/ns/X/kv/y"},
            error_code="UNAUTHORIZED",
        )
        time.sleep(0.1)
        shutdown_audit_logger()

        # 파일명에 pid가 들어가야 함
        my_pid = os.getpid()
        matching = list(target.glob(f"audit.w{my_pid}.*.log"))
        assert matching, f"expected audit.w{my_pid}.*.log in {list(target.iterdir())}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Level=2 동작 (별도 인스턴스)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestAuditLevel2:
    """Level=2 동작은 별도 init으로 모듈 단위 검증 (세션 앱은 Level=1)."""

    def setup_method(self):
        from app.audit import reset_audit_logger
        reset_audit_logger()

    def teardown_method(self):
        _restore_session_audit()

    def test_level_2_record_event_captures(self, tmp_path):
        from app.audit import (
            init_audit_logger,
            record_event,
            get_event_count,
        )
        from app.config import AuditConfig

        init_audit_logger(AuditConfig(level=2, directory=str(tmp_path / "l2")))
        # Level 2에서도 record_event는 정상 동작 (auth_fail 캡처)
        record_event(
            event="auth_fail",
            scope={"method": "GET", "path": "/api/v1/ns/A/kv/k"},
            error_code="UNAUTHORIZED",
        )
        assert get_event_count() == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 라우터별 쓰기 캡처 — Level=1 (PR-2 매트릭스 보강)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestAuditRouterCaptureLevel1:
    """모든 쓰기 라우터(kv/map/queue/group/rank/event)가 Level=1에서 캡처되는지."""

    async def _baseline(self) -> int:
        await _flush_audit()
        return len(_read_audit_lines())

    async def _check_capture(self, client, path, method, json_body=None):
        """status와 무관하게 audit 라인 캡처 확인."""
        before = await self._baseline()
        if method == "PUT":
            await client.put(
                path,
                headers={"X-API-Key": HRM_KEY, "Content-Type": "application/json"},
                json=json_body or {"value": "v"},
            )
        elif method == "POST":
            await client.post(
                path,
                headers={"X-API-Key": HRM_KEY, "Content-Type": "application/json"},
                json=json_body or {"value": "v"},
            )
        elif method == "DELETE":
            await client.delete(path, headers={"X-API-Key": HRM_KEY})
        else:
            raise ValueError(method)
        lines = await _lines_since(before)
        entry = _last_line_for(lines, path, method)
        assert entry is not None, f"{method} {path} not captured"
        assert entry["event"] == "api_call"
        return entry

    @pytest.mark.asyncio
    async def test_capture_kv_put(self, client):
        await self._check_capture(client, "/api/v1/ns/HRM/kv/router_kv_w", "PUT")

    @pytest.mark.asyncio
    async def test_capture_map_put(self, client):
        await self._check_capture(
            client,
            "/api/v1/ns/HRM/map/router_map_w",
            "PUT",
            {"fields": {"a": "1"}},
        )

    @pytest.mark.asyncio
    async def test_capture_queue_post(self, client):
        await self._check_capture(
            client,
            "/api/v1/ns/HRM/queue/router_q_w",
            "POST",
            {"value": "x"},
        )

    @pytest.mark.asyncio
    async def test_capture_group_post(self, client):
        await self._check_capture(
            client,
            "/api/v1/ns/HRM/group/router_g_w",
            "POST",
            {"members": ["a", "b"]},
        )

    @pytest.mark.asyncio
    async def test_capture_rank_post(self, client):
        await self._check_capture(
            client,
            "/api/v1/ns/HRM/rank/router_r_w",
            "POST",
            {"members": [{"member": "a", "score": 1}]},
        )

    @pytest.mark.asyncio
    async def test_capture_event_post(self, client):
        await self._check_capture(
            client,
            "/api/v1/ns/HRM/event/router_e_w",
            "POST",
            {"data": {"k": "v"}},
        )

    @pytest.mark.asyncio
    async def test_pubsub_publish_not_captured_at_level_1(self, client):
        """Pub/Sub publish는 Level 2 전용 — Level 1에서 미캡처."""
        before = await self._baseline()
        path = "/api/v1/ns/HRM/publish/ch1"
        resp = await client.post(
            path,
            headers={"X-API-Key": HRM_KEY, "Content-Type": "application/json"},
            json={"message": "hi"},
        )
        # publish는 정상 200/201
        assert resp.status_code in (200, 201, 204)
        lines = await _lines_since(before)
        pub_lines = [ln for ln in lines if ln.get("path") == path]
        assert pub_lines == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# rate_limit_exceeded 이벤트
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestAuditRateLimit:
    """rate_limit_exceeded 이벤트의 audit 로거 기록을 단위 검증한다.
    실 rate limit은 Nginx(10r/s)가 앱 도달 전 429로 차단하므로 앱은 이 이벤트를
    직접 생성하지 않는다 — record_event로 로거 동작만 검증."""

    def setup_method(self):
        from app.audit import reset_audit_logger
        reset_audit_logger()

    def teardown_method(self):
        _restore_session_audit()

    def test_record_event_rate_limit(self, tmp_path):
        from app.audit import (
            init_audit_logger,
            record_event,
            get_event_count,
        )
        from app.config import AuditConfig

        init_audit_logger(AuditConfig(level=1, directory=str(tmp_path / "rl")))
        record_event(
            event="rate_limit_exceeded",
            scope={"method": "GET", "path": "/api/v1/ns/HRM/kv/foo"},
            error_code="RATE_LIMIT_EXCEEDED",
        )
        assert get_event_count() == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# value_type binary·string 판정 (Level=1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestAuditValueType:

    async def _baseline(self) -> int:
        await _flush_audit()
        return len(_read_audit_lines())

    @pytest.mark.asyncio
    async def test_value_type_binary_content_type(self, client):
        body = b"\x00\x01\x02hello"
        before = await self._baseline()
        await client.put(
            "/api/v1/ns/HRM/kv/vt_bin",
            headers={
                "X-API-Key": HRM_KEY,
                "Content-Type": "application/octet-stream",
            },
            content=body,
        )
        lines = await _lines_since(before)
        entry = _last_line_for(lines, "/api/v1/ns/HRM/kv/vt_bin", "PUT")
        assert entry is not None
        assert entry["value_type"] == "binary"

    @pytest.mark.asyncio
    async def test_value_type_string_default(self, client):
        # text/plain → string
        before = await self._baseline()
        await client.put(
            "/api/v1/ns/HRM/kv/vt_str",
            headers={"X-API-Key": HRM_KEY, "Content-Type": "text/plain"},
            content=b"plain text body",
        )
        lines = await _lines_since(before)
        entry = _last_line_for(lines, "/api/v1/ns/HRM/kv/vt_str", "PUT")
        if entry is not None:
            assert entry["value_type"] == "string"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Q3 Correlation ID (audit.correlation)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestAuditCorrelation:
    """audit.correlation=true 시 origin 메타 SET + 조회 엔드포인트."""

    def setup_method(self):
        from app.audit import reset_audit_logger
        reset_audit_logger()
        # 세션 settings를 임시로 correlation=true로 바꿔서 init
        from app.config import get_settings
        s = get_settings()
        self._orig = s.audit.correlation
        s.audit.correlation = True
        from app.audit import init_audit_logger
        init_audit_logger(s.audit)

    def teardown_method(self):
        from app.config import get_settings
        get_settings().audit.correlation = self._orig
        _restore_session_audit()

    @pytest.mark.asyncio
    async def test_origin_set_on_write(self, client):
        """PUT /kv → __redgw:audit:origin:* 키가 Redis에 생성된다."""
        from app.redis_client import get_redis_manager
        from app.utils.key_builder import build_key

        resp = await client.put(
            "/api/v1/ns/HRM/kv/corr_write",
            headers={"X-API-Key": HRM_KEY, "Content-Type": "application/json"},
            json={"value": "x"},
        )
        assert resp.status_code == 200

        # 직접 Redis 조회
        await asyncio.sleep(0.05)
        r = get_redis_manager().get_client()
        redis_key = build_key("HRM", "kv", "corr_write")
        origin_key = f"__redgw:audit:origin:{redis_key}"
        raw = await r.get(origin_key)
        assert raw is not None, f"origin key missing: {origin_key}"
        meta = json.loads(raw)
        assert "request_id" in meta
        assert meta["client_id"] == "HRM"
        assert "ts" in meta

    @pytest.mark.asyncio
    async def test_origin_queue_uses_storage_prefix(self, client):
        """queue write origin은 저장 접두어 q로 저장돼 admin 조회와 일치한다(R2 회귀).

        미수정 시 origin은 URL 토큰 'queue'로 저장되어 admin 읽기(resolve_type_prefix→q)와
        어긋나 /admin/audit/origin 조회가 404가 됐다(kv/map/rank는 토큰==접두어라 우연히 통과).
        """
        from app.redis_client import get_redis_manager
        from app.utils.key_builder import build_key

        resp = await client.post(
            "/api/v1/ns/HRM/queue/corr_q",
            headers={"X-API-Key": HRM_KEY, "Content-Type": "application/json"},
            json={"value": "x"},
        )
        assert resp.status_code < 400
        await asyncio.sleep(0.05)

        r = get_redis_manager().get_client()
        # 저장 접두어 q로 origin이 있어야 한다(URL 토큰 queue가 아니라)
        assert await r.get(
            f"__redgw:audit:origin:{build_key('HRM', 'q', 'corr_q')}"
        ) is not None
        assert await r.get("__redgw:audit:origin:HRM:queue:corr_q") is None

        # admin 조회도 200으로 일치
        resp2 = await client.get(
            "/api/v1/admin/audit/origin/HRM/queue/corr_q",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert resp2.status_code == 200

    @pytest.mark.asyncio
    async def test_no_phantom_origin_on_action_routes(self, client):
        """batch·ops 등 다중 세그먼트 액션 라우트는 phantom origin 키를 만들지 않는다.

        extract_key(정규식)는 'batch'·'ops/inter'를 key로 잡아 존재하지 않는 origin 키
        (예: HRM:grp:ops/inter)를 생성했다. correlation origin을 path_params["key"] 기반으로
        전환해, key 파라미터가 없는 라우트는 origin을 만들지 않는다(정상 단일 키는 불변).
        """
        from app.redis_client import get_redis_manager
        from app.utils.key_builder import build_key

        r = get_redis_manager().get_client()
        h = {"X-API-Key": HRM_KEY, "Content-Type": "application/json"}

        # kv batch(MSET) — 라우트에 key 파라미터 없음
        resp = await client.put(
            "/api/v1/ns/HRM/kv/batch", headers=h,
            json={"items": {"pa": "1", "pb": "2"}},
        )
        assert resp.status_code < 400

        # group ops/inter — 다중 세그먼트 액션 라우트
        for name, members in (("ps1", ["a", "b"]), ("ps2", ["b", "c"])):
            await client.post(
                f"/api/v1/ns/HRM/group/{name}", headers=h,
                json={"members": members},
            )
        resp2 = await client.post(
            "/api/v1/ns/HRM/group/ops/inter", headers=h,
            json={"keys": ["ps1", "ps2"]},
        )
        assert resp2.status_code < 400
        await asyncio.sleep(0.05)

        # phantom origin 키가 없어야 한다
        assert await r.get("__redgw:audit:origin:HRM:kv:batch") is None
        assert await r.get("__redgw:audit:origin:HRM:grp:ops/inter") is None
        # 대조: 정상 데이터 키(group POST /{key})는 origin이 정상 생성된다
        assert await r.get(
            f"__redgw:audit:origin:{build_key('HRM', 'grp', 'ps1')}"
        ) is not None

    @pytest.mark.asyncio
    async def test_admin_audit_origin_endpoint(self, client):
        """GET /admin/audit/origin/{ns}/{type}/{key} — admin 권한, origin 반환."""
        # write로 origin 생성
        await client.put(
            "/api/v1/ns/HRM/kv/corr_endpoint",
            headers={"X-API-Key": HRM_KEY, "Content-Type": "application/json"},
            json={"value": "v"},
        )
        await asyncio.sleep(0.05)

        # admin 권한 없으면 403
        resp_403 = await client.get(
            "/api/v1/admin/audit/origin/HRM/kv/corr_endpoint",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp_403.status_code == 403

        # admin으로 조회
        resp = await client.get(
            "/api/v1/admin/audit/origin/HRM/kv/corr_endpoint",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "request_id" in body
        assert body["client_id"] == "HRM"
        assert "ttl_seconds" in body
        assert "redis_key" in body

    @pytest.mark.asyncio
    async def test_admin_audit_origin_not_found(self, client):
        """존재하지 않는 key는 404."""
        resp = await client.get(
            "/api/v1/admin/audit/origin/HRM/kv/never_written_key_xyz_123",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert resp.status_code == 404


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 드롭 카운터 (queue_full / disk_full)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestAuditDroppedCounters:

    def setup_method(self):
        from app.audit import reset_audit_logger, reset_dropped_counts
        reset_audit_logger()
        reset_dropped_counts()

    def teardown_method(self):
        from app.audit import reset_dropped_counts
        reset_dropped_counts()
        _restore_session_audit()

    def test_queue_full_increments_drop(self, tmp_path):
        """maxsize=2 큐에서 3건 emit → 마지막 1건 드롭."""
        from app.audit import init_audit_logger, get_dropped_counts
        from app.config import AuditConfig
        from queue import Queue
        import app.audit.logger as _mod

        init_audit_logger(AuditConfig(level=1, directory=str(tmp_path / "qf")))

        # 큐를 maxsize=2짜리로 교체 + listener 일시 중지 효과
        _mod._audit_queue = Queue(maxsize=2)
        if _mod._audit_listener:
            _mod._audit_listener.stop()
        # 새 핸들러로 교체 (listener 없음 → put 시 즉시 full)
        from app.audit.logger import _DropCountingQueueHandler
        for h in list(_mod._audit_logger.handlers):
            _mod._audit_logger.removeHandler(h)
        _mod._audit_logger.addHandler(_DropCountingQueueHandler(_mod._audit_queue))

        # 3건 emit
        _mod._audit_logger.info("line1")
        _mod._audit_logger.info("line2")
        _mod._audit_logger.info("line3")  # full → 드롭

        counts = get_dropped_counts()
        assert counts["queue_full"] >= 1

    def test_disk_full_increments_drop(self, tmp_path):
        """audit 디렉토리 총량 초과 시 가장 오래된 파일 삭제 + 카운터 증가."""
        from app.audit import init_audit_logger, get_dropped_counts
        from app.config import AuditConfig
        import os

        target = tmp_path / "audit_disk"
        target.mkdir()

        # 더미 audit .gz 5개 생성 (각 200KB)
        from datetime import datetime, timedelta
        for i in range(5):
            d = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
            f = target / f"audit.{d}.log.gz"
            f.write_bytes(b"x" * (200 * 1024))
            os.utime(f, (1700000000 + i, 1700000000 + i))

        # max_total = 500KB → 3개 삭제 필요
        init_audit_logger(
            AuditConfig(
                level=1,
                directory=str(target),
                max_total_size_mb=0,  # init에서는 동작 안 함
            )
        )

        # 핸들러를 가져와 직접 정리 트리거
        listener = None
        import app.audit.logger as _mod
        listener = _mod._audit_listener
        if listener and listener.handlers:
            audit_handler = listener.handlers[0]
            audit_handler.max_total_bytes = 500 * 1024
            audit_handler._cleanup_by_total_size()

        counts = get_dropped_counts()
        assert counts["disk_full"] >= 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /admin/health-detail에 dropped_local 노출 확인
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestHealthDetailDroppedExposure:

    @pytest.mark.asyncio
    async def test_health_detail_exposes_dropped_local(self, client):
        from tests.conftest import ADMIN_KEY
        resp = await client.get(
            "/api/v1/admin/health-detail",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "dropped_local" in body
        assert "queue_full" in body["dropped_local"]
        assert "disk_full" in body["dropped_local"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 시작 배너 — format_audit_banner 단위 테스트
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestAuditBanner:

    def test_banner_level_0_warning_3_lines(self, tmp_path):
        from app.audit.banner import format_audit_banner
        from app.config import AuditConfig

        lines = format_audit_banner(AuditConfig(level=0, directory=str(tmp_path)))
        assert len(lines) == 3
        assert all("WARNING" in l or "*" in l for l in lines)
        assert any("AUDIT_LEVEL=0" in l for l in lines)
        assert any("FULLY DISABLED" in l for l in lines)
        assert any("Not recommended for production" in l for l in lines)

    def test_banner_level_1_basic(self, tmp_path):
        from app.audit.banner import format_audit_banner
        from app.config import AuditConfig

        d = tmp_path / "audit_banner"
        d.mkdir()
        lines = format_audit_banner(AuditConfig(level=1, directory=str(d)))
        # 최소 6줄: Level, Directory, Disk, Usage, Limits, Policy
        assert len(lines) >= 6
        joined = "\n".join(lines)
        assert "Level         : 1 (BASIC)" in joined
        assert f"Directory     : {d}" in joined
        assert "Disk capacity" in joined
        assert "Current usage" in joined
        assert "Limits" in joined
        assert "Policy" in joined
        assert "payload=hash-only" in joined
        assert "correlation=false" in joined

    def test_banner_level_2_full_with_prefix(self, tmp_path):
        from app.audit.banner import format_audit_banner
        from app.config import AuditConfig

        d = tmp_path / "audit_l2"
        d.mkdir()
        lines = format_audit_banner(
            AuditConfig(level=2, directory=str(d), payload_prefix_bytes=128)
        )
        joined = "\n".join(lines)
        assert "Level         : 2 (FULL)" in joined
        assert "payload=hash+prefix(128B)" in joined

    def test_banner_low_disk_warning_appended(self, tmp_path):
        """disk_min_free_pct를 100으로 설정하면 항상 LOW DISK 트리거."""
        from app.audit.banner import format_audit_banner
        from app.config import AuditConfig

        d = tmp_path / "audit_lowdisk"
        d.mkdir()
        lines = format_audit_banner(
            AuditConfig(level=1, directory=str(d), disk_min_free_pct=100)
        )
        assert any("LOW DISK" in l for l in lines)

    def test_banner_directory_not_exists(self, tmp_path):
        """디렉토리 미생성 상태에서도 안전 (부팅 직후)."""
        from app.audit.banner import format_audit_banner
        from app.config import AuditConfig

        ghost = tmp_path / "never_created"
        lines = format_audit_banner(AuditConfig(level=1, directory=str(ghost)))
        joined = "\n".join(lines)
        assert "(directory not yet created)" in joined
        # Limits/Policy는 그래도 출력
        assert "Limits" in joined
        assert "Policy" in joined

    def test_banner_only_leader_worker(self):
        """리더 워커만 배너를 출력 — main.py에서 format_audit_banner 호출이
        is_startup_logger() 가드 블록 안에 위치해야 한다.

        회귀 보호: 가드가 깨지면 gunicorn 4 워커가 동일 배너를 4번 출력 (계획서 §6 PR-4).
        """
        import re
        from pathlib import Path

        src = Path("app/main.py").read_text(encoding="utf-8")
        match = re.search(
            r"if\s+is_startup_logger\s*\(\s*\)\s*:\s*\n(?:.*\n)*?\s*"
            r"for\s+\w+\s+in\s+format_audit_banner\s*\(",
            src,
        )
        assert match is not None, (
            "format_audit_banner 호출이 is_startup_logger() 가드 블록 안에 없다 — "
            "비-리더 워커도 배너를 출력하는 회귀 가능"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Level 2 캡처 동작 — _decide_capture 단위
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestAuditDecideCapture:
    """계획서 §6 PR-2 — Level 2가 GET·Pub/Sub publish를 캡처."""

    def test_level_2_captures_get(self):
        from app.audit.middleware import _decide_capture
        assert _decide_capture("GET", "/api/v1/ns/HRM/kv/foo", level=2) is True

    def test_level_2_captures_pubsub_publish(self):
        from app.audit.middleware import _decide_capture
        assert _decide_capture("POST", "/api/v1/ns/HRM/publish/ch1", level=2) is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Level 2 payload prefix 옵트인 가드
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestAuditPayloadPrefixGuard:
    """계획서 §6 PR-2 — payload_prefix_bytes=0이면 record에 value_prefix 키 없음."""

    def test_default_payload_prefix_zero(self):
        from app.config import AuditConfig
        assert AuditConfig().payload_prefix_bytes == 0

    def test_value_prefix_only_when_opted_in(self):
        """middleware 정적 검증: value_prefix 설정이 payload_prefix_bytes > 0 가드 안에 위치."""
        import re
        from pathlib import Path

        src = Path("app/audit/middleware.py").read_text(encoding="utf-8")
        # `config.payload_prefix_bytes > 0` 조건 안에서만 record["value_prefix"]를 설정
        pattern = (
            r"config\.payload_prefix_bytes\s*>\s*0[\s\S]{0,300}?"
            r'record\[\s*"value_prefix"\s*\]'
        )
        assert re.search(pattern, src), (
            "value_prefix 설정이 payload_prefix_bytes > 0 가드 밖에 있음 — "
            "디폴트 0에서도 평문 prefix가 기록되는 회귀 위험"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Level 0 미들웨어 미등록 가드
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestAuditMiddlewareGuards:
    """계획서 §6 PR-2 — Level=0이면 main.py에서 AuditMiddleware 등록 자체 안 함."""

    def test_level_0_no_middleware_static_guard(self):
        import re
        from pathlib import Path

        src = Path("app/main.py").read_text(encoding="utf-8")
        pattern = (
            r"if\s+settings\.audit\.level\s*>=\s*1\s*:\s*\n"
            r"(?:.*\n){0,3}?\s*app\.add_middleware\s*\(\s*AuditMiddleware"
        )
        assert re.search(pattern, src), (
            "AuditMiddleware add_middleware가 settings.audit.level >= 1 가드 안에 없다 — "
            "Level=0에서도 미들웨어가 등록되어 오버헤드 발생"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AccessLog ↔ Audit request_id 공유 (G4 결정)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestAuditAccessLogShares:
    """계획서 §6 PR-2 — Level≥1에서 운영 로그 한 줄에 `req=<id>` 포함, audit 라인과 동일."""

    @pytest.mark.asyncio
    async def test_accesslog_includes_audit_request_id(self, client, caplog):
        before = len(_read_audit_lines())
        with caplog.at_level("INFO", logger="redgw"):
            resp = await client.put(
                "/api/v1/ns/HRM/kv/al_rid",
                headers={"X-API-Key": HRM_KEY, "Content-Type": "application/json"},
                json={"value": "v"},
            )
        assert resp.status_code == 200
        rid = resp.headers.get("x-request-id")
        assert rid

        # AccessLog 라인 — middleware.py 형식 "%s %s %s %.2fms req=%s"
        access_lines = [r.getMessage() for r in caplog.records if "/al_rid" in r.getMessage()]
        assert any(f"req={rid}" in line for line in access_lines), (
            f"AccessLog 라인에 audit의 request_id가 없음: {access_lines}"
        )

        # audit 라인의 request_id도 일치
        await _flush_audit()
        lines = _read_audit_lines()[before:]
        entry = _last_line_for(lines, "/api/v1/ns/HRM/kv/al_rid", "PUT")
        assert entry is not None and entry["request_id"] == rid


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Shutdown summary line (PCI-DSS 10.2.6)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestAuditShutdownLine:
    """계획서 §7 — lifespan shutdown 훅이 종료 라인을 운영 로그에 출력."""

    def test_shutdown_summary_line_format(self):
        """main.py shutdown 훅에 'Audit shutdown — flushed {N} events since start' 출력."""
        from pathlib import Path
        src = Path("app/main.py").read_text(encoding="utf-8")
        assert "Audit shutdown" in src
        assert "flushed" in src and "events since start" in src


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /admin/health-detail — Level=0 분기
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestHealthDetailLevelZero:
    """계획서 §6 PR-3 — Level=0 부팅 시 audit_directory=None."""

    def test_directory_null_when_level_zero(self):
        from pathlib import Path
        src = Path("app/routers/admin.py").read_text(encoding="utf-8")
        # audit_cfg.level > 0 가드 안에서만 directory 노출
        assert "audit_cfg.directory if audit_cfg.level > 0 else None" in src


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 메트릭 카운터 증가 (Redis 키)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestMetricCounterIncrement:
    """계획서 §6 PR-3 — record_auth_failure_async가 Redis 카운터를 증가시킴."""

    @pytest.mark.asyncio
    async def test_auth_fail_increments_redis_counter(self, client):
        from app.redis_client import get_redis_manager

        r = get_redis_manager().get_client()
        key = "redgw:metrics:auth_failures:auth_fail"
        before = int(await r.get(key) or 0)

        resp = await client.put(
            "/api/v1/ns/HRM/kv/cnt_af",
            headers={"X-API-Key": INVALID_KEY, "Content-Type": "application/json"},
            json={"value": "x"},
        )
        assert resp.status_code == 401

        # api_key.py는 record_auth_failure_async를 await로 호출하므로 즉시 반영
        after = int(await r.get(key) or 0)
        assert after >= before + 1, f"auth_fail 카운터 미증가: before={before}, after={after}"


class TestAuditAuthFailOnReadPath:
    """GET(read) 인증 실패도 Level 1에서 감사된다(메서드 무관 — 문서 계약, R2)."""

    @pytest.mark.asyncio
    async def test_auth_fail_on_get_is_audited(self, client):
        before = len(_read_audit_lines())
        resp = await client.get(
            "/api/v1/ns/HRM/kv/audit-read-x",
            headers={"X-API-Key": "invalid_api_key_xxxxx"},
        )
        assert resp.status_code == 401
        lines = await _lines_since(before)
        entry = _last_line_for(lines, "/api/v1/ns/HRM/kv/audit-read-x", "GET")
        assert entry is not None, "GET read 인증 실패가 감사되지 않음"
        assert entry["event"] == "auth_fail"


class TestAuditExcludePathNotAudited:
    """exclude_paths의 pending event(인증/권한 실패)는 감사하지 않는다(R3 회귀 가드).

    실제 /metrics는 테스트 ASGI에서 404라 인증 실패를 못 만든다 — 미들웨어를 직접 구동해
    pending event가 설정된 응답을 exclude/비-exclude 경로로 보내 동작을 검증한다.
    """

    @staticmethod
    def _mock_app_setting_pending():
        from app.audit.logger import set_pending_event

        async def app(scope, receive, send):
            set_pending_event("auth_fail", "UNAUTHORIZED")
            await send({"type": "http.response.start", "status": 401, "headers": []})
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        return app

    @staticmethod
    async def _drive(path):
        from app.audit.middleware import AuditMiddleware

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_msg):
            pass

        mw = AuditMiddleware(TestAuditExcludePathNotAudited._mock_app_setting_pending())
        scope = {"type": "http", "path": path, "method": "GET", "headers": []}
        await mw(scope, receive, send)

    @pytest.mark.asyncio
    async def test_exclude_path_pending_not_audited(self):
        before = len(_read_audit_lines())
        await self._drive("/metrics")            # exclude_paths
        lines = await _lines_since(before)
        assert _last_line_for(lines, "/metrics", "GET") is None

    @pytest.mark.asyncio
    async def test_real_prometheus_path_excluded(self):
        """nginx가 rewrite하는 실제 경로 /api/v1/metrics도 exclude되어 감사 안 됨(R4)."""
        before = len(_read_audit_lines())
        await self._drive("/api/v1/metrics")
        lines = await _lines_since(before)
        assert _last_line_for(lines, "/api/v1/metrics", "GET") is None

    @pytest.mark.asyncio
    async def test_non_exclude_read_pending_audited(self):
        """대조군: 비-exclude GET read의 pending은 감사된다(R2 동작 보존)."""
        before = len(_read_audit_lines())
        await self._drive("/api/v1/ns/HRM/kv/unit-excl-ctrl")
        lines = await _lines_since(before)
        entry = _last_line_for(lines, "/api/v1/ns/HRM/kv/unit-excl-ctrl", "GET")
        assert entry is not None
        assert entry["event"] == "auth_fail"
