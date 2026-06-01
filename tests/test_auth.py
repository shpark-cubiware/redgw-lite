"""
=============================================================================
인증/권한 테스트 (test_auth.py)
=============================================================================

RedGW의 인증(Authentication) 및 권한(Authorization) 체계를 검증합니다.

인증 흐름:
  HTTP 요청 → X-API-Key 헤더 → verify_api_key() → ClientInfo
  - 헤더 누락: 422 Unprocessable Entity
  - 잘못된 키: 401 Unauthorized

권한 매트릭스 (config.yaml 기준):
  ┌────────────┬──────┬──────┬──────┬──────┬────────┐
  │ 클라이언트   │ HRM │ ERP │ CRM │shared│   *    │
  ├────────────┼──────┼──────┼──────┼──────┼────────┤
  │ HRM       │ rw   │  r   │  -   │  rw  │   -    │
  │ ERP       │  r   │ rw   │  r   │  rw  │   -    │
  │ CRM       │  -   │  r   │ rw   │  rw  │   -    │
  │ MONITOR │  -   │  -   │  -   │  -   │   r    │
  │ admin      │ rw   │ rw   │ rw   │  rw  │  rw    │
  └────────────┴──────┴──────┴──────┴──────┴────────┘
=============================================================================
"""

from httpx import AsyncClient

from tests.conftest import (
    ADMIN_KEY, HRM_KEY, INVALID_KEY, ERP_KEY,
    MONITOR_KEY, CRM_KEY,
)


