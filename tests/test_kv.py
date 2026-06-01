"""
=============================================================================
String (KV) API 테스트 (test_kv.py)
=============================================================================

Redis String 타입을 사용하는 KV API의 전체 기능을 테스트합니다.

Redis 키 형식: {ns}:kv:{key}
엔드포인트:
  PUT    /ns/{ns}/kv/{key}       — 값 저장
  GET    /ns/{ns}/kv/{key}       — 값 조회
  DELETE /ns/{ns}/kv/{key}       — 값 삭제
  POST   /ns/{ns}/kv/{key}/incr  — 원자적 증가 (INCRBY)
  PUT    /ns/{ns}/kv/{key}/nx    — 값 없을 때만 저장 (SETNX, 분산 락)

테스트 시나리오:
  - 기본 CRUD (저장, 조회, 삭제)
  - 404 Not Found
  - TTL 설정
  - 카운터/시퀀스 (INCR)
  - 분산 락 (SETNX)
  - 캐시 프록시 패턴
  - 교차 네임스페이스 읽기
  - 진행률 공유
=============================================================================
"""

import pytest
from httpx import AsyncClient

from tests.conftest import HRM_KEY, ERP_KEY, CRM_KEY


class TestKvCrud:
    """String 타입 기본 CRUD 테스트"""

    async def test_set_and_get(self, client: AsyncClient):
        """값 저장 후 조회 — 응답 구조 검증"""
        # 저장
        resp = await client.put(
            "/api/v1/ns/HRM/kv/status",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "running", "ttl": 60},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["data"]["value"] == "running"
        assert data["meta"]["ns"] == "HRM"
        assert data["meta"]["type"] == "string"
        assert data["meta"]["ttl"] > 0

        # 조회
        resp = await client.get(
            "/api/v1/ns/HRM/kv/status",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["value"] == "running"

    async def test_get_not_found(self, client: AsyncClient):
        """존재하지 않는 키 조회 → 404 KEY_NOT_FOUND"""
        resp = await client.get(
            "/api/v1/ns/HRM/kv/nonexistent",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"]["code"] == "KEY_NOT_FOUND"

    async def test_delete(self, client: AsyncClient):
        """값 삭제 후 조회 시 404"""
        await client.put(
            "/api/v1/ns/HRM/kv/to-delete",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "temp"},
        )
        resp = await client.delete(
            "/api/v1/ns/HRM/kv/to-delete",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["deleted"] is True

        # 삭제 확인
        resp = await client.get(
            "/api/v1/ns/HRM/kv/to-delete",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 404

    async def test_delete_not_found(self, client: AsyncClient):
        """존재하지 않는 키 삭제 → 404"""
        resp = await client.delete(
            "/api/v1/ns/HRM/kv/nonexistent",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 404

    async def test_overwrite_value(self, client: AsyncClient):
        """같은 키에 재저장 → 값 덮어쓰기"""
        await client.put(
            "/api/v1/ns/HRM/kv/status",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "old"},
        )
        await client.put(
            "/api/v1/ns/HRM/kv/status",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "new"},
        )
        resp = await client.get(
            "/api/v1/ns/HRM/kv/status",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.json()["data"]["value"] == "new"


class TestKvIncr:
    """INCRBY — 카운터/시퀀스 테스트"""

    async def test_incr_sequence(self, client: AsyncClient):
        """시퀀스 생성 — 원자적 증가로 고유 번호 발급"""
        # 첫 번째 호출: 키가 없으면 0에서 시작
        resp = await client.post(
            "/api/v1/ns/shared/kv/seq:order-number/incr",
            headers={"X-API-Key": ERP_KEY},
            json={"delta": 1},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["value"] == "1"

        # 두 번째 호출
        resp = await client.post(
            "/api/v1/ns/shared/kv/seq:order-number/incr",
            headers={"X-API-Key": ERP_KEY},
            json={"delta": 1},
        )
        assert resp.json()["data"]["value"] == "2"

    async def test_incr_by_delta(self, client: AsyncClient):
        """delta 지정 — 한 번에 여러 단위 증가"""
        resp = await client.post(
            "/api/v1/ns/HRM/kv/stats:api-calls/incr",
            headers={"X-API-Key": HRM_KEY},
            json={"delta": 50},
        )
        assert resp.json()["data"]["value"] == "50"

    async def test_incr_counter_shared(self, client: AsyncClient):
        """공유 카운터 — 여러 시스템이 동일 카운터 사용"""
        await client.post(
            "/api/v1/ns/shared/kv/counter:total-requests/incr",
            headers={"X-API-Key": HRM_KEY},
            json={"delta": 10},
        )
        await client.post(
            "/api/v1/ns/shared/kv/counter:total-requests/incr",
            headers={"X-API-Key": ERP_KEY},
            json={"delta": 20},
        )
        resp = await client.get(
            "/api/v1/ns/shared/kv/counter:total-requests",
            headers={"X-API-Key": CRM_KEY},
        )
        assert resp.json()["data"]["value"] == "30"

    async def test_incr_delta_out_of_range_rejected(self, client: AsyncClient):
        """int64 범위를 벗어난 delta 값은 422로 차단 — Redis INCRBY 인자 범위초과(500) 방지
        (합산 오버플로·비정수 기존값은 incr_kv가 400 INVALID_VALUE로 처리 — 아래 테스트)"""
        resp = await client.post(
            "/api/v1/ns/HRM/kv/stats:overflow/incr",
            headers={"X-API-Key": HRM_KEY},
            json={"delta": 99999999999999999999999999},
        )
        assert resp.status_code == 422

    async def test_incr_non_integer_existing_rejected(self, client: AsyncClient):
        """비정수 기존값에 incr → 400 INVALID_VALUE (미처리 500 아님)"""
        await client.put(
            "/api/v1/ns/HRM/kv/stats:nonint",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "abc"},
        )
        resp = await client.post(
            "/api/v1/ns/HRM/kv/stats:nonint/incr",
            headers={"X-API-Key": HRM_KEY},
            json={"delta": 1},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "INVALID_VALUE"

    async def test_incr_cumulative_overflow_rejected(self, client: AsyncClient):
        """기존값+delta 합이 int64 초과 → 400 INVALID_VALUE (미처리 500 아님)"""
        await client.put(
            "/api/v1/ns/HRM/kv/stats:ovfsum",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "9223372036854775807"},
        )
        resp = await client.post(
            "/api/v1/ns/HRM/kv/stats:ovfsum/incr",
            headers={"X-API-Key": HRM_KEY},
            json={"delta": 1},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "INVALID_VALUE"


class TestKvSetnx:
    """SETNX — 분산 락 테스트"""

    async def test_setnx_acquire_lock(self, client: AsyncClient):
        """분산 락 획득 성공"""
        resp = await client.put(
            "/api/v1/ns/shared/kv/lock:report-gen/nx",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "HRM", "ttl": 30},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["acquired"] is True

    async def test_setnx_conflict(self, client: AsyncClient):
        """분산 락 충돌 — 이미 보유 중이면 409 Conflict"""
        # HRM가 락 획득
        await client.put(
            "/api/v1/ns/shared/kv/lock:report-gen/nx",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "HRM", "ttl": 30},
        )
        # ERP가 동일 락 시도 → 실패
        resp = await client.put(
            "/api/v1/ns/shared/kv/lock:report-gen/nx",
            headers={"X-API-Key": ERP_KEY},
            json={"value": "ERP", "ttl": 30},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"]["code"] == "KEY_EXISTS"

    async def test_setnx_release_and_reacquire(self, client: AsyncClient):
        """락 해제 후 재획득 가능"""
        # 락 획득
        await client.put(
            "/api/v1/ns/shared/kv/lock:daily-report/nx",
            headers={"X-API-Key": ERP_KEY},
            json={"value": "ERP", "ttl": 30},
        )
        # 락 해제
        await client.delete(
            "/api/v1/ns/shared/kv/lock:daily-report",
            headers={"X-API-Key": ERP_KEY},
        )
        # HRM가 재획득
        resp = await client.put(
            "/api/v1/ns/shared/kv/lock:daily-report/nx",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "HRM", "ttl": 30},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["acquired"] is True


class TestKvScenarios:
    """실전 시나리오 테스트"""

    async def test_cross_namespace_read(self, client: AsyncClient):
        """HRM 상태를 ERP에서 읽기 (교차 네임스페이스)"""
        await client.put(
            "/api/v1/ns/HRM/kv/status",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "running"},
        )
        resp = await client.get(
            "/api/v1/ns/HRM/kv/status",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["value"] == "running"

    async def test_cache_proxy_pattern(self, client: AsyncClient):
        """캐시 프록시 패턴 — TTL 기반 캐싱"""
        # 캐시 미스 (404)
        resp = await client.get(
            "/api/v1/ns/shared/kv/cache:region-codes",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 404

        # 캐시에 저장 (TTL 600초)
        await client.put(
            "/api/v1/ns/shared/kv/cache:region-codes",
            headers={"X-API-Key": ERP_KEY},
            json={"value": '{"GN":"강남","SC":"서초"}', "ttl": 600},
        )

        # 캐시 히트 — 다른 시스템도 shared 네임스페이스로 조회 가능
        resp = await client.get(
            "/api/v1/ns/shared/kv/cache:region-codes",
            headers={"X-API-Key": CRM_KEY},
        )
        assert resp.status_code == 200
        assert "GN" in resp.json()["data"]["value"]

    async def test_progress_sharing(self, client: AsyncClient):
        """진행률 공유 — HRM 배치 처리 진행률을 ERP가 모니터링"""
        # HRM가 진행률 기록
        await client.put(
            "/api/v1/ns/HRM/kv/progress:batch-2024-01",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "35", "ttl": 3600},
        )
        # ERP가 진행률 확인 (교차 읽기)
        resp = await client.get(
            "/api/v1/ns/HRM/kv/progress:batch-2024-01",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["value"] == "35"

        # 진행률 업데이트
        await client.put(
            "/api/v1/ns/HRM/kv/progress:batch-2024-01",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "100", "ttl": 3600},
        )
        resp = await client.get(
            "/api/v1/ns/HRM/kv/progress:batch-2024-01",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.json()["data"]["value"] == "100"


class TestKvExists:
    """키 존재 여부 확인 (EXISTS) 테스트"""

    async def test_exists_true(self, client: AsyncClient):
        """존재하는 키 → exists=True"""
        await client.put(
            "/api/v1/ns/HRM/kv/check-me",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "here"},
        )
        resp = await client.get(
            "/api/v1/ns/HRM/kv/check-me/exists",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["exists"] is True

    async def test_exists_false(self, client: AsyncClient):
        """존재하지 않는 키 → exists=False"""
        resp = await client.get(
            "/api/v1/ns/HRM/kv/no-such-key/exists",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["exists"] is False


class TestKvBatch:
    """일괄 조회/저장 (MGET/MSET) 테스트"""

    async def test_batch_set_and_get(self, client: AsyncClient):
        """일괄 저장 후 일괄 조회"""
        # 일괄 저장
        resp = await client.put(
            "/api/v1/ns/HRM/kv/batch",
            headers={"X-API-Key": HRM_KEY},
            json={"items": {"status": "ok", "version": "1.0", "mode": "production"}, "ttl": 300},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["stored"] == 3

        # 일괄 조회
        resp = await client.post(
            "/api/v1/ns/HRM/kv/batch",
            headers={"X-API-Key": HRM_KEY},
            json={"keys": ["status", "version", "nonexistent"]},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["values"]["status"] == "ok"
        assert data["values"]["version"] == "1.0"
        assert "nonexistent" not in data["values"]
        assert data["count"] == 2

    async def test_batch_get_empty(self, client: AsyncClient):
        """존재하지 않는 키만 일괄 조회 → count=0"""
        resp = await client.post(
            "/api/v1/ns/HRM/kv/batch",
            headers={"X-API-Key": HRM_KEY},
            json={"keys": ["no-key-1", "no-key-2"]},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["count"] == 0

    async def test_batch_set_value_too_large(self, client: AsyncClient):
        """일괄 저장 시 값 크기 초과 → 400"""
        large_value = "x" * (1048576 + 1)
        resp = await client.put(
            "/api/v1/ns/HRM/kv/batch",
            headers={"X-API-Key": HRM_KEY},
            json={"items": {"big": large_value}},
        )
        assert resp.status_code == 400


class TestKvTouch:
    """TTL 갱신 (Touch) 테스트"""

    async def test_touch_update_ttl(self, client: AsyncClient):
        """TTL 갱신 — 기존 키의 TTL을 변경"""
        await client.put(
            "/api/v1/ns/HRM/kv/touch-test",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "data", "ttl": 60},
        )
        resp = await client.put(
            "/api/v1/ns/HRM/kv/touch-test/touch",
            headers={"X-API-Key": HRM_KEY},
            json={"ttl": 3600},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["touched"] is True
        assert resp.json()["meta"]["ttl"] > 60

    async def test_touch_persist(self, client: AsyncClient):
        """TTL 제거 (PERSIST) — ttl=0으로 영구 보관"""
        await client.put(
            "/api/v1/ns/HRM/kv/persist-test",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "permanent", "ttl": 60},
        )
        resp = await client.put(
            "/api/v1/ns/HRM/kv/persist-test/touch",
            headers={"X-API-Key": HRM_KEY},
            json={"ttl": 0},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["touched"] is True
        assert resp.json()["meta"]["ttl"] == -1  # no expiry

    async def test_touch_not_found(self, client: AsyncClient):
        """존재하지 않는 키 touch → 404"""
        resp = await client.put(
            "/api/v1/ns/HRM/kv/no-such-key/touch",
            headers={"X-API-Key": HRM_KEY},
            json={"ttl": 3600},
        )
        assert resp.status_code == 404


class TestKeyValidationNewline:
    """ns/key의 trailing·embedded 개행 거부 — Rust·Python fallback 경로 동작 일치 회귀 가드.

    Python fallback 정규식이 과거 `^...$` + `.match()`라 끝 개행(`\\n`)을 통과시켜
    Rust(바이트 집합) 경로와 갈렸다(05-Rust_Python_동기화 위반). `.fullmatch()`로 정렬.
    활성 경로(Rust 또는 fallback) 무엇이든 거부해야 한다.
    """

    def test_validate_ns_rejects_newline(self):
        from app.utils.key_builder import validate_ns
        assert validate_ns("HRM") == "HRM"
        for bad in ("HRM\n", "H\nRM"):
            with pytest.raises(Exception):
                validate_ns(bad)

    def test_validate_key_rejects_newline(self):
        from app.utils.key_builder import validate_key
        assert validate_key("a/b:c.d_1") == "a/b:c.d_1"
        for bad in ("key\n", "k\ney"):
            with pytest.raises(Exception):
                validate_key(bad)


def test_kv_batch_rejects_empty_value():
    """KV 배치 저장도 단건(min_length=1)과 동일하게 빈 값 거부(R7 일관성)."""
    import pytest as _p
    from pydantic import ValidationError
    from app.schemas.kv import KvBatchSetRequest
    KvBatchSetRequest(items={"k": "v"})           # OK
    with _p.raises(ValidationError):
        KvBatchSetRequest(items={"k": ""})        # 빈 값 거부
