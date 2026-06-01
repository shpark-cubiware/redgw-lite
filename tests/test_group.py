"""
=============================================================================
Set (Group) API 테스트 (test_group.py)
=============================================================================

Redis Set 타입을 사용하는 Group API 전체 기능을 테스트합니다.

Redis 키 형식: {ns}:grp:{key}
엔드포인트:
  POST   /ns/{ns}/group/{key}                   — 멤버 추가 (SADD)
  GET    /ns/{ns}/group/{key}                   — 전체 멤버 조회 (SMEMBERS)
  GET    /ns/{ns}/group/{key}/contains/{member}  — 멤버 존재 확인 (SISMEMBER)
  DELETE /ns/{ns}/group/{key}/{member}           — 멤버 제거 (SREM)
  GET    /ns/{ns}/group/{key}/count              — 멤버 수 (SCARD)
  POST   /ns/{ns}/group/ops/inter               — 교집합 (SINTER)
  POST   /ns/{ns}/group/ops/union               — 합집합 (SUNION)
  POST   /ns/{ns}/group/ops/diff                — 차집합 (SDIFF)

테스트 시나리오:
  - 기본 CRUD (멤버 추가, 조회, 삭제)
  - 멤버 존재 확인 (SISMEMBER)
  - 멤버 수 조회 (SCARD)
  - 중복 추가 멱등성 (Set 특성)
  - 집합 연산 (교집합, 합집합, 차집합)
  - 교차 네임스페이스 읽기
  - 실전 시나리오: 태그 관리, 중복 처리 추적, 공통 고객 분석
=============================================================================
"""

from httpx import AsyncClient

from tests.conftest import HRM_KEY, ERP_KEY


