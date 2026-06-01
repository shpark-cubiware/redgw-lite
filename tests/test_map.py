"""
=============================================================================
Hash (Map) API 테스트 (test_map.py)
=============================================================================

Redis Hash 타입을 사용하는 Map API 전체 기능을 테스트합니다.

Redis 키 형식: {ns}:map:{key}
엔드포인트:
  PUT    /ns/{ns}/map/{key}          — 다수 필드 저장 (HMSET)
  GET    /ns/{ns}/map/{key}          — 전체 필드 조회 (HGETALL)
  GET    /ns/{ns}/map/{key}/{field}  — 특정 필드 조회 (HGET)
  PUT    /ns/{ns}/map/{key}/{field}  — 단일 필드 저장 (HSET)
  DELETE /ns/{ns}/map/{key}          — 전체 삭제
  DELETE /ns/{ns}/map/{key}/{field}  — 특정 필드 삭제 (HDEL)

테스트 시나리오:
  - 기본 CRUD (다수 필드 저장/조회/삭제)
  - 단일 필드 조회/업데이트/삭제
  - 주문 정보, 고객 정보, 거래처 정보
  - 장비 상태 캐시
  - 교차 네임스페이스 읽기
  - 배정 결과 연계 조회
=============================================================================
"""

from httpx import AsyncClient

from tests.conftest import HRM_KEY, ERP_KEY, CRM_KEY


