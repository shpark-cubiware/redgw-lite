"""
=============================================================================
Admin API 테스트 (test_admin.py)
=============================================================================

RedGW 관리 API의 전체 기능을 테스트합니다.

엔드포인트:
  GET    /admin/keys                          — 키 목록 조회 (필터링)
  GET    /admin/info/{ns}/{type}/{key:path}   — 키 상세 정보
  PUT    /admin/ttl/{ns}/{type}/{key:path}    — TTL 설정/변경
  DELETE /admin/keys/{ns}/{type}/{key:path}   — 키 삭제
  GET    /admin/stats                         — 네임스페이스별 통계

권한 요구사항:
  - TTL 설정, 키 삭제 → admin 권한 필요 (client_id=="admin" 만 허용)
  - 키 목록, 상세 정보, 통계 → 인증된 모든 사용자

테스트 시나리오:
  - 키 목록 조회 (전체, 네임스페이스별, 타입별, 패턴별)
  - 키 상세 정보 (타입별 크기 정보 포함)
  - TTL 설정 및 영구 전환 (PERSIST)
  - 키 삭제
  - 네임스페이스별 통계
  - 관리자가 아닌 사용자의 admin 작업 거부
=============================================================================
"""

import pytest
from httpx import AsyncClient

from tests.conftest import ADMIN_KEY, HRM_KEY, ERP_KEY, MONITOR_KEY, INVALID_KEY