class TestGroupCrud:
    """Set 타입 기본 CRUD 테스트"""

    async def test_add_and_members(self, client: AsyncClient):
        """
        멤버 추가 후 전체 조회.

        Redis SADD는 Set에 멤버를 추가합니다.
        Set은 중복을 허용하지 않으므로 같은 값을 여러 번 추가해도
        하나만 저장됩니다. 순서는 보장되지 않습니다.
        """
        # 멤버 추가 (SADD)
        resp = await client.post(
            "/api/v1/ns/ERP/group/tags:order-2024-001",
            headers={"X-API-Key": ERP_KEY},
            json={"members": ["긴급", "대량주문", "서울"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["data"]["added"] == 3  # 새로 추가된 멤버 수
        assert data["meta"]["type"] == "set"

        # 전체 멤버 조회 (SMEMBERS)
        resp = await client.get(
            "/api/v1/ns/ERP/group/tags:order-2024-001",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 200
        assert set(resp.json()["data"]["members"]) == {"긴급", "대량주문", "서울"}

    async def test_contains(self, client: AsyncClient):
        """
        멤버 존재 확인 (SISMEMBER).

        O(1) 시간 복잡도로 Set에 특정 멤버가 있는지 확인합니다.
        대량의 인력 처리에서 이미 처리된 건인지 빠르게 확인하는 용도.
        """
        await client.post(
            "/api/v1/ns/HRM/group/processed:batch-001",
            headers={"X-API-Key": HRM_KEY},
            json={"members": ["EMP-2024-001", "EMP-2024-002"]},
        )

        # 존재하는 멤버
        resp = await client.get(
            "/api/v1/ns/HRM/group/processed:batch-001/contains/EMP-2024-001",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.json()["data"]["exists"] is True

        # 존재하지 않는 멤버
        resp = await client.get(
            "/api/v1/ns/HRM/group/processed:batch-001/contains/EMP-9999-999",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.json()["data"]["exists"] is False

    async def test_remove_member(self, client: AsyncClient):
        """멤버 제거 (SREM) 후 카운트 확인"""
        await client.post(
            "/api/v1/ns/ERP/group/online:users",
            headers={"X-API-Key": ERP_KEY},
            json={"members": ["kim", "lee", "park"]},
        )
        # kim 제거
        resp = await client.delete(
            "/api/v1/ns/ERP/group/online:users/kim",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 200

        # 멤버 수 확인 (SCARD)
        resp = await client.get(
            "/api/v1/ns/ERP/group/online:users/count",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.json()["data"]["count"] == 2

    async def test_count(self, client: AsyncClient):
        """멤버 수 조회 (SCARD) — O(1) 시간 복잡도"""
        await client.post(
            "/api/v1/ns/ERP/group/tags:test",
            headers={"X-API-Key": ERP_KEY},
            json={"members": ["a", "b", "c", "d", "e"]},
        )
        resp = await client.get(
            "/api/v1/ns/ERP/group/tags:test/count",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["count"] == 5

    async def test_duplicate_add_idempotent(self, client: AsyncClient):
        """
        중복 추가 멱등성 — Set은 같은 멤버를 다시 추가해도 무시합니다.

        이 특성은 '이미 처리된 건인지 체크 후 추가' 없이
        단순히 SADD만 하면 되므로 race condition 방지에 유용합니다.
        """
        await client.post(
            "/api/v1/ns/ERP/group/tags:order-dup",
            headers={"X-API-Key": ERP_KEY},
            json={"members": ["대량주문", "긴급"]},
        )
        # 동일 멤버 재추가 시도
        resp = await client.post(
            "/api/v1/ns/ERP/group/tags:order-dup",
            headers={"X-API-Key": ERP_KEY},
            json={"members": ["대량주문", "정기주문"]},  # "대량주문"은 이미 존재
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["added"] == 1  # "정기주문"만 새로 추가됨

        # 전체 확인: 3개
        resp = await client.get(
            "/api/v1/ns/ERP/group/tags:order-dup/count",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.json()["data"]["count"] == 3

    async def test_get_empty_group_returns_empty(self, client: AsyncClient):
        """
        존재하지 않는 그룹 조회 → 빈 멤버 목록.

        Redis SMEMBERS는 키가 없어도 빈 Set을 반환합니다 (에러 아님).
        이는 Redis의 일관된 동작: 대부분의 읽기 명령은 키 없음 = 빈 결과.
        """
        resp = await client.get(
            "/api/v1/ns/ERP/group/nonexistent",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["members"] == []


class TestGroupSetOps:
    """집합 연산 테스트 — SINTER, SUNION, SDIFF"""

    async def test_intersection(self, client: AsyncClient):
        """
        교집합 (SINTER) — 두 시스템에서 공통으로 식별된 고객.

        활용 예: HRM 인력 배정 결과와 ERP 처리 대상자 목록의
        교집합을 구해 실제 처리 대상을 확정합니다.
        """
        await client.post(
            "/api/v1/ns/shared/group/customers:HRM",
            headers={"X-API-Key": HRM_KEY},
            json={"members": ["kim-001", "lee-002", "park-003"]},
        )
        await client.post(
            "/api/v1/ns/shared/group/customers:ERP",
            headers={"X-API-Key": ERP_KEY},
            json={"members": ["kim-001", "choi-004", "park-003"]},
        )

        resp = await client.post(
            "/api/v1/ns/shared/group/ops/inter",
            headers={"X-API-Key": ERP_KEY},
            json={"keys": ["customers:HRM", "customers:ERP"]},
        )
        assert resp.status_code == 200
        members = set(resp.json()["data"]["members"])
        assert members == {"kim-001", "park-003"}

    async def test_union(self, client: AsyncClient):
        """
        합집합 (SUNION) — 두 시스템의 모든 고객을 통합.

        활용 예: 여러 지점에서 각각 등록한 고객을 하나로 합쳐
        전체 대상 목록을 생성합니다.
        """
        await client.post(
            "/api/v1/ns/shared/group/targets:seoul",
            headers={"X-API-Key": ERP_KEY},
            json={"members": ["kim-001", "lee-002"]},
        )
        await client.post(
            "/api/v1/ns/shared/group/targets:busan",
            headers={"X-API-Key": ERP_KEY},
            json={"members": ["park-003", "kim-001"]},  # kim-001 중복
        )

        resp = await client.post(
            "/api/v1/ns/shared/group/ops/union",
            headers={"X-API-Key": ERP_KEY},
            json={"keys": ["targets:seoul", "targets:busan"]},
        )
        assert resp.status_code == 200
        members = set(resp.json()["data"]["members"])
        # 합집합이므로 중복 제거 후 3명
        assert members == {"kim-001", "lee-002", "park-003"}

    async def test_diff(self, client: AsyncClient):
        """
        차집합 (SDIFF) — A에는 있지만 B에는 없는 멤버.

        활용 예: 전체 처리 대상에서 이미 처리 완료된 건을 빼서
        미처리 건 목록을 추출합니다.
        """
        await client.post(
            "/api/v1/ns/shared/group/all-requests",
            headers={"X-API-Key": HRM_KEY},
            json={"members": ["EMP-001", "EMP-002", "EMP-003", "EMP-004"]},
        )
        await client.post(
            "/api/v1/ns/shared/group/processed-requests",
            headers={"X-API-Key": HRM_KEY},
            json={"members": ["EMP-001", "EMP-003"]},
        )

        resp = await client.post(
            "/api/v1/ns/shared/group/ops/diff",
            headers={"X-API-Key": HRM_KEY},
            json={"keys": ["all-requests", "processed-requests"]},
        )
        assert resp.status_code == 200
        members = set(resp.json()["data"]["members"])
        # 미처리: EMP-002, EMP-004
        assert members == {"EMP-002", "EMP-004"}


class TestGroupScenarios:
    """실전 시나리오 테스트"""

    async def test_tag_management(self, client: AsyncClient):
        """
        주문 태그 관리 — 여러 태그를 Set으로 관리.

        Set은 태그 저장에 이상적: 중복 방지, O(1) 존재 확인,
        교집합으로 특정 태그 조합에 해당하는 주문 필터링 가능.
        """
        # 주문에 태그 추가
        await client.post(
            "/api/v1/ns/ERP/group/tags:order-2024-100",
            headers={"X-API-Key": ERP_KEY},
            json={"members": ["대량주문", "야간", "강남"]},
        )
        # 추가 태그 부여
        await client.post(
            "/api/v1/ns/ERP/group/tags:order-2024-100",
            headers={"X-API-Key": ERP_KEY},
            json={"members": ["긴급"]},
        )
        # 태그 확인
        resp = await client.get(
            "/api/v1/ns/ERP/group/tags:order-2024-100/count",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.json()["data"]["count"] == 4

        # 태그 제거
        await client.delete(
            "/api/v1/ns/ERP/group/tags:order-2024-100/야간",
            headers={"X-API-Key": ERP_KEY},
        )
        resp = await client.get(
            "/api/v1/ns/ERP/group/tags:order-2024-100/count",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.json()["data"]["count"] == 3

    async def test_duplicate_processing_tracker(self, client: AsyncClient):
        """
        중복 처리 추적 — HRM 배치에서 이미 처리된 인력 추적.

        Set + SISMEMBER로 O(1) 중복 검사: 수만 건 인력 처리에서도
        이미 처리된 건인지 즉시 확인 가능.
        """
        processed = ["EMP-001", "EMP-002", "EMP-003"]
        await client.post(
            "/api/v1/ns/HRM/group/processed:daily-2024-01-15",
            headers={"X-API-Key": HRM_KEY},
            json={"members": processed},
        )

        # 새 요청이 이미 처리되었는지 확인
        resp = await client.get(
            "/api/v1/ns/HRM/group/processed:daily-2024-01-15/contains/EMP-002",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.json()["data"]["exists"] is True  # 이미 처리됨 → skip

        resp = await client.get(
            "/api/v1/ns/HRM/group/processed:daily-2024-01-15/contains/EMP-999",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.json()["data"]["exists"] is False  # 미처리 → 진행

    async def test_cross_namespace_read(self, client: AsyncClient):
        """ERP가 HRM 그룹 데이터를 읽기 (교차 네임스페이스)"""
        await client.post(
            "/api/v1/ns/HRM/group/assigned:batch-001",
            headers={"X-API-Key": HRM_KEY},
            json={"members": ["EMP-001", "EMP-002"]},
        )
        # ERP → HRM read (허용)
        resp = await client.get(
            "/api/v1/ns/HRM/group/assigned:batch-001",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 200
        assert len(resp.json()["data"]["members"]) == 2


class TestGroupTtl:
    """Group TTL 테스트"""

    async def test_add_with_ttl(self, client: AsyncClient):
        """TTL 지정 멤버 추가 — 응답에 ttl 포함"""
        resp = await client.post(
            "/api/v1/ns/ERP/group/ttl-test",
            headers={"X-API-Key": ERP_KEY},
            json={"members": ["a", "b"], "ttl": 300},
        )
        assert resp.status_code == 200
        assert resp.json()["meta"]["ttl"] > 0


class TestGroupBatch:
    """Group 배치 조회/추가 테스트"""

    async def test_batch_add_and_get(self, client: AsyncClient):
        """
        여러 Set에 한 번에 멤버 추가 후 일괄 조회.

        Pipeline SADD로 N개 Set에 멤버를 추가하고,
        Pipeline SMEMBERS로 일괄 조회합니다.
        """
        # 배치 추가
        resp = await client.put(
            "/api/v1/ns/ERP/group/batch",
            headers={"X-API-Key": ERP_KEY},
            json={
                "items": {
                    "team:alpha": ["kim", "lee"],
                    "team:bravo": ["park", "choi", "jung"],
                },
                "ttl": 300,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["stored"] == 2

        # 배치 조회
        resp = await client.post(
            "/api/v1/ns/ERP/group/batch",
            headers={"X-API-Key": ERP_KEY},
            json={"keys": ["team:alpha", "team:bravo", "team:noexist"]},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["count"] == 2
        assert set(data["values"]["team:alpha"]) == {"kim", "lee"}
        assert len(data["values"]["team:bravo"]) == 3
        assert "team:noexist" not in data["values"]

    async def test_batch_get_empty(self, client: AsyncClient):
        """존재하지 않는 Set들 배치 조회 → 빈 결과"""
        resp = await client.post(
            "/api/v1/ns/ERP/group/batch",
            headers={"X-API-Key": ERP_KEY},
            json={"keys": ["no1", "no2"]},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["count"] == 0

    async def test_batch_add_empty_member_list(self, client: AsyncClient):
        """빈 멤버 리스트 → 422 (SADD 0인자 500 방지)."""
        resp = await client.put(
            "/api/v1/ns/ERP/group/batch",
            headers={"X-API-Key": ERP_KEY},
            json={"items": {"team:empty": []}},
        )
        assert resp.status_code == 422


class TestGroupTouch:
    """TTL 갱신 (Touch) 테스트"""

    async def test_touch_update_ttl(self, client: AsyncClient):
        """Set TTL 갱신"""
        await client.post(
            "/api/v1/ns/ERP/group/touch-set",
            headers={"X-API-Key": ERP_KEY},
            json={"members": ["a", "b"]},
        )
        resp = await client.put(
            "/api/v1/ns/ERP/group/touch-set/touch",
            headers={"X-API-Key": ERP_KEY},
            json={"ttl": 7200},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["touched"] is True
        assert resp.json()["meta"]["ttl"] > 0

    async def test_touch_persist(self, client: AsyncClient):
        """TTL 제거 (PERSIST)"""
        await client.post(
            "/api/v1/ns/ERP/group/persist-set",
            headers={"X-API-Key": ERP_KEY},
            json={"members": ["x"]},
        )
        resp = await client.put(
            "/api/v1/ns/ERP/group/persist-set/touch",
            headers={"X-API-Key": ERP_KEY},
            json={"ttl": 0},
        )
        assert resp.status_code == 200
        assert resp.json()["meta"]["ttl"] == -1

    async def test_touch_not_found(self, client: AsyncClient):
        """존재하지 않는 키 touch → 404"""
        resp = await client.put(
            "/api/v1/ns/ERP/group/nonexistent/touch",
            headers={"X-API-Key": ERP_KEY},
            json={"ttl": 3600},
        )
        assert resp.status_code == 404


class TestGroupValueSizeValidation:
    """Set 멤버 크기 검증 — kv/map/queue/rank와 동일한 1MB 가드"""

    async def test_add_group_member_too_large(self, client: AsyncClient):
        large = "x" * (1048576 + 1)
        resp = await client.post(
            "/api/v1/ns/ERP/group/oversized-set",
            headers={"X-API-Key": ERP_KEY},
            json={"members": [large]},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "VALUE_TOO_LARGE"

    async def test_batch_add_group_member_too_large(self, client: AsyncClient):
        large = "x" * (1048576 + 1)
        resp = await client.put(
            "/api/v1/ns/ERP/group/batch",
            headers={"X-API-Key": ERP_KEY},
            json={"items": {"set-a": ["ok"], "set-b": [large]}},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "VALUE_TOO_LARGE"

    async def test_add_group_member_at_limit_ok(self, client: AsyncClient):
        member = "x" * 1048576
        resp = await client.post(
            "/api/v1/ns/ERP/group/at-limit-set",
            headers={"X-API-Key": ERP_KEY},
            json={"members": [member]},
        )
        assert resp.status_code == 200


def test_group_ops_keys_max_length():
    """GroupOpsRequest.keys 상한 100 — 다른 배치 스키마와 동일(초과 시 ValidationError)."""
    import pytest as _pytest
    from pydantic import ValidationError
    from app.schemas.group import GroupOpsRequest

    GroupOpsRequest(keys=["a", "b"])                       # 최소 2
    GroupOpsRequest(keys=[str(i) for i in range(100)])     # 정확히 100 OK
    with _pytest.raises(ValidationError):
        GroupOpsRequest(keys=[str(i) for i in range(101)])  # 101 > 100
    with _pytest.raises(ValidationError):
        GroupOpsRequest(keys=["only-one"])                  # min_length=2


class TestGroupAddTtlZero:
    """group add ttl=0도 기존 TTL을 제거한다(kv/map과 동일 계약, R4 회귀)."""

    async def test_group_add_ttl_zero_clears_existing_ttl(self, client: AsyncClient):
        await client.post(
            "/api/v1/ns/HRM/group/gtz", headers={"X-API-Key": HRM_KEY},
            json={"members": ["a"], "ttl": 120},
        )
        resp = await client.post(
            "/api/v1/ns/HRM/group/gtz", headers={"X-API-Key": HRM_KEY},
            json={"members": ["b"], "ttl": 0},
        )
        assert resp.status_code == 200
        assert resp.json()["meta"]["ttl"] == -1
