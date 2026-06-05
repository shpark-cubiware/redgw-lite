"""
=============================================================================
List (Queue) API 테스트 (test_queue.py)
=============================================================================

Redis List 타입을 사용하는 Queue API 전체 기능을 테스트합니다.

Redis 키 형식: {ns}:q:{key}
엔드포인트:
  POST /ns/{ns}/queue/{key}       — 큐에 추가 (RPUSH/LPUSH)
  GET  /ns/{ns}/queue/{key}/pop   — 큐에서 추출 (LPOP/RPOP)
  GET  /ns/{ns}/queue/{key}       — 범위 조회 (LRANGE)
  GET  /ns/{ns}/queue/{key}/len   — 큐 길이 (LLEN)
  POST /ns/{ns}/queue/{key}/trim  — 트리밍 (LTRIM)

큐 패턴:
  FIFO: RPUSH(right) → LPOP(left) — 먼저 넣은 것이 먼저 나옴
  LIFO: LPUSH(left)  → LPOP(left) — 나중에 넣은 것이 먼저 나옴 (긴급 처리)
=============================================================================
"""

from httpx import AsyncClient

from tests.conftest import HRM_KEY, ERP_KEY, MONITOR_KEY


class TestQueueCrud:
    """Queue 기본 기능 테스트"""

    async def test_push_and_pop_fifo(self, client: AsyncClient):
        """FIFO 큐 — right push → left pop"""
        # 순서대로 push
        await client.post(
            "/api/v1/ns/HRM/queue/hrm-requests",
            headers={"X-API-Key": HRM_KEY},
            json={"value": '{"staff_id":"EMP-001"}', "direction": "right"},
        )
        await client.post(
            "/api/v1/ns/HRM/queue/hrm-requests",
            headers={"X-API-Key": HRM_KEY},
            json={"value": '{"staff_id":"EMP-002"}', "direction": "right"},
        )

        # FIFO pop → 먼저 넣은 EMP-001이 먼저 나옴
        resp = await client.get(
            "/api/v1/ns/HRM/queue/hrm-requests/pop?direction=left",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 200
        assert "EMP-001" in resp.json()["data"]["value"]

    async def test_push_lifo_urgent(self, client: AsyncClient):
        """LIFO 패턴 — left push로 긴급 요청을 큐 앞에 삽입"""
        # 일반 요청 (right push)
        await client.post(
            "/api/v1/ns/HRM/queue/urgent",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "normal-001", "direction": "right"},
        )
        await client.post(
            "/api/v1/ns/HRM/queue/urgent",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "normal-002", "direction": "right"},
        )
        # 긴급 요청 (left push → 맨 앞)
        await client.post(
            "/api/v1/ns/HRM/queue/urgent",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "URGENT", "direction": "left"},
        )

        # left pop → 긴급 요청이 먼저 나옴
        resp = await client.get(
            "/api/v1/ns/HRM/queue/urgent/pop?direction=left",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.json()["data"]["value"] == "URGENT"

    async def test_pop_empty_queue(self, client: AsyncClient):
        """빈 큐에서 pop → 404 QUEUE_EMPTY"""
        resp = await client.get(
            "/api/v1/ns/HRM/queue/empty-q/pop?direction=left",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"]["code"] == "QUEUE_EMPTY"

    async def test_queue_len(self, client: AsyncClient):
        """큐 길이 조회 (LLEN)"""
        await client.post(
            "/api/v1/ns/HRM/queue/test-q",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "item1"},
        )
        await client.post(
            "/api/v1/ns/HRM/queue/test-q",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "item2"},
        )
        resp = await client.get(
            "/api/v1/ns/HRM/queue/test-q/len",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.json()["data"]["length"] == 2

    async def test_queue_range_peek(self, client: AsyncClient):
        """범위 조회 (LRANGE) — 큐에서 꺼내지 않고 peek"""
        for i in range(5):
            await client.post(
                "/api/v1/ns/ERP/queue/log:api-calls",
                headers={"X-API-Key": ERP_KEY},
                json={"value": f"call-{i}"},
            )
        # 상위 3건만 peek
        resp = await client.get(
            "/api/v1/ns/ERP/queue/log:api-calls?start=0&stop=2",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["count"] == 3

    async def test_queue_trim(self, client: AsyncClient):
        """트리밍 — 최근 N건만 유지"""
        for i in range(10):
            await client.post(
                "/api/v1/ns/ERP/queue/log:trim-test",
                headers={"X-API-Key": ERP_KEY},
                json={"value": f"item-{i}"},
            )
        resp = await client.post(
            "/api/v1/ns/ERP/queue/log:trim-test/trim",
            headers={"X-API-Key": ERP_KEY},
            json={"keep": 5},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["length"] == 5


class TestQueueScenarios:
    """실전 시나리오 테스트"""

    async def test_notification_queue(self, client: AsyncClient):
        """알림 큐 — 여러 건 push 후 순차 pop"""
        notifications = [
            '{"type":"order_registered","id":"2024-001"}',
            '{"type":"assign_complete","id":"req-001"}',
            '{"type":"status_changed","id":"2024-001"}',
        ]
        for n in notifications:
            await client.post(
                "/api/v1/ns/ERP/queue/notifications",
                headers={"X-API-Key": ERP_KEY},
                json={"value": n, "direction": "right"},
            )

        # 순서대로 소비
        resp = await client.get(
            "/api/v1/ns/ERP/queue/notifications/pop?direction=left",
            headers={"X-API-Key": ERP_KEY},
        )
        assert "order_registered" in resp.json()["data"]["value"]

    async def test_contact_support_queue(self, client: AsyncClient):
        """거래처 지원 요청 큐"""
        await client.post(
            "/api/v1/ns/ERP/queue/contact-support",
            headers={"X-API-Key": ERP_KEY},
            json={"value": '{"contact_id":"v-001","urgency":"normal"}', "direction": "right"},
        )
        await client.post(
            "/api/v1/ns/ERP/queue/contact-support",
            headers={"X-API-Key": ERP_KEY},
            json={"value": '{"contact_id":"v-002","urgency":"high"}', "direction": "left"},
        )

        # 긴급 요청(v-002)이 먼저 나옴
        resp = await client.get(
            "/api/v1/ns/ERP/queue/contact-support/pop?direction=left",
            headers={"X-API-Key": ERP_KEY},
        )
        assert "v-002" in resp.json()["data"]["value"]

    async def test_log_buffer_with_trim(self, client: AsyncClient):
        """로그 버퍼 — push 후 최근 3건만 유지"""
        for i in range(7):
            await client.post(
                "/api/v1/ns/ERP/queue/log:buffer",
                headers={"X-API-Key": ERP_KEY},
                json={"value": f"log-{i}"},
            )
        # 7건 → 3건으로 트리밍
        await client.post(
            "/api/v1/ns/ERP/queue/log:buffer/trim",
            headers={"X-API-Key": ERP_KEY},
            json={"keep": 3},
        )
        resp = await client.get(
            "/api/v1/ns/ERP/queue/log:buffer/len",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.json()["data"]["length"] == 3
        # 방향 검증: 기본 RPUSH(꼬리 추가)이므로 trim은 '최신 3건'(log-4,5,6)을 남겨야 한다.
        # (LTRIM(0,keep-1)이면 오래된 log-0,1,2가 남아 회귀 — 이 단언이 그것을 잡는다)
        resp = await client.get(
            "/api/v1/ns/ERP/queue/log:buffer?start=0&stop=-1",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.json()["data"]["values"] == ["log-4", "log-5", "log-6"]


class TestQueueFull:
    """큐 최대 길이(QUEUE_FULL) 경계 — Lua 원자적 max 검사 회귀 고정.

    push는 ``llen >= max``, batch는 ``cur+cnt > max``에서 Lua가 -1을 반환하고 라우터가
    400 QUEUE_FULL로 변환한다([app/routers/queue.py]). 이 경계(single ``>=`` vs batch ``>``)와
    batch 원자성(실패 시 부분 push 없음)을 고정한다. ``max_list_length``는 기본 10000이라
    monkeypatch로 작게 낮춰 도달시킨다(get_settings() 싱글턴 속성 변경 — test_config_validator 선례).
    """

    async def test_push_rejects_at_max(self, client: AsyncClient, monkeypatch):
        """single push: max까지 허용하고 max 도달 후 추가 push는 400 QUEUE_FULL, 길이 불변."""
        from app.config import get_settings
        monkeypatch.setattr(get_settings().defaults, "max_list_length", 3)

        for i in range(3):  # llen 0→1→2 (push 전 llen<3이라 모두 허용), 최종 길이 3
            resp = await client.post(
                "/api/v1/ns/HRM/queue/full-q",
                headers={"X-API-Key": HRM_KEY},
                json={"value": f"v-{i}", "direction": "right"},
            )
            assert resp.status_code == 200

        # 4번째: llen==3 >= max(3) → 거부
        resp = await client.post(
            "/api/v1/ns/HRM/queue/full-q",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "overflow", "direction": "right"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "QUEUE_FULL"

        # 거부돼도 길이는 max 그대로 (overflow 미삽입)
        resp = await client.get(
            "/api/v1/ns/HRM/queue/full-q/len",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.json()["data"]["length"] == 3

    async def test_batch_push_atomic_on_overflow(self, client: AsyncClient, monkeypatch):
        """batch push: cur+cnt>max면 400 QUEUE_FULL이고 부분 push가 없어야 한다(원자성)."""
        from app.config import get_settings
        monkeypatch.setattr(get_settings().defaults, "max_list_length", 5)

        for i in range(3):  # seed 3건
            await client.post(
                "/api/v1/ns/HRM/queue/batch-full",
                headers={"X-API-Key": HRM_KEY},
                json={"value": f"seed-{i}", "direction": "right"},
            )

        # cur(3)+cnt(4)=7 > max(5) → 거부 + 부분 적재 없음(원자성)
        resp = await client.post(
            "/api/v1/ns/HRM/queue/batch-full/batch",
            headers={"X-API-Key": HRM_KEY},
            json={"values": ["a", "b", "c", "d"], "direction": "right"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "QUEUE_FULL"

        resp = await client.get(
            "/api/v1/ns/HRM/queue/batch-full/len",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.json()["data"]["length"] == 3  # a~d 어느 것도 안 들어감

        # cur(3)+cnt(2)=5 <= max(5) → 정확히 경계에서 허용
        resp = await client.post(
            "/api/v1/ns/HRM/queue/batch-full/batch",
            headers={"X-API-Key": HRM_KEY},
            json={"values": ["d", "e"], "direction": "right"},
        )
        assert resp.status_code == 200
        resp = await client.get(
            "/api/v1/ns/HRM/queue/batch-full/len",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.json()["data"]["length"] == 5


class TestQueueValueSizeAndTtl:
    """Queue 값 크기 검증 + TTL 테스트"""

    async def test_push_value_too_large(self, client: AsyncClient):
        """값 크기 초과 → 400"""
        large_value = "x" * (1048576 + 1)
        resp = await client.post(
            "/api/v1/ns/HRM/queue/big-q",
            headers={"X-API-Key": HRM_KEY},
            json={"value": large_value},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "VALUE_TOO_LARGE"

    async def test_push_with_ttl(self, client: AsyncClient):
        """TTL 지정 push — 응답에 ttl 포함"""
        resp = await client.post(
            "/api/v1/ns/HRM/queue/ttl-q",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "item1", "ttl": 120},
        )
        assert resp.status_code == 200
        assert resp.json()["meta"]["ttl"] > 0


class TestQueueBatch:
    """Queue 배치 푸시 테스트"""

    async def test_batch_push(self, client: AsyncClient):
        """
        여러 값을 한 번에 큐에 추가.

        Lua 스크립트로 원자적 max_length 검사 후 일괄 RPUSH합니다.
        """
        resp = await client.post(
            "/api/v1/ns/ERP/queue/batch-q/batch",
            headers={"X-API-Key": ERP_KEY},
            json={"values": ["task-1", "task-2", "task-3"], "direction": "right"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["pushed"] == 3
        assert data["length"] == 3

    async def test_batch_push_with_ttl(self, client: AsyncClient):
        """배치 푸시 시 TTL 적용"""
        resp = await client.post(
            "/api/v1/ns/ERP/queue/batch-ttl-q/batch",
            headers={"X-API-Key": ERP_KEY},
            json={"values": ["a", "b"], "ttl": 300},
        )
        assert resp.status_code == 200
        assert resp.json()["meta"]["ttl"] > 0

    async def test_batch_push_value_too_large(self, client: AsyncClient):
        """배치 푸시 시 값 크기 초과 → 400"""
        large_value = "x" * (1048576 + 1)
        resp = await client.post(
            "/api/v1/ns/ERP/queue/batch-big-q/batch",
            headers={"X-API-Key": ERP_KEY},
            json={"values": ["ok", large_value]},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "VALUE_TOO_LARGE"


class TestQueueTouch:
    """TTL 갱신 (Touch) 테스트"""

    async def test_touch_update_ttl(self, client: AsyncClient):
        """Queue TTL 갱신"""
        await client.post(
            "/api/v1/ns/ERP/queue/touch-q",
            headers={"X-API-Key": ERP_KEY},
            json={"value": "item1"},
        )
        resp = await client.put(
            "/api/v1/ns/ERP/queue/touch-q/touch",
            headers={"X-API-Key": ERP_KEY},
            json={"ttl": 7200},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["touched"] is True
        assert resp.json()["meta"]["ttl"] > 0

    async def test_touch_persist(self, client: AsyncClient):
        """TTL 제거 (PERSIST)"""
        await client.post(
            "/api/v1/ns/ERP/queue/persist-q",
            headers={"X-API-Key": ERP_KEY},
            json={"value": "item1"},
        )
        resp = await client.put(
            "/api/v1/ns/ERP/queue/persist-q/touch",
            headers={"X-API-Key": ERP_KEY},
            json={"ttl": 0},
        )
        assert resp.status_code == 200
        assert resp.json()["meta"]["ttl"] == -1

    async def test_touch_not_found(self, client: AsyncClient):
        """존재하지 않는 키 touch → 404"""
        resp = await client.put(
            "/api/v1/ns/ERP/queue/nonexistent/touch",
            headers={"X-API-Key": ERP_KEY},
            json={"ttl": 3600},
        )
        assert resp.status_code == 404


class TestQueueBatchTtlZero:
    """queue 단일키 batch push ttl=0도 기존 TTL을 제거한다(R5 커버리지)."""

    async def test_batch_push_ttl_zero_clears_existing_ttl(self, client: AsyncClient):
        await client.post(
            "/api/v1/ns/HRM/queue/qbtz", headers={"X-API-Key": HRM_KEY},
            json={"value": "a", "ttl": 120},
        )
        resp = await client.post(
            "/api/v1/ns/HRM/queue/qbtz/batch", headers={"X-API-Key": HRM_KEY},
            json={"values": ["b", "c"], "ttl": 0},
        )
        assert resp.status_code == 200
        assert resp.json()["meta"]["ttl"] == -1


class TestQueueDelete:
    """전체 키 삭제 (DEL) — 연결 키의 NS write 권한으로 삭제"""

    async def test_delete_whole_queue(self, client: AsyncClient):
        """큐 생성 후 전체 삭제 → 200, deleted=true. 이후 len=0."""
        await client.post(
            "/api/v1/ns/HRM/queue/del-target",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "a", "direction": "right"},
        )
        resp = await client.delete(
            "/api/v1/ns/HRM/queue/del-target",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["deleted"] is True
        assert body["meta"]["type"] == "list"

        resp = await client.get(
            "/api/v1/ns/HRM/queue/del-target/len",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.json()["data"]["length"] == 0

    async def test_delete_absent_queue_404(self, client: AsyncClient):
        """없는 키 삭제 → 404 KEY_NOT_FOUND."""
        resp = await client.delete(
            "/api/v1/ns/HRM/queue/no-such-key",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"]["code"] == "KEY_NOT_FOUND"

    async def test_delete_requires_write(self, client: AsyncClient):
        """read-only 키(MONITOR)는 전체 삭제 거부 → 403 NAMESPACE_DENIED."""
        await client.post(
            "/api/v1/ns/HRM/queue/ro-guard",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "a", "direction": "right"},
        )
        resp = await client.delete(
            "/api/v1/ns/HRM/queue/ro-guard",
            headers={"X-API-Key": MONITOR_KEY},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"]["code"] == "NAMESPACE_DENIED"