class TestApiKeyAuth:
    """API Key 인증 테스트 — verify_api_key() 검증"""

    async def test_missing_api_key(self, client: AsyncClient):
        """X-API-Key 헤더 누락 → 422"""
        resp = await client.get("/api/v1/ns/HRM/kv/status")
        assert resp.status_code == 422

    async def test_invalid_api_key(self, client: AsyncClient):
        """config.yaml에 없는 API 키 → 401 Unauthorized"""
        resp = await client.get(
            "/api/v1/ns/HRM/kv/status",
            headers={"X-API-Key": INVALID_KEY},
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["detail"]["ok"] is False
        assert body["detail"]["error"]["code"] == "UNAUTHORIZED"

    async def test_valid_api_key(self, client: AsyncClient):
        """유효한 API 키 → 인증 통과 (키 없으면 404)"""
        resp = await client.get(
            "/api/v1/ns/HRM/kv/nonexistent",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 404


class TestNamespaceGuard:
    """네임스페이스 접근 권한 테스트"""

    # ─── 자기 네임스페이스 read/write ──────────────────────

    async def test_own_namespace_write(self, client: AsyncClient):
        """자기 네임스페이스 write → 허용"""
        resp = await client.put(
            "/api/v1/ns/HRM/kv/status",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "running", "ttl": 60},
        )
        assert resp.status_code == 200

    async def test_own_namespace_read(self, client: AsyncClient):
        """자기 네임스페이스 read → 허용"""
        await client.put(
            "/api/v1/ns/HRM/kv/status",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "running"},
        )
        resp = await client.get(
            "/api/v1/ns/HRM/kv/status",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 200

    # ─── 교차 네임스페이스 read (허용) ─────────────────────

    async def test_hrm_read_erp_allowed(self, client: AsyncClient):
        """HRM → ERP read 허용"""
        await client.put(
            "/api/v1/ns/ERP/kv/test-key",
            headers={"X-API-Key": ERP_KEY},
            json={"value": "test"},
        )
        resp = await client.get(
            "/api/v1/ns/ERP/kv/test-key",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 200

    async def test_erp_read_hrm_allowed(self, client: AsyncClient):
        """ERP → HRM read 허용"""
        await client.put(
            "/api/v1/ns/HRM/kv/status",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "online"},
        )
        resp = await client.get(
            "/api/v1/ns/HRM/kv/status",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["value"] == "online"

    async def test_crm_read_erp_allowed(self, client: AsyncClient):
        """CRM → ERP read 허용"""
        await client.put(
            "/api/v1/ns/ERP/kv/status",
            headers={"X-API-Key": ERP_KEY},
            json={"value": "active"},
        )
        resp = await client.get(
            "/api/v1/ns/ERP/kv/status",
            headers={"X-API-Key": CRM_KEY},
        )
        assert resp.status_code == 200

    # ─── 교차 네임스페이스 write (거부) ────────────────────

    async def test_hrm_write_erp_denied(self, client: AsyncClient):
        """HRM → ERP write 거부 (HRM는 ERP에 read만)"""
        resp = await client.put(
            "/api/v1/ns/ERP/kv/hacked",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "hacked"},
        )
        assert resp.status_code == 403

    async def test_crm_write_erp_denied(self, client: AsyncClient):
        """CRM → ERP write 거부"""
        resp = await client.put(
            "/api/v1/ns/ERP/kv/test",
            headers={"X-API-Key": CRM_KEY},
            json={"value": "test"},
        )
        assert resp.status_code == 403

    # ─── 권한 없는 네임스페이스 접근 (거부) ─────────────────

    async def test_crm_read_hrm_denied(self, client: AsyncClient):
        """CRM → HRM read 거부 (CRM는 HRM에 아무 권한 없음)"""
        resp = await client.get(
            "/api/v1/ns/HRM/kv/status",
            headers={"X-API-Key": CRM_KEY},
        )
        assert resp.status_code == 403

    # ─── shared 네임스페이스 ───────────────────────────────

    async def test_shared_write_and_cross_read(self, client: AsyncClient):
        """shared 네임스페이스 — 모든 클라이언트 read/write"""
        await client.put(
            "/api/v1/ns/shared/kv/config:max-retry",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "3"},
        )
        resp = await client.get(
            "/api/v1/ns/shared/kv/config:max-retry",
            headers={"X-API-Key": CRM_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["value"] == "3"

    # ─── MONITOR 와일드카드 "*" ─────────────────────────

    async def test_monitoring_wildcard_read_all(self, client: AsyncClient):
        """MONITOR → 모든 네임스페이스 read 허용 (와일드카드)"""
        # 각 시스템에 데이터 생성
        for ns, api_key in [("HRM", HRM_KEY), ("ERP", ERP_KEY), ("CRM", CRM_KEY)]:
            await client.put(
                f"/api/v1/ns/{ns}/kv/status",
                headers={"X-API-Key": api_key},
                json={"value": "online"},
            )
        # MONITOR이 모든 네임스페이스 읽기
        for ns in ["HRM", "ERP", "CRM"]:
            resp = await client.get(
                f"/api/v1/ns/{ns}/kv/status",
                headers={"X-API-Key": MONITOR_KEY},
            )
            assert resp.status_code == 200, f"MONITOR → {ns} read 실패"

    async def test_monitoring_write_denied(self, client: AsyncClient):
        """MONITOR → write 거부 (와일드카드는 read만)"""
        resp = await client.put(
            "/api/v1/ns/ERP/kv/test",
            headers={"X-API-Key": MONITOR_KEY},
            json={"value": "test"},
        )
        assert resp.status_code == 403

    # ─── admin 전체 접근 ──────────────────────────────────

    async def test_admin_read_any(self, client: AsyncClient):
        """admin → 모든 네임스페이스 read"""
        await client.put(
            "/api/v1/ns/HRM/kv/status",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "running"},
        )
        resp = await client.get(
            "/api/v1/ns/HRM/kv/status",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert resp.status_code == 200

    async def test_admin_write_any(self, client: AsyncClient):
        """admin → 모든 네임스페이스 write"""
        resp = await client.put(
            "/api/v1/ns/CRM/kv/admin-test",
            headers={"X-API-Key": ADMIN_KEY},
            json={"value": "admin-data"},
        )
        assert resp.status_code == 200