class TestAdminKeys:
    """키 목록 조회 테스트"""

    async def test_list_all_keys(self, client: AsyncClient):
        """
        전체 키 목록 조회.

        기본적으로 모든 네임스페이스, 모든 타입의 키를 반환합니다.
        Cursor 기반 페이지네이션으로 cursor=0이 첫 페이지입니다.
        """
        # 테스트 데이터 생성
        await client.put(
            "/api/v1/ns/HRM/kv/status",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "running"},
        )
        await client.put(
            "/api/v1/ns/ERP/map/order:2024-001",
            headers={"X-API-Key": ERP_KEY},
            json={"fields": {"type": "일반주문"}},
        )

        resp = await client.get(
            "/api/v1/admin/keys",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["count"] >= 2
        assert isinstance(data["keys"], list)
        assert "cursor" in data
        assert "has_more" in data

    async def test_list_keys_by_namespace(self, client: AsyncClient):
        """네임스페이스 필터로 키 조회"""
        await client.put(
            "/api/v1/ns/HRM/kv/status",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "running"},
        )
        await client.put(
            "/api/v1/ns/ERP/kv/status",
            headers={"X-API-Key": ERP_KEY},
            json={"value": "active"},
        )

        resp = await client.get(
            "/api/v1/admin/keys?ns=HRM",
            headers={"X-API-Key": ADMIN_KEY},
        )
        data = resp.json()["data"]
        # HRM 키만 포함
        assert all("HRM" in k for k in data["keys"])

    async def test_list_keys_by_type(self, client: AsyncClient):
        """타입 필터로 키 조회"""
        await client.put(
            "/api/v1/ns/ERP/kv/test",
            headers={"X-API-Key": ERP_KEY},
            json={"value": "v"},
        )
        await client.put(
            "/api/v1/ns/ERP/map/test",
            headers={"X-API-Key": ERP_KEY},
            json={"fields": {"a": "1"}},
        )

        resp = await client.get(
            "/api/v1/admin/keys?ns=ERP&type=kv",
            headers={"X-API-Key": ADMIN_KEY},
        )
        data = resp.json()["data"]
        assert data["count"] >= 1
        # kv 타입만 포함
        assert all(":kv:" in k for k in data["keys"])

    async def test_cursor_pagination(self, client: AsyncClient):
        """
        Cursor 기반 페이지네이션 — count=1로 한 번에 1개씩 순회.

        Redis SCAN은 cursor=0이 시작, 반환 cursor=0이면 순회 완료.
        """
        # 테스트 데이터 생성 (3개 키)
        for i in range(3):
            await client.put(
                f"/api/v1/ns/ERP/kv/page-{i}",
                headers={"X-API-Key": ERP_KEY},
                json={"value": f"v{i}"},
            )

        # 첫 페이지
        resp = await client.get(
            "/api/v1/admin/keys?ns=ERP&type=kv&count=1",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "cursor" in data
        assert isinstance(data["has_more"], bool)

        # 커서로 전체 순회
        all_keys: list[str] = list(data["keys"])
        cursor = data["cursor"]
        while cursor != 0:
            resp = await client.get(
                f"/api/v1/admin/keys?ns=ERP&type=kv&cursor={cursor}&count=1",
                headers={"X-API-Key": ADMIN_KEY},
            )
            page = resp.json()["data"]
            all_keys.extend(page["keys"])
            cursor = page["cursor"]

        # page- 키 3개가 모두 포함
        page_keys = [k for k in all_keys if "page-" in k]
        assert len(page_keys) >= 3

    async def test_list_keys_ns_denied_for_unauthorized(self, client: AsyncClient):
        """구체 NS 지정 시 read 권한 없는 클라이언트는 403 — 타 NS 키 열거 차단.

        HRM 클라이언트는 CRM NS에 read 권한이 없다(config: HRM/shared/ERP만).
        데이터 플레인과 동일하게 admin 메타데이터 조회도 NS read를 강제한다.
        """
        resp = await client.get(
            "/api/v1/admin/keys?ns=CRM",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"]["code"] == "NAMESPACE_DENIED"

    async def test_list_keys_ns_allowed_for_authorized(self, client: AsyncClient):
        """read 권한이 있는 NS는 정상 조회 — HRM은 ERP에 read 권한 보유."""
        resp = await client.get(
            "/api/v1/admin/keys?ns=ERP",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 200


class TestAdminKeyInfo:
    """키 상세 정보 테스트"""

    async def test_kv_info(self, client: AsyncClient):
        """String 타입 키 상세 정보"""
        await client.put(
            "/api/v1/ns/HRM/kv/status",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "running", "ttl": 3600},
        )

        resp = await client.get(
            "/api/v1/admin/info/HRM/kv/status",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["type"] == "string"
        assert data["ttl"] > 0
        assert "length" in data  # STRLEN

    async def test_map_info(self, client: AsyncClient):
        """Hash 타입 키 상세 정보 — field_count 포함"""
        await client.put(
            "/api/v1/ns/ERP/map/order:2024-001",
            headers={"X-API-Key": ERP_KEY},
            json={"fields": {"type": "일반주문", "status": "처리중", "manager": "kim"}},
        )

        resp = await client.get(
            "/api/v1/admin/info/ERP/map/order:2024-001",
            headers={"X-API-Key": ADMIN_KEY},
        )
        data = resp.json()["data"]
        assert data["type"] == "hash"
        assert data["field_count"] == 3

    async def test_info_not_found(self, client: AsyncClient):
        """존재하지 않는 키 정보 → 404"""
        resp = await client.get(
            "/api/v1/admin/info/HRM/kv/nonexistent",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert resp.status_code == 404

    async def test_info_ns_denied_for_unauthorized(self, client: AsyncClient):
        """단일 키 메타데이터도 read 권한 없는 NS는 403 — HRM은 CRM에 권한 없음.

        키 존재 여부(404)보다 인가 검사(403)가 먼저 — 존재 누설 방지.
        """
        resp = await client.get(
            "/api/v1/admin/info/CRM/kv/whatever",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"]["code"] == "NAMESPACE_DENIED"


def test_storage_prefixes_accepted_as_type():
    """저장 접두어(q·grp·evt)도 타입 토큰으로 수용 — `keys` 출력의 prefix를 그대로
    admin del/info 등에 입력할 수 있어야 한다(보이는 것=입력 가능). kv·map·rank는 기존부터 충족."""
    from app.utils.key_builder import resolve_type_prefix

    # 저장 접두어 자신을 타입으로 입력 → 같은 접두어로 환원(round-trip).
    assert resolve_type_prefix("q") == "q"
    assert resolve_type_prefix("grp") == "grp"
    assert resolve_type_prefix("evt") == "evt"
    # 기존 canonical 타입·별칭도 유지.
    assert resolve_type_prefix("list") == "q"
    assert resolve_type_prefix("string") == "kv"
    assert resolve_type_prefix("stream") == "evt"


class TestAdminTtl:
    """TTL 설정/변경 테스트"""

    async def test_set_ttl(self, client: AsyncClient):
        """키에 TTL 설정"""
        await client.put(
            "/api/v1/ns/HRM/kv/ttl-test",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "test"},
        )

        resp = await client.put(
            "/api/v1/admin/ttl/HRM/kv/ttl-test",
            headers={"X-API-Key": ADMIN_KEY},
            json={"ttl": 600},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["ttl"] > 0

    async def test_persist_ttl(self, client: AsyncClient):
        """
        TTL 제거 (영구 보존).

        ttl=0으로 설정하면 PERSIST 명령으로
        기존 TTL을 제거하여 키가 만료되지 않습니다.
        """
        await client.put(
            "/api/v1/ns/HRM/kv/persist-test",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "keep", "ttl": 60},
        )

        # TTL 제거 (PERSIST)
        resp = await client.put(
            "/api/v1/admin/ttl/HRM/kv/persist-test",
            headers={"X-API-Key": ADMIN_KEY},
            json={"ttl": 0},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["ttl"] == -1  # 영구 보존

    async def test_ttl_not_found(self, client: AsyncClient):
        """존재하지 않는 키에 TTL 설정 → 404"""
        resp = await client.put(
            "/api/v1/admin/ttl/HRM/kv/nonexistent",
            headers={"X-API-Key": ADMIN_KEY},
            json={"ttl": 60},
        )
        assert resp.status_code == 404

    async def test_ttl_requires_admin(self, client: AsyncClient):
        """
        TTL 설정은 admin 권한 필요.

        일반 사용자(HRM_KEY)는 TTL 설정 불가 → 403.
        """
        await client.put(
            "/api/v1/ns/HRM/kv/admin-only",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "test"},
        )
        resp = await client.put(
            "/api/v1/admin/ttl/HRM/kv/admin-only",
            headers={"X-API-Key": HRM_KEY},
            json={"ttl": 60},
        )
        assert resp.status_code == 403


class TestAdminDelete:
    """키 삭제 테스트"""

    async def test_delete_key(self, client: AsyncClient):
        """admin이 키 삭제"""
        await client.put(
            "/api/v1/ns/ERP/kv/to-delete",
            headers={"X-API-Key": ERP_KEY},
            json={"value": "temp"},
        )
        resp = await client.delete(
            "/api/v1/admin/keys/ERP/kv/to-delete",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["deleted"] is True

    async def test_delete_not_found(self, client: AsyncClient):
        """존재하지 않는 키 삭제 → 404"""
        resp = await client.delete(
            "/api/v1/admin/keys/ERP/kv/nonexistent",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert resp.status_code == 404

    async def test_delete_requires_admin(self, client: AsyncClient):
        """일반 사용자는 admin 삭제 불가 → 403"""
        await client.put(
            "/api/v1/ns/ERP/kv/no-delete",
            headers={"X-API-Key": ERP_KEY},
            json={"value": "keep"},
        )
        resp = await client.delete(
            "/api/v1/admin/keys/ERP/kv/no-delete",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 403


class TestAdminStats:
    """네임스페이스별 통계 테스트"""

    async def test_get_stats(self, client: AsyncClient):
        """
        네임스페이스별 통계 조회.

        각 네임스페이스의 키 수, 타입별 분포, Redis 메모리 사용량을 반환합니다.
        """
        # 여러 타입의 데이터 생성
        await client.put(
            "/api/v1/ns/HRM/kv/status",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "running"},
        )
        await client.put(
            "/api/v1/ns/ERP/map/order:2024-001",
            headers={"X-API-Key": ERP_KEY},
            json={"fields": {"type": "일반주문"}},
        )

        resp = await client.get(
            "/api/v1/admin/stats",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "redis_memory" in data
        assert "namespaces" in data
        # 최소 1개 네임스페이스 존재
        assert len(data["namespaces"]) >= 1

    async def test_stats_monitoring_read(self, client: AsyncClient):
        """MONITOR 사용자도 통계 조회 가능 (read 권한)"""
        resp = await client.get(
            "/api/v1/admin/stats",
            headers={"X-API-Key": MONITOR_KEY},
        )
        assert resp.status_code == 200


class TestAdminMonitoringDenied:
    """MONITOR 사용자의 admin 쓰기 작업 거부 테스트"""

    async def test_monitoring_cannot_set_ttl(self, client: AsyncClient):
        """
        MONITOR 사용자는 와일드카드 read 권한만 가지므로
        admin TTL 설정 불가 → 403.
        """
        await client.put(
            "/api/v1/ns/HRM/kv/monitor-test",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "test"},
        )
        resp = await client.put(
            "/api/v1/admin/ttl/HRM/kv/monitor-test",
            headers={"X-API-Key": MONITOR_KEY},
            json={"ttl": 60},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"]["code"] == "ADMIN_REQUIRED"

    async def test_monitoring_cannot_delete_key(self, client: AsyncClient):
        """MONITOR 사용자는 admin 키 삭제 불가 → 403"""
        await client.put(
            "/api/v1/ns/ERP/kv/monitor-del",
            headers={"X-API-Key": ERP_KEY},
            json={"value": "test"},
        )
        resp = await client.delete(
            "/api/v1/admin/keys/ERP/kv/monitor-del",
            headers={"X-API-Key": MONITOR_KEY},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"]["code"] == "ADMIN_REQUIRED"


class TestAdminBulkDelete:
    """관리자 벌크 삭제 테스트"""

    async def test_bulk_delete(self, client: AsyncClient):
        """
        패턴 기반 벌크 삭제 — ns 필수, pattern으로 키 매칭.

        SCAN 기반으로 매칭되는 키를 찾아 일괄 삭제합니다.
        """
        # 테스트 데이터 생성
        for i in range(3):
            await client.put(
                f"/api/v1/ns/HRM/kv/bulk-del-{i}",
                headers={"X-API-Key": HRM_KEY},
                json={"value": f"val-{i}"},
            )

        # 벌크 삭제
        resp = await client.delete(
            "/api/v1/admin/keys/bulk?ns=HRM&type=kv&pattern=bulk-del-*",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["deleted"] >= 3

    async def test_bulk_delete_dry_run(self, client: AsyncClient):
        """
        dry_run=true — 삭제하지 않고 매칭 키 수만 반환.
        """
        await client.put(
            "/api/v1/ns/HRM/kv/dry-test-1",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "temp"},
        )
        resp = await client.delete(
            "/api/v1/admin/keys/bulk?ns=HRM&type=kv&pattern=dry-test-*&dry_run=true",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["count"] >= 1
        assert data["dry_run"] is True

    async def test_bulk_delete_non_admin_denied(self, client: AsyncClient):
        """admin이 아닌 사용자의 벌크 삭제 → 403"""
        resp = await client.delete(
            "/api/v1/admin/keys/bulk?ns=HRM&pattern=*",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 403

    async def test_bulk_delete_rejects_invalid_glob_pattern(self, client: AsyncClient):
        """pattern에 저장 키 charset 밖 glob 문자([, ], \\, 공백)가 있으면 400 INVALID_KEY.

        정상 키에는 이 문자들이 등장할 수 없으므로 거부해도 매칭 손실이 없다(입력단 defense-in-depth).
        """
        for bad in ("foo[ab]", "foo\\bar", "a b"):
            resp = await client.delete(
                "/api/v1/admin/keys/bulk",
                params={"ns": "HRM", "type": "kv", "pattern": bad},
                headers={"X-API-Key": ADMIN_KEY},
            )
            assert resp.status_code == 400, f"pattern={bad!r}"
            assert resp.json()["detail"]["error"]["code"] == "INVALID_KEY"

    async def test_list_keys_rejects_invalid_glob_pattern(self, client: AsyncClient):
        """GET /admin/keys도 동일하게 키 charset 밖 glob pattern을 400으로 거부.

        정상 glob(`*`, `?`)과 키 문자는 통과(test_bulk_delete가 `bulk-del-*`로 커버).
        """
        resp = await client.get(
            "/api/v1/admin/keys",
            params={"ns": "HRM", "type": "kv", "pattern": "a[bc]"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "INVALID_KEY"


class TestAdminMetrics:
    """서비스 메트릭 테스트"""

    async def test_get_metrics(self, client: AsyncClient):
        """
        서비스 메트릭 조회 — 총 요청 수, 상태 코드 분포, 업타임.

        Redis 기반 메트릭은 미들웨어에서 응답 반환 후 기록되므로,
        사전 요청을 보내야 집계된 카운트를 확인할 수 있습니다.
        """
        # 사전 요청: 메트릭이 Redis에 기록되도록 더미 요청 발생.
        # MetricsMiddleware가 fire-and-forget(create_task)로 기록하므로 짧게 대기.
        await client.get("/health")
        import asyncio
        await asyncio.sleep(0.05)

        resp = await client.get(
            "/api/v1/admin/metrics",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "total_requests" in data
        assert data["total_requests"] > 0
        assert "status_codes" in data
        assert "uptime_seconds" in data
        assert data["uptime_seconds"] >= 0

    async def test_metrics_non_admin_denied(self, client: AsyncClient):
        """admin이 아닌 사용자의 메트릭 조회 → 403"""
        resp = await client.get(
            "/api/v1/admin/metrics",
            headers={"X-API-Key": MONITOR_KEY},
        )
        assert resp.status_code == 403


class TestPrometheusMetrics:
    """Prometheus 형식 메트릭 테스트"""

    async def test_prometheus_format(self, client: AsyncClient):
        """
        GET /metrics — Prometheus text format 반환.

        Content-Type: text/plain, HELP/TYPE 주석 + 메트릭 라인 형식.
        Grafana, Prometheus, VictoriaMetrics 등에서 직접 수집 가능.
        """
        # 사전 요청: 메트릭이 Redis에 기록되도록 더미 요청 발생
        await client.get("/health")

        resp = await client.get(
            "/api/v1/metrics",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]

        body = resp.text
        # Prometheus 형식 검증: HELP/TYPE 주석 + 메트릭 라인
        assert "# HELP redgw_requests_total" in body
        assert "# TYPE redgw_requests_total counter" in body
        assert "redgw_requests_total{" in body
        assert "# HELP redgw_uptime_seconds" in body
        assert "redgw_uptime_seconds " in body
        assert "# HELP redgw_redis_memory_bytes" in body
        assert "redgw_redis_memory_bytes " in body

    async def test_prometheus_non_admin_denied(self, client: AsyncClient):
        """admin이 아닌 사용자의 Prometheus 메트릭 조회 → 403"""
        resp = await client.get(
            "/api/v1/metrics",
            headers={"X-API-Key": MONITOR_KEY},
        )
        assert resp.status_code == 403


# ─────────────────────────────────────────────────────────────
# PR-3 신규: /health audit_level 노출 + /admin/health-detail
# ─────────────────────────────────────────────────────────────
class TestHealthAuditLevel:

    @pytest.mark.asyncio
    async def test_health_exposes_audit_level(self, client):
        """공개 /health 응답에 audit_level 키 포함 (인증 없이 접근 가능)."""
        resp = await client.get("/health")
        assert resp.status_code in (200, 503)
        body = resp.json()
        assert "audit_level" in body
        assert body["audit_level"] in (0, 1, 2)

    @pytest.mark.asyncio
    async def test_health_preserves_existing_subsystems(self, client):
        """health 응답에 핵심 서브시스템 키가 유지된다."""
        resp = await client.get("/health")
        body = resp.json()

        assert "status" in body
        assert "redis" in body


class TestHealthDetail:

    @pytest.mark.asyncio
    async def test_health_detail_requires_admin(self, client):
        """admin 키 없으면 미인증(422 또는 401), 일반 키는 403."""
        # 키 없음 — FastAPI Header(...) 필수 의존성 누락 → 422
        resp = await client.get("/api/v1/admin/health-detail")
        assert resp.status_code in (401, 422)
        # 일반 클라이언트
        resp = await client.get(
            "/api/v1/admin/health-detail",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_health_detail_returns_all_fields(self, client):
        """admin 키로 200, 필수 필드 모두 포함."""
        resp = await client.get(
            "/api/v1/admin/health-detail",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert resp.status_code == 200
        body = resp.json()
        expected_keys = {
            "audit_level",
            "audit_directory",
            "max_total_size_mb",
            "retention_days",
            "per_worker_files",
            "payload_prefix_bytes",
            "exclude_paths",
            "queue_size",
            "total_gb",
            "used_gb",
            "free_gb",
            "free_pct",
            "current_usage_mb",
            "file_count",
        }
        assert expected_keys.issubset(body.keys()), (
            f"missing: {expected_keys - body.keys()}"
        )


class TestAuthFailureMetrics:

    @pytest.mark.asyncio
    async def test_auth_fail_counter_increments(self, client):
        """잘못된 API 키 → redgw_auth_failures_total{reason=auth_fail} +1."""
        from app.utils.metrics import get_metrics_async

        before = await get_metrics_async()
        b_count = before.get("auth_failures", {}).get("auth_fail", 0)

        resp = await client.get(
            "/api/v1/ns/HRM/kv/anything",
            headers={"X-API-Key": INVALID_KEY},
        )
        assert resp.status_code == 401

        after = await get_metrics_async()
        a_count = after.get("auth_failures", {}).get("auth_fail", 0)
        assert a_count >= b_count + 1

    @pytest.mark.asyncio
    async def test_namespace_denied_counter_increments(self, client):
        """권한 거부 → namespace_denied 카운터 증가."""
        from app.utils.metrics import get_metrics_async

        before = await get_metrics_async()
        b_count = before.get("auth_failures", {}).get("namespace_denied", 0)

        # MONITORING은 모든 NS read만 가능 → write 시도 시 NAMESPACE_DENIED
        resp = await client.put(
            "/api/v1/ns/HRM/kv/some_key",
            headers={"X-API-Key": MONITOR_KEY, "Content-Type": "application/json"},
            json={"value": "x"},
        )
        assert resp.status_code == 403

        # 짧은 대기 (asyncio.create_task fire-and-forget)
        import asyncio
        await asyncio.sleep(0.05)

        after = await get_metrics_async()
        a_count = after.get("auth_failures", {}).get("namespace_denied", 0)
        assert a_count >= b_count + 1

    @pytest.mark.asyncio
    async def test_admin_required_counter_increments(self, client):
        """admin 요구 거부 → admin_required 카운터 증가."""
        from app.utils.metrics import get_metrics_async

        before = await get_metrics_async()
        b_count = before.get("auth_failures", {}).get("admin_required", 0)

        resp = await client.delete(
            "/api/v1/admin/keys/HRM/kv/foo",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 403

        import asyncio
        await asyncio.sleep(0.05)

        after = await get_metrics_async()
        a_count = after.get("auth_failures", {}).get("admin_required", 0)
        assert a_count >= b_count + 1


def test_build_scan_pattern_three_segments_and_unknown_type():
    """build_scan_pattern은 항상 3세그먼트 패턴을 내고, 미등록 type은 400으로 거부한다."""
    from fastapi import HTTPException
    from app.utils.key_builder import build_scan_pattern

    assert build_scan_pattern(None, None, "*") == "*:*:*"
    assert build_scan_pattern("HRM", None, "*") == "HRM:*:*"
    assert build_scan_pattern("HRM", "kv", "*") == "HRM:kv:*"
    assert build_scan_pattern(None, "hash", "x*") == "*:map:x*"
    # 미등록 type → 조용한 전체타입 확장 대신 INVALID_TYPE(400)
    with pytest.raises(HTTPException) as ei:
        build_scan_pattern("HRM", "bogus", "*")
    assert ei.value.status_code == 400


def test_metrics_ns_label_guard():
    """메트릭 ns 라벨 카디널리티 가드 정규식 — 정상 ns만 통과."""
    from app.middleware import _valid_ns_re

    assert _valid_ns_re.match("HRM")
    assert _valid_ns_re.match("a-b_c1")
    assert not _valid_ns_re.match("a/b")
    assert not _valid_ns_re.match("a:b")
    assert not _valid_ns_re.match("")
    assert not _valid_ns_re.match("x" * 65)


class TestAdminInternalKeyFiltering:
    """admin SCAN이 내부 제어/메트릭 키(__redgw:*, redgw:metrics:*)를 노출·집계하지 않는다."""

    async def test_list_keys_excludes_internal(self, client: AsyncClient):
        from app.redis_client import get_redis_manager
        r = get_redis_manager().get_client()
        await r.set("__redgw:status_monitor:tick", "w1")
        await r.set("__redgw:audit:origin:HRM:kv:x", "meta")
        await r.set("redgw:metrics:status:200", "5")
        await client.put(
            "/api/v1/ns/HRM/kv/visible",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "1"},
        )

        resp = await client.get(
            "/api/v1/admin/keys?pattern=*",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert resp.status_code == 200
        keys = resp.json()["data"]["keys"]
        assert all(not k.startswith("__redgw:") for k in keys)
        assert all(not k.startswith("redgw:metrics:") for k in keys)
        assert "HRM:kv:visible" in keys

    async def test_stats_excludes_internal_namespaces(self, client: AsyncClient):
        from app.redis_client import get_redis_manager
        r = get_redis_manager().get_client()
        await r.set("__redgw:status_monitor:tick", "w1")
        await r.set("redgw:metrics:status:200", "5")
        await client.put(
            "/api/v1/ns/HRM/kv/visible2",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "1"},
        )

        resp = await client.get(
            "/api/v1/admin/stats",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert resp.status_code == 200
        namespaces = resp.json()["data"]["namespaces"]
        assert "__redgw" not in namespaces
        assert "redgw" not in namespaces
        assert "HRM" in namespaces


class TestBulkDeleteInternalKeyProtection:
    """bulk_delete도 내부 제어/메트릭 키를 삭제 대상에서 제외한다(_is_storage_key)."""

    async def test_bulk_delete_excludes_internal_keys(self, client: AsyncClient):
        from app.redis_client import get_redis_manager
        r = get_redis_manager().get_client()
        await r.set("__redgw:status_monitor:tick", "worker-1")
        # ns=__redgw, type 미지정 → 패턴 __redgw:*:* 가 내부키와 매칭하지만 필터로 제외
        resp = await client.delete(
            "/api/v1/admin/keys/bulk?ns=__redgw",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["deleted"] == 0
        assert await r.get("__redgw:status_monitor:tick") == "worker-1"   # 내부키 생존