class TestMapCrud:
    """Hash 타입 기본 CRUD 테스트"""

    async def test_set_and_get_all(self, client: AsyncClient):
        """전체 필드 저장 후 조회"""
        resp = await client.put(
            "/api/v1/ns/ERP/map/order:2024-001",
            headers={"X-API-Key": ERP_KEY},
            json={
                "fields": {
                    "type": "일반주문",
                    "status": "처리중",
                    "region": "서울강남",
                    "manager": "kim",
                },
                "ttl": 86400,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["meta"]["type"] == "hash"

        # 전체 조회
        resp = await client.get(
            "/api/v1/ns/ERP/map/order:2024-001",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 200
        fields = resp.json()["data"]["fields"]
        assert fields["type"] == "일반주문"
        assert fields["status"] == "처리중"
        assert fields["region"] == "서울강남"
        assert fields["manager"] == "kim"

    async def test_get_single_field(self, client: AsyncClient):
        """특정 필드 조회 (HGET)"""
        await client.put(
            "/api/v1/ns/ERP/map/customer:kim-001",
            headers={"X-API-Key": ERP_KEY},
            json={"fields": {"name": "김철수", "staff_id": "EMP-2024-001", "status": "고객"}},
        )
        resp = await client.get(
            "/api/v1/ns/ERP/map/customer:kim-001/staff_id",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["value"] == "EMP-2024-001"

    async def test_get_field_not_found(self, client: AsyncClient):
        """존재하지 않는 필드 조회 → 404"""
        await client.put(
            "/api/v1/ns/ERP/map/order:2024-001",
            headers={"X-API-Key": ERP_KEY},
            json={"fields": {"type": "일반주문"}},
        )
        resp = await client.get(
            "/api/v1/ns/ERP/map/order:2024-001/nonexistent",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 404

    async def test_update_single_field(self, client: AsyncClient):
        """필드 단위 업데이트 (HSET)"""
        await client.put(
            "/api/v1/ns/ERP/map/order:2024-001",
            headers={"X-API-Key": ERP_KEY},
            json={"fields": {"status": "처리중", "type": "일반주문"}},
        )
        # status 필드만 변경
        resp = await client.put(
            "/api/v1/ns/ERP/map/order:2024-001/status",
            headers={"X-API-Key": ERP_KEY},
            json={"value": "완료"},
        )
        assert resp.status_code == 200

        # 변경 확인
        resp = await client.get(
            "/api/v1/ns/ERP/map/order:2024-001/status",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.json()["data"]["value"] == "완료"

    async def test_delete_field(self, client: AsyncClient):
        """특정 필드 삭제 (HDEL)"""
        await client.put(
            "/api/v1/ns/ERP/map/order:2024-002",
            headers={"X-API-Key": ERP_KEY},
            json={"fields": {"status": "처리중", "manager": "park"}},
        )
        resp = await client.delete(
            "/api/v1/ns/ERP/map/order:2024-002/manager",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 200

    async def test_delete_entire_map(self, client: AsyncClient):
        """전체 삭제 후 404"""
        await client.put(
            "/api/v1/ns/ERP/map/temp-order",
            headers={"X-API-Key": ERP_KEY},
            json={"fields": {"a": "1"}},
        )
        resp = await client.delete(
            "/api/v1/ns/ERP/map/temp-order",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 200

        # 삭제 확인
        resp = await client.get(
            "/api/v1/ns/ERP/map/temp-order",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 404

    async def test_get_map_not_found(self, client: AsyncClient):
        """존재하지 않는 Hash 키 조회 → 404"""
        resp = await client.get(
            "/api/v1/ns/ERP/map/nonexistent",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 404


class TestMapScenarios:
    """실전 시나리오 테스트"""

    async def test_contact_info(self, client: AsyncClient):
        """거래처 정보 저장 및 조회"""
        await client.put(
            "/api/v1/ns/ERP/map/contact:v-2024-001",
            headers={"X-API-Key": ERP_KEY},
            json={
                "fields": {
                    "name": "홍길동",
                    "order_id": "2024-001",
                    "type": "일반주문",
                    "support_status": "대기",
                }
            },
        )
        # 상태 업데이트
        await client.put(
            "/api/v1/ns/ERP/map/contact:v-2024-001/support_status",
            headers={"X-API-Key": ERP_KEY},
            json={"value": "상담진행중"},
        )
        resp = await client.get(
            "/api/v1/ns/ERP/map/contact:v-2024-001/support_status",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.json()["data"]["value"] == "상담진행중"

    async def test_equipment_status_cache(self, client: AsyncClient):
        """장비 상태 캐시 — HRM 스캐너 상태"""
        await client.put(
            "/api/v1/ns/HRM/map/device:scanner-01",
            headers={"X-API-Key": HRM_KEY},
            json={
                "fields": {
                    "status": "online",
                    "firmware": "v2.3.1",
                    "queue_depth": "12",
                },
                "ttl": 300,
            },
        )
        resp = await client.get(
            "/api/v1/ns/HRM/map/device:scanner-01/status",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.json()["data"]["value"] == "online"

    async def test_cross_namespace_order_read(self, client: AsyncClient):
        """CRM → ERP 주문 정보 읽기 (교차 네임스페이스)"""
        await client.put(
            "/api/v1/ns/ERP/map/order:2024-001",
            headers={"X-API-Key": ERP_KEY},
            json={"fields": {"type": "일반주문", "status": "처리중"}},
        )
        resp = await client.get(
            "/api/v1/ns/ERP/map/order:2024-001",
            headers={"X-API-Key": CRM_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["fields"]["type"] == "일반주문"

    async def test_hrm_assign_result(self, client: AsyncClient):
        """HRM 배정 결과 저장 → ERP 조회 (교차 읽기)"""
        await client.put(
            "/api/v1/ns/HRM/map/assign:req-001",
            headers={"X-API-Key": HRM_KEY},
            json={
                "fields": {
                    "score": "95",
                    "staff_id": "kim-001",
                    "emp_id": "EMP-2024-001",
                    "status": "assigned",
                }
            },
        )
        # ERP가 HRM 배정 결과 조회
        resp = await client.get(
            "/api/v1/ns/HRM/map/assign:req-001",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["fields"]["score"] == "95"

    async def test_shared_config_map(self, client: AsyncClient):
        """공유 설정 (Hash) — 여러 시스템이 참조"""
        await client.put(
            "/api/v1/ns/shared/map/config:global",
            headers={"X-API-Key": ERP_KEY},
            json={
                "fields": {
                    "max_retry": "3",
                    "timeout_sec": "30",
                    "maintenance": "false",
                }
            },
        )
        # HRM가 설정 조회
        resp = await client.get(
            "/api/v1/ns/shared/map/config:global/timeout_sec",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.json()["data"]["value"] == "30"


class TestMapValueSizeValidation:
    """Map 값 크기 검증 테스트"""

    async def test_set_map_field_too_large(self, client: AsyncClient):
        """단일 필드 값이 max_value_size 초과 → 400"""
        large_value = "x" * (1048576 + 1)
        resp = await client.put(
            "/api/v1/ns/ERP/map/big-field/data",
            headers={"X-API-Key": ERP_KEY},
            json={"value": large_value},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "VALUE_TOO_LARGE"

    async def test_set_map_fields_too_large(self, client: AsyncClient):
        """다수 필드 중 하나가 max_value_size 초과 → 400"""
        large_value = "x" * (1048576 + 1)
        resp = await client.put(
            "/api/v1/ns/ERP/map/big-map",
            headers={"X-API-Key": ERP_KEY},
            json={"fields": {"small": "ok", "big": large_value}},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "VALUE_TOO_LARGE"


class TestMapBatch:
    """Map 배치 조회/저장 테스트"""

    async def test_batch_set_and_get(self, client: AsyncClient):
        """
        여러 해시 일괄 저장 후 일괄 조회.

        Pipeline으로 HMSET N건을 실행하고, Pipeline HGETALL N건으로 조회합니다.
        """
        # 배치 저장
        resp = await client.put(
            "/api/v1/ns/ERP/map/batch",
            headers={"X-API-Key": ERP_KEY},
            json={
                "items": {
                    "order:b001": {"type": "일반주문", "status": "처리중"},
                    "order:b002": {"type": "긴급주문", "status": "완료"},
                },
                "ttl": 300,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["stored"] == 2

        # 배치 조회
        resp = await client.post(
            "/api/v1/ns/ERP/map/batch",
            headers={"X-API-Key": ERP_KEY},
            json={"keys": ["order:b001", "order:b002", "order:b999"]},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["count"] == 2
        assert data["values"]["order:b001"]["type"] == "일반주문"
        assert data["values"]["order:b002"]["status"] == "완료"
        assert "order:b999" not in data["values"]

    async def test_batch_get_empty(self, client: AsyncClient):
        """존재하지 않는 키들 배치 조회 → 빈 결과"""
        resp = await client.post(
            "/api/v1/ns/ERP/map/batch",
            headers={"X-API-Key": ERP_KEY},
            json={"keys": ["noexist-1", "noexist-2"]},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["count"] == 0

    async def test_batch_set_value_too_large(self, client: AsyncClient):
        """배치 저장 시 필드 값이 max_value_size 초과 → 400"""
        large_value = "x" * (1048576 + 1)
        resp = await client.put(
            "/api/v1/ns/ERP/map/batch",
            headers={"X-API-Key": ERP_KEY},
            json={"items": {"bad-hash": {"big": large_value}}},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "VALUE_TOO_LARGE"

    async def test_batch_set_empty_fields(self, client: AsyncClient):
        """빈 fields dict → 422 (HSET mapping={} 500 방지)."""
        resp = await client.put(
            "/api/v1/ns/ERP/map/batch",
            headers={"X-API-Key": ERP_KEY},
            json={"items": {"order:empty": {}}},
        )
        assert resp.status_code == 422


class TestMapTouch:
    """TTL 갱신 (Touch) 테스트"""

    async def test_touch_update_ttl(self, client: AsyncClient):
        """Hash TTL 갱신"""
        await client.put(
            "/api/v1/ns/ERP/map/touch-order",
            headers={"X-API-Key": ERP_KEY},
            json={"fields": {"status": "active"}, "ttl": 60},
        )
        resp = await client.put(
            "/api/v1/ns/ERP/map/touch-order/touch",
            headers={"X-API-Key": ERP_KEY},
            json={"ttl": 7200},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["touched"] is True
        assert resp.json()["meta"]["ttl"] > 60

    async def test_touch_persist(self, client: AsyncClient):
        """TTL 제거 (PERSIST)"""
        await client.put(
            "/api/v1/ns/ERP/map/persist-order",
            headers={"X-API-Key": ERP_KEY},
            json={"fields": {"type": "일반주문"}, "ttl": 60},
        )
        resp = await client.put(
            "/api/v1/ns/ERP/map/persist-order/touch",
            headers={"X-API-Key": ERP_KEY},
            json={"ttl": 0},
        )
        assert resp.status_code == 200
        assert resp.json()["meta"]["ttl"] == -1

    async def test_touch_not_found(self, client: AsyncClient):
        """존재하지 않는 키 touch → 404"""
        resp = await client.put(
            "/api/v1/ns/ERP/map/nonexistent/touch",
            headers={"X-API-Key": ERP_KEY},
            json={"ttl": 3600},
        )
        assert resp.status_code == 404


class TestMapWriteTtlZero:
    """map write-path ttl=0은 기존 TTL을 제거한다(kv write·touch와 동일 'ttl=0=무TTL' 계약)."""

    async def test_set_map_ttl_zero_clears_existing_ttl(self, client: AsyncClient):
        await client.put(
            "/api/v1/ns/HRM/map/wtz", headers={"X-API-Key": HRM_KEY},
            json={"fields": {"a": "1"}, "ttl": 120},
        )
        resp = await client.put(
            "/api/v1/ns/HRM/map/wtz", headers={"X-API-Key": HRM_KEY},
            json={"fields": {"a": "2"}, "ttl": 0},
        )
        assert resp.status_code == 200
        assert resp.json()["meta"]["ttl"] == -1

    async def test_set_map_field_ttl_zero_clears_existing_ttl(self, client: AsyncClient):
        await client.put(
            "/api/v1/ns/HRM/map/wtzf", headers={"X-API-Key": HRM_KEY},
            json={"fields": {"a": "1"}, "ttl": 120},
        )
        resp = await client.put(
            "/api/v1/ns/HRM/map/wtzf/b", headers={"X-API-Key": HRM_KEY},
            json={"value": "2", "ttl": 0},
        )
        assert resp.status_code == 200
        assert resp.json()["meta"]["ttl"] == -1


class TestMapBatchWriteTtlZero:
    """map batch write ttl=0도 기존 TTL을 제거한다(단건과 동일 계약, R3 회귀)."""

    async def test_batch_set_map_ttl_zero_clears_existing_ttl(self, client: AsyncClient):
        # ttl>0로 생성
        await client.put(
            "/api/v1/ns/HRM/map/btz", headers={"X-API-Key": HRM_KEY},
            json={"fields": {"a": "1"}, "ttl": 120},
        )
        # batch ttl=0으로 재저장 → TTL 제거되어야 함
        resp = await client.put(
            "/api/v1/ns/HRM/map/batch", headers={"X-API-Key": HRM_KEY},
            json={"items": {"btz": {"a": "2"}}, "ttl": 0},
        )
        assert resp.status_code == 200
        # 단건 조회로 TTL 제거 확인
        info = await client.get(
            "/api/v1/ns/HRM/map/btz", headers={"X-API-Key": HRM_KEY},
        )
        assert info.json()["meta"]["ttl"] == -1


def test_map_set_and_batch_reject_empty_field_value():
    """map 다중/배치 필드 저장도 단일 필드(min_length=1)와 동일하게 빈 값 거부(R7 일관성)."""
    import pytest as _p
    from pydantic import ValidationError
    from app.schemas.map import MapSetRequest, MapBatchSetRequest
    MapSetRequest(fields={"f": "v"})              # OK
    with _p.raises(ValidationError):
        MapSetRequest(fields={"f": ""})           # 빈 필드값 거부
    MapBatchSetRequest(items={"k": {"f": "v"}})   # OK
    with _p.raises(ValidationError):
        MapBatchSetRequest(items={"k": {"f": ""}})  # 빈 필드값 거부
